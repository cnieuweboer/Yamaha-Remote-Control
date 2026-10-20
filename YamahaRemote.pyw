import ctypes
from datetime import datetime
import faulthandler
import hashlib
import ipaddress
import json
import math
import os
import re
import sys
import time
import threading
import traceback
import tempfile
from collections import deque
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import build_opener, ProxyHandler
from PySide6.QtCore import QEasingCurve, QElapsedTimer, QRect, QEvent, QFileInfo, QFileSystemWatcher, QObject, QLockFile, QProcess, QRunnable, QStandardPaths, QThreadPool, QTimer, Qt, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileIconProvider,
    QGridLayout,
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QMenu,
    QLabel,
    QMessageBox,
    QPushButton,
    QProxyStyle,
    QStyle,
    QStylePainter,
    QStyleOptionComboBox,
    QStyledItemDelegate,
    QSystemTrayIcon,
    QWidget,
)


# Device requests connect directly; never forward LAN traffic or credentials to a proxy.
DEVICE_HTTP_OPENER = build_opener(ProxyHandler({}))


# ----------------------------
# Local amplifier protocol (newline-delimited JSON, version 1)
# ----------------------------


IPC_USER_SUFFIX = hashlib.sha256(str(Path.home()).encode("utf-8")).hexdigest()[:16]


def ipc_server_name(role, *, channel=None):
    channel = f"{role}.{channel}" if channel else role
    return f"remote-control.{channel}.v1.{IPC_USER_SUFFIX}"


IPC_SERVER_NAME = ipc_server_name("amplifier")
IPC_MAX_FRAME = 4096


def _ipc_encode(message):
    return (json.dumps(message, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def _ipc_frames(socket, buffer):
    messages = []
    while socket.bytesAvailable():
        # Parse between bounded reads so a burst of valid frames is allowed,
        # without allowing an oversized complete or partial frame.
        chunk = bytes(socket.read(IPC_MAX_FRAME + 1))
        if not chunk:
            break
        buffer.extend(chunk)
        while b"\n" in buffer:
            frame, _, tail = buffer.partition(b"\n")
            buffer[:] = tail
            if len(frame) > IPC_MAX_FRAME:
                raise ValueError("Local protocol frame too large")
            try:
                message = json.loads(frame)
            except RecursionError as error:
                raise ValueError("Local protocol nesting too deep") from error
            if (not isinstance(message, dict) or type(message.get("version")) is not int
                    or message["version"] != 1):
                raise ValueError("Unsupported local protocol message")
            messages.append(message)
        if len(buffer) > IPC_MAX_FRAME:
            raise ValueError("Local protocol frame too large")
    return messages


class AmplifierServer(QObject):
    """User-local state publisher and bounded command receiver; no device polling."""

    def __init__(self, snapshot, command, changed, parent=None):
        super().__init__(parent)
        self.snapshot = snapshot
        self.command = command
        self.changed = changed
        self.clients = {}
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.setMaxPendingConnections(8)
        # Only the role-lock owner may remove a socket left by a crashed process.
        self.lock = QLockFile(str(Path(QStandardPaths.writableLocation(
            QStandardPaths.TempLocation)) / (IPC_SERVER_NAME + ".lock")))
        self.lock.setStaleLockTime(0)
        self.available = self.lock.tryLock(0)
        if self.available:
            QLocalServer.removeServer(IPC_SERVER_NAME)
            self.available = self.server.listen(IPC_SERVER_NAME)
            if not self.available:
                self.lock.unlock()
        self.server.newConnection.connect(self._accept)

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if len(self.clients) >= 8:
                socket.abort()
                socket.deleteLater()
                continue
            socket.setReadBufferSize(IPC_MAX_FRAME + 1)
            self.clients[socket] = bytearray()
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(lambda socket=socket: self._remove(socket))
            self.changed()
            self._send(socket, self.snapshot())

    def _remove(self, socket):
        if socket in self.clients:
            del self.clients[socket]
            socket.deleteLater()
            self.changed()

    def _send(self, socket, message):
        if socket.bytesToWrite() > IPC_MAX_FRAME * 4:
            socket.abort()
            return
        socket.write(_ipc_encode(message))

    def _read(self, socket):
        try:
            for message in _ipc_frames(socket, self.clients[socket]):
                if (message.get("type") != "command"
                        or type(message.get("id")) is not int
                        or not isinstance(message.get("input"), str)
                        or message.get("command") not in ("volume_up", "volume_down", "mute")):
                    raise ValueError("Invalid local command")
                accepted, reason = self.command(message["command"], message["input"])
                self._send(socket, {"version": 1, "type": "ack", "id": message["id"],
                                    "accepted": accepted, "reason": reason})
        except (ValueError, UnicodeError, KeyError):
            socket.abort()

    def publish(self):
        message = self.snapshot()
        for socket in list(self.clients):
            self._send(socket, message)

    def close(self):
        self.available = False
        for socket in list(self.clients):
            socket.abort()
        self.server.close()
        self.lock.unlock()


# ----------------------------
# Exclusive calibration sessions (generic role-local protocol)
# ----------------------------


class ActivationServer(QObject):
    """User-local window activation, separate from device commands and leases."""

    def __init__(self, role, activate, parent=None):
        super().__init__(parent)
        self.role, self.activate = role, activate
        self.name = ipc_server_name(role, channel="activation")
        self.clients = {}
        self.lock = QLockFile(str(Path(tempfile.gettempdir()) / (self.name + ".lock")))
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.setMaxPendingConnections(4)
        self.available = self.lock.tryLock(0)
        if self.available:
            QLocalServer.removeServer(self.name)
            self.available = self.server.listen(self.name)
            if not self.available:
                self.lock.unlock()
        self.server.newConnection.connect(self._accept)

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if not self.available or len(self.clients) >= 4:
                socket.abort()
                socket.deleteLater()
                continue
            self.clients[socket] = bytearray()
            socket.setReadBufferSize(IPC_MAX_FRAME + 1)
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(lambda socket=socket: self._lost(socket))
            timer = QTimer(socket)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda socket=socket: socket.abort())
            timer.start(1000)
            socket.write(_ipc_encode({"version": 1, "type": "instance", "role": self.role,
                                      "process_id": os.getpid()}))

    def _read(self, socket):
        try:
            messages = _ipc_frames(socket, self.clients[socket])
            if not messages:
                return
            if (not self.available or len(messages) != 1
                    or messages[0] != {"version": 1, "type": "activate"}):
                raise ValueError("Invalid activation request")
            ok = bool(self.activate())
            socket.write(_ipc_encode({"version": 1, "type": "activated", "ok": ok}))
            socket.disconnectFromServer()
        except (ValueError, UnicodeError, KeyError):
            socket.abort()

    def _lost(self, socket):
        if socket in self.clients:
            del self.clients[socket]
            socket.deleteLater()

    def close(self):
        self.available = False
        for socket in list(self.clients):
            socket.abort()
        self.server.close()
        self.lock.unlock()


def _allow_foreground_activation(process_id):
    if sys.platform == "win32":
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.AllowSetForegroundWindow.argtypes = [ctypes.c_uint32]
            user32.AllowSetForegroundWindow.restype = ctypes.c_int
            user32.AllowSetForegroundWindow(process_id)
        except (OSError, AttributeError):
            pass  # Window restoration still works if foreground permission is denied.


def activate_existing_instance(role, timeout_ms=1500):
    """Bound startup retries and the complete handshake to one deadline."""
    socket = QLocalSocket()
    socket.setReadBufferSize(IPC_MAX_FRAME + 1)
    buffer = bytearray()
    deadline = time.monotonic() + timeout_ms / 1000

    def remaining():
        return max(0, int((deadline - time.monotonic()) * 1000))

    def receive(expected):
        while remaining():
            messages = _ipc_frames(socket, buffer)
            if messages:
                if len(messages) != 1 or messages[0].get("type") != expected:
                    return None
                return messages[0]
            if not socket.waitForReadyRead(remaining()) and not socket.bytesAvailable():
                return None
        return None

    try:
        while remaining():
            socket.connectToServer(ipc_server_name(role, channel="activation"))
            if socket.waitForConnected(min(150, remaining())):
                break
            socket.abort()
            time.sleep(min(0.05, remaining() / 1000))
        else:
            return False
        hello = receive("instance")
        if (not hello or hello.get("role") != role or type(hello.get("process_id")) is not int
                or not 0 < hello["process_id"] <= 0xFFFFFFFF):
            return False
        _allow_foreground_activation(hello["process_id"])
        payload = _ipc_encode({"version": 1, "type": "activate"})
        if socket.write(payload) != len(payload):
            return False
        if socket.bytesToWrite() and not socket.waitForBytesWritten(remaining()):
            return False
        reply = receive("activated")
        return bool(reply and reply.get("ok") is True)
    except (ValueError, UnicodeError, OSError, TypeError):
        return False
    finally:
        socket.abort()


class CalibrationServer(QObject):
    """A socket-owned, expiring lease. No device I/O in protocol callbacks."""

    def __init__(self, role, snapshot, changed, perform, parent=None):
        super().__init__(parent)
        self.snapshot, self.changed, self.perform = snapshot, changed, perform
        self.owner = None
        self.token = None
        self.busy = False
        self.sockets = {}
        self.name = ipc_server_name(role, channel="calibration")
        self.lock = QLockFile(str(Path(tempfile.gettempdir()) / (self.name + ".lock")))
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.setMaxPendingConnections(4)
        self.lease = QTimer(self)
        self.lease.setSingleShot(True)
        self.lease.setInterval(8000)
        self.lease.timeout.connect(self._expire)
        self.available = self.lock.tryLock(0)
        if self.available:
            QLocalServer.removeServer(self.name)
            self.available = self.server.listen(self.name)
            if not self.available:
                self.lock.unlock()
        self.server.newConnection.connect(self._accept)

    @property
    def active(self):
        return self.owner is not None

    def _accept(self):
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if len(self.sockets) >= 4:
                socket.abort()
                socket.deleteLater()
                continue
            socket.setReadBufferSize(IPC_MAX_FRAME + 1)
            self.sockets[socket] = bytearray()
            socket.readyRead.connect(lambda socket=socket: self._read(socket))
            socket.disconnected.connect(lambda socket=socket: self._lost(socket))
            self._send(socket, self.snapshot())

    def _send(self, socket, message):
        if socket not in self.sockets:
            return
        if socket.bytesToWrite() > IPC_MAX_FRAME * 4:
            socket.abort()
        else:
            socket.write(_ipc_encode(message))

    def _reply(self, socket, request_id, ok, error=""):
        self._send(socket, {"version": 1, "type": "result", "id": request_id,
                            "ok": bool(ok), "error": str(error)[:200]})

    def _read(self, socket):
        try:
            for message in _ipc_frames(socket, self.sockets[socket]):
                request_id, action, token = (message.get(key) for key in ("id", "action", "token"))
                if (message.get("type") != "calibration" or type(request_id) is not int
                        or not isinstance(action, str) or not isinstance(token, str)
                        or not 16 <= len(token) <= 80 or not isinstance(message.get("args", {}), dict)):
                    raise ValueError("Invalid calibration request")
                if action == "acquire":
                    if self.active:
                        self._reply(socket, request_id, False, "Another calibration session is active.")
                        continue
                    self.owner, self.token = socket, token
                    self.lease.start()
                    self.changed(True)
                elif socket is not self.owner or token != self.token:
                    self._reply(socket, request_id, False, "Calibration session is not owned by this connection.")
                    continue
                if action == "renew":
                    self.lease.start()
                    self._reply(socket, request_id, True)
                    continue
                if action == "release":
                    if self.busy:
                        self._reply(socket, request_id, False, "A device operation is still running.")
                    else:
                        self._expire()
                        self._reply(socket, request_id, True)
                    continue
                if self.busy:
                    self._reply(socket, request_id, False, "A device operation is still running.")
                    continue
                self.busy = True
                def done(ok, error="", socket=socket, request_id=request_id, token=token, action=action):
                    if socket is self.owner and token == self.token:
                        self.busy = False
                        self._reply(socket, request_id, ok, error)
                        if action == "acquire" and not ok:
                            self._expire()
                self.perform(action, message.get("args", {}), done)
        except (ValueError, UnicodeError, KeyError):
            socket.abort()

    def publish(self):
        state = self.snapshot()
        for socket in list(self.sockets):
            self._send(socket, state)

    def _expire(self):
        was_active = self.active
        self.owner = self.token = None
        self.busy = False
        self.lease.stop()
        if was_active:
            self.changed(False)

    def _lost(self, socket):
        if socket is self.owner:
            self._expire()
        if socket in self.sockets:
            del self.sockets[socket]
            socket.deleteLater()

    def close(self):
        self._expire()
        for socket in list(self.sockets):
            socket.abort()
        self.server.close()
        self.lock.unlock()


# ----------------------------
# Application constants
# ----------------------------


APP_NAME = "Yamaha Remote"


__version__ = "2026.10.20.1"


CONFIG_PATH = Path(__file__).resolve().with_name("YamahaRemote.json")


NETWORK_TIMEOUT_SECONDS = 2.0


POLL_INTERVAL_MS = 2000
POPUP_ANIMATION_DURATION_MS = 200
SPEAKER_STATUS_GRACE_SECONDS = 3.0
WINDOW_WIDTH = 303  # 305 including the 2 px border on the reference Windows setup.


CONFIG_RESTART_DEBOUNCE_MS = 200
HOTKEY_STATE_MAX_AGE_SECONDS = 6.0
MAX_PENDING_HOTKEYS = 8
VOLUME_BATCH_MAX_COMMANDS = 4
VOLUME_BATCH_MAX_AGE_SECONDS = 1.0


# ----------------------------
# Configuration
# ----------------------------


DEFAULT_CONFIG = {
    "Host": "",
    "DarkMode": True,
    "Mica": False,
    "VolumeDb": False,
    "VolumeDbAtZero": -97.0,
    "VolumeDbStepSize": 0.5,
    "GlobalVolumeHotkeys": False,
    "DiagnosticsEnabled": False,
    "SortByName": False,
    "DropdownAnimationWorkaround": sys.platform == "win32",
    "Inputs": {},
    "SystemTray": False,
    "CloseToTray": False,
}


BOOLEAN_CONFIG_KEYS = (
    "GlobalVolumeHotkeys", "DiagnosticsEnabled", "SortByName", "DarkMode", "Mica",
    "SystemTray", "CloseToTray", "VolumeDb", "DropdownAnimationWorkaround",
)


class ConfigError(ValueError):
    pass


def _require_bool(config, key, default):
    value = config.get(key, default)

    if not isinstance(value, bool):
        raise ConfigError(
            f'"{key}" must be true or false.'
        )

    return value


def _validate_number(value, key):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f'"{key}" must be a number.'
        )

    value = float(value)

    if not math.isfinite(value):
        raise ConfigError(
            f'"{key}" must be a finite number.'
        )

    return value


def _validate_host(value):
    if not isinstance(value, str):
        raise ConfigError(
            '"Host" must be a string.'
        )

    host = value.strip()

    if not host:
        return host

    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    if len(host) > 253:
        raise ConfigError(
            '"Host" is too long.'
        )

    labels = host.rstrip(".").split(".")
    if all(label.isdigit() for label in labels):
        raise ConfigError(
            '"Host" looks like an IP address but is not a valid one.'
        )

    hostname_label = re.compile(
        r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
    )

    if not labels or any(
        not hostname_label.fullmatch(label)
        for label in labels
    ):
        raise ConfigError(
            '"Host" must be a valid IP address or hostname.'
        )

    return host


def _validate_entries(value, collection):
    if not isinstance(value, dict):
        raise ConfigError(
            f'"{collection}" must be an object.'
        )

    normalized = {}

    for input_id, settings in value.items():
        if not isinstance(input_id, str) or not input_id.strip():
            raise ConfigError(
                f'"{collection}" IDs must be non-empty strings.'
            )

        input_id = input_id.strip()

        if not isinstance(settings, dict):
            raise ConfigError(
                f'"{collection}" entry {input_id!r} must be an object.'
            )

        unknown_keys = set(settings) - {
            "Name",
            "NameOverride",
            "Hidden",
            "Favorite",
        }

        if unknown_keys:
            names = ", ".join(sorted(unknown_keys))
            raise ConfigError(
                f'"{collection}" entry {input_id!r} has unknown '
                f'field(s): {names}.'
            )

        name = settings.get("Name", input_id)
        name_override = settings.get("NameOverride", "")
        hidden = settings.get("Hidden", False)
        favorite = settings.get("Favorite", False)

        if not isinstance(name, str) or not name.strip():
            raise ConfigError(
                f'"{collection}" entry {input_id!r} must have a '
                'non-empty string "Name".'
            )

        if not isinstance(name_override, str):
            raise ConfigError(
                f'"{collection}" entry {input_id!r} "NameOverride" '
                'must be a string.'
            )

        if not isinstance(hidden, bool):
            raise ConfigError(
                f'"{collection}" entry {input_id!r} "Hidden" '
                'must be true or false.'
            )

        if not isinstance(favorite, bool):
            raise ConfigError(
                f'"{collection}" entry {input_id!r} "Favorite" '
                'must be true or false.'
            )

        normalized[input_id] = {
            "Name": name.strip(),
            "NameOverride": name_override.strip(),
            "Hidden": hidden,
            "Favorite": favorite,
        }

    return normalized


def _entry_display_name(settings):
    return settings["NameOverride"] or settings["Name"]


def _ordered_entries(entries, sort_by_name):
    items = list(entries.items())
    if sort_by_name:
        items.sort(key=lambda item: _entry_display_name(item[1]).casefold())
    return items


def _default_config_copy():
    return {
        key: value.copy() if isinstance(value, (dict, list)) else value
        for key, value in DEFAULT_CONFIG.items()
    }


def missing_config_settings(config):
    return ["Host"] if not config["Host"] else []


def show_missing_config_settings(config):
    missing = missing_config_settings(config)
    if missing:
        path = CONFIG_PATH
        QMessageBox.information(
            None, "Yamaha Remote - Configuration Required",
            f"Add the following required settings in {path}: "
            + ", ".join(missing)
            + ".\nDevice communication is disabled until you save a valid configuration.",
        )


def validate_config(raw_config, *, allow_incomplete=False):
    if not isinstance(raw_config, dict):
        raise ConfigError(
            "The configuration root must be a JSON object."
        )

    unknown = set(raw_config) - set(DEFAULT_CONFIG)
    if unknown:
        raise ConfigError("Unknown configuration setting(s): " + ", ".join(sorted(unknown)) + ".")

    config = _default_config_copy()
    config.update(raw_config)

    config["Host"] = _validate_host(config["Host"])
    for key in BOOLEAN_CONFIG_KEYS:
        config[key] = _require_bool(config, key, DEFAULT_CONFIG[key])

    for key in ("VolumeDbAtZero", "VolumeDbStepSize"):
        if config["VolumeDb"] and key not in raw_config:
            raise ConfigError(f'"{key}" is required when "VolumeDb" is true.')
        config[key] = _validate_number(config[key], key)
    if config["VolumeDbStepSize"] <= 0:
        raise ConfigError('"VolumeDbStepSize" must be greater than 0.')

    if config["CloseToTray"] and not config["SystemTray"]:
        raise ConfigError(
            '"CloseToTray" can only be true when '
            '"SystemTray" is also true.'
        )

    config["Inputs"] = _validate_entries(config.get("Inputs", {}), "Inputs")

    if not allow_incomplete and missing_config_settings(config):
        raise ConfigError('"Host" must not be empty.')

    return config


def load_config():
    config_path = CONFIG_PATH

    if not config_path.exists():
        config = _default_config_copy()
        save_config(config)
        return validate_config(config, allow_incomplete=True)

    try:
        with config_path.open(
            "r",
            encoding="utf-8",
        ) as f:
            raw_config = json.load(f)
    except json.JSONDecodeError as ex:
        raise ConfigError(
            "Invalid JSON syntax in "
            f"{config_path.name}: line {ex.lineno}, "
            f"column {ex.colno}: {ex.msg}"
        ) from ex
    except UnicodeError as ex:
        raise ConfigError(f"{config_path.name} must use UTF-8 encoding.") from ex
    except OSError as ex:
        raise ConfigError(
            f"Unable to read {config_path.name}: {ex}"
        ) from ex

    return validate_config(raw_config, allow_incomplete=True)


def save_config(config, *, expected_digest=None):
    content = (json.dumps(config, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    temp_path = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    try:
        temp_path.write_bytes(content)
        # Discovery must not replace unapplied edits, including invalid JSON.
        # Check after preparing the replacement to minimize the edit/save race.
        if expected_digest is not None and _file_sha256(CONFIG_PATH) != expected_digest:
            return None
        os.replace(temp_path, CONFIG_PATH)
        return hashlib.sha256(content).digest()
    except OSError as ex:
        raise ConfigError(f"Unable to update {CONFIG_PATH.name}: {ex}") from ex
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _file_snapshot(path):
    try:
        content = path.read_bytes()
    except OSError:
        return None, None
    return content, hashlib.sha256(content).digest()


def _file_sha256(path):
    return _file_snapshot(path)[1]


# ----------------------------
# Platform integration
# ----------------------------


class TooltipDelayStyle(QProxyStyle):
    def styleHint(self, hint, option=None, widget=None, returnData=None):
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            return 1000
        if hint == QStyle.SH_ToolTip_FallAsleepDelay:
            # Moving between controls must still require a full hover delay.
            return 0
        return super().styleHint(hint, option, widget, returnData)





_instance_lock = None


class _WindowsSingleInstanceMutex:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name):
        self.handle = None
        self.kernel32 = None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_bool,
            ctypes.c_wchar_p,
        ]
        kernel32.CreateMutexW.restype = ctypes.c_void_p

        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_bool

        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(
            None,
            False,
            name,
        )

        error = ctypes.get_last_error()

        if not handle:
            raise OSError(
                error,
                "CreateMutexW failed.",
            )

        if error == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            self.handle = None
            self.kernel32 = None
            return

        self.handle = handle
        self.kernel32 = kernel32

    @property
    def acquired(self):
        return self.handle is not None

    def close(self):
        if self.handle is not None:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None

    def __del__(self):
        self.close()


def _create_single_instance_lock():
    if sys.platform == "win32":
        mutex = _WindowsSingleInstanceMutex(
            "YamahaRemote.SingleInstance"
        )

        if not mutex.acquired:
            return None

        return mutex

    temp_dir = QStandardPaths.writableLocation(
        QStandardPaths.TempLocation
    )

    lock_path = str(
        Path(temp_dir) / "YamahaRemote.lock"
    )

    lock = QLockFile(lock_path)

    # Disable age-based stale detection for this long-lived lock. QLockFile
    # can still recover a lock whose owning process no longer exists.
    lock.setStaleLockTime(0)

    if not lock.tryLock(0):
        if lock.error() != QLockFile.LockFailedError:
            raise OSError("Could not create the single-instance lock file.")
        return None

    return lock


def acquire_single_instance():
    global _instance_lock
    lock = _create_single_instance_lock()
    if lock is None:
        return False
    _instance_lock = lock
    return True


def release_single_instance():
    global _instance_lock
    if isinstance(_instance_lock, _WindowsSingleInstanceMutex):
        _instance_lock.close()
    elif isinstance(_instance_lock, QLockFile):
        _instance_lock.unlock()
    _instance_lock = None


def set_app_user_model_id(app_id):
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


# Windows Mica integration inspired by win32mica (MIT); see THIRD_PARTY_LICENSES.txt.


class _MicaMargins(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


def supports_mica():
    return (
        sys.platform == "win32"
        and sys.getwindowsversion().build >= 22000
    )


def apply_mica(hwnd):
    """Apply the Windows 11 Mica backdrop to a native window handle."""
    if not supports_mica() or not hwnd:
        return False

    try:
        hwnd = int(hwnd)
        dwm = ctypes.windll.dwmapi

        # Extend DWM composition over the full client area.
        margins = _MicaMargins(-1, -1, -1, -1)
        dwm.DwmExtendFrameIntoClientArea(
            ctypes.c_void_p(hwnd),
            ctypes.byref(margins),
        )

        build = sys.getwindowsversion().build

        if build < 22523:
            # Legacy Windows 11 fallback used before
            # DWMWA_SYSTEMBACKDROP_TYPE was available.
            attribute = 1029
            value = ctypes.c_int(1)
        else:
            # DWMWA_SYSTEMBACKDROP_TYPE = 38
            # DWMSBT_MAINWINDOW = 2 (default Mica)
            attribute = 38
            value = ctypes.c_int(2)

        result = dwm.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(attribute),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )

        return result == 0
    except Exception:
        return False


def set_dark_titlebar(hwnd):
    """Use a dark native Windows title bar when supported."""
    if sys.platform != "win32":
        return

    try:
        value = ctypes.c_int(1)

        # Windows 10 20H1+ / Windows 11.
        result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(20),
            ctypes.byref(value),
            ctypes.sizeof(value),
        )

        if result != 0:
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(19),
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
    except Exception:
        pass


def get_app_icon():
    if sys.platform != "win32":
        return QIcon()

    try:
        windir = Path(
            os.environ.get("WINDIR", r"C:\Windows")
        )
        volume_mixer = windir / "System32" / "sndvol.exe"

        if volume_mixer.exists():
            provider = QFileIconProvider()
            return provider.icon(QFileInfo(str(volume_mixer)))
    except Exception:
        pass

    return QIcon()


# Only these three media keys belong to the amplifier remote.
_VOLUME_KEYS = {0xAF: "volume_up", 0xAE: "volume_down", 0xAD: "mute"}
_RETAINED_VOLUME_HOOKS = []
_QT_VOLUME_KEYS = {
    Qt.Key_VolumeUp: "volume_up",
    Qt.Key_VolumeDown: "volume_down",
    Qt.Key_VolumeMute: "mute",
}


class _VolumeKeyState:
    """Keep a physical press with its original owner until release."""

    def __init__(self):
        self.pressed = {}

    def press(self, key, command, accept):
        if key in self.pressed:
            owned = self.pressed[key]
            if owned and command != "mute":
                accept(command)
            return owned
        owned = bool(accept(command))
        self.pressed[key] = owned
        return owned

    def release(self, key):
        return self.pressed.pop(key, False)


class _LowLevelKey(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_uint32), ("scanCode", ctypes.c_uint32),
        ("flags", ctypes.c_uint32), ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _NativePoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class _NativeMessage(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p), ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32), ("pt", _NativePoint),
        ("lPrivate", ctypes.c_uint32),
    ]


_HookProc = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t
)


def _load_volume_hook_api():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = (
        (user32.SetWindowsHookExW, [ctypes.c_int, _HookProc, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_void_p),
        (user32.UnhookWindowsHookEx, [ctypes.c_void_p], ctypes.c_int),
        (user32.CallNextHookEx, [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_ssize_t),
        (user32.GetForegroundWindow, [], ctypes.c_void_p),
        (user32.GetAsyncKeyState, [ctypes.c_int], ctypes.c_int16),
        (user32.PeekMessageW, [ctypes.POINTER(_NativeMessage), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int),
        (user32.GetMessageW, [ctypes.POINTER(_NativeMessage), ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int),
        (user32.PostThreadMessageW, [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t], ctypes.c_int),
        (kernel32.GetModuleHandleW, [ctypes.c_wchar_p], ctypes.c_void_p),
        (kernel32.GetCurrentThreadId, [], ctypes.c_uint32),
    )
    for function, arguments, result in signatures:
        function.argtypes = arguments
        function.restype = result
    return user32, kernel32


class _WindowsVolumeHook(QObject):
    changed = Signal(bool, object)

    def __init__(self, accept, parent=None):
        super().__init__(parent)
        self._accept = accept
        self._keys = _VolumeKeyState()
        self._hook = None
        self._callback = None
        self._user32 = None
        self._thread_id = None
        self._thread = None
        self._stopping = threading.Event()
        self._faulted = False

    @property
    def installed(self):
        return bool(self._hook and not self._faulted and not self._stopping.is_set())

    def start(self):
        # A separate native message loop keeps the hook responsive even while
        # the GUI waits for workers during restart/shutdown. No Qt UI calls or
        # network/file operations run inside the native callback.
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="VolumeHotkeys", daemon=True)
        self._thread.start()

    def _notify(self, installed, error=None):
        try:
            self.changed.emit(installed, error)
        except RuntimeError:
            pass  # The Qt parent may already be closing.

    def _run(self):
        error = None
        try:
            self._user32, kernel32 = _load_volume_hook_api()
            message = _NativeMessage()
            self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self._thread_id = kernel32.GetCurrentThreadId()
            # stop() can arrive before the native thread has finished startup.
            if self._stopping.is_set():
                return
            # Do not seize keys that were already held when the app started.
            self._keys.pressed = {
                key: False for key in _VOLUME_KEYS
                if self._user32.GetAsyncKeyState(key) & 0x8000
            }
            self._callback = _HookProc(self._handle_key)
            self._hook = self._user32.SetWindowsHookExW(
                13, self._callback, kernel32.GetModuleHandleW(None), 0
            )
            if not self._hook:
                raise ctypes.WinError()
            if not self._stopping.is_set():
                self._notify(True)
            while not self._stopping.is_set():
                result = self._user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError()
        except Exception as exc:
            error = exc
            self._faulted = True
        finally:
            if self._hook:
                try:
                    if not self._user32.UnhookWindowsHookEx(self._hook):
                        raise ctypes.WinError()
                    self._hook = None
                except Exception as exc:
                    error = error or exc
                    self._faulted = True
            # Keep the C callback alive if native unregistration failed.
            if self._hook is None:
                self._callback = None
            elif self not in _RETAINED_VOLUME_HOOKS:
                _RETAINED_VOLUME_HOOKS.append(self)
            self._thread_id = None
            self._notify(False, error)

    def _handle_key(self, code, message, address):
        key = None
        if code == 0 and message in (0x100, 0x101, 0x104, 0x105):
            try:
                data = ctypes.cast(address, ctypes.POINTER(_LowLevelKey)).contents
                key = data.vkCode
                command = _VOLUME_KEYS.get(key)
                # Remappers can generate injected volume-key events. They are
                # safe to handle too: this remote never synthesizes key events.
                if command:
                    if message in (0x101, 0x105):
                        owned = self._keys.release(data.vkCode)
                    else:
                        foreground = self._user32.GetForegroundWindow()
                        owned = self._keys.press(
                            data.vkCode, command,
                            lambda value: (
                                not self._faulted and not self._stopping.is_set()
                                and self._accept(value, foreground)
                            ),
                        )
                    if owned:
                        return 1
            except Exception as error:
                if not self._faulted:
                    self._faulted = True
                    self._notify(False, error)
                if self._keys.pressed.get(key, False):
                    return 1
        try:
            return self._user32.CallNextHookEx(self._hook, code, message, address)
        except Exception as error:
            if not self._faulted:
                self._faulted = True
                self._notify(False, error)
            return 0  # Never let a Python exception escape a native callback.

    def stop(self):
        self._stopping.set()
        if self._thread is not None and self._thread.is_alive() and self._thread_id and self._user32:
            try:
                if not self._user32.PostThreadMessageW(self._thread_id, 0x12, 0, 0):  # WM_QUIT
                    raise ctypes.WinError()
            except Exception as error:
                record_exception(error, "Hotkey shutdown")
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(timeout=2)
        if self._thread is not None and self._thread.is_alive():
            # Never free a live C callback. Disable interception even if the
            # native message thread cannot be stopped normally.
            try:
                if self._hook and self._user32.UnhookWindowsHookEx(self._hook):
                    self._hook = None
            except Exception as error:
                record_exception(error, "Hotkey shutdown")
            if self._hook and self not in _RETAINED_VOLUME_HOOKS:
                _RETAINED_VOLUME_HOOKS.append(self)
            record_exception(RuntimeError("Volume hotkey thread did not stop."), "Hotkey shutdown")


# ----------------------------
# Request errors and optional diagnostics
# ----------------------------


class DeviceConnectionError(RuntimeError):
    """The device could not be reached."""


class DeviceResponseError(RuntimeError):
    """The device rejected a request or returned an invalid response."""


_diagnostics = None


class Diagnostics:
    """Optional, best-effort logs without messages, source lines, or locals."""

    def __init__(self):
        self._stream = None
        self._lock = threading.RLock()
        self._seen = set()
        self._owns_fault_handler = False
        self._old_sys_hook = sys.excepthook
        self._old_thread_hook = threading.excepthook
        self._sys_hook = self._exception_hook
        self._thread_hook = self._thread_exception_hook
        try:
            self._stream = Path(__file__).with_name(
                Path(__file__).stem + "-crash.log"
            ).open("a", encoding="utf-8")
        except OSError:
            return
        sys.excepthook = self._sys_hook
        threading.excepthook = self._thread_hook
        try:
            if not faulthandler.is_enabled():
                faulthandler.enable(self._stream)
                self._owns_fault_handler = True
        except (OSError, RuntimeError, ValueError):
            pass

    def _format_exception(self, error, context):
        # Never stringify exceptions: messages and chained causes may contain
        # credentials, request payloads, or text entered by the user.
        lines = [f"{context}: {type(error).__name__}"]
        visited = set()
        while error is not None and id(error) not in visited:
            visited.add(id(error))
            lines.append(f"Exception type: {type(error).__name__}")
            for frame, line in traceback.walk_tb(error.__traceback__):
                lines.append(
                    f"  {Path(frame.f_code.co_filename).name}:{line} in {frame.f_code.co_name}"
                )
            error = error.__cause__ or (
                None if error.__suppress_context__ else error.__context__
            )
        return "\n".join(lines) + "\n"

    def record(self, error, context, *, deduplicate=False):
        with self._lock:
            if self._stream is None:
                return
            text = self._format_exception(error, context)
            if deduplicate and text in self._seen:
                return
            if deduplicate:
                self._seen.add(text)
            try:
                timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
                self._stream.write(f"\n[{timestamp}] {Path(__file__).stem} {__version__}\n{text}")
                self._stream.flush()
            except (OSError, ValueError):
                self.close()

    def _exception_hook(self, exc_type, error, tb):
        self.record(error, "Unhandled exception")
        try:
            sys.stderr.write(self._format_exception(error, "Unhandled exception"))
        except (OSError, ValueError, AttributeError):
            pass

    def _thread_exception_hook(self, args):
        self._exception_hook(args.exc_type, args.exc_value, args.exc_traceback)

    def close(self):
        with self._lock:
            if sys.excepthook is self._sys_hook:
                sys.excepthook = self._old_sys_hook
            if threading.excepthook is self._thread_hook:
                threading.excepthook = self._old_thread_hook
            if self._owns_fault_handler:
                try:
                    faulthandler.disable()
                except (OSError, RuntimeError, ValueError):
                    pass
                self._owns_fault_handler = False
            stream, self._stream = self._stream, None
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


def configure_diagnostics(config):
    global _diagnostics
    close_diagnostics()
    if config["DiagnosticsEnabled"]:
        _diagnostics = Diagnostics()


def close_diagnostics():
    global _diagnostics
    if _diagnostics is not None:
        _diagnostics.close()
        _diagnostics = None


def record_exception(error, context, *, deduplicate=False):
    if _diagnostics is not None:
        _diagnostics.record(error, context, deduplicate=deduplicate)


def record_unexpected_exception(error, context, *, background=False):
    if not isinstance(error, (DeviceConnectionError, DeviceResponseError)):
        record_exception(error, context, deduplicate=background)


# ----------------------------
# Device API
# ----------------------------


class YamahaConnectionError(DeviceConnectionError):
    """Raised when the Yamaha device cannot be reached."""


def _decimal_places(value):
    exponent = Decimal(str(value)).normalize().as_tuple().exponent
    return max(0, -exponent)


class YamahaApi:
    def __init__(self, host, config=None):
        self.config = config or {}

        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            url_host = host
        else:
            url_host = (
                f"[{host.replace('%', '%25')}]"
                if address.version == 6
                else host
            )

        self.base_url = (
            f"http://{url_host}/YamahaExtendedControl/v1"
        )

        if self.config.get("VolumeDb", False):
            self.volume_db_decimals = max(
                _decimal_places(self.config["VolumeDbAtZero"]),
                _decimal_places(self.config["VolumeDbStepSize"]),
            )
        else:
            self.volume_db_decimals = 0

    def _get_json(self, path, timeout=NETWORK_TIMEOUT_SECONDS):
        try:
            with DEVICE_HTTP_OPENER.open(
                f"{self.base_url}/{path}",
                timeout=timeout,
            ) as response:
                data = json.loads(
                    response.read().decode("utf-8")
                )
        except HTTPError as ex:
            raise DeviceResponseError(f"Yamaha returned HTTP {ex.code}.") from ex
        except (URLError, TimeoutError, OSError) as ex:
            raise YamahaConnectionError(
                "Unable to reach the Yamaha."
            ) from ex
        except (UnicodeError, json.JSONDecodeError) as ex:
            raise DeviceResponseError("Yamaha returned invalid JSON.") from ex

        if not isinstance(data, dict):
            raise DeviceResponseError("Yamaha response must be a JSON object.")
        response_code = data.get("response_code")
        if isinstance(response_code, bool) or not isinstance(response_code, int):
            raise DeviceResponseError("Yamaha response has a missing or invalid response code.")
        if response_code != 0:
            raise DeviceResponseError(f"Yamaha returned response code {response_code}.")

        return data

    def get_status(self, timeout=NETWORK_TIMEOUT_SECONDS):
        return self._get_json(
            "main/getStatus",
            timeout=timeout,
        )

    def format_volume(self, volume):
        if volume is None:
            return "-"

        if not self.config.get("VolumeDb", False):
            return str(volume)

        try:
            raw_volume = float(volume)
            db_at_zero = self.config["VolumeDbAtZero"]
            step_size = self.config["VolumeDbStepSize"]
            db_value = db_at_zero + (raw_volume * step_size)
        except (TypeError, ValueError, KeyError):
            return str(volume)

        return f"{db_value:.{self.volume_db_decimals}f} dB"

    def set_power(self, on):
        state = "on" if on else "standby"
        self._get_json(
            f"main/setPower?power={state}"
        )

    def set_mute(self, enabled):
        value = "true" if enabled else "false"
        self._get_json(
            f"main/setMute?enable={value}"
        )

    def volume_up(self):
        self._get_json(
            "main/setVolume?volume=up"
        )

    def volume_down(self):
        self._get_json(
            "main/setVolume?volume=down"
        )

    def set_input(self, input_id):
        status = self.get_status()

        if status.get("power") == "standby":
            self.set_power(True)

            if not self.wait_until_powered_on():
                raise YamahaConnectionError(
                    "The Yamaha did not finish "
                    "powering on."
                )

        self._get_json(
            "main/setInput?input="
            + quote(input_id, safe="")
        )

    def get_speaker_outputs(self):
        features = self._get_json("system/getFeatures")
        system = features.get("system", {})
        functions = system.get("func_list", []) if isinstance(system, dict) else None
        if not isinstance(functions, list):
            raise DeviceResponseError("Yamaha returned invalid speaker capabilities.")
        return tuple(name for name in ("a", "b") if f"speaker_{name}" in functions)

    def get_speaker_state(self, outputs):
        status = self._get_json("system/getFuncStatus")
        state = []
        for name in ("a", "b"):
            if name not in outputs:
                state.append(False)
            elif type(status.get(f"speaker_{name}")) is bool:
                state.append(status[f"speaker_{name}"])
            else:
                raise DeviceResponseError(f"Yamaha did not report a valid Speaker {name.upper()} state.")
        return tuple(state)

    def set_speakers(self, target, outputs):
        if (not outputs or not isinstance(target, (tuple, list)) or len(target) != 2
                or any(type(value) is not bool for value in target)
                or any(enabled and name not in outputs for name, enabled in zip(("a", "b"), target))):
            raise DeviceResponseError("The selected speaker combination is unavailable.")
        target = tuple(target)
        current = self.get_speaker_state(outputs)
        # Turn unwanted outputs off and confirm that before enabling others.
        for enabled in (False, True):
            for index, name in enumerate(("a", "b")):
                if name not in outputs or target[index] != enabled or current[index] == enabled:
                    continue
                self._get_json(f"system/setSpeaker{name.upper()}?enable={str(enabled).lower()}")
                expected = list(current)
                expected[index] = enabled
                deadline = time.monotonic() + SPEAKER_STATUS_GRACE_SECONDS
                while True:
                    current = self.get_speaker_state(outputs)
                    if current == tuple(expected):
                        break
                    if time.monotonic() >= deadline:
                        raise DeviceResponseError("The amplifier did not confirm the selected speakers in time.")
                    time.sleep(0.10)
        return current

    def get_inputs(self):
        features = self._get_json(
            "system/getFeatures"
        )

        names = {}

        try:
            name_text = self._get_json(
                "system/getNameText"
            )

            for item in name_text.get(
                "input_list", []
            ):
                input_id = str(
                    item.get("id", "")
                ).strip()
                text = str(
                    item.get("text", "")
                ).strip()

                if input_id and text:
                    names[input_id] = text
        except Exception as ex:
            record_unexpected_exception(ex, "Optional input names", background=True)

        inputs = []

        for item in features.get(
            "system", {}
        ).get("input_list", []):
            input_id = str(
                item.get("id", "")
            ).strip()

            if not input_id:
                continue

            label = names.get(input_id)

            if not label:
                label = (
                    input_id
                    .replace("_", " ")
                    .title()
                )

            inputs.append(
                {
                    "Id": input_id,
                    "Label": label,
                }
            )

        return inputs

    def wait_until_powered_on(self, timeout=5):
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()

            try:
                status = self.get_status(
                    timeout=max(0.1, min(0.5, remaining))
                )
                if status.get("power") == "on":
                    return True
            except YamahaConnectionError:
                pass

            time.sleep(0.1)

        return False


# ----------------------------
# Background workers
# ----------------------------


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(object)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.result.emit(self.function())
        except Exception as exc:
            self.signals.error.emit(exc)
        finally:
            self.signals.finished.emit()


# ----------------------------
# Specialized widgets
# ----------------------------


class DraggableLabel(QLabel):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            window = self.window()
            handle = window.windowHandle()

            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return

        super().mousePressEvent(event)


class CenteredComboBox(QComboBox):
    def __init__(self, parent=None, *, animate_popup=False):
        super().__init__(parent)
        self._animate_popup = animate_popup
        self._popup = None
        self._popup_view = None
        self._final_rect = None
        self._view_rect = None
        self._popup_layout = None
        self._layout_was_enabled = False
        self._opens_downward = True
        self._elapsed = QElapsedTimer()
        self._easing = QEasingCurve(QEasingCurve.OutCubic)
        self._popup_timer = QTimer(self)
        self._popup_timer.setTimerType(Qt.PreciseTimer)
        self._popup_timer.setInterval(8)
        self._popup_timer.timeout.connect(self._animation_tick)

    def showPopup(self):
        if not self._animate_popup:
            super().showPopup()
            return
        if self.view().window().isVisible():
            self.hidePopup()
            return

        app = QApplication.instance()
        effect = Qt.UI_AnimateCombo
        animate = app.isEffectEnabled(effect)
        # Suppress Qt's rolling window, then animate only the real popup.
        # Restore the preference immediately, including when opening fails.
        app.setEffectEnabled(effect, False)
        try:
            super().showPopup()
        finally:
            app.setEffectEnabled(effect, animate)
        if not animate:
            return  # Respect the user's/system's disabled UI animations.

        view = self.view()
        popup = view.window()
        final_rect = popup.geometry()
        if not popup.isVisible() or final_rect.height() <= 2:
            return

        # Keep Qt's final placement, including screen-edge clipping and
        # popups that overlap the combo. Never relocate it above/below by hand.
        self._opens_downward = final_rect.center().y() >= self.mapToGlobal(self.rect().center()).y()
        self._popup, self._popup_view = popup, view
        self._final_rect = QRect(final_rect)
        self._view_rect = QRect(view.geometry())
        self._popup_layout = popup.layout()
        if self._popup_layout is not None:
            self._layout_was_enabled = self._popup_layout.isEnabled()
            # Deferred layout passes must not shrink the live view between
            # animation frames and change its scrollbar geometry.
            self._popup_layout.setEnabled(False)
        popup.installEventFilter(self)
        self._elapsed.start()
        self._animation_tick()
        popup.raise_()
        self._popup_timer.start()

    def _animation_tick(self):
        if self._popup is None:
            return
        linear = min(1.0, self._elapsed.elapsed() / POPUP_ANIMATION_DURATION_MS)
        progress = self._easing.valueForProgress(linear)
        # Scroll controls can impose a taller minimum on long popups.
        minimum = min(self._final_rect.height(), max(2, self._popup.minimumHeight()))
        height = round(minimum + (self._final_rect.height() - minimum) * progress)
        rect = QRect(self._final_rect)
        rect.setHeight(height)
        if not self._opens_downward:
            rect.moveBottom(self._final_rect.bottom())
        self._popup.setGeometry(rect)
        view_rect = QRect(self._view_rect)
        if self._opens_downward:
            view_rect.translate(0, height - self._final_rect.height())
        self._popup_view.setGeometry(view_rect)
        if linear >= 1:
            self._finish_popup_animation(focus_view=True)

    def _finish_popup_animation(self, *, focus_view=False):
        self._popup_timer.stop()
        popup, view = self._popup, self._popup_view
        if popup is None:
            return
        # Clear the active marker before restoring widgets: restoration itself
        # can generate Qt show/hide/focus events.
        self._popup = None
        popup.removeEventFilter(self)
        popup.setGeometry(self._final_rect)
        view.setGeometry(self._view_rect)
        view.show()
        view.raise_()
        view.update()
        view.viewport().update()
        if self._popup_layout is not None:
            self._popup_layout.setEnabled(self._layout_was_enabled)
        self._popup_layout = None
        if focus_view and popup.isVisible():
            view.setFocus(Qt.PopupFocusReason)
        self._popup_view = None
        self._final_rect = self._view_rect = None

    def eventFilter(self, watched, event):
        if self._popup is not None:
            if watched is self._popup and event.type() in (QEvent.Hide, QEvent.Close):
                self._finish_popup_animation()
        return super().eventFilter(watched, event)

    def hidePopup(self):
        self._finish_popup_animation()
        super().hidePopup()

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)

        # Let the styled control paint its frame without a separate arrow button.
        option.subControls &= ~QStyle.SC_ComboBoxArrow
        painter.drawComplexControl(QStyle.CC_ComboBox, option)
        arrow = QStyleOptionComboBox(option)
        arrow_rect = self.style().subControlRect(
            QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxArrow, self)
        arrow.rect = QRect(0, 0, 12, 12)
        arrow.rect.moveCenter(arrow_rect.center())
        group = QPalette.Active if self.isEnabled() else QPalette.Disabled
        arrow.palette.setColor(QPalette.ButtonText, option.palette.color(group, QPalette.Text))
        QApplication.style().drawPrimitive(QStyle.PE_IndicatorArrowDown, arrow, painter, self)

        rect = self.style().subControlRect(
            QStyle.CC_ComboBox,
            option,
            QStyle.SC_ComboBoxEditField,
            self,
        )

        # Reserve equal space on both sides so the native arrow does not
        # shift the label away from the center of the whole button.
        inset = max(rect.left(), self.width() - rect.right() - 1)
        rect.setLeft(inset)
        rect.setRight(self.width() - inset - 1)

        text = self.currentText()
        if self.currentIndex() < 0:
            text = self.placeholderText()

        group = QPalette.Active if self.isEnabled() else QPalette.Disabled
        painter.setPen(option.palette.color(group, QPalette.Text))
        painter.drawText(rect, Qt.AlignCenter, text)


class CenteredComboDelegate(QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.displayAlignment = Qt.AlignCenter


# ----------------------------
# Main window
# ----------------------------


class YamahaRemote(QWidget):
    _hotkey_ready = Signal()


    # ----------------------------
    # Initialization
    # ----------------------------

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.calibration_active = False
        self._calibration_key_blocked = False
        self.amplifier_server = None
        self._calibration_operation = False
        self._status_sequence = 0
        self.calibration_server = None
        self.activation_server = None
        self.mica_enabled = bool(config["Mica"] and supports_mica())
        self.api = YamahaApi(config["Host"], config)
        self.pool = QThreadPool(self)
        # Keep receiver requests serialized, including status refreshes.
        self.pool.setMaxThreadCount(1)
        self._workers = set()
        self.poll_running = False
        self.initialized = False
        self.tray = None
        self._really_close = False
        self._shutting_down = False
        self._shutdown_complete = False
        self._invalid_config_digest = None
        self._worker_generation = 0
        self._restart_failed = False
        self._config_digest = self._get_config_digest()
        self._config_restart_pending = False

        self.connected = False
        self.action_running = False
        self._volume_batch_count = 0
        self.last_status = None
        self.inputs = []
        self._speaker_outputs = None
        self._speaker_discovery_running = False
        self._speaker_state = None
        self._speaker_target = None
        self._speaker_grace_deadline = None
        self.input_settings = config["Inputs"]
        self.favorite_buttons = {}
        self.control_widgets = []
        self._polling_initialized = False
        self._last_status_at = None
        self._hotkey_lock = threading.Lock()
        self._pending_hotkeys = deque()
        self._hotkey_drain_scheduled = False
        self._hotkey_state_refresh_pending = False
        self._hotkey_state = (False, False, 0, None, False)
        self._qt_volume_keys = _VolumeKeyState()
        self._native_volume_hook = None

        self._build_ui()
        self._apply_style()
        self._fit_and_lock_window()
        self._setup_timers()
        self._setup_config_watcher()
        self._setup_tray()
        self._setup_hotkeys()
        self._setup_ipc()
        self._setup_calibration()
        self._setup_activation()
        self.set_controls_enabled(False)
        QTimer.singleShot(0, self.initialize)

    def _setup_calibration(self):
        if self.calibration_server is not None:
            self.calibration_server.deleteLater()
        self.calibration_server = CalibrationServer(
            "amplifier", self._calibration_snapshot, self._calibration_changed,
            self._calibration_perform, self,
        )

    def _calibration_snapshot(self):
        state = self._amplifier_snapshot()
        state.update(sample_id=self._status_sequence, role="amplifier")
        return state

    def _calibration_changed(self, active):
        self._volume_batch_count = 0
        self.calibration_active = active
        self.setEnabled(not active)
        self.poll_timer.setInterval(250 if active else POLL_INTERVAL_MS)
        with self._hotkey_lock:
            self._calibration_key_blocked = active
            self._pending_hotkeys.clear()
        self._publish_hotkey_state()
        self._update_polling_state(refresh=True)

    def _calibration_perform(self, action, args, done):
        if self._shutting_down or missing_config_settings(self.config):
            done(False, "Device configuration is missing or the app is shutting down.")
            return
        if action != "acquire":
            done(False, "Unsupported calibration operation.")
            return
        if (sys.platform == "win32"
                and (self._native_volume_hook is None or not self._native_volume_hook.installed)):
            done(False, "The amplifier's native volume hook must be available for calibration.")
            return
        self._calibration_operation = True
        token = self.calibration_server.token
        deadline = time.monotonic() + 5
        def finish(ok, error=""):
            self._calibration_operation = False
            done(ok, error)
            self._update_polling_state(refresh=True)
        def ready():
            if self._shutting_down or self.calibration_server.token != token:
                finish(False, "Calibration session ended.")
                return
            if self._workers or self.pool.activeThreadCount():
                if time.monotonic() >= deadline:
                    finish(False, "Device requests did not finish in time.")
                else:
                    QTimer.singleShot(25, ready)
                return
            finish(True)
        ready()

    def _setup_timers(self):
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(POLL_INTERVAL_MS)
        self.poll_timer.timeout.connect(
            self.poll_status
        )

        self.startup_status_timer = QTimer(self)
        self.startup_status_timer.setSingleShot(True)
        self.startup_status_timer.setInterval(500)
        self.startup_status_timer.timeout.connect(
            self.show_connecting_status
        )

    def _setup_hotkeys(self):
        app = QApplication.instance()
        app.installEventFilter(self)
        self._hotkey_ready.connect(self._drain_hotkeys, Qt.QueuedConnection)
        if sys.platform == "win32":
            self._native_volume_hook = _WindowsVolumeHook(self._accept_native_hotkey, self)
            self._native_volume_hook.changed.connect(self._native_hotkey_changed)
            self._publish_hotkey_state()
            try:
                self._native_volume_hook.start()
            except Exception as error:
                QTimer.singleShot(0, lambda error=error: self._native_hotkey_changed(False, error))
        elif self.config["GlobalVolumeHotkeys"]:
            QTimer.singleShot(0, lambda: self.show_error(
                "Global volume hotkeys are supported on Windows only. "
                "Volume keys still work while this window is focused."
            ))


    # ----------------------------
    # UI construction and styling
    # ----------------------------



    def _build_ui(self):
        self.setWindowTitle(APP_NAME)
        self.setObjectName("remoteRoot")
        self.setWindowIcon(QApplication.windowIcon())
        self.setFixedWidth(WINDOW_WIDTH)
        if self.mica_enabled:
            self.setAttribute(Qt.WA_TranslucentBackground)
        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(12, 12, 12, 12)
        self.main_layout.setSpacing(9)
        self.power_button = self._new_button("Standby")
        self.power_button.setMinimumHeight(38)
        self.power_button.clicked.connect(self.toggle_power)
        self.main_layout.addWidget(self.power_button)
        self.control_widgets.append(self.power_button)
        self._build_input_controls()
        self._build_favorites_from_config()
        self._build_volume_controls()
        self._fit_and_lock_window()

    def _section(self, title):
        frame = QFrame()
        frame.setObjectName("section")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(9, 8, 9, 9)
        layout.setSpacing(6)
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        self.main_layout.addWidget(frame)
        return layout

    def _build_volume_controls(self):
        layout = self._section("Volume")
        self.volume_value = DraggableLabel("—", self)
        self.volume_value.setObjectName("volumeValue")
        self.volume_value.setAlignment(Qt.AlignCenter)
        volume_font = self.volume_value.font()
        volume_font.setPointSizeF(13)
        volume_font.setWeight(QFont.DemiBold)
        self.volume_value.setFont(volume_font)
        self.volume_down = self._new_button("−")
        self.volume_up = self._new_button("+")
        for button, direction in ((self.volume_down, -1), (self.volume_up, 1)):
            font = button.font()
            font.setPointSizeF(9)
            button.setFont(font)
            button.setToolTip("Shortcut: Volume " + ("Down" if direction < 0 else "Up"))
            button.setAutoRepeat(True)
            button.setAutoRepeatDelay(500)
            button.setAutoRepeatInterval(100)
            button.pressed.connect(lambda direction=direction: self.adjust_volume(direction))
        volume_row = QGridLayout()
        volume_row.setSpacing(6)
        for column in range(3):
            volume_row.setColumnStretch(column, 1)
        for column, widget in enumerate((self.volume_down, self.volume_value, self.volume_up)):
            volume_row.addWidget(widget, 0, column)
        layout.addLayout(volume_row)
        self.mute_button = self._new_button("Mute")
        self.mute_button.setToolTip("Shortcut: Volume Mute")
        self.mute_button.clicked.connect(self.toggle_mute)
        self.speaker_combo = self._new_combo()
        self.speaker_combo.setPlaceholderText("Speakers: unknown")
        self.speaker_combo.setToolTip("Speaker outputs")
        self.speaker_combo.activated.connect(self.select_speakers)
        row = QGridLayout()
        row.setSpacing(6)
        row.setColumnStretch(0, 1)
        row.setColumnStretch(1, 2)
        row.addWidget(self.mute_button, 0, 0)
        row.addWidget(self.speaker_combo, 0, 1)
        layout.addLayout(row)
        self.control_widgets.extend((self.volume_down, self.volume_up, self.mute_button, self.speaker_combo))

    def _build_favorites_from_config(self):
        buttons = dict(self.favorite_buttons)
        for button in buttons.values():
            self.input_favorites_layout.removeWidget(button)
        self.favorite_buttons.clear()
        favorites = [(input_id, settings) for input_id, settings in
                     _ordered_entries(self.input_settings, self.config["SortByName"]) if settings["Favorite"]]
        for index, (input_id, settings) in enumerate(favorites):
            button = buttons.pop(input_id, None)
            if button is None:
                button = self._new_button("")
                button.clicked.connect(lambda checked=False, input_id=input_id: self.set_input(input_id))
                self.control_widgets.append(button)
            button.setText(_entry_display_name(settings))
            self.favorite_buttons[input_id] = button
            row, column = divmod(index, 2)
            self.input_favorites_layout.addWidget(button, row, column)
            button.show()
        for button in buttons.values():
            self.control_widgets.remove(button)
            button.hide()
            button.deleteLater()

    def _new_combo(self):
        combo = CenteredComboBox(animate_popup=self.config["DropdownAnimationWorkaround"])
        combo.setItemDelegate(CenteredComboDelegate(combo))
        combo.setMinimumHeight(34)
        return combo

    def _build_input_controls(self):
        layout = self._section("Input")
        self.input_combo = self._new_combo()
        self.input_combo.activated.connect(self.select_input)
        layout.addWidget(self.input_combo)
        self.input_favorites_layout = QGridLayout()
        self.input_favorites_layout.setSpacing(6)
        self.input_favorites_layout.setColumnStretch(0, 1)
        self.input_favorites_layout.setColumnStretch(1, 1)
        layout.addLayout(self.input_favorites_layout)
        self.control_widgets.append(self.input_combo)

    def _fit_and_lock_window(self):
        # Size the window for the configured number of favorite rows,
        # then prevent manual resizing while the app is running.
        self.setMinimumHeight(0)
        self.setMaximumHeight(16777215)
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())

    def _new_button(self, text):
        button = QPushButton(text)
        button.setMinimumHeight(34)
        return button

    def _apply_style(self):
        palette = self.palette()

        if self.config["DarkMode"]:
            if self.mica_enabled:
                palette.setColor(QPalette.Window, QColor(0, 0, 0, 0))
            else:
                palette.setColor(QPalette.Window, QColor(32, 32, 32))

            palette.setColor(QPalette.WindowText, QColor(235, 235, 235))
            self.setPalette(palette)

            self.setStyleSheet("""
                QWidget#remoteRoot {
                    background: palette(window);
                }
                QWidget {
                    color: #f0f0f0;
                    font-size: 9pt;
                }
                QFrame#section {
                    background: rgba(38, 38, 38, 185);
                    border: 1px solid rgba(120, 120, 120, 110);
                    border-radius: 8px;
                }
                QLabel {
                    background: transparent;
                }
                QLabel#sectionTitle {
                    color: #d6d6d6;
                    font-weight: 600;
                }
                QLabel#volumeValue {
                    font-size: 13pt;
                    font-weight: 600;
                }
                QPushButton, QComboBox {
                    background-color: rgba(48, 48, 48, 185);
                    color: rgb(235, 235, 235);
                    border: 1px solid rgba(120, 120, 120, 150);
                    border-radius: 7px;
                    padding: 5px 7px;
                    min-height: 22px;
                }
                QComboBox::drop-down {
                    border: none;
                    background: transparent;
                    width: 20px;
                }
                QPushButton:hover, QComboBox:hover {
                    background-color: rgba(68, 68, 68, 205);
                }
                QPushButton:pressed {
                    background-color: rgba(38, 38, 38, 220);
                }
                QPushButton:disabled, QComboBox:disabled:disabled {
                    color: rgba(235, 235, 235, 100);
                    background-color: rgba(48, 48, 48, 100);
                }
                QComboBox QAbstractItemView {
                    background-color: rgb(40, 40, 40);
                    color: rgb(235, 235, 235);
                    selection-background-color: rgb(80, 110, 170);
                    border: 1px solid rgb(120, 120, 120);
                    text-align: center;
                }
            """)
        else:
            if self.mica_enabled:
                palette.setColor(QPalette.Window, QColor(0, 0, 0, 0))
            self.setPalette(palette)

            self.setStyleSheet("""
                QWidget#remoteRoot {
                    background: palette(window);
                }
                QWidget {
                    font-size: 9pt;
                }
                QFrame#section {
                    background: rgba(255,255,255,190);
                    border: 1px solid rgba(0,0,0,25);
                    border-radius: 8px;
                }
                QLabel {
                    background: transparent;
                }
                QLabel#sectionTitle {
                    font-weight: 600;
                }
                QLabel#volumeValue {
                    font-size: 13pt;
                    font-weight: 600;
                }
                QPushButton, QComboBox {
                    background: rgba(255,255,255,215);
                    border: 1px solid rgba(0,0,0,35);
                    border-radius: 7px;
                    padding: 5px 7px;
                    min-height: 22px;
                }
                QComboBox::drop-down {
                    border: none;
                    background: transparent;
                    width: 20px;
                }
                QComboBox QAbstractItemView {
                    background-color: rgb(250, 250, 250);
                    color: rgb(20, 20, 20);
                    selection-background-color: rgb(210, 225, 250);
                    border: 1px solid rgb(180, 180, 180);
                    text-align: center;
                }
                QPushButton:hover, QComboBox:hover {
                    background: rgba(245,245,245,235);
                }
            """)

    # ----------------------------
    # Background work
    # ----------------------------

    def run_worker(self, function, on_result=None, on_error=None, on_finished=None):
        if self._shutting_down or missing_config_settings(self.config):
            return
        worker = Worker(function)
        self._workers.add(worker)
        generation = self._worker_generation

        def deliver_result(result):
            if not self._shutting_down and generation == self._worker_generation and on_result:
                on_result(result)

        def deliver_error(error):
            if not self._shutting_down and generation == self._worker_generation:
                (on_error or self.handle_request_error)(error)

        def finished():
            self._workers.discard(worker)
            if not self._shutting_down and generation == self._worker_generation and on_finished:
                on_finished()

        worker.signals.result.connect(deliver_result)
        worker.signals.error.connect(deliver_error)
        worker.signals.finished.connect(finished)
        self.pool.start(worker)

    def run_action(self, action, *, background=False, batch_volume=False, on_result=None, on_error=None):
        if self._shutting_down or self.calibration_active:
            return False

        if self.action_running or not self.connected:
            return False

        self.action_running = True

        def done(result):
            self.action_running = False
            if on_result:
                on_result(result)
            if batch_volume:
                self._volume_batch_count += 1
            if not batch_volume or not self._continue_volume_batch():
                self.poll_status()
            self._schedule_hotkey_drain()

        def failed(error):
            self.action_running = False
            if on_error:
                on_error(error)
            else:
                self.handle_request_error(error, background=background, command=True)
            self.poll_status()
            self._schedule_hotkey_drain()

        self.run_worker(action, on_result=done, on_error=failed)
        return True

    def _continue_volume_batch(self):
        # Freshness belongs to device responses, never optimistic volume steps.
        if (self.calibration_active or self._last_status_at is None
                or self._volume_batch_count >= VOLUME_BATCH_MAX_COMMANDS
                or time.monotonic() - self._last_status_at >= VOLUME_BATCH_MAX_AGE_SECONDS):
            return False
        with self._hotkey_lock:
            if not self._pending_hotkeys:
                return False
            command = self._pending_hotkeys[0]
        if isinstance(command, tuple):
            command, expected_input = command
            if self.last_status.get("input") != expected_input:
                return False
        return command in ("volume_up", "volume_down")

    # ----------------------------
    # Discovery and polling
    # ----------------------------

    def initialize(self):
        if self._shutting_down:
            return
        self._polling_initialized = True
        if missing_config_settings(self.config):
            self.power_button.setText("Configure device")
            return
        self._update_polling_state()
        self.startup_status_timer.start()
        self.load_inputs()
        self.load_speaker_outputs()

    def load_inputs(self):
        if self._shutting_down:
            return

        if self.initialized:
            self.poll_status()
            return

        def done(inputs):
            config_changed = False

            for item in inputs:
                input_id = item["Id"]
                yamaha_name = item["Label"]
                settings = self.input_settings.get(input_id)

                if settings is None:
                    settings = {
                        "Name": yamaha_name,
                        "NameOverride": "",
                        "Hidden": False,
                        "Favorite": False,
                    }
                    self.input_settings[input_id] = settings
                    config_changed = True
                elif settings["Name"] != yamaha_name:
                    settings["Name"] = yamaha_name
                    config_changed = True

            # After a failed restart the file contains unapplied user edits.
            # Discovery may update memory, but must not overwrite that file.
            if config_changed and not self._restart_failed:
                try:
                    self._save_config_without_restart()
                except ConfigError as ex:
                    record_exception(ex, "Configuration save")
                    QMessageBox.warning(
                        self,
                        "Yamaha Remote - Configuration Warning",
                        str(ex),
                    )

            available_ids = {item["Id"] for item in inputs}
            visible_inputs = [
                {"Id": input_id, "Label": _entry_display_name(settings)}
                for input_id, settings in _ordered_entries(
                    self.input_settings, self.config["SortByName"]
                )
                if input_id in available_ids and not settings["Hidden"]
            ]

            self._build_favorites_from_config()

            self.inputs = visible_inputs
            self.initialized = True
            self.input_combo.clear()

            for item in self.inputs:
                self.input_combo.addItem(
                    item["Label"],
                    item["Id"],
                )

                index = self.input_combo.count() - 1
                self.input_combo.setItemData(
                    index,
                    Qt.AlignCenter,
                    Qt.TextAlignmentRole,
                )

            self._fit_and_lock_window()
            self.poll_status()

        self.run_worker(
            self.api.get_inputs,
            on_result=done,
            on_error=lambda error: self.handle_request_error(error, background=True),
        )

    def load_speaker_outputs(self):
        if self._shutting_down or self._speaker_outputs is not None or self._speaker_discovery_running:
            return
        self._speaker_discovery_running = True
        def done(outputs):
            self._speaker_outputs = outputs
            self.speaker_combo.clear()
            for label, state in (("Off", (False, False)), ("A", (True, False)),
                                 ("B", (False, True)), ("A+B", (True, True))):
                if all(not enabled or name in outputs for name, enabled in zip(("a", "b"), state)):
                    self.speaker_combo.addItem("Speakers: " + label, state)
            if not outputs:
                self.speaker_combo.clear()
                self.speaker_combo.setPlaceholderText("Speakers unavailable")
                self.speaker_combo.setToolTip("No controllable speaker outputs are advertised by this amplifier.")
            self._update_speaker_ui()
            self.update_control_states()
        self.run_worker(
            self.api.get_speaker_outputs, on_result=done,
            on_error=self._speaker_read_failed,
            on_finished=lambda: setattr(self, "_speaker_discovery_running", False),
        )

    def _speaker_read_failed(self, error):
        record_unexpected_exception(error, "Speaker status", background=True)
        self._speaker_state = None
        self.speaker_combo.setToolTip(f"Speaker status unavailable: {error}")
        self._update_speaker_ui()
        self.speaker_combo.setEnabled(False)

    def _accept_speaker_state(self, state):
        self._speaker_state = state
        if self._speaker_target is not None:
            if state == self._speaker_target:
                self._speaker_target = None
            elif self._speaker_grace_deadline is None or time.monotonic() < self._speaker_grace_deadline:
                return  # Do not repaint a requested selection with a lagging GET.
            else:
                self._speaker_target = None
        self.speaker_combo.setToolTip("Speaker outputs")
        self._update_speaker_ui()

    def _update_speaker_ui(self):
        state = self._speaker_target if self._speaker_target is not None else self._speaker_state
        # Qt's findData compares opaque Python tuples by identity, not value.
        index = next((i for i in range(self.speaker_combo.count())
                      if self.speaker_combo.itemData(i) == state), -1)
        self.speaker_combo.setCurrentIndex(index)
        if index < 0 and self._speaker_outputs:
            self.speaker_combo.setPlaceholderText("Speakers: unknown")

    def select_speakers(self, index):
        target = self.speaker_combo.itemData(index)
        if not isinstance(target, tuple) or self.calibration_active:
            self._update_speaker_ui()
            return
        def done(state):
            self._speaker_state = state
            self._speaker_grace_deadline = time.monotonic() + SPEAKER_STATUS_GRACE_SECONDS
            self._update_speaker_ui()
        def failed(error):
            self._speaker_target = None
            self._speaker_state = None
            self._update_speaker_ui()
            self.handle_request_error(error, command=True)
        if self.run_action(lambda: self.api.set_speakers(target, self._speaker_outputs),
                           on_result=done, on_error=failed):
            self._speaker_target = target
            self._speaker_grace_deadline = None
        self._update_speaker_ui()

    def show_connecting_status(self):
        if (
            not self.connected
            and not self._shutting_down
        ):
            self.power_button.setText("Connecting...")

    def clear_startup_status(self):
        if self.startup_status_timer.isActive():
            self.startup_status_timer.stop()

    def poll_status(self):
        if not self._should_poll():
            return

        if self.poll_running or self.action_running or self._calibration_operation:
            return

        self.poll_running = True

        speaker_state = speaker_error = None
        outputs = self._speaker_outputs
        def query():
            nonlocal speaker_state, speaker_error
            status = self.api.get_status()
            observed_at = time.monotonic()
            if outputs and status.get("power") == "on":
                try:
                    speaker_state = self.api.get_speaker_state(outputs)
                except Exception as error:
                    speaker_error = error
            return status, observed_at

        def done(result):
            status, observed_at = result
            self.poll_running = False
            self.clear_startup_status()
            self.connected = True
            self.power_button.setToolTip("")
            self.last_status = status
            self._volume_batch_count = 0
            self._status_sequence += 1
            self._last_status_at = observed_at
            self._publish_hotkey_state()
            self.show_cached_status()
            if speaker_error is not None:
                self._speaker_read_failed(speaker_error)
            elif speaker_state is not None:
                self._accept_speaker_state(speaker_state)
            elif status.get("power") != "on":
                self._speaker_state = self._speaker_target = None
                self._update_speaker_ui()
            self.update_control_states()
            if self._speaker_outputs is None:
                self.load_speaker_outputs()

            if not self.initialized:
                self.load_inputs()
            self._schedule_hotkey_drain()

        def failed(error):
            self.poll_running = False
            self.handle_request_error(error, background=True)

        self.run_worker(query, on_result=done, on_error=failed)

    def _should_poll(self):
        background_control = bool(
            self.config["GlobalVolumeHotkeys"] and self._native_volume_hook is not None
            and self._native_volume_hook.installed
        )
        return bool(
            not self._shutting_down and self._polling_initialized
            and not missing_config_settings(self.config)
            and ((self.isVisible() and not self.isMinimized()) or background_control or self.calibration_active
                 or bool(self.amplifier_server is not None and self.amplifier_server.clients))
        )


    def _update_polling_state(self, refresh=False):
        if self._shutting_down or not hasattr(self, "poll_timer"):
            return
        should_poll = self._should_poll()
        if should_poll:
            if refresh:
                self.poll_status()
            if not self.poll_timer.isActive():
                self.poll_timer.start()
        else:
            self.poll_timer.stop()

    # ----------------------------
    # State and request errors
    # ----------------------------

    def set_controls_enabled(self, enabled):
        enabled = bool(enabled)

        for widget in self.control_widgets:
            widget.setEnabled(enabled)

    def update_control_states(self):
        if not self.connected or not self.last_status:
            self.set_controls_enabled(False)
            return

        # Power and input controls remain available in standby. Selecting an
        # input is an intentional wake-and-switch action.
        for widget in self.control_widgets:
            widget.setEnabled(True)

        power_on = self.last_status.get("power") == "on"

        self.mute_button.setEnabled(power_on)
        self.volume_down.setEnabled(power_on)
        self.volume_up.setEnabled(power_on)
        self.speaker_combo.setEnabled(power_on and bool(self._speaker_outputs)
                                      and self._speaker_state is not None)

    def handle_request_error(self, error, *, background=False, hint=None, command=False):
        record_unexpected_exception(
            error, "Background request" if background else "Command", background=background
        )
        if isinstance(error, DeviceConnectionError):
            self.set_offline()
        elif background and not command:
            self.set_error_status()
        if isinstance(error, (DeviceConnectionError, DeviceResponseError)):
            message = str(error)
        else:
            message = f"Unexpected request error ({type(error).__name__})."
        self.power_button.setToolTip(message)
        if not background:
            if hint:
                message += "\n\n" + hint
            self.show_error(message)

    def show_error(self, message):
        QMessageBox.warning(self, "Yamaha Remote", message)

    def set_offline(self):
        self.clear_startup_status()
        self.connected = False
        self.last_status = None
        self._last_status_at = None
        self._publish_hotkey_state()

        self._speaker_state = self._speaker_target = None
        self._update_speaker_ui()
        self.volume_value.setText("—")
        self.power_button.setText("Offline")

        self.set_controls_enabled(False)

    def set_error_status(self):
        self.clear_startup_status()
        self.connected = False
        self.last_status = None
        self._last_status_at = None
        self._publish_hotkey_state()
        self._speaker_state = self._speaker_target = None
        self._update_speaker_ui()
        self.volume_value.setText("—")
        self.power_button.setText("Error")
        self.set_controls_enabled(False)

    def show_cached_status(self):
        if not self.last_status:
            return

        status = self.last_status

        power = status.get("power", "")

        volume_text = (
            self.api.format_volume(
                status.get("volume")
            )
            if power == "on"
            else "—"
        )

        muted = bool(status.get("mute"))

        self.volume_value.setText(
            volume_text
        )

        self.power_button.setText(
            "Standby" if power == "on" else "On"
        )
        self.mute_button.setText(
            "Unmute" if power == "on" and muted else "Mute"
        )

        if not self.input_combo.view().isVisible():
            current_input = str(
                status.get("input", "")
            )

            matching_index = -1

            for index in range(
                self.input_combo.count()
            ):
                if (
                    str(self.input_combo.itemData(index))
                    == current_input
                ):
                    matching_index = index
                    break

            self.input_combo.setCurrentIndex(matching_index)

    # ----------------------------
    # Device actions and shortcuts
    # ----------------------------

    def toggle_power(self):
        if not self.last_status:
            return

        power_on = self.last_status.get("power") != "on"

        def action():
            self.api.set_power(power_on)

            if power_on:
                self.api.wait_until_powered_on()

        self.run_action(action)

    def toggle_mute(self, *, background=False):
        if not self.last_status:
            return

        enabled = not bool(self.last_status.get("mute"))

        if self.run_action(
            lambda: self.api.set_mute(enabled), background=background
        ):
            self.last_status["mute"] = enabled
            self.show_cached_status()

    def adjust_volume(self, direction, *, background=False, batch_volume=False):
        if not self.last_status:
            return

        action = (
            self.api.volume_down
            if direction < 0
            else self.api.volume_up
        )

        self.run_action(action, background=background, batch_volume=batch_volume)

    def select_input(self, index):
        if index < 0:
            return

        input_id = self.input_combo.itemData(index)
        if input_id:
            self.set_input(str(input_id))

    def set_input(self, input_id):
        self.run_action(
            lambda: self.api.set_input(input_id)
        )


    # ----------------------------
    # Local amplifier state and volume hotkeys
    # ----------------------------

    def _setup_ipc(self):
        if self.amplifier_server is not None:
            self.amplifier_server.deleteLater()
        self.amplifier_server = AmplifierServer(
            self._amplifier_snapshot, self._accept_ipc_command,
            lambda: self._update_polling_state(refresh=True), self,
        )
        if not self.amplifier_server.available:
            record_exception(RuntimeError("Local amplifier endpoint unavailable"), "Local amplifier IPC")

    def _amplifier_snapshot(self):
        status = self.last_status or {}
        age = (time.monotonic() - self._last_status_at
               if self._last_status_at is not None else HOTKEY_STATE_MAX_AGE_SECONDS)
        valid_for = max(0, min(int(HOTKEY_STATE_MAX_AGE_SECONDS * 1000),
                               int((HOTKEY_STATE_MAX_AGE_SECONDS - age) * 1000)))
        def raw(key):
            value = status.get(key)
            return value if type(value) in (int, float) and math.isfinite(value) else None
        return {"version": 1, "type": "state",
                "device_id": hashlib.sha256(self.config["Host"].casefold().encode()).hexdigest(),
                "online": bool(self.connected and not self._shutting_down),
                "power": status.get("power") if status.get("power") in ("on", "standby") else None,
                "input": status.get("input") if isinstance(status.get("input"), str) else None,
                "raw_volume": raw("volume"),
                "volume_display": self.api.format_volume(raw("volume")),
                "volume_unit": "dB" if self.config["VolumeDb"] else "raw",
                "raw_max_volume": raw("max_volume"),
                "mute": status.get("mute") if type(status.get("mute")) is bool else None,
                "global_hotkeys_enabled": self.config["GlobalVolumeHotkeys"],
                "global_hotkeys_active": self.global_volume_hotkeys_active,
                "calibration_active": self.calibration_active,
                "valid_for_ms": valid_for if self.connected and not self._shutting_down else 0}

    def _accept_ipc_command(self, command, expected_input):
        if (self.calibration_active or not expected_input or not self._volume_hotkeys_eligible()
                or self.last_status.get("input") != expected_input
                or QApplication.activeModalWidget() is not None):
            return False, "state_changed"
        with self._hotkey_lock:
            accepted = self._enqueue_hotkey_locked((command, expected_input))
            return accepted, "" if accepted else "queue_full"

    def _native_hotkey_changed(self, installed, error):
        if self._shutting_down:
            return
        self._publish_hotkey_state()
        self._update_polling_state(refresh=installed)
        if error is not None:
            record_exception(error, "Windows volume hotkeys")
            self.show_error(
                "Windows volume-key interception is unavailable. "
                "Global hotkeys will not be claimed; focused Qt handling may be limited."
            )

    def _volume_hotkeys_eligible(self):
        status = self.last_status or {}
        volume = status.get("volume")
        valid_volume = (
            isinstance(volume, (int, float)) and not isinstance(volume, bool)
            and (not isinstance(volume, float) or math.isfinite(volume))
        )
        return bool(
            not self._shutting_down and self.connected and status.get("power") == "on"
            and valid_volume and isinstance(status.get("mute"), bool)
            and self._last_status_at is not None
            and time.monotonic() - self._last_status_at <= HOTKEY_STATE_MAX_AGE_SECONDS
        )

    @property
    def global_volume_hotkeys_active(self):
        return bool(self.calibration_active or (
            self.config["GlobalVolumeHotkeys"]
            and self._native_volume_hook is not None and self._native_volume_hook.installed
            and self._volume_hotkeys_eligible()
            and QApplication.activeModalWidget() is None
        ))

    def _publish_hotkey_state(self):
        # The native hook thread reads only this plain-Python snapshot. Qt
        # focus/modal queries stay on the GUI thread.
        if not hasattr(self, "_hotkey_lock"):
            return
        state = (
            self._volume_hotkeys_eligible(), self.config["GlobalVolumeHotkeys"],
            int(self.winId()) if sys.platform == "win32" and self.isVisible() and not self.isMinimized() else 0,
            self._last_status_at, QApplication.activeModalWidget() is not None,
        )
        with self._hotkey_lock:
            self._hotkey_state = state
            if not state[0]:
                self._pending_hotkeys.clear()
        if self.amplifier_server is not None:
            self.amplifier_server.publish()
        if self.calibration_server is not None:
            self.calibration_server.publish()

    def _refresh_hotkey_state(self):
        self._hotkey_state_refresh_pending = False
        if not self._shutting_down:
            self._publish_hotkey_state()

    def _accept_native_hotkey(self, command, foreground):
        with self._hotkey_lock:
            if self._calibration_key_blocked:
                return command in _VOLUME_KEYS.values()
            eligible, global_mode, window, observed_at, blocked = self._hotkey_state
            if (command not in _VOLUME_KEYS.values() or not eligible or blocked or observed_at is None
                    or time.monotonic() - observed_at > HOTKEY_STATE_MAX_AGE_SECONDS
                    or (not global_mode and (not window or foreground != window))):
                return False
            # Ownership depends on eligibility, not worker capacity. A wheel
            # emits distinct presses; forwarding overflow would alter Windows
            # volume while the rest of the same burst controls the amplifier.
            self._enqueue_hotkey_locked(command)
            return True

    def _enqueue_hotkey_locked(self, command):
        key_command = command[0] if isinstance(command, tuple) else command
        if key_command not in _VOLUME_KEYS.values() or len(self._pending_hotkeys) >= MAX_PENDING_HOTKEYS:
            return False
        self._pending_hotkeys.append(command)
        if not self._hotkey_drain_scheduled:
            self._hotkey_drain_scheduled = True
            self._hotkey_ready.emit()
        return True

    def _accept_focused_hotkey(self, command):
        if self.calibration_active:
            return command in _VOLUME_KEYS.values()
        if (command not in _VOLUME_KEYS.values() or not self.isActiveWindow()
                or QApplication.activeModalWidget() is not None
                or not self._volume_hotkeys_eligible()):
            return False
        with self._hotkey_lock:
            self._enqueue_hotkey_locked(command)
            return True

    def _schedule_hotkey_drain(self):
        with self._hotkey_lock:
            if self._pending_hotkeys and not self._hotkey_drain_scheduled:
                self._hotkey_drain_scheduled = True
                self._hotkey_ready.emit()

    def _drain_hotkeys(self):
        with self._hotkey_lock:
            self._hotkey_drain_scheduled = False
            if not self._volume_hotkeys_eligible() or QApplication.activeModalWidget() is not None:
                self._pending_hotkeys.clear()
                return
            if self.action_running or self.poll_running or not self._pending_hotkeys:
                return
            refresh = bool(self._volume_batch_count and (
                self._last_status_at is None
                or time.monotonic() - self._last_status_at >= VOLUME_BATCH_MAX_AGE_SECONDS
            ))
            command = None if refresh else self._pending_hotkeys.popleft()
        if refresh:
            self.poll_status()
            return
        if isinstance(command, tuple):
            command, expected_input = command
            if self.last_status.get("input") != expected_input:
                self._schedule_hotkey_drain()
                return
        if command == "mute":
            self.toggle_mute(background=True)
        else:
            self.adjust_volume(1 if command == "volume_up" else -1, background=True, batch_volume=True)


    def eventFilter(self, watched, event):
        event_type = event.type()
        if event_type in (QEvent.Show, QEvent.Hide, QEvent.WindowActivate, QEvent.WindowDeactivate):
            if not self._hotkey_state_refresh_pending and not self._shutting_down:
                self._hotkey_state_refresh_pending = True
                QTimer.singleShot(0, self._refresh_hotkey_state)
        if event_type not in (QEvent.KeyPress, QEvent.KeyRelease):
            return super().eventFilter(watched, event)
        command = _QT_VOLUME_KEYS.get(event.key())
        # Windows' native hook already handles both focused and global keys.
        if command is None or (self._native_volume_hook is not None and self._native_volume_hook.installed):
            return super().eventFilter(watched, event)
        if event_type == QEvent.KeyRelease:
            owned = self._qt_volume_keys.pressed.get(event.key(), False) if event.isAutoRepeat() else self._qt_volume_keys.release(event.key())
        else:
            if event.isAutoRepeat() and event.key() not in self._qt_volume_keys.pressed:
                self._qt_volume_keys.pressed[event.key()] = False
            owned = self._qt_volume_keys.press(event.key(), command, self._accept_focused_hotkey)
        if owned:
            event.accept()
            return True
        return super().eventFilter(watched, event)


    # ----------------------------
    # Configuration watching and restart
    # ----------------------------

    def _get_config_digest(self):
        return _file_sha256(CONFIG_PATH)

    def _setup_config_watcher(self):
        self.config_watcher = QFileSystemWatcher(self)
        self.config_watcher.fileChanged.connect(
            self._config_path_changed
        )
        self.config_watcher.directoryChanged.connect(
            self._config_path_changed
        )

        # Event-driven debounce only: QFileSystemWatcher supplies the events.
        # This timer does not poll the filesystem.
        self.config_restart_timer = QTimer(self)
        self.config_restart_timer.setSingleShot(True)
        self.config_restart_timer.setInterval(CONFIG_RESTART_DEBOUNCE_MS)
        self.config_restart_timer.timeout.connect(
            self._check_external_config_change
        )

        self._watch_config_paths()

    def _watch_config_paths(self):
        paths = {
            str(CONFIG_PATH),
            str(CONFIG_PATH.parent),
        }

        watched = set(self.config_watcher.files())
        watched.update(self.config_watcher.directories())

        missing = [
            path
            for path in paths
            if path not in watched
        ]

        if missing:
            self.config_watcher.addPaths(missing)

    def _config_path_changed(self, _path):
        if self._shutting_down or self._config_restart_pending:
            return
        # Many editors save by replacing the file. Watching both the file and
        # its directory lets us re-add a replaced file without polling.
        self.config_restart_timer.start()

    def _check_external_config_change(self):
        if self.calibration_active:
            self.config_restart_timer.start(1000)
            return
        if self._shutting_down or self._config_restart_pending:
            return
        self._watch_config_paths()

        content, new_hash = _file_snapshot(CONFIG_PATH)

        if new_hash is None or new_hash == self._config_digest:
            return

        try:
            raw_config = json.loads(content.decode("utf-8"))
            validate_config(raw_config)
        except (OSError, ValueError) as ex:
            if new_hash != self._invalid_config_digest:
                self._invalid_config_digest = new_hash
                QMessageBox.warning(
                    self, "Yamaha Remote - Configuration Error",
                    f"The configuration change was not applied:\n{ex}\n\n"
                    "Correct the JSON settings and save again. The current instance remains running.",
                )
            return

        self._invalid_config_digest = None
        self._restart_for_config_change()

    def _save_config_without_restart(self):
        # A failed restart or unknown baseline must never authorize a save.
        if self._restart_failed or self._config_digest is None:
            return
        digest = save_config(self.config, expected_digest=self._config_digest)
        if digest is not None:
            # Use our own bytes; a later user edit must remain detectable.
            self._config_digest = digest
        self._watch_config_paths()

    def _restart_for_config_change(self):
        if self.calibration_active:
            self._config_restart_pending = False
            self.config_restart_timer.start(1000)
            return
        if self._shutting_down or self._config_restart_pending:
            return
        self._config_restart_pending = True
        active_timers = self._pause_background_work()
        release_single_instance()
        try:
            launch_replacement()
        except Exception as exc:
            try:
                if not acquire_single_instance():
                    raise RuntimeError("Another instance may already be running.")
            except Exception as lock_error:
                QMessageBox.critical(
                    self, "Yamaha Remote - Restart Error",
                    f"Could not restart: {exc}\nCould not restore the instance lock: "
                    f"{lock_error}\nThis instance will close.",
                )
                self._shutdown()
                QApplication.quit()
                return
            # Retain the original in-memory settings, but consume this file
            # revision so an unrelated directory event cannot retry forever.
            self._config_digest = _file_sha256(CONFIG_PATH)
            self._restart_failed = True
            self._resume_background_work(active_timers)
            QMessageBox.warning(
                self, "Yamaha Remote - Restart Error",
                f"Could not restart: {exc}\n\nThe current instance is still running "
                "with its previous settings. Edit and save the JSON again to retry.",
            )
            return
        self._shutdown()
        QApplication.quit()

    # ----------------------------
    # Tray and shutdown lifecycle
    # ----------------------------

    def _setup_activation(self):
        if self.activation_server is not None:
            self.activation_server.deleteLater()
        self.activation_server = ActivationServer(
            "amplifier", self._activate_existing_window, self,
        )
        if not self.activation_server.available:
            record_exception(RuntimeError("Local activation endpoint unavailable"), "Window activation")

    def _activate_existing_window(self):
        if self._shutting_down:
            return False
        self.show_from_tray()
        dialog = QApplication.activeModalWidget()
        if dialog is not None:
            dialog.raise_()
            dialog.activateWindow()
        return True

    def _setup_tray(self):
        if not self.config["SystemTray"] or not QSystemTrayIcon.isSystemTrayAvailable():
            return

        self.tray = QSystemTrayIcon(
            QApplication.windowIcon(),
            self,
        )
        self.tray.setToolTip("Yamaha Remote")

        menu = QMenu(self)

        show_action = QAction("Show", self)
        show_action.triggered.connect(
            self.show_from_tray
        )
        menu.addAction(show_action)

        menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(
            self.quit_from_tray
        )
        menu.addAction(exit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            self.tray_activated
        )
        self.tray.show()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self._update_polling_state(refresh=True)

    def quit_from_tray(self):
        self._really_close = True
        self.close()

    def _pause_background_work(self):
        self._volume_batch_count = 0
        self._shutting_down = True
        if self.activation_server is not None:
            self.activation_server.close()
        if self.calibration_server is not None:
            self.calibration_server.close()
        self._publish_hotkey_state()
        self.amplifier_server.close()
        self._qt_volume_keys.pressed.clear()
        self._worker_generation += 1
        timers = (self.poll_timer, self.startup_status_timer, self.config_restart_timer)
        active_timers = [timer for timer in timers if timer.isActive()]
        for timer in timers:
            timer.stop()
        self.config_watcher.blockSignals(True)
        # Drop queued jobs; active requests have their own bounded timeouts.
        self.pool.clear()
        self.pool.waitForDone()
        self._workers.clear()
        self.poll_running = False
        self.action_running = False
        self._speaker_discovery_running = False
        self._speaker_target = None
        self._update_speaker_ui()
        return active_timers

    def _resume_background_work(self, active_timers):
        self._shutting_down = False
        self._setup_activation()
        self._setup_calibration()
        self._config_restart_pending = False
        self._setup_ipc()
        self._publish_hotkey_state()
        self.config_watcher.blockSignals(False)
        self._watch_config_paths()
        for timer in active_timers:
            timer.start()
        if not missing_config_settings(self.config):
            self.update_control_states()
            self.load_inputs()
        self._update_polling_state(refresh=True)

    def _shutdown(self):
        if self._shutdown_complete:
            return
        self._pause_background_work()
        self._shutdown_complete = True
        self._really_close = True
        QApplication.instance().removeEventFilter(self)
        if self._native_volume_hook is not None:
            self._native_volume_hook.stop()
        if self.tray is not None:
            self.tray.hide()
        self.amplifier_server.close()
        release_single_instance()

    # ----------------------------
    # Qt events
    # ----------------------------

    def showEvent(self, event):
        super().showEvent(event)

        if self.mica_enabled:
            apply_mica(int(self.winId()))

        if self.config["DarkMode"] and sys.platform == "win32":
            set_dark_titlebar(int(self.winId()))
        self._publish_hotkey_state()
        self._update_polling_state(refresh=True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._publish_hotkey_state()
        self._qt_volume_keys.pressed.clear()
        self._update_polling_state()

    def changeEvent(self, event):
        super().changeEvent(event)
        self._publish_hotkey_state()
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow():
            self._qt_volume_keys.pressed.clear()
        if event.type() == QEvent.WindowStateChange:
            QTimer.singleShot(
                0,
                lambda: self._update_polling_state(
                    refresh=self.isVisible() and not self.isMinimized()
                ),
            )

    def closeEvent(self, event):
        if (self.config["CloseToTray"] and not self._really_close and self.tray is not None
                and QSystemTrayIcon.isSystemTrayAvailable()):
            event.ignore()
            self.hide()
            return
        self._shutdown()
        event.accept()
        QApplication.quit()

    def mousePressEvent(self, event):
        if (
            event.button() == Qt.LeftButton
            and self.childAt(event.position().toPoint()) is None
        ):
            handle = self.windowHandle()

            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return

        super().mousePressEvent(event)


# ----------------------------
# Application entry points
# ----------------------------


def launch_replacement():
    script_path = Path(__file__).resolve()
    arguments = list(sys.argv[1:])
    if not getattr(sys, "frozen", False):
        arguments.insert(0, str(script_path))
    result = QProcess.startDetached(sys.executable, arguments, str(script_path.parent))
    started = result[0] if isinstance(result, tuple) else bool(result)
    if not started:
        raise OSError("The replacement process could not be started.")


def _run_application():
    app = QApplication(sys.argv)
    app.setStyle(TooltipDelayStyle())

    try:
        acquired = acquire_single_instance()
    except Exception as exc:
        QMessageBox.critical(None, "Yamaha Remote", f"Could not acquire the instance lock:\n{exc}")
        return 1
    if not acquired:
        if activate_existing_instance("amplifier"):
            return 0
        QMessageBox.warning(
            None, APP_NAME,
            "The remote is already running, but its window could not be brought forward. "
            "Check the system tray, or wait for startup/shutdown to finish and try again.",
        )
        return 1

    if sys.platform == "win32":
        set_app_user_model_id("Yamaha.Remote")

    app.setApplicationName(APP_NAME)
    app.setWindowIcon(get_app_icon())

    try:
        config = load_config()
    except ConfigError as ex:
        QMessageBox.critical(
            None,
            "Yamaha Remote - Configuration Error",
            str(ex),
        )
        return 1

    configure_diagnostics(config)
    try:
        window = YamahaRemote(config)
    except Exception as ex:
        record_exception(ex, "Startup")
        QMessageBox.critical(
            None,
            "Yamaha Remote - Startup Error",
            f"Unexpected startup error ({type(ex).__name__}).",
        )
        return 1

    app.setQuitOnLastWindowClosed(window.tray is None)
    app.aboutToQuit.connect(window._shutdown)
    show_missing_config_settings(config)
    if ("--start-in-tray" in sys.argv[1:]
            and config["SystemTray"] and config["CloseToTray"]
            and window.tray is not None and QSystemTrayIcon.isSystemTrayAvailable()):
        # A new window is already hidden: do not show it even for one frame.
        window._update_polling_state()
    else:
        window.show()

    try:
        return app.exec()
    finally:
        window._shutdown()


def main():
    try:
        return _run_application()
    except Exception as error:
        record_exception(error, "Application")
        if QApplication.instance() is not None:
            QMessageBox.critical(
                None, "Yamaha Remote", f"Unexpected application error ({type(error).__name__})."
            )
        return 1
    finally:
        release_single_instance()
        close_diagnostics()


if __name__ == "__main__":
    raise SystemExit(main())
