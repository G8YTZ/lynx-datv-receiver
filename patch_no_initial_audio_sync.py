#!/usr/bin/env python3
"""
patch_no_initial_audio_sync.py

Stops mpv holding video back at start to align it with audio.

WHY THIS ONE
------------
--initial-audio-sync defaults to yes: at the start of playback mpv
aligns video to the audio timeline before settling. On a transport
stream whose PMT declares an audio PID that carries nothing — an
encoder with its sound switched off — there is no audio timeline to
align to, and video is held back waiting for one. Measured at roughly
twenty seconds of stale, smeared frames before video ran properly,
against an immediate picture when the transmitter had sound.

It also fits an observation from the bench: with audio present, audio
always arrives first. That is the alignment working as designed. With
audio declared and absent, the same mechanism has nothing to work with.

WHAT THIS REPLACES
------------------
An earlier attempt polled mpv over IPC a few seconds after each launch
and sent "aid no" when no audio bitrate had appeared. It helped a
silent stream, but made streams WITH audio worse — mpv's own IPC
helper carries a comment about commands arriving mid-load disrupting
its startup, and that is exactly the window the polling landed in.

This needs no polling, no IPC, and no state. It changes one default at
launch and applies equally to every source.

WHAT IT DOES NOT DO
-------------------
Audio still plays when present; only the initial alignment is skipped.
Combined with --video-sync=desync, already applied, video presents on
its own clock throughout rather than waiting on audio at any stage.

Run from the repo root:  python3 patch_no_initial_audio_sync.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")

OLD = '        f"--video-sync=desync "'

NEW = ('        f"--video-sync=desync "\n'
       '        # mpv aligns video to audio at the start of playback by\n'
       '        # default. A stream declaring an audio PID that never\n'
       '        # carries anything gives it nothing to align to, and video\n'
       '        # is held back waiting — twenty seconds of stale frames on\n'
       '        # a transmitter with its sound switched off, against an\n'
       '        # instant picture when the sound was on.\n'
       '        #\n'
       '        # Audio still plays when it is there. Only the alignment at\n'
       '        # start is skipped, which is the part that cannot work when\n'
       '        # one side of it is missing.\n'
       '        f"--no-initial-audio-sync "')


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")

    src = TARGET.read_text()

    if "no-initial-audio-sync" in src:
        sys.exit("ABORT: already patched")
    if "mpv_dead_audio_monitor" in src:
        sys.exit("ABORT: the dead-audio monitor is still present — revert that "
                 "commit first, so this is tested on its own")

    n = src.count(OLD)
    if n != 1:
        sys.exit(f"ABORT: anchor found {n} times, expected exactly 1")

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_ias"))
    TARGET.write_text(src.replace(OLD, NEW, 1))

    print("  ok  --no-initial-audio-sync added to every mpv launch")
    print("\n  Now: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
