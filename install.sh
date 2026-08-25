#!/bin/bash
# ============================================================
#  Lynx DATV Receiver — Installer
#  Replaces the manual SFTP-based setup in the install guide's
#  Sections 4-5 with a single script. Run this on a fresh
#  Raspberry Pi OS (Desktop) install, after Section 3 (SSH in).
# ============================================================
set -e   # stop on any error, rather than leaving a half-installed system

REPO_URL="https://github.com/G8YTZ/lynx-datv-receiver.git"

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
  git

# --- Python dependencies -------------------------------------
# NOTE: the install guide's own copy of this line is missing
# `requests` and `gpiozero` - confirmed directly against the
# actual source tonight, both are genuinely required (Slack
# webhook notifications and GPIO Tx control respectively).
# Corrected here rather than reproducing the guide's stale list.
echo "--- Installing Python dependencies ---"
pip install --break-system-packages fastapi uvicorn pyyaml requests gpiozero

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
# exactly the installs that have been running longest.
#
# /tmp is RAM-backed on Raspberry Pi OS and loses its contents on every
# reboot - confirmed as a real problem (2026-08-01) when a stack-trace
# dump meant to diagnose an intermittent freeze was lost to exactly
# that, and again on 2026-08-25 when a refusing Reboot button destroyed
# its own evidence every time it was tried. lynx_start.sh and
# lynx_app.py both prefer /var/log/lynx and fall back to /tmp when it is
# missing - a silent fallback, so its absence shows up as "the
# persistent log doesn't work" rather than as a missing directory.
# Idempotent and safe to redo on every update.
echo "--- Setting up persistent log directory ---"
sudo mkdir -p /var/log/lynx
sudo chown "$(id -un)":"$(id -gn)" /var/log/lynx

# --deps-only stops here - used by "Update Now" (lynx_app.py) to
# re-confirm every OS/apt/pip dependency above is genuinely present
# on an existing install, without touching the repo clone, config, or
# anything else below that would be unsafe to redo on an
# already-configured system. See this section's own comment further
# up (near the apt install list) for the full rationale.
if [ "$1" = "--deps-only" ]; then
  echo "--- Dependencies confirmed (--deps-only) ---"
  exit 0
fi

# --- Clone the repo --------------------------------------------
echo "--- Fetching Lynx ---"
if [ -d ~/lynx ]; then
  echo "~/lynx already exists - leaving it as-is."
  echo "(Delete it first if you want a completely fresh clone instead.)"
else
  git clone "$REPO_URL" ~/lynx
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
chmod +x ~/lynx/lynx_start.sh

# --- Autostart file (labwc) -----------------------------------
# This is the one part of Section 8 that's safe to script -
# creating the file itself is unambiguous. Enabling auto-login
# to the desktop is NOT scripted here deliberately: it's a
# one-time GUI toggle, and getting it wrong via an unverified
# raspi-config flag risks leaving the Pi in a broken boot state,
# a worse outcome than just asking for one manual step.
echo "--- Setting up labwc autostart ---"
mkdir -p ~/.config/labwc
if [ -f ~/.config/labwc/autostart ] && grep -q "lynx_start.sh" ~/.config/labwc/autostart; then
  echo "Autostart already configured - leaving it untouched."
else
  echo "lxterminal -e $HOME/lynx/lynx_start.sh &" >> ~/.config/labwc/autostart
  chmod +x ~/.config/labwc/autostart
fi

# --- systemd user service (installed, NOT enabled) --------------
# lynx.service exists and lynx_start.sh already calls systemd-notify
# (READY, and WATCHDOG=1 from its own health-check loop) for exactly
# this purpose - genuinely working when started manually
# (systemctl --user start lynx.service). But confirmed directly on
# real hardware (2026-07-31) that labwc doesn't reliably activate
# graphical-session.target on its own, which lynx.service waits on -
# meaning if enabled, it can sit there doing nothing at boot while
# never actually starting Lynx, with no obvious sign anything's
# wrong. Installed here so it's ready to experiment with later
# (a fix for the graphical-session.target gap, once found, could be
# added to this same autostart file above) - but NOT enabled, so the
# proven, working lxterminal line above remains the one thing this
# install actually depends on to start Lynx on boot.
echo "--- Installing (not enabling) systemd user service ---"
mkdir -p ~/.config/systemd/user
cp ~/lynx/lynx.service ~/.config/systemd/user/lynx.service
systemctl --user daemon-reload

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
echo "One thing still needs doing by hand:"
echo ""
echo "  Enable auto-login to the desktop (one-time GUI setting):"
echo "    Preferences -> Raspberry Pi Configuration -> System -> Auto login -> on"
echo "    Then reboot to confirm Lynx starts automatically."
echo ""
echo "To test right now without rebooting:"
echo "  cd ~/lynx && ./lynx_start.sh"
echo ""
echo "Once it's running, set your Picotuner's IP address and site details"
echo "from the Web Control Portal's Configuration page - no need to hand-edit"
echo "lynx_config.yaml for that. (Setting the Picotuner IP needs a quick"
echo "restart of Lynx to take effect - the page will remind you.)"
echo ""
