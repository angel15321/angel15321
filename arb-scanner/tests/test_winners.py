"""Offline tests for name-paired winner markets (soccer, college, UFC): pairing rules, the axis encoding and
the cross-venue price check.

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbscan import engine, winners  # noqa: E402
from arbscan.fees import FeeModel  # noqa: E402
from arbscan.model import Book  # noqa: E402


def side(code, name, ordering, long):
    return {"long": long, "description": name, "team": {"abbreviation": code.lower(), "name": name, "ordering": ordering}}


def pm(slug, start, *sides, markets=None):
    ev = {"slug": slug, "startTime": start, "live": None}
    wins = markets or [{"slug": f"aec-{slug}", "sportsMarketType": "ufc_fight_winner", "feeCoefficient": 0.0695,
                        "marketSides": list(sides)}]
    return ev, wins


def kalshi_event(et, **teams):
    return {et: {code: {"ticker": f"{et}-{code}", "yes_sub_title": name, "exchange_index": 0}
                 for code, name in teams.items()}}


class MatchTests(unittest.TestCase):
    def test_ufc_us_date_is_a_day_before_the_slug(self):
        ev, wins = pm("ufc-jonpar-jossou-2026-11-07", "2026-11-07T22:00:00Z",
                      side("jonpar", "Jonny Parsons", "home", True), side("jossou", "Jose Souza", "away", False))
        kev = kalshi_event("KXUFCFIGHT-26NOV06PARSOU", PAR="Jonny Parsons", SOU="Jose Souza")
        m = winners.match(ev, wins, kev)
        self.assertIsNotNone(m)
        self.assertEqual(m["kalshi"]["JONPAR"]["ticker"], "KXUFCFIGHT-26NOV06PARSOU-PAR")
        self.assertEqual((m["home"], m["away"]), ("JONPAR", "JOSSOU"))

    def test_one_shared_word_is_not_a_match(self):
        ev, wins = pm("cbb-semo-tenst-2026-11-07", "2026-11-07T19:00:00Z",
                      side("semo", "Southeast Missouri", "away", True), side("tenst", "Tennessee State", "home", False))
        kev = kalshi_event("KXNCAAMBGAME-26NOV07MIZZTNST", MIZZ="Missouri", TNST="Tennessee State")
        self.assertIsNone(winners.match(ev, wins, kev))

    def test_both_sides_must_map(self):
        ev, wins = pm("ufc-a-b-2026-11-07", "2026-11-07T22:00:00Z",
                      side("aaa", "Alex Alpha", "home", True), side("bbb", "Ben Beta", "away", False))
        kev = kalshi_event("KXUFCFIGHT-26NOV07ALPGAM", ALP="Alex Alpha", GAM="Gus Gamma")
        self.assertIsNone(winners.match(ev, wins, kev))

    def test_two_kalshi_events_fit_is_ambiguous(self):
        ev, wins = pm("ufc-a-b-2026-11-07", "2026-11-07T22:00:00Z",
                      side("aaa", "Alex Alpha", "home", True), side("bbb", "Ben Beta", "away", False))
        kev = {**kalshi_event("KXUFCFIGHT-26NOV07ALPBET", ALP="Alex Alpha", BET="Ben Beta"),
               **kalshi_event("KXUFCFIGHT-26NOV06ALPBET", ALP="Alex Alpha", BET="Ben Beta")}
        self.assertIsNone(winners.match(ev, wins, kev))

    def test_soccer_three_way_skips_the_draw(self):
        home, away = side("ars", "Arsenal FC", "home", True), side("che", "Chelsea FC", "away", True)
        markets = [{"slug": "aec-epl-che-ars-2026-11-08-ars", "sportsMarketType": "soccer_team_full_time_winner",
                    "marketSides": [home]},
                   {"slug": "aec-epl-che-ars-2026-11-08-che", "sportsMarketType": "soccer_team_full_time_winner",
                    "marketSides": [away]},
                   {"slug": "aec-epl-che-ars-2026-11-08-draw", "sportsMarketType": "soccer_team_full_time_winner",
                    "marketSides": [{"long": True, "description": "Draw"}]}]
        ev, wins = pm("epl-che-ars-2026-11-08", "2026-11-08T16:30:00Z", markets=markets)
        kev = kalshi_event("KXEPLGAME-26NOV08CHEARS", ARS="Arsenal", CHE="Chelsea", TIE="Tie")
        m = winners.match(ev, wins, kev)
        self.assertEqual(sorted(m["pm"]), ["ARS", "CHE"])
        self.assertEqual(m["kalshi"]["ARS"]["ticker"], "KXEPLGAME-26NOV08CHEARS-ARS")


class EncodingTests(unittest.TestCase):
    def _game(self):
        home, away = side("ars", "Arsenal FC", "home", True), side("che", "Chelsea FC", "away", True)
        markets = [{"slug": "aec-x-ars", "sportsMarketType": "soccer_team_full_time_winner", "marketSides": [home]},
                   {"slug": "aec-x-che", "sportsMarketType": "soccer_team_full_time_winner", "marketSides": [away]}]
        ev, wins = pm("epl-che-ars-2026-11-08", "2026-11-08T16:30:00Z", markets=markets)
        m = winners.match(ev, wins, kalshi_event("KXEPLGAME-26NOV08CHEARS", ARS="Arsenal", CHE="Chelsea", TIE="Tie"))
        return winners.build("soccer", m, FeeModel("Kalshi", 0.07))

    def test_win_and_not_win_cover_every_result_draw_included(self):
        g = self._game()
        by = {(c.venue, c.label): c for c in g.contracts}
        for team in ("Arsenal FC", "Chelsea FC"):
            a, b = by[("Polymarket", f"{team} wins")], by[("Kalshi", f"{team} not to win")]
            self.assertEqual(engine.guaranteed_payout(*sorted((a, b), key=lambda c: c.side == "lt"))[0], 1.0)
        # both "not to win" sides together: a draw pays both, every other result pays one
        x, y = by[("Kalshi", "Arsenal FC not to win")], by[("Polymarket", "Chelsea FC not to win")]
        gt, lt = (x, y) if x.side == "gt" else (y, x)
        self.assertEqual(engine.guaranteed_payout(gt, lt)[0], 1.0)

    def test_home_win_plus_away_win_is_not_an_arb(self):
        g = self._game()
        for c in g.contracts:
            c.book = Book(asks=[(0.30, 100)])          # 0.30 + 0.30 looks cheap, but a draw pays neither
        arbs, _ = engine.find([g])
        for o in arbs:
            labels = {o.a.label, o.b.label}
            self.assertNotEqual(labels, {"Arsenal FC wins", "Chelsea FC wins"})

    def test_price_check_drops_a_mismatched_pairing(self):
        g = self._game()
        for c in g.contracts:
            c.book = Book(asks=[(0.52, 10)], bids=[(0.50, 10)])
        self.assertFalse(winners.priced_apart(g))
        k = next(c for c in g.contracts if c.venue == "Kalshi" and c.label == "Arsenal FC wins")
        k.book = Book(asks=[(0.82, 10)], bids=[(0.80, 10)])     # 30c apart on the same side
        self.assertTrue(winners.priced_apart(g))


if __name__ == "__main__":
    unittest.main()
