"""SQLite transactions, schema/migration and encrypted-blob file lifecycle.

The main ClipCascade process is the sole writer, but it writes and reads from
more than one thread (a queued capture writer plus direct callers), so every
method that touches `self._conn` is serialized behind one lock and the
connection itself allows cross-thread use. `HistoryStore` only ever sees
ciphertext/opaque bytes for sensitive fields — encryption itself is
crypto.py's job, called by service.py. This module never imports Qt or
DPAPI.
"""

import contextlib
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid

CURRENT_SCHEMA_VERSION = 1

DB_FILE_NAME = "history.db"
BLOBS_DIR_NAME = "blobs"
THUMBNAILS_DIR_NAME = "thumbnails"
QUARANTINE_DIR_NAME = "quarantine"

# Files that belong to one logical store and must move together on quarantine.
_DB_SIDECAR_SUFFIXES = ("", "-wal", "-shm", "-journal")

# Individual statements, not one executescript() blob: sqlite3's executescript
# implicitly autocommits each statement (verified live — a mid-script failure
# leaves earlier CREATE TABLEs in place), so it cannot participate in the
# explicit BEGIN/COMMIT/ROLLBACK transaction _create_schema needs for
# all-or-nothing creation. Plain execute() calls inside our own explicit
# transaction can.
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE history_entry (
      id TEXT PRIMARY KEY,
      created_at_utc INTEGER NOT NULL,
      updated_at_utc INTEGER NOT NULL,
      payload_type TEXT NOT NULL CHECK(payload_type IN ('text','link','image','files')),
      direction TEXT NOT NULL CHECK(direction IN ('local','remote')),
      transport TEXT NOT NULL CHECK(transport IN ('local','p2s','p2p')),
      pinned INTEGER NOT NULL DEFAULT 0,
      byte_size INTEGER NOT NULL,
      encrypted_summary BLOB NOT NULL,
      encrypted_payload BLOB,
      blob_relative_path TEXT,
      payload_sha256 BLOB NOT NULL,
      source_device_id_hash BLOB,
      encrypted_source_device BLOB,
      file_state TEXT,
      expires_at_utc INTEGER,
      downloaded_directory_encrypted BLOB
    )
    """,
    "CREATE INDEX history_order ON history_entry(created_at_utc DESC, id DESC)",
    "CREATE INDEX history_type_order ON history_entry(payload_type, created_at_utc DESC)",
    "CREATE INDEX history_pinned_order ON history_entry(pinned, created_at_utc DESC)",
    "CREATE TABLE history_setting (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE schema_version (version INTEGER NOT NULL)",
)

_ENTRY_COLUMNS = (
    "id",
    "created_at_utc",
    "updated_at_utc",
    "payload_type",
    "direction",
    "transport",
    "pinned",
    "byte_size",
    "encrypted_summary",
    "encrypted_payload",
    "blob_relative_path",
    "payload_sha256",
    "source_device_id_hash",
    "encrypted_source_device",
    "file_state",
    "expires_at_utc",
    "downloaded_directory_encrypted",
)


class SchemaTooNewError(RuntimeError):
    """The on-disk schema is newer than this build understands; leave it untouched."""


class StoreCorruptError(RuntimeError):
    """SQLite integrity_check failed; caller must quarantine, never repair in place."""


def new_entry_id() -> str:
    return str(uuid.uuid4())


class HistoryStore:
    def __init__(self, history_dir: str):
        self.history_dir = history_dir
        self.db_path = os.path.join(history_dir, DB_FILE_NAME)
        self.blobs_dir = os.path.join(history_dir, BLOBS_DIR_NAME)
        self.thumbnails_dir = os.path.join(history_dir, THUMBNAILS_DIR_NAME)
        self._conn = None
        # Guards every _conn access: a queued capture writer runs on its own
        # background thread while callers may read from another, and Python's
        # sqlite3 module does not serialize concurrent statements for you.
        self._lock = threading.RLock()

    # --- lifecycle -----------------------------------------------------

    def open(self):
        os.makedirs(self.history_dir, exist_ok=True)
        os.makedirs(self.blobs_dir, exist_ok=True)
        os.makedirs(self.thumbnails_dir, exist_ok=True)
        with self._lock:
            # A prior _create_schema() that failed and rolled back (see
            # _explicit_transaction) leaves a valid-but-empty (0-byte) db
            # file behind, not a missing one — treat that the same as "no
            # file yet" so the next open() retries creation instead of
            # misdiagnosing an empty, never-initialized file as corrupt.
            is_new = (
                not os.path.isfile(self.db_path) or os.path.getsize(self.db_path) == 0
            )
            # connect + the PRAGMAs sit inside the guard too: on a db whose
            # header is corrupt they raise before integrity_check can ever
            # run, and that must surface as StoreCorruptError (callers
            # quarantine) rather than a raw sqlite3.Error that escapes
            # open() and aborts startup.
            conn = None
            try:
                conn = sqlite3.connect(
                    self.db_path, isolation_level=None, timeout=5.0, check_same_thread=False
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA busy_timeout=5000")
                self._conn = conn
                if is_new:
                    self._create_schema()
                else:
                    self._check_integrity()
                    self._migrate()
            except sqlite3.Error as error:
                if self._conn is not None:
                    self.close()
                elif conn is not None:
                    conn.close()
                raise StoreCorruptError(
                    f"Failed to open history database: {error}"
                ) from error
            except Exception:
                self.close()
                raise
        return self

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextlib.contextmanager
    def _explicit_transaction(self):
        """True atomicity for a multi-statement sequence under isolation_level=None.

        `with self._conn:` is a no-op here — the connection is opened in
        autocommit mode, where commit()/rollback() do nothing (verified
        live: conn.in_transaction stays False inside a `with conn:` block).
        executescript() cannot substitute either — it autocommits each of
        its own statements individually, so a mid-script failure leaves
        earlier statements applied (also verified live). This issues an
        explicit BEGIN IMMEDIATE, commits on success, rolls back on any
        exception — real all-or-nothing atomicity for the multi-statement
        sequences that need it (schema creation, migration).
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _create_schema(self):
        with self._explicit_transaction():
            for statement in SCHEMA_STATEMENTS:
                self._conn.execute(statement)
            self._conn.execute(
                "INSERT INTO schema_version(version) VALUES (?)",
                (CURRENT_SCHEMA_VERSION,),
            )

    def _check_integrity(self):
        try:
            row = self._conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.DatabaseError as error:
            raise StoreCorruptError(f"SQLite integrity_check raised: {error}") from error
        result = row[0] if row else None
        if result != "ok":
            raise StoreCorruptError(f"SQLite integrity_check failed: {result!r}")

    def schema_version(self) -> int:
        with self._lock:
            try:
                row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            except sqlite3.DatabaseError as error:
                raise StoreCorruptError(f"Failed reading schema_version: {error}") from error
            return int(row[0]) if row else 0

    def _migrate(self):
        """Forward-only, transactional, backed-up-before-migration schema upgrades.

        No migration exists yet above v1 (v1 is the first shipped schema).
        `MIGRATIONS` exists so a future v1->v2 step plugs in without touching
        this walk loop; tests exercise the walk loop itself by registering a
        synthetic migration.
        """
        version = self.schema_version()
        if version > CURRENT_SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"History schema v{version} is newer than supported v{CURRENT_SCHEMA_VERSION}"
            )
        while version < CURRENT_SCHEMA_VERSION:
            step = MIGRATIONS.get(version)
            if step is None:
                raise SchemaTooNewError(
                    f"No migration registered from schema v{version} to v{CURRENT_SCHEMA_VERSION}"
                )
            backup_path = f"{self.db_path}.pre-migration-v{version}.bak"
            # SQLite Online Backup API, not shutil.copyfile: the connection
            # is open in WAL mode, so a plain file copy can miss committed
            # data still living only in the -wal sidecar. backup() is
            # WAL-consistent; shutil stays imported for quarantine moves.
            backup_conn = sqlite3.connect(backup_path)
            try:
                self._conn.backup(backup_conn)
            finally:
                backup_conn.close()
            # Explicit BEGIN/COMMIT/ROLLBACK (see _explicit_transaction —
            # `with self._conn:` is a no-op under isolation_level=None),
            # with the rollback failure swallowed so it never masks the
            # error that triggered it.
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                step(self._conn)
                next_version = version + 1
                self._conn.execute("DELETE FROM schema_version")
                self._conn.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (next_version,)
                )
                self._conn.execute("COMMIT")
            except BaseException:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            version = next_version

    # --- entry CRUD ------------------------------------------------------

    def insert_entry(self, row: dict) -> None:
        values = [row.get(column) for column in _ENTRY_COLUMNS]
        placeholders = ", ".join("?" for _ in _ENTRY_COLUMNS)
        columns_sql = ", ".join(_ENTRY_COLUMNS)
        with self._lock, self._conn:
            self._conn.execute(
                f"INSERT INTO history_entry ({columns_sql}) VALUES ({placeholders})",
                values,
            )

    def get_entry(self, entry_id: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM history_entry WHERE id = ?", (entry_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_entries(
        self,
        payload_types=None,
        direction=None,
        pinned_only=False,
        cursor=None,
        limit=50,
    ):
        """Keyset-paginated, newest first. Returns (rows, has_more)."""
        clauses = []
        params = []
        if payload_types:
            placeholders = ", ".join("?" for _ in payload_types)
            clauses.append(f"payload_type IN ({placeholders})")
            params.extend(payload_types)
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        if pinned_only:
            clauses.append("pinned = 1")
        if cursor:
            cursor_created_at, cursor_id = _decode_cursor(cursor)
            clauses.append(
                "(created_at_utc < ? OR (created_at_utc = ? AND id < ?))"
            )
            params.extend([cursor_created_at, cursor_created_at, cursor_id])

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM history_entry {where_sql} "
            "ORDER BY created_at_utc DESC, id DESC LIMIT ?"
        )
        params.append(limit + 1)
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(sql, params).fetchall()]
        has_more = len(rows) > limit
        return rows[:limit], has_more

    def list_unpinned_oldest_first(self, limit=None):
        sql = "SELECT * FROM history_entry WHERE pinned = 0 ORDER BY created_at_utc ASC, id ASC"
        params = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def list_pending_transfers_expired(self, now_utc: int):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM history_entry WHERE file_state = 'ready' "
                "AND expires_at_utc IS NOT NULL AND expires_at_utc <= ?",
                (now_utc,),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_pinned(self, entry_id: str, pinned: bool, updated_at_utc: int) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE history_entry SET pinned = ?, updated_at_utc = ? WHERE id = ?",
                (1 if pinned else 0, updated_at_utc, entry_id),
            )
            return cursor.rowcount > 0

    def set_file_state(
        self,
        entry_id: str,
        file_state: str,
        updated_at_utc: int,
        downloaded_directory_encrypted=None,
    ) -> bool:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE history_entry SET file_state = ?, updated_at_utc = ?, "
                "downloaded_directory_encrypted = COALESCE(?, downloaded_directory_encrypted) "
                "WHERE id = ?",
                (file_state, updated_at_utc, downloaded_directory_encrypted, entry_id),
            )
            return cursor.rowcount > 0

    def expire_transfer(self, entry_id: str, updated_at_utc: int, retained_byte_size: int) -> bool:
        """Marks a files entry's pending transfer bytes expired: clears the
        blob reference and shrinks byte_size to what's actually retained (the
        encrypted summary/metadata), while keeping the row and its metadata."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE history_entry SET file_state = 'expired', blob_relative_path = NULL, "
                "byte_size = ?, updated_at_utc = ? WHERE id = ?",
                (retained_byte_size, updated_at_utc, entry_id),
            )
            return cursor.rowcount > 0

    def delete_entries(self, entry_ids):
        """Delete rows transactionally; returns the deleted rows (with blob paths)
        so the caller can remove blob files afterward per the retention transaction
        order (commit record changes first, then delete now-unreferenced blobs)."""
        entry_ids = list(entry_ids)
        if not entry_ids:
            return []
        placeholders = ", ".join("?" for _ in entry_ids)
        with self._lock, self._conn:
            rows = [
                dict(r)
                for r in self._conn.execute(
                    f"SELECT * FROM history_entry WHERE id IN ({placeholders})",
                    entry_ids,
                ).fetchall()
            ]
            self._conn.execute(
                f"DELETE FROM history_entry WHERE id IN ({placeholders})", entry_ids
            )
        return rows

    def list_all_ids(self):
        with self._lock:
            rows = self._conn.execute("SELECT id FROM history_entry").fetchall()
            return [r[0] for r in rows]

    def count_unpinned(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM history_entry WHERE pinned = 0"
            ).fetchone()
            return int(row[0])

    def total_bytes(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(SUM(byte_size), 0) FROM history_entry").fetchone()
            return int(row[0])

    def unpinned_bytes(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(byte_size), 0) FROM history_entry WHERE pinned = 0"
            ).fetchone()
            return int(row[0])

    # --- settings kv -----------------------------------------------------

    def get_setting(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM history_setting WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO history_setting(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # --- blob lifecycle ----------------------------------------------------

    def blob_path_for(self, entry_id: str) -> str:
        return os.path.join(self.blobs_dir, f"{entry_id}.bin")

    def write_blob_atomic(self, entry_id: str, data: bytes) -> str:
        """Temp file -> flush -> rename, completed before any SQLite row
        references it. Returns the path relative to `history_dir`."""
        destination = self.blob_path_for(entry_id)
        fd, temp_path = tempfile.mkstemp(prefix=".tmp-", dir=self.blobs_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, destination)
        except BaseException:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise
        return os.path.relpath(destination, self.history_dir)

    def read_blob(self, blob_relative_path: str) -> bytes:
        with open(os.path.join(self.history_dir, blob_relative_path), "rb") as f:
            return f.read()

    def delete_blob(self, blob_relative_path: str) -> bool:
        """Best-effort blob delete. False (not raised) means the caller should
        leave it for orphan_cleanup rather than claim the bytes as released."""
        if not blob_relative_path:
            return True
        path = os.path.join(self.history_dir, blob_relative_path)
        try:
            os.remove(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def orphan_cleanup(self, grace_period_seconds: int) -> dict:
        """Remove blob files with no referencing row, older than the grace
        period (so a blob mid-write for a not-yet-committed row is untouched)."""
        with self._lock:
            referenced = set()
            for r in self._conn.execute(
                "SELECT blob_relative_path FROM history_entry WHERE blob_relative_path IS NOT NULL"
            ).fetchall():
                referenced.add(os.path.normpath(os.path.join(self.history_dir, r[0])))

        removed = []
        failed = []
        cutoff = time.time() - grace_period_seconds
        if os.path.isdir(self.blobs_dir):
            for name in os.listdir(self.blobs_dir):
                if name.startswith(".tmp-"):
                    continue
                path = os.path.normpath(os.path.join(self.blobs_dir, name))
                if path in referenced:
                    continue
                try:
                    if os.path.getmtime(path) > cutoff:
                        continue
                except OSError:
                    continue
                try:
                    os.remove(path)
                    removed.append(path)
                except OSError:
                    failed.append(path)
        return {"removed": removed, "failed": failed}


def _decode_cursor(cursor: str):
    created_at_str, entry_id = cursor.split(":", 1)
    return int(created_at_str), entry_id


def encode_cursor(created_at_utc: int, entry_id: str) -> str:
    return f"{created_at_utc}:{entry_id}"


def quarantine_history_directory(history_dir: str, now_utc: int) -> str:
    """Move the db/key/blob set into a timestamped quarantine dir.

    Never deletes or overwrites anything; callers must have closed any open
    HistoryStore connection first (Windows locks open files). Safe to call
    even when the store never fully opened (DPAPI failure before store.open()).
    """
    quarantine_root = os.path.join(history_dir, QUARANTINE_DIR_NAME)
    os.makedirs(quarantine_root, exist_ok=True)
    destination = os.path.join(quarantine_root, str(now_utc))
    suffix = 0
    unique_destination = destination
    while os.path.exists(unique_destination):
        suffix += 1
        unique_destination = f"{destination}-{suffix}"
    os.makedirs(unique_destination)

    db_path = os.path.join(history_dir, DB_FILE_NAME)
    for db_suffix in _DB_SIDECAR_SUFFIXES:
        candidate = f"{db_path}{db_suffix}"
        if os.path.isfile(candidate):
            shutil.move(candidate, os.path.join(unique_destination, os.path.basename(candidate)))

    from history.crypto import MASTER_KEY_FILE_NAME

    key_path = os.path.join(history_dir, MASTER_KEY_FILE_NAME)
    if os.path.isfile(key_path):
        shutil.move(key_path, os.path.join(unique_destination, MASTER_KEY_FILE_NAME))

    blobs_dir = os.path.join(history_dir, BLOBS_DIR_NAME)
    if os.path.isdir(blobs_dir):
        shutil.move(blobs_dir, os.path.join(unique_destination, BLOBS_DIR_NAME))

    return unique_destination


# Forward-only schema migrations, keyed by the version migrated *from*. Empty
# today because v1 is the first shipped schema; a v1->v2 step would add
# MIGRATIONS[1] = _migrate_v1_to_v2.
MIGRATIONS = {}
