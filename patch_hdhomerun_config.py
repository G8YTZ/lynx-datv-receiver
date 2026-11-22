#!/usr/bin/env python3
"""
patch_hdhomerun_config.py — manual HDHomeRun address on the Config page.

Discovery is a UDP broadcast, so it stops at the first router. A tuner
on another subnet - at a repeater site reached over an SD-WAN, say - is
perfectly usable once found, because everything after discovery is
ordinary unicast, and the stream target is already computed from the
routing table rather than assumed. It just cannot be found by asking
the local segment.

So: a Config card showing what was discovered, a field to name a device
that was not, and a note saying why.

What it adds
------------
1. A card on the Config page under Slave Rx, showing the discovered
   address and offering an override.
2. hdhomerun.address in the config, queried directly at startup via
   /discover.json - no broadcast involved, so it crosses routers.
3. GET /api/hdhomerun already reports the discovered address, which the
   form reads to show what was found.

The override is additive, not exclusive: a configured device is added
to whatever discovery found rather than replacing it, since a receiver
may reasonably have one of each.

Touches lynx_app.py only, including its HTML and JavaScript, so run
verify.py and check the browser console afterwards.

Usage
-----
    python3 patch_hdhomerun_config.py            # dry run
    python3 patch_hdhomerun_config.py --apply    # back up and write
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
MARKER = "hdhr-address-input"


# --- 1. query a configured address at startup ----------------------------

OLD_DISC = '''    if count:
        for st in hdhr_devices():
            print(f"[hdhr] found {st['device_id']} at {st['address']}")
    return count'''

NEW_DISC = '''    # A configured address, asked directly. Discovery is a broadcast
    # and stops at the first router, so a device on another subnet can
    # never answer it - but it is perfectly reachable once named, and
    # everything after this point is ordinary unicast.
    #
    # Additive rather than exclusive: a receiver may have one tuner on
    # the local segment and another at a site across a link, and there
    # is no reason to make it choose.
    _manual = (config.get('hdhomerun') or {}).get('address', '')
    if _manual:
        try:
            info = lynx_hdhomerun.discover_http(_manual)
            _did = (info.get('DeviceID') or '').upper()
            if _did:
                with hdhr_lock:
                    st = hdhr_states.setdefault(_did, {
                        "device_id": _did,
                        "name": info.get('FriendlyName') or f"HDHomeRun {_did}",
                        "tuner": 0,
                        "address": _manual,
                        "online": False,
                        "locked": False,
                        "streaming": False,
                        "last_seen": 0.0,
                        "last_error": "",
                        **{field: None for field in HDHR_QUALITY_FIELDS},
                    })
                    # A configured address wins over a discovered one
                    # for the same device: somebody typed it, which is
                    # a stronger statement than a broadcast reply.
                    st["address"] = _manual
                    st["configured"] = True
                count = len(hdhr_states)
        except lynx_hdhomerun.HDHomeRunError as e:
            print(f"[hdhr] configured address {_manual}: {e}")

    if count:
        for st in hdhr_devices():
            print(f"[hdhr] found {st['device_id']} at {st['address']}")
    return count'''


# --- 2. the Config card ---------------------------------------------------

OLD_CARD = '''                <div class="card mb-3">
                    <div class="card-header">&#x1F4E1; Slave Rx (remote receiver)</div>'''

NEW_CARD = '''                <div class="card mb-3">
                    <div class="card-header">&#x1F4FA; DVB-T2 Tuner (HDHomeRun)</div>
                    <div class="card-body">
                        <p class="text-muted small">
                            A SiliconDust HDHomeRun on the network, used as a
                            DVB-T/T2/C receiver. Found automatically on the
                            same subnet - nothing needs configuring for one
                            plugged into the local network.
                        </p>
                        <div class="alert alert-warning py-2 small mb-3">
                            Auto-discovery is a broadcast, so it does not cross
                            a router. A tuner on another subnet - at a repeater
                            site, say - works perfectly once its address is
                            given here, but will never be found on its own.
                        </div>
                        <label class="small">Discovered</label>
                        <div class="mb-3">
                            <span class="status-value" id="hdhr-discovered">-</span>
                        </div>
                        <label class="small">Address (optional)</label>
                        <input type="text" class="form-control mb-2"
                               id="hdhr-address-input"
                               placeholder="e.g. 10.20.30.40">
                        <p class="text-muted small">
                            Leave empty unless the tuner is on another subnet.
                            An address given here is asked directly rather than
                            broadcast for, and is used in addition to anything
                            discovered locally.
                        </p>
                        <div class="mt-3 d-flex align-items-center gap-2">
                            <button class="btn btn-save" onclick="saveHdhr()">Save HDHomeRun settings</button>
                            <span class="save-status" id="hdhr-status"></span>
                        </div>
                    </div>
                </div>
                <div class="card mb-3">
                    <div class="card-header">&#x1F4E1; Slave Rx (remote receiver)</div>'''


# --- 3. load the values into the form ------------------------------------

OLD_LOAD = '''        const rs = cfg.remote_source || {};
        document.getElementById('rs-enabled').checked = rs.enabled === true;'''

NEW_LOAD = '''        const hdhrCfg = cfg.hdhomerun || {};
        const hdhrAddr = document.getElementById('hdhr-address-input');
        if (hdhrAddr) { hdhrAddr.value = hdhrCfg.address || ''; }
        // What was actually found, from the live endpoint rather than
        // the config: the whole point of the field is to cover the case
        // where discovery found nothing, so showing the configured
        // value back as "discovered" would be worse than useless.
        try {
            const hr = await fetch('/api/hdhomerun');
            const hd = await hr.json();
            const el = document.getElementById('hdhr-discovered');
            if (el) {
                const devs = (hd && hd.devices) || [];
                if (!devs.length) {
                    el.textContent = 'none found';
                } else {
                    el.textContent = devs.map(function (d) {
                        return d.device_id + ' at ' + (d.address || '?');
                    }).join(', ');
                }
            }
        } catch (e) { /* the card still works without it */ }

        const rs = cfg.remote_source || {};
        document.getElementById('rs-enabled').checked = rs.enabled === true;'''


# --- 4. save ---------------------------------------------------------------

OLD_SAVE = '''async function saveRemoteSource() {'''

NEW_SAVE = '''async function saveHdhr() {
    const statusEl = document.getElementById('hdhr-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const body = { hdhomerun: {
            address: document.getElementById('hdhr-address-input').value.trim()
        }};
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        // Restart required: the address is read once, during the single
        // discovery sweep at startup. A device is installed rather than
        // plugged in and out, so there is no reason to poll for one.
        statusEl.textContent = 'Saved - restart required.';
        statusEl.className = 'save-status text-success';
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function saveRemoteSource() {'''


EDITS = [
    ("startup query", OLD_DISC, NEW_DISC),
    ("config card", OLD_CARD, NEW_CARD),
    ("form load", OLD_LOAD, NEW_LOAD),
    ("save function", OLD_SAVE, NEW_SAVE),
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
        print(f"{APP} already patched - nothing to do.")
        return 0

    print(f"\nchecking anchors in {APP}\n")
    ok = True
    for name, anchor, _new in EDITS:
        count = text.count(anchor)
        if count == 1:
            print(f"  {name:<18} OK   one match")
        else:
            print(f"  {name:<18} FAIL {count} matches - expected exactly 1")
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
        print("\n  syntax check       OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"\n  syntax check       FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tmp_path.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = APP.with_suffix(APP.suffix + f".bak-hdhrcfg-{stamp}")
    shutil.copy2(APP, backup)
    APP.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {APP} patched")
    print("\nNow run:  python3 verify.py")
    print("Then open the Config page with the browser console visible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
