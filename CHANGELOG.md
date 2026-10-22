# Release history

Release identifiers below come from the corresponding commits. Some releases updated only the calibration helper; the remote and helper can therefore have different version numbers. Historical behavior may have been superseded by later releases.

## 2026-10-22

- Yamaha Remote 2026.10.22.1 — Include session-only polling success, empty-inventory, failure-category and recovery counters in both dump formats, plus grouped request totals. Preserve polling, exceptions, removal and recovery behavior.

## 2026-10-21

- Yamaha Remote 2026.10.21.14 — Show the full-report privacy warning in the save confirmation popup and console output, as well as in the JSON.

- Yamaha Remote 2026.10.21.13 — Add --dump-state-full with timestamped cached decoded responses for allowlisted polling/discovery reads. Keep ordinary dumps compact and command payloads, headers and submitted text uncached. No fresh queries; document identifiers and inventory privacy.

- Yamaha Remote 2026.10.21.12 — Add --dump-state cached diagnostic reports through second-launch IPC, without activating the window or querying devices. Record bounded request outcomes, omit credentials/text/payloads, use timestamped files with app-data fallback, and ignore generated reports in Git.

- Yamaha Remote 2026.10.21.11 — Track automatically maintained `Removed` flags without deleting input preferences or JSON positions. Confirm absence with another successful inventory after five seconds, retain entries on errors/standby, restore rediscovered entries, and rebuild dropdowns/favorites with immediate window resizing. Reuse ordinary status/input polling and add a 30-second visible/awake inventory refresh plus guarded verification; preserve unapplied JSON edits.

- Yamaha Remote 2026.10.21.10 — Add the focused TV-volume exception to global amplifier shortcuts. Prepare the TV handler before advertising leased readiness; the amplifier yields only matching bindings for the actual TV foreground window while using another known input. Preserve global ownership elsewhere, SMTC priority, calibration guards, expiry and disconnect recovery. Add native-hook and cross-repository IPC regressions.

- Yamaha Remote 2026.10.21.9 — Replace the global enable toggle with independent configurable GlobalShortcuts for volume/mute. Preserve global settings on migration, validate native bindings before restart, keep bounded press ownership, and publish per-binding IPC ownership. Global bindings take priority over SMTC and focused shortcuts; retain hidden polling and offline/standby passthrough.

- Yamaha Remote 2026.10.21.8 — Add configurable focused `Shortcuts` with readable Qt key strings, multiple bindings, explicit disabling, conflict validation, missing-default insertion, and generated button tooltips. Preserve native Windows volume suppression, press ownership/repeat rules, popup safeguards, and calibration key isolation. Global volume keys/IPC ownership remain unchanged for phase two.

- Yamaha Remote 2026.10.21.7 — Elide long closed-combobox and favorite labels at the right edge, keeping the leading text. Keep favorite columns equal and prevent names from widening the layout. Truncation follows available pixel width; full names and action targets remain intact. Popup-list rendering is unchanged.

- Yamaha Remote 2026.10.21.6 — Clarify the half-interval freshness conversion, keep activation deadlines/retry sleeps in seconds with conversion only at Qt waits, and share a local validity helper between main and speaker IPC snapshots. Add boundary checks and a real-window periodic-timer/event-refresh regression. Timing defaults and wire format are unchanged.

- Yamaha Remote 2026.10.21.5 — Always pass polling groups explicitly when dispatching refreshes; remove the redundant all-groups branch. Remove the Yamaha polling-eligibility forwarding method and unused test-fixture fields. Keep the defensive shutdown timer stops.

- Yamaha Remote 2026.10.21.4 — Keep periodic polling on its fixed cadence and skip main or speaker status while its refresh is pending or its response is less than one second old. Volume/mute follow-ups refresh main status only; verified speaker responses also count as fresh. Failed reads remain retryable, speaker-only updates do not extend main/hotkey freshness, and explicit follow-ups/calibration bypass freshness.

- Yamaha Remote 2026.10.21.3 — Add a local polling coordinator for periodic cadence, immediate/delayed coalesced refreshes, visibility/global-hotkey/IPC-client eligibility, and pause/resume. Defer requested refreshes behind active commands, status polls, or calibration operations while preserving volume-batch freshness limits and combined status reads.

- Yamaha Remote 2026.10.21.2 — Share one successful system/getFeatures response between input and speaker discovery for each API instance. Retry failed/malformed capability responses; keep name and speaker-state reads separate.

- 2026.10.21.1 — Expand dropdown popups one Qt logical pixel on each side while leaving combobox buttons unchanged. Use the expanded geometry throughout the live opening animation.

## 2026-10-20

- 2026.10.20.10 — Rename the speaker-completion callback to avoid duplicate closure names and correct the earlier Mica changelog wording. Detect missing Qt theme support before acquiring the instance lock, display an explicit upgrade message, and remove unused imports.

- 2026.10.20.9 — Read the initial speaker state immediately after capability discovery so the selector does not wait for a later periodic status poll. Preserve optional-read failure isolation and the existing speaker SET grace period.

- 2026.10.20.8 — With Windows Mica, restore custom button/combobox/text-input fills and outlines, hover/pressed/disabled styling, section borders, and matching dropdown colors. Use opaque control fills to keep the backdrop out of controls; other configurations retain Qt-native colors.

- 2026.10.20.7 — Give the power/standby button an opaque dark/light background only with Windows Mica, including hover/pressed states. Other native controls retain Qt-managed styling.

- 2026.10.20.6 — Restore compact control padding and previous dark/light section fills only with Windows Mica; retain Qt-managed control colors and explicit JSON theme selection.

- 2026.10.20.5 — Request light/dark appearance through Qt 6.8+ platform integration. Remove fixed control colors and the manual dark title-bar override; retain layout, typography, Mica support, and live dropdown animation. Windows appearance and live desktop-theme changes require visual verification.

- 2026.10.20.4 — Preserve Qt window geometry and visible/minimized/tray state across configuration restarts. Startup-in-tray arguments apply only to the initial launch; disabling tray support makes a hidden replacement visible.

- 2026.10.20.3 — Publish independently fresh, confirmed speaker state to calibration clients and support exclusive, verified speaker changes. Enable helper isolation and volume-first restoration without changing ordinary amplifier IPC or optimistic speaker UI. Add command and snapshot regression tests.

- 2026.10.20.2 — Give Mute and the speaker selector equal-width columns, matching the favorite buttons. Use compact Off/A/B/A+B labels and keep the speaker-output tooltip.

- 2026.10.20.1 — Add advertised A/B speaker-output selection using system/getFuncStatus and system/setSpeakerA/B. Optimistically display requested combinations and allow delayed GET updates, verifying unwanted outputs off before enabling others. Move controls into Sony-style Input and Volume sections, place favorites below the input selector, and fit Mute beside Speakers. Preserve standalone operation and existing input/volume/IPC behavior; add fake-device safety, timing, and layout coverage. Calibration integration is a separate follow-up.

## 2026-10-19

- 2026.10.19.19 — Restore the native Qt dropdown arrow in a borderless, transparent right-hand area. Keep labels centered across the full combobox and retain the live-view popup animation; no icon files are required.

- 2026.10.19.18 — Increase the input combobox height from 30 to 34 pixels to match the buttons. Verify that fitted window height grows by exactly 4 pixels in both light and dark themes.

- 2026.10.19.17 — Remove extra side insets from the volume row so the −/+ buttons align with the outer edges of the mute/power buttons above. Check alignment in both themes.

- 2026.10.19.16 — Reduce client width from 330 to 303 pixels, matching the supplied Sony width of 305 pixels overall with the reference Windows border. Restyle the volume row with equal columns, compact −/+ labels, and 13-point semibold volume text; preserve raw/dB conversion. Add immediate volume-button presses and repeat after 500 ms at 100 ms intervals, without a release step. Add offscreen geometry and hold/release regression coverage.

- 2026.10.19.15 — Make tray startup tests independent of the checkout folder name and describe dropdown flicker as a popup fill/frame timing mismatch. No Yamaha runtime behavior changes.

- 2026.10.19.13 — Replace tracked device settings with a sanitized `YamahaRemote.example.json`; ignore local settings, crash logs, and temporary JSON files. Make hook lifecycle tests independent of the checkout folder name. Remove the unused configuration-loader path variable.

- Protocol contract coverage — Add the same version-1 wire fixtures as Sony Remote and verify actual amplifier snapshots and command acknowledgements against them. Document optional acknowledgement reasons and feedback behavior. Yamaha runtime remains 2026.10.19.11.

- 2026.10.19.11 — Add the same live-view input-dropdown animation as Sony Remote, with the `DropdownAnimationWorkaround` setting (default enabled on Windows). Keep scrolling and keyboard navigation live, preserve native placement and disabled system animations, and add popup lifecycle regression coverage.

- 2026.10.19.9 — Add `--start-in-tray`, gated by both tray settings and actual tray availability. Start hidden without a visible-window flash, keep ordinary fallback/second-launch activation and restart arguments, and add startup regression tests.

- 2026.10.19.6 — Consolidate dB numeric validation into one path and remove the unused required-number helper. Preserve explicit settings when dB display is enabled, numeric normalization, finite-value checks, and the positive step requirement. Extend configuration regression coverage.

- 2026.10.19.4 — Remove unused direct amplifier calibration mutations and obsolete volume-command handling. Keep session acquisition, worker draining, hook checks, leases, and state publishing; add focused acquisition regression tests.

- 2026.10.19.2 — Preserve unapplied JSON edits when saving discovered inputs/applications. Compare the accepted file digest immediately before replacement and track the exact saved bytes so later edits still trigger validation. Add discovery-save regression tests.

- 2026.10.19.1 — Skip native keyboard-hook installation if shutdown was requested during startup, and avoid reporting installation after shutdown is observed. Add deterministic startup/stop lifecycle tests; worker shutdown behavior is unchanged.

## 2026-10-18

- 2026.10.18.36 — Batch queued volume commands with status refreshes after at most four commands or one second, plus a final refresh. Keep displayed/published volume authoritative and retain immediate error refreshes and calibration polling. Add bounded-batch regression tests.

- 2026.10.18.35 — Second launches restore the existing window through a separate user-local activation endpoint, including from tray/minimized state. Add bounded startup retries, Windows foreground permission, restart recovery, and cross-process regression tests.

- 2026.10.18.34 — Preserve connection and cached status after rejected commands, refresh authoritative status, and retain existing offline/status-failure handling. Add focused graceful-failure regression tests.

- 2026.10.18.33 — Drain IPC reads with frame bounds, use cached Win32 mutex errors, hash/validate the same configuration bytes, and derive published freshness from the hotkey age constant. Add focused reliability regression tests.

- 2026.10.18.31 — Derive configuration validation from defaults, initialize persistent state explicitly, centralize local endpoint naming, and add development-only regression tests. Scripts remain self-contained; IPC names and calibration file format are unchanged.

- 2026.10.18.28 — Bypass system/environment proxies for device requests using a dedicated direct-connection opener.

- 2026.10.18.27 — Reject invalid numeric IP-like hosts during configuration validation; correct the Sony sample address.

- 2026.10.18.26 — Silently drop amplifier IPC queue overflow and report other IPC failures non-modally. Update both remotes together.

- 2026.10.18.25 — Require one second of hovering before showing tooltips, including when moving between controls.

- 2026.10.18.24 — Show keyboard shortcuts in button hover tooltips.

- 2026.10.18.19 — Share Yamaha’s formatted volume and unit over IPC; Sony mirrors them when no calibration matches. Update both remotes together.

- [2026.10.18.15](https://github.com/cnieuweboer/Yamaha-Remote-Control/commit/a288584) — Remove obsolete calibration state and branches in Yamaha Remote 2026.10.18.15.
- [2026.10.18.10](https://github.com/cnieuweboer/Yamaha-Remote-Control/commit/1f11e23) — Publish amplifier identity for calibrated routing in version 2026.10.18.10.
- [2026.10.18.5](https://github.com/cnieuweboer/Yamaha-Remote-Control/commit/9a3c5b5) — Add exclusive calibration sessions and temporary polling for version 2026.10.18.5.
- [2026.10.18.4](https://github.com/cnieuweboer/Yamaha-Remote-Control/commit/012873b) — Keep volume-wheel overflow from reaching Windows in version 2026.10.18.4.
- [2026.10.18.2](https://github.com/cnieuweboer/Yamaha-Remote-Control/commit/12e9700) — Add amplifier state IPC and volume routing for version 2026.10.18.2.
- [2026.10.18.1](https://github.com/cnieuweboer/Yamaha-Remote-Control/commit/4467fb0) — Add focused and global volume hotkeys for version 2026.10.18.1.

## 2026-10-17

- [2026.10.17.7](https://github.com/cnieuweboer/Yamaha-Remote-Control/commit/d3b6b2b) — Align remote structure and behavior for version 2026.10.17.7.

See the [full commit history](https://github.com/cnieuweboer/Yamaha-Remote-Control/commits/main/) for earlier changes and documentation updates.
