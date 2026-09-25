#!/usr/bin/env python3
"""
patch_hdhomerun_service_name.py — service name and callsign on the OSD.

The device reports the service name carried in the multiplex. On an
amateur transmission that is the station's callsign, which is why it is
worth having rather than treating as a label: it can drive the QRZ
lookup and the Pathfinder map exactly as a DVB-S2 contact does.

Why the first attempt deadlocked
--------------------------------
The previous version fetched the name inline in the tune route, with:

    with hdhr_lock:
        live = hdhr_states.get(req.device_id or hdhr_default_device_id())

hdhr_default_device_id() takes hdhr_lock itself, and threading.Lock is
not reentrant - so the thread blocked waiting for a lock it was already
holding. Everything behind that lock stopped, including /api/status.

Two changes fix it and improve it:

1. The device ID is resolved BEFORE the lock is taken. That alone would
   have been enough.
2. The name is fetched on the poller thread rather than in the tune
   route. The tune only records what to look for and returns; the
   poller, which already talks to the device on its own schedule, does
   the work on its next pass. A tune should not wait on a streaminfo
   request, and the name appearing a second later is fine - it is a
   label, not a lock indicator.

The callsign is gated on the tuned frequency being in an amateur band,
NOT on the shape of the name. Ryde's logbook code truncates a provider
string at the first non-alphanumeric character, which is right for a
repeater that only hears amateurs - but this tuner also covers Band III
and Bands IV/V, where the same rule would send "BBC" to QRZ. What band
it is tuned to is a fact; what a name looks like is a guess.

Requires the earlier HDHomeRun patches. Touches lynx_app.py and
lynx_overlay.py. No HTML or JavaScript.

Usage
-----
    python3 patch_hdhomerun_service_name.py            # dry run
    python3 patch_hdhomerun_service_name.py --apply    # back up and write

Then:
    python3 verify.py
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
OVERLAY = Path("lynx_overlay.py")
MARKER = "hdhr_is_amateur_band"


# ==========================================================================
# lynx_app.py
# ==========================================================================

# --- 1. band test, beside the other helpers -------------------------------

APP_A_OLD = "def hdhr_source(device_id=None):"

APP_A_NEW = '''def hdhr_is_amateur_band(freq_hz) -> bool:
    """Is this frequency in an amateur band?

    Broadcast Band III (174-230 MHz) and Bands IV/V (470-862 MHz) are
    excluded by simply not being listed. A service name from a
    broadcast multiplex is a channel name, not a callsign, and must
    never reach QRZ as one.
    """
    try:
        mhz = float(freq_hz) / 1e6
    except (TypeError, ValueError):
        return False
    return (50 <= mhz <= 52          # 6m
            or 70.0 <= mhz <= 70.5   # 4m
            or 144 <= mhz <= 148     # 2m
            or 430 <= mhz <= 440     # 70cm
            or 1240 <= mhz <= 1325)  # 23cm


def hdhr_source(device_id=None):'''


# --- 2. the tune route records what to look for ---------------------------

APP_B_OLD = '''    current_mode = "dvbt"
    displayed_receiver_id = None'''

APP_B_NEW = '''    # Ask the poller to fetch the service name on its next pass rather
    # than fetching it here. An earlier version called streaminfo
    # inline and deadlocked the whole app - see this patch's own notes.
    # A tune has no business waiting on a name lookup anyway.
    #
    # device_id resolved BEFORE the lock is taken: hdhr_default_device_id()
    # takes hdhr_lock itself, and threading.Lock is not reentrant.
    _dev = req.device_id or hdhr_default_device_id()
    with hdhr_lock:
        live = hdhr_states.get(_dev)
        if live is not None:
            # Cleared, not left stale: the previous service's name over
            # a new one is worse than no name at all.
            live["service_name"] = ""
            live["callsign"] = ""
            live["pending_program"] = (str(req.program)
                                       if req.program is not None else None)
            live["pending_freq_hz"] = req.freq

    current_mode = "dvbt"
    displayed_receiver_id = None'''


# --- 3. the poller does the work ------------------------------------------

APP_C_OLD = '''                status = tuner.status()
                with hdhr_lock:'''

APP_C_NEW = '''                status = tuner.status()
                pending_program = None
                pending_freq = None
                with hdhr_lock:'''

APP_D_OLD = '''                        "bitrate_bps": status.bits_per_second,
                        "frequency_hz": status.frequency_hz,
                    })'''

APP_D_NEW = '''                        "bitrate_bps": status.bits_per_second,
                        "frequency_hz": status.frequency_hz,
                    })
                    pending_program = live.get("pending_program")
                    pending_freq = live.get("pending_freq_hz")

                # Outside the lock deliberately: this makes a request to
                # the device, and holding a lock across network I/O is
                # how a poller stops everything else in the process.
                if pending_program and status.locked:
                    raw = ""
                    for prog in tuner.stream_info():
                        if prog.get("program") == pending_program:
                            raw = prog.get("name", "")
                            break

                    # Same truncation as Ryde's logbook code: keep
                    # characters up to the first non-alphanumeric one,
                    # so "G8YTZ /P" becomes "G8YTZ".
                    call = ""
                    for ch in raw:
                        if ch.isalnum():
                            call += ch
                        else:
                            break

                    with hdhr_lock:
                        live = hdhr_states.get(device_id)
                        if live is not None:
                            live["service_name"] = raw
                            live["callsign"] = (
                                call.upper()
                                if hdhr_is_amateur_band(pending_freq) else "")
                            # Once only: a name that did not arrive is a
                            # blank label, not something to keep asking
                            # about every two seconds for ever.
                            live["pending_program"] = None'''


# --- 4. carry both in /api/status -----------------------------------------

APP_E_OLD = '''                "frequency_hz": d["frequency_hz"],
            } if d else None)(next(iter(hdhr_devices()), None)),'''

APP_E_NEW = '''                "frequency_hz": d["frequency_hz"],
                "service_name": d.get("service_name", ""),
                # Empty unless the tuned frequency was in an amateur
                # band - see hdhr_is_amateur_band(). The overlay feeds
                # this to the callsign field, which reaches QRZ and
                # Pathfinder, so a broadcast channel name must never
                # arrive here.
                "callsign": d.get("callsign", ""),
            } if d else None)(next(iter(hdhr_devices()), None)),'''


# ==========================================================================
# lynx_overlay.py
# ==========================================================================

OV_A_OLD = '''    "hdhr_frequency_hz": None,'''

OV_A_NEW = '''    "hdhr_frequency_hz": None,
    "hdhr_service_name": "",
    "hdhr_callsign": "",'''

OV_B_OLD = '''            state["hdhr_frequency_hz"] = hh.get('frequency_hz')'''

OV_B_NEW = '''            state["hdhr_frequency_hz"] = hh.get('frequency_hz')
            state["hdhr_service_name"] = hh.get('service_name', "") or ""
            # Fed into the shared callsign field as well, so the QRZ
            # lookup and Pathfinder treat a DVB-T2 station exactly as
            # they treat a DVB-S2 one. Empty on a broadcast frequency,
            # by design - lynx_app.py gates it on the band.
            hdhr_call = hh.get('callsign', "") or ""
            state["hdhr_callsign"] = hdhr_call
            if state.get("mode") == "dvbt" and hdhr_call:
                state["callsign"] = hdhr_call'''


APP_EDITS = [
    ("band test helper", APP_A_OLD, APP_A_NEW),
    ("tune records pending", APP_B_OLD, APP_B_NEW),
    ("poller locals", APP_C_OLD, APP_C_NEW),
    ("poller fetches name", APP_D_OLD, APP_D_NEW),
    ("status fields", APP_E_OLD, APP_E_NEW),
]

OV_EDITS = [
    ("overlay state keys", OV_A_OLD, OV_A_NEW),
    ("overlay poll unpack", OV_B_OLD, OV_B_NEW),
]


def check_and_patch(path: Path, edits: list):
    text = path.read_text(encoding="utf-8")
    print(f"\nchecking anchors in {path}\n")
    ok = True
    for name, anchor, _new in edits:
        count = text.count(anchor)
        if count == 1:
            print(f"  {name:<24} OK   one match")
        else:
            print(f"  {name:<24} FAIL {count} matches - expected exactly 1")
            ok = False
    if not ok:
        return None
    patched = text
    for _name, anchor, new in edits:
        patched = patched.replace(anchor, new, 1)
    return patched


def compiles(text: str, label: str) -> bool:
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    try:
        py_compile.compile(str(tmp_path), doraise=True)
        print(f"  {label:<24} OK   result parses")
        return True
    except py_compile.PyCompileError as e:
        print(f"  {label:<24} FAIL {e}")
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    args = parser.parse_args()

    for path in (APP, OVERLAY):
        if not path.exists():
            print(f"ERROR: {path} not found. Run from the lynx directory.")
            return 2

    app_text = APP.read_text(encoding="utf-8")
    if "HDHR_VIDEO_PORT" not in app_text:
        print("ERROR: the earlier HDHomeRun patches have not been applied.")
        return 2
    if MARKER in app_text:
        print(f"{APP} already contains {MARKER} - already applied.")
        return 0

    app_patched = check_and_patch(APP, APP_EDITS)
    ov_patched = check_and_patch(OVERLAY, OV_EDITS)
    if app_patched is None or ov_patched is None:
        print("\nNo changes made to either file.")
        return 1

    print("\nsyntax\n")
    if not (compiles(app_patched, "lynx_app.py")
            and compiles(ov_patched, "lynx_overlay.py")):
        print("\nNo changes made to either file.")
        return 1

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for path, patched in ((APP, app_patched), (OVERLAY, ov_patched)):
        backup = path.with_suffix(path.suffix + f".bak-svcname-{stamp}")
        shutil.copy2(path, backup)
        path.write_text(patched, encoding="utf-8")
        print(f"\n  {path} patched, backup {backup.name}")

    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
