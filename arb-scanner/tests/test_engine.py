"""Offline tests for fee math, arb detection and sizing.

Run:  python3 -m unittest discover -s tests
"""
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbscan import engine, fees, kalshi  # noqa: E402
from arbscan.model import Book, Contract, Game  # noqa: E402
from arbscan.teams import from_code, from_name  # noqa: E402

K = fees.kalshi_model("quadratic_with_maker_fees", 1)
P = fees.FeeModel("Polymarket", 0.03, round_fee_to=0.00001)
FREE = fees.FeeModel("Free", 0.0)


def c(venue, axis, side, line, asks, fee=FREE, kind="spread", tie=False, tid=None, min_size=1):
    return Contract(venue=venue, game="KC@DEN", axis=axis, side=side, line=line, kind=kind,
                    label=f"{side}{line}", how="", fee=fee, tie_half=tie, min_size=min_size,
                    ref={"ticker": tid or f"{venue}{side}{line}", "side": "yes"} if venue == "Kalshi"
                    else {"token_id": tid or f"t{side}{line}", "token0": tid or f"t0{side}{line}",
                          "outcome": 0, "market_id": "m"},
                    book=Book(asks=asks))


def game(*contracts):
    g = Game("KC", "DEN", dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
    g.contracts = list(contracts)
    return g


class FeeTests(unittest.TestCase):
    def test_kalshi_formula_and_cent_rounding(self):
        # 100 contracts at 50c: 0.07*100*.25 = $1.75 exactly
        n, f = K.order_total([(0.50, 100)])
        self.assertAlmostEqual(n, 50.0)
        self.assertAlmostEqual(f, 1.75)
        # 1 contract at 50c: model fee 0.0175 -> outlay 0.5175 rounds up to 0.52
        n, f = K.order_total([(0.50, 1)])
        self.assertAlmostEqual(n + f, 0.52)

    def test_polymarket_matches_docs_table(self):
        # Docs (sports rate 0.05): 100 shares @ 50c -> $1.25; @ 10c -> $0.45
        p5 = fees.FeeModel("Polymarket", 0.05, round_fee_to=0.00001)
        self.assertAlmostEqual(p5.order_total([(0.50, 100)])[1], 1.25)
        self.assertAlmostEqual(p5.order_total([(0.10, 100)])[1], 0.45)

    def test_symmetric(self):
        self.assertAlmostEqual(P.per_contract(0.3), P.per_contract(0.7))


class CoverageTests(unittest.TestCase):
    def test_exact_complement(self):
        a = c("Kalshi", "total", "gt", 44.5, [(0.5, 10)])
        b = c("Polymarket", "total", "lt", 44.5, [(0.5, 10)])
        g, mid = engine.guaranteed_payout(a, b)
        self.assertEqual(g, 1.0)
        self.assertIsNone(mid)

    def test_middle(self):
        a = c("Kalshi", "total", "gt", 44.5, [(0.5, 10)])
        b = c("Polymarket", "total", "lt", 46.5, [(0.5, 10)])
        g, mid = engine.guaranteed_payout(a, b)
        self.assertEqual(g, 1.0)
        self.assertEqual(mid, (45, 46))

    def test_gap_is_not_arb(self):
        a = c("Kalshi", "total", "gt", 46.5, [(0.40, 10)])
        b = c("Polymarket", "total", "lt", 44.5, [(0.40, 10)])
        arbs, _ = engine.find([game(a, b)])
        self.assertEqual(arbs, [])

    def test_moneyline_tie_pays_half_each(self):
        home = c("Kalshi", "margin", "gt", 0.0, [(0.5, 10)], kind="ml", tie=True)
        away = c("Polymarket", "margin", "lt", 0.0, [(0.5, 10)], kind="ml", tie=True)
        self.assertEqual(engine.guaranteed_payout(home, away)[0], 1.0)

    def test_ml_plus_spread_needs_tie_cover(self):
        # home -0.5 (needs home win) + away ML: on a tie -> 0 + 0.5
        home_m05 = c("Kalshi", "margin", "gt", 0.5, [(0.4, 10)])
        away_ml = c("Polymarket", "margin", "lt", 0.0, [(0.4, 10)], kind="ml", tie=True)
        # b.line (0) < a.line (0.5) -> rejected outright
        self.assertEqual(engine.find([game(home_m05, away_ml)])[0], [])
        # home +0.5 (home doesn't lose) + away ML: tie -> 1 + 0.5
        home_p05 = c("Kalshi", "margin", "gt", -0.5, [(0.4, 10)])
        self.assertEqual(engine.guaranteed_payout(home_p05, away_ml)[0], 1.0)


class SizingTests(unittest.TestCase):
    def test_depth_walk_stops_when_unprofitable(self):
        a = c("Kalshi", "total", "gt", 44.5, [(0.45, 100), (0.50, 100), (0.60, 100)])
        b = c("Polymarket", "total", "lt", 44.5, [(0.50, 150), (0.55, 100)])
        arbs, _ = engine.find([game(a, b)])
        self.assertEqual(len(arbs), 1)
        s = arbs[0].max
        # pairs: 100 @ .95 ; 50 @ 1.00 (not < 1) -> stop at 100
        self.assertEqual(s.qty, 100)
        self.assertAlmostEqual(s.profit, 5.0)

    def test_fees_can_kill_an_arb(self):
        a = c("Kalshi", "total", "gt", 44.5, [(0.49, 100)], fee=K)
        b = c("Polymarket", "total", "lt", 44.5, [(0.50, 100)], fee=P)
        # raw 99c, fees 1.75c + 0.75c -> 101.5c
        self.assertEqual(engine.find([game(a, b)])[0], [])

    def test_outlay_cap(self):
        a = c("Kalshi", "total", "gt", 44.5, [(0.40, 1000)])
        b = c("Polymarket", "total", "lt", 44.5, [(0.40, 1000)])
        s = engine.size(a, b, 1.0, max_outlay=80)
        self.assertEqual(s.qty, 100)
        self.assertLessEqual(s.outlay, 80)

    def test_polymarket_min_size(self):
        a = c("Kalshi", "total", "gt", 44.5, [(0.40, 3)])
        b = c("Polymarket", "total", "lt", 44.5, [(0.40, 100)], min_size=5)
        self.assertEqual(engine.size(a, b, 1.0).qty, 0)

    def test_same_market_pairs_skipped(self):
        a = c("Kalshi", "total", "gt", 44.5, [(0.40, 10)], tid="X")
        b = c("Kalshi", "total", "lt", 44.5, [(0.40, 10)], tid="X")
        self.assertEqual(engine.find([game(a, b)])[0], [])


class TeamTests(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(kalshi.split_teams("KCDEN"), ("KC", "DEN"))
        self.assertEqual(kalshi.split_teams("LACLAR"), ("LAC", "LAR"))
        self.assertEqual(kalshi.split_teams("NONYG"), ("NO", "NYG"))
        self.assertEqual(from_code("jac"), "JAX")
        self.assertEqual(from_code("la"), "LAR")
        self.assertEqual(from_name("Chiefs"), "KC")
        self.assertEqual(from_name("49ers"), "SF")


if __name__ == "__main__":
    unittest.main()
