"""Offline tests for the Telegram notifier: hedge outcomes are queued (never sent) by the trading thread, sent late
as one message, held 9 PM - 5 AM MST, and a maker fill -> Kalshi hedge ends in exactly one 'HEDGE FILLED' line.

Run:  python3 -m unittest discover -s tests
"""
import datetime as dt
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import time  # noqa: E402

from arbscan import maker, notify, trader  # noqa: E402
from arbscan.model import Book  # noqa: E402
from test_maker import QUIET, Base, pair  # noqa: E402


def mst(h, m=0, day=4):
    """A wall-clock time in MST on 2026-11-<day> as a unix timestamp."""
    return dt.datetime(2026, 11, day, h, m, tzinfo=notify.MST).timestamp()


class Sink:
    def __init__(self, ok=True):
        self.msgs, self.ok = [], ok

    def __call__(self, text):
        self.msgs.append(text)
        return self.ok


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.sink = Sink()
        notify.configure(post=self.sink)

    def tearDown(self):
        notify.configure()

    def push_at(self, ts, text, urgent=False):
        with mock.patch("time.time", return_value=ts):
            notify.push(text, urgent)

    def test_off_without_a_token(self):
        notify.configure()
        self.assertFalse(notify.enabled())
        notify.push("x")
        self.assertEqual(notify.drain(mst(12)), 0)
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            self.assertFalse(notify.start(log=lambda *_: None))

    def test_sent_only_after_the_queue_has_been_still(self):
        t = mst(12)
        self.push_at(t, "🛡 HEDGE FILLED #1 · A v B")
        self.push_at(t + 10, "🛡 HEDGE FILLED #2 · C v D")
        self.assertEqual(notify.drain(t + 25), 0)              # 15 s after the last one: still moving
        self.assertEqual(self.sink.msgs, [])
        self.assertEqual(notify.drain(t + 41), 2)
        self.assertEqual(self.sink.msgs, ["[ARB] 2 updates\n🛡 HEDGE FILLED #1 · A v B\n🛡 HEDGE FILLED #2 · C v D"])
        self.assertEqual(notify.drain(t + 80), 0)              # nothing is sent twice

    def test_one_line_is_one_short_message(self):
        self.push_at(mst(12), "🛡 HEDGE FILLED #1 · A v B")
        notify.drain(mst(12, 1))
        self.assertEqual(self.sink.msgs, ["[ARB] 🛡 HEDGE FILLED #1 · A v B"])

    def test_night_is_held_until_5am_but_a_halt_goes_out(self):
        self.push_at(mst(23), "🛡 HEDGE FILLED #1 · A v B")
        self.assertEqual(notify.drain(mst(23, 5)), 0)
        self.push_at(mst(2, day=5), "🔴 HALTED", urgent=True)
        self.assertEqual(notify.drain(mst(2, 0, day=5) + 1), 1)             # at once, no 30 s wait
        self.assertEqual(self.sink.msgs, ["[ARB] 🔴 HALTED"])
        self.assertEqual(notify.drain(mst(4, 59, day=5)), 0)
        self.assertEqual(notify.drain(mst(5, 0, day=5) + 1), 1)
        self.assertEqual(self.sink.msgs[-1], "🌙 [ARB] overnight · 1\n🛡 HEDGE FILLED #1 · A v B")
        self.assertTrue(notify.quiet(mst(21)))
        self.assertFalse(notify.quiet(mst(20, 59)))
        self.assertFalse(notify.quiet(mst(5)))

    def test_a_failed_send_is_retried_and_exit_flushes(self):
        self.sink.ok = False
        self.push_at(mst(12), "line")
        self.assertEqual(notify.drain(mst(12, 1)), 0)
        self.sink.ok = True
        self.assertEqual(notify.drain(mst(12, 2)), 1)
        self.push_at(mst(23), "held at night")
        with mock.patch("time.time", return_value=mst(23, 1)):
            notify.flush()                                      # the bot is exiting: nothing may be lost
        self.assertIn("held at night", self.sink.msgs[-1])

    def test_trade_text(self):
        row = {"id": 57, "mode": "live", "game": "Boulais v Giron", "outcome": "hedged", "locked_profit": 0.38,
               "cash_out": 7.62, "fills": [
                   {"venue": "Polymarket", "action": "buy", "label": "Boulais", "qty": 8.0, "avg_price": 0.54},
                   {"venue": "Kalshi", "action": "buy", "label": "Giron", "qty": 0.0, "avg_price": 0.0},
                   {"venue": "Kalshi", "action": "buy", "label": "Giron", "qty": 8.0, "avg_price": 0.41}]}
        self.assertEqual(notify.trade_text(row), "🛡 HEDGE FILLED #57 · Boulais v Giron · 8 Boulais @ 54¢ on Polymarket "
                                                 "+ 8 Giron @ 41¢ on Kalshi · locked +$0.38")
        row.update(outcome="exposed", locked_profit=None, cash_out=4.39, fills=row["fills"][:2])
        self.assertEqual(notify.trade_text(row), "⚠️ NOT HEDGED #57 · Boulais v Giron · 8 Boulais @ 54¢ on Polymarket "
                                                 "· $4.39 at risk, one side held")
        notify.trade(dict(row, outcome="missed"))               # nothing filled: not a message
        notify.trade(dict(row, mode="dry"))                     # paper rows never reach the phone
        self.assertEqual(len(notify._q), 0)


class MakerFillTests(Base):
    """The whole path: a resting Polymarket quote fills, the Kalshi hedge fills, the row is handed to notify.trade
    once -- and nothing is sent from the trading thread."""

    def setUp(self):
        super().setUp()
        self.sink = Sink()
        notify.configure(post=self.sink)

    def tearDown(self):
        notify.configure()
        super().tearDown()

    def test_maker_fill_and_hedge_hand_one_row_to_the_notifier(self):
        g, pm, k = pair()
        maker.step(self.db, self.cfg, self.paper, [g], full=True, **QUIET)
        sold_into = Book(asks=[(0.39, 50)], bids=[(0.38, 100)], fetched_at=time.time())
        with mock.patch.object(trader, "live_book", return_value=sold_into),                 mock.patch.object(notify, "trade", wraps=notify.trade) as tr:
            maker.poll(self.db, self.cfg, self.paper, force=True, **QUIET)
        self.assertEqual(tr.call_count, 1)
        row = tr.call_args[0][0]
        self.assertEqual((row["outcome"], row["mode"]), ("hedged", "dry"))
        self.assertEqual(len(notify._q), 0)                     # a paper row never reaches the phone
        notify.trade(dict(row, mode="live"))                    # the same row from a live run: ONE line, queued
        self.assertEqual(self.sink.msgs, [])                    # ... and not sent by the caller
        self.assertEqual(len(notify._q), 1)
        line = notify._q[0][1]
        self.assertTrue(line.startswith(f"🛡 HEDGE FILLED #{row['id']} · "), line)
        self.assertIn("20 Under 44.5 @ 39¢ on Polymarket + 20 Over 44.5 @ 55¢ on Kalshi · locked +$0.", line)

    def test_halt_is_urgent(self):
        self.cfg.live = False
        with mock.patch.object(trader, "recent_failures", return_value=2),                 mock.patch.object(trader, "settle"), mock.patch.object(notify, "push") as push:
            trader._loop(self.db, self.cfg, self.paper, 1.0, 1.0, False, lambda *_: None, {}, None, "live")
        self.assertEqual(push.call_count, 1)
        self.assertIn("HALTED", push.call_args[0][0])
        self.assertTrue(push.call_args[1]["urgent"])


if __name__ == "__main__":
    unittest.main()
