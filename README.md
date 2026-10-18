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

Version `2026.10.18.4` keeps eligible volume keys owned by the amplifier even when its command queue is full, fixing occasional Windows volume changes during fast wheel scrolling. This applies to both focused and global control. Standby, offline, stale state, modal dialogs, and unfocused operation with global control disabled still release new presses normally.

Only Volume Up, Volume Down, and Mute are handled. Other media keys retain their normal behavior. These keys control the amplifier only when it is reachable, powered on, and its last confirmed status is no more than six seconds old. Otherwise, new keypresses pass through normally; hotkeys never wake the amplifier. Global keys target the amplifier regardless of its selected input.

Windows uses a dedicated keyboard-hook thread and queues requests to the existing serialized device worker. Key handling does not wait for network requests. Mute toggles once per physical press, while holding Volume Up or Volume Down repeats. A small bounded queue prevents an unbounded delayed volume ramp; excess repeats and new presses are consumed without queueing when full. This keeps fast volume-wheel bursts from changing Windows volume while the amplifier owns those keys. A press already claimed remains claimed until release, even if availability changes during that press. Remapped keys are supported when Windows exposes them as volume-key events.

On macOS and Linux, focused-window Qt shortcuts are available; global interception is Windows-only. If Windows hook installation fails, the app reports it and stays usable; focused Qt fallback may depend on the desktop's media-key handling. Modal dialogs temporarily suspend new hotkey commands. Hotkey request failures update the remote's status without opening a dialog over another application.

The app publishes both the configured global-hotkey preference and actual ownership. Sony Remote defers its focused volume/mute shortcuts only while actual global ownership is active; standby, offline, stale state, hook failure, and modal dialogs release ownership.

## Local amplifier connection

Version `2026.10.18.2` adds optional cooperation with Sony Remote of the same version. No additional files or dependencies are needed. Both scripts remain self-contained and neither reads the other app's configuration.

Yamaha Remote publishes state over a user-local Qt socket automatically. Configure `AmplifierTVInput` in Sony Remote with the amplifier's exact input ID, such as `hdmi1`, `optical1`, or `tv`. Use the keys under Yamaha Remote's `Inputs` setting, rather than a display name or `NameOverride`. Sony routes its volume buttons, mute button, and focused volume keys to the amplifier only while its fresh state says it is on and using that input. Global Yamaha hotkeys continue to target the amplifier on every input.

The existing two-second device polling continues while a TV client is connected, including when the amplifier window is hidden, minimized, in standby, or offline. IPC itself does not poll device state. If both global control and connected clients are absent, hidden-window polling pauses again.

The generic `remote-control.amplifier.v1.<user-suffix>` endpoint uses `QLocalServer` / `QLocalSocket` with user-only access, a role lock, versioned newline-delimited JSON, bounded frames, and bounded command queues. State messages carry `online`, `power`, `input`, `raw_volume`, `raw_max_volume`, `mute`, `global_hotkeys_enabled`, `global_hotkeys_active`, and a remaining freshness lease in `valid_for_ms`. Status freshness is measured from the device response, not socket delivery. Commands are limited to `volume_up`, `volume_down`, and `mute`, and include the expected amplifier input. Input and availability are checked before queueing and again before execution. Acknowledgements confirm queue acceptance; later device failures follow the amplifier's normal background error handling.

`raw_max_volume` describes the network control interface's ceiling, not the amplifier's physical maximum. Sony uses a matching calibration file for TV-scale display, including the observed range cap, and otherwise shows amplifier raw volume. It does not write a converted value back to the TV. A separate helper measures the shared observable range; see the calibration section below.

## Calibration helper — session and restoration stage

Release `2026.10.18.5` adds exclusive calibration sessions. Update **both** remotes before using `VolumeCalibration.pyw`, available in the Sony Remote repository. The helper is a separate, self-contained PySide6 application and communicates only through generic local IPC endpoints. It does not read either remote's settings or credentials.

During a session, both remotes reuse their existing polling timers at 250 ms, including while hidden. Ordinary controls are disabled and pending volume targets are cleared. Yamaha's Windows hook consumes volume/mute media keys without issuing device commands, so they cannot unmute Windows or change volume during the test. Config-driven restarts wait until the session ends. A socket-owned eight-second lease, renewed by the helper, restores normal polling and controls on release, disconnect, or helper failure. Only the owner can perform calibration operations; API work stays in the existing device workers.

Current scripts are **YamahaRemote.pyw and SonyRemote.pyw `2026.10.18.15`**, with **VolumeCalibration.pyw `2026.10.18.14`**. The remote cleanup release preserves existing calibration files and behavior; no recalibration is required. The helper measures upward through the shared observable range and rechecks skipped raw values through CEC before estimating missing entries. It saves the completed lookup and captured TV input in a separate `VolumeCalibration.json`; Sony automatically uses a matching device-pair mapping for input routing and TV-scale volume display, retaining `AmplifierTVInput` as fallback. Yamaha only publishes its own raw state and configured-endpoint identity; it does not read the mapping, interpolate TV values, or read Sony settings. The helper stops upward commands at the first API ceiling reading, restores the saved states, and only then activates the mapping. See the Sony repository README for silent-source requirements, bounds, recovery, and result interpretation.

## Third-party code

YamahaRemote includes Windows Mica integration code inspired by
[win32mica](https://github.com/marticliment/win32mica), licensed under the MIT License.

The relevant license text is included in `THIRD_PARTY_LICENSES.txt`.
