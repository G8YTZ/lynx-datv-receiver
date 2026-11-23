#!/usr/bin/env python3
"""What every Remote Rx does, whatever tuner is in front of it.

A Remote Rx is a tuner plus a contract with the repeater, and the
contract is the same whether the tuner is a Minitiouner driven by
Longmynd or a Knucker driven by CombiTunerExpress: Send a heartbeat
every couple of seconds, carry the picture over SRT, accept signed
settings in reply to that heartbeat, and accept a signed command to
update yourself.

This module is the contract. The tuner modules are the parts that
differ: How to drive the hardware, how to read its status, and what
rich quality figures it can honestly report.

WHAT IS HERE AND WHAT IS NOT
----------------------------
Here: The heartbeat and its sockets, the settings and update channel,
the on-card settings file, and reading this checkout's version.

Not here: Anything that knows what a MER is, or an AGC, or a PLP. A
tuner module supplies a snapshot dict and a function that turns it into
quality lines, and this module neither knows nor cares what is in them.
That is why StatusSender takes the quality builder as an argument
rather than calling one by name: Two tuners with different capabilities
would otherwise be fighting over the same function.
"""

import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time

import updater

# Lynx binds 9997 on all interfaces for its own local Picotuner, so a
# Remote Rx sending there would collide with the receiver's own tuner.
DEFAULT_STATUS_PORT = 10997

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

# Remote configuration. The repeater answers our heartbeat with our
# settings when we are running an older version; we check its signature
# with the key shared at install, save them so a power cut brings us
# back the same, and restart to apply them.
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


def local_version(slave_dir: str):
    """This checkout's short commit and branch, or ("-", "-").

    Read once at start-up rather than per heartbeat: it only changes
    when the Remote Rx is updated, and that restarts the service.

    A Remote Rx installed by copying files rather than by cloning has
    no git metadata, and a repeater that cannot be told what the remote
    is running simply says so instead of guessing.
    """
    def git(*a):
        try:
            out = subprocess.run(["git", "-C", slave_dir, *a],
                                 capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    commit = git("rev-parse", "--short", "HEAD")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    # A dirty checkout is worth flagging: it means somebody edited the
    # Remote Rx in place, and an update would overwrite their work.
    # Tracked files only: the repository ignores __pycache__, but a
    # stray file somebody left in the directory is not a reason to
    # call their checkout modified.
    if commit and git("status", "--porcelain", "--untracked-files=no"):
        commit += "+"
    return (commit or "-", branch or "-")


def parse_settings_packet(data: bytes, key: str):
    """The settings in a reply from the repeater, or None if it is not
    one, is not signed with our key, or is malformed.

    "sr" is the second tuning parameter, whatever that means for the
    tuner in question: Symbol rate in kS/s for a Minitiouner, channel
    bandwidth in kHz for a Knucker. The repeater sends one number and
    the tuner module decides what it is called.
    """
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


def build_status_line(snap: dict, fallback_khz: int) -> str:
    """One line in the format Lynx's picotuner_monitor() already parses.

    Locked:    "RX1 438.021 G8YTZ"
    Unlocked:  "RX1 438.000 search"

    ALWAYS two fields. Lynx reads the first token as the frequency, so a
    single-field line would display the word "search" where a number
    belongs.

    The frequency reported is the tuner's own measurement when it gives
    one, falling back to what we asked for. They differ by the carrier
    offset - Longmynd read 438021 for a 438000 request - and the
    measured value is the more useful of the two.
    """
    khz = snap["frequency_khz"] or fallback_khz
    freq = f"{khz / 1000.0:.3f}"
    if snap["locked"] and snap["callsign"]:
        return f"RX1 {freq} {snap['callsign']}"
    if snap["locked"]:
        # Locked, but no callsign to report: either the transmitter did
        # not populate the SDT, or this tuner does not read it at all.
        # The lock is still worth reporting.
        return f"RX1 {freq} LOCKED"
    return f"RX1 {freq} search"


def build_site_line(name, locator) -> str:
    """"SITE <locator> <name>", locator first and always present.

    Locator first because a name contains spaces and a locator does not,
    so name-first cannot be split reliably. "-" when unset keeps the
    field count fixed.
    """
    return f"SITE {locator or '-'} {name}"


class StatusSender(threading.Thread):
    """Sends the heartbeat, independently of everything else.

    Its own thread deliberately: it must keep going while the tuner
    software is restarting, and must not be delayed by anything
    happening to the tuner. The heartbeat stopping is what tells the
    repeater the site has gone, so it stops only when this process does.

    quality_builder is the tuner module's own function: it takes a
    snapshot and returns the tagged quality block, or "" when the tuner
    has nothing rich to say. Passed in rather than imported so that two
    tuners reporting different things do not have to share one
    function.
    """

    def __init__(self, args, tuner, quality_builder):
        super().__init__(daemon=True)
        self.args = args
        self.tuner = tuner
        self.quality_builder = quality_builder
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
        # "VER <commit> <branch>": what code this Remote Rx is running,
        # so the repeater can say whether it is behind. Fixed two
        # fields, "-" when unknown, so the parser never has to guess.
        ver_line = f"VER {self.args.version_commit} {self.args.version_branch}"
        # "TUN <kind>": which tuner this is, so the repeater can show the
        # fields that tuner actually measures rather than a Minitiouner's
        # labels with gaps in them. One token, no spaces. An older Remote
        # Rx sends no TUN line at all, which the repeater has to read as
        # a Minitiouner - every one built so far is one.
        tun_line = f"TUN {getattr(self.args, 'tuner_kind', 'minitiouner')}"
        while self.running:
            snap = self.tuner.snapshot()
            rx = build_status_line(snap, self.args.freq)
            payload = f"{site}\n{rx}" if site else rx
            payload += "\n" + cfg_line + "\n" + ver_line + "\n" + tun_line
            try:
                sock.sendto((payload + "\n").encode(),
                            (self.args.host, self.args.status_port))
                self.sent += 1
                # Second port, same thread and same cadence. A
                # separate thread would let the two drift apart, and
                # a panel showing a MER from one moment beside a lock
                # state from another is how you chase a fault that is
                # not there.
                quality = self.quality_builder(snap)
                if quality:
                    sock.sendto((quality + "\n").encode(),
                                (self.args.host, self.args.quality_port))
            except OSError as e:
                # Never fatal. A Remote Rx whose link is down keeps
                # trying: it is the end nobody can reach to restart
                # anything.
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

    def handle_update(self, cmd, addr):
        """Act on an update command, once.

        Runs on this thread: it takes a minute or two and the heartbeat
        is on its own thread, so the repeater keeps hearing from the
        Remote Rx throughout. A failed update is reported and life goes
        on with the code that is already running.
        """
        if cmd["v"] <= self.args.update_version:
            return   # already done, or a replayed packet
        self.args.update_version = cmd["v"]
        print(f"[update] asked by {addr[0]} to update to {cmd['ref']}")
        ok, msg, restart = updater.perform_update(
            self.args.slave_dir, self.args.slave_dir, cmd["ref"],
            self.args.update_file)
        print(f"[update] {msg}")
        if ok and restart:
            self.on_new()

    def run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except OSError:
                time.sleep(1)
                continue
            # An update command, on the same socket and signed the same
            # way, under its own magic. Checked first: both parsers
            # reject anything that is not theirs, so order only decides
            # which does the rejecting.
            cmd = updater.parse_command_packet(data, self.args.key)
            if cmd is not None:
                self.handle_update(cmd, addr)
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
                  f"{st['freq_khz']} kHz, {st['sr']} ({self.args.sr_units}), "
                  f"name {st['name']!r}, locator {st['locator']!r} "
                  f"- saved, restarting to apply")
            self.on_new()
            return


def add_common_args(ap, sr_help: str):
    """The arguments every Remote Rx takes, whatever its tuner.

    sr_help differs because the second tuning parameter does: symbol
    rate for a Minitiouner, channel bandwidth for a Knucker.
    """
    ap.add_argument("host", help="Lynx receiver's address at the repeater end")
    ap.add_argument("--freq", type=int, required=True,
                    help="frequency in kHz, e.g. 438000")
    ap.add_argument("--sr", type=int, required=True, help=sr_help)
    ap.add_argument("--name", default="",
                    help='what this Remote Rx calls itself, e.g. "GB3OO North"')
    ap.add_argument("--locator", default="", help="Maidenhead locator of this site")
    ap.add_argument("--status-port", type=int, default=DEFAULT_STATUS_PORT)
    ap.add_argument("--quality-port", type=int, default=None,
                    help=f"defaults to the status port minus {QUALITY_PORT_OFFSET}")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--key", default="",
                    help="shared key for settings and update commands from "
                         "the repeater; without it this Remote Rx is "
                         "configured locally only")
    ap.add_argument("--settings-file", default=DEFAULT_SETTINGS_FILE)
    return ap


def prepare(args, sr_units: str, tuner_kind: str):
    """Everything between parsing the arguments and starting the tuner.

    Resolves the derived ports, applies any settings the repeater sent
    before, works out what version this is, and runs the update
    rollback check.

    tuner_kind is what this source module drives, reported in the
    heartbeat so the repeater can label the panel for the right tuner.
    Each module states its own rather than being asked to detect it:
    detection decides which module to RUN, and once running a module
    knows perfectly well what it is.

    Returns None to carry on, or an exit code the caller should return
    at once: 0 after a rollback (systemd is about to bring us back on
    the restored version), 2 for a configuration error.
    """
    args.sr_units = sr_units
    args.tuner_kind = tuner_kind
    if args.quality_port is None:
        args.quality_port = args.status_port - QUALITY_PORT_OFFSET

    # Settings from the repeater win over the conf: they are the latest
    # thing anyone asked for, and they survive a power cut on the card.
    saved = load_settings(args.settings_file) if args.key else {}
    args.slave_dir = os.path.dirname(os.path.abspath(__file__))
    args.update_file = updater.DEFAULT_UPDATE_FILE
    args.update_version = 0

    # Before anything else: was the last start a new version that has
    # not yet proved itself? Each start counts against it, and past the
    # limit the previous commit is restored and we exit so systemd
    # brings us back on the old code. This is the net under a bad
    # update that got past the compile and start checks.
    rolled, why = updater.startup_check(args.slave_dir, args.update_file)
    if rolled:
        print(f"[update] {why} - restarting on the restored version")
        return 0

    args.version_commit, args.version_branch = local_version(args.slave_dir)

    args.settings_version = int(saved.get("v", 0) or 0)
    if saved:
        args.freq = int(saved.get("freq_khz") or args.freq)
        args.sr = int(saved.get("sr") or args.sr)
        if saved.get("name"):
            args.name = saved["name"]
        # Tested for presence rather than truth, so the repeater can
        # clear a locator by sending an empty one. "or" here would make
        # an empty locator mean "keep the old one", and a Remote Rx
        # that cannot be told to forget where it was is a nuisance.
        if "locator" in saved:
            args.locator = saved["locator"]

    if args.status_port == 9997:
        print("Refusing port 9997: Lynx binds that for its own local Picotuner.",
              file=sys.stderr)
        return 2
    return None
