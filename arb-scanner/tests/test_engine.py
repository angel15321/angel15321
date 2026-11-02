"""Offline tests for fee math, arb detection and sizing.

Run:  python3 -m unittest discover -s tests
"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbscan import engine, fees, kalshi, polymarket  # noqa: E402
from arbscan.model import Book, Contract, Game  # noqa: E402
from arbscan.teams import from_code, from_name  # noqa: E402

K = fees.kalshi_model("quadratic_with_maker_fees", 1)
P = fees.poly_model({"feeCoefficient": 0.0695})
FREE = fees.FeeModel("Free", 0.0)


def c(venue, axis, side, line, asks, fee=FREE, kind="spread", tie=False, tid=None, min_size=1, pm_side="long"):
    slug = tid or f"asc-nfl-kc-den-{side}{line}"
    return Contract(venue=venue, game="KC@DEN", axis=axis, side=side, line=line, kind=kind,
                    label=f"{side}{line}", how="", fee=fee, tie_half=tie, min_size=min_size,
                    ref={"ticker": tid or f"{venue}{side}{line}", "side": "yes"} if venue == "Kalshi"
                    else {"ticker": slug, "slug": slug, "side": pm_side, "market_id": "m"},
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

    def test_polymarket_us_matches_docs(self):
        # docs.polymarket.us/fees: 100-lot @ 50c -> $1.74, @ 1c -> $0.07, @ 97c -> $0.20 (cent rounding)
        self.assertAlmostEqual(P.order_total([(0.50, 100)])[1], 1.74)
        self.assertAlmostEqual(P.order_total([(0.01, 100)])[1], 0.07)
        self.assertAlmostEqual(P.order_total([(0.97, 100)])[1], 0.20)
        self.assertAlmostEqual(fees.poly_model({}).rate, 0.0695)

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
        a = c("Polymarket", "margin", "gt", 0.0, [(0.40, 10)], tid="S", kind="ml", tie=True)
        b = c("Polymarket", "margin", "lt", 0.0, [(0.40, 10)], tid="S", kind="ml", tie=True, pm_side="short")
        self.assertEqual(engine.find([game(a, b)])[0], [])


def _side(abbr, long, ordering=None):
    return {"long": long, "team": {"abbreviation": abbr, "ordering": ordering}}


class PolymarketUSParseTests(unittest.TestCase):
    EVENT = {"slug": "nfl-kc-den-2026-11-01", "startTime": "2026-11-01T21:25:00Z", "live": True}

    def test_moneyline_long_is_listed_team(self):
        m = {"slug": "aec-nfl-kc-den-2026-11-01", "sportsMarketType": "football_team_full_game_winner",
             "active": True, "closed": False, "feeCoefficient": 0.0695, "minimumTradeQty": 0.01,
             "orderPriceMinTickSize": 0.0025, "marketSides": [_side("kc", True, "away"), _side("den", False, "home")]}
        g, cs = polymarket.parse_event(self.EVENT, [m])
        self.assertEqual(g.key, "KC@DEN")
        by = {x.ref["side"]: x for x in cs}
        # long = KC (away) wins -> home margin < 0; short = DEN wins
        self.assertEqual((by["long"].side, by["long"].line, by["long"].label), ("lt", 0.0, "KC ML"))
        self.assertEqual((by["short"].side, by["short"].label), ("gt", "DEN ML"))
        self.assertTrue(by["long"].tie_half)
        self.assertEqual((by["long"].tick, by["long"].min_size), (0.0025, 0.01))
        self.assertTrue(by["long"].ref["live"])

    def test_spread_line_maps_to_margin(self):
        # "Will KC cover -6.5" (KC away): KC wins by > 6.5 -> home margin < -6.5
        m = {"slug": "asc-nfl-kc-den-2026-11-01-neg-6pt5", "sportsMarketType": "football_team_full_game_spread",
             "line": -6.5, "active": True, "closed": False,
             "marketSides": [_side("kc", True), _side("den", False)]}
        _, cs = polymarket.parse_event(self.EVENT, [m])
        by = {x.ref["side"]: x for x in cs}
        self.assertEqual((by["long"].side, by["long"].line, by["long"].label), ("lt", -6.5, "KC -6.5"))
        self.assertEqual((by["short"].side, by["short"].label), ("gt", "DEN +6.5"))

    def test_half_game_and_whole_lines_skipped(self):
        half = {"slug": "x", "sportsMarketType": "football_team_first_half_spread", "line": -3.5,
                "marketSides": [_side("kc", True), _side("den", False)]}
        whole = {"slug": "y", "sportsMarketType": "football_team_full_game_spread", "line": -3.0,
                 "marketSides": [_side("kc", True), _side("den", False)]}
        self.assertEqual(polymarket.parse_event(self.EVENT, [half, whole])[1], [])

    def test_book_mirrors_long_into_short_and_skips_closed(self):
        book = {"marketData": {"state": "MARKET_STATE_OPEN",
                               "bids": [{"px": {"value": "0.40"}, "qty": "100"}, {"px": {"value": "0.39"}, "qty": "5"}],
                               "offers": [{"px": {"value": "0.42"}, "qty": "50"}]}}
        with mock.patch("arbscan.http.get", return_value=book):
            long, short = polymarket.fetch_book("s")
        self.assertEqual(long.asks, [(0.42, 50.0)])
        self.assertEqual(short.asks, [(0.6, 100.0), (0.61, 5.0)])
        self.assertEqual(short.bids, [(0.58, 50.0)])
        book["marketData"]["state"] = "MARKET_STATE_SUSPENDED"
        with mock.patch("arbscan.http.get", return_value=book):
            self.assertEqual(polymarket.fetch_book("s")[0].asks, [])

    def test_books_come_from_live_feed_never_rest(self):
        md = {"state": "MARKET_STATE_OPEN", "bids": [{"px": {"value": "0.40"}, "qty": "100"}],
              "offers": [{"px": {"value": "0.42"}, "qty": "50"}]}

        class FakeFeed:
            def __init__(self):
                self.asked = []

            def want(self, slugs):
                self.asked.append(list(slugs))
                return set()

            def wait_for(self, slugs, timeout=2.0):
                pass

            def get_at(self, slug):
                return (123.0, md) if slug == "live" else None
        feed = FakeFeed()
        polymarket.use_feed(feed)
        try:
            with mock.patch("arbscan.http.get", side_effect=AssertionError("REST must not be used")):
                out = polymarket.fetch_books(["live", "nobook"])
        finally:
            polymarket.use_feed(None)
        self.assertEqual(feed.asked, [["live", "nobook"]])
        self.assertEqual(out["live"][0].asks, [(0.42, 50.0)])
        self.assertEqual(out["live"][0].fetched_at, 123.0)   # age counts from when the feed sent it
        self.assertNotIn("nobook", out)          # no live book -> no price, never a stale REST one

    def test_feed_stores_latest_book_and_goes_stale(self):
        from arbscan import pmws
        f = pmws.BookFeed.__new__(pmws.BookFeed)
        f.books, f.connected, f.last_msg, f.error = {}, True, 0.0, ""
        f._handle('{"marketData": {"marketSlug": "s", "bids": [], "offers": []}}')
        f._handle('{"heartbeat": {}}')
        f._handle('{"requestId": "x", "error": "bad slug"}')
        self.assertIn("s", f.books)
        self.assertEqual(f.error, "bad slug")
        import time as _t
        f.last_msg = _t.time()
        self.assertIsNotNone(f.get("s"))
        f.last_msg = _t.time() - 60               # silent connection: books are not trusted
        self.assertIsNone(f.get("s"))

    def test_settlement_sides(self):
        with mock.patch("arbscan.http.get", return_value={"slug": "s", "settlement": 1}):
            self.assertEqual(polymarket.settlement("s", "long"), 1.0)
            self.assertEqual(polymarket.settlement("s", "short"), 0.0)
        with mock.patch("arbscan.http.get", side_effect=RuntimeError("404")):
            self.assertIsNone(polymarket.settlement("s", "long"))


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
