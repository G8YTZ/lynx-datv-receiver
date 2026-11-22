#!/usr/bin/env python3
"""
patch_pathfinder_sources.py — Pathfinder for DVB-T2 and Slave sources.

_pathfinder_current_source() knew about tri_watch, diversity and the
local Picotuner, and nothing else. In dvbt mode it fell through to the
last line and reported the Picotuner's lock - false on a receiver
without one - so Pathfinder believed nothing was ever on air and no
card was ever drawn. A Slave had the same problem for the same reason.

Three changes
-------------
1. The function returns the state dict alongside (receiving, rcv),
   instead of the watcher working out which one to read. With four
   kinds of source and only two of them Picotuner receivers, "which
   state does rcv mean" stopped being answerable in the caller.

2. rcv may now be None, meaning "not a local Picotuner receiver".
   _pathfinder_on_air_frequency() returns the frequency unchanged for
   those - a Slave's converter is its own business, and DVB-T2 here is
   fed straight from an aerial. If a DVB-T2 converter is ever wanted,
   this is where it would go.

3. dvbt and Slave branches, each supplying telemetry in the field
   names the watcher already caches.

DVB-T2 telemetry
----------------
frequency and modcod are real; the rest is left empty rather than
filled with something that looks like it but is not:

  modcod        "DVB-T2 1 MHz" - the standard and the channel width,
                which is what an operator would say, and what Portsdown
                puts in a log entry
  symbol_rate   empty. There isn't one, and the card's row appends
                kS/s, so the bandwidth cannot go here
  mer, margin   empty. SNQ is the closest thing this tuner reports to
                MER and it is still not MER - a percentage under a dB
                heading invites exactly the comparison it should not

Usage
-----
    python3 patch_pathfinder_sources.py            # dry run
    python3 patch_pathfinder_sources.py --apply    # back up and write

Then verify.py and restart.
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
MARKER = "_pathfinder_dvbt_state"


# --- 1. converter maths skipped when rcv is None --------------------------

OLD_FREQ = '''    raw = str(reported_mhz or '').strip()
    if not raw:
        return reported_mhz
    on_air = on_air_from_if(rcv, raw)'''

NEW_FREQ = '''    raw = str(reported_mhz or '').strip()
    if not raw:
        return reported_mhz
    # rcv None means the source is not a local Picotuner receiver - a
    # Slave, or the DVB-T2 tuner. Neither goes through a converter at
    # this end: a Slave's is its own business and reported as such,
    # and the DVB-T2 tuner is fed from an aerial. Applying Rx1's LNB LO
    # to either would be wrong by construction, even where the LO is
    # currently zero and the answer happens to come out right.
    if rcv is None:
        return reported_mhz
    on_air = on_air_from_if(rcv, raw)'''


# --- 2. the source function gains two branches and returns the state ------

OLD_SRC = '''    tw = config.get('tri_watch', {}) or {}
    if tw.get('enabled') and tri_watch_arbitrator is not None:
        idx = tri_watch_arbitrator.displayed_idx
        if idx is None:
            return (False, 1)
        try:
            src = tri_watch_sources_cfg[idx]
        except (IndexError, TypeError):
            return (False, 1)
        if src.get('type') != 'rf':
            return (False, 1)          # a stream is showing - no card
        rcv = src.get('rcv', 1)
        st = picotuner_state_b if rcv == 2 else picotuner_state
        return (bool(st.get('locked')), rcv)'''

NEW_SRC = '''    tw = config.get('tri_watch', {}) or {}
    if tw.get('enabled') and tri_watch_arbitrator is not None:
        idx = tri_watch_arbitrator.displayed_idx
        if idx is None:
            return (False, 1, picotuner_state)
        try:
            src = tri_watch_sources_cfg[idx]
        except (IndexError, TypeError):
            return (False, 1, picotuner_state)
        if src.get('type') != 'rf':
            return (False, 1, picotuner_state)   # a stream - no card
        rcv = src.get('rcv', 1)
        st = picotuner_state_b if rcv == 2 else picotuner_state
        return (bool(st.get('locked')), rcv, st)

    # DVB-T2. Checked before the Picotuner branches because when this
    # is what is on screen, the Picotuner's own lock state is not
    # merely irrelevant - on a receiver without one it is permanently
    # false, which is why Pathfinder never drew a card for a DVB-T2
    # contact while the OSD showed it perfectly.
    if current_mode == "dvbt":
        return (_pathfinder_dvbt_state(), None, _pathfinder_dvbt_state.last)

    # A Slave: a receiver at another site, whose telemetry already
    # arrives in the same field names a local tuner uses, so nothing
    # here has to translate anything.
    if displayed_receiver_id is not None:
        try:
            _idx = displayed_receiver_id - REMOTE_RECEIVER_ID_BASE
            if 0 <= _idx < len(remote_states):
                _rst = remote_states[_idx]
                return (bool(_rst.get('locked')), None, _rst)
        except (TypeError, ValueError):
            pass'''


# --- 3. the dvbt state builder -------------------------------------------

OLD_HELPER = '''def _pathfinder_on_air_frequency(rcv, reported_mhz):'''

NEW_HELPER = '''def _pathfinder_dvbt_state():
    """Is the DVB-T2 tuner locked, and what is it hearing?

    Returns a bool, and leaves the state dict on .last for the caller -
    an unusual shape, but it keeps _pathfinder_current_source()'s two
    lines readable and avoids building the dict twice.

    The dict uses the same field names the watcher caches for a local
    tuner, so the watcher needs no branch of its own. Fields this tuner
    genuinely does not report are left EMPTY rather than filled with an
    approximation: there is no symbol rate, and SNQ is not MER however
    similar it looks on a meter.
    """
    _dev = hdhr_default_device_id()
    _st = None
    if _dev:
        for _d in hdhr_devices():
            if _d["device_id"] == _dev:
                _st = _d
                break
    if not _st:
        _pathfinder_dvbt_state.last = {}
        return False

    _lm = _st.get("lock_mode") or ""
    _std = "DVB-T2" if _lm.endswith("dvbt2") else ("DVB-T" if "dvbt" in _lm else "")
    _bw = _lm[1:2]
    _freq = _st.get("frequency_hz")

    _pathfinder_dvbt_state.last = {
        "locked": bool(_st.get("locked")),
        "callsign": _st.get("callsign", ""),
        # Standard and channel width together, which is what an
        # operator would say out loud and what Portsdown writes in a
        # log entry. The card's symbol-rate row appends kS/s, so the
        # bandwidth cannot go there instead.
        "modcod": (f"{_std} {_bw} MHz" if _std and _bw else _std),
        "symbol_rate": "",
        "frequency": (f"{_freq / 1e6:.3f}" if _freq else ""),
        "mer": "",
        "margin": "",
    }
    return bool(_st.get("locked"))


_pathfinder_dvbt_state.last = {}


def _pathfinder_on_air_frequency(rcv, reported_mhz):'''


# --- 4. the watcher takes the state it is given ---------------------------

OLD_WATCH = '''            receiving, rcv = _pathfinder_current_source()
            prev = _pathfinder_prev

            if receiving:
                st = picotuner_state_b if rcv == 2 else picotuner_state
                cs = st.get('callsign', '')'''

NEW_WATCH = '''            # The state comes back with the answer now: with four kinds
            # of source and only two of them Picotuner receivers, "which
            # state does rcv mean" stopped being a question this loop
            # could answer.
            receiving, rcv, st = _pathfinder_current_source()
            prev = _pathfinder_prev

            if receiving:
                cs = st.get('callsign', '')'''


# --- 5. the final fall-through returns a state too ------------------------

OLD_TAIL = '''        if a:
            return (True, 1)
        if b:
            return (True, 2)
        return (False, 1)

    return (bool(picotuner_state.get('locked')), 1)'''

NEW_TAIL = '''        if a:
            return (True, 1, picotuner_state)
        if b:
            return (True, 2, picotuner_state_b)
        return (False, 1, picotuner_state)

    return (bool(picotuner_state.get('locked')), 1, picotuner_state)'''


EDITS = [
    ("converter skip", OLD_FREQ, NEW_FREQ),
    ("dvbt state builder", OLD_HELPER, NEW_HELPER),
    ("source branches", OLD_SRC, NEW_SRC),
    ("diversity tail", OLD_TAIL, NEW_TAIL),
    ("watcher", OLD_WATCH, NEW_WATCH),
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

    # Every caller of the function must be updated together: it is
    # about to return three things instead of two, and one left behind
    # would unpack wrongly at runtime rather than at import.
    callers = text.count("_pathfinder_current_source()")
    print(f"\nchecking {APP}\n")
    if callers != 2:
        print(f"  callers              FAIL {callers} references to "
              "_pathfinder_current_source() - expected 2 (its own def "
              "and the watcher). Another caller would need updating too.")
        print("\nNo changes made.")
        return 1
    print("  callers              OK   definition and watcher only")

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
    backup = APP.with_suffix(APP.suffix + f".bak-pathfinder-{stamp}")
    shutil.copy2(APP, backup)
    APP.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {APP} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
