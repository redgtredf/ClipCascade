"""DPAPI key wrap and AES-256-GCM record encryption for the Windows history store.

Only this module touches the plaintext history master key. It never imports
Qt/PySide6. Everything here is Windows-only; callers on other platforms get
HistoryUnavailableError rather than an import-time crash, so the package can
still be imported (and unit-tested) off Windows.
"""

import hashlib
import os
import secrets
import struct
import sys
import tempfile

from Crypto.Cipher import AES

WINDOWS = sys.platform == "win32"

try:
    import win32api
    import win32crypt
    import win32security
    import ntsecuritycon as con
except ImportError:  # pragma: no cover - exercised only off Windows
    win32api = None
    win32crypt = None
    win32security = None
    con = None


SCHEMA_VERSION = 1
KEY_LENGTH_BYTES = 32
NONCE_LENGTH_BYTES = 12
TAG_LENGTH_BYTES = 16

MASTER_KEY_FILE_NAME = "master-key.dpapi"

_KEY_DESCRIPTION = "ClipCascade history master key"
_ENTROPY_CONTEXT = b"ClipCascade.History.MasterKey.v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_SYSTEM_SID_STRING = "S-1-5-18"
_ADMINISTRATORS_SID_STRING = "S-1-5-32-544"


class HistoryUnavailableError(RuntimeError):
    """Raised when DPAPI/crypto primitives cannot run on this platform."""


class KeyProtectionError(RuntimeError):
    """Raised when DPAPI wrap/unwrap fails (wrong user, corruption, tamper)."""


class RecordAuthenticationError(ValueError):
    """Raised when an encrypted record fails AES-GCM authentication."""


def _require_windows():
    if not WINDOWS or win32crypt is None:
        raise HistoryUnavailableError(
            "Windows DPAPI is required for encrypted history storage"
        )


def _entropy() -> bytes:
    return hashlib.sha256(_ENTROPY_CONTEXT).digest()


def generate_history_key() -> bytes:
    """32 random bytes for a fresh AES-256 history master key."""
    return secrets.token_bytes(KEY_LENGTH_BYTES)


def wrap_key(key: bytes) -> bytes:
    """Protect the history master key for the current Windows user with DPAPI."""
    _require_windows()
    try:
        return win32crypt.CryptProtectData(
            key, _KEY_DESCRIPTION, _entropy(), None, None, _CRYPTPROTECT_UI_FORBIDDEN
        )
    except Exception as error:
        raise KeyProtectionError(f"Failed to wrap history master key: {error}") from error


def unwrap_key(blob: bytes) -> bytes:
    """Recover the history master key; fails closed for any other user or tampered blob."""
    _require_windows()
    try:
        _description, key = win32crypt.CryptUnprotectData(
            blob, _entropy(), None, None, _CRYPTPROTECT_UI_FORBIDDEN
        )
    except Exception as error:
        raise KeyProtectionError(f"Failed to unwrap history master key: {error}") from error
    if len(key) != KEY_LENGTH_BYTES:
        raise KeyProtectionError("Unwrapped history master key has unexpected length")
    return key


def current_user_sid():
    _require_windows()
    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
    return sid


def secure_directory(path: str) -> None:
    """Restrict `path` (and everything created inside it) to this user, SYSTEM and Admins.

    Sets an inheritable, DACL-protected ACL so files written later (the
    database, the wrapped key, blobs) pick it up automatically without a
    per-file ACL call. Best-effort defense in depth: DPAPI itself already
    ties key recovery to the current user regardless of file permissions.
    """
    _require_windows()
    os.makedirs(path, exist_ok=True)
    user_sid = current_user_sid()
    system_sid = win32security.ConvertStringSidToSid(_SYSTEM_SID_STRING)
    admins_sid = win32security.ConvertStringSidToSid(_ADMINISTRATORS_SID_STRING)

    dacl = win32security.ACL()
    inherit_flags = con.CONTAINER_INHERIT_ACE | con.OBJECT_INHERIT_ACE
    for sid in (user_sid, system_sid, admins_sid):
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS, inherit_flags, con.FILE_ALL_ACCESS, sid
        )

    security_descriptor = win32security.GetFileSecurity(
        path, win32security.DACL_SECURITY_INFORMATION
    )
    security_descriptor.SetSecurityDescriptorDacl(1, dacl, 0)
    security_descriptor.SetSecurityDescriptorControl(
        win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED
    )
    win32security.SetFileSecurity(
        path, win32security.DACL_SECURITY_INFORMATION, security_descriptor
    )


def load_or_create_master_key(history_dir: str) -> tuple:
    """Load the wrapped master key from `history_dir`, creating one on first use.

    Returns (key_bytes, created: bool). Raises KeyProtectionError if a key
    file exists but cannot be unwrapped (wrong user, corruption, tamper) —
    callers must quarantine rather than overwrite in that case.
    """
    _require_windows()
    os.makedirs(history_dir, exist_ok=True)
    key_path = os.path.join(history_dir, MASTER_KEY_FILE_NAME)
    if os.path.isfile(key_path):
        with open(key_path, "rb") as f:
            blob = f.read()
        return unwrap_key(blob), False

    key = generate_history_key()
    wrapped = wrap_key(key)
    _atomic_write(key_path, wrapped)
    return key, True


def _atomic_write(path: str, data: bytes) -> None:
    """Write via temp file -> flush -> rename so a crash never leaves a partial file."""
    directory = os.path.dirname(path) or "."
    fd, temp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def build_aad(
    entry_id: str,
    payload_type: str,
    direction: str,
    created_at_utc: int,
    schema_version: int = SCHEMA_VERSION,
    field: str = None,
) -> bytes:
    """Deterministic additional authenticated data: schema version, entry UUID,
    payload type, direction and creation timestamp, length-prefixed so no
    concatenation ambiguity exists between fields.

    `field` additionally binds a ciphertext to the exact column it belongs to
    ("summary", "payload", "source_device", ...): without it every field of
    one entry shares identical AAD, so a ciphertext swapped between columns
    (summary blob moved into the payload column) would still authenticate.
    Omitting `field` reproduces the original (un-fielded) byte format."""
    parts = [
        struct.pack(">I", schema_version),
        _length_prefixed(entry_id.encode("utf-8")),
        _length_prefixed(payload_type.encode("utf-8")),
        _length_prefixed(direction.encode("utf-8")),
        struct.pack(">q", created_at_utc),
    ]
    if field is not None:
        parts.append(_length_prefixed(field.encode("utf-8")))
    return b"".join(parts)


def _length_prefixed(data: bytes) -> bytes:
    if len(data) > 0xFFFF:
        raise ValueError("AAD field too long")
    return struct.pack(">H", len(data)) + data


def encrypt_field(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Encrypt one record field. Returns nonce (12B) || tag (16B) || ciphertext."""
    cipher = AES.new(key, AES.MODE_GCM, nonce=secrets.token_bytes(NONCE_LENGTH_BYTES))
    cipher.update(aad)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return cipher.nonce + tag + ciphertext


def decrypt_field(key: bytes, blob: bytes, aad: bytes) -> bytes:
    """Decrypt one record field; fails closed (RecordAuthenticationError) on any
    ciphertext tampering or AAD mismatch."""
    if len(blob) < NONCE_LENGTH_BYTES + TAG_LENGTH_BYTES:
        raise RecordAuthenticationError("Encrypted record is truncated")
    nonce = blob[:NONCE_LENGTH_BYTES]
    tag = blob[NONCE_LENGTH_BYTES : NONCE_LENGTH_BYTES + TAG_LENGTH_BYTES]
    ciphertext = blob[NONCE_LENGTH_BYTES + TAG_LENGTH_BYTES :]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    try:
        return cipher.decrypt_and_verify(ciphertext, tag)
    except ValueError as error:
        raise RecordAuthenticationError(
            f"History record failed authentication: {error}"
        ) from error


def fingerprint(canonical_plaintext: bytes) -> bytes:
    """One-way SHA-256 deduplication fingerprint; never used as a key."""
    return hashlib.sha256(canonical_plaintext).digest()
