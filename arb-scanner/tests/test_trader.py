"""Offline tests for the auto-arb executor and the venue brokers.

No network, no real orders: books are synthetic and venue APIs are faked.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
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


class PolymarketBrokerTests(unittest.TestCase):
    def setUp(self):
        self.b = brokers.PolymarketBroker.__new__(brokers.PolymarketBroker)
        self.b.client = mock.Mock()
        self.cn = c("Polymarket", "total", "lt", 44.5, [(0.5, 100)], fee=P)
        self.cn.ref["token_id"] = "tok"

    def test_matched_immediately(self):
        self.b.client.place_market_order.return_value = SimpleNamespace(
            ok=True, order_id="x", status="matched", making_amount="25.00", taking_amount="50.00")
        f = self.b.buy(self.cn, 50, 0.51, notional_hint=25.5)
        kw = self.b.client.place_market_order.call_args.kwargs
        self.assertEqual((kw["side"], kw["order_type"], kw["max_price"], kw["amount"]), ("BUY", "FAK", "0.5100", "25.50"))
        self.assertEqual((f.qty, round(f.avg_price, 4)), (50.0, 0.5))

    def test_delayed_then_matched(self):
        self.b.client.place_market_order.return_value = SimpleNamespace(
            ok=True, order_id="x", status="delayed", making_amount="0", taking_amount="0")
        self.b.client.get_order.return_value = SimpleNamespace(status="matched", size_matched="40", price="0.5")
        with mock.patch("time.sleep"):
            f = self.b.buy(self.cn, 50, 0.51)
        self.assertEqual(f.qty, 40.0)
        self.assertAlmostEqual(f.avg_price, 0.5)

    def test_delayed_never_matches_is_cancelled(self):
        self.b.client.place_market_order.return_value = SimpleNamespace(
            ok=True, order_id="x", status="delayed", making_amount="0", taking_amount="0")
        self.b.client.get_order.return_value = SimpleNamespace(status="delayed", size_matched="0", price="0.5")
        with mock.patch("time.sleep"), mock.patch("time.time", side_effect=[0, 0, 100, 100, 100]):
            f = self.b.buy(self.cn, 50, 0.51)
        self.assertEqual(f.qty, 0)
        self.b.client.cancel_order.assert_called_once()

    def test_rejected(self):
        self.b.client.place_market_order.return_value = SimpleNamespace(ok=False, code="X", message="not enough balance")
        f = self.b.buy(self.cn, 50, 0.51)
        self.assertEqual((f.qty, f.status), (0.0, "rejected"))
        self.assertIn("balance", f.error)


class HelperTests(unittest.TestCase):
    def test_max_price_within(self):
        x = c("Kalshi", "total", "gt", 44.5, [], fee=K)
        p = brokers.max_price_within(0.52, x)
        self.assertLessEqual(p + K.per_contract(p), 0.52)
        self.assertGreater(p + 0.01 + K.per_contract(p + 0.01), 0.52)


if __name__ == "__main__":
    unittest.main()
