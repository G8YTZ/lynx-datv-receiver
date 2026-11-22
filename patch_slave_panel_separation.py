#!/usr/bin/env python3
"""
patch_slave_panel_separation.py

Stops a selected Slave appearing in the Stream panel as well as its own.

A Slave currently plays through start_stream(), so current_mode is
"stream" and the Stream panel quite reasonably shows it. Streams and
Slaves are independent inputs though, and with several Slaves and a real
stream running at once the panel would be actively misleading about which
is on screen.

Rather than adding a "remote" mode — which would mean rethreading
current_mode through the hard-freeze and mpv-lifecycle watchdogs, the
most heavily debugged code in the file — this derives the answer from
what is already known. A Slave is the only thing ever played from the
relay's output port, so the URL alone says whether the current stream is
a Slave. Nothing new to set, and so nothing new to forget to clear: the
value cannot go stale because it is not stored.

/api/status gains lynx.stream_is_remote, and the Stream panel shows its
idle state when that is true, leaving the Slave's own panel to report it.

Does NOT change is_rf. A Slave still plays through the stream path with
the RTMP-shaped demuxer flags. That belongs with the mode work, together
with deleting the leftover "Slave RX" memory.

Run from ~/lynx:  python3 patch_slave_panel_separation.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


def apply(src: str, old: str, new: str, label: str) -> str:
    n = src.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected exactly 1")
    print(f"  ok  {label}")
    return src.replace(old, new, 1)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from ~/lynx")

    src = TARGET.read_text()
    before = len(src.splitlines())

    if "REMOTE_VIDEO_OUT_PORT" not in src:
        sys.exit("ABORT: select patch not applied — run patch_slave_select.py first")
    if "stream_is_remote" in src:
        sys.exit("ABORT: already patched")

    # ── 1. derived flag in /api/status ───────────────────────────
    src = apply(
        src,
        '            "stream_info": get_live_stream_info() if current_mode == "stream" else None,',
        '            # True when what is playing is a Slave rather than a real\n'
        '            # stream. Derived from the URL rather than stored: the relay\n'
        '            # output port is the only thing a Slave is ever played from,\n'
        '            # so this cannot disagree with reality the way a separate\n'
        '            # flag set in one place and cleared in three eventually\n'
        '            # would. The Stream panel uses it to stand down.\n'
        '            "stream_is_remote": (current_mode == "stream"\n'
        '                                 and current_stream_url == f"udp://@:{REMOTE_VIDEO_OUT_PORT}"),\n'
        '            "stream_info": get_live_stream_info() if current_mode == "stream" else None,',
        "status stream_is_remote",
    )

    # ── 2. Stream panel stands down for a Slave ──────────────────
    src = apply(
        src,
        "        if (lynxMode === 'stream') {",
        "        // A Slave plays through the stream path but is not a stream,\n"
        "        // and showing it in both panels leaves no way to tell which\n"
        "        // input is actually on screen once there is more than one of\n"
        "        // either. Its own panel reports it; this one stays idle.\n"
        "        const streamIsRemote = s.lynx?.stream_is_remote === true;\n"
        "        if (lynxMode === 'stream' && !streamIsRemote) {",
        "Stream panel stands down",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak4"))
    TARGET.write_text(src)

    after = len(src.splitlines())
    print(f"\n  lynx_app.py: {before} -> {after} lines (+{after - before})")
    print("  backup: lynx_app.py.bak4")
    print("\nNow: python3 -m py_compile lynx_app.py, then node --check the script blocks")


if __name__ == "__main__":
    main()
