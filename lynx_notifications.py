# Copyright (C) 2026 Justin, G8YTZ / EI3IOB
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
lynx_notifications.py — Repeater-activity notifications and control outputs.

Fires, on a confirmed lock/unlock transition of EITHER Picotuner (diversity-
aware, reusing the exact same "either tuner locked" expression already
proven in rf_mpv_lifecycle_monitor(), but evaluated independently and
continuously here — deliberately NOT gated on Lynx's own current playback
mode, since a repeater controller needs to know about incoming signals
regardless of whether the operator happens to be watching RF, a stream, or
nothing at all right now):

  - A QRZ.com logbook entry (via QRZ's XML/ADIF Logbook API)
  - A Slack notification (via an incoming webhook)
  - Two independent Bitfocus Companion HTTP triggers (lock, unlock)
  - A GPIO "Tx on/off" output, signal-driven with its own settling timers,
    optionally overridden by a configurable weekday/weekend schedule

Each output has its own independent settling timer(s) — the delay between a
confirmed lock/unlock and actually firing, matching the proven design in the
reference Ryde webhook/Companion code this was adapted from (tested and
running in production for over a year): long enough that a transmission's
callsign metadata has had a chance to decode, short enough not to miss
brief contacts or delay the notification unreasonably.

QRZ and Slack payload construction, and the ADIF field-length-prefixed
format specifically, are carried over unchanged from that reference
code — this is a well-tested, working format, not something to redesign.
"""

import json
import threading
import time
import datetime
import requests
import xml.etree.ElementTree as ET

# ── Physical (BOARD) pin -> BCM GPIO number, or a fixed power/ground label ──
# Standard 40-pin header layout, unchanged across every 40-pin Raspberry Pi
# model including the 5 (only the underlying chip serving these pins
# changed, not the physical header layout or numbering). Verified directly
# against pinout.xyz before use here, given how important getting this
# exactly right is - a wrong mapping could mean driving the wrong physical
# pin entirely.
PHYSICAL_PIN_MAP = {
    1: "3V3", 2: "5V",
    3: "GPIO2", 4: "5V",
    5: "GPIO3", 6: "GND",
    7: "GPIO4", 8: "GPIO14",
    9: "GND", 10: "GPIO15",
    11: "GPIO17", 12: "GPIO18",
    13: "GPIO27", 14: "GND",
    15: "GPIO22", 16: "GPIO23",
    17: "3V3", 18: "GPIO24",
    19: "GPIO10", 20: "GND",
    21: "GPIO9", 22: "GPIO25",
    23: "GPIO11", 24: "GPIO8",
    25: "GND", 26: "GPIO7",
    27: "GPIO0", 28: "GPIO1",   # reserved for HAT EEPROM ID - deliberately
                                 # excluded from USABLE_PHYSICAL_PINS below
    29: "GPIO5", 30: "GND",
    31: "GPIO6", 32: "GPIO12",
    33: "GPIO13", 34: "GND",
    35: "GPIO19", 36: "GPIO16",
    37: "GPIO26", 38: "GPIO20",
    39: "GND", 40: "GPIO21",
}

# Physical pins actually offered as GPIO output choices in the UI - power,
# ground, and the two HAT-EEPROM-reserved pins (27, 28) are excluded, since
# none of these are appropriate as a general-purpose output regardless of
# what's wired to them.
USABLE_PHYSICAL_PINS = [
    p for p, label in PHYSICAL_PIN_MAP.items()
    if label.startswith("GPIO") and p not in (27, 28)
]

def pin_label(physical_pin):
    """'Pin 11 (GPIO17)' - for displaying both numbering schemes together,
    exactly as requested, so there's never ambiguity about which physical
    pin a given config value actually refers to."""
    bcm = PHYSICAL_PIN_MAP.get(physical_pin, "?")
    return f"Pin {physical_pin} ({bcm})"


class SettlingAction:
    """Fires `callback` after `delay_secs` of continuous, unbroken trigger
    condition, cleanly cancelling if the condition reverts before the delay
    elapses. threading.Timer-based - the same proven pattern as the
    reference code's own settling-timer logic (there implemented by hand
    with a polled timestamp comparison; here as a small, reusable,
    thread-safe helper so the same correct behaviour doesn't need
    re-implementing six separate times for six separate outputs).

    A delay of 0 fires (almost) immediately, asynchronously. This is a
    sensible general-purpose default for "no settling wanted" - NOT the
    same thing as the Tx pin's own, specific "0 = never auto power-down"
    sentinel, which is a different concept entirely and is handled at the
    call site, before ever constructing or triggering a SettlingAction for
    that specific case."""

    def __init__(self, delay_secs, callback, name=""):
        self.delay_secs = delay_secs
        self.callback = callback
        self.name = name
        self._timer = None
        self._lock = threading.Lock()

    def trigger(self):
        with self._lock:
            self._cancel_locked()
            if self.delay_secs <= 0:
                threading.Thread(target=self._fire, daemon=True).start()
            else:
                self._timer = threading.Timer(self.delay_secs, self._fire)
                self._timer.daemon = True
                self._timer.start()

    def cancel(self):
        with self._lock:
            self._cancel_locked()

    @property
    def pending(self):
        """True if a timer is currently armed and waiting to fire."""
        with self._lock:
            return self._timer is not None

    def _cancel_locked(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _fire(self):
        try:
            self.callback()
        except Exception as e:
            print(f"[notifications] {self.name} action failed: {type(e).__name__}: {e}")


class GpioPin:
    """Thin wrapper around a single gpiozero output, using physical (BOARD)
    pin numbering directly - gpiozero natively supports this via a
    "BOARDxx" pin specifier, so no manual physical-to-BCM conversion is
    needed for the actual hardware call itself (PHYSICAL_PIN_MAP above is
    purely for UI display, matching what was asked for).

    Deliberately fails soft: if gpiozero isn't installed, or GPIO hardware
    access fails for any reason (wrong permissions, unavailable on this
    specific board, etc.), this logs clearly and disables itself rather
    than crashing the whole Lynx process - GPIO output is opt-in,
    supplementary functionality, and a hardware/permissions problem with it
    should never be able to take down reception."""

    def __init__(self, physical_pin, active_high, name=""):
        self.name = name
        self.physical_pin = physical_pin
        self.active_high = active_high
        self._device = None
        try:
            from gpiozero import OutputDevice
            # active_high here controls gpiozero's own idea of what
            # .on()/.off() mean electrically - NOT yet whether the pin
            # starts on or off, which is set explicitly below regardless.
            self._device = OutputDevice(f"BOARD{physical_pin}",
                                         active_high=active_high,
                                         initial_value=False)
            print(f"[notifications] GPIO {name}: {pin_label(physical_pin)} ready "
                  f"(active {'high' if active_high else 'low'})")
        except Exception as e:
            print(f"[notifications] GPIO {name}: could not initialise "
                  f"{pin_label(physical_pin)} - {type(e).__name__}: {e}. "
                  f"This output is disabled.")

    @property
    def available(self):
        return self._device is not None

    def set(self, on: bool):
        if self._device is None:
            return
        try:
            if on:
                self._device.on()
            else:
                self._device.off()
        except Exception as e:
            print(f"[notifications] GPIO {self.name}: failed to set state - "
                  f"{type(e).__name__}: {e}")


# ── QRZ.com Logbook ─────────────────────────────────────────────────────
# The core ADIF construction below - the <field:LENGTH>VALUE format and
# the original field set (band/mode/call/qso_date/time_on/station_callsign/
# freq/comment/rst_sent) - is carried over unchanged from the reference
# code (confirmed tested, working in production for over a year), and
# deliberately preserved exactly as proven, not redesigned. gridsquare is
# a genuinely new, additive field (portable-locator override, see
# submit_qrz_logbook) - it's optional and only appears in the payload
# when actually set, so it can't affect the proven, original behaviour.

QRZ_LOGBOOK_URL = "https://logbook.qrz.com/api"

def _build_qrz_adif(api_key, action, call, band, mode, qso_date, time_on,
                     station_callsign, freq, comment, rst_sent, gridsquare=""):
    ps = ""
    ps += "KEY=" + api_key + "&"
    ps += "ACTION=" + action + "&"
    ps += "ADIF="
    ps += "<band:" + str(len(band)) + ">" + str(band)
    ps += "<mode:" + str(len(mode)) + ">" + str(mode)
    ps += "<call:" + str(len(call)) + ">" + str(call)
    ps += "<qso_date:" + str(len(qso_date)) + ">" + str(qso_date)
    ps += "<time_on:" + str(len(time_on)) + ">" + str(time_on)
    ps += "<station_callsign:" + str(len(station_callsign)) + ">" + str(station_callsign)
    ps += "<freq:" + str(len(str(freq))) + ">" + str(freq)
    ps += "<comment:" + str(len(str(comment))) + ">" + str(comment)
    ps += "<rst_sent:" + str(len(str(rst_sent))) + ">" + str(rst_sent)
    if gridsquare:
        # Portable locator override (see submit_qrz_logbook) - only added
        # when actually set, so normal operation produces byte-identical
        # ADIF to before this field existed.
        ps += "<gridsquare:" + str(len(gridsquare)) + ">" + str(gridsquare)
    ps += "<eor>"
    return ps

def _qrz_band_from_freq_mhz(freq_mhz):
    """Reference code's original three bands, plus 3cm - added after
    direct testing of the LNB downlink-frequency reversal (see
    _current_source_data) showed a genuine QO-100 downlink (~10489MHz)
    had nowhere to land. Range confirmed against ADIF's own Band
    Enumeration (10000-10500MHz). Extend further if a deployment ever
    needs a band beyond these four."""
    if 430 <= freq_mhz <= 440:
        return "70cm"
    elif 1240 <= freq_mhz <= 1325:
        return "23cm"
    elif 3400 <= freq_mhz <= 3410:
        return "9cm"
    elif 10000 <= freq_mhz <= 10500:
        return "3cm"
    return ""

# ── QRZ XML Data API - callsign lookup (separate from the Logbook API
# above, which uses its own api_key) ──────────────────────────────
_qrz_lookup_session_key = None
_qrz_lookup_session_obtained_at = 0.0
_qrz_lookup_cache = {}   # {callsign: (fname_or_None, cached_at)}
QRZ_LOOKUP_URL = "https://xmldata.qrz.com/xml/current/"
QRZ_LOOKUP_CACHE_TTL_SECS = 86400       # 24h - a callsign's registered name
                                          # essentially never changes within a
                                          # session, so this avoids repeated
                                          # lookups for a station seen more
                                          # than once
QRZ_LOOKUP_SESSION_MAX_AGE_SECS = 3000   # ~50 min - proactively refreshed a
                                          # little before QRZ's own ~1hr
                                          # session expiry, rather than
                                          # waiting to be told it's stale
QRZ_LOOKUP_TIMEOUT_SECS = 10.0           # was 4.0 - increased per Justin's own
                                          # report of misses on a poorer
                                          # connection (cellular). Safe to be
                                          # more generous here now: this runs
                                          # entirely in its own background
                                          # thread (see
                                          # _kick_off_qrz_notification_lookup
                                          # in lynx_app.py), so a slow lookup
                                          # can no longer block the arbitrator
                                          # or anything else, regardless of
                                          # how long it takes - the only
                                          # remaining cost of a slower timeout
                                          # is the name arriving later (or not
                                          # at all, if it exceeds the
                                          # notification's own
                                          # notification_duration_secs, 20s by
                                          # default, before completing).

def _qrz_xml_request(params):
    """Fire one request against QRZ's XML Data API and return the
    parsed root element, or None on any failure (network, timeout,
    malformed response) - deliberately never raises, since every
    caller in this section needs to degrade gracefully rather than
    ever break the notification a lookup is meant to enhance."""
    try:
        r = requests.get(QRZ_LOOKUP_URL, params=params, timeout=QRZ_LOOKUP_TIMEOUT_SECS)
        r.raise_for_status()
        return ET.fromstring(r.content)
    except Exception as e:
        print(f"[qrz_lookup] request failed: {e}")
        return None


def _qrz_xml_ns(root):
    """QRZ's XML responses are namespaced (xmlns=\"http://xmldata.qrz.com\")
    - ElementTree requires that namespace on every tag name passed to
    find(), so this extracts it once per response rather than
    hardcoding it, in case QRZ ever changes it."""
    if root.tag.startswith('{'):
        return root.tag.split('}')[0] + '}'
    return ''


def _qrz_lookup_login(username, password):
    """Logs in to the XML Data API and stores the session key for
    reuse across multiple lookups - each individual lookup does NOT
    need its own fresh login. Returns True/False rather than raising."""
    global _qrz_lookup_session_key, _qrz_lookup_session_obtained_at
    root = _qrz_xml_request({"username": username, "password": password, "agent": "LynxDATV1.0"})
    if root is None:
        return False
    ns = _qrz_xml_ns(root)
    session = root.find(f'{ns}Session')
    if session is None:
        print("[qrz_lookup] login response had no Session element")
        return False
    err = session.find(f'{ns}Error')
    if err is not None:
        print(f"[qrz_lookup] login error: {err.text}")
        return False
    key_el = session.find(f'{ns}Key')
    if key_el is None or not key_el.text:
        print("[qrz_lookup] login response had no session key")
        return False
    _qrz_lookup_session_key = key_el.text
    _qrz_lookup_session_obtained_at = time.time()
    return True


def qrz_callsign_lookup(username, password, callsign):
    """Looks up a callsign via QRZ's XML Data API and returns just the
    first/given name, or None if not found, not configured, or on any
    error - a lookup failure must never be allowed to break the
    notification it's meant to enhance, so this always degrades
    gracefully rather than raising. See the module-level comments
    above for the caching/session-reuse rationale."""
    global _qrz_lookup_session_key

    callsign = (callsign or "").strip().upper()
    if not callsign or not username or not password:
        return None

    cached = _qrz_lookup_cache.get(callsign)
    if cached and (time.time() - cached[1]) < QRZ_LOOKUP_CACHE_TTL_SECS:
        return cached[0]

    if (_qrz_lookup_session_key is None or
            (time.time() - _qrz_lookup_session_obtained_at) > QRZ_LOOKUP_SESSION_MAX_AGE_SECS):
        if not _qrz_lookup_login(username, password):
            return None

    root = _qrz_xml_request({"s": _qrz_lookup_session_key, "callsign": callsign})
    if root is None:
        return None
    ns = _qrz_xml_ns(root)

    session = root.find(f'{ns}Session')
    if session is not None and session.find(f'{ns}Error') is not None:
        # Session key rejected/expired mid-use - log in once more and
        # retry this one lookup, rather than failing outright.
        if not _qrz_lookup_login(username, password):
            return None
        root = _qrz_xml_request({"s": _qrz_lookup_session_key, "callsign": callsign})
        if root is None:
            return None
        ns = _qrz_xml_ns(root)

    callsign_el = root.find(f'{ns}Callsign')
    fname = None
    if callsign_el is not None:
        fname_el = callsign_el.find(f'{ns}fname')
        if fname_el is not None and fname_el.text:
            fname = fname_el.text.strip()

    _qrz_lookup_cache[callsign] = (fname, time.time())
    return fname


def submit_qrz_logbook(api_key, station_callsign, rx_callsign, freq_khz,
                        mode, mer, margin, portable_locator="", comment_override=None):
    """Builds and submits one QRZ logbook entry. freq_khz matches Lynx's
    own convention throughout (presets, tuning) - converted to MHz here,
    which is what QRZ's freq field expects (confirmed against the
    reference code's own comment: "FREQ: frequency in MHz").

    Lynx has no direct equivalent of the reference code's getPowerInd()
    (a Ryde-specific "power indication" reading) - margin (signal margin,
    dB) is used in its place for the comment/rst_sent fields as the
    closest available, meaningful signal-quality figure Lynx actually has.

    portable_locator: an operator-supplied override for the contacted
    station's grid square, for the case where they're operating portable
    and haven't updated their QRZ profile - without this, QRZ calculates
    distance/bearing from the contacted callsign's stale, registered
    locator rather than where they actually are. Empty by default, in
    which case no gridsquare is sent at all and QRZ's own lookup behaves
    exactly as it always has.

    Deliberately just a plain string, sourced from wherever the caller
    gets it - today that's a manually-entered config value (see
    NotificationManager._fire_qrz), but nothing here assumes a human
    typed it. A future, automated source - an onboard GPS module, or a
    phone's GPS relayed over Bluetooth, with the lat/long converted to a
    Maidenhead locator - could populate the same underlying config value
    on its own, and this function would need no changes at all to use it.

    comment_override: replaces the normal, auto-built comment entirely
    when set - used by the /diagnostics test feature to mark its entries
    clearly as test data directly in the logbook itself, not just via the
    TESTQRZ callsign. None by default, so real logging is unaffected.
    """
    freq_mhz = freq_khz / 1000.0
    band = _qrz_band_from_freq_mhz(freq_mhz)

    call_trunc = ""
    for ch in rx_callsign:
        if ch.isalnum():
            call_trunc += ch
        else:
            break

    now = datetime.datetime.now(datetime.timezone.utc)
    qso_date = now.strftime("%Y%m%d")
    time_on = now.strftime("%H%M%S")

    comment = comment_override if comment_override is not None else f"{mode} | {mer}dB MER"
    rst_sent = f"{margin}dB"

    payload = _build_qrz_adif(api_key, "INSERT", call_trunc, band, str(mode),
                               qso_date, time_on, station_callsign,
                               str(freq_mhz), comment, str(rst_sent),
                               gridsquare=portable_locator.strip())

    r = requests.post(QRZ_LOGBOOK_URL, data=payload,
                       headers={"User-agent": "lynx-notifications/1.0"}, timeout=10)
    result, logid, count, reason = "none", "none", "none", "none"
    if r.status_code == 200:
        for pair in r.text.split("&"):
            kv = pair.split("=")
            if len(kv) == 2:
                if kv[0] == "RESULT":
                    result = kv[1]
                elif kv[0] == "LOGID":
                    logid = kv[1]
                elif kv[0] == "COUNT":
                    count = kv[1]
                elif kv[0] == "REASON":
                    reason = kv[1]
    else:
        print(f"[notifications] QRZ: non-200 response ({r.status_code}): {r.text}")
    if result not in ("OK", "REPLACE"):
        print(f"[notifications] QRZ: API reported an error - result={result} reason={reason}")
    else:
        print(f"[notifications] QRZ: logged {call_trunc} - result={result} logid={logid}")
    return {
        "result": result, "logid": logid, "count": count, "reason": reason,
        "http_status": r.status_code, "raw_response": r.text,
        "mode_sent": str(mode), "band_sent": band,
    }


# ── Slack ────────────────────────────────────────────────────────────────

def send_slack_message(webhook_url, template, placeholders):
    """placeholders: dict of {name: value} substituted into template via
    str.format(). Unknown/missing placeholders raise KeyError rather than
    silently posting a broken message - caught and logged by the caller."""
    text = template.format(**placeholders)
    payload = json.dumps({"text": text})
    r = requests.post(webhook_url, data=payload,
                       headers={"Content-type": "application/json"}, timeout=10)
    if r.status_code != 200:
        print(f"[notifications] Slack: non-200 response ({r.status_code}): {r.text}")
    else:
        print(f"[notifications] Slack: sent")


# ── Bitfocus Companion ───────────────────────────────────────────────────

def trigger_companion(url):
    """The reference code's Companion trigger had no error handling at
    all, unlike its own Slack/QRZ code - a genuine gap, fixed here to
    match the same try/except discipline used everywhere else in this
    module, since an unreachable Companion instance should never be able
    to raise uncaught inside a timer callback."""
    r = requests.post(url, timeout=10)
    if r.status_code not in (200, 204):
        print(f"[notifications] Companion: non-200 response ({r.status_code}) from {url}")
    else:
        print(f"[notifications] Companion: triggered {url}")



# ── Schedule window evaluation ──────────────────────────────────────────

def _time_in_window(now_time, start_str, end_str):
    """now_time: datetime.time. start_str/end_str: "HH:MM" strings, or
    falsy (empty string / None) for "no schedule" (always False in that
    case - this is the "No schedule" dropdown option). Handles a window
    that crosses midnight correctly (e.g. 22:00-02:00)."""
    if not start_str or not end_str:
        return False
    start = datetime.datetime.strptime(start_str, "%H:%M").time()
    end = datetime.datetime.strptime(end_str, "%H:%M").time()
    if start <= end:
        return start <= now_time < end
    else:
        return now_time >= start or now_time < end

def is_in_schedule_window(now, weekday_start, weekday_end, weekend_start, weekend_end):
    """now: datetime.datetime. Saturday/Sunday use the weekend window,
    Monday-Friday use the weekday window - each independently configurable,
    each independently able to be "No schedule" (disabled)."""
    is_weekend = now.weekday() >= 5  # Python: Monday=0 ... Sunday=6
    if is_weekend:
        return _time_in_window(now.time(), weekend_start, weekend_end)
    return _time_in_window(now.time(), weekday_start, weekday_end)


# ── Main manager ─────────────────────────────────────────────────────────

class NotificationManager:
    """Owns the independent lock-monitoring loop and drives all five
    outputs (QRZ, Slack, Companion lock/unlock, GPIO Tx) from it.

    Deliberately does NOT gate on Lynx's own current_mode the way
    rf_mpv_lifecycle_monitor() does - a repeater controller needs to know
    about incoming signals regardless of what's currently on screen.
    Reuses that same monitor's exact "either tuner locked" expression and
    LOCK_CONFIRM_POLLS debounce value, just evaluated independently and
    continuously here.

    `get_config` is a callable (not a captured dict reference) specifically
    because lynx_app.py's own `config` global gets REASSIGNED (not just
    mutated) on every config save - holding a direct reference captured at
    startup would silently go stale after the first save. picotuner_state /
    picotuner_state_b are passed as direct dict references instead, since
    those are only ever mutated in place, never reassigned, so a captured
    reference to them stays valid and current indefinitely."""

    LOCK_CONFIRM_POLLS = 3   # matches rf_mpv_lifecycle_monitor()'s own, proven value
    POLL_SECS = 1.0

    def __init__(self, picotuner_state, picotuner_state_b, get_config, record_event=None,
                 get_lnb_state=None, get_tri_watch_displayed_rcv=None, get_tri_watch_enabled=None):
        self.picotuner_state = picotuner_state
        self.picotuner_state_b = picotuner_state_b
        self.get_config = get_config
        # Lets this module log onto the same, persistent Diagnostics page
        # timeline lynx_app.py already uses for mpv events, rather than
        # only to the terminal - defaults to a harmless no-op so this
        # class stays constructible/testable without lynx_app.py present.
        self.record_event = record_event or (lambda category, detail="", count_as_mpv_restart=False: None)
        # (lnb_lo_khz, lnb_side) for the current tune, or (0, "low") if no
        # LNB is in use - a callback (like get_config) since these are
        # runtime state that changes on every tune, not a one-time value.
        # Defaults to "no LNB" so this class stays constructible/testable
        # standalone.
        self.get_lnb_state = get_lnb_state or (lambda: (0, "low"))
        # 1, 2, or None - which receiver tri_watch is currently displaying,
        # if any. Used by _current_source_data() to log whichever source
        # is actually on screen under tri_watch, rather than the MER
        # comparison below (which only means something in diversity mode,
        # where both tuners chase the SAME signal - under tri_watch they're
        # on independent frequencies, so comparing their MER to decide
        # which to log doesn't mean anything and can silently pick the
        # wrong one). Defaults to always-None so this class stays
        # constructible/testable standalone, and diversity/normal-mode
        # behaviour is completely unaffected when this isn't supplied.
        self.get_tri_watch_displayed_rcv = get_tri_watch_displayed_rcv or (lambda: None)
        # Whether tri_watch is enabled at all, independent of what it's
        # currently displaying - needed because get_tri_watch_displayed_rcv()
        # alone returns None in TWO genuinely different situations that must
        # be told apart: tri_watch being completely off (where the MER
        # comparison above is correct and intended), and tri_watch being on
        # but currently displaying its stream source or sitting idle (where
        # NEITHER receiver is the active, displayed source, even if one
        # happens to be locked in the background - tri_watch keeps both
        # continuously tuned regardless of what's shown). Confirmed as a
        # real, reported bug: every logged QRZ contact showed the site's
        # own callsign whenever the stream was active under tri_watch,
        # because a background lock alone was enough to both arm the
        # lock-settle timer in _poll() and, once fired, get silently
        # picked up by the MER-comparison fallback here - despite neither
        # receiver actually being what was on screen. Defaults to
        # always-False so this class stays constructible/testable
        # standalone, and diversity/normal-mode behaviour (which never
        # reaches the tri_watch-specific branches at all) is unaffected.
        self.get_tri_watch_enabled = get_tri_watch_enabled or (lambda: False)

        self._stop_event = threading.Event()
        self._thread = None

        self._lock_streak = 0
        self._loss_streak = 0
        self._confirmed_locked = False

        # Per-receiver lock tracking for QRZ/Slack specifically, used
        # only when tri_watch is enabled - see _poll_tri_watch_qrz_slack()
        # for the full rationale. Kept entirely separate from
        # self._confirmed_locked above (which still drives Companion/
        # GPIO Tx, and QRZ/Slack too outside tri_watch) rather than
        # replacing it, since those still want a single, combined "is
        # anything locked" signal - only QRZ/Slack logging needs to know
        # about each receiver independently, so every genuine contact
        # gets logged with its own correct data regardless of which (if
        # either) receiver is currently displayed.
        self._tw_lock_streak = {1: 0, 2: 0}
        self._tw_loss_streak = {1: 0, 2: 0}
        self._tw_confirmed_locked = {1: False, 2: False}

        self._qrz_last_logged = {}   # {callsign: unix_timestamp} - suppression window

        self._actions = {}   # {name: SettlingAction} - persistent, keyed store so a
                              # later event (e.g. unlock) can find and cancel an earlier
                              # one's (e.g. lock's) still-pending timer. Confirmed as a
                              # genuine, real gap when this was a bare local variable per
                              # call: a lock that starts a 15s QRZ timer, followed by a
                              # genuine unlock 5s later, had no way to cancel that timer -
                              # QRZ would still fire after the contact had already ended,
                              # contradicting the reference code's own explicit design
                              # ("a webhook will not be sent if the state goes to UNLOCK
                              # during the settling time").

        self._gpio_companion = None          # GpioPin mirroring Companion lock/unlock,
        self._companion_gpio_cfg_key = None  # for relay-based input switching

        self._gpio_tx = None          # GpioPin, (re)built if pin/polarity config changes
        self._tx_pin_cfg_key = None   # (pin, polarity) the current GpioPin was built for
        self._tx_power_up_action = None
        self._tx_power_down_action = None
        self._tx_was_in_window = False
        self._tx_was_locked = False   # Tx pin's own view of lock state, tracked
                                        # separately from self._confirmed_locked so the
                                        # schedule-driven and signal-driven paths can
                                        # never desync from each other

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        print("[notifications] monitor started")

    def stop(self):
        self._stop_event.set()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self._poll()
            except Exception as e:
                print(f"[notifications] poll error: {type(e).__name__}: {e}")
            time.sleep(self.POLL_SECS)

    def _source_data_from(self, src, to_float):
        """Given a specific picotuner_state-shaped dict (already chosen
        by the caller - never does any A-vs-B picking itself), builds
        the callsign/frequency/mer/margin/modcod/symbol_rate dict every
        output below reads from. Extracted as its own method so both
        _current_source_data() (MER-comparison/displayed-receiver based)
        and _source_data_for_rcv() (explicit, per-receiver - see its own
        docstring) share this exact same, single-tested frequency/LNB
        conversion logic rather than risking two copies drifting apart."""
        # picotuner_state['frequency'] is parsed directly from the
        # Picotuner's own status broadcast, which reports it in MHz (see
        # lynx_app.py's own parsing comment: "437.024 G8YTZ") - genuinely
        # converted to kHz here to match this field's name and the rest
        # of Lynx's own kHz convention (presets, tuning). Confirmed bug:
        # this used to just relabel the raw MHz value as "kHz" with no
        # actual conversion, so submit_qrz_logbook()'s own /1000 (which
        # correctly expects real kHz input) silently turned a genuine
        # 437MHz signal into 0.437MHz - outside every defined ADIF band
        # range, leaving QRZ's own <band> field empty and the whole
        # submission rejected.
        freq_mhz_raw = to_float(src.get("frequency")) or 0.0

        # When an LNB is in use, the Picotuner reports the L-band/IF
        # frequency it's actually locked on, not the real satellite
        # downlink frequency - same underlying fact lynx_app.py's own
        # _compute_downlink_frequency() exists to handle, reversed here
        # against whichever source was actually picked, rather than
        # assuming tuner A the way that function does - the two tuners
        # are always tuned to the same frequency in diversity mode, but
        # not necessarily in single-plug-B-only or tri_watch operation.
        lnb_lo_khz, lnb_side = self.get_lnb_state()
        if lnb_lo_khz:
            lo_mhz = lnb_lo_khz / 1000
            if lnb_side == "high":
                # High-side injection (C-band): IF = LO - downlink
                freq_mhz = lo_mhz - freq_mhz_raw
            else:
                # Low-side injection (Ku-band): IF = downlink - LO
                freq_mhz = freq_mhz_raw + lo_mhz
        else:
            freq_mhz = freq_mhz_raw

        return {
            "rx_callsign": src.get("callsign", "") or "",
            "frequency_khz": freq_mhz * 1000.0,
            "mer": to_float(src.get("mer")),
            "margin": to_float(src.get("margin")),
            "modcod": src.get("modcod", "") or "",
            "symbol_rate": src.get("symbol_rate", "") or "",
        }

    def _source_data_for_rcv(self, rcv):
        """Like _current_source_data(), but for an EXPLICIT receiver (1
        or 2) rather than picking one via MER comparison or the tri_watch
        displayed-receiver callback. Used only by
        _poll_tri_watch_qrz_slack() - each receiver's own, independent
        lock tracking already knows exactly which one just confirmed, so
        there's no picking to do here at all, unlike
        _current_source_data()."""
        def to_float(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        src = self.picotuner_state_b if rcv == 2 else self.picotuner_state
        return self._source_data_from(src, to_float)

    def _current_source_data(self):
        """Picks whichever tuner is actually the active signal right now
        (higher MER if both are locked simultaneously) and returns its
        callsign/frequency/mer/margin/modcod/symbol_rate as a plain dict -
        the single source of truth every output below reads from, so QRZ
        and Slack can never disagree about which tuner's data they used."""
        def to_float(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        a = self.picotuner_state
        b = self.picotuner_state_b
        a_mer = to_float(a.get("mer"))
        b_mer = to_float(b.get("mer"))
        a_locked = a.get("locked", False)
        b_locked = b.get("locked", False)

        tri_watch_displayed_rcv = self.get_tri_watch_displayed_rcv()
        if tri_watch_displayed_rcv is not None:
            # tri_watch is actively displaying a specific receiver right
            # now - log THAT one, not whichever happens to have a
            # numerically better MER. Confirmed as a real, reported bug
            # otherwise: the two receivers are on completely independent
            # frequencies under tri_watch (unlike diversity mode's
            # redundant-signal comparison below), and tri_watch keeps
            # both continuously tuned regardless of which is displayed -
            # so if the OTHER receiver also happened to be locked with a
            # higher MER at that moment, the comparison below would
            # silently log it instead of the one actually on screen.
            use_b = (tri_watch_displayed_rcv == 2)
        elif self.get_tri_watch_enabled():
            # tri_watch is on but not currently displaying an RF source
            # at all (stream or idle) - return neutral, empty data
            # rather than falling through to the MER comparison below,
            # which would otherwise silently pick up whichever
            # background receiver happens to be locked, even though
            # neither is actually the displayed source. This method is
            # no longer used for QRZ/Slack under tri_watch at all (see
            # _poll_tri_watch_qrz_slack()) - kept defensive regardless,
            # since Companion/GPIO Tx settling actions don't currently
            # call this, but nothing prevents a future caller from doing
            # so.
            return {
                "rx_callsign": "", "frequency_khz": 0.0, "mer": None,
                "margin": None, "modcod": "", "symbol_rate": "",
            }
        else:
            use_b = b_locked and (not a_locked or (b_mer is not None and (a_mer is None or b_mer > a_mer)))
        src = b if use_b else a
        return self._source_data_from(src, to_float)

    # -- lock/unlock transition handling (QRZ, Slack, Companion) --------

    def _poll(self):
        cfg = self.get_config()
        notif_cfg = cfg.get('notifications', {})
        diversity_enabled = cfg.get('diversity', {}).get('enabled', False)
        # Rx2's lock status is only meaningful to consider when it's
        # actually, deliberately being monitored - true during diversity
        # mode (as before), but also true during tri_watch whenever it
        # has an enabled Rx2 source configured. Missing this second case
        # was a real, confirmed bug: QRZ/Slack/Companion/GPIO Tx all
        # silently never fired for an Rx2-only lock under tri_watch,
        # since raw_locked below never considered b_locked at all
        # outside diversity mode - even though _current_source_data()
        # just below already correctly, independently picks whichever
        # tuner is actually active, with no such gating of its own.
        tri_watch_cfg = cfg.get('tri_watch', {})
        tri_watch_has_rx2 = tri_watch_cfg.get('enabled', False) and any(
            src.get('type') == 'rf' and src.get('rcv') == 2 and src.get('enabled', False)
            for src in tri_watch_cfg.get('sources', [])
        )
        b_locked_relevant = diversity_enabled or tri_watch_has_rx2

        # Drives Companion lock/unlock and GPIO Tx - both represent a
        # single, physical on/off indicator for "is the repeater's RF
        # input active at all", independent of what's currently
        # displayed, so genuine background activity on either receiver
        # under tri_watch should still count here, exactly as it always
        # did before tri_watch existed. QRZ/Slack are handled completely
        # separately below when tri_watch is enabled (each receiver
        # tracked independently, so every genuine contact gets logged
        # correctly regardless of what's displayed) - outside tri_watch,
        # they still piggyback on this same shared signal via
        # _on_confirmed_lock/_on_confirmed_unlock below, unchanged.
        raw_locked = self.picotuner_state.get("locked", False) or \
                     (b_locked_relevant and self.picotuner_state_b.get("locked", False))

        if raw_locked:
            self._loss_streak = 0
            self._lock_streak += 1
        else:
            self._lock_streak = 0
            self._loss_streak += 1

        if self._lock_streak >= self.LOCK_CONFIRM_POLLS and not self._confirmed_locked:
            self._confirmed_locked = True
            print(f"[notifications] confirmed LOCK (own {self.LOCK_CONFIRM_POLLS}-poll debounce) - arming QRZ/Slack/Companion settle timers")
            self.record_event("notif_confirmed_lock",
                               "Notifications manager's own lock confirmation - arming settle timers",
                               count_as_mpv_restart=False)
            self._on_confirmed_lock(notif_cfg, cfg)
        elif self._loss_streak >= self.LOCK_CONFIRM_POLLS and self._confirmed_locked:
            self._confirmed_locked = False
            print(f"[notifications] confirmed UNLOCK (own {self.LOCK_CONFIRM_POLLS}-poll debounce) - cancelling any pending settle timers")
            self.record_event("notif_confirmed_unlock",
                               "Notifications manager's own unlock confirmation - cancelling pending settle timers",
                               count_as_mpv_restart=False)
            self._on_confirmed_unlock(notif_cfg, tri_watch_cfg.get('enabled', False))

        # QRZ/Slack under tri_watch: tracked independently per receiver
        # here, rather than via the shared _confirmed_locked above -
        # see _poll_tri_watch_qrz_slack()'s own docstring for the full
        # rationale. Runs alongside, not instead of, everything above -
        # Companion/GPIO Tx keep using the shared signal regardless.
        if tri_watch_cfg.get('enabled', False):
            self._poll_tri_watch_qrz_slack(notif_cfg, cfg)

        self._poll_tx_pin(notif_cfg, cfg)

    def _arm_action(self, key, delay, callback, name):
        """Start (or restart) a named, persistent settling timer. Storing
        it in self._actions (rather than a bare local variable) is what
        lets a later, opposite event find and cancel it before it fires -
        see the constructor's own note on why this matters."""
        action = SettlingAction(delay, callback, name)
        self._actions[key] = action
        action.trigger()

    def _cancel_action(self, key):
        action = self._actions.get(key)
        if action and action.pending:
            print(f"[notifications] cancelling pending '{key}' action before it fired")
            self.record_event("notif_action_cancelled",
                               f"Pending '{key}' action cancelled before its settle time elapsed "
                               f"(state reverted before firing)",
                               count_as_mpv_restart=False)
        if action:
            action.cancel()

    def _ensure_companion_gpio(self, comp_cfg):
        """(Re)builds the Companion-mirroring GPIO pin if its pin/polarity
        config has changed, or returns the existing one - identical
        pattern to the Tx pin's own cfg_key check in _poll_tx_pin()."""
        pin = comp_cfg.get('gpio_pin')
        if pin is None:
            return None
        active_high = (comp_cfg.get('gpio_polarity', 'high') == 'high')
        cfg_key = (pin, active_high)
        if self._gpio_companion is None or self._companion_gpio_cfg_key != cfg_key:
            self._gpio_companion = GpioPin(pin, active_high, name="Companion lock/unlock")
            self._companion_gpio_cfg_key = cfg_key
            self._cancel_action('companion_gpio_lock')
            self._cancel_action('companion_gpio_unlock')
        return self._gpio_companion

    def _on_confirmed_lock(self, notif_cfg, cfg):
        site_callsign = cfg.get('site', {}).get('callsign', '')
        tri_watch_enabled_now = cfg.get('tri_watch', {}).get('enabled', False)

        # We're locked again - cancel anything the unlock side had
        # pending a moment ago, so it can't fire late against a signal
        # that's actually back.
        self._cancel_action('companion_unlock')
        self._cancel_action('companion_gpio_unlock')

        # QRZ/Slack: only fired from here outside tri_watch - under
        # tri_watch, _poll_tri_watch_qrz_slack() handles both
        # independently per receiver instead, so firing them again here
        # too (using this shared signal's own, MER-compared source data,
        # which doesn't mean anything once two receivers are on
        # independent frequencies) would risk duplicate or incorrectly-
        # attributed entries alongside the correct, per-receiver ones.
        if not tri_watch_enabled_now:
            qrz_cfg = notif_cfg.get('qrz', {})
            if qrz_cfg.get('enabled', False):
                delay = float(qrz_cfg.get('settle_secs', 15.0))
                self._arm_action('qrz', delay, lambda: self._fire_qrz(qrz_cfg, site_callsign), "QRZ")

            slack_cfg = notif_cfg.get('slack', {})
            if slack_cfg.get('enabled', False):
                delay = float(slack_cfg.get('settle_secs', 15.0))
                self._arm_action('slack', delay, lambda: self._fire_slack(slack_cfg, site_callsign), "Slack")

        comp_cfg = notif_cfg.get('companion', {})
        if comp_cfg.get('enabled', False) and comp_cfg.get('lock_url'):
            delay = float(comp_cfg.get('lock_settle_secs', 5.0))
            self._arm_action('companion_lock', delay,
                              lambda: trigger_companion(comp_cfg['lock_url']), "Companion-lock")

        if comp_cfg.get('gpio_enabled', False):
            gpio = self._ensure_companion_gpio(comp_cfg)
            if gpio is not None and gpio.available:
                # Deliberately the SAME settling timer as the Companion
                # webhook above, not a separate one - this pin exists to
                # mirror the webhook-driven lock/unlock for relay-based
                # input switching, not to introduce its own timing.
                delay = float(comp_cfg.get('lock_settle_secs', 5.0))
                self._arm_action('companion_gpio_lock', delay,
                                  lambda: gpio.set(True), "Companion-GPIO-lock")

    def _on_confirmed_unlock(self, notif_cfg, tri_watch_enabled_now=False):
        # We're unlocked again - cancel anything the lock side had
        # pending, matching the reference code's explicit design: a
        # webhook (or now, GPIO change) must not fire if the state goes
        # back to unlocked during its own settling time.
        if not tri_watch_enabled_now:
            self._cancel_action('qrz')
            self._cancel_action('slack')
        self._cancel_action('companion_lock')
        self._cancel_action('companion_gpio_lock')

        comp_cfg = notif_cfg.get('companion', {})
        if comp_cfg.get('enabled', False) and comp_cfg.get('unlock_url'):
            delay = float(comp_cfg.get('unlock_settle_secs', 5.0))
            self._arm_action('companion_unlock', delay,
                              lambda: trigger_companion(comp_cfg['unlock_url']), "Companion-unlock")

        if comp_cfg.get('gpio_enabled', False):
            gpio = self._ensure_companion_gpio(comp_cfg)
            if gpio is not None and gpio.available:
                delay = float(comp_cfg.get('unlock_settle_secs', 5.0))
                self._arm_action('companion_gpio_unlock', delay,
                                  lambda: gpio.set(False), "Companion-GPIO-unlock")

    def _poll_tri_watch_qrz_slack(self, notif_cfg, cfg):
        """QRZ/Slack under tri_watch: tracks Rx1 and Rx2's own lock state
        completely independently of each other and of Companion/GPIO
        Tx's shared signal above - confirmed as what's actually wanted
        (Justin: "happy for it to be ALL"). Every genuine contact gets
        logged, correctly attributed, from whichever receiver actually
        decoded it, regardless of which (if either) is currently
        displayed, and regardless of what the OTHER receiver happens to
        be doing at the same moment - fixing a second, related gap
        found while building this: with only one shared confirmed-lock
        flag, a genuine, distinct contact on the non-displayed receiver
        would previously never get its own settle timer armed at all
        while the displayed one was already confirmed-locked, since the
        shared flag never transitioned back to trigger it again.

        Each receiver gets its own settle timer (the same configured
        settle_secs, applied independently) and its own lock/loss
        debounce, keyed apart via f'tw_qrz_{rcv}' / f'tw_slack_{rcv}' so
        one receiver's timer can never cancel or be confused with the
        other's. The suppress-window dedup (self._qrz_last_logged,
        keyed by callsign, not receiver) is shared and unchanged - it
        already correctly applies globally regardless of which receiver
        decoded a given callsign."""
        tri_watch_cfg = cfg.get('tri_watch', {})
        site_callsign = cfg.get('site', {}).get('callsign', '')
        sources = tri_watch_cfg.get('sources', [])
        qrz_cfg = notif_cfg.get('qrz', {})
        slack_cfg = notif_cfg.get('slack', {})

        for rcv in (1, 2):
            # Only track a receiver that's actually configured and
            # enabled as a tri_watch source - one that isn't has no
            # business independently triggering anything here.
            rcv_enabled = any(
                src.get('type') == 'rf' and src.get('rcv') == rcv and src.get('enabled', False)
                for src in sources
            )
            if not rcv_enabled:
                continue

            state = self.picotuner_state_b if rcv == 2 else self.picotuner_state
            locked = state.get("locked", False)

            if locked:
                self._tw_loss_streak[rcv] = 0
                self._tw_lock_streak[rcv] += 1
            else:
                self._tw_lock_streak[rcv] = 0
                self._tw_loss_streak[rcv] += 1

            if self._tw_lock_streak[rcv] >= self.LOCK_CONFIRM_POLLS and not self._tw_confirmed_locked[rcv]:
                self._tw_confirmed_locked[rcv] = True
                print(f"[notifications] tri_watch Rx{rcv}: confirmed LOCK - arming its own QRZ/Slack settle timers")
                self.record_event("notif_confirmed_lock",
                                   f"tri_watch Rx{rcv}: own lock confirmation - arming settle timers",
                                   count_as_mpv_restart=False)

                if qrz_cfg.get('enabled', False):
                    delay = float(qrz_cfg.get('settle_secs', 15.0))
                    self._arm_action(
                        f'tw_qrz_{rcv}', delay,
                        lambda r=rcv: self._fire_qrz(qrz_cfg, site_callsign,
                                                      source_override=self._source_data_for_rcv(r)),
                        f"QRZ (tri_watch Rx{rcv})")

                if slack_cfg.get('enabled', False):
                    delay = float(slack_cfg.get('settle_secs', 15.0))
                    self._arm_action(
                        f'tw_slack_{rcv}', delay,
                        lambda r=rcv: self._fire_slack(slack_cfg, site_callsign,
                                                        source_override=self._source_data_for_rcv(r)),
                        f"Slack (tri_watch Rx{rcv})")

            elif self._tw_loss_streak[rcv] >= self.LOCK_CONFIRM_POLLS and self._tw_confirmed_locked[rcv]:
                self._tw_confirmed_locked[rcv] = False
                print(f"[notifications] tri_watch Rx{rcv}: confirmed UNLOCK - cancelling its own pending settle timers")
                self.record_event("notif_confirmed_unlock",
                                   f"tri_watch Rx{rcv}: own unlock confirmation - cancelling pending settle timers",
                                   count_as_mpv_restart=False)
                self._cancel_action(f'tw_qrz_{rcv}')
                self._cancel_action(f'tw_slack_{rcv}')

    def _fire_qrz(self, qrz_cfg, site_callsign, source_override=None):
        api_key = qrz_cfg.get('api_key', '')
        if not api_key:
            print("[notifications] QRZ enabled but no API key configured - skipping")
            self.record_event("qrz_skipped", "No API key configured", count_as_mpv_restart=False)
            return
        # source_override: an explicit source dict (from
        # _source_data_for_rcv), used by tri_watch's own, independent
        # per-receiver tracking - see _poll_tri_watch_qrz_slack(). Falls
        # back to the MER-comparison/displayed-receiver logic otherwise,
        # exactly as before, for normal/diversity mode.
        src = source_override if source_override is not None else self._current_source_data()
        call = src["rx_callsign"].strip()
        if not call:
            print("[notifications] QRZ: no callsign decoded by settling time - skipping this entry")
            self.record_event("qrz_skipped", "No callsign had been decoded by the time the settle timer fired",
                               count_as_mpv_restart=False)
            return

        suppress_secs = float(qrz_cfg.get('suppress_mins', 60)) * 60.0
        now = time.time()
        last = self._qrz_last_logged.get(call, 0)
        if now - last < suppress_secs:
            remaining = suppress_secs - (now - last)
            print(f"[notifications] QRZ: suppressing duplicate for {call} "
                  f"({remaining:.0f}s remaining in window)")
            self.record_event("qrz_skipped",
                               f"Suppressed duplicate for {call} ({remaining:.0f}s remaining in window)",
                               count_as_mpv_restart=False)
            return

        try:
            portable_locator = qrz_cfg.get('portable_locator', '').strip()
            if portable_locator:
                print(f"[notifications] QRZ: logging {call} with portable "
                      f"locator override ({portable_locator}) instead of "
                      f"their registered QRZ locator")
            # ADIF's MODE field wants the standard name alongside the
            # modcod (confirmed against a genuine, correct QRZ Logbook
            # entry: "DVB-S2 QPSK 2/3") - src["modcod"] alone is just the
            # coding rate half of that. Scoped to this QRZ submission
            # only, not the underlying modcod value itself, which the
            # OSD/Web UI/Slack all still show on its own for brevity.
            # Assumption worth flagging: DVB-S2 is hardcoded here since
            # every modcod Lynx has ever reported includes an explicit
            # modulation type (QPSK/8PSK) alongside the coding rate -
            # DVB-S2's variable-modulation notation, not DVB-S1's simpler,
            # fixed-QPSK one - so this should be safe unless the Picotuner
            # is ever used somewhere that genuinely receives DVB-S1.
            adif_mode = f"DVB-S2 {src['modcod']}" if src["modcod"] else "DVB-S2"
            result = submit_qrz_logbook(api_key, site_callsign, call, src["frequency_khz"],
                                         adif_mode, src["mer"], src["margin"],
                                         portable_locator=portable_locator)
            self._qrz_last_logged[call] = now
            if result["result"] in ("OK", "REPLACE"):
                self.record_event("qrz_logged",
                                   f"Logged {call} - logid={result['logid']}",
                                   count_as_mpv_restart=False)
            else:
                self.record_event("qrz_failed",
                                   f"QRZ rejected {call} - result={result['result']} "
                                   f"reason={result['reason']} | sent: mode={result['mode_sent']!r} "
                                   f"band={result['band_sent']!r} freq_khz={src['frequency_khz']!r} "
                                   f"mer={src['mer']!r} margin={src['margin']!r}",
                                   count_as_mpv_restart=False)
        except Exception as e:
            print(f"[notifications] QRZ: submission failed - {type(e).__name__}: {e}")
            self.record_event("qrz_failed", f"Exception during submission - {type(e).__name__}: {e}",
                               count_as_mpv_restart=False)

    def _fire_slack(self, slack_cfg, site_callsign, source_override=None):
        webhook_url = slack_cfg.get('webhook_url', '')
        template = slack_cfg.get('message_template', '')
        if not webhook_url or not template:
            print("[notifications] Slack enabled but not fully configured - skipping")
            return
        # source_override: see _fire_qrz's own comment - same rationale.
        src = source_override if source_override is not None else self._current_source_data()
        placeholders = {
            "site_callsign": site_callsign,
            "site_callsign_lower": site_callsign.lower(),
            "rx_callsign": src["rx_callsign"],
            "mer": f"{src['mer']:.1f}" if src["mer"] is not None else "?",
            "margin": f"{src['margin']:.1f}" if src["margin"] is not None else "?",
            "modcod": src["modcod"],
            "frequency": f"{src['frequency_khz'] / 1000.0:.3f}",
        }
        try:
            send_slack_message(webhook_url, template, placeholders)
        except KeyError as e:
            print(f"[notifications] Slack: message template uses unknown placeholder {e} - skipping")
        except Exception as e:
            print(f"[notifications] Slack: send failed - {type(e).__name__}: {e}")

    # -- GPIO Tx on/off pin: schedule-gated, signal-driven fallback ------

    def _poll_tx_pin(self, notif_cfg, cfg, now=None):
        tx_cfg = notif_cfg.get('gpio_tx', {})
        if not tx_cfg.get('enabled', False):
            return
        pin = tx_cfg.get('pin')
        if pin is None:
            return
        active_high = (tx_cfg.get('polarity', 'high') == 'high')

        cfg_key = (pin, active_high)
        if self._gpio_tx is None or self._tx_pin_cfg_key != cfg_key:
            # Pin or polarity changed (or first run) - (re)build the
            # underlying GPIO object and reset our own tracked state, so
            # stale assumptions from a previous pin/polarity can't leak in.
            self._gpio_tx = GpioPin(pin, active_high, name="Tx on/off")
            self._tx_pin_cfg_key = cfg_key
            self._cancel_tx_actions()
            self._tx_was_in_window = False
            self._tx_was_locked = False

        if not self._gpio_tx.available:
            return

        if now is None:
            now = datetime.datetime.now()
        in_window = is_in_schedule_window(
            now,
            tx_cfg.get('schedule_weekday_start', ''),
            tx_cfg.get('schedule_weekday_end', ''),
            tx_cfg.get('schedule_weekend_start', ''),
            tx_cfg.get('schedule_weekend_end', ''),
        )
        locked = self._confirmed_locked
        power_up_secs = float(tx_cfg.get('power_up_settle_secs', 5.0))
        power_down_secs = float(tx_cfg.get('power_down_settle_secs', 900.0))

        if in_window and not self._tx_was_in_window:
            # Just entered a schedule window - force on immediately, no
            # settling timer (a scheduled event is predictable, not a
            # noisy signal needing debounce), cancelling anything the
            # auto logic had pending.
            self._cancel_tx_actions()
            self._gpio_tx.set(True)
            print("[notifications] Tx: schedule window started - forcing on")

        elif not in_window and self._tx_was_in_window:
            # Just left a schedule window.
            if locked:
                # Repeater is in use right now - leave the pin on and do
                # nothing further. Normal auto rules (including the
                # power-down timer) pick up from the NEXT unlock.
                print("[notifications] Tx: schedule window ended, still in use - staying on")
            else:
                # Idle - start the power-down timer from here, unless the
                # sentinel (0) says never auto power-down.
                print("[notifications] Tx: schedule window ended, idle - starting power-down timer")
                if power_down_secs > 0:
                    self._arm_tx_power_down(power_down_secs)

        elif not in_window:
            # Normal, signal-driven auto logic - only applies outside any
            # schedule window (or when no schedule is configured for
            # today at all, in which case in_window is always False and
            # this is simply the 24-hour-a-day behaviour).
            if locked and not self._tx_was_locked:
                self._cancel_tx_actions()
                self._arm_tx_power_up(power_up_secs)
            elif not locked and self._tx_was_locked:
                self._cancel_tx_actions()
                if power_down_secs > 0:
                    self._arm_tx_power_down(power_down_secs)
                # else: sentinel - never auto power down once triggered on

        # else: in_window with no transition - already forced on, nothing to do

        self._tx_was_in_window = in_window
        self._tx_was_locked = locked

    def _cancel_tx_actions(self):
        if self._tx_power_up_action:
            self._tx_power_up_action.cancel()
            self._tx_power_up_action = None
        if self._tx_power_down_action:
            self._tx_power_down_action.cancel()
            self._tx_power_down_action = None

    def _arm_tx_power_up(self, delay):
        self._tx_power_up_action = SettlingAction(
            delay, lambda: self._gpio_tx.set(True), "Tx-power-up")
        self._tx_power_up_action.trigger()

    def _arm_tx_power_down(self, delay):
        self._tx_power_down_action = SettlingAction(
            delay, lambda: self._gpio_tx.set(False), "Tx-power-down")
        self._tx_power_down_action.trigger()
