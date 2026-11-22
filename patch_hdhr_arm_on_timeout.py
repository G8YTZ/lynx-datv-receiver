#!/usr/bin/env python3
"""
patch_hdhr_arm_on_timeout.py

An HDHomeRun tuned to a quiet repeater picks the picture up when the
repeater comes on.

THE BUG (Rob VK3IE, HDHR5-4DT on VK3RTV, which only transmits on
activity; reported 27 Oct 2026)
---------------------------------------------------------------------
HDHomeRunTuner.tune() did things in this order: set the channel, wait
for lock, THEN set the programme filter and the stream target. With the
repeater off air the wait timed out and raised before the last two, so
the box was left on the right channel with no programme chosen and no
stream target. When the repeater keyed up, the box locked (the panel
showed it, at about 10 Mb/s: the whole multiplex, unfiltered) but sent
nothing to Lynx, so nothing decoded, however long it was left. A preset
change and back cured it, because that tune found the signal present.

The same happens after a reboot or restart while the repeater is quiet:
the resume tune times out the same way. Exactly Rob's "if VK3RTV is not
broadcasting, it fails to decode when a signal does come up".

THE FIX
-------
  1. lynx_hdhomerun: on a lock timeout, the programme filter and the
     stream target are still set before the timeout is reported. The box
     is then armed: the moment the repeater keys up it locks and streams
     the chosen programme to Lynx. The timeout is still raised, so the
     manual card's "DVB-T2, then DVB-T" fallback works as before.
  2. lynx_app: a tune that timed out still records what it is waiting
     for (programme, frequency) and saves it as the state to resume, so
     the service name and callsign are fetched when the lock comes, and
     a restart goes back to the same repeater. Lynx's existing re-lock
     restart then gives mpv a fresh start on the stream.

Run from the repo root:  python3 patch_hdhr_arm_on_timeout.py
Then:                    python3 verify.py lynx_app.py
"""

import sys
from pathlib import Path


def apply(src, old, new, label, count=1):
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


def main():
    for f in ("lynx_hdhomerun.py", "lynx_app.py"):
        if not Path(f).exists():
            sys.exit(f"ABORT: {f} not found - run this from the repo root")

    p = Path("lynx_hdhomerun.py")
    src = p.read_text()
    if "armed for when the signal" in src:
        sys.exit("ABORT: already patched")
    src = apply(src,
                "        status = self._wait_for_lock(lock_timeout, poll_interval)\n"
                "\n"
                "        if program is not None:\n",
                "        try:\n"
                "            status = self._wait_for_lock(lock_timeout, poll_interval)\n"
                "        except HDHomeRunLockTimeout:\n"
                "            # Nothing on air yet - a repeater that only transmits on\n"
                "            # activity. Set the programme and the target anyway, so\n"
                "            # the box is armed for when the signal arrives: it then\n"
                "            # locks and streams by itself. Without this it locked\n"
                "            # later but sent nothing (patch_hdhr_arm_on_timeout.py).\n"
                "            if program is not None:\n"
                "                self.set(self._path(\"program\"), str(program))\n"
                "            if target:\n"
                "                self.set(self._path(\"target\"), target)\n"
                "                self._target = target\n"
                "            raise\n"
                "\n"
                "        if program is not None:\n",
                "hdhomerun: arm the box on a lock timeout")
    p.write_text(src)

    p = Path("lynx_app.py")
    src = p.read_text()
    src = apply(src,
                "    except lynx_hdhomerun.HDHomeRunLockTimeout as e:\n"
                "        # Lower the cover before giving up, or a failed tune leaves the\n"
                "        # screen covered until something else happens to raise and\n"
                "        # lower it - which on a receiver doing nothing else is never.\n"
                "        end_transition_cover()\n"
                "        raise HTTPException(status_code=504, detail=str(e))\n",
                "    except lynx_hdhomerun.HDHomeRunLockTimeout as e:\n"
                "        # The box is armed for when the signal arrives (see\n"
                "        # patch_hdhr_arm_on_timeout.py): remember what it is waiting\n"
                "        # for, so the name and callsign are fetched on lock, and so a\n"
                "        # restart resumes the same repeater rather than the last one\n"
                "        # that happened to be on air.\n"
                "        with hdhr_lock:\n"
                "            _w = hdhr_states.get(_dev)\n"
                "            if _w is not None:\n"
                "                _w[\"service_name\"] = \"\"\n"
                "                _w[\"callsign\"] = \"\"\n"
                "                _w[\"pending_program\"] = (str(req.program)\n"
                "                                         if req.program is not None else None)\n"
                "                _w[\"pending_freq_hz\"] = req.freq\n"
                "                _w[\"last_program\"] = _w[\"pending_program\"]\n"
                "                _w[\"logged_callsign\"] = \"\"\n"
                "                _w[\"pending_attempts\"] = 10\n"
                "        try:\n"
                "            save_last_state({\n"
                "                \"mode\": \"dvbt\",\n"
                "                \"freq\": req.freq,\n"
                "                \"modulation\": req.modulation,\n"
                "                \"program\": req.program,\n"
                "                \"device_id\": req.device_id,\n"
                "            })\n"
                "        except Exception as _e:\n"
                "            print(f\"[hdhr] could not save the armed tune: {_e}\")\n"
                "        print(f\"[hdhr] no signal on {req.freq / 1e6:.3f} MHz yet - \"\n"
                "              f\"armed, will play when it arrives\", flush=True)\n"
                "        # Lower the cover before giving up, or a failed tune leaves the\n"
                "        # screen covered until something else happens to raise and\n"
                "        # lower it - which on a receiver doing nothing else is never.\n"
                "        end_transition_cover()\n"
                "        raise HTTPException(status_code=504, detail=str(e))\n",
                "app: remember the armed tune")
    p.write_text(src)


if __name__ == "__main__":
    main()
