#!/usr/bin/env python3
"""
patch_docs_hdhomerun_correction.py — corrections from SiliconDust.

Three things came back from SiliconDust after the first documentation
pass, and two of them make what we published wrong:

1. Both the HDHR5-2DT and 4DT are DISCONTINUED. The README and
   CHANGELOG currently point people at hardware they cannot buy.

2. The tuning range is 44-866 MHz for the DT models and 44-1002 MHz
   for the ATSC 3.0 ones. 23cm starts at 1240 MHz, so it is out of
   reach of both - the earlier note that 6 MHz would "fit 70cm and
   23cm" was wrong. 44 MHz at the bottom is better than expected
   though: 6m and 4m are reachable, even if neither has room for a
   1 MHz channel.

3. The tuner's RF filtering cannot go below about 5 or 6 MHz whatever
   the demodulator is set to. At 1 or 2 MHz there is demodulator
   selectivity but not front-end selectivity, which matters at a
   repeater site.

Also worth recording: the narrow bandwidths are not a standard feature
anywhere. SiliconDust reverse-engineered how the driver sets channel
width on that demodulator and added a custom set - which is why no
other model has them, and why a replacement depends on them choosing
to build one.

Usage
-----
    python3 patch_docs_hdhomerun_correction.py            # dry run
    python3 patch_docs_hdhomerun_correction.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

CHANGELOG = Path("CHANGELOG.md")
README = Path("README.md")
MARKER = "discontinued"


README_EDITS = [
    ("- Optionally, a SiliconDust HDHomeRun for DVB-T/T2/C. The narrow "
     "amateur bandwidths are available only on the **HDHR5-2DT** (two tuners) "
     "and **HDHR5-4DT** (four tuners) — other models do not have them in the "
     "demodulator",

     "- Optionally, a SiliconDust HDHomeRun for DVB-T/T2/C. The narrow "
     "amateur bandwidths are available only on the **HDHR5-2DT** (two tuners) "
     "and **HDHR5-4DT** (four tuners), **both now discontinued** — "
     "second-hand only. No other model has them, and SiliconDust know of no "
     "other demodulator supporting anything narrower than 1.7 MHz as "
     "standard. They have said they would like to make a new model for this. "
     "Tuning range 44–866 MHz, so 2m and 70cm are usable and 23cm is out of "
     "reach"),
]


CL_ANCHOR = ("- The narrow bandwidths are available only on the HDHR5-2DT and "
             "HDHR5-4DT. SiliconDust have confirmed this directly and have "
             "said they are considering a model aimed at amateur use, so it "
             "may broaden.")

CL_NEW = (
 "- The narrow bandwidths are available only on the HDHR5-2DT and HDHR5-4DT, "
 "and both are now discontinued - second-hand only. They are not a standard "
 "feature anywhere: SiliconDust reverse-engineered how the driver sets "
 "channel width on that demodulator and added a custom set of bandwidths, "
 "which is why no other model has them and why SiliconDust know of no other "
 "demodulator supporting anything narrower than 1.7 MHz as standard. They "
 "have said they would like to make a new model for this using the same "
 "demodulator, and are watching what the amateur community does with it.\n"
 "- The tuner's RF filtering cannot go below about 5 or 6 MHz whatever the "
 "demodulator is set to, so at 1 or 2 MHz there is demodulator selectivity "
 "but not front-end selectivity - a strong signal within a few MHz of the "
 "wanted one still reaches the demodulator. At a repeater site, or anywhere "
 "with a transmitter nearby, that argues for a filter ahead of the tuner.\n"
 "- The tuning range is 44-866 MHz on the DT models and 44-1002 MHz on the "
 "ATSC 3.0 ones, so 23cm is out of reach of both. 6m and 4m are within range, "
 "though neither has room for a 1 MHz channel in its amateur allocation."
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    args = parser.parse_args()

    for path in (CHANGELOG, README):
        if not path.exists():
            print(f"ERROR: {path} not found. Run from the lynx directory.")
            return 2

    cl = CHANGELOG.read_text(encoding="utf-8")
    rm = README.read_text(encoding="utf-8")

    if MARKER in rm:
        print("README already mentions the discontinuation - nothing to do.")
        return 0

    print("\nchecking anchors\n")
    ok = True

    if cl.count(CL_ANCHOR) == 1:
        print("  CHANGELOG known issue  OK   one match")
    else:
        print(f"  CHANGELOG known issue  FAIL {cl.count(CL_ANCHOR)} matches")
        ok = False

    for i, (old, _new) in enumerate(README_EDITS, 1):
        if rm.count(old) == 1:
            print(f"  README edit {i}          OK   one match")
        else:
            print(f"  README edit {i}          FAIL {rm.count(old)} matches")
            ok = False

    if not ok:
        print("\nNo changes made.")
        return 1

    cl_new = cl.replace(CL_ANCHOR, CL_NEW, 1)
    rm_new = rm
    for old, new in README_EDITS:
        rm_new = rm_new.replace(old, new, 1)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for path, text in ((CHANGELOG, cl_new), (README, rm_new)):
        shutil.copy2(path, path.with_suffix(path.suffix + f".bak-{stamp}"))
        path.write_text(text, encoding="utf-8")
        print(f"\n  {path} updated")

    return 0


if __name__ == "__main__":
    sys.exit(main())
