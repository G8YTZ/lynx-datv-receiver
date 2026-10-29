#!/usr/bin/env python3
"""
patch_netout_relock.py

Network output from a repeater that only transmits on activity: the
picture reaches VLC as well as the sound.

FOUND (Rob VK3IE, VK3RTV through an HDHomeRun, 28 Oct 2026)
-----------------------------------------------------------
VLC played VK3RTV's sound but never its picture, and the forwarder's log
was nothing but "non-existing PPS 0 referenced / decode_slice_header
error / no frame!". Those are exactly the lines ffmpeg prints when an
H.264 stream reaches it without its SPS/PPS headers (reproduced here).
Broadcast multiplexes repeat them before every keyframe, which is why
commercial channels were fine; VK3RTV's encoder appears to send them
rarely, perhaps only at the start of each over. So:

  - the forwarder was started once, at the tune, and carried on across
    every over; it had to catch the headers at the start of an over,
    and anything that missed them got no picture for the rest of it;
  - it was also told to discard damaged packets (+discardcorrupt), and
    the first packets of an over, as the signal arrives, are the ones
    most likely to be damaged - taking the headers with them.

Lynx's own player never had the problem: it is restarted on every
HDHomeRun re-lock, at the start of the over.

THE FIX
-------
  1. The forwarder is restarted on every HDHomeRun re-lock too, so each
     over starts with a fresh forwarder that sees its headers. Once it
     has seen them, ffmpeg's transport stream output repeats them before
     every keyframe by itself (checked: a stream whose headers were
     given once came out with them before all four keyframes), so VLC
     can join at any point in the over.
  2. Damaged packets are passed through rather than thrown away, as on
     any set-top box: the decoder at the far end conceals the damage,
     and a damaged opening packet no longer takes the headers with it.

The proper cure is still at the transmitter (repeat SPS/PPS at every
keyframe); this makes Lynx cope with one that does not.

Run from the repo root:  python3 patch_netout_relock.py
Then:                    python3 verify.py lynx_app.py
"""

import sys
from pathlib import Path


def apply(src, old, new, label):
    n = src.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected 1")
    print(f"  ok  {label}")
    return src.replace(old, new, 1)


def main():
    p = Path("lynx_app.py")
    if not p.exists():
        sys.exit("ABORT: lynx_app.py not found - run this from the repo root")
    src = p.read_text()
    if "forwarder restarted with the over" in src:
        sys.exit("ABORT: already patched")

    src = apply(src,
                '                "-fflags", "+genpts+discardcorrupt",\n',
                '                # Damaged packets passed through, not discarded: the\n'
                '                # far end conceals them, and a damaged first packet of\n'
                '                # an over must not take the H.264 headers with it\n'
                '                # (patch_netout_relock.py).\n'
                '                "-fflags", "+genpts",\n',
                "forwarder: pass damaged packets through")

    src = apply(src,
                '                            restart_mpv(f"udp://@:{HDHR_VIDEO_PORT}",\n'
                '                                        is_rf=False)\n'
                '                            end_transition_cover()\n',
                '                            restart_mpv(f"udp://@:{HDHR_VIDEO_PORT}",\n'
                '                                        is_rf=False)\n'
                '                            # Network output: the forwarder restarted with the over\n'
                '                            # too, so it sees the H.264 headers at its start - see\n'
                '                            # patch_netout_relock.py. restart_mpv() starts no\n'
                '                            # player while network output is on.\n'
                '                            if network_output_enabled:\n'
                '                                print("[netout] re-lock - restarting the forwarder",\n'
                '                                      flush=True)\n'
                '                                start_network_output(f"udp://@:{HDHR_VIDEO_PORT}")\n'
                '                            end_transition_cover()\n',
                "re-lock: restart the forwarder")

    p.write_text(src)


if __name__ == "__main__":
    main()
