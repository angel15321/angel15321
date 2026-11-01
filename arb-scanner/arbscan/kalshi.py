"""Kalshi adapter: NFL moneyline, spread and total markets + order books."""
from __future__ import annotations

import datetime as dt
import re
import time

from . import http
from .fees import FeeModel, kalshi_model
from .model import Book, Contract, Game, margin_contract_label, total_label
from .teams import from_code

API = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = {"KXNFLGAME": "ml", "KXNFLSPREAD": "spread", "KXNFLTOTAL": "total"}
VENUE = "Kalshi"


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def split_teams(code: str) -> tuple[str, str] | None:
    """'KCDEN' -> ('KC', 'DEN'): first team is away, second is home."""
    for i in range(2, len(code) - 1):
        a, h = from_code(code[:i]), from_code(code[i:])
        if a and h:
            return a, h
    return None


def fee_models(direct_member: bool = False) -> dict[str, FeeModel]:
    out = {}
    for s in SERIES:
        try:
            info = http.get(f"{API}/series/{s}")["series"]
            out[s] = kalshi_model(info.get("fee_type"), info.get("fee_multiplier"), direct_member)
        except Exception:
            out[s] = kalshi_model(None, None, direct_member)
    return out


def _markets(series: str) -> list[dict]:
    out, cursor = [], ""
    while True:
        params = {"series_ticker": series, "status": "open", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        data = http.get(f"{API}/markets", params)
        out += data.get("markets", [])
        cursor = data.get("cursor") or ""
        if not cursor:
            return out


def fetch_books(tickers: list[str]) -> dict[str, tuple[Book, Book]]:
    """ticker -> (YES book, NO book). Kalshi returns bids only; a YES ask at
    p is a NO bid at 1-p and vice versa."""
    out: dict[str, tuple[Book, Book]] = {}
    for i in range(0, len(tickers), 100):
        chunk = tickers[i:i + 100]
        data = http.get(f"{API}/markets/orderbooks", {"tickers": chunk})
        now = time.time()
        for ob in data.get("orderbooks", []):
            o = ob.get("orderbook_fp") or {}
            yes_bids = sorted(((float(p), float(s)) for p, s in o.get("yes_dollars") or []), reverse=True)
            no_bids = sorted(((float(p), float(s)) for p, s in o.get("no_dollars") or []), reverse=True)
            yes = Book(asks=[(round(1 - p, 4), s) for p, s in no_bids], bids=yes_bids, fetched_at=now)
            no = Book(asks=[(round(1 - p, 4), s) for p, s in yes_bids], bids=no_bids, fetched_at=now)
            out[ob["ticker"]] = (yes, no)
    return out


def fetch(horizon: dt.datetime, direct_member: bool = False) -> dict[str, Game]:
    fees = fee_models(direct_member)
    games: dict[str, Game] = {}
    contracts: list[Contract] = []

    for series, kind in SERIES.items():
        for m in _markets(series):
            ev = m["event_ticker"]                       # KXNFLSPREAD-26NOV01KCDEN
            tail = ev.rsplit("-", 1)[1]
            teams = split_teams(tail[7:])
            if not teams:
                continue
            away, home = teams
            occ = m.get("occurrence_datetime") or m.get("expected_expiration_time")
            start = (dt.datetime.fromisoformat(occ.replace("Z", "+00:00")) - dt.timedelta(hours=3)) if occ else None
            if start and start > horizon:
                continue
            g = games.setdefault(f"{away}@{home}", Game(away, home, start))
            game_slug = f"KXNFLGAME-{tail}".lower()
            g.links.setdefault(VENUE, f"https://kalshi.com/markets/kxnflgame/{game_slug}")

            ticker = m["ticker"]
            suffix = ticker.rsplit("-", 1)[1]
            common = dict(venue=VENUE, game=g.key, fee=fees[series],
                          url=f"https://kalshi.com/markets/{series.lower()}/{ev.lower()}",
                          volume_24h=_f(m.get("volume_24h_fp")), open_interest=_f(m.get("open_interest_fp")))

            if kind == "ml":
                team = from_code(suffix)
                if team not in (away, home):
                    continue
                yes_side = "gt" if team == home else "lt"
                no_side = "lt" if yes_side == "gt" else "gt"
                other = away if team == home else home
                specs = [(yes_side, f"YES {team}", "yes"), (no_side, f"NO {team} (= {other} wins)", "no")]
                for side, how, s in specs:
                    contracts.append(Contract(axis="margin", side=side, line=0.0, kind="ml", tie_half=True,
                                              label=margin_contract_label(g, side, 0.0, "ml"), how=how,
                                              ref={"ticker": ticker, "side": s}, **common))
            elif kind == "spread":
                team = from_code(re.sub(r"\d+$", "", suffix))
                x = _f(m.get("floor_strike"))
                if team not in (away, home) or x is None or x == int(x):
                    continue
                # YES = team wins by more than x
                yes_side, yes_line = ("gt", x) if team == home else ("lt", -x)
                no_side = "lt" if yes_side == "gt" else "gt"
                for side, s in ((yes_side, "yes"), (no_side, "no")):
                    lbl = margin_contract_label(g, side, yes_line, "spread")
                    how = f"{s.upper()} '{team} wins by over {x:g}'"
                    contracts.append(Contract(axis="margin", side=side, line=yes_line, kind="spread",
                                              label=lbl, how=how, ref={"ticker": ticker, "side": s}, **common))
            else:
                line = _f(m.get("floor_strike"))
                if line is None or line == int(line):
                    continue
                for side, s in (("gt", "yes"), ("lt", "no")):
                    contracts.append(Contract(axis="total", side=side, line=line, kind="total",
                                              label=total_label(side, line), how=f"{s.upper()} 'over {line:g}'",
                                              ref={"ticker": ticker, "side": s}, **common))

    books = fetch_books(sorted({c.ref["ticker"] for c in contracts}))
    for c in contracts:
        pair = books.get(c.ref["ticker"])
        if pair:
            c.book = pair[0] if c.ref["side"] == "yes" else pair[1]
        games[c.game].contracts.append(c)
    return games


def settlement(ticker: str) -> float | None:
    """YES payout per contract once settled, else None."""
    m = http.get(f"{API}/markets/{ticker}")["market"]
    if m.get("status") not in ("settled", "finalized", "determined"):
        return None
    v = _f(m.get("settlement_value_dollars"))
    if v is not None:
        return v
    r = (m.get("result") or "").lower()
    return 1.0 if r == "yes" else 0.0 if r == "no" else None


def refresh(contracts: list[Contract]) -> None:
    books = fetch_books(sorted({c.ref["ticker"] for c in contracts}))
    for c in contracts:
        if c.ref["ticker"] in books:
            yes, no = books[c.ref["ticker"]]
            c.book = yes if c.ref["side"] == "yes" else no
