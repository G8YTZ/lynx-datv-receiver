#!/usr/bin/env python3
"""Add Lynx's SRT virtual-screen keys to labwc's rc.xml (ROCK and Pi).

labwc has no IPC, so lynx_screen_output.py makes and removes its virtual
screen by pressing these with wtype. Idempotent: an old Lynx SRT block is
replaced. Inside an existing <keyboard> section if there is one (the Pi's
rc.xml has one for the pointer key), else a new section with <default />
so labwc keeps its own keybinds.
"""
import os
import re
import subprocess

BEGIN, END = "<!-- Lynx SRT -->", "<!-- /Lynx SRT -->"
KEYS = (f"{BEGIN}\n"
        '    <keybind key="W-A-S-F9"><action name="VirtualOutputAdd" output_name="LYNX-SRT" /></keybind>\n'
        '    <keybind key="W-A-S-F10"><action name="VirtualOutputRemove" /></keybind>\n'
        f"    {END}")

p = os.path.expanduser("~/.config/labwc/rc.xml")
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    s = open(p).read()
except FileNotFoundError:
    s = '<?xml version="1.0"?>\n<openbox_config>\n</openbox_config>\n'

# Remove any earlier Lynx SRT block, including the first version's own
# <keyboard> wrapper (install/rock5t.sh, 11 Nov 2026).
s = re.sub(r"\s*<!-- Lynx SRT -->\s*<keyboard>.*?</keyboard>\s*<!-- /Lynx SRT -->",
           "", s, flags=re.S)
s = re.sub(r"\s*<!-- Lynx SRT -->.*?<!-- /Lynx SRT -->", "", s, flags=re.S)

if re.search(r"<keyboard\s*>", s):
    s = re.sub(r"(<keyboard\s*>)", r"\1\n    " + KEYS.replace("\\", "\\\\"), s, count=1)
elif re.search(r"<keyboard\s*/>", s):
    s = re.sub(r"<keyboard\s*/>", "<keyboard>\n    <default />\n    " + KEYS +
               "\n  </keyboard>", s, count=1)
else:
    s = s.replace("</openbox_config>",
                  "  <keyboard>\n    <default />\n    " + KEYS +
                  "\n  </keyboard>\n</openbox_config>", 1)
open(p, "w").write(s)
print("labwc: SRT virtual screen keys in rc.xml.")
# labwc rereads rc.xml on SIGHUP.
subprocess.run(["pkill", "-HUP", "-x", "labwc"], check=False)
