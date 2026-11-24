#!/usr/bin/env python3
"""Tests for the Remote Rx "needs updating" decision in lynx_app.py.

Run:  python3 test_slave_staleness.py

Worth having, because this decision is the one thing standing between a
real fix and a Remote Rx in somebody's loft. Get it too eager and every
commit to anything - a QuickLynx tweak, a line in the CHANGELOG - offers
an update that restarts the service and writes to the card's SD for no
benefit. Get it too clever and a genuine fix never prompts anybody and
the receiver stays broken. Neither failure is visible from the panel,
which is why it is tested here instead.

The functions under test are read straight out of lynx_app.py rather
than copied, so this tests what ships. They are run against a git
repository built here commit by commit, so the cases are exact and
nothing depends on this project's own history staying put.
"""

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lynx_app.py")

WANT_FUNCS = ("slave_code_changed", "remote_version_state")
WANT_CONSTS = ("SLAVE_DOC_SUFFIXES", "SLAVE_DOC_PREFIXES",
               "_slave_diff_cache", "SLAVE_DIFF_CACHE_MAX")


def load_from_app():
    """Pull the two functions and their constants out of lynx_app.py.

    lynx_app.py cannot simply be imported: it builds a FastAPI app and
    starts threads at import time. Lifting out the definitions keeps
    this a unit test of the real source, with no server anywhere.
    """
    src = open(APP).read()
    funcs, consts = {}, ""
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            funcs[node.name] = ast.get_source_segment(src, node)
        names = []
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        if set(names) & set(WANT_CONSTS):
            consts += ast.get_source_segment(src, node) + "\n"
    missing = [n for n in WANT_FUNCS if n not in funcs]
    missing += [n for n in WANT_CONSTS if n not in consts]
    if missing:
        raise AssertionError(
            "lynx_app.py no longer defines: " + ", ".join(missing)
            + " - if these were renamed, rename them here too rather than "
              "deleting this test")
    return funcs, consts


class Repo:
    """A throwaway git repository, committed one file at a time."""

    def __init__(self, path):
        self.path = path
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        r = subprocess.run(
            ["git", "-C", self.path,
             "-c", "user.email=test@example.invalid",
             "-c", "user.name=Lynx Test",
             "-c", "commit.gpgsign=false", *args],
            capture_output=True, text=True)
        return (r.returncode == 0,
                (r.stdout or r.stderr).strip())

    def write(self, rel, text):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(text)

    def commit(self, msg, files):
        """files: {"slave/README.md": "text"} - real paths, because the
        decision under test turns on the extension and the directory."""
        for rel, text in files.items():
            self.write(rel, text)
        self.git("add", "-A")
        ok, out = self.git("commit", "-q", "-m", msg)
        assert ok, out
        ok, sha = self.git("rev-parse", "--short", "HEAD")
        assert ok, sha
        return sha


class StalenessTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        funcs, consts = load_from_app()
        cls.funcs, cls.consts = funcs, consts
        cls.dir = tempfile.mkdtemp(prefix="lynx-staleness-")
        r = Repo(cls.dir)

        # A plausible slice of the real tree: code the Remote Rx runs,
        # documentation it only carries, and files that are nothing to
        # do with it at all.
        cls.base = r.commit("initial", {
            "slave/longmynd_source.py": "print('tune')\n",
            "slave/remote_common.py": "VERSION = 1\n",
            "slave/install.sh": "#!/bin/bash\necho install\n",
            "slave/README.md": "# Remote Rx\n",
            "slave/docs/wiring.md": "# Wiring\n",
            "lynx_app.py": "app = None\n",
            "quicklynx.html": "<html></html>\n",
            "CHANGELOG.md": "# Changelog\n"})

        # Deliberately in this order, with the documentation commits
        # LAST, so that "behind by documentation only" exists as a span
        # against the final commit. Each step changes one kind of file,
        # so any consecutive pair isolates exactly one question.
        #
        #   base -> unrelated -> slave_code -> slave_installer
        #                     -> slave_doc -> slave_subdoc (= HEAD)

        # Nothing under slave/ at all.
        cls.unrelated = r.commit("QuickLynx: a one-line fix", {
            "quicklynx.html": "<html><!-- fixed --></html>\n",
            "CHANGELOG.md": "# Changelog\n\n## today\n"})
        # Code it genuinely runs.
        cls.slave_code = r.commit("Remote Rx: hand over to the right tuner", {
            "slave/longmynd_source.py": "print('tune')\nprint('handover')\n"})
        # Carried, but not re-run by pressing Update.
        cls.slave_installer = r.commit("Remote Rx: install the Knucker driver", {
            "slave/install.sh": "#!/bin/bash\necho install\necho knucker\n"})
        # Documentation the card carries but never runs.
        cls.slave_doc = r.commit("Remote Rx guide: clone the right branch", {
            "slave/README.md": "# Remote Rx\n\nClone beta.\n"})
        cls.slave_subdoc = r.commit("Remote Rx: redraw the wiring diagram", {
            "slave/docs/wiring.md": "# Wiring\n\nLOWER F connector.\n"})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def ns(self, local=None):
        """A fresh namespace, so each test starts with an empty cache."""
        calls = []

        def git_cmd(*args, timeout=15):
            calls.append(args)
            r = subprocess.run(["git", "-C", self.dir, *args],
                               capture_output=True, text=True)
            if r.returncode != 0:
                return False, r.stderr.strip()
            return True, r.stdout.strip()

        ns = {"git_cmd": git_cmd, "calls": calls}
        exec(self.consts, ns)
        for name in WANT_FUNCS:
            exec(self.funcs[name], ns)
        if local is not None:
            ns["lynx_local_version"] = lambda: local
        return ns

    # ── what counts as a change the Remote Rx needs ──────────────

    def test_a_commit_touching_nothing_under_slave_is_not_a_change(self):
        """The case that prompted this: a QuickLynx fix and a CHANGELOG
        line left the Remote Rx's commit no longer matching ours, so the
        panel offered an update that would have restarted a working
        receiver and written to its SD card for nothing."""
        ns = self.ns()
        self.assertIs(False,
                      ns["slave_code_changed"](self.base, self.unrelated))

    def test_documentation_under_slave_is_not_a_change(self):
        """The card carries slave/README.md and never executes it."""
        ns = self.ns()
        self.assertIs(False,
                      ns["slave_code_changed"](self.slave_installer, self.slave_doc))
        self.assertIs(False,
                      ns["slave_code_changed"](self.slave_doc, self.slave_subdoc))
        # Both doc commits together, still nothing it runs.
        self.assertIs(False,
                      ns["slave_code_changed"](self.slave_installer, self.slave_subdoc))

    def test_code_it_runs_is_a_change(self):
        ns = self.ns()
        self.assertIs(True,
                      ns["slave_code_changed"](self.unrelated, self.slave_code))

    def test_the_installer_counts_even_though_update_does_not_run_it(self):
        """install.sh, the unit file and the conf example are included
        on purpose. Pressing Update re-runs none of them, so this is
        strictly over-eager - but deciding for ourselves which files
        matter is how a real fix ends up never reaching anybody, and the
        cost of being wrong this way is one restart."""
        ns = self.ns()
        self.assertIs(True,
                      ns["slave_code_changed"](self.slave_code,
                                               self.slave_installer))

    def test_docs_and_unrelated_commits_do_not_hide_a_code_change(self):
        """The realistic case: a Remote Rx several commits behind, where
        most of what happened was documentation and QuickLynx, but one
        commit in the middle changed what it runs. That one must win."""
        ns = self.ns()
        self.assertIs(True,
                      ns["slave_code_changed"](self.base, self.slave_subdoc))

    def test_the_comparison_is_symmetric(self):
        """A receiver rolled back sits BEHIND its Remote Rx. That is
        still a difference in code it runs, and still worth saying."""
        ns = self.ns()
        self.assertIs(True,
                      ns["slave_code_changed"](self.slave_code, self.unrelated))

    # ── not knowing, which must never look like "nothing to do" ──

    def test_a_commit_we_do_not_have_is_unknown_not_unchanged(self):
        """A shallow clone or a Remote Rx ahead of us. None, not False:
        False would silently withhold a real fix."""
        ns = self.ns()
        self.assertIsNone(ns["slave_code_changed"]("0000000", self.slave_code))

    def test_unknown_still_offers_the_update(self):
        ns = self.ns(local=(self.slave_code, "main"))
        ns["remote_states"] = [{"version": "0000000", "branch": "main"}]
        v = ns["remote_version_state"](0)
        self.assertTrue(v["behind"])
        self.assertIsNone(v["slave_changed"])
        self.assertTrue(v["stale"], "an unprovable difference must still prompt")

    # ── the cache, which exists because /api/status polls hard ───

    def test_a_known_answer_is_not_asked_of_git_twice(self):
        """remote_version_state() runs on every status poll for every
        Remote Rx. Without this, each poll spawned a git diff per site."""
        ns = self.ns()
        ns["slave_code_changed"](self.base, self.slave_code)
        before = len(ns["calls"])
        for _ in range(5):
            ns["slave_code_changed"](self.base, self.slave_code)
        self.assertEqual(before, len(ns["calls"]))

    def test_not_knowing_is_never_cached(self):
        """Today's unknown commit is tomorrow's fetched one. Caching
        None would leave the panel permanently wrong about a Remote Rx
        whose commit arrived a minute later."""
        ns = self.ns()
        ns["slave_code_changed"]("0000000", self.slave_code)
        before = len(ns["calls"])
        ns["slave_code_changed"]("0000000", self.slave_code)
        self.assertEqual(before + 1, len(ns["calls"]))

    def test_the_cache_cannot_grow_without_limit(self):
        """This runs for months without a restart."""
        ns = self.ns()
        cap = ns["SLAVE_DIFF_CACHE_MAX"]
        for i in range(cap + 10):
            ns["_slave_diff_cache"][("x%d" % i, "y")] = False
            if len(ns["_slave_diff_cache"]) >= cap:
                ns["slave_code_changed"](self.base, self.unrelated)
        self.assertLessEqual(len(ns["_slave_diff_cache"]), cap)

    # ── the whole decision, as the panel sees it ─────────────────

    def test_the_panel_decision_end_to_end(self):
        ours = self.slave_subdoc        # the tip of the synthetic history
        cases = [
            # (what the Remote Rx reports, branch, behind, stale)
            ("in step",                 ours,                 "main", False, False),
            ("edited on the card",      ours + "+",           "main", False, False),
            # Spans the whole history, code included, so this one must
            # prompt - it is here to prove the filter is not simply
            # answering False to everything.
            ("behind by everything",    self.base,            "main", True,  True),
            ("behind by code it runs",  self.unrelated,       "main", True,  True),
            ("behind by the installer", self.slave_code,      "main", True,  True),
            ("behind by documentation only",
             self.slave_installer,      "main", True,  False),
            ("held on another branch",  self.base,            "beta", False, False),
            ("version not reported",    "-",                  "-",    False, False),
        ]
        for why, theirs, branch, behind, stale in cases:
            with self.subTest(why):
                ns = self.ns(local=(ours, "main"))
                ns["remote_states"] = [{"version": theirs, "branch": branch}]
                v = ns["remote_version_state"](0)
                self.assertEqual(behind, v["behind"], f"{why}: behind")
                self.assertEqual(stale, v["stale"], f"{why}: stale")

    def test_the_case_that_prompted_this(self):
        """Justin's own observation: pull a QuickLynx fix onto the
        receiver and the Remote Rx was offered an update too. One
        commit, nothing under slave/, and the Zero would have fetched,
        checked out, restarted and sat on probation for two minutes -
        every bit of it written to an SD card in a loft."""
        ns = self.ns(local=(self.unrelated, "main"))
        ns["remote_states"] = [{"version": self.base, "branch": "main"}]
        v = ns["remote_version_state"](0)
        self.assertTrue(v["behind"], "it IS on a different commit")
        self.assertIs(False, v["slave_changed"])
        self.assertFalse(v["stale"], "but no button, and no write to the card")

    def test_behind_on_documentation_is_still_reported_as_behind(self):
        """Not silently equal. The operator can see the Remote Rx is not
        on this commit AND that there is no reason to act on it."""
        ns = self.ns(local=(self.slave_subdoc, "main"))
        ns["remote_states"] = [{"version": self.slave_doc, "branch": "main"}]
        v = ns["remote_version_state"](0)
        self.assertTrue(v["behind"])
        self.assertFalse(v["stale"])

    def test_stale_is_never_true_without_behind(self):
        """Belt and braces: the button must not appear on a Remote Rx
        that is on our exact commit, whatever git says."""
        ns = self.ns(local=(self.slave_code, "main"))
        ns["remote_states"] = [{"version": self.slave_code, "branch": "main"}]
        v = ns["remote_version_state"](0)
        self.assertFalse(v["behind"])
        self.assertFalse(v["stale"])
        self.assertIs(False, v["slave_changed"])


if __name__ == "__main__":
    if not shutil.which("git"):
        print("git is not installed; nothing to test against")
        sys.exit(0)
    unittest.main(verbosity=2)
