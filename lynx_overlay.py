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
#    Bottom right — Magic eye (dBm for RF / BER-PER for streams),
#                    with a reserved slot alongside for a PPM
#                    audio meter (graphic to be supplied)
#    Bottom left  — Frequency, Symbol rate
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

LYNX_API = "http://localhost:8080/api/status"
MPV_TRANSITION_MARKER = "/tmp/lynx_mpv_transitioning"
POLL_SECS = 2

# ── Magic eye calibration ───────────────────────────────────────
# Re-calibrated 2026-07-23 against the real dBm values now available
# from ptwh0v3k+'s look-up table (see draw_bottom_right) - the old
# -85/-25 range was tuned against the previous "level"-based rough
# approximation, not genuine dBm readings.
EYE_DBM_MIN = -70   # fully open (nothing / weakest signal)
EYE_DBM_MAX = -30   # fully closed (full scale / strongest signal)

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
    "dbm_a": "",
    "level_a": "",
    "dbm_b": "",
    "level_b": "",
    "level": "",
    "dbm": "",
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

def poll_status():
    """Background thread — polls the Lynx API continuously."""
    global _raw_lock_history, _raw_online_history
    while True:
        raw_online = False
        try:
            with urllib.request.urlopen(LYNX_API, timeout=2) as r:
                data = json.loads(r.read().decode())
            pt = data.get('picotuner', {})
            div = data.get('diversity', {})
            diversity_enabled = div.get('enabled', False)
            tuner_b = div.get('tuner_b', {}) if diversity_enabled else {}

            # Diversity mode: locked means EITHER tuner is locked, not
            # just tuner A — the combiner can produce a perfectly good
            # picture from just one healthy receiver. Without this,
            # pulling A's antenna showed "SEARCHING" and covered the
            # screen even while B alone was locked and the combined
            # output was playing completely fine.
            a_locked = pt.get('locked', False)
            b_locked = tuner_b.get('locked', False)
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
            # below — prefer A whenever it's genuinely locked, falling
            # back to B only when A isn't locked but B is.
            use_b = diversity_enabled and not a_locked and b_locked
            source = tuner_b if use_b else pt
            state["locked_via"] = "b" if use_b else "a"
            state["tuner_b_pct_nul"] = tuner_b.get("pct_nul", "")
            state["diversity_stats"] = div.get("stats") or {}

            state["callsign"]  = source.get('callsign', '')
            state["frequency"] = source.get('frequency', '')
            state["mer"]       = source.get('mer', '')
            state["margin"]    = source.get('margin', '')
            state["level"]     = source.get('level', '')
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
        self.draw_bottom_left(cr, width, height)
        self.draw_bottom_right(cr, width, height)

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

        for i, line in enumerate(lines):
            y = margin + size + (i * line_h)
            self.draw_text(cr, width - margin, y, line, size=size, align="right", colour=colour)

    def draw_bottom_left(self, cr, width, height):
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
            for i, line in enumerate(reversed(lines)):
                y = height - margin - (i * line_h)
                self.draw_text(cr, margin, y, line, size=size)
            return

        NOT_LOCKED_COLOUR = (0.9, 0.5, 0.1)
        LOCKED_COLOUR = (0.0, 1.0, 0.25)

        if not state["online"]:
            self.draw_text(cr, LEFT_MARGIN, height - 30, "Picotuner offline", size=30, colour=NOT_LOCKED_COLOUR)
            return
        margin = LEFT_MARGIN
        size = 30
        line_h = size * 1.3
        freq = state["frequency"] or f"{state['freq_khz']/1000:.3f}"
        lines = [f"{freq} MHz  {state['sr_ks']} kS/s"]
        if state["modcod"]:
            modcod_line = state["modcod"]
            if state["codec"]:
                modcod_line += f"  {state['codec']}"
            if state["audio_codec"]:
                modcod_line += f"/{state['audio_codec']}"
            lines.append(modcod_line)

        # Diversity mode: a 4th row showing the combiner's own live
        # A/B/gaps split — moved here from a separate top-left
        # display, which looked visually disconnected from the rest
        # of the OSD. This is the combiner's genuine rolling-window
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
        for i, line in enumerate(reversed(lines)):
            y = height - margin - (i * line_h)
            self.draw_text(cr, margin, y, line, size=size, colour=text_colour)

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
            text = f"{dbm:.0f}" if (dbm_str or level_str) else "--"
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

    app = LynxOverlayApp()
    app.run(None)
