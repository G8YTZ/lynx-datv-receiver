#!/usr/bin/env python3
"""
lynx_gnss.py - automatic Maidenhead locator from a GNSS receiver.

Written against real output from a Waveshare L76K HAT on a Pi 500+.
That module reports GPS, GLONASS, BeiDou and QZSS, and emits combined
"GN" sentences with per-constellation "GP"/"GL"/"GB" satellite views -
so the parser accepts any talker ID rather than only "GP", which is the
usual mistake.

WHAT THIS IS FOR
----------------
Portable operation. A receiver taken to a hilltop for a survey should
report where it actually is, not where it was last configured. Fixed
sites simply do not fit the HAT and are unaffected.

WHY A STABILITY WINDOW
----------------------
A 6-character locator square is about 5.8 x 4.6 km at UK latitudes, so
a metre or two of GPS wander cannot flip it - you would have to be
parked within metres of a boundary. But "cannot normally" is not
"never", and a locator that oscillates between two squares would put
noise into QRZ logging and Pathfinder. Requiring the same square for a
sustained period costs nothing and removes the possibility.

VALIDITY IS CHECKED TWICE
-------------------------
A receiver that has lost lock will happily keep emitting its last
position with the validity flag set to void. Reading the coordinates
without checking the flags is the classic way to end up reporting a
stale fix as though it were current - so both the GGA quality digit and
the RMC status letter must be good before a position is accepted.
"""

import datetime
import re
import struct
import time


# ── Maidenhead ──────────────────────────────────────────────────────

def to_locator(lat, lon, length=6):
    """Maidenhead locator for a latitude and longitude in degrees.

    The inverse of the conversion Pathfinder already does when placing
    a station on the map.
    """
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError(f"position out of range: {lat}, {lon}")

    lon += 180.0
    lat += 90.0
    A = ord('A')

    out = chr(A + int(lon // 20)) + chr(A + int(lat // 10))
    lon %= 20.0
    lat %= 10.0

    out += str(int(lon // 2)) + str(int(lat // 1))
    if length <= 4:
        return out

    lon %= 2.0
    lat %= 1.0
    out += chr(A + int(lon / (2.0 / 24))).lower()
    out += chr(A + int(lat / (1.0 / 24))).lower()
    if length <= 6:
        return out

    # Extended square: each subsquare divided ten by ten. About
    # 580 x 460 m at UK latitudes, which is still comfortably larger
    # than the module's 2 m accuracy - so this is display detail, not a
    # source of instability.
    lon %= (2.0 / 24)
    lat %= (1.0 / 24)
    out += str(int(lon / (2.0 / 240)))
    out += str(int(lat / (1.0 / 240)))
    return out


# ── NMEA ────────────────────────────────────────────────────────────

def nmea_checksum_ok(sentence):
    """Verify the trailing *hh checksum.

    Worth doing: a partly-received sentence looks like a valid one right
    up until the point where it silently isn't, and a corrupted digit in
    a latitude field would move the receiver kilometres.
    """
    s = sentence.strip()
    if not s.startswith('$') or '*' not in s:
        return False
    body, _, given = s[1:].partition('*')
    try:
        want = int(given[:2], 16)
    except ValueError:
        return False
    got = 0
    for ch in body:
        got ^= ord(ch)
    return got == want


def _dm_to_degrees(value, hemisphere):
    """NMEA ddmm.mmmm / dddmm.mmmm to signed decimal degrees."""
    if not value or not hemisphere:
        return None
    try:
        dot = value.index('.')
    except ValueError:
        return None
    if dot < 3:
        return None
    degrees = int(value[:dot - 2])
    minutes = float(value[dot - 2:])
    deg = degrees + minutes / 60.0
    return -deg if hemisphere.upper() in ('S', 'W') else deg


# Any talker: GN for combined, GP/GL/GB/GA per constellation.
_GGA = re.compile(r'^\$G[A-Z]GGA,')
_RMC = re.compile(r'^\$G[A-Z]RMC,')


def parse_gga(sentence):
    """Position, fix quality and satellite count, or None.

    Returns None rather than a partial result when the fix is not
    usable, so a caller cannot accidentally treat "no fix" as "at the
    equator" - which is what reading empty coordinate fields as zero
    would give.
    """
    if not _GGA.match(sentence) or not nmea_checksum_ok(sentence):
        return None
    f = sentence.split(',')
    if len(f) < 10:
        return None
    try:
        quality = int(f[6]) if f[6] else 0
    except ValueError:
        return None
    if quality == 0:              # 0 = no fix
        return None
    lat = _dm_to_degrees(f[2], f[3])
    lon = _dm_to_degrees(f[4], f[5])
    if lat is None or lon is None:
        return None
    try:
        sats = int(f[7]) if f[7] else 0
    except ValueError:
        sats = 0
    try:
        hdop = float(f[8]) if f[8] else None
    except ValueError:
        hdop = None
    try:
        alt = float(f[9]) if f[9] else None
    except ValueError:
        alt = None
    return {"lat": lat, "lon": lon, "quality": quality,
            "satellites": sats, "hdop": hdop, "altitude_m": alt}


def _rmc_utc_datetime(time_field, date_field):
    """Combines RMC's own UTC time-of-day (hhmmss.sss, field 1) and
    date (ddmmyy, field 9) into one timezone-aware datetime - RMC is
    self-sufficient for this, unlike GGA which carries time-of-day but
    no date at all. Returns None for anything that doesn't parse
    cleanly rather than guessing: an 'A'-status sentence with a
    malformed date/time field would itself be a sign of a corrupted
    sentence, and corrupted time is worse than no time at all for
    anything that might go on to set a system clock from it.

    2000 + yy for the two-digit year: GPS itself has no concept of
    century, but GPS didn't exist before 1980 and yy won't reach a
    genuinely ambiguous value within this hardware's working lifetime.
    """
    try:
        if len(time_field) < 6 or len(date_field) != 6:
            return None
        hh = int(time_field[0:2])
        mm = int(time_field[2:4])
        ss = float(time_field[4:])
        dd = int(date_field[0:2])
        mo = int(date_field[2:4])
        yy = int(date_field[4:6])
        sec = int(ss)
        micro = int(round((ss - sec) * 1_000_000))
        return datetime.datetime(2000 + yy, mo, dd, hh, mm, sec, micro,
                                  tzinfo=datetime.timezone.utc)
    except (ValueError, IndexError):
        return None


def parse_rmc(sentence):
    """Position, validity and UTC date/time, or None if the status is
    not 'A'. The "utc" key is additive - existing callers reading only
    lat/lon/valid are unaffected."""
    if not _RMC.match(sentence) or not nmea_checksum_ok(sentence):
        return None
    f = sentence.split(',')
    if len(f) < 10:
        return None
    if f[2] != 'A':               # A = valid, V = warning/void
        return None
    lat = _dm_to_degrees(f[3], f[4])
    lon = _dm_to_degrees(f[5], f[6])
    if lat is None or lon is None:
        return None
    utc = _rmc_utc_datetime(f[1], f[9])
    if utc is None:
        return None
    return {"lat": lat, "lon": lon, "valid": True, "utc": utc}


# ── stability gate ──────────────────────────────────────────────────

def _fix_meets_quality(fix, min_satellites, max_hdop):
    """True if a parsed GGA fix meets the given satellite-count/HDOP
    bar - the same quality gate LocatorTracker.update() applies,
    pulled out as its own pure function so it's independently testable
    and reusable without also requiring LocatorTracker's own 30s
    stability window. GnssReader uses this directly for GPS time sync:
    time doesn't wander the way position does, so a single
    good-quality fix is enough to trust it immediately, whereas
    position needs to prove itself stable first to avoid a locator
    flip at a boundary."""
    return (fix.get("satellites", 0) >= min_satellites
            and (fix.get("hdop") is None or fix["hdop"] <= max_hdop))


class LocatorTracker:
    """Accepts a locator only once it has been stable for a while.

    Also requires a minimum satellite count and a maximum HDOP: a fix
    computed from four satellites with an HDOP of 20 is technically a
    fix, but not one to log a contact against.
    """

    def __init__(self, stable_secs=30.0, min_satellites=4, max_hdop=5.0,
                 length=6, clock=time.monotonic):
        self.stable_secs = stable_secs
        self.min_satellites = min_satellites
        self.max_hdop = max_hdop
        self.length = length
        self._clock = clock
        self.locator = None          # the accepted, stable value
        self._candidate = None
        self._candidate_since = None
        self.last_fix = None

    def update(self, fix):
        """Feed a parsed GGA. Returns the accepted locator, or None.

        A fix that fails the quality bar does NOT clear an already
        accepted locator - losing sight of the sky for a moment should
        not throw away a position that was good a second ago. It only
        stops a new one being adopted.
        """
        if not fix:
            self._candidate = None
            self._candidate_since = None
            return self.locator

        if not _fix_meets_quality(fix, self.min_satellites, self.max_hdop):
            return self.locator

        self.last_fix = fix
        try:
            loc = to_locator(fix["lat"], fix["lon"], self.length)
        except ValueError:
            return self.locator

        now = self._clock()
        if loc != self._candidate:
            self._candidate = loc
            self._candidate_since = now
            return self.locator

        if (self.locator != loc
                and now - self._candidate_since >= self.stable_secs):
            self.locator = loc
        return self.locator

    def seconds_pending(self):
        """How long the current candidate still needs, or None."""
        if self._candidate is None or self._candidate == self.locator:
            return None
        return max(0.0, self.stable_secs - (self._clock() - self._candidate_since))


# ── constellation selection ─────────────────────────────────────────

# $PCAS04 modes. QZSS is always enabled and cannot be configured.
# Note the board ships as GPS+GLONASS despite one Waveshare page
# claiming GPS+BeiDou is the default - the sentences the module
# actually emits settle it.
CONSTELLATIONS = {
    1: "GPS",
    2: "BeiDou",
    3: "GPS + BeiDou",
    4: "GLONASS",
    5: "GPS + GLONASS",
    6: "BeiDou + GLONASS",
    7: "GPS + BeiDou + GLONASS",
}


def pcas_sentence(body):
    """Build a CASIC $PCAS command with its checksum.

    The chip is an AT6558R and takes CASIC's PCAS sentences, NOT
    MediaTek's PMTK - which one Waveshare FAQ wrongly suggests. A PMTK
    command is silently ignored, so this is worth getting right.
    """
    csum = 0
    for ch in body:
        csum ^= ord(ch)
    return f"${body}*{csum:02X}\r\n"


def constellation_command(mode):
    """$PCAS04 sentence for a constellation mode."""
    if mode not in CONSTELLATIONS:
        raise ValueError(f"unknown constellation mode {mode}; "
                         f"expected one of {sorted(CONSTELLATIONS)}")
    return pcas_sentence(f"PCAS04,{mode}")


def baud_command(baud):
    """$PCAS01 sentence to change the module's serial rate."""
    rates = {4800: 0, 9600: 1, 19200: 2, 38400: 3, 57600: 4, 115200: 5}
    if baud not in rates:
        raise ValueError(f"unsupported baud {baud}")
    return pcas_sentence(f"PCAS01,{rates[baud]}")


# ── chrony time sync (SOCK refclock) ─────────────────────────────────
# Feeds GPS time to chrony's SOCK refclock driver, letting chrony's own
# mature, well-tested source-selection algorithm pick between this and
# NTP - deliberately not a manual "prefer NTP unless unreachable"
# switch here, since chrony already does exactly that natively: it
# tracks each source's own reachability and estimated accuracy and
# picks the best available one, falling back to GPS automatically the
# moment NTP genuinely becomes unreachable (the normal case at a
# remote portable site with no internet), and preferring NTP whenever
# it's actually reachable (the normal case at home) - with no custom
# precedence logic needed on this end at all.
#
# Deliberately NOT gpsd + chrony's SHM driver, the more commonly
# documented combination - gpsd needs exclusive ownership of the
# serial port, which would conflict directly with GnssReader's own
# existing, already-proven ownership of /dev/ttyAMA0. Talking to
# chrony's SOCK driver directly from here needs no second process and
# no port-sharing question at all - this module keeps sole ownership
# of the serial port exactly as it always has.
#
# Requires a matching chrony.conf entry, e.g.:
#   refclock SOCK /var/run/chrony.gnss.sock refid GPS precision 1e-1
# The socket path is created BY CHRONY when it starts, not by this
# module - this only ever connects to an already-existing socket, and
# fails quietly (see _connect_chrony_sock) if chrony isn't configured
# for it yet, matching this module's own "no HAT, no problem" fail-
# quiet philosophy throughout.
#
# WORTH VERIFYING ON REAL HARDWARE: the struct layout below matches
# chrony's own sock_sample struct (sock.h) for a 64-bit Linux target
# (qqdiiii - tv_sec, tv_usec, offset, pulse, leap, padding, magic;
# 40 bytes, naturally aligned) - this is the one part of the whole
# GNSS feature that can't be proven by the test suite, since it
# depends on byte-for-byte agreement with chrony's own compiled C
# struct rather than anything testable in pure Python. Confirm with
# `chronyc sources -v` showing GPS as a reachable source, and
# `chronyc sourcestats` showing a sane, bounded offset, before relying
# on this operationally.
_SOCK_MAGIC = 0x534f434b


def _connect_chrony_sock(sock_path):
    """Connects to chrony's SOCK refclock socket, or returns None on
    any failure - no chrony configured for this yet, wrong
    permissions, wrong path, whatever the reason. Never raises: a
    time-sync problem must never be allowed to stop Lynx's own locator
    tracking, which has nothing to do with this."""
    import socket as _socket
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        s.connect(sock_path)
        return s
    except OSError as e:
        print(f"[gnss] chrony time sync unavailable ({sock_path}): {e}")
        return None


def _send_chrony_sample(sock, utc_time):
    """Sends one time sample to chrony's SOCK refclock protocol. Not
    a PPS-grade signal - there's inherent latency between the GPS fix
    and this call (serial transmission, parsing) that isn't corrected
    for, treated as a small, roughly constant systematic error the
    same way any lightweight timing source has some inherent latency
    baked in. Still a meaningful improvement over an unsynced Pi with
    no RTC and no internet at a remote site, which is the actual
    problem this solves.

    Returns True if the sample was sent, False on any failure -
    callers use this only to know whether time-sync is genuinely
    active for status() purposes, never as a reason to stop trying."""
    now = time.time()
    tv_sec = int(now)
    tv_usec = int(round((now - tv_sec) * 1_000_000))
    offset = utc_time.timestamp() - now
    try:
        payload = struct.pack('qqdiiii', tv_sec, tv_usec, offset,
                               0, 0, 0, _SOCK_MAGIC)
        sock.send(payload)
        return True
    except OSError:
        return False


# ── serial reader ───────────────────────────────────────────────────

class GnssReader:
    """Background thread reading NMEA and maintaining a stable locator.

    Deliberately fails quietly. A receiver with no HAT fitted, or with
    the serial port disabled, must carry on exactly as before - the
    locator simply stays as configured. Nothing here is allowed to stop
    Lynx starting.

    Note the default port. /dev/serial0 is the conventional symlink,
    but on a Pi 500+ it was found pointing at ttyAMA10, the debug UART
    connector, rather than the GPIO header - so the header port is
    named explicitly and the symlink offered only as a fallback.
    """

    def __init__(self, port="/dev/ttyAMA0", baud=9600, stable_secs=30.0,
                 length=6, on_change=None, constellations=None,
                 chrony_sock_path=None):
        self.port = port
        self.baud = baud
        self.tracker = LocatorTracker(stable_secs=stable_secs, length=length)
        self.on_change = on_change
        # None leaves the module as it is. Setting it sends $PCAS04 once
        # on connect; the module keeps the setting in its own memory.
        self.constellations = constellations
        # None disables GPS time sync entirely - fails quietly, same
        # philosophy as everything else here. When set, RMC's own
        # UTC date+time is fed to chrony's SOCK refclock (see
        # _send_chrony_sample) once self.tracker.locator is confirmed -
        # deliberately the SAME 30s-stability gate the locator itself
        # requires, not a separate timer, so "GPS is trusted" means one
        # consistent thing everywhere in this module.
        self.chrony_sock_path = chrony_sock_path
        self._chrony_sock = None
        self.time_synced = False   # true once at least one sample has
                                    # actually been sent - for status()
        self._time_quality_ok = False  # refreshed on every GGA line -
                                        # see _run()'s own comment
        self.running = False
        self.connected = False
        self.last_error = None
        self.last_sentence_at = None
        self._thread = None

    # -- status for the web UI --------------------------------------
    def status(self):
        fix = self.tracker.last_fix or {}
        display = None
        if self.tracker.locator and fix.get("lat") is not None:
            # A longer locator for the indication only. The value Lynx
            # actually uses stays at 6 characters, since that is what
            # logbooks and Pathfinder expect - this is extra precision
            # for the person looking at the screen.
            try:
                display = to_locator(fix["lat"], fix["lon"], 8)
            except ValueError:
                display = self.tracker.locator
        return {
            "connected": self.connected,
            "locator": self.tracker.locator,
            "locator_display": display,
            "satellites": fix.get("satellites"),
            "hdop": fix.get("hdop"),
            "altitude_m": fix.get("altitude_m"),
            "pending_secs": self.tracker.seconds_pending(),
            "last_error": self.last_error,
            "time_synced": self.time_synced,
            "time_quality_ok": self._time_quality_ok,
            "age_secs": (time.time() - self.last_sentence_at)
                        if self.last_sentence_at else None,
        }

    def start(self):
        import threading
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="gnss")
        self._thread.start()

    def stop(self):
        self.running = False
        if self._chrony_sock is not None:
            try:
                self._chrony_sock.close()
            except Exception:
                pass
            self._chrony_sock = None
        self.time_synced = False
        self._time_quality_ok = False

    def _run(self):
        try:
            import serial
        except ImportError:
            self.last_error = ("pyserial not installed - "
                               "pip install pyserial")
            print(f"[gnss] {self.last_error}")
            self.running = False
            return

        while self.running:
            ser = None
            try:
                ser = serial.Serial(self.port, self.baud, timeout=2)
                self.connected = True
                self.last_error = None
                print(f"[gnss] reading {self.port} at {self.baud}")

                if self.chrony_sock_path:
                    self._chrony_sock = _connect_chrony_sock(self.chrony_sock_path)

                if self.constellations:
                    try:
                        cmd = constellation_command(self.constellations)
                        ser.write(cmd.encode("ascii"))
                        print(f"[gnss] constellations set to "
                              f"{CONSTELLATIONS[self.constellations]}")
                    except Exception as e:
                        print(f"[gnss] could not set constellations: {e}")

                while self.running:
                    raw = ser.readline()
                    if not raw:
                        continue
                    try:
                        line = raw.decode("ascii", errors="ignore").strip()
                    except Exception:
                        continue
                    if not line.startswith("$"):
                        continue
                    self.last_sentence_at = time.time()

                    fix = parse_gga(line)
                    if _GGA.match(line):
                        # A genuine GGA sentence - whether or not
                        # parse_gga() accepted it, this line says
                        # something current about fix quality (or its
                        # absence), so this is exactly when to refresh
                        # _time_quality_ok. A no-fix or malformed GGA
                        # both correctly clear it - time shouldn't be
                        # trusted off a quality reading that's bad or
                        # absent right now, whatever it looked like a
                        # moment ago. Matters most exactly when signal
                        # is marginal (indoors, under a heavy canopy) -
                        # the situation this whole feature exists for.
                        if fix is not None:
                            self._time_quality_ok = _fix_meets_quality(
                                fix, self.tracker.min_satellites, self.tracker.max_hdop)
                        else:
                            self._time_quality_ok = False

                    # Time sync: gated on quality alone (see
                    # _time_quality_ok above), deliberately decoupled
                    # from the locator's own 30s stability window - GPS
                    # time is trustworthy as soon as fix quality is
                    # good, unlike position it doesn't need to wait for
                    # the same square to hold for 30s. RMC carries a
                    # full UTC date+time in the same sentence as its
                    # own validity flag, so no correlation against a
                    # separate GGA sentence is needed for the time
                    # value itself - only the QUALITY info (satellites,
                    # HDOP) has to come from GGA, since RMC doesn't
                    # carry either.
                    if self._chrony_sock is not None and self._time_quality_ok:
                        rmc = parse_rmc(line)
                        if rmc and rmc.get("utc"):
                            if _send_chrony_sample(self._chrony_sock, rmc["utc"]):
                                self.time_synced = True

                    if fix is None:
                        continue
                    before = self.tracker.locator
                    after = self.tracker.update(fix)
                    if after and after != before:
                        print(f"[gnss] locator {after} "
                              f"({fix['satellites']} sats, HDOP {fix['hdop']})")
                        if self.on_change:
                            try:
                                self.on_change(after)
                            except Exception as e:
                                print(f"[gnss] on_change failed: {e}")

            except Exception as e:
                self.connected = False
                if self._chrony_sock is not None:
                    try:
                        self._chrony_sock.close()
                    except Exception:
                        pass
                    self._chrony_sock = None
                self.time_synced = False
                self._time_quality_ok = False
                msg = f"{type(e).__name__}: {e}"
                if msg != self.last_error:
                    print(f"[gnss] {msg}")
                self.last_error = msg
                # Retry rather than give up: the HAT may be fitted later,
                # or the port may appear after a reboot.
                for _ in range(10):
                    if not self.running:
                        break
                    time.sleep(1)
            finally:
                if ser is not None:
                    try:
                        ser.close()
                    except Exception:
                        pass
        self.connected = False


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Watch the GNSS receiver.")
    ap.add_argument("--port", default="/dev/ttyAMA0")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--stable", type=float, default=30.0)
    a = ap.parse_args()

    r = GnssReader(port=a.port, baud=a.baud, stable_secs=a.stable)
    r.start()
    print("watching - Ctrl+C to stop\n")
    try:
        while True:
            s = r.status()
            pend = s["pending_secs"]
            print(f"  locator {str(s['locator'] or '-'):<8} "
                  f"sats {str(s['satellites'] or '-'):>3}  "
                  f"HDOP {str(s['hdop'] or '-'):>5}  "
                  f"alt {str(s['altitude_m'] or '-'):>6}  "
                  + (f"settling {pend:.0f}s" if pend else ""))
            time.sleep(2)
    except KeyboardInterrupt:
        r.stop()
        print("\nstopped")
