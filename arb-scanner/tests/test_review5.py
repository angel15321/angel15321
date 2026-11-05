"""Offline tests for the pull-latency work that follows the scan thread (11-05):

  * maker.guard: the resting quotes against the LIVE books, first thing on every pass of the trading loop
  * maker.sync + sliced pricing: a full scan is applied without one long block
  * the two pricing speedups (payout cache, one cooldown read per step)

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_maker import QUIET, Base, pair  # noqa: E402
from test_review3 import Maker  # noqa: E402
from test_review4 import loop_state  # noqa: E402

from arbscan import engine, kalshi, maker, polymarket, scanner, trader  # noqa: E402
from arbscan.model import Book  # noqa: E402


def live(ask, size=100):
    return Book(asks=[(ask, size)], bids=[(round(ask - 0.02, 2), 100)], fetched_at=time.time())


class GuardTests(Maker):
    def test_guard_pulls_on_the_live_kalshi_book_while_the_cached_copy_still_looks_fine(self):
        g, pm, k, q = self.posted()                            # quoted off Kalshi 0.55
        self.assertEqual(k.book.asks[0][0], 0.55)              # the game's copy has not been re-read yet ...
        logs = []
        with mock.patch.object(trader, "live_book", return_value=live(0.62)):      # ... Kalshi is at 0.62 now
            maker.guard(self.db, self.cfg, self.paper, logs.append)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertTrue(any("Kalshi ask moved 0.55 -> 0.62" in x for x in logs))

    def test_guard_leaves_a_safe_quote_and_a_dead_one_alone(self):
        g, pm, k, q = self.posted()
        b = self.paper["Polymarket"]
        b.cancel = mock.Mock()
        with mock.patch.object(trader, "live_book", return_value=live(0.55)):
            maker.guard(self.db, self.cfg, self.paper, QUIET["log"])
        q.dead = True                                          # its cancel is already out: poll() owns it
        with mock.patch.object(trader, "live_book", return_value=live(0.90)):
            maker.guard(self.db, self.cfg, self.paper, QUIET["log"])
        b.cancel.assert_not_called()
        self.assertEqual(len(maker.STATE.quotes), 1)

    def test_no_live_book_for_a_second_pulls_the_quote(self):
        g, pm, k, q = self.posted()
        k.book.fetched_at = time.time() - 2                    # the feed has no book for the hedge any more
        logs = []
        with mock.patch.object(trader, "live_book", return_value=None):
            maker.guard(self.db, self.cfg, self.paper, logs.append)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertTrue(any("Kalshi book went stale" in x for x in logs))

    def test_the_gap_between_live_checks_is_measured_for_the_log(self):
        g, pm, k, q = self.posted()
        with mock.patch.object(trader, "live_book", return_value=live(0.55)):
            maker.guard(self.db, self.cfg, self.paper, QUIET["log"])
            maker.STATE.guard_at -= 0.25                       # the loop was busy for 250 ms
            maker.guard(self.db, self.cfg, self.paper, QUIET["log"])
        self.assertAlmostEqual(maker.STATE.guard_gap, 0.25, delta=0.05)
        self.assertIn("quotes checked live at most 2", maker.stats_text())
        self.assertEqual(maker.STATE.guard_gap, 0.0)           # per 60-s line
        maker.STATE.quotes.clear()
        maker.guard(self.db, self.cfg, self.paper, QUIET["log"])
        self.assertEqual(maker.STATE.guard_at, 0.0)            # nothing resting: no gap is counted across it

    def test_sync_follows_the_new_objects_and_trusts_the_live_book_not_the_scan_copy(self):
        g, pm, k, q = self.posted()
        g2, pm2, k2 = pair()                                   # the next scan: new objects, same markets
        k2.book.fetched_at = time.time() - 5                   # its copy of the book is 5 s old (a slow scan)
        with mock.patch.object(trader, "live_book", return_value=live(0.55)):
            maker.sync(self.db, self.cfg, self.paper, [g2], QUIET["log"])
        self.assertEqual(len(maker.STATE.quotes), 1)           # not pulled as stale: the live book is current
        self.assertIs(q.k, k2)
        self.assertIs(q.pm, pm2)
        self.assertLess(time.time() - q.k.book.fetched_at, 1.0)

    def test_sync_pulls_a_quote_whose_market_left_the_scan(self):
        g, pm, k, q = self.posted()
        logs = []
        maker.sync(self.db, self.cfg, self.paper, [], logs.append)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertTrue(any("its market left the scan" in x for x in logs))

    def test_the_loop_checks_the_quotes_before_anything_else(self):
        g, pm, k, q = self.posted()
        order = []
        cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0, trigger="event")
        with mock.patch.object(maker, "guard", side_effect=lambda *a, **kw: order.append("guard")), \
                mock.patch.object(trader, "settle", side_effect=lambda *a, **kw: order.append("settle")), \
                mock.patch.object(trader, "_tick", side_effect=lambda *a, **kw: order.append("tick")):
            trader._loop_on(self.db, cfg, self.paper, 1, 1, True, QUIET["log"], loop_state(g), trader.Trigger(),
                            "dry", None)
        self.assertEqual(order[:3], ["guard", "settle", "tick"])

    def test_scan_inline_keeps_the_old_loop(self):
        g, pm, k, q = self.posted()
        cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0, trigger="event", scan_thread=False)
        with mock.patch.object(maker, "guard", side_effect=AssertionError("guard on the old path")), \
                mock.patch.object(trader, "_evaluate_sliced", side_effect=AssertionError("slices on the old path")), \
                mock.patch.object(trader, "settle"), mock.patch.object(polymarket, "_feed", None), \
                mock.patch.object(kalshi, "_feed", None), \
                mock.patch.object(scanner, "snapshot", return_value=scanner.Snapshot([g])):
            trader._loop_on(self.db, cfg, self.paper, 1, 1, True, QUIET["log"], loop_state(g), trader.Trigger(),
                            "dry", None)
        self.assertEqual(len(maker.STATE.quotes), 1)


class SliceTests(Base):
    def games(self, n):
        out = []
        for i in range(n):
            g, pm, k = pair()
            g.away = f"T{i}"
            for x in (pm, k):
                x.ref["ticker"] += f"-{i}"
                if "slug" in x.ref:
                    x.ref["slug"] += f"-{i}"
            out.append(g)
        return out

    def test_quoted_games_first_and_slices_stay_small(self):
        gs = self.games(5)
        with mock.patch.object(trader, "SLICE_CONTRACTS", 4):  # 2 contracts a game: two games a slice
            sl = trader._slices(gs, {gs[3].key})
        self.assertEqual(sl[0][0], gs[3])                      # the game we are quoting is priced first
        self.assertEqual([len(x) for x in sl], [2, 2, 1])
        self.assertEqual(sum(len(x) for x in sl), 5)

    def test_one_big_game_is_its_own_slice(self):
        gs = self.games(2)
        with mock.patch.object(trader, "SLICE_CONTRACTS", 1):
            self.assertEqual([len(x) for x in trader._slices(gs, set())], [1, 1])

    def test_a_live_check_runs_before_every_slice(self):
        gs = self.games(4)
        order = []
        with mock.patch.object(trader, "SLICE_CONTRACTS", 4), \
                mock.patch.object(maker, "guard", side_effect=lambda *a, **k: order.append("guard")), \
                mock.patch.object(trader, "_evaluate", side_effect=lambda *a, **k: order.append(len(a[3]))):
            trader._evaluate_sliced(self.db, self.cfg, self.paper, gs, {}, QUIET["log"])
        self.assertEqual(order, ["guard", 2, "guard", 2])

    def test_a_full_scan_is_applied_in_slices_in_event_mode(self):
        gs = self.games(3)
        cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0, trigger="event")
        st = loop_state()
        logs, order = [], []
        with mock.patch.object(maker, "sync", side_effect=lambda *a, **k: order.append("sync")), \
                mock.patch.object(trader, "_evaluate_sliced", side_effect=lambda *a, **k: order.append("slices")), \
                mock.patch.object(trader, "_evaluate", side_effect=AssertionError("one long block")):
            trader._tick(self.db, cfg, self.paper, 1, logs.append, st, snap=scanner.Snapshot(gs))
        self.assertEqual(order, ["sync", "slices"])
        self.assertIn("3 games · 6 contracts · maker", logs[0])
        self.assertEqual(len(st["games"]), 3)

    def test_sliced_pricing_still_quotes_one_per_game(self):
        gs = self.games(3)
        cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0, trigger="event")
        with mock.patch.object(polymarket, "_feed", None), mock.patch.object(kalshi, "_feed", None), \
                mock.patch.object(trader, "SLICE_CONTRACTS", 2):
            for _ in range(3):
                maker.STATE.last_post = 0
                trader._tick(self.db, cfg, self.paper, 1, QUIET["log"], loop_state(), snap=scanner.Snapshot(gs))
        self.assertEqual(len(maker.STATE.quotes), 3)
        self.assertEqual(len({q.game_key for q in maker.STATE.quotes.values()}), 3)
        self.assertIn("pairs quotable", maker.stats_text())

    def test_a_pair_inside_its_cooldown_is_not_quoted(self):
        g, pm, k = pair()
        key = maker.price_pair(self.cfg, g, pm, k, 20, None).key
        self.db.execute("INSERT INTO trades (mode, detected_at, opp_key, outcome) VALUES ('dry', ?, ?, 'hedged')",
                        (time.time() - 60, key))
        self.db.commit()
        self.assertEqual(trader.recent_keys(self.db, self.cfg.cooldown_min), {key})
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertEqual(trader.recent_keys(self.db, 0.5), set())             # a 30 s cooldown has passed


class PayoutCacheTests(unittest.TestCase):
    def test_same_answer_computed_once(self):
        g, pm, k = pair()
        a, b = (pm, k) if pm.side == "gt" else (k, pm)
        engine._payouts.clear()
        first = engine.guaranteed_payout(a, b)
        with mock.patch.object(type(a), "payout", side_effect=AssertionError("computed again")):
            self.assertEqual(engine.guaranteed_payout(a, b), first)
        self.assertEqual(first[0], 1.0)

    def test_a_different_pair_is_a_different_answer(self):
        home = types.SimpleNamespace(side="gt", line=0.5, tie_half=False, payout=lambda x: 1.0 if x > 0.5 else 0.0)
        away = types.SimpleNamespace(side="lt", line=-0.5, tie_half=False, payout=lambda x: 1.0 if x < -0.5 else 0.0)
        not_home = types.SimpleNamespace(side="lt", line=0.5, tie_half=False, payout=lambda x: 1.0 if x < 0.5 else 0.0)
        self.assertEqual(engine.guaranteed_payout(home, away)[0], 0.0)        # a draw pays neither
        self.assertEqual(engine.guaranteed_payout(home, not_home)[0], 1.0)


if __name__ == "__main__":
    unittest.main()
