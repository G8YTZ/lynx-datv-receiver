#!/usr/bin/env python3
"""
patch_hdhomerun_presets.py — memories for the DVB-T2 tuner.

The Picotuner card has a save-as-preset button and a preset list; the
DVB-T2 card had neither, so every tune meant typing a frequency.

Presets are already a typed collection - add_preset() takes "rf" or
"stream" - so this is a third type rather than a second store. The
config file, the delete path and the persistence all work unchanged.

What it adds
------------
1. A "dvbt" preset type, carrying frequency in Hz, modulation and
   program.
2. A save button on the DVB-T2 card, beside Tune.
3. A preset list on that card, and a filter on the Picotuner's so the
   two do not show each other's. Without that filter a DVB-T2 preset
   would appear under the Picotuner and, clicked, would tune it to a
   frequency in kHz that was really MHz.

Deliberately NOT included: a boot-default button. The resume already
brings back whatever was last tuned, which covers the case that
matters, and the boot default is Picotuner-shaped (freq, sr, plug,
lnb_lo_khz) in both its API and its resume path. Making that general
is a bigger change than it looks and can wait for a reason to do it.

Usage
-----
    python3 patch_hdhomerun_presets.py            # dry run
    python3 patch_hdhomerun_presets.py --apply    # back up and write

Then verify.py, restart, and check the browser console.
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
MARKER = "saveDvbtMemory"


# --- 1. the request model gains DVB-T2 fields -----------------------------

OLD_MODEL = '''    # Stream field (type="stream")
    url: Optional[str] = None'''

NEW_MODEL = '''    # Stream field (type="stream")
    url: Optional[str] = None
    # DVB-T2 fields (type="dvbt"). freq_hz rather than reusing freq:
    # that one is kHz by long-standing convention throughout Lynx, and
    # the device's own API works in Hz. Two units in one field is how a
    # 437 MHz contact once got logged as 0.437 MHz.
    freq_hz: Optional[int] = None
    modulation: Optional[str] = None
    program: Optional[str] = None'''


# --- 2. the endpoint accepts the type -------------------------------------

OLD_TYPE = '''    preset_type = req.type if req.type in ("rf", "stream") else "rf"'''

NEW_TYPE = '''    preset_type = req.type if req.type in ("rf", "stream", "dvbt") else "rf"'''


OLD_VALIDATE = '''    else:
        if req.freq is None or req.sr is None:
            raise HTTPException(status_code=400, detail="An RF memory needs a frequency and symbol rate")
        name = req.name.strip() if req.name.strip() else f"{req.freq/1000:.3f} MHz"'''

NEW_VALIDATE = '''    elif preset_type == "dvbt":
        if req.freq_hz is None or not req.modulation:
            raise HTTPException(status_code=400,
                                detail="A DVB-T2 memory needs a frequency and modulation")
        name = (req.name.strip() if req.name.strip()
                else f"{req.freq_hz/1e6:.3f} MHz")
    else:
        if req.freq is None or req.sr is None:
            raise HTTPException(status_code=400, detail="An RF memory needs a frequency and symbol rate")
        name = req.name.strip() if req.name.strip() else f"{req.freq/1000:.3f} MHz"'''


OLD_SAME = '''            if preset_type == "stream":
                same = (p.get('url') == req.url)
            else:'''

NEW_SAME = '''            if preset_type == "stream":
                same = (p.get('url') == req.url)
            elif preset_type == "dvbt":
                same = (p.get('freq_hz') == req.freq_hz
                        and p.get('modulation') == req.modulation
                        and p.get('program') == req.program)
            else:'''


OLD_APPEND = '''    else:
        config['presets'].append({
            "type": "rf",
            "name": name,'''

NEW_APPEND = '''    elif preset_type == "dvbt":
        config['presets'].append({
            "type": "dvbt",
            "name": name,
            "freq_hz": req.freq_hz,
            "modulation": req.modulation,
            "program": req.program,
            "note": "User saved via web UI"
        })
    else:
        config['presets'].append({
            "type": "rf",
            "name": name,'''


# --- 3. the Picotuner list stops showing DVB-T2 presets -------------------

OLD_LIST = '''        const local = (data.local || []).map(p => ({...p, _local: true}));
        const all = [...local, ...(data.ryde || [])];
        const el = document.getElementById('preset-list');'''

NEW_LIST = '''        const local = (data.local || []).map(p => ({...p, _local: true}));
        // DVB-T2 memories belong to their own card. Left in here they
        // would tune the Picotuner to a frequency in kHz that was
        // really MHz - a number it would accept without complaint.
        const all = [...local, ...(data.ryde || [])]
                        .filter(p => p.type !== 'dvbt');
        const el = document.getElementById('preset-list');'''


# --- 4. the save button ---------------------------------------------------

OLD_BUTTON = '''                    <button class="btn btn-danger w-100"
                            onclick="tuneDvbtManual()">Tune</button>'''

NEW_BUTTON = '''                    <div class="d-flex gap-2">
                        <button class="btn btn-danger flex-grow-1"
                                onclick="tuneDvbtManual()">Tune</button>
                        <button class="btn btn-outline-warning"
                                onclick="saveDvbtMemory()"
                                title="Save this frequency and mode as a preset">&#x1F4BE;</button>
                    </div>'''


# --- 5. the preset list on the card ---------------------------------------

OLD_CARDLIST = '''                <div class="card-body">
                    <h6 class="text-muted">Manual Tune</h6>'''

NEW_CARDLIST = '''                <div class="card-body">
                    <h6 class="text-muted">Presets</h6>
                    <div id="dvbt-preset-list" class="mb-3"
                         style="max-height: 180px; overflow-y: auto;">
                        <div class="text-muted small">No presets</div>
                    </div>
                    <hr>
                    <h6 class="text-muted">Manual Tune</h6>'''


# --- 6. the functions -----------------------------------------------------

OLD_FN = '''async function tuneDvbtManual() {'''

NEW_FN = '''async function saveDvbtMemory() {
    var f = document.getElementById('dvbt-freq').value;
    var bw = document.getElementById('dvbt-bw').value;
    var std = document.getElementById('dvbt-std').value;
    var progSel = document.getElementById('dvbt-program');
    if (!f) { return; }
    var name = prompt('Name this preset:', parseFloat(f).toFixed(3) + ' MHz');
    if (name === null) { return; }
    var result = await api('POST', '/api/presets/add', {
        type: 'dvbt',
        name: name,
        freq_hz: Math.round(parseFloat(f) * 1e6),
        modulation: 't' + bw + std,
        program: (progSel && progSel.value) ? progSel.value : null
    });
    if (result && result.note === 'already saved') {
        alert('A preset with this exact name and tuning already exists.');
    } else if (result && result.note === 'name already used') {
        alert('A preset named "' + name + '" already exists with different tuning.');
    }
    await loadDvbtPresets();
}

async function loadDvbtPresets() {
    var el = document.getElementById('dvbt-preset-list');
    if (!el) { return; }
    try {
        var data = await api('GET', '/api/presets');
        var all = (data.local || []).filter(function (p) {
            return p.type === 'dvbt';
        });
        // Rebuilt with DOM calls rather than innerHTML. A preset name
        // is whatever somebody typed, and concatenating it into an
        // onclick attribute means one apostrophe takes the page down.
        while (el.firstChild) { el.removeChild(el.firstChild); }
        if (!all.length) {
            var none = document.createElement('div');
            none.className = 'text-muted small';
            none.textContent = 'No presets';
            el.appendChild(none);
            return;
        }
        all.forEach(function (p) {
            var row = document.createElement('div');
            row.className = 'd-flex align-items-center gap-1 mb-1';

            var b = document.createElement('button');
            b.className = 'btn btn-outline-secondary btn-sm flex-grow-1 text-start text-light';
            b.textContent = p.name;
            var mhz = document.createElement('small');
            mhz.className = 'text-muted float-end';
            mhz.textContent = (p.freq_hz / 1e6).toFixed(3) + ' MHz';
            b.appendChild(mhz);
            b.addEventListener('click', function () {
                tuneDvbt(p.freq_hz / 1e6, p.modulation, p.program);
            });
            row.appendChild(b);

            var d = document.createElement('button');
            d.className = 'btn btn-outline-danger btn-sm';
            d.title = 'Delete';
            d.textContent = '\\u00d7';
            d.addEventListener('click', function () {
                deletePreset(p.name);
                setTimeout(loadDvbtPresets, 300);
            });
            row.appendChild(d);

            el.appendChild(row);
        });
    } catch (e) { /* the card still tunes without its presets */ }
}

async function tuneDvbtManual() {'''


# --- 7. load them at startup ---------------------------------------------

OLD_INIT = '''    await loadDvbtPrograms();
}'''

NEW_INIT = '''    await loadDvbtPrograms();
    await loadDvbtPresets();
}'''


EDITS = [
    ("request model", OLD_MODEL, NEW_MODEL),
    ("accepted types", OLD_TYPE, NEW_TYPE),
    ("validation", OLD_VALIDATE, NEW_VALIDATE),
    ("duplicate check", OLD_SAME, NEW_SAME),
    ("append branch", OLD_APPEND, NEW_APPEND),
    ("picotuner filter", OLD_LIST, NEW_LIST),
    ("save button", OLD_BUTTON, NEW_BUTTON),
    ("preset list markup", OLD_CARDLIST, NEW_CARDLIST),
    ("preset functions", OLD_FN, NEW_FN),
    ("load after tune", OLD_INIT, NEW_INIT),
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
        print("ERROR: the web UI patches have not been applied.")
        return 2
    if MARKER in text:
        print(f"{APP} already patched - nothing to do.")
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
    backup = APP.with_suffix(APP.suffix + f".bak-dvbtpresets-{stamp}")
    shutil.copy2(APP, backup)
    APP.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {APP} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
