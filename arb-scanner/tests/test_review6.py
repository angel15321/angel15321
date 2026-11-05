"""Offline tests for what the 11-05 04:55Z deploy showed was still on the trading thread (busy 100%, quotes checked
live up to 1.7 s apart):

  * a pull SENDS the cancel and moves on; the cancel is confirmed later (order stream, or a read every 0.3 s)
  * an order the private stream follows is read over REST every 3 s, not every second
  * a game we hold no quote on is re-priced on book changes at most every 0.5 s
  * settlement and balance reads run on worker threads

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_maker import QUIET, Base, pair  # noqa: E402
from test_review3 import Maker, state  # noqa: E402
from test_review4 import loop_state  # noqa: E402

from arbscan import maker, polymarket, scanner, trader  # noqa: E402

NEW = state(0.0, False, st="ORDER_STATE_NEW")
GONE = state(0.0, True, st="ORDER_STATE_CANCELED")


def stream(orders=None, alive=True):
    """A private order stream that knows `orders` ({order id: maker state dict})."""
    orders = orders or {}
    return types.SimpleNamespace(orders={k: (time.time(), {}) for k in orders}, alive=lambda: alive,
                                 state=lambda oid, c: orders.get(oid))


class PullTests(Maker):
    def test_a_pull_sends_the_cancel_and_does_not_wait_for_it(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(cancel=NEW)                           # accepted, not showing yet (the usual first read)
        logs = []
        self.assertIsNone(maker.pull(self.db, self.cfg, self.paper, q, "Kalshi ask moved", logs.append))
        self.assertEqual(b.cancel.call_count, 1)
        self.assertEqual(b.cancel.call_args.kwargs, {"wait": 0.0})             # one read, no polling loop
        self.assertTrue(q.dead)
        self.assertIn(q.oid, maker.STATE.quotes)               # still ours until the cancel shows: the slot is held
        self.assertTrue(any("pulled" in x and "Kalshi ask moved" in x for x in logs))
        self.assertFalse(any("not confirmed" in x for x in logs))              # the normal case is not a warning
        self.assertIsNone(self.db.execute("SELECT closed_at FROM quotes").fetchone()[0])

    def test_the_order_stream_confirms_it_without_another_request(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(cancel=NEW)
        b.order_state = mock.Mock(side_effect=AssertionError("REST read although the stream had the answer"))
        maker.pull(self.db, self.cfg, self.paper, q, "Kalshi ask moved", QUIET["log"])
        with mock.patch.object(polymarket, "_orders", stream({q.oid: GONE})):
            maker.poll(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertEqual(b.cancel.call_count, 1)
        row = self.db.execute("SELECT closed_at, note FROM quotes").fetchone()
        self.assertIsNotNone(row["closed_at"])
        self.assertEqual(row["note"], "pulled: Kalshi ask moved")

    def test_without_the_stream_a_read_confirms_it_and_the_cancel_is_not_sent_twice(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(cancel=NEW, order_state=GONE)
        maker.pull(self.db, self.cfg, self.paper, q, "x", QUIET["log"])
        maker.poll(self.db, self.cfg, self.paper, **QUIET)     # 0 ms later: too soon to look
        self.assertEqual(b.order_state.call_count, 0)
        q.polled_at -= maker.DEAD_POLL + 0.01
        maker.poll(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual((b.order_state.call_count, b.cancel.call_count), (1, 1))
        self.assertEqual(maker.STATE.quotes, {})

    def test_a_cancel_that_does_not_show_for_a_second_is_sent_again(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(cancel=NEW, order_state=NEW)
        maker.pull(self.db, self.cfg, self.paper, q, "x", QUIET["log"])
        q.polled_at -= 1.0
        q.cancel_at -= self.cfg.maker_poll + 0.01
        maker.poll(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual(b.cancel.call_count, 2)
        self.assertIn(q.oid, maker.STATE.quotes)

    def test_nothing_new_is_posted_on_the_game_while_its_cancel_is_out(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(cancel=NEW)
        b.post = mock.Mock()
        maker.pull(self.db, self.cfg, self.paper, q, "x", QUIET["log"])
        maker.STATE.last_post = 0
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        b.post.assert_not_called()                             # two orders of ours on one game: never
        self.assertEqual(b.cancel.call_count, 1)               # and a dead quote is not pulled over and over

    def test_a_fill_that_beat_the_cancel_is_hedged(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(cancel=NEW)
        maker.pull(self.db, self.cfg, self.paper, q, "Kalshi ask ticked up", QUIET["log"])
        b.cancel.return_value = state(20.0, True, st="ORDER_STATE_FILLED")     # it had filled before the cancel
        q.polled_at -= 1.0
        q.cancel_at -= 2.0
        maker.poll(self.db, self.cfg, self.paper, **QUIET)
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["hedged"])
        self.assertAlmostEqual(self.kalshi_bought(), 20)
        self.assertEqual(maker.STATE.quotes, {})

    def test_an_immediate_confirmation_closes_it_in_one_go(self):
        g, pm, k, q = self.posted()
        self.pm_says(cancel=GONE)
        maker.pull(self.db, self.cfg, self.paper, q, "x", QUIET["log"])
        self.assertEqual(maker.STATE.quotes, {})
        self.assertEqual(maker.STATE.stats["pulled"], 1)

    def test_the_way_out_still_waits_for_the_cancel(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(cancel=GONE)
        maker.shutdown(self.db, self.cfg, self.paper, QUIET["log"])
        self.assertEqual(b.cancel.call_args.kwargs, {})        # the broker's own wait (1.5 s), not wait=0
        self.assertEqual(maker.STATE.quotes, {})


class StreamPollTests(Maker):
    def test_an_order_the_stream_follows_is_not_read_every_second(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(order_state=NEW)
        with mock.patch.object(polymarket, "_orders", stream({q.oid: NEW})):
            q.polled_at = time.time() - 1.5                    # a second and a half: the old cadence would read
            maker.poll(self.db, self.cfg, self.paper, **QUIET)
            self.assertEqual(b.order_state.call_count, 0)
            q.polled_at = time.time() - maker.STREAM_POLL - 0.1
            maker.poll(self.db, self.cfg, self.paper, **QUIET)
            self.assertEqual(b.order_state.call_count, 1)      # the safety net, every 3 s

    def test_an_order_the_stream_has_not_shown_keeps_the_one_second_read(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(order_state=NEW)
        for feed in (stream({}), stream({q.oid: NEW}, alive=False), None):
            b.order_state.reset_mock()
            with mock.patch.object(polymarket, "_orders", feed):
                q.polled_at = time.time() - 1.1
                maker.poll(self.db, self.cfg, self.paper, **QUIET)
            self.assertEqual(b.order_state.call_count, 1, feed)

    def test_a_fill_on_the_stream_is_hedged_without_a_read(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(order_state=mock.DEFAULT)
        b.order_state = mock.Mock(side_effect=AssertionError("REST read for a fill the stream reported"))
        with mock.patch.object(polymarket, "_orders", stream({q.oid: state(20.0, True, st="ORDER_STATE_FILLED")})):
            maker.poll(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual([r["outcome"] for r in self.rows()], ["hedged"])


class EventGapTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0, trigger="event", event_gap=0.05)

    def checks(self, g, st, times):
        seen = []
        key = trader.market_key(g.contracts[0])
        with mock.patch.object(trader, "_evaluate_sliced", side_effect=lambda *a, **k: seen.append(1)):
            for t in times:
                with mock.patch.object(trader.time, "time", return_value=t):
                    trader._on_events(self.db, self.cfg, self.paper, st, {key}, QUIET["log"])
        return len(seen)

    def test_a_game_without_our_quote_is_repriced_at_most_every_half_second(self):
        g, pm, k = pair()
        t0 = time.time()
        self.assertEqual(self.checks(g, loop_state(g), [t0, t0 + 0.1, t0 + 0.2, t0 + 0.4, t0 + 0.51]), 2)

    def test_a_game_we_are_quoting_keeps_the_fast_look(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertEqual(len(maker.STATE.quotes), 1)
        t0 = time.time()
        self.assertEqual(self.checks(g, loop_state(g), [t0, t0 + 0.1, t0 + 0.2, t0 + 0.4, t0 + 0.51]), 5)

    def test_taker_mode_is_untouched(self):
        g, pm, k = pair()
        cfg = trader.Config(min_profit=0.01, trigger="event", event_gap=0.05)
        seen = []
        key = trader.market_key(g.contracts[0])
        t0 = time.time()
        st = loop_state(g)
        with mock.patch.object(trader, "_evaluate", side_effect=lambda *a, **k: seen.append(1)):
            for t in (t0, t0 + 0.1, t0 + 0.2):
                with mock.patch.object(trader.time, "time", return_value=t):
                    trader._on_events(self.db, cfg, self.paper, st, {key}, QUIET["log"])
        self.assertEqual(len(seen), 3)                         # 100 ms apart > the 50 ms gap: all three


class BackgroundTests(Base):
    def open_row(self, qa=5.0, qb=5.0):
        leg = '{"venue": "Kalshi", "ref": {"ticker": "T", "side": "yes"}}'
        self.db.execute("INSERT INTO trades (mode, detected_at, game, opp_key, leg_a, leg_b, fills, outcome, qty_a, "
                        "qty_b, cash_out, status) VALUES ('dry', ?, 'x', 'k', ?, ?, '[]', 'hedged', ?, ?, 4.0, 'open')",
                        (time.time(), leg, leg, qa, qb))
        self.db.commit()

    def wait_job(self):
        job = trader._settle["job"]
        if job is not None:
            job.result(timeout=5)

    def test_settlement_reads_run_on_a_worker_and_are_booked_at_the_next_call(self):
        self.open_row()
        ran_on = []
        trader._settled_at = 0.0
        trader._settle["job"] = None

        def result(ref, venue):
            ran_on.append(threading.current_thread().name)
            return 1.0
        with mock.patch.object(scanner, "settlement", side_effect=result):
            self.assertEqual(trader.settle(self.db, QUIET["log"], min_gap=60, background=True), 0)   # started only
            self.wait_job()
            self.assertEqual(self.db.execute("SELECT status FROM trades").fetchone()[0], "open")
            self.assertEqual(trader.settle(self.db, QUIET["log"], min_gap=60, background=True), 1)   # booked now
        self.assertTrue(ran_on and all(n.startswith("settle") for n in ran_on))
        r = self.db.execute("SELECT status, payout, pnl FROM trades").fetchone()
        self.assertEqual((r["status"], r["payout"], r["pnl"]), ("settled", 10.0, 6.0))

    def test_a_row_that_changed_while_it_was_being_read_is_not_booked_from_the_old_read(self):
        self.open_row(qa=5.0, qb=0.0)
        trader._settled_at = 0.0
        trader._settle["job"] = None
        with mock.patch.object(scanner, "settlement", return_value=1.0):
            trader.settle(self.db, QUIET["log"], min_gap=60, background=True)
            self.wait_job()
            self.db.execute("UPDATE trades SET qty_b=5.0, cash_out=7.0")      # a late hedge landed meanwhile
            self.db.commit()
            self.assertEqual(trader.settle(self.db, QUIET["log"], min_gap=60, background=True), 0)
        self.assertEqual(self.db.execute("SELECT status FROM trades").fetchone()[0], "open")

    def test_the_plain_call_still_settles_at_once(self):
        self.open_row()
        with mock.patch.object(scanner, "settlement", return_value=0.5):
            self.assertEqual(trader.settle(self.db, QUIET["log"]), 1)
        self.assertEqual(self.db.execute("SELECT status FROM trades").fetchone()[0], "settled")

    def test_a_due_balance_read_does_not_run_on_the_calling_thread(self):
        ran_on = []

        def bal():
            ran_on.append(threading.current_thread().name)
            return 50.0
        brokers_ = {"Kalshi": types.SimpleNamespace(balance=bal), "Polymarket": types.SimpleNamespace(balance=bal)}
        trader._cash.update(at=time.time() - 30, values={"Kalshi": 40.0, "Polymarket": 40.0}, dirty=False, bg=True,
                            thread=None, gen=0)
        try:
            got = trader.balances(brokers_, background=True)
            self.assertEqual(got, {"Kalshi": 40.0, "Polymarket": 40.0})        # the cached pots, at once
            trader._cash["thread"].join(5)
            self.assertEqual(set(ran_on), {"cash"})
            self.assertEqual(trader._cash["values"], {"Kalshi": 50.0, "Polymarket": 50.0})
            self.assertFalse(trader._cash["dirty"])
        finally:
            trader._cash.update(at=0.0, values=None, bg=False, thread=None)

    def test_a_balance_read_older_than_a_fill_is_dropped(self):
        brokers_ = {"Kalshi": types.SimpleNamespace(balance=lambda: 50.0)}
        trader._cash.update(at=time.time(), values={"Kalshi": 30.0}, dirty=True, gen=7)
        try:
            trader._reread_cash(brokers_, 6)                   # started before the fill that made it gen 7
            self.assertEqual(trader._cash["values"], {"Kalshi": 30.0})
            self.assertTrue(trader._cash["dirty"])
            trader._reread_cash(brokers_, 7)
            self.assertEqual(trader._cash["values"], {"Kalshi": 50.0})
        finally:
            trader._cash.update(at=0.0, values=None, dirty=False)

    def test_outside_the_loop_balances_are_read_as_before(self):
        calls = []
        brokers_ = {"Kalshi": types.SimpleNamespace(balance=lambda: calls.append(1) or 9.0)}
        trader._cash.update(at=time.time() - 30, values={"Kalshi": 1.0}, dirty=False, bg=False)
        try:
            self.assertEqual(trader.balances(brokers_, background=True), {"Kalshi": 9.0})
            self.assertEqual(len(calls), 1)
        finally:
            trader._cash.update(at=0.0, values=None)

    def test_the_timing_line_says_where_the_time_went(self):
        tm = dict(trader._timing(), at=100.0, busy=30.0, block=0.04, t_events=18.0, t_apply=6.0, t_poll=3.0,
                  t_guard=0.6, t_house=0.1)
        line = trader.timing_text(tm, 160.0)
        self.assertIn("busy 50%", line)
        self.assertIn("pricing book changes 30%", line)
        self.assertIn("applying scans 10%", line)
        self.assertIn("order checks 5%", line)
        self.assertIn("quote checks 1%", line)
        self.assertNotIn("housekeeping", line)                 # under half a percent: left out


if __name__ == "__main__":
    unittest.main()
