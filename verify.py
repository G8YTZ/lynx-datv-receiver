#!/usr/bin/env python3
"""verify.py - checks lynx_app.py is intact after editing.

Written after an evening of breaking it three times. Every failure came
from the same place: verifying the fragment I had just written rather
than the file I had disturbed. ast.parse() is not enough - the page
JavaScript lives inside Python strings, so a broken script passes a
Python syntax check without complaint, and the browser then abandons
the whole page.

So this checks the things that actually broke:

  1. Python parses.
  2. EVERY embedded <script> block parses as JavaScript.
  3. The Config page's three columns are balanced and at equal depth.
  4. Nothing was removed that was not meant to be removed.

Usage:  python3 verify.py lynx_app.py [reference.py]
"""

import ast
import re
import subprocess
import sys


def check_python(path):
    try:
        ast.parse(open(path).read())
        return True, "parses"
    except SyntaxError as e:
        return False, f"line {e.lineno}: {e.msg}"


def check_scripts(path):
    """Every <script> block, checked as JavaScript. This is the one that
    would have caught the loadPresets damage."""
    s = open(path).read()
    blocks = re.findall(r'<script[^>]*>(.*?)</script>', s, re.S)
    results = []
    for n, b in enumerate(blocks):
        if len(b.strip()) < 40:
            continue
        tmp = f"/tmp/_verify_{n}.js"
        with open(tmp, "w") as f:
            f.write(b)
        r = subprocess.run(["node", "--check", tmp],
                           capture_output=True, text=True)
        if r.returncode == 0:
            results.append((n, len(b), True, ""))
        else:
            first = [l for l in r.stderr.strip().split("\n") if l.strip()]
            msg = " / ".join(first[:3])
            results.append((n, len(b), False, msg))
    return results


def check_columns(path):
    """The Config page's three columns must open at the same nesting
    depth. A stray <div> puts column 3 inside column 2, which is exactly
    what happened."""
    s = open(path).read()
    try:
        i = s.index('<div class="row g-3 align-items-start">')
        j = s.index('</html>', i)
    except ValueError:
        return None, "config page block not found"
    blk = s[i:j]
    depth = 0
    depths = []
    for m in re.finditer(r'<div\b[^>]*>|</div>', blk):
        t = m.group(0)
        if t.startswith('</div'):
            depth -= 1
        else:
            depth += 1
            if 'col-md-4' in t:
                depths.append(depth)
    cols = depths[:3]
    ok = len(cols) == 3 and len(set(cols)) == 1
    return ok, f"column depths {cols}"


def check_removals(path, reference):
    """What has been taken out relative to a known-good file. Additions
    are expected; removals almost never are, and every fault tonight
    involved removing something by accident."""
    r = subprocess.run(["diff", reference, path],
                       capture_output=True, text=True)
    removed = [l[2:] for l in r.stdout.split("\n")
               if l.startswith("< ") and l.strip() != "<"]
    return removed


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "lynx_app.py"
    reference = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"\nverifying {path}\n")
    failed = False

    ok, msg = check_python(path)
    print(f"  python           {'OK' if ok else 'FAIL'}   {msg}")
    failed |= not ok

    for n, size, ok, msg in check_scripts(path):
        print(f"  script block {n}   {'OK' if ok else 'FAIL'}   "
              f"{size:>6} chars{'' if ok else '   ' + msg}")
        failed |= not ok

    ok, msg = check_columns(path)
    if ok is None:
        print(f"  config columns   SKIP   {msg}")
    else:
        print(f"  config columns   {'OK' if ok else 'FAIL'}   {msg}")
        failed |= not ok

    if reference:
        removed = check_removals(path, reference)
        if removed:
            print(f"\n  {len(removed)} line(s) REMOVED relative to the reference:")
            for l in removed[:12]:
                print(f"      {l.strip()[:88]}")
            if len(removed) > 12:
                print(f"      ... and {len(removed) - 12} more")
        else:
            print("  removals         none - additions only")

    print("\n  " + ("FAILED" if failed else "all checks passed") + "\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
