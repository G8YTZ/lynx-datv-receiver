#!/bin/bash
# wf-recorder for Lynx's SRT screen output (lynx_screen_output.py), on
# the ROCK and the Pi 5. Built from source on both:
#  - ROCK: against ffmpeg-rockchip in /usr/local, for the rkmpp hardware
#    encoders; Debian's wf-recorder is linked to Debian's FFmpeg, which
#    has none.
#  - Pi: against the system FFmpeg (libx264), for the PipeWire audio
#    backend and frame pacing of 0.6.0, which older Debian releases lack.
# Patches: FFmpeg 8 (removed AVCodec fields; guarded by version, so a
# no-op on older FFmpeg) and zero-copy DRM_PRIME capture (rkmpp only).
# A failed build costs SRT output, not the install: always exits 0.
# Default sound backend pulse, which Lynx uses (through pipewire-pulse):
# set to pipewire, meson refused to configure where PipeWire is older
# than 1.0.5 (Debian 12 based systems), so wf-recorder never built
# (Thomas DL5BCA, 11 Nov 2026). Its PipeWire backend still builds where
# it can; Lynx asks for pulse explicitly either way.
LYNX_DIR="${LYNX_DIR:-$HOME/lynx}"
SRC_DIR="${SRC_DIR:-$HOME/src}"
TAG=v0.6.0
BIN=/usr/local/bin/wf-recorder

ok() {
  [ -x "$BIN" ] || return 1
  "$BIN" --version 2>/dev/null | grep -q "${TAG#v}" || return 1
  grep -qa 'rkmpp: DMA-BUF capture' "$BIN" || return 1     # patched build
  if [ "${LYNX_ROCK:-0}" = "1" ]; then
    ldd "$BIN" 2>/dev/null | grep -q '/usr/local/lib/libavcodec' || return 1
  fi
}

if ok; then
  echo "wf-recorder already installed ($TAG)"
  exit 0
fi

echo "--- Building wf-recorder $TAG for SRT screen output (about a minute) ---"
(
  sudo apt install -y build-essential pkg-config meson ninja-build git \
    libwayland-dev wayland-protocols libgbm-dev libdrm-dev \
    libpulse-dev libpipewire-0.3-dev || exit 1
  if [ "${LYNX_ROCK:-0}" = "1" ]; then
    export PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/local/lib/aarch64-linux-gnu/pkgconfig
    CODEC=h264_rkmpp
  else
    # The system FFmpeg's development files.
    sudo apt install -y libavcodec-dev libavformat-dev libavutil-dev \
      libavfilter-dev libavdevice-dev libswresample-dev libswscale-dev || exit 1
    CODEC=libx264
  fi
  mkdir -p "$SRC_DIR" && cd "$SRC_DIR" || exit 1
  rm -rf wf-recorder
  git clone --depth 1 -b "$TAG" https://github.com/ammen99/wf-recorder.git || exit 1
  cd wf-recorder || exit 1
  git apply "$LYNX_DIR/patches/wf-recorder-0.6.0-ffmpeg8.patch" || exit 1
  git apply "$LYNX_DIR/patches/wf-recorder-0.6.0-rkmpp-dmabuf.patch" || exit 1
  meson setup build --prefix=/usr/local --buildtype=release \
    -Ddefault_codec="$CODEC" -Ddefault_audio_backend=pulse || exit 1
  ninja -C build || exit 1
  sudo ninja -C build install && sudo ldconfig
) || true

if ok; then
  echo "wf-recorder installed."
else
  echo ">>> wf-recorder did not build: SRT screen output will be unavailable."
fi
exit 0
