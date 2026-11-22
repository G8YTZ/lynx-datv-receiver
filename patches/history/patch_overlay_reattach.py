#!/usr/bin/env python3
"""
patch_overlay_reattach.py

The OSD overlay survives the screen going away, and comes back with it.

THE BUG
-------
When the monitor is switched to another input or into standby, hot-plug
drops and the overlay's output disappears. gtk4-layer-shell then closes
the overlay window, and with no window left the GTK application quits:
the overlay process simply ended. Nothing restarted it until the
heartbeat check gave up on it, so the picture came back without the
overlay, then about 45 s later Lynx restarted to bring it back. Seen on
the ROCK 5T on 22 Oct 2026, and confirmed on a test compositor by
unplugging a virtual output under a layer-shell window: map, close-request,
unmap, app exited.

THE FIX
-------
  1. The overlay refuses close requests and hides instead. A vanished
     output is not a reason to stop.
  2. The application holds itself alive with no window showing.
  3. When a monitor appears, the overlay re-shows itself half a second
     later, which gives it a fresh layer surface on the new output.

Tested on the same rig: the close is refused, the window hides, drawing
stops while there is no output, and resumes at the normal 10 frames a
second within 0.6 s of the output returning.

The heartbeat check in lynx_start.sh already pauses while no screen is
connected (patch_heartbeat_no_screen.py), so a hidden overlay is never
mistaken for a wedged one.

Run from the repo root:  python3 patch_overlay_reattach.py
Then:                    python3 -m py_compile lynx_overlay.py
"""

import sys
from pathlib import Path

TARGET = Path("lynx_overlay.py")


def apply(src: str, old: str, new: str, label: str, count: int = 1) -> str:
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    src = TARGET.read_text()
    if "_on_close_request" in src:
        sys.exit("ABORT: already patched")

    src = apply(
        src,
        "        self.set_decorated(False)\n",
        "        self.set_decorated(False)\n"
        "        # A vanished output closes a layer-shell window, which would\n"
        "        # end the whole overlay - see patch_overlay_reattach.py.\n"
        "        self.connect(\"close-request\", self._on_close_request)\n",
        "window: refuse close")

    src = apply(
        src,
        "    def tick(self):\n",
        "    def _on_close_request(self, *args):\n"
        "        \"\"\"Hide rather than close. gtk4-layer-shell closes the window\n"
        "        when its output disappears (monitor switched to another input,\n"
        "        or into standby); LynxOverlayApp re-shows it when a monitor\n"
        "        comes back.\"\"\"\n"
        "        print(\"[overlay] output gone - hiding until a monitor returns\", flush=True)\n"
        "        self.set_visible(False)\n"
        "        return True\n"
        "\n"
        "    def tick(self):\n",
        "window: close handler")

    src = apply(
        src,
        "    def do_activate(self):\n"
        "        win = LynxOverlay()\n"
        "        win.set_application(self)\n"
        "        win.present()\n",
        "    def do_activate(self):\n"
        "        # Stay alive with no window showing: while the screen is away\n"
        "        # the overlay is hidden, not gone.\n"
        "        self.hold()\n"
        "        win = LynxOverlay()\n"
        "        win.set_application(self)\n"
        "        win.present()\n"
        "        self.win = win\n"
        "        monitors = Gdk.Display.get_default().get_monitors()\n"
        "        monitors.connect(\"items-changed\", self._on_monitors_changed)\n"
        "\n"
        "    def _on_monitors_changed(self, model, position, removed, added):\n"
        "        \"\"\"A monitor came (back): re-show the overlay on it. Hiding and\n"
        "        showing again gives the window a fresh layer surface on the new\n"
        "        output. Half a second's delay lets the compositor finish setting\n"
        "        the output up first.\"\"\"\n"
        "        if model.get_n_items() == 0:\n"
        "            return\n"
        "        def reshow():\n"
        "            print(\"[overlay] monitor back - re-showing\", flush=True)\n"
        "            self.win.set_visible(False)\n"
        "            self.win.set_visible(True)\n"
        "            return False\n"
        "        GLib.timeout_add(500, reshow)\n",
        "app: hold and re-show")

    TARGET.write_text(src)


if __name__ == "__main__":
    main()
