"""Enums and immutable DTOs for the Windows clipboard history domain.

Pure data + protocols only — no SQLite, no DPAPI, no Qt. This is the contract
`ClipboardManager` (via `HistorySink`) and the later history UI/IPC tickets
build against.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional, Protocol, Union

# --- schema-aligned enums (values match the schema_v1 CHECK constraints) ---

PayloadType = Literal["text", "link", "image", "files"]
CapturePayloadType = Literal["text", "image", "files"]
Direction = Literal["local", "remote"]
Transport = Literal["local", "p2s", "p2p"]
FileBatchState = Literal["ready", "downloaded", "expired", "unavailable"]
SourceScope = Literal["this_pc", "other_devices"]

PAYLOAD_TYPES: tuple = ("text", "link", "image", "files")
DIRECTIONS: tuple = ("local", "remote")
TRANSPORTS: tuple = ("local", "p2s", "p2p")
FILE_BATCH_STATES: tuple = ("ready", "downloaded", "expired", "unavailable")

HistoryEntryId = str


# --- capture boundary (owned by this ticket's contract, wired by T2) ---


@dataclass(frozen=True)
class HistoryCaptureEvent:
    direction: Direction
    payload_type: CapturePayloadType
    payload: Union[str, bytes, dict]
    source_device_id: Optional[str]
    source_device_name: Optional[str]
    transport: Transport
    occurred_at_utc: datetime


class HistorySink(Protocol):
    def record(self, event: HistoryCaptureEvent) -> None: ...


# --- query/detail DTOs (already decrypted; size-bounded for list pages) ---


@dataclass(frozen=True)
class FileBatchItem:
    name: str
    size_bytes: int


@dataclass(frozen=True)
class HistoryEntrySummary:
    id: HistoryEntryId
    created_at_utc: datetime
    updated_at_utc: datetime
    payload_type: PayloadType
    direction: Direction
    transport: Transport
    pinned: bool
    byte_size: int
    preview: str
    source_device_name: Optional[str] = None
    file_state: Optional[FileBatchState] = None
    file_count: Optional[int] = None
    image_width: Optional[int] = None
    image_height: Optional[int] = None


@dataclass(frozen=True)
class HistoryEntryDetail(HistoryEntrySummary):
    text: Optional[str] = None
    url: Optional[str] = None
    image_bytes: Optional[bytes] = None
    files: tuple = ()
    downloaded_directory: Optional[str] = None


@dataclass(frozen=True)
class HistoryFilter:
    payload_types: Optional[frozenset] = None
    direction: Optional[Direction] = None
    pinned_only: bool = False


@dataclass(frozen=True)
class HistoryPage:
    entries: tuple
    next_cursor: Optional[str] = None
    has_more: bool = False


# --- retention ---


DEFAULT_KEEP_UNPINNED_DAYS = 30
DEFAULT_MAX_UNPINNED_ENTRIES = 500
DEFAULT_MAX_STORAGE_BYTES = 250 * 1024 * 1024
DEFAULT_KEEP_PENDING_TRANSFER_HOURS = 24
DEFAULT_RECORDING_ENABLED = True


@dataclass(frozen=True)
class RetentionPolicy:
    keep_unpinned_days: Optional[int] = DEFAULT_KEEP_UNPINNED_DAYS
    max_unpinned_entries: Optional[int] = DEFAULT_MAX_UNPINNED_ENTRIES
    max_storage_bytes: Optional[int] = DEFAULT_MAX_STORAGE_BYTES
    keep_pending_transfer_hours: Optional[int] = DEFAULT_KEEP_PENDING_TRANSFER_HOURS


@dataclass(frozen=True)
class RetentionImpact:
    entries_to_remove: int
    bytes_to_release: int
    transfer_entries_to_expire: int
    transfer_bytes_to_expire: int


@dataclass(frozen=True)
class RetentionResult:
    entries_removed: int
    bytes_released: int
    transfer_entries_expired: int
    blobs_pending_cleanup: int


# --- command boundary ---


@dataclass(frozen=True)
class PinEntryCommand:
    entry_id: HistoryEntryId


@dataclass(frozen=True)
class UnpinEntryCommand:
    entry_id: HistoryEntryId


@dataclass(frozen=True)
class DeleteEntryCommand:
    entry_id: HistoryEntryId


@dataclass(frozen=True)
class ClearUnpinnedCommand:
    pass


@dataclass(frozen=True)
class ClearAllCommand:
    pass


@dataclass(frozen=True)
class ExpireTransfersNowCommand:
    pass


@dataclass(frozen=True)
class SetRecordingEnabledCommand:
    enabled: bool


@dataclass(frozen=True)
class MarkEntryDownloadedCommand:
    """ready -> downloaded transition after a real on-disk write."""

    entry_id: HistoryEntryId
    downloaded_directory: str


Command = Union[
    PinEntryCommand,
    UnpinEntryCommand,
    DeleteEntryCommand,
    ClearUnpinnedCommand,
    ClearAllCommand,
    ExpireTransfersNowCommand,
    SetRecordingEnabledCommand,
    MarkEntryDownloadedCommand,
]


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    error: Optional[str] = None
    entry_id: Optional[HistoryEntryId] = None
    affected_count: int = 0
