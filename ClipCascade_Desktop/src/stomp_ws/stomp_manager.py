import json
import logging
import random
import threading
import time


from interfaces.ws_interface import WSInterface
from stomp_ws.client import Client
from core.config import Config
from core.device_metadata import extract_remote_identity, outgoing_device_metadata
from utils.cipher_manager import CipherManager
from clipboard.clipboard_manager import ClipboardManager
from utils.notification_manager import NotificationManager
from utils.request_manager import RequestManager
from utils.ssl_helper import websocket_sslopt_for_config
from core.constants import *

if PLATFORM.startswith(LINUX) and LINUX_USE_CLI_UI:
    from cli.tray import TaskbarPanel
else:
    from gui.tray import TaskbarPanel

# Exponential backoff for automatic reconnects: start fast so a brief network
# blip is invisible, grow so a down server is not hammered in lockstep by
# every client (stampede), with jitter to de-synchronise reconnect attempts.
RECONNECT_BACKOFF_INITIAL_S = 1.0
RECONNECT_BACKOFF_FACTOR = 2.0
RECONNECT_BACKOFF_MAX_S = 60.0
RECONNECT_BACKOFF_JITTER_S = 0.5
RECEIVE_FAILURE_NOTIFY_INTERVAL_S = 60.0


class STOMPManager(WSInterface):
    def __init__(self, config: Config, is_login_phase=True, history_sink=None):
        self.config = config
        self.clipboard_manager = ClipboardManager(
            self.config, history_sink=history_sink, history_transport="p2s"
        )
        self.cipher_manager = CipherManager(self.config)
        self.notification_manager = NotificationManager(self.config)
        self.sys_tray: TaskbarPanel = None
        self.first_conn_lost = True
        self.is_login_phase = is_login_phase
        self.client = None
        self.is_connected = False
        self.disconnected = False
        self.is_auto_reconnecting = False
        self._reconnect_attempts = 0
        self._reconnect_scheduled = False
        self._reconnect_lock = threading.Lock()
        self._reconnect_timer = None

    def set_tray_ref(self, sys_tray: TaskbarPanel):
        """
        Sets the system tray reference.
        """
        self.sys_tray = sys_tray
        self.clipboard_manager.set_tray_ref(sys_tray)

    def get_total_timeout(self):
        """
        Returns the total timeout value in milliseconds."""
        return (RECONNECT_WS_TIMER * 1000) + WEBSOCKET_TIMEOUT

    def get_stats(self):
        return None

    def connect(self) -> tuple[bool, str]:
        try:
            if self.is_connected:
                return True, ""
            self.client = Client(
                self.config.data["websocket_url"],
                headers={
                    "Cookie": RequestManager.format_cookie(
                        self.config.data["cookie"]
                    )
                },
                on_close_callback=self._on_close,
                sslopt=websocket_sslopt_for_config(self.config),
            )
            self.client.connect(
                timeout=WEBSOCKET_TIMEOUT,
                connectCallback=lambda _: self.client.subscribe(  # receive event
                    destination=SUBSCRIPTION_DESTINATION,
                    callback=self._receive,
                ),
            )
            if self.disconnected:
                self.disconnect()
                return False, "Websocket disconnected"

            # logging.info("Websocket connected")
            self.is_connected = True
            self.is_auto_reconnecting = False
            self._reset_reconnect_state()
            if not self.first_conn_lost:
                self.first_conn_lost = True
                self.notification_manager.notify(
                    title=f"{APP_NAME}: WebSocket Connection Restored 🔗",
                    message="Connection re-established",
                )

            # send event
            self.clipboard_manager.on_copy(self.send)
            return True, "Websocket connected"
        except Exception as e:
            msg = f"Failed to connect websocket: {e}"
            logging.error(msg)
            return False, msg

    def _on_close(self):
        self.is_connected = False
        if not self.is_login_phase and not self.disconnected:
            self._schedule_reconnect()

    def _reset_reconnect_state(self):
        with self._reconnect_lock:
            self._reconnect_attempts = 0
            self._reconnect_scheduled = False
            timer, self._reconnect_timer = self._reconnect_timer, None
        if timer is not None:
            timer.cancel()

    def _schedule_reconnect(self, delay_s=None):
        """Single-flight reconnect scheduling with exponential backoff.

        The guard matters: the websocket close callback, a failed connect()
        and manual_reconnect() can all race to schedule the next attempt;
        without it every failure would stack another retry thread (the
        stampede)."""
        with self._reconnect_lock:
            if self._reconnect_scheduled:
                return
            if delay_s is None:
                delay_s = min(
                    RECONNECT_BACKOFF_INITIAL_S
                    * (RECONNECT_BACKOFF_FACTOR ** self._reconnect_attempts),
                    RECONNECT_BACKOFF_MAX_S,
                )
                delay_s += random.uniform(0.0, RECONNECT_BACKOFF_JITTER_S)
            self._reconnect_scheduled = True
            self._reconnect_attempts += 1
            timer = threading.Timer(delay_s, self._reconnect_now)
            timer.daemon = True
            self._reconnect_timer = timer
        timer.start()

    def _reconnect_now(self):
        with self._reconnect_lock:
            self._reconnect_scheduled = False
            self._reconnect_timer = None
        if self.is_login_phase or self.disconnected:
            self._reset_reconnect_state()
            return
        self.is_auto_reconnecting = True
        if self.first_conn_lost:
            self.first_conn_lost = False
            self.notification_manager.notify(
                title=f"{APP_NAME}: WebSocket Connection Lost ⛓️‍💥",
                message="Check your internet connection. Retrying...",
            )
        ok, _ = self.connect()
        if not ok:
            # backoff grows with every consecutive failure
            self._schedule_reconnect()

    def send(self, payload: str, payload_type: str = "text"):
        try:
            if self.is_connected:
                previous_hash = self.clipboard_manager.previous_clipboard_hash
                if self.clipboard_manager.has_clipboard_changed(payload):
                    if self.config.data["cipher_enabled"]:
                        payload = CipherManager.encode_to_json_string(
                            **self.cipher_manager.encrypt(payload)
                        )
                    body_dict = {"payload": payload, "type": payload_type}
                    # Optional device metadata: legacy receivers ignore the
                    # extra field; absent when no identity was generated.
                    device_metadata = outgoing_device_metadata(self.config)
                    if device_metadata is not None:
                        body_dict["metadata"] = device_metadata
                    body = json.dumps(body_dict)
                    try:
                        self.client.send(destination=SEND_DESTINATION, body=body)
                    except Exception:
                        # The dedupe hash was burned by the changed-check
                        # above; un-burn it so the same content is re-sent
                        # on the next copy/attempt instead of being lost.
                        self.clipboard_manager.restore_previous_clipboard_hash(previous_hash)
                        raise
        except Exception as e:
            logging.error(f"Failed to send data: {e}")

    def _receive(self, frame: any) -> str:
        try:
            if self.is_connected:
                body = json.loads(frame.body)
                payload = body["payload"]
                payload_type = body.get("type", "text")
                # Optional device metadata: absent/invalid maps to "Remote
                # device" in history, never to a rejected payload.
                device_id, device_name = extract_remote_identity(body.get("metadata"))
                if self.config.data["cipher_enabled"]:
                    payload = self.cipher_manager.decrypt(
                        **CipherManager.decode_from_json_string(payload)
                    )

                previous_hash = self.clipboard_manager.previous_clipboard_hash
                if self.clipboard_manager.has_clipboard_changed(payload):
                    delivered = self.clipboard_manager.base64_to_clipboard(
                        base64_string=payload,
                        type_=payload_type,
                        source_device_id=device_id,
                        source_device_name=device_name,
                    )
                    if not delivered:
                        # Paste failed (e.g. clipboard busy): un-burn the
                        # dedupe hash so a retry of the same content works.
                        self.clipboard_manager.restore_previous_clipboard_hash(previous_hash)
        except json.decoder.JSONDecodeError:
            logging.error(
                "If cipher is enabled, please make sure it is enabled on all devices"
            )
            self._notify_receive_failure(
                "Invalid clipboard data received",
                "If encryption is enabled, make sure it is enabled on all devices",
            )
        except Exception as e:
            logging.error(f"Failed to receive data: {e}")
            self._notify_receive_failure(
                "Invalid clipboard data received",
                "The clipboard payload could not be processed",
            )

    def _notify_receive_failure(self, title, message):
        """Surface a receive-path failure to the user, rate-limited so a
        stream of bad frames cannot spam notifications; returns silently when
        notifications are unavailable."""
        now = time.monotonic()
        last = getattr(self, "_last_receive_failure_notify_s", None)
        if last is not None and now - last < RECEIVE_FAILURE_NOTIFY_INTERVAL_S:
            return
        self._last_receive_failure_notify_s = now
        try:
            self.notification_manager.notify(title=f"{APP_NAME}: {title}", message=message)
        except Exception:
            logging.exception("Failed to show receive-failure notification")

    def manual_reconnect(self):
        if not self.is_auto_reconnecting:
            self.disconnected = False
            if self.is_connected:
                return
            self._reset_reconnect_state()
            self._schedule_reconnect(delay_s=0.0)

    def disconnect(self):
        try:
            self.clipboard_manager.reset_previous_clipboard_hash()
            self.disconnected = True
            self.first_conn_lost = True
            self._reset_reconnect_state()
            try:
                self.client.disconnect()
                self.is_connected = False
                logging.info("Websocket disconnected")
            except Exception:
                pass  # silent catch
            self.clipboard_manager.stop()
        except Exception as e:
            logging.error(f"Failed to disconnect websocket: {e}")
