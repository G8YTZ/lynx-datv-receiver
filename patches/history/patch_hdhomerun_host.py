#!/usr/bin/env python3
"""
patch_hdhomerun_host.py — fix the HDHomeRun stream target address.

The bug
-------
hdhr_source() passed lynx_host="127.0.0.1". The HDHomeRun is a separate
box on the network, so its idea of 127.0.0.1 is ITSELF: it dutifully
accepted the target, reported it back correctly, and sent the stream to
its own loopback. Nothing ever left the device, and tcpdump on the Pi
saw nothing on any interface.

The fix
-------
Ask the kernel which of OUR addresses it would use to reach THAT device,
each time a tune happens. Computed rather than configured, because
addresses change: DHCP, a move between Ethernet and Wi-Fi, or a site
renumbering would all silently break a remembered value, and the failure
looks exactly like the one above - device reports success, no packets
arrive.

Requires patch_hdhomerun_source.py to have been applied first.

Usage
-----
    python3 patch_hdhomerun_host.py            # dry run
    python3 patch_hdhomerun_host.py --apply    # back up and write

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

TARGET = Path("lynx_app.py")
MARKER = "def hdhr_local_address"       # presence means already applied


# --------------------------------------------------------------------------
# 1. the helper, inserted before hdhr_source()
# --------------------------------------------------------------------------

ANCHOR_HELPER = "def hdhr_source(device_id=None):"

INSERT_HELPER = '''def hdhr_local_address(device_address: str) -> str:
    """Which of OUR addresses the device should stream to.

    Asked of the kernel rather than configured, and asked again on
    every tune rather than remembered. A UDP connect() sends nothing -
    it only consults the routing table - so this costs nothing and is
    correct on a multi-homed receiver, after a DHCP change, and after
    a move between Ethernet and Wi-Fi.

    This exists because the first version passed "127.0.0.1", which
    the device accepted and reported back perfectly while sending the
    stream to its OWN loopback. Nothing arrived, and nothing said why:
    status showed locked and streaming, pps was non-zero, and tcpdump
    on every interface here saw nothing at all. A wrong address that
    the far end cheerfully confirms is worth guarding against properly.
    """
    family = socket.AF_INET6 if ":" in device_address else socket.AF_INET
    s = socket.socket(family, socket.SOCK_DGRAM)
    try:
        # Port 1 is arbitrary: no packet is sent, and the device is
        # never contacted. Only the routing decision is wanted.
        s.connect((device_address, 1))
        return s.getsockname()[0]
    finally:
        s.close()


'''


# --------------------------------------------------------------------------
# 2. use it
# --------------------------------------------------------------------------

ANCHOR_HOST = '''    return lynx_hdhomerun.HDHomeRunSource(
        device_id=device_id,
        lynx_host="127.0.0.1",     # device streams here; mpv reads locally
        tuner=st.get("tuner", 0),
        udp_port=HDHR_VIDEO_PORT,
    )'''

REPLACE_HOST = '''    return lynx_hdhomerun.HDHomeRunSource(
        device_id=device_id,
        lynx_host=hdhr_local_address(st.get("address") or device_id),
        tuner=st.get("tuner", 0),
        udp_port=HDHR_VIDEO_PORT,
    )'''


EDITS = [
    ("hdhr_local_address helper", ANCHOR_HELPER, INSERT_HELPER, None),
    ("stream target address", ANCHOR_HOST, None, REPLACE_HOST),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    parser.add_argument("--file", default=str(TARGET))
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: {path} not found. Run this from the lynx directory.")
        return 2

    text = path.read_text(encoding="utf-8")

    if "HDHR_VIDEO_PORT" not in text:
        print("ERROR: patch_hdhomerun_source.py has not been applied.")
        return 2

    if MARKER in text:
        print(f"{path} already contains {MARKER} - patch already applied.")
        return 0

    print(f"\nchecking anchors in {path}\n")
    ok = True
    for name, anchor, _insert, _replace in EDITS:
        count = text.count(anchor)
        if count == 1:
            print(f"  {name:<26} OK   one match")
        else:
            print(f"  {name:<26} FAIL {count} matches - expected exactly 1")
            ok = False

    if not ok:
        print("\nNo changes made.")
        return 1

    patched = text
    for _name, anchor, insert, replace in EDITS:
        if insert is not None:
            patched = patched.replace(anchor, insert + anchor, 1)
        else:
            patched = patched.replace(anchor, replace, 1)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(patched)
        tmp_path = Path(tmp.name)
    try:
        py_compile.compile(str(tmp_path), doraise=True)
        print("\n  syntax check              OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"\n  syntax check              FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tmp_path.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak-hdhrhost-{stamp}")
    shutil.copy2(path, backup)
    path.write_text(patched, encoding="utf-8")

    print(f"\n  backup written to {backup.name}")
    print(f"  {path} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
