"""Link validation and per-hostname trust for the history window.

Only a *complete* `http://` or `https://` URL is a link; everything else is
text. Opening is explicit (never automatic), always confirmed per untrusted
hostname, and performed through argument-safe OS APIs only — never a shell
string. Trust is granted per hostname, for HTTPS only, and persisted in the
window's own QSettings (UI preference, not history data).
"""

import string
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048

_VALID_SCHEMES = ("http", "https")
# Characters that must never appear anywhere in a link we are about to hand
# to the OS URL handler: control characters, whitespace and separators that
# different shells/handlers treat differently.
_FORBIDDEN_CHARS = set(
    "".join(chr(code) for code in range(0x20)) + "\x7f" + " \t\r\n" + "\\"
)
_SAFE_HOST_CHARS = set(string.ascii_letters + string.digits + ".-[]:")


@dataclass(frozen=True)
class LinkCheck:
    url: Optional[str]
    scheme: Optional[str]
    hostname: Optional[str]
    error: Optional[str] = None

    @property
    def ok(self):
        return self.error is None

    @property
    def is_secure(self):
        return self.scheme == "https"


def validate_link(raw_url) -> LinkCheck:
    """Full validation for the Open-in-browser action. Returns a LinkCheck
    whose `url` is the stripped original (never a rewrite: open exactly what
    was validated) plus its scheme and hostname for display and trust."""
    if not isinstance(raw_url, str):
        return LinkCheck(None, None, None, "not-a-string")
    url = raw_url.strip()
    if not url:
        return LinkCheck(None, None, None, "empty")
    if len(url) > MAX_URL_LENGTH:
        return LinkCheck(None, None, None, "too-long")
    if any(char in _FORBIDDEN_CHARS for char in url):
        return LinkCheck(None, None, None, "forbidden-characters")
    try:
        parts = urlsplit(url)
    except ValueError:
        return LinkCheck(None, None, None, "unparseable")

    scheme = (parts.scheme or "").lower()
    if scheme not in _VALID_SCHEMES:
        return LinkCheck(None, None, None, "scheme-not-http(s)")
    if not parts.netloc:
        return LinkCheck(None, None, None, "no-host")
    if "@" in parts.netloc:
        # userinfo tricks (user:pass@host / user@evil) are not legitimate
        # clipboard links and make the hostname display lie.
        return LinkCheck(None, None, None, "userinfo-in-url")

    hostname = (parts.hostname or "").lower()
    if not hostname:
        return LinkCheck(None, None, None, "no-host")
    if not set(hostname) <= _SAFE_HOST_CHARS:
        return LinkCheck(None, None, None, "bad-hostname-characters")
    return LinkCheck(url=url, scheme=scheme, hostname=hostname)


def trust_setting_key(hostname: str) -> str:
    return f"linkTrust/{hostname.strip().lower()}"


def is_trusted(settings, hostname: str) -> bool:
    if not hostname:
        return False
    return settings.value(trust_setting_key(hostname)) in (True, "true", "1", 1)


def set_trusted(settings, hostname: str):
    settings.setValue(trust_setting_key(hostname), True)
