# Red Button discipline

Rules for changing anything that ships to a Remote Rx.

A Remote Rx lives in somebody's loft, behind somebody else's Wi-Fi,
with a ladder between it and its owner. One is an afternoon's
inconvenience. Twenty is a project that has stopped being fun, and the
failures do not arrive one at a time: A change that breaks updating
breaks it on every unit at once, and leaves no way to send the fix.

Every rule below was paid for. The date says when.

---

## 1. If it can change, it must live in the git checkout

The update mechanism is `git fetch` and `git checkout` inside
`~/lynx-datv-receiver`. Anything installed anywhere else cannot be
changed remotely, ever.

`install.sh` used to copy the start-up wrapper to
`/usr/local/bin/lynx-slave`. Updates changed the Python and never the
wrapper, so the first change that needed a different start-up could not
reach a single existing unit. `/usr/local/bin/lynx-slave` is now a
nine-line launcher that hands over to the checkout, and it is written
to never need changing again. (23 Nov 2026)

**Before adding a file outside the checkout, ask how it gets fixed when
it is wrong.** If the answer involves ssh, it is the wrong design.

## 2. Recovery must not depend on the thing being recovered

An update exists to fix a broken unit. So nothing in the update path
may require the broken part to be working.

- The heartbeat runs on its own thread, independent of the tuner. A
  Remote Rx with no aerial still reports in.
- The update command rides back on that heartbeat, so nothing needs a
  port open at the remote site.
- The Update button must be reachable whatever the receiver is doing.

That last one was false for a month. The button lived inside a panel
body that is hidden unless the receiver is locked, so the only unit
that could be updated was one already working, and a Remote Rx that was
actually broken could not be updated at all. It is in the panel header
now. (23 Nov 2026)

## 3. Nothing in an update may need root

The service runs as `pi`. An update is `git` plus a restart, and that
is the whole budget. Anything needing `sudo` - a package, a udev rule,
a systemd unit, a file in `/etc` - cannot happen remotely and means a
visit.

This is why `install.sh` cannot be run from the updater, and why
changing tuner had to be solved inside the Python instead.

## 4. Both ends update separately, so both orders must work

The repeater and each Remote Rx are updated by different people at
different times. Every change to what passes between them has to
survive both orders, for at least one release:

- **New Remote Rx, old repeater:** New heartbeat lines must be ignored
  harmlessly. New quality tags must be unrecognised rather than
  misread - which is why the status format is tagged and not a column
  table.
- **New repeater, old Remote Rx:** Anything the old unit does not send
  must have a defined default. No `TUN` line means a Minitiouner,
  because every Remote Rx built before that line existed is one.

**Write the answer to both in the commit message.** If you cannot, the
change is not ready.

## 5. The gates test what actually runs

Before switching: every `.py` compiles, the wrapper parses with
`bash -n`, and every source module starts with `--help`. After
switching: probation, three starts within two minutes, then rollback.

Gate 2 tested only `longmynd_source.py` for a while. A Knucker unit was
proving that the code it does not run would start, and would have
passed an update that broke the code it does. It now tests every source
module. (23 Nov 2026)

A module that is missing is **skipped, not failed**: an older commit
has no `knucker_source.py`, and a rollback to it must not be refused
for lacking a file that did not exist yet.

## 6. Every new code path must tolerate being rolled back

Rollback goes to a commit that knows nothing about the change being
rolled back. So a new file must be optional, a new config key must have
a default, and new behaviour must degrade to the old behaviour rather
than error.

Test by asking: what happens when this runs against the commit before
it, and what happens when the commit before it runs against the state
this one left behind?

## 7. Fail soft, and never off the air

`Restart=always` with no start limit, so no fault parks a unit in a
failed state. Everything in the start-up path follows the same rule:

- Tuner detection that cannot run assumes a Minitiouner.
- A wrapper that cannot read something carries on rather than exiting.
- A parser fault costs information, never the picture - the transport
  stream tap forwards first and parses second.

A Remote Rx that exits because something was not quite right is a
Remote Rx somebody has to visit.

## 8. Silence is not failure

The updater does not treat "the repeater stopped hearing from it" as a
failed update. A unit that updates perfectly and then loses its
internet would otherwise roll back repeatedly for something that was
never the update's fault. **Crashing is the signal. Silence is not.**

## 9. Never update a checkout somebody has edited

`git status --porcelain --untracked-files=no` is checked first, and a
dirty checkout refuses the update. Overwriting another operator's work
on a machine they may not be able to reach is not ours to do.

(Untracked files are ignored deliberately: `__pycache__` appears beside
the source and counting it made every Remote Rx refuse every update.)

## 10. Prove it on hardware before it reaches beta

Experiments go on `beta-wip`. `beta` is what other people run, and
their time is not ours to spend. The test is not "the tests pass", it
is "this ran on a real Remote Rx and did the thing".

Where that is impossible - a tuner is off the bench, a case cannot be
reproduced - say so in the commit message rather than letting silence
imply it was proven.

## 11. The button must mean something

An update is not free. It fetches, checks out, restarts the service and
sits on probation for two minutes, and every bit of that is a write
cycle on an SD card in a loft, plus an interruption to whatever the
receiver was showing.

A Remote Rx holds a checkout of this whole repository, so its commit
stops matching the repeater's after **any** change at all: a QuickLynx
fix, a line in this file. For a while the Update button appeared for
all of them, so pressing it was usually a restart in exchange for
nothing - and a button that is usually noise is a button that gets
ignored on the day it matters.

`remote_version_state()` now separates two questions. `behind` is the
plain fact that the commits differ, and is still reported. `stale` is
`behind` **and** a change to something under `slave/` that the unit
actually runs, and that is what the button switches on. Documentation
under `slave/` does not count. (24 Nov 2026)

Two rules follow:

- **When git cannot answer, offer the update.** An unknown commit or a
  shallow clone gives `None`, which counts as stale. Never withhold a
  real fix because it could not be proved to be one.
- **Do not narrow it further by guessing.** `install.sh`, the unit file
  and the conf example all count, even though pressing Update re-runs
  none of them. Deciding for ourselves which files really matter is how
  a genuine fix ends up never prompting anybody, and the cost of being
  wrong the generous way is one restart.

**The trap: this comparison looks only under `slave/`.** Every module a
Remote Rx runs lives there today, and every import in `slave/*.py` is
either the standard library or another file in `slave/`. The day
anything a Remote Rx executes is added outside that directory, this
comparison goes quietly blind to changes in it and the button stops
appearing for real fixes. Keep the unit self-contained, or teach
`slave_code_changed()` about the new path in the same commit.

Tested in `test_slave_staleness.py`, against a git repository the test
builds commit by commit, because neither failure here is visible from
the panel.

---

## The question to ask before pressing push

> If this change is wrong, how does the fix reach a Remote Rx in
> somebody's loft?

If there is no answer that does not involve a ladder, the change is not
finished.
