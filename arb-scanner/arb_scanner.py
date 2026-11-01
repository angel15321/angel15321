#!/usr/bin/env python3
"""NFL moneyline arbitrage scanner.

Pulls live prices for every open NFL game from Kalshi and Polymarket (public
APIs, no key needed) and, optionally, US sportsbooks via the-odds-api
(set ODDS_API_KEY). For each game it finds the cheapest way to back each team
across every venue, fees included, and flags games where backing both teams
costs less than the guaranteed payout -- an arbitrage.

Standard library only. Run:  python3 arb_scanner.py --help
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import html
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
ODDS_API = "https://api.the-odds-api.com/v4"
POLY_NFL_SERIES = "12185"
USER_AGENT = "nfl-arb-scanner/1.0 (personal use)"

# Canonical abbreviation -> (city, nickname). Aliases cover each venue's codes.
TEAMS = {
    "ARI": ("Arizona", "Cardinals"), "ATL": ("Atlanta", "Falcons"),
    "BAL": ("Baltimore", "Ravens"), "BUF": ("Buffalo", "Bills"),
    "CAR": ("Carolina", "Panthers"), "CHI": ("Chicago", "Bears"),
    "CIN": ("Cincinnati", "Bengals"), "CLE": ("Cleveland", "Browns"),
    "DAL": ("Dallas", "Cowboys"), "DEN": ("Denver", "Broncos"),
    "DET": ("Detroit", "Lions"), "GB": ("Green Bay", "Packers"),
    "HOU": ("Houston", "Texans"), "IND": ("Indianapolis", "Colts"),
    "JAX": ("Jacksonville", "Jaguars"), "KC": ("Kansas City", "Chiefs"),
    "LAC": ("Los Angeles", "Chargers"), "LAR": ("Los Angeles", "Rams"),
    "LV": ("Las Vegas", "Raiders"), "MIA": ("Miami", "Dolphins"),
    "MIN": ("Minnesota", "Vikings"), "NE": ("New England", "Patriots"),
    "NO": ("New Orleans", "Saints"), "NYG": ("New York", "Giants"),
    "NYJ": ("New York", "Jets"), "PHI": ("Philadelphia", "Eagles"),
    "PIT": ("Pittsburgh", "Steelers"), "SEA": ("Seattle", "Seahawks"),
    "SF": ("San Francisco", "49ers"), "TB": ("Tampa Bay", "Buccaneers"),
    "TEN": ("Tennessee", "Titans"), "WAS": ("Washington", "Commanders"),
}
ALIASES = {"JAC": "JAX", "LA": "LAR", "WSH": "WAS", "LVR": "LV", "OAK": "LV"}
NICKNAMES = {nick.lower(): abbr for abbr, (_, nick) in TEAMS.items()}


def team_from_code(code: str) -> str | None:
    code = code.upper()
    code = ALIASES.get(code, code)
    return code if code in TEAMS else None


def team_from_name(name: str) -> str | None:
    """Match 'Vikings', 'Minnesota Vikings', etc. by nickname."""
    words = name.lower().replace(".", "").split()
    for w in reversed(words):
        if w in NICKNAMES:
            return NICKNAMES[w]
    return None


# --------------------------------------------------------------------------
# HTTP

def get_json(url: str, params: dict | None = None, timeout: float = 20):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                last_err = e
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise last_err  # type: ignore[misc]


# --------------------------------------------------------------------------
# Data model

@dataclass
class Quote:
    """Cost to get $1 back if `team` wins, on one venue, one way of buying."""
    venue: str
    team: str
    price: float          # raw price per $1 payout (0-1); for books = 1/decimal
    fee: float            # fee per $1 payout at that price
    size: float | None    # $ payout available at that price (None = unknown)
    how: str              # e.g. "YES MIN", "NO BUF", "DraftKings"
    american: int | None = None

    @property
    def cost(self) -> float:
        return self.price + self.fee


@dataclass
class Game:
    away: str
    home: str
    start: dt.datetime | None
    quotes: list[Quote] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> frozenset:
        return frozenset((self.away, self.home))


# --------------------------------------------------------------------------
# Fees

def kalshi_fee(p: float, rate: float) -> float:
    # Kalshi taker fee: rate * P * (1 - P) per contract (rounded up to the
    # cent per *order*; ignored here, so treat tiny edges with care).
    return rate * p * (1 - p)


def poly_fee(p: float, rate: float, exponent: float = 1.0) -> float:
    # Polymarket sports taker fee per share, from each market's feeSchedule.
    return rate * (p * (1 - p)) ** exponent


# --------------------------------------------------------------------------
# Kalshi

def fetch_kalshi(args) -> dict[frozenset, Game]:
    games: dict[frozenset, Game] = {}
    cursor = ""
    markets = []
    while True:
        params = {"series_ticker": "KXNFLGAME", "status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = get_json(f"{KALSHI_API}/markets", params)
        markets += data.get("markets", [])
        cursor = data.get("cursor") or ""
        if not cursor:
            break

    by_event: dict[str, list] = {}
    for m in markets:
        by_event.setdefault(m["event_ticker"], []).append(m)

    for ev, ms in by_event.items():
        if len(ms) != 2:
            continue
        # Event ticker looks like KXNFLGAME-26NOV09BUFMIN (away then home).
        teams = [team_from_code(m["ticker"].rsplit("-", 1)[1]) for m in ms]
        if None in teams:
            continue
        tail = ev.rsplit("-", 1)[1][7:]  # strip YYMMMDD
        a_code = ms[0]["ticker"].rsplit("-", 1)[1]
        away, home = (teams[0], teams[1]) if tail.startswith(a_code) else (teams[1], teams[0])
        start = None
        if ms[0].get("expected_expiration_time"):
            # Kalshi lists expected settlement (~3h after kickoff).
            start = parse_ts(ms[0]["expected_expiration_time"]) - dt.timedelta(hours=3)
        g = Game(away, home, start)
        g.links["Kalshi"] = f"https://kalshi.com/markets/kxnflgame/{ev.lower()}"
        mk = {t: m for t, m in zip(teams, ms)}
        for team in teams:
            other = home if team == away else away
            m_yes, m_other = mk[team], mk[other]
            # Back `team`: buy YES on team, or buy NO on the other team.
            ya = fnum(m_yes.get("yes_ask_dollars"))
            if ya and 0 < ya < 1:
                g.quotes.append(Quote("Kalshi", team, ya, kalshi_fee(ya, args.kalshi_fee),
                                      fnum(m_yes.get("yes_ask_size_fp")), f"YES {team}"))
            ob = fnum(m_other.get("yes_bid_dollars"))
            if ob and 0 < ob < 1:
                na = round(1 - ob, 4)
                g.quotes.append(Quote("Kalshi", team, na, kalshi_fee(na, args.kalshi_fee),
                                      fnum(m_other.get("yes_bid_size_fp")), f"NO {other}"))
        games[g.key] = g
    return games


# --------------------------------------------------------------------------
# Polymarket

def fetch_polymarket(args) -> dict[frozenset, Game]:
    events = get_json(f"{POLY_GAMMA}/events",
                      {"series_id": POLY_NFL_SERIES, "closed": "false", "limit": 200})
    now = dt.datetime.now(dt.timezone.utc)
    jobs = []
    for e in events:
        slug = e.get("slug", "")
        parts = slug.split("-")  # nfl-min-det-2026-11-01
        if len(parts) < 4 or parts[0] != "nfl":
            continue
        away, home = team_from_code(parts[1]), team_from_code(parts[2])
        if not away or not home:
            continue
        start = parse_ts(e.get("startTime")) if e.get("startTime") else None
        if start and start < now - dt.timedelta(hours=5):
            continue  # finished, awaiting settlement
        ml = [m for m in e.get("markets", [])
              if m.get("sportsMarketType") == "moneyline" and not m.get("closed")
              and m.get("acceptingOrders", True)]
        if not ml:
            continue
        jobs.append((away, home, start, slug, ml[0]))

    def build(job):
        away, home, start, slug, m = job
        outcomes = json.loads(m.get("outcomes") or "[]")
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if len(outcomes) != 2 or len(tokens) != 2:
            return None
        t0, t1 = team_from_name(outcomes[0]), team_from_name(outcomes[1])
        if not t0 or not t1:
            return None
        sched = m.get("feeSchedule") or {}
        rate = args.poly_fee if args.poly_fee is not None else (
            sched.get("rate", 0.0) if m.get("feesEnabled") else 0.0)
        expo = sched.get("exponent", 1) or 1
        g = Game(away, home, start)
        g.links["Polymarket"] = f"https://polymarket.com/event/{slug}"
        # One order book covers both sides: token0 asks back outcome 0;
        # token0 bids (sell to them) are the same as buying outcome 1.
        try:
            book = get_json(f"{POLY_CLOB}/book", {"token_id": tokens[0]})
            asks = sorted(((float(x["price"]), float(x["size"])) for x in book.get("asks", [])))
            bids = sorted(((float(x["price"]), float(x["size"])) for x in book.get("bids", [])),
                          reverse=True)
        except Exception:
            asks = [(float(m["bestAsk"]), None)] if m.get("bestAsk") else []
            bids = [(float(m["bestBid"]), None)] if m.get("bestBid") else []
        if asks and 0 < asks[0][0] < 1:
            p, s = asks[0]
            g.quotes.append(Quote("Polymarket", t0, p, poly_fee(p, rate, expo), s, f"{t0} shares"))
        if bids and 0 < bids[0][0] < 1:
            p, s = round(1 - bids[0][0], 4), bids[0][1]
            g.quotes.append(Quote("Polymarket", t1, p, poly_fee(p, rate, expo), s, f"{t1} shares"))
        return g

    games: dict[frozenset, Game] = {}
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for g in ex.map(build, jobs):
            if g:
                games[g.key] = g
    return games


# --------------------------------------------------------------------------
# Sportsbooks (the-odds-api, optional)

def fetch_sportsbooks(args) -> dict[frozenset, Game]:
    key = args.odds_api_key or os.environ.get("ODDS_API_KEY")
    if not key:
        return {}
    data = get_json(f"{ODDS_API}/sports/americanfootball_nfl/odds", {
        "apiKey": key, "regions": args.regions, "markets": "h2h",
        "oddsFormat": "decimal",
    })
    games: dict[frozenset, Game] = {}
    for ev in data:
        away, home = team_from_name(ev.get("away_team", "")), team_from_name(ev.get("home_team", ""))
        if not away or not home:
            continue
        g = Game(away, home, parse_ts(ev.get("commence_time")) if ev.get("commence_time") else None)
        for bk in ev.get("bookmakers", []):
            if args.books and bk["key"] not in args.books:
                continue
            for mk in bk.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                for o in mk.get("outcomes", []):
                    t = team_from_name(o.get("name", ""))
                    dec = float(o.get("price") or 0)
                    if not t or dec <= 1:
                        continue
                    g.quotes.append(Quote("Sportsbook", t, 1 / dec, 0.0, None, bk["title"],
                                          american=decimal_to_american(dec)))
        games[g.key] = g
    return games


# --------------------------------------------------------------------------
# Arb math

@dataclass
class Opportunity:
    game: Game
    leg_a: Quote
    leg_b: Quote

    @property
    def total_cost(self) -> float:
        return self.leg_a.cost + self.leg_b.cost

    @property
    def roi(self) -> float:
        return 1 / self.total_cost - 1

    def stakes(self, bankroll: float) -> tuple[float, float, float]:
        """Split `bankroll` so both outcomes pay the same. Returns
        (stake_a, stake_b, guaranteed_payout)."""
        payout = bankroll / self.total_cost
        return payout * self.leg_a.cost, payout * self.leg_b.cost, payout

    def max_payout(self) -> float | None:
        sizes = [q.size for q in (self.leg_a, self.leg_b) if q.size is not None]
        return min(sizes) if sizes else None


def best_quotes(g: Game) -> dict[str, Quote]:
    best: dict[str, Quote] = {}
    for q in g.quotes:
        if q.team not in best or q.cost < best[q.team].cost:
            best[q.team] = q
    return best


def find_best_pair(g: Game, cross_venue_only: bool) -> Opportunity | None:
    a = [q for q in g.quotes if q.team == g.away]
    b = [q for q in g.quotes if q.team == g.home]
    best = None
    for qa in a:
        for qb in b:
            if cross_venue_only and qa.venue == qb.venue:
                continue
            o = Opportunity(g, qa, qb)
            if best is None or o.total_cost < best.total_cost:
                best = o
    return best


def merge(*sources: dict[frozenset, Game]) -> list[Game]:
    merged: dict[frozenset, Game] = {}
    for src in sources:
        for k, g in src.items():
            if k not in merged:
                merged[k] = Game(g.away, g.home, g.start)
            m = merged[k]
            if m.start is None and g.start:
                m.start = g.start
            m.quotes += g.quotes
            m.links.update(g.links)
    return sorted(merged.values(), key=lambda g: (g.start or dt.datetime.max.replace(tzinfo=dt.timezone.utc), g.away))


# --------------------------------------------------------------------------
# Helpers

def fnum(x) -> float | None:
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def decimal_to_american(dec: float) -> int:
    return round((dec - 1) * 100) if dec >= 2 else round(-100 / (dec - 1))


def prob_to_american(p: float) -> str:
    if p <= 0 or p >= 1:
        return "—"
    v = round((1 - p) / p * 100) if p <= 0.5 else round(-p / (1 - p) * 100)
    return f"+{v}" if v > 0 else str(v)


def fmt_local(t: dt.datetime | None) -> str:
    if not t:
        return "?"
    return t.astimezone().strftime("%a %b %d %I:%M %p")


# --------------------------------------------------------------------------
# Output

def render_text(games: list[Game], opps: list[Opportunity], args) -> str:
    out = []
    venues = ["Kalshi", "Polymarket"] + (["Sportsbook"] if any(
        q.venue == "Sportsbook" for g in games for q in g.quotes) else [])
    hdr = f"{'GAME':<12} {'TEAM':<5}" + "".join(f"{v:>13}" for v in venues) + f"{'BEST':>9}"
    out.append(hdr)
    out.append("-" * len(hdr))
    for g in games:
        best = best_quotes(g)
        for t in (g.away, g.home):
            row = f"{(g.away + ' @ ' + g.home) if t == g.away else fmt_local(g.start)[4:10]:<12} {t:<5}"
            for v in venues:
                qs = [q for q in g.quotes if q.team == t and q.venue == v]
                row += f"{prob_to_american(min(qs, key=lambda q: q.cost).cost) if qs else '—':>13}"
            row += f"{prob_to_american(best[t].cost) if t in best else '—':>9}"
            out.append(row)
    out.append("")
    arbs = [o for o in opps if o.roi >= args.min_roi]
    if not arbs:
        closest = sorted(opps, key=lambda o: o.total_cost)[:3]
        out.append("No arbs right now (after fees). Closest:")
        for o in closest:
            out.append(f"  {o.game.away} @ {o.game.home}: cost {o.total_cost:.4f} per $1 "
                       f"({o.roi * 100:+.2f}%)  {o.leg_a.venue} {o.leg_a.how} + {o.leg_b.venue} {o.leg_b.how}")
    else:
        out.append(f"ARBS (ROI ≥ {args.min_roi * 100:.2f}% after fees):")
        for o in sorted(arbs, key=lambda o: -o.roi):
            sa, sb, pay = o.stakes(args.bankroll)
            cap = o.max_payout()
            out.append(f"  {o.game.away} @ {o.game.home}  ROI {o.roi * 100:.2f}%")
            out.append(f"    ${sa:,.2f} on {o.leg_a.team} via {o.leg_a.venue} ({o.leg_a.how}) @ {o.leg_a.price:.3f}")
            out.append(f"    ${sb:,.2f} on {o.leg_b.team} via {o.leg_b.venue} ({o.leg_b.how}) @ {o.leg_b.price:.3f}")
            out.append(f"    pays ${pay:,.2f} either way (+${pay - args.bankroll:,.2f})"
                       + (f"; top-of-book depth ≈ ${cap:,.0f} payout" if cap else ""))
    return "\n".join(out)


def render_html(games: list[Game], opps: list[Opportunity], args, errors: list[str]) -> str:
    now = dt.datetime.now().astimezone().strftime("%b %d, %I:%M:%S %p %Z")
    venues = ["Kalshi", "Polymarket"] + (["Sportsbook"] if any(
        q.venue == "Sportsbook" for g in games for q in g.quotes) else [])
    opp_by_game = {id(o.game): o for o in opps}
    arbs = sorted([o for o in opps if o.roi >= args.min_roi], key=lambda o: -o.roi)
    e = html.escape

    def cell(q: Quote | None, is_best: bool) -> str:
        if not q:
            return '<td class="na">—</td>'
        sub = e(q.how if q.venue != "Kalshi" else q.how.split()[0])
        cls = "best" if is_best else ""
        return (f'<td class="{cls}" title="price {q.price:.3f} + fee {q.fee:.4f}">'
                f'{prob_to_american(q.cost)}<small>{q.cost * 100:.1f}¢ · {sub}</small></td>')

    rows = []
    for g in games:
        best = best_quotes(g)
        o = opp_by_game.get(id(g))
        arb = o is not None and o.roi >= args.min_roi
        edge = f'{o.roi * 100:+.2f}%' if o else "—"
        for i, t in enumerate((g.away, g.home)):
            tds = []
            for v in venues:
                qs = [q for q in g.quotes if q.team == t and q.venue == v]
                q = min(qs, key=lambda q: q.cost) if qs else None
                tds.append(cell(q, q is not None and best.get(t) is q))
            first = i == 0
            lead = (f'<td rowspan="2" class="when">{e(fmt_local(g.start))}'
                    + "".join(f'<a href="{e(u)}" target="_blank" rel="noopener">{e(k)}</a>' for k, u in g.links.items())
                    + "</td>") if first else ""
            tail = (f'<td rowspan="2" class="edge {"pos" if arb else ""}">{edge}</td>') if first else ""
            rows.append(f'<tr class="{"top" if first else ""} {"arbrow" if arb else ""}">{lead}'
                        f'<td class="team"><b>{t}</b> {e(TEAMS[t][1])}</td>{"".join(tds)}{tail}</tr>')

    cards = []
    for o in arbs:
        sa, sb, pay = o.stakes(args.bankroll)
        cap = o.max_payout()
        cards.append(f"""<div class="card"><div class="cardhead"><b>{o.game.away} @ {o.game.home}</b>
<span class="roi">+{o.roi * 100:.2f}%</span></div>
<p>${sa:,.2f} on <b>{o.leg_a.team}</b> — {e(o.leg_a.venue)} ({e(o.leg_a.how)}) @ {o.leg_a.price:.3f}</p>
<p>${sb:,.2f} on <b>{o.leg_b.team}</b> — {e(o.leg_b.venue)} ({e(o.leg_b.how)}) @ {o.leg_b.price:.3f}</p>
<p class="muted">Pays ${pay:,.2f} either way (+${pay - args.bankroll:,.2f} on ${args.bankroll:,.0f})
{f"· top-of-book depth ≈ ${cap:,.0f}" if cap else ""}</p></div>""")
    if not cards:
        closest = sorted(opps, key=lambda o: o.total_cost)[:3]
        cards.append('<div class="card empty"><b>No arbs right now after fees.</b><p class="muted">Closest: '
                     + "; ".join(f"{o.game.away} @ {o.game.home} {o.roi * 100:+.2f}%" for o in closest)
                     + "</p></div>")
    err = "".join(f'<div class="warn">{e(x)}</div>' for x in errors)
    refresh = f'<meta http-equiv="refresh" content="{args.watch}">' if args.watch else ""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}
<title>NFL Arb Scanner</title><style>
:root{{--bg:#0f1115;--panel:#171a21;--line:#262b36;--text:#e6e8ee;--muted:#8a91a3;--good:#1f9d55;--goodbg:#13361f;--best:#2b6cb0;--warn:#b7791f}}
@media (prefers-color-scheme: light){{:root:not([data-theme="dark"]){{--bg:#f6f7f9;--panel:#fff;--line:#e3e6ec;--text:#161a22;--muted:#5d6577;--goodbg:#e3f6ea;--best:#2b6cb0}}}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:1100px;margin:0 auto;padding:20px 16px}}
h1{{font-size:20px;margin:0 0 4px}} .muted{{color:var(--muted)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin:16px 0}}
.card{{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--good);border-radius:8px;padding:12px 14px}}
.card.empty{{border-left-color:var(--line)}} .card p{{margin:6px 0}}
.cardhead{{display:flex;justify-content:space-between}} .roi{{color:var(--good);font-weight:700}}
.wrap{{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--panel)}}
table{{border-collapse:collapse;width:100%;min-width:640px}}
th,td{{padding:7px 10px;text-align:right;white-space:nowrap}}
th{{font-size:12px;color:var(--muted);font-weight:600;border-bottom:1px solid var(--line)}}
td small{{display:block;font-size:11px;color:var(--muted)}}
tr.top td{{border-top:1px solid var(--line)}}
td.when,td.team,th.l{{text-align:left}} td.when a{{display:block;font-size:12px;color:var(--best)}}
td.best{{font-weight:700;color:var(--best)}} td.na{{color:var(--muted)}}
td.edge{{font-weight:600;color:var(--muted)}} td.edge.pos{{color:var(--good)}}
tr.arbrow td{{background:var(--goodbg)}}
.warn{{color:var(--warn);margin:6px 0}} footer{{margin-top:16px;font-size:12px}}
</style></head><body><main>
<h1>NFL moneyline arbs</h1>
<div class="muted">Updated {e(now)} · prices are the cost to win $1, fees included · arb when backing both teams costs &lt; $1</div>
{err}
<div class="cards">{"".join(cards)}</div>
<div class="wrap"><table><thead><tr><th class="l">Kickoff</th><th class="l">Team</th>
{"".join(f"<th>{v}</th>" for v in venues)}<th>Arb edge</th></tr></thead><tbody>
{"".join(rows)}</tbody></table></div>
<footer class="muted">Blue = cheapest way to back that team. "Arb edge" = return from the best two-leg combo
({'different venues only' if args.cross_venue_only else 'any venues'}); negative means no arb.
Kalshi fee {args.kalshi_fee:g}·P·(1−P); Polymarket fee from each market's schedule. Prices move fast — confirm both legs before placing.</footer>
</main></body></html>"""


# --------------------------------------------------------------------------

def scan(args):
    errors: list[str] = []
    sources = {"Kalshi": fetch_kalshi, "Polymarket": fetch_polymarket, "Sportsbooks": fetch_sportsbooks}
    results = {}
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(fn, args): name for name, fn in sources.items()}
        for f in cf.as_completed(futs):
            name = futs[f]
            try:
                results[name] = f.result()
            except Exception as exc:  # keep going with the other venues
                errors.append(f"{name} failed: {exc}")
                results[name] = {}
    if not (args.odds_api_key or os.environ.get("ODDS_API_KEY")):
        errors.append("Sportsbooks off: set ODDS_API_KEY (free key at the-odds-api.com) to include them.")
    # Polymarket first: its kickoff times are exact (Kalshi's are estimated).
    games = merge(results["Polymarket"], results["Kalshi"], results["Sportsbooks"])
    horizon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=args.days)
    games = [g for g in games if g.quotes and (g.start is None or g.start <= horizon)]
    opps = [o for g in games if (o := find_best_pair(g, args.cross_venue_only))]
    return games, opps, errors


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bankroll", type=float, default=100.0, help="total $ to split across both legs (default 100)")
    ap.add_argument("--min-roi", type=float, default=0.0, help="only report arbs at or above this ROI, e.g. 0.005 = 0.5%%")
    ap.add_argument("--kalshi-fee", type=float, default=0.07, help="Kalshi taker fee coefficient (default 0.07)")
    ap.add_argument("--poly-fee", type=float, default=None,
                    help="override Polymarket fee rate (default: read from each market)")
    ap.add_argument("--odds-api-key", default=None, help="the-odds-api key (or set ODDS_API_KEY)")
    ap.add_argument("--regions", default="us", help="the-odds-api regions (default us)")
    ap.add_argument("--books", nargs="*", default=None,
                    help="only these sportsbook keys, e.g. draftkings fanduel betmgm")
    ap.add_argument("--cross-venue-only", action="store_true",
                    help="ignore combos where both legs are on the same venue")
    ap.add_argument("--days", type=float, default=8,
                    help="only games kicking off within N days (default 8)")
    ap.add_argument("--html", metavar="FILE", help="also write an HTML report to FILE")
    ap.add_argument("--json", metavar="FILE", help="also write arbs as JSON to FILE")
    ap.add_argument("--watch", type=int, metavar="SECONDS", default=0,
                    help="rescan every N seconds (min 30)")
    args = ap.parse_args(argv)
    if args.watch:
        args.watch = max(30, args.watch)

    while True:
        games, opps, errors = scan(args)
        print(f"\n=== {dt.datetime.now().strftime('%H:%M:%S')} · {len(games)} games ===")
        for x in errors:
            print("! " + x, file=sys.stderr)
        print(render_text(games, opps, args))
        if args.html:
            with open(args.html, "w", encoding="utf-8") as f:
                f.write(render_html(games, opps, args, errors))
        if args.json:
            arbs = [o for o in opps if o.roi >= args.min_roi]
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump([{
                    "game": f"{o.game.away} @ {o.game.home}",
                    "start": o.game.start.isoformat() if o.game.start else None,
                    "roi": round(o.roi, 5),
                    "legs": [{"team": q.team, "venue": q.venue, "how": q.how,
                              "price": q.price, "fee": round(q.fee, 5), "size": q.size}
                             for q in (o.leg_a, o.leg_b)],
                } for o in arbs], f, indent=2)
        if not args.watch:
            break
        time.sleep(args.watch)


if __name__ == "__main__":
    main()
