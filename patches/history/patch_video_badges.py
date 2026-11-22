#!/usr/bin/env python3
"""
patch_video_badges.py

Two badges on the top row of the receiver page: what is being received
(IN) and what the HDMI output is actually sending (OUT).

WHY TWO
-------
They are usually different, and the difference is the useful part. A
1024x576 DATV stream on a 3840x2160 output is normal; a 2160p source on
a 1080 output is being thrown away; 25 fps on a 60 Hz output judders on
every pan (3:2 pulldown). One glance at the top row now says which.

Found the hard way on the ROCK 5T on 21 Oct 2026: The display was
quietly at 1080 (the Pi's height cap, inherited from a copied config)
and nothing on the page said so.

  IN   from mpv itself over IPC: video-params (size, transfer
       characteristic, so SDR / HLG / PQ), container-fps, video-format,
       hwdec-current. "NO VIDEO" when nothing is decoding.
  OUT  from wlr-randr: every enabled output and its current mode. Read
       from the compositor, not from Lynx's own idea of what it set,
       because the compositor is the one that knows. Works the same on
       the Pi 5 and the ROCK 5T.

Amber, with the reason in the tooltip, when:
  * the output is smaller than the picture (detail being discarded)
  * the refresh rate is not a whole multiple of the frame rate (judder)

Polled every 3 s by one background thread and cached, so /api/status
never waits on wlr-randr or mpv.

Run from the repo root:  python3 patch_video_badges.py
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


# ── 1. Backend: the monitor and its helpers ──────────────────────
MONITOR = r'''
# ── Video badges: what is received (IN), what HDMI sends (OUT) ───
# See patch_video_badges.py for the reasoning. One thread polls both
# every few seconds and caches the answer; /api/status only reads it.
VIDEO_BADGE_POLL_SECS = 3.0
video_in_info = None     # dict, or None when nothing is decoding
video_out_info = []      # one dict per enabled output, [] if unknown
_video_badge_lock = threading.Lock()


def _parse_wlr_randr(text):
    """Every ENABLED output and its current mode, from plain wlr-randr
    output. Plain text rather than --json: bookworm's wlr-randr (0.2)
    has no --json, and the layout below is the same in every version.

        HDMI-A-1 "LG Electronics LG HDR 4K ..."
          Enabled: yes
          Modes:
            3840x2160 px, 50.000000 Hz (current)
    """
    outputs, cur = [], None
    for line in text.splitlines():
        if line and not line[0].isspace():
            cur = {"name": line.split()[0], "enabled": True,
                   "width": None, "height": None, "refresh": None}
            outputs.append(cur)
            continue
        if cur is None:
            continue
        s = line.strip()
        if s.startswith("Enabled:"):
            cur["enabled"] = s.split(":", 1)[1].strip() == "yes"
        elif "current" in s and " px" in s:
            m = re.match(r"(\d+)x(\d+)\s+px,\s+([\d.]+)\s+Hz", s)
            if m:
                cur["width"], cur["height"] = int(m.group(1)), int(m.group(2))
                cur["refresh"] = round(float(m.group(3)), 3)
    return [o for o in outputs if o["enabled"] and o["height"]]


def _read_video_out():
    env = os.environ.copy()
    # The service does not always carry the session's variables, so the
    # same defaults lynx.service itself assumes.
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    try:
        out = subprocess.run(["wlr-randr"], capture_output=True, text=True,
                             timeout=3, env=env)
        return _parse_wlr_randr(out.stdout) if out.returncode == 0 else []
    except Exception:
        return []


def _mpv_prop(name):
    r = mpv_query({"command": ["get_property", name]})
    if r and r.get("error") == "success":
        return r.get("data")
    return None


def _read_video_in():
    params = _mpv_prop("video-params")
    if not isinstance(params, dict) or not params.get("h"):
        return None
    fps = _mpv_prop("container-fps") or _mpv_prop("estimated-vf-fps")
    gamma = (params.get("gamma") or "").lower()
    return {
        "width": params.get("w"),
        "height": params.get("h"),
        "fps": round(float(fps), 3) if fps else None,
        "codec": (_mpv_prop("video-format") or "").upper() or None,
        "hwdec": _mpv_prop("hwdec-current") or "no",
        # Transfer characteristic: what tells SDR from HDR.
        "range": "PQ" if gamma == "pq" else "HLG" if gamma == "hlg" else "SDR",
        "primaries": params.get("primaries"),
        "pixelformat": params.get("hw-pixelformat") or params.get("pixelformat"),
    }


def video_badge_monitor():
    global video_in_info, video_out_info
    while True:
        try:
            outs = _read_video_out()
            vin = _read_video_in()
            with _video_badge_lock:
                video_out_info = outs
                video_in_info = vin
        except Exception as e:
            print(f"[video_badges] {e}")
        time.sleep(VIDEO_BADGE_POLL_SECS)


def mpv_decoder_health_monitor():'''


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    src = TARGET.read_text()
    before = len(src.splitlines())
    if "video_badge_monitor" in src:
        sys.exit("ABORT: already patched")

    src = apply(src, "\ndef mpv_decoder_health_monitor():", MONITOR,
                "monitor and helpers")

    # ── 2. Start the thread with the other monitors ──────────────
    src = apply(
        src,
        "    decoder_health = threading.Thread(target=mpv_decoder_health_monitor, daemon=True)\n"
        "    decoder_health.start()\n",
        "    decoder_health = threading.Thread(target=mpv_decoder_health_monitor, daemon=True)\n"
        "    decoder_health.start()\n"
        "    video_badges = threading.Thread(target=video_badge_monitor, daemon=True)\n"
        "    video_badges.start()\n",
        "thread start")

    # ── 3. Into /api/status, beside the mode ─────────────────────
    src = apply(
        src,
        '            "timestamp": utc_now_iso()\n',
        '            # Cached by video_badge_monitor(); never waits on mpv or wlr-randr.\n'
        '            "video_in": video_in_info,\n'
        '            "video_out": video_out_info,\n'
        '            "timestamp": utc_now_iso()\n',
        "status fields")

    # ── 4. The two badges, after the mode badge ──────────────────
    mode_badge = ('<span class="btn btn-sm" id="mode-badge" style="background:#3a4a63; '
                  'color:#fff; cursor:default;">IDLE</span>\n')
    src = apply(
        src, mode_badge,
        mode_badge +
        '            <span class="btn btn-sm" id="video-in-badge" style="background:#3a4a63; '
        'color:#fff; cursor:default;" title="Received video">NO VIDEO</span>\n'
        '            <span class="btn btn-sm" id="video-out-badge" style="background:#3a4a63; '
        'color:#fff; cursor:default;" title="HDMI output">OUT ?</span>\n',
        "badge markup")

    # ── 5. JavaScript: render them on every status poll ──────────
    # No backslashes anywhere below: this lands inside a plain Python
    # string, where they would be interpreted before the browser sees them.
    js = r'''// Video badges - see patch_video_badges.py.
function videoLabel(h, rate) {
    if (!h) return '?';
    if (!rate) return h + 'p';
    const r = Math.abs(rate - Math.round(rate)) < 0.01 ? Math.round(rate) : rate.toFixed(2);
    return h + 'p' + r;
}

function renderVideoBadges(vin, vout) {
    const NORMAL = '#3a4a63', WARN = '#b7791f', HDR = '#7c3aed';
    const inB = document.getElementById('video-in-badge');
    const outB = document.getElementById('video-out-badge');
    if (!inB || !outB) return;
    const outs = Array.isArray(vout) ? vout : [];
    const warnings = [];

    if (vin && vin.height) {
        let txt = 'IN ' + videoLabel(vin.height, vin.fps);
        if (vin.codec) txt += ' ' + vin.codec;
        if (vin.range && vin.range !== 'SDR') txt += ' ' + vin.range;
        inB.textContent = txt;
        inB.style.background = (vin.range && vin.range !== 'SDR') ? HDR : NORMAL;
        inB.title = 'Received: ' + vin.width + ' x ' + vin.height
            + (vin.fps ? ', ' + vin.fps + ' fps' : '')
            + (vin.codec ? ', ' + vin.codec : '')
            + ', ' + (vin.range || 'SDR')
            + (vin.primaries ? ' (' + vin.primaries + ')' : '')
            + '. Decode: ' + (vin.hwdec && vin.hwdec !== 'no' ? 'hardware (' + vin.hwdec + ')' : 'software');
    } else {
        inB.textContent = 'NO VIDEO';
        inB.style.background = NORMAL;
        inB.title = 'Nothing is being decoded';
    }

    if (!outs.length) {
        outB.textContent = 'OUT ?';
        outB.style.background = NORMAL;
        outB.title = 'Output mode unknown (wlr-randr gave no answer)';
        return;
    }
    outB.textContent = 'OUT ' + outs.map(o => videoLabel(o.height, o.refresh)).join(' + ');
    outB.title = outs.map(o => o.name + ': ' + o.width + ' x ' + o.height + ' at ' + o.refresh + ' Hz').join('; ');

    if (vin && vin.height) {
        const o = outs[0];
        if (o.height < vin.height) {
            warnings.push('Output is smaller than the picture: ' + vin.height + ' lines shown on ' + o.height);
        }
        if (vin.fps && o.refresh) {
            const ratio = o.refresh / vin.fps;
            if (Math.abs(ratio - Math.round(ratio)) > 0.01) {
                warnings.push(vin.fps + ' fps on ' + o.refresh + ' Hz is not a whole multiple: motion will judder');
            }
        }
    }
    outB.style.background = warnings.length ? WARN : NORMAL;
    if (warnings.length) outB.title += '. ' + warnings.join('. ');
}

async function updateStatus() {'''
    src = apply(src, "async function updateStatus() {", js, "badge rendering")

    src = apply(
        src,
        "        badge.style.background = mode === 'idle' ? '#3a4a63' : mode === 'rf' ? '#1a9850' : '#3b82c4';\n",
        "        badge.style.background = mode === 'idle' ? '#3a4a63' : mode === 'rf' ? '#1a9850' : '#3b82c4';\n"
        "\n"
        "        renderVideoBadges(s.lynx?.video_in, s.lynx?.video_out);\n",
        "badge update call")

    TARGET.write_text(src)
    after = len(src.splitlines())
    print(f"lynx_app.py: {before} -> {after} lines (+{after - before})")


if __name__ == "__main__":
    main()
