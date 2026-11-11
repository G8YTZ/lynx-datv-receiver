# Lynx road map

Agreed with Justin G8YTZ, 2 November 2026. In order; each step is tried on Justin's own hardware before it goes to the beta testers.

## 1. Lynx Rx on the Radxa ROCK 5C

- The receiver recognises only the ROCK 5T by name today (`IS_ROCK = BOARD == "rock5t"` in `lynx_platform.py`). Widen that to the RK3588 family, so the 5C takes the ROCK video path (`--hwdec=rkmpp`, `gpu-next`, Wayland).
- Test what differs on the 5C: a single HDMI output (no second receiver output), device names, and 4K H.265.
- One install script for every board: it works out Pi 4, Pi 5, ROCK 5T or ROCK 5C by itself.
- GNSS: Justin has a spare GNSS hat for the 5C, for location and time as on the other receivers.
- Folded into `beta-wip`, then `beta`.
- **Where it is (10 Nov 2026):** The 5C (Armbian 26.11.1 trixie, vendor 6.1.115, 4 GB) installs with the one `install.sh`, which now runs `install/rock5t.sh` itself on a ROCK 5T or 5C. Recognised as "Radxa ROCK 5C", ROCK video path, one HDMI. 4K HEVC on air plays in hardware (rkmpp, mpv about 17% of one core). Fan control works. The same evening, on Lynx-Tx's two-programme multiplex: the diversity combiner cut on one PCR PID; a bare frequency line counts as a Picotuner lock; mpv starts on the first lock reading (lock to picture about 6 s); a chosen programme survives mpv restarts; the drift and freeze checks leave a stills programme alone; `overrun_nonfatal=1` on mpv's UDP source (FFmpeg's ring had overrun and stopped reading for good). Still open: why mpv falls behind on the carousel at all; mpv only lists programme 2 if it shows up in its first second; the Picotuner's "header" every 35 to 40 s; the GNSS hat (step 5).

## Tried and dropped: fast programme change relay (11 Nov 2026)

A relay (lynx_fcc.py) holding each programme from its last keyframe and bursting it to a newly started mpv. Ch1 came 1.6 s instead of 2.4 to 4, but Ch2 (Lynx-Tx's carousel) stayed at 4 to 5 s with one change in three failing. Justin dropped it (01:02): the real fix is similar keyframe intervals on both programmes at the Tx. Reverted; the code is in the history (861cb64 to adb1b70) if wanted again.

## 2. Beta merged into main

Once the 5C and the current beta (Pi 5 zero-copy H.265, the mpv restart fix, the 90 s start-up wait) have had more testing.

## 3. Lynx-Tx takes SRT streams

From a Pi 4-based receiver at the repeater site, so Lynx-Tx becomes a repeater: receive, SRT, transmit, all managed from one page.

## 4. Lynx Rx inside Lynx-Tx

- First as an off-air quality monitor: is it good on air?
- Later, pre-correction. Suggested route: sample the PA output through a coupler into the SDR's own receive side (PlutoSky and LibreSDR both have one), so Lynx-Tx compares the real transmitted constellation with what it meant to send, on the same box and clock. A DVB receiver's MER says that something is wrong but not what, so it suits the monitor rather than the correction.

## Also open

- Lynx-Tx chat on air: merge `chat-on-air` into main once Justin is happy with it.
- Lynx for Enigma2: pin the chat bubbles to the bottom, as on Lynx-Tx.
- Lynx for Apple TV: under way in its own repository. Android TV later.
