"""Main-process execution of history actions requested by the history UI.

The history window runs in a separate process and never touches the OS
clipboard, the filesystem or the encrypted store. Every user action it
offers (copy again, download, open folder, ...) is requested over the
authenticated IPC channel and performed HERE, in the main process, through
the same safe paths normal clipboard operation uses:

- Copy again routes through `ClipboardManager.paste` with the history-origin
  suppression contract (send-flag + capture token + echo hash) so the
  re-copy neither records a duplicate entry nor resends over the network.
- Downloads go through `core.document_safety` (name validation, collision
  safety) and finish with a transactional ready->downloaded transition.
- Links and folders open via argument-safe OS APIs only (no shell strings).

Every method returns a plain dict and raises `HistoryActionError` with a
stable machine-readable code that the IPC layer maps onto error responses.
"""

import io
import logging
import os
from typing import Callable, List, Optional

from core.document_safety import (
    DocumentNameError,
    save_received_files,
    sanitize_received_filenames,
)
from history import models


class HistoryActionError(Exception):
    """Machine-readable action failure surfaced to the UI over IPC."""

    def __init__(self, code: str, message: str = None):
        self.code = code
        super().__init__(message or code)


def _default_clear_windows_clipboard():
    import platform as _platform

    if _platform.system() != "Windows":
        raise HistoryActionError("unsupported-platform")
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
    finally:
        win32clipboard.CloseClipboard()


def _default_set_clipboard_text(text: str):
    import pyperclip

    pyperclip.copy(text)


class HistoryActionExecutor:
    def __init__(
        self,
        service,
        managers_provider: Callable[[], List],
        clipboard_clear: Callable = None,
        clipboard_text_setter: Callable = None,
    ):
        self._service = service
        self._managers_provider = managers_provider
        self._clipboard_clear = clipboard_clear or _default_clear_windows_clipboard
        self._clipboard_text_setter = clipboard_text_setter or _default_set_clipboard_text

    # --- copy again ---------------------------------------------------------

    def copy_again(self, entry_id: str) -> dict:
        detail = self._service.get_detail(entry_id)
        if detail is None:
            raise HistoryActionError("not-found")
        payload_type = detail.payload_type
        if payload_type == "files":
            raise HistoryActionError("not-supported-for-files")

        if payload_type == "link":
            payload = detail.url or ""
        elif payload_type == "text":
            payload = detail.text or ""
        elif payload_type == "image":
            if not detail.image_bytes:
                raise HistoryActionError("payload-unavailable")
            from PIL import Image

            payload = Image.open(io.BytesIO(detail.image_bytes))
        else:
            raise HistoryActionError(f"unsupported-type:{payload_type}")

        managers = [m for m in self._managers_provider() if m is not None]
        if not managers:
            raise HistoryActionError("clipboard-unavailable")

        if payload_type in ("text", "link"):
            # The OS monitor will observe this paste as a local copy. Arm the
            # suppression contract on every manager so that monitor event is
            # swallowed once: no fresh history record, no network resend.
            # (Image pastes block the monitor for that event instead.)
            for manager in managers:
                manager.suppress_next_local_send = True
                manager.history_origin_suppress_token = f"history-copy-again:{entry_id}"
                if isinstance(payload, str):
                    manager.previous_clipboard_hash = manager.hash_clipboard(payload)

        managers[0].paste(payload, payload_type)
        return {"ok": True, "entry_id": entry_id, "payload_type": payload_type}

    # --- files / images -----------------------------------------------------

    def download_files(self, entry_id: str, target_directory: str, filenames: Optional[List[str]] = None) -> dict:
        detail = self._service.get_detail(entry_id)
        if detail is None:
            raise HistoryActionError("not-found")
        if detail.payload_type != "files":
            raise HistoryActionError("not-a-file-batch")
        target_directory = os.path.normpath(target_directory)
        if not os.path.isdir(target_directory):
            raise HistoryActionError("directory-missing")
        if detail.file_state != "ready":
            raise HistoryActionError(f"invalid-state:{detail.file_state}")

        payload = self._service.get_file_batch_payload(entry_id)
        if filenames is not None:
            wanted = set(filenames)
            payload = {name: data for name, data in payload.items() if name in wanted}
            if not payload:
                raise HistoryActionError("no-such-file")

        per_file = []
        for name, data in payload.items():
            try:
                written = save_received_files({name: io.BytesIO(data)}, target_directory)
                per_file.append({"name": name, "ok": True, "path": written[0]})
            except DocumentNameError:
                logging.warning("History download rejected an unsafe file name (metadata only)")
                per_file.append({"name": name, "ok": False, "error": "unsafe-filename"})
            except OSError:
                logging.warning("History download hit an I/O error (metadata only)")
                per_file.append({"name": name, "ok": False, "error": "io-error"})

        if any(item["ok"] for item in per_file):
            result = self._service.execute(
                models.MarkEntryDownloadedCommand(entry_id, target_directory)
            )
            if not result.ok:
                raise HistoryActionError(result.error or "state-update-failed")

        return {
            "ok": any(item["ok"] for item in per_file),
            "entry_id": entry_id,
            "files": per_file,
            "downloaded_directory": os.path.normpath(target_directory),
        }

    def save_image(self, entry_id: str, target_path: str) -> dict:
        detail = self._service.get_detail(entry_id)
        if detail is None:
            raise HistoryActionError("not-found")
        if detail.payload_type != "image" or not detail.image_bytes:
            raise HistoryActionError("payload-unavailable")

        directory, filename = os.path.split(target_path)
        directory = os.path.normpath(directory)
        if not filename or not os.path.isdir(directory):
            raise HistoryActionError("invalid-target")
        try:
            sanitize_received_filenames({filename: b""})
        except DocumentNameError:
            raise HistoryActionError("unsafe-filename")
        try:
            written = save_received_files({filename: io.BytesIO(detail.image_bytes)}, directory)
        except OSError:
            logging.warning("History image save hit an I/O error (metadata only)")
            raise HistoryActionError("io-error")
        return {"ok": True, "entry_id": entry_id, "path": written[0]}

    def open_folder(self, entry_id: str) -> dict:
        detail = self._service.get_detail(entry_id)
        if detail is None:
            raise HistoryActionError("not-found")
        directory = detail.downloaded_directory
        if not directory:
            raise HistoryActionError("never-downloaded")
        if not os.path.isdir(directory):
            raise HistoryActionError("directory-missing")
        os.startfile(directory)  # argument-safe documented API; never a shell string
        return {"ok": True, "entry_id": entry_id, "directory": directory}

    def copy_file_paths(self, entry_id: str) -> dict:
        detail = self._service.get_detail(entry_id)
        if detail is None:
            raise HistoryActionError("not-found")
        directory = detail.downloaded_directory
        if not directory:
            raise HistoryActionError("never-downloaded")
        paths = [os.path.join(directory, item.name) for item in (detail.files or ())]
        if not paths:
            raise HistoryActionError("no-files")
        self._clipboard_text_setter("\n".join(paths))
        return {"ok": True, "entry_id": entry_id, "count": len(paths)}

    # --- destructive extras ---------------------------------------------------

    def clear_windows_clipboard(self) -> dict:
        self._clipboard_clear()
        return {"ok": True}
