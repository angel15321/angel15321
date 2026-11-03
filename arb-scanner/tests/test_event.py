"""Offline tests for the event trigger: book changes re-check their game, arbs confirm before trading.

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_trader import opp  # noqa: E402

from arbscan import brokers, polymarket, trader  # noqa: E402


def state_for(*games):
    s = {"games": list(games), "index": {}, "moves": {}, "confirm": {}, "due": {}, "last_eval": {},
         "stats": {"wakes": 0, "evals": 0, "seen": 0, "blips": 0, "confirmed": 0}, "stats_at": time.time()}
    for g in games:
        for c in g.contracts:
            s["index"][c.ref["slug"] if c.venue == "Polymarket" else c.ref["ticker"]] = g
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = trader.connect(self.tmp.name)
        self.paper = {"Kalshi": brokers.PaperBroker("Kalshi", latency=0),
                      "Polymarket": brokers.PaperBroker("Polymarket", latency=0)}
        trader._cash.update(at=0.0, values=None)
        self.cfg = trader.Config(min_profit=0.01, min_roi=0.0, trigger="event", event_gap=0.05, confirm_s=0.03)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)


class TriggerTests(unittest.TestCase):
    def test_mark_from_threads_take_once(self):
        t = trader.Trigger()
        ths = [threading.Thread(target=lambda i=i: t.mark(f"m{i % 5}")) for i in range(50)]
        for x in ths:
            x.start()
        for x in ths:
            x.join()
        self.assertTrue(t.event.is_set())
        self.assertEqual(t.take(), {f"m{i}" for i in range(5)})
        self.assertFalse(t.event.is_set())
        self.assertEqual(t.take(), set())


class EventPathTests(Base):
    def _opp(self):
        o = opp([(0.45, 100)], [(0.50, 100)])
        o.a.book.fetched_at = o.b.book.fetched_at = time.time()
        return o

    def _run(self, st, keys, now):
        traded = []
        with mock.patch.object(trader.time, "time", return_value=now), \
                mock.patch.object(polymarket, "_feed", None), \
                mock.patch.object(trader, "execute", side_effect=lambda *a, **k: traded.append(a[1]) or {}):
            trader._on_events(self.db, self.cfg, self.paper, st, keys, log=lambda *_: None)
        return traded

    def test_first_sighting_parks_then_confirms(self):
        o = self._opp()
        st = state_for(o.game)
        key = next(iter(st["index"]))
        t0 = time.time()
        self.assertEqual(self._run(st, {key}, t0), [])              # parked, not traded
        self.assertEqual(st["stats"]["seen"], 1)
        self.assertAlmostEqual(st["due"][o.game.key], t0 + 0.03)
        for c in (o.a, o.b):
            c.book.fetched_at = t0 + 0.06
        self.assertEqual(len(self._run(st, set(), t0 + 0.06)), 1)  # still there at the confirm look -> trade
        self.assertEqual(st["stats"]["confirmed"], 1)

    def test_blip_is_dropped(self):
        o = self._opp()
        st = state_for(o.game)
        key = next(iter(st["index"]))
        t0 = time.time()
        self._run(st, {key}, t0)
        o.b.book.asks = [(0.80, 100)]                               # the gap closed before the confirm look
        self.assertEqual(self._run(st, {key}, t0 + 0.06), [])
        self.assertEqual(st["stats"]["blips"], 1)
        self.assertEqual(st["confirm"], {})

    def test_game_rechecked_at_most_every_event_gap(self):
        o = self._opp()
        o.b.book.asks = [(0.80, 100)]                               # no arb: just count the checks
        st = state_for(o.game)
        key = next(iter(st["index"]))
        t0 = time.time()
        for dt_ in (0.0, 0.01, 0.02, 0.03):
            self._run(st, {key}, t0 + dt_)
        self.assertEqual(st["stats"]["evals"], 1)
        self.assertAlmostEqual(st["due"][o.game.key], t0 + 0.05)    # deferred, not dropped
        self._run(st, set(), t0 + 0.05)
        self.assertEqual(st["stats"]["evals"], 2)

    def test_unknown_market_ignored(self):
        o = self._opp()
        st = state_for(o.game)
        self._run(st, {"not-ours"}, time.time())
        self.assertEqual(st["stats"]["evals"], 0)


class ScanModeTests(Base):
    def test_scan_mode_trades_without_confirm(self):
        o = opp([(0.45, 100)], [(0.50, 100)])
        o.a.book.fetched_at = o.b.book.fetched_at = time.time()
        cfg = trader.Config(min_profit=0.01, min_roi=0.0)
        traded = []
        with mock.patch.object(polymarket, "_feed", None), \
                mock.patch.object(trader, "execute", side_effect=lambda *a, **k: traded.append(a[1]) or {}):
            trader._evaluate(self.db, cfg, self.paper, [o.game], {}, log=lambda *_: None, scan_errors=[])
        self.assertEqual(len(traded), 1)


if __name__ == "__main__":
    unittest.main()
