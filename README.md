# Lynx DATV Receiver

A Raspberry Pi 5 based DATV receiver for the Picotuner (WinterHill firmware), with a transparent on-screen display, a full web control portal, and optional diversity reception (the two picotuner receivers combined for improved resilience against fades).

**Status: Alpha.** Actively developed and in trial use. Expect rough edges — feedback and bug reports welcome.

## Features

- Full-screen live DATV picture with an on-screen overlay (callsign, MER, frequency, modcod, split-eye signal display for diversity mode)
- Web Control Portal for tuning, memory presets, live BATC stream browsing, and volume control
- Optional two-tuner diversity combining, with automatic source switching based on signal quality
- Repeater/receiver-activity notifications (QRZ Logbook, Slack, Bitfocus Companion, GPIO Tx control)
- Resumes its previous state automatically after any restart, including a genuine power loss
- Optional M5Dial ("Knobler") front-panel or remote control

## Getting started

See [`lynx_install_guide.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_install_guide.docx) for the full, step-by-step setup guide, from a blank SD card to a working receiver — or use the one-line installer described in that guide's Section 4.1.

Copy [`lynx_config.example.yaml`](./lynx_config.example.yaml) to `config/lynx_config.yaml`, start Lynx, then set your Picotuner's IP address from the Web Control Portal's Configuration page (⚙️ Config → Picotuner Network Settings) — no need to hand-edit the config file for this.

## Documentation

- [`lynx_install_guide.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_install_guide.docx) — full setup guide, blank SD card to working receiver
- [`lynx_webui_manual.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_webui_manual.docx) — what every button and field on the Web Control Portal actually does
- [`lynx_overlay_annotated.pdf`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_overlay_annotated.pdf) — annotated guide to the on-screen display overlay
- [`lynx_diversity_overview.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_diversity_overview.docx) — how diversity reception actually works, and what's next

## Requirements

- Raspberry Pi 5 (4GB or more), running Raspberry Pi OS with Desktop
- A Picotuner (WinterHill firmware) on the same network
- See the install guide for the full system/Python dependency list
- Recommended to disable Wi-Fi and Bluetooth and use just wired Ethernet

## Contributing

This project is under active development, with new features and fixes being added — issues and pull requests are welcome, particularly bug reports from alpha trial sites.
