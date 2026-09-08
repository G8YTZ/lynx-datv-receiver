#!/usr/bin/env python3
"""
patch_overlay_remote.py

Makes the OSD draw a Slave the way it draws a local receiver.

THE PROBLEM
-----------
A Slave plays through the stream path, so lynx.mode arrives as "stream"
and every branch in this file draws the stream layout — protocol and
codec, no frequency, no MER, no margin. The data is all there; the
overlay is simply being told it is watching a stream.

THE APPROACH
------------
Not a third case in each of the six mode branches. Instead the state
dict is filled from the selected Slave's record before any of them run,
and mode is set to "rf". Every branch downstream then works unchanged,
because a Slave genuinely is a receiver and now sends the same fields
under the same names — which was the whole point of the quality port.

Six branches taught a new case is six chances to get it wrong, in code
that decides whether the screen shows video or the desktop. One
substitution at the top is one chance, and it either fills the state or
it does not.

mpv_running_for_rf is set true deliberately. It feeds genuinely_locked,
which drives whether the picture is covered. It means "RF video is
actually up", and for a selected Slave it is — mpv is running and
playing the relay's output. Leaving it false would cover live video.

Diversity and tri_watch state are cleared rather than left as they were:
those describe two local tuners, a Slave has one receiver, and stale
values would draw a third row or a searching indicator for hardware
that has nothing to do with what is on screen.

Run from the repo root:  python3 patch_overlay_remote.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_overlay.py")


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

    if "_remote_display" in src:
        sys.exit("ABORT: already patched")

    # ── 1. somewhere to hold it, reset every poll ────────────────
    src = apply(
        src,
        "        raw_online = False\n        try:",
        "        raw_online = False\n"
        "        # The Slave whose video is on screen, if it is a Slave.\n"
        "        # Reset every poll so a stale one cannot outlive the\n"
        "        # selection that produced it.\n"
        "        _remote_display = None\n"
        "        try:",
        "_remote_display holder",
    )

    # ── 2. find it while the response is in hand ─────────────────
    src = apply(
        src,
        "            state[\"mode\"]      = lynx.get('mode', 'idle')",
        "            state[\"mode\"]      = lynx.get('mode', 'idle')\n"
        "            # Matched on index rather than position in the list:\n"
        "            # a disabled Slave still has a record, so the two are\n"
        "            # not the same thing.\n"
        "            if lynx.get('stream_is_remote') and lynx.get('remote_selected') is not None:\n"
        "                for _rem in data.get('remotes', []):\n"
        "                    if _rem.get('index') == lynx.get('remote_selected'):\n"
        "                        _remote_display = _rem\n"
        "                        break",
        "find the selected Slave",
    )

    # ── 3. substitute, after online has settled ──────────────────
    src = apply(
        src,
        "        if any(_raw_online_history):\n"
        "            state[\"online\"] = True\n"
        "        elif not any(_raw_online_history) and len(_raw_online_history) >= ONLINE_STABLE_POLLS:\n"
        "            state[\"online\"] = False\n",
        "        if any(_raw_online_history):\n"
        "            state[\"online\"] = True\n"
        "        elif not any(_raw_online_history) and len(_raw_online_history) >= ONLINE_STABLE_POLLS:\n"
        "            state[\"online\"] = False\n"
        "\n"
        "        # ── A Slave is a receiver, so draw it as one ──────────\n"
        "        # Applied last, after online and lock have settled from\n"
        "        # the local tuner: those describe hardware in this box,\n"
        "        # and none of it is what is on screen right now.\n"
        "        if _remote_display is not None:\n"
        "            rem = _remote_display\n"
        "            state[\"mode\"] = \"rf\"\n"
        "            # The Slave's own reachability, not the Picotuner's.\n"
        "            state[\"online\"] = bool(rem.get('online'))\n"
        "            state[\"locked\"] = bool(rem.get('locked'))\n"
        "            # mpv is running and playing the relay's output, which\n"
        "            # is what this flag means. False here would cover live\n"
        "            # video with the transition cover and leave it there.\n"
        "            state[\"mpv_running_for_rf\"] = True\n"
        "            state[\"callsign\"] = rem.get('callsign', '')\n"
        "            state[\"callsign_name\"] = rem.get('callsign_name', '')\n"
        "            state[\"frequency\"] = rem.get('frequency', '')\n"
        "            # A Slave reports what it is tuned to. Any converter\n"
        "            # at its end is its business, and inventing a downlink\n"
        "            # frequency from this end would be a guess.\n"
        "            state[\"downlink_frequency\"] = None\n"
        "            state[\"mer\"] = rem.get('mer', '')\n"
        "            state[\"margin\"] = rem.get('margin', '')\n"
        "            # dBm arrives directly, so the level approximation the\n"
        "            # RF path falls back to is not wanted here.\n"
        "            state[\"level\"] = ''\n"
        "            state[\"dbm\"] = rem.get('dbm', '')\n"
        "            state[\"modcod\"] = rem.get('modcod', '')\n"
        "            state[\"codec\"] = rem.get('codec', '')\n"
        "            state[\"audio_codec\"] = rem.get('audio_codec', '')\n"
        "            state[\"programme\"] = rem.get('programme', '')\n"
        "            if rem.get('symbol_rate'):\n"
        "                state[\"sr_ks\"] = rem['symbol_rate']\n"
        "            # Two local tuners' worth of state, describing hardware\n"
        "            # that is not the source. Left set, it would draw a\n"
        "            # diversity row or a tri_watch indicator over a Slave.\n"
        "            state[\"diversity_enabled\"] = False\n"
        "            state[\"diversity_stats\"] = {}\n"
        "            state[\"tri_watch_show_searching_rx2\"] = False\n"
        "            state[\"locked_via\"] = \"a\"\n",
        "substitute Slave into the RF state",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_remote"))
    TARGET.write_text(src)

    after = len(src.splitlines())
    print(f"\n  lynx_overlay.py: {before} -> {after} lines (+{after - before})")
    print("\nNow: python3 -m py_compile lynx_overlay.py")


if __name__ == "__main__":
    main()
