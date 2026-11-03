"""Venue-neutral representation of everything we can buy.

Every contract pays $1 depending on one integer outcome of the game:

* axis "margin": home score minus away score  (moneyline and spreads)
* axis "total":  home score plus away score   (over/under)

and a side: "gt" pays when outcome > line, "lt" when outcome < line.
Moneylines are margin contracts at line 0 that pay $0.50 on a tie (both
Kalshi and Polymarket settle NFL ties 50/50). Because every contract lives
on the same axis, a Kalshi spread can be paired with a Polymarket moneyline,
a 44.5 over with a 45.5 under, and so on.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .fees import FeeModel
from .teams import nickname

AXIS_RANGE = {"margin": range(-80, 81), "total": range(0, 151)}


@dataclass
class Book:
    """Ask side of one contract: [(price, size), ...] best (cheapest) first."""
    asks: list[tuple[float, float]] = field(default_factory=list)
    bids: list[tuple[float, float]] = field(default_factory=list)  # best first
    fetched_at: float = 0.0

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    def depth_within(self, cents: float) -> tuple[float, float]:
        """(contracts, $ notional) available within `cents` of the best ask."""
        if not self.asks:
            return 0.0, 0.0
        cap = self.asks[0][0] + cents / 100 + 1e-9
        q = sum(s for p, s in self.asks if p <= cap)
        n = sum(p * s for p, s in self.asks if p <= cap)
        return q, n


@dataclass
class Contract:
    venue: str               # "Kalshi" | "Polymarket"
    game: str                # Game.key
    axis: str                # "margin" | "total"
    side: str                # "gt" | "lt"
    line: float
    kind: str                # "ml" | "spread" | "total"
    label: str               # human: "KC -2.5", "Over 44.5", "DEN ML"
    how: str                 # how to buy it on the venue: "YES ...", "NO ...", "Broncos shares"
    fee: FeeModel
    ref: dict                # venue ids needed to re-fetch book / settle
    book: Book = field(default_factory=Book)
    tie_half: bool = False   # moneyline: pays 0.5 when outcome == line
    url: str = ""
    volume_24h: float | None = None
    open_interest: float | None = None
    min_size: float = 1.0
    tick: float = 0.01

    @property
    def id(self) -> str:
        return f"{self.venue}:{self.ref.get('ticker') or self.ref.get('token_id')}:{self.ref.get('side', '')}"

    def payout(self, x: int) -> float:
        if x == self.line:
            return 0.5 if self.tie_half else 0.0
        return 1.0 if (x > self.line) == (self.side == "gt") else 0.0

    @property
    def unit_cost(self) -> float | None:
        """Top-of-book cost per $1 payout including the marginal fee."""
        p = self.book.best_ask
        return None if p is None else p + self.fee.per_contract(p)


@dataclass
class Game:
    away: str
    home: str
    start: dt.datetime | None = None
    links: dict[str, str] = field(default_factory=dict)
    contracts: list[Contract] = field(default_factory=list)
    sport: str = "nfl"
    uid: str = ""             # unique key when names can repeat (tennis: the Polymarket event slug)
    live: bool | None = None  # the venue's own in-progress flag; None = judge by start time

    @property
    def key(self) -> str:
        if self.uid:
            return self.uid
        # NFL keeps its bare key; other leagues are prefixed (DEN@MIN can be an NFL and an NBA game the same night)
        return f"{self.away}@{self.home}" if self.sport == "nfl" else f"{self.sport}:{self.away}@{self.home}"

    @property
    def title(self) -> str:
        return f"{self.away} v {self.home}" if self.sport == "tennis" else f"{self.away} @ {self.home}"

    @property
    def long_title(self) -> str:
        if self.sport != "nfl":
            return self.title
        return f"{nickname(self.away)} @ {nickname(self.home)}"

    def started(self, now: dt.datetime) -> bool:
        """In progress (or over). Tennis start times are schedule slots, so the live flag wins when there is one."""
        if self.live is not None:
            return self.live
        return self.start is not None and self.start <= now


def spread_label(game: Game, team: str, cover_by: float) -> str:
    """team must win by more than `cover_by` (negative = may lose by < |cover_by|)."""
    sign = "-" if cover_by > 0 else "+"
    if cover_by == 0:
        return f"{team} ML"
    return f"{team} {sign}{abs(cover_by):g}"


def margin_contract_label(game: Game, side: str, line: float, kind: str) -> str:
    """Human label for a margin-axis contract (home margin `side` `line`)."""
    if kind == "ml":
        return f"{game.home if side == 'gt' else game.away} ML"
    # gt L  -> home wins by more than L  -> home -L
    # lt L  -> away "wins by more than -L" -> away +L  (i.e. away -(-L))
    if side == "gt":
        return spread_label(game, game.home, line)
    return spread_label(game, game.away, -line)


def total_label(side: str, line: float) -> str:
    return f"{'Over' if side == 'gt' else 'Under'} {line:g}"
