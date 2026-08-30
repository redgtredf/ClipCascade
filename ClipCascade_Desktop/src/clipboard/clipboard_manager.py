import base64
import hashlib
import io
import json
import logging
import os
import time
import xxhash
from datetime import datetime, timezone
from typing import Optional


from PIL import Image
from core.constants import *
from core.config import Config
from core.document_safety import (
    DocumentNameError,
    sanitize_received_filenames,
    save_received_files,
)
from history.models import HistoryCaptureEvent, HistorySink

if PLATFORM.startswith(LINUX) and LINUX_USE_CLI_UI:
    from cli.tray import TaskbarPanel
else:
    from gui.tray import TaskbarPanel

if PLATFORM == WINDOWS or PLATFORM == MACOS:
    import pyperclip


if PLATFORM == WINDOWS:
    import win32clipboard
    from clipboard import clipboard_monitor_win as clipboard_monitor
elif PLATFORM == MACOS:
    import pasteboard
    from clipboard import clipboard_monitor_mac as clipboard_monitor
elif PLATFORM.startswith(LINUX):
    from clipboard import clipboard_monitor_linux as clipboard_monitor
    import subprocess


# History capture dedup: same fingerprint (type + direction/source + canonical
# bytes) seen again inside this window coalesces instead of creating a second
# history row. The cache itself is pruned well past the window so a
# long-running process never accumulates unbounded entries.
_HISTORY_DEDUP_WINDOW_SECONDS = 2.0
_HISTORY_DEDUP_CACHE_TTL_SECONDS = 30.0


class NoOpHistorySink:
    """Default `HistorySink`: discards every capture event.

    Keeps history-absent/disabled/non-Windows behaviour byte-for-byte
    identical to a build with no history code at all.
    """

    def record(self, event: HistoryCaptureEvent) -> None:
        pass


class ClipboardManager:
    def __init__(
        self,
        config: Config,
        history_sink: Optional[HistorySink] = None,
        history_transport: str = "local",
    ):
        self.config = config
        self.previous_clipboard_hash = 0
        self.sys_tray: TaskbarPanel = None
        self.is_files_download_enabled = False
        self.history_sink = history_sink or NoOpHistorySink()
        # Fixed at construction: which network transport (if any) this
        # manager's remote-direction captures should be attributed to.
        self.history_transport = history_transport
        # Stub for a future "Copy again" command (T6): set this to a
        # non-None marker immediately before calling paste() so the
        # resulting local capture isn't recorded as a fresh duplicate.
        # Consumed (reset to None) by the next local capture attempt.
        self.history_origin_suppress_token: Optional[str] = None
        # dedup key -> monotonic timestamp of the last capture attempt seen
        # for that (payload_type, direction/source, canonical-content) tuple.
        self._history_dedup_cache: dict = {}

        if PLATFORM.startswith(LINUX) and XMODE:
            self.is_x_clipboard_owner = clipboard_monitor.is_x_clipboard_owner()

    def set_tray_ref(self, sys_tray: TaskbarPanel):
        """
        Sets the system tray reference.
        """
        self.sys_tray = sys_tray

    def reset_files_download(self):
        """
        Resets the files download flag and disables the download functionality in the system tray if enabled.
        """
        if self.is_files_download_enabled:
            self.is_files_download_enabled = False
            if self.sys_tray:
                self.sys_tray.disable_files_download()

    sanitize_received_filenames = staticmethod(sanitize_received_filenames)
    save_received_files = staticmethod(save_received_files)

    @staticmethod
    def hash_clipboard(clipboard: str) -> int:
        # Encode explicitly: some xxhash releases no longer accept str
        # directly (raise "Strings must be encoded before hashing"), while
        # UTF-8 encoding here reproduces the same digest older releases
        # computed implicitly, so stored/compared hashes are unaffected.
        return xxhash.xxh64(clipboard.encode("utf-8")).intdigest()

    def is_clipboard_size_within_limit(
        self, clipboard_content: any, type_: str = "text"
    ) -> bool:
        if clipboard_content is None:
            raise ValueError("Clipboard content cannot be None")

        content_size_in_bytes = None
        if type_ == "text":
            content_size_in_bytes = len(clipboard_content.encode("utf-8"))
        elif type_ == "image":
            content_size_in_bytes = ClipboardManager.get_image_size(
                img=clipboard_content
            )
        elif type_ == "files":
            content_size_in_bytes = ClipboardManager.calculate_cumulative_file_size(
                files=clipboard_content
            )

        # Check if the content size exceeds the server limit
        max_allowed_size = self.config.data["maxsize"]
        if (
            max_allowed_size is not None
            and max_allowed_size >= 0
            and content_size_in_bytes > max_allowed_size
        ):
            logging.warning(
                "Clipboard content size exceeds the maximum allowed limit. "
                f"Allowed: {max_allowed_size} bytes, Found: {content_size_in_bytes} bytes."
            )
            return False

        # Check if the content size exceeds the local limit
        local_clipboard_size_limit = self.config.data[
            "max_clipboard_size_local_limit_bytes"
        ]
        if local_clipboard_size_limit is not None and local_clipboard_size_limit >= 0:
            if content_size_in_bytes > local_clipboard_size_limit:
                logging.warning(
                    "Clipboard content size exceeds the local allowed limit. "
                    f"Allowed: {local_clipboard_size_limit} bytes, Found: {content_size_in_bytes} bytes."
                )
                return False

        return True

    def has_clipboard_changed(self, payload: str) -> bool:
        """
        Check if the clipboard content has changed by comparing the current hash
        with the previous clipboard hash.

        Parameters:
        - payload: The current clipboard content.

        Returns:
        - True if the clipboard content has changed, False otherwise.
        """
        current_clipboard_hash = ClipboardManager.hash_clipboard(payload)
        if current_clipboard_hash != self.previous_clipboard_hash:
            self.previous_clipboard_hash = current_clipboard_hash
            return True
        return False

    def on_copy(self, copy_callback):
        clipboard_monitor.on_update(
            callback=lambda type_, content: self.clipboard_to_base64(
                copy_callback, content, type_
            ),
            enable_image_monitoring=self.config.data["enable_image_sharing"],
            enable_file_monitoring=self.config.data["enable_file_sharing"],
        )

    def clipboard_to_base64(self, callback, content: any, type_: str = "text"):
        try:
            self.reset_files_download()

            type_ = type_.lower()
            if type_ == "text":
                if self.is_clipboard_size_within_limit(content, type_):
                    self._try_capture_history(
                        lambda: self._build_local_capture_event("text", content, content)
                    )
                    callback(content, type_)

            elif type_ == "image":
                if isinstance(content, list):
                    if content is None or len(content) == 0:
                        raise ValueError(
                            "Clipboard image content cannot be None or empty"
                        )

                    content = Image.open(content[0])
                if self.is_clipboard_size_within_limit(content, type_):
                    content_str = ClipboardManager.convert_image_to_base64(img=content)
                    self._try_capture_history(
                        lambda: self._build_local_capture_event(
                            "image", base64.b64decode(content_str), content_str
                        )
                    )
                    callback(content_str, type_)

            elif type_ == "files":
                if PLATFORM.startswith(LINUX):
                    if content is not None and len(content) > 0:
                        temp = []
                        for path in content:
                            if path.startswith("file:"):
                                temp.append(path[5:])
                            else:
                                temp.append(path)
                        content = temp

                if self.is_clipboard_size_within_limit(content, type_):
                    content_str = ClipboardManager.convert_files_to_base64(
                        file_paths=content
                    )
                    if content_str != "{}":  # Check if the JSON string is empty
                        self._try_capture_history(
                            lambda: self._build_local_capture_event(
                                "files", ClipboardManager._files_to_bytes_map(content_str), content_str
                            )
                        )
                        callback(content_str, type_)
        except Exception as e:
            logging.error(f"Failed to convert clipboard data to base64: {e}")

    def base64_to_clipboard(self, base64_string: str, type_: str = "text"):
        try:
            if type_ == "text":
                txt = base64_string
                if self.is_clipboard_size_within_limit(txt, type_):
                    self.paste(txt, type_)
                    self._try_capture_history(
                        lambda: self._build_remote_capture_event("text", txt)
                    )
            elif type_ == "image":
                img = ClipboardManager.convert_base64_to_image(base64_img=base64_string)
                if self.is_clipboard_size_within_limit(img, type_):
                    self.paste(img, type_)
                    self._try_capture_history(
                        lambda: self._build_remote_capture_event(
                            "image", base64.b64decode(base64_string)
                        )
                    )
            elif type_ == "files":
                file_objects = ClipboardManager.convert_base64_to_files(
                    base64_json=base64_string
                )
                if self.is_clipboard_size_within_limit(file_objects, type_):
                    self.paste(file_objects, type_)
                    self._try_capture_history(
                        lambda: self._build_remote_files_capture_event(file_objects)
                    )
        except Exception as e:
            logging.error(f"Failed to convert base64 data to clipboard: {e}")

    # --- history capture (T2) -------------------------------------------
    #
    # Capture is a pure side-observation of already-validated clipboard
    # activity: it never gates, delays or mutates the surrounding sync path.
    # `_try_capture_history` swallows every exception a capture attempt can
    # raise (a bad decode, a sink failure) so a broken/absent history feature
    # can never break clipboard synchronisation.

    def _try_capture_history(self, build_event) -> None:
        try:
            event = build_event()
            if event is not None:
                self.history_sink.record(event)
        except Exception:
            logging.exception(
                "History capture failed; continuing without recording "
                "(metadata only, no payload logged)"
            )

    def _build_local_capture_event(
        self, payload_type: str, canonical_payload, hash_source: str
    ) -> Optional[HistoryCaptureEvent]:
        """Local-outbound capture, gated so an OS clipboard write caused by
        this manager's own `paste()` (a remote item echoing back into the
        local clipboard and retriggering the monitor) is never recorded as a
        fresh local entry. Reads `previous_clipboard_hash` only — never
        writes it, so echo-prevention/send behaviour is untouched."""
        if self.history_origin_suppress_token is not None:
            self.history_origin_suppress_token = None
            return None
        if ClipboardManager.hash_clipboard(hash_source) == self.previous_clipboard_hash:
            return None
        if self._should_coalesce_capture("local", "local", payload_type, canonical_payload):
            return None
        return HistoryCaptureEvent(
            direction="local",
            payload_type=payload_type,
            payload=canonical_payload,
            source_device_id=None,
            source_device_name=None,
            transport="local",
            occurred_at_utc=datetime.now(timezone.utc),
        )

    def _build_remote_capture_event(
        self, payload_type: str, canonical_payload
    ) -> Optional[HistoryCaptureEvent]:
        if self._should_coalesce_capture(
            "remote", self.history_transport, payload_type, canonical_payload
        ):
            return None
        return HistoryCaptureEvent(
            direction="remote",
            payload_type=payload_type,
            payload=canonical_payload,
            source_device_id=None,
            source_device_name=None,
            transport=self.history_transport,
            occurred_at_utc=datetime.now(timezone.utc),
        )

    def _build_remote_files_capture_event(
        self, file_objects: dict
    ) -> Optional[HistoryCaptureEvent]:
        """Only reached after size validation and a successful `paste()`.
        Safe-name validation runs here, for the history record only: an
        unsafe name raises and `_try_capture_history` drops the capture,
        while the already-completed `paste()` (and the existing
        save-time validation in `save_received_files`) are unaffected."""
        safe_files = self.sanitize_received_filenames(file_objects)
        files_bytes = {name: obj.getvalue() for name, obj in safe_files.items()}
        return self._build_remote_capture_event("files", files_bytes)

    @staticmethod
    def _files_to_bytes_map(content_str: str) -> dict:
        file_objects = ClipboardManager.convert_base64_to_files(content_str)
        return {name: obj.getvalue() for name, obj in file_objects.items()}

    # --- history capture deduplication (T2) -------------------------------
    #
    # Direction/source-aware: local and remote captures of identical content
    # never coalesce with each other (separate `source_scope` keys), matching
    # "direction must never be conflated". Device-level source identity is
    # T3's scope; `source_scope` uses direction ("local") or transport
    # ("p2s"/"p2p") as the best available proxy until then.

    @staticmethod
    def _canonical_bytes_for_dedup(payload_type: str, canonical_payload) -> bytes:
        if payload_type == "text":
            return canonical_payload.encode("utf-8")
        if payload_type == "image":
            return canonical_payload
        if payload_type == "files":
            parts = []
            for name in sorted(canonical_payload.keys()):
                parts.append(name.encode("utf-8"))
                parts.append(b"\x00")
                parts.append(canonical_payload[name])
                parts.append(b"\x00")
            return b"".join(parts)
        return b""

    def _should_coalesce_capture(
        self, direction: str, source_scope: str, payload_type: str, canonical_payload
    ) -> bool:
        canonical_bytes = ClipboardManager._canonical_bytes_for_dedup(
            payload_type, canonical_payload
        )
        digest = hashlib.sha256(
            b"|".join(
                [
                    payload_type.encode("utf-8"),
                    direction.encode("utf-8"),
                    source_scope.encode("utf-8"),
                    canonical_bytes,
                ]
            )
        ).hexdigest()

        now = time.monotonic()
        cache = self._history_dedup_cache
        cutoff = now - _HISTORY_DEDUP_CACHE_TTL_SECONDS
        for stale_key in [key for key, seen_at in cache.items() if seen_at < cutoff]:
            del cache[stale_key]

        last_seen = cache.get(digest)
        cache[digest] = now
        return last_seen is not None and (now - last_seen) <= _HISTORY_DEDUP_WINDOW_SECONDS

    @staticmethod
    def execute_command(*args, input_data):
        """
        Execute a command with input data.

        Parameters:
        - *args: Positional arguments for the command.
        - input_data: Input data to be passed to the command.
        """
        if PLATFORM.startswith(LINUX):
            try:
                subprocess.run(
                    args,
                    input=input_data,
                    check=True,
                )
            except Exception as e:
                logging.error(f"Failed to execute command: {e}")
                raise

    def paste(self, payload: any, payload_type: str = "text"):
        try:
            self.reset_files_download()

            if payload_type == "text":
                if PLATFORM == WINDOWS or PLATFORM == MACOS:
                    pyperclip.copy(payload)
                elif PLATFORM.startswith(LINUX):
                    if XMODE and self.is_x_clipboard_owner:
                        ClipboardManager.execute_command(
                            "xclip",
                            "-selection",
                            "clipboard",
                            input_data=payload.encode("utf-8"),
                        )
                    else:
                        ClipboardManager.execute_command(
                            "wl-copy",
                            input_data=payload.encode("utf-8"),
                        )

            elif payload_type == "image":
                if PLATFORM == WINDOWS:
                    # Save the image to a binary buffer in BMP format
                    with io.BytesIO() as output:
                        payload.convert("RGB").save(output, format="BMP")
                        bmp_data = output.getvalue()[14:]  # Skip BMP header (14 bytes)

                    win32clipboard.OpenClipboard()
                    win32clipboard.EmptyClipboard()
                    clipboard_monitor.enable_block_image_once()  # Block image copy to prevent deadlock
                    win32clipboard.SetClipboardData(win32clipboard.CF_DIB, bmp_data)
                    win32clipboard.CloseClipboard()
                elif PLATFORM == MACOS:
                    # Save the image to a binary buffer in TIFF format
                    with io.BytesIO() as output:
                        payload.convert("RGB").save(output, format="TIFF")
                        tiff_data = output.getvalue()

                    clipboard_monitor.enable_block_image_once()  # Block image copy to prevent deadlock
                    clipboard_monitor.write_to_pasteboard(tiff_data, pasteboard.TIFF)
                elif PLATFORM.startswith(LINUX):
                    # Save the image to a binary buffer in PNG format
                    with io.BytesIO() as output:
                        payload.convert("RGB").save(output, format="PNG")
                        png_data = output.getvalue()

                    clipboard_monitor.enable_block_image_once()  # Block image copy to prevent deadlock
                    if XMODE and self.is_x_clipboard_owner:
                        ClipboardManager.execute_command(
                            "xclip",
                            "-selection",
                            "clipboard",
                            "-t",
                            "image/png",
                            input_data=png_data,
                        )
                    else:
                        ClipboardManager.execute_command(
                            "wl-copy",
                            "--type",
                            "image/png",
                            input_data=png_data,
                        )

            elif payload_type == "files":
                if (
                    payload is not None
                    and isinstance(payload, dict)
                    and len(payload) > 0
                ):
                    if self.sys_tray is not None:
                        self.is_files_download_enabled = True
                        self.sys_tray.enable_files_download(files=payload)

        except Exception as e:
            logging.error(f"Failed to copy data to clipboard: {e}")
            raise

    def stop(self):
        self.reset_files_download()
        clipboard_monitor.stop()

    @staticmethod
    def calculate_cumulative_file_size(files: tuple | list | dict) -> int:
        """
        Calculate the cumulative size of a list of files.

        Args:
            files (tuple or list): A tuple of file paths.
            files (dict): A dictionary of files with file names as keys and file object as values.

        Returns:
            int: The cumulative size of the files in bytes.

        Raises:
            IOError: If a file cannot be read or processed.
        """
        cumulative_size = 0
        if isinstance(files, tuple | list):
            for file_path in files:
                try:
                    if os.path.isfile(file_path):
                        cumulative_size += os.path.getsize(file_path)
                except Exception as e:
                    raise IOError(
                        f"Failed to calculate size for file '{file_path}' {e}."
                    ) from e

        if isinstance(files, dict):
            for file_name, file_object in files.items():
                try:
                    cumulative_size += file_object.getbuffer().nbytes
                except Exception as e:
                    raise IOError(
                        f"Failed to calculate size for file '{file_name}' {e}."
                    ) from e

        return cumulative_size

    @staticmethod
    def convert_files_to_base64(file_paths: tuple | list) -> str:
        """
        Converts a list of files to base64-encoded strings and returns them as a JSON string.

        Args:
            file_paths (tuple or list): A tuple of file paths to be converted.

        Returns:
            str: A JSON string where file names are keys and base64-encoded content is values.

        Raises:
            IOError: If a file cannot be read or processed.
        """
        base64_encoded_files = {}
        for file_path in file_paths:
            try:
                if os.path.isfile(file_path):
                    file_name = os.path.basename(file_path)
                    with open(file_path, "rb") as file:
                        encoded_string = base64.b64encode(file.read()).decode("utf-8")
                        base64_encoded_files[file_name] = encoded_string
            except Exception as e:
                raise IOError(f"Failed to process file '{file_path}'. {e}") from e

        return json.dumps(base64_encoded_files)

    @staticmethod
    def convert_base64_to_files(base64_json: dict) -> dict:
        """
        Converts a JSON string with base64-encoded file content to a dictionary of file-like objects.

        Args:
            base64_json (str): A JSON string where file names are keys and base64-encoded content is values.

        Returns:
            dict: A dictionary where keys are file names, and values are BytesIO objects containing the decoded file data.
        """
        file_objects = {}
        try:
            base64_data = json.loads(base64_json)
            for file_name, encoded_content in base64_data.items():
                decoded_content = base64.b64decode(encoded_content)
                file_objects[file_name] = io.BytesIO(decoded_content)
        except Exception as e:
            raise IOError(f"Error processing base64 JSON. {e}") from e

        return file_objects

    @staticmethod
    def get_image_size(img: Image.Image | bytes) -> int:
        """
        Calculate the size of an image in bytes.

        Args:
            img (Image.Image | bytes): The image to calculate the size for.

        Returns:
            int: The size of the image in bytes.

        Raises:
            IOError: If the image cannot be processed or saved.
        """
        try:
            if isinstance(img, bytes):
                size_in_bytes = len(img)
            if isinstance(img, Image.Image):
                with io.BytesIO() as buffer:
                    img.save(buffer, format=img.format or "PNG")
                    size_in_bytes = buffer.tell()

            return size_in_bytes
        except Exception as e:
            raise IOError(f"Failed to calculate the image size. {e}") from e

    @staticmethod
    def convert_image_to_base64(img: Image.Image | bytes) -> str:
        """
        Converts an image to a base64-encoded string.

        Args:
            img (Image.Image | bytes): The image to be converted.

        Returns:
            str: The base64-encoded string representation of the image.

        Raises:
            IOError: If the image cannot be processed or saved.
        """
        try:
            if isinstance(img, bytes):
                base64_string = base64.b64encode(img).decode("utf-8")
            if isinstance(img, Image.Image):
                with io.BytesIO() as buffered:
                    img.save(buffered, format=img.format or "PNG")
                    base64_string = base64.b64encode(buffered.getvalue()).decode(
                        "utf-8"
                    )

            return base64_string
        except Exception as e:
            raise IOError(f"Failed to convert the image to base64. {e}") from e

    @staticmethod
    def convert_base64_to_image(base64_img: str) -> Image.Image:
        """
        Converts a base64-encoded string to a PIL Image object.

        Args:
            base64_img (str): The base64-encoded string to be converted.

        Returns:
            Image.Image: A PIL Image object representing the decoded image.

        Raises:
            ValueError: If the base64 string is invalid.
            IOError: If the image cannot be processed.
        """
        try:
            # Decode the base64 string
            image_data = base64.b64decode(base64_img)
        except base64.binascii.Error as e:
            raise ValueError("Invalid base64 string.") from e

        try:
            # Load the image from the decoded bytes
            with io.BytesIO(image_data) as image_stream:
                image = Image.open(image_stream)
                image.load()
            return image
        except IOError as e:
            raise IOError(f"Failed to process the image. {e}") from e
