#!/bin/bash
# ============================================================
#  Lynx — Radxa ROCK 5T platform setup
#  Called by install.sh when it finds a ROCK 5T. Also safe to run
#  on its own: every step checks before it acts, so a second run
#  changes nothing that is already right.
#
#  Base OS: Armbian Debian 13 (trixie), MINIMAL, vendor kernel 6.1.
#  The same Debian as Raspberry Pi OS on the Pi 5, so Python, GTK4,
#  labwc and gtk4-layer-shell are the same versions on both boards.
#
#  Everything here was found the hard way on 21 Oct 2026. Each step
#  says why it exists, so nobody "tidies" one away.
# ============================================================
set -e

LYNX_DIR="${LYNX_DIR:-$HOME/lynx}"
LYNX_USER="$(id -un)"
SRC_DIR="$HOME/src"
REBOOT_NEEDED=0

# Versions of the media stack this script builds. Bump here, nowhere
# else. The stamp file records what is installed, so a re-run skips the
# (15 minute) build unless a version below has changed.
MEDIA_STACK_VERSION="1"
MPV_TAG="v0.41.0"
STAMP=/usr/local/share/lynx/media-stack.version

say() { echo ""; echo "--- $* ---"; }

# --- Is this really a ROCK 5T on Armbian trixie? ------------------
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)"
case "$MODEL" in
  *"ROCK 5T"*|*"Rock 5T"*) ;;
  *) echo "This script is for the Radxa ROCK 5T (found: '${MODEL:-unknown}')."; exit 1 ;;
esac
. /etc/os-release
if [ "${VERSION_CODENAME:-}" != "trixie" ]; then
  echo "Lynx on the ROCK 5T needs Armbian trixie (found: ${PRETTY_NAME:-unknown})."
  echo "Write the 'Trixie vendor minimal' image from armbian.com/boards/rock-5t first."
  exit 1
fi
echo "Platform: $MODEL, $PRETTY_NAME, kernel $(uname -r)"

# --- GPU: Panthor, not Rockchip's Mali driver ---------------------
# Armbian's vendor kernel boots with Rockchip's own Mali driver
# (/dev/mali0). Mesa cannot drive that, so labwc, GTK and mpv would all
# fall back to software rendering - hopeless at 4K. The Panthor overlay
# switches the GPU to the open driver Mesa uses. Takes effect on reboot.
say "GPU driver (Panthor)"
ENVTXT=/boot/armbianEnv.txt
if grep -qE '^overlays=.*panthor-gpu' "$ENVTXT"; then
  echo "Panthor overlay already enabled."
elif grep -q '^overlays=' "$ENVTXT"; then
  sudo sed -i 's/^overlays=\(.*\)$/overlays=\1 panthor-gpu/' "$ENVTXT"
  echo "Added panthor-gpu to the existing overlays line."; REBOOT_NEEDED=1
else
  echo "overlays=panthor-gpu" | sudo tee -a "$ENVTXT" >/dev/null
  echo "Enabled the panthor-gpu overlay."; REBOOT_NEEDED=1
fi

# --- Hardware video access for the video group --------------------
# MPP (decode/encode), RGA (scaling) and the DMA buffer heaps arrive
# root-only on Armbian. Without this mpv only works as root.
say "Video hardware permissions"
sudo tee /etc/udev/rules.d/99-rockchip-media.rules >/dev/null <<'EOF'
KERNEL=="mpp_service", MODE="0660", GROUP="video"
KERNEL=="rga", MODE="0660", GROUP="video"
SUBSYSTEM=="dma_heap", MODE="0660", GROUP="video"
EOF
sudo udevadm control --reload && sudo udevadm trigger
sudo usermod -aG video,render,audio "$LYNX_USER"

# --- Packages ------------------------------------------------------
# The ROCK list differs from the Pi's in three ways:
#  - no Debian mpv or ffmpeg: ours in /usr/local must be the ones used
#  - Armbian minimal has no desktop, audio server or even ping, so the
#    pieces Raspberry Pi OS Desktop provides are listed here
#  - Python modules come from apt, not pip (gpiozero is Pi-only and
#    Lynx's GPIO code fails soft without it)
say "System packages"
sudo apt update
sudo apt install -y \
  labwc wlr-randr wtype \
  libgtk4-layer-shell0 gir1.2-gtk4layershell-1.0 gir1.2-gtk-4.0 gir1.2-gdkpixbuf-2.0 \
  python3-gi python3-gi-cairo python3-cairo \
  python3-fastapi python3-uvicorn python3-yaml python3-requests \
  python3-serial python3-numpy python3-pyshp \
  pipewire pipewire-pulse pipewire-bin wireplumber \
  iputils-ping curl socat librsvg2-bin iw chrony git tmux \
  hdhomerun-config

# --- Media stack: MPP, RGA, ffmpeg-rockchip, mpv -------------------
# TODO: Replace with prebuilt .debs from the GitHub release, so a user
# install takes seconds rather than 15 minutes of compiling. Until then
# this builds from source, exactly as proven on 21 Oct 2026.
media_stack_ok() {
  [ "$(cat "$STAMP" 2>/dev/null)" = "$MEDIA_STACK_VERSION $MPV_TAG" ] || return 1
  command -v ffmpeg >/dev/null && ffmpeg -hide_banner -decoders 2>/dev/null | grep -q hevc_rkmpp || return 1
  mpv --no-config --no-osc --version >/dev/null 2>&1 || return 1   # --no-osc only exists with Lua
}

if media_stack_ok; then
  say "Media stack already installed ($MEDIA_STACK_VERSION, mpv $MPV_TAG)"
else
  say "Building the media stack (about 15 minutes)"
  sudo apt install -y build-essential pkg-config cmake meson ninja-build nasm \
    libdrm-dev libx264-dev libx265-dev libssl-dev libsrt-openssl-dev \
    libass-dev libfreetype-dev libfontconfig-dev libplacebo-dev \
    libwayland-dev wayland-protocols libxkbcommon-dev \
    libegl-dev libgles-dev libgbm-dev libdisplay-info-dev \
    libpulse-dev libasound2-dev libpipewire-0.3-dev libjpeg-dev \
    libuchardet-dev liblcms2-dev zlib1g-dev libluajit-5.1-dev
  export PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/local/lib/aarch64-linux-gnu/pkgconfig
  mkdir -p "$SRC_DIR"; cd "$SRC_DIR"

  # MPP and RGA: The branches ffmpeg-rockchip is developed against.
  [ -d rkmpp ] || git clone -b jellyfin-mpp --depth 1 https://github.com/nyanmisaka/mpp.git rkmpp
  cmake -S rkmpp -B rkmpp/build -DCMAKE_INSTALL_PREFIX=/usr/local \
    -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON -DBUILD_TEST=OFF
  make -C rkmpp/build -j"$(nproc)"
  sudo make -C rkmpp/build install

  [ -d rkrga ] || git clone -b jellyfin-rga --depth 1 https://github.com/nyanmisaka/rk-mirrors.git rkrga
  [ -d rkrga/build ] || meson setup rkrga rkrga/build --prefix=/usr/local --libdir=lib \
    --buildtype=release --default-library=shared -Dcpp_args=-fpermissive \
    -Dlibdrm=false -Dlibrga_demo=false
  sudo ninja -C rkrga/build install
  sudo ldconfig

  # ffmpeg-rockchip (FFmpeg 8 based): rkmpp decoders and encoders, RGA.
  [ -d ffmpeg-rockchip ] || git clone --depth 1 https://github.com/nyanmisaka/ffmpeg-rockchip.git
  cd ffmpeg-rockchip
  ./configure --prefix=/usr/local --enable-gpl --enable-version3 --enable-nonfree \
    --enable-shared --disable-static \
    --enable-libdrm --enable-rkmpp --enable-rkrga \
    --enable-libx264 --enable-libx265 --enable-libsrt --enable-openssl \
    --enable-libass --enable-libfreetype --enable-libfontconfig
  make -j"$(nproc)"
  sudo make install && sudo ldconfig
  cd "$SRC_DIR"

  # mpv: 0.41 or later (FFmpeg 8 removed names older mpv uses), Wayland
  # only, Lua REQUIRED (Lynx's drift-correction script and --no-osc both
  # need it; left on "auto" it silently builds without), plus our
  # two-part rkmpp patch for zero-copy decode (see the patch header).
  rm -rf mpv
  git clone --depth 1 -b "$MPV_TAG" https://github.com/mpv-player/mpv.git
  cd mpv
  git apply "$LYNX_DIR/patches/mpv-0.41-rkmpp.patch"
  meson setup build --prefix=/usr/local -Dwayland=enabled -Dx11=disabled \
    -Degl=enabled -Ddrm=enabled -Dgbm=enabled -Dlua=luajit
  ninja -C build
  sudo ninja -C build install && sudo ldconfig
  cd "$SRC_DIR"

  sudo mkdir -p "$(dirname "$STAMP")"
  echo "$MEDIA_STACK_VERSION $MPV_TAG" | sudo tee "$STAMP" >/dev/null
  media_stack_ok || { echo ">>> Media stack built but failed its self-check."; exit 1; }
  echo "Media stack installed."
fi

# --- Audio: PipeWire user session -----------------------------------
# Raspberry Pi OS Desktop runs this already. Armbian minimal does not,
# and without it mpv has no HDMI audio and Lynx's level meter reports
# "audio stream ended" on a loop.
say "Audio (PipeWire)"
systemctl --user enable --now pipewire pipewire-pulse wireplumber 2>/dev/null || \
  echo "PipeWire will start with the first desktop login."

# --- Kiosk: log in on the console and start labwc --------------------
# getty auto-login rather than a display manager: if labwc ever exits,
# the console logs in again and restarts it, so the box heals itself.
# (greetd's auto-login did not survive a reboot in testing.)
say "Kiosk start-up (auto-login, labwc)"
if systemctl is-enabled greetd >/dev/null 2>&1; then
  sudo systemctl disable greetd
  echo "Disabled greetd."
fi
sudo mkdir -p /etc/systemd/system/getty@tty1.service.d
sudo tee /etc/systemd/system/getty@tty1.service.d/autologin.conf >/dev/null <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $LYNX_USER --noclear %I \$TERM
EOF
if ! grep -q 'Lynx: start labwc' ~/.bash_profile 2>/dev/null; then
  cat >> ~/.bash_profile <<'EOF'

# Lynx: start labwc on the console at boot
if [ "$(tty)" = "/dev/tty1" ] && [ -z "$WAYLAND_DISPLAY" ]; then
    export GTK_A11Y=none
    exec labwc
fi
EOF
  echo "labwc will start at boot on the console."
fi

# --- labwc: pointer hidden --------------------------------------------
# Same keybind as the Pi (Alt+Super+H hides the pointer and parks it
# off-screen). labwc's own autostart presses it once labwc is ready, so
# it cannot race a slow boot; lynx_start.sh presses it again when Lynx
# starts. Moving a mouse brings the pointer back, by design.
say "Mouse pointer"
mkdir -p ~/.config/labwc
if [ ! -f ~/.config/labwc/rc.xml ] || ! grep -q 'HideCursor' ~/.config/labwc/rc.xml; then
  [ -f ~/.config/labwc/rc.xml ] && cp ~/.config/labwc/rc.xml ~/.config/labwc/rc.xml.lynx.bak
  cat > ~/.config/labwc/rc.xml <<'EOF'
<?xml version="1.0"?>
<openbox_config>
  <keyboard>
    <keybind key="A-W-h">
      <action name="HideCursor" />
      <action name="WarpCursor" x="-1" y="-1" />
    </keybind>
  </keyboard>
</openbox_config>
EOF
  echo "Pointer-hide keybind installed."
fi
if ! grep -q 'Lynx: hide the pointer' ~/.config/labwc/autostart 2>/dev/null; then
  cat >> ~/.config/labwc/autostart <<'EOF'
# Lynx: hide the pointer once labwc is up (Alt+Super+H, bound in rc.xml)
sh -c 'sleep 3; wtype -M alt -M logo -P h' &
EOF
  echo "Pointer hidden at every start of labwc."
fi

# --- Lynx keeps running with nobody logged in ----------------------------
# Lynx is a user service. Without linger, systemd stops a user's
# services when their last session ends - so when labwc exited (a
# monitor unplugged, 21 Oct 2026), the web UI went with it.
say "User services survive logout (linger)"
sudo loginctl enable-linger "$LYNX_USER"

# --- Ping for ordinary users -----------------------------------------------
# Armbian's ping lacks the capability to run unprivileged, so Lynx's
# "wait for the network" check failed every time and waited 45 s at each
# start. Debian's own recommended fix: allow unprivileged ICMP sockets.
say "Unprivileged ping"
echo 'net.ipv4.ping_group_range = 0 2147483647' | sudo tee /etc/sysctl.d/99-lynx-ping.conf >/dev/null
sudo sysctl --system >/dev/null

# Deliberately NOT done: forcing the HDMI connector on (video=HDMI-A-1:e).
# At 4K50 HDMI 2.0 needs scrambling re-enabled in the monitor whenever its
# input wakes, and the hot-plug event is what triggers it. Forced on, an
# LG 27UK850 came back from another input to a signal it would not show.
# Monitor-off is handled in lynx_start.sh instead (no screen, no verdict).

# --- Lynx service: the ROCK's extra environment ------------------------
# install.sh installs the same lynx.service as on the Pi. On the Pi,
# labwc's session hands the service its Wayland details; here the
# service is started by the user manager, so it is told directly.
say "Lynx service environment"
mkdir -p ~/.config/systemd/user/lynx.service.d
cat > ~/.config/systemd/user/lynx.service.d/rock.conf <<'EOF'
[Service]
Environment=WAYLAND_DISPLAY=wayland-0 GTK_A11Y=none
EOF
systemctl --user daemon-reload 2>/dev/null || true

echo ""
echo "=== ROCK 5T platform setup complete ==="
if [ "$REBOOT_NEEDED" = "1" ]; then
  echo ">>> The GPU driver changed: reboot before using Lynx (sudo reboot)."
fi
