# Changelog

All notable changes to Lynx are documented here, in reverse chronological order. Kept as short, scannable headlines - see the git history for full detail on any entry.

## 2026-08-08

**Fixed**
- `install.sh` dependency check now runs as part of every "Update Now", not just at initial install - closes a real gap: `wtype` was found missing on production despite being correctly listed, since a fresh dependency added (or somehow lost) after initial install had no path to ever reach an existing system.
- A literal `dbm=0` from the Picotuner was being shown as a real reading (both Web UI and OSD) - confirmed as a genuine Picotuner firmware quirk at higher Tx gain, specific to tuner B. Now treated the same as "no reading."
- Startup network-wait check now pings the local default gateway instead of a fixed internet host - fixes a misleading "no network detected" when outbound ICMP is blocked but the local network (and internet access generally) is fine; also correctly supports genuinely offline/RF-only setups. Extended 20s → 45s after confirming the route can genuinely take longer than that to appear on some hardware. `PATH` is now also set explicitly at script start, as a defensive measure.

**Added**
- Consistent button-style navigation on all three pages (previously only the main page had it).
- LNB PSU control - independent H/V (18V/13V) buttons per plug, plus a Tone toggle for Hi-Band LNBs (defaults off, since Amateur TV never needs it). Sent as a standalone command, re-applied on every startup with a settling delay for the voltage to stabilise before the first tune attempt. The indicator reflects the Picotuner's own reported state, not just "a command was sent."
- Shutdown and a redefined Stop button, ported from beta - Shutdown gracefully powers the whole Pi off (does not come back up on its own); Stop now closes the Lynx app and returns to the desktop.
- A general diversity stuck-lock watchdog - catches the known "correctly tuned, good margin, but never locking" state whenever it happens, not just at startup. Deliberately keyed on margin rather than MER, so a genuinely empty tuner (e.g. the opposite polarisation) is correctly left alone rather than endlessly re-tuned.

**Changed**
- "Manual Tune" label clarified to "Manual Tune (kHz)".
- Configured site name removed from the header.

**Docs**
- Web UI manual updated (v1.5) - new navigation, LNB PSU, Shutdown/Stop, and a note that only the v3 Picotuner board is supported.
- Installation guide updated (v1.8) - recommends a genuine SSH terminal over VNC/remote-desktop sessions for pasting commands, after a real install was broken by a locale-related encoding issue introduced that way.

## 2026-08-06

**Fixed**
- WiFi power-save disabled automatically at startup - root cause of a real overnight crash on the beta channel, ported here now that it's confirmed.
- NTP wait at startup extended 10s → 45s (was cutting off before a real sync sometimes finished).

**Added**
- Update Channel switching (stable/beta), mirroring the model BATC Portsdown uses.
- Kill/Restore WiFi - a control to fully disable and re-enable the WiFi radio from the Web UI.
- OS package updates now run automatically as part of "Update Now" - previously only ever applied once, at initial install.
- Overlay stall detection - a heartbeat file plus a stack-trace dump if it ever goes stale, so a stalled-but-alive overlay can actually be caught and diagnosed.

**Changed**
- Volume slider redesigned to dB as the primary unit, with a cosmetic 0-11 "Spinal Tap" readout alongside it.

**Known limitation**
- Automatic hard-reboot recovery and PoE-power-cycle support (both on `beta`) are deliberately not yet on `main`, pending real-world proof they work correctly.

## 2026-08-01

**Added**
- Diagnostic capability (`SIGUSR1` stack-trace dump) for tracking down an intermittent, hard-to-reproduce hang.
- Persistent `/var/log/lynx/` logging directory, so diagnostic logs survive a reboot instead of living only in volatile `/tmp`.

**Fixed**
- Mouse cursor-hide only ever fired once at startup - now re-triggered periodically so it recovers if a mouse is plugged in later.
- `wtype` (needed for cursor-hide) was missing from `install.sh`'s dependency list.

**Known limitation**
- A real, confirmed, intermittent freeze/hang on a Pi 500 - not yet root-caused at the time.

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
