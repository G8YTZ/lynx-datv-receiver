#!/usr/bin/env python3
"""Tests for the Lynx Bar: what it says, and when it is up.

Run:  python3 test_lynx_bar.py

The drawing is judged by eye, with tools/preview_lynx_bar.py. What is
tested here is everything that is not a matter of taste: which fields
appear in which state, that a diversity pair stays a pair, and the
timing of the pop-up and the auto-hide.

The functions are lifted out of lynx_overlay.py rather than imported:
that module builds a Gtk window at import time and needs a display.
Lifting them keeps this runnable on a machine with no screen, and
tests what ships rather than a copy of it.
"""

import ast
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "lynx_overlay.py")

WANT_FUNCS = ("_bar_cells",)
WANT_METHODS = ("_bar_hold_secs", "_bar_progress", "_bar_trigger_signature",
                "update_lynx_bar", "show_lynx_bar", "hide_lynx_bar",
                "toggle_lynx_bar", "apply_lynx_bar_command")


class FakeClock:
    """Stands in for time.monotonic, so the auto-hide can be tested
    without sleeping through a minute of it."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def advance(self, secs):
        self.now += secs


def load():
    """(module namespace, a Bar class carrying the methods and
    constants). `state` in both is one shared dict the tests write."""
    src = open(SRC).read()
    tree = ast.parse(src)
    state = {}
    clock = FakeClock()
    ns = {"state": state, "time": clock}

    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT_FUNCS:
            exec(compile(ast.Module([node], []), "<bar>", "exec"), ns)
    missing = [f for f in WANT_FUNCS if f not in ns]

    cls = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LynxOverlay":
            cls = node
    if cls is None:
        raise SystemExit("LynxOverlay not found")

    body = []
    consts = {}
    for item in cls.body:
        if isinstance(item, ast.FunctionDef) and item.name in WANT_METHODS:
            body.append(item)
        elif (isinstance(item, ast.Assign) and len(item.targets) == 1
              and isinstance(item.targets[0], ast.Name)
              and item.targets[0].id.startswith(("LYNX_BAR", "BAR_"))):
            consts[item.targets[0].id] = ast.literal_eval(item.value)
    missing += [m for m in WANT_METHODS
                if m not in {f.name for f in body}]
    if missing:
        raise SystemExit("not found in lynx_overlay.py: " + ", ".join(missing))

    holder = ast.ClassDef(name="Bar", bases=[], keywords=[], body=body,
                          decorator_list=[], type_params=[])
    ast.fix_missing_locations(ast.Module([holder], []))
    exec(compile(ast.Module([holder], []), "<bar>", "exec"), ns)
    bar_cls = ns["Bar"]
    for k, v in consts.items():
        setattr(bar_cls, k, v)
    return ns, bar_cls, state, clock


class CellTests(unittest.TestCase):
    """What the bar says."""

    def setUp(self):
        self.ns, _, _, _ = load()
        self.cells = self.ns["_bar_cells"]

    def labels(self, st):
        return [c[0] for c in self.cells(st)]

    LOCKED = {"mode": "rf", "locked": True, "callsign": "G8YTZ",
              "frequency": 438.021, "sr_ks": 333, "sr_units": "kS/s",
              "modcod": "QPSK 9/10", "mer": 23.4, "margin": 17.0,
              "codec": "H265", "audio_codec": "AAC"}

    def test_a_locked_carrier_says_everything_the_osd_did(self):
        """The bar stands in for the text zones, so anything they
        carried and it does not is information lost from the screen."""
        self.assertEqual(
            ["FREQUENCY", "SYMBOL RATE", "MODE", "CODEC", "MER", "MARGIN"],
            self.labels(self.LOCKED))

    def test_values_are_formatted_for_reading_not_for_precision(self):
        got = dict((c[0], c[1]) for c in self.cells(self.LOCKED))
        self.assertEqual("438.021 MHz", got["FREQUENCY"])
        self.assertEqual("333 kS/s", got["SYMBOL RATE"])
        self.assertEqual("23.4 dB", got["MER"])
        self.assertEqual("H265/AAC", got["CODEC"])

    def test_searching_dims_rather_than_hides(self):
        """A tuner with no lock still has a frequency worth seeing, and
        blanking it would make the bar flash its whole width every time
        a signal dropped."""
        st = dict(self.LOCKED, locked=False, mer=None, margin=None)
        cells = self.cells(st)
        self.assertIn("FREQUENCY", [c[0] for c in cells])
        self.assertTrue(all(c[2] for c in cells), "everything should be dim")

    def test_diversity_stays_a_pair(self):
        """Two tuners means two columns. An average of two tuners is a
        number describing nothing."""
        st = dict(self.LOCKED, diversity_enabled=True, locked_a=True,
                  locked_b=True, mer_a=23.4, mer_b=19.8)
        got = dict((c[0], c[1]) for c in self.cells(st))
        self.assertEqual("23.4 dB", got["MER A"])
        self.assertEqual("19.8 dB", got["MER B"])
        self.assertNotIn("MER", got, "the single MER must give way to the pair")

    def test_a_diversity_tuner_with_no_lock_is_dim_but_present(self):
        """Its absence is the information. Dropping the column would
        make a half-working pair look like a working single."""
        st = dict(self.LOCKED, diversity_enabled=True, locked_a=True,
                  locked_b=False, mer_a=23.4, mer_b=None)
        cells = dict((c[0], (c[1], c[2])) for c in self.cells(st))
        self.assertIn("MER B", cells)
        self.assertTrue(cells["MER B"][1], "B should be dim")
        self.assertFalse(cells["MER A"][1], "A should not be")

    def test_a_stream_says_stream_things(self):
        st = {"mode": "stream", "locked": True, "stream_name": "Lenham",
              "stream_protocol": "srt", "stream_bitrate_kbps": 610,
              "stream_video_codec": "H265", "stream_audio_codec": "AAC"}
        got = dict((c[0], c[1]) for c in self.cells(st))
        self.assertEqual("Lenham", got["SOURCE"])
        self.assertEqual("SRT", got["LINK"])
        self.assertEqual("0.61 Mb/s", got["RATE"])
        self.assertNotIn("FREQUENCY", got, "a stream has no frequency")

    def test_nothing_known_is_not_a_crash(self):
        for st in ({}, {"mode": "rf"}, {"locked": True},
                   {"mode": "stream", "locked": False}):
            with self.subTest(st):
                self.cells(st)


class TimingTests(unittest.TestCase):
    """When it is up."""

    def setUp(self):
        self.ns, self.cls, self.state, self.clock = load()
        self.bar = self.cls()
        self.state.update({"locked": False, "lynx_bar_secs": 15})

    def lock(self, call="G8YTZ"):
        self.state.update({"locked": True, "callsign": call})
        self.bar.update_lynx_bar()

    def test_a_new_transmission_raises_it(self):
        self.assertEqual(0.0, self.bar._bar_progress())
        self.lock()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS)
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2)

    def test_it_rises_rather_than_appearing(self):
        """Part way through the slide it must be part way up, or the
        pop-up is just a flash."""
        self.lock()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS / 2.0)
        p = self.bar._bar_progress()
        self.assertGreater(p, 0.05)
        self.assertLess(p, 0.95)

    def test_it_hides_again(self):
        self.lock()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS + 15
                           + self.cls.LYNX_BAR_SLIDE_SECS + 0.1)
        self.assertEqual(0.0, self.bar._bar_progress())

    def test_it_is_still_up_just_before_its_time(self):
        self.lock()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS + 14.5)
        self.assertGreater(self.bar._bar_progress(), 0.9)

    def test_keep_it_up_means_keep_it_up(self):
        """0 is the "permanent" setting and must not be read as "hide
        immediately", which is what a plain falsy test would do."""
        self.state["lynx_bar_secs"] = 0
        self.lock()
        self.clock.advance(3600)
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2)

    def test_each_hold_setting_is_honoured(self):
        for secs in (8, 15, 30, 60):
            with self.subTest(secs):
                ns, cls, state, clock = load()
                bar = cls()
                state.update({"locked": True, "callsign": "G8YTZ",
                              "lynx_bar_secs": secs})
                bar.update_lynx_bar()
                clock.advance(cls.LYNX_BAR_SLIDE_SECS + secs - 0.5)
                self.assertGreater(bar._bar_progress(), 0.5, "too early")
                clock.advance(1.0 + cls.LYNX_BAR_SLIDE_SECS)
                self.assertEqual(0.0, bar._bar_progress(), "too late")

    def test_a_rubbish_setting_falls_back_rather_than_raising(self):
        for bad in (None, "", "soon", float("nan")):
            with self.subTest(bad):
                self.state["lynx_bar_secs"] = bad
                self.assertIsInstance(self.bar._bar_hold_secs(), float)

    def test_tuning_around_does_not_keep_popping_it_up(self):
        """Frequency and symbol rate change constantly while somebody
        tunes. A bar that reappeared on every twitch would be an
        irritation rather than information."""
        self.lock()
        self.clock.advance(100)                 # let it hide
        self.assertEqual(0.0, self.bar._bar_progress())
        for f in (438.0, 439.0, 440.0):
            self.state["frequency"] = f
            self.state["sr_ks"] = 125
            self.bar.update_lynx_bar()
        self.assertEqual(0.0, self.bar._bar_progress(),
                         "tuning must not raise the bar")

    def test_a_different_station_does_raise_it(self):
        self.lock("G8YTZ")
        self.clock.advance(100)
        self.assertEqual(0.0, self.bar._bar_progress())
        self.lock("G4KLB")
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS)
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2)

    def test_losing_lock_does_not_raise_it(self):
        """Signals drop all the time. The bar announces arrivals."""
        self.lock()
        self.clock.advance(100)
        self.state["locked"] = False
        self.bar.update_lynx_bar()
        self.assertEqual(0.0, self.bar._bar_progress())

    def test_it_can_be_asked_for(self):
        self.bar.show_lynx_bar()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS)
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2)


class ButtonTests(unittest.TestCase):
    """The button on the receiver page.

    The web app cannot talk to the overlay process, so a press is left
    in the status payload as a token and collected on the next poll.
    Everything that can go wrong with that - a press missed, a press
    acted on twice, a press invented at startup - is tested here.
    """

    def setUp(self):
        self.ns, self.cls, self.state, self.clock = load()
        self.bar = self.cls()
        self.state.update({"locked": False, "lynx_bar_secs": 15,
                           "lynx_bar_cmd": {"token": 0, "action": "toggle"}})
        # The overlay adopts whatever token it finds on its first poll.
        self.bar.apply_lynx_bar_command()

    def press(self, action="toggle"):
        cmd = self.state["lynx_bar_cmd"]
        self.state["lynx_bar_cmd"] = {"token": cmd["token"] + 1,
                                      "action": action}
        self.bar.apply_lynx_bar_command()

    def settle(self):
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS)

    def test_a_press_brings_it_up(self):
        self.press()
        self.settle()
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2)

    def test_a_second_press_sends_it_away(self):
        self.press()
        self.settle()
        self.press()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS + 0.1)
        self.assertEqual(0.0, self.bar._bar_progress())

    def test_it_slides_away_rather_than_vanishing(self):
        """Half way through the dismissal it must be half way down. A
        bar that blinked out would read as a glitch, not as a
        dismissal."""
        self.press()
        self.settle()
        self.press()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS / 2.0)
        p = self.bar._bar_progress()
        self.assertGreater(p, 0.05)
        self.assertLess(p, 0.95)

    def test_the_button_beats_keep_it_up(self):
        """0 secs means the bar never hides itself. Somebody pressing
        the button has said what they want more recently than the
        settings page did, so it must still go."""
        self.state["lynx_bar_secs"] = 0
        self.press()
        self.settle()
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2)
        self.press()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS + 0.1)
        self.assertEqual(0.0, self.bar._bar_progress())

    def test_one_press_is_acted_on_once(self):
        """The status payload is polled several times a second and
        carries the same token every time. Acting on it again would
        make one press flap the bar up and down."""
        self.press()
        self.settle()
        for _ in range(20):
            self.bar.apply_lynx_bar_command()
            self.clock.advance(0.2)
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2,
                               msg="a repeat poll toggled it back down")

    def test_a_press_is_never_missed(self):
        """Pressing it twice between two polls - possible, if the
        overlay is busy - must still leave it acted on, not dropped."""
        cmd = self.state["lynx_bar_cmd"]
        self.state["lynx_bar_cmd"] = {"token": cmd["token"] + 2,
                                      "action": "toggle"}
        self.bar.apply_lynx_bar_command()
        self.settle()
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2)

    def test_a_restarted_overlay_does_not_pop_it_up(self):
        """The first poll after the overlay starts carries whatever
        token the receiver already had. Acting on it would raise the
        bar on every restart, for a button nobody pressed."""
        ns, cls, state, clock = load()
        state.update({"locked": False, "lynx_bar_secs": 15,
                      "lynx_bar_cmd": {"token": 37, "action": "toggle"}})
        fresh = cls()
        for _ in range(5):
            fresh.apply_lynx_bar_command()
            clock.advance(0.2)
        self.assertEqual(0.0, fresh._bar_progress())

    def test_show_and_hide_are_not_toggles(self):
        """Anything driving the receiver from outside - a Companion
        button, say - wants to set the state, not flip it. Two shows in
        a row must leave it up."""
        self.press("show")
        self.settle()
        self.press("show")
        self.settle()
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2)
        self.press("hide")
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS + 0.1)
        self.assertEqual(0.0, self.bar._bar_progress())
        self.press("hide")
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS + 0.1)
        self.assertEqual(0.0, self.bar._bar_progress())

    def test_hiding_something_already_down_is_harmless(self):
        self.press("hide")
        self.clock.advance(5)
        self.assertEqual(0.0, self.bar._bar_progress())

    def test_it_can_be_brought_back_after_a_dismissal(self):
        """The dismissal must not leave state behind that stops the
        next press working - the bug this is here to catch."""
        self.press()
        self.settle()
        self.press()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS + 0.1)
        self.assertEqual(0.0, self.bar._bar_progress())
        self.press()
        self.settle()
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2)

    def test_catching_it_on_the_way_down_brings_it_back(self):
        self.press()
        self.settle()
        self.press()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS / 2.0)
        self.press()
        self.settle()
        self.assertAlmostEqual(1.0, self.bar._bar_progress(), places=2)

    def test_a_bar_raised_by_the_button_still_auto_hides(self):
        """Asking for it is not asking to keep it: the configured hold
        still applies, so nobody has to remember to put it away."""
        self.press()
        self.clock.advance(self.cls.LYNX_BAR_SLIDE_SECS + 15
                           + self.cls.LYNX_BAR_SLIDE_SECS + 0.1)
        self.assertEqual(0.0, self.bar._bar_progress())

    def test_a_missing_or_rubbish_command_is_not_a_crash(self):
        for bad in (None, {}, {"token": None}, {"action": "toggle"},
                    {"token": 1, "action": None},
                    {"token": 2, "action": "wibble"}):
            with self.subTest(bad):
                self.state["lynx_bar_cmd"] = bad
                self.bar.apply_lynx_bar_command()

    def test_an_unknown_action_falls_back_to_toggling(self):
        """Rather than being ignored. A newer web app sending an action
        this overlay has not heard of should still do something
        sensible - the button is the common case."""
        self.press("wibble")
        self.settle()
        self.assertGreater(self.bar._bar_progress(), 0.9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
