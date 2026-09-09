#!/usr/bin/env python3
"""
patch_video_sync_desync.py

Stops video presentation waiting on the audio clock.

THE FAULT
---------
A transmitter can declare an audio PID in the PMT and never send a
packet on it — a camera with the sound switched off does exactly this.
mpv selects the track, has no audio clock to sync against, and video
crawls: frames arrive, but slowly and unevenly. A slideshow rather than
a freeze.

Confirmed live on both sources, independently. Sending "aid no" over
IPC turned a slideshow into smooth video on the local Picotuner, and
then again on a Slave, each time with an AAC track present and
audio-bitrate reporting unavailable.

Note this is NOT the same as a quiet passage in a programme. A track
carrying frames of digital silence still gives mpv a clock and behaves
perfectly. The problem is a declared track that never carries anything
at all.

WHY DESYNC RATHER THAN DISABLING AUDIO
--------------------------------------
--audio=no would remove the dependency entirely, but a transmitter can
start sending audio partway through a transmission, and a disabled
track would never pick it up without restarting mpv. --video-sync=desync
lets video run on its own clock while leaving audio selected, so sound
appears if and when it arrives.

Applied to both branches, not just RF. The fault was seen on a Slave
(which plays through the stream path) as well as on the local tuner, so
fixing one branch would leave the other broken.

THE TRADE
---------
Video no longer stays locked to the audio clock, so a genuinely
drifting source could develop lip-sync error over a long session. For a
repeater that is the better side of the trade against unwatchable
video, and the drift correction script already watches for divergence
of exactly that kind.

Run from the repo root:  python3 patch_video_sync_desync.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")

OLD = """        f"--audio-pitch-correction=no --script={DRIFT_SCRIPT_PATH} \""""

NEW = """        f"--audio-pitch-correction=no --script={DRIFT_SCRIPT_PATH} "
        # Video presents on its own clock and never waits for audio. A
        # transmitter that declares an audio PID and sends nothing on it
        # otherwise leaves mpv syncing to a clock that never ticks, and
        # video crawls — confirmed live on both a local tuner and a
        # Slave, and cured on both by dropping the audio track.
        #
        # The track stays selected rather than disabled, so audio that
        # starts partway through a transmission is still picked up.
        f"--video-sync=desync \""""


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")

    src = TARGET.read_text()

    if "video-sync=desync" in src:
        sys.exit("ABORT: already patched")

    n = src.count(OLD)
    if n != 1:
        sys.exit(f"ABORT: anchor found {n} times, expected exactly 1")

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_desync"))
    TARGET.write_text(src.replace(OLD, NEW, 1))

    print("  ok  --video-sync=desync added to every mpv launch")
    print("\nNow: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
