#!/usr/bin/env python3
"""Tests for the Watch button on the local Picotuner receivers.

Run:  python3 test_local_watch.py

What is pinned here is every decision, not the mpv hand-off itself
(that is rf_mpv_lifecycle_monitor's, unchanged, and needs hardware):
which value displayed_receiver_id takes, when a Watch is refused, and
that the port and receiver helpers follow the choice so a freeze
recovery lands on the receiver actually on screen.

The functions are lifted out of lynx_app.py by AST, decorators
stripped, into a namespace of fakes. Importing the module would start
the whole receiver.
"""

import ast
import os
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WANT = ("watch_local_receiver", "current_rf_target_port",
        "active_receiver_id", "active_receiver", "receiver_state",
        "receiver_is_remote")


class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


def load():
    tree = ast.parse(open(os.path.join(HERE, "lynx_app.py")).read())
    calls = []
    ns = {
        "HTTPException": HTTPException, "time": time,
        "tune_lock": threading.Lock(),
        "start_transition_cover": lambda: calls.append("cover"),
        "kill_mpv": lambda: calls.append("kill"),
        "picotuner_ts_port": lambda tuner, cfg: {"a": 10001, "b": 10002}[tuner],
        "config": {"picotuner": {}, "diversity": {"combiner_out_port": 10010}},
        "HDHR_VIDEO_PORT": 10020, "REMOTE_VIDEO_OUT_PORT": 10030,
        "REMOTE_RECEIVER_ID_BASE": 11, "MAX_REMOTE_RECEIVERS": 5,
        "remote_states": [{"locked": True}],
        "picotuner_state": {"locked": True},
        "picotuner_state_b": {"locked": False},
        "current_mode": "rf", "tri_watch_enabled": False,
        "tri_watch_target_rcv": 1, "diversity_enabled": False,
        "displayed_receiver_id": None, "mpv_running_for_rf": True,
        "rf_tuned_at": 0.0, "_calls": calls,
    }
    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in WANT:
            node.decorator_list = []
            exec(compile(ast.Module([node], []), "<lw>", "exec"), ns)
            found.add(node.name)
    missing = set(WANT) - found
    if missing:
        raise SystemExit("not found in lynx_app.py: " + ", ".join(sorted(missing)))
    return ns


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.ns = load()

    def watch(self, rid):
        return self.ns["watch_local_receiver"](rid)

    def refused(self, rid, code):
        with self.assertRaises(HTTPException) as cm:
            self.watch(rid)
        self.assertEqual(code, cm.exception.status_code)
        return cm.exception

    # -- what it chooses --

    def test_watching_rx2_puts_rx2_on_screen(self):
        self.ns["picotuner_state_b"]["locked"] = True
        self.watch(2)
        self.assertEqual(2, self.ns["displayed_receiver_id"])
        self.assertEqual(2, self.ns["active_receiver_id"]())

    def test_watching_rx1_is_stored_as_none(self):
        """None already means Rx 1, and every tune path clears to None.
        Storing 1 would be a second spelling of the same state."""
        self.ns["displayed_receiver_id"] = 11          # a Remote on screen
        self.watch(1)
        self.assertIsNone(self.ns["displayed_receiver_id"])
        self.assertEqual(1, self.ns["active_receiver_id"]())

    def test_watch_brings_a_remote_back_to_local(self):
        """The gap this closes: until now, the only way back from a
        Remote Rx to a local tuner was to retune it."""
        self.ns["displayed_receiver_id"] = 11
        self.assertEqual(11, self.ns["active_receiver_id"]())
        self.watch(1)
        self.assertEqual(1, self.ns["active_receiver_id"]())

    def test_in_diversity_either_receiver_means_the_pair(self):
        self.ns["diversity_enabled"] = True
        for rid in (1, 2):
            with self.subTest(rid):
                self.ns["displayed_receiver_id"] = 11
                self.watch(rid)
                self.assertIsNone(self.ns["displayed_receiver_id"])

    def test_in_diversity_rx2_needs_no_lock_of_its_own(self):
        """It is half of the pair; the pair is what goes on screen."""
        self.ns["diversity_enabled"] = True
        self.ns["picotuner_state_b"]["locked"] = False
        self.watch(2)

    # -- the hand-off --

    def test_a_switch_raises_the_cover_and_drops_the_old_picture(self):
        self.ns["displayed_receiver_id"] = 11
        self.watch(1)
        self.assertEqual(["cover", "kill"], self.ns["_calls"])
        self.assertFalse(self.ns["mpv_running_for_rf"])

    def test_the_lock_monitor_is_told_to_wait(self):
        """rf_tuned_at makes it ignore the first 1.5 s, so it cannot
        start mpv on the previous receiver's lock reading."""
        self.ns["displayed_receiver_id"] = 11
        before = time.time()
        self.watch(1)
        self.assertGreaterEqual(self.ns["rf_tuned_at"], before)

    def test_watching_what_is_already_on_screen_changes_nothing(self):
        """A second press must not black the picture for a restart."""
        got = self.watch(1)
        self.assertFalse(got["changed"])
        self.assertEqual([], self.ns["_calls"])

    def test_the_tune_lock_is_always_released(self):
        self.watch(1)
        self.assertTrue(self.ns["tune_lock"].acquire(blocking=False))

    def test_a_busy_tune_lock_is_refused_not_waited_on_forever(self):
        self.ns["tune_lock"] = _NeverLock()
        self.ns["displayed_receiver_id"] = 11
        self.refused(1, 503)
        self.assertEqual(11, self.ns["displayed_receiver_id"])

    # -- refusals --

    def test_tri_watch_is_left_to_choose_for_itself(self):
        self.ns["tri_watch_enabled"] = True
        self.refused(1, 409)

    def test_outside_rf_mode_it_is_refused(self):
        """From a stream, returning to a tuner means tuning it."""
        for mode in ("stream", "dvbt", "idle"):
            with self.subTest(mode):
                self.ns["current_mode"] = mode
                self.refused(1, 409)

    def test_rx2_with_nothing_tuned_is_refused(self):
        """Outside diversity nothing tunes Rx 2, so watching it would be
        a cover with nothing behind it."""
        self.ns["picotuner_state_b"]["locked"] = False
        e = self.refused(2, 409)
        self.assertIn("not locked", e.detail)

    def test_only_local_ids(self):
        for rid in (0, 3, 11):
            with self.subTest(rid):
                self.refused(rid, 404)

    def test_a_refusal_changes_nothing(self):
        self.ns["displayed_receiver_id"] = 11
        self.ns["tri_watch_enabled"] = True
        self.refused(1, 409)
        self.assertEqual(11, self.ns["displayed_receiver_id"])
        self.assertEqual([], self.ns["_calls"])


class PortTests(unittest.TestCase):
    """The port follows the choice - the freeze-recovery bug that
    current_rf_target_port() was created to end."""

    def setUp(self):
        self.ns = load()
        self.port = self.ns["current_rf_target_port"]

    def test_rx1_plays_port_a(self):
        self.assertEqual(10001, self.port())

    def test_a_watched_rx2_plays_port_b(self):
        self.ns["displayed_receiver_id"] = 2
        self.assertEqual(10002, self.port())

    def test_a_remote_still_plays_the_relay(self):
        self.ns["displayed_receiver_id"] = 11
        self.assertEqual(10030, self.port())

    def test_diversity_still_plays_the_combiner(self):
        self.ns["diversity_enabled"] = True
        self.assertEqual(10010, self.port())

    def test_tri_watch_still_decides_for_itself(self):
        self.ns["tri_watch_enabled"] = True
        self.ns["tri_watch_target_rcv"] = 2
        self.assertEqual(10002, self.port())
        self.ns["tri_watch_target_rcv"] = 1
        self.assertEqual(10001, self.port())

    def test_rx2_is_described_by_rx2s_state(self):
        self.ns["displayed_receiver_id"] = 2
        self.assertIs(self.ns["picotuner_state_b"], self.ns["active_receiver"]())


class _NeverLock:
    def acquire(self, *a, **k):
        return False

    def release(self):
        raise AssertionError("released a lock it never held")


if __name__ == "__main__":
    unittest.main(verbosity=2)
