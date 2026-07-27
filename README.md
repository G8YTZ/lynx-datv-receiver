# Lynx DATV Receiver

A Raspberry Pi 5 based DATV receiver for the Picotuner (WinterHill firmware), with a transparent on-screen display, a full web control portal, and optional diversity reception (two Picotuners combined for improved resilience against interference).

**Status: Alpha.** Actively developed and in trial use at several BATC repeater sites. Expect rough edges — feedback and bug reports welcome.

## Features

- Full-screen live DATV picture with an on-screen overlay (callsign, MER, frequency, modcod, split-eye signal display for diversity mode)
- Web Control Portal for tuning, memory presets, live BATC stream browsing, and volume control
- Optional two-receiver diversity combining, with automatic source switching based on signal quality
- Repeater-activity notifications (QRZ Logbook, Slack, Bitfocus Companion, GPIO Tx control)
- Resumes its previous state automatically after any restart, including a genuine power loss
- Optional M5Dial ("Knobler") front-panel control

## Getting started

See [`lynx_install_guide.docx`](./lynx_install_guide.docx) for the full, step-by-step setup guide, from a blank SD card to a working receiver — or use the one-line installer described in that guide's Section 4.1.

Copy [`lynx_config.example.yaml`](./lynx_config.example.yaml) to `config/lynx_config.yaml` and set your own Picotuner IP and site details before first run.

## Documentation

- [`lynx_install_guide.docx`](./lynx_install_guide.docx) — full setup guide, blank SD card to working receiver
- [`lynx_webui_manual.docx`](./lynx_webui_manual.docx) — what every button and field on the Web Control Portal actually does
- [`lynx_overlay_annotated.pdf`](./lynx_overlay_annotated.pdf) — annotated guide to the on-screen display overlay

## Requirements

- Raspberry Pi 5 (4GB or more), running Raspberry Pi OS with Desktop
- A Picotuner (WinterHill firmware) on the same network
- See the install guide for the full system/Python dependency list

## Contributing

This project is under active development — issues and pull requests are welcome, particularly bug reports from alpha trial sites.
