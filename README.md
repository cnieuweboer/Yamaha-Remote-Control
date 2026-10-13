# Yamaha-Remote-Control

A small Windows remote for Yamaha receivers/amplifiers using the Yamaha Extended Control API.

Designed as a simple desktop remote for common controls such as power, mute, volume, favorites, and input selection.

## Requirements

- Windows
- Python 3
- PySide6

Install PySide6 with:

```powershell
py -m pip install PySide6
```

## Usage

Keep these files together:

```text
YamahaRemote.pyw
YamahaRemote.json
```

Run `YamahaRemote.pyw` to launch the remote.

## Configuration

Edit `YamahaRemote.json`:

```json
{
  "Host": "192.168.1.100",
  "DarkMode": true,
  "Mica": true,
  "VolumeDb": true,
  "VolumeDbAtZero": -97.0,
  "VolumeDbStepSize": 0.5,
  "FavoriteInputs": [
    {
      "Label": "TV",
      "Input": "tv"
    },
    {
      "Label": "USB DAC",
      "Input": "usb_dac"
    }
  ],
  "SystemTray": false,
  "CloseToTray": false
}

```

- `Host` — IP address or hostname of the Yamaha device.
- `DarkMode` — use the dark UI theme.
- `Mica` — enable the Windows 11 Mica background. Ignored on other platforms.
- `VolumeDb` — display volume in dB instead of the Yamaha API's raw volume value.
- `VolumeDbAtZero` — dB value corresponding to Yamaha volume `0`.
- `VolumeDbStepSize` — dB change corresponding to one Yamaha volume step.
- `FavoriteInputs` — buttons shown directly in the remote. `Label` is the displayed name and `Input` is the Yamaha input ID.
- `SystemTray` — show a system tray icon.
- `CloseToTray` — hide the main window to the system tray instead of exiting when it is closed. Requires `SystemTray` to be enabled.

To read the input ID of the currently selected input, use `YamahaStatus.ps1` from [Yamaha-Power-Helper](https://github.com/cnieuweboer/Yamaha-Power-Helper).

The input dropdown is populated automatically from the Yamaha device.

When `VolumeDb` is enabled, the displayed dB value is calculated using:

`dB = VolumeDbAtZero + (volume × VolumeDbStepSize)`

For example:

`VolumeDbAtZero = -97.0`  
`VolumeDbStepSize = 0.5`

means that a volume value of `100` is displayed as `-47.0 dB`.

`VolumeDbAtZero` sets the dB value at volume `0`, while `VolumeDbStepSize` sets how much the displayed dB value changes for each volume step.

## Third-party code

YamahaRemote includes a small amount of Windows Mica integration code adapted from
[win32mica](https://github.com/marticliment/win32mica), licensed under the MIT License.

The relevant license text is included in `THIRD_PARTY_LICENSES.txt`.
