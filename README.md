# Yamaha-Remote-Control

Small PySide6 desktop remote for Yamaha receivers/amplifiers using the Yamaha Extended Control API.

![Yamaha Remote](screenshots/yamaha_remote.jpg)

## Installation

Run `YamahaRemote.pyw` from a writable folder. On first launch it creates `YamahaRemote.json` and prompts you to fill in the missing connection settings; you can also copy `YamahaRemote.example.json` to `YamahaRemote.json` before starting.

Local settings, diagnostic logs, and temporary JSON files are ignored by Git. When updating an older Git checkout, back up `YamahaRemote.json` before pulling this change and restore it afterward if Git removes it. Do not put credentials in the example file.

## Windows

```powershell
py -m pip install PySide6
```

Run `YamahaRemote.pyw`.

For Windows login startup, add `--start-in-tray` to your shortcut target, for example:

```text
"C:\Path\To\pythonw.exe" "C:\Remotes\YamahaRemote.pyw" --start-in-tray
```

This starts hidden only when `SystemTray` and `CloseToTray` are both `true` and a tray is available; otherwise the window opens normally. Launching the app again still brings the existing window forward.

## macOS

```bash
python3 -m pip install PySide6
python3 YamahaRemote.pyw
```

The volume `−` and `+` buttons repeat while held, starting after half a second.

The Input section keeps its selector above favorite inputs. The Volume section includes a speaker selector next to Mute: Off/A for A-only amplifiers, or Off/A/B/A+B when both outputs are advertised; unsupported devices show it disabled.

Speaker selection updates immediately while the amplifier applies and confirms the change. Switching between A and B first confirms the unwanted output is off before enabling the other.

## Configuration

```json
{
  "Host": "192.168.1.100",
  "DarkMode": true,
  "Mica": true,
  "VolumeDb": true,
  "VolumeDbAtZero": -97.0,
  "VolumeDbStepSize": 0.5,
  "GlobalVolumeHotkeys": false,
  "DiagnosticsEnabled": false,
  "SortByName": false,
  "Inputs": {},
  "SystemTray": false,
  "CloseToTray": false
}
```

- `Host` — IP address or hostname of the Yamaha device.
- `DarkMode` — `true` or `false`.
- `Mica` — Windows-only Mica background. Ignored on macOS.
- `VolumeDb` — display volume in dB instead of the API volume value.
- `VolumeDbAtZero` — dB value corresponding to volume `0`. Required when `VolumeDb` is `true`.
- `VolumeDbStepSize` — dB change corresponding to one volume step. Required when `VolumeDb` is `true` and must be greater than `0`.
- `GlobalVolumeHotkeys` — `false` by default. On Windows, set to `true` to control the amplifier with Volume Up, Volume Down, and Mute even when another app is focused. With `false`, these keys control the amplifier only while Yamaha Remote is focused.
- `DiagnosticsEnabled` — `false` by default. Set to `true` to append unexpected errors and crash diagnostics to `YamahaRemote-crash.log` beside the script. Logs contain the app version, exception types, and stack locations; exception messages, source lines, request data, and local variable values are omitted. If the log cannot be opened or written, the app continues without logging.
- `SortByName` — `false` (default) keeps favorites and dropdown entries in JSON insertion order; `true` sorts them alphabetically by `NameOverride`, falling back to `Name`, ignoring case.
- `DropdownAnimationWorkaround` — use a live 200 ms input-dropdown animation to avoid dropdown flicker on Windows (enabled by default on Windows). Set to `false` for the standard Qt dropdown; disabled system animations remain disabled.
- `Inputs` — settings for the inputs discovered from the Yamaha device.
  - `Name` — name reported by the Yamaha device. This is updated automatically.
  - `NameOverride` — optional replacement name shown in the dropdown and on favorite buttons.
  - `Hidden` — hide the input from the dropdown menu. Hidden inputs can still be favorites.
  - `Favorite` — show a button for the input directly in the remote.
- `SystemTray` — show a system tray icon when the desktop supports it. If unavailable, the app runs as an ordinary window.
- `CloseToTray` — clicking the window close button hides the app to the tray instead of exiting. If no tray is available, closing the window exits normally.

YamahaRemote automatically adds newly discovered inputs to `Inputs` with default settings. Existing `NameOverride`, `Hidden`, and `Favorite` values are preserved. Existing entries keep their JSON positions; newly discovered entries append in device discovery order. Dropdowns only include currently discovered, non-hidden entries. Favorite buttons remain independent of `Hidden`. Name sorting affects display only and does not reorder the JSON file.

When `VolumeDb` is enabled, the displayed dB value is calculated using:

`dB = VolumeDbAtZero + (volume × VolumeDbStepSize)`

For example:

`VolumeDbAtZero = -97.0`  
`VolumeDbStepSize = 0.5`

means that a volume value of `100` is displayed as `-47.0 dB`.

The app automatically restarts when `YamahaRemote.json` changes.

Launching Yamaha Remote again restores its existing window, including from the system tray.

Regular status polling pauses while the window is hidden or minimized and refreshes immediately when restored, unless Windows global volume hotkeys are enabled and their hook is installed, or a local TV remote is connected through IPC. In that case, the existing polling timer continues while hidden, including in standby or offline, so external power changes and reconnections can be detected. Active requests finish normally; configuration file watching remains enabled.


## Volume hotkeys

On Windows, Volume Up, Volume Down, and Mute control the amplifier while this window is focused; enable `GlobalVolumeHotkeys` to use them from other apps or the system tray. When the amplifier is off or unavailable, new presses retain their normal behavior instead of waking it.

Global control applies on every amplifier input, and other media keys keep their usual behavior. On macOS and Linux, volume shortcuts work only while the remote is focused.

## Use with Sony Remote

Run both remotes to let Sony control amplifier volume directly and avoid the slower TV/CEC path. Sony automatically falls back to TV volume when the amplifier is unavailable, in standby, or on another input.

A completed calibration supplies the amplifier's TV input and TV-scale volume display automatically. Without calibration, set `AmplifierTVInput` in Sony Remote to the input ID from Yamaha Remote's `Inputs` keys, rather than its display name.

## Volume calibration

- **Purpose:** The Windows helper maps amplifier volume to familiar TV volume numbers for Sony Remote.
- **Requirements:** Connect the TV to the amplifier through HDMI-CEC, run current versions of both remotes, and place `VolumeCalibration.pyw` beside `SonyRemote.pyw`.
- **Before starting:** Turn on both devices, select the amplifier input connected to the TV, and put the TV on a silent source. Stop other playback and keep the same Windows audio output selected.
- **Speaker isolation:** The helper verifies amplifier speakers are off and restores the original combination after restoring volume. If isolation is unavailable or lost, it offers one Continue/Cancel decision for the sweep; keep all sources silent either way.
- **Safety:** Volume can go **VERY HIGH** and the devices may unmute during calibration. Keep the source silent, avoid physical controls, and **check speaker outputs, volume, and mute on the TV, amplifier, and computer before playing sound afterward**.

See the [Sony Remote calibration guide](https://github.com/cnieuweboer/Sony-Remote-Control#volume-calibration) for Start/Replace/Cancel instructions and restoration guidance.

## More information

- [Implementation details](IMPLEMENTATION.md) — hotkeys, IPC, and calibration coordination.
- [Release history](CHANGELOG.md) — versions and changes.

## Third-party code

YamahaRemote includes Windows Mica integration code inspired by
[win32mica](https://github.com/marticliment/win32mica), licensed under the MIT License.

The relevant license text is included in `THIRD_PARTY_LICENSES.txt`.
