#!/usr/bin/env python3
"""
patch_relay_ts_align.py

Re-aligns a Slave's video to transport stream packet boundaries before
it reaches mpv.

THE PROBLEM, FROM LONGMYND'S OWN SOURCE
---------------------------------------
udp.c sends the transport stream in 510-byte chunks, and its comment
says why:

    we need to loop round sending 510 byte chunks so that we can skip
    the 2 extra bytes put in by the FTDI chip every 512 bytes of USB
    message

That is correct as far as it goes — the FTDI framing has to come out.
But 510 is not a multiple of 188, so every datagram boundary lands in
the middle of a TS packet. The stream is valid read end to end; it is
simply never aligned.

A Picotuner sends 1316 bytes, exactly 7 x 188, with the sync byte
first. mpv handles that comfortably. Given 510-byte fragments it must
find sync itself and has no margin left for anything else, which is why
a Slave would play but freeze while a local tuner on the same signal
did not.

WHAT THIS DOES
--------------
Accumulates incoming bytes per source, finds the 0x47 sync byte, and
emits whole 188-byte packets in groups of seven — the same shape a
Picotuner sends. Nothing is dropped except bytes before the first sync,
which cannot be part of a whole packet anyway.

Done here rather than at the Slave because the relay already sees every
Slave's traffic: one fix covers every Slave, including ones running
Longmynd builds nobody has touched.

Alignment applies only to the selected source, since it is only for
mpv's benefit. Counters still see every packet from every Slave, so
video-present detection is unaffected.

Run from the repo root:  python3 patch_relay_ts_align.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_relay.py")


def apply(src: str, old: str, new: str, label: str) -> str:
    n = src.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected exactly 1")
    print(f"  ok  {label}")
    return src.replace(old, new, 1)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")

    src = TARGET.read_text()
    before = len(src.splitlines())

    if "TS_PACKET_SIZE" in src:
        sys.exit("ABORT: already patched")

    # ── 1. constants ─────────────────────────────────────────────
    src = apply(
        src,
        "# Max TS datagram we expect (7 x 188 = 1316, plus headroom).\nRECV_SIZE = 2048",
        '''# Transport stream packet size, and the sync byte every one starts
# with. Not negotiable — this is the MPEG-2 TS format itself.
TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47

# Seven packets per datagram: 7 x 188 = 1316, which fits inside a
# normal 1500-byte MTU and is what a Picotuner sends. Matching it means
# mpv sees the same shape of stream whatever the source.
TS_PACKETS_PER_DATAGRAM = 7
TS_DATAGRAM_SIZE = TS_PACKET_SIZE * TS_PACKETS_PER_DATAGRAM

# If this much arrives without a sync byte ever being found, the buffer
# is discarded rather than grown forever. A source that never syncs is
# not a transport stream, and holding its bytes helps nobody.
TS_MAX_RESYNC_BYTES = TS_PACKET_SIZE * 16

# Max TS datagram we expect (7 x 188 = 1316, plus headroom).
RECV_SIZE = 2048''',
        "TS constants",
    )

    # ── 2. per-source buffer ─────────────────────────────────────
    src = apply(
        src,
        "        self._unknown = {}        # ip -> packet count, for diagnosis only",
        "        self._unknown = {}        # ip -> packet count, for diagnosis only\n"
        "        # Bytes received from the selected source that have not yet\n"
        "        # made up a whole group of TS packets. Only the selected one\n"
        "        # is buffered: alignment exists for mpv's benefit, and\n"
        "        # nothing else is being sent anywhere.\n"
        "        self._align_buf = bytearray()",
        "alignment buffer",
    )

    # ── 3. reset it on selection change ──────────────────────────
    src = apply(
        src,
        "        with self._lock:\n"
        "            self._selected = slave_id\n"
        "            self._selected_ip = self._resolve_locked(slave_id)",
        "        with self._lock:\n"
        "            self._selected = slave_id\n"
        "            self._selected_ip = self._resolve_locked(slave_id)\n"
        "            # Dropped on every switch: leftover bytes belong to the\n"
        "            # previous source and splicing them onto the next one\n"
        "            # would hand mpv a packet made of two streams.\n"
        "            self._align_buf.clear()",
        "clear buffer on select",
    )

    # ── 4. align before forwarding ───────────────────────────────
    src = apply(
        src,
        "            if forward:\n"
        "                try:\n"
        "                    out.sendto(data, self.mpv_addr)\n"
        "                except OSError as exc:\n"
        "                    log.debug(\"forward to mpv failed: %s\", exc)",
        "            if forward:\n"
        "                for datagram in self._align(data):\n"
        "                    try:\n"
        "                        out.sendto(datagram, self.mpv_addr)\n"
        "                    except OSError as exc:\n"
        "                        log.debug(\"forward to mpv failed: %s\", exc)",
        "forward aligned datagrams",
    )

    # ── 5. the aligner ───────────────────────────────────────────
    src = apply(
        src,
        "    def _advance(self, next_sample):",
        '''    def _align(self, data):
        """Turn arbitrary-length chunks into whole, sync-aligned TS
        datagrams.

        Longmynd sends 510-byte pieces because that is what is left
        after stripping the FTDI chip's framing, and 510 is not a
        multiple of 188 — so every datagram it sends straddles TS packet
        boundaries. A Picotuner sends 1316 bytes starting on a sync
        byte. This makes the first look like the second.

        Yields complete datagrams; incomplete tails stay buffered for
        the next chunk rather than being sent short.
        """
        buf = self._align_buf
        buf.extend(data)

        # Find the first sync byte. Anything before it is a fragment of
        # a packet whose beginning was never seen, so it cannot be
        # reconstructed and is dropped rather than passed on.
        if not buf or buf[0] != TS_SYNC_BYTE:
            idx = buf.find(TS_SYNC_BYTE)
            if idx < 0:
                # Nothing usable. Keep a bounded tail in case a sync
                # byte is the very next thing to arrive.
                if len(buf) > TS_MAX_RESYNC_BYTES:
                    del buf[:-TS_PACKET_SIZE]
                return
            del buf[:idx]

        while len(buf) >= TS_DATAGRAM_SIZE:
            # Every packet in the group is checked before any of it is
            # sent. Checking only the first would let a byte lost
            # upstream put six misaligned packets into mpv before
            # anything noticed — measured, not hypothetical.
            first_bad = None
            for off in range(0, TS_DATAGRAM_SIZE, TS_PACKET_SIZE):
                if buf[off] != TS_SYNC_BYTE:
                    first_bad = off
                    break

            if first_bad is None:
                yield bytes(buf[:TS_DATAGRAM_SIZE])
                del buf[:TS_DATAGRAM_SIZE]
                continue

            # Sync was lost partway through. Emit the whole packets that
            # came before it, then hunt for the next sync byte and carry
            # on from there rather than sending anything doubtful.
            if first_bad >= TS_PACKET_SIZE:
                yield bytes(buf[:first_bad])
            del buf[:first_bad + 1]
            idx = buf.find(TS_SYNC_BYTE)
            if idx < 0:
                buf.clear()
                return
            del buf[:idx]

    def _advance(self, next_sample):''',
        "_align()",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_align"))
    TARGET.write_text(src)

    after = len(src.splitlines())
    print(f"\n  lynx_relay.py: {before} -> {after} lines (+{after - before})")
    print("\nNow: python3 -m py_compile lynx_relay.py")


if __name__ == "__main__":
    main()
