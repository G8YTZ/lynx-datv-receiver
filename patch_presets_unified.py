#!/usr/bin/env python3
"""
patch_presets_unified.py — one memory list, not two.

Step 6 of the Lynx+ roadmap, brought forward because the reason is a
good one: the Knobler turns through a single list, and a preset the
front panel cannot reach may as well not exist. The web UI should show
what the front panel shows.

The storage was always unified - one presets: list in the config, each
entry carrying its own type - so nothing migrates and every existing
preset keeps working untouched. Only the display and the dispatch were
split, and both were my caution rather than a requirement.

What changes
------------
1. /api/preset gains a dvbt branch, so tuning by name works for a
   DVB-T2 memory as it already does for RF and stream.

2. The Picotuner card stops filtering DVB-T2 presets out, and the
   DVB-T2 card's own list goes. One list, on the RF card, showing
   everything.

3. Each entry is marked with what it is, so clicking one is never a
   surprise: a dish for DVB-S2, a screen for DVB-T2, an aerial for a
   stream.

Why the filter existed
----------------------
A DVB-T2 preset clicked on the Picotuner's card would have tuned the
Picotuner to a frequency in kHz that was really MHz - a number it
would accept without complaint. That was a real hazard, and filtering
was the quick answer. Dispatching on the preset's own type is the
right one, and it is three lines.

Usage
-----
    python3 patch_presets_unified.py            # dry run
    python3 patch_presets_unified.py --apply

Then verify.py, and check the browser console.
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
MARKER = "preset_type == \"dvbt\""


# --- 1. tune by name, whatever kind it is ---------------------------

OLD_TUNE = '''            if preset_type == "stream":
                result = start_stream(StreamRequest(url=p['url'], name=p['name']))
                current_preset = p['name']
                return result
            else:'''

NEW_TUNE = '''            if preset_type == "stream":
                result = start_stream(StreamRequest(url=p['url'], name=p['name']))
                current_preset = p['name']
                return result
            elif preset_type == "dvbt":
                # Dispatched on the preset's own type rather than kept
                # in a separate list. The two were split because a
                # DVB-T2 memory clicked on the Picotuner's card would
                # have tuned it to a frequency in kHz that was really
                # MHz - a number it accepts without complaint. Knowing
                # what a preset IS fixes that properly, and means the
                # Knobler can reach every memory rather than two
                # thirds of them.
                result = tune_dvbt(DvbtTuneRequest(
                    freq=p['freq_hz'],
                    modulation=p.get('modulation', 't8dvbt2'),
                    program=p.get('program'),
                    device_id=p.get('device_id'),
                ))
                current_preset = p['name']
                return result
            else:'''


# --- 2. the Picotuner list stops filtering --------------------------

OLD_FILTER = '''        const local = (data.local || []).map(p => ({...p, _local: true}));
        // DVB-T2 memories belong to their own card. Left in here they
        // would tune the Picotuner to a frequency in kHz that was
        // really MHz - a number it would accept without complaint.
        const all = [...local, ...(data.ryde || [])]
                        .filter(p => p.type !== 'dvbt');
        const el = document.getElementById('preset-list');'''

NEW_FILTER = '''        const local = (data.local || []).map(p => ({...p, _local: true}));
        // One list, every kind. /api/preset dispatches on the preset's
        // own type, so clicking a DVB-T2 memory tunes the HDHomeRun
        // rather than handing the Picotuner a number in the wrong
        // unit - which is why these used to be filtered apart.
        //
        // One list also because the Knobler turns through one, and a
        // memory the front panel cannot reach may as well not exist.
        const all = [...local, ...(data.ryde || [])];
        const el = document.getElementById('preset-list');'''


# --- 3. mark each one with what it is -------------------------------

OLD_ICON = '''                    ${(p.type === 'stream') ? '&#x1F4F6; ' : ''}${p.name}
                    ${p.freq ? '<small class="text-muted float-end">' + (p.freq/1000).toFixed(3) + ' MHz</small>' : ''}'''

NEW_ICON = '''                    ${(p.type === 'stream') ? '&#x1F4F6; ' : (p.type === 'dvbt') ? '&#x1F4FA; ' : '&#x1F4E1; '}${p.name}
                    ${p.freq ? '<small class="text-muted float-end">' + (p.freq/1000).toFixed(3) + ' MHz</small>'
                             : p.freq_hz ? '<small class="text-muted float-end">' + (p.freq_hz/1e6).toFixed(3) + ' MHz</small>' : ''}'''


# --- 4. the DVB-T2 card's own list goes -----------------------------

OLD_CARD = '''                    <h6 class="text-muted">Presets</h6>
                    <div id="dvbt-preset-list" class="mb-3"
                         style="max-height: 180px; overflow-y: auto;">
                        <div class="text-muted small">No presets</div>
                    </div>
                    <hr>
                    <h6 class="text-muted">Manual Tune (kHz)</h6>'''

NEW_CARD = '''                    <!-- No preset list here: DVB-T2 memories live in the
                         one list on the RF card above, alongside DVB-S2
                         and streams, so the Knobler and the Web UI show
                         the same thing. -->
                    <h6 class="text-muted">Manual Tune (kHz)</h6>'''


EDITS = [
    ("tune dispatch", OLD_TUNE, NEW_TUNE),
    ("list filter", OLD_FILTER, NEW_FILTER),
    ("preset icons", OLD_ICON, NEW_ICON),
    ("dvbt card list", OLD_CARD, NEW_CARD),
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
            print(f"  {name:<16} OK   one match")
        else:
            print(f"  {name:<16} FAIL {n} matches - expected 1")
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
        print("\n  syntax check     OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"\n  syntax check     FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tp.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(TARGET, TARGET.with_suffix(f".py.bak-presets-{stamp}"))
    TARGET.write_text(patched, encoding="utf-8")
    print(f"\n  {TARGET} patched")
    print("\nNow run:  python3 verify.py")
    print("Existing presets are untouched - the storage was always one")
    print("list, so nothing migrates.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
