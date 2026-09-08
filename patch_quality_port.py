#!/usr/bin/env python3
"""
patch_quality_port.py

Adds the quality port to longmynd_source.py: the full field set Lynx's
per-Slave quality monitor listens for, on the status port minus 96.

    $9  symbol rate   $12 MER        $14 programme   $18 modcod
    $30 margin        $85 dBm        $26 AGC1        $27 AGC2

UNITS ARE NOT PASS-THROUGH
--------------------------
Three of these needed converting, because Longmynd and Lynx do not
agree on units and Lynx appends the unit when it renders:

    $9   Longmynd sends symbols/s; Lynx renders "<value> kS/s", so
         raw pass-through would read "333000 kS/s".
    $12  Longmynd sends dB x 10; Lynx renders "<value> dB", so raw
         would read "102 dB". TunerState already divides by ten for
         its own use — that conversion just never reached the wire.
    $18  Longmynd sends an integer; Lynx renders it as text under
         "Mode", so raw would read "4" rather than "QPSK 1/2".

$14 alone is genuinely direct.

MARGIN
------
MER minus the modcod's own threshold, from the DVB-S2 table below.
QPSK 1/2 has a threshold of 1.0 dB, so a MER of 10.2 gives a margin of
9.2 — the ON4VVV reading this was validated against.

DVB-S, NOT DVB-S2
-----------------
Longmynd reports $18 only for DVB-S2 (state 4). In DVB-S (state 3) the
code rate arrives on $3 instead, with different thresholds. Rather than
guess, modcod and margin are simply not emitted in DVB-S: absent is
honest, and a wrong margin on a marginal signal is worse than none.

WHAT IS SENT WHEN
-----------------
AGC and dBm always, because they are true whether or not anything is
locked and are exactly what you want while pointing a dish. MER, margin,
modcod, symbol rate and programme only while locked, since unlocked they
describe a signal that is not being received.

Run from the repo root:  python3 patch_quality_port.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("longmynd_source.py")


def apply(src: str, old: str, new: str, label: str) -> str:
    n = src.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected exactly 1")
    print(f"  ok  {label}")
    return src.replace(old, new, 1)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")
    if not Path("agc_table.py").exists():
        sys.exit("ABORT: agc_table.py not beside longmynd_source.py")

    src = TARGET.read_text()
    before = len(src.splitlines())

    if "build_quality_lines" in src:
        sys.exit("ABORT: already patched")

    # ── 1. import the AGC table ──────────────────────────────────
    src = apply(
        src,
        "import threading\nimport time\n",
        "import threading\nimport time\n\nimport agc_table\n",
        "import agc_table",
    )

    # ── 2. quality port offset + modcod table ────────────────────
    src = apply(
        src,
        "# Longmynd's own default, and the file it insists on.",
        '''# The quality port sits this far below the status port, matching the
# relationship Lynx's own Picotuner uses between 9997 and 9901. Derived
# at both ends from one configured number rather than being a second
# number to keep in step.
QUALITY_PORT_OFFSET = 96

# DVB-S2 modcod -> (name, MER threshold in dB). The name is what Lynx
# displays under "Mode"; the threshold is what margin is measured
# against. Thresholds are the DVB-S2 required Es/No figures.
#
# Validated against a live ON4VVV reading: MER 10.2 on QPSK 1/2, whose
# threshold here is 1.0, giving the 9.2 dB margin that was observed.
MODCODS = {
    1:  ("QPSK 1/4",    -2.35), 2:  ("QPSK 1/3",    -1.24),
    3:  ("QPSK 2/5",    -0.30), 4:  ("QPSK 1/2",     1.00),
    5:  ("QPSK 3/5",     2.23), 6:  ("QPSK 2/3",     3.10),
    7:  ("QPSK 3/4",     4.03), 8:  ("QPSK 4/5",     4.68),
    9:  ("QPSK 5/6",     5.18), 10: ("QPSK 8/9",     6.20),
    11: ("QPSK 9/10",    6.42), 12: ("8PSK 3/5",     5.50),
    13: ("8PSK 2/3",     6.62), 14: ("8PSK 3/4",     7.91),
    15: ("8PSK 5/6",     9.35), 16: ("8PSK 8/9",    10.69),
    17: ("8PSK 9/10",   10.98), 18: ("16APSK 2/3",   8.97),
    19: ("16APSK 3/4",  10.21), 20: ("16APSK 4/5",  11.03),
    21: ("16APSK 5/6",  11.61), 22: ("16APSK 8/9",  12.89),
    23: ("16APSK 9/10", 13.13), 24: ("32APSK 3/4",  12.73),
    25: ("32APSK 4/5",  13.64), 26: ("32APSK 5/6",  14.28),
    27: ("32APSK 8/9",  15.69), 28: ("32APSK 9/10", 16.05),
}

# Longmynd state 4. Only here does $18 carry a modcod at all — in
# DVB-S (state 3) the code rate arrives on $3 with its own thresholds.
DVBS2_STATE = 4

# Longmynd's own default, and the file it insists on.''',
        "quality port offset + modcod table",
    )

    # ── 3. capture AGC ───────────────────────────────────────────
    src = apply(
        src,
        "        self.modcod = 0\n        self.last_line_at = 0.0",
        "        self.modcod = 0\n"
        "        # Raw tuner gains. Sent on as-is as well as being turned\n"
        "        # into dBm: the raw pair is what a fault report wants when\n"
        "        # the derived figure looks wrong.\n"
        "        self.agc1 = None\n"
        "        self.agc2 = None\n"
        "        self.last_line_at = 0.0",
        "TunerState AGC fields",
    )

    src = apply(
        src,
        "                elif field == 18:\n                    self.modcod = int(value)",
        "                elif field == 18:\n"
        "                    self.modcod = int(value)\n"
        "                elif field == 26:\n"
        "                    self.agc1 = int(value)\n"
        "                elif field == 27:\n"
        "                    self.agc2 = int(value)",
        "parse $26/$27",
    )

    src = apply(
        src,
        '                "programme": self.programme,\n                "state": self.state,',
        '                "programme": self.programme,\n'
        '                "modcod": self.modcod,\n'
        '                "agc1": self.agc1,\n'
        '                "agc2": self.agc2,\n'
        '                "state": self.state,',
        "snapshot carries modcod + AGC",
    )

    # AGC belongs to a signal, like MER does.
    src = apply(
        src,
        '            self.callsign = ""\n            self.programme = ""',
        '            self.callsign = ""\n'
        '            self.programme = ""\n'
        '            self.modcod = 0\n'
        '            self.agc1 = None\n'
        '            self.agc2 = None',
        "clear_signal clears modcod + AGC",
    )

    # ── 4. the quality lines themselves ──────────────────────────
    src = apply(
        src,
        'def build_site_line(name, locator) -> str:',
        '''def build_quality_lines(snap: dict) -> str:
    """The tagged $n,m block for the quality port, or "" if nothing yet.

    Tagged rather than a column table on purpose. Lynx parses both, but
    its own comments record a live-confirmed bug where a column shift
    fed one receiver's MER into another receiver's display with nothing
    to show for it. A tagged value is whatever its tag says it is.

    Fields are omitted rather than zeroed when they are not known, so
    the repeater can tell "this Slave has no rich status" from "this
    Slave is reporting nothing", which are different faults.
    """
    lines = []

    # Always, lock or no lock: the gains are real either way, and they
    # are exactly what somebody aligning a dish is watching.
    if snap["agc1"] is not None:
        lines.append(f"$26,{snap['agc1']}")
    if snap["agc2"] is not None:
        lines.append(f"$27,{snap['agc2']}")
    dbm = agc_table.agc_to_dbm(snap["agc1"], snap["agc2"])
    if dbm is not None:
        lines.append(f"$85,{dbm:.1f}")

    if snap["locked"]:
        if snap["symbol_rate"]:
            # Longmynd counts symbols per second; Lynx appends "kS/s".
            lines.append(f"$9,{round(snap['symbol_rate'] / 1000.0):g}")
        if snap["mer_db"]:
            # Already divided by ten on the way in.
            lines.append(f"$12,{snap['mer_db']:.1f}")
        if snap["programme"]:
            lines.append(f"$14,{snap['programme']}")
        # Only DVB-S2 populates $18. In DVB-S the mode and its threshold
        # would both be guesses, and a guessed margin is worse than none.
        if snap["state"] == DVBS2_STATE and snap["modcod"] in MODCODS:
            name, threshold = MODCODS[snap["modcod"]]
            lines.append(f"$18,{name}")
            if snap["mer_db"]:
                lines.append(f"$30,{snap['mer_db'] - threshold:.1f}")

    return "\\n".join(lines)


def build_site_line(name, locator) -> str:''',
        "build_quality_lines()",
    )

    # ── 5. send it, alongside the heartbeat ──────────────────────
    src = apply(
        src,
        "            try:\n"
        "                sock.sendto((payload + \"\\n\").encode(),\n"
        "                            (self.args.host, self.args.status_port))\n"
        "                self.sent += 1",
        "            try:\n"
        "                sock.sendto((payload + \"\\n\").encode(),\n"
        "                            (self.args.host, self.args.status_port))\n"
        "                self.sent += 1\n"
        "                # Second port, same thread and same cadence. A\n"
        "                # separate thread would let the two drift apart, and\n"
        "                # a panel showing a MER from one moment beside a lock\n"
        "                # state from another is how you chase a fault that is\n"
        "                # not there.\n"
        "                quality = build_quality_lines(snap)\n"
        "                if quality:\n"
        "                    sock.sendto((quality + \"\\n\").encode(),\n"
        "                                (self.args.host, self.args.quality_port))",
        "send on the quality port",
    )

    # ── 6. the argument ──────────────────────────────────────────
    src = apply(
        src,
        '    ap.add_argument("--video-port", type=int, default=DEFAULT_VIDEO_PORT)',
        '    ap.add_argument("--video-port", type=int, default=DEFAULT_VIDEO_PORT)\n'
        '    ap.add_argument("--quality-port", type=int, default=None,\n'
        '                    help="rich status port; defaults to the status port minus "\n'
        '                         f"{QUALITY_PORT_OFFSET}, which is what Lynx derives")',
        "--quality-port argument",
    )

    src = apply(
        src,
        "    args = ap.parse_args()\n",
        "    args = ap.parse_args()\n"
        "    if args.quality_port is None:\n"
        "        args.quality_port = args.status_port - QUALITY_PORT_OFFSET\n",
        "derive quality port default",
    )

    # ── 7. say so on startup ─────────────────────────────────────
    src = apply(
        src,
        '    print(f"  status : UDP port {args.status_port}, every {args.interval}s")',
        '    print(f"  status : UDP port {args.status_port}, every {args.interval}s")\n'
        '    print(f"  quality: UDP port {args.quality_port}")',
        "startup banner",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak"))
    TARGET.write_text(src)

    after = len(src.splitlines())
    print(f"\n  longmynd_source.py: {before} -> {after} lines (+{after - before})")
    print("  backup: longmynd_source.py.bak")
    print("\nNow: python3 -m py_compile longmynd_source.py")


if __name__ == "__main__":
    main()
