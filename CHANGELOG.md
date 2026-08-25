# Changelog

All notable changes to Lynx are documented here, in reverse chronological order. Kept as short, scannable headlines - see the git history for full detail on any entry.

## 2026-08-25

**Action required after updating**
- **Reboot the Pi by hand once.** "Update Now" runs whatever is already loaded in memory, so the update that delivers these fixes is still carried out by the code they replace. Every update after this one restarts on its own.
- **If Reboot, Shutdown or Kill WiFi still do nothing, run the installer once:** `cd ~/lynx && ./install.sh`. Those buttons need passwordless sudo. Raspberry Pi OS normally grants it to the first user, but not always - a user added later, or some headless setups, may not have it, and both receivers examined while fixing this turned out to lack it. `install.sh` configures it properly, but it needs sudo to do so, so it has to be run from a terminal where you can type your password once. It cannot be done from the Web UI, and no amount of updating will fix it on its own. After this release those buttons say so in the log rather than silently doing nothing, so you will know which situation you are in.

**Fixed**
- The Picotuner tuning watchdog could take the tune lock away from a tune already in progress. Its restore path called `_tune_impl()` directly rather than going through `tune()`, which is what acquires the lock - but `_tune_impl()` is written assuming the caller already holds it, and hands it to its own mpv thread to release in a `finally`. So the watchdog had a thread releasing a lock nobody had taken: `RuntimeError: release unlocked lock` in the log, and, far worse, the lock freed regardless of who was holding it. An operator tune underway at that moment lost the lock mid-sequence and two tune sequences then ran against the Picotuner at once, interleaving their commands. Reported from the field as a receiver that was reachable, responsive and visibly healthy, but whose frequency simply would not change - and self-sustaining, because a restore that never completed left the mismatch in place, so the watchdog fired again on the next cooldown and kept doing so. The restore now takes the lock exactly as `tune()` does, and backs off if a tune is already running; nothing is lost when it does, since the watchdog still sees the mismatch and retries.
- Passwordless sudo is configured by the installer rather than left to the operator. Reboot, Shutdown, Update and Kill WiFi all shell out to sudo, and Raspberry Pi OS usually grants it - but not always, depending on how the user was created, and when it is missing the buttons fail in a way that reads like a broken install. The rule is validated with `visudo -c` before being installed, so a malformed entry can never lock anyone out of sudo, and superseded `lynx-reboot`/`lynx-shutdown` drop-ins from the older manual instructions are removed. Uses `id -un` rather than `$USER`, which is not guaranteed to be set when the script runs from inside the Lynx process.
- That block, and the `/var/log/lynx` directory, are placed above the `--deps-only` exit so they reach receivers being updated and not only fresh installs - the same mistake, and the same fix, as the chrony setup on 2026-08-21 on beta. Both are idempotent and safe to redo on every update.
- "Update Now" could sit for twenty minutes doing nothing visible and then not reboot. The dependency step runs `install.sh`, which needs sudo throughout - for apt, for writing to `/etc` - and on a receiver without passwordless sudo it stopped at a password prompt with no terminal to answer on, waiting out the full timeout in silence. It is now guarded by a blanket sudo check, run with stdin closed so any prompt fails immediately rather than blocking, and skipped with an explanation rather than attempted and hung. The reboot is judged separately and always attempted, so a receiver that cannot refresh its dependencies still restarts and still runs the new code.
- Every power action goes through one place, passes `-i`, and reports its own failures. Reboot, Shutdown, the post-update reboot and the channel-switch reboot were four separate call sites, three of them firing into a bare `Popen` with the result discarded - so a refusal left the button looking like it had worked, which is what turned a one-line error into a two-day hunt. `-i` is there because these go through logind, which can refuse while a user is logged in on seat0 - permanently true on a receiver, where the desktop user is auto-logged-in, and not something root overrides. Observed once refusing with exactly that message; not reproducible on demand afterwards, so `-i` is insurance rather than a proven cure, and the captured error is the part that actually matters. Shutdown now uses `poweroff -i` rather than `shutdown -h now`, which is the same systemd action but has no `-i`.
- The sudo instructions in those error messages could not be run as printed. Three commands separated by newlines inside a JSON error detail collapse to one line in a browser; pasted as a single line, `tee` swallowed the rest as filenames and reported `-c invalid argument`. Confirmed in the field. Replaced with one `&&`-chained command that survives being collapsed and validates the rule before installing it.
- The app log survives a reboot. It was written only to `/tmp`, which is cleared on restart - so the one event most worth having a log of was also the one guaranteed to destroy it, and a refusing Reboot button had to be diagnosed from a dropped SSH session two days later. Now written to `/var/log/lynx` as well when that is available, appended across boots with a dated start marker, rotated at 20 MB with one generation kept, and degrading quietly to `/tmp` alone if the directory is missing.

## 2026-08-16

**Fixed**
- Lynx would not start at all on main. The Picotuner LNB restore added on 2026-08-15 was ported across from beta, where `picotuner_rcv2_cmd()` takes a config dict as a second argument; on main it takes the command only. Both call sites raised `TypeError` at startup, before the web app came up. Fixed by matching main's own signature.
- The GPIO Tx pin and Bitfocus Companion's source-switching webhook never fired for a web stream. Both followed the RF tuners' lock state alone, so a repeater relaying a stream would neither key its transmitter nor switch its vision mixer to the receiver. Reported from DB0OV. Both now follow "is there a picture to transmit" rather than "is RF locked", with the settling timers and schedule-window behaviour unchanged. Deliberately a SEPARATE signal from the RF lock rather than a widening of it, so each output gets the question it actually wants: Companion, the GPIO Tx pin and Slack all fire for a stream, while QRZ does not - a logbook entry needs a callsign and a stream has none. Slack uses its own `stream_message_template`, since the RF template's placeholders (callsign, MER, MODCOD, frequency) mean nothing for a stream; only `{site_callsign}` and `{site_callsign_lower}` apply.

## 2026-08-15

**Fixed**
- The Picotuner losing its tuning left the receiver permanently deaf. Its WinterHill firmware keeps neither tuning, symbol rate nor LNB supply across a power cycle, so anything that restarts it - a PoE renegotiation, a supply blip, static - brought it back with nothing set. It would resume broadcasting, Lynx would report a perfectly healthy tuner, and the receiver would never lock again. At an unattended repeater that is silent, total, and indistinguishable from a quiet band. Lynx now continuously compares the frequency, symbol rate and LNB supply the Picotuner reports against what they should be, and restores any sustained disagreement. Deliberately a state comparison rather than a timing check: a Pico reboots in seconds, so watching for a gap in its broadcasts is both easy to miss and blind to every other cause. Safe to run continuously because a tuned receiver keeps reporting its frequency even with no signal on it ("437.000B lost" is a correctly tuned receiver hearing nothing), so a quiet band is left completely alone.
- LNB supply is restored from the configured value, not the reported one. The broadcast carries "LNB supply X/Y" and Lynx updates its own globals from it so the UI buttons show the truth - which means that after a power cycle those globals already read "off". The configured value is the only thing that still knows what the supply is meant to be. Checked in BOTH directions: a Picotuner powers up with its supply ON (18V observed on plug A on real hardware), so a plug configured "off" coming back live matters most - failing to apply a supply costs a picture, applying one that should not be there can reach a preamp or antenna that is not expecting it.
- Same fix at startup, which had the identical flaw: the LNB re-apply skipped any plug configured "off", so a Pi rebooting while its Picotuner sat at 18V left the voltage on.
- Plug B's LNB controls looked broken when they were simply absent. The PicoTuner has only one LNB voltage generator fitted as standard, and its firmware reports the second plug as "absent" - but Lynx mapped that to "off", so the buttons appeared live, turned green on a click and reverted on the next status poll. Confirmed by sending `vgy` to every documented command port (9920, 9921 and 9922): the state never changes, because there is no hardware there to switch. "absent" is now its own state - the buttons are disabled and explain why on hover, and the tuning watchdog skips that plug. A second generator can be fitted by hand; see the BATC PicoTuner wiki.
- Unrecognised "LNB supply X/Y" values are now logged once each rather than silently ignored, so a future overload or fault indication from the firmware would be visible rather than quietly leaving Lynx showing the last value it understood.

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
