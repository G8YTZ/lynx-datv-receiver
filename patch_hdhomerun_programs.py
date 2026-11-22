#!/usr/bin/env python3
"""
patch_hdhomerun_programs.py — pick a service by name, not by number.

The program field was a plain number box, so any number could be typed
and the device would accept it: a lock with no picture and nothing to
say why. The multiplex knows exactly what is in it, so the receiver
should ask rather than making the operator guess.

What it does
------------
1. GET /api/hdhomerun/programs - the services in the currently tuned
   multiplex, from the device's own streaminfo.

2. The number box becomes a dropdown, filled from that endpoint after
   every tune. On a broadcast multiplex it lists the services by name;
   on an amateur transmission it lists the one service, usually the
   callsign.

3. Changing the dropdown switches service immediately without
   retuning. The tuner is already locked, so there is nothing to
   reacquire - it is a demultiplexer change, not an RF one.

The endpoint reads from the device rather than from cached state,
because the program list is only wanted when somebody opens the
dropdown - there is no reason to poll it every two seconds alongside
the status.

Usage
-----
    python3 patch_hdhomerun_programs.py            # dry run
    python3 patch_hdhomerun_programs.py --apply    # back up and write

Then verify.py, restart, and check the browser console is clean.
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
MARKER = "api/hdhomerun/programs"


# --- 1. the endpoint ------------------------------------------------------

OLD_API = '''@app.post("/api/hdhomerun/stop", tags=["RF Reception"],'''

NEW_API = '''@app.get("/api/hdhomerun/programs", tags=["RF Reception"],
         summary="Services in the currently tuned multiplex",
         description="Read from the device's own streaminfo. Empty when "
                     "the tuner is not locked, since an unlocked tuner "
                     "has nothing to list.")
def hdhomerun_programs(device_id: str = None):
    # Read live rather than from the polled state: this is wanted when
    # somebody opens the dropdown, which is rare, and there is no
    # reason to fetch it every two seconds alongside the status.
    try:
        tuner = lynx_hdhomerun.HDHomeRunTuner(
            device_id=device_id or hdhr_default_device_id() or "",
            tuner=0)
        return {"programs": tuner.stream_info()}
    except lynx_hdhomerun.HDHomeRunError as e:
        return {"programs": [], "error": str(e)}


@app.post("/api/hdhomerun/stop", tags=["RF Reception"],'''


# --- 2. the dropdown replaces the number box ------------------------------

OLD_INPUT = '''                        <div class="col-5">
                            <input type="number" class="form-control form-control-sm bg-dark text-light border-secondary"
                                   id="dvbt-program" placeholder="Program" value="1">
                        </div>'''

NEW_INPUT = '''                        <div class="col-5">
                            <select class="form-select form-select-sm bg-dark text-light border-secondary"
                                    id="dvbt-program" onchange="switchDvbtProgram()">
                                <option value="">Service...</option>
                            </select>
                        </div>'''


# --- 3. fill it after a tune, and switch on change ------------------------

OLD_FN = '''async function tuneDvbtManual() {'''

NEW_FN = '''async function loadDvbtPrograms() {
    // Filled from the multiplex itself rather than typed. A number box
    // let any number be entered, and the device would accept one that
    // did not exist - a lock with no picture and nothing to say why.
    var sel = document.getElementById('dvbt-program');
    if (!sel) { return; }
    var wanted = sel.value;
    try {
        var r = await api('GET', '/api/hdhomerun/programs');
        var progs = (r && r.programs) || [];
        // Rebuilt with DOM calls rather than innerHTML: service names
        // come from the broadcaster and can contain anything at all,
        // and concatenating them into markup is how a stray quote
        // takes the whole page down.
        while (sel.firstChild) { sel.removeChild(sel.firstChild); }
        if (!progs.length) {
            var none = document.createElement('option');
            none.value = '';
            none.textContent = 'No services';
            sel.appendChild(none);
            return;
        }
        for (var i = 0; i < progs.length; i++) {
            var o = document.createElement('option');
            o.value = progs[i].program;
            o.textContent = progs[i].name || progs[i].program;
            sel.appendChild(o);
        }
        // Keep the current selection if it survived the retune,
        // otherwise take the first service - which is the right
        // default for an amateur transmission carrying only one.
        sel.value = wanted;
        if (!sel.value) { sel.selectedIndex = 0; }
    } catch (e) {
        while (sel.firstChild) { sel.removeChild(sel.firstChild); }
        var err = document.createElement('option');
        err.value = '';
        err.textContent = 'Unavailable';
        sel.appendChild(err);
    }
}

async function switchDvbtProgram() {
    // No retune: the tuner is already locked on the multiplex, so this
    // is a demultiplexer change and takes effect immediately. Sending
    // a full tune here would drop the lock and reacquire it for no
    // reason, with several seconds of black screen to show for it.
    var sel = document.getElementById('dvbt-program');
    if (!sel || !sel.value) { return; }
    var f = document.getElementById('dvbt-freq').value;
    var bw = document.getElementById('dvbt-bw').value;
    var std = document.getElementById('dvbt-std').value;
    await tuneDvbt(f, 't' + bw + std, sel.value);
}

async function tuneDvbtManual() {'''


# --- 4. refresh the list after tuning ------------------------------------

OLD_TUNE = '''    if (!f) { return; }
    await tuneDvbt(f, 't' + bw + std, prog);
}'''

NEW_TUNE = '''    if (!f) { return; }
    await tuneDvbt(f, 't' + bw + std, prog);
    // After the tune, not before: the list comes from the multiplex,
    // and until the tuner has locked there is no multiplex to ask.
    await loadDvbtPrograms();
}'''


EDITS = [
    ("programs endpoint", OLD_API, NEW_API),
    ("program dropdown", OLD_INPUT, NEW_INPUT),
    ("dropdown functions", OLD_FN, NEW_FN),
    ("refresh after tune", OLD_TUNE, NEW_TUNE),
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

    if "dvbt-tune-card" not in text:
        print("ERROR: patch_hdhomerun_webui2.py has not been applied.")
        return 2
    if MARKER in text:
        print(f"{APP} already contains the programs endpoint - nothing to do.")
        return 0

    print(f"\nchecking anchors in {APP}\n")
    ok = True
    for name, anchor, _new in EDITS:
        count = text.count(anchor)
        if count == 1:
            print(f"  {name:<20} OK   one match")
        else:
            print(f"  {name:<20} FAIL {count} matches - expected exactly 1")
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
        print("\n  syntax check         OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"\n  syntax check         FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tmp_path.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = APP.with_suffix(APP.suffix + f".bak-programs-{stamp}")
    shutil.copy2(APP, backup)
    APP.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {APP} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
