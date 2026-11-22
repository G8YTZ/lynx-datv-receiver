#!/usr/bin/env python3
"""
Fixes the indentation bug in ensure_current_version() - a stray
print() line left at the wrong indentation by the earlier patch,
which never knew that line existed (it was cut off in the grep
output used to build that patch).

Run this directly on the Pi5, in /home/pi/lynx/.
"""
import sys

PATH = "lynx_app.py"

with open(PATH) as f:
    content = f.read()

broken = '''    if update_state["current_version"] is None:
        try:
            update_state["current_version"] = detect_current_version()
        except Exception as e:
            update_state["current_version"] = "unknown"
    if update_state["channel"] is None:
        update_state["channel"] = config.get('update', {}).get('channel', 'stable')
            print(f"[update-check] version detection failed unexpectedly: {e}")'''

fixed = '''    if update_state["current_version"] is None:
        try:
            update_state["current_version"] = detect_current_version()
        except Exception as e:
            update_state["current_version"] = "unknown"
            print(f"[update-check] version detection failed unexpectedly: {e}")
    if update_state["channel"] is None:
        update_state["channel"] = config.get('update', {}).get('channel', 'stable')'''

count = content.count(broken)
if count != 1:
    print(f"FAILED: expected exactly 1 match, found {count} - stopping, nothing changed.")
    sys.exit(1)

content = content.replace(broken, fixed)

with open(PATH, "w") as f:
    f.write(content)

print("Fixed. Verifying syntax now...")

import ast
try:
    ast.parse(open(PATH).read())
    print("Syntax OK - safe to restart Lynx.")
except SyntaxError as e:
    print(f"STILL BROKEN: {e}")
    sys.exit(1)
