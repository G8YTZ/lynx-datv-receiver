# Changelog

All notable changes to Lynx are documented here, in reverse chronological order. Kept as short, scannable headlines - see the git history for full detail on any entry.

## 2026-08-18

**Added**
- Audio output device is now selectable on the Config page, and defaults to HDMI rather than letting mpv choose. Requested by G8GKQ, who lost sound for a while before discovering a USB audio dongle plugged in for something else had captured it - mpv's automatic selection takes whatever ALSA offers first, which is not necessarily the television. On a PipeWire system the selector offers PipeWire's own nodes, not the raw ALSA devices - PipeWire owns the sound card, and telling mpv to open ALSA directly either fights it for the device or bypasses the graph, which produces no sound and no PPM at all. It falls back to grouped ALSA cards only on systems without PipeWire. Either way the selector lists one entry per physical output rather than everything mpv can name - a Pi with two HDMI ports reports around twenty devices, being five ALSA access paths to each card plus backend defaults for pipewire, pulse, alsa, jack, sdl and sndio, of which only two are things anyone would recognise as an output. They are named "HDMI 1" and "HDMI 2" rather than "vc4-hdmi-0, MAI PCM i2s-hifi-0/Default Audio Device". "hdmi" resolves at launch rather than being hardcoded, since the card number varies between Pi models and ports.

- The PPM meter now follows mpv's audio device rather than always tapping the default sink. The meter is deliberately independent of mpv - its own astats path is broken upstream - but that independence became a problem the moment the output device became selectable: with mpv sent to HDMI while the default sink was a USB dongle, the meter would have sat watching a silent dongle and read nothing, with no indication why. It now follows mpv. On PipeWire that is exact rather than inferred, since mpv's device name is "pipewire/<node>" and the monitor is simply "<node>.monitor" - the two cannot diverge. Falling back, in order, to searching PipeWire for a matching ALSA card, then to the default sink. The ALSA fallback comparison strips punctuation from both sides, since ALSA calls a card "vc4hdmi0" while PipeWire records it as "vc4-hdmi-0" and a plain substring match fails on the hyphens.

**Fixed**
- Pathfinder drew no land at all for close-range contacts outside the UK - just town names floating on an empty sea. Reported from Nordenham (DL5BCA) at 19km. The visibility test asked whether any VERTEX of a shape fell inside the window, which fails when the window is smaller than the shape and sits entirely inside it: at 19km the frame is about 27km across and lies well within Germany's outline, so not one vertex of that polygon was in view and the whole country was skipped as invisible. Now a bounding-box overlap test, which cannot make that mistake. Nobody had seen it before because Great Britain is an island - the UK's country polygon IS its coastline, so any UK contact has vertices within a few tens of kilometres and passed by luck. The first continental user at close range hit it immediately.

## 2026-08-16

**Fixed**
- The GPIO Tx pin and Bitfocus Companion's source-switching webhook never fired for a web stream. Both followed the RF tuners' lock state alone, so a repeater relaying a stream would neither key its transmitter nor switch its vision mixer to the receiver. Worse under Tri-Watch, where a stream is a first-class source: the transmitter would drop the moment the arbitrator switched to it, with the picture still going out on the network. Reported from DB0OV. Both now follow "is there a picture to transmit" rather than "is RF locked" - stream, single Rx, diversity and Tri-Watch all drive the pin and the webhook, with the settling timers and schedule-window behaviour unchanged. Deliberately a SEPARATE signal from the RF lock rather than a widening of it, so each output gets the question it actually wants: Companion, the GPIO Tx pin and Slack all fire for a stream, while QRZ does not - a logbook entry needs a callsign and a stream has none. Slack uses its own `stream_message_template`, since the RF template's placeholders (callsign, MER, MODCOD, frequency) mean nothing for a stream; only `{site_callsign}` and `{site_callsign_lower}` apply. The point of the Slack alert is telling people the repeater is in use, and someone watching for that does not care how the picture arrived.

## 2026-08-15

**Fixed**
- Unrecognised "LNB supply X/Y" values from the Picotuner are now logged once each, rather than silently ignored. The displayed state is still left alone - an unknown value must not be mistaken for "off" - but the firmware already distinguishes real hardware states in that field, so a future overload or fault indication would most naturally appear there too. Without this it would be invisible, with Lynx quietly continuing to show the last value it understood.

- Plug B's LNB controls looked broken when they were simply absent. The PicoTuner has only one LNB voltage generator fitted as standard, and its firmware reports the second plug as "absent" - but Lynx mapped that to "off", so the buttons appeared live, turned green on a click and reverted on the next status poll, with no way to tell that nothing was ever going to happen. Confirmed by sending `vgy` to every documented command port (9920, 9921 and 9922): the state never changes, because there is no hardware there to switch. "absent" is now its own state - the buttons are disabled and explain why on hover, and the tuning watchdog skips that plug rather than trying to correct something that cannot be set. A second generator can be fitted by hand; see the BATC PicoTuner wiki.

- The Picotuner watchdog restored LNB supply from the wrong source, making it useless in exactly the case it existed for. The broadcast carries "LNB supply X/Y" and Lynx updates its own globals from it so the UI buttons show the truth - which means that after a power cycle those globals already read "off", and a restore built on them faithfully re-applied "off". Both the check and the restore now use the configured value from `lnb_psu`, which is the only thing that still knows what the supply is meant to be.
- The watchdog now also detects LNB supply loss rather than only restoring it blindly, and checks it in BOTH directions. A Picotuner powers up with its supply ON - 18V observed on plug A on real hardware, every time - so a plug configured "off" coming back live is the case that matters most, not one to skip: failing to apply a supply costs a picture, applying one that should not be there can reach a preamp or antenna that is not expecting it. "off" is now commanded explicitly rather than treated as "nothing to do".
- Same fix at startup, which had the identical flaw: the LNB re-apply skipped any plug configured "off", so a Pi rebooting while its Picotuner sat at 18V left the voltage on. Both the command and the settling pause now key off the configured value rather than the reported one, which by that point may already have been overwritten by the Picotuner reporting whatever it powered up with.
- Symbol rate is now checked alongside frequency, with 2 kS of slack. Checked only once the frequency is right, so a receiver on the wrong channel reports one fault rather than two.

## 2026-08-13

**Added**
- **Pathfinder** — the end-of-contact station map: after a station stops transmitting, a full-screen card shows where they were, the great-circle path back to this receiver, and the signal figures from the contact just ended - locator, distance, bearing, MER, MODCOD and symbol rate. Replaces the idle logo screen rather than overlaying live video, so it can never obscure a picture, and a station keying up cancels it immediately. Enabled/delay/duration are on the Config page; span and distance limits stay in `config.yaml`. Default delay is 2s, set from on-air testing - long enough to read as a pause after the contact, short enough that nobody has drifted away before the card appears.
- Map data (`geo/`) pre-clipped to a 1200km radius and bundled with the receiver - nothing is fetched at runtime, so this works at a site with no outbound connectivity beyond the QRZ lookup. Natural Earth (public domain) for coastlines, borders, rivers, lakes and urban areas; GeoNames (CC BY) for towns, which carries far more places than Natural Earth's own populated-places layer (13 across the whole of southern England, against ~360 in the same window).
- The station's position comes from their QRZ.com locator, reusing the existing lookup - `qrz_callsign_details()` now returns the grid alongside the name from the same response, at no extra API cost. No QRZ entry, no locator, or a locator implying an improbable distance all skip the card and log the reason, rather than publishing a confidently wrong map.
- Config page cross-check: if Bitfocus Companion's unlock action would fire before the card finishes, a warning appears in both sections with a one-click button to match the two. The field stays editable - `unlock_settle_secs` is a debounce, not a hold-off, so lengthening it delays every unlock notification and that is the operator's call to make knowingly.
- New dependency: `pyshp` (74KB, pure Python). Rendering is Cairo, reusing the overlay's existing stack - no matplotlib or geopandas.

- Scheduled reboot units (`lynx-scheduled-reboot.timer` / `.service`), installed but NOT enabled. A twice-daily reboot at 04:00 and 16:00 with up to 5 minutes of jitter, as a blunt backstop for states nobody has specifically thought of yet - which is what matters at an unattended site where the alternative is a visit. The jitter stops several receivers at one site all dropping simultaneously. Enable with `sudo systemctl enable --now lynx-scheduled-reboot.timer`. A fixed clock time rather than a rolling 12 hours from boot, so the reboot happens at a known quiet hour rather than drifting into the middle of a contest. Note this was referred to in several code comments but had never actually been implemented anywhere in the repo.

**Fixed**
- The Picotuner losing its tuning left the receiver permanently deaf. Its WinterHill firmware keeps neither tuning nor LNB supply across a power cycle, so anything that restarts it - a PoE renegotiation, a supply blip, static - brought it back with nothing tuned. It would resume broadcasting, Lynx would report a perfectly healthy tuner, and the receiver would never lock again. At an unattended repeater that is silent, total, and indistinguishable from a quiet band. Lynx now continuously compares what the Picotuner reports it is tuned to against what it should be, and re-applies the LNB supply and re-tunes on any sustained disagreement - tri_watch sources, diversity, or the last single-tune state as appropriate. Deliberately a state comparison rather than a timing check: a Pico reboots in seconds, so watching for a gap in its broadcasts is both easy to miss and blind to every other cause. Safe to run continuously because a tuned receiver keeps reporting its frequency even with no signal on it ("437.000B lost" is a correctly tuned receiver hearing nothing), so a quiet band is left completely alone.

- Save buttons on the Config page now all use the same style; PPM Meter Style was the only one still using the old outline style.

**Docs**
- Web UI manual v1.7: new §3.10 covering the station map, its settings, the diversity/Tri-Watch behaviour, the test endpoint, and the repeater timing warning (the card is on screen until `delay_secs + duration_secs` after unlock - controller hang timers and source switching need to allow for that).
- Install guide Appendix A: moving a working installation from microSD to NVMe, including firmware, boot order and troubleshooting.
- `geo/README.md` documents the data sources, licences and how to regenerate.

## 2026-08-08

**Fixed**
- A literal `dbm=0` from the Picotuner was being shown as a real reading (both Web UI and OSD) - confirmed as a genuine Picotuner firmware quirk at higher Tx gain, specific to tuner B. Now treated the same as "no reading."
- Startup network-wait check now pings the local default gateway instead of a fixed internet host - fixes a misleading "no network detected" when outbound ICMP is blocked but the local network (and internet access generally) is fine; also correctly supports genuinely offline/RF-only setups. Extended 20s → 45s after confirming the route can genuinely take longer than that to appear on some hardware. `PATH` is now also set explicitly at script start, as a defensive measure.

**Added**
- Consistent button-style navigation on all three pages, ported from main (previously only the main page had it).
- LNB PSU control, ported from main - independent H/V (18V/13V) buttons per plug, plus a Tone toggle for Hi-Band LNBs (defaults off, since Amateur TV never needs it). Sent as a standalone command, re-applied on every startup with a settling delay for the voltage to stabilise before the first tune attempt. The indicator reflects the Picotuner's own reported state, not just "a command was sent."
- A general diversity stuck-lock watchdog, ported from main - catches the known "correctly tuned, good margin, but never locking" state whenever it happens, not just at startup. Deliberately keyed on margin rather than MER, so a genuinely empty tuner (e.g. the opposite polarisation) is correctly left alone rather than endlessly re-tuned.

**Changed**
- "Manual Tune" label clarified to "Manual Tune (kHz)".
- Configured site name removed from the header.

**Docs**
- Web UI manual updated (v1.6) - new navigation, LNB PSU, and Shutdown/Stop finally documented (a pre-existing gap - it was never written up even when originally added), plus a note that only the v3 Picotuner board is supported.
- Installation guide brought back in line with main's (v1.8) - had drifted out of date, including a genuine factual error (diversity described as needing a second, separate Picotuner, when it's actually one unit's second built-in tuner circuit). Also adds the SSH-terminal-over-VNC recommendation, after a real install was broken by a locale-related encoding issue introduced that way.

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
