#!/usr/bin/env python3
"""
patch_hdhr_card_tidy.py

Config page, HDHomeRun card: readable names, and a title that fits.

After patch_hdhr_two_boxes.py (24 Oct 2026, Lynx-DATV-Receiver-1):
  - each box's name field was empty, showing its default name only as
    placeholder text, dark grey on dark blue and hard to read;
  - the card was titled "DVB-T2 Tuner", with a US ATSC 3.0 box in it.

Now the name field holds the box's current name in ordinary text, so it
reads like every other field and can simply be typed over. A name left
at the default, or cleared, is saved as "no name given", so the default
can still follow the box (if it is replaced, say). The card is
"HDHomeRun Tuners", and its description mentions ATSC.

Run from the repo root:  python3 patch_hdhr_card_tidy.py
Then:                    python3 verify.py lynx_app.py
"""

import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


def apply(src, old, new, label, count=1):
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    src = TARGET.read_text()
    if "def hdhr_device_for" not in src:
        sys.exit("ABORT: patch_hdhr_two_boxes.py has not been applied")
    if "HDHomeRun Tuners</div>" in src:
        sys.exit("ABORT: already patched")

    src = apply(src,
                '<div class="card-header">&#x1F4FA; DVB-T2 Tuner (HDHomeRun)</div>',
                '<div class="card-header">&#x1F4FA; HDHomeRun Tuners</div>',
                "card title")
    src = apply(src,
                "                            A SiliconDust HDHomeRun on the network, used as a\n"
                "                            DVB-T/T2/C receiver. Found automatically on the\n"
                "                            same subnet - nothing needs configuring for one\n"
                "                            plugged into the local network.\n",
                "                            SiliconDust HDHomeRuns on the network: DVB-T/T2/C\n"
                "                            boxes, and ATSC boxes. Found automatically on the\n"
                "                            same subnet, and each tune goes to a box that can\n"
                "                            receive it. Name them here if you like.\n",
                "card description")
    src = apply(src,
                "                        inp.value = given[d.device_id] || '';\n",
                "                        // The current name, in ordinary text: placeholder\n"
                "                        // grey was too faint to read. Left at the default,\n"
                "                        // it saves as no name given (patch_hdhr_card_tidy.py).\n"
                "                        inp.dataset.def = d.default_name || '';\n"
                "                        inp.value = given[d.device_id] || d.default_name || d.name || '';\n",
                "card: name field shows the name")
    src = apply(src,
                "                    n[i.id.slice(10)] = i.value.trim();\n",
                "                    const v = i.value.trim();\n"
                "                    n[i.id.slice(10)] = (v === (i.dataset.def || '')) ? '' : v;\n",
                "card: the default saves as no name")

    TARGET.write_text(src)


if __name__ == "__main__":
    main()
