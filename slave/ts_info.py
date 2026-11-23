#!/usr/bin/env python3
"""Reading a transport stream's own description of itself.

WHY THIS EXISTS
---------------
Longmynd parses the SDT and PMT for us: the callsign arrives on $13,
the provider on $14, the elementary stream types on $17, and the
Minitiouner source does no parsing of its own. CombiTunerExpress does
none of that. It hands over a transport stream and says nothing
whatever about what is inside it.

So for a Knucker Remote Rx the callsign exists in exactly one place -
the stream itself - and without it the repeater cannot name the
station, and the QRZ lookup has nothing to look up. The stream is
already passing through this machine on its way to the repeater, so
reading it costs a copy and some arithmetic.

WHAT IT READS
-------------
    PAT  (PID 0)     which PID carries the PMT
    PMT              the elementary streams, and so the codecs
    SDT  (PID 0x11)  the service name and provider

The service name is what an amateur transmitter puts its callsign in,
which is why Lynx treats the two as the same field.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not verify CRCs. A corrupt section would have to survive the
demodulator's own FEC to get here, and the cost of a wrong service
name for one refresh is a misspelt callsign that corrects itself two
seconds later. Dropping every section to guard against that would mean
no callsign at all on a marginal signal, which is when you most want
one.

It does not follow a stream with more than one service. A Remote Rx
tunes one signal and relays one programme; if multi-programme support
ever arrives, the choice of which belongs with whatever makes that
choice, not here.
"""

import socket
import sys
import threading
import time

import remote_common

TS_PACKET = 188
TS_SYNC = 0x47

PID_PAT = 0x0000
PID_SDT = 0x0011

TABLE_PAT = 0x00
TABLE_PMT = 0x02
TABLE_SDT_ACTUAL = 0x42

DESC_SERVICE = 0x48

# How long the information survives without the stream refreshing it.
# The SDT repeats every couple of seconds, so this is generous; its job
# is to make sure a callsign disappears when its transmitter does
# rather than sitting on the panel over a dead carrier.
STALE_SECS = 10.0

# A section must be at least a header plus a CRC to be worth looking
# at, and the standard caps it at 1021 bytes past the length field.
MIN_SECTION = 8
MAX_SECTION = 1024


def _text(raw: bytes) -> str:
    """A DVB text string, as something printable.

    The first byte may select a character table (0x01-0x1F), and some
    encoders put control codes inside the string as well. Anything
    below 0x20 is dropped rather than decoded: the alternative is a
    character-set table for the sake of callsigns, which are ASCII.
    """
    cleaned = bytes(b for b in raw if b >= 0x20)
    for enc in ("utf-8", "latin-1"):
        try:
            return cleaned.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return ""


class _Sections:
    """Reassembles PSI sections on one PID.

    Sections usually fit in a single packet at these bitrates, but
    "usually" is not a thing to build on: a PMT with a few descriptors
    crosses the boundary and the callsign would then appear only on
    whichever refresh happened to fit.
    """

    def __init__(self):
        self.buf = bytearray()
        self.want = 0

    def feed(self, payload: bytes, start: bool):
        """Yields complete sections."""
        if start:
            if not payload:
                return
            # The first payload byte is a pointer to where the section
            # starts, so a packet can carry the tail of one section
            # and the head of the next.
            ptr = payload[0]
            rest = payload[1:]
            if ptr:
                tail, rest = rest[:ptr], rest[ptr:]
                for s in self._add(tail, finish=True):
                    yield s
            self.buf = bytearray()
            self.want = 0
            for s in self._add(rest):
                yield s
        else:
            for s in self._add(payload):
                yield s

    def _add(self, data: bytes, finish=False):
        if not data:
            return
        if not self.buf and not self.want:
            if len(data) < 3:
                return
            if data[0] == 0xFF:
                return      # stuffing, not a section
            length = ((data[1] & 0x0F) << 8) | data[2]
            self.want = length + 3
            if not (MIN_SECTION <= self.want <= MAX_SECTION):
                self.want = 0
                return
        self.buf.extend(data)
        if self.want and len(self.buf) >= self.want:
            section = bytes(self.buf[:self.want])
            self.buf = bytearray()
            self.want = 0
            yield section
        elif finish:
            self.buf = bytearray()
            self.want = 0


class TsInfo:
    """What the stream says about itself.

    Thread-safe: the tap thread writes it and the status sender reads
    it, at different rates.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._pmt_pid = None
        self._sections = {}
        self.service = ""
        self.provider = ""
        self.video_codec = ""
        self.audio_codec = ""
        self.last_update = 0.0
        self.packets = 0

    # ── reading ────────────────────────────────────────────────
    def snapshot(self) -> dict:
        with self._lock:
            fresh = (self.last_update > 0
                     and (time.time() - self.last_update) < STALE_SECS)
            if not fresh:
                return {"service": "", "provider": "",
                        "video_codec": "", "audio_codec": ""}
            return {"service": self.service, "provider": self.provider,
                    "video_codec": self.video_codec,
                    "audio_codec": self.audio_codec}

    def clear(self):
        """Forget the programme, keeping nothing. Called when the tuner
        loses lock or restarts: a callsign left over a signal that has
        gone is worse than no callsign."""
        with self._lock:
            self._pmt_pid = None
            self._sections = {}
            self.service = self.provider = ""
            self.video_codec = self.audio_codec = ""
            self.last_update = 0.0

    # ── parsing ────────────────────────────────────────────────
    def feed(self, data: bytes):
        """One UDP datagram of transport stream."""
        for off in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
            pkt = data[off:off + TS_PACKET]
            if pkt[0] != TS_SYNC:
                # Not aligned, or not a transport stream. The Knucker's
                # output is measured 1316-byte aligned on TS
                # boundaries, so this means something is wrong rather
                # than something to resynchronise around.
                return
            self._packet(pkt)

    def _packet(self, pkt: bytes):
        if pkt[1] & 0x80:
            return                                  # transport error
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        with self._lock:
            want = (pid == PID_PAT or pid == PID_SDT or pid == self._pmt_pid)
        if not want:
            return
        afc = (pkt[3] >> 4) & 0x03
        if afc in (0, 2):
            return                                  # no payload
        start = 4
        if afc == 3:
            start = 5 + pkt[4]
            if start >= TS_PACKET:
                return
        payload = pkt[start:]
        with self._lock:
            asm = self._sections.get(pid)
            if asm is None:
                asm = self._sections[pid] = _Sections()
            sections = list(asm.feed(payload, bool(pkt[1] & 0x40)))
        for section in sections:
            self._section(pid, section)

    def _section(self, pid: int, s: bytes):
        table = s[0]
        if pid == PID_PAT and table == TABLE_PAT:
            self._pat(s)
        elif pid == PID_SDT and table == TABLE_SDT_ACTUAL:
            self._sdt(s)
        elif table == TABLE_PMT and pid == self._pmt_pid:
            self._pmt(s)

    def _pat(self, s: bytes):
        # 3 header + 5 fixed, then four bytes per programme, less the
        # trailing CRC.
        end = len(s) - 4
        i = 8
        while i + 4 <= end:
            programme = (s[i] << 8) | s[i + 1]
            pid = ((s[i + 2] & 0x1F) << 8) | s[i + 3]
            # Programme 0 is the network information table, not a
            # service. The first real programme is the one this Remote
            # Rx is relaying.
            if programme != 0:
                with self._lock:
                    if self._pmt_pid != pid:
                        self._pmt_pid = pid
                        self._sections.pop(pid, None)
                return
            i += 4

    def _pmt(self, s: bytes):
        end = len(s) - 4
        if end < 12:
            return
        prog_info_len = ((s[10] & 0x0F) << 8) | s[11]
        i = 12 + prog_info_len
        video = audio = ""
        while i + 5 <= end:
            es_type = s[i]
            es_info_len = ((s[i + 3] & 0x0F) << 8) | s[i + 4]
            if not video and es_type in remote_common.VIDEO_STREAM_TYPES:
                video = remote_common.VIDEO_STREAM_TYPES[es_type]
            elif not audio and es_type in remote_common.AUDIO_STREAM_TYPES:
                audio = remote_common.AUDIO_STREAM_TYPES[es_type]
            elif not video and es_type not in remote_common.STREAM_TYPES:
                # Worth reporting as a number: that is what anybody
                # would look up anyway, and a blank would suggest there
                # was no stream at all.
                video = f"type {es_type}"
            i += 5 + es_info_len
        with self._lock:
            if video:
                self.video_codec = video
            if audio:
                self.audio_codec = audio
            self.last_update = time.time()

    def _sdt(self, s: bytes):
        end = len(s) - 4
        i = 11                      # 3 header + 8 fixed for an SDT
        while i + 5 <= end:
            desc_len = ((s[i + 3] & 0x0F) << 8) | s[i + 4]
            j, stop = i + 5, i + 5 + desc_len
            while j + 2 <= stop and j + 2 <= end:
                tag, dlen = s[j], s[j + 1]
                if tag == DESC_SERVICE and j + 2 + dlen <= end:
                    d = s[j + 3:j + 2 + dlen]       # past service_type
                    if d:
                        plen = d[0]
                        provider = _text(d[1:1 + plen])
                        rest = d[1 + plen:]
                        name = _text(rest[1:1 + rest[0]]) if rest else ""
                        with self._lock:
                            if name:
                                self.service = name
                            self.provider = provider
                            self.last_update = time.time()
                    return                  # first service only
                j += 2 + dlen
            i = stop


class TapThread(threading.Thread):
    """Reads the stream on loopback, reads what it says, passes it on.

    In the path rather than beside it because there is no way to see a
    copy of a UDP flow without being in it: SO_REUSEPORT would share
    the packets out between the two readers rather than duplicate them,
    which would quietly halve the stream going to the repeater.

    Forwarding happens FIRST and parsing after, so a fault in the
    parser can cost information but never the picture.
    """

    def __init__(self, listen_port, dest, info, stop):
        super().__init__(daemon=True, name="ts-info")
        self.listen_port = listen_port
        self.dest = dest
        self.info = info
        self.stop = stop
        self.datagrams = 0

    def run(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        rx.bind(("127.0.0.1", self.listen_port))
        rx.settimeout(0.5)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not self.stop.is_set():
            try:
                data = rx.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                print(f"[ts] receive failed: {e}", file=sys.stderr)
                time.sleep(0.5)
                continue
            try:
                tx.sendto(data, self.dest)
                self.datagrams += 1
            except OSError:
                pass        # nothing listening yet: the sender restarting
            try:
                self.info.feed(data)
            except Exception as e:      # never let parsing break the relay
                print(f"[ts] parse failed: {e}", file=sys.stderr)
