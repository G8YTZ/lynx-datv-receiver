#!/usr/bin/env python3
"""
patch_rock_codecs_dvbt_freq.py

Two fixes found on 22 Oct 2026 watching Freeview on the ROCK 5T.

1. NO PICTURE ON MPEG-2 (ROCK only)
   ArqB (SD, MPEG-2 video, MP2 sound): sound and overlay, black picture.
   mpv only hardware-decodes codecs on its default --hwdec-codecs list,
   and MPEG-2 is not on it, so it was decoded in software ("mpeg2video",
   not "mpeg2_rkmpp"). The ROCK's output, dmabuf-wayland, can only show
   hardware (DMA-buffer) frames, so software-decoded pictures went
   nowhere.

   Now: --hwdec-codecs=all, so MPEG-2 (and anything else the RK3588's
   decoder can do) is decoded in hardware; and --vo=gpu-next instead of
   dmabuf-wayland, which shows software frames as well, so a codec the
   hardware cannot handle still gets a picture rather than black.
   Measured on 21 Oct: zero-copy rkmpp through gpu-next costs about the
   same CPU as dmabuf-wayland. The Pi is unchanged.

2. CHANGING SERVICE ON A DVB-T2 MULTIPLEX DID NOTHING (both boards)
   The DVB-T card's frequency box starts at 437000 kHz (70 cm) and only
   followed a tune made from that card. After tuning PSB3 from any other
   preset, choosing another service retuned to 437 MHz with that
   service's number, which cannot lock. Exposed by
   patch_dvbt_service_list.py, which made the service list fill however
   the multiplex was tuned. The box now follows the frequency the tuner
   is actually locked to, whenever the multiplex changes (but never
   while somebody is typing in it).

Run from the repo root:  python3 patch_rock_codecs_dvbt_freq.py
Then:                    python3 verify.py lynx_app.py
"""

import sys
from pathlib import Path


def apply(src: str, old: str, new: str, label: str, count: int = 1) -> str:
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


def main():
    for f in ("lynx_platform.py", "lynx_app.py"):
        if not Path(f).exists():
            sys.exit(f"ABORT: {f} not found - run this from the repo root")

    p = Path("lynx_platform.py")
    src = p.read_text()
    if "--hwdec-codecs=all" in src:
        sys.exit("ABORT: already patched")
    src = apply(
        src,
        "#   ROCK: Rockchip's hardware decoder (rkmpp, via ffmpeg-rockchip and\n"
        "#         Lynx's patched mpv), frames handed straight to the compositor\n"
        "#         as DMA buffers. 4K50 HEVC at about 7% of the machine; the\n"
        "#         copying path ran at 282% CPU and dropped frames.\n"
        'MPV_VIDEO_FLAGS = ("--vo=dmabuf-wayland --hwdec=rkmpp" if IS_ROCK\n',
        "#   ROCK: Rockchip's hardware decoder (rkmpp, via ffmpeg-rockchip and\n"
        "#         Lynx's patched mpv), zero-copy: 4K50 HEVC at a few per cent\n"
        "#         of the machine; the copying path ran at 282% CPU and dropped\n"
        "#         frames. --hwdec-codecs=all because mpv's default list leaves\n"
        "#         out MPEG-2 (UK SD broadcast). gpu-next rather than\n"
        "#         dmabuf-wayland: the same cost, but it can also show a\n"
        "#         software-decoded picture, where dmabuf-wayland shows black.\n"
        'MPV_VIDEO_FLAGS = ("--vo=gpu-next --hwdec=rkmpp --hwdec-codecs=all" if IS_ROCK\n',
        "platform: ROCK mpv flags")
    p.write_text(src)

    p = Path("lynx_app.py")
    src = p.read_text()
    # No backslashes below: this lands inside a plain Python string.
    src = apply(
        src,
        "                            window._dvbtProgramsFor = fkey;\n",
        "                            window._dvbtProgramsFor = fkey;\n"
        "                            // The frequency box follows the multiplex\n"
        "                            // actually locked, so changing service retunes\n"
        "                            // the right one - see\n"
        "                            // patch_rock_codecs_dvbt_freq.py. Not while\n"
        "                            // somebody is typing in it.\n"
        "                            if (stale) {\n"
        "                                var fbox = document.getElementById('dvbt-freq');\n"
        "                                if (fbox && document.activeElement !== fbox) {\n"
        "                                    fbox.value = Math.round(fkey / 1000);\n"
        "                                }\n"
        "                            }\n",
        "app: frequency box follows the lock")
    p.write_text(src)


if __name__ == "__main__":
    main()
