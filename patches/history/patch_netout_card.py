#!/usr/bin/env python3
"""
patch_netout_card.py — the network output card, top of column 3.

A switch, the address of the machine that will show the picture, and
the URL to paste into VLC there. Sits above Control on the receiver
page because it changes what the receiver does rather than how it is
configured - the operator reaches for it during a contact, not when
setting the thing up.

The warning text matters as much as the switch. Turning this on takes
the picture off the local screen entirely, which is alarming if it is
not what you expected, so the card says so plainly before and after.

Usage
-----
    python3 patch_netout_card.py            # dry run
    python3 patch_netout_card.py --apply
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
MARKER = "netout-toggle"


OLD_CARD = '''        <div class="col-md-4">
            <div class="card">
                <div class="card-header">&#x2699;&#xFE0F; Control</div>'''

NEW_CARD = '''        <div class="col-md-4">
            <!-- Network video output. First in this column deliberately:
                 it changes what the receiver is doing right now, which
                 is what an operator reaches for mid-contact. -->
            <div class="card mb-3">
                <div class="card-header">&#x1F4FA; Picture to another screen</div>
                <div class="card-body">
                    <div class="form-check form-switch mb-2">
                        <input class="form-check-input" type="checkbox" role="switch"
                               id="netout-toggle" onchange="setNetworkOutput(this.checked)"
                               style="transform:scale(1.4); margin-right:0.6em;">
                        <label class="form-check-label" for="netout-toggle"
                               style="font-size:1.05em">Send picture over the network</label>
                    </div>
                    <p class="text-muted small mb-2">
                        Nothing is decoded here: the transport stream goes to the
                        machine below and VLC draws the picture there. This screen
                        keeps the overlay &mdash; frequency, MER, callsign, magic eye
                        &mdash; over a dark background.
                    </p>
                    <p class="text-muted small mb-2">
                        Worth using for 4K, which a Pi&nbsp;5 decodes only just and a
                        Pi&nbsp;4 not at all, while almost any television, Apple TV or
                        Fire Stick has a hardware decoder sitting idle.
                    </p>
                    <label class="small text-muted mb-1" for="netout-host">Machine to send to</label>
                    <div class="input-group input-group-sm mb-1">
                        <input type="text" class="form-control" id="netout-host"
                               placeholder="192.168.1.50" onchange="saveNetworkOutputHost()">
                        <span class="input-group-text">:</span>
                        <input type="number" class="form-control" id="netout-port"
                               style="max-width:6em" value="9950" onchange="saveNetworkOutputHost()">
                    </div>
                    <div class="small mb-2" id="netout-subnet-warning" style="display:none; color:#e8a33d;">
                        That address is on a different subnet from this receiver. It
                        may still work if your network routes between them &mdash; but
                        if nothing appears, that is the first thing to check.
                    </div>
                    <label class="small text-muted mb-1">Open this in VLC</label>
                    <div class="input-group input-group-sm mb-1">
                        <input type="text" class="form-control" id="netout-url"
                               readonly value="udp://@:9950">
                        <button class="btn btn-outline-light" onclick="copyNetworkOutputUrl()"
                                title="Copy, then paste into VLC's File > Open Network">Copy</button>
                    </div>
                    <div class="text-muted small">
                        In VLC: File &rarr; Open Network, and paste. The
                        <code>@</code> tells it to listen rather than send.
                    </div>
                </div>
            </div>

            <div class="card">
                <div class="card-header">&#x2699;&#xFE0F; Control</div>'''


# --- the script ------------------------------------------------------

OLD_JS = """async function shutdownPi() {"""

NEW_JS = """        // ── Network video output ──────────────────────────────
//
// The host and port are saved to config, the switch is not:
// where the picture goes is a property of the installation,
// whether it is going there right now is not, and a receiver
// that came back from a power cut with its own screen dark
// would be a puzzle rather than a convenience.
async function setNetworkOutput(on) {
    try {
        const r = await api('POST', '/api/network_output', {enabled: !!on});
        if (r && r.vlc_url) {
            document.getElementById('netout-url').value = r.vlc_url;
        }
        showToast(on
            ? 'Picture now going to ' + (r.target || 'the network')
            : 'Picture back on this screen');
    } catch (e) {
        showToast('Could not change network output: ' + e, true);
        document.getElementById('netout-toggle').checked = !on;
    }
}

async function saveNetworkOutputHost() {
    const host = document.getElementById('netout-host').value.trim();
    const port = parseInt(document.getElementById('netout-port').value, 10) || 9950;
    document.getElementById('netout-url').value = 'udp://@:' + port;
    checkNetworkOutputSubnet(host);
    try {
        await api('POST', '/api/config', {network_output: {host: host, port: port}});
    } catch (e) {
        showToast('Could not save: ' + e, true);
    }
}

// A /24 comparison rather than anything cleverer: the browser
// cannot see the receiver's netmask, and on the networks this
// runs on a /24 is right often enough to be a useful warning
// and never a refusal. Routed networks exist, and telling
// somebody their working setup is impossible would be worse
// than saying nothing.
function checkNetworkOutputSubnet(host) {
    const warn = document.getElementById('netout-subnet-warning');
    if (!warn) return;
    const here = window.location.hostname;
    const a = (host || '').split('.');
    const b = (here || '').split('.');
    const looksLikeIp = a.length === 4 && b.length === 4 && !isNaN(+b[0]);
    warn.style.display = (looksLikeIp &&
                          (a[0] !== b[0] || a[1] !== b[1] || a[2] !== b[2]))
                         ? 'block' : 'none';
}

function copyNetworkOutputUrl() {
    const el = document.getElementById('netout-url');
    el.select();
    el.setSelectionRange(0, 99999);
    navigator.clipboard.writeText(el.value).then(
        () => showToast('Copied - paste into VLC'),
        () => showToast('Select and copy it by hand', true));
}

async function shutdownPi() {"""


EDITS = [
    ("card", OLD_CARD, NEW_CARD),
    ("script", OLD_JS, NEW_JS),
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
            print(f"  {name:<8} OK   one match")
        else:
            print(f"  {name:<8} FAIL {n} matches - expected 1")
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
        print("\n  syntax  OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"\n  syntax  FAIL {e}")
        return 1
    finally:
        tp.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(TARGET, TARGET.with_suffix(f".py.bak-netcard-{stamp}"))
    TARGET.write_text(patched, encoding="utf-8")
    print(f"\n  {TARGET} patched")
    print("\nNow run:  python3 verify.py")
    print("\nThe card reads its host and port from /api/status on load -")
    print("if those fields come up blank, the status payload needs the")
    print("network_output host and port adding alongside the flag.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
