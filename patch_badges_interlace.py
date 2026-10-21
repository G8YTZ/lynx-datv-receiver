#!/usr/bin/env python3
"""
patch_badges_interlace.py

Two corrections to the top-row badges, both found on 21 Oct 2026
watching Freeview on the ROCK 5T.

1. INTERLACE. UK HD (and SD) broadcast is interlaced: 1080i25, 576i25.
   The IN badge said 1080p25, because mpv's frame rate cannot tell
   fields from frames, and its per-frame "interlaced" flag reads false
   once a hardware deinterlacer has done its work.

   What does tell them apart: mpv's container-fps (the stream's frame
   rate, 25) against estimated-vf-fps (what the decoder actually puts
   out). Measured on a 576i25 service: 25 in, 50 out - the ROCK's
   decoder bobs each field to its own frame, keeping full motion. Output
   at twice the input means fields were bobbed, so the source was
   interlaced. Also counted as interlaced: a frame still flagged
   interlaced (a decoder that does not deinterlace).

   The badge now reads e.g. "IN 1080i25 H264", and the tooltip says what
   the decoder did: "deinterlaced to 50 fps". The judder check uses the
   frames actually shown (50), not the stream's 25: a bobbed 25i on
   60 Hz is still 50 on 60, and judders.

2. DVB-T2. The mode badge showed Lynx's internal mode name, "DVBT", for
   both standards. It now shows what the tuner has locked: "DVB-T2" or
   "DVB-T" (from the HDHomeRun's lock mode, t8dvbt2 and so on).

Run from the repo root:  python3 patch_badges_interlace.py
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


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    src = TARGET.read_text()
    before = len(src.splitlines())
    if '"interlaced": interlaced' in src:
        sys.exit("ABORT: already patched")
    if "def _read_video_in" not in src:
        sys.exit("ABORT: patch_video_badges.py not applied")

    # ── 1. Backend: stream rate, shown rate, interlace ──────────────
    src = apply(
        src,
        '    fps = _mpv_prop("container-fps") or _mpv_prop("estimated-vf-fps")\n',
        '    fps = _mpv_prop("container-fps")\n'
        '    shown_fps = _mpv_prop("estimated-vf-fps")\n'
        '    finfo = _mpv_prop("video-frame-info") or {}\n'
        '    # Interlaced if the decoder put out twice the stream\'s frame\n'
        '    # rate (fields bobbed to frames - see patch_badges_interlace.py),\n'
        '    # or a frame is still flagged interlaced (not deinterlaced).\n'
        '    interlaced = bool(finfo.get("interlaced")) or bool(\n'
        '        fps and shown_fps and abs(float(shown_fps) / float(fps) - 2) < 0.05)\n'
        '    if not fps:\n'
        '        fps = shown_fps\n',
        "backend: interlace")

    src = apply(
        src,
        '        "fps": round(float(fps), 3) if fps else None,\n',
        '        "fps": round(float(fps), 3) if fps else None,\n'
        '        "shown_fps": round(float(shown_fps), 3) if shown_fps else None,\n'
        '        "interlaced": interlaced,\n'
        '        "display_width": params.get("dw"),\n',
        "backend: fields")

    # ── 2. Badge text and tooltip (no backslashes: plain Python string) ─
    src = apply(
        src,
        "function videoLabel(h, rate) {\n"
        "    if (!h) return '?';\n"
        "    if (!rate) return h + 'p';\n"
        "    const r = Math.abs(rate - Math.round(rate)) < 0.01 ? Math.round(rate) : rate.toFixed(2);\n"
        "    return h + 'p' + r;\n"
        "}\n",
        "function videoLabel(h, rate, interlaced) {\n"
        "    const scan = interlaced ? 'i' : 'p';\n"
        "    if (!h) return '?';\n"
        "    if (!rate) return h + scan;\n"
        "    const r = Math.abs(rate - Math.round(rate)) < 0.01 ? Math.round(rate) : rate.toFixed(2);\n"
        "    return h + scan + r;\n"
        "}\n",
        "js: i or p")

    src = apply(
        src,
        "        let txt = 'IN ' + videoLabel(vin.height, vin.fps);\n",
        "        let txt = 'IN ' + videoLabel(vin.height, vin.fps, vin.interlaced);\n",
        "js: IN label")

    src = apply(
        src,
        "        inB.title = 'Received: ' + vin.width + ' x ' + vin.height\n"
        "            + (vin.fps ? ', ' + vin.fps + ' fps' : '')\n",
        "        inB.title = 'Received: ' + vin.width + ' x ' + vin.height\n"
        "            + (vin.display_width && vin.display_width !== vin.width\n"
        "               ? ' (shown ' + vin.display_width + ' wide)' : '')\n"
        "            + (vin.fps ? ', ' + vin.fps + ' fps' : '')\n"
        "            + (vin.interlaced ? ', interlaced' : '')\n"
        "            + (vin.interlaced && vin.shown_fps && vin.shown_fps !== vin.fps\n"
        "               ? ', deinterlaced to ' + vin.shown_fps + ' fps' : '')\n",
        "js: IN tooltip")

    src = apply(
        src,
        "        if (vin.fps && o.refresh) {\n"
        "            const ratio = o.refresh / vin.fps;\n"
        "            if (Math.abs(ratio - Math.round(ratio)) > 0.01) {\n"
        "                warnings.push(vin.fps + ' fps on ' + o.refresh + ' Hz is not a whole multiple: motion will judder');\n",
        "        // The frames actually shown: a bobbed 25i is 50 a second.\n"
        "        const shown = vin.shown_fps || vin.fps;\n"
        "        if (shown && o.refresh) {\n"
        "            const ratio = o.refresh / shown;\n"
        "            if (Math.abs(ratio - Math.round(ratio)) > 0.01) {\n"
        "                warnings.push(shown + ' fps on ' + o.refresh + ' Hz is not a whole multiple: motion will judder');\n",
        "js: judder uses shown rate")

    # ── 3. Mode badge: DVB-T2 or DVB-T, as locked ───────────────────
    src = apply(
        src,
        "        badge.textContent = mode.toUpperCase();\n",
        "        badge.textContent = mode.toUpperCase();\n"
        "        if (mode === 'dvbt') {\n"
        "            // The tuner says which: t8dvbt2 and so on. See\n"
        "            // patch_badges_interlace.py.\n"
        "            const lmode = ((s.lynx && s.lynx.hdhomerun) || s.hdhomerun || {}).lock_mode || '';\n"
        "            badge.textContent = lmode.indexOf('dvbt2') >= 0 ? 'DVB-T2'\n"
        "                              : lmode.indexOf('dvbt') >= 0 ? 'DVB-T' : 'DVB-T/T2';\n"
        "        }\n",
        "js: mode badge DVB-T2")

    TARGET.write_text(src)
    print(f"lynx_app.py: {before} -> {len(src.splitlines())} lines")


if __name__ == "__main__":
    main()
