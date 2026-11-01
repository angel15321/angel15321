"""Offline tests for the auto-arb executor and the venue brokers.

No network, no real orders: books are synthetic and venue APIs are faked.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_engine import K, P, c, game  # noqa: E402

from arbscan import brokers, engine, trader  # noqa: E402
from arbscan.model import Book  # noqa: E402

QUIET = dict(log=lambda *_: None)


def opp(a_asks, b_asks, a_bids=None, b_bids=None, a_venue="Kalshi", b_venue="Polymarket", fee_a=None, fee_b=None):
    a = c(a_venue, "total", "gt", 44.5, a_asks, **({"fee": fee_a} if fee_a else {}))
    b = c(b_venue, "total", "lt", 44.5, b_asks, **({"fee": fee_b} if fee_b else {}))
    a.book.bids = a_bids or []
    b.book.bids = b_bids or []
    a.label, b.label = "Over 44.5", "Under 44.5"
    arbs, _ = engine.find([game(a, b)])
    return arbs[0]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()                  # Windows cannot unlink a file that is still open
        self.db = trader.connect(self.tmp.name)
        self.cfg = trader.Config(min_profit=0.01, min_roi=0.0)
        self.paper = {"Kalshi": brokers.PaperBroker("Kalshi", latency=0),
                      "Polymarket": brokers.PaperBroker("Polymarket", latency=0)}

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)


class ExecuteTests(Base):
    def test_hedged_and_polymarket_first(self):
        o = opp([(0.45, 100)], [(0.50, 100)])
        order = []
        for v, b in self.paper.items():
            orig = b.buy
            b.buy = (lambda orig, v: lambda *x, **k: (order.append(v), orig(*x, **k))[1])(orig, v)
        with mock.patch("arbscan.scanner.refresh"):
            row = trader.execute(self.db, o, self.paper, self.cfg, budget=1000, **QUIET)
        self.assertEqual(order, ["Polymarket", "Kalshi"])
        self.assertEqual(row["outcome"], "hedged")
        self.assertAlmostEqual(row["locked_profit"], 5.0)
        self.assertEqual(row["status"], "open")

    def test_budget_caps_size(self):
        o = opp([(0.45, 1000)], [(0.50, 1000)])
        with mock.patch("arbscan.scanner.refresh"):
            row = trader.execute(self.db, o, self.paper, self.cfg, budget=95, **QUIET)
        self.assertLessEqual(row["cash_out"], 95 + 1e-9)
        self.assertEqual(row["qty_a"], 100)

    def test_missed_when_leg1_gone(self):
        o = opp([(0.45, 100)], [(0.50, 100)])

        def gone(cs):
            for x in cs:
                if x.venue == "Polymarket":
                    x.book = Book(asks=[(0.80, 100)])
        with mock.patch("arbscan.scanner.refresh", side_effect=gone):
            row = trader.execute(self.db, o, self.paper, self.cfg, budget=1000, **QUIET)
        self.assertEqual(row["outcome"], "missed")
        self.assertEqual(row["status"], "closed")
        self.assertAlmostEqual(row["cash_out"], 0)

    def _leg2_moves(self, cfg):
        o = opp([(0.45, 100)], [(0.50, 100)], b_bids=[(0.49, 500)])

        def move(cs):
            for x in cs:
                if x.venue == "Kalshi":
                    x.book = Book(asks=[(0.60, 500)])
                elif x.venue == "Polymarket":
                    x.book = Book(asks=[(0.50, 100)], bids=[(0.49, 500)])
        with mock.patch("arbscan.scanner.refresh", side_effect=move):
            return trader.execute(self.db, o, self.paper, cfg, budget=1000, **QUIET)

    def test_unwind_when_hedge_fails(self):
        row = self._leg2_moves(self.cfg)
        self.assertEqual(row["outcome"], "unwound")
        self.assertEqual(row["status"], "closed")
        self.assertAlmostEqual(row["cash_out"], 1.0)      # bought 100 @ .50, sold 100 @ .49
        self.assertAlmostEqual(row["pnl"], -1.0)
        self.assertLess(row["qty_a"] + row["qty_b"], 0.01)

    def test_hold_when_hedge_fails(self):
        self.cfg.on_fail = "hold"
        row = self._leg2_moves(self.cfg)
        self.assertEqual(row["outcome"], "exposed")
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["qty_b"], 100)

    def test_leg1_limit_never_breaks_even_against_leg2(self):
        # Leg 2 (Kalshi) at 0.52 + fee: leg 1 limit must stay below 1 - 0.52 - fee.
        o = opp([(0.45, 100), (0.52, 100)], [(0.47, 100)], fee_a=K, fee_b=P)
        sent = {}
        b = self.paper["Polymarket"]
        orig = b.buy

        def spy(cn, q, limit, notional_hint=None):
            sent["limit"] = limit
            return orig(cn, q, limit, notional_hint)
        b.buy = spy
        with mock.patch("arbscan.scanner.refresh"):
            trader.execute(self.db, o, self.paper, self.cfg, budget=1000, **QUIET)
        worst2 = o.max.fills_a[-1][0]
        self.assertLessEqual(sent["limit"] + P.per_contract(sent["limit"]) + worst2 + K.per_contract(worst2), 1.0)

    def test_settle_hedged(self):
        o = opp([(0.45, 100)], [(0.50, 100)])
        with mock.patch("arbscan.scanner.refresh"):
            trader.execute(self.db, o, self.paper, self.cfg, budget=1000, **QUIET)
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=5)).isoformat()
        self.db.execute("UPDATE trades SET kickoff=?", (past,))
        with mock.patch("arbscan.scanner.settlement", side_effect=lambda ref, v: 0.0 if v == "Kalshi" else 1.0):
            self.assertEqual(trader.settle(self.db, **QUIET), 1)
        self.assertAlmostEqual(trader.summary(self.db)["realized"], 5.0)


class SafetyTests(Base):
    def test_kill_file_stops_loop(self):
        with tempfile.TemporaryDirectory() as d:
            self.cfg.kill_file = os.path.join(d, "STOP")
            open(self.cfg.kill_file, "w").close()
            with mock.patch("arbscan.trader._tick") as tick:
                trader.run(self.db, self.cfg, self.paper, days=1, interval=0, **QUIET)
            tick.assert_not_called()

    def test_halts_after_failed_hedges(self):
        self.cfg.on_fail = "hold"
        self._fail_twice()
        with mock.patch("arbscan.trader._tick") as tick:
            trader.run(self.db, self.cfg, self.paper, days=1, interval=0, **QUIET)
        tick.assert_not_called()

    def _fail_twice(self):
        for _ in range(2):
            ExecuteTests._leg2_moves(self, self.cfg)

    def test_daily_spend_tracked(self):
        o = opp([(0.45, 100)], [(0.50, 100)])
        with mock.patch("arbscan.scanner.refresh"):
            trader.execute(self.db, o, self.paper, self.cfg, budget=1000, **QUIET)
        self.assertAlmostEqual(trader.spent_today(self.db, "dry"), 95.0)
        self.assertAlmostEqual(trader.open_exposure(self.db, "dry"), 95.0)
        self.assertEqual(trader.spent_today(self.db, "live"), 0)


class KalshiBrokerTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        self.b = brokers.KalshiBroker(key_id="test-key", private_key_pem=pem)

    def _contract(self, side):
        x = c("Kalshi", "total", "gt", 44.5, [(0.4, 10)], fee=K)
        x.ref = {"ticker": "KXNFLTOTAL-26NOV01KCDEN-45", "side": side}
        return x

    def test_signature_headers(self):
        h = self.b._sign("POST", "/trade-api/v2/portfolio/events/orders?x=1")
        self.assertEqual(h["KALSHI-ACCESS-KEY"], "test-key")
        self.assertTrue(h["KALSHI-ACCESS-TIMESTAMP"].isdigit())
        self.assertGreater(len(h["KALSHI-ACCESS-SIGNATURE"]), 100)

    def test_buy_no_maps_to_yes_ask(self):
        resp = {"order_id": "o1", "fill_count": "10.00", "remaining_count": "0.00",
                "average_fill_price": "0.6200", "average_fee_paid": "0.0165"}
        with mock.patch.object(self.b, "_req", return_value=resp) as req:
            f = self.b.buy(self._contract("no"), 10, 0.38)
        body = req.call_args[0][2]
        self.assertEqual(body["side"], "ask")
        self.assertEqual(body["price"], "0.6200")
        self.assertEqual(body["time_in_force"], "immediate_or_cancel")
        self.assertFalse(body["reduce_only"])
        self.assertAlmostEqual(f.avg_price, 0.38)      # NO cost = 1 - YES price
        self.assertAlmostEqual(f.fee, 0.165)
        self.assertEqual(f.qty, 10)

    def test_buy_yes_and_sell_yes(self):
        resp = {"order_id": "o", "fill_count": "5.00", "average_fill_price": "0.4000", "average_fee_paid": "0.02"}
        with mock.patch.object(self.b, "_req", return_value=resp) as req:
            self.b.buy(self._contract("yes"), 5, 0.40)
            self.assertEqual((req.call_args[0][2]["side"], req.call_args[0][2]["price"]), ("bid", "0.4000"))
            self.b.sell(self._contract("yes"), 5, 0.37)
            body = req.call_args[0][2]
            self.assertEqual((body["side"], body["price"], body["reduce_only"]), ("ask", "0.3700", True))

    def test_sell_no_maps_to_yes_bid(self):
        resp = {"order_id": "o", "fill_count": "5.00", "average_fill_price": "0.6500", "average_fee_paid": "0"}
        with mock.patch.object(self.b, "_req", return_value=resp) as req:
            f = self.b.sell(self._contract("no"), 5, 0.35)
        body = req.call_args[0][2]
        self.assertEqual((body["side"], body["price"], body["reduce_only"]), ("bid", "0.6500", True))
        self.assertAlmostEqual(f.avg_price, 0.35)

    def test_http_error_becomes_failed_fill(self):
        with mock.patch.object(self.b, "_req", side_effect=RuntimeError("HTTP 400")):
            f = self.b.buy(self._contract("yes"), 5, 0.4)
        self.assertEqual((f.qty, f.status), (0.0, "error"))


def _exec(kind, shares, yes_px, state="ORDER_STATE_FILLED", cum=None, avg=None, fee="0.02"):
    order = {"state": state, "cumQuantity": shares if cum is None else cum,
             "avgPx": {"value": f"{yes_px if avg is None else avg:.4f}"},
             "commissionNotionalTotalCollected": {"value": fee}}
    return {"type": kind, "lastShares": str(shares), "lastPx": {"value": f"{yes_px:.4f}"}, "order": order}


class PolymarketBrokerTests(unittest.TestCase):
    def setUp(self):
        import base64
        self.b = brokers.PolymarketBroker(key_id="kid", secret_key_b64=base64.b64encode(b"" * 64).decode())
        self.long = c("Polymarket", "margin", "lt", 0.0, [(0.5, 100)], fee=P, kind="ml", tie=True)
        self.short = c("Polymarket", "margin", "gt", 0.0, [(0.5, 100)], fee=P, kind="ml", tie=True, pm_side="short")
        for x in (self.long, self.short):
            x.tick = 0.0025

    def test_signature_headers(self):
        h = self.b._sign("POST", "/v1/orders")
        self.assertEqual(h["X-PM-Access-Key"], "kid")
        self.assertTrue(h["X-PM-Timestamp"].isdigit())
        self.assertGreater(len(h["X-PM-Signature"]), 80)

    def test_buy_long_ioc_at_yes_limit(self):
        resp = {"id": "o1", "executions": [_exec("EXECUTION_TYPE_FILL", 40, 0.4975, fee="0.48")]}
        with mock.patch.object(self.b, "_req", return_value=resp) as req:
            f = self.b.buy(self.long, 40, 0.50)
        body = req.call_args[0][2]
        self.assertEqual((body["intent"], body["price"]["value"], body["tif"], body["quantity"]),
                         ("ORDER_INTENT_BUY_LONG", "0.5000", "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", 40))
        self.assertTrue(body["synchronousExecution"])
        self.assertEqual((f.qty, f.status), (40, "filled"))
        self.assertAlmostEqual(f.avg_price, 0.4975)
        self.assertAlmostEqual(f.fee, 0.48)

    def test_buy_short_sends_yes_price_and_reports_no_cost(self):
        # NO for at most 0.31 -> sell YES at no less than 0.69; filled at YES 0.70 -> NO cost 0.30
        resp = {"id": "o2", "executions": [_exec("EXECUTION_TYPE_FILL", 10, 0.70)]}
        with mock.patch.object(self.b, "_req", return_value=resp) as req:
            f = self.b.buy(self.short, 10, 0.31)
        body = req.call_args[0][2]
        self.assertEqual((body["intent"], body["price"]["value"]), ("ORDER_INTENT_BUY_SHORT", "0.6900"))
        self.assertAlmostEqual(f.avg_price, 0.30)

    def test_short_limit_rounds_to_safe_tick(self):
        # NO limit 0.3101 -> YES floor 0.6899 must round UP to 0.6900 (never pay more than the limit)
        with mock.patch.object(self.b, "_req", return_value={"id": "o", "executions": []}) as req,                 mock.patch.object(self.b, "_final", return_value={"state": "ORDER_STATE_CANCELED", "cumQuantity": 0}):
            self.b.buy(self.short, 10, 0.3101)
        self.assertEqual(req.call_args[0][2]["price"]["value"], "0.6900")

    def test_sell_long_and_sell_short(self):
        resp = {"id": "o", "executions": [_exec("EXECUTION_TYPE_FILL", 5, 0.45)]}
        with mock.patch.object(self.b, "_req", return_value=resp) as req:
            self.b.sell(self.long, 5, 0.44)
            self.assertEqual((req.call_args[0][2]["intent"], req.call_args[0][2]["price"]["value"]),
                             ("ORDER_INTENT_SELL_LONG", "0.4400"))
            f = self.b.sell(self.short, 5, 0.44)
            self.assertEqual((req.call_args[0][2]["intent"], req.call_args[0][2]["price"]["value"]),
                             ("ORDER_INTENT_SELL_SHORT", "0.5600"))
        self.assertAlmostEqual(f.avg_price, 0.55)     # NO sold = 1 - YES bought back at 0.45

    def test_partial_fill(self):
        resp = {"id": "o", "executions": [_exec("EXECUTION_TYPE_PARTIAL_FILL", 7, 0.5, state="ORDER_STATE_CANCELED")]}
        with mock.patch.object(self.b, "_req", return_value=resp):
            f = self.b.buy(self.long, 20, 0.5)
        self.assertEqual(f.qty, 7)

    def test_rejected(self):
        rej = {"type": "EXECUTION_TYPE_REJECTED", "orderRejectReason": "ORD_REJECT_REASON_NO_LIQUIDITY", "text": "",
               "order": {"state": "ORDER_STATE_REJECTED", "cumQuantity": 0}}
        with mock.patch.object(self.b, "_req", return_value={"id": "o", "executions": [rej]}):
            f = self.b.buy(self.long, 20, 0.5)
        self.assertEqual((f.qty, f.status), (0, "rejected"))
        self.assertIn("NO_LIQUIDITY", f.error)

    def test_unfinished_order_is_polled_then_cancelled(self):
        calls = []

        def fake(method, path, body=None):
            calls.append((method, path))
            if path == "/v1/orders":
                return {"id": "o9"}
            if method == "GET":
                return {"order": {"state": "ORDER_STATE_NEW", "cumQuantity": 0}}
            return {}
        with mock.patch.object(self.b, "_req", side_effect=fake), mock.patch("time.sleep"),                 mock.patch("time.time", side_effect=[0, 0, 100, 100, 100, 100]):
            f = self.b.buy(self.long, 20, 0.5)
        self.assertEqual(f.qty, 0)
        self.assertIn(("POST", "/v1/order/o9/cancel"), calls)

    def test_http_error_becomes_failed_fill(self):
        with mock.patch.object(self.b, "_req", side_effect=RuntimeError("HTTP 400")):
            f = self.b.buy(self.long, 5, 0.4)
        self.assertEqual((f.qty, f.status), (0.0, "error"))

    def test_balance_is_buying_power(self):
        with mock.patch.object(self.b, "_req", return_value={"balances": [{"buyingPower": 25.0036, "currentBalance": 30}]}):
            self.assertAlmostEqual(self.b.balance(), 25.0036)


class LegOrderTests(unittest.TestCase):
    def _opp(self, live):
        o = opp([(0.45, 100)], [(0.50, 100)])
        o.game.start = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=-1 if live else 1)
        return o

    def test_pregame_polymarket_first(self):
        o = self._opp(live=False)
        self.assertEqual(trader.first_leg(o, trader.Config(), {o.a.id: 0.10, o.b.id: 0.0}), "b")

    def test_in_play_stale_venue_first(self):
        o = self._opp(live=True)          # a = Kalshi, b = Polymarket
        # Polymarket repriced 8c since the last scan, Kalshi did not: Kalshi is stale -> first
        self.assertEqual(trader.first_leg(o, trader.Config(), {o.a.id: 0.0, o.b.id: 0.08}), "a")
        self.assertEqual(trader.first_leg(o, trader.Config(), {o.a.id: 0.08, o.b.id: 0.0}), "b")
        # no history yet, or both moved alike -> default Polymarket first
        self.assertEqual(trader.first_leg(o, trader.Config(), {}), "b")
        self.assertEqual(trader.first_leg(o, trader.Config(), {o.a.id: 0.03, o.b.id: 0.03}), "b")

    def test_forced_leg1(self):
        o = self._opp(live=True)
        self.assertEqual(trader.first_leg(o, trader.Config(leg1="kalshi"), {o.a.id: 0.1, o.b.id: 0}), "a")
        self.assertEqual(trader.first_leg(o, trader.Config(leg1="polymarket"), {}), "b")

    def test_moves_track_mid_between_scans(self):
        trader._last_mid.clear()
        o = opp([(0.45, 100)], [(0.50, 100)], a_bids=[(0.43, 10)], b_bids=[(0.48, 10)])
        self.assertEqual(trader._moves([o.game]), {})
        o.a.book.asks, o.a.book.bids = [(0.55, 100)], [(0.53, 10)]
        mv = trader._moves([o.game])
        self.assertAlmostEqual(mv[o.a.id], 0.10)
        self.assertAlmostEqual(mv[o.b.id], 0.0)


class HelperTests(unittest.TestCase):
    def test_max_price_within(self):
        x = c("Kalshi", "total", "gt", 44.5, [], fee=K)
        p = brokers.max_price_within(0.52, x)
        self.assertLessEqual(p + K.per_contract(p), 0.52)
        self.assertGreater(p + 0.01 + K.per_contract(p + 0.01), 0.52)


if __name__ == "__main__":
    unittest.main()
