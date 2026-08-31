"""T6 link-policy tests: complete-URL validation corpus and per-hostname
HTTPS-only trust persistence."""


import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings

from history_ui.link_policy import is_trusted, set_trusted, validate_link


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "ui.ini"), QSettings.IniFormat)


VALID = [
    "https://example.com",
    "http://example.com/path?q=1",
    "HTTPS://EXAMPLE.COM/UPPER",
    "https://example.co.uk:8443/a/b?x=1#frag",
    "https://192.168.1.10/admin",
    "https://[::1]:9000/ipv6",
]

INVALID = {
    "javascript:alert(1)": "scheme-not-http(s)",
    "file:///etc/passwd": "scheme-not-http(s)",
    "data:text/html,hello": "scheme-not-http(s)",
    "ws://example.com": "scheme-not-http(s)",
    "ftp://example.com/pub": "scheme-not-http(s)",
    "https://": "no-host",
    "https://user@example.com": "userinfo-in-url",
    "https://user:pass@example.com": "userinfo-in-url",
    "example.com/path": "scheme-not-http(s)",
    "": "empty",
    "   ": "empty",
    "https://exa mple.com": "forbidden-characters",
    "https://example.com/a\nb": "forbidden-characters",
    "https://example.com\x00": "forbidden-characters",
    "https://example.com\\..\\..\\x": "forbidden-characters",
    "a" * 2049: "too-long",
}


@pytest.mark.parametrize("url", VALID)
def test_valid_links_pass(url):
    check = validate_link(url)
    assert check.ok, check.error
    assert check.url == url.strip()
    assert check.scheme in ("http", "https")
    assert check.hostname


@pytest.mark.parametrize("url,expected_error", INVALID.items())
def test_malicious_or_malformed_links_fail(url, expected_error):
    check = validate_link(url)
    assert not check.ok
    assert check.error == expected_error
    assert check.url is None


def test_non_string_is_rejected():
    assert validate_link(None).error == "not-a-string"
    assert validate_link(b"https://example.com").error == "not-a-string"


def test_trust_round_trip_and_isolation(settings):
    hostname = "Example.COM"
    assert not is_trusted(settings, hostname)
    set_trusted(settings, hostname)
    assert is_trusted(settings, "example.com")  # case-insensitive hostname
    assert not is_trusted(settings, "other.example.com")

    settings.sync()
    reloaded = QSettings(settings.fileName(), QSettings.IniFormat)
    assert is_trusted(reloaded, "example.com")  # persisted


def test_trust_never_matches_empty_hostname(settings):
    set_trusted(settings, "example.com")
    assert not is_trusted(settings, "")
    assert not is_trusted(settings, None)
