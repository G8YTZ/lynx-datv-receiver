#!/usr/bin/env python3
"""
patch_qrz_my_latlon.py

Sends MY_LAT and MY_LON alongside MY_GRIDSQUARE, so a portable locator
actually takes effect on QRZ.

WHY IT WAS NOT WORKING
----------------------
MY_GRIDSQUARE was already being sent, and QRZ was accepting it. From
Steve KF8KI at QRZ:

    The application does not calculate the lat/lon from the provided
    gridsquare in uploads.

So the square was going up while the coordinates stayed at whatever the
account profile holds. The QSO ended up labelled with the portable
square but positioned at the home site, which is why distances and the
map looked wrong and the override appeared to be ignored.

The same reply gives the general rule: account settings are used as a
default only where the upload omits the field, and anything omitted
falls back silently. So the coordinates have to be supplied explicitly.

WHAT IS SENT
------------
The centre of the reported square, formatted as ADIF's Location type —
direction letter, three-digit degrees, then minutes to three decimals,
e.g. "N051 23.750". Both fields are added only when a portable locator
is set, so ordinary operation still produces byte-identical ADIF to
before, exactly as the existing MY_GRIDSQUARE block is careful to do.

The square's centre is the honest answer to "where was this received":
a six-character locator is about 4.6 x 9.3 km, so the centre is within
roughly 3 km of anywhere in it. Better precision would need a real fix,
and inventing one from a locator would be a false claim.

Run from the repo root:  python3 patch_qrz_my_latlon.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_notifications.py")


def apply(src: str, old: str, new: str, label: str) -> str:
    n = src.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected exactly 1")
    print(f"  ok  {label}")
    return src.replace(old, new, 1)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")

    src = TARGET.read_text()
    before = len(src.splitlines())

    if "_maidenhead_centre" in src:
        sys.exit("ABORT: already patched")

    # ── 1. the conversion ────────────────────────────────────────
    src = apply(
        src,
        "def _build_qrz_adif(api_key, action, call, band, mode, qso_date, time_on,",
        '''def _maidenhead_centre(locator):
    """(lat, lon) of the centre of a Maidenhead square, or None.

    Four or six characters. Six gives a square about 4.6 km by 9.3 km,
    so its centre is within roughly 3 km of anywhere inside it — which
    is the honest limit of what a locator can say. Four is coarser and
    is treated the same way rather than refused, since a coarse position
    is still better than the wrong one.

    Returns None for anything malformed rather than guessing, so a
    mistyped locator omits the coordinates instead of logging a QSO
    somewhere it did not happen.
    """
    loc = (locator or "").strip().upper()
    if len(loc) < 4:
        return None
    if not ('A' <= loc[0] <= 'R' and 'A' <= loc[1] <= 'R'):
        return None
    if not (loc[2].isdigit() and loc[3].isdigit()):
        return None

    lon = -180.0 + (ord(loc[0]) - 65) * 20.0 + int(loc[2]) * 2.0
    lat = -90.0 + (ord(loc[1]) - 65) * 10.0 + int(loc[3]) * 1.0

    if len(loc) >= 6 and 'A' <= loc[4] <= 'X' and 'A' <= loc[5] <= 'X':
        lon += (ord(loc[4]) - 65) * (2.0 / 24.0) + (2.0 / 24.0) / 2.0
        lat += (ord(loc[5]) - 65) * (1.0 / 24.0) + (1.0 / 24.0) / 2.0
    else:
        # Centre of the four-character square.
        lon += 1.0
        lat += 0.5

    return lat, lon


def _adif_location(value, is_lat):
    """ADIF Location: direction, three-digit degrees, minutes to 3dp.

    e.g. "N051 23.750". Not decimal degrees — ADIF specifies this
    format, and QRZ takes what it is given without correcting it, so
    getting it wrong would put the QSO somewhere else entirely rather
    than being rejected.
    """
    hemi = ("N" if value >= 0 else "S") if is_lat else ("E" if value >= 0 else "W")
    value = abs(value)
    degrees = int(value)
    minutes = (value - degrees) * 60.0
    # Rounding can carry into the next degree at the boundary.
    if round(minutes, 3) >= 60.0:
        degrees += 1
        minutes = 0.0
    return "%s%03d %06.3f" % (hemi, degrees, minutes)


def _build_qrz_adif(api_key, action, call, band, mode, qso_date, time_on,''',
        "Maidenhead + ADIF Location helpers",
    )

    # ── 2. send them with the square ─────────────────────────────
    src = apply(
        src,
        '        ps += "<my_gridsquare:" + str(len(my_gridsquare)) + ">" + str(my_gridsquare)\n'
        '    ps += "<eor>"',
        '        ps += "<my_gridsquare:" + str(len(my_gridsquare)) + ">" + str(my_gridsquare)\n'
        '        # QRZ does not derive coordinates from the square on upload\n'
        '        # (confirmed by QRZ support), so sending MY_GRIDSQUARE alone\n'
        '        # left the QSO labelled with the portable square but still\n'
        '        # positioned at the account\'s home coordinates. Anything\n'
        '        # omitted falls back to the profile silently, so the position\n'
        '        # has to be stated explicitly for the override to mean\n'
        '        # anything.\n'
        '        centre = _maidenhead_centre(my_gridsquare)\n'
        '        if centre:\n'
        '            my_lat = _adif_location(centre[0], True)\n'
        '            my_lon = _adif_location(centre[1], False)\n'
        '            ps += "<my_lat:" + str(len(my_lat)) + ">" + my_lat\n'
        '            ps += "<my_lon:" + str(len(my_lon)) + ">" + my_lon\n'
        '    ps += "<eor>"',
        "send MY_LAT / MY_LON",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_latlon"))
    TARGET.write_text(src)

    print(f"\n  lynx_notifications.py: {before} -> {len(src.splitlines())}")
    print("\n  Now: python3 -m py_compile lynx_notifications.py")


if __name__ == "__main__":
    main()
