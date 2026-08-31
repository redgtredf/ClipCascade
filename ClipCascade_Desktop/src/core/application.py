import logging
import sys


from core.constants import *

from core.config import Config
from core.device_metadata import ensure_device_identity
from utils.request_manager import RequestManager
from utils.cipher_manager import CipherManager
from stomp_ws.stomp_manager import STOMPManager
from p2p.p2p_manager import P2PManager
from history import service as history_service
from history import ipc as history_ipc
from history.actions import HistoryActionExecutor
from history_ui import launcher as history_launcher_mod
from utils.notification_manager import NotificationManager

if PLATFORM == WINDOWS:
    import ctypes
    from clipboard import clipboard_monitor_win
    from history import hotkey_win
elif PLATFORM == MACOS or PLATFORM.startswith(LINUX):
    import fcntl


if PLATFORM.startswith(LINUX) and LINUX_USE_CLI_UI:
    import pyfiglet
    from cli.login import LoginForm
    from cli.info import CustomDialog
    from cli.tray import TaskbarPanel
    from cli.message_box import MessageBox
    from cli.echo import Echo
else:
    from gui.login import LoginForm
    from gui.info import CustomDialog
    from gui.tray import TaskbarPanel
    from gui.message_box import MessageBox


class Application:
    def __init__(
        self,
        log_file_path=LOG_FILE_NAME,
        data_file_path=DATA_FILE_NAME,
        mutex_identifier=MUTEX_NAME,
    ):
        self.history_sink = None
        self.history_ipc_server = None
        self.history_launcher = None
        self.history_service = None
        try:
            self.log_file_path = os.path.join(
                get_program_files_directory(), log_file_path
            )
            self.data_file_path = os.path.join(
                get_program_files_directory(), data_file_path
            )
            self.mutex_identifier = mutex_identifier

            if PLATFORM == MACOS or PLATFORM.startswith(LINUX):
                self.lock_file = None  # File(lock) object
                self.mutex_identifier = os.path.join(
                    get_program_files_directory(), self.mutex_identifier
                )

            self.config = Config(
                file_name=self.data_file_path
            )  # Maintain a single configuration instance for the entire application lifecycle.

            self.request_manager = RequestManager(self.config)
            self.history_sink = self._init_history_sink()
            self.stomp_manager = STOMPManager(self.config, history_sink=self.history_sink)
            self.p2p_manager = P2PManager(self.config, history_sink=self.history_sink)
            self.cipher_manager = CipherManager(self.config)
            self._attach_history_actions()
            self._setup_history_hotkey_callbacks()
        except Exception as e:
            CustomDialog(
                f"An error occurred during application initialization: {e}",
                msg_type="error",
            ).mainloop()

    def _init_history_sink(self):
        """Windows-only: open the encrypted clipboard-history store, start
        the authenticated history IPC server and launcher behind it, and
        wrap capture in a bounded, non-blocking sink. Any failure
        (unsupported platform, DPAPI/SQLite trouble, IPC startup) must fall
        back to no history at all, never to a broken/half-initialized app,
        so this never lets an exception escape."""
        if PLATFORM != WINDOWS:
            return None
        try:
            history_dir = os.path.join(get_program_files_directory(), "history")
            init_result = history_service.bootstrap(history_dir)
            if not init_result.enabled or init_result.service is None:
                if init_result.disabled_reason:
                    logging.warning(
                        "Clipboard history disabled: %s", init_result.disabled_reason
                    )
                return None
            try:
                init_result.service.orphan_cleanup()
            except Exception:
                logging.exception(
                    "History orphan cleanup failed at startup; continuing"
                )

            ipc_server = self._start_history_ipc_server(init_result.service)
            self.history_ipc_server = ipc_server
            self.history_service = init_result.service
            self.history_launcher = history_launcher_mod.HistoryProcessLauncher(
                ipc_server=ipc_server
            )

            return history_service.QueuedHistorySink(
                init_result.service,
                on_recorded=(ipc_server.notify_entry_added if ipc_server else None),
            )
        except Exception:
            logging.exception(
                "Failed to initialize clipboard history; continuing without it"
            )
            return None

    @staticmethod
    def _start_history_ipc_server(service):
        """The IPC server lets an on-demand history window query/command the
        service without ever touching SQLite or the DPAPI key directly. Its
        absence must never take clipboard history capture down with it."""
        try:
            server = history_ipc.HistoryIpcServer(service)
            server.start()
            return server
        except history_ipc.HistoryIpcUnavailableError:
            return None
        except Exception:
            logging.exception(
                "Failed to start the history IPC server; the history window "
                "will be unavailable, clipboard history capture continues"
            )
            return None

    def _attach_history_actions(self):
        """Give the IPC server the main-process action executor (copy again,
        downloads, folder open...). The managers are constructed after the
        history stack, so this runs once both exist. Failure only costs the
        window's action buttons, never capture or sync."""
        try:
            if self.history_ipc_server is None or self.history_service is None:
                return

            def managers():
                found = []
                for transport_manager in (self.stomp_manager, self.p2p_manager):
                    clipboard_manager = getattr(transport_manager, "clipboard_manager", None)
                    if clipboard_manager is not None:
                        found.append(clipboard_manager)
                return found

            self.history_ipc_server.action_executor = HistoryActionExecutor(
                self.history_service,
                managers,
            )
        except Exception:
            logging.exception(
                "Failed to attach history actions; the history window will "
                "be read-only, capture and sync continue"
            )

    def _setup_history_hotkey_callbacks(self):
        """Windows-only: install the hotkey lifecycle hooks on the clipboard
        monitor before it can possibly start (the hidden message window is
        created when a transport manager connects). Enablement itself happens
        in run(), once the loaded config is available; see history/hotkey_win."""
        if PLATFORM != WINDOWS:
            return
        try:
            clipboard_monitor_win.set_hotkey_callbacks(
                on_window_ready=hotkey_win.on_window_ready,
                on_window_closing=hotkey_win.on_window_closing,
                on_hotkey=hotkey_win.on_hotkey,
            )
        except Exception:
            logging.exception(
                "Failed to prepare the history hotkey; the tray path still works"
            )

    def _setup_history_hotkey(self):
        """Turn the (off-by-default) Ctrl+Alt+V history shortcut on for this
        session when the setting says so. Failure only costs the hotkey."""
        if PLATFORM != WINDOWS:
            return
        try:
            hotkey_win.enable(
                self.config,
                on_trigger=self._open_history_window,
                on_notice=self._notify_history_notice,
            )
        except Exception:
            logging.exception(
                "Failed to enable the history hotkey; the tray path still works"
            )

    def _open_history_window(self):
        """Open or focus the single history window (tray menu, tray
        double-click and hotkey all land here). Every failure degrades to a
        log line so a broken history can never break the tray or sync."""
        launcher = self.history_launcher
        if launcher is None:
            logging.info(
                "History window unavailable: history is not active in this session"
            )
            return
        try:
            outcome = launcher.open_or_focus()
            logging.info("History window request outcome: %s", outcome)
        except Exception:
            logging.exception("Failed to open the clipboard history window")

    def _notify_history_notice(self, message):
        """One-shot user notices for the history feature, respecting the
        user's notification preference like every other notice."""
        try:
            NotificationManager(self.config).notify(APP_NAME, message, timeout=10)
        except Exception:
            logging.exception("Failed to show a history notice")

    def setup_logging(self):
        from utils.error_logging import install_excepthooks, setup_rotating_logging

        setup_rotating_logging(self.log_file_path, level=LOG_LEVEL)
        install_excepthooks()

    def ensure_single_instance(self):
        if PLATFORM == WINDOWS:
            ctypes.windll.kernel32.CreateMutexW(None, False, self.mutex_identifier)
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                CustomDialog(
                    "Another instance of ClipCascade is already running.",
                    msg_type="warning",
                ).mainloop()
                sys.exit(0)
        elif PLATFORM == MACOS or PLATFORM.startswith(LINUX):
            if PLATFORM == MACOS:
                app_dir = get_program_files_directory()
                if not os.path.exists(app_dir):
                    try:
                        os.makedirs(app_dir)
                    except Exception as e:
                        CustomDialog(
                            f"An error occurred while creating the directory '{app_dir}'. Error: {e}",
                            msg_type="error",
                        ).mainloop()
                        sys.exit(1)

            # Create the lock file
            try:
                self.create_lock_file()
            except IOError:
                run_anyway = MessageBox().askquestion(
                    "ClipCascade",
                    "Another instance of ClipCascade is already running. Do you want to run anyway?",
                )
                if run_anyway == "yes":
                    os.remove(self.mutex_identifier)
                    self.create_lock_file()
                else:
                    self.lock_file = None
                    sys.exit(0)

    def create_lock_file(self, path=None):
        if path is None:
            path = self.mutex_identifier

        if PLATFORM == MACOS or PLATFORM.startswith(LINUX):
            self.lock_file = open(path, "w")
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def authenticate_and_connect(self):
        # Attempt to connect with existing cookie
        if self.config.data.get("cookie"):
            ws_conn_successful, msg = self._get_ws_manager().connect()
            if ws_conn_successful:
                self._get_ws_manager().is_login_phase = False
                return

        # enable login form
        used_saved_credentials = False
        display_login_success_dialog = False
        if PLATFORM.startswith(LINUX) and LINUX_USE_CLI_UI:
            Echo("═" * 14 + "\n║ LOGIN FORM ║\n" + "═" * 14)
        while True:
            if (
                self.config.data.get("cookie") is not None
                and self.config.data["save_password"]
                and self.config.data["cipher_enabled"] == False
                and not used_saved_credentials
            ):
                # Attempt to connect with password when using saved credentials
                used_saved_credentials = True
            else:
                display_login_success_dialog = True
                self.config.data["password"] = ""  # Clear the password
                login_form = LoginForm(
                    self.config,
                    on_quit_callback=(
                        None
                        if (PLATFORM.startswith(LINUX) and LINUX_USE_CLI_UI)
                        else lambda: sys.exit(0)
                    ),
                )
                login_form.mainloop()  # wait until login form is closed
                raw_password = self.config.data[
                    "password"
                ]  # Store the raw password temporarily for hashing
                self.config.data["password"] = (
                    CipherManager.string_to_sha3_512_lowercase_hex(raw_password)
                )  # Hash the password

            login_successful, msg_login, self.config.data["cookie"] = (
                self.request_manager.login()
            )
            if login_successful:
                self.config.data["csrf_token"] = self.request_manager.get_csrf_token()
                self.config.data["server_mode"] = self.request_manager.get_server_mode()
                if self.config.data["server_mode"] == "P2P":
                    self.config.data["stun_url"] = self.request_manager.get_stun_url()
                    self.config.data["maxsize"] = -1
                    self.config.data["websocket_url"] = Config.convert_to_websocket_url(
                        self.config.data["server_url"], WEBSOCKET_ENDPOINT_P2P
                    )
                else:
                    self.config.data["stun_url"] = ""
                    self.config.data["maxsize"] = self.request_manager.maxsize()
                    self.config.data["websocket_url"] = Config.convert_to_websocket_url(
                        self.config.data["server_url"], WEBSOCKET_ENDPOINT
                    )
                ws_conn_successful, msg = self._get_ws_manager().connect()
                if ws_conn_successful:
                    self._get_ws_manager().is_login_phase = False
                    if self.config.data["cipher_enabled"]:
                        self.config.data["hashed_password"] = (
                            self.cipher_manager.hash_password(raw_password)
                        )
                    if not self.config.data["save_password"]:
                        self.config.data["password"] = ""
                    if display_login_success_dialog:
                        CustomDialog(
                            "Success! ClipCascade will now run in the task bar/menu bar.",
                            msg_type="success",
                            timeout=5000,
                        ).mainloop()
                    break
                else:
                    CustomDialog(
                        "Login successful but websocket connection failed. \nPlease check websocket-url\n"
                        + msg,
                        msg_type="error",
                    ).mainloop()
            else:
                CustomDialog("Login Failed\n" + msg_login, msg_type="error").mainloop()

            raw_password = None  # Clear the raw password
            if PLATFORM.startswith(LINUX) and LINUX_USE_CLI_UI:
                Echo("-" * 53)

    def _get_ws_manager(self):
        if self.config.data["server_mode"] == "P2P":
            return self.p2p_manager
        else:
            return self.stomp_manager

    def get_version_update_status(self) -> list:
        """
        Checks for a new version of the application by comparing the current version
        with the one available in a remote JSON file.

        Returns:
        list: [bool, str, str, str] - [Is new version available, latest version, current version, release URL]
        """
        try:
            response = RequestManager.get(VERSION_URL)
            response_data = response.json()
            if PLATFORM == WINDOWS:
                key = "windows"
            elif PLATFORM == MACOS:
                key = "macos"
            elif PLATFORM.startswith(LINUX):
                if not LINUX_USE_CLI_UI:
                    key = "linux_gui"
                else:
                    key = "linux_non_gui"

            if response_data[key] != APP_VERSION:
                return [True, response_data[key], APP_VERSION, RELEASE_URL]
        except Exception as e:
            logging.error(f"Error checking for new version: {e}")
        return [False, "", APP_VERSION, RELEASE_URL]

    def get_donation_url(self) -> str:
        try:
            metadata = self.request_manager.get_metadata()
            if metadata is not None:
                return metadata.get("funding", None)
        except Exception as e:
            logging.error(f"Error fetching metadata: {e}")
        return None

    def logoff_and_exit(self):
        try:
            self._get_ws_manager().disconnect()
            self.request_manager.logout()
            self.config.data["hashed_password"] = None
            self.config.data["cookie"] = None
            self.config.data["maxsize"] = None
            self.config.data["password"] = ""
            self.config.data["csrf_token"] = ""
            self.config.save()
        except Exception as e:
            raise Exception(f"Error during logging off: {e}")

    def banner(self):
        if PLATFORM.startswith(LINUX) and LINUX_USE_CLI_UI:
            Echo(pyfiglet.figlet_format(APP_NAME))
            Echo("*" * 53)
            Echo("Real-Time Clipboard Syncing".center(53))
            Echo(GITHUB_URL.center(53))
            Echo("*" * 53)

    def run(self):
        try:
            self.setup_logging()
            self.banner()
            self.ensure_single_instance()
            self.config.load()
            ensure_device_identity(self.config)
            self.authenticate_and_connect()
            self.config.save()
            update_available = self.get_version_update_status()
            donation_url = self.get_donation_url()

            sys_tray = TaskbarPanel(
                on_connect_callback=self._get_ws_manager().manual_reconnect,
                on_disconnect_callback=self._get_ws_manager().disconnect,
                on_logoff_callback=self.logoff_and_exit,
                new_version_available=update_available,
                github_url=GITHUB_URL,
                donation_url=donation_url,
                ws_interface=self._get_ws_manager(),
                config=self.config,
                **(
                    {"on_open_history_callback": self._open_history_window}
                    if PLATFORM == WINDOWS
                    else {}
                ),
            )
            self._get_ws_manager().set_tray_ref(sys_tray)
            self._setup_history_hotkey()
            sys_tray.run()
        except Exception as e:
            msg = f"An unexpected error has occurred: {e}"
            logging.error(msg)
            CustomDialog(
                msg + "\nCheck logs in project directory", msg_type="error"
            ).mainloop()
        finally:
            self._get_ws_manager().disconnect()
            if PLATFORM == WINDOWS:
                try:
                    hotkey_win.shutdown()
                except Exception:
                    logging.exception("Failed to shut down the history hotkey")
            if self.history_launcher is not None:
                try:
                    self.history_launcher.shutdown_ipc()
                except Exception:
                    logging.exception("Failed to shut down the history IPC server/child")
            if self.history_sink is not None:
                try:
                    self.history_sink.stop()
                except Exception:
                    logging.exception("Failed to drain clipboard history on shutdown")
            if PLATFORM == MACOS or PLATFORM.startswith(LINUX):
                if self.lock_file is not None:
                    fcntl.flock(self.lock_file, fcntl.LOCK_UN)
                    self.lock_file.close()
                    os.remove(self.mutex_identifier)
