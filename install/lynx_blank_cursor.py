#!/usr/bin/env python3
"""lynx_blank_cursor.py - an invisible mouse pointer for labwc.

Installs a cursor theme whose every cursor is one transparent pixel, and
tells labwc to use it. The pointer then simply isn't there, with no key
presses and nothing that can go wrong.

Why: Lynx hid the pointer with a labwc keybind (Alt+Super+H: HideCursor,
then WarpCursor off-screen), pressed by wtype. On the ROCK 5T's labwc
(0.8.3, Debian trixie) HideCursor does not exist, and with no screen
connected the WarpCursor that follows made labwc quit - found 22 Oct 2026,
when switching the monitor to another input took the whole display down.

Run as the Lynx user:  python3 lynx_blank_cursor.py
Undo:                  remove XCURSOR_THEME from ~/.config/labwc/environment
"""

import struct
from pathlib import Path

THEME = "lynx-blank"
SIZE = 24   # nominal size; the image itself is 1x1

# Every name labwc, GTK or mpv might ask for. A name missing from the
# theme would fall back to the default theme and show a visible pointer.
NAMES = [
    "default", "left_ptr", "arrow", "top_left_arrow", "pointer", "hand1", "hand2",
    "text", "xterm", "ibeam", "wait", "watch", "progress", "left_ptr_watch",
    "crosshair", "cross", "move", "grabbing", "grab", "all-scroll", "fleur",
    "not-allowed", "no-drop", "help", "question_arrow", "context-menu",
    "col-resize", "row-resize", "n-resize", "s-resize", "e-resize", "w-resize",
    "ne-resize", "nw-resize", "se-resize", "sw-resize", "ew-resize", "ns-resize",
    "nesw-resize", "nwse-resize", "top_side", "bottom_side", "left_side",
    "right_side", "top_left_corner", "top_right_corner", "bottom_left_corner",
    "bottom_right_corner", "sb_h_double_arrow", "sb_v_double_arrow",
]


def xcursor_blank() -> bytes:
    """A valid Xcursor file holding one 1x1 fully transparent image."""
    image_type = 0xFFFD0002
    header = struct.pack("<4sIII", b"Xcur", 16, 0x10000, 1)
    toc = struct.pack("<III", image_type, SIZE, 16 + 12)
    image = struct.pack("<IIIIIIIII", 36, image_type, SIZE, 1,
                        1, 1, 0, 0, 0)          # width, height, xhot, yhot, delay
    pixel = struct.pack("<I", 0x00000000)       # ARGB, fully transparent
    return header + toc + image + pixel


def main():
    home = Path.home()
    theme_dir = home / ".local/share/icons" / THEME
    cursors = theme_dir / "cursors"
    cursors.mkdir(parents=True, exist_ok=True)
    (theme_dir / "index.theme").write_text(
        f"[Icon Theme]\nName={THEME}\nComment=Lynx: no visible pointer\n")
    blank = xcursor_blank()
    for name in NAMES:
        (cursors / name).write_bytes(blank)

    env = home / ".config/labwc/environment"
    env.parent.mkdir(parents=True, exist_ok=True)
    lines = [l for l in (env.read_text().splitlines() if env.exists() else [])
             if not l.startswith(("XCURSOR_THEME=", "XCURSOR_SIZE="))]
    lines += [f"XCURSOR_THEME={THEME}", f"XCURSOR_SIZE={SIZE}"]
    env.write_text("\n".join(lines) + "\n")
    print(f"Installed {len(NAMES)} blank cursors in {cursors}")
    print(f"labwc told to use them in {env} (takes effect when labwc restarts)")


if __name__ == "__main__":
    main()
