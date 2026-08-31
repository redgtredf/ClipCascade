"""T6 action-executor tests: copy-again suppression, safe downloads with
state transitions, folder/path actions and the destructive extras.

Runs against a REAL HistoryService (bootstrap on a temp dir) and fake
ClipboardManagers so the suppression contract is asserted at the exact
manager seam the monitor-triggered send path reads.
"""

import io
import os
import sys
from datetime import datetime, timezone

import pytest

from history import models, service
from history.actions import HistoryActionError, HistoryActionExecutor

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="history/ is Windows-only")


def _event(payload_type, payload, direction="local"):
    return models.HistoryCaptureEvent(
        direction=direction,
        payload_type=payload_type,
        payload=payload,
        source_device_id=None,
        source_device_name=None,
        transport=direction,
        occurred_at_utc=datetime.now(timezone.utc),
    )


class FakeManager:
    def __init__(self):
        self.previous_clipboard_hash = 0
        self.history_origin_suppress_token = None
        self.suppress_next_local_send = False
        self.pasted = []

    def paste(self, payload, payload_type="text"):
        self.pasted.append((payload, payload_type))

    @staticmethod
    def hash_clipboard(clipboard: str) -> int:
        return len(clipboard)  # deterministic stand-in


@pytest.fixture
def svc(tmp_path):
    result = service.bootstrap(str(tmp_path / "history"))
    assert result.enabled, result.disabled_reason
    yield result.service
    result.service.close()


@pytest.fixture
def managers():
    return [FakeManager(), FakeManager()]


@pytest.fixture
def executor(svc, managers):
    return HistoryActionExecutor(
        svc,
        lambda: managers,
        clipboard_clear=lambda: None,
        clipboard_text_setter=lambda text: None,
    )


def _text_entry(svc, text="hello again"):
    return svc.record(_event("text", text))


def _link_entry(svc):
    # Text captures are auto-classified: a complete http(s) URL becomes a
    # 'link' entry (service.classify_text).
    return svc.record(_event("text", "https://example.com/page"))


def _image_entry(svc):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 200, 30)).save(buffer, format="PNG")
    return svc.record(_event("image", buffer.getvalue()))


def _files_entry(svc, files=None):
    files = files or {"report.txt": b"data-one", "photo.bin": b"\x00\x01\x02"}
    return svc.record(_event("files", files))


# --- copy again -----------------------------------------------------------------


def test_copy_again_text_arms_suppression_and_pastes_once(svc, managers, executor):
    entry_id = _text_entry(svc, "retry this")
    result = executor.copy_again(entry_id)
    assert result["ok"] is True

    for manager in managers:
        assert manager.suppress_next_local_send is True
        assert manager.history_origin_suppress_token == f"history-copy-again:{entry_id}"
        assert manager.previous_clipboard_hash == FakeManager.hash_clipboard("retry this")
    assert managers[0].pasted == [("retry this", "text")]
    assert managers[1].pasted == []  # only the primary manager pastes


def test_copy_again_link_pastes_the_url(svc, managers, executor):
    entry_id = _link_entry(svc)
    executor.copy_again(entry_id)
    assert managers[0].pasted == [("https://example.com/page", "link")]


def test_copy_again_image_pastes_without_send_flag(svc, managers, executor):
    entry_id = _image_entry(svc)
    executor.copy_again(entry_id)
    payload, payload_type = managers[0].pasted[0]
    assert payload_type == "image"
    # Image pastes block the monitor for that event instead: no send flag,
    # because a lingering flag would swallow a later legitimate copy.
    assert all(m.suppress_next_local_send is False for m in managers)


def test_copy_again_files_is_rejected(svc, managers, executor):
    entry_id = _files_entry(svc)
    with pytest.raises(HistoryActionError) as excinfo:
        executor.copy_again(entry_id)
    assert excinfo.value.code == "not-supported-for-files"
    assert all(not m.pasted for m in managers)


def test_copy_again_missing_entry(svc, executor):
    with pytest.raises(HistoryActionError) as excinfo:
        executor.copy_again("no-such-entry")
    assert excinfo.value.code == "not-found"


# --- downloads -------------------------------------------------------------------


def test_download_files_writes_marks_downloaded_and_round_trips(svc, executor, tmp_path):
    entry_id = _files_entry(svc)
    target = tmp_path / "out"
    target.mkdir()

    result = executor.download_files(entry_id, str(target))
    assert result["ok"] is True
    assert all(item["ok"] for item in result["files"])
    assert (target / "report.txt").read_bytes() == b"data-one"

    detail = svc.get_detail(entry_id)
    assert detail.file_state == "downloaded"
    assert os.path.normpath(detail.downloaded_directory) == os.path.normpath(str(target))


def test_download_files_per_file_selection_and_partial_failure(svc, executor, tmp_path, monkeypatch):
    entry_id = _files_entry(svc)
    target = tmp_path / "out"
    target.mkdir()

    real_save = __import__("core.document_safety", fromlist=["save_received_files"]).save_received_files
    calls = []

    def flaky_save(files, directory):
        name = next(iter(files))
        calls.append(name)
        if name == "photo.bin":
            raise OSError("disk full")
        return real_save(files, directory)

    monkeypatch.setattr("history.actions.save_received_files", flaky_save)
    result = executor.download_files(entry_id, str(target))
    assert result["ok"] is True  # at least one file succeeded
    by_name = {item["name"]: item for item in result["files"]}
    assert by_name["report.txt"]["ok"] is True
    assert by_name["photo.bin"]["ok"] is False
    assert by_name["photo.bin"]["error"] == "io-error"
    assert svc.get_detail(entry_id).file_state == "downloaded"

    # Per-file selection: only the requested file is considered
    calls.clear()
    entry_id2 = _files_entry(svc)
    result2 = executor.download_files(entry_id2, str(target), filenames=["photo.bin"])
    assert [item["name"] for item in result2["files"]] == ["photo.bin"]


def test_download_files_unsafe_name_fails_before_write_and_state_stays_ready(
    svc, executor, tmp_path, monkeypatch
):
    entry_id = svc.record(_event("files", {"../escape.txt": b"x"}))
    target = tmp_path / "out"
    target.mkdir()
    result = executor.download_files(entry_id, str(target))
    assert result["ok"] is False
    assert result["files"][0]["error"] == "unsafe-filename"
    assert list(target.iterdir()) == []  # nothing written
    assert svc.get_detail(entry_id).file_state == "ready"


def test_download_files_expired_batch_is_rejected(svc, executor, tmp_path):
    from history import models as _models

    # The expiry stamp is applied at record time from the then-current
    # policy, so the zero-hours policy must be in place first.
    policy = svc.get_retention_policy()
    svc.set_retention_policy(
        _models.RetentionPolicy(
            keep_unpinned_days=policy.keep_unpinned_days,
            max_unpinned_entries=policy.max_unpinned_entries,
            max_storage_bytes=policy.max_storage_bytes,
            keep_pending_transfer_hours=0,
        )
    )
    entry_id = _files_entry(svc)
    svc.execute(models.ExpireTransfersNowCommand())
    assert svc.get_detail(entry_id).file_state == "expired"
    with pytest.raises(HistoryActionError) as excinfo:
        executor.download_files(entry_id, str(tmp_path))
    assert excinfo.value.code == "invalid-state:expired"


def test_download_missing_directory_is_a_state_error(svc, executor, tmp_path):
    entry_id = _files_entry(svc)
    with pytest.raises(HistoryActionError) as excinfo:
        executor.download_files(entry_id, str(tmp_path / "does-not-exist"))
    assert excinfo.value.code == "directory-missing"


# --- delete/clear never touch downloaded files -------------------------------------


def test_delete_entry_keeps_downloaded_files_on_disk(svc, executor, tmp_path):
    entry_id = _files_entry(svc)
    target = tmp_path / "out"
    target.mkdir()
    executor.download_files(entry_id, str(target))

    result = svc.execute(models.DeleteEntryCommand(entry_id))
    assert result.ok
    assert (target / "report.txt").exists()
    assert (target / "photo.bin").exists()


def test_clear_all_keeps_downloaded_files_on_disk(svc, executor, tmp_path):
    entry_id = _files_entry(svc)
    target = tmp_path / "out"
    target.mkdir()
    executor.download_files(entry_id, str(target))

    result = svc.execute(models.ClearAllCommand())
    assert result.ok
    assert (target / "report.txt").exists()


# --- images / folder / paths / clipboard clear ---------------------------------------


def test_save_image_writes_file_and_rejects_bad_targets(svc, executor, tmp_path):
    entry_id = _image_entry(svc)
    target = tmp_path / "clipboard.png"
    result = executor.save_image(entry_id, str(target))
    assert result["ok"] is True
    assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # A target in a directory that does not exist fails before any write.
    with pytest.raises(HistoryActionError) as excinfo:
        executor.save_image(entry_id, str(tmp_path / "no-such-dir" / "x.png"))
    assert excinfo.value.code == "invalid-target"
    assert not (tmp_path / "no-such-dir").exists()


def test_open_folder_validates_before_starting(svc, executor, tmp_path, monkeypatch):
    entry_id = _files_entry(svc)
    target = tmp_path / "out"
    target.mkdir()
    executor.download_files(entry_id, str(target))

    started = []
    monkeypatch.setattr(os, "startfile", lambda path: started.append(path))
    result = executor.open_folder(entry_id)
    assert result["ok"] is True
    assert started == [os.path.normpath(str(target))]

    # Before any download exists:
    entry_id2 = _files_entry(svc)
    with pytest.raises(HistoryActionError) as excinfo:
        executor.open_folder(entry_id2)
    assert excinfo.value.code == "never-downloaded"

    # After the folder vanished:
    import shutil

    shutil.rmtree(target)
    with pytest.raises(HistoryActionError) as excinfo:
        executor.open_folder(entry_id)
    assert excinfo.value.code == "directory-missing"


def test_copy_file_paths_lists_batch_files(svc, executor, tmp_path):
    copied = {}
    executor2 = HistoryActionExecutor(
        svc,
        lambda: [],
        clipboard_clear=lambda: None,
        clipboard_text_setter=lambda text: copied.setdefault("text", text),
    )
    entry_id = _files_entry(svc)
    target = tmp_path / "out"
    target.mkdir()
    executor.download_files(entry_id, str(target))

    result = executor2.copy_file_paths(entry_id)
    assert result["ok"] is True
    assert result["count"] == 2
    assert str(target / "report.txt") in copied["text"].splitlines()


def test_clear_windows_clipboard_uses_injected_seam(executor):
    assert executor.clear_windows_clipboard() == {"ok": True}


# --- mark_entry_downloaded command semantics ------------------------------------------


def test_mark_downloaded_command_guards(svc, tmp_path):
    text_id = _text_entry(svc)
    files_id = _files_entry(svc)

    assert svc.execute(models.MarkEntryDownloadedCommand("missing", str(tmp_path))).error == "not-found"
    assert (
        svc.execute(models.MarkEntryDownloadedCommand(text_id, str(tmp_path))).error
        == "not-a-file-batch"
    )
    result = svc.execute(models.MarkEntryDownloadedCommand(files_id, str(tmp_path)))
    assert result.ok
    # Second transition is invalid: ready -> downloaded happens once.
    assert (
        svc.execute(models.MarkEntryDownloadedCommand(files_id, str(tmp_path))).error
        == "invalid-state:downloaded"
    )
