#!/usr/bin/env python3
"""
patch_registry_step3.py — the overlay asks the registry.

Step 3 of the Lynx+ roadmap. Two decisions move: whether a picture is
genuinely on screen, and whether the transition cover should be drawn.
They are the two that caused the most trouble this week, and both are
the same mistake in different places - a question about "is something
showing" answered by enumerating modes.

Behind sources.publish, as before. With the flag off the existing logic
runs untouched, so the two can be compared directly on air by changing
one line of config.

What moves
----------
genuinely_locked was:

    (state["locked"] and state["mpv_running_for_rf"])
     or state["mode"] == "stream"
     or (state["mode"] == "dvbt" and state["hdhr_locked"])

which is three sources enumerated, each needing its own clause, and a
fourth clause waiting to be written for ATSC 3.0. The app already
computes the same thing properly in the registry, so the overlay reads
the answer rather than reassembling it.

What is fixed on the way
------------------------
The cover branch was gated on tri_watch being enabled:

    if mpv_transitioning and state["tri_watch_enabled"]:

The cover has nothing to do with tri_watch. It exists because mpv
holds its last frame when a source changes, which is true whatever is
doing the changing - and gated this way, a receiver with tri_watch off
never got a cover at all. Confirmed live: tuning DVB-T2 left the
previous source on screen throughout, which read as the tune having
done nothing.

The gate goes. The branch draws Pathfinder, the switching graphic or
SWITCHING, all of which are right for any transition.

Usage
-----
    python3 patch_registry_step3.py            # dry run
    python3 patch_registry_step3.py --apply
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TARGET = Path("lynx_overlay.py")
MARKER = "sources_showing_picture"


# --- 1. read the registry's answer from the API ----------------------

OLD_READ = '''            state["mpv_transitioning"] = lynx.get('mpv_transitioning', False)'''

NEW_READ = '''            state["mpv_transitioning"] = lynx.get('mpv_transitioning', False)
            # Lynx+ step 3. The app publishes its registry's own view
            # of whether a picture is on screen; the overlay reads the
            # answer rather than reassembling it from a mode string and
            # three lock flags. None when the flag is off, which is how
            # the old path knows to run instead.
            _srcs = lynx.get('sources')
            state["sources_showing_picture"] = (
                _srcs.get('showing_picture') if isinstance(_srcs, dict)
                and 'showing_picture' in _srcs else None)'''


# --- 2. the state key ------------------------------------------------

OLD_KEY = '''    "mpv_transitioning": False,'''

NEW_KEY = '''    "mpv_transitioning": False,
    # None means the registry is not being published, so the old
    # mode-enumerating logic runs. True/False is the registry's answer.
    "sources_showing_picture": None,'''


# --- 3. use it ------------------------------------------------------

OLD_LOCKED = '''        # dvbt has its own lock, reported by the device itself, so it
        # can answer this properly rather than taking the blanket
        # exemption "stream" needs. state["locked"] describes the
        # PICOTUNER, which on a receiver without one is correctly
        # false - and left unqualified it covered live DVB-T2 video
        # with the logo screen while mpv decoded away underneath.
        genuinely_locked = ((state["locked"] and state["mpv_running_for_rf"])
                            or state["mode"] == "stream"
                            or (state["mode"] == "dvbt" and state["hdhr_locked"]))
        showing_picture = genuinely_locked and not mpv_transitioning'''

NEW_LOCKED = '''        # The registry answers this when it is being published, and
        # the old logic runs when it is not - so the two can be
        # compared on air by changing one line of config.
        #
        # What the old logic was: three sources enumerated, each with
        # its own clause, and a fourth waiting to be written for
        # ATSC 3.0. Every clause here was added after something was
        # visibly wrong on screen - the dvbt one after the logo screen
        # covered live video while mpv decoded away underneath.
        _reg = state["sources_showing_picture"]
        if _reg is not None:
            genuinely_locked = bool(_reg)
        else:
            genuinely_locked = ((state["locked"] and state["mpv_running_for_rf"])
                                or state["mode"] == "stream"
                                or (state["mode"] == "dvbt" and state["hdhr_locked"]))
        showing_picture = genuinely_locked and not mpv_transitioning'''


# --- 4. the cover is not a tri_watch feature -------------------------

OLD_COVER = '''            if mpv_transitioning and state["tri_watch_enabled"]:'''

NEW_COVER = '''            # NOT gated on tri_watch. The cover exists because mpv
            # holds its last frame when a source changes, which is true
            # whatever is doing the changing - and gated that way, a
            # receiver with tri_watch off never got one at all.
            # Confirmed live: tuning DVB-T2 left the previous source on
            # screen for the whole attempt, which reads as the tune
            # having done nothing.
            if mpv_transitioning:'''


EDITS = [
    ("api read", OLD_READ, NEW_READ),
    ("state key", OLD_KEY, NEW_KEY),
    ("genuinely_locked", OLD_LOCKED, NEW_LOCKED),
    ("cover gate", OLD_COVER, NEW_COVER),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found. Run from the lynx directory.")
        return 2

    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"{TARGET} already patched - nothing to do.")
        return 0

    print(f"\nchecking anchors in {TARGET}\n")
    ok = True
    for name, anchor, _new in EDITS:
        n = text.count(anchor)
        if n == 1:
            print(f"  {name:<18} OK   one match")
        else:
            print(f"  {name:<18} FAIL {n} matches - expected 1")
            ok = False
    if not ok:
        print("\nNo changes made.")
        return 1

    patched = text
    for _n, anchor, new in EDITS:
        patched = patched.replace(anchor, new, 1)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(patched)
        tp = Path(tmp.name)
    try:
        py_compile.compile(str(tp), doraise=True)
        print("\n  syntax check       OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"\n  syntax check       FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tp.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(TARGET, TARGET.with_suffix(f".py.bak-registry3-{stamp}"))
    TARGET.write_text(patched, encoding="utf-8")
    print(f"\n  {TARGET} patched")
    print("\nTest both ways - sources.publish true and false - and")
    print("compare. The cover fix applies either way.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
