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
  git

# --- Python dependencies -------------------------------------
# NOTE: the install guide's own copy of this line is missing
# `requests` and `gpiozero` - confirmed directly against the
# actual source tonight, both are genuinely required (Slack
# webhook notifications and GPIO Tx control respectively).
# Corrected here rather than reproducing the guide's stale list.
echo "--- Installing Python dependencies ---"
pip install --break-system-packages fastapi uvicorn pyyaml requests gpiozero

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

# --- systemd user service (replaces labwc autostart) -----------
# Previously just a bare "lxterminal -e lynx_start.sh &" line in
# labwc's own autostart file - that only ever launches Lynx once, at
# boot, with nothing bringing it back if it ever exits or hangs.
# lynx_start.sh's own health-check loop already calls systemd-notify
# (READY, and WATCHDOG=1 every ~10s) for exactly this purpose - it's
# just never had a real systemd unit wired up to act on it until now.
# A user-level (not system-level) service, since mpv and the OSD
# overlay need the logged-in user's own Wayland session (labwc) and
# PipeWire audio - resources scoped to that user's session, not
# available to a system-wide service.
echo "--- Setting up systemd user service ---"
mkdir -p ~/.config/systemd/user
cp ~/lynx/lynx.service ~/.config/systemd/user/lynx.service
systemctl --user daemon-reload
systemctl --user enable lynx.service
loginctl enable-linger "$USER" 2>/dev/null || true

# Remove the old autostart line if present, so Lynx isn't launched
# twice (once by labwc autostart, once by the new service) - leaves
# the rest of the autostart file untouched, since it's a
# general-purpose labwc file, not Lynx-specific.
if [ -f ~/.config/labwc/autostart ] && grep -q "lynx_start.sh" ~/.config/labwc/autostart; then
  sed -i '/lynx_start\.sh/d' ~/.config/labwc/autostart
  echo "Removed the old autostart line - the systemd service replaces it."
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
echo "  (or: systemctl --user start lynx.service - runs the same script, but"
echo "   under systemd's own supervision, matching how it'll actually run on boot)"
echo ""
echo "Once it's running, set your Picotuner's IP address and site details"
echo "from the Web Control Portal's Configuration page - no need to hand-edit"
echo "lynx_config.yaml for that. (Setting the Picotuner IP needs a quick"
echo "restart of Lynx to take effect - the page will remind you.)"
echo ""
