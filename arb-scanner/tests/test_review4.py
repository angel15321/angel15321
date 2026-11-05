"""Offline tests for the three items left open by the 11-05 review:

  * full scans run on their own thread; the trading thread applies the finished snapshot, never reads the network
    for a Kalshi book, and never trades on a game list that has gone stale
  * a halt stops every quote and arb but keeps the process up while a one-sided position can still be closed
  * a post that came back as an error is looked for (open orders + the order stream), taken over and pulled

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_maker import QUIET, Base, pair  # noqa: E402
from test_review3 import Maker, state  # noqa: E402
from test_trader import opp  # noqa: E402

from arbscan import engine, http, kalshi, maker, polymarket, rescue, scanner, trader  # noqa: E402
from arbscan.brokers import Fill  # noqa: E402


def loop_state(*games):
    s = {"games": list(games), "index": {}, "moves": {}, "confirm": {}, "due": {}, "last_eval": {},
         "stats": {"wakes": 0, "evals": 0, "seen": 0, "blips": 0, "confirmed": 0, "pm_skipped": 0},
         "stats_at": time.time()}
    for g in games:
        for c in g.contracts:
            s["index"][trader.market_key(c)] = g
    return s


def fail_rows(db, n=2, status="open", outcome="exposed", mode="dry"):
    vals = dict(mode=mode, detected_at=time.time(), game="x", opp_key="k", leg_a="{}", leg_b="{}", fills="[]",
                qty_a=5, qty_b=0, cash_out=2.0, status=status, outcome=outcome)
    for _ in range(n):
        db.execute(f"INSERT INTO trades ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
    db.commit()


class ScanThreadTests(unittest.TestCase):
    def setUp(self):
        self.cfg = trader.Config(trigger="event", min_profit=0.01)

    def test_scan_runs_on_its_own_thread_and_is_taken_once(self):
        ran_on, woke = [], threading.Event()

        def snap(*a, **k):
            ran_on.append(threading.current_thread().name)
            return scanner.Snapshot([])
        with mock.patch.object(scanner, "snapshot", side_effect=snap):
            st = trader.ScanThread(self.cfg, 1, 1, wake=woke.set)
            st.start()
            self.assertTrue(woke.wait(3))
            st.stop()
        self.assertEqual(ran_on[0], "scan")
        self.assertNotEqual(ran_on[0], threading.current_thread().name)
        self.assertTrue(st.ready())
        self.assertIsNotNone(st.take())
        self.assertIsNone(st.take())                           # one snapshot is applied once
        self.assertFalse(st.ready())

    def test_a_failed_scan_is_reported_not_raised(self):
        woke = threading.Event()
        with mock.patch.object(scanner, "snapshot", side_effect=RuntimeError("gateway down")):
            st = trader.ScanThread(self.cfg, 1, 1, wake=woke.set)
            st.start()
            self.assertTrue(woke.wait(3))
            st.stop()
        self.assertIn("gateway down", st.error)
        self.assertFalse(st.ready())

    def test_tick_applies_a_finished_snapshot_without_scanning_again(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = trader.connect(tmp.name)
        try:
            seen = []
            with mock.patch.object(scanner, "snapshot", side_effect=AssertionError("scanned on the trading thread")), \
                    mock.patch.object(trader, "_evaluate", side_effect=lambda *a, **k: seen.append(a[3])):
                trader._tick(db, self.cfg, {}, 8, log=lambda *_: None, snap=scanner.Snapshot([]))
            self.assertEqual(seen, [[]])
        finally:
            db.close()
            os.unlink(tmp.name)

    def test_the_loop_takes_its_snapshots_from_the_thread(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = trader.connect(tmp.name)
        with tempfile.TemporaryDirectory() as d:
            cfg = trader.Config(trigger="event", min_profit=0.01, kill_file=os.path.join(d, "STOP"))
            got, logs = [], []

            def tick(db_, cfg_, brokers_, days, log, state=None, snap=None):
                got.append((snap, threading.current_thread().name))
                open(cfg.kill_file, "w").close()               # one applied scan is enough: stop
            try:
                with mock.patch.object(scanner, "snapshot", return_value=scanner.Snapshot([])) as sn, \
                        mock.patch.object(polymarket, "_feed", None), mock.patch.object(kalshi, "_feed", None), \
                        mock.patch.object(polymarket, "_orders", None), mock.patch.object(trader, "_tick", side_effect=tick):
                    trader.run(db, cfg, {}, days=1, interval=1, log=logs.append)
                self.assertGreaterEqual(sn.call_count, 1)
                self.assertEqual(len(got), 1)
                self.assertIsInstance(got[0][0], scanner.Snapshot)                  # handed over, not None
                self.assertEqual(got[0][1], threading.current_thread().name)       # applied on the trading thread
                self.assertTrue(any("their own thread" in x for x in logs))
            finally:
                db.close()
                os.unlink(tmp.name)

    def test_scan_inline_keeps_the_old_path(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        db = trader.connect(tmp.name)
        with tempfile.TemporaryDirectory() as d:
            cfg = trader.Config(trigger="event", min_profit=0.01, kill_file=os.path.join(d, "STOP"), scan_thread=False)
            got = []

            def tick(db_, cfg_, brokers_, days, log, state=None, snap=None):
                got.append(snap)
                open(cfg.kill_file, "w").close()
            try:
                with mock.patch.object(polymarket, "_feed", None), mock.patch.object(kalshi, "_feed", None), \
                        mock.patch.object(polymarket, "_orders", None), mock.patch.object(trader, "_tick", side_effect=tick), \
                        mock.patch.object(trader, "ScanThread", side_effect=AssertionError("thread started")):
                    trader.run(db, cfg, {}, days=1, interval=1, log=lambda *_: None)
                self.assertEqual(got, [None])                  # _tick scans by itself
            finally:
                db.close()
                os.unlink(tmp.name)

    def test_timing_line(self):
        tm = {"scan": [0.4, 0.6], "apply": [0.02, 0.04], "busy": 6.0, "block": 0.05, "at": 100.0}
        line = trader.timing_text(tm, 160.0)
        self.assertIn("busy 10%", line)
        self.assertIn("longest block 50 ms", line)
        self.assertIn("2 scans applied in 30 ms avg / 40 ms max", line)
        self.assertIn("scan thread 500 ms avg / 600 ms max", line)
        self.assertIn("scans inline", trader.timing_text(dict(tm, scan=[], apply=[]), 160.0))


class StaleScanTests(Maker):
    def test_no_scan_for_too_long_drops_the_game_list_and_pulls_the_quotes(self):
        g, pm, k, q = self.posted()
        st = loop_state(g)
        dead = types.SimpleNamespace(take=lambda: None, ready=lambda: False, error="TimeoutError: gateway",
                                     done_at=time.time() - 60, started_at=time.time() - 120, took=0.0, stop=lambda: None)
        logs = []
        cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0, trigger="event")
        trader._loop_on(self.db, cfg, self.paper, 1, 1, True, logs.append, st, trader.Trigger(), "dry", dead)
        self.assertEqual(maker.STATE.quotes, {})                               # nothing rests on an old game list
        self.assertEqual((st["games"], st["index"]), ([], {}))                 # ... and a book change finds no game
        self.assertTrue(any("no full scan for" in x for x in logs))
        self.assertTrue(any("scan error: TimeoutError: gateway" in x for x in logs))

    def test_a_late_scan_inside_the_limit_changes_nothing(self):
        g, pm, k, q = self.posted()
        st = loop_state(g)
        slow = types.SimpleNamespace(take=lambda: None, ready=lambda: False, error="", done_at=time.time() - 3,
                                     started_at=time.time() - 120, took=0.0, stop=lambda: None)
        cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0, trigger="event")
        trader._loop_on(self.db, cfg, self.paper, 1, 1, True, lambda *_: None, st, trader.Trigger(), "dry", slow)
        self.assertEqual(len(maker.STATE.quotes), 1)
        self.assertEqual(st["games"], [g])


class TradingThreadTests(Base):
    def test_maker_mode_does_not_run_the_arb_search(self):
        g, pm, k = pair()
        logs = []
        with mock.patch.object(engine, "find", side_effect=AssertionError("arb search in maker mode")), \
                mock.patch.object(polymarket, "_feed", None), mock.patch.object(kalshi, "_feed", None):
            trader._evaluate(self.db, self.cfg, self.paper, [g], {}, logs.append, scan_errors=[])
        self.assertEqual(len(maker.STATE.quotes), 1)                           # it still quotes
        self.assertIn("1 games · 2 contracts · maker", logs[0])

    def test_kalshi_books_are_reread_from_the_feed_only(self):
        g, pm, k = pair()
        feed = types.SimpleNamespace(alive=lambda: True)
        with mock.patch.object(kalshi, "_feed", feed), mock.patch.object(polymarket, "_feed", None), \
                mock.patch.object(kalshi, "refresh") as kr:
            trader._evaluate(self.db, self.cfg, self.paper, [g], {}, lambda *_: None, scan_errors=[])
        self.assertEqual(kr.call_args.kwargs, {"rest": False})

    def test_feed_only_read_never_touches_the_network(self):
        feed = types.SimpleNamespace(alive=lambda: True, want=lambda t: set(), get=lambda t: None)
        with mock.patch.object(kalshi, "_feed", feed), \
                mock.patch.object(http, "get", side_effect=AssertionError("REST read on the trading thread")):
            self.assertEqual(kalshi.fetch_books(["KXNBAGAME-X"], rest=False), {})
        with mock.patch.object(kalshi, "_feed", feed), \
                mock.patch.object(http, "get", return_value={"orderbooks": []}) as get:
            kalshi.fetch_books(["KXNBAGAME-X"])                                # the scan thread's read still falls back
        self.assertEqual(get.call_count, 1)

    def test_throttle_does_not_sleep_holding_the_lock(self):
        host = "https://api.elections.kalshi.com/x"
        slept = []

        def nap(s):
            self.assertTrue(http._lock.acquire(blocking=False), "slept while holding the throttle lock")
            http._lock.release()
            slept.append(s)
        http._last.pop("api.elections.kalshi.com", None)
        with mock.patch.object(http.time, "sleep", side_effect=nap):
            http._throttle(host)
            http._throttle(host)
            http._throttle(host)
        self.assertEqual(len(slept), 2)                        # the first call goes at once
        self.assertAlmostEqual(slept[0], 0.08, delta=0.02)
        self.assertAlmostEqual(slept[1], 0.16, delta=0.02)     # slots queue up 80 ms apart: the spacing is kept
        http._last.pop("api.elections.kalshi.com", None)


class HaltTests(Base):
    def test_halt_stays_up_for_held_legs_then_stops(self):
        cfg = trader.Config(min_profit=0.01, on_fail="hold")
        fail_rows(self.db, 2)                                  # two held legs: the case rescue.py exists for
        logs, ticks = [], []

        def tick(*a, **k):
            ticks.append(trader._halted)
            self.db.execute("UPDATE trades SET status='settled'")          # the games end: nothing left to watch
            self.db.commit()
        with mock.patch.object(trader, "_tick", side_effect=tick), mock.patch.object(trader, "settle"):
            trader.run(self.db, cfg, self.paper, days=1, interval=0, log=logs.append)
        self.assertEqual(len(ticks), 1)                        # it kept scanning (the watcher needs the books) ...
        self.assertTrue(ticks[0])                              # ... in the halted state
        self.assertTrue(any("Halting" in x and "still watching 2 held leg(s)" in x for x in logs))
        self.assertTrue(any("no held leg left" in x for x in logs))
        self.assertEqual(trader._halted, "")                   # the flag does not outlive the loop

    def test_halt_with_nothing_held_stops_at_once(self):
        cfg = trader.Config(min_profit=0.01)
        fail_rows(self.db, 2, status="closed", outcome="unwound")
        logs = []
        with mock.patch.object(trader, "_tick") as tick, mock.patch.object(trader, "settle"):
            trader.run(self.db, cfg, self.paper, days=1, interval=0, log=logs.append)
        tick.assert_not_called()
        self.assertFalse(any("still watching" in x for x in logs))

    def test_halt_pulls_every_quote(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertEqual(len(maker.STATE.quotes), 1)
        fail_rows(self.db, 2, status="closed", outcome="unwound")
        with mock.patch.object(trader, "_tick") as tick, mock.patch.object(trader, "settle"):
            trader._loop(self.db, self.cfg, self.paper, 1, 0, False, lambda *_: None, {}, None, "dry")
        tick.assert_not_called()
        self.assertEqual(maker.STATE.quotes, {})

    def test_nothing_is_quoted_or_taken_while_halted_but_held_legs_are_checked(self):
        g, pm, k = pair()
        o = opp([(0.45, 100)], [(0.50, 100)])
        o.a.book.fetched_at = o.b.book.fetched_at = time.time()
        taker = trader.Config(min_profit=0.01, min_roi=0.0)
        logs = []
        trader._halted = "last 2 trades failed to hedge"
        try:
            with mock.patch.object(polymarket, "_feed", None), mock.patch.object(kalshi, "_feed", None), \
                    mock.patch.object(rescue, "check") as chk, mock.patch.object(trader, "execute") as ex, \
                    mock.patch.object(maker, "step") as step:
                trader._evaluate(self.db, self.cfg, self.paper, [g], {}, logs.append, scan_errors=[])     # maker
                trader._evaluate(self.db, taker, self.paper, [o.game], {}, logs.append, scan_errors=[])   # taker
            self.assertEqual(chk.call_count, 2)
            ex.assert_not_called()
            step.assert_not_called()
            self.assertIn("HALTED (last 2 trades failed to hedge): held legs only", logs[0])
        finally:
            trader._halted = ""

    def test_held_legs_counts_only_what_the_watcher_can_close(self):
        fail_rows(self.db, 1)                                  # exposed, open
        fail_rows(self.db, 1, outcome="dust")                  # cents: not worth staying up for
        fail_rows(self.db, 1, status="settled")
        fail_rows(self.db, 1, outcome="hedged")
        self.assertEqual(trader.held_legs(self.db, "dry"), 1)
        self.assertEqual(trader.held_legs(self.db, "live"), 0)


class FailedPostTests(Maker):
    def failed(self, error="timed out"):
        g, pm, k = pair()
        b = self.paper["Polymarket"]
        b.post = mock.Mock(return_value=Fill("Polymarket", "buy", pm.label, status="error", error=error))
        logs = []
        maker.step(self.db, self.cfg, self.paper, [g], log=logs.append, full=True)
        self.assertEqual(maker.STATE.quotes, {})
        return g, pm, k, b, logs

    def order(self, pm, oid="ORPH1"):
        return {"id": oid, "marketSlug": pm.ref["slug"], "intent": "ORDER_INTENT_BUY_LONG",
                "price": {"value": "0.39"}, "quantity": 20, "state": "ORDER_STATE_NEW"}

    def test_error_reply_is_looked_for_and_the_resting_order_is_pulled(self):
        g, pm, k, b, logs = self.failed()
        self.assertEqual(len(maker.STATE.suspects), 1)
        self.assertTrue(any("looking for it on Polymarket" in x for x in logs))
        b.open_orders = mock.Mock(return_value=[self.order(pm)])
        b.cancel = mock.Mock(return_value=state(0.0, True, st="ORDER_STATE_CANCELED"))
        maker.poll(self.db, self.cfg, self.paper, force=True, log=logs.append)
        self.assertEqual(maker.STATE.suspects, [])
        self.assertEqual(b.cancel.call_args.args[1], "ORPH1")                  # the order nobody knew is cancelled
        self.assertEqual(maker.STATE.quotes, {})
        row = self.db.execute("SELECT * FROM quotes WHERE order_id='ORPH1'").fetchone()
        self.assertIsNotNone(row["closed_at"])                                 # ... and in the ledger
        self.assertIn("placed through an error reply", row["note"])
        self.assertTrue(any("WAS placed" in x for x in logs))

    def test_an_order_that_filled_before_it_was_found_is_hedged(self):
        g, pm, k, b, logs = self.failed()
        b.open_orders = mock.Mock(return_value=[])                             # filled: not open any more
        feed = types.SimpleNamespace(orders={"ORPH2": (time.time(), self.order(pm, "ORPH2"))},
                                     state=lambda oid, c: None)
        b.cancel = mock.Mock(return_value=state(20.0, True, st="ORDER_STATE_FILLED"))
        with mock.patch.object(polymarket, "_orders", feed):
            maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["hedged"])
        self.assertAlmostEqual(self.kalshi_bought(), 20)
        self.assertEqual(maker.STATE.quotes, {})

    def test_orders_this_run_already_tracks_are_never_taken_for_the_lost_one(self):
        g, pm, k, q = self.posted()                            # a normal quote on this market, tracked
        b = self.paper["Polymarket"]
        maker.STATE.suspects.append({"cand": maker.price_pair(self.cfg, g, pm, k, 20, None), "at": time.time(),
                                     "intent": "ORDER_INTENT_BUY_LONG"})
        b.open_orders = mock.Mock(return_value=[self.order(pm, q.oid)])
        maker._sweep(self.db, self.cfg, self.paper, QUIET["log"], time.time())
        self.assertEqual(len(maker.STATE.suspects), 1)         # not matched to our own tracked quote
        self.assertEqual(list(maker.STATE.quotes), [q.oid])

    def test_a_4xx_refusal_is_an_answer_not_a_lost_order(self):
        self.failed(error="Polymarket POST /v1/orders → HTTP 400: insufficient buying power")
        self.assertEqual(maker.STATE.suspects, [])

    def test_no_new_quote_on_that_game_while_looking_then_it_expires(self):
        g, pm, k, b, logs = self.failed()
        b.open_orders = mock.Mock(return_value=[])
        maker.STATE.last_post = 0
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertEqual(b.post.call_count, 1)                 # not posted a second time on top of a maybe-order
        maker.STATE.suspects[0]["at"] -= maker.SUSPECT_S + 1
        maker.STATE.sweep_at = 0
        maker.poll(self.db, self.cfg, self.paper, force=True, log=logs.append)
        self.assertEqual(maker.STATE.suspects, [])
        self.assertTrue(any("it was not placed" in x for x in logs))
        maker.STATE.last_post = 0
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertEqual(b.post.call_count, 2)                 # the game is quotable again

    def test_start_reports_an_open_order_the_ledger_does_not_know_and_leaves_it(self):
        g, pm, k = pair()
        b = self.paper["Polymarket"]
        b.open_orders = mock.Mock(return_value=[self.order(pm, "SOMEONE")])
        b.cancel = mock.Mock()
        logs = []
        maker.recover(self.db, self.cfg, self.paper, logs.append)
        self.assertTrue(any("SOMEONE is OPEN on Polymarket and not in this ledger" in x for x in logs))
        b.cancel.assert_not_called()                           # the account is shared: reported, never cancelled


if __name__ == "__main__":
    unittest.main()
