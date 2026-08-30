"""Live Windows DPAPI + AES-GCM tests for history/crypto.py.

These call the real win32crypt APIs (no mocking) — this suite only makes
sense on Windows, which is the only platform history/ ever runs on.
"""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")

from history import crypto  # noqa: E402


def test_generate_history_key_is_32_random_bytes():
    a = crypto.generate_history_key()
    b = crypto.generate_history_key()
    assert len(a) == 32
    assert len(b) == 32
    assert a != b


def test_dpapi_same_user_round_trip_is_live_not_mocked():
    """Live probe: wrap with the real CryptProtectData, unwrap with the real
    CryptUnprotectData, under this actual Windows user session."""
    key = crypto.generate_history_key()
    wrapped = crypto.wrap_key(key)
    assert wrapped != key
    unwrapped = crypto.unwrap_key(wrapped)
    assert unwrapped == key


def test_dpapi_unwrap_fails_closed_on_tampered_blob():
    key = crypto.generate_history_key()
    wrapped = bytearray(crypto.wrap_key(key))
    wrapped[-1] ^= 0xFF
    with pytest.raises(crypto.KeyProtectionError):
        crypto.unwrap_key(bytes(wrapped))


def test_dpapi_unwrap_fails_closed_on_garbage_blob():
    with pytest.raises(crypto.KeyProtectionError):
        crypto.unwrap_key(b"not a real DPAPI blob at all")


def test_load_or_create_master_key_persists_atomically(tmp_path):
    history_dir = str(tmp_path / "history")
    key1, created1 = crypto.load_or_create_master_key(history_dir)
    assert created1 is True
    assert len(key1) == 32

    key2, created2 = crypto.load_or_create_master_key(history_dir)
    assert created2 is False
    assert key2 == key1

    key_path = tmp_path / "history" / crypto.MASTER_KEY_FILE_NAME
    assert key_path.is_file()
    # Only the wrapped blob ever touches disk.
    raw = key_path.read_bytes()
    assert key1 not in raw


def test_secure_directory_grants_only_current_user_system_and_admins(tmp_path):
    history_dir = str(tmp_path / "history")
    crypto.secure_directory(history_dir)
    # A file created afterward must inherit the restricted ACL without a
    # separate per-file call.
    marker = tmp_path / "history" / "marker.txt"
    marker.write_text("x")

    import win32security

    sd = win32security.GetFileSecurity(str(marker), win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    assert dacl.GetAceCount() == 3


def test_build_aad_is_deterministic_and_field_sensitive():
    aad1 = crypto.build_aad("entry-1", "text", "local", 1_700_000_000)
    aad2 = crypto.build_aad("entry-1", "text", "local", 1_700_000_000)
    assert aad1 == aad2

    assert crypto.build_aad("entry-2", "text", "local", 1_700_000_000) != aad1
    assert crypto.build_aad("entry-1", "link", "local", 1_700_000_000) != aad1
    assert crypto.build_aad("entry-1", "text", "remote", 1_700_000_000) != aad1
    assert crypto.build_aad("entry-1", "text", "local", 1_700_000_001) != aad1


def test_encrypt_decrypt_round_trip():
    key = crypto.generate_history_key()
    aad = crypto.build_aad("entry-1", "text", "local", 1_700_000_000)
    plaintext = "sensitive clipboard text ☃".encode("utf-8")
    blob = crypto.encrypt_field(key, plaintext, aad)

    assert plaintext not in blob
    assert crypto.decrypt_field(key, blob, aad) == plaintext


def test_decrypt_fails_closed_on_tampered_ciphertext():
    key = crypto.generate_history_key()
    aad = crypto.build_aad("entry-1", "text", "local", 1_700_000_000)
    blob = bytearray(crypto.encrypt_field(key, b"secret", aad))
    blob[-1] ^= 0xFF
    with pytest.raises(crypto.RecordAuthenticationError):
        crypto.decrypt_field(key, bytes(blob), aad)


def test_decrypt_fails_closed_on_mismatched_authenticated_metadata():
    key = crypto.generate_history_key()
    aad = crypto.build_aad("entry-1", "text", "local", 1_700_000_000)
    blob = crypto.encrypt_field(key, b"secret", aad)

    wrong_aad = crypto.build_aad("entry-2", "text", "local", 1_700_000_000)
    with pytest.raises(crypto.RecordAuthenticationError):
        crypto.decrypt_field(key, blob, wrong_aad)


def test_decrypt_fails_closed_with_wrong_key():
    key = crypto.generate_history_key()
    other_key = crypto.generate_history_key()
    aad = crypto.build_aad("entry-1", "text", "local", 1_700_000_000)
    blob = crypto.encrypt_field(key, b"secret", aad)
    with pytest.raises(crypto.RecordAuthenticationError):
        crypto.decrypt_field(other_key, blob, aad)


def test_decrypt_rejects_truncated_blob():
    key = crypto.generate_history_key()
    with pytest.raises(crypto.RecordAuthenticationError):
        crypto.decrypt_field(key, b"short", b"aad")


def test_fingerprint_is_one_way_and_stable():
    fp1 = crypto.fingerprint(b"same input")
    fp2 = crypto.fingerprint(b"same input")
    fp3 = crypto.fingerprint(b"different input")
    assert fp1 == fp2
    assert fp1 != fp3
    assert len(fp1) == 32
