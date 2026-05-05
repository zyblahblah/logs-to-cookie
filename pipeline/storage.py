"""Persistent state for VIP allow-listing and one-shot redemption keys.

The bot used to gate access purely by ``ADMIN_IDS`` (env var). To keep
that knob useful without forcing a redeploy every time a new user is
added, we layer a small JSON-backed store on top:

* **VIPs** — Telegram user IDs allowed to use the bot.
* **Keys** — opaque, single-use tokens an admin generates with
  ``/genkey``. A user redeems one with ``/redeem <key>`` to add
  themselves to the VIP list.

State lives in a single JSON file (``STATE_PATH``) and is written
atomically (write-temp + ``os.replace``). All methods are
``threading.Lock``-guarded so the store is safe to call from both the
asyncio main thread and the pipeline worker thread.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class KeyInfo:
    """One redemption key, as stored on disk."""

    key: str
    created_at: int  # unix epoch seconds
    expires_at: Optional[int] = None  # unix epoch seconds, or None for forever
    note: Optional[str] = None
    redeemed_by: Optional[int] = None  # telegram user id, or None
    redeemed_at: Optional[int] = None

    def is_expired(self, now: Optional[int] = None) -> bool:
        if self.expires_at is None:
            return False
        return (now if now is not None else int(time.time())) >= self.expires_at

    def is_redeemed(self) -> bool:
        return self.redeemed_by is not None


@dataclass
class _State:
    vips: List[int] = field(default_factory=list)
    keys: Dict[str, KeyInfo] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "vips": list(self.vips),
            "keys": {k: asdict(v) for k, v in self.keys.items()},
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "_State":
        vips_raw = raw.get("vips") or []
        vips: List[int] = []
        for v in vips_raw:
            try:
                vips.append(int(v))
            except (TypeError, ValueError):
                continue

        keys_raw = raw.get("keys") or {}
        keys: Dict[str, KeyInfo] = {}
        for k, info in keys_raw.items():
            if not isinstance(info, dict):
                continue
            try:
                keys[str(k)] = KeyInfo(
                    key=str(info.get("key") or k),
                    created_at=int(info.get("created_at") or 0),
                    expires_at=(
                        int(info["expires_at"])
                        if info.get("expires_at") is not None
                        else None
                    ),
                    note=info.get("note"),
                    redeemed_by=(
                        int(info["redeemed_by"])
                        if info.get("redeemed_by") is not None
                        else None
                    ),
                    redeemed_at=(
                        int(info["redeemed_at"])
                        if info.get("redeemed_at") is not None
                        else None
                    ),
                )
            except (TypeError, ValueError):
                continue
        return cls(vips=vips, keys=keys)


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class AccessStore:
    """Thread-safe, JSON-backed VIP + key store."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._state = _State()
        self._loaded = False
        self._load_locked()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load_locked(self) -> None:
        if not self.path.exists():
            self._loaded = True
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning(
                "AccessStore: could not load %s (%s); starting empty",
                self.path,
                exc,
            )
            self._state = _State()
            self._loaded = True
            return
        if not isinstance(raw, dict):
            log.warning(
                "AccessStore: %s is not a JSON object; starting empty",
                self.path,
            )
            self._state = _State()
            self._loaded = True
            return
        self._state = _State.from_dict(raw)
        self._loaded = True

    def _save_locked(self) -> None:
        _atomic_write_json(self.path, self._state.to_dict())

    # ------------------------------------------------------------------
    # VIP API
    # ------------------------------------------------------------------
    def add_vip(self, user_id: int) -> bool:
        """Add ``user_id`` to the VIP list. Returns False if already VIP."""
        with self._lock:
            if user_id in self._state.vips:
                return False
            self._state.vips.append(int(user_id))
            self._save_locked()
            return True

    def remove_vip(self, user_id: int) -> bool:
        """Remove ``user_id`` from the VIP list. Returns False if missing."""
        with self._lock:
            if user_id not in self._state.vips:
                return False
            self._state.vips = [v for v in self._state.vips if v != user_id]
            self._save_locked()
            return True

    def is_vip(self, user_id: int) -> bool:
        with self._lock:
            return int(user_id) in self._state.vips

    def list_vips(self) -> List[int]:
        with self._lock:
            return list(self._state.vips)

    # ------------------------------------------------------------------
    # Key API
    # ------------------------------------------------------------------
    @staticmethod
    def _generate_token() -> str:
        # 24 url-safe chars ≈ 144 bits of entropy. Plenty for one-shot
        # redemption keys; readable enough to paste into Telegram.
        return secrets.token_urlsafe(18)

    def generate_key(
        self,
        *,
        valid_days: Optional[int] = None,
        note: Optional[str] = None,
    ) -> KeyInfo:
        """Mint a fresh redemption key and persist it."""
        now = int(time.time())
        expires_at: Optional[int]
        if valid_days is None or valid_days <= 0:
            expires_at = None
        else:
            expires_at = now + int(valid_days) * 86400
        with self._lock:
            # Loop on the off-chance ``token_urlsafe`` collides with an
            # existing key (probability is astronomically small but the
            # cost to handle it is one extra iteration).
            for _ in range(8):
                token = self._generate_token()
                if token not in self._state.keys:
                    break
            else:  # pragma: no cover — defensive only
                raise RuntimeError("could not allocate unique key")
            info = KeyInfo(
                key=token,
                created_at=now,
                expires_at=expires_at,
                note=note,
            )
            self._state.keys[token] = info
            self._save_locked()
            return info

    def remove_key(self, key: str) -> bool:
        with self._lock:
            if key not in self._state.keys:
                return False
            del self._state.keys[key]
            self._save_locked()
            return True

    def redeem_key(self, key: str, user_id: int) -> KeyInfo:
        """Redeem ``key`` for ``user_id``.

        Raises :class:`KeyError` if the key doesn't exist, ``ValueError``
        if it's expired or already redeemed. On success the user is
        added to the VIP list and the redemption is marked on the key.
        """
        now = int(time.time())
        with self._lock:
            info = self._state.keys.get(key)
            if info is None:
                raise KeyError(key)
            if info.is_redeemed():
                raise ValueError("key already redeemed")
            if info.is_expired(now=now):
                raise ValueError("key has expired")
            info.redeemed_by = int(user_id)
            info.redeemed_at = now
            if user_id not in self._state.vips:
                self._state.vips.append(int(user_id))
            self._save_locked()
            return info

    def list_keys(self) -> List[KeyInfo]:
        with self._lock:
            return list(self._state.keys.values())

    def get_key(self, key: str) -> Optional[KeyInfo]:
        with self._lock:
            return self._state.keys.get(key)
