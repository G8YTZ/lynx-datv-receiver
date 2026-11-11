#!/usr/bin/env python3
# Copyright (C) 2026 Justin, G8YTZ / EI3IOB
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""lynx_fcc.py - fast programme change: a relay between the Picotuner and mpv.

Justin's idea (11 Nov 2026). The Picotuner's transport stream passes through
here on its way to mpv, unchanged and without delay. On the way, this keeps,
for every programme in the stream, everything from that programme's most
recent keyframe onwards, and the latest PAT and PMTs.

When Lynx starts mpv (a new lock, or a change of programme), it asks for a
burst: the PAT, every PMT, then the chosen programme's packets from its last
keyframe. mpv so gets a keyframe at once instead of waiting for the next one
(up to 10 s on Lynx-Tx's stills carousel), and sees every programme's table
in its first second, so it always offers every programme.

Single receiver only: in diversity the combiner holds these ports.

    python3 lynx_fcc.py --in-port 9941 --out-port 9947 --control-port 9948

Control (UDP, from 127.0.0.1):
  "HOLD"  stop passing live packets on (Lynx sends it just before restarting
          mpv); everything is still stored. Lifted by the next BURST, or by
          itself after 8 s.
  "BURST <programme number>" (or "BURST" for the first programme): the PAT,
          every PMT, the programme from its last keyframe, then live again,
          continuing exactly where the store ended.

The hold matters: without it, the new mpv got a fraction of a second of live
packets first and the older burst after them, timestamps going backwards in
its first second, and a programme 2 change failed (11 Nov 2026).
"""
import argparse
import select
import socket
import sys
import time

TS = 188
VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24, 0x33}
STORE_MAX = 40000            # packets per programme (7.5 MB): a 10 s GOP of 4K stills fits

ap = argparse.ArgumentParser()
ap.add_argument("--in-port", type=int, required=True)
ap.add_argument("--out-port", type=int, required=True)
ap.add_argument("--out-ip", default="127.0.0.1")
ap.add_argument("--control-port", type=int, required=True)
a = ap.parse_args()


def log(msg):
    print(time.strftime("%H:%M:%S") + " [fcc] " + msg, flush=True)


pat_pkt = None               # the latest packet carrying a PAT section start
pat = {}                     # programme -> PMT PID
pmt_pkt = {}                 # PMT PID -> latest packet
progs = {}                   # programme -> {"pids": set, "video": pid}
pid_prog = {}                # elementary/PCR PID -> programme
store = {}                   # programme -> [packets since its last keyframe]


def parse_section(pid, p, i):
    """A PSI section starting in this packet (payload_unit_start)."""
    global pat_pkt
    if i >= TS:
        return
    sec = p[i + 1 + p[i]:]
    if len(sec) < 8:
        return
    tid = sec[0]
    length = ((sec[1] & 0x0F) << 8) | sec[2]
    body = sec[3:3 + length]
    if pid == 0 and tid == 0x00:
        pat_pkt = p
        new = {}
        for j in range(5, len(body) - 4, 4):
            prog = (body[j] << 8) | body[j + 1]
            if prog:
                new[prog] = ((body[j + 2] & 0x1F) << 8) | body[j + 3]
        if new != pat:
            pat.clear()
            pat.update(new)
            log("PAT: programmes " + ", ".join(f"{k} (PMT 0x{v:04x})" for k, v in sorted(pat.items())))
    elif tid == 0x02 and pid in pat.values():
        pmt_pkt[pid] = p
        prog = (body[0] << 8) | body[1]
        pcr = ((body[5] & 0x1F) << 8) | body[6]
        info = ((body[7] & 0x0F) << 8) | body[8]
        j, pids, video, vtype = 9 + info, {pcr}, None, None
        while j + 5 <= len(body) - 4:
            st = body[j]
            spid = ((body[j + 1] & 0x1F) << 8) | body[j + 2]
            es = ((body[j + 3] & 0x0F) << 8) | body[j + 4]
            pids.add(spid)
            if video is None and st in VIDEO_TYPES:
                video, vtype = spid, st
            j += 5 + es
        old = progs.get(prog)
        if not old or old["pids"] != pids or old["video"] != video:
            progs[prog] = {"pids": pids, "video": video, "vtype": vtype, "params_seen": False}
            for k in [k for k, v in pid_prog.items() if v == prog]:
                del pid_prog[k]
            for x in pids:
                pid_prog[x] = prog
            store.setdefault(prog, [])
            log(f"programme {prog}: picture on 0x{(video or 0):04x}, PIDs "
                + ", ".join(f"0x{x:04x}" for x in sorted(pids)))


def starts_with_params(p, i, vtype):
    """Does this PES start (payload_unit_start) carry the codec's parameter
    sets, i.e. is it a keyframe a decoder can start from? HEVC: VPS or SPS
    (NAL types 32, 33); H.264 and MPEG-2: SPS (7) or a sequence header.

    The random access flag alone was not enough: a burst starting at it
    still gave "PPS id out of range" on Lynx-Tx's carousel and failed one
    change in three (11 Nov 2026), so the store now starts where the
    parameter sets are."""
    pl = p[i:]
    if len(pl) < 9 or pl[0:3] != b"\x00\x00\x01":
        return False
    data = pl[9 + pl[8]:]
    k = data.find(b"\x00\x00\x01")
    while 0 <= k < len(data) - 3:
        b = data[k + 3]
        if vtype in (0x24, 0x33):                  # HEVC
            if (b >> 1) & 0x3F in (32, 33):
                return True
        elif vtype == 0x1B:                        # H.264
            if b & 0x1F == 7:
                return True
        elif vtype in (0x01, 0x02):                # MPEG-1/2 sequence header
            if b == 0xB3:
                return True
        k = data.find(b"\x00\x00\x01", k + 3)
    return False


def take(p):
    pid = ((p[1] & 0x1F) << 8) | p[2]
    afc = (p[3] >> 4) & 3
    i = 4
    rai = False
    if afc in (2, 3):
        alen = p[4]
        if alen and p[5] & 0x40:
            rai = True
        i = 5 + alen
    if afc in (1, 3) and p[1] & 0x40 and (pid == 0 or pid in pat.values()):
        parse_section(pid, p, i)
        return
    prog = pid_prog.get(pid)
    if prog is None:
        return
    st = store[prog]
    pr = progs[prog]
    if pid == pr["video"] and p[1] & 0x40 and afc in (1, 3) \
            and starts_with_params(p, i, pr["vtype"]):
        pr["params_seen"] = True
        st.clear()              # a keyframe with its parameter sets: start again here
    elif rai and pid == pr["video"] and not pr["params_seen"]:
        st.clear()              # no parameter sets found yet: the flag is the best guess
    st.append(p)
    if len(st) > STORE_MAX:     # no keyframe for a long time: keep the newest
        del st[:len(st) - STORE_MAX]


HOLD_MAX = 8.0
held_since = None            # set while live packets are held back
held_live = []               # live datagrams received while held: sent after the burst


def burst(out, prog):
    if prog is None:
        prog = min(progs) if progs else None
    pkts = []
    if pat_pkt:
        pkts.append(pat_pkt)
    pkts += [pmt_pkt[x] for x in sorted(pmt_pkt)]
    held = list(store.get(prog, [])) if prog is not None else []
    pkts += held
    for n in range(0, len(pkts), 7):
        out.sendto(b"".join(pkts[n:n + 7]), (a.out_ip, a.out_port))
        if n % 140 == 0:
            time.sleep(0.001)   # about 25 MB/s: within what the receiving socket takes
    log(f"burst for programme {prog}: {len(held)} packets from its last keyframe "
        f"({len(held) * TS // 1024} kB)")


def release(out):
    """Live again. The store already holds every held packet of the chosen
    programme, so only datagrams that arrived after the burst was built are
    owed; simplest and safe is to drop the held queue (the store covered
    it) and carry on live from the next datagram."""
    global held_since
    held_since = None
    held_live.clear()


def main():
    global held_since
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    rx.bind(("", a.in_port))
    ctl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctl.bind(("127.0.0.1", a.control_port))
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    log(f"relaying UDP {a.in_port} -> {a.out_ip}:{a.out_port}, control on {a.control_port}")
    while True:
        r, _, _ = select.select([rx, ctl], [], [], 1.0)
        if ctl in r:
            msg = ctl.recv(256).decode(errors="replace").split()
            if msg and msg[0] == "HOLD":
                held_since = time.time()
            elif msg and msg[0] == "BURST":
                try:
                    burst(out, int(msg[1]) if len(msg) > 1 else None)
                except Exception as e:
                    log(f"burst failed: {e}")
                release(out)
        if held_since is not None and time.time() - held_since > HOLD_MAX:
            log("held for 8 s with no burst asked for: live again")
            release(out)
        if rx in r:
            d = rx.recv(65535)
            if held_since is None:
                out.sendto(d, (a.out_ip, a.out_port))
            for n in range(0, len(d) - TS + 1, TS):
                p = d[n:n + TS]
                if p[0] == 0x47:
                    take(p)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
