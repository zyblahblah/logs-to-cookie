import json

from logs_to_cookie.cookies import (
    parse_json_cookies,
    parse_netscape,
    to_netscape_line,
)


def test_parse_netscape_basic():
    text = (
        "# Netscape HTTP Cookie File\n"
        ".example.com\tTRUE\t/\tFALSE\t1900000000\tsid\tabc123\n"
        "#HttpOnly_other.test\tFALSE\t/path\tTRUE\t0\tk\tv\n"
    )
    out = list(parse_netscape(text))
    assert len(out) == 2
    assert out[0]["domain"] == ".example.com"
    assert out[0]["hostOnly"] is False
    assert out[0]["secure"] is False
    assert out[0]["name"] == "sid"
    assert out[1]["domain"] == "other.test"
    assert out[1]["hostOnly"] is True
    assert out[1]["secure"] is True


def test_parse_json_cookies_extension_format():
    data = [
        {
            "domain": ".example.com",
            "hostOnly": False,
            "name": "sid",
            "value": "abc",
            "path": "/",
            "secure": True,
            "expirationDate": 1900000000.5,
        },
        {
            "domain": "host.test",
            "name": "k",
            "value": "v",
        },
    ]
    out = list(parse_json_cookies(json.dumps(data)))
    assert out[0]["expires"] == 1900000000
    assert out[0]["secure"] is True
    assert out[1]["hostOnly"] is True  # domain has no leading dot


def test_to_netscape_line_roundtrip():
    cookie = {
        "domain": "example.com",
        "hostOnly": False,
        "path": "/",
        "secure": True,
        "expires": 1700000000,
        "name": "sid",
        "value": "v",
    }
    line = to_netscape_line(cookie)
    parts = line.split("\t")
    assert parts[0] == ".example.com"
    assert parts[1] == "TRUE"
    assert parts[3] == "TRUE"
    assert parts[4] == "1700000000"
