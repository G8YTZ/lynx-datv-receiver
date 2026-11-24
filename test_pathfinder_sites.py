#!/usr/bin/env python3
"""Tests for plotting Pathfinder from the site that heard the signal.

Run:  python3 test_pathfinder_sites.py

Before this, the card always plotted from the configured home locator.
On a Remote Rx contact that drew a path the signal never travelled and
reported a distance that was simply wrong. These tests pin what replaced
it: one leg per receiving site, two local tuners counting as one site,
and a card with no site named drawing exactly what it always did.

The app-side helper is lifted out of lynx_app.py by AST rather than
imported, since importing that module starts the whole receiver.
"""

import ast
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))

import lynx_map  # noqa: E402

ORPINGTON, LENHAM, STATION = "JO01IF", "JO01KG", "IO90BR"


def load_site_helper(config):
    tree = ast.parse(open(os.path.join(HERE, "lynx_app.py")).read())
    ns = {"config": config}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_pathfinder_site_for":
            exec(compile(ast.Module([node], []), "<pf>", "exec"), ns)
    if "_pathfinder_site_for" not in ns:
        raise SystemExit("_pathfinder_site_for not found in lynx_app.py")
    return ns["_pathfinder_site_for"]


class SiteForTests(unittest.TestCase):
    """Which site a given source's contact belongs to."""

    CFG = {"site": {"locator": ORPINGTON, "location": "Orpington"}}

    def setUp(self):
        self.site_for = load_site_helper(self.CFG)

    def test_a_remote_rx_is_plotted_from_itself(self):
        got = self.site_for({"locator": LENHAM, "name": "Lenham"})
        self.assertEqual({"locator": LENHAM, "name": "Lenham"}, got)

    def test_a_local_tuner_is_plotted_from_the_configured_site(self):
        got = self.site_for({"callsign": "G8YTZ", "locked": True})
        self.assertEqual({"locator": ORPINGTON, "name": "Orpington"}, got)

    def test_a_remote_that_has_not_reported_yet_falls_back_to_home(self):
        """Its locator is learned from its heartbeat, so it is empty
        until the first packet. Plotting from nowhere would draw no
        card; home is at least a card."""
        got = self.site_for({"locator": "", "name": ""})
        self.assertEqual(ORPINGTON, got["locator"])

    def test_no_source_state_at_all_is_not_a_crash(self):
        for st in (None, {}):
            with self.subTest(st):
                self.assertEqual(ORPINGTON, self.site_for(st)["locator"])

    def test_no_configured_site_gives_an_empty_locator_not_a_crash(self):
        got = load_site_helper({})({})
        self.assertEqual("", got["locator"])


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.r = lynx_map.MapRenderer(ORPINGTON)

    def sites(self, heard, name="Orpington"):
        return self.r._resolve_sites(heard, name)

    def test_no_site_named_means_home(self):
        """What every card meant before Remote Rx existed."""
        got = self.sites(None)
        self.assertEqual([ORPINGTON], [s["locator"] for s in got])
        self.assertEqual("Orpington", got[0]["name"])

    def test_a_named_site_replaces_home_rather_than_joining_it(self):
        got = self.sites([{"locator": LENHAM, "name": "Lenham"}])
        self.assertEqual([LENHAM], [s["locator"] for s in got])

    def test_two_sites_are_two_legs(self):
        got = self.sites([{"locator": ORPINGTON, "name": "Orpington"},
                          {"locator": LENHAM, "name": "Lenham"}])
        self.assertEqual(2, len(got))

    def test_two_tuners_at_one_site_are_one_leg(self):
        """Local diversity is two receivers at ONE place, so it must
        not draw two identical lines on top of each other."""
        got = self.sites([{"locator": ORPINGTON, "name": "Rx A"},
                          {"locator": ORPINGTON, "name": "Rx B"}])
        self.assertEqual(1, len(got))

    def test_the_same_locator_in_different_case_is_the_same_site(self):
        got = self.sites([{"locator": "jo01if"}, {"locator": "JO01IF"}])
        self.assertEqual(1, len(got))

    def test_unusable_entries_are_dropped_not_fatal(self):
        got = self.sites([None, {}, {"locator": "nonsense"},
                          {"locator": LENHAM, "name": "Lenham"}])
        self.assertEqual([LENHAM], [s["locator"] for s in got])

    def test_all_rubbish_falls_back_to_home(self):
        got = self.sites([{"locator": "nonsense"}])
        self.assertEqual([ORPINGTON], [s["locator"] for s in got])

    def test_order_is_preserved_so_the_first_is_the_primary(self):
        got = self.sites([{"locator": LENHAM}, {"locator": ORPINGTON}])
        self.assertEqual([LENHAM, ORPINGTON], [s["locator"] for s in got])

    def test_no_home_and_nothing_named_is_an_empty_list(self):
        self.assertEqual([], lynx_map.MapRenderer("")._resolve_sites(None, ""))


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.r = lynx_map.MapRenderer(ORPINGTON)

    def pt(self, loc):
        return lynx_map.locator_to_latlon(loc)

    def contains(self, win, pts):
        x0, x1, y0, y1, _ = win
        return all(x0 <= lon <= x1 and y0 <= lat <= y1 for lat, lon in pts)

    def test_every_point_is_inside_the_frame(self):
        pts = [self.pt(STATION), self.pt(ORPINGTON), self.pt(LENHAM)]
        self.assertTrue(self.contains(self.r._window(pts, 1920, 1080), pts))

    def test_a_distant_second_site_does_not_fall_off_the_edge(self):
        """The failure mode of framing only the first pair."""
        pts = [self.pt(STATION), self.pt(ORPINGTON), self.pt("IO63UA")]
        self.assertTrue(self.contains(self.r._window(pts, 1920, 1080), pts))

    def test_adding_a_site_never_shrinks_the_frame(self):
        two = self.r._window([self.pt(STATION), self.pt(ORPINGTON)], 1920, 1080)
        three = self.r._window([self.pt(STATION), self.pt(ORPINGTON),
                                self.pt("IO63UA")], 1920, 1080)
        self.assertGreaterEqual(three[4], two[4])


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.r = lynx_map.MapRenderer(ORPINGTON)

    def card(self, **kw):
        return self.r.render(1920, 1080, "G4KLB", STATION, name="Colin",
                             mer="12.4", modcod="QPSK 3/4",
                             symbol_rate="333", frequency="437.000",
                             site_name="Orpington", **kw)

    def test_a_card_with_no_site_named_still_draws(self):
        self.assertIsNotNone(self.card())

    def test_a_remote_rx_card_draws(self):
        self.assertIsNotNone(
            self.card(heard_by=[{"locator": LENHAM, "name": "Lenham"}]))

    def test_a_diversity_card_draws(self):
        self.assertIsNotNone(self.card(heard_by=[
            {"locator": ORPINGTON, "name": "Orpington"},
            {"locator": LENHAM, "name": "Lenham"}]))

    def test_a_remote_card_differs_from_a_home_card(self):
        """The whole point: the same station, heard somewhere else,
        must not produce the same picture."""
        home = bytes(self.card().get_data())
        remote = bytes(self.card(heard_by=[
            {"locator": LENHAM, "name": "Lenham"}]).get_data())
        self.assertNotEqual(home, remote)

    def test_an_unusable_station_locator_draws_no_card(self):
        self.assertIsNone(self.r.render(1920, 1080, "X", "nonsense"))

    def test_a_card_with_no_home_and_no_site_draws_nothing(self):
        self.assertIsNone(
            lynx_map.MapRenderer("").render(1920, 1080, "G4KLB", STATION))

    def test_a_remote_card_draws_even_with_no_home_configured(self):
        """The receiving site is enough. Requiring a home locator as
        well would silently drop a card the data fully supports."""
        r = lynx_map.MapRenderer("")
        self.assertIsNotNone(r.render(
            1920, 1080, "G4KLB", STATION,
            heard_by=[{"locator": LENHAM, "name": "Lenham"}]))


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.t = lynx_map.PathfinderTracker(delay_secs=0, duration_secs=30)

    def test_heard_by_travels_with_the_card(self):
        heard = [{"locator": LENHAM, "name": "Lenham"}]
        self.t.station_unlocked("G4KLB", STATION, heard_by=heard)
        self.assertEqual(heard, self.t.pending["heard_by"])

    def test_nothing_named_is_stored_as_none(self):
        self.t.station_unlocked("G4KLB", STATION)
        self.assertIsNone(self.t.pending["heard_by"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
