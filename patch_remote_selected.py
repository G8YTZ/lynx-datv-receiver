#!/usr/bin/env python3
"""
patch_remote_selected.py

Adds lynx.remote_selected to /api/status: the index of the Slave whose
video is on screen, or null.

stream_is_remote already says a Slave is playing. The overlay needs to
know which one, so it can fill its display from that Slave's record.
Taken from the relay, which is the thing that actually decides whose
packets reach mpv — asking it is asking the component that knows,
rather than keeping a second copy of the answer somewhere it could
drift out of step.

Run from the repo root:  python3 patch_remote_selected.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")

OLD = '            "stream_is_remote": (current_mode == "stream"\n'
NEW = ('            # Which Slave, not just that it is one. Read from the\n'
       '            # relay rather than stored: the relay is what decides\n'
       '            # whose packets reach mpv, so it cannot disagree with\n'
       '            # what is actually on screen.\n'
       '            "remote_selected": slave_relay.selected(),\n'
       '            "stream_is_remote": (current_mode == "stream"\n')


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")

    src = TARGET.read_text()
    if "remote_selected" in src:
        sys.exit("ABORT: already patched")
    if "stream_is_remote" not in src:
        sys.exit("ABORT: panel separation patch not applied — run that first")

    n = src.count(OLD)
    if n != 1:
        sys.exit(f"ABORT: anchor found {n} times, expected exactly 1")

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_sel"))
    TARGET.write_text(src.replace(OLD, NEW, 1))
    print("  ok  lynx.remote_selected added")
    print("\nNow: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
