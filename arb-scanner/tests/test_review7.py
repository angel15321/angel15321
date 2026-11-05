"""Offline tests for the venue-hours guard (hours.py) and the empty-Polymarket-list line.

11-05 08:04Z: Kalshi was in its Thursday maintenance, two quotes still resting on Polymarket filled, both hedges
came back `409 trading_is_paused`, the bot halted on "2 failed hedges" and was down four hours.
11-05 11:00-13:00Z: Polymarket's maintenance; its game lists came back HTTP 200 and empty, with no line saying so.

  * the closed windows are read from Kalshi's schedule (ET, with and without daylight time); Polymarket's is built in
  * a quote comes off before a window, and when Kalshi's exchange status says trading is paused; none is posted
  * a hedge refused as "paused" is not retried, is not a failed-hedge strike, and closes the venue here
  * the held-leg watcher sends no order to a closed venue
  * a scan in which Polymarket lists no games says so

Run:  python3 -m unittest discover -s tests
"""
import datetime as dt
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_maker import QUIET, pair  # noqa: E402
from test_review3 import Maker, state  # noqa: E402

from arbscan import fees, hours, kalshi, maker, polymarket, rescue, scanner, trader  # noqa: E402
from arbscan.brokers import Fill  # noqa: E402
from arbscan.model import Book, Contract, Game  # noqa: E402

# GET /trade-api/v2/exchange/schedule as it read on 11-05
SCHEDULE = {"schedule": {"maintenance_windows": [], "standard_hours": [{
    "start_time": "2024-12-01T00:00:00Z", "end_time": "2200-12-01T00:00:00Z",
    "monday": [{"open_time": "00:00", "close_time": "00:00"}], "tuesday": [{"open_time": "00:00", "close_time": "00:00"}],
    "wednesday": [{"open_time": "00:00", "close_time": "00:00"}],
    "thursday": [{"open_time": "00:00", "close_time": "03:00"}, {"open_time": "05:00", "close_time": "00:00"}],
    "friday": [{"open_time": "00:00", "close_time": "00:00"}], "saturday": [{"open_time": "00:00", "close_time": "00:00"}],
    "sunday": [{"open_time": "00:00", "close_time": "00:00"}]}]}}
ACTIVE = {"exchange_active": True, "trading_active": True, "exchange_index_statuses": [
    {"exchange_index": 0, "exchange_active": True, "trading_active": True},
    {"exchange_index": 3, "exchange_active": True, "trading_active": True}]}
PAUSED_409 = ('Kalshi POST /portfolio/events/orders → HTTP 409: {"error":{"code":"trading_is_paused",'
              '"message":"trading is paused"}}')


def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s + "+00:00").timestamp()


def paused_status(index=None):
    """Kalshi's status with trading paused everywhere (index None) or on one exchange index only."""
    return {"exchange_active": True, "trading_active": index is not None, "exchange_index_statuses": [
        {"exchange_index": i, "exchange_active": True, "trading_active": index is not None and i != index}
        for i in (0, 3)]}


class Clean(unittest.TestCase):
    def setUp(self):
        hours.reset()
        self.addCleanup(hours.reset)


class ScheduleTests(Clean):
    def test_eastern_time_with_and_without_daylight_time(self):
        self.assertEqual(hours.et_offset(dt.datetime(2026, 11, 5, 12, tzinfo=dt.timezone.utc)), 5)
        self.assertEqual(hours.et_offset(dt.datetime(2026, 7, 9, 12, tzinfo=dt.timezone.utc)), 4)
        self.assertEqual(hours.et_offset(dt.datetime(2026, 11, 1, 5, 59, tzinfo=dt.timezone.utc)), 4)   # 1:59 EDT
        self.assertEqual(hours.et_offset(dt.datetime(2026, 11, 1, 6, 0, tzinfo=dt.timezone.utc)), 5)
        self.assertEqual(hours.et_offset(dt.datetime(2026, 3, 8, 7, 0, tzinfo=dt.timezone.utc)), 4)

    def test_kalshi_thursday_window_from_its_schedule(self):
        hours.S.schedule = SCHEDULE
        w = hours.windows("Kalshi", ts("2026-11-05T07:00:00"))
        self.assertIn((ts("2026-11-05T08:00:00"), ts("2026-11-05T10:00:00"), "scheduled closure"), w)   # 03-05 EST
        self.assertEqual([x for x in w if x[0] < ts("2026-11-05T00:00:00")], [])     # no window on the other days
        hours.S.windows.clear()
        w = hours.windows("Kalshi", ts("2026-07-09T05:00:00"))
        self.assertIn((ts("2026-07-09T07:00:00"), ts("2026-07-09T09:00:00"), "scheduled closure"), w)   # 03-05 EDT

    def test_an_announced_maintenance_window_counts(self):
        sc = {"schedule": {"standard_hours": [], "maintenance_windows": [
            {"start_datetime": "2026-11-07T02:00:00Z", "end_datetime": "2026-11-07T02:30:00Z"}]}}
        hours.S.schedule = sc
        hours.enable()
        self.assertIn("maintenance until 02:30Z", hours.closed("Kalshi", None, ts("2026-11-07T02:10:00")))

    def test_without_a_schedule_the_weekly_windows_are_built_in(self):
        hours.enable()
        self.assertIn("until 10:00Z", hours.closed("Kalshi", None, ts("2026-11-05T08:04:16")))        # the 11-05 fills
        self.assertIn("until 13:00Z", hours.closed("Polymarket", None, ts("2026-11-05T12:03:04")))    # the restart
        self.assertEqual(hours.closed("Polymarket", None, ts("2026-11-04T12:03:04")), "")             # a Wednesday
        self.assertEqual(hours.closed("Kalshi", None, ts("2026-11-05T12:03:04")), "")

    def test_a_schedule_we_cannot_read_falls_back_to_the_weekly_window(self):
        hours.S.schedule = {"schedule": {"standard_hours": [{"thursday": [{"open_time": "soon"}]}]}}
        hours.enable()
        self.assertIn("until 10:00Z", hours.closed("Kalshi", None, ts("2026-11-05T09:00:00")))

    def test_quotes_come_off_before_a_window_and_go_back_after_it(self):
        hours.S.schedule = SCHEDULE
        hours.enable()
        self.assertEqual(hours.closed("Kalshi", None, ts("2026-11-05T07:57:30")), "")
        self.assertIn("starts 08:00Z", hours.closed("Kalshi", None, ts("2026-11-05T07:58:30")))        # 90 s before
        self.assertIn("until 10:00Z", hours.closed("Kalshi", None, ts("2026-11-05T09:59:59")))
        self.assertIn("until 10:00Z", hours.closed("Kalshi", None, ts("2026-11-05T10:00:30")))         # the tail
        self.assertEqual(hours.closed("Kalshi", None, ts("2026-11-05T10:01:01")), "")
        # closing a held position may run to the last second: no lead, no tail
        self.assertEqual(hours.closed("Kalshi", None, ts("2026-11-05T07:59:30"), pad=False), "")
        self.assertEqual(hours.closed("Kalshi", None, ts("2026-11-05T10:00:30"), pad=False), "")
        self.assertNotEqual(hours.closed("Kalshi", None, ts("2026-11-05T08:00:01"), pad=False), "")

    def test_nothing_is_consulted_until_it_is_switched_on(self):
        hours.S.schedule, hours.S.status, hours.S.status_at = SCHEDULE, paused_status(), ts("2026-11-05T09:00:00")
        self.assertEqual(hours.closed("Kalshi", None, ts("2026-11-05T09:00:00")), "")     # tests, `scan`, `status`
        hours.enable()
        self.assertNotEqual(hours.closed("Kalshi", None, ts("2026-11-05T09:00:00")), "")


class StatusTests(Clean):
    def setUp(self):
        super().setUp()
        hours.enable()
        self.now = ts("2026-11-03T20:00:00")           # a Tuesday: no window anywhere near

    def test_exchange_status_paused_closes_kalshi_only(self):
        hours.S.status, hours.S.status_at = paused_status(), self.now
        self.assertIn("paused", hours.closed("Kalshi", 3, self.now + 1))
        self.assertEqual(hours.closed("Polymarket", None, self.now + 1), "")
        self.assertIn("Kalshi", hours.quoting(3, self.now + 1))
        self.assertIn("Kalshi", hours.text(self.now + 1))

    def test_one_exchange_index_paused_closes_only_its_markets(self):
        hours.S.status, hours.S.status_at = paused_status(index=3), self.now
        self.assertIn("exchange 3", hours.closed("Kalshi", 3, self.now + 1))
        self.assertEqual(hours.closed("Kalshi", 0, self.now + 1), "")
        self.assertEqual(hours.closed("Kalshi", None, self.now + 1), "")

    def test_a_stale_status_says_nothing(self):
        hours.S.status, hours.S.status_at = paused_status(), self.now
        self.assertEqual(hours.closed("Kalshi", 3, self.now + hours.STATUS_STALE_S + 1), "")

    def test_refresh_keeps_the_last_status_when_a_read_fails(self):
        replies = iter([ACTIVE, SCHEDULE])
        with mock.patch.object(hours, "_fetch", side_effect=lambda url, timeout=5.0: next(replies)):
            hours.refresh(self.now)
        self.assertIs(hours.S.status, ACTIVE)
        self.assertIs(hours.S.schedule, SCHEDULE)
        with mock.patch.object(hours, "_fetch", side_effect=TimeoutError("slow")) as f:
            hours.refresh(self.now + 5)
        self.assertEqual(f.call_count, 1)                              # the schedule is not due for an hour
        self.assertIs(hours.S.status, ACTIVE)
        self.assertEqual(hours.S.status_at, self.now)
        self.assertIn("TimeoutError", hours.S.status_error)

    def test_a_reply_that_is_not_a_status_is_not_adopted(self):
        with mock.patch.object(hours, "_fetch", return_value={"error": "nope"}):
            hours.refresh(self.now)
        self.assertIsNone(hours.S.status)
        self.assertIsNone(hours.S.schedule)
        self.assertIn("unreadable", hours.S.status_error)

    def test_a_dead_status_read_is_reported(self):
        self.assertEqual(hours.problem(self.now), "")                   # no reader thread (a test, `scan`): silent
        hours.S.thread = mock.Mock()
        hours.S.status, hours.S.status_at, hours.S.status_error = ACTIVE, self.now, "TimeoutError: slow"
        self.assertEqual(hours.problem(self.now + 10), "")
        self.assertIn("not readable (TimeoutError: slow)", hours.problem(self.now + 31))
        hours.S.thread = None

    def test_only_a_pause_reply_is_a_pause(self):
        self.assertTrue(hours.is_pause(PAUSED_409))
        self.assertFalse(hours.is_pause("best ask 0.58 > limit 0.540"))
        self.assertFalse(hours.is_pause('HTTP 400: {"error":{"code":"insufficient_balance"}}'))
        self.assertFalse(hours.is_pause(None))

    def test_a_refused_order_closes_the_venue_longer_each_time(self):
        until = hours.note_refused("Kalshi", self.now)
        self.assertAlmostEqual(until - self.now, 60.0)
        self.assertIn("refused", hours.closed("Kalshi", None, self.now + 59))
        self.assertEqual(hours.closed("Kalshi", None, self.now + 61), "")
        hours.note_refused("Kalshi", self.now + 1)                      # a retry of the same hedge: one refusal
        self.assertEqual(len(hours.S.refusals["Kalshi"]), 1)
        until = hours.note_refused("Kalshi", self.now + 70)             # refused again a minute later: 2 min
        self.assertAlmostEqual(until - (self.now + 70), 120.0)
        until = hours.note_refused("Kalshi", self.now + 200)
        self.assertAlmostEqual(until - (self.now + 200), 240.0)
        for i in range(8):
            until = hours.note_refused("Kalshi", self.now + 500 + 10 * i)
        self.assertLessEqual(until - (self.now + 570), hours.HOLD_MAX_S)

    def test_a_refusal_counts_even_when_the_guard_is_not_switched_on(self):
        hours.reset()
        hours.note_refused("Kalshi")
        self.assertNotEqual(hours.closed("Kalshi"), "")


class MakerGuardTests(Maker):
    def setUp(self):
        super().setUp()
        hours.reset()
        self.addCleanup(hours.reset)
        hours.enable()
        hours.S.windows = {v: (time.time(), []) for v in ("Kalshi", "Polymarket")}     # no window near the test's clock
        hours.S.status, hours.S.status_at = ACTIVE, time.time()

    def pulled(self):
        return self.db.execute("SELECT note FROM quotes ORDER BY rowid DESC").fetchone()[0]

    def test_a_resting_quote_is_pulled_when_kalshi_says_trading_is_paused(self):
        g, pm, k, q = self.posted()
        k.ref["shard"] = 3
        with mock.patch.object(trader, "live_book", return_value=None):
            self.assertIsNone(maker.guard(self.db, self.cfg, self.paper, **QUIET))
        self.assertEqual(len(maker.STATE.quotes), 1)                    # both venues trade: it rests
        hours.S.status = paused_status()
        with mock.patch.object(trader, "live_book", return_value=None):
            maker.guard(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertIn("paused", self.pulled())

    def test_one_paused_exchange_index_pulls_only_its_quotes(self):
        g, pm, k, q = self.posted()
        k.ref["shard"] = 0
        hours.S.status = paused_status(index=3)                         # the tennis / basketball shard, not ours
        with mock.patch.object(trader, "live_book", return_value=None):
            maker.guard(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual(len(maker.STATE.quotes), 1)
        k.ref["shard"] = 3
        with mock.patch.object(trader, "live_book", return_value=None):
            maker.guard(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})

    def test_nothing_is_posted_while_a_venue_is_closed_and_quoting_resumes_after(self):
        g, pm, k = pair()
        hours.S.status = paused_status()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertIn("Kalshi", maker.STATE.closed)
        self.assertIn("NOT QUOTING: Kalshi trading is paused", maker.stats_text())
        self.assertIn("NOT QUOTING", trader._status([g], "maker", []))
        hours.S.status = ACTIVE
        maker.STATE.last_post = 0
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertEqual(len(maker.STATE.quotes), 1)
        self.assertEqual(maker.STATE.closed, "")
        self.assertNotIn("NOT QUOTING", maker.stats_text())

    def test_a_quote_on_a_paused_exchange_index_is_not_posted(self):
        g, pm, k = pair()
        k.ref["shard"] = 3
        hours.S.status = paused_status(index=3)
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})
        k.ref["shard"] = 0
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertEqual(len(maker.STATE.quotes), 1)

    def test_a_quote_comes_off_two_minutes_before_a_scheduled_window(self):
        g, pm, k, q = self.posted()
        now = time.time()
        hours.S.windows["Kalshi"] = (now, [(now + 300, now + 7500, "scheduled closure")])
        with mock.patch.object(trader, "live_book", return_value=None):
            maker.guard(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual(len(maker.STATE.quotes), 1)                    # five minutes out: still rests
        hours.S.windows["Kalshi"] = (now, [(now + 90, now + 7290, "scheduled closure")])
        with mock.patch.object(trader, "live_book", return_value=None):
            maker.guard(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertIn("scheduled closure starts", self.pulled())

    def test_polymarket_maintenance_pulls_and_posts_nothing(self):
        g, pm, k, q = self.posted()
        now = time.time()
        hours.S.windows["Polymarket"] = (now, [(now - 60, now + 3600, "weekly maintenance")])
        with mock.patch.object(trader, "live_book", return_value=None):
            maker.guard(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})
        maker.STATE.last_post = 0
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertIn("Polymarket weekly maintenance", maker.STATE.closed)


class PausedHedgeTests(Maker):
    """11-05 08:04:16Z #68 and 08:04:17Z #69."""

    def setUp(self):
        super().setUp()
        hours.reset()
        self.addCleanup(hours.reset)

    def fill_into_a_pause(self, qty=6.0):
        maker.STATE.last_post = 0
        g, pm, k, q = self.posted()
        buy = mock.Mock(return_value=Fill("Kalshi", "buy", k.label, status="error", error=PAUSED_409))
        self.paper["Kalshi"].buy = buy
        self.pm_says(order_state=state(qty, False), cancel=state(qty, True))
        out = maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        return g, pm, k, buy, out

    def test_a_hedge_refused_as_paused_is_not_retried_and_is_not_a_strike(self):
        g, pm, k, buy, out = self.fill_into_a_pause()
        self.assertIsNone(out)                                          # not "halt"
        self.assertEqual(buy.call_count, 1)                             # the venue's answer: no second try
        r = self.rows()[0]
        self.assertIn(r["outcome"], ("exposed", "unwound"))
        self.assertIn(hours.PAUSE_NOTE, r["note"])
        self.assertIn("trading is paused", r["note"])
        self.assertEqual(trader.recent_failures(self.db, "dry", self.cfg.max_failures), 0)

    def test_two_of_them_do_not_halt_the_bot(self):
        for _ in range(2):
            g, pm, k, buy, out = self.fill_into_a_pause()
            self.assertIsNone(out)
            hours.reset()                                               # (the guard itself is tested below)
            self.db.execute("UPDATE trades SET detected_at = detected_at - 3600")     # past the cooldown
            self.db.commit()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(trader.recent_failures(self.db, "dry", self.cfg.max_failures), 0)

    def test_the_refusal_closes_kalshi_here_so_nothing_is_posted_next(self):
        g, pm, k, buy, out = self.fill_into_a_pause()
        self.assertIn("refused an order as paused", hours.closed("Kalshi"))
        self.db.execute("UPDATE trades SET detected_at = detected_at - 3600")
        self.db.commit()
        maker.STATE.last_post = 0
        post = mock.Mock()
        self.paper["Polymarket"].post = post
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        post.assert_not_called()
        self.assertEqual(maker.STATE.quotes, {})

    def test_a_pause_row_neither_counts_nor_breaks_a_real_streak(self):
        vals = dict(mode="dry", detected_at=time.time(), game="x", opp_key="k", leg_a="{}", leg_b="{}", fills="[]",
                    qty_a=0, qty_b=0, cash_out=1.0, status="open", outcome="exposed")
        for note in ("hedge quote gone", f"Kalshi answered 'trading is paused' to the hedge ({hours.PAUSE_NOTE})",
                     None):
            v = dict(vals, note=note)
            self.db.execute(f"INSERT INTO trades ({','.join(v)}) VALUES ({','.join('?' * len(v))})", list(v.values()))
        self.db.commit()
        self.assertEqual(trader.recent_failures(self.db, "dry", 2), 2)

    def test_a_hedge_that_misses_for_its_price_is_still_a_strike(self):
        maker.STATE.last_post = 0
        g, pm, k, q = self.posted()
        k.book.asks = [(0.70, 100)]
        self.pm_says(order_state=state(6.0, False), cancel=state(6.0, True))
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        r = self.rows()[0]
        self.assertNotIn(hours.PAUSE_NOTE, r["note"] or "")
        self.assertEqual(trader.recent_failures(self.db, "dry", 2), 1)
        self.assertEqual(hours.closed("Kalshi"), "")


class RescueHoursTests(Maker):
    def setUp(self):
        super().setUp()
        hours.reset()
        self.addCleanup(hours.reset)
        rescue._next_try.clear()

    def held(self):
        g, pm, k, q = self.posted()
        k.book.asks = [(0.70, 100)]                                     # the hedge is gone: leg 1 is held
        self.pm_says(order_state=state(6.0, False), cancel=state(6.0, True))
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        self.assertEqual(self.rows()[0]["outcome"], "exposed")
        k.book.asks = [(0.45, 100)]                                     # ... and now a late hedge would lock a profit
        return g, pm, k

    def test_no_late_hedge_is_sent_to_a_closed_venue(self):
        g, pm, k = self.held()
        hours.enable()
        hours.S.windows = {v: (time.time(), []) for v in ("Kalshi", "Polymarket")}
        hours.S.status, hours.S.status_at = paused_status(), time.time()
        buy = mock.Mock()
        self.paper["Kalshi"].buy = buy
        self.assertEqual(rescue.check(self.db, self.cfg, self.paper, [g], QUIET["log"]), 0)
        buy.assert_not_called()
        self.assertEqual(self.rows()[0]["outcome"], "exposed")

    def test_the_late_hedge_goes_through_once_kalshi_trades_again(self):
        g, pm, k = self.held()
        hours.enable()
        hours.S.windows = {v: (time.time(), []) for v in ("Kalshi", "Polymarket")}
        hours.S.status, hours.S.status_at = ACTIVE, time.time()
        self.assertEqual(rescue.check(self.db, self.cfg, self.paper, [g], QUIET["log"]), 1)
        self.assertEqual(self.rows()[0]["outcome"], "hedged")

    def test_a_late_hedge_refused_as_paused_stops_the_tries(self):
        g, pm, k = self.held()                                          # the guard is off: nothing known yet
        buy = mock.Mock(return_value=Fill("Kalshi", "buy", k.label, status="error", error=PAUSED_409))
        self.paper["Kalshi"].buy = buy
        self.assertEqual(rescue.check(self.db, self.cfg, self.paper, [g], QUIET["log"]), 0)
        self.assertEqual(buy.call_count, 1)
        rescue._next_try.clear()                                        # 11-05: nine tries in ten seconds
        self.assertEqual(rescue.check(self.db, self.cfg, self.paper, [g], QUIET["log"]), 0)
        self.assertEqual(buy.call_count, 1)

    def test_a_sell_back_still_goes_to_polymarket_while_kalshi_is_closed(self):
        g, pm, k = self.held()
        k.book.asks = [(0.70, 100)]
        pm.book.bids = [(0.50, 100)]
        hours.note_refused("Kalshi")
        self.assertEqual(rescue.check(self.db, self.cfg, self.paper, [g], QUIET["log"]), 1)
        self.assertEqual(self.rows()[0]["outcome"], "sold")


class EmptyListTests(unittest.TestCase):
    def games(self):
        start = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=12)
        kg = Game("OKC", "CLE", start, sport="nba")
        kg.contracts = [Contract(venue="Kalshi", game=kg.key, axis="total", side="gt", line=218.5, kind="total",
                                 label="Over 218.5", how="YES", fee=fees.kalshi_model("quadratic", 1),
                                 ref={"ticker": "KXNBATOTAL-X-219", "side": "yes"}, book=Book(asks=[(0.71, 1000)]))]
        pg = Game("OKC", "CLE", start, sport="nba")
        pg.contracts = [Contract(venue="Polymarket", game=pg.key, axis="total", side="lt", line=218.5, kind="total",
                                 label="Under 218.5", how="NO", fee=fees.FeeModel("Polymarket", 0.0),
                                 ref={"slug": "nba-okc-cle", "side": "short"}, book=Book(asks=[(0.26, 1000)]))]
        return kg, pg

    def scan(self, pm_games, k_games, sports=("nba", "nhl")):
        with mock.patch.object(polymarket, "fetch", side_effect=lambda h, f, sp: dict(pm_games.get(sp, {}))), \
                mock.patch.object(kalshi, "fetch", side_effect=lambda h, d, sp: dict(k_games.get(sp, {}))):
            return scanner.snapshot(sports=sports)

    def test_a_scan_with_no_polymarket_market_says_so(self):
        kg, pg = self.games()
        snap = self.scan({}, {"nba": {kg.key: kg}})                     # 11-05 12:03Z: HTTP 200, "events": []
        self.assertEqual(len(snap.games), 1)
        self.assertEqual(len(snap.errors), 1)
        self.assertIn("Polymarket lists NO games (1 here are Kalshi-only)", snap.errors[0])
        self.assertIn("Polymarket lists NO games", trader._status(snap.games, "maker", snap.errors))

    def test_one_league_missing_is_named(self):
        kg, pg = self.games()
        hk = Game("BOS", "PIT", kg.start, sport="nhl")
        hk.contracts = [kg.contracts[0]]
        snap = self.scan({"nba": {pg.key: pg}}, {"nba": {kg.key: kg}, "nhl": {hk.key: hk}})
        self.assertEqual(snap.errors, ["Polymarket lists no nhl games (Kalshi has them)"])

    def test_a_normal_scan_says_nothing(self):
        kg, pg = self.games()
        self.assertEqual(self.scan({"nba": {pg.key: pg}}, {"nba": {kg.key: kg}}).errors, [])
        self.assertEqual(self.scan({}, {}).errors, [])                   # no games anywhere: nothing to say


if __name__ == "__main__":
    unittest.main()
