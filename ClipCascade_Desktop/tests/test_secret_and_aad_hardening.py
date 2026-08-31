"""C-batch-3 hardening tests: DPAPI protection of credentials saved next to
the executable (config file must never contain the plaintext secret), and
field-scoped GCM AAD binding (a ciphertext swapped between columns must fail
authentication instead of decrypting).
"""

import base64
import json
import sys

import pytest

from core.config import Config
from history import crypto
from utils import secret_store

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="DPAPI protection is Windows-only"
)

SECRET = "correct horse battery staple"


# --- secret store --------------------------------------------------------


def test_protect_unprotect_roundtrip_str():
    envelope = secret_store.protect(SECRET)
    assert isinstance(envelope, dict) and envelope.get("dpapi") is True
    assert SECRET not in json.dumps(envelope)
    assert secret_store.unprotect(envelope) == SECRET


def test_protect_unprotect_roundtrip_bytes():
    raw = base64.b64decode("cGFzc3dvcmQ=")
    envelope = secret_store.protect(raw)
    assert secret_store.unprotect(envelope) == raw


def test_protect_unprotect_roundtrip_dict():
    cookie = {"JSESSIONID": "session-id-123"}
    envelope = secret_store.protect(cookie)
    assert "session-id-123" not in json.dumps(envelope)
    assert secret_store.unprotect(envelope) == cookie


def test_empty_and_none_pass_through():
    assert secret_store.protect(None) is None
    assert secret_store.protect("") == ""
    assert secret_store.unprotect(None) is None


def test_legacy_plaintext_passes_through():
    assert secret_store.unprotect(SECRET) == SECRET
    assert secret_store.unprotect({"JSESSIONID": "x"}) == {"JSESSIONID": "x"}


def test_corrupted_envelope_returns_none():
    envelope = secret_store.protect(SECRET)
    tampered = dict(envelope)
    tampered["b64"] = base64.b64encode(b"\x00" * 16).decode("ascii")
    assert secret_store.unprotect(tampered) is None


def test_malformed_envelope_returns_none():
    assert secret_store.unprotect({"dpapi": True, "kind": "x", "b64": "zz"}) is None


# --- config file round-trip ----------------------------------------------


def test_config_roundtrip_never_writes_plaintext_secrets(tmp_path):
    config_file = tmp_path / "data.json"
    config = Config(file_name=str(config_file))
    config.data["password"] = SECRET
    config.data["csrf_token"] = "csrf-token-value"
    config.data["cookie"] = {"JSESSIONID": "cookie-session-id"}
    config.save()

    on_disk = config_file.read_text(encoding="utf-8")
    assert SECRET not in on_disk
    assert "csrf-token-value" not in on_disk
    assert "cookie-session-id" not in on_disk

    reloaded = Config(file_name=str(config_file))
    assert reloaded.load() is True
    assert reloaded.data["password"] == SECRET
    assert reloaded.data["csrf_token"] == "csrf-token-value"
    assert reloaded.data["cookie"] == {"JSESSIONID": "cookie-session-id"}


def test_config_loads_legacy_plaintext_file(tmp_path):
    config_file = tmp_path / "legacy.json"
    legacy = {
        "password": SECRET,
        "csrf_token": "legacy-csrf",
        "cookie": {"JSESSIONID": "legacy-session"},
    }
    config_file.write_text(json.dumps(legacy), encoding="utf-8")

    config = Config(file_name=str(config_file))
    assert config.load() is True
    assert config.data["password"] == SECRET
    assert config.data["cookie"] == {"JSESSIONID": "legacy-session"}


def test_config_roundtrip_hashed_password(tmp_path):
    config_file = tmp_path / "data.json"
    config = Config(file_name=str(config_file))
    config.data["cipher_enabled"] = True
    config.data["hashed_password"] = b"\x01\x02\x03hash-bytes"
    config.save()

    on_disk = config_file.read_text(encoding="utf-8")
    assert "hash-bytes" not in on_disk

    reloaded = Config(file_name=str(config_file))
    assert reloaded.load() is True
    assert reloaded.data["hashed_password"] == b"\x01\x02\x03hash-bytes"


# --- field-scoped AAD ----------------------------------------------------


def test_build_aad_without_field_reproduces_legacy_bytes():
    plain = crypto.build_aad("entry-id", "text", "local", 1_700_000_000)
    explicit = crypto.build_aad("entry-id", "text", "local", 1_700_000_000, field=None)
    assert plain == explicit
    # a field-scoped AAD is always different from the unfielded one
    assert plain != crypto.build_aad(
        "entry-id", "text", "local", 1_700_000_000, field="payload"
    )


def test_swapped_ciphertext_between_fields_fails_authentication():
    key = crypto.generate_history_key()
    aad_summary = crypto.build_aad("e1", "text", "local", 1, field="summary")
    aad_payload = crypto.build_aad("e1", "text", "local", 1, field="payload")

    summary_blob = crypto.encrypt_field(key, b'{"preview": "p"}', aad_summary)
    payload_blob = crypto.encrypt_field(key, b"the real payload", aad_payload)

    # normal decryption works
    assert crypto.decrypt_field(key, payload_blob, aad_payload) == b"the real payload"

    # a blob swapped into the other column fails authentication
    with pytest.raises(crypto.RecordAuthenticationError):
        crypto.decrypt_field(key, payload_blob, aad_summary)
    with pytest.raises(crypto.RecordAuthenticationError):
        crypto.decrypt_field(key, summary_blob, aad_payload)


def test_service_detail_survives_and_swapped_columns_are_rejected(tmp_path):
    from datetime import datetime, timezone

    from history import models, service

    result = service.bootstrap(str(tmp_path / "history"))
    assert result.enabled, result.disabled_reason
    svc = result.service
    try:
        entry_id = svc.record(
            models.HistoryCaptureEvent(
                direction="local",
                payload_type="text",
                payload="secret payload content",
                source_device_id=None,
                source_device_name=None,
                transport="local",
                occurred_at_utc=datetime.now(timezone.utc),
            )
        )
        detail = svc.get_detail(entry_id)
        assert detail is not None
        assert detail.text == "secret payload content"

        # swap the payload ciphertext into the summary column at the row level:
        # authentication must fail rather than decrypt foreign plaintext
        row = svc._store.get_entry(entry_id)
        swapped = dict(row)
        swapped["encrypted_summary"], swapped["encrypted_payload"] = (
            row["encrypted_payload"],
            row["encrypted_summary"],
        )
        with pytest.raises(crypto.RecordAuthenticationError):
            svc._to_summary(swapped)
    finally:
        svc.close()
