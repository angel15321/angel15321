"""Offline tests for maker mode: quote pricing, pull rules, fill -> Kalshi hedge, crash recovery, pause on rejects.

Run:  python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_engine import K, c, game  # noqa: E402

from arbscan import brokers, maker, trader  # noqa: E402
from arbscan.brokers import Fill  # noqa: E402
from arbscan.model import Book  # noqa: E402

QUIET = dict(log=lambda *_: None)


def pair(pm_bids=((0.38, 100),), pm_asks=((0.47, 100),), k_asks=((0.55, 100),)):
    """Polymarket Under 44.5 (we bid for it) + Kalshi Over 44.5 (the hedge)."""
    pm = c("Polymarket", "total", "lt", 44.5, list(pm_asks), tid="pm-under")
    pm.book.bids = list(pm_bids)
    k = c("Kalshi", "total", "gt", 44.5, list(k_asks), fee=K, tid="k-over")
    now = time.time()
    pm.book.fetched_at = k.book.fetched_at = now
    pm.label, k.label = "Under 44.5", "Over 44.5"
    return game(pm, k), pm, k


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = trader.connect(self.tmp.name)
        self.cfg = trader.Config(maker=True, max_per_trade=20, min_profit=0.0)
        self.paper = {"Kalshi": brokers.PaperBroker("Kalshi", latency=0),
                      "Polymarket": brokers.PaperBroker("Polymarket", latency=0)}
        trader._cash.update(at=0.0, values=None)
        maker.reset()
        self.patches = [mock.patch.object(maker, "_feeds_ok", return_value=True),
                        mock.patch("arbscan.scanner.refresh")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.db.close()
        os.unlink(self.tmp.name)


class PricingTests(Base):
    def test_one_tick_over_the_bid_under_the_ceiling(self):
        g, pm, k = pair()
        cand = maker.price_pair(self.cfg, g, pm, k, 20, None)
        # ceiling = 1 - (0.55 + Kalshi fee 0.0173) - 0.01 = 0.4227 -> 0.42; bid 0.38 -> quote 0.39
        self.assertAlmostEqual(cand.price, 0.39)
        self.assertEqual(cand.qty, 20)                       # $20 / (0.39 + 0.5673)
        self.assertGreaterEqual(cand.edge, self.cfg.maker_edge)

    def test_capped_by_the_ceiling_and_below_the_ask(self):
        g, pm, k = pair(pm_bids=[(0.42, 10)], pm_asks=[(0.44, 10)])
        self.assertAlmostEqual(maker.price_pair(self.cfg, g, pm, k, 20, None).price, 0.42)   # joins at the ceiling
        g, pm, k = pair(pm_bids=[(0.38, 10)], pm_asks=[(0.39, 10)])
        # 0.38 + tick would hit the ask: join the best bid instead
        self.assertAlmostEqual(maker.price_pair(self.cfg, g, pm, k, 20, None).price, 0.38)

    def test_no_quote_behind_the_best_bid(self):
        g, pm, k = pair(pm_bids=[(0.45, 10)])
        self.assertIsNone(maker.price_pair(self.cfg, g, pm, k, 20, None))

    def test_own_quote_is_not_outbid(self):
        g, pm, k = pair(pm_bids=[(0.39, 20), (0.38, 100)])
        own = maker.Quote("o", "key", pm.id, k.id, g.key, g.title, 0.39, 20, 0.55, 1.0, time.time())
        self.assertAlmostEqual(maker.price_pair(self.cfg, g, pm, k, 20, own).price, 0.39)   # stays, not 0.40

    def test_junk_prices_skipped(self):
        g, pm, k = pair(k_asks=[(0.99, 100)], pm_bids=[], pm_asks=[(0.02, 10)])
        self.assertIsNone(maker.price_pair(self.cfg, g, pm, k, 20, None))


class StepTests(Base):
    def test_posts_then_pulls_when_kalshi_moves(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        q = maker.STATE.quote
        self.assertIsNotNone(q)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM quotes WHERE closed_at IS NULL").fetchone()[0], 1)
        k.book.asks = [(0.62, 100)]                            # hedge got 7c dearer: the quote is no longer safe
        logs = []
        maker.step(self.db, self.cfg, self.paper, [g], log=logs.append, full=True)
        self.assertIsNone(maker.STATE.quote)
        self.assertTrue(any("pulled" in x and "Kalshi ask moved" in x for x in logs))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM quotes WHERE closed_at IS NULL").fetchone()[0], 0)

    def test_pulled_when_the_game_starts_and_on_shutdown(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        g.live = True
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertIsNone(maker.STATE.quote)
        g.live = False
        maker.STATE.last_post = 0
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertIsNotNone(maker.STATE.quote)
        maker.shutdown(self.db, self.cfg, self.paper, **QUIET)
        self.assertIsNone(maker.STATE.quote)

    def test_fill_is_hedged_on_kalshi(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        sold_into = Book(asks=[(0.39, 50)], bids=[(0.38, 100)], fetched_at=time.time())
        with mock.patch.object(trader, "live_book", return_value=sold_into):
            maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        self.assertIsNone(maker.STATE.quote)
        row = self.db.execute("SELECT * FROM trades").fetchone()
        self.assertEqual(row["outcome"], "hedged")
        self.assertAlmostEqual(row["qty_a"], 20)
        self.assertAlmostEqual(row["qty_b"], 20)
        self.assertGreater(row["locked_profit"], 0.5)          # ~4c a pair on 20 pairs
        venues = [f["venue"] for f in json.loads(row["fills"])]
        self.assertEqual(venues, ["Polymarket", "Kalshi"])
        self.assertIn("filled", self.db.execute("SELECT note FROM quotes").fetchone()[0])

    def test_fill_with_the_hedge_gone_is_held_and_counts_as_failure(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        k.book.asks = [(0.70, 100)]                            # Kalshi ran before we saw the fill
        sold_into = Book(asks=[(0.39, 50)], bids=[(0.30, 100)], fetched_at=time.time())
        with mock.patch.object(trader, "live_book", return_value=sold_into):
            maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        row = self.db.execute("SELECT * FROM trades").fetchone()
        self.assertEqual(row["outcome"], "exposed")
        self.assertIn("HELD to settlement", row["note"])
        self.assertEqual(trader.recent_failures(self.db, "dry", 2), 1)

    def test_rejects_pause_maker(self):
        g, pm, k = pair()
        self.paper["Polymarket"].post = lambda *a, **k: Fill("Polymarket", "buy", "x", status="rejected", error="nope")
        for _ in range(3):
            maker.STATE.last_post = 0
            maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertIn("refused 3", maker.STATE.paused)
        maker.STATE.last_post = 0
        self.paper["Polymarket"].post = mock.Mock()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.paper["Polymarket"].post.assert_not_called()


class ChurnTests(Base):
    def test_event_on_another_game_does_not_move_the_quote(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        oid = maker.STATE.quote.oid
        other, pm2, k2 = pair(pm_bids=[(0.20, 10)], pm_asks=[(0.60, 10)])     # a juicier pair elsewhere
        other.away = "LV"
        for x in (pm2, k2):
            x.ref["ticker"] += "-2"
            x.ref["slug"] = x.ref.get("slug", "") + "-2"
        maker.STATE.quote.priced_at -= 10
        maker.step(self.db, self.cfg, self.paper, [other], full=False, **QUIET)   # event path, our game not in view
        self.assertEqual(maker.STATE.quote.oid, oid)

    def test_outbid_reprices_the_same_pair(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        pm.book.bids = [(0.40, 50)]                               # someone bid over our 0.39
        maker.STATE.quote.priced_at -= 10
        maker.STATE.last_post -= 10
        logs = []
        maker.step(self.db, self.cfg, self.paper, [g], log=logs.append, full=True)
        self.assertAlmostEqual(maker.STATE.quote.price, 0.41)
        self.assertTrue(any("re-priced KC @ DEN Under 44.5 0.39 -> 0.41 (modify)" in x for x in logs))
        self.assertEqual(self.paper["Polymarket"].quotes[maker.STATE.quote.oid]["price"], 0.41)   # same order id
        self.assertEqual(self.db.execute("SELECT price FROM quotes").fetchone()[0], 0.41)

    def test_modify_applied_a_moment_later_still_counts(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        b = self.paper["Polymarket"]
        reads = {"n": 0}
        real = b.order_state

        def lagging(c_, oid):
            reads["n"] += 1
            st = real(c_, oid)
            return {**st, "price": 0.39} if reads["n"] < 3 else st     # the first two reads still show the old price
        b.order_state = lagging
        pm.book.bids = [(0.40, 50)]
        maker.STATE.quote.priced_at -= 10
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertTrue(maker.STATE.modify_ok)
        self.assertAlmostEqual(maker.STATE.quote.price, 0.41)

    def test_modify_that_does_not_take_falls_back_to_cancel_and_post(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        old = maker.STATE.quote.oid
        self.paper["Polymarket"].modify = lambda *a, **kw: (False, "HTTP 400: bad modify")   # refused outright
        pm.book.bids = [(0.40, 50)]
        maker.STATE.quote.priced_at -= 10
        logs = []
        maker.step(self.db, self.cfg, self.paper, [g], log=logs.append, full=True)
        self.assertFalse(maker.STATE.modify_ok)
        self.assertNotEqual(maker.STATE.quote.oid, old)                 # a fresh order at the new price
        self.assertAlmostEqual(maker.STATE.quote.price, 0.41)
        self.assertTrue(any("modify did not take" in x for x in logs))

    def test_polymarket_live_flag_pulls(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        pm.ref["live"] = True
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        self.assertIsNone(maker.STATE.quote)


def second_game(away="LV"):
    g, pm, k = pair(pm_bids=[(0.30, 10)], pm_asks=[(0.60, 10)])
    g.away = away
    for x in (pm, k):
        x.ref["ticker"] += "-" + away
        if "slug" in x.ref:
            x.ref["slug"] += "-" + away
    return g, pm, k


class MultiQuoteTests(Base):
    def test_one_quote_per_game_up_to_the_slots(self):
        g1, _, _ = pair()
        g2, _, _ = second_game("LV")
        g3, _, _ = second_game("SEA")
        g4, _, _ = second_game("SF")
        for _ in range(6):
            maker.STATE.last_post = 0
            maker.step(self.db, self.cfg, self.paper, [g1, g2, g3, g4], full=True, **QUIET)
        qs = list(maker.STATE.quotes.values())
        self.assertEqual(len(qs), 3)
        self.assertEqual(len({q.game_key for q in qs}), 3)

    def test_day_room_is_split_across_slots(self):
        cfg = trader.Config(maker=True, max_per_trade=20, max_daily_spend=12, min_profit=0.0)
        g1, _, _ = pair()
        g2, _, _ = second_game("LV")
        for _ in range(3):
            maker.STATE.last_post = 0
            maker.step(self.db, cfg, self.paper, [g1, g2], full=True, **QUIET)
        qs = list(maker.STATE.quotes.values())
        self.assertEqual(len(qs), 2)
        self.assertLessEqual(sum(q.outlay() for q in qs), 12 + 1e-6)   # all filling at once still fits the day cap

    def test_shutdown_pulls_every_quote(self):
        g1, _, _ = pair()
        g2, _, _ = second_game("LV")
        for _ in range(3):
            maker.STATE.last_post = 0
            maker.step(self.db, self.cfg, self.paper, [g1, g2], full=True, **QUIET)
        self.assertEqual(len(maker.STATE.quotes), 2)
        maker.shutdown(self.db, self.cfg, self.paper, **QUIET)
        self.assertEqual(maker.STATE.quotes, {})
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM quotes WHERE closed_at IS NULL").fetchone()[0], 0)


class RecoverTests(Base):
    def _left_open(self, pm, k):
        self.db.execute("INSERT INTO quotes (order_id, mode, posted_at, game, opp_key, pm, k, price, qty, k_ask, "
                        "guaranteed, filled) VALUES ('old1','dry',?,?,?,?,?,0.39,20,0.55,1.0,0)",
                        (time.time() - 60, "KC @ DEN", f"{k.id}|{pm.id}", maker._snap(pm), maker._snap(k)))
        self.db.commit()

    def test_unfilled_leftover_is_cancelled(self):
        g, pm, k = pair()
        self._left_open(pm, k)
        self.paper["Polymarket"].cancel = mock.Mock(return_value={"state": "canceled", "done": True, "cum": 0.0})
        self.assertEqual(maker.recover(self.db, self.cfg, self.paper, **QUIET), 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0], 0)
        self.assertIsNotNone(self.db.execute("SELECT closed_at FROM quotes").fetchone()[0])

    def test_leftover_filled_while_down_is_booked_and_halts(self):
        g, pm, k = pair()
        self._left_open(pm, k)
        self.paper["Polymarket"].cancel = mock.Mock(return_value={"state": "ORDER_STATE_FILLED", "done": True,
                                                                  "cum": 20.0, "avg": 0.39, "fee": 0.0})
        maker.recover(self.db, self.cfg, self.paper, **QUIET)
        row = self.db.execute("SELECT * FROM trades").fetchone()
        self.assertEqual(row["outcome"], "error")
        self.assertAlmostEqual(row["qty_b"], 20)               # the Polymarket leg is leg b of this pair
        self.assertAlmostEqual(row["cash_out"], 7.8)
        self.assertEqual(trader.recent_failures(self.db, "dry", 1), 1)


class PolymarketBrokerMakerTests(unittest.TestCase):
    def setUp(self):
        import base64
        self.b = brokers.PolymarketBroker(key_id="k", secret_key_b64=base64.b64encode(b"\x01" * 32).decode())
        self.pm = c("Polymarket", "total", "lt", 44.5, [], tid="pm-under", pm_side="short")

    def test_post_is_resting_post_only_gtc_on_the_yes_price(self):
        sent = {}
        with mock.patch.object(self.b, "_req", side_effect=lambda m, p, body=None: sent.update(body or {}) or {"id": "o1"}):
            f = self.b.post(self.pm, 20, 0.39)
        self.assertEqual((f.status, f.order_id), ("resting", "o1"))
        self.assertEqual(sent["tif"], "TIME_IN_FORCE_GOOD_TILL_CANCEL")
        self.assertTrue(sent["participateDontInitiate"])
        self.assertEqual(sent["intent"], "ORDER_INTENT_BUY_SHORT")
        self.assertEqual(sent["price"]["value"], "0.6100")    # a short bought at 0.39 = YES at 0.61

    def test_modify_sends_the_yes_price_in_place(self):
        sent = {}
        with mock.patch.object(self.b, "_req", side_effect=lambda m, p, body=None: sent.update(path=p, **(body or {})) or {}):
            ok, err = self.b.modify(self.pm, "o1", 0.41, 20)
        self.assertTrue(ok)
        self.assertEqual(sent["path"], "/v1/order/o1/modify")
        self.assertEqual(sent["price"]["value"], "0.5900")
        self.assertTrue(sent["participateDontInitiate"])

    def test_cancel_reads_the_fill(self):
        replies = iter([{}, {"order": {"state": "ORDER_STATE_CANCELED", "cumQuantity": "5", "avgPx": {"value": "0.61"}}}])
        with mock.patch.object(self.b, "_req", side_effect=lambda *a, **k: next(replies)):
            st = self.b.cancel(self.pm, "o1")
        self.assertTrue(st["done"])
        self.assertAlmostEqual(st["cum"], 5)
        self.assertAlmostEqual(st["avg"], 0.39)


if __name__ == "__main__":
    unittest.main()
