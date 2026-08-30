"""Schema, migration, integrity and blob-lifecycle tests for history/store.py."""

import os
import sqlite3
import sys

import pytest

from history import store as store_mod

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="history/ is Windows-only")


def _row(entry_id="e1", created_at=1_700_000_000, payload_type="text", **overrides):
    row = {
        "id": entry_id,
        "created_at_utc": created_at,
        "updated_at_utc": created_at,
        "payload_type": payload_type,
        "direction": "local",
        "transport": "local",
        "pinned": 0,
        "byte_size": 10,
        "encrypted_summary": b"summary-bytes",
        "encrypted_payload": b"payload-bytes",
        "blob_relative_path": None,
        "payload_sha256": b"0" * 32,
        "source_device_id_hash": None,
        "encrypted_source_device": None,
        "file_state": None,
        "expires_at_utc": None,
        "downloaded_directory_encrypted": None,
    }
    row.update(overrides)
    return row


def test_fresh_creation_produces_schema_v1(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    try:
        assert store.schema_version() == store_mod.CURRENT_SCHEMA_VERSION
        assert os.path.isfile(store.db_path)
        assert os.path.isdir(store.blobs_dir)
    finally:
        store.close()


class _FailAfterNCalls(sqlite3.Connection):
    """sqlite3.Connection instances don't allow attribute assignment (C
    extension type), so a subclass is the way to inject a failure at a
    specific execute() call to test transaction rollback."""

    def configure_failure(self, call_index):
        self._call_count = 0
        self._fail_at = call_index

    def execute(self, sql, *args, **kwargs):
        self._call_count = getattr(self, "_call_count", 0) + 1
        if self._call_count == getattr(self, "_fail_at", None):
            raise sqlite3.OperationalError("synthetic failure mid schema creation")
        return super().execute(sql, *args, **kwargs)


def test_schema_creation_is_atomic_all_or_nothing(tmp_path):
    """Kills the connection mid schema-creation (after a couple of DDL
    statements) and asserts nothing partial survives — the store is either
    fully created or not created at all, never half-created."""
    history_dir = str(tmp_path / "history")
    os.makedirs(history_dir, exist_ok=True)
    db_path = os.path.join(history_dir, store_mod.DB_FILE_NAME)

    store = store_mod.HistoryStore(history_dir)
    conn = sqlite3.connect(
        db_path, isolation_level=None, check_same_thread=False, factory=_FailAfterNCalls
    )
    conn.configure_failure(4)  # BEGIN, then a couple of real DDL statements, then boom
    store._conn = conn

    with pytest.raises(sqlite3.OperationalError):
        store._create_schema()

    remaining = conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
    ).fetchall()
    assert remaining == []  # nothing partially created
    conn.close()

    # A clean re-open afterward must still fully succeed.
    store2 = store_mod.HistoryStore(history_dir).open()
    try:
        assert store2.schema_version() == store_mod.CURRENT_SCHEMA_VERSION
    finally:
        store2.close()


def test_reopen_existing_store_passes_integrity_check(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    store.insert_entry(_row())
    store.close()

    store2 = store_mod.HistoryStore(history_dir).open()
    try:
        assert store2.get_entry("e1") is not None
    finally:
        store2.close()


def test_newer_schema_is_refused_and_left_untouched(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    store.close()

    conn = sqlite3.connect(os.path.join(history_dir, store_mod.DB_FILE_NAME))
    conn.execute("UPDATE schema_version SET version = 999")
    conn.commit()
    conn.close()

    with pytest.raises(store_mod.SchemaTooNewError):
        store_mod.HistoryStore(history_dir).open()

    # Left untouched: no quarantine, still readable at the (newer) version.
    conn = sqlite3.connect(os.path.join(history_dir, store_mod.DB_FILE_NAME))
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    conn.close()
    assert version == 999


def test_supported_upgrade_is_transactional_backed_up_and_forward_only(tmp_path):
    """No real product migration exists above v1 yet (v1 is the first shipped
    schema), so this proves the migration *engine* itself — backup before
    mutation, transactional apply, schema_version bump — using a synthetic
    migration registered for the test."""
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    store.close()

    applied = []

    def fake_migration(conn):
        conn.execute(
            "INSERT INTO history_setting(key, value) VALUES ('migrated', 'yes')"
        )
        applied.append(True)

    original_current = store_mod.CURRENT_SCHEMA_VERSION
    original_migrations = dict(store_mod.MIGRATIONS)
    store_mod.CURRENT_SCHEMA_VERSION = original_current + 1
    store_mod.MIGRATIONS[original_current] = fake_migration
    try:
        store2 = store_mod.HistoryStore(history_dir).open()
        try:
            assert store2.schema_version() == original_current + 1
            assert store2.get_setting("migrated") == "yes"
            assert applied == [True]
            backup_path = f"{store2.db_path}.pre-migration-v{original_current}.bak"
            assert os.path.isfile(backup_path)
        finally:
            store2.close()
    finally:
        store_mod.CURRENT_SCHEMA_VERSION = original_current
        store_mod.MIGRATIONS.clear()
        store_mod.MIGRATIONS.update(original_migrations)


def test_migration_failure_rolls_back_and_leaves_schema_version_unchanged(tmp_path):
    """A migration step that raises partway through must not leave the
    version bumped or its partial writes applied — the backup file proves a
    backup was taken, but the live db must be exactly as before the attempt."""
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    store.close()

    def failing_migration(conn):
        conn.execute(
            "INSERT INTO history_setting(key, value) VALUES ('partial', 'should-not-survive')"
        )
        raise RuntimeError("synthetic mid-migration failure")

    original_current = store_mod.CURRENT_SCHEMA_VERSION
    original_migrations = dict(store_mod.MIGRATIONS)
    store_mod.CURRENT_SCHEMA_VERSION = original_current + 1
    store_mod.MIGRATIONS[original_current] = failing_migration
    try:
        with pytest.raises(RuntimeError):
            store_mod.HistoryStore(history_dir).open()
    finally:
        store_mod.CURRENT_SCHEMA_VERSION = original_current
        store_mod.MIGRATIONS.clear()
        store_mod.MIGRATIONS.update(original_migrations)

    backup_path = f"{os.path.join(history_dir, store_mod.DB_FILE_NAME)}.pre-migration-v{original_current}.bak"
    assert os.path.isfile(backup_path)  # backup was taken before the attempt

    # The live db must still be at the original version with no partial write.
    store2 = store_mod.HistoryStore(history_dir).open()
    try:
        assert store2.schema_version() == original_current
        assert store2.get_setting("partial") is None
    finally:
        store2.close()


def test_migration_refuses_when_no_step_registered(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    store.close()

    original_current = store_mod.CURRENT_SCHEMA_VERSION
    store_mod.CURRENT_SCHEMA_VERSION = original_current + 1
    try:
        with pytest.raises(store_mod.SchemaTooNewError):
            store_mod.HistoryStore(history_dir).open()
    finally:
        store_mod.CURRENT_SCHEMA_VERSION = original_current


def test_forced_corruption_is_detected_and_never_silently_opened(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    for i in range(50):
        store.insert_entry(_row(entry_id=f"e{i}", created_at=1_700_000_000 + i))
    store.close()

    db_path = os.path.join(history_dir, store_mod.DB_FILE_NAME)
    size = os.path.getsize(db_path)
    with open(db_path, "r+b") as f:
        f.seek(4096)
        f.write(b"\xff" * min(8192, size - 4096))

    with pytest.raises(store_mod.StoreCorruptError):
        store_mod.HistoryStore(history_dir).open()

    # Corrupt file left in place — never overwritten or "repaired".
    assert os.path.isfile(db_path)


def test_quarantine_moves_db_key_and_blobs_without_deleting(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    store.insert_entry(_row())
    store.close()

    key_path = os.path.join(history_dir, "master-key.dpapi")
    with open(key_path, "wb") as f:
        f.write(b"fake-wrapped-key")
    blob_path = os.path.join(history_dir, store_mod.BLOBS_DIR_NAME, "e1.bin")
    with open(blob_path, "wb") as f:
        f.write(b"blob-bytes")

    quarantine_path = store_mod.quarantine_history_directory(history_dir, now_utc=1_700_000_000)

    assert os.path.isdir(quarantine_path)
    assert os.path.isfile(os.path.join(quarantine_path, store_mod.DB_FILE_NAME))
    assert os.path.isfile(os.path.join(quarantine_path, "master-key.dpapi"))
    assert os.path.isdir(os.path.join(quarantine_path, store_mod.BLOBS_DIR_NAME))
    assert os.path.isfile(os.path.join(quarantine_path, store_mod.BLOBS_DIR_NAME, "e1.bin"))

    # Nothing left behind at the original location; nothing deleted, only moved.
    assert not os.path.exists(os.path.join(history_dir, store_mod.DB_FILE_NAME))
    assert not os.path.exists(key_path)
    assert not os.path.exists(blob_path)


def test_quarantine_never_collides_on_repeated_calls(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    store.close()
    store2 = store_mod.HistoryStore(history_dir).open()
    store2.close()

    first = store_mod.quarantine_history_directory(history_dir, now_utc=1_700_000_000)

    # Recreate a store so a second quarantine at the same timestamp has
    # something to move without overwriting the first quarantine directory.
    store3 = store_mod.HistoryStore(history_dir).open()
    store3.close()
    second = store_mod.quarantine_history_directory(history_dir, now_utc=1_700_000_000)

    assert first != second
    assert os.path.isdir(first)
    assert os.path.isdir(second)


def test_write_blob_atomic_never_leaves_a_partial_file(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    try:
        relative_path = store.write_blob_atomic("e1", b"encrypted-blob-bytes")
        assert store.read_blob(relative_path) == b"encrypted-blob-bytes"
        # no leftover temp files
        leftovers = [n for n in os.listdir(store.blobs_dir) if n.startswith(".tmp-")]
        assert leftovers == []
    finally:
        store.close()


def test_delete_blob_missing_file_is_treated_as_already_gone(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    try:
        assert store.delete_blob("blobs/does-not-exist.bin") is True
        assert store.delete_blob(None) is True
    finally:
        store.close()


def test_orphan_cleanup_removes_only_unreferenced_blobs_past_grace_period(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    try:
        referenced_relative = store.write_blob_atomic("referenced", b"kept")
        store.insert_entry(_row(entry_id="referenced", payload_type="image", blob_relative_path=referenced_relative))

        orphan_relative = store.write_blob_atomic("orphan", b"unreferenced")
        orphan_path = os.path.join(store.history_dir, orphan_relative)
        old_time = os.path.getmtime(orphan_path) - 7200
        os.utime(orphan_path, (old_time, old_time))

        result = store.orphan_cleanup(grace_period_seconds=3600)
        assert os.path.normpath(orphan_path) in [os.path.normpath(p) for p in result["removed"]]
        assert os.path.isfile(os.path.join(store.history_dir, referenced_relative))
        assert not os.path.isfile(orphan_path)
    finally:
        store.close()


def test_orphan_cleanup_leaves_recent_unreferenced_blobs_for_next_grace_window(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    try:
        relative = store.write_blob_atomic("fresh-orphan", b"just written")
        result = store.orphan_cleanup(grace_period_seconds=3600)
        assert result["removed"] == []
        assert os.path.isfile(os.path.join(store.history_dir, relative))
    finally:
        store.close()


def test_keyset_pagination_is_stable_across_pages(tmp_path):
    history_dir = str(tmp_path / "history")
    store = store_mod.HistoryStore(history_dir).open()
    try:
        for i in range(10):
            store.insert_entry(_row(entry_id=f"e{i:02d}", created_at=1_700_000_000 + i))

        page1, has_more1 = store.list_entries(limit=4)
        assert has_more1 is True
        assert [r["id"] for r in page1] == ["e09", "e08", "e07", "e06"]

        cursor = store_mod.encode_cursor(page1[-1]["created_at_utc"], page1[-1]["id"])
        page2, has_more2 = store.list_entries(cursor=cursor, limit=4)
        assert has_more2 is True
        assert [r["id"] for r in page2] == ["e05", "e04", "e03", "e02"]

        cursor2 = store_mod.encode_cursor(page2[-1]["created_at_utc"], page2[-1]["id"])
        page3, has_more3 = store.list_entries(cursor=cursor2, limit=4)
        assert has_more3 is False
        assert [r["id"] for r in page3] == ["e01", "e00"]
    finally:
        store.close()
