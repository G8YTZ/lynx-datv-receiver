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

# --- Persistent log directory ----------------------------------
# /tmp is typically RAM-backed on Raspberry Pi OS, losing its
# contents on every reboot - genuinely confirmed as a real problem
# (2026-08-01) when a stack-trace dump meant to help diagnose an
# intermittent freeze was lost to exactly this, right when it would
# have mattered most. lynx_app.py's own diagnostic logging prefers
# this location when it's writable, falling back to /tmp otherwise -
# this just makes sure it's actually there and owned correctly from
# the start.
echo "--- Setting up persistent log directory ---"
sudo mkdir -p /var/log/lynx
sudo chown "$USER":"$USER" /var/log/lynx

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

# --- Passwordless sudo for the privileged buttons ---------------
# Reboot, Shutdown, Update and Kill WiFi in the Web UI all shell out to
# sudo. They are fire-and-forget by necessity - the server cannot report
# on the success of its own reboot - so the app checks permission first
# and refuses rather than claiming a success that never happens.
#
# Stock Raspberry Pi OS gives the first user NOPASSWD: ALL, so this is
# usually already satisfied and this step changes nothing. It is not
# guaranteed though: it depends on how the user was created, and when it
# is absent the buttons fail in a way that reads like a broken install
# rather than a missing permission. Cheaper to make it explicit.
#
# visudo -c validates the file BEFORE it is installed. A syntax error in
# /etc/sudoers.d/ locks the user out of sudo entirely, which on a remote
# receiver means a site visit, so this is not a corner worth cutting.
# Guarded and non-fatal under `set -e` for the same reason the scheduled
# reboot units are: a failure here should not take down an otherwise
# good install.
echo "--- Configuring passwordless sudo for reboot/shutdown/rfkill ---"
LYNX_SUDO_TMP="$(mktemp)"
printf '%s ALL=(ALL) NOPASSWD: /sbin/reboot, /usr/sbin/reboot, /sbin/shutdown, /usr/sbin/shutdown, /sbin/poweroff, /usr/sbin/poweroff, /usr/sbin/rfkill, /usr/bin/rfkill\n' \
  "$USER" > "$LYNX_SUDO_TMP"
if sudo visudo -c -f "$LYNX_SUDO_TMP" >/dev/null 2>&1; then
  sudo install -m 0440 -o root -g root "$LYNX_SUDO_TMP" /etc/sudoers.d/lynx
  echo "Installed /etc/sudoers.d/lynx"
else
  echo ""
  echo ">>> Could not validate the sudoers rule - NOT installing it."
  echo ">>> Everything else is installed and usable. The Reboot, Shutdown"
  echo ">>> and Kill WiFi buttons may refuse to act until this is sorted;"
  echo ">>> rebooting over SSH always works regardless."
  echo ""
fi
rm -f "$LYNX_SUDO_TMP"

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
