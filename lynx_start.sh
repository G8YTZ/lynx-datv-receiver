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
for i in $(seq 1 20); do
    if ping -c1 -W1 1.1.1.1 > /dev/null 2>&1; then
        echo -e "${GREEN}up${NC}"
        break
    fi
    sleep 1
    if [ "$i" = "20" ]; then
        echo -e "${AMBER}no network detected — proceeding anyway${NC}"
    fi
done

# ── Wait for NTP time sync ──────────────────────────────────────
# On boot the system clock is often wrong until NTP corrects it a few
# seconds later — that correction (a backward time jump) was found to
# trigger a real-time-scheduling validation failure in
# xdg-desktop-portal/RTKit ("Could not get pidns for pid ...") which
# cascades into mpv being killed shortly after starting, with no OOM
# or session-related cause at all. Waiting here avoids ever starting
# mpv into that window.
#
# Capped at 10s (a real sync over an internet connection typically
# completes in a couple of seconds, so this is still generous) rather
# than proceeding forever, since a genuinely offline site (no internet
# at the repeater location, isolated network, etc) would otherwise pay
# a much longer cost on every single boot for nothing. Offline sites
# are actually fine either way — if NTP can never reach a server, the
# clock never jumps at all, which is exactly the condition this wait
# exists to protect against in the first place.
echo -ne "Waiting for system time sync... "
for i in $(seq 1 10); do
    if [ "$(timedatectl show --property=NTPSynchronized --value 2>/dev/null)" = "yes" ]; then
        echo -e "${GREEN}synced${NC}"
        break
    fi
    sleep 1
    if [ "$i" = "10" ]; then
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
echo -ne "Starting Lynx web app... "
python3 ${LYNX_DIR}/lynx_app.py > >(tee /tmp/lynx_app.log) 2>&1 &
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

    if ! curl -sf http://localhost:8080/api/status > /dev/null 2>&1; then
        echo -e "${RED}Web app not responding — exiting for restart.${NC}"
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
    if [ -f /tmp/lynx_mpv_transitioning ]; then
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
            break
        fi
    fi
    if ! kill -0 ${APP_PID} 2>/dev/null; then
        echo -e "${RED}Web app process has died — exiting for restart.${NC}"
        break
    fi

    systemd-notify WATCHDOG=1 2>/dev/null || true
done

cleanup
