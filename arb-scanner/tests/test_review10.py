"""Review 10 (11-06): the Polymarket event caches grew without bound.

winners._pm_event / tennis._pm_event kept the FULL event (every spread, total and prop market, ~0.8 MB parsed) for
every slug ever seen; the TTL only decided when to re-fetch, nothing dropped a finished game. The box bot was
OOM-killed at its 400 MB cgroup limit 11-06 09:30:42Z. Now each cache keeps only the markets its pairing reads, and
drops slugs older than its TTL on every write. polymarket._event_markets drops stale slugs the same way.

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbscan import polymarket, tennis, winners  # noqa: E402


def event(*types):
    return {"event": {"slug": "x", "title": "big", "series": {"blob": "z" * 1000},
                      "markets": [{"slug": f"m{i}", "sportsMarketType": t, "rules": "r" * 1000}
                                  for i, t in enumerate(types)]}}


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class WinnersCacheTests(unittest.TestCase):
    def setUp(self):
        winners._events.clear()

    def tearDown(self):
        winners._events.clear()

    def test_keeps_only_winner_markets(self):
        a_type = next(iter(winners.WIN_TYPES))
        with mock.patch.object(winners.http, "get", return_value=event(a_type, "spread", "total", "prop")):
            ev = winners._pm_event("soc-a-b-2026-11-06")
        self.assertEqual([m["sportsMarketType"] for m in ev["markets"]], [a_type])
        self.assertEqual(set(ev), {"markets"})

    def test_every_sport_winner_type_is_kept(self):
        for sp, (_, types) in winners.SPORTS.items():
            for t in types:
                self.assertIn(t, winners.WIN_TYPES, sp)

    def test_cache_hit_inside_ttl_no_refetch(self):
        clock = Clock()
        with mock.patch.object(winners.time, "time", clock), \
                mock.patch.object(winners.http, "get", return_value=event()) as get:
            winners._pm_event("a")
            clock.t += winners.EVENT_TTL - 1
            winners._pm_event("a")
        self.assertEqual(get.call_count, 1)

    def test_stale_slugs_dropped_on_write(self):
        clock = Clock()
        with mock.patch.object(winners.time, "time", clock), \
                mock.patch.object(winners.http, "get", return_value=event()):
            for i in range(50):
                winners._pm_event(f"old-{i}")          # games that finish and are never asked for again
            clock.t += winners.EVENT_TTL + 1
            winners._pm_event("new")
        self.assertEqual(list(winners._events), ["new"])

    def test_fresh_slugs_survive_a_write(self):
        clock = Clock()
        with mock.patch.object(winners.time, "time", clock), \
                mock.patch.object(winners.http, "get", return_value=event()):
            winners._pm_event("a")
            clock.t += 60
            winners._pm_event("b")
        self.assertEqual(set(winners._events), {"a", "b"})


class TennisCacheTests(unittest.TestCase):
    def setUp(self):
        tennis._events.clear()

    def tearDown(self):
        tennis._events.clear()

    def test_keeps_only_the_winner_market(self):
        with mock.patch.object(tennis.http, "get", return_value=event(tennis.WINNER, "set_winner", "total_games")):
            ev = tennis._pm_event("atp-a-b-2026-11-06")
        self.assertEqual([m["sportsMarketType"] for m in ev["markets"]], [tennis.WINNER])

    def test_stale_slugs_dropped_on_write(self):
        clock = Clock()
        with mock.patch.object(tennis.time, "time", clock), \
                mock.patch.object(tennis.http, "get", return_value=event(tennis.WINNER)):
            for i in range(30):
                tennis._pm_event(f"old-{i}")
            clock.t += tennis.EVENT_TTL + 1
            tennis._pm_event("new")
        self.assertEqual(list(tennis._events), ["new"])


class PolymarketCacheTests(unittest.TestCase):
    def setUp(self):
        polymarket._events.clear()

    def tearDown(self):
        polymarket._events.clear()

    def test_stale_slugs_dropped_on_write(self):
        clock = Clock()
        kind = next(iter(polymarket.KINDS))
        with mock.patch.object(polymarket.time, "time", clock), \
                mock.patch.object(polymarket.http, "get", return_value=event(kind)):
            for i in range(20):
                polymarket._event_markets(f"nfl-old-{i}")
            clock.t += polymarket.EVENT_TTL + 1
            ms = polymarket._event_markets("nfl-new")
        self.assertEqual(list(polymarket._events), ["nfl-new"])
        self.assertEqual(len(ms), 1)


if __name__ == "__main__":
    unittest.main()
