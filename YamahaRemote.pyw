import ctypes
import ipaddress
import json
import math
import os
import re
import sys
import time
from decimal import Decimal
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen

from PySide6.QtCore import QFileInfo, QEvent, QObject, QPoint, QLockFile, QRunnable, QStandardPaths, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QCursor, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileIconProvider,
    QGridLayout,
    QHBoxLayout,
    QMenu,
    QLabel,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QToolTip,
    QWidget,
)

# ----------------------------
# Config
# ----------------------------

CONFIG_FILE = "YamahaRemote.json"


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


def _require_number(config, key):
    if key not in config:
        raise ConfigError(
            f'"{key}" is required when "VolumeDb" is true.'
        )

    return _validate_number(config[key], key)


def _validate_host(value):
    if not isinstance(value, str):
        raise ConfigError(
            '"Host" must be a string.'
        )

    host = value.strip()

    if not host:
        raise ConfigError(
            '"Host" must not be empty.'
        )

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


def _validate_favorites(value):
    if not isinstance(value, list):
        raise ConfigError(
            '"FavoriteInputs" must be a list.'
        )

    normalized = []

    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ConfigError(
                f'"FavoriteInputs" item {index} must be an object.'
            )

        unknown_keys = set(item) - {
            "Label",
            "Input",
        }

        if unknown_keys:
            names = ", ".join(sorted(unknown_keys))
            raise ConfigError(
                f'"FavoriteInputs" item {index} has unknown '
                f'field(s): {names}.'
            )

        label = item.get("Label")
        input_id = item.get("Input")

        if not isinstance(label, str) or not label.strip():
            raise ConfigError(
                f'"FavoriteInputs" item {index} must have a '
                'non-empty string "Label".'
            )

        if not isinstance(input_id, str) or not input_id.strip():
            raise ConfigError(
                f'"FavoriteInputs" item {index} must have a '
                'non-empty string "Input".'
            )

        normalized.append(
            {
                "Label": label.strip(),
                "Input": input_id.strip(),
            }
        )

    return normalized


def _validate_input_name_overrides(value):
    if not isinstance(value, dict):
        raise ConfigError(
            '"InputNameOverrides" must be an object.'
        )

    normalized = {}

    for input_id, label in value.items():
        if not isinstance(input_id, str) or not input_id.strip():
            raise ConfigError(
                '"InputNameOverrides" input IDs must be non-empty strings.'
            )

        if not isinstance(label, str) or not label.strip():
            raise ConfigError(
                f'"InputNameOverrides" value for {input_id!r} must be '
                'a non-empty string.'
            )

        normalized[input_id.strip()] = label.strip()

    return normalized


def _validate_hidden_inputs(value):
    if not isinstance(value, list):
        raise ConfigError(
            '"HiddenInputs" must be a list.'
        )

    normalized = []

    for index, input_id in enumerate(value, start=1):
        if not isinstance(input_id, str) or not input_id.strip():
            raise ConfigError(
                f'"HiddenInputs" item {index} must be a non-empty string.'
            )

        normalized.append(input_id.strip())

    return normalized


def validate_config(raw_config):
    if not isinstance(raw_config, dict):
        raise ConfigError(
            "The configuration root must be a JSON object."
        )

    config = dict(raw_config)

    config["Host"] = _validate_host(
        config.get("Host")
    )
    config["DarkMode"] = _require_bool(
        config,
        "DarkMode",
        True,
    )
    config["Mica"] = _require_bool(
        config,
        "Mica",
        False,
    )
    config["SystemTray"] = _require_bool(
        config,
        "SystemTray",
        False,
    )
    config["CloseToTray"] = _require_bool(
        config,
        "CloseToTray",
        False,
    )
    config["VolumeDb"] = _require_bool(
        config,
        "VolumeDb",
        False,
    )

    if config["VolumeDb"]:
        config["VolumeDbAtZero"] = _require_number(
            config,
            "VolumeDbAtZero",
        )
        config["VolumeDbStepSize"] = _require_number(
            config,
            "VolumeDbStepSize",
        )

        if config["VolumeDbStepSize"] <= 0:
            raise ConfigError(
                '"VolumeDbStepSize" must be greater than 0.'
            )
    else:
        if "VolumeDbAtZero" in config:
            config["VolumeDbAtZero"] = _validate_number(
                config["VolumeDbAtZero"],
                "VolumeDbAtZero",
            )

        if "VolumeDbStepSize" in config:
            config["VolumeDbStepSize"] = _validate_number(
                config["VolumeDbStepSize"],
                "VolumeDbStepSize",
            )

            if config["VolumeDbStepSize"] <= 0:
                raise ConfigError(
                    '"VolumeDbStepSize" must be greater than 0.'
                )

    if config["CloseToTray"] and not config["SystemTray"]:
        raise ConfigError(
            '"CloseToTray" can only be true when '
            '"SystemTray" is also true.'
        )

    config["FavoriteInputs"] = _validate_favorites(
        config.get("FavoriteInputs", [])
    )
    config["InputNameOverrides"] = _validate_input_name_overrides(
        config.get("InputNameOverrides", {})
    )
    config["HiddenInputs"] = _validate_hidden_inputs(
        config.get("HiddenInputs", [])
    )

    allowed_keys = {
        "Host",
        "DarkMode",
        "Mica",
        "SystemTray",
        "CloseToTray",
        "VolumeDb",
        "VolumeDbAtZero",
        "VolumeDbStepSize",
        "FavoriteInputs",
        "InputNameOverrides",
        "HiddenInputs",
    }

    unknown = set(config) - allowed_keys

    if unknown:
        names = ", ".join(sorted(unknown))
        raise ConfigError(
            "Unknown configuration setting(s): "
            f"{names}."
        )

    return config


def load_config():
    script_path = Path(__file__).resolve()

    config_path = script_path.with_name(CONFIG_FILE)

    if not config_path.exists():
        raise ConfigError(
            f"{CONFIG_FILE} was not found next to "
            f"{script_path.name}."
        )

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
    except OSError as ex:
        raise ConfigError(
            f"Unable to read {config_path.name}: {ex}"
        ) from ex

    return validate_config(raw_config)


# ----------------------------
# Platform integration
# ----------------------------

def set_app_user_model_id(app_id):
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


# ----------------------------
# Windows Mica integration
# ----------------------------
# Copy-paste friendly: this block only depends on the standard-library
# ``ctypes`` and ``sys`` modules. Pass it a native Windows HWND, for example
# ``int(widget.winId())`` in PySide6.
#
# The Mica implementation is inspired by win32mica (MIT licensed), but uses
# the documented Windows 11 system-backdrop API on current builds. The legacy
# attribute is retained only for early Windows 11 builds.

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

# End of copy-pasteable Windows Mica integration block.

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


# ----------------------------
# Yamaha communication
# ----------------------------

class YamahaConnectionError(RuntimeError):
    """Raised when the Yamaha device cannot be reached."""


def _decimal_places(value):
    exponent = Decimal(str(value)).normalize().as_tuple().exponent
    return max(0, -exponent)


class YamahaBackend:
    def __init__(self, host, config=None):
        self.host = host
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

    def _get_json(self, path, timeout=2):
        try:
            with urlopen(
                f"{self.base_url}/{path}",
                timeout=timeout,
            ) as response:
                data = json.loads(
                    response.read().decode("utf-8")
                )
        except (URLError, TimeoutError, OSError) as ex:
            raise YamahaConnectionError(
                "Unable to reach the Yamaha."
            ) from ex

        response_code = data.get("response_code")
        if (
            response_code is not None
            and response_code != 0
        ):
            raise RuntimeError(
                "Yamaha returned response code "
                f"{response_code}."
            )

        return data

    def get_status(self, timeout=2):
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
        except Exception:
            pass

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

        inputs.sort(
            key=lambda x: x["Label"].lower()
        )
        return inputs

    def preview_volume(self, volume, direction):
        try:
            return int(volume) + direction
        except (TypeError, ValueError):
            return volume

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
# Background work
# ----------------------------

class WorkerSignals(QObject):
    completed = Signal(object, object)


class Worker(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    def run(self):
        try:
            result = self.function()
            self.signals.completed.emit(result, None)
        except Exception as ex:
            self.signals.completed.emit(None, ex)


# ----------------------------
# UI
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


class DelayedInputTooltip(QObject):
    def __init__(self, combo, delay_ms=2000):
        super().__init__(combo)

        self.combo = combo
        self.view = combo.view()
        self.viewport = self.view.viewport()
        self.hovered_row = -1

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(delay_ms)
        self.timer.timeout.connect(self.show_tooltip)

        self.viewport.setMouseTracking(True)
        self.viewport.installEventFilter(self)
        self.view.installEventFilter(self)

    def reset(self):
        self.timer.stop()
        self.hovered_row = -1
        QToolTip.hideText()

    def update_hovered_row(self, position):
        index = self.view.indexAt(position)
        row = index.row() if index.isValid() else -1

        if row == self.hovered_row:
            return

        self.timer.stop()
        QToolTip.hideText()
        self.hovered_row = row

        if row >= 0:
            self.timer.start()

    def show_tooltip(self):
        if self.hovered_row < 0 or not self.view.isVisible():
            return

        input_id = self.combo.itemData(self.hovered_row)

        if input_id is None:
            return

        QToolTip.showText(
            QCursor.pos() + QPoint(12, 18),
            f"Input ID: {input_id}",
            self.viewport,
        )

    def eventFilter(self, watched, event):
        event_type = event.type()

        if watched is self.viewport:
            if event_type == QEvent.MouseMove:
                self.update_hovered_row(
                    event.position().toPoint()
                )
            elif event_type == QEvent.Enter:
                self.update_hovered_row(
                    self.viewport.mapFromGlobal(QCursor.pos())
                )
            elif event_type in (QEvent.Leave, QEvent.Hide):
                self.reset()

        elif watched is self.view and event_type == QEvent.Hide:
            self.reset()

        return False


class YamahaRemote(QWidget):
    def __init__(self, config):
        super().__init__()

        self.dark_mode = config["DarkMode"]
        self.mica_enabled = bool(
            config["Mica"]
            and supports_mica()
        )
        self.system_tray_enabled = config["SystemTray"]
        self.close_to_tray = config["CloseToTray"]
        self.force_exit = False
        self.tray_icon = None
        self.backend = YamahaBackend(config["Host"], config)
        self.favorite_inputs = config["FavoriteInputs"]
        self.input_name_overrides = config["InputNameOverrides"]
        self.hidden_inputs = set(config["HiddenInputs"])

        self.last_status = None
        self.inputs = []
        self.connected = False
        self.refresh_running = False
        self.action_running = False
        self.initialized_inputs = False
        self.closing = False

        # Keep receiver communication serialized. Embedded receiver APIs often
        # expect one request stream at a time, and retaining the workers also
        # avoids PySide lifetime races.
        self.thread_pool = QThreadPool(self)
        self.thread_pool.setMaxThreadCount(1)
        self.active_workers = set()

        self.control_widgets = []

        self.setWindowTitle("Yamaha Remote")
        self.setWindowIcon(QApplication.windowIcon())
        self.setFixedWidth(330)

        if self.mica_enabled:
            self.setAttribute(Qt.WA_TranslucentBackground)

        self.layout = QGridLayout(self)
        self.layout.setHorizontalSpacing(8)
        self.layout.setVerticalSpacing(8)
        self.layout.setContentsMargins(10, 10, 10, 10)


        self.volume_status = self._new_status_label("")

        self.mute_button = self._new_button("Mute")
        self.power_button = self._new_button("Standby")
        self.volume_down_button = self._new_button("Volume -")
        self.volume_up_button = self._new_button("Volume +")

        self.mute_button.clicked.connect(self.toggle_mute)
        self.power_button.clicked.connect(self.toggle_power)
        self.volume_down_button.clicked.connect(
            lambda: self.change_volume(-1)
        )
        self.volume_up_button.clicked.connect(
            lambda: self.change_volume(1)
        )

        row = 0

        self.layout.addWidget(self.mute_button, row, 0)
        self.layout.addWidget(self.power_button, row, 1)
        row += 1

        volume_row = QHBoxLayout()
        volume_row.setSpacing(8)
        volume_row.setContentsMargins(0, 0, 0, 0)
        volume_row.addWidget(self.volume_down_button, 1)
        volume_row.addWidget(self.volume_status, 0)
        volume_row.addWidget(self.volume_up_button, 1)

        self.layout.addLayout(
            volume_row,
            row,
            0,
            1,
            2,
        )
        row += 1

        for i in range(0, len(self.favorite_inputs), 2):
            for column in range(2):
                index = i + column

                if index < len(self.favorite_inputs):
                    favorite = self.favorite_inputs[index]
                    button = self._new_button(favorite["Label"])

                    button.clicked.connect(
                        lambda checked=False, input_id=favorite["Input"]:
                            self.change_input(input_id)
                    )

                    self.control_widgets.append(button)
                    self.layout.addWidget(
                        button,
                        row,
                        column,
                    )

            row += 1

        self.input_combo = QComboBox()
        self.input_combo.setMinimumHeight(30)
        self.input_combo.activated.connect(
            self.input_selected
        )
        self.input_hover_tooltip = DelayedInputTooltip(
            self.input_combo,
            delay_ms=2000,
        )

        self.layout.addWidget(
            self.input_combo,
            row,
            0,
            1,
            2,
        )

        self.control_widgets.extend(
            [
                self.mute_button,
                self.power_button,
                self.volume_down_button,
                self.volume_up_button,
                self.input_combo,
            ]
        )

        # Size the window for the configured number of favorite rows,
        # then prevent manual resizing while the app is running.
        self.setFixedHeight(self.sizeHint().height())

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(
            self.refresh_status
        )

        self.startup_status_timer = QTimer(self)
        self.startup_status_timer.setSingleShot(True)
        self.startup_status_timer.setInterval(500)
        self.startup_status_timer.timeout.connect(
            self.show_connecting_status
        )

        if self.dark_mode:
            self.apply_dark_theme()

        self.set_controls_enabled(False)

        if self.system_tray_enabled:
            self.setup_system_tray()

        QTimer.singleShot(0, self.initialize)

    def setup_system_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            raise ConfigError(
                "System tray support was enabled, but no "
                "system tray is available on this desktop."
            )

        self.tray_icon = QSystemTrayIcon(
            QApplication.windowIcon(),
            self,
        )
        self.tray_icon.setToolTip("Yamaha Remote")

        menu = QMenu(self)

        show_action = QAction("Show", self)
        show_action.triggered.connect(
            self.show_from_tray
        )
        menu.addAction(show_action)

        menu.addSeparator()

        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(
            self.exit_from_tray
        )
        menu.addAction(exit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(
            self.tray_activated
        )
        self.tray_icon.show()

    def tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_from_tray()

    def show_from_tray(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def exit_from_tray(self):
        self.force_exit = True
        self.close()

    def closeEvent(self, event):
        if (
            self.close_to_tray
            and not self.force_exit
            and self.tray_icon is not None
        ):
            event.ignore()
            self.hide()
            return

        self.closing = True
        self.refresh_timer.stop()
        self.startup_status_timer.stop()

        # Drop work that has not started, then wait for the single running
        # receiver request to finish. Receiver operations have their own
        # bounded network/action timeouts, so leaving a QThreadPool worker
        # behind is both unnecessary and unsafe during interpreter shutdown.
        self.thread_pool.clear()
        self.thread_pool.waitForDone(-1)
        self.active_workers.clear()

        if self.tray_icon is not None:
            self.tray_icon.hide()

        event.accept()

        # Be explicit about terminating the application. This is especially
        # important for tray exits, where Qt objects can otherwise outlive the
        # main window even though it has disappeared.
        app = QApplication.instance()
        if app is not None:
            app.quit()

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

    def showEvent(self, event):
        super().showEvent(event)

        if self.mica_enabled:
            apply_mica(int(self.winId()))

        if self.dark_mode and sys.platform == "win32":
            set_dark_titlebar(int(self.winId()))

    def _new_status_label(self, text):
        label = DraggableLabel(text, self)
        label.setAlignment(Qt.AlignCenter)
        label.setMinimumHeight(22)
        label.setFixedWidth(52)
        return label

    def _new_button(self, text):
        button = QPushButton(text)
        button.setMinimumHeight(34)
        return button

    def apply_dark_theme(self):
        palette = self.palette()

        if self.mica_enabled:
            palette.setColor(
                QPalette.Window,
                QColor(0, 0, 0, 0),
            )
        else:
            palette.setColor(
                QPalette.Window,
                QColor(32, 32, 32),
            )

        palette.setColor(
            QPalette.WindowText,
            QColor(235, 235, 235),
        )
        palette.setColor(
            QPalette.Base,
            QColor(48, 48, 48),
        )
        palette.setColor(
            QPalette.AlternateBase,
            QColor(48, 48, 48),
        )
        palette.setColor(
            QPalette.Text,
            QColor(235, 235, 235),
        )
        palette.setColor(
            QPalette.Button,
            QColor(48, 48, 48),
        )
        palette.setColor(
            QPalette.ButtonText,
            QColor(235, 235, 235),
        )
        palette.setColor(
            QPalette.Highlight,
            QColor(70, 100, 160),
        )
        palette.setColor(
            QPalette.HighlightedText,
            QColor(255, 255, 255),
        )

        self.setPalette(palette)

        if self.mica_enabled:
            self.setStyleSheet("""
                QPushButton {
                    background-color: rgba(48, 48, 48, 185);
                    color: rgb(235, 235, 235);
                    border: 1px solid rgba(120, 120, 120, 150);
                    border-radius: 7px;
                    padding: 5px;
                }

                QPushButton:hover {
                    background-color: rgba(68, 68, 68, 205);
                }

                QPushButton:pressed {
                    background-color: rgba(38, 38, 38, 220);
                }

                QPushButton:disabled {
                    color: rgba(235, 235, 235, 100);
                    background-color: rgba(48, 48, 48, 100);
                }

                QComboBox {
                    background-color: rgba(48, 48, 48, 185);
                    color: rgb(235, 235, 235);
                    border: 1px solid rgba(120, 120, 120, 150);
                    border-radius: 7px;
                    padding: 4px 7px;
                }

                QComboBox:disabled {
                    color: rgba(235, 235, 235, 100);
                    background-color: rgba(48, 48, 48, 100);
                }

                QComboBox::drop-down {
                    width: 0px;
                    border: none;
                    background: transparent;
                }

                QComboBox::down-arrow {
                    image: none;
                    width: 0px;
                    height: 0px;
                }

                QComboBox QAbstractItemView {
                    background-color: rgba(40, 40, 40, 240);
                    color: rgb(235, 235, 235);
                    selection-background-color: rgba(80, 110, 170, 230);
                    border: 1px solid rgba(120, 120, 120, 150);
                }

                QLabel {
                    background: transparent;
                    color: rgb(235, 235, 235);
                }
            """)

    def start_worker(self, function, callback):
        if self.closing:
            return

        worker = Worker(function)
        self.active_workers.add(worker)

        def completed(result, error, worker=worker):
            self.active_workers.discard(worker)

            if not self.closing:
                callback(result, error)

        worker.signals.completed.connect(completed)
        self.thread_pool.start(worker)

    def initialize(self):
        self.refresh_timer.start()
        self.startup_status_timer.start()
        self.load_inputs()

    def show_connecting_status(self):
        if (
            not self.connected
            and not self.closing
        ):
            self.power_button.setText("Connecting...")

    def clear_startup_status(self):
        if self.startup_status_timer.isActive():
            self.startup_status_timer.stop()

    def load_inputs(self):
        if self.closing:
            return

        if self.initialized_inputs:
            self.refresh_status()
            return

        def completed(inputs, error):
            if error is not None:
                self.set_offline()
                return

            inputs = [
                item
                for item in inputs
                if item["Id"] not in self.hidden_inputs
            ]

            for item in inputs:
                override = self.input_name_overrides.get(item["Id"])
                if override is not None:
                    item["Label"] = override

            inputs.sort(
                key=lambda item: item["Label"].lower()
            )

            self.inputs = inputs
            self.initialized_inputs = True
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

            self.refresh_status()

        self.start_worker(
            self.backend.get_inputs,
            completed,
        )

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
        self.volume_down_button.setEnabled(power_on)
        self.volume_up_button.setEnabled(power_on)

    def set_offline(self):
        self.clear_startup_status()
        self.connected = False
        self.last_status = None

        self.volume_status.setText("-")
        self.power_button.setText("Offline")

        self.set_controls_enabled(False)

    def set_error_status(self):
        self.power_button.setText("Error")
        self.update_control_states()

    def refresh_status(self):
        if self.closing:
            return

        if self.refresh_running or self.action_running:
            return

        self.refresh_running = True

        def completed(status, error):
            self.refresh_running = False

            if error is not None:
                if isinstance(error, YamahaConnectionError):
                    self.set_offline()
                else:
                    self.set_error_status()
                return

            self.clear_startup_status()
            self.connected = True
            self.last_status = status
            self.show_cached_status()
            self.update_control_states()

            if not self.initialized_inputs:
                self.load_inputs()

        self.start_worker(
            self.backend.get_status,
            completed,
        )

    def run_action(self, action):
        if self.closing:
            return False

        if self.action_running or not self.connected:
            return False

        self.action_running = True

        def completed(result, error):
            self.action_running = False

            if error is not None:
                if isinstance(error, YamahaConnectionError):
                    self.set_offline()
                else:
                    self.set_error_status()
                return

            self.refresh_status()

        self.start_worker(
            action,
            completed,
        )
        return True

    def toggle_mute(self):
        if not self.last_status:
            return

        enabled = not bool(self.last_status.get("mute"))

        if self.run_action(
            lambda: self.backend.set_mute(enabled)
        ):
            self.last_status["mute"] = enabled
            self.show_cached_status()

    def toggle_power(self):
        if not self.last_status:
            return

        power_on = self.last_status.get("power") != "on"

        def action():
            self.backend.set_power(power_on)

            if power_on:
                self.backend.wait_until_powered_on()

        self.run_action(action)

    def change_volume(self, direction):
        if not self.last_status:
            return

        action = (
            self.backend.volume_down
            if direction < 0
            else self.backend.volume_up
        )

        if self.run_action(action) and self.last_status.get("power") == "on":
            self.last_status["volume"] = (
                self.backend.preview_volume(
                    self.last_status.get("volume"),
                    direction,
                )
            )
            self.show_cached_status()

    def change_input(self, input_id):
        self.run_action(
            lambda: self.backend.set_input(input_id)
        )

    def input_selected(self, index):
        if index < 0:
            return

        input_id = self.input_combo.itemData(index)
        if input_id:
            self.change_input(str(input_id))

    def show_cached_status(self):
        if not self.last_status:
            return

        status = self.last_status

        power = status.get("power", "")

        volume_text = (
            self.backend.format_volume(
                status.get("volume")
            )
            if power == "on"
            else "-"
        )

        muted = bool(status.get("mute"))

        self.volume_status.setText(
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



def acquire_single_instance_lock():
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
        return None

    return lock


def main():
    app = QApplication(sys.argv)

    instance_lock = acquire_single_instance_lock()

    if instance_lock is None:
        return 0

    if sys.platform == "win32":
        set_app_user_model_id("Yamaha.Remote")

    app.setApplicationName("Yamaha Remote")
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

    try:
        window = YamahaRemote(config)
    except Exception as ex:
        QMessageBox.critical(
            None,
            "Yamaha Remote - Startup Error",
            f"{type(ex).__name__}: {ex}",
        )
        return 1

    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
