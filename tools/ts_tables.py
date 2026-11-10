#!/usr/bin/env python3
# Copyright (C) 2026 Justin, G8YTZ / EI3IOB
# GPL-3.0-or-later, as the rest of Lynx.
"""ts_tables.py - what is in a transport stream, and how often.

Listens on a UDP port (or reads a .ts file) for a few seconds and reports:
every programme the PAT lists, each programme's PMT (how often it comes),
its PIDs, which carry PCR, and when each video PID first has a random
access point (a keyframe, as flagged in the adaptation field).

Written 10 Nov 2026 to find out why mpv on a ROCK 5C listed only the first
of Lynx-Tx's two programmes.

    python3 tools/ts_tables.py --port 9941 --seconds 30
    python3 tools/ts_tables.py --file capture.ts
"""
import argparse
import socket
import sys
import time
from collections import defaultdict

ap = argparse.ArgumentParser()
ap.add_argument("--port", type=int, default=9941)
ap.add_argument("--seconds", type=float, default=30)
ap.add_argument("--file")
a = ap.parse_args()

counts = defaultdict(int)
seen_at = defaultdict(list)       # PID -> times a table section started
pat = {}                          # programme number -> PMT PID
pmt = {}                          # PMT PID -> (programme, PCR PID, [(type, PID)])
pcr_pids = set()
first_rap = {}                    # PID -> time of first random access point
t0 = None


def section(pid, payload, now):
    if not payload or payload[0] > len(payload) - 1:
        return
    sec = payload[1 + payload[0]:]
    if len(sec) < 8:
        return
    tid = sec[0]
    length = ((sec[1] & 0x0F) << 8) | sec[2]
    body = sec[3:3 + length]
    if pid == 0 and tid == 0x00:
        seen_at[0].append(now)
        for i in range(5, len(body) - 4, 4):
            prog = (body[i] << 8) | body[i + 1]
            ppid = ((body[i + 2] & 0x1F) << 8) | body[i + 3]
            if prog:
                pat[prog] = ppid
    elif tid == 0x02 and pid in pat.values():
        seen_at[pid].append(now)
        prog = (body[0] << 8) | body[1]
        pcr = ((body[5] & 0x1F) << 8) | body[6]
        info = ((body[7] & 0x0F) << 8) | body[8]
        i, streams = 9 + info, []
        while i + 5 <= len(body) - 4:
            st = body[i]
            spid = ((body[i + 1] & 0x1F) << 8) | body[i + 2]
            es = ((body[i + 3] & 0x0F) << 8) | body[i + 4]
            streams.append((st, spid))
            i += 5 + es
        pmt[pid] = (prog, pcr, streams)


def packet(p, now):
    if len(p) != 188 or p[0] != 0x47:
        return
    pid = ((p[1] & 0x1F) << 8) | p[2]
    counts[pid] += 1
    afc = (p[3] >> 4) & 3
    i = 4
    if afc in (2, 3):
        alen = p[4]
        if alen and len(p) > 5:
            flags = p[5]
            if flags & 0x10:
                pcr_pids.add(pid)
            if flags & 0x40 and pid not in first_rap:
                first_rap[pid] = now
        i = 5 + alen
    if afc in (1, 3) and p[1] & 0x40 and (pid == 0 or pid in pat.values()):
        section(pid, p[i:], now)


if a.file:
    data = open(a.file, "rb").read()
    for n in range(0, len(data) - 187, 188):
        packet(data[n:n + 188], n / 188.0)   # "time" in packets for a file
else:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", a.port))
    s.settimeout(1)
    end = time.time() + a.seconds
    print(f"Listening on UDP {a.port} for {a.seconds:.0f} s...", file=sys.stderr)
    while time.time() < end:
        try:
            d = s.recv(65535)
        except socket.timeout:
            continue
        now = time.time()
        t0 = t0 or now
        for n in range(0, len(d) - 187, 188):
            packet(d[n:n + 188], now - t0)

total = sum(counts.values())
print(f"{total} packets")
if not total:
    sys.exit("Nothing received: is the port right, and is something else holding it?")


def every(times):
    if len(times) < 2:
        return "once" if times else "never"
    gaps = [b - a for a, b in zip(times, times[1:])]
    return f"{len(times)} times, every {sum(gaps)/len(gaps):.2f} s (longest gap {max(gaps):.2f} s)"


print(f"PAT: {every(seen_at[0])}")
for prog, ppid in sorted(pat.items()):
    print(f"Programme {prog}: PMT on PID 0x{ppid:04x}, {every(seen_at[ppid])}")
    if ppid in pmt:
        _, pcr, streams = pmt[ppid]
        print(f"   PCR on 0x{pcr:04x}{'' if pcr in pcr_pids else ' (never seen carrying PCR)'}")
        for st, spid in streams:
            rap = first_rap.get(spid)
            print(f"   stream type 0x{st:02x} PID 0x{spid:04x}: {counts[spid]} packets"
                  + (f", first keyframe at {rap:.1f} s" if rap is not None else ", no keyframe seen"))
    else:
        print("   its PMT never arrived")
print("PIDs: " + ", ".join(f"0x{p:04x}={n}" for p, n in sorted(counts.items())))
