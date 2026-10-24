#!/usr/bin/env python3
"""
patch_hdhr_after_atsc3.py

What patch_hdhr_two_boxes.py and patch_hdhr_card_tidy.py added that the
atsc3 branch did not already have, re-made on top of atsc3's own two-box
code (16 Oct 2026: routing by /sys/features, the other box released, an
ATSC 3.0 panel and tuning card).

  1. "The HDHomeRun" means the box in use. atsc3 still took the lowest
     device ID as the default, and 10C096F5 (the US ATSC box) sorts
     first, so the DVB-T2 re-lock restart watched the US box and the
     DVB-T2 Pathfinder read it. Now: the box last tuned, else the
     preferred one, else the first DVB box.
  2. Names. From what each box receives ("HDHomeRun DVB-T2",
     "HDHomeRun ATSC 3.0"), editable on the config page, applied at once.
  3. The config card lists each box (name, model kind, address, in use),
     is titled "HDHomeRun Tuners", and its Save button really saves:
     /api/config had no hdhomerun section, so the address was dropped
     while the page said "Saved".

Run from the repo root:  python3 patch_hdhr_after_atsc3.py
Then:                    python3 verify.py lynx_app.py
"""

import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


def apply(src, old, new, label, count=1):
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


OLD_DEFAULT = '''    preferred = (config.get('hdhomerun') or {}).get('prefer_device_id')
    with hdhr_lock:
        if preferred and preferred in hdhr_states:
            return preferred
        for device_id in sorted(hdhr_states):
            return device_id
    return None
'''

NEW_DEFAULT = '''    # The box in use first: see patch_hdhr_after_atsc3.py. The lowest ID
    # alone made the US ATSC box "the HDHomeRun" for the DVB-T2 re-lock
    # restart and Pathfinder, whichever box was playing.
    preferred = ((config.get('hdhomerun') or {}).get('prefer_device_id') or "").upper()
    with hdhr_lock:
        if hdhr_active_device and hdhr_active_device in hdhr_states:
            return hdhr_active_device
        if preferred and preferred in hdhr_states:
            return preferred
        ids = sorted(hdhr_states)
    # Outside the lock: asking a box its kind takes the lock itself.
    for device_id in ids:
        if hdhr_device_kind(device_id) == "dvb":
            return device_id
    return ids[0] if ids else None
'''

NAMES = '''def _hdhr_apply_names():
    """Each box's name: the one given on the config page, else one from
    what it receives. Two boxes with the same default get the end of
    their ID added. See patch_hdhr_after_atsc3.py."""
    given = (config.get('hdhomerun') or {}).get('names') or {}
    kinds = {d["device_id"]: hdhr_device_kind(d["device_id"]) for d in hdhr_devices()}
    defaults = {did: ("HDHomeRun ATSC 3.0" if k == "atsc" else "HDHomeRun DVB-T2")
                for did, k in kinds.items()}
    with hdhr_lock:
        for did, st in hdhr_states.items():
            d = defaults.get(did, f"HDHomeRun {did}")
            if list(defaults.values()).count(d) > 1:
                d = f"{d} {did[-4:]}"
            st["default_name"] = d
            st["name"] = (given.get(did) or "").strip() or d


def hdhr_local_address(device_address: str) -> str:'''

CFG_MODEL = '''class HdhomerunConfigUpdate(BaseModel):
    """The HDHomeRun card on the config page. There was no such section
    before, so its Save button saved nothing (patch_hdhr_after_atsc3.py)."""
    address: str = ""
    names: dict = {}           # device ID -> friendly name; empty = default
    prefer_device_id: Optional[str] = None   # None = leave as it is


class ConfigUpdateRequest(BaseModel):
'''

CFG_SAVE = '''        if req.hdhomerun is not None:
            h = on_disk.setdefault('hdhomerun', {})
            h['address'] = (req.hdhomerun.address or "").strip()
            names = {str(k).upper(): str(v).strip()
                     for k, v in (req.hdhomerun.names or {}).items()
                     if v is not None and str(v).strip()}
            if names:
                h['names'] = names
            else:
                h.pop('names', None)
            if req.hdhomerun.prefer_device_id is not None:
                pref = req.hdhomerun.prefer_device_id.strip().upper()
                if pref:
                    h['prefer_device_id'] = pref
                else:
                    h.pop('prefer_device_id', None)
            # Names apply now; only a changed address waits for a restart.
            config.setdefault('hdhomerun', {})['names'] = names
            _hdhr_apply_names()
        if req.remote_source is not None:
'''

CARD_JS_OLD = '''                    el.textContent = devs.map(function (d) {
                        return d.device_id + ' at ' + (d.address || '?');
                    }).join(', ');
'''

CARD_JS_NEW = '''                    // One row per box: its name, editable, in ordinary
                    // text, and what it is. Left at the default, a name
                    // saves as no name given. See patch_hdhr_after_atsc3.py.
                    const given = (hdhrCfg.names || {});
                    el.textContent = '';
                    devs.forEach(function (d) {
                        const row = document.createElement('div');
                        row.className = 'mb-2';
                        const inp = document.createElement('input');
                        inp.type = 'text';
                        inp.className = 'form-control form-control-sm';
                        inp.id = 'hdhr-name-' + d.device_id;
                        inp.dataset.def = d.default_name || '';
                        inp.value = given[d.device_id] || d.default_name || d.name || '';
                        const info = document.createElement('div');
                        info.className = 'small text-muted';
                        info.textContent = (d.kind === 'atsc' ? 'ATSC / ATSC 3.0' : 'DVB-T / T2 / C')
                            + ' - ' + d.device_id + ' at ' + (d.address || '?')
                            + (d.online ? '' : ' - offline') + (d.active ? ' - in use' : '');
                        row.appendChild(inp);
                        row.appendChild(info);
                        el.appendChild(row);
                    });
'''


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    src = TARGET.read_text()
    if "def hdhr_device_kind" not in src:
        sys.exit("ABORT: the atsc3 branch has not been merged")
    if "def _hdhr_apply_names" in src:
        sys.exit("ABORT: already patched")

    src = apply(src, OLD_DEFAULT, NEW_DEFAULT, "default: the box in use, else a DVB box")
    src = apply(src, "\ndef hdhr_local_address(device_address: str) -> str:", "\n" + NAMES, "names")
    src = apply(src,
                "    if count:\n        for st in hdhr_devices():\n"
                "            print(f\"[hdhr] found {st['device_id']} at {st['address']}\")\n",
                "    _hdhr_apply_names()\n"
                "    if count:\n        for st in hdhr_devices():\n"
                "            print(f\"[hdhr] found {st['name']} {st['device_id']} at {st['address']}\")\n",
                "discovery: name the boxes")
    src = apply(src, '        "device_id": d["device_id"],\n        "kind": hdhr_device_kind(d["device_id"]),\n',
                '        "device_id": d["device_id"],\n        "name": d.get("name", ""),\n'
                '        "kind": hdhr_device_kind(d["device_id"]),\n',
                "status: the name")
    src = apply(src, "def hdhomerun_status():\n    return {\n",
                "def hdhomerun_status():\n    _active = hdhr_default_device_id()\n    return {\n",
                "api: which box is in use")
    src = apply(src, '                "device_id": st["device_id"],\n                "name": st["name"],\n',
                '                "device_id": st["device_id"],\n                "name": st["name"],\n'
                '                "default_name": st.get("default_name", st["name"]),\n'
                '                "kind": hdhr_device_kind(st["device_id"]),\n'
                '                "active": (current_mode == "dvbt" and st["device_id"] == _active),\n',
                "api: names, kind, in use")

    src = apply(src, "class ConfigUpdateRequest(BaseModel):\n", CFG_MODEL, "config: model")
    src = apply(src, "    remote_source: Optional[RemoteSourceConfigUpdate] = None\n",
                "    remote_source: Optional[RemoteSourceConfigUpdate] = None\n"
                "    hdhomerun: Optional[HdhomerunConfigUpdate] = None\n", "config: field")
    src = apply(src, "        if req.remote_source is not None:\n", CFG_SAVE, "config: save")

    src = apply(src, '<div class="card-header">&#x1F4FA; DVB-T2 Tuner (HDHomeRun)</div>',
                '<div class="card-header">&#x1F4FA; HDHomeRun Tuners</div>', "card title")
    src = apply(src,
                "                            A SiliconDust HDHomeRun on the network, used as a\n"
                "                            DVB-T/T2/C receiver. Found automatically on the\n"
                "                            same subnet - nothing needs configuring for one\n"
                "                            plugged into the local network.\n",
                "                            SiliconDust HDHomeRuns on the network: DVB-T/T2/C\n"
                "                            boxes, and ATSC boxes. Found automatically on the\n"
                "                            same subnet, and each tune goes to a box that can\n"
                "                            receive it. Name them here if you like.\n",
                "card description")
    src = apply(src, CARD_JS_OLD, CARD_JS_NEW, "card: a row per box")
    src = apply(src,
                "            address: document.getElementById('hdhr-address-input').value.trim()\n"
                "        }};\n",
                "            address: document.getElementById('hdhr-address-input').value.trim(),\n"
                "            names: (function () {\n"
                "                const n = {};\n"
                "                document.querySelectorAll('[id^=\"hdhr-name-\"]').forEach(function (i) {\n"
                "                    const v = i.value.trim();\n"
                "                    n[i.id.slice(10)] = (v === (i.dataset.def || '')) ? '' : v;\n"
                "                });\n"
                "                return n;\n"
                "            })()\n"
                "        }};\n",
                "card: save the names")
    src = apply(src,
                "        // plugged in and out, so there is no reason to poll for one.\n"
                "        statusEl.textContent = 'Saved - restart required.';\n",
                "        // plugged in and out, so there is no reason to poll for one.\n"
                "        statusEl.textContent = 'Saved. Names apply now; a new address needs a restart.';\n",
                "card: honest save message")

    TARGET.write_text(src)
    print("lynx_app.py patched")


if __name__ == "__main__":
    main()
