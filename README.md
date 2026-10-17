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

Regular status polling pauses while the window is hidden or minimized and refreshes immediately when restored, unless Windows global volume hotkeys are enabled and their hook is installed, or a local TV remote is connected through IPC. In that case, the existing polling timer continues while hidden, including in standby or offline, so external power changes and reconnections can be detected. Active requests finish normally; configuration file watching remains enabled.


## Volume hotkeys

Only Volume Up, Volume Down, and Mute are handled. Other media keys retain their normal behavior. These keys control the amplifier only when it is reachable, powered on, and its last confirmed status is no more than six seconds old. Otherwise, new keypresses pass through normally; hotkeys never wake the amplifier. Global keys target the amplifier regardless of its selected input.

Windows uses a dedicated keyboard-hook thread and queues requests to the existing serialized device worker. Key handling does not wait for network requests. Mute toggles once per physical press, while holding Volume Up or Volume Down repeats. A small bounded queue prevents an unbounded delayed volume ramp; excess repeats are ignored when full, and new presses that cannot be accepted pass through. A press already claimed remains claimed until release, even if availability changes during that press. Remapped keys are supported when Windows exposes them as volume-key events.

On macOS and Linux, focused-window Qt shortcuts are available; global interception is Windows-only. If Windows hook installation fails, the app reports it and stays usable; focused Qt fallback may depend on the desktop's media-key handling. Modal dialogs temporarily suspend new hotkey commands. Hotkey request failures update the remote's status without opening a dialog over another application.

The app publishes both the configured global-hotkey preference and actual ownership. Sony Remote defers its focused volume/mute shortcuts only while actual global ownership is active; standby, offline, stale state, hook failure, and modal dialogs release ownership.

## Local amplifier connection

Version `2026.10.18.2` adds optional cooperation with Sony Remote of the same version. No additional files or dependencies are needed. Both scripts remain self-contained and neither reads the other app's configuration.

Yamaha Remote publishes state over a user-local Qt socket automatically. Configure `AmplifierTVInput` in Sony Remote with the amplifier's exact input ID, such as `hdmi1`, `optical1`, or `tv`. Use the keys under Yamaha Remote's `Inputs` setting, rather than a display name or `NameOverride`. Sony routes its volume buttons, mute button, and focused volume keys to the amplifier only while its fresh state says it is on and using that input. Global Yamaha hotkeys continue to target the amplifier on every input.

The existing two-second device polling continues while a TV client is connected, including when the amplifier window is hidden, minimized, in standby, or offline. IPC itself does not poll device state. If both global control and connected clients are absent, hidden-window polling pauses again.

The generic `remote-control.amplifier.v1.<user-suffix>` endpoint uses `QLocalServer` / `QLocalSocket` with user-only access, a role lock, versioned newline-delimited JSON, bounded frames, and bounded command queues. State messages carry `online`, `power`, `input`, `raw_volume`, `raw_max_volume`, `mute`, `global_hotkeys_enabled`, `global_hotkeys_active`, and a remaining freshness lease in `valid_for_ms`. Status freshness is measured from the device response, not socket delivery. Commands are limited to `volume_up`, `volume_down`, and `mute`, and include the expected amplifier input. Input and availability are checked before queueing and again before execution. Acknowledgements confirm queue acceptance; later device failures follow the amplifier's normal background error handling.

`raw_max_volume` describes the network control interface's ceiling, not the amplifier's physical maximum. No TV-scale conversion, volume-setting synchronization back to the TV, or calibration is performed in this release. Sony displays the amplifier's raw value while routing, with a source tooltip. Two-point calibration, caps, and alternative mappings remain a later step.

## Third-party code

YamahaRemote includes Windows Mica integration code inspired by
[win32mica](https://github.com/marticliment/win32mica), licensed under the MIT License.

The relevant license text is included in `THIRD_PARTY_LICENSES.txt`.
