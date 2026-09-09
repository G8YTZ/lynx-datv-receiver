#!/usr/bin/env python3
"""
patch_combiner_full_datagrams.py

Stops the diversity combiner sending short datagrams.

WHAT IT DOES NOW
----------------
The output flush chunks whatever the decider produced in that pass into
1316-byte pieces:

    CHUNK = TS_PACKET_SIZE * 7
    for i in range(0, len(out_buf), CHUNK):
        out_sock.sendto(bytes(out_buf[i:i + CHUNK]), ...)

out_buf holds however many packets happened to be ready, which is rarely
a multiple of seven, so the last chunk of every pass goes out short.
Measured on the wire with diversity running and both receivers locked:

    153 x 1316    28 x 1128    14 x 188    2 x 376    2 x 564    1 x 940

Roughly a quarter of datagrams undersized. Every length is a multiple of
188, so alignment is correct and packets are never split — this is not
the Longmynd fault. It is under-filling.

WHY IT MATTERS
--------------
A Picotuner sends a steady 1316 every time. The combiner sends a mixture,
which means more datagrams per second than necessary and an uneven
arrival pattern into a player deliberately tuned for minimum latency with
a tight buffer. That jitter is a plausible contributor to the periodic
re-syncs diversity has always shown.

THE CHANGE
----------
Whole datagrams are sent and the remainder is carried to the next pass
instead of going out short. At most six packets are held, about 1.1 kB,
a couple of milliseconds at these rates.

A held tail is flushed anyway if nothing has completed it within
TAIL_MAX_HOLD_SECS. Without that, the last few packets of a transmission
would sit in the buffer indefinitely once the signal stopped — a small
thing, but it would be a silent one, and those are the ones that take an
evening to find later.

Run from the repo root:  python3 patch_combiner_full_datagrams.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("diversity_combiner_pcr.py")


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

    if "out_tail" in src:
        sys.exit("ABORT: already patched")

    # ── 1. somewhere for the remainder to live ───────────────────
    src = apply(
        src,
        "    stall_last_logged = 0\n    last_backlog_log = 0\n",
        "    stall_last_logged = 0\n"
        "    last_backlog_log = 0\n"
        "\n"
        "    # Packets left over when a pass does not produce a whole\n"
        "    # multiple of seven. Carried to the next pass rather than sent\n"
        "    # short, so every datagram leaving here is the same size a\n"
        "    # Picotuner sends. Lives outside the loop because out_buf is\n"
        "    # rebuilt on every iteration.\n"
        "    out_tail = bytearray()\n"
        "    tail_since = 0.0\n"
        "    TAIL_MAX_HOLD_SECS = 0.25   # never hold a partial datagram longer\n"
        "                                # than this: at the end of a\n"
        "                                # transmission nothing arrives to\n"
        "                                # complete it, and those packets\n"
        "                                # would otherwise sit here for good.\n",
        "tail buffer",
    )

    # ── 2. send whole datagrams, carry the rest ──────────────────
    src = apply(
        src,
        "        if out_buf:\n"
        "            CHUNK = TS_PACKET_SIZE * 7\n"
        "            for i in range(0, len(out_buf), CHUNK):\n"
        "                out_sock.sendto(bytes(out_buf[i:i + CHUNK]), (out_ip, out_port))\n",
        "        if out_buf or out_tail:\n"
        "            CHUNK = TS_PACKET_SIZE * 7\n"
        "            if out_buf:\n"
        "                if not out_tail:\n"
        "                    tail_since = now\n"
        "                out_tail += out_buf\n"
        "\n"
        "            sent = 0\n"
        "            while len(out_tail) - sent >= CHUNK:\n"
        "                out_sock.sendto(bytes(out_tail[sent:sent + CHUNK]),\n"
        "                                (out_ip, out_port))\n"
        "                sent += CHUNK\n"
        "            if sent:\n"
        "                del out_tail[:sent]\n"
        "                tail_since = now\n"
        "\n"
        "            # Whatever is left is a partial datagram. Held for the\n"
        "            # next pass to complete — unless nothing has, in which\n"
        "            # case the transmission has probably ended and it goes\n"
        "            # as it is rather than being lost.\n"
        "            if out_tail and (now - tail_since) >= TAIL_MAX_HOLD_SECS:\n"
        "                out_sock.sendto(bytes(out_tail), (out_ip, out_port))\n"
        "                out_tail.clear()\n",
        "carry the remainder",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_chunk"))
    TARGET.write_text(src)

    print(f"\n  diversity_combiner_pcr.py: {before} -> {len(src.splitlines())}")
    print("\n  Now: python3 -m py_compile diversity_combiner_pcr.py")


if __name__ == "__main__":
    main()
