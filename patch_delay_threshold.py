#!/usr/bin/env python3
"""
patch_delay_threshold.py

Raises the decoder-health delay threshold from 3 to 6 seconds.

WHAT WAS HAPPENING
------------------
mpv_decoder_health_monitor() restarts mpv when the gap between playback
and the buffered position stays at or above DELAY_THRESHOLD_SECS for two
consecutive checks, two seconds apart. That threshold was 3.0.

A Slave in perfectly good health sits at 2.6 to 2.7 seconds of cache —
measured repeatedly today, with no underrun, no errors and the clock
advancing in step. Normal operation was living a tenth of a second below
the line, so ordinary fluctuation crossed it and mpv was restarted while
nothing was wrong.

Observed live: fifteen restarts in a few minutes, each confirming
rendering in 0.4 to 6.9 seconds and then being restarted again, until
the monitor's own circuit breaker tripped after five in 300 seconds and
backed off — which is what finally allowed a stable picture. The visible
symptom was a long series of stills before video settled.

The restart also makes things worse rather than better: it discards the
buffer and forces a fresh acquisition, which on a five-second GOP costs
more delay than it removes.

WHY NOT A STARTUP GRACE PERIOD
------------------------------
There already is one, and it is generous: STARTUP_GRACE_SECS is 12.0,
with no evaluation at all during it. These restarts happened after that
expired, in steady state. The grace period was never the gap.

WHY 6
-----
Roughly double a healthy working delay, so normal variation cannot reach
it, while staying far below DELAY_EMERGENCY_THRESHOLD_SECS at 20.0,
which catches a gap that is genuinely running away.

It should also help through a fade. Delay grows when data arrives
raggedly, and restarting mid-fade discards the buffer and guarantees a
black screen, where riding it out can recover cleanly when the signal
comes back.

Run from the repo root:  python3 patch_delay_threshold.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")

OLD = "    DELAY_THRESHOLD_SECS = 3.0   # gap between playback and buffered position..."

NEW = ("    DELAY_THRESHOLD_SECS = 6.0   # gap between playback and buffered position...\n"
       "                                 # Was 3.0, which a healthy stream could reach on its own:\n"
       "                                 # a Slave in good health measures 2.6-2.7s of cache with\n"
       "                                 # no underrun and the clock advancing normally, so routine\n"
       "                                 # variation crossed the line and restarted mpv for no\n"
       "                                 # reason. Observed as fifteen restarts in a few minutes,\n"
       "                                 # each rendering successfully before being restarted again,\n"
       "                                 # until the circuit breaker below backed off and let a\n"
       "                                 # picture settle. The restart is also counterproductive: it\n"
       "                                 # discards the buffer and forces a fresh acquisition, which\n"
       "                                 # on a five-second GOP costs more delay than it recovers.\n"
       "                                 # 6.0 is about double a healthy working delay and still far\n"
       "                                 # below the 20s emergency threshold that catches a gap\n"
       "                                 # genuinely running away...")


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")

    src = TARGET.read_text()

    if "DELAY_THRESHOLD_SECS = 6.0" in src:
        sys.exit("ABORT: already patched")

    n = src.count(OLD)
    if n != 1:
        sys.exit(f"ABORT: anchor found {n} times, expected exactly 1")

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_delay"))
    TARGET.write_text(src.replace(OLD, NEW, 1))

    print("  ok  DELAY_THRESHOLD_SECS 3.0 -> 6.0")
    print("\n  Now: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
