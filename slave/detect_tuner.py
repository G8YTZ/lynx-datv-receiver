#!/usr/bin/env python3
"""Which tuner is plugged into this Remote Rx.

Identified by the USB product STRING, not by the vendor and product
IDs, because the two supported tuners share a pair of IDs:

    0403:6010   FTDI FT2232H    Minitiouner AND Knucker
    2e8a:ba2c   BATC PicoTuner  (the USB one, RP2040)

The product string tells all three apart:

    Minitiouner          BATC's own string, or a generic FTDI one
    CombiTuner-Express   the Knucker
    BATC PicoTuner       the USB Picotuner

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
# supported=False means Lynx can see the tuner and name it but cannot
# drive it: the Remote Rx then says so and stays reachable, rather than
# pretending to be something else.
SIGNATURES = (
    # (needle, kind, supported)
    ("combituner", "knucker", True),
    ("minitiouner", "minitiouner", True),
    ("picotuner", "picotuner", False),
)

# Kinds this Remote Rx can actually drive. The wrapper maps a kind to
# <kind>_source.py; an unsupported kind gets a source module that
# reports the fact and keeps the heartbeat going.
SUPPORTED = tuple(k for _n, k, s in SIGNATURES if s)


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

    root overrides where sysfs is read from, for the tests.
    """
    pattern = SYSFS_DEVICES if root is None else os.path.join(
        root, "sys", "bus", "usb", "devices", "*/")
    unsupported = None      # remembered, but only used if nothing better
    ftdi = None             # an FT2232H with a string we do not know
    for d in sorted(glob.glob(pattern)):
        product = _read(os.path.join(d, "product"))
        low = product.lower()
        for needle, kind, supported in SIGNATURES:
            if needle not in low:
                continue
            if supported:
                # A tuner we can drive. Nothing beats it, so stop here.
                return kind, product, True
            if unsupported is None:
                unsupported = (kind, product, True)
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
    if unsupported is not None:
        return unsupported
    return "minitiouner", "", False


def main():
    kind, product, found = detect()
    if "-v" in sys.argv or "--verbose" in sys.argv:
        if not found:
            print("no tuner found on USB; assuming minitiouner",
                  file=sys.stderr)
        elif kind not in SUPPORTED:
            print(f"USB product {product!r} -> {kind}, which this Remote Rx "
                  f"cannot drive", file=sys.stderr)
        else:
            print(f"USB product {product!r} -> {kind}", file=sys.stderr)
    print(kind)
    return 0


if __name__ == "__main__":
    sys.exit(main())
