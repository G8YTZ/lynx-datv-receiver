#!/usr/bin/env python3
"""
lynx_fan.py - ROCK 5T fan control: hold the SoC at a steady temperature.

Runs as a system service (lynx-fan.service, installed by
install/rock5t.sh). Every 2 s it reads the SoC temperature and sets the
fan's PWM (0-255) to hold it at SET_C:

  from ON_C            fan on at MIN_PWM, its slowest reliable speed
  below OFF_C          fan off again (5 C lower, so it does not keep
                       switching on and off at light load)
  above SET_C          faster, proportional to how far over, plus a slow
                       integral term that trims out any steady error

Changes are limited to STEP_MAX per 2 s, so the speed glides rather than
jumps and nobody hears it hunting. The kernel's own protection stays in
charge above that: its fan trip point (60 C on Armbian) and the CPU's
throttling point (85 C) are left alone, so if this service ever stops,
the board is exactly as safe as it was before it existed. The one thing
it does refuse is a kernel fan trip point below TRIP_FLOOR_C, which would
have the kernel stepping the fan to full speed under it.

Settings, in /etc/default/lynx-fan (all optional):
  LYNX_FAN_SET_C=40  LYNX_FAN_ON_C=35  LYNX_FAN_OFF_C=30
  LYNX_FAN_MIN_PWM=128

Measured on lynx-rx, 23 Oct 2026: a new fan starts turning at PWM 100,
so 128 (the kernel's own lowest step) is the slowest reliable speed.
"""

import glob
import os
import signal
import sys
import time


def env_num(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


SET_C = env_num("LYNX_FAN_SET_C", 40)
ON_C = env_num("LYNX_FAN_ON_C", 35)
OFF_C = env_num("LYNX_FAN_OFF_C", 30)
MIN_PWM = int(env_num("LYNX_FAN_MIN_PWM", 128))
MAX_PWM = 255
KP = 12.0           # PWM per degree over SET_C
KI = 0.4            # PWM per degree-second over SET_C
STEP_MAX = 8        # most the PWM moves in one 2 s step
INTERVAL = 2.0
TRIP_FLOOR_C = 55   # never let the kernel's fan trip sit below this
SYSFS = os.environ.get("LYNX_FAN_SYSFS", "/sys/class")


def log(msg):
    print(f"lynx-fan: {msg}", flush=True)


def read(path):
    with open(path) as f:
        return f.read().strip()


def write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


def find_zone():
    for z in sorted(glob.glob(f"{SYSFS}/thermal/thermal_zone*")):
        try:
            if read(f"{z}/type") == "soc-thermal":
                return z
        except OSError:
            pass
    return None


def find_fan():
    for h in sorted(glob.glob(f"{SYSFS}/hwmon/hwmon*")):
        try:
            if read(f"{h}/name") in ("pwmfan", "pwm-fan") and os.path.exists(f"{h}/pwm1"):
                return f"{h}/pwm1"
        except OSError:
            pass
    return None


def guard_kernel_trip(zone):
    """A kernel fan trip point below TRIP_FLOOR_C (for example one moved
    to 35 C by hand) would have the kernel stepping the fan to full speed
    underneath this service. Put it back to 60 C."""
    p = f"{zone}/trip_point_0_temp"
    try:
        if int(read(p)) < TRIP_FLOOR_C * 1000 and os.access(p, os.W_OK):
            write(p, 60000)
            log("kernel fan trip point was below "
                f"{TRIP_FLOOR_C} C - put back to 60 C")
    except (OSError, ValueError):
        pass


class Controller:
    """Pure control logic, kept apart from sysfs so it can be tested."""

    def __init__(self):
        self.running = False
        self.integral = 0.0
        self.pwm = 0
        self.smoothed = None

    def step(self, temp_c, dt=INTERVAL):
        # Light smoothing: the sensor reads in whole-ish degrees and
        # flickers by one, which would otherwise nudge the fan constantly.
        self.smoothed = temp_c if self.smoothed is None else \
            0.7 * self.smoothed + 0.3 * temp_c
        t = self.smoothed

        if not self.running and t >= ON_C:
            self.running = True
            self.integral = 0.0
            self.pwm = MIN_PWM          # straight to a speed that starts it
            return self.pwm, "on"
        if self.running and t < OFF_C:
            self.running = False
            self.integral = 0.0
            self.pwm = 0
            return self.pwm, "off"
        if not self.running:
            return 0, None

        err = t - SET_C
        self.integral += err * dt
        # Anti-windup: the integral only ever adds speed, and never more
        # than the full range.
        self.integral = max(0.0, min(self.integral, (MAX_PWM - MIN_PWM) / KI))
        want = MIN_PWM + KP * max(err, 0.0) + KI * self.integral
        want = int(round(max(MIN_PWM, min(MAX_PWM, want))))
        delta = max(-STEP_MAX, min(STEP_MAX, want - self.pwm))
        self.pwm += delta
        return self.pwm, None


def main():
    zone, fan = find_zone(), find_fan()
    if not zone or not fan:
        log(f"no SoC temperature ({zone}) or no fan ({fan}) - nothing to do")
        return 0
    guard_kernel_trip(zone)
    log(f"holding {SET_C:g} C: fan on at {ON_C:g} C (PWM {MIN_PWM}), "
        f"off below {OFF_C:g} C")

    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))
    ctl = Controller()
    last_report = 0.0
    while not stop:
        try:
            temp = int(read(f"{zone}/temp")) / 1000.0
            pwm, event = ctl.step(temp)
            write(fan, pwm)
            if event:
                log(f"fan {event} at {temp:.1f} C")
            now = time.monotonic()
            if now - last_report >= 600:          # a line every 10 min
                log(f"{temp:.1f} C, fan PWM {pwm}")
                last_report = now
        except OSError as e:
            log(f"{type(e).__name__}: {e}")
        time.sleep(INTERVAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
