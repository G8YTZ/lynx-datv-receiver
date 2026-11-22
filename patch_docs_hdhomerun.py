#!/usr/bin/env python3
"""
patch_docs_hdhomerun.py — CHANGELOG entry and README updates.

Adds the 2026-09-25 entry to CHANGELOG.md in the house style, and
brings README.md up to date: Alpha becomes Beta, and the HDHomeRun
appears in Features and Requirements with the model restriction stated
plainly, since buying the wrong one is the single most likely way for
somebody to be disappointed by this.

Usage
-----
    python3 patch_docs_hdhomerun.py            # dry run
    python3 patch_docs_hdhomerun.py --apply    # back up and write
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

CHANGELOG = Path("CHANGELOG.md")
README = Path("README.md")
MARKER = "## 2026-09-25"


CL_ANCHOR = ("All notable changes to Lynx are documented here, in reverse "
             "chronological order. Kept as short, scannable headlines - see "
             "the git history for full detail on any entry.\n")

CL_ENTRY = """
## 2026-09-25

**Added**
- DVB-T/T2/C reception, via a SiliconDust HDHomeRun on the network. It appears as a fourth source alongside the two Picotuner receivers and the Slave Rx, with its own OSD panel, Web UI card, presets, QRZ logging and Pathfinder card. The device is found automatically at startup, so a tuner on the same network needs nothing configured at all. It is not a dongle or a HAT: it sits on the LAN and streams a transport stream to whatever address it is told, so the receiver need not be in the same room as the antenna, and one box carries two or four tuners.
- Narrowband DVB-T2 on the amateur bands. The HDHR5-2DT and 4DT demodulate channel bandwidths from 1 to 8 MHz and tune to arbitrary frequencies rather than a broadcast channel plan. Confirmed on air at 437.000 MHz and at 146.500 MHz, both 1 MHz DVB-T2 from a modified Portsdown 4, with SNQ and SEQ at 100. 2m is the interesting one: amateur television has been effectively a UHF and microwave activity because of the bandwidth involved, and 1 MHz changes that arithmetic. 4m and 6m are too narrow to fit a 1 MHz signal, so 2m is as low as this is useful.
- A service dropdown, filled from the multiplex itself. The program number was a plain box, so any number could be typed and the device would accept one that did not exist - a lock with no picture and nothing to say why. Services are now listed by name, and switching between them does not retune: the tuner is already locked on the multiplex, so it is a demultiplexer change and takes effect immediately.
- QRZ logging for DVB-T2 contacts, with the callsign taken from the service name carried in the multiplex - which on an amateur transmission is exactly what it is. Gated on the tuned frequency being in an amateur band, not on the shape of the name: what band a tuner is on is a fact, whereas what a name looks like is a guess, and "BBC" would otherwise have been logged as a callsign. The quality figures go in the comment field, where a percentage cannot be mistaken for a dB value.
- Pathfinder cards for DVB-T2 and for a Slave. `_pathfinder_current_source()` knew about tri_watch, diversity and the local Picotuner and nothing else, so in either mode it reported the Picotuner's lock - false on a receiver without one - and no card was ever drawn.
- A manual address field on the Config page, for a tuner on another subnet. Discovery is a UDP broadcast and stops at the first router; everything after it is ordinary unicast, so a device at a repeater site across a link works perfectly once named but can never be found on its own.

**Changed**
- The QRZ ADIF mode string is no longer hardcoded to DVB-S2. That assumption was sound while the Picotuner was the only source and was flagged as such in its own comment; a source that knows what it is now says so.
- The DVB-T/DVB-T2 selector was removed from the Web UI. The tuner detects the standard itself, so asking the operator to name it was asking them to know something the device already knew. It is not replaced by the device's own "auto" modulation, which is unreliable - it repeatedly failed to lock a mux that locked within seconds when the modulation was given explicitly. Lynx tries DVB-T2 and falls back to DVB-T.
- The DVB-T2 card takes kHz, like the Picotuner card. Two cards on one page asking for the same thing in different units is the sort of thing that catches somebody once and annoys them for ever.

**Fixed**
- The stream target was set to `udp://127.0.0.1:<port>`, which the HDHomeRun accepted and reported back perfectly while sending the stream to its own loopback - it is a separate box on the network, so its idea of localhost is itself. Nothing arrived and nothing said why: the status showed locked and streaming, pps was non-zero, and tcpdump on every interface at this end saw nothing at all. The address is now asked of the kernel on every tune rather than configured, since a remembered one would break on a DHCP change or a move between interfaces, and the failure would look identical.
- The service-name fetch deadlocked the whole application. It ran inline in the tune route and took `hdhr_lock` while calling a function that takes the same lock - and `threading.Lock` is not reentrant, so the thread blocked waiting for a lock it was already holding. Everything behind that lock stopped, including `/api/status`.
- mpv was given the RF flag set, which forces `--demuxer-lavf-format=mpegts` and skips lavf's own probe - and that probe is what finds the AAC LATM audio in a broadcast multiplex. mpv listed video and subtitles only and played in silence, while ffprobe found the audio track immediately. The Picotuner's own transport stream carries simpler audio and does not need the probe, which is why the flag was right there and wrong here.
- mpv held a stale frame when the content changed underneath it: a station stopped, the source was changed at the far end, and the old test card was still on screen when it came back. The poller now restarts mpv when lock returns, so a station keying up after a gap gets a picture without anyone touching anything - the difference between a bench demonstration and something usable at a repeater.
- The OSD covered live DVB-T2 video with the logo screen, and took the sound with it. `genuinely_locked` was computed from the Picotuner's lock state, correctly false on a receiver that has no Picotuner, so the cover went up over a picture mpv was decoding perfectly underneath. The magic eye and PPM were suppressed by the same test.
- A restart left a DVB-T2 source idle. The tune route saved the mode correctly but the startup resume had no branch for it, so a receiver watching a DVB-T2 station came back to nothing after a reboot, watchdog restart or power cut.
- The service list was read once, immediately after the tune. The device reports lock before its service tables can be read, so the first attempt came back empty and the callsign and dropdown only filled after a retune. Both now retry rather than waiting a fixed time, which is also quicker where the device is ready at once.

**Known issues**
- The narrow bandwidths are available only on the HDHR5-2DT and HDHR5-4DT. SiliconDust have confirmed this directly and have said they are considering a model aimed at amateur use, so it may broaden.
- `t1dvbt2` is a literal 1 MHz, not 1.7 MHz rounded. The ladder is exact, so a 1.7 MHz transmission has no matching setting and will never lock, however healthy the signal strength looks.
- The multiplex name is not shown. The device does not parse the NIT, and streaminfo gives only the tsid and onid. Deriving a name from the tsid would need a per-country table, and would be confidently wrong outside the country it was written for.
"""


README_EDITS = [
    ("**Status: Alpha.** Actively developed and in trial use. Expect rough "
     "edges — feedback and bug reports welcome.",
     "**Status: Beta.** Actively developed and in use at several sites. "
     "Feedback and bug reports welcome."),

    ("- Web Control Portal for tuning, memory presets, live BATC stream "
     "browsing, and volume control",
     "- Web Control Portal for tuning, memory presets, live BATC stream "
     "browsing, and volume control\n"
     "- DVB-T/T2/C reception via a SiliconDust HDHomeRun network tuner, found "
     "automatically on the local network — including narrowband DVB-T2 at 1 "
     "and 2 MHz on 2m and 70cm, as well as full-bandwidth broadcast "
     "multiplexes"),

    ("- A Picotuner (WinterHill firmware) on the same network",
     "- A Picotuner (WinterHill firmware) on the same network\n"
     "- Optionally, a SiliconDust HDHomeRun for DVB-T/T2/C. The narrow "
     "amateur bandwidths are available only on the **HDHR5-2DT** (two tuners) "
     "and **HDHR5-4DT** (four tuners) — other models do not have them in the "
     "demodulator"),

    ("particularly bug reports from alpha trial sites.",
     "particularly bug reports from receivers in the field."),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    args = parser.parse_args()

    for path in (CHANGELOG, README):
        if not path.exists():
            print(f"ERROR: {path} not found. Run from the lynx directory.")
            return 2

    cl = CHANGELOG.read_text(encoding="utf-8")
    rm = README.read_text(encoding="utf-8")

    if MARKER in cl:
        print(f"{CHANGELOG} already has the {MARKER} entry - nothing to do.")
        return 0

    print("\nchecking anchors\n")
    ok = True

    if cl.count(CL_ANCHOR) == 1:
        print("  CHANGELOG header     OK   one match")
    else:
        print(f"  CHANGELOG header     FAIL {cl.count(CL_ANCHOR)} matches")
        ok = False

    for i, (old, _new) in enumerate(README_EDITS, 1):
        if rm.count(old) == 1:
            print(f"  README edit {i}        OK   one match")
        else:
            print(f"  README edit {i}        FAIL {rm.count(old)} matches "
                  f"- {old[:45]}...")
            ok = False

    if not ok:
        print("\nNo changes made.")
        return 1

    cl_new = cl.replace(CL_ANCHOR, CL_ANCHOR + CL_ENTRY, 1)
    rm_new = rm
    for old, new in README_EDITS:
        rm_new = rm_new.replace(old, new, 1)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for path, text in ((CHANGELOG, cl_new), (README, rm_new)):
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{stamp}"))
        path.write_text(text, encoding="utf-8")
        print(f"\n  {path} updated")

    return 0


if __name__ == "__main__":
    sys.exit(main())
