import logging
import win32gui
import win32api
import win32con
import win32clipboard
import threading
import ctypes
import time
from PIL import ImageGrab


_clipboard_thread = None
_hwnd = None  # Store the window handle
_callback_update = None
_block_image_once = False

# History-hotkey lifecycle hooks (see history/hotkey_win.py). All three run on
# this module's message-window thread, which the thread-affine
# RegisterHotKey/UnregisterHotKey calls require.
_hotkey_on_window_ready = None  # (hwnd) -> None
_hotkey_on_window_closing = None  # (hwnd) -> None
_hotkey_on_hotkey = None  # () -> None

WM_HOTKEY = 0x0312
# App-private message asking the window thread to (re)run the ready hook
# after the hotkey is enabled post-creation (WM_APP range is free for apps).
_WM_APP_HOTKEY_READY = 0x8001


def _get_clipboard_content(enable_image_monitoring=False, enable_file_monitoring=False):
    """
    Get the content of the clipboard.

    Format priority when several formats are present (e.g. a Word/browser
    copy renders both text and bitmap): text wins, then files, then image.
    Preferring the image silently discarded the text of every composite
    copy; the text is the intentional payload in those copies.

    Image:
        PNG -> PngImagePlugin.PngImageFile
        DIB -> BmpImagePlugin.BibImageFile
        PNG, DIB, JPG, etc. -> [file_path1, file_path2, ...]

    Text:
        CF_UNICODETEXT, CF_TEXT -> str

    Files:
        CF_HDROP -> (file_path1, file_path2, ...)
    """
    # sleep 0.5 to avoid clipboard not ready for read
    time.sleep(0.5)
    clipboard_type = None
    clipboard_content = None

    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            clipboard_type = "text"
            clipboard_content = text
        elif win32clipboard.IsClipboardFormatAvailable(win32con.CF_TEXT):
            text_bytes = win32clipboard.GetClipboardData(win32con.CF_TEXT)
            text = text_bytes.decode()
            clipboard_type = "text"
            clipboard_content = text
        elif enable_file_monitoring and win32clipboard.IsClipboardFormatAvailable(
            win32con.CF_HDROP
        ):
            files = win32clipboard.GetClipboardData(win32con.CF_HDROP)
            clipboard_type = "files"
            clipboard_content = files
    finally:
        win32clipboard.CloseClipboard()

    if (
        clipboard_type is None
        and enable_image_monitoring
        and win32clipboard.IsClipboardFormatAvailable(win32con.CF_BITMAP)
    ):
        clipboard_type = "image"
        clipboard_content = ImageGrab.grabclipboard()

    return (clipboard_type, clipboard_content)


def _process_message(
    hwnd: int,
    msg: int,
    wparam: int,
    lparam: int,
    enable_image_monitoring=False,
    enable_file_monitoring=False,
):
    global _block_image_once
    WM_CLIPBOARDUPDATE = 0x031D
    if msg == WM_CLIPBOARDUPDATE:
        clip = _get_clipboard_content(enable_image_monitoring, enable_file_monitoring)

        try:
            if clip[0] == "text" and _callback_update:
                _callback_update(clip[0], clip[1])

            if enable_image_monitoring and clip[0] == "image" and _callback_update:
                if _block_image_once:
                    _block_image_once = False
                else:
                    _callback_update(clip[0], clip[1])

            if enable_file_monitoring and clip[0] == "files" and _callback_update:
                _callback_update(clip[0], clip[1])
        except Exception as e:
            logging.error(f"Error processing clipboard update: {e}")
    elif msg == WM_HOTKEY and _hotkey_on_hotkey is not None:
        try:
            _hotkey_on_hotkey()
        except Exception as e:
            logging.error(f"Error handling the history hotkey: {e}")
    elif msg == _WM_APP_HOTKEY_READY and _hotkey_on_window_ready is not None:
        try:
            _hotkey_on_window_ready(hwnd)
        except Exception as e:
            logging.error(f"Error registering the history hotkey: {e}")
    return 0


def _create_window(enable_image_monitoring=False, enable_file_monitoring=False):
    global _hwnd
    className = "ClipboardHook"
    wc = win32gui.WNDCLASS()
    wc.lpfnWndProc = lambda hwnd, msg, wparam, lparam: _process_message(
        hwnd, msg, wparam, lparam, enable_image_monitoring, enable_file_monitoring
    )
    wc.lpszClassName = className
    wc.hInstance = win32api.GetModuleHandle(None)
    class_atom = win32gui.RegisterClass(wc)
    _hwnd = win32gui.CreateWindow(
        class_atom, className, 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None
    )


def _runner(enable_image_monitoring=False, enable_file_monitoring=False):
    global _hwnd
    _create_window(enable_image_monitoring, enable_file_monitoring)
    ctypes.windll.user32.AddClipboardFormatListener(_hwnd)
    if _hotkey_on_window_ready is not None:
        try:
            _hotkey_on_window_ready(_hwnd)
        except Exception as e:
            logging.error(f"Error setting up the history hotkey: {e}")
    try:
        win32gui.PumpMessages()
    finally:
        if _hotkey_on_window_closing is not None:
            try:
                _hotkey_on_window_closing(_hwnd)
            except Exception as e:
                logging.error(f"Error unregistering the history hotkey: {e}")
        ctypes.windll.user32.RemoveClipboardFormatListener(_hwnd)
        win32gui.DestroyWindow(_hwnd)
        win32gui.UnregisterClass("ClipboardHook", win32api.GetModuleHandle(None))


def _start(enable_image_monitoring=False, enable_file_monitoring=False):
    global _clipboard_thread
    if not _clipboard_thread:
        _clipboard_thread = threading.Thread(
            target=_runner,
            args=(enable_image_monitoring, enable_file_monitoring),
            daemon=True,
        )
        _clipboard_thread.start()


def set_hotkey_callbacks(on_window_ready=None, on_window_closing=None, on_hotkey=None):
    """Install the history-hotkey lifecycle hooks. Must be called before the
    monitor starts; hooks stay installed across monitor restarts so a
    recreated window re-registers automatically."""
    global _hotkey_on_window_ready, _hotkey_on_window_closing, _hotkey_on_hotkey
    _hotkey_on_window_ready = on_window_ready
    _hotkey_on_window_closing = on_window_closing
    _hotkey_on_hotkey = on_hotkey


def request_hotkey_setup():
    """Thread-safe: ask the window thread to (re)run the window-ready hook,
    so a hotkey enabled after the window was created can register with the
    thread-affine RegisterHotKey. No-op while no window exists."""
    if _hwnd is not None:
        win32gui.PostMessage(_hwnd, _WM_APP_HOTKEY_READY, 0, 0)


def stop():
    global _clipboard_thread, _hwnd, _callback_update, _block_image_once
    if _clipboard_thread and _hwnd:
        win32gui.PostMessage(
            _hwnd, win32con.WM_QUIT, 0, 0
        )  # Send WM_QUIT to the window
        _clipboard_thread.join()  # Wait for the thread to finish
        _clipboard_thread = None
        _hwnd = None
        _callback_update = None
        _block_image_once = False
        logging.info("Clipboard monitor stopped")


def wait():
    global _clipboard_thread
    if _clipboard_thread:
        _clipboard_thread.join()


def enable_block_image_once():
    global _block_image_once
    _block_image_once = True


def on_update(callback, enable_image_monitoring=False, enable_file_monitoring=False):
    global _callback_update
    _callback_update = callback
    _start(enable_image_monitoring, enable_file_monitoring)
