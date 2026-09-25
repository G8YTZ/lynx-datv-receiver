#!/usr/bin/env python3
"""
patch_slave_panel_rows.py

Gives a Slave panel the same rows as a local receiver, and reads the
codec fields the Slave now sends.

Three parts:

  1. $31 and $34 parsed into codec / audio_codec, the same names and
     the same tags the Picotuner path already uses.

  2. The panel gains the local receiver's full row set, in its order:
     Callsign, Programme, Frequency, Symbol Rate, MER, Margin, Level,
     Mode, Codec, Audio Codec, Firmware.

  3. The locator moves from a row into the panel header, beside the
     Slave's name. It had been an extra row, which is precisely what
     stops the two panels looking alike — and the header is where a
     thing only a Slave has belongs, since only a Slave has a header
     of its own to put it in.

Firmware stays a dash. A Slave runs Longmynd on a Pico adaptor, not
Picotuner firmware, so there is no equivalent file name to report and
inventing one would be worse than the dash. Something honest could go
there later — a Longmynd version — but it would be the Slave's own
identifier rather than something pretending to be a Picotuner's.

Run from the repo root:  python3 patch_slave_panel_rows.py
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

    if "remote_quality_monitor" not in src:
        sys.exit("ABORT: quality monitor patch not applied — run that first")
    if '"audio_codec",' in src and "REMOTE_QUALITY_FIELDS" in src and \
            src.count('st["audio_codec"]') > 0:
        sys.exit("ABORT: already patched")

    # ── 1. codec joins the quality field list ────────────────────
    src = apply(
        src,
        '    "mer", "margin", "symbol_rate", "modcod", "programme",\n'
        '    "dbm", "agc1", "agc2",\n'
        ')',
        '    "mer", "margin", "symbol_rate", "modcod", "programme",\n'
        '    "dbm", "agc1", "agc2", "codec", "audio_codec",\n'
        ')',
        "codec in REMOTE_QUALITY_FIELDS",
    )

    # ── 2. parse the tags ────────────────────────────────────────
    src = apply(
        src,
        "            if '$27' in fields: st[\"agc2\"]        = fields['$27']",
        "            if '$27' in fields: st[\"agc2\"]        = fields['$27']\n"
        "            # Same tags the Picotuner uses, so a Slave and a local\n"
        "            # tuner fill these rows by the same route.\n"
        "            if '$31' in fields: st[\"codec\"]       = fields['$31']\n"
        "            if '$34' in fields: st[\"audio_codec\"] = fields['$34']",
        "parse $31/$34",
    )

    # ── 3. the panel ─────────────────────────────────────────────
    src = apply(
        src,
        """            setPanelState(id + '-header', id + '-status',
                          '&#x1F4E1; ' + (rem.name || 'Slave Rx'), st);
            const rows = [
                ['Callsign',  rem.callsign || '—'],
                ['Frequency', rem.frequency ? rem.frequency + ' MHz' : '—'],
            ];
            if (rem.locator) rows.push(['Locator', rem.locator]);""",
        """            // Locator in the header rather than as a row: it is the one
            // thing a Slave has that a local receiver does not, and putting
            // it in the rows is what stopped the two panels looking alike.
            const label = '&#x1F4E1; ' + (rem.name || 'Slave Rx')
                        + (rem.locator ? ' — ' + rem.locator : '');
            setPanelState(id + '-header', id + '-status', label, st);
            // Deliberately the local receiver's row set, in its order and
            // with its units. A Slave sends the same fields under the same
            // tags, so anything that reads differently here is a fault
            // rather than a difference worth preserving.
            const rows = [
                ['Callsign',    rem.callsign || '—'],
                ['Programme',   rem.programme || '—'],
                ['Frequency',   rem.frequency ? rem.frequency + ' MHz' : '—'],
                ['Symbol Rate', rem.symbol_rate ? rem.symbol_rate + ' kS/s' : '—'],
                ['MER',         rem.mer ? rem.mer + ' dB' : '—'],
                ['Margin',      rem.margin ? rem.margin + ' dB' : '—'],
                ['Level',       (rem.dbm && rem.dbm !== '0') ? rem.dbm + ' dBm' : '—'],
                ['Mode',        rem.modcod || '—'],
                ['Codec',       rem.codec || '—'],
                ['Audio Codec', rem.audio_codec || '—'],
                // A Slave runs Longmynd on a Pico adaptor, not Picotuner
                // firmware. There is no equivalent to report, and a dash
                // says so more honestly than something invented.
                ['Firmware',    '—'],
            ];""",
        "Slave panel rows + locator in header",
    )

    shutil.copy2(TARGET, TARGET.with_suffix(".py.bak_rows"))
    TARGET.write_text(src)

    after = len(src.splitlines())
    print(f"\n  lynx_app.py: {before} -> {after} lines (+{after - before})")
    print("\nNow: python3 -m py_compile lynx_app.py, then node --check the script blocks")


if __name__ == "__main__":
    main()
