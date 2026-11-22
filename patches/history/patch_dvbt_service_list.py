#!/usr/bin/env python3
"""
patch_dvbt_service_list.py

The DVB-T2 service list (the "Service" dropdown on the DVB-T card) now
fills itself whenever the HDHomeRun is locked, and shows the service
that is actually playing.

THE BUG
-------
loadDvbtPrograms() had exactly two callers: the card's own Tune button
and a click on one of the card's own presets. So the list was empty

  * after any page refresh, even mid-programme
  * after a tune from anywhere else: the main preset list, the Tune
    boxes, Companion, the knobler, or the restore at boot

and when it did fill outside those two paths it selected the first
service on the multiplex, whatever was playing. Reported on 21 Oct 2026
watching BBC TWO on PSB3: Manual tune filled the list, a preset tune
did not, and a refresh emptied it. Not platform-specific; the Pi does
the same.

THE FIX
-------
The status poll already knows when the HDHomeRun is locked and on which
frequency. When it is locked on a multiplex the page has not loaded a
list for, it loads one. Once per multiplex, so the dropdown is never
rebuilt under somebody's mouse. An empty answer (service tables not in
yet) is retried, at most every 10 s.

Selection then prefers, in order: the service the tuner reports it is
playing, what was already selected, the first service. Playing comes
first because a service number kept from the previous multiplex can
name a different service on this one.

JavaScript only; nothing changes on the server.

Run from the repo root:  python3 patch_dvbt_service_list.py
Then:                    python3 verify.py lynx_app.py
"""

import sys
from pathlib import Path

TARGET = Path("lynx_app.py")


def apply(src: str, old: str, new: str, label: str, count: int = 1) -> str:
    n = src.count(old)
    if n != count:
        sys.exit(f"ABORT [{label}]: anchor found {n} times, expected {count}")
    print(f"  ok  {label}")
    return src.replace(old, new, count)


def main():
    if not TARGET.exists():
        sys.exit(f"ABORT: {TARGET} not found - run this from the repo root")
    src = TARGET.read_text()
    before = len(src.splitlines())
    if "_dvbtProgramsFor" in src:
        sys.exit("ABORT: already patched")

    # No backslashes below: this lands inside a plain Python string.

    # ── 1. Status poll: load the list once per locked multiplex ─────
    src = apply(
        src,
        "                        if (lm) { window._dvbtLockMode = lm; }\n",
        "                        if (lm) { window._dvbtLockMode = lm; }\n"
        "                        // Service list: see patch_dvbt_service_list.py.\n"
        "                        // Once per multiplex, whoever tuned it, so a\n"
        "                        // refresh or a tune from elsewhere still fills it.\n"
        "                        window._dvbtServiceName = hh.service_name || '';\n"
        "                        var fkey = hh.frequency_hz || 0;\n"
        "                        var stale = window._dvbtProgramsFor !== fkey;\n"
        "                        var retry = window._dvbtProgramsEmptyAt &&\n"
        "                                    (Date.now() - window._dvbtProgramsEmptyAt > 10000);\n"
        "                        if (fkey && (stale || retry) && !window._dvbtProgramsBusy) {\n"
        "                            window._dvbtProgramsFor = fkey;\n"
        "                            window._dvbtProgramsBusy = true;\n"
        "                            loadDvbtPrograms(8).finally(function () {\n"
        "                                window._dvbtProgramsBusy = false;\n"
        "                            });\n"
        "                        }\n",
        "load on lock")

    # ── 2. Remember an empty answer so it is retried, not forgotten ─
    src = apply(
        src,
        "        if (!progs.length) {\n"
        "            var none = document.createElement('option');\n",
        "        window._dvbtProgramsEmptyAt = progs.length ? 0 : Date.now();\n"
        "        if (!progs.length) {\n"
        "            var none = document.createElement('option');\n",
        "empty answer retried")

    # ── 3. Select what is playing, not just the first service ───────
    src = apply(
        src,
        "        sel.value = wanted;\n"
        "        if (!sel.value) { sel.selectedIndex = 0; }\n",
        "        // The service the tuner says is playing comes first: a\n"
        "        // service number kept from the previous multiplex can\n"
        "        // name a different service on this one.\n"
        "        var playing = -1;\n"
        "        for (var k = 0; k < sel.options.length; k++) {\n"
        "            if (window._dvbtServiceName &&\n"
        "                sel.options[k].textContent === window._dvbtServiceName) {\n"
        "                playing = k;\n"
        "                break;\n"
        "            }\n"
        "        }\n"
        "        if (playing >= 0) { sel.selectedIndex = playing; }\n"
        "        else { sel.value = wanted; }\n"
        "        if (!sel.value) { sel.selectedIndex = 0; }\n",
        "select the playing service")

    TARGET.write_text(src)
    after = len(src.splitlines())
    print(f"lynx_app.py: {before} -> {after} lines (+{after - before})")


if __name__ == "__main__":
    main()
