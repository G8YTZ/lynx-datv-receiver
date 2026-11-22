#!/usr/bin/env python3
"""
patch_rock_fan.py

ROCK 5T: the fan holds the SoC at a steady 40 C.

Armbian's own fan control is a thermostat: five fixed speeds (PWM 0, 64,
128, 192, 255), stepped up once the SoC passes 60 C. Too late, and too
coarse to hold a temperature. install/lynx_fan.py replaces it with a
small control loop: fan on at 35 C at PWM 128 (the slowest reliable
speed: a new fan starts at 100), faster above 40 C, off below 30 C.
The kernel's 60 C fan step and 85 C CPU throttling stay as the safety
net. See install/lynx_fan.py.

Needs install/lynx_fan.py in the repo (copy it there first).

Run from the repo root:  python3 patch_rock_fan.py
"""

import sys
from pathlib import Path

TARGET = Path("install/rock5t.sh")
ANCHOR = "# Deliberately NOT done: forcing the HDMI connector on (video=HDMI-A-1:e).\n"

SECTION = r'''# --- Fan: hold the SoC at a steady 40 C --------------------------------
# Armbian only steps the fan up at 60 C, in five coarse speeds. lynx_fan.py
# holds 40 C instead: on at 35 C at its slowest reliable speed, faster as
# needed, off below 30 C. The kernel's 60 C fan step and 85 C CPU
# throttling stay as the safety net. Settings: /etc/default/lynx-fan.
say "Fan control (steady 40 C)"
sudo install -m 755 "$LYNX_DIR/install/lynx_fan.py" /usr/local/sbin/lynx-fan
[ -f /etc/default/lynx-fan ] || sudo tee /etc/default/lynx-fan >/dev/null <<'EOF'
# Lynx fan control - see install/lynx_fan.py
LYNX_FAN_SET_C=40
LYNX_FAN_ON_C=35
LYNX_FAN_OFF_C=30
LYNX_FAN_MIN_PWM=128
EOF
sudo tee /etc/systemd/system/lynx-fan.service >/dev/null <<'EOF'
[Unit]
Description=Lynx fan control (hold the SoC at a steady temperature)
After=systemd-modules-load.service

[Service]
EnvironmentFile=-/etc/default/lynx-fan
ExecStart=/usr/bin/python3 /usr/local/sbin/lynx-fan
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable lynx-fan.service
sudo systemctl restart lynx-fan.service

'''


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    if not Path("install/lynx_fan.py").exists():
        sys.exit("ABORT: install/lynx_fan.py missing - copy it into install/ first")
    src = TARGET.read_text()
    if "lynx-fan.service" in src:
        sys.exit("ABORT: already patched")
    if src.count(ANCHOR) != 1:
        sys.exit(f"ABORT: anchor found {src.count(ANCHOR)} times, expected 1")
    TARGET.write_text(src.replace(ANCHOR, SECTION + ANCHOR, 1))
    print("  ok  install/rock5t.sh: fan control section added")


if __name__ == "__main__":
    main()
