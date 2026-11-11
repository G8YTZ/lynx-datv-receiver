#!/usr/bin/env python3
"""
patch_picotuner_takeover.py

Picotuner: a receiver another device has taken over is left alone, not
snatched back after 30 s.

Found on 3 Nov 2026. The iPhone, over a VPN, tuned Picotuner receiver 1
to the same channel the Pi was watching in diversity. About 30 s later
the Pi took it back. The Picotuner sends a receiver's $-status only to
whoever tuned it, so once the iPhone had it the Pi stopped hearing its
symbol rate, and the tuning watchdog read "Rx1 reports no symbol rate"
as a lost tuning: 20 s of mismatch plus 8 s of settling, then a restore
that pointed the stream back at the Pi. The diversity stuck-lock monitor
can do the same, after 25 s.

The 9997 broadcast says where each receiver is sending its stream:

    TS target RX1   192.168.0.188:9941

So, before either "recovers" a receiver:

  Target is another device: it has been taken over deliberately. Leave
      it alone, log it once, and ask that device (the Lynx presence
      question, UDP 9950) every 30 s whether it is still watching. A
      Lynx answering "LYNX IDLE" has finished, so the receiver is ours
      again and the watchdog restores it as usual. "LYNX WATCHING n", or
      no answer (not a Lynx, or a phone in the background), leaves it
      with them.
  Target is this Pi, nowhere, or unknown: a genuine lost tuning (a
      Picotuner reboot, say). Restored exactly as before.

Run from the repo root:  python3 patch_picotuner_takeover.py
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
if "_picotuner_taken_over" in src:
    sys.exit("Already applied.")

# 1. Read "TS target RXn" from the configured Picotuner's broadcast.
src = apply(src,
'''            rx_label_a = f"RX{picotuner_rx_numbers['a']}"
            rx_label_b = f"RX{picotuner_rx_numbers['b']}"
''',
'''            # Where each receiver is sending its stream: "TS target RX1
            # 192.168.0.188:9941". Labelled with the board's own receiver
            # numbers, like the RX lines below. Used to tell a receiver
            # another device has taken over from one that has lost its
            # tuning (see _picotuner_taken_over).
            for line in text.splitlines():
                parts = re.split(r'\\s{2,}', line.strip())
                if len(parts) != 2 or not parts[0].lower().startswith("ts target rx"):
                    continue
                num = parts[0][len("TS target RX"):].strip()
                value = parts[1].strip().split()[0] if parts[1].strip() else ""
                if num == str(picotuner_rx_numbers['a']):
                    picotuner_state["ts_target"] = value
                elif num == str(picotuner_rx_numbers['b']):
                    picotuner_state_b["ts_target"] = value

            rx_label_a = f"RX{picotuner_rx_numbers['a']}"
            rx_label_b = f"RX{picotuner_rx_numbers['b']}"
''', "broadcast: TS targets")

# 2. The helper, beside the tuning watchdog's own settings.
src = apply(src,
'''PICOTUNER_RETUNE_COOLDOWN_SECS = 90.0   # don't hammer a tuner that won't take
''',
'''PICOTUNER_RETUNE_COOLDOWN_SECS = 90.0   # don't hammer a tuner that won't take

PICOTUNER_TAKEOVER_ASK_SECS = 30.0  # how often to ask a taker if it has finished
_my_ips_cache = (0.0, set())
_takeover_seen = {}                 # rcv -> (ip, last asked, answer)


def _my_ips():
    """This Pi's own addresses, refreshed once a minute."""
    global _my_ips_cache
    when, ips = _my_ips_cache
    if time.time() - when > 60:
        try:
            out = subprocess.run(["hostname", "-I"], capture_output=True,
                                 text=True, timeout=5).stdout
            ips = set(out.split())
        except Exception:
            pass
        _my_ips_cache = (time.time(), ips)
    return ips


def _ask_lynx_presence(ip, rcv):
    """The question Lynx Apple TV and iPhone answer on UDP 9950:
    "LYNX? n" -> "LYNX WATCHING n" or "LYNX IDLE". None when nothing
    answers (not a Lynx, asleep, or a phone with Lynx in the background)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(1.2)
        q = f"LYNX? {rcv}".encode()
        sock.sendto(q, (ip, 9950))
        sock.sendto(q, (ip, 9950))
        while True:
            data, addr = sock.recvfrom(256)
            if addr[0] == ip:
                return data.decode(errors="replace").strip()
    except Exception:
        return None
    finally:
        sock.close()


def _picotuner_taken_over(rcv):
    """The address of the device that has taken over receiver rcv (1 or
    2), or None when it is still ours (or nobody's).

    The Picotuner sends a receiver's $-status only to whoever tuned it, so
    a receiver someone else has tuned looks, from here, exactly like one
    that has lost its tuning: no symbol rate. Without this check the
    watchdogs "restored" it within 30 s, snatching it back from an
    iPhone watching over a VPN (3 Nov 2026)."""
    st = picotuner_state_b if rcv == 2 else picotuner_state
    target = str(st.get("ts_target", "") or "")
    ip = target.split(":")[0]
    if not ip or ip == "0.0.0.0" or ip in _my_ips():
        _takeover_seen.pop(rcv, None)
        return None
    now = time.time()
    seen = _takeover_seen.get(rcv)
    if seen is None or seen[0] != ip or now - seen[1] >= PICOTUNER_TAKEOVER_ASK_SECS:
        answer = _ask_lynx_presence(ip, rcv)
        if seen is None or seen[0] != ip:
            print(f"[picotuner] Rx{rcv} taken over by {ip} - leaving it alone "
                  f"(presence answer: {answer or 'none'})")
        elif answer != seen[2]:
            print(f"[picotuner] Rx{rcv} at {ip}: presence answer now {answer or 'none'}")
        seen = (ip, now, answer)
        _takeover_seen[rcv] = seen
    if seen[2] and seen[2].startswith("LYNX") and seen[2] != f"LYNX WATCHING {rcv}":
        return None          # a Lynx that has finished with it: ours again
    return ip
''', "helper: _picotuner_taken_over")

# 3. Tuning watchdog: skip a receiver that has been taken over.
src = apply(src,
'''            wrong = []
            for rcv, (want_khz, want_sr) in expected.items():
                st = picotuner_state_b if rcv == 2 else picotuner_state
                if rcv == 2 and not st.get("online"):
                    continue          # Rx2 not reporting at all - nothing to compare
''',
'''            wrong = []
            for rcv, (want_khz, want_sr) in expected.items():
                st = picotuner_state_b if rcv == 2 else picotuner_state
                if rcv == 2 and not st.get("online"):
                    continue          # Rx2 not reporting at all - nothing to compare
                if _picotuner_taken_over(rcv):
                    continue          # another device is using it: not a lost tuning
''', "tuning watchdog: leave a taken-over receiver alone")

# 4. Diversity stuck-lock monitor: likewise.
src = apply(src,
'''            stuck_a = check("a", picotuner_state)
            stuck_b = check("b", picotuner_state_b)
''',
'''            stuck_a = check("a", picotuner_state)
            stuck_b = check("b", picotuner_state_b)
            # A receiver another device has tuned stops reporting to us,
            # which can look stuck. It isn't ours to recover.
            if stuck_a and _picotuner_taken_over(1):
                stuck_a = False
            if stuck_b and _picotuner_taken_over(2):
                stuck_b = False
''', "diversity monitor: leave a taken-over receiver alone")

TARGET.write_text(src)
print("Done.")
