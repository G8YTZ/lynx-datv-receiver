#!/usr/bin/env python3
"""
patch_dead_audio_track.py

Deselects an audio track that never carries anything.

THE FAULT
---------
A transmitter can declare an audio PID in its PMT and send nothing on
it — a camera or an encoder with the sound switched off does exactly
this. mpv selects the track and will not settle into normal playback
while it is waiting on a stream that never delivers. Measured on real
hardware: roughly twenty seconds of stale and smeared frames before
video ran properly, against an immediate picture with the sound turned
on at the transmitter.

Confirmed three separate times by sending mpv "aid no" over IPC while
it was misbehaving — once on the local Picotuner, twice on a Slave. The
picture came good within a second every time.

Note this is NOT a quiet passage in a programme. A track carrying
frames of digital silence delivers packets, mpv has a clock, and
everything behaves. The problem is specifically a declared track that
carries no data at all.

WHAT WAS TRIED FIRST
--------------------
--video-sync=desync stops video presentation waiting on the audio
clock, and did not help: mpv still will not get going until every
selected track has been identified. --demuxer-lavf-probe-info=nostreams
did not help either — it still selected the AAC track. Only removing
the track from consideration works, which is what this does.

HOW
---
A monitor evaluates once per mpv launch, three seconds after it starts:
if an audio track is selected and audio-bitrate is unavailable across
three consecutive checks, the track is deselected.

Deliberately once per launch rather than continuously. Having disabled
the track, mpv reports nothing more about it, so there is no honest way
to notice audio appearing later without re-enabling it to look — which
would flap. mpv is relaunched on every source switch and on lock
regained, so a transmission that has audio when it starts gets audio.
A transmitter that switches its sound on mid-transmission will be
picked up at the next restart rather than immediately. That is a real
limitation, and it is a better trade than twenty seconds of stale video
on every silent transmission.

Run from the repo root:  python3 patch_dead_audio_track.py
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

    if "mpv_dead_audio_monitor" in src:
        sys.exit("ABORT: already patched")
    if "def mpv_query" not in src:
        sys.exit("ABORT: mpv_query helper not found — unexpected file layout")

    # ── the monitor ──────────────────────────────────────────────
    src = apply(
        src,
        "def _kill_process_reliably(proc, pkill_pattern=None):",
        '''def mpv_dead_audio_monitor():
    """Deselect an audio track that is declared but never carries data.

    mpv will not settle into normal playback while waiting on a
    selected track that never identifies, and an encoder with its sound
    switched off produces exactly that: an AAC PID in the PMT with
    nothing on it. Measured at roughly twenty seconds of stale, smeared
    video before it recovered, against an immediate picture when the
    transmitter had audio.

    Evaluated once per mpv launch. Once the track is deselected mpv
    stops reporting on it, so noticing audio appearing later would mean
    re-enabling it to look — which flaps. mpv is relaunched on every
    source switch and whenever lock is regained, so a transmission that
    has audio from the start gets audio.
    """
    POLL_SECS = 2.0
    START_GRACE_SECS = 3.0   # let mpv open the stream before judging it
    CONFIRM_CHECKS = 3       # ...and see it fail to identify audio this often

    evaluated_for = 0.0      # which mpv launch this has already decided about
    last_seen_start = 0.0    # ...and which one the current tally belongs to
    silent_checks = 0

    while True:
        try:
            time.sleep(POLL_SECS)

            started = mpv_last_started_at
            if started != last_seen_start:
                # A new mpv. Anything learned about the last one is void.
                # Compared against its own variable rather than against
                # evaluated_for: that one stays behind until a decision is
                # made, so testing it here reset the tally on every pass
                # and the count never reached the threshold.
                last_seen_start = started
                silent_checks = 0

            if started <= 0 or started == evaluated_for:
                continue
            if time.time() - started < START_GRACE_SECS:
                continue

            tracks = mpv_query({"command": ["get_property", "track-list"]})
            if not tracks or tracks.get("error") != "success":
                continue
            audio_selected = any(t.get("type") == "audio" and t.get("selected")
                                 for t in (tracks.get("data") or []))
            if not audio_selected:
                # Nothing to do, and nothing to keep watching for on this
                # launch — a track that was never selected will not
                # select itself.
                evaluated_for = started
                continue

            # "Unavailable" is mpv's way of saying it has no idea, which
            # is exactly the state a track with no packets produces. A
            # genuinely silent programme still reports a bitrate.
            br = mpv_query({"command": ["get_property", "audio-bitrate"]})
            has_audio_data = bool(br and br.get("error") == "success"
                                  and br.get("data"))

            if has_audio_data:
                evaluated_for = started
                silent_checks = 0
                continue

            silent_checks += 1
            if silent_checks >= CONFIRM_CHECKS:
                print("[mpv_audio] audio track declared but carrying no data "
                      "after %.0fs - deselecting it so video can run"
                      % (time.time() - started))
                mpv_cmd({"command": ["set_property", "aid", "no"]})
                evaluated_for = started
                silent_checks = 0

        except Exception as e:
            print(f"[mpv_audio] {type(e).__name__}: {e}")
            time.sleep(2)


def _kill_process_reliably(proc, pkill_pattern=None):''',
        "mpv_dead_audio_monitor()",
    )

    # ── start it ─────────────────────────────────────────────────
    src = apply(
        src,
        "    decoder_health = threading.Thread(target=mpv_decoder_health_monitor, daemon=True)",
        "    threading.Thread(target=mpv_dead_audio_monitor, daemon=True).start()\n"
        "    decoder_health = threading.Thread(target=mpv_decoder_health_monitor, daemon=True)",
        "start the monitor",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_audio"))
    TARGET.write_text(src)

    print(f"\n  lynx_app.py: {before} -> {len(src.splitlines())}")
    print("\n  Now: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
