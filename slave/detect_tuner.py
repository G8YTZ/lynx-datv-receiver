#!/usr/bin/env python3
"""Which tuner is plugged into this Remote Rx.

Identified by the USB product STRING, not by the vendor and product
IDs, because the Minitiouner and the Knucker share a pair of IDs:

    0403:6010   FTDI FT2232H    Minitiouner AND Knucker
    2e8a:ba2c   BATC PicoTuner  (the USB one, RP2040)

The product string tells them apart:

    USB <-> NIM tuner    the Minitiouner - note that it does NOT say
                         "Minitiouner" anywhere          [hardware]
    MiniTiouner-Express  the Express variant             [BATC]
    CombiTuner-Express   the Knucker                     [hardware]
    BATC PicoTuner       the USB Picotuner               [hardware]

[hardware]: read off G8YTZ's own cards, 23-24 Nov 2026.
[BATC]: from the BATC's own udev rule in Ryde's longmynd
(ryde-build/longmynd/minitiouner.rules), which also carries the
Minitiouner and PicoTuner strings above and so corroborates them.

Read straight from sysfs rather than by shelling out to lsusb, which
is not installed on a Lite image, and without opening the device at
all: claiming the interface while the other tuner's software has it
makes CombiTunerExpress segfault rather than complain, which is a
miserable thing to debug on a machine up a ladder.

Anything unrecognised is reported as a Minitiouner, so an unknown or
missing product string lands on the behaviour every existing Remote Rx
already has.

A supported tuner always wins over an unsupported one. Plug a Picotuner
into a working Minitiouner card and the Minitiouner keeps the site on
air, which is the right way round for a wrong guess.

Run it with no arguments to print the kind, which is how the service
wrapper chooses a source module:

    KIND=$(python3 .../detect_tuner.py)
"""

import glob
import os
import sys

SYSFS_DEVICES = "/sys/bus/usb/devices/*/"

# The FTDI chip both the Minitiouner and the Knucker are built on. Used
# only as a last resort, for an FT2232H whose product string says
# something nobody here recognises.
FTDI_ID = ("0403", "6010")

# Substrings of the USB product string, lower case, first match wins.
#
# The flag is PREFERENCE, not capability. This module says only what is
# plugged in; whether there is a driver for it on this card is the
# wrapper's business, since the wrapper is what looks for one. A
# preferred=False tuner is one to fall back to when nothing better is
# present - so plugging a PicoTuner into a working Minitiouner card
# cannot take the site off the air, whether or not a PicoTuner longmynd
# has been built there.
SIGNATURES = (
    # (needle, kind, preferred)
    ("combituner", "knucker", True),
    # What a real Minitiouner actually calls itself. Confirmed on
    # hardware 24 Nov 2026; before that this relied entirely on the
    # FTDI fallback below, which gave the right answer for the wrong
    # reason. "minitiouner" is kept in case a board somewhere does say
    # it, and costs nothing.
    ("nim tuner", "minitiouner", True),
    # The Express variant, named in the BATC's own udev rule alongside
    # the other two. Reached the right answer through the FTDI fallback
    # before; positive identification is better.
    ("minitiouner-express", "minitiouner", True),
    ("minitiouner", "minitiouner", True),
    ("picotuner", "picotuner", False),
)

# Kinds that win when more than one tuner is plugged in.
PREFERRED = tuple(k for _n, k, s in SIGNATURES if s)


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def detect(root=None):
    """(kind, product_string, found_any).

    kind is "minitiouner", "knucker" or "picotuner". found_any is False
    when no tuner was recognised at all, which is worth telling apart
    from "a tuner we could not identify": one means nothing is plugged
    in, the other means something is.

    This says what is FITTED. It does not say whether this card can
    drive it - a PicoTuner needs Ryde's longmynd, built by
    install.sh --picotuner, and looking for that is the wrapper's job.

    root overrides where sysfs is read from, for the tests.
    """
    pattern = SYSFS_DEVICES if root is None else os.path.join(
        root, "sys", "bus", "usb", "devices", "*/")
    second_best = None      # remembered, but only used if nothing better
    ftdi = None             # an FT2232H with a string we do not know
    for d in sorted(glob.glob(pattern)):
        product = _read(os.path.join(d, "product"))
        low = product.lower()
        for needle, kind, preferred in SIGNATURES:
            if needle not in low:
                continue
            if preferred:
                # Nothing beats a preferred tuner, so stop here.
                return kind, product, True
            if second_best is None:
                second_best = (kind, product, True)
            break
        else:
            ids = (_read(os.path.join(d, "idVendor")),
                   _read(os.path.join(d, "idProduct")))
            if ids == FTDI_ID and ftdi is None:
                ftdi = ("minitiouner", product, True)
    if ftdi is not None:
        # An FT2232H that says something else. Every Remote Rx built so
        # far is a Minitiouner, so that is the safe assumption.
        return ftdi
    if second_best is not None:
        return second_best
    return "minitiouner", "", False


def main():
    kind, product, found = detect()
    if "-v" in sys.argv or "--verbose" in sys.argv:
        if not found:
            print("no tuner found on USB; assuming minitiouner",
                  file=sys.stderr)
        else:
            print(f"USB product {product!r} -> {kind}", file=sys.stderr)
    print(kind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
