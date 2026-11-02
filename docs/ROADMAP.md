# Lynx road map

Agreed with Justin G8YTZ, 2 November 2026. In order; each step is tried on Justin's own hardware before it goes to the beta testers.

## 1. Lynx Rx on the Radxa ROCK 5C

- The receiver recognises only the ROCK 5T by name today (`IS_ROCK = BOARD == "rock5t"` in `lynx_platform.py`). Widen that to the RK3588 family, so the 5C takes the ROCK video path (`--hwdec=rkmpp`, `gpu-next`, Wayland).
- Test what differs on the 5C: a single HDMI output (no second receiver output), device names, and 4K H.265.
- One install script for every board: it works out Pi 4, Pi 5, ROCK 5T or ROCK 5C by itself.
- GNSS: Justin has a spare GNSS hat for the 5C, for location and time as on the other receivers.
- Folded into `beta-wip`, then `beta`.

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
