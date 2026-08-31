"""Opt-in global Ctrl+Alt+V shortcut that opens the clipboard history window.

Windows-only, implemented with a single Win32 `RegisterHotKey` bound to the
clipboard monitor's existing hidden message window -- no keyboard hooks, no
key-state polling, no elevation (per the integration plan's "Global shortcut"
section).

RegisterHotKey/UnregisterHotKey are thread-affine, so every win32 call here
runs on the monitor window's own thread via the lifecycle hooks installed by
`clipboard_monitor_win.set_hotkey_callbacks`:

- window creation (re)registers -- this also covers monitor restarts, so the
  hotkey is re-registered automatically when the window is recreated;
- window destruction always unregisters first, which is the guaranteed
  shutdown path: Application.run() -> ws disconnect -> clipboard_manager.stop()
  -> WM_QUIT -> monitor thread finally-block -> `on_window_closing`. The OS
  additionally drops any registration when the registering thread exits.

A registration failure (the combination is taken by another application, or
policy denies it) leaves the setting enabled in the config but the hotkey
inactive for the session; the user sees exactly ONE notice per session, and
the tray "Open history" path keeps working. The WM_HOTKEY dispatch itself is
a lightweight call onto a short-lived daemon thread so the monitor's message
pump never blocks behind a focus request or process spawn.
"""

import logging
import threading

import win32con
import win32gui

from clipboard import clipboard_monitor_win as _monitor

# Stable internal id; arbitrary app-private value within the documented
# 0x0000-0xBFFF hotkey-id range.
HOTKEY_ID = 0xBEEF
MODIFIERS = win32con.MOD_CONTROL | win32con.MOD_ALT
VIRTUAL_KEY = ord("V")

_enabled_config = False  # the persisted setting, honoured for this session
_session_active = False  # a hotkey is actually registered right now
_notice_shown = False  # the one-per-session failure notice was shown
_trigger = None  # opens/focuses the history window (Application callback)
_notice = None  # user-visible one-time notice (NotificationManager callback)
_registered_hwnd = None

# Coalesces hotkey presses while a dispatch is in flight, so the launcher's
# open_or_focus never runs concurrently with itself.
_dispatch_lock = threading.Lock()
_dispatch_in_flight = False


def enable(config, on_trigger, on_notice=None):
    """Turn the hotkey on for this session when the setting says so.

    Safe to call before or after the monitor window exists: if it is already
    up, a setup request is posted to its thread; otherwise the window-ready
    hook registers when it appears.
    """
    global _enabled_config, _trigger, _notice
    _trigger = on_trigger
    _notice = on_notice
    _enabled_config = bool(config.data.get("enable_history_hotkey"))
    if _enabled_config:
        logging.info("History hotkey enabled (Ctrl+Alt+V)")
        _monitor.request_hotkey_setup()


def on_window_ready(hwnd):
    """(Monitor thread.) (Re)register the hotkey against the fresh window."""
    global _session_active, _registered_hwnd
    if not _enabled_config or _session_active:
        return
    try:
        win32gui.RegisterHotKey(hwnd, HOTKEY_ID, MODIFIERS, VIRTUAL_KEY)
        _registered_hwnd = hwnd
        _session_active = True
        logging.info("History hotkey Ctrl+Alt+V registered")
    except Exception as error:
        _registration_failed(error)


def on_window_closing(hwnd):
    """(Monitor thread.) Guaranteed unregister before the window is destroyed."""
    global _session_active, _registered_hwnd
    if _registered_hwnd != hwnd:
        return
    try:
        win32gui.UnregisterHotKey(hwnd, HOTKEY_ID)
        logging.info("History hotkey Ctrl+Alt+V unregistered")
    except Exception:
        logging.exception("Failed to unregister the history hotkey")
    finally:
        _session_active = False
        _registered_hwnd = None


def on_hotkey():
    """(Monitor thread.) WM_HOTKEY arrived: dispatch the trigger off-thread."""
    global _dispatch_in_flight
    if not _session_active or _trigger is None:
        return
    with _dispatch_lock:
        if _dispatch_in_flight:
            return
        _dispatch_in_flight = True

    def run():
        global _dispatch_in_flight
        try:
            _trigger()
        except Exception:
            logging.exception("History hotkey handler failed")
        finally:
            with _dispatch_lock:
                _dispatch_in_flight = False

    threading.Thread(target=run, name="history-hotkey", daemon=True).start()


def shutdown():
    """Application-exit teardown. Ignores any late WM_HOTKEY and, if the
    monitor window is somehow still up, stops it so its destruction path
    performs the unregister. The normal quit path already stopped the
    monitor via the ws disconnect, making this a no-op there."""
    global _enabled_config, _session_active, _registered_hwnd
    _enabled_config = False
    _session_active = False
    _registered_hwnd = None
    try:
        _monitor.stop()
    except Exception:
        logging.exception("Failed to stop the clipboard monitor during hotkey shutdown")


def _registration_failed(error):
    """One log line per failure; exactly one user notice per session."""
    global _notice_shown
    logging.warning(
        "History hotkey Ctrl+Alt+V unavailable (%s); the tray menu still opens history",
        error,
    )
    if _notice is None or _notice_shown:
        return
    _notice_shown = True
    try:
        _notice(
            "The Ctrl+Alt+V history shortcut could not be registered "
            "(it may be in use by another application). "
            "History remains available from the ClipCascade tray menu."
        )
    except Exception:
        logging.exception("Failed to show the history hotkey notice")
