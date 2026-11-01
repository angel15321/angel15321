"""Polymarket adapter: NFL moneyline, spread and total markets + CLOB books."""
from __future__ import annotations

import datetime as dt
import json
import time

from . import http
from .fees import poly_model
from .model import Book, Contract, Game, margin_contract_label, total_label
from .teams import from_code, from_name

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
NFL_SERIES = "12185"
VENUE = "Polymarket"
KINDS = {"moneyline": "ml", "spreads": "spread", "totals": "total"}


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fetch_books(token_ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(token_ids), 400):
        for b in http.post(f"{CLOB}/books", [{"token_id": t} for t in token_ids[i:i + 400]]):
            out[b["asset_id"]] = b
    return out


def _books_for(raw: dict) -> tuple[Book, Book, float, float]:
    """Outcome-0 book -> (book0, book1, min_size, tick). The two outcome
    books mirror each other: an outcome-0 bid at p is an outcome-1 ask at 1-p."""
    now = time.time()
    asks = sorted((float(x["price"]), float(x["size"])) for x in raw.get("asks", []))
    bids = sorted(((float(x["price"]), float(x["size"])) for x in raw.get("bids", [])), reverse=True)
    b0 = Book(asks=asks, bids=bids, fetched_at=now)
    b1 = Book(asks=[(round(1 - p, 4), s) for p, s in bids], bids=[(round(1 - p, 4), s) for p, s in asks],
              fetched_at=now)
    return b0, b1, _f(raw.get("min_order_size")) or 5.0, _f(raw.get("tick_size")) or 0.01


def fetch(horizon: dt.datetime, fee_override: float | None = None) -> dict[str, Game]:
    events = http.get(f"{GAMMA}/events", {"series_id": NFL_SERIES, "closed": "false", "limit": 300})
    now = dt.datetime.now(dt.timezone.utc)
    games: dict[str, Game] = {}
    pending: list[tuple[Contract, int, str]] = []   # (contract, outcome index, token0)

    for e in events:
        parts = e.get("slug", "").split("-")       # nfl-kc-den-2026-11-01
        if len(parts) < 4 or parts[0] != "nfl":
            continue
        away, home = from_code(parts[1]), from_code(parts[2])
        if not away or not home:
            continue
        start = dt.datetime.fromisoformat(e["startTime"].replace("Z", "+00:00")) if e.get("startTime") else None
        if start and (start > horizon or start < now - dt.timedelta(hours=5)):
            continue
        g = games.setdefault(f"{away}@{home}", Game(away, home, start))
        g.links[VENUE] = f"https://polymarket.com/event/{e['slug']}"

        for m in e.get("markets", []):
            kind = KINDS.get(m.get("sportsMarketType"))
            if not kind or m.get("closed") or not m.get("acceptingOrders", True):
                continue
            outcomes = json.loads(m.get("outcomes") or "[]")
            tokens = json.loads(m.get("clobTokenIds") or "[]")
            if len(outcomes) != 2 or len(tokens) != 2:
                continue
            fee = poly_model(m, fee_override)
            common = dict(venue=VENUE, game=g.key, fee=fee, url=g.links[VENUE],
                          volume_24h=_f(m.get("volume24hr")))
            specs: list[tuple[str, float, str, bool]] = []   # per outcome: side, line, kind, tie_half
            if kind == "ml":
                for o in outcomes:
                    t = from_name(o)
                    if t not in (away, home):
                        break
                    specs.append(("gt" if t == home else "lt", 0.0, "ml", True))
            elif kind == "spread":
                line = _f(m.get("line"))
                t = from_name(outcomes[0])
                if line is None or line == int(line) or t not in (away, home):
                    continue
                # outcome 0: team t wins by more than -line
                need = -line
                s0, l0 = ("gt", need) if t == home else ("lt", -need)
                specs = [(s0, l0, "spread", False), ("lt" if s0 == "gt" else "gt", l0, "spread", False)]
            else:
                line = _f(m.get("line"))
                if line is None or line == int(line) or [o.lower() for o in outcomes] != ["over", "under"]:
                    continue
                specs = [("gt", line, "total", False), ("lt", line, "total", False)]
            if len(specs) != 2:
                continue
            for idx, (side, line, k, tie) in enumerate(specs):
                axis = "total" if k == "total" else "margin"
                label = total_label(side, line) if axis == "total" else margin_contract_label(g, side, line, k)
                c = Contract(axis=axis, side=side, line=line, kind=k, tie_half=tie, label=label,
                             how=f"buy '{outcomes[idx]}' on \"{m.get('question')}\"",
                             ref={"token_id": tokens[idx], "token0": tokens[0], "outcome": idx,
                                  "market_id": m.get("id"), "seconds_delay": m.get("secondsDelay") or 0},
                             **common)
                pending.append((c, idx, tokens[0]))

    raw = fetch_books(sorted({t0 for _, _, t0 in pending}))
    for c, idx, t0 in pending:
        if t0 in raw:
            b0, b1, min_size, tick = _books_for(raw[t0])
            c.book = b0 if idx == 0 else b1
            c.min_size, c.tick = min_size, tick
        games[c.game].contracts.append(c)
    return games


def settlement(market_id: str, outcome: int) -> float | None:
    """Payout per share for `outcome` once resolved, else None."""
    m = http.get(f"{GAMMA}/markets/{market_id}")
    if not m.get("closed"):
        return None
    prices = [_f(p) for p in json.loads(m.get("outcomePrices") or "[]")]
    # Only trust final prices (1/0, or 0.5/0.5 for a tie or cancellation).
    if len(prices) != 2 or any(p not in (0.0, 0.5, 1.0) for p in prices):
        return None
    return prices[outcome]


def refresh(contracts: list[Contract]) -> None:
    raw = fetch_books(sorted({c.ref["token0"] for c in contracts}))
    for c in contracts:
        if c.ref["token0"] in raw:
            b0, b1, _, _ = _books_for(raw[c.ref["token0"]])
            c.book = b0 if c.ref["outcome"] == 0 else b1
