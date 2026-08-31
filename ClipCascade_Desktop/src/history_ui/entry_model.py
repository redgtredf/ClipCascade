"""Virtualised history entry list: QAbstractListModel + card delegate.

The model wraps plain summary dicts exactly as they arrive over IPC
(`history.ipc._summary_to_dict`'s shape); it never sees SQLite, keys or
payload bytes. QListView only materialises delegates for visible rows, so a
1,000-entry list costs the same as a 50-entry one.

Row painting is deliberately plain-text (QTextOption-less `elidedText`):
clipboard content is never rendered as HTML.
"""

from datetime import datetime

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QRect,
    QSize,
    Qt,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QStyle, QStyledItemDelegate

from history_ui import theme

TYPE_ROLE = Qt.ItemDataRole.UserRole + 1
PREVIEW_ROLE = TYPE_ROLE + 1
TIME_ROLE = TYPE_ROLE + 2
SOURCE_ROLE = TYPE_ROLE + 3
PINNED_ROLE = TYPE_ROLE + 4
STATE_LABEL_ROLE = TYPE_ROLE + 5
FILE_COUNT_ROLE = TYPE_ROLE + 6
DIMENSIONS_ROLE = TYPE_ROLE + 7
ENTRY_ID_ROLE = TYPE_ROLE + 8
IS_LINK_ROLE = TYPE_ROLE + 9
INSECURE_LINK_ROLE = TYPE_ROLE + 10

CARD_WIDTH_MARGIN = theme.SPACING_L * 2
CARD_MIN_HEIGHT = 64
CARD_VERTICAL_GAP = theme.SPACING_S
MAX_PREVIEW_LINES = 2


def format_row_time(iso_timestamp: str) -> str:
    """Local-time HH:MM for the card meta line (created_at_utc is UTC ISO)."""
    try:
        moment = datetime.fromisoformat(iso_timestamp)
    except (TypeError, ValueError):
        return ""
    local = moment.astimezone()
    return local.strftime("%H:%M")


def insecure_link(preview: str) -> bool:
    return preview.strip().lower().startswith("http://")


class HistoryListModel(QAbstractListModel):
    """Read-only display model over IPC summary dicts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows = []

    # --- Qt model plumbing -------------------------------------------------

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_text(row)
        if role == TYPE_ROLE:
            return theme.TYPE_LABELS.get(row.get("payload_type", ""), "TEXT")
        if role == PREVIEW_ROLE:
            return row.get("preview", "")
        if role == TIME_ROLE:
            return format_row_time(row.get("created_at_utc", ""))
        if role == SOURCE_ROLE:
            return row.get("source_device_name") or (
                "This PC" if row.get("direction") == "local" else "Remote device"
            )
        if role == PINNED_ROLE:
            return bool(row.get("pinned"))
        if role == STATE_LABEL_ROLE:
            state = row.get("file_state")
            return theme.FILE_STATE_LABELS.get(state) if state else None
        if role == FILE_COUNT_ROLE:
            return row.get("file_count")
        if role == DIMENSIONS_ROLE:
            width, height = row.get("image_width"), row.get("image_height")
            if width and height:
                return f"{width} × {height}"
            return None
        if role == ENTRY_ID_ROLE:
            return row.get("id")
        if role == IS_LINK_ROLE:
            return row.get("payload_type") == "link"
        if role == INSECURE_LINK_ROLE:
            return insecure_link(row.get("preview", ""))
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemNeverHasChildren
        )

    @staticmethod
    def _display_text(row):
        if row.get("payload_type") == "image":
            dimensions = ""
            width, height = row.get("image_width"), row.get("image_height")
            if width and height:
                dimensions = f"{width} × {height} · "
            return f"{dimensions}Image"
        return row.get("preview", "")

    # --- bulk content -------------------------------------------------------

    def set_rows(self, rows):
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self):
        return list(self._rows)

    def entry_id_at(self, row_number):
        if 0 <= row_number < len(self._rows):
            return self._rows[row_number].get("id")
        return None

    def row_of_entry_id(self, entry_id):
        for row_number, row in enumerate(self._rows):
            if row.get("id") == entry_id:
                return row_number
        return None


class HistoryItemDelegate(QStyledItemDelegate):
    """Paints one history card: type label, time, source, pin star and a
    two-line elided plain-text preview. Selected rows get the 2 px primary
    border from the design; keyboard focus is additionally outlined."""

    def sizeHint(self, option, index):
        height = max(CARD_MIN_HEIGHT, option.fontMetrics.height() * 4)
        return QSize(option.rect.width(), height + CARD_VERTICAL_GAP)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = self._card_rect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        focused = bool(option.state & QStyle.StateFlag.State_HasFocus)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.COLORS.surface))
        painter.drawRoundedRect(card, theme.RADIUS_CARD, theme.RADIUS_CARD)

        border_color = theme.COLORS.primary if selected else theme.COLORS.border
        pen = QPen(QColor(border_color), theme.BORDER_EMPHASIS if selected else theme.BORDER_RESTING)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(card, theme.RADIUS_CARD, theme.RADIUS_CARD)
        if focused:
            focus_pen = QPen(QColor(theme.COLORS.primary), theme.BORDER_EMPHASIS)
            focus_pen.setStyle(Qt.PenStyle.DotLine)
            painter.setPen(focus_pen)
            painter.drawRect(card.adjusted(-2, -2, 2, 2))

        padding = theme.SPACING_M
        content = card.adjusted(padding, padding - 2, -padding, -padding)
        metrics = option.fontMetrics

        self._paint_meta_line(painter, content, metrics, index, option)
        preview_top = content.top() + metrics.height() + theme.SPACING_XS
        preview_rect = QRect(
            content.left(), preview_top, content.width(), content.height() - metrics.height()
        )
        self._paint_preview(painter, preview_rect, metrics, index, option)
        painter.restore()

    @staticmethod
    def _card_rect(cell_rect):
        return QRect(
            cell_rect.left() + theme.SPACING_XS,
            cell_rect.top() + theme.SPACING_XS,
            cell_rect.width() - CARD_WIDTH_MARGIN,
            cell_rect.height() - CARD_VERTICAL_GAP,
        )

    def _paint_meta_line(self, painter, content, metrics, index, option):
        type_label = index.data(TYPE_ROLE) or "TEXT"
        file_count = index.data(FILE_COUNT_ROLE)
        if type_label == "FILES" and file_count:
            type_label = f"{type_label} · {file_count}"
        time_label = index.data(TIME_ROLE) or ""
        source_label = index.data(SOURCE_ROLE) or ""
        pinned = bool(index.data(PINNED_ROLE))

        type_font = QFont(option.font)
        type_font.setPointSizeF(max(metrics.height() / 2.4, 8.0))
        type_font.setBold(True)
        painter.setFont(type_font)
        painter.setPen(QColor(theme.COLORS.primary))
        type_width = QFontMetrics(type_font).horizontalAdvance(type_label)
        painter.drawText(
            QRect(content.left(), content.top(), type_width + theme.SPACING_XS, metrics.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            type_label,
        )

        meta_font = QFont(option.font)
        meta_font.setPointSizeF(max(metrics.height() / 2.6, 7.5))
        meta_metrics = QFontMetrics(meta_font)
        painter.setFont(meta_font)
        painter.setPen(QColor(theme.COLORS.muted))
        x = content.left() + type_width + theme.SPACING_S
        for label in (time_label, source_label):
            if not label:
                continue
            available = max(0, content.right() - x - theme.SPACING_L)
            elided_label = meta_metrics.elidedText(label, Qt.TextElideMode.ElideRight, available)
            label_width = meta_metrics.horizontalAdvance(elided_label)
            painter.drawText(
                QRect(x, content.top(), label_width, metrics.height()),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                elided_label,
            )
            x += label_width + theme.SPACING_S
            if x >= content.right():
                break

        if pinned:
            painter.setPen(QColor(theme.COLORS.accent))
            star_font = QFont(option.font)
            star_font.setPointSizeF(max(metrics.height() / 2.4, 9.0))
            painter.setFont(star_font)
            painter.drawText(
                QRect(content.right() - theme.SPACING_L, content.top(), theme.SPACING_L, metrics.height()),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                "★",
            )

    def _paint_preview(self, painter, rect, metrics, index, option):
        preview = index.data(PREVIEW_ROLE) or ""
        is_link = bool(index.data(IS_LINK_ROLE))
        if index.data(STATE_LABEL_ROLE):
            state = index.data(STATE_LABEL_ROLE)
            preview = f"{state} · {preview}" if preview else state
        elif index.data(DIMENSIONS_ROLE):
            preview = f"{index.data(DIMENSIONS_ROLE)} · {preview}" if preview else index.data(DIMENSIONS_ROLE)

        flags = Qt.TextFlag.TextSingleLine
        elided = metrics.elidedText(preview, Qt.TextElideMode.ElideRight, rect.width())
        if is_link:
            painter.setPen(QColor(theme.COLORS.accent if index.data(INSECURE_LINK_ROLE) else theme.COLORS.primary))
            font = QFont(option.font)
            font.setUnderline(True)
            painter.setFont(font)
        else:
            painter.setPen(QColor(theme.COLORS.text))
            painter.setFont(option.font)
        painter.drawText(rect, flags | Qt.AlignmentFlag.AlignVCenter, elided)
