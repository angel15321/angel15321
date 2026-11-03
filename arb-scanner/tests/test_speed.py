"""Offline tests for fresh books, the book-age gate, both-legs-at-once orders and per-shard Kalshi cash.

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

from test_trader import opp  # noqa: E402

from arbscan import brokers, engine, polymarket, scanner, trader  # noqa: E402
from arbscan.model import Book  # noqa: E402

QUIET = dict(log=lambda *_: None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = trader.connect(self.tmp.name)
        self.paper = {"Kalshi": brokers.PaperBroker("Kalshi", latency=0),
                      "Polymarket": brokers.PaperBroker("Polymarket", latency=0)}
        trader._cash.update(at=0.0, values=None)

    def tearDown(self):
        self.db.close()
        os.unlink(self.tmp.name)


class ParallelLegTests(Base):
    def test_both_legs_sent_at_once_and_hedged(self):
        cfg = trader.Config(min_profit=0.01, min_roi=0.0, legs="parallel")
        o = opp([(0.45, 100)], [(0.50, 100)])
        sent = []
        for b in self.paper.values():
            orig = b.buy
            b.buy = (lambda orig: lambda cn, q, lim, **k: (sent.append((cn.venue, q, lim)), orig(cn, q, lim, **k))[1])(orig)
        with mock.patch("arbscan.scanner.refresh"):
            row = trader.execute(self.db, o, self.paper, cfg, budget=1000, **QUIET)
        self.assertEqual(row["outcome"], "hedged")
        self.assertEqual(sorted(v for v, _, _ in sent), ["Kalshi", "Polymarket"])   # one order each, no hedge retry
        # both limits together still leave the pair profitable
        lim = {v: l for v, _, l in sent}
        self.assertLess(lim["Kalshi"] + lim["Polymarket"], 1.0)

    def test_vanished_quote_leaves_the_other_leg_filled(self):
        # the reason sequential is the default: a quote that is gone by the time the order lands
        cfg = trader.Config(min_profit=0.01, min_roi=0.0, legs="parallel")
        o = opp([(0.45, 100)], [(0.50, 100)], a_bids=[(0.40, 100)])

        def gone(cs):
            for x in cs:
                if x.venue == "Polymarket":
                    x.book = Book(asks=[(0.80, 100)])
        with mock.patch("arbscan.scanner.refresh", side_effect=gone):
            row = trader.execute(self.db, o, self.paper, cfg, budget=1000, **QUIET)
        self.assertEqual(row["outcome"], "exposed")
        self.assertIn("HELD to settlement", row["note"])

    def test_sequential_is_default_and_misses_cleanly(self):
        self.assertEqual(trader.Config().legs, "sequential")
        o = opp([(0.45, 100)], [(0.50, 100)])

        def gone(cs):
            for x in cs:
                if x.venue == "Polymarket":
                    x.book = Book(asks=[(0.80, 100)])
        with mock.patch("arbscan.scanner.refresh", side_effect=gone):
            row = trader.execute(self.db, o, self.paper, trader.Config(min_profit=0.01, min_roi=0.0), budget=1000, **QUIET)
        self.assertEqual(row["outcome"], "missed")


class TickTests(Base):
    def _tick(self, o, cfg, feed=None, kalshi_shards=None):
        snap = scanner.Snapshot([o.game])
        o.game.start = None
        o.game.live = True
        seen = {}

        def run(db, opp_, brokers_, cfg_, budget, log, moves=None, leg_budgets=None):
            seen["budget"], seen["legs"] = budget, leg_budgets
            return {}
        if kalshi_shards is not None:
            self.paper["Kalshi"].shards = kalshi_shards
        with mock.patch.object(scanner, "snapshot", return_value=snap), \
                mock.patch.object(polymarket, "_feed", feed), \
                mock.patch.object(polymarket, "refresh") as pr, \
                mock.patch.object(trader, "execute", side_effect=run):
            logs = []
            trader._tick(self.db, cfg, self.paper, 8, log=logs.append)
        return seen, pr, logs

    def test_stale_kalshi_book_skipped(self):
        o = opp([(0.45, 100)], [(0.50, 100)])          # a = Kalshi, b = Polymarket
        o.a.book.fetched_at, o.b.book.fetched_at = time.time() - 2.0, time.time()
        seen, _, logs = self._tick(o, trader.Config(min_profit=0.01, min_roi=0.0, in_play_only=True))
        self.assertNotIn("budget", seen)
        self.assertIn("skipped (Kalshi book > 1s old)", logs[0])

    def test_quiet_polymarket_book_is_not_stale(self):
        o = opp([(0.45, 100)], [(0.50, 100)])
        o.a.book.fetched_at, o.b.book.fetched_at = time.time(), time.time() - 30.0
        seen, _, _ = self._tick(o, trader.Config(min_profit=0.01, min_roi=0.0, in_play_only=True))
        self.assertIn("budget", seen)

    def test_fresh_book_traded_and_feed_reread(self):
        o = opp([(0.45, 100)], [(0.50, 100)])
        o.a.book.fetched_at = o.b.book.fetched_at = time.time()
        feed = mock.Mock()
        feed.alive.return_value = True
        seen, pr, _ = self._tick(o, trader.Config(min_profit=0.01, min_roi=0.0, in_play_only=True), feed=feed)
        self.assertIn("budget", seen)
        pr.assert_called_once()                        # Polymarket books re-read from the feed before pricing
        self.assertEqual([x.venue for x in pr.call_args[0][0]], ["Polymarket"])

    def test_kalshi_leg_capped_by_its_shard_cash(self):
        o = opp([(0.45, 100)], [(0.50, 100)])          # a = Kalshi
        o.a.ref["shard"] = 0
        o.a.book.fetched_at = o.b.book.fetched_at = time.time()
        cfg = trader.Config(min_profit=0.01, min_roi=0.0, in_play_only=True)
        seen, _, logs = self._tick(o, cfg, kalshi_shards={0: 0.0, 3: 25.0})
        self.assertNotIn("budget", seen)               # $0 on shard 0 -> no trade
        self.assertTrue(any("legs $0/" in x for x in logs))
        trader._cash.update(at=0.0, values=None)
        seen, _, _ = self._tick(o, cfg, kalshi_shards={0: 10.0, 3: 15.0})
        # the Kalshi leg is capped by ITS shard's cash, the Polymarket leg by its own (not half the pair each)
        self.assertAlmostEqual(seen["legs"]["a"], 10.0 * 0.95)
        self.assertGreater(seen["legs"]["b"], 1000)
        self.assertAlmostEqual(seen["budget"], cfg.max_per_trade)


class KalshiShardBalanceTests(unittest.TestCase):
    def setUp(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        self.b = brokers.KalshiBroker(key_id="k", private_key_pem=pem)

    def test_restricted_key_rows(self):
        d = {"subaccount_balances": [{"balance": "10.0000", "exchange_index": 0, "subaccount_number": 2},
                                     {"balance": "15.0000", "exchange_index": 3, "subaccount_number": 2}]}
        with mock.patch.object(self.b, "_req", return_value=d):
            self.assertAlmostEqual(self.b.balance(), 25.0)
        self.assertEqual(self.b.shards, {0: 10.0, 3: 15.0})

    def test_unrestricted_key_uses_primary(self):
        d = {"subaccount_balances": [{"balance": "500", "exchange_index": 0, "subaccount_number": 0},
                                     {"balance": "25", "exchange_index": 0, "subaccount_number": 2}]}
        with mock.patch.object(self.b, "_req", return_value=d), mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KALSHI_SUBACCOUNT", None)
            self.assertAlmostEqual(self.b.balance(), 500.0)

    def test_429_is_raised_not_swallowed(self):
        with mock.patch.object(self.b, "_req", side_effect=RuntimeError("HTTP 429")):
            with self.assertRaises(RuntimeError):
                self.b.balance()


class AckTests(Base):
    def test_ack_ends_the_halt_streak(self):
        cfg = trader.Config(min_profit=0.01, min_roi=0.0)

        def move(cs):
            for x in cs:
                if x.venue == "Kalshi":
                    x.book = Book(asks=[(0.60, 500)])
                elif x.venue == "Polymarket":
                    x.book = Book(asks=[(0.50, 100)], bids=[(0.49, 500)])
        with mock.patch("arbscan.scanner.refresh", side_effect=move):
            for _ in range(2):
                o = opp([(0.45, 100)], [(0.50, 100)], b_bids=[(0.49, 500)])
                self.assertEqual(trader.execute(self.db, o, self.paper, cfg, budget=1000, **QUIET)["outcome"], "exposed")
        self.assertEqual(trader.recent_failures(self.db, "dry", 2), 2)
        self.assertEqual(trader.ack_failures(self.db, "dry"), 2)
        self.assertEqual(trader.recent_failures(self.db, "dry", 2), 0)
        rows = self.db.execute("SELECT status FROM trades").fetchall()
        self.assertEqual({r["status"] for r in rows}, {"open"})      # held positions untouched


if __name__ == "__main__":
    unittest.main()
