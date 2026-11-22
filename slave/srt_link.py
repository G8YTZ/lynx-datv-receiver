"""
SRT transport for a Slave Rx: line the stream up, then call the repeater.

    Longmynd --UDP 510-byte pieces--> TsAligner --UDP 1316--> srt-live-transmit --SRT--> Lynx

WHY ALIGN HERE
--------------
Longmynd sends 510-byte pieces, which is what is left of the FTDI chip's
framing, and 510 is not a multiple of 188, so every datagram straddles TS
packets. SRT carries whatever it is given in 1316-byte chunks, so a
misaligned stream would arrive misaligned. Doing it on the Slave means
the repeater receives the same clean 7 x 188 datagrams a Picotuner sends,
and its own _align() becomes a safety net rather than a necessity.

WHY THE SLAVE CALLS
-------------------
A Slave on somebody's home broadband can always call out; nobody can
call in without port forwarding at every site. The repeater listens, one
port per Slave, and that is the only place a port needs opening.

WHY A SUPERVISED srt-live-transmit
----------------------------------
There is no maintained Python binding for libsrt. srt-live-transmit is
the reference tool, small and already on the Zero (Debian trixie,
srt-tools). It is restarted whenever it exits, so a dropped link or a
repeater restart heals by itself: the Slave is the end nobody can reach.
"""

import socket
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import quote

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47
TS_DATAGRAM_SIZE = TS_PACKET_SIZE * 7          # 1316, as a Picotuner sends
TS_MAX_RESYNC_BYTES = TS_PACKET_SIZE * 16

# Local hop: Longmynd sends here, the aligner forwards to the next port,
# srt-live-transmit reads that. Loopback only, never seen on the network.
LONGMYND_LOCAL_PORT = 15998
ALIGNED_LOCAL_PORT = 15999

# The repeater listens on the Slave's status port minus this, derived
# the same way at both ends, as the quality port is.
SRT_PORT_OFFSET = 50

RESTART_DELAY_SECS = 2.0


class TsAligner:
    """Turns arbitrary-length chunks into whole, sync-aligned 1316-byte
    datagrams. The same rules as the repeater's lynx_relay._align():
    every packet in a group is checked before any of it is sent, and
    anything doubtful is dropped rather than passed on."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        buf = self.buf
        buf.extend(data)
        if not buf or buf[0] != TS_SYNC_BYTE:
            idx = buf.find(TS_SYNC_BYTE)
            if idx < 0:
                if len(buf) > TS_MAX_RESYNC_BYTES:
                    del buf[:-TS_PACKET_SIZE]
                return
            del buf[:idx]
        while len(buf) >= TS_DATAGRAM_SIZE:
            first_bad = None
            for off in range(0, TS_DATAGRAM_SIZE, TS_PACKET_SIZE):
                if buf[off] != TS_SYNC_BYTE:
                    first_bad = off
                    break
            if first_bad is None:
                yield bytes(buf[:TS_DATAGRAM_SIZE])
                del buf[:TS_DATAGRAM_SIZE]
                continue
            if first_bad >= TS_PACKET_SIZE:
                yield bytes(buf[:first_bad])
            del buf[:first_bad + 1]
            idx = buf.find(TS_SYNC_BYTE)
            if idx < 0:
                buf.clear()
                return
            del buf[:idx]


class AlignerThread(threading.Thread):
    """Receives Longmynd's pieces on loopback and forwards aligned groups."""

    def __init__(self, stop):
        super().__init__(daemon=True, name="ts-aligner")
        self.stop = stop
        self.datagrams = 0

    def run(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        rx.bind(("127.0.0.1", LONGMYND_LOCAL_PORT))
        rx.settimeout(0.5)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        out = ("127.0.0.1", ALIGNED_LOCAL_PORT)
        aligner = TsAligner()
        while not self.stop.is_set():
            try:
                data = rx.recv(4096)
            except socket.timeout:
                continue
            except OSError as e:
                print(f"[srt] aligner receive failed: {e}", file=sys.stderr)
                time.sleep(0.5)
                continue
            for dg in aligner.feed(data):
                try:
                    tx.sendto(dg, out)
                    self.datagrams += 1
                except OSError:
                    pass   # nothing listening yet: srt-live-transmit restarting


def srt_target(host, port, latency_ms, streamid):
    """The caller URI. streamid carries the Slave's name for the
    repeater's logs; the port is what identifies the Slave."""
    q = f"mode=caller&latency={int(latency_ms)}"
    if streamid:
        q += "&streamid=" + quote(streamid, safe="")
    return f"srt://{host}:{port}?{q}"


class SrtSender(threading.Thread):
    """Keeps srt-live-transmit running, restarting it whenever it exits."""

    def __init__(self, host, port, latency_ms, streamid, stop):
        super().__init__(daemon=True, name="srt-sender")
        self.cmd = ["srt-live-transmit",
                    f"udp://127.0.0.1:{ALIGNED_LOCAL_PORT}",
                    srt_target(host, port, latency_ms, streamid)]
        self.stop = stop
        self.proc = None
        self.starts = 0

    def run(self):
        while not self.stop.is_set():
            self.starts += 1
            print(f"[srt] calling {self.cmd[2]}")
            try:
                # Its messages are read here and passed to the journal:
                # connect, disconnect and errors are exactly what a remote
                # fault report needs. Line-buffered so we see them at once.
                cmd = self.cmd
                if shutil.which("stdbuf"):
                    cmd = ["stdbuf", "-oL", "-eL"] + cmd
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT,
                                             text=True, bufsize=1)
                for line in self.proc.stdout:
                    line = line.rstrip()
                    if line:
                        print(line, flush=True)
                    # srt-live-transmit does not call again once the far end
                    # has gone (a reboot of the receiver, a broken link): it
                    # carries on reading UDP and throwing it away for ever.
                    # So a disconnect means start a fresh caller.
                    if "disconnected" in line.lower():
                        print("[srt] receiver went away - calling again",
                              flush=True)
                        self.proc.terminate()
                        break
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
                rc = self.proc.returncode
            except FileNotFoundError:
                print("[srt] srt-live-transmit not found: sudo apt install srt-tools",
                      file=sys.stderr)
                rc = None
            if self.stop.is_set():
                break
            print(f"[srt] srt-live-transmit exited (rc={rc}) - "
                  f"calling again in {RESTART_DELAY_SECS:.0f}s", flush=True)
            time.sleep(RESTART_DELAY_SECS)

    def terminate(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
