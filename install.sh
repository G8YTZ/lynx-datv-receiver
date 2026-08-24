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

# --deps-only stops here - used by "Update Now" (lynx_app.py) to
# re-confirm every OS/apt/pip dependency above, AND the GPS time-sync
# setup just above (equally safe to redo), is genuinely present on an
# existing install - without touching the repo clone, config, or
# anything else below that would be genuinely unsafe to redo on an
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

# --- Scheduled reboot (installed, NOT enabled) ------------------
# A twice-daily reboot as a blunt backstop: it recovers from states
# nobody has specifically thought of yet, which is exactly the sort of
# thing that matters at an unattended repeater where the alternative is
# a site visit. Deliberately NOT enabled automatically - a receiver
# rebooting itself twice a day is right for a hilltop and wrong for a
# desk, so that call belongs to whoever installed it. System units
# rather than user ones, since rebooting needs root.
echo "--- Installing (not enabling) scheduled reboot units ---"
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
echo "For a repeater or other unattended site, consider enabling the"
echo "scheduled reboot - twice daily, at 04:00 and 16:00 with a few"
echo "minutes of jitter so several receivers don't all drop at once:"
echo ""
echo "    sudo systemctl enable --now lynx-scheduled-reboot.timer"
echo ""
echo "  Check when it will next fire:  systemctl list-timers lynx-scheduled-reboot.timer"
echo "  Turn it off again:            sudo systemctl disable --now lynx-scheduled-reboot.timer"
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
