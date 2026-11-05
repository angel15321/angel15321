"""Winner markets paired by NAME: soccer (EPL, MLS, La Liga, Bundesliga, UCL), college football, college
basketball and UFC.

Both venues list one market per side ("Arsenal wins", "Jonny Parsons wins"; soccer also has a draw market,
not used here). A game is paired only when it is certainly the same game:

    the Polymarket event date, or the day before (Kalshi dates by the US day, the slug by UTC: UFC 11-07
    on Polymarket is 26NOV06 on Kalshi), is in the Kalshi event ticker, and
    BOTH sides' names map one-to-one onto the Kalshi event's two sides (a code match, equal name tokens,
    or one name inside the other with at least two shared words -- 'Missouri' alone does not match
    'Southeast Missouri'), and exactly one Kalshi event fits.

A pairing that passes is then price-checked once books are in: if the two venues' prices for the same side
are more than MAX_GAP apart, the game is dropped (a wrong pairing looks hedged and is not).

Axis encoding (margin = home score - away score, works with or without a draw):
    home wins  = margin > 0.5      home does not win  = margin < 0.5
    away wins  = margin < -0.5     away does not win  = margin > -0.5
"X wins" on one venue + "X does not win" on the other pays $1 on every result, draw included; so do the two
"does not win" sides together (a draw pays both). Kalshi YES = wins, NO = does not win; Polymarket long =
wins, short = does not win.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import time
import unicodedata

from . import http, kalshi, polymarket
from .fees import poly_model
from .model import Contract, Game
from .tennis import _kalshi_fee

SPORTS = {   # sport -> ({Polymarket league: Kalshi winner series}, Polymarket winner market types)
    "soccer": ({"epl": "KXEPLGAME", "mls": "KXMLSGAME", "lal": "KXLALIGAGAME", "bun": "KXBUNDESLIGAGAME",
                "ucl": "KXUCLGAME"}, {"soccer_team_full_time_winner"}),
    "cfb": ({"cfb": "KXNCAAFGAME"}, {"football_team_full_game_winner"}),
    "cbb": ({"cbb": "KXNCAAMBGAME"}, {"basketball_team_full_game_winner"}),
    "ufc": ({"ufc": "KXUFCFIGHT"}, {"ufc_fight_winner"}),
}
MON = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split()
PAIR_TTL = 60            # s to reuse a sport's pairing (books are re-read every scan)
EVENT_TTL = 6 * 3600     # s to cache each event's market list
LOOKBACK = dt.timedelta(hours=5)
MAX_GAP = 0.15           # $: same side priced this far apart on the two venues = not the same game
# "state" is NOT a stop word: with it dropped, Michigan State == Michigan, Ohio State == Ohio, Kansas State == Kansas
# (11-05 review). "St" / "St." reads as "state" on both venues instead, so "Ohio St." still pairs with "Ohio State".
STOP = {"fc", "cf", "afc", "sc", "the", "de", "club", "university", "of", "u"}
ALIAS = {"st": "state"}
_pairs: dict[str, tuple[float, list]] = {}
_events: dict[str, tuple[float, dict]] = {}
dropped: dict[str, int] = {}   # sport -> games dropped by the price check at the last fetch (for the log / tests)


def norm(s: str) -> frozenset:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return frozenset(ALIAS.get(w, w) for w in "".join(c if c.isalnum() else " " for c in s).split() if w not in STOP)


def same(a: frozenset, b: frozenset) -> bool:
    """Same team / fighter: equal tokens, or one inside the other sharing >= 2 words (or both one word)."""
    if not a or not b:
        return False
    if a == b:
        return True
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    return small <= big and len(small) >= 2


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


def kalshi_dates(event: dict) -> set[str]:
    """Kalshi event-ticker dates that may hold this Polymarket event: its slug date and start date, and the day
    before each (Kalshi uses the US day)."""
    out = set()
    days = []
    if event.get("startTime"):
        days.append(dt.datetime.fromisoformat(event["startTime"].replace("Z", "+00:00")).date())
    p = (event.get("slug") or "").split("-")
    try:
        days.append(dt.date(int(p[-3]), int(p[-2]), int(p[-1])))
    except (ValueError, IndexError):
        pass
    for d in days:
        for back in (0, 1):
            x = d - dt.timedelta(days=back)
            out.add(f"{x:%y}{MON[x.month - 1]}{x:%d}")
    return out


def pm_sides(wins: list[dict]) -> dict[str, dict]:
    """Polymarket team code -> {"names": tokens, "name": display name, "ordering": home/away}."""
    out = {}
    for m in wins:
        for s in m.get("marketSides") or []:
            t = s.get("team") or {}
            code = (t.get("abbreviation") or "").upper()
            if not code:
                continue
            names = " ".join(filter(None, (t.get("name"), t.get("safeName"), t.get("alias"), s.get("description"))))
            out[code] = {"names": norm(names), "name": t.get("name") or s.get("description") or code,
                         "ordering": t.get("ordering")}
    return out


def match(event: dict, wins: list[dict], kev: dict[str, dict[str, dict]]) -> dict | None:
    """One Polymarket event + its winner markets -> the Kalshi event and code map, or None when not certain."""
    sides = pm_sides(wins)
    if len(sides) != 2:
        return None
    dates = kalshi_dates(event)
    hits = []
    for et, ms in kev.items():
        if not any(d in et for d in dates):
            continue
        kteams = {kc: norm(m.get("yes_sub_title")) for kc, m in ms.items() if kc != "TIE"}
        if len(kteams) != 2:
            continue
        mp = {}
        for code, s in sides.items():
            cand = [kc for kc, kt in kteams.items() if kc == code or same(s["names"], kt)]
            if len(cand) == 1:
                mp[code] = cand[0]
        if len(mp) == 2 and len(set(mp.values())) == 2:
            hits.append((et, ms, mp))
    if len(hits) != 1:
        return None
    et, ms, mp = hits[0]
    order = {s["ordering"]: code for code, s in sides.items()}
    if set(order) != {"home", "away"}:
        p = (event.get("slug") or "").split("-")      # <league>-<away>-<home>-date when the sides carry no ordering
        a, h = (p[1].upper(), p[2].upper()) if len(p) > 3 else ("", "")
        if {a, h} != set(sides):
            return None
        order = {"away": a, "home": h}
    markets = {}
    for m in wins:
        long = next((s for s in m.get("marketSides") or [] if s.get("long")), None)
        code = ((long or {}).get("team") or {}).get("abbreviation", "").upper()
        if not code:
            tail = m["slug"].rsplit("-", 1)[1].upper()     # soccer: one market per outcome, slug ends in the code
            code = "" if tail == "DRAW" else tail
        if code in mp:
            markets[code] = m
    if not markets:
        return None
    return dict(event=event["slug"], live=event.get("live"), start=event.get("startTime"), kalshi_event=et,
                series=et.split("-")[0], home=order["home"], away=order["away"],
                names={c: sides[c]["name"] for c in sides}, kalshi={c: ms[mp[c]] for c in mp}, pm=markets)


def pairs(sport: str, now: dt.datetime, horizon: dt.datetime) -> list[dict]:
    hit = _pairs.get(sport)
    if hit and time.time() - hit[0] < PAIR_TTL:
        return hit[1]
    leagues, types = SPORTS[sport]
    out = []
    for lg, series in leagues.items():
        kev: dict[str, dict[str, dict]] = {}
        for m in kalshi._markets(series):
            kev.setdefault(m["event_ticker"], {})[m["ticker"].rsplit("-", 1)[1]] = m
        if not kev:
            continue
        evs = []
        for e in _pm_events(lg):
            if e.get("ended") or e.get("closed"):
                continue
            st = dt.datetime.fromisoformat(e["startTime"].replace("Z", "+00:00")) if e.get("startTime") else None
            if not e.get("live") and (st is None or st > horizon or st < now - LOOKBACK):
                continue
            evs.append(e)
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            full = list(ex.map(lambda e: _pm_event(e["slug"]), evs))
        for e, ev in zip(evs, full):
            wins = [m for m in ev.get("markets") or [] if m.get("sportsMarketType") in types
                    and not m.get("closed") and m.get("active", True)]
            if wins:
                hit = match(e, wins, kev)
                if hit:
                    out.append(hit)
    _pairs[sport] = (time.time(), out)
    return out


def _names(m: dict) -> tuple[str, str]:
    return m["names"][m["away"]], m["names"][m["home"]]


def build(sport: str, m: dict, kalshi_fee, poly_fee_override: float | None = None) -> Game:
    """One matched game -> Game with 4 contracts per side it can pair (no books)."""
    start = dt.datetime.fromisoformat(m["start"].replace("Z", "+00:00")) if m.get("start") else None
    away, home = _names(m)
    g = Game(away, home, start, sport=sport, uid=m["event"], live=m.get("live"))
    g.links["Polymarket"] = f"https://polymarket.us/event/{m['event']}"
    g.links["Kalshi"] = f"https://kalshi.com/markets/{m['series'].lower()}/{m['kalshi_event'].lower()}"
    for code in (m["home"], m["away"]):
        if code not in m["kalshi"] or code not in m["pm"]:
            continue
        name = m["names"][code]
        win, lose = (("gt", 0.5), ("lt", 0.5)) if code == m["home"] else (("lt", -0.5), ("gt", -0.5))
        km = m["kalshi"][code]
        common = dict(game=g.key, axis="margin", kind="ml", tie_half=False)
        for (side, line), s, lbl in ((win, "yes", f"{name} wins"), (lose, "no", f"{name} not to win")):
            g.contracts.append(Contract(venue="Kalshi", side=side, line=line, label=lbl, how=f"{s.upper()} {name}",
                                        fee=kalshi_fee, url=g.links["Kalshi"],
                                        ref={"ticker": km["ticker"], "side": s, "shard": km.get("exchange_index")},
                                        **common))
        w = m["pm"][code]
        fee = poly_model(w, poly_fee_override)
        for (side, line), s, lbl in ((win, "long", f"{name} wins"), (lose, "short", f"{name} not to win")):
            g.contracts.append(Contract(
                venue="Polymarket", side=side, line=line, label=lbl,
                how=f"{'BUY_LONG' if s == 'long' else 'BUY_SHORT'} on \"{w.get('question') or g.title}\"", fee=fee,
                url=g.links["Polymarket"], min_size=polymarket._f(w.get("minimumTradeQty")) or 1.0,
                tick=polymarket._f(w.get("orderPriceMinTickSize")) or 0.01,
                ref={"ticker": w["slug"], "slug": w["slug"], "side": s, "market_id": w.get("id"),
                     "event": m["event"], "live": bool(m.get("live"))}, **common))
    return g


def _mid(b):
    return None if b.best_bid is None or b.best_ask is None else (b.best_bid + b.best_ask) / 2


def priced_apart(g: Game) -> bool:
    """True when some side is priced more than MAX_GAP apart on the two venues (the pairing is not trusted)."""
    by = {}
    for c in g.contracts:
        if c.label.endswith(" wins"):
            by.setdefault(c.label, {})[c.venue] = _mid(c.book)
    for v in by.values():
        a, b = v.get("Kalshi"), v.get("Polymarket")
        if a is not None and b is not None and abs(a - b) > MAX_GAP:
            return True
    return False


def fetch(horizon: dt.datetime, sport: str, poly_fee: float | None = None, kalshi_direct: bool = False) -> dict[str, Game]:
    """One sport's paired games with books; games whose prices disagree across venues are left out."""
    now = dt.datetime.now(dt.timezone.utc)
    games = {}
    for m in pairs(sport, now, horizon):
        g = build(sport, m, _kalshi_fee(m["series"], kalshi_direct), poly_fee)
        if g.contracts:
            games[g.key] = g
    contracts = [c for g in games.values() for c in g.contracts]
    kb = kalshi.fetch_books(sorted({c.ref["ticker"] for c in contracts if c.venue == "Kalshi"}))
    pb = polymarket.fetch_books(sorted({c.ref["slug"] for c in contracts if c.venue == "Polymarket"}))
    for c in contracts:
        if c.venue == "Kalshi" and c.ref["ticker"] in kb:
            c.book = kb[c.ref["ticker"]][0 if c.ref["side"] == "yes" else 1]
        elif c.venue == "Polymarket" and c.ref["slug"] in pb:
            c.book = pb[c.ref["slug"]][0 if c.ref["side"] == "long" else 1]
    bad = [k for k, g in games.items() if priced_apart(g)]
    dropped[sport] = len(bad)
    for k in bad:
        games.pop(k)
    return games
