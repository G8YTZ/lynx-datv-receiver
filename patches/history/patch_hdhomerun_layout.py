#!/usr/bin/env python3
"""
patch_hdhomerun_layout.py — DVB-T2 OSD layout, to match the DVB-S2 one.

The DVB-S2 display reads:

    438.023 MHz   333 kS/s            A: MER/D  23.9/ 14.5 dB
    8PSK 5/6   H264/AAC               B: MER/D  22.5/ 13.1 dB
                                              Justin - G8YTZ

...so the DVB-T2 one should read the same way: what it is tuned to,
then how it is coded, with quality top right and the meters bottom.

Changes
-------
1. Top left becomes two lines instead of five:
       546.000 MHz   8 MHz
       DVB-T2   6.62 Mb/s

2. The magic eye's two halves carry two different readings, which is
   what it was built for in diversity mode. Top: SEQ. Bottom: level.
   Quality and level are meters, not text - so SNQ moves top right and
   LEVEL leaves the text block entirely.

Top right is deliberately NOT touched here: that function has not been
read yet, and patching a display function blind is how an evening gets
longer than it needs to be.

Usage
-----
    python3 patch_hdhomerun_layout.py            # dry run
    python3 patch_hdhomerun_layout.py --apply    # back up and write

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

OVERLAY = Path("lynx_overlay.py")
MARKER = "EYE_DVBT"


# --------------------------------------------------------------------------
# 1. magic eye: top half SEQ, bottom half level
# --------------------------------------------------------------------------

OLD_EYE = '''            lvl = state.get("hdhr_level")
            if lvl in ("", None):
                text = "--"
                frac = 0.0
            else:
                text = f"{float(lvl):.0f}%"
                frac = min(1.0, float(lvl) / 100.0) ** 0.5
            fraction_a = fraction_b = frac
            value_text_a = value_text_b = text
            locked_a = locked_b = state.get("hdhr_locked", False)'''

NEW_EYE = '''            # EYE_DVBT: two halves, two readings - the same use the
            # eye was built for in diversity mode, except that here
            # they are two properties of one tuner rather than two
            # tuners. Top: SEQ. Bottom: level.
            #
            # Both are 0-100 from the device, shown as the percentages
            # they are rather than converted into a dBm figure it never
            # measured. If a model is ever found that reports dBmV,
            # that converts to dBm properly and the bottom half can
            # become directly comparable with the DVB-S2 side.
            seq = state.get("hdhr_seq")
            if seq in ("", None):
                value_text_a = "--"
                fraction_a = 0.0
            else:
                value_text_a = f"SEQ {float(seq):.0f}%"
                fraction_a = min(1.0, float(seq) / 100.0) ** 0.5

            lvl = state.get("hdhr_level")
            if lvl in ("", None):
                value_text_b = "--"
                fraction_b = 0.0
            else:
                value_text_b = f"{float(lvl):.0f}%"
                fraction_b = min(1.0, float(lvl) / 100.0) ** 0.5

            # One tuner, so one lock state - both halves agree.
            locked_a = locked_b = state.get("hdhr_locked", False)'''


# --------------------------------------------------------------------------
# 2. top left: two lines
# --------------------------------------------------------------------------

OLD_TL = '''            lines = []
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
                lines = ["--"]'''

NEW_TL = '''            # Two lines, the same shape as the DVB-S2 block above:
            # what it is tuned to, then how it is coded and at what
            # rate. Quality goes top right and the meters bottom, so
            # neither belongs here - the first version put all five on
            # one side and it read as a wall rather than a display.
            freq_hz = state.get("hdhr_frequency_hz")
            mode_name = state.get("hdhr_lock_mode") or ""
            std, bw = "", ""
            if mode_name:
                # t8dvbt2 -> "DVB-T2" and "8 MHz": what an operator
                # would say out loud, not what the API calls it.
                try:
                    bw = f"{mode_name[1]} MHz"
                    std = "DVB-T2" if mode_name.endswith("dvbt2") else "DVB-T"
                except Exception:
                    std = mode_name

            line1 = f"{freq_hz / 1e6:.3f} MHz" if freq_hz else "--"
            if bw:
                line1 += f"   {bw}"

            if state["hdhr_locked"]:
                line2 = std or "--"
                bps = state.get("hdhr_bitrate_bps") or 0
                if bps:
                    line2 += f"   {bps / 1e6:.2f} Mb/s"
            else:
                line2 = "SEARCHING"

            lines = [line1, line2]'''


EDITS = [
    ("magic eye split", OLD_EYE, NEW_EYE),
    ("top left layout", OLD_TL, NEW_TL),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    args = parser.parse_args()

    if not OVERLAY.exists():
        print(f"ERROR: {OVERLAY} not found. Run from the lynx directory.")
        return 2

    text = OVERLAY.read_text(encoding="utf-8")

    if "hdhr_lock_mode" not in text:
        print("ERROR: the HDHomeRun overlay patches have not been applied.")
        return 2
    if MARKER in text:
        print(f"{OVERLAY} already contains {MARKER} - already applied.")
        return 0

    print(f"\nchecking anchors in {OVERLAY}\n")
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
    backup = OVERLAY.with_suffix(OVERLAY.suffix + f".bak-layout-{stamp}")
    shutil.copy2(OVERLAY, backup)
    OVERLAY.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {OVERLAY} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
