#!/usr/bin/env python3
"""Tests for slave/detect_tuner.py - which tuner is on the USB cable.

Run:  python3 test_detect_tuner.py

Worth having rather than testing on hardware, because the hardware
cannot exercise most of it. Proving this on the bench needs three
tuners, and the cases that matter are the awkward combinations: two
plugged in at once, a tuner nobody recognises, a card with nothing on
it at all. Each decides which source module a Remote Rx in a loft
starts, and getting it wrong looks exactly like a signal problem.

sysfs is built here as real directories, in the layout the kernel
uses, so a failure means the code disagrees with the kernel rather
than with a mock.

Real IDs, for the record:
    0403:6010  FTDI FT2232H    Minitiouner and Knucker both
    2e8a:ba2c  BATC PicoTuner  (USB, RP2040) - read off G8YTZ's own
                               card, 24 Nov 2026
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, "slave")
import detect_tuner          # noqa: E402


class Sysfs:
    """A throwaway /sys/bus/usb/devices tree."""

    def __init__(self, root):
        self.root = root
        self.base = os.path.join(root, "sys", "bus", "usb", "devices")
        os.makedirs(self.base, exist_ok=True)
        self.n = 0

    def add(self, name=None, vendor=None, product_id=None, product=None):
        """One USB device. Any attribute left None is simply absent,
        which is the real case for a hub or a root port."""
        self.n += 1
        d = os.path.join(self.base, name or f"1-{self.n}")
        os.makedirs(d, exist_ok=True)
        for attr, value in (("idVendor", vendor),
                            ("idProduct", product_id),
                            ("product", product)):
            if value is not None:
                with open(os.path.join(d, attr), "w") as f:
                    f.write(value + "\n")
        return self

    # The three tuners, as they actually present. The strings are the
    # ones read off G8YTZ's own cards, not invented.
    def minitiouner(self, product="USB <-> NIM tuner"):
        return self.add(vendor="0403", product_id="6010", product=product)

    def knucker(self):
        return self.add(vendor="0403", product_id="6010",
                        product="CombiTuner-Express")

    def picotuner(self):
        return self.add(vendor="2e8a", product_id="ba2c",
                        product="BATC PicoTuner")

    def hub(self):
        return self.add(name="usb1", product="DWC OTG Controller")


class DetectTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lynx-sysfs-")
        self.fs = Sysfs(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def detect(self):
        return detect_tuner.detect(root=self.dir)

    # ── one tuner at a time ──────────────────────────────────────

    def test_a_minitiouner(self):
        """The real string is "USB <-> NIM tuner" - it does not contain
        the word Minitiouner at all, which is why this was matching via
        the FTDI fallback rather than by name until 24 Nov 2026."""
        self.fs.hub().minitiouner()
        self.assertEqual(("minitiouner", "USB <-> NIM tuner", True),
                         self.detect())

    def test_a_minitiouner_is_matched_by_name_not_by_the_fallback(self):
        """Prove it is the signature doing the work now. With the FTDI
        IDs absent, only the product string can identify it - so this
        fails if "nim tuner" is ever dropped from SIGNATURES."""
        self.fs.hub().add(vendor="1234", product_id="5678",
                          product="USB <-> NIM tuner")
        self.assertEqual("minitiouner", self.detect()[0])

    def test_a_board_that_does_say_minitiouner(self):
        self.fs.hub().minitiouner(product="BATC MiniTiouner v2")
        self.assertEqual(("minitiouner", "BATC MiniTiouner v2", True),
                         self.detect())

    def test_a_knucker(self):
        self.fs.hub().knucker()
        self.assertEqual(("knucker", "CombiTuner-Express", True), self.detect())

    def test_a_picotuner(self):
        """The case that prompted this: G8YTZ's own card, where the
        answer used to be "minitiouner" and longmynd then flapped every
        five seconds for ever."""
        self.fs.hub().picotuner()
        self.assertEqual(("picotuner", "BATC PicoTuner", True), self.detect())

    def test_nothing_plugged_in(self):
        """found_any False, which is what makes the journal able to say
        "nothing found" rather than "assuming a Minitiouner"."""
        self.fs.hub()
        self.assertEqual(("minitiouner", "", False), self.detect())

    def test_no_sysfs_at_all(self):
        """Not a Linux box, or sysfs not mounted. Must not raise."""
        self.assertEqual(("minitiouner", "", False),
                         detect_tuner.detect(root="/nonexistent-really"))

    # ── an FT2232H we do not recognise ───────────────────────────

    def test_an_ftdi_with_a_generic_string_is_a_minitiouner(self):
        """BATC's boards do not all carry the same string, and a bare
        FTDI cable says "FT2232H MiniModule" or similar. Every Remote Rx
        built so far is a Minitiouner, so that is the safe guess."""
        self.fs.hub().minitiouner(product="Dual RS232-HS")
        self.assertEqual(("minitiouner", "Dual RS232-HS", True), self.detect())

    def test_an_ftdi_with_no_product_string_at_all(self):
        self.fs.hub().add(vendor="0403", product_id="6010")
        self.assertEqual(("minitiouner", "", True), self.detect())

    def test_an_unrelated_usb_device_is_not_a_tuner(self):
        """A memory stick must not be read as a tuner."""
        self.fs.hub().add(vendor="0781", product_id="5567",
                          product="Cruzer Blade")
        self.assertEqual(("minitiouner", "", False), self.detect())

    # ── two at once, where the order of preference matters ───────

    def test_a_minitiouner_beats_a_picotuner(self):
        """Plug a Picotuner into a working Minitiouner card and the
        Minitiouner must keep the site on the air. Both orders, because
        sysfs enumeration order is not ours to choose."""
        self.fs.hub().picotuner().minitiouner()
        self.assertEqual("minitiouner", self.detect()[0])

    def test_a_minitiouner_beats_a_picotuner_whichever_enumerates_first(self):
        self.fs.hub().minitiouner().picotuner()
        self.assertEqual("minitiouner", self.detect()[0])

    def test_a_knucker_beats_a_picotuner(self):
        self.fs.hub().picotuner().knucker()
        self.assertEqual("knucker", self.detect()[0])

    def test_two_picotuners_is_still_a_picotuner(self):
        """A Picotuner board can host two NIMs, and somebody may yet
        plug in two boards. Not an error, just the same answer."""
        self.fs.hub().picotuner().picotuner()
        self.assertEqual("picotuner", self.detect()[0])

    def test_an_unrecognised_ftdi_still_beats_a_picotuner(self):
        """The fallback guess is a SUPPORTED tuner, so it must win over
        a tuner known to be undriveable."""
        self.fs.hub().picotuner().minitiouner(product="Dual RS232-HS")
        self.assertEqual("minitiouner", self.detect()[0])

    # ── the contract the rest of the Remote Rx relies on ─────────

    # The Minitiouner is the one kind not named after its module: it is
    # the default, served by the original longmynd_source.py, and both
    # the wrapper and longmynd_source's own handover special-case it.
    SOURCE_FOR = {"minitiouner": "longmynd_source.py"}

    def test_every_kind_has_a_source_module_to_start(self):
        """A kind detect() can return with no module behind it means a
        Remote Rx that starts nothing, or starts longmynd by accident."""
        for _n, kind, _s in detect_tuner.SIGNATURES:
            with self.subTest(kind):
                module = self.SOURCE_FOR.get(kind, f"{kind}_source.py")
                self.assertTrue(
                    os.path.exists(os.path.join("slave", module)),
                    f"detect() can return {kind!r} but slave/{module} "
                    f"does not exist")

    def test_the_wrapper_has_a_case_for_every_kind_but_the_default(self):
        """The failure this catches: a kind added to SIGNATURES and not
        to lynx-slave. The wrapper's catch-all would then start longmynd
        against a tuner it cannot drive, which is the exact fault this
        whole change was fixing - silently reintroduced."""
        with open(os.path.join("slave", "lynx-slave")) as f:
            wrapper = f.read()
        for _n, kind, _s in detect_tuner.SIGNATURES:
            if kind in self.SOURCE_FOR:
                continue        # the catch-all serves it on purpose
            with self.subTest(kind):
                self.assertIn(f"{kind})", wrapper,
                              f"slave/lynx-slave has no case for {kind!r}, "
                              f"so it would fall through to longmynd")

    def test_the_updater_start_tests_every_source_module(self):
        """Gate 2 proves each source starts with --help before switching
        version. A module missing from that list is one an update can
        break without the gate noticing."""
        sys.path.insert(0, "slave")
        import updater
        for _n, kind, _s in detect_tuner.SIGNATURES:
            module = self.SOURCE_FOR.get(kind, f"{kind}_source.py")
            with self.subTest(kind):
                self.assertIn(module, updater.SOURCE_MODULES)

    def test_supported_is_derived_and_excludes_the_picotuner(self):
        self.assertIn("minitiouner", detect_tuner.SUPPORTED)
        self.assertIn("knucker", detect_tuner.SUPPORTED)
        self.assertNotIn("picotuner", detect_tuner.SUPPORTED)

    def test_the_receiver_accepts_every_kind_detection_can_report(self):
        """A kind the Remote Rx reports in TUN but the receiver does not
        list is dropped on the floor, and the panel quietly shows a
        Minitiouner. Checked against lynx_app.py's own tuple rather
        than a copy of it."""
        import ast
        with open("lynx_app.py") as f:
            src = f.read()
        listed = None
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "REMOTE_TUNER_KINDS"
                    for t in node.targets):
                listed = set(ast.literal_eval(node.value))
        self.assertIsNotNone(listed, "REMOTE_TUNER_KINDS not found")
        for _n, kind, _s in detect_tuner.SIGNATURES:
            with self.subTest(kind):
                self.assertIn(kind, listed)

    def test_never_opens_the_device(self):
        """Claiming the interface while the other tuner's software has
        it makes CombiTunerExpress segfault rather than complain. Only
        the three text attributes may be read."""
        import builtins
        opened = []
        orig = builtins.open

        def spy(path, *a, **k):
            opened.append(str(path))
            return orig(path, *a, **k)

        self.fs.hub().knucker().picotuner()
        builtins.open = spy
        try:
            self.detect()
        finally:
            builtins.open = orig
        allowed = {"idVendor", "idProduct", "product"}
        for path in opened:
            self.assertIn(os.path.basename(path), allowed,
                          f"read {path}, which is not a sysfs text attribute")


if __name__ == "__main__":
    unittest.main(verbosity=2)
