# Yamaha-Remote-Control

Small PySide6 desktop remote for Yamaha receivers/amplifiers using the Yamaha Extended Control API.

![Yamaha Remote](screenshots/yamaha_remote.jpg)

## Installation

Place these files in the same folder.

```text
YamahaRemote.pyw
YamahaRemote.json
```

## Windows

```powershell
py -m pip install PySide6
```

Run `YamahaRemote.pyw`.

## macOS

```bash
python3 -m pip install PySide6
python3 YamahaRemote.pyw
```

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

The app automatically restart when it detects the json file is changed.

Regular status polling pauses while the window is hidden or minimized and refreshes immediately when restored, unless Windows global volume hotkeys are enabled and their hook is installed. In that case, the existing polling timer continues while hidden, including in standby or offline, so external power changes and reconnections can be detected. Active requests finish normally; configuration file watching remains enabled.


## Volume hotkeys

Only Volume Up, Volume Down, and Mute are handled. Other media keys retain their normal behavior. These keys control the amplifier only when it is reachable, powered on, and its last confirmed status is no more than six seconds old. Otherwise, new keypresses pass through normally; hotkeys never wake the amplifier. Global keys target the amplifier regardless of its selected input.

Windows uses a dedicated keyboard-hook thread and queues requests to the existing serialized device worker. Key handling does not wait for network requests. Mute toggles once per physical press, while holding Volume Up or Volume Down repeats. A small bounded queue prevents an unbounded delayed volume ramp; excess repeats are ignored when full, and new presses that cannot be accepted pass through. A press already claimed remains claimed until release, even if availability changes during that press. Remapped keys are supported when Windows exposes them as volume-key events.

On macOS and Linux, focused-window Qt shortcuts are available; global interception is Windows-only. If Windows hook installation fails, the app reports it and stays usable; focused Qt fallback may depend on the desktop's media-key handling. Modal dialogs temporarily suspend new hotkey commands. Hotkey request failures update the remote's status without opening a dialog over another application.

The app tracks whether global ownership is actually active separately from the JSON preference, for the later IPC integration. Sony Remote coordination is not part of this release, so avoid relying on both apps' volume shortcuts simultaneously until that step is implemented.

## Third-party code

YamahaRemote includes Windows Mica integration code inspired by
[win32mica](https://github.com/marticliment/win32mica), licensed under the MIT License.

The relevant license text is included in `THIRD_PARTY_LICENSES.txt`.
