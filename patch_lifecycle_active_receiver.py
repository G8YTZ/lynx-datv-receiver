#!/usr/bin/env python3
"""
patch_lifecycle_active_receiver.py

Makes the RF lifecycle monitor follow whichever receiver is displayed.

TWO THINGS, ONE CHANGE
----------------------
First, a correction. Now that a displayed Slave is current_mode "rf",
rf_mpv_lifecycle_monitor() runs for it — but read the LOCAL tuner's
lock to decide whether there was anything to show. So a local Picotuner
losing lock would have stopped mpv while a Slave was playing perfectly
well, and a local tuner regaining lock would have restarted it onto the
wrong source. Introduced by the mode change, caught before it bit.

Second, the point of the exercise. A Slave losing signal now stops mpv
and regaining it starts mpv, exactly as Rx 1 has always done. Without
that, nothing tells mpv the transmission ended: it keeps its reference
frames from a stream that stopped, and when the signal returns it
paints new partial data over a stale picture until the next keyframe —
observed as roughly twenty seconds of smeared stills before video
settled, with a five-second GOP.

Ryde does not have this problem because it is a different design
throughout: VLC reading Longmynd's transport stream from a file
descriptor, with none of the low-latency tuning applied here. Not a
flag Lynx is missing — a player that is handed a continuous pipe rather
than one that has to be told when its source went away.

THE PORT, TOO
-------------
The restart calls chose their own port: combiner output if diversity,
otherwise receiver A. That duplicated a decision current_rf_target_port()
already makes, and duplicating it is precisely what that function's
docstring documents going wrong before. Now it asks.

Run from the repo root:  python3 patch_lifecycle_active_receiver.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


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

    if "def active_receiver" not in src:
        sys.exit("ABORT: receiver registry not applied")
    if "_lifecycle_active" in src:
        sys.exit("ABORT: already patched")

    # ── 1. watch the displayed receiver's lock ───────────────────
    src = apply(
        src,
        '            raw_locked = picotuner_state.get("locked", False) or \\\n'
        '                         (diversity_enabled and picotuner_state_b.get("locked", False))',
        '            # Whichever receiver is actually on screen. Reading the\n'
        '            # local tuner here would stop mpv when IT lost lock while\n'
        '            # a Slave was playing quite happily, and restart it onto\n'
        '            # the wrong source when it came back.\n'
        '            #\n'
        '            # This is also what gives a Slave a clean restart when a\n'
        '            # transmission ends. Without it nothing tells mpv the\n'
        '            # source went away: it holds reference frames from a\n'
        '            # stream that stopped and smears them over the new one\n'
        '            # until a keyframe arrives.\n'
        '            _lifecycle_active = active_receiver()\n'
        '            raw_locked = (_lifecycle_active.get("locked", False)\n'
        '                          if _lifecycle_active else False) or \\\n'
        '                         (diversity_enabled and picotuner_state_b.get("locked", False))',
        "lifecycle watches the displayed receiver",
    )

    # ── 2. and ask for the port rather than deciding again ───────
    src = apply(
        src,
        "                            if diversity_enabled:\n"
        "                                div_cfg = config['diversity']\n"
        "                                restart_mpv(f\"udp://@:{div_cfg['combiner_out_port']}\")\n"
        "                            else:\n"
        "                                cfg = config['picotuner']\n"
        "                                restart_mpv(f\"udp://@:{picotuner_ts_port('a', cfg)}\")",
        "                            # Same answer as before for diversity and\n"
        "                            # for receiver A, and the right one for a\n"
        "                            # Slave. Asking beats deciding again:\n"
        "                            # current_rf_target_port() exists because\n"
        "                            # call sites making this choice themselves\n"
        "                            # is how one of them ended up recovering\n"
        "                            # onto the wrong receiver's port.\n"
        "                            restart_mpv(f\"udp://@:{current_rf_target_port()}\")",
        "lifecycle asks for the port",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_life"))
    TARGET.write_text(src)

    print(f"\n  lynx_app.py: {before} -> {len(src.splitlines())}")
    print("\n  Now: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
