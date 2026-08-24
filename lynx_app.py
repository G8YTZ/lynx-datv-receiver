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
import faulthandler
import json
import threading
import os
import re
import signal
import socket
import shlex
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
import lynx_gnss
import lynx_map

# Auto-Squeak needs numpy, which older installs may not have - it was
# added to install.sh at the same time as this feature, and anyone who
# updates by copying files rather than running the installer will not
# have picked it up. A missing optional feature must never stop the
# receiver starting, so this degrades to "Auto-Squeak unavailable"
# rather than taking Lynx down on an import error.
try:
    import lynx_squeak
    SQUEAK_AVAILABLE = True
except Exception as _e:
    lynx_squeak = None
    SQUEAK_AVAILABLE = False
    print(f"[squeak] unavailable ({_e}) - install numpy to enable "
          f"audio measurement: pip install --break-system-packages numpy")
import lynx_rtmp_probe

# Diagnostic: dump every thread's current stack trace to a log file on
# demand (kill -USR1 <pid of lynx_app.py>) - added 2026-08-01 after a
# real, confirmed incident where the Web UI became completely
# unresponsive while mpv (a genuinely separate process) kept running
# fine - strongly suggesting something stuck INSIDE this process (a
# held lock, a hung background thread) rather than anything display/
# GPU-related. lynx_start.sh's own health-check loop also triggers
# this automatically right before concluding the web app isn't
# responding, so a dump gets captured at the actual moment of failure
# without needing anyone to catch it manually in the act. Purely
# diagnostic and read-only - registering this cannot itself cause the
# hang it's meant to help catch, and does nothing at all unless the
# signal is actually sent.
#
# Prefers /var/log/lynx/ (persistent - install.sh creates this with
# the right ownership on a fresh install) over /tmp, which is
# typically RAM-backed on Raspberry Pi OS - confirmed directly that a
# second, later freeze lost this exact log for that reason, right when
# it would have mattered most. Falls back to /tmp rather than fail to
# start at all if /var/log/lynx/ doesn't exist yet and can't be
# created - this process runs as a regular user, not root, so it
# can't reliably create a new directory under /var/log/ itself if
# install.sh hasn't already set it up with the right ownership.
try:
    os.makedirs("/var/log/lynx", exist_ok=True)
    _faulthandler_log = open('/var/log/lynx/stacktrace.log', 'a')
except OSError:
    _faulthandler_log = open('/tmp/lynx_stacktrace.log', 'a')
faulthandler.register(signal.SIGUSR1, file=_faulthandler_log, all_threads=True)

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

# Every writer of lynx_config.yaml (the /api/config POST handler, the
# update-channel switcher, and the GNSS confirmed-fix callback) reads
# the file fresh, modifies it, writes to the SAME "<config>.tmp" path,
# then os.replace()s it into place - and none of that was ever guarded
# by a lock, even before GNSS existed. Two human-triggered saves
# genuinely landing in the same instant was rare enough not to matter
# in practice. GNSS changes that: its callback is an AUTOMATIC,
# high-frequency writer that can fire every time a fix is confirmed,
# which meaningfully raises the odds of colliding with someone editing
# Config in a browser tab at the same time - two processes opening the
# same tmp path concurrently can truncate each other's write, and
# whichever os.replace() lands second wins outright, silently
# discarding the other save in full (not just the fields it touched).
# One shared lock around the read-modify-write-replace cycle, used by
# every writer, closes this for GNSS and for the two pre-existing
# paths at the same time.
_config_write_lock = threading.Lock()

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

# ── GNSS (portable locator) ─────────────────────────────────────
# Waveshare L76K HAT, GPIO header UART - see lynx_gnss.py's own
# docstring for why /dev/ttyAMA0 rather than the /dev/serial0 symlink,
# and why 9600 baud. Fixed here rather than exposed in config: a
# repeater site simply doesn't have the HAT fitted, and GnssReader
# already fails quietly and keeps retrying when there's nothing on
# the port - so there is nothing here for a fixed site to configure
# wrong. What IS configurable is gnss.mode (see GnssConfigUpdate) -
# whether a fix, once confirmed, is trusted to drive the locator at
# all.

def _on_gnss_locator_change(locator: str):
    """Called by GnssReader only once a fix has held the same 6-char
    square for the full stability window (30s by default) - i.e. on a
    genuinely confirmed fix, not on every NMEA sentence.

    Writes straight into notifications.qrz.portable_locator, the same
    field an operator would otherwise type by hand into the Config
    page's QRZ card. This is not a new field: lynx_notifications.
    submit_qrz_logbook's own docstring already anticipated exactly
    this - "a future, automated source - an onboard GPS module ...
    could populate the same underlying config value on its own, and
    this function would need no changes at all to use it." GPS is
    just that automated source now.

    Only actually writes in "automatic" mode. "GPS always wins when
    there's a fix" is a decision about automatic mode specifically -
    manual mode's entire point is a fixed, operator-chosen value that
    GPS must never silently overwrite, even with a HAT fitted and
    locked. Any mode value other than "automatic" (which today just
    means "manual", but also covers a legacy "off" from a config
    written before that mode was removed) is treated the same way:
    don't write.

    Uses the shared _config_write_lock (see its own comment by
    CONFIG_PATH) - this fires from GnssReader's own background thread,
    completely independent of and concurrent with any /api/config POST
    a browser tab might be doing at the same moment, and both write the
    same file via the same tmp path. Distinct from tune_lock, which
    serializes an unrelated pair of operations (tune/start_stream)."""
    global config
    with _config_write_lock:
        mode = config.get('gnss', {}).get('mode', 'automatic')
        if mode != 'automatic':
            return
        on_disk = load_config()
        on_disk.setdefault('notifications', {}).setdefault('qrz', {})['portable_locator'] = locator
        tmp_path = str(CONFIG_PATH) + ".tmp"
        with open(tmp_path, 'w') as f:
            yaml.safe_dump(on_disk, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, CONFIG_PATH)
        config = on_disk
        print(f"[gnss] confirmed fix - portable_locator set to {locator}")

gnss_reader = lynx_gnss.GnssReader(
    port="/dev/ttyAMA0", baud=9600, stable_secs=30.0, length=6,
    on_change=_on_gnss_locator_change,
    # Mode 7 = GPS + BeiDou + GLONASS, all three the L76K can actually
    # be told to use - QZSS is always on regardless and can't be
    # configured, so this is every constellation available. Sent once
    # via $PCAS04 on each serial connect; the module keeps the setting
    # in its own memory afterwards. The board ships as GPS + GLONASS
    # only - this was discussed and settled on, but never actually
    # passed through until now.
    constellations=7,
)

# Fixed, like the port/baud above - matched by a chrony.conf refclock
# entry install.sh sets up automatically. Not user-configurable:
# there's nothing about this path that varies per installation, only
# whether it's used at all (gnss.time_sync).
GNSS_CHRONY_SOCK_PATH = "/var/run/chrony.gnss.sock"

def _apply_gnss_mode():
    """Applies gnss.time_sync live. Called once at startup and again
    after every /api/config save that touches the gnss section.

    The reader itself is started once, unconditionally, at startup
    (see the bottom of this file) and stays running for the life of
    the process - there is deliberately no mode that stops it. A site
    with no HAT fitted costs nothing either way: GnssReader already
    fails quietly and keeps retrying forever with nothing on the port.
    A site WITH a HAT fitted should almost always want at least GPS
    time sync active regardless of whether GPS is trusted to drive the
    locator - an earlier "Off" mode that stopped the whole reader,
    time sync included, defeated that for no benefit: Manual mode
    already gives an operator everything Off was for (GPS never
    touches portable_locator) without also losing the clock
    correction. Removed rather than kept as a third option nobody had
    a real use for.

    time_sync's own on/off is still separate from locator mode -
    useful even while Manual is driving the locator. Setting
    chrony_sock_path to None when disabled means _run()'s next
    reconnect simply won't attempt a chrony connection at all. Like
    constellations, this is only re-read once per serial reconnect,
    not instantaneously mid-connection - toggling it off takes effect
    on the next reconnect cycle, same limitation this module already
    has for constellation changes."""
    gnss_reader.chrony_sock_path = (
        GNSS_CHRONY_SOCK_PATH if config.get('gnss', {}).get('time_sync', True) else None
    )

def _gnss_provenance() -> str:
    """'gnss' if the value currently in portable_locator is live,
    GPS-derived data; 'config' if it's the operator's own configured
    value - because mode is manual, or because mode is automatic but
    there's no confirmed fix yet (cold start, indoors, or simply no
    HAT - a fixed repeater site, exactly as expected)."""
    mode = config.get('gnss', {}).get('mode', 'automatic')
    if mode == 'automatic' and gnss_reader.tracker.locator:
        return 'gnss'
    return 'config'

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
# Tri-watch (up to 3 sources - any mix of RF-A, RF-B, and a stream
# input - watched simultaneously) - Stage 1: infrastructure only.
# Generalized from an earlier, more limited "dual-watch" (RF-A + one
# fixed stream) design after a genuinely good real-world use case
# surfaced: two RF sources on the SAME frequency but different symbol
# rates, since a tuner given one specific, known symbol rate to lock
# onto (rather than scanning a range) does noticeably better on weak
# signals - this needed the source list to be generic (any type, any
# combination), not RF-vs-stream-shaped, so it was worth generalizing
# now rather than bolting a special case onto the earlier, narrower
# design.
#
# Each configured, enabled source is watched continuously in the
# background regardless of what's actually being displayed right now -
# no priority/arbitration logic yet (Stage 2+), and RF sources are NOT
# automatically tuned to their configured frequency/symbol-rate by this
# stage either (that's active control, not detection - deliberately
# left for Stage 2, matching the same scope boundary drawn for the
# original, narrower dual-watch design). For now, getting an RF
# source's configured tuning actually applied still means using the
# existing Manual Tune feature by hand.
#
# Created once at startup from config, since this was described as a
# fixed, permanent setup rather than something reconfigured often -
# changing tri_watch config currently needs a restart to take effect,
# not a live reload.
tri_watch_enabled: bool = False
tri_watch_sources_cfg = []  # a STABLE SNAPSHOT of config's tri_watch.sources[]
                             # taken once at startup, alongside tri_watch_probes
                             # below - deliberately NOT re-read live from config,
                             # unlike most other settings in this file. Confirmed
                             # as a real, reported bug otherwise: tri_watch_probes
                             # (and the arbitrator's own internal per-index state)
                             # are only ever built once at startup from this exact
                             # list; if the arbitrator's own loop or the status
                             # endpoint instead read the LIVE config.tri_watch.sources
                             # after a Config-page save (which reloads config
                             # immediately, before any restart), a source removed
                             # or reordered there would silently shift every
                             # later index - a stream probe built at startup
                             # index 2 would suddenly be looked up at whatever
                             # index 1 now means, mismatching real probes against
                             # the wrong sources and breaking tri_watch's actual,
                             # live status entirely, well before the restart the
                             # UI already warns is required actually happens.
tri_watch_probes = {}  # maps config's tri_watch.sources[] list index -> a
                        # lynx_rtmp_probe.RTMPStreamProbe instance, for
                        # type:stream sources only - RF sources need no
                        # separate probe object, since their status is
                        # already tracked by the existing
                        # picotuner_state/picotuner_state_b globals
tri_watch_arbitrator = None  # a TriWatchArbitrator instance once tri_watch
                              # is enabled, or None otherwise - actually
                              # instantiated further down, after its own
                              # class and callback functions are defined
tri_watch_target_rcv = None  # 1, 2, or None - which receiver tri_watch's
                              # own arbitrator currently wants displayed,
                              # if any (None while a stream is showing, or
                              # while idle). Read by rf_mpv_lifecycle_monitor()
                              # so it knows which receiver to actually watch
                              # and start mpv for, instead of assuming Rx1
                              # the way it always has for normal, non-
                              # tri_watch RF mode.
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
current_lnb_psu_a: str = config.get('lnb_psu', {}).get('plug_a', 'off')
                                # "off"/"lo"/"hi" - Plug A's (rcv=1's
                                # own) LNB PSU voltage, sent to the
                                # Picotuner as its own standalone
                                # command (VGX=), independent of
                                # tuning - see set_lnb_psu() for the
                                # full rationale.
current_lnb_psu_b: str = config.get('lnb_psu', {}).get('plug_b', 'off')
                                # Same as above, for Plug B (rcv=2's
                                # own LNB PSU / VGY=) - only relevant
                                # on units with the optional second
                                # LNB PSU board fitted.
current_lnb_tone_a: bool = config.get('lnb_psu', {}).get('plug_a_tone', False)
                                # Hi-Band LO tone for Plug A - defaults
                                # off (Lo-Band), since Amateur TV never
                                # uses the Hi-Band LO. Combines with
                                # current_lnb_psu_a into the Picotuner's
                                # own single combined value (see
                                # set_lnb_psu()).
current_lnb_tone_b: bool = config.get('lnb_psu', {}).get('plug_b_tone', False)
                                # Same as above, for Plug B.
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

# ── Versioning / update checking ────────────────────────────────
# LYNX_DIR is the repo root (this file's own directory) - used to run
# git commands against the right checkout regardless of exactly where
# it's installed, rather than assuming ~/lynx specifically.
LYNX_DIR = os.path.dirname(os.path.abspath(__file__))

update_state = {
    "current_version": None,   # populated lazily on first GET /api/update/status (see ensure_current_version)
    "checked_at": None,        # ISO timestamp of the last check (auto or manual)
    "check_error": None,       # set if the last check itself failed (e.g. no network) - distinct from "no update available"
    "update_available": False,
    "commits_behind": 0,
    "new_commits": [],         # list of "abc1234 commit subject" strings, most recent first
    "channel": None,           # 'stable' or 'beta' - populated lazily, same timing as current_version (see ensure_current_version)
}

def git_cmd(*args, timeout=15):
    """Run a git command against the Lynx repo. Never raises - callers
    just check the returned success flag, so a git failure (no
    network, corrupted checkout, misconfigured remote, etc) degrades
    to a clear error message rather than crashing anything that calls
    this."""
    try:
        env = os.environ.copy()
        # If a remote were ever configured to need credentials, this
        # makes git fail fast and cleanly instead of trying to prompt
        # interactively for them - there's no terminal attached here
        # to prompt on, and this is a defensive measure against that
        # ever behaving unpredictably rather than a confirmed fix for
        # a specific observed problem.
        env["GIT_TERMINAL_PROMPT"] = "0"
        result = subprocess.run(
            ["git", "-C", LYNX_DIR, *args],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode != 0:
            return False, result.stderr.strip() or f"git {' '.join(args)} failed"
        return True, result.stdout.strip()
    except Exception as e:
        return False, str(e)

def detect_current_version():
    """git describe --tags --always - falls back to a short commit SHA
    if no tags exist yet, which is genuinely true for every existing
    install until the first vYYYY.MM.DD tag is actually created and
    pushed. Called once at startup; the result doesn't change while
    this process keeps running (only a restart after a real update
    would pick up a new one)."""
    ok, out = git_cmd("describe", "--tags", "--always")
    return out if ok and out else "unknown"

def get_default_branch():
    """The actual default branch name (main/master/whatever) rather
    than assuming - confirmed correct either way, since guessing wrong
    here would silently compare against the wrong branch."""
    ok, ref = git_cmd("symbolic-ref", "refs/remotes/origin/HEAD")
    if ok and ref:
        return ref.rsplit("/", 1)[-1]
    return "main"  # reasonable fallback if the symbolic ref isn't set for some reason

def get_update_branch():
    """Which branch update checks/pulls should actually use, based on
    the configured update channel - added to support a beta channel
    (per Justin's own request) alongside the existing stable one,
    mirroring the model BATC's own Portsdown project already uses:
    regular users stay on the repo's normal default branch (whatever
    it's actually named - unaffected by any of this), while
    experimenters can opt into tracking a separate 'beta' branch for
    newer, less-proven code, and switch back to stable at any time.
    'beta' is intentionally a fixed, literal branch name rather than
    also going through get_default_branch()'s own auto-detection -
    there's only ever one beta branch, by definition, so there's
    nothing to detect."""
    channel = config.get('update', {}).get('channel', 'stable')
    if channel == 'beta':
        return 'beta'
    return get_default_branch()

def check_for_updates():
    """Safe and read-only: git fetch, then compare local HEAD to the
    remote's HEAD - never touches working files or applies anything.
    Called both by the background auto-check loop and the on-demand
    /api/update/check endpoint, so they share one, single code path
    rather than two that could quietly drift apart."""
    global update_state
    ok, err = git_cmd("fetch", "origin", "--quiet")
    if not ok:
        update_state["check_error"] = f"Could not reach GitHub: {err}"
        update_state["checked_at"] = utc_now_iso()
        return

    branch = get_update_branch()
    ok, behind_str = git_cmd("rev-list", "--count", f"HEAD..origin/{branch}")
    if not ok:
        update_state["check_error"] = f"Could not compare versions: {behind_str}"
        update_state["checked_at"] = utc_now_iso()
        return

    behind = int(behind_str) if behind_str.isdigit() else 0
    update_state["check_error"] = None
    update_state["checked_at"] = utc_now_iso()
    update_state["update_available"] = behind > 0
    update_state["commits_behind"] = behind

    if behind > 0:
        ok, log_out = git_cmd("log", f"HEAD..origin/{branch}", "--oneline", "--max-count=20")
        update_state["new_commits"] = log_out.split("\n") if ok and log_out else []
    else:
        update_state["new_commits"] = []

def ensure_current_version():
    """Detects the current version on first call only, caching the
    result - not a background thread, not periodic, nothing runs
    until this is actually called (from GET /api/update/status, which
    happens once when the page loads). git describe is local-only, no
    network needed, so this is safe regardless of connectivity - the
    thing that must never run automatically is the actual update
    CHECK (git fetch), which only ever happens from an explicit
    button press now. Not all Lynx receivers are reliably online,
    sometimes running RF-only with no internet at all - background
    network activity has no place interfering with that."""
    if update_state["current_version"] is None:
        try:
            update_state["current_version"] = detect_current_version()
        except Exception as e:
            update_state["current_version"] = "unknown"
            print(f"[update-check] version detection failed unexpectedly: {e}")
    if update_state["channel"] is None:
        update_state["channel"] = config.get('update', {}).get('channel', 'stable')


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

def current_rf_target_port():
    """Returns the UDP port mpv should currently be pointed at for RF
    playback, given whatever mode is actually active right now -
    tri_watch's own chosen receiver, diversity's combiner output, or
    normal single-receiver mode's own port.

    Centralises this decision specifically because it was previously
    duplicated, inconsistently, across multiple call sites - two of
    which (both inside mpv_decoder_health_monitor(), its playback-delay
    and hard-freeze restart triggers) were never updated for tri_watch
    at all, and kept silently restarting to Rx1's port even while
    tri_watch was actively, correctly displaying Rx2. Confirmed as a
    real, reproduced bug via the diagnostics timeline: a hard-freeze
    restart during a genuine tri_watch Rx2 session pointed mpv at the
    wrong receiver's port, which could never render there, producing a
    repeated "restart did not confirm rendering" failure specifically
    during Rx2 sessions - the freeze detection itself was correct, but
    the recovery attempt was restarting the wrong thing entirely.

    rf_mpv_lifecycle_monitor()'s own tri_watch-aware branch is left as
    it already was rather than switched over to this helper too - it's
    already proven correct and tested, and touching genuinely-working
    code for its own sake adds risk without benefit."""
    cfg = config['picotuner']
    if tri_watch_enabled and tri_watch_target_rcv == 2:
        return cfg['ts_port_b']
    elif diversity_enabled:
        return config['diversity']['combiner_out_port']
    else:
        return cfg['ts_port']

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
        f"{audio_device_flag()}"
        f"--audio-pitch-correction=no --script={DRIFT_SCRIPT_PATH} "
        f"{source_flags}"
        f"--keep-open=yes --idle=yes "
        f"--input-ipc-server={MPV_SOCKET} "
        f"'{target_url}'"
    )
    env = os.environ.copy()
    env["DISPLAY"] = ":0"
    env["XAUTHORITY"] = os.path.expanduser("~/.Xauthority")
    # Explicitly set rather than relying on inheritance from
    # lynx_app.py's own environment — that varied depending on how the
    # whole process chain was originally started (local desktop
    # terminal vs SSH session), causing intermittent video freezes
    # where mpv's playback stayed internally healthy (time-pos still
    # advancing, no eof) but nothing reached the actual display.
    # Was hardcoded to "/home/pi/..." until 2026-07-31 - broke for a
    # user who'd chosen a different username during setup (increasingly
    # common, since Raspberry Pi Imager now commonly prompts for a
    # custom username rather than defaulting to "pi"). Confirmed via a
    # real report: install completed and Lynx worked fine when started
    # manually, but not via the autostart line - installed by install.sh
    # with the same hardcoded assumption, fixed there too.
    env["XDG_RUNTIME_DIR"] = f"/run/user/{os.getuid()}"
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

def _recent_hevc_decoder_error(since_ts: float) -> bool:
    """Checks dmesg for the specific hardware HEVC decoder error
    ("rpi-hevc-dec ... Missing inuse DPB ent") directly tied to a real,
    confirmed freeze earlier this session - one a plain mpv restart
    couldn't recover from, only a full Pi reboot could. Scoped to
    entries from since_ts onwards specifically, not the whole kernel
    ring buffer (which persists across the entire uptime) - an old,
    already-resolved occurrence from hours or days ago must not keep
    incorrectly flagging every future render attempt as failed forever.
    Best-effort only: dmesg can require elevated permissions on some
    systems, or simply be unavailable - any failure here is treated as
    "no error found" rather than blocking rendering confirmation on
    something that isn't itself the actual video pipeline."""
    try:
        since_str = datetime.fromtimestamp(since_ts).strftime("%Y-%m-%d %H:%M:%S")
        out = subprocess.run(["dmesg", "-T", "--since", since_str],
                              capture_output=True, text=True, timeout=2)
        return "rpi-hevc-dec" in out.stdout and "DPB" in out.stdout
    except Exception as e:
        print(f"[mpv_render] dmesg check failed (non-fatal, treating as no error found): {e}")
        return False

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

    Also checks dmesg for a recent hardware HEVC decoder error before
    trusting mpv's own "AV:"/"VO:" markers - confirmed as a real gap
    otherwise: those markers only prove mpv's own software pipeline is
    progressing (demuxing, decoding, submitting frames onward), not
    that a frame actually reached the screen. If an earlier freeze left
    the GPU decoder itself wedged, a fresh mpv process can still print
    entirely normal progress markers while the hardware never produces
    a real frame - reported directly as a stream switch that "swapped
    OK" (this function returning True, cover dropped) but exposed the
    desktop underneath, recovered only by a full Pi reboot, not a
    simple mpv restart. Treating a detected decoder error as an
    immediate, definite failure rather than retrying within this same
    call - the hardware is confirmed wedged at that point, not just
    "not ready yet", so further polling here would only waste the
    remaining timeout.

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
                if _recent_hevc_decoder_error(start):
                    print(f"[mpv_render] mpv log shows rendering markers after {elapsed:.1f}s, "
                          f"but a hardware HEVC decoder error was also just logged - treating "
                          f"this as NOT genuinely rendering despite mpv's own pipeline appearing "
                          f"to progress normally (likely a wedged GPU decoder from an earlier "
                          f"crash - a full Pi reboot, not just an mpv restart, may be needed)")
                    record_diagnostic_event(
                        "mpv_render_hevc_decoder_error",
                        "Rendering markers present but a hardware HEVC decoder error was also "
                        "logged - treating as a failed render, GPU may be wedged and need a full "
                        "Pi reboot to clear",
                        count_as_mpv_restart=False)
                    return False
                print(f"[mpv_render] confirmed rendering after {elapsed:.1f}s")
                return True
        except OSError:
            pass
        time.sleep(0.1)
    print(f"[mpv_render] did NOT confirm rendering within {timeout:.1f}s timeout")
    return False

_audio_resolved_cache = {"value": "", "at": 0.0}


def _cached_audio_device_resolved():
    """What the configured device actually resolves to.

    Cached for a minute: resolving "hdmi" shells out to mpv, and the
    status endpoint is polled every second by the overlay - running mpv
    that often to answer a question whose answer almost never changes
    would be silly.
    """
    dev = str(config.get('display', {}).get('audio_device', 'hdmi')).strip()
    if dev.lower() != 'hdmi':
        return dev
    now = time.time()
    if now - _audio_resolved_cache["at"] > 60:
        _audio_resolved_cache["value"] = _first_hdmi_device() or ""
        _audio_resolved_cache["at"] = now
    return _audio_resolved_cache["value"]


def audio_device_flag():
    """The --audio-device flag for mpv, or nothing if set to auto.

    Defaults to HDMI rather than mpv's own auto-selection. Auto picks
    whatever ALSA offers first, and a USB audio dongle left plugged in
    outsorts the HDMI output - so sound disappears into a device nobody
    is listening to, with no indication anything is wrong. Reported by
    G8GKQ, who spent a while on it before finding a dongle was the
    cause. A receiver driving a television should send its audio to the
    television unless told otherwise.
    """
    dev = str(config.get('display', {}).get('audio_device', 'hdmi')).strip()
    if not dev or dev.lower() == 'auto':
        return ""
    if dev.lower() == 'hdmi':
        # Ask mpv for the first HDMI output ALSA is offering. Resolved
        # at launch rather than baked in, since the card number varies
        # between Pi models and between HDMI ports.
        found = _first_hdmi_device()
        if not found:
            print("[mpv] audio_device is 'hdmi' but no HDMI output found - "
                  "letting mpv choose")
            return ""
        dev = found
    return f"--audio-device={shlex.quote(dev)} "


def list_audio_devices():
    """Every audio output mpv can see, as (name, description) pairs.

    Asks mpv itself rather than parsing ALSA directly, so the names are
    exactly what --audio-device will accept.
    """
    out = []
    try:
        r = subprocess.run(["mpv", "--audio-device=help"],
                           capture_output=True, text=True, timeout=10)
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line.startswith("'"):
                continue
            # Format: 'name' (Description)
            try:
                name = line.split("'")[1]
            except IndexError:
                continue
            desc = ""
            if "(" in line:
                desc = line[line.index("(") + 1:].rstrip(")").strip()
            if name and name != "auto":
                out.append({"name": name, "description": desc or name})
    except Exception as e:
        print(f"[mpv] could not list audio devices: {e}")
    return out


def physical_audio_devices():
    """One entry per real output, and via PipeWire where it exists.

    Two things matter here, and the first version got the second wrong.

    mpv's raw list is mostly plumbing: a Pi with two HDMI ports reports
    around twenty devices, being several ALSA access paths to each card
    plus backend defaults for pipewire, pulse, alsa, jack, sdl and
    sndio. Only a couple are things anyone would recognise.

    But on a PipeWire system the "pipewire/..." entries are the ones to
    use, not the raw ALSA ones. PipeWire owns the sound card; telling
    mpv to open alsa/sysdefault:CARD=... directly either fights it for
    the device or bypasses the graph altogether. Filtering those entries
    out as "not physical devices" produced exactly that - no sound at
    all, and no PPM either, since nothing then appeared on any PipeWire
    node for the meter to tap.

    So: prefer PipeWire node entries when they exist, and fall back to
    grouped ALSA cards only on systems without it. The bonus is that a
    PipeWire device name carries its node name, which makes the PPM's
    monitor target exact rather than something to be inferred.
    """
    raw = list_audio_devices()

    pw = []
    for d in raw:
        name = d["name"]
        if name.startswith("pipewire/"):
            pw.append({
                "name": name,
                "description": _friendly_audio_name(name, d["description"]),
                "node": name.split("/", 1)[1],
            })
    if pw:
        return pw

    # No PipeWire: group the ALSA access paths down to one per card.
    # sysdefault first because it does software format conversion;
    # plughw and hw are the least forgiving, dmix is a mixing layer.
    PREFER = ["sysdefault", "default", "hdmi", "dmix", "plughw", "hw"]
    by_card = {}
    for d in raw:
        name = d["name"]
        card = _alsa_card_from_device_name(name)
        if not card:
            continue
        access = name.split("/", 1)[1].split(":", 1)[0] if "/" in name else ""
        rank = PREFER.index(access) if access in PREFER else len(PREFER)
        if card not in by_card or rank < by_card[card][0]:
            by_card[card] = (rank, d)

    return [{"name": d["name"],
             "description": _friendly_audio_name(card, d["description"]),
             "node": ""}
            for card, (_, d) in sorted(by_card.items())]


def _alsa_card_from_device_name(name):
    """The ALSA card in an mpv device string, or None if it hasn't one."""
    m = re.search(r'CARD=([A-Za-z0-9_\-]+)', name or "")
    return m.group(1) if m else None


def _friendly_audio_name(key, description):
    """Something a person would recognise, rather than
    'vc4-hdmi-0, MAI PCM i2s-hifi-0/Default Audio Device'."""
    blob = f"{key} {description}"
    m = re.search(r'vc4hdmi(\d+)', key, re.I)
    if m:
        return f"HDMI {int(m.group(1)) + 1}"
    if "hdmi" in blob.lower():
        # PipeWire names its HDMI sinks by platform address rather than
        # port number, so there is nothing reliable to number them by.
        return description.split("(")[0].strip() or "HDMI"
    base = description.split("/")[0].strip()
    base = re.sub(r',\s*(MAI PCM|USB Audio).*$', '', base).strip()
    return base or key


def _first_hdmi_device():
    """The first HDMI output mpv reports, or None."""
    # Uses the same filtered list the Config page offers, so the
    # automatic choice and the visible choice can never disagree.
    for d in physical_audio_devices():
        if "hdmi" in (d["name"] + " " + d["description"]).lower():
            return d["name"]
    return None


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
# Unrecognised "LNB supply X/Y" values already reported, so each one is
# logged once rather than at one broadcast per second.
_lnb_unknown_seen = set()

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

def looks_like_callsign(token):
    """Is this plausibly an amateur callsign?

    The parser used to accept anything that was not "search", "lost" or
    empty. That is a blocklist, and blocklists are always one surprise
    behind: the Picotuner also emits "header", which sailed through and
    produced 53 pointless QRZ lookups in a single overnight session -
    one every eight minutes, none of which could ever succeed.

    So test positively instead. Every callsign in the world has at least
    one digit and at least two letters, which "header", "search",
    "lost", "idle" and anything else of that kind all fail. Deliberately
    permissive about length and about "/" and "-", so portable and
    special-event suffixes are not rejected.
    """
    if not token:
        return False
    t = str(token).strip().upper()
    if not (3 <= len(t) <= 16):
        return False
    if not all(c.isalnum() or c in "/-" for c in t):
        return False
    if not any(c.isdigit() for c in t):
        return False
    if sum(1 for c in t if c.isalpha()) < 2:
        return False
    # A short blocklist as well, for the handful of status tokens that
    # happen to share a valid callsign's shape. Secondary to the test
    # above, not a replacement for it - "K1A" is a real callsign and
    # "RX1" is not, and nothing about their form distinguishes them.
    if t in {"RX1", "RX2", "RX3", "RX4"}:
        return False
    return True


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

            # Discovery: this is a genuine broadcast, so it arrives here
            # from EVERY Picotuner on the local network, not just the
            # one currently configured - a side effect of how this
            # listener already works, not a separate discovery
            # mechanism. Confirmed live (2026-07-29) against a real v3
            # board's actual broadcast - format is a labelled table,
            # "Label   Value" separated by 2+ spaces (labels themselves
            # can contain a single space, e.g. "IP address"). Left
            # ungated deliberately - discovery genuinely wants every
            # unit, unlike the status parsing below.
            if "PicoTuner Broadcast" in text:
                fields = {}
                for line in text.splitlines():
                    parts = re.split(r'\s{2,}', line.strip())
                    if len(parts) == 2:
                        fields[parts[0].strip()] = parts[1].strip()
                mac = fields.get("MAC", "")
                if mac:
                    discovered_picotuners[mac] = {
                        "ip": fields.get("IP address", addr[0]),
                        "host_name": fields.get("Host name", ""),
                        "serial": fields.get("Pico serial", ""),
                        "software": fields.get("Software", ""),
                        "nim_type": fields.get("NIM type", ""),
                        "mac": mac,
                        "last_seen": time.time(),
                    }

            # Everything below drives the actual status/OSD display, so -
            # unlike discovery above - it must only trust broadcasts from
            # the specifically configured Picotuner. This is a genuine
            # broadcast port: with two units on the same network (a real,
            # likely scenario now that discovery makes them easy to spot),
            # an unfiltered listener would let whichever one's packet
            # happened to arrive most recently silently overwrite the
            # other's status - including showing "online" from a
            # completely unrelated unit while the actually-configured one
            # was genuinely offline.
            if addr[0] != cfg['host']:
                continue

            picotuner_state["online"] = True
            picotuner_state["last_seen"] = time.time()

            # Reflects the Picotuner's own reported LNB supply state
            # (voltage + Hi-Band tone), not just "a command was last
            # sent" - confirmed directly as a real gap: a voltage/tone
            # changed via the API directly, or a Picotuner that
            # already had a state configured before Lynx ever sent a
            # command, would otherwise show incorrectly. Same
            # "Label   Value" broadcast format as the discovery block
            # above (not reused directly since that dict is scoped to
            # this specific, gated, configured-host branch instead of
            # every broadcast on the network). Falls back gracefully
            # (leaves the existing value alone) if this field is ever
            # absent/unrecognized in a given broadcast (e.g. older
            # firmware) rather than incorrectly resetting to "off".
            global current_lnb_psu_a, current_lnb_psu_b, current_lnb_tone_a, current_lnb_tone_b
            # "absent" is NOT the same as "off" and must not be folded
            # into it. The PicoTuner has only one LNB voltage generator
            # fitted; a second (VGY, plug B) can be added by hand but
            # normally isn't, and the firmware reports "absent" to say
            # there is no hardware there to switch. Mapping that to
            # "off" made plug B look like a working control that simply
            # refused to stay on - the button went green on click and
            # reverted on the next broadcast, with no way to tell that
            # nothing was ever going to happen. Confirmed against the
            # BATC wiki and by sending vgy to every documented command
            # port (9920, 9921, 9922): the state never changes.
            lnb_supply_map = {
                "off": ("off", False), "absent": ("absent", False),
                "hi": ("hi", False), "hit": ("hi", True),
                "lo": ("lo", False), "lot": ("lo", True),
            }
            for line in text.splitlines():
                parts = re.split(r'\s{2,}', line.strip())
                if len(parts) != 2:
                    continue
                label, value = parts[0].strip().lower(), parts[1].strip().lower()
                if label in ("lnb supply x", "lnb supply y"):
                    if value in lnb_supply_map:
                        if label.endswith("x"):
                            current_lnb_psu_a, current_lnb_tone_a = lnb_supply_map[value]
                        else:
                            current_lnb_psu_b, current_lnb_tone_b = lnb_supply_map[value]
                    else:
                        # Deliberately logged rather than silently ignored.
                        # The existing value is still left alone (an
                        # unrecognised state must not be mistaken for
                        # "off"), but an unknown value is worth knowing
                        # about: the firmware already distinguishes real
                        # hardware states in this field ("absent" means no
                        # generator fitted), so a future overload or fault
                        # indication would most naturally appear here too.
                        # Without this, a new state would be invisible -
                        # Lynx would quietly carry on showing the last
                        # thing it understood. Logged once per distinct
                        # value so a persistent fault can't flood the log
                        # at one broadcast per second.
                        if value not in _lnb_unknown_seen:
                            _lnb_unknown_seen.add(value)
                            print(f"[picotuner] {parts[0].strip()} reports "
                                  f"'{parts[1].strip()}', which this version "
                                  f"doesn't recognise - leaving the displayed "
                                  f"state unchanged. If this is a fault or "
                                  f"overload indication, please report it so "
                                  f"it can be shown properly.")

            # Parse RX1 line: "437.024 G8YTZ" or "437.000T search"
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("RX1"):
                    rx1 = line.replace("RX1", "").strip()
                    picotuner_state["rx1_raw"] = rx1
                    parts = rx1.split()
                    # Not-locked states. "header" is WinterHill's
                    # intermediate state: it has found something
                    # transport-stream-shaped and is trying to acquire,
                    # but has no callsign and no picture yet - confirmed
                    # by watching the broadcast, where a "header" line
                    # carries a wandering frequency (436.085, 436.281,
                    # 436.351...) while "search" sits exactly on the
                    # tuned value. It was previously accepted as a
                    # callsign and looked up on QRZ 53 times overnight.
                    unlocked_states = {"search", "lost", "header", ""}
                    if (len(parts) >= 2 and parts[-1] not in unlocked_states
                            and looks_like_callsign(parts[-1])):
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
                    # Same set as RX1 above - see the note there on
                    # "header".
                    unlocked_states = {"search", "lost", "header", ""}
                    if (len(parts) >= 2 and parts[-1] not in unlocked_states
                            and looks_like_callsign(parts[-1])):
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
                        # Was: hardcoded to Rx1's port outside diversity
                        # mode, regardless of what tri_watch actually
                        # wanted displayed - see current_rf_target_port()'s
                        # own docstring for the confirmed, real bug this
                        # caused.
                        restart_mpv(f"udp://@:{current_rf_target_port()}")
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
                        # Was: hardcoded to Rx1's port outside diversity
                        # mode, regardless of what tri_watch actually
                        # wanted displayed - the same bug as the
                        # playback-delay trigger above, confirmed as the
                        # real, reproduced cause of "restart did not
                        # confirm rendering" specifically during
                        # tri_watch Rx2 sessions.
                        restart_mpv(f"udp://@:{current_rf_target_port()}")
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

DIVERSITY_STUCK_UNLOCKED_SECS = 25.0    # how long unlocked-with-margin counts as "stuck"
DIVERSITY_STUCK_MARGIN_THRESHOLD = 0.0  # a positive margin means this SHOULD be locking
DIVERSITY_STUCK_RETUNE_COOLDOWN_SECS = 60.0  # don't re-trigger more often than this
_diversity_stuck_since = {"a": None, "b": None}
_diversity_stuck_last_retune = 0.0

def diversity_stuck_lock_monitor():
    """Background thread, diversity mode only: watches for a receiver
    that's genuinely unlocked despite reporting a positive margin -
    see this patch's own module docstring for the full rationale and
    real-hardware evidence behind it."""
    global _diversity_stuck_last_retune
    while True:
        time.sleep(5)
        try:
            if not diversity_enabled:
                _diversity_stuck_since["a"] = None
                _diversity_stuck_since["b"] = None
                continue

            def check(key, state):
                if state.get("locked"):
                    _diversity_stuck_since[key] = None
                    return False
                try:
                    margin = float(state.get("margin", ""))
                except (ValueError, TypeError):
                    _diversity_stuck_since[key] = None
                    return False
                if margin <= DIVERSITY_STUCK_MARGIN_THRESHOLD:
                    _diversity_stuck_since[key] = None
                    return False
                if _diversity_stuck_since[key] is None:
                    _diversity_stuck_since[key] = time.time()
                    return False
                return (time.time() - _diversity_stuck_since[key]) >= DIVERSITY_STUCK_UNLOCKED_SECS

            stuck_a = check("a", picotuner_state)
            stuck_b = check("b", picotuner_state_b)

            if stuck_a or stuck_b:
                now = time.time()
                if now - _diversity_stuck_last_retune < DIVERSITY_STUCK_RETUNE_COOLDOWN_SECS:
                    continue
                last_state = load_last_state()
                if last_state and last_state.get("mode") == "rf" and last_state.get("plug", "").lower() == "diversity":
                    which = "A" if stuck_a else "B"
                    detail = f"Rx {which} unlocked with positive margin for {DIVERSITY_STUCK_UNLOCKED_SECS:.0f}s+ - auto re-tune sent"
                    print(f"Diversity: {detail}")
                    record_diagnostic_event("Diversity: stuck-lock auto-recovery", detail, count_as_mpv_restart=False)
                    _diversity_stuck_last_retune = now
                    _diversity_stuck_since["a"] = None
                    _diversity_stuck_since["b"] = None
                    tune(TuneRequest(
                        freq=last_state["freq"], sr=last_state["sr"],
                        plug=last_state["plug"], lnb_lo_khz=last_state.get("lnb_lo_khz", 0)
                    ))
        except Exception as e:
            print(f"[diversity_stuck_lock_monitor] error: {e}")

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
            # tri_watch, when enabled, gets its own genuinely separate
            # branch here rather than being skipped entirely - it needs
            # this same lock-confirm/start-mpv machinery just as much
            # as normal RF mode does, just watching whichever receiver
            # tri_watch_target_rcv currently points at (set by the
            # arbitrator) instead of always assuming Rx1. Kept as a
            # fully separate branch, not interleaved with the existing,
            # proven non-tri_watch logic below, specifically to leave
            # that logic completely untouched and at zero added risk.
            if tri_watch_enabled:
                if tri_watch_target_rcv is None:
                    # No RF source is what tri_watch currently wants
                    # displayed (a stream is showing, or it's idle) -
                    # nothing for this monitor to do.
                    lock_streak = 0
                    loss_streak = 0
                    continue
                cfg = config['picotuner']
                target_rcv = tri_watch_target_rcv
                target_state = picotuner_state_b if target_rcv == 2 else picotuner_state
                target_port = cfg['ts_port_b'] if target_rcv == 2 else cfg['ts_port']
                raw_locked = target_state.get("locked", False)

                if raw_locked:
                    loss_streak = 0
                    lock_streak += 1
                    if lock_streak >= LOCK_CONFIRM_POLLS and not mpv_running_for_rf:
                        if tune_lock.acquire(timeout=2):
                            try:
                                print(f"[rf_mpv_lifecycle/tri_watch] Confirmed lock on Rx{target_rcv} "
                                      f"after {lock_streak * POLL_SECS}s - starting mpv on {target_port}")
                                restart_mpv(f"udp://@:{target_port}")
                                rendering_confirmed = wait_for_mpv_rendering()
                                if rendering_confirmed:
                                    time.sleep(0.3)
                                    end_transition_cover()
                                    mpv_running_for_rf = True
                                    mpv_last_started_at = time.time()
                                    record_diagnostic_event("rf_lock_confirmed_start",
                                                      f"after {lock_streak * POLL_SECS}s idle (tri_watch Rx{target_rcv})")
                                else:
                                    print("[rf_mpv_lifecycle/tri_watch] mpv did not confirm rendering "
                                          "in time - keeping the cover up and retrying next poll")
                                    record_diagnostic_event("rf_lock_render_not_confirmed",
                                                      "mpv started but did not confirm rendering within "
                                                      "the timeout - will retry (tri_watch)", count_as_mpv_restart=False)
                            finally:
                                tune_lock.release()
                        # If the lock was busy, a user-initiated tune/
                        # stream switch is already in progress and will
                        # itself establish the correct mpv state - try
                        # again next poll.
                else:
                    lock_streak = 0
                    loss_streak += 1
                    if loss_streak >= LOSS_CONFIRM_POLLS and mpv_running_for_rf:
                        if tune_lock.acquire(timeout=2):
                            try:
                                print(f"[rf_mpv_lifecycle/tri_watch] Confirmed loss of lock on Rx{target_rcv} "
                                      "- stopping mpv rather than leaving it running with no data")
                                start_transition_cover()
                                kill_mpv()
                                mpv_running_for_rf = False
                                record_diagnostic_event("rf_loss_confirmed_stop",
                                                  f"after {loss_streak * POLL_SECS}s of confirmed loss (tri_watch Rx{target_rcv})")
                            finally:
                                tune_lock.release()
                continue

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

# Any Picotuner heard on the local network, not just the one currently
# configured - this is a genuine UDP broadcast, so picotuner_quality_
# monitor() below already receives it from every unit on the segment,
# not just the configured one, purely as a side effect of how it's
# already listening. Keyed by MAC (the most stable identifier - IP can
# change on DHCP renewal, host name is operator-set and could collide).
# Pruned of stale entries (>10s since last seen) whenever read via the
# API, matching the same staleness window picotuner_state itself uses.
discovered_picotuners = {}

def picotuner_table_monitor_b():
    """Background thread: reads the rich table-format status from
    Picotuner port 9904 (a confirmed duplicate of 9902). Live-tested
    directly: this table contains BOTH receivers' rows together
    (RX 1 and RX 2), each with STATUS/CALLSIGN/MER/D(margin)/
    FREQUENCY/SR/MODULATION/FPRO/CODECS/ANT/PACKETS/%NUL/NIMTYPE/
    TS DESTINATION.

    SHARED-STATE WARNING: this is the ONLY function that writes fields
    picotuner_quality_monitor() also owns - specifically
    picotuner_state["mer"] and ["margin"] for tuner A. Every other
    field in picotuner_state / picotuner_state_b has exactly one
    writing function (verified by a full audit of both dicts). That
    shared ownership caused a real, confirmed bug: this function reads
    fixed whitespace COLUMN POSITIONS, whereas the 9901 monitor reads
    explicitly TAGGED fields ($12 = MER, $30 = margin). Column
    positions are not stable across firmware revisions, so on
    ptwh0v3k-w5100s this wrote non-numeric junk over values that had
    arrived correctly moments earlier, roughly twice a second - the
    Web UI showed MER and Margin permanently blank while tcpdump
    proved the correct values were being received. The writes below
    are now guarded so a column mismatch leaves the tagged values
    alone. If any further field is ever added here, check first
    whether the 9901 monitor already owns it, and guard it the same
    way - tagged data should always win over positional data.

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

    Rx2's row is genuinely variable-width, not a fixed 16-column
    layout - confirmed directly against real captured broadcasts: the
    Picotuner sends a shorter row whenever it doesn't yet have the
    full field set (no callsign while purely searching, a modulation-
    type field appearing partway through acquisition, different column
    counts seen live at different stages), and symbol_rate is present
    in every one of these shorter rows too, just at a different column
    position each time depending on what else is or isn't present.
    Rows with the full 16+ columns (once genuinely locked) are parsed
    by fixed column index as before; shorter rows use a second,
    position-independent extraction instead, built around the one
    pattern confirmed consistent across every real, observed variant:
    the tuned frequency (a number >= 50, matching the tuner's own
    valid 50-2500MHz range - low enough to rule out other numeric
    fields seen in these same rows, like MER or NUL%) is always
    immediately followed by the symbol rate, regardless of what comes
    before it in that particular row.
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
                    #
                    # Guarded because this is the SECOND writer of these
                    # two fields: picotuner_quality_monitor() already
                    # sets them from the $-tagged broadcast on 9901,
                    # where they arrive correctly tagged ($12 = MER,
                    # $30 = margin). This function instead reads fixed
                    # whitespace column positions from the table-format
                    # broadcast, and those positions are not stable
                    # across firmware revisions. Confirmed live on
                    # ptwh0v3k-w5100s: the $-tagged packets carried
                    # $12,25.0 and $30,14.3 while the Web UI showed
                    # both fields blank, because this ran afterwards
                    # and overwrote them with non-numeric junk from the
                    # wrong columns. MER and Margin were exactly - and
                    # only - the two fields affected, which is exactly
                    # the pair written here.
                    #
                    # Now only supplements when the columns genuinely
                    # hold numbers, so a column-layout mismatch quietly
                    # leaves the already-correct tagged values alone
                    # instead of destroying them.
                    def _numeric(tok):
                        try:
                            float(tok)
                            return True
                        except ValueError:
                            return False
                    # Only supplement when the TAGGED source hasn't
                    # provided a value yet - i.e. exactly the
                    # unlocked/searching case this branch exists for
                    # (see docstring). Once port 9901 is reporting,
                    # its tagged values are authoritative and are
                    # never overwritten from column positions here.
                    #
                    # A numeric-only guard is NOT sufficient on its
                    # own: this table's rows are variable-width (the
                    # docstring above documents that for Rx2, and the
                    # same acquisition-stage variation applies to
                    # Rx1), so a shifted layout can put a genuine but
                    # WRONG number in parts[3]/parts[4] - which would
                    # pass a numeric check and silently corrupt a good
                    # reading rather than blanking it. That is the
                    # intermittent, hard-to-reproduce form of this same
                    # bug: values that look plausible but are wrong,
                    # appearing and clearing at random depending on
                    # what the tuner happened to include in the row.
                    if not picotuner_state.get("mer"):
                        if _numeric(parts[3]) and _numeric(parts[4]):
                            picotuner_state["mer"] = parts[3]
                            picotuner_state["margin"] = parts[4]
                    continue
                if parts[0] == '2' and len(parts) < 16:
                    # Shorter row - see docstring above. Still worth
                    # checking for a usable symbol_rate even though it
                    # doesn't have the full field set for MER/margin/
                    # modcod/etc.
                    for i, token in enumerate(parts):
                        try:
                            freq_val = float(token)
                        except ValueError:
                            continue
                        if freq_val >= 50 and i + 1 < len(parts) and parts[i + 1].isdigit():
                            picotuner_state_b["symbol_rate"] = parts[i + 1]
                            break
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
    rcv: int = 1             # 1 or 2 — which actual receiver circuit
                             # (NOT the same thing as plug, which only
                             # selects the physical antenna input — see
                             # tri_watch's own config comments for the
                             # full rcv/fplug distinction). Only
                             # meaningful outside diversity mode, which
                             # always tunes both receivers together.
                             # Defaults to 1, preserving every existing
                             # caller's behaviour exactly - added
                             # specifically so tri_watch's arbitrator
                             # can reuse this exact, proven tune/display
                             # path for Rx2 too, rather than maintaining
                             # its own, separate reimplementation of it.

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

class SqueakConfigUpdate(BaseModel):
    # On by default - it is idle until a sequence arrives, and a
    # receiver that measures itself when someone sends a test is more
    # useful than one that must be configured first.
    enabled: bool = True
    # Blank means "monitor of the default output", which is what almost
    # everyone wants and needs no maintenance if the output changes.
    source: str = ""
    # Worth matching to the gap between passes in the test file, so the
    # previous result is still on screen while an adjustment is made.
    hold_secs: float = 45.0


class GnssConfigUpdate(BaseModel):
    # automatic (default): GPS always wins once it has a confirmed,
    # stable fix (see lynx_gnss.LocatorTracker) - it overwrites
    # portable_locator itself, live, no restart. Until then - cold
    # start, indoors, or simply no HAT - portable_locator is untouched,
    # so it quietly falls back to whatever is already configured there.
    # manual: the operator's own typed value in the QRZ card is
    # authoritative and GPS, even if a HAT is fitted and locked, is
    # never allowed to overwrite it.
    #
    # No "off": an earlier version had one, stopping the whole reader.
    # Removed - a site with no HAT costs nothing either way (the
    # reader fails quietly and keeps retrying), and a site WITH a HAT
    # fitted should almost always want at least GPS time sync (see
    # time_sync below) even while keeping GPS out of the locator,
    # which Manual mode already gives with no downside. If an older
    # config still has "off" saved, it's treated the same as "manual"
    # (see _on_gnss_locator_change) - it just never gets cleaned up to
    # "manual" automatically.
    mode: str = "automatic"
    # Independent of mode above (deliberately - a fixed value entered
    # for Manual locator mode is still worth correcting the system
    # clock from, and the two questions "should GPS drive the
    # portable locator" and "should GPS feed chrony" are genuinely
    # separate ones). Default on: it fails quietly if chrony isn't
    # configured for it (see lynx_gnss._connect_chrony_sock), so
    # there's no downside to leaving it on for an install that hasn't
    # set up the chrony side yet.
    time_sync: bool = True

class QrzConfigUpdate(BaseModel):
    enabled: bool
    api_key: str
    settle_secs: float
    suppress_mins: float
    portable_locator: str = ""
    lookup_username: str = ""    # QRZ XML Data API login - a genuinely
    lookup_password: str = ""    # separate credential from api_key above
                                   # (that one's for the Logbook API only).
                                   # Both blank by default; the lookup
                                   # feature simply does nothing if either
                                   # is empty, rather than erroring.
    lookup_for_notifications: bool = False  # whether tri_watch's own
                                              # "someone else wants in"
                                              # notification does a name
                                              # lookup at all - independent
                                              # of whether QRZ logging
                                              # itself (enabled, above) is on
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

class QuickLynxConfigUpdate(BaseModel):
    """Off by default. It holds an outbound connection to BATC, and most
    Lynx installations are terrestrial repeaters that would never use
    it - so opt-in rather than opt-out."""
    enabled: bool = False


class DisplayConfigUpdate(BaseModel):
    ppm_style: str = "full_fat"  # "skeleton" or "full_fat"
    # "hdmi" (resolved to the first HDMI output at launch), "auto" to let
    # mpv choose, or an explicit mpv device name from /api/audio/devices.
    audio_device: str = "hdmi"

class TriWatchRfSourceUpdate(BaseModel):
    enabled: bool = True   # unchecked = this source is omitted from
                            # tri_watch entirely, same as not listing
                            # it in config.yaml at all
    rcv: int                # 1 or 2 - which receiver this source uses
    fplug: str = "a"        # which physical Picotuner plug feeds this receiver
    freq: int                # kHz, matching the same convention used
                              # throughout Lynx's own tuning UI
    sr: int                  # kS/s
    lnb_lo_khz: int = 0
    label: str = ""
    callsign: str = ""

class TriWatchStreamSourceUpdate(BaseModel):
    enabled: bool = True
    domain: str = ""
    app: str = ""
    streamname: str = ""
    port: int = 1935
    label: str = ""
    waiting_message: str = ""

class TriWatchSourcesUpdate(BaseModel):
    enabled: bool
    rx1: TriWatchRfSourceUpdate
    rx2: TriWatchRfSourceUpdate
    stream: TriWatchStreamSourceUpdate

class PathfinderConfigUpdate(BaseModel):
    """Only the three settings worth changing from the UI. The span and
    distance limits stay in config.yaml deliberately - they are tuned
    once against the shipped map data and then never touched, and
    exposing every knob makes the page harder to scan."""
    enabled: bool = True
    delay_secs: float = 2.0
    duration_secs: float = 30.0


class ConfigUpdateRequest(BaseModel):
    site: Optional[SiteConfigUpdate] = None
    picotuner: Optional[PicotunerConfigUpdate] = None
    diversity: Optional[DiversityConfigUpdate] = None
    notifications_qrz: Optional[QrzConfigUpdate] = None
    notifications_slack: Optional[SlackConfigUpdate] = None
    notifications_companion: Optional[CompanionConfigUpdate] = None
    notifications_gpio_tx: Optional[GpioTxConfigUpdate] = None
    quicklynx: Optional[QuickLynxConfigUpdate] = None
    display: Optional[DisplayConfigUpdate] = None
    tri_watch: Optional[TriWatchSourcesUpdate] = None
    pathfinder: Optional[PathfinderConfigUpdate] = None
    gnss: Optional[GnssConfigUpdate] = None
    squeak: Optional[SqueakConfigUpdate] = None

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
        # Deliberately NOT end_transition_cover() here - confirmed as
        # the real cause of a reported ~1s flash of raw desktop when a
        # tri_watch stream ends. The cover's own marker file is
        # checked by the overlay in near-real-time (every frame), but
        # state["mode"]/state["mpv_running_for_rf"] only update via
        # its own, much slower periodic /api/status poll. Removing the
        # marker here happened fast enough that the overlay could
        # still be working off stale "still streaming" state for a
        # second or two afterwards - marker gone + stale-but-still-
        # "locked" belief together briefly told it to show through the
        # now-transparent cover, except mpv had already been killed,
        # so there was nothing left behind it but the desktop. The
        # cover should simply stay up indefinitely after a genuine
        # stop - only a path that starts a NEW, successfully-rendering
        # source (which already calls end_transition_cover() itself,
        # only once rendering is actually confirmed) should ever take
        # it back down.
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
def _default_tri_watch_label(src_cfg):
    """A sensible fallback label for a tri_watch source that wasn't
    given an explicit one in config."""
    if src_cfg.get('type') == 'rf':
        return f"RF Rx{src_cfg.get('rcv', 1)} ({src_cfg.get('freq', '?')} kHz)"
    elif src_cfg.get('type') == 'stream':
        return src_cfg.get('streamname', 'Stream')
    return "Unknown source"

def get_tri_watch_status():
    """Builds the /api/status "tri_watch" section - a list of every
    configured, enabled source (up to 3, any mix of Rx1, Rx2, and a
    stream input), each with its own current active/locked status,
    determined however is appropriate for that source's type. Purely a
    status snapshot - no priority/arbitration logic here (Stage 2+).

    RF sources are distinguished by `rcv` (1 or 2 - which actual
    receiver/demodulator circuit), NOT `plug` (a/b - which physical
    antenna input that circuit reads from). Confirmed directly against
    _tune_impl()'s own, existing logic (2026-08-01): outside diversity
    mode, a manual tune ALWAYS sends rcv=1 regardless of which plug was
    selected - "plug B" in ordinary use still runs through the same
    rcv=1 circuit, not a second one. picotuner_state_b (rcv=2's status)
    is only ever genuinely populated during diversity mode, where rcv=1
    and rcv=2 are each tuned separately and explicitly. An earlier
    version of this function checked plug instead of rcv, which would
    have silently, permanently shown "no lock" for any RF source
    configured with plug: b outside diversity mode, regardless of
    actual signal quality - caught and fixed before ever being deployed
    to real hardware."""
    if not tri_watch_enabled:
        return {"enabled": False, "sources": []}

    sources_out = []
    # Deliberately the startup snapshot, NOT config.get('tri_watch',
    # {}).get('sources', []) - see tri_watch_sources_cfg's own
    # module-level comment. Confirmed as a real, reported bug when this
    # read the live config directly: saving a Tri-Watch Sources change
    # on the Config page reassigns the in-memory config immediately,
    # well before any restart - reading it here would desync this
    # status display from tri_watch_probes (built once at startup) the
    # instant a source was added/removed/reordered, breaking the whole
    # display rather than just leaving it correctly showing the old,
    # still-actually-running configuration until a genuine restart.
    for idx, src_cfg in enumerate(tri_watch_sources_cfg):
        if not src_cfg.get('enabled', False):
            continue
        src_type = src_cfg.get('type')
        entry = {
            "idx": idx,
            "type": src_type,
            "label": src_cfg.get('label') or _default_tri_watch_label(src_cfg),
        }
        if src_type == 'rf':
            rcv = src_cfg.get('rcv', 1)
            state = picotuner_state_b if rcv == 2 else picotuner_state
            entry["rcv"] = rcv
            entry["active"] = state["locked"]
        elif src_type == 'stream':
            probe = tri_watch_probes.get(idx)
            entry["active"] = probe.is_active if probe is not None else None
            entry["last_change"] = probe.last_change if probe is not None else None
        else:
            entry["active"] = None
        sources_out.append(entry)

    return {
        "enabled": True,
        "sources": sources_out,
        "displayed_source_idx": tri_watch_arbitrator.displayed_idx,
        "notification": tri_watch_arbitrator.get_notification(),
    }

def tri_watch_startup_tune():
    """Actively tunes every enabled RF source in tri_watch to its own
    configured frequency/symbol-rate at startup, rather than resuming
    whatever was previously tuned - the whole point for a dedicated
    repeater receiver is coming up on known, correct inputs every time,
    regardless of what happened before the restart.

    Sends raw tune commands directly rather than going through tune()/
    _tune_impl() - confirmed neither can actually do what's needed
    here: outside diversity mode, tune() only ever commands rcv=1
    regardless of which plug is requested; in diversity mode it forces
    rcv=1 and rcv=2 to the SAME frequency/symbol-rate, for combining.
    Neither can independently tune two receivers to two different
    values, which is exactly what two tri_watch RF sources need (e.g.
    the same-frequency/different-symbol-rate weak-signal technique).

    rcv=2 genuinely needs its own command port (cmd_port_b) - sending
    it to the shared port with a different rcv= value in the command
    text does NOT work, confirmed the hard way during diversity mode's
    own development. Same 0.3s settling delay between commands that
    diversity-mode tuning already proved necessary too: sent back-to-
    back with no gap, rcv=1 could intermittently fail to lock despite
    a strong signal.

    Deliberately does NOT touch mpv, the display, or current_mode at
    all - deciding what's actually worth showing is Stage 2's job
    (once priority/arbitration logic exists), not this one's."""
    cfg = config['picotuner']
    # tri_watch_sources_cfg (the startup snapshot - see its own
    # module-level comment) rather than reading config directly - by
    # the time this runs (after _resume_on_startup's own 7s delay) the
    # two are identical anyway, since this only ever executes once,
    # immediately at startup, before any Config-page edit could
    # possibly have happened - but reading the same source of truth as
    # everything else here avoids any doubt.
    for idx, src in enumerate(tri_watch_sources_cfg):
        if not src.get('enabled', False) or src.get('type') != 'rf':
            continue
        rcv = src.get('rcv')
        if rcv not in (1, 2):
            print(f"[tri_watch] startup tune: source {idx} has an invalid rcv - skipping")
            continue
        try:
            tuner_freq = calc_tuner_freq(src['freq'], src.get('lnb_lo_khz', 0))
            fplug = src.get('fplug', 'a' if rcv == 1 else 'b')
            cmd = f"[to@wh] rcv={rcv} fplug={fplug} offset=0 freq={tuner_freq} srate={src['sr']}"
            if rcv == 1:
                picotuner_cmd(cmd)
            else:
                sock_b = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    sock_b.sendto(cmd.encode(), (cfg['host'], cfg['cmd_port_b']))
                finally:
                    sock_b.close()
            print(f"[tri_watch] startup tune: Rx{rcv} -> {src['freq']} kHz / {src['sr']} kS/s")
        except Exception as e:
            print(f"[tri_watch] startup tune: source {idx} (Rx{rcv}) failed: {e}")
        time.sleep(0.3)  # same settling delay diversity-mode tuning already proved necessary

class PortDrainer:
    """Continuously reads and discards UDP packets from a port -
    used to keep a non-displayed RF source's TS stream from going
    completely unread while another source is what's actually being
    shown.

    Built to test a specific hypothesis (2026-08-01, not yet confirmed
    on real hardware): every other proven-working RF path that runs
    both receivers simultaneously (diversity mode) always has SOMETHING
    continuously reading from both TS ports - the combiner process.
    tri_watch never did: only whichever port mpv is actively watching
    ever gets read at all, and the other, non-displayed source's full
    TS stream goes into a port with nothing on the other end. If the
    Picotuner shares any internal buffering/resources between its two
    output paths, a permanently unread stream could plausibly
    destabilize the other one too - consistent with the reported
    pattern: both receivers failing together (not just the unread one),
    and even matching same-frequency/same-symbol-rate diversity-mode
    settings (ruling out an RF-domain explanation) still failing under
    tri_watch specifically, where this draining never happened.

    Tested directly with real UDP sockets: correctly receives packets
    sent to its port, and correctly releases the port on stop() so
    something else (mpv) can bind to it afterward."""
    def __init__(self, port):
        self.port = port
        self._sock = None
        self._thread = None
        self._stop_flag = threading.Event()

    def start(self):
        if self._thread is not None:
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                         name=f"tri_watch-drain-{self.port}")
        self._thread.start()

    def stop(self):
        self._stop_flag.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self._sock = None

    def _run(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.bind(('0.0.0.0', self.port))
            self._sock.settimeout(1.0)
            while not self._stop_flag.is_set():
                try:
                    self._sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
        except Exception as e:
            print(f"[tri_watch] port drainer for port {self.port} failed: {e}")

tri_watch_port_drainers = {}  # idx -> PortDrainer, for enabled RF sources not currently displayed

def _tri_watch_sync_drainers(currently_displayed_rf_idx):
    """Ensures every enabled RF source EXCEPT the one currently being
    displayed by mpv (if any) has an active port-drainer running -
    call this any time the displayed source changes (to RF, to
    stream, or to idle) so the set of drained ports always matches
    reality. Pass None if no RF source is currently displayed (stream
    playing, or idle) - every enabled RF source gets drained in that
    case."""
    # Deliberately the startup snapshot - see tri_watch_sources_cfg's
    # own module-level comment. This runs repeatedly during live
    # operation (any time the displayed source changes), so it has the
    # exact same index-mismatch risk as the arbitrator loop if it read
    # the live config instead - tri_watch_port_drainers is keyed by
    # the same startup indices as tri_watch_probes.
    sources = tri_watch_sources_cfg
    cfg = config['picotuner']
    for idx, src in enumerate(sources):
        if not src.get('enabled', False) or src.get('type') != 'rf':
            continue
        rcv = src.get('rcv', 1)
        port = cfg['ts_port_b'] if rcv == 2 else cfg['ts_port']
        if idx == currently_displayed_rf_idx:
            drainer = tri_watch_port_drainers.pop(idx, None)
            if drainer is not None:
                drainer.stop()
        else:
            if idx not in tri_watch_port_drainers:
                drainer = PortDrainer(port)
                drainer.start()
                tri_watch_port_drainers[idx] = drainer

class TriWatchArbitrator:
    """Decides which tri_watch source (if any) should currently be
    displayed, and tracks "someone else wants in" notifications for
    the OSD to show. Built and unit-tested as a standalone, pure-logic
    class before ever being wired to real mpv/Picotuner control (7
    scenarios confirmed directly: idle-to-active, notification-while-
    displayed, no duplicate re-notification, handover on the displayed
    source going inactive, notification expiry, everything-inactive-
    goes-idle, and re-notification after a source drops out and comes
    back) - kept exactly as tested, so display_callback/idle_callback
    are the only genuinely new, untested surface here.

    display_callback(idx, source_cfg) is called whenever the
    arbitrator decides a NEW source should become the one shown -
    responsible for actually starting mpv/RF playback. idle_callback()
    is called when nothing should be shown anymore. Both are only ever
    called on a genuine transition, never repeatedly for a source
    that's already displayed.

    settling_seconds: a source must be continuously active for at
    least this long before the arbitrator acts on it at all (for
    either display switching or a notification) - confirmed live as
    genuinely needed: brief, noise-induced or accidental short-burst
    "locks" were triggering notifications (and could equally have
    triggered an unwanted switch) for something that was never a real,
    sustained transmission. Applied uniformly to both the initial
    display decision and notifications, not just notifications
    specifically - a brief noise burst causing an unwanted switch when
    nothing was previously displayed seemed just as worth filtering out
    as an unwanted notification while something else plays.
    """
    def __init__(self, display_callback, idle_callback, notification_duration_secs=20,
                 settling_seconds=15, lock_confirm_seconds=3):
        self.display_callback = display_callback
        self.idle_callback = idle_callback
        self.notification_duration_secs = notification_duration_secs
        self.settling_seconds = settling_seconds
        self.lock_confirm_seconds = lock_confirm_seconds
        self.displayed_idx = None
        self.notification = None  # {"message": str, "triggered_at": float, "source_idx": int} or None
        self._notified_while_active = set()
        self._pending_since = {}  # idx -> monotonic time first seen active, not yet "settled" (for notifications)
        self._confirmed_active = {}  # idx -> bool, the debounced state actually used for switching decisions
        self._pending_confirm_change = {}  # idx -> (candidate_state, monotonic time first seen) - for the short switching debounce

    def _confirm_for_switching(self, active_map: dict) -> dict:
        """A SEPARATE, much SHORTER debounce than _settle() below -
        requires lock_confirm_seconds of a sustained state change
        (either direction: becoming active OR becoming inactive)
        before it's reported here, used only for the actual display-
        switching decision (both showing something new and noticing
        the current source went away). Matches the same, already-
        proven ~3s "confirm before acting" pattern normal RF mode
        already gets from rf_mpv_lifecycle_monitor() (its own
        LOCK_CONFIRM_POLLS/LOSS_CONFIRM_POLLS, deliberately bypassed
        entirely while tri_watch is enabled, since that monitor has no
        concept of tri_watch's own source selection at all - see its
        own docstring). Without this, tri_watch's arbitrator inherited
        none of that protection once the display decision was changed
        to react immediately to the raw signal - confirmed live as a
        real, reported symptom: brief flicker in the raw lock signal
        (invisible on the OSD, which applies its own, separate
        smoothing) was repeatedly tearing down and re-establishing the
        display, most visible as a picture briefly appearing then
        disappearing again, or on a source with more flicker, never
        getting a complete cycle to show anything at all."""
        now = time.monotonic()
        confirmed = {}
        for idx, raw_active in active_map.items():
            last_confirmed = self._confirmed_active.get(idx, False)
            if raw_active == last_confirmed:
                self._pending_confirm_change.pop(idx, None)
            else:
                pending = self._pending_confirm_change.get(idx)
                if pending is None or pending[0] != raw_active:
                    self._pending_confirm_change[idx] = (raw_active, now)
                elif (now - pending[1]) >= self.lock_confirm_seconds:
                    self._confirmed_active[idx] = raw_active
                    last_confirmed = raw_active
                    self._pending_confirm_change.pop(idx, None)
            confirmed[idx] = last_confirmed
        return confirmed

    def _settle(self, active_map: dict) -> dict:
        """Filters a raw active_map down to only sources that have been
        CONTINUOUSLY active for at least settling_seconds - a source
        that drops out and comes back starts its settling clock over
        from scratch, same as a genuinely fresh activation."""
        now = time.monotonic()
        settled = {}
        for idx, active in active_map.items():
            if not active:
                self._pending_since.pop(idx, None)
                settled[idx] = False
                continue
            if idx not in self._pending_since:
                self._pending_since[idx] = now
            settled[idx] = (now - self._pending_since[idx]) >= self.settling_seconds
        # Sources present in _pending_since but no longer in active_map
        # at all (e.g. a source got disabled) shouldn't linger forever.
        for idx in list(self._pending_since):
            if idx not in active_map:
                self._pending_since.pop(idx, None)
        return settled

    def step(self, active_map: dict, sources_cfg: list, build_message_fn):
        """active_map: {source_idx: bool} - current active/locked state
        for every enabled source, RAW (not debounced at all).
        sources_cfg: the tri_watch.sources config list.
        build_message_fn(idx, src_cfg) -> str, called only when a
        genuinely new notification is actually needed.

        Two SEPARATE debounces, for two genuinely different concerns:
        - _confirm_for_switching() (short, ~lock_confirm_seconds):
          used for the actual display/switching decision - matches the
          same, already-proven "confirm before acting" protection
          normal RF mode already gets from rf_mpv_lifecycle_monitor(),
          which tri_watch's arbitrator otherwise inherits none of at
          all, since that monitor is deliberately bypassed while
          tri_watch is enabled. Confirmed live as genuinely necessary:
          without it, brief flicker in the raw lock signal (invisible
          on the OSD, which applies its own, separate smoothing) was
          repeatedly tearing the display down and re-establishing it,
          sometimes never completing a full cycle at all.
        - _settle() (much longer, ~settling_seconds): used ONLY for
          deciding whether a notification appears for a new, waiting
          source - filtering brief, noise-induced "locks" out of the
          notification specifically, not the actual switch. Confirmed
          live as a real bug when this and the switching decision were
          accidentally the same debounce: a flickering raw signal could
          then also block the actual picture from ever appearing,
          despite the OSD showing a solid green light throughout."""
        switch_map = self._confirm_for_switching(active_map)
        settled_map = self._settle(active_map)

        for idx in list(self._notified_while_active):
            if not settled_map.get(idx, False):
                self._notified_while_active.discard(idx)

        # Noticing the displayed source going inactive uses the SHORT
        # switching debounce, not the raw signal directly - a brief
        # flicker shouldn't tear down an already-good display.
        if self.displayed_idx is not None and not switch_map.get(self.displayed_idx, False):
            self.displayed_idx = None
            self.notification = None

        if self.displayed_idx is None:
            # The initial display decision ALSO uses the SHORT
            # switching debounce - confirmed sustained for
            # lock_confirm_seconds, not just a momentary blip, but
            # still far short of the much longer notification-only
            # settling_seconds. Tries every currently-confirmed-active
            # source in order, moving on immediately if one fails,
            # rather than stopping at the first (failing) one -
            # confirmed as a real, live bug otherwise: if the earliest-
            # indexed source's display attempt failed, the arbitrator
            # would keep retrying only that same source forever on
            # every subsequent step(), never even attempting a
            # different, genuinely active source that might actually
            # work.
            last_exception = None
            for idx in range(len(sources_cfg)):
                if not switch_map.get(idx, False):
                    continue
                try:
                    # displayed_idx is only set AFTER display_callback
                    # genuinely succeeds (doesn't raise) - confirmed as
                    # a real, live bug otherwise: if the switch silently
                    # failed or got reverted by something else, the
                    # arbitrator would believe it had already succeeded
                    # and never retry, leaving the display stuck wrong
                    # indefinitely with no way to notice.
                    self.display_callback(idx, sources_cfg[idx])
                except Exception as e:
                    last_exception = e
                    print(f"[tri_watch] source {idx} failed to display, trying next active source if any: {e}")
                    continue
                self.displayed_idx = idx
                self._notified_while_active.discard(idx)
                self.notification = None
                return
            if last_exception is not None:
                raise last_exception  # every active source failed - still surface this rather than going silently idle
            self.idle_callback()
            return

        # Notifications about a NEW, waiting source DO use the settled
        # view - this is what the settling timer was actually meant
        # for, filtering out brief, noise-induced or accidental short-
        # burst "locks" from popping up a notification for something
        # that was never a real, sustained transmission.
        for idx in range(len(sources_cfg)):
            if idx == self.displayed_idx:
                continue
            if settled_map.get(idx, False) and idx not in self._notified_while_active:
                message = build_message_fn(idx, sources_cfg[idx])
                self.notification = {"message": message, "triggered_at": time.time(), "source_idx": idx}
                self._notified_while_active.add(idx)
                break  # one new notification per step - avoids piling several up if multiple go active in the same instant

    def get_notification(self):
        """Returns the current notification dict if still within its
        display window, else None."""
        if self.notification is None:
            return None
        if time.time() - self.notification["triggered_at"] > self.notification_duration_secs:
            return None
        return self.notification

def _build_tri_watch_message(idx, src_cfg):
    """Builds the "someone else wants in" notification text for a
    given source. RF uses a configurable template (tri_watch's own,
    genuinely separate from the existing Slack notification system's
    template - that one's built specifically around RF-lock events
    with fields a stream source has no equivalent for, and wiring the
    two together properly is Stage 4's job, not this one's) - default
    wording matches what was actually asked for. Stream sources use
    plain, user-typed text, since there's no live "who's transmitting"
    data available for a stream the way there is for RF.

    No format-based callsign validation here (an earlier version tried
    filtering on "contains at least one digit", after the Picotuner's
    own status parsing was observed picking up a stray word as though
    it were a genuine callsign) - removed after Justin's own, direct
    correction: callsign formats are genuinely diverse across countries
    (his own EI3IOB/EI3IO span both 2- and 3-letter suffixes, and
    GB3RS-style calls exist too), and a heuristic risks rejecting a
    real, valid callsign in some format this doesn't account for. The
    settling timer (see TriWatchArbitrator) already solves the actual
    underlying problem more robustly - a transient parsing artifact
    can't survive settling_seconds of sustained activity, so there's no
    need to also guess at which specific values are "real"."""
    if src_cfg.get('type') == 'rf':
        rcv = src_cfg.get('rcv', 1)
        state = picotuner_state_b if rcv == 2 else picotuner_state
        live_callsign = state.get('callsign') or 'A station'

        # QRZ name lookup, if configured, happens entirely in the
        # BACKGROUND - never synchronously here. Confirmed as a real,
        # serious risk otherwise: this function runs inside the
        # arbitrator's own step() loop, which must stay responsive for
        # tri_watch to work at all - a slow/unreachable QRZ could
        # block it for the better part of a minute (multiple chained
        # network timeouts: login, lookup, retry-after-expiry),
        # delaying every other tri_watch decision for that entire
        # window. The notification fires immediately with just the
        # callsign; _kick_off_qrz_notification_lookup() below handles
        # updating it in place afterwards, only if the same
        # notification (matched by source_idx) is still showing by
        # the time the lookup completes.
        qrz_cfg = config.get('notifications', {}).get('qrz', {})
        if qrz_cfg.get('lookup_for_notifications') and live_callsign != 'A station':
            _kick_off_qrz_notification_lookup(idx, live_callsign, qrz_cfg, src_cfg)

        return _format_rf_notification(live_callsign, "", src_cfg)
    elif src_cfg.get('type') == 'stream':
        site_callsign = config.get('site', {}).get('callsign') or 'this input'
        return src_cfg.get('waiting_message') or f"Someone is waiting to access {site_callsign} via the web stream"
    return "Another source wants attention"


def _format_rf_notification(live_callsign, name, src_cfg):
    """Builds the actual RF notification text, given whatever name is
    currently known (empty string if none). Shared by both
    _build_tri_watch_message()'s own immediate, name-less call and
    _kick_off_qrz_notification_lookup()'s later, name-included one, so
    the two can never drift apart in formatting."""
    name_prefix = f"{name} " if name else ""
    template = config.get('tri_watch', {}).get(
        'rf_notification_template',
        "{name_prefix}{callsign} is waiting to access {configured_callsign} on {frequency}")
    try:
        result = template.format(
            callsign=live_callsign,
            name=name,
            name_prefix=name_prefix,
            configured_callsign=src_cfg.get('callsign') or src_cfg.get('label') or 'this input',
            frequency=f"{src_cfg.get('freq', 0) / 1000:.3f} MHz",
        )
    except (KeyError, ValueError) as e:
        print(f"[tri_watch] rf_notification_template has an unusable placeholder ({e}) - using a plain fallback")
        return f"{name_prefix}{live_callsign} is waiting on {src_cfg.get('label') or 'another input'}"

    # A known name that the (possibly custom, possibly predating this
    # feature) template doesn't actually reference anywhere would
    # otherwise be silently dropped - confirmed as a real, reported
    # gap: Python's str.format() ignores any keyword it's given that
    # the template string doesn't use, so a template written before
    # this feature existed computed the name correctly and then simply
    # never showed it. Prepend it directly in that case, rather than
    # requiring every existing, already-configured template to be
    # manually updated just for the feature to do anything at all.
    if name and "{name_prefix}" not in template and "{name}" not in template:
        result = f"{name} {result}"

    return result


def _kick_off_qrz_notification_lookup(idx, live_callsign, qrz_cfg, src_cfg):
    """Runs the QRZ name lookup in a background thread - see
    _build_tri_watch_message()'s own comment for why this must never
    run synchronously on the arbitrator's own step() loop. If it
    completes while the SAME notification (matched by source_idx) is
    still the one showing, rebuilds and replaces its message to
    include the name. If a different/newer notification has since
    replaced it, or it's already expired, the result is simply
    discarded - never overwrites something unrelated."""
    def _worker():
        looked_up = lynx_notifications.qrz_callsign_lookup(
            qrz_cfg.get('lookup_username', ''),
            qrz_cfg.get('lookup_password', ''),
            live_callsign)
        if not looked_up:
            return

        # Wait briefly for the arbitrator to actually set
        # self.notification. Confirmed as a real, reproduced race
        # otherwise: this thread is started from INSIDE
        # _build_tri_watch_message(), before that function has even
        # returned its own message string back to the arbitrator's
        # step() - which only sets self.notification on the very next
        # line after that return. Normally a gap of microseconds, but
        # a cached lookup (near-instant, e.g. the same callsign
        # checked moments earlier via the /diagnostics test tool) can
        # genuinely complete and check here before that single
        # assignment has happened at all, silently discarding a
        # perfectly good result. This bounded wait costs nothing in
        # the normal case and only ever matters for this brief window.
        deadline = time.time() + 2.0
        current = tri_watch_arbitrator.notification
        while (current is None or current.get("source_idx") != idx) and time.time() < deadline:
            time.sleep(0.05)
            current = tri_watch_arbitrator.notification
        if current is None or current.get("source_idx") != idx:
            return  # never appeared in time, or a different one is showing now

        updated_message = _format_rf_notification(live_callsign, looked_up, src_cfg)
        # Re-check immediately before writing - a best-effort UI
        # enhancement, not something safety-critical, so this narrows
        # the race against the notification expiring/changing between
        # the check above and now without needing a full lock.
        current = tri_watch_arbitrator.notification
        if current is not None and current.get("source_idx") == idx:
            current["message"] = updated_message

    threading.Thread(target=_worker, daemon=True).start()


def _tri_watch_display_source(idx, src_cfg):
    """The arbitrator's display_callback - actually switches to show
    the given source. Reuses the exact same, proven tune()/
    start_stream() paths a manual memory/preset selection would use,
    rather than a separate, custom reimplementation of the display
    logic - per Justin's own suggestion, after tri_watch's earlier,
    hand-built RF-switching code missed a subtle but critical detail
    (re-sending a fresh tune command every time, exactly like manual
    selection always does) that a parallel reimplementation was always
    at genuine risk of drifting from, and did.

    For RF sources: sets tri_watch_target_rcv so the now tri_watch-
    aware rf_mpv_lifecycle_monitor() knows which receiver to actually
    watch and start mpv for, then calls tune() itself. This is
    genuinely fire-and-forget for the mpv-starting part - exactly like
    a normal, manual RF tune already is - since tune() itself never
    starts mpv directly; the monitor handles confirming a stable lock
    and getting mpv running from there, with its own, already-proven
    retry logic, rather than this function needing its own, separate
    copy of that same logic.

    RF and stream sources need genuinely different locking: tune() and
    start_stream() both acquire tune_lock internally, so this function
    must never also acquire it itself, or it would deadlock against
    itself."""
    global tri_watch_target_rcv, mpv_running_for_rf
    src_type = src_cfg.get('type')
    if src_type == 'rf':
        rcv = src_cfg.get('rcv', 1)
        # Stop draining THIS source's own port before mpv (once
        # rf_mpv_lifecycle_monitor confirms lock) tries to bind to it -
        # a drainer and mpv can't both be bound to the same UDP port at
        # once - while every other enabled RF source keeps being
        # drained. See PortDrainer's own docstring for why this might
        # matter.
        _tri_watch_sync_drainers(idx)
        tri_watch_target_rcv = rcv
        # Confirmed as a real bug: mpv_running_for_rf is a single,
        # shared flag that doesn't distinguish which receiver mpv is
        # actually running for. Switching targets here (e.g. Rx1 to
        # Rx2) left it True from the previous receiver's session, so
        # rf_mpv_lifecycle_monitor()'s own `not mpv_running_for_rf`
        # check believed mpv was "already running" and never attempted
        # to start it for the new target - it only got reset
        # asynchronously by tune()'s own _kick_mpv() thread, after a
        # delay, which wasn't reliably resolving this. Reset it
        # immediately, synchronously, right here instead, so the very
        # next poll correctly sees nothing running yet for this target.
        mpv_running_for_rf = False
        tune(TuneRequest(
            freq=src_cfg['freq'],
            sr=src_cfg['sr'],
            plug=src_cfg.get('fplug', 'a' if rcv == 1 else 'b'),
            lnb_lo_khz=src_cfg.get('lnb_lo_khz', 0),
            rcv=rcv,
        ))
        print(f"[tri_watch] now displaying source {idx}: RF Rx{rcv}")
    elif src_type == 'stream':
        # No RF source is being displayed while a stream plays - every
        # enabled RF source should be drained, and tri_watch_target_rcv
        # cleared so rf_mpv_lifecycle_monitor() knows there's nothing
        # for it to do.
        _tri_watch_sync_drainers(None)
        tri_watch_target_rcv = None
        url = f"rtmp://{src_cfg['domain']}/{src_cfg['app']}/{src_cfg['streamname']}"
        start_stream(StreamRequest(url=url, name=src_cfg.get('label', '')))
        print(f"[tri_watch] now displaying source {idx}: stream")

def _tri_watch_go_idle():
    global tri_watch_target_rcv
    if current_mode != "idle":
        stop_current()
        print("[tri_watch] no sources active - going idle")
    # No RF source is displayed while idle either - drain everything
    # and clear the target so rf_mpv_lifecycle_monitor() has nothing
    # to watch.
    _tri_watch_sync_drainers(None)
    tri_watch_target_rcv = None

tri_watch_arbitrator = TriWatchArbitrator(
    _tri_watch_display_source, _tri_watch_go_idle,
    notification_duration_secs=config.get('tri_watch', {}).get('notification_duration_secs', 20),
    settling_seconds=config.get('tri_watch', {}).get('settling_seconds', 15),
    lock_confirm_seconds=config.get('tri_watch', {}).get('lock_confirm_seconds', 3))

# ---------------------------------------------------------------------
#  End-of-contact station map
# ---------------------------------------------------------------------
#  Shows a full-screen card - where the station was, the path back here,
#  and the signal figures from the contact - for a configurable window
#  after the station stops transmitting. It replaces the idle logo
#  screen rather than compositing over video, so it can never obscure a
#  live picture.
#
#  No timers anywhere, deliberately. This follows the same pattern as
#  TriWatchArbitrator.get_notification(): store a timestamp, work out on
#  read whether the card is due, showing, or finished. A stale timer
#  firing a card over a station that has since come back simply cannot
#  happen, because nothing is ever scheduled.
#
#  WHY A WATCHER LOOP AND NOT A HOOK IN picotuner_monitor()
#  --------------------------------------------------------
#  The first version of this hooked the RX1 lock transition directly
#  inside picotuner_monitor(), which is wrong in both of the modes that
#  matter:
#
#    * Diversity - A and B receive the SAME station, and either one
#      alone can carry the picture. Arming on A dropping would put a
#      card up while B was still locked and the picture still playing.
#      "Unlocked" here has to mean BOTH tuners unlocked, exactly as the
#      OSD's own lock indicator already treats it.
#
#    * Tri-Watch - RX1 dropping is irrelevant if the arbitrator is
#      currently displaying RX2 or a stream, and a station stopping on
#      RX2 wouldn't have armed anything at all, because that monitor
#      only parses RX1.
#
#  So the question is not "did a tuner unlock" but "did the thing we
#  were actually SHOWING stop", which is a composite of mode, diversity
#  state and the arbitrator's current choice. That is computed in one
#  place below and evaluated on a slow poll - the same shape as the
#  arbitrator's own loop.

_pathfinder_cfg = config.get('pathfinder', {}) or {}
pathfinder_tracker = lynx_map.PathfinderTracker(
    delay_secs=_pathfinder_cfg.get('delay_secs', 2),
    duration_secs=_pathfinder_cfg.get('duration_secs', 30),
    max_distance_km=_pathfinder_cfg.get('max_distance_km', 1200),
    enabled=_pathfinder_cfg.get('enabled', True))

_pathfinder_prev = {"receiving": False, "callsign": "", "rcv": 1, "telemetry": {}}

# ---------------------------------------------------------------------
#  Auto-Squeak - Lindos sequence measurement
# ---------------------------------------------------------------------
# Listens continuously for a Lindos test sequence and measures the
# audio path. On by default: it costs one more reader on an audio
# monitor that is already being read for the PPM, does nothing at all
# until a sequence actually arrives, and a receiver that quietly
# measures itself when someone sends a test is more useful than one
# that has to be configured first.
_squeak_cfg = config.get('squeak', {}) or {}
squeak_tracker = (lynx_squeak.SqueakTracker(
    hold_secs=_squeak_cfg.get('hold_secs', 45),
    enabled=_squeak_cfg.get('enabled', True))
    if SQUEAK_AVAILABLE else None)
squeak_listener = None

# Longest Pathfinder will wait for an Auto-Squeak card to clear.
SQUEAK_DEFER_MAX_S = 120.0


def _squeak_monitor_target():
    """Which monitor Auto-Squeak should listen on.

    Follows mpv's output device, exactly as the PPM meter does, and for
    the same reason it had to: the meter originally watched the default
    sink, so with mpv sent to HDMI while a USB dongle was the system
    default it sat reading silence with no indication why. Auto-Squeak
    would fail the same way and look like a broken detector rather than
    a misdirected one.

    On PipeWire this is exact rather than inferred: mpv's device name
    is "pipewire/<node>", so the monitor is "<node>.monitor". Falling
    back to the default sink's monitor otherwise, which is right for a
    single-output receiver and no worse than guessing on any other.
    """
    fallback = '@DEFAULT_SINK@.monitor'
    try:
        dev = str(config.get('display', {}).get('audio_device', 'hdmi')).strip()
        if not dev or dev.lower() == 'auto':
            return fallback
        resolved = _cached_audio_device_resolved() or dev
        if resolved.startswith('pipewire/'):
            return resolved.split('/', 1)[1] + '.monitor'
        m = re.search(r'CARD=([A-Za-z0-9_\-]+)', resolved)
        if m:
            for mon in list_audio_monitors():
                if m.group(1).lower() in mon['name'].lower():
                    return mon['name']
        return fallback
    except Exception as e:
        print(f"[squeak] could not resolve the audio monitor ({e}) - "
              f"using the default sink")
        return fallback


def list_audio_monitors():
    """Monitor sources available for Auto-Squeak to listen on.

    Uses pw-cli rather than pactl: pactl belongs to the PulseAudio
    utilities package, which is not installed on a stock Raspberry Pi
    OS running PipeWire, whereas pw-cli comes with PipeWire itself.
    Confirmed the hard way on a receiver where pactl simply did not
    exist."""
    out = [{'name': '', 'description': 'Automatic (default output)'}]
    try:
        r = subprocess.run(['pw-cli', 'list-objects', 'Node'],
                           capture_output=True, text=True, timeout=5)
        name = None
        for line in r.stdout.splitlines():
            line = line.strip()
            if line.startswith('node.name'):
                name = line.split('=', 1)[1].strip().strip('"')
            elif line.startswith('media.class') and name:
                cls = line.split('=', 1)[1].strip().strip('"')
                if cls == 'Audio/Sink':
                    out.append({'name': name + '.monitor',
                                'description': name + ' (monitor)'})
                name = None
    except Exception as e:
        print(f"[squeak] could not list audio monitors: {e}")
    return out


def _squeak_status():
    if squeak_tracker is None:
        return None
    """The card, trimmed for JSON. The response curves are numpy arrays
    of a couple of hundred points each and are not JSON-serialisable,
    so they are converted to plain lists here rather than anywhere that
    would have to know about numpy."""
    c = squeak_tracker.get_card()
    if not c:
        return None
    out = {}
    for k, v in c.items():
        if k in ('resp_l', 'resp_r'):
            out[k] = [list(map(float, v[0])), list(map(float, v[1]))]
        elif k == 'segments':
            continue
        elif isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
    return out


def _squeak_result(res):
    """Called by the listener when a pass completes."""
    res['measured_at'] = time.time()
    res['seq_name'] = _squeak_cfg.get('label', 'off-air')
    if squeak_tracker is not None:
        squeak_tracker.on_result(res)


def _start_squeak():
    """Started unconditionally like the other background threads, but
    returns immediately unless enabled - the audio source is only
    opened when the feature is actually wanted."""
    global squeak_listener
    if not SQUEAK_AVAILABLE or not _squeak_cfg.get('enabled', True):
        return
    # Blank means "follow whatever mpv is playing to" - see
    # _squeak_monitor_target. An explicit name overrides it.
    src = (_squeak_cfg.get('source') or '').strip() or _squeak_monitor_target()
    print(f"[squeak] listening on {src}")
    squeak_listener = lynx_squeak.SqueakListener(src, on_result=_squeak_result)
    squeak_tracker.listener = squeak_listener
    squeak_listener.start()




def _pathfinder_current_source():
    """Which tuner, if any, is currently supplying the displayed picture.

    Returns (receiving, rcv) where rcv is 1 or 2. receiving is False
    whenever nothing RF is on screen - including when Tri-Watch is
    showing a stream, which has no callsign or locator and therefore no
    card to draw.
    """
    tw = config.get('tri_watch', {}) or {}
    if tw.get('enabled') and tri_watch_arbitrator is not None:
        idx = tri_watch_arbitrator.displayed_idx
        if idx is None:
            return (False, 1)
        try:
            src = tri_watch_sources_cfg[idx]
        except (IndexError, TypeError):
            return (False, 1)
        if src.get('type') != 'rf':
            return (False, 1)          # a stream is showing - no card
        rcv = src.get('rcv', 1)
        st = picotuner_state_b if rcv == 2 else picotuner_state
        return (bool(st.get('locked')), rcv)

    # The RUNTIME flag, not config['diversity']['enabled']. Turning
    # diversity on by tuning sets the global and does not write the
    # config, so reading the file reports False - this branch was
    # skipped entirely and the fall-through below looked only at tuner
    # A. Pathfinder therefore drew nothing for a contact carried by Rx2
    # alone, while mpv and the OSD showed it perfectly. The same
    # mistake, in the same session, as the one that stopped QRZ logging
    # an Rx2-only diversity contact.
    if diversity_enabled:
        # Either tuner alone can carry the picture, so the contact is
        # only over once both have dropped.
        a = bool(picotuner_state.get('locked'))
        b = bool(picotuner_state_b.get('locked'))
        if a:
            return (True, 1)
        if b:
            return (True, 2)
        return (False, 1)

    return (bool(picotuner_state.get('locked')), 1)


def pathfinder_watcher():
    """Background thread - watches for the displayed station stopping,
    and arms the end-of-contact card when it does.

    Polls every second: fast enough that the configured delay is
    accurate to within a second, slow enough to be free. Deliberately
    tolerant of anything going wrong, since nothing here is worth taking
    the receiver down for.
    """
    while True:
        time.sleep(1)
        try:
            if not pathfinder_tracker.enabled:
                continue
            receiving, rcv = _pathfinder_current_source()
            prev = _pathfinder_prev

            if receiving:
                st = picotuner_state_b if rcv == 2 else picotuner_state
                cs = st.get('callsign', '')
                if cs:
                    prev['callsign'] = cs
                    prev['rcv'] = rcv
                if not prev['receiving']:
                    # Something is on air again - cancel any card that
                    # is pending or currently showing, and drop the
                    # previous contact's telemetry. Done BEFORE caching
                    # below, so this poll's own readings survive.
                    pathfinder_tracker.station_locked()
                    prev['telemetry'] = {}
                prev['receiving'] = True
                # Cache quality telemetry WHILE the signal is live.
                # picotuner_quality_monitor() rebuilds these fields from
                # each broadcast with fields.get('$12', '') /
                # fields.get('$18', '') - so the moment the signal drops
                # and the Picotuner stops sending those fields, mer and
                # modcod are overwritten with empty strings, typically
                # within ~500ms. Reading them at unlock is therefore a
                # race that is essentially always lost: confirmed live,
                # with a diagnostic print showing mer='10.8' one line
                # before the snapshot that captured ''. symbol_rate and
                # frequency survive that transition (they're not blanked
                # the same way), which is exactly why those two kept
                # appearing on the card while MER/MODCOD did not.
                # Only overwrite with non-empty values, so a single
                # broadcast missing a field can't wipe a good reading.
                for k in ('modcod', 'symbol_rate', 'frequency'):
                    v = st.get(k, '')
                    if v:
                        prev['telemetry'][k] = v
                # MER is kept at its BEST value for the contact, not its
                # most recent. Caching the latest reading on every poll
                # sounds right but isn't: the final poll before unlock
                # catches the signal already collapsing as the operator
                # unkeys, so the card reported a far worse figure than
                # the contact actually achieved (25.0 dB live on the
                # tuner, 3.7 dB on the card, dropping further on repeat
                # transmissions). The best reading is the honest
                # summary of how the path actually performed, and it's
                # what an operator means by "what was my MER".
                # Margin is captured alongside whichever poll produced
                # that best MER, so the two always come from the same
                # instant rather than being mixed from different ones.
                mer_now = st.get('mer', '')
                if mer_now:
                    try:
                        if float(mer_now) > float(prev['telemetry'].get('mer') or '-inf'):
                            prev['telemetry']['mer'] = mer_now
                            margin_now = st.get('margin', '')
                            if margin_now:
                                prev['telemetry']['margin'] = margin_now
                    except ValueError:
                        pass
                continue

            if prev['receiving']:
                prev['receiving'] = False
                if prev['callsign']:
                    _pathfinder_arm(prev['callsign'], prev['rcv'],
                                     dict(prev['telemetry']))
        except Exception as e:
            print(f"[map] watcher: {e}")


def _pathfinder_lnb_lo_khz(rcv):
    """The LNB local oscillator currently in front of a receiver, in kHz.

    Needed because the frequency the Picotuner reports is the IF, not
    the downlink - the tuner has no idea an LNB sits in front of it.
    With a 9750 LNB a QO-100 contact reports as roughly 743 MHz, and
    with a 9000 LNB as roughly 1493 MHz; neither is the frequency being
    received, and testing either against the 3cm bandplan would simply
    return False for a contact that plainly did come via the satellite.
    Adding the LO back reconstructs the real downlink, which is the
    only number worth testing.

    Mirrors _picotuner_expected_tuning()'s own source-of-truth rules
    deliberately: tri_watch's per-source config when it is driving,
    otherwise the saved tuning state."""
    try:
        if _tri_watch_present():
            for src in globals().get('tri_watch_sources_cfg', []):
                if src.get('enabled') and src.get('type') == 'rf' \
                        and src.get('rcv') == rcv:
                    return int(src.get('lnb_lo_khz', 0) or 0)
            return 0
        state = load_last_state() or {}
        return int(state.get('lnb_lo_khz', 0) or 0)
    except Exception as e:
        print(f"[map] could not determine LNB LO: {e}")
        return 0


def _pathfinder_via_qo100(rcv, telemetry):
    """Did this contact come via QO-100?

    Config switch first: pathfinder.qo100_test forces the globe view on
    for any contact, so the card can be developed and demonstrated at a
    site with no 3cm satellite installation at all. Deliberately not
    exposed on the Config page - it makes every card wrong, and is only
    ever wanted by someone editing the file by hand.

    Otherwise the downlink is reconstructed from the reported IF plus
    the LNB LO and tested against the bandplan's satellite-only
    segment. See lynx_map.is_qo100()."""
    try:
        if config.get('pathfinder', {}).get('qo100_test', False):
            print("[map] pathfinder.qo100_test is set - forcing QO-100 globe view")
            return True
        raw = str(telemetry.get('frequency', '') or '').strip()
        if not raw:
            return False
        if_khz = float(raw) * 1000.0     # Picotuner reports MHz
        return lynx_map.is_qo100(if_khz + _pathfinder_lnb_lo_khz(rcv))
    except Exception as e:
        print(f"[map] QO-100 check failed, assuming terrestrial: {e}")
        return False


def _pathfinder_arm(callsign, rcv, telemetry):
    """Looks the station up and arms the card if it has a usable
    locator. The QRZ lookup runs on its own thread - never inline, for
    the same reason tri_watch's name lookup doesn't: a slow or
    unreachable QRZ must not stall anything.

    telemetry (mer/modcod/symbol_rate/frequency) is passed in by
    pathfinder_watcher(), which caches it continuously while the
    signal is still LIVE. It is deliberately NOT read from
    picotuner_state here, and this is the whole fix for a
    long-standing bug where the card showed no MER or MODCOD:
    picotuner_quality_monitor() rebuilds those fields from every
    broadcast via fields.get('$12', '') / fields.get('$18', ''), so
    the instant a signal drops and the Picotuner stops sending them,
    both are overwritten with empty strings - typically within about
    500ms, and always before this function gets to run. Confirmed
    live: a diagnostic print here read mer='10.8' and modcod='QPSK
    8/9', and the snapshot taken on the very next line captured ''
    for both. symbol_rate and frequency aren't blanked on that
    transition, which is exactly why they kept appearing on the card
    while MER and MODCOD didn't - the one clue that separated a lost
    race from a rendering problem.

    Two earlier attempts at this misdiagnosed it as a startup race and
    added a bounded wait for MER to *arrive*; both were wrong, because
    the data was already there and about to be destroyed rather than
    still on its way. Waiting made it strictly worse.
    """
    snapshot = {
        'mer': telemetry.get('mer', ''),
        'modcod': telemetry.get('modcod', ''),
        'symbol_rate': telemetry.get('symbol_rate', ''),
        'frequency': telemetry.get('frequency', ''),
    }

    def _lookup():
        try:
            qrz_cfg = config.get('notifications', {}).get('qrz', {})
            name, grid = lynx_notifications.qrz_callsign_details(
                qrz_cfg.get('lookup_username', ''),
                qrz_cfg.get('lookup_password', ''),
                callsign)
            if not grid:
                print(f"[map] {callsign}: no locator on QRZ - no card")
                return
            home = config.get('site', {}).get('locator', '')
            pos_h = lynx_map.locator_to_latlon(home)
            pos_s = lynx_map.locator_to_latlon(grid)
            via_qo100 = _pathfinder_via_qo100(rcv, telemetry)
            # max_distance_km exists to catch a stale or default QRZ
            # locator on a terrestrial contact, where a few thousand km
            # really does mean the data is wrong rather than the contact
            # extraordinary. Via QO-100 that reasoning inverts: the
            # satellite's footprint is most of a hemisphere, so a South
            # African or Brazilian station is entirely ordinary and
            # rejecting it is the bug. The sanity check the globe view
            # relies on instead is the bandplan test that got us here -
            # a contact in the satellite-only part of 3cm genuinely did
            # come via the bird, whatever the distance.
            if pos_h and pos_s and not via_qo100:
                d = lynx_map.haversine_km(*pos_h, *pos_s)
                if d > pathfinder_tracker.max_distance_km:
                    # Almost always a stale or default QRZ locator rather
                    # than a genuinely extraordinary contact. Better to
                    # show nothing than to publish a confidently wrong
                    # map on a live stream.
                    print(f"[map] {callsign}: {d:.0f}km exceeds max_distance_km "
                          f"- treating locator {grid} as unreliable, no card")
                    return
            # Queue behind Auto-Squeak rather than fighting it. A test
            # transmission ends like any other, so Pathfinder arms as
            # normal - but the squeak measurement is still running and
            # its card is the one wanted first. Waiting here rather
            # than suppressing means Pathfinder still gets its full
            # display window afterwards instead of being lost.
            #
            # This runs on the QRZ lookup thread, so blocking is free.
            # Bounded, because a card several minutes after the contact
            # would be worse than none: if Auto-Squeak somehow never
            # finishes, Pathfinder goes ahead anyway.
            waited = 0.0
            while squeak_tracker is not None and squeak_tracker.busy() \
                    and waited < SQUEAK_DEFER_MAX_S:
                if waited == 0.0:
                    print(f"[map] {callsign}: holding card while Auto-Squeak finishes")
                time.sleep(0.5)
                waited += 0.5
            if waited:
                print(f"[map] {callsign}: resuming after {waited:.0f}s")

            pathfinder_tracker.station_unlocked(
                callsign, grid, name=name, via_qo100=via_qo100,
                **snapshot)
        except Exception as e:
            print(f"[map] lookup for {callsign} failed: {e}")

    threading.Thread(target=_lookup, daemon=True).start()


# ---------------------------------------------------------------------
#  Picotuner tuning watchdog
# ---------------------------------------------------------------------
#  The Picotuner is powered separately from the Pi, so it can restart on
#  its own - a PoE renegotiation, a supply blip, a knocked plug, static.
#  Its WinterHill firmware keeps NEITHER tuning nor LNB supply across a
#  power cycle: it comes back with nothing tuned at all.
#
#  Lynx would previously never notice. Broadcasts resume, the Web UI
#  shows a healthy tuner, and the receiver sits deaf indefinitely -
#  because the only things that ever tune are Lynx's own startup and an
#  explicit API call. At an unattended repeater that is silent, total,
#  and indistinguishable from a quiet band.
#
#  WHY THIS WATCHES STATE, NOT TIMING
#  ----------------------------------
#  The obvious approach is to spot the broadcasts stopping and restarting.
#  It is also the wrong one. A Pico reboots in a couple of seconds and
#  most of the outage is the W5100S bringing its Ethernet link back, so
#  the gap to catch is short and unpredictable - too short and a reboot
#  is missed entirely; too eager and network jitter triggers spurious
#  re-tunes. Worse, it only catches that one cause. Anything else that
#  leaves the tuner untuned - a command lost in flight, static, a
#  firmware hiccup - looks completely normal to a timing check.
#
#  So this compares what the Picotuner REPORTS it is tuned to against
#  what Lynx believes it should be, and fixes any disagreement. That is
#  safe to do continuously because a tuned receiver keeps reporting its
#  frequency even with no signal on it at all - confirmed directly:
#  "437.000B lost" and "1249.000T search" are both a correctly tuned
#  receiver saying it can't hear anything. Only a receiver that has
#  genuinely lost its tuning reports nothing. A quiet band is therefore
#  left completely alone, which is the trap a naive level-triggered
#  check would fall into.

PICOTUNER_CHECK_SECS = 2.0          # how often to compare
PICOTUNER_MISMATCH_SECS = 20.0      # sustained disagreement before acting
PICOTUNER_RETUNE_SETTLE_SECS = 8.0  # let its firmware finish booting first
PICOTUNER_RETUNE_COOLDOWN_SECS = 90.0   # don't hammer a tuner that won't take


def _tri_watch_present():
    """True only on builds that actually have tri_watch.

    The main branch doesn't yet, so this code must not assume the name
    exists - a bare reference would raise NameError inside the watchdog
    thread, and since that loop catches exceptions it would log the same
    error every couple of seconds and silently protect nothing at all.

    Deliberately a runtime lookup rather than two different versions of
    this file: keeping main and beta identical here means no divergence
    to merge, and no conflict to resolve wrongly the day tri_watch does
    land on main.
    """
    return bool(globals().get('tri_watch_enabled'))


def _stream_is_being_shown():
    """True when a web stream is what's currently on screen.

    Supplied to the notifications manager so Companion's source-switching
    webhook and the GPIO Tx pin can follow "is there a picture to
    transmit" rather than "is RF locked" - which are different questions
    in three of the four modes. A repeater relaying a stream needs its
    transmitter keyed and its vision mixer switched exactly as it would
    for an RF signal.

    Under tri_watch, follows whichever source the arbitrator is actually
    displaying, so a background stream that isn't on screen doesn't key
    anything. Outside it, plain stream mode is the whole answer.
    """
    try:
        tw = config.get('tri_watch', {}) or {}
        if tw.get('enabled') and tri_watch_arbitrator is not None:
            idx = tri_watch_arbitrator.displayed_idx
            if idx is None:
                return False
            try:
                src = tri_watch_sources_cfg[idx]
            except (IndexError, TypeError):
                return False
            return src.get('type') == 'stream'
        return current_mode == 'stream'
    except Exception as e:
        print(f"[notifications] stream-active check failed: {e}")
        return False


def _picotuner_expected_tuning():
    """What each receiver SHOULD be tuned to.

    Returns {rcv: (freq_khz, symbol_rate)}. Empty when Lynx has no
    opinion - idle, a web stream, or nothing ever tuned - in which case
    there is nothing to check and the watchdog stays quiet.
    """
    out = {}
    try:
        if _tri_watch_present():
            for src in globals().get('tri_watch_sources_cfg', []):
                if not src.get('enabled') or src.get('type') != 'rf':
                    continue
                rcv = src.get('rcv')
                if rcv in (1, 2):
                    out[rcv] = (calc_tuner_freq(src['freq'],
                                                src.get('lnb_lo_khz', 0)),
                                src.get('sr'))
            return out

        if current_mode != "rf":
            return out

        state = load_last_state()
        if not state or state.get("mode") != "rf":
            return out
        f = calc_tuner_freq(state["freq"], state.get("lnb_lo_khz", 0))
        sr = state.get("sr")
        out[1] = (f, sr)
        # Diversity drives both receivers to the same frequency
        if str(state.get("plug", "")).lower() == "diversity" or diversity_enabled:
            out[2] = (f, sr)
    except Exception as e:
        print(f"[picotuner] could not work out expected tuning: {e}")
    return out


def _picotuner_expected_lnb():
    """The LNB supply each plug SHOULD have, from the CONFIG.

    Deliberately not from current_lnb_psu_a/b: those track what the
    Picotuner is REPORTING, since the broadcast carries "LNB supply X/Y"
    and Lynx updates them from it so the UI buttons show the truth.

    That reporting is exactly why the first version of this restore was
    useless. When the Picotuner power-cycles it comes back with the
    supply off and says so, Lynx dutifully updates its globals to "off",
    and a restore built on those globals then faithfully re-applies...
    off. The configured value is the only thing that still knows what
    the supply is meant to be.
    """
    cfg = config.get('lnb_psu', {}) or {}
    return {
        'a': (str(cfg.get('plug_a', 'off')).lower(), bool(cfg.get('plug_a_tone', False))),
        'b': (str(cfg.get('plug_b', 'off')).lower(), bool(cfg.get('plug_b_tone', False))),
    }


def _picotuner_reported_khz(st):
    """The frequency a receiver says it is tuned to, in kHz, or None if
    it isn't reporting one at all - which is what an untuned receiver
    looks like."""
    raw = str(st.get("frequency", "") or "").strip()
    if not raw:
        return None
    try:
        mhz = float(raw)
    except ValueError:
        return None
    if mhz <= 0:
        return None          # a freshly booted, untuned receiver
    return mhz * 1000.0


def _picotuner_reported_sr(st):
    """The symbol rate a receiver reports, or None if it isn't reporting
    one - which, like a missing frequency, is what an untuned receiver
    looks like."""
    raw = str(st.get("symbol_rate", "") or "").strip()
    if not raw:
        return None
    try:
        sr = float(raw)
    except ValueError:
        return None
    return sr if sr > 0 else None


def picotuner_tuning_watchdog():
    """Puts the Picotuner back where it belongs whenever it has lost its
    tuning, whatever the cause."""
    bad_since = None
    last_fix = 0.0
    while True:
        time.sleep(PICOTUNER_CHECK_SECS)
        try:
            if not picotuner_state.get("online"):
                bad_since = None      # can't judge what we can't hear
                continue

            expected = _picotuner_expected_tuning()
            if not expected:
                bad_since = None
                continue

            wrong = []
            for rcv, (want_khz, want_sr) in expected.items():
                st = picotuner_state_b if rcv == 2 else picotuner_state
                if rcv == 2 and not st.get("online"):
                    continue          # Rx2 not reporting at all - nothing to compare
                got_khz = _picotuner_reported_khz(st)
                if got_khz is None:
                    wrong.append(f"Rx{rcv} not tuned "
                                 f"(should be {want_khz/1000:.3f} MHz)")
                    continue
                if abs(got_khz - want_khz) > 1000.0:
                    # 1 MHz of slack. A locked receiver reports the
                    # frequency it actually found, not the one it was
                    # asked for - "437.023" against a commanded 437.000
                    # is a transmitter's own offset, not a fault. Only a
                    # genuinely different channel is worth acting on.
                    wrong.append(f"Rx{rcv} on {got_khz/1000:.3f} MHz "
                                 f"(should be {want_khz/1000:.3f})")
                    continue
                # Symbol rate, checked only once the frequency is right -
                # otherwise a receiver on the wrong channel would report
                # two faults for one cause.
                if want_sr:
                    got_sr = _picotuner_reported_sr(st)
                    if got_sr is None:
                        wrong.append(f"Rx{rcv} reports no symbol rate "
                                     f"(should be {want_sr} kS)")
                    elif abs(got_sr - float(want_sr)) > 2.0:
                        wrong.append(f"Rx{rcv} at {got_sr:.0f} kS "
                                     f"(should be {want_sr})")

            # LNB supply. The configured value is the intent; the global
            # is what the Picotuner is reporting back. They disagree when
            # the tuner has lost its supply setting - which it does on
            # every power cycle, silently, while continuing to look
            # perfectly healthy.
            want_lnb = _picotuner_expected_lnb()
            for plug, (want_v, want_t) in want_lnb.items():
                got_v = current_lnb_psu_a if plug == 'a' else current_lnb_psu_b
                got_t = current_lnb_tone_a if plug == 'a' else current_lnb_tone_b
                # Checked in BOTH directions. An earlier version skipped
                # plugs configured "off" on the reasoning that there was
                # nothing to restore - which was exactly backwards. A
                # Picotuner comes back from a power cycle with its supply
                # ON (18V observed on plug A on real hardware, every
                # time), so a plug that should be off is the case that
                # matters most: unexpected voltage can reach a preamp or
                # an antenna that isn't expecting it. Failing to apply a
                # supply costs a picture; applying one that shouldn't be
                # there can cost hardware.
                if got_v == 'absent':
                    # No voltage generator fitted on this plug - there is
                    # nothing to correct, and retrying forever would just
                    # fill the log.
                    continue
                if got_v != want_v or (want_v != 'off' and got_t != want_t):
                    wrong.append(f"LNB supply {plug.upper()} is "
                                 f"{got_v}{'+tone' if got_t else ''} "
                                 f"(should be {want_v}{'+tone' if want_t else ''})")

            if not wrong:
                bad_since = None
                continue

            now = time.time()
            if bad_since is None:
                bad_since = now
                continue
            if now - bad_since < PICOTUNER_MISMATCH_SECS:
                continue
            if now - last_fix < PICOTUNER_RETUNE_COOLDOWN_SECS:
                continue

            print("[picotuner] state does not match what it should be - "
                  "its firmware keeps neither tuning nor LNB supply across a "
                  "power cycle, so restoring:")
            for why in wrong:
                print(f"[picotuner]     {why}")
            last_fix = now
            bad_since = None
            threading.Thread(target=_picotuner_restore_tuning,
                             daemon=True).start()

        except Exception as e:
            print(f"[picotuner] tuning watchdog: {e}")


def _picotuner_restore_tuning():
    """Re-applies LNB supply then tuning, in the same order and with the
    same settling delays startup uses - so there is one behaviour to
    reason about rather than two."""
    try:
        time.sleep(PICOTUNER_RETUNE_SETTLE_SECS)

        if not picotuner_state.get("online"):
            print("[picotuner] went away before restore - will pick it up "
                  "again when it returns")
            return

        # ---- LNB supply first, same as startup ----
        # Commanded from the CONFIG, never from current_lnb_psu_a/b.
        # Those globals are overwritten by the Picotuner's own broadcast,
        # so after a power cycle they already say "off" - and an earlier
        # version of this restore, built on them, therefore re-applied
        # "off" and achieved precisely nothing. The configured value is
        # the only thing that still knows what the supply is meant to be.
        want_lnb = _picotuner_expected_lnb()
        sent_psu = False
        try:
            # "off" is commanded explicitly, not skipped. The Picotuner
            # powers up with its supply ON, so leaving a plug alone
            # because it is configured off would leave 18V sitting on it.
            v_a, t_a = want_lnb['a']
            cmd_a = v_a if v_a == "off" else f"{v_a}{'t' if t_a else ''}"
            if current_lnb_psu_a != v_a or (v_a != "off" and current_lnb_tone_a != t_a):
                picotuner_cmd(f"[to@wh] vgx={cmd_a}")
                print(f"[picotuner] restore: LNB supply A -> {cmd_a}")
                sent_psu = sent_psu or v_a != "off"

            v_b, t_b = want_lnb['b']
            cmd_b = v_b if v_b == "off" else f"{v_b}{'t' if t_b else ''}"
            if current_lnb_psu_b == 'absent':
                pass          # no generator fitted - nothing to command
            elif current_lnb_psu_b != v_b or (v_b != "off" and current_lnb_tone_b != t_b):
                picotuner_rcv2_cmd(f"[to@wh] vgy={cmd_b}", config['picotuner'])
                print(f"[picotuner] restore: LNB supply B -> {cmd_b}")
                sent_psu = sent_psu or v_b != "off"
        except Exception as e:
            print(f"[picotuner] restore: LNB supply failed ({e}) - "
                  "continuing to the tune anyway")

        if sent_psu:
            # Same 5s the startup path uses, for the same confirmed
            # reason: a cold PSU start with the tune sent too soon after
            # took minutes to lock on a genuinely good signal.
            time.sleep(5.0)

        # ---- then the tuning ----
        if _tri_watch_present():
            print("[picotuner] restore: re-tuning tri_watch RF sources")
            # Looked up rather than called directly, for the same reason
            # as _tri_watch_present(): this file is identical on main,
            # which has no tri_watch at all.
            _startup_tune = globals().get('tri_watch_startup_tune')
            if not _startup_tune:
                print("[picotuner] restore: tri_watch enabled but its tune "
                      "function is missing - nothing to re-tune")
                return
            _startup_tune()
            try:
                _arb = globals().get('tri_watch_arbitrator')
                _sync = globals().get('_tri_watch_sync_drainers')
                if _sync:
                    _sync(_arb.displayed_idx if _arb else None)
            except Exception as e:
                print(f"[picotuner] restore: drainer sync failed: {e}")
            return

        state = load_last_state()
        if not state or state.get("mode") != "rf":
            print("[picotuner] restore: no previous RF tuning to restore")
            return

        req = TuneRequest(freq=state["freq"], sr=state["sr"],
                          plug=state.get("plug", "a"),
                          lnb_lo_khz=state.get("lnb_lo_khz", 0))
        print(f"[picotuner] restore: re-tuning to {state['freq']} kHz / "
              f"{state['sr']} kS/s on plug {req.plug}")
        _tune_impl(req)

    except Exception as e:
        print(f"[picotuner] restore failed: {e}")


def tri_watch_arbitrator_loop():
    """Background thread - periodically builds the current active/
    locked state for every enabled tri_watch source and feeds it to
    the arbitrator's step() to decide what, if anything, needs to
    change. Every 2s: frequent enough that switching feels reasonably
    prompt, infrequent enough not to spam restart_mpv()/picotuner_cmd()
    if something is flapping right at the edge of lock."""
    while True:
        time.sleep(2)
        if not tri_watch_enabled:
            continue
        # Deliberately the startup snapshot, NOT config.get('tri_watch',
        # {}).get('sources', []) - see tri_watch_sources_cfg's own
        # module-level comment for why: this list's indices must stay
        # exactly matched to tri_watch_probes, which is also only ever
        # built once at startup.
        sources_cfg = tri_watch_sources_cfg
        active_map = {}
        for idx, src in enumerate(sources_cfg):
            if not src.get('enabled', False):
                continue
            if src.get('type') == 'rf':
                rcv = src.get('rcv', 1)
                state = picotuner_state_b if rcv == 2 else picotuner_state
                active_map[idx] = state['locked']
            elif src.get('type') == 'stream':
                probe = tri_watch_probes.get(idx)
                active_map[idx] = probe.is_active if probe is not None else False
        try:
            tri_watch_arbitrator.step(active_map, sources_cfg, _build_tri_watch_message)
        except Exception as e:
            print(f"[tri_watch] arbitrator step failed: {e}")

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

@app.get("/api/picotuner/discovered", tags=["Status"],
         summary="List Picotuners currently heard on the local network",
         description="Every Picotuner broadcasting on this network segment, "
                     "not just the one currently configured - a genuine UDP "
                     "broadcast, so this includes units never yet pointed at "
                     "this Lynx instance. Entries not heard from in the last "
                     "10 seconds are treated as offline and left out.")
def get_discovered_picotuners():
    now = time.time()
    return {
        "picotuners": [
            info for info in discovered_picotuners.values()
            if now - info["last_seen"] <= 10
        ]
    }

@app.get("/quicklynx", include_in_schema=False)
def serve_quicklynx():
    """Serves QuickLynx from this receiver, when enabled.

    Two things this buys over running it from a laptop. Served from the
    same origin as /api/tune there is no host to configure and no
    cross-origin question - the setting people most often get wrong
    otherwise. And it is reachable from any device on the network
    without running anything locally.

    Off by default: it holds an outbound connection to BATC, and most
    installations are repeaters that would never use it. QuickLynx's own
    standalone server still works unchanged for anyone who prefers it.
    """
    if not config.get('quicklynx', {}).get('enabled', False):
        return HTMLResponse(
            "<h3>QuickLynx is not enabled</h3>"
            "<p>Turn it on in <a href='/config'>Config</a>, under "
            "QuickLynx Spectrum Tuner.</p>", status_code=404)

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "quicklynx.html")
    if not os.path.exists(path):
        return HTMLResponse(
            "<h3>quicklynx.html not found</h3>"
            "<p>It is expected alongside lynx_app.py. Copy it from the "
            "QuickLynx repository.</p>", status_code=404)
    with open(path, encoding='utf-8') as f:
        html = f.read()
    # Tells the page which server it is under, so it can pick the right
    # chat proxy path and default the receiver address to this origin.
    # The same file is also served by QuickLynx's own standalone server,
    # where neither applies.
    html = html.replace("<head>", '<head>\n<meta name="lynx-hosted" content="1">', 1)
    return HTMLResponse(html)


@app.get("/quicklynx/chat", include_in_schema=False)
def proxy_quicklynx_chat():
    """Re-serves BATC's wideband chat page so it can be framed.

    BATC send X-Frame-Options, which stops the page being embedded from
    another origin - it loads fine in its own tab but comes up blank in
    an iframe. Fetching it here and serving it from this origin sidesteps
    that: the browser's frame check only cares where the framed DOCUMENT
    came from, not where its scripts and sockets subsequently talk to,
    which continue to reach BATC directly.
    """
    if not config.get('quicklynx', {}).get('enabled', False):
        return HTMLResponse("QuickLynx is not enabled", status_code=404)
    CHAT_URL = "https://eshail.batc.org.uk/wb/chat/"
    try:
        req = urllib.request.Request(
            CHAT_URL,
            # Some servers reject urllib's default User-Agent outright.
            headers={"User-Agent": "Mozilla/5.0 (compatible; Lynx QuickLynx proxy)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            html = r.read().decode(charset, errors="replace")

        # A <base> tag is essential, not cosmetic. Without it every
        # relative URL in the page - stylesheets, scripts, and any
        # relative Socket.IO connection its own JavaScript builds -
        # resolves against THIS server instead of BATC. The browser then
        # fetches a pile of files that do not exist here and renders a
        # blank white block, which is exactly what happened when this was
        # first written without one.
        base_tag = f'<base href="{CHAT_URL}">'
        if "<head>" in html:
            html = html.replace("<head>", "<head>" + base_tag, 1)
        elif "<head " in html:
            # a <head> with attributes: insert just after its closing >
            idx = html.index("<head ")
            close = html.index(">", idx) + 1
            html = html[:close] + base_tag + html[close:]
        else:
            html = base_tag + html

        # Deliberately a fresh response from this origin rather than a
        # forwarding of BATC's own headers - not carrying their
        # X-Frame-Options across is the entire point of proxying.
        return HTMLResponse(html)
    except Exception as e:
        print(f"[quicklynx] chat proxy failed: {e}")
        return HTMLResponse(
            f"<p style='font-family:sans-serif;padding:1em'>"
            f"Chat unavailable: {e}</p>", status_code=502)


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
            "portable_locator_provenance": _gnss_provenance(),
            "gnss": {"mode": config.get('gnss', {}).get('mode', 'automatic'),
                      "running": gnss_reader.running, **gnss_reader.status()},
            "ppm_style": config.get('display', {}).get('ppm_style', 'full_fat'),
            "quicklynx_enabled": bool(config.get('quicklynx', {}).get('enabled', False)),
            "site_locator": config.get('site', {}).get('locator', ''),
            # Published so the overlay's PPM can follow mpv to whichever
            # device it is actually using. Without this the meter taps
            # the default sink and would read silence whenever mpv is
            # sent somewhere else.
            "audio_device": config.get('display', {}).get('audio_device', 'hdmi'),
            "audio_device_resolved": _cached_audio_device_resolved(),
            "site_location": config.get('site', {}).get('location', ''),
            # End-of-contact map: None unless a card is due to be on
            # screen right now. The overlay only has to check presence -
            # all the timing is worked out here, from a timestamp.
            "pathfinder": pathfinder_tracker.get_card(),
            "squeak": _squeak_status(),
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
            "lnb_psu": {
                "plug_a": current_lnb_psu_a, "plug_a_tone": current_lnb_tone_a,
                "plug_b": current_lnb_psu_b, "plug_b_tone": current_lnb_tone_b,
            },
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
        },
        "tri_watch": get_tri_watch_status()
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

class PathfinderTestRequest(BaseModel):
    """Injects a card directly, bypassing lock detection and QRZ - the
    only practical way to iterate on the layout without waiting for a
    real station to appear and then stop."""
    callsign: str = "DL5BCA"
    locator: str = "JO31KL"
    name: str = ""
    mer: str = "9.4"
    modcod: str = "QPSK 2/3"
    symbol_rate: str = "333"


@app.get("/api/audio/monitors", tags=["Control"],
         summary="List audio monitor sources for Auto-Squeak",
         description="Monitor sources Auto-Squeak can listen on. The blank "
                     "entry means the monitor of whatever output is current, "
                     "which is the sensible default.")
def api_audio_monitors():
    return {"current": (config.get('squeak', {}) or {}).get('source', ''),
            "monitors": list_audio_monitors()}


@app.get("/api/audio/devices", tags=["Control"],
         summary="List the audio outputs mpv can see",
         description="Asks mpv directly, so the names returned are exactly "
                     "what the audio_device setting will accept. Used to "
                     "populate the Config page selector.")
def api_audio_devices():
    devices = physical_audio_devices()
    current = str(config.get('display', {}).get('audio_device', 'hdmi'))
    return {
        "current": current,
        "resolved": _first_hdmi_device() if current.lower() == 'hdmi' else current,
        "devices": devices,
    }


@app.post("/api/pathfinder/test", tags=["Status"],
          summary="Show a test end-of-contact map card",
          description="Arms the end-of-contact map for the given callsign "
                      "and locator as though that station had just stopped "
                      "transmitting. Honours the configured delay, so the "
                      "card appears after delay_secs and clears after "
                      "duration_secs. Any real station locking will cancel "
                      "it, exactly as it would a genuine card.")
def pathfinder_test(req: PathfinderTestRequest):
    if not pathfinder_tracker.enabled:
        raise HTTPException(status_code=400,
                            detail="pathfinder is disabled in the config")
    pos = lynx_map.locator_to_latlon(req.locator)
    if pos is None:
        raise HTTPException(status_code=400,
                            detail=f"'{req.locator}' is not a usable Maidenhead locator")
    # Honours pathfinder.qo100_test, so this endpoint is the way to
    # exercise the QO-100 globe at a site with no 3cm satellite
    # installation: set the switch, POST a distant locator, watch the
    # card. Without that the globe could only ever be seen by someone
    # who already had the hardware working.
    pathfinder_tracker.station_unlocked(
        req.callsign, req.locator, name=req.name or None, mer=req.mer,
        modcod=req.modcod, symbol_rate=req.symbol_rate,
        via_qo100=bool(config.get('pathfinder', {}).get('qo100_test', False)))
    home = config.get('site', {}).get('locator', '')
    pos_h = lynx_map.locator_to_latlon(home)
    dist = lynx_map.haversine_km(*pos_h, *pos) if pos_h else None
    return {
        "armed": True,
        "callsign": req.callsign,
        "locator": req.locator,
        "distance_km": round(dist, 1) if dist is not None else None,
        "home_locator": home,
        "appears_in_secs": pathfinder_tracker.delay_secs,
        "visible_for_secs": pathfinder_tracker.duration_secs,
        "note": None if home else
                "site.locator is not set in the config - no card can be drawn",
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

class QrzLookupTestRequest(BaseModel):
    test_callsign: str = "G8YTZ"

@app.post("/api/qrz/lookup_test", tags=["Status"],
          summary="Test the QRZ XML Data API name lookup",
          description="Directly tests the callsign-name lookup used for "
                      "tri_watch's notification, independent of an actual "
                      "notification firing - reports exactly which "
                      "pre-condition (if any) isn't met, or the real lookup "
                      "result. Deliberately synchronous/blocking here, unlike "
                      "the notification path itself - this is a one-off, "
                      "user-initiated test request, not something running "
                      "inside any time-critical loop.")
def qrz_lookup_test(req: QrzLookupTestRequest):
    qrz_cfg = config.get('notifications', {}).get('qrz', {})
    username = qrz_cfg.get('lookup_username', '')
    password = qrz_cfg.get('lookup_password', '')
    lookup_for_notifications = qrz_cfg.get('lookup_for_notifications', False)

    if not username or not password:
        return {
            "success": False,
            "reason": "No lookup_username/lookup_password configured - set both on "
                      "the Config page (or in config.yaml directly) first. These are "
                      "your normal QRZ.com login, separate from the Logbook API key above.",
            "lookup_for_notifications": lookup_for_notifications,
        }

    name = lynx_notifications.qrz_callsign_lookup(username, password, req.test_callsign)

    return {
        "success": name is not None,
        "test_callsign": req.test_callsign.strip().upper(),
        "name_found": name,
        "lookup_for_notifications": lookup_for_notifications,
        "note": (None if lookup_for_notifications else
                 "lookup_for_notifications is currently OFF - the lookup itself just "
                 "worked, but real notifications won't use it until this is turned on."),
    }

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
            <div class="d-flex gap-2 mb-2">
                <a href="/" class="btn btn-sm btn-outline-light">&#x1F3E0; Receiver</a>
                <a href="/config" class="btn btn-sm btn-outline-light">&#x2699;&#xFE0F; Config</a>
                <a href="/docs" class="btn btn-sm btn-outline-light">&#x1F4D6; API Docs</a>
            </div>
            <small class="text-muted">mpv start/stop events - auto-refreshes every 5s.</small>
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

    <div class="card mb-3">
        <div class="card-header">Test QRZ Name Lookup</div>
        <div class="card-body">
            <p class="text-muted small">Directly tests the callsign-name lookup used for tri_watch's
                waiting-station notification, independent of an actual notification firing. Shows
                exactly which pre-condition (if any) isn't met, or the real lookup result. Uses
                whatever lookup_username/lookup_password are currently configured on the Config
                page - a genuinely separate login from the Logbook API key above.</p>
            <div class="input-group input-group-sm mb-2" style="max-width: 300px;">
                <input type="text" class="form-control" id="qrz-lookup-test-callsign" value="G8YTZ"
                       placeholder="Callsign to look up">
                <button class="btn btn-outline-warning" onclick="sendQrzLookupTest()">Test Lookup</button>
            </div>
            <pre id="qrz-lookup-test-result" class="mt-3 mb-0 small" style="white-space: pre-wrap;"></pre>
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

async function sendQrzLookupTest() {
    const resultEl = document.getElementById('qrz-lookup-test-result');
    const callsign = document.getElementById('qrz-lookup-test-callsign').value.trim() || 'G8YTZ';
    resultEl.textContent = 'Looking up...';
    try {
        const r = await fetch('/api/qrz/lookup_test', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({test_callsign: callsign})
        });
        const data = await r.json();
        if (!r.ok) {
            resultEl.textContent = 'Error: ' + (data.detail || 'request failed');
            return;
        }
        let lines = [
            'success:                  ' + data.success,
        ];
        if (data.reason) {
            lines.push('reason:                   ' + data.reason);
        } else {
            lines.push('test_callsign:            ' + data.test_callsign);
            lines.push('name_found:               ' + data.name_found);
        }
        lines.push('lookup_for_notifications: ' + data.lookup_for_notifications);
        if (data.note) {
            lines.push('note:                     ' + data.note);
        }
        resultEl.textContent = lines.join('\\n');
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
        /* .form-select is a genuinely different Bootstrap 5 class from
           .form-control (used for <select> specifically) - every select
           on this page already deliberately uses form-control instead,
           which is why they've always matched; audio-device-select is
           the one exception that used the more "correct" Bootstrap
           class, which meant it fell through to Bootstrap's own default
           (light) select styling with nothing here overriding it. Styled
           to match form-control exactly rather than changing that
           input's own class, in case anything future uses form-select
           deliberately. */
        .form-select { background: #0f3460; border: 1px solid #1e4a7a; color: #e0e0e0; }
        .form-select:focus { background: #0f3460; border-color: #00d4aa; color: #e0e0e0; box-shadow: none; }
        /* Disabled inputs (e.g. the QRZ card's portable-locator field
           while GNSS Automatic mode is driving it) had no override at
           all, so they fell back to the browser's own default disabled
           styling - a light, out-of-place box against this dark theme.
           Keeps the same dark background, dims the text to signal
           "not editable right now" without breaking the theme. */
        .form-control:disabled, .form-select:disabled { background: #0f3460; opacity: 0.55; color: #a8b5c7; }
        /* Tells the browser to render native form-control chrome - the
           time-picker spinner/icon on <input type="time"> (GPIO Tx's
           schedule fields), and any date/datetime-local input - using
           its own built-in dark variant. background/color on the outer
           input don't reach into that native widget chrome at all,
           which is why those specific fields were still showing white
           despite already having the same form-control styling as
           every other input on the page. */
        input[type="time"], input[type="date"], input[type="datetime-local"] { color-scheme: dark; }
        .btn-save { background: #e94560; border-color: #e94560; }
        .btn-save:hover { background: #c73652; border-color: #c73652; }
        .save-status { font-size: 0.85em; min-height: 1.2em; }
        .placeholder-card { opacity: 0.6; }
        .gnss-unavailable { opacity: 0.5; }
        /* Visually masks sensitive fields (API keys, passwords) the same
           way type="password" would, without actually using that type -
           deliberately avoids browsers treating these as login
           credentials to offer saving/autofilling, which type="password"
           triggers regardless of autocomplete="off" (browsers routinely
           ignore that specific override on real password fields).
           -webkit-text-security is Chromium/WebKit-only (Chrome, Safari,
           Edge) - Firefox has no equivalent, so the field there falls
           back to showing plain text rather than being masked at all;
           an acceptable trade-off given the alternative was a genuinely
           broken, unwanted save-password prompt on every visit. */
        .mask-field { -webkit-text-security: disc; }
    </style>
</head>
<body>
<div class="container-fluid py-3">
    <div class="row mb-3">
        <div class="col">
            <h2 class="lynx-title">&#x25B6; LYNX CONFIGURATION</h2>
            <div class="d-flex gap-2">
                <a href="/" class="btn btn-sm btn-outline-light">&#x1F3E0; Receiver</a>
                <a href="/diagnostics" class="btn btn-sm btn-outline-light">&#x1F4CA; Diagnostics</a>
                <a href="/docs" class="btn btn-sm btn-outline-light">&#x1F4D6; API Docs</a>
            </div>
        </div>
    </div>

    <div class="row g-3 align-items-start">

        <div class="col-md-4">
                <div class="card mb-3">
                    <div class="card-header">&#x1F3AC; Video Switching (Bitfocus Companion / GPIO)</div>
                    <div class="card-body">
                        <p class="text-muted small">
                            Switches your video source to this receiver when it has
                            something to show, and away again when it hasn't. Fires a
                            webhook, a GPIO pin, or both. Bitfocus Companion is the
                            usual thing on the other end, but anything that accepts an
                            HTTP request or a contact closure will do - you don't need
                            Companion to use this.
                        </p>
                        <p class="text-muted small">
                            Follows RF <em>and</em> streams: a relayed stream switches
                            the source just as an off-air signal does. To key a
                            transmitter rather than switch a source, see GPIO Tx On/Off
                            below.
                        </p>
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
                        <div id="companion-pathfinder-warning" class="alert alert-warning py-2 px-2 small mt-2 mb-0" style="display:none;"></div>
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
                <div class="card mb-3">
                    <div class="card-header">&#x1F50C; GPIO Tx On/Off</div>
                    <div class="card-body">
                        <p class="text-muted small">
                            Keys a transmitter when there is something to send, with
                            long settle times so it isn't cycled by brief gaps, and
                            optional scheduled on-air windows.
                        </p>
                        <p class="text-muted small">
                            This is <em>not</em> the pin for switching a video source -
                            for that use Video Switching above, which has its own pin
                            and much shorter timings.
                        </p>
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
        </div>

        <div class="col-md-4">
                <div class="card mb-3">
                <div class="card mb-3">
                    <div class="card-header">&#x1F50A; Audio Output</div>
                    <div class="card-body">
                        <p class="text-muted small">
                            Which device mpv sends audio to. Defaults to HDMI, so sound
                            follows the picture to the television. Left on automatic,
                            a USB audio dongle plugged in for some other purpose can
                            quietly capture the audio instead. The PPM meter follows
                            this setting, so it always reads whatever is actually
                            being sent out.
                        </p>
                        <label class="small text-muted mb-1" for="audio-device-select">Device</label>
                        <select class="form-select form-select-sm mb-2" id="audio-device-select">
                            <option value="hdmi">HDMI (recommended)</option>
                            <option value="auto">Automatic - let mpv choose</option>
                        </select>
                        <div id="audio-device-resolved" class="small text-muted mb-2"></div>
                        <button class="btn btn-save" onclick="saveAudioDevice()">Save Audio settings</button>
                        <span id="audio-save-status" class="save-status ms-2"></span>
                    </div>
                </div>

                    <div class="card-header">&#x1F3AF; PPM Meter Style</div>
                    <div class="card-body">
                        <p class="text-muted small">Applies live within a few seconds - no restart needed.</p>
                        <div class="form-check">
                            <input class="form-check-input" type="radio" name="ppm-style" id="ppm-style-skeleton" value="skeleton">
                            <label class="form-check-label" for="ppm-style-skeleton">
                                Skeleton <span class="text-muted small">- needles and graduations only</span>
                            </label>
                        </div>
                        <div class="form-check mb-2">
                            <input class="form-check-input" type="radio" name="ppm-style" id="ppm-style-full-fat" value="full_fat">
                            <label class="form-check-label" for="ppm-style-full-fat">
                                Full Fat <span class="text-muted small">- with the round meter housing</span>
                            </label>
                        </div>
                        <button class="btn btn-save" onclick="savePpmStyle()">Save PPM style</button>
                        <span id="ppm-style-status" class="save-status ms-2"></span>
                    </div>
                </div>
                <div class="card mb-3">
                    <div class="card-header">&#x1F5FA;&#xFE0F; Pathfinder</div>
                    <div class="card-body">
                        <p class="text-muted small">
                            Shows a full-screen map after a station stops transmitting -
                            where they were, the path back here, and the signal figures
                            from the contact. Needs the QRZ.com lookup below configured,
                            since the station's position comes from their QRZ locator.
                        </p>
                        <div class="form-check form-switch mb-3">
                            <input class="form-check-input" type="checkbox" id="pf-enabled">
                            <label class="form-check-label" for="pf-enabled">Enabled</label>
                        </div>
                        <div class="row g-2 mb-2">
                            <div class="col-6">
                                <label class="form-label small mb-1" for="pf-delay">Delay (s)</label>
                                <input type="number" min="0" max="300" step="1" class="form-control form-control-sm" id="pf-delay">
                                <div class="small text-muted mt-1">Quiet gap after unlock</div>
                            </div>
                            <div class="col-6">
                                <label class="form-label small mb-1" for="pf-duration">Duration (s)</label>
                                <input type="number" min="1" max="300" step="1" class="form-control form-control-sm" id="pf-duration">
                                <div class="small text-muted mt-1">How long it stays up</div>
                            </div>
                        </div>
                        <div id="pf-window" class="text-muted small mb-2"></div>
                        <div id="pf-warning" class="alert alert-warning py-2 px-2 small mb-2" style="display:none;"></div>
                        <div class="text-muted small mb-2">
                            At a repeater, also check your controller's own hang/tail timer -
                            Lynx can't see that, and it will cut the transmitter regardless
                            of anything set here.
                        </div>
                        <button class="btn btn-save" onclick="savePathfinder()">Save Pathfinder settings</button>
                        <span id="pf-status" class="save-status ms-2"></span>
                    </div>
                </div>
                <div class="card mb-3">
                    <div class="card-header">&#x1F4D6; QRZ.com Logbook</div>
                    <div class="card-body">
                        <div class="form-check form-switch mb-3">
                            <input class="form-check-input" type="checkbox" id="qrz-enabled-input">
                            <label class="form-check-label" for="qrz-enabled">Enabled</label>
                        </div>
                        <label for="qrz-api-key">API key</label>
                        <div class="input-group mb-2">
                            <input type="text" class="form-control mask-field" id="qrz-api-key-input"
                                   autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
                                   placeholder="Your QRZ Logbook API key">
                            <button class="btn btn-outline-secondary reveal-btn" type="button" tabindex="-1"
                                    data-reveal-target="qrz-api-key-input" title="Hold to reveal">&#x1F441;&#xFE0F;</button>
                        </div>
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
                        <label for="qrz-portable-locator" class="mt-2">Portable locator (this receiver's own position)</label>
                        <input type="text" class="form-control" id="qrz-portable-locator-input"
                               placeholder="e.g. IO91VG - leave blank for normal operation">
                        <p class="text-muted small mt-2 mb-0">
                            Settle time: delay after lock before logging, so the callsign has time to
                            decode. Suppress: don't log the same callsign again within this many minutes.
                            Portable locator: set this when Lynx itself is operating away from its
                            normal, registered site - it's attached to every logged contact as Lynx's
                            own current position (ADIF MY_GRIDSQUARE), so QRZ's distance/bearing figures
                            are correct for wherever Lynx actually is right now, not its usual fixed
                            site. It does not touch the worked station's own locator, which QRZ
                            continues to derive from their profile as normal. Clear it (empty + save)
                            once the portable session ends. See GNSS Portable Locator below to populate
                            this automatically from a GPS receiver instead of typing it by hand.
                        </p>
                        <hr class="my-3">
                        <p class="text-muted small mb-2">
                            <strong>Callsign lookup</strong> (used to add a station's first name to
                            tri_watch's "someone else wants in" notification) uses QRZ's separate XML
                            Data API - a genuinely different login to the Logbook API key above, since
                            it authenticates with your normal QRZ username/password rather than an API
                            key. Requires an XML-subscriber-level QRZ account.
                        </p>
                        <div class="row g-2 mb-2">
                            <div class="col-6">
                                <label for="qrz-lookup-username">QRZ username</label>
                                <input type="text" class="form-control" id="qrz-lookup-username-input"
                                       autocomplete="off"
                                       placeholder="Your QRZ.com login">
                            </div>
                            <div class="col-6">
                                <label for="qrz-lookup-password">QRZ password</label>
                                <div class="input-group">
                                    <input type="text" class="form-control mask-field" id="qrz-lookup-password-input"
                                           autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false"
                                           placeholder="Your QRZ.com password">
                                    <button class="btn btn-outline-secondary reveal-btn" type="button" tabindex="-1"
                                            data-reveal-target="qrz-lookup-password-input" title="Hold to reveal">&#x1F441;&#xFE0F;</button>
                                </div>
                            </div>
                        </div>
                        <div class="form-check form-switch">
                            <input class="form-check-input" type="checkbox" id="qrz-lookup-notif-input">
                            <label class="form-check-label" for="qrz-lookup-notif">
                                Add looked-up name to tri_watch's waiting-station notification
                            </label>
                        </div>
                        <div class="mt-3 d-flex align-items-center gap-2">
                            <button class="btn btn-save" onclick="saveQrz()">Save QRZ settings</button>
                            <span class="save-status" id="qrz-save-status"></span>
                        </div>
                    </div>
                </div>
                <div class="card mb-3">
                <div class="card mb-3">
                    <div class="card-header">&#x1F4C8; Auto-Squeak &mdash; audio measurement</div>
                    <div class="card-body">
                        <p class="text-muted small">
                            Listens for a Lindos test sequence on the received audio and puts a
                            measurement card on screen after each pass: level against alignment,
                            channel balance, frequency response, noise, separation and distortion.
                            Nothing to send from here &mdash; whoever transmits the sequence provides
                            the signal, and Lindos publish the standard sequences as WAV files, so
                            no test equipment is needed at either end. On by default: it stays idle
                            until a sequence actually arrives.
                        </p>
                        <div class="form-check form-switch mb-2">
                            <input class="form-check-input" type="checkbox" id="squeak-enabled-input">
                            <label class="form-check-label" for="squeak-enabled-input">
                                Measure the audio path when a test sequence is received
                            </label>
                        </div>
                        <label class="small text-muted mb-1" for="squeak-source-select">Listen on</label>
                        <select class="form-select form-select-sm mb-2" id="squeak-source-select"></select>
                        <p class="text-muted small mb-2">
                            Automatic follows whatever output is selected above, so it keeps working
                            if that is changed. Only pick a specific monitor to override this.
                        </p>
                        <label class="small text-muted mb-1" for="squeak-hold-input">Card on screen for (seconds)</label>
                        <input type="number" min="10" max="300" step="5"
                               class="form-control mb-2" id="squeak-hold-input">
                        <p class="text-muted small mb-2">
                            Worth matching to the gap between passes in the test file, so the previous
                            result is still visible while an adjustment is being made. The card is drawn
                            over the picture, and takes precedence over a Pathfinder map &mdash; the map
                            waits its turn rather than being lost.
                        </p>
                        <div class="mt-2 d-flex align-items-center gap-2">
                            <button class="btn btn-save" onclick="saveSqueak()">Save Auto-Squeak settings</button>
                            <span class="save-status" id="squeak-save-status"></span>
                        </div>
                    </div>
                </div>

                    <div class="card-header">&#x1F6F0;&#xFE0F; GNSS Portable Locator</div>
                    <div class="card-body">
                        <p class="text-muted small">
                            Reads a Waveshare L76K HAT on /dev/ttyAMA0 and, once a fix has held
                            steady in the same 6-character square for 30 seconds, writes it straight
                            into the "Portable locator" field above - the same field an operator
                            would otherwise type by hand, attached to every logged contact as this
                            receiver's own current position. A fixed repeater site simply doesn't
                            have the HAT fitted, so this fails quietly and does nothing there.
                            Configures the module for GPS + BeiDou + GLONASS (plus QZSS, always on
                            regardless) on each connect - every constellation the L76K can actually
                            use, for the widest satellite visibility.
                        </p>
                        <div class="form-check">
                            <input class="form-check-input" type="radio" name="gnss-mode"
                                   id="gnss-mode-manual" value="manual" onchange="onGnssModeChange()">
                            <label class="form-check-label" for="gnss-mode-manual">
                                <strong>Manual</strong>
                                <span class="text-muted small">- the typed value above is authoritative; GPS is only shown</span>
                            </label>
                        </div>
                        <div id="gnss-no-module-warning" class="alert alert-warning py-1 px-2 small mb-2" style="display:none;">
                            &#x26A0;&#xFE0F; No GNSS module detected on /dev/ttyAMA0. Automatic is greyed
                            out below until one responds - Manual above is unaffected either way, and
                            un-greys live, no reload, the moment a fitted HAT starts answering.
                        </div>
                        <div id="gnss-hw-dependent">
                        <div class="form-check mb-2">
                            <input class="form-check-input" type="radio" name="gnss-mode"
                                   id="gnss-mode-automatic" value="automatic" onchange="onGnssModeChange()">
                            <label class="form-check-label" for="gnss-mode-automatic">
                                <strong>Automatic</strong>
                                <span class="text-muted small">- GPS drives the locator on every confirmed fix (default)</span>
                            </label>
                        </div>
                        <hr class="my-2">
                        <div class="form-check form-switch mb-2">
                            <input class="form-check-input" type="checkbox" id="gnss-time-sync-input">
                            <label class="form-check-label" for="gnss-time-sync">
                                Set the clock from GPS when there's no internet
                            </label>
                            <p class="text-muted small mt-1 mb-0">
                                Keeps logged contacts correctly timestamped at a site with no
                                network. Uses the internet as usual whenever it's available.
                            </p>
                        </div>
                        </div>
                        <div id="gnss-status-box" class="small p-2 mb-2" style="background:#0f3460; border-radius:4px;">
                            <div class="d-flex justify-content-between"><span>Receiver</span><span class="status-value" id="gnss-connected">—</span></div>
                            <div class="d-flex justify-content-between"><span>Locator (8-char)</span><span class="status-value" id="gnss-locator-display">—</span></div>
                            <div class="d-flex justify-content-between"><span>Satellites / HDOP</span><span class="status-value" id="gnss-quality">—</span></div>
                            <div class="d-flex justify-content-between"><span>Time sync</span><span class="status-value" id="gnss-time-sync-status">—</span></div>
                            <div class="d-flex justify-content-between"><span>Status</span><span class="status-value" id="gnss-detail">—</span></div>
                        </div>
                        <div class="mt-2 d-flex align-items-center gap-2">
                            <button class="btn btn-save" onclick="saveGnssMode()">Save GNSS mode</button>
                            <span class="save-status" id="gnss-save-status"></span>
                        </div>
                    </div>
                </div>
        </div>

        <div class="col-md-4">
                <div class="card mb-3">
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

                <div class="card mb-3">
                    <div class="card-header">&#x1F50D; Discovered on this network</div>
                    <div class="card-body">
                        <p class="text-muted small">Any Picotuner currently broadcasting nearby, not just the
                            one configured above - click "Use" to fill in its IP address directly, rather
                            than finding it by hand.</p>
                        <div id="discovered-picotuners-list">
                            <p class="text-muted small">Checking...</p>
                        </div>
                    </div>
                </div>
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
                <div class="card mb-3">
                    <div class="card-header">&#x1F9EA; Update Channel</div>
                    <div class="card-body">
                        <p class="text-muted small">
                            Stable is the recommended channel for everyday/repeater use -
                            known-working releases only. Beta tracks newer, less-tested code
                            for anyone who wants to try new features early, mirroring how
                            BATC Portsdown offers its own beta channel. Switchable back to
                            Stable at any time.
                        </p>
                        <div class="form-check">
                            <input class="form-check-input" type="radio" name="update-channel"
                                   id="channel-stable" value="stable" onchange="onChannelRadioChange()">
                            <label class="form-check-label" for="channel-stable">
                                <strong>Stable</strong>
                                <span class="text-muted small">- recommended</span>
                            </label>
                        </div>
                        <div class="form-check mb-2">
                            <input class="form-check-input" type="radio" name="update-channel"
                                   id="channel-beta" value="beta" onchange="onChannelRadioChange()">
                            <label class="form-check-label" for="channel-beta">
                                <strong>Beta</strong>
                                <span class="text-muted small">- newer, experimental code</span>
                            </label>
                        </div>
                        <div id="beta-warning" class="alert alert-warning py-2 px-3 small mb-2" style="display:none;">
                            &#x26A0;&#xFE0F; <strong>Beta channel:</strong> newer code that hasn't
                            been as thoroughly tested - may be less stable, and could contain
                            bugs not yet caught. Not recommended for an unattended repeater
                            without someone available to fix issues if something goes wrong.
                        </div>
                        <div class="mt-2 d-flex align-items-center gap-2">
                            <button class="btn btn-outline-warning btn-sm" onclick="switchUpdateChannel()">Switch Channel</button>
                            <span class="save-status" id="channel-save-status"></span>
                        </div>
                    </div>
                </div>
                <div class="card mb-3">
                    <div class="card-header">&#x1F4F6; WiFi</div>
                    <div class="card-body">
                        <p class="text-muted small" style="color:#d98a1e !important">
                            &#x26A0;&#xFE0F; WiFi is not recommended for this receiver, especially
                            with more than one saved network - a roaming/reconnect event can
                            land on a different subnet and silently break the
                            Picotuner/Knobler's local discovery. Wired Ethernet is strongly
                            preferred. If WiFi must be used, power-save is now disabled
                            automatically at startup.
                        </p>
                        <p class="text-muted small">
                            WARNING: if this Pi is only reachable over WiFi right now,
                            disabling it will cut off Web UI access until it's re-enabled
                            locally or via SSH (<code>sudo rfkill unblock wifi</code>).
                        </p>
                        <div class="d-flex align-items-center gap-2">
                            <button class="btn btn-outline-danger btn-sm" onclick="killWifi()">Disable WiFi</button>
                            <button class="btn btn-outline-light btn-sm" onclick="restoreWifi()">Re-enable WiFi</button>
                            <span class="save-status" id="wifi-status"></span>
                        </div>
                    </div>
                </div>

                <div class="card mb-3">
                    <div class="card-header">&#x1F4E1; QuickLynx Spectrum Tuner</div>
                    <div class="card-body">
                        <p class="text-muted small">
                            Serves QuickLynx from this receiver: the QO-100 wideband
                            spectrum, with click-to-tune straight into Lynx. Served
                            from here it needs no address configuring and can be
                            opened from any device on your network.
                        </p>
                        <div class="form-check form-switch mb-2">
                            <input class="form-check-input" type="checkbox" id="quicklynx-enabled">
                            <label class="form-check-label" for="quicklynx-enabled">Enabled</label>
                        </div>
                        <div class="small text-muted mb-2">
                            Off by default. Needs internet access for the spectrum feed
                            and the chat pane, so it is opt-in rather than something a
                            repeater carries unasked.
                        </div>
                        <button class="btn btn-save" onclick="saveQuickLynx()">Save QuickLynx settings</button>
                        <span id="quicklynx-status" class="save-status ms-2"></span>
                    </div>
                </div>
        </div>

    </div>

    <div class="row g-3 mt-1">
        <div class="col-12">
            <div class="card mb-3">
                <div class="card-header">&#x1F500; Tri-Watch Sources</div>
                <div class="card-body">
                    <div class="form-check form-switch mb-2">
                        <input class="form-check-input" type="checkbox" id="tw-enabled-input">
                        <label class="form-check-label" for="tw-enabled">Tri-Watch enabled</label>
                    </div>
                    <p class="text-muted small">
                        Up to three sources - any mix of Rx1, Rx2, and a stream. Uncheck
                        "Include" on a source to leave it out entirely, same as not listing
                        it in config.yaml at all. Frequency is in kHz, matching the RF
                        Tuning card elsewhere on this page. Changes here need a Lynx
                        restart to take effect - tri_watch's sources are set up once at
                        startup, unlike most other settings on this page.
                    </p>
                    <div class="row g-3">
                        <div class="col-md-4">
                            <h6 class="mb-2">Rx 1</h6>
                            <div class="form-check form-switch mb-2">
                                <input class="form-check-input" type="checkbox" id="tw-rx1-enabled-input">
                                <label class="form-check-label" for="tw-rx1-enabled">Include Rx1</label>
                            </div>
                            <label for="tw-rx1-freq" class="small">Frequency (kHz)</label>
                            <input type="number" step="1" class="form-control mb-2" id="tw-rx1-freq-input" placeholder="e.g. 437000">
                            <label for="tw-rx1-sr" class="small">Symbol rate (kS/s)</label>
                            <input type="number" step="1" class="form-control mb-2" id="tw-rx1-sr-input" placeholder="e.g. 1000">
                            <label for="tw-rx1-plug" class="small">Plug</label>
                            <select class="form-control mb-2" id="tw-rx1-plug-input">
                                <option value="a">A</option>
                                <option value="b">B</option>
                            </select>
                            <label for="tw-rx1-label" class="small">Label</label>
                            <input type="text" class="form-control mb-2" id="tw-rx1-label-input" placeholder="e.g. 70cm Live">
                            <label for="tw-rx1-callsign" class="small">Expected callsign</label>
                            <input type="text" class="form-control" id="tw-rx1-callsign-input" placeholder="e.g. GB3JT">
                        </div>
                        <div class="col-md-4">
                            <h6 class="mb-2">Rx 2</h6>
                            <div class="form-check form-switch mb-2">
                                <input class="form-check-input" type="checkbox" id="tw-rx2-enabled-input">
                                <label class="form-check-label" for="tw-rx2-enabled">Include Rx2</label>
                            </div>
                            <label for="tw-rx2-freq" class="small">Frequency (kHz)</label>
                            <input type="number" step="1" class="form-control mb-2" id="tw-rx2-freq-input" placeholder="e.g. 1249000">
                            <label for="tw-rx2-sr" class="small">Symbol rate (kS/s)</label>
                            <input type="number" step="1" class="form-control mb-2" id="tw-rx2-sr-input" placeholder="e.g. 1000">
                            <label for="tw-rx2-plug" class="small">Plug</label>
                            <select class="form-control mb-2" id="tw-rx2-plug-input">
                                <option value="a">A</option>
                                <option value="b" selected>B</option>
                            </select>
                            <label for="tw-rx2-label" class="small">Label</label>
                            <input type="text" class="form-control mb-2" id="tw-rx2-label-input" placeholder="e.g. 23cm Live">
                            <label for="tw-rx2-callsign" class="small">Expected callsign</label>
                            <input type="text" class="form-control" id="tw-rx2-callsign-input" placeholder="e.g. GB3JT">
                        </div>
                        <div class="col-md-4">
                            <h6 class="mb-2">Stream</h6>
                            <div class="form-check form-switch mb-2">
                                <input class="form-check-input" type="checkbox" id="tw-stream-enabled-input">
                                <label class="form-check-label" for="tw-stream-enabled">Include stream</label>
                            </div>
                            <label for="tw-stream-domain" class="small">RTMP domain</label>
                            <input type="text" class="form-control mb-2" id="tw-stream-domain-input" placeholder="e.g. rtmp.batc.org.uk">
                            <label for="tw-stream-app" class="small">App</label>
                            <input type="text" class="form-control mb-2" id="tw-stream-app-input" placeholder="e.g. live">
                            <label for="tw-stream-name" class="small">Stream name</label>
                            <input type="text" class="form-control mb-2" id="tw-stream-name-input" placeholder="e.g. gb3jtinput">
                            <label for="tw-stream-port" class="small">Port</label>
                            <input type="number" step="1" class="form-control mb-2" id="tw-stream-port-input" placeholder="1935">
                            <label for="tw-stream-label" class="small">Label</label>
                            <input type="text" class="form-control mb-2" id="tw-stream-label-input" placeholder="e.g. Live Stream">
                            <label for="tw-stream-waiting-message" class="small">Notification text</label>
                            <input type="text" class="form-control" id="tw-stream-waiting-message-input"
                                   placeholder="Leave blank for the default wording">
                        </div>
                    </div>
                    <div class="mt-3 d-flex align-items-center gap-2">
                        <button class="btn btn-save" onclick="saveTriWatch()">Save Tri-Watch sources</button>
                        <span class="save-status" id="tw-save-status"></span>
                    </div>
                </div>
            </div>
        </div>
    </div>

</div>

<script>
// ── Reveal sensitive fields (API keys, passwords) ────────────────
// Click-to-toggle (not hold-to-view - that was tried first and,
// despite passing every isolated event-dispatch test, repeatedly
// didn't work for Justin in practice; a genuinely quick click on a
// hold-only control can reveal-and-immediately-re-hide within
// milliseconds, easy to miss and easy to mistake for "does nothing"
// at all). A plain click is the simplest, most universally-reliable
// interaction there is - identical for mouse and touch, with no
// press-duration timing involved anywhere. Auto-hides after 10s so a
// revealed value still can't be left exposed indefinitely if someone
// walks away - keeping the original camera/shoulder-surfing concern
// addressed without needing a sustained hold to do it. Toggles the
// CSS masking (see .mask-field) directly rather than the input's
// type - these fields are genuinely type="text" throughout,
// deliberately never type="password", so browsers never treat them
// as login credentials to offer saving/autofilling (confirmed as a
// real, reported annoyance when they briefly were: an unwanted
// save-password prompt on every visit, sometimes guessing a
// nonsensical "username" from an unrelated nearby field).
function revealField(id) {
    const el = document.getElementById(id);
    if (el) el.style.webkitTextSecurity = 'none';
}
function hideField(id) {
    const el = document.getElementById(id);
    if (el) el.style.webkitTextSecurity = 'disc';
}
function setupRevealButtons() {
    document.querySelectorAll('.reveal-btn').forEach(btn => {
        const targetId = btn.getAttribute('data-reveal-target');
        let autoHideTimer = null;
        btn.addEventListener('click', () => {
            const el = document.getElementById(targetId);
            if (!el) return;
            const isRevealed = el.style.webkitTextSecurity === 'none';
            clearTimeout(autoHideTimer);
            if (isRevealed) {
                hideField(targetId);
                btn.textContent = '\U0001F441\uFE0F';
            } else {
                revealField(targetId);
                btn.textContent = '\U0001F648';  // visually distinct "currently revealed" state
                autoHideTimer = setTimeout(() => {
                    hideField(targetId);
                    btn.textContent = '\U0001F441\uFE0F';
                }, 10000);
            }
        });
    });
}
setupRevealButtons();

async function loadCurrentConfig() {
    try {
        const cfg = await fetch('/api/config').then(r => r.json());
        document.getElementById('site-name-input').value = cfg.site?.name || '';
        document.getElementById('site-callsign-input').value = cfg.site?.callsign || '';
        document.getElementById('site-location-input').value = cfg.site?.location || '';
        document.getElementById('site-locator-input').value = cfg.site?.locator || '';

        const pf = cfg.pathfinder || {};
        document.getElementById('pf-enabled').checked = pf.enabled !== false;
        document.getElementById('pf-delay').value = pf.delay_secs ?? 2;
        document.getElementById('pf-duration').value = pf.duration_secs ?? 30;
        updatePathfinderWarning();
        // Re-check whenever any of the four inputs move, so the warning
        // appears the moment a conflict is created rather than only on
        // reload. Bound here (after the values are populated) rather
        // than at page load, so setting the initial values can't itself
        // fire the handler before the config has arrived.
        ['pf-enabled', 'pf-delay', 'pf-duration',
         'companion-unlock-settle-input', 'companion-enabled-input'
        ].forEach(id => {
            const el = document.getElementById(id);
            if (el && !el.dataset.pfBound) {
                el.addEventListener('change', updatePathfinderWarning);
                el.addEventListener('input', updatePathfinderWarning);
                el.dataset.pfBound = '1';
            }
        });

        loadAudioDevices(cfg.display?.audio_device || 'hdmi');

        const ppmStyle = cfg.display?.ppm_style || 'full_fat';
        document.getElementById(ppmStyle === 'full_fat' ? 'ppm-style-full-fat' : 'ppm-style-skeleton').checked = true;

        const updateChannel = cfg.update?.channel || 'stable';
        document.getElementById(updateChannel === 'beta' ? 'channel-beta' : 'channel-stable').checked = true;
        onChannelRadioChange();

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

        document.getElementById('squeak-enabled-input').checked = (cfg.squeak?.enabled !== false);
        document.getElementById('squeak-hold-input').value = cfg.squeak?.hold_secs ?? 45;
        loadSqueakSources();

        const rawGnssMode = cfg.gnss?.mode;
        // Anything other than exactly 'automatic' displays as Manual -
        // covers a genuinely unset config (defaults to automatic,
        // matching the backend) as well as a legacy 'off' value from
        // before that mode was removed, which the backend already
        // treats the same as manual (see _on_gnss_locator_change).
        const gnssMode = (rawGnssMode === undefined || rawGnssMode === 'automatic')
            ? 'automatic' : 'manual';
        document.getElementById('gnss-mode-' + gnssMode).checked = true;
        document.getElementById('gnss-time-sync-input').checked = cfg.gnss?.time_sync ?? true;
        onGnssModeChange();
        document.getElementById('qrz-lookup-username-input').value = qrz.lookup_username || '';
        document.getElementById('qrz-lookup-password-input').value = qrz.lookup_password || '';
        document.getElementById('qrz-lookup-notif-input').checked = qrz.lookup_for_notifications || false;

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

        // Tri-Watch: sources is a list (any mix of Rx1/Rx2/stream, in
        // any order) rather than fixed fields, so each is found by
        // type/rcv rather than assumed to be at a fixed index.
        const tw = cfg.tri_watch || {};
        const twSources = tw.sources || [];
        const twRx1 = twSources.find(s => s.type === 'rf' && s.rcv === 1 && s.enabled !== false);
        const twRx2 = twSources.find(s => s.type === 'rf' && s.rcv === 2 && s.enabled !== false);
        const twStream = twSources.find(s => s.type === 'stream' && s.enabled !== false);

        document.getElementById('tw-enabled-input').checked = tw.enabled || false;

        document.getElementById('tw-rx1-enabled-input').checked = !!twRx1;
        document.getElementById('tw-rx1-freq-input').value = twRx1?.freq ?? '';
        document.getElementById('tw-rx1-sr-input').value = twRx1?.sr ?? '';
        document.getElementById('tw-rx1-plug-input').value = twRx1?.fplug || 'a';
        document.getElementById('tw-rx1-label-input').value = twRx1?.label || '';
        document.getElementById('tw-rx1-callsign-input').value = twRx1?.callsign || '';

        document.getElementById('tw-rx2-enabled-input').checked = !!twRx2;
        document.getElementById('tw-rx2-freq-input').value = twRx2?.freq ?? '';
        document.getElementById('tw-rx2-sr-input').value = twRx2?.sr ?? '';
        document.getElementById('tw-rx2-plug-input').value = twRx2?.fplug || 'b';
        document.getElementById('tw-rx2-label-input').value = twRx2?.label || '';
        document.getElementById('tw-rx2-callsign-input').value = twRx2?.callsign || '';

        document.getElementById('tw-stream-enabled-input').checked = !!twStream;
        document.getElementById('tw-stream-domain-input').value = twStream?.domain || '';
        document.getElementById('tw-stream-app-input').value = twStream?.app || '';
        document.getElementById('tw-stream-name-input').value = twStream?.streamname || '';
        document.getElementById('tw-stream-port-input').value = twStream?.port ?? 1935;
        document.getElementById('tw-stream-label-input').value = twStream?.label || '';
        document.getElementById('tw-stream-waiting-message-input').value = twStream?.waiting_message || '';
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

function onChannelRadioChange() {
    const selected = document.querySelector('input[name="update-channel"]:checked')?.value;
    document.getElementById('beta-warning').style.display = (selected === 'beta') ? 'block' : 'none';
}

async function switchUpdateChannel() {
    const statusEl = document.getElementById('channel-save-status');
    const selected = document.querySelector('input[name="update-channel"]:checked')?.value;
    if (!selected) return;

    try {
        const current = await fetch('/api/update/status').then(r => r.json());
        if ((current.channel || 'stable') === selected) {
            statusEl.textContent = 'Already on this channel.';
            statusEl.className = 'save-status text-muted';
            return;
        }
    } catch (e) { /* if this check fails, fall through and let the switch itself report any real problem */ }

    const warning = selected === 'beta'
        ? 'Switch to the Beta channel? This pulls newer, less-tested code and reboots the Pi. ' +
          'You can switch back to Stable at any time.'
        : 'Switch back to the Stable channel? This pulls the known-working release and reboots the Pi.';
    if (!confirm(warning)) return;

    statusEl.textContent = 'Switching...';
    statusEl.className = 'save-status text-muted';
    try {
        const r = await fetch('/api/update/channel', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({channel: selected})
        });
        const result = await r.json();
        if (!r.ok) {
            statusEl.textContent = 'Failed: ' + (result.detail || 'unknown error');
            statusEl.className = 'save-status text-danger';
            return;
        }
        statusEl.textContent = 'Switched - rebooting the Pi now, back in about a minute.';
        statusEl.className = 'save-status text-warning';
    } catch (e) {
        // The server may already be going down as part of the reboot by
        // the time this resolves - not itself a sign anything went wrong.
        statusEl.textContent = 'Switched - rebooting the Pi now, back in about a minute.';
        statusEl.className = 'save-status text-warning';
    }
}

async function killWifi() {
    const statusEl = document.getElementById('wifi-status');
    if (!confirm('Disable WiFi now? If this Pi is only reachable over WiFi, you will lose ' +
                 'Web UI access immediately until it is re-enabled locally or via SSH.')) return;
    statusEl.textContent = 'Disabling...';
    statusEl.className = 'save-status text-muted';
    try {
        const r = await fetch('/api/wifi/kill', {method: 'POST'});
        const result = await r.json();
        statusEl.textContent = r.ok ? 'WiFi disabled.' : ('Failed: ' + (result.detail || 'unknown error'));
        statusEl.className = r.ok ? 'save-status text-success' : 'save-status text-danger';
    } catch (e) {
        statusEl.textContent = 'Request failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function restoreWifi() {
    const statusEl = document.getElementById('wifi-status');
    statusEl.textContent = 'Re-enabling...';
    statusEl.className = 'save-status text-muted';
    try {
        const r = await fetch('/api/wifi/restore', {method: 'POST'});
        const result = await r.json();
        statusEl.textContent = r.ok ? 'WiFi re-enabled.' : ('Failed: ' + (result.detail || 'unknown error'));
        statusEl.className = r.ok ? 'save-status text-success' : 'save-status text-danger';
    } catch (e) {
        statusEl.textContent = 'Request failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

// The card is on screen from delay_secs to delay_secs + duration_secs
// after unlock. Anything reacting to that unlock can cut away before
// viewers see it - so cross-check the Companion unlock action here and
// say so, rather than silently rewriting a value the operator set
// deliberately. Note unlock_settle_secs is a DEBOUNCE ("has it really
// unlocked?"), not a hold-off, so lengthening it delays every unlock
// notification, not just the source switch. That is the operator's call
// to make knowingly, which is why the field stays editable.
function pathfinderWindow() {
    const d = parseFloat(document.getElementById('pf-delay').value) || 0;
    const u = parseFloat(document.getElementById('pf-duration').value) || 0;
    return d + u;
}

function updatePathfinderWarning() {
    const on = document.getElementById('pf-enabled').checked;
    const total = pathfinderWindow();
    const winEl = document.getElementById('pf-window');
    const warnEl = document.getElementById('pf-warning');
    const compWarnEl = document.getElementById('companion-pathfinder-warning');

    winEl.textContent = on
        ? `Card is on screen until ${total.toFixed(0)}s after the station stops.`
        : '';

    const hide = () => {
        if (warnEl) warnEl.style.display = 'none';
        if (compWarnEl) compWarnEl.style.display = 'none';
    };

    const settleEl = document.getElementById('companion-unlock-settle-input');
    const compEnabled = document.getElementById('companion-enabled-input');
    // No conflict worth flagging if the map is off, or if Companion
    // isn't enabled - nothing is firing on unlock to switch away.
    if (!on || !settleEl || !compEnabled || !compEnabled.checked) { hide(); return; }

    const settle = parseFloat(settleEl.value);
    if (isNaN(settle) || settle >= total) { hide(); return; }

    const btn = `<button type="button" class="btn btn-sm btn-outline-dark py-0 px-2 ms-1" ` +
                `onclick="matchCompanionSettle()">Match to ${total.toFixed(0)}s</button>`;

    // Shown in BOTH cards deliberately. The Station Map card is where
    // the timings were just changed; the Companion card is where the
    // field that needs changing actually lives, and someone editing
    // settle times may never scroll past it.
    if (warnEl) {
        warnEl.innerHTML =
            `&#9888; The Companion unlock action fires at <strong>${settle}s</strong>, ` +
            `before this card finishes at <strong>${total.toFixed(0)}s</strong>. ` +
            `If it switches the output away from Lynx, nobody will see the map. ` + btn;
        warnEl.style.display = 'block';
    }
    if (compWarnEl) {
        compWarnEl.innerHTML =
            `&#9888; Pathfinder shows a card until <strong>${total.toFixed(0)}s</strong> ` +
            `after unlock, but this action fires at <strong>${settle}s</strong>. ` +
            `If it switches the output away from Lynx, the map won't be seen. ` + btn;
        compWarnEl.style.display = 'block';
    }
}

function matchCompanionSettle() {
    const settleEl = document.getElementById('companion-unlock-settle-input');
    if (settleEl) {
        settleEl.value = pathfinderWindow().toFixed(0);
        settleEl.dispatchEvent(new Event('change'));
        updatePathfinderWarning();
        const st = document.getElementById('pf-status');
        if (st) {
            st.textContent = 'Companion settle updated - save the Companion section too.';
            st.className = 'save-status text-warning';
        }
        // Deliberately not saved for you: writing to another section on
        // your behalf is exactly the silent-change behaviour this design
        // set out to avoid.
        const cs = document.getElementById('companion-save-status');
        if (cs) {
            cs.textContent = 'Settle time changed - press Save Companion settings.';
            cs.className = 'save-status text-warning';
        }
    }
}

async function savePathfinder() {
    const statusEl = document.getElementById('pf-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const body = { pathfinder: {
            enabled: document.getElementById('pf-enabled').checked,
            delay_secs: parseFloat(document.getElementById('pf-delay').value),
            duration_secs: parseFloat(document.getElementById('pf-duration').value)
        }};
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        statusEl.textContent = 'Saved - restart required.';
        statusEl.className = 'save-status text-success';
        updatePathfinderWarning();
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function loadAudioDevices(current) {
    const sel = document.getElementById('audio-device-select');
    const note = document.getElementById('audio-device-resolved');
    if (!sel) return;
    try {
        const r = await fetch('/api/audio/devices');
        const d = await r.json();
        // Keep the two friendly options at the top, then whatever mpv
        // actually reports - asked of mpv rather than ALSA so the names
        // are exactly what --audio-device will accept.
        sel.length = 2;
        (d.devices || []).forEach(dev => {
            const o = document.createElement('option');
            o.value = dev.name;
            o.textContent = dev.description + '  (' + dev.name + ')';
            sel.appendChild(o);
        });
        sel.value = current;
        if (sel.value !== current) {
            // The saved device is no longer present - a dongle unplugged,
            // or a card renumbered. Say so rather than silently showing
            // something else as selected.
            const o = document.createElement('option');
            o.value = current;
            o.textContent = current + '  (not currently present)';
            sel.appendChild(o);
            sel.value = current;
        }
        if (note) {
            note.textContent = (current === 'hdmi' && d.resolved)
                ? 'Currently resolves to: ' + d.resolved
                : '';
        }
    } catch (e) {
        console.error('audio device list failed', e);
        if (note) note.textContent = 'Could not read the device list from mpv.';
    }
}

async function saveAudioDevice() {
    const st = document.getElementById('audio-save-status');
    st.textContent = 'Saving...';
    st.className = 'save-status text-muted';
    try {
        const body = { display: {
            ppm_style: document.querySelector('input[name="ppm-style"]:checked')?.value || 'full_fat',
            audio_device: document.getElementById('audio-device-select').value
        }};
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        st.textContent = 'Saved - takes effect when mpv next starts.';
        st.className = 'save-status text-success';
    } catch (e) {
        st.textContent = 'Save failed - see console.';
        st.className = 'save-status text-danger';
        console.error(e);
    }
}

async function savePpmStyle() {
    const statusEl = document.getElementById('ppm-style-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const style = document.querySelector('input[name="ppm-style"]:checked').value;
        // Both fields are sent together: the config save merges the
        // whole display block, so sending only one would drop the other.
        const body = { display: {
            ppm_style: style,
            audio_device: document.getElementById('audio-device-select')?.value || 'hdmi'
        }};
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        statusEl.textContent = 'Saved - applies within a few seconds.';
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
                lookup_username: document.getElementById('qrz-lookup-username-input').value.trim(),
                lookup_password: document.getElementById('qrz-lookup-password-input').value,
                lookup_for_notifications: document.getElementById('qrz-lookup-notif-input').checked,
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

function onGnssModeChange() {
    // In Automatic mode GPS is authoritative and will overwrite the
    // QRZ field on its own next confirmed fix, so it's disabled here
    // to avoid a typed edit that looks saved but is about to be
    // silently overwritten - and, while disabled, loadGnssStatus()
    // keeps its value live-updated to whatever GPS actually currently
    // reports, rather than leaving it showing a stale config snapshot
    // from page load. Manual leaves it a normal, editable field - the
    // operator's typed value is the whole point.
    const mode = document.querySelector('input[name="gnss-mode"]:checked')?.value || 'automatic';
    const qrzInput = document.getElementById('qrz-portable-locator-input');
    qrzInput.disabled = (mode === 'automatic');
    qrzInput.placeholder = mode === 'automatic'
        ? 'Driven by GPS - see GNSS card below'
        : 'e.g. IO91VG - leave blank for normal operation';
}

async function loadSqueakSources() {
    try {
        const r = await fetch('/api/audio/monitors').then(r => r.json());
        const sel = document.getElementById('squeak-source-select');
        sel.innerHTML = '';
        (r.monitors || []).forEach(m => {
            const o = document.createElement('option');
            o.value = m.name; o.textContent = m.description;
            sel.appendChild(o);
        });
        sel.value = r.current || '';
    } catch (e) { console.error(e); }
}

async function saveSqueak() {
    const el = document.getElementById('squeak-save-status');
    el.textContent = 'Saving...'; el.className = 'save-status text-muted';
    try {
        const body = {squeak: {
            enabled: document.getElementById('squeak-enabled-input').checked,
            source: document.getElementById('squeak-source-select').value,
            hold_secs: parseFloat(document.getElementById('squeak-hold-input').value) || 45
        }};
        const r = await fetch('/api/config', {method: 'POST',
            headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
        if (!r.ok) throw new Error(await r.text());
        // Enabling or changing the source needs a restart: the listener
        // holds an open reader on the audio monitor for the life of the
        // process. The display time alone applies to the next card.
        el.textContent = 'Saved - restart Lynx to start or stop listening.';
        el.className = 'save-status text-success';
    } catch (e) {
        el.textContent = 'Save failed - see console.';
        el.className = 'save-status text-danger';
        console.error(e);
    }
}

async function saveGnssMode() {
    const statusEl = document.getElementById('gnss-save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const mode = document.querySelector('input[name="gnss-mode"]:checked')?.value || 'automatic';
        const timeSync = document.getElementById('gnss-time-sync-input').checked;
        // Also submits the QRZ card's current field values, portable_locator
        // included - not just gnss.mode/time_sync. Without this, switching
        // to Manual and clearing the locator field looked saved (the field
        // itself went blank) but wasn't: this button only ever wrote the
        // gnss section, so GPS's last-written value stayed sitting in
        // notifications.qrz.portable_locator until "Save QRZ settings" was
        // ALSO clicked separately - confirmed as the actual cause of a
        // reported bug (a stale GPS locator kept appearing in the logbook
        // after switching to Manual, since nothing had told the backend
        // the field was now meant to be empty). One save now covers both.
        const body = {
            gnss: {mode: mode, time_sync: timeSync},
            notifications_qrz: {
                enabled: document.getElementById('qrz-enabled-input').checked,
                api_key: document.getElementById('qrz-api-key-input').value,
                settle_secs: parseFloat(document.getElementById('qrz-settle-input').value),
                suppress_mins: parseFloat(document.getElementById('qrz-suppress-input').value),
                portable_locator: document.getElementById('qrz-portable-locator-input').value.trim(),
                lookup_username: document.getElementById('qrz-lookup-username-input').value.trim(),
                lookup_password: document.getElementById('qrz-lookup-password-input').value,
                lookup_for_notifications: document.getElementById('qrz-lookup-notif-input').checked,
            }
        };
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        statusEl.textContent = 'Saved - applied immediately.';
        statusEl.className = 'save-status text-success';
        onGnssModeChange();
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

function setGnssHardwareAvailable(available) {
    // No module fitted (a repeater site, or the HAT not wired up yet)
    // gets the same treatment as the PicoTuner's own LNB-plug-absent
    // buttons: disabled and dimmed rather than left looking live, with
    // the reason moved onto a hover tooltip. pointerEvents is forced
    // back to 'auto' because a disabled control's own title attribute
    // doesn't reliably show on hover in every browser otherwise - the
    // same fix already proven there.
    //
    // Manual (gnss-mode-manual) is deliberately outside
    // #gnss-hw-dependent and never touched here - unlike Automatic
    // (which does nothing at all without a HAT actually answering),
    // Manual's whole behaviour is hardware-independent by definition:
    // it just means GPS doesn't touch the typed value, which is true
    // and useful whether or not a HAT exists. It stays the always-
    // selectable option, including to back out of Automatic if a HAT
    // stops answering.
    const why = 'No GNSS module detected on /dev/ttyAMA0 - fit the HAT ' +
                '(or check the serial connection) to use this mode.';
    document.getElementById('gnss-no-module-warning').style.display = available ? 'none' : 'block';

    const wrap = document.getElementById('gnss-hw-dependent');
    wrap.classList.toggle('gnss-unavailable', !available);

    for (const id of ['gnss-mode-automatic']) {
        const input = document.getElementById(id);
        input.disabled = !available;
        for (const el of [input, input.closest('.form-check')]) {
            if (!el) continue;
            if (!available) {
                if (!el.dataset.titleOrig) el.dataset.titleOrig = el.title || '';
                el.title = why;
                el.style.cursor = 'not-allowed';
                el.style.pointerEvents = 'auto';
            } else if (el.dataset.titleOrig !== undefined) {
                el.title = el.dataset.titleOrig;
                el.style.cursor = '';
            }
        }
    }
}

async function loadGnssStatus() {
    // Polls regardless of which mode is currently selected - the
    // no-module warning/greying needs to track real hardware state
    // continuously, not just while Automatic happens to be selected.
    try {
        const s = await fetch('/api/status').then(r => r.json());
        const g = s.lynx?.gnss || {};
        const noModule = g.running && !g.connected;
        setGnssHardwareAvailable(!noModule);

        document.getElementById('gnss-connected').textContent =
            noModule ? 'No GNSS Module' : (g.connected ? 'Connected' : 'Not connected');
        document.getElementById('gnss-locator-display').textContent = g.locator_display || g.locator || '—';
        const sats = g.satellites != null ? g.satellites : '—';
        const hdop = g.hdop != null ? g.hdop : '—';
        document.getElementById('gnss-quality').textContent = sats + ' / ' + hdop;
        const tsEl = document.getElementById('gnss-time-sync-status');
        if (!document.getElementById('gnss-time-sync-input').checked) {
            tsEl.textContent = 'Off';
        } else if (g.time_synced) {
            tsEl.textContent = 'Active';
        } else if (g.time_quality_ok) {
            tsEl.textContent = 'Unavailable on this receiver';
        } else {
            tsEl.textContent = 'Waiting for a fix';
        }
        let detail;
        if (noModule) {
            detail = 'No GNSS Module';
        } else if (!g.connected) {
            detail = g.last_error || 'Not yet connected';
        } else if (g.pending_secs != null) {
            detail = 'New square settling, ' + Math.ceil(g.pending_secs) + 's to confirm';
        } else if (g.locator) {
            detail = 'Confirmed fix';
        } else {
            detail = 'Waiting for a fix';
        }
        document.getElementById('gnss-detail').textContent = detail;

        // In Automatic mode the QRZ field is disabled (see
        // onGnssModeChange) and shows the live GPS value rather than
        // a stale config snapshot from whenever the page happened to
        // load - keeps it visibly in sync with whatever's actually
        // about to be logged, not just at load time.
        const currentMode = document.querySelector('input[name="gnss-mode"]:checked')?.value;
        if (currentMode === 'automatic') {
            document.getElementById('qrz-portable-locator-input').value = g.locator || '';
        }
    } catch (e) {
        console.error(e);
    }
}
setInterval(loadGnssStatus, 3000);

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

async function saveTriWatch() {
    const statusEl = document.getElementById('tw-save-status');
    statusEl.textContent = 'Saving...';
    statusEl.className = 'save-status text-muted';
    try {
        const body = {
            tri_watch: {
                enabled: document.getElementById('tw-enabled-input').checked,
                rx1: {
                    enabled: document.getElementById('tw-rx1-enabled-input').checked,
                    rcv: 1,
                    fplug: document.getElementById('tw-rx1-plug-input').value,
                    freq: parseInt(document.getElementById('tw-rx1-freq-input').value) || 0,
                    sr: parseInt(document.getElementById('tw-rx1-sr-input').value) || 0,
                    label: document.getElementById('tw-rx1-label-input').value,
                    callsign: document.getElementById('tw-rx1-callsign-input').value,
                },
                rx2: {
                    enabled: document.getElementById('tw-rx2-enabled-input').checked,
                    rcv: 2,
                    fplug: document.getElementById('tw-rx2-plug-input').value,
                    freq: parseInt(document.getElementById('tw-rx2-freq-input').value) || 0,
                    sr: parseInt(document.getElementById('tw-rx2-sr-input').value) || 0,
                    label: document.getElementById('tw-rx2-label-input').value,
                    callsign: document.getElementById('tw-rx2-callsign-input').value,
                },
                stream: {
                    enabled: document.getElementById('tw-stream-enabled-input').checked,
                    domain: document.getElementById('tw-stream-domain-input').value,
                    app: document.getElementById('tw-stream-app-input').value,
                    streamname: document.getElementById('tw-stream-name-input').value,
                    port: parseInt(document.getElementById('tw-stream-port-input').value) || 1935,
                    label: document.getElementById('tw-stream-label-input').value,
                    waiting_message: document.getElementById('tw-stream-waiting-message-input').value,
                },
            }
        };
        const r = await fetch('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
        if (!r.ok) throw new Error(await r.text());
        statusEl.textContent = 'Saved - restart Lynx to apply.';
        statusEl.className = 'save-status text-warning';
    } catch (e) {
        statusEl.textContent = 'Save failed - see console.';
        statusEl.className = 'save-status text-danger';
        console.error(e);
    }
}

async function loadDiscoveredPicotuners() {
    const listEl = document.getElementById('discovered-picotuners-list');
    try {
        const data = await fetch('/api/picotuner/discovered').then(r => r.json());
        const units = data.picotuners || [];
        if (units.length === 0) {
            listEl.innerHTML = '<p class="text-muted small mb-0">None heard yet - give it a few seconds.</p>';
            return;
        }
        listEl.innerHTML = units.map(u => `
            <div class="d-flex justify-content-between align-items-center mb-2 pb-2" style="border-bottom: 1px solid #333;">
                <div>
                    <div>${u.host_name || u.ip}</div>
                    <small class="text-muted">${u.ip} &middot; ${u.software || 'unknown firmware'}</small>
                </div>
                <button class="btn btn-outline-light btn-sm" onclick="document.getElementById('pt-host-input').value='${u.ip}'">Use</button>
            </div>
        `).join('');
    } catch (e) {
        listEl.innerHTML = '<p class="text-muted small mb-0">Could not check.</p>';
    }
}

loadCurrentConfig();
loadGnssStatus();
loadDiscoveredPicotuners();
setInterval(loadDiscoveredPicotuners, 5000);

// ── QuickLynx ────────────────────────────────────────────────
// Deliberately self-contained, with its own polling rather than a hook
// into an existing update function. An earlier attempt edited the middle
// of a large handler and left it broken, which took the whole page down
// with it. This way a fault here fails alone.
async function saveQuickLynx() {
    const st = document.getElementById('quicklynx-status');
    if (!st) return;
    st.textContent = 'Saving...';
    st.className = 'save-status text-muted';
    try {
        const r = await fetch('/api/config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ quicklynx: {
                enabled: document.getElementById('quicklynx-enabled').checked
            }})
        });
        if (!r.ok) throw new Error(await r.text());
        st.textContent = 'Saved.';
        st.className = 'save-status text-success';
    } catch (e) {
        st.textContent = 'Save failed - see console.';
        st.className = 'save-status text-danger';
        console.error('QuickLynx save failed', e);
    }
}

async function loadQuickLynxSetting() {
    try {
        const r = await fetch('/api/config');
        const cfg = await r.json();
        const el = document.getElementById('quicklynx-enabled');
        if (el) el.checked = !!(cfg.quicklynx && cfg.quicklynx.enabled);
    } catch (e) {
        console.error('QuickLynx setting load failed', e);
    }
}
document.addEventListener('DOMContentLoaded', loadQuickLynxSetting);

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
    """Send a UDP command to the Picotuner. Deliberately never raises -
    this is fire-and-forget UDP with no response or acknowledgement
    expected, so a caller shouldn't be able to fail just because this
    couldn't be sent. Confirmed directly (2026-07-31) that the config
    template's own placeholder host value ("192.168.0.XXX", set before
    the Config page is ever visited on a fresh install) isn't a valid
    IP, so Python tries to resolve it as a hostname via DNS - which
    fails with socket.gaierror. With no try/except around this call in
    _start_stream_impl(), that crashed the ENTIRE stream-start request,
    meaning network streams (RTMP/SRT/UDP/RTSP - architecturally
    unrelated to the Picotuner at all) couldn't be played until the
    Picotuner was configured, on a receiver some people run for
    streaming only. Returns True/False rather than raising, so callers
    that genuinely need to know (RF tuning) still can, via the
    Picotuner's own separate, already-visible "offline" status rather
    than a raw exception."""
    try:
        cfg = config['picotuner']
        host = cfg.get('host', '')
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(cmd.encode(), (host, cfg['cmd_port']))
        finally:
            sock.close()
        return True
    except Exception as e:
        print(f"[picotuner_cmd] could not send (Picotuner likely not configured/reachable): {e}")
        return False

def picotuner_rcv2_cmd(cmd: str, cfg: dict):
    """Same fire-and-forget, never-raises contract as picotuner_cmd()
    above - sends to Rx2's own, separate command port instead of Rx1's,
    since the Picotuner genuinely requires this (a single shared port
    with different rcv= values in the command text does NOT work,
    confirmed the hard way during diversity mode's own development).
    Takes cfg explicitly (config['picotuner']) rather than reading it
    itself, since callers already have it in scope."""
    try:
        host = cfg.get('host', '')
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.sendto(cmd.encode(), (host, cfg['cmd_port_b']))
        finally:
            sock.close()
        return True
    except Exception as e:
        print(f"[picotuner_rcv2_cmd] could not send (Picotuner likely not configured/reachable): {e}")
        return False

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
        if req.rcv == 2:
            # Same command format and port already confirmed working
            # via tri_watch's own startup-tuning and diversity mode's
            # own rcv=2 command - reused directly, not reimplemented.
            picotuner_rcv2_cmd(
                f"[to@wh] rcv=2 fplug={req.plug} offset=0 "
                f"freq={tuner_freq} srate={req.sr}",
                cfg
            )
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
            if not tri_watch_enabled:
                # Deliberately do NOT start mpv here. It's only started once
                # rf_mpv_lifecycle_monitor confirms a stable signal lock -
                # see that function's docstring for why. Just ensure nothing
                # from a previous tune/stream is left running against what
                # is now a stale target, and leave the cover up; the
                # overlay already shows the useful status/metadata (call-
                # sign, MER, modcod) during acquisition on its own.
                kill_mpv()
                mpv_running_for_rf = False
            # else: tri_watch is enabled. Confirmed as a genuine, serious
            # race condition otherwise: this unconditional kill_mpv(),
            # delayed by a fixed 1s, has no way to know whether a NEWLY,
            # correctly-started mpv process (from rf_mpv_lifecycle_monitor()'s
            # own tri_watch-aware branch, which can legitimately fire
            # within that same second - tri_watch's receivers are often
            # already continuously locked, unlike normal RF mode where a
            # genuinely fresh lock confirmation always takes at least
            # LOCK_CONFIRM_POLLS seconds after any tune) is what's
            # actually running by the time this delay elapses, and would
            # kill it just as readily as a genuinely stale one - this was
            # confirmed as the actual reason Rx2 never showed video even
            # after the earlier mpv_running_for_rf fix, which addressed a
            # real but different problem and didn't touch this one at
            # all. tri_watch's own caller (_tri_watch_display_source())
            # already resets mpv_running_for_rf itself, synchronously,
            # and restart_mpv() (called by rf_mpv_lifecycle_monitor()'s
            # own tri_watch-aware branch) already kills any existing
            # process immediately before starting the new one - nothing
            # further is needed or safe to do here in that case.
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
    #
    # Skipped entirely when tri-watch is enabled (2026-08-01, renamed
    # from an earlier "dual-watch") - the whole point of that feature is
    # keeping the Picotuner genuinely tuned and monitored continuously,
    # regardless of what's currently being displayed, so real RF
    # activity on a configured repeater frequency is never missed just
    # because the stream happens to be on screen right now. The
    # RF/network bandwidth this normally frees up is a deliberate,
    # accepted trade-off for anyone who's turned this on.
    if not tri_watch_enabled:
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
                   "isn't currently configured - the Pi was NOT rebooted. Fix (run "
                   "on the Pi over SSH):\n\n"
                   'echo "$USER ALL=(ALL) NOPASSWD: /sbin/reboot, /usr/sbin/reboot" '
                   "| sudo tee /etc/sudoers.d/lynx-reboot\n"
                   "sudo chmod 0440 /etc/sudoers.d/lynx-reboot\n"
                   "sudo visudo -c\n\n"
                   "Or reboot manually over SSH instead.")

    def _do_reboot():
        time.sleep(1.0)  # let this HTTP response actually reach the browser first
        subprocess.Popen(["sudo", "reboot"])
    threading.Thread(target=_do_reboot, daemon=True).start()
    return {"success": True, "message": "Rebooting the Pi - back in about a minute"}

@app.post("/api/shutdown", tags=["Control"],
          summary="Shut down (power off) the Pi",
          description="Gracefully stops current reception, then powers off "
                      "the entire Raspberry Pi via 'sudo shutdown -h now'. "
                      "Unlike Reboot, the Pi does NOT come back up on its "
                      "own afterwards - it needs power physically cycled "
                      "(or a remote power switch) to bring the receiver "
                      "back. Same passwordless-sudo requirement as Reboot, "
                      "checked synchronously before responding for the same "
                      "reason: a fire-and-forget shutdown can't report back "
                      "if it's actually going to work.")
def shutdown_pi():
    check = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
    if check.returncode != 0:
        raise HTTPException(status_code=500,
            detail="'sudo shutdown' requires passwordless sudo for this user, which "
                   "isn't currently configured - the Pi was NOT shut down. Fix (run "
                   "on the Pi over SSH):\n\n"
                   'echo "$USER ALL=(ALL) NOPASSWD: /sbin/shutdown, /sbin/poweroff" '
                   "| sudo tee /etc/sudoers.d/lynx-shutdown\n"
                   "sudo chmod 0440 /etc/sudoers.d/lynx-shutdown\n"
                   "sudo visudo -c\n\n"
                   "Or shut down manually over SSH instead.")

    stop_current()  # graceful cover-up + mpv stop first, same as the Stop button

    def _do_shutdown():
        time.sleep(1.5)  # let this HTTP response actually reach the browser first
        subprocess.Popen(["sudo", "shutdown", "-h", "now"])
    threading.Thread(target=_do_shutdown, daemon=True).start()
    return {"success": True, "message": "Shutting down - the Pi will power off shortly and will NOT restart on its own."}

@app.post("/api/app_stop", tags=["Control"],
          summary="Stop the Lynx app, back to the desktop",
          description="Gracefully stops current reception, then closes the "
                      "Lynx app itself (and its overlay) entirely, leaving "
                      "the Pi running and returning to its desktop - unlike "
                      "Stop, which only stops reception, and unlike Reboot/"
                      "Shutdown, which restart or power off the whole Pi. "
                      "Kills by process name rather than a tracked PID, "
                      "since the overlay is started independently of this "
                      "app rather than launched by it - works regardless of "
                      "how both were originally started. NOTE: if something "
                      "is separately supervising these processes with "
                      "auto-restart (e.g. a systemd service configured with "
                      "Restart=always), they may simply relaunch a moment "
                      "later - that would need stopping at the supervisor "
                      "level instead, not from here.")
def app_stop():
    stop_current()  # graceful cover-up + mpv stop first

    def _do_app_stop():
        time.sleep(1.0)  # let this HTTP response actually reach the browser first
        subprocess.run(["pkill", "-f", "lynx_overlay.py"])
        subprocess.run(["pkill", "-f", "lynx_app.py"])
        os._exit(0)  # hard exit - simpler and more reliable here than trying
                      # to gracefully unwind uvicorn's own event loop for what
                      # is, by design, an abrupt full stop
    threading.Thread(target=_do_app_stop, daemon=True).start()
    return {"success": True, "message": "Stopping Lynx - returning to the desktop."}

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

class LnbPsuRequest(BaseModel):
    plug: str                       # "a" or "b"
    voltage: Optional[str] = None   # "off"/"lo"/"hi" - omit to leave voltage unchanged
    tone: Optional[bool] = None     # Hi-Band LO tone - omit to leave tone unchanged

@app.get("/api/lnb_psu", tags=["Control"],
         summary="Get the current LNB PSU voltage/tone for both plugs",
         description="Returns the Picotuner's own last-reported LNB "
                     "supply state for each plug (voltage + Hi-Band "
                     "tone), parsed from its status broadcast - falls "
                     "back to the last-commanded value if that "
                     "broadcast field is ever briefly unavailable.")
def get_lnb_psu():
    return {
        "plug_a": current_lnb_psu_a, "plug_a_tone": current_lnb_tone_a,
        "plug_b": current_lnb_psu_b, "plug_b_tone": current_lnb_tone_b,
    }

@app.post("/api/lnb_psu", tags=["Control"],
          summary="Set the LNB PSU voltage and/or tone for one plug",
          description="voltage: off/lo (13V, Vertical)/hi (18V, "
                      "Horizontal - the correct setting for Amateur "
                      "TV). tone: Hi-Band LO (almost never needed for "
                      "Amateur TV - default is Lo-Band/off). Either "
                      "can be omitted to change only the other, "
                      "leaving the current value of whichever is "
                      "omitted untouched. Combined into the "
                      "Picotuner's own single VGX=/VGY= command value "
                      "(off/lo/hi/lot/hit) and sent immediately, "
                      "independent of any tune command - deliberately "
                      "persistent rather than tied to a preset, since "
                      "an LNB is normally meant to stay powered "
                      "continuously once connected. Also saved to "
                      "config so it's correctly re-applied on every "
                      "future Lynx startup, since the Picotuner's own "
                      "remote settings aren't guaranteed to survive "
                      "its own power cycle. Toggling tone while "
                      "voltage is currently off only updates the "
                      "stored preference - there's no voltage to "
                      "apply a tone to yet.")
def set_lnb_psu(req: LnbPsuRequest):
    global current_lnb_psu_a, current_lnb_psu_b, current_lnb_tone_a, current_lnb_tone_b
    plug = req.plug.lower()
    if plug not in ("a", "b"):
        return {"success": False, "error": "plug must be 'a' or 'b'"}

    cur_voltage = current_lnb_psu_a if plug == "a" else current_lnb_psu_b
    cur_tone = current_lnb_tone_a if plug == "a" else current_lnb_tone_b
    new_voltage = req.voltage.lower() if req.voltage is not None else cur_voltage
    new_tone = req.tone if req.tone is not None else cur_tone
    if new_voltage not in ("off", "lo", "hi"):
        return {"success": False, "error": "voltage must be 'off'/'lo'/'hi'"}

    # "off" always means genuinely off, regardless of the stored tone
    # preference - tone is only meaningful once voltage is actually on.
    combined = new_voltage if new_voltage == "off" else (new_voltage + ("t" if new_tone else ""))

    if plug == "a":
        picotuner_cmd(f"[to@wh] vgx={combined}")
        current_lnb_psu_a, current_lnb_tone_a = new_voltage, new_tone
    else:
        picotuner_rcv2_cmd(f"[to@wh] vgy={combined}", config['picotuner'])
        current_lnb_psu_b, current_lnb_tone_b = new_voltage, new_tone

    config.setdefault('lnb_psu', {})[f'plug_{plug}'] = new_voltage
    config.setdefault('lnb_psu', {})[f'plug_{plug}_tone'] = new_tone
    save_config(config)
    return {"success": True, "plug": plug, "voltage": new_voltage, "tone": new_tone}

@app.get("/api/update/status", tags=["Configuration"],
         summary="Current version and update-check status",
         description="Populates current_version on first call (local-"
                     "only, no network needed) if not already known. "
                     "Does NOT itself check for updates - that stays "
                     "fully manual (see POST /api/update/check) and "
                     "never runs automatically, since not every Lynx "
                     "receiver is reliably online. current_version is "
                     "'unknown' only if git itself isn't available or "
                     "this isn't a git checkout at all.")
def get_update_status():
    ensure_current_version()
    return update_state

@app.post("/api/update/check", tags=["Configuration"],
          summary="Check for updates now",
          description="Safe and read-only - runs git fetch and compares "
                      "against the remote. This is the ONLY way a check "
                      "ever happens - deliberately no automatic/"
                      "background checking, since not every Lynx "
                      "receiver is reliably online and this must never "
                      "touch the network on its own. Never applies "
                      "anything.")
def post_update_check():
    check_for_updates()
    return update_state

@app.post("/api/update/apply", tags=["Configuration"],
          summary="Pull the latest code and reboot",
          description="Fails safely: if git pull fails for any reason "
                      "(no network, local changes conflicting, etc), "
                      "this returns an error and nothing is touched - "
                      "no reboot is triggered unless the pull itself "
                      "succeeded cleanly first. Reboots the whole Pi "
                      "(same mechanism as the Reboot button) rather than "
                      "just restarting the Lynx process - deliberately "
                      "chosen (2026-07-31) over relying on systemd's "
                      "Restart=always, since the lynx.service unit's "
                      "auto-start currently has a known, unresolved issue "
                      "on labwc (graphical-session.target never "
                      "activating). A full reboot reliably brings Lynx "
                      "back up regardless via the proven autostart line, "
                      "at the cost of being slower than a simple process "
                      "restart would be.")
def post_update_apply():
    branch = get_update_branch()

    # Defensive: git pull merges into whatever's CURRENTLY checked out
    # locally, not necessarily the branch actually named in the
    # command below - if the local checkout ever drifted out of sync
    # with the configured channel (shouldn't normally happen, since
    # POST /api/update/channel always does an explicit checkout
    # whenever the channel itself changes, but worth guarding against
    # regardless given the stakes: a mismatched pull here could
    # silently merge beta content into what's meant to be a stable
    # checkout, or vice versa), self-heal by checking out the correct
    # branch first rather than trusting it's already right.
    ok, current_branch = git_cmd("branch", "--show-current")
    if ok and current_branch and current_branch != branch:
        print(f"[update] local branch '{current_branch}' doesn't match configured "
              f"channel's branch '{branch}' - checking out the correct one first")
        ok, err = git_cmd("checkout", branch)
        if not ok:
            raise HTTPException(status_code=502,
                detail=f"Update failed: local checkout ('{current_branch}') didn't match "
                       f"the configured channel ('{branch}'), and switching to it failed: {err}")

    ok, pull_output = git_cmd("pull", "--ff-only", "origin", branch)
    if not ok:
        raise HTTPException(status_code=502,
            detail=f"Update failed, nothing was changed: {pull_output}")

    # Refresh state immediately so a client that doesn't reload fast
    # enough after the reboot at least sees the truth if it asks again.
    update_state["update_available"] = False
    update_state["commits_behind"] = 0
    update_state["new_commits"] = []

    # Same passwordless-sudo check as the Reboot button, and for the
    # same reason: a fire-and-forget reboot can't report back if it
    # silently fails to actually happen, so check first rather than
    # claim success either way. The pull has already succeeded by this
    # point though, so a failed check here still leaves the code
    # genuinely updated - just not yet running - which the error
    # message below says explicitly, rather than leaving that unclear.
    check = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
    if check.returncode != 0:
        raise HTTPException(status_code=500,
            detail="Code pulled successfully, but couldn't reboot automatically - "
                   "passwordless sudo isn't configured for this user. The update "
                   "IS applied; reboot the Pi manually now, or fix this permanently "
                   "(run on the Pi over SSH):\n\n"
                   'echo "$USER ALL=(ALL) NOPASSWD: /sbin/reboot, /usr/sbin/reboot" '
                   "| sudo tee /etc/sudoers.d/lynx-reboot\n"
                   "sudo chmod 0440 /etc/sudoers.d/lynx-reboot\n"
                   "sudo visudo -c")

    def _do_reboot():
        time.sleep(1.0)  # let this HTTP response actually reach the browser first
        # OS packages, AND every one of Lynx's own apt/pip
        # dependencies, are (re-)confirmed here too, not just Lynx's
        # own code - see this function's own patch history for the
        # full rationale. Delegates to install.sh's own "--deps-only"
        # mode rather than duplicating its dependency lists here -
        # install.sh is itself pulled fresh as part of this same
        # update, so this always runs whatever the current, correct
        # list actually is, with only one place that list is ever
        # maintained. Non-interactive under the hood (install.sh's
        # own apt calls), so this can never hang on a prompt that
        # will never come; a bounded timeout so even something going
        # wrong here still lets the reboot below proceed rather than
        # hanging indefinitely.
        try:
            install_script = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "install.sh")
            env = os.environ.copy()
            env["DEBIAN_FRONTEND"] = "noninteractive"
            subprocess.run(["bash", install_script, "--deps-only"],
                            env=env, timeout=1200)
        except Exception as e:
            print(f"[update] Dependency check failed or timed out ({e}) - "
                  "rebooting anyway with whatever succeeded so far.")
        subprocess.Popen(["sudo", "reboot"])
    threading.Thread(target=_do_reboot, daemon=True).start()

    return {"result": "ok",
            "message": "Update pulled successfully - now updating OS packages and rebooting (this can take a few minutes).",
            "pull_output": pull_output}

class UpdateChannelRequest(BaseModel):
    channel: str   # 'stable' or 'beta'

@app.post("/api/update/channel", tags=["Configuration"],
          summary="Switch update channel (stable/beta) and reboot",
          description="Per Justin's own request, mirroring BATC Portsdown's "
                      "existing beta-channel model: lets Lynx track a "
                      "separate 'beta' branch of newer, less-proven code "
                      "instead of the normal stable one, for anyone who "
                      "wants to experiment - switchable back to stable at "
                      "any time. Unlike a normal update (POST /api/update/"
                      "apply, which pulls within whatever branch is "
                      "already checked out), switching channels means "
                      "actually checking out a DIFFERENT branch entirely - "
                      "git checkout, not git pull. Fails safely: if the "
                      "fetch or checkout fails for any reason, this "
                      "returns an error and nothing is touched - no "
                      "reboot is triggered unless the switch itself "
                      "succeeded cleanly first.")
def post_update_channel(req: UpdateChannelRequest):
    global config
    if req.channel not in ("stable", "beta"):
        raise HTTPException(status_code=400,
            detail="channel must be 'stable' or 'beta'")

    target_branch = "beta" if req.channel == "beta" else get_default_branch()

    # Make sure the remote branch's latest content is actually known
    # locally before trying to check it out - a plain `git checkout
    # -B x origin/x` against a stale/never-fetched remote ref would
    # otherwise silently land on old content rather than genuinely
    # failing, which would be a much more confusing failure mode than
    # this fetch simply erroring out cleanly if the branch doesn't
    # exist or the network's unreachable.
    ok, err = git_cmd("fetch", "origin", target_branch)
    if not ok:
        raise HTTPException(status_code=502,
            detail=f"Could not fetch the '{target_branch}' branch: {err}")

    # -B creates the local branch if it doesn't exist yet (first time
    # ever switching to beta) or forcibly resets it to exactly match
    # the remote if it does (switching back again later, possibly
    # after diverging locally somehow) - one command handles both
    # cases identically rather than needing to detect which applies.
    ok, err = git_cmd("checkout", "-B", target_branch, f"origin/{target_branch}")
    if not ok:
        raise HTTPException(status_code=502,
            detail=f"Could not switch to the '{target_branch}' branch: {err}")

    # Persist the choice - same read-fresh-from-disk, write-via-tmp-
    # then-replace pattern as every other config write in this file,
    # now under the same shared lock as all of them (see
    # _config_write_lock's own comment by CONFIG_PATH).
    with _config_write_lock:
        with open(CONFIG_PATH) as f:
            on_disk = yaml.safe_load(f)
        on_disk.setdefault('update', {})['channel'] = req.channel
        tmp_path = str(CONFIG_PATH) + ".tmp"
        with open(tmp_path, 'w') as f:
            yaml.safe_dump(on_disk, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, CONFIG_PATH)
        config = on_disk

    # Refresh state immediately, same reasoning as post_update_apply()
    # above - a client that doesn't reload fast enough after the
    # reboot at least sees the truth if it asks again.
    update_state["current_version"] = detect_current_version()
    update_state["channel"] = req.channel
    update_state["update_available"] = False
    update_state["commits_behind"] = 0
    update_state["new_commits"] = []

    # Same passwordless-sudo check, and for the same reason, as Reboot/
    # update-apply above: a fire-and-forget reboot can't report back
    # if it silently fails to actually happen. The channel switch has
    # already succeeded by this point though, so a failed check here
    # still leaves the code genuinely on the new channel - just not
    # yet running - which the error message says explicitly.
    check = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
    if check.returncode != 0:
        raise HTTPException(status_code=500,
            detail=f"Switched to the '{req.channel}' channel successfully, but couldn't "
                   "reboot automatically - passwordless sudo isn't configured for this "
                   "user. The switch IS applied; reboot the Pi manually now, or fix this "
                   "permanently (run on the Pi over SSH):\n\n"
                   'echo "$USER ALL=(ALL) NOPASSWD: /sbin/reboot, /usr/sbin/reboot" '
                   "| sudo tee /etc/sudoers.d/lynx-reboot\n"
                   "sudo chmod 0440 /etc/sudoers.d/lynx-reboot\n"
                   "sudo visudo -c")

    def _do_reboot():
        time.sleep(1.0)  # let this HTTP response actually reach the browser first
        subprocess.Popen(["sudo", "reboot"])
    threading.Thread(target=_do_reboot, daemon=True).start()

    return {"result": "ok", "channel": req.channel,
            "message": f"Switched to the '{req.channel}' channel - rebooting the Pi now."}

@app.post("/api/wifi/kill", tags=["Control"],
          summary="Disable WiFi entirely (rfkill block)",
          description="For sites where WiFi is causing problems "
                      "(power-save driver bugs, or roaming onto a "
                      "second saved network and breaking the "
                      "Picotuner/Knobler's local UDP broadcast "
                      "discovery) - disables the WiFi radio "
                      "completely via rfkill. Does NOT touch wired "
                      "Ethernet. WARNING: if this Pi is only reachable "
                      "over WiFi, this will cut off Web UI access "
                      "until WiFi is re-enabled locally or via SSH.")
def kill_wifi():
    check = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
    if check.returncode != 0:
        raise HTTPException(status_code=500,
            detail="Couldn't disable WiFi - passwordless sudo isn't configured for this user.")
    result = subprocess.run(["sudo", "rfkill", "block", "wifi"],
                             capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"Failed to disable WiFi: {result.stderr}")
    return {"result": "ok", "message": "WiFi disabled."}

@app.post("/api/wifi/restore", tags=["Control"],
          summary="Re-enable WiFi (rfkill unblock)",
          description="Reverses Kill WiFi.")
def restore_wifi():
    check = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5)
    if check.returncode != 0:
        raise HTTPException(status_code=500,
            detail="Couldn't re-enable WiFi - passwordless sudo isn't configured for this user.")
    result = subprocess.run(["sudo", "rfkill", "unblock", "wifi"],
                             capture_output=True, text=True, timeout=5)
    if result.returncode != 0:
        raise HTTPException(status_code=502, detail=f"Failed to re-enable WiFi: {result.stderr}")
    return {"result": "ok", "message": "WiFi re-enabled."}

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
    # Everything below is one atomic read-modify-write-replace cycle,
    # held under the shared _config_write_lock (see its own comment by
    # CONFIG_PATH) - without this, a GNSS confirmed-fix write landing
    # mid-way through this handler could either corrupt the shared tmp
    # file both writers use, or silently discard whichever save
    # completed second. Confirmed as a real, reported failure: QRZ
    # logging stopped entirely after GNSS and a Config page save
    # happened to land close together during testing.
    with _config_write_lock:
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

        # Display: takes effect live, no restart - the overlay picks this
        # up on its own next /api/status poll (a few seconds), same as
        # portable_locator and the notification settings above.
        if req.quicklynx is not None:
            on_disk.setdefault('quicklynx', {}).update(req.quicklynx.model_dump())

        if req.display is not None:
            on_disk.setdefault('display', {}).update(req.display.model_dump())

        # pathfinder: merges only the three fields the form sends, leaving
        # min_span_km/max_span_km/max_distance_km untouched - the form never
        # sees them, and a blind update() would wipe them.
        if req.pathfinder is not None:
            on_disk.setdefault('pathfinder', {}).update(req.pathfinder.model_dump())

        # tri_watch: rebuilds just the `sources` list and top-level
        # `enabled` flag from whichever of Rx1/Rx2/stream are actually
        # enabled in the submitted form - every other tri_watch field
        # (settling_seconds, lock_confirm_seconds, notification_duration_secs,
        # rf_notification_template) is left completely untouched, since
        # this form never sees or sends them. Always needs a restart to
        # take effect - tri_watch's own probes/arbitrator are set up once
        # at process start from this exact list, unlike most other
        # sections here which are re-read live.
        tri_watch_changed = req.tri_watch is not None
        if req.tri_watch is not None:
            new_sources = []
            # Every existing tri_watch reader (get_tri_watch_status, the
            # arbitrator loop, startup tune, the port drainer sync) checks
            # src_cfg.get('enabled', False) on each individual source dict
            # - defaulting to False if that key is absent entirely. This
            # form only ever appends a source here when its own "Include"
            # checkbox was on, so it's always enabled - but that must be
            # written explicitly as its own field, not left implied by
            # presence in the list, or every single saved source would
            # silently fail that check and disappear, not just an
            # intentionally-excluded one. Confirmed as a real, reported bug
            # otherwise: a save that only meant to drop Rx2 dropped Rx1 and
            # the stream too, since neither carried this field either.
            if req.tri_watch.rx1.enabled:
                r = req.tri_watch.rx1
                new_sources.append({
                    "type": "rf", "rcv": 1, "fplug": r.fplug, "freq": r.freq, "sr": r.sr,
                    "lnb_lo_khz": r.lnb_lo_khz, "label": r.label, "callsign": r.callsign,
                    "enabled": True,
                })
            if req.tri_watch.rx2.enabled:
                r = req.tri_watch.rx2
                new_sources.append({
                    "type": "rf", "rcv": 2, "fplug": r.fplug, "freq": r.freq, "sr": r.sr,
                    "lnb_lo_khz": r.lnb_lo_khz, "label": r.label, "callsign": r.callsign,
                    "enabled": True,
                })
            if req.tri_watch.stream.enabled:
                s = req.tri_watch.stream
                new_sources.append({
                    "type": "stream", "domain": s.domain, "app": s.app,
                    "streamname": s.streamname, "port": s.port, "label": s.label,
                    "waiting_message": s.waiting_message, "enabled": True,
                })
            on_disk.setdefault('tri_watch', {})['enabled'] = req.tri_watch.enabled
            on_disk.setdefault('tri_watch', {})['sources'] = new_sources

        # GNSS: no restart needed either - _apply_gnss_mode() below
        # applies time_sync live, immediately after config is
        # reassigned, the same "takes effect right away" pattern as
        # site. Mode itself needs no live-apply step at all: it only
        # gates whether _on_gnss_locator_change writes on the next
        # confirmed fix, which reads config fresh every time anyway.
        if req.gnss is not None:
            on_disk.setdefault('gnss', {}).update(req.gnss.model_dump())
        # Auto-Squeak: enabling or changing the source needs a restart,
        # because the listener holds an open ffmpeg reader on the audio
        # monitor for the life of the process. hold_secs alone is read
        # live and takes effect on the next card.
        if req.squeak is not None:
            on_disk.setdefault('squeak', {}).update(req.squeak.model_dump())

        tmp_path = str(CONFIG_PATH) + ".tmp"
        with open(tmp_path, 'w') as f:
            yaml.safe_dump(on_disk, f, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, CONFIG_PATH)

        config = on_disk  # reload in-memory immediately - site fields take effect right away
        _apply_gnss_mode()
        return {
            "success": True,
            "restart_required": picotuner_changed or diversity_changed or tri_watch_changed,
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
            <div class="d-flex gap-2 mb-2">
                <a href="/diagnostics" class="btn btn-sm btn-outline-light">&#x1F4CA; Diagnostics</a>
                <a href="/config" class="btn btn-sm btn-outline-light">&#x2699;&#xFE0F; Config</a>
                <a href="/docs" class="btn btn-sm btn-outline-light">&#x1F4D6; API Docs</a>
                <!-- Rendered disabled rather than hidden when switched off:
                     a button that simply is not there teaches nobody the
                     feature exists. Greyed with an explanation on hover does. -->
                <a href="/quicklynx" target="_blank" rel="noopener"
                   id="quicklynx-btn"
                   style="pointer-events: auto; cursor: help;"
                   class="btn btn-sm btn-outline-light disabled"
                   aria-disabled="true"
                   title="QuickLynx is switched off. It shows the QO-100 wideband spectrum and lets you click a signal to tune this receiver to it. Turn it on in Config to use it.">&#x1F4E1; QuickLynx</a>
            </div>
            <small class="text-muted" id="site-name"></small>
            <!-- Diversity: replaces the site-name line above with a highlighted stats box while diversity mode is active -->
            <div id="diversity-stats-line" style="display:none; background:#0f3460; color:#ffffff; font-weight:500; padding: 4px 10px; border-radius: 4px; font-size: 1rem;"></div>
            <!-- Tri-watch (Stage 1): shows status for every currently-enabled source (any mix of RF-A/RF-B/stream), only when enabled in config. Element id kept as "dual-watch-line" from the earlier, narrower design - purely cosmetic, no need to rename it -->
            <div id="dual-watch-line" style="display:none; background:#3a2a5c; color:#ffffff; font-weight:500; padding: 4px 10px; border-radius: 4px; font-size: 1rem;"></div>
        </div>
        <div class="col-auto d-flex flex-wrap align-items-start gap-2 pt-1">
            <span><span class="led led-grey" id="picotuner-led"></span><small id="picotuner-status" class="text-muted">Picotuner</small></span>
            <span class="d-flex align-items-center gap-1" title="LNB PSU, Plug A - press the active button again to turn off. Amateur TV is always Horizontal (18V), labelled by voltage since some LNBs are physically mounted rotated 90 degrees.">
                <small class="text-muted">LNB&nbsp;A</small>
                <button class="btn btn-sm" id="lnb-psu-a-h" onclick="onLnbPsuClick('a','hi')" title="18V (Horizontal, for Amateur TV)">18V</button>
                <button class="btn btn-sm" id="lnb-psu-a-v" onclick="onLnbPsuClick('a','lo')" title="13V (Vertical)">13V</button>
                <button class="btn btn-sm" id="lnb-psu-a-tone" onclick="onLnbToneClick('a')" title="Hi-Band LO (22kHz tone) - almost never needed for Amateur TV, default is Lo-Band">Tone</button>
            </span>
            <span class="d-flex align-items-center gap-1" title="LNB PSU, Plug B - press the active button again to turn off. Amateur TV is always Horizontal (18V), labelled by voltage since some LNBs are physically mounted rotated 90 degrees.">
                <small class="text-muted">LNB&nbsp;B</small>
                <button class="btn btn-sm" id="lnb-psu-b-h" onclick="onLnbPsuClick('b','hi')" title="18V (Horizontal, for Amateur TV)">18V</button>
                <button class="btn btn-sm" id="lnb-psu-b-v" onclick="onLnbPsuClick('b','lo')" title="13V (Vertical)">13V</button>
                <button class="btn btn-sm" id="lnb-psu-b-tone" onclick="onLnbToneClick('b')" title="Hi-Band LO (22kHz tone) - almost never needed for Amateur TV, default is Lo-Band">Tone</button>
            </span>
            <span class="btn btn-sm" id="mode-badge" style="background:#3a4a63; color:#fff; cursor:default;">IDLE</span>
            <a href="/config" class="btn btn-sm" id="locator-badge" style="background:#3a4a63; color:#fff;" title="Portable locator">&#x1F4CD; —</a>
            <a href="/diagnostics" class="btn btn-sm btn-outline-light" title="mpv restart/stop diagnostics" id="diagnostics-link">mpv: <span id="mpv-restart-count">0</span></a>
            <span class="btn btn-sm" id="version-badge" style="background:#3a4a63; color:#fff; cursor:default;" title="Current version">v?</span>
            <button class="btn btn-sm btn-outline-light" onclick="checkForUpdates()" id="update-check-btn" title="Check for updates now">&#x1F504; Check Updates</button>
            <button class="btn btn-sm btn-success" onclick="applyUpdate()" id="update-apply-btn" style="display:none" title="Pull the latest code and restart">&#x2B06;&#xFE0F; Update</button>
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
            <!-- tri_watch: the active stream's own info, shown independently
                 of Rx1's panel above rather than sharing its slot - confirmed
                 as a real, reported bug otherwise: Rx1 stays continuously,
                 independently tuned in the background under tri_watch
                 regardless of what's actually being displayed, but the main
                 panel above used to switch entirely to stream info whenever
                 a stream was on screen, hiding Rx1's own status completely
                 until whichever one "came up first" lost that slot again. -->
            <div class="card mt-2" id="tri-watch-stream-panel" style="display:none">
                <div class="card-header">&#x1F4FA; Stream</div>
                <div class="card-body" id="tri-watch-stream-status"></div>
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
                    <h6 class="text-muted">Manual Tune (kHz)</h6>
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
                                <option value="9000000">Ku 9000 MHz (QO-100, 9-10GHz mod. LNB)</option>
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
                        <button class="btn btn-dark flex-fill" onclick="shutdownPi()">&#x23FB; Shutdown</button>
                        <button class="btn btn-danger flex-fill" onclick="stopApp()">&#x23F9; Stop</button>
                        <button class="btn btn-warning flex-fill" onclick="restartLynx()">&#x1F504; Reboot</button>
                    </div>
                    <hr>
                    <h6 class="text-muted">Volume</h6>
                    <div class="d-flex align-items-center gap-2 mb-1">
                        <span style="font-size:1.1em">&#x1F50A;</span>
                        <input type="range" class="form-range flex-grow-1" id="volume-slider"
                               min="-60" max="0" step="0.5" value="0" title="These go to eleven"
                               oninput="onVolumeInput(this.value)" onchange="setVolume(this.value)">
                        <span class="status-value" id="volume-value" style="min-width:9em; text-align:right;">+0.0 dB (11.0/11)</span>
                    </div>
                    <div class="text-muted small mb-2">
                        0 dB = unity gain (11) — correct reference for
                        24-bit sources already mastered to EBU R128
                        (normal programme level ≈ -18 dBFS). dB shown
                        relative to 24-bit full scale, in 0.5 dB steps -
                        mpv's own volume scale is cubic, not linear, so
                        percent alone doesn't correspond evenly to
                        level. The (x/11) alongside it is exactly what
                        it looks like.
                    </div>
                    <div class="text-muted small mb-2" style="color:#d98a1e !important">
                        &#x26A0;&#xFE0F; The OSD's PPM meter is only correctly
                        calibrated at 0dB (11) - it reads mpv's actual
                        output after this gain is applied, and doesn't
                        currently compensate for it.
                    </div>
                    <div class="input-group input-group-sm mb-2">
                        <span class="input-group-text bg-dark text-light border-secondary" style="font-size:0.8em">Default on boot</span>
                        <input type="number" class="form-control bg-dark text-light border-secondary"
                               id="default-volume-input" min="-60" max="0" step="0.5" value="0"
                               oninput="onDefaultVolumeInput()">
                        <span class="input-group-text bg-dark text-light border-secondary" id="default-volume-eleven" style="font-size:0.8em; min-width:5em;">(11.0/11)</span>
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
function renderLnbPsuButtons(lnbPsu) {
    // Green = on, grey = off - reads the Picotuner's own last-reported
    // state from /api/status (parsed from its real status broadcast,
    // not just "a command was last sent" - see this feature's own
    // backend comments for the full rationale) so this stays correct
    // across a page reload, a second browser tab, or a change made
    // some other way entirely (direct API call, pre-existing
    // Picotuner configuration), not just within one session's own
    // in-memory state.
    const lnbPsuState = lnbPsu || {plug_a: 'off', plug_a_tone: false, plug_b: 'off', plug_b_tone: false};
    for (const plug of ['a', 'b']) {
        const current = lnbPsuState['plug_' + plug] || 'off';
        const tone = lnbPsuState['plug_' + plug + '_tone'] || false;
        const hBtn = document.getElementById('lnb-psu-' + plug + '-h');
        const vBtn = document.getElementById('lnb-psu-' + plug + '-v');
        const toneBtn = document.getElementById('lnb-psu-' + plug + '-tone');

        // "absent" means the Picotuner has no voltage generator fitted
        // on this plug - normally the case for plug B, since only one
        // is populated as standard and a second has to be added by
        // hand. Disable rather than leave the buttons looking live:
        // clicking them turned the button green for one poll and then
        // reverted, which looked like a fault in Lynx rather than
        // hardware that simply isn't there.
        const absent = (current === 'absent');
        const why = absent
            ? 'No LNB voltage generator is fitted on plug ' + plug.toUpperCase() +
              '. The PicoTuner has only one as standard (plug A); a second ' +
              'can be added by hand - see the BATC PicoTuner wiki.'
            : null;

        for (const [btn, active] of [[hBtn, current === 'hi'],
                                     [vBtn, current === 'lo'],
                                     [toneBtn, tone]]) {
            btn.disabled = absent;
            btn.className = 'btn btn-sm ' +
                (absent ? 'btn-outline-secondary opacity-50'
                        : (active ? 'btn-success' : 'btn-outline-secondary'));
            if (absent) {
                if (!btn.dataset.titleOrig) btn.dataset.titleOrig = btn.title || '';
                btn.title = why;
                btn.style.cursor = 'not-allowed';
                btn.style.pointerEvents = 'auto';   // so the tooltip still shows
            } else if (btn.dataset.titleOrig !== undefined) {
                btn.title = btn.dataset.titleOrig;
                btn.style.cursor = '';
            }
        }

        // The surrounding span carries its own explanatory tooltip -
        // replace it too, or hovering anywhere but the buttons still
        // claims this is a working control.
        const wrap = hBtn.parentElement;
        if (wrap) {
            if (!wrap.dataset.titleOrig) wrap.dataset.titleOrig = wrap.title || '';
            wrap.title = absent ? why : wrap.dataset.titleOrig;
        }
    }
}

async function onLnbPsuClick(plug, voltage) {
    // Disabled buttons shouldn't fire, but guard anyway - a keyboard
    // activation or a stale page could still get here, and sending a
    // command to a plug with no generator just produces a button that
    // flicks green and reverts.
    const probe = document.getElementById('lnb-psu-' + plug + '-h');
    if (probe && probe.disabled) return;
    // Pressing the currently-active button again turns it off, rather
    // than needing a separate Off button - matches exactly what was
    // asked for. Reads the CURRENT state from the button's own class
    // (already kept in sync by renderLnbPsuButtons() on every status
    // poll) rather than a separate tracked variable, so there's only
    // ever one source of truth for what's currently showing. Only
    // ever touches this one plug's own two buttons - the other
    // plug's state, and the tone toggle, are untouched either way
    // (the backend preserves the current tone when only voltage is
    // sent - see set_lnb_psu()'s own docstring).
    const hBtn = document.getElementById('lnb-psu-' + plug + '-h');
    const vBtn = document.getElementById('lnb-psu-' + plug + '-v');
    const clickedBtn = voltage === 'hi' ? hBtn : vBtn;
    const alreadyActive = clickedBtn.classList.contains('btn-success');
    const newVoltage = alreadyActive ? 'off' : voltage;
    const result = await api('POST', '/api/lnb_psu', {plug: plug, voltage: newVoltage});
    if (result.success) {
        hBtn.className = 'btn btn-sm ' + (newVoltage === 'hi' ? 'btn-success' : 'btn-outline-secondary');
        vBtn.className = 'btn btn-sm ' + (newVoltage === 'lo' ? 'btn-success' : 'btn-outline-secondary');
    }
}

async function onLnbToneClick(plug) {
    const probeT = document.getElementById('lnb-psu-' + plug + '-tone');
    if (probeT && probeT.disabled) return;
    // Simple on/off toggle, unlike the mutually-exclusive H/V pair
    // above - Hi-Band is rarely/never needed for Amateur TV, so this
    // stays a single button rather than a Hi-Band/Lo-Band pair.
    // Sending only {plug, tone} leaves the current voltage untouched
    // on the backend (see set_lnb_psu()'s own docstring) - if voltage
    // is currently off, this only updates the stored preference for
    // whenever it's next turned on, without sending a voltage change.
    const toneBtn = document.getElementById('lnb-psu-' + plug + '-tone');
    const newTone = !toneBtn.classList.contains('btn-success');
    const result = await api('POST', '/api/lnb_psu', {plug: plug, tone: newTone});
    if (result.success) {
        toneBtn.className = 'btn btn-sm ' + (newTone ? 'btn-success' : 'btn-outline-secondary');
    }
}

async function updateStatus() {
    try {
        const s = await api('GET', '/api/status');
        
        // Mode badge
        const mode = s.lynx?.mode || 'idle';
        const badge = document.getElementById('mode-badge');
        badge.textContent = mode.toUpperCase();
        badge.style.background = mode === 'idle' ? '#3a4a63' : mode === 'rf' ? '#1a9850' : '#3b82c4';

        // Locator badge - value AND provenance (GPS vs configured),
        // per the task: the two must always be shown together, since
        // a GPS-driven value and an operator-typed one carry very
        // different confidence for anyone reading the display.
        const loc = s.lynx?.portable_locator || '';
        const provenance = s.lynx?.portable_locator_provenance || 'config';
        const gnss = s.lynx?.gnss || {};
        const locBadge = document.getElementById('locator-badge');
        if (gnss.running && !gnss.connected) {
            // Reader is meant to be talking to the HAT (mode isn't
            // Off) but isn't - either no HAT is physically fitted (a
            // fixed repeater site, exactly as expected) or the serial
            // port isn't answering. Takes priority over the value/
            // provenance display below: whatever locator is in use
            // right now is necessarily the configured one, and that's
            // secondary information to "there's no GPS talking to
            // this receiver at all".
            locBadge.textContent = '\U0001F4CD No GNSS Module';
            locBadge.style.background = '#3a4a63';
            locBadge.title = (gnss.last_error ? gnss.last_error + ' \u2014 ' : '') +
                              (loc ? 'using configured locator ' + loc : 'no locator configured');
        } else if (!loc) {
            locBadge.textContent = '\U0001F4CD \u2014';
            locBadge.style.background = '#3a4a63';
            locBadge.title = 'Portable locator: not set';
        } else if (provenance === 'gnss') {
            locBadge.textContent = '\U0001F4CD ' + (gnss.locator_display || loc) + ' (GPS)';
            locBadge.style.background = '#1a9850';
            const sats = gnss.satellites != null ? gnss.satellites + ' sats' : 'sats \u2014';
            const hdop = gnss.hdop != null ? 'HDOP ' + gnss.hdop : '';
            locBadge.title = 'Confirmed GNSS fix - ' + sats + (hdop ? ', ' + hdop : '');
        } else {
            locBadge.textContent = '\U0001F4CD ' + loc + ' (config)';
            locBadge.style.background = '#3a4a63';
            if (gnss.mode === 'automatic' && gnss.pending_secs != null) {
                locBadge.title = 'Configured value - GPS fix settling, ' +
                                  Math.ceil(gnss.pending_secs) + 's to confirm';
            } else if (gnss.mode === 'automatic') {
                locBadge.title = 'Configured value - no GNSS fix (no HAT, or indoors)';
            } else {
                locBadge.title = 'Configured value - GNSS mode is Manual';
            }
        }

        // Tri-watch (Stage 1) - status of every currently-enabled source
        // (any mix of RF-A, RF-B, a stream) shown side by side. No
        // priority/switching logic yet - this is purely informational.
        const tw = s.tri_watch || {};
        const twLine = document.getElementById('dual-watch-line');
        if (tw.enabled && (tw.sources || []).length > 0) {
            const parts = tw.sources.map(src => {
                const icon = src.active ? '🟢' : (src.active === false ? '⚪' : '❓');
                return `${icon} ${src.label}`;
            });
            twLine.textContent = `Tri-watch: ${parts.join('  |  ')}`;
            twLine.style.display = 'block';
        } else {
            twLine.style.display = 'none';
        }

        // mpv restart counter - links through to /diagnostics for detail
        const restartCount = s.lynx?.mpv_restarts_total ?? 0;
        document.getElementById('mpv-restart-count').textContent = restartCount;

        renderLnbPsuButtons(s.picotuner?.lnb_psu);
        
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

        // tri_watch: Rx1 stays continuously, independently tuned in the
        // background regardless of what's currently being displayed -
        // its own panel should never be taken over by stream info in
        // that case, same principle already proven correct for Tuner
        // B's own panel below. Confirmed as a real, reported bug
        // otherwise: whichever of Rx1/stream "came up first" took this
        // shared slot, hiding the other's status entirely.
        const triWatchUsesRx1 = tw.enabled && (tw.sources || []).some(src => src.type === 'rf' && src.rcv === 1);
        const showStreamInMainPanel = (lynxMode === 'stream') && !triWatchUsesRx1;

        if (showStreamInMainPanel) {
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
                ['Level',     (pt.dbm && pt.dbm !== '0') ? pt.dbm + ' dBm' : (pt.level ? '-' + pt.level + ' dBm' : '—')],
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

        // Diversity: second tuner panel, shown while diversity mode is
        // active OR while tri_watch has an RF source configured for
        // rcv=2 - picotuner_state_b is always correctly populated
        // either way, but this panel used to be the ONLY place any of
        // that data (callsign, MER, margin, etc.) was ever shown, and
        // it was gated to diversity mode specifically. tri_watch users
        // previously had no way to see Rx2's detailed lock info at all
        // beyond the arbitrator's own simple green/grey indicator -
        // confirmed as a real, reported gap.
        const triWatchUsesRx2 = (s.tri_watch?.sources || []).some(src => src.type === 'rf' && src.rcv === 2);
        const panelB = document.getElementById('diversity-panel-b');
        if (div.enabled || triWatchUsesRx2) {
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
                    ['Level',     (b.dbm && b.dbm !== '0') ? b.dbm + ' dBm' : '—'],  // a literal "0" is a known, documented Picotuner firmware quirk for rcv=2, not a genuine reading - treated the same as no data
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
            // Live combining stats - the combiner's own rolling window,
            // not a cumulative-since-start figure. Diversity-only,
            // deliberately not shown for tri_watch - there's no
            // combiner running in that mode, so nothing to report here.
            if (div.enabled) {
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
            }
        } else {
            panelB.style.display = 'none';
            document.getElementById('diversity-stats-line').style.display = 'none';
            document.getElementById('site-name').style.display = '';
        }

        // tri_watch: the stream's own info, shown independently of Rx1's
        // panel above - only needed when that panel is actually showing
        // Rx1's own RF details instead (triWatchUsesRx1), since otherwise
        // the stream info is already correctly shown there and showing it
        // twice would just be a duplicate.
        const streamPanel = document.getElementById('tri-watch-stream-panel');
        if (tw.enabled && lynxMode === 'stream' && triWatchUsesRx1) {
            streamPanel.style.display = '';
            const info = s.lynx?.stream_info || {};
            const bitrate = info.bitrate_kbps;
            const protocol = s.lynx?.stream_protocol;
            const streamRows = [
                ['Stream',     s.lynx?.stream_name || '—'],
                ['Protocol',   protocol || '—'],
                ['Bitrate',    bitrate != null ? bitrate.toFixed(0) + ' kbps' : '—'],
                ['Video',      info.video_codec || '—'],
                ['Audio',      info.audio_codec || '—'],
            ];
            document.getElementById('tri-watch-stream-status').innerHTML = streamRows.map(r =>
                '<div class="d-flex justify-content-between mb-1" style="flex-wrap:wrap; gap: 4px 12px;"><span>' + r[0] + '</span>' +
                '<span class="status-value">' + r[1] + '</span></div>'
            ).join('');
        } else {
            streamPanel.style.display = 'none';
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
        alert(`Calculated tuner frequency ${(tunerFreq/1000).toFixed(3)} MHz is out of range.\\n` +
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

async function stopApp() {
    if (!confirm('Stop Lynx? This closes the app and returns the Pi to its desktop - ' +
                 'the receiver will be off the air until Lynx is started again.')) {
        return;
    }
    try {
        const result = await api('POST', '/api/app_stop');
        if (result && result.detail) {
            alert('Could not stop: ' + result.detail);
            return;
        }
    } catch (e) {
        // The server is expected to go down as part of this - not itself
        // a sign anything went wrong.
    }
}

async function shutdownPi() {
    if (!confirm('Shut down the Pi completely? Unlike Reboot, it will NOT come back up on ' +
                 'its own - you will need to physically power it back on (or use a remote ' +
                 'power switch) to bring the receiver back.')) {
        return;
    }
    try {
        const result = await api('POST', '/api/shutdown');
        if (result && result.detail) {
            alert('Could not shut down: ' + result.detail);
            return;
        }
    } catch (e) {
        // The server is expected to go down as part of this - not itself
        // a sign anything went wrong.
    }
    alert('Shutting down - the Pi will power off shortly.');
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
// dB is the actual, functional unit throughout the UI now (0.5dB
// steps) - the backend's own /api/volume is completely unchanged and
// still speaks 0-100 percent, so every value crosses this conversion
// at the boundary rather than the underlying representation changing
// everywhere. -60dB is the slider's floor: mpv's own volume scale is
// cubic (percent = 100 * 10^(dB/60)), so percent asymptotically
// approaches but never reaches exactly 0 as dB decreases - a
// continuous slider needs a genuine, finite minimum rather than -∞,
// and -60dB (10% - already very quiet) is a practical, sensible one.
// The "(x/11)" alongside it is the Spinal Tap joke Justin actually
// asked for - a cosmetic-only value derived FROM the dB figure, not
// the other way around as an earlier version of this had it.
let volumeDebounce = null;
const VOLUME_MIN_DB = -60;

function dbToPercent(db) {
    return Math.round(100 * Math.pow(10, db / 60));
}
function percentToDb(percent) {
    if (percent <= 0) return VOLUME_MIN_DB;
    const db = 60 * Math.log10(percent / 100);
    return Math.max(VOLUME_MIN_DB, db);
}
function dbToEleven(db) {
    const raw = 11 * (db - VOLUME_MIN_DB) / (0 - VOLUME_MIN_DB);
    return Math.max(0, Math.min(11, raw)).toFixed(1);
}
function volumeReadoutText(db) {
    const dbNum = parseFloat(db);
    const sign = dbNum >= 0 ? '+' : '';
    return sign + dbNum.toFixed(1) + ' dB (' + dbToEleven(dbNum) + '/11)';
}

function onVolumeInput(dbValue) {
    // Update the live readout immediately as the slider moves, but
    // debounce the actual API call so dragging doesn't flood requests
    document.getElementById('volume-value').textContent = volumeReadoutText(dbValue);
    clearTimeout(volumeDebounce);
    volumeDebounce = setTimeout(() => setVolume(dbValue), 150);
}

async function setVolume(dbValue) {
    await api('POST', '/api/volume', {level: dbToPercent(parseFloat(dbValue))});
}

function onDefaultVolumeInput() {
    // Keeps the (x/11) readout next to the Default on boot field live
    // as the dB value is typed/adjusted, matching the main slider's
    // own readout - separate from saveDefaultVolume() below, which
    // only actually persists the value once "Save" is clicked.
    const db = parseFloat(document.getElementById('default-volume-input').value) || 0;
    document.getElementById('default-volume-eleven').textContent = '(' + dbToEleven(db) + '/11)';
}

async function saveDefaultVolume() {
    const db = parseFloat(document.getElementById('default-volume-input').value) || 0;
    await api('POST', '/api/volume/default', {level: dbToPercent(db)});
}

async function loadVolume() {
    try {
        const data = await api('GET', '/api/volume');
        if (data.level != null) {
            const db = percentToDb(data.level);
            document.getElementById('volume-slider').value = db;
            document.getElementById('volume-value').textContent = volumeReadoutText(db);
        }
    } catch(e) {}
    try {
        const cfg = await api('GET', '/api/config');
        const defaultVolPercent = cfg.audio?.default_volume ?? 100;
        const defaultDb = percentToDb(defaultVolPercent);
        document.getElementById('default-volume-input').value = defaultDb;
        document.getElementById('default-volume-eleven').textContent = '(' + dbToEleven(defaultDb) + '/11)';
    } catch(e) {}
}

function renderUpdateStatus(status) {
    const badge = document.getElementById('version-badge');
    const applyBtn = document.getElementById('update-apply-btn');
    const version = status.current_version || '?';
    // Shown on ALL four branches below via versionText, rather than
    // repeating it in each one separately - which channel is active
    // matters regardless of whether an update check has ever run or
    // what it found.
    const versionText = 'v' + version.replace(/^v/, '') + (status.channel === 'beta' ? ' [BETA]' : '');

    if (status.check_error) {
        badge.textContent = versionText;
        badge.title = 'Update check failed: ' + status.check_error;
        badge.style.background = '#3a4a63';
        applyBtn.style.display = 'none';
    } else if (status.update_available) {
        badge.textContent = versionText + ' — ' +
            status.commits_behind + ' update' + (status.commits_behind === 1 ? '' : 's') + ' available';
        badge.title = (status.new_commits || []).join('\\n') || 'Update available';
        badge.style.background = '#e8a33d';
        applyBtn.style.display = 'inline-block';
    } else if (status.checked_at) {
        badge.textContent = versionText + ' — up to date';
        badge.title = 'Last checked: ' + status.checked_at;
        badge.style.background = '#1a9850';
        applyBtn.style.display = 'none';
    } else {
        // Never actually checked yet - checking is entirely manual,
        // so this is the normal, expected state until "Check Updates"
        // is clicked, not an error or something stale.
        badge.textContent = versionText;
        badge.title = 'Not yet checked - click "Check Updates"';
        badge.style.background = '#3a4a63';
        applyBtn.style.display = 'none';
    }
}

async function loadUpdateStatus() {
    try {
        const status = await api('GET', '/api/update/status');
        renderUpdateStatus(status);
    } catch (e) {}
}

async function checkForUpdates() {
    const btn = document.getElementById('update-check-btn');
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    try {
        const status = await api('POST', '/api/update/check');
        renderUpdateStatus(status);
    } catch (e) {
        document.getElementById('version-badge').title = 'Check failed: ' + e.message;
    } finally {
        btn.disabled = false;
        btn.textContent = original;
    }
}

async function applyUpdate() {
    const commits = (document.getElementById('version-badge').title || '').trim();
    const preview = commits ? ('\\n\\nChanges:\\n' + commits) : '';
    if (!confirm('Pull the latest code and reboot the Pi now? This reboots the whole ' +
                 'system (not just Lynx), so it takes longer than a simple restart - ' +
                 'roughly 30-60 seconds.' + preview)) {
        return;
    }
    const btn = document.getElementById('update-apply-btn');
    btn.disabled = true;
    btn.textContent = 'Updating…';

    let result;
    try {
        result = await api('POST', '/api/update/apply');
    } catch (e) {
        result = { detail: e.message };
    }
    // api() returns the parsed body regardless of HTTP status - a
    // failure comes back as {detail: "..."} (FastAPI's own error
    // shape), not a thrown exception, so check for that explicitly
    // rather than relying on a catch block that wouldn't actually fire.
    // Shows the backend's own message directly rather than prefixing
    // "nothing was changed" - that's only true for a failed pull, not
    // for the separate case where the pull succeeded but the automatic
    // reboot itself couldn't run (passwordless sudo not configured) -
    // the backend's own wording already covers both cases correctly.
    if (!result || result.detail) {
        alert(result?.detail || 'Update failed: unknown error');
        btn.disabled = false;
        btn.textContent = '⬆️ Update';
        return;
    }

    btn.textContent = 'Rebooting…';
    // Poll for the server coming back rather than a fixed delay - the
    // retry loop below handles however long the actual reboot takes
    // regardless, but starting the first attempt after a reasonable
    // pause avoids wasting several retries on a Pi that's still mid-
    // shutdown.
    setTimeout(() => {
        const tryReload = async () => {
            try {
                const r = await fetch('/api/status');
                if (!r.ok) throw new Error('not ready');
                location.reload();
            } catch (e) {
                setTimeout(tryReload, 2000);
            }
        };
        tryReload();
    }, 10000);
}

// ── Init ──────────────────────────────────────────────────────
loadConfig();
loadPresets();
loadLiveStreams();
loadVolume();
loadBootDefault();
loadUpdateStatus();
updateStatus();
setInterval(updateStatus, 3000);
setInterval(loadLiveStreams, 3600000);

// ── QuickLynx button ─────────────────────────────────────────
// Its own poll, deliberately not hooked into updateStatus(). Editing
// the middle of that function is what broke this page once already;
// keeping it separate means a fault here cannot stop the rest of the
// page updating.
// Bootstrap's .disabled class sets pointer-events:none, which stops the
// browser registering a hover at all - so the title never appeared,
// which defeated the point of greying the button rather than hiding it.
// Pointer events are re-enabled inline; this handler then swallows the
// click so a disabled button still does not navigate.
document.addEventListener('DOMContentLoaded', () => {
    const b = document.getElementById('quicklynx-btn');
    if (b) b.addEventListener('click', (ev) => {
        if (b.classList.contains('disabled')) {
            ev.preventDefault();
            ev.stopPropagation();
        }
    });
});

async function refreshQuickLynxButton() {
    const btn = document.getElementById('quicklynx-btn');
    if (!btn) return;
    try {
        const r = await fetch('/api/status');
        const s = await r.json();
        if (s.lynx && s.lynx.quicklynx_enabled) {
            btn.classList.remove('disabled');
            btn.removeAttribute('aria-disabled');
            btn.title = 'Open QuickLynx - the QO-100 wideband spectrum, '
                      + 'click a signal to tune this receiver to it.';
            btn.style.cursor = 'pointer';
        } else {
            btn.classList.add('disabled');
            btn.setAttribute('aria-disabled', 'true');
            btn.title = 'QuickLynx is switched off. It shows the QO-100 '
                      + 'wideband spectrum and lets you click a signal to '
                      + 'tune this receiver to it. Turn it on in Config to use it.';
            btn.style.cursor = 'help';
        }
    } catch (e) {
        // Silent: the status poll elsewhere already reports connection
        // trouble, and a second complaint about the same thing helps
        // nobody.
    }
}
refreshQuickLynxButton();
setInterval(refreshQuickLynxButton, 5000);

</script>
</body>
</html>"""

# ── Run ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    # Version detection is lazy (see ensure_current_version) and
    # update checking is entirely manual (POST /api/update/check) -
    # deliberately nothing here at startup: not all Lynx receivers
    # are reliably online, sometimes running RF-only with no internet
    # at all, and this must never touch the network unless a user
    # explicitly asks it to.
    # GNSS (portable locator) - started unconditionally, like the
    # Picotuner monitor threads just below. There is no mode that
    # stops the reader itself anymore (see _apply_gnss_mode's own
    # docstring) - a site with no HAT fails quietly regardless, and a
    # site with one fitted should keep the option of GPS time sync
    # even while Manual mode keeps GPS out of the locator.
    gnss_reader.start()
    _apply_gnss_mode()
    # Start Picotuner monitor background threads
    monitor = threading.Thread(target=picotuner_monitor, daemon=True)
    monitor.start()
    quality = threading.Thread(target=picotuner_quality_monitor, daemon=True)
    quality.start()
    quality_b = threading.Thread(target=picotuner_table_monitor_b, daemon=True)
    quality_b.start()
    freshness = threading.Thread(target=rf_mpv_lifecycle_monitor, daemon=True)
    freshness.start()
    diversity_stuck = threading.Thread(target=diversity_stuck_lock_monitor, daemon=True)
    diversity_stuck.start()
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
    # Tri-watch (up to 3 sources, Stage 1) - probes for any enabled
    # stream sources are started here, once, rather than lazily on
    # first use, so their connect/handshake/reconnect cycle is already
    # warmed up and settled by the time anyone actually looks at their
    # status, matching how the Picotuner's own monitoring threads work.
    # RF sources need no separate startup step at all - their status is
    # already tracked by the existing picotuner_state/picotuner_state_b
    # globals, and this stage deliberately doesn't auto-tune them (see
    # the tri_watch_enabled global's own docstring for why).
    # NOT yet validated against the real BATC RTMP server for more than
    # one simultaneous stream source - the single-stream case has been
    # confirmed working directly against real hardware; a second,
    # concurrent stream probe alongside it is untested.
    _tw_cfg = config.get('tri_watch', {})
    if _tw_cfg.get('enabled', False):
        tri_watch_enabled = True
        tri_watch_sources_cfg = _tw_cfg.get('sources', [])
        for _tw_idx, _tw_src in enumerate(_tw_cfg.get('sources', [])):
            if not _tw_src.get('enabled', False):
                continue
            _tw_type = _tw_src.get('type')
            if _tw_type == 'rf':
                if _tw_src.get('rcv') not in (1, 2):
                    print(f"[tri_watch] source {_tw_idx}: type rf needs rcv 1 or 2 - skipping")
                    continue
                print(f"[tri_watch] source {_tw_idx}: RF Rx{_tw_src.get('rcv')} - status tracked, not auto-tuned (Stage 2)")
            elif _tw_type == 'stream':
                if _tw_src.get('domain') and _tw_src.get('app') and _tw_src.get('streamname'):
                    _probe = lynx_rtmp_probe.RTMPStreamProbe(
                        domain=_tw_src['domain'],
                        app=_tw_src['app'],
                        stream_name=_tw_src['streamname'],
                        rtmp_port=_tw_src.get('port', 1935),
                    )
                    _probe.start()
                    tri_watch_probes[_tw_idx] = _probe
                    print(f"[tri_watch] source {_tw_idx}: stream probe started for {_tw_src['domain']}/{_tw_src['app']}/{_tw_src['streamname']}")
                else:
                    print(f"[tri_watch] source {_tw_idx}: type stream missing domain/app/streamname - skipping")
            else:
                print(f"[tri_watch] source {_tw_idx}: unknown type {_tw_type!r} - skipping")
    # Started unconditionally, matching every other monitor thread here -
    # the loop itself checks tri_watch_enabled before doing anything.
    tri_watch_arbitrator_thread = threading.Thread(target=tri_watch_arbitrator_loop, daemon=True)
    tri_watch_arbitrator_thread.start()
    # End-of-contact station map - started unconditionally for the same
    # reason as the arbitrator above; the loop checks whether the
    # feature is enabled before doing anything, and works out for itself
    # whether plain RF, diversity or tri_watch rules apply.
    pathfinder_thread = threading.Thread(target=pathfinder_watcher, daemon=True)
    pathfinder_thread.start()
    # Auto-Squeak - see _start_squeak for why this is safe to call
    # unconditionally even when the feature is off.
    _start_squeak()
    # Picotuner tuning watchdog - catches the tuner losing its tuning
    # for ANY reason (its own reboot, a lost command, static), which
    # otherwise leaves a repeater deaf and reporting itself healthy.
    picotuner_watchdog_thread = threading.Thread(
        target=picotuner_tuning_watchdog, daemon=True)
    picotuner_watchdog_thread.start()
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
        get_lnb_state=lambda: (current_lnb_lo_khz, current_lnb_side),
        get_tri_watch_displayed_rcv=lambda: tri_watch_target_rcv,
        get_tri_watch_enabled=lambda: tri_watch_enabled,
        get_stream_active=_stream_is_being_shown,
        # Runtime diversity state, not the saved config. Turning
        # diversity on by tuning sets a module global and does NOT write
        # the config file, so the manager was reading a flag that stayed
        # False - and tuner B's lock was therefore ignored for QRZ,
        # Slack, Companion and GPIO Tx. Pathfinder, mpv and the OSD all
        # read the tuner state directly, so they worked perfectly and
        # hid the problem: a receiver could sit showing a picture from
        # Rx2 while logging nothing at all.
        get_diversity_enabled=lambda: diversity_enabled)
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

    # Re-apply the saved LNB PSU voltage for both plugs every startup -
    # the Picotuner's own remote settings aren't guaranteed to survive
    # its own power cycle, and this needs to be sent regardless of
    # what's being tuned (or whether anything is), unlike a tune
    # command - see set_lnb_psu()'s own docstring for the full
    # rationale. Fire-and-forget, same as every other picotuner_cmd()
    # call - if the Picotuner isn't reachable yet at this exact
    # moment, there's nothing more useful to do than what already
    # happens (a clear, logged failure) rather than blocking startup
    # on it.
    # Sent for both plugs unconditionally, INCLUDING "off". A Picotuner
    # powers up with its supply on - 18V observed on plug A on real
    # hardware, every time - so skipping a plug configured off would
    # leave voltage sitting on it after any power cycle the Pi didn't
    # share. Commanded from the config rather than current_lnb_psu_a/b,
    # which by this point may already have been overwritten by the
    # Picotuner's own broadcast telling us what IT came up with.
    _startup_lnb = config.get('lnb_psu', {}) or {}
    _v_a = str(_startup_lnb.get('plug_a', 'off')).lower()
    _t_a = bool(_startup_lnb.get('plug_a_tone', False))
    _v_b = str(_startup_lnb.get('plug_b', 'off')).lower()
    _t_b = bool(_startup_lnb.get('plug_b_tone', False))
    picotuner_cmd(f"[to@wh] vgx={_v_a if _v_a == 'off' else _v_a + ('t' if _t_a else '')}")
    # Plug B skipped entirely when the Picotuner reports no generator
    # fitted - the usual case, since only one is populated as standard.
    if current_lnb_psu_b != 'absent':
        picotuner_rcv2_cmd(f"[to@wh] vgy={_v_b if _v_b == 'off' else _v_b + ('t' if _t_b else '')}",
                           config['picotuner'])

    # A short pause for the LNB PSU voltage to physically stabilise
    # before the very first tune attempt below - confirmed directly on
    # real hardware (main): a cold PoE power cycle with Plug A's PSU
    # freshly turned on saw Rx A take roughly 4 minutes to lock on an
    # otherwise genuinely good signal; follow-up testing found 2s
    # wasn't quite enough margin, 5s resolved it reliably. Only pauses
    # if a voltage was actually turned ON above - keyed off the CONFIG,
    # not current_lnb_psu_a/b, which may already have been overwritten
    # by the Picotuner reporting whatever it powered up with, and would
    # then skip this pause in precisely the case it exists for.
    if _v_a != "off" or _v_b != "off":
        LNB_PSU_STARTUP_SETTLE_SECS = 5.0
        time.sleep(LNB_PSU_STARTUP_SETTLE_SECS)

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

        # tri_watch, when enabled, deliberately replaces the whole
        # resume-last-state concept below - a dedicated repeater
        # receiver should always come up on its configured inputs,
        # not whatever a human happened to be doing before the last
        # restart. Applies even with only one source enabled within
        # tri_watch, not just 2 or 3 - the behavioural switch is
        # tri_watch.enabled itself, not how many sources are in use.
        if tri_watch_enabled:
            print("[tri_watch] enabled - skipping resume-last-state, tuning configured RF sources instead")
            tri_watch_startup_tune()
            # Both receivers start transmitting TS data as soon as
            # they're tuned, well before the arbitrator ever decides
            # what to display - drain everything immediately so
            # nothing goes unread even during this initial window.
            _tri_watch_sync_drainers(None)
            return

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
