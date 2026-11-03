"""Offline tests for tennis pairing and its arbs.

Run:  python3 -m unittest discover -s tests
"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbscan import engine, fees, scanner, tennis, trader  # noqa: E402
from arbscan.model import Book, Game  # noqa: E402

K = fees.kalshi_model("quadratic", 1)

# 11-02 Smith v Shick (Challenger): Polymarket long = Colton Smith (away), Kalshi one market per player.
EVENT = {"slug": "atp-colsmi-brashi-2026-11-02", "startTime": "2026-11-02T22:00:00Z", "live": True}
WINNER = {"slug": "aec-atp-colsmi-brashi-2026-11-02", "sportsMarketType": "tennis_match_winner", "id": "m1",
          "feeCoefficient": 0.0695, "minimumTradeQty": 0.01, "orderPriceMinTickSize": 0.01,
          "marketSides": [{"long": True, "description": "Colton Smith", "team": {"abbreviation": "colsmi", "ordering": "away"}},
                          {"long": False, "description": "Braden Shick", "team": {"abbreviation": "brashi", "ordering": "home"}}]}
KEV = {"KXATPCHALLENGERMATCH-26NOV02SMISHI": {
    "SMI": {"ticker": "KXATPCHALLENGERMATCH-26NOV02SMISHI-SMI", "yes_sub_title": "Colton Smith"},
    "SHI": {"ticker": "KXATPCHALLENGERMATCH-26NOV02SMISHI-SHI", "yes_sub_title": "Braden Shick"}}}


class PairingTests(unittest.TestCase):
    def test_kalshi_date(self):
        self.assertEqual(tennis.kalshi_date("atp-colsmi-brashi-2026-11-02"), "26NOV02")
        self.assertIsNone(tennis.kalshi_date("atp-bad"))

    def test_match_maps_home_away_to_kalshi_markets(self):
        m = tennis.match(EVENT, WINNER, KEV)
        self.assertEqual((m["away"], m["home"]), ("Smith", "Shick"))
        self.assertEqual(m["k_away"], "KXATPCHALLENGERMATCH-26NOV02SMISHI-SMI")
        self.assertEqual(m["k_home"], "KXATPCHALLENGERMATCH-26NOV02SMISHI-SHI")
        self.assertFalse(m["long_is_home"])
        self.assertEqual(m["series"], "KXATPCHALLENGERMATCH")

    def test_no_match_on_other_date_or_wrong_player(self):
        self.assertIsNone(tennis.match(dict(EVENT, slug="atp-colsmi-brashi-2026-11-03"), WINNER, KEV))
        wrong = {ev: {k: dict(v, yes_sub_title="Someone Else") for k, v in ms.items()} for ev, ms in KEV.items()}
        self.assertIsNone(tennis.match(EVENT, WINNER, wrong))

    def test_ambiguous_kalshi_event_skipped(self):
        two = dict(KEV, **{"KXATPMATCH-26NOV02SMISHI": KEV["KXATPCHALLENGERMATCH-26NOV02SMISHI"]})
        self.assertIsNone(tennis.match(EVENT, WINNER, two))


class ContractTests(unittest.TestCase):
    def _game(self):
        return tennis.build(tennis.match(EVENT, WINNER, KEV), K)

    def test_six_contracts_on_margin_axis(self):
        g = self._game()
        self.assertEqual((g.sport, g.key, g.title, g.live), ("tennis", EVENT["slug"], "Smith v Shick", True))
        by = {(c.venue, c.ref["ticker"].rsplit("-", 1)[-1], c.ref["side"]): c for c in g.contracts}
        self.assertEqual(len(by), 6)
        # home = Shick: margin > 0
        self.assertEqual(by[("Kalshi", "SHI", "yes")].side, "gt")
        self.assertEqual(by[("Kalshi", "SHI", "no")].side, "lt")
        self.assertEqual(by[("Kalshi", "SMI", "yes")].side, "lt")
        self.assertEqual(by[("Kalshi", "SMI", "no")].side, "gt")
        self.assertEqual(by[("Polymarket", "02", "long")].side, "lt")       # long = Smith (away)
        self.assertEqual(by[("Polymarket", "02", "short")].label, "Shick wins")
        self.assertTrue(all(c.tie_half and c.line == 0 and c.axis == "margin" for c in g.contracts))

    def test_finds_the_1102_smith_shick_arb(self):
        g = self._game()
        for c in g.contracts:
            if c.venue == "Polymarket" and c.ref["side"] == "short":
                c.book = Book(asks=[(0.46, 449)])                  # Shick wins at 0.46
            elif c.venue == "Kalshi" and c.ref["ticker"].endswith("SMI") and c.ref["side"] == "yes":
                c.book = Book(asks=[(0.02, 449)])                  # Smith wins at 0.02
        arbs, _ = engine.find([g], cross_venue_only=True)
        self.assertEqual(len(arbs), 1)
        o = arbs[0]
        self.assertEqual(o.guaranteed, 1.0)
        self.assertEqual({o.a.label, o.b.label}, {"Shick wins", "Smith wins"})
        s = o.at_outlay(20)
        self.assertGreater(s.profit, 15)

    def test_same_player_both_venues_is_not_an_arb(self):
        g = self._game()
        for c in g.contracts:
            if c.label == "Smith wins":
                c.book = Book(asks=[(0.10, 100)])
        self.assertEqual(engine.find([g])[0], [])


class LiveFlagTests(unittest.TestCase):
    def test_live_flag_beats_start_time(self):
        now = dt.datetime.now(dt.timezone.utc)
        late = Game("A", "B", now - dt.timedelta(hours=7), sport="tennis", uid="x", live=True)
        not_yet = Game("A", "B", now - dt.timedelta(hours=1), sport="tennis", uid="y", live=False)
        nfl = Game("KC", "DEN", now - dt.timedelta(hours=1))
        self.assertTrue(late.started(now))
        self.assertFalse(not_yet.started(now))
        self.assertTrue(nfl.started(now))

    def test_in_play_filter_uses_live_flag(self):
        now = dt.datetime.now(dt.timezone.utc)
        games = [Game("A", "B", now - dt.timedelta(hours=7), sport="tennis", uid="late", live=True),
                 Game("C", "D", now - dt.timedelta(hours=1), sport="tennis", uid="slot-passed", live=False),
                 Game("KC", "DEN", now - dt.timedelta(hours=1)),
                 Game("NE", "MIA", now - dt.timedelta(hours=6))]
        snap = scanner.Snapshot(games)
        seen = {}

        def fake_find(gs, **kw):
            seen["keys"] = [g.key for g in gs]
            return [], []
        with mock.patch.object(scanner, "snapshot", return_value=snap) as sp, \
                mock.patch.object(engine, "find", side_effect=fake_find):
            trader._tick(None, trader.Config(in_play_only=True, sports=("nfl", "tennis")), {}, 8, log=lambda *_: None)
        self.assertEqual(sp.call_args.kwargs["sports"], ("nfl", "tennis"))
        self.assertEqual(seen["keys"], ["late", "KC@DEN"])


class SnapshotTests(unittest.TestCase):
    def test_sports_pick_the_fetchers(self):
        g = tennis.build(tennis.match(EVENT, WINNER, KEV), K)
        with mock.patch.object(tennis, "fetch", return_value={g.key: g}) as tf, \
                mock.patch("arbscan.polymarket.fetch", return_value={}) as pf, \
                mock.patch("arbscan.kalshi.fetch", return_value={}) as kf:
            snap = scanner.snapshot(sports=("tennis",))
        self.assertEqual([x.key for x in snap.games], [g.key])
        self.assertEqual(len(snap.games[0].contracts), 6)
        tf.assert_called_once()
        pf.assert_not_called()
        kf.assert_not_called()


if __name__ == "__main__":
    unittest.main()
