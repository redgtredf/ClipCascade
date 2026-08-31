"""The three-pane clipboard-history window.

Left: type/source filters with live counts. Middle: the virtualised entry
list (QListView + HistoryListModel). Right: the detail pane with
state-appropriate actions. Header: search (150 ms debounce). Footer: status
totals, which double as the screen-reader live region.

T6: every mutation is dispatched through the controller's gateway (the
authenticated IPC channel) and executed in the main process -- this window
process still never touches the clipboard, filesystem or store. Link
opening validates through `link_policy`, confirms per untrusted hostname
and opens via argument-safe OS APIs only. Geometry is restored from
QSettings and clamped to visible monitors.
"""

import os

from PySide6.QtCore import QRect, QSettings, Qt, QUrl, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QListView,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from history_ui import link_policy, theme
from history_ui.controller import HistoryController
from history_ui.detail_views import DetailPane
from history_ui.entry_model import (
    ENTRY_ID_ROLE,
    PREVIEW_ROLE,
    STATE_LABEL_ROLE,
    HistoryItemDelegate,
    HistoryListModel,
)

WINDOW_TITLE = "ClipCascade history"
DEFAULT_SIZE = (1040, 700)
MIN_SIZE = (820, 560)
GEOMETRY_KEY = "window/geometry"

TYPE_FILTERS = (
    ("all", "All items"),
    ("pinned", "Pinned"),
    ("text", "Text"),
    ("link", "Links"),
    ("image", "Images"),
    ("files", "Files"),
)

SOURCE_FILTERS = (
    ("all", "All sources"),
    ("local", "This PC"),
    ("remote", "Other devices"),
)


class HistoryWindow(QMainWindow):
    """Read-only history browser. The T0 contract (present, focus handling,
    first-paint hook) is preserved for `history_ui.main` and the launcher."""

    def __init__(self, on_first_paint=None, gateway=None, settings=None, ui_scale=1.0, synchronous=False):
        super().__init__()
        self._on_first_paint = on_first_paint
        self._painted = False
        self.focus_requests = 0
        self._restored_selection_done = False
        self._current_detail = None
        self._pending_retention_dialog = False
        self._last_announcement = ""

        self.setWindowTitle(WINDOW_TITLE)
        self.resize(*DEFAULT_SIZE)
        self.setMinimumSize(*MIN_SIZE)
        self.setAccessibleName("Clipboard history")
        self.setStyleSheet(theme.build_stylesheet(ui_scale))

        self._settings = settings or QSettings("ClipCascade", "ClipCascadeHistory")
        self._gateway = gateway
        self.model = HistoryListModel(self)
        self.controller = (
            HistoryController(gateway, parent=self, synchronous=synchronous)
            if gateway is not None
            else None
        )

        self._build_ui()
        self._build_shortcuts()
        if self.controller is not None:
            self._wire_controller()

        self._restore_and_clamp_geometry()

    # --- UI composition -------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        central.setAccessibleName("History window content")
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._build_nav_pane())
        body.addWidget(self._build_list_pane(), stretch=390)
        body.addWidget(self._build_detail_pane(), stretch=520)
        root.addLayout(body, stretch=1)

        root.addWidget(self._build_footer())
        self.setCentralWidget(central)

    def _build_header(self):
        header = QFrame()
        header.setObjectName("paneSurface")
        header.setAccessibleName("Search header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(theme.SPACING_L, theme.SPACING_M, theme.SPACING_L, theme.SPACING_M)
        layout.setSpacing(theme.SPACING_L)

        brand = QLabel("Clipboard history")
        brand.setObjectName("brandLabel")
        brand.setAccessibleName("Clipboard history heading")
        layout.addWidget(brand)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search text, links and file names    Ctrl+F")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setAccessibleName("Search history")
        self.search_box.setAccessibleDescription(
            "Searches copied text, link hostnames, file names, sources and types"
        )
        self.search_box.textChanged.connect(self._on_search_text_changed)
        layout.addWidget(self.search_box, stretch=1)
        return header

    def _build_nav_pane(self):
        nav = QFrame()
        nav.setFixedWidth(220)
        nav.setAccessibleName("Filters")
        nav_layout = QVBoxLayout(nav)
        nav_layout.setContentsMargins(theme.SPACING_M, theme.SPACING_L, theme.SPACING_M, theme.SPACING_L)
        nav_layout.setSpacing(theme.SPACING_XS)

        nav_layout.addWidget(self._section_label("History"))
        self._type_group = QButtonGroup(self)
        self._type_buttons = {}
        for value, label in TYPE_FILTERS:
            button = self._nav_button(f"{label}", f"Filter: {label}")
            button.clicked.connect(lambda _checked=False, v=value: self._on_type_filter(v))
            self._type_group.addButton(button)
            self._type_buttons[value] = button
            nav_layout.addWidget(button)
        self._type_buttons["all"].setChecked(True)

        nav_layout.addSpacing(theme.SPACING_L)
        nav_layout.addWidget(self._section_label("Source"))
        self._source_group = QButtonGroup(self)
        self._source_buttons = {}
        for value, label in SOURCE_FILTERS:
            button = self._nav_button(label, f"Source filter: {label}")
            button.clicked.connect(lambda _checked=False, v=value: self._on_source_filter(v))
            self._source_group.addButton(button)
            self._source_buttons[value] = button
            nav_layout.addWidget(button)
        self._source_buttons["all"].setChecked(True)

        nav_layout.addSpacing(theme.SPACING_L)
        self.order_button = self._nav_button("Oldest first", "Sort order: newest first; activate for oldest first")
        self.order_button.setCheckable(True)
        self.order_button.toggled.connect(self._on_order_toggled)
        nav_layout.addWidget(self.order_button)

        nav_layout.addSpacing(theme.SPACING_L)
        self.retention_button = self._nav_button("Manage retention…", "Preview and apply history retention")
        self.retention_button.clicked.connect(self._on_manage_retention)
        nav_layout.addWidget(self.retention_button)
        self.clear_button = self._nav_button("Clear history…", "Clear unpinned or all history entries")
        self.clear_button.clicked.connect(self._on_clear_history)
        nav_layout.addWidget(self.clear_button)

        nav_layout.addStretch(1)
        return nav

    def _nav_button(self, label, accessible_name):
        button = QPushButton(label)
        button.setCheckable(True)
        button.setProperty("navButton", True)
        button.setStyleSheet(theme.nav_button_stylesheet())
        button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        button.setAccessibleName(accessible_name)
        return button

    @staticmethod
    def _section_label(text):
        label = QLabel(text.upper())
        label.setObjectName("sectionLabel")
        label.setAccessibleName(f"{text} section")
        return label

    def _build_list_pane(self):
        pane = QWidget()
        pane.setAccessibleName("History entry list")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(theme.SPACING_M, theme.SPACING_M, theme.SPACING_S, theme.SPACING_M)
        self.list_stack = QStackedWidget()
        self.list_stack.setAccessibleName("History list states")

        self.list_view = QListView()
        self.list_view.setModel(self.model)
        self.list_view.setItemDelegate(HistoryItemDelegate(self.list_view))
        self.list_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerItem)
        self.list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_view.setMouseTracking(True)
        self.list_view.setUniformItemSizes(True)
        self.list_view.setAccessibleName("History entries")
        self.list_view.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.list_view.selectionModel().currentChanged.connect(self._on_current_changed)
        self.list_view.verticalScrollBar().valueChanged.connect(self._on_scroll_moved)
        self.list_stack.addWidget(self.list_view)

        self.loading_label = QLabel("Loading history…")
        self.loading_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.loading_label.setAccessibleName("Loading history")
        self.list_stack.addWidget(self.loading_label)

        error_page = QWidget()
        error_layout = QVBoxLayout(error_page)
        error_layout.addStretch(1)
        self.error_label = QLabel("History is unavailable right now.")
        self.error_label.setWordWrap(True)
        self.error_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_label.setAccessibleName("History error message")
        error_layout.addWidget(self.error_label)
        self.retry_button = QPushButton("Try again")
        self.retry_button.setAccessibleName("Try loading history again")
        self.retry_button.clicked.connect(self._on_retry)
        error_layout.addWidget(self.retry_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        error_layout.addStretch(1)
        self.list_stack.addWidget(error_page)

        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.addStretch(1)
        self.empty_label = QLabel(
            "Your clipboard history is empty.\n\n"
            "1. Copy anything as usual with Ctrl+C.\n"
            "2. ClipCascade keeps a private, encrypted copy on this PC.\n"
            "3. Open History any time from the tray icon."
        )
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setAccessibleName("History is empty guidance")
        empty_layout.addWidget(self.empty_label)
        empty_layout.addStretch(1)
        self.list_stack.addWidget(empty_page)

        search_empty_page = QWidget()
        search_empty_layout = QVBoxLayout(search_empty_page)
        search_empty_layout.addStretch(1)
        self.search_empty_label = QLabel("")
        self.search_empty_label.setWordWrap(True)
        self.search_empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.search_empty_label.setAccessibleName("No search results message")
        search_empty_layout.addWidget(self.search_empty_label)
        self.clear_search_button = QPushButton("Clear search")
        self.clear_search_button.setAccessibleName("Clear search")
        self.clear_search_button.clicked.connect(self.search_box.clear)
        search_empty_layout.addWidget(self.clear_search_button, alignment=Qt.AlignmentFlag.AlignHCenter)
        search_empty_layout.addStretch(1)
        self.list_stack.addWidget(search_empty_page)

        layout.addWidget(self.list_stack)
        return pane

    def _build_detail_pane(self):
        pane = QWidget()
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, theme.SPACING_M, theme.SPACING_L, theme.SPACING_M)
        self.detail_pane = DetailPane()
        self.detail_pane.action_requested.connect(self._on_action_requested)
        layout.addWidget(self.detail_pane)
        return pane

    def _build_footer(self):
        footer = QFrame()
        footer.setObjectName("footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(theme.SPACING_L, theme.SPACING_XS, theme.SPACING_L, theme.SPACING_XS)
        self.status_label = QLabel("Loading history…")
        self.status_label.setObjectName("mutedLabel")
        self.status_label.setAccessibleName("History summary")
        footer_layout.addWidget(self.status_label, stretch=1)
        self.live_region = QLabel("")
        self.live_region.setAccessibleName("History announcements")
        self.live_region.setFixedHeight(0)
        footer_layout.addWidget(self.live_region)
        return footer

    def _build_shortcuts(self):
        search_shortcut = QShortcut(QKeySequence("Ctrl+F"), self)
        search_shortcut.activated.connect(self._focus_search)
        context_shortcut = QShortcut(QKeySequence("Shift+F10"), self.list_view)
        context_shortcut.activated.connect(self._show_context_menu)
        clear_shortcut = QShortcut(QKeySequence("Escape"), self.search_box)
        clear_shortcut.activated.connect(self.search_box.clear)

    # --- controller wiring ------------------------------------------------------

    def _wire_controller(self):
        self.controller.rows_changed.connect(self._on_rows_changed)
        self.controller.state_changed.connect(self._on_state_changed)
        self.controller.status_changed.connect(self._on_status_changed)
        self.controller.announced.connect(self.live_region.setText)
        self.controller.detail_loading.connect(lambda _entry_id: self.detail_pane.show_loading())
        self.controller.detail_loaded.connect(self._on_detail_loaded)
        self.controller.detail_failed.connect(lambda _entry_id, message: self.detail_pane.show_error(message))
        self.controller.action_completed.connect(self._on_action_completed)
        self.controller.action_failed.connect(self._on_action_failed)

    def attach_controller(self, controller):
        """Late binding used by `history_ui.main` when the IPC client only
        becomes available after the window exists."""
        self.controller = controller
        self._wire_controller()

    @Slot(str)
    def handle_ipc_event(self, name, data, gap):
        if self.controller is not None:
            self.controller.handle_event(name, data, gap)

    # --- slots -------------------------------------------------------------------

    @Slot()
    def handle_focus_request(self):
        self.focus_requests += 1
        self.present()

    def present(self):
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
        self._focus_list_on_first_present()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._painted:
            return
        self._painted = True
        if self._on_first_paint is not None:
            self._on_first_paint()

    def closeEvent(self, event):
        self._settings.setValue(GEOMETRY_KEY, self.saveGeometry())
        super().closeEvent(event)
        event.accept()

    def _focus_search(self):
        self.search_box.setFocus()
        self.search_box.selectAll()

    def _focus_list_on_first_present(self):
        if self._restored_selection_done or self.controller is None:
            return
        if self.model.rowCount() > 0:
            self._restored_selection_done = True
            self.list_view.setFocus()
            self.list_view.setCurrentIndex(self.model.index(0, 0))

    def _on_search_text_changed(self, text):
        if self.controller is not None:
            self.controller.set_search_text(text)

    def _on_type_filter(self, value):
        if self.controller is not None:
            self.controller.set_type_filter(value)

    def _on_source_filter(self, value):
        if self.controller is not None:
            self.controller.set_source_filter(value)

    def _on_order_toggled(self, checked):
        self.order_button.setText("Oldest first" if checked else "Newest first")
        self.order_button.setAccessibleName(
            "Sort order: oldest first; activate for newest first"
            if checked
            else "Sort order: newest first; activate for oldest first"
        )
        if self.controller is not None:
            self.controller.set_order(checked)

    def _on_retry(self):
        if self.controller is not None:
            self._restored_selection_done = False
            self.controller.start()

    def _on_rows_changed(self, rows):
        previous_id = self.controller.selected_id if self.controller is not None else None
        self.model.set_rows(rows)
        self._update_filter_counts()
        if previous_id is not None:
            row = self.model.row_of_entry_id(previous_id)
            if row is not None:
                self.list_view.setCurrentIndex(self.model.index(row, 0))
                return
        if self.model.rowCount() > 0:
            self._focus_list_on_first_present()

    def _on_state_changed(self, state):
        pages = {
            "ready": 0,
            "loading": 1,
            "error": 2,
            "empty": 3,
            "search_empty": 4,
        }
        self.list_stack.setCurrentIndex(pages.get(state, 0))
        if state == "error" and self.controller is not None and self.controller.failure:
            self.error_label.setText(
                f"History is unavailable right now.\n\n{self.controller.failure}"
            )
        if state == "search_empty":
            self.search_empty_label.setText(
                f"No history matches “{self.controller.search_text().strip()}”"
                if self.controller is not None
                else "No history matches your search"
            )

    def _on_status_changed(self, text):
        self.status_label.setText(text)

    def _on_current_changed(self, current, _previous):
        if self.controller is None or not current.isValid():
            return
        self.controller.select_entry(current.data(ENTRY_ID_ROLE))

    def _on_scroll_moved(self, value):
        if self.controller is None:
            return
        bar = self.list_view.verticalScrollBar()
        if bar.maximum() > 0 and value >= bar.maximum() - 2:
            self.controller.request_more()

    def _update_filter_counts(self):
        if self.controller is None:
            return
        counts = self.controller.type_counts()
        for value, label in TYPE_FILTERS:
            button = self._type_buttons[value]
            button.setText(f"{label} ({counts.get(value, 0)})")
            button.setAccessibleName(f"Filter: {label}, {counts.get(value, 0)} entries")
        for value, label in SOURCE_FILTERS:
            button = self._source_buttons[value]
            button.setText(f"{label} ({counts.get(value, 0)})")
            button.setAccessibleName(f"Source filter: {label}, {counts.get(value, 0)} entries")

    # --- actions (T6): dispatch, dialogs, context menu --------------------------

    def _on_detail_loaded(self, detail):
        self._current_detail = detail
        self.detail_pane.show_detail(detail)

    def _selected_entry_id(self):
        index = self.list_view.currentIndex()
        return index.data(ENTRY_ID_ROLE) if index.isValid() else None

    @Slot(str, object)
    def _on_action_requested(self, action, extra):
        entry_id = self._selected_entry_id()
        if entry_id is None or self.controller is None:
            return
        extra = extra or {}

        if action == "copy_again":
            self.controller.execute_action("copy_again", {"entry_id": entry_id})
        elif action == "pin":
            self.controller.execute_action(
                "execute_command", {"command": "pin_entry", "entry_id": entry_id}
            )
        elif action == "unpin":
            self.controller.execute_action(
                "execute_command", {"command": "unpin_entry", "entry_id": entry_id}
            )
        elif action == "delete":
            if self._confirm_delete():
                self.controller.execute_action(
                    "execute_command", {"command": "delete_entry", "entry_id": entry_id}
                )
        elif action == "open_link":
            self._open_link(extra.get("url") or self._current_link_url())
        elif action == "save_image":
            self._save_image_as(entry_id)
        elif action == "download_all":
            self._download_files(entry_id, None)
        elif action == "download_one":
            self._download_files(entry_id, [extra.get("filename")])
        elif action == "open_folder":
            self.controller.execute_action("open_folder", {"entry_id": entry_id})
        elif action == "copy_file_paths":
            self.controller.execute_action("copy_file_paths", {"entry_id": entry_id})
        else:
            self._announce(f"Unsupported action: {action}")

    def _current_link_url(self):
        detail = self._current_detail or {}
        if detail.get("payload_type") == "link":
            return detail.get("url") or detail.get("preview", "")
        return ""

    def _current_model_row(self):
        index = self.list_view.currentIndex()
        if not index.isValid():
            return None
        return {
            "entry_id": index.data(ENTRY_ID_ROLE),
            "preview": index.data(PREVIEW_ROLE) or "",
            "state_label": index.data(STATE_LABEL_ROLE),
        }

    def _confirm_delete(self):
        box = QMessageBox(self)
        box.setWindowTitle("Delete entry")
        box.setText("Delete this entry from history?")
        box.setInformativeText("Files you already downloaded are not deleted.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _open_link(self, raw_url):
        check = link_policy.validate_link(raw_url)
        if not check.ok:
            self._announce("This entry is not a valid web link, so it was not opened.")
            return
        if not link_policy.is_trusted(self._settings, check.hostname):
            dialog = LinkConfirmDialog(check, self)
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            if not accepted:
                self._announce("Link opening cancelled.")
                return
            if dialog.trust_requested() and check.is_secure:
                link_policy.set_trusted(self._settings, check.hostname)
        QDesktopServices.openUrl(QUrl(check.url))

    def _save_image_as(self, entry_id):
        target, _filter = QFileDialog.getSaveFileName(
            self, "Save image", os.path.join(os.path.expanduser("~"), "Pictures", "clipboard.png")
        )
        if not target:
            self._announce("Image save cancelled.")
            return
        self.controller.execute_action(
            "save_image", {"entry_id": entry_id, "target_path": target}
        )

    def _download_files(self, entry_id, filenames):
        directory = QFileDialog.getExistingDirectory(self, "Download files to folder")
        if not directory:
            self._announce("Download cancelled.")
            return
        self.controller.execute_action(
            "download_files",
            {
                "entry_id": entry_id,
                "target_directory": directory,
                "filenames": filenames,
            },
        )

    def _on_manage_retention(self):
        if self.controller is None:
            return
        self._pending_retention_dialog = True
        self.controller.execute_action("preview_retention", {"policy": None})

    def _on_clear_history(self):
        if self.controller is None:
            return
        dialog = ClearHistoryDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._announce("Clear cancelled.")
            return
        command = "clear_all" if dialog.clear_all_selected() else "clear_unpinned"
        self.controller.execute_action("execute_command", {"command": command})
        if dialog.clear_clipboard_selected():
            self.controller.execute_action("clear_windows_clipboard", {})

    def _on_action_completed(self, action, result):
        result = result if isinstance(result, dict) else {}
        if action == "copy_again":
            self._announce("Copied to the clipboard.")
        elif action == "execute_command":
            self._announce("Done.")
        elif action == "download_files":
            failed = [item["name"] for item in result.get("files", []) if not item.get("ok")]
            saved = result.get("downloaded_directory", "")
            if failed:
                self._announce(
                    f"Downloaded with problems: {', '.join(failed)} could not be written. Others saved to {saved}."
                )
            else:
                self._announce(f"Files downloaded to {saved}.")
        elif action == "save_image":
            self._announce(f"Image saved to {result.get('path', '')}.")
        elif action == "open_folder":
            self._announce("Opened the downloaded files folder.")
        elif action == "copy_file_paths":
            self._announce("File paths copied to the clipboard.")
        elif action == "clear_windows_clipboard":
            self._announce("Windows clipboard cleared.")
        elif action == "preview_retention":
            self._show_retention_dialog(result)
        elif action == "apply_retention":
            self._announce(
                "Retention applied: "
                f"{result.get('entries_removed', 0)} entries removed, "
                f"{result.get('transfer_entries_expired', 0)} file batches expired."
            )

    def _on_action_failed(self, action, message):
        friendly = {
            "not-found": "The entry no longer exists.",
            "not-supported-for-files": "File batches are downloaded, not re-copied.",
            "payload-unavailable": "The content is no longer available.",
            "clipboard-unavailable": "The clipboard is not reachable right now.",
            "invalid-state:expired": "This file batch has expired and can no longer be downloaded.",
            "invalid-state:unavailable": "This file batch is unavailable.",
            "directory-missing": "The saved folder no longer exists.",
            "never-downloaded": "These files have not been downloaded yet.",
            "unsafe-filename": "A file name was rejected as unsafe.",
            "actions-unavailable": "Actions are unavailable in this session.",
            "unsupported-platform": "This action is only available on Windows.",
        }
        text = friendly.get(message, f"{message}")
        self._announce(f"{action} failed: {text}")

    def _show_retention_dialog(self, impact):
        if not self._pending_retention_dialog:
            return
        self._pending_retention_dialog = False
        dialog = RetentionDialog(impact if impact else {}, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.controller.execute_action("apply_retention", {"policy": None})

    def _announce(self, text):
        self._last_announcement = text
        self.live_region.setText(text)

    def build_context_menu(self):
        """Actions for the entry under the cursor, mirroring the detail pane."""
        menu = QMenu(self)
        menu.setObjectName("historyContextMenu")
        row = self._current_model_row()
        if row is None or self.controller is None:
            placeholder = menu.addAction("No entry selected")
            placeholder.setEnabled(False)
            return menu

        preview = row["preview"].strip()
        looks_like_link = bool(link_policy.validate_link(preview).ok) if preview else False
        if looks_like_link:
            menu.addAction("Copy link", lambda: self._on_action_requested("copy_again", None))
            menu.addAction(
                "Open in browser…", lambda: self._on_action_requested("open_link", {"url": preview})
            )
        else:
            menu.addAction("Copy again", lambda: self._on_action_requested("copy_again", None))
        if row["state_label"]:
            menu.addAction("Open folder", lambda: self._on_action_requested("open_folder", None))
        menu.addSeparator()
        menu.addAction("Delete", lambda: self._on_action_requested("delete", None))
        return menu

    def _show_context_menu(self):
        menu = self.build_context_menu()
        rect = self.list_view.visualRect(self.list_view.currentIndex())
        menu.exec(self.list_view.viewport().mapToGlobal(rect.center()))

    def contextMenuEvent(self, event):
        menu = self.build_context_menu()
        menu.exec(event.globalPos())

    # --- geometry ---------------------------------------------------------------

    def _restore_and_clamp_geometry(self):
        saved = self._settings.value(GEOMETRY_KEY)
        if isinstance(saved, (bytes, bytearray)) and saved:
            self.restoreGeometry(bytes(saved))
        clamped = self.clamp_geometry(
            self.frameGeometry(),
            self._available_screen_rects(),
            DEFAULT_SIZE,
        )
        self.setGeometry(clamped)

    @staticmethod
    def _available_screen_rects():
        screens = QGuiApplication.screens()
        rects = [screen.availableGeometry() for screen in screens]
        return rects or [QRect(0, 0, DEFAULT_SIZE[0], DEFAULT_SIZE[1])]

    @staticmethod
    def clamp_geometry(restored, available_rects, default_size):
        """Never restore off-screen: pick the screen the window overlaps most
        (or the first one), pull the frame fully inside it, and bound the
        size to that screen's usable area."""
        if not available_rects:
            return QRect(restored)
        target = available_rects[0]
        best_overlap = -1
        for rect in available_rects:
            overlap = rect.intersected(restored).width() * rect.intersected(restored).height()
            if overlap > best_overlap:
                best_overlap = overlap
                target = rect
        if best_overlap <= 0:
            width, height = default_size
            return QRect(
                target.left() + max(0, (target.width() - width) // 2),
                target.top() + max(0, (target.height() - height) // 2),
                min(width, target.width()),
                min(height, target.height()),
            )
        width = max(1, min(restored.width(), target.width()))
        height = max(1, min(restored.height(), target.height()))
        x = min(max(restored.left(), target.left()), target.right() - width + 1)
        y = min(max(restored.top(), target.top()), target.bottom() - height + 1)
        return QRect(x, y, width, height)


class LinkConfirmDialog(QDialog):
    """Per-hostname confirmation before a link is opened. Trust is only
    offerable (and only grantable) for HTTPS."""

    def __init__(self, check, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open link in browser?")
        self.setModal(True)
        layout = QVBoxLayout(self)

        warning = QLabel("Open this link in your default browser?")
        warning.setAccessibleName("Link confirmation question")
        layout.addWidget(warning)

        scheme_label = QLabel(
            "Not encrypted (HTTP)" if not check.is_secure else "Encrypted (HTTPS)"
        )
        scheme_label.setObjectName("warningChip" if not check.is_secure else "secureChip")
        layout.addWidget(scheme_label)

        host = QLabel(f"Hostname: {check.hostname}")
        host.setTextFormat(Qt.TextFormat.PlainText)
        host.setAccessibleName("Link hostname")
        layout.addWidget(host)

        url_label = QLabel(check.url)
        url_label.setTextFormat(Qt.TextFormat.PlainText)
        url_label.setWordWrap(True)
        url_label.setAccessibleName("Full link address")
        layout.addWidget(url_label)

        self._trust_checkbox = QCheckBox("Do not ask again for this hostname")
        # The design allows silent re-opening for trusted HTTPS hostnames
        # only; HTTP always warns.
        self._trust_checkbox.setEnabled(check.is_secure)
        self._trust_checkbox.setAccessibleName(
            "Do not ask again for this hostname, available for HTTPS only"
        )
        layout.addWidget(self._trust_checkbox)

        buttons = QHBoxLayout()
        open_button = QPushButton("Open")
        open_button.setDefault(True)
        open_button.clicked.connect(self.accept)
        cancel_button = QPushButton("Cancel")
        cancel_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(open_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

    def trust_requested(self):
        return self._trust_checkbox.isChecked()


class ClearHistoryDialog(QDialog):
    """Destructive clear flow: scope choice (unpinned default) plus the
    opt-in current-Windows-clipboard clear (off by default)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Clear history")
        self.setModal(True)
        layout = QVBoxLayout(self)

        info = QLabel("Remove entries from the encrypted history on this PC.")
        info.setAccessibleName("Clear history explanation")
        layout.addWidget(info)

        self._unpinned_radio = QListWidget()
        self._unpinned_radio.setAccessibleName("Clear scope")
        unpinned_item = QListWidgetItem("Clear unpinned entries (pinned are kept)")
        all_item = QListWidgetItem("Clear ALL entries (including pinned)")
        unpinned_item.setData(Qt.ItemDataRole.UserRole, "unpinned")
        all_item.setData(Qt.ItemDataRole.UserRole, "all")
        self._unpinned_radio.addItem(unpinned_item)
        self._unpinned_radio.addItem(all_item)
        self._unpinned_radio.setCurrentRow(0)
        layout.addWidget(self._unpinned_radio)

        self._clipboard_checkbox = QCheckBox("Also clear the current Windows clipboard")
        self._clipboard_checkbox.setChecked(False)
        self._clipboard_checkbox.setAccessibleName(
            "Also clear the current Windows clipboard, off by default"
        )
        layout.addWidget(self._clipboard_checkbox)

        note = QLabel("Files you already downloaded are never deleted.")
        note.setObjectName("mutedLabel")
        layout.addWidget(note)

        buttons = QHBoxLayout()
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.accept)
        cancel_button = QPushButton("Cancel")
        cancel_button.setDefault(True)
        cancel_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(clear_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)

    def clear_all_selected(self):
        item = self._unpinned_radio.currentItem()
        return bool(item and item.data(Qt.ItemDataRole.UserRole) == "all")

    def clear_clipboard_selected(self):
        return self._clipboard_checkbox.isChecked()


class RetentionDialog(QDialog):
    """Retention impact preview, then apply-on-confirm. The numbers come
    from the main process's preview; apply uses the same policy server-side
    so preview and applied result are computed by the same engine."""

    def __init__(self, impact, parent=None):
        super().__init__(parent)
        self.setWindowTitle("History retention")
        self.setModal(True)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Applying retention now removes old unpinned entries and expires\n"
            "stale file transfers. Pinned entries are always kept."
        )
        intro.setAccessibleName("Retention explanation")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        entries_removed = impact.get("entries_to_remove", 0)
        bytes_released = impact.get("bytes_to_release", 0)
        transfers_expired = impact.get("transfer_entries_to_expire", 0)
        facts = QLabel(
            f"Entries to remove: {entries_removed}\n"
            f"Storage released: {bytes_released} bytes\n"
            f"File batches to expire: {transfers_expired}"
        )
        facts.setAccessibleName("Retention impact numbers")
        layout.addWidget(facts)

        buttons = QHBoxLayout()
        apply_button = QPushButton("Apply now")
        apply_button.clicked.connect(self.accept)
        cancel_button = QPushButton("Cancel")
        cancel_button.setDefault(True)
        cancel_button.clicked.connect(self.reject)
        buttons.addStretch(1)
        buttons.addWidget(apply_button)
        buttons.addWidget(cancel_button)
        layout.addLayout(buttons)
