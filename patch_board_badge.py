#!/usr/bin/env python3
"""
patch_board_badge.py

Receiver page: a board badge showing processor temperature and fan
speed, beside the IN and OUT video badges. Both boards.

  ROCK 5T: soc-thermal, and the pwm-fan PWM held by lynx-fan (see
           install/lynx_fan.py). No RPM: the 5T's fan header has no
           tachometer pin.
  Pi 5:    cpu-thermal, and the official Active Cooler, whose driver also
           reports RPM - shown when it is there.

The badge reads, for example, "38°C fan 50%" (with RPM where known).
Amber from 60 C (where the kernel starts stepping the fan), red from
80 C (the CPU throttles at 85), and red with STALLED when the fan is
asked to run but reports 0 rpm. The tooltip has the detail.

The numbers are read by the same thread as the video badges, every 3 s,
and cached; /api/status only reads them, as lynx.board:
  {"temp_c": 38.2, "sensor": "soc-thermal", "fan_pwm": 128,
   "fan_pct": 50, "fan_rpm": null}
That is also the shape SNMP will publish later.

Run from the repo root:  python3 patch_board_badge.py
Then:                    python3 verify.py lynx_app.py
"""

import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


def apply(src, old, new, label, count=1):
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


READER = '''board_info = None        # processor temperature and fan - see patch_board_badge.py
THERMAL_SYSFS = os.environ.get("LYNX_THERMAL_SYSFS", "/sys/class")


def _read_board():
    """Processor temperature and fan speed, from the kernel. Prefers the
    ROCK's soc-thermal or the Pi's cpu-thermal sensor, otherwise the
    first one. None if there is no sensor at all."""
    import glob as _glob
    def _rd(p):
        with open(p) as f:
            return f.read().strip()
    zones = {}
    for z in sorted(_glob.glob(f"{THERMAL_SYSFS}/thermal/thermal_zone*")):
        try:
            zones[_rd(f"{z}/type")] = z
        except OSError:
            pass
    if not zones:
        return None
    sensor = next((n for n in ("soc-thermal", "cpu-thermal") if n in zones),
                  next(iter(zones)))
    try:
        temp_c = round(int(_rd(f"{zones[sensor]}/temp")) / 1000.0, 1)
    except (OSError, ValueError):
        return None
    info = {"temp_c": temp_c, "sensor": sensor,
            "fan_pwm": None, "fan_pct": None, "fan_rpm": None}
    for h in sorted(_glob.glob(f"{THERMAL_SYSFS}/hwmon/hwmon*")):
        try:
            if _rd(f"{h}/name") not in ("pwmfan", "pwm-fan", "cooling_fan"):
                continue
            pwm = int(_rd(f"{h}/pwm1"))
            info["fan_pwm"] = pwm
            info["fan_pct"] = round(pwm * 100 / 255)
            try:
                info["fan_rpm"] = int(_rd(f"{h}/fan1_input"))
            except (OSError, ValueError):
                pass
            break
        except (OSError, ValueError):
            continue
    return info


def video_badge_monitor():
    global video_in_info, video_out_info, board_info'''

JS = '''// Board badge - see patch_board_badge.py.
function renderBoardBadge(b) {
    const el = document.getElementById('board-badge');
    if (!el) return;
    if (!b || b.temp_c === null || b.temp_c === undefined) {
        el.style.display = 'none';
        return;
    }
    el.style.display = '';
    const deg = String.fromCharCode(176);
    let stalled = false;
    let txt = Math.round(b.temp_c) + deg + 'C';
    let tip = 'Processor ' + b.temp_c + deg + 'C (' + b.sensor + ')';
    if (b.fan_pct !== null && b.fan_pct !== undefined) {
        txt += b.fan_pct > 0 ? ' fan ' + b.fan_pct + '%' : ' fan off';
        tip += '. Fan ' + (b.fan_pct > 0 ? b.fan_pct + '% (PWM ' + b.fan_pwm + ')' : 'off');
        if (b.fan_pct > 0 && b.fan_rpm !== null && b.fan_rpm !== undefined) {
            if (b.fan_rpm === 0) {
                stalled = true;
                txt += ' STALLED';
                tip += ', but not turning (0 rpm)';
            } else {
                txt += ' ' + b.fan_rpm + 'rpm';
                tip += ', ' + b.fan_rpm + ' rpm';
            }
        }
    }
    el.textContent = txt;
    el.title = tip;
    el.style.background = (stalled || b.temp_c >= 80) ? '#c0392b'
                        : b.temp_c >= 60 ? '#b7791f' : '#3a4a63';
}

async function updateStatus() {'''


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    src = TARGET.read_text()
    if "def _read_board" in src:
        sys.exit("ABORT: already patched")
    before = len(src.splitlines())

    src = apply(src, "def video_badge_monitor():\n"
                     "    global video_in_info, video_out_info",
                READER, "board reader")
    src = apply(src,
                "            vin = _read_video_in()\n"
                "            with _video_badge_lock:\n",
                "            vin = _read_video_in()\n"
                "            brd = _read_board()\n"
                "            with _video_badge_lock:\n"
                "                board_info = brd\n",
                "monitor: read the board too")
    src = apply(src, '            "video_out": video_out_info,\n',
                '            "video_out": video_out_info,\n'
                '            "board": board_info,\n',
                "status: lynx.board")
    src = apply(src,
                'title="HDMI output">OUT ?</span>\n',
                'title="HDMI output">OUT ?</span>\n'
                '            <span class="btn btn-sm" id="board-badge" style="background:#3a4a63; '
                'color:#fff; cursor:default; display:none;" title="Processor temperature and fan">'
                '--</span>\n',
                "page: board badge")
    src = apply(src, "async function updateStatus() {", JS, "page: badge renderer")
    src = apply(src,
                "        renderVideoBadges(s.lynx?.video_in, s.lynx?.video_out);\n",
                "        renderVideoBadges(s.lynx?.video_in, s.lynx?.video_out);\n"
                "        renderBoardBadge(s.lynx?.board);\n",
                "page: call the renderer")

    TARGET.write_text(src)
    print(f"lynx_app.py: {before} -> {len(src.splitlines())} lines")


if __name__ == "__main__":
    main()
