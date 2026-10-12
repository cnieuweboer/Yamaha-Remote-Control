import ctypes
import json
import sys
from pathlib import Path

from PySide6.QtCore import QFileInfo, QTimer, Qt
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileIconProvider,
    QGridLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)
from urllib.parse import quote
from urllib.request import urlopen

try:
    from win32mica import ApplyMica, MicaTheme, MicaStyle
except ImportError:
    ApplyMica = None
    MicaTheme = None
    MicaStyle = None


# ----------------------------
# Config
# ----------------------------

CONFIG_FILE = "YamahaRemote.json"


def load_config():
    config_path = Path(__file__).resolve().with_name(CONFIG_FILE)

    if not config_path.exists():
        raise FileNotFoundError(
            f"{CONFIG_FILE} was not found next to {Path(__file__).name}."
        )

    with config_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    yamaha_ip = str(config.get("YamahaIP", "")).strip()
    if not yamaha_ip:
        raise ValueError("YamahaIP must be set in YamahaRemote.json.")

    dark_mode = bool(config.get("DarkMode", True))
    mica = bool(config.get("Mica", False))

    favorites = config.get("FavoriteInputs", [])
    if not isinstance(favorites, list):
        raise ValueError("FavoriteInputs must be a list.")

    normalized_favorites = []
    for favorite in favorites:
        if not isinstance(favorite, dict):
            continue

        label = str(favorite.get("Label", "")).strip()
        input_id = str(favorite.get("Input", "")).strip()

        if label and input_id:
            normalized_favorites.append(
                {"Label": label, "Input": input_id}
            )

    return {
        "YamahaIP": yamaha_ip,
        "DarkMode": dark_mode,
        "Mica": mica,
        "FavoriteInputs": normalized_favorites,
    }


# ----------------------------
# Windows integration
# ----------------------------

def set_app_user_model_id(app_id):
    if sys.platform != "win32":
        return

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def set_dark_titlebar(hwnd):
    if sys.platform != "win32":
        return

    try:
        value = ctypes.c_int(1)

        # Windows 10 20H1+ / Windows 11
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


def get_windows_app_icon():
    if sys.platform != "win32":
        return QIcon()

    try:
        windir = Path(
            __import__("os").environ.get("WINDIR", r"C:\Windows")
        )
        volume_mixer = windir / "System32" / "sndvol.exe"

        if volume_mixer.exists():
            provider = QFileIconProvider()
            return provider.icon(QFileInfo(str(volume_mixer)))
    except Exception:
        pass

    return QIcon()


# ----------------------------
# Yamaha API
# ----------------------------

class YamahaClient:
    def __init__(self, ip):
        self.base_url = f"http://{ip}/YamahaExtendedControl/v1"

    def _get_json(self, path):
        with urlopen(f"{self.base_url}/{path}", timeout=2) as response:
            data = json.loads(response.read().decode("utf-8"))

        response_code = data.get("response_code")
        if response_code is not None and response_code != 0:
            raise RuntimeError(
                f"Yamaha returned response code {response_code}."
            )

        return data

    def get_status(self):
        return self._get_json("main/getStatus")

    def set_power(self, on):
        state = "on" if on else "standby"
        self._get_json(f"main/setPower?power={state}")

    def set_mute(self, enabled):
        value = "true" if enabled else "false"
        self._get_json(f"main/setMute?enable={value}")

    def volume_up(self):
        self._get_json("main/setVolume?volume=up")

    def volume_down(self):
        self._get_json("main/setVolume?volume=down")

    def wait_until_powered_on(self):
        import time

        for _ in range(30):
            time.sleep(0.1)

            try:
                status = self.get_status()
                if status.get("power") == "on":
                    return True
            except Exception:
                pass

        return False

    def set_input(self, input_id):
        status = self.get_status()

        if status.get("power") == "standby":
            self.set_power(True)

            if not self.wait_until_powered_on():
                raise RuntimeError(
                    "The Yamaha did not finish powering on."
                )

        self._get_json(
            f"main/setInput?input={quote(input_id, safe='')}"
        )

    def get_inputs(self):
        features = self._get_json("system/getFeatures")

        names = {}

        try:
            name_text = self._get_json("system/getNameText")

            for item in name_text.get("input_list", []):
                input_id = str(item.get("id", "")).strip()
                text = str(item.get("text", "")).strip()

                if input_id and text:
                    names[input_id] = text
        except Exception:
            pass

        inputs = []

        for item in features.get("system", {}).get("input_list", []):
            input_id = str(item.get("id", "")).strip()
            if not input_id:
                continue

            label = names.get(input_id)
            if not label:
                label = input_id.replace("_", " ").title()

            inputs.append(
                {"Id": input_id, "Label": label}
            )

        inputs.sort(key=lambda x: x["Label"].lower())
        return inputs


# ----------------------------
# UI
# ----------------------------

class YamahaRemote(QWidget):
    def __init__(self, config):
        super().__init__()

        self.dark_mode = config["DarkMode"]
        self.mica_enabled = config["Mica"]
        self.yamaha = YamahaClient(config["YamahaIP"])
        self.favorite_inputs = config["FavoriteInputs"]

        self.last_status = None
        self.inputs = []
        self.refresh_running = False

        self.setWindowTitle("Yamaha Remote")
        self.setWindowIcon(QApplication.windowIcon())
        self.setFixedWidth(390)

        self.layout = QGridLayout(self)
        self.layout.setHorizontalSpacing(8)
        self.layout.setVerticalSpacing(8)
        self.layout.setContentsMargins(10, 10, 10, 10)

        self.mute_status = self._new_status_label("Mute: -")
        self.power_status = self._new_status_label("Power: Connecting...")
        self.volume_status = self._new_status_label("Volume: -")
        self.input_status = self._new_status_label("Input: -")

        self.layout.addWidget(self.mute_status, 0, 0)
        self.layout.addWidget(self.power_status, 0, 1)
        self.layout.addWidget(self.volume_status, 1, 0)
        self.layout.addWidget(self.input_status, 1, 1)

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

        row = 2

        self.layout.addWidget(self.mute_button, row, 0)
        self.layout.addWidget(self.power_button, row, 1)
        row += 1

        self.layout.addWidget(self.volume_down_button, row, 0)
        self.layout.addWidget(self.volume_up_button, row, 1)
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

                    self.layout.addWidget(
                        button,
                        row,
                        column,
                    )

            row += 1

        self.input_combo = QComboBox()
        self.input_combo.setMinimumHeight(32)
        self.input_combo.activated.connect(
            self.input_selected
        )

        self.layout.addWidget(
            self.input_combo,
            row,
            0,
            1,
            2,
        )

        # Size the window for the configured number of favorite rows,
        # then prevent manual vertical resizing while the app is running.
        self.setFixedHeight(self.sizeHint().height())

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(2000)
        self.refresh_timer.timeout.connect(
            lambda: self.update_status(quiet=True)
        )

        if self.dark_mode:
            self.apply_dark_theme()

        QTimer.singleShot(0, self.initialize)

    def showEvent(self, event):
        super().showEvent(event)

        if self.mica_enabled and ApplyMica is not None:
            try:
                theme = MicaTheme.DARK if self.dark_mode else MicaTheme.LIGHT
                ApplyMica(
                    int(self.winId()),
                    theme,
                    MicaStyle.DEFAULT,
                )
            except Exception:
                pass

        if self.dark_mode:
            set_dark_titlebar(int(self.winId()))

    def _new_status_label(self, text):
        label = QLabel(text)
        label.setAlignment(Qt.AlignCenter)
        label.setMinimumHeight(22)
        return label

    def _new_button(self, text):
        button = QPushButton(text)
        button.setMinimumHeight(38)
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
                    padding: 6px;
                }

                QPushButton:hover {
                    background-color: rgba(68, 68, 68, 205);
                }

                QPushButton:pressed {
                    background-color: rgba(38, 38, 38, 220);
                }

                QComboBox {
                    background-color: rgba(48, 48, 48, 185);
                    color: rgb(235, 235, 235);
                    border: 1px solid rgba(120, 120, 120, 150);
                    border-radius: 7px;
                    padding: 5px 8px;
                }

                /* Keep the combo box as one clean rounded field.
                   The whole field remains clickable; no separate arrow button is shown. */
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

    def initialize(self):
        try:
            self.inputs = self.yamaha.get_inputs()

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

            self.update_status(quiet=False)
            self.refresh_timer.start()
        except Exception as ex:
            self.show_error(str(ex))

    def show_error(self, message):
        QMessageBox.critical(
            self,
            "Yamaha Remote",
            message,
        )

    def run_action(self, action):
        try:
            action()
            self.update_status(quiet=True)
        except Exception as ex:
            self.show_error(str(ex))
            self.update_status(quiet=True)

    def toggle_mute(self):
        try:
            status = self.yamaha.get_status()
            self.yamaha.set_mute(
                not bool(status.get("mute"))
            )
            self.update_status(quiet=True)
        except Exception as ex:
            self.show_error(str(ex))

    def toggle_power(self):
        try:
            status = self.yamaha.get_status()

            if status.get("power") == "on":
                self.yamaha.set_power(False)
            else:
                self.yamaha.set_power(True)
                self.yamaha.wait_until_powered_on()

            self.update_status(quiet=True)
        except Exception as ex:
            self.show_error(str(ex))

    def change_volume(self, direction):
        try:
            if direction < 0:
                self.yamaha.volume_down()
            else:
                self.yamaha.volume_up()

            if (
                self.last_status
                and self.last_status.get("power") == "on"
            ):
                self.last_status["volume"] = (
                    int(self.last_status.get("volume", 0))
                    + direction
                )
                self.show_cached_status()

        except Exception as ex:
            self.show_error(str(ex))
            self.update_status(quiet=True)

    def change_input(self, input_id):
        self.run_action(
            lambda: self.yamaha.set_input(input_id)
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
        power_text = "On" if power == "on" else "Standby"

        input_text = (
            status.get("input_text")
            or status.get("input")
            or "-"
        )

        volume_text = (
            str(status.get("volume", "-"))
            if power == "on"
            else "-"
        )

        muted = bool(status.get("mute"))
        mute_text = "Muted" if muted else "Unmuted"

        self.power_status.setText(
            f"Power: {power_text}"
        )
        self.input_status.setText(
            f"Input: {input_text}"
        )
        self.volume_status.setText(
            f"Volume: {volume_text}"
        )
        self.mute_status.setText(
            f"Mute: {mute_text}"
        )

        self.power_button.setText(
            "Standby" if power == "on" else "Power On"
        )
        self.mute_button.setText(
            "Unmute" if muted else "Mute"
        )

        if not self.input_combo.view().isVisible():
            current_input = str(
                status.get("input", "")
            )

            for index in range(
                self.input_combo.count()
            ):
                if (
                    str(self.input_combo.itemData(index))
                    == current_input
                ):
                    self.input_combo.setCurrentIndex(index)
                    break

    def update_status(self, quiet):
        if self.refresh_running:
            return

        self.refresh_running = True

        try:
            self.last_status = self.yamaha.get_status()
            self.show_cached_status()

        except Exception as ex:
            self.power_status.setText("Power: Unavailable")
            self.input_status.setText("Input: -")
            self.volume_status.setText("Volume: -")
            self.mute_status.setText("Mute: -")

            if not quiet:
                self.show_error(str(ex))

        finally:
            self.refresh_running = False


def main():
    set_app_user_model_id("Yamaha.Remote")

    app = QApplication(sys.argv)
    app.setApplicationName("Yamaha Remote")
    app.setWindowIcon(get_windows_app_icon())

    try:
        config = load_config()
    except Exception as ex:
        QMessageBox.critical(
            None,
            "Yamaha Remote",
            str(ex),
        )
        return 1

    window = YamahaRemote(config)
    window.show()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
