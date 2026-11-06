"""Offline tests for the trading loop's sleep clock.

11-05 13:02Z on 86f7062, the first minutes with quotes resting: `294,683 wakes`, `busy 98%`, `quote checks 62%`.
The loop slept until `polled_at + maker_poll` (1 s) for each resting quote while poll() itself read a
stream-followed order only every STREAM_POLL (3 s): two seconds in three that time had passed, the wait returned
at once and the loop span. The live quote checks were as close together as could be (95th 0 ms), but one core was
burnt and the feed threads had to fight the trading thread for the interpreter.

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_maker import QUIET  # noqa: E402
from test_review3 import Maker  # noqa: E402
from test_review4 import loop_state  # noqa: E402
from test_review6 import NEW, stream  # noqa: E402

from arbscan import maker, polymarket, trader  # noqa: E402


class SleepClockTests(Maker):
    def test_the_loop_sleeps_until_the_poll_s_own_next_read(self):
        g, pm, k, q = self.posted()
        now = time.time()
        with mock.patch.object(polymarket, "_orders", stream({q.oid: NEW})):
            q.polled_at = now - 1.5                            # past the 1-s clock, not yet the stream's 3 s
            self.assertGreater(maker.next_poll(self.cfg), now + 1.0)
            self.assertAlmostEqual(maker.next_poll(self.cfg), q.polled_at + maker.STREAM_POLL)
        for feed in (stream({}), stream({q.oid: NEW}, alive=False), None):     # the stream does not follow it
            with mock.patch.object(polymarket, "_orders", feed):
                self.assertAlmostEqual(maker.next_poll(self.cfg), q.polled_at + self.cfg.maker_poll)
        q.dead = True
        with mock.patch.object(polymarket, "_orders", stream({q.oid: NEW})):
            self.assertAlmostEqual(maker.next_poll(self.cfg), q.polled_at + maker.DEAD_POLL)
        maker.STATE.quotes.clear()
        self.assertEqual(maker.next_poll(self.cfg), float("inf"))

    def test_whenever_the_clock_is_due_the_poll_reads(self):
        """The two must agree: a clock that is due while poll() does nothing is the spin."""
        g, pm, k, q = self.posted()
        b = self.pm_says(order_state=NEW)
        for feed in (stream({q.oid: NEW}), stream({}), None):
            for age in (0.5, 1.5, 2.5, 3.5):
                b.order_state.reset_mock()
                with mock.patch.object(polymarket, "_orders", feed):
                    q.polled_at = time.time() - age
                    due = maker.next_poll(self.cfg) <= time.time()
                    maker.poll(self.db, self.cfg, self.paper, **QUIET)
                    self.assertEqual(b.order_state.call_count, 1 if due else 0, (feed, age))
                    self.assertGreater(maker.next_poll(self.cfg), time.time())      # never left due

    def test_a_resting_quote_does_not_spin_the_loop(self):
        g, pm, k, q = self.posted()
        self.pm_says(order_state=NEW)
        cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0, trigger="event")
        with tempfile.TemporaryDirectory() as d:
            cfg.kill_file = os.path.join(d, "STOP")
            ticks = []

            def tick(*a, **kw):
                ticks.append(time.time())
                if len(ticks) == 2:
                    open(cfg.kill_file, "w").close()           # the third pass through the scan branch stops

            st = loop_state(g)
            with mock.patch.object(polymarket, "_orders", stream({q.oid: NEW})), \
                    mock.patch.object(trader, "live_book", return_value=None), \
                    mock.patch.object(trader, "settle"), mock.patch.object(trader, "_tick", side_effect=tick), \
                    mock.patch.object(maker, "guard", wraps=maker.guard) as guard:
                q.polled_at = time.time() - 1.2                # as after a minute of resting: past the 1-s clock
                trader._loop_on(self.db, cfg, self.paper, 1, 1, False, QUIET["log"], st, trader.Trigger(),
                                "dry", None)
        self.assertGreaterEqual(ticks[-1] - ticks[0], 0.9)     # it ran a second or more ...
        self.assertLess(guard.call_count, 50)                  # ... in a handful of passes (it was tens of thousands)
        self.assertLess(st["stats"]["wakes"], 50)

    def test_a_game_that_left_the_list_does_not_keep_a_clock(self):
        g, pm, k, q = self.posted()
        st = loop_state(g)
        st["due"]["nba:GONE@AWAY"] = time.time() - 5           # its game is in no scan any more
        st["due"][g.key] = time.time() + 30
        trader._on_events(self.db, self.cfg, self.paper, st, set(), QUIET["log"])
        self.assertEqual(list(st["due"]), [g.key])


class ChangeLagTests(Maker):
    """What the 60-s line should be judged on: a book changed -> the resting quotes were checked against it."""

    def test_the_trigger_remembers_when_its_oldest_change_arrived(self):
        t = trader.Trigger()
        self.assertEqual(t.take(), set())
        self.assertEqual(t.taken_since, 0.0)
        t0 = time.time()
        t.mark("a")
        time.sleep(0.03)
        t.mark("b")                                            # a later change does not move the clock
        self.assertEqual(t.take(), {"a", "b"})
        self.assertGreaterEqual(t.taken_since, t0)
        self.assertLess(t.taken_since, t0 + 0.02)
        self.assertEqual(t.take(), set())
        self.assertEqual(t.taken_since, 0.0)

    def test_the_guard_logs_how_long_a_change_waited(self):
        g, pm, k, q = self.posted()
        maker.STATE.changed_at = time.time() - 0.040
        with mock.patch.object(trader, "live_book", return_value=None):
            maker.guard(self.db, self.cfg, self.paper, **QUIET)
            maker.guard(self.db, self.cfg, self.paper, **QUIET)        # no new change: nothing more is logged
        self.assertEqual(len(maker.STATE.lags), 1)
        self.assertAlmostEqual(maker.STATE.lags[0], 0.040, delta=0.02)
        self.assertEqual(maker.STATE.changed_at, 0.0)
        line = maker.stats_text()
        self.assertIn("book change -> quote check:", line)
        self.assertIn("1 changes", line)
        self.assertEqual(maker.STATE.lags, [])                         # the 60-s line starts a new count

    def test_the_loop_hands_a_book_change_to_the_guard_and_not_a_scan_wake(self):
        g, pm, k, q = self.posted()
        cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0, trigger="event")
        for keys, counted in (({trader.market_key(k)}, 1), ({trader.SCAN_KEY}, 0)):
            maker.STATE.lags, maker.STATE.changed_at = [], 0.0
            with tempfile.TemporaryDirectory() as d:
                cfg.kill_file = os.path.join(d, "STOP")
                trig = trader.Trigger()
                ticks = []

                def tick(*a, **kw):
                    ticks.append(1)
                    if len(ticks) == 1:
                        for key in keys:
                            trig.mark(key)                     # a book changes right after the first scan
                    open(cfg.kill_file, "w").close()

                with mock.patch.object(polymarket, "_orders", stream({q.oid: NEW})), \
                        mock.patch.object(trader, "live_book", return_value=None), \
                        mock.patch.object(trader, "settle"), mock.patch.object(trader, "_tick", side_effect=tick), \
                        mock.patch.object(trader, "_evaluate_sliced", return_value=None):
                    trader._loop_on(self.db, cfg, self.paper, 1, 1, False, QUIET["log"], loop_state(g), trig,
                                    "dry", None)
            self.assertEqual(len(maker.STATE.lags), counted, keys)
            if counted:
                self.assertLess(maker.STATE.lags[0], 0.5)


if __name__ == "__main__":
    unittest.main()
