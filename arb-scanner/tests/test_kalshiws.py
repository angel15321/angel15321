"""Offline tests for the live Kalshi websocket book feed.

Run:  python3 -m unittest discover -s tests
"""
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbscan import kalshi, kalshiws  # noqa: E402


def feed():
    """A BookFeed without its network thread."""
    f = object.__new__(kalshiws.BookFeed)
    f.books, f.wanted, f._lock, f._loop, f._ws = {}, set(), threading.Lock(), None, None
    f._reset_session()
    f.connected, f.last_msg, f.last_book, f.reconnects, f.gaps, f.resyncs, f.error = True, time.time(), 0.0, 0, 0, 0, ""
    f.sid = 1
    return f


def frame(typ, sid, seq, **msg):
    return json.dumps({"type": typ, "sid": sid, "seq": seq, "msg": msg})


class BookTests(unittest.TestCase):
    def test_snapshot_then_deltas(self):
        f = feed()
        f._handle(frame("orderbook_snapshot", 1, 1, market_ticker="T",
                        yes_dollars_fp=[["0.4000", "100.00"]], no_dollars_fp=[["0.5500", "50.00"], ["0.5400", "10.00"]]))
        f._handle(frame("orderbook_delta", 1, 2, market_ticker="T", price_dollars="0.5500", delta_fp="-50.00", side="no"))
        f._handle(frame("orderbook_delta", 1, 3, market_ticker="T", price_dollars="0.4200", delta_fp="7.00", side="yes"))
        yes, no = f.get("T").levels()
        self.assertEqual(yes, [(0.42, 7.0), (0.40, 100.0)])
        self.assertEqual(no, [(0.54, 10.0)])            # the 0.55 level went to 0 and was removed

    def test_float_residue_never_leaves_a_phantom_level(self):
        f = feed()
        f._handle(frame("orderbook_snapshot", 1, 1, market_ticker="T", yes_dollars_fp=[["0.3000", "0.10"]]))
        for i in range(2, 5):
            f._handle(frame("orderbook_delta", 1, i, market_ticker="T", price_dollars="0.3000", delta_fp="0.10", side="yes"))
        f._handle(frame("orderbook_delta", 1, 5, market_ticker="T", price_dollars="0.3000", delta_fp="-0.40", side="yes"))
        self.assertEqual(f.get("T").levels()[0], [])

    def test_control_acks_advance_seq_no_false_gap(self):
        f = feed()
        f._handle(frame("orderbook_snapshot", 1, 1, market_ticker="A", yes_dollars_fp=[["0.4000", "1.00"]]))
        f._handle(json.dumps({"type": "ok", "sid": 1, "seq": 2, "msg": {"market_tickers": ["B"]}}))
        f._handle(frame("orderbook_snapshot", 1, 3, market_ticker="B", yes_dollars_fp=[["0.4000", "1.00"]]))
        self.assertEqual(f.gaps, 0)
        self.assertIsNotNone(f.get("A"))
        self.assertIsNotNone(f.get("B"))

    def test_real_gap_rebuilds_the_feed(self):
        f = feed()
        f._handle(frame("orderbook_snapshot", 1, 1, market_ticker="A", yes_dollars_fp=[["0.4000", "1.00"]]))
        f._handle(frame("orderbook_delta", 1, 5, market_ticker="A", price_dollars="0.4000", delta_fp="1.00", side="yes"))
        self.assertEqual(f.gaps, 1)
        self.assertIsNone(f.get("A"))                   # rebuilding: nothing served until fresh snapshots

    def test_dead_subscription_error_rebuilds(self):
        f = feed()
        f._handle(frame("orderbook_snapshot", 1, 1, market_ticker="A", yes_dollars_fp=[["0.4000", "1.00"]]))
        f._handle(json.dumps({"type": "error", "id": 3, "msg": {"code": 25, "msg": "Subscription buffer overflow"}}))
        self.assertIsNone(f.get("A"))

    def test_lagging_deltas_rebuild(self):
        f = feed()
        f._handle(frame("orderbook_snapshot", 1, 1, market_ticker="A", yes_dollars_fp=[["0.4000", "1.00"]]))
        f._handle(frame("orderbook_delta", 1, 2, market_ticker="A", price_dollars="0.4000", delta_fp="1.00",
                        side="yes", ts_ms=int((time.time() - 5) * 1000)))
        self.assertIsNone(f.get("A"))

    def test_subscribe_once_then_grow_in_place(self):
        f = feed()
        f.sid, sent = None, []

        class WS:
            async def send(self, s):
                sent.append(json.loads(s))
        f._ws = WS()
        import asyncio
        f.wanted = {"A", "B"}
        asyncio.run(f._grow())
        self.assertEqual(sent[-1]["cmd"], "subscribe")
        f.wanted |= {"C"}
        asyncio.run(f._grow())                          # not acked yet: wait
        self.assertEqual(len(sent), 1)
        f._handle(json.dumps({"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 9}}))
        asyncio.run(f._grow())
        self.assertEqual((sent[-1]["cmd"], sent[-1]["params"]["action"], sent[-1]["params"]["sids"],
                          sent[-1]["params"]["market_tickers"]), ("update_subscription", "add_markets", [9], ["C"]))

    def test_dead_feed_serves_nothing(self):
        f = feed()
        f._handle(frame("orderbook_snapshot", 1, 1, market_ticker="T", yes_dollars_fp=[["0.4000", "1.00"]]))
        f.last_book = time.time() - kalshiws.DEAD_AFTER - 1
        self.assertIsNone(f.get("T"))


class FetchBooksTests(unittest.TestCase):
    def tearDown(self):
        kalshi.use_feed(None)

    def test_live_books_from_memory_rest_only_for_missing(self):
        f = feed()
        f._handle(frame("orderbook_snapshot", 1, 1, market_ticker="LIVE",
                        yes_dollars_fp=[["0.4000", "10.00"]], no_dollars_fp=[["0.5500", "20.00"]]))
        kalshi.use_feed(f)
        rest = {"orderbooks": [{"ticker": "NEW", "orderbook_fp": {"yes_dollars": [["0.30", "5"]], "no_dollars": [["0.60", "5"]]}}]}
        with mock.patch("arbscan.http.get", return_value=rest) as get:
            out = kalshi.fetch_books(["LIVE", "NEW"])
        self.assertEqual(get.call_args[0][1]["tickers"], ["NEW"])
        yes, no = out["LIVE"]
        self.assertEqual(yes.asks, [(0.45, 20.0)])     # YES ask = 1 - best NO bid
        self.assertEqual(no.asks, [(0.6, 10.0)])
        self.assertLess(time.time() - yes.fetched_at, 0.5)
        self.assertIn("NEW", out)
        self.assertEqual(f.wanted, {"LIVE", "NEW"})


if __name__ == "__main__":
    unittest.main()
