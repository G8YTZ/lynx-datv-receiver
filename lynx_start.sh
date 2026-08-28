#!/bin/bash
# ============================================================
#  Lynx — Master startup script
#  G8YTZ / EI3IOB  —  July 2026
#
#  Architecture: mpv plays udp://@:9941 ALWAYS in true kiosk
#  mode (no chrome, no menus, no progress bar — mpv never
#  draws any of that unless explicitly enabled).
#  Picotuner sends live TS to port 9941 when locked; when
#  unlocked, mpv shows nothing/last-frame and the transparent
#  OSD overlay (lynx_overlay.py) covers it with the Lynx logo.
#  The player and the OSD are fully decoupled — swapping
#  players again later needs no OSD changes at all.
# ============================================================

# Explicitly guarantee the standard system directories are always
# searched, regardless of what PATH (if any) the launching environment
# provided - confirmed directly as a real, plausible cause of commands
# like ping/ip silently failing (command not found, indistinguishable
# from a genuine timeout inside a redirected `if` check) specifically
# when launched via labwc's autostart, versus working fine from a
# manual SSH-triggered launch. Prepended, not replaced - anything
# already present in an inherited PATH is still searched too.
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

LYNX_DIR="$(cd "$(dirname $0)" && pwd)"
# No command-line arguments needed — tuning is entirely handled by
# lynx_app.py's own startup resume logic (previous state, or the
# configured default boot preset). This script's job is just to start
# the web app, the player, and the overlay.
PICOTUNER_IP="192.168.0.126"
PICOTUNER_CMD_PORT=9921
PICOTUNER_TS_PORT=9941
STREAM_TS_PORT=9945   # separate port for web streams — see notes below
MPV_SOCKET="/tmp/mpv-socket"
LAYER_SHELL_LIB="/usr/lib/aarch64-linux-gnu/libgtk4-layer-shell.so.0"

RED='\033[0;31m'
GREEN='\033[0;32m'
AMBER='\033[0;33m'
BLUE='\033[0;34m'
NC='\033[0m'

cleanup() {
    echo -e "\n${AMBER}Shutting down Lynx...${NC}"
    echo "[to@wh] ts=1 tsip=0.0.0.0 tsport=0" | \
        nc -u -w1 ${PICOTUNER_IP} ${PICOTUNER_CMD_PORT} 2>/dev/null
    kill ${APP_PID} ${OVERLAY_PID} 2>/dev/null
    pkill -f lynx_overlay.py 2>/dev/null
    killall mpv vlc 2>/dev/null
    wait ${APP_PID} 2>/dev/null
    echo -e "${GREEN}Done.${NC}"
    exit 0
}
trap cleanup SIGINT SIGTERM

# Preserves the last part of lynx_app.py's own live log to persistent
# storage - see this file's own patch history / CHANGELOG for the
# full rationale on why this is only ever done here, at the moment a
# real problem is detected, rather than continuously. Best-effort:
# never anything this script depends on succeeding, so failing
# quietly is correct if /var/log/lynx isn't writable (an install that
# predates this feature, or genuinely out of space).
save_log_tail() {
    if [ -w /var/log/lynx ] 2>/dev/null; then
        tail -200 /tmp/lynx_app.log > "/var/log/lynx/lynx_app_$(date -u +%Y%m%dT%H%M%SZ).log" 2>/dev/null || true
        # Keep only the 10 most recent - each incident is small, but
        # nothing here should be left to accumulate unbounded forever.
        ls -t /var/log/lynx/lynx_app_*.log 2>/dev/null | tail -n +11 | xargs -r rm -- 2>/dev/null || true
    fi
}

echo -e "${BLUE}╔══════════════════════════════════════╗${NC}"
echo -e "${BLUE}║       Lynx DATV Receiver             ║${NC}"
echo -e "${BLUE}║       G8YTZ / EI3IOB  2026           ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════╝${NC}"

# ── Wait for network to be genuinely ready ──────────────────────
# Autostart launches this script the moment labwc itself starts,
# which can be BEFORE the network interface has finished DHCP/come
# up at all — confirmed directly: the NTP sync below reliably
# succeeds within a couple of seconds on a manual SSH-triggered
# launch, but has been observed to NEVER succeed at all when this
# script is launched via labwc's autostart. A genuine reachability
# check (not just "interface has an IP") catches this properly.
echo -ne "Waiting for network... "
for i in $(seq 1 45); do
    GATEWAY=$(ip route show default 2>/dev/null | awk '/default/ {print $3; exit}')
    if [ -n "$GATEWAY" ] && ping -c1 -W1 "$GATEWAY" > /dev/null 2>&1; then
        echo -e "${GREEN}up${NC}"
        break
    fi
    sleep 1
    if [ "$i" = "45" ]; then
        if [ -z "$GATEWAY" ]; then
            echo -e "${AMBER}no default route found — proceeding anyway${NC}"
        else
            echo -e "${AMBER}default gateway ($GATEWAY) not responding to ping — proceeding anyway${NC}"
        fi
    fi
done

# ── Disable WiFi power-save ──────────────────────────────────────
# brcmfmac (this hardware's WiFi driver, Broadcom BCM4345/6) has a
# well-documented history of silently wedging or failing to
# reconnect, specifically tied to power-save mode - confirmed
# directly as the cause of a real overnight crash: network completely
# dead (no SSH, no Web UI) while the rest of the system, including
# Lynx's own process, stayed fully healthy the entire time (confirmed
# via SIGUSR1 stack dumps showing every thread genuinely idle, not
# stuck) - dmesg explicitly showed
# "brcmf_cfg80211_set_power_mgmt: power save enabled" at boot.
# Placed here, right after the network-up wait above rather than
# before it, so wlan0 (if present at all) has already had up to 20s
# to actually come up before this tries to configure it. Non-fatal if
# wlan0 doesn't exist (a wired-only install) or `iw` isn't installed -
# also re-applied every cycle in the watchdog loop further down, in
# case a later reconnect/roam event ever resets it.
iw wlan0 set power_save off 2>/dev/null || true

# ── Wait for NTP time sync ──────────────────────────────────────
# On boot the system clock is often wrong until NTP corrects it a few
# seconds later — that correction (a backward time jump) was found to
# trigger a real-time-scheduling validation failure in
# xdg-desktop-portal/RTKit ("Could not get pidns for pid ...") which
# cascades into mpv being killed shortly after starting, with no OOM
# or session-related cause at all. Waiting here avoids ever starting
# mpv into that window.
#
# Capped at 45s - extended from an original 10s cap per Justin's own
# request (a future use case). Confirmed directly this cap can
# genuinely matter: NTP sync was observed taking around 30s on this
# exact hardware on at least one real boot, most likely tied to the
# same WiFi instability investigated elsewhere this session - the
# original 10s cap would have given up and proceeded before that sync
# actually completed. Still capped, not indefinite: a genuinely
# offline site (no internet at the repeater location, isolated
# network, etc) would otherwise pay this full cost on every single
# boot for nothing. Offline sites are actually fine either way — if
# NTP can never reach a server, the clock never jumps at all, which is
# exactly the condition this wait exists to protect against in the
# first place.
echo -ne "Waiting for system time sync... "
for i in $(seq 1 45); do
    if [ "$(timedatectl show --property=NTPSynchronized --value 2>/dev/null)" = "yes" ]; then
        echo -e "${GREEN}synced${NC}"
        break
    fi
    sleep 1
    if [ "$i" = "45" ]; then
        echo -e "${AMBER}no internet/NTP available — proceeding (this is fine)${NC}"
    fi
done


# ── Kill existing processes ───────────────────────────────────
# Uses -9 and waits for confirmation — a previous lynx_app.py instance
# still holding port 9901 when a new one starts (both using
# SO_REUSEPORT) silently splits incoming Picotuner status packets
# between the two, causing MER/margin/FEC data to appear randomly
# missing in the new instance. Make sure the old one is truly gone.
#
# Also sweep for stray manual `nc -lu 9901`/`nc -lu 9997` listeners —
# these are easy to leave running after a debugging session and, via
# the same SO_REUSEPORT mechanism, silently steal a share of the
# Picotuner's status packets from lynx_app.py's own listener. This
# genuinely happened and cost a debugging session to track down.
pkill -9 -f lynx_app.py 2>/dev/null
pkill -9 -f lynx_overlay.py 2>/dev/null
pkill -9 -f ffmpeg 2>/dev/null
pkill -9 -f "nc -lu 990" 2>/dev/null   # matches 9901, 9902 etc
pkill -9 -f "nc -lu 999" 2>/dev/null   # matches 9997 etc
killall -9 mpv vlc 2>/dev/null
rm -f ${MPV_SOCKET}
for i in $(seq 1 10); do
    pgrep -f lynx_app.py > /dev/null || break
    sleep 0.5
done
sleep 1

# ── Start web app ─────────────────────────────────────────────
# Output goes to both the terminal (live, as before) AND a
# persistent log file — previously only mpv and the combiner had
# their own logs, so a crash in lynx_app.py itself (the orchestrator
# for both of them) left no evidence anywhere once the terminal
# session that launched it was gone. Process substitution here
# (rather than a plain pipe to tee) is deliberate: a pipe would
# make $! refer to tee's own PID instead of lynx_app.py's, breaking
# the cleanup trap and watchdog checks further down this script
# that rely on APP_PID genuinely being the Python process itself —
# confirmed directly before deploying this.
#
# The log is written to /var/log/lynx as well as /tmp, when that
# directory is writable. /tmp alone is erased by a reboot - which is
# precisely the event most worth having a log of, and the reason a
# refusing Reboot button took two days to diagnose: every attempt
# destroyed its own evidence. save_log_tail() above covers the cases the
# watchdog itself detects, but not a reboot Lynx asked for deliberately.
#
# Appended, not truncated, so it survives across boots; rotated on size
# here rather than via logrotate, so an install needs no extra package
# and no root-owned config to keep it bounded.
echo -ne "Starting Lynx web app... "
APP_LOG_TARGETS=(/tmp/lynx_app.log)
rm -f /tmp/lynx_app.log
if [ -w /var/log/lynx ] 2>/dev/null; then
    # Rotate the stack-trace logs on the same principle as the app log
    # below. These are opened for APPEND by faulthandler in both
    # lynx_app.py and lynx_overlay.py, and written to again by this
    # script's own watchdog - so unlike everything else in
    # /var/log/lynx they had no cap at all and grew across reboots
    # forever. Each incident is only about a kilobyte, which is why it
    # went unnoticed, but a fault that recurs on an unattended site
    # writes one every time it happens: the HDMI hotplug case alone
    # produced three inside a quarter of an hour. Small threshold,
    # because these are diagnostic breadcrumbs rather than a log
    # anyone reads in bulk, and the recent ones are the useful ones.
    for _st in /var/log/lynx/stacktrace.log /var/log/lynx/stacktrace_overlay.log; do
        if [ -f "$_st" ] && \
           [ "$(stat -c %s "$_st" 2>/dev/null || echo 0)" -gt 2000000 ]; then
            mv -f "$_st" "${_st}.1" 2>/dev/null || true
        fi
    done
    PERSIST_LOG=/var/log/lynx/lynx_app.log
    if [ -f "$PERSIST_LOG" ] && \
       [ "$(stat -c %s "$PERSIST_LOG" 2>/dev/null || echo 0)" -gt 20000000 ]; then
        mv -f "$PERSIST_LOG" "${PERSIST_LOG}.1" 2>/dev/null || true
    fi
    printf '\n=== %s  Lynx starting ===\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$PERSIST_LOG" 2>/dev/null || true
    APP_LOG_TARGETS+=("$PERSIST_LOG")
fi
python3 ${LYNX_DIR}/lynx_app.py > >(tee -a "${APP_LOG_TARGETS[@]}") 2>&1 &
APP_PID=$!
for i in $(seq 1 10); do
    sleep 1
    curl -sf http://localhost:8080/api/status > /dev/null 2>&1 && break
done
if ! curl -sf http://localhost:8080/api/status > /dev/null 2>&1; then
    echo -e "${RED}failed!${NC}"; exit 1
fi
MY_IP=$(hostname -I | awk '{print $1}')
echo -e "${GREEN}OK — http://${MY_IP}:8080${NC}"

# ── Ensure the PipeWire sink itself is at full volume ───────────
# Lynx's own volume control (web UI slider, lynx_config.yaml default)
# only ever adjusts mpv's OWN internal software gain — a completely
# separate layer from PipeWire's own hardware-level sink volume.
# Found sitting at 40% by default on a fresh Pi image, silently
# capping all audio regardless of what Lynx's own volume was set to.
# @DEFAULT_AUDIO_SINK@ always refers to whatever the current default
# sink actually is, since its numeric ID isn't guaranteed stable
# across reboots.
wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.0 2>/dev/null || true

# ── mpv is started by lynx_app.py itself, not here ─────────────
# See rf_mpv_lifecycle_monitor() in lynx_app.py: mpv is only ever
# started once a genuine signal lock is confirmed (or immediately
# for streams), and lynx_app.py's own resume-on-startup flow handles
# picking up whatever Lynx was last doing. An earlier, direct
# "permanent kiosk mode" launch used to live here, pointed at port
# 9941 immediately and unconditionally - but that ran before the
# Picotuner had necessarily locked at all, and if it failed to stay
# alive for a few seconds (entirely normal when there's no signal yet
# at boot), this script aborted its ENTIRE startup sequence, never
# even starting lynx_app.py, the overlay, or resuming RF state.
# Confirmed live as a real, intermittent failure - removed entirely.

# ── Start OSD overlay — shows logo when unlocked, OSD always ──────
echo -ne "Starting OSD overlay... "
LD_PRELOAD=${LAYER_SHELL_LIB} python3 ${LYNX_DIR}/lynx_overlay.py > /tmp/lynx_overlay.log 2>&1 &
OVERLAY_PID=$!
sleep 2
if ! kill -0 ${OVERLAY_PID} 2>/dev/null; then
    echo -e "${AMBER}overlay failed to start (non-fatal)${NC}"
    cat /tmp/lynx_overlay.log
else
    echo -e "${GREEN}OK${NC}"
fi

# ── Hide the mouse cursor ───────────────────────────────────────
# Triggers labwc's own HideCursor+WarpCursor keybind (see rc.xml) -
# placed here, after the overlay has already confirmed starting,
# rather than in ~/.config/labwc/autostart with a fixed sleep, since
# by this point labwc is guaranteed to have been running long enough
# to have its keybinds registered - a fixed sleep elsewhere risked
# racing against this script's own, much longer startup sequence
# (network wait, NTP wait, etc).
# Checked explicitly, once, rather than just letting the command fail
# silently - confirmed directly (2026-08-01) that an install predating
# wtype being added to install.sh's dependency list left the cursor
# permanently visible with absolutely nothing in the logs to explain
# why, since the failure was fully swallowed by `2>/dev/null || true`
# below. git pull / Update Now only ever pull code, never install
# system packages, so any install that predates this fix needs `sudo
# apt install -y wtype` run by hand regardless of how up to date its
# code is - this warning at least makes that obvious in the startup
# log instead of a silent, invisible failure.
if ! command -v wtype > /dev/null 2>&1; then
    echo -e "${AMBER}wtype not installed - mouse cursor will stay visible.${NC}"
    echo -e "${AMBER}Fix: sudo apt install -y wtype${NC}"
fi
wtype -M alt -M logo -P h 2>/dev/null || true

# ── Tuning is now handled inside lynx_app.py itself ────────────
# On startup it automatically resumes whatever Lynx was last doing
# (crash recovery, watchdog restart, scheduled reboot, or a genuine
# power cycle), falling back to the explicitly-configured default
# boot preset only if there's no valid previous state. Command-line
# arguments to this script are no longer used at all — this matches
# how it's actually invoked automatically (no arguments), rather than
# testing with args that don't reflect real startup behaviour.
echo -e "${GREEN}RF will resume automatically (previous state, or default boot preset)${NC}"

echo -e "\n${GREEN}Lynx running!${NC}"
echo -e "  Web UI : http://${MY_IP}:8080"
echo -e "  (see web UI or overlay for actual resumed frequency/stream)"
echo -e "  Ctrl+C to stop"

# Tell systemd startup is genuinely complete (harmless no-op if not
# running under systemd — systemd-notify just isn't found/needed then).
systemd-notify --ready 2>/dev/null || true

# ── Health-check / watchdog loop ────────────────────────────────
# Runs instead of a plain `wait` so we can detect a genuine HANG (API
# stops responding, or mpv/the web app dies) rather than only a clean
# exit. Pings systemd's watchdog while healthy; if anything looks
# wrong, exits so systemd's Restart=always brings everything back up
# fresh rather than limping along in a broken state indefinitely.
while true; do
    sleep 10

    # Re-hide the mouse cursor - the one at startup only ever fires
    # once, so if a mouse gets plugged in later (e.g. during
    # troubleshooting a frozen screen), the cursor stays visible
    # indefinitely with nothing to re-hide it short of a full restart.
    # Cheap enough to just re-trigger every cycle rather than track
    # whether it's actually needed.
    wtype -M alt -M logo -P h 2>/dev/null || true

    # Re-disable WiFi power-save every cycle too, same rationale as
    # the cursor re-hide above - cheap, non-disruptive, and a
    # reconnect/roam event could in principle reset this without a
    # full reboot, so there's no reason to trust it stays off forever
    # just because it was set once at startup. See the startup-time
    # comment (above, near the network-wait loop) for the full
    # rationale on why this exists at all.
    iw wlan0 set power_save off 2>/dev/null || true

    status_json=$(curl -sf http://localhost:8080/api/status 2>/dev/null)
    if [ -z "$status_json" ]; then
        echo -e "${RED}Web app not responding — exiting for restart.${NC}"
        # Capture what every thread inside lynx_app.py was actually
        # doing at the exact moment this was detected, before it gets
        # killed and restarted - the one chance to catch a genuinely
        # hung lock or background thread directly, rather than losing
        # that evidence the moment the process is replaced. Same
        # /var/log/lynx-preferred, /tmp-fallback logic as
        # lynx_app.py's own faulthandler setup, so this marker and the
        # actual dump always land in the same file.
        STACKTRACE_LOG="/tmp/lynx_stacktrace.log"
        [ -w /var/log/lynx ] 2>/dev/null && STACKTRACE_LOG="/var/log/lynx/stacktrace.log"
        echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) - web app unresponsive, dumping stacks ===" >> "$STACKTRACE_LOG"
        kill -USR1 "$APP_PID" 2>/dev/null || true
        sleep 1  # give it a moment to actually write before the kill below
        break
    fi
    # Check for ANY currently-running mpv process matching our socket —
    # NOT the original MPV_PID captured at launch. lynx_app.py's
    # restart_mpv() intentionally kills and replaces mpv on every
    # tune/stream switch as part of normal operation; checking the
    # stale original PID here meant every single legitimate restart
    # looked identical to a genuine crash, since that old PID
    # genuinely is gone — even though a healthy new mpv process (with
    # a different PID) is running at that exact moment. This was
    # shutting down the whole stack after every routine restart.
    #
    # Confirmed a SECOND, related bug still present after that fix:
    # even checking for "any" mpv by pattern, there is a genuine,
    # intentional gap during every tune - old mpv killed, brief pause,
    # new one started - where NO mpv matches at all, entirely by
    # design. This check runs once every 10s; if it happens to land
    # inside that few-second gap, it can't tell a normal transition
    # apart from a real crash. Confirmed via live reports of the whole
    # stack going down after ordinary preset/memory switches, with no
    # diversity mode involved at all. Fixed two ways: first, checking
    # the same transition marker lynx_app.py already sets for the
    # overlay's benefit, which spans the entire deliberate gap; second,
    # as defense-in-depth for any tune path that might not set it, a
    # short retry rather than concluding death from one single snapshot.
    #
    # Confirmed a THIRD, genuinely distinct bug on a fresh install with
    # no previous state and no default boot preset configured: Lynx
    # correctly stays idle in that case, and rf_mpv_lifecycle_monitor()
    # in lynx_app.py deliberately never starts mpv at all while idle
    # (only once an RF signal lock is confirmed, or a stream is
    # playing) - so mpv's absence here was never a crash, just the
    # correct, intended state, and this check had no way to tell the
    # difference. Fixed by reading the actual mode this same status
    # response already carries, and skipping the mpv check entirely
    # whenever it's "idle".
    lynx_mode=$(echo "$status_json" | grep -o '"mode"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed -E 's/.*:[[:space:]]*"([^"]*)"/\1/')
    if [ "$lynx_mode" = "idle" ]; then
        : # genuinely, correctly idle - mpv isn't supposed to be running at all, nothing to check
    elif [ -f /tmp/lynx_mpv_transitioning ]; then
        : # deliberate, in-progress tune transition — mpv's temporary absence is expected, skip this check entirely for this cycle
    elif ! pgrep -f "mpv.*input-ipc-server=${MPV_SOCKET}" > /dev/null 2>&1; then
        # Not found on the first check — could still be a normal
        # transition that this cycle's timing just caught mid-gap.
        # Give it a moment and re-check before concluding it's genuinely dead.
        sleep 3
        if [ -f /tmp/lynx_mpv_transitioning ] || pgrep -f "mpv.*input-ipc-server=${MPV_SOCKET}" > /dev/null 2>&1; then
            : # resolved itself — was a normal transition, not a crash
        else
            echo -e "${RED}mpv has died — exiting for restart.${NC}"
            save_log_tail
            break
        fi
    fi
    if ! kill -0 ${APP_PID} 2>/dev/null; then
        echo -e "${RED}Web app process has died — exiting for restart.${NC}"
        save_log_tail
        break
    fi

    # Overlay heartbeat check - see lynx_overlay.py's own top-of-file
    # comment for the full incident this is fixed against: a stall
    # where the overlay process stayed alive and its threads stayed
    # genuinely busy (confirmed directly via strace - a completely
    # normal-looking GTK event loop), but nothing was actually
    # reaching the screen - a plain "is the process alive" check would
    # never catch this, only proof of a genuinely completed render
    # will. Only checked if the heartbeat file actually exists at all
    # - its absence just means either the overlay hasn't drawn its
    # first frame yet (early in startup) or is running an older
    # version that predates this feature, neither of which is itself
    # a failure.
    OVERLAY_HEARTBEAT_FILE="/tmp/lynx_overlay_heartbeat"
    if [ -f "$OVERLAY_HEARTBEAT_FILE" ]; then
        heartbeat_age=$(( $(date +%s) - $(stat -c %Y "$OVERLAY_HEARTBEAT_FILE" 2>/dev/null || echo 0) ))
        if [ "$heartbeat_age" -gt 30 ]; then
            echo -e "${RED}Overlay heartbeat stale (${heartbeat_age}s) — not actually rendering.${NC}"
            # Same idea as the web-app stack dump above: capture what
            # every overlay thread is actually doing right now, before
            # it gets killed and replaced - the one chance to catch
            # this specific stall directly rather than losing the
            # evidence the moment the process is restarted.
            OVERLAY_STACKTRACE_LOG="/tmp/lynx_stacktrace_overlay.log"
            [ -w /var/log/lynx ] 2>/dev/null && OVERLAY_STACKTRACE_LOG="/var/log/lynx/stacktrace_overlay.log"
            echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) - overlay heartbeat stale, dumping stacks ===" >> "$OVERLAY_STACKTRACE_LOG"
            kill -USR1 "$OVERLAY_PID" 2>/dev/null || true
            sleep 1  # give it a moment to actually write before rebooting/exiting
            save_log_tail

            # A plain process-level restart alone is confirmed NOT
            # sufficient for this specific failure - see this file's
            # own top-of-file comment (the section on this patch) for
            # the full rationale. Rate-limited via a PERSISTENT marker
            # (surviving the very reboot this triggers, unlike /tmp)
            # so a genuinely bad case - this recurring immediately
            # after every reboot - can never turn into a tight loop.
            REBOOT_MARKER="/var/log/lynx/last_overlay_reboot"
            [ ! -w /var/log/lynx ] 2>/dev/null && REBOOT_MARKER="/tmp/lynx_last_overlay_reboot"
            now=$(date +%s)
            last_attempt=$(cat "$REBOOT_MARKER" 2>/dev/null || echo 0)
            if [ $(( now - last_attempt )) -gt 900 ] && sudo -n true 2>/dev/null; then
                echo "$now" > "$REBOOT_MARKER"
                echo -e "${RED}Rebooting the Pi — a process-level restart alone has not been sufficient for this.${NC}"
                # Genuine hard reset via SysRq, not a plain `sudo
                # reboot` - see this script's own patch history /
                # CHANGELOG for the full rationale: a plain reboot did
                # NOT reliably clear this overnight, while a full
                # physical power cycle did. Sync first (SysRq's own
                # 's') to avoid needless SD card corruption, then the
                # actual hard reboot ('b').
                echo 1 | sudo tee /proc/sys/kernel/sysrq > /dev/null 2>&1
                sync
                echo s | sudo tee /proc/sysrq-trigger > /dev/null 2>&1
                sleep 2
                echo b | sudo tee /proc/sysrq-trigger > /dev/null 2>&1
                sleep 5
                # Fallback if SysRq wasn't available/didn't take effect -
                # harmless no-op if the system already went down above.
                sudo reboot
                sleep 30  # the reboot itself takes a few seconds to actually happen - avoid racing ahead into cleanup below
            else
                echo -e "${AMBER}Not triggering a recovery reboot (attempted recently, or passwordless sudo unavailable) — exiting for restart instead.${NC}"
            fi
            break
        fi
    fi

    systemd-notify WATCHDOG=1 2>/dev/null || true
done

cleanup
