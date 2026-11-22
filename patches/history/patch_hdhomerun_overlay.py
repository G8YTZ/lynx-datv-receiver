#!/usr/bin/env python3
"""
patch_hdhomerun_overlay.py — show the HDHomeRun on the OSD.

The problem
-----------
With current_mode == "dvbt" the overlay falls through its "stream"
branch into the DVB-S2 path, finds state["online"] false (that field
describes the PICOTUNER, which on a receiver without one is correctly
offline) and draws "Picotuner offline" over a picture that is decoding
perfectly well underneath. Confirmed directly: mpv reported video-format
h264, core-idle false, and 45% CPU while the screen showed the banner.

The fix
-------
1. lynx_app.py     — carry the HDHomeRun's status in /api/status, which
                     is the one thing the overlay polls. The separate
                     /api/hdhomerun route stays for tooling; the overlay
                     should not have to make two requests to draw one
                     screen.
2. lynx_overlay.py — five state keys, their unpacking in poll_status(),
                     and a "dvbt" draw branch beside the "stream" one,
                     returning before the Picotuner check.

Labels are DVB-T/T2 throughout: SNQ and SEQ, not MER and margin. They
are different measurements and putting one under the other's name is
how somebody ends up comparing them.

Requires patch_hdhomerun_source.py and patch_hdhomerun_host.py first.

Usage
-----
    python3 patch_hdhomerun_overlay.py            # dry run
    python3 patch_hdhomerun_overlay.py --apply    # back up and write

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
MARKER = "hdhr_lock_mode"


# ==========================================================================
# lynx_app.py — carry the fields in /api/status
# ==========================================================================

APP_ANCHOR = '            "mode": current_mode,'

APP_INSERT = '''            # The overlay polls this endpoint and only this
            # endpoint, so a source it has to draw has to be here.
            # None when no device was found at startup, which the
            # overlay treats as "not this receiver's problem" rather
            # than as a fault.
            "hdhomerun": (lambda d: {
                "online": d["online"],
                "locked": d["locked"],
                "lock_mode": d["lock_mode"],
                "level": d["signal_strength"],
                "snq": d["signal_quality"],
                "seq": d["symbol_quality"],
                "bitrate_bps": d["bitrate_bps"],
                "frequency_hz": d["frequency_hz"],
            } if d else None)(next(iter(hdhr_devices()), None)),
'''


# ==========================================================================
# lynx_overlay.py — state keys
# ==========================================================================

OV_ANCHOR_STATE = '    "mode": "idle",'

OV_INSERT_STATE = '''    # HDHomeRun (DVB-T/T2/C). Deliberately their own keys rather than
    # reusing mer/margin: those are DVB-S2 measurements, and a 0-100
    # quality percentage drawn under "MER" would invite exactly the
    # comparison it is not valid to make.
    "hdhr_online": False,
    "hdhr_locked": False,
    "hdhr_lock_mode": "",
    "hdhr_level": "",
    "hdhr_snq": "",
    "hdhr_seq": "",
    "hdhr_bitrate_bps": 0,
    "hdhr_frequency_hz": None,
'''


# ==========================================================================
# lynx_overlay.py — unpack in poll_status()
# ==========================================================================

OV_ANCHOR_POLL = "            pt = data.get('picotuner', {})"

OV_INSERT_POLL = '''
            # Absent on an older lynx_app.py, and absent on a receiver
            # with no device found - both are "nothing to draw", not an
            # error, so .get() with a default rather than a branch.
            hh = data.get('hdhomerun') or {}
            state["hdhr_online"] = bool(hh.get('online', False))
            state["hdhr_locked"] = bool(hh.get('locked', False))
            state["hdhr_lock_mode"] = hh.get('lock_mode', "") or ""
            state["hdhr_level"] = hh.get('level', "")
            state["hdhr_snq"] = hh.get('snq', "")
            state["hdhr_seq"] = hh.get('seq', "")
            state["hdhr_bitrate_bps"] = hh.get('bitrate_bps', 0) or 0
            state["hdhr_frequency_hz"] = hh.get('frequency_hz')
'''


# ==========================================================================
# lynx_overlay.py — draw branch
# ==========================================================================

OV_ANCHOR_DRAW = """        NOT_LOCKED_COLOUR = (0.9, 0.5, 0.1)
        LOCKED_COLOUR = (0.0, 1.0, 0.25)"""

OV_INSERT_DRAW = '''        if state["mode"] == "dvbt":
            # Its own branch, returning before the Picotuner check
            # below. Without this it fell through to the DVB-S2 path,
            # which reads state["online"] - the PICOTUNER's status, not
            # this tuner's - and drew "Picotuner offline" across a
            # picture that was decoding perfectly underneath.
            DVBT_NOT_LOCKED = (0.9, 0.5, 0.1)
            DVBT_LOCKED = (0.0, 1.0, 0.25)
            margin = LEFT_MARGIN
            size = 30
            line_h = size * 1.3
            colour = DVBT_LOCKED if state["hdhr_locked"] else DVBT_NOT_LOCKED

            lines = []
            freq_hz = state.get("hdhr_frequency_hz")
            mode_name = state.get("hdhr_lock_mode") or ""
            if freq_hz:
                # MHz on screen, Hz in the API: the device works in Hz
                # and nobody reads a nine-digit frequency.
                lines.append(f"{freq_hz / 1e6:.3f} MHz")
            if mode_name:
                # t8dvbt2 -> DVB-T2 8 MHz, which is what an operator
                # would say out loud.
                try:
                    bw = mode_name[1]
                    std = "DVB-T2" if mode_name.endswith("dvbt2") else "DVB-T"
                    lines.append(f"{std} {bw} MHz")
                except Exception:
                    lines.append(mode_name)
            elif not state["hdhr_locked"]:
                lines.append("SEARCHING")

            if state["hdhr_locked"]:
                # SNQ and SEQ by name. They are the device's own terms
                # and they are not MER and margin.
                lines.append(f"SNQ {state['hdhr_snq']}%  "
                             f"SEQ {state['hdhr_seq']}%")
                lines.append(f"LEVEL {state['hdhr_level']}%")
                bps = state.get("hdhr_bitrate_bps") or 0
                if bps:
                    lines.append(f"{bps / 1e6:.2f} Mb/s")

            if not lines:
                lines = ["--"]
            for i, line in enumerate(lines):
                y = margin + size + (i * line_h)
                self.draw_text(cr, margin, y, line, size=size,
                               colour=colour)
            return

'''


# ==========================================================================
# Machinery
# ==========================================================================

APP_EDITS = [
    ("status dict", APP_ANCHOR, APP_INSERT, "after"),
]

OV_EDITS = [
    ("overlay state keys", OV_ANCHOR_STATE, OV_INSERT_STATE, "before"),
    ("overlay poll unpack", OV_ANCHOR_POLL, OV_INSERT_POLL, "after"),
    ("overlay dvbt branch", OV_ANCHOR_DRAW, OV_INSERT_DRAW, "before"),
]


def check_and_patch(path: Path, edits: list) -> str | None:
    """Verify every anchor, then return the patched text. None on failure."""
    text = path.read_text(encoding="utf-8")
    print(f"\nchecking anchors in {path}\n")
    ok = True
    for name, anchor, _insert, _where in edits:
        count = text.count(anchor)
        if count == 1:
            print(f"  {name:<24} OK   one match")
        else:
            print(f"  {name:<24} FAIL {count} matches - expected exactly 1")
            ok = False
    if not ok:
        return None

    patched = text
    for _name, anchor, insert, where in edits:
        if where == "before":
            patched = patched.replace(anchor, insert + anchor, 1)
        else:
            patched = patched.replace(anchor, anchor + "\n" + insert, 1)
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
        print("ERROR: patch_hdhomerun_source.py has not been applied.")
        return 2
    if MARKER in OVERLAY.read_text(encoding="utf-8"):
        print(f"{OVERLAY} already contains {MARKER} - already applied.")
        return 0

    app_patched = check_and_patch(APP, APP_EDITS)
    ov_patched = check_and_patch(OVERLAY, OV_EDITS)

    if app_patched is None or ov_patched is None:
        print("\nNo changes made to either file. An anchor is missing or "
              "ambiguous, so nothing here is safe to apply.")
        return 1

    print("\nsyntax\n")
    if not (compiles(app_patched, "lynx_app.py")
            and compiles(ov_patched, "lynx_overlay.py")):
        print("\nNo changes made to either file.")
        return 1

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    # Both files together or neither: an overlay expecting fields that
    # lynx_app.py is not sending would draw an empty panel and say
    # nothing about why.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for path, patched in ((APP, app_patched), (OVERLAY, ov_patched)):
        backup = path.with_suffix(path.suffix + f".bak-hdhrosd-{stamp}")
        shutil.copy2(path, backup)
        path.write_text(patched, encoding="utf-8")
        print(f"\n  {path} patched, backup {backup.name}")

    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
