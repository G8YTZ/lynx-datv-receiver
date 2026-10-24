#!/usr/bin/env python3
"""
patch_hdhr_two_boxes.py

Two HDHomeRuns on one network, a UK DVB-T/T2 box and a US ATSC 3.0 box,
working without any setting up.

FOUND ON 23 OCT 2026 (Lynx-DATV-Receiver-1, both boxes on the LAN)
------------------------------------------------------------------
  10C096F5  hdhomerun_dvr_atsc3  modulation: atsc3 8vsb qam256 qam64
  1250048C  hdhomerun5_dvbt      modulation: t8dvbt2 ... t1dvbt ...

Lynx knew neither box's abilities, so "the default box" was simply the
lowest device ID, and 10C096F5 sorts first. So, with both boxes on:
  - a DVB-T2 tune with no box named went to the US ATSC box;
  - the status, the overlay and the service list all followed the
    lowest ID too, so they could show the US box while the UK box was
    the one playing;
  - the re-lock restart only ever fired for the lowest ID, so a UK
    re-lock never got its fresh mpv;
  - switching boxes left the old one still streaming to the same UDP
    port as the new one;
  - and the config card's "Save HDHomeRun settings" saved nothing at
    all: /api/config has no hdhomerun section, so the address was
    silently dropped while the page said "Saved".

THE FIX
-------
  1. At discovery each box is asked its model and the modulations it
     supports (/sys/model, /sys/features), which say what it receives:
     DVB-T2, DVB-T, ATSC 3.0, ATSC, cable.
  2. A tune goes to a box that can receive it: the box named (a preset
     or the tuner selector), otherwise the box in use, otherwise the
     preferred one, otherwise the first that can. "auto" modulations
     can go anywhere.
  3. "The box in use" is the one last tuned. Status, overlay, service
     list and the re-lock restart all follow it.
  4. Changing box stops the old one streaming first.
  5. Friendly names: from the model by default ("HDHomeRun DVB-T2",
     "HDHomeRun ATSC 3.0"), editable on the config page ("UK", "US"),
     applied at once. The card lists each box with its model,
     standards and address.
  6. /api/config gains its hdhomerun section, so the address and names
     really are saved.
  7. The DVB-T card gets a Tuner selector (Auto, or a named box), saved
     with a preset. It only appears when two or more boxes can receive
     DVB-T/T2: with one UK box and one US box, Auto is always right.

Run from the repo root:  python3 patch_hdhr_two_boxes.py
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


HELPERS = '''# ── Two or more boxes (see patch_hdhr_two_boxes.py) ──────────────
hdhr_active_device = None    # the box last tuned: what "the HDHomeRun" means

HDHR_STANDARD_LABELS = {"dvbt2": "DVB-T2", "dvbt": "DVB-T", "atsc3": "ATSC 3.0",
                        "atsc": "ATSC", "cable": "Cable"}


def hdhr_standard_of(modulation):
    """What a modulation name needs a box to receive. None for the
    device's auto modes, which any box can be asked for."""
    m = (modulation or "").lower()
    if m.endswith("dvbt2"):
        return "dvbt2"
    if m.endswith("dvbt"):
        return "dvbt"
    if m == "atsc3":
        return "atsc3"
    if m == "8vsb":
        return "atsc"
    if "qam" in m:
        return "cable"
    return None


def _hdhr_read_features(target):
    """A box's model and the modulations it lists, asked directly. Both
    empty if it does not answer: capability then counts as unknown, and
    an unknown box is never ruled out."""
    model, mods = "", []
    binary = lynx_hdhomerun.HDHOMERUN_CONFIG
    try:
        model = subprocess.run([binary, target, "get", "/sys/model"],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        out = subprocess.run([binary, target, "get", "/sys/features"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.startswith("modulation:"):
                mods = line.split(":", 1)[1].split()
    except Exception as e:
        print(f"[hdhr] could not ask {target} what it receives: {e}")
    return model, mods


def _hdhr_apply_names():
    """Each box's name: the one given on the config page, else one from
    what it receives. Two boxes with the same default get the end of
    their ID added, so they can still be told apart."""
    given = (config.get('hdhomerun') or {}).get('names') or {}
    with hdhr_lock:
        defaults = {}
        for did, st in hdhr_states.items():
            std = st.get("standards") or []
            lead = next((HDHR_STANDARD_LABELS[s] for s in ("dvbt2", "atsc3", "dvbt", "atsc", "cable")
                         if s in std), "")
            defaults[did] = f"HDHomeRun {lead}".strip()
        for did, st in hdhr_states.items():
            d = defaults[did]
            if list(defaults.values()).count(d) > 1:
                d = f"{d} {did[-4:]}"
            st["default_name"] = d
            st["name"] = (given.get(did) or "").strip() or d


def hdhr_device_for(modulation=None, explicit=None):
    """Which box a tune goes to, and which box "the HDHomeRun" means.

    The box named, if it is present. Otherwise the first of: the box in
    use, the preferred box, any box, that can receive what is asked for.
    With no modulation (status, service list, start-up), "can receive"
    means DVB-T2, what the DVB-T card tunes."""
    want = hdhr_standard_of(modulation) if modulation is not None else "dvbt2"
    preferred = ((config.get('hdhomerun') or {}).get('prefer_device_id') or "").upper()
    with hdhr_lock:
        if explicit and explicit in hdhr_states:
            return explicit

        def able(did):
            stds = hdhr_states[did].get("standards")
            return (not want) or (not stds) or (want in stds)

        for did in (hdhr_active_device, preferred):
            if did and did in hdhr_states and able(did):
                return did
        for did in sorted(hdhr_states):
            if able(did):
                return did
        for did in sorted(hdhr_states):
            return did
    return None


def hdhr_default_device_id():
    """Which box "the HDHomeRun" means when none is named: the one in
    use, else the best DVB-T2 box. See hdhr_device_for()."""
    return hdhr_device_for()


'''

OLD_DEFAULT = '''def hdhr_default_device_id():
    """Which device a tune goes to when none is named.

    A configured preference wins if that device is actually present;
    otherwise the first one by ID. Returns None when nothing has been
    found, which the caller reports rather than guessing.
    """
    preferred = (config.get('hdhomerun') or {}).get('prefer_device_id')
    with hdhr_lock:
        if preferred and preferred in hdhr_states:
            return preferred
        for device_id in sorted(hdhr_states):
            return device_id
    return None


'''

DISCOVER_OLD = '''    if count:
        for st in hdhr_devices():
            print(f"[hdhr] found {st['device_id']} at {st['address']}")
    return count
'''

DISCOVER_NEW = '''    # What each box receives, asked of the box itself.
    for st in hdhr_devices():
        model, mods = _hdhr_read_features(st.get("address") or st["device_id"])
        with hdhr_lock:
            live = hdhr_states.get(st["device_id"])
            if live is not None:
                live["model"] = model
                live["modulations"] = mods
                live["standards"] = sorted({s for s in map(hdhr_standard_of, mods) if s})
    _hdhr_apply_names()

    if count:
        for st in hdhr_devices():
            stds = ", ".join(HDHR_STANDARD_LABELS[s] for s in st.get("standards") or [])
            print(f"[hdhr] found {st['name']} ({st.get('model') or '?'}: "
                  f"{stds or 'unknown'}) {st['device_id']} at {st['address']}")
    return count
'''

TUNE_SWITCH = '''    # The box in use from now on. Changing box: the old one is stopped
    # first, or both would stream to the same port at once.
    global hdhr_active_device
    if hdhr_active_device and hdhr_active_device != _dev:
        try:
            hdhr_source(hdhr_active_device).stop()
            print(f"[hdhr] changing box: stopped {hdhr_active_device}")
        except Exception as e:
            print(f"[hdhr] could not stop {hdhr_active_device}: {e}")
    hdhr_active_device = _dev
'''

CFG_MODEL = '''class HdhomerunConfigUpdate(BaseModel):
    """The HDHomeRun card on the config page. See patch_hdhr_two_boxes.py:
    there was no such section before, so its Save button saved nothing."""
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

CARD_JS_NEW = '''                    // One row per box: its name, editable, and what it
                    // is. See patch_hdhr_two_boxes.py.
                    const given = (hdhrCfg.names || {});
                    el.textContent = '';
                    devs.forEach(function (d) {
                        const row = document.createElement('div');
                        row.className = 'mb-2';
                        const inp = document.createElement('input');
                        inp.type = 'text';
                        inp.className = 'form-control form-control-sm';
                        inp.id = 'hdhr-name-' + d.device_id;
                        inp.value = given[d.device_id] || '';
                        inp.placeholder = d.default_name || d.name || d.device_id;
                        const info = document.createElement('div');
                        info.className = 'small text-muted';
                        info.textContent = (d.model || 'model unknown') + ' - '
                            + ((d.standards_text || '') || 'receives: unknown') + ' - '
                            + d.device_id + ' at ' + (d.address || '?')
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
    if "def hdhr_device_for" in src:
        sys.exit("ABORT: already patched")
    before = len(src.splitlines())

    # Capabilities, names and routing
    src = apply(src, OLD_DEFAULT, HELPERS, "routing helpers replace the lowest-ID default")
    src = apply(src, DISCOVER_OLD, DISCOVER_NEW, "discovery asks each box what it receives")

    # Tuning
    src = apply(src, "    source = hdhr_source(req.device_id)\n",
                "    # A box that can receive this - see patch_hdhr_two_boxes.py.\n"
                "    _dev = hdhr_device_for(req.modulation, req.device_id)\n"
                "    source = hdhr_source(_dev)\n",
                "tune: route by modulation")
    src = apply(src, "    _dev = req.device_id or hdhr_default_device_id()\n", TUNE_SWITCH,
                "tune: stop the old box, remember the new one")
    src = apply(src, '        "device_id": req.device_id or hdhr_default_device_id(),\n',
                '        "device_id": _dev,\n', "tune: report the box used")

    # Status follows the box in use, not the lowest ID
    src = apply(src, '            } if d else None)(next(iter(hdhr_devices()), None)),\n',
                '            } if d else None)(next((x for x in hdhr_devices()\n'
                '                                    if x["device_id"] == hdhr_default_device_id()),\n'
                '                                   None)),\n',
                "status: the box in use")
    src = apply(src, '            "hdhomerun": (lambda d: {\n                "online": d["online"],\n',
                '            "hdhomerun": (lambda d: {\n                "name": d.get("name", ""),\n'
                '                "online": d["online"],\n',
                "status: its name")

    # /api/hdhomerun
    src = apply(src, "def hdhomerun_status():\n    return {\n",
                "def hdhomerun_status():\n    _active = hdhr_default_device_id()\n    return {\n",
                "api: which box is in use")
    src = apply(src, '                "device_id": st["device_id"],\n                "name": st["name"],\n',
                '                "device_id": st["device_id"],\n                "name": st["name"],\n'
                '                "default_name": st.get("default_name", st["name"]),\n'
                '                "model": st.get("model", ""),\n'
                '                "standards": st.get("standards", []),\n'
                '                "standards_text": ", ".join(HDHR_STANDARD_LABELS[s]\n'
                '                                            for s in st.get("standards") or []),\n'
                '                "active": st["device_id"] == _active,\n',
                "api: model, standards, names")

    # /api/config hdhomerun section
    src = apply(src, "class ConfigUpdateRequest(BaseModel):\n", CFG_MODEL, "config: model")
    src = apply(src, "    remote_source: Optional[RemoteSourceConfigUpdate] = None\n",
                "    remote_source: Optional[RemoteSourceConfigUpdate] = None\n"
                "    hdhomerun: Optional[HdhomerunConfigUpdate] = None\n",
                "config: field")
    src = apply(src, "        if req.remote_source is not None:\n", CFG_SAVE, "config: save")

    # Config page card
    src = apply(src, CARD_JS_OLD, CARD_JS_NEW, "card: a row per box")
    src = apply(src,
                "            address: document.getElementById('hdhr-address-input').value.trim()\n"
                "        }};\n",
                "            address: document.getElementById('hdhr-address-input').value.trim(),\n"
                "            names: (function () {\n"
                "                const n = {};\n"
                "                document.querySelectorAll('[id^=\"hdhr-name-\"]').forEach(function (i) {\n"
                "                    n[i.id.slice(10)] = i.value.trim();\n"
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

    # Receiver page: tuner selector on the DVB-T card
    src = apply(src,
                '                    <div class="d-flex gap-2 align-items-center">\n'
                '                        <select class="form-select form-select-sm bg-dark text-light border-secondary"\n'
                '                                id="dvbt-program" onchange="switchDvbtProgram()">\n',
                '                    <div class="mb-2" id="dvbt-device-row" style="display:none">\n'
                '                        <select class="form-select form-select-sm bg-dark text-light border-secondary"\n'
                '                                id="dvbt-device" title="Which HDHomeRun. Auto picks one that can receive the tune.">\n'
                '                            <option value="">Tuner: Auto</option>\n'
                '                        </select>\n'
                '                    </div>\n'
                '                    <div class="d-flex gap-2 align-items-center">\n'
                '                        <select class="form-select form-select-sm bg-dark text-light border-secondary"\n'
                '                                id="dvbt-program" onchange="switchDvbtProgram()">\n',
                "page: tuner selector")
    src = apply(src,
                "        modulation: modulation,\n"
                "        program: program ? String(program) : null\n"
                "    });\n",
                "        modulation: modulation,\n"
                "        program: program ? String(program) : null,\n"
                "        device_id: dvbtDevice()\n"
                "    });\n",
                "page: tune with the chosen box")
    src = apply(src,
                "        modulation: 't' + bw + std,\n"
                "        program: (progSel && progSel.value) ? progSel.value : null\n"
                "    });\n"
                "    if (result && result.note === 'already saved') {\n",
                "        modulation: 't' + bw + std,\n"
                "        program: (progSel && progSel.value) ? progSel.value : null,\n"
                "        device_id: dvbtDevice()\n"
                "    });\n"
                "    if (result && result.note === 'already saved') {\n",
                "page: save the box with a preset")
    src = apply(src, "async function tuneDvbt(freqMhz, modulation, program) {\n",
                "// The Tuner selector - see patch_hdhr_two_boxes.py. Shown only when\n"
                "// two or more boxes can receive DVB-T/T2; otherwise Auto is always\n"
                "// right and the choice would only be clutter.\n"
                "function dvbtDevice() {\n"
                "    var s = document.getElementById('dvbt-device');\n"
                "    return (s && s.value) ? s.value : null;\n"
                "}\n"
                "\n"
                "async function loadDvbtDevices() {\n"
                "    try {\n"
                "        var d = await api('GET', '/api/hdhomerun');\n"
                "        var able = ((d && d.devices) || []).filter(function (x) {\n"
                "            var s = x.standards || [];\n"
                "            return !s.length || s.indexOf('dvbt2') >= 0 || s.indexOf('dvbt') >= 0;\n"
                "        });\n"
                "        var row = document.getElementById('dvbt-device-row');\n"
                "        var sel = document.getElementById('dvbt-device');\n"
                "        if (!row || !sel) { return; }\n"
                "        if (able.length < 2) { row.style.display = 'none'; sel.value = ''; return; }\n"
                "        var keep = sel.value;\n"
                "        while (sel.options.length > 1) { sel.remove(1); }\n"
                "        able.forEach(function (x) {\n"
                "            var o = document.createElement('option');\n"
                "            o.value = x.device_id;\n"
                "            o.textContent = 'Tuner: ' + (x.name || x.device_id);\n"
                "            sel.appendChild(o);\n"
                "        });\n"
                "        sel.value = keep;\n"
                "        row.style.display = '';\n"
                "    } catch (e) { /* Auto still works */ }\n"
                "}\n"
                "\n"
                "async function tuneDvbt(freqMhz, modulation, program) {\n",
                "page: selector functions")
    src = apply(src, "loadDvbtPresets();\nloadDvbtBootDefault();\n",
                "loadDvbtPresets();\nloadDvbtDevices();\nloadDvbtBootDefault();\n",
                "page: fill the selector at load")

    # Presets remember the box
    src = apply(src, "    modulation: Optional[str] = None\n    program: Optional[str] = None\n\nclass SiteConfigUpdate",
                "    modulation: Optional[str] = None\n    program: Optional[str] = None\n"
                "    device_id: Optional[str] = None   # None = Auto: any box that can receive it\n"
                "\nclass SiteConfigUpdate",
                "preset model: the box")
    src = apply(src,
                '            "modulation": req.modulation,\n'
                '            "program": req.program,\n'
                '            "note": "User saved via web UI"\n'
                '        })\n',
                '            "modulation": req.modulation,\n'
                '            "program": req.program,\n'
                '            **({"device_id": req.device_id} if req.device_id else {}),\n'
                '            "note": "User saved via web UI"\n'
                '        })\n',
                "preset save: the box, when one was chosen")

    TARGET.write_text(src)
    print(f"lynx_app.py: {before} -> {len(src.splitlines())} lines")


if __name__ == "__main__":
    main()
