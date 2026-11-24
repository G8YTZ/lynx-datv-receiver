#!/usr/bin/env python3
"""Tests for slave/net_info.py and the SRT latency setting.

Run:  python3 test_net_info.py

Both of these exist because of one afternoon (24 Nov 2026). A Remote Rx
sat at 25.1 dB MER with 18.7 dB of margin - an immaculate signal off the
air - and produced grey frames and freezes at the repeater, because its
Wi-Fi was on a distant AP and SRT could not recover the bursts inside
300 ms. The panel could show every RF figure and nothing at all about
the radio carrying the result, and the one setting that would have
fixed it could only be changed over ssh to a machine in a loft.

So: report the signal, and let the latency be set from the panel. The
reporting is tested here against synthetic /proc trees, because the
machine running the tests has whatever network it has and that is not a
test fixture.
"""

import ast
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, "slave")
import net_info          # noqa: E402
import remote_common     # noqa: E402

HEADER = ("Inter-| sta-|   Quality        |   Discarded packets"
          "               | Missed | WE\n"
          " face | tus | link level noise |  nwid  crypt   frag  retry"
          "   misc | beacon | 22\n")


class Fake:
    """A /proc and /sys tree with whatever interfaces a test wants."""

    def __init__(self, root):
        self.root = root
        os.makedirs(os.path.join(root, "proc/net"), exist_ok=True)
        os.makedirs(os.path.join(root, "sys/class/net"), exist_ok=True)

    def wireless(self, *lines):
        with open(os.path.join(self.root, "proc/net/wireless"), "w") as f:
            f.write(HEADER + "".join(lines))
        return self

    def iface(self, name, operstate="up", wireless=False):
        d = os.path.join(self.root, "sys/class/net", name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "operstate"), "w") as f:
            f.write(operstate + "\n")
        if wireless:
            os.makedirs(os.path.join(d, "wireless"), exist_ok=True)
        return self


class NetInfoTests(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lynx-net-")
        self.fs = Fake(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    # ── reading the radio ────────────────────────────────────────

    def test_g8ytz_rx2_as_it_actually_reads(self):
        """The real line off remote-rx-2, 24 Nov 2026, which was the
        HEALTHY card. Its -66 dBm is the yardstick the marginal one is
        compared against."""
        self.fs.wireless(" wlan0: 0000   52.  -66.  -256        0      0"
                         "      0      0      0        0\n")
        self.assertEqual(("wifi", -66, 52), net_info.describe(self.dir))
        self.assertEqual("NET wifi -66 52", net_info.heartbeat_line(self.dir))

    def test_the_trailing_dots_are_not_decimals(self):
        """"-66." is how the kernel writes it. Read as a float it is
        -66.0, which is right; read with a naive int() it raises."""
        self.fs.wireless(" wlan0: 0000   70.  -45.  -256  0 0 0 0 0 0\n")
        self.assertEqual(-45, net_info.describe(self.dir)[1])

    def test_an_interface_with_no_signal_yet(self):
        """Associating, or associated with nothing to report."""
        self.fs.wireless(" wlan0: 0000    0.    0.  -256  0 0 0 0 0 0\n")
        kind, dbm, _ = net_info.describe(self.dir)
        self.assertEqual("wifi", kind)
        self.assertIsNone(dbm, "0 is not a signal level; it must not read as one")

    def test_an_old_driver_reporting_an_unsigned_scale(self):
        """A few drivers report 0-255 rather than dBm. Reported as
        unknown rather than shown as a positive 'signal strength',
        which would read as very strong when it means nothing."""
        self.fs.wireless(" wlan0: 0000   52.  180.  -256  0 0 0 0 0 0\n")
        self.assertIsNone(net_info.describe(self.dir)[1])

    def test_wired(self):
        self.fs.wireless().iface("eth0")
        self.assertEqual(("wired", None, None), net_info.describe(self.dir))
        self.assertEqual("NET wired - -", net_info.heartbeat_line(self.dir))

    def test_loopback_alone_is_not_a_network(self):
        self.fs.wireless().iface("lo")
        self.assertEqual("-", net_info.describe(self.dir)[0])

    def test_a_down_interface_does_not_count(self):
        self.fs.wireless().iface("eth0", operstate="down")
        self.assertEqual("-", net_info.describe(self.dir)[0])

    def test_wifi_wins_when_a_card_has_both(self):
        """The radio is the one that degrades quietly, so it is the one
        worth reporting."""
        self.fs.wireless(" wlan0: 0000   52.  -70.  -256  0 0 0 0 0 0\n")
        self.fs.iface("eth0")
        self.assertEqual("wifi", net_info.describe(self.dir)[0])

    def test_nothing_at_all_does_not_raise(self):
        """No /proc/net/wireless, no sysfs. This runs on a machine
        nobody can reach, in the heartbeat path."""
        self.assertEqual(("-", None, None),
                         net_info.describe("/nonexistent-really"))
        self.assertEqual("NET - - -",
                         net_info.heartbeat_line("/nonexistent-really"))

    def test_rubbish_in_proc_does_not_raise(self):
        for junk in ("", "garbage\n", HEADER,
                     HEADER + " wlan0: nonsense here\n",
                     HEADER + " wlan0:\n"):
            with self.subTest(junk[:20]):
                with open(os.path.join(self.dir, "proc/net/wireless"), "w") as f:
                    f.write(junk)
                net_info.describe(self.dir)   # must not raise

    # ── the verdict a person acts on ─────────────────────────────

    def test_the_words_match_the_thresholds(self):
        for dbm, want in ((-30, "strong"), (-60, "strong"), (-61, "fine"),
                          (-70, "fine"), (-71, "marginal"), (-80, "marginal"),
                          (-81, "poor"), (-95, "poor")):
            with self.subTest(dbm):
                self.assertEqual(want, net_info.quality_words(dbm))

    def test_the_healthy_card_reads_as_fine_and_not_as_strong(self):
        """-66 dBm worked, but with little to spare. Calling it strong
        would tell somebody their marginal site was fine."""
        self.assertEqual("fine", net_info.quality_words(-66))

    def test_unknown_has_no_verdict(self):
        self.assertEqual("", net_info.quality_words(None))

    # ── the heartbeat line's shape ───────────────────────────────

    def test_always_four_tokens(self):
        """Fixed width, as VER and TUN are, so the far end never has to
        work out how many fields it was sent."""
        for build in (lambda: self.fs.wireless(
                          " wlan0: 0000  52. -66. -256 0 0 0 0 0 0\n"),
                      lambda: self.fs.wireless().iface("eth0"),
                      lambda: self.fs.wireless()):
            build()
            with self.subTest(net_info.heartbeat_line(self.dir)):
                self.assertEqual(4, len(net_info.heartbeat_line(self.dir).split()))


class SrtLatencySettingTests(unittest.TestCase):
    """The latency that can now be pushed from the panel."""

    KEY = "shared-key"

    def packet(self, **extra):
        body = {"v": 99, "freq_khz": 438000, "sr": 333,
                "name": "Lenham", "locator": "JO01IF"}
        body.update(extra)
        import hashlib
        import hmac
        text = remote_common.SETTINGS_MAGIC + "\n" + json.dumps(body)
        sig = hmac.new(self.KEY.encode(), text.encode(),
                       hashlib.sha256).hexdigest()
        return (text + "\n" + sig).encode()

    def test_a_latency_arrives_and_is_an_int(self):
        st = remote_common.parse_settings_packet(
            self.packet(srt_latency=1000), self.KEY)
        self.assertEqual(1000, st["srt_latency"])

    def test_an_older_repeater_sends_none_which_means_leave_it_alone(self):
        """Rule 4, both orders: a packet from a repeater that predates
        this setting must not read as "set the latency to zero"."""
        st = remote_common.parse_settings_packet(self.packet(), self.KEY)
        self.assertIsNone(st["srt_latency"])

    def test_explicit_null_also_means_leave_it_alone(self):
        st = remote_common.parse_settings_packet(
            self.packet(srt_latency=None), self.KEY)
        self.assertIsNone(st["srt_latency"])

    def test_absurd_values_are_clamped_not_obeyed(self):
        """The far end cannot be reached when a typo takes it off the
        air, so the bounds are enforced where it is applied as well as
        where it is entered."""
        for sent, want in ((0, 20), (1, 20), (19, 20), (20, 20),
                           (8000, 8000), (300000, 8000), (-5, 20)):
            with self.subTest(sent):
                st = remote_common.parse_settings_packet(
                    self.packet(srt_latency=sent), self.KEY)
                self.assertEqual(want, st["srt_latency"])

    def test_a_non_numeric_latency_rejects_the_whole_packet(self):
        """Not "ignore that field": a settings packet that cannot be
        read as intended should not be half-applied."""
        self.assertIsNone(remote_common.parse_settings_packet(
            self.packet(srt_latency="loads"), self.KEY))

    def test_the_signature_still_covers_it(self):
        """Changing the latency must not be possible without the key."""
        data = self.packet(srt_latency=1000)
        self.assertIsNone(remote_common.parse_settings_packet(data, "wrong-key"))

    def test_the_receiver_sends_the_same_bounds(self):
        """lynx_app.py's own clamp, read from the source rather than
        copied, so the two cannot drift apart."""
        with open("lynx_app.py") as f:
            src = f.read()
        fn = None
        for node in ast.parse(src).body:
            if isinstance(node, ast.FunctionDef) and \
                    node.name == "_remote_cfg_latency":
                fn = ast.get_source_segment(src, node)
        self.assertIsNotNone(fn, "_remote_cfg_latency has gone")
        ns = {"Optional": None}
        exec(fn, ns)
        clamp = ns["_remote_cfg_latency"]
        self.assertIsNone(clamp({}), "unset must stay unset")
        self.assertIsNone(clamp({"srt_latency": ""}))
        self.assertIsNone(clamp({"srt_latency": "nonsense"}))
        self.assertEqual(20, clamp({"srt_latency": 1}))
        self.assertEqual(8000, clamp({"srt_latency": 999999}))
        self.assertEqual(1000, clamp({"srt_latency": 1000}))

    def test_a_latency_change_restamps_the_settings_version(self):
        """Otherwise the Remote Rx, which only accepts a NEWER version,
        would ignore the change and the panel would claim it applied."""
        with open("lynx_app.py") as f:
            src = f.read()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "REMOTE_CFG_FIELDS"
                    for t in node.targets):
                self.assertIn("srt_latency", ast.literal_eval(node.value))
                return
        self.fail("REMOTE_CFG_FIELDS not found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
