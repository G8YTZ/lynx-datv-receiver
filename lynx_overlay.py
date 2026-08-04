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
#  Lynx — Fullscreen Transparent OSD Overlay
#  G8YTZ / EI3IOB  —  July 2026
#
#  A GTK4 layer-shell overlay that sits above whatever video
#  player is running underneath (mpv, VLC, etc). Renders OSD
#  text and graphics at native display resolution, completely
#  independent of the underlying video source's resolution.
#
#  Layout:
#    Top right    — MER, Margin, Callsign
#    Top left     — Frequency, Symbol rate, modcod/codec, diversity split
#    Bottom right — Magic eye (dBm for RF / BER-PER for streams)
#    Bottom left  — BBC-style PPM audio meter, calibrated PPM4=-18dBFS,
#                    driven by a real, live PipeWire audio tap
#                    independent of mpv (see audio_ppm_monitor)
#    Centre       — Lynx logo, shown only when not locked
#
#  This decouples the OSD from the player entirely — swapping
#  players in future (e.g. for H.266 support) needs no OSD
#  changes at all.
#
#  DIVERSITY MODE FIX (this version): this file long predates
#  Diversity mode, and originally only ever read the top-level
#  "picotuner" field from /api/status — which only ever reflects
#  tuner A (rcv=1). With A's antenna unplugged and B carrying the
#  picture entirely on its own via the combiner, this showed
#  "SEARCHING" and covered the screen with the opaque logo even
#  though a perfectly good picture (and audio) was actually
#  playing underneath — confirmed directly: audio was audible the
#  whole time the cover was shown. Now checks both tuners and
#  falls back to tuner B's own callsign/MER/frequency/etc for
#  display when B is the one actually locked, so the OSD shows
#  real, correct info rather than blank fields.
#
#  Usage: python3 lynx_overlay.py
# ============================================================

import gi
gi.require_version('Gtk', '4.0')
gi.require_foreign('cairo')
gi.require_version('Gtk4LayerShell', '1.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, Gtk4LayerShell, GLib, Gdk, GdkPixbuf
import cairo
import os
import math

import urllib.request
import json
import threading
import time
import subprocess
import struct

LYNX_API = "http://localhost:8080/api/status"
MPV_TRANSITION_MARKER = "/tmp/lynx_mpv_transitioning"
POLL_SECS = 2

# tri_watch's "someone else wants in" notification sound - place the
# actual audio file here yourself (not fetched/bundled by Claude - see
# chat). Any format mpv can play (mp3, wav, etc) works, since mpv
# itself is what plays it below.
NOTIFICATION_SOUND_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "tri_watch_notification.mp3")

def play_notification_sound():
    """Fire-and-forget playback of the notification sound via a short-
    lived, audio-only mpv process - completely separate from the main,
    video-playing mpv instance, so it can never interfere with it.
    Silently does nothing if the sound file isn't present, rather than
    erroring - this feature is opt-in by placing the file, not
    required for Lynx to run."""
    if not os.path.exists(NOTIFICATION_SOUND_PATH):
        return
    try:
        subprocess.Popen(
            ["mpv", "--no-video", "--really-quiet", NOTIFICATION_SOUND_PATH],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except Exception as e:
        print(f"Could not play notification sound: {e}")

# ── Magic eye calibration ───────────────────────────────────────
# Re-calibrated 2026-07-23 against the real dBm values now available
# from ptwh0v3k+'s look-up table (see draw_bottom_right) - the old
# -85/-25 range was tuned against the previous "level"-based rough
# approximation, not genuine dBm readings.
EYE_DBM_MIN = -70   # fully open (nothing / weakest signal)
EYE_DBM_MAX = -30   # fully closed (full scale / strongest signal)

# ── BBC-style PPM ballistics ────────────────────────────────────
# Calibration: PPM4 = -18dBFS, matching EBU R68's digital reference
# alignment (specified directly).
#
# Ballistic time constants confirmed against multiple independent
# sources (Sound on Sound, Wikipedia/IEC 60268-10 summaries, and a
# detailed technical DIY audio project citing the exact circuit-level
# constants): 1.7ms attack, 650ms decay.
#
# Critical detail, found only by testing against the documented spec
# numbers rather than assumed correct: the smoothing must happen in
# the LINEAR amplitude domain, not the dB domain. A real, physical
# PPM's moving coil responds to rectified linear voltage - the dB
# scale is only the faceplate markings, not the domain the ballistics
# actually operate in. Smoothing directly in dB (tried first) gave a
# 20dB decay in ~0.15s instead of the documented ~1.5s - roughly 10x
# too fast. Smoothing in linear amplitude and converting to dB only
# for display reproduces the documented spec numbers closely:
#   - Calibration: settles to exactly -18.000dBFS for a steady
#     -18dBFS input (verified).
#   - Decay: 1.497s to fall 20dB (documented spec: ~1.5s).
#   - Attack: 1.20/2.79/4.68dB down for a genuine, sample-by-sample
#     1kHz tone burst of 10/5/3ms respectively (documented spec:
#     ~1/2/4dB down) - also confirmed that feeding the filter a
#     pre-computed "peak over N ms" block value, rather than
#     individual samples, gives badly wrong attack timing; the filter
#     must run on (or very close to) individual samples.
PPM_ATTACK_TC = 0.0017    # seconds
PPM_DECAY_TC = 0.650      # seconds
PPM4_DBFS = -18.0          # calibration point
PPM_DB_PER_DIVISION = 4.0
PPM_FLOOR_LINEAR = 10 ** (-100.0 / 20)  # effective silence, avoids log(0)

class PpmBallistics:
    """Feed it a stream of peak-amplitude readings (linear, 0.0-1.0+)
    at a known interval; it tracks the smoothed, ballistics-correct
    meter position in PPM scale units (e.g. 4.0 = sitting exactly on
    PPM4)."""

    def __init__(self):
        self._level_linear = PPM_FLOOR_LINEAR

    def update(self, peak_amplitude, dt_seconds):
        target = max(peak_amplitude, PPM_FLOOR_LINEAR)
        tc = PPM_ATTACK_TC if target > self._level_linear else PPM_DECAY_TC
        alpha = 1 - math.exp(-dt_seconds / tc)
        self._level_linear += (target - self._level_linear) * alpha

    def ppm_position(self):
        return 4.0 + (self.level_dbfs - PPM4_DBFS) / PPM_DB_PER_DIVISION

    @property
    def level_dbfs(self):
        return 20 * math.log10(self._level_linear)


# ── Shared state, updated by the polling thread ────────────────
state = {
    "online": False,
    "locked": False,
    "mpv_running_for_rf": False,
    "callsign": "",
    "frequency": "",
    "mer": "",
    "margin": "",
    "locked_a": False,
    "mer_a": "",
    "margin_a": "",
    "locked_b": False,
    "mer_b": "",
    "margin_b": "",
    "frequency_b": "",
    "sr_ks_b": "",
    "tri_watch_show_searching_rx2": False,
    "dbm_a": "",
    "level_a": "",
    "dbm_b": "",
    "level_b": "",
    "level": "",
    "dbm": "",
    "ppm_position_l": None,   # set by audio_ppm_monitor() - None until the audio tap produces its first real reading
    "ppm_level_dbfs_l": None,
    "ppm_position_r": None,
    "ppm_level_dbfs_r": None,
    "ppm_style": "skeleton",   # "skeleton" or "full_fat" - set by poll_status() from /api/status; defaults to skeleton if the API doesn't provide it (e.g. an older lynx_app.py)
    "modcod": "",
    "codec": "",
    "audio_codec": "",
    "programme": "",
    "mode": "idle",
    "freq_khz": 437000,
    "sr_ks": 333,
    "stream_name": "",
    "stream_bitrate_kbps": None,
    "stream_video_codec": "",
    "stream_audio_codec": "",
    "stream_protocol": "",
    "mpv_transitioning": False,
    "portable_locator": "",
    "tri_watch_notification": None,   # the current "someone else wants in" message text, or None
    # Diversity mode — which tuner is actually the one supplying the
    # locked/displayed state.
    "diversity_enabled": False,
    "locked_via": "a",  # "a" or "b" — which tuner's data populated the fields above
    # Tuner B's %NUL — an interim signal-quality proxy until Brian
    # adds proper $15-equivalent level data for rcv=2 to the
    # firmware. Deliberately NOT part of the source-swapping fields
    # above (mer/margin/etc) — this always reflects tuner B
    # specifically, regardless of which tuner is currently "primary",
    # since the point is visibility into B on its own.
    "tuner_b_pct_nul": "",
    "diversity_stats": {},  # combiner's live rolling-window A/B/gaps stats
}

LOCK_STABLE_POLLS = 5  # ~10s of sustained lock at POLL_SECS=2 before trusting it. Was 2 (~2-4s) -
                        # confirmed live that's short enough for genuine noise-induced false locks
                        # (common on an open, signal-free frequency) to persist through, briefly
                        # uncovering the screen and exposing whatever mpv last actually decoded,
                        # potentially hours-stale since mpv never restarts without a genuine re-tune.
                        # A real transmission locks almost immediately and stays locked continuously
                        # for the whole broadcast, so this costs nothing perceptible for genuine signal.
_raw_lock_history = []

ONLINE_STABLE_POLLS = 3  # slightly more tolerant than lock, since "online" flapping is more visually jarring (whole zones disappear) than a lock badge changing colour
_raw_online_history = []
_last_notification_sound_played_for = None  # triggered_at of the last tri_watch
                                              # notification a sound was played for -
                                              # lets a genuinely new notification be
                                              # distinguished from the same one still
                                              # being displayed across multiple polls

def audio_ppm_monitor():
    """Background thread: drives the stereo PPM meter with real, live
    audio levels - taps the system's actual audio output via PipeWire
    directly, deliberately independent of mpv. mpv's own af-metadata/
    astats property path for this exact purpose is confirmed broken in
    current mpv versions (a real, active mpv upstream bug, not
    specific to this project - see mpv-player/mpv#14464), so this
    bypasses mpv's audio pipeline entirely rather than depend on it -
    matching the same "OSD and player fully decoupled" principle the
    rest of this file already follows.

    Runs pw-cat as a subprocess, reading raw s16 PCM straight from the
    default sink's monitor (i.e. "whatever's actually playing right
    now"), stereo - left and right are each tracked with their own,
    independent PpmBallistics instance, for a genuine two-needle
    stereo display rather than a single combined reading.

    Feeds the ballistics filter one individual sample at a time per
    channel - see PpmBallistics' own docstring for why this
    specifically matters for getting the attack timing right,
    confirmed by direct testing.

    Confirmed working against real PipeWire/real audio hardware
    (2026-07-29) - the --target=@DEFAULT_SINK@.monitor approach is
    genuinely correct, not just reasoned. Still fails gracefully
    (leaves ppm_position_l/r as None, so draw_bottom_left() simply
    doesn't draw anything) if pw-cat isn't found or misbehaves, since
    this remains a separate subsystem nothing else in this file
    depends on.
    """
    SAMPLE_RATE = 48000
    CHANNELS = 2
    FRAME_BYTES = 2 * CHANNELS  # s16 = 2 bytes/sample
    READ_FRAMES = 240           # ~5ms worth per read - keeps the meter responsive
    dt = 1.0 / SAMPLE_RATE

    meter_l = PpmBallistics()
    meter_r = PpmBallistics()
    proc = None

    while True:
        try:
            if proc is None or proc.poll() is not None:
                proc = subprocess.Popen(
                    ["pw-cat", "-r", "--target=@DEFAULT_SINK@.monitor",
                     "--format=s16", "--rate", str(SAMPLE_RATE),
                     "--channels", str(CHANNELS), "-"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                )

            chunk = proc.stdout.read(FRAME_BYTES * READ_FRAMES)
            if not chunk:
                time.sleep(0.05)
                continue

            n_frames = len(chunk) // FRAME_BYTES
            if n_frames == 0:
                continue
            # Bulk-unpack rather than per-sample int.from_bytes() calls -
            # this runs continuously at 48kHz, so the unpack cost adds up.
            samples = struct.unpack(f"<{n_frames * CHANNELS}h", chunk[:n_frames * FRAME_BYTES])
            for i in range(n_frames):
                left = samples[i * CHANNELS]
                right = samples[i * CHANNELS + 1]
                meter_l.update(abs(left) / 32768.0, dt)
                meter_r.update(abs(right) / 32768.0, dt)

            state["ppm_position_l"] = meter_l.ppm_position()
            state["ppm_level_dbfs_l"] = meter_l.level_dbfs
            state["ppm_position_r"] = meter_r.ppm_position()
            state["ppm_level_dbfs_r"] = meter_r.level_dbfs

        except FileNotFoundError:
            print("[overlay] pw-cat not found - PPM meter unavailable (is pipewire-utils installed?)")
            time.sleep(30)
        except Exception as e:
            print(f"[overlay] PPM audio tap error: {type(e).__name__}: {e}")
            if proc:
                proc.kill()
                proc = None
            time.sleep(2)

def poll_status():
    """Background thread — polls the Lynx API continuously."""
    global _raw_lock_history, _raw_online_history, _last_notification_sound_played_for
    while True:
        raw_online = False
        try:
            with urllib.request.urlopen(LYNX_API, timeout=2) as r:
                data = json.loads(r.read().decode())
            pt = data.get('picotuner', {})
            div = data.get('diversity', {})
            diversity_enabled = div.get('enabled', False)
            tuner_b = div.get('tuner_b', {})

            # Diversity mode: locked means EITHER tuner is locked, not
            # just tuner A — the combiner can produce a perfectly good
            # picture from just one healthy receiver. Without this,
            # pulling A's antenna showed "SEARCHING" and covered the
            # screen even while B alone was locked and the combined
            # output was playing completely fine.
            a_locked = pt.get('locked', False)
            b_locked = tuner_b.get('locked', False)

            # tri_watch: which receiver (if any) is it currently
            # displaying? Matched via the "idx" field each source entry
            # carries, against tri_watch's own displayed_source_idx.
            # Computed here, before raw_locked below, specifically so
            # raw_locked (which drives the OSD's green/orange lock
            # colour) can correctly account for it too - confirmed as
            # the actual cause of a real, reported bug: the display
            # stayed permanently orange while tri_watch showed Rx2, no
            # matter how solidly locked Rx2 genuinely was, since the
            # diversity-only formula below never considered b_locked at
            # all outside diversity mode.
            tri_watch = data.get('tri_watch') or {}
            tri_watch_use_b = False
            if tri_watch.get('enabled'):
                displayed_idx = tri_watch.get('displayed_source_idx')
                if displayed_idx is not None:
                    for src in tri_watch.get('sources', []):
                        if src.get('idx') == displayed_idx and src.get('type') == 'rf':
                            tri_watch_use_b = (src.get('rcv') == 2)
                            break

            # Diversity mode: locked means EITHER tuner is locked, not
            # just tuner A — the combiner can produce a perfectly good
            # picture from just one healthy receiver. Without this,
            # pulling A's antenna showed "SEARCHING" and covered the
            # screen even while B alone was locked and the combined
            # output was playing completely fine. tri_watch: locked
            # means specifically whichever receiver it's currently
            # displaying is locked - a genuinely different question
            # from diversity's "is either one locked", so it needs its
            # own branch here rather than reusing that formula.
            if tri_watch_use_b:
                raw_locked = b_locked
            elif tri_watch.get('enabled') and tri_watch.get('displayed_source_idx') is not None:
                raw_locked = a_locked
            else:
                raw_locked = a_locked or (diversity_enabled and b_locked)

            # Independent per-tuner MER/margin, for the diversity-mode
            # top-right display which shows both tuners at once -
            # separate from state["mer"]/state["margin"] below, which
            # only ever reflect whichever single tuner is currently
            # driving the video.
            state["locked_a"] = a_locked
            state["mer_a"] = pt.get('mer', '')
            state["margin_a"] = pt.get('margin', '')
            state["locked_b"] = b_locked
            state["mer_b"] = tuner_b.get('mer', '')
            state["margin_b"] = tuner_b.get('margin', '')
            state["frequency_b"] = tuner_b.get('frequency', '')
            state["sr_ks_b"] = tuner_b.get('symbol_rate', '')
            # Independent per-tuner dBm (ptwh0v3k+) / level fallback,
            # for the split magic eye - top half driven by A, bottom by B.
            state["dbm_a"] = pt.get('dbm', '')
            state["level_a"] = pt.get('level', '')
            state["dbm_b"] = tuner_b.get('dbm', '')
            state["level_b"] = tuner_b.get('level', '')

            _raw_lock_history.append(raw_locked)
            if len(_raw_lock_history) > LOCK_STABLE_POLLS:
                _raw_lock_history.pop(0)
            if all(_raw_lock_history):
                state["locked"] = True
            elif not any(_raw_lock_history):
                state["locked"] = False

            raw_online = pt.get('online', False) or (diversity_enabled and tuner_b.get('online', False))
            state["diversity_enabled"] = diversity_enabled

            # Which tuner's data actually populates the display fields
            # below — tri_watch's own choice takes priority when it
            # applies; otherwise, in diversity mode, prefer A whenever
            # it's genuinely locked, falling back to B only when A
            # isn't locked but B is. tri_watch and diversity are
            # mutually exclusive modes, so these two conditions never
            # genuinely compete in practice.
            use_b = tri_watch_use_b or (diversity_enabled and not a_locked and b_locked)
            source = tuner_b if use_b else pt
            state["locked_via"] = "b" if use_b else "a"
            state["tuner_b_pct_nul"] = tuner_b.get("pct_nul", "")
            state["diversity_stats"] = div.get("stats") or {}

            # While tri_watch is enabled but hasn't selected anything to
            # display yet (still "searching" - displayed_source_idx is
            # None), also show Rx2's own frequency on a second line, if
            # tri_watch has an Rx2 source configured at all - neither
            # receiver is "winning" yet in this state, and there's no
            # video underneath being obscured by showing both. Once
            # something is actually selected, this reverts to showing
            # only that one receiver, same as any other RF display.
            state["tri_watch_show_searching_rx2"] = False
            if tri_watch.get('enabled') and tri_watch.get('displayed_source_idx') is None:
                for src in tri_watch.get('sources', []):
                    if src.get('type') == 'rf' and src.get('rcv') == 2:
                        state["tri_watch_show_searching_rx2"] = True
                        break

            state["callsign"]  = source.get('callsign', '')
            state["frequency"] = source.get('frequency', '')
            state["mer"]       = source.get('mer', '')
            state["margin"]    = source.get('margin', '')
            state["level"]     = source.get('level', '')
            state["ppm_style"] = data.get('lynx', {}).get('ppm_style', 'skeleton')
            # ptwh0v3k+ (2026-07-23): real dBm from the firmware's own
            # look-up table - preferred over the "level"-based
            # approximation below when available, kept as a fallback
            # for older firmware that doesn't send this field.
            state["dbm"]       = source.get('dbm', '')
            state["modcod"]    = source.get('modcod', '')
            state["codec"]     = source.get('codec', '')
            state["audio_codec"] = source.get('audio_codec', '')
            state["programme"] = source.get('programme', '')
            live_sr = source.get('symbol_rate', '')
            if live_sr:
                state["sr_ks"] = live_sr
            lynx = data.get('lynx', {})
            state["mode"]      = lynx.get('mode', 'idle')
            state["mpv_running_for_rf"] = lynx.get('mpv_running_for_rf', False)
            state["stream_name"] = lynx.get('stream_name', '')
            stream_info = lynx.get('stream_info') or {}
            state["stream_bitrate_kbps"] = stream_info.get('bitrate_kbps')
            state["stream_video_codec"] = stream_info.get('video_codec') or ""
            state["stream_audio_codec"] = stream_info.get('audio_codec') or ""
            state["stream_protocol"] = lynx.get('stream_protocol') or ""
            state["mpv_transitioning"] = lynx.get('mpv_transitioning', False)
            state["portable_locator"] = lynx.get('portable_locator', '')
            # tri_watch's "someone else wants in" notification - the
            # backend already handles its own expiry (get_notification()
            # returns None once past the configured display window), so
            # this side just needs to check presence, not compute timing.
            tri_watch = data.get('tri_watch') or {}
            notification = tri_watch.get('notification') if tri_watch.get('enabled') else None
            state["tri_watch_notification"] = notification.get('message') if notification else None
            if notification:
                triggered_at = notification.get('triggered_at')
                if triggered_at is not None and triggered_at != _last_notification_sound_played_for:
                    _last_notification_sound_played_for = triggered_at
                    play_notification_sound()
        except Exception:
            raw_online = False  # request itself failed (timeout, connection refused, etc) — treated the same as a genuine "not online" reading, fed into the same debounced history below rather than forcing the displayed state immediately

        # Debounced "online" — a single missed/failed poll (this can be
        # caused by Lynx's own web server being briefly slow, entirely
        # local and nothing to do with Wi-Fi) no longer immediately
        # flashes "Picotuner offline" across the whole OSD. Only a
        # sustained run of bad polls flips the displayed state, same
        # principle already proven for the lock indicator above.
        _raw_online_history.append(raw_online)
        if len(_raw_online_history) > ONLINE_STABLE_POLLS:
            _raw_online_history.pop(0)
        if any(_raw_online_history):
            state["online"] = True
        elif not any(_raw_online_history) and len(_raw_online_history) >= ONLINE_STABLE_POLLS:
            state["online"] = False

        time.sleep(POLL_SECS)


class LynxOverlay(Gtk.Window):
    def __init__(self):
        super().__init__()

        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        for edge in (Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM,
                     Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT):
            Gtk4LayerShell.set_anchor(self, edge, True)
        Gtk4LayerShell.set_exclusive_zone(self, -1)
        Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.NONE)

        self.set_decorated(False)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window, drawingarea {
                background-color: transparent;
                background-image: none;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        drawing_area = Gtk.DrawingArea()
        drawing_area.set_draw_func(self.on_draw)
        drawing_area.set_hexpand(True)
        drawing_area.set_vexpand(True)
        self.set_child(drawing_area)
        self.drawing_area = drawing_area

        self.logo_pixbuf = None
        logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lynx_logo_transparent.png")
        if not os.path.exists(logo_path):
            logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lynx_bg.png")
        if os.path.exists(logo_path):
            try:
                self.logo_pixbuf = GdkPixbuf.Pixbuf.new_from_file(logo_path)
            except Exception as e:
                print(f"Could not load logo: {e}")

        GLib.timeout_add(100, self.tick)

    def tick(self):
        self.drawing_area.queue_draw()
        return True

    def on_draw(self, area, cr, width, height):
        mpv_transitioning = os.path.exists(MPV_TRANSITION_MARKER)
        genuinely_locked = (state["locked"] and state["mpv_running_for_rf"]) or state["mode"] == "stream"
        showing_picture = genuinely_locked and not mpv_transitioning

        if not showing_picture:
            if mpv_transitioning and genuinely_locked:
                # mpv is being restarted (decoder/freeze recovery)
                # while the tuner itself remains genuinely locked - a
                # plain black slide reads as a brief, minor interruption
                # rather than the full logo screen reappearing, which
                # looks like a fresh "searching" state starting over.
                cr.set_source_rgba(0, 0, 0, 1.0)
                cr.set_operator(cairo.OPERATOR_SOURCE)
                cr.paint()
                cr.set_operator(cairo.OPERATOR_OVER)
                self.draw_text(cr, width / 2, height / 2, "RECONNECTING", size=48, align="center")
            else:
                cr.set_source_rgba(0.04, 0.04, 0.08, 1.0)
                cr.set_operator(cairo.OPERATOR_SOURCE)
                cr.paint()
                cr.set_operator(cairo.OPERATOR_OVER)
                if self.logo_pixbuf:
                    self.draw_centered_logo(cr, width, height)
        else:
            cr.set_source_rgba(0, 0, 0, 0)
            cr.set_operator(cairo.OPERATOR_SOURCE)
            cr.paint()
            cr.set_operator(cairo.OPERATOR_OVER)

        cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)

        self.draw_top_right(cr, width, height)
        self.draw_top_left(cr, width, height)
        self.draw_bottom_left(cr, width, height)
        self.draw_bottom_right(cr, width, height)
        self.draw_waiting_bubble(cr, width, height)

    def draw_text(self, cr, x, y, text, size=30, align="left", colour=(0.0, 1.0, 0.25)):
        cr.set_font_size(size)
        extents = cr.text_extents(text)
        if align == "right":
            x = x - extents.width
        elif align == "center":
            x = x - extents.width / 2
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.move_to(x + 2, y + 2)
        cr.show_text(text)
        cr.set_source_rgba(*colour, 0.95)
        cr.move_to(x, y)
        cr.show_text(text)
        cr.new_path()  # show_text() advances the current point rather than clearing it (unlike fill()) - without this, whatever draws next inherits a stray point and gets an unintended connector line on its first arc()/line_to()

    def _wrap_text(self, cr, text, max_width):
        """Balanced word-wrap: rather than greedily filling each line
        all the way to max_width (which can leave an awkward, near-
        empty final line, e.g. a single word by itself), finds the
        narrowest width that still only needs as many lines as filling
        to the true max_width would - a standard technique for more
        evenly-distributed line lengths. Assumes the font/size are
        already set on cr before calling."""
        words = text.split()
        if not words:
            return []

        def wrap_at(width):
            lines = []
            current = ""
            for word in words:
                candidate = (current + " " + word).strip()
                if cr.text_extents(candidate).width <= width or not current:
                    current = candidate
                else:
                    lines.append(current)
                    current = word
            if current:
                lines.append(current)
            return lines

        lines_at_max = wrap_at(max_width)
        if len(lines_at_max) <= 1:
            return lines_at_max

        target_line_count = len(lines_at_max)
        lo = max(cr.text_extents(w).width for w in words)  # can't go narrower than the single widest word
        hi = max_width
        for _ in range(20):  # plenty of precision for pixel-level text
            mid = (lo + hi) / 2
            if len(wrap_at(mid)) <= target_line_count:
                hi = mid
            else:
                lo = mid
        return wrap_at(hi)

    def _rounded_rect_path(self, cr, x, y, w, h, r):
        """Traces a rounded-rectangle path on cr - does not fill/stroke
        itself, so the caller can do either (or both, for a border)."""
        cr.new_path()
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.arc(x + w - r, y + r, r, 3 * math.pi / 2, 2 * math.pi)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.close_path()

    def draw_waiting_bubble(self, cr, width, height):
        """tri_watch's "someone else wants in" notification - an
        iMessage-style speech bubble, positioned middle-right so it's
        clearly visible without overlapping any of the existing
        corner-anchored OSD elements. The backend already handles
        timing/expiry (state["tri_watch_notification"] is simply None
        once the notification's display window has passed), so this
        only ever needs to check presence, not compute anything
        time-based itself."""
        message = state.get("tri_watch_notification")
        if not message:
            return

        font_size = 26
        line_height = font_size * 1.35
        padding_x = 24
        padding_y = 20
        tail_size = 30  # made longer per feedback (was 18)
        max_text_width = width * 0.32  # keeps the bubble from dominating the screen, since it sits over live video

        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(font_size)
        lines = self._wrap_text(cr, message, max_text_width)
        text_width = max((cr.text_extents(line).width for line in lines), default=0)
        cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)  # restored immediately after measuring, so nothing else drawn this frame is affected

        bubble_w = text_width + padding_x * 2
        bubble_h = line_height * len(lines) + padding_y * 2

        # Middle-right placement, with margin from the screen edge and
        # room for the tail pointing left toward the video content.
        right_margin = 40
        bubble_x = width - right_margin - bubble_w - tail_size
        bubble_y = height / 2 - bubble_h / 2

        corner_radius = 18
        bubble_colour = (0.0, 0.478, 1.0)  # Apple's system blue, #007AFF - the actual iMessage outgoing-bubble colour, confirmed rather than approximated

        # Drop shadow first, offset slightly, so the bubble reads clearly against any video content behind it
        cr.set_source_rgba(0, 0, 0, 0.35)
        self._rounded_rect_path(cr, bubble_x + 4, bubble_y + 4, bubble_w, bubble_h, corner_radius)
        cr.fill()

        # Main bubble body
        cr.set_source_rgba(*bubble_colour, 0.92)
        self._rounded_rect_path(cr, bubble_x, bubble_y, bubble_w, bubble_h, corner_radius)
        cr.fill()

        # Tail - emerges from the lower-left corner area, pointing
        # down and further left, matching Apple's own bubble style
        # (moved from the middle of the left edge per feedback). Three
        # points: one attachment higher up the left edge (past where
        # the rounded corner starts), the outward-pointing tip below
        # and to the left of the bubble, and a second attachment along
        # the bottom edge (past where the rounded corner ends) - this
        # triangle reads as a tail emerging from the corner rather than
        # a fin sticking out of a flat edge.
        tail_attach_top_y = bubble_y + bubble_h - corner_radius - 6
        tail_attach_bottom_x = bubble_x + corner_radius + 12
        cr.new_path()
        cr.move_to(bubble_x, tail_attach_top_y)
        cr.line_to(bubble_x - tail_size, bubble_y + bubble_h + tail_size * 0.45)
        cr.line_to(tail_attach_bottom_x, bubble_y + bubble_h)
        cr.close_path()
        cr.set_source_rgba(*bubble_colour, 0.92)
        cr.fill()

        # Text, left-aligned within the bubble (matching the
        # conventional messaging-app look, and avoiding each line
        # appearing to float at a different horizontal position when
        # line lengths vary, even with the balanced wrap above)
        cr.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        cr.set_font_size(font_size)
        text_block_h = line_height * len(lines)
        first_line_y = bubble_y + (bubble_h - text_block_h) / 2 + font_size
        for i, line in enumerate(lines):
            line_x = bubble_x + padding_x
            line_y = first_line_y + i * line_height
            cr.set_source_rgba(1, 1, 1, 0.98)
            cr.move_to(line_x, line_y)
            cr.show_text(line)
        cr.new_path()
        cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)  # restore the OSD's normal font for anything drawn after this

    def draw_top_right(self, cr, width, height):
        if not state["online"] and state["mode"] != "stream":
            return
        margin = 16  # matches the bottom-left zone's margin
        size = 30
        line_h = size * 1.3
        lines = []

        if state["mode"] == "stream":
            lines.append(state["stream_name"] or "STREAMING")
            colour = (0.0, 1.0, 0.25)
        else:
            colour = (0.0, 1.0, 0.25) if state["locked"] else (0.9, 0.5, 0.1)

            # Shown whenever set, regardless of lock state - a constant,
            # deliberately visible reminder that QRZ logging is currently
            # using an overridden locator rather than each contacted
            # station's own registered one, so it isn't left on by
            # accident after a portable session ends.
            if state["portable_locator"]:
                lines.append(f"PORTABLE: {state['portable_locator']}")

            if state["diversity_enabled"]:
                # Compact per-tuner MER/margin - one row per tuner,
                # each independently suppressed if that tuner isn't
                # currently producing valid readings, rather than
                # always showing two rows regardless of which
                # antenna/receiver is actually working right now.
                if state["locked_a"] and state["mer_a"] and state["margin_a"]:
                    lines.append(f"A: MER/D {state['mer_a']:>5}/{state['margin_a']:>5} dB")
                if state["locked_b"] and state["mer_b"] and state["margin_b"]:
                    lines.append(f"B: MER/D {state['mer_b']:>5}/{state['margin_b']:>5} dB")
            else:
                if state["locked"] and state["mer"] and state["margin"]:
                    lines.append(f"MER/D {state['mer']:>5}/{state['margin']:>5} dB")

            # Callsign/searching indicator goes last - it takes up
            # less visual real estate than the MER/margin rows above it.
            if state["locked"]:
                if state["callsign"]:
                    label = state["callsign"]
                    if state["diversity_enabled"] and state["locked_via"] == "b":
                        label += " (B)"
                    lines.append(label)
            else:
                if state["callsign"] and state["callsign"] not in ("lost", "search", ""):
                    lines.append(f">> {state['callsign']} <<")
                else:
                    lines.append("SEARCHING")

            # tri_watch, still "searching" (nothing selected/displayed
            # yet): Rx2 gets its own "SEARCHING" indicator too, right
            # below Rx1's - by definition of this state, Rx2 hasn't
            # been confirmed locked and selected yet either (otherwise
            # tri_watch would have already picked it), so this is
            # always exactly "SEARCHING", mirroring the top-left zone's
            # own two-line pattern for the same state.
            if state["tri_watch_show_searching_rx2"]:
                lines.append("SEARCHING")

        for i, line in enumerate(lines):
            y = margin + size + (i * line_h)
            self.draw_text(cr, width - margin, y, line, size=size, align="right", colour=colour)

    def draw_top_left(self, cr, width, height):
        LEFT_MARGIN = 16  # ~1 character in from the screen edge at size=30
        if state["mode"] == "stream":
            margin = LEFT_MARGIN
            size = 30
            line_h = size * 1.3
            lines = []
            if state["stream_protocol"]:
                lines.append(state["stream_protocol"])
            codec_parts = []
            if state["stream_video_codec"]:
                codec_parts.append(state["stream_video_codec"].upper())
            if state["stream_audio_codec"]:
                codec_parts.append(state["stream_audio_codec"].upper())
            if codec_parts:
                lines.append("/".join(codec_parts))
            if not lines:
                lines = ["--"]
            for i, line in enumerate(lines):
                y = margin + size + (i * line_h)
                self.draw_text(cr, margin, y, line, size=size)
            return

        NOT_LOCKED_COLOUR = (0.9, 0.5, 0.1)
        LOCKED_COLOUR = (0.0, 1.0, 0.25)

        if not state["online"]:
            self.draw_text(cr, LEFT_MARGIN, LEFT_MARGIN + 30, "Picotuner offline", size=30, colour=NOT_LOCKED_COLOUR)
            return
        margin = LEFT_MARGIN
        size = 30
        line_h = size * 1.3
        freq = state["frequency"] or f"{state['freq_khz']/1000:.3f}"
        lines = [f"{freq} MHz  {state['sr_ks']} kS/s"]

        # tri_watch, still "searching" (nothing selected/displayed yet):
        # also show Rx2's own frequency on the line right below Rx1's -
        # no naming/labelling needed, the frequency itself already
        # tells the audience which input it is. Reverts to the normal,
        # single-receiver view automatically once something locks and
        # gets selected (state["tri_watch_show_searching_rx2"] goes
        # False the moment tri_watch's own displayed_source_idx is set).
        if state["tri_watch_show_searching_rx2"] and state["frequency_b"]:
            sr_b = f"  {state['sr_ks_b']} kS/s" if state["sr_ks_b"] else ""
            lines.append(f"{state['frequency_b']} MHz{sr_b}")

        if state["modcod"]:
            modcod_line = state["modcod"]
            if state["codec"]:
                modcod_line += f"  {state['codec']}"
            if state["audio_codec"]:
                modcod_line += f"/{state['audio_codec']}"
            lines.append(modcod_line)

        # Diversity mode: a 3rd row showing the combiner's own live
        # A/B/gaps split — this is the combiner's genuine rolling-window
        # figures (see diversity_combiner_pcr.py), not a cumulative
        # since-start average, so it reflects current conditions.
        if state["diversity_enabled"]:
            st = state.get("diversity_stats") or {}
            if st:
                a = st.get("window_pct_a")
                b = st.get("window_pct_b")
                gaps = st.get("window_pct_gap")
                if a is not None and b is not None:
                    lines.append(f"A:{a:.0f}% B:{b:.0f}% gaps:{gaps:.1f}%")
                else:
                    lines.append("Diversity: starting...")
            else:
                lines.append("Diversity: starting...")

        text_colour = LOCKED_COLOUR if state["locked"] else NOT_LOCKED_COLOUR
        for i, line in enumerate(lines):
            y = margin + size + (i * line_h)
            self.draw_text(cr, margin, y, line, size=size, colour=text_colour)

    def draw_ppm_frame(self, cr, frame_cx, frame_cy, frame_radius):
        """The round, vintage-style meter housing - deliberately a
        separate, self-contained piece from the needle/graduation core
        below, so it can be toggled on ('Full Fat') or off ('Skeleton')
        independently, per Justin's own architecture: construct the
        frame separately, switch it on or off as a user preference.
        Styled after genuine round BBC-era PPM housings (dark bakelite-
        style body with a lighter rim/bezel), sized to exactly match
        the magic eye's own 75px radius.
        """
        cr.set_source_rgba(0.08, 0.08, 0.08, 0.9)
        cr.arc(frame_cx, frame_cy, frame_radius, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0.5, 0.5, 0.5, 0.6)
        cr.set_line_width(2.0)
        cr.arc(frame_cx, frame_cy, frame_radius, 0, 2 * math.pi)
        cr.stroke()
        cr.set_source_rgba(0.3, 0.3, 0.3, 0.5)
        cr.set_line_width(1.0)
        cr.arc(frame_cx, frame_cy, frame_radius - 4, 0, 2 * math.pi)
        cr.stroke()
        self.draw_text(cr, frame_cx, frame_cy + frame_radius - 14, "PPM", size=14,
                        align="center", colour=(0.55, 0.55, 0.55))

    def draw_bottom_left(self, cr, width, height):
        """BBC-style stereo PPM (Peak Programme Meter), classic analogue
        needle-meter styling - calibrated PPM4 = -18dBFS per spec,
        ballistics verified separately against the documented
        IEC 60268-10 Type IIa timing (1.7ms attack / 650ms decay time
        constants, smoothed in the linear amplitude domain - see
        PpmBallistics). Sized to exactly match the magic eye's own
        75px radius (verified: every element's distance from the
        frame's own centre stays within it, max ~52px of 75px,
        confirmed numerically before shipping), mirrored to the
        opposite bottom corner so the two meters visually balance.

        Left/right channels each get their own needle - confirmed
        directly from Wikipedia's own Peak Programme Meter article
        that red=left, green=right is the genuine, documented UK
        convention, not an arbitrary choice.

        The optional round housing ('Full Fat' style) is a separate,
        self-contained piece (see draw_ppm_frame) that can be toggled
        independently of this needle/graduation core, which stays
        identical either way ('Skeleton' style is just this, with no
        frame call).
        """
        if not state["online"] and state["mode"] != "stream":
            return

        pos_l = state.get("ppm_position_l")
        pos_r = state.get("ppm_position_r")
        if pos_l is None or pos_r is None:
            return  # no audio level data available yet - draw nothing
                     # rather than a static, misleading meter

        # Frame centre mirrors the magic eye's own exact convention
        # (95px in from the left edge, 95px up from the bottom) - safe
        # to do now that the whole meter is sized to match it; the
        # larger, wider-armed earlier design needed pivot_x=120 to
        # avoid the screen edge, but this smaller scale doesn't.
        frame_cx = 95
        frame_cy = height - 95
        frame_radius = 75

        # The needle pivot sits below the frame's own centre, not at
        # it - the genuine, vintage round-housing design has the
        # needle sweep only the UPPER portion of the circle, leaving
        # the lower portion for housing/branding (see draw_ppm_frame).
        pivot_x = frame_cx
        pivot_y = frame_cy + 25
        needle_radius = 55
        ARC_HALF_SPAN_DEG = 40  # tightened from the original 50 degrees
                                  # to leave room for the minor marks
                                  # below PPM1/above PPM7 while still
                                  # fitting inside the 75px frame -
                                  # verified numerically before shipping
                                  # (max element distance from frame
                                  # centre ~52px, comfortable margin).
        MINOR_MARK_EXTRA_DEG = 6  # how far beyond PPM1/PPM7 the minor,
                                    # unlabelled half-graduations sit

        def angle_for_ppm(ppm_value):
            """0 degrees = straight up. Negative = left (toward PPM1),
            positive = right (toward PPM7)."""
            frac = (ppm_value - 4.0) / 3.0  # -1.0 at PPM1, 0.0 at PPM4, +1.0 at PPM7
            return frac * ARC_HALF_SPAN_DEG

        def point_at(radius, angle_deg):
            """angle_deg: 0 = straight up from the pivot, positive =
            clockwise (right). Screen y increases downward, so 'up'
            is a NEGATIVE y offset - verified directly (not assumed)
            with a bounds check below before shipping."""
            theta = math.radians(angle_deg)
            dx = radius * math.sin(theta)
            dy = -radius * math.cos(theta)
            return pivot_x + dx, pivot_y + dy

        if state.get("ppm_style") == "full_fat":
            self.draw_ppm_frame(cr, frame_cx, frame_cy, frame_radius)

        # Minor, unlabelled half-graduations just beyond PPM1 and PPM7
        # - genuine UK PPMs have these (confirmed directly from a real
        # reference photo), shorter than the main, numbered ticks and
        # without their own number.
        for extreme_angle in (-ARC_HALF_SPAN_DEG - MINOR_MARK_EXTRA_DEG,
                                ARC_HALF_SPAN_DEG + MINOR_MARK_EXTRA_DEG):
            inner = point_at(needle_radius - 3, extreme_angle)
            outer = point_at(needle_radius + 4, extreme_angle)
            cr.set_source_rgba(1, 1, 1, 1.0)
            cr.set_line_width(1.2)
            cr.move_to(*inner)
            cr.line_to(*outer)
            cr.stroke()

        # Tick marks (graduations) and numbers 1-7. No connecting arc
        # line between them - genuine UK PPMs never had one, just the
        # graduations themselves. PPM4 (alignment level) still
        # emphasised via line weight as the actual calibration
        # reference point, since all graduations are peak white -
        # colour itself no longer distinguishes it. Digits themselves
        # also peak white, matching the genuine BBC PPM's own
        # black-background/white-digits scale convention.
        for mark in range(1, 8):
            angle = angle_for_ppm(mark)
            inner = point_at(needle_radius - 9, angle)
            outer = point_at(needle_radius + 4, angle)
            cr.set_source_rgba(1, 1, 1, 1.0)
            cr.set_line_width(2.2 if mark == 4 else 1.4)
            cr.move_to(*inner)
            cr.line_to(*outer)
            cr.stroke()
            label_x, label_y = point_at(needle_radius + 14, angle)
            self.draw_text(cr, label_x, label_y + 5, str(mark), size=13, align="center",
                            colour=(1.0, 1.0, 1.0))

        # The two needles - red (left) and green (right), vivid,
        # fully-saturated colours and equal length. Green drawn first,
        # red second - Cairo paints in the order given, so whatever's
        # drawn last ends up in front; red needs to be in front.
        for position, colour in (
            (pos_r, (0.0, 1.0, 0.0)),
            (pos_l, (1.0, 0.0, 0.0)),
        ):
            clamped = max(0.0, min(8.0, position))
            angle = angle_for_ppm(max(1.0, min(7.0, clamped)))
            # A small amount of genuine overshoot past the physical
            # scale ends, matching how a real needle can briefly swing
            # a little past its own printed extremes.
            if clamped < 1.0:
                angle = angle_for_ppm(1.0) - (1.0 - clamped) * 3
            elif clamped > 7.0:
                angle = angle_for_ppm(7.0) + (clamped - 7.0) * 3
            tip = point_at(needle_radius, angle)
            cr.set_source_rgba(*colour, 1.0)
            cr.set_line_width(2.5)
            cr.move_to(pivot_x, pivot_y)
            cr.line_to(*tip)
            cr.stroke()

        # Zero-adjustment screw head over the pivot - a real analogue
        # meter's needle pivot sits behind exactly this: a black,
        # slotted screw used to mechanically zero the needle. Scaled
        # proportionally down from the previous design (16 -> 10) to
        # match the smaller overall meter. Slot drawn at a slight angle
        # (20 degrees) rather than perfectly horizontal/vertical, since
        # a genuinely hand-adjusted screw is never exactly aligned to
        # the meter face.
        screw_radius = 10
        cr.set_source_rgba(0.05, 0.05, 0.05, 1.0)
        cr.arc(pivot_x, pivot_y, screw_radius, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(0.4, 0.4, 0.4, 0.8)
        cr.set_line_width(1.0)
        cr.arc(pivot_x, pivot_y, screw_radius, 0, 2 * math.pi)
        cr.stroke()
        slot_angle = math.radians(20)
        slot_dx = screw_radius * 0.8 * math.cos(slot_angle)
        slot_dy = screw_radius * 0.8 * math.sin(slot_angle)
        cr.set_source_rgba(0.55, 0.55, 0.55, 0.9)
        cr.set_line_width(1.6)
        cr.move_to(pivot_x - slot_dx, pivot_y - slot_dy)
        cr.line_to(pivot_x + slot_dx, pivot_y + slot_dy)
        cr.stroke()

    def draw_bottom_right(self, cr, width, height):
        if not state["online"] and state["mode"] != "stream":
            return

        eye_cx = width - 95
        eye_cy = height - 95
        eye_radius = 75

        def dbm_to_fraction(dbm_str, level_str):
            try:
                if dbm_str:
                    # ptwh0v3k+ (2026-07-23): real dBm from the firmware's
                    # own look-up table - already correctly signed.
                    dbm = float(dbm_str)
                elif level_str:
                    dbm = -float(level_str)  # older-firmware approximation
                else:
                    dbm = EYE_DBM_MIN
            except (ValueError, TypeError):
                dbm = EYE_DBM_MIN
            frac = (dbm - EYE_DBM_MIN) / (EYE_DBM_MAX - EYE_DBM_MIN)
            frac = max(0.0, min(1.0, frac))
            frac = frac ** 0.5
            text = f"{dbm:.0f} dBm" if (dbm_str or level_str) else "--"
            return frac, text

        if state["mode"] == "stream":
            # No independent tuners in stream mode - both halves show
            # the same bitrate-derived value, per "otherwise both
            # segments show the single tuner that's being displayed".
            EYE_BITRATE_MAX_KBPS = 3000
            bitrate = state["stream_bitrate_kbps"]
            if bitrate is None:
                text = "-- kbps"
                frac = 0.0
            else:
                text = f"{bitrate:.0f} kbps"
                frac = min(1.0, bitrate / EYE_BITRATE_MAX_KBPS) ** 0.3
            fraction_a = fraction_b = frac
            value_text_a = value_text_b = text
            locked_a = locked_b = True  # a playing stream is "active" by definition
        elif state["diversity_enabled"]:
            # Diversity mode: top half is genuinely tuner A, bottom is
            # genuinely tuner B - independent readings, independent
            # lock states.
            fraction_a, value_text_a = dbm_to_fraction(state["dbm_a"], state["level_a"])
            fraction_b, value_text_b = dbm_to_fraction(state["dbm_b"], state["level_b"])
            locked_a = state["locked_a"]
            locked_b = state["locked_b"]
        else:
            # Not diversity mode - both halves show the single tuner
            # actually being displayed (state["dbm"]/state["level"],
            # already resolved to whichever plug is in use).
            frac, text = dbm_to_fraction(state["dbm"], state["level"])
            fraction_a = fraction_b = frac
            value_text_a = value_text_b = text
            locked_a = locked_b = state["locked"]

        self.draw_magic_eye(cr, eye_cx, eye_cy, eye_radius,
                             fraction_a, fraction_b, locked_a, locked_b,
                             value_text_a, value_text_b)

    def draw_magic_eye(self, cr, cx, cy, radius, fraction_a, fraction_b,
                        locked_a, locked_b, value_text_a, value_text_b):
        """Split magic eye - top half independently reflects tuner A,
        bottom half tuner B (or both halves show the same single value
        when there's only one active source - see draw_bottom_right).
        Built by splitting the original, single-fraction wedge geometry
        into four independent quarter-wedges (top-left/top-right driven
        by fraction_a, bottom-left/bottom-right by fraction_b) rather
        than two - the classic EM84 look already opens at top and
        bottom, which is what makes this split possible without
        changing the overall aesthetic."""
        glow_a = (0.0, 0.85, 0.25) if locked_a else (0.85, 0.15, 0.1)
        glow_b = (0.0, 0.85, 0.25) if locked_b else (0.85, 0.15, 0.1)
        active = locked_a or locked_b
        glow = (0.0, 0.85, 0.25) if active else (0.85, 0.15, 0.1)
        outer_r = radius
        inner_r = radius * 0.48

        cr.new_path()  # defensive - ensure no stray point from anything drawn before this, regardless of source
        cr.set_source_rgba(*glow, 0.25)
        cr.set_line_width(2)
        cr.arc(cx, cy, outer_r + 6, 0, 2 * math.pi)
        cr.stroke()

        glow_bright = 0.10 + 0.20 * ((fraction_a + fraction_b) / 2)
        gradient = cairo.RadialGradient(cx, cy, inner_r * 0.1, cx, cy, inner_r)
        gradient.add_color_stop_rgba(0.0, 0.0, 0.0, 0.0, 1.0)
        gradient.add_color_stop_rgba(1.0,
            min(1, glow[0] * glow_bright * 2),
            min(1, glow[1] * glow_bright * 2),
            min(1, glow[2] * glow_bright * 2), 1.0)
        cr.set_source(gradient)
        cr.arc(cx, cy, inner_r, 0, 2 * math.pi)
        cr.fill()

        cr.save()
        cr.new_path()
        cr.arc(cx, cy, outer_r, 0, 2 * math.pi)
        cr.new_sub_path()
        cr.arc(cx, cy, inner_r, 0, 2 * math.pi)
        cr.set_fill_rule(cairo.FILL_RULE_EVEN_ODD)
        cr.clip()

        # Phosphor background - top half brightness from fraction_a,
        # bottom half from fraction_b, each clipped to its own half.
        for half_glow, half_frac, y0, y1 in (
            (glow_a, fraction_a, cy - outer_r - 1, cy),
            (glow_b, fraction_b, cy, cy + outer_r + 1),
        ):
            cr.save()
            cr.new_path()
            cr.rectangle(cx - outer_r - 1, y0, (outer_r + 1) * 2, y1 - y0)
            cr.clip()
            bg_bright = 0.24 + 0.40 * half_frac
            cr.set_source_rgba(min(1, half_glow[0] * bg_bright * 3),
                                min(1, half_glow[1] * bg_bright * 3),
                                min(1, half_glow[2] * bg_bright * 3), 0.9)
            cr.arc(cx, cy, outer_r, 0, 2 * math.pi)
            cr.fill()
            cr.restore()

        # Shadow wedges - four independent quarter-pieces instead of
        # the original two symmetric halves, so the top opening
        # (fraction_a) and bottom opening (fraction_b) can differ.
        # NOTE: Cairo angles are clockwise in this Y-down coordinate
        # system - angle pi/2 is visually the BOTTOM of the circle,
        # not the top (confirmed directly: sin(pi/2)=1, and larger y
        # is lower on screen). So the wedge portions bordering the
        # bottom opening use half_angle_b, and the portions bordering
        # the top opening use half_angle_a - easy to get backwards,
        # confirmed correct here before shipping rather than assumed.
        #
        # Each wedge only ever needs to span up to 90deg to correctly
        # meet the wedge from the other side, with zero gap and zero
        # overlap, at fraction=0 (weakest signal, "nothing"). An
        # earlier version of this formula (matching the original,
        # single-eye version's math) could produce a half_angle up to
        # 180deg, and was first "fixed" by clamping to 90deg - but that
        # clamp left a hard, flat dead zone: for ANY dBm between -70
        # and roughly -56 (fraction below ~0.59), the unclamped value
        # already exceeded 90deg, so the clamp pinned EVERY one of
        # those readings to exactly 90deg - an identical, permanent
        # zero-degree opening across that whole sub-range, confirmed
        # live as "still nothing below -60dBm" even after that fix.
        # Rescaled here so half_angle itself never exceeds 90deg for
        # any fraction in [0,1] - no clamp needed, and the opening
        # varies smoothly and monotonically across the entire range,
        # from exactly 0deg at fraction=0 up to 126deg at fraction=1.
        half_angle_a = (math.pi / 2) * (1.0 - 0.7 * fraction_a)
        half_angle_b = (math.pi / 2) * (1.0 - 0.7 * fraction_b)
        cr.set_source_rgba(0.02, 0.02, 0.03, 0.92)
        for start, end in (
            (math.pi - half_angle_b, math.pi),            # bottom-left
            (math.pi, math.pi + half_angle_a),             # top-left
            (-half_angle_a, 0),                            # top-right
            (0, half_angle_b),                             # bottom-right
        ):
            cr.move_to(cx, cy)
            cr.arc(cx, cy, outer_r * 1.5, start, end)
            cr.close_path()
            cr.fill()
        cr.restore()

        # Thin horizontal divider across the ring (not the hole) to
        # visually reinforce the top/bottom split.
        cr.set_source_rgba(0, 0, 0, 0.5)
        cr.set_line_width(1.5)
        cr.move_to(cx - outer_r, cy)
        cr.line_to(cx - inner_r, cy)
        cr.stroke()
        cr.move_to(cx + inner_r, cy)
        cr.line_to(cx + outer_r, cy)
        cr.stroke()

        # Two stacked value lines in the hole - A on top, B below,
        # smaller font than the original single-value version to fit
        # both comfortably in the same hole.
        font_size = max(14, int(inner_r * 0.55))
        cr.set_font_size(font_size)
        line_gap = font_size * 1.15

        def draw_eye_line(text, y, colour):
            extents = cr.text_extents(text)
            fs = font_size
            while extents.width > inner_r * 1.75 and fs > 9:
                fs -= 1
                cr.set_font_size(fs)
                extents = cr.text_extents(text)
            tx = cx - extents.width / 2
            cr.set_source_rgba(0, 0, 0, 0.8)
            cr.move_to(tx + 1, y + 1)
            cr.show_text(text)
            cr.set_source_rgba(*colour, 0.95)
            cr.move_to(tx, y)
            cr.show_text(text)
            cr.set_font_size(font_size)

        draw_eye_line(value_text_a, cy - line_gap * 0.15, glow_a)
        draw_eye_line(value_text_b, cy + line_gap * 0.85, glow_b)
        cr.new_path()  # show_text() leaves a stray current point otherwise

    def draw_centered_logo(self, cr, width, height):
        pb = self.logo_pixbuf
        pw, ph = pb.get_width(), pb.get_height()
        max_w = width * 0.55
        max_h = height * 0.55
        scale = min(max_w / pw, max_h / ph)
        draw_w = pw * scale
        draw_h = ph * scale
        x = (width - draw_w) / 2
        y = (height - draw_h) / 2 - 30

        cr.save()
        cr.translate(x, y)
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()
        cr.restore()


class LynxOverlayApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="uk.co.g8ytz.lynxoverlay")

    def do_activate(self):
        win = LynxOverlay()
        win.set_application(self)
        win.present()


if __name__ == "__main__":
    poll_thread = threading.Thread(target=poll_status, daemon=True)
    poll_thread.start()
    ppm_thread = threading.Thread(target=audio_ppm_monitor, daemon=True)
    ppm_thread.start()

    app = LynxOverlayApp()
    app.run(None)
