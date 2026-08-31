"""T5 model tests: virtualised read-only list model over IPC summary dicts.

Runs under QT_QPA_PLATFORM=offscreen via pytest-qt; the model layer never
sees SQLite, keys or payloads -- only the summary dicts the IPC server sent.
"""

import os
import time
from datetime import datetime, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QListView  # noqa: E402

from history_ui.entry_model import (  # noqa: E402
    DIMENSIONS_ROLE,
    ENTRY_ID_ROLE,
    FILE_COUNT_ROLE,
    INSECURE_LINK_ROLE,
    IS_LINK_ROLE,
    PINNED_ROLE,
    PREVIEW_ROLE,
    SOURCE_ROLE,
    STATE_LABEL_ROLE,
    TIME_ROLE,
    TYPE_ROLE,
    HistoryItemDelegate,
    HistoryListModel,
    insecure_link,
)


def summary(index, payload_type="text", direction="local", pinned=False, **overrides):
    moment = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
    row = {
        "id": f"entry-{index}",
        "created_at_utc": moment.isoformat(),
        "updated_at_utc": moment.isoformat(),
        "payload_type": payload_type,
        "direction": direction,
        "transport": "local",
        "pinned": pinned,
        "byte_size": 100 + index,
        "preview": f"preview text {index}",
        "source_device_name": None,
        "file_state": None,
        "file_count": None,
        "image_width": None,
        "image_height": None,
    }
    row.update(overrides)
    return row


@pytest.fixture
def model():
    return HistoryListModel()


def test_roles_expose_ipc_summary_fields(model):
    model.set_rows(
        [
            summary(
                1,
                payload_type="files",
                pinned=True,
                file_count=3,
                file_state="ready",
                source_device_name="Office PC",
            )
        ]
    )
    index = model.index(0, 0)
    assert model.rowCount() == 1
    assert index.data(TYPE_ROLE) == "FILES"
    assert index.data(PREVIEW_ROLE) == "preview text 1"
    assert index.data(SOURCE_ROLE) == "Office PC"
    assert index.data(PINNED_ROLE) is True
    assert index.data(FILE_COUNT_ROLE) == 3
    assert index.data(STATE_LABEL_ROLE) == "Ready to download"
    assert index.data(ENTRY_ID_ROLE) == "entry-1"
    assert index.data(TIME_ROLE)  # local-time HH:MM derived from the ISO stamp


def test_display_role_is_plain_preview_text_never_html(model):
    model.set_rows([summary(1, preview="<script>alert(1)</script> & <b>bold</b>")])
    text = model.index(0, 0).data(Qt.ItemDataRole.DisplayRole)
    assert text == "<script>alert(1)</script> & <b>bold</b>"


def test_source_falls_back_to_direction_labels(model):
    model.set_rows([summary(1, direction="local"), summary(2, direction="remote")])
    assert model.index(0, 0).data(SOURCE_ROLE) == "This PC"
    assert model.index(1, 0).data(SOURCE_ROLE) == "Remote device"


def test_link_roles_and_insecure_detection(model):
    model.set_rows(
        [
            summary(1, payload_type="link", preview="https://example.com/a"),
            summary(2, payload_type="link", preview="http://insecure.example/x"),
            summary(3, payload_type="text", preview="see https://mixed.example"),
        ]
    )
    assert model.index(0, 0).data(IS_LINK_ROLE) is True
    assert model.index(0, 0).data(INSECURE_LINK_ROLE) is False
    assert model.index(1, 0).data(INSECURE_LINK_ROLE) is True
    assert model.index(2, 0).data(IS_LINK_ROLE) is False
    assert insecure_link("  HTTP://UPPER.CASE ") is True
    assert insecure_link("https://secure") is False


def test_image_rows_show_dimensions(model):
    model.set_rows([summary(1, payload_type="image", image_width=1440, image_height=900)])
    index = model.index(0, 0)
    assert index.data(DIMENSIONS_ROLE) == "1440 × 900"
    assert "1440 × 900" in index.data(Qt.ItemDataRole.DisplayRole)


def test_row_lookup_helpers(model):
    rows = [summary(i) for i in range(5)]
    model.set_rows(rows)
    assert model.entry_id_at(0) == "entry-0"
    assert model.entry_id_at(4) == "entry-4"
    assert model.entry_id_at(99) is None
    assert model.row_of_entry_id("entry-3") == 3
    assert model.row_of_entry_id("missing") is None
    model.set_rows([])
    assert model.rowCount() == 0


def test_thousand_entry_list_stays_virtualised_and_navigable(qtbot):
    """QListView + QAbstractListModel is structurally virtualised: the model
    holds all 1,000 rows but the view never materialises per-index widgets,
    and keyboard navigation walks the full range."""
    model = HistoryListModel()
    view = QListView()
    view.setModel(model)
    view.setItemDelegate(HistoryItemDelegate(view))
    view.setUniformItemSizes(True)
    qtbot.addWidget(view)
    view.resize(390, 600)

    started = time.perf_counter()
    model.set_rows([summary(i) for i in range(1000)])
    view.show()
    if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
        # The offscreen QPA has no windowing backend: waiting for an
        # exposure event access-violates natively. Virtualisation and
        # keyboard navigation are fully testable without a mapped window.
        QTest.qWaitForWindowExposed(view)
    elapsed = time.perf_counter() - started

    assert model.rowCount() == 1000
    assert elapsed < 1.0, f"populating 1,000 virtualised rows took {elapsed:.3f}s"

    for row_number in range(1000):
        assert model.flags(model.index(row_number, 0)) & Qt.ItemFlag.ItemNeverHasChildren

    view.setCurrentIndex(model.index(0, 0))
    QTest.keyClick(view, Qt.Key.Key_PageDown)
    assert view.currentIndex().row() > 0
    QTest.keyClick(view, Qt.Key.Key_End)
    assert view.currentIndex().row() == 999
    QTest.keyClick(view, Qt.Key.Key_Home)
    assert view.currentIndex().row() == 0
    QTest.keyClick(view, Qt.Key.Key_Down)
    assert view.currentIndex().row() == 1


def test_delegate_paint_executes_for_every_entry_type(qtbot):
    """view.grab() forces real delegate paints. Paint-only API misuse
    (wrong flag scopes, missing QFontMetrics methods) never surfaces in
    model-level tests — this test fails the first time rendering breaks."""
    model = HistoryListModel()
    view = QListView()
    view.setModel(model)
    view.setItemDelegate(HistoryItemDelegate(view))
    qtbot.addWidget(view)
    view.resize(390, 600)
    model.set_rows(
        [
            summary(1, payload_type="text"),
            summary(2, payload_type="link", preview="https://example.com/a"),
            summary(3, payload_type="link", preview="http://insecure.example/x"),
            summary(4, payload_type="image", image_width=1440, image_height=900),
            summary(5, payload_type="files", file_count=3, file_state="ready", pinned=True),
            summary(
                6,
                payload_type="files",
                file_state="expired",
                source_device_name="A very long device name that must elide cleanly",
            ),
        ]
    )
    view.show()
    pixmap = view.grab()
    assert not pixmap.isNull()
