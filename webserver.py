"""Tiny aiohttp file server for oversized results.

Whenever the pipeline produces a zip larger than the Telegram bot
upload limit, the bot drops it into this server's spool directory and
sends the user a direct download URL instead.

Files are kept on disk for ``RESULT_TTL_SECONDS`` (default 1 hour) and
swept periodically.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from aiohttp import web

log = logging.getLogger(__name__)

DEFAULT_TTL = 3600  # 1 hour


@dataclass
class HostedFile:
    token: str
    filename: str
    path: Path
    created_at: float


class FileHost:
    """Serve files from ``spool_dir`` at ``/files/<token>/<filename>``."""

    def __init__(
        self,
        spool_dir: Path,
        *,
        host: str = "0.0.0.0",
        port: int = 8080,
        public_base_url: Optional[str] = None,
        ttl_seconds: int = DEFAULT_TTL,
    ) -> None:
        self.spool_dir = spool_dir
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.port = port
        self._configured_base_url = (public_base_url or "").rstrip("/")
        self.ttl_seconds = ttl_seconds
        self._files: dict[str, HostedFile] = {}
        self._runner: Optional[web.AppRunner] = None
        self._cleanup_task: Optional[asyncio.Task[None]] = None

    # ------------------------------------------------------------------
    # Hosting
    # ------------------------------------------------------------------
    def host_file(self, source: Path, *, filename: Optional[str] = None) -> str:
        """Copy ``source`` into the spool and return its public URL."""
        token = secrets.token_urlsafe(12)
        target_dir = self.spool_dir / token
        target_dir.mkdir(parents=True, exist_ok=True)
        out_name = filename or source.name
        target = target_dir / out_name
        shutil.copy2(source, target)
        self._files[token] = HostedFile(
            token=token,
            filename=out_name,
            path=target,
            created_at=time.time(),
        )
        return self.url_for(token, out_name)

    def url_for(self, token: str, filename: str) -> str:
        base = self._configured_base_url or f"http://{self.host}:{self.port}"
        return f"{base}/files/{token}/{filename}"

    # ------------------------------------------------------------------
    # aiohttp lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._on_root)
        app.router.add_get("/health", self._on_health)
        app.router.add_get(
            "/files/{token}/{filename}", self._on_file
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=self.host, port=self.port)
        await site.start()
        self._runner = runner
        self._cleanup_task = asyncio.create_task(self._sweep_loop())
        log.info(
            "file host listening on http://%s:%s (ttl=%ds, base=%s)",
            self.host,
            self.port,
            self.ttl_seconds,
            self._configured_base_url or "<auto>",
        )

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._cleanup_task = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------
    async def _on_root(self, _: web.Request) -> web.Response:
        return web.Response(text="logs-to-cookie file host\n")

    async def _on_health(self, _: web.Request) -> web.Response:
        return web.json_response({"ok": True, "files": len(self._files)})

    async def _on_file(self, req: web.Request) -> web.StreamResponse:
        token = req.match_info["token"]
        filename = req.match_info["filename"]
        record = self._files.get(token)
        if record is None or record.filename != filename:
            raise web.HTTPNotFound(text="link expired or invalid")
        if not record.path.exists():
            raise web.HTTPNotFound(text="file no longer available")
        return web.FileResponse(
            record.path,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{record.filename}"'
                ),
            },
        )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(max(60, self.ttl_seconds // 4 or 60))
                self._sweep_once()
        except asyncio.CancelledError:
            return

    def _sweep_once(self) -> None:
        now = time.time()
        expired: list[str] = []
        for token, record in self._files.items():
            if now - record.created_at > self.ttl_seconds:
                expired.append(token)
        for token in expired:
            record = self._files.pop(token, None)
            if record is None:
                continue
            shutil.rmtree(record.path.parent, ignore_errors=True)
            log.info("expired hosted file %s (%s)", token, record.filename)


def from_env(spool_dir: Path) -> Tuple[FileHost, str]:
    """Build a :class:`FileHost` from environment variables.

    Returns ``(host, public_base_url)`` so the caller can log the
    effective base URL even when it was auto-derived.
    """
    host = os.getenv("WEBSERVER_HOST", "0.0.0.0")
    port = int(os.getenv("WEBSERVER_PORT", "8080"))
    public = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    ttl = int(os.getenv("RESULT_TTL_SECONDS", str(DEFAULT_TTL)))
    server = FileHost(
        spool_dir=spool_dir,
        host=host,
        port=port,
        public_base_url=public,
        ttl_seconds=ttl,
    )
    base = public or f"http://{host}:{port}"
    return server, base
