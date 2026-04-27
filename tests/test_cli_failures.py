"""Regression tests: when every input archive fails extraction (e.g.
wrong password), the CLI must exit with a non-zero error code rather
than silently produce empty output. The bot relies on this to surface
failures in chat instead of uploading an empty result zip.
"""
import argparse

from logs_to_cookie import cli


class _FakeStack:
    def enter_context(self, cm):
        return cm.__enter__()


def test_cmd_sort_returns_3_when_all_archives_fail(tmp_path, monkeypatch, capsys):
    fake = tmp_path / "fake.zip"
    fake.write_bytes(b"\x00\x00\x00\x00")  # not a real archive

    def fake_resolve(stack, input_path, passwords, *, workers=4):
        return [], [fake]

    monkeypatch.setattr(cli, "_resolve_roots", fake_resolve)

    args = argparse.Namespace(
        input=str(fake),
        password=["wrong"],
        keywords="netflix",
        output=str(tmp_path / "out"),
        per_source=False,
        workers=1,
    )
    rc = cli.cmd_sort(args)
    assert rc == 3
    err = capsys.readouterr().err
    assert "no archives could be extracted" in err


def test_cmd_cookies_returns_3_when_all_archives_fail(tmp_path, monkeypatch, capsys):
    fake = tmp_path / "fake.zip"
    fake.write_bytes(b"\x00\x00\x00\x00")

    monkeypatch.setattr(
        cli, "_resolve_roots", lambda *a, **k: ([], [fake])
    )
    args = argparse.Namespace(
        input=str(fake),
        password=["wrong"],
        filter=None,
        format="netscape",
        per_source=False,
        output=str(tmp_path / "out"),
        workers=1,
        no_dedupe=False,
    )
    rc = cli.cmd_cookies(args)
    assert rc == 3
    assert "no archives could be extracted" in capsys.readouterr().err


def test_cmd_ulp_returns_3_when_all_archives_fail(tmp_path, monkeypatch, capsys):
    fake = tmp_path / "fake.zip"
    fake.write_bytes(b"\x00\x00\x00\x00")

    monkeypatch.setattr(cli, "_resolve_roots", lambda *a, **k: ([], [fake]))
    args = argparse.Namespace(
        input=str(fake),
        password=["wrong"],
        filter=None,
        output=str(tmp_path / "out.txt"),
        no_dedupe=False,
        workers=1,
    )
    rc = cli.cmd_ulp(args)
    assert rc == 3
    assert "no archives could be extracted" in capsys.readouterr().err
