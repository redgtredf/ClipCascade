"""Received-document path-safety tests (Task B3)."""

import io

import pytest

from core.document_safety import DocumentNameError, save_received_files, sanitize_received_filenames


def _files(*names):
    return {name: io.BytesIO(b"payload") for name in names}


def test_safe_filename_saves_inside_target_directory(tmp_path):
    files = _files("report.txt")
    written = save_received_files(files, str(tmp_path))
    assert len(written) == 1
    saved = tmp_path / "report.txt"
    assert saved.is_file()
    assert saved.read_bytes() == b"payload"
    assert saved.parent == tmp_path


def test_traversal_absolute_nested_and_dot_names_rejected():
    for bad in ["../escape.txt", "..\\escape.txt", "C:\\evil.txt", "sub/dir.txt", "sub\\dir.txt", ".", "..", "", "  ", "/abs.txt"]:
        with pytest.raises(DocumentNameError):
            sanitize_received_filenames({bad: io.BytesIO(b"x")})


def test_duplicate_output_names_rejected():
    # Identical keys collapse at dict construction; case variants are the
    # realistic duplicate vector on a case-sensitive mapping.
    with pytest.raises(DocumentNameError):
        sanitize_received_filenames(_files("a.txt", "A.txt"))
    with pytest.raises(DocumentNameError):
        sanitize_received_filenames(_files("Report.pdf", "report.PDF"))


def test_existing_file_not_silently_overwritten(tmp_path):
    original = tmp_path / "report.txt"
    original.write_bytes(b"original")
    written = save_received_files({"report.txt": io.BytesIO(b"new")}, str(tmp_path))
    assert original.read_bytes() == b"original"
    assert len(written) == 1
    assert written[0] != str(original)
    assert (tmp_path / "report (1).txt").read_bytes() == b"new"


def test_cancelled_or_empty_selection_leaves_files_untouched(tmp_path):
    assert save_received_files({}, str(tmp_path)) == []
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(DocumentNameError):
        save_received_files({"../x.txt": io.BytesIO(b"x"), "ok.txt": io.BytesIO(b"x")}, str(tmp_path))
    assert list(tmp_path.iterdir()) == []  # batch rejected before anything was written


def test_clearing_pending_item_releases_payload_reference(manager):
    class FakeTray:
        def __init__(self):
            self.enabled_with = None
            self.disable_calls = 0

        def enable_files_download(self, files):
            self.enabled_with = files

        def disable_files_download(self):
            self.disable_calls += 1
            if self.enabled_with is not None:
                self.enabled_with.clear()
            self.enabled_with = None

    tray = FakeTray()
    manager.set_tray_ref(tray)
    payload = {"doc.txt": io.BytesIO(b"data")}
    manager.paste(payload, "files")
    assert tray.enabled_with is payload
    assert manager.is_files_download_enabled is True

    manager.reset_files_download()
    assert payload == {}  # in-memory payload released via tray contract
    assert manager.is_files_download_enabled is False
