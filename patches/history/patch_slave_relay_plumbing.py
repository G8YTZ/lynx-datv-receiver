#!/usr/bin/env python3
"""
patch_slave_relay_plumbing.py

Wires lynx_relay.py into lynx_app.py — plumbing only, no behaviour change.

After this patch:
  * each Slave's source IP is captured from its status packets and held
    in its state record (the address was already being unpacked from
    recvfrom() and thrown away)
  * the relay is started at boot and told which address belongs to which
    Slave, so it counts video per Slave
  * /api/status remotes[] gains 'addr' and a 'video' block

Deliberately NOT included: selecting a Slave for display. That needs the
same tune_lock hand-off and transition-cover sequence _start_stream_impl
uses, and getting it wrong either deadlocks the lock or exposes the
desktop mid-switch. Separate patch, daylight.

Nothing here changes what plays. The relay forwards only when something
has been selected, and nothing selects anything yet.

Run from ~/lynx:  python3 patch_slave_relay_plumbing.py
"""

import re
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
        sys.exit(f"ABORT: {TARGET} not found — run this from ~/lynx")

    src = TARGET.read_text()
    original_lines = len(src.splitlines())

    if "lynx_relay" in src:
        sys.exit("ABORT: lynx_app.py already mentions lynx_relay — already patched?")

    # ── 1. import ────────────────────────────────────────────────
    src = apply(
        src,
        "import lynx_notifications\nimport lynx_gnss\nimport lynx_map\n",
        "import lynx_notifications\nimport lynx_gnss\nimport lynx_map\nimport lynx_relay\n",
        "import lynx_relay",
    )

    # ── 2. relay instance, beside the other remote constants ─────
    src = apply(
        src,
        "# One state record per configured Slave, built at startup and indexed\n",
        '''# Every Slave sends its video to this one port, and the relay tells
# them apart by source address — the same address that is already
# sending that Slave's status, so identity comes free and there is no
# per-Slave port to allocate, configure, or firewall.
REMOTE_VIDEO_INGRESS_PORT = 10998

# The relay forwards the selected Slave here, and nothing is selected
# until a later patch adds that path — so it currently receives, counts
# and discards. Kept as a module-level singleton because the socket must
# outlive any one request.
slave_relay = lynx_relay.SlaveVideoRelay(ingress_port=REMOTE_VIDEO_INGRESS_PORT)


def relay_refresh_sources():
    """Hand the relay the current address -> Slave index mapping.

    Called whenever an address is learned or changes. Only Slaves that
    have actually sent something appear, so an unheard-from Slave cannot
    claim video, and a Slave that moves (DHCP) is followed automatically
    on its next status packet.
    """
    try:
        slave_relay.set_sources({
            st["addr"]: i
            for i, st in enumerate(remote_states)
            if st.get("addr")
        })
    except Exception as e:
        print(f"[relay] could not refresh sources: {type(e).__name__}: {e}")


# One state record per configured Slave, built at startup and indexed
''',
        "relay instance + relay_refresh_sources()",
    )

    # ── 3. addr field on the state record ────────────────────────
    src = apply(
        src,
        '        "locator": "",\n        "last_seen": 0,\n    }',
        '        "locator": "",\n'
        '        # Where this Slave\'s packets come from, learned from its own\n'
        '        # status traffic rather than configured. The video relay uses\n'
        '        # it to tell one Slave\'s stream from another\'s. Empty until\n'
        '        # the first packet arrives.\n'
        '        "addr": "",\n'
        '        "last_seen": 0,\n    }',
        'state record "addr" field',
    )

    # ── 4. stamp the address in the monitor ──────────────────────
    src = apply(
        src,
        '            data, addr = sock.recvfrom(4096)\n'
        '            st["last_seen"] = time.time()\n',
        '            data, addr = sock.recvfrom(4096)\n'
        '            st["last_seen"] = time.time()\n'
        '            # Only on change: this runs on every heartbeat from every\n'
        '            # Slave, and rebuilding the relay map two or three times a\n'
        '            # second for no reason would take its lock needlessly.\n'
        '            if st["addr"] != addr[0]:\n'
        '                print(f"[remote {index}] source address {st[\'addr\'] or \'(none)\'} -> {addr[0]}")\n'
        '                st["addr"] = addr[0]\n'
        '                relay_refresh_sources()\n',
        "monitor stamps addr",
    )

    # ── 5. start the relay at boot ───────────────────────────────
    src = apply(
        src,
        '        threading.Thread(target=remote_source_monitor, args=(_i,),\n'
        '                         daemon=True).start()\n',
        '        threading.Thread(target=remote_source_monitor, args=(_i,),\n'
        '                         daemon=True).start()\n'
        '    # Started whenever any Slave is configured. A failed bind is not\n'
        '    # fatal — status, panels and every local source carry on exactly\n'
        '    # as before; only Slave video would be unavailable, and the reason\n'
        '    # is on the console and in /api/status rather than silent.\n'
        '    if remote_any_enabled():\n'
        '        if slave_relay.start():\n'
        '            print(f"[relay] Slave video ingress listening on {REMOTE_VIDEO_INGRESS_PORT}")\n'
        '        else:\n'
        '            print(f"[relay] NOT listening: {slave_relay.bind_error}")\n',
        "start relay at boot",
    )

    # ── 6. expose in /api/status ─────────────────────────────────
    src = apply(
        src,
        '                "status_port": st["status_port"],\n'
        '                "last_seen": st["last_seen"],\n',
        '                "status_port": st["status_port"],\n'
        '                "addr": st["addr"],\n'
        '                # Video presence, deliberately separate from "locked":\n'
        '                # a Slave can be heartbeating happily while its own\n'
        '                # receiver is unlocked and no TS is flowing, and the\n'
        '                # two states need telling apart on the panel.\n'
        '                "video": _relay_video_for(i),\n'
        '                "last_seen": st["last_seen"],\n',
        "/api/status remotes[] addr + video",
    )

    # helper for the above, placed with the other remote helpers
    src = apply(
        src,
        "def remote_online(i: int) -> bool:",
        '''def _relay_video_for(i: int) -> dict:
    """Per-Slave video counters, or a blank record if the relay is not
    running. Always the same shape so a consumer never has to test
    whether the key exists — the same reasoning as the remotes list
    itself always being present."""
    blank = {"live": False, "kbps": 0, "packets": 0}
    try:
        s = slave_relay.stats()
        v = s.get("sources", {}).get(i)
        if not v:
            return blank
        return {"live": v["live"], "kbps": v["kbps"], "packets": v["packets"]}
    except Exception:
        return blank


def remote_online(i: int) -> bool:''',
        "_relay_video_for() helper",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak"))
    TARGET.write_text(src)

    new_lines = len(src.splitlines())
    print(f"\n  lynx_app.py: {original_lines} -> {new_lines} lines (+{new_lines - original_lines})")
    print("  backup: lynx_app.py.bak")
    print("\nNow: python3 -m py_compile lynx_app.py && git diff --stat")


if __name__ == "__main__":
    main()
