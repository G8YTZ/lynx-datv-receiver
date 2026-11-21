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

sudo install -m 755 "$here/lynx-slave" /usr/local/bin/lynx-slave
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
