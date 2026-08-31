"""DPAPI protection for credentials saved next to the executable (Windows).

The config file historically stored the saved password, session cookie, CSRF
token and hashed password as plaintext JSON beside the exe; anyone with file
access (or a synced/backup copy) could reuse them. On Windows these values
are now wrapped with DPAPI for the current user; other platforms keep the
previous format so behaviour there is unchanged.

Envelope format (JSON-safe):
    {"dpapi": true, "kind": "s"|"b", "b64": "<base64 of wrapped bytes>"}

`unprotect` passes non-envelope values through unchanged, so existing config
files keep loading and are upgraded to protected form on the next save.
"""

import base64
import logging
import sys

WINDOWS = sys.platform == "win32"

if WINDOWS:
    try:
        import win32crypt
    except ImportError:  # pragma: no cover - pywin32 is a hard dep on Windows
        win32crypt = None
else:  # pragma: no cover - non-Windows platforms keep the legacy format
    win32crypt = None

_ENTROPY = b"ClipCascade.Config.Secrets.v1"
_DESCRIPTION = "ClipCascade saved credentials"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _dpapi_available() -> bool:
    return WINDOWS and win32crypt is not None


def protect(value):
    """Wrap `value` (str, bytes, dict or None) for storage. Returns a
    JSON-safe envelope on success; on any failure returns the value
    unchanged (the app keeps working, with a log line) rather than losing
    settings."""
    if not _dpapi_available() or value in (None, ""):
        return value

    try:
        if isinstance(value, bytes):
            kind, raw = "b", value
        else:
            kind, raw = "s", None
            if isinstance(value, str):
                raw = value.encode("utf-8")
            else:
                import json

                kind = "j"
                raw = json.dumps(value, separators=(",", ":")).encode("utf-8")

        wrapped = win32crypt.CryptProtectData(
            raw, _DESCRIPTION, _ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN
        )
        return {
            "dpapi": True,
            "kind": kind,
            "b64": base64.b64encode(wrapped).decode("ascii"),
        }
    except Exception as error:
        logging.warning(
            "Could not DPAPI-protect a saved credential (%s); storing it as before",
            type(error).__name__,
        )
        return value


def unprotect(value):
    """Recover a value stored by `protect`. Non-envelope (legacy plaintext)
    values pass through unchanged. A protected value that cannot be unwrapped
    (different Windows user, corrupted) returns None: callers treat that as
    'credential gone, re-login' rather than reusing stale secrets."""
    if not isinstance(value, dict) or not value.get("dpapi"):
        return value

    kind = value.get("kind")
    b64 = value.get("b64")
    if kind not in ("s", "b", "j") or not isinstance(b64, str):
        logging.warning("Malformed protected credential envelope; ignoring it")
        return None

    if not _dpapi_available():
        logging.warning(
            "Saved credential is DPAPI-protected but DPAPI is unavailable; re-login required"
        )
        return None

    try:
        _description, raw = win32crypt.CryptUnprotectData(
            base64.b64decode(b64), _ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN
        )
    except Exception as error:
        logging.warning(
            "Could not unwrap a saved credential (%s); re-login required",
            type(error).__name__,
        )
        return None

    if kind == "b":
        return raw
    if kind == "s":
        return raw.decode("utf-8")
    import json

    return json.loads(raw.decode("utf-8"))
