"""Placeholder history window used by the T0 packaging probe.

Deliberately unstyled and empty: it exists to measure what a real PySide6
window costs to package, launch and keep resident. The approved three-pane
design replaces its contents in the UI ticket.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

WINDOW_TITLE = "ClipCascade history"
DEFAULT_SIZE = (900, 600)


class HistoryWindow(QWidget):
    def __init__(self, on_first_paint=None):
        super().__init__()
        self._on_first_paint = on_first_paint
        self._painted = False
        self.focus_requests = 0

        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*DEFAULT_SIZE)

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading = QLabel("Clipboard history")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subheading = QLabel(
            "Packaging probe only - no history is stored or shown yet."
        )
        subheading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        layout.addWidget(subheading)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._painted:
            return
        self._painted = True
        if self._on_first_paint is not None:
            self._on_first_paint()

    def present(self):
        """Show, restore and raise the window for a first or repeat open."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()

    def handle_focus_request(self):
        self.focus_requests += 1
        self.present()
