#!/usr/bin/env python3
"""
patch_hdhomerun_lifecycle.py — restart mpv when the signal comes back.

The problem
-----------
mpv holds a stale frame when the content changes underneath it. Confirmed
on air: a station stopped transmitting, the source was changed at the
far end, and when it came back mpv was still showing the old test card.
Re-tuning through the API cleared it instantly, because that restarts
the process.

This is the same failure restart_mpv()'s own docstring describes - a
stuck video output where mpv reports decoding normally while the
displayed picture never updates - and the same reason IPC reload was
abandoned there in favour of a full restart.

On a bench that means re-tuning by hand. On a repeater, where stations
come and go all day, it would mean somebody re-tuning after every single
transmission. So the dvbt path needs what the RF path already has in
rf_mpv_lifecycle_monitor(): something watching the lock, restarting mpv
when a signal returns.

How
---
The poller already reads lock state every couple of seconds, so it is
the natural place. It remembers the previous state per device, and on a
false-to-true transition - a station keying up - restarts mpv and lowers
the transition cover.

Guarded three ways:
  - only when current_mode is "dvbt", so it cannot disturb a receiver
    watching something else while a DVB-T2 tuner happens to be locked
  - only for the device actually selected, once more than one exists
  - only when tune_lock is free, taken non-blocking. A tune in progress
    is already restarting mpv itself, and two restarts racing is how you
    get a dead player rather than a fresh one.

Requires the earlier HDHomeRun patches. lynx_app.py only.

Usage
-----
    python3 patch_hdhomerun_lifecycle.py            # dry run
    python3 patch_hdhomerun_lifecycle.py --apply    # back up and write

Then:
    python3 verify.py
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

APP = Path("lynx_app.py")
MARKER = "was_locked"


# --- 1. remember the previous lock state ----------------------------------

OLD_A = '''                status = tuner.status()
                pending_program = None
                pending_freq = None
                with hdhr_lock:'''

NEW_A = '''                status = tuner.status()
                pending_program = None
                pending_freq = None
                # Captured BEFORE the update below overwrites it: the
                # transition from not-locked to locked is the whole
                # signal this monitor acts on.
                was_locked = bool(st.get("locked"))
                with hdhr_lock:'''


# --- 2. act on the transition ---------------------------------------------

OLD_B = '''                            # Once only: a name that did not arrive is a
                            # blank label, not something to keep asking
                            # about every two seconds for ever.
                            live["pending_program"] = None'''

NEW_B = '''                            # Once only: a name that did not arrive is a
                            # blank label, not something to keep asking
                            # about every two seconds for ever.
                            live["pending_program"] = None

                # A station has keyed up. mpv will happily sit there
                # showing the last frame of whoever was on before -
                # confirmed on air, and the same stuck-output behaviour
                # restart_mpv()'s docstring describes. A full restart is
                # the only thing that reliably clears it.
                #
                # Without this, somebody has to re-tune by hand after
                # every transmission, which is no use at a repeater.
                if (status.locked and not was_locked
                        and current_mode == "dvbt"
                        and device_id == hdhr_default_device_id()):
                    # Non-blocking: a tune in progress is already
                    # restarting mpv itself, and two restarts racing
                    # gives you a dead player rather than a fresh one.
                    if tune_lock.acquire(blocking=False):
                        try:
                            print(f"[hdhr] {device_id} re-locked - "
                                  "restarting mpv")
                            restart_mpv(f"udp://@:{HDHR_VIDEO_PORT}",
                                        is_rf=False)
                            end_transition_cover()
                        except Exception as e:
                            print(f"[hdhr] restart failed: "
                                  f"{type(e).__name__}: {e}")
                        finally:
                            tune_lock.release()'''


EDITS = [
    ("remember lock state", OLD_A, NEW_A),
    ("restart on re-lock", OLD_B, NEW_B),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    args = parser.parse_args()

    if not APP.exists():
        print(f"ERROR: {APP} not found. Run from the lynx directory.")
        return 2

    text = APP.read_text(encoding="utf-8")

    if "HDHR_VIDEO_PORT" not in text:
        print("ERROR: the earlier HDHomeRun patches have not been applied.")
        return 2
    if "pending_program" not in text:
        print("ERROR: patch_hdhomerun_service_name.py has not been applied.")
        return 2
    if MARKER in text:
        print(f"{APP} already contains {MARKER} - already applied.")
        return 0

    print(f"\nchecking anchors in {APP}\n")
    ok = True
    for name, anchor, _new in EDITS:
        count = text.count(anchor)
        if count == 1:
            print(f"  {name:<22} OK   one match")
        else:
            print(f"  {name:<22} FAIL {count} matches - expected exactly 1")
            ok = False
    if not ok:
        print("\nNo changes made.")
        return 1

    patched = text
    for _name, anchor, new in EDITS:
        patched = patched.replace(anchor, new, 1)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(patched)
        tmp_path = Path(tmp.name)
    try:
        py_compile.compile(str(tmp_path), doraise=True)
        print("\n  syntax check           OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"\n  syntax check           FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tmp_path.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = APP.with_suffix(APP.suffix + f".bak-lifecycle-{stamp}")
    shutil.copy2(APP, backup)
    APP.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {APP} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
