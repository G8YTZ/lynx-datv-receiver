#!/usr/bin/env python3
"""
patch_heartbeat_no_screen.py

No screen, no verdict: lynx_start.sh no longer treats an overlay that
has stopped drawing as wedged while there is no screen to draw on.

THE BUG
-------
The overlay heartbeat check restarts Lynx (and, on a receiver with full
passwordless sudo, hard-reboots it via SysRq) once the overlay has not
completed a frame for 30 s. It exists for a real fault: an overlay that
stays alive but stops reaching the screen.

But with no screen attached, nothing draws, by design: the compositor
stops asking for frames. Switching the monitor to another input, or
into standby, drops HDMI hot-plug, and 30 s later Lynx "recovered" from
a fault that was not there. Found on the ROCK 5T on 22 Oct 2026: one
restart per input switch, four in a morning. On a Pi with stock
passwordless sudo the same path would reboot the receiver.

Forcing the HDMI connector "always connected" in the kernel was tried
first and rejected: at 4K50 the link runs above 340 MHz, where HDMI 2.0
needs scrambling switched on in the monitor every time its input wakes
up, and the hot-plug event is what triggers that. With it forced off,
the LG came back to a signal it would not show.

THE FIX
-------
Every cycle, read the DRM connectors' status. When connectors exist and
none is "connected", note the time and skip the heartbeat verdict. When
a screen comes back, allow 45 s grace (the check's own 30 s, plus time
for the overlay to draw again) before judging the heartbeat.

Only when connector status is readable and says nothing is connected:
a board with no DRM status files at all keeps exactly the old
behaviour.

Run from the repo root:  python3 patch_heartbeat_no_screen.py
Then:                    bash -n lynx_start.sh
"""

import sys
from pathlib import Path

TARGET = Path("lynx_start.sh")


def apply(src: str, old: str, new: str, label: str, count: int = 1) -> str:
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    src = TARGET.read_text()
    if "LYNX_NO_SCREEN_AT" in src:
        sys.exit("ABORT: already patched")

    src = apply(
        src,
        '    OVERLAY_HEARTBEAT_FILE="/tmp/lynx_overlay_heartbeat"\n'
        '    if [ -f "$OVERLAY_HEARTBEAT_FILE" ]; then\n',
        '    OVERLAY_HEARTBEAT_FILE="/tmp/lynx_overlay_heartbeat"\n'
        '\n'
        '    # No screen, no verdict - see patch_heartbeat_no_screen.py. A\n'
        '    # monitor switched to another input or into standby drops\n'
        '    # hot-plug; the compositor then stops asking the overlay for\n'
        '    # frames, so a stale heartbeat means nothing. Paused while no\n'
        '    # connector is connected, and for 45 s after one comes back.\n'
        '    _screen_states=""\n'
        '    for _st_file in "${LYNX_DRM_SYSFS:-/sys/class/drm}"/card*-*/status; do\n'
        '        case "$_st_file" in *Writeback*) continue ;; esac\n'
        '        [ -r "$_st_file" ] && _screen_states="$_screen_states $(cat "$_st_file")"\n'
        '    done\n'
        '    if [ -n "$_screen_states" ] && ! echo "$_screen_states" | grep -qw connected; then\n'
        '        [ -z "$LYNX_NO_SCREEN_AT" ] && \\\n'
        '            echo -e "${AMBER}No screen connected - overlay heartbeat check paused.${NC}"\n'
        '        LYNX_NO_SCREEN_AT=$(date +%s)\n'
        '    elif [ -n "$LYNX_NO_SCREEN_AT" ] && \\\n'
        '         [ $(( $(date +%s) - LYNX_NO_SCREEN_AT )) -gt 45 ]; then\n'
        '        echo -e "${GREEN}Screen back - overlay heartbeat check resumed.${NC}"\n'
        '        LYNX_NO_SCREEN_AT=""\n'
        '    fi\n'
        '\n'
        '    if [ -f "$OVERLAY_HEARTBEAT_FILE" ] && [ -z "$LYNX_NO_SCREEN_AT" ]; then\n',
        "start: no screen, no verdict")

    TARGET.write_text(src)


if __name__ == "__main__":
    main()
