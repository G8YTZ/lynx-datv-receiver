#!/usr/bin/env python3
"""
patch_slave_card_order.py

Moves the Slave Receivers card above Network Streams, so the running
order is RF presets -> Slaves -> Streams. Slaves are checked far more
often than the BATC list, so they belong nearer the top.

Pure HTML move: the block is cut from below the Streams card and
inserted above it, character for character. No JavaScript, no handlers,
no backend. loadSlaves() finds the container by id, so where the card
sits on the page is irrelevant to it.

Run from ~/lynx:  python3 patch_slave_card_order.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")

CARD = """
            <!-- Slave Receivers -->
            <div class="card mt-3">
                <div class="card-header">&#x1F4E1; Slave Receivers</div>
                <div class="card-body p-0">
                    <div id="slave-list" style="max-height: 300px; overflow-y: auto;">
                        <div class="text-muted small p-3">Loading Slaves...</div>
                    </div>
                </div>
            </div>
"""


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from ~/lynx")

    src = TARGET.read_text()
    before = len(src.splitlines())

    if CARD not in src:
        sys.exit("ABORT: Slave card block not found in its expected form — "
                 "has it already been moved or edited by hand?")
    if src.count(CARD) != 1:
        sys.exit(f"ABORT: Slave card block found {src.count(CARD)} times, expected 1")

    # Cut it out.
    src = src.replace(CARD, "", 1)

    # Put it back above the Streams card.
    anchor = "            <!-- Streams -->\n"
    n = src.count(anchor)
    if n != 1:
        sys.exit(f"ABORT: Streams card anchor found {n} times, expected 1 — "
                 "file left unchanged apart from the cut, DO NOT SAVE")
    src = src.replace(anchor, CARD.lstrip("\n") + "\n" + anchor, 1)

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak3"))
    TARGET.write_text(src)

    after = len(src.splitlines())
    print(f"  moved Slave Receivers card above Network Streams")
    print(f"  lynx_app.py: {before} -> {after} lines (expect no change)")
    print("  backup: lynx_app.py.bak3")
    print("\nNow: python3 -m py_compile lynx_app.py && git diff --stat")


if __name__ == "__main__":
    main()
