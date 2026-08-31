"""T2: manager-level tests for non-blocking history capture integration.

Covers every payload type x direction x failure path against a fake
`HistorySink`, without touching Windows DPAPI/SQLite (that's T1's own test
suite). Also proves capture never mutates `previous_clipboard_hash` and
never adds meaningful synchronous latency to the send/receive path.
"""

import logging
import threading
import time

from PIL import Image

from clipboard.clipboard_manager import ClipboardManager, NoOpHistorySink
from history import service as history_service


class FakeHistorySink:
    def __init__(self, raise_on_record=False):
        self.events = []
        self.raise_on_record = raise_on_record

    def record(self, event):
        if self.raise_on_record:
            raise RuntimeError("simulated storage failure")
        self.events.append(event)


def _make_manager(tmp_config, history_sink=None, history_transport="local"):
    return ClipboardManager(
        tmp_config, history_sink=history_sink, history_transport=history_transport
    )


def _small_image():
    return Image.new("RGB", (2, 2), color=(10, 20, 30))


# --- local outbound capture -------------------------------------------------


def test_local_text_capture_creates_one_local_entry_and_still_sends_once(tmp_config):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink, "p2s")
    calls = []
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), "hello world", "text")

    assert len(calls) == 1
    assert calls[0] == ("hello world", "text")
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.direction == "local"
    assert event.payload_type == "text"
    assert event.payload == "hello world"
    assert event.transport == "local"


def test_copy_again_suppression_swallows_one_send_then_resumes(tmp_config):
    """The T6 copy-again contract at the real manager seam: with the
    suppression armed (flag + token), the monitor-triggered local path is
    swallowed exactly once — no resend callback, no history record — and
    normal operation resumes afterwards."""
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink, "p2s")

    manager.suppress_next_local_send = True
    manager.history_origin_suppress_token = "history-copy-again:entry-1"
    manager.previous_clipboard_hash = ClipboardManager.hash_clipboard("retry this")

    sent = []
    manager.clipboard_to_base64(lambda c, t: sent.append((c, t)), "retry this", "text")
    assert sent == []
    assert len(sink.events) == 0
    assert manager.suppress_next_local_send is False
    assert manager.history_origin_suppress_token is None

    manager.clipboard_to_base64(lambda c, t: sent.append((c, t)), "a fresh copy", "text")
    assert sent == [("a fresh copy", "text")]
    assert len(sink.events) == 1


def test_local_image_capture_records_raw_bytes_not_base64(tmp_config):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink)
    img = _small_image()
    calls = []
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), img, "image")

    assert len(calls) == 1
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.direction == "local"
    assert event.payload_type == "image"
    assert isinstance(event.payload, bytes)
    assert event.payload != calls[0][0]  # raw bytes, not the base64 string sent


def test_local_files_capture_records_filename_to_bytes_map(tmp_config, tmp_path):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink)
    file_path = tmp_path / "report.txt"
    file_path.write_bytes(b"file payload bytes")
    calls = []
    manager.clipboard_to_base64(
        lambda c, t: calls.append((c, t)), [str(file_path)], "files"
    )

    assert len(calls) == 1
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.direction == "local"
    assert event.payload_type == "files"
    assert event.payload == {"report.txt": b"file payload bytes"}


def test_local_empty_file_batch_creates_no_history_and_no_send(tmp_config):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink)
    calls = []
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), [], "files")

    assert calls == []
    assert sink.events == []


def test_oversized_local_text_creates_no_history_and_no_send(tmp_config):
    tmp_config.data["max_clipboard_size_local_limit_bytes"] = 4
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink)
    calls = []
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), "way too long", "text")

    assert calls == []
    assert sink.events == []


# --- remote inbound capture -------------------------------------------------


def test_remote_text_capture_creates_one_remote_entry(tmp_config):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink, "p2s")
    manager.base64_to_clipboard("hello from remote", "text")

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.direction == "remote"
    assert event.payload_type == "text"
    assert event.payload == "hello from remote"
    assert event.transport == "p2s"


def test_remote_image_capture_records_raw_bytes(tmp_config):
    import base64

    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink, "p2p")
    img_bytes = ClipboardManager.convert_image_to_base64(img=_small_image())
    manager.base64_to_clipboard(img_bytes, "image")

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.direction == "remote"
    assert event.payload_type == "image"
    assert event.payload == base64.b64decode(img_bytes)
    assert event.transport == "p2p"


def test_remote_files_capture_recorded_after_safe_name_validation(tmp_config):
    import base64
    import json

    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink, "p2s")
    payload_json = json.dumps(
        {"report.txt": base64.b64encode(b"safe file bytes").decode("utf-8")}
    )
    manager.base64_to_clipboard(payload_json, "files")

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.direction == "remote"
    assert event.payload_type == "files"
    assert event.payload == {"report.txt": b"safe file bytes"}


def test_remote_files_unsafe_name_creates_no_history_but_paste_still_succeeds(tmp_config):
    import base64
    import json

    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink, "p2s")

    class FakeTray:
        def __init__(self):
            self.enabled_with = None

        def enable_files_download(self, files):
            self.enabled_with = files

        def disable_files_download(self):
            pass

    tray = FakeTray()
    manager.set_tray_ref(tray)

    payload_json = json.dumps(
        {"../escape.txt": base64.b64encode(b"unsafe").decode("utf-8")}
    )
    manager.base64_to_clipboard(payload_json, "files")

    # Existing paste()/download-enable behaviour is unaffected by the
    # unsafe name: it's only the history record that gets dropped.
    assert tray.enabled_with is not None
    assert sink.events == []


def test_unsafe_filename_capture_never_logs_the_filename(tmp_config, caplog):
    import base64
    import json
    import traceback

    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink, "p2s")

    class FakeTray:
        def __init__(self):
            self.enabled_with = None

        def enable_files_download(self, files):
            self.enabled_with = files

        def disable_files_download(self):
            pass

    tray = FakeTray()
    manager.set_tray_ref(tray)

    unsafe_name = "../escape.txt"
    payload_json = json.dumps(
        {unsafe_name: base64.b64encode(b"unsafe").decode("utf-8")}
    )
    with caplog.at_level(logging.WARNING):
        manager.base64_to_clipboard(payload_json, "files")

    # Paste/download-enable behaviour is unaffected; only the history
    # capture is dropped.
    assert tray.enabled_with is not None
    assert sink.events == []

    # The unsafe filename must never reach the logs — neither through a
    # log message nor through an exception/traceback attached to a record.
    for record in caplog.records:
        assert unsafe_name not in record.getMessage()
        assert record.exc_text is None
        if record.exc_info is not None:
            formatted = "".join(traceback.format_exception(*record.exc_info))
            assert unsafe_name not in formatted


def test_malformed_remote_base64_creates_no_history(tmp_config, caplog):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink)
    with caplog.at_level(logging.ERROR):
        manager.base64_to_clipboard("not-valid-base64!!!", "image")

    assert sink.events == []


# --- echo-loop / previous_clipboard_hash safety -----------------------------


def test_capture_never_mutates_previous_clipboard_hash(tmp_config):
    sink = FakeHistorySink()
    with_history = _make_manager(tmp_config, sink, "p2s")
    without_history = _make_manager(tmp_config, None, "p2s")

    for manager in (with_history, without_history):
        manager.clipboard_to_base64(lambda c, t: None, "same content", "text")
        manager.base64_to_clipboard("remote content", "text")

    assert with_history.previous_clipboard_hash == without_history.previous_clipboard_hash
    assert with_history.previous_clipboard_hash == 0  # capture alone never sets it


def test_paste_echo_does_not_create_spurious_local_entry(tmp_config):
    """Simulates a remote receive (which sets previous_clipboard_hash via
    has_clipboard_changed, as the real transport does) followed by the local
    monitor retriggering with the identical content because paste() wrote it
    to the OS clipboard. The retrigger must still invoke the send callback
    (unchanged sync behaviour) but must not be recorded as a new local entry
    alongside the correctly-recorded remote one."""
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink, "p2s")

    remote_text = "shared content"
    assert manager.has_clipboard_changed(remote_text) is True
    manager.base64_to_clipboard(remote_text, "text")
    assert len(sink.events) == 1
    assert sink.events[0].direction == "remote"

    calls = []
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), remote_text, "text")

    assert calls == [(remote_text, "text")]  # callback still runs unchanged
    assert len(sink.events) == 1  # no spurious second (local) entry


def test_genuine_local_copy_after_remote_receive_is_still_captured(tmp_config):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink, "p2s")

    manager.has_clipboard_changed("remote content")
    manager.base64_to_clipboard("remote content", "text")
    assert len(sink.events) == 1

    calls = []
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), "different local copy", "text")

    assert calls == [("different local copy", "text")]
    assert len(sink.events) == 2
    assert sink.events[1].direction == "local"
    assert sink.events[1].payload == "different local copy"


# --- "Copy again" origin-suppression token stub -----------------------------


def test_history_origin_suppress_token_is_consumed_once(tmp_config):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink)

    manager.history_origin_suppress_token = "copy-again-marker"
    calls = []
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), "re-copied text", "text")

    assert calls == [("re-copied text", "text")]  # send path unaffected
    assert sink.events == []  # suppressed once
    assert manager.history_origin_suppress_token is None  # consumed

    # A subsequent, unrelated local copy is captured normally.
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), "next text", "text")
    assert len(sink.events) == 1
    assert sink.events[0].payload == "next text"


# --- deduplication -----------------------------------------------------------


def test_repeated_local_capture_within_window_coalesces(tmp_config, monkeypatch):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink)

    fake_now = [1000.0]
    monkeypatch.setattr(
        "clipboard.clipboard_manager.time.monotonic", lambda: fake_now[0]
    )

    calls = []
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), "dup text", "text")
    fake_now[0] += 1.0  # still inside the 2s window
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), "dup text", "text")

    assert len(calls) == 2  # send path runs both times, unaffected
    assert len(sink.events) == 1  # second capture coalesced


def test_repeated_local_capture_outside_window_creates_second_entry(tmp_config, monkeypatch):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink)

    fake_now = [1000.0]
    monkeypatch.setattr(
        "clipboard.clipboard_manager.time.monotonic", lambda: fake_now[0]
    )

    manager.clipboard_to_base64(lambda c, t: None, "dup text", "text")
    fake_now[0] += 5.0  # past the 2s window
    manager.clipboard_to_base64(lambda c, t: None, "dup text", "text")

    assert len(sink.events) == 2


def test_dedup_does_not_merge_local_and_remote_direction(tmp_config, monkeypatch):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink, "p2s")

    fake_now = [2000.0]
    monkeypatch.setattr(
        "clipboard.clipboard_manager.time.monotonic", lambda: fake_now[0]
    )

    manager.base64_to_clipboard("same text", "text")  # remote
    fake_now[0] += 0.5
    manager.clipboard_to_base64(lambda c, t: None, "same text", "text")  # local

    assert len(sink.events) == 2
    assert {e.direction for e in sink.events} == {"local", "remote"}


# --- dedup cache thread safety ------------------------------------------------


def test_dedup_cache_survives_concurrent_captures_from_many_threads(tmp_config):
    sink = FakeHistorySink()
    manager = _make_manager(tmp_config, sink)

    def worker():
        for _ in range(50):
            manager._try_capture_history(
                lambda: manager._build_local_capture_event(
                    "text", "shared-payload", "shared-payload"
                )
            )

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert all(not thread.is_alive() for thread in threads)
    # Sync behaviour unaffected: whatever the interleaving, only
    # correctly-formed local captures of the shared payload get recorded.
    for event in sink.events:
        assert event.direction == "local"
        assert event.payload == "shared-payload"
        assert event.payload_type == "text"
    # Coherence bound: every attempt hashes to the same single dedup digest
    # (identical content + type + direction + source scope), so the cache can
    # only ever hold one entry no matter how the threads interleave.
    assert len(manager._history_dedup_cache) <= 1


# --- failure isolation --------------------------------------------------------


def test_sink_failure_never_blocks_send_or_leaks_payload_in_logs(tmp_config, caplog):
    secret_payload = "top secret clipboard contents"
    sink = FakeHistorySink(raise_on_record=True)
    manager = _make_manager(tmp_config, sink)

    calls = []
    with caplog.at_level(logging.ERROR):
        manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), secret_payload, "text")

    assert calls == [(secret_payload, "text")]  # sync path unaffected
    for record in caplog.records:
        assert secret_payload not in record.getMessage()


def test_no_op_sink_is_default_when_history_absent(tmp_config):
    manager = ClipboardManager(tmp_config)
    assert isinstance(manager.history_sink, NoOpHistorySink)
    calls = []
    # Must not raise even though no sink was supplied.
    manager.clipboard_to_base64(lambda c, t: calls.append((c, t)), "text content", "text")
    assert calls == [("text content", "text")]


# --- non-blocking latency --------------------------------------------------


def test_history_capture_adds_no_meaningful_synchronous_latency(tmp_config):
    class InstantFakeService:
        def record(self, event):
            return "fake-id"

    real_queued_sink = history_service.QueuedHistorySink(InstantFakeService(), maxsize=500)
    try:
        manager_on = _make_manager(tmp_config, real_queued_sink)
        manager_off = _make_manager(tmp_config, None)

        large_text = "x" * (200 * 1024)  # near a "maximum-size" text payload

        start = time.perf_counter()
        for _ in range(200):
            manager_off.clipboard_to_base64(lambda c, t: None, large_text, "text")
        baseline = time.perf_counter() - start

        start = time.perf_counter()
        for _ in range(200):
            manager_on.clipboard_to_base64(lambda c, t: None, large_text, "text")
        with_history = time.perf_counter() - start

        # Enqueuing must stay cheap: generous bound to avoid environment flakiness.
        assert with_history < baseline + 0.5
    finally:
        real_queued_sink.stop(timeout=2.0)


# --- transport wiring smoke test --------------------------------------------


def test_transport_manager_construction_wires_history_sink_and_transport_label():
    from stomp_ws.stomp_manager import STOMPManager
    from p2p.p2p_manager import P2PManager
    from core.config import Config

    sink = FakeHistorySink()
    cfg = Config(file_name="unused-in-this-test")

    stomp = STOMPManager(cfg, history_sink=sink)
    assert stomp.clipboard_manager.history_sink is sink
    assert stomp.clipboard_manager.history_transport == "p2s"

    p2p = P2PManager(cfg, history_sink=sink)
    try:
        assert p2p.clipboard_manager.history_sink is sink
        assert p2p.clipboard_manager.history_transport == "p2p"
    finally:
        # P2PManager starts an asyncio event-loop thread in __init__; stop it.
        p2p.loop.call_soon_threadsafe(p2p.loop.stop)
