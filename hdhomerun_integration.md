# Adding the HDHomeRun as a fourth tuner

Integration guide for `lynx_hdhomerun.py` into `lynx_app.py` (beta-wip).

Written against the code as it stands: `tune_lock` / `_tune_lock_handed_off`
handoff, `restart_mpv()` rather than IPC reload, `current_mode` of
`idle | rf | stream`, and the Slave pattern of one state record per
configured source built at startup.

Every block below is additive. Nothing existing is modified except the
three small edits listed at the end.

---

## Design decisions taken

| Question | Decision | Why |
|---|---|---|
| New tune route or extend `/api/tune`? | New route, `POST /api/tune_dvbt` | `TuneRequest` is entirely DVB-S2: `sr`, `plug`, `lnb_lo_khz`, `rcv`. None apply. `_tune_impl()` is Picotuner-specific throughout. |
| Video port | Its own, `HDHR_VIDEO_PORT = 10996` | 9941 is already contended between the RF path, ffmpeg and the diversity combiner, and has caused one "Address already in use" crash. Follows `REMOTE_VIDEO_OUT_PORT`'s precedent. |
| Status fields | DVB-T/T2 terms of its own | SNQ is not MER. Filling `mer` with an SNQ percentage would make two different measurements look comparable in the overlay. |
| `current_mode` | New value, `"dvbt"` | Checked against the sites seen so far and it behaves correctly — see the audit note below before merging. |
| mpv | `restart_mpv(url, is_rf=True)` | The device sends raw MPEG-TS over UDP, exactly what the RF flags are correct for. IPC reload was abandoned for good reasons documented at `restart_mpv()`. |

---

## 1. Config

Add to `lynx_config.yaml` (and the example file). A list, following
`remote_sources`, so a second device needs no schema change:

```yaml
hdhomerun_sources:
  - name: "HDHomeRun"
    device_id: "1250048C"     # 8 hex digits, from --discover
    tuner: 0                  # which tuner on the device
    presets:
      - name: "BBC One HD"
        modulation: "t8dvbt2"
        frequency: 546000000  # Hz
        program: 17536
      - name: "BBC A"
        modulation: "t8dvbt"
        frequency: 490000000
        program: 4164
```

Note frequency is in **Hz** here, not kHz. The device's own API works in
Hz, and converting at the edge invites exactly the kind of unit confusion
`calc_tuner_freq()` exists to prevent. The UI can show MHz.

---

## 2. Module-level additions

Place near the Slave block (around line 3460), so the two network-attached
tuners sit together.

```python
import lynx_hdhomerun

# Its own port, not 9941. That port is already shared between the RF
# direct-play path, stop_ffmpeg_bg()'s transcode and the diversity
# combiner, and has already produced one "Address already in use"
# crash when two of them overlapped. A third writer would be asking
# for the same failure, and a tuner that can stay locked while another
# source is on screen is worth having anyway.
HDHR_VIDEO_PORT = 10996

# How often the poller asks the device for status. The device is on
# the network, not a local broadcast, so this is a request rather than
# a packet arriving: often enough for the overlay to feel live, not so
# often that a busy device is being interrogated needlessly.
HDHR_POLL_INTERVAL_SECS = 2.0

# Filled only once the device has actually answered. Deliberately NOT
# the same names as REMOTE_QUALITY_FIELDS: these are DVB-T/T2
# measurements and presenting SNQ as MER would make two different
# things look comparable on one overlay.
HDHR_QUALITY_FIELDS = (
    "lock_mode",        # e.g. "t8dvbt2", "" when unlocked
    "signal_strength",  # ss,  0-100
    "signal_quality",   # snq, 0-100
    "symbol_quality",   # seq, 0-100
    "bitrate_bps",      # bps
    "frequency_hz",
    "programme",        # program name, once streaminfo has been read
)

# One state record per configured device, built at startup and indexed
# in step with hdhr_sources_cfg() — same reasoning as remote_states: a
# device that has never answered still has a record and shows as
# offline, rather than being absent.
hdhr_states: list = []


def hdhr_sources_cfg() -> list:
    """The configured HDHomeRun devices, as a list."""
    srcs = config.get('hdhomerun_sources')
    if isinstance(srcs, list):
        return srcs
    single = config.get('hdhomerun_source')
    if isinstance(single, dict) and single:
        return [single]
    return []


def hdhr_init_states():
    """Build one state record per configured device. Call at startup,
    alongside whatever builds remote_states."""
    global hdhr_states
    hdhr_states = []
    for cfg_entry in hdhr_sources_cfg():
        hdhr_states.append({
            "name": cfg_entry.get("name", "HDHomeRun"),
            "device_id": str(cfg_entry.get("device_id", "")),
            "tuner": int(cfg_entry.get("tuner", 0)),
            "online": False,
            "locked": False,
            "streaming": False,
            "last_seen": 0.0,
            "last_error": "",
            **{field: None for field in HDHR_QUALITY_FIELDS},
        })


def hdhr_source(i: int = 0) -> lynx_hdhomerun.HDHomeRunSource:
    """The configured device at index i, as a source object."""
    if not 0 <= i < len(hdhr_states):
        raise HTTPException(status_code=404,
                            detail=f"No HDHomeRun source at index {i}")
    st = hdhr_states[i]
    return lynx_hdhomerun.HDHomeRunSource(
        device_id=st["device_id"],
        lynx_host="127.0.0.1",
        tuner=st["tuner"],
        udp_port=HDHR_VIDEO_PORT,
    )
```

`lynx_host` is `127.0.0.1` because the device streams to this machine and
mpv reads it locally. If Lynx ever needs to receive on a specific
interface, make it a config value rather than guessing here.

---

## 3. Status poller

Mirrors `picotuner_monitor()`'s role, but polls rather than listens.

```python
def hdhomerun_monitor():
    """Background thread: polls each configured HDHomeRun for status.

    Polls rather than listens, because unlike the Picotuner and the
    Slaves, an HDHomeRun does not broadcast — it answers when asked.
    That difference is the whole reason this is a separate monitor
    rather than another branch inside an existing one.

    Runs regardless of current_mode, deliberately: knowing whether a
    tuner is locked before selecting it is more useful than only
    finding out afterwards, and the cost is one small request every
    couple of seconds.
    """
    while True:
        for i, st in enumerate(hdhr_states):
            try:
                tuner = lynx_hdhomerun.HDHomeRunTuner(
                    device_id=st["device_id"], tuner=st["tuner"]
                )
                status = tuner.status()
                st["online"] = True
                st["last_error"] = ""
                st["last_seen"] = time.time()
                st["locked"] = status.locked
                st["streaming"] = status.streaming
                st["lock_mode"] = status.lock if status.locked else ""
                st["signal_strength"] = status.signal_strength
                st["signal_quality"] = status.signal_quality
                st["symbol_quality"] = status.symbol_quality
                st["bitrate_bps"] = status.bits_per_second
                st["frequency_hz"] = status.frequency_hz
            except lynx_hdhomerun.HDHomeRunError as e:
                # An unreachable device is ordinary — it may be
                # unplugged, or on a site link that is down. Recorded,
                # not logged every two seconds.
                st["online"] = False
                st["locked"] = False
                st["streaming"] = False
                st["last_error"] = str(e)
        time.sleep(HDHR_POLL_INTERVAL_SECS)
```

Start it wherever the other monitor threads are started:

```python
threading.Thread(target=hdhomerun_monitor, daemon=True).start()
```

---

## 4. Request model

Place beside `TuneRequest` (around line 4014).

```python
class DvbtTuneRequest(BaseModel):
    freq: int                 # Hz — the actual frequency. No LNB, no
                              # transverter arithmetic: this tuner is
                              # connected to an aerial, not a down-converter.
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
    source: int = 0           # Index into hdhomerun_sources, for sites
                              # with more than one device.
```

---

## 5. The tune route

Same lock discipline as `tune()`. The thin-wrapper/impl split is kept for
exactly the reason given at `tune()`: a failure before the async thread
starts must not leave the lock held.

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

    source = hdhr_source(req.source)

    # Lynx is changing source, so whatever was being received is no
    # longer being listened to — same reason and same placement as in
    # _tune_impl(), before anything is tuned, so the watcher's next
    # poll already knows rather than racing.
    try:
        pathfinder_source_changed()
    except Exception:
        pass

    # Tear down whatever owns the video path now. The HDHomeRun has
    # its own port, so this is not about port contention — it is so
    # that a Picotuner still pushing to 9941, or a combiner still
    # running, does not sit there consuming CPU and confusing the
    # lifecycle monitor while a different source is on screen.
    stop_ffmpeg_bg()
    if diversity_enabled:
        stop_diversity_combiner()
        diversity_enabled = False

    # Tune and wait for lock. Unlike the RF path — which defers mpv
    # until rf_mpv_lifecycle_monitor() confirms a stable lock — the
    # device tells us itself when it has locked and when packets are
    # actually flowing, so there is nothing to infer and no reason to
    # wait twice.
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
        "source": req.source,
    })

    return {
        "success": True,
        "mode": "dvbt",
        "freq_hz": req.freq,
        "modulation": req.modulation,
        "program": req.program,
        "source": req.source,
    }
```

---

## 6. Teardown

The device keeps streaming until told otherwise, so every path that
leaves this source must stop it. Add to whatever `stop_reception()` or
equivalent already stops the Picotuner:

```python
def hdhr_stop_all():
    """Stop every configured device streaming.

    Called when Lynx moves away from this source. Without it the tuner
    stays busy and keeps pushing packets at a port nobody is reading —
    which is not harmful, but it is exactly the kind of thing that is
    invisible until the day it matters.
    """
    for i in range(len(hdhr_states)):
        try:
            hdhr_source(i).stop()
        except Exception as e:
            print(f"[hdhr] stop failed: {type(e).__name__}: {e}")
```

Call it from:
- `_tune_impl()`, near the existing teardown, so an RF tune stops it
- the stream tune path, likewise
- whatever handles going idle

---

## 7. Status endpoint

Add to the existing status dict (around line 6299, beside `"mode"`):

```python
"hdhomerun": [
    {
        "name": st["name"],
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
    for st in hdhr_states
],
```

The overlay shows `quality` labelled **SNQ**, not MER, and `errors`
labelled **SEQ**. Different measurements, different names.

---

## Three edits to existing code

1. **Startup** — call `hdhr_init_states()` where `remote_states` is built,
   and start `hdhomerun_monitor()` with the other monitor threads.
2. **Teardown** — call `hdhr_stop_all()` from the RF and stream tune
   paths, and from the idle path.
3. **Resume** — `save_last_state({"mode": "dvbt", ...})` means the resume
   logic needs a `"dvbt"` branch, or a receiver that reboots while on this
   source comes back idle.

---

## Before merging: the `current_mode` audit

`current_mode` is checked in at least 30 places. `"dvbt"` was checked
against the sites visible so far and behaves correctly:

- `if current_mode != "rf"` — RF monitors stay out of the way. Correct.
- `current_mode == "stream"` — stream info stays absent. Correct.
- `picture_ready=(current_mode != "rf" or mpv_running_for_rf)` at ~6357 —
  evaluates True, so the cover comes down. Correct.

That is not a substitute for checking the rest:

```bash
grep -n "current_mode" lynx_app.py
```

Look for anywhere that treats "not rf" as "must be stream", or
"not stream" as "must be rf". Those are the two shapes that break when a
third value appears, and they will not announce themselves.

Then, as always:

```bash
python3 verify.py
```
