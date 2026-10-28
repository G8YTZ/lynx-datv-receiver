#!/usr/bin/env python3
"""
patch_version_tags.py

The version badge only ever counts from a release tag (v...).

Lynx names its version with `git describe --tags`, which uses the
nearest tag of any kind. On 24 Oct 2026 a safety tag, beta-wip-old,
existed on GitHub for about five minutes; a receiver that updated in
those minutes kept it (a fetch never deletes tags), and from then on
showed "vbeta-wip-old-50-g62191fe [BETA]" instead of
"v2026.08.04-239-g62191fe [BETA]". The code was right; only the name
was wrong. --match 'v*' means no other tag can ever do this again.

Run from the repo root:  python3 patch_version_tags.py
Then:                    python3 verify.py lynx_app.py
"""

import sys
from pathlib import Path

p = Path("lynx_app.py")
if not p.exists():
    sys.exit("ABORT: lynx_app.py not found - run this from the repo root")
src = p.read_text()
old = 'ok, out = git_cmd("describe", "--tags", "--always")'
new = 'ok, out = git_cmd("describe", "--tags", "--match", "v*", "--always")'
if new in src:
    sys.exit("ABORT: already patched")
if src.count(old) != 1:
    sys.exit(f"ABORT: anchor found {src.count(old)} times, expected 1")
p.write_text(src.replace(old, new, 1))
print("  ok  version counts from release tags only")
