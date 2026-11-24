#!/usr/bin/env python3
"""Tests for slave/lynx-slave: which source module starts, and with what.

Run:  python3 test_wrapper_routing.py

This is the junction every Remote Rx passes through at every start. Get
it wrong and a card either drives the wrong tuner or drives nothing,
and in both cases the journal looks plausible. It is nine lines of bash
nobody will ever read again, which is exactly why it is tested.

The real wrapper is executed, against stub source modules that announce
their own argv. Nothing is mocked except the things it is supposed to
find: the conf, the slave directory, the tuner detection, and whether a
PicoTuner longmynd has been built.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WRAPPER = os.path.join(HERE, "slave", "lynx-slave")
MODULES = ("longmynd_source.py", "knucker_source.py", "picotuner_source.py")


class RoutingTests(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lynx-wrapper-")
        self.slave = os.path.join(self.root, "slave")
        os.makedirs(self.slave)
        self.conf = os.path.join(self.root, "lynx-slave.conf")
        with open(self.conf, "w") as f:
            f.write('LYNX_HOST=192.168.0.106\nFREQ=437000\nSR=1000\n'
                    'NAME="Crystal Palace"\nLOCATOR=JO01IF\n')
        for m in MODULES:
            with open(os.path.join(self.slave, m), "w") as f:
                f.write("import sys\nprint('RAN', %r, ' '.join(sys.argv[1:]))\n"
                        % m)
        self.pico = os.path.join(self.root, "longmynd-pico")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def build_pico_longmynd(self):
        os.makedirs(self.pico, exist_ok=True)
        b = os.path.join(self.pico, "longmynd")
        with open(b, "w") as f:
            f.write("#!/bin/bash\nexit 0\n")
        os.chmod(b, 0o755)

    def run_wrapper(self, detected=None, forced=None):
        """Returns (module, args, stderr)."""
        with open(os.path.join(self.slave, "detect_tuner.py"), "w") as f:
            f.write("import sys\nprint(%r)\n" % (detected or "minitiouner"))
        if forced:
            with open(self.conf, "a") as f:
                f.write("TUNER=%s\n" % forced)
        with open(WRAPPER) as f:
            src = f.read()
        src = src.replace("/etc/lynx-slave.conf", self.conf)
        src = src.replace(
            'SLAVE_DIR="${LYNX_SLAVE_DIR:-/home/pi/lynx-datv-receiver/slave}"',
            'SLAVE_DIR="%s"' % self.slave)
        w = os.path.join(self.root, "wrapper")
        with open(w, "w") as f:
            f.write(src)
        os.chmod(w, 0o755)
        r = subprocess.run(["bash", w], capture_output=True, text=True,
                           env=dict(os.environ, PICOTUNER_DIR=self.pico))
        ran = [l for l in r.stdout.split("\n") if l.startswith("RAN ")]
        self.assertTrue(ran, "the wrapper started nothing. stderr:\n" + r.stderr)
        _, module, args = ran[0].split(" ", 2)
        return module, args, r.stderr

    # ── the ordinary tuners ──────────────────────────────────────

    def test_a_minitiouner_runs_longmynd_with_no_extras(self):
        """The default path, and it must stay bare: a --tuner-kind here
        would be a second place for the answer to be wrong."""
        module, args, _ = self.run_wrapper(detected="minitiouner")
        self.assertEqual("longmynd_source.py", module)
        self.assertNotIn("--tuner-kind", args)
        self.assertNotIn("--longmynd-dir", args)

    def test_a_knucker_runs_its_own_source(self):
        module, _, _ = self.run_wrapper(detected="knucker")
        self.assertEqual("knucker_source.py", module)

    def test_an_unknown_kind_falls_back_to_longmynd(self):
        """Rule 7: every Remote Rx built so far is a Minitiouner, so a
        kind nothing recognises must keep a working site on the air
        rather than refusing to start."""
        module, _, _ = self.run_wrapper(detected="something-new")
        self.assertEqual("longmynd_source.py", module)

    # ── a PicoTuner, which goes two ways ─────────────────────────

    def test_a_picotuner_with_its_longmynd_is_driven(self):
        self.build_pico_longmynd()
        module, args, _ = self.run_wrapper(detected="picotuner")
        self.assertEqual("longmynd_source.py", module)
        self.assertIn("--tuner-kind picotuner", args)
        self.assertIn("--longmynd-dir %s" % self.pico, args)

    def test_a_picotuner_without_its_longmynd_reports_only(self):
        """The card G8YTZ had tonight. It must NOT start longmynd: that
        is the five-second restart loop, writing to an SD card up a
        ladder, achieving nothing."""
        module, _, err = self.run_wrapper(detected="picotuner")
        self.assertEqual("picotuner_source.py", module)
        self.assertIn("--picotuner", err,
                      "the journal should say how to fix it")

    def test_a_half_built_pico_directory_is_not_mistaken_for_a_build(self):
        """A directory with no binary in it, which is what an
        interrupted install leaves behind."""
        os.makedirs(self.pico, exist_ok=True)
        module, _, _ = self.run_wrapper(detected="picotuner")
        self.assertEqual("picotuner_source.py", module)

    def test_a_non_executable_pico_longmynd_is_not_a_build(self):
        """-x not -f: a binary that cannot be run is not a driver, and
        this is the shape a failed `make` leaves."""
        os.makedirs(self.pico, exist_ok=True)
        b = os.path.join(self.pico, "longmynd")
        with open(b, "w") as f:
            f.write("not built\n")
        os.chmod(b, 0o644)
        module, _, _ = self.run_wrapper(detected="picotuner")
        self.assertEqual("picotuner_source.py", module)

    # ── the operator's override ──────────────────────────────────

    def test_tuner_in_the_conf_overrides_detection(self):
        """TUNER= exists for a tuner detection has not met. It must beat
        detection, and must say so, or an operator who set it has no way
        to know whether it took."""
        module, _, err = self.run_wrapper(detected="minitiouner",
                                          forced="knucker")
        self.assertEqual("knucker_source.py", module)
        self.assertIn("forced", err.lower())

    def test_the_override_can_force_a_picotuner_too(self):
        self.build_pico_longmynd()
        module, args, _ = self.run_wrapper(detected="minitiouner",
                                           forced="picotuner")
        self.assertEqual("longmynd_source.py", module)
        self.assertIn("--tuner-kind picotuner", args)

    # ── what every path must carry ───────────────────────────────

    def test_the_conf_settings_reach_every_source(self):
        """A routing change that quietly dropped --name or --locator
        would show up as a Remote Rx with no label on the panel."""
        for detected, build in (("minitiouner", False), ("knucker", False),
                                ("picotuner", True), ("picotuner", False)):
            with self.subTest(detected=detected, pico_build=build):
                shutil.rmtree(self.pico, ignore_errors=True)
                if build:
                    self.build_pico_longmynd()
                _, args, _ = self.run_wrapper(detected=detected)
                self.assertIn("192.168.0.106", args)
                self.assertIn("--freq 437000", args)
                self.assertIn("--sr 1000", args)
                self.assertIn("--name Crystal Palace", args)
                self.assertIn("--locator JO01IF", args)

    # ── INPUT=, which only one source understands ────────────────

    def test_input_reaches_a_longmynd_source(self):
        with open(self.conf, "a") as f:
            f.write("INPUT=bottom\n")
        _, args, _ = self.run_wrapper(detected="minitiouner")
        self.assertIn("--input bottom", args)

    def test_input_reaches_a_driven_picotuner(self):
        """Both Minitiouner and PicoTuner boards have two F connectors."""
        self.build_pico_longmynd()
        with open(self.conf, "a") as f:
            f.write("INPUT=bottom\n")
        module, args, _ = self.run_wrapper(detected="picotuner")
        self.assertEqual("longmynd_source.py", module)
        self.assertIn("--input bottom", args)

    def test_input_is_not_passed_to_a_source_that_has_no_such_option(self):
        """A Knucker takes its input differently and its source has no
        --input. Passing it would stop that card starting AT ALL, which
        is a far worse fault than the one INPUT= was added to fix."""
        with open(self.conf, "a") as f:
            f.write("INPUT=bottom\n")
        for detected, want in (("knucker", "knucker_source.py"),
                               ("picotuner", "picotuner_source.py")):
            with self.subTest(detected):
                module, args, _ = self.run_wrapper(detected=detected)
                self.assertEqual(want, module)
                self.assertNotIn("--input", args)

    def test_detection_is_logged_with_what_it_saw(self):
        """The -v run, added because a card with an unrecognised tuner
        logged the same cheerful line as a working one."""
        _, _, err = self.run_wrapper(detected="picotuner")
        self.assertIn("Tuner detected: picotuner", err)


if __name__ == "__main__":
    if not os.path.exists(WRAPPER):
        print("slave/lynx-slave not found")
        sys.exit(1)
    unittest.main(verbosity=2)
