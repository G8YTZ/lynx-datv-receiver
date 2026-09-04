#!/bin/bash
# ============================================================
#  Lynx DATV Receiver — Installer
#  Replaces the manual SFTP-based setup in the install guide's
#  Sections 4-5 with a single script. Run this on a fresh
#  Raspberry Pi OS (Desktop) install, after Section 3 (SSH in).
# ============================================================
set -e   # stop on any error, rather than leaving a half-installed system

REPO_URL="https://github.com/G8YTZ/lynx-datv-receiver.git"

# Which branch to install. Defaults to beta, because that is the branch
# this script itself lives on and is fetched from - the quick-install
# one-liner in the guide points at .../beta/install.sh, so cloning
# anything else silently installs code that does not match the installer.
#
# That was a real, confirmed bug: `git clone` with no branch takes the
# repository default (main), so fetching this script from beta and
# running it produced a main checkout. The failure was not obvious -
# it surfaced much later as "cannot stat lynx-scheduled-reboot.service",
# because the systemd units only exist on beta.
#
# Override on the command line if you genuinely want another branch:
#   ./install.sh --branch main
BRANCH="beta"
if [ "$1" = "--branch" ] && [ -n "$2" ]; then
  BRANCH="$2"
  shift 2
fi

echo "=== Lynx DATV Receiver — Installer ==="
echo ""

# --- System update -----------------------------------------
echo "--- Updating system packages (this can take a while on first run) ---"
sudo apt update && sudo apt full-upgrade -y

# --- System (apt) dependencies -------------------------------
# Matches the install guide's Section 4 list exactly, plus git
# (needed to clone the repo below - almost always already
# present, but listed explicitly rather than assumed, same
# reasoning the guide itself gives for curl).
echo "--- Installing system dependencies ---"
sudo apt install -y \
  mpv \
  python3-pip \
  python3-gi \
  python3-gi-cairo \
  gir1.2-gtk-4.0 \
  gir1.2-gdkpixbuf-2.0 \
  libgtk4-layer-shell0 \
  gir1.2-gtk4layershell-1.0 \
  ffmpeg \
  curl \
  socat \
  librsvg2-bin \
  wtype \
  iw \
  chrony \
  pipewire-bin \
  wlr-randr \
  git

# --- Python dependencies -------------------------------------
# NOTE: the install guide's own copy of this line is missing
# `requests` and `gpiozero` - confirmed directly against the
# actual source tonight, both are genuinely required (Slack
# webhook notifications and GPIO Tx control respectively).
# Corrected here rather than reproducing the guide's stale list.
# `pyshp` reads the Natural Earth shapefiles in geo/ for Pathfinder
# (the end-of-contact station map). 74KB, pure Python - deliberately
# chosen over geopandas, which would drag in GEOS, PROJ, pandas and
# NumPy for what amounts to reading a few polygons.
# `pyserial` reads the optional GNSS HAT (lynx_gnss.py) over
# /dev/ttyAMA0 - harmless to install even on a receiver with no HAT
# fitted at all, since lynx_gnss.py already fails quietly with
# nothing on the port. Often already present via Raspberry Pi OS's
# own python3-serial package (confirmed: `pip install` reports
# "Requirement already satisfied" in that case rather than
# reinstalling) - listed explicitly regardless rather than assumed,
# same reasoning this section already gives for requests/gpiozero.
echo "--- Installing Python dependencies ---"
pip install --break-system-packages fastapi uvicorn pyyaml requests gpiozero pyshp pyserial numpy

# --- GPS time sync (chrony) -------------------------------------
# Only relevant to a receiver with the optional GNSS HAT fitted
# (lynx_gnss.py) - harmless either way on one without, since chrony
# simply never sees a source that never sends it anything. Feeds
# GPS time directly to chrony's own SOCK refclock driver rather than
# via gpsd, since gpsd would need exclusive ownership of the same
# serial port GnssReader already owns - see lynx_gnss.py's own
# module comment for the full rationale. Genuinely useful even
# without a HAT fitted yet: chrony is a better NTP client than
# systemd-timesyncd regardless, and this leaves the receiver ready
# for a HAT to be fitted later with no further setup needed.
#
# Deliberately placed BEFORE the --deps-only exit below, unlike the
# repo/config/autostart steps further down: this needs to happen
# automatically on every "Update Now" for an EXISTING install, not
# only on a from-scratch one, since a HAT can be fitted to a receiver
# that's already been running for months (exactly this session's own
# case). Safe to put here specifically because it's fully idempotent
# and side-effect-free on a re-run: unlike lynx_config.yaml (holds an
# operator's own site-specific settings that must never be silently
# reset) or the autostart entry (duplicating it would run two
# competing Lynx instances), disabling an already-disabled
# systemd-timesyncd or re-checking an already-present chrony.conf
# line does nothing on the second and every subsequent run.
#
# Raspberry Pi OS runs systemd-timesyncd by default - the two
# shouldn't both be actively managing the clock at once, so this
# disables it in favour of chrony.
echo "--- Setting up GPS time sync (chrony) ---"
if systemctl is-enabled systemd-timesyncd >/dev/null 2>&1 || systemctl is-active systemd-timesyncd >/dev/null 2>&1; then
  echo "Disabling systemd-timesyncd in favour of chrony..."
  sudo systemctl disable --now systemd-timesyncd
fi
CHRONY_CONF="/etc/chrony/chrony.conf"
if [ -f "$CHRONY_CONF" ]; then
  if sudo grep -q "refclock SOCK /var/run/chrony.gnss.sock" "$CHRONY_CONF"; then
    echo "chrony.conf already has the GNSS refclock line - leaving it untouched."
  else
    echo "refclock SOCK /var/run/chrony.gnss.sock refid GPS precision 1e-1" | sudo tee -a "$CHRONY_CONF" >/dev/null
    echo "Added GNSS refclock line to $CHRONY_CONF."
    sudo systemctl restart chrony
  fi
else
  echo "$CHRONY_CONF not found - skipping (unexpected chrony packaging on this OS;"
  echo "add 'refclock SOCK /var/run/chrony.gnss.sock refid GPS precision 1e-1' to"
  echo "chrony's own config file by hand, then 'sudo systemctl restart chrony')."
fi

# Socket permissions. chrony creates the SOCK refclock owned by
# root:root, mode 0755 - readable by everyone but writable only by
# root. Lynx runs as the desktop user and has to WRITE samples into
# it, so without this it connects and immediately fails with
# "[Errno 13] Permission denied", leaving GPS time sync silently
# non-functional even though chrony itself is installed and correctly
# configured. Confirmed live: this was the second of two separate
# reasons GPS time sync had never actually worked on an existing
# install.
#
# Handled with a shared group plus a systemd drop-in rather than a
# one-off chmod, because chrony recreates the socket from scratch on
# every start - a manual chmod is undone by the next restart or
# reboot. The drop-in re-applies it each time chrony starts.
#
# The '+' prefix on ExecStartPost runs that command as full root
# regardless of the unit's own User=_chrony - without it the chgrp
# fails with "Operation not permitted" AND takes the whole chrony
# service down with it, which is considerably worse than not having
# GPS time sync. The trailing 'exit 0' is the same insurance:
# timekeeping must never fail because a GPS extra didn't work.
echo "--- Setting up GNSS socket permissions ---"
sudo groupadd -f gpsshare
if id -nG "$USER" | tr ' ' '\n' | grep -qx gpsshare; then
  echo "$USER is already in the gpsshare group."
else
  sudo usermod -aG gpsshare "$USER"
  echo "Added $USER to the gpsshare group (takes effect at next login/reboot)."
fi
CHRONY_DROPIN_DIR="/etc/systemd/system/chrony.service.d"
CHRONY_DROPIN="$CHRONY_DROPIN_DIR/gnss-sock-perms.conf"
sudo mkdir -p "$CHRONY_DROPIN_DIR"
sudo tee "$CHRONY_DROPIN" >/dev/null <<'DROPIN'
[Service]
ExecStartPost=
ExecStartPost=+/bin/sh -c 'for i in $(seq 1 50); do [ -S /var/run/chrony.gnss.sock ] && break; sleep 0.1; done; if [ -S /var/run/chrony.gnss.sock ]; then chgrp gpsshare /var/run/chrony.gnss.sock && chmod g+w /var/run/chrony.gnss.sock; fi; exit 0'
DROPIN
sudo systemctl daemon-reload
sudo systemctl restart chrony 2>/dev/null || true
echo "GNSS socket permissions configured."

# --- Passwordless sudo for the privileged buttons ---------------
# ABOVE the --deps-only exit deliberately. Reboot, Shutdown, Update and
# Kill WiFi all shell out to sudo, and they are fire-and-forget by
# necessity - the server cannot report on the success of its own reboot.
# If this only ran on a fresh install, every EXISTING receiver updating
# through "Update Now" would keep the broken buttons forever, which is
# the opposite of what a fix is for. It qualifies on the --deps-only
# test's own terms: it is a dependency of those buttons working, it is
# idempotent, and redoing it on an already-configured system is safe -
# the file has fixed contents and is validated before replacing anything.
#
# Stock Raspberry Pi OS gives the first user NOPASSWD: ALL, so this
# usually changes nothing. It is not guaranteed though - it depends on
# how the user was created - and a receiver carrying an older, narrower
# rule (a /etc/sudoers.d/lynx-reboot granting reboot but not shutdown,
# say) gets a Shutdown button that stops playback, reports success and
# leaves the Pi running. Confirmed in the field.
#
# visudo -c validates BEFORE installing. A syntax error in
# /etc/sudoers.d/ locks the user out of sudo entirely, which on a remote
# receiver means a site visit, so this is not a corner worth cutting.
# Non-fatal under `set -e`: a failure here should not take down an
# otherwise good install or update.
echo "--- Configuring passwordless sudo for reboot/shutdown/rfkill ---"
# id -un, not $USER. Everything below the --deps-only exit runs in an
# interactive shell where $USER is always set, but this block also runs
# from "Update Now", which invokes this script from inside the Lynx
# process with a COPY of that process's environment. $USER survives an
# lxterminal-started Lynx and would not survive a systemd-started one -
# and an empty $USER produces a malformed rule that visudo rejects, so
# the guard below would quietly skip the fix on exactly the receivers
# that need it. id -un asks the kernel and cannot be unset.
LYNX_USER="$(id -un)"
LYNX_SUDO_TMP="$(mktemp)"
printf '%s ALL=(ALL) NOPASSWD: /sbin/reboot, /usr/sbin/reboot, /sbin/shutdown, /usr/sbin/shutdown, /sbin/poweroff, /usr/sbin/poweroff, /usr/sbin/rfkill, /usr/bin/rfkill\n' \
  "$LYNX_USER" > "$LYNX_SUDO_TMP"
if sudo visudo -c -f "$LYNX_SUDO_TMP" >/dev/null 2>&1; then
  sudo install -m 0440 -o root -g root "$LYNX_SUDO_TMP" /etc/sudoers.d/lynx
  echo "Installed /etc/sudoers.d/lynx"
  # An older install may have left a narrower rule behind. Harmless to
  # keep, but it is the thing that made Reboot work while Shutdown did
  # not, so removing it stops anyone chasing that ghost twice.
  if [ -f /etc/sudoers.d/lynx-reboot ]; then
    sudo rm -f /etc/sudoers.d/lynx-reboot
    echo "Removed superseded /etc/sudoers.d/lynx-reboot"
  fi
  if [ -f /etc/sudoers.d/lynx-shutdown ]; then
    sudo rm -f /etc/sudoers.d/lynx-shutdown
    echo "Removed superseded /etc/sudoers.d/lynx-shutdown"
  fi
else
  echo ""
  echo ">>> Could not validate the sudoers rule - NOT installing it."
  echo ">>> Everything else is installed and usable. The Reboot, Shutdown"
  echo ">>> and Kill WiFi buttons may refuse to act until this is sorted;"
  echo ">>> rebooting over SSH always works regardless."
  echo ""
fi
rm -f "$LYNX_SUDO_TMP"

# --- Persistent log directory -----------------------------------
# ABOVE the --deps-only exit, for the same reason as the sudoers block:
# an existing receiver that has only ever been updated never runs
# anything below that line, so this directory would never appear on
# exactly the installs that have been running longest. lynx_start.sh and
# lynx_app.py both prefer /var/log/lynx and fall back to /tmp when it is
# missing - a silent fallback, so the absence shows up as "the
# persistent log doesn't work" rather than as a missing directory.
# Idempotent and safe to redo on every update.
echo "--- Setting up persistent log directory ---"
sudo mkdir -p /var/log/lynx
sudo chown "$(id -un)":"$(id -gn)" /var/log/lynx

# --- Startup mechanism: systemd user service -------------------
# Lynx starts from a systemd user service, NOT the labwc autostart
# line this script used to write. lynx.service waits for the
# compositor's Wayland socket directly; the graphical-session.target
# ordering that made it unreliable - and was the only reason it was
# previously installed but left disabled - is gone.
#
# Placed ABOVE the --deps-only exit deliberately, so "Update Now"
# migrates an existing receiver rather than only fresh installs. The
# autostart repair further down sits BELOW that exit and so has never
# run on an existing install even once, which is exactly why receivers
# are still found on the original exec-bit-dependent line.
#
# Why this matters beyond tidiness: the lxterminal line runs Lynx
# inside a visible terminal window on the desktop. Whenever mpv shows
# nothing - a stream that connects but delivers no data, for instance -
# what is on the HDMI output is a desktop, a taskbar and a terminal
# full of HTTP logs. At a repeater that goes out on air.
# Defined here, called in two places: immediately below when the
# clone already exists (an existing receiver being retro-fitted via
# Update Now, which is why this section is above the --deps-only exit
# at all), and again after the clone for a fresh install, where ~/lynx
# does not exist yet. The first version of this ran the cp
# unconditionally above the exit, which with `set -e` aborted a fresh
# install outright, before anything was even cloned - invisible in
# testing, because every test was an Update Now on a machine that
# already had the clone.
LYNX_SERVICE_DONE=0
setup_startup_service() {
  echo "--- Setting up the systemd user service ---"
  mkdir -p ~/.config/systemd/user
  cp ~/lynx/lynx.service ~/.config/systemd/user/lynx.service
  systemctl --user daemon-reload
# reenable, NOT enable. `enable` adds the symlink for the unit's
# current [Install] section but does NOT remove one left by a previous
# version of that unit. A receiver carrying an older lynx.service -
# which said WantedBy=graphical-session.target, the target labwc does
# not reliably activate - keeps that stale link forever: systemctl
# reports "enabled", nothing is wrong anywhere, and the service simply
# never starts at boot. Confirmed on real hardware (2026-09-04) on a
# receiver migrated from stable. `reenable` removes every existing
# link and recreates from the unit as it stands now, which is also why
# the cp above must come first.
  systemctl --user reenable lynx.service
  LYNX_SERVICE_DONE=1
}

if [ -f ~/lynx/lynx.service ]; then
  setup_startup_service
fi

# --- Serial UART for the GNSS HAT ------------------------------
# Two separate things are needed before lynx_gnss.py can read the
# Waveshare L76K on the GPIO header, and NEITHER is true by default on
# a Pi 5. Both were confirmed the hard way (2026-09-04) on a receiver
# where the HAT was fitted, correctly wired, and completely silent.
#
# First, the device has to exist. Without enable_uart=1 there is no
# /dev/ttyAMA0 at all - /dev/serial0 points at ttyAMA10 instead - and
# every read fails with ENOENT, which reads like a missing HAT rather
# than a missing setting.
#
# Second, it has to be openable. On a Pi 5 this UART sits behind the
# PCIe bridge and comes up root:root 0600, with nothing assigning it a
# group, so being in dialout achieves precisely nothing. The chrony
# socket permissions and the gpsshare group set up further down have
# always assumed the port itself was readable; it is not.
#
# Both are skipped silently where already present, so this is safe on
# every run and on receivers with no HAT fitted - an enabled UART and a
# udev rule cost a machine without one nothing at all.
#
# NOT sufficient on a Pi 4: there /dev/ttyAMA0 is the Bluetooth UART,
# not the header, so a HAT would additionally need
# dtoverlay=disable-bt. lynx_gnss.py opens ttyAMA0 by name, so a Pi 4
# would read the wrong port even once it exists. Deliberately left
# alone rather than guessed at without the hardware to test on.
if [ -f /boot/firmware/config.txt ]; then
  if grep -q "^enable_uart=1" /boot/firmware/config.txt; then
    echo "Serial UART already enabled - leaving config.txt untouched."
  else
    echo "--- Enabling the serial UART (GNSS HAT) ---"
    sudo cp /boot/firmware/config.txt /boot/firmware/config.txt.lynx.bak
    echo "enable_uart=1" | sudo tee -a /boot/firmware/config.txt > /dev/null
    echo "Added enable_uart=1 to config.txt - takes effect at next reboot."
  fi
fi

# Third, nothing else may hold the port. cmdline.txt carries
# console=serial0,115200, and once enable_uart=1 is set serial0
# resolves to ttyAMA0 - so systemd's generator starts a getty on the
# very port the HAT needs. The getty takes ownership as root:root 0600
# and respawns whenever it exits, which silently undoes the udev rule
# above on every boot and every respawn. Confirmed on real hardware
# (2026-09-04): the HAT read correctly for a few seconds after a manual
# chgrp, then went silent again, looking exactly like flaky hardware.
#
# Masked, not disabled. The unit is enabled-runtime - generated from
# the kernel command line rather than enabled on disk - so `disable`
# is simply regenerated at the next boot. Masking is one command to
# undo (systemctl unmask) and leaves the serial console available on
# any other port, which editing cmdline.txt would not.
if systemctl list-unit-files serial-getty@.service >/dev/null 2>&1; then
  if [ "$(systemctl is-enabled serial-getty@ttyAMA0 2>/dev/null)" = "masked" ]; then
    echo "Serial getty on ttyAMA0 already masked."
  else
    echo "--- Masking the serial getty on the GNSS UART ---"
    sudo systemctl stop serial-getty@ttyAMA0 2>/dev/null || true
    sudo systemctl mask serial-getty@ttyAMA0 >/dev/null 2>&1 || true
    echo "Serial getty masked - the GNSS UART stays available to Lynx."
  fi
fi

if [ -f /etc/udev/rules.d/60-lynx-gnss.rules ]; then
  echo "GNSS serial permissions rule already present."
else
  echo "--- Setting up GNSS serial port permissions ---"
  echo 'KERNEL=="ttyAMA[0-9]*", GROUP="dialout", MODE="0660"' \
      | sudo tee /etc/udev/rules.d/60-lynx-gnss.rules > /dev/null
  sudo udevadm control --reload-rules 2>/dev/null || true
  sudo udevadm trigger --subsystem-match=tty 2>/dev/null || true
  echo "GNSS serial port permissions configured."
fi

# --- Desktop shell ---------------------------------------------
# labwc is required - the overlay is a GTK4 layer-shell client and
# needs a wlroots compositor - but the Pi desktop shell running on top
# of it is not. With it, anything that leaves mpv showing nothing puts
# a wallpaper, a taskbar and whatever windows are open on the HDMI
# output. At a repeater that goes out on air.
#
# Default on a FRESH install, opt-in on an existing one. A machine
# with no ~/lynx is a machine being turned into a receiver, and a
# dedicated receiver has no use for a taskbar it will spend its life
# hiding. An existing machine may well be a Pi someone also uses
# normally, and silently removing their desktop is not this script's
# business unless asked - so there it still needs --kiosk.
#
# --keep-desktop opts out of the fresh-install default, for anyone
# setting Lynx up on a Pi they also intend to use.
#
# Commented rather than deleted, with a .bak alongside: reversible by
# hand, and visible to whoever reads the file next. A Pi OS update may
# restore the file, in which case run with --kiosk again.
LYNX_WANT_KIOSK=0
if [ "$1" = "--kiosk" ]; then
  LYNX_WANT_KIOSK=1
elif [ ! -d ~/lynx ] && [ "$1" != "--keep-desktop" ]; then
  LYNX_WANT_KIOSK=1
  echo "Fresh install - the desktop shell will be suppressed."
  echo "(Re-run with --keep-desktop if this Pi is also used normally.)"
fi

if [ "$LYNX_WANT_KIOSK" = "1" ] && [ -f /etc/xdg/labwc/autostart ]; then
  if grep -q "^/usr/bin/lwrespawn" /etc/xdg/labwc/autostart; then
    echo "--- Suppressing the desktop shell ---"
    sudo cp /etc/xdg/labwc/autostart /etc/xdg/labwc/autostart.lynx.bak
    sudo sed -i \
      -e "s|^\(/usr/bin/lwrespawn.*\)$|#\1|" \
      -e "s|^\(/usr/bin/lxsession-xdg-autostart.*\)$|#\1|" \
      /etc/xdg/labwc/autostart
    echo "Desktop shell suppressed - labwc will run bare from next boot."
  else
    echo "Desktop shell already suppressed - leaving it untouched."
  fi
fi

# Retire the old labwc autostart line if one is present. Commented out
# rather than deleted: reversible by hand, and it leaves visible
# evidence of what changed for whoever reads the file next. Both forms
# are matched - the original one and the later "bash" repair of it -
# since a receiver may be running either. Leaving either in place
# alongside an enabled service would start Lynx twice.
if [ -f ~/.config/labwc/autostart ] \
   && grep -q "^[^#]*lynx_start\.sh" ~/.config/labwc/autostart; then
  sed -i "s|^\([^#]*lynx_start\.sh.*\)$|# retired by install.sh - Lynx now starts from lynx.service\n#\1|" \
      ~/.config/labwc/autostart
  echo "Retired the old labwc autostart line - Lynx starts from lynx.service now."
fi

# --deps-only stops here - used by "Update Now" (lynx_app.py) to
# re-confirm every OS/apt/pip dependency above, AND the GPS time-sync
# setup just above (equally safe to redo), is genuinely present on an
# existing install - without touching the repo clone, config, or
# anything else below that would be genuinely unsafe to redo on an
# already-configured system. See this section's own comment further
# up (near the apt install list) for the full rationale.
# --kiosk implies --deps-only: it is a one-off toggle run against an
# already-working receiver, not a reason to redo the whole install.
if [ "$1" = "--deps-only" ] || [ "$1" = "--kiosk" ]; then
  echo "--- Dependencies confirmed ($1) ---"
  exit 0
fi

# --- Clone the repo --------------------------------------------
echo "--- Fetching Lynx ---"
if [ -d ~/lynx ]; then
  echo "~/lynx already exists - leaving it as-is."
  echo "(Delete it first if you want a completely fresh clone instead.)"
  # Warn if what is already there is not the branch being installed -
  # otherwise the rest of this script runs against unexpected code and
  # fails later in ways that do not point back to here.
  EXISTING_BRANCH="$(git -C ~/lynx rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
  if [ "$EXISTING_BRANCH" != "$BRANCH" ]; then
    echo ""
    echo ">>> WARNING: ~/lynx is on branch '$EXISTING_BRANCH', but this"
    echo ">>> installer expects '$BRANCH'. Files this script needs may be"
    echo ">>> missing. To switch:  cd ~/lynx && git checkout $BRANCH"
    echo ""
  fi
else
  git clone -b "$BRANCH" "$REPO_URL" ~/lynx
fi

# Fresh install: the clone did not exist when the startup service was
# set up above, so do it now that it does.
if [ "$LYNX_SERVICE_DONE" = "0" ]; then
  setup_startup_service
fi

# --- Configuration ------------------------------------------
echo "--- Setting up configuration ---"
mkdir -p ~/lynx/config
if [ ! -f ~/lynx/config/lynx_config.yaml ]; then
  cp ~/lynx/lynx_config.example.yaml ~/lynx/config/lynx_config.yaml
  echo ""
  echo ">>> ACTION NEEDED: start Lynx, then go to the Web Control Portal's"
  echo ">>> Configuration page and set your Picotuner's IP address and"
  echo ">>> your own site details there - no need to hand-edit this file"
  echo ">>> for that. (Config -> Picotuner Network Settings)"
  echo ""
else
  echo "Config already exists at ~/lynx/config/lynx_config.yaml - leaving it untouched."
fi

# --- Make the start script executable ------------------------
# SFTP transfers never preserved this bit; a fresh git clone
# does preserve it correctly, but set it explicitly regardless
# so this script is safe to re-run.
#
# The autostart entry no longer DEPENDS on this - it invokes the
# script through bash, so a missing execute bit can never again
# leave a receiver silently sitting at the desktop - but someone
# typing ./lynx_start.sh by hand still needs it, and so does
# install.sh itself on the next run.
chmod +x ~/lynx/lynx_start.sh ~/lynx/install.sh 2>/dev/null || true

# --- Persistent log directory ----------------------------------
# /tmp is typically RAM-backed on Raspberry Pi OS, losing its
# contents on every reboot - genuinely confirmed as a real problem
# (2026-08-01) when a stack-trace dump meant to help diagnose an
# intermittent freeze was lost to exactly this, right when it would
# have mattered most. lynx_app.py's own diagnostic logging prefers
# this location when it's writable, falling back to /tmp otherwise -
# this just makes sure it's actually there and owned correctly from
# the start.
# --- labwc config directory ------------------------------------
# Startup itself is handled by the systemd user service set up above
# the --deps-only exit, so nothing is written to the autostart file
# any more - an lxterminal line here alongside an enabled service
# would start Lynx twice. The directory is still needed by the
# cursor-hide rc.xml written further down.
#
# Auto-login to the desktop remains deliberately unscripted: it is a
# one-time GUI toggle, and getting it wrong through an unverified
# raspi-config flag risks a broken boot state, which is a worse
# outcome than asking for one manual step.
mkdir -p ~/.config/labwc

# --- Scheduled reboot (installed, NOT enabled) ------------------
# A twice-daily reboot as a blunt backstop: it recovers from states
# nobody has specifically thought of yet, which is exactly the sort of
# thing that matters at an unattended repeater where the alternative is
# a site visit. Deliberately NOT enabled automatically - a receiver
# rebooting itself twice a day is right for a hilltop and wrong for a
# desk, so that call belongs to whoever installed it. System units
# rather than user ones, since rebooting needs root.
echo "--- Installing and enabling the nightly reboot ---"
# Guarded rather than a bare cp. With `set -e` at the top of this
# script, a missing unit file aborts the whole install here - leaving a
# half-configured system, and an error message ("cannot stat
# lynx-scheduled-reboot.service") that points at the symptom rather than
# the cause. That happened for real when the clone above took the wrong
# branch: these units exist only on beta, so a main checkout got most of
# the way through an install and then died. The branch bug is fixed
# above; this makes the failure non-fatal and self-explaining anyway,
# because a scheduled reboot is an optional extra and is not worth
# losing an otherwise good install over.
if [ -f ~/lynx/lynx-scheduled-reboot.service ] && [ -f ~/lynx/lynx-scheduled-reboot.timer ]; then
  sudo cp ~/lynx/lynx-scheduled-reboot.service /etc/systemd/system/
  sudo cp ~/lynx/lynx-scheduled-reboot.timer /etc/systemd/system/
  sudo systemctl daemon-reload
  # Enabled by default now, rather than merely installed and left for
  # the operator to discover. A receiver is an appliance expected to run
  # unattended for months, and a restart at a genuinely quiet hour costs
  # nothing while clearing anything that has slowly crept up which
  # nobody has thought to look for. 03:00, with up to 5 minutes of
  # jitter so several receivers on one site never drop together.
  #
  # Turn it off with:
  #   sudo systemctl disable --now lynx-scheduled-reboot.timer
  if sudo systemctl enable --now lynx-scheduled-reboot.timer >/dev/null 2>&1; then
    echo "Nightly reboot enabled (03:00)."
  else
    echo "Could not enable the nightly reboot timer - enable it by hand if wanted."
  fi
else
  echo ""
  echo ">>> Scheduled reboot units not found in ~/lynx - skipping."
  echo ">>> Everything else is installed and usable. This usually means"
  echo ">>> ~/lynx is on a branch that does not carry these files;"
  echo ">>> check with:  git -C ~/lynx rev-parse --abbrev-ref HEAD"
  echo ""
fi

# --- Cursor-hide keybind (labwc rc.xml) ------------------------
# Only created if rc.xml doesn't exist at all - same reasoning as
# the auto-login decision above: rc.xml is a general-purpose labwc
# config file, not Lynx-specific, so someone may already have their
# own customisations in there. Blindly merging a <keybind> into an
# existing file risks producing invalid XML or clobbering something
# unrelated - safer to only act when there's genuinely nothing there
# yet, and print clear manual instructions otherwise.
echo "--- Setting up cursor-hide keybind ---"
if [ -f ~/.config/labwc/rc.xml ]; then
  echo "rc.xml already exists - leaving it untouched."
  echo "(To hide the mouse cursor, add this inside its <keyboard> section:"
  echo '  <keybind key="A-W-h">'
  echo '    <action name="HideCursor" />'
  echo '    <action name="WarpCursor" x="-1" y="-1" />'
  echo '  </keybind>)'
else
  cat > ~/.config/labwc/rc.xml << 'RCXML'
<?xml version="1.0"?>
<openbox_config>
  <keyboard>
    <keybind key="A-W-h">
      <action name="HideCursor" />
      <action name="WarpCursor" x="-1" y="-1" />
    </keybind>
  </keyboard>
</openbox_config>
RCXML
  echo "Created."
fi

echo ""
echo "=== Install complete ==="
echo ""
echo "This receiver will reboot itself nightly at 03:00, with a few minutes"
echo "of jitter so several receivers on one site never drop together."
echo "Enabled by default - it costs nothing at that hour and clears"
echo "anything that has slowly crept up."
echo ""
echo "  Check when it will next fire:  systemctl list-timers lynx-scheduled-reboot.timer"
echo "  Turn it off:                  sudo systemctl disable --now lynx-scheduled-reboot.timer"
echo ""
echo "One thing still needs doing by hand:"
echo ""
echo "  Enable auto-login to the desktop (one-time GUI setting):"
echo "    Preferences -> Raspberry Pi Configuration -> System -> Auto login -> on"
echo "    Then reboot to confirm Lynx starts automatically."
echo ""
echo "If a GNSS HAT is fitted (optional - portable operation only), two more"
echo "one-time hardware/raspi-config steps, deliberately left manual for the"
echo "same reason auto-login is above - getting a boot-config or raspi-config"
echo "flag wrong via an unverified script is a worse outcome than asking:"
echo ""
echo "  1. Enable the GPIO header UART, then reboot:"
echo "       sudo raspi-config"
echo "       -> Interface Options -> Serial Port"
echo "       -> 'login shell over serial' = No"
echo "       -> 'enable serial port hardware' = Yes"
echo "     Confirm /boot/firmware/config.txt has 'dtparam=uart0=on'"
echo "     (add it by hand if raspi-config didn't) - reboot to apply."
echo ""
echo "  2. Confirm the HAT itself: UART jumper at position B (not A, which"
echo "     routes to onboard USB instead), STANDBY switch OFF."
echo ""
echo "  Then enable it on the Config page's GNSS Portable Locator card"
echo "  (Automatic by default), and if GPS time sync is wanted too, confirm"
echo "  chrony sees it once a fix is confirmed:  chronyc sources -v"
echo ""
echo "  Note: GPS time sync needs this user to be in the 'gpsshare' group,"
echo "  which only takes effect after a reboot (or a full log out and back"
echo "  in). Until then the locator works normally but time sync will still"
echo "  report as unavailable."
echo ""
echo "To test right now without rebooting:"
echo "  cd ~/lynx && ./lynx_start.sh"
echo ""
echo "Once it's running, set your Picotuner's IP address and site details"
echo "from the Web Control Portal's Configuration page - no need to hand-edit"
echo "lynx_config.yaml for that. (Setting the Picotuner IP needs a quick"
echo "restart of Lynx to take effect - the page will remind you.)"
echo ""
