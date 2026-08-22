#!/usr/bin/env python3
"""Tests for lynx_gnss.py, run on any machine - no hardware needed.

The sentences below are real output captured from the L76K on the Pi,
including the no-fix state it sat in indoors behind Low-E glazing and
the good fix it reached outside. Testing against real output rather
than invented sentences is the point: it catches things like the "GN"
talker ID that a made-up example would not.

    python3 test_lynx_gnss.py
"""

import sys
import lynx_gnss as g

fails = 0


def check(what, ok, detail=""):
    global fails
    print(f"  {what:<56} {'ok' if ok else 'FAIL'}   {detail}")
    if not ok:
        fails += 1


# ── real captured sentences ────────────────────────────────────────

# outside, 10 satellites, HDOP 1.6
FIX_GGA = "$GNGGA,114633.000,5123.20177,N,00004.79306,E,1,10,1.6,84.8,M,45.5,M,,*7F"
FIX_RMC = "$GNRMC,114633.000,A,5123.20177,N,00004.79306,E,0.00,128.94,210826,,,A,V*08"

# indoors, no fix at all
NOFIX_GGA = "$GNGGA,,,,,,0,00,25.5,,,,,,*64"
NOFIX_RMC = "$GNRMC,,V,,,,,,,,,,N,V*37"

# outside but not yet solved - satellites seen, still no position
ACQ_GGA = "$GNGGA,114309.000,,,,,0,00,25.5,,,,,,*74"
ACQ_RMC = "$GNRMC,114309.000,V,,,,,,,210826,,,N,V*28"


print("\nchecksum verification")
check("valid sentence accepted", g.nmea_checksum_ok(FIX_GGA))
check("valid RMC accepted", g.nmea_checksum_ok(FIX_RMC))
check("no-fix sentence still checksums", g.nmea_checksum_ok(NOFIX_GGA))
bad = FIX_GGA.replace("5123.20177", "5123.20178")
check("corrupted latitude rejected", not g.nmea_checksum_ok(bad),
      "one digit changed")
check("truncated sentence rejected", not g.nmea_checksum_ok("$GNGGA,1146"))
check("non-NMEA rejected", not g.nmea_checksum_ok("hello"))


print("\nGGA parsing")
fix = g.parse_gga(FIX_GGA)
check("real fix parsed", fix is not None)
check("latitude", abs(fix["lat"] - 51.386696) < 1e-5, f'{fix["lat"]:.6f}')
check("longitude", abs(fix["lon"] - 0.079884) < 1e-5, f'{fix["lon"]:.6f}')
check("satellite count", fix["satellites"] == 10)
check("HDOP", fix["hdop"] == 1.6)
check("altitude", fix["altitude_m"] == 84.8, "m above MSL")
check("no-fix returns None, not 0,0", g.parse_gga(NOFIX_GGA) is None)
check("acquiring returns None", g.parse_gga(ACQ_GGA) is None)
check("RMC not parsed as GGA", g.parse_gga(FIX_RMC) is None)


print("\nRMC parsing")
rmc = g.parse_rmc(FIX_RMC)
check("valid RMC parsed", rmc is not None)
check("agrees with GGA position",
      abs(rmc["lat"] - fix["lat"]) < 1e-9 and abs(rmc["lon"] - fix["lon"]) < 1e-9)
check("void RMC returns None", g.parse_rmc(NOFIX_RMC) is None)
check("acquiring RMC returns None", g.parse_rmc(ACQ_RMC) is None)


print("\nlocator conversion")
check("Petts Wood fix -> JO01aj",
      g.to_locator(51.386696, 0.079884) == "JO01aj",
      g.to_locator(51.386696, 0.079884))
check("4-character form", g.to_locator(51.386696, 0.079884, 4) == "JO01")
check("GB3OO 4-char", g.to_locator(51.11463, 1.13914, 4) == "JO01")


def locator_box(loc):
    """Bounding box of a locator - the inverse conversion, written
    independently so a round-trip test actually proves something rather
    than just re-running the same arithmetic."""
    loc = loc.strip()
    lon = (ord(loc[0].upper()) - 65) * 20.0 - 180.0
    lat = (ord(loc[1].upper()) - 65) * 10.0 - 90.0
    w, h = 20.0, 10.0
    if len(loc) >= 4:
        lon += int(loc[2]) * 2.0
        lat += int(loc[3]) * 1.0
        w, h = 2.0, 1.0
    if len(loc) >= 6:
        lon += (ord(loc[4].lower()) - 97) * (2.0 / 24)
        lat += (ord(loc[5].lower()) - 97) * (1.0 / 24)
        w, h = 2.0 / 24, 1.0 / 24
    return lon, lat, w, h


print("  round-trip: every point must fall inside its own square")
points = [
    (51.386696, 0.079884, "the captured fix"),
    (51.11463, 1.13914, "GB3OO Paddlesworth"),
    (48.8584, 2.2945, "Paris"),
    (-33.8568, 151.2153, "Sydney"),
    (0.0, 0.0, "equator/meridian"),
    (-45.0, -120.0, "southern/western"),
    (89.9, 179.9, "near the corner"),
    (-89.9, -179.9, "opposite corner"),
]
bad = []
for lat, lon, note in points:
    loc = g.to_locator(lat, lon)
    x, y, w, h = locator_box(loc)
    inside = (x <= lon <= x + w) and (y <= lat <= y + h)
    if not inside:
        bad.append((note, loc))
    check(f"    {note} -> {loc}", inside)
check("all points inside their own square", not bad, str(bad) if bad else "")

print("  extended square: 8 characters must differ from 6")
loc6 = g.to_locator(51.386717, 0.079740, 6)
loc8 = g.to_locator(51.386717, 0.079740, 8)
check("    8-char is longer than 6-char", len(loc8) == 8 and len(loc6) == 6,
      f"{loc6} / {loc8}")
check("    8-char extends rather than replaces", loc8.startswith(loc6), loc8)
for lat, lon, note in points:
    l6, l8 = g.to_locator(lat, lon, 6), g.to_locator(lat, lon, 8)
    x, y, w, h = locator_box(l8)
    check(f"    {note} -> {l8}",
          len(l8) == 8 and l8.startswith(l6)
          and x <= lon <= x + w and y <= lat <= y + h)


print("\nPCAS command construction")
# checksums verified against the examples published by Waveshare
check("PCAS04,3 matches the documented example",
      g.constellation_command(3) == "$PCAS04,3*1A\r\n",
      repr(g.constellation_command(3)))
check("PCAS01,1 (9600) matches",
      g.baud_command(9600) == "$PCAS01,1*1D\r\n")
check("PCAS01,5 (115200) matches",
      g.baud_command(115200) == "$PCAS01,5*19\r\n")
check("every constellation mode builds",
      all(g.nmea_checksum_ok(g.constellation_command(m).strip())
          for m in g.CONSTELLATIONS))
try:
    g.constellation_command(9)
    check("unknown mode rejected", False)
except ValueError:
    check("unknown mode rejected", True)
try:
    g.baud_command(12345)
    check("unsupported baud rejected", False)
except ValueError:
    check("unsupported baud rejected", True)
try:
    g.to_locator(91.0, 0.0)
    check("out-of-range latitude rejected", False)
except ValueError:
    check("out-of-range latitude rejected", True)


print("\nstability gate")


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


clk = Clock()
tr = g.LocatorTracker(stable_secs=30.0, clock=clk)

check("starts with nothing", tr.update(None) is None)
check("first good fix not adopted immediately", tr.update(fix) is None,
      "waiting for stability")
clk.advance(29.0)
check("still waiting at 29 s", tr.update(fix) is None)
clk.advance(2.0)
check("adopted at 31 s", tr.update(fix) == "JO01aj")

# a fix in a different square must serve its own 30 s
other = dict(fix, lat=52.0, lon=1.0)
check("new square does not take over at once",
      tr.update(other) == "JO01aj")
clk.advance(31.0)
check("new square adopted after 30 s",
      tr.update(other) == g.to_locator(52.0, 1.0))

# losing the fix must not discard a known-good locator
before = tr.locator
check("losing the fix keeps the last locator",
      tr.update(None) == before, before)

print("\nquality gates")
clk2 = Clock()
tr2 = g.LocatorTracker(stable_secs=1.0, min_satellites=4, max_hdop=5.0, clock=clk2)
thin = dict(fix, satellites=3)
clk2.advance(5.0)
check("3 satellites refused", tr2.update(thin) is None)
sloppy = dict(fix, hdop=20.0)
check("HDOP 20 refused", tr2.update(sloppy) is None)
clk2.advance(5.0)
tr2.update(fix)                      # starts the stability window
clk2.advance(2.0)                    # stable_secs is 1.0 here
check("good fix accepted after its window", tr2.update(fix) == "JO01aj")


print("\nGPS time (RMC date/time, chrony sample encoding)")
# What's testable in pure Python: RMC's own date/time parsing, and the
# chrony SOCK sample struct's own encoding. What ISN'T testable here:
# whether chrony's own compiled C struct genuinely agrees byte-for-
# byte with what's packed below - that can only be confirmed on real
# hardware via `chronyc sources -v` actually showing GPS as reachable.
rmc_full = g.parse_rmc(FIX_RMC)
check("RMC carries a utc key", "utc" in rmc_full and rmc_full["utc"] is not None)
utc = rmc_full["utc"]
check("date from field 9 (210826 -> 2026-08-21)",
      (utc.year, utc.month, utc.day) == (2026, 8, 21), str(utc))
check("time from field 1 (114633.000 -> 11:46:33)",
      (utc.hour, utc.minute, utc.second) == (11, 46, 33), str(utc))
check("tzinfo is UTC", utc.utcoffset().total_seconds() == 0)

check("void RMC has no utc key to worry about", g.parse_rmc(NOFIX_RMC) is None)

check("malformed date returns None, not a wrong date",
      g._rmc_utc_datetime("114633.000", "999999") is None)
check("malformed time returns None, not a wrong time",
      g._rmc_utc_datetime("xxbadxx", "210826") is None)
check("short date field returns None",
      g._rmc_utc_datetime("114633.000", "2108") is None)

print("  chrony SOCK sample struct")
import struct as _struct


class _FakeSock:
    """Records what would have been sent, without needing a real
    chrony socket - proves the encoding is self-consistent, not that
    chrony agrees with it (see module comment)."""
    def __init__(self):
        self.sent = None
    def send(self, data):
        self.sent = data


fake = _FakeSock()
sample_time = utc  # the real, parsed UTC datetime from above
ok = g._send_chrony_sample(fake, sample_time)
check("send reports success", ok is True)
check("sample is exactly 40 bytes (qqdiiii, 64-bit layout)",
      len(fake.sent) == 40, f"{len(fake.sent)} bytes")
unpacked = _struct.unpack('qqdiiii', fake.sent)
tv_sec, tv_usec, offset, pulse, leap, pad, magic = unpacked
check("magic matches SOCK_MAGIC", magic == g._SOCK_MAGIC, hex(magic))
check("pulse is 0 (a real offset sample, not a bare PPS edge)", pulse == 0)
check("leap is 0 (no leap second announced)", leap == 0)
check("padding is 0", pad == 0)
check("tv_usec is a genuine microsecond fraction", 0 <= tv_usec < 1_000_000)
# offset = "true time" - "local time" at the sample instant. Local
# time here is genuinely `now`, so this only checks the encoding
# preserved a plausible, finite offset - not a specific value, since
# the real offset depends on the test machine's own clock.
check("offset is a finite float", offset == offset and abs(offset) < 1e12,
      f"{offset}")

fail_sock = _FakeSock()
def _raise(data): raise OSError("no chrony listening")
fail_sock.send = _raise
check("a closed/unavailable socket fails quietly, doesn't raise",
      g._send_chrony_sample(fail_sock, sample_time) is False)

print("  time-trust is decoupled from the locator's own stability window")
# The actual point of the redesign: a fix can be good enough to trust
# for TIME on the very first good reading, well before the locator
# itself has proven 30s of stability - matters most exactly when
# signal is marginal (indoors, under a heavy canopy), where position
# may take much longer to settle into one square than the fix quality
# itself needs to become trustworthy.
clk3 = Clock()
tr3 = g.LocatorTracker(stable_secs=30.0, clock=clk3)
check("fresh tracker: no locator yet", tr3.update(fix) is None)
check("...but that SAME fix already meets the quality bar for time",
      g._fix_meets_quality(fix, tr3.min_satellites, tr3.max_hdop) is True)
clk3.advance(5.0)
check("still no locator at 5s (well under the 30s stability window)",
      tr3.update(fix) is None)
check("quality bar is unaffected by elapsed time - it was never time-gated",
      g._fix_meets_quality(fix, tr3.min_satellites, tr3.max_hdop) is True)

thin_fix = dict(fix, satellites=3)
check("a genuinely poor fix correctly fails the same quality bar",
      g._fix_meets_quality(thin_fix, tr3.min_satellites, tr3.max_hdop) is False)
sloppy_fix = dict(fix, hdop=20.0)
check("...and so does one with poor HDOP",
      g._fix_meets_quality(sloppy_fix, tr3.min_satellites, tr3.max_hdop) is False)
check("HDOP of exactly the max is still acceptable (boundary is inclusive)",
      g._fix_meets_quality(dict(fix, hdop=5.0), tr3.min_satellites, tr3.max_hdop) is True)
check("a fix with hdop=None is not penalised for a field GGA sometimes omits",
      g._fix_meets_quality(dict(fix, hdop=None), tr3.min_satellites, tr3.max_hdop) is True)


print(f"\n  {'TESTS FAILED' if fails else 'all tests passed'} "
      f"({fails} failure{'' if fails == 1 else 's'})\n")
sys.exit(1 if fails else 0)
