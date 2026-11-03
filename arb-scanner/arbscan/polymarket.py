"""Polymarket US adapter: NFL moneyline + full-game spread, NBA moneyline + full-game spread + total, and order books.

Market lists come from the PUBLIC gateway (gateway.polymarket.us). Order books
come from the live markets websocket (pmws.BookFeed, needs the API key) when one
is attached with use_feed(); otherwise from the REST book endpoint, which is a
cached snapshot that can lag the exchange by 10-30 s.

Polymarket US lists ONE instrument per market (the YES / "long" side). Buying
NO is a short of that instrument, so each market yields two contracts here:

    long   pays when the market's outcome happens     (buy  = ORDER_INTENT_BUY_LONG)
    short  pays when it does not                      (buy  = ORDER_INTENT_BUY_SHORT)

A short ask at p is a long bid at 1-p, so one order book prices both.
NFL totals are not listed on Polymarket US (11-2026), so NFL maps moneylines and
full-game spreads; NBA also maps full-game totals. Half/quarter lines have no Kalshi match.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import time

from . import http
from .fees import poly_model
from .model import Book, Contract, Game, margin_contract_label, total_label
from .teams import from_code

GATEWAY = "https://gateway.polymarket.us"
VENUE = "Polymarket"
SPORT_KINDS = {
    "nfl": {"football_team_full_game_winner": "ml", "football_team_full_game_spread": "spread"},
    "nba": {"basketball_team_full_game_winner": "ml", "basketball_team_full_game_spread": "spread",
            "basketball_team_full_game_total": "total"},
}
KINDS = SPORT_KINDS["nfl"]
EVENT_TTL = 600          # seconds to cache each game's market list (lines rarely change)
_events: dict[str, tuple[float, list[dict]]] = {}


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _levels(rows: list[dict]) -> list[tuple[float, float]]:
    out = []
    for x in rows or []:
        p, q = _f((x.get("px") or {}).get("value")), _f(x.get("qty"))
        if p is not None and q:
            out.append((p, q))
    return out


def _events_for_league(league: str = "nfl") -> list[dict]:
    """Every game event of the league with its start time and live flag (markets fetched per event)."""
    out, offset = [], 0
    while True:
        d = http.get(f"{GATEWAY}/v2/leagues/{league}/events", {"limit": 100, "offset": offset})
        page = d.get("events") or []
        out += [e for e in page if str(e.get("slug", "")).startswith(f"{league}-")]
        if len(page) < 100:
            return out
        offset += 100


def _event_markets(slug: str, kinds: dict = KINDS) -> list[dict]:
    hit = _events.get(slug)
    if hit and time.time() - hit[0] < EVENT_TTL:
        return hit[1]
    ms = (http.get(f"{GATEWAY}/v1/events/slug/{slug}").get("event") or {}).get("markets") or []
    ms = [m for m in ms if m.get("sportsMarketType") in kinds]
    _events[slug] = (time.time(), ms)
    return ms


_feed = None             # pmws.BookFeed when live books are available (see use_feed)


def use_feed(feed) -> None:
    """Read books from a live websocket feed instead of the (cached, lagging) REST endpoints."""
    global _feed
    _feed = feed


def books_from(md: dict, now: float | None = None) -> tuple[Book, Book]:
    """marketData -> (long book, short book). A market not open for trading gets empty books."""
    now = time.time() if now is None else now
    if md.get("state") not in (None, "MARKET_STATE_OPEN"):
        return Book(fetched_at=now), Book(fetched_at=now)
    offers = sorted(_levels(md.get("offers")))
    bids = sorted(_levels(md.get("bids")), reverse=True)
    long = Book(asks=offers, bids=bids, fetched_at=now)
    short = Book(asks=[(round(1 - p, 4), s) for p, s in bids], bids=[(round(1 - p, 4), s) for p, s in offers],
                 fetched_at=now)
    return long, short


def fetch_book(slug: str) -> tuple[Book, Book]:
    """REST snapshot. Cached server-side and up to ~30 s old: fine for a scan, not for trading in-play."""
    return books_from(http.get(f"{GATEWAY}/v1/markets/{slug}/book").get("marketData") or {})


def _safe_book(slug: str):
    try:
        return fetch_book(slug)
    except Exception:
        return None


def fetch_books(slugs: list[str]) -> dict[str, tuple[Book, Book]]:
    if _feed is not None:
        # Live feed only: a market it has no book for yet is left out (no trade) rather
        # than priced from a stale REST snapshot.
        new = _feed.want(slugs)
        if new:
            _feed.wait_for(new, timeout=2.0)          # first books for newly subscribed markets
        out = {}
        for slug in slugs:
            hit = _feed.get_at(slug)
            if hit is not None:
                out[slug] = books_from(hit[1], now=hit[0])     # book age = time since the feed sent it
        return out
    out = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for slug, books in zip(slugs, ex.map(_safe_book, slugs)):
            if books:
                out[slug] = books
    return out


def _teams(event: dict, m: dict, sport: str = "nfl") -> tuple[str, str] | None:
    """(away, home). Market sides carry `ordering`; else the slug is <league>-away-home-date."""
    order = {}
    for s in m.get("marketSides") or []:
        t = s.get("team") or {}
        code = from_code(t.get("abbreviation") or "", sport)
        if code and t.get("ordering") in ("away", "home"):
            order[t["ordering"]] = code
    if "away" in order and "home" in order:
        return order["away"], order["home"]
    parts = event["slug"].split("-")
    a, h = from_code(parts[1], sport), from_code(parts[2], sport)
    return (a, h) if a and h else None


def parse_event(e: dict, markets: list[dict], fee_override: float | None = None,
                sport: str = "nfl") -> tuple[Game | None, list[Contract]]:
    """One event + its markets -> (Game, contracts without books)."""
    start = dt.datetime.fromisoformat(e["startTime"].replace("Z", "+00:00")) if e.get("startTime") else None
    kinds = SPORT_KINDS[sport]
    g, out = None, []
    for m in markets:
        kind = kinds.get(m.get("sportsMarketType"))
        if not kind or m.get("closed") or not m.get("active", True):
            continue
        teams = _teams(e, m, sport)
        long_side = next((s for s in m.get("marketSides") or [] if s.get("long")), None)
        if not teams or not long_side:
            continue
        away, home = teams
        if kind == "total":
            # sides are Over (long) / Under, no team
            ln = _f(m.get("line"))
            if ln is None or ln == int(ln) or (long_side.get("description") or "").lower() != "over":
                continue
            axis, s_long, line = "total", "gt", ln
        else:
            t = from_code((long_side.get("team") or {}).get("abbreviation") or "", sport)
            if t not in (away, home):
                continue
            axis = "margin"
            if kind == "ml":
                s_long, line = ("gt" if t == home else "lt"), 0.0
            else:
                ln = _f(m.get("line"))
                if ln is None or ln == int(ln):
                    continue
                # long = team t covers `ln`, i.e. wins by more than -ln
                need = -ln
                s_long, line = ("gt", need) if t == home else ("lt", -need)
        if g is None:
            g = Game(away, home, start, sport=sport)
            g.links[VENUE] = f"https://polymarket.us/event/{e['slug']}"
        s_short = "lt" if s_long == "gt" else "gt"
        fee = poly_model(m, fee_override)
        for side, s in ((s_long, "long"), (s_short, "short")):
            out.append(Contract(
                venue=VENUE, game=g.key, axis=axis, side=side, line=line, kind=kind, tie_half=kind == "ml",
                label=total_label(side, line) if kind == "total" else margin_contract_label(g, side, line, kind),
                how=f"{'BUY_LONG' if s == 'long' else 'BUY_SHORT'} on \"{m.get('question')}\"",
                fee=fee, url=g.links[VENUE], min_size=_f(m.get("minimumTradeQty")) or 1.0,
                tick=_f(m.get("orderPriceMinTickSize")) or 0.01,
                ref={"ticker": m["slug"], "slug": m["slug"], "side": s, "market_id": m.get("id"),
                     "event": e["slug"], "live": bool(e.get("live"))}))
    return g, out


def fetch(horizon: dt.datetime, fee_override: float | None = None, sport: str = "nfl") -> dict[str, Game]:
    now = dt.datetime.now(dt.timezone.utc)
    games: dict[str, Game] = {}
    contracts: list[Contract] = []
    for e in _events_for_league(sport):
        start = dt.datetime.fromisoformat(e["startTime"].replace("Z", "+00:00")) if e.get("startTime") else None
        if start and (start > horizon or start < now - dt.timedelta(hours=5)):
            continue
        g, cs = parse_event(e, _event_markets(e["slug"], SPORT_KINDS[sport]), fee_override, sport)
        if g:
            games.setdefault(g.key, g).links.update(g.links)
            contracts += cs
    books = fetch_books(sorted({c.ref["slug"] for c in contracts}))
    for c in contracts:
        pair = books.get(c.ref["slug"])
        if pair:
            c.book = pair[0] if c.ref["side"] == "long" else pair[1]
        games[c.game].contracts.append(c)
    return games


def settlement(slug: str, side: str) -> float | None:
    """Payout per contract for `side` ('long'/'short') once settled, else None."""
    try:
        d = http.get(f"{GATEWAY}/v1/markets/{slug}/settlement")
    except Exception:
        return None          # 404 until the market settles
    v = _f(d.get("settlement"))
    if v not in (0.0, 0.5, 1.0):
        return None
    return v if side == "long" else 1 - v


def refresh(contracts: list[Contract]) -> None:
    books = fetch_books(sorted({c.ref["slug"] for c in contracts}))
    for c in contracts:
        if c.ref["slug"] in books:
            long, short = books[c.ref["slug"]]
            c.book = long if c.ref["side"] == "long" else short
