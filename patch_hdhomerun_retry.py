#!/usr/bin/env python3
"""
patch_hdhomerun_retry.py — keep asking for the service list.

The device reports lock before its service tables can be read. Both
consumers of streaminfo were written as though a lock meant the list
was available, so both failed on the first attempt and succeeded on a
retune - which is exactly what it looked like from the outside: "a bit
flaky, gets there after a retune".

Two fixes, both the same idea: retry rather than wait a fixed time. A
fixed delay is a guess that is either too short sometimes or too long
always, and this one was already both.

1. The poller cleared pending_program whether or not it found the
   name. If the tables were not ready it gave up permanently, and the
   callsign stayed empty until the next tune. It now clears only on
   success, and gives up after a bounded number of attempts so a
   multiplex that genuinely never reports one does not have it asked
   for every two seconds for ever.

2. loadDvbtPrograms() read the list once, 1.5 seconds after the tune.
   It now retries a few times, half a second apart, and stops as soon
   as it has something. The fixed delay it replaces goes, so a device
   that is ready immediately no longer waits at all.

Usage
-----
    python3 patch_hdhomerun_retry.py            # dry run
    python3 patch_hdhomerun_retry.py --apply    # back up and write
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
MARKER = "pending_attempts"


# --- 1. the poller keeps trying ------------------------------------------

OLD_SET = '''            live["last_program"] = live["pending_program"]
            live["logged_callsign"] = ""'''

NEW_SET = '''            live["last_program"] = live["pending_program"]
            live["logged_callsign"] = ""
            # Attempts remaining. The device reports lock before its
            # service tables can be read, so the first ask usually
            # comes back empty - bounded rather than endless, since a
            # multiplex that genuinely never names its services should
            # not be interrogated about it every two seconds for ever.
            live["pending_attempts"] = 10'''


OLD_FETCH = '''                if pending_program and status.locked:
                    raw = ""
                    for prog in tuner.stream_info():
                        if prog.get("program") == pending_program:
                            raw = prog.get("name", "")
                            break'''

NEW_FETCH = '''                if pending_program and status.locked:
                    raw = ""
                    try:
                        for prog in tuner.stream_info():
                            if prog.get("program") == pending_program:
                                raw = prog.get("name", "")
                                break
                    except lynx_hdhomerun.HDHomeRunError:
                        # Not ready yet, most likely. Left to the retry
                        # below rather than treated as a failure.
                        raw = ""'''


OLD_CLEAR = '''                            # Once only: a name that did not arrive is a
                            # blank label, not something to keep asking
                            # about every two seconds for ever.
                            live["pending_program"] = None'''

NEW_CLEAR = '''                            # Cleared on success only. The earlier version
                            # cleared it either way, so an empty first
                            # read - which is the usual case, since the
                            # tables lag the lock - gave up permanently
                            # and left the callsign blank until the next
                            # tune. That is what "flaky, works after a
                            # retune" was.
                            if raw:
                                live["pending_program"] = None
                            else:
                                _left = int(live.get("pending_attempts", 0)) - 1
                                live["pending_attempts"] = _left
                                if _left <= 0:
                                    live["pending_program"] = None'''


# --- 2. the dropdown retries instead of waiting --------------------------

OLD_DELAY = '''    // And not immediately after either. The tune returns once the
    // device reports lock, but streaminfo needs the service tables
    // to have been read, which takes a moment longer - asking too
    // early returned an empty list, and it took three tunes before
    // the dropdown filled.
    await new Promise(function (r) { setTimeout(r, 1500); });
    await loadDvbtPrograms();'''

NEW_DELAY = '''    // Retried rather than delayed. The tune returns once the device
    // reports lock, but streaminfo needs the service tables, which
    // lag it by an amount nobody can predict - a fixed wait was
    // either too short sometimes or too long always, and the one it
    // replaces managed both.
    await loadDvbtPrograms(8);'''


OLD_FN = '''async function loadDvbtPrograms() {
    // Filled from the multiplex itself rather than typed. A number box
    // let any number be entered, and the device would accept one that
    // did not exist - a lock with no picture and nothing to say why.
    var sel = document.getElementById('dvbt-program');
    if (!sel) { return; }
    var wanted = sel.value;
    try {
        var r = await api('GET', '/api/hdhomerun/programs');
        var progs = (r && r.programs) || [];'''

NEW_FN = '''async function loadDvbtPrograms(retries) {
    // Filled from the multiplex itself rather than typed. A number box
    // let any number be entered, and the device would accept one that
    // did not exist - a lock with no picture and nothing to say why.
    var sel = document.getElementById('dvbt-program');
    if (!sel) { return; }
    var wanted = sel.value;
    try {
        var r = await api('GET', '/api/hdhomerun/programs');
        var progs = (r && r.programs) || [];
        // Nothing yet, and attempts left: the tables lag the lock, so
        // an empty answer this soon means "not ready" rather than "no
        // services". Stops as soon as there is something, so a device
        // that answers immediately waits for nothing at all.
        if (!progs.length && retries && retries > 0) {
            await new Promise(function (r2) { setTimeout(r2, 500); });
            return await loadDvbtPrograms(retries - 1);
        }'''


EDITS = [
    ("attempt counter", OLD_SET, NEW_SET),
    ("tolerant fetch", OLD_FETCH, NEW_FETCH),
    ("clear on success", OLD_CLEAR, NEW_CLEAR),
    ("dropdown retry call", OLD_DELAY, NEW_DELAY),
    ("dropdown retry loop", OLD_FN, NEW_FN),
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

    if "pending_program" not in text:
        print("ERROR: the service-name patch has not been applied.")
        return 2
    if MARKER in text:
        print(f"{APP} already patched - nothing to do.")
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
    backup = APP.with_suffix(APP.suffix + f".bak-retry-{stamp}")
    shutil.copy2(APP, backup)
    APP.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {APP} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
