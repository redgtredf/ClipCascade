"""Retention preview/apply parity and eviction-order tests, all on a fake clock."""

import sys
from datetime import datetime, timezone

import pytest

from history import models, retention, service

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="history/ is Windows-only")

NOW = 1_700_000_000


@pytest.fixture
def svc(tmp_path):
    clock = retention.FixedClock(now_utc=NOW)
    result = service.bootstrap(str(tmp_path / "history"), clock=clock)
    assert result.enabled, result.disabled_reason
    yield result.service, clock
    result.service.close()


def _record(service_obj, seconds_ago, pinned=False, text="x", payload_type="text"):
    ts = NOW - seconds_ago
    payload = text if payload_type == "text" else {"a.bin": b"content"}
    event = models.HistoryCaptureEvent(
        direction="local",
        payload_type=payload_type,
        payload=payload,
        source_device_id=None,
        source_device_name=None,
        transport="local",
        occurred_at_utc=datetime.fromtimestamp(ts, tz=timezone.utc),
    )
    entry_id = service_obj.record(event)
    if pinned:
        service_obj.execute(models.PinEntryCommand(entry_id=entry_id))
    return entry_id


def test_age_based_eviction_spares_pinned_and_recent_entries(svc):
    service_obj, clock = svc
    old_unpinned = _record(service_obj, seconds_ago=40 * 86400)
    old_pinned = _record(service_obj, seconds_ago=40 * 86400, pinned=True)
    recent = _record(service_obj, seconds_ago=1 * 86400)

    policy = models.RetentionPolicy(keep_unpinned_days=30, max_unpinned_entries=None, max_storage_bytes=None, keep_pending_transfer_hours=None)
    impact = service_obj.preview_retention(policy)
    assert impact.entries_to_remove == 1

    result = service_obj.apply_retention(policy)
    assert result.entries_removed == 1

    remaining_ids = {e.id for e in service_obj.query().entries}
    assert old_unpinned not in remaining_ids
    assert old_pinned in remaining_ids
    assert recent in remaining_ids


def test_preview_exactly_matches_subsequent_apply(svc):
    service_obj, clock = svc
    for i in range(8):
        _record(service_obj, seconds_ago=i)

    policy = models.RetentionPolicy(keep_unpinned_days=None, max_unpinned_entries=3, max_storage_bytes=None, keep_pending_transfer_hours=None)
    impact = service_obj.preview_retention(policy)
    result = service_obj.apply_retention(policy)

    assert result.entries_removed == impact.entries_to_remove
    assert result.bytes_released == impact.bytes_to_release

    # Applying again against the now-stable state previews and removes nothing.
    impact2 = service_obj.preview_retention(policy)
    assert impact2.entries_to_remove == 0
    assert impact2.bytes_to_release == 0


def test_count_based_eviction_removes_oldest_first(svc):
    service_obj, clock = svc
    ids = [_record(service_obj, seconds_ago=5 - i, text=f"item-{i}") for i in range(5)]

    policy = models.RetentionPolicy(keep_unpinned_days=None, max_unpinned_entries=3, max_storage_bytes=None, keep_pending_transfer_hours=None)
    result = service_obj.apply_retention(policy)
    assert result.entries_removed == 2

    remaining = {e.id for e in service_obj.query().entries}
    assert ids[0] not in remaining
    assert ids[1] not in remaining
    assert ids[2] in remaining
    assert ids[3] in remaining
    assert ids[4] in remaining


def test_byte_based_eviction_stops_once_budget_is_met(svc):
    service_obj, clock = svc
    for i in range(6):
        _record(service_obj, seconds_ago=6 - i, text="x" * 200)

    store = service_obj._store
    total_before = store.total_bytes()
    # Budget that requires evicting roughly half the entries.
    budget = total_before // 2

    policy = models.RetentionPolicy(keep_unpinned_days=None, max_unpinned_entries=None, max_storage_bytes=budget, keep_pending_transfer_hours=None)
    result = service_obj.apply_retention(policy)
    assert result.entries_removed > 0
    assert store.total_bytes() <= budget


def test_pinned_bytes_are_never_evicted_even_over_budget(svc):
    service_obj, clock = svc
    pinned_id = _record(service_obj, seconds_ago=5, pinned=True, text="y" * 500)

    policy = models.RetentionPolicy(keep_unpinned_days=None, max_unpinned_entries=None, max_storage_bytes=1, keep_pending_transfer_hours=None)
    result = service_obj.apply_retention(policy)
    assert result.entries_removed == 0

    remaining = {e.id for e in service_obj.query().entries}
    assert pinned_id in remaining


def test_pending_transfer_bytes_expire_on_their_own_policy_and_keep_metadata(svc):
    service_obj, clock = svc
    files_id = _record(service_obj, seconds_ago=0, payload_type="files", pinned=True)

    row = service_obj._store.get_entry(files_id)
    assert row["file_state"] == "ready"

    clock.advance(25 * 3600)  # past the default 24h transfer window

    policy = models.RetentionPolicy()
    impact = service_obj.preview_retention(policy)
    assert impact.transfer_entries_to_expire == 1

    result = service_obj.apply_retention(policy)
    assert result.transfer_entries_expired == 1

    row_after = service_obj._store.get_entry(files_id)
    assert row_after["file_state"] == "expired"
    assert row_after["blob_relative_path"] is None
    assert row_after["pinned"] == 1  # pinned metadata survives byte expiry

    detail = service_obj.get_detail(files_id)
    assert len(detail.files) == 1  # filenames/sizes preserved after expiry


def test_preview_matches_apply_when_transfer_expiry_and_eviction_coincide(svc):
    """The normal, expected shape of a routine retention run: a pending
    transfer expires (step 1) in the same pass as an age-triggered eviction
    (steps 2-4). The byte totals — not just the entry counts — must match
    exactly between preview and apply."""
    service_obj, clock = svc
    files_id = _record(service_obj, seconds_ago=0, payload_type="files")
    old_text_id = _record(service_obj, seconds_ago=40 * 86400, text="old text" * 20)

    clock.advance(25 * 3600)  # files transfer now past 24h; old_text still past 30 days

    policy = models.RetentionPolicy()  # defaults: 30 days / 500 / 250MB / 24h
    impact = service_obj.preview_retention(policy)
    result = service_obj.apply_retention(policy)

    assert impact.transfer_entries_to_expire == 1
    assert impact.entries_to_remove == 1
    assert result.transfer_entries_expired == 1
    assert result.entries_removed == 1

    # The regression: byte totals must match exactly, not just entry counts.
    assert impact.bytes_to_release == result.bytes_released
    assert impact.bytes_to_release > 0

    remaining = {e.id for e in service_obj.query().entries}
    assert old_text_id not in remaining
    assert files_id in remaining  # expired, not deleted — metadata survives


def test_transfer_with_no_expiry_set_never_auto_expires(svc):
    service_obj, clock = svc
    files_id = _record(service_obj, seconds_ago=0, payload_type="files")
    # "Keep transfer files until I delete them" == clearing expires_at_utc.
    service_obj._store.expire_transfer  # sanity: method exists
    with service_obj._store._lock, service_obj._store._conn:
        service_obj._store._conn.execute(
            "UPDATE history_entry SET expires_at_utc = NULL WHERE id = ?", (files_id,)
        )

    clock.advance(365 * 86400)
    impact = service_obj.preview_retention(models.RetentionPolicy())
    assert impact.transfer_entries_to_expire == 0
