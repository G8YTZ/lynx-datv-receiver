#!/usr/bin/env python3
"""
Slave Rx - Longmynd tuner source.

Runs a MiniTiouner converted with the BATC Pico adaptor as a Slave Rx:
drives it with Longmynd over USB, sends the transport stream to the
repeater, and translates Longmynd's status into the format Lynx already
parses.

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
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import agc_table
import srt_link

# Lynx binds 9997 on all interfaces for its own local Picotuner, so a
# Slave sending there would collide with the receiver's own tuner.
DEFAULT_STATUS_PORT = 10997

# Every Slave sends video to this one port at the repeater; the relay
# tells them apart by source address, which is the same address their
# status arrives from, so identity comes free.
DEFAULT_VIDEO_PORT = 10998

# Heartbeat cadence. Slow on purpose: a Picotuner emits status about 19
# times a second regardless of lock, which is roughly 11 GB a month of
# saying nothing is happening. On a cellular or solar site that is a
# third of a typical allowance spent on silence.
DEFAULT_INTERVAL = 2.0

# The quality port sits this far below the status port, matching the
# relationship Lynx's own Picotuner uses between 9997 and 9901. Derived
# at both ends from one configured number rather than being a second
# number to keep in step.
QUALITY_PORT_OFFSET = 96

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

# MPEG-2 stream types, as $17 reports them. Split by kind rather
# than by arrival order: Longmynd cycles through the elementary
# streams and the order is not something to rely on, particularly on
# a service carrying more than one audio track.
VIDEO_STREAM_TYPES = {
    1:  "MPEG1",
    2:  "MPEG2",
    27: "H264",
    36: "H265",
}
AUDIO_STREAM_TYPES = {
    3:   "MPA",
    4:   "MPA",
    15:  "AAC",
    17:  "AAC",
    129: "AC3",
}

# Both tables together, only so an unknown type can be told from a
# known one without asking twice.
STREAM_TYPES = {**VIDEO_STREAM_TYPES, **AUDIO_STREAM_TYPES}


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

# Remote configuration (21 Nov 2026). The repeater answers our heartbeat
# with our settings when we are running an older version; we check its
# signature with the key shared at install, save them so a power cut
# brings us back the same, and restart to apply them.
SETTINGS_MAGIC = "LYNXCFG1"
DEFAULT_SETTINGS_FILE = "/var/lib/lynx-slave/settings.json"


def load_settings(path):
    """The last settings the repeater sent, or {} if there are none."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(path, data):
    """Write the settings so that a power cut at any moment leaves either
    the old file or the new one, never half of one: write a temporary
    file, flush it to the card, rename it over the old one, flush the
    directory. The previous version is kept beside it."""
    d = os.path.dirname(path)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            os.replace(path, path + ".prev")
        except OSError:
            pass
    os.replace(tmp, path)
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def parse_settings_packet(data: bytes, key: str):
    """The settings in a reply from the repeater, or None if it is not
    one, is not signed with our key, or is malformed."""
    try:
        magic, body, sig = data.decode().split("\n")[:3]
    except (UnicodeDecodeError, ValueError):
        return None
    if magic != SETTINGS_MAGIC:
        return None
    want = hmac.new(key.encode(), (magic + "\n" + body).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig.strip()):
        return None
    try:
        st = json.loads(body)
        st["v"], st["freq_khz"], st["sr"] = int(st["v"]), int(st["freq_khz"]), int(st["sr"])
    except (ValueError, KeyError, TypeError):
        return None
    if st["freq_khz"] <= 0 or st["sr"] <= 0:
        return None
    st["name"] = str(st.get("name", ""))[:40]
    st["locator"] = str(st.get("locator", ""))[:10]
    return st


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


def build_status_line(snap: dict, args) -> str:
    """One line in the format Lynx's picotuner_monitor() already parses.

    Locked:    "RX1 438.021 G8YTZ"
    Unlocked:  "RX1 438.000 search"

    ALWAYS two fields. Lynx reads the first token as the frequency, so a
    single-field line would display the word "search" where a number
    belongs.

    The frequency reported is Longmynd's own ($6) when it has told us
    one, falling back to what we asked for. They differ by the carrier
    offset - $6 read 438021 for a 438000 request tonight - and the
    measured value is the more useful of the two.
    """
    khz = snap["frequency_khz"] or args.freq
    freq = f"{khz / 1000.0:.3f}"
    if snap["locked"] and snap["callsign"]:
        return f"RX1 {freq} {snap['callsign']}"
    if snap["locked"]:
        # Locked but the transmitter did not populate the SDT. Reporting
        # the lock is still worth doing; there is simply no callsign to
        # go with it.
        return f"RX1 {freq} LOCKED"
    return f"RX1 {freq} search"


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


def build_site_line(name, locator) -> str:
    """"SITE <locator> <name>", locator first and always present.

    Locator first because a name contains spaces and a locator does not,
    so name-first cannot be split reliably. "-" when unset keeps the
    field count fixed.
    """
    return f"SITE {locator or '-'} {name}"


class StatusSender(threading.Thread):
    """Sends the heartbeat, independently of everything else.

    Its own thread deliberately: it must keep going while Longmynd is
    restarting, and must not be delayed by anything happening to the
    tuner. The heartbeat stopping is what tells the repeater the site
    has gone, so it stops only when this process does.
    """

    def __init__(self, args, tuner: TunerState):
        super().__init__(daemon=True)
        self.args = args
        self.tuner = tuner
        self.running = True
        self.sent = 0
        # Bound now, so the settings receiver can listen on it from the
        # start: the repeater replies to the address and port our
        # heartbeat comes from, which is what gets through NAT.
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("", 0))

    def run(self):
        sock = self.sock
        site = build_site_line(self.args.name, self.args.locator) if self.args.name else None
        # "CFG <version>": the settings we are running, so the repeater
        # knows whether to send newer ones. "CFG -": no key, so none can
        # be applied here.
        cfg_line = f"CFG {self.args.settings_version}" if self.args.key else "CFG -"
        while self.running:
            snap = self.tuner.snapshot()
            rx = build_status_line(snap, self.args)
            payload = f"{site}\n{rx}" if site else rx
            payload += "\n" + cfg_line
            try:
                sock.sendto((payload + "\n").encode(),
                            (self.args.host, self.args.status_port))
                self.sent += 1
                # Second port, same thread and same cadence. A
                # separate thread would let the two drift apart, and
                # a panel showing a MER from one moment beside a lock
                # state from another is how you chase a fault that is
                # not there.
                quality = build_quality_lines(snap)
                if quality:
                    sock.sendto((quality + "\n").encode(),
                                (self.args.host, self.args.quality_port))
            except OSError as e:
                # Never fatal. A Slave whose link is down keeps trying:
                # it is the end nobody can reach to restart anything.
                print(f"[status] send failed ({e}) - continuing", file=sys.stderr)
            time.sleep(self.args.interval)


class SettingsReceiver(threading.Thread):
    """Listens on the heartbeat's own socket for settings from the
    repeater. A newer, correctly signed set is saved, then the service
    restarts itself to apply it (systemd brings it back in about five
    seconds, reading the saved file)."""

    def __init__(self, args, sock, on_new):
        super().__init__(daemon=True, name="settings")
        self.args = args
        self.sock = sock
        self.on_new = on_new

    def run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                time.sleep(1)
                continue
            st = parse_settings_packet(data, self.args.key)
            if st is None:
                continue
            if st["v"] <= self.args.settings_version:
                continue   # what we have, or older: a late or replayed packet
            try:
                save_settings(self.args.settings_file, st)
            except OSError as e:
                print(f"[settings] could not save {self.args.settings_file}: {e}",
                      file=sys.stderr)
                continue
            print(f"[settings] version {st['v']} from {addr[0]}: "
                  f"{st['freq_khz']} kHz, {st['sr']} kS/s, name {st['name']!r}, "
                  f"locator {st['locator']!r} - saved, restarting to apply")
            self.on_new()
            return


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


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host", help="Lynx receiver's address at the repeater end")
    ap.add_argument("--freq", type=int, required=True, help="frequency in kHz, e.g. 438000")
    ap.add_argument("--sr", type=int, required=True, help="symbol rate in kS/s, e.g. 333")
    ap.add_argument("--name", default="", help='what this Slave calls itself, e.g. "GB3OO Slave"')
    ap.add_argument("--locator", default="", help="Maidenhead locator of this site")
    ap.add_argument("--status-port", type=int, default=DEFAULT_STATUS_PORT)
    ap.add_argument("--video-port", type=int, default=DEFAULT_VIDEO_PORT)
    ap.add_argument("--quality-port", type=int, default=None,
                    help="rich status port; defaults to the status port minus "
                         f"{QUALITY_PORT_OFFSET}, which is what Lynx derives")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--longmynd-dir", default=os.path.expanduser("~/longmynd"),
                    help="where the longmynd binary and its status fifo live")
    ap.add_argument("--tuner-timeout", type=float, default=0.6,
                    help="Longmynd -S scan width, its own default is 0.6")
    ap.add_argument("--transport", choices=("udp", "srt"), default="udp",
                    help="how the video reaches the repeater: plain UDP on a LAN, "
                         "SRT anywhere else (status always stays UDP)")
    ap.add_argument("--srt-port", type=int, default=None,
                    help="the repeater's SRT listener for this Slave; defaults to "
                         f"the status port minus {srt_link.SRT_PORT_OFFSET}, which "
                         "is what Lynx derives")
    ap.add_argument("--key", default="",
                    help="shared with the repeater, which signs the settings it "
                         "sends with it; without one the Slave cannot be configured "
                         "remotely")
    ap.add_argument("--settings-file", default=DEFAULT_SETTINGS_FILE,
                    help="where settings from the repeater are kept, so a power "
                         "cut brings the Slave back the same")
    ap.add_argument("--srt-latency", type=int, default=300,
                    help="SRT latency in ms; about four round trips. 300 suits "
                         "most broadband, a LAN needs far less")
    args = ap.parse_args()
    if args.quality_port is None:
        args.quality_port = args.status_port - QUALITY_PORT_OFFSET
    if args.srt_port is None:
        args.srt_port = args.status_port - srt_link.SRT_PORT_OFFSET

    # Settings from the repeater win over the conf: they are the latest
    # thing anyone asked for, and they survive a power cut on the card.
    saved = load_settings(args.settings_file) if args.key else {}
    args.settings_version = int(saved.get("v", 0) or 0)
    if saved:
        args.freq = int(saved.get("freq_khz") or args.freq)
        args.sr = int(saved.get("sr") or args.sr)
        if saved.get("name"):
            args.name = saved["name"]
        if "locator" in saved:
            args.locator = saved["locator"]

    if args.status_port == 9997:
        print("Refusing port 9997: Lynx binds that for its own local Picotuner.",
              file=sys.stderr)
        return 2

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

    status = StatusSender(args, tuner)
    status.start()

    threading.Thread(target=read_fifo, args=(fifo, tuner, stop), daemon=True).start()

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
        SettingsReceiver(args, status.sock,
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
