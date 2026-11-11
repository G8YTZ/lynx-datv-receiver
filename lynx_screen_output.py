"""Screen output: HDMI or SRT (ROCK first; 11 Nov 2026).

What goes out over SRT is the finished screen, the picture with the
overlay, chat and PPM on it, not the received transport stream. That is
what a viewer at the far end (a transmitter's input, VLC, mpv) wants to
see, and it is the first step towards Lynx feeding Lynx-Tx directly.

How it works
------------
  mpv + overlay --> labwc output --> wf-recorder (screen copy,
  RK3588 hardware H.264, AAC from PipeWire) --> MPEG-TS over UDP on
  the loopback --> ffmpeg -c copy --> SRT (caller by default).

The two halves are joined by loopback UDP rather than a pipe, so the
SRT side can drop and reconnect (the far end is restarted, the
internet hiccups) without touching the capture, and the capture can
restart without tearing down the SRT link's process.

Modes
-----
  hdmi : As before; nothing is captured.
  srt  : labwc adds a virtual screen, LYNX-SRT, at 1920x1080 50 Hz and
         the HDMI screen is switched off, so a receiver with nothing
         plugged in still has a screen to draw on, at 50 Hz (no EDID
         dummy needed). mpv locks to it as it would to a television.
         Sound goes to a PipeWire null sink, lynx_srt, which is what
         is captured.

There is no "both": capturing a real HDMI screen made labwc composite
and copy every frame against the screen's own refresh, and at 2160p it
missed refreshes, so the LG and the SRT feed juddered alike, while each
alone was smooth (5C, 11 Nov 2026). Removed (Justin): a mode that
cannot reach the quality would only bring complaints.

labwc has no IPC, so the virtual screen is made with its own
VirtualOutputAdd action, bound to a key in rc.xml (install/rock5t.sh)
and pressed with wtype, as Lynx already does for other labwc actions.
"""

import os
import re
import shutil
import subprocess
import threading
import time

try:
    import lynx_platform
    HARDWARE_ENCODER = bool(lynx_platform.IS_ROCK)
except Exception:
    HARDWARE_ENCODER = False

MODES = ("hdmi", "srt")
VIRTUAL = "LYNX-SRT"
NULL_SINK = "lynx_srt"
LOOP_PORT = 9960
LOG = "/tmp/lynx-srt.log"
# Present while the virtual screen is the one in use: lynx_app's
# screen_connected() and lynx_start.sh's heartbeat check read it, as
# the HDMI connector says "disconnected" but something is still asking
# for frames.
VIRTUAL_MARKER = "/tmp/lynx_virtual_screen"
LOG_MAX = 512 * 1024

# Keys bound in labwc's rc.xml (install/rock5t.sh): Super+Alt+Shift+F9
# adds the virtual screen, F10 removes it.
KEY_ADD = "F9"
KEY_REMOVE = "F10"

# Picture formats for the SRT feed: (width, height, frames a second).
# Any of them on SRT mode's virtual screen, whatever a monitor offers.
FORMATS = {
    "1080p50": (1920, 1080, 50),
    "1080p25": (1920, 1080, 25),
    "2160p25": (3840, 2160, 25),
    "2160p50": (3840, 2160, 50),
}

# What a board without a hardware encoder (the Pi) can offer: H.264 in
# software (libx264), 1080p only. H.265 in software is far too slow.
SOFTWARE_FORMATS = ("1080p25", "1080p50")

DEFAULTS = {
    "mode": "hdmi",
    "format": "1080p50",
    "codec": "hevc",           # hevc (H.265, Justin 11 Nov 2026) or h264
    "host": "",
    "port": 9000,
    "srt_mode": "caller",      # caller (Lynx dials) or listener
    "latency_ms": 500,
    "bitrate_kbps": None,      # None: by codec and format (see settings())
    "passphrase": "",
    "streamid": "",
}


def settings(config):
    s = dict(DEFAULTS)
    s.update((config.get("screen_output") or {}))
    # Its own format, not Lynx's Refresh rate (50 or 60 only): in SRT
    # mode the screen output owns the screen mode, and the
    # Refresh rate setting stands aside (lynx_app._reapply_refresh_rate).
    if s.get("format") not in FORMATS:
        s["format"] = "1080p50"
    if not HARDWARE_ENCODER:
        if s["format"] not in SOFTWARE_FORMATS:
            s["format"] = "1080p25"
        s["codec"] = "h264"
    s["width"], s["height"], s["fps"] = FORMATS[s["format"]]
    if s.get("codec") not in ("hevc", "h264"):
        s["codec"] = "hevc" if HARDWARE_ENCODER else "h264"
    if not s.get("bitrate_kbps"):
        # H.265 needs about two thirds of H.264's rate for the same look.
        uhd = s["height"] > 1080
        s["bitrate_kbps"] = ({"hevc": 12000, "h264": 16000} if uhd else
                             {"hevc": 4000, "h264": 6000})[s["codec"]]
    if s["mode"] not in MODES:
        s["mode"] = "hdmi"      # includes a saved "both" (removed)
    return s


def wayland_env():
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("WAYLAND_DISPLAY", "wayland-0")
    return env


def _run(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=wayland_env())
        return r.returncode, r.stdout + r.stderr
    except Exception as e:
        return 1, str(e)


def outputs():
    """{name: {"enabled", "modes": [(w, h, hz)], "current": (w, h, hz)}}
    from wlr-randr, or None if it cannot be asked."""
    rc, out = _run(["wlr-randr"])
    if rc != 0:
        return None
    found = {}
    cur = None
    for line in out.splitlines():
        if line and not line[0].isspace():
            cur = {"enabled": False, "modes": [], "current": None}
            found[line.split()[0]] = cur
            continue
        if cur is None:
            continue
        t = line.strip()
        if t.startswith("Enabled:"):
            cur["enabled"] = t.split(":", 1)[1].strip() == "yes"
        m = re.match(r"(\d+)x(\d+) px, ([\d.]+) Hz(.*)", t)
        if m:
            mode = (int(m.group(1)), int(m.group(2)), round(float(m.group(3)), 2))
            cur["modes"].append(mode)
            if "current" in m.group(4):
                cur["current"] = mode
    return found


def _is(mode, w, h, hz):
    return bool(mode) and mode[0] == w and mode[1] == h and abs(mode[2] - hz) < 0.05


def _press(key):
    return _run(["wtype", "-M", "logo", "-M", "alt", "-M", "shift",
                 "-k", key, "-m", "shift", "-m", "alt", "-m", "logo"])


def virtual_name(outs):
    """The virtual screen's name: LYNX-SRT if labwc named it as asked,
    else the first HEADLESS-n (older labwc ignores output_name)."""
    if VIRTUAL in outs:
        return VIRTUAL
    for n in outs:
        if n.startswith("HEADLESS-"):
            return n
    return None


def _sink_ids():
    """PipeWire object ids of nodes named lynx_srt."""
    rc, out = _run(["pw-cli", "ls", "Node"])
    if rc != 0:
        return []
    ids, cur = [], None
    for line in out.splitlines():
        m = re.match(r"\s*id (\d+), type PipeWire:Interface:Node", line)
        if m:
            cur = m.group(1)
        elif cur and f'node.name = "{NULL_SINK}"' in line:
            ids.append(cur)
    return ids


def null_sink_exists():
    return bool(_sink_ids())


_sink_fresh = [False]


def ensure_null_sink():
    """A PipeWire sink nobody listens to, for mpv's sound in SRT mode.
    object.linger keeps it after pw-cli exits; it lasts until PipeWire
    restarts, so the watchdog checks it each time round.

    node.latency 1024/48000: the quantum HDMI runs at. Without it the
    graph ran at 2048, and mpv (RF's low-latency profile, almost no
    audio buffer) filled only every other block: alternate 2048-sample
    gaps, a scrubbing sound on the SRT feed (5C, 11 Nov 2026). The
    first call after Lynx starts replaces any sink left from before,
    so a change here takes effect."""
    if not _sink_fresh[0]:
        for i in _sink_ids():
            _run(["pw-cli", "destroy", i])
        _sink_fresh[0] = True
    if null_sink_exists():
        return True
    rc, out = _run(["pw-cli", "create-node", "adapter",
                    "{ factory.name=support.null-audio-sink "
                    f"node.name={NULL_SINK} "
                    "node.description=\"Lynx SRT output\" "
                    "media.class=Audio/Sink object.linger=true "
                    "node.latency=1024/48000 "
                    "audio.rate=48000 audio.position=[ FL FR ] }"])
    if rc != 0:
        print(f"[srt] could not make the {NULL_SINK} sink: {out.strip()}",
              flush=True)
    return null_sink_exists()


def _log_file():
    try:
        if os.path.getsize(LOG) > LOG_MAX:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass
    return open(LOG, "ab")


def srt_url(s):
    host = (s.get("host") or "").strip()
    port = int(s.get("port") or 9000)
    mode = "listener" if s.get("srt_mode") == "listener" else "caller"
    if mode == "listener":
        host = "0.0.0.0"
    if not host:
        return ""
    q = [f"mode={mode}",
         f"latency={int(s.get('latency_ms') or 500) * 1000}",
         "pkt_size=1316", "connect_timeout=5000"]
    if s.get("passphrase"):
        q.append(f"passphrase={s['passphrase']}")
        q.append("pbkeylen=16")
    if s.get("streamid"):
        q.append(f"streamid={s['streamid']}")
    return f"srt://{host}:{port}?" + "&".join(q)


def srt_url_public(s):
    """The address for show: no passphrase."""
    return re.sub(r"passphrase=[^&]*", "passphrase=***", srt_url(s))


class ScreenOutput:
    """Keeps the screens and the capture chain as the settings say.

    get_config() returns Lynx's config dict; service_name() names the
    programme in the stream;
    on_screen_change() is called after the screens change (Lynx
    restarts mpv once, so it settles on the new screen).
    """

    def __init__(self, get_config, service_name=None, on_screen_change=None):
        self.get_config = get_config
        self.service_name = service_name or (lambda: "Lynx")
        self.on_screen_change = on_screen_change or (lambda: None)
        self.lock = threading.RLock()
        self.capture = None
        self.sender = None
        self.capture_key = None
        self.sender_key = None
        self.capture_fails = 0
        self.sender_fails = 0
        self.capture_next = 0.0
        self.sender_next = 0.0
        self.sender_since = 0.0
        self.capture_since = 0.0
        self.last_error = ""
        self.screen = None          # the output being captured
        self.thread = None
        self.stopping = False

    # ---- what mpv needs ---------------------------------------------
    def mpv_audio_device(self):
        """pipewire/lynx_srt in SRT mode, else None (Lynx's own choice)."""
        if settings(self.get_config())["mode"] == "srt":
            return f"pipewire/{NULL_SINK}"
        return None

    def available(self):
        missing = [t for t in ("wf-recorder", "wlr-randr", "wtype",
                               "ffmpeg", "pw-cli") if not shutil.which(t)]
        return missing

    # ---- screens ----------------------------------------------------
    def _arrange(self, mode, s):
        """Bring the screens to what the mode needs. Returns the name of
        the screen to capture (None in hdmi mode, or if not ready)."""
        outs = outputs()
        if outs is None:
            return None
        w, h, hz = int(s["width"]), int(s["height"]), int(s["fps"])
        changed = False
        virt = virtual_name(outs)
        real = [n for n in outs if n != virt]

        if mode == "srt":
            if not virt:
                _press(KEY_ADD)
                time.sleep(0.5)
                outs = outputs() or {}
                virt = virtual_name(outs)
                if not virt:
                    self.last_error = ("labwc made no virtual screen: is the "
                                       "VirtualOutputAdd key in rc.xml?")
                    return None
                changed = True
            if not _is(outs[virt]["current"], w, h, hz):
                _run(["wlr-randr", "--output", virt,
                      "--custom-mode", f"{w}x{h}@{hz}Hz"])
                changed = True
            for n in real:
                if outs[n]["enabled"]:
                    _run(["wlr-randr", "--output", n, "--off"])
                    changed = True
            screen = virt
        else:
            # hdmi: real screens on, the virtual one gone.
            for n in real:
                if not outs[n]["enabled"]:
                    _run(["wlr-randr", "--output", n, "--on"])
                    changed = True
            if virt:
                _press(KEY_REMOVE)
                changed = True
            screen = None
        if changed:
            print(f"[srt] screens arranged for {mode}: capturing "
                  f"{screen or 'nothing'}", flush=True)
            # Wait until labwc reports the new mode before mpv is
            # restarted: restarted a second after a 4K to 1080p change,
            # mpv came up sized for 4K, a zoomed frozen frame (5C, LG,
            # 11 Nov 2026). Then two seconds more for the screen to sync.
            if screen and mode != "hdmi":
                deadline = time.time() + 8
                while time.time() < deadline:
                    o = (outputs() or {}).get(screen)
                    if o and o["enabled"] and _is(o["current"], w, h, hz):
                        break
                    time.sleep(0.5)
            time.sleep(2.0)
            try:
                self.on_screen_change()
            except Exception as e:
                print(f"[srt] screen change hook: {e}", flush=True)
        return screen

    # ---- processes --------------------------------------------------
    @staticmethod
    def _alive(p):
        return p is not None and p.poll() is None

    def _stop_proc(self, p):
        if p is None:
            return
        try:
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def _capture_cmd(self, screen, s):
        node = NULL_SINK
        kbps = int(s["bitrate_kbps"])
        fps = int(s.get("fps") or 50)
        cmd = ["wf-recorder", "-o", screen, "-b", "0",
               "-r", str(fps), "-B", str(fps)]
        if HARDWARE_ENCODER:
            # RK3588: rkmpp, the screen's RGB straight in (zero-copy
            # DRM_PRIME with Lynx's wf-recorder patch, else bgr0 copied).
            hevc = s.get("codec") == "hevc"
            cmd += ["-c", "hevc_rkmpp" if hevc else "h264_rkmpp",
                    "-x", "bgr0",
                    "-p", "rc_mode=CBR", "-p", f"b={kbps * 1000}",
                    "-p", f"g={fps}",
                    "-p", "profile=main" if hevc else "profile=high"]
        else:
            # Pi: libx264 in software. crf=-1 turns off wf-recorder's
            # default constant quality so the bit rate holds; the VBV
            # (maxrate, bufsize of one second) keeps it near constant.
            cmd += ["-c", "libx264", "-x", "yuv420p",
                    "-p", "preset=superfast", "-p", "tune=zerolatency",
                    "-p", "crf=-1", "-p", f"b={kbps * 1000}",
                    "-p", f"maxrate={kbps * 1000}",
                    "-p", f"bufsize={kbps * 1000}",
                    "-p", f"g={fps}", "-p", "profile=high"]
        # PulseAudio backend (through pipewire-pulse): wf-recorder's
        # PipeWire backend needs PipeWire 1.0.5, which older Pi OS lacks.
        cmd += ["--audio-backend=pulse", f"--audio={node}.monitor",
                "-C", "aac", "-P", "b=192000", "-R", "48000",
                "-m", "mpegts", "-y",
                "-f", f"udp://127.0.0.1:{LOOP_PORT}?pkt_size=1316"]
        return cmd

    def _sender_cmd(self, s):
        url = srt_url(s)
        if not url:
            return None
        # The sender reads the capture while it waits to connect (a
        # caller to come, or the far end to answer), and on connecting
        # sent all it had queued in one burst: older than SRT's latency,
        # so the far end dropped it ("RCV-DROPPED", VLC, 11 Nov 2026).
        # Its queue now holds a quarter of a second at the bit rate, and
        # the kernel's no more than that, so what goes first is recent.
        bps = int(s["bitrate_kbps"]) * 1000 + 256000
        fifo = max(300, int(bps * 0.25 / 8 / 188))
        kbuf = max(65536, int(bps * 0.25 / 8))
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-fflags", "+nobuffer+discardcorrupt",
                "-probesize", "1000000", "-analyzeduration", "1000000",
                "-i", f"udp://127.0.0.1:{LOOP_PORT}?overrun_nonfatal=1"
                      f"&fifo_size={fifo}&buffer_size={kbuf}&timeout=15000000",
                "-map", "0", "-c", "copy",
                "-f", "mpegts", "-mpegts_service_id", "1",
                "-metadata", f"service_name={self.service_name()}",
                "-metadata", "service_provider=Lynx",
                "-mpegts_flags", "+resend_headers",
                url]

    def _backoff(self, fails):
        if fails < 0:
            return 0.5
        return (2, 5, 10)[min(fails, 2)]

    def tick(self):
        """One pass of the watchdog: arrange screens, keep both
        processes running, stop them when not wanted."""
        cfg = self.get_config()
        s = settings(cfg)
        mode = s["mode"]
        missing = self.available()
        if missing and mode != "hdmi":
            self.last_error = ("missing on this receiver: " + ", ".join(missing)
                               + " (run the installer again)")
            with self.lock:
                self._stop_all()
            return
        with self.lock:
            if mode == "srt":
                ensure_null_sink()
            screen = self._arrange(mode, s)
            self.screen = screen
            self._mark(mode == "srt" and screen is not None)
            if mode == "hdmi" or not screen:
                self._stop_all()
                return
            now = time.time()

            ckey = tuple(self._capture_cmd(screen, s))
            if self._alive(self.capture) and ckey != self.capture_key:
                self._stop_proc(self.capture)
                self.capture = None
            if not self._alive(self.capture):
                if self.capture is not None:
                    self.capture_fails += 1
                    self.last_error = ("screen capture stopped (code "
                                       f"{self.capture.returncode}); see {LOG}")
                    print(f"[srt] {self.last_error}", flush=True)
                    self.capture = None
                    self.capture_next = now + self._backoff(self.capture_fails)
                if now >= self.capture_next:
                    try:
                        log = _log_file()
                        log.write(f"\n=== {time.strftime('%F %T')} capture: "
                                  f"{' '.join(ckey)}\n".encode())
                        log.flush()
                        self.capture = subprocess.Popen(
                            list(ckey), stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL, env=wayland_env())
                        self.capture_key = ckey
                        self.capture_since = now
                        print(f"[srt] capturing {screen}", flush=True)
                    except Exception as e:
                        self.last_error = f"could not start wf-recorder: {e}"
                        self.capture_fails += 1
                        self.capture_next = now + self._backoff(self.capture_fails)
            elif now - self.capture_since > 60:
                self.capture_fails = 0

            scmd = self._sender_cmd(s)
            skey = tuple(scmd) if scmd else None
            if self._alive(self.sender) and skey != self.sender_key:
                self._stop_proc(self.sender)
                self.sender = None
            if skey is None:
                self.last_error = "no SRT address set"
                return
            # The sender reads the capture's loopback stream: give the
            # capture a few seconds to start writing before it opens.
            if not self._alive(self.capture) or now - self.capture_since < 3:
                return
            if not self._alive(self.sender):
                if self.sender is not None:
                    up = now - self.sender_since
                    self.sender_fails = 0 if up > 60 else self.sender_fails + 1
                    if s.get("srt_mode") == "listener":
                        # A viewer leaving ends the listener; listen
                        # again at once rather than backing off (it was
                        # up to 20 s, and the next viewer waited).
                        self.sender_fails = -1
                    self.last_error = (f"SRT link closed after {int(up)} s "
                                       f"(code {self.sender.returncode}); "
                                       f"see {LOG}")
                    print(f"[srt] {self.last_error}", flush=True)
                    self.sender = None
                    self.sender_next = now + self._backoff(self.sender_fails)
                if now >= self.sender_next:
                    try:
                        log = _log_file()
                        log.write(f"\n=== {time.strftime('%F %T')} sender: "
                                  f"{srt_url_public(s)}\n".encode())
                        log.flush()
                        self.sender = subprocess.Popen(
                            list(skey), stdout=log, stderr=log,
                            stdin=subprocess.DEVNULL)
                        self.sender_key = skey
                        self.sender_since = now
                        print(f"[srt] sending to {srt_url_public(s)}",
                              flush=True)
                    except Exception as e:
                        self.last_error = f"could not start ffmpeg: {e}"
                        self.sender_fails += 1
                        self.sender_next = now + self._backoff(self.sender_fails)
            elif now - self.sender_since > 10:
                self.last_error = ""

    @staticmethod
    def _mark(on):
        try:
            if on:
                open(VIRTUAL_MARKER, "w").close()
            elif os.path.exists(VIRTUAL_MARKER):
                os.remove(VIRTUAL_MARKER)
        except OSError:
            pass

    def _stop_all(self):
        for attr in ("sender", "capture"):
            p = getattr(self, attr)
            if p is not None:
                self._stop_proc(p)
                setattr(self, attr, None)
        self.capture_key = self.sender_key = None

    def apply_now(self):
        """After a settings change: act at once, not at the next tick."""
        threading.Thread(target=self.tick, daemon=True).start()

    def run(self):
        missing = self.available()
        if missing:
            print(f"[srt] not available, missing: {', '.join(missing)}",
                  flush=True)
        while not self.stopping:
            try:
                self.tick()
            except Exception as e:
                print(f"[srt] watchdog: {e}", flush=True)
            time.sleep(1)

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, daemon=True)
            self.thread.start()

    def status(self):
        s = settings(self.get_config())
        up = self._alive(self.sender)
        return {
            "mode": s["mode"],
            "screen": self.screen,
            "capturing": self._alive(self.capture),
            "sending": up,
            "connected_for_s": int(time.time() - self.sender_since) if up else 0,
            "url": srt_url_public(s),
            "error": self.last_error,
            "missing": self.available(),
            "hardware_encoder": HARDWARE_ENCODER,
            "formats": list(FORMATS) if HARDWARE_ENCODER else list(SOFTWARE_FORMATS),
            "settings": {k: v for k, v in s.items() if k != "passphrase"},
            "has_passphrase": bool(s.get("passphrase")),
            "bitrate_set": bool((self.get_config().get("screen_output") or {})
                                .get("bitrate_kbps")),
        }
