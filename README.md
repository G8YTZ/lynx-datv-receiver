# Lynx DATV Receiver

A DATV receiver for the Picotuner (WinterHill firmware), on a Raspberry Pi 5 or a Radxa ROCK 5T or 5C, with a transparent on-screen display, a full web control portal, and optional diversity reception (the Picotuner's two receivers combined for better resilience against fades).

**Status: Beta.** Actively developed and in use at several sites. Feedback and bug reports welcome.

## Features

- Full-screen live DATV picture with an on-screen overlay (callsign, MER, frequency, modcod, split-eye signal display for diversity mode)
- Hardware decoding, 4K H.265 included: on the Pi 5 through its HEVC decoder, on the ROCK 5T and 5C through Rockchip's (rkmpp)
- More than one programme in a carrier (Lynx-Tx's 4K stills carousel beside the live picture): every programme offered on the web page, and remembered for each tune like a television's channel scan
- Web Control Portal for tuning, memory presets, live BATC stream browsing, and volume control
- **SRT output**: the finished screen (picture, overlay, chat and sound) sent over SRT, to VLC or a transmitter, with nothing plugged into HDMI. Up to 2160p50 H.265 on a ROCK, 1080p H.264 on a Pi 5. See [`docs/SRT_OUTPUT.md`](./docs/SRT_OUTPUT.md)
- DVB-T/T2/C reception via a SiliconDust HDHomeRun network tuner, found automatically on the local network, including narrowband DVB-T2 at 1 and 2 MHz on 2m and 70cm as well as full-bandwidth broadcast multiplexes
- Optional two-tuner diversity combining, with automatic source switching based on signal quality
- Repeater/receiver-activity notifications (QRZ Logbook, Slack, Bitfocus Companion, GPIO Tx control)
- **Pathfinder**: an end-of-contact station map, a full-screen card showing where a station was, the path back to the receiver, and the signal figures from the contact, drawn from their QRZ locator
- Recovers by itself: when a signal goes, the screen shows the receiver's status, and the picture comes back when the signal does. Resumes its previous state after any restart, including a power cut
- Optional M5Dial ("Knobler") front-panel or remote control

## Getting started

See [`lynx_install_guide.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_install_guide.docx) for the full, step-by-step setup guide, from a blank SD card to a working receiver, or use the one-line installer described in that guide's Section 4.1. The same installer works on every board: it finds out whether it is on a Raspberry Pi or a ROCK 5T or 5C, and on a ROCK also builds the hardware video stack (about 20 minutes the first time).

Start Lynx, then set your Picotuner's IP address and your site details from the Web Control Portal's Configuration page (⚙️ Config → Picotuner Network Settings). There is no need to hand-edit the config file for this.

## Documentation

- [`lynx_install_guide.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_install_guide.docx): full setup guide, blank SD card to working receiver
- [`lynx_webui_manual.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_webui_manual.docx): what every button and field on the Web Control Portal does
- [`lynx_overlay_annotated.pdf`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_overlay_annotated.pdf): annotated guide to the on-screen display overlay
- [`lynx_diversity_overview.docx`](https://github.com/G8YTZ/lynx-datv-receiver/raw/main/docs/lynx_diversity_overview.docx): how diversity reception works, and what's next
- [`docs/SRT_OUTPUT.md`](./docs/SRT_OUTPUT.md): the SRT output, the Screen output card, and watching it in VLC
- [`CHANGELOG.md`](./CHANGELOG.md): what's changed recently, in one place
- [`docs/ROADMAP.md`](./docs/ROADMAP.md): what's being worked on next

## Requirements

- One of:
  - **Raspberry Pi 5** (4 GB or more), running Raspberry Pi OS with Desktop. A Pi 4 also runs Lynx, without 4K.
  - **Radxa ROCK 5T or ROCK 5C**, running Armbian Debian 13 (trixie), **Minimal, vendor kernel**. The vendor kernel is the one with Rockchip's video decoder; Armbian's "current" kernel has none. The ROCK drives 4K with most of the machine idle; the 5T has two HDMI outputs, the 5C one.
- A Picotuner (WinterHill firmware) on the same network
- Optionally, a SiliconDust HDHomeRun for DVB-T/T2/C. The narrow amateur bandwidths are available only on the **HDHR5-2DT** (two tuners) and **HDHR5-4DT** (four tuners), **both now discontinued**, so second-hand only. No other model has them, and SiliconDust know of no other demodulator supporting anything narrower than 1.7 MHz as standard. They have said they would like to make a new model for this. Tuning range 44 to 866 MHz, so 2m and 70cm are usable and 23cm is out of reach
- See the install guide for the full system/Python dependency list (`pyshp` is required for the station map)
- Recommended: disable Wi-Fi and Bluetooth and use wired Ethernet

## Contributing

This project is under active development, with new features and fixes being added. Issues and pull requests are welcome, particularly bug reports from receivers in the field.
