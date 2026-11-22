#!/usr/bin/env python3
"""
patch_band_gate.py — exclude broadcast rather than list the amateur bands.

The callsign gate was an allowlist: 6m, 4m, 2m, 70cm, 23cm, and a
service name outside those was not treated as a callsign. That was the
cautious choice at the time and it is now the wrong one.

Why it has to change
--------------------
A converter puts the tuner somewhere that is not an amateur band at
all. The IC-9700's 23cm converter brings 1304 MHz down to 375 MHz, so
every 23cm contact would be silently refused a callsign, a QRZ lookup
and a Pathfinder card - on the reasoning that 375 MHz is not an amateur
frequency, which is true and beside the point.

The same applies to any other converter anybody fits, and to the ATSC
3.0 tuner, which cannot reach 23cm at all and will only ever see it
through one.

So the test is inverted. What actually needs excluding is broadcast,
where a service name is a channel name and would otherwise reach QRZ
as a callsign - "BBC ONE Lon HD" being the example that prompted the
gate in the first place. Everything else is assumed to be amateur,
which is a safe assumption on a receiver pointed at amateur bands by
somebody who knows where they are pointing it.

Usage
-----
    python3 patch_band_gate.py            # dry run
    python3 patch_band_gate.py --apply
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
MARKER = "BROADCAST_BANDS"


OLD = '''def hdhr_is_amateur_band(freq_hz) -> bool:
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
            or 1240 <= mhz <= 1325)  # 23cm'''


NEW = '''# Where a service name is a channel name rather than a callsign, and
# must never reach QRZ as one. Everything NOT listed here is treated
# as amateur.
#
# Inverted from an allowlist of amateur bands deliberately. A
# converter puts the tuner somewhere that is not an amateur band at
# all - the IC-9700's 23cm converter brings 1304 MHz down to 375 MHz -
# so listing the amateur bands silently refused a callsign, a QRZ
# lookup and a Pathfinder card to every contact made through one. The
# same is true of any converter, and of the ATSC 3.0 tuner, which
# cannot reach 23cm except through one.
BROADCAST_BANDS = [
    (174.0, 230.0),   # Band III - DAB and DVB-T
    (470.0, 694.0),   # Bands IV/V - DVB-T2, post-700MHz-clearance
]


def hdhr_is_amateur_band(freq_hz) -> bool:
    """Should a service name from this frequency be treated as a callsign?

    True for anything outside the broadcast bands. Not a claim that
    the frequency IS an amateur allocation - a receiver behind a
    converter is legitimately tuned to an IF that belongs to nobody -
    only that a name found there is worth believing.
    """
    try:
        mhz = float(freq_hz) / 1e6
    except (TypeError, ValueError):
        return False
    if mhz <= 0:
        return False
    return not any(low <= mhz <= high for low, high in BROADCAST_BANDS)'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found. Run from the lynx directory.")
        return 2

    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"{TARGET} already patched - nothing to do.")
        return 0

    n = text.count(OLD)
    print(f"\nchecking anchor in {TARGET}\n")
    if n != 1:
        print(f"  band gate           FAIL {n} matches - expected 1")
        print("\nNo changes made.")
        return 1
    print("  band gate           OK   one match")

    patched = text.replace(OLD, NEW, 1)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(patched)
        tp = Path(tmp.name)
    try:
        py_compile.compile(str(tp), doraise=True)
        print("  syntax check        OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"  syntax check        FAIL {e}")
        return 1
    finally:
        tp.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the change.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(TARGET, TARGET.with_suffix(f".py.bak-bandgate-{stamp}"))
    TARGET.write_text(patched, encoding="utf-8")
    print(f"\n  {TARGET} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
