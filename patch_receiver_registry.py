#!/usr/bin/env python3
"""
patch_receiver_registry.py

Step one of treating Slaves as receivers rather than as streams.

Adds a numbering scheme and one place that answers "which receiver is on
screen". Nothing calls it yet — this patch changes no behaviour at all.
It exists so the consumers can be moved across one at a time afterwards,
each step testable on its own, rather than in one large change that
either works or does not.

WHY
---
A Slave is a DVB receiver. It demodulates, locks, reports MER and margin
and modcod, and sends MPEG-TS. The only reason it currently travels the
stream path is that the stream path was the quickest way to get its
video on screen.

That has cost five separate fixes, each correct in isolation and each a
symptom of the same misclassification: the wrong demuxer flags, no
analysis window, the stream OSD layout, a different panel row set, and a
resume path that did not know it existed. Logbook, QRZ and name display
still do not see a Slave at all, for the same reason.

None of those need fixing individually once a Slave is a receiver. They
work because they already work for Rx 1 and Rx 2.

NUMBERING
---------
    1, 2      local Picotuner receivers
    11 - 15   Slaves

Deliberately leaving 3 to 10 free: a WinterHill presents receivers 5 and
6 on its own, and numbering Slaves into that range would collide the
moment one is connected. Starting at 11 also makes a Slave obvious at a
glance in a log line, which a low number would not.

Five is a cap rather than a limit of the design. It bounds the UI, and a
sixth Slave gets a clear refusal instead of quietly not working.

Run from the repo root:  python3 patch_receiver_registry.py
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

    if "def receiver_state" in src:
        sys.exit("ABORT: already patched")
    if "slave_relay" not in src:
        sys.exit("ABORT: Slave relay patches not applied — this builds on them")

    src = apply(
        src,
        "def remote_online(i: int) -> bool:",
        '''# ── Receiver registry ────────────────────────────────────────────
#
# Every receiver Lynx can display, under one numbering:
#
#     1, 2      local Picotuner receivers
#     11 - 15   Slaves
#
# 3 to 10 are left free for a WinterHill, which presents its own
# receivers 5 and 6 and would collide with anything numbered into that
# range.
REMOTE_RECEIVER_ID_BASE = 11
MAX_REMOTE_RECEIVERS = 5


def receiver_state(rid: int):
    """The state record for one receiver, or None if there is no such
    receiver.

    Returns the live dict, not a copy — the monitor threads write to
    these, and a caller holding a snapshot would be reading a receiver's
    state from whenever it happened to ask. One record per receiver,
    one place that hands it out.
    """
    if rid == 1:
        return picotuner_state
    if rid == 2:
        return picotuner_state_b
    if REMOTE_RECEIVER_ID_BASE <= rid < REMOTE_RECEIVER_ID_BASE + MAX_REMOTE_RECEIVERS:
        i = rid - REMOTE_RECEIVER_ID_BASE
        if 0 <= i < len(remote_states):
            return remote_states[i]
    return None


def receiver_ids() -> list:
    """Every receiver that exists, in display order.

    Local receivers always exist, whether or not anything is connected
    to them — the hardware is either there or it is a fault worth
    showing. A Slave exists once it is configured, enabled or not, so a
    disabled one appears switched off rather than vanishing.
    """
    ids = [1, 2]
    for i in range(min(len(remote_states), MAX_REMOTE_RECEIVERS)):
        ids.append(REMOTE_RECEIVER_ID_BASE + i)
    return ids


def receiver_is_remote(rid: int) -> bool:
    return rid >= REMOTE_RECEIVER_ID_BASE


def active_receiver_id():
    """Which receiver is on screen, or None if a stream is.

    THE one derivation point. The port equivalent of this question,
    current_rf_target_port(), already exists and its docstring records
    what happened when it did not: the decision was duplicated across
    call sites, two of them were never taught about tri_watch, and a
    freeze recovery restarted mpv pointed at the wrong receiver's port
    for the whole of a tri_watch session. Same question, same reason to
    answer it in one place.

    Order matters. A Slave is checked first because selecting one is an
    explicit act by somebody at the front panel, where diversity and
    tri_watch are modes the receiver puts itself into.
    """
    try:
        sel = slave_relay.selected()
    except Exception:
        sel = None
    if sel is not None and current_mode in ("rf", "stream"):
        rid = REMOTE_RECEIVER_ID_BASE + sel
        # Only if it is genuinely what mpv is being fed. The relay keeps
        # its selection when the display moves to a local tuner, so the
        # selection alone does not mean a Slave is on screen.
        if current_stream_url == f"udp://@:{REMOTE_VIDEO_OUT_PORT}":
            return rid if receiver_state(rid) is not None else None

    if current_mode != "rf":
        return None
    if tri_watch_enabled and tri_watch_target_rcv == 2:
        return 2
    # Diversity combines both receivers into one output. Reported as 1
    # because that is the receiver whose tuning and callsign describe
    # what is being watched; rcv 2 is contributing, not being displayed.
    return 1


def active_receiver():
    """The state record for whatever is on screen, or None."""
    rid = active_receiver_id()
    return receiver_state(rid) if rid is not None else None


def remote_online(i: int) -> bool:''',
        "receiver registry",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_registry"))
    TARGET.write_text(src)

    after = len(src.splitlines())
    print(f"\n  lynx_app.py: {before} -> {after} lines (+{after - before})")
    print("\n  Nothing calls these yet — no behaviour change.")
    print("  Now: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
