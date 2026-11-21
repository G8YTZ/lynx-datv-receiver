#!/bin/bash
# Installs the Lynx Slave Rx from this checkout.
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

sudo install -m 755 "$here/lynx-slave" /usr/local/bin/lynx-slave
sudo install -m 644 "$here/lynx-slave.service" /etc/systemd/system/lynx-slave.service

conf=/etc/lynx-slave.conf
if [ ! -f "$conf" ]; then
    sudo install -m 644 "$here/lynx-slave.conf.example" "$conf"
    echo "Created $conf from the example: edit LYNX_HOST, FREQ, SR and NAME"
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
