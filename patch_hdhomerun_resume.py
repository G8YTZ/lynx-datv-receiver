#!/usr/bin/env python3
"""
patch_hdhomerun_resume.py — resume a DVB-T2 source after a restart.

The tune route already writes save_last_state({"mode": "dvbt", ...}),
but _resume_on_startup() had no branch for it - so a receiver that was
watching a DVB-T2 station came back idle after a reboot, watchdog
restart or power cut. At a repeater that is the difference between
recovering by itself and waiting for somebody to drive up the hill.

The shape follows the rf branch's own optimisation. An HDHomeRun is a
network device: it keeps its tuning and its stream target through a
Lynx restart, and was confirmed doing exactly that - still locked and
still streaming to the video port while Lynx was coming back up. So if
it is already on the saved frequency, there is no need to retune it.
mpv still gets restarted, because mpv is the part that did not survive.

Discovery matters here too. It runs once at startup, and this resume
fires seven seconds later, so the device is normally known by now - but
a device that is slow to answer, or a switch still bringing a port up,
would leave hdhr_states empty. Rather than giving up, this waits a
bounded few seconds and sweeps once more, the same way the Slave resume
waits for a Slave to report in.

lynx_app.py only.

Usage
-----
    python3 patch_hdhomerun_resume.py            # dry run
    python3 patch_hdhomerun_resume.py --apply    # back up and write

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
MARKER = "Resuming previous DVB-T2"


OLD = '''        # No valid previous state — fall back to the explicit default
        # boot preset, if one has been configured.'''

NEW = '''        if state and state.get("mode") == "dvbt":
            # Discovery has normally finished by now - it runs at
            # startup and this is seven seconds later - but a device
            # slow to answer, or a switch still bringing its port up,
            # would leave nothing found. Bounded wait rather than an
            # immediate failure, same principle as REMOTE_RESUME_WAIT_SECS.
            _waited = 0
            while not hdhr_devices() and _waited < 20:
                time.sleep(2)
                _waited += 2
                hdhr_discover_once()

            _dev = state.get("device_id") or hdhr_default_device_id()
            if not _dev:
                print("Could not resume DVB-T2: no HDHomeRun found")
            else:
                try:
                    # Already tuned? An HDHomeRun keeps its channel and
                    # its stream target across a Lynx restart - it is a
                    # separate box on the network and has no idea we
                    # went away. Confirmed live: after a reboot it was
                    # still locked and still streaming. So retuning is
                    # usually unnecessary; mpv is the part that needs
                    # restarting, and the tune route does both.
                    _st = None
                    for _d in hdhr_devices():
                        if _d["device_id"] == _dev:
                            _st = _d
                            break
                    _same = bool(_st and _st.get("locked")
                                 and _st.get("frequency_hz") == state["freq"])

                    print(f"Resuming previous DVB-T2: "
                          f"{state['freq']/1e6:.3f} MHz / "
                          f"{state.get('modulation')}"
                          f"{' (already locked)' if _same else ''}")

                    tune_dvbt(DvbtTuneRequest(
                        freq=state["freq"],
                        modulation=state.get("modulation", "t8dvbt2"),
                        program=state.get("program"),
                        device_id=state.get("device_id"),
                    ))
                    return
                except Exception as e:
                    # Left idle rather than falling through to the
                    # default boot preset: that is a Picotuner preset,
                    # and tuning a tuner this receiver may not even
                    # have is not a useful recovery.
                    print(f"Could not resume DVB-T2: {type(e).__name__}: {e}")
                    return

        # No valid previous state — fall back to the explicit default
        # boot preset, if one has been configured.'''


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
        print(f"{APP} already contains the dvbt resume - nothing to do.")
        return 0

    count = text.count(OLD)
    print(f"\nchecking anchor in {APP}\n")
    if count != 1:
        print(f"  resume fallback      FAIL {count} matches - expected 1")
        print("\nNo changes made.")
        return 1
    print("  resume fallback      OK   one match")

    patched = text.replace(OLD, NEW, 1)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(patched)
        tmp_path = Path(tmp.name)
    try:
        py_compile.compile(str(tmp_path), doraise=True)
        print("  syntax check         OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"  syntax check         FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tmp_path.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = APP.with_suffix(APP.suffix + f".bak-resume-{stamp}")
    shutil.copy2(APP, backup)
    APP.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {APP} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
