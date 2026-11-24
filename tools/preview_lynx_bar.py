#!/usr/bin/env python3
"""Render the Lynx Bar to PNGs, so it can be looked at before it ships.

    python3 tools/preview_lynx_bar.py [outdir]

The bar lives on a television in another room, behind a deploy and a
service restart. Designing it by pushing a change and walking next door
is how you end up with something nobody is happy with and nobody can
say why. This draws it to a file instead.

Imports render_lynx_bar straight out of lynx_overlay.py without running
the overlay: the renderer takes everything it needs as arguments
precisely so this is possible. A stub stands in for the overlay object,
carrying the layout constants, the palette and draw_text.
"""

import ast
import os
import sys

import cairo

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W, H = 1920, 1080


def load_renderer():
    """Pull the two bar functions out of lynx_overlay.py.

    Not an import: the module builds a Gtk window at import time and
    needs a display. Lifting the functions keeps this runnable on a
    machine with no screen, which is the entire point.
    """
    src = open(os.path.join(HERE, "lynx_overlay.py")).read()
    tree = ast.parse(src)
    want = {"_bar_cells", "render_lynx_bar"}
    ns = {"cairo": cairo}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in want:
            exec(compile(ast.Module([node], []), "<bar>", "exec"), ns)
    missing = want - set(ns)
    if missing:
        raise SystemExit("not found in lynx_overlay.py: " + ", ".join(missing))
    return ns


def load_constants():
    """The layout constants and palette, read off the class rather than
    copied - so a preview can never quietly disagree with the real
    thing."""
    src = open(os.path.join(HERE, "lynx_overlay.py")).read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ClassDef) and node.name == "LynxOverlay":
            out = {}
            for item in node.body:
                if isinstance(item, ast.Assign) and len(item.targets) == 1 \
                        and isinstance(item.targets[0], ast.Name):
                    name = item.targets[0].id
                    if name.startswith(("LYNX_BAR", "BAR_")):
                        out[name] = ast.literal_eval(item.value)
            return out
    raise SystemExit("LynxOverlay not found")


class StubOverlay:
    """Enough of the overlay for the renderer: the constants, and the
    two drawing helpers it calls. draw_text and _rounded_rect_path are
    copied from lynx_overlay.py deliberately - they are four lines each
    and lifting them by AST as well would obscure more than it saved."""

    def __init__(self, consts):
        self.__dict__.update(consts)

    def draw_text(self, cr, x, y, text, size=30, align="left",
                  colour=(0.0, 1.0, 0.25)):
        cr.set_font_size(size)
        ext = cr.text_extents(text)
        if align == "right":
            x = x - ext.width
        elif align == "center":
            x = x - ext.width / 2
        cr.set_source_rgba(0, 0, 0, 0.75)
        cr.move_to(x + 2, y + 2)
        cr.show_text(text)
        cr.set_source_rgba(*colour, 0.95)
        cr.move_to(x, y)
        cr.show_text(text)
        cr.new_path()

    def _rounded_rect_path(self, cr, x, y, w, h, r):
        import math
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()


def backdrop(cr):
    """Something picture-like behind it, because a bar judged against
    flat grey tells you nothing about whether it is readable over
    video."""
    g = cairo.LinearGradient(0, 0, W, H)
    g.add_color_stop_rgb(0.0, 0.16, 0.20, 0.28)
    g.add_color_stop_rgb(0.5, 0.42, 0.34, 0.24)
    g.add_color_stop_rgb(1.0, 0.10, 0.12, 0.16)
    cr.set_source(g)
    cr.paint()
    # A bright band low down: the hard case, a pale picture under the bar.
    cr.set_source_rgba(0.85, 0.82, 0.75, 0.55)
    cr.rectangle(0, H - 260, W, 150)
    cr.fill()


LOCKED = {
    "mode": "rf", "locked": True, "callsign": "G8YTZ",
    "callsign_name": "Justin",
    "site_locator": "JO01IF", "frequency": 438.021, "sr_ks": 333,
    "sr_units": "kS/s", "modcod": "QPSK 9/10", "mer": 23.4, "margin": 17.0,
    "codec": "H265", "audio_codec": "AAC", "lynx_bar_secs": 15,
}
SEARCHING = dict(LOCKED, locked=False, callsign="", mer=None, margin=None,
                 modcod=None, codec=None)
# Margins as well as MERs: the diversity columns carry both, and a
# fixture missing them renders "23.4/-" and makes a working layout look
# broken. A preview is only worth having if it lies about nothing.
DIVERSITY = dict(LOCKED, diversity_enabled=True, locked_a=True,
                 locked_b=True, mer_a=23.4, mer_b=19.8,
                 margin_a=17.0, margin_b=13.2)
DIVERSITY_ONE = dict(DIVERSITY, locked_b=False, mer_b=None, margin_b=None)
# The combination that binds the layout: a broadcast symbol rate, the
# longest modcod and codec names, and both margins gone negative.
DIVERSITY_WIDE = dict(DIVERSITY, sr_ks=27500, modcod="32APSK 9/10",
                      audio_codec="OPUS", margin_a=-2.5, margin_b=-4.5)
STREAM = {"mode": "stream", "locked": True, "stream_name": "Lenham",
          "callsign_name": "Justin",
    "site_locator": "JO01IF", "stream_protocol": "srt",
          "stream_bitrate_kbps": 610, "stream_video_codec": "H265",
          "stream_audio_codec": "AAC", "lynx_bar_secs": 15}


def meters(cr, ov):
    """Where the PPM meter and the magic eye actually sit, as outlines.

    They are NOT suppressed by the bar - they are continuous
    instruments and watching them is the point - so a preview without
    them shows a layout that does not exist. Drawn from the same
    geometry the overlay uses (centres at 95 and width-95, radius 75),
    so the clearance shown here is the real one.
    """
    for cx, name in ((95, "PPM"), (W - 95, "MAGIC EYE")):
        cy, r = H - 95, 75
        cr.set_source_rgba(0.75, 0.80, 0.88, 0.40)
        cr.set_line_width(2)
        cr.set_dash([7, 6])
        cr.arc(cx, cy, r, 0, 2 * 3.14159265)
        cr.stroke()
        cr.set_dash([])
        cr.set_font_size(15)
        ext = cr.text_extents(name)
        cr.set_source_rgba(0.75, 0.80, 0.88, 0.65)
        cr.move_to(cx - ext.width / 2, cy + 5)
        cr.show_text(name)
        cr.new_path()


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "preview")
    os.makedirs(out, exist_ok=True)
    ns = load_renderer()
    ov = StubOverlay(load_constants())
    render = ns["render_lynx_bar"]

    shots = [("locked", LOCKED, 1.0), ("searching", SEARCHING, 1.0),
             ("diversity", DIVERSITY, 1.0),
             ("diversity-one-down", DIVERSITY_ONE, 1.0),
             ("diversity-widest", DIVERSITY_WIDE, 1.0),
             ("stream", STREAM, 1.0),
             ("rising-25", LOCKED, 0.25), ("rising-60", LOCKED, 0.60)]
    for name, st, p in shots:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
        cr = cairo.Context(surf)
        backdrop(cr)
        cr.select_font_face("monospace", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD)
        meters(cr, ov)
        render(cr, W, H, st, p, ov)
        path = os.path.join(out, "bar-%s.png" % name)
        surf.write_to_png(path)
        print("wrote", path)

    # One strip of the whole motion, which is the thing a still cannot
    # show: does it read as arriving, or as appearing?
    n, crop = 6, 300
    tile_w, tile_h = W // 2, crop // 2
    strip = cairo.ImageSurface(cairo.FORMAT_ARGB32, tile_w, tile_h * n)
    sc = cairo.Context(strip)
    for i in range(n):
        p_lin = (i + 1) / n
        frame = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
        fc = cairo.Context(frame)
        backdrop(fc)
        fc.select_font_face("monospace", cairo.FONT_SLANT_NORMAL,
                            cairo.FONT_WEIGHT_BOLD)
        t = p_lin * p_lin * (3.0 - 2.0 * p_lin)     # the same smoothstep
        meters(fc, ov)
        render(fc, W, H, LOCKED, t, ov)
        # Crop to the bottom strip at half scale: clip first, THEN
        # paint, or each frame spills over its neighbours.
        sc.save()
        sc.translate(0, tile_h * i)
        sc.rectangle(0, 0, tile_w, tile_h)
        sc.clip()
        sc.scale(0.5, 0.5)
        sc.set_source_surface(frame, 0, -(H - crop))
        sc.paint()
        sc.restore()
        sc.set_source_rgba(1, 1, 1, 0.22)
        sc.rectangle(0, tile_h * i, tile_w, 1)
        sc.fill()
    path = os.path.join(out, "bar-motion.png")
    strip.write_to_png(path)
    print("wrote", path)


if __name__ == "__main__":
    main()
