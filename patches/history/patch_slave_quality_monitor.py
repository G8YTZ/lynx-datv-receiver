#!/usr/bin/env python3
"""
patch_slave_quality_monitor.py

Adds a per-Slave quality monitor: one thread per Slave reading the rich
status port (its status port minus 96) and filling the same state fields
picotuner_quality_monitor() fills for a local tuner.

Port derivation mirrors the Picotuner's own 9997/9901 relationship, so a
Slave on the default 10997 has its quality port on 10901. Derived rather
than configured for the same reason the video ingress port is fixed:
another number in the config file is another number to get wrong, and
this one has a natural answer.

Reads the TAGGED $n,m format only. Never the column table — lynx_app's
own comments record a real bug where column drift silently fed one
receiver's MER into another's display, and the tagged form is immune to
it by construction.

Fields, exactly as the sender emits them:
    $9  symbol rate      $12 MER          $14 programme    $18 modcod
    $30 margin           $85 dBm          $26 AGC1         $27 AGC2

Fields are added to the state record when they first arrive rather than
pre-created empty, preserving the distinction _blank_remote_state()'s
docstring is explicit about: absent means no rich status port, which is
a different thing from a receiver reporting zeros.

Run from the repo root:  python3 patch_slave_quality_monitor.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


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

    if "REMOTE_VIDEO_INGRESS_PORT" not in src:
        sys.exit("ABORT: Slave relay patches not applied — this builds on them")
    if "remote_quality_monitor" in src:
        sys.exit("ABORT: already patched")

    # ── 1. port derivation + field list ──────────────────────────
    src = apply(
        src,
        "REMOTE_VIDEO_INGRESS_PORT = 10998",
        '''# A Slave's rich status arrives on its status port minus this, the
# same relationship the Picotuner uses between 9997 and 9901. Derived
# rather than configured: it has a natural answer, and a configurable
# one would be a third port per Slave to keep in step at both ends.
REMOTE_QUALITY_PORT_OFFSET = 96

# Filled only from the quality port, and only once it has actually sent
# something. Same names as picotuner_state so a panel or overlay that
# already reads a tuner can read a Slave without a second shape.
REMOTE_QUALITY_FIELDS = (
    "mer", "margin", "symbol_rate", "modcod", "programme",
    "dbm", "agc1", "agc2",
)


def remote_quality_port(i: int) -> int:
    return remote_states[i]["status_port"] - REMOTE_QUALITY_PORT_OFFSET


REMOTE_VIDEO_INGRESS_PORT = 10998''',
        "quality port derivation + field list",
    )

    # ── 2. the monitor itself ────────────────────────────────────
    src = apply(
        src,
        "def remote_source_monitor(index: int):",
        '''def remote_quality_monitor(index: int):
    """Background thread: rich status for one Slave, tagged $n,m only.

    Deliberately the tagged format and never the 9904-style column
    table. The comments on picotuner_quality_monitor() and its table
    counterpart record a real, live-confirmed bug where a column shift
    fed one receiver's MER and modcod into another receiver's display,
    with nothing to indicate it. Tagged fields cannot drift: a value is
    whatever its own tag says it is, and an unrecognised tag is ignored
    rather than silently adopted as the next field along.

    Values are stored exactly as sent, as strings. The Slave has already
    done the arithmetic — margin against the modcod threshold, dBm from
    the AGC table — and re-deriving either at this end would be a second
    implementation to keep in step with the first.
    """
    sock = None
    bound_port = None
    while True:
        try:
            st = remote_states[index]
            want_port = st["status_port"] - REMOTE_QUALITY_PORT_OFFSET
            if sock is None or bound_port != want_port:
                if sock is not None:
                    try: sock.close()
                    except Exception: pass
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                # No SO_REUSEADDR/SO_REUSEPORT here, unlike the Picotuner
                # monitors. One Slave, one quality port, one reader: with
                # them set a second process binds the same port and shares
                # the packets silently, which is how a stray listener once
                # ate Picotuner status. A clash should be loud.
                sock.settimeout(5)
                sock.bind(('', want_port))
                bound_port = want_port
                print(f"[remote {index}] quality monitor listening on {want_port}")

            data, addr = sock.recvfrom(4096)

            # Only from the address this Slave's own status comes from.
            # Until that is known the packet cannot be attributed to
            # anybody, and adopting it on the strength of the port alone
            # is how one site's MER ends up on another site's panel.
            if not st["addr"] or addr[0] != st["addr"]:
                continue

            fields = {}
            for line in data.decode(errors='replace').splitlines():
                line = line.strip()
                if line.startswith('$'):
                    parts = line.split(',', 1)
                    if len(parts) == 2:
                        fields[parts[0]] = parts[1].strip()
            if not fields:
                continue

            # Each field written only if the sender actually included it,
            # so a sender that emits a subset leaves the rest absent
            # rather than blanking values it never mentioned.
            if '$12' in fields: st["mer"]         = fields['$12']
            if '$30' in fields: st["margin"]      = fields['$30']
            if '$9'  in fields: st["symbol_rate"] = fields['$9']
            if '$18' in fields: st["modcod"]      = fields['$18']
            if '$14' in fields: st["programme"]   = fields['$14'].replace('_', ' ')
            if '$85' in fields: st["dbm"]         = fields['$85']
            if '$26' in fields: st["agc1"]        = fields['$26']
            if '$27' in fields: st["agc2"]        = fields['$27']

        except socket.timeout:
            # Silence is normal and says nothing on its own: the status
            # port decides online/offline, and duplicating that judgement
            # here would give two answers to one question.
            continue
        except Exception as e:
            print(f"[remote {index}] quality monitor: {type(e).__name__}: {e}")
            if sock is not None:
                try: sock.close()
                except Exception: pass
                sock = None
            time.sleep(2)


def remote_source_monitor(index: int):''',
        "remote_quality_monitor()",
    )

    # ── 3. start one per enabled Slave ───────────────────────────
    src = apply(
        src,
        "        threading.Thread(target=remote_source_monitor, args=(_i,),\n"
        "                         daemon=True).start()\n",
        "        threading.Thread(target=remote_source_monitor, args=(_i,),\n"
        "                         daemon=True).start()\n"
        "        # Separate thread rather than parsing both formats in one:\n"
        "        # the two ports update at different rates and a stall on\n"
        "        # either must not hold up the other.\n"
        "        threading.Thread(target=remote_quality_monitor, args=(_i,),\n"
        "                         daemon=True).start()\n",
        "start quality monitor per Slave",
    )

    # ── 4. expose whatever has arrived ───────────────────────────
    src = apply(
        src,
        '                "video": _relay_video_for(i),',
        '                "video": _relay_video_for(i),\n'
        '                # Only the fields the Slave has actually sent. A\n'
        '                # Slave with no quality port simply has none of\n'
        '                # these, which reads differently from a Slave\n'
        '                # reporting zeros — and it should.\n'
        '                **{k: st[k] for k in REMOTE_QUALITY_FIELDS if k in st},',
        "expose quality fields in /api/status",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak5"))
    TARGET.write_text(src)

    after = len(src.splitlines())
    print(f"\n  lynx_app.py: {before} -> {after} lines (+{after - before})")
    print("  backup: lynx_app.py.bak5")
    print("\nNow: python3 -m py_compile lynx_app.py && git diff --stat")


if __name__ == "__main__":
    main()
