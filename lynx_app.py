#!/usr/bin/env python3

# Copyright (C) 2026 Justin, G8YTZ / EI3IOB
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

# ============================================================
#  Lynx DATV Receiver — Web API and Configuration Interface
#  G8YTZ / EI3IOB  —  July 2026
#
#  FastAPI backend providing:
#    - REST API for tuning, streaming, status
#    - Web configuration interface
#    - Compatible with Bitfocus Companion, Home Assistant,
#      M5Stack Dial, and any HTTP client
#
#  Usage: python3 lynx_app.py
#  API docs: http://localhost:8080/docs
# ============================================================

import asyncio
import json
import threading
import os
import re
import signal
import socket
import subprocess
import time
import urllib.request
import yaml

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import lynx_notifications

def utc_now_iso() -> str:
    """Same output format as the deprecated datetime.utcnow().isoformat()
    (naive, no timezone suffix) via the current, non-deprecated API -
    avoids changing the string format for anything already parsing it
    (the web UI, Bitfocus Companion, the M5Dial)."""
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

# ── Config ───────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config" / "lynx_config.yaml"
STATE_PATH = Path(__file__).parent / "lynx_state.json"
DRIFT_SCRIPT_PATH = Path(__file__).parent / "lynx_drift_correction.lua"

def load_config():
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def save_config(cfg):
    with open(CONFIG_PATH, 'w') as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)

def save_last_state(state: dict):
    """Persist the most recent successful tune/stream so Lynx can
    resume it automatically after any restart — whether from a crash,
    the watchdog, a scheduled 12-hour reboot, or a genuine power
    cycle. Written on every successful tune()/start_stream() call."""
    try:
        with open(STATE_PATH, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        print(f"Could not save last state: {e}")

def load_last_state():
    """Returns the last saved state, or None if there isn't one
    (e.g. genuinely first boot) or it's corrupted."""
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return None

config = load_config()

# ── App ───────────────────────────────────────────────────────
app = FastAPI(
    title="Lynx DATV Receiver",
    description="REST API for the Lynx software DATV receiver. "
                "Compatible with Bitfocus Companion, Home Assistant, "
                "M5Stack Dial, and any HTTP client.",
    version="1.0.0",
    contact={"name": "G8YTZ / EI3IOB"}
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── State ─────────────────────────────────────────────────────
current_mode: str = "idle"    # idle | rf | stream
mpv_running_for_rf: bool = False  # tracks whether mpv has actually been started for the
                                   # current RF tune (as opposed to just tuned-and-waiting-
                                   # for-lock) - see rf_mpv_lifecycle_monitor()
mpv_last_started_at: float = 0.0  # timestamp of the most recent mpv start - lets
                                   # mpv_decoder_health_monitor grant a startup grace period
current_preset: str = ""
current_stream_name: str = ""  # friendly name for the OSD, set by whoever
                                # initiated the stream — not inspected from
                                # the stream content itself.
current_stream_url: str = ""   # the actual URL, tracked separately from
                                # the friendly name for protocol detection
current_lnb_lo_khz: int = 0    # LNB LO in use for the current tune, so
                                # the API can report both the L-band/IF
                                # frequency the Picotuner is actually
                                # locked on AND the real downlink
                                # frequency for display.
current_lnb_side: str = "low"  # "low" (Ku-band, IF=downlink-LO) or
                                # "high" (C-band, IF=LO-downlink) —
                                # needed to correctly reverse the
                                # calculation for display.
current_volume: int = config.get('audio', {}).get('default_volume', 100)
                                # the user's actual current session
                                # volume, distinct from the config's
                                # own "default_volume" (the boot-time
                                # starting point only). restart_mpv()
                                # was reapplying the config default on
                                # EVERY tune, silently discarding
                                # whatever the user had actually set
                                # via the volume slider — that's the
                                # bug this variable fixes: restart_mpv()
                                # now reapplies THIS value instead.

tune_lock = threading.Lock()  # serializes tune()/start_stream() calls end-to-
                               # end (including the async mpv restart, not
                               # just the initial synchronous part) — both
                               # share the same underlying resources (mpv,
                               # the transition-cover marker, the combiner)
                               # so both must serialize against each other,
                               # not just against themselves. FastAPI runs
                               # plain (non-async) route handlers like these
                               # in a threadpool, so requests fired close
                               # together (e.g. rapid preset/stream
                               # switching) could genuinely execute
                               # concurrently with no locking at all: both
                               # trying to kill/start mpv and the combiner
                               # at the same time, both mutating shared
                               # global state (diversity_enabled,
                               # current_mode, etc) without coordination,
                               # and racing on the shared transition-cover
                               # marker (briefly exposing the desktop
                               # underneath - confirmed live). Confirmed as
                               # the likely cause of reported "random"
                               # crashes specifically when switching
                               # presets/modes in quick succession. Lock
                               # release is now guaranteed via thin
                               # tune()/start_stream() wrapper functions
                               # around _tune_impl()/_start_stream_impl() -
                               # confirmed live that a prior, bounded-
                               # timeout-only approach could leave the lock
                               # stuck forever if _tune_impl raised before
                               # its async thread started, silently
                               # blocking all future RF tunes while
                               # streaming kept working (since it didn't
                               # touch this lock at all at the time).
FFMPEG_BG_CMD = None

MPV_SOCKET = "/tmp/mpv-socket"
MPV_TRANSITION_MARKER = "/tmp/lynx_mpv_transitioning"
mpv_transitioning = False  # mirrored to a local marker file (see
                            # MPV_TRANSITION_MARKER) which the overlay
                            # checks directly and instantly on every
                            # fast draw tick, rather than relying on
                            # its own HTTP poll cycle to notice the
                            # change — a local file check has no such
                            # delay since both processes run on the
                            # same machine.

def start_transition_cover():
    """Signal the overlay to show its opaque Lynx-logo cover, and mute
    whatever mpv is currently playing immediately. Called well BEFORE
    any actual source-switching begins (killing the old ffmpeg, tuning
    the Picotuner, starting the new ffmpeg) — not just around the mpv
    restart itself — so there's generous margin on the front end even
    if the Pi is under load and scheduling is delayed."""
    global mpv_transitioning
    mpv_transitioning = True
    open(MPV_TRANSITION_MARKER, 'w').close()
    # Mute the OLD process right away — launching the new one with
    # --mute=yes only silences audio from the new source, it does
    # nothing about the old source still being audible for however
    # long it takes to actually get killed.
    try:
        mpv_cmd({"command": ["set_property", "mute", True]})
    except Exception:
        pass

def end_transition_cover():
    """Let the overlay uncover the screen again."""
    global mpv_transitioning
    mpv_transitioning = False
    try:
        os.remove(MPV_TRANSITION_MARKER)
    except FileNotFoundError:
        pass

def kill_mpv():
    """Kills the current mpv process, if any, and waits for it to
    genuinely release its resources — including the UDP port it may
    have bound (e.g. 9941 for direct RF playback). Extracted out of
    restart_mpv() so diversity mode can call this explicitly BEFORE
    starting the combiner, which needs that same port free to bind
    for itself. Without this, starting the combiner immediately
    while the old mpv was still bound to 9941 caused it to crash on
    startup with "Address already in use" — confirmed directly."""
    # SIGKILL cannot be caught or handled — the old mpv process never
    # gets a chance to release its own DRM/GPU resources (framebuffers,
    # planes, GBM buffers) before dying. Trying a graceful IPC quit
    # first, so it can clean up properly, in case that's what's been
    # leaving stale GPU state for the next process to inherit — only
    # falling back to SIGKILL if it doesn't exit promptly on its own.
    try:
        mpv_cmd({"command": ["quit"]})
    except Exception:
        pass
    for _ in range(10):  # up to ~1s grace period
        result = subprocess.run(["pgrep", "-f", f"mpv.*input-ipc-server={MPV_SOCKET}"],
                                 capture_output=True)
        if result.returncode != 0:  # no longer running — exited cleanly
            break
        time.sleep(0.1)
    else:
        # Didn't exit in time — force it.
        subprocess.run(["pkill", "-9", "-f", f"mpv.*input-ipc-server={MPV_SOCKET}"],
                        capture_output=True)
    try:
        os.remove(MPV_SOCKET)
    except FileNotFoundError:
        pass
    time.sleep(0.5)

MPV_DRIFT_STATUS_PATH = "/tmp/lynx_mpv_drift.json"

def get_mpv_drift_status():
    """Reads the drift-from-live status the lynx_drift_correction.lua
    script writes out. Returns None cleanly if it doesn't exist yet
    (mpv just started, or the script isn't loaded) rather than raising."""
    try:
        with open(MPV_DRIFT_STATUS_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

def restart_mpv(target_url: str, is_rf: bool = True):
    """Fully kill and restart the mpv process, pointing it at a fresh
    target URL. Used instead of IPC-only reload commands (loadfile,
    stop+loadfile, video-track cycling) — all of these were tried and
    either failed to fix a stuck video output (decoder diagnostics
    showed mpv genuinely decoding, core-idle false, but the displayed
    picture never updating) or actively made things worse (cycling the
    video track off/on broke the RF path too). A fresh process cannot
    carry over whatever stale VO state was causing this.

    is_rf distinguishes the local Picotuner UDP port from a remote
    stream URL — several flags below are only correct for one or the
    other, not both. Forcing --demuxer-lavf-format=mpegts is correct
    for RF (the Picotuner genuinely sends raw MPEG-TS over UDP), but
    actively wrong for something like an RTMP stream (FLV-based, not
    MPEG-TS at all) — forcing that interpretation was confirmed to
    prevent mpv from detecting the video track at all (audio still
    got through, giving a black window with sound). Similarly the
    tight demuxer-max-bytes buffer cap (needed to fix RF's delay
    slowly growing over a long session) was found to be too small for
    RTMP to buffer enough data to identify its video track during the
    initial connection — confirmed directly by testing the exact same
    command by hand with and without these flags.

    VO backend: --vo=gpu with --hwdec=drm-copy is the correct, working
    configuration — genuinely hardware-accelerated decode, ~30% CPU
    even on 1080p HEVC, confirmed stable through RF resume and stream
    switches. A long investigation earlier blamed this combination for
    a restart-freeze and a display-flashing bug, and tried x11 as a
    CPU-heavy fallback while ruling out GPU memory/CMA allocation,
    thermal throttling, mesa version differences, and abrupt-vs-
    graceful process termination as the cause. The ACTUAL root cause
    turned out to be unrelated to any of that: a physical HDMI signal
    integrity problem, caused by a case's carrier/riser card rotating
    the HDMI connector 90 degrees. Once the physical connection was
    solid, gpu worked correctly first time, with no code changes at
    all. Worth remembering if a similar symptom (decoder healthy,
    picture not updating, or display flashing/resyncing) shows up
    again on different hardware — check the physical display
    connection before assuming it's a driver/software bug.

    Callers are responsible for the transition cover (see
    start_transition_cover/end_transition_cover) — this function only
    handles the actual process mechanics, so callers can make the
    covered window deliberately wider than just this restart."""
    kill_mpv()

    # Launch muted so there's no audio pop/glitch as the new process
    # spins up and briefly plays whatever it first receives before
    # everything has settled.
    if is_rf:
        source_flags = (
            "--demuxer=lavf --demuxer-lavf-format=mpegts "
            "--demuxer-max-bytes=512KiB --demuxer-max-back-bytes=128KiB "
            "--profile=low-latency --cache-pause=no "
        )
    else:
        # No format-forcing (let mpv auto-detect the real container —
        # RTMP/FLV, HLS, whatever it actually is), no tight buffer cap,
        # and no low-latency profile either — that profile bundles its
        # own internal cache/readahead settings that were found to
        # still block video track detection on RTMP even after the
        # explicit demuxer-max-bytes flags above were removed. Streams
        # get mpv's plain default caching behaviour, matching exactly
        # what was confirmed working in a direct hand-run test.
        source_flags = ""

    cmd = (
        f"mpv --fullscreen --ontop --border=no --no-osc --no-input-default-bindings "
        f"--cursor-autohide=always --force-window=yes --vo=gpu --hwdec=drm-copy --mute=yes "
        f"--audio-pitch-correction=no --script={DRIFT_SCRIPT_PATH} "
        f"{source_flags}"
        f"--keep-open=yes --idle=yes "
        f"--input-ipc-server={MPV_SOCKET} "
        f"'{target_url}'"
    )
    env = os.environ.copy()
    env["DISPLAY"] = ":0"
    env["XAUTHORITY"] = "/home/pi/.Xauthority"
    # Explicitly hardcoded rather than relying on inheritance from
    # lynx_app.py's own environment — that varied depending on how the
    # whole process chain was originally started (local desktop
    # terminal vs SSH session), causing intermittent video freezes
    # where mpv's playback stayed internally healthy (time-pos still
    # advancing, no eof) but nothing reached the actual display.
    env["XDG_RUNTIME_DIR"] = "/run/user/1000"
    # Force Xwayland rather than native Wayland — confirmed fix for a
    # freeze that only occurred when launched from within the actual
    # desktop/labwc session (local terminal or autostart), never via
    # SSH. The distinguishing factor wasn't autostart timing at all:
    # SSH sessions never have WAYLAND_DISPLAY set, so mpv was always
    # accidentally forced onto the older Xwayland/X11 path, which
    # works fine. Launched from within the real session, mpv has a
    # genuine WAYLAND_DISPLAY available and prefers native Wayland
    # EGL/Vulkan — confirmed to be the actually-broken path (this is
    # what the "VK_ERROR_OUT_OF_HOST_MEMORY" swapchain failures were).
    # Clearing WAYLAND_DISPLAY here removes that choice entirely.
    env.pop("WAYLAND_DISPLAY", None)
    # Truncate and redirect to a real log file on every restart -
    # confirmed this was previously going to DEVNULL entirely, meaning
    # /tmp/mpv.log never actually reflected the current, running mpv
    # process at all, only whatever mpv was first launched at boot by
    # lynx_start.sh. Every diagnostic read of this file across an
    # entire session was showing stale, potentially very old content -
    # this is what made repeated identical-looking "av_find_stream_info
    # failed" tails appear across genuinely different live situations.
    # Truncating (not appending) on each restart specifically so the
    # file always unambiguously reflects only the CURRENT mpv instance.
    mpv_log_fh = open('/tmp/mpv.log', 'w')
    subprocess.Popen(cmd, shell=True, env=env,
                      stdin=subprocess.DEVNULL,
                      stdout=mpv_log_fh,
                      stderr=subprocess.STDOUT,
                      preexec_fn=os.setsid)
    mpv_log_fh.close()  # the child has its own duplicated fd - safe to close our handle immediately
    # RF connects to a local UDP port almost instantly, but a stream
    # URL needs a genuine network connection (DNS, TCP handshake,
    # RTMP/SRT negotiation) which can easily take longer than the 2s
    # this used to be — sending IPC commands too early was found to
    # abruptly disconnect a socket mid-startup and disrupt mpv's own
    # loading process entirely ("client removed during hook handling"
    # followed by broken pipe errors, with mpv never actually reaching
    # video/audio format detection at all).
    #
    # Poll for the actual IPC socket file to exist rather than a blind
    # fixed sleep - addresses the root cause directly (don't connect
    # until the socket genuinely exists) instead of guessing how long
    # that takes. Confirmed live as worth doing: the fixed 4s was a
    # meaningful chunk of a 20+ second recovery delay after a genuine
    # freeze, and mpv typically creates this socket well under a
    # second after launch when nothing's actually wrong. Small safety
    # margin kept after detection since file existence alone doesn't
    # 100% guarantee mpv's own IPC server loop is fully listening yet;
    # generous timeout ceiling (6s, slightly above the original fixed
    # value) as a fallback so a genuine problem still can't hang
    # forever - if the socket never appears, proceeds anyway rather
    # than blocking indefinitely, same risk profile as the original
    # fixed sleep in a worst case.
    socket_wait_start = time.time()
    while not os.path.exists(MPV_SOCKET):
        if time.time() - socket_wait_start > 6.0:
            break
        time.sleep(0.05)
    time.sleep(0.3)  # small safety margin after the socket first appears

    # Unmute and reapply the CURRENT SESSION volume now that it's
    # settled — NOT the config default. Previously always used the
    # config default here, meaning any volume change made via the
    # slider silently reverted on every single tune/restart, even
    # though the slider itself kept showing the value the user had
    # actually set (confirmed: set to 50%, tune elsewhere, mpv's
    # real volume snapped back to 100% while the UI still read 50%).
    global current_volume
    mpv_cmd({"command": ["set_property", "volume", current_volume]})
    mpv_cmd({"command": ["set_property", "mute", False]})

def wait_for_mpv_rendering(timeout: float = 8.0) -> bool:
    """Polls mpv's own log (freshly truncated by restart_mpv() just
    before this is called) for concrete evidence it has actually
    started rendering - not just that the process was launched.

    Confirmed live as a real, necessary distinction: mpv_running_for_rf
    being True only ever meant "the process was launched", never
    "something is genuinely on screen yet". The existing fixed delays
    (4s inside restart_mpv() plus another 0.5s at the call site, 4.5s
    total) were still not always enough - mpv can take a variable
    amount of time to reach a valid keyframe (the PPS-error/NALU-skip
    pattern seen earlier tonight can repeat for several seconds before
    resolving), so a fixed guess can't reliably cover every case. This
    polls for real evidence instead, bounded by a timeout so a genuine
    problem never blocks the caller indefinitely.

    Returns True if confirmed within the timeout, False if it timed
    out. Callers now check this return value (a real bug, confirmed
    live: this docstring used to say "callers should proceed either
    way", which was true of every actual call site at the time - the
    cover was removed unconditionally regardless of this return value,
    meaning a genuine timeout still uncovered onto whatever mpv hadn't
    actually rendered yet). All three call sites now keep the cover up
    and retry rather than uncover on a timeout.

    Every call logs how long confirmation actually took (or that it
    timed out) - a permanent, ongoing record specifically so a future
    "is this slow start getting worse over time" question can be
    answered directly from the logs, rather than requiring a fresh
    manual measurement with no earlier data point to compare against."""
    start = time.time()
    deadline = start + timeout
    markers = ("VO:", "AV:")
    while time.time() < deadline:
        try:
            with open('/tmp/mpv.log') as f:
                content = f.read()
            if any(m in content for m in markers):
                elapsed = time.time() - start
                print(f"[mpv_render] confirmed rendering after {elapsed:.1f}s")
                return True
        except OSError:
            pass
        time.sleep(0.1)
    print(f"[mpv_render] did NOT confirm rendering within {timeout:.1f}s timeout")
    return False

def mpv_cmd(cmd: dict):
    """Send a JSON command to mpv via IPC socket (fire and forget)."""
    import json as _json
    try:
        import socket as _socket
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect('/tmp/mpv-socket')
        s.sendall((_json.dumps(cmd) + '\n').encode())
        # Graceful shutdown rather than an immediate close() — closing
        # abruptly right after sendall() was found to sometimes log
        # "client removed during hook handling" on mpv's side and
        # appeared to disrupt its own startup sequence when a command
        # arrived while it was still mid-load. shutdown() tells the
        # OS we're done sending but lets any in-flight processing on
        # mpv's end complete cleanly first.
        try:
            s.shutdown(_socket.SHUT_WR)
        except Exception:
            pass
        s.close()
    except Exception:
        pass

def mpv_query(cmd: dict):
    """Send a JSON command to mpv via IPC socket and return its response
    (used for get_property calls where we need the actual value back)."""
    import json as _json
    try:
        import socket as _socket
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect('/tmp/mpv-socket')
        s.sendall((_json.dumps(cmd) + '\n').encode())
        response = s.recv(4096).decode()
        s.close()
        return _json.loads(response.splitlines()[0])
    except Exception:
        return None

def _kill_process_reliably(proc, pkill_pattern=None):
    """Reliably terminate a subprocess started with shell=True.
    proc.terminate() alone sends SIGTERM to the shell wrapper, not
    necessarily to the actual child process (e.g. ffmpeg) it spawned —
    on Linux this frequently leaves the real process running. Using
    a dedicated process group (via preexec_fn=os.setsid at spawn time)
    and killing the whole group fixes this properly. A pattern-based
    pkill is also run as a belt-and-braces fallback in case the
    process group approach doesn't catch everything (e.g. old
    processes started before this fix was in place)."""
    if proc and proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
    if pkill_pattern:
        subprocess.run(["pkill", "-9", "-f", pkill_pattern], capture_output=True)

def start_ffmpeg_bg():
    """Start ffmpeg background stream to port 9941."""
    global FFMPEG_BG_CMD
    stop_ffmpeg_bg()
    bg_path = Path(__file__).parent / "lynx_bg.png"
    if bg_path.exists():
        import subprocess as _sp
        my_ip = _sp.run(["hostname", "-I"], capture_output=True, text=True).stdout.split()[0]
        cmd = (f"ffmpeg -nostdin -hide_banner -loglevel error "
               f"-loop 1 -i {bg_path} -vf scale=1920:1080 -r 25 -g 50 "
               f"-c:v libx264 -preset ultrafast -tune stillimage "
               f"-f mpegts udp://127.0.0.1:9941")
        FFMPEG_BG_CMD = subprocess.Popen(cmd, shell=True,
                                          stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL,
                                          preexec_fn=os.setsid)

def stop_ffmpeg_bg():
    """Stop ffmpeg background stream."""
    global FFMPEG_BG_CMD
    _kill_process_reliably(FFMPEG_BG_CMD, pkill_pattern="ffmpeg.*lynx_bg")
    FFMPEG_BG_CMD = None

# ── Picotuner monitor state ──────────────────────────────────
# Updated by background thread reading port 9997 broadcast.
picotuner_state = {
    "online": False,
    "locked": False,
    "callsign": "",
    "frequency": "",
    "symbol_rate": "",
    "rx1_raw": "",
    "firmware": "",
    "last_seen": 0,
    "mer": "",
    "margin": "",
    "programme": "",
    "modcod": "",
    "codec": "",
    "audio_codec": "",
    "level": "",
    # ptwh0v3k+ (2026-07-23): a real dBm value from the firmware's own
    # look-up table, plus raw AGC1/AGC2 - confirmed live via tcpdump
    # against genuinely flashed hardware before this was added, not
    # assumed from documentation alone. Kept separate from "level"
    # above (the old, rough approximation) rather than replacing it,
    # so older firmware without these fields still works unchanged.
    "dbm": "",
    "agc1": "",
    "agc2": "",
}

def picotuner_monitor():
    """Background thread: reads Picotuner broadcast on port 9997.
    Keeps the socket open continuously for efficiency. Also parses
    RX2 from the SAME broadcast for Diversity mode's second tuner —
    confirmed via tcpdump that this single broadcast already
    contains both RX1 and RX2 lines together; no separate port
    needed. (An earlier version tried reading a supposedly-separate
    rich status port for rcv=2, based on an unverified assumption
    about the port scheme that turned out to be wrong — rcv=2 never
    actually sends anything to that port at all.)"""
    global picotuner_state, picotuner_state_b
    cfg = config['picotuner']
    sock = None
    while True:
        try:
            if sock is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                sock.settimeout(5)
                sock.bind(('', cfg['status_port']))
            
            data, addr = sock.recvfrom(4096)
            text = data.decode(errors='replace')
            picotuner_state["online"] = True
            picotuner_state["last_seen"] = time.time()
            
            # Parse RX1 line: "437.024 G8YTZ" or "437.000T search"
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("RX1"):
                    rx1 = line.replace("RX1", "").strip()
                    picotuner_state["rx1_raw"] = rx1
                    parts = rx1.split()
                    # "search" and "lost" both mean not locked
                    unlocked_states = {"search", "lost", ""}
                    if len(parts) >= 2 and parts[-1] not in unlocked_states:
                        picotuner_state["locked"] = True
                        picotuner_state["callsign"] = parts[-1]
                        picotuner_state["frequency"] = parts[0].rstrip("TB")
                    else:
                        picotuner_state["locked"] = False
                        picotuner_state["callsign"] = ""
                        if parts:
                            picotuner_state["frequency"] = parts[0].rstrip("TB")
                if line.startswith("RX2"):
                    # Same format as RX1 — confirmed live: "437.024B G8YTZ".
                    # The trailing letter (T/B here) appears to indicate
                    # which physical plug that receiver is currently on,
                    # not something specific to RX1 vs RX2 — strip either.
                    rx2 = line.replace("RX2", "").strip()
                    picotuner_state_b["online"] = True
                    picotuner_state_b["last_seen"] = time.time()
                    parts = rx2.split()
                    unlocked_states = {"search", "lost", ""}
                    if len(parts) >= 2 and parts[-1] not in unlocked_states:
                        picotuner_state_b["locked"] = True
                        picotuner_state_b["callsign"] = parts[-1]
                        picotuner_state_b["frequency"] = parts[0].rstrip("TB")
                    else:
                        picotuner_state_b["locked"] = False
                        picotuner_state_b["callsign"] = ""
                        if parts:
                            picotuner_state_b["frequency"] = parts[0].rstrip("TB")
                if line.startswith("Software"):
                    picotuner_state["firmware"] = line.split()[-1] if line.split() else ""
        
        except socket.timeout:
            if time.time() - picotuner_state["last_seen"] > 10:
                picotuner_state["online"] = False
                picotuner_state["locked"] = False
            if time.time() - picotuner_state_b.get("last_seen", 0) > 10:
                picotuner_state_b["online"] = False
                picotuner_state_b["locked"] = False
        except Exception as e:
            if sock:
                try: sock.close()
                except: pass
                sock = None
            if time.time() - picotuner_state["last_seen"] > 10:
                picotuner_state["online"] = False
            time.sleep(1)

MER_PUBLISH_PATH = "/tmp/lynx_tuner_mer.json"

HEVC_RESTART_DIAGNOSTIC_MODE = False  # Diagnostic complete - live evidence confirmed the decoder
# genuinely self-recovers from bursts up to 6 errors with no freeze and no growing buffer delay
# (Cache: stayed 0.0-0.4s throughout, playback_delay never fired). The restart trigger was firing
# on things that would have cleared themselves - see ERROR_THRESHOLD below, raised accordingly.
HEVC_DIAGNOSTIC_LOG_PATH = "/tmp/hevc_error_diagnostic.log"

DELAY_RESTART_DIAGNOSTIC_MODE = False  # Diagnostic complete - unlike the HEVC trigger, this one
# appears to be catching something genuinely real: live evidence showed the gap stuck at exactly
# 4.0s, completely unchanging, for 28+ seconds with no sign of recovering on its own - both
# playback and buffered position frozen together, not a decoder hiccup that clears itself. The
# normal restart trigger was legitimate and stays active. Also revealed a real gap in
# DELAY_EMERGENCY_THRESHOLD_SECS's design: it was built to catch a gap that GROWS large, but
# never accounts for a gap that STAYS STUCK at a modest, non-growing value indefinitely - the
# emergency safety net would never have fired in this exact scenario. Left the emergency
# threshold in place as a backstop for a different, more severe failure mode, but it should not
# be relied on as the only protection while this flag is False.
DELAY_DIAGNOSTIC_LOG_PATH = "/tmp/delay_diagnostic.log"
DELAY_EMERGENCY_THRESHOLD_SECS = 20.0        # a gap this large, sustained, is unambiguously bad
DELAY_EMERGENCY_CONSECUTIVE_CHECKS = 5        # ...for this many consecutive checks (~10s) always restarts

def mpv_decoder_health_monitor():
    """Background thread, RF mode only: watches mpv's own log for two
    related but distinct symptoms of the decoder falling behind, and
    restarts mpv if either recurs - rather than leaving it silently
    stuck (or steadily drifting later and later) while other health
    signals (combiner, tuner status) all look completely normal.

    Only possible to discover after fixing restart_mpv()'s stdout/
    stderr previously going to /dev/null on every restart (see that
    function) - once genuinely captured, mpv's log revealed two
    related, live-observed failure modes:

    1. Repeated "[ffmpeg/video] hevc: Could not find ref with POC N"
       errors clustering in a short window - the decoder losing track
       of a reference frame, observed during a hard freeze. A single
       occurrence is often self-concealing in HEVC decoders; several
       clustered together suggests the decoder's own internal state
       has genuinely diverged from the bitstream.

    2. A persistently growing gap between mpv's own playback position
       and its buffered/demuxed position (the two timestamps in every
       "AV: X / Y" line) - observed separately as "massive delay
       building up" without necessarily a burst of the error above.
       Plausibly the same underlying mechanism (occasional reference-
       frame recovery costing the decoder real processing time) but
       spread out sparsely enough to never cluster past threshold #1 -
       each hiccup adds a little permanent delay rather than causing
       a hard stop.

    Both plausibly stem from switching between tuner A and B mid-GOP,
    unique to diversity mode (non-diversity never switches physical
    sources, so has nothing to misalign).

    These are new hypotheses from evidence only just made visible -
    worth treating the thresholds/windows below as reasonable starting
    points to refine with real data, not finally-tuned constants."""
    ERROR_PATTERN = "Could not find ref"
    ERROR_THRESHOLD = 10    # this many occurrences...
    ERROR_WINDOW = 5.0      # ...within this many seconds triggers a restart. Threshold was 3,
                              # raised to 10 after live diagnostic testing confirmed the decoder
                              # cleanly self-recovers from bursts up to 6 (no freeze, no growing
                              # buffer delay) - the old threshold was firing on things that would
                              # have cleared themselves. Window was 10.0, halved earlier tonight -
                              # evidence under real, dynamic content showed observed freeze
                              # duration closely matching this window, suggesting it was setting
                              # the freeze length rather than mpv's own recovery time. Halved to
                              # reduce worst-case detection latency, now paired with the raised
                              # 10-error threshold above rather than the original 3.
    DELAY_THRESHOLD_SECS = 3.0   # gap between playback and buffered position...
    DELAY_CONSECUTIVE_CHECKS = 2  # ...persisting for this many consecutive checks triggers a restart.
                                    # Was 3 - reduced given this trigger was already confirmed (via
                                    # direct diagnostic testing) to catch a genuine, non-self-recovering
                                    # problem, not a false positive - part of trimming a 20+ second
                                    # real-world recovery delay down toward something more reasonable.
    CHECK_INTERVAL = 2.0
    LOG_PATH = "/tmp/mpv.log"
    AV_LINE_RE = re.compile(r'AV:\s*(\d+):(\d+):(\d+)\s*/\s*(\d+):(\d+):(\d+)')

    # Circuit breaker: if restarting mpv isn't actually resolving the
    # condition - it keeps recurring rapidly, restart after restart -
    # that's strong evidence the real problem is upstream of mpv
    # entirely (the combiner falling behind, or the RF signal itself),
    # not mpv's own decoder. A fresh mpv process inherits no state
    # from the last one, so a genuinely mpv-side problem should NOT
    # recur immediately after a restart. Confirmed live as a real
    # failure mode: up to 70 restarts in a few minutes, none of them
    # actually fixing anything - just repeatedly covering and
    # uncovering the screen for no benefit.
    CIRCUIT_BREAKER_THRESHOLD = 5   # this many restarts...
    CIRCUIT_BREAKER_WINDOW = 300.0  # ...within this many seconds (5 min)...
    CIRCUIT_BREAKER_COOLDOWN = 300.0  # ...trips a 5 min cooldown with no further restarts

    def hms_to_secs(h, m, s):
        return int(h) * 3600 + int(m) * 60 + int(s)

    STARTUP_GRACE_SECS = 12.0   # no evaluation at all for this long after mpv starts -
                                  # covers mpv's own normal initial buffering, which can
                                  # legitimately show a temporarily large playback/buffered
                                  # gap that isn't a real problem

    last_size = 0
    recent_error_times = []
    high_delay_streak = 0
    emergency_delay_streak = 0
    recent_restart_times = []
    breaker_tripped_until = 0.0

    while True:
        time.sleep(CHECK_INTERVAL)
        try:
            if current_mode != "rf" or not mpv_running_for_rf:
                last_size = 0
                recent_error_times = []
                high_delay_streak = 0
                emergency_delay_streak = 0
                continue
            try:
                size = os.path.getsize(LOG_PATH)
            except OSError:
                continue
            if size < last_size:
                # restart_mpv() truncates this file on every restart -
                # a smaller size means a genuinely new mpv instance,
                # not something to interpret as an error.
                last_size = 0
                recent_error_times = []
                high_delay_streak = 0
                emergency_delay_streak = 0
            if size == last_size:
                continue
            if time.time() - mpv_last_started_at < STARTUP_GRACE_SECS:
                # Still within the startup grace window - advance our
                # read position so nothing backlogs, but don't evaluate
                # anything yet.
                last_size = size
                continue
            with open(LOG_PATH) as f:
                f.seek(last_size)
                new_content = f.read()
            last_size = size

            now = time.time()
            occurrences = new_content.count(ERROR_PATTERN)
            recent_error_times.extend([now] * occurrences)
            recent_error_times = [t for t in recent_error_times if now - t <= ERROR_WINDOW]

            if HEVC_RESTART_DIAGNOSTIC_MODE and occurrences > 0:
                try:
                    with open(HEVC_DIAGNOSTIC_LOG_PATH, 'a') as f:
                        f.write(f"{utc_now_iso()}  "
                                f"+{occurrences} new (total in window: {len(recent_error_times)})\n")
                except OSError:
                    pass

            # Check the gap using the LAST "AV:" line in this chunk -
            # the most recent, current reading.
            av_matches = AV_LINE_RE.findall(new_content)
            if av_matches:
                h1, m1, s1, h2, m2, s2 = av_matches[-1]
                playback = hms_to_secs(h1, m1, s1)
                buffered = hms_to_secs(h2, m2, s2)
                gap = buffered - playback
                # Sanity cap: confirmed live that mpv can occasionally
                # misinterpret a corrupted timestamp in the stream as
                # an absurdly long total duration (observed: 27+
                # hours) rather than a genuine buffered position. Given
                # the demuxer's own small cache cap, a real delay
                # should never plausibly exceed well under a minute -
                # anything wildly larger is a bogus duration reading,
                # not a real one, and must be ignored rather than
                # acted on (it previously triggered a restart every
                # single time, regardless of anything actually wrong).
                GAP_SANITY_CAP_SECS = 60.0
                DRIFT_ACTION_GRACE_SECS = 5.0  # confirmed live: a speed change from the drift
                # correction script can plausibly cause a brief, misleading blip in this gap
                # reading that isn't a genuine new problem - a restart fired 9s after the drift
                # script itself reported catching up, right around when it reverted speed back
                # to normal. Suppress this trigger briefly after any such action rather than
                # mistake our own correction's side effect for something needing a full restart.
                drift_status = get_mpv_drift_status()
                in_drift_grace = False
                if drift_status is not None:
                    # Both last_action_at and t come from mpv's own internal clock
                    # (mp.get_time()), which mpv's own docs describe as "basically
                    # the system time, with an arbitrary offset" - NOT guaranteed to
                    # share an epoch with Python's time.time(). Compare the two
                    # entirely within that same clock domain rather than mixing them.
                    action_age = drift_status.get("t", 0) - drift_status.get("last_action_at", 0)
                    in_drift_grace = 0 <= action_age < DRIFT_ACTION_GRACE_SECS
                if gap > GAP_SANITY_CAP_SECS or in_drift_grace:
                    pass  # ignore this reading entirely - don't affect either streak
                else:
                    if DELAY_RESTART_DIAGNOSTIC_MODE:
                        try:
                            with open(DELAY_DIAGNOSTIC_LOG_PATH, 'a') as f:
                                f.write(f"{utc_now_iso()}  gap={gap:.1f}s\n")
                        except OSError:
                            pass
                    if gap >= DELAY_THRESHOLD_SECS:
                        high_delay_streak += 1
                    else:
                        high_delay_streak = 0
                    if gap >= DELAY_EMERGENCY_THRESHOLD_SECS:
                        emergency_delay_streak += 1
                    else:
                        emergency_delay_streak = 0

            restart_reason = None
            restart_category = None
            if len(recent_error_times) >= ERROR_THRESHOLD:
                actual_span = max(recent_error_times) - min(recent_error_times)
                if HEVC_RESTART_DIAGNOSTIC_MODE:
                    try:
                        with open(HEVC_DIAGNOSTIC_LOG_PATH, 'a') as f:
                            f.write(f"{utc_now_iso()}  WOULD HAVE RESTARTED HERE - "
                                    f"{len(recent_error_times)} errors, span {actual_span:.1f}s "
                                    f"(diagnostic mode: not actually restarting)\n")
                    except OSError:
                        pass
                    recent_error_times = []  # matches normal post-restart reset, so we keep
                                               # measuring fresh clusters rather than one giant one
                else:
                    restart_reason = (f"{len(recent_error_times)} HEVC reference errors "
                                       f"in {ERROR_WINDOW:.0f}s window (actual span: {actual_span:.1f}s)")
                    restart_category = "decoder_hevc_errors"
            elif emergency_delay_streak >= DELAY_EMERGENCY_CONSECUTIVE_CHECKS:
                # Safety net - always active regardless of diagnostic mode, so
                # testing can never leave the picture stuck indefinitely.
                restart_reason = (f"playback delay stayed >= {DELAY_EMERGENCY_THRESHOLD_SECS:.0f}s "
                                   f"(EMERGENCY threshold) for {emergency_delay_streak} consecutive checks")
                restart_category = "decoder_playback_delay_emergency"
            elif high_delay_streak >= DELAY_CONSECUTIVE_CHECKS:
                if DELAY_RESTART_DIAGNOSTIC_MODE:
                    try:
                        with open(DELAY_DIAGNOSTIC_LOG_PATH, 'a') as f:
                            f.write(f"{utc_now_iso()}  WOULD HAVE RESTARTED HERE - delay stayed "
                                    f">= {DELAY_THRESHOLD_SECS:.0f}s for {high_delay_streak} "
                                    f"consecutive checks (diagnostic mode: not actually restarting)\n")
                    except OSError:
                        pass
                    high_delay_streak = 0  # matches normal post-restart reset
                else:
                    restart_reason = (f"playback delay stayed >= {DELAY_THRESHOLD_SECS:.0f}s for "
                                       f"{high_delay_streak} consecutive checks")
                    restart_category = "decoder_playback_delay"

            if restart_reason:
                now2 = time.time()
                if now2 < breaker_tripped_until:
                    # Circuit breaker is active - skip restarting, but
                    # still clear the triggering condition's own state
                    # so we don't re-trip the instant the cooldown ends
                    # on stale, already-counted evidence.
                    recent_error_times = []
                    high_delay_streak = 0
                    emergency_delay_streak = 0
                elif tune_lock.acquire(timeout=2):
                    try:
                        print(f"[mpv_decoder_health] {restart_reason} - restarting mpv for a fresh decoder")
                        start_transition_cover()
                        time.sleep(0.3)
                        if diversity_enabled:
                            div_cfg = config['diversity']
                            restart_mpv(f"udp://@:{div_cfg['combiner_out_port']}")
                        else:
                            cfg = config['picotuner']
                            restart_mpv(f"udp://@:{cfg['ts_port']}")
                        rendering_confirmed = wait_for_mpv_rendering()  # real rendering, not a guess
                        record_diagnostic_event(restart_category, restart_reason)
                        if rendering_confirmed:
                            # Same safety margin as the stream-mode restart
                            # path (§5.5) - mpv's log confirming rendering
                            # doesn't guarantee the compositor has actually
                            # painted a frame yet (a sub-0.5s gap). Unlike
                            # the initial lock-triggered start,
                            # mpv_running_for_rf is already True and never
                            # changes here, so there's no incidental
                            # protection from the overlay's own status-poll
                            # staleness - confirmed live as a genuine,
                            # repeatable desktop flash without this.
                            time.sleep(0.3)
                            end_transition_cover()
                        else:
                            # Confirmed live as a genuine bug when this return
                            # value was ignored: the cover would come off
                            # anyway, exposing the desktop/terminal until mpv
                            # eventually caught up. Leaving the cover up here
                            # means the NEXT decoder-health check (or the RF
                            # lifecycle monitor, or drift monitor) picks this
                            # up rather than silently showing a blank/stale
                            # screen behind an already-removed cover.
                            print("[mpv_decoder_health] mpv did not confirm rendering in time - "
                                  "keeping the cover up")
                            record_diagnostic_event("decoder_render_not_confirmed",
                                              f"restart for '{restart_reason}' did not confirm "
                                              f"rendering within the timeout", count_as_mpv_restart=False)

                        recent_restart_times.append(now2)
                        recent_restart_times = [t for t in recent_restart_times if now2 - t <= CIRCUIT_BREAKER_WINDOW]
                        if len(recent_restart_times) >= CIRCUIT_BREAKER_THRESHOLD:
                            breaker_tripped_until = now2 + CIRCUIT_BREAKER_COOLDOWN
                            recent_restart_times = []
                            print(f"[mpv_decoder_health] Circuit breaker tripped - "
                                  f"{CIRCUIT_BREAKER_THRESHOLD} restarts within "
                                  f"{CIRCUIT_BREAKER_WINDOW:.0f}s clearly aren't resolving "
                                  f"whatever's actually wrong (likely upstream of mpv) - "
                                  f"backing off for {CIRCUIT_BREAKER_COOLDOWN:.0f}s")
                            record_diagnostic_event("decoder_circuit_breaker_tripped",
                                                     f"{CIRCUIT_BREAKER_THRESHOLD} restarts in "
                                                     f"{CIRCUIT_BREAKER_WINDOW:.0f}s - backing off "
                                                     f"{CIRCUIT_BREAKER_COOLDOWN:.0f}s",
                                                     count_as_mpv_restart=False)
                        last_size = 0  # restart_mpv() genuinely truncated the log - reset tracking to match
                    finally:
                        tune_lock.release()
                recent_error_times = []
                high_delay_streak = 0
                emergency_delay_streak = 0
        except Exception as e:
            print(f"[mpv_decoder_health] error: {e}")

def _get_process_rss_mb(pid: int):
    """Reads a process's resident set size in MB directly from /proc -
    no external dependency (e.g. psutil) needed for this."""
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None

def _find_overlay_pid():
    try:
        out = subprocess.run(['pgrep', '-f', 'lynx_overlay.py'],
                              capture_output=True, text=True, timeout=2)
        pids = [p for p in out.stdout.strip().split('\n') if p]
        return int(pids[0]) if pids else None
    except Exception:
        return None

def memory_rss_monitor():
    """Background thread: periodically logs this process's own RSS and
    the overlay's RSS. Confirmed useful directly: a single, one-off `ps`
    snapshot can show current size but not growth - there's no way to
    tell "always been this size" from "climbing since boot" without an
    earlier data point already on record. This gives every future
    memory-leak question a real trend to look back at, rather than
    needing a fresh measurement with nothing to compare it against."""
    INTERVAL_SECS = 300  # 5 minutes - frequent enough to catch a real
                         # trend developing, infrequent enough not to
                         # add noise to the log
    self_pid = os.getpid()
    while True:
        time.sleep(INTERVAL_SECS)
        try:
            app_rss = _get_process_rss_mb(self_pid)
            overlay_pid = _find_overlay_pid()
            overlay_rss = _get_process_rss_mb(overlay_pid) if overlay_pid else None
            app_str = f"{app_rss:.1f}MB" if app_rss is not None else "unavailable"
            overlay_str = f"{overlay_rss:.1f}MB" if overlay_rss is not None else "unavailable"
            print(f"[memory_rss] lynx_app.py: {app_str}  overlay: {overlay_str}")
        except Exception as e:
            print(f"[memory_rss] monitor error: {type(e).__name__}: {e}")

DIAL_DISCOVERY_PORT = 9998
DIAL_DISCOVERY_MAGIC = "LYNX_DISCOVER_V1"

def _get_local_ip():
    """Best-effort local IP for the discovery response - opens a UDP
    socket 'connected' to a public address (no packets actually sent
    for UDP connect()) purely to ask the OS which local IP would be
    used for outbound traffic. The standard, portable way to find this
    without parsing interface lists directly."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()

def dial_discovery_responder():
    """Background thread: answers M5Dial auto-discovery requests.
    Listens for a UDP broadcast carrying the fixed magic string below,
    and replies - unicast, straight back to the sender's own address,
    not broadcast - with this receiver's name, callsign, IP, and
    actual configured API port, so a Dial on the same subnet can find
    and start polling Lynx without any manual IP entry.

    Deliberately minimal: checks the magic string for an exact match
    and ignores anything else outright, rather than attempting to
    parse arbitrary broadcast traffic that happens to land on this
    port - matches the same discipline used for the Picotuner's own
    $-field parsing elsewhere in this file."""
    sock = None
    while True:
        try:
            if sock is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(5)
                sock.bind(('', DIAL_DISCOVERY_PORT))
            data, addr = sock.recvfrom(1024)
            if data.decode(errors='replace').strip() != DIAL_DISCOVERY_MAGIC:
                continue  # not a genuine discovery request - ignore silently
            web_cfg = config.get('web', {})
            response = json.dumps({
                "name": config.get('site', {}).get('name', 'Lynx Receiver'),
                "callsign": config.get('site', {}).get('callsign', ''),
                "ip": _get_local_ip(),
                "api_port": web_cfg.get('port', 8080),
            })
            sock.sendto(response.encode(), addr)
            print(f"[dial_discovery] answered a discovery request from {addr[0]}")
        except socket.timeout:
            pass
        except Exception as e:
            print(f"[dial_discovery] error: {type(e).__name__}: {e}")
            if sock:
                try: sock.close()
                except: pass
                sock = None
            time.sleep(1)

def mpv_drift_monitor():
    """Background thread: watches the drift-correction status file
    (written by lynx_drift_correction.lua) for state transitions and
    logs them to the same diagnostics timeline as everything else -
    a running commentary of when drift was detected and corrected,
    not just a snapshot of the current state. Nudges/drop-buffers are
    not counted as mpv restarts, since the process itself is never
    killed/relaunched for either.

    Also handles the hard-freeze signal directly: confirmed live that
    routing a genuine freeze through the full tiered drift-correction
    sequence (nudge -> drop-buffers -> breaker cooldown -> more
    nudging) before ever reaching a full restart added real, unhelpful
    delay - none of those steps can fix playback that isn't advancing
    at all. When the Lua script confirms a hard freeze, this restarts
    mpv immediately rather than waiting for the separate, slower
    playback-delay trigger to independently notice and confirm the
    same thing. Poll interval reduced from 2.0 to 1.0s given this now
    carries a time-critical signal, not just diagnostic logging."""
    POLL_SECS = 1.0
    last_nudge_active = False
    last_drop_buffers_count = 0
    last_breaker_active = False
    last_handled_hard_freeze_at = 0.0

    # Circuit breaker for the hard-freeze restart path. Thresholds are
    # now read fresh from config each time (see below) rather than
    # hardcoded, so they're tunable from /config without a restart.
    # Confirmed live (2026-07-21): the original fixed-cooldown design
    # suppressed every restart for ~6 minutes while the combiner's own
    # output had already been fully clean for over a minute - once
    # tripped, this now checks that condition before each retry rather
    # than blindly waiting out a fixed timer either way.
    recent_hard_freeze_restarts = []
    hard_freeze_breaker_tripped_until = 0.0
    last_condition_retry_at = 0.0  # floor for how often a condition-based
                                     # early retry can happen, even if the
                                     # combiner looks clean throughout -
                                     # guards against a tight loop if that
                                     # signal itself is flapping

    while True:
        time.sleep(POLL_SECS)
        try:
            status = get_mpv_drift_status()
            if status is None:
                continue

            nudge_active = status.get("nudge_active", False)
            drift_secs = status.get("estimated_drift_secs", 0.0)
            drop_buffers_count = status.get("drop_buffers_count", 0)
            breaker_active = status.get("breaker_active", False)
            hard_freeze_detected_at = status.get("hard_freeze_detected_at", 0.0)

            if nudge_active and not last_nudge_active:
                record_diagnostic_event("drift_nudge_started",
                                         f"estimated drift {drift_secs:.2f}s - speed nudged to catch up",
                                         count_as_mpv_restart=False)
            elif not nudge_active and last_nudge_active:
                record_diagnostic_event("drift_nudge_stopped",
                                         "caught up to live - speed back to normal",
                                         count_as_mpv_restart=False)
            last_nudge_active = nudge_active

            if drop_buffers_count > last_drop_buffers_count:
                fired = drop_buffers_count - last_drop_buffers_count
                record_diagnostic_event("drift_drop_buffers",
                                         f"fired {fired} time(s) - drift exceeded the nudge-only "
                                         f"threshold, forced an immediate resync",
                                         count_as_mpv_restart=False)
            last_drop_buffers_count = drop_buffers_count

            if breaker_active and not last_breaker_active:
                record_diagnostic_event("drift_breaker_tripped",
                                         "repeated drop-buffers calls weren't resolving the "
                                         "issue - suppressed for a cooldown, external restart "
                                         "monitor takes over if needed",
                                         count_as_mpv_restart=False)
            last_breaker_active = breaker_active

            if (hard_freeze_detected_at > 0 and
                    hard_freeze_detected_at != last_handled_hard_freeze_at and
                    current_mode == "rf" and mpv_running_for_rf):
                now2 = time.time()
                div_cfg = config.get('diversity', {})
                breaker_enabled = div_cfg.get('hard_freeze_breaker_enabled', True)
                breaker_threshold = div_cfg.get('hard_freeze_breaker_threshold', 5)
                breaker_window = div_cfg.get('hard_freeze_breaker_window_secs', 300.0)
                breaker_cooldown = div_cfg.get('hard_freeze_breaker_cooldown_secs', 300.0)
                required_clean = div_cfg.get('hard_freeze_breaker_required_clean_secs', 2.0)
                min_retry_interval = div_cfg.get('hard_freeze_breaker_min_retry_interval_secs', 5.0)

                should_suppress = False
                early_retry = False
                if breaker_enabled and now2 < hard_freeze_breaker_tripped_until:
                    # Nominally tripped - but check the combiner's own
                    # tight, immediate signal before blindly suppressing.
                    # Genuinely clean output ends this early; anything
                    # else (or retrying too soon) keeps it suppressed.
                    stats = read_diversity_stats() if diversity_enabled else None
                    seconds_clean = stats.get('seconds_since_bad_segment') if stats else None
                    condition_met = seconds_clean is not None and seconds_clean >= required_clean
                    rate_limited = (now2 - last_condition_retry_at) < min_retry_interval
                    if condition_met and not rate_limited:
                        early_retry = True
                        last_condition_retry_at = now2
                    else:
                        should_suppress = True

                if should_suppress:
                    detail = ("hard freeze detected but the restart breaker is active - "
                              "repeated restarts weren't resolving this")
                    stats = read_diversity_stats() if diversity_enabled else None
                    seconds_clean = stats.get('seconds_since_bad_segment') if stats else None
                    if seconds_clean is not None:
                        detail += f" (combiner output clean for {seconds_clean:.1f}s, needs {required_clean:.1f}s)"
                    record_diagnostic_event("drift_hard_freeze_suppressed", detail,
                                             count_as_mpv_restart=False)
                elif tune_lock.acquire(timeout=2):
                    try:
                        if early_retry:
                            record_diagnostic_event("drift_hard_freeze_early_retry",
                                                     "restart breaker nominally still active, but the "
                                                     "combiner's own output has genuinely been clean for "
                                                     f"at least {required_clean:.1f}s - retrying now rather "
                                                     "than waiting out the rest of the cooldown")
                        else:
                            record_diagnostic_event("drift_hard_freeze_restart",
                                                     "playback stopped advancing entirely - "
                                                     "restarting immediately rather than waiting on "
                                                     "the slower playback-delay trigger")
                        start_transition_cover()
                        time.sleep(0.3)
                        if diversity_enabled:
                            div_cfg2 = config['diversity']
                            restart_mpv(f"udp://@:{div_cfg2['combiner_out_port']}")
                        else:
                            cfg = config['picotuner']
                            restart_mpv(f"udp://@:{cfg['ts_port']}")
                        rendering_confirmed = wait_for_mpv_rendering()  # real rendering, not a guess
                        if rendering_confirmed:
                            # Same safety margin as the stream-mode restart
                            # path (§5.5) and the decoder-health path above -
                            # confirmed live twice tonight as the direct
                            # source of a real, repeatable desktop flash
                            # without this: mpv_running_for_rf stays True
                            # throughout a hard-freeze restart, so the
                            # overlay's own status-poll staleness gives no
                            # incidental protection here at all.
                            time.sleep(0.3)
                            end_transition_cover()
                        else:
                            print("[mpv_drift] mpv did not confirm rendering in time after "
                                  "hard-freeze restart - keeping the cover up")
                            record_diagnostic_event("hard_freeze_render_not_confirmed",
                                              "restart did not confirm rendering within the timeout",
                                              count_as_mpv_restart=False)

                        if breaker_enabled:
                            recent_hard_freeze_restarts.append(now2)
                            recent_hard_freeze_restarts[:] = [
                                t for t in recent_hard_freeze_restarts
                                if now2 - t <= breaker_window]
                            if len(recent_hard_freeze_restarts) >= breaker_threshold:
                                hard_freeze_breaker_tripped_until = now2 + breaker_cooldown
                                recent_hard_freeze_restarts.clear()
                                record_diagnostic_event("drift_hard_freeze_breaker_tripped",
                                                         f"{breaker_threshold} hard-freeze "
                                                         f"restarts within {breaker_window:.0f}s "
                                                         f"- backing off for up to {breaker_cooldown:.0f}s "
                                                         f"(sooner if the combiner's own output confirms "
                                                         f"it's genuinely clean again)",
                                                         count_as_mpv_restart=False)
                    finally:
                        tune_lock.release()
                last_handled_hard_freeze_at = hard_freeze_detected_at
        except Exception as e:
            print(f"[mpv_drift_monitor] error: {e}")

def picotuner_modcod_monitor():
    """Background thread: logs modcod changes for both tuners to the
    diagnostics timeline. Exists specifically to test a live hypothesis:
    that a slow lock after a long idle period is caused by the
    Picotuner's own demodulator falsely locking onto an unexpected
    modcod (e.g. 8PSK/16APSK rather than the expected QPSK) while
    scanning with no real signal present, then taking longer than
    usual to recover from that false lock. This doesn't fix anything -
    the modcod detection itself is entirely inside the Picotuner's own
    firmware, outside anything Lynx controls - but it turns "what did
    the OSD seem to show" into a concrete, timestamped sequence."""
    POLL_SECS = 1.0
    last_modcod_a = None
    last_modcod_b = None

    while True:
        time.sleep(POLL_SECS)
        try:
            modcod_a = picotuner_state.get("modcod") or None
            modcod_b = picotuner_state_b.get("modcod") or None

            if modcod_a and modcod_a != last_modcod_a:
                record_diagnostic_event("modcod_change_a", f"Tuner A: {modcod_a}",
                                         count_as_mpv_restart=False)
                last_modcod_a = modcod_a
            if modcod_b and modcod_b != last_modcod_b:
                record_diagnostic_event("modcod_change_b", f"Tuner B: {modcod_b}",
                                         count_as_mpv_restart=False)
                last_modcod_b = modcod_b
        except Exception as e:
            print(f"[picotuner_modcod] error: {e}")

def picotuner_connectivity_monitor():
    """Background thread: tracks Picotuner online/offline transitions
    (its status broadcast stopping/resuming entirely - a more severe
    condition than just losing signal lock) with the same hysteresis
    pattern as the overlay's own ONLINE_STABLE_POLLS, and logs
    transitions to the same diagnostics timeline as mpv events.

    Directly addresses a live-observed gap: a Wi-Fi connectivity
    event's full progression (lock lost -> brief recovery -> HEVC
    errors -> Picotuner offline) could previously only be reconstructed
    by manually cross-referencing what appeared on the OSD against the
    mpv event log by eye. With this, the connectivity event itself
    lands on the same timeline, so cause and effect are visible
    directly rather than inferred after the fact."""
    ONLINE_STABLE_POLLS = 3
    POLL_SECS = 2
    online_streak = 0
    offline_streak = 0
    known_online = True  # optimistic start - avoids a spurious "went offline"
                          # event firing before the very first real poll lands

    while True:
        time.sleep(POLL_SECS)
        try:
            raw_online = picotuner_state.get("online", False)
            if raw_online:
                online_streak += 1
                offline_streak = 0
                if online_streak >= ONLINE_STABLE_POLLS and not known_online:
                    record_diagnostic_event("picotuner_online", "status broadcast resumed",
                                             count_as_mpv_restart=False)
                    known_online = True
            else:
                offline_streak += 1
                online_streak = 0
                if offline_streak >= ONLINE_STABLE_POLLS and known_online:
                    record_diagnostic_event("picotuner_offline", "status broadcast stopped",
                                             count_as_mpv_restart=False)
                    known_online = False
        except Exception as e:
            print(f"[picotuner_connectivity] error: {e}")

def mer_publisher():
    """Background thread: periodically writes both tuners' current MER
    to a shared file for the combiner (a separate process) to read.

    Used only for the diversity combiner's MER tie-break: when both
    sources are clean for a given segment (a genuine tie), it prefers
    the stronger signal rather than whichever happened to arrive
    fractionally first. The fast, per-segment clean/error check
    remains the primary, unchanged mechanism for everything else -
    this only ever resolves an otherwise-arbitrary tie.

    Deliberately a separate, simple thread rather than added to the
    existing, already-complex monitor threads - keeps this narrow and
    easy to reason about independently."""
    while True:
        time.sleep(1.0)
        try:
            def to_float(s):
                try:
                    return float(s) if s not in (None, '') else None
                except (ValueError, TypeError):
                    return None
            payload = {
                "mer_a": to_float(picotuner_state.get("mer")),
                "mer_b": to_float(picotuner_state_b.get("mer")),
                "t": time.time(),
            }
            tmp_path = MER_PUBLISH_PATH + ".tmp"
            with open(tmp_path, 'w') as f:
                json.dump(payload, f)
            os.replace(tmp_path, MER_PUBLISH_PATH)  # atomic - the combiner never sees a partial write
        except Exception as e:
            print(f"[mer_publisher] error: {e}")

def rf_mpv_lifecycle_monitor():
    """Background thread, RF mode only: mpv is only ever STARTED once a
    signal lock has been confirmed stable for a few seconds, and is
    STOPPED again once loss of lock has been confirmed stable.

    This replaces an earlier, more reactive fix (restarting mpv after
    the fact once it was found stuck) with an architectural one: mpv is
    now never left running against a dead/no-signal stream for any
    extended period at all, which eliminates the underlying condition
    that let its own MPEG-TS demuxer get confused in the first place
    (confirmed live: mpv sat with no genuine input for 90+ minutes, then
    failed to properly re-probe once a real signal resumed, silently
    displaying stale, hours-old content despite the combiner and tuner
    status both being genuinely healthy the whole time).

    It also means the overlay's opaque cover - which already displays
    all the useful status/metadata (callsign, MER, modcod) during
    weak-signal acquisition on its own, independent of mpv - is
    genuinely the only thing ever shown until mpv has actually started
    for real, never racing with or exposing whatever mpv last decoded.

    _tune_impl() no longer starts mpv itself for RF tunes; it now just
    ensures nothing is left running against a now-stale target, clears
    mpv_running_for_rf, and leaves the cover up - this function owns
    starting mpv (and clearing the cover) from that point on.
    """
    global mpv_running_for_rf, mpv_last_started_at
    LOCK_CONFIRM_POLLS = 3   # ~3s of sustained lock before starting mpv - avoids reacting to a brief false lock
    LOSS_CONFIRM_POLLS = 3   # ~3s of sustained loss before stopping mpv - avoids stopping on a brief, normal fade.
                               # Was ~6s (POLL_SECS=2) - live testing with a co-channel interference edge case
                               # (two overlapping transmissions) showed the receiver can flicker between locked/
                               # unlocked fast enough that the unbroken streak this requires never completed,
                               # leaving mpv stuck on stale content with neither a stop nor a fresh restart ever
                               # triggering. A shorter window helps but doesn't fully close this if flickering is
                               # faster than even this - worth further testing per Justin's plan.
    POLL_SECS = 1

    lock_streak = 0
    loss_streak = 0

    while True:
        time.sleep(POLL_SECS)
        try:
            if current_mode != "rf":
                lock_streak = 0
                loss_streak = 0
                mpv_running_for_rf = False  # streaming/idle modes manage mpv themselves
                continue

            raw_locked = picotuner_state.get("locked", False) or \
                         (diversity_enabled and picotuner_state_b.get("locked", False))

            if raw_locked:
                loss_streak = 0
                lock_streak += 1
                if lock_streak >= LOCK_CONFIRM_POLLS and not mpv_running_for_rf:
                    if tune_lock.acquire(timeout=2):
                        try:
                            print(f"[rf_mpv_lifecycle] Confirmed lock after "
                                  f"{lock_streak * POLL_SECS}s - starting mpv")
                            if diversity_enabled:
                                div_cfg = config['diversity']
                                restart_mpv(f"udp://@:{div_cfg['combiner_out_port']}")
                            else:
                                cfg = config['picotuner']
                                restart_mpv(f"udp://@:{cfg['ts_port']}")
                            rendering_confirmed = wait_for_mpv_rendering()  # real rendering, not a guess
                            if rendering_confirmed:
                                # Same safety margin as the other two RF
                                # restart paths and stream mode (§5.5). This
                                # path is currently, incidentally protected
                                # anyway - mpv_running_for_rf transitions
                                # False->True here, and only reaches the
                                # overlay on its next status poll, which
                                # happens to buy enough time on its own. But
                                # that's a side effect of poll timing, not a
                                # deliberate guarantee, so this makes it
                                # correct by design rather than by accident.
                                time.sleep(0.3)
                                end_transition_cover()
                                mpv_running_for_rf = True
                                mpv_last_started_at = time.time()
                                record_diagnostic_event("rf_lock_confirmed_start",
                                                  f"after {lock_streak * POLL_SECS}s idle")
                            else:
                                # mpv never confirmed real rendering within the
                                # timeout - confirmed live as a genuine bug when
                                # this was ignored: the cover would come off
                                # anyway, exposing the desktop/terminal
                                # underneath until mpv eventually caught up.
                                # Leaving mpv_running_for_rf False here means
                                # the next poll (still locked) naturally
                                # retries the whole start sequence instead,
                                # with the cover staying up throughout.
                                print("[rf_mpv_lifecycle] mpv did not confirm rendering in time - "
                                      "keeping the cover up and retrying next poll")
                                record_diagnostic_event("rf_lock_render_not_confirmed",
                                                  "mpv started but did not confirm rendering within "
                                                  "the timeout - will retry", count_as_mpv_restart=False)
                        finally:
                            tune_lock.release()
                    # If the lock was busy, a user-initiated tune/stream
                    # switch is already in progress and will itself
                    # establish the correct mpv state - try again next poll.
            else:
                lock_streak = 0
                loss_streak += 1
                if loss_streak >= LOSS_CONFIRM_POLLS and mpv_running_for_rf:
                    if tune_lock.acquire(timeout=2):
                        try:
                            print("[rf_mpv_lifecycle] Confirmed loss of lock - stopping mpv "
                                  "rather than leaving it running with no data")
                            start_transition_cover()
                            kill_mpv()
                            mpv_running_for_rf = False
                            record_diagnostic_event("rf_loss_confirmed_stop",
                                              f"after {loss_streak * POLL_SECS}s of confirmed loss")
                        finally:
                            tune_lock.release()
        except Exception as e:
            print(f"[rf_mpv_lifecycle] error: {e}")

def picotuner_quality_monitor():
    """Background thread: reads rich status from Picotuner port 9901.

    Originally filtered strictly for $0,1 (RX=1's own report) — this
    port was assumed to only ever carry rcv=1's data based on a short
    (20s) capture window, but confirmed via live comparison against
    the table-format broadcast that rcv=2's own report DOES
    occasionally arrive here too, and this function had no defense
    against silently adopting it into tuner A's state. That's exactly
    what was happening: tuner A's displayed MER/margin intermittently
    matched tuner B's real values precisely, traced directly to a
    live side-by-side comparison of raw broadcast data against the
    API's own output. $0,2 is now handled deliberately rather than
    rejected, but narrowly - see below.

    ptwh0v3k+ (2026-07-23): confirmed live via tcpdump against
    genuinely flashed hardware (not assumed from documentation alone)
    that this same port now also carries two further things: tuner
    B's own full report ($0,2, previously discarded outright by the
    old filter), and a separate, much faster (~125ms vs 500ms) update
    carrying just lock state, AGC1/AGC2, and dBm for whichever
    receiver $77 identifies ($0,0 - "receiver 0" is the firmware's own
    marker for this special, not-a-normal-report packet type)."""
    global picotuner_state, picotuner_state_b
    cfg = config['picotuner']
    sock = None
    while True:
        try:
            if sock is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                sock.settimeout(5)
                sock.bind(('', cfg['status_port'] - 96))  # 9997-96 = 9901
            data, _ = sock.recvfrom(4096)
            fields = {}
            for line in data.decode(errors='replace').splitlines():
                line = line.strip()
                if line.startswith('$'):
                    parts = line.split(',', 1)
                    if len(parts) == 2:
                        fields[parts[0]] = parts[1].strip()
            if not fields:
                continue

            rx_id = fields.get('$0')

            if rx_id == '1':
                picotuner_state["mer"]         = fields.get('$12', '')
                picotuner_state["symbol_rate"] = fields.get('$9', '')
                picotuner_state["margin"]      = fields.get('$30', '')
                picotuner_state["programme"]   = fields.get('$14', '').replace('_', ' ')
                picotuner_state["modcod"]      = fields.get('$18', '')
                picotuner_state["codec"]       = fields.get('$31', '')
                picotuner_state["audio_codec"] = fields.get('$34', '')
                picotuner_state["level"]       = fields.get('$15', '')
                if '$85' in fields: picotuner_state["dbm"]  = fields['$85']
                if '$26' in fields: picotuner_state["agc1"] = fields['$26']
                if '$27' in fields: picotuner_state["agc2"] = fields['$27']

            elif rx_id == '2':
                # Deliberately narrow, matching the same principle
                # already established for tuner B elsewhere in this
                # file: only fields genuinely new here, not
                # mer/margin/modcod/etc, which continue to come from
                # the existing, working table-format port (9904) as
                # before - not touching a path that already works.
                # "programme" is a genuine addition, not a duplicate -
                # confirmed present in the live capture ($14) and
                # never available for rcv=2 from the table format at
                # all (it has no such column), unlike dBm/AGC below.
                if '$14' in fields: picotuner_state_b["programme"] = fields['$14'].replace('_', ' ')
                if '$85' in fields: picotuner_state_b["dbm"]  = fields['$85']
                if '$26' in fields: picotuner_state_b["agc1"] = fields['$26']
                if '$27' in fields: picotuner_state_b["agc2"] = fields['$27']

            elif rx_id == '0':
                receiver_num = fields.get('$77')
                target = (picotuner_state if receiver_num == '1' else
                          picotuner_state_b if receiver_num == '2' else None)
                if target is not None:
                    if '$85' in fields: target["dbm"]  = fields['$85']
                    if '$26' in fields: target["agc1"] = fields['$26']
                    if '$27' in fields: target["agc2"] = fields['$27']
        except socket.timeout:
            pass
        except Exception:
            if sock:
                try: sock.close()
                except: pass
                sock = None
            time.sleep(1)

# ── Second tuner (rcv=2) state — Diversity mode only ─────────
# Populated by picotuner_monitor() above, which parses the RX2 line
# from the SAME port 9997 broadcast already used for rcv=1 — no
# separate monitor or port needed. Confirmed via tcpdump that rcv=2
# never sends the richer $-field report rcv=1 does (that appears to
# only be available for the primary receiver on this firmware), so
# this only has the same basic fields RX1/RX2 both provide: lock
# state, callsign, frequency. No MER/margin/modcod for tuner B — a
# genuine hardware/firmware limitation, not a bug to work around.
picotuner_state_b = {
    "online": False,
    "locked": False,          # from picotuner_monitor()'s RX2 line (port 9997) — that format cleanly reports search/lost states
    "callsign": "",
    "frequency": "",
    "last_seen": 0,
    # Richer fields below — from picotuner_table_monitor_b() (port
    # 9904), confirmed via live testing to carry both receivers'
    # full status together. Kept as a second monitor rather than
    # folded into picotuner_monitor() above, since 'locked' is more
    # reliably determined from the RX2 line's explicit search/lost
    # states — this table's own unlocked-row format hasn't been
    # confirmed, so it's used purely for the extra detail once we
    # already know from elsewhere that the receiver is locked.
    "mer": "",
    "margin": "",
    "symbol_rate": "",
    "modcod": "",
    "fec_profile": "",
    "codec": "",
    "audio_codec": "",
    "plug": "",
    "pct_nul": "",  # interim signal-quality proxy for B until Brian adds proper $15-equivalent level data to the firmware
    # ptwh0v3k+ (2026-07-23): this is that proper data, and better -
    # a real dBm value from the firmware's own look-up table, not just
    # a $15-equivalent. Confirmed live via tcpdump that tuner B's own
    # full report (marked $0,2) now arrives on the same $-field port
    # as tuner A's (9901) - previously discarded entirely by that
    # monitor's $0=='1' filter. Sourced from there, not this table
    # format, which was never extended to carry these fields.
    "dbm": "",
    "agc1": "",
    "agc2": "",
    "programme": "",  # same source as above ($14 in the $0,2 report) -
                      # genuinely new, not previously available for B
                      # from any source at all, unlike mer/margin/etc.
}

def picotuner_table_monitor_b():
    """Background thread: reads the rich table-format status from
    Picotuner port 9904 (a confirmed duplicate of 9902). Live-tested
    directly: this table contains BOTH receivers' rows together
    (RX 1 and RX 2), each with STATUS/CALLSIGN/MER/D(margin)/
    FREQUENCY/SR/MODULATION/FPRO/CODECS/ANT/PACKETS/%NUL/NIMTYPE/
    TS DESTINATION.

    Primarily extracts the RX=2 row for tuner B's rich stats (rcv=1's
    own equivalent monitor already exists separately, reading the
    $-field format on port 9901 — left untouched since it's confirmed
    working for callsign/frequency/programme/modcod).

    ALSO extracts RX=1's mer/margin specifically as a supplement for
    tuner A: confirmed live that the $-format source on port 9901 only
    reports once tuner A has at least some lock, leaving mer/margin
    empty while genuinely unlocked/searching - even though this same
    table broadcast already includes RX1's row the whole time (this is
    exactly why tuner B's MER/margin were showing on the "searching"
    overlay while tuner A's weren't, despite both being displayed with
    identical logic - the underlying data itself was asymmetric, not
    the display code). Deliberately narrow: only mer/margin for tuner
    A here, not the full field set, to avoid touching anything already
    reliably sourced elsewhere.
    """
    global picotuner_state_b
    cfg = config['picotuner']
    sock = None
    while True:
        try:
            if sock is None:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                sock.settimeout(5)
                sock.bind(('', cfg['status_port'] - 93))  # 9997-93 = 9904
            data, _ = sock.recvfrom(4096)
            text = data.decode(errors='replace')
            for line in text.splitlines():
                parts = line.split()
                # Data rows start with the RX number (1 or 2) — header
                # and separator rows don't parse as a leading digit,
                # so this alone is enough to skip them without needing
                # to match the header text itself.
                if not parts or not parts[0].isdigit():
                    continue
                if parts[0] == '1' and len(parts) >= 16:
                    # Supplement tuner A's mer/margin only - see
                    # docstring above for why this specific gap exists.
                    picotuner_state["mer"] = parts[3]
                    picotuner_state["margin"] = parts[4]
                    continue
                if parts[0] != '2' or len(parts) < 16:
                    continue
                # Column indices confirmed directly against real
                # captured output before deployment — see the
                # verification test this was built from.
                picotuner_state_b["mer"] = parts[3]
                picotuner_state_b["margin"] = parts[4]
                picotuner_state_b["symbol_rate"] = parts[6]
                picotuner_state_b["modcod"] = parts[7] + " " + parts[8]
                picotuner_state_b["fec_profile"] = parts[9]
                # CODECS column is combined video-audio, e.g.
                # "H265-AAC" — split on the first hyphen to match
                # tuner A's separate codec/audio_codec fields.
                codec_combined = parts[10]
                if '-' in codec_combined:
                    v, a = codec_combined.split('-', 1)
                    picotuner_state_b["codec"] = v
                    picotuner_state_b["audio_codec"] = a
                else:
                    picotuner_state_b["codec"] = codec_combined
                    picotuner_state_b["audio_codec"] = ""
                picotuner_state_b["plug"] = parts[11]
                picotuner_state_b["pct_nul"] = parts[13]  # column index confirmed earlier: PACKETS(12) %NUL(13) NIMTYPE(14)
        except socket.timeout:
            pass
        except Exception:
            if sock:
                try: sock.close()
                except: pass
                sock = None
            time.sleep(1)

# ── Diversity combiner process management ────────────────────
diversity_enabled: bool = False

MAX_DIAGNOSTIC_EVENTS = 200
diagnostics = {
    "mpv_restarts_total": 0,
    "mpv_restarts_by_reason": {},
    "started_at": time.time(),
    "events": [],  # each: {"t": timestamp, "category": str, "detail": str}
}

def record_diagnostic_event(category: str, detail: str = "", count_as_mpv_restart: bool = True):
    """Records an event for the diagnostics page - mpv starts/stops/
    restarts (routine tunes, and the anomaly-driven restarts from
    rf_mpv_lifecycle_monitor and mpv_decoder_health_monitor), and other
    correlatable events like Picotuner connectivity, all land on the
    same timeline so cause and effect (e.g. a connectivity drop
    followed by decode errors) can be read directly rather than
    manually cross-referencing the OSD against separate logs.
    count_as_mpv_restart=False for non-mpv events, so the summary
    counter (labelled "mpv" in the UI) stays accurate."""
    now = time.time()
    if count_as_mpv_restart:
        diagnostics["mpv_restarts_total"] += 1
        diagnostics["mpv_restarts_by_reason"][category] = diagnostics["mpv_restarts_by_reason"].get(category, 0) + 1
    diagnostics["events"].append({"t": now, "category": category, "detail": detail})
    if len(diagnostics["events"]) > MAX_DIAGNOSTIC_EVENTS:
        diagnostics["events"] = diagnostics["events"][-MAX_DIAGNOSTIC_EVENTS:]
DIVERSITY_COMBINER_CMD = None  # subprocess.Popen handle, or None if not running
DIVERSITY_STATS_PATH = "/tmp/lynx_diversity_stats.json"

def start_diversity_combiner():
    """Launches diversity_combiner_pcr.py as a managed background
    process. Idempotent — calling this while already running is a
    safe no-op, matching the pattern used for mpv/ffmpeg elsewhere
    in this file.

    Defensively kills any orphaned instance first, regardless of
    what DIVERSITY_COMBINER_CMD's in-memory state says. The combiner
    is launched with os.setsid, detaching it into its own process
    group — if Lynx itself crashes or is restarted while the
    combiner is running, the combiner does NOT die with it and keeps
    holding its ports, but Lynx's own in-memory tracking of it is
    lost on restart either way. Without this, the next diversity
    launch attempt fails to bind those same ports with "Address
    already in use" — confirmed as a real, repeatable cause of
    diversity mode failing after any earlier crash."""
    global DIVERSITY_COMBINER_CMD
    if DIVERSITY_COMBINER_CMD is not None and DIVERSITY_COMBINER_CMD.poll() is None:
        return  # already running under our own tracking
    subprocess.run(["pkill", "-9", "-f", "diversity_combiner_pcr.py"], capture_output=True)
    time.sleep(0.3)  # let the OS actually release the ports before we try to bind them again
    cfg = config['picotuner']
    div_cfg = config['diversity']
    script_path = Path(__file__).parent / "diversity_combiner_pcr.py"
    cmd = (
        f"python3 -u {script_path} "
        f"--port-a {cfg['ts_port']} --port-b {cfg['ts_port_b']} "
        f"--out-ip 127.0.0.1 --out-port {div_cfg['combiner_out_port']} "
        f"--live-stats-file {DIVERSITY_STATS_PATH} --stats-interval 1.0 "
        f"--mer-switch-dwell-secs {div_cfg.get('mer_switch_dwell_secs', 10.0)} "
        f"--mer-switch-margin-db {div_cfg.get('mer_switch_margin_db', 1.0)}"
    )
    DIVERSITY_COMBINER_CMD = subprocess.Popen(
        cmd, shell=True,
        stdin=subprocess.DEVNULL,
        stdout=open("/tmp/diversity_combiner.log", "w"),
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid
    )

def stop_diversity_combiner():
    """Stops the combiner process if running. Safe no-op otherwise."""
    global DIVERSITY_COMBINER_CMD
    _kill_process_reliably(DIVERSITY_COMBINER_CMD, pkill_pattern="diversity_combiner_pcr.py")
    DIVERSITY_COMBINER_CMD = None
    # Stats file reflects a now-dead process — remove it rather than
    # leaving stale numbers behind for /api/status to keep reporting.
    try:
        os.remove(DIVERSITY_STATS_PATH)
    except FileNotFoundError:
        pass

def read_diversity_stats():
    """Returns the combiner's own live rolling-window stats, written
    to a small file once a second — see diversity_combiner_pcr.py.
    Returns None if diversity mode isn't active or the file isn't
    there yet (e.g. combiner only just started). Deliberately cheap:
    a single file existence check and read, no polling loop or
    socket of its own — reuses work the combiner was already doing
    for its own console output."""
    if not diversity_enabled:
        return None
    try:
        with open(DIVERSITY_STATS_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

# ── BATC stream cache ─────────────────────────────────────────
# Cached server-side to avoid hammering the BATC API.
# Refreshed on startup, manually via /api/streams/refresh,
# and automatically every hour.
BATC_CACHE_TTL = 3600  # seconds
_batc_cache: list = []
_batc_cache_time: float = 0

# ── Pydantic models ───────────────────────────────────────────
class TuneRequest(BaseModel):
    freq: int               # kHz — the REAL downlink/satellite frequency
                             # when lnb_lo_khz is set, otherwise the
                             # direct frequency to tune to
    sr: int                 # kS/s
    plug: str = "a"
    lnb_lo_khz: int = 0      # LNB local oscillator frequency in kHz.
                             # 0 = no LNB, freq is sent directly.
                             # When set, Lynx subtracts this from freq
                             # before sending to the Picotuner — e.g.
                             # a standard Ku-band LNB (9750000 kHz LO)
                             # downconverts a QO-100 downlink of
                             # 10489500 kHz to an IF of 739500 kHz,
                             # which is what the Picotuner actually
                             # needs to be tuned to.

class StreamRequest(BaseModel):
    url: str
    name: str = ""  # friendly display name, e.g. "GB3OO" — chosen by
                     # whoever initiated the stream, not inspected from
                     # the stream's own content. Shown on the OSD.

class PresetTuneRequest(BaseModel):
    name: str

class PresetSaveRequest(BaseModel):
    type: str = "rf"  # "rf" or "stream" - determines which fields below are used
    name: str = ""  # if blank, auto-generated from frequency (rf only)
    # RF fields (type="rf")
    freq: Optional[int] = None
    sr: Optional[int] = None
    plug: str = "a"
    lnb_lo_khz: int = 0
    # Stream field (type="stream")
    url: Optional[str] = None

class SiteConfigUpdate(BaseModel):
    name: str
    callsign: str
    location: str
    locator: str

class PicotunerConfigUpdate(BaseModel):
    host: str
    cmd_port: int
    cmd_port_b: int
    ts_port: int
    ts_port_b: int
    status_port: int

class DiversityConfigUpdate(BaseModel):
    mer_switch_dwell_secs: Optional[float] = None
    mer_switch_margin_db: Optional[float] = None
    hard_freeze_breaker_enabled: Optional[bool] = None
    hard_freeze_breaker_threshold: Optional[int] = None
    hard_freeze_breaker_window_secs: Optional[float] = None
    hard_freeze_breaker_cooldown_secs: Optional[float] = None
    hard_freeze_breaker_required_clean_secs: Optional[float] = None
    hard_freeze_breaker_min_retry_interval_secs: Optional[float] = None

class QrzConfigUpdate(BaseModel):
    enabled: bool
    api_key: str
    settle_secs: float
    suppress_mins: float
    portable_locator: str = ""

class SlackConfigUpdate(BaseModel):
    enabled: bool
    webhook_url: str
    settle_secs: float
    message_template: str

class CompanionConfigUpdate(BaseModel):
    enabled: bool
    lock_url: str
    lock_settle_secs: float
    unlock_url: str
    unlock_settle_secs: float
    gpio_enabled: bool
    gpio_pin: int
    gpio_polarity: str

class GpioTxConfigUpdate(BaseModel):
    enabled: bool
    pin: int
    polarity: str
    power_up_settle_secs: float
    power_down_settle_secs: float
    schedule_weekday_start: str
    schedule_weekday_end: str
    schedule_weekend_start: str
    schedule_weekend_end: str

class ConfigUpdateRequest(BaseModel):
    site: Optional[SiteConfigUpdate] = None
    picotuner: Optional[PicotunerConfigUpdate] = None
    diversity: Optional[DiversityConfigUpdate] = None
    notifications_qrz: Optional[QrzConfigUpdate] = None
    notifications_slack: Optional[SlackConfigUpdate] = None
    notifications_companion: Optional[CompanionConfigUpdate] = None
    notifications_gpio_tx: Optional[GpioTxConfigUpdate] = None

# ── Helpers ───────────────────────────────────────────────────
def stop_current():
    """Stop current stream/transcode/RF reception entirely - kills mpv
    and deliberately does NOT restart it, unlike every other tune/
    stream path in this file. This previously pointed mpv at the raw,
    single-tuner Picotuner UDP port (9941) instead of actually
    stopping anything - but the Picotuner is a hardware demodulator
    that keeps transmitting TS data on that port continuously
    regardless of what Lynx's own software does, so if that tuner was
    genuinely locked, mpv would just start playing its raw feed
    directly instead of stopping. Since one source usually dominates
    the diversity combiner's output anyway, this could look nearly
    identical to what was already on screen - confirmed live as the
    reason Stop appeared to do nothing at all. The overlay's own
    cover stays up as soon as current_mode is "idle" regardless
    (state["mode"] isn't "rf" or "stream"), which is exactly the
    intended, visible "nothing is playing" result."""
    global current_mode, current_preset, mpv_running_for_rf
    # One-time defensive sweep for any leftover ffmpeg transcode
    # process from before streams were switched to direct mpv
    # playback — harmless no-op once none remain.
    subprocess.run(["pkill", "-9", "-f", "ffmpeg.*udp://127.0.0.1:9945"], capture_output=True)
    subprocess.run(["pkill", "-9", "-f", "ffplay"], capture_output=True)
    stop_ffmpeg_bg()  # in case an old background stream is somehow still running
    current_mode = "idle"
    current_preset = ""
    mpv_running_for_rf = False

    def _stop_mpv():
        start_transition_cover()
        kill_mpv()  # deliberately NOT restart_mpv() - Stop means stop, not "switch to
                     # whatever the raw tuner happens to be showing"
        end_transition_cover()
    threading.Thread(target=_stop_mpv, daemon=True).start()

def fetch_batc_streams_from_api() -> list:
    """Fetch live streams directly from BATC API. Call sparingly."""
    url = "https://batc.org.uk/live-api/stream_list.php"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read().decode())
    results = []
    for category, streams in data.items():
        if not isinstance(streams, list):
            continue
        for s in streams:
            if not isinstance(s, dict) or not s.get('active'):
                continue
            if s.get('stream_listed') != '1':
                continue
            title = s.get('stream_title', '').strip()
            stream_url = s.get('stream_output_url', '').strip()
            if not title or not stream_url:
                continue
            results.append({
                "name": title,
                "url": f"rtmp://rtmp.batc.org.uk/live/{stream_url}",
                "repeater": s.get('stream_type_repeater', '0') == '1',
                "active": s.get('active')
            })
    results.sort(key=lambda x: (0 if x['repeater'] else 1, x['name']))
    return results

def get_batc_streams_cached() -> list:
    """Return cached BATC stream list, refreshing if stale (>1 hour)."""
    global _batc_cache, _batc_cache_time
    age = time.time() - _batc_cache_time
    if age > BATC_CACHE_TTL or not _batc_cache:
        try:
            _batc_cache = fetch_batc_streams_from_api()
            _batc_cache_time = time.time()
        except Exception as e:
            pass  # Return stale cache on error
    return _batc_cache
    """Send a command to the Picotuner."""
    cfg = config['picotuner']
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(cmd.encode(), (cfg['host'], cfg['cmd_port']))
    sock.close()

def ryde_cmd(request: dict) -> dict:
    """Send a command to the Ryde network interface.

    RYDE DISABLED (temporarily, at user request) — Ryde's own preset
    list was found to grow without bound over time (a leak on Ryde's
    own side, outside Lynx's visibility), and was crashing Ryde
    itself. Body commented out rather than removed, for easy
    re-enabling later if wanted.
    """
    raise HTTPException(status_code=503, detail="Ryde integration is currently disabled")
    # cfg = config['ryde']
    # try:
    #     with socket.create_connection((cfg['host'], cfg['port']), timeout=3) as s:
    #         s.sendall(json.dumps(request).encode())
    #         response = b""
    #         while True:
    #             chunk = s.recv(4096)
    #             if not chunk:
    #                 break
    #             response += chunk
    #             try:
    #                 return json.loads(response.decode())
    #             except json.JSONDecodeError:
    #                 continue
    # except Exception as e:
    #     raise HTTPException(status_code=503, detail=f"Ryde unavailable: {e}")

# ── API: Status ───────────────────────────────────────────────
def _compute_downlink_frequency():
    """When an LNB LO is in use, the Picotuner reports the L-band/IF
    frequency it's actually locked on (e.g. 739.500 MHz), not the real
    satellite downlink frequency. This reverses the LNB math to give
    the real-world figure for display (e.g. 10489.500 MHz for QO-100).
    Must match whichever injection side (low/high) was actually used
    at tune time — see current_lnb_side."""
    if not current_lnb_lo_khz:
        return None
    try:
        ifreq_mhz = float(picotuner_state["frequency"])
        lo_mhz = current_lnb_lo_khz / 1000
        if current_lnb_side == "high":
            # High-side injection (C-band): IF = LO - downlink,
            # so downlink = LO - IF
            return round(lo_mhz - ifreq_mhz, 3)
        else:
            # Low-side injection (Ku-band): IF = downlink - LO,
            # so downlink = IF + LO
            return round(ifreq_mhz + lo_mhz, 3)
    except (ValueError, TypeError):
        return None

@app.get("/api/status", tags=["Status"],
         summary="Get current receiver status",
         description="Returns lock state, MER, callsign, frequency and more. "
                     "Suitable for polling by Bitfocus Companion or the M5Dial.")
def get_status():
    status = {
        "lynx": {
            "mode": current_mode,
            "preset": current_preset,
            "stream_name": current_stream_name,
            "stream_info": get_live_stream_info() if current_mode == "stream" else None,
            "stream_protocol": get_stream_protocol(current_stream_url) if current_mode == "stream" and current_stream_url else None,
            "mpv_transitioning": mpv_transitioning,
            "mpv_running_for_rf": mpv_running_for_rf,
            "mpv_restarts_total": diagnostics["mpv_restarts_total"],
            "mpv_drift": get_mpv_drift_status(),
            "portable_locator": config.get('notifications', {}).get('qrz', {}).get('portable_locator', ''),
            "timestamp": utc_now_iso()
        },
        "picotuner": {
            "online": picotuner_state["online"],
            "locked": picotuner_state["locked"],
            "callsign": picotuner_state["callsign"],
            "frequency": picotuner_state["frequency"],
            "downlink_frequency": _compute_downlink_frequency(),
            "lnb_lo_khz": current_lnb_lo_khz,
            "symbol_rate": picotuner_state["symbol_rate"],
            "rx1": picotuner_state["rx1_raw"],
            "firmware": picotuner_state["firmware"],
            "last_seen": picotuner_state["last_seen"],
            "mer": picotuner_state["mer"],
            "margin": picotuner_state["margin"],
            "programme": picotuner_state["programme"],
            "modcod": picotuner_state["modcod"],
            "codec": picotuner_state["codec"],
            "audio_codec": picotuner_state["audio_codec"],
            "level": picotuner_state["level"],
            # ptwh0v3k+ (2026-07-23) - real dBm from the firmware's own
            # look-up table, plus raw AGC1/AGC2. Empty strings on older
            # firmware that doesn't send these fields.
            "dbm": picotuner_state["dbm"],
            "agc1": picotuner_state["agc1"],
            "agc2": picotuner_state["agc2"],
        },
        "diversity": {
            "enabled": diversity_enabled,
            # rcv=2's own native status — only meaningful while
            # diversity mode is active, but harmless to include the
            # (idle/offline) values otherwise rather than special-
            # casing the response shape. Rich fields confirmed
            # available for rcv=2 via the table-format broadcast on
            # port 9904 (live-tested directly) — an earlier version
            # of this comment incorrectly said only basic fields
            # were available; that was based on the wrong port.
            "tuner_b": {
                "online": picotuner_state_b["online"],
                "locked": picotuner_state_b["locked"],
                "callsign": picotuner_state_b["callsign"],
                "frequency": picotuner_state_b["frequency"],
                "mer": picotuner_state_b["mer"],
                "margin": picotuner_state_b["margin"],
                "symbol_rate": picotuner_state_b["symbol_rate"],
                "modcod": picotuner_state_b["modcod"],
                "codec": picotuner_state_b["codec"],
                "plug": picotuner_state_b["plug"],
                "audio_codec": picotuner_state_b["audio_codec"],
                "firmware": picotuner_state["firmware"],  # same physical unit, not per-receiver
                "pct_nul": picotuner_state_b["pct_nul"],
                "dbm": picotuner_state_b["dbm"],
                "agc1": picotuner_state_b["agc1"],
                "agc2": picotuner_state_b["agc2"],
                "programme": picotuner_state_b["programme"],
            },
            # Combiner's own live rolling-window stats (see
            # diversity_combiner_pcr.py) — None when not running.
            # Deliberately NOT the cumulative-since-start figures,
            # which get less representative of current conditions
            # the longer the combiner has been running.
            "stats": read_diversity_stats(),
        }
    }
    # Ryde status block — commented out, see ryde_cmd() docstring for why.
    # if config['ryde']['enabled']:
    #     try:
    #         ryde_status = ryde_cmd({"request": "getStatus"})
    #         status["ryde"] = ryde_status
    #     except Exception:
    #         status["ryde"] = {"available": False}
    
    return status

@app.get("/api/diagnostics", tags=["Status"],
         summary="Get mpv restart/stop diagnostics",
         description="Per-reason counters and a rolling log of recent mpv "
                     "start/stop events, for tracking down intermittent issues.")
def get_diagnostics():
    return {
        "started_at": diagnostics["started_at"],
        "mpv_restarts_total": diagnostics["mpv_restarts_total"],
        "mpv_restarts_by_reason": diagnostics["mpv_restarts_by_reason"],
        "events": list(reversed(diagnostics["events"])),  # newest first
    }

class QrzTestRequest(BaseModel):
    mode: str = "DVB-S2"
    test_callsign: str = "TESTQRZ"

@app.post("/api/qrz/test", tags=["Status"],
          summary="Send a test QRZ Logbook entry",
          description="Sends a real, clearly-marked test entry to QRZ Logbook "
                      "using the configured API key, and returns QRZ's own, full "
                      "response - the exact result/reason it gave, not just "
                      "success/failure. Useful for diagnosing why real logging "
                      "might be failing (e.g. a rejected mode value) without "
                      "waiting for a genuine RF lock or using a terminal. Uses a "
                      "clearly-marked test callsign so it's easy to spot and "
                      "delete from the real logbook afterwards.")
def qrz_test(req: QrzTestRequest):
    qrz_cfg = config.get('notifications', {}).get('qrz', {})
    api_key = qrz_cfg.get('api_key', '')
    if not api_key:
        raise HTTPException(status_code=400,
                             detail="No QRZ API key configured - set one on the Config page first")
    site_callsign = config.get('site', {}).get('callsign', '')
    result = lynx_notifications.submit_qrz_logbook(
        api_key, site_callsign, req.test_callsign, 437024,
        req.mode, "20", "5",  # freq_khz, mer, margin - realistic dummy test values
        comment_override="Lynx diagnostic test entry - safe to delete"
    )
    return result

@app.get("/diagnostics", response_class=HTMLResponse, include_in_schema=False)
def diagnostics_page():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Lynx Diagnostics</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background: #1a1a2e; color: #e0e0e0; }
        .card { background: #16213e; border: 1px solid #0f3460; color: #e0e0e0; }
        .card-header { background: #0f3460; color: #ffffff; font-weight: 500; }
        .lynx-title { color: #e94560; font-weight: bold; letter-spacing: 2px; }
        .text-muted { color: #a8b5c7 !important; }
        table { font-family: monospace; font-size: 0.9em; }
        .reason-badge { font-family: monospace; }
        #events-table td { vertical-align: top; }
        a { color: #00d4aa; }
    </style>
</head>
<body>
<div class="container-fluid py-3">
    <div class="row mb-3">
        <div class="col">
            <h2 class="lynx-title">&#x25B6; LYNX DIAGNOSTICS</h2>
            <small class="text-muted">mpv start/stop events - auto-refreshes every 5s. <a href="/">&larr; Back to receiver</a></small>
        </div>
    </div>

    <div class="card mb-3">
        <div class="card-header">Test QRZ Logging</div>
        <div class="card-body">
            <p class="text-muted small">Sends one real, clearly-marked test entry to your QRZ Logbook
                (callsign TESTQRZ, comment noting it's a diagnostic test - safe to delete afterwards),
                and shows QRZ's own, full response. Useful for checking your QRZ setup is genuinely
                working without waiting for a real RF lock. Uses whatever API key is currently
                configured on the Config page.</p>
            <button class="btn btn-outline-warning btn-sm" onclick="sendQrzTest()">Send Test Entry</button>
            <pre id="qrz-test-result" class="mt-3 mb-0 small" style="white-space: pre-wrap;"></pre>
        </div>
    </div>

    <div class="row g-3 mb-3">
        <div class="col-md-4">
            <div class="card">
                <div class="card-header">Total mpv events</div>
                <div class="card-body text-center">
                    <span style="font-size:2rem; font-family:monospace;" id="total-count">-</span>
                </div>
            </div>
        </div>
        <div class="col-md-8">
            <div class="card">
                <div class="card-header">By reason</div>
                <div class="card-body" id="by-reason">
                    <div class="text-muted">Loading...</div>
                </div>
            </div>
        </div>
    </div>

    <div class="card">
        <div class="card-header">Recent events (newest first, last 200 kept)</div>
        <div class="card-body p-0">
            <table class="table table-dark table-sm mb-0" id="events-table">
                <thead><tr><th style="width:180px">Time</th><th style="width:220px">Reason</th><th>Detail</th></tr></thead>
                <tbody><tr><td colspan="3" class="text-muted p-3">Loading...</td></tr></tbody>
            </table>
        </div>
    </div>
</div>

<script>
const REASON_LABELS = {
    "rf_lock_confirmed_start": "RF: lock confirmed, mpv started",
    "rf_loss_confirmed_stop": "RF: lock lost, mpv stopped",
    "decoder_hevc_errors": "Decoder: HEVC reference errors",
    "decoder_playback_delay": "Decoder: playback delay grew",
    "decoder_playback_delay_emergency": "Decoder: EMERGENCY delay threshold (diagnostic safety net)",
    "user_stream_start": "Stream started",
    "picotuner_offline": "Picotuner went offline",
    "picotuner_online": "Picotuner back online",
    "decoder_circuit_breaker_tripped": "Decoder: restarts not helping, backed off",
    "modcod_change_a": "Modcod changed",
    "modcod_change_b": "Modcod changed",
    "drift_nudge_started": "Drift correction: speed nudged up",
    "drift_nudge_stopped": "Drift correction: caught up, back to normal speed",
    "drift_drop_buffers": "Drift correction: drop-buffers resync",
    "drift_breaker_tripped": "Drift correction: repeated resyncs not helping, backed off",
    "drift_hard_freeze_restart": "Hard freeze detected - immediate mpv restart",
    "drift_hard_freeze_suppressed": "Hard freeze detected - restart breaker active, suppressed",
    "drift_hard_freeze_breaker_tripped": "Hard freeze restarts not helping, backed off",
    "notif_confirmed_lock": "Notifications: own lock confirmation (arms settle timers)",
    "notif_confirmed_unlock": "Notifications: own unlock confirmation (cancels pending timers)",
    "notif_action_cancelled": "Notifications: a pending action was cancelled before firing",
    "qrz_skipped": "QRZ: entry skipped",
    "qrz_logged": "QRZ: logged successfully",
    "qrz_failed": "QRZ: submission failed",
};

function fmtTime(t) {
    const d = new Date(t * 1000);
    return d.toLocaleString();
}

async function sendQrzTest() {
    const resultEl = document.getElementById('qrz-test-result');
    resultEl.textContent = 'Sending...';
    try {
        const r = await fetch('/api/qrz/test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({})
        });
        const data = await r.json();
        if (!r.ok) {
            resultEl.textContent = 'Error: ' + (data.detail || 'request failed');
            return;
        }
        resultEl.textContent =
            'result:       ' + data.result + '\\n' +
            'reason:       ' + data.reason + '\\n' +
            'logid:        ' + data.logid + '\\n' +
            'http_status:  ' + data.http_status + '\\n' +
            'mode_sent:    ' + data.mode_sent + '\\n' +
            'band_sent:    ' + data.band_sent + '\\n' +
            'raw_response: ' + data.raw_response;
    } catch (e) {
        resultEl.textContent = 'Request failed: ' + e;
    }
}

async function refresh() {
    try {
        const r = await fetch('/api/diagnostics');
        const data = await r.json();

        document.getElementById('total-count').textContent = data.mpv_restarts_total;

        const reasons = Object.entries(data.mpv_restarts_by_reason)
            .sort((a, b) => b[1] - a[1]);
        const byReasonEl = document.getElementById('by-reason');
        if (reasons.length === 0) {
            byReasonEl.innerHTML = '<div class="text-muted">No events yet.</div>';
        } else {
            byReasonEl.innerHTML = reasons.map(([reason, count]) =>
                `<div class="d-flex justify-content-between mb-1">
                    <span class="reason-badge">${REASON_LABELS[reason] || reason}</span>
                    <span class="badge bg-secondary">${count}</span>
                </div>`
            ).join('');
        }

        const tbody = document.querySelector('#events-table tbody');
        if (data.events.length === 0) {
            tbody.innerHTML = '<tr><td colspan="3" class="text-muted p-3">No events yet.</td></tr>';
        } else {
            tbody.innerHTML = data.events.map(ev =>
                `<tr>
                    <td>${fmtTime(ev.t)}</td>
                    <td>${REASON_LABELS[ev.category] || ev.category}</td>
                    <td class="text-muted">${ev.detail || ''}</td>
                </tr>`
            ).join('');
        }
    } catch (e) {
        console.error('Failed to load diagnostics', e);
    }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""

@app.get("/config", response_class=HTMLResponse, include_in_schema=False)
def config_page():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Lynx Configuration</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background: #1a1a2e; color: #e0e0e0; }
        .card { background: #16213e; border: 1px solid #0f3460; color: #e0e0e0; }
        .card-header { background: #0f3460; color: #ffffff; font-weight: 500; }
        .lynx-title { color: #e94560; font-weight: bold; letter-spacing: 2px; }
        .text-muted { color: #a8b5c7 !important; }
        a { color: #00d4aa; }
        label { color: #a8b5c7; font-size: 0.85em; margin-bottom: 2px; }
        .form-control { background: #0f3460; border: 1px solid #1e4a7a; color: #e0e0e0; }
        .form-control:focus { background: #0f3460; border-color: #00d4aa; color: #e0e0e0; box-shadow: none; }
        .btn-save { background: #e94560; border-color: #e94560; }
        .btn-save:hover { background: #c73652; border-color: #c73652; }
        .save-status { font-size: 0.85em; min-height: 1.2em; }
        .placeholder-card { opacity: 0.6; }
    </style>
</head>
<body>
<div class="container-fluid py-3">
    <div class="row mb-3">
        <div class="col">
            <h2 class="lynx-title">&#x25B6; LYNX CONFIGURATION</h2>
            <small class="text-muted"><a href="/">&larr; Back to receiver</a></small>
        </div>
    </div>

    <div class="row g-3">

        <!-- Site Information -->
        <div class="col-md-4">
            <div class="card mb-3">
                <div class="card-header">&#x1F3E0; Site Information</div>
                <div class="card-body">
                    <div class="row g-3">
                        <div class="col-md-6">
                            <label for="site-name">Receiver name</label>
                            <input type="text" class="form-control" id="site-name-input">
                        </div>
                        <div class="col-md-6">
                            <label for="site-callsign">Callsign</label>
                            <input type="text" class="form-control" id="site-callsign-input">
                        </div>
                        <div class="col-md-6">
                            <label for="site-location">Location</label>
                            <input type="text" class="form-control" id="site-location-input">
                        </div>
                        <div class="col-md-6">
                            <label for="site-locator">Locator</label>
                            <input type="text" class="form-control" id="site-locator-input">
                        </div>
                    </div>
                    <div class="mt-3 d-flex align-items-center gap-2">
                        <button class="btn btn-save" onclick="saveSite()">Save site info</button>
                        <span class="save-status" id="site-save-status"></span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Picotuner Network Settings -->
        <div class="col-md-4">
            <div class="card mb-3">
                <div class="card-header">&#x1F4E1; Picotuner Network Settings</div>
                <div class="card-body">
                    <div class="alert alert-warning py-2 small mb-3">
                        Changing these requires a restart of Lynx to take effect safely - background
                        monitoring threads read these once at startup.
                    </div>
                    <div class="row g-3">
                        <div class="col-md-6">
                            <label for="pt-host">Picotuner IP address</label>
                            <input type="text" class="form-control" id="pt-host-input">
                        </div>
                        <div class="col-md-3">
                            <label for="pt-status-port">Status port</label>
                            <input type="number" class="form-control" id="pt-status-port-input">
                        </div>
                        <div class="col-md-3"></div>
                        <div class="col-md-3">
                            <label for="pt-cmd-port">Cmd port (A)</label>
                            <input type="number" class="form-control" id="pt-cmd-port-input">
                        </div>
                        <div class="col-md-3">
                            <label for="pt-ts-port">TS port (A)</label>
                            <input type="number" class="form-control" id="pt-ts-port-input">
                        </div>
                        <div class="col-md-3">
                            <label for="pt-cmd-port-b">Cmd port (B)</label>
                            <input type="number" class="form-control" id="pt-cmd-port-b-input">
                        </div>
                        <div class="col-md-3">
                            <label for="pt-ts-port-b">TS port (B)</label>
                            <input type="number" class="form-control" id="pt-ts-port-b-input">
                        </div>
                    </div>
                    <div class="mt-3 d-flex align-items-center gap-2">
                        <button class="btn btn-save" onclick="savePicotuner()">Save Picotuner settings</button>
                        <span class="save-status" id="pt-save-status"></span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Diversity Source Switching + Integrations -->
        <div class="col-md-4">
            <div class="card mb-3">
                <div class="card-header">&#x1F500; Diversity Source Switching</div>
                <div class="card-body">
                    <div class="alert alert-warning py-2 small mb-3">
                        Changing these requires restarting the diversity combiner to take effect -
                        it reads these once at startup, not continuously.
                    </div>
                    <p class="text-muted small mb-3">
                        When both tuners are clean, the combiner sticks with whichever source is
                        currently preferred rather than re-deciding every segment. The preferred
                        source only changes when the other tuner's MER has been consistently,
                        meaningfully better for a sustained period - not on a single momentary
                        blip. These two settings control how sustained and how meaningful that has
                        to be.
                    </p>
                    <div class="row g-3">
                        <div class="col-md-6">
                            <label for="div-dwell">Dwell time (seconds)</label>
                            <input type="number" step="0.5" min="0" class="form-control" id="div-dwell-input">
                        </div>
                        <div class="col-md-6">
                            <label for="div-margin">MER margin (dB)</label>
                            <input type="number" step="0.1" min="0" class="form-control" id="div-margin-input">
                        </div>
                    </div>
                    <div class="mt-3 d-flex align-items-center gap-2">
                        <button class="btn btn-save" onclick="saveDiversity()">Save diversity settings</button>
                        <span class="save-status" id="div-save-status"></span>
                    </div>
                </div>
            </div>
        </div>

        <!-- QRZ Logbook + Bitfocus Companion -->
        <div class="col-md-4">
            <div class="card mb-3">
                <div class="card-header">&#x1F4D6; QRZ.com Logbook</div>
                <div class="card-body">
                    <div class="form-check form-switch mb-3">
                        <input class="form-check-input" type="checkbox" id="qrz-enabled-input">
                        <label class="form-check-label" for="qrz-enabled">Enabled</label>
                    </div>
                    <label for="qrz-api-key">API key</label>
                    <input type="text" class="form-control mb-2" id="qrz-api-key-input"
                           placeholder="Your QRZ Logbook API key">
                    <div class="row g-2">
                        <div class="col-6">
                            <label for="qrz-settle">Settle time (s)</label>
                            <input type="number" step="1" min="0" class="form-control" id="qrz-settle-input">
                        </div>
                        <div class="col-6">
                            <label for="qrz-suppress">Suppress (min)</label>
                            <input type="number" step="1" min="0" class="form-control" id="qrz-suppress-input">
                        </div>
                    </div>
                    <label for="qrz-portable-locator" class="mt-2">Portable locator override</label>
                    <input type="text" class="form-control" id="qrz-portable-locator-input"
                           placeholder="e.g. IO91VG - leave blank for normal operation">
                    <p class="text-muted small mt-2 mb-0">
                        Settle time: delay after lock before logging, so the callsign has time to
                        decode. Suppress: don't log the same callsign again within this many minutes.
                        Portable locator override: when a contacted station is operating portable and
                        hasn't updated their QRZ profile, QRZ's own distance/bearing calculation uses
                        their stale, registered locator. Set this to override it with their actual,
                        current one for every contact logged while it's set - clear it (empty + save)
                        once the portable session ends.
                    </p>
                    <div class="mt-3 d-flex align-items-center gap-2">
                        <button class="btn btn-save" onclick="saveQrz()">Save QRZ settings</button>
                        <span class="save-status" id="qrz-save-status"></span>
                    </div>
                </div>
            </div>

            <div class="card">
                <div class="card-header">&#x1F3AC; Bitfocus Companion</div>
                <div class="card-body">
                    <div class="form-check form-switch mb-3">
                        <input class="form-check-input" type="checkbox" id="companion-enabled-input">
                        <label class="form-check-label" for="companion-enabled">Enabled</label>
                    </div>
                    <label for="companion-lock-url">Lock URL</label>
                    <input type="text" class="form-control mb-1" id="companion-lock-url-input"
                           placeholder="http://companion-ip:8888/api/...">
                    <label for="companion-lock-settle" class="small text-muted">Settle time (s)</label>
                    <input type="number" step="1" min="0" class="form-control mb-2" id="companion-lock-settle-input">
                    <label for="companion-unlock-url">Unlock URL</label>
                    <input type="text" class="form-control mb-1" id="companion-unlock-url-input"
                           placeholder="http://companion-ip:8888/api/...">
                    <label for="companion-unlock-settle" class="small text-muted">Settle time (s)</label>
                    <input type="number" step="1" min="0" class="form-control" id="companion-unlock-settle-input">
                    <hr>
                    <div class="form-check form-switch mb-2">
                        <input class="form-check-input" type="checkbox" id="companion-gpio-enabled-input">
                        <label class="form-check-label" for="companion-gpio-enabled">
                            Also mirror on a GPIO pin (relay-based switching)
                        </label>
                    </div>
                    <p class="text-muted small mb-2">
                        Follows lock/unlock using the same settle times above - no separate timing.
                    </p>
                    <div class="row g-2">
                        <div class="col-8">
                            <label for="companion-gpio-pin" class="small">Physical pin</label>
                            <select class="form-control" id="companion-gpio-pin-input"></select>
                        </div>
                        <div class="col-4">
                            <label for="companion-gpio-polarity" class="small">Polarity</label>
                            <select class="form-control" id="companion-gpio-polarity-input">
                                <option value="high">Active high</option>
                                <option value="low">Active low</option>
                            </select>
                        </div>
                    </div>
                    <div class="mt-3 d-flex align-items-center gap-2">
                        <button class="btn btn-save" onclick="saveCompanion()">Save Companion settings</button>
                        <span class="save-status" id="companion-save-status"></span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Slack -->
        <div class="col-md-4">
            <div class="card mb-3">
                <div class="card-header">&#x1F4AC; Slack</div>
                <div class="card-body">
                    <div class="form-check form-switch mb-3">
                        <input class="form-check-input" type="checkbox" id="slack-enabled-input">
                        <label class="form-check-label" for="slack-enabled">Enabled</label>
                    </div>
                    <label for="slack-webhook-url">Webhook URL</label>
                    <input type="text" class="form-control mb-2" id="slack-webhook-url-input"
                           placeholder="https://hooks.slack.com/services/...">
                    <label for="slack-settle">Settle time (seconds)</label>
                    <input type="number" step="1" min="0" class="form-control mb-2" id="slack-settle-input">
                    <label for="slack-template">Message template</label>
                    <textarea class="form-control" id="slack-template-input" rows="5"></textarea>
                    <p class="text-muted small mt-2 mb-0">
                        Placeholders: <code>{site_callsign}</code> <code>{site_callsign_lower}</code>
                        <code>{rx_callsign}</code> <code>{mer}</code> <code>{margin}</code>
                        <code>{modcod}</code> <code>{frequency}</code>
                    </p>
                    <div class="mt-3 d-flex align-items-center gap-2">
                        <button class="btn btn-save" onclick="saveSlack()">Save Slack settings</button>
                        <span class="save-status" id="slack-save-status"></span>
                    </div>
                </div>
            </div>

            <div class="card">
                <div class="card-header">&#x1F6E1;&#xFE0F; Hard-Freeze Recovery</div>
                <div class="card-body">
                    <p class="text-muted small mb-3">
                        Applies immediately, no restart needed - these are read fresh on every check.
                        After enough restarts in quick succession, further attempts pause for a
                        cooldown - but resume early the moment the combiner's own output confirms
                        it's genuinely been clean for long enough, rather than always waiting out
                        the full cooldown regardless.
                    </p>
                    <div class="form-check form-switch mb-3">
                        <input class="form-check-input" type="checkbox" id="breaker-enabled-input">
                        <label class="form-check-label" for="breaker-enabled">Enabled</label>
                    </div>
                    <div class="row g-2">
                        <div class="col-6">
                            <label for="breaker-threshold" class="small">Trip after this many restarts</label>
                            <input type="number" step="1" min="1" class="form-control" id="breaker-threshold-input">
                        </div>
                        <div class="col-6">
                            <label for="breaker-window" class="small">...within this many seconds</label>
                            <input type="number" step="10" min="1" class="form-control" id="breaker-window-input">
                        </div>
                        <div class="col-6">
                            <label for="breaker-cooldown" class="small">Max cooldown (s)</label>
                            <input type="number" step="10" min="0" class="form-control" id="breaker-cooldown-input">
                        </div>
                        <div class="col-6">
                            <label for="breaker-clean" class="small">Required clean time (s)</label>
                            <input type="number" step="0.5" min="0" class="form-control" id="breaker-clean-input">
                        </div>
                        <div class="col-12">
                            <label for="breaker-retry-interval" class="small">Min. seconds between early retries</label>
                            <input type="number" step="0.5" min="0" class="form-control" id="breaker-retry-interval-input">
                        </div>
                    </div>
                    <div class="mt-3 d-flex align-items-center gap-2">
                        <button class="btn btn-save" onclick="saveBreaker()">Save recovery settings</button>
                        <span class="save-status" id="breaker-save-status"></span>
                    </div>
                </div>
            </div>
        </div>

        <!-- GPIO Tx on/off -->
        <div class="col-md-4">
            <div class="card">
                <div class="card-header">&#x1F50C; GPIO Tx On/Off</div>
                <div class="card-body">
                    <div class="form-check form-switch mb-3">
                        <input class="form-check-input" type="checkbox" id="gpio-enabled-input">
                        <label class="form-check-label" for="gpio-enabled">Enabled</label>
                    </div>
                    <div class="row g-2 mb-2">
                        <div class="col-8">
                            <label for="gpio-pin">Physical pin</label>
                            <select class="form-control" id="gpio-pin-input"></select>
                        </div>
                        <div class="col-4">
                            <label for="gpio-polarity">Polarity</label>
                            <select class="form-control" id="gpio-polarity-input">
                                <option value="high">Active high</option>
                                <option value="low">Active low</option>
                            </select>
                        </div>
                    </div>
                    <div class="row g-2 mb-2">
                        <div class="col-6">
                            <label for="gpio-power-up" class="small">Power-up settle (s)</label>
                            <input type="number" step="1" min="0" class="form-control" id="gpio-power-up-input">
                        </div>
                        <div class="col-6">
                            <label for="gpio-power-down" class="small">Power-down settle (s)</label>
                            <input type="number" step="1" min="0" class="form-control" id="gpio-power-down-input">
                        </div>
                    </div>
                    <p class="text-muted small mb-2">
                        Power-down settle of <strong>0</strong> means never auto power-down once
                        triggered on.
                    </p>
                    <hr>
                    <p class="text-muted small mb-2">
                        Inside a configured schedule window, the pin is forced on immediately
                        (no settling). Outside a window, or with no schedule set for that day
                        type, normal power-up/power-down timing above applies 24 hours a day.
                    </p>
                    <label class="small">Weekday schedule</label>
                    <div class="row g-2 mb-2">
                        <div class="col-6">
                            <input type="time" class="form-control" id="gpio-weekday-start-input">
                        </div>
                        <div class="col-6">
                            <input type="time" class="form-control" id="gpio-weekday-end-input">
                        </div>
                    </div>
                    <div class="form-check mb-2">
                        <input class="form-check-input" type="checkbox" id="gpio-weekday-none-input">
                        <label class="form-check-label small" for="gpio-weekday-none">No schedule (24hr auto)</label>
                    </div>
                    <label class="small">Weekend schedule</label>
                    <div class="row g-2 mb-2">
                        <div class="col-6">
                            <input type="time" class="form-control" id="gpio-weekend-start-input">
                        </div>
                        <div class="col-6">
                            <input type="time" class="form-control" id="gpio-weekend-end-input">
                        </div>
                    </div>
                    <div class="form-check mb-2">
                        <input class="form-check-input" type="checkbox" id="gpio-weekend-none-input">
                        <label class="form-check-label small" for="gpio-weekend-none">No schedule (24hr auto)</label>
                    </div>
                    <div class="mt-3 d-flex align-items-center gap-2">
                        <button class="btn btn-save" onclick="saveGpioTx()">Save GPIO settings</button>
                        <span class="save-status" id="gpio-save-status"></span>
                    </div>
                </div>
            </div>
        </div>

    </div>
</div>

<script>
async function loadCurrentConfig() {
    try {
        const cfg = await fetch('/api/config').then(r => r.json());
        document.getElementById('site-name-input').value = cfg.site?.name || '';
        document.getElementById('site-callsign-input').value = cfg.site?.callsign || '';
        document.getElementById('site-location-input').value = cfg.site?.location || '';
        document.getElementById('site-locator-input').value = cfg.site?.locator || '';

        document.getElementById('pt-host-input').value = cfg.picotuner?.host || '';
        document.getElementById('pt-status-port-input').value = cfg.picotuner?.status_port || '';
        document.getElementById('pt-cmd-port-input').value = cfg.picotuner?.cmd_port || '';
        document.getElementById('pt-ts-port-input').value = cfg.picotuner?.ts_port || '';
        document.getElementById('pt-cmd-port-b-input').value = cfg.picotuner?.cmd_port_b || '';
        document.getElementById('pt-ts-port-b-input').value = cfg.picotuner?.ts_port_b || '';

        document.getElementById('div-dwell-input').value = cfg.diversity?.mer_switch_dwell_secs ?? 10.0;
        document.getElementById('div-margin-input').value = cfg.diversity?.mer_switch_margin_db ?? 1.0;
        document.getElementById('breaker-enabled-input').checked = cfg.diversity?.hard_freeze_breaker_enabled ?? true;
        document.getElementById('breaker-threshold-input').value = cfg.diversity?.hard_freeze_breaker_threshold ?? 5;
        document.getElementById('breaker-window-input').value = cfg.diversity?.hard_freeze_breaker_window_secs ?? 300.0;
        document.getElementById('breaker-cooldown-input').value = cfg.diversity?.hard_freeze_breaker_cooldown_secs ?? 300.0;
        document.getElementById('breaker-clean-input').value = cfg.diversity?.hard_freeze_breaker_required_clean_secs ?? 2.0;
        document.getElementById('breaker-retry-interval-input').value = cfg.diversity?.hard_freeze_breaker_min_retry_interval_secs ?? 5.0;

        const qrz = cfg.notifications?.qrz || {};
        document.getElementById('qrz-enabled-input').checked = qrz.enabled || false;
        document.getElementById('qrz-api-key-input').value = qrz.api_key || '';
        document.getElementById('qrz-settle-input').value = qrz.settle_secs ?? 15;
        document.getElementById('qrz-suppress-input').value = qrz.suppress_mins ?? 60;
        document.getElementById('qrz-portable-locator-input').value = qrz.portable_locator || '';

        const slack = cfg.notifications?.slack || {};
        document.getElementById('slack-enabled-input').checked = slack.enabled || false;
        document.getElementById('slack-webhook-url-input').value = slack.webhook_url || '';
        document.getElementById('slack-settle-input').value = slack.settle_secs ?? 15;
        document.getElementById('slack-template-input').value = slack.message_template || '';

        const comp = cfg.notifications?.companion || {};
        document.getElementById('companion-enabled-input').checked = comp.enabled || false;
        document.getElementById('companion-lock-url-input').value = comp.lock_url || '';
        document.getElementById('companion-lock-settle-input').value = comp.lock_settle_secs ?? 5;
        document.getElementById('companion-unlock-url-input').value = comp.unlock_url || '';
        document.getElementById('companion-unlock-settle-input').value = comp.unlock_settle_secs ?? 5;
        document.getElementById('companion-gpio-enabled-input').checked = comp.gpio_enabled || false;
        document.getElementById('companion-gpio-polarity-input').value = comp.gpio_polarity || 'high';
        await loadGpioPinList(comp.gpio_pin ?? 13, 'companion-gpio-pin-input');

        const gpio = cfg.notifications?.gpio_tx || {};
        document.getElementById('gpio-enabled-input').checked = gpio.enabled || false;
        document.getElementById('gpio-polarity-input').value = gpio.polarity || 'high';
        document.getElementById('gpio-power-up-input').value = gpio.power_up_settle_secs ?? 5;
        document.getElementById('gpio-power-down-input').value = gpio.power_down_settle_secs ?? 900;
        setScheduleFields('weekday', gpio.schedule_weekday_start, gpio.schedule_weekday_end);
        setScheduleFields('weekend', gpio.schedule_weekend_start, gpio.schedule_weekend_end);

        // Pin dropdown is built dynamically (see loadGpioPinList) - set
        // its value once populated, defaulting sensibly if unset.
        await loadGpioPinList(gpio.pin ?? 11);
    } catch (e) {
        console.error('Failed to load config', e);
    }
}

function setScheduleFields(prefix, start, end) {
    const noSchedule = !start || !end;
    document.getElementById(`gpio-${prefix}-none-input`).checked = noSchedule;
    document.getElementById(`gpio-${prefix}-start-input`).value = start || '';
    document.getElementById(`gpio-${prefix}-end-input`).value = end || '';
    document.getElementById(`gpio-${prefix}-start-input`).disabled = noSchedule;
    document.getElementById(`gpio-${prefix}-end-input`).disabled = noSchedule;
}

function getScheduleFields(prefix) {
    if (document.getElementById(`gpio-${prefix}-none-input`).checked) {
        return {start: '', end: ''};
    }
    return {
        start: document.getElementById(`gpio-${prefix}-start-input`).value || '',
        end: document.getElementById(`gpio-${prefix}-end-input`).value || '',
    };
}

let _gpioPinListCache = null;
async function loadGpioPinList(selectedPin, elementId = 'gpio-pin-input') {
    try {
        const pins = _gpioPinListCache || await fetch('/api/notifications/gpio-pins').then(r => r.json());
        _gpioPinListCache = pins;
        const select = document.getElementById(elementId);
        select.innerHTML = pins.map(p =>
            `<option value="${p.pin}" ${p.pin === selectedPin ? 'selected' : ''}>${p.label}</option>`
        ).join('');
    } catch (e) {
        console.error('Failed to load GPIO pin list', e);
    }
}

// "No schedule" checkboxes disable/enable their paired time inputs live
for (const prefix of ['weekday', 'weekend']) {
    document.addEventListener('DOMContentLoaded', () => {
        const cb = document.getElementById(`gpio-${prefix}-none-input`);
        if (cb) {
            cb.addEventListener('change', () => {
                document.getElementById(`gpio-${prefix}-start-input`).disabled = cb.checked;
                document.getElementById(`gpio-${prefix}-end-input`).disabled = cb.checked;
            });
        }
    });
}

async function saveSite() {
    const statusEl = document.getElementById('site-save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const body = {
            site: {
                name: document.getElementById('site-name-input').value,
                callsign: document.getElementById('site-callsign-input').value,
                location: document.getElementById('site-location-input').value,
                locator: document.getElementById('site-locator-input').value,
            }
        };
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        statusEl.textContent = 'Saved - applied immediately.';
        statusEl.className = 'save-status text-success';
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function savePicotuner() {
    const statusEl = document.getElementById('pt-save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const body = {
            picotuner: {
                host: document.getElementById('pt-host-input').value,
                status_port: parseInt(document.getElementById('pt-status-port-input').value),
                cmd_port: parseInt(document.getElementById('pt-cmd-port-input').value),
                ts_port: parseInt(document.getElementById('pt-ts-port-input').value),
                cmd_port_b: parseInt(document.getElementById('pt-cmd-port-b-input').value),
                ts_port_b: parseInt(document.getElementById('pt-ts-port-b-input').value),
            }
        };
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        const result = await r.json();
        if (result.restart_required) {
            statusEl.textContent = 'Saved - restart Lynx to apply.';
            statusEl.className = 'save-status text-warning';
        } else {
            statusEl.textContent = 'Saved - no changes needed restart.';
            statusEl.className = 'save-status text-success';
        }
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function saveDiversity() {
    const statusEl = document.getElementById('div-save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const body = {
            diversity: {
                mer_switch_dwell_secs: parseFloat(document.getElementById('div-dwell-input').value),
                mer_switch_margin_db: parseFloat(document.getElementById('div-margin-input').value),
            }
        };
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        const result = await r.json();
        if (result.restart_required) {
            statusEl.textContent = 'Saved - restart the diversity combiner to apply.';
            statusEl.className = 'save-status text-warning';
        } else {
            statusEl.textContent = 'Saved - no changes needed restart.';
            statusEl.className = 'save-status text-success';
        }
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function saveBreaker() {
    const statusEl = document.getElementById('breaker-save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const body = {
            diversity: {
                hard_freeze_breaker_enabled: document.getElementById('breaker-enabled-input').checked,
                hard_freeze_breaker_threshold: parseInt(document.getElementById('breaker-threshold-input').value),
                hard_freeze_breaker_window_secs: parseFloat(document.getElementById('breaker-window-input').value),
                hard_freeze_breaker_cooldown_secs: parseFloat(document.getElementById('breaker-cooldown-input').value),
                hard_freeze_breaker_required_clean_secs: parseFloat(document.getElementById('breaker-clean-input').value),
                hard_freeze_breaker_min_retry_interval_secs: parseFloat(document.getElementById('breaker-retry-interval-input').value),
            }
        };
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        statusEl.textContent = 'Saved - applied immediately, no restart needed.';
        statusEl.className = 'save-status text-success';
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function saveQrz() {
    const statusEl = document.getElementById('qrz-save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const body = {
            notifications_qrz: {
                enabled: document.getElementById('qrz-enabled-input').checked,
                api_key: document.getElementById('qrz-api-key-input').value,
                settle_secs: parseFloat(document.getElementById('qrz-settle-input').value),
                suppress_mins: parseFloat(document.getElementById('qrz-suppress-input').value),
                portable_locator: document.getElementById('qrz-portable-locator-input').value.trim(),
            }
        };
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        statusEl.textContent = 'Saved - applied immediately.';
        statusEl.className = 'save-status text-success';
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function saveSlack() {
    const statusEl = document.getElementById('slack-save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const body = {
            notifications_slack: {
                enabled: document.getElementById('slack-enabled-input').checked,
                webhook_url: document.getElementById('slack-webhook-url-input').value,
                settle_secs: parseFloat(document.getElementById('slack-settle-input').value),
                message_template: document.getElementById('slack-template-input').value,
            }
        };
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        statusEl.textContent = 'Saved - applied immediately.';
        statusEl.className = 'save-status text-success';
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function saveCompanion() {
    const statusEl = document.getElementById('companion-save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const body = {
            notifications_companion: {
                enabled: document.getElementById('companion-enabled-input').checked,
                lock_url: document.getElementById('companion-lock-url-input').value,
                lock_settle_secs: parseFloat(document.getElementById('companion-lock-settle-input').value),
                unlock_url: document.getElementById('companion-unlock-url-input').value,
                unlock_settle_secs: parseFloat(document.getElementById('companion-unlock-settle-input').value),
                gpio_enabled: document.getElementById('companion-gpio-enabled-input').checked,
                gpio_pin: parseInt(document.getElementById('companion-gpio-pin-input').value),
                gpio_polarity: document.getElementById('companion-gpio-polarity-input').value,
            }
        };
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        statusEl.textContent = 'Saved - applied immediately.';
        statusEl.className = 'save-status text-success';
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function saveGpioTx() {
    const statusEl = document.getElementById('gpio-save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const weekday = getScheduleFields('weekday');
        const weekend = getScheduleFields('weekend');
        const body = {
            notifications_gpio_tx: {
                enabled: document.getElementById('gpio-enabled-input').checked,
                pin: parseInt(document.getElementById('gpio-pin-input').value),
                polarity: document.getElementById('gpio-polarity-input').value,
                power_up_settle_secs: parseFloat(document.getElementById('gpio-power-up-input').value),
                power_down_settle_secs: parseFloat(document.getElementById('gpio-power-down-input').value),
                schedule_weekday_start: weekday.start,
                schedule_weekday_end: weekday.end,
                schedule_weekend_start: weekend.start,
                schedule_weekend_end: weekend.end,
            }
        };
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        statusEl.textContent = 'Saved - applied immediately, no restart needed.';
        statusEl.className = 'save-status text-success';
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

loadCurrentConfig();
</script>
</body>
</html>"""

# ── API: RF Reception ─────────────────────────────────────────
def get_my_ip():
    """Our own LAN IP address, used both for the Picotuner's TS target
    and for ffmpeg's stream output destination."""
    result = subprocess.run(["hostname", "-I"], capture_output=True, text=True)
    return result.stdout.split()[0]

def picotuner_cmd(cmd: str):
    """Send a UDP command to the Picotuner."""
    cfg = config['picotuner']
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.sendto(cmd.encode(), (cfg['host'], cfg['cmd_port']))
    sock.close()

def calc_tuner_freq(freq_khz: int, lnb_lo_khz: int) -> int:
    """Given a downlink frequency and an LNB LO (0 = no LNB), returns
    the actual IF frequency the Picotuner needs to be tuned to.
    Auto-detects low-side (Ku-band) vs high-side (C-band) injection —
    see tune() for the full explanation. Shared so the startup resume
    logic can check whether the Picotuner is already correctly tuned
    without duplicating this calculation."""
    if not lnb_lo_khz:
        return freq_khz
    if freq_khz >= lnb_lo_khz:
        return freq_khz - lnb_lo_khz   # low-side (Ku-band)
    return lnb_lo_khz - freq_khz        # high-side (C-band)

_tune_lock_handed_off = False  # set by _tune_impl() once the async thread has taken over
                                # responsibility for releasing tune_lock - protects against a
                                # double-release if anything AFTER that point in _tune_impl
                                # were ever to throw (nothing currently does, since
                                # save_last_state() already catches its own exceptions, but
                                # this makes that safe by construction rather than by
                                # happening to currently be true)

@app.post("/api/tune", tags=["RF Reception"],
          summary="Tune Picotuner to a frequency",
          description="Tunes the Picotuner and starts mpv playing the stream. "
                      "Stops any current reception first.")
def tune(req: TuneRequest):
    # Thin wrapper around _tune_impl() specifically so the lock's
    # release is guaranteed regardless of where/how _tune_impl fails,
    # without needing to restructure that large function's own body.
    # Confirmed live that the previous approach (a bounded 15s
    # acquisition timeout, with release left to _tune_impl's own async
    # thread) was a real gap, not just a theoretical one: an exception
    # inside _tune_impl before that async thread ever started left the
    # lock stuck forever, silently blocking every future RF tune
    # (returning the 503 below on each attempt) while streaming kept
    # working fine, since it never touched this lock at all — exactly
    # what was reported after a burst of rapid, overlapping preset/
    # stream switches.
    global _tune_lock_handed_off
    if not tune_lock.acquire(timeout=15):
        raise HTTPException(status_code=503, detail="Another tune operation is already in progress — please try again shortly")
    _tune_lock_handed_off = False
    try:
        return _tune_impl(req)
    except Exception:
        if not _tune_lock_handed_off:
            tune_lock.release()
        raise
    # NOTE: on success, the lock is deliberately still held here — it's
    # released later by _tune_impl's own async mpv-restart thread, once
    # the actual tune is fully complete, not just accepted.

def _tune_impl(req: TuneRequest):
    global current_mode, current_preset, current_lnb_lo_khz, current_lnb_side, diversity_enabled, _tune_lock_handed_off
    cfg = config['picotuner']
    is_diversity = req.plug.lower() == "diversity"

    # One-time defensive sweep for any leftover ffmpeg transcode
    # process from before streams were switched to direct mpv
    # playback — harmless no-op once none remain.
    subprocess.run(["pkill", "-9", "-f", "ffmpeg.*udp://127.0.0.1:9945"], capture_output=True)
    stop_ffmpeg_bg()
    time.sleep(0.3)
    
    # If an LNB LO is given, req.freq is the real downlink/satellite
    # frequency — subtract the LO to get the actual IF frequency the
    # Picotuner needs to be tuned to (standard low-side-injection LNB
    # architecture, e.g. 9750000 kHz LO for a Ku-band Universal LNB).
    # LNBs use two different mixing architectures depending on band:
    #   - Ku-band (9750/10600/10750 MHz LO): LOW-side injection —
    #     the LO sits BELOW the downlink frequency, IF = downlink - LO
    #   - C-band (5150 MHz LO): HIGH-side injection — the LO sits
    #     ABOVE the downlink frequency, IF = LO - downlink
    # Rather than requiring the operator to know/select which side,
    # auto-detect it from which value is larger — this always
    # produces a positive IF for a correctly-matched LNB/frequency
    # pair, regardless of band. (A previous version always subtracted
    # LO from downlink regardless of which was larger, which produced
    # a nonsensical negative frequency for C-band — that was a genuine
    # calculation bug, not a case of that frequency being unreceivable.)
    if req.lnb_lo_khz:
        if req.freq >= req.lnb_lo_khz:
            tuner_freq = req.freq - req.lnb_lo_khz       # low-side (Ku-band)
            current_lnb_side = "low"
        else:
            tuner_freq = req.lnb_lo_khz - req.freq        # high-side (C-band)
            current_lnb_side = "high"
    else:
        tuner_freq = req.freq

    # CRITICAL SAFETY CHECK — a mismatched LNB selection can produce a
    # negative or nonsensical frequency (e.g. downlink 3404 MHz minus a
    # 5150 MHz LO = -1746 MHz). Sending that straight to the Picotuner
    # over the WinterHill protocol has been confirmed to hang the unit,
    # requiring a physical power cycle to recover. Reject anything
    # outside the FTS4334L NIM's realistic tuning range (50–2500 MHz)
    # rather than ever sending it to the hardware.
    if tuner_freq < 50000 or tuner_freq > 2500000:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Calculated tuner frequency {tuner_freq/1000:.3f} MHz is out of "
                f"range (50–2500 MHz). Check the LNB LO selection matches the "
                f"downlink frequency entered — this was NOT sent to the Picotuner."
            )
        )

    current_lnb_lo_khz = req.lnb_lo_khz

    # Cover the screen well before touching the source. This must come
    # AFTER the safety check above — if it started earlier and the
    # frequency was then rejected, the screen would be left covered
    # with no matching uncover ever running.
    start_transition_cover()
    time.sleep(1.0)

    # Send tune command(s) to the Picotuner. Its TS target was set to
    # our IP and the configured port the very first time and is never
    # touched again — it keeps sending there regardless of what Lynx
    # is doing, which is exactly why streams use a separate port
    # instead of trying to fight that behaviour.
    if is_diversity:
        # Both receivers, same frequency/SR — plug assignment for
        # each is configurable (config['diversity']['rcv1_plug'] /
        # 'rcv2_plug'), NOT a fixed rcv=1-must-be-plug-a rule. rcv=
        # and fplug= are independent settings on the Picotuner —
        # which physical input each receiver actually uses depends
        # on real wiring, which varies by site. Each receiver has
        # its own dedicated command port; a single shared port with
        # different rcv= values in the command text does NOT work —
        # confirmed the hard way during tonight's standalone testing.
        div_cfg = config['diversity']
        picotuner_cmd(
            f"[to@wh] rcv=1 fplug={div_cfg.get('rcv1_plug', 'a')} offset=0 freq={tuner_freq} srate={req.sr}"
        )
        # Give rcv=1's own tune command time to fully register on the
        # Picotuner's firmware before rcv=2's arrives - confirmed these
        # were previously sent back-to-back with zero gap (picotuner_cmd()
        # itself has no inherent delay), which is a strong candidate for
        # a real, intermittent bug reported live: rcv=1 sometimes failing
        # to lock in diversity mode despite a strong signal, while rcv=2
        # (sent second, with nothing after it to interrupt it) always
        # locked - and rcv=1 always locked fine in non-diversity mode,
        # where only one tune command is ever sent at all.
        time.sleep(0.3)
        sock_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock_b.sendto(
            f"[to@wh] rcv=2 fplug={div_cfg.get('rcv2_plug', 'b')} offset=0 freq={tuner_freq} srate={req.sr}".encode(),
            (cfg['host'], cfg['cmd_port_b'])
        )
        sock_b.close()
        # Same settling delay as rcv=1 above, closing the one clear
        # asymmetry left in this sequence: rcv=1's own command already
        # gets 0.3s to fully register before anything else happens, but
        # rcv=2's previously had no equivalent protection at all -
        # kill_mpv()/start_diversity_combiner() ran immediately after
        # it with no gap. Live evidence: only tuner A locked at 8PSK,
        # both locked at 16APSK after a long delay - consistent with
        # (though not conclusively proven to be) rcv=2's tune command
        # being similarly exposed to the same class of timing issue
        # rcv=1's fix above already addresses.
        time.sleep(0.3)
        # Ensure the port the combiner needs to bind (9941, same as
        # RF's own direct-play port) is genuinely free first — if
        # mpv from a previous tune is still bound to it, the
        # combiner crashes immediately on startup with "Address
        # already in use" (confirmed directly). restart_mpv() itself
        # only kills the old process ~1s later via the async
        # _kick_mpv() thread below, which is too late for the
        # combiner's own startup.
        kill_mpv()
        start_diversity_combiner()
        diversity_enabled = True
    else:
        picotuner_cmd(
            f"[to@wh] rcv=1 fplug={req.plug} offset=0 "
            f"freq={tuner_freq} srate={req.sr}"
        )
        if diversity_enabled:
            # Switching away from diversity mode — stop the combiner
            # rather than leaving it running with nothing using its
            # output, which would just waste CPU on both Picotuner
            # sockets for no reason.
            stop_diversity_combiner()
        diversity_enabled = False
    
    current_mode = "rf"
    if req.lnb_lo_khz:
        current_preset = f"{req.freq/1000:.3f} MHz (LNB LO {req.lnb_lo_khz/1000:.3f} MHz) / {req.sr} kS/s"
    else:
        current_preset = f"{req.freq/1000:.3f} MHz / {req.sr} kS/s"

    # Switch back to the correct source via a full mpv restart — see
    # restart_mpv() docstring for why IPC-only reload commands were
    # abandoned in favour of this. In diversity mode, mpv plays the
    # COMBINER's output, not the Picotuner's raw TS port directly —
    # this is the one line that actually makes diversity mode work
    # end-to-end, everything else is just getting both tuners locked
    # and the combiner running.
    def _kick_mpv():
        global mpv_running_for_rf
        try:
            time.sleep(1)
            # Deliberately do NOT start mpv here. It's only started once
            # rf_mpv_lifecycle_monitor confirms a stable signal lock -
            # see that function's docstring for why. Just ensure nothing
            # from a previous tune/stream is left running against what
            # is now a stale target, and leave the cover up; the
            # overlay already shows the useful status/metadata (call-
            # sign, MER, modcod) during acquisition on its own.
            kill_mpv()
            mpv_running_for_rf = False
        finally:
            tune_lock.release()
    threading.Thread(target=_kick_mpv, daemon=True).start()
    _tune_lock_handed_off = True

    # Remember this so we can resume automatically after any restart —
    # crash, watchdog, scheduled 12-hour reboot, or a genuine power cycle.
    save_last_state({
        "mode": "rf",
        "freq": req.freq,
        "sr": req.sr,
        "plug": req.plug,
        "lnb_lo_khz": req.lnb_lo_khz
    })
    
    return {
        "success": True,
        "mode": "rf",
        "freq_khz": req.freq,
        "tuner_freq_khz": tuner_freq,
        "lnb_lo_khz": req.lnb_lo_khz,
        "sr_ks": req.sr,
        "plug": req.plug
    }

@app.get("/api/tune", tags=["RF Reception"],
         summary="Tune Picotuner to a frequency (URL/GET version)",
         description="Identical to POST /api/tune, but takes the same "
                     "values as URL query parameters instead of a JSON "
                     "body — so it can be triggered by simply visiting "
                     "a link. A browser navigating to a URL can only "
                     "ever send a GET request, never a POST with a JSON "
                     "body, so this exists specifically for browser "
                     "bookmarks, Bitfocus Companion buttons, or any "
                     "tool that can only fire a plain URL. Reuses the "
                     "exact same tune() logic as the POST version below "
                     "— nothing is duplicated.")
def tune_via_url(freq: int, sr: int, plug: str = "a", lnb_lo_khz: int = 0):
    return tune(TuneRequest(freq=freq, sr=sr, plug=plug, lnb_lo_khz=lnb_lo_khz))

@app.post("/api/preset", tags=["RF Reception"],
          summary="Tune to a named preset",
          description="Looks up the preset by name in lynx_config.yaml and tunes to it. "
                      "Also works with Ryde presets via the Ryde API. The name is "
                      "sent in the request body rather than the URL path, since "
                      "preset names can contain characters (like '/') that are "
                      "unreliable when embedded in a URL path across browsers.")
def tune_preset(req: PresetTuneRequest):
    global current_preset
    name = req.name
    # Check local presets first
    for p in config.get('presets', []):
        if p['name'].lower() == name.lower():
            preset_type = p.get('type', 'rf')  # missing type = saved
                                                 # before RF/stream memories
                                                 # existed - always RF back then
            if preset_type == "stream":
                result = start_stream(StreamRequest(url=p['url'], name=p['name']))
                current_preset = p['name']
                return result
            else:
                result = tune(TuneRequest(
                    freq=p['freq'], sr=p['sr'],
                    plug=p.get('plug', 'a'),
                    lnb_lo_khz=p.get('lnb_lo_khz', 0)
                ))
                current_preset = p['name']
                return result
    
    # Ryde preset fallback — commented out, see ryde_cmd() docstring for why.
    # if config['ryde']['enabled']:
    #     try:
    #         result = ryde_cmd({"request": "setPreset", "name": name})
    #         if result.get('success'):
    #             current_preset = name
    #             return {"success": True, "mode": "ryde_preset", "preset": name}
    #     except Exception:
    #         pass
    
    raise HTTPException(status_code=404, detail=f"Preset '{name}' not found")

@app.get("/api/presets", tags=["RF Reception"],
         summary="List all tuning presets",
         description="Returns local presets from config plus Ryde presets if available.")
def list_presets():
    local = config.get('presets', [])
    ryde_presets = []
    # Ryde preset fetch — commented out, see ryde_cmd() docstring for why.
    # if config['ryde']['enabled']:
    #     try:
    #         result = ryde_cmd({"request": "getPresets"})
    #         ryde_presets = result.get('presets', [])
    #     except Exception:
    #         pass
    return {
        "local": local,
        "ryde": ryde_presets
    }

@app.post("/api/presets/add", tags=["RF Reception"],
          summary="Save a new memory preset (RF or stream)",
          description="Adds either an RF (frequency/symbol-rate/plug) or a "
                      "stream (URL) memory to the local preset list and "
                      "persists it to lynx_config.yaml, so it survives "
                      "restarts. If no name is given for an RF save, one "
                      "is generated from the frequency (e.g. '437.025 MHz') "
                      "- a stream save always requires an explicit name.")
def add_preset(req: PresetSaveRequest):
    preset_type = req.type if req.type in ("rf", "stream") else "rf"
    config.setdefault('presets', [])

    if preset_type == "stream":
        if not req.url or not req.url.strip():
            raise HTTPException(status_code=400, detail="A stream memory needs a URL")
        if not req.name.strip():
            raise HTTPException(status_code=400, detail="A stream memory needs a name")
        name = req.name.strip()
    else:
        if req.freq is None or req.sr is None:
            raise HTTPException(status_code=400, detail="An RF memory needs a frequency and symbol rate")
        name = req.name.strip() if req.name.strip() else f"{req.freq/1000:.3f} MHz"

    # Names must stay unique — remove_preset() identifies a preset by
    # name alone, so two presets sharing a name would make deletion
    # ambiguous (removing one would silently remove both). Reusing
    # the same tuning/URL under a genuinely different name is fine and
    # explicitly allowed here - what counts as "the same" depends on
    # the type, since RF and stream memories don't share a comparable
    # identity (a frequency isn't a URL).
    for p in config['presets']:
        if p.get('name') == name:
            if p.get('type', 'rf') != preset_type:
                return {"success": False, "presets": config['presets'],
                        "note": "name already used by a different memory type"}
            if preset_type == "stream":
                same = (p.get('url') == req.url)
            else:
                same = (p.get('freq') == req.freq and p.get('sr') == req.sr
                        and p.get('plug', 'a') == req.plug
                        and p.get('lnb_lo_khz', 0) == req.lnb_lo_khz)
            if same:
                return {"success": True, "presets": config['presets'], "note": "already saved"}
            else:
                return {"success": False, "presets": config['presets'], "note": "name already used"}

    if preset_type == "stream":
        config['presets'].append({
            "type": "stream",
            "name": name,
            "url": req.url.strip(),
            "note": "User saved via web UI"
        })
    else:
        config['presets'].append({
            "type": "rf",
            "name": name,
            "freq": req.freq,
            "sr": req.sr,
            "plug": req.plug,
            "lnb_lo_khz": req.lnb_lo_khz,
            "note": "User saved via web UI"
        })
    save_config(config)
    return {"success": True, "presets": config['presets']}

@app.post("/api/presets/remove", tags=["RF Reception"],
          summary="Remove a saved memory preset",
          description="Removes a local preset by name and persists the change.")
def remove_preset(req: PresetTuneRequest):
    config['presets'] = [p for p in config.get('presets', []) if p.get('name') != req.name]
    save_config(config)
    return {"success": True, "presets": config['presets']}

# ── API: Streaming ────────────────────────────────────────────
def get_live_stream_info():
    """Queries mpv directly via IPC for live bitrate and codec info.
    Replaces the old ffmpeg-progress-file-based approach entirely —
    streams are now played directly by mpv via its own native RTMP/
    SRT/HTTP demuxers, with no separate transcode process to monitor
    at all. Bitrate here is a genuine instantaneous reading from mpv
    itself, not a rolling average parsed from a file."""
    result = {"bitrate_kbps": None, "video_codec": None, "audio_codec": None}
    try:
        vb = mpv_query({"command": ["get_property", "video-bitrate"]})
        ab = mpv_query({"command": ["get_property", "audio-bitrate"]})
        v = vb.get("data", 0) if vb and vb.get("error") == "success" else 0
        a = ab.get("data", 0) if ab and ab.get("error") == "success" else 0
        if v or a:
            result["bitrate_kbps"] = (v + a) / 1000.0  # mpv reports bits/s
    except Exception:
        pass
    try:
        # video-codec gives a long descriptive string like "H.265 / HEVC
        # (High Efficiency Video Coding)" — video-format gives the short
        # code ("hevc") that's actually usable on the OSD.
        vf = mpv_query({"command": ["get_property", "video-format"]})
        if vf and vf.get("error") == "success":
            result["video_codec"] = vf.get("data")
    except Exception:
        pass
    try:
        # audio-codec-name is the short-form equivalent for audio (e.g.
        # "aac") — audio-codec itself is the same overly long style as
        # video-codec above.
        acn = mpv_query({"command": ["get_property", "audio-codec-name"]})
        if acn and acn.get("error") == "success":
            result["audio_codec"] = acn.get("data")
    except Exception:
        pass
    return result

def get_stream_protocol(url: str) -> str:
    """Best-effort protocol label parsed from the stream URL's scheme,
    for display purposes (e.g. 'RTMP', 'SRT', 'HTTP')."""
    if "://" not in url:
        return ""
    return url.split("://", 1)[0].upper()

@app.post("/api/stream", tags=["Streaming"],
          summary="Start playing a network stream",
          description="Plays an RTMP, SRT, UDP or RTSP stream directly in "
                      "mpv, using mpv's own native demuxers — no transcode "
                      "step. Stops any current reception first.")
def start_stream(req: StreamRequest):
    # Thin wrapper, same pattern and same tune_lock as tune()/_tune_impl().
    # Confirmed live: streaming had NO locking at all, sharing the same
    # start_transition_cover()/end_transition_cover() marker as RF tuning
    # with zero coordination between them. Rapid, overlapping stream
    # switches (or a stream switch overlapping an RF tune) could race on
    # that shared marker - one request's end_transition_cover() removing
    # the cover while another request's mpv restart was still mid-flight,
    # briefly exposing the desktop underneath. Reported directly: a
    # "flash of desktop" during rapid stream switching, right before RF
    # tuning stopped responding entirely from the related tune_lock bug.
    global _tune_lock_handed_off
    if not tune_lock.acquire(timeout=15):
        raise HTTPException(status_code=503, detail="Another tune/stream operation is already in progress — please try again shortly")
    _tune_lock_handed_off = False
    try:
        return _start_stream_impl(req)
    except Exception:
        if not _tune_lock_handed_off:
            tune_lock.release()
        raise
    # NOTE: on success, the lock is deliberately still held here - it's
    # released later by _start_stream_impl's own async mpv-restart thread.

def _start_stream_impl(req: StreamRequest):
    global current_mode, current_preset, current_stream_name, current_stream_url, _tune_lock_handed_off

    # Cover the screen well before touching the source — a full second
    # of head start, generous enough to absorb scheduling jitter if the
    # Pi is under load, before we switch mpv to the new source.
    start_transition_cover()
    time.sleep(1.0)

    # NOTE: we tried multiple WinterHill commands (ts=0, ts=1 with a
    # null/zero target, changing tsport) to stop the Picotuner sending
    # its own TS while a stream plays, and confirmed NONE of them
    # actually change the broadcast TS target — the Picotuner appears
    # to keep sending to whatever address was set the very first time,
    # indefinitely, regardless of further commands. Retuning it to a
    # frequency with no usable signal means there's nothing to lock
    # onto, so no valid demodulated TS is ever produced regardless of
    # target address — this also frees up genuine RF/network bandwidth
    # a locked Picotuner would otherwise be consuming the whole time a
    # stream plays.
    picotuner_cmd("[to@wh] rcv=1 fplug=a offset=0 freq=0 srate=333")

    current_mode = "stream"
    # The friendly name is whatever the caller says it is — this is our
    # own record of what WE chose to play, not anything read from the
    # stream's content, so there's no privacy concern in displaying it.
    current_stream_name = req.name or req.url
    current_stream_url = req.url
    current_preset = current_stream_name

    # mpv plays the stream URL directly via its own native RTMP/SRT/
    # HTTP demuxers — no ffmpeg transcode step, no separate process,
    # no re-encode pass at all. This removes real CPU/thermal load and
    # a full latency hop, now that mpv restarts on every switch anyway
    # — the original reason for transcoding (keeping mpv's input on a
    # single fixed port so it never had to change) no longer applies
    # once mpv itself is routinely killed and relaunched on switches.
    def _kick_mpv():
        try:
            time.sleep(1)
            restart_mpv(req.url, is_rf=False)
            # Confirms real rendering rather than guessing with a fixed
            # delay - a weak/low-bandwidth stream source can genuinely
            # take longer to start producing a real picture than RF
            # ever does, and uncovering too early exposes whatever's
            # behind mpv (the desktop, on this non-headless build) for
            # the gap. A fixed delay was tried before (bumped 0.5s ->
            # 2.5s) but that's still just a guess - this waits as long
            # as actually needed, bounded by a timeout.
            #
            # The return value is now actually checked - confirmed
            # live as a genuine bug when it wasn't: this call already
            # existed, but its True/False result was silently ignored,
            # so a stream that took longer than the 12s timeout still
            # got uncovered anyway, exposing the desktop/terminal
            # behind it. A second, more patient attempt is tried
            # before giving up; if that also fails, the cover stays up
            # rather than exposing anything - a stuck cover is a far
            # smaller problem than a silently exposed desktop.
            rendering_confirmed = wait_for_mpv_rendering(timeout=12.0)
            if not rendering_confirmed:
                print(f"[stream_start] mpv did not confirm rendering within 12s for "
                      f"'{req.name or req.url}' - giving it one more, longer attempt "
                      f"before uncovering")
                record_diagnostic_event("stream_render_slow",
                                  "did not confirm rendering within the initial timeout - "
                                  "retrying with a longer one", count_as_mpv_restart=False)
                rendering_confirmed = wait_for_mpv_rendering(timeout=20.0)

            if rendering_confirmed:
                # Small safety margin after log-confirmed rendering, before
                # uncovering - confirmed live that mpv's log line appearing
                # doesn't guarantee the compositor has painted a frame yet
                # (a sub-0.5s gap). RF mode is incidentally protected from
                # this same gap by mpv_running_for_rf only being set after
                # end_transition_cover() and only reaching the overlay on
                # its next status poll; stream mode's uncover condition has
                # no equivalent gate, so it needs this explicitly.
                time.sleep(0.3)
                end_transition_cover()
                record_diagnostic_event("user_stream_start", req.name or req.url)
            else:
                print(f"[stream_start] mpv still did not confirm rendering for "
                      f"'{req.name or req.url}' after a second, longer attempt - "
                      f"leaving the cover up rather than exposing whatever's underneath")
                record_diagnostic_event("stream_render_not_confirmed",
                                  f"mpv never confirmed rendering for '{req.name or req.url}' "
                                  f"- cover left up", count_as_mpv_restart=False)
        finally:
            tune_lock.release()
    threading.Thread(target=_kick_mpv, daemon=True).start()
    _tune_lock_handed_off = True

    # Remember this so we can resume automatically after any restart.
    save_last_state({
        "mode": "stream",
        "url": req.url,
        "name": current_stream_name
    })
    
    return {"success": True, "mode": "stream", "url": req.url, "name": current_stream_name}

@app.post("/api/stream/{name}", tags=["Streaming"],
          summary="Start a named stream",
          description="Looks up a stream by name in config and starts it.")
def start_named_stream(name: str):
    for s in config.get('streams', []):
        if s['name'].lower() == name.lower():
            return start_stream(StreamRequest(url=s['url'], name=s['name']))
    raise HTTPException(status_code=404, detail=f"Stream '{name}' not found")

@app.get("/api/streams", tags=["Streaming"],
         summary="List configured streams")
def list_streams():
    return {"streams": config.get('streams', [])}

@app.get("/api/streams/live", tags=["Streaming"],
         summary="Get currently live BATC streams",
         description="Returns cached live stream list from BATC. "
                     "Cache is refreshed automatically every hour, "
                     "or manually via POST /api/streams/refresh. "
                     "This avoids hammering the BATC API from multiple receivers.")
def list_live_streams():
    streams = get_batc_streams_cached()
    age = int(time.time() - _batc_cache_time)
    return {
        "streams": streams,
        "count": len(streams),
        "cache_age_seconds": age,
        "cache_expires_seconds": max(0, BATC_CACHE_TTL - age)
    }

@app.post("/api/streams/refresh", tags=["Streaming"],
          summary="Force refresh of BATC live stream list",
          description="Fetches a fresh copy from the BATC API immediately. "
                      "Use sparingly — the cache updates automatically every hour.")
def refresh_live_streams():
    global _batc_cache, _batc_cache_time
    try:
        _batc_cache = fetch_batc_streams_from_api()
        _batc_cache_time = time.time()
        return {"success": True, "count": len(_batc_cache), "refreshed_at": utc_now_iso()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"BATC API unavailable: {e}")

# ── API: Control ──────────────────────────────────────────────
@app.post("/api/stop", tags=["Control"],
          summary="Stop current reception/stream")
def stop():
    stop_current()
    return {"success": True, "mode": "idle"}

@app.post("/api/restart", tags=["Control"],
          summary="Reboot the Pi",
          description="Reboots the entire Raspberry Pi (not just the Lynx "
                      "software) via 'sudo reboot'. Takes roughly 30-60 "
                      "seconds for the Pi to come back up and be reachable "
                      "again. Requires the user Lynx runs as to have "
                      "passwordless sudo for this command - the default on "
                      "standard Raspberry Pi OS. Checked synchronously "
                      "before responding, rather than assumed.")
def restart_lynx():
    # 'sudo -n' fails immediately with a clear, non-zero exit code if it
    # would otherwise prompt for a password, rather than hanging or
    # silently doing nothing - exactly the failure mode confirmed live
    # as the likely cause of the reboot button appearing to do nothing:
    # the async reboot below is fire-and-forget by design (it has to
    # be - the server can't wait around for its own reboot to finish),
    # so without this check, a missing passwordless-sudo setup would
    # return a false "success" that never actually reboots anything,
    # with no way for the operator to know why.
    check = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
    if check.returncode != 0:
        raise HTTPException(status_code=500,
            detail="'sudo reboot' requires passwordless sudo for this user, which "
                   "isn't currently configured - the Pi was NOT rebooted. Run "
                   "'sudo visudo' and add a NOPASSWD entry for reboot, or reboot "
                   "manually over SSH instead.")

    def _do_reboot():
        time.sleep(1.0)  # let this HTTP response actually reach the browser first
        subprocess.Popen(["sudo", "reboot"])
    threading.Thread(target=_do_reboot, daemon=True).start()
    return {"success": True, "message": "Rebooting the Pi - back in about a minute"}

class VolumeRequest(BaseModel):
    level: int  # 0-100

@app.get("/api/volume", tags=["Control"],
         summary="Get current playback volume",
         description="Returns mpv's current volume, 0-100. 100 is unity "
                     "gain (no amplification/attenuation) — the correct "
                     "reference point for content already mastered to "
                     "EBU R128 standards, where a 24-bit source's normal "
                     "programme level sits around -18 dBFS.")
def get_volume():
    result = mpv_query({"command": ["get_property", "volume"]})
    if result and result.get("error") == "success":
        return {"success": True, "level": round(result.get("data", 100))}
    return {"success": False, "level": None}

@app.post("/api/volume", tags=["Control"],
          summary="Set playback volume",
          description="0-100, applied immediately via mpv's IPC socket. "
                      "100 is unity gain — see GET /api/volume for the "
                      "EBU R128 reference rationale. This only changes "
                      "the volume for the current session; use "
                      "POST /api/volume/default to change the level "
                      "applied automatically on every future startup.")
def set_volume(req: VolumeRequest):
    global current_volume
    level = max(0, min(100, req.level))
    current_volume = level
    mpv_cmd({"command": ["set_property", "volume", level]})
    return {"success": True, "level": level}

@app.post("/api/volume/default", tags=["Control"],
          summary="Set the default boot-time volume",
          description="Persists to lynx_config.yaml so this level is "
                      "applied automatically every time Lynx starts, "
                      "not just for the current session.")
def set_default_volume(req: VolumeRequest):
    level = max(0, min(100, req.level))
    config.setdefault('audio', {})['default_volume'] = level
    save_config(config)
    return {"success": True, "default_volume": level}

class DefaultBootRequest(BaseModel):
    freq: int
    sr: int
    plug: str = "a"
    lnb_lo_khz: int = 0

@app.post("/api/boot-default", tags=["Control"],
          summary="Set the default boot-time RF preset",
          description="Persists a frequency/SR/plug/LNB combination as "
                      "the fallback tune used on startup whenever there "
                      "is no valid previous state to resume (e.g. a "
                      "genuinely first boot, or a corrupted state file). "
                      "On any restart Lynx first tries to resume exactly "
                      "what it was last doing — this is only the safety "
                      "net for when that isn't possible.")
def set_boot_default(req: DefaultBootRequest):
    config['default_boot_preset'] = {
        "freq": req.freq, "sr": req.sr, "plug": req.plug, "lnb_lo_khz": req.lnb_lo_khz
    }
    save_config(config)
    return {"success": True, "default_boot_preset": config['default_boot_preset']}

@app.get("/api/boot-default", tags=["Control"],
         summary="Get the current default boot-time RF preset")
def get_boot_default():
    return config.get('default_boot_preset')

# ── API: Configuration ────────────────────────────────────────
@app.get("/api/config", tags=["Configuration"],
         summary="Get current configuration")
def get_config():
    return config

@app.post("/api/config", tags=["Configuration"],
          summary="Save site and/or Picotuner configuration",
          description="Updates only the submitted sections, preserving everything else "
                      "(presets, streams, and all other settings) completely untouched.")
def update_config(req: ConfigUpdateRequest):
    global config
    # Read the actual on-disk file fresh, not the in-memory config -
    # avoids clobbering anything changed directly on disk since the
    # last reload, and guarantees every other section (presets,
    # streams, ryde, relay, dial, web, diversity,
    # default_boot_preset) is preserved byte-for-byte.
    with open(CONFIG_PATH) as f:
        on_disk = yaml.safe_load(f)

    picotuner_changed = False
    if req.site is not None:
        on_disk.setdefault('site', {}).update(req.site.model_dump())
    if req.picotuner is not None:
        new_pt = req.picotuner.model_dump()
        picotuner_changed = on_disk.get('picotuner', {}) != {**on_disk.get('picotuner', {}), **new_pt}
        on_disk.setdefault('picotuner', {}).update(new_pt)

    diversity_changed = False
    if req.diversity is not None:
        new_div = req.diversity.model_dump(exclude_none=True)
        # update() merges these in without disturbing enabled/
        # combiner_out_port/rcv1_plug/rcv2_plug, which this endpoint
        # never sees or sends. exclude_none above means a save from
        # either the MER-hysteresis card or the hard-freeze-recovery
        # card only touches its own fields, leaving the other
        # untouched, rather than requiring every field from both on
        # every single save.
        current_div = on_disk.get('diversity', {})
        # Only the MER-hysteresis fields actually require a restart -
        # they're passed as combiner CLI args, read once at process
        # launch. The hard_freeze_breaker_* fields are read fresh from
        # config on every check (mpv_drift_monitor's own loop) and take
        # effect immediately. Checked only against keys THIS request
        # actually included - otherwise a breaker-only save would
        # compare its own absent MER keys (None) against their real,
        # unrelated saved values and wrongly report a restart needed.
        RESTART_NEEDED_KEYS = ('mer_switch_dwell_secs', 'mer_switch_margin_db')
        diversity_changed = any(
            k in new_div and current_div.get(k) != new_div.get(k) for k in RESTART_NEEDED_KEYS
        )
        on_disk.setdefault('diversity', {}).update(new_div)

    # Notifications: none of these ever require a restart. QRZ/Slack/
    # Companion settings are re-read fresh on every poll (NotificationManager
    # holds a getter, not a captured config reference). The GPIO pin object
    # itself is also rebuilt automatically the moment its pin/polarity
    # config changes (see NotificationManager._poll_tx_pin's cfg_key check) -
    # so even that takes effect live, no restart needed.
    if req.notifications_qrz is not None:
        on_disk.setdefault('notifications', {}).setdefault('qrz', {}).update(
            req.notifications_qrz.model_dump())
    if req.notifications_slack is not None:
        on_disk.setdefault('notifications', {}).setdefault('slack', {}).update(
            req.notifications_slack.model_dump())
    if req.notifications_companion is not None:
        on_disk.setdefault('notifications', {}).setdefault('companion', {}).update(
            req.notifications_companion.model_dump())
    if req.notifications_gpio_tx is not None:
        on_disk.setdefault('notifications', {}).setdefault('gpio_tx', {}).update(
            req.notifications_gpio_tx.model_dump())

    tmp_path = str(CONFIG_PATH) + ".tmp"
    with open(tmp_path, 'w') as f:
        yaml.safe_dump(on_disk, f, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, CONFIG_PATH)

    config = on_disk  # reload in-memory immediately - site fields take effect right away
    return {
        "success": True,
        "restart_required": picotuner_changed or diversity_changed,
    }

@app.post("/api/config/reload", tags=["Configuration"],
          summary="Reload configuration from disk")
def reload_config():
    global config
    config = load_config()
    return {"success": True}

@app.get("/api/notifications/gpio-pins", tags=["Configuration"],
         summary="List usable physical GPIO pins",
         description="Physical (board) pin numbers with their BCM equivalent shown "
                     "together, excluding power/ground pins and the two HAT-EEPROM-"
                     "reserved pins (27/28).")
def gpio_pin_list():
    return [{"pin": p, "label": lynx_notifications.pin_label(p)}
            for p in lynx_notifications.USABLE_PHYSICAL_PINS]

# ── Web UI ────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def web_ui():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Lynx DATV Receiver</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background: #1a1a2e; color: #e0e0e0; }
        .card { background: #16213e; border: 1px solid #0f3460; color: #e0e0e0; }
        .card-header { background: #0f3460; color: #ffffff; font-weight: 500; }
        .btn-primary { background: #e94560; border-color: #e94560; }
        .btn-primary:hover { background: #c73652; border-color: #c73652; }
        .badge-locked { background: #28a745; }
        .badge-unlocked { background: #dc3545; }
        .status-value { font-family: monospace; color: #00d4aa; font-size: 1.1em; word-break: break-word; text-align: right; }
        .lynx-title { color: #e94560; font-weight: bold; letter-spacing: 2px; }
        .stream-item { cursor: pointer; transition: background 0.2s; }
        .stream-item:hover { background: #0f3460 !important; }
        /* Bootstrap's default .text-muted is calibrated for light
           backgrounds and is nearly illegible on this dark theme —
           override to a lighter grey that still reads as
           de-emphasised without disappearing. */
        .text-muted { color: #a8b5c7 !important; }
        #status-panel .d-flex > span:first-child, #status-panel-b .d-flex > span:first-child { color: #dce3ec; }
        /* Both tuner panels share identical sizing — previously
           tuner A had an explicit 1.15em override that tuner B
           never got, making them visually inconsistent. Reduced
           rather than raising tuner B up to match, for a more
           compact overall look. */
        #status-panel, #status-panel-b { font-size: 1em; }
        #status-panel .d-flex, #status-panel-b .d-flex { margin-bottom: 0.15rem !important; }
        .led { display: inline-block; width: 16px; height: 16px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
        .led-green { background: #00ff00; box-shadow: 0 0 10px #00ff00, 0 0 20px #00ff00; }
        .led-amber { background: #ffcc00; box-shadow: 0 0 10px #ffcc00, 0 0 20px #ffcc00; }
        .led-red   { background: #ff2020; box-shadow: 0 0 10px #ff2020, 0 0 20px #ff2020; }
        .led-grey  { background: #444; }
    </style>
</head>
<body>
<div class="container-fluid py-3">
    
    <!-- Header -->
    <div class="row mb-3">
        <div class="col">
            <h2 class="lynx-title">&#x25B6; LYNX DATV RECEIVER</h2>
            <small class="text-muted" id="site-name">Loading...</small>
            <!-- Diversity: replaces the site-name line above with a highlighted stats box while diversity mode is active -->
            <div id="diversity-stats-line" style="display:none; background:#0f3460; color:#ffffff; font-weight:500; padding: 4px 10px; border-radius: 4px; font-size: 1rem;"></div>
        </div>
        <div class="col-auto d-flex align-items-start gap-2 pt-1">
            <span><span class="led led-grey" id="picotuner-led"></span><small id="picotuner-status" class="text-muted">Picotuner</small></span>
            <span class="badge bg-secondary" id="mode-badge">IDLE</span>
            <a href="/diagnostics" class="badge bg-secondary text-decoration-none" title="mpv restart/stop diagnostics" id="diagnostics-link">mpv: <span id="mpv-restart-count">0</span></a>
            <a href="/diagnostics" class="btn btn-sm btn-outline-light">&#x1F4CA; Diagnostics</a>
            <a href="/config" class="btn btn-sm btn-outline-light">&#x2699;&#xFE0F; Config</a>
        </div>
    </div>

    <div class="row g-3">
        
        <!-- Status Panel -->
        <div class="col-md-4">
            <div class="card">
                <div class="card-header">&#x1F4E1; Tuner Rx 1</div>
                <div class="card-body" id="status-panel">
                    <div class="text-center text-muted py-3">Loading...</div>
                </div>
            </div>
            <!-- Diversity: second tuner's own native status, shown only when diversity mode is active -->
            <div class="card mt-2" id="diversity-panel-b" style="display:none">
                <div class="card-header">&#x1F4E1; Tuner Rx 2 (Diversity)</div>
                <div class="card-body" id="status-panel-b"></div>
            </div>
        </div>

        <!-- RF Tuning -->
        <div class="col-md-4">
            <div class="card">
                <div class="card-header">&#x1F4FB; RF Reception (Picotuner)</div>
                <div class="card-body">
                    <!-- Presets -->
                    <h6 class="text-muted">Presets</h6>
                    <div id="preset-list" class="mb-3" style="max-height: 220px; overflow-y: auto;">
                        <div class="text-muted small">Loading presets...</div>
                    </div>
                    <hr>
                    <!-- Manual tune -->
                    <h6 class="text-muted">Manual Tune</h6>
                    <div class="row g-2">
                        <div class="col">
                            <input type="number" class="form-control form-control-sm bg-dark text-light border-secondary" 
                                   id="freq-input" placeholder="Freq (kHz)" value="437000">
                        </div>
                        <div class="col">
                            <input type="number" class="form-control form-control-sm bg-dark text-light border-secondary"
                                   id="sr-input" placeholder="SR (kS/s)" value="333">
                        </div>
                    </div>
                    <div class="row g-2 mt-1">
                        <div class="col">
                            <select class="form-select form-select-sm bg-dark text-light border-secondary"
                                    id="lnb-select" onchange="onLnbSelectChange()" title="LNB local oscillator — freq above is the real downlink frequency when set">
                                <option value="0">No LNB (direct)</option>
                                <option value="9750000">Ku 9750 MHz (QO-100 std.)</option>
                                <option value="10600000">Ku 10600 MHz</option>
                                <option value="10750000">Ku 10750 MHz</option>
                                <option value="5150000">C-band 5150 MHz (3.4 GHz)</option>
                                <option value="custom">Custom...</option>
                            </select>
                        </div>
                        <div class="col" id="lnb-custom-col" style="display:none">
                            <input type="number" class="form-control form-control-sm bg-dark text-light border-secondary"
                                   id="lnb-custom-input" placeholder="LNB LO (kHz)">
                        </div>
                    </div>
                    <div class="mt-2 d-flex gap-2">
                        <select class="form-select form-select-sm bg-dark text-light border-secondary" id="plug-select">
                            <option value="a">Plug A (top)</option>
                            <option value="b">Plug B (bottom)</option>
                            <option value="diversity">Diversity (A+B)</option>
                        </select>
                        <button class="btn btn-primary btn-sm" onclick="tuneTo()">Tune</button>
                        <button class="btn btn-outline-warning btn-sm" onclick="saveMemory()" title="Save current frequency/SR as a preset">&#x1F4BE;</button>
                        <button class="btn btn-outline-info btn-sm" onclick="saveBootDefault()" title="Use this as the fallback frequency on startup, if there's nothing to resume">&#x1F3E0;</button>
                    </div>
                    <div id="boot-default-note" class="text-muted small mt-1"></div>
                </div>
            </div>

            <!-- Streams -->
            <div class="card mt-3">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <span>&#x1F4F6; Network Streams</span>
                    <button class="btn btn-outline-light btn-sm" onclick="refreshLiveStreams()">&#x21BB; Refresh</button>
                </div>
                <div class="card-body p-0">
                    <div id="stream-list" style="max-height: 300px; overflow-y: auto;">
                        <div class="text-muted small p-3">Loading live streams...</div>
                    </div>
                    <div class="p-2 border-top border-secondary">
                        <div class="input-group input-group-sm">
                            <input type="text" class="form-control bg-dark text-light border-secondary" 
                                   id="custom-url" placeholder="Custom URL (rtmp/srt/udp/rtsp)">
                            <button class="btn btn-outline-light" onclick="playCustom()">Play</button>
                            <button class="btn btn-outline-warning" title="Save as a Dial memory"
                                    onclick="saveStreamMemory(document.getElementById('custom-url').value.trim(), '')">&#x1F4BE;</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Control & Config -->
        <div class="col-md-4">
            <div class="card">
                <div class="card-header">&#x2699;&#xFE0F; Control</div>
                <div class="card-body">
                    <div class="d-flex gap-2 mb-2">
                        <button class="btn btn-danger flex-fill" onclick="stopAll()">&#x23F9; Stop</button>
                        <button class="btn btn-warning flex-fill" onclick="restartLynx()">&#x1F504; Reboot</button>
                    </div>
                    <hr>
                    <h6 class="text-muted">Volume</h6>
                    <div class="d-flex align-items-center gap-2 mb-1">
                        <span style="font-size:1.1em">&#x1F50A;</span>
                        <input type="range" class="form-range flex-grow-1" id="volume-slider"
                               min="0" max="100" value="100"
                               oninput="onVolumeInput(this.value)" onchange="setVolume(this.value)">
                        <span class="status-value" id="volume-value" style="min-width:3.2em; text-align:right;">100%</span>
                    </div>
                    <div class="text-muted small mb-2">
                        100% = unity gain (0 dB) — correct reference for
                        24-bit sources already mastered to EBU R128
                        (normal programme level ≈ -18 dBFS).
                    </div>
                    <div class="input-group input-group-sm mb-2">
                        <span class="input-group-text bg-dark text-light border-secondary" style="font-size:0.8em">Default on boot</span>
                        <input type="number" class="form-control bg-dark text-light border-secondary"
                               id="default-volume-input" min="0" max="100" value="100">
                        <button class="btn btn-outline-light" onclick="saveDefaultVolume()">Save</button>
                    </div>
                    <hr>
                    <h6 class="text-muted">API Quick Reference</h6>
                    <div class="small text-muted">
                        <div><code>GET /api/status</code> — live status</div>
                        <div><code>POST /api/tune</code> — tune RF</div>
                        <div><code>POST /api/stream</code> — play stream</div>
                        <div><code>POST /api/volume</code> — set volume</div>
                        <div><code>POST /api/stop</code> — stop</div>
                        <div><code>GET /api/streams/live</code> — BATC live list</div>
                        <div class="mt-1"><a href="/docs" target="_blank" class="text-info">Full API docs &#x2192;</a></div>
                    </div>
                </div>
            </div>

            <div class="card mt-3">
                <div class="card-header d-flex justify-content-between align-items-center">
                    <span>&#x1F4CB; Current Config</span>
                    <a href="/config" class="btn btn-sm btn-outline-light py-0">Edit</a>
                </div>
                <div class="card-body small" id="config-panel">
                    <div class="text-muted">Loading...</div>
                </div>
            </div>
        </div>

    </div>
</div>

<script>
// ── API helpers ───────────────────────────────────────────────
async function api(method, path, body) {
    const opts = { method, headers: {'Content-Type': 'application/json'} };
    if (body) opts.body = JSON.stringify(body);
    const r = await fetch(path, opts);
    return r.json();
}

// ── Status polling ────────────────────────────────────────────
async function updateStatus() {
    try {
        const s = await api('GET', '/api/status');
        
        // Mode badge
        const mode = s.lynx?.mode || 'idle';
        const badge = document.getElementById('mode-badge');
        badge.textContent = mode.toUpperCase();
        badge.className = 'badge ' + (mode === 'idle' ? 'bg-secondary' : mode === 'rf' ? 'bg-success' : 'bg-info');

        // mpv restart counter - links through to /diagnostics for detail
        const restartCount = s.lynx?.mpv_restarts_total ?? 0;
        document.getElementById('mpv-restart-count').textContent = restartCount;
        
        // Picotuner LED — in diversity mode, "locked" should mean
        // EITHER tuner is locked, since the combiner can produce a
        // perfectly good picture from just one healthy receiver.
        // Previously only ever checked tuner A, so pulling A's
        // antenna showed "Searching" even while B alone was locked
        // and the combined output was working completely fine.
        const pt = s.picotuner || {};
        const div = s.diversity || {};
        const effectiveLocked = pt.locked || (div.enabled && div.tuner_b?.locked);
        const effectiveOnline = pt.online || (div.enabled && div.tuner_b?.online);
        const led = document.getElementById('picotuner-led');
        const ptLabel = document.getElementById('picotuner-status');
        if (effectiveOnline && effectiveLocked) {
            led.className = 'led led-green';
            ptLabel.textContent = (pt.locked ? pt.callsign : div.tuner_b?.callsign) || 'Locked';
            ptLabel.className = 'small fw-bold'; ptLabel.style.color='#00ff00';
        } else if (effectiveOnline && !effectiveLocked) {
            led.className = 'led led-amber';
            ptLabel.textContent = 'Searching';
            ptLabel.className = 'small fw-bold'; ptLabel.style.color='#ffcc00';
        } else {
            led.className = 'led led-red';
            ptLabel.textContent = 'Offline';
            ptLabel.className = 'small fw-bold'; ptLabel.style.color='#ff4040';
        }

        // Status panel — RF, stream, or offline
        const panel = document.getElementById('status-panel');
        const locked = pt.locked;
        const lynxMode = s.lynx?.mode || 'idle';

        if (lynxMode === 'stream') {
            const info = s.lynx?.stream_info || {};
            const bitrate = info.bitrate_kbps;
            const protocol = s.lynx?.stream_protocol;
            const rows = [
                ['Mode',       '<span class="badge bg-info">STREAMING</span>'],
                ['Stream',     s.lynx?.stream_name || '—'],
                ['Protocol',   protocol || '—'],
                ['Bitrate',    bitrate != null ? bitrate.toFixed(0) + ' kbps' : '—'],
                ['Video',      info.video_codec || '—'],
                ['Audio',      info.audio_codec || '—'],
            ];
            panel.innerHTML = rows.map(r =>
                '<div class="d-flex justify-content-between mb-1" style="flex-wrap:wrap; gap: 4px 12px;"><span>' + r[0] + '</span>' +
                '<span class="status-value">' + r[1] + '</span></div>'
            ).join('');
        } else if (!pt.online) {
            panel.innerHTML = '<div class="text-danger small text-center mt-2">Picotuner offline</div>';
        } else if (locked) {
            const rows = [
                ['Lock',      '<span class="badge bg-success">LOCKED</span>'],
                ['Callsign',  pt.callsign || '—'],
                ['Programme', pt.programme || '—'],
            ];
            if (pt.lnb_lo_khz && pt.downlink_frequency != null) {
                rows.push(['Downlink', pt.downlink_frequency.toFixed(3) + ' MHz']);
                rows.push(['IF (L-band)', pt.frequency ? pt.frequency + ' MHz' : '—']);
                rows.push(['LNB LO', (pt.lnb_lo_khz/1000).toFixed(3) + ' MHz']);
            } else {
                rows.push(['Frequency', pt.frequency ? pt.frequency + ' MHz' : '—']);
            }
            rows.push(
                ['Symbol Rate', pt.symbol_rate ? pt.symbol_rate + ' kS/s' : '—'],
                ['MER',       pt.mer ? pt.mer + ' dB' : '—'],
                ['Margin',    pt.margin ? pt.margin + ' dB' : '—'],
                ['Level',     pt.dbm ? pt.dbm + ' dBm' : (pt.level ? '-' + pt.level + ' dBm' : '—')],
                ['Mode',      pt.modcod || '—'],
                ['Codec',     pt.codec || '—'],
                ['Audio Codec', pt.audio_codec || '—'],
                ['Firmware',  '<span style="font-size:0.85em">' + (pt.firmware || '—') + '</span>'],
            );
            panel.innerHTML = rows.map(r =>
                '<div class="d-flex justify-content-between mb-1" style="flex-wrap:wrap; gap: 4px 12px;"><span>' + r[0] + '</span>' +
                '<span class="status-value">' + r[1] + '</span></div>'
            ).join('');
        } else {
            panel.innerHTML =
                '<div class="d-flex justify-content-between mb-2"><span>Lock</span>' +
                '<span class="badge bg-danger">NO LOCK</span></div>' +
                '<div class="text-muted small text-center mt-2">Searching for signal...</div>';
        }

        // Diversity: second tuner panel, only shown while diversity mode is active
        const panelB = document.getElementById('diversity-panel-b');
        if (div.enabled) {
            panelB.style.display = '';
            const b = div.tuner_b || {};
            const bodyB = document.getElementById('status-panel-b');
            if (b.online && b.locked) {
                const rowsB = [
                    ['Lock',      '<span class="badge bg-success">LOCKED</span>'],
                    ['Callsign',  b.callsign || '—'],
                    ['Programme', b.programme || '—'],  // ptwh0v3k+ (2026-07-23): now genuinely available for rcv=2, confirmed in the live $0,2 capture
                    ['Frequency', b.frequency ? b.frequency + ' MHz' : '—'],
                    ['Symbol Rate', b.symbol_rate ? b.symbol_rate + ' kS/s' : '—'],
                    ['MER',       b.mer ? b.mer + ' dB' : '—'],
                    ['Margin',    b.margin ? b.margin + ' dB' : '—'],
                    ['Level',     b.dbm ? b.dbm + ' dBm' : '—'],  // ptwh0v3k+ (2026-07-23): now genuinely available for rcv=2
                    ['Mode',      b.modcod || '—'],
                    ['Codec',     b.codec || '—'],
                    ['Audio Codec', b.audio_codec || '—'],
                    ['Firmware',  '<span style="font-size:0.85em">' + (b.firmware || '—') + '</span>'],
                ];
                bodyB.innerHTML = rowsB.map(r =>
                    '<div class="d-flex justify-content-between mb-1" style="flex-wrap:wrap; gap: 4px 12px;"><span>' + r[0] + '</span>' +
                    '<span class="status-value">' + r[1] + '</span></div>'
                ).join('');
            } else if (b.online) {
                bodyB.innerHTML = '<div class="text-muted small text-center mt-2">Searching for signal...</div>';
            } else {
                bodyB.innerHTML = '<div class="text-danger small text-center mt-2">Offline</div>';
            }
            // Live combining stats — the combiner's own rolling window,
            // not a cumulative-since-start figure. Shown in place of
            // the site-name line at the top while diversity is active.
            const statsLine = document.getElementById('diversity-stats-line');
            const siteName = document.getElementById('site-name');
            statsLine.style.display = '';
            siteName.style.display = 'none';
            const st = div.stats;
            if (st) {
                statsLine.textContent =
                    `Diversity: A ${st.window_pct_a?.toFixed(0) ?? '—'}% \u00b7 B ${st.window_pct_b?.toFixed(0) ?? '—'}% \u00b7 gaps ${st.window_pct_gap?.toFixed(1) ?? '—'}%`;
            } else {
                statsLine.textContent = 'Diversity: combiner starting...';
            }
        } else {
            panelB.style.display = 'none';
            document.getElementById('diversity-stats-line').style.display = 'none';
            document.getElementById('site-name').style.display = '';
        }
    } catch(e) {
        document.getElementById('status-panel').innerHTML = '<div class="text-danger small">Status unavailable</div>';
    }
}

// ── Load presets ──────────────────────────────────────────────
async function loadPresets() {
    try {
        const data = await api('GET', '/api/presets');
        const local = (data.local || []).map(p => ({...p, _local: true}));
        const all = [...local, ...(data.ryde || [])];
        const el = document.getElementById('preset-list');
        if (!all.length) { el.innerHTML = '<div class="text-muted small">No presets</div>'; return; }
        el.innerHTML = all.map(p => `
            <div class="d-flex align-items-center gap-1 mb-1">
                <button class="btn btn-outline-secondary btn-sm flex-grow-1 text-start text-light" 
                        onclick="tunePreset('${p.name}')">
                    ${(p.type === 'stream') ? '&#x1F4F6; ' : ''}${p.name}
                    ${p.freq ? '<small class="text-muted float-end">' + (p.freq/1000).toFixed(3) + ' MHz</small>' : ''}
                </button>
                ${p._local ? `<button class="btn btn-outline-danger btn-sm" title="Delete" onclick="deletePreset('${p.name}')">&times;</button>` : ''}
            </div>
        `).join('');
    } catch(e) {}
}

function getLnbLoKhz() {
    const sel = document.getElementById('lnb-select').value;
    if (sel === 'custom') {
        return parseInt(document.getElementById('lnb-custom-input').value) || 0;
    }
    return parseInt(sel) || 0;
}

function onLnbSelectChange() {
    const isCustom = document.getElementById('lnb-select').value === 'custom';
    document.getElementById('lnb-custom-col').style.display = isCustom ? '' : 'none';
}

async function saveMemory() {
    const freq = parseInt(document.getElementById('freq-input').value);
    const sr = parseInt(document.getElementById('sr-input').value);
    const plug = document.getElementById('plug-select').value;
    const lnb_lo_khz = getLnbLoKhz();
    if (!freq || !sr) return;
    const name = prompt('Name this preset:', `${(freq/1000).toFixed(3)} MHz`);
    if (name === null) return;  // cancelled
    const result = await api('POST', '/api/presets/add', {type: 'rf', freq, sr, plug, lnb_lo_khz, name});
    if (result?.note === 'already saved') {
        alert('A preset with this exact name, frequency, symbol rate, plug, and LNB LO already exists — not saved as a duplicate.');
    } else if (result?.note === 'name already used') {
        alert(`A preset named "${name}" already exists with different tuning — please choose a different name.`);
    }
    await loadPresets();
}

async function saveStreamMemory(url, suggestedName) {
    if (!url) { alert('No stream URL to save.'); return; }
    const name = prompt('Name this stream memory:', suggestedName || '');
    if (name === null) return;  // cancelled
    if (!name.trim()) { alert('A stream memory needs a name.'); return; }
    const result = await api('POST', '/api/presets/add', {type: 'stream', url, name});
    if (result?.note === 'already saved') {
        alert('A memory with this exact name and URL already exists — not saved as a duplicate.');
    } else if (result?.note === 'name already used') {
        alert(`A memory named "${name}" already exists — please choose a different name.`);
    }
    await loadPresets();
}

async function saveBootDefault() {
    const freq = parseInt(document.getElementById('freq-input').value);
    const sr = parseInt(document.getElementById('sr-input').value);
    const plug = document.getElementById('plug-select').value;
    const lnb_lo_khz = getLnbLoKhz();
    if (!freq || !sr) return;
    if (!confirm(`Use ${(freq/1000).toFixed(3)} MHz / ${sr} kS/s as the fallback ` +
                 `frequency on startup, whenever there's nothing previous to resume?`)) return;
    await api('POST', '/api/boot-default', {freq, sr, plug, lnb_lo_khz});
    await loadBootDefault();
}

async function loadBootDefault() {
    try {
        const data = await api('GET', '/api/boot-default');
        const note = document.getElementById('boot-default-note');
        if (data && data.freq) {
            note.textContent = `Default boot: ${(data.freq/1000).toFixed(3)} MHz / ${data.sr} kS/s`;
        } else {
            note.textContent = 'No default boot preset set';
        }
    } catch(e) {}
}

async function deletePreset(name) {
    if (!confirm(`Remove preset "${name}"?`)) return;
    await api('POST', '/api/presets/remove', {name});
    await loadPresets();
}

// ── Load live streams ─────────────────────────────────────────
async function loadLiveStreams() {
    const el = document.getElementById('stream-list');
    el.innerHTML = '<div class="text-muted small p-3">Fetching live streams...</div>';
    try {
        const data = await api('GET', '/api/streams/live');
        if (!data.streams?.length) {
            el.innerHTML = '<div class="text-muted small p-3">No live streams right now</div>';
            return;
        }
        // Show cache age in the card header
        const age = data.cache_age_seconds;
        const ageStr = age < 60 ? `${age}s ago` : `${Math.floor(age/60)}m ago`;
        const btn = document.querySelector('[onclick="refreshLiveStreams()"]'); if(btn) btn.textContent = `↻ ${ageStr}`;
        
        el.innerHTML = data.streams.map((s,i) => `
            <div class="stream-item p-2 border-bottom border-secondary d-flex justify-content-between align-items-center"
                 data-url="${s.url}" data-name="${s.name}" data-idx="${i}"
                 onclick="playStream(this.dataset.url, this.dataset.name)">
                <span class="small text-light">${s.name}</span>
                <span class="d-flex align-items-center gap-1">
                    ${s.repeater ? '<span class="badge bg-primary" style="font-size:0.65em">REP</span>' : ''}
                    <button class="btn btn-outline-warning btn-sm py-0 px-1" title="Save as a Dial memory"
                            onclick="event.stopPropagation(); saveStreamMemory(this.closest('.stream-item').dataset.url, this.closest('.stream-item').dataset.name)">&#x1F4BE;</button>
                </span>
            </div>
        `).join('');
    } catch(e) {
        el.innerHTML = '<div class="text-danger small p-3">BATC API unavailable</div>';
    }
}

async function refreshLiveStreams() {
    try {
        await api('POST', '/api/streams/refresh');
    } catch(e) {}
    await loadLiveStreams();
}

// ── Load config summary ───────────────────────────────────────
async function loadConfig() {
    try {
        const cfg = await api('GET', '/api/config');
        document.getElementById('site-name').textContent = cfg.site?.name || 'Lynx Receiver';
        document.getElementById('config-panel').innerHTML = `
            <div class="d-flex justify-content-between"><span>Picotuner</span><span class="status-value">${cfg.picotuner?.host}</span></div>
            <div class="d-flex justify-content-between"><span>Callsign</span><span class="status-value">${cfg.site?.callsign}</span></div>
            <div class="d-flex justify-content-between"><span>Location</span><span class="status-value">${cfg.site?.locator}</span></div>
        `;
    } catch(e) {}
}

// ── Actions ───────────────────────────────────────────────────
async function tuneTo() {
    const freq = parseInt(document.getElementById('freq-input').value);
    const sr = parseInt(document.getElementById('sr-input').value);
    const plug = document.getElementById('plug-select').value;
    const lnb_lo_khz = getLnbLoKhz();
    const tunerFreq = freq - lnb_lo_khz;
    if (tunerFreq < 50000 || tunerFreq > 2500000) {
        alert(`Calculated tuner frequency ${(tunerFreq/1000).toFixed(3)} MHz is out of range.\n` +
              `Check the LNB LO selection matches the frequency entered — nothing was sent.`);
        return;
    }
    await api('POST', '/api/tune', {freq, sr, plug, lnb_lo_khz});
}

async function tunePreset(name) {
    await api('POST', '/api/preset', {name});
}

async function playStream(url, name) {
    await api('POST', '/api/stream', {url, name: name || ''});
}

async function playCustom() {
    const url = document.getElementById('custom-url').value.trim();
    if (url) await playStream(url);
}

async function stopAll() {
    await api('POST', '/api/stop');
}

async function restartLynx() {
    if (!confirm('Reboot the whole Pi? This will take the receiver and this page ' +
                 'offline for roughly 30-60 seconds while it comes back up.')) {
        return;
    }
    try {
        const result = await api('POST', '/api/restart');
        if (result && result.detail) {
            // A genuine, informative error response (e.g. passwordless
            // sudo isn't configured) - the Pi was NOT rebooted. The
            // shared api() helper doesn't check the HTTP status, so
            // this has to be checked explicitly here rather than
            // relying on the catch block below to ever see it.
            alert('Could not reboot: ' + result.detail);
            return;
        }
    } catch (e) {
        // The server may already be going down by the time this
        // resolves - not itself a sign anything went wrong.
    }
    alert('Rebooting the Pi - give it about a minute, then refresh this page.');
}

// ── Volume ────────────────────────────────────────────────────
let volumeDebounce = null;

function onVolumeInput(value) {
    // Update the live readout immediately as the slider moves, but
    // debounce the actual API call so dragging doesn't flood requests
    document.getElementById('volume-value').textContent = value + '%';
    clearTimeout(volumeDebounce);
    volumeDebounce = setTimeout(() => setVolume(value), 150);
}

async function setVolume(value) {
    await api('POST', '/api/volume', {level: parseInt(value)});
}

async function saveDefaultVolume() {
    const value = parseInt(document.getElementById('default-volume-input').value);
    await api('POST', '/api/volume/default', {level: value});
}

async function loadVolume() {
    try {
        const data = await api('GET', '/api/volume');
        if (data.level != null) {
            document.getElementById('volume-slider').value = data.level;
            document.getElementById('volume-value').textContent = data.level + '%';
        }
    } catch(e) {}
    try {
        const cfg = await api('GET', '/api/config');
        const defaultVol = cfg.audio?.default_volume ?? 100;
        document.getElementById('default-volume-input').value = defaultVol;
    } catch(e) {}
}

// ── Init ──────────────────────────────────────────────────────
loadConfig();
loadPresets();
loadLiveStreams();
loadVolume();
loadBootDefault();
updateStatus();
setInterval(updateStatus, 3000);
setInterval(loadLiveStreams, 3600000);
</script>
</body>
</html>"""

# ── Run ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    # Start Picotuner monitor background threads
    monitor = threading.Thread(target=picotuner_monitor, daemon=True)
    monitor.start()
    quality = threading.Thread(target=picotuner_quality_monitor, daemon=True)
    quality.start()
    quality_b = threading.Thread(target=picotuner_table_monitor_b, daemon=True)
    quality_b.start()
    freshness = threading.Thread(target=rf_mpv_lifecycle_monitor, daemon=True)
    freshness.start()
    mer_pub = threading.Thread(target=mer_publisher, daemon=True)
    mer_pub.start()
    decoder_health = threading.Thread(target=mpv_decoder_health_monitor, daemon=True)
    decoder_health.start()
    connectivity = threading.Thread(target=picotuner_connectivity_monitor, daemon=True)
    connectivity.start()
    modcod_monitor = threading.Thread(target=picotuner_modcod_monitor, daemon=True)
    modcod_monitor.start()
    drift_monitor = threading.Thread(target=mpv_drift_monitor, daemon=True)
    drift_monitor.start()
    rss_monitor = threading.Thread(target=memory_rss_monitor, daemon=True)
    rss_monitor.start()
    dial_discovery = threading.Thread(target=dial_discovery_responder, daemon=True)
    dial_discovery.start()
    # Repeater-activity notifications (QRZ/Slack/Companion/GPIO Tx) - its
    # own, independent monitor, deliberately not gated on current_mode the
    # way rf_mpv_lifecycle_monitor() is (see lynx_notifications.py's own
    # docstring for why). `lambda: config` is passed rather than the
    # config object itself, since config gets REASSIGNED (not just
    # mutated) on every save - a captured reference would silently go
    # stale after the first config change.
    notification_manager = lynx_notifications.NotificationManager(
        picotuner_state, picotuner_state_b, lambda: config,
        record_event=record_diagnostic_event,
        get_lnb_state=lambda: (current_lnb_lo_khz, current_lnb_side))
    notification_manager.start()
    print("Picotuner monitor started.")

    # Pre-populate BATC stream cache on startup
    print("Fetching BATC live streams...")
    try:
        _batc_cache = fetch_batc_streams_from_api()
        _batc_cache_time = time.time()
        print(f"  {len(_batc_cache)} active streams cached.")
    except Exception as e:
        print(f"  BATC API unavailable at startup: {e}")

    # Apply the configured default boot volume to mpv once it's
    # actually running. mpv is launched by lynx_start.sh, separately
    # from this app, and only starts once a genuine RF lock (or a
    # stream) is confirmed - which could take anywhere from a few
    # seconds to much longer, entirely dependent on when a signal
    # actually shows up. Confirmed live as a genuine bug: the previous
    # version made a single, blind attempt after a fixed 6s sleep
    # using mpv_cmd() (fire-and-forget - it swallows connection
    # errors, giving no way to know if the command actually landed).
    # If mpv wasn't up yet at that exact moment, the default was
    # silently never applied for the rest of that boot session - mpv
    # would go on to start later at its own native 100% default
    # instead, with nothing left to correct it. Now polls with
    # mpv_query() (which returns a real, checkable response) until it
    # genuinely succeeds, rather than hoping a single blind attempt
    # landed at the right moment.
    def _apply_default_volume():
        global current_volume
        default_vol = config.get('audio', {}).get('default_volume', 100)
        while True:
            result = mpv_query({"command": ["set_property", "volume", default_vol]})
            if result and result.get("error") == "success":
                current_volume = default_vol
                print(f"Applied default volume: {default_vol}%")
                return
            time.sleep(2.0)
    threading.Thread(target=_apply_default_volume, daemon=True).start()

    # Resume whatever Lynx was last doing before this restart — crash,
    # watchdog recovery, scheduled 12-hour reboot, or a genuine power
    # cycle. Falls back to the explicitly-configured default boot
    # preset only when there's no valid saved state to resume (e.g. a
    # genuinely first boot, or a corrupted/missing state file) — this
    # is deliberately a separate, explicit setting rather than always
    # falling back to some arbitrary hardcoded frequency, so unattended
    # repeater-site equipment always comes up somewhere sensible.
    def _resume_tune(freq, sr, plug, lnb_lo_khz):
        """tune(), with a follow-up identical re-tune for diversity mode
        specifically. Confirmed live: if a signal is already present
        before a diversity tune command arrives at startup (e.g. the
        operator's Tx was switched on before Lynx had finished
        rebooting), one receiver's own in-progress lock acquisition can
        be left in a stuck state - correctly tuned, decent MER, but
        never locking - that a manual re-tune to the exact same
        frequency reliably clears. This automates that same, already-
        proven recovery step once at startup, rather than requiring a
        manual re-tune every time this happens.

        Deliberately scoped to the startup-resume path only, not every
        diversity tune - there's no evidence this risk applies outside
        the specific "signal already present when Lynx boots" scenario,
        and doubling every manual preset switch's tune time in the
        normal, already-running case would be a real, felt cost for no
        proven benefit.

        tune()'s own lock serialises the two calls naturally - the
        second blocks until the first's async mpv-restart thread has
        genuinely finished and released the lock, not just been
        accepted, so no explicit delay needs guessing at here."""
        tune(TuneRequest(freq=freq, sr=sr, plug=plug, lnb_lo_khz=lnb_lo_khz))
        if plug.lower() == "diversity":
            print("Diversity mode - sending a second, identical tune command as a "
                  "precaution against a stuck-acquisition state on one receiver")
            tune(TuneRequest(freq=freq, sr=sr, plug=plug, lnb_lo_khz=lnb_lo_khz))

    def _resume_on_startup():
        time.sleep(7)  # let mpv/overlay settle first
        state = load_last_state()
        if state and state.get("mode") == "rf":
            target_tuner_freq = calc_tuner_freq(state["freq"], state.get("lnb_lo_khz", 0))
            already_tuned = False
            try:
                if picotuner_state["locked"]:
                    live_freq_mhz = float(picotuner_state["frequency"])
                    live_sr = float(picotuner_state["symbol_rate"])
                    # Picotuner reports frequency in MHz with limited
                    # precision — small tolerances account for that,
                    # not for genuine mistuning.
                    already_tuned = (
                        abs(live_freq_mhz - target_tuner_freq / 1000) < 0.01 and
                        abs(live_sr - state["sr"]) < 1
                    )
            except (ValueError, TypeError):
                pass

            if already_tuned and state.get("plug", "a").lower() != "diversity":
                # The Picotuner is a physical device — it doesn't forget
                # its own tuning just because Lynx restarts. If it's
                # already locked on exactly the frequency/SR we'd be
                # asking for, skip the whole tune()/restart_mpv() cycle
                # entirely. Calling tune() unconditionally here was
                # triggering an avoidable mpv restart on every single
                # startup, even when the picture was already live and
                # correct — genuinely unnecessary, and any instability
                # in that restart path was being triggered needlessly
                # on every boot as a result.
                #
                # Deliberately excluded for diversity mode: this check
                # only looks at rcv=1's own lock state, with no idea
                # whether rcv=2 is tuned or whether the combiner is
                # even running. Skipping the full tune() here would
                # risk resuming with mpv still pointed at the raw
                # single-tuner port instead of the combiner's output.
                print(f"Already locked on {state['freq']} kHz / {state['sr']} kS/s — skipping resume tune.")
                global current_mode, current_preset, current_lnb_lo_khz
                current_mode = "rf"
                current_preset = f"{state['freq']/1000:.3f} MHz / {state['sr']} kS/s"
                current_lnb_lo_khz = state.get("lnb_lo_khz", 0)
                return

            print(f"Resuming previous RF state: {state.get('freq')} kHz / {state.get('sr')} kS/s")
            try:
                _resume_tune(
                    freq=state["freq"], sr=state["sr"],
                    plug=state.get("plug", "a"),
                    lnb_lo_khz=state.get("lnb_lo_khz", 0)
                )
                return
            except Exception as e:
                print(f"Could not resume previous RF state: {e}")
        elif state and state.get("mode") == "stream":
            print(f"Resuming previous stream: {state.get('name')}")
            try:
                start_stream(StreamRequest(url=state["url"], name=state.get("name", "")))
                return
            except Exception as e:
                print(f"Could not resume previous stream: {e}")

        # No valid previous state — fall back to the explicit default
        # boot preset, if one has been configured.
        default_preset = config.get('default_boot_preset')
        if default_preset:
            print(f"No previous state — using default boot preset: {default_preset}")
            try:
                _resume_tune(
                    freq=default_preset["freq"], sr=default_preset["sr"],
                    plug=default_preset.get("plug", "a"),
                    lnb_lo_khz=default_preset.get("lnb_lo_khz", 0)
                )
            except Exception as e:
                print(f"Could not apply default boot preset: {e}")
        else:
            print("No previous state and no default boot preset configured — staying idle.")
    threading.Thread(target=_resume_on_startup, daemon=True).start()

    cfg = config.get('web', {})
    uvicorn.run(app, host=cfg.get('host', '0.0.0.0'),
                port=cfg.get('port', 8080))
