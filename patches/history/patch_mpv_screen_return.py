#!/usr/bin/env python3
"""
patch_mpv_screen_return.py

mpv gets its picture back when the screen comes back.

THE BUG
-------
Left on a DVB-T MPEG-2 service for two hours on 22 Oct 2026, the ROCK
came back with sound but no picture. The monitor had gone to sleep
(hot-plug dropped) and, while it was away, Lynx restarted mpv (about
13:43). An mpv started with no screen cannot set up its video output: it
plays the sound and reports "no video", and nothing ever told it to try
again when the screen woke at 14:06. The overlay had the same weakness
and was fixed in patch_overlay_reattach.py; this is mpv's turn.

Two of Lynx's watchdogs can also cause such a restart for no good
reason. With no screen, nothing asks mpv for frames, so its playback
position stops advancing - which reads exactly like a frozen decoder to
the drift monitor's hard-freeze check and to the RF decoder-health
check.

THE FIX
-------
  1. screen_connected(): the kernel's view of the display connectors,
     the same rule as lynx_start.sh's heartbeat pause. Connector status
     files present and none "connected" means no screen; no status
     files at all means "assume a screen", i.e. the old behaviour.
  2. The drift monitor's hard-freeze restart and the RF decoder-health
     restart stand down while there is no screen.
  3. screen_return_monitor(): when a screen comes back, wait 6 s for the
     compositor to set it up, then ask mpv. If it is playing a source
     with a video track but has no picture (no video-params), restart it
     on the source it was last given - the one restart_mpv() records
     for network output - under the tune lock, with the usual cover.
     A source with no video, a stopped mpv, or network output on: left
     alone.
  4. Also on the screen's return (ROCK only): the configured refresh rate
     is asked for again. The LG came back at 3840x2160p60, because a
     returning monitor gets its preferred mode, and 50 Hz had only been
     set at start-up.

The re-lock restart for the HDHomeRun is deliberately untouched: a lost
and regained lock does need a fresh mpv, screen or no screen. If it
happens while the screen is away, part 3 sorts the picture out when it
returns.

Run from the repo root:  python3 patch_mpv_screen_return.py
Then:                    python3 verify.py lynx_app.py
"""

import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


def apply(src: str, old: str, new: str, label: str, count: int = 1) -> str:
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


HELPERS = '''
# ── Screen presence (see patch_mpv_screen_return.py) ─────────────
DRM_SYSFS = os.environ.get("LYNX_DRM_SYSFS", "/sys/class/drm")


def screen_connected() -> bool:
    """True unless the kernel says no display is connected. Connector
    status files present and none "connected" means no screen - a
    monitor switched to another input or asleep. No status files at
    all means assume a screen, i.e. behave exactly as before."""
    states = []
    try:
        for name in os.listdir(DRM_SYSFS):
            if not name.startswith("card") or "-" not in name or "Writeback" in name:
                continue
            try:
                with open(os.path.join(DRM_SYSFS, name, "status")) as f:
                    states.append(f.read().strip())
            except OSError:
                pass
    except OSError:
        return True
    return (not states) or ("connected" in states)


def _mpv_has_picture_problem() -> bool:
    """mpv is playing something with a video track, but has no picture.
    False when mpv is not running, the source has no video, or all is
    well."""
    vp = mpv_query({"command": ["get_property", "video-params"]})
    if vp and vp.get("error") == "success" and isinstance(vp.get("data"), dict) \\
            and vp["data"].get("h"):
        return False
    tl = mpv_query({"command": ["get_property", "track-list"]})
    if not tl or tl.get("error") != "success":
        return False          # no mpv answering: nothing to fix here
    return any(t.get("type") == "video" for t in (tl.get("data") or []))


def _reapply_refresh_rate():
    """ROCK: a monitor coming back is driven at its own preferred mode
    (60 Hz on an LG 27UK850), and lynx_start.sh only asks for the
    configured rate at start-up. Ask again: a listed mode at the current
    size first, then a custom mode - the same rule as lynx_start.sh.
    Pi: nothing, exactly as before."""
    if not lynx_platform.CUSTOM_REFRESH_MODES:
        return
    try:
        want = int((config.get('display') or {}).get('refresh_hz', 50) or 50)
    except (TypeError, ValueError):
        want = 50
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    try:
        text = subprocess.run(["wlr-randr"], capture_output=True, text=True,
                              timeout=3, env=env).stdout
    except Exception:
        return
    name, modes = None, []
    for line in text.splitlines():
        if line and not line[0].isspace():
            if name:
                break          # first output only
            name = line.split()[0]
            continue
        m = re.match(r"\\s+(\\d+x\\d+) px, ([\\d.]+) Hz(.*)", line)
        if m:
            modes.append((m.group(1), float(m.group(2)), "current" in m.group(3)))
    current = next((m for m in modes if m[2]), None)
    if not name or not current or round(current[1]) == want:
        return
    res = current[0]
    listed = next((m for m in modes if m[0] == res and round(m[1]) == want), None)
    args = (["--mode", f"{res}@{listed[1]:.6f}Hz"] if listed
            else ["--custom-mode", f"{res}@{want}Hz"])
    r = subprocess.run(["wlr-randr", "--output", name, *args],
                       capture_output=True, text=True, timeout=5, env=env)
    print(f"[screen] {name} came back at {res} {current[1]:g} Hz - asked for {want} Hz: "
          f"{'ok' if r.returncode == 0 else 'not accepted'}", flush=True)


def screen_return_monitor():
    """When a screen comes back: put the refresh rate back, then make
    sure mpv has its picture. An mpv (re)started while no screen was
    connected plays sound but never sets up its video output, and
    nothing else tells it to try again."""
    global mpv_last_started_at
    was = screen_connected()
    while True:
        time.sleep(2)
        try:
            now = screen_connected()
            if now and not was:
                print("[screen] screen back - checking mpv has its picture", flush=True)
                time.sleep(4)   # let the compositor set the output up first
                _reapply_refresh_rate()
                time.sleep(2)
                src = network_output_source
                is_rf = network_output_source_is_rf
                if (not network_output_enabled and src
                        and _mpv_has_picture_problem()
                        and tune_lock.acquire(timeout=2)):
                    try:
                        print("[screen] mpv has sound but no picture - restarting it on "
                              f"{src}", flush=True)
                        record_diagnostic_event(
                            "screen_back_restart",
                            "screen came back and mpv had no picture - restarted it")
                        start_transition_cover()
                        time.sleep(0.3)
                        restart_mpv(src, is_rf=is_rf)
                        if is_rf:
                            mpv_last_started_at = time.time()
                        wait_for_mpv_rendering()
                        end_transition_cover()
                    finally:
                        tune_lock.release()
            was = now
        except Exception as e:
            print(f"[screen] {type(e).__name__}: {e}", flush=True)


def mpv_decoder_health_monitor():'''


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    src = TARGET.read_text()
    before = len(src.splitlines())
    if "def screen_connected" in src:
        sys.exit("ABORT: already patched")

    src = apply(src, "\ndef mpv_decoder_health_monitor():", HELPERS, "helpers and monitor")

    src = apply(
        src,
        "        time.sleep(CHECK_INTERVAL)\n"
        "        try:\n"
        "            if current_mode != \"rf\" or not mpv_running_for_rf:\n"
        "                last_size = 0\n",
        "        time.sleep(CHECK_INTERVAL)\n"
        "        try:\n"
        "            # No screen: playback stalls by design (nothing asks for\n"
        "            # frames), which is not a decoder fault.\n"
        "            if current_mode != \"rf\" or not mpv_running_for_rf or not screen_connected():\n"
        "                last_size = 0\n",
        "decoder health: stand down with no screen")

    src = apply(
        src,
        "            if (hard_freeze_detected_at > 0 and\n",
        "            if (hard_freeze_detected_at > 0 and\n"
        "                    hard_freeze_detected_at != last_handled_hard_freeze_at and\n"
        "                    hard_freeze_target and not screen_connected()):\n"
        "                # No screen: nothing asks mpv for frames, so playback\n"
        "                # stopping is expected. Restarting here would start an\n"
        "                # mpv that cannot set up its picture at all - see\n"
        "                # patch_mpv_screen_return.py.\n"
        "                print(\"[mpv_drift] playback stopped with no screen connected - \"\n"
        "                      \"expected, not restarting\")\n"
        "                last_handled_hard_freeze_at = hard_freeze_detected_at\n"
        "            if (hard_freeze_detected_at > 0 and\n",
        "drift: no hard-freeze restart with no screen")

    src = apply(
        src,
        "    connectivity = threading.Thread(target=picotuner_connectivity_monitor, daemon=True)\n",
        "    screen_return = threading.Thread(target=screen_return_monitor, daemon=True)\n"
        "    screen_return.start()\n"
        "    connectivity = threading.Thread(target=picotuner_connectivity_monitor, daemon=True)\n",
        "thread start")

    src = apply(
        src,
        '    "drift_hard_freeze_restart": "Hard freeze detected - immediate mpv restart",\n',
        '    "drift_hard_freeze_restart": "Hard freeze detected - immediate mpv restart",\n'
        '    "screen_back_restart": "Screen came back with no picture - mpv restarted",\n',
        "diagnostics label")

    TARGET.write_text(src)
    print(f"lynx_app.py: {before} -> {len(src.splitlines())} lines")


if __name__ == "__main__":
    main()
