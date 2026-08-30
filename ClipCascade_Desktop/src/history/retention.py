"""Deterministic retention preview and eviction, driven by an injectable clock.

`preview_impact` and `apply` share one selection function (`_plan`) so a
preview can never diverge from what `apply` actually removes — this is what
lets the acceptance criterion "retention previews exactly match subsequent
removals and released bytes" hold by construction, not by coincidence.

Transaction order matches the storage spec:
  1. Expire pending transfer bytes past `expires_at_utc` (metadata kept).
  2. Remove oldest unpinned entries beyond the age limit.
  3. Remove oldest unpinned entries beyond the count limit.
  4. Remove oldest unpinned payloads until byte usage is within budget.
  5. Commit row changes, then delete now-unreferenced blobs; failed blob
     deletion is queued for orphan cleanup and not counted as released.

A files entry with `expires_at_utc = NULL` never appears in step 1 — that is
how "keep transfer files until I delete them" is represented; no separate
schema flag is needed.
"""

import time

from history import models

SECONDS_PER_DAY = 86400


class Clock:
    def now_utc(self) -> int:
        raise NotImplementedError


class SystemClock(Clock):
    def now_utc(self) -> int:
        return int(time.time())


class FixedClock(Clock):
    """Test clock: fully controls "now" instead of racing the system clock."""

    def __init__(self, now_utc: int = 0):
        self._now = now_utc

    def now_utc(self) -> int:
        return self._now

    def set(self, now_utc: int) -> None:
        self._now = now_utc

    def advance(self, seconds: int) -> None:
        self._now += seconds


def _plan(store, policy: models.RetentionPolicy, now_utc: int) -> dict:
    """Pure selection: which rows retention would touch, without mutating
    anything. Never reads the system clock — `now_utc` is always passed in."""
    expiring_transfers = store.list_pending_transfers_expired(now_utc)
    expiring_transfer_ids = {row["id"] for row in expiring_transfers}
    retained_summary_bytes = {
        row["id"]: len(row["encrypted_summary"]) if row["encrypted_summary"] else 0
        for row in expiring_transfers
    }
    # Only the blob portion is released by transfer expiry; the row keeps
    # its (now smaller) summary-only footprint.
    transfer_bytes_released = sum(
        row["byte_size"] - retained_summary_bytes[row["id"]] for row in expiring_transfers
    )

    unpinned = store.list_unpinned_oldest_first()

    remove_ids = []
    remove_bytes = 0
    removed = set()

    if policy.keep_unpinned_days is not None:
        age_cutoff = now_utc - policy.keep_unpinned_days * SECONDS_PER_DAY
        for row in unpinned:
            if row["id"] in expiring_transfer_ids or row["id"] in removed:
                continue
            if row["created_at_utc"] < age_cutoff:
                remove_ids.append(row["id"])
                remove_bytes += row["byte_size"]
                removed.add(row["id"])

    if policy.max_unpinned_entries is not None:
        remaining_count = len(unpinned) - len(removed)
        overflow = remaining_count - policy.max_unpinned_entries
        if overflow > 0:
            taken = 0
            for row in unpinned:
                if taken >= overflow:
                    break
                if row["id"] in expiring_transfer_ids or row["id"] in removed:
                    continue
                remove_ids.append(row["id"])
                remove_bytes += row["byte_size"]
                removed.add(row["id"])
                taken += 1

    if policy.max_storage_bytes is not None:
        # Pinned bytes count toward the total but can never be evicted; if
        # they alone exceed the budget this loop simply exhausts unpinned
        # rows and stops (a UI-level warning is out of this ticket's scope).
        projected_total = store.total_bytes() - remove_bytes - transfer_bytes_released
        if projected_total > policy.max_storage_bytes:
            for row in unpinned:
                if projected_total <= policy.max_storage_bytes:
                    break
                if row["id"] in expiring_transfer_ids or row["id"] in removed:
                    continue
                remove_ids.append(row["id"])
                remove_bytes += row["byte_size"]
                removed.add(row["id"])
                projected_total -= row["byte_size"]

    return {
        "remove_ids": remove_ids,
        "remove_bytes": remove_bytes,
        "expiring_transfer_ids": list(expiring_transfer_ids),
        "expiring_transfer_bytes": transfer_bytes_released,
        "expiring_rows_by_id": {row["id"]: row for row in expiring_transfers},
        "retained_summary_bytes": retained_summary_bytes,
    }


def preview_impact(store, policy: models.RetentionPolicy, clock: Clock) -> models.RetentionImpact:
    plan = _plan(store, policy, clock.now_utc())
    # `bytes_to_release` is the total released across the whole pass — steps
    # 2-4's full-row evictions *plus* step 1's transfer-blob expiry — so it
    # stays equal to apply()'s `bytes_released`, which already combines both.
    # `transfer_bytes_to_expire` still exposes the step-1-only figure
    # separately for callers that want the breakdown.
    return models.RetentionImpact(
        entries_to_remove=len(plan["remove_ids"]),
        bytes_to_release=plan["remove_bytes"] + plan["expiring_transfer_bytes"],
        transfer_entries_to_expire=len(plan["expiring_transfer_ids"]),
        transfer_bytes_to_expire=plan["expiring_transfer_bytes"],
    )


def apply(store, policy: models.RetentionPolicy, clock: Clock) -> models.RetentionResult:
    now = clock.now_utc()
    plan = _plan(store, policy, now)

    bytes_released = 0
    blobs_pending_cleanup = 0

    # Step 1: expire pending transfer bytes; row stays, blob goes.
    transfer_entries_expired = 0
    for entry_id in plan["expiring_transfer_ids"]:
        row = plan["expiring_rows_by_id"][entry_id]
        old_blob_path = row["blob_relative_path"]
        retained_size = plan["retained_summary_bytes"][entry_id]
        if store.expire_transfer(entry_id, now, retained_size):
            transfer_entries_expired += 1
            if old_blob_path:
                if store.delete_blob(old_blob_path):
                    bytes_released += row["byte_size"] - retained_size
                else:
                    blobs_pending_cleanup += 1

    # Steps 2-4: remove oldest unpinned entries beyond age/count/byte limits.
    deleted_rows = store.delete_entries(plan["remove_ids"])
    for row in deleted_rows:
        blob_path = row.get("blob_relative_path")
        if not blob_path:
            bytes_released += row["byte_size"]
            continue
        if store.delete_blob(blob_path):
            bytes_released += row["byte_size"]
        else:
            blobs_pending_cleanup += 1

    return models.RetentionResult(
        entries_removed=len(deleted_rows),
        bytes_released=bytes_released,
        transfer_entries_expired=transfer_entries_expired,
        blobs_pending_cleanup=blobs_pending_cleanup,
    )
