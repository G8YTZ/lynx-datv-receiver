# Lynx DATV Receiver

A Raspberry Pi 5 based DATV receiver for the Picotuner (WinterHill firmware), with a transparent on-screen display, a full web control portal, and optional diversity reception (the two picotuner receivers combined for improved resilience against fades).

**Status: Beta.** Actively developed and in use at several sites. Feedback and bug reports welcome.

## Features

- Full-screen live DATV picture with an on-screen overlay (callsign, MER, frequency, modcod, split-eye signal display for diversity mode)
- Web Control Portal for tuning, memory presets, live BATC stream browsing, and volume control
- DVB-T/T2/C reception via a SiliconDust HDHomeRun network tuner, found automatically on the local network — including narrowband DVB-T2 at 1 and 2 MHz on 2m and 70cm, as well as full-bandwidth broadcast multiplexes
- Optional two-tuner diversity combining, with automatic source switching based on signal quality
- Repeater/receiver-activity notifications (QRZ Logbook, Slack, Bitfocus Companion, GPIO Tx control)
- **Pathfinder** — an end-of-contact station map: a full-screen card showing where a station was, the path back to the receiver, and the signal figures from the contact, drawn from their QRZ locator
- Resumes its previous state automatically after any restart, including a genuine power loss
- Optional M5Dial ("Knobler") front-panel or remote control

## Getting started

See [`lynx_install_guide.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_install_guide.docx) for the full, step-by-step setup guide, from a blank SD card to a working receiver — or use the one-line installer described in that guide's Section 4.1.

Copy [`config/lynx_config.example.yaml`](./config/lynx_config.example.yaml) to `config/lynx_config.yaml`, start Lynx, then set your Picotuner's IP address from the Web Control Portal's Configuration page (⚙️ Config → Picotuner Network Settings) — no need to hand-edit the config file for this.

## Documentation

- [`lynx_install_guide.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_install_guide.docx) — full setup guide, blank SD card to working receiver
- [`lynx_webui_manual.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_webui_manual.docx) — what every button and field on the Web Control Portal actually does
- [`lynx_overlay_annotated.pdf`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_overlay_annotated.pdf) — annotated guide to the on-screen display overlay
- [`lynx_diversity_overview.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_diversity_overview.docx) — how diversity reception actually works, and what's next
- [`CHANGELOG.md`](./CHANGELOG.md) — what's changed recently, in one place

## Requirements

- Raspberry Pi 5 (4GB or more), running Raspberry Pi OS with Desktop
- A Picotuner (WinterHill firmware) on the same network
- Optionally, a SiliconDust HDHomeRun for DVB-T/T2/C. The narrow amateur bandwidths are available only on the **HDHR5-2DT** (two tuners) and **HDHR5-4DT** (four tuners), **both now discontinued** — second-hand only. No other model has them, and SiliconDust know of no other demodulator supporting anything narrower than 1.7 MHz as standard. They have said they would like to make a new model for this. Tuning range 44–866 MHz, so 2m and 70cm are usable and 23cm is out of reach
- See the install guide for the full system/Python dependency list (`pyshp` is required for the station map)
- Recommended to disable Wi-Fi and Bluetooth and use just wired Ethernet

## Contributing

This project is under active development, with new features and fixes being added — issues and pull requests are welcome, particularly bug reports from receivers in the field.
