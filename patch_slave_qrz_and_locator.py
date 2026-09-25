#!/usr/bin/env python3
"""
patch_slave_qrz_and_locator.py

Two small gaps left over from treating Slaves as receivers.

QRZ NAMES
---------
qrz_first_name() is called for Rx 1 and Rx 2 when the status is
assembled, and doing so is what warms the lookup cache — the name
appears on the first contact rather than the second. Slaves were never
put through it, so a station heard on a Slave got a bare callsign and
nothing was ever looked up.

Nothing else needs changing. The overlay already reads callsign_name
from a Slave's record; there simply was never anything in it.

THE PORTABLE LOCATOR
--------------------
The OSD draws "PORTABLE: <locator>" from the local configuration. While
a Slave is displayed that is the wrong site: the receiver is wherever
the Slave is, not wherever this Lynx is. It looked right on the bench
only because both happened to be in the same place, and would have been
quietly wrong the moment a Slave appeared at another site.

A Slave reports its own locator in its SITE line, so it is used when one
is displayed. If the Slave has not given one, the line is dropped rather
than falling back to the local locator, which would be a confident wrong
answer.

Run from the repo root:  python3 patch_slave_qrz_and_locator.py
"""

import shutil
import sys
from pathlib import Path

APP = Path("lynx_app.py")
OVL = Path("lynx_overlay.py")


def apply(src: str, old: str, new: str, label: str) -> str:
    n = src.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected exactly 1")
    print(f"  ok  {label}")
    return src.replace(old, new, 1)


def main():
    for p in (APP, OVL):
        if not p.exists():
            sys.exit(f"ABORT: {p} not found — run this from the repo root")

    app, ovl = APP.read_text(), OVL.read_text()

    if "qrz_first_name(st[" in app:
        sys.exit("ABORT: already patched")
    if "_remote_display" not in ovl:
        sys.exit("ABORT: overlay Slave substitution not present — this builds on it")

    # ── 1. look the callsign up for Slaves too ───────────────────
    app = apply(
        app,
        '                "video": _relay_video_for(i),',
        '                "video": _relay_video_for(i),\n'
        '                # Same call the local receivers get. Asking is also\n'
        '                # what warms the cache, so a station heard on a\n'
        '                # Slave gets its name on the first contact rather\n'
        '                # than the second — or never, which is what\n'
        '                # happened while nothing asked.\n'
        '                "callsign_name": qrz_first_name(st["callsign"]),',
        "QRZ lookup for Slaves",
    )

    # ── 2. the displayed site's locator, not this one's ──────────
    ovl = apply(
        ovl,
        '            state["programme"] = rem.get(\'programme\', \'\')',
        '            state["programme"] = rem.get(\'programme\', \'\')\n'
        '            # The receiver on screen is at the Slave\'s site, so the\n'
        '            # locator shown should be the Slave\'s. The local one is\n'
        '            # right only when both happen to be in the same place,\n'
        '            # which is a coincidence of the bench rather than a\n'
        '            # design. Blank when the Slave has not reported one:\n'
        '            # the line is then dropped, which beats confidently\n'
        '            # showing somewhere else.\n'
        '            state["portable_locator"] = rem.get(\'locator\', \'\')',
        "Slave's own locator on the OSD",
    )

    shutil.copy2(APP, APP.with_suffix(".py.bak_qrz"))
    shutil.copy2(OVL, OVL.with_suffix(".py.bak_qrz"))
    APP.write_text(app)
    OVL.write_text(ovl)

    print("\n  Now: python3 -m py_compile lynx_app.py lynx_overlay.py")


if __name__ == "__main__":
    main()
