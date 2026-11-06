"""Offline tests: Polymarket cancels and order reads on worker threads (maker.start_calls).

Box 11-05 13:01Z - 11-06 00:20Z: the trading thread's longest block was over 1 s in 74 of 645 minutes, and each of
the worst sat on ONE slow Polymarket call made on that thread -- a 7.4 s cancel, 2-5 s of order reads. While it was
out no other quote was checked. Miss #70: Kalshi's tick sat ~75 ms unread behind an order read. 11-06 00:32Z and
00:44Z (after the loop stopped spinning): 1.2 s and 1.7 s waits, order reads only.

  * a pull hands its cancel to a worker and returns at once; the reply is applied by poll()
  * an order read never runs on the calling thread; a slow one holds up nothing
  * a fill reported by a cancel or a read reply is hedged exactly once
  * a call still out is not sent twice; a cancel that does not show is sent again
  * the way out and force=True still act here and now

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

from test_maker import QUIET, pair, second_game  # noqa: E402
from test_review3 import Maker, state  # noqa: E402
from test_review4 import loop_state  # noqa: E402
from test_review5 import live  # noqa: E402
from test_review6 import GONE, NEW, stream  # noqa: E402

from arbscan import maker, polymarket, trader  # noqa: E402


def replies(n=1, timeout=3.0):
    """Wait until the workers have put `n` replies back."""
    end = time.time() + timeout
    while maker.STATE.results.qsize() < n and time.time() < end:
        time.sleep(0.005)
    return maker.STATE.results.qsize()


class Calls(Maker):
    def setUp(self):
        super().setUp()
        self.woken = []
        maker.start_calls(wake=lambda: self.woken.append(threading.current_thread().name))
        self.addCleanup(maker.stop_calls)

    def poll(self, **kw):
        return maker.poll(self.db, self.cfg, self.paper, **kw, **QUIET)

    def on_thread(self, reply, gate=None):
        """A broker call that notes its thread, optionally waits for `gate`, then answers `reply`."""
        seen = []

        def call(*a, **kw):
            seen.append(threading.current_thread().name)
            if gate is not None:
                gate.wait(5)
            return reply
        return mock.Mock(side_effect=call), seen


class PullTests(Calls):
    def test_a_pull_returns_while_its_cancel_is_still_out(self):
        g, pm, k, q = self.posted()
        gate = threading.Event()
        cancel, seen = self.on_thread(GONE, gate)                      # Polymarket takes its time (11-05: 7.4 s)
        self.paper["Polymarket"].cancel = cancel
        logs = []
        t0 = time.time()
        self.assertIsNone(maker.pull(self.db, self.cfg, self.paper, q, "Kalshi ask moved", logs.append))
        self.assertLess(time.time() - t0, 0.2)
        self.assertTrue(q.dead)                                        # slot held, never re-priced
        self.assertEqual(maker.STATE.stats["pulled"], 1)
        self.assertTrue(any("pulled" in x and "Kalshi ask moved" in x for x in logs))
        self.assertIsNone(self.poll())                                 # nothing back yet: nothing to apply
        self.assertEqual(len(maker.STATE.quotes), 1)
        gate.set()
        self.assertEqual(replies(), 1)
        self.assertIsNone(self.poll())
        self.assertEqual(maker.STATE.quotes, {})
        self.assertTrue(seen[0].startswith("pm-call"))                 # never on the calling thread
        self.assertEqual(cancel.call_args.kwargs, {"wait": 0.0})
        self.assertEqual(self.woken, seen)                             # the worker woke the loop
        self.assertIn("pulled: Kalshi ask moved", self.db.execute("SELECT note FROM quotes").fetchone()[0])

    def test_one_slow_cancel_does_not_hold_up_the_next_pull(self):
        g, pm, k = pair()
        g2, pm2, k2 = second_game()
        for _ in range(3):                                             # one post a pass
            maker.STATE.last_post = 0
            maker.step(self.db, self.cfg, self.paper, [g, g2], full=True, **QUIET)
        a, b = list(maker.STATE.quotes.values())
        gate = threading.Event()
        cancel, seen = self.on_thread(GONE, gate)
        self.paper["Polymarket"].cancel = cancel
        t0 = time.time()
        with mock.patch.object(trader, "live_book", return_value=live(0.70)):      # Kalshi ran on both games
            maker.guard(self.db, self.cfg, self.paper, **QUIET)
        self.assertLess(time.time() - t0, 0.2)
        self.assertTrue(a.dead and b.dead)
        end = time.time() + 2
        while len(seen) < 2 and time.time() < end:
            time.sleep(0.005)
        self.assertEqual(len(seen), 2)                                 # both cancels are out at the same time
        gate.set()
        self.assertEqual(replies(2), 2)
        self.poll()
        self.assertEqual(maker.STATE.quotes, {})

    def test_a_fill_the_cancel_reports_is_hedged_once(self):
        g, pm, k, q = self.posted()
        self.pm_says(cancel=state(6.0, True))                          # it had filled 6 before the cancel landed
        maker.pull(self.db, self.cfg, self.paper, q, "Kalshi ask moved", QUIET["log"])
        self.assertEqual(replies(), 1)
        self.assertEqual(self.rows(), [])                              # nothing booked on the pulling pass
        self.assertIsNone(self.poll())
        self.assertEqual(maker.STATE.quotes, {})
        self.assertEqual([r["outcome"] for r in self.rows()], ["hedged"])
        self.assertEqual(self.kalshi_bought(), 6)
        self.poll()
        self.assertEqual(self.kalshi_bought(), 6)

    def test_the_stream_and_a_late_reply_do_not_hedge_the_same_fill_twice(self):
        g, pm, k, q = self.posted()
        gate = threading.Event()
        cancel, _ = self.on_thread(state(6.0, True), gate)
        self.paper["Polymarket"].cancel = cancel
        maker.pull(self.db, self.cfg, self.paper, q, "Kalshi ask moved", QUIET["log"])
        with mock.patch.object(polymarket, "_orders", stream({q.oid: state(6.0, True)})):
            self.poll()                                                # the order stream says it first
        self.assertEqual(self.kalshi_bought(), 6)
        self.assertEqual(maker.STATE.quotes, {})
        gate.set()
        self.assertEqual(replies(), 1)
        self.poll()                                                    # the cancel's own reply: the quote is closed
        self.assertEqual(self.kalshi_bought(), 6)
        self.assertEqual(len(self.rows()), 1)

    def test_a_cancel_that_errors_keeps_the_quote_and_is_sent_again(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(cancel={"error": "TimeoutError: timed out"})
        logs = []
        maker.pull(self.db, self.cfg, self.paper, q, "x", logs.append)
        self.assertEqual(replies(), 1)
        maker.poll(self.db, self.cfg, self.paper, logs.append)
        self.assertEqual(len(maker.STATE.quotes), 1)
        self.assertTrue(q.dead)
        self.assertTrue(any("cancel not confirmed" in x and "timed out" in x for x in logs))
        self.assertEqual(q.cancel_out, 0.0)                            # the reply is in: the next one may go
        b.cancel = mock.Mock(return_value=GONE)
        q.polled_at = q.cancel_at = time.time() - self.cfg.maker_poll - 0.1      # a second on: send it again
        self.poll()
        self.assertEqual(replies(), 1)
        self.assertEqual(b.cancel.call_count, 1)
        self.poll()
        self.assertEqual(maker.STATE.quotes, {})

    def test_a_cancel_not_showing_yet_is_read_then_sent_again(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(order_state=NEW, cancel=NEW)                  # accepted, not showing (the usual first reply)
        maker.pull(self.db, self.cfg, self.paper, q, "x", QUIET["log"])
        self.assertEqual(replies(), 1)
        self.poll()
        self.assertTrue(q.dead)
        self.assertEqual((b.cancel.call_count, b.order_state.call_count), (1, 0))
        q.polled_at = time.time() - maker.DEAD_POLL - 0.05             # 0.3 s on: a read
        self.poll()
        self.assertEqual(replies(), 1)
        self.assertEqual((b.cancel.call_count, b.order_state.call_count), (1, 1))
        self.poll()
        q.polled_at = q.cancel_at = time.time() - self.cfg.maker_poll - 0.1      # a second on: the cancel again
        self.poll()
        self.assertEqual(replies(), 1)
        self.assertEqual(b.cancel.call_count, 2)


class ReadTests(Calls):
    def test_an_order_read_never_runs_on_the_calling_thread(self):
        g, pm, k, q = self.posted()
        read, seen = self.on_thread(NEW)
        self.paper["Polymarket"].order_state = read
        q.polled_at = time.time() - self.cfg.maker_poll - 0.1
        self.assertIsNone(self.poll())
        self.assertEqual(replies(), 1)
        self.assertTrue(seen[0].startswith("pm-call"))
        self.poll()
        self.assertEqual(len(maker.STATE.quotes), 1)                   # resting, nothing filled: it stays
        self.assertFalse(q.dead)

    def test_a_slow_read_holds_up_neither_the_poll_nor_a_pull(self):
        g, pm, k, q = self.posted()
        gate = threading.Event()
        read, seen = self.on_thread(NEW, gate)                         # 11-06 00:44Z: one read took 1.7 s
        self.paper["Polymarket"].order_state = read
        self.pm_says(cancel=GONE)
        q.polled_at = time.time() - self.cfg.maker_poll - 0.1
        t0 = time.time()
        self.poll()
        with mock.patch.object(trader, "live_book", return_value=live(0.70)):      # Kalshi runs while the read is out
            maker.guard(self.db, self.cfg, self.paper, **QUIET)
        self.assertLess(time.time() - t0, 0.2)
        self.assertTrue(q.dead)                                        # its cancel left without waiting for the read
        self.assertEqual(replies(), 1)
        self.poll()
        self.assertEqual(maker.STATE.quotes, {})
        gate.set()
        self.assertEqual(replies(), 1)
        self.poll()                                                    # the late read finds the quote closed: ignored
        self.assertEqual(maker.STATE.quotes, {})

    def test_a_fill_a_read_reports_is_hedged(self):
        g, pm, k, q = self.posted()
        self.pm_says(order_state=state(6.0, False), cancel=state(6.0, True))
        q.polled_at = time.time() - self.cfg.maker_poll - 0.1
        self.poll()
        self.assertEqual(self.rows(), [])
        self.assertEqual(replies(), 1)
        self.poll()
        self.assertEqual([r["outcome"] for r in self.rows()], ["hedged"])
        self.assertEqual(self.kalshi_bought(), 6)
        self.assertEqual(maker.STATE.quotes, {})

    def test_a_read_still_out_is_not_sent_twice(self):
        g, pm, k, q = self.posted()
        gate = threading.Event()
        read, seen = self.on_thread(NEW, gate)
        self.paper["Polymarket"].order_state = read
        for _ in range(3):
            q.polled_at = time.time() - self.cfg.maker_poll - 0.1
            self.poll()
        time.sleep(0.05)
        self.assertEqual(len(seen), 1)
        q.read_out = time.time() - maker.CALL_STALE - 0.1              # it never came back: another may go
        q.polled_at = time.time() - self.cfg.maker_poll - 0.1
        self.poll()
        end = time.time() + 2
        while len(seen) < 2 and time.time() < end:
            time.sleep(0.005)
        self.assertEqual(len(seen), 2)
        gate.set()

    def test_with_polymarket_stalling_repeats_do_not_queue_ahead_of_a_fresh_pull(self):
        g, pm, k, q = self.posted()
        gate = threading.Event()
        read, reads = self.on_thread(NEW, gate)
        cancel, cancels = self.on_thread(GONE, gate)
        self.paper["Polymarket"].order_state, self.paper["Polymarket"].cancel = read, cancel
        maker.STATE.calls_out = maker.CALL_WORKERS - 2                # four calls already hang on Polymarket
        q.polled_at = time.time() - self.cfg.maker_poll - 0.1
        self.poll()
        time.sleep(0.05)
        self.assertEqual(reads, [])                                    # no read joins the queue: two workers stay free
        maker.STATE.calls_out = maker.CALL_WORKERS + 3                 # every worker busy and a backlog behind them
        maker.pull(self.db, self.cfg, self.paper, q, "Kalshi ask moved", QUIET["log"])
        self.assertEqual(maker.STATE.calls_out, maker.CALL_WORKERS + 4)        # the quote's first cancel always goes
        q.cancel_out, q.cancel_at = 0.0, time.time() - self.cfg.maker_poll - 0.1
        q.polled_at = time.time() - maker.DEAD_POLL - 0.05
        self.poll()
        self.assertEqual(maker.STATE.calls_out, maker.CALL_WORKERS + 4)        # ... its repeat waits for a free worker
        gate.set()
        self.assertEqual(replies(), 1)
        self.poll()
        self.assertEqual(maker.STATE.calls_out, maker.CALL_WORKERS + 3)        # a reply applied frees its place
        self.assertEqual(maker.STATE.quotes, {})

    def test_the_stream_is_still_the_first_word(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(order_state=NEW, cancel=state(6.0, True))
        with mock.patch.object(polymarket, "_orders", stream({q.oid: state(6.0, False)})):
            self.poll()                                                # a fill on the stream: hedged on this pass
        self.assertEqual(b.order_state.call_count, 0)
        self.assertEqual(self.kalshi_bought(), 6)

    def test_a_failed_read_is_said_and_the_quote_stays(self):
        g, pm, k, q = self.posted()
        self.pm_says(order_state={"error": "HTTP 503"})
        q.polled_at = time.time() - self.cfg.maker_poll - 0.1
        logs = []
        self.poll()
        self.assertEqual(replies(), 1)
        maker.poll(self.db, self.cfg, self.paper, logs.append)
        self.assertTrue(any("order read failed: HTTP 503" in x for x in logs))
        self.assertEqual(len(maker.STATE.quotes), 1)


class HereAndNowTests(Calls):
    def test_the_way_out_cancels_on_its_own_thread_and_waits(self):
        g, pm, k, q = self.posted()
        cancel, seen = self.on_thread(GONE)
        self.paper["Polymarket"].cancel = cancel
        maker.shutdown(self.db, self.cfg, self.paper, QUIET["log"])
        self.assertEqual(maker.STATE.quotes, {})
        self.assertEqual(seen, [threading.current_thread().name])
        self.assertEqual(cancel.call_args.kwargs, {})                  # the broker's own wait, not wait=0

    def test_force_reads_here_and_now(self):
        g, pm, k, q = self.posted()
        read, seen = self.on_thread(NEW)
        self.paper["Polymarket"].order_state = read
        self.poll(force=True)
        self.assertEqual(seen, [threading.current_thread().name])

    def test_without_start_calls_everything_stays_on_the_caller(self):
        maker.stop_calls()
        g, pm, k, q = self.posted()
        cancel, seen = self.on_thread(GONE)
        self.paper["Polymarket"].cancel = cancel
        maker.pull(self.db, self.cfg, self.paper, q, "x", QUIET["log"])
        self.assertEqual(maker.STATE.quotes, {})                       # confirmed in the same call, as before
        self.assertEqual(seen, [threading.current_thread().name])

    def test_reset_stops_the_workers(self):
        pool = maker.STATE.pool
        maker.reset()
        self.assertIsNone(maker.STATE.pool)
        with self.assertRaises(RuntimeError):
            pool.submit(lambda: None)

    def test_the_60_s_line_says_where_the_calls_ran(self):
        g, pm, k, q = self.posted()
        self.pm_says(order_state=NEW)
        q.polled_at = time.time() - self.cfg.maker_poll - 0.1
        self.poll()
        self.assertEqual(replies(), 1)
        self.poll()
        maker.STATE.gaps = [0.001]
        line = maker.stats_text()
        self.assertIn("cancels and reads on worker threads, the slowest", line)
        self.assertEqual(maker.STATE.net["slow"], 0.0)                 # the line starts a new count


class LoopTests(Calls):
    def test_the_loop_runs_the_calls_on_workers_and_a_reply_wakes_it(self):
        g, pm, k, q = self.posted()
        cancel, seen = self.on_thread(GONE)
        self.paper["Polymarket"].cancel = cancel
        cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0, trigger="event")
        with tempfile.TemporaryDirectory() as d:
            cfg.kill_file = os.path.join(d, "STOP")
            trig = trader.Trigger()
            maker.start_calls(wake=lambda: trig.mark(maker.WAKE_KEY))
            gone_at = []

            def tick(*a, **kw):
                open(cfg.kill_file, "w").close()               # one scan, then stop at the next (a second later)

            real_drop = maker._drop

            def drop(*a, **kw):
                gone_at.append(time.time())
                return real_drop(*a, **kw)

            t0 = time.time()
            with mock.patch.object(polymarket, "_orders", stream({})), \
                    mock.patch.object(trader, "live_book", return_value=live(0.70)), \
                    mock.patch.object(trader, "settle"), mock.patch.object(trader, "_tick", side_effect=tick), \
                    mock.patch.object(maker, "_drop", side_effect=drop):
                trader._loop_on(self.db, cfg, self.paper, 1, 1, False, QUIET["log"], loop_state(g), trig, "dry", None)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertTrue(seen and seen[0].startswith("pm-call"))
        self.assertLess(gone_at[0] - t0, 0.5)                  # applied when the reply woke the loop, not a second on
        self.assertEqual(maker.STATE.lags, [])                 # a reply is not a book change

    def test_the_trading_loop_starts_and_stops_the_workers(self):
        maker.stop_calls()
        seen = {}

        def loop_on(db, cfg, brokers, days, interval, once, log, state, trig, mode, scan):
            seen["pool"], seen["wake"] = maker.STATE.pool, maker.STATE.wake
            seen["scan"] = scan

        cfg = trader.Config(maker=True, trigger="event")
        logs = []
        with mock.patch.object(trader, "_loop_on", side_effect=loop_on), \
                mock.patch.object(trader.ScanThread, "start"), mock.patch.object(trader.ScanThread, "stop"):
            trig = trader.Trigger()
            trader._loop(self.db, cfg, self.paper, 1, 1, False, logs.append, {}, trig, "dry")
            self.assertIsNotNone(seen["pool"])
            seen["wake"]()
            self.assertEqual(trig.take(), {maker.WAKE_KEY})
            self.assertIsNone(maker.STATE.pool)                # stopped on the way out
            self.assertTrue(any("worker threads" in x for x in logs))
            seen.clear()
            trader._loop(self.db, trader.Config(maker=True, trigger="event", scan_thread=False), self.paper, 1, 1,
                         False, logs.append, {}, trig, "dry")
            self.assertIsNone(seen["pool"])                    # --scan-inline keeps the old, all-on-one-thread loop


if __name__ == "__main__":
    unittest.main()
