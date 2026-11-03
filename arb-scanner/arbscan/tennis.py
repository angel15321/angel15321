"""Tennis match winners on both venues: ATP, WTA and ATP Challenger.

Kalshi lists one market per player (KXATPMATCH-26NOV02SMISHI-SMI = "Smith wins");
Polymarket US lists one market per match whose long side is one player. Both
venues are paired here in one pass, because a match is only known to be the
same match once both sides line up:

    same date (Polymarket slug date == Kalshi event date) and
    both players' 3-letter codes (Polymarket team.abbreviation[3:6] == Kalshi
    market suffix) inside exactly one Kalshi event, and
    the Polymarket long player's last name inside that Kalshi market's player.

A match lives on the margin axis like a game: "home wins" pays when margin > 0,
"away wins" when margin < 0 (home/away = Polymarket's ordering). Tennis has no
draw; both sides are marked tie_half so the impossible 0 outcome pays 0.5 + 0.5
and never makes a winner pair look uncovered. A retirement settles both venues
to the player who advances.

Polymarket US has no ITF league, so Kalshi's KXITFMATCH has nothing to pair with.
"""
from __future__ import annotations

import datetime as dt
import time

from . import http, kalshi, polymarket
from .fees import kalshi_model, poly_model
from .model import Contract, Game

SERIES = ("KXATPMATCH", "KXWTAMATCH", "KXATPCHALLENGERMATCH")
LEAGUES = ("atp", "wta")
WINNER = "tennis_match_winner"
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
PAIR_TTL = 60            # seconds to reuse the match pairing (books are re-read every scan)
EVENT_TTL = 6 * 3600     # seconds to cache each match's market list: the winner market never changes, and a
                         # full refetch of ~50 matches takes ~30 s, which would stall every scan behind it
LOOKBACK = dt.timedelta(hours=12)   # a scheduled slot can run hours late; the live flag decides in-play
_pairs: dict = {"at": 0.0, "key": None, "matches": []}
_events: dict[str, tuple[float, dict]] = {}
_fees: dict[str, object] = {}


def _kalshi_fee(series: str, direct_member: bool):
    if series not in _fees:
        try:
            info = http.get(f"{kalshi.API}/series/{series}")["series"]
            _fees[series] = kalshi_model(info.get("fee_type"), info.get("fee_multiplier"), direct_member)
        except Exception:
            return kalshi_model(None, None, direct_member)
    return _fees[series]


def _pm_events(league: str) -> list[dict]:
    out, offset = [], 0
    while True:
        d = http.get(f"{polymarket.GATEWAY}/v2/leagues/{league}/events", {"limit": 100, "offset": offset})
        page = d.get("events") or []
        out += page
        if len(page) < 100:
            return out
        offset += 100


def _pm_event(slug: str) -> dict:
    hit = _events.get(slug)
    if hit and time.time() - hit[0] < EVENT_TTL:
        return hit[1]
    ev = http.get(f"{polymarket.GATEWAY}/v1/events/slug/{slug}").get("event") or {}
    _events[slug] = (time.time(), ev)
    return ev


def kalshi_date(slug: str) -> str | None:
    """'atp-colsmi-brashi-2026-11-02' -> '26NOV02' (the Kalshi event-ticker date)."""
    p = slug.split("-")
    try:
        return f"{p[-3][2:]}{MON[int(p[-2]) - 1]}{p[-1]}"
    except (IndexError, ValueError):
        return None


def match(event: dict, winner: dict, kalshi_events: dict[str, dict[str, dict]]) -> dict | None:
    """One Polymarket match + its winner market -> the Kalshi markets for each side, or None."""
    sides = {bool(s.get("long")): s for s in winner.get("marketSides") or []}
    if set(sides) != {True, False}:
        return None
    code = {k: ((s.get("team") or {}).get("abbreviation") or "")[3:6].upper() for k, s in sides.items()}
    order = {k: (s.get("team") or {}).get("ordering") for k, s in sides.items()}
    kd = kalshi_date(event.get("slug", ""))
    if not kd or code[True] == code[False] or not all(code.values()) or set(order.values()) != {"home", "away"}:
        return None
    hits = [ev for ev, ms in kalshi_events.items() if kd in ev and code[True] in ms and code[False] in ms]
    if len(hits) != 1:
        return None
    ms = kalshi_events[hits[0]]
    k_long, k_short = ms[code[True]], ms[code[False]]
    last = ((sides[True].get("description") or "").split() or [""])[-1].lower()
    if not last or last not in (k_long.get("yes_sub_title") or "").lower():
        return None
    name = {k: ((s.get("description") or "").split() or ["?"])[-1] for k, s in sides.items()}
    home = True if order[True] == "home" else False
    return dict(event=event["slug"], slug=winner["slug"], winner=winner, live=event.get("live"),
                start=event.get("startTime"), kalshi_event=hits[0], series=hits[0].split("-")[0],
                home=name[home], away=name[not home],
                k_home=(k_long if home else k_short)["ticker"], k_away=(k_short if home else k_long)["ticker"],
                long_is_home=home)


def pairs(now: dt.datetime, horizon: dt.datetime) -> list[dict]:
    """Matched matches, reused for PAIR_TTL seconds (the live flag refreshes with them)."""
    key = (horizon - now).total_seconds() // 600
    if _pairs["key"] == key and time.time() - _pairs["at"] < PAIR_TTL:
        return _pairs["matches"]
    kev: dict[str, dict[str, dict]] = {}
    for s in SERIES:
        for m in kalshi._markets(s):
            kev.setdefault(m["event_ticker"], {})[m["ticker"].rsplit("-", 1)[1]] = m
    out = []
    for lg in LEAGUES:
        for e in _pm_events(lg):
            if e.get("ended") or e.get("closed"):
                continue
            st = dt.datetime.fromisoformat(e["startTime"].replace("Z", "+00:00")) if e.get("startTime") else None
            if not e.get("live") and (st is None or st > horizon or st < now - LOOKBACK):
                continue
            ev = _pm_event(e["slug"])
            w = next((m for m in ev.get("markets") or [] if m.get("sportsMarketType") == WINNER
                      and not m.get("closed") and m.get("active", True)), None)
            if w:
                hit = match(e, w, kev)
                if hit:
                    out.append(hit)
    _pairs.update(at=time.time(), key=key, matches=out)
    return out


def build(m: dict, kalshi_fee, poly_fee_override: float | None = None) -> Game:
    """One matched match -> Game with its 6 contracts (no books)."""
    start = dt.datetime.fromisoformat(m["start"].replace("Z", "+00:00")) if m.get("start") else None
    g = Game(m["away"], m["home"], start, sport="tennis", uid=m["event"], live=m.get("live"))
    g.links["Polymarket"] = f"https://polymarket.us/event/{m['event']}"
    g.links["Kalshi"] = f"https://kalshi.com/markets/{m['series'].lower()}/{m['kalshi_event'].lower()}"
    wins = {"gt": f"{g.home} wins", "lt": f"{g.away} wins"}
    k_common = dict(venue="Kalshi", game=g.key, axis="margin", line=0.0, kind="ml", tie_half=True,
                    fee=kalshi_fee, url=g.links["Kalshi"])
    for ticker, player, yes_side in ((m["k_home"], g.home, "gt"), (m["k_away"], g.away, "lt")):
        no_side = "lt" if yes_side == "gt" else "gt"
        g.contracts.append(Contract(side=yes_side, label=wins[yes_side], how=f"YES {player}",
                                    ref={"ticker": ticker, "side": "yes"}, **k_common))
        g.contracts.append(Contract(side=no_side, label=wins[no_side], how=f"NO {player}",
                                    ref={"ticker": ticker, "side": "no"}, **k_common))
    w = m["winner"]
    long_side = "gt" if m["long_is_home"] else "lt"
    fee = poly_model(w, poly_fee_override)
    for side, s in ((long_side, "long"), ("lt" if long_side == "gt" else "gt", "short")):
        g.contracts.append(Contract(
            venue="Polymarket", game=g.key, axis="margin", side=side, line=0.0, kind="ml", tie_half=True,
            label=wins[side], how=f"{'BUY_LONG' if s == 'long' else 'BUY_SHORT'} on \"{g.title}\"", fee=fee,
            url=g.links["Polymarket"], min_size=polymarket._f(w.get("minimumTradeQty")) or 1.0,
            tick=polymarket._f(w.get("orderPriceMinTickSize")) or 0.01,
            ref={"ticker": w["slug"], "slug": w["slug"], "side": s, "market_id": w.get("id"),
                 "event": m["event"], "live": bool(m.get("live"))}))
    return g


def fetch(horizon: dt.datetime, poly_fee: float | None = None, kalshi_direct: bool = False) -> dict[str, Game]:
    """Both venues' tennis contracts, already paired, with books."""
    now = dt.datetime.now(dt.timezone.utc)
    games = {}
    for m in pairs(now, horizon):
        g = build(m, _kalshi_fee(m["series"], kalshi_direct), poly_fee)
        games[g.key] = g
    contracts = [c for g in games.values() for c in g.contracts]
    kb = kalshi.fetch_books(sorted({c.ref["ticker"] for c in contracts if c.venue == "Kalshi"}))
    pb = polymarket.fetch_books(sorted({c.ref["slug"] for c in contracts if c.venue == "Polymarket"}))
    for c in contracts:
        if c.venue == "Kalshi" and c.ref["ticker"] in kb:
            c.book = kb[c.ref["ticker"]][0 if c.ref["side"] == "yes" else 1]
        elif c.venue == "Polymarket" and c.ref["slug"] in pb:
            c.book = pb[c.ref["slug"]][0 if c.ref["side"] == "long" else 1]
    return games
