#!/usr/bin/env python3
"""Updating a Remote Rx from the repeater, safely.

A Remote Rx lives in somebody's loft, behind somebody else's Wi-Fi,
with no open ports and quite possibly a ladder between it and its
owner. So the only interesting question here is not how to update it;
it is what happens when an update is bad.

WHY THERE IS A ROLLBACK AT ALL
------------------------------
lynx-slave.service runs with Restart=always and StartLimitIntervalSec=0,
set deliberately so a remote unit never parks itself in a failed state
and goes off the air until somebody visits. The other edge of that is
that a bad update gives a unit restarting broken code for ever, just as
unreachable, and now with no way in. Rollback is what makes the restart
policy safe rather than dangerous.

THE THREE GATES
---------------
An update has to get past all of these, in order:

  1. Every Python file compiles. Catches a syntax error.
  2. The program starts: it is run with --help, which exercises every
     import and the argument parser. Catches the broken import that a
     compile check sails straight past, and that would otherwise kill
     the service before any of this code ran.
  3. It survives. The new version is on probation until it has run for
     UPDATE_CONFIRM_SECS without the process dying. Crash on the third
     attempt and the previous commit is restored.

Gates 1 and 2 happen before anything is switched, so a bad update is
usually refused without the Remote Rx ever going off the air. Gate 3 is
the net under the cases they cannot see.

WHAT THIS DELIBERATELY DOES NOT DO
----------------------------------
It does not treat "the repeater stopped hearing from it" as a failed
update. A Remote Rx that updates fine and then loses its internet would
roll back for no reason, repeatedly, on a link that was never the
update's fault. Crashing is the signal; silence is not.
"""

import hashlib
import hmac
import json
import os
import subprocess
import time

UPDATE_MAGIC = "LYNXUPD1"
DEFAULT_UPDATE_FILE = "/var/lib/lynx-slave/update.json"

# How long a new version runs before it is taken off probation. Long
# enough to get past start-up and a first tune, short enough that a
# watching owner is not left wondering.
UPDATE_CONFIRM_SECS = 120.0

# Starts of an unconfirmed version before it is given up on. Three
# rather than one: a single failure to start can be something else
# entirely (the card, a power blip during boot), and rolling back on
# that would undo a good update for an unrelated reason.
UPDATE_MAX_ATTEMPTS = 3

GIT_TIMEOUT_SECS = 120

# Start-tested by gate 2. One that is missing is skipped rather than
# failed: an older commit has no knucker_source.py, and a rollback TO
# that commit must not be refused for not having a file that did not
# exist yet.
SOURCE_MODULES = ("longmynd_source.py", "knucker_source.py")


def git(repo, *args, timeout=GIT_TIMEOUT_SECS):
    """(ok, output). Never raises: every caller here is running
    unattended at the far end of a link nobody can reach."""
    env = os.environ.copy()
    # No terminal to prompt on, so fail fast rather than hang for ever
    # waiting for credentials that are not coming.
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        r = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                           text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or r.stdout).strip() or f"git {args[0]} failed"
    return True, r.stdout.strip()


def current_commit(repo):
    ok, out = git(repo, "rev-parse", "HEAD")
    return out if ok else ""


def parse_command_packet(data: bytes, key: str):
    """An update command from the repeater, or None.

    Same shape and the same HMAC as the settings packet, under its own
    magic so neither can ever be mistaken for the other. Carries a
    version like the settings do, so a captured packet cannot be
    replayed later to force an unwanted update.
    """
    try:
        magic, body, sig = data.decode().split("\n")[:3]
    except (UnicodeDecodeError, ValueError):
        return None
    if magic != UPDATE_MAGIC:
        return None
    want = hmac.new(key.encode(), (magic + "\n" + body).encode(),
                    hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig.strip()):
        return None
    try:
        cmd = json.loads(body)
        cmd["v"] = int(cmd["v"])
    except (ValueError, KeyError, TypeError):
        return None
    if cmd.get("cmd") != "update":
        return None
    # A branch name, not an arbitrary ref: this is fetched and checked
    # out on a machine nobody can reach, so keep the surface small.
    ref = str(cmd.get("ref") or "").strip()
    if not ref or not all(c.isalnum() or c in "-_./" for c in ref) or ".." in ref:
        return None
    cmd["ref"] = ref
    return cmd


def load_state(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, data):
    """Atomic, like the settings file: a power cut leaves the old file or
    the new one, never half of one. A half-written rollback marker is
    the one thing that must not happen."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def clear_state(path):
    for p in (path, path + ".tmp"):
        try:
            os.remove(p)
        except OSError:
            pass


def smoke_test(slave_dir, log=print):
    """Gates 1 and 2: everything compiles, and the program starts.

    --help is deliberate: it runs every import and builds the argument
    parser, then exits. A missing module or a typo at module level
    fails here, on the old code's watch, rather than at the next start
    when there is nothing left to catch it.
    """
    pys = sorted(f for f in os.listdir(slave_dir) if f.endswith(".py"))
    for name in pys:
        try:
            subprocess.run(["python3", "-m", "py_compile",
                            os.path.join(slave_dir, name)],
                           capture_output=True, text=True, timeout=60,
                           check=True)
        except subprocess.CalledProcessError as e:
            return False, f"{name} does not compile: {(e.stderr or '').strip()[:200]}"
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"could not compile {name}: {e}"

    # The start-up wrapper is in the update path too, now that
    # /usr/local/bin/lynx-slave is only a launcher that hands over to
    # it. A bash syntax error in it would take the site off the air with
    # nothing in Python to catch it, so it gets the same treatment as
    # the .py files: checked, not run.
    wrapper = os.path.join(slave_dir, "lynx-slave")
    if os.path.exists(wrapper):
        try:
            subprocess.run(["bash", "-n", wrapper], capture_output=True,
                           text=True, timeout=60, check=True)
        except subprocess.CalledProcessError as e:
            return False, f"lynx-slave does not parse: {(e.stderr or '').strip()[:200]}"
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"could not check lynx-slave: {e}"

    # Every source module, not just the one this unit happens to run.
    # A Knucker unit that only start-tested the Minitiouner source
    # would pass an update that breaks the code it actually runs, and
    # the gate would have looked the other way while it did.
    for name in SOURCE_MODULES:
        main = os.path.join(slave_dir, name)
        if not os.path.exists(main):
            continue
        try:
            r = subprocess.run(["python3", main, "--help"], capture_output=True,
                               text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"{name} would not start: {e}"
        if r.returncode != 0:
            return False, (f"{name} would not start: "
                           f"{(r.stderr or r.stdout).strip()[:200]}")
    return True, f"{len(pys)} files compile and the program starts"


def perform_update(slave_dir, repo, ref, state_path, log=print):
    """Fetch and switch to ref, if it passes the gates.

    Returns (ok, message, restart_needed). Nothing is switched unless
    the new code compiles and starts, so a refused update leaves the
    Remote Rx running exactly as it was.
    """
    prev = current_commit(repo)
    if not prev:
        return False, "not a git checkout: nothing to update", False

    # Tracked files only, so a stray file somebody left in the
    # directory does not read as "they edited this".
    #
    # The first version of this comment claimed that __pycache__ made
    # every Remote Rx refuse every update. That was wrong: the
    # repository ignores __pycache__, and the refusal came from a test
    # fixture built without a .gitignore. The check is still right for
    # the reason above; the reasoning was not.
    dirty_ok, dirty = git(repo, "status", "--porcelain",
                          "--untracked-files=no")
    if dirty_ok and dirty:
        # Somebody edited this Remote Rx in place. Overwriting their
        # work unasked, on a machine they may not be able to reach, is
        # not ours to do.
        return False, "refusing: the checkout has local changes", False

    ok, out = git(repo, "fetch", "--depth", "50", "origin", ref)
    if not ok:
        return False, f"fetch failed: {out}", False

    ok, target = git(repo, "rev-parse", "FETCH_HEAD")
    if not ok:
        return False, f"cannot resolve {ref}: {target}", False
    if target == prev:
        return True, "already up to date", False

    # Stay ON the branch, not on a detached HEAD. Detaching worked
    # once and then disabled itself: the Remote Rx reported its branch
    # as "HEAD", the repeater stopped seeing a branch it could compare
    # against, and the Update button never came back. It also broke the
    # documented "git pull" that people use by hand.
    ok, out = git(repo, "checkout", "--force", "-B", ref, target)
    if not ok:
        return False, f"checkout failed: {out}", False

    good, why = smoke_test(slave_dir, log=log)
    if not good:
        log(f"[update] rejected: {why}")
        rb_ok, rb = git(repo, "checkout", "--force", "-B", ref, prev)
        if not rb_ok:
            # Should not happen: prev is a commit we were just sitting
            # on. Say so loudly rather than pretend it is fine.
            log(f"[update] COULD NOT RESTORE {prev[:7]}: {rb}")
            return False, f"rejected ({why}), and restoring failed: {rb}", False
        log(f"[update] restored {prev[:7]}, still running the old version")
        return False, f"rejected: {why}", False

    # On probation from here: the marker is what the next start reads.
    save_state(state_path, {
        "prev": prev,
        "new": target,
        "ref": ref,
        "attempts": 0,
        "confirmed": False,
        "at": time.time(),
    })
    log(f"[update] {prev[:7]} -> {target[:7]} on {ref}: {why}; restarting")
    return True, f"updated to {target[:7]}", True


def startup_check(repo, state_path, log=print):
    """Called at every start. Returns (rolled_back, message or None).

    An unconfirmed marker means the last start was a new version that
    has not yet proved itself. Each start bumps the count; past
    UPDATE_MAX_ATTEMPTS the previous commit is restored. The caller
    exits on a rollback so systemd starts it again on the old code.
    """
    st = load_state(state_path)
    if not st or st.get("confirmed"):
        return False, None

    attempts = int(st.get("attempts") or 0) + 1
    st["attempts"] = attempts
    prev, new = st.get("prev"), st.get("new")

    if attempts > UPDATE_MAX_ATTEMPTS:
        log(f"[update] {str(new)[:7]} failed to stay up over {attempts - 1} "
            f"attempts: restoring {str(prev)[:7]}")
        ok, out = git(repo, "checkout", "--force", "-B",
                      st.get("ref") or "beta", prev)
        clear_state(state_path)
        if not ok:
            log(f"[update] COULD NOT RESTORE {str(prev)[:7]}: {out}")
            return False, f"rollback failed: {out}"
        return True, f"rolled back to {str(prev)[:7]}"

    save_state(state_path, st)
    log(f"[update] {str(new)[:7]} on probation, start {attempts} of "
        f"{UPDATE_MAX_ATTEMPTS}: confirming in {UPDATE_CONFIRM_SECS:.0f}s")
    return False, None


def confirm_after(state_path, secs=UPDATE_CONFIRM_SECS, log=print,
                  stop=None):
    """Take the running version off probation once it has survived.

    Meant to be run on its own daemon thread. Does nothing if there is
    no marker, so it is harmless to start unconditionally.
    """
    st = load_state(state_path)
    if not st or st.get("confirmed"):
        return
    deadline = time.time() + secs
    while time.time() < deadline:
        if stop is not None and stop.is_set():
            return
        time.sleep(1.0)
    st = load_state(state_path)
    if not st or st.get("confirmed"):
        return
    clear_state(state_path)
    log(f"[update] {str(st.get('new'))[:7]} confirmed after "
        f"{secs:.0f}s: no longer on probation")
