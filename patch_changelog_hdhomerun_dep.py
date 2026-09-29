#!/usr/bin/env python3
"""
patch_changelog_hdhomerun_dep.py — record the missing dependency.

Lynx drives an HDHomeRun through hdhomerun_config, SiliconDust's own
command-line tool, and install.sh did not install it. The development
machine had it from earlier experiments, so discovery worked there from
the first run and the gap went unnoticed - the beta channel shipped
code that could not work out of the box for anyone else.

Worth recording properly rather than quietly fixing, because it is the
kind of mistake that repeats: a dependency that happens to be present
on the machine it was written on is invisible until somebody else
tries.

Run from the lynx directory:
    python3 patch_changelog_hdhomerun_dep.py            # dry run
    python3 patch_changelog_hdhomerun_dep.py --apply
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

TARGET = Path("CHANGELOG.md")
MARKER = "hdhomerun_config"

ANCHOR = "**Fixed**\n- The stream target was set to `udp://127.0.0.1:<port>`"

ENTRY = """**Fixed**
- Lynx drives an HDHomeRun through `hdhomerun_config`, SiliconDust's own command-line tool, and `install.sh` did not install it. The development machine had it from earlier experiments, so discovery worked there from the first run and the dependency went unnoticed: the beta channel shipped code that could not work out of the box for anyone else. Found by the first user to try it, whose log said exactly what was missing - the error message at least did its job. The package is now installed above the `--deps-only` exit, so Update Now retro-fits an existing receiver rather than only helping fresh installs.
- The stream target was set to `udp://127.0.0.1:<port>`"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    if not TARGET.exists():
        print(f"ERROR: {TARGET} not found. Run from the lynx directory.")
        return 2

    text = TARGET.read_text(encoding="utf-8")

    if MARKER in text:
        print("CHANGELOG already mentions it - nothing to do.")
        return 0

    n = text.count(ANCHOR)
    print(f"\nchecking anchor in {TARGET}\n")
    if n != 1:
        print(f"  Fixed section       FAIL {n} matches - expected 1")
        print("\nNo changes made.")
        return 1
    print("  Fixed section       OK   one match")

    patched = text.replace(ANCHOR, ENTRY, 1)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write the change.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    shutil.copy2(TARGET, TARGET.with_suffix(f".md.bak-{stamp}"))
    TARGET.write_text(patched, encoding="utf-8")
    print(f"\n  {TARGET} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
