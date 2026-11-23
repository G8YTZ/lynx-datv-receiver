#!/bin/bash
# Installs the Lynx Remote Rx from this checkout, Longmynd included.
#
#   cd ~/lynx-datv-receiver/slave && ./install.sh
#
# Puts the wrapper and the systemd unit in place, keeps an existing
# /etc/lynx-slave.conf (only adding the transport settings it lacks), and
# restarts the service. Run again after a git pull that changes the
# wrapper or the unit; changes to the Python need only a restart.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

if ! command -v srt-live-transmit >/dev/null; then
    echo "Installing srt-tools for the SRT transport"
    sudo apt-get install -y srt-tools
fi

# Longmynd, the BATC's open-source Minitiouner driver, built in ~/longmynd.
# Skipped if it is already there, so running this again is harmless.
lm_dir=/home/pi/longmynd
if [ ! -x "$lm_dir/longmynd" ]; then
    echo "Building Longmynd in $lm_dir"
    sudo apt-get install -y git make gcc libusb-1.0-0-dev libasound2-dev
    if [ ! -d "$lm_dir" ]; then
        git clone --depth 1 https://github.com/BritishAmateurTelevisionClub/longmynd.git "$lm_dir"
    fi
    make -C "$lm_dir"
fi
# Lets Longmynd open the Minitiouner without root. Takes effect the next
# time the Minitiouner is plugged in (or after a reboot).
if [ ! -f /etc/udev/rules.d/minitiouner.rules ]; then
    sudo cp "$lm_dir/minitiouner.rules" /etc/udev/rules.d/
    sudo udevadm control --reload-rules
    echo "Installed the Minitiouner udev rule: unplug and replug it, or reboot"
fi

# ── Knucker (BATC CombiTuner), only if one is here ──────────────────
#
# Done only for a card that needs it. The Knucker's driver,
# CombiTunerExpress, is a closed 32-bit armhf binary, and running it on
# 64-bit Raspberry Pi OS needs the armhf architecture and its runtime
# libraries. That is 30-50 MB and an odd-looking package list forever,
# which a Minitiouner card should not carry for a tuner it will never
# have. DVB-S2 is the common case; this is the exception.
#
# Triggered by a Knucker actually being plugged in, or by --knucker for
# building a card before the tuner arrives.
#
# The processor runs 32-bit ARM natively - AArch32 is a hardware
# execution state on these cores, not emulation - so there is no
# performance cost here, only the multiarch packaging.
knucker_wanted=false
for a in "$@"; do [ "$a" = "--knucker" ] && knucker_wanted=true; done
if [ "$knucker_wanted" = false ] \
   && [ "$(python3 "$here/detect_tuner.py" 2>/dev/null || true)" = "knucker" ]; then
    knucker_wanted=true
    echo "A Knucker is plugged in: installing its driver as well"
fi

cte=/home/pi/knucker/CombiTunerExpress
if [ "$knucker_wanted" = true ]; then
    if ! dpkg --print-foreign-architectures | grep -qx armhf; then
        echo "Adding the armhf architecture for the Knucker's 32-bit driver"
        sudo dpkg --add-architecture armhf
        sudo apt-get update
    fi
    # The loader plus the libraries this binary has been seen to want.
    # Not a guess left to stand: the check below reads what it ACTUALLY
    # needs and says so, rather than leaving a card that looks
    # installed and fails at the first tune.
    sudo apt-get install -y libc6:armhf libusb-1.0-0:armhf libstdc++6:armhf \
        || echo "Some armhf packages would not install - the check below will say if it matters"

    if [ ! -x "$cte" ]; then
        echo "Fetching CombiTunerExpress"
        mkdir -p "$(dirname "$cte")"
        curl -sSL -o "$cte" \
          https://raw.githubusercontent.com/BritishAmateurTelevisionClub/ryde-build/master/configs/dvbt/CombiTunerExpress
        chmod +x "$cte"
    fi

    # Verify rather than hope. ldd cannot read a 32-bit ARM object from
    # a 64-bit userland ("not a dynamic executable", which is ldd
    # failing and not the binary being static), but readelf reads any
    # architecture. Every NEEDED library is checked against what is
    # actually installed for armhf.
    missing=""
    for lib in $(readelf -d "$cte" 2>/dev/null \
                 | sed -n 's/.*NEEDED.*\[\(.*\)\].*/\1/p'); do
        if ! ls /lib/arm-linux-gnueabihf/"$lib"* \
               /usr/lib/arm-linux-gnueabihf/"$lib"* >/dev/null 2>&1; then
            missing="$missing $lib"
        fi
    done
    if [ -n "$missing" ]; then
        echo "WARNING: CombiTunerExpress still needs:$missing"
        echo "Find the package with:  apt-file search <library>  (then install it :armhf)"
        echo "The Knucker will not tune until those are present."
    else
        echo "Knucker driver installed, and every library it needs is present"
    fi
fi

# /usr/local/bin/lynx-slave is a thin launcher, NOT the wrapper. It
# hands straight over to slave/lynx-slave in the git checkout, so that
# a remote update - which can only ever change the checkout - can change
# how the service starts.
#
# It used to be a copy of the wrapper, and the result was a Remote Rx
# that updated its Python perfectly and went on running the old
# start-up logic for ever, because nothing in the update path could
# reach /usr/local/bin. Tuner auto-detection was the first change to
# fall down that hole.
#
# Generated here rather than kept as a file in the repo because it must
# be able to launch a checkout newer than itself: it has to stay
# trivial and never need updating again.
sudo tee /usr/local/bin/lynx-slave >/dev/null <<'SHIM'
#!/bin/bash
# Thin launcher for the Lynx Remote Rx. Generated by slave/install.sh -
# do not add logic here: this file is outside the remote update path.
# The real wrapper is slave/lynx-slave in the checkout.
set -euo pipefail
CONF=/etc/lynx-slave.conf
if [ -r "$CONF" ]; then
    # shellcheck disable=SC1090
    . "$CONF"
fi
SLAVE_DIR="${LYNX_SLAVE_DIR:-/home/pi/lynx-datv-receiver/slave}"
WRAPPER="$SLAVE_DIR/lynx-slave"
[ -r "$WRAPPER" ] || { echo "Cannot read $WRAPPER" >&2; exit 1; }
# Through bash rather than exec'ing it directly, so a checkout whose
# execute bit did not survive still starts.
exec /bin/bash "$WRAPPER" "$@"
SHIM
sudo chmod 755 /usr/local/bin/lynx-slave
sudo install -m 644 "$here/lynx-slave.service" /etc/systemd/system/lynx-slave.service

conf=/etc/lynx-slave.conf
if [ ! -f "$conf" ]; then
    sudo install -m 644 "$here/lynx-slave.conf.example" "$conf"
    echo "Created $conf from the example: edit LYNX_HOST, NAME and LOCATOR"
else
    sudo cp "$conf" "$conf.bak-$(date +%Y%m%d%H%M%S)"
    for key in TRANSPORT SRT_LATENCY; do
        if ! grep -q "^${key}=" "$conf"; then
            line="$(grep "^${key}=" "$here/lynx-slave.conf.example")"
            echo "$line" | sudo tee -a "$conf" >/dev/null
            echo "Added to $conf: $line"
        fi
    done
fi

# Remote configuration: a key shared with the repeater, and somewhere
# the service (running as pi) can keep the settings it is sent.
sudo install -d -o pi -g pi -m 755 /var/lib/lynx-slave
if ! grep -q '^KEY=' "$conf"; then
    key="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "KEY=\"$key\"" | sudo tee -a "$conf" >/dev/null
    echo
    echo "This Remote Rx's key (enter it in Lynx: Config, Remote Rx, Key):"
    echo "    $key"
    echo
fi

# The wrapper runs the Python from this checkout. Say so in the conf if
# the checkout is somewhere other than the default.
default_dir=/home/pi/lynx-datv-receiver/slave
if [ "$here" != "$default_dir" ] && ! grep -q '^LYNX_SLAVE_DIR=' "$conf"; then
    echo "LYNX_SLAVE_DIR=\"$here\"" | sudo tee -a "$conf" >/dev/null
    echo "Added to $conf: LYNX_SLAVE_DIR=\"$here\""
fi

sudo systemctl daemon-reload
sudo systemctl enable lynx-slave >/dev/null
sudo systemctl restart lynx-slave
echo "lynx-slave restarted. Follow it with:  journalctl -u lynx-slave -f"
