"""
lynx_rtmp_probe.py - Minimal, dependency-free RTMP client for detecting
whether a stream input is actively publishing, without decoding any
audio/video data.

Built as a deliberate replacement for python-librtmp (which Ryde Player
uses for the same purpose) after confirming that package is effectively
abandoned (no ARM64/modern-Python wheels, requires compiling against an
unmaintained C library) - see CHANGELOG.md for the full reasoning.

Detection mechanism, matching Ryde's own proven approach: connects as an
RTMP *player* (not a publisher), performs connect/createStream/play, then
watches only for the protocol's own User Control messages - "Stream
Begin" (event type 0) when a publisher starts, "Stream EOF" (event type
1) when they stop. No media packets are ever decoded or even fully
parsed; the moment a User Control message is seen, that's all this module
needs.

STATUS (2026-08-01): Built and unit-tested against synthetic protocol
data constructed directly from the RTMP specification. NOT yet tested
against a real RTMP server - the sandbox this was built in cannot reach
rtmp.batc.org.uk. Real-server validation on actual Lynx hardware is the
next required step before this is trusted for anything live.
"""

import socket
import struct
import threading
import time


# ─────────────────────────────────────────────────────────────────
# AMF0 encoding (for the outgoing connect/createStream/play commands)
# ─────────────────────────────────────────────────────────────────
# Only the handful of AMF0 types RTMP command messages actually need -
# this is not a general-purpose AMF0 library, deliberately.

AMF0_NUMBER = 0x00
AMF0_BOOLEAN = 0x01
AMF0_STRING = 0x02
AMF0_OBJECT = 0x03
AMF0_NULL = 0x05
AMF0_OBJECT_END = b'\x00\x00\x09'


def amf0_encode_number(n: float) -> bytes:
    return bytes([AMF0_NUMBER]) + struct.pack('>d', float(n))


def amf0_encode_boolean(b: bool) -> bytes:
    return bytes([AMF0_BOOLEAN, 1 if b else 0])


def amf0_encode_string(s: str) -> bytes:
    encoded = s.encode('utf-8')
    if len(encoded) > 0xFFFF:
        raise ValueError("amf0_encode_string: string too long for AMF0 short string")
    return bytes([AMF0_STRING]) + struct.pack('>H', len(encoded)) + encoded


def amf0_encode_null() -> bytes:
    return bytes([AMF0_NULL])


def amf0_encode_object(props: dict) -> bytes:
    out = bytearray([AMF0_OBJECT])
    for key, value in props.items():
        key_bytes = key.encode('utf-8')
        out += struct.pack('>H', len(key_bytes)) + key_bytes
        if isinstance(value, bool):
            out += amf0_encode_boolean(value)
        elif isinstance(value, (int, float)):
            out += amf0_encode_number(value)
        elif isinstance(value, str):
            out += amf0_encode_string(value)
        elif value is None:
            out += amf0_encode_null()
        else:
            raise TypeError(f"amf0_encode_object: unsupported value type {type(value)}")
    out += AMF0_OBJECT_END
    return bytes(out)


def amf0_decode_value(data: bytes, offset: int):
    """Decodes a single AMF0 value starting at offset. Returns (value, new_offset).
    Only implements the types actually seen in RTMP command-message
    responses (_result/_error/onStatus) - deliberately not a complete
    AMF0 decoder."""
    if offset >= len(data):
        raise ValueError("amf0_decode_value: ran out of data")
    marker = data[offset]
    offset += 1
    if marker == AMF0_NUMBER:
        value = struct.unpack('>d', data[offset:offset+8])[0]
        return value, offset + 8
    elif marker == AMF0_BOOLEAN:
        value = data[offset] != 0
        return value, offset + 1
    elif marker == AMF0_STRING:
        strlen = struct.unpack('>H', data[offset:offset+2])[0]
        offset += 2
        value = data[offset:offset+strlen].decode('utf-8', errors='replace')
        return value, offset + strlen
    elif marker == AMF0_NULL:
        return None, offset
    elif marker == 0x06:  # undefined
        return None, offset
    elif marker == AMF0_OBJECT:
        obj = {}
        while True:
            if data[offset:offset+3] == AMF0_OBJECT_END:
                offset += 3
                break
            keylen = struct.unpack('>H', data[offset:offset+2])[0]
            offset += 2
            key = data[offset:offset+keylen].decode('utf-8', errors='replace')
            offset += keylen
            value, offset = amf0_decode_value(data, offset)
            obj[key] = value
        return obj, offset
    elif marker == 0x08:  # ECMA array - same as object but with a 4-byte count prefix first
        offset += 4
        obj = {}
        while True:
            if data[offset:offset+3] == AMF0_OBJECT_END:
                offset += 3
                break
            keylen = struct.unpack('>H', data[offset:offset+2])[0]
            offset += 2
            key = data[offset:offset+keylen].decode('utf-8', errors='replace')
            offset += keylen
            value, offset = amf0_decode_value(data, offset)
            obj[key] = value
        return obj, offset
    else:
        raise ValueError(f"amf0_decode_value: unsupported AMF0 marker 0x{marker:02x}")


def amf0_decode_all(data: bytes):
    """Decodes a sequence of concatenated AMF0 values (an RTMP command
    message payload is just a list of these) into a list."""
    values = []
    offset = 0
    while offset < len(data):
        value, offset = amf0_decode_value(data, offset)
        values.append(value)
    return values


# ─────────────────────────────────────────────────────────────────
# RTMP handshake (plain, unencrypted - "simple handshake")
# ─────────────────────────────────────────────────────────────────
# C0(1) + C1(1536) sent, S0(1) + S1(1536) + S2(1536) expected back,
# then C2(1536, an echo of S1) completes it. This is the original,
# widely-supported handshake - no need for the newer "complex"
# (Diffie-Hellman) variant for a plain player connection like this.

RTMP_VERSION = 0x03
HANDSHAKE_SIZE = 1536


def rtmp_handshake(sock: socket.socket, timeout: float):
    sock.settimeout(timeout)

    c1 = struct.pack('>II', 0, 0) + bytes(HANDSHAKE_SIZE - 8)  # time=0, zero=0, then zero-filled random field (server doesn't validate this for the simple handshake)
    sock.sendall(bytes([RTMP_VERSION]) + c1)

    s0 = _recv_exact(sock, 1)
    if s0[0] != RTMP_VERSION:
        raise RTMPProtocolError(f"Unexpected RTMP version from server: {s0[0]}")
    s1 = _recv_exact(sock, HANDSHAKE_SIZE)
    s2 = _recv_exact(sock, HANDSHAKE_SIZE)

    c2 = s1  # echo S1 back as C2, per spec
    sock.sendall(c2)
    # s2 (echo of our C1) intentionally unused - nothing to validate for
    # a simple-handshake player connection


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Reads exactly n bytes or raises - plain sock.recv() can return
    fewer bytes than asked for, which would otherwise silently corrupt
    the handshake/chunk parsing."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise RTMPConnectionClosed("Connection closed while reading")
        buf += chunk
    return bytes(buf)


class RTMPProtocolError(Exception):
    pass


class RTMPConnectionClosed(Exception):
    pass


# ─────────────────────────────────────────────────────────────────
# RTMP chunk framing - sending
# ─────────────────────────────────────────────────────────────────
# Deliberately always sends full (fmt=0) headers - this client only
# ever sends a handful of small command messages (connect,
# createStream, play), never high-volume data, so the bandwidth
# saved by the header-compression chunk types the spec allows isn't
# worth the added complexity here.

def build_chunk(csid: int, msg_type: int, msg_stream_id: int, payload: bytes, chunk_size: int = 128) -> bytes:
    if csid < 2 or csid > 63:
        raise ValueError("build_chunk: only supports 1-byte basic headers (csid 2-63) - sufficient for this client's needs")
    out = bytearray()
    offset = 0
    first = True
    while offset < len(payload) or (first and len(payload) == 0):
        piece = payload[offset:offset + chunk_size]
        if first:
            basic_header = bytes([(0 << 6) | csid])  # fmt=0
            msg_header = (
                struct.pack('>I', 0)[1:4] +  # timestamp (3 bytes) - always 0, fine for command messages
                struct.pack('>I', len(payload))[1:4] +  # message length (3 bytes)
                bytes([msg_type]) +
                struct.pack('<I', msg_stream_id)  # message stream id, little-endian per spec
            )
            out += basic_header + msg_header + piece
            first = False
        else:
            basic_header = bytes([(3 << 6) | csid])  # fmt=3, continuation
            out += basic_header + piece
        offset += len(piece)
    return bytes(out)


# ─────────────────────────────────────────────────────────────────
# RTMP chunk framing - receiving
# ─────────────────────────────────────────────────────────────────
# Must handle all 4 header-compression formats (fmt 0-3), since the
# server chooses whichever it likes to save bandwidth - fmt 1/2/3
# chunks omit fields that are "unchanged from the last chunk on this
# same chunk stream ID", so per-CSID state has to be tracked across
# the whole connection, not just within one message.

class _ChunkStreamState:
    __slots__ = ('timestamp', 'msg_length', 'msg_type', 'msg_stream_id',
                 'partial_payload', 'timestamp_delta')
    def __init__(self):
        self.timestamp = 0
        self.msg_length = 0
        self.msg_type = 0
        self.msg_stream_id = 0
        self.partial_payload = bytearray()
        self.timestamp_delta = 0


class ChunkReader:
    def __init__(self):
        self.chunk_size = 128  # RTMP spec default until renegotiated
        self._streams = {}  # csid -> _ChunkStreamState

    def _get_stream(self, csid):
        if csid not in self._streams:
            self._streams[csid] = _ChunkStreamState()
        return self._streams[csid]

    def read_message(self, sock: socket.socket):
        """Blocks until one complete RTMP message has been reassembled
        from however many chunks it took, then returns
        (msg_type, msg_stream_id, payload). Transparently handles
        Set Chunk Size (type 1) control messages by updating
        self.chunk_size and continuing to read the next message,
        rather than returning it to the caller - the caller never
        needs to see it."""
        while True:
            first_byte = _recv_exact(sock, 1)[0]
            fmt = first_byte >> 6
            csid = first_byte & 0x3F
            if csid == 0:
                csid = 64 + _recv_exact(sock, 1)[0]
            elif csid == 1:
                extra = _recv_exact(sock, 2)
                csid = 64 + extra[0] + (extra[1] * 256)
            # else: csid is already correct (2-63), 1-byte basic header

            stream = self._get_stream(csid)

            if fmt == 0:
                header = _recv_exact(sock, 11)
                stream.timestamp = int.from_bytes(header[0:3], 'big')
                stream.msg_length = int.from_bytes(header[3:6], 'big')
                stream.msg_type = header[6]
                stream.msg_stream_id = struct.unpack('<I', header[7:11])[0]
                stream.timestamp_delta = 0
            elif fmt == 1:
                header = _recv_exact(sock, 7)
                stream.timestamp_delta = int.from_bytes(header[0:3], 'big')
                stream.msg_length = int.from_bytes(header[3:6], 'big')
                stream.msg_type = header[6]
                # msg_stream_id unchanged from this CSID's last chunk
            elif fmt == 2:
                header = _recv_exact(sock, 3)
                stream.timestamp_delta = int.from_bytes(header[0:3], 'big')
                # msg_length, msg_type, msg_stream_id all unchanged
            elif fmt == 3:
                pass  # everything unchanged - either a continuation of
                      # the current message, or a repeat of the previous
                      # header for a brand new message with identical
                      # parameters (distinguished below by whether a
                      # message is already in progress for this CSID)

            # Extended timestamp: present whenever the 3-byte field
            # (whichever one this fmt type included) reads as the
            # sentinel value 0xFFFFFF, meaning "see the next 4 bytes
            # for the real value instead"
            ts_field = stream.timestamp_delta if fmt in (1, 2) else stream.timestamp
            if ts_field == 0xFFFFFF:
                ext = _recv_exact(sock, 4)
                real_ts = struct.unpack('>I', ext)[0]
                if fmt in (1, 2):
                    stream.timestamp_delta = real_ts
                else:
                    stream.timestamp = real_ts

            if fmt in (1, 2):
                stream.timestamp += stream.timestamp_delta

            # Starting a new message for this stream if nothing is
            # already in progress
            if len(stream.partial_payload) == 0:
                bytes_needed = min(self.chunk_size, stream.msg_length)
            else:
                bytes_needed = min(self.chunk_size, stream.msg_length - len(stream.partial_payload))

            piece = _recv_exact(sock, bytes_needed)
            stream.partial_payload += piece

            if len(stream.partial_payload) >= stream.msg_length:
                payload = bytes(stream.partial_payload)
                msg_type = stream.msg_type
                msg_stream_id = stream.msg_stream_id
                stream.partial_payload = bytearray()  # ready for next message on this CSID

                if msg_type == 1 and len(payload) >= 4:  # Set Chunk Size control message
                    new_size = struct.unpack('>I', payload[0:4])[0]
                    if 1 <= new_size <= 0x7FFFFFFF:
                        self.chunk_size = new_size
                    continue  # not returned to caller - handled transparently

                return msg_type, msg_stream_id, payload
            # else: message not yet complete, loop back for the next chunk


# ─────────────────────────────────────────────────────────────────
# Message type / User Control event type constants
# ─────────────────────────────────────────────────────────────────
MSG_TYPE_USER_CONTROL = 4
MSG_TYPE_COMMAND_AMF0 = 20

USER_CONTROL_STREAM_BEGIN = 0
USER_CONTROL_STREAM_EOF = 1
USER_CONTROL_PING_REQUEST = 6
USER_CONTROL_PING_RESPONSE = 7


class RTMPStreamProbe:
    """Watches one RTMP stream URL for a publisher being actively
    connected, without decoding any media. Runs its own background
    thread; safe to read .is_active from any other thread at any time.

    Debounce: matches the same anti-bounce pattern already used
    elsewhere in Lynx for Picotuner offline detection - a state change
    only takes effect after it's held steady for debounce_seconds,
    so a single transient blip (a momentary reconnect, a dropped
    packet) doesn't flip the reported state. Since detection here is
    already event-driven off the protocol's own Stream Begin/EOF
    messages rather than a noisy poll, this matters less than it would
    have for a polling-based approach, but is kept as a safety margin
    against connection-level flapping (e.g. rapid reconnect loops).
    """

    def __init__(self, domain: str, app: str, stream_name: str, rtmp_port: int = 1935,
                 connect_timeout: float = 10.0, monitor_timeout: float = 90.0,
                 reconnect_delay: float = 5.0, debounce_seconds: float = 3.0):
        self.domain = domain
        self.app = app
        self.stream_name = stream_name
        self.rtmp_port = rtmp_port
        self.connect_timeout = connect_timeout
        # Deliberately separate from connect_timeout - this is how long
        # to wait for ANY data at all once already connected and
        # playing, not just for the initial handshake/connect sequence.
        # Needs to comfortably exceed whatever ping interval the real
        # server actually uses (unconfirmed - untested against the real
        # BATC server) or a perfectly healthy stream would be
        # disconnected and reconnected repeatedly, with is_active
        # visibly flickering false each time even though nothing was
        # actually wrong. 90s is a first, conservative guess; worth
        # tuning once real-server behaviour is observed.
        self.monitor_timeout = monitor_timeout
        self.reconnect_delay = reconnect_delay
        self.debounce_seconds = debounce_seconds

        self._lock = threading.Lock()
        self._raw_active = False        # last state actually seen from the protocol, no debounce applied
        self._debounced_active = False  # the public, debounced state
        self._pending_since = None      # monotonic time the raw state last changed, awaiting debounce
        self._last_change = None        # wall-clock time of the last reported (debounced) change
        self._stop_requested = False
        self._thread = None
        self._debounce_thread = None

    @property
    def is_active(self) -> bool:
        with self._lock:
            self._apply_pending_debounce_locked()
            return self._debounced_active

    @property
    def last_change(self):
        with self._lock:
            return self._last_change

    def start(self):
        if self._thread is not None:
            return
        self._stop_requested = False
        self._thread = threading.Thread(target=self._run_loop, daemon=True,
                                         name=f"RTMPStreamProbe-{self.stream_name}")
        self._thread.start()
        # Separate, lightweight timer thread purely for committing a
        # pending debounce change on time - without this, a state
        # transition that held steady for the full debounce window
        # could still be silently lost if a later event arrived and
        # reset the pending timer before anything happened to read
        # is_active in between. Wakes up at a fraction of the debounce
        # window so the commit itself is never more than that fraction
        # late.
        self._debounce_thread = threading.Thread(target=self._debounce_timer_loop, daemon=True,
                                                   name=f"RTMPStreamProbe-{self.stream_name}-debounce")
        self._debounce_thread.start()

    def stop(self):
        self._stop_requested = True
        if self._thread is not None:
            self._thread.join(timeout=self.connect_timeout + 2)
            self._thread = None
        if getattr(self, '_debounce_thread', None) is not None:
            self._debounce_thread.join(timeout=2)
            self._debounce_thread = None

    def _debounce_timer_loop(self):
        poll_interval = max(self.debounce_seconds / 5, 0.05)
        while not self._stop_requested:
            time.sleep(poll_interval)
            with self._lock:
                self._apply_pending_debounce_locked()

    def _apply_pending_debounce_locked(self):
        """Must be called with self._lock already held. Commits a
        pending raw-state change to the public, debounced state once
        it's held steady for debounce_seconds - called both when a new
        raw event arrives AND lazily on every is_active read, since an
        event-driven detector can otherwise go quiet indefinitely after
        a single event with nothing to trigger a time-based re-check."""
        if self._pending_since is not None:
            if (time.monotonic() - self._pending_since) >= self.debounce_seconds:
                if self._debounced_active != self._raw_active:
                    self._debounced_active = self._raw_active
                    self._last_change = time.time()
                self._pending_since = None

    def _set_raw_state(self, active: bool):
        with self._lock:
            if active != self._raw_active:
                self._raw_active = active
                self._pending_since = time.monotonic()
            self._apply_pending_debounce_locked()

    def _run_loop(self):
        while not self._stop_requested:
            try:
                self._connect_and_monitor()
            except (RTMPConnectionClosed, RTMPProtocolError, OSError, socket.timeout) as e:
                print(f"[RTMPStreamProbe:{self.stream_name}] connection lost/failed: {e}")
            self._set_raw_state(False)
            if self._stop_requested:
                break
            time.sleep(self.reconnect_delay)

    def _connect_and_monitor(self):
        sock = socket.create_connection((self.domain, self.rtmp_port), timeout=self.connect_timeout)
        try:
            rtmp_handshake(sock, self.connect_timeout)
            reader = ChunkReader()

            tc_url = f"rtmp://{self.domain}/{self.app}"
            connect_payload = (
                amf0_encode_string("connect") +
                amf0_encode_number(1) +
                amf0_encode_object({"app": self.app, "tcUrl": tc_url, "flashVer": "LNX 9,0,124,2"})
            )
            sock.sendall(build_chunk(csid=3, msg_type=MSG_TYPE_COMMAND_AMF0, msg_stream_id=0, payload=connect_payload))
            self._wait_for_command_result(sock, reader, expected_command="_result", expected_transaction_id=1)

            create_stream_payload = (
                amf0_encode_string("createStream") +
                amf0_encode_number(2) +
                amf0_encode_null()
            )
            sock.sendall(build_chunk(csid=3, msg_type=MSG_TYPE_COMMAND_AMF0, msg_stream_id=0, payload=create_stream_payload))
            result_values = self._wait_for_command_result(sock, reader, expected_command="_result", expected_transaction_id=2)
            if len(result_values) < 4 or not isinstance(result_values[3], (int, float)):
                raise RTMPProtocolError("createStream response missing new stream ID")
            new_stream_id = int(result_values[3])

            play_payload = (
                amf0_encode_string("play") +
                amf0_encode_number(0) +
                amf0_encode_null() +
                amf0_encode_string(self.stream_name)
            )
            sock.sendall(build_chunk(csid=8, msg_type=MSG_TYPE_COMMAND_AMF0, msg_stream_id=new_stream_id, payload=play_payload))

            # From here on, just watch for User Control messages -
            # nothing else needs a response, except PingRequest, which
            # gets echoed back as a PingResponse (standard RTMP client
            # behaviour - some servers may drop clients that never
            # respond to these)
            sock.settimeout(self.monitor_timeout)
            while not self._stop_requested:
                msg_type, msg_stream_id, payload = reader.read_message(sock)
                if msg_type == MSG_TYPE_USER_CONTROL and len(payload) >= 2:
                    event_type = struct.unpack('>H', payload[0:2])[0]
                    if event_type == USER_CONTROL_STREAM_BEGIN:
                        self._set_raw_state(True)
                    elif event_type == USER_CONTROL_STREAM_EOF:
                        self._set_raw_state(False)
                    elif event_type == USER_CONTROL_PING_REQUEST and len(payload) >= 6:
                        timestamp_bytes = payload[2:6]
                        response_payload = struct.pack('>H', USER_CONTROL_PING_RESPONSE) + timestamp_bytes
                        sock.sendall(build_chunk(csid=2, msg_type=MSG_TYPE_USER_CONTROL, msg_stream_id=0, payload=response_payload))
                # command/data/control messages of any other type are
                # deliberately ignored - detection needs nothing from them
        finally:
            sock.close()

    def _wait_for_command_result(self, sock, reader: ChunkReader, expected_command: str, expected_transaction_id, max_messages: int = 20):
        """Reads messages until the expected _result/_error response to
        one of our own commands arrives, ignoring anything else that
        shows up first (the server can interleave other control
        messages). Raises if too many unrelated messages arrive first,
        or if the server responds with _error."""
        for _ in range(max_messages):
            msg_type, msg_stream_id, payload = reader.read_message(sock)
            if msg_type != MSG_TYPE_COMMAND_AMF0:
                continue
            try:
                values = amf0_decode_all(payload)
            except Exception:
                continue
            if len(values) < 2:
                continue
            command_name, transaction_id = values[0], values[1]
            if transaction_id != expected_transaction_id:
                continue
            if command_name == "_error":
                raise RTMPProtocolError(f"Server returned _error for transaction {expected_transaction_id}: {values}")
            if command_name == expected_command:
                return values
        raise RTMPProtocolError(f"Did not receive {expected_command} for transaction {expected_transaction_id} within {max_messages} messages")
