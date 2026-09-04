#!/usr/bin/env python3
"""
Independent, standalone health watchdog for Lynx - deliberately
outside lynx_start.sh's own process tree entirely, run periodically by
its own systemd timer (lynx-watchdog.timer), so it keeps running even
if the entire Lynx stack - and its own internal watchdog loop - has
become completely unresponsive.

Built directly in response to a real, confirmed incident: SSH itself
became completely unreachable while systemd's own hardware watchdog
(RuntimeWatchdogUSec=1min, confirmed present and active) kept petting
normally throughout, never firing - strong evidence the kernel
scheduler itself stayed alive and responsive the whole time, while the
network/graphics subsystem was genuinely, completely dead. systemd's
own hardware watchdog only proves the kernel is scheduling threads; it
has no way to know whether networking or the display pipeline
specifically have failed - Justin's own analogy captures this exactly:
a 555 timer wired to the wrong pin, being reset by something that
stayed alive throughout, not by the thing that actually mattered.

This checks something that actually reflects whether the system is
USABLE, not just executing instructions - and, per the same evidence,
can still act where systemd's own watchdog couldn't, precisely because
the kernel itself is confirmed to keep functioning even during this
exact failure mode.

Two independent checks, each with its own separate consecutive-failure
counter (persisted to a small state file, since this script itself is
deliberately short-lived - launched fresh every 30s by its own systemd
timer, not a long-running loop, so nothing here can itself become the
kind of process that gets stuck):

  1. Network reachability - pings the actual default gateway directly,
     not just checking whether the interface reports itself as "up".
     Confirmed directly, tonight: an interface can still show a
     nominally healthy state while genuinely unable to pass any
     traffic at all.
  2. Overlay render health - reuses the exact same heartbeat file
     lynx_overlay.py's own background thread already maintains (see
     that file's own top-of-file comments for the full history here),
     rather than duplicating that detection logic. Given a much longer
     threshold than lynx_start.sh's own equivalent check uses (60s
     there) - this is deliberately the LAST-RESORT backup for when
     that mechanism's own attempt at recovery has already had a fair
     chance to work, not a competing first responder for the same
     failure.

Three consecutive failures of EITHER check (so a single, momentary
blip in either doesn't itself trigger a reboot) forces an immediate
`sudo reboot` - the whole point being this happens completely outside
Lynx's own process tree, so it isn't itself vulnerable to whatever
already took the rest of the system down.
"""
import subprocess
import os
import time
import json

STATE_FILE = "/var/log/lynx/watchdog_state.json"
OVERLAY_HEARTBEAT_FILE = "/tmp/lynx_overlay_heartbeat"
HEARTBEAT_STALE_SECS = 180  # deliberately generous relative to lynx_start.sh's
                             # own 60s equivalent check - this is a last-resort
                             # backup, not a competing first responder
CONSECUTIVE_FAILURES_BEFORE_REBOOT = 3


def get_default_gateway():
    """Resolves the ACTUAL current default gateway rather than a
    hardcoded IP - confirmed correct either way, since guessing wrong
    here would silently check reachability to nothing meaningful."""
    try:
        result = subprocess.run(["ip", "route", "show", "default"],
                                 capture_output=True, text=True, timeout=5)
        parts = result.stdout.split()
        if "via" in parts:
            return parts[parts.index("via") + 1]
    except Exception:
        pass
    return None


def check_network():
    gateway = get_default_gateway()
    if not gateway:
        return False  # can't even determine the gateway - treat as a failure, not "unknown"
    try:
        result = subprocess.run(["ping", "-c", "1", "-W", "3", gateway],
                                 capture_output=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False


def check_overlay_heartbeat():
    if not os.path.exists(OVERLAY_HEARTBEAT_FILE):
        # Not yet written (e.g. very early in startup) - not itself a
        # failure, same reasoning as lynx_start.sh's own equivalent check.
        return True
    age = time.time() - os.path.getmtime(OVERLAY_HEARTBEAT_FILE)
    return age <= HEARTBEAT_STALE_SECS


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"network_failures": 0, "overlay_failures": 0}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError:
        pass


LYNX_CONFIG_PATH = "/home/pi/lynx/config/lynx_config.yaml"


def load_poe_config():
    """Reads the optional poe_recovery section from the same
    lynx_config.yaml the rest of Lynx uses - read-only, never written
    here. Returns None if not present, not enabled, or missing any
    required field, so the caller can cleanly fall through to the
    SysRq hard reset without any special-casing."""
    try:
        import yaml
        with open(LYNX_CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}
        poe = config.get("poe_recovery", {}) or {}
        if not poe.get("enabled"):
            return None
        required = ["controller_url", "site", "username", "password", "switch_mac", "port_idx"]
        if not all(poe.get(k) for k in required):
            return None
        return poe
    except Exception:
        return None


def try_poe_power_cycle():
    """Attempts a genuine PoE power cycle via the UniFi controller API
    - see this file's own top-of-file comment for the full rationale
    and the IMPORTANT caveat that this is not yet field-tested against
    real hardware. Returns True if a power-cycle request was
    successfully SENT (not necessarily confirmed complete - there's no
    reliable way to verify that from this side, since the Pi is about
    to lose power), False if not configured or the attempt itself
    failed outright, in which case the caller falls through to the
    SysRq hard reset instead."""
    poe = load_poe_config()
    if not poe:
        return False

    import urllib.request
    import http.cookiejar
    import ssl
    import json as json_module

    # UniFi controllers commonly use self-signed certificates by
    # default on the local LAN - not a meaningful security concern for
    # a purely local controller/switch relationship, but urllib will
    # otherwise refuse the connection outright.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        urllib.request.HTTPSHandler(context=ctx),
    )

    controller_url = poe["controller_url"].rstrip("/")
    is_udm = poe.get("controller_type") == "udm"
    login_path = "/api/auth/login" if is_udm else "/api/login"
    cmd_prefix = "/proxy/network" if is_udm else ""

    try:
        login_body = json_module.dumps(
            {"username": poe["username"], "password": poe["password"]}).encode()
        login_req = urllib.request.Request(
            controller_url + login_path, data=login_body,
            headers={"Content-Type": "application/json"}, method="POST")
        opener.open(login_req, timeout=10)

        cycle_body = json_module.dumps({
            "cmd": "power-cycle",
            "mac": poe["switch_mac"],
            "port_idx": poe["port_idx"],
        }).encode()
        cycle_req = urllib.request.Request(
            f"{controller_url}{cmd_prefix}/api/s/{poe['site']}/cmd/devmgr",
            data=cycle_body, headers={"Content-Type": "application/json"}, method="POST")
        opener.open(cycle_req, timeout=10)

        print("[lynx-watchdog] PoE power-cycle request sent successfully.")
        return True
    except Exception as e:
        print(f"[lynx-watchdog] PoE power-cycle attempt failed ({e}) - falling back to SysRq hard reset.")
        return False


def hard_reboot():
    """Genuine hard reset via SysRq, not a plain `sudo reboot` - see
    this file's own patch history for the full rationale: a plain
    reboot did NOT reliably clear this overnight, while a full
    physical power cycle did. Explicitly enables sysrq first rather
    than assuming it's already on. Syncs filesystems (SysRq's own 's'
    function) before the actual reboot ('b'), to avoid needless SD
    card corruption. Falls back to a plain `sudo reboot` if SysRq
    itself wasn't available or somehow didn't take effect - a harmless
    no-op if the system already went down via the SysRq trigger."""
    try:
        subprocess.run(["sudo", "sh", "-c", "echo 1 > /proc/sys/kernel/sysrq"], timeout=5)
        subprocess.run(["sync"], timeout=10)
        subprocess.run(["sudo", "sh", "-c", "echo s > /proc/sysrq-trigger"], timeout=5)
        time.sleep(2)
        subprocess.run(["sudo", "sh", "-c", "echo b > /proc/sysrq-trigger"], timeout=5)
        time.sleep(5)
    except Exception:
        pass
    # Fallback if SysRq wasn't available/didn't take effect - harmless
    # no-op if the system already went down above.
    subprocess.run(["sudo", "reboot"])


def main():
    state = load_state()

    network_ok = check_network()
    overlay_ok = check_overlay_heartbeat()

    state["network_failures"] = 0 if network_ok else state.get("network_failures", 0) + 1
    state["overlay_failures"] = 0 if overlay_ok else state.get("overlay_failures", 0) + 1

    print(f"[lynx-watchdog] network_ok={network_ok} (consecutive failures={state['network_failures']}), "
          f"overlay_ok={overlay_ok} (consecutive failures={state['overlay_failures']})")

    if (state["network_failures"] >= CONSECUTIVE_FAILURES_BEFORE_REBOOT or
            state["overlay_failures"] >= CONSECUTIVE_FAILURES_BEFORE_REBOOT):
        print("[lynx-watchdog] Sustained failure detected on an independent check - rebooting.")
        state["network_failures"] = 0
        state["overlay_failures"] = 0
        save_state(state)
        if not try_poe_power_cycle():
            hard_reboot()
        return

    save_state(state)


if __name__ == "__main__":
    main()
