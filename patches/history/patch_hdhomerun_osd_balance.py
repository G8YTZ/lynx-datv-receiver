#!/usr/bin/env python3
"""
patch_hdhomerun_osd_balance.py — rebalance the DVB-T2 top-left block.

Before:
    146.500 MHz   1 MHz
    DVB-T2   0.67 Mb/s  H264/AAC

After:
    146.500 MHz  1 MHz  DVB-T2
    0.67 Mb/s  H264/AAC

Line 1 is the RF - where it is, how wide, which standard. Line 2 is the
payload - how much of it and in what codec. A cleaner division than
putting the standard with the bitrate, and it leaves the two lines about
the same length rather than one short and one long.

lynx_overlay.py only.

Usage
-----
    python3 patch_hdhomerun_osd_balance.py            # dry run
    python3 patch_hdhomerun_osd_balance.py --apply    # back up and write
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
MARKER = "Line 1 is the RF"


OLD = '''            line1 = f"{freq_hz / 1e6:.3f} MHz" if freq_hz else "--"
            if bw:
                line1 += f"   {bw}"

            if state["hdhr_locked"]:
                line2 = std or "--"
                bps = state.get("hdhr_bitrate_bps") or 0
                if bps:
                    line2 += f"   {bps / 1e6:.2f} Mb/s"'''

NEW = '''            # Line 1 is the RF: where it is, how wide, which standard.
            # Line 2 is the payload: how much of it, in what codec.
            # A cleaner division than putting the standard with the
            # bitrate, and it leaves the two lines about the same
            # length rather than one short and one long.
            line1 = f"{freq_hz / 1e6:.3f} MHz" if freq_hz else "--"
            if bw:
                line1 += f"  {bw}"
            if std and state["hdhr_locked"]:
                line1 += f"  {std}"

            if state["hdhr_locked"]:
                bps = state.get("hdhr_bitrate_bps") or 0
                line2 = f"{bps / 1e6:.2f} Mb/s" if bps else "--"'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    args = parser.parse_args()

    if not OVERLAY.exists():
        print(f"ERROR: {OVERLAY} not found. Run from the lynx directory.")
        return 2

    text = OVERLAY.read_text(encoding="utf-8")

    if MARKER in text:
        print(f"{OVERLAY} already rebalanced - nothing to do.")
        return 0

    count = text.count(OLD)
    print(f"\nchecking anchor in {OVERLAY}\n")
    if count != 1:
        print(f"  top left block       FAIL {count} matches - expected 1")
        print("\nNo changes made.")
        return 1
    print("  top left block       OK   one match")

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
    backup = OVERLAY.with_suffix(OVERLAY.suffix + f".bak-osdbal-{stamp}")
    shutil.copy2(OVERLAY, backup)
    OVERLAY.write_text(patched, encoding="utf-8")
    print(f"\n  backup written to {backup.name}")
    print(f"  {OVERLAY} patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
