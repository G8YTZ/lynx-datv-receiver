"""
lynx_relay.py — single-port Slave video ingress and demux.

All Slaves send MPEG-TS to one well-known UDP port on the Lynx master
(default 10998). This module binds that port once, identifies each
datagram by its source address, counts packets per source, and forwards
only the currently selected source to mpv on 127.0.0.1:9941.

No ffmpeg, no per-Slave ports, no allocation protocol. Slave identity
comes from the status feed on 10997: whatever address is sending
SITE/status lines for a Slave is the address whose video belongs to it.

Integration contract with lynx_app.py:

    relay = SlaveVideoRelay()
    relay.start()

    # Whenever the Slave monitor threads learn or lose a Slave:
    relay.set_sources({"192.168.0.51": "slave1", "192.168.0.52": "slave2"})

    # When the plug dropdown selects a Slave (or moves away from one):
    relay.select("slave1")      # forward that Slave to mpv
    relay.select(None)          # stop forwarding (Plug A/B/Stream selected)

    # For /api/status:
    relay.stats()               # {"slave1": {"live": True, "kbps": 3980, ...}}

    relay.stop()
"""

import logging
import socket
import threading
import time

log = logging.getLogger("lynx.relay")

# Single ingress port every Slave sends video to.
INGRESS_PORT = 10998

# mpv sits on 9941 permanently, bound to all interfaces (udp://@:9941).
MPV_HOST = "127.0.0.1"
MPV_PORT = 9941

# 4 MB receive buffer: a burst from several Slaves at once must not be
# dropped by the kernel while the loop is between syscalls.
RCVBUF_BYTES = 4 * 1024 * 1024

# A source is "live" if it has sent anything within this many seconds.
LIVE_TIMEOUT = 2.0

# Stats are recomputed on this cadence.
SAMPLE_INTERVAL = 1.0

# Cap on unknown senders tracked, so a spray can't grow the dict forever.
MAX_UNKNOWN = 32

# Max TS datagram we expect (7 x 188 = 1316, plus headroom).
RECV_SIZE = 2048


class _SourceStats:
    """Rolling counters for one video source."""

    __slots__ = ("packets", "bytes", "last_seen", "_last_bytes", "_last_packets", "kbps", "pps")

    def __init__(self):
        self.packets = 0
        self.bytes = 0
        self.last_seen = 0.0
        self._last_bytes = 0
        self._last_packets = 0
        self.kbps = 0
        self.pps = 0

    def sample(self, interval):
        d_bytes = self.bytes - self._last_bytes
        d_packets = self.packets - self._last_packets
        self._last_bytes = self.bytes
        self._last_packets = self.packets
        if interval > 0:
            self.kbps = int((d_bytes * 8) / interval / 1000)
            self.pps = int(d_packets / interval)
        else:
            self.kbps = 0
            self.pps = 0


class SlaveVideoRelay:
    """Binds one UDP ingress port, demuxes by source address, forwards one."""

    def __init__(self, ingress_port=INGRESS_PORT, mpv_host=MPV_HOST, mpv_port=MPV_PORT):
        self.ingress_port = ingress_port
        self.mpv_addr = (mpv_host, mpv_port)

        self._sock = None
        self._out = None
        self._thread = None
        self._running = False

        # Guards _sources, _selected, _selected_ip, _stats, _unknown.
        self._lock = threading.Lock()

        self._sources = {}        # ip -> slave_id  (the whitelist)
        self._selected = None     # slave_id currently routed to mpv
        self._selected_ip = None  # resolved ip for the selected slave_id
        self._stats = {}          # slave_id -> _SourceStats
        self._unknown = {}        # ip -> packet count, for diagnosis only

        self.bind_error = None    # human-readable reason if the bind failed

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Bind the ingress port and start the demux thread.

        Returns True on success. On failure bind_error carries the reason
        and every Slave panel should show red with that text, rather than
        the amber 'reachable but receiving nothing' state — the port never
        came up, so 'receiving nothing' would be misleading.
        """
        if self._running:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Deliberately NOT SO_REUSEADDR / SO_REUSEPORT. On UDP those let a
            # second process bind the same port and silently share the packet
            # stream — the exact failure mode as the stray 'nc -lu' that stole
            # Picotuner status packets. We want the second binder to fail loudly
            # with EADDRINUSE so bind_error can be shown on the panel.
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF_BYTES)
            except OSError:
                log.warning("could not raise SO_RCVBUF; kernel default in use")
            sock.bind(("0.0.0.0", self.ingress_port))
            sock.settimeout(0.5)
        except OSError as exc:
            self.bind_error = "port %d: %s" % (self.ingress_port, exc)
            log.error("relay bind failed — %s", self.bind_error)
            return False

        actual = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
        log.info("relay listening on %d (SO_RCVBUF=%d)", self.ingress_port, actual)

        self._sock = sock
        self._out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.bind_error = None
        self._running = True
        self._thread = threading.Thread(target=self._run, name="slave-video-relay", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        for sock in (self._sock, self._out):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        self._sock = None
        self._out = None

    # ------------------------------------------------------------------
    # Control surface, called from the status monitor and the plug switch
    # ------------------------------------------------------------------

    def set_sources(self, mapping):
        """Replace the address -> slave_id whitelist.

        Call this whenever the Slave monitor threads learn an address, lose
        one, or see it change (DHCP). Video follows status automatically:
        a Slave with no live status record has no entry here, so its packets
        are counted as unknown and dropped rather than reaching mpv.
        """
        with self._lock:
            self._sources = dict(mapping)
            for slave_id in self._sources.values():
                self._stats.setdefault(slave_id, _SourceStats())
            for slave_id in list(self._stats):
                if slave_id not in self._sources.values():
                    del self._stats[slave_id]
            self._selected_ip = self._resolve_locked(self._selected)

    def select(self, slave_id):
        """Route one Slave's video to mpv, or None to stop forwarding.

        Switching is a variable assignment — no process is spawned or killed,
        so the plug dropdown responds immediately. mpv will see a PID and PCR
        discontinuity at the switch, which is the re-acquire watchdog's job.
        """
        with self._lock:
            self._selected = slave_id
            self._selected_ip = self._resolve_locked(slave_id)
        log.info("relay selection -> %s", slave_id or "(none)")

    def _resolve_locked(self, slave_id):
        if slave_id is None:
            return None
        for ip, sid in self._sources.items():
            if sid == slave_id:
                return ip
        return None

    def selected(self):
        with self._lock:
            return self._selected

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def stats(self):
        """Per-Slave video presence, for /api/status and the plug dropdown.

        'live' here means video is arriving, which is deliberately independent
        of whether the Slave is heartbeating: a Slave can report happily while
        its receiver is unlocked and no TS is flowing.
        """
        now = time.time()
        with self._lock:
            out = {}
            for slave_id, st in self._stats.items():
                out[slave_id] = {
                    "live": (now - st.last_seen) < LIVE_TIMEOUT if st.last_seen else False,
                    "kbps": st.kbps,
                    "pps": st.pps,
                    "packets": st.packets,
                    "bytes": st.bytes,
                    "last_seen": st.last_seen or None,
                    "selected": slave_id == self._selected,
                }
            return {
                "port": self.ingress_port,
                "running": self._running,
                "bind_error": self.bind_error,
                "selected": self._selected,
                "sources": out,
                "unknown": dict(self._unknown),
            }

    # ------------------------------------------------------------------
    # Demux loop
    # ------------------------------------------------------------------

    def _run(self):
        sock = self._sock
        out = self._out
        next_sample = time.time() + SAMPLE_INTERVAL

        while self._running:
            try:
                data, addr = sock.recvfrom(RECV_SIZE)
            except socket.timeout:
                self._maybe_sample(next_sample)
                next_sample = self._advance(next_sample)
                continue
            except OSError as exc:
                if self._running:
                    log.error("relay recv failed: %s", exc)
                break

            ip = addr[0]
            now = time.time()

            # Hot path: read the small amount of shared state under the lock,
            # then do the sendto outside it so a slow socket can't block the
            # plug switch or the status monitor.
            with self._lock:
                slave_id = self._sources.get(ip)
                forward = (ip == self._selected_ip)
                if slave_id is not None:
                    st = self._stats.get(slave_id)
                    if st is None:
                        st = self._stats[slave_id] = _SourceStats()
                    st.packets += 1
                    st.bytes += len(data)
                    st.last_seen = now
                elif len(self._unknown) < MAX_UNKNOWN or ip in self._unknown:
                    self._unknown[ip] = self._unknown.get(ip, 0) + 1

            if forward:
                try:
                    out.sendto(data, self.mpv_addr)
                except OSError as exc:
                    log.debug("forward to mpv failed: %s", exc)

            if now >= next_sample:
                self._maybe_sample(next_sample)
                next_sample = self._advance(next_sample)

        log.info("relay thread exiting")

    def _advance(self, next_sample):
        now = time.time()
        while next_sample <= now:
            next_sample += SAMPLE_INTERVAL
        return next_sample

    def _maybe_sample(self, due):
        with self._lock:
            for st in self._stats.values():
                st.sample(SAMPLE_INTERVAL)


if __name__ == "__main__":
    # Standalone smoke test: bind, accept from one hard-coded source, print rates.
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    relay = SlaveVideoRelay()
    if not relay.start():
        sys.exit("bind failed: %s" % relay.bind_error)
    if len(sys.argv) > 1:
        relay.set_sources({sys.argv[1]: "test"})
        relay.select("test")
        print("forwarding %s -> mpv" % sys.argv[1])
    else:
        print("no source given; counting unknown senders only")
    try:
        while True:
            time.sleep(2)
            print(relay.stats())
    except KeyboardInterrupt:
        relay.stop()
