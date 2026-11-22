#!/usr/bin/env python3
"""
patch_rock_mpeg2_software.py

ROCK 5T: MPEG-2 decoded in software, deinterlaced by mpv.

patch_rock_codecs_dvbt_freq.py sent MPEG-2 to the hardware decoder
(--hwdec-codecs=all). On the ROCK that gave a solid purple picture: the
RK3588 decodes MPEG-2 on an older decoder block, separate from the HEVC
and H.264 one, and on Armbian's vendor kernel it does not work (MPP logs
"client 12 driver is not ready!" at start-up). The frames come back
empty.

So MPEG-2 stays in software, which at UK SD (544x576 or 720x576) costs
the RK3588 next to nothing, and gpu-next displays it. mpv's own default
--hwdec-codecs list (h264, vc1, hevc, vp8, vp9, av1, ...) already leaves
MPEG-2 out, so the option is simply dropped.

--deinterlace=auto deinterlaces only frames still flagged interlaced:
software-decoded 576i MPEG-2 gets deinterlaced; H.264 and HEVC from the
hardware arrive already bobbed to 50p by the RK3588's deinterlacer and
are left alone.

Tested on 22 Oct 2026: GB News and another SD service (MPEG-2) with
picture; BBC TWO HD on PSB3 (H.264) still in hardware.

Run from the repo root:  python3 patch_rock_mpeg2_software.py
"""

import sys
from pathlib import Path


def apply(src: str, old: str, new: str, label: str) -> str:
    n = src.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected 1")
    print(f"  ok  {label}")
    return src.replace(old, new, 1)


def main():
    p = Path("lynx_platform.py")
    if not p.exists():
        sys.exit("ABORT: lynx_platform.py not found - run from the repo root")
    src = p.read_text()
    if "--deinterlace=auto" in src:
        sys.exit("ABORT: already patched")
    src = apply(
        src,
        "#         frames. --hwdec-codecs=all because mpv's default list leaves\n"
        "#         out MPEG-2 (UK SD broadcast). gpu-next rather than\n"
        "#         dmabuf-wayland: the same cost, but it can also show a\n"
        "#         software-decoded picture, where dmabuf-wayland shows black.\n"
        'MPV_VIDEO_FLAGS = ("--vo=gpu-next --hwdec=rkmpp --hwdec-codecs=all" if IS_ROCK\n',
        "#         frames. MPEG-2 (UK SD broadcast) deliberately stays in\n"
        "#         software: the RK3588's MPEG-2 block does not work on this\n"
        "#         kernel and returns a purple picture, and mpv's default\n"
        "#         hardware list already leaves MPEG-2 out. gpu-next rather\n"
        "#         than dmabuf-wayland: the same cost, but it can also show a\n"
        "#         software-decoded picture, where dmabuf-wayland shows none.\n"
        "#         --deinterlace=auto: only frames still flagged interlaced\n"
        "#         (software 576i); hardware frames arrive already bobbed.\n"
        'MPV_VIDEO_FLAGS = ("--vo=gpu-next --hwdec=rkmpp --deinterlace=auto" if IS_ROCK\n',
        "platform: ROCK mpv flags")
    p.write_text(src)


if __name__ == "__main__":
    main()
