"""Read-only detail pane: text, link, image and file-batch views.

Everything shown here comes from a single bounded `get_detail` IPC response
that the user explicitly requested by selecting an entry. There are no
action buttons: copy/open/download/pin/delete belong to ticket T6. Content
is rendered as plain text (never HTML), links are displayed with scheme and
hostname broken out, and corrupt or unavailable data degrades to a stable
metadata-only state instead of a broken preview.
"""

import base64

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from history_ui import theme

PAGE_EMPTY = 0
PAGE_LOADING = 1
PAGE_ERROR = 2
PAGE_CONTENT = 3

TYPE_TITLES = {
    "text": "Text",
    "link": "Web link",
    "image": "Image",
    "files": "File batch",
}

MAX_IMAGE_PREVIEW_PX = 420
MAX_FILE_ROWS_SHOWN = 50


def format_bytes(size):
    if size is None:
        return ""
    size = int(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size / 1.0:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def split_url(url):
    """(scheme, hostname) for display, or (None, None) when not parseable."""
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url.strip())
        if parts.scheme not in ("http", "https"):
            return None, None
        return parts.scheme, parts.hostname or ""
    except (ValueError, AttributeError):
        return None, None


def _chip(text, object_name):
    chip = QLabel(text)
    chip.setObjectName(object_name)
    chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return chip


class DetailPane(QStackedWidget):
    """Right-hand pane. Pages: empty -> loading -> error -> content."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Entry details")
        self._empty_label = QLabel("Select an entry to see its details.")
        self._empty_label.setObjectName("mutedLabel")
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

        self._loading_label = QLabel("Loading details…")
        self._loading_label.setObjectName("mutedLabel")

        self._error_label = QLabel("")
        self._error_label.setWordWrap(True)
        self._error_label.setAccessibleName("Details error message")

        self._content_scroll = QScrollArea()
        self._content_scroll.setWidgetResizable(True)
        self._content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content_holder = QWidget()
        self._content_layout = QVBoxLayout(self._content_holder)
        self._content_layout.setContentsMargins(
            theme.SPACING_L, theme.SPACING_L, theme.SPACING_L, theme.SPACING_L
        )
        self._content_layout.setSpacing(theme.SPACING_M)
        self._content_scroll.setWidget(self._content_holder)

        for widget in (self._empty_label, self._loading_label, self._error_label, self._content_scroll):
            self.addWidget(widget)
        self.show_empty()

    # --- state switches -----------------------------------------------------

    def show_empty(self):
        self.setCurrentIndex(PAGE_EMPTY)

    def show_loading(self):
        self.setCurrentIndex(PAGE_LOADING)

    def show_error(self, message):
        self._error_label.setText(f"Details are unavailable.\n\n{message}")
        self.setCurrentIndex(PAGE_ERROR)

    # --- content ------------------------------------------------------------

    def show_detail(self, detail):
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        payload_type = detail.get("payload_type", "text")
        title = TYPE_TITLES.get(payload_type, "Entry")

        heading = QLabel(title)
        heading.setObjectName("detailTitle")
        heading.setAccessibleName(f"{title} details")
        self._content_layout.addWidget(heading)

        meta = QLabel(self._meta_text(detail))
        meta.setObjectName("mutedLabel")
        meta.setWordWrap(True)
        self._content_layout.addWidget(meta)

        builder = {
            "text": self._build_text_body,
            "link": self._build_link_body,
            "image": self._build_image_body,
            "files": self._build_files_body,
        }.get(payload_type, self._build_text_body)
        builder(detail)

        self._content_layout.addStretch(1)
        self._content_scroll.setAccessibleName(f"{title} details")
        self.setCurrentIndex(PAGE_CONTENT)

    @staticmethod
    def _meta_text(detail):
        pieces = []
        source = detail.get("source_device_name")
        if not source:
            source = "This PC" if detail.get("direction") == "local" else "Remote device"
        pieces.append(f"From {source}")
        created = detail.get("created_at_utc")
        if created:
            try:
                from datetime import datetime

                local = datetime.fromisoformat(created).astimezone()
                pieces.append(local.strftime("%d %b %Y at %H:%M"))
            except (TypeError, ValueError):
                pass
        byte_size = detail.get("byte_size")
        if byte_size:
            pieces.append(format_bytes(byte_size))
        return " · ".join(pieces)

    def _add_body_label(self, text, accessible_name, plain=True):
        label = QLabel(text)
        label.setTextFormat(
            Qt.TextFormat.PlainText if plain else Qt.TextFormat.RichText
        )
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        label.setAccessibleName(accessible_name)
        self._content_layout.addWidget(label)
        return label

    # Text -------------------------------------------------------------------

    def _build_text_body(self, detail):
        self._add_body_label(detail.get("text") or "(empty text)", "Entry text")

    # Link -------------------------------------------------------------------

    def _build_link_body(self, detail):
        url = detail.get("url") or detail.get("preview", "")
        scheme, hostname = split_url(url)
        chip_row = QWidget()
        row_layout = QVBoxLayout(chip_row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(theme.SPACING_XS)
        if scheme == "https":
            row_layout.addWidget(_chip("HTTPS · Encrypted", "secureChip"))
        elif scheme == "http":
            row_layout.addWidget(_chip("Not encrypted", "warningChip"))
        if hostname:
            host_label = self._add_body_label(hostname, "Link hostname")
            host_label.setStyleSheet(f"color: {theme.COLORS.primary}; font-weight: 600;")
        self._content_layout.insertWidget(self._content_layout.count() - 1, chip_row)
        self._add_body_label(url or "(empty link)", "Full link address")

    # Image ------------------------------------------------------------------

    def _build_image_body(self, detail):
        dimensions = None
        width, height = detail.get("image_width"), detail.get("image_height")
        if width and height:
            dimensions = f"{width} × {height} pixels"
        size_text = format_bytes(detail.get("byte_size"))
        facts = " · ".join(piece for piece in (dimensions, size_text) if piece)
        if facts:
            self._add_body_label(facts, "Image facts")

        pixmap = self._decode_thumbnail(detail.get("image_base64"))
        if pixmap is None:
            self._add_body_label(
                "Preview unavailable — the stored image could not be decoded.",
                "Image preview status",
            )
            return
        preview = QLabel()
        preview.setObjectName("paneSurface")
        scaled = pixmap.scaled(
            MAX_IMAGE_PREVIEW_PX,
            MAX_IMAGE_PREVIEW_PX,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        preview.setPixmap(scaled)
        preview.setAccessibleName("Image preview")
        self._content_layout.addWidget(preview)

    @staticmethod
    def _decode_thumbnail(image_base64):
        if not image_base64:
            return None
        try:
            raw = base64.b64decode(image_base64, validate=False)
        except (ValueError, TypeError):
            return None
        pixmap = QPixmap()
        if not pixmap.loadFromData(raw):
            return None
        return pixmap

    # File batch --------------------------------------------------------------

    def _build_files_body(self, detail):
        state = detail.get("file_state")
        if state:
            chip = _chip(
                theme.FILE_STATE_LABELS.get(state, state), "stateChip"
            )
            chip.setStyleSheet(
                f"background: {theme.FILE_STATE_COLORS.get(state, theme.COLORS.muted)};"
                "color: #FFFFFF;"
            )
            self._content_layout.addWidget(chip)

        files = detail.get("files") or []
        total = sum(item.get("size_bytes", 0) for item in files)
        summary = f"{len(files)} files · {format_bytes(total)} in total"
        self._add_body_label(summary, "File batch summary")

        if detail.get("downloaded_directory"):
            self._add_body_label(
                f"Saved to: {detail['downloaded_directory']}",
                "Download location",
            )

        for item in files[:MAX_FILE_ROWS_SHOWN]:
            row = QLabel(f"{item.get('name', '')} — {format_bytes(item.get('size_bytes', 0))}")
            row.setTextFormat(Qt.TextFormat.PlainText)
            row.setWordWrap(False)
            row.setAccessibleName("File in batch")
            self._content_layout.addWidget(row)
        if len(files) > MAX_FILE_ROWS_SHOWN:
            self._add_body_label(
                f"… and {len(files) - MAX_FILE_ROWS_SHOWN} more files",
                "Additional files count",
            )
