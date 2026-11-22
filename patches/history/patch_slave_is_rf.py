#!/usr/bin/env python3
"""
patch_slave_is_rf.py

Plays a Slave with the MPEG-TS flags instead of the stream ones.

WHAT WAS WRONG
--------------
A Slave plays through start_stream(), which calls restart_mpv() with
is_rf=False. That was noted as a known limitation when the select path
was written, on the grounds that it was only about buffer sizes. It is
not.

is_rf=False means no --demuxer-lavf-format=mpegts, so mpv probes the
stream instead of being told what it is. On a sparse, audio-less feed
it guesses wrong: observed live with a Slave reporting duration 51.084
seconds for a live stream, sitting paused at position zero, with
"Cannot seek in this stream" repeating. Ryde played the identical
stream perfectly, which is what ruled out the stream itself.

restart_mpv's own docstring already says forcing mpegts is correct for
something genuinely sending MPEG-TS over UDP, and wrong for RTMP. A
Slave is the former. It was only getting the latter because it happens
to travel through the code path built for streams.

DERIVED, NOT STORED
-------------------
The relay's output port is the only thing a Slave is ever played from,
so the URL says whether this is a Slave. No flag to set when selecting
and clear on every other path — the same reasoning as
stream_is_remote, and for the same reason: a stored copy is a copy that
can disagree.

is_rf affects nothing in restart_mpv except which source_flags are
used, so nothing else changes.

Run from the repo root:  python3 patch_slave_is_rf.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")

OLD = "            restart_mpv(req.url, is_rf=False)"
NEW = """            # A Slave sends genuine MPEG-TS over UDP, exactly what the
            # RF flags exist for, and only reaches this path because it
            # borrows start_stream()'s lock and cover handling. Without
            # the mpegts hint mpv probes instead of being told, and on a
            # sparse feed it infers a bogus finite duration and parks
            # itself paused at position zero.
            #
            # Derived from the URL rather than passed in: the relay's
            # output port is the only source a Slave is ever played
            # from, so there is nothing to set and nothing to clear.
            _is_slave = req.url == f"udp://@:{REMOTE_VIDEO_OUT_PORT}"
            restart_mpv(req.url, is_rf=_is_slave)"""


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")

    src = TARGET.read_text()

    if "_is_slave" in src:
        sys.exit("ABORT: already patched")
    if "REMOTE_VIDEO_OUT_PORT" not in src:
        sys.exit("ABORT: Slave select patch not applied — this builds on it")

    n = src.count(OLD)
    if n != 1:
        sys.exit(f"ABORT: anchor found {n} times, expected exactly 1")

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_isrf"))
    TARGET.write_text(src.replace(OLD, NEW, 1))

    print("  ok  Slave video now played with the MPEG-TS flags")
    print("\nNow: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
