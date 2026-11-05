"""Offline tests for the 11-05 review fixes (maker mode):

  * a fill under one Kalshi contract is `dust`, not a failed hedge (11-05 03:53Z two of them halted the bot)
  * a quote whose cancel is not confirmed after a fill stays tracked, and every fill is booked exactly once
  * shutdown / recover repeat a cancel that is not confirmed
  * an error after the fill still leaves a ledger row
  * a pre-game quote ends at the scheduled start even when the venue flag still says "not live"
  * resting quotes never add up to more than the day's room
  * an order update without avgPx is "price unknown", not $1.00 on a short contract
  * "Michigan State" is not "Michigan"

Run:  python3 -m unittest discover -s tests
"""
import datetime as dt
import json
import os
import sys
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_engine import c  # noqa: E402
from test_maker import QUIET, Base, pair, second_game  # noqa: E402
from test_winners import kalshi_event, pm as pm_event, side  # noqa: E402

from arbscan import brokers, maker, notify, pmws, rescue, trader, winners  # noqa: E402


def state(cum, done, avg=0.39, st="ORDER_STATE_PARTIALLY_FILLED"):
    return {"state": "ORDER_STATE_CANCELED" if done and st.endswith("PARTIALLY_FILLED") else st, "done": done,
            "cum": cum, "avg": avg if cum else 0.0, "fee": 0.0, "price": 0.39}


class Maker(Base):
    def posted(self, **kw):
        g, pm, k = pair(**kw)
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertIsNotNone(maker.STATE.quote)
        return g, pm, k, maker.STATE.quote

    def pm_says(self, order_state=None, cancel=None):
        b = self.paper["Polymarket"]
        if order_state is not None:
            b.order_state = mock.Mock(side_effect=order_state if isinstance(order_state, list) else None,
                                      return_value=None if isinstance(order_state, list) else order_state)
        if cancel is not None:
            b.cancel = mock.Mock(side_effect=cancel if isinstance(cancel, list) else None,
                                 return_value=None if isinstance(cancel, list) else cancel)
        return b

    def rows(self):
        return self.db.execute("SELECT * FROM trades ORDER BY id").fetchall()

    def kalshi_bought(self):
        return sum(f["qty"] for r in self.rows() for f in json.loads(r["fills"])
                   if f["venue"] == "Kalshi" and f["action"] == "buy")


class DustTests(Maker):
    def test_a_fill_under_one_contract_is_dust_and_never_halts(self):
        for i in range(2):                                     # 11-05 03:52:53Z and 03:53:04Z
            maker.STATE.last_post = 0
            g, pm, k, q = self.posted()
            self.pm_says(order_state=state(0.01, False), cancel=state(0.01, True))
            self.assertIsNone(maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET))     # not "halt"
            self.assertEqual(maker.STATE.quotes, {})
            self.db.execute("UPDATE trades SET detected_at = detected_at - 3600")                 # past the cooldown
            self.db.commit()
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["dust", "dust"])
        self.assertEqual(rows[0]["status"], "open")            # bid 0.38 < the 0.39 paid: held, it still settles
        self.assertAlmostEqual(rows[0]["qty_b"], 0.01)
        self.assertEqual(trader.recent_failures(self.db, "dry", self.cfg.max_failures), 0)
        self.assertEqual(self.kalshi_bought(), 0)

    def test_dust_sold_back_is_closed_and_not_unwound(self):
        g, pm, k, q = self.posted()
        pm.book.bids = [(0.45, 100)]                           # someone bids over what we paid: sell the 0.06 back
        self.pm_says(order_state=state(0.06, False), cancel=state(0.06, True))
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        r = self.rows()[0]
        self.assertEqual((r["outcome"], r["status"]), ("dust", "closed"))
        self.assertGreater(r["pnl"], 0)
        self.assertEqual(trader.recent_failures(self.db, "dry", 2), 0)

    def test_dust_neither_counts_nor_breaks_a_real_streak(self):
        vals = dict(mode="dry", detected_at=time.time(), game="x", opp_key="k", leg_a="{}", leg_b="{}", fills="[]",
                    qty_a=0, qty_b=0, cash_out=1.0, status="open")
        for outcome in ("exposed", "dust", "exposed"):
            v = dict(vals, outcome=outcome)
            self.db.execute(f"INSERT INTO trades ({','.join(v)}) VALUES ({','.join('?' * len(v))})", list(v.values()))
        self.db.commit()
        self.assertEqual(trader.recent_failures(self.db, "dry", 2), 2)

    def test_a_whole_contract_that_misses_its_hedge_is_still_a_failure(self):
        g, pm, k, q = self.posted()
        k.book.asks = [(0.70, 100)]
        self.pm_says(order_state=state(1.0, False), cancel=state(1.0, True))
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        self.assertEqual(self.rows()[0]["outcome"], "exposed")
        self.assertEqual(trader.recent_failures(self.db, "dry", 2), 1)

    def test_dust_is_not_a_telegram_line(self):
        sent = []
        notify.configure(post=lambda t: sent.append(t) or True)
        try:
            notify.trade({"mode": "live", "outcome": "dust", "id": 1, "game": "x", "fills": []})
            self.assertEqual(len(notify._q), 0)
            notify.trade({"mode": "live", "outcome": "exposed", "id": 2, "game": "x", "fills": [], "cash_out": 3.0})
            self.assertEqual(len(notify._q), 1)
        finally:
            notify.configure()

    def test_rescue_sells_held_dust_and_never_tries_a_kalshi_hedge_for_it(self):
        rescue._next_try.clear()
        g, pm, k, q = self.posted()
        self.pm_says(order_state=state(0.5, False), cancel=state(0.5, True))
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        self.assertEqual(self.rows()[0]["outcome"], "dust")
        k.book.asks = [(0.30, 100)]                            # a hedge would lock far more than a sale ...
        pm.book.bids = [(0.50, 100)]                           # ... but half a contract cannot be bought on Kalshi
        self.paper["Polymarket"] = brokers.PaperBroker("Polymarket", latency=0)
        self.assertEqual(rescue.check(self.db, self.cfg, self.paper, [g], QUIET["log"]), 1)
        r = self.rows()[0]
        self.assertEqual((r["outcome"], r["status"]), ("sold", "closed"))
        self.assertEqual(self.kalshi_bought(), 0)
        self.assertTrue(trader._cash["dirty"])                 # the next quote is sized from a fresh balance read


class DeadQuoteTests(Maker):
    def test_unconfirmed_cancel_keeps_the_quote_and_books_every_fill_once(self):
        g, pm, k, q = self.posted()
        oid = q.oid
        # 5 fill; the cancel is accepted but the order still reads open (11-05: ORDER_STATE_NEW 1.5 s later, 29 times)
        b = self.pm_says(order_state=state(5.0, False), cancel=state(5.0, False))
        logs = []
        maker.poll(self.db, self.cfg, self.paper, force=True, log=logs.append)
        self.assertIn(oid, maker.STATE.quotes)                 # NOT forgotten
        self.assertTrue(q.dead)
        self.assertAlmostEqual(q.booked, 5.0)
        self.assertTrue(any("still tracked" in x for x in logs))
        row = self.db.execute("SELECT closed_at, filled FROM quotes WHERE order_id=?", (oid,)).fetchone()
        self.assertIsNone(row["closed_at"])                    # a crash now: recover() still finds it ...
        self.assertAlmostEqual(row["filled"], 5.0)             # ... and knows 5 are booked
        self.assertEqual(len(self.rows()), 1)
        self.assertAlmostEqual(self.kalshi_bought(), 5)

        # the same 5 seen again (stream + REST, the next poll): nothing new, no second hedge
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        self.assertEqual(len(self.rows()), 1)
        self.assertAlmostEqual(self.kalshi_bought(), 5)
        self.assertEqual(b.cancel.call_args.kwargs.get("wait"), 0.0)        # the repeat does not block the loop

        # 7 more fill while the cancel is still not showing: hedged and booked as their own row
        b.cancel.return_value = state(12.0, False)
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        rows = self.rows()
        self.assertEqual([r["outcome"] for r in rows], ["hedged", "hedged"])
        self.assertAlmostEqual(rows[1]["qty_a"], 7)
        self.assertAlmostEqual(rows[1]["qty_b"], 7)
        self.assertAlmostEqual(self.kalshi_bought(), 12)
        self.assertIn(oid, maker.STATE.quotes)

        # the cancel finally shows: the quote is closed with all 12
        b.cancel.return_value = state(12.0, True)
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})
        row = self.db.execute("SELECT closed_at, filled FROM quotes WHERE order_id=?", (oid,)).fetchone()
        self.assertIsNotNone(row["closed_at"])
        self.assertAlmostEqual(row["filled"], 12.0)
        self.assertEqual(len(self.rows()), 2)

    def test_pull_with_a_partial_fill_and_no_confirmation_keeps_it_too(self):
        g, pm, k, q = self.posted()
        self.pm_says(cancel=state(3.0, False))
        maker.pull(self.db, self.cfg, self.paper, q, "test", QUIET["log"])
        self.assertTrue(q.dead)
        self.assertIn(q.oid, maker.STATE.quotes)
        self.assertAlmostEqual(self.kalshi_bought(), 3)

    def test_a_dead_quote_is_never_repriced_and_still_holds_its_slot(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(order_state=state(5.0, False), cancel=state(5.0, False))
        b.modify = mock.Mock(return_value=(True, ""))
        b.post = mock.Mock()
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        pm.book.bids = [(0.40, 50)]                            # outbid: a live quote would re-price
        q.priced_at = 0
        maker.STATE.last_post = 0
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        b.modify.assert_not_called()
        b.post.assert_not_called()                             # one quote per game: the dead one still counts

    def test_fill_more_before_the_cancel_lands_is_one_row(self):
        g, pm, k, q = self.posted()
        self.pm_says(order_state=state(5.0, False), cancel=state(20.0, True, st="ORDER_STATE_FILLED"))
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "hedged")
        self.assertAlmostEqual(rows[0]["qty_a"], 20)
        self.assertAlmostEqual(self.kalshi_bought(), 20)
        self.assertEqual(maker.STATE.quotes, {})


class StopTests(Maker):
    def test_shutdown_repeats_a_cancel_that_is_not_confirmed(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(cancel=[state(0.0, False, st="ORDER_STATE_NEW"), state(0.0, False, st="ORDER_STATE_NEW"),
                                 state(0.0, True, st="ORDER_STATE_CANCELED")])
        logs = []
        maker.shutdown(self.db, self.cfg, self.paper, logs.append)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertEqual(b.cancel.call_count, 3)
        self.assertFalse(any("may still be resting" in x for x in logs))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM quotes WHERE closed_at IS NULL").fetchone()[0], 0)

    def test_shutdown_gives_up_loudly_and_leaves_the_row_open(self):
        g, pm, k, q = self.posted()
        b = self.pm_says(cancel=state(0.0, False, st="ORDER_STATE_NEW"))
        logs = []
        maker.shutdown(self.db, self.cfg, self.paper, logs.append)
        self.assertEqual(b.cancel.call_count, maker.STOP_TRIES)
        self.assertTrue(any("may still be resting" in x for x in logs))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM quotes WHERE closed_at IS NULL").fetchone()[0], 1)

    def _left_open(self, pm, k, filled=0.0):
        self.db.execute("INSERT INTO quotes (order_id, mode, posted_at, game, opp_key, pm, k, price, qty, k_ask, "
                        "guaranteed, filled) VALUES ('old1','dry',?,?,?,?,?,0.39,20,0.55,1.0,?)",
                        (time.time() - 60, "KC @ DEN", f"{k.id}|{pm.id}", maker._snap(pm), maker._snap(k), filled))
        self.db.commit()

    def test_recover_does_not_trust_a_cancel_that_is_not_confirmed(self):
        g, pm, k = pair()
        self._left_open(pm, k)
        self.pm_says(cancel=state(0.0, False, st="ORDER_STATE_NEW"))
        logs = []
        self.assertEqual(maker.recover(self.db, self.cfg, self.paper, logs.append), 0)
        self.assertIsNone(self.db.execute("SELECT closed_at FROM quotes").fetchone()[0])     # tried again next start
        self.assertTrue(any("CANCEL IT ON POLYMARKET" in x for x in logs))

    def test_recover_asks_again_until_the_cancel_shows(self):
        g, pm, k = pair()
        self._left_open(pm, k)
        self.pm_says(cancel=[state(0.0, False, st="ORDER_STATE_NEW"), state(0.0, True, st="ORDER_STATE_CANCELED")])
        self.assertEqual(maker.recover(self.db, self.cfg, self.paper, **QUIET), 1)
        self.assertIsNotNone(self.db.execute("SELECT closed_at FROM quotes").fetchone()[0])

    def test_recover_books_only_what_was_not_booked_before_the_crash(self):
        g, pm, k = pair()
        self._left_open(pm, k, filled=5.0)                     # a dead quote at the crash: 5 already in a trade row
        self.pm_says(cancel=state(12.0, True))
        maker.recover(self.db, self.cfg, self.paper, **QUIET)
        row = self.rows()[0]
        self.assertEqual(row["outcome"], "error")
        self.assertAlmostEqual(row["qty_b"], 7)
        self.assertAlmostEqual(row["cash_out"], 7 * 0.39)
        self.assertAlmostEqual(self.db.execute("SELECT filled FROM quotes").fetchone()[0], 12.0)


class ErrorAfterFillTests(Maker):
    def test_an_error_after_the_fill_still_leaves_a_ledger_row_and_one_hedge(self):
        g, pm, k, q = self.posted()
        self.pm_says(order_state=state(20.0, True, st="ORDER_STATE_FILLED"))
        with mock.patch.object(trader, "_after_leg1", side_effect=RuntimeError("database is locked")):
            maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "error")
        self.assertAlmostEqual(rows[0]["qty_a"], 20)           # the Kalshi hedge that did fill is in the row ...
        self.assertAlmostEqual(rows[0]["qty_b"], 20)           # ... next to the Polymarket leg
        self.assertIn("database is locked", rows[0]["note"])
        self.assertEqual(maker.STATE.quotes, {})
        maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        self.assertAlmostEqual(self.kalshi_bought(), 20)       # never hedged a second time
        self.assertIsNotNone(self.db.execute("SELECT closed_at FROM quotes").fetchone()[0])


class PregameTests(Maker):
    def test_a_passed_start_ends_pre_game_even_with_the_flag_still_false(self):
        now = dt.datetime.now(dt.timezone.utc)
        g, pm, k = pair()
        g.sport, g.live = "soccer", False                      # winners.py: the venue flag, cached up to 60 s
        g.start = now + dt.timedelta(minutes=5)
        self.assertTrue(maker._pregame(g, now))
        g.start = now - dt.timedelta(seconds=5)
        self.assertFalse(maker._pregame(g, now))
        g.sport = "tennis"                                     # tennis start times are schedule slots: the flag rules
        self.assertTrue(maker._pregame(g, now))
        g.live = True
        self.assertFalse(maker._pregame(g, now))

    def test_a_pre_game_quote_is_pulled_at_the_scheduled_start(self):
        g, pm, k, q = self.posted()
        g.sport, g.live = "soccer", False
        g.start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        logs = []
        maker.step(self.db, self.cfg, self.paper, [g], log=logs.append, full=True)
        self.assertEqual(maker.STATE.quotes, {})               # soccer is not a live sport in this config: not re-posted
        self.assertTrue(any("the game started" in x for x in logs))


class RoomTests(Maker):
    def test_resting_quotes_fit_the_room_with_no_slack_on_top(self):
        cfg = trader.Config(maker=True, max_per_trade=20, max_daily_spend=30, min_profit=0.0)
        games = [pair()[0], second_game("LV")[0], second_game("SEA")[0]]
        for _ in range(4):
            maker.STATE.last_post = 0
            maker.step(self.db, cfg, self.paper, games, full=True, **QUIET)
        qs = list(maker.STATE.quotes.values())
        self.assertEqual(len(qs), 3)
        committed = sum(q.outlay() for q in qs)
        self.assertLessEqual(committed, 30 / maker.ROOM_SLACK + 1e-6)      # sized into the room less the slack
        # the room shrinks to 5% under what rests (inside the old 15% slack): the newest quote must go
        cfg.max_daily_spend = committed * 0.95
        newest = max(qs, key=lambda q: q.posted_at).oid
        maker.STATE.last_post = time.time()
        maker.step(self.db, cfg, self.paper, games, full=True, **QUIET)
        left = maker.STATE.quotes
        self.assertNotIn(newest, left)
        self.assertLessEqual(sum(q.outlay() for q in left.values()), cfg.max_daily_spend + 0.01)


class AvgPriceTests(unittest.TestCase):
    def setUp(self):
        import base64
        self.b = brokers.PolymarketBroker(key_id="k", secret_key_b64=base64.b64encode(b"\x01" * 32).decode())
        self.short = c("Polymarket", "total", "lt", 44.5, [], tid="pm-under", pm_side="short")
        self.order = {"id": "o1", "state": "ORDER_STATE_PARTIALLY_FILLED", "cumQuantity": "5"}      # no avgPx

    def test_rest_state_without_avgpx_is_unknown_not_a_dollar(self):
        self.assertEqual(self.b._state(self.short, self.order)["avg"], 0.0)
        st = self.b._state(self.short, dict(self.order, avgPx={"value": "0.61"}))
        self.assertAlmostEqual(st["avg"], 0.39)

    def test_stream_state_without_avgpx_is_unknown_not_a_dollar(self):
        feed = types.SimpleNamespace(orders={"o1": (time.time(), self.order)})
        self.assertEqual(pmws.OrderFeed.state(feed, "o1", self.short)["avg"], 0.0)

    def test_the_fill_then_uses_the_quote_price(self):
        q = maker.Quote("o1", "k|p", self.short.id, "k", "g", "t", 0.39, 20, 0.55, 1.0, time.time(), pm=self.short)
        f = maker._fill_since(q, {"cum": 5.0, "avg": 0.0, "fee": 0.0})
        self.assertAlmostEqual(f.avg_price, 0.39)
        self.assertAlmostEqual(f.qty, 5.0)


class PairingTests(unittest.TestCase):
    def test_state_is_part_of_the_name(self):
        self.assertFalse(winners.same(winners.norm("Michigan State"), winners.norm("Michigan")))
        self.assertFalse(winners.same(winners.norm("Kansas State"), winners.norm("Kansas")))
        self.assertTrue(winners.same(winners.norm("Ohio St."), winners.norm("Ohio State")))
        self.assertTrue(winners.same(winners.norm("Tennessee State"), winners.norm("Tennessee St")))

    def test_kansas_state_is_not_paired_with_kansas(self):
        # the date-1 rule lets a Polymarket game on the 7th look at Kalshi's event of the 6th: Duke v Kansas there,
        # Duke v Kansas State here (a multi-team event, back-to-back days) must not pair
        ev, wins = pm_event("cbb-ksu-duke-2026-11-07", "2026-11-07T19:00:00Z",
                            side("ksu", "Kansas State", "away", True), side("duke", "Duke", "home", False))
        kev = kalshi_event("KXNCAAMBGAME-26NOV06KUDUKE", KU="Kansas", DUKE="Duke")
        self.assertIsNone(winners.match(ev, wins, kev))
        kev = kalshi_event("KXNCAAMBGAME-26NOV07KSUDUKE", KSU="Kansas St.", DUKE="Duke")
        self.assertIsNotNone(winners.match(ev, wins, kev))


if __name__ == "__main__":
    unittest.main()
