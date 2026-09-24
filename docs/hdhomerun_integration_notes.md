# Adding the HDHomeRun as a fourth tuner

Integration guide for `lynx_hdhomerun.py` into `lynx_app.py` (beta-wip).

Revised: devices are **discovered**, not configured. Config carries
presets only.

Written against the code as it stands: `tune_lock` / `_tune_lock_handed_off`
handoff, `restart_mpv()` rather than IPC reload, `current_mode` of
`idle | rf | stream`, and the Slave pattern of one state record per source.

**No HTML in this patch.** Python only — a route, a model, two threads and
one status dict entry. The receiver page's own markup is deliberately left
alone; that is a separate, smaller change to be made on its own.

---

## Design decisions taken

| Question | Decision | Why |
|---|---|---|
| Configure devices? | No — discover them | The device announces itself on the LAN and `--discover` already finds it. Anything that announces itself should not need declaring. |
| Configure presets? | Yes | Which frequencies and services matter is not discoverable. `lineup.json` gives every broadcast channel, not the few worth a button. |
| New tune route? | Yes, `POST /api/tune_dvbt` | `TuneRequest` is entirely DVB-S2: `sr`, `plug`, `lnb_lo_khz`, `rcv`. None apply, and `_tune_impl()` is Picotuner-specific throughout. |
| Video port | Its own, `HDHR_VIDEO_PORT = 10996` | 9941 is already contended between the RF path, ffmpeg and the diversity combiner, and has caused one "Address already in use" crash. Follows `REMOTE_VIDEO_OUT_PORT`'s precedent. |
| Status fields | DVB-T/T2 terms of its own | SNQ is not MER. Filling `mer` with an SNQ percentage would make two different measurements look comparable on one overlay. |
| `current_mode` | New value, `"dvbt"` | Behaves correctly at the sites checked — see the audit note before merging. |
| mpv | `restart_mpv(url, is_rf=True)` | The device sends raw MPEG-TS over UDP, exactly what those flags were written for. IPC reload was abandoned for reasons documented at `restart_mpv()`. |

---

## 1. Config — presets only

Add to `lynx_config.yaml` and the example file. No device IDs, no
addresses: those are found.

```yaml
# Presets for any HDHomeRun found on the network. The devices
# themselves are discovered — nothing to configure for a new one.
hdhomerun_presets:
  - name: "BBC One HD"
    modulation: "t8dvbt2"
    frequency: 546000000      # Hz
    program: 17536
  - name: "BBC A"
    modulation: "t8dvbt"
    frequency: 490000000
    program: 4164
```

Frequency is in **Hz**, matching the device's own API. Converting at the
edge is how unit confusion starts — see what `calc_tuner_freq()` exists to
prevent. The UI can show MHz.

Optional, only if a site ever has two devices and one must be preferred:

```yaml
hdhomerun:
  prefer_device_id: "1250048C"   # omit entirely unless needed
```

---

## 2. Module-level additions

Place near the Slave block (around line 3460), so the two
network-attached tuners sit together.

```python
import lynx_hdhomerun

# Its own port, not 9941. That port is already shared between the RF
# direct-play path, stop_ffmpeg_bg()'s transcode and the diversity
# combiner, and has already produced one "Address already in use"
# crash when two of them overlapped. A third writer would be inviting
# the same failure, and a tuner that can stay locked while another
# source is on screen is worth having anyway.
HDHR_VIDEO_PORT = 10996

# How often each known device is asked for status. It does not
# broadcast, so this is a request rather than a packet arriving:
# often enough for the overlay to feel live, not so often that a busy
# device is being interrogated for no reason.
HDHR_POLL_INTERVAL_SECS = 2.0

# How often the network is swept for devices. Much slower than the
# status poll: a tuner appearing is a rare event, and discovery is a
# broadcast that every device on the LAN has to answer.
HDHR_DISCOVER_INTERVAL_SECS = 60.0

# How long without a successful status read before a device counts as
# gone. Several polls, so one timeout on a busy network does not make
# a tuner appear to come and go.
HDHR_OFFLINE_AFTER_SECS = 15.0

# Filled only once a device has actually answered. Deliberately NOT the
# same names as REMOTE_QUALITY_FIELDS: these are DVB-T/T2 measurements,
# and presenting SNQ as MER would make two different things look
# comparable on one overlay.
HDHR_QUALITY_FIELDS = (
    "lock_mode",        # e.g. "t8dvbt2"; "" when unlocked
    "signal_strength",  # ss,  0-100
    "signal_quality",   # snq, 0-100
    "symbol_quality",   # seq, 0-100
    "bitrate_bps",      # bps
    "frequency_hz",
)

# Discovered devices, keyed by device ID. Unlike remote_states, this is
# NOT built at startup from config and NOT a list indexed in step with
# anything: there is no configured set to be absent from, so a device
# that has never been seen simply is not here. Keyed rather than
# indexed because a device's identity is its ID, and discovery may
# return them in any order.
hdhr_states: dict = {}
hdhr_lock = threading.Lock()   # guards hdhr_states across the two threads


def hdhr_presets_cfg() -> list:
    """The configured presets, as a list."""
    presets = config.get('hdhomerun_presets')
    return presets if isinstance(presets, list) else []


def hdhr_devices() -> list:
    """Known devices, newest state, as a list of records.

    Sorted by device ID so the order on screen is stable between polls
    — discovery returns them in whatever order they answer, which is
    not an order anybody wants to watch a list re-sort itself into.
    """
    with hdhr_lock:
        return [dict(st) for _, st in sorted(hdhr_states.items())]


def hdhr_default_device_id() -> str | None:
    """Which device a tune goes to when none is named.

    A configured preference wins if the device is actually present;
    otherwise the first online device by ID. Returns None when nothing
    has been found, which the caller reports as 404 rather than
    guessing.
    """
    preferred = (config.get('hdhomerun') or {}).get('prefer_device_id')
    with hdhr_lock:
        if preferred and preferred in hdhr_states:
            return preferred
        for device_id, st in sorted(hdhr_states.items()):
            if st.get("online"):
                return device_id
    return None


def hdhr_source(device_id: str | None = None) -> lynx_hdhomerun.HDHomeRunSource:
    """A source object for the named device, or the default one."""
    device_id = device_id or hdhr_default_device_id()
    if not device_id:
        raise HTTPException(
            status_code=404,
            detail="No HDHomeRun found on the network")
    with hdhr_lock:
        st = hdhr_states.get(device_id)
    if not st:
        raise HTTPException(
            status_code=404,
            detail=f"HDHomeRun {device_id} not found on the network")
    return lynx_hdhomerun.HDHomeRunSource(
        device_id=device_id,
        lynx_host="127.0.0.1",     # the device streams here; mpv reads locally
        tuner=st.get("tuner", 0),
        udp_port=HDHR_VIDEO_PORT,
    )
```

---

## 3. Discovery thread

```python
def hdhomerun_discovery():
    """Background thread: sweeps the network for HDHomeRun devices.

    Devices are not configured, so this is how they come to exist at
    all. A device that disappears is left in hdhr_states rather than
    removed: the status poller will mark it offline, and "was here,
    now unreachable" is more useful to somebody staring at a receiver
    page than a tuner that silently vanishes.

    Note discovery returns one row per address — a device reachable
    over IPv4 and IPv6 answers three times (global v6, link-local v6,
    v4). Keyed by device ID, so they collapse to one record, and the
    IPv4 address is preferred because that is what the rest of the
    site is addressed by.
    """
    while True:
        try:
            found = lynx_hdhomerun.discover()
        except lynx_hdhomerun.HDHomeRunError as e:
            # An empty network, or hdhomerun_config not installed.
            # Neither is an error worth logging every minute.
            found = []
            print(f"[hdhr] discovery: {e}")

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
                # Prefer the IPv4 address: v6 works, but every other
                # address on this site is v4 and a mixed listing is
                # confusing to read.
                if ":" not in address or ":" not in st.get("address", ""):
                    if ":" not in address:
                        st["address"] = address

        time.sleep(HDHR_DISCOVER_INTERVAL_SECS)
```

---

## 4. Status poller

```python
def hdhomerun_monitor():
    """Background thread: polls each known HDHomeRun for status.

    Polls rather than listens, because unlike the Picotuner and the
    Slaves, an HDHomeRun does not broadcast — it answers when asked.
    That difference is the whole reason this is a separate monitor
    rather than another branch inside an existing one.

    Runs regardless of current_mode, deliberately: knowing whether a
    tuner is locked before selecting it is more useful than finding
    out afterwards, and the cost is one small request every couple of
    seconds.
    """
    while True:
        for st in hdhr_devices():
            device_id = st["device_id"]
            try:
                tuner = lynx_hdhomerun.HDHomeRunTuner(
                    device_id=device_id, tuner=st.get("tuner", 0)
                )
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
                # An unreachable device is ordinary — unplugged, or a
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
```

Start both where the other monitor threads are started:

```python
threading.Thread(target=hdhomerun_discovery, daemon=True).start()
threading.Thread(target=hdhomerun_monitor, daemon=True).start()
```

Discovery first, so the poller has something to poll on its first pass.

---

## 5. Request model

Beside `TuneRequest` (around line 4014).

```python
class DvbtTuneRequest(BaseModel):
    freq: int                 # Hz — the actual frequency. No LNB and no
                              # transverter arithmetic: this tuner is on
                              # an aerial, not behind a down-converter.
    modulation: str = "t8dvbt2"
                              # tNdvbt2 / tNdvbt / aNqamNNN, where N is the
                              # nominal channel bandwidth in MHz. Explicit
                              # rather than "auto": auto was confirmed to
                              # fail repeatedly on a mux that locks instantly
                              # when the modulation is given.
    program: str | None = None
                              # Program number within the multiplex. None
                              # locks the mux without selecting a service,
                              # which is useful for checking a signal.
    device_id: str | None = None
                              # Which HDHomeRun. None uses the default —
                              # the configured preference if present,
                              # otherwise the first one found.
```

---

## 6. The tune route

Same lock discipline as `tune()`, and for the same reason: a failure
before the async thread starts must not leave the lock held.

```python
@app.post("/api/tune_dvbt", tags=["RF Reception"],
          summary="Tune an HDHomeRun to a DVB-T/T2/C frequency",
          description="Tunes a network-attached HDHomeRun tuner and starts "
                      "mpv playing its stream. Stops any current reception "
                      "first.")
def tune_dvbt(req: DvbtTuneRequest):
    global _tune_lock_handed_off
    if not tune_lock.acquire(timeout=15):
        raise HTTPException(
            status_code=503,
            detail="Another tune operation is already in progress — "
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
    # longer being listened to — same reason and same placement as in
    # _tune_impl(), before anything is tuned, so the watcher's next
    # poll already knows rather than racing.
    try:
        pathfinder_source_changed()
    except Exception:
        pass

    # Tear down whatever owns the video path now. The HDHomeRun has its
    # own port, so this is not about port contention — it is so that a
    # Picotuner still pushing to 9941, or a combiner still running, is
    # not left burning CPU and confusing the lifecycle monitor while a
    # different source is on screen.
    stop_ffmpeg_bg()
    if diversity_enabled:
        stop_diversity_combiner()
        diversity_enabled = False

    # Tune and wait for lock. Unlike the RF path — which defers mpv
    # until rf_mpv_lifecycle_monitor() confirms a stable lock — the
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
    # written for. The RTMP-specific concerns in restart_mpv()'s
    # docstring do not apply.
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
```

---

## 7. Teardown

The device keeps streaming until told otherwise, so every path that
leaves this source must stop it.

```python
def hdhr_stop_all():
    """Stop every known device streaming.

    Called when Lynx moves away from this source. Without it a tuner
    stays busy and keeps pushing packets at a port nobody is reading —
    not harmful, but exactly the kind of thing that stays invisible
    until the day it matters.
    """
    for st in hdhr_devices():
        if not st.get("online"):
            continue
        try:
            hdhr_source(st["device_id"]).stop()
        except Exception as e:
            print(f"[hdhr] stop failed: {type(e).__name__}: {e}")
```

Call it from `_tune_impl()` near the existing teardown, from the stream
tune path, and from whatever handles going idle.

---

## 8. Status endpoint

Add beside `"mode"` in the existing status dict (around line 6299):

```python
"hdhomerun": [
    {
        "device_id": st["device_id"],
        "name": st["name"],
        "address": st.get("address"),
        "online": st["online"],
        "locked": st["locked"],
        "streaming": st["streaming"],
        "lock_mode": st["lock_mode"],
        "level": st["signal_strength"],
        "quality": st["signal_quality"],
        "errors": st["symbol_quality"],
        "bitrate_bps": st["bitrate_bps"],
        "frequency_hz": st["frequency_hz"],
    }
    for st in hdhr_devices()
],
"hdhomerun_presets": hdhr_presets_cfg(),
```

The overlay labels `quality` as **SNQ** and `errors` as **SEQ**. Different
measurements from MER and margin, so different names.

---

## Three edits to existing code

1. **Startup** — start `hdhomerun_discovery()` then `hdhomerun_monitor()`
   with the other monitor threads. Nothing to initialise from config.
2. **Teardown** — call `hdhr_stop_all()` from the RF and stream tune paths
   and from the idle path.
3. **Resume** — `save_last_state({"mode": "dvbt", ...})` needs a matching
   branch in the resume logic, or a receiver that reboots on this source
   comes back idle. Note the device may not have been discovered yet at
   that point, so the resume should wait for it in the same bounded way
   `REMOTE_RESUME_WAIT_SECS` waits for a Slave.

---

## Before merging: the `current_mode` audit

`current_mode` is checked in at least 30 places. `"dvbt"` behaves
correctly at the sites inspected:

- `if current_mode != "rf"` — RF monitors stay out of the way. Correct.
- `current_mode == "stream"` — stream info stays absent. Correct.
- `picture_ready=(current_mode != "rf" or mpv_running_for_rf)` at ~6357 —
  evaluates True, so the cover comes down. Correct.

That is not a substitute for checking the rest:

```bash
grep -n "current_mode" lynx_app.py
```

Look for anywhere treating "not rf" as "must be stream", or "not stream"
as "must be rf". Those are the two shapes that break when a third value
appears, and neither announces itself.

Then, as always:

```bash
python3 verify.py
```

---

## Still to do, separately

The receiver page needs the new tuner added to its source list and the
overlay needs to render the DVB-T/T2 fields. Both are HTML and JavaScript
changes inside `lynx_app.py`, and both are deliberately **not** in this
patch — that is where breakage has happened before, and it deserves its
own small, reviewed change once the Python side is proved working through
the API.

Test it through the API first:

```bash
curl -X POST http://localhost:8080/api/tune_dvbt \
  -H 'Content-Type: application/json' \
  -d '{"freq":546000000,"modulation":"t8dvbt2","program":"17536"}'
```
