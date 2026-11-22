#!/usr/bin/env python3
"""
patch_hdhomerun_source.py — add the HDHomeRun as a fourth tuner.

Adds DVB-T/T2/C reception via a network-attached SiliconDust HDHomeRun,
using lynx_hdhomerun.py. Python only: no HTML or JavaScript is touched.

What it inserts
---------------
1. import of lynx_hdhomerun, beside the Slave block
2. module-level constants, discovery, state and helpers
3. hdhomerun_monitor() status poller and hdhr_stop_all()
4. DvbtTuneRequest model, beside TuneRequest
5. POST /api/tune_dvbt route, beside the other tune routes
6. GET /api/hdhomerun status route
7. startup: one discovery sweep, then a poller thread only if
   something was found

Every edit is anchored on text confirmed present, and each refuses to
apply if its anchor is missing or appears more than once. Nothing is
written unless ALL required edits match, so a partial application is
not possible.

Usage
-----
    python3 patch_hdhomerun_source.py            # dry run, reports only
    python3 patch_hdhomerun_source.py --apply    # take a backup and write

After applying:
    python3 verify.py

Safe to re-run: if the marker is already present it reports and stops.
"""

from __future__ import annotations

import argparse
import py_compile
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

TARGET = Path("lynx_app.py")
MARKER = "HDHR_VIDEO_PORT"        # presence means already applied


# --------------------------------------------------------------------------
# 1. import — placed beside the Slave block rather than in the header
# --------------------------------------------------------------------------

ANCHOR_IMPORT = "remote_states: list = []"

INSERT_IMPORT = '''# --- HDHomeRun (DVB-T/T2/C) ------------------------------------------
# Imported here rather than in the header block at the top: this is an
# optional source whose module sits beside the Slave code it most
# resembles, and keeping the two together means somebody reading one
# finds the other.
import lynx_hdhomerun

# Its own port, not 9941. That port is already shared between the RF
# direct-play path, stop_ffmpeg_bg()'s transcode and the diversity
# combiner, and has already produced one "Address already in use"
# crash when two of them overlapped. A third writer would be asking
# for the same failure, and a tuner that can stay locked while another
# source is on screen is worth having anyway.
HDHR_VIDEO_PORT = 10996

# How often each known device is asked for status. It does not
# broadcast, so this is a request rather than a packet arriving:
# often enough for the overlay to feel live, not so often that a busy
# device is being interrogated for no reason.
HDHR_POLL_INTERVAL_SECS = 2.0

# How long without a successful status read before a device counts as
# gone. Several polls, so one timeout on a busy network does not make
# a tuner appear to come and go.
HDHR_OFFLINE_AFTER_SECS = 15.0

# Deliberately NOT the same names as REMOTE_QUALITY_FIELDS. Those are
# DVB-S2 measurements; these are DVB-T/T2 ones. Presenting SNQ as MER
# would make two different things look comparable on one overlay, and
# somebody would eventually compare them.
HDHR_QUALITY_FIELDS = (
    "lock_mode",        # e.g. "t8dvbt2"; "" when unlocked
    "signal_strength",  # ss,  0-100
    "signal_quality",   # snq, 0-100
    "symbol_quality",   # seq, 0-100
    "bitrate_bps",      # bps
    "frequency_hz",
)

# Discovered devices, keyed by device ID. Unlike remote_states this is
# NOT built from config and NOT a list indexed in step with anything:
# there is no configured set for a device to be absent from, so one
# that has never been seen simply is not here. Keyed rather than
# indexed because a device's identity is its ID, and discovery returns
# them in whatever order they answer.
hdhr_states: dict = {}
hdhr_lock = threading.Lock()


def hdhr_presets_cfg() -> list:
    """The configured presets, as a list.

    Presets are configured because which frequencies and services
    matter is not discoverable. The devices themselves are not: they
    announce themselves on the LAN, and anything that announces itself
    should not also have to be declared.
    """
    presets = config.get('hdhomerun_presets')
    return presets if isinstance(presets, list) else []


def hdhr_devices() -> list:
    """Known devices, newest state, as a list of records.

    Sorted by device ID so the order on screen is stable: discovery
    returns them in whatever order they answer, which is not an order
    anybody wants to watch a list re-sort itself into.
    """
    with hdhr_lock:
        return [dict(st) for _, st in sorted(hdhr_states.items())]


def hdhr_discover_once() -> int:
    """Sweep the network for HDHomeRun devices. Returns how many.

    Called once at startup rather than from a background thread: an
    HDHomeRun is installed once, not plugged in and out, and a
    receiver with none should run no thread and send no broadcast -
    absent hardware means absent behaviour, the same principle the
    Slave threads follow for absent configuration. A device added
    later is picked up on the next restart.

    Discovery returns one row per ADDRESS, so a device reachable over
    IPv4 and IPv6 answers three times (global v6, link-local v6, v4).
    Keyed by device ID so those collapse to one record, and the IPv4
    address is preferred because that is what the rest of the site is
    addressed by.
    """
    try:
        found = lynx_hdhomerun.discover()
    except lynx_hdhomerun.HDHomeRunError as e:
        # No devices, or hdhomerun_config not installed. Neither is an
        # error: this source is optional.
        print(f"[hdhr] discovery: {e}")
        return 0

    with hdhr_lock:
        for device in found:
            device_id = device["device_id"]
            address = device["address"]
            st = hdhr_states.setdefault(device_id, {
                "device_id": device_id,
                "name": f"HDHomeRun {device_id}",
                "tuner": 0,
                "address": address,
                "online": False,
                "locked": False,
                "streaming": False,
                "last_seen": 0.0,
                "last_error": "",
                **{field: None for field in HDHR_QUALITY_FIELDS},
            })
            if ":" not in address:
                st["address"] = address        # prefer IPv4
        count = len(hdhr_states)

    if count:
        for st in hdhr_devices():
            print(f"[hdhr] found {st['device_id']} at {st['address']}")
    return count


def hdhr_default_device_id():
    """Which device a tune goes to when none is named.

    A configured preference wins if that device is actually present;
    otherwise the first one by ID. Returns None when nothing has been
    found, which the caller reports rather than guessing.
    """
    preferred = (config.get('hdhomerun') or {}).get('prefer_device_id')
    with hdhr_lock:
        if preferred and preferred in hdhr_states:
            return preferred
        for device_id in sorted(hdhr_states):
            return device_id
    return None


def hdhr_source(device_id=None):
    """A source object for the named device, or the default one."""
    device_id = device_id or hdhr_default_device_id()
    if not device_id:
        raise HTTPException(status_code=404,
                            detail="No HDHomeRun found on the network")
    with hdhr_lock:
        st = hdhr_states.get(device_id)
    if not st:
        raise HTTPException(
            status_code=404,
            detail=f"HDHomeRun {device_id} not found on the network")
    return lynx_hdhomerun.HDHomeRunSource(
        device_id=device_id,
        lynx_host="127.0.0.1",     # device streams here; mpv reads locally
        tuner=st.get("tuner", 0),
        udp_port=HDHR_VIDEO_PORT,
    )


def hdhomerun_monitor():
    """Background thread: polls each known HDHomeRun for status.

    Polls rather than listens, because unlike the Picotuner and the
    Slaves an HDHomeRun does not broadcast - it answers when asked.
    That difference is the whole reason this is a separate monitor
    rather than another branch inside an existing one.

    Runs regardless of current_mode, deliberately: knowing whether a
    tuner is locked BEFORE selecting it is more useful than finding
    out afterwards, and the cost is one small request every couple of
    seconds.
    """
    while True:
        for st in hdhr_devices():
            device_id = st["device_id"]
            try:
                tuner = lynx_hdhomerun.HDHomeRunTuner(
                    device_id=device_id, tuner=st.get("tuner", 0))
                status = tuner.status()
                with hdhr_lock:
                    live = hdhr_states.get(device_id)
                    if live is None:
                        continue
                    live.update({
                        "online": True,
                        "last_error": "",
                        "last_seen": time.time(),
                        "locked": status.locked,
                        "streaming": status.streaming,
                        "lock_mode": status.lock if status.locked else "",
                        "signal_strength": status.signal_strength,
                        "signal_quality": status.signal_quality,
                        "symbol_quality": status.symbol_quality,
                        "bitrate_bps": status.bits_per_second,
                        "frequency_hz": status.frequency_hz,
                    })
            except lynx_hdhomerun.HDHomeRunError as e:
                # An unreachable device is ordinary - unplugged, or a
                # site link down. Recorded, not logged every two seconds.
                with hdhr_lock:
                    live = hdhr_states.get(device_id)
                    if live is not None:
                        if (time.time() - live.get("last_seen", 0)
                                > HDHR_OFFLINE_AFTER_SECS):
                            live["online"] = False
                            live["locked"] = False
                            live["streaming"] = False
                        live["last_error"] = str(e)
        time.sleep(HDHR_POLL_INTERVAL_SECS)


def hdhr_stop_all():
    """Stop every known device streaming.

    The device keeps sending until told otherwise, so every path that
    leaves this source must call this. Without it a tuner stays busy
    and keeps pushing packets at a port nobody is reading - not
    harmful, but exactly the kind of thing that stays invisible until
    the day it matters.
    """
    for st in hdhr_devices():
        try:
            hdhr_source(st["device_id"]).stop()
        except Exception as e:
            print(f"[hdhr] stop failed: {type(e).__name__}: {e}")
# --- end HDHomeRun ---------------------------------------------------

'''


# --------------------------------------------------------------------------
# 2. request model
# --------------------------------------------------------------------------

ANCHOR_MODEL = "class StreamRequest(BaseModel):"

INSERT_MODEL = '''class DvbtTuneRequest(BaseModel):
    freq: int                 # Hz - the actual frequency. No LNB and no
                              # transverter arithmetic: this tuner is on
                              # an aerial, not behind a down-converter.
                              # Hz rather than kHz because that is what
                              # the device's own API takes, and a unit
                              # converted at the edge is a unit somebody
                              # eventually converts twice.
    modulation: str = "t8dvbt2"
                              # tNdvbt2 / tNdvbt / aNqamNNN, where N is the
                              # nominal channel bandwidth in MHz. Explicit
                              # rather than "auto": auto was confirmed to
                              # fail repeatedly on a mux that locks within
                              # seconds when the modulation is given.
    program: Optional[str] = None
                              # Program number within the multiplex. None
                              # locks the mux without selecting a service,
                              # which is useful for checking a signal.
    device_id: Optional[str] = None
                              # Which HDHomeRun. None uses the default -
                              # the configured preference if present,
                              # otherwise the first one found.

'''


# --------------------------------------------------------------------------
# 3. tune route and status route
# --------------------------------------------------------------------------

ANCHOR_ROUTE = '@app.get("/api/tune", tags=["RF Reception"],'

INSERT_ROUTE = '''@app.post("/api/tune_dvbt", tags=["RF Reception"],
          summary="Tune an HDHomeRun to a DVB-T/T2/C frequency",
          description="Tunes a network-attached HDHomeRun tuner and starts "
                      "mpv playing its stream. Stops any current reception "
                      "first.")
def tune_dvbt(req: DvbtTuneRequest):
    # Same thin-wrapper split as tune(), and for the same reason: a
    # failure inside the implementation before the async thread starts
    # must not leave tune_lock held, or every future tune returns 503
    # while streaming carries on working and nothing says why.
    global _tune_lock_handed_off
    if not tune_lock.acquire(timeout=15):
        raise HTTPException(
            status_code=503,
            detail="Another tune operation is already in progress - "
                   "please try again shortly")
    _tune_lock_handed_off = False
    try:
        return _tune_dvbt_impl(req)
    except Exception:
        if not _tune_lock_handed_off:
            tune_lock.release()
        raise


def _tune_dvbt_impl(req: DvbtTuneRequest):
    global current_mode, current_preset, displayed_receiver_id
    global diversity_enabled, mpv_running_for_rf, _tune_lock_handed_off

    source = hdhr_source(req.device_id)

    # Lynx is changing source, so whatever was being received is no
    # longer being listened to - same reason and same placement as in
    # _tune_impl(), before anything is tuned, so the watcher's next
    # poll already knows rather than racing.
    try:
        pathfinder_source_changed()
    except Exception:
        pass

    # Tear down whatever owns the video path now. The HDHomeRun has its
    # own port, so this is not about port contention - it is so that a
    # Picotuner still pushing to 9941, or a combiner still running, is
    # not left burning CPU and confusing the lifecycle monitor while a
    # different source is on screen.
    stop_ffmpeg_bg()
    if diversity_enabled:
        stop_diversity_combiner()
        diversity_enabled = False

    # Tune and wait for lock. Unlike the RF path - which defers mpv
    # until rf_mpv_lifecycle_monitor() confirms a stable lock - the
    # device reports its own lock and its own packet rate, so there is
    # nothing to infer and no reason to wait twice.
    try:
        source.tune(
            modulation=req.modulation,
            frequency_hz=req.freq,
            program=req.program,
        )
    except lynx_hdhomerun.HDHomeRunLockTimeout as e:
        raise HTTPException(status_code=504, detail=str(e))
    except lynx_hdhomerun.HDHomeRunError as e:
        raise HTTPException(status_code=502, detail=str(e))

    current_mode = "dvbt"
    displayed_receiver_id = None
    current_preset = f"{req.freq / 1e6:.3f} MHz / {req.modulation}"

    # is_rf=True is correct here and is not a fudge: the device sends
    # raw MPEG-TS over UDP, which is exactly what that flag set was
    # written for. The RTMP-specific concerns in restart_mpv()'s own
    # docstring do not apply to it.
    def _kick_mpv():
        global mpv_running_for_rf
        try:
            time.sleep(1)
            restart_mpv(f"udp://@:{HDHR_VIDEO_PORT}", is_rf=True)
            mpv_running_for_rf = True
        finally:
            tune_lock.release()

    threading.Thread(target=_kick_mpv, daemon=True).start()
    _tune_lock_handed_off = True

    save_last_state({
        "mode": "dvbt",
        "freq": req.freq,
        "modulation": req.modulation,
        "program": req.program,
        "device_id": req.device_id,
    })

    return {
        "success": True,
        "mode": "dvbt",
        "freq_hz": req.freq,
        "modulation": req.modulation,
        "program": req.program,
        "device_id": req.device_id or hdhr_default_device_id(),
    }


@app.get("/api/hdhomerun", tags=["RF Reception"],
         summary="HDHomeRun tuners found on the network",
         description="Devices discovered at startup, their current status, "
                     "and the configured presets.")
def hdhomerun_status():
    return {
        "devices": [
            {
                "device_id": st["device_id"],
                "name": st["name"],
                "address": st.get("address"),
                "online": st["online"],
                "locked": st["locked"],
                "streaming": st["streaming"],
                "lock_mode": st["lock_mode"],
                "level": st["signal_strength"],      # ss
                "quality": st["signal_quality"],     # snq - NOT MER
                "errors": st["symbol_quality"],      # seq
                "bitrate_bps": st["bitrate_bps"],
                "frequency_hz": st["frequency_hz"],
            }
            for st in hdhr_devices()
        ],
        "presets": hdhr_presets_cfg(),
    }


@app.post("/api/hdhomerun/stop", tags=["RF Reception"],
          summary="Stop all HDHomeRun streaming",
          description="Releases every tuner. Useful when a device has been "
                      "left streaming by an interrupted tune.")
def hdhomerun_stop():
    hdhr_stop_all()
    return {"success": True}


'''


# --------------------------------------------------------------------------
# 4. startup
# --------------------------------------------------------------------------

ANCHOR_STARTUP = """    init_remote_states()
    for _i, _st in enumerate(remote_states):
        if not _st["enabled"]:
            continue
        threading.Thread(target=remote_source_monitor, args=(_i,),
                         daemon=True).start()"""

INSERT_STARTUP = """
    # One sweep rather than a background discovery thread: an HDHomeRun
    # is installed once, not plugged in and out, and a receiver with
    # none should run no thread and send no broadcast - absent hardware
    # means absent behaviour, the same principle the Slave threads
    # follow for absent configuration. A device added later is picked
    # up on the next restart.
    if hdhr_discover_once():
        hdhr_monitor = threading.Thread(target=hdhomerun_monitor,
                                        daemon=True)
        hdhr_monitor.start()"""


# --------------------------------------------------------------------------
# Machinery
# --------------------------------------------------------------------------

EDITS = [
    ("import and module block", ANCHOR_IMPORT, INSERT_IMPORT, "before"),
    ("DvbtTuneRequest model", ANCHOR_MODEL, INSERT_MODEL, "before"),
    ("tune and status routes", ANCHOR_ROUTE, INSERT_ROUTE, "before"),
    ("startup discovery", ANCHOR_STARTUP, INSERT_STARTUP, "after"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    parser.add_argument("--file", default=str(TARGET))
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"ERROR: {path} not found. Run this from the lynx directory.")
        return 2

    text = path.read_text(encoding="utf-8")

    if MARKER in text:
        print(f"{path} already contains {MARKER} - patch already applied.")
        return 0

    print(f"\nchecking anchors in {path}\n")
    ok = True
    for name, anchor, _insert, _where in EDITS:
        count = text.count(anchor)
        if count == 1:
            print(f"  {name:<26} OK   one match")
        else:
            print(f"  {name:<26} FAIL {count} matches - expected exactly 1")
            ok = False

    if not ok:
        print("\nNo changes made. An anchor is missing or ambiguous, which "
              "means this patch was written against a different version of "
              "the file. Nothing is safe to apply.")
        return 1

    patched = text
    for _name, anchor, insert, where in EDITS:
        if where == "before":
            patched = patched.replace(anchor, insert + anchor, 1)
        else:
            patched = patched.replace(anchor, anchor + insert, 1)

    # Compile the RESULT before touching the real file, so a syntax
    # error in this patch never reaches lynx_app.py at all.
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as tmp:
        tmp.write(patched)
        tmp_path = Path(tmp.name)
    try:
        py_compile.compile(str(tmp_path), doraise=True)
        print("\n  syntax check              OK   result parses")
    except py_compile.PyCompileError as e:
        print(f"\n  syntax check              FAIL {e}")
        print("\nNo changes made.")
        return 1
    finally:
        tmp_path.unlink(missing_ok=True)

    added = len(patched.splitlines()) - len(text.splitlines())
    print(f"\n  {added} lines would be added")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the changes.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak-hdhr-{stamp}")
    shutil.copy2(path, backup)
    path.write_text(patched, encoding="utf-8")

    print(f"\n  backup written to {backup.name}")
    print(f"  {path} patched")
    print("\nNow run:  python3 verify.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
