#!/usr/bin/env python3
"""
Lynx beta patch: stop the hard-freeze restart racing a deliberate source
switch.

The fault
---------
Every stream switch in the diagnostics log is preceded, seconds earlier,
by "Hard freeze detected - immediate mpv restart" followed by
"hard_freeze_render_not_confirmed". Confirmed on 2026-09-02 at 08:02,
08:05 and again at 22:57 the previous night. It happens on every switch,
not occasionally.

user_stream_start is only recorded at the END of a switch, after
rendering is confirmed and the cover comes down, so the log ordering is
the reverse of how it reads. The real sequence is:

  1. Switch begins. start_transition_cover() raises the cover,
     current_mode/current_stream_url are set to the NEW source.
  2. _kick_mpv() kills the old mpv and starts the new one.
  3. The kill makes playback stop advancing, so
     lynx_drift_correction.lua correctly writes hard_freeze_detected_at.
  4. mpv_drift_monitor() reads that flag. current_mode and
     current_stream_url already point at the new source, so it builds a
     valid hard_freeze_target, concludes mpv has frozen, and restarts
     mpv ON TOP OF the restart already in progress.
  5. Its restart cannot confirm rendering, because the legitimate switch
     is mid-flight underneath it.
  6. The real switch finally confirms and records user_stream_start.

A kill that is part of a deliberate switch is indistinguishable from a
freeze, because this path never asks whether a switch is happening.

The fix
-------
mpv_transitioning is already exactly that signal. start_transition_cover()
sets it before anything is touched, end_transition_cover() clears it once
the new source is genuinely rendering, and the overlay already depends on
it. This path simply never consulted it.

The freeze flag is marked handled rather than left pending, so it does
not fire the instant the cover comes down and re-create the same race one
step later. A freeze that is still genuinely present after the switch
raises a fresh hard_freeze_detected_at from the Lua script and is caught
on its own merits.

Guarding on mpv_transitioning covers RF tunes as well as streams -
_tune_impl() raises the same cover and kills mpv the same way.

Why not rely on tune_lock
-------------------------
The existing tune_lock.acquire(timeout=2) below looks like it should
already serialise against this, but _start_stream_impl() hands the lock
off to its own _kick_mpv thread and sets _tune_lock_handed_off, so there
is a real window where the lock is free while the switch is still
running. That window is what is being hit. The lock is left exactly as it
is - it still serialises the restart itself - and the transition check is
added in front of it rather than replacing it.

Run from the directory containing lynx_app.py.
"""

import sys

PATH = "lynx_app.py"

OLD = '''            if (hard_freeze_detected_at > 0 and
                    hard_freeze_detected_at != last_handled_hard_freeze_at and
                    hard_freeze_target):
                now2 = time.time()
'''

NEW = '''            if (hard_freeze_detected_at > 0 and
                    hard_freeze_detected_at != last_handled_hard_freeze_at and
                    hard_freeze_target and
                    mpv_transitioning):
                # A source switch is in progress. mpv was killed
                # deliberately, so playback stopping is expected, not a
                # freeze - and restarting it here lands on top of the
                # switch already underway. Confirmed as a real, repeating
                # fault: every stream switch in the diagnostics log was
                # preceded seconds earlier by a hard-freeze restart and a
                # "did not confirm rendering" immediately after it, with
                # user_stream_start (recorded only once the switch
                # genuinely completes) arriving last.
                #
                # Marked handled rather than left pending: leaving it
                # would simply fire the moment the cover comes down,
                # moving the same race one step later. A freeze that is
                # still genuinely present afterwards raises a fresh
                # hard_freeze_detected_at from the Lua script and is
                # caught then, on its own merits.
                print("[mpv_drift] hard freeze flag seen during a source "
                      "switch - expected while mpv is being replaced, "
                      "ignoring")
                last_handled_hard_freeze_at = hard_freeze_detected_at

            elif (hard_freeze_detected_at > 0 and
                    hard_freeze_detected_at != last_handled_hard_freeze_at and
                    hard_freeze_target):
                now2 = time.time()
'''

with open(PATH, "r") as f:
    content = f.read()

count = content.count(OLD)
if count != 1:
    print(f"ABORT: expected exactly 1 match, found {count}. No changes written.")
    sys.exit(1)

content = content.replace(OLD, NEW)

with open(PATH, "w") as f:
    f.write(content)

print("Patched: hard-freeze restart now skipped while a source switch is in progress.")
