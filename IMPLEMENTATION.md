# Implementation details

For everyday use, see the [README](README.md). Dated changes are in the [release history](CHANGELOG.md).

## Volume hotkeys

Only Volume Up, Volume Down, and Mute are handled. Other media keys retain their normal behavior. These keys control the amplifier only when it is reachable, powered on, and its last confirmed status is no more than six seconds old. Otherwise, new keypresses pass through normally; hotkeys never wake the amplifier. Global keys target the amplifier regardless of its selected input.

Windows uses a dedicated keyboard-hook thread and queues requests to the existing serialized device worker. Key handling does not wait for network requests. Mute toggles once per physical press, while holding Volume Up or Volume Down repeats. A small bounded queue prevents an unbounded delayed volume ramp; excess repeats and new presses are consumed without queueing when full. This keeps fast volume-wheel bursts from changing Windows volume while the amplifier owns those keys. A press already claimed remains claimed until release, even if availability changes during that press. Remapped keys are supported when Windows exposes them as volume-key events.

On macOS and Linux, focused-window Qt shortcuts are available; global interception is Windows-only. If Windows hook installation fails, the app reports it and stays usable; focused Qt fallback may depend on the desktop's media-key handling. Modal dialogs temporarily suspend new hotkey commands. Hotkey request failures update the remote's status without opening a dialog over another application.

The app publishes both the configured global-hotkey preference and actual ownership. Sony Remote defers its focused volume/mute shortcuts only while actual global ownership is active; standby, offline, stale state, hook failure, and modal dialogs release ownership.

## Local amplifier connection

The remotes cooperate through optional local IPC. No additional files or dependencies are needed. Both scripts remain self-contained and neither reads the other app's configuration.

Yamaha Remote publishes state over a user-local Qt socket automatically. Configure `AmplifierTVInput` in Sony Remote with the amplifier's exact input ID, such as `hdmi1`, `optical1`, or `tv`. Use the keys under Yamaha Remote's `Inputs` setting, rather than a display name or `NameOverride`. Sony routes its volume buttons, mute button, and focused volume keys to the amplifier only while its fresh state says it is on and using that input. Global Yamaha hotkeys continue to target the amplifier on every input.

The existing two-second device polling continues while a TV client is connected, including when the amplifier window is hidden, minimized, in standby, or offline. IPC itself does not poll device state. If both global control and connected clients are absent, hidden-window polling pauses again.

The generic `remote-control.amplifier.v1.<user-suffix>` endpoint uses `QLocalServer` / `QLocalSocket` with user-only access, a role lock, versioned newline-delimited JSON, bounded frames, and bounded command queues. State messages carry `online`, `power`, `input`, `raw_volume`, `raw_max_volume`, `mute`, `global_hotkeys_enabled`, `global_hotkeys_active`, and a remaining freshness lease in `valid_for_ms`. Status freshness is measured from the device response, not socket delivery. Commands are limited to `volume_up`, `volume_down`, and `mute`, and include the expected amplifier input. Input and availability are checked before queueing and again before execution. Acknowledgements confirm queue acceptance; later device failures follow the amplifier's normal background error handling.

`raw_max_volume` describes the network control interface's ceiling, not the amplifier's physical maximum. Sony uses a matching calibration file for TV-scale display, including the observed range cap, and otherwise shows amplifier raw volume. It does not write a converted value back to the TV. A separate helper measures the shared observable range; see the calibration section below.

## Calibration sessions

Use current versions of both remotes before using `VolumeCalibration.pyw`, available in the Sony Remote repository. The helper is a separate, self-contained PySide6 application and communicates only through generic local IPC endpoints. It does not read either remote's settings or credentials.

During a session, both remotes reuse their existing polling timers at 250 ms, including while hidden. Ordinary controls are disabled and pending volume targets are cleared. Yamaha's Windows hook consumes volume/mute media keys without issuing device commands, so they cannot unmute Windows or change volume during the test. Config-driven restarts wait until the session ends. A socket-owned eight-second lease, renewed by the helper, restores normal polling and controls on release, disconnect, or helper failure. Only the owner can perform calibration operations; API work stays in the existing device workers.

The amplifier publishes raw state and configured-endpoint identity; it does not read the calibration mapping or Sony settings. The API ceiling is a control-interface limit, not the physical amplifier maximum.

See the [Sony implementation details](https://github.com/cnieuweboer/Sony-Remote-Control/blob/main/IMPLEMENTATION.md) for sweep behavior, restoration, and mapping storage.

## Amplifier volume display

State includes `volume_display` (the exact text from the existing amplifier formatter, including any dB suffix) and `volume_unit` (`raw` or `dB`). Sony uses these only without a matching calibration; calibrated display and calibration still use `raw_volume`. Sony validates the display fields without converting dB. Update both remotes together for these required fields.
