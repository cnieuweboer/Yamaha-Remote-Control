# Yamaha-Remote-Control

A small Windows remote for Yamaha receivers/amplifiers using the Yamaha Extended Control API.

Designed as a simple desktop remote for common controls such as power, mute, volume, favorites, and input selection.

## Requirements

- Windows
- Python 3
- PySide6
- win32mica (only if enabled in configuration)

Install the required packages with:

```powershell
py -m pip install PySide6 win32mica
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
  "YamahaIP": "192.168.1.100",
  "DarkMode": true,
  "Mica": true,
  "FavoriteInputs": [
    {
      "Label": "TV",
      "Input": "tv"
    },
    {
      "Label": "USB DAC",
      "Input": "usb_dac"
    }
  ]
}
```

- `YamahaIP` — IP address of the Yamaha device.
- `DarkMode` — use the dark UI theme.
- `Mica` — enable the Windows 11 Mica background.
- `FavoriteInputs` — buttons shown directly in the remote. `Label` is the displayed name and `Input` is the Yamaha input ID.

The input dropdown is populated automatically from the Yamaha device.
