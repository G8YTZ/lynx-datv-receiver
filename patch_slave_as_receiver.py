#!/usr/bin/env python3
"""
patch_slave_as_receiver.py

Step two: a Slave stops being a stream and becomes a receiver.

Four changes, which have to land together — each on its own would leave
the display or the watchdogs describing a different receiver from the
one on screen.

  1. current_rf_target_port() returns the relay's output port when a
     Slave is displayed. This one is load-bearing: the freeze-recovery
     watchdogs use it to decide where to point mpv after a restart, and
     that function's own docstring records what happened last time the
     answer was wrong — a whole tri_watch session recovering onto Rx1's
     port and never rendering.

  2. Selecting a Slave leaves current_mode as "rf". It still borrows
     start_stream() for the lock hand-off and transition cover, which
     are proven, but the mode is corrected once that returns. Every
     branch keyed on "rf" then applies: the RF OSD layout, the RF
     watchdogs, and in due course QRZ, the logbook and name display,
     none of which ever saw a Slave.

  3. /api/status publishes active_receiver, the single answer to "which
     receiver is on screen". stream_is_remote stays for now because the
     Stream panel reads it, but it is no longer the way anything
     identifies a Slave.

  4. The overlay keys off active_receiver rather than stream_is_remote.
     It has to: with the mode now "rf", stream_is_remote is false for a
     Slave, and without this change the OSD would draw a Slave using
     Rx 1's callsign and MER. Worse than before rather than better.

Run from the repo root:  python3 patch_slave_as_receiver.py
"""

import shutil
import sys
from pathlib import Path

APP = Path("lynx_app.py")
OVL = Path("lynx_overlay.py")


def apply(src: str, old: str, new: str, label: str) -> str:
    n = src.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected exactly 1")
    print(f"  ok  {label}")
    return src.replace(old, new, 1)


def main():
    for p in (APP, OVL):
        if not p.exists():
            sys.exit(f"ABORT: {p} not found — run this from the repo root")

    app = APP.read_text()
    ovl = OVL.read_text()

    if "def active_receiver_id" not in app:
        sys.exit("ABORT: receiver registry not applied — run patch_receiver_registry.py first")
    if '"active_receiver"' in app:
        sys.exit("ABORT: already patched")

    app_before, ovl_before = len(app.splitlines()), len(ovl.splitlines())

    # ── 1. the port a Slave plays from ───────────────────────────
    app = apply(
        app,
        "    cfg = config['picotuner']\n"
        "    if tri_watch_enabled and tri_watch_target_rcv == 2:\n"
        "        return cfg['ts_port_b']\n"
        "    elif diversity_enabled:\n"
        "        return config['diversity']['combiner_out_port']\n"
        "    else:\n"
        "        return cfg['ts_port']",
        "    cfg = config['picotuner']\n"
        "    # Checked before the local cases: a Slave being displayed is\n"
        "    # not a mode the receiver puts itself into, it is an explicit\n"
        "    # choice, and while it holds neither tri_watch nor diversity\n"
        "    # describes what is on screen. Getting this wrong would send\n"
        "    # a freeze recovery to a local tuner's port while a Slave is\n"
        "    # playing — the same shape of bug this function exists to\n"
        "    # prevent.\n"
        "    if receiver_is_remote(active_receiver_id() or 0):\n"
        "        return REMOTE_VIDEO_OUT_PORT\n"
        "    if tri_watch_enabled and tri_watch_target_rcv == 2:\n"
        "        return cfg['ts_port_b']\n"
        "    elif diversity_enabled:\n"
        "        return config['diversity']['combiner_out_port']\n"
        "    else:\n"
        "        return cfg['ts_port']",
        "current_rf_target_port knows Slaves",
    )

    # ── 2. a displayed Slave is RF ───────────────────────────────
    app = apply(
        app,
        '        "remote_index": _remote_resume_index,\n'
        '    })\n'
        '    return result',
        '        "remote_index": _remote_resume_index,\n'
        '    })\n'
        '    # start_stream() sets the mode to "stream" because that is\n'
        '    # what it does. A Slave is a receiver, so the mode is\n'
        '    # corrected here, once the machinery that actually needed\n'
        '    # borrowing — the lock hand-off and the transition cover —\n'
        '    # has done its work.\n'
        '    #\n'
        '    # Safe after the call: _start_stream_impl sets the mode\n'
        '    # before handing off to its background thread, and nothing\n'
        '    # in that thread touches it again.\n'
        '    global current_mode\n'
        '    current_mode = "rf"\n'
        '    return result',
        "displayed Slave is mode rf",
    )

    # ── 3. status publishes the one answer ───────────────────────
    app = apply(
        app,
        '            "remote_selected": slave_relay.selected(),',
        '            "remote_selected": slave_relay.selected(),\n'
        '            # Which receiver is on screen: 1 or 2 for the local\n'
        '            # Picotuner, 11+ for a Slave, null for a stream. The\n'
        '            # single answer, derived in one place, for anything\n'
        '            # that needs to know — panels, overlay, and whatever\n'
        '            # comes next.\n'
        '            "active_receiver": active_receiver_id(),',
        "status publishes active_receiver",
    )

    # ── 4. overlay follows the receiver, not the mode ────────────
    ovl = apply(
        ovl,
        "            if lynx.get('stream_is_remote') and lynx.get('remote_selected') is not None:\n"
        "                for _rem in data.get('remotes', []):\n"
        "                    if _rem.get('index') == lynx.get('remote_selected'):\n"
        "                        _remote_display = _rem\n"
        "                        break",
        "            # Keyed on which receiver is on screen rather than on\n"
        "            # the mode. A displayed Slave is now mode \"rf\" like any\n"
        "            # other receiver, so the old stream_is_remote test\n"
        "            # would never fire and the OSD would quietly show a\n"
        "            # Slave using Rx 1's callsign and MER.\n"
        "            _active = lynx.get('active_receiver')\n"
        "            if _active is not None and _active >= 11:\n"
        "                for _rem in data.get('remotes', []):\n"
        "                    if _rem.get('index') == _active - 11:\n"
        "                        _remote_display = _rem\n"
        "                        break",
        "overlay keys off active_receiver",
    )

    shutil.copy2(APP, APP.with_suffix(".py.bak_recv2"))
    shutil.copy2(OVL, OVL.with_suffix(".py.bak_recv2"))
    APP.write_text(app)
    OVL.write_text(ovl)

    print(f"\n  lynx_app.py:     {app_before} -> {len(app.splitlines())}")
    print(f"  lynx_overlay.py: {ovl_before} -> {len(ovl.splitlines())}")
    print("\n  Now: python3 -m py_compile lynx_app.py lynx_overlay.py")


if __name__ == "__main__":
    main()
