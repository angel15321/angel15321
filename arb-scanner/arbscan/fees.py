"""Exact taker-fee models for Kalshi and Polymarket.

Kalshi (docs.kalshi.com, series `fee_type` = quadratic / quadratic_with_maker_fees)
    taker fee = 0.07 x fee_multiplier x C x P x (1 - P)
    maker fee = 0.0175 x fee_multiplier x C x P x (1 - P)   (only on *_with_maker_fees)
    Fees are computed to $0.000001 and the order's balance change is rounded
    to the account's precision: $0.01 for regular (non-direct) members, with a
    per-order accumulator so the total converges to a single-fill equivalent.
    Net effect for a normal account: the order's total outlay rounds UP to the
    next cent.

Polymarket (docs.polymarket.com/trading/fees)
    taker fee = C x feeRate x p x (1 - p)        makers pay nothing
    feeRate comes from each market's `feeSchedule` (NFL markets currently 0.03;
    the docs' generic sports rate is 0.05). Fees round to 5 decimals.

Both fees peak at p = 0.50 and fall to ~0 at the extremes, so an arb whose
legs are priced near 50c needs the biggest raw edge to survive fees.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

KALSHI_TAKER = 0.07
KALSHI_MAKER = 0.0175
POLY_DEFAULT_SPORTS_RATE = 0.05


def _ceil(x: float, step: float) -> float:
    return math.ceil(round(x / step, 9)) * step


@dataclass(frozen=True)
class FeeModel:
    venue: str
    rate: float              # coefficient on C*p*(1-p)
    exponent: float = 1.0
    round_outlay_to: float = 0.0   # Kalshi: 0.01 (cent) for regular accounts
    round_fee_to: float = 0.0      # Polymarket: 0.00001

    def per_contract(self, p: float) -> float:
        """Marginal fee per contract at price p (before order-level rounding)."""
        return self.rate * (p * (1 - p)) ** self.exponent

    def fill_fee(self, p: float, qty: float) -> float:
        fee = qty * self.per_contract(p)
        if self.round_fee_to:
            fee = round(fee / self.round_fee_to) * self.round_fee_to
        return fee

    def order_total(self, fills: list[tuple[float, float]]) -> tuple[float, float]:
        """(notional, fee) for an order filled at [(price, qty), ...]."""
        notional = sum(p * q for p, q in fills)
        fee = sum(self.fill_fee(p, q) for p, q in fills)
        if self.round_outlay_to:
            total = _ceil(notional + fee, self.round_outlay_to)
            fee = total - notional
        return notional, fee

    def describe(self) -> str:
        r = f"{self.rate:g} x C x p x (1-p)"
        if self.exponent != 1:
            r = f"{self.rate:g} x C x (p(1-p))^{self.exponent:g}"
        if self.round_outlay_to:
            r += f"; order outlay rounded up to ${self.round_outlay_to:g}"
        if self.round_fee_to:
            r += f"; fee rounded to ${self.round_fee_to:g}"
        return r


def kalshi_model(fee_type: str | None, multiplier: float | None,
                 direct_member: bool = False) -> FeeModel:
    mult = multiplier if multiplier is not None else 1.0
    return FeeModel("Kalshi", KALSHI_TAKER * mult,
                    round_outlay_to=0.0001 if direct_member else 0.01)


def poly_model(market: dict, override: float | None = None) -> FeeModel:
    if override is not None:
        return FeeModel("Polymarket", override, round_fee_to=0.00001)
    if not market.get("feesEnabled"):
        return FeeModel("Polymarket", 0.0)
    sched = market.get("feeSchedule") or {}
    rate = sched.get("rate", POLY_DEFAULT_SPORTS_RATE)
    expo = sched.get("exponent", 1) or 1
    return FeeModel("Polymarket", float(rate), float(expo), round_fee_to=0.00001)


def fee_table(model: FeeModel, prices=(0.05, 0.10, 0.20, 0.30, 0.40, 0.50,
                                       0.60, 0.70, 0.80, 0.90, 0.95), qty: int = 100):
    """Rows of (price, fee for `qty` contracts, fee as % of notional)."""
    rows = []
    for p in prices:
        _, fee = model.order_total([(p, qty)])
        rows.append((p, fee, fee / (p * qty)))
    return rows
