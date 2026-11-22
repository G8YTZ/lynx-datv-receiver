#!/usr/bin/env python3
"""
patch_remote_resume.py

Makes a selected Slave come back after a restart.

THE GAP
-------
A Slave is saved as {"mode": "stream", "url": "udp://@:10999"} because
it plays through the stream path. On restart Lynx resumes that happily
and points mpv at the relay's output port — but the relay itself starts
with nothing selected, so nothing is forwarded there. Everything runs,
the Slave is locked, packets are arriving on the ingress port, and the
screen is black. Confirmed live after a reboot.

The selection only ever lived in memory. Saving the URL was never enough
on its own: it says where mpv should listen, not whose video should be
put there.

THE WAIT
--------
Resume cannot simply select on startup. The relay identifies a Slave by
the address its status arrives from, and at boot that has not happened
yet — so an immediate select would be refused by the endpoint's own
guard and the Slave would stay dark until somebody clicked. So the
resume waits for the Slave to report, up to a limit, then selects.

The wait is bounded rather than indefinite: a Slave that never comes
back should leave Lynx idle and available for something else, not
blocked forever on a site that is off the air.

Run from the repo root:  python3 patch_remote_resume.py
"""

import shutil
import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


def apply(src: str, old: str, new: str, label: str) -> str:
    n = src.count(old)
    if n != 1:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected exactly 1")
    print(f"  ok  {label}")
    return src.replace(old, new, 1)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found — run this from the repo root")

    src = TARGET.read_text()
    before = len(src.splitlines())

    if "remote_index" in src:
        sys.exit("ABORT: already patched")
    if "REMOTE_VIDEO_OUT_PORT" not in src:
        sys.exit("ABORT: Slave select patch not applied — this builds on it")

    # ── 1. how long to wait at boot ──────────────────────────────
    src = apply(
        src,
        "REMOTE_QUALITY_PORT_OFFSET = 96",
        "REMOTE_QUALITY_PORT_OFFSET = 96\n"
        "\n"
        "# How long a resume waits for a saved Slave to report in before\n"
        "# giving up. It cannot be selected until its address is known, and\n"
        "# at boot nothing has been heard yet. Bounded rather than endless:\n"
        "# a site that is off the air should leave the receiver idle and\n"
        "# usable, not stuck waiting for it.\n"
        "REMOTE_RESUME_WAIT_SECS = 30",
        "resume wait constant",
    )

    # ── 2. save which Slave, not just the port ───────────────────
    src = apply(
        src,
        "    slave_relay.select(index)\n",
        "    slave_relay.select(index)\n"
        "    # start_stream() below saves the URL, which says where mpv\n"
        "    # should listen but not whose video belongs there. Saved after\n"
        "    # it returns, so this write is the one that survives.\n"
        "    _remote_resume_index = index\n",
        "capture index for save",
    )

    src = apply(
        src,
        "    return start_stream(StreamRequest(\n"
        "        url=f\"udp://@:{REMOTE_VIDEO_OUT_PORT}\",",
        "    result = start_stream(StreamRequest(\n"
        "        url=f\"udp://@:{REMOTE_VIDEO_OUT_PORT}\",",
        "capture start_stream result",
    )

    src = apply(
        src,
        "        name=remote_display_name(index),\n"
        "    ))\n",
        "        name=remote_display_name(index),\n"
        "    ))\n"
        "    save_last_state({\n"
        "        \"mode\": \"stream\",\n"
        "        \"url\": f\"udp://@:{REMOTE_VIDEO_OUT_PORT}\",\n"
        "        \"name\": remote_display_name(index),\n"
        "        \"remote_index\": _remote_resume_index,\n"
        "    })\n"
        "    return result\n",
        "save remote_index",
    )

    # ── 3. resume it ─────────────────────────────────────────────
    src = apply(
        src,
        '        elif state and state.get("mode") == "stream":\n'
        '            print(f"Resuming previous stream: {state.get(\'name\')}")\n'
        '            try:\n'
        '                start_stream(StreamRequest(url=state["url"], name=state.get("name", "")))\n'
        '                return\n'
        '            except Exception as e:\n'
        '                print(f"Could not resume previous stream: {e}")\n',
        '        elif state and state.get("mode") == "stream":\n'
        '            # A Slave is saved as a stream, because that is how it\n'
        '            # plays, plus the index of which Slave it was. Without\n'
        '            # telling the relay, resuming the URL alone points mpv\n'
        '            # at a port nothing is being forwarded to.\n'
        '            _rem_idx = state.get("remote_index")\n'
        '            if _rem_idx is not None:\n'
        '                print(f"Resuming Slave Rx {_rem_idx}: waiting for it to report")\n'
        '                for _ in range(REMOTE_RESUME_WAIT_SECS):\n'
        '                    if (0 <= _rem_idx < len(remote_states)\n'
        '                            and remote_states[_rem_idx]["enabled"]\n'
        '                            and remote_states[_rem_idx]["addr"]):\n'
        '                        break\n'
        '                    time.sleep(1)\n'
        '                try:\n'
        '                    select_remote_source(_rem_idx)\n'
        '                    print(f"Resumed Slave Rx {_rem_idx}")\n'
        '                    return\n'
        '                except Exception as e:\n'
        '                    # Left idle rather than falling through to the\n'
        '                    # plain stream resume: that would point mpv at\n'
        '                    # the relay output with nothing selected, which\n'
        '                    # is a black screen with no explanation.\n'
        '                    print(f"Could not resume Slave Rx {_rem_idx}: {e}")\n'
        '            else:\n'
        '                print(f"Resuming previous stream: {state.get(\'name\')}")\n'
        '                try:\n'
        '                    start_stream(StreamRequest(url=state["url"], name=state.get("name", "")))\n'
        '                    return\n'
        '                except Exception as e:\n'
        '                    print(f"Could not resume previous stream: {e}")\n',
        "resume a saved Slave",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_resume"))
    TARGET.write_text(src)

    after = len(src.splitlines())
    print(f"\n  lynx_app.py: {before} -> {after} lines (+{after - before})")
    print("\nNow: python3 -m py_compile lynx_app.py")


if __name__ == "__main__":
    main()
