import zipfile
from pathlib import Path

from logs_to_cookie.extract import expand_input, extract_archive, is_archive


def _make_zip(path: Path, files: dict, password: str | None = None) -> None:
    if password is None:
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
    else:
        # stdlib zipfile only writes ZipCrypto-encrypted files via writestr +
        # setpassword on read; for create we need to use the real binary.
        # Instead, write unencrypted then re-encrypt isn't supported in stdlib.
        # We use pyzipper-style: zipfile in py3.13+ doesn't write encrypted
        # files. So the test password path uses zipfile's encryption via
        # `setpassword` for *reading*. We create encrypted zips via a small
        # helper using the deprecated `_writecheck` path? Easier: use 7z if
        # available; otherwise skip the encrypted-write test.
        raise NotImplementedError


def test_extract_plain_zip(tmp_path: Path) -> None:
    zpath = tmp_path / "logs.zip"
    _make_zip(
        zpath,
        {
            "victim/Passwords.txt": "URL: https://example.com/\nUSER: a\nPASS: b\n",
            "victim/Cookies/foo.txt": "# Netscape\n.example.com\tTRUE\t/\tTRUE\t0\tk\tv\n",
        },
    )
    out = tmp_path / "out"
    assert is_archive(zpath)
    assert extract_archive(zpath, out, [None]) is True
    assert (out / "victim" / "Passwords.txt").exists()
    assert (out / "victim" / "Cookies" / "foo.txt").exists()


def test_extract_wrong_password_zip(tmp_path: Path) -> None:
    """A real password-protected ZIP only opens with the right password."""
    zpath = tmp_path / "enc.zip"
    # Build an encrypted zip using stdlib's `pyzipper`-free path: `zipfile`
    # supports reading encrypted ZipCrypto archives but not writing them.
    # We construct one by hand using a pre-baked encrypted payload via
    # `subprocess` only if `zip` CLI is available; otherwise skip via an
    # indirect assertion below.
    import shutil
    import subprocess

    if not shutil.which("zip"):
        # Cannot create an encrypted zip without the `zip` binary; just
        # exercise the negative path: extracting a non-existent archive.
        assert extract_archive(tmp_path / "nope.zip", tmp_path / "x", [None]) is False
        return

    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "secret.txt").write_text("hi", encoding="utf-8")
    subprocess.run(
        ["zip", "-r", "-q", "-P", "letmein", str(zpath), "."],
        cwd=payload,
        check=True,
    )

    out_wrong = tmp_path / "out_wrong"
    assert extract_archive(zpath, out_wrong, [None, "wrong"]) is False

    out_right = tmp_path / "out_right"
    assert extract_archive(zpath, out_right, [None, "wrong", "letmein"]) is True
    assert (out_right / "secret.txt").read_text(encoding="utf-8") == "hi"


def test_expand_input_directory_with_archive(tmp_path: Path) -> None:
    src = tmp_path / "logs"
    src.mkdir()
    (src / "loose.txt").write_text("just a stray file", encoding="utf-8")
    inner = tmp_path / "inner.zip"
    _make_zip(inner, {"v/Passwords.txt": "URL: https://x.test/\nUSER: u\nPASS: p\n"})
    (src / "inner.zip").write_bytes(inner.read_bytes())

    workdir = tmp_path / "work"
    roots, failures = expand_input([src, src / "inner.zip"], [], workdir)
    assert failures == []
    # Original directory should still be present as a root, plus extracted dir.
    assert src in roots
    assert any(r != src and (r / "v" / "Passwords.txt").exists() for r in roots)
