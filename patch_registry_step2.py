#!/usr/bin/env python3
"""
patch_registry_step2.py — build the source registry and publish it.

Step 2 of the Lynx+ roadmap. The registry is constructed at startup
from the state dicts that already exist, and published in /api/status
under "sources" so it can be compared against the live values.

Nothing consumes it. Every existing consumer keeps its own logic
untouched, so a receiver behaves exactly as it does today whether the
flag is on or off. The flag only decides whether the extra block
appears in the status payload.

    sources:
      publish: true        # default false

The point of this step is to be able to look at what the registry
thinks before anything depends on it being right. If it reports a
frequency the OSD disagrees with, or a lock state that lags, better to
find that now than after six consumers have been moved onto it.

What it builds
--------------
  rf:a                 Picotuner tuner A
  rf:b                 Picotuner tuner B (only when diversity or
                       tri_watch actually uses it)
  slave:0, slave:1 ... one per configured Slave
  dvbt:<device>:0      one per discovered HDHomeRun, tuner 0 for now
  stream               the network stream player

Tuner 1 of an HDHomeRun is deliberately not registered yet: nothing
tunes it, and a source in the registry that cannot be selected is a
lie. The identifier carries the index so adding it later changes
nothing else.

Usage
-----
    python3 patch_registry_step2.py            # dry run
    python3 patch_registry_step2.py --apply

Then:
    python3 verify.py
    # enable it:
    #   sources:
    #     publish: true
    curl -s localhost:8080/api/status | python3 -m json.tool | less
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
MARKER = "build_source_registry"


# --- 1. import the module -------------------------------------------

OLD_IMPORT = "import lynx_hdhomerun"
NEW_IMPORT = "import lynx_hdhomerun\nimport lynx_sources"


# --- 2. build it, after the state dicts exist -----------------------

OLD_BUILD = '''@app.get("/api/status", tags=["Status"],'''

NEW_BUILD = '''# ── Source registry (Lynx+ step 2) ────────────────────────────
#
# Built from the state dicts above rather than replacing them. Nothing
# consumes it yet: every existing consumer keeps its own logic, so the
# receiver behaves identically whether this is published or not.
#
# The state dicts are passed as callables rather than references
# because several of these globals are rebound rather than mutated,
# and a reference captured here would quietly stop updating - which is
# the kind of fault that shows up as a figure frozen an hour ago.
source_registry = lynx_sources.SourceRegistry()


def build_source_registry():
    """Populate the registry from whatever this receiver actually has.

    Called after discovery and after the config is loaded, so it sees
    the real hardware rather than the possible hardware. Rebuilt rather
    than patched when sources appear or disappear - there are at most a
    handful, and a rebuild cannot leave a stale entry behind.
    """
    reg = lynx_sources.SourceRegistry()

    reg.add(lynx_sources.PicotunerSource(
        "rf:a", "Tuner Rx 1",
        get_state=lambda: picotuner_state,
        rcv=1,
        get_mpv_running=lambda: mpv_running_for_rf,
        get_on_air_freq=on_air_from_if))

    # Tuner B only when something actually uses it. A source in the
    # registry that nothing can select is a lie, and a receiver with
    # one plug should not list two.
    _tw = (config.get('tri_watch') or {})
    _tw_has_b = _tw.get('enabled') and any(
        s.get('type') == 'rf' and s.get('rcv') == 2 and s.get('enabled')
        for s in (_tw.get('sources') or []))
    if diversity_enabled or _tw_has_b:
        reg.add(lynx_sources.PicotunerSource(
            "rf:b", "Tuner Rx 2",
            get_state=lambda: picotuner_state_b,
            rcv=2,
            get_mpv_running=lambda: mpv_running_for_rf,
            get_on_air_freq=on_air_from_if))

    for _i, _rs in enumerate(remote_states):
        if not _rs.get('enabled', True):
            continue
        reg.add(lynx_sources.SlaveSource(
            f"slave:{_i}", _rs.get('name') or f"Slave Rx {_i + 1}",
            get_state=(lambda i=_i: remote_states[i])))

    # Tuner 0 only for now - nothing tunes tuner 1 yet. The index is in
    # the identifier from the start so adding it later changes nothing
    # else, here or in any config file that has stored one.
    for _dev in hdhr_devices():
        _did = _dev["device_id"]
        reg.add(lynx_sources.HdhrSource(
            f"dvbt:{_did}:0", "DVB-T2",
            get_state=(lambda d=_did: next(
                (x for x in hdhr_devices() if x["device_id"] == d), None)),
            device_id=_did, tuner=0,
            get_stream_info=lambda: (get_live_stream_info()
                                     if current_mode == "dvbt" else {}),
            is_broadcast=lambda hz: not hdhr_is_amateur_band(hz)))

    reg.add(lynx_sources.StreamSource(
        "stream", "Stream",
        get_name=lambda: current_stream_name,
        get_stream_info=lambda: (get_live_stream_info()
                                 if current_mode == "stream" else {}),
        get_active=lambda: current_mode == "stream"))

    return reg


def _published_sources():
    """The registry as plain data, rebuilt on each call.

    Rebuilt rather than cached because sources appear and disappear -
    a Slave comes online, an HDHomeRun is unplugged - and this runs
    twice a second at most, against a handful of entries. Cheap enough
    not to need a cache, and a cache is how a stale entry survives.

    Never allowed to fail: this is diagnostic, and a receiver must not
    stop reporting its status because an experimental block raised.
    """
    try:
        reg = build_source_registry()
        reg.set_active(_registry_active_id())
        return {
            "active": reg.active_id(),
            "showing_picture": reg.showing_picture(),
            "list": reg.describe(),
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _registry_active_id():
    """Which registry id corresponds to the current mode.

    A translation rather than a source of truth: current_mode remains
    the authority until consumers move across, and this exists so the
    published registry can say which source is active without anything
    depending on it.
    """
    if current_mode == "stream":
        return "stream"
    if current_mode == "dvbt":
        _d = hdhr_default_device_id()
        return f"dvbt:{_d}:0" if _d else None
    if current_mode == "rf":
        if displayed_receiver_id is not None:
            try:
                return f"slave:{displayed_receiver_id - REMOTE_RECEIVER_ID_BASE}"
            except (TypeError, ValueError):
                return None
        # Diversity can be carried by either tuner; the one reporting a
        # lock is the honest answer, A first.
        if picotuner_state.get('locked'):
            return "rf:a"
        if diversity_enabled and picotuner_state_b.get('locked'):
            return "rf:b"
        return "rf:a"
    return None


@app.get("/api/status", tags=["Status"],'''


# --- 3. publish it, behind the flag ---------------------------------

OLD_PUB = '''    status = {
        "lynx": {
            "mode": current_mode,'''

NEW_PUB = '''    status = {
        "lynx": {
            "mode": current_mode,
            # Lynx+ step 2: the registry's own view, for comparison
            # against everything else in this payload. Off by default,
            # and nothing consumes it - it is here to be looked at.
            **({"sources": _published_sources()}
               if (config.get('sources') or {}).get('publish') else {}),'''


EDITS = [
    ("import", OLD_IMPORT, NEW_IMPORT),
    ("registry builder", OLD_BUILD, NEW_BUILD),
    ("status block", OLD_PUB, NEW_PUB),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found. Run from the lynx directory.")
        return 2
    if not Path("lynx_sources.py").exists():
        print("ERROR: lynx_sources.py not found - copy it over first.")
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
            print(f"  {name:<18} OK   one match")
        else:
            print(f"  {name:<18} FAIL {n} matches - expected 1")
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
        print("\n  syntax check       OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"\n  syntax check       FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tp.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(TARGET, TARGET.with_suffix(f".py.bak-registry2-{stamp}"))
    TARGET.write_text(patched, encoding="utf-8")
    print(f"\n  {TARGET} patched")
    print("\nNow run:  python3 verify.py")
    print("Then add to config/lynx_config.yaml:")
    print("    sources:")
    print("      publish: true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
