#!/usr/bin/env python3
"""
lynx_hdhomerun.py — HDHomeRun network tuner source for the Lynx DATV Receiver.

Adds SiliconDust HDHomeRun tuners (DVB-T/T2/C models) as a Lynx source.

Why this fits Lynx's architecture
---------------------------------
The HDHomeRun can push a transport stream to a UDP address itself, so no
ffmpeg transcode is needed in the path. Lynx's mpv instance already sits
permanently on udp://@:9941, so tuning becomes:

    set /tunerN/channel <modulation>:<frequency>
    set /tunerN/program <program>
    set /tunerN/target  udp://<lynx-host>:9941

...and the picture appears. The device does the demodulation and the
demultiplexing; Lynx just plays what arrives.

Hard-won notes from bench testing (ROCK 5T / Pi 5, HDHR5-4DT fw 20260326)
------------------------------------------------------------------------
1. Lock takes SECONDS, not milliseconds. Reading status immediately after
   setting the channel always shows lock=none. Poll; don't sample once.
2. "auto:<freq>" is unreliable — it repeatedly failed to lock a mux that
   locked instantly with an explicit "t8dvbt2:<freq>". Always specify the
   modulation.
3. Arbitrary frequencies in Hz are accepted, not just the channel plan.
   The front end tunes VHF through UHF continuously.
4. pps in the status line is the cleanest confirmation the stream is
   actually flowing. Check it before blaming the player.
5. ALWAYS set target back to "none" when switching away, or the tuner
   keeps streaming into nowhere and stays busy.
6. The narrow DVB-T2 bandwidths (t1..t5) are accepted by the command
   parser but did NOT engage the demodulator on the HDHR5-4DT. Use the
   Sony CXD2880 (Lynx DVB-T2 Receiver) for narrowband amateur work.

Requires the hdhomerun_config binary:
    sudo apt install hdhomerun-config

Author: Justin, G8YTZ
Licence: same as the parent project.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

HDHOMERUN_CONFIG = "hdhomerun_config"

# Sanity limits for any frequency we will send to hardware. This mirrors the
# server-side range check added after the LNB LO bug: never let an
# out-of-range value reach the device.
MIN_FREQUENCY_HZ = 40_000_000
MAX_FREQUENCY_HZ = 1_002_000_000

# Modulations the firmware advertises. The tN prefix is the nominal channel
# bandwidth in MHz. See note 6 above before using t1..t5.
DVBT2_MODULATIONS = {
    "t8dvbt2", "t7dvbt2", "t6dvbt2", "t5dvbt2",
    "t4dvbt2", "t3dvbt2", "t2dvbt2", "t1dvbt2",
}
DVBT_MODULATIONS = {
    "t8dvbt", "t7dvbt", "t6dvbt", "t5dvbt",
    "t4dvbt", "t3dvbt", "t2dvbt", "t1dvbt",
}
# DVB-C QAM modes are wildcards in /sys/features (e.g. a8qam256-*), so they
# are matched by pattern rather than listed exhaustively.
QAM_PATTERN = re.compile(r"^a[678]qam(64|128|256)(-\w+)?$")

# Bandwidths that did not engage the demodulator in testing. Tuning is still
# permitted (the firmware accepts them), but a warning is logged so the
# behaviour is not a surprise six months from now.
UNPROVEN_MODULATIONS = {
    "t1dvbt2", "t2dvbt2", "t3dvbt2", "t4dvbt2", "t5dvbt2",
    "t1dvbt", "t2dvbt", "t3dvbt", "t4dvbt", "t5dvbt",
}

DEFAULT_LOCK_TIMEOUT = 20.0     # seconds to wait for lock
DEFAULT_POLL_INTERVAL = 1.0     # seconds between status polls
DEFAULT_COMMAND_TIMEOUT = 10.0  # seconds for one hdhomerun_config call


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class HDHomeRunError(Exception):
    """Any failure talking to, or tuning, an HDHomeRun device."""


class HDHomeRunNotInstalled(HDHomeRunError):
    """The hdhomerun_config binary is not on PATH."""


class HDHomeRunLockTimeout(HDHomeRunError):
    """The tuner accepted the channel but never reported a lock."""


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

@dataclass
class TunerStatus:
    """Parsed output of `get /tunerN/status`.

    Raw line looks like:
        ch=t8dvbt2:546000000 lock=t8dvbt2 ss=100 snq=100 seq=100
        bps=40427520 pps=3840
    """
    channel: str = ""
    lock: str = "none"
    signal_strength: int = 0     # ss  — RF power, 0-100
    signal_quality: int = 0      # snq — demod quality, 0-100 (the MER analogue)
    symbol_quality: int = 0      # seq — post-FEC, 0-100
    bits_per_second: int = 0     # bps — stream bitrate
    packets_per_second: int = 0  # pps — non-zero once target streaming runs
    raw: str = ""

    @property
    def locked(self) -> bool:
        return self.lock not in ("", "none")

    @property
    def streaming(self) -> bool:
        return self.packets_per_second > 0

    @property
    def frequency_hz(self) -> int | None:
        if ":" in self.channel:
            try:
                return int(self.channel.split(":", 1)[1])
            except ValueError:
                return None
        return None

    @property
    def modulation(self) -> str:
        return self.channel.split(":", 1)[0] if ":" in self.channel else ""

    @classmethod
    def parse(cls, line: str) -> "TunerStatus":
        fields: dict[str, str] = {}
        for token in line.strip().split():
            if "=" in token:
                key, _, value = token.partition("=")
                fields[key] = value

        def as_int(key: str) -> int:
            try:
                return int(fields.get(key, 0))
            except ValueError:
                return 0

        return cls(
            channel=fields.get("ch", ""),
            lock=fields.get("lock", "none"),
            signal_strength=as_int("ss"),
            signal_quality=as_int("snq"),
            symbol_quality=as_int("seq"),
            bits_per_second=as_int("bps"),
            packets_per_second=as_int("pps"),
            raw=line.strip(),
        )

    def to_osd(self) -> dict[str, Any]:
        """Map onto the fields the Lynx overlay expects.

        Deliberately labelled SNQ rather than MER: they are different
        measurements and should not be presented as interchangeable.
        """
        return {
            "source": "hdhomerun",
            "locked": self.locked,
            "mode": self.lock if self.locked else "",
            "frequency_hz": self.frequency_hz,
            "level": self.signal_strength,
            "quality": self.signal_quality,
            "quality_label": "SNQ",
            "errors": self.symbol_quality,
            "bitrate_bps": self.bits_per_second,
            "streaming": self.streaming,
        }


# --------------------------------------------------------------------------
# Tuner
# --------------------------------------------------------------------------

@dataclass
class HDHomeRunTuner:
    """One tuner on one HDHomeRun device.

    device_id may be the 8-hex-digit device ID (e.g. "1250048C") or an
    IP address. The device ID is preferred: it survives DHCP changes.
    """
    device_id: str
    tuner: int = 0
    binary: str = HDHOMERUN_CONFIG
    command_timeout: float = DEFAULT_COMMAND_TIMEOUT
    min_frequency_hz: int = MIN_FREQUENCY_HZ
    max_frequency_hz: int = MAX_FREQUENCY_HZ
    _target: str = field(default="none", init=False, repr=False)

    # -- low level ---------------------------------------------------------

    def _run(self, *args: str) -> str:
        if shutil.which(self.binary) is None:
            raise HDHomeRunNotInstalled(
                f"{self.binary} not found on PATH — "
                "install it with: sudo apt install hdhomerun-config"
            )
        cmd = [self.binary, self.device_id, *args]
        log.debug("hdhomerun: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise HDHomeRunError(
                f"timed out after {self.command_timeout}s: {' '.join(cmd)}"
            ) from exc

        output = (result.stdout or "").strip()
        if result.returncode != 0:
            raise HDHomeRunError(
                f"command failed ({result.returncode}): {' '.join(cmd)}: "
                f"{output or (result.stderr or '').strip()}"
            )
        # hdhomerun_config reports some failures on stdout with rc=0.
        if output.lower().startswith("error"):
            raise HDHomeRunError(f"{' '.join(cmd)}: {output}")
        return output

    def get(self, key: str) -> str:
        return self._run("get", key)

    def set(self, key: str, value: str) -> str:
        return self._run("set", key, value)

    def _path(self, leaf: str) -> str:
        return f"/tuner{self.tuner}/{leaf}"

    # -- status ------------------------------------------------------------

    def status(self) -> TunerStatus:
        return TunerStatus.parse(self.get(self._path("status")))

    def stream_info(self) -> list[dict[str, str]]:
        """Programs carried in the currently tuned multiplex."""
        programs: list[dict[str, str]] = []
        for line in self.get(self._path("streaminfo")).splitlines():
            line = line.strip()
            if not line or line.startswith("tsid"):
                continue
            number, _, rest = line.partition(":")
            if not number.strip().isdigit():
                continue
            parts = rest.strip().split(None, 1)
            programs.append({
                "program": number.strip(),
                "virtual_channel": parts[0] if parts else "",
                "name": parts[1] if len(parts) > 1 else "",
            })
        return programs

    # -- tuning ------------------------------------------------------------

    def _validate(self, modulation: str, frequency_hz: int) -> None:
        """Reject bad values before they reach hardware."""
        if not isinstance(frequency_hz, int):
            raise HDHomeRunError("frequency must be an integer number of Hz")
        if not self.min_frequency_hz <= frequency_hz <= self.max_frequency_hz:
            raise HDHomeRunError(
                f"frequency {frequency_hz} Hz out of range "
                f"({self.min_frequency_hz}-{self.max_frequency_hz} Hz)"
            )
        known = (
            modulation in DVBT2_MODULATIONS
            or modulation in DVBT_MODULATIONS
            or bool(QAM_PATTERN.match(modulation))
        )
        if modulation.startswith("auto"):
            raise HDHomeRunError(
                "refusing to use auto modulation: it is unreliable and has "
                "failed to lock muxes that lock instantly when the "
                "modulation is given explicitly. Specify e.g. t8dvbt2."
            )
        if not known:
            raise HDHomeRunError(f"unknown modulation: {modulation!r}")
        if modulation in UNPROVEN_MODULATIONS:
            log.warning(
                "modulation %s is accepted by the firmware but did not "
                "engage the demodulator in testing; a lock is unlikely",
                modulation,
            )

    def tune(
        self,
        modulation: str,
        frequency_hz: int,
        program: str | int | None = None,
        target: str | None = None,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> TunerStatus:
        """Tune, wait for lock, select a program, and start streaming.

        target is a full URL such as "udp://192.168.0.160:9941". If None,
        the tuner locks but no stream is sent.

        Raises HDHomeRunLockTimeout if no lock appears within lock_timeout.
        """
        self._validate(modulation, frequency_hz)

        # Release any previous stream first: a tuner still pushing to an old
        # target is busy, and leaves packets arriving where nobody expects.
        self.release()

        self.set(self._path("channel"), f"{modulation}:{frequency_hz}")

        status = self._wait_for_lock(lock_timeout, poll_interval)

        if program is not None:
            self.set(self._path("program"), str(program))

        if target:
            self.set(self._path("target"), target)
            self._target = target
            status = self._wait_for_packets(poll_interval)

        return status

    def _wait_for_lock(
        self, timeout: float, poll_interval: float
    ) -> TunerStatus:
        deadline = time.monotonic() + timeout
        status = self.status()
        while time.monotonic() < deadline:
            status = self.status()
            if status.locked:
                log.info(
                    "locked %s: ss=%d snq=%d seq=%d bps=%d",
                    status.lock, status.signal_strength,
                    status.signal_quality, status.symbol_quality,
                    status.bits_per_second,
                )
                return status
            time.sleep(poll_interval)

        raise HDHomeRunLockTimeout(
            f"no lock after {timeout:.0f}s on {status.channel} "
            f"(ss={status.signal_strength} snq={status.signal_quality}). "
            "Strong ss with zero snq usually means the demodulator does not "
            "support this mode, or nothing is transmitting."
        )

    def _wait_for_packets(
        self, poll_interval: float, attempts: int = 5
    ) -> TunerStatus:
        """Confirm the stream is really flowing before trusting the player."""
        status = self.status()
        for _ in range(attempts):
            status = self.status()
            if status.streaming:
                log.info("streaming: pps=%d", status.packets_per_second)
                return status
            time.sleep(poll_interval)
        log.warning(
            "target set but pps still 0 — check the destination address "
            "and that nothing is filtering UDP between here and there"
        )
        return status

    def release(self) -> None:
        """Stop streaming and free the tuner. Safe to call repeatedly."""
        try:
            self.set(self._path("target"), "none")
        except HDHomeRunError as exc:
            log.debug("release failed (ignored): %s", exc)
        self._target = "none"

    def __enter__(self) -> "HDHomeRunTuner":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

def discover_http(address: str, timeout: float = 5.0) -> dict[str, Any]:
    """Read /discover.json from a device at a known address."""
    url = f"http://{address}/discover.json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - surface as one error type
        raise HDHomeRunError(f"discovery failed at {url}: {exc}") from exc


def discover(binary: str = HDHOMERUN_CONFIG,
             timeout: float = DEFAULT_COMMAND_TIMEOUT) -> list[dict[str, str]]:
    """Find HDHomeRun devices on the local network.

    Output lines look like:
        hdhomerun device 1250048C found at 192.168.0.30
    """
    if shutil.which(binary) is None:
        raise HDHomeRunNotInstalled(
            f"{binary} not found on PATH — "
            "install it with: sudo apt install hdhomerun-config"
        )
    try:
        result = subprocess.run(
            [binary, "discover"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HDHomeRunError("discovery timed out") from exc

    devices: list[dict[str, str]] = []
    pattern = re.compile(
        r"device\s+([0-9A-Fa-f]{8})\s+found\s+at\s+(\S+)", re.IGNORECASE
    )
    for line in (result.stdout or "").splitlines():
        match = pattern.search(line)
        if match:
            devices.append({"device_id": match.group(1).upper(),
                            "address": match.group(2)})
    return devices


# --------------------------------------------------------------------------
# Lynx source wrapper
# --------------------------------------------------------------------------

@dataclass
class HDHomeRunSource:
    """Lynx-facing wrapper: one configured HDHomeRun source.

    Expected configuration in lynx_config.yaml:

        sources:
          hdhomerun:
            device_id: "1250048C"
            tuner: 0
            lynx_host: "192.168.0.160"
            udp_port: 9941
            presets:
              - name: "BBC One HD"
                modulation: "t8dvbt2"
                frequency: 546000000
                program: 17536
    """
    device_id: str
    lynx_host: str
    tuner: int = 0
    udp_port: int = 9941
    presets: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._tuner = HDHomeRunTuner(device_id=self.device_id,
                                     tuner=self.tuner)

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "HDHomeRunSource":
        try:
            return cls(
                device_id=str(config["device_id"]),
                lynx_host=str(config["lynx_host"]),
                tuner=int(config.get("tuner", 0)),
                udp_port=int(config.get("udp_port", 9941)),
                presets=list(config.get("presets", [])),
            )
        except KeyError as exc:
            raise HDHomeRunError(
                f"missing required hdhomerun config key: {exc}"
            ) from exc

    @property
    def target_url(self) -> str:
        return f"udp://{self.lynx_host}:{self.udp_port}"

    def tune(self, modulation: str, frequency_hz: int,
             program: str | int | None = None) -> dict[str, Any]:
        """Tune and start streaming to Lynx. Returns OSD-shaped status."""
        status = self._tuner.tune(
            modulation=modulation,
            frequency_hz=frequency_hz,
            program=program,
            target=self.target_url,
        )
        return status.to_osd()

    def tune_preset(self, name: str) -> dict[str, Any]:
        for preset in self.presets:
            if str(preset.get("name", "")).lower() == name.lower():
                return self.tune(
                    modulation=str(preset["modulation"]),
                    frequency_hz=int(preset["frequency"]),
                    program=preset.get("program"),
                )
        raise HDHomeRunError(f"no preset named {name!r}")

    def status(self) -> dict[str, Any]:
        return self._tuner.status().to_osd()

    def programs(self) -> list[dict[str, str]]:
        return self._tuner.stream_info()

    def stop(self) -> None:
        self._tuner.release()


# --------------------------------------------------------------------------
# Command line test harness
# --------------------------------------------------------------------------

def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Test the Lynx HDHomeRun source without running Lynx."
    )
    parser.add_argument("--device", help="device ID, e.g. 1250048C")
    parser.add_argument("--tuner", type=int, default=0)
    parser.add_argument("--modulation", default="t8dvbt2")
    parser.add_argument("--frequency", type=int,
                        help="frequency in Hz, e.g. 546000000")
    parser.add_argument("--program", help="program number to select")
    parser.add_argument("--target", help="e.g. udp://192.168.0.160:9941")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--streaminfo", action="store_true")
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    try:
        if args.discover:
            for device in discover():
                print(f"{device['device_id']}  {device['address']}")
            return 0

        if not args.device:
            parser.error("--device is required unless using --discover")

        tuner = HDHomeRunTuner(device_id=args.device, tuner=args.tuner)

        if args.release:
            tuner.release()
            print("tuner released")
            return 0

        if args.status:
            print(tuner.status().raw)
            return 0

        if args.streaminfo:
            for program in tuner.stream_info():
                print(f"{program['program']:>8}  "
                      f"{program['virtual_channel']:>6}  {program['name']}")
            return 0

        if args.frequency:
            status = tuner.tune(
                modulation=args.modulation,
                frequency_hz=args.frequency,
                program=args.program,
                target=args.target,
            )
            print(status.raw)
            print(json.dumps(status.to_osd(), indent=2))
            return 0

        parser.print_help()
        return 1

    except HDHomeRunError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(_main())
