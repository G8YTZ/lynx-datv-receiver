# Copyright (C) 2026 Justin, G8YTZ / EI3IOB
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
"""lynx_platform.py - the few things that differ between the boards
Lynx runs on, in one place.

Everything else in Lynx (tuning, the Picotuner, HDHomeRun, the overlay,
the web UI) is the same on every board. When something genuinely has to
differ, it is decided here and nowhere else, so lynx_app.py and
lynx_start.sh ask this module rather than each guessing for themselves.

Boards:
  pi      Raspberry Pi 5 / 4 (and anything not recognised: an unknown
          board gets exactly the behaviour Lynx had before this file
          existed, so nothing changes for anyone by accident)
  rock5t  Radxa ROCK 5T on Armbian trixie

Every value below was found the hard way on 21 Oct 2026, porting Lynx
to the ROCK 5T. The reason is given beside each one.

For testing, LYNX_PLATFORM=pi or LYNX_PLATFORM=rock5t overrides the
detection.

From the shell:  python3 lynx_platform.py                 (all values)
                 python3 lynx_platform.py default_max_height
"""

import os
import sys
from pathlib import Path


def _model() -> str:
    try:
        return (Path("/proc/device-tree/model").read_bytes()
                .rstrip(b"\0").decode(errors="replace"))
    except OSError:
        return ""


MODEL = _model()

_override = os.environ.get("LYNX_PLATFORM", "").strip().lower()
if _override in ("pi", "rock5t"):
    BOARD = _override
elif "ROCK 5T" in MODEL.upper():
    BOARD = "rock5t"
else:
    BOARD = "pi"

IS_ROCK = BOARD == "rock5t"
# The Pi 5 gets its own video path (below); a Pi 4 keeps the old one.
IS_PI5 = not IS_ROCK and "RASPBERRY PI 5" in MODEL.upper()
BOARD_NAME = "Radxa ROCK 5T" if IS_ROCK else "Raspberry Pi"

# How mpv decodes and draws.
#   Pi:   the GPU path with drm-copy, as proven over months of use.
#   ROCK: Rockchip's hardware decoder (rkmpp, via ffmpeg-rockchip and
#         Lynx's patched mpv), zero-copy: 4K50 HEVC at a few per cent
#         of the machine; the copying path ran at 282% CPU and dropped
#         frames. MPEG-2 (UK SD broadcast) deliberately stays in
#         software: the RK3588's MPEG-2 block does not work on this
#         kernel and returns a purple picture, and mpv's default
#         hardware list already leaves MPEG-2 out. gpu-next rather
#         than dmabuf-wayland: the same cost, but it can also show a
#         software-decoded picture, where dmabuf-wayland shows none.
#         --deinterlace=auto: only frames still flagged interlaced
#         (software 576i); hardware frames arrive already bobbed.
#   Pi 5: the HEVC decoder's frames go straight to the GPU (hwdec=drm, no
#         copy), drawn by gpu-next through OpenGL on native Wayland. With
#         drm-copy every decoded frame was copied by the processor: fine
#         at 1080, never smooth at 4K. gpu-next, not dmabuf-wayland: the
#         Pi 5 decodes only HEVC in hardware, and dmabuf-wayland cannot
#         show the software-decoded H.264 that most streams are. OpenGL,
#         not Vulkan: native Wayland froze with Vulkan's swapchain on the
#         Pi. Tested 24 Oct 2026 on a Pi 5 into an LG 4K at 2160p25:
#         4K HEVC from Lynx-Tx as smooth as a Zgemma, and every H.264
#         stream tried played.
#   Pi 4: unchanged until it has been tried there.
if IS_ROCK:
    MPV_VIDEO_FLAGS = "--vo=gpu-next --hwdec=rkmpp --deinterlace=auto"
elif IS_PI5:
    MPV_VIDEO_FLAGS = "--vo=gpu-next --gpu-api=opengl --hwdec=drm"
else:
    MPV_VIDEO_FLAGS = "--vo=gpu --hwdec=drm-copy"
# For trying other video paths without editing code (Pi 5 4K, 24 Oct 2026):
#   systemctl --user set-environment LYNX_MPV_VIDEO_FLAGS="--vo=dmabuf-wayland --hwdec=drm" LYNX_KEEP_WAYLAND=1
#   systemctl --user restart lynx
# and back:  systemctl --user unset-environment LYNX_MPV_VIDEO_FLAGS LYNX_KEEP_WAYLAND
MPV_VIDEO_FLAGS = os.environ.get("LYNX_MPV_VIDEO_FLAGS") or MPV_VIDEO_FLAGS

# Whether mpv keeps WAYLAND_DISPLAY.
#   Pi 4: removed, forcing mpv onto Xwayland - native Wayland froze on
#         the Pi (Vulkan swapchain failures; see restart_mpv()).
#   Pi 5: kept, for the no-copy path above (OpenGL, not Vulkan).
#   ROCK: kept. Lynx's mpv there is built Wayland-only, and dmabuf-
#         wayland is Wayland by definition: without the variable it has
#         nowhere to draw ("Error opening/initializing the VO window").
KEEP_WAYLAND_DISPLAY = IS_ROCK or IS_PI5 or os.environ.get("LYNX_KEEP_WAYLAND") == "1"

# Default display height cap, when the config does not set one.
#   Pi:   1080. At 4K a Pi 5 glitches, drops audio and loses the overlay.
#   ROCK: 2160. It drives 4K with most of the machine idle.
DEFAULT_MAX_HEIGHT = 2160 if IS_ROCK else 1080

# Whether lynx_start.sh may ask for the configured refresh rate as a
# custom mode when the monitor does not list it. Many 4K PC monitors
# (an LG 27UK850, for one) accept 3840x2160 at 50 Hz but list only 60,
# 30 and 25; at 60 Hz a 25 fps picture judders. ROCK and Pi 5 (24 Oct
# 2026: an LG 4K left at 60 Hz judders a 25 fps picture on the Pi 5 too,
# and a screen plugged in after start-up kept its own 60 Hz). This also
# puts the configured rate back when a screen is plugged in later. Pi 4:
# exactly as it was.
CUSTOM_REFRESH_MODES = IS_ROCK or IS_PI5

# HDMI outputs Lynx may drive as separate receivers (Lynx+). The Pi 5
# has two sockets but one HEVC decoder, so its second output is shown
# greyed out: "HDMI 2: ROCK 5T only".
HDMI_OUTPUTS = 2 if IS_ROCK else 1


def as_dict() -> dict:
    return {
        "board": BOARD,
        "board_name": BOARD_NAME,
        "model": MODEL,
        "mpv_video_flags": MPV_VIDEO_FLAGS,
        "keep_wayland_display": KEEP_WAYLAND_DISPLAY,
        "default_max_height": DEFAULT_MAX_HEIGHT,
        "custom_refresh_modes": CUSTOM_REFRESH_MODES,
        "hdmi_outputs": HDMI_OUTPUTS,
    }


if __name__ == "__main__":
    values = as_dict()
    if len(sys.argv) > 1:
        v = values.get(sys.argv[1])
        if v is None:
            sys.exit(f"unknown value: {sys.argv[1]}")
        # Shell-friendly: booleans as 1/0.
        print(int(v) if isinstance(v, bool) else v)
    else:
        for k, v in values.items():
            print(f"{k}={v}")
