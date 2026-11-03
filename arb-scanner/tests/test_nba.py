"""Offline tests for NBA on both venues.

Run:  python3 -m unittest discover -s tests
"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbscan import engine, fees, kalshi, polymarket, scanner  # noqa: E402
from arbscan.model import Book, Contract, Game  # noqa: E402
from arbscan.teams import from_code  # noqa: E402

EVENT = {"slug": "nba-okc-cle-2026-11-02", "startTime": "2026-11-03T00:00:00Z", "live": True}


def side(abbr, long, ordering=None, desc=None):
    return {"long": long, "description": desc, "team": {"abbreviation": abbr, "ordering": ordering} if abbr else None}


ML = {"slug": "aec-nba-okc-cle-2026-11-02", "sportsMarketType": "basketball_team_full_game_winner", "active": True,
      "marketSides": [side("okc", True, "away"), side("cle", False, "home")]}
SPREAD = {"slug": "asc-nba-okc-cle-2026-11-02-pos-10pt5", "sportsMarketType": "basketball_team_full_game_spread",
          "line": 10.5, "active": True, "marketSides": [side("okc", True, "away"), side("cle", False, "home")]}
TOTAL = {"slug": "tsc-nba-okc-cle-2026-11-02-218pt5", "sportsMarketType": "basketball_team_full_game_total",
         "line": 218.5, "active": True, "marketSides": [side(None, True, desc="Over"), side(None, False, desc="Under")]}


class TeamTests(unittest.TestCase):
    def test_polymarket_short_codes_map_to_kalshi(self):
        for pm, k in (("ny", "NYK"), ("no", "NOP"), ("gs", "GSW"), ("pho", "PHX"), ("sa", "SAS"), ("okc", "OKC")):
            self.assertEqual(from_code(pm, "nba"), k)
        self.assertIsNone(from_code("KC", "nba"))
        self.assertEqual(from_code("KC"), "KC")             # NFL default unchanged

    def test_kalshi_event_codes_split(self):
        self.assertEqual(kalshi.split_teams("BKNNYK", "nba"), ("BKN", "NYK"))
        self.assertEqual(kalshi.split_teams("NOPMEM", "nba"), ("NOP", "MEM"))
        self.assertEqual(kalshi.split_teams("SASGSW", "nba"), ("SAS", "GSW"))
        self.assertEqual(kalshi.split_teams("OKCCLE", "nba"), ("OKC", "CLE"))

    def test_nba_key_never_collides_with_nfl(self):
        self.assertEqual(Game("DEN", "MIN", sport="nba").key, "nba:DEN@MIN")
        self.assertEqual(Game("DEN", "MIN").key, "DEN@MIN")


class PolymarketNBATests(unittest.TestCase):
    def test_ml_spread_total(self):
        g, cs = polymarket.parse_event(EVENT, [ML, SPREAD, TOTAL], sport="nba")
        self.assertEqual((g.key, g.sport), ("nba:OKC@CLE", "nba"))
        by = {(x.kind, x.ref["side"]): x for x in cs}
        self.assertEqual((by[("ml", "long")].side, by[("ml", "long")].label), ("lt", "OKC ML"))
        # OKC (away) +10.5 = home margin < 10.5
        self.assertEqual((by[("spread", "long")].side, by[("spread", "long")].line), ("lt", 10.5))
        t = by[("total", "long")]
        self.assertEqual((t.axis, t.side, t.line, t.label), ("total", "gt", 218.5, "Over 218.5"))
        self.assertEqual(by[("total", "short")].label, "Under 218.5")
        self.assertFalse(t.tie_half)

    def test_football_market_ignored_for_nba(self):
        fb = dict(ML, sportsMarketType="football_team_full_game_winner")
        self.assertEqual(polymarket.parse_event(EVENT, [fb], sport="nba")[1], [])


class KalshiNBATests(unittest.TestCase):
    def test_fetch_builds_nba_games(self):
        mk = {
            "KXNBAGAME": [{"event_ticker": "KXNBAGAME-26NOV02OKCCLE", "ticker": "KXNBAGAME-26NOV02OKCCLE-OKC",
                           "occurrence_datetime": "2026-11-03T03:00:00Z"}],
            "KXNBASPREAD": [], "KXNBATOTAL": [{"event_ticker": "KXNBATOTAL-26NOV02OKCCLE",
                                              "ticker": "KXNBATOTAL-26NOV02OKCCLE-219", "floor_strike": 218.5,
                                              "occurrence_datetime": "2026-11-03T03:00:00Z"}]}
        horizon = dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc)
        with mock.patch.object(kalshi, "_markets", side_effect=lambda s: mk[s]), \
                mock.patch.object(kalshi, "fee_models", return_value={s: polymarket.poly_model({}) for s in mk}), \
                mock.patch.object(kalshi, "fetch_books", return_value={}):
            games = kalshi.fetch(horizon, sport="nba")
        g = games["nba:OKC@CLE"]
        self.assertEqual(g.start, dt.datetime(2026, 11, 3, 0, tzinfo=dt.timezone.utc))
        kinds = sorted((c.kind, c.side, c.ref["side"]) for c in g.contracts)
        self.assertEqual(kinds, [("ml", "gt", "no"), ("ml", "lt", "yes"), ("total", "gt", "yes"), ("total", "lt", "no")])


class CrossVenueTests(unittest.TestCase):
    def test_total_arb_and_merge(self):
        _, pcs = polymarket.parse_event(EVENT, [TOTAL], sport="nba")
        pg = Game("OKC", "CLE", dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc), sport="nba")
        pg.contracts = pcs
        kg = Game("OKC", "CLE", dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc), sport="nba")
        under = [c for c in pcs if c.ref["side"] == "short"][0]                        # PM Under 218.5
        under.book = Book(asks=[(0.26, 1000)])
        over = Contract(venue="Kalshi", game=kg.key, axis="total", side="gt", line=218.5, kind="total",
                        label="Over 218.5", how="YES", fee=fees.kalshi_model("quadratic", 1),
                        ref={"ticker": "KXNBATOTAL-26NOV02OKCCLE-219", "side": "yes"}, book=Book(asks=[(0.71, 1000)]))
        kg.contracts = [over]
        with mock.patch.object(polymarket, "fetch", side_effect=lambda h, f, sp: {pg.key: pg} if sp == "nba" else {}), \
                mock.patch.object(kalshi, "fetch", side_effect=lambda h, d, sp: {kg.key: kg} if sp == "nba" else {}):
            snap = scanner.snapshot(sports=("nba",))
        self.assertEqual([g.key for g in snap.games], ["nba:OKC@CLE"])
        self.assertEqual(snap.games[0].sport, "nba")
        arbs, _ = engine.find(snap.games, cross_venue_only=True)
        self.assertEqual(len(arbs), 1)
        self.assertEqual({arbs[0].a.label, arbs[0].b.label}, {"Over 218.5", "Under 218.5"})


if __name__ == "__main__":
    unittest.main()
