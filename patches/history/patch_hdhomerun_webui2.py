#!/usr/bin/env python3
"""
patch_hdhomerun_webui2.py — DVB-T2 panel and manual tuning card.

Second attempt. The first put the whole thing in at once, including a
preset list built by concatenating onclick="" strings with nested
quotes. That parsed cleanly - verify.py was happy, and so was Python -
and then threw at runtime, which left every panel on the page stuck on
"Loading...". A block that parses is not a block that runs.

So this one leaves presets out entirely. Panel, card, manual tune, and
nothing that needs quote-escaping inside generated HTML. Presets follow
separately, built with createElement and addEventListener rather than
string concatenation, so there is nothing to escape in the first place.

What it adds
------------
1. A source panel in the left column, between Tuner Rx 2 and Stream,
   using setPanelState() so its header badge behaves like the others.
2. A manual tuning card in the middle column below the Picotuner's.
3. tuneDvbt() and tuneDvbtManual(), using the existing api() helper.

Both hidden when no HDHomeRun was discovered, on the same principle as
the Slave panels: a receiver without one should not carry a permanently
red panel for hardware it does not have.

Frequency is entered in MHz and sent in Hz - the device's API works in
Hz and nobody types nine digits - converted in one place at the form.

Usage
-----
    python3 patch_hdhomerun_webui2.py            # dry run
    python3 patch_hdhomerun_webui2.py --apply    # back up and write

Then run verify.py, restart, and open the page with the browser console
visible. A clean console is the test this patch actually needs to pass.
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
MARKER = "dvbt-panel"


# --- 1. left column panel, between Tuner Rx 2 and Stream ------------------

OLD_PANEL = '''            <div class="card mt-2" id="tri-watch-stream-panel" style="display:none">'''

NEW_PANEL = '''            <!-- DVB-T2 (HDHomeRun): a network tuner, so it may not be
                 there at all. Hidden rather than shown red when none
                 was discovered, the same as the Slave panels - a
                 receiver without one should not carry a permanent
                 fault indication for hardware it does not have. -->
            <div class="card mt-2" id="dvbt-panel" style="display:none">
                <div class="card-header" id="dvbt-header">&#x1F4FA; DVB-T2</div>
                <div class="card-body" id="dvbt-status"></div>
            </div>
            <div class="card mt-2" id="tri-watch-stream-panel" style="display:none">'''


# --- 2. tuning card -------------------------------------------------------

OLD_CARD = '''            <!-- Slave Receivers -->'''

NEW_CARD = '''            <!-- DVB-T2 Reception (HDHomeRun) -->
            <div class="card mt-3" id="dvbt-tune-card" style="display:none">
                <div class="card-header">&#x1F4FA; DVB-T2 Reception (HDHomeRun)</div>
                <div class="card-body">
                    <h6 class="text-muted">Manual Tune</h6>
                    <div class="row g-2 mb-2">
                        <div class="col-7">
                            <input type="number" step="0.001" class="form-control"
                                   id="dvbt-freq" placeholder="MHz" value="146.500">
                        </div>
                        <div class="col-5">
                            <select class="form-select" id="dvbt-bw">
                                <option value="1">1 MHz</option>
                                <option value="2">2 MHz</option>
                                <option value="3">3 MHz</option>
                                <option value="4">4 MHz</option>
                                <option value="5">5 MHz</option>
                                <option value="6">6 MHz</option>
                                <option value="7">7 MHz</option>
                                <option value="8" selected>8 MHz</option>
                            </select>
                        </div>
                    </div>
                    <div class="row g-2 mb-2">
                        <div class="col-7">
                            <select class="form-select" id="dvbt-std">
                                <option value="dvbt2" selected>DVB-T2</option>
                                <option value="dvbt">DVB-T</option>
                            </select>
                        </div>
                        <div class="col-5">
                            <input type="number" class="form-control"
                                   id="dvbt-program" placeholder="Program" value="1">
                        </div>
                    </div>
                    <button class="btn btn-danger w-100"
                            onclick="tuneDvbtManual()">Tune</button>
                    <div class="text-muted small mt-2">
                        Frequency in MHz. Bandwidth is the channel width,
                        not the bitrate - 1 and 2 MHz are the narrowband
                        amateur modes, 7 and 8 MHz are broadcast.
                    </div>
                </div>
            </div>

            <!-- Slave Receivers -->'''


# --- 3. panel rendering ---------------------------------------------------

OLD_RENDER = '''        const remotes = s.remotes || [];
        const remoteHost = document.getElementById('remote-panels');'''

NEW_RENDER = '''        // DVB-T2 (HDHomeRun). Looked up both ways: the status payload
        // nests some blocks under "lynx" and exposes others at the top
        // level, and this renderer is called with both shapes.
        const hh = (s.lynx && s.lynx.hdhomerun) || s.hdhomerun || null;
        const si = (s.lynx && s.lynx.stream_info) || s.stream_info || {};
        const dvbtPanel = document.getElementById('dvbt-panel');
        const dvbtCard = document.getElementById('dvbt-tune-card');
        if (dvbtPanel) {
            if (!hh) {
                dvbtPanel.style.display = 'none';
                if (dvbtCard) { dvbtCard.style.display = 'none'; }
            } else {
                dvbtPanel.style.display = '';
                if (dvbtCard) { dvbtCard.style.display = ''; }
                var dstate = 'offline';
                if (hh.online) { dstate = hh.locked ? 'locked' : 'idle'; }
                setPanelState('dvbt-header', 'dvbt-status',
                              '&#x1F4FA; DVB-T2', dstate);
                var dbody = document.getElementById('dvbt-status');
                if (dbody) {
                    if (!hh.online) {
                        dbody.innerHTML = '<div class="text-danger small text-center mt-2">HDHomeRun offline</div>';
                    } else if (hh.locked) {
                        // t8dvbt2 gives "8 MHz" and "DVB-T2". The second
                        // character is the nominal bandwidth in MHz and
                        // the ladder is exact, so 1 really does mean 1.
                        var lm = hh.lock_mode || '';
                        var dbw = lm.charAt(1);
                        var dstd = '-';
                        if (lm.indexOf('dvbt2') >= 0) { dstd = 'DVB-T2'; }
                        else if (lm.indexOf('dvbt') >= 0) { dstd = 'DVB-T'; }
                        var drows = [
                            ['Callsign', hh.callsign || '-'],
                            ['Service', hh.service_name || '-'],
                            ['Frequency', hh.frequency_hz ? (hh.frequency_hz / 1e6).toFixed(3) + ' MHz' : '-'],
                            ['Bandwidth', dbw ? dbw + ' MHz' : '-'],
                            ['Standard', dstd],
                            ['SNQ', (hh.snq !== null && hh.snq !== undefined) ? hh.snq + ' %' : '-'],
                            ['SEQ', (hh.seq !== null && hh.seq !== undefined) ? hh.seq + ' %' : '-'],
                            ['Level', (hh.level !== null && hh.level !== undefined) ? hh.level + ' %' : '-'],
                            ['Bitrate', hh.bitrate_bps ? (hh.bitrate_bps / 1e6).toFixed(2) + ' Mb/s' : '-'],
                            ['Codec', si.video_codec || '-'],
                            ['Audio Codec', si.audio_codec || '-']
                        ];
                        var dhtml = '';
                        for (var di = 0; di < drows.length; di++) {
                            dhtml += '<div class="d-flex justify-content-between mb-1" style="flex-wrap:wrap; gap: 4px 12px;">';
                            dhtml += '<span>' + drows[di][0] + '</span>';
                            dhtml += '<span class="status-value">' + drows[di][1] + '</span></div>';
                        }
                        dbody.innerHTML = dhtml;
                    } else {
                        dbody.innerHTML = '<div class="text-muted small text-center mt-2">Searching for signal...</div>';
                    }
                }
            }
        }

        const remotes = s.remotes || [];
        const remoteHost = document.getElementById('remote-panels');'''


# --- 4. tune functions ----------------------------------------------------

OLD_FN = '''async function tunePreset(name) {'''

NEW_FN = '''async function tuneDvbt(freqMhz, modulation, program) {
    // MHz in the form, Hz on the wire: the device's own API works in
    // Hz and nobody wants to type nine digits. Converted here, in one
    // place, rather than at each caller.
    await api('POST', '/api/tune_dvbt', {
        freq: Math.round(parseFloat(freqMhz) * 1e6),
        modulation: modulation,
        program: program ? String(program) : null
    });
}

async function tuneDvbtManual() {
    var f = document.getElementById('dvbt-freq').value;
    var bw = document.getElementById('dvbt-bw').value;
    var std = document.getElementById('dvbt-std').value;
    var prog = document.getElementById('dvbt-program').value;
    if (!f) { return; }
    await tuneDvbt(f, 't' + bw + std, prog);
}

async function tunePreset(name) {'''


EDITS = [
    ("left column panel", OLD_PANEL, NEW_PANEL),
    ("tuning card", OLD_CARD, NEW_CARD),
    ("panel rendering", OLD_RENDER, NEW_RENDER),
    ("tune functions", OLD_FN, NEW_FN),
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
    if MARKER in text:
        print(f"{APP} already contains {MARKER} - already applied.")
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
    backup = APP.with_suffix(APP.suffix + f".bak-webui2-{stamp}")
    shutil.copy2(APP, backup)
    APP.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {APP} patched")
    print("\nNow run:  python3 verify.py")
    print("Then open the page with the browser console visible - a clean")
    print("console is the test that actually matters here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
