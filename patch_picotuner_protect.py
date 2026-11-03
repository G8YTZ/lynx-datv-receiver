#!/usr/bin/env python3
"""
patch_picotuner_protect.py

Picotuner receivers: Share or Protect. Follows patch_picotuner_takeover.py.

  Share (default): as now. Another device may take a receiver; Lynx
      leaves it with them and takes it back when that device, asked
      every 30 s, says it has finished ("LYNX IDLE").
  Protect (a repeater): a receiver Lynx is using is never left with
      anyone else. If another device takes it - an iPhone over a VPN
      that can't see who is using what, QuickTune, OpenTuner - Lynx
      tunes it straight back, about 10 s later.

Either way, Lynx now answers the presence question the Apple TV and
iPhone ask before taking a receiver (UDP 9950, "LYNX? n"): "LYNX
WATCHING n" for a receiver it is using, otherwise "LYNX IDLE". On the
home network the apps already ask, so a receiver this Pi is using is
reported as in use and is not taken: until now the Pi said nothing, and
silence led to a "take over?" prompt. The answer is the exact text the
existing app builds expect, so no app update is needed.

Config page, Picotuner card: a "Receivers" choice. Saved to
config.yaml as picotuner.receivers (share | protect) and read live: no
restart needed.

Run from the repo root:  python3 patch_picotuner_protect.py
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


src = TARGET.read_text()
if "_picotuner_taken_over" not in src:
    sys.exit("Apply patch_picotuner_takeover.py first.")
if "lynx_presence_responder" in src:
    sys.exit("Already applied.")

# 1. Config model
src = apply(src,
'''class PicotunerConfigUpdate(BaseModel):
    host: str
    cmd_port: int
    cmd_port_b: int
    ts_port: int
    ts_port_b: int
    status_port: int
''',
'''class PicotunerConfigUpdate(BaseModel):
    host: str
    cmd_port: int
    cmd_port_b: int
    ts_port: int
    ts_port_b: int
    status_port: int
    receivers: Optional[str] = None   # "share" or "protect"
''', "config model: receivers")

# 2. Saving: receivers is read live, so it does not ask for a restart.
src = apply(src,
'''            new_pt = req.picotuner.model_dump()
            picotuner_changed = on_disk.get('picotuner', {}) != {**on_disk.get('picotuner', {}), **new_pt}
''',
'''            new_pt = req.picotuner.model_dump(exclude_none=True)
            if new_pt.get('receivers') not in (None, 'share', 'protect'):
                new_pt.pop('receivers')
            # receivers (share/protect) is read live by the tuning watchdog
            # and the presence responder: changing it needs no restart.
            restart_part = {k: v for k, v in new_pt.items() if k != 'receivers'}
            picotuner_changed = on_disk.get('picotuner', {}) != {**on_disk.get('picotuner', {}), **restart_part}
''', "config save: receivers needs no restart")

# 3. Config page: the choice
src = apply(src,
'''                                <input type="number" class="form-control" id="pt-ts-port-b-input">
                            </div>
                        </div>
''',
'''                                <input type="number" class="form-control" id="pt-ts-port-b-input">
                            </div>
                            <div class="col-md-6">
                                <label for="pt-receivers">Receivers</label>
                                <select class="form-select" id="pt-receivers-input">
                                    <option value="share">Share with other devices</option>
                                    <option value="protect">Protect (repeater)</option>
                                </select>
                            </div>
                            <div class="col-12">
                                <p class="text-muted small mb-0">
                                    Share: another device (an iPhone or Apple TV, say) may take a
                                    receiver this Lynx is using; Lynx takes it back when that device has
                                    finished. Protect: a receiver Lynx is using is always taken straight
                                    back, about 10 s later. Use Protect on a repeater. Takes effect on Save,
                                    with no restart.
                                </p>
                            </div>
                        </div>
''', "config page: Receivers choice")

src = apply(src,
'''        document.getElementById('pt-ts-port-b-input').value = cfg.picotuner?.ts_port_b || '';
''',
'''        document.getElementById('pt-ts-port-b-input').value = cfg.picotuner?.ts_port_b || '';
        document.getElementById('pt-receivers-input').value = cfg.picotuner?.receivers === 'protect' ? 'protect' : 'share';
''', "config page: load")

src = apply(src,
'''                ts_port_b: parseInt(document.getElementById('pt-ts-port-b-input').value),
            }
''',
'''                ts_port_b: parseInt(document.getElementById('pt-ts-port-b-input').value),
                receivers: document.getElementById('pt-receivers-input').value,
            }
''', "config page: save")

# 4. Helpers: the mode, and "is another device's" without the presence logic
src = apply(src,
'''def _picotuner_taken_over(rcv):
''',
'''PICOTUNER_PROTECT_RECLAIM_SECS = 15.0  # protected: at most one take-back per 15 s


def _picotuner_mode():
    """"share" (the default) or "protect", from the Config page."""
    m = str((config.get('picotuner', {}) or {}).get('receivers', 'share')).lower()
    return 'protect' if m == 'protect' else 'share'


def _picotuner_target_other(rcv):
    """The address receiver rcv is sending to, when that is another
    device; None when it is sending to this Pi, to nowhere, or unknown."""
    st = picotuner_state_b if rcv == 2 else picotuner_state
    ip = str(st.get("ts_target", "") or "").split(":")[0]
    if not ip or ip == "0.0.0.0" or ip in _my_ips():
        return None
    return ip


def _picotuner_taken_over(rcv):
''', "helpers: mode and target")

src = apply(src,
'''    if not ip or ip == "0.0.0.0" or ip in _my_ips():
        _takeover_seen.pop(rcv, None)
        return None
    now = time.time()
''',
'''    if not ip or ip == "0.0.0.0" or ip in _my_ips():
        _takeover_seen.pop(rcv, None)
        return None
    if _picotuner_mode() == 'protect':
        return None          # protected: the tuning watchdog takes it straight back
    now = time.time()
''', "takeover: not in protect mode")

# 5. Tuning watchdog: protected receivers are taken straight back
src = apply(src,
'''            expected = _picotuner_expected_tuning()
            if not expected:
                bad_since = None
                continue

            wrong = []
''',
'''            expected = _picotuner_expected_tuning()
            if not expected:
                bad_since = None
                continue

            # Protected (a repeater): a receiver Lynx is using that another
            # device has tuned is taken back at once, not after the 20 s a
            # lost tuning waits, and with a shorter cooldown than a restore
            # that isn't taking.
            if _picotuner_mode() == 'protect':
                grabbed = [(r, _picotuner_target_other(r)) for r in expected]
                grabbed = [(r, ip) for r, ip in grabbed if ip]
                if grabbed:
                    now = time.time()
                    if now - last_fix >= PICOTUNER_PROTECT_RECLAIM_SECS:
                        for r, ip in grabbed:
                            print(f"[picotuner] Rx{r} taken by {ip} - receivers "
                                  f"are protected (repeater), taking it back")
                        last_fix = now
                        bad_since = None
                        threading.Thread(target=_picotuner_restore_tuning,
                                         daemon=True).start()
                    continue

            wrong = []
''', "tuning watchdog: protect mode takes back")

# 6. Presence responder
src = apply(src,
'''def picotuner_tuning_watchdog():
''',
'''def lynx_presence_responder():
    """Answers the question Lynx Apple TV and iPhone ask before taking a
    Picotuner receiver: "LYNX? n" on UDP 9950, answered from the same
    socket straight back to the asker. "LYNX WATCHING n" while this Lynx is
    using receiver n (tuned for it, and its stream still comes here),
    otherwise "LYNX IDLE". The exact text existing app builds compare
    against, so a receiver in use here is reported as in use."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", 9950))
    except OSError as e:
        print(f"[presence] could not listen on UDP 9950: {e}")
        return
    print("[presence] answering Lynx devices on UDP 9950")
    while True:
        try:
            data, addr = sock.recvfrom(256)
            text = data.decode(errors="replace").strip()
            if not text.startswith("LYNX?"):
                continue
            try:
                n = int(text.split()[1])
            except (IndexError, ValueError):
                continue
            answer = "LYNX IDLE"
            if n in _picotuner_expected_tuning() and not _picotuner_target_other(n):
                answer = f"LYNX WATCHING {n}"
            sock.sendto(answer.encode(), addr)
            print(f"[presence] {addr[0]} asked about Rx{n}: {answer}")
        except Exception as e:
            print(f"[presence] {e}")
            time.sleep(1)


def picotuner_tuning_watchdog():
''', "presence responder")

src = apply(src,
'''        target=picotuner_tuning_watchdog, daemon=True)
    picotuner_watchdog_thread.start()
''',
'''        target=picotuner_tuning_watchdog, daemon=True)
    picotuner_watchdog_thread.start()
    threading.Thread(target=lynx_presence_responder, daemon=True).start()
''', "start the presence responder")

TARGET.write_text(src)
print("Done.")
