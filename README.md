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
- `Inputs` — settings for the inputs discovered from the Yamaha device.
  - `Name` — name reported by the Yamaha device. This is updated automatically.
  - `NameOverride` — optional replacement name shown in the dropdown and on favorite buttons.
  - `Hidden` — hide the input from the dropdown menu. Hidden inputs can still be favorites.
  - `Favorite` — show a button for the input directly in the remote.
- `SystemTray` — show a system tray icon.
- `CloseToTray` — clicking the window close button hides the app to the tray instead of exiting.

When the input list is loaded, YamahaRemote automatically adds newly discovered inputs to `Inputs` with default settings. Existing `NameOverride`, `Hidden`, and `Favorite` values are preserved.

When `VolumeDb` is enabled, the displayed dB value is calculated using:

`dB = VolumeDbAtZero + (volume × VolumeDbStepSize)`

For example:

`VolumeDbAtZero = -97.0`  
`VolumeDbStepSize = 0.5`

means that a volume value of `100` is displayed as `-47.0 dB`.

The input ID is the key used for each entry under `Inputs`.
The `Inputs`are populated automatically from the Yamaha device when the app starts.

## Third-party code

YamahaRemote includes Windows Mica integration code inspired by
[win32mica](https://github.com/marticliment/win32mica), licensed under the MIT License.

The relevant license text is included in `THIRD_PARTY_LICENSES.txt`.
