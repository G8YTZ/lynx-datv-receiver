#!/usr/bin/env python3
"""Remote Rx fallback for a USB BATC PicoTuner with no driver for it.

A PicoTuner CAN be driven - by Ryde's build of longmynd, which
install.sh fetches with --picotuner into ~/longmynd-pico. When that is
present the wrapper runs longmynd_source.py against it and this module
is not used at all.

This is for the card where it is absent. The stock BATC longmynd talks
only to an FTDI FT2232H, and a PicoTuner is an RP2040 (2e8a:ba2c)
presenting a different interface, so longmynd exits within a second of
starting and keeps doing so for as long as the card is left alone.

This module exists so that THAT failure is honest instead of
misleading. Before it, detection fell back to "minitiouner", the panel
showed a Minitiouner that was not there, and the only clue was a
journal filling at a line every five seconds - on an SD card up a
ladder.

So it does everything a Remote Rx does EXCEPT drive a tuner:

  - heartbeats, reporting "TUN picotuner_nodriver", so the site stays
    visible and the panel can say what is actually wrong
  - accepts settings, so frequency and name can still be set
  - accepts the Red Button, so the card can be updated into a version
    that drives the tuner, with no visit and no ssh

The last of those is the whole point. A Remote Rx that failed in a way
that left it unreachable would need somebody with a ladder on the day
the fix was ready. This one takes an update like any other.

It reports unlocked, always, because it is: no tuner is running. The
status line still carries the configured frequency, because
build_status_line always sends two fields and the repeater reads the
first as a number.

Two notes for later. A PicoTuner board can host TWO NIMs, but Ryde's
longmynd drives only one transport stream (Colin G4EML, BATC forum);
OpenTuner is the only software said to do two. And Lynx's own 550-odd
PicoTuner references are for the NETWORK one - WinterHill style, base
IP port 9900, TS on base+41 and +42 - which shares nothing with this.
"""

import argparse
import os
import signal
import sys
import threading
import time

import remote_common

# How often to repeat the explanation in the journal. Once at start
# would be lost behind a week of heartbeats by the time anybody looked;
# every few seconds is what this module exists to stop. Hourly is
# enough to be found by someone reading a fresh journal.
REMINDER_SECS = 3600.0


class NoTuner:
    """A tuner that is never locked, because there is not one running.

    Presents the same snapshot() the real sources do, so StatusSender
    needs no special case for it.
    """

    def __init__(self, freq_khz):
        self.freq_khz = freq_khz

    def snapshot(self):
        return {"locked": False, "frequency_khz": self.freq_khz,
                "callsign": "", "provider": "",
                "video_codec": "", "audio_codec": ""}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    remote_common.add_common_args(
        ap, "symbol rate in kS/s - recorded and reported, but nothing is "
            "tuned: this card cannot drive its Picotuner")
    ap.add_argument("--video-port", type=int, default=10998,
                    help="accepted and ignored; no video is produced")
    ap.add_argument("--transport", choices=("udp", "srt"), default="srt",
                    help="accepted and ignored; no video is produced")
    ap.add_argument("--srt-port", type=int, default=None,
                    help="accepted and ignored; no video is produced")
    ap.add_argument("--srt-latency", type=int, default=300,
                    help="accepted and ignored; no video is produced")
    ap.add_argument("--binary", default=None,
                    help="accepted and ignored; there is no binary to run")
    args = ap.parse_args()

    # "picotuner_nodriver", not "picotuner": a PicoTuner that IS being
    # driven reports "picotuner" from longmynd_source.py and gets the
    # ordinary DVB-S2 panel. This kind means the tuner is fitted and
    # nothing is driving it, which is a different thing to show.
    code = remote_common.prepare(args, "kS/s", "picotuner_nodriver")
    if code is not None:
        return code

    tuner = NoTuner(args.freq)
    stop = threading.Event()

    # No quality block: there are no measurements to send. Returning ""
    # is how a source says it has nothing rich to report, so the
    # repeater shows the plain rows rather than empty labelled ones.
    status = remote_common.StatusSender(args, tuner, lambda snap: "")
    status.start()

    if args.key:
        threading.Thread(
            target=remote_common.SettingsReceiver(
                args, status.sock,
                lambda: os.kill(os.getpid(), signal.SIGTERM)).run,
            daemon=True).start()

    # If this start is a new version on probation, take it off once it
    # has survived - exactly as the working sources do. Without this a
    # card updated INTO this state would roll itself back out of it
    # three starts later, which is not a decision this module should be
    # making on somebody's behalf.
    import updater
    threading.Thread(target=updater.confirm_after,
                     args=(args.update_file,),
                     kwargs={"stop": stop}, daemon=True).start()

    banner = (
        "Remote Rx (PicoTuner) -> " + args.host + "\n"
        "  tuner  : NOT DRIVEN. A USB BATC PicoTuner (2e8a:ba2c) is\n"
        "           fitted, but the longmynd that can drive one is not\n"
        "           installed. Build it with:\n"
        "             ~/lynx-datv-receiver/slave/install.sh --picotuner\n"
        f"  status : UDP port {args.status_port}, every {args.interval}s\n"
        f"  name   : {args.name or '(not set - the repeater will show a generic label)'}\n"
        f"  version: {args.version_commit} on {args.version_branch}\n"
        "  this card stays visible and UPDATABLE, so the fix can arrive\n"
        "  by the Update button with no visit. Fit a Minitiouner or a\n"
        "  Knucker to receive anything in the meantime.")
    print(banner)
    sys.stdout.flush()

    def shutdown(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Deliberately not a busy loop and deliberately not an exit. Exiting
    # would have systemd restart us for ever (Restart=always, no start
    # limit), which is the five-second flap this module replaces.
    last = time.monotonic()
    while not stop.is_set():
        stop.wait(1.0)
        now = time.monotonic()
        if now - last >= REMINDER_SECS:
            last = now
            print("[tuner] still a PicoTuner with no driver installed; "
                  "heartbeat and updates are running normally")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
