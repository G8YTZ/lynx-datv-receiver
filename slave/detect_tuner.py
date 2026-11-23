#!/usr/bin/env python3
"""Which tuner is plugged into this Remote Rx.

Both tuners are an FTDI FT2232H and both appear as USB 0403:6010, so
the vendor and product IDs cannot tell them apart. The USB product
STRING can:

    Minitiouner          (BATC's own string, or a generic FTDI one)
    CombiTuner-Express   the Knucker

Read straight from sysfs rather than by shelling out to lsusb, which
is not installed on a Lite image, and without opening the device at
all: claiming the interface while the other tuner's software has it
makes CombiTunerExpress segfault rather than complain, which is a
miserable thing to debug on a machine up a ladder.

Only the Knucker is identified positively. Anything else is reported
as a Minitiouner, so an unknown or missing product string lands on the
behaviour every existing Remote Rx already has.

Run it with no arguments to print the kind, which is how the service
wrapper chooses a source module:

    KIND=$(python3 .../detect_tuner.py)
"""

import glob
import os
import sys

USB_ID = ("0403", "6010")

# Substrings of the USB product string, lower case. First match wins.
SIGNATURES = (
    ("combituner", "knucker"),
    ("minitiouner", "minitiouner"),
)


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


def detect():
    """(kind, product_string, found_any).

    kind is "knucker" or "minitiouner". found_any is False when no
    FTDI device is present at all, which is worth telling apart from
    "a tuner we could not identify": one means nothing is plugged in,
    the other means something is.
    """
    for d in sorted(glob.glob("/sys/bus/usb/devices/*/")):
        if (_read(os.path.join(d, "idVendor")),
                _read(os.path.join(d, "idProduct"))) != USB_ID:
            continue
        product = _read(os.path.join(d, "product"))
        low = product.lower()
        for needle, kind in SIGNATURES:
            if needle in low:
                return kind, product, True
        # An FT2232H that says something else. Every Remote Rx built
        # so far is a Minitiouner, so that is the safe assumption.
        return "minitiouner", product, True
    return "minitiouner", "", False


def main():
    kind, product, found = detect()
    if "-v" in sys.argv or "--verbose" in sys.argv:
        if not found:
            print("no FTDI 0403:6010 device found; assuming minitiouner",
                  file=sys.stderr)
        else:
            print(f"USB product {product!r} -> {kind}", file=sys.stderr)
    print(kind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
