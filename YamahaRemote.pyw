import ctypes
import copy
from datetime import datetime, timezone
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
from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import qVersion, QByteArray, QEasingCurve, QElapsedTimer, QRect, QEvent, QFileInfo, QFileSystemWatcher, QObject, QLockFile, QProcess, QRunnable, QStandardPaths, QThreadPool, QTimer, Qt, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QAction, QKeySequence, QFont, QIcon, QPalette, QStyleHints
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileIconProvider,
    QGridLayout,
    QLayout,
    QFrame,
    QVBoxLayout,
    QMenu,
    QLabel,
    QMessageBox,
    QPushButton,
    QProxyStyle,
    QStyle,
    QStylePainter,
    QStyleOptionButton, QSizePolicy, QStyleOptionComboBox,
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
IPC_MAX_TV_CONTEXT_VALIDITY_MS = 6000  # Protocol v1 wire limit.


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
        self.focused_volume = {}
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
            self.focused_volume.pop(socket, None)
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
                if message.get("type") == "focused_volume":
                    window, bindings, input_id, valid_for = (message.get(key) for key in
                        ("window", "shortcuts", "input", "valid_for_ms"))
                    if (type(window) is not int or not 0 <= window < 2**64
                            or not isinstance(input_id, str) or len(input_id) > 256
                            or type(valid_for) is not int or not 0 <= valid_for <= IPC_MAX_TV_CONTEXT_VALIDITY_MS
                            or not isinstance(bindings, list) or len(bindings) > 128
                            or any(type(code) is not int or not 0 < code <= 0x7FFFFFFF for code in bindings)
                            or len(set(bindings)) != len(bindings)):
                        raise ValueError("Invalid focused volume context")
                    if window and bindings and input_id and valid_for:
                        self.focused_volume[socket] = (window, tuple(bindings), input_id,
                                                      time.monotonic() + valid_for / 1000)
                    else:
                        self.focused_volume.pop(socket, None)
                    self.changed()
                    continue
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

    def __init__(self, role, activate, parent=None, *, dump=None):
        super().__init__(parent)
        self.role, self.activate = role, activate
        self.dump = dump
        self.handled = set()
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
            timer.setObjectName("request_deadline")
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
            if not self.available or len(messages) != 1 or socket in self.handled:
                raise ValueError("Invalid instance request")
            self.handled.add(socket)
            if messages[0] == {"version": 1, "type": "activate"}:
                ok = bool(self.activate())
                socket.write(_ipc_encode({"version": 1, "type": "activated", "ok": ok}))
                socket.disconnectFromServer()
            elif (messages[0] in ({"version": 1, "type": "dump_state"},
                                  {"version": 1, "type": "dump_state_full"}) and self.dump is not None):
                socket.findChild(QTimer, "request_deadline").start(5000)
                def completed(result):
                    if socket in self.clients:
                        socket.write(_ipc_encode(dict(result, version=1, type="state_dump")))
                        socket.disconnectFromServer()
                if messages[0]["type"] == "dump_state_full":
                    self.dump(completed, full=True)
                else:
                    self.dump(completed)
            else:
                raise ValueError("Unsupported instance request")
        except (ValueError, UnicodeError, KeyError):
            socket.abort()

    def _lost(self, socket):
        if socket in self.clients:
            del self.clients[socket]
            self.handled.discard(socket)
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


def _request_existing_instance(role, request_type, timeout_seconds):
    """Bound startup retries and the complete handshake to one deadline."""
    socket = QLocalSocket()
    socket.setReadBufferSize(IPC_MAX_FRAME + 1)
    buffer = bytearray()
    deadline = time.monotonic() + timeout_seconds

    def remaining():
        return max(0.0, deadline - time.monotonic())

    def receive(expected):
        while remaining():
            messages = _ipc_frames(socket, buffer)
            if messages:
                if len(messages) != 1 or messages[0].get("type") != expected:
                    return None
                return messages[0]
            if not socket.waitForReadyRead(int(remaining() * 1000)) and not socket.bytesAvailable():
                return None
        return None

    try:
        while remaining():
            socket.connectToServer(ipc_server_name(role, channel="activation"))
            if socket.waitForConnected(int(min(0.15, remaining()) * 1000)):
                break
            socket.abort()
            time.sleep(min(0.05, remaining()))
        else:
            return None
        hello = receive("instance")
        if (not hello or hello.get("role") != role or type(hello.get("process_id")) is not int
                or not 0 < hello["process_id"] <= 0xFFFFFFFF):
            return None
        if request_type == "activate":
            _allow_foreground_activation(hello["process_id"])
        payload = _ipc_encode({"version": 1, "type": request_type})
        if socket.write(payload) != len(payload):
            return None
        if socket.bytesToWrite() and not socket.waitForBytesWritten(int(remaining() * 1000)):
            return None
        return receive("activated" if request_type == "activate" else "state_dump")
    except (ValueError, UnicodeError, OSError, TypeError):
        return None
    finally:
        socket.abort()



def activate_existing_instance(role, timeout_seconds=1.5):
    reply = _request_existing_instance(role, "activate", timeout_seconds)
    return bool(reply and reply.get("ok") is True)


def dump_existing_state(role, *, full=False):
    reply = _request_existing_instance(role, "dump_state_full" if full else "dump_state", 5.0)
    if reply and reply.get("ok") is True and isinstance(reply.get("path"), str):
        message = "Cached state saved to:\n" + reply["path"]
        if reply.get("fallback"):
            message += "\n(The script folder was unavailable; the app data folder was used.)"
        if full:
            message += ("\n\nPrivacy warning: This full report may contain device/network identifiers "
                        "and app inventories. Review it before sharing.")
        ok = True
    else:
        error = reply.get("error", "unavailable") if reply else "unavailable"
        message = ("No state dump was confirmed. Start the remote first, or wait until it is responsive."
                   if error == "unavailable" else "Could not write the cached state dump (" + str(error) + ").")
        ok = False
    if sys.stdout is not None:
        print(message)
    else:
        (QMessageBox.information if ok else QMessageBox.warning)(None, APP_NAME + " - State dump", message)
    return 0 if ok else 1


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


__version__ = "2026.10.22.1"


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


DEFAULT_SHORTCUTS = {
    "VolumeUp": ["Volume Up"],
    "VolumeDown": ["Volume Down"],
    "Mute": ["Volume Mute"],
}


SHORTCUT_ACTIONS = {
    "VolumeUp": "volume_up",
    "VolumeDown": "volume_down",
    "Mute": "mute",
}


DEFAULT_CONFIG = {
    "Shortcuts": DEFAULT_SHORTCUTS,
    "Host": "",
    "DarkMode": True,
    "Mica": False,
    "VolumeDb": False,
    "VolumeDbAtZero": -97.0,
    "VolumeDbStepSize": 0.5,
    "GlobalShortcuts": {"VolumeUp": [], "VolumeDown": [], "Mute": []},
    "DiagnosticsEnabled": False,
    "SortByName": False,
    "DropdownAnimationWorkaround": sys.platform == "win32",
    "Inputs": {},
    "SystemTray": False,
    "CloseToTray": False,
}


BOOLEAN_CONFIG_KEYS = (
    "DiagnosticsEnabled", "SortByName", "DarkMode", "Mica",
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
            "Removed",
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
        removed = settings.get("Removed", False)

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

        if not isinstance(removed, bool):
            raise ConfigError(f'"{collection}" entry {input_id!r} "Removed" must be true or false.')

        normalized[input_id] = {
            "Name": name.strip(),
            "NameOverride": name_override.strip(),
            "Hidden": hidden,
            "Favorite": favorite,
            "Removed": removed,
        }

    return normalized


def _entry_display_name(settings):
    return settings["NameOverride"] or settings["Name"]


def _ordered_entries(entries, sort_by_name):
    items = list(entries.items())
    if sort_by_name:
        items.sort(key=lambda item: _entry_display_name(item[1]).casefold())
    return items


def _validate_shortcuts(raw, *, defaults=None, section="Shortcuts"):
    defaults = DEFAULT_SHORTCUTS if defaults is None else defaults
    if not isinstance(raw, dict):
        raise ConfigError(f'"{section}" must be an object.')
    unknown = set(raw) - set(defaults)
    if unknown:
        raise ConfigError('Unknown shortcut action(s): ' + ', '.join(sorted(unknown)) + '.')
    normalized, owners = {}, {}
    for action, fallback in defaults.items():
        bindings = raw.get(action, fallback)
        if bindings == "":
            bindings = []
        if not isinstance(bindings, list):
            raise ConfigError(f'{section}.{action} must be a list of key strings, or empty to disable.')
        normalized[action] = []
        for text in bindings:
            if not isinstance(text, str) or not text.strip():
                raise ConfigError(f'{section}.{action} contains an empty or invalid key.')
            sequence = QKeySequence.fromString(text.strip(), QKeySequence.PortableText)
            if (sequence.count() != 1 or sequence[0].key() in (Qt.Key_unknown, 0)
                    or not sequence.toString(QKeySequence.PortableText)):
                raise ConfigError(f'{section}.{action}: invalid single-key combination {text!r}.')
            code = sequence[0].toCombined()
            canonical = sequence.toString(QKeySequence.PortableText)
            if code in owners:
                raise ConfigError(f'Shortcut {canonical!r} is assigned more than once '
                                  f'({owners[code]} and {action}).')
            owners[code] = action
            normalized[action].append(canonical)
    return normalized


def _compile_shortcuts(config, section="Shortcuts"):
    return {QKeySequence.fromString(text, QKeySequence.PortableText)[0].toCombined():
            SHORTCUT_ACTIONS[action]
            for action, bindings in config[section].items() for text in bindings}


def _shortcut_code(event):
    return event.key() | event.modifiers().value


def _focused_shortcut_command(bindings, event):
    code = _shortcut_code(event)
    command = bindings.get(code)
    # Printable punctuation already encodes Shift on many layouts (e.g. '+').
    # Prefer an explicitly modified binding, then accept the literal symbol.
    if (command is None and event.modifiers() & Qt.ShiftModifier
            and 0x21 <= event.key() <= 0x7E and not chr(event.key()).isalnum()):
        command = bindings.get(code & ~Qt.ShiftModifier.value)
    return command


def _shortcut_tip(config, command):
    action = next((name for name, value in SHORTCUT_ACTIONS.items() if value == command), None)
    labels = [QKeySequence.fromString(text, QKeySequence.PortableText).toString(QKeySequence.NativeText)
              for text in config["Shortcuts"].get(action, [])]
    lines = ["Shortcut: " + " / ".join(labels)] if labels else []
    global_labels = [QKeySequence.fromString(text, QKeySequence.PortableText).toString(QKeySequence.NativeText)
                     for text in config.get("GlobalShortcuts", {}).get(action, [])]
    if global_labels:
        lines.append("Global shortcut: " + " / ".join(global_labels))
    return "\n".join(lines)


def _default_config_copy():
    return copy.deepcopy(DEFAULT_CONFIG)


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

    # Retired toggle is removed when the file is next saved; bindings are authoritative.
    raw_config = dict(raw_config)
    legacy_global = raw_config.pop("GlobalVolumeHotkeys", None)
    if "GlobalShortcuts" not in raw_config and legacy_global is True:
        raw_config["GlobalShortcuts"] = copy.deepcopy(DEFAULT_SHORTCUTS)
    unknown = set(raw_config) - set(DEFAULT_CONFIG)
    if unknown:
        raise ConfigError("Unknown configuration setting(s): " + ", ".join(sorted(unknown)) + ".")

    config = _default_config_copy()
    config.update(raw_config)
    config["Shortcuts"] = _validate_shortcuts(config["Shortcuts"])
    config["GlobalShortcuts"] = _validate_shortcuts(
        config["GlobalShortcuts"], defaults=DEFAULT_CONFIG["GlobalShortcuts"], section="GlobalShortcuts")
    if sum(map(len, config["GlobalShortcuts"].values())) > 128:
        raise ConfigError("At most 128 global shortcuts can be configured.")
    native_owners = set()
    for code in _compile_shortcuts(config, "GlobalShortcuts"):
        native = _native_binding(code)
        if native in native_owners:
            raise ConfigError("Global shortcuts resolve to the same physical key combination.")
        native_owners.add(native)

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
        content = config_path.read_bytes()
        raw_config = json.loads(content.decode("utf-8"))
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

    config = validate_config(raw_config, allow_incomplete=True)
    collections = ("Apps", "Inputs") if "Apps" in config else ("Inputs",)
    missing_removed = any("Removed" not in item for collection in collections
                          for item in raw_config.get(collection, {}).values())
    if ("Shortcuts" not in raw_config or set(DEFAULT_SHORTCUTS) - set(raw_config["Shortcuts"])
            or "GlobalShortcuts" not in raw_config or "GlobalVolumeHotkeys" in raw_config
            or set(DEFAULT_CONFIG["GlobalShortcuts"]) - set(raw_config["GlobalShortcuts"]) or missing_removed):
        updated = dict(raw_config)
        updated["Shortcuts"] = config["Shortcuts"]
        updated["GlobalShortcuts"] = config["GlobalShortcuts"]
        updated.pop("GlobalVolumeHotkeys", None)
        if missing_removed:
            for collection in collections:
                updated[collection] = config[collection]
        save_config(updated, expected_digest=hashlib.sha256(content).digest())
    return config


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


# Volume actions are independent of their configurable physical bindings.
_VOLUME_KEYS = {0xAF: "volume_up", 0xAE: "volume_down", 0xAD: "mute"}
_RETAINED_VOLUME_HOOKS = []



_NATIVE_QT_VOLUME_KEYS = {0xAF: Qt.Key_VolumeUp.value, 0xAE: Qt.Key_VolumeDown.value,
                          0xAD: Qt.Key_VolumeMute.value}


_NATIVE_SPECIAL_KEYS = {
    Qt.Key_Backspace.value: 0x08, Qt.Key_Tab.value: 0x09, Qt.Key_Return.value: 0x0D,
    Qt.Key_Enter.value: 0x0D, Qt.Key_Escape.value: 0x1B, Qt.Key_Space.value: 0x20,
    Qt.Key_PageUp.value: 0x21, Qt.Key_PageDown.value: 0x22, Qt.Key_End.value: 0x23,
    Qt.Key_Home.value: 0x24, Qt.Key_Left.value: 0x25, Qt.Key_Up.value: 0x26,
    Qt.Key_Right.value: 0x27, Qt.Key_Down.value: 0x28, Qt.Key_Insert.value: 0x2D,
    Qt.Key_Delete.value: 0x2E, Qt.Key_VolumeMute.value: 0xAD,
    Qt.Key_VolumeDown.value: 0xAE, Qt.Key_VolumeUp.value: 0xAF,
    Qt.Key_MediaNext.value: 0xB0, Qt.Key_MediaPrevious.value: 0xB1,
    Qt.Key_MediaStop.value: 0xB2, Qt.Key_MediaTogglePlayPause.value: 0xB3,
    **{Qt.Key_F1.value + i: 0x70 + i for i in range(24)},
}


def _native_binding(code):
    key, modifiers = code & 0x01FFFFFF, code & ~0x01FFFFFF
    if modifiers & ~int((Qt.ShiftModifier | Qt.ControlModifier | Qt.AltModifier |
                         Qt.MetaModifier | Qt.KeypadModifier).value):
        raise ConfigError("Unsupported global shortcut modifier.")
    if key == Qt.Key_Enter.value:
        return 0x0D, modifiers | Qt.KeypadModifier.value
    if modifiers & Qt.KeypadModifier.value:
        keypad = {ord(str(i)): 0x60 + i for i in range(10)}
        keypad.update({ord('+'): 0x6B, ord('-'): 0x6D, ord('*'): 0x6A,
                       ord('/'): 0x6F, ord('.'): 0x6E})
        if key in keypad:
            return keypad[key], modifiers & ~Qt.KeypadModifier.value
        if key in (Qt.Key_Enter.value, Qt.Key_Left.value, Qt.Key_Right.value,
                   Qt.Key_Up.value, Qt.Key_Down.value, Qt.Key_Home.value, Qt.Key_End.value,
                   Qt.Key_PageUp.value, Qt.Key_PageDown.value, Qt.Key_Insert.value, Qt.Key_Delete.value):
            return _NATIVE_SPECIAL_KEYS[key], modifiers
        raise ConfigError("Unsupported global keypad shortcut.")
    if key in _NATIVE_SPECIAL_KEYS:
        return _NATIVE_SPECIAL_KEYS[key], modifiers
    if ord('A') <= key <= ord('Z') or ord('0') <= key <= ord('9'):
        return key, modifiers
    if 0x21 <= key <= 0xFFFF:
        if sys.platform != "win32":
            return key, modifiers  # No global hook on this platform.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
        user32.VkKeyScanW.restype = ctypes.c_short
        value = user32.VkKeyScanW(chr(key))
        if value != -1:
            for bit, mask in ((1, Qt.ShiftModifier.value), (2, Qt.ControlModifier.value),
                              (4, Qt.AltModifier.value)):
                if (value >> 8) & bit:
                    modifiers |= mask
            return value & 255, modifiers
    raise ConfigError("Unsupported global shortcut: " + QKeySequence(code).toString())


def _native_event_modifiers(key, flags, modifiers):
    if key == 0x0D and flags & 1 or key in range(0x21, 0x2F) and not flags & 1:
        modifiers |= Qt.KeypadModifier.value
    return modifiers


def _native_shortcut_modifiers(user32):
    return sum(mask for keys, mask in (((0x10,), 0x02000000), ((0x11,), 0x04000000),
                                      ((0x12,), 0x08000000), ((0x5B, 0x5C), 0x10000000))
               if any(user32.GetAsyncKeyState(key) & 0x8000 for key in keys))


class _VolumeKeyState:
    """Keep a physical press with its original owner until release."""

    def __init__(self):
        self.pressed = {}
        self.commands = {}

    def clear(self):
        self.pressed.clear()
        self.commands.clear()

    def press(self, key, command, accept):
        if key in self.pressed:
            owned = self.pressed[key]
            original = self.commands.get(key, command)
            if owned and (original[-1] if isinstance(original, tuple) else original) not in ("mute", "PlayPause"):
                accept(original)
            return owned
        owned = bool(accept(command))
        self.pressed[key] = owned
        if owned:
            self.commands[key] = command
        return owned

    def release(self, key):
        self.commands.pop(key, None)
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

    def __init__(self, accept, parent=None, *, resolve=None):
        super().__init__(parent)
        self._accept = accept
        self._resolve = resolve
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
            self._keys.clear()
            self._keys.pressed = {
                key: False for key in range(1, 256)
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
                if self._resolve is not None:
                    command = self._keys.commands.get(key) or self._resolve(
                        key, _native_event_modifiers(key, data.flags, _native_shortcut_modifiers(self._user32)))
                # Remappers can generate injected volume-key events. They are
                # safe to handle too: this remote never synthesizes key events.
                if command or key in self._keys.pressed:
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
        self.polling_responses = PollingResponses(('main/getStatus', 'system/getFeatures', 'system/getFuncStatus', 'system/getNameText'))
        self._features = None

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
            self.polling_responses.failed(path, "http_errors")
            raise DeviceResponseError(f"Yamaha returned HTTP {ex.code}.") from ex
        except (URLError, TimeoutError, OSError) as ex:
            self.polling_responses.failed(path, _connection_failure_category(ex))
            raise YamahaConnectionError(
                "Unable to reach the Yamaha."
            ) from ex
        except (UnicodeError, json.JSONDecodeError) as ex:
            self.polling_responses.failed(path, "malformed_responses")
            raise DeviceResponseError("Yamaha returned invalid JSON.") from ex

        if not isinstance(data, dict):
            self.polling_responses.failed(path, "malformed_responses")
            raise DeviceResponseError("Yamaha response must be a JSON object.")
        response_code = data.get("response_code")
        if isinstance(response_code, bool) or not isinstance(response_code, int):
            self.polling_responses.failed(path, "malformed_responses")
            raise DeviceResponseError("Yamaha response has a missing or invalid response code.")
        if response_code != 0:
            self.polling_responses.failed(path, "device_rejections")
            raise DeviceResponseError(f"Yamaha returned response code {response_code}.")

        self.polling_responses.record(path, data)
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

    def _get_features(self, *, refresh=False):
        # Initial input/speaker discovery shares one response. Explicit inventory
        # refreshes bypass the cache; device calls use the single worker pool.
        if self._features is not None and not refresh:
            return self._features
        features = self._get_json("system/getFeatures")
        system = features.get("system")
        if (isinstance(system, dict) and isinstance(system.get("func_list", []), list)
                and isinstance(system.get("input_list", []), list)
                and all(isinstance(item, dict) for item in system.get("input_list", []))):
            self._features = features
        # Leave malformed capability responses uncached so discovery can retry.
        return features

    def get_speaker_outputs(self):
        features = self._get_features()
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

    def get_inputs(self, *, refresh=False):
        features = self._get_features(refresh=refresh)
        system = features.get("system")
        if (not isinstance(system, dict) or not isinstance(system.get("input_list"), list)
                or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                       or not item["id"].strip() for item in system["input_list"])
                or len({item["id"].strip() for item in system["input_list"]}) != len(system["input_list"])):
            raise DeviceResponseError("Yamaha returned an invalid input inventory.")

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


# ----------------------------
# Cached diagnostics (no device requests)
# ----------------------------


def _state_age(observed_at, now):
    return round(max(0.0, now - observed_at), 3) if observed_at else None


def _connection_failure_category(error):
    while error is not None:
        if isinstance(error, TimeoutError):
            return "timeouts"
        if isinstance(error, URLError) and isinstance(error.reason, TimeoutError):
            return "timeouts"
        error = error.__cause__
    return "connection_failures"


class PollingResponses:
    """Latest successful decoded response per allowlisted read; worker-thread safe."""

    def __init__(self, request_types):
        self._responses = dict.fromkeys(request_types)
        self._counters = {key: dict.fromkeys(("successes", "empty_inventories", "failures",
            "malformed_responses", "connection_failures", "timeouts", "http_errors",
            "device_rejections", "recoveries", "consecutive_failures"), 0) for key in request_types}
        self._lock = threading.Lock()

    def record(self, request_type, response):
        if request_type not in self._responses:
            return  # Never retain command responses, parameters, or headers.
        entry = {"at_utc": datetime.now(timezone.utc).isoformat(),
                 "observed_at": time.monotonic(), "response": copy.deepcopy(response)}
        with self._lock:
            self._responses[request_type] = entry
            counters = self._counters[request_type]
            counters["successes"] += 1
            if counters["consecutive_failures"]:
                counters["recoveries"] += 1
            counters["consecutive_failures"] = 0
            if request_type == "system/getFeatures":
                system = response.get("system")
                empty = isinstance(system, dict) and system.get("input_list") == []
            else:
                empty = False
            if empty:
                counters["empty_inventories"] += 1

    def failed(self, request_type, category):
        if request_type not in self._counters:
            return
        with self._lock:
            counters = self._counters[request_type]
            counters["failures"] += 1
            counters["consecutive_failures"] += 1
            counters[category] += 1
            if category == "timeouts":
                counters["connection_failures"] += 1

    def counters(self):
        with self._lock:
            return copy.deepcopy(self._counters)

    def snapshot(self, now):
        with self._lock:
            result = copy.deepcopy(self._responses)
        for entry in result.values():
            if entry is not None:
                entry["age_seconds"] = _state_age(entry.pop("observed_at"), now)
        return result


class RequestHistory:
    """Bounded GUI-thread outcome summaries; never retain exceptions or payloads."""

    def __init__(self):
        self.groups = {}

    def record(self, group, error=None):
        if group not in ("status", "command", "apps", "inputs", "speakers", "background"):
            group = "background"
        outcome = "failure" if error is not None else "success"
        record = self.groups.setdefault(group, {"successes": 0, "failures": 0,
                                                "recoveries": 0, "consecutive_failures": 0})
        if error is None:
            record["successes"] += 1
            record["recoveries"] += bool(record["consecutive_failures"])
            record["consecutive_failures"] = 0
        else:
            record["failures"] += 1
            record["consecutive_failures"] += 1
        record[outcome] = {"at_utc": datetime.now(timezone.utc).isoformat(),
                           "observed_at": time.monotonic()}
        if error is not None:
            record[outcome]["exception_type"] = type(error).__name__
        record["latest"] = outcome

    def snapshot(self, now):
        result = copy.deepcopy(self.groups)
        for record in result.values():
            for outcome in ("success", "failure"):
                if outcome in record:
                    observed = record[outcome].pop("observed_at")
                    record[outcome]["age_seconds"] = _state_age(observed, now)
        return result


def write_state_dump(snapshot, directories):
    """Write a complete, exclusive report; fall back without overwriting files."""
    payload = json.dumps(snapshot, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    filename = f"{Path(__file__).stem}-state-{stamp}-{os.urandom(4).hex()}.json"
    last_error = None
    for index, directory in enumerate(directories):
        path = Path(directory) / filename
        created = False
        try:
            Path(directory).mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                created = True
                stream.write(payload)
            return {"ok": True, "path": str(path.resolve()), "fallback": index > 0}
        except OSError as error:
            last_error = error
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
    raise last_error or OSError("No diagnostic output directory is available")


class StateDumper(QObject):
    """Capture on the GUI thread; one file write at a time on a separate pool."""

    def __init__(self, parent, snapshot):
        super().__init__(parent)
        self.snapshot = snapshot
        self.worker = None

    def request(self, done, *, full=False):
        if self.worker is not None:
            done({"ok": False, "error": "busy"})
            return
        try:
            snapshot = copy.deepcopy(self.snapshot(full=True) if full else self.snapshot())
            directories = [CONFIG_PATH.parent]
            fallback = QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)
            if fallback:
                directories.append(Path(fallback) / "diagnostics")
            worker = Worker(lambda: write_state_dump(snapshot, directories))
        except Exception as error:
            done({"ok": False, "error": type(error).__name__})
            return
        self.worker = worker
        worker.signals.result.connect(done)
        worker.signals.error.connect(lambda error: done({"ok": False, "error": type(error).__name__}))
        worker.signals.finished.connect(self._finished)
        QThreadPool.globalInstance().start(worker)

    def _finished(self):
        self.worker = None


def _cached_common(window, role, now):
    polling = window.polling
    scheduled = {}
    for group, value in polling._scheduled.items():
        deadline = value[0] if isinstance(value, tuple) else value
        scheduled[group] = round(max(0.0, deadline - now), 3)
    return {
        "report_format_version": 1, "mode": "cached", "fresh_queries_performed": False,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "app": {"name": APP_NAME, "version": __version__, "role": role,
                "python_version": sys.version.split()[0], "qt_version": qVersion(),
                "pyside_version": PYSIDE_VERSION, "platform": sys.platform},
        "window": {"visible": window.isVisible(), "minimized": window.isMinimized(),
                   "active": window.isActiveWindow(), "tray_available": window.tray is not None},
        "lifecycle": {"shutting_down": window._shutting_down, "restart_failed": window._restart_failed,
                      "invalid_config_edit": window._invalid_config_digest is not None,
                      "missing_config_settings": missing_config_settings(window.config)},
        "calibration": {"active": window.calibration_active, "operation_running": window._calibration_operation},
        "polling": {"paused": polling.paused, "periodic_active": polling.periodic.isActive(),
                    "periodic_interval_ms": polling.periodic.interval(),
                    "poll_running": window.poll_running, "scheduled_in_seconds": scheduled,
                    "observed_age_seconds": {key: _state_age(value, now) for key, value in polling._observed.items()},
                    "active_device_workers": len(window._workers)},
        "requests": window.request_history.snapshot(now),
        "discovery": {"paused": window.discovery.paused,
                      "last_inventory_age_seconds": {key: _state_age(value, now) for key, value in window.discovery.checked.items()},
                      "pending_removal_counts": {key: len(value) for key, value in window.discovery.pending.items()}},
        "privacy": {"omitted": ["credentials", "device_addresses", "text_input", "exception_messages",
                                 "request_payloads", "app_and_input_inventories", "configuration_file"]},
    }


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


class FavoriteButton(QPushButton):
    """Keep favorite columns equal; elide only the painted label."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMinimumHeight(34)

    def paintEvent(self, event):
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        contents = self.style().subElementRect(QStyle.SE_PushButtonContents, option, self)
        option.text = self.fontMetrics().elidedText(
            option.text, Qt.ElideRight, max(0, contents.width()), Qt.TextShowMnemonic)
        painter.drawControl(QStyle.CE_PushButton, option)


class CenteredComboBox(QComboBox):
    def __init__(self, parent=None, *, animate_popup=False):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
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

    def _widen_popup(self):
        popup = self.view().window()
        if popup.isVisible():
            popup.setGeometry(popup.geometry().adjusted(-1, 0, 1, 0))
            if popup.layout() is not None:
                popup.layout().activate()

    def showPopup(self):
        if not self._animate_popup:
            super().showPopup()
            self._widen_popup()
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
        self._widen_popup()
        if not animate:
            return  # Respect the user's/system's disabled UI animations.

        view = self.view()
        popup = view.window()
        final_rect = popup.geometry()
        if not popup.isVisible() or final_rect.height() <= 2:
            return

        # Keep Qt's vertical placement, including popups that overlap the combo,
        # and the one-pixel expansion on each side. Never relocate it above/below.
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
        text = self.fontMetrics().elidedText(text, Qt.ElideRight, max(0, rect.width()))
        painter.drawText(rect, Qt.AlignCenter, text)


class CenteredComboDelegate(QStyledItemDelegate):
    def initStyleOption(self, option, index):
        super().initStyleOption(option, index)
        option.displayAlignment = Qt.AlignCenter


# ----------------------------
# Main window
# ----------------------------


def remaining_validity_ms(age_seconds, max_age_seconds):
    """Convert a bounded remaining status lifetime to the IPC wire unit."""
    return int(max(0.0, min(max_age_seconds, max_age_seconds - age_seconds)) * 1000)


# Discovery inventories retain preferences while absent entries are confirmed.
DISCOVERY_INTERVAL_SECONDS = 30.0
REMOVAL_CONFIRM_SECONDS = 5.0


def _inventory_ids(entries, id_key, name_key):
    if not isinstance(entries, list):
        raise DeviceResponseError("Discovery did not return a complete inventory list.")
    ids = []
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get(id_key), str)
                or not entry[id_key].strip() or not isinstance(entry.get(name_key), str)
                or not entry[name_key].strip()):
            raise DeviceResponseError("Discovery returned an invalid inventory entry.")
        ids.append(entry[id_key].strip())
    if len(set(ids)) != len(ids):
        raise DeviceResponseError("Discovery returned duplicate inventory IDs.")
    return set(ids)


class DiscoveryTracker(QObject):
    """Confirm absences with fresh inventories; reuse the ordinary status cadence."""

    def __init__(self, parent, collections, refresh, allowed):
        super().__init__(parent)
        self.collections, self.refresh, self.allowed = collections, refresh, allowed
        self.pending = {}
        self.checked = {}
        self.retry_after = {}
        self.paused = False
        self.verification = QTimer(self)
        self.verification.setSingleShot(True)
        self.verification.timeout.connect(self.tick)

    def observe(self, collection, present, settings, *, allow_missing, observed_at=None):
        now = time.monotonic() if observed_at is None else observed_at
        missing = self.pending.setdefault(collection, {})
        changed = False
        for key, item in settings.items():
            if "Removed" not in item:
                item["Removed"] = False
                changed = True
            if key in present:
                missing.pop(key, None)
                if item["Removed"]:
                    item["Removed"] = False
                    changed = True
            elif allow_missing and not item["Removed"]:
                first = missing.setdefault(key, now)
                if now - first >= REMOVAL_CONFIRM_SECONDS:
                    item["Removed"] = True
                    missing.pop(key, None)
                    changed = True
        if allow_missing or not any(key not in present and not item["Removed"]
                                    for key, item in settings.items()):
            self.checked[collection] = now
        else:
            missing.clear()  # Standby/unknown power cannot establish an absence.
        self._arm()
        return changed

    def tick(self):
        if self.paused:
            return
        now = time.monotonic()
        for collection in self.collections:
            if not self.allowed(collection):
                continue
            pending = self.pending.get(collection, {})
            due = min(pending.values()) + REMOVAL_CONFIRM_SECONDS if pending else float("inf")
            if (now >= self.retry_after.get(collection, 0)
                    and (now >= due or now - self.checked.get(collection, 0) >= DISCOVERY_INTERVAL_SECONDS)):
                self.retry_after[collection] = now + REMOVAL_CONFIRM_SECONDS
                self.refresh(collection)
        self._arm()

    def _arm(self):
        self.verification.stop()
        if self.paused:
            return
        now = time.monotonic()
        deadlines = [max(min(missing.values()) + REMOVAL_CONFIRM_SECONDS,
                         self.retry_after.get(collection, 0))
                     for collection, missing in self.pending.items()
                     if missing and self.allowed(collection)]
        if deadlines:
            self.verification.start(max(1, int(max(0, min(deadlines) - now) * 1000) + 1))

    def pause(self):
        self.paused = True
        self.verification.stop()
        self.pending.clear()
        self.checked.clear()
        self.retry_after.clear()

    def resume(self):
        self.paused = False
        self._arm()


class PollingCoordinator(QObject):
    """Own ordinary polling policy and coalesced refresh scheduling.

    Device queries and their results stay in the window. Command batching
    retains its freshness limits; pending refreshes wait for serialized work.
    """

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.paused = False
        self._scheduled = {}
        self._observed = {}
        self.periodic = QTimer(self)
        self.periodic.timeout.connect(self._periodic_due)
        self.refresh = QTimer(self)
        self.refresh.setSingleShot(True)
        self.refresh.timeout.connect(self._dispatch)

    def allowed(self):
        w = self.window
        background_control = bool(
            bool(w._global_bindings) and w._native_volume_hook is not None
            and w._native_volume_hook.installed)
        return bool(not self.paused and not w._shutting_down and w._polling_initialized
                    and not missing_config_settings(w.config)
                    and ((w.isVisible() and not w.isMinimized()) or background_control
                         or w.calibration_active
                         or bool(w.amplifier_server is not None and w.amplifier_server.clients)))

    def busy(self):
        w = self.window
        return w.poll_running or w.action_running or w._calibration_operation or w._input_discovery_running

    def update(self, refresh=False):
        interval = 250 if self.window.calibration_active else POLL_INTERVAL_MS
        if self.periodic.interval() != interval:
            self.periodic.setInterval(interval)
        if not self.allowed():
            self.periodic.stop()
            self.refresh.stop()
            return
        if not self.periodic.isActive():
            self.periodic.start()
        if refresh:
            self.request()
        elif self.pending:
            self._arm()

    GROUPS = frozenset(("main", "speakers"))
    FRESHNESS_SECONDS = POLL_INTERVAL_MS / 1000 / 2  # Half the normal interval.

    @property
    def pending(self):
        return bool(self._scheduled)

    def invalidate(self, groups=None):
        for group in self.GROUPS if groups is None else groups:
            self._observed.pop(group, None)

    def observed(self, group, observed_at):
        self._observed[group] = max(self._observed.get(group, 0), observed_at)

    def periodic_groups(self):
        if self.window.calibration_active:
            return self.GROUPS
        now = time.monotonic()
        groups = {group for group in self.GROUPS if group not in self._scheduled
                  and now - self._observed.get(group, -math.inf) >= self.FRESHNESS_SECONDS}
        if not self.window._speaker_outputs or self.window._speaker_discovery_running:
            groups.discard("speakers")
        return frozenset(groups)

    def request(self, delay_ms=0, groups=None):
        deadline = time.monotonic() + max(0, delay_ms) / 1000
        for group in self.GROUPS if groups is None else groups:
            self._observed.pop(group, None)
            self._scheduled[group] = min(self._scheduled.get(group, deadline), deadline)
        if delay_ms <= 0:
            self._dispatch()
        else:
            self._arm()

    def _ready_groups(self):
        return {group for group in self._scheduled
                if group != "speakers" or not self.window._speaker_discovery_running}

    def _arm(self):
        self.refresh.stop()
        groups = self._ready_groups()
        if groups and self.allowed() and not self.busy():
            deadline = min(self._scheduled[group] for group in groups)
            self.refresh.start(max(0, math.ceil((deadline - time.monotonic()) * 1000)))

    def _dispatch(self):
        if not self.allowed() or self.busy():
            return
        now = time.monotonic()
        groups = {group for group in self._ready_groups() if self._scheduled[group] <= now}
        for group in groups:
            del self._scheduled[group]
        if groups:
            self.window.poll_status(groups=groups)
        self._arm()

    def _periodic_due(self):
        # Keep the fixed timer. Pending/fresh groups do not defer other reads.
        if self.allowed() and not self.busy():
            groups = self.periodic_groups()
            if groups:
                self.window.poll_status(groups=groups)

    def finished(self):
        self._arm()

    def pause(self):
        self.paused = True
        self._scheduled.clear()
        self.periodic.stop()
        self.refresh.stop()

    def resume(self):
        self.paused = False
        self.update(refresh=True)


class YamahaRemote(QWidget):
    _hotkey_ready = Signal()


    # ----------------------------
    # Initialization
    # ----------------------------

    def __init__(self, config):
        super().__init__()
        self.config = config
        self._shortcut_bindings = _compile_shortcuts(config)
        self._global_bindings = _compile_shortcuts(config, "GlobalShortcuts")
        self._global_native_bindings = {}
        for code, command in self._global_bindings.items():
            native = _native_binding(code)
            self._global_native_bindings[native] = command
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
        self.request_history = RequestHistory()
        self.state_dumper = StateDumper(self, self._cached_state)
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
        self._speaker_state_at = None
        self._speaker_target = None
        self._speaker_grace_deadline = None
        self.input_settings = config["Inputs"]
        self._input_discovery_running = False
        self.discovery = DiscoveryTracker(self, ("Inputs",), self._refresh_discovery, self._can_discover)
        self.favorite_buttons = {}
        self.control_widgets = []
        self._polling_initialized = False
        self._last_status_at = None
        self._hotkey_lock = threading.Lock()
        self._pending_hotkeys = deque()
        self._hotkey_drain_scheduled = False
        self._hotkey_state_refresh_pending = False
        self._hotkey_state = (False, 0, None, False)
        self._focused_tv_overrides = ()
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
        speaker_age = (time.monotonic() - self._speaker_state_at
                       if self._speaker_state_at is not None else HOTKEY_STATE_MAX_AGE_SECONDS)
        state.update(speaker_outputs=list(self._speaker_outputs) if self._speaker_outputs is not None else None,
                     speaker_state=list(self._speaker_state) if self._speaker_state is not None else None,
                     speaker_valid_for_ms=remaining_validity_ms(speaker_age, HOTKEY_STATE_MAX_AGE_SECONDS)
                     if self.connected and self._speaker_state is not None else 0)
        return state

    def _calibration_changed(self, active):
        self._volume_batch_count = 0
        self.calibration_active = active
        self.setEnabled(not active)
        with self._hotkey_lock:
            self._calibration_key_blocked = active
            self._pending_hotkeys.clear()
        self._publish_hotkey_state()
        self._update_polling_state(refresh=True)

    def _calibration_perform(self, action, args, done):
        if self._shutting_down or missing_config_settings(self.config):
            done(False, "Device configuration is missing or the app is shutting down.")
            return
        if action == "set_speakers":
            value = args.get("value")
            if (not self.calibration_active or not self.calibration_server.token
                    or not isinstance(value, list) or len(value) != 2
                    or any(type(enabled) is not bool for enabled in value)
                    or not self._speaker_outputs):
                done(False, "Verified speaker control is unavailable or the request is invalid.")
                return
            self._calibration_operation = True
            def finish_speakers(ok, error=""):
                self._calibration_operation = False
                self.calibration_server.publish()
                done(ok, error)
                self._update_polling_state(refresh=True)
            def succeeded(result):
                state, observed_at = result
                self._speaker_target = None
                self._accept_speaker_state(state, observed_at)
                finish_speakers(True)
            def failed(error):
                self._speaker_state = self._speaker_target = None
                self._speaker_state_at = None
                self._update_speaker_ui()
                self.handle_request_error(error, background=True, command=True)
                finish_speakers(False, str(error))
            self.run_worker(lambda: (self.api.set_speakers(tuple(value), self._speaker_outputs),
                                     time.monotonic()), on_result=succeeded, on_error=failed)
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
        self.polling = PollingCoordinator(self)
        self.poll_timer = self.polling.periodic

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
            self._native_volume_hook = _WindowsVolumeHook(self._accept_native_hotkey, self, resolve=self._resolve_native_volume)
            self._native_volume_hook.changed.connect(self._native_hotkey_changed)
            self._publish_hotkey_state()
            try:
                self._native_volume_hook.start()
            except Exception as error:
                QTimer.singleShot(0, lambda error=error: self._native_hotkey_changed(False, error))
        elif bool(self._global_bindings):
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
            button.setToolTip(_shortcut_tip(self.config, "volume_down" if direction < 0 else "volume_up"))
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
        self.mute_button.setToolTip(_shortcut_tip(self.config, "mute"))
        self.mute_button.clicked.connect(self.toggle_mute)
        self.speaker_combo = self._new_combo()
        self.speaker_combo.setAccessibleName("Speaker outputs")
        self.speaker_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.speaker_combo.setMinimumContentsLength(1)
        self.speaker_combo.setPlaceholderText("Speakers")
        self.speaker_combo.setToolTip("Speaker outputs")
        self.speaker_combo.activated.connect(self.select_speakers)
        row = QGridLayout()
        row.setSpacing(6)
        row.setColumnStretch(0, 1)
        row.setColumnStretch(1, 1)
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
                     _ordered_entries(self.input_settings, self.config["SortByName"]) if settings["Favorite"] and not settings.get("Removed", False)]
        for index, (input_id, settings) in enumerate(favorites):
            button = buttons.pop(input_id, None)
            if button is None:
                button = FavoriteButton()
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
        # Recalculate nested section layouts before locking the new size.
        for layout in reversed(self.findChildren(QLayout)):
            layout.invalidate()
            layout.activate()
        self.adjustSize()
        self.setFixedHeight(self.sizeHint().height())

    def _new_button(self, text):
        button = QPushButton(text)
        button.setMinimumHeight(34)
        return button

    def _apply_style(self):
        # Leave colors and control rendering to the platform's Qt style.
        # Geometry/typography and the optional Windows Mica section surfaces are customized.
        for kind in (QPushButton, QComboBox):
            for control in self.findChildren(kind):
                control.setMinimumHeight(max(34, control.minimumHeight()))
        background = "transparent" if self.mica_enabled else "palette(window)"
        # mica_enabled is only true on supported Windows versions.
        section_fill = ""
        mica_controls = ""
        if self.mica_enabled:
            dark = self.config["DarkMode"]
            surface = "rgba(38,38,38,185)" if dark else "rgba(255,255,255,190)"
            section_border = "rgba(120,120,120,110)" if dark else "rgba(0,0,0,25)"
            section_fill = f"background: {surface}; border-color: {section_border};"
            normal, hover, pressed = (("48,48,48", "68,68,68", "38,38,38") if dark else
                                      ("255,255,255", "245,245,245", "235,235,235"))
            text = "235,235,235" if dark else "20,20,20"
            border = "rgba(120,120,120,150)" if dark else "rgba(0,0,0,35)"
            menu = "40,40,40" if dark else "250,250,250"
            menu_border = "120,120,120" if dark else "180,180,180"
            selection = "80,110,170" if dark else "210,225,250"
            # Opaque control surfaces prevent native Mica treatment showing through.
            mica_controls = f"""
                QPushButton, QComboBox, QTextEdit {{
                    background-color: rgb({normal});
                    color: rgb({text});
                    border: 1px solid {border};
                    border-radius: 7px;
                    padding: 5px 7px;
                }}
                QComboBox::drop-down {{
                    border: none; background: transparent; width: 20px;
                }}
                QPushButton:hover, QComboBox:hover {{ background-color: rgb({hover}); }}
                QPushButton:pressed {{ background-color: rgb({pressed}); }}
                QPushButton:disabled, QComboBox:disabled, QTextEdit:disabled {{
                    background-color: rgb({normal});
                    color: rgba({text},100);
                }}
                QComboBox QAbstractItemView {{
                    background-color: rgb({menu});
                    color: rgb({text});
                    selection-background-color: rgb({selection});
                    selection-color: rgb({text});
                    border: 1px solid rgb({menu_border});
                }}
            """
        self.setStyleSheet("""
            ROOT_SELECTOR { background: BACKGROUND; }
            QWidget { font-size: 9pt; }
            QFrame#section {
                border: 1px solid palette(mid);
                border-radius: 8px;
                SECTION_FILL
            }
            QPushButton, QComboBox { padding: 5px 7px; }
            QLabel { background: transparent; }
            QLabel#sectionTitle { font-weight: 600; }
            QLabel#volumeValue { font-size: 13pt; font-weight: 600; }
            MICA_CONTROLS
        """.replace("ROOT_SELECTOR", "QWidget#remoteRoot").replace("BACKGROUND", background).replace("SECTION_FILL", section_fill).replace("MICA_CONTROLS", mica_controls))

    # ----------------------------
    # Background work
    # ----------------------------

    def run_worker(self, function, on_result=None, on_error=None, on_finished=None, *, request_group="background"):
        if self._shutting_down or missing_config_settings(self.config):
            return
        worker = Worker(function)
        self._workers.add(worker)
        generation = self._worker_generation

        def deliver_result(result):
            if not self._shutting_down and generation == self._worker_generation:
                self.request_history.record(request_group)
                if on_result:
                    on_result(result)

        def deliver_error(error):
            if not self._shutting_down and generation == self._worker_generation:
                self.request_history.record(request_group, error)
                (on_error or self.handle_request_error)(error)

        def finished():
            self._workers.discard(worker)
            if not self._shutting_down and generation == self._worker_generation and on_finished:
                on_finished()

        worker.signals.result.connect(deliver_result)
        worker.signals.error.connect(deliver_error)
        worker.signals.finished.connect(finished)
        self.pool.start(worker)

    def run_action(self, action, *, background=False, batch_volume=False, on_result=None, on_error=None,
                   refresh_groups=None):
        if self._shutting_down or self.calibration_active:
            return False

        if self.action_running or not self.connected:
            return False

        groups = self.polling.GROUPS if refresh_groups is None else frozenset(refresh_groups)
        # action_running reserves the serialized device worker immediately.
        self.polling.invalidate(groups)
        self.action_running = True

        def done(result):
            self.action_running = False
            if on_result:
                on_result(result)
            if batch_volume:
                self._volume_batch_count += 1
            if not batch_volume or not self._continue_volume_batch():
                self._request_poll(groups=groups)
            self.polling.finished()
            self._schedule_hotkey_drain()

        def failed(error):
            self.action_running = False
            if on_error:
                on_error(error)
            else:
                self.handle_request_error(error, background=background, command=True)
            self._request_poll(groups=groups)
            self.polling.finished()
            self._schedule_hotkey_drain()

        self.run_worker(action, on_result=done, on_error=failed, request_group="command")
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

    def _can_discover(self, collection):
        return bool(not self._shutting_down and not self.calibration_active
                    and not self._calibration_operation and self.connected
                    and (self.last_status or {}).get("power") == "on"
                    and self._last_status_at is not None
                    and time.monotonic() - self._last_status_at < HOTKEY_STATE_MAX_AGE_SECONDS
                    and self.isVisible() and not self.isMinimized() and self.polling.allowed())

    def _refresh_discovery(self, collection):
        if self._can_discover(collection) and not self.polling.busy() and not self._pending_hotkeys:
            self.load_inputs(refresh=True)

    def load_inputs(self, *, refresh=False):
        if self._shutting_down or self.calibration_active or self._input_discovery_running:
            return
        if self.initialized and not refresh:
            self._request_poll()
            return
        self._input_discovery_running = True
        def query():
            inputs = self.api.get_inputs(refresh=True) if refresh else self.api.get_inputs()
            _inventory_ids(inputs, "Id", "Label")
            return inputs, time.monotonic()
        def done(result):
            inputs, observed_at = result
            config_changed = False
            for item in inputs:
                input_id, name = item["Id"].strip(), item["Label"].strip()
                settings = self.input_settings.get(input_id)
                if settings is None:
                    self.input_settings[input_id] = {"Name": name, "NameOverride": "",
                        "Hidden": False, "Favorite": False, "Removed": False}
                    config_changed = True
                elif settings["Name"] != name:
                    settings["Name"] = name
                    config_changed = True
            config_changed = self.discovery.observe(
                "Inputs", {item["Id"].strip() for item in inputs}, self.input_settings,
                allow_missing=self._can_discover("Inputs"), observed_at=observed_at) or config_changed
            if config_changed and not self._restart_failed:
                try:
                    self._save_config_without_restart()
                except ConfigError as ex:
                    record_exception(ex, "Configuration save")
                    QMessageBox.warning(self, "Yamaha Remote - Configuration Warning", str(ex))
            first = not self.initialized
            if config_changed or first:
                self.input_combo.hidePopup()
                self.inputs = [{"Id": key, "Label": _entry_display_name(settings)}
                    for key, settings in _ordered_entries(self.input_settings, self.config["SortByName"])
                    if not settings["Hidden"] and not settings.get("Removed", False)]
                self._build_favorites_from_config()
                self.input_combo.blockSignals(True)
                self.input_combo.clear()
                for item in self.inputs:
                    self.input_combo.addItem(item["Label"], item["Id"])
                    self.input_combo.setItemData(self.input_combo.count()-1, Qt.AlignCenter, Qt.TextAlignmentRole)
                self.input_combo.blockSignals(False)
                self._fit_and_lock_window()
            self.initialized = True
            self.update_control_states()
            if self.last_status:
                self.show_cached_status()
            if first:
                self._request_poll()
        def finished():
            self._input_discovery_running = False
            self.polling.finished()
        accepted = self.run_worker(query, on_result=done,
            on_error=lambda error: self.handle_request_error(error, background=True), on_finished=finished, request_group="inputs")
        if accepted is False:
            self._input_discovery_running = False

    def load_speaker_outputs(self):
        if self._shutting_down or self._speaker_outputs is not None or self._speaker_discovery_running:
            return
        self._speaker_discovery_running = True
        def query():
            outputs = self.api.get_speaker_outputs()
            state = observed_at = error = None
            if outputs:
                try:
                    state = self.api.get_speaker_state(outputs)
                    observed_at = time.monotonic()
                except Exception as exc:
                    error = exc
            return outputs, state, observed_at, error
        def done(result):
            outputs, initial_state, observed_at, error = result
            self._speaker_outputs = outputs
            self.speaker_combo.clear()
            for label, state in (("Off", (False, False)), ("A", (True, False)),
                                 ("B", (False, True)), ("A+B", (True, True))):
                if all(not enabled or name in outputs for name, enabled in zip(("a", "b"), state)):
                    self.speaker_combo.addItem(label, state)
            if not outputs:
                self.speaker_combo.clear()
                self.speaker_combo.setPlaceholderText("Unavailable")
                self.speaker_combo.setToolTip("No controllable speaker outputs are advertised by this amplifier.")
            if error is not None:
                self._speaker_read_failed(error)
            elif initial_state is not None:
                self._accept_speaker_state(initial_state, observed_at)
            else:
                self._update_speaker_ui()
            self.update_control_states()
        self.run_worker(
            query, on_result=done,
            on_error=self._speaker_read_failed,
            on_finished=self._speaker_discovery_finished,
        )

    def _speaker_discovery_finished(self):
        self._speaker_discovery_running = False
        self.polling.finished()

    def _speaker_read_failed(self, error):
        self.request_history.record("speakers", error)
        self.polling.invalidate({"speakers"})
        record_unexpected_exception(error, "Speaker status", background=True)
        self._speaker_state = None
        self._speaker_state_at = None
        self.speaker_combo.setToolTip(f"Speaker status unavailable: {error}")
        self._update_speaker_ui()
        self.speaker_combo.setEnabled(False)

    def _accept_speaker_state(self, state, observed_at=None):
        self.request_history.record("speakers")
        self._speaker_state = state
        self._speaker_state_at = time.monotonic() if observed_at is None else observed_at
        self.polling.observed("speakers", self._speaker_state_at)
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
            self.speaker_combo.setPlaceholderText("Speakers")

    def select_speakers(self, index):
        target = self.speaker_combo.itemData(index)
        if not isinstance(target, tuple) or self.calibration_active:
            self._update_speaker_ui()
            return
        def done(result):
            state, observed_at = result
            self._speaker_state = state
            self._speaker_state_at = observed_at
            self.polling.observed("speakers", observed_at)
            self._speaker_grace_deadline = time.monotonic() + SPEAKER_STATUS_GRACE_SECONDS
            self._update_speaker_ui()
        def failed(error):
            self.polling.invalidate({"speakers"})
            self._speaker_target = None
            self._speaker_state = None
            self._update_speaker_ui()
            self.handle_request_error(error, command=True)
        def change():
            state = self.api.set_speakers(target, self._speaker_outputs)
            return state, time.monotonic()
        if self.run_action(change, on_result=done, on_error=failed, refresh_groups={"main"}):
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

    def poll_status(self, groups=None):
        if not self.polling.allowed():
            return

        if self.polling.busy():
            return

        groups = self.polling.GROUPS if groups is None else frozenset(groups)
        self.poll_running = True

        speaker_state = speaker_error = speaker_observed_at = None
        outputs = self._speaker_outputs
        cached_status = self.last_status
        def query():
            nonlocal speaker_state, speaker_error, speaker_observed_at
            read_main = "main" in groups or cached_status is None
            status = self.api.get_status() if read_main else cached_status
            observed_at = time.monotonic() if read_main else None
            if "speakers" in groups and outputs and status.get("power") == "on":
                try:
                    speaker_state = self.api.get_speaker_state(outputs)
                    speaker_observed_at = time.monotonic()
                except Exception as error:
                    speaker_error = error
            return status, observed_at

        def done(result):
            status, observed_at = result
            self.poll_running = False
            if observed_at is not None:
                self.clear_startup_status()
                self.connected = True
                self.power_button.setToolTip("")
                self.last_status = status
                self._volume_batch_count = 0
                self._status_sequence += 1
                self._last_status_at = observed_at
                self._publish_hotkey_state()
                self.show_cached_status()
                self.polling.observed("main", observed_at)
            if speaker_error is not None:
                self._speaker_read_failed(speaker_error)
            elif speaker_state is not None:
                self._accept_speaker_state(speaker_state, speaker_observed_at)
            elif status.get("power") != "on":
                self._speaker_state = self._speaker_target = None
                self._update_speaker_ui()
            self.update_control_states()
            if self._speaker_outputs is None:
                self.load_speaker_outputs()

            if not self.initialized:
                self.load_inputs()
            self._schedule_hotkey_drain()
            self.discovery.tick()

        def failed(error):
            self.poll_running = False
            self.handle_request_error(error, background=True)

        self.run_worker(query, on_result=done, on_error=failed,
                        on_finished=self.polling.finished, request_group="status")

    def _request_poll(self, delay_ms=0, groups=None):
        self.polling.request(delay_ms, groups)

    def _update_polling_state(self, refresh=False):
        if hasattr(self, "polling"):
            self.polling.update(refresh=refresh)

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
        self.polling.invalidate()
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
        self.polling.invalidate()
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
            lambda: self.api.set_mute(enabled), background=background, refresh_groups={"main"}
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

        self.run_action(action, background=background, batch_volume=batch_volume, refresh_groups={"main"})

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
            self._ipc_clients_changed, self,
        )
        if not self.amplifier_server.available:
            record_exception(RuntimeError("Local amplifier endpoint unavailable"), "Local amplifier IPC")

    def _ipc_clients_changed(self):
        self._publish_hotkey_state()
        self._update_polling_state(refresh=True)

    def _focused_tv_grant(self, context):
        # GUI-thread policy. Input identity comes from the TV peer, never its JSON.
        if (context is None or self.calibration_active or not self.global_volume_hotkeys_active):
            return None
        window, bindings, tv_input, deadline = context
        current_input = (self.last_status or {}).get("input")
        valid_for = max(0, min(IPC_MAX_TV_CONTEXT_VALIDITY_MS, int((deadline - time.monotonic()) * 1000)))
        if not valid_for or not current_input or current_input == tv_input:
            return None
        candidates = set()
        for code in bindings:
            try:
                candidates.add(_native_binding(code))
            except ConfigError:
                continue
        granted = [code for code in self._active_global_codes() if _native_binding(code) in candidates]
        if not granted:
            return None
        return {"window": window, "shortcuts": granted, "valid_for_ms": valid_for}

    def _amplifier_snapshot(self):
        status = self.last_status or {}
        age = (time.monotonic() - self._last_status_at
               if self._last_status_at is not None else HOTKEY_STATE_MAX_AGE_SECONDS)
        valid_for = remaining_validity_ms(age, HOTKEY_STATE_MAX_AGE_SECONDS)
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
                "global_hotkeys_enabled": bool(self._global_bindings),
                "global_hotkeys_active": self.global_volume_hotkeys_active,
                "global_shortcuts": self._active_global_codes(),
                "focused_volume_override_supported": True,
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

    def _active_global_codes(self):
        if not self.global_volume_hotkeys_active:
            return []
        return [code | Qt.KeypadModifier.value if code & 0x01FFFFFF == Qt.Key_Enter.value else code
                for code in self._global_bindings]

    @property
    def global_volume_hotkeys_active(self):
        return bool(self.calibration_active or (
            bool(self._global_bindings)
            and self._native_volume_hook is not None and self._native_volume_hook.installed
            and self._volume_hotkeys_eligible()
            and QApplication.activeModalWidget() is None
            and QApplication.activePopupWidget() is None
        ))

    def _publish_hotkey_state(self):
        # The native hook thread reads only this plain-Python snapshot. Qt
        # focus/modal queries stay on the GUI thread.
        if not hasattr(self, "_hotkey_lock"):
            return
        state = (
            self._volume_hotkeys_eligible(),
            int(self.winId()) if sys.platform == "win32" and self.isVisible() and not self.isMinimized() else 0,
            self._last_status_at, (QApplication.activeModalWidget() is not None or QApplication.activePopupWidget() is not None),
        )
        overrides = []
        if self.amplifier_server is not None:
            for context in self.amplifier_server.focused_volume.values():
                grant = self._focused_tv_grant(context)
                if grant:
                    overrides.append((grant["window"], context[3],
                                      frozenset(_native_binding(code) for code in grant["shortcuts"])))
        with self._hotkey_lock:
            self._focused_tv_overrides = tuple(overrides)
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

    def _resolve_native_volume(self, key, modifiers):
        if self._calibration_key_blocked:
            return _VOLUME_KEYS.get(key)
        eligible, _, observed_at, blocked = self._hotkey_state
        if (eligible and not blocked and observed_at is not None
                and time.monotonic() - observed_at <= HOTKEY_STATE_MAX_AGE_SECONDS):
            binding = self._global_native_bindings.get((key, modifiers))
            if binding:
                return ("global", (key, modifiers), binding)
        # Only physical volume keys need a native focused fallback.
        qt_key = _NATIVE_QT_VOLUME_KEYS.get(key)
        return self._shortcut_bindings.get(qt_key | modifiers) if qt_key else None

    def _accept_native_hotkey(self, command, foreground):
        with self._hotkey_lock:
            if self._calibration_key_blocked:
                return command in _VOLUME_KEYS.values()
            global_binding = isinstance(command, tuple)
            action = command[-1] if global_binding else command
            eligible, window, observed_at, blocked = self._hotkey_state
            if (action not in _VOLUME_KEYS.values() or not eligible or blocked or observed_at is None
                    or time.monotonic() - observed_at > HOTKEY_STATE_MAX_AGE_SECONDS
                    or (not global_binding and (not window or foreground != window))):
                return False
            if global_binding and any(
                    foreground == target and time.monotonic() < deadline and command[1] in bindings
                    for target, deadline, bindings in self._focused_tv_overrides):
                return False
            self._enqueue_hotkey_locked(action)
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
                or QApplication.activePopupWidget() is not None
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
            self._request_poll(groups={"main"})
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
            if watched is self and event_type in (QEvent.Hide, QEvent.WindowDeactivate):
                self._qt_volume_keys.clear()
            if not self._hotkey_state_refresh_pending and not self._shutting_down:
                self._hotkey_state_refresh_pending = True
                QTimer.singleShot(0, self._refresh_hotkey_state)
        if event_type not in (QEvent.ShortcutOverride, QEvent.KeyPress, QEvent.KeyRelease):
            return super().eventFilter(watched, event)
        if event_type == QEvent.KeyRelease:
            owned = (self._qt_volume_keys.pressed.get(event.key(), False) if event.isAutoRepeat()
                     else self._qt_volume_keys.release(event.key()))
            if owned:
                event.accept()
                return True
            return super().eventFilter(watched, event)
        command = _focused_shortcut_command(self._shortcut_bindings, event)
        # Windows' native hook already handles both focused and global keys.
        if ((event.key() in _NATIVE_QT_VOLUME_KEYS.values() and self._native_volume_hook is not None
             and self._native_volume_hook.installed)
                or QApplication.activePopupWidget() is not None):
            return super().eventFilter(watched, event)
        if event_type == QEvent.ShortcutOverride:
            if (command and self.isActiveWindow() and self._volume_hotkeys_eligible()
                    and QApplication.activeModalWidget() is None):
                event.accept()
                return True
            return super().eventFilter(watched, event)
        if command is None:
            return super().eventFilter(watched, event)
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
            launch_replacement(self)
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

    def _cached_state(self, *, full=False):
        now = time.monotonic()
        report = _cached_common(self, "amplifier", now)
        report["polling_counters"] = self.api.polling_responses.counters()
        if full:
            report["mode"] = "cached_full"
            report["polling_responses"] = self.api.polling_responses.snapshot(now)
            report["privacy"]["warning"] = "Full device responses may contain device/network identifiers and app inventories. Review before sharing."
            report["privacy"]["omitted"] = ["credentials", "request_headers", "submitted_text", "request_parameters", "exception_messages", "configuration_file"]
        status = self.last_status or {}
        report["device"] = {"connected": self.connected,
            "status_age_seconds": _state_age(self._last_status_at, now),
            "state": {key: status.get(key) for key in ("power", "input", "volume", "max_volume", "mute")},
            "speakers": {"available_outputs": list(self._speaker_outputs or ()), "observed": self._speaker_state,
                         "target": self._speaker_target, "age_seconds": _state_age(self._speaker_state_at, now),
                         "grace_remaining_seconds": max(0, (self._speaker_grace_deadline or now)-now)}}
        with self._hotkey_lock:
            pending = len(self._pending_hotkeys)
            overrides = [{"window": window, "remaining_seconds": round(max(0, deadline-now),3),
                          "binding_count": len(bindings)} for window, deadline, bindings in self._focused_tv_overrides]
        report["hotkeys"] = {"focused_configured": copy.deepcopy(self.config["Shortcuts"]),
            "global_configured": copy.deepcopy(self.config["GlobalShortcuts"]),
            "global_active": self.global_volume_hotkeys_active, "active_global_bindings": self._active_global_codes(),
            "eligible": self._volume_hotkeys_eligible(),
            "native_hook_installed": bool(self._native_volume_hook and self._native_volume_hook.installed),
            "pending_commands": pending, "drain_scheduled": self._hotkey_drain_scheduled,
            "calibration_key_blocked": self._calibration_key_blocked, "focused_tv_overrides": overrides}
        server = self.amplifier_server
        report["ipc"] = {"server_available": bool(server and server.available),
                         "connected_peers": len(server.clients) if server else 0,
                         "focused_volume_contexts": len(server.focused_volume) if server else 0}
        report["routing"] = {"volume_destination": "amplifier", "input": status.get("input"),
                             "global_eligible": self._volume_hotkeys_eligible()}
        report["volume_commands"] = {"action_running": self.action_running,
                                     "batch_count": self._volume_batch_count, "pending_count": pending}
        return report

    def _setup_activation(self):
        if self.activation_server is not None:
            self.activation_server.deleteLater()
        self.activation_server = ActivationServer(
            "amplifier", self._activate_existing_window, self, dump=self.state_dumper.request,
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
        self.discovery.pause()
        self.polling.pause()
        if self.activation_server is not None:
            self.activation_server.close()
        if self.calibration_server is not None:
            self.calibration_server.close()
        self._publish_hotkey_state()
        self.amplifier_server.close()
        self._qt_volume_keys.clear()
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
        self._input_discovery_running = False
        self._speaker_target = None
        self._update_speaker_ui()
        return active_timers

    def _resume_background_work(self, active_timers):
        self._shutting_down = False
        self.discovery.resume()
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
        self.polling.resume()

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

        self._publish_hotkey_state()
        self._update_polling_state(refresh=True)

    def hideEvent(self, event):
        super().hideEvent(event)
        self._publish_hotkey_state()
        self._qt_volume_keys.clear()
        self._update_polling_state()

    def changeEvent(self, event):
        super().changeEvent(event)
        self._publish_hotkey_state()
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow():
            self._qt_volume_keys.clear()
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


def restart_window_arguments(window):
    # One-shot process arguments, never persisted to the user's configuration.
    state = "tray" if not window.isVisible() else "minimized" if window.isMinimized() else "visible"
    return [f"--restart-window-state={state}",
            "--restart-window-geometry=" + bytes(window.saveGeometry()).hex()]


def show_startup_window(window, config):
    state = next((arg.split("=", 1)[1] for arg in sys.argv[1:]
                  if arg.startswith("--restart-window-state=")), None)
    geometry = next((arg.split("=", 1)[1] for arg in sys.argv[1:]
                     if arg.startswith("--restart-window-geometry=")), None)
    if geometry:
        try:
            # Qt also adjusts saved geometry when the original screen is unavailable.
            window.restoreGeometry(QByteArray(bytes.fromhex(geometry)))
        except ValueError:
            pass
    tray_available = (config["SystemTray"] and config["CloseToTray"]
                      and window.tray is not None and QSystemTrayIcon.isSystemTrayAvailable())
    if tray_available and (state == "tray" or
                           (state is None and "--start-in-tray" in sys.argv[1:])):
        window._update_polling_state()
    elif state == "minimized":
        window.showMinimized()
    else:
        window.show()


def launch_replacement(window=None):
    script_path = Path(__file__).resolve()
    arguments = [arg for arg in sys.argv[1:]
                 if not arg.startswith(("--restart-window-state=", "--restart-window-geometry="))]
    if window is not None:
        arguments.extend(restart_window_arguments(window))
    if not getattr(sys, "frozen", False):
        arguments.insert(0, str(script_path))
    result = QProcess.startDetached(sys.executable, arguments, str(script_path.parent))
    started = result[0] if isinstance(result, tuple) else bool(result)
    if not started:
        raise OSError("The replacement process could not be started.")


def check_qt_theme_support():
    if hasattr(QStyleHints, "setColorScheme"):
        return True
    command = 'py' if sys.platform == "win32" else 'python3'
    QMessageBox.critical(
        None, APP_NAME,
        "PySide6 6.8 or newer is required for light/dark appearance.\n\n"
        f'Upgrade with:\n{command} -m pip install --upgrade "PySide6>=6.8"')
    return False


def configure_app_theme(config):
    # Qt 6.8+ forwards this override to supported platform theme backends.
    QApplication.styleHints().setColorScheme(
        Qt.ColorScheme.Dark if config["DarkMode"] else Qt.ColorScheme.Light)


def _run_application():
    app = QApplication(sys.argv)
    if "--dump-state-full" in sys.argv[1:]:
        return dump_existing_state("amplifier", full=True)
    if "--dump-state" in sys.argv[1:]:
        return dump_existing_state("amplifier")
    if not check_qt_theme_support():
        return 1
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

    configure_app_theme(config)
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
    # Refresh Qt palette roles without changing the explicit JSON light/dark selection.
    app.paletteChanged.connect(lambda palette: window._apply_style())
    show_missing_config_settings(config)
    show_startup_window(window, config)

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
