#!/usr/bin/env python3
"""
lynx_sources.py — one description of what a source is.

Step 1 of the Lynx+ roadmap. This module is deliberately inert: it
describes the sources that already exist and nothing consumes it yet,
so it cannot break a receiver that currently works. Consumers move onto
it one at a time, each testable on air on its own.

The problem it solves
---------------------
Adding the HDHomeRun meant editing eight separate places, each asking
"what mode are we in?" and then reaching into whichever state dict
belongs to that mode. Every one of those eight was found by something
visibly wrong on screen rather than by reading the code. The same eight
will need finding again for ATSC 3.0, and again after that.

The half-solution already in the code
-------------------------------------
REMOTE_QUALITY_FIELDS carries a comment worth quoting: "Same names as
picotuner_state so a panel or overlay that already reads a tuner can
read a Slave without a second shape." That is exactly the right idea,
applied once, and it works perfectly - for two sources that happen to
report the same things.

It broke on the HDHomeRun, whose figures genuinely are different. SNQ
is not MER and SEQ is not margin; they are percentages, measured
differently, and pretending otherwise would put a number under a dB
heading that does not belong there.

So the shape here is one step more general: a source reports a LIST of
figures, each carrying its own label and unit. A DVB-S2 tuner reports
MER in dB and margin in dB; an HDHomeRun reports SNQ and SEQ in per
cent. Anything drawing them draws what it is given, and needs to know
nothing about which tuner produced them. That is the whole objective -
ATSC 3.0 should be a registry entry and no overlay work at all.

What this module does NOT do
----------------------------
It does not talk to hardware. Every source here reads state that
lynx_app.py already maintains, through accessors passed in at
construction. The layers that drive the Picotuner, the HDHomeRun and
the Slaves work and are not the problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


# ──────────────────────────────────────────────────────────────────
# What a source reports
# ──────────────────────────────────────────────────────────────────

@dataclass
class Figure:
    """One quality reading, carrying its own name and unit.

    The point of this class is everything it prevents. A consumer
    drawing a magic eye or a status panel asks for the figures and
    draws them; it never asks "is this a Picotuner, so should I look
    for mer?" - which is the question that needed answering in eight
    places every time a source was added.

    scale_max exists for the magic eye, which needs to know what full
    scale means: 100 for a percentage, but a dBm reading needs its own
    range and a symbol rate has no meaningful gauge at all.
    """
    label: str                      # "MER", "SNQ", "Margin", "Level"
    value: Optional[float]          # None when not currently known
    unit: str = ""                  # "dB", "%", "dBm", "Mb/s"
    scale_max: Optional[float] = None   # full scale, or None if ungauged
    primary: bool = False           # the headline figure for this source


@dataclass
class Telemetry:
    """Everything a consumer might want to display about a source.

    Deliberately flat and deliberately optional. A source fills in what
    it genuinely knows and leaves the rest alone; nothing downstream
    should invent a value it was not given, and a blank field is an
    honest answer.
    """
    locked: bool = False
    online: bool = False

    # What it is tuned to, already in display form. The source does the
    # unit conversion, because the source is the only thing that knows
    # whether its own frequency arrived in Hz, kHz or MHz - and getting
    # that wrong once put a 437 MHz contact in the logbook at 0.437.
    frequency_mhz: Optional[float] = None
    mode_name: str = ""             # "QPSK 3/4", "DVB-T2 1 MHz"
    rate_text: str = ""             # "333 kS/s", "1 MHz", "6.62 Mb/s"

    # Who is on it. callsign is what reaches QRZ and the logbook, so a
    # source that cannot vouch for its own service names leaves it
    # empty rather than guessing - see trusts_callsign() below.
    callsign: str = ""
    callsign_name: str = ""         # first name from QRZ, when cached
    programme: str = ""

    codec: str = ""
    audio_codec: str = ""

    figures: list = field(default_factory=list)   # of Figure


# ──────────────────────────────────────────────────────────────────
# Sources
# ──────────────────────────────────────────────────────────────────

class Source:
    """Base class. Every source answers the same questions.

    Nothing here knows about any other source, and nothing outside
    needs to know which subclass it is holding.
    """

    kind = "?"          # rf, dvbt, atsc3, slave, stream - display only

    def __init__(self, source_id: str, label: str):
        self.id = source_id
        self.label = label

    # -- the questions every consumer asks -------------------------

    def is_locked(self) -> bool:
        """Is it genuinely carrying a picture right now?

        Not "is the hardware present" and not "is the mode selected" -
        those are different questions and conflating them is what put
        the logo screen over live DVB-T2 video while mpv decoded away
        underneath.
        """
        raise NotImplementedError

    def is_online(self) -> bool:
        """Is the hardware there at all?"""
        raise NotImplementedError

    def telemetry(self) -> Telemetry:
        raise NotImplementedError

    def trusts_callsign(self) -> bool:
        """Is a service name from this source worth believing?

        A Picotuner hears only amateurs, so yes. An HDHomeRun on a
        broadcast multiplex hears "BBC ONE Lon HD", which must never
        reach QRZ as a callsign - so it decides per frequency.
        """
        return True

    def stream_url(self) -> str:
        """Where a player can find this source's transport stream."""
        return ""

    def __repr__(self):
        return f"<{type(self).__name__} {self.id}>"


class PicotunerSource(Source):
    """A Picotuner receiver, A or B.

    The state dict arrives as a callable rather than a reference
    because lynx_app.py rebinds these globals, and a reference captured
    at construction would go stale in a way that is invisible until
    something reads a figure that stopped updating an hour ago.
    """

    kind = "rf"

    def __init__(self, source_id, label, get_state, rcv: int,
                 get_mpv_running=None, get_on_air_freq=None):
        super().__init__(source_id, label)
        self._state = get_state
        self.rcv = rcv
        self._mpv_running = get_mpv_running or (lambda: True)
        self._on_air = get_on_air_freq

    def is_online(self) -> bool:
        return bool(self._state().get("online"))

    def is_locked(self) -> bool:
        # mpv_running_for_rf matters: the tuner can be locked while the
        # player is mid-restart, and calling that "showing a picture"
        # is what made a stream-to-RF switch show a blank page.
        return bool(self._state().get("locked")) and bool(self._mpv_running())

    def trusts_callsign(self) -> bool:
        return True

    def telemetry(self) -> Telemetry:
        st = self._state()

        def num(key):
            try:
                v = st.get(key, "")
                return float(v) if str(v).strip() else None
            except (TypeError, ValueError):
                return None

        # The IF the tuner reports is not necessarily what is on air:
        # a converter sits between them, and five places once each did
        # their own version of the reversal. One function owns it.
        freq = num("frequency")
        if freq is not None and self._on_air is not None:
            on_air = self._on_air(self.rcv, freq)
            if on_air is not None:
                freq = on_air

        sr = st.get("symbol_rate", "")
        dbm = num("dbm")
        if dbm is None:
            lvl = num("level")
            # The old rough approximation is reported as a negative
            # dBm, which is how every consumer has always read it.
            dbm = -lvl if lvl is not None else None

        return Telemetry(
            locked=self.is_locked(),
            online=self.is_online(),
            frequency_mhz=freq,
            mode_name=st.get("modcod", ""),
            rate_text=f"{sr} kS/s" if sr else "",
            callsign=st.get("callsign", ""),
            programme=st.get("programme", ""),
            codec=st.get("codec", ""),
            audio_codec=st.get("audio_codec", ""),
            figures=[
                Figure("MER", num("mer"), "dB", scale_max=30, primary=True),
                Figure("Margin", num("margin"), "dB", scale_max=20),
                Figure("Level", dbm, "dBm"),
            ],
        )


class SlaveSource(Source):
    """A receiver at another site, reporting over the network.

    Reports the same figures as a Picotuner because it IS one - the
    remote end is a converted MiniTiouner. The existing code already
    reuses picotuner_state's field names for exactly this reason.

    Its converter, if it has one, is its own business: it reports what
    it is tuned to and nothing at this end should apply local
    arithmetic to a number from another site.
    """

    kind = "slave"

    def __init__(self, source_id, label, get_state):
        super().__init__(source_id, label)
        self._state = get_state

    def is_online(self) -> bool:
        return bool(self._state().get("online"))

    def is_locked(self) -> bool:
        return bool(self._state().get("locked"))

    def telemetry(self) -> Telemetry:
        st = self._state()

        def num(key):
            try:
                v = st.get(key, "")
                return float(v) if str(v).strip() else None
            except (TypeError, ValueError):
                return None

        sr = st.get("symbol_rate", "")
        return Telemetry(
            locked=self.is_locked(),
            online=self.is_online(),
            frequency_mhz=num("frequency"),
            mode_name=st.get("modcod", ""),
            rate_text=f"{sr} kS/s" if sr else "",
            callsign=st.get("callsign", ""),
            programme=st.get("programme", ""),
            codec=st.get("codec", ""),
            audio_codec=st.get("audio_codec", ""),
            figures=[
                Figure("MER", num("mer"), "dB", scale_max=30, primary=True),
                Figure("Margin", num("margin"), "dB", scale_max=20),
                Figure("Level", num("dbm"), "dBm"),
            ],
        )


class HdhrSource(Source):
    """One tuner of one HDHomeRun.

    One tuner, not one device: a CONNECT QUATRO has four and they tune
    independently, so the identifier carries the tuner index from the
    start. Retrofitting an index into ids already written into config
    files and presets is cheap now and expensive later.

    Its figures are its own. SNQ is the closest thing this demodulator
    reports to MER - the view before error correction - and SEQ behaves
    more like margin, sitting at 100 until the correction starts to
    struggle. They are percentages and they are not those things, so
    they are reported under their own names and a consumer draws what
    it is given.
    """

    kind = "dvbt"

    def __init__(self, source_id, label, get_state, device_id, tuner=0,
                 get_stream_info=None, is_broadcast=None):
        super().__init__(source_id, label)
        self._state = get_state
        self.device_id = device_id
        self.tuner = tuner
        self._stream_info = get_stream_info or (lambda: {})
        self._is_broadcast = is_broadcast

    def is_online(self) -> bool:
        st = self._state()
        return bool(st and st.get("online"))

    def is_locked(self) -> bool:
        st = self._state()
        return bool(st and st.get("locked"))

    def trusts_callsign(self) -> bool:
        """The test is the frequency, not the shape of the name.

        What band a tuner is on is a fact; whether a name looks like a
        callsign is a guess. Inverted from an allowlist of amateur
        bands because a converter puts the tuner somewhere that is not
        an amateur band at all - the IC-9700 brings 23cm down to
        375 MHz - and an allowlist silently refused every contact made
        through one.
        """
        st = self._state()
        if not st:
            return False
        if self._is_broadcast is None:
            return True
        return not self._is_broadcast(st.get("frequency_hz"))

    def telemetry(self) -> Telemetry:
        st = self._state() or {}
        lm = st.get("lock_mode") or ""

        # t8dvbt2 -> "DVB-T2" and "8 MHz". The second character is the
        # nominal bandwidth in MHz and the ladder is exact, so 1 really
        # does mean 1 - which took an evening and a correction from
        # SiliconDust to establish.
        std = ("DVB-T2" if lm.endswith("dvbt2")
               else "DVB-T" if "dvbt" in lm
               else "DVB-C" if "dvbc" in lm else "")
        bw = lm[1:2] if len(lm) > 1 else ""

        si = self._stream_info() or {}
        freq_hz = st.get("frequency_hz")
        bps = st.get("bitrate_bps") or 0

        def num(key):
            v = st.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return Telemetry(
            locked=self.is_locked(),
            online=self.is_online(),
            frequency_mhz=(freq_hz / 1e6) if freq_hz else None,
            mode_name=f"{std} {bw} MHz".strip() if std else "",
            rate_text=f"{bps / 1e6:.2f} Mb/s" if bps else "",
            callsign=st.get("callsign", ""),
            callsign_name=st.get("callsign_name", ""),
            programme=st.get("service_name", ""),
            # From the player rather than the tuner: the HDHomeRun
            # reports bitrate and quality but nothing about what is
            # inside the multiplex.
            codec=si.get("video_codec", "") or "",
            audio_codec=si.get("audio_codec", "") or "",
            figures=[
                Figure("SNQ", num("signal_quality"), "%",
                       scale_max=100, primary=True),
                Figure("SEQ", num("symbol_quality"), "%", scale_max=100),
                Figure("Level", num("signal_strength"), "%", scale_max=100),
            ],
        )


class StreamSource(Source):
    """A network stream - BATC, or any URL.

    Has no RF figures at all, which is the case that proves the list:
    a consumer that assumed MER and margin existed had to be given a
    blanket exemption for streams. Here it simply reports one figure,
    and anything drawing figures draws one.
    """

    kind = "stream"

    def __init__(self, source_id, label, get_name, get_stream_info,
                 get_active):
        super().__init__(source_id, label)
        self._name = get_name
        self._stream_info = get_stream_info
        self._active = get_active

    def is_online(self) -> bool:
        return True

    def is_locked(self) -> bool:
        return bool(self._active())

    def trusts_callsign(self) -> bool:
        # A stream's name is whatever the streamer typed. It reaches
        # the OSD but never the logbook.
        return False

    def telemetry(self) -> Telemetry:
        si = self._stream_info() or {}
        kbps = si.get("bitrate_kbps")
        return Telemetry(
            locked=self.is_locked(),
            online=True,
            programme=self._name() or "",
            rate_text=f"{kbps:.0f} kb/s" if kbps else "",
            codec=si.get("video_codec", "") or "",
            audio_codec=si.get("audio_codec", "") or "",
            figures=[
                Figure("Bitrate", kbps, "kb/s", primary=True),
            ],
        )


# ──────────────────────────────────────────────────────────────────
# The registry
# ──────────────────────────────────────────────────────────────────

class SourceRegistry:
    """Every source this receiver has, and which one is current.

    Ordered, because the web UI and the OSD both list sources and the
    order should be stable and sensible rather than whatever a dict
    happens to yield.
    """

    def __init__(self):
        self._sources = []          # in display order
        self._active_id = None

    def add(self, source: Source) -> Source:
        self._sources.append(source)
        return source

    def remove(self, source_id: str):
        self._sources = [s for s in self._sources if s.id != source_id]
        if self._active_id == source_id:
            self._active_id = None

    def all(self):
        return list(self._sources)

    def of_kind(self, kind: str):
        return [s for s in self._sources if s.kind == kind]

    def get(self, source_id: str) -> Optional[Source]:
        for s in self._sources:
            if s.id == source_id:
                return s
        return None

    # -- what is on screen -----------------------------------------

    def set_active(self, source_id: Optional[str]):
        self._active_id = source_id

    def active(self) -> Optional[Source]:
        return self.get(self._active_id) if self._active_id else None

    def active_id(self) -> Optional[str]:
        return self._active_id

    # -- the question eight different places used to answer --------

    def showing_picture(self) -> bool:
        """Is a real picture on screen right now?

        The one place this is decided. Every consumer that needs to
        know - the cover, the OSD corners, the magic eye, whether to
        draw the logo screen - asks here rather than assembling its own
        answer out of a mode string and a lock flag.
        """
        src = self.active()
        return bool(src and src.is_locked())

    def locked_sources(self):
        """Everything carrying a picture, whether displayed or not.

        For arbitration: an output's policy picks from these.
        """
        return [s for s in self._sources if s.is_locked()]

    def describe(self) -> list:
        """The whole registry as plain data, for /api/status.

        Consumers outside this process - the overlay, the web UI, and
        one day an Apple TV - get this rather than a mode string, and
        draw what they are given.
        """
        out = []
        for s in self._sources:
            t = s.telemetry()
            out.append({
                "id": s.id,
                "kind": s.kind,
                "label": s.label,
                "active": s.id == self._active_id,
                "online": t.online,
                "locked": t.locked,
                "frequency_mhz": t.frequency_mhz,
                "mode_name": t.mode_name,
                "rate_text": t.rate_text,
                "callsign": t.callsign,
                "callsign_name": t.callsign_name,
                "programme": t.programme,
                "codec": t.codec,
                "audio_codec": t.audio_codec,
                "figures": [
                    {"label": f.label, "value": f.value, "unit": f.unit,
                     "scale_max": f.scale_max, "primary": f.primary}
                    for f in t.figures
                ],
            })
        return out
