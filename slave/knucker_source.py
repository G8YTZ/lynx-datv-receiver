#!/usr/bin/env python3
"""Remote Rx - BATC Knucker (CombiTuner) tuner source.

Runs a Knucker as a Remote Rx: drives it with the BATC's
CombiTunerExpress over USB, sends the transport stream to the repeater,
and translates what the tuner says into the format Lynx already parses.

The Knucker receives narrowband DVB-T and DVB-T2, which is what makes it
worth having beside the Minitiouner: The same Remote Rx design, the
other modulation family, and no hardware modification to the tuner.

TWO THINGS TO KNOW BEFORE THIS WILL RUN AT ALL
----------------------------------------------
1. CombiTunerExpress is a 32-bit ARM (armhf) binary, and the Remote Rx
   card is built as 64-bit Raspberry Pi OS Lite, which has no armhf
   loader. Without the multiarch libraries it fails with "cannot
   execute: required file not found", which does not hint at the cause
   at all. install.sh adds them.

2. It is a closed binary with no retune interface: frequency and
   bandwidth are command-line arguments only. So a change of frequency
   means restarting it, and restarting it means re-uploading the
   demodulator's firmware patch, which takes about 8.5 seconds before
   acquisition even begins. That is why this module keeps one process
   alive rather than restarting it per tune: A repeater input sits on
   one frequency, so the cost is paid once at power-up and never again.

   Measured on a Zero 2 W against a Portsdown at 436 MHz, 333 kHz:

       firmware upload and init   8.5 s   every process start
       first acquisition, T2      ~7 s    every process start
       first acquisition, DVB-T   ~19 s   every process start
       RE-LOCK when a signal       ~1 s   each time a station returns
       returns

   The re-lock figure is the one that matters in service, and it is
   excellent. DVB-T's cold acquisition is nearly three times T2's,
   which is the opposite of what you would expect and appears to be
   fixed timeouts inside the DVB-T path rather than anything physical.

NO ALIGNMENT NEEDED HERE
------------------------
Longmynd needs its output realigned, because it emits the transport
stream in 510-byte chunks that straddle TS packets. The Knucker does
not: measured on real hardware, 300 datagrams out of 300 were 1316
bytes and every one began on a TS packet boundary. So its stream goes
straight to srt-live-transmit and the aligner thread is not started.

WHAT IT CAN AND CANNOT REPORT
-----------------------------
It reports SSI as a percentage, SQI, SNR in dB and PER, plus the
modulation it found. From those, honestly:

    MER     <- SNR, which is the same measurement by another name
    Mode    <- constellation and FEC rate, e.g. "QPSK 5/6"

And deliberately not:

    Level   SSI is a percentage with no published mapping to dBm.
            Inventing one would put a number on the panel that looks
            like the Minitiouner's calibrated reading and is not.
    Margin  It would need a required-C/N figure per constellation and
            code rate, and a guessed margin is worse than none - the
            same rule the Minitiouner's DVB-S path already follows.
    Callsign and codecs
            They come from the SDT and the PMT, which Longmynd parses
            and this binary does not expose. Reading them would mean
            parsing the transport stream here. Worth doing one day;
            until then the panel says LOCKED without a callsign, which
            is honest.

STDOUT MUST BE DRAINED
----------------------
Same trap as Longmynd's status FIFO, by a different route: status comes
on stdout, and if nothing reads it the pipe fills, the tuner blocks
trying to write, and the stream dies. So one process owns both, and the
reader never exits while the tuner is running.
"""

import argparse
import os
import pty
import re
import signal
import subprocess
import sys
import threading
import time

import remote_common
import srt_link

# How long without a line from the tuner before it is assumed gone. It
# speaks every second or two whether locked or not, so this is generous.
STATUS_STALE_SECS = 8.0

RESTART_DELAY_SECS = 5.0

# How often to say it is still searching, when it has not locked. Long
# enough not to fill the journal on a site that is quiet for hours,
# short enough that somebody watching sees it is alive.
SEARCHING_REPORT_SECS = 30.0

# What the tuner says. Anchored, because "locked" also appears inside
# "[DVB_Tx_tuner_Lock] Tuner locked!", which is the RF tuner's PLL and
# not a demodulator lock: treating that as locked would report a lock
# about nine seconds before there is a picture.
RE_LOCKED = re.compile(r"^locked\s*$")
RE_UNLOCKED = re.compile(r"^Unlocked\s*$")
RE_SSI = re.compile(r"^SSI is (\d+)")
RE_SQI = re.compile(r"^SQI is (\d+)")
RE_SNR = re.compile(r"^SNR is ([-\d.]+)")
RE_PER = re.compile(r"^PER is ([\d.]+)")
RE_MOD = re.compile(r"^MOD\s*:\s*(\S+)")
RE_CONST = re.compile(r"^Const\s*:\s*(\S+)")
RE_FEC = re.compile(r"^FEC\s*:\s*(\S+)")
RE_GUARD = re.compile(r"^Guard\s*:\s*(\S+)")
RE_FFT = re.compile(r"^FFT\s*:\s*(\S+)")


class KnuckerState:
    """What CombiTunerExpress has most recently said.

    Guarded by a lock because the stdout reader writes it and the
    status sender reads it, on different threads and at different
    rates.

    snapshot() returns the same keys the heartbeat expects, so this
    stands in for the Minitiouner's TunerState without either knowing
    about the other.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.locked = False
        self.snr_db = 0.0
        self.ssi = None
        self.sqi = None
        self.per = None
        self.mod = ""
        self.const = ""
        self.fec = ""
        self.guard = ""
        self.fft = ""
        self.last_line_at = 0.0

    def update(self, line: str) -> bool:
        """True if the line was one we understand.

        The caller echoes the ones that are not, which is how
        "Unable to claim interface" reaches the journal instead of
        being silently dropped: that message is the only clue when
        something else already has the tuner, and the binary answers
        it with a segfault rather than a sensible exit.
        """
        with self._lock:
            self.last_line_at = time.time()
            if RE_LOCKED.match(line):
                self.locked = True
                return True
            if RE_UNLOCKED.match(line):
                self.locked = False
                # The figures belong to a signal that is no longer
                # there. Keep the modulation: it is what we were
                # receiving and is useful while waiting for it back.
                self.snr_db = 0.0
                self.sqi = None
                self.per = None
                return True
            for rx, attr, cast in (
                    (RE_SNR, "snr_db", float), (RE_SSI, "ssi", int),
                    (RE_SQI, "sqi", int), (RE_PER, "per", float),
                    (RE_MOD, "mod", str), (RE_CONST, "const", str),
                    (RE_FEC, "fec", str), (RE_GUARD, "guard", str),
                    (RE_FFT, "fft", str)):
                m = rx.match(line)
                if m:
                    try:
                        setattr(self, attr, cast(m.group(1)))
                    except ValueError:
                        pass
                    return True
            return False

    def snapshot(self) -> dict:
        with self._lock:
            fresh = (self.last_line_at > 0
                     and (time.time() - self.last_line_at) < STATUS_STALE_SECS)
            return {
                "locked": fresh and self.locked,
                "fresh": fresh,
                # The Knucker does not report a measured frequency, so
                # the heartbeat falls back to what we asked for. Unlike
                # Longmynd there is no carrier offset to show.
                "frequency_khz": 0,
                # No SDT parsing, so never a callsign. The heartbeat
                # reports LOCKED instead, which is the same thing it
                # does for a Minitiouner whose transmitter left the SDT
                # empty.
                "callsign": "",
                "snr_db": self.snr_db,
                "ssi": self.ssi,
                "sqi": self.sqi,
                "per": self.per,
                "mod": self.mod,
                "const": self.const,
                "fec": self.fec,
                "guard": self.guard,
                "fft": self.fft,
            }

    def clear_signal(self):
        """Forget what we were hearing, keeping what we are tuned to.

        Called when the tuner process restarts: showing a stale SNR at
        the repeater would be worse than showing none.
        """
        with self._lock:
            self.locked = False
            self.snr_db = 0.0
            self.sqi = None
            self.per = None


def build_quality_lines(snap: dict) -> str:
    """The tagged $n,m block for the quality port, or "" if nothing yet.

    Tagged rather than a column table, for the reason Lynx's own
    comments record: a column shift once fed one receiver's MER into
    another receiver's display. A tagged value is whatever its tag says
    it is.

    Fields are omitted rather than zeroed when they are not known, so
    the repeater can tell "this Remote Rx has no rich status" from
    "this Remote Rx is reporting nothing", which are different faults.
    """
    if not snap["locked"]:
        return ""
    lines = []
    if snap["snr_db"]:
        # $12 is the MER tag Lynx already reads for a Picotuner. SNR
        # and MER are the same measurement under two names here.
        lines.append(f"$12,{snap['snr_db']:.1f}")
    if snap["const"] and snap["fec"]:
        # $18 is the mode tag, e.g. "QPSK 5/6". No $30 margin to go
        # with it: that needs a required-C/N figure per mode, and a
        # guessed margin is worse than none.
        lines.append(f"$18,{snap['const']} {snap['fec']}")
    return "\n".join(lines)


def start_tuner(cmd):
    """Launch the tuner on a pty, and return (proc, reader).

    WHY A PTY AND NOT A PIPE
    ------------------------
    CombiTunerExpress uses ordinary C stdio, which line-buffers to a
    terminal and block-buffers to a pipe. Down a pipe its status sits
    in a 4 KB buffer that takes minutes to fill at a few short lines
    every two seconds, so the Remote Rx reported "searching" while the
    tuner was locked and sending pictures perfectly well. Measured
    against a stand-in with the same buffering: zero lines in the
    first 1.5 seconds through a pipe, everything immediately through
    a pty.

    stdbuf is the usual answer and cannot help here: its helper
    library is 64-bit and the tuner binary is 32-bit, so the preload
    is refused.
    """
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(cmd, preexec_fn=os.setsid,
                                stdout=slave, stderr=slave, close_fds=True)
    finally:
        # The parent has no use for its end, and holding it open would
        # stop the reader ever seeing end-of-file.
        os.close(slave)
    return proc, os.fdopen(master, "r", errors="replace")


def read_tuner(reader, tuner: KnuckerState, stop: threading.Event):
    """Drain the tuner's output and parse it.

    Draining matters more than parsing. If this stops, the buffer
    fills, the tuner blocks writing to it, and the transport stream
    dies: the same trap as Longmynd's status FIFO by another route.

    Lines we do not recognise are echoed rather than dropped. They are
    the start-up sequence, the PLP report, and the errors.
    """
    try:
        for raw in reader:
            if stop.is_set():
                return
            line = raw.strip()
            if line and not tuner.update(line):
                print(f"[tuner] {line}", flush=True)
    except OSError:
        # Reading a pty whose child has gone raises EIO on Linux. That
        # is how this loop ends normally; the supervisor restarts it.
        return
    finally:
        try:
            reader.close()
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    remote_common.add_common_args(
        ap, "channel bandwidth in kHz, e.g. 333 (not a symbol rate: this "
            "is DVB-T/T2)")
    ap.add_argument("--video-port", type=int, default=10998,
                    help="plain-UDP video port at the repeater")
    ap.add_argument("--binary",
                    default=os.path.expanduser("~/knucker/CombiTunerExpress"),
                    help="the BATC CombiTunerExpress binary")
    ap.add_argument("--transport", choices=("udp", "srt"), default="srt",
                    help="how the video reaches the repeater: plain UDP on a "
                         "LAN, SRT anywhere else (status always stays UDP)")
    ap.add_argument("--srt-port", type=int, default=None,
                    help="the repeater's SRT listener for this Remote Rx; "
                         f"defaults to the status port minus "
                         f"{srt_link.SRT_PORT_OFFSET}, which is what Lynx "
                         "derives")
    ap.add_argument("--srt-latency", type=int, default=300,
                    help="SRT latency in ms; about four round trips. 300 suits "
                         "most broadband, a LAN needs far less")
    args = ap.parse_args()

    code = remote_common.prepare(args, "kHz bandwidth")
    if code is not None:
        return code
    if args.srt_port is None:
        args.srt_port = args.status_port - srt_link.SRT_PORT_OFFSET

    if not os.path.exists(args.binary):
        print(f"No CombiTunerExpress at {args.binary}.\n"
              f"Fetch it with:\n"
              f"  mkdir -p {os.path.dirname(args.binary)} && curl -sSL -o "
              f"{args.binary} https://raw.githubusercontent.com/"
              f"BritishAmateurTelevisionClub/ryde-build/master/configs/dvbt/"
              f"CombiTunerExpress && chmod +x {args.binary}\n"
              f"It is a 32-bit binary, so the armhf libraries must be "
              f"installed too (install.sh does this).", file=sys.stderr)
        return 2

    tuner = KnuckerState()
    stop = threading.Event()

    status = remote_common.StatusSender(args, tuner, build_quality_lines)
    status.start()

    # Where the tuner sends its transport stream. Already 1316-byte
    # aligned and on TS boundaries, so for SRT it goes straight to
    # srt-live-transmit's own input port and the aligner thread that
    # Longmynd needs is not started at all.
    if args.transport == "srt":
        ts_host, ts_port = "127.0.0.1", srt_link.ALIGNED_LOCAL_PORT
        srt_link.SrtSender(args.host, args.srt_port, args.srt_latency,
                           args.name, stop).start()
    else:
        ts_host, ts_port = args.host, args.video_port

    if args.key:
        threading.Thread(
            target=remote_common.SettingsReceiver(
                args, status.sock,
                lambda: os.kill(os.getpid(), signal.SIGTERM)).run,
            daemon=True).start()

    # If this start is a new version on probation, take it off once it
    # has survived. Harmless when there is nothing on probation.
    import updater
    threading.Thread(target=updater.confirm_after,
                     args=(args.update_file,),
                     kwargs={"stop": stop}, daemon=True).start()

    cmd = [args.binary,
           "-m", "dvbt",
           "-f", str(args.freq),
           "-b", str(args.sr),
           "-i", ts_host,
           "-p", str(ts_port)]

    print(f"Remote Rx (Knucker) -> {args.host}")
    print(f"  tuner  : {args.freq} kHz, {args.sr} kHz bandwidth, DVB-T/T2")
    if args.transport == "srt":
        print(f"  video  : SRT to port {args.srt_port}, "
              f"latency {args.srt_latency} ms")
    else:
        print(f"  video  : UDP port {args.video_port}")
    print(f"  status : UDP port {args.status_port}, every {args.interval}s")
    print(f"  quality: UDP port {args.quality_port}")
    print(f"  name   : {args.name or '(not set - the repeater will show a generic label)'}")
    print(f"  version: {args.version_commit} on {args.version_branch}")
    print("Ctrl-C to stop.\n")

    proc = None
    stopping = False

    def shutdown(signum, frame):
        nonlocal stopping
        stopping = True
        stop.set()
        status.running = False
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    last_reported = None
    while not stopping:
        started = time.time()
        print("[tuner] starting CombiTunerExpress")
        tuner.clear_signal()
        proc, reader = start_tuner(cmd)
        threading.Thread(target=read_tuner, args=(reader, tuner, stop),
                         daemon=True).start()

        last_searching = 0.0
        while proc.poll() is None and not stopping:
            snap = tuner.snapshot()
            now = snap["locked"]
            if now != last_reported:
                if snap["locked"]:
                    print(f"[tuner] locked - {snap['mod'] or 'DVB-T/T2'} "
                          f"{snap['const']} {snap['fec']} "
                          f"{args.freq / 1000.0:.3f} MHz "
                          f"SNR {snap['snr_db']:.1f} dB")
                elif last_reported is not None:
                    print("[tuner] lock lost")
                last_reported = now
            elif not snap["locked"]:
                # A Remote Rx that has never locked would otherwise log
                # nothing at all after start-up, which over SSH is
                # indistinguishable from a crashed service.
                nowt = time.time()
                if nowt - last_searching >= SEARCHING_REPORT_SECS:
                    last_searching = nowt
                    print(f"[tuner] searching - {args.freq / 1000.0:.3f} MHz, "
                          f"{args.sr} kHz, no lock for {nowt - started:.0f}s")
            time.sleep(0.5)

        if stopping:
            break

        tuner.clear_signal()
        last_reported = None
        ran = time.time() - started
        print(f"[tuner] CombiTunerExpress exited (rc={proc.returncode}) after "
              f"{ran:.0f}s - restarting in {RESTART_DELAY_SECS:.0f}s")
        time.sleep(RESTART_DELAY_SECS)

    print(f"\nStopped. {status.sent} heartbeats sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
