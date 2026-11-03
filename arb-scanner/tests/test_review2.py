"""Offline tests for the round-2 review fixes (speed and fills on the event path).

1. leg 1 = the venue of the pair that did NOT just move; a stale Polymarket leg is skipped in-play by default.
2. hedge retries consult the feed's in-memory book and stop when the quote is gone; --hedge-max-loss widens leg 2.
3. a live miss / failed hedge logs the Polymarket REST book next to the feed book.
4. the cash cache is debited from the fills and a failed re-read falls back to the adjusted copy.

Run:  python3 -m unittest discover -s tests
"""
import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_event import state_for  # noqa: E402
from test_trader import opp  # noqa: E402

from arbscan import brokers, polymarket, trader  # noqa: E402
from arbscan.model import Book  # noqa: E402

QUIET = dict(log=lambda *_: None)


def live_opp(a_asks=((0.45, 100),), b_asks=((0.50, 100),)):
    """Kalshi a / Polymarket b pair in a game that started an hour ago (in-play)."""
    o = opp(list(a_asks), list(b_asks))
    o.game.start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    o.a.book.fetched_at = o.b.book.fetched_at = time.time()
    return o


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = trader.connect(self.tmp.name)
        self.paper = {"Kalshi": brokers.PaperBroker("Kalshi", latency=0),
                      "Polymarket": brokers.PaperBroker("Polymarket", latency=0)}
        trader._cash.update(at=0.0, values=None, dirty=False)
        trader._skip_said.clear()

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)


class FirstLegTests(unittest.TestCase):
    def test_moved_venue_is_fresh_the_other_goes_first(self):
        o = live_opp()
        cfg = trader.Config()
        self.assertEqual(trader.first_leg(o, cfg, {}, changed={"Polymarket"}), "a")   # PM moved -> Kalshi stale
        self.assertEqual(trader.first_leg(o, cfg, {}, changed={"Kalshi"}), "b")       # Kalshi moved -> PM stale

    def test_trigger_venue_beats_the_mid_move_heuristic(self):
        o = live_opp()
        moves = {o.a.id: 0.0, o.b.id: 0.10}                 # heuristic alone: Kalshi moved less -> Kalshi first
        self.assertEqual(trader.first_leg(o, trader.Config(), moves), "a")
        self.assertEqual(trader.first_leg(o, trader.Config(), moves, changed={"Kalshi"}), "b")

    def test_both_or_none_moved_falls_back(self):
        o = live_opp()
        moves = {o.a.id: 0.0, o.b.id: 0.10}
        self.assertEqual(trader.first_leg(o, trader.Config(), moves, changed={"Kalshi", "Polymarket"}), "a")
        self.assertEqual(trader.first_leg(o, trader.Config(), moves, changed=set()), "a")

    def test_forced_leg1_still_wins(self):
        o = live_opp()
        self.assertEqual(trader.first_leg(o, trader.Config(leg1="polymarket"), {}, changed={"Kalshi"}), "b")
        self.assertEqual(trader.first_leg(o, trader.Config(leg1="kalshi"), {}, changed={"Polymarket"}), "a")


class EventPathLegTests(Base):
    def _run(self, cfg, st, keys, now):
        calls = []
        with mock.patch.object(trader.time, "time", return_value=now), \
                mock.patch.object(polymarket, "_feed", None), \
                mock.patch.object(trader, "execute", side_effect=lambda *a, **k: calls.append(k) or {}):
            trader._on_events(self.db, cfg, self.paper, st, keys, log=lambda *_: None)
        return calls

    def test_confirm_carries_the_first_sighting_venue_into_execute(self):
        o = live_opp()
        cfg = trader.Config(min_profit=0.01, min_roi=0.0, trigger="event", stale_pm="first")
        st = state_for(o.game)
        t0 = time.time()
        self.assertEqual(self._run(cfg, st, {trader.market_key(o.b)}, t0), [])          # PM moved: parked
        self.assertEqual(st["confirm"][o.key][2], {"Polymarket"})
        calls = self._run(cfg, st, set(), t0 + 0.06)                                      # confirm look, no new keys
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["changed"], {"Polymarket"})                             # -> Kalshi goes first

    def test_stale_polymarket_leg_is_skipped_in_play(self):
        o = live_opp()
        cfg = trader.Config(min_profit=0.01, min_roi=0.0, trigger="event")                # stale_pm = skip
        st = state_for(o.game)
        t0 = time.time()
        self._run(cfg, st, {trader.market_key(o.a)}, t0)                                  # Kalshi moved: PM stale
        logs = []
        with mock.patch.object(trader.time, "time", return_value=t0 + 0.06), \
                mock.patch.object(polymarket, "_feed", None), \
                mock.patch.object(trader, "execute") as ex:
            trader._on_events(self.db, cfg, self.paper, st, set(), log=logs.append)
        ex.assert_not_called()
        self.assertEqual(st["stats"]["pm_skipped"], 1)
        self.assertTrue(any("stale leg is Polymarket" in x for x in logs))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)   # no row, no cooldown

    def test_stale_pm_first_still_trades_it(self):
        o = live_opp()
        cfg = trader.Config(min_profit=0.01, min_roi=0.0, trigger="event", stale_pm="first")
        st = state_for(o.game)
        t0 = time.time()
        self._run(cfg, st, {trader.market_key(o.a)}, t0)
        calls = self._run(cfg, st, set(), t0 + 0.06)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["changed"], {"Kalshi"})

    def test_pre_game_polymarket_first_is_not_skipped(self):
        o = opp([(0.45, 100)], [(0.50, 100)])                 # game tomorrow: PM-first default, no skip
        o.a.book.fetched_at = o.b.book.fetched_at = time.time()
        cfg = trader.Config(min_profit=0.01, min_roi=0.0)
        traded = []
        with mock.patch.object(polymarket, "_feed", None), \
                mock.patch.object(trader, "execute", side_effect=lambda *a, **k: traded.append(k) or {}):
            trader._evaluate(self.db, cfg, self.paper, [o.game], {}, log=lambda *_: None, scan_errors=[])
        self.assertEqual(len(traded), 1)


class HedgeRetryTests(Base):
    def _leg2_moves(self, cfg, live):
        """Leg 1 = Polymarket fills; the Kalshi hedge book has moved to .60 (above the .50 break-even)."""
        o = opp([(0.45, 100)], [(0.50, 100)], b_bids=[(0.49, 500)])

        def move(cs):
            for x in cs:
                if x.venue == "Kalshi":
                    x.book = Book(asks=[(0.60, 500)])
                elif x.venue == "Polymarket":
                    x.book = Book(asks=[(0.50, 100)], bids=[(0.49, 500)])
        with mock.patch("arbscan.scanner.refresh", side_effect=move), \
                mock.patch("arbscan.trader.live_book", side_effect=live), \
                mock.patch("arbscan.trader.time.sleep") as slept:
            row = trader.execute(self.db, o, self.paper, cfg, budget=1000, **QUIET)
        return row, slept

    def test_quote_gone_at_the_source_stops_the_retries(self):
        cfg = trader.Config(min_profit=0.01, min_roi=0.0)
        row, slept = self._leg2_moves(cfg, live=lambda c: Book(asks=[(0.60, 500)], fetched_at=time.time()))
        fills = json.loads(row["fills"])
        self.assertEqual(sum(1 for f in fills if f["venue"] == "Kalshi"), 1)      # one IOC, not three
        self.assertEqual(row["outcome"], "exposed")
        self.assertIn("retries stopped", row["note"])
        p = json.loads(self.db.execute("SELECT probe FROM trades WHERE id=?", (row["id"],)).fetchone()[0])
        self.assertEqual(p["hedge_books"], [{"asks": [[0.60, 500]], "age": p["hedge_books"][0]["age"]}])
        slept.assert_not_called()

    def test_quote_still_there_retries_without_the_blind_pause(self):
        cfg = trader.Config(min_profit=0.01, min_roi=0.0)
        row, slept = self._leg2_moves(cfg, live=lambda c: Book(asks=[(0.50, 500)], fetched_at=time.time()))
        self.assertEqual(sum(1 for f in json.loads(row["fills"]) if f["venue"] == "Kalshi"), 3)
        slept.assert_not_called()

    def test_no_live_book_keeps_the_old_pause(self):
        cfg = trader.Config(min_profit=0.01, min_roi=0.0)
        row, slept = self._leg2_moves(cfg, live=lambda c: None)
        self.assertEqual(sum(1 for f in json.loads(row["fills"]) if f["venue"] == "Kalshi"), 3)
        self.assertEqual([c.args[0] for c in slept.call_args_list], [0.3, 0.3])

    def test_hedge_max_loss_locks_a_small_loss_instead_of_a_hold(self):
        cfg = trader.Config(min_profit=0.01, min_roi=0.0, hedge_max_loss=0.15)      # limit2 .50 + .15 >= .60
        row, _ = self._leg2_moves(cfg, live=lambda c: None)
        self.assertEqual(row["outcome"], "hedged")
        self.assertAlmostEqual(row["locked_profit"], 100 * 1.0 - (50.0 + 60.0))   # -$10 locked, not a coin flip

    def test_default_hedge_limit_is_break_even(self):
        cfg = trader.Config(min_profit=0.01, min_roi=0.0)
        row, _ = self._leg2_moves(cfg, live=lambda c: None)
        self.assertEqual(row["outcome"], "exposed")


class ProbeRestBookTests(unittest.TestCase):
    def test_book_now_adds_the_rest_snapshot_for_polymarket(self):
        o = opp([(0.45, 100)], [(0.50, 100)])
        feed_books = {o.b.ref["slug"]: (Book(asks=[(0.50, 10)], bids=[(0.49, 5)], fetched_at=time.time()), Book())}
        with mock.patch.object(polymarket, "fetch_books", return_value=feed_books), \
                mock.patch.object(polymarket, "fetch_book", return_value=(Book(asks=[(0.55, 3)]), Book())):
            plain = trader.book_now(o.b)
            withrest = trader.book_now(o.b, rest=True)
        self.assertNotIn("rest", plain)
        self.assertEqual(withrest["asks"], [[0.50, 10]])
        self.assertEqual(withrest["rest"], {"asks": [[0.55, 3]], "bid": None})

    def test_rest_failure_does_not_break_the_probe(self):
        o = opp([(0.45, 100)], [(0.50, 100)])
        feed_books = {o.b.ref["slug"]: (Book(asks=[(0.50, 10)], fetched_at=time.time()), Book())}
        with mock.patch.object(polymarket, "fetch_books", return_value=feed_books), \
                mock.patch.object(polymarket, "fetch_book", side_effect=RuntimeError("HTTP 500")):
            out = trader.book_now(o.b, rest=True)
        self.assertEqual(out["asks"], [[0.50, 10]])
        self.assertIn("HTTP 500", out["rest"]["error"])


class CashAfterTradeTests(unittest.TestCase):
    def setUp(self):
        trader._cash.update(at=0.0, values=None, dirty=False)
        self.k, self.p = mock.Mock(), mock.Mock()
        self.k.balance.return_value, self.p.balance.return_value = 25.0, 24.0
        self.k.shards = {0: 10.0, 3: 15.0}
        self.b = {"Kalshi": self.k, "Polymarket": self.p}
        self.o = opp([(0.45, 100)], [(0.50, 100)])
        self.o.a.ref["shard"] = 3

    def _fills(self):
        return [{"venue": "Kalshi", "action": "buy", "label": self.o.a.label, "qty": 10, "avg_price": 0.45, "fee": 0.10},
                {"venue": "Polymarket", "action": "buy", "label": self.o.b.label, "qty": 10, "avg_price": 0.50, "fee": 0.20},
                {"venue": "Polymarket", "action": "buy", "label": self.o.b.label, "qty": 0, "avg_price": 0, "fee": 0}]

    def test_pots_debited_from_fills_and_marked_dirty(self):
        trader.balances(self.b)
        trader.spend(self.b, self.o, self._fills())
        self.assertAlmostEqual(trader._cash["values"]["Kalshi"], 25.0 - 4.60)
        self.assertAlmostEqual(trader._cash["values"]["Polymarket"], 24.0 - 5.20)
        self.assertAlmostEqual(self.k.shards[3], 15.0 - 4.60)                 # the leg's shard, not shard 0
        self.assertAlmostEqual(self.k.shards[0], 10.0)
        self.assertTrue(trader._cash["dirty"])

    def test_dirty_cache_rereads_and_a_429_falls_back_to_the_debited_copy(self):
        trader.balances(self.b)
        trader.spend(self.b, self.o, self._fills())
        self.k.balance.side_effect = RuntimeError("Kalshi GET /portfolio/subaccounts/balances → HTTP 429")
        vals = trader.balances(self.b)                                          # re-read attempted (dirty) ...
        self.assertEqual(self.k.balance.call_count, 2)
        self.assertAlmostEqual(vals["Kalshi"], 25.0 - 4.60)                      # ... and the debited copy is kept
        self.assertTrue(trader._cash["dirty"])                                  # still dirty: next call retries
        self.k.balance.side_effect = None
        self.k.balance.return_value = 20.40
        self.assertAlmostEqual(trader.balances(self.b)["Kalshi"], 20.40)
        self.assertFalse(trader._cash["dirty"])

    def test_spend_without_a_cache_only_marks_dirty(self):
        trader.spend(self.b, self.o, self._fills())
        self.assertIsNone(trader._cash["values"])
        self.assertTrue(trader._cash["dirty"])
        self.assertAlmostEqual(self.k.shards[3], 15.0 - 4.60)


if __name__ == "__main__":
    unittest.main()
