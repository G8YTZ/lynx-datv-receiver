#!/usr/bin/env python3
"""
patch_screen_return_stall.py

When the screen comes back, mpv must be playing, not just showing a
picture. Builds on patch_mpv_screen_return.py (c61b9ab).

THE BUG
-------
22 Oct 2026, ROCK 5T, DVB-T: the LG was off from 14:43 to 16:49. When it
came back there was no sound and no picture, though the overlay was
fine. The screen-return check ran at 16:49:36, asked mpv whether it had
a picture, got "yes" (mpv still reports the size of its last frame) and
left it alone. No watchdog fired in the two hours, and changing service
cured it.

So mpv was alive but stuck. The likely cause is the HDMI sound device:
after a long time off, the LG drops into deep standby and stops
offering HDMI audio, so PipeWire takes the HDMI sink away. mpv times
playback from its audio output, so losing it stops the picture as well,
and when the sink returns mpv stays attached to the one that went.
A 20-minute input switch earlier the same afternoon did not do this.

THE FIX
-------
When the screen comes back, after the existing picture check:
  1. Is mpv actually moving? playback-time is read twice, 3 s apart.
  2. Does it still have a sound output? (current-ao, when the source has
     a sound track.)
If either answer is no, mpv's state is logged (for the next diagnosis),
mpv is asked to reopen its sound output (ao-reload, which costs no
picture), and the two checks are run again. Still stuck: mpv is
restarted on the same service, exactly as the no-picture case does.

A paused mpv is left alone.

Run from the repo root:  python3 patch_screen_return_stall.py
Then:                    python3 verify.py lynx_app.py
"""

import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


def apply(src: str, old: str, new: str, label: str, count: int = 1) -> str:
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


HELPERS = '''
# _mpv_prop() comes from patch_video_badges.py.


def _mpv_playback_problem():
    """None if mpv is playing normally (or not running, or paused).
    Otherwise a short reason: playback not moving, or no sound output
    although the source has sound. See patch_screen_return_stall.py."""
    tracks = _mpv_prop("track-list")
    if not isinstance(tracks, list):
        return None                      # no mpv answering
    if _mpv_prop("pause"):
        return None                      # paused on purpose
    has_audio = any(t.get("type") == "audio" and t.get("selected") for t in tracks)
    reasons = []
    if has_audio and not _mpv_prop("current-ao"):
        reasons.append("no sound output")
    t1 = _mpv_prop("playback-time")
    time.sleep(3)
    t2 = _mpv_prop("playback-time")
    if isinstance(t1, (int, float)) and isinstance(t2, (int, float)):
        if t2 - t1 < 0.5:
            reasons.append(f"playback not moving ({t1:.1f}s -> {t2:.1f}s)")
    elif t1 is None and t2 is None:
        reasons.append("no playback position")
    return ", ".join(reasons) or None


def _mpv_state_line():
    """mpv's state in one log line, for working out what went wrong."""
    names = ("playback-time", "core-idle", "paused-for-cache", "pause",
             "current-ao", "audio-device", "eof-reached", "demuxer-cache-idle")
    return " ".join(f"{n}={_mpv_prop(n)!r}" for n in names)


def screen_return_monitor():'''


RESTART_OLD = '''                src = network_output_source
                is_rf = network_output_source_is_rf
                if (not network_output_enabled and src
                        and _mpv_has_picture_problem()
                        and tune_lock.acquire(timeout=2)):
                    try:
                        print("[screen] mpv has sound but no picture - restarting it on "
                              f"{src}", flush=True)
                        record_diagnostic_event(
                            "screen_back_restart",
                            "screen came back and mpv had no picture - restarted it")
                        start_transition_cover()
                        time.sleep(0.3)
                        restart_mpv(src, is_rf=is_rf)
                        if is_rf:
                            mpv_last_started_at = time.time()
                        wait_for_mpv_rendering()
                        end_transition_cover()
                    finally:
                        tune_lock.release()
'''

RESTART_NEW = '''                src = network_output_source
                is_rf = network_output_source_is_rf
                why, event = None, None
                if not network_output_enabled and src:
                    if _mpv_has_picture_problem():
                        why, event = "mpv has sound but no picture", "screen_back_restart"
                    else:
                        problem = _mpv_playback_problem()
                        if problem:
                            # Log first: this is the evidence for next time.
                            print(f"[screen] mpv stuck ({problem}): {_mpv_state_line()}",
                                  flush=True)
                            mpv_query({"command": ["ao-reload"]})
                            time.sleep(2)
                            problem = _mpv_playback_problem()
                            if problem:
                                why, event = (f"mpv still stuck after reopening sound "
                                              f"({problem})"), "screen_back_stall_restart"
                            else:
                                print("[screen] reopening mpv's sound output fixed it",
                                      flush=True)
                                record_diagnostic_event(
                                    "screen_back_ao_reload",
                                    "screen came back with mpv stuck - reopening its "
                                    "sound output fixed it")
                if why and tune_lock.acquire(timeout=2):
                    try:
                        print(f"[screen] {why} - restarting it on {src}", flush=True)
                        record_diagnostic_event(event,
                                                f"screen came back: {why} - restarted it")
                        start_transition_cover()
                        time.sleep(0.3)
                        restart_mpv(src, is_rf=is_rf)
                        if is_rf:
                            mpv_last_started_at = time.time()
                        wait_for_mpv_rendering()
                        end_transition_cover()
                    finally:
                        tune_lock.release()
'''


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    src = TARGET.read_text()
    if "def _mpv_playback_problem" in src:
        sys.exit("ABORT: already patched")
    if "def _mpv_prop(" not in src:
        sys.exit("ABORT: patch_video_badges.py has not been applied")
    if "def screen_return_monitor" not in src:
        sys.exit("ABORT: patch_mpv_screen_return.py has not been applied")
    before = len(src.splitlines())

    src = apply(src, "\ndef screen_return_monitor():", HELPERS, "helpers")
    src = apply(src, RESTART_OLD, RESTART_NEW, "screen return: stall check")
    src = apply(
        src,
        '    "screen_back_restart": "Screen came back with no picture - mpv restarted",\n',
        '    "screen_back_restart": "Screen came back with no picture - mpv restarted",\n'
        '    "screen_back_stall_restart": "Screen came back with mpv stuck - mpv restarted",\n'
        '    "screen_back_ao_reload": "Screen came back with mpv stuck - sound output reopened",\n',
        "diagnostics labels")

    TARGET.write_text(src)
    print(f"lynx_app.py: {before} -> {len(src.splitlines())} lines")


if __name__ == "__main__":
    main()
