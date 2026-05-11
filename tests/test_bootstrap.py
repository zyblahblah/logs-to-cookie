"""Tests for the runtime 7zz bootstrap.

These tests never reach out to www.7-zip.org. ``conftest.py`` sets
``LOGS_TO_COOKIE_DISABLE_7ZZ_BOOTSTRAP=1`` so the bootstrap is a
no-op by default; individual tests opt in by clearing the var,
overriding the tarball URL with a local ``file://`` URL, and
resetting the module's internal cache.
"""

from __future__ import annotations

import io
import lzma
import os
import stat
import tarfile
from pathlib import Path
from typing import Callable

import pytest

from pipeline import bootstrap


@pytest.fixture
def reset_bootstrap(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Reset the bootstrap state + clear the disable env var so the
    function actually runs. The cache dir is also pinned via env var
    so the test has a deterministic destination.
    """

    def _do_reset(cache_dir: Path | None = None) -> None:
        bootstrap.reset_for_tests()
        monkeypatch.delenv(
            "LOGS_TO_COOKIE_DISABLE_7ZZ_BOOTSTRAP", raising=False
        )
        if cache_dir is not None:
            monkeypatch.setenv(
                "LOGS_TO_COOKIE_7ZZ_CACHE_DIR", str(cache_dir)
            )

    return _do_reset


def _make_fake_7zz_tarball(payload: bytes) -> bytes:
    """Build a minimal ``.tar.xz`` containing a single ``7zz`` entry."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tf:
        info = tarfile.TarInfo(name="7zz")
        info.size = len(payload)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(payload))
    return lzma.compress(raw.getvalue())


def test_disabled_by_env_var_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The env var defaults to ``1`` in ``conftest.py`` — bootstrap
    must return ``None`` without touching the network."""
    bootstrap.reset_for_tests()
    monkeypatch.setenv("LOGS_TO_COOKIE_DISABLE_7ZZ_BOOTSTRAP", "1")
    monkeypatch.setenv("LOGS_TO_COOKIE_7ZZ_CACHE_DIR", str(tmp_path))
    assert bootstrap.ensure_bundled_7zz() is None


def test_downloads_extracts_and_verifies(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_bootstrap: Callable[[Path | None], None],
) -> None:
    """End-to-end: a ``file://`` URL serves a fake tarball; the
    bootstrap extracts ``7zz`` and verifies its banner.

    The fake ``7zz`` is a tiny shell script that echoes a 7-Zip
    banner so :func:`_verify_7zz` accepts it.
    """
    fake_7zz_body = b"#!/bin/sh\necho '7-Zip (fake) for tests' && exit 0\n"
    tarball = _make_fake_7zz_tarball(fake_7zz_body)
    tarball_path = tmp_path / "fake.tar.xz"
    tarball_path.write_bytes(tarball)

    cache_dir = tmp_path / "cache"
    reset_bootstrap(cache_dir)
    monkeypatch.setenv(
        "LOGS_TO_COOKIE_7ZZ_TARBALL_URL",
        tarball_path.as_uri(),
    )

    path = bootstrap.ensure_bundled_7zz()
    assert path is not None, "bootstrap should have produced a path"
    assert Path(path).is_file()
    # Binary is chmod +x.
    mode = os.stat(path).st_mode
    assert mode & stat.S_IXUSR, "bootstrapped binary must be executable"
    assert Path(path) == cache_dir / "7zz"
    assert Path(path).read_bytes() == fake_7zz_body


def test_cached_path_is_reused_across_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_bootstrap: Callable[[Path | None], None],
) -> None:
    """A second call must return the same path without re-downloading
    (we'd notice the re-download because the URL is invalidated)."""
    fake_7zz_body = b"#!/bin/sh\necho '7-Zip (fake)' && exit 0\n"
    tarball_path = tmp_path / "fake.tar.xz"
    tarball_path.write_bytes(_make_fake_7zz_tarball(fake_7zz_body))

    cache_dir = tmp_path / "cache"
    reset_bootstrap(cache_dir)
    monkeypatch.setenv(
        "LOGS_TO_COOKIE_7ZZ_TARBALL_URL",
        tarball_path.as_uri(),
    )

    first = bootstrap.ensure_bundled_7zz()
    assert first is not None
    # Invalidate the tarball URL so a second download would fail.
    monkeypatch.setenv(
        "LOGS_TO_COOKIE_7ZZ_TARBALL_URL",
        "file:///definitely/does/not/exist.tar.xz",
    )
    second = bootstrap.ensure_bundled_7zz()
    assert second == first


def test_existing_cached_binary_is_picked_up_without_redownload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_bootstrap: Callable[[Path | None], None],
) -> None:
    """If ``<cache>/7zz`` already exists and passes verification, the
    bootstrap must NOT issue a fresh download."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    existing = cache_dir / "7zz"
    existing.write_text(
        "#!/bin/sh\necho '7-Zip (already cached)' && exit 0\n",
        encoding="utf-8",
    )
    existing.chmod(0o755)

    reset_bootstrap(cache_dir)
    monkeypatch.setenv(
        "LOGS_TO_COOKIE_7ZZ_TARBALL_URL",
        "file:///would/blow/up/if/used.tar.xz",
    )
    path = bootstrap.ensure_bundled_7zz()
    assert path == str(existing)
    assert existing.read_text(encoding="utf-8").startswith("#!/bin/sh")


def test_failed_download_returns_none_and_caches_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_bootstrap: Callable[[Path | None], None],
) -> None:
    """A network failure must not raise. Subsequent calls also return
    ``None`` (without retrying) until the process restarts."""
    cache_dir = tmp_path / "cache"
    reset_bootstrap(cache_dir)
    monkeypatch.setenv(
        "LOGS_TO_COOKIE_7ZZ_TARBALL_URL",
        "file:///definitely/does/not/exist.tar.xz",
    )
    assert bootstrap.ensure_bundled_7zz() is None
    # And again — no retry.
    assert bootstrap.ensure_bundled_7zz() is None


def test_failed_verification_discards_partial_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_bootstrap: Callable[[Path | None], None],
) -> None:
    """If the extracted binary doesn't print a 7-Zip banner, the
    bootstrap deletes it instead of caching garbage."""
    # A body that exits 0 but never mentions "7-Zip" — verification
    # must reject it.
    bogus_body = b"#!/bin/sh\necho 'not the binary you expected' && exit 0\n"
    tarball_path = tmp_path / "bogus.tar.xz"
    tarball_path.write_bytes(_make_fake_7zz_tarball(bogus_body))

    cache_dir = tmp_path / "cache"
    reset_bootstrap(cache_dir)
    monkeypatch.setenv(
        "LOGS_TO_COOKIE_7ZZ_TARBALL_URL",
        tarball_path.as_uri(),
    )

    assert bootstrap.ensure_bundled_7zz() is None
    assert not (cache_dir / "7zz").exists(), (
        "verification failure must discard the bogus binary"
    )


def test_archive_extractor_chain_prepends_bundled_7zz(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reset_bootstrap: Callable[[Path | None], None],
) -> None:
    """``_resolve_7z_candidates`` puts the bundled binary first.

    Verified without invoking the real extractor — we stub
    :func:`pipeline.bootstrap.ensure_bundled_7zz` to return a fake
    path and pretend ``7z`` lives elsewhere on ``PATH``.
    """
    from pipeline import archive as A

    bundled = tmp_path / "bundled" / "7zz"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("#!/bin/sh\necho '7-Zip fake'\n")
    bundled.chmod(0o755)

    monkeypatch.setattr(A, "ensure_bundled_7zz", lambda: str(bundled))
    monkeypatch.setattr(A, "_all_on_path", lambda _cands: ["/usr/bin/7z"])

    chain = A._resolve_7z_candidates()
    assert chain[0] == str(bundled)
    assert chain[1] == "/usr/bin/7z"


def test_archive_extractor_chain_dedupes_same_realpath(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """When the bundled path AND a PATH entry resolve to the same
    physical file, ``_resolve_7z_candidates`` must keep only one
    copy (the bundled one)."""
    from pipeline import archive as A

    real = tmp_path / "7zz"
    real.write_text("#!/bin/sh\necho '7-Zip'\n")
    real.chmod(0o755)
    symlink = tmp_path / "alias"
    symlink.symlink_to(real)

    monkeypatch.setattr(A, "ensure_bundled_7zz", lambda: str(real))
    monkeypatch.setattr(A, "_all_on_path", lambda _cands: [str(symlink)])

    chain = A._resolve_7z_candidates()
    # Bundled wins; the symlink is deduped out.
    assert chain == [str(real)]
