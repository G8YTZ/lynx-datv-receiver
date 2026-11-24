#!/usr/bin/env python3
"""
Slave Rx - Longmynd tuner source.

Runs a standard, unmodified MiniTiouner as a Remote Rx: drives it with
Longmynd over USB, sends the transport stream to the repeater, and
translates Longmynd's status into the format Lynx already parses.

A Picotuner (the non-Ethernet one) is not supported yet. It presents
differently over USB and has not been tested against this, so whether
Longmynd can drive it here is an open question rather than an assumed
yes. This docstring previously claimed a Pico-converted board, which
was never what the Remote Rx was built and proved against.

WHY THE STATUS READER IS NOT OPTIONAL
-------------------------------------
Longmynd writes status to a named pipe. If nothing drains that pipe it
fills after about 64KB, Longmynd blocks trying to write to it, stops
servicing the USB endpoint, and the transport stream dies. Observed
directly: stop the reader and the video stops within seconds, with
Longmynd looping through "No-data timeout, re-init config" and nothing
in the log to connect the two.

So status and video are coupled whether we want them to be or not, and
one process has to own both. A separate "status daemon" that could fail
independently would take the picture with it.

TRANSLATION, NOT INTERPRETATION
-------------------------------
Longmynd emits "$n,m" lines with fixed numeric IDs. The mapping to what
Lynx wants turned out to be direct, which was not a given - the design
notes had assumed the callsign might need to be teased out of a
combined string:

    $1  state       0 init, 1 search, 2 headers, 3 DVB-S, 4 DVB-S2
    $6  frequency   kHz
    $9  symbol rate symbols/s
    $12 MER         dB x 10
    $13 provider    the callsign, when the transmitter populates the SDT
    $14 service     the programme name
    $18 MODCOD

Confirmed on real hardware against a Muntjac, which fills in $13 and
$14 properly. A transmitter that does not will simply leave them empty,
and the Slave reports unlocked-with-no-callsign rather than inventing
one.

VIDEO GATES ITSELF
------------------
Longmynd emits no transport stream while unlocked, so the "stream only
when there is something to send" rule needs no code here at all. The
heartbeat still runs regardless - that is the one thing that must never
stop, or "band is quiet" and "site is off the air" look identical at
the repeater.
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time

import agc_table
import remote_common
import srt_link
import updater

# What a Remote Rx reports and how it talks to the repeater lives in
# remote_common; what is left here is Longmynd and the Minitiouner.

# DVB-S2 modcod -> (name, MER threshold in dB). The name is what Lynx
# displays under "Mode"; the threshold is what margin is measured
# against. Thresholds are the DVB-S2 required Es/No figures.
#
# Validated against a live ON4VVV reading: MER 10.2 on QPSK 1/2, whose
# threshold here is 1.0, giving the 9.2 dB margin that was observed.
MODCODS = {
    1:  ("QPSK 1/4",    -2.35), 2:  ("QPSK 1/3",    -1.24),
    3:  ("QPSK 2/5",    -0.30), 4:  ("QPSK 1/2",     1.00),
    5:  ("QPSK 3/5",     2.23), 6:  ("QPSK 2/3",     3.10),
    7:  ("QPSK 3/4",     4.03), 8:  ("QPSK 4/5",     4.68),
    9:  ("QPSK 5/6",     5.18), 10: ("QPSK 8/9",     6.20),
    11: ("QPSK 9/10",    6.42), 12: ("8PSK 3/5",     5.50),
    13: ("8PSK 2/3",     6.62), 14: ("8PSK 3/4",     7.91),
    15: ("8PSK 5/6",     9.35), 16: ("8PSK 8/9",    10.69),
    17: ("8PSK 9/10",   10.98), 18: ("16APSK 2/3",   8.97),
    19: ("16APSK 3/4",  10.21), 20: ("16APSK 4/5",  11.03),
    21: ("16APSK 5/6",  11.61), 22: ("16APSK 8/9",  12.89),
    23: ("16APSK 9/10", 13.13), 24: ("32APSK 3/4",  12.73),
    25: ("32APSK 4/5",  13.64), 26: ("32APSK 5/6",  14.28),
    27: ("32APSK 8/9",  15.69), 28: ("32APSK 9/10", 16.05),
}

# MPEG-2 stream types, as $17 reports them. They live in
# remote_common because the Knucker reads the same numbers out of the
# PMT itself, and two copies of one table is two chances to add H.266
# to only one of them.
VIDEO_STREAM_TYPES = remote_common.VIDEO_STREAM_TYPES
AUDIO_STREAM_TYPES = remote_common.AUDIO_STREAM_TYPES
STREAM_TYPES = remote_common.STREAM_TYPES


# Longmynd state 4. Only here does $18 carry a modcod at all — in
# DVB-S (state 3) the code rate arrives on $3 with its own thresholds.
DVBS2_STATE = 4

# Longmynd's own default, and the file it insists on. Created if absent
# - it refuses to start without it, which is how the first attempt at
# this failed.
FIFO_NAME = "longmynd_main_status"

# How long without a status line before the tuner is assumed gone. The
# FIFO carries several lines a second when healthy, so this is generous.
STATUS_STALE_SECS = 5.0

RESTART_DELAY_SECS = 5.0

# How often to say it is still searching, when it has not locked.
# Long enough not to fill the journal on a site that is quiet for
# hours, short enough that somebody watching sees it is alive.
SEARCHING_REPORT_SECS = 30.0

# Longmynd state values that count as a lock. 2 is "headers" - carrier
# found, still acquiring - which is deliberately NOT treated as locked:
# it comes and goes on a marginal signal and would make the repeater
# flap. Only a full demodulator lock counts.
LOCKED_STATES = {3, 4}

STATUS_RE = re.compile(r"^\$(\d+),(.*)$")

class TunerState:
    """What Longmynd has most recently said.

    Guarded by a lock because the FIFO reader writes it and the status
    sender reads it, on different threads and at different rates.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.state = 0
        self.frequency_khz = 0
        self.symbol_rate = 0
        self.mer_db = 0.0
        self.callsign = ""
        self.programme = ""
        self.modcod = 0
        # Raw tuner gains. Sent on as-is as well as being turned
        # into dBm: the raw pair is what a fault report wants when
        # the derived figure looks wrong.
        self.agc1 = None
        self.agc2 = None
        # $16 arrives before the $17 it belongs to, so the PID is
        # held until its type turns up. Only used to pair them: the
        # PID itself is not reported.
        self._pending_pid = None
        self.video_codec = ""
        self.audio_codec = ""
        self.last_line_at = 0.0

    def update(self, field: int, value: str):
        with self._lock:
            self.last_line_at = time.time()
            try:
                if field == 1:
                    self.state = int(value)
                elif field == 6:
                    self.frequency_khz = int(value)
                elif field == 9:
                    self.symbol_rate = int(value)
                elif field == 12:
                    self.mer_db = int(value) / 10.0
                elif field == 13:
                    self.callsign = value.strip()
                elif field == 14:
                    self.programme = value.strip()
                elif field == 18:
                    self.modcod = int(value)
                elif field == 16:
                    self._pending_pid = int(value)
                elif field == 17:
                    es_type = int(value)
                    # The type decides which row it belongs in. An
                    # unknown one is still worth reporting: the number
                    # is what anybody would look up anyway, and a blank
                    # would suggest there was no stream at all.
                    if es_type in VIDEO_STREAM_TYPES:
                        self.video_codec = VIDEO_STREAM_TYPES[es_type]
                    elif es_type in AUDIO_STREAM_TYPES:
                        self.audio_codec = AUDIO_STREAM_TYPES[es_type]
                    elif self._pending_pid is not None:
                        self.video_codec = self.video_codec or f"type {es_type}"
                    self._pending_pid = None
                elif field == 26:
                    self.agc1 = int(value)
                elif field == 27:
                    self.agc2 = int(value)
            except ValueError:
                # A malformed line is not worth dying over. Longmynd
                # occasionally emits a partial line when it restarts.
                pass

    def snapshot(self) -> dict:
        with self._lock:
            fresh = (self.last_line_at > 0
                     and (time.time() - self.last_line_at) < STATUS_STALE_SECS)
            return {
                "locked": fresh and self.state in LOCKED_STATES,
                "fresh": fresh,
                "frequency_khz": self.frequency_khz,
                "symbol_rate": self.symbol_rate,
                "mer_db": self.mer_db,
                "callsign": self.callsign,
                "programme": self.programme,
                "modcod": self.modcod,
                "agc1": self.agc1,
                "agc2": self.agc2,
                "video_codec": self.video_codec,
                "audio_codec": self.audio_codec,
                "state": self.state,
            }

    def clear_signal(self):
        """Forget what we were hearing, keeping what we are tuned to.

        Called when Longmynd restarts. The frequency is still true - it
        is what we asked for - but the callsign and MER belong to a
        signal that is no longer being received, and showing a stale
        callsign at the repeater would be worse than showing none.
        """
        with self._lock:
            self.state = 0
            self.mer_db = 0.0
            self.callsign = ""
            self.programme = ""
            self.modcod = 0
            self.agc1 = None
            self.agc2 = None
            self.video_codec = ""
            self.audio_codec = ""
            self._pending_pid = None


def build_quality_lines(snap: dict) -> str:
    """The tagged $n,m block for the quality port, or "" if nothing yet.

    Tagged rather than a column table on purpose. Lynx parses both, but
    its own comments record a live-confirmed bug where a column shift
    fed one receiver's MER into another receiver's display with nothing
    to show for it. A tagged value is whatever its tag says it is.

    Fields are omitted rather than zeroed when they are not known, so
    the repeater can tell "this Slave has no rich status" from "this
    Slave is reporting nothing", which are different faults.
    """
    lines = []

    # Always, lock or no lock: the gains are real either way, and they
    # are exactly what somebody aligning a dish is watching.
    if snap["agc1"] is not None:
        lines.append(f"$26,{snap['agc1']}")
    if snap["agc2"] is not None:
        lines.append(f"$27,{snap['agc2']}")
    dbm = agc_table.agc_to_dbm(snap["agc1"], snap["agc2"])
    if dbm is not None:
        lines.append(f"$85,{dbm:.0f}")

    if snap["locked"]:
        if snap["symbol_rate"]:
            # Longmynd counts symbols per second; Lynx appends "kS/s".
            lines.append(f"$9,{round(snap['symbol_rate'] / 1000.0):g}")
        if snap["mer_db"]:
            # Already divided by ten on the way in.
            lines.append(f"$12,{snap['mer_db']:.1f}")
        if snap["programme"]:
            lines.append(f"$14,{snap['programme']}")
        # $31 and $34 are the tags Lynx already reads for a
        # Picotuner, so nothing changes at the receiving end.
        if snap["video_codec"]:
            lines.append(f"$31,{snap['video_codec']}")
        if snap["audio_codec"]:
            lines.append(f"$34,{snap['audio_codec']}")
        # Only DVB-S2 populates $18. In DVB-S the mode and its threshold
        # would both be guesses, and a guessed margin is worse than none.
        if snap["state"] == DVBS2_STATE and snap["modcod"] in MODCODS:
            name, threshold = MODCODS[snap["modcod"]]
            lines.append(f"$18,{name}")
            if snap["mer_db"]:
                lines.append(f"$30,{snap['mer_db'] - threshold:.1f}")

    return "\n".join(lines)


def read_fifo(path: str, tuner: TunerState, stop: threading.Event):
    """Drain Longmynd's status pipe and parse it.

    Draining matters more than parsing. If this stops, the pipe fills,
    Longmynd blocks writing to it, and the transport stream dies - so
    this loop reopens on any error rather than exiting.

    Opening a FIFO for reading blocks until a writer opens it, which is
    exactly what we want: it rendezvous with Longmynd starting up.
    """
    while not stop.is_set():
        try:
            with open(path, "r") as f:
                for line in f:
                    if stop.is_set():
                        return
                    m = STATUS_RE.match(line.strip())
                    if m:
                        tuner.update(int(m.group(1)), m.group(2))
        except Exception as e:
            if not stop.is_set():
                print(f"[fifo] {type(e).__name__}: {e} - reopening", file=sys.stderr)
                time.sleep(1)


def hand_over_if_not_a_minitiouner():
    """Start the right source even when something else started us.

    A Remote Rx installed before tuner detection existed has a wrapper
    in /usr/local/bin that always runs THIS module, whatever is
    plugged in. That wrapper is a copy, outside the git checkout, so a
    remote update cannot replace it - which would leave every existing
    Remote Rx needing a visit, or at least an ssh session and a sudo
    password, before it could ever drive a Knucker.

    So this module checks too, and hands over. An old wrapper then
    reaches the right source through the update path alone, with
    nothing to reinstall: the Update button is enough.

    LYNX_TUNER_CHOSEN is set by the current wrapper, which has already
    decided - including when the operator forced a tuner with TUNER= in
    the conf. Without this, that explicit choice would be silently
    overridden here, which is precisely what an override is for.
    """
    if os.environ.get("LYNX_TUNER_CHOSEN"):
        return
    # --help is how the updater's second gate checks this module
    # starts. It must answer for itself rather than hand the question
    # to another module, or the gate stops testing what it thinks it
    # is testing.
    if "--help" in sys.argv or "-h" in sys.argv:
        return
    try:
        import detect_tuner
        kind, product, _found = detect_tuner.detect()
    except Exception as e:
        # Never fatal. Carrying on as a Minitiouner is what this
        # Remote Rx did before any of this existed.
        print(f"[tuner] could not check which tuner is fitted ({e}) - "
              f"carrying on as a Minitiouner", file=sys.stderr)
        return
    if kind == "minitiouner":
        return
    if kind == "picotuner":
        # THIS module drives a PicoTuner too, given Ryde's build of
        # longmynd. So when that build is present there is nothing to
        # hand over to: carry on, pointed at it. Handing over to
        # picotuner_source.py here would put a card that can receive
        # into report-only.
        pico = os.path.expanduser(
            os.environ.get("PICOTUNER_DIR", "~/longmynd-pico"))
        if os.path.exists(os.path.join(pico, "longmynd")):
            sys.argv += ["--tuner-kind", "picotuner", "--longmynd-dir", pico]
            print(f"[tuner] {product} found, with its longmynd in {pico}: "
                  f"driving it from here")
            return
        # No build for it, so the report-only source after all.
    target = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          f"{kind}_source.py")
    if not os.path.exists(target):
        print(f"[tuner] {product} found, but there is no {kind}_source.py "
              f"in this checkout - carrying on as a Minitiouner",
              file=sys.stderr)
        return
    print(f"[tuner] {product} found: handing over to {kind}_source.py")
    sys.stdout.flush()
    os.execve(sys.executable,
              [sys.executable, "-u", target] + sys.argv[1:],
              dict(os.environ, LYNX_TUNER_CHOSEN="1"))


def main():
    hand_over_if_not_a_minitiouner()
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    remote_common.add_common_args(ap, "symbol rate in kS/s, e.g. 333")
    ap.add_argument("--video-port", type=int, default=10998,
                    help="plain-UDP video port at the repeater")
    ap.add_argument("--longmynd-dir", default=os.path.expanduser("~/longmynd"),
                    help="where the longmynd binary and its status fifo live")
    ap.add_argument("--tuner-timeout", type=float, default=0.6,
                    help="Longmynd -S scan width, its own default is 0.6")
    # Longmynd drives a Minitiouner or, with Ryde's build of it, a USB
    # PicoTuner. Both are DVB-S/S2 through the same Serit NIM and report
    # the same measurements in the same format, so the only thing that
    # differs is what the heartbeat should call it - and only the
    # wrapper knows, since it is what chose the build. Not detected
    # here: this module is told, so TUNER= in the conf keeps working as
    # an override rather than being second-guessed.
    ap.add_argument("--tuner-kind", choices=("minitiouner", "picotuner"),
                    default="minitiouner",
                    help="which tuner this Longmynd is driving, for the "
                         "heartbeat's TUN line (default minitiouner)")
    ap.add_argument("--transport", choices=("udp", "srt"), default="udp",
                    help="how the video reaches the repeater: plain UDP on a LAN, "
                         "SRT anywhere else (status always stays UDP)")
    ap.add_argument("--srt-port", type=int, default=None,
                    help="the repeater's SRT listener for this Slave; defaults to "
                         f"the status port minus {srt_link.SRT_PORT_OFFSET}, which "
                         "is what Lynx derives")
    ap.add_argument("--srt-latency", type=int, default=300,
                    help="SRT latency in ms; about four round trips. 300 suits "
                         "most broadband, a LAN needs far less")
    args = ap.parse_args()
    code = remote_common.prepare(args, "kS/s", args.tuner_kind)
    if code is not None:
        return code
    if args.srt_port is None:
        args.srt_port = args.status_port - srt_link.SRT_PORT_OFFSET

    binary = os.path.join(args.longmynd_dir, "longmynd")
    if not os.path.exists(binary):
        print(f"No longmynd binary at {binary} - build it first, or pass "
              f"--longmynd-dir", file=sys.stderr)
        return 2

    # Longmynd refuses to start if the fifo is missing, and its error
    # ("Failed to open fifo ... No such file or directory") does not
    # make it obvious that creating it is the fix.
    fifo = os.path.join(args.longmynd_dir, FIFO_NAME)
    if not os.path.exists(fifo):
        os.mkfifo(fifo)
        print(f"[fifo] created {fifo}")

    tuner = TunerState()
    stop = threading.Event()

    # The quality builder is this module's own: a Minitiouner can
    # report AGC, MER and a DVB-S2 margin, which another tuner cannot.
    status = remote_common.StatusSender(args, tuner, build_quality_lines)
    status.start()

    threading.Thread(target=read_fifo, args=(fifo, tuner, stop), daemon=True).start()

    # If this start is a new version on probation, take it off once it
    # has survived. Harmless when there is nothing on probation, which
    # is almost always, so it is started unconditionally rather than
    # guarded by a check that could itself be wrong.
    threading.Thread(target=updater.confirm_after,
                     args=(args.update_file,),
                     kwargs={"stop": stop}, daemon=True).start()

    sender = None
    if args.transport == "srt":
        srt_link.AlignerThread(stop).start()
        sender = srt_link.SrtSender(args.host, args.srt_port, args.srt_latency,
                                    args.name, stop)
        sender.start()

    # TS straight to the repeater rather than to localhost and forwarded.
    # One fewer process, and Longmynd only emits a transport stream while
    # it is locked, so the "send video only when there is something to
    # send" rule needs no code here.
    #
    # UDP: straight to the repeater's shared video port, told apart by
    # source address. Fine on a LAN.
    #
    # SRT (21 Nov 2026): Longmynd sends to loopback, srt_link lines the
    # stream up into whole 1316-byte groups and srt-live-transmit calls
    # the repeater's listener for this Slave, which identifies it by
    # port. Works across the internet and through NAT; status stays UDP.
    if args.transport == "srt":
        ts_host, ts_port = "127.0.0.1", srt_link.LONGMYND_LOCAL_PORT
    else:
        ts_host, ts_port = args.host, args.video_port
    cmd = [binary,
           "-i", ts_host, str(ts_port),
           "-S", str(args.tuner_timeout),
           str(args.freq), str(args.sr)]

    print(f"Slave Rx (Longmynd) -> {args.host}")
    print(f"  tuner  : {args.freq} kHz, {args.sr} kS/s")
    if args.transport == "srt":
        print(f"  video  : SRT to port {args.srt_port}, latency {args.srt_latency} ms")
    else:
        print(f"  video  : UDP port {args.video_port}")
    print(f"  status : UDP port {args.status_port}, every {args.interval}s")
    print(f"  quality: UDP port {args.quality_port}")
    print(f"  name   : {args.name or '(not set - repeater will show a generic label)'}")
    print(f"  version: {args.version_commit} on {args.version_branch}")
    print(f"  config : " + (f"remote, settings version {args.settings_version}"
                            if args.key else "local only (no KEY in the conf)"))
    print("Ctrl-C to stop.\n")

    proc = None
    stopping = False

    def shutdown(signum, frame):
        nonlocal stopping
        stopping = True
        stop.set()
        status.running = False
        if sender:
            sender.terminate()
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    if args.key:
        # New settings: stop cleanly; systemd restarts us with them.
        remote_common.SettingsReceiver(args, status.sock,
                         lambda: os.kill(os.getpid(), signal.SIGTERM)).start()

    last_reported = None
    while not stopping:
        started = time.time()
        print("[tuner] starting longmynd")
        # Inherited rather than discarded: under systemd these become
        # journal entries, so "rc=-11" arrives with whatever Longmynd
        # printed on the way down instead of on its own.
        proc = subprocess.Popen(cmd, cwd=args.longmynd_dir,
                                preexec_fn=os.setsid,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

        last_searching = 0.0
        while proc.poll() is None and not stopping:
            snap = tuner.snapshot()
            now = (snap["locked"], snap["callsign"])
            if now != last_reported:
                if snap["locked"]:
                    print(f"[tuner] locked - {snap['callsign'] or '(no callsign)'} "
                          f"{snap['frequency_khz'] / 1000.0:.3f} MHz "
                          f"MER {snap['mer_db']:.1f} dB")
                elif last_reported is not None:
                    print("[tuner] lock lost")
                last_reported = now
            # A tuner that has never locked changed state never, so the
            # loop above printed nothing at all and the journal simply
            # stopped - indistinguishable, over SSH to a loft, from a
            # crashed service. Say so periodically instead, and say what
            # it is looking for: the usual cause is that it is tuned
            # somewhere the signal is not.
            elif not snap["locked"]:
                nowt = time.time()
                if nowt - last_searching >= SEARCHING_REPORT_SECS:
                    last_searching = nowt
                    print(f"[tuner] searching - {args.freq / 1000.0:.3f} MHz, "
                          f"{args.sr} kS/s, no lock for "
                          f"{nowt - started:.0f}s")
            time.sleep(0.5)

        if stopping:
            break

        # Longmynd died. Forget the signal but keep sending the
        # heartbeat: the site is still there, it just has nothing.
        tuner.clear_signal()
        last_reported = None
        ran = time.time() - started
        print(f"[tuner] longmynd exited (rc={proc.returncode}) after {ran:.0f}s "
              f"- restarting in {RESTART_DELAY_SECS:.0f}s")
        time.sleep(RESTART_DELAY_SECS)

    print(f"\nStopped. {status.sent} heartbeats sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
