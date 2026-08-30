"""End-to-end HistoryService tests: capture, query, commands, bootstrap/recovery,
and the plaintext-canary scan the acceptance criteria require."""

import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

import pytest

from history import crypto, models, service
from history import store as store_mod

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="history/ is Windows-only")

CANARY_TEXT = "SECRET_CANARY_do-not-leak-me"
CANARY_URL = "https://example.com/canary-do-not-leak"
CANARY_FILENAME = "secret_canary_report.pdf"
CANARY_FILE_CONTENT = b"CANARY_FILE_BYTES_do-not-leak"
CANARY_DEVICE_NAME = "Canary Laptop Do Not Leak"


def _event(payload_type, payload, direction="local", source_device_id=None, source_device_name=None, transport="local"):
    return models.HistoryCaptureEvent(
        direction=direction,
        payload_type=payload_type,
        payload=payload,
        source_device_id=source_device_id,
        source_device_name=source_device_name,
        transport=transport,
        occurred_at_utc=datetime.now(timezone.utc),
    )


@pytest.fixture
def svc(tmp_path):
    result = service.bootstrap(str(tmp_path / "history"))
    assert result.enabled, result.disabled_reason
    yield result.service
    result.service.close()


def _read_all_bytes_on_disk(service_obj):
    blobs = []
    db_path = service_obj._store.db_path
    with open(db_path, "rb") as f:
        blobs.append(f.read())
    for suffix in ("-wal", "-shm"):
        candidate = db_path + suffix
        if os.path.isfile(candidate):
            with open(candidate, "rb") as f:
                blobs.append(f.read())
    blobs_dir = service_obj._store.blobs_dir
    for name in os.listdir(blobs_dir):
        with open(os.path.join(blobs_dir, name), "rb") as f:
            blobs.append(f.read())
    return blobs


def test_plaintext_canary_scan_of_db_and_blobs_is_clean(svc):
    svc.record(_event("text", CANARY_TEXT, source_device_id="dev-1", source_device_name=CANARY_DEVICE_NAME))
    svc.record(_event("text", CANARY_URL, direction="remote", source_device_id="dev-2", source_device_name=CANARY_DEVICE_NAME))
    svc.record(_event("files", {CANARY_FILENAME: CANARY_FILE_CONTENT}, direction="remote", source_device_id="dev-3", source_device_name=CANARY_DEVICE_NAME))

    canaries = [
        CANARY_TEXT.encode("utf-8"),
        CANARY_URL.encode("utf-8"),
        CANARY_FILENAME.encode("utf-8"),
        CANARY_FILE_CONTENT,
        CANARY_DEVICE_NAME.encode("utf-8"),
    ]
    for blob in _read_all_bytes_on_disk(svc):
        for canary in canaries:
            assert canary not in blob, f"plaintext leak: {canary!r} found on disk"


def test_record_text_round_trips_through_query_and_detail(svc):
    entry_id = svc.record(_event("text", "hello world"))
    page = svc.query()
    assert len(page.entries) == 1
    assert page.entries[0].payload_type == "text"
    assert page.entries[0].preview == "hello world"

    detail = svc.get_detail(entry_id)
    assert detail.text == "hello world"
    assert detail.url is None


def test_full_url_is_classified_as_link_mixed_prose_is_not(svc):
    link_id = svc.record(_event("text", "https://example.com/path"))
    text_id = svc.record(_event("text", "check https://example.com/path out"))

    link_detail = svc.get_detail(link_id)
    text_detail = svc.get_detail(text_id)
    assert link_detail.payload_type == "link"
    assert link_detail.url == "https://example.com/path"
    assert text_detail.payload_type == "text"
    assert text_detail.text == "check https://example.com/path out"


def test_image_round_trips_with_dimensions_when_decodable(svc):
    from PIL import Image
    import io

    buf = io.BytesIO()
    Image.new("RGB", (12, 7), color="red").save(buf, format="PNG")
    image_bytes = buf.getvalue()

    entry_id = svc.record(_event("image", image_bytes))
    detail = svc.get_detail(entry_id)
    assert detail.image_bytes == image_bytes
    assert detail.image_width == 12
    assert detail.image_height == 7


def test_corrupt_image_bytes_still_store_metadata_only(svc):
    entry_id = svc.record(_event("image", b"not a real image"))
    detail = svc.get_detail(entry_id)
    assert detail.image_bytes == b"not a real image"
    assert detail.image_width is None
    assert detail.image_height is None


def test_files_batch_round_trips_and_batch_payload_is_retrievable(svc):
    files = {"a.txt": b"AAA", "b.txt": b"BBBB"}
    entry_id = svc.record(_event("files", files))
    detail = svc.get_detail(entry_id)
    assert {f.name: f.size_bytes for f in detail.files} == {"a.txt": 3, "b.txt": 4}
    assert detail.file_state == "ready"

    payload = svc.get_file_batch_payload(entry_id)
    assert payload == files


def test_pin_unpin_and_delete_commands(svc):
    entry_id = svc.record(_event("text", "pin me"))

    result = svc.execute(models.PinEntryCommand(entry_id=entry_id))
    assert result.ok
    assert svc.query().entries[0].pinned is True

    result = svc.execute(models.UnpinEntryCommand(entry_id=entry_id))
    assert result.ok
    assert svc.query().entries[0].pinned is False

    result = svc.execute(models.DeleteEntryCommand(entry_id=entry_id))
    assert result.ok
    assert svc.query().entries == ()


def test_delete_unknown_entry_reports_not_found(svc):
    result = svc.execute(models.DeleteEntryCommand(entry_id="does-not-exist"))
    assert result.ok is False
    assert result.error == "not-found"


def test_clear_unpinned_keeps_pinned_entries(svc):
    pinned_id = svc.record(_event("text", "keep me"))
    svc.execute(models.PinEntryCommand(entry_id=pinned_id))
    svc.record(_event("text", "drop me"))

    result = svc.execute(models.ClearUnpinnedCommand())
    assert result.affected_count == 1

    remaining = {e.id for e in svc.query().entries}
    assert remaining == {pinned_id}


def test_clear_all_removes_pinned_entries_too(svc):
    pinned_id = svc.record(_event("text", "keep me"))
    svc.execute(models.PinEntryCommand(entry_id=pinned_id))
    svc.record(_event("text", "drop me"))

    result = svc.execute(models.ClearAllCommand())
    assert result.affected_count == 2
    assert svc.query().entries == ()


def test_recording_disabled_setting_stops_new_captures(svc):
    svc.execute(models.SetRecordingEnabledCommand(enabled=False))
    assert svc.enabled is False
    entry_id = svc.record(_event("text", "should not be recorded"))
    assert entry_id is None
    assert svc.query().entries == ()

    svc.execute(models.SetRecordingEnabledCommand(enabled=True))
    assert svc.enabled is True


def test_source_device_name_is_encrypted_but_decrypts_correctly(svc):
    entry_id = svc.record(_event("text", "hi", source_device_id="dev-9", source_device_name="Office PC"))
    page = svc.query()
    assert page.entries[0].source_device_name == "Office PC"

    row = svc._store.get_entry(entry_id)
    assert row["encrypted_source_device"] is not None
    assert b"Office PC" not in row["encrypted_source_device"]


def test_bootstrap_catches_unexpected_errors_without_crashing(tmp_path, monkeypatch):
    """A failure that isn't one of the four typed exceptions — e.g. a real
    Win32 ACL call failing for a reason other than "not Windows" — must
    still disable history instead of escaping bootstrap() uncaught."""

    def boom(*args, **kwargs):
        raise OSError("simulated ACL failure")

    monkeypatch.setattr(crypto.win32security, "SetFileSecurity", boom)

    history_dir = str(tmp_path / "history")
    result = service.bootstrap(history_dir)

    assert result.enabled is False
    assert result.service is None
    assert result.disabled_reason.startswith("unexpected:")


def test_bootstrap_disables_history_without_crashing_on_dpapi_failure(tmp_path):
    history_dir = str(tmp_path / "history")
    r1 = service.bootstrap(history_dir)
    assert r1.enabled
    r1.service.close()

    key_path = os.path.join(history_dir, crypto.MASTER_KEY_FILE_NAME)
    with open(key_path, "r+b") as f:
        data = bytearray(f.read())
        data[-1] ^= 0xFF
        f.seek(0)
        f.write(data)

    r2 = service.bootstrap(history_dir)
    assert r2.enabled is False
    assert r2.service is None
    assert r2.disabled_reason.startswith("dpapi-failure")
    assert r2.quarantined_path is not None
    assert os.path.isdir(r2.quarantined_path)
    assert not os.path.exists(key_path)


def test_bootstrap_quarantines_corrupt_store_without_crashing(tmp_path):
    history_dir = str(tmp_path / "history")
    r1 = service.bootstrap(history_dir)
    for i in range(30):
        r1.service.record(_event("text", f"row {i}" * 40))
    r1.service.close()

    db_path = os.path.join(history_dir, store_mod.DB_FILE_NAME)
    size = os.path.getsize(db_path)
    with open(db_path, "r+b") as f:
        f.seek(4096)
        f.write(b"\xff" * min(8192, size - 4096))

    r2 = service.bootstrap(history_dir)
    assert r2.enabled is False
    assert r2.disabled_reason.startswith("store-corrupt")
    assert r2.quarantined_path is not None
    assert not os.path.exists(db_path)


def test_reset_requires_explicit_confirmation(tmp_path):
    history_dir = str(tmp_path / "history")
    with pytest.raises(ValueError):
        service.reset_history_directory(history_dir, confirm=False)


def test_reset_after_quarantine_creates_a_working_store(tmp_path):
    history_dir = str(tmp_path / "history")
    r1 = service.bootstrap(history_dir)
    r1.service.close()
    key_path = os.path.join(history_dir, crypto.MASTER_KEY_FILE_NAME)
    with open(key_path, "r+b") as f:
        data = bytearray(f.read())
        data[-1] ^= 0xFF
        f.seek(0)
        f.write(data)

    r2 = service.bootstrap(history_dir)
    assert not r2.enabled

    r3 = service.reset_history_directory(history_dir, confirm=True)
    assert r3.enabled
    entry_id = r3.service.record(_event("text", "fresh start"))
    assert entry_id is not None
    r3.service.close()


def test_newer_schema_is_disabled_but_not_quarantined(tmp_path):
    history_dir = str(tmp_path / "history")
    r1 = service.bootstrap(history_dir)
    r1.service.close()

    conn = sqlite3.connect(os.path.join(history_dir, store_mod.DB_FILE_NAME))
    conn.execute("UPDATE schema_version SET version = 999")
    conn.commit()
    conn.close()

    r2 = service.bootstrap(history_dir)
    assert not r2.enabled
    assert r2.disabled_reason.startswith("schema-too-new")
    assert r2.quarantined_path is None
    assert os.path.isfile(os.path.join(history_dir, store_mod.DB_FILE_NAME))


def test_queued_sink_drops_on_full_queue_without_blocking_the_caller(svc):
    gate = threading.Event()
    original_record = svc.record

    def slow_record(event):
        gate.wait(2)
        return original_record(event)

    svc.record = slow_record
    sink = service.QueuedHistorySink(svc, maxsize=2)
    try:
        sink.record(_event("text", "e1"))
        time.sleep(0.1)  # let the worker pick up e1 and block on the gate
        sink.record(_event("text", "e2"))
        sink.record(_event("text", "e3"))
        sink.record(_event("text", "e4"))  # queue full -> dropped

        assert sink.dropped_count == 1
        gate.set()
        sink._queue.join()
    finally:
        sink.stop(timeout=3)

    svc.record = original_record
    assert len(svc.query().entries) == 3
