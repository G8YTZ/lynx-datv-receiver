#!/usr/bin/env python3
"""
patch_network_output.py — send the picture to VLC instead of decoding it.

Adds a Web UI switch that stops the Pi decoding altogether and forwards
the transport stream to another machine on the network, where VLC (or an
Apple TV, or a Fire Stick) does the work instead.

Why
---
A Pi 5 decodes 4K HEVC, but only just, and a Pi 4 does not decode it at
all. Meanwhile almost any modern television, streaming box or laptop has
a hardware decoder sitting idle a few feet away. Forwarding the stream
costs the receiver nothing - it is a socket copy, not a decode - and
moves the expensive part to hardware built for it.

That also makes a Pi 4, or a Pi 3, a perfectly good Lynx receiver: it
does the tuning, the control and the overlay, and something else draws
the picture.

What the operator sees
----------------------
The Pi's own screen goes dark and keeps only the overlay - frequency,
MER, callsign, magic eye, PPM. Everything that tells you how the
reception is going stays local; only the picture leaves. That is
deliberate: a repeater operator watching the OSD on a monitor in the
shack, with the picture on the television in the next room, is a
perfectly sensible arrangement.

Usage
-----
    python3 patch_network_output.py            # dry run
    python3 patch_network_output.py --apply

Then verify.py, restart, and the switch appears on the Web UI.
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TARGET = Path("lynx_app.py")
MARKER = "network_output_enabled"


# --- 1. state ---------------------------------------------------------

OLD_STATE = """mpv_transitioning = False  # mirrored to a local marker file (see"""

NEW_STATE = """# Network video output (see start_network_output() below). When on,
# nothing is decoded here at all: the transport stream is forwarded to
# another machine and the Pi keeps only its overlay.
network_output_enabled = False
network_output_proc = None

mpv_transitioning = False  # mirrored to a local marker file (see"""


# --- 2. the forwarder -------------------------------------------------

OLD_FWD = """def restart_mpv(target_url: str, is_rf: bool = True):"""

NEW_FWD = '''def network_output_target() -> str:
    """Where the stream goes, as host:port.

    A single address rather than a list: this is "put the picture on
    that screen over there", not a distribution system. Anyone wanting
    several viewers should point this at a multicast group, which costs
    nothing extra here and lets the network do the work.
    """
    cfg = config.get('network_output', {}) or {}
    host = cfg.get('host', '') or ''
    port = cfg.get('port', 9950)
    return f"{host}:{port}" if host else ""


def network_output_url() -> str:
    """The URL to type into VLC at the far end.

    udp://@:PORT rather than udp://HOST:PORT - the @ tells VLC to
    listen on that port rather than to send to it, which is the
    difference between seeing a picture and seeing nothing while being
    certain the transmitter is at fault.
    """
    cfg = config.get('network_output', {}) or {}
    return f"udp://@:{cfg.get('port', 9950)}"


def stop_network_output():
    """Stop forwarding, if we are."""
    global network_output_proc
    if network_output_proc is not None:
        try:
            network_output_proc.terminate()
            network_output_proc.wait(timeout=3)
        except Exception:
            try:
                network_output_proc.kill()
            except Exception:
                pass
        network_output_proc = None


def start_network_output(source_url: str):
    """Forward the transport stream instead of decoding it.

    socat rather than ffmpeg or tsp: there is nothing to transcode,
    nothing to remux and nothing to analyse - the bytes arriving are
    exactly the bytes that should leave. socat is already a dependency,
    starts instantly, and uses no measurable CPU. Anything cleverer
    would be a decode we are specifically trying to avoid.

    The source is the same UDP port mpv would have read, so this works
    identically for a Picotuner, a Slave or an HDHomeRun without
    knowing which it is.
    """
    global network_output_proc
    stop_network_output()

    target = network_output_target()
    if not target:
        print("[netout] no target configured - not forwarding")
        return

    # udp://@:9941 -> 9941. Everything reaching mpv is a UDP URL of
    # that shape; a stream from the internet is not forwarded, because
    # the far end can simply open the same URL itself.
    if not source_url.startswith("udp://"):
        print(f"[netout] source {source_url} is not UDP - not forwarding")
        return
    try:
        src_port = int(source_url.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        print(f"[netout] could not read a port from {source_url}")
        return

    host, port = target.rsplit(":", 1)
    cmd = ["socat", f"UDP4-RECV:{src_port}", f"UDP4-SENDTO:{host}:{port}"]
    try:
        network_output_proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"[netout] forwarding :{src_port} -> {host}:{port}")
    except Exception as e:
        print(f"[netout] could not start forwarder: {e}")
        network_output_proc = None


def restart_mpv(target_url: str, is_rf: bool = True):'''


# --- 3. the branch ----------------------------------------------------

OLD_BRANCH = """    is_rf distinguishes the local Picotuner UDP port from a remote"""

NEW_BRANCH = """    When network output is on this starts no player at all - see
    start_network_output(). The overlay carries on drawing over a dark
    screen, which is the point: the figures stay here and the picture
    goes to whatever is better at decoding it.

    is_rf distinguishes the local Picotuner UDP port from a remote"""


OLD_KILL = """    covered window deliberately wider than just this restart.\"\"\"
    kill_mpv()"""

NEW_KILL = """    covered window deliberately wider than just this restart.\"\"\"
    if network_output_enabled:
        # Nothing decodes here. kill_mpv() still runs, because
        # switching the feature on mid-session has to take the picture
        # off the local screen as well as put it on the remote one.
        kill_mpv()
        start_network_output(target_url)
        return

    stop_network_output()
    kill_mpv()"""


EDITS = [
    ("state", OLD_STATE, NEW_STATE),
    ("forwarder", OLD_FWD, NEW_FWD),
    ("docstring", OLD_BRANCH, NEW_BRANCH),
    ("branch", OLD_KILL, NEW_KILL),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found. Run from the lynx directory.")
        return 2

    text = TARGET.read_text(encoding="utf-8")
    if MARKER in text:
        print(f"{TARGET} already patched - nothing to do.")
        return 0

    print(f"\nchecking anchors in {TARGET}\n")
    ok = True
    for name, anchor, _new in EDITS:
        n = text.count(anchor)
        if n == 1:
            print(f"  {name:<12} OK   one match")
        else:
            print(f"  {name:<12} FAIL {n} matches - expected 1")
            ok = False
    if not ok:
        print("\nNo changes made. The 'branch' anchor is the likely one -")
        print("kill_mpv() may appear more than once; tell Claude the")
        print("surrounding lines and it will widen the anchor.")
        return 1

    patched = text
    for _n, anchor, new in EDITS:
        patched = patched.replace(anchor, new, 1)

    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(patched)
        tp = Path(tmp.name)
    try:
        py_compile.compile(str(tp), doraise=True)
        print("\n  syntax check  OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"\n  syntax check  FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tp.unlink(missing_ok=True)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(TARGET, TARGET.with_suffix(f".py.bak-netout-{stamp}"))
    TARGET.write_text(patched, encoding="utf-8")
    print(f"\n  {TARGET} patched")
    print("\nThis is the back end only - the API route and the Web UI")
    print("card come next. Add to config/lynx_config.yaml:")
    print("    network_output:")
    print("      host: 192.168.0.150")
    print("      port: 9950")
    return 0


if __name__ == "__main__":
    sys.exit(main())
