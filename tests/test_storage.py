"""Tests for the JSON-backed AccessStore (VIPs + redemption keys)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pipeline.storage import AccessStore


def test_starts_empty(tmp_path: Path) -> None:
    s = AccessStore(tmp_path / "state.json")
    assert s.list_vips() == []
    assert s.list_keys() == []
    assert not s.is_vip(1)


def test_add_remove_vip_persists(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    s = AccessStore(p)
    assert s.add_vip(101)
    # Idempotent re-add returns False without duplicating.
    assert not s.add_vip(101)
    assert s.add_vip(202)
    assert sorted(s.list_vips()) == [101, 202]
    assert s.is_vip(101)
    assert s.is_vip(202)

    # Reload from disk — state should round-trip.
    s2 = AccessStore(p)
    assert sorted(s2.list_vips()) == [101, 202]

    assert s2.remove_vip(101)
    assert not s2.remove_vip(999)
    assert sorted(s2.list_vips()) == [202]


def test_generate_key_no_expiry(tmp_path: Path) -> None:
    s = AccessStore(tmp_path / "state.json")
    k = s.generate_key()
    assert k.key
    assert k.expires_at is None
    assert k.note is None
    assert not k.is_expired()
    assert not k.is_redeemed()
    listed = s.list_keys()
    assert len(listed) == 1
    assert listed[0].key == k.key


def test_generate_key_with_expiry(tmp_path: Path) -> None:
    s = AccessStore(tmp_path / "state.json")
    k = s.generate_key(valid_days=7, note="vip beta")
    assert k.expires_at is not None
    assert k.expires_at > int(time.time())
    assert k.note == "vip beta"


def test_redeem_key_adds_vip(tmp_path: Path) -> None:
    s = AccessStore(tmp_path / "state.json")
    k = s.generate_key()
    redeemed = s.redeem_key(k.key, user_id=42)
    assert redeemed.is_redeemed()
    assert redeemed.redeemed_by == 42
    assert s.is_vip(42)


def test_redeem_unknown_key_raises(tmp_path: Path) -> None:
    s = AccessStore(tmp_path / "state.json")
    with pytest.raises(KeyError):
        s.redeem_key("definitely-not-a-key", user_id=1)


def test_redeem_twice_rejects(tmp_path: Path) -> None:
    s = AccessStore(tmp_path / "state.json")
    k = s.generate_key()
    s.redeem_key(k.key, user_id=1)
    with pytest.raises(ValueError):
        s.redeem_key(k.key, user_id=2)


def test_redeem_expired_key_rejects(tmp_path: Path) -> None:
    s = AccessStore(tmp_path / "state.json")
    k = s.generate_key(valid_days=1)
    # Backdate the expiry so it's in the past.
    info = s.get_key(k.key)
    assert info is not None
    info.expires_at = int(time.time()) - 10
    # Save through the normal API by toggling vips (forces save).
    s.add_vip(999)
    with pytest.raises(ValueError):
        s.redeem_key(k.key, user_id=1)


def test_remove_key(tmp_path: Path) -> None:
    s = AccessStore(tmp_path / "state.json")
    k = s.generate_key()
    assert s.remove_key(k.key)
    assert not s.remove_key(k.key)
    assert s.get_key(k.key) is None


def test_atomic_write_creates_valid_json(tmp_path: Path) -> None:
    p = tmp_path / "nested" / "state.json"
    s = AccessStore(p)
    s.add_vip(7)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert "vips" in raw and "keys" in raw
    assert raw["vips"] == [7]


def test_handles_corrupt_state_file(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text("not json", encoding="utf-8")
    # Should silently start empty rather than crash.
    s = AccessStore(p)
    assert s.list_vips() == []
    s.add_vip(1)
    assert s.list_vips() == [1]
