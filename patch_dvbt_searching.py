#!/usr/bin/env python3
"""
patch_dvbt_searching.py — show the searching screen while tuning.

Pressing Tune showed nothing at all until the tune succeeded, and
nothing ever if it failed: the mode was set after the lock, so a
receiver spent twenty seconds still displaying whatever was on before
and then, on failure, simply stayed there.

That is arbitration behaviour - "this source did not work, so keep
showing the other one" - and it belongs in tri_watch, where several
sources genuinely compete. In manual mode the operator asked for a
frequency, and the receiver should show what is happening on it:
frequency, symbol rate, SEARCHING, and either a picture or an honest
lack of one.

So the mode is set BEFORE the attempt rather than after. The OSD
switches to DVB-T2 the moment Tune is pressed, exactly as the
Picotuner's does.

A failed tune therefore leaves the receiver in dvbt mode showing a
searching screen, rather than reverting to the previous source. That is
deliberate: a tune to an empty channel should look like a tune to an
empty channel, not like nothing happened.

Usage
-----
    python3 patch_dvbt_searching.py            # dry run
    python3 patch_dvbt_searching.py --apply
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TARGET = Path("lynx_app.py")
MARKER = "so the OSD can show it while the tune runs"


# The mode and the frequency move above the tune attempt. Everything
# that genuinely depends on a successful lock stays where it is.
OLD = '''    # Cover the screen before anything else. Without it mpv carries on
    # showing whatever was on before - a stream, or the previous
    # multiplex - for the several seconds a tune takes, which reads as
    # the tune having done nothing at all. _kick_mpv() lowers it again
    # when the new picture arrives.
    start_transition_cover()
'''

NEW = '''    # Cover the screen before anything else. Without it mpv carries on
    # showing whatever was on before - a stream, or the previous
    # multiplex - for the several seconds a tune takes, which reads as
    # the tune having done nothing at all. _kick_mpv() lowers it again
    # when the new picture arrives.
    start_transition_cover()

    # Mode and frequency BEFORE the attempt, so the OSD can show it
    # while the tune runs - and keeps showing it if the tune fails.
    #
    # Setting them afterwards meant a receiver displayed the PREVIOUS
    # source for the twenty seconds a lock attempt takes, and went on
    # displaying it for ever if no lock came. That is arbitration
    # behaviour, which belongs in tri_watch where sources genuinely
    # compete; in manual mode the operator asked for a frequency and
    # is entitled to see what is happening on it. A tune to an empty
    # channel should look like one.
    current_mode = "dvbt"
    displayed_receiver_id = None
    with hdhr_lock:
        _live_early = hdhr_states.get(_dev)
        if _live_early is not None:
            _live_early["frequency_hz"] = req.freq
            _live_early["lock_mode"] = req.modulation
'''


# _dev is resolved further down, so it has to move up with the rest.
OLD2 = '''    # Ask the poller to fetch the service name on its next pass rather
    # than fetching it here.'''

NEW2 = '''    # Ask the poller to fetch the service name on its next pass rather
    # than fetching it here.'''


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

    n = text.count(OLD)
    print(f"\nchecking anchors in {TARGET}\n")
    if n != 1:
        print(f"  cover call          FAIL {n} matches - expected 1")
        print("\nNo changes made.")
        return 1
    print("  cover call          OK   one match")

    # _dev must already exist above this point. It is set in the
    # pending-service-name block, which currently sits BELOW the tune.
    # Check where it is before moving anything.
    cover_at = text.index(OLD)
    dev_at = text.find("_dev = req.device_id or hdhr_default_device_id()")
    if dev_at == -1:
        print("  _dev resolution     FAIL not found")
        print("\nNo changes made.")
        return 1
    if dev_at > cover_at:
        print("  _dev resolution     NOTE resolved below the cover call;")
        print("                      moving it up as part of this change")
        # Move the single line up, so the early state write can use it.
        dev_line = "    _dev = req.device_id or hdhr_default_device_id()\n"
        if text.count(dev_line) != 1:
            print("  _dev line           FAIL not a single clean line")
            print("\nNo changes made.")
            return 1
        text = text.replace(dev_line, "", 1)
        patched = text.replace(OLD, OLD + dev_line, 1)
        patched = patched.replace(OLD + dev_line, dev_line + NEW, 1)
    else:
        print("  _dev resolution     OK   already above")
        patched = text.replace(OLD, NEW, 1)

    # The later assignment is now redundant but harmless; leave it, so
    # the successful path still sets them explicitly.

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(patched)
        tp = Path(tmp.name)
    try:
        py_compile.compile(str(tp), doraise=True)
        print("  syntax check        OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"  syntax check        FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tp.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the change.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(TARGET, TARGET.with_suffix(f".py.bak-searching-{stamp}"))
    TARGET.write_text(patched, encoding="utf-8")
    print(f"\n  {TARGET} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
