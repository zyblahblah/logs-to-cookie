import json
from pathlib import Path

from logs_to_cookie.sorter import sort_logs


def _make_sample_logs(root: Path) -> None:
    victim = root / "victim01"
    (victim / "Cookies").mkdir(parents=True)
    (victim / "Passwords.txt").write_text(
        "URL: https://www.netflix.com/login\nUSER: a@x.com\nPASS: pw1\n\n"
        "URL: https://accounts.spotify.com/\nUSER: b\nPASS: pw2\n\n"
        "URL: https://random.test/\nUSER: c\nPASS: pw3\n",
        encoding="utf-8",
    )
    (victim / "Cookies" / "netflix.txt").write_text(
        "# Netscape HTTP Cookie File\n"
        ".netflix.com\tTRUE\t/\tTRUE\t1900000000\tNetflixId\tabc\n",
        encoding="utf-8",
    )
    (victim / "Cookies" / "spotify.json").write_text(
        json.dumps(
            [
                {
                    "domain": ".spotify.com",
                    "name": "sp_dc",
                    "value": "xyz",
                    "path": "/",
                    "secure": True,
                    "expirationDate": 1900000000,
                }
            ]
        ),
        encoding="utf-8",
    )


def test_sort_logs(tmp_path: Path) -> None:
    src = tmp_path / "logs"
    src.mkdir()
    _make_sample_logs(src)
    out = tmp_path / "out"

    stats = sort_logs(src, out, ["netflix", "spotify"])
    assert stats == {"netflix": (1, 1), "spotify": (1, 1)}

    nf_ulp = (out / "netflix.ulp.txt").read_text(encoding="utf-8")
    assert "netflix.com/login:a@x.com:pw1" in nf_ulp
    nf_cookies = (out / "netflix.cookies.txt").read_text(encoding="utf-8")
    assert "NetflixId" in nf_cookies

    sp_ulp = (out / "spotify.ulp.txt").read_text(encoding="utf-8")
    assert "spotify.com" in sp_ulp
    sp_cookies = (out / "spotify.cookies.txt").read_text(encoding="utf-8")
    assert "sp_dc" in sp_cookies
