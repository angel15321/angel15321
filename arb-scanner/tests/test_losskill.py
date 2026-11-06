"""Offline tests for the operator loss kill (11-06): one trade losing > --kill-trade-loss, or a UTC day's realized
P&L below -(--kill-day-loss), halts the bot; `arbscan ack` clears it.

Run:  python3 -m unittest discover -s tests
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbscan import trader  # noqa: E402


def row(db, pnl, status="settled", outcome="exposed", at=None, settled=True, mode="live"):
    at = time.time() if at is None else at
    db.execute("INSERT INTO trades (mode, detected_at, done_at, game, outcome, status, pnl, settled_at) "
               "VALUES (?,?,?,?,?,?,?,?)", (mode, at, at, "A @ B", outcome, status, pnl, at if settled else None))
    db.commit()


class LossKillTests(unittest.TestCase):
    def setUp(self):
        self.db = trader.connect(":memory:")
        self.cfg = trader.Config(kill_trade_loss=15, kill_day_loss=25)

    def test_off_by_default(self):
        row(self.db, -500)
        self.assertIsNone(trader.loss_kill(self.db, "live", trader.Config()))

    def test_one_trade_over_the_limit_trips(self):
        row(self.db, -14.99)
        self.assertIsNone(trader.loss_kill(self.db, "live", self.cfg))
        row(self.db, -15.01)
        why = trader.loss_kill(self.db, "live", self.cfg)
        self.assertIn("lost $15.01", why)

    def test_open_rows_do_not_count(self):
        row(self.db, -40, status="open")
        self.assertIsNone(trader.loss_kill(self.db, "live", self.cfg))

    def test_day_sum_trips_and_wins_offset(self):
        for _ in range(3):
            row(self.db, -9)                      # -27 on its own, each under the per-trade limit
        row(self.db, +5)                          # net -22: still above -25
        self.assertIsNone(trader.loss_kill(self.db, "live", self.cfg))
        row(self.db, -4)                          # net -26
        self.assertIn("-26.00", trader.loss_kill(self.db, "live", self.cfg))

    def test_yesterday_does_not_count_for_the_day(self):
        old = trader._today_start() - 3600
        for _ in range(4):
            row(self.db, -9, at=old)
        self.assertIsNone(trader.loss_kill(self.db, "live", self.cfg))

    def test_closed_row_without_settled_at_counts_today(self):
        for _ in range(3):
            row(self.db, -9, status="closed", outcome="unwound", settled=False)
        self.assertIsNotNone(trader.loss_kill(self.db, "live", self.cfg))

    def test_dry_rows_never_trip_live(self):
        row(self.db, -100, mode="dry")
        self.assertIsNone(trader.loss_kill(self.db, "live", self.cfg))

    def test_ack_clears_both_rules(self):
        row(self.db, -20, outcome="hedged")       # a loss on a row that was not a failed hedge
        for _ in range(3):
            row(self.db, -9)
        self.assertIsNotNone(trader.loss_kill(self.db, "live", self.cfg))
        self.assertEqual(trader.ack_failures(self.db, "live"), 4)
        self.assertIsNone(trader.loss_kill(self.db, "live", self.cfg))
        row(self.db, -16)                          # a NEW loss after the ack trips again
        self.assertIsNotNone(trader.loss_kill(self.db, "live", self.cfg))


if __name__ == "__main__":
    unittest.main()
