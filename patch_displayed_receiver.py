#!/usr/bin/env python3
"""
patch_displayed_receiver.py

Makes "which receiver is displayed" a fact rather than a guess.

THE BUG
-------
active_receiver_id() decided a Slave was on screen by testing whether
current_stream_url equalled the relay's output port. That variable is
set when a stream starts and never cleared when tuning back to RF —
nothing else needed it cleared, because nothing else asked that
question of it.

So after displaying a Slave once, every later RF tune still looked like
a Slave. Confirmed live: watching the local tuner, with the panel
correctly showing it, while active_receiver read 11 and mpv was pointed
at the relay's output port. Main appeared as a series of freeze frames
because mpv was genuinely playing the Slave.

It also explains something from earlier that was never accounted for:
stopping the Slave "fixed" the local tuner. Of course it did. The local
tuner was not being displayed at all.

THE FIX
-------
One variable, displayed_receiver_id, written wherever the displayed
source actually changes:

  * selecting a Slave sets it to that Slave
  * tuning to RF clears it, in all three places that switch to RF mode

Derivation reads it instead of inferring from a URL that nobody
maintains for the purpose. A guess that is right most of the time is
worse than a fact, because it fails silently and only under the
conditions nobody tests.

Run from the repo root:  python3 patch_displayed_receiver.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


def apply(src: str, old: str, new: str, label: str, count: int = 1) -> str:
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")

    src = TARGET.read_text()
    before = len(src.splitlines())

    if "displayed_receiver_id" in src:
        sys.exit("ABORT: already patched")
    if "def active_receiver_id" not in src:
        sys.exit("ABORT: receiver registry not applied")

    # ── 1. the variable ──────────────────────────────────────────
    src = apply(
        src,
        "REMOTE_RECEIVER_ID_BASE = 11\nMAX_REMOTE_RECEIVERS = 5",
        "REMOTE_RECEIVER_ID_BASE = 11\n"
        "MAX_REMOTE_RECEIVERS = 5\n"
        "\n"
        "# Which receiver is on screen, when it is one that had to be\n"
        "# chosen rather than one the receiver arrived at by itself.\n"
        "# None means a local tuner, and which local tuner is then a\n"
        "# question tri_watch and diversity already answer.\n"
        "#\n"
        "# Real state, deliberately. This was originally inferred from\n"
        "# current_stream_url still holding the relay's port, which is\n"
        "# true when a Slave is displayed and stays true afterwards,\n"
        "# because nothing clears it when tuning back to RF. The result\n"
        "# was a local tuner on the panel and a Slave on the screen.\n"
        "displayed_receiver_id = None",
        "displayed_receiver_id",
    )

    # ── 2. derive from it ────────────────────────────────────────
    src = apply(
        src,
        "    try:\n"
        "        sel = slave_relay.selected()\n"
        "    except Exception:\n"
        "        sel = None\n"
        "    if sel is not None and current_mode in (\"rf\", \"stream\"):\n"
        "        rid = REMOTE_RECEIVER_ID_BASE + sel\n"
        "        # Only if it is genuinely what mpv is being fed. The relay keeps\n"
        "        # its selection when the display moves to a local tuner, so the\n"
        "        # selection alone does not mean a Slave is on screen.\n"
        "        if current_stream_url == f\"udp://@:{REMOTE_VIDEO_OUT_PORT}\":\n"
        "            return rid if receiver_state(rid) is not None else None\n",
        "    # Set when a Slave is chosen, cleared when anything tunes to\n"
        "    # RF. The relay keeps its own selection across a switch to a\n"
        "    # local tuner, so what the relay is forwarding and what is on\n"
        "    # screen are different questions.\n"
        "    if displayed_receiver_id is not None:\n"
        "        if receiver_state(displayed_receiver_id) is not None:\n"
        "            return displayed_receiver_id\n"
        "        # Configured away underneath us — a Slave that no longer\n"
        "        # exists is not on screen, whatever was chosen earlier.\n"
        "        return None\n",
        "derive from state, not URL",
    )

    # ── 3. set it when a Slave is chosen ─────────────────────────
    src = apply(
        src,
        "    global current_mode\n    current_mode = \"rf\"\n    return result",
        "    global current_mode, displayed_receiver_id\n"
        "    current_mode = \"rf\"\n"
        "    displayed_receiver_id = REMOTE_RECEIVER_ID_BASE + index\n"
        "    return result",
        "set on Slave selection",
    )

    # ── 4. clear it wherever RF takes over ───────────────────────
    src = apply(
        src,
        "            current_mode = \"rf\"\n"
        "            set_converter_state(rcv, src_cfg['freq'], _lo,",
        "            current_mode = \"rf\"\n"
        "            # A local tuner is being displayed now, whatever was\n"
        "            # chosen before.\n"
        "            displayed_receiver_id = None\n"
        "            set_converter_state(rcv, src_cfg['freq'], _lo,",
        "clear on tri_watch/arbitrated RF",
    )

    src = apply(
        src,
        "    current_mode = \"rf\"\n    if req.lnb_lo_khz:",
        "    current_mode = \"rf\"\n"
        "    displayed_receiver_id = None\n"
        "    if req.lnb_lo_khz:",
        "clear on tune",
    )

    src = apply(
        src,
        "                global current_mode, current_preset\n"
        "                current_mode = \"rf\"",
        "                global current_mode, current_preset, displayed_receiver_id\n"
        "                current_mode = \"rf\"\n"
        "                displayed_receiver_id = None",
        "clear on resume-already-tuned",
    )

    # Both functions that clear it need it declared global. Without
    # that the assignment quietly creates a local and the clear never
    # happens — which is exactly the failure this patch is fixing, so
    # it would have been a poor one to repeat.
    src = apply(
        src,
        "def _tune_impl(req: TuneRequest):\n    global ",
        "def _tune_impl(req: TuneRequest):\n    global displayed_receiver_id\n    global ",
        "global in _tune_impl",
    )

    src = apply(
        src,
        "def _tri_watch_display_source(idx, src_cfg):\n",
        "def _tri_watch_display_source(idx, src_cfg):\n    global displayed_receiver_id\n",
        "global in _tri_watch_display_source",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_disp"))
    TARGET.write_text(src)

    print(f"\n  lynx_app.py: {before} -> {len(src.splitlines())}")
    print("\n  Now: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
