#!/usr/bin/env python3
"""
resolve_atsc3_merge.py

Resolves the two conflicts left by `git merge origin/atsc3` on beta-wip
(after reverting the two HDHomeRun commits of 24 Oct 2026):

  1. The receiver page's HDHomeRun panel. beta drew one DVB-T2 panel
     inline and had since added the service list and the frequency box
     that follows the lock; atsc3 replaced the inline code with
     renderHdhrPanel(), one panel per kind of box (DVB-T2, ATSC 3.0).
     atsc3's version is kept, and beta's service-list code moves into
     renderHdhrPanel() for the DVB-T2 panel.
  2. Two blocks of new functions side by side (beta's DVB-S2 programme
     selector, atsc3's modulation helpers): both kept.

Run from the repo root, with the merge in progress:
    python3 resolve_atsc3_merge.py && python3 verify.py lynx_app.py
"""

import re
import sys
from pathlib import Path

p = Path("lynx_app.py")
s = p.read_text()
blocks = list(re.finditer(r'^<<<<<<< [^\n]*\n(.*?)^=======\n(.*?)^>>>>>>> [^\n]*\n',
                          s, re.S | re.M))
if len(blocks) != 2:
    sys.exit(f"ABORT: expected 2 conflicts in lynx_app.py, found {len(blocks)}")
b1, b2 = blocks
if "renderHdhrPanel(hdvb" not in b1.group(2) or "function dvbtModulation" not in b2.group(2):
    sys.exit("ABORT: these are not the conflicts this script knows")

out = (s[:b1.start()] + b1.group(2) + s[b1.end():b2.start()]
       + b2.group(1) + b2.group(2) + s[b2.end():])

anchor = "    if (lm && prefix === 'dvbt') { window._dvbtLockMode = lm; }\n"
if out.count(anchor) != 1:
    sys.exit("ABORT: renderHdhrPanel() not as expected")
out = out.replace(anchor, anchor + """    // Service list: see patch_dvbt_service_list.py. Once per multiplex,
    // whoever tuned it, so a refresh or a tune from elsewhere still fills
    // it. The frequency box follows the multiplex actually locked, so
    // changing service retunes the right one (patch_rock_codecs_dvbt_freq.py),
    // but never while somebody is typing in it.
    if (prefix === 'dvbt') {
        window._dvbtServiceName = hh.service_name || '';
        var fkey = hh.frequency_hz || 0;
        var stale = window._dvbtProgramsFor !== fkey;
        var retry = window._dvbtProgramsEmptyAt &&
                    (Date.now() - window._dvbtProgramsEmptyAt > 10000);
        if (fkey && (stale || retry) && !window._dvbtProgramsBusy) {
            window._dvbtProgramsFor = fkey;
            if (stale) {
                var fbox = document.getElementById('dvbt-freq');
                if (fbox && document.activeElement !== fbox) {
                    fbox.value = Math.round(fkey / 1000);
                }
            }
            window._dvbtProgramsBusy = true;
            loadDvbtPrograms(8).finally(function () {
                window._dvbtProgramsBusy = false;
            });
        }
    }
""")
p.write_text(out)
print("  ok  both conflicts resolved; service list carried into renderHdhrPanel()")
