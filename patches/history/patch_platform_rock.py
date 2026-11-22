#!/usr/bin/env python3
"""
patch_platform_rock.py

Lynx runs on the Radxa ROCK 5T from stock beta code, with no hand edits.
Everything that differs between boards now comes from lynx_platform.py
(new, beside this file); a Raspberry Pi behaves exactly as before.

WHAT CHANGES, AND ONLY ON THE ROCK
---------------------------------
  mpv          --vo=dmabuf-wayland --hwdec=rkmpp instead of --vo=gpu
               --hwdec=drm-copy (zero-copy hardware decode; the copying
               path ran at 282% CPU and dropped frames)
  Wayland      mpv keeps WAYLAND_DISPLAY. The Pi strips it to force
               Xwayland; on the ROCK that leaves mpv nowhere to draw
               ("Error opening/initializing the VO window")
  Height cap   2160 by default instead of 1080. An explicit max_height
               in the config still wins, on either board
  Refresh      When the monitor is not at the configured rate (50 Hz),
               lynx_start.sh asks for it: a listed mode first, then a
               custom mode. The LG 27UK850 lists 4K only at 60/30/25
               but takes 4K50 happily

CHANGED FOR EVERY BOARD (harmless, and fixes found on the way)
-------------------------------------------------------------
  * /api/config fills in the effective max_height when the config file
    has none. Without it the config page fell back to 1080, and saving
    the Screen card would quietly cap a ROCK at 1080.
  * The Wi-Fi power-save line only runs when there is a wlan0. With
    none, `iw` printed its whole help text into the log every cycle.
  * verify.py says "SKIP" instead of crashing when Node.js is missing,
    and refuses a Node too old for optional chaining (?.) instead of
    reporting the page as broken. Both happened on 21 Oct 2026.

Run from the repo root:  python3 patch_platform_rock.py
Then:                    python3 verify.py lynx_app.py
Needs lynx_platform.py in the repo root.
"""

import sys
from pathlib import Path


def apply(src: str, old: str, new: str, label: str, count: int = 1) -> str:
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


def patch_app():
    p = Path("lynx_app.py")
    src = p.read_text()
    before = len(src.splitlines())
    if "import lynx_platform" in src:
        sys.exit("ABORT: lynx_app.py already patched")

    src = apply(src, "import lynx_notifications\n",
                "import lynx_notifications\n"
                "import lynx_platform   # what differs between boards - see that file\n",
                "app: import")

    src = apply(
        src,
        "config = load_config()\n\n# ── GNSS (portable locator)",
        "config = load_config()\n"
        "\n"
        "def display_max_height() -> int:\n"
        "    \"\"\"The display height cap in force: the config's if it sets one,\n"
        "    otherwise the board's default (1080 on a Pi, 2160 on a ROCK 5T).\n"
        "    lynx_start.sh applies the same rule at start-up.\"\"\"\n"
        "    try:\n"
        "        v = (config.get('display') or {}).get('max_height')\n"
        "        return int(v) if v is not None else lynx_platform.DEFAULT_MAX_HEIGHT\n"
        "    except (TypeError, ValueError):\n"
        "        return lynx_platform.DEFAULT_MAX_HEIGHT\n"
        "\n"
        "print(f\"[platform] {lynx_platform.BOARD_NAME} ({lynx_platform.MODEL or 'model unknown'}): \"\n"
        "      f\"mpv {lynx_platform.MPV_VIDEO_FLAGS}, \"\n"
        "      f\"height cap {display_max_height()}\")\n"
        "\n"
        "# ── GNSS (portable locator)",
        "app: display_max_height()")

    src = apply(
        src,
        'f"--cursor-autohide=always --force-window=yes --vo=gpu --hwdec=drm-copy --mute=yes "',
        'f"--cursor-autohide=always --force-window=yes {lynx_platform.MPV_VIDEO_FLAGS} --mute=yes "',
        "app: mpv video flags")

    src = apply(
        src,
        "    env.pop(\"WAYLAND_DISPLAY\", None)\n",
        "    #\n"
        "    # Pi only. On the ROCK 5T mpv is Wayland-only (dmabuf-wayland),\n"
        "    # so it must keep the variable - see lynx_platform.py.\n"
        "    if lynx_platform.KEEP_WAYLAND_DISPLAY:\n"
        "        env.setdefault(\"WAYLAND_DISPLAY\", \"wayland-0\")\n"
        "    else:\n"
        "        env.pop(\"WAYLAND_DISPLAY\", None)\n",
        "app: WAYLAND_DISPLAY per board")

    src = apply(
        src,
        "    max_height: int = 1080\n",
        "    max_height: int = lynx_platform.DEFAULT_MAX_HEIGHT\n",
        "app: model default")

    src = apply(
        src,
        "            \"max_height\": config.get('display', {}).get('max_height', 1080),\n",
        "            \"max_height\": display_max_height(),\n"
        "            \"board\": lynx_platform.BOARD,\n"
        "            \"board_name\": lynx_platform.BOARD_NAME,\n",
        "app: status")

    src = apply(
        src,
        "def get_config():\n    return config\n",
        "def get_config():\n"
        "    # The effective max_height is filled in when the file has none:\n"
        "    # the config page round-trips it, and falling back to 1080 there\n"
        "    # would cap a ROCK 5T at 1080 the first time Screen was saved.\n"
        "    display = dict(config.get('display') or {})\n"
        "    display.setdefault('max_height', display_max_height())\n"
        "    return {**config, 'display': display}\n",
        "app: /api/config")

    src = apply(
        src,
        "                            Lynx caps the screen at 1080 lines, switching a 4K monitor down when it\n"
        "                            starts. A 4K screen is more than a Raspberry Pi can drive while also\n",
        "                            On a Raspberry Pi, Lynx caps the screen at 1080 lines, switching a 4K\n"
        "                            monitor down when it starts. A 4K screen is more than a Pi can drive while also\n",
        "app: Screen card wording")
    src = apply(
        src,
        "                            same picture. An ultrawide monitor is unaffected.\n",
        "                            same picture. An ultrawide monitor is unaffected. On a ROCK 5T the full 4K\n"
        "                            output is kept.\n",
        "app: Screen card ROCK note")

    p.write_text(src)
    print(f"lynx_app.py: {before} -> {len(src.splitlines())} lines")


def patch_start():
    p = Path("lynx_start.sh")
    src = p.read_text()
    if "lynx_platform.py" in src:
        sys.exit("ABORT: lynx_start.sh already patched")

    src = apply(
        src,
        'LYNX_MAX_HEIGHT="${LYNX_MAX_HEIGHT:-$(_cfg_display max_height 1080)}"\n',
        "# The board's own default when the config sets none: 1080 on a Pi,\n"
        "# 2160 on a ROCK 5T (lynx_platform.py). Unknown board, or the file\n"
        "# missing: 1080, exactly as before.\n"
        'LYNX_PLATFORM_MAX=$(python3 "${LYNX_DIR}/lynx_platform.py" default_max_height 2>/dev/null || echo 1080)\n'
        'LYNX_MAX_HEIGHT="${LYNX_MAX_HEIGHT:-$(_cfg_display max_height ${LYNX_PLATFORM_MAX:-1080})}"\n',
        "start: platform height default")

    src = apply(
        src,
        "            echo -e \"${AMBER}no usable ${LYNX_MAX_HEIGHT}p mode - leaving as-is${NC}\"\n"
        "        fi\n"
        "    fi\n"
        "else\n"
        "    echo \"wlr-randr not found - display height not capped.\"\n",
        "            echo -e \"${AMBER}no usable ${LYNX_MAX_HEIGHT}p mode - leaving as-is${NC}\"\n"
        "        fi\n"
        "    fi\n"
        "\n"
        "    # ROCK 5T: the height is usually fine as it is, but the refresh rate\n"
        "    # still matters - a 4K monitor comes up at 60 Hz, and 25 fps on\n"
        "    # 60 Hz judders. Ask for the configured rate at the current size:\n"
        "    # a listed mode first, then a custom one (an LG 27UK850 lists 4K\n"
        "    # only at 60/30/25 but takes 4K50 happily). Pi: skipped, unchanged.\n"
        "    if [ \"$(python3 \"${LYNX_DIR}/lynx_platform.py\" custom_refresh_modes 2>/dev/null)\" = \"1\" ] \\\n"
        "       && [ -n \"$_out\" ]; then\n"
        "        _now=$(wlr-randr 2>/dev/null | awk '/current/ {print $1, $3; exit}')\n"
        "        _res=${_now%% *}; _hz=${_now##* }\n"
        "        if [ -n \"$_res\" ] && [ -n \"$_hz\" ] \\\n"
        "           && [ \"$(printf '%.0f' \"$_hz\" 2>/dev/null)\" != \"$LYNX_REFRESH_HZ\" ]; then\n"
        "            echo -ne \"Display is ${_res} at ${_hz} Hz - asking for ${LYNX_REFRESH_HZ} Hz... \"\n"
        "            _listed=$(wlr-randr 2>/dev/null | awk -v m=\"$_res\" -v r=\"$LYNX_REFRESH_HZ\" \\\n"
        "                '$1 == m && $2 == \"px,\" && int($3 + 0.5) == r {print $1 \"@\" $3; exit}')\n"
        "            if [ -n \"$_listed\" ] && wlr-randr --output \"$_out\" --mode \"$_listed\" > /dev/null 2>&1; then\n"
        "                echo -e \"${GREEN}${_listed}${NC}\"\n"
        "                sleep 2\n"
        "            elif wlr-randr --output \"$_out\" --custom-mode \"${_res}@${LYNX_REFRESH_HZ}Hz\" > /dev/null 2>&1; then\n"
        "                echo -e \"${GREEN}${_res}@${LYNX_REFRESH_HZ}Hz (custom mode)${NC}\"\n"
        "                sleep 2\n"
        "            else\n"
        "                echo -e \"${AMBER}not accepted - leaving ${_hz} Hz${NC}\"\n"
        "            fi\n"
        "        fi\n"
        "    fi\n"
        "else\n"
        "    echo \"wlr-randr not found - display height not capped.\"\n",
        "start: ROCK refresh rate")

    src = apply(
        src,
        "iw wlan0 set power_save off 2>/dev/null || true",
        "{ [ -e /sys/class/net/wlan0 ] && iw wlan0 set power_save off > /dev/null 2>&1; } || true",
        "start: Wi-Fi line only with a wlan0", count=2)

    p.write_text(src)


def patch_verify():
    p = Path("verify.py")
    src = p.read_text()
    if "_node_usable" in src:
        sys.exit("ABORT: verify.py already patched")

    src = apply(src, "import ast\n", "import ast\nimport shutil\n", "verify: import")

    src = apply(
        src,
        "    for n, size, ok, msg in check_scripts(path):\n",
        "    node_ok, node_msg = _node_usable()\n"
        "    if not node_ok:\n"
        "        print(f\"  javascript       SKIP   {node_msg}\")\n"
        "    for n, size, ok, msg in (check_scripts(path) if node_ok else []):\n",
        "verify: node check")

    src = apply(
        src,
        "\ndef main():\n",
        "\ndef _node_usable():\n"
        "    \"\"\"Node.js present, and new enough for the page's optional\n"
        "    chaining (?.), which needs Node 14. An old Node reports every\n"
        "    script block as broken when nothing is wrong with them - it did,\n"
        "    on a Mac, on 21 Oct 2026.\"\"\"\n"
        "    if not shutil.which(\"node\"):\n"
        "        return False, \"node not installed - JavaScript NOT checked (install nodejs)\"\n"
        "    try:\n"
        "        v = subprocess.run([\"node\", \"--version\"], capture_output=True,\n"
        "                           text=True).stdout.strip()\n"
        "        if int(v.lstrip(\"v\").split(\".\")[0]) < 14:\n"
        "            return False, f\"node {v} too old (need 14+) - JavaScript NOT checked\"\n"
        "    except (ValueError, OSError):\n"
        "        pass\n"
        "    return True, \"\"\n"
        "\n"
        "\ndef main():\n",
        "verify: _node_usable()")

    p.write_text(src)


def main():
    for f in ("lynx_app.py", "lynx_start.sh", "verify.py", "lynx_platform.py"):
        if not Path(f).exists():
            sys.exit(f"ABORT: {f} not found - run from the repo root, with lynx_platform.py beside this")
    patch_app()
    patch_start()
    patch_verify()


if __name__ == "__main__":
    main()
