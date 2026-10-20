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

## IPC command backpressure

Acknowledgements include a generic `reason`: empty on acceptance, `queue_full` for dropped overflow, or `state_changed` for rejected eligibility/input. Sony silently drops queue-full commands, including local pending/write-buffer overflow. Other failures appear in the power button tooltip prefixed “Amplifier control”, without changing TV state or opening a dialog. Commands are never replayed or redirected to the TV after an uncertain result. Update both remotes together.

## Device HTTP transport

Device requests use a dedicated urllib opener with `ProxyHandler({})`, bypassing system and environment proxies. This keeps LAN device traffic and TV authentication headers off configured proxies without modifying urllib’s global opener. Existing request timeouts and HTTP/connection exception handling are unchanged.

## Development checks

Install PySide6, then run from the repository root:

```sh
python -m unittest discover -s tests -v
```

These development-only tests do not require real devices and are not imported by the applications. Each script remains independently runnable without the tests or a shared module. Endpoint naming is centralized locally in `ipc_server_name`; the user-local socket names and wire protocol are unchanged.

## Small reliability checks

IPC frame readers drain available socket data in bounded chunks, preserving incomplete frames and enforcing the 4,096-byte limit per frame rather than per burst. Configuration-change checks hash and parse the same byte snapshot. Sony applies the same rule to calibration-file reloads, retaining its last valid mapping after a malformed edit.

Windows mutex creation uses `WinDLL(..., use_last_error=True)` and the ctypes cached last-error value immediately after `CreateMutexW`. Handle release retains pointer-sized signatures. Yamaha’s published freshness uses its existing hotkey age constant; no protocol or freshness-policy change is introduced.

`tests/test_reliability.py` covers fragmented UTF-8, multi-read bursts, frame-size boundaries, invalid frames, changing/unreadable configuration files, and mocked Win32 mutex success/existing/error paths. Native Windows mutex behavior still requires Windows testing.

## Command failures versus status failures

`run_action` identifies its errors as command failures even for non-modal hotkey/IPC execution. A rejected command or unexpected command exception preserves the connection and cached status, then triggers the existing authoritative status refresh. Actual connection errors still mark the device offline; a failed background status read still invalidates status and disables controls. Foreground command errors keep their existing dialog feedback, while hotkey/IPC errors remain non-modal. No rejected or uncertain command is automatically retried.

`tests/test_graceful_failures.py` covers rejected and unexpected command errors, genuine connection/status failures, and a simulated non-zero Yamaha API response. It does not assume a particular device's response at the volume ceiling.

## Second-launch activation

Window activation uses a separate user-only endpoint: `remote-control.tv.activation.v1.<user-suffix>` or `remote-control.amplifier.activation.v1.<user-suffix>`. It has its own role lock and accepts only window-activation requests, independently of device commands and calibration leases. `ipc_server_name(role, channel=...)` builds the state, calibration, and activation names locally in each self-contained script; existing state/calibration names and file formats are unchanged.

After failing to acquire the single-instance lock, a new process contacts the existing instance without loading device configuration or starting a backend. The handshake supplies the existing process ID; Windows clients call `AllowSetForegroundWindow` before requesting activation. The existing process restores its window and raises any active modal dialog, then acknowledges. OS rules still determine actual foreground focus. Calibration-disabled controls stay disabled, and activation only requests the normal status refresh associated with showing the window.

Connection retries and the handshake share a 1.5-second startup deadline. An unavailable/unresponsive endpoint produces a clear already-running message instead of a silent exit. The server allows four connected clients, bounds frames, and disconnects idle clients after one second. Activation closes before restart/shutdown draining and is recreated if restart fails; it adds no periodic device/IPC polling.

`tests/test_activation.py` launches real second Python processes against isolated local endpoints and private instance locks. It covers hidden/minimized windows, modal dialogs, calibration-disabled state, concurrent launches, delayed startup, malformed/partial requests, idle timeouts, role mismatch, duplicate endpoints, and endpoint recovery. Windows foreground-permission calls are mocked; actual desktop focus behavior requires platform testing.

## Queued volume status reads

Queued keyboard and amplifier-IPC volume steps share bounded batches: a successful step refreshes device status when four commands have completed, the last device reading is at least one second old, or no eligible volume command remains queued. The drain also checks reading age before starting another step. These limits trigger a read; network latency can delay its completion. The existing six-second freshness limit still releases ownership if a device becomes too slow or unavailable. No timer or IPC polling was added.

Volume display and IPC retain the last confirmed device value until the next response; commands do not predict volume or renew freshness. Mute, ordinary button actions, and command failures still request immediate status. Calibration uses its existing 250 ms polling independently, and session/lifecycle transitions clear the batch counter.

`tests/test_volume_polling.py` checks request reduction, final refreshes, elapsed-time bounds during sustained input, route/mute boundaries, error refreshes, and calibration exclusion using a deterministic clock and worker callbacks. Physical device/network latency and Windows hook behavior still require hardware testing.

## Hook startup shutdown safeguard

The hook thread checks its existing stop flag after native message-queue setup and before installation. If shutdown arrives during installation, the thread skips the started notification once it observes the flag and immediately follows its existing unhook cleanup path. Running hooks still use `WM_QUIT` to wake the native message loop. Worker draining, join limits, and restart behavior are unchanged.

`tests/test_hook_lifecycle.py` uses real Python threads with a fake Win32 API and event barriers to force shutdown before thread-ID publication and during hook installation. It also checks normal message-loop shutdown, repeated stop calls, and stopping before start. These tests exercise lifecycle ordering without relying on randomly hitting a race; actual Windows API behavior still requires Windows testing.

## Discovery saves and user configuration edits

Automatic discovery writes use the last accepted file digest as their baseline. After preparing a temporary file, they check the current configuration immediately before replacement; changed, missing, or unreadable files are left alone. A skipped save keeps the baseline unchanged so existing file watching can validate the user's edit and restart only when it is valid. Failed-restart and unknown-baseline guards also prevent discovery saves.

Successful saves return the digest of the exact UTF-8 bytes written, rather than re-reading the destination. An edit made after replacement therefore remains detectable. Default-file creation still works without an existing baseline. Both scripts retain their own implementation and atomic replacement; no shared module or additional polling is introduced.

`tests/test_reliability.py` covers ordinary discovery saves, valid and unfinished edits, deletion/read failures, edits during replacement preparation and immediately after replacement, failed-restart protection, failed replacement cleanup, and initial default-file creation. Temporary files isolate every write.

## Amplifier calibration operations

The amplifier calibration handler supports acquisition only: it verifies native hook availability on Windows and waits for pending device work to drain before acknowledging. The calibration server handles lease renewal/release, and existing polling publishes observations. All calibration volume changes and mute restoration are performed through the TV; ordinary amplifier mute/volume controls retain their existing behavior.

`tests/test_calibration_acquisition.py` checks successful acquisition without device mutations, pending worker/thread draining, timeout and lost-session failures, missing-hook/configuration/shutdown rejection, and rejection of unsupported calibration mutations. The helper does not require an update.

## dB configuration validation

Both conversion settings use the same numeric validation path whether dB display is enabled or disabled. Enabling dB display still requires both settings explicitly in the JSON; otherwise missing values use the existing defaults. Values must be finite numbers, and the step size must be positive. `tests/test_local_cleanup.py` covers both display modes, invalid numeric values, normalization, explicit-setting requirements, and formatted output.

## Starting in the system tray

`--start-in-tray` leaves the newly constructed window hidden when both `SystemTray` and `CloseToTray` are true, an icon was created, and the tray is still available. It never shows/minimizes the main window first. Initialization, configuration feedback, IPC, and visibility-aware polling retain their existing paths; Yamaha global control or connected TV clients can still keep hidden-window polling active. Missing/disabled tray support falls back to an ordinary visible window.

The switch is checked after single-instance activation, so a second launch restores the existing window regardless of the flag. Configuration-driven replacement already forwards command-line arguments and retains the switch. `tests/test_tray_startup.py` checks the visibility/fallback gates, startup feedback, and argument forwarding without live tray icons or devices; `tests/test_activation.py` also exercises a real second process with the switch.

## Dropdown animation workaround

Closed comboboxes retain Qt's native dropdown arrow. The styled arrow area is transparent and borderless, avoiding a separate inset button; text uses symmetric space reservation to remain centered across the whole control. The arrow follows the active Qt/platform style, including disabled states, with no image assets.

The Windows popup fill can lag a frame behind its outline and shadow during native animation, making underlying text appear to flicker. The live-view animation addresses that rendering mismatch.

`DropdownAnimationWorkaround` enables a 200 ms OutCubic animation inside the real Qt popup. Yamaha keeps the actual list and its scrollbars live at their final size, temporarily suspends the popup layout, and grows the native popup frame using elapsed time with an 8 ms precise timer. The full-size list slides within the clipping popup; keyboard navigation and wheel scrolling continue during opening. Cleanup restores the original layout-enabled state, including after native dismissal. Underlying favorites and navigation buttons remain untouched. Qt's animation preference is restored immediately after opening; if combo animations are disabled, the popup appears without the custom animation. Disabling the workaround retains the ordinary Qt popup.

The final popup rectangle comes directly from Qt, including screen-constrained placement for long lists. Upward/downward growth stays inside that rectangle. Finishing or interrupting the animation restores the native geometry and layout, removes the event filter, and stops the timer. Navigation and scrolling remain live throughout opening; selecting or dismissing the popup performs normal cleanup. The centered text/delegate, native frame/shadow, selection signals, and active-popup identity used by shortcut blocking are retained. The implementation is adapted from the supplied live-view comparison prototype and remains self-contained.

`tests/test_dropdown_animation.py` exercises selection, early keyboard input, Escape and native dismissal, repeated opening, owner close/reopen, long-list navigation and both placement directions, disabled animations/workaround, empty lists, and preference restoration on failure. Offscreen tests verify Qt behavior and geometry; the Windows-specific flicker fix and native shadow/animation appearance require desktop verification.

## Amplifier protocol contract checks

Protocol version 1 acknowledgements have integer `id`, boolean `accepted`, and an optional `reason` (`""`, `queue_full`, or `state_changed`). Missing reasons are treated as empty; rejection without an explanation receives generic feedback. Queue-full drops stay silent and commands are never replayed on uncertain delivery. Sony records invalid messages in optional diagnostics with duplicate suppression and displays brand-neutral incompatibility feedback.

Amplifier feedback is stored separately from discovery feedback and survives TV polling. Connection-loss feedback clears when a fresh valid amplifier state arrives again. Timeout, rejection, and other command feedback remain until a pending amplifier command receives an accepted acknowledgement; receiving state does not resolve uncertain command delivery. Error categories are explicit and do not depend on message wording. Acceptance means queued by the amplifier remote, not confirmation that the physical device completed the command.

Both repositories keep identical `tests/fixtures/amplifier_protocol_v1.json` wire examples. Tests compare Yamaha's actual snapshots and acknowledgement production with those examples and feed the same messages through Sony's actual consumer. Fixtures and tests are development-only; the scripts remain self-contained. Changes to the wire contract should update and verify both copies together.

## Local configuration files

`YamahaRemote.example.json` is the tracked connection-settings template, with an empty host. `YamahaRemote.json` is local and ignored by Git; the script generates defaults and prompts for missing settings when it is absent. Crash logs and temporary JSON files are also ignored. Existing local configuration bytes are preserved when removing the file from Git tracking; no Git history is rewritten.

## Window and volume controls

The fixed client width is 303 Qt logical pixels. On the supplied Windows setup, the previous 330 px client measured 332 px including its border, so the new width matches the supplied Sony measurement of 305 px overall. Window decorations vary by platform, and Qt handles display scaling. The input combobox has the same 34 px minimum height as the buttons; the fitted window grows by 4 px compared with its previous 30 px height. The volume row uses three equal-stretch columns with 6 px spacing, compact −/+ labels, and a centered 13 pt semibold display. Power is above the Input and Volume sections; Mute and the speaker selector share the row below volume controls. Raw/dB conversion and draggable-label behavior remain unchanged.

Qt button auto-repeat uses a 500 ms initial delay and 100 ms interval. The `pressed` signal performs the immediate and repeated adjustments; releasing does not add a step. Adjustments use the existing serialized action path and its busy, connection, and calibration guards. No new worker queue, device polling timer, or global-key ownership behavior is introduced.

## Speaker outputs and grouped controls

The standalone remote uses Sony-style Input and Volume frames with a full-width power button above them. Input selection precedes a two-column favorites grid; favorites retain JSON ordering and Hidden/Favorite independence. The volume controls keep their hold/repeat behavior, with equal-width Mute and speaker controls matching the two-column favorites. Both selectors use the existing live dropdown animation and native indicator. The fitted height is recalculated after styled controls and discovered favorites are added; the client width remains 303 px.

Speaker capabilities are discovered independently through `system/getFeatures` and its `speaker_a`/`speaker_b` function names. An A-only device offers Off/A; A+B devices offer Off/A/B/A+B (B-only capabilities are also handled). Missing capabilities leave a disabled explanatory selector. Valid speaker states require boolean values in `system/getFuncStatus`. Once capabilities are known, supported devices read that state alongside the existing main status poll while powered on; optional speaker-read errors disable only the speaker selector, without invalidating valid power/volume status. There is no separate periodic polling timer.

`YamahaApi.set_speakers()` reads the current outputs, sends OFF commands for unwanted outputs first, and verifies each intermediate state before any ON command. It allows up to 3 seconds of lagging status after each SET, checking at 100 ms intervals through the existing bounded HTTP API. It does not resend an uncertain SET. The entire operation uses the existing serialized worker pool rather than blocking or pumping GUI events. Failures preserve the normal exception types and command-error handling; partial failure leaves speaker state unknown until the next valid read.

The GUI displays the requested tuple immediately. A target remains displayed over stale GET results while the worker runs and for a 3-second grace interval after completion, unless a matching read confirms it sooner. After that interval the latest actual state takes precedence. Busy/rejected requests revert to the existing target or confirmed state; command failure clears optimism. Calibration and shutdown guards apply to these operations. The calibration helper also uses confirmed speaker state for output isolation, as described below.

`tests/test_speakers.py` simulates delayed state propagation with a virtual clock, verifies disable-before-enable sequencing, A-only/unsupported combinations, strict status parsing, timeout behavior, optional-read failure isolation, and optimistic reconciliation. A real Qt worker test holds a fake SET open while GUI events continue. Section/favorite layout and existing volume hold behavior are covered by offscreen GUI tests; physical relay switching still needs device verification.


## Calibration speaker isolation

Calibration-only amplifier snapshots include generic `speaker_outputs`, confirmed `speaker_state`, and `speaker_valid_for_ms` facts. Freshness is tracked separately from main status; optimistic selector targets are never published as confirmed speaker isolation. Ordinary amplifier state messages and mapping JSON are unchanged.

The exclusive calibration lease permits a serialized `set_speakers` operation. It uses the existing disable-first API verification and publishes the confirmed result before acknowledging success. Uncertain commands are not replayed. Unsupported controls, an explicit rejection, or failure to verify isolation offers one Cancel-default Continue/Cancel decision per sweep. Continuing applies for the entire session; later speaker changes are logged without more dialogs. Connection, lease, identity, and volume failures remain fatal.

The helper keeps Windows muted and all sources must remain silent: speaker OFF does not guarantee isolation of TV speakers, headphones, or other outputs. While the decision is open, its worker keeps reading state and renewing leases; user decision time is excluded from volume-step deadlines. Cleanup restores and verifies original volumes and mute before intentionally restoring original speakers, then verifies the combined state and restores Windows mute last. A volume-restoration failure does not re-enable speakers; failed cleanup leaves Windows muted where possible and preserves the previous mapping. Original speaker settings live only in session memory, with no recovery journal or resume workflow.

Fake-device session tests cover unsupported isolation, rejection, repeated loss with one decision, long decisions with live lease renewal, communication failure, cancellation, and restoration failures. Qt tests cover the warning and worker handoff; amplifier tests cover confirmed snapshots and the leased command. Physical speaker switching during a full sweep still requires device verification.


## Window state across configuration restarts

Configuration replacements receive one-shot internal arguments containing Qt's saved window geometry and current visible/minimized/tray state. Prior restart arguments are replaced rather than accumulated, and unrelated startup arguments are preserved. Qt restores screen geometry without platform-specific window-manager code; a hidden window opens visibly if tray support was disabled or is unavailable. Initial launches still honor `--start-in-tray`. Focus is left to the operating system; no forced activation is requested. Window size remains governed by the current control layout.
