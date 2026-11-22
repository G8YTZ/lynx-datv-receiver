#!/usr/bin/env python3
"""
patch_analyzeduration.py

Gives ffmpeg time to look at the stream before it starts decoding it.

THE FAULT
---------
--profile=low-latency sets analyzeduration to zero. mpv's own log says
so plainly, while failing:

    mpegts: Could not find codec parameters for stream 1
            (Audio: aac, 0 channels): unspecified sample format
    Consider increasing the value for the 'analyzeduration' (0) and
    'probesize' (5000000) options

With no analysis time, mpv commits to a picture of the stream from
whatever bytes happen to arrive first. Joining a live stream lands
mid-GOP, so it starts decoding slices before it has the parameter set
that describes them:

    h264: non-existing PPS 0 referenced
    h264: decode_slice_header error
    h264: no frame!

Nothing decodes until the next keyframe, which on a long GOP is a long
time to look at a frozen screen. The drift correction then calls it a
hard freeze and restarts mpv, which rejoins mid-GOP and does it again.

THE FIX
-------
One second of analysis. Enough to find a keyframe and identify both
tracks properly at any sane GOP length, and enough that a sparse or
audio-less stream is understood rather than guessed at.

WHAT IT COSTS
-------------
One second, once, at start of playback and on each source switch —
never during playback. restart_mpv already kills and relaunches on
every switch behind a transition cover that waits up to twelve seconds
for rendering, so this sits comfortably inside a delay that already
exists. Nothing is added to running latency.

WHERE IT APPLIES
----------------
The RF branch, which is where low-latency zeroes it. That covers the
local tuners and Slaves (which take is_rf=True since they send genuine
MPEG-TS). RTMP and other streams never had the profile applied and keep
mpv's defaults untouched.

Run from the repo root:  python3 patch_analyzeduration.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")

OLD = """            "--demuxer=lavf --demuxer-lavf-format=mpegts "
            "--demuxer-max-bytes=512KiB --demuxer-max-back-bytes=128KiB "
            "--profile=low-latency --cache-pause=no "
        )"""

NEW = """            "--demuxer=lavf --demuxer-lavf-format=mpegts "
            "--demuxer-max-bytes=512KiB --demuxer-max-back-bytes=128KiB "
            "--profile=low-latency --cache-pause=no "
            # low-latency sets analyzeduration to zero, which means mpv
            # decides what the stream contains from whatever bytes turn
            # up first. Joining a live stream lands mid-GOP, so it
            # starts decoding slices before it has the PPS describing
            # them — "non-existing PPS 0 referenced", then no picture
            # until the next keyframe. It also leaves a sparse audio
            # track unidentified: "0 channels: unspecified sample
            # format".
            #
            # A second is enough to find a keyframe at any sane GOP
            # length and to identify both tracks properly. It is spent
            # once per source switch, behind a transition cover that
            # already waits far longer than that for rendering, and
            # adds nothing to running latency.
            "--demuxer-lavf-analyzeduration=1 "
        )"""


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")

    src = TARGET.read_text()

    if "analyzeduration" in src:
        sys.exit("ABORT: already patched")

    n = src.count(OLD)
    if n != 1:
        sys.exit(f"ABORT: anchor found {n} times, expected exactly 1")

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_analyze"))
    TARGET.write_text(src.replace(OLD, NEW, 1))

    print("  ok  analyzeduration set to 1s on the MPEG-TS path")
    print("\nNow: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
