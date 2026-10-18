# Release history

Release identifiers below come from the corresponding commits. Some releases updated only the calibration helper; the remote and helper can therefore have different version numbers. Historical behavior may have been superseded by later releases.

## 2026-10-19

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
