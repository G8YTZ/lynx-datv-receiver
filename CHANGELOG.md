# Changelog

All notable changes to Lynx are documented here, in reverse chronological order. Kept as short, scannable headlines - see the git history for full detail on any entry.

## 2026-08-06

**Fixed**
- Root-caused and fixed the overnight stability crashes - traced to `labwc` no longer sending frame callbacks (overlay stalls) and possible GPU/WiFi firmware state surviving warm reboots. A full `apt full-upgrade` (new kernel + graphics drivers) applied as the leading fix candidate.
- OS package updates now run automatically as part of "Update Now" - previously only ever applied once, at initial install.
- NTP wait at startup extended 10s → 45s (was cutting off before a real sync sometimes finished).

**Added**
- Overlay heartbeat monitoring, with a genuine SysRq hard reset (not just `sudo reboot`) if it goes stale and doesn't recover, rate-limited to avoid reboot loops.
- Independent watchdog daemon, running outside Lynx's own process tree, with optional (not yet field-tested) PoE power-cycle support.
- Persistent boot-to-boot logging enabled.

**Known limitation**
- Not yet confirmed which of (graphics driver / WiFi firmware) was the actual root cause of the crashes.
- SysRq/PoE recovery not yet proven live - deliberately held back from `main` for now.

## 2026-08-04

**Fixed**
- Volume slider redesigned: first to a 0-11 "Spinal Tap" scale, then to dB as the actual primary unit.
- QRZ name-lookup timeout increased 4s → 10s.
- WiFi power-save disabled automatically at startup - root cause of an earlier overnight crash.
- New Beta update channel - switch between stable/beta from the Web UI.

## 2026-08-03

**Fixed**
- Fixed a JS-escaping bug in the Shutdown confirm dialog that broke the entire main page.
- Hardened render-confirmation against a wedged GPU decoder giving a false "rendering confirmed".

## 2026-08-01

**Added**
- `tri_watch`: simultaneous RF+RF+stream monitoring on one receiver, with simple arbitration and an on-screen "someone else wants in" notification.
- QRZ name lookup for the waiting-station notification.

**Fixed**
- Extensive real-hardware debugging to get `tri_watch`'s Rx2 display working reliably - several distinct root causes found and fixed, eventually resolved.
- Several QRZ/Slack/Companion notification bugs under `tri_watch`, all from the same root pattern (logic assuming Rx2 only matters in diversity mode).
- New Tri-Watch Sources config card, Shutdown/redefined-Stop buttons, and QRZ password-field masking (several attempts needed to get right).

## 2026-07-31

**Fixed**
- Fixed a JS-escaping bug that froze the entire main page.
- Fixed `lynx.service` failing to start under systemd (`Type=notify` → `Type=simple`).
- Fixed `install.sh` reintroducing a known-broken systemd-only autostart on fresh installs.
- Fixed a false "mpv crashed" detection while genuinely idle.
- Fixed stream playback failing when the Picotuner isn't configured.
- Fixed autostart breaking for any non-"pi" username.
- Fixed incorrect diversity-mode documentation in the install guide.
- Added copy-pasteable passwordless-sudo setup instructions to error messages.

**Known limitation**
- `graphical-session.target` never activates on this labwc setup - genuinely unresolved, `lynx.service` disabled again in favour of the proven autostart line.

**Changed**
- PPM meter default style changed to "full_fat".
- First version tag created.
- 14 preset memories now ship by default on a fresh install.

## 2026-07-30

**Added**
- BBC-style stereo PPM meter on the OSD.
- Manual version checking/updating (Check Updates / Update Now buttons).
- `lynx.service`, a proper systemd unit replacing the bare autostart line.

**Changed**
- OSD layout reshuffled for the new PPM meter.
- Config page cards rebalanced.
- Volume display switched to dB.

**Known limitation**
- PPM calibration only holds at 100% volume.

## 2026-07-29

**Added**
- Portable locator override for QRZ Logbook.
- Test QRZ Logging tool on Diagnostics.
- New Diagnostics event categories for notifications/QRZ.
- 3cm band added to QRZ band derivation.
- Picotuner auto-discovery on the Config page.
- Mouse cursor auto-hide on boot.

**Fixed**
- QRZ Logbook silently failing due to a kHz/MHz frequency bug.
- QRZ Logbook logging the wrong (IF, not downlink) frequency with an LNB configured.
- QRZ mode field missing the DVB-S2 standard name.
- Slack `{frequency}` placeholder showing 1000x too low.
