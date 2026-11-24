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
    # And APPLY it to what is already plugged in. A udev rule only
    # acts on a device when the device appears, so reloading alone
    # leaves a tuner that was connected before this ran with its old
    # permissions - and longmynd then fails with errno=13 on the
    # device node, which reads as "tuner not found" and sends people
    # looking for a cable fault. Saves the unplug-and-replug.
    sudo udevadm trigger --subsystem-match=usb --action=add || true
    # trigger is ASYNCHRONOUS: it queues the events and returns, so
    # anything checking the device node straight afterwards can still
    # see the old permissions and conclude the rule did not work.
    sudo udevadm settle --timeout=10 || true
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

# ── PicoTuner (USB), only if one is here ────────────────────────────
#
# The USB PicoTuner (2e8a:ba2c) is an RP2040, not an FTDI FT2232H, and
# the BATC's own longmynd has no support for it at all - confirmed by
# reading the source, and by Colin G4EML on the BATC forum, who doubted
# it was ever backported. The support lives in the longmynd vendored
# inside Ryde (ryde-build/longmynd), which is the same repository this
# script already takes CombiTunerExpress from.
#
# Built as a SECOND longmynd in ~/longmynd-pico rather than replacing
# the one in ~/longmynd, deliberately. Ryde's copy is an OLDER fork of
# the same code, and differs from upstream in ways that have nothing to
# do with the PicoTuner:
#
#   - no DVB character-set conversion for service names (upstream has
#     iconv and 223 lines of code tables; this has none of it)
#   - halfscan_ratio 1.5 -> 0.6, plus a search_algorithm default and a
#     -A option, so it hunts for a signal differently
#   - a symbol-rate offset correction copied from WinterHill
#   - serial TS out of the STV0910, selected by which USB device was
#     found - which is how one binary drives either tuner
#
# Several of those are probably improvements. None is proven under
# Lynx, and putting them beneath every working Minitiouner to benefit a
# PicoTuner card would be the wrong trade. What matters is that the
# STATUS OUTPUT FORMAT is identical between the two, which is why
# longmynd_source.py drives either one with no change beyond being
# told where the binary is.
#
# Triggered by a PicoTuner actually being plugged in, or by --picotuner
# for building a card before the tuner arrives.
pico_wanted=false
for a in "$@"; do [ "$a" = "--picotuner" ] && pico_wanted=true; done
if [ "$pico_wanted" = false ] \
   && [ "$(python3 "$here/detect_tuner.py" 2>/dev/null || true)" = "picotuner" ]; then
    pico_wanted=true
    echo "A PicoTuner is plugged in: building its longmynd as well"
fi

pico_dir=/home/pi/longmynd-pico
if [ "$pico_wanted" = true ] && [ ! -x "$pico_dir/longmynd" ]; then
    echo "Building Ryde's longmynd (PicoTuner support) in $pico_dir"
    echo "This is a second longmynd build: expect it to be slow on a Zero."
    sudo apt-get install -y git make gcc libusb-1.0-0-dev libasound2-dev
    tmp="$(mktemp -d)"
    # Sparse and blobless: longmynd is 660 kB of that repository and
    # the rest of it is of no use here.
    git clone --depth 1 --filter=blob:none --sparse \
        https://github.com/BritishAmateurTelevisionClub/ryde-build.git "$tmp/ryde"
    git -C "$tmp/ryde" sparse-checkout set longmynd
    if [ ! -f "$tmp/ryde/longmynd/ftdi.c" ]; then
        echo "ERROR: ryde-build has no longmynd/ directory any more." >&2
        echo "The PicoTuner build cannot proceed; the card will run" >&2
        echo "report-only instead. Nothing else is affected." >&2
        rm -rf "$tmp"
    else
        # Prove it is the Pico-capable one before building it, rather
        # than discovering at the first tune that it is not.
        if ! grep -q "0x2E8A" "$tmp/ryde/longmynd/ftdi.c"; then
            echo "WARNING: that longmynd has no PicoTuner USB ID in ftdi.c." >&2
            echo "Building it anyway, but it may not drive a PicoTuner." >&2
        fi
        cp -r "$tmp/ryde/longmynd" "$pico_dir"
        rm -rf "$tmp"
        make -C "$pico_dir"
    fi
fi
# Ryde's own udev rule names all three product strings - the
# Minitiouner, the MiniTiouner-Express variant and the PicoTuner - so it
# REPLACES the Minitiouner-only rule rather than sitting beside it.
# Checked by content, not by existence, because the file is already
# there from the Minitiouner install above.
if [ "$pico_wanted" = true ] && [ -f "$pico_dir/minitiouner.rules" ] \
   && ! grep -q "BATC PicoTuner" /etc/udev/rules.d/minitiouner.rules 2>/dev/null; then
    sudo cp "$pico_dir/minitiouner.rules" /etc/udev/rules.d/minitiouner.rules
    sudo udevadm control --reload-rules
    # And APPLY it to what is already plugged in. A udev rule only
    # acts on a device when the device appears, so reloading alone
    # leaves a tuner that was connected before this ran with its old
    # permissions - and longmynd then fails with errno=13 on the
    # device node, which reads as "tuner not found" and sends people
    # looking for a cable fault. Saves the unplug-and-replug.
    sudo udevadm trigger --subsystem-match=usb --action=add || true
    # trigger is ASYNCHRONOUS: it queues the events and returns, so
    # anything checking the device node straight afterwards can still
    # see the old permissions and conclude the rule did not work.
    sudo udevadm settle --timeout=10 || true
    echo "Installed the udev rule covering the PicoTuner: unplug and replug it, or reboot"
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
