from logs_to_cookie.ulp import parse_credentials_text


def test_parse_block_format():
    text = """SOFT: Google_[Chrome]_Default
URL: https://example.com/login
USER: alice@example.com
PASS: hunter2
Application: Google_[Chrome]_Default

URL: https://other.test/
USER: bob
PASS: p@ss:wd
"""
    out = list(parse_credentials_text(text))
    assert ("https://example.com/login", "alice@example.com", "hunter2") in out
    assert ("https://other.test/", "bob", "p@ss:wd") in out


def test_parse_redline_style():
    text = """URL: https://shop.example/
Username: shopper
Password: 12345

URL: https://api.example/
Username: api
Password: zzz
"""
    out = list(parse_credentials_text(text))
    assert len(out) == 2
    assert out[0] == ("https://shop.example/", "shopper", "12345")


def test_parse_inline_ulp():
    text = "https://foo.test/login:user1:pass1\nhttps://bar.test:443/x:user2:pa:ss\n"
    out = list(parse_credentials_text(text))
    assert ("https://foo.test/login", "user1", "pass1") in out
    assert ("https://bar.test:443/x", "user2", "pa:ss") in out


def test_password_with_colon_in_block():
    text = """URL: https://x.test/
USER: u
PASS: a:b:c
"""
    out = list(parse_credentials_text(text))
    assert out == [("https://x.test/", "u", "a:b:c")]
