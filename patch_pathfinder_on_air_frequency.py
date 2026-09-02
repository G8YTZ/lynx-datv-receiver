#!/usr/bin/env python3
"""
Lynx beta-wip patch: Pathfinder card shows the on-air frequency, not the IF.

The fault
---------
Confirmed live 2026-09-02: receiving 71.010 MHz through a SpyVerter
(120 MHz LO), the Web UI correctly showed Downlink 71.010 / IF 191.010 /
LNB LO 120.000, while the Pathfinder card showed 191 MHz - the raw IF.

_pathfinder_arm() builds its snapshot straight from
telemetry.get('frequency'), which is what the Picotuner reports: the IF.
The tuner has no idea a converter sits in front of it. Nothing in that
path ever reverses the conversion, so the card shows the IF whenever any
converter is in use - wrong by the LO for an LNB, and wrong by the LO in
the other direction for an up-converter.

The globals were verified correct at the time (lnb_lo_khz 120000,
downlink 71.01), so this is purely a consumer not using them, not a
converter-state problem.

The fix
-------
Reverse the conversion using the machinery that already exists and is
already proven right on the Web UI path:

  - _pathfinder_lnb_lo_khz(rcv) already gives the LO for this receiver,
    following the same source-of-truth rules as
    _picotuner_expected_tuning() (tri_watch's per-source config when it
    is driving, otherwise the saved tuning state). Pathfinder already
    calls it for the QO-100 check.
  - converter_side() decides up/low/high from the numbers, in the one
    place that rule lives.
  - _compute_downlink_frequency() does the reversal itself, and already
    accepts an explicit state/lo/side rather than assuming tuner A.

Adding a fourth copy of the arithmetic here is exactly how the three
existing copies came to disagree, so this adds none.

The on-air frequency needs the WANTED frequency to pick the converter
side, not the IF - so _pathfinder_converter() reads both the LO and the
wanted frequency from the same source, and asks converter_side().

Deliberately unchanged
----------------------
The QO-100 check at the top of this section still does its own
`if_khz + LO`. It is only ever asking "is this a 3cm satellite contact",
and an up-converter contact is never one, so the answer it reaches is
right either way. Changing it is a separate question from what the card
displays, and this patch does not touch it.

Falls back to the raw reported value on any failure, which is exactly
today's behaviour - a converter fault must not cost the card its
frequency entirely.

Run from the directory containing lynx_app.py.
"""

import sys

PATH = "lynx_app.py"

OLD = '''    snapshot = {
        'mer': telemetry.get('mer', ''),
        'modcod': telemetry.get('modcod', ''),
        'symbol_rate': telemetry.get('symbol_rate', ''),
        'frequency': telemetry.get('frequency', ''),
    }
'''

NEW = '''    snapshot = {
        'mer': telemetry.get('mer', ''),
        'modcod': telemetry.get('modcod', ''),
        'symbol_rate': telemetry.get('symbol_rate', ''),
        'frequency': _pathfinder_on_air_frequency(
            rcv, telemetry.get('frequency', '')),
    }
'''

HELPER_ANCHOR = '''def _pathfinder_arm(callsign, rcv, telemetry):'''

HELPER = '''def _pathfinder_converter(rcv):
    """The converter in front of a receiver, as (lo_khz, side).

    The LO alone is not enough to reverse the conversion: an
    up-converter ADDS its LO to reach the tuner while an LNB subtracts,
    so the side has to be known too. converter_side() decides that from
    the WANTED frequency and the LO - not from the IF - so both are read
    here from the same source of truth
    _pathfinder_lnb_lo_khz() already uses.

    Returns (0, "low") when there is no converter, which the caller
    treats as nothing to do.
    """
    try:
        if _tri_watch_present():
            for src in globals().get('tri_watch_sources_cfg', []):
                if src.get('enabled') and src.get('type') == 'rf' \\
                        and src.get('rcv') == rcv:
                    lo = int(src.get('lnb_lo_khz', 0) or 0)
                    return lo, converter_side(int(src.get('freq', 0) or 0), lo)
            return 0, "low"
        state = load_last_state() or {}
        lo = int(state.get('lnb_lo_khz', 0) or 0)
        return lo, converter_side(int(state.get('freq', 0) or 0), lo)
    except Exception as e:
        print(f"[map] could not determine converter state: {e}")
        return 0, "low"


def _pathfinder_on_air_frequency(rcv, reported_mhz):
    """The real on-air frequency for the card, from the reported IF.

    The Picotuner reports the IF - it has no idea a converter is in
    front of it. Confirmed live: 71.010 MHz through a 120 MHz SpyVerter
    reported as 191.010, and the card showed 191 until this existed,
    while the Web UI showed 71.010 correctly the whole time from the
    same underlying state.

    Reuses _compute_downlink_frequency() rather than repeating its
    arithmetic. Three copies of this conversion already existed and
    disagreed with each other, which is the whole reason the card was
    wrong; a fourth would not help.

    Returns the reported value unchanged when there is no converter, or
    if anything at all goes wrong - a card showing the IF is a much
    smaller problem than a card with no frequency on it.
    """
    raw = str(reported_mhz or '').strip()
    if not raw:
        return reported_mhz
    try:
        lo_khz, side = _pathfinder_converter(rcv)
        if not lo_khz:
            return reported_mhz
        on_air = _compute_downlink_frequency(
            state={"frequency": raw}, lo_khz=lo_khz, side=side)
        if on_air is None:
            return reported_mhz
        return f"{on_air:.3f}"
    except Exception as e:
        print(f"[map] could not convert IF to on-air frequency: {e}")
        return reported_mhz


def _pathfinder_arm(callsign, rcv, telemetry):'''

with open(PATH, "r") as f:
    content = f.read()

n_old = content.count(OLD)
n_anchor = content.count(HELPER_ANCHOR)
if n_old != 1 or n_anchor != 1:
    print(f"ABORT: expected 1 match each, found snapshot={n_old} "
          f"anchor={n_anchor}. No changes written.")
    sys.exit(1)

content = content.replace(OLD, NEW)
content = content.replace(HELPER_ANCHOR, HELPER)

with open(PATH, "w") as f:
    f.write(content)

print("Patched: Pathfinder card now shows the on-air frequency, not the IF.")
