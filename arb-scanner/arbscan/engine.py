"""Arb detection and depth-aware sizing.

A pair (A, B) is an arb when, for every possible final score, A and B
together pay at least `guaranteed` per contract pair, and buying one of each
costs less than that. We then walk both order books level by level to find
how many pairs can be bought while each additional pair is still profitable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .model import Contract, Game


@dataclass
class Sizing:
    qty: float = 0.0
    fills_a: list[tuple[float, float]] = field(default_factory=list)
    fills_b: list[tuple[float, float]] = field(default_factory=list)
    notional: float = 0.0
    fees: float = 0.0
    payout: float = 0.0

    @property
    def outlay(self) -> float:
        return self.notional + self.fees

    @property
    def profit(self) -> float:
        return self.payout - self.outlay

    @property
    def roi(self) -> float:
        return self.profit / self.outlay if self.outlay else 0.0

    def avg_price(self, leg: str) -> float:
        fills = self.fills_a if leg == "a" else self.fills_b
        q = sum(s for _, s in fills)
        return sum(p * s for p, s in fills) / q if q else 0.0


@dataclass
class Opportunity:
    game: Game
    a: Contract                     # "gt" leg
    b: Contract                     # "lt" leg
    guaranteed: float               # min payout per pair across all outcomes
    middle: tuple[int, int] | None  # outcomes where both legs pay
    max: Sizing                     # largest profitable size
    curve: list[tuple[float, float, float]] = field(default_factory=list)  # (qty, outlay, profit)

    @property
    def unit_cost(self) -> float:
        return (self.a.unit_cost or 9) + (self.b.unit_cost or 9)

    @property
    def top_roi(self) -> float:
        return self.guaranteed / self.unit_cost - 1

    @property
    def key(self) -> str:
        return f"{self.a.id}|{self.b.id}"

    @property
    def cross_venue(self) -> bool:
        return self.a.venue != self.b.venue

    def at_outlay(self, dollars: float) -> Sizing | None:
        """Best result when spending at most `dollars` (None if not even 1 pair fits)."""
        s = size(self.a, self.b, self.guaranteed, max_outlay=dollars)
        return s if s.qty > 0 else None

    def middle_text(self) -> str:
        if not self.middle:
            return ""
        lo, hi = self.middle
        g = self.game
        if self.a.axis == "total":
            return f"both win if total is {lo}" if lo == hi else f"both win if total is {lo}–{hi}"
        def m(x):
            return "tie" if x == 0 else f"{g.home} by {x}" if x > 0 else f"{g.away} by {-x}"
        return f"both win if {m(lo)}" if lo == hi else f"both win if result is {m(lo)} … {m(hi)}"


def guaranteed_payout(a: Contract, b: Contract) -> tuple[float, tuple[int, int] | None]:
    """Minimum combined payout over every integer outcome, and the outcome
    window (if any) where both pay in full."""
    lo = math.floor(min(a.line, b.line)) - 2
    hi = math.ceil(max(a.line, b.line)) + 2
    worst = min(a.payout(x) + b.payout(x) for x in range(lo, hi + 1))
    both = [x for x in range(lo, hi + 1) if a.payout(x) == 1 and b.payout(x) == 1]
    return worst, ((both[0], both[-1]) if both else None)


def _walk(a: Contract, b: Contract, guaranteed: float, max_outlay: float | None,
          max_qty: float | None, min_edge: float):
    """Greedy level-by-level walk. Returns (qty, curve)."""
    asks_a, asks_b = list(a.book.asks), list(b.book.asks)
    ia = ib = 0
    ra = asks_a[0][1] if asks_a else 0
    rb = asks_b[0][1] if asks_b else 0
    qty = spent = profit = 0.0
    curve = []
    while ia < len(asks_a) and ib < len(asks_b):
        pa, pb = asks_a[ia][0], asks_b[ib][0]
        unit = pa + a.fee.per_contract(pa) + pb + b.fee.per_contract(pb)
        if unit >= guaranteed - min_edge:
            break
        dq = min(ra, rb)
        if max_outlay is not None:
            dq = min(dq, max(0.0, (max_outlay - spent) / unit))
        if max_qty is not None:
            dq = min(dq, max_qty - qty)
        if dq <= 1e-9:
            break
        qty += dq
        spent += dq * unit
        profit += dq * (guaranteed - unit)
        curve.append((qty, spent, profit))
        ra -= dq
        rb -= dq
        if ra <= 1e-9:
            ia += 1
            ra = asks_a[ia][1] if ia < len(asks_a) else 0
        if rb <= 1e-9:
            ib += 1
            rb = asks_b[ib][1] if ib < len(asks_b) else 0
    return qty, curve


def _fills(asks: list[tuple[float, float]], qty: float) -> list[tuple[float, float]]:
    out, left = [], qty
    for p, s in asks:
        if left <= 1e-9:
            break
        take = min(s, left)
        out.append((p, take))
        left -= take
    return out


def size(a: Contract, b: Contract, guaranteed: float, max_outlay: float | None = None,
         max_qty: float | None = None, min_edge: float = 0.0) -> Sizing:
    """Largest whole-contract size where every pair is profitable, with exact
    venue fees and rounding applied to the final orders."""
    raw, _ = _walk(a, b, guaranteed, max_outlay, max_qty, min_edge)
    qty = math.floor(raw + 1e-9)
    min_q = max(a.min_size, b.min_size)
    while qty >= min_q:
        fa, fb = _fills(a.book.asks, qty), _fills(b.book.asks, qty)
        na, fee_a = a.fee.order_total(fa)
        nb, fee_b = b.fee.order_total(fb)
        s = Sizing(qty, fa, fb, na + nb, fee_a + fee_b, qty * guaranteed)
        if (max_outlay is None or s.outlay <= max_outlay + 1e-9) and s.profit > 0:
            return s
        # Order-level rounding (Kalshi rounds outlay up to the cent) can tip a
        # tiny order negative; shrink and retry.
        qty = qty - 1 if qty < 50 else math.floor(qty * 0.9)
    return Sizing()


def find(games: list[Game], min_roi: float = 0.0, cross_venue_only: bool = False,
         near: float = 0.0) -> tuple[list[Opportunity], list[Opportunity]]:
    """Returns (arbs, closest_non_arbs). `near` widens what counts as a
    candidate for the 'closest' list (e.g. 0.02 = within 2% of breakeven)."""
    arbs: list[Opportunity] = []
    closest: list[Opportunity] = []
    for g in games:
        for axis in ("margin", "total"):
            gts = [c for c in g.contracts if c.axis == axis and c.side == "gt" and c.unit_cost is not None]
            lts = [c for c in g.contracts if c.axis == axis and c.side == "lt" and c.unit_cost is not None]
            for a in gts:
                for b in lts:
                    if b.line < a.line:
                        continue               # gap where both lose
                    if cross_venue_only and a.venue == b.venue:
                        continue
                    if a.ref.get("ticker") and a.ref.get("ticker") == b.ref.get("ticker"):
                        continue               # YES+NO (long+short) of one market
                    unit = a.unit_cost + b.unit_cost
                    if unit >= 1 + near:
                        continue
                    guaranteed, middle = guaranteed_payout(a, b)
                    if guaranteed <= 0:
                        continue
                    if guaranteed / unit - 1 < min_roi:
                        if guaranteed / unit - 1 > -max(near, 0.0001):
                            closest.append(Opportunity(g, a, b, guaranteed, middle, Sizing()))
                        continue
                    s = size(a, b, guaranteed)
                    if s.qty <= 0:
                        continue
                    _, curve = _walk(a, b, guaranteed, None, None, 0.0)
                    arbs.append(Opportunity(g, a, b, guaranteed, middle, s, curve))
    arbs.sort(key=lambda o: -o.max.profit)
    closest.sort(key=lambda o: -o.top_roi)
    return arbs, closest


def non_overlapping(opps: list[Opportunity]) -> list[Opportunity]:
    """Greedy pick by profit so each contract's liquidity is used once."""
    used: set[str] = set()
    out = []
    for o in opps:
        if o.a.id in used or o.b.id in used:
            continue
        used |= {o.a.id, o.b.id}
        out.append(o)
    return out
