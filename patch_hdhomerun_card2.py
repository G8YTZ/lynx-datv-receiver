#!/usr/bin/env python3
"""
patch_hdhomerun_card2.py — DVB-T2 card: layout, auto standard, defaults.

Four changes
------------
1. The DVB-T / DVB-T2 selector goes. The tuner detects the standard
   itself; asking the operator to name it is asking them to know
   something the device already knows.

   It is NOT replaced with the device's own "auto" modulation, which
   was confirmed unreliable - it repeatedly failed to lock a mux that
   locked within seconds when the modulation was given explicitly. Lynx
   tries DVB-T2 and then DVB-T instead. Two explicit attempts beat one
   unreliable one, and the second is only reached when the first fails.

2. Layout matched to the Picotuner card: tuning on the first row,
   service on the second, then Tune with the save and boot-default
   buttons beside it, same sizes.

3. A boot-default button, and the config entry gains a type so the
   resume can tell a DVB-T2 default from a Picotuner one.

4. The startup fallback learns that type. Without it a DVB-T2 default
   would be handed to _resume_tune(), which would ask a Picotuner that
   may not exist for a frequency in the wrong unit.

Usage
-----
    python3 patch_hdhomerun_card2.py            # dry run
    python3 patch_hdhomerun_card2.py --apply    # back up and write

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
MARKER = "saveDvbtBootDefault"


# --- 1. the form: no standard selector, service on its own row ------------

OLD_FORM = '''                    <div class="row g-2 mb-2">
                        <div class="col-7">
                            <select class="form-select form-select-sm bg-dark text-light border-secondary" id="dvbt-std">
                                <option value="dvbt2" selected>DVB-T2</option>
                                <option value="dvbt">DVB-T</option>
                            </select>
                        </div>
                        <div class="col-5">
                            <select class="form-select form-select-sm bg-dark text-light border-secondary"
                                    id="dvbt-program" onchange="switchDvbtProgram()">
                                <option value="">Service...</option>
                            </select>
                        </div>
                    </div>
                    <div class="d-flex gap-2">
                        <button class="btn btn-danger flex-grow-1"
                                onclick="tuneDvbtManual()">Tune</button>
                        <button class="btn btn-outline-warning"
                                onclick="saveDvbtMemory()"
                                title="Save this frequency and mode as a preset">&#x1F4BE;</button>
                    </div>'''

NEW_FORM = '''                    <div class="d-flex gap-2 align-items-center">
                        <select class="form-select form-select-sm bg-dark text-light border-secondary"
                                id="dvbt-program" onchange="switchDvbtProgram()">
                            <option value="">Service...</option>
                        </select>
                        <button class="btn btn-danger btn-sm" onclick="tuneDvbtManual()">Tune</button>
                        <button class="btn btn-outline-warning btn-sm"
                                onclick="saveDvbtMemory()"
                                title="Save this frequency and mode as a preset">&#x1F4BE;</button>
                        <button class="btn btn-outline-info btn-sm"
                                onclick="saveDvbtBootDefault()"
                                title="Use this as the fallback on startup, if there's nothing to resume">&#x1F3E0;</button>
                    </div>
                    <div id="dvbt-boot-note" class="text-muted small mt-1"></div>'''


# --- 2. the help text no longer mentions a standard selector --------------

OLD_HELP = '''                    <div class="text-muted small mt-2">
                        Frequency in MHz. Bandwidth is the channel width,
                        not the bitrate - 1 and 2 MHz are the narrowband
                        amateur modes, 7 and 8 MHz are broadcast.
                    </div>'''

NEW_HELP = '''                    <div class="text-muted small mt-2">
                        Frequency in MHz. Bandwidth is the channel width, not
                        the bitrate - 1 and 2 MHz are the narrowband amateur
                        modes, 7 and 8 MHz are broadcast. DVB-T2 is tried
                        first and DVB-T after it, so there is nothing to
                        choose.
                    </div>'''


# --- 3. tuning tries T2 then T -------------------------------------------

OLD_TUNE = '''async function tuneDvbtManual() {
    var f = document.getElementById('dvbt-freq').value;
    var bw = document.getElementById('dvbt-bw').value;
    var std = document.getElementById('dvbt-std').value;
    var prog = document.getElementById('dvbt-program').value;
    if (!f) { return; }
    await tuneDvbt(f, 't' + bw + std, prog);'''

NEW_TUNE = '''async function tuneDvbtManual() {
    var f = document.getElementById('dvbt-freq').value;
    var bw = document.getElementById('dvbt-bw').value;
    var prog = document.getElementById('dvbt-program').value;
    if (!f) { return; }
    // DVB-T2 first, DVB-T if that does not lock. The device's own
    // "auto" modulation exists but was confirmed unreliable - it
    // repeatedly failed to lock a mux that locked within seconds when
    // the modulation was named - so two explicit attempts it is. The
    // second only runs when the first genuinely fails, which costs
    // the lock timeout once and nothing thereafter.
    try {
        await tuneDvbt(f, 't' + bw + 'dvbt2', prog);
    } catch (e) {
        await tuneDvbt(f, 't' + bw + 'dvbt', prog);
    }'''


OLD_SWITCH = '''    var f = document.getElementById('dvbt-freq').value;
    var bw = document.getElementById('dvbt-bw').value;
    var std = document.getElementById('dvbt-std').value;
    await tuneDvbt(f, 't' + bw + std, sel.value);'''

NEW_SWITCH = '''    var f = document.getElementById('dvbt-freq').value;
    var bw = document.getElementById('dvbt-bw').value;
    // Whatever is locked now - the tuner reports it, so there is no
    // need to guess or retry here.
    var lm = (window._dvbtLockMode || ('t' + bw + 'dvbt2'));
    await tuneDvbt(f, lm, sel.value);'''


OLD_SAVEMEM = '''    var f = document.getElementById('dvbt-freq').value;
    var bw = document.getElementById('dvbt-bw').value;
    var std = document.getElementById('dvbt-std').value;
    var progSel = document.getElementById('dvbt-program');'''

NEW_SAVEMEM = '''    var f = document.getElementById('dvbt-freq').value;
    var bw = document.getElementById('dvbt-bw').value;
    var std = (window._dvbtLockMode || '').indexOf('dvbt2') >= 0 ? 'dvbt2'
            : ((window._dvbtLockMode || '').indexOf('dvbt') >= 0 ? 'dvbt' : 'dvbt2');
    var progSel = document.getElementById('dvbt-program');'''


# --- 4. remember what actually locked ------------------------------------

OLD_REMEMBER = '''                        var lm = hh.lock_mode || '';
                        var dbw = lm.charAt(1);'''

NEW_REMEMBER = '''                        var lm = hh.lock_mode || '';
                        // Kept so the form does not have to ask which
                        // standard this is - the tuner has already said.
                        if (lm) { window._dvbtLockMode = lm; }
                        var dbw = lm.charAt(1);'''


# --- 5. the boot default -------------------------------------------------

OLD_BOOTFN = '''async function loadDvbtPresets() {'''

NEW_BOOTFN = '''async function saveDvbtBootDefault() {
    var f = document.getElementById('dvbt-freq').value;
    var bw = document.getElementById('dvbt-bw').value;
    var progSel = document.getElementById('dvbt-program');
    if (!f) { return; }
    if (!confirm('Use ' + parseFloat(f).toFixed(3) + ' MHz as the fallback on '
                 + 'startup, whenever there is nothing previous to resume?')) { return; }
    var std = (window._dvbtLockMode || '').indexOf('dvbt2') >= 0 ? 'dvbt2'
            : ((window._dvbtLockMode || '').indexOf('dvbt') >= 0 ? 'dvbt' : 'dvbt2');
    await api('POST', '/api/boot-default', {
        type: 'dvbt',
        freq_hz: Math.round(parseFloat(f) * 1e6),
        modulation: 't' + bw + std,
        program: (progSel && progSel.value) ? progSel.value : null
    });
    await loadDvbtBootDefault();
}

async function loadDvbtBootDefault() {
    var note = document.getElementById('dvbt-boot-note');
    if (!note) { return; }
    try {
        var d = await api('GET', '/api/boot-default');
        if (d && d.type === 'dvbt' && d.freq_hz) {
            note.textContent = 'Default boot: '
                + (d.freq_hz / 1e6).toFixed(3) + ' MHz';
        } else {
            note.textContent = '';
        }
    } catch (e) { note.textContent = ''; }
}

async function loadDvbtPresets() {'''


OLD_INIT = '''    await loadDvbtPrograms();
    await loadDvbtPresets();
}'''

NEW_INIT = '''    await loadDvbtPrograms();
    await loadDvbtPresets();
    await loadDvbtBootDefault();
}'''


# --- 6. the boot-default endpoint and model ------------------------------

OLD_ENDPOINT = '''def set_boot_default(req: DefaultBootRequest):
    config['default_boot_preset'] = {
        "freq": req.freq, "sr": req.sr, "plug": req.plug, "lnb_lo_khz": req.lnb_lo_khz
    }'''

NEW_ENDPOINT = '''def set_boot_default(req: DefaultBootRequest):
    # A type, because the fallback can now be either kind of tuner and
    # the resume has to tell them apart. Absent means "rf", so an
    # existing config file keeps working untouched.
    if req.type == "dvbt":
        config['default_boot_preset'] = {
            "type": "dvbt",
            "freq_hz": req.freq_hz,
            "modulation": req.modulation,
            "program": req.program,
        }
        save_config(config)
        return {"success": True,
                "default_boot_preset": config['default_boot_preset']}
    config['default_boot_preset'] = {
        "freq": req.freq, "sr": req.sr, "plug": req.plug, "lnb_lo_khz": req.lnb_lo_khz
    }'''


OLD_BOOTMODEL = '''class PresetSaveRequest(BaseModel):'''

NEW_BOOTMODEL = '''class _DvbtBootFields(BaseModel):
    """Not used directly - see DefaultBootRequest's own dvbt fields."""


class PresetSaveRequest(BaseModel):'''


# --- 7. the startup fallback learns the type -----------------------------

OLD_FALLBACK = '''        default_preset = config.get('default_boot_preset')
        if default_preset:'''

NEW_FALLBACK = '''        default_preset = config.get('default_boot_preset')
        if default_preset and default_preset.get('type') == 'dvbt':
            # A DVB-T2 fallback. Handed to _resume_tune() it would ask
            # a Picotuner - which this receiver may not even have - for
            # a frequency in the wrong unit.
            print(f"No previous state - using default boot preset: "
                  f"{default_preset.get('freq_hz', 0)/1e6:.3f} MHz DVB-T2")
            try:
                tune_dvbt(DvbtTuneRequest(
                    freq=default_preset["freq_hz"],
                    modulation=default_preset.get("modulation", "t8dvbt2"),
                    program=default_preset.get("program"),
                ))
            except Exception as e:
                print(f"Could not apply default boot preset: "
                      f"{type(e).__name__}: {e}")
        elif default_preset:'''


EDITS = [
    ("form layout", OLD_FORM, NEW_FORM),
    ("help text", OLD_HELP, NEW_HELP),
    ("tune retry", OLD_TUNE, NEW_TUNE),
    ("service switch", OLD_SWITCH, NEW_SWITCH),
    ("save memory", OLD_SAVEMEM, NEW_SAVEMEM),
    ("remember lock mode", OLD_REMEMBER, NEW_REMEMBER),
    ("boot default fns", OLD_BOOTFN, NEW_BOOTFN),
    ("load after tune", OLD_INIT, NEW_INIT),
    ("boot endpoint", OLD_ENDPOINT, NEW_ENDPOINT),
    ("startup fallback", OLD_FALLBACK, NEW_FALLBACK),
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

    if "saveDvbtMemory" not in text:
        print("ERROR: patch_hdhomerun_presets.py has not been applied.")
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

    # DefaultBootRequest needs the dvbt fields. Reported rather than
    # patched blind, because its definition has not been seen.
    if "class DefaultBootRequest" in text:
        seg = text.split("class DefaultBootRequest", 1)[1][:600]
        if "freq_hz" not in seg:
            print("\n  NOTE: DefaultBootRequest has no freq_hz/modulation/"
                  "program/type fields.")
            print("  Add them before using the boot-default button:")
            print("      type: str = \"rf\"")
            print("      freq_hz: Optional[int] = None")
            print("      modulation: Optional[str] = None")
            print("      program: Optional[str] = None")

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
    backup = APP.with_suffix(APP.suffix + f".bak-card2-{stamp}")
    shutil.copy2(APP, backup)
    APP.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {APP} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
