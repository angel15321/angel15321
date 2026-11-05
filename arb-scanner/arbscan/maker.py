"""Maker mode: rest bids on Polymarket US priced off Kalshi's live ask, hedge on Kalshi the moment one fills.

Why: as a taker the bot lost every race (11-02/03: 50 attempts, 0 hedged). The quotes it chased were pulled
before even a 10 ms Kalshi order landed. A maker does not race for the fill: the other side comes to it.

Up to cfg.maker_quotes quotes at once, at most one per game (a game's news would hit them all together):
1. For every cross-venue pair (a Polymarket contract + the Kalshi contract that covers the other outcomes)
   the most we may pay on Polymarket is  guaranteed - (Kalshi ask + Kalshi taker fee) - maker_edge.
   Polymarket makers pay no fee. A quote goes one tick over the best Polymarket bid (our own excluded),
   never above that ceiling, never at or above the Polymarket ask (participateDontInitiate: the exchange
   rejects a crossing quote instead of taking).
2. A quote is PULLED whenever it is no longer safe: the Kalshi ask it is priced from got worse or thinner,
   either feed stopped, the game started (schedule or Polymarket's live flag), or it outlived maker_ttl.
   A new price for the SAME pair is one modify call (no gap with nothing resting, one round trip instead of
   cancel + post); a different pair is a cancel + post. At most one re-price per quote per maker_reprice s.
3. Size: the day's remaining room (and cash) is split across the quote slots, so if every quote filled at
   once the total still fits the daily cap.
4. A fill (private order stream, REST poll as backup): Kalshi hedge IOC at once for the whole contracts
   filled, then cancel the rest, then the normal hedge retries / sell-back-or-hold rule (trader._after_leg1).
   Each fill is booked once (Quote.booked). A quote whose cancel is not confirmed after a fill stays tracked
   ("dead"): its cancel is repeated every poll and anything more it fills is still hedged and booked. A fill
   under one Kalshi contract cannot be hedged at all: it is booked as `dust`, which is not a failed hedge.
5. Every quote is written to the `quotes` table before it is sent; quotes left open by a crash are
   cancelled at the next start, and one that filled meanwhile is booked as an `error` row (halts the bot).
   On any exit (kill file, halt, Ctrl-C, SIGTERM) every resting quote is cancelled.

The risk is the known one: a resting quote is a free option, and the fills we least want come when news
moves Kalshi before we pull it (Polymarket's cancel takes 85-385 ms). Pre-game only by default, small size,
and a fill whose hedge fails goes through the same hold / halt rules as a taker trade.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import time
from dataclasses import dataclass, field

from . import engine, kalshi, polymarket
from .brokers import Fill, floor_to, max_price_within
from .model import Contract

SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
  order_id TEXT PRIMARY KEY,
  mode TEXT, posted_at REAL, closed_at REAL,
  game TEXT, kickoff TEXT, opp_key TEXT,
  pm TEXT, k TEXT,              -- contract snapshots (JSON)
  price REAL, qty REAL, k_ask REAL, guaranteed REAL,
  filled REAL, note TEXT
);
"""
MODIFY_CHECKS = 3        # the first modifies of a run are read back to confirm the new price took
MODIFY_WAIT = 1.5        # s: a modify is applied a moment after its (empty) reply -- 11-04 01:52Z the instant read
                         # still showed the old price and the run wrongly fell back to cancel + post
SWAP_AGE = 30.0          # s: a quote must have rested this long before a better pair elsewhere may replace it
ROOM_SLACK = 1.15        # new quotes are sized into room / this, so a re-price a tick or two up still fits the room
STOP_TRIES = 4           # cancel passes on the way out (each waits up to 1.5 s per quote; systemd allows 20 s)
DEAD_POLL = 0.3          # s between looks at a quote whose cancel is out but not confirmed yet
STREAM_POLL = 3.0        # s between REST reads of a quote the private order stream is following (a safety net only;
                         # 11-05: one read a second per quote was three round trips a second on the trading thread)
SUSPECT_S = 30.0         # s a quote whose post came back as an ERROR is looked for on Polymarket (the exchange
                         # processes or rejects an order within 5 s of receiving it; our HTTP timeout is 15 s)


def connect(db) -> None:
    db.executescript(SCHEMA)


@dataclass
class Quote:
    oid: str
    key: str                 # opp key  a.id|b.id
    pm_id: str
    k_id: str
    game_key: str
    title: str
    price: float
    qty: float
    k_ask: float             # Kalshi ask the quote was priced from
    guaranteed: float
    posted_at: float         # first posted (maker_ttl counts from here)
    priced_at: float = 0.0   # last post or modify (maker_reprice counts from here)
    polled_at: float = 0.0
    pm: Contract | None = None   # latest contract objects (books are refreshed every scan / event)
    k: Contract | None = None
    game: object = None
    live: bool = False           # posted on a game in progress (live edge, stricter pull)
    booked: float = 0.0          # contracts of this order already in a trade row (a fill is booked once)
    booked_notional: float = 0.0
    booked_fee: float = 0.0
    dead: bool = False           # its cancel is out but not confirmed yet: never re-priced or checked again, the cancel
                                 # is confirmed (or sent again) by poll(), and any fill meanwhile is hedged and booked
    why: str = ""                # why it was pulled (the ledger note once the cancel is confirmed)
    cancel_at: float = 0.0       # when its cancel was last sent

    def unbooked(self, st: dict) -> float:
        """Contracts the order state `st` shows filled that no trade row holds yet."""
        return (st.get("cum") or 0.0) - self.booked

    def outlay(self) -> float:
        fee = self.k.fee.per_contract(self.k_ask) if self.k is not None else 0.0
        return self.qty * (self.price + self.k_ask + fee)


@dataclass
class State:
    quotes: dict = field(default_factory=dict)     # order id -> Quote
    last_post: float = 0.0
    rejects: int = 0
    paused: str = ""
    modify_ok: bool = True    # False after a modify that did not take: re-price by cancel + post this run
    modify_checks: int = 0
    modify_slow: int = 0      # slow modifies in a row (accepted, not applied within MODIFY_WAIT)
    ids: set = field(default_factory=set)          # every order id tracked this run (resting or closed)
    quotable: dict = field(default_factory=dict)   # game key -> quotable pairs at its last check (for the log)
    guard_at: float = 0.0     # last live check of the resting quotes (0 = nothing was resting)
    guard_gap: float = 0.0    # longest time between two of them since the last 60-s line: the pull latency we own
    gaps: list = field(default_factory=list)       # every such gap since the last 60-s line (median / 95th for the log)
    net: dict = field(default_factory=lambda: {"cancel": 0.0, "post": 0.0, "read": 0.0})   # s in Polymarket calls
    suspects: list = field(default_factory=list)   # posts that came back as an error: may rest all the same
    sweep_at: float = 0.0
    stats: dict = field(default_factory=lambda: {"quotable": 0, "posted": 0, "pulled": 0, "repriced": 0,
                                                 "modified": 0, "fills": 0})

    @property
    def quote(self) -> Quote | None:
        """The one resting quote when there is exactly one (tests, the 1-slot setup)."""
        return next(iter(self.quotes.values())) if len(self.quotes) == 1 else None


STATE = State()


def reset() -> None:
    global STATE
    STATE = State()


# ---------------------------------------------------------------- pricing

def own_free_bid(c: Contract, own: dict) -> float | None:
    """Best Polymarket bid on c with our own resting quote taken out (else we would outbid ourselves).
    `own` = {pm contract id: Quote}."""
    q = own.get(c.id) if own else None
    for p, s in c.book.bids:
        if q is not None and abs(p - q.price) < 1e-6:
            s -= max(0.0, q.qty - q.booked)
        if s > 1e-9:
            return p
    return None


@dataclass
class Cand:
    key: str
    game: object
    pm: Contract
    k: Contract
    a_is_pm: bool
    guaranteed: float
    middle: tuple | None
    price: float
    qty: float
    k_ask: float
    edge: float               # $ per pair after fees, at our price and the Kalshi ask
    live: bool = False

    @property
    def score(self) -> float:
        return self.edge * self.qty


def live_sports(cfg) -> set:
    """Sports whose games in progress may be quoted: cfg.maker_in_play is a sport tuple (True = every sport)."""
    v = cfg.maker_in_play
    if v is True:
        return set(cfg.sports)
    return set(v or ())


def live_ok(cfg, g) -> bool:
    return g.sport in live_sports(cfg)


def price_pair(cfg, g, pm: Contract, k: Contract, budget: float, own, live: bool | None = None) -> Cand | None:
    """The quote for one pair, or None when it cannot be quoted at the top of the Polymarket book.
    `own` = {pm contract id: Quote} (or one Quote / None). A game in progress needs cfg.maker_live_edge."""
    if isinstance(own, Quote):
        own = {own.pm_id: own}
    if live is None:
        live = not _pregame(g, dt.datetime.now(dt.timezone.utc))
    need = cfg.maker_live_edge if live else cfg.maker_edge
    a, b = (pm, k) if pm.side == "gt" else (k, pm)
    if b.line < a.line:
        return None
    guaranteed, middle = engine.guaranteed_payout(a, b)
    if guaranteed <= 0 or not k.book.asks:
        return None
    ka, k_size = k.book.asks[0]
    if ka < cfg.maker_min_price or ka > 1 - cfg.maker_min_price:
        return None
    unit_k = ka + k.fee.per_contract(ka)
    tick = pm.tick or 0.01
    ceiling = floor_to(guaranteed - unit_k - need + 1e-9, tick)
    bid = own_free_bid(pm, own or {})
    ask = pm.book.best_ask
    price = ceiling if bid is None else min(ceiling, round(bid + tick, 4))
    if ask is not None:
        price = min(price, round(ask - tick, 4))
    if price < cfg.maker_min_price or price > 1 - cfg.maker_min_price:
        return None
    if bid is not None and price + 1e-9 < bid:
        return None               # behind the best bid: it would only fill when the book is swept through us
    qty = math.floor(min(budget / (price + unit_k), k_size) + 1e-9)
    qty = min(qty, cfg.maker_max_qty) if cfg.maker_max_qty else qty
    if qty < max(1.0, pm.min_size):
        return None
    edge = guaranteed - price - unit_k
    if edge < need - 1e-9:
        return None
    return Cand(f"{a.id}|{b.id}", g, pm, k, pm is a, guaranteed, middle, price, qty, ka, edge, live)


def candidates(cfg, games, budget: float, own) -> list[Cand]:
    if isinstance(own, Quote):
        own = {own.pm_id: own}
    out = []
    now_dt = dt.datetime.now(dt.timezone.utc)
    for g in games:
        live = not _pregame(g, now_dt)
        pms = [c for c in g.contracts if c.venue == "Polymarket"]
        ks = [c for c in g.contracts if c.venue == "Kalshi"]
        for pm in pms:
            for k in ks:
                if k.axis != pm.axis or k.side == pm.side:
                    continue
                c = price_pair(cfg, g, pm, k, budget, own, live)
                if c is not None:
                    out.append(c)
    out.sort(key=lambda c: -c.score)
    return out


# ---------------------------------------------------------------- the loop's entry points

def _snap(c: Contract) -> str:
    return json.dumps({"venue": c.venue, "label": c.label, "how": c.how, "ref": c.ref, "kind": c.kind,
                       "side": c.side, "line": c.line, "tie_half": c.tie_half, "fee_rate": c.fee.rate})


def _room(db, cfg, brokers, log) -> tuple[float, float, dict | None, float]:
    """(day/open room $, Polymarket pot $, Kalshi shard pots or None, Kalshi pot $) -- before any resting quote."""
    from . import trader
    mode = "live" if cfg.live else "dry"
    room = min(cfg.max_daily_spend - trader.spent_today(db, mode), cfg.max_open - trader.open_exposure(db, mode))
    try:
        cash = trader.balances(brokers, background=True)     # in the loop: never a balance read on this thread
    except Exception as e:
        log(f"! maker: balance check failed: {e}")
        return 0.0, 0.0, None, 0.0
    shards = getattr(brokers.get("Kalshi"), "shards", None)
    return max(0.0, room), cash.get("Polymarket", 0.0) * 0.95, shards, cash.get("Kalshi", 0.0) * 0.95


def _k_pot(k: Contract, shards, k_all: float) -> float:
    if shards is not None and k.ref.get("shard") is not None:
        return shards.get(int(k.ref["shard"]), 0.0) * 0.95
    return k_all


def _feeds_ok() -> bool:
    pf, kf = polymarket._feed, kalshi._feed
    return pf is not None and pf.alive() and kf is not None and kf.alive()


def _pregame(g, now_dt) -> bool:
    """Not started by the schedule, and Polymarket does not flag it live either (a belt to the start-time braces).
    Game.started() trusts a venue live flag over the start time, and the name-paired sports (winners.py) carry that
    flag as False until Polymarket sets it AND the 60 s pairing cache turns over: a pre-game quote would ride into
    play. So a scheduled start that has passed also ends pre-game -- except tennis, whose start times are slots."""
    if g.started(now_dt) or any(c.ref.get("live") for c in g.contracts if c.venue == "Polymarket"):
        return False
    return g.sport == "tennis" or g.start is None or g.start > now_dt


def step(db, cfg, brokers, games, log, full: bool = False):
    """Called with freshly refreshed books for `games` (a full scan, or the games an event touched)."""
    s = STATE
    now, now_dt = time.time(), dt.datetime.now(dt.timezone.utc)
    by_id = {c.id: (g, c) for g in games for c in g.contracts}
    in_view = {g.key for g in games}
    for q in list(s.quotes.values()):
        if q.pm_id in by_id and q.k_id in by_id:
            q.game, q.pm = by_id[q.pm_id]
            q.k = by_id[q.k_id][1]
        if q.dead:
            continue                             # its cancel is already out; poll() repeats it until it shows
        if q.pm is not None and (q.pm_id in by_id or full):
            # seen = BOTH legs: a game whose Kalshi side dropped out of this scan left q.k on a book nobody refreshes
            why = _unsafe(cfg, q, q.pm_id in by_id and q.k_id in by_id, now, now_dt)
            if why and pull(db, cfg, brokers, q, why, log) == "halt":
                return "halt"
    if s.paused:
        return None
    ok = [g for g in games if _pregame(g, now_dt) or live_ok(cfg, g)]
    if not ok or not _feeds_ok():
        return None
    from . import trader
    room, pm_pot, shards, k_all = _room(db, cfg, brokers, log)
    slots = max(1, cfg.maker_quotes)
    plan = room / ROOM_SLACK          # what new quotes are sized into; the slack above it is for re-prices only
    per = min(cfg.max_per_trade, plan / slots, pm_pot / slots)
    if per < 2:
        return None
    own = {q.pm_id: q for q in s.quotes.values()}
    taken = trader.recent_keys(db, cfg.cooldown_min)        # one read, not one per candidate
    cands = [c for c in candidates(cfg, ok, per, own)
             if now - c.k.book.fetched_at <= cfg.max_book_age and c.key not in taken]
    if full:
        s.stats["quotable"] = max(s.stats["quotable"], len(cands))
        s.quotable = {}
    for g in ok:
        s.quotable[g.key] = 0
    for c in cands:
        s.quotable[c.game.key] = s.quotable.get(c.game.key, 0) + 1
    by_key = {}
    for c in cands:
        by_key.setdefault(c.key, c)

    # 1. our quotes in view: keep, re-price (same pair), or pull
    for q in list(s.quotes.values()):
        if q.dead or q.game_key not in in_view or now - q.priced_at < cfg.maker_reprice:
            continue
        cur = by_key.get(q.key)
        if cur is None:
            if pull(db, cfg, brokers, q, "no longer quotable at the top of the book", log) == "halt":
                return "halt"
        elif abs(cur.price - q.price) > 1e-6:
            if reprice(db, cfg, brokers, q, cur, log) == "halt":
                return "halt"

    # 2. room shrank (a fill booked spend): the newest quotes go until the rest fit the room itself. The slack that
    #    keeps a re-price a tick or two up from pull-and-re-posting a quote every second (box paper run 11-04 01:46Z)
    #    is INSIDE the room now (new quotes are sized into room / ROOM_SLACK): until 11-05 it sat on top, so every
    #    resting quote filling at once could pass --max-daily by 15% of the room left.
    committed = sum(q.outlay() for q in s.quotes.values())
    for q in sorted((x for x in s.quotes.values() if not x.dead), key=lambda x: -x.posted_at):
        if committed <= room + 0.01:
            break
        committed -= q.outlay()
        if pull(db, cfg, brokers, q, f"day room ${room:.2f} no longer covers every resting quote", log) == "halt":
            return "halt"

    # 3. an empty slot takes the best pair of a game we are not quoting yet (one new post per step)
    if now - s.last_post < cfg.maker_reprice / max(1, slots):
        return None
    games_used = {q.game_key for q in s.quotes.values()} | {sp["cand"].game.key for sp in s.suspects}
    ks_used = {q.k_id for q in s.quotes.values()} | {sp["cand"].k.id for sp in s.suspects}
    n_live = sum(q.live for q in s.quotes.values())
    free = [c for c in cands if c.game.key not in games_used and c.k.id not in ks_used and c.key not in
            {q.key for q in s.quotes.values()} and not (c.live and n_live >= cfg.maker_live_quotes)]
    if not free:
        return None
    best = free[0]
    if len(s.quotes) >= slots:
        # full: swap out the weakest quote only for a clearly better pair, and only one older than maker_reprice
        def score(q):
            c = by_key.get(q.key)
            return c.score if c is not None else 0.0
        alive = [q for q in s.quotes.values() if not q.dead]
        if not alive:
            return None
        weak = min(alive, key=score)
        if weak.game_key not in in_view or best.score <= score(weak) * 1.5 or now - weak.posted_at < SWAP_AGE:
            return None
        s.stats["repriced"] += 1
        if pull(db, cfg, brokers, weak, f"swapped for {best.game.title} {best.pm.label} @ {best.price:.2f}", log) == "halt":
            return "halt"
        if weak.oid in s.quotes:
            return None
    # a new quote is sized from the room the resting ones leave, split over the free slots
    committed = sum(q.outlay() for q in s.quotes.values())
    free_slots = max(1, slots - len(s.quotes))
    per_new = min(cfg.max_per_trade, (plan - committed) / free_slots, pm_pot / slots)
    if per_new < 2:
        return None
    best = price_pair(cfg, best.game, best.pm, best.k, per_new, own, best.live)
    if best is None:
        return None
    k_pot = _k_pot(best.k, shards, k_all) - sum(q.qty * (q.k_ask + q.k.fee.per_contract(q.k_ask))
                                                for q in s.quotes.values() if q.k is not None and
                                                q.k.ref.get("shard") == best.k.ref.get("shard"))
    if k_pot < best.qty * (best.k_ask + best.k.fee.per_contract(best.k_ask)) - 1e-9:
        return None
    return post(db, cfg, brokers, best, log)


def guard(db, cfg, brokers, log):
    """Every resting quote against the LIVE books, before anything slower. Reads the feeds' in-memory books (no
    network) for our own two or three quotes only, so it costs microseconds and the trading loop runs it at the
    start of every pass and between pricing slices: a pull never waits for a scan or for the pricing of other
    games (11-05: quotes filled with Kalshi already 8-20c past the price they were quoted off)."""
    s = STATE
    if not s.quotes:
        s.guard_at = 0.0
        return None
    from . import trader
    now, now_dt = time.time(), dt.datetime.now(dt.timezone.utc)
    if s.guard_at:
        s.guard_gap = max(s.guard_gap, now - s.guard_at)
        if len(s.gaps) < 100_000:
            s.gaps.append(now - s.guard_at)
    s.guard_at = now
    for q in list(s.quotes.values()):
        if q.dead or q.pm is None or q.k is None:
            continue
        kb = trader.live_book(q.k)
        if kb is not None:
            q.k.book = kb                        # the game's own contract object: the next pricing sees it too
        why = _unsafe(cfg, q, True, now, now_dt)
        if why and pull(db, cfg, brokers, q, why, log) == "halt":
            return "halt"
    return None


def sync(db, cfg, brokers, games, log):
    """A full scan arrived: follow its (new) contract objects, pull a quote whose market is not in it any more,
    then check every quote against the live books. The pricing of the games themselves follows in slices."""
    s = STATE
    by_id = {c.id: (g, c) for g in games for c in g.contracts}
    for q in list(s.quotes.values()):
        if q.pm_id in by_id and q.k_id in by_id:
            q.game, q.pm = by_id[q.pm_id]
            q.k = by_id[q.k_id][1]
        elif not q.dead and pull(db, cfg, brokers, q, "its market left the scan", log) == "halt":
            return "halt"
    keys = {g.key for g in games}
    s.quotable = {k: v for k, v in s.quotable.items() if k in keys}
    return guard(db, cfg, brokers, log)


def _unsafe(cfg, q: Quote, seen: bool, now: float, now_dt) -> str:
    if not _feeds_ok():
        return "a book feed is down"
    if not seen:
        return "its market left the scan"
    if q.game is not None and not q.live and not _pregame(q.game, now_dt):
        return "the game started"                # a pre-game quote never carries into play (its edge was pre-game)
    if now - q.posted_at > cfg.maker_ttl:
        return f"older than {cfg.maker_ttl:g}s (refresh)"
    k = q.k
    if k is None or not k.book.asks:
        return "Kalshi hedge side is empty"
    if now - k.book.fetched_at > cfg.max_book_age:
        return "Kalshi book went stale"
    ka, size = k.book.asks[0]
    unit_k = ka + k.fee.per_contract(ka)
    if q.live and ka > q.k_ask + 1e-9:
        return f"Kalshi ask ticked up {q.k_ask:.2f} -> {ka:.2f} (live)"   # in play any move against the hedge is news
    if q.guaranteed - q.price - unit_k < (cfg.maker_live_edge if q.live else cfg.maker_edge) / 2 - 1e-9:
        return f"Kalshi ask moved {q.k_ask:.2f} -> {ka:.2f}"
    if size + 1e-9 < q.qty:
        return f"Kalshi ask thinned to {size:g} < {q.qty:g}"
    return ""


def post(db, cfg, brokers, c: Cand, log):
    s = STATE
    s.last_post = t0 = time.time()
    f = brokers["Polymarket"].post(c.pm, c.qty, c.price)
    s.net["post"] += time.time() - t0
    if f.status != "resting" or not f.order_id:
        s.rejects += 1
        log(f"  ! quote not placed {c.game.title}: {c.pm.label} @ {c.price:.2f} -- {f.status}: {f.error}")
        if s.rejects >= cfg.maker_max_rejects:
            s.paused = f"Polymarket refused {s.rejects} quotes in a row (last: {f.status} {f.error})"
            log(f"  ! MAKER PAUSED: {s.paused}. Fix, then restart.")
        if f.order_id:                         # an id with no resting state: make sure it is gone
            _cancel(brokers, c.pm, f.order_id)
        elif f.status == "error" and "HTTP 4" not in (f.error or ""):
            # No reply we can read (a timeout, a 5xx; a 4xx IS an answer: refused): the exchange may have taken the
            # order all the same, and then it rests with no id here -- nothing would ever cancel or hedge it. Look
            # for it (poll -> _sweep).
            s.suspects.append({"cand": c, "at": time.time(),
                               "intent": "ORDER_INTENT_BUY_LONG" if c.pm.ref.get("side") == "long"
                               else "ORDER_INTENT_BUY_SHORT"})
            log(f"    looking for it on Polymarket for {SUSPECT_S:g}s (open orders + the order stream); no new quote "
                "on this game meanwhile")
        return None
    s.rejects = 0
    _track(db, cfg, c, f.order_id)
    s.stats["posted"] += 1
    log(f"  ◷ QUOTE{' LIVE' if c.live else ''} {c.game.title}: bid {c.pm.label} @ {c.price:.2f} x{c.qty:g} on Polymarket; hedge "
        f"{c.k.label} @ {c.k_ask:.2f} on Kalshi; edge {c.edge * 100:.1f} c/pair after fees "
        f"[{len(s.quotes)}/{max(1, cfg.maker_quotes)} resting]")
    return None


def _track(db, cfg, c: Cand, oid: str, posted_at: float | None = None) -> Quote:
    """Write the quote to the ledger and start following it."""
    s = STATE
    now = time.time()
    db.execute("INSERT OR REPLACE INTO quotes (order_id, mode, posted_at, game, kickoff, opp_key, pm, k, price, qty, "
               "k_ask, guaranteed, filled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
               (oid, "live" if cfg.live else "dry", posted_at or now, c.game.title,
                c.game.start.isoformat() if c.game.start else None, c.key, _snap(c.pm), _snap(c.k), c.price, c.qty,
                c.k_ask, c.guaranteed))
    db.commit()
    q = Quote(oid, c.key, c.pm.id, c.k.id, c.game.key, c.game.title, c.price, c.qty, c.k_ask, c.guaranteed,
              posted_at or now, now, now, c.pm, c.k, c.game, c.live)
    s.quotes[oid] = q
    s.ids.add(oid)
    return q


def _sweep(db, cfg, brokers, log, now: float):
    """Find the order of a post that came back as an error. An order of ours that this run never tracked, on the
    suspect's market and side -- still open (GET /v1/orders/open) or already seen by the private order stream, which
    also catches one that filled before we looked -- is taken over as a quote and pulled at once: the cancel reports
    what it filled, and that goes through the normal hedge path."""
    s = STATE
    if not s.suspects or now < s.sweep_at:
        return None
    s.sweep_at = now + cfg.maker_poll
    unknown = {}
    fn = getattr(brokers["Polymarket"], "open_orders", None)
    if fn is not None:
        try:
            for o in fn() or []:
                if o.get("id") and o["id"] not in s.ids:
                    unknown[o["id"]] = o
        except Exception as e:
            log(f"! maker: open-orders read failed: {e}")
    feed = polymarket._orders
    if feed is not None:
        first = min(sp["at"] for sp in s.suspects) - 1.0
        for oid, (ts, o) in list(feed.orders.items()):
            if oid not in s.ids and ts >= first:
                unknown.setdefault(oid, o)
    for sp in list(s.suspects):
        c = sp["cand"]
        hit = next((o for o in unknown.values() if o.get("marketSlug") == c.pm.ref.get("slug")
                    and o.get("intent") == sp["intent"]), None)
        if hit is not None:
            s.suspects.remove(sp)
            unknown.pop(hit["id"], None)
            q = _track(db, cfg, c, hit["id"], posted_at=sp["at"])
            log(f"  ! quote {hit['id']} ({c.game.title} {c.pm.label} @ {c.price:.2f}) WAS placed although its post "
                "came back as an error: now tracked, pulling it")
            if pull(db, cfg, brokers, q, "placed through an error reply", log) == "halt":
                return "halt"
        elif now >= sp["at"] + SUSPECT_S:
            s.suspects.remove(sp)
            log(f"  maker: no order found for the failed quote on {c.game.title} after {SUSPECT_S:g}s: it was not placed")
    return None


def _repost(db, cfg, brokers, q: Quote, c: Cand, why: str, log):
    STATE.stats["repriced"] += 1
    r = pull(db, cfg, brokers, q, why, log)
    if r == "halt" or q.oid in STATE.quotes:
        return r
    return post(db, cfg, brokers, c, log)


def reprice(db, cfg, brokers, q: Quote, c: Cand, log):
    """New price for the same pair. Pre-game: modify in place (quantity kept). A LIVE quote always re-prices by
    cancel + post: 11-04 19:24Z a live modify applied more than 1.5 s after the call, while a cancel + post lands in
    0.2-0.7 s, and in play a quote resting at the old price that long is the one that gets picked off."""
    s = STATE
    if q.live:
        return _repost(db, cfg, brokers, q, c, f"re-price to {c.price:.2f} (live: cancel + post)", log)
    if not s.modify_ok:
        return _repost(db, cfg, brokers, q, c, f"re-price to {c.price:.2f}", log)
    b = brokers["Polymarket"]
    ok, err = b.modify(q.pm, q.oid, c.price, q.qty)
    now = time.time()
    if not ok:
        # refused outright (HTTP error): modify is not usable on this account -- cancel + post for the run
        s.modify_ok = False
        log(f"  ! modify refused ({q.title} {q.oid}): {err} -- re-pricing by cancel + post from now on")
        return _repost(db, cfg, brokers, q, c, f"re-price to {c.price:.2f}", log)
    if s.modify_checks < MODIFY_CHECKS:
        # read the order back: the reply is empty on success, so the first few are confirmed from the order itself
        s.modify_checks += 1
        deadline = time.time() + MODIFY_WAIT
        slow = ""
        while True:
            try:
                st = b.order_state(q.pm, q.oid)
            except Exception as e:
                slow = f"read-back failed: {e}"
                break
            if q.unbooked(st) > 1e-9:
                return _filled(db, cfg, brokers, q, st, log)
            if st.get("price") is None or abs(st["price"] - c.price) < 1e-6:
                break
            if st.get("done") or time.time() >= deadline:
                slow = f"order reads {st.get('state')} @ {st.get('price')} {MODIFY_WAIT:g}s after the modify"
                break
            time.sleep(0.1)
        if slow:
            # accepted but not applied yet (or unreadable): this quote's price is unknown -> cancel it and post fresh.
            # Only a run of them turns modify off; one slow modify is not a broken one.
            s.modify_slow += 1
            if s.modify_slow >= MODIFY_CHECKS:
                s.modify_ok = False
            log(f"  ! modify slow ({q.title} {q.oid}): {slow} -- this quote re-posted"
                + ("; 3 slow in a row: cancel + post from now on" if not s.modify_ok else ""))
            return _repost(db, cfg, brokers, q, c, f"re-price to {c.price:.2f} (modify slow)", log)
        s.modify_slow = 0
    old = q.price
    q.price, q.k_ask, q.priced_at = c.price, c.k_ask, now
    db.execute("UPDATE quotes SET price=?, k_ask=? WHERE order_id=?", (q.price, q.k_ask, q.oid))
    db.commit()
    s.stats["modified"] += 1
    log(f"  ↔ re-priced {q.title} {q.pm.label} {old:.2f} -> {c.price:.2f} (modify)")
    return None


def _close_row(db, oid: str, filled: float, note: str) -> None:
    db.execute("UPDATE quotes SET closed_at=?, filled=?, note=? WHERE order_id=?", (time.time(), filled, note, oid))
    db.commit()


def _mark_row(db, oid: str, filled: float, note: str) -> None:
    """What a still-open quote has filled so far (after a crash recover() books only what is above it)."""
    db.execute("UPDATE quotes SET filled=?, note=? WHERE order_id=?", (filled, note, oid))
    db.commit()


def _cancel(brokers, c: Contract, oid: str, wait: float | None = None) -> dict:
    """Cancel + read the order. wait=0: one read, no waiting for the state to turn (the repeat on a dead quote)."""
    t0 = time.time()
    try:
        b = brokers["Polymarket"]
        return (b.cancel(c, oid) if wait is None else b.cancel(c, oid, wait=wait)) or {}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        STATE.net["cancel"] += time.time() - t0


def _read(brokers, q: Quote) -> dict:
    """One REST read of a quote's order ({"error"} when it failed)."""
    t0 = time.time()
    try:
        return brokers["Polymarket"].order_state(q.pm, q.oid) or {}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        STATE.net["read"] += time.time() - t0


def _confirmed(st: dict) -> bool:
    """The order is finished on the exchange (cancelled / filled / expired) and that could be read."""
    return bool(st.get("done")) and not st.get("error")


def _drop(db, q: Quote, note: str) -> None:
    STATE.quotes.pop(q.oid, None)
    _close_row(db, q.oid, q.booked, note)


def pull(db, cfg, brokers, q: Quote, why: str, log, wait: float | None = 0.0):
    """Cancel one resting quote; a fill that happened meanwhile is hedged.

    The cancel is SENT here and read back once (wait=0); it is not waited for. Until 11-05 every pull polled the
    order until it read cancelled (up to 1.5 s), and nothing else -- no other quote's pull -- could happen meanwhile.
    A quote whose cancel has not shown yet is `dead`: it keeps its slot, is never re-priced, and poll() confirms it
    from the order stream or a read every DEAD_POLL s (sending the cancel again each maker_poll s). wait=None is the
    blocking form, for the way out."""
    s = STATE
    if q.oid not in s.quotes:
        return None
    q.cancel_at = time.time()
    st = _cancel(brokers, q.pm, q.oid, wait=wait)
    if q.unbooked(st) > 1e-9:
        if not q.why:
            q.why = why
        return _filled(db, cfg, brokers, q, st, log, cancelled=True)
    first = not q.dead
    if first:
        q.why = q.why or why
        s.stats["pulled"] += 1
        log(f"  ✕ pulled {q.title} {q.pm.label} @ {q.price:.2f}: {why}")
    if not _confirmed(st):
        q.dead, q.polled_at = True, time.time()
        if wait is None or st.get("error"):
            log(f"  ! quote cancel not confirmed ({q.title} {q.oid}): {st.get('error') or st.get('state')}; retrying")
        return None
    _drop(db, q, f"pulled: {q.why}" + (f"; {q.booked:g} had filled" if q.booked else ""))
    return None


def poll(db, cfg, brokers, log, force: bool = False):
    """Fill check per quote. The private order stream is the first word; a REST read backs it up every maker_poll s
    for an order the stream has not shown, every STREAM_POLL s for one it follows. A dead quote (cancel out, not
    confirmed) is confirmed here, and anything it fills meanwhile is hedged and booked."""
    s = STATE
    now = time.time()
    feed = polymarket._orders
    if s.suspects and _sweep(db, cfg, brokers, log, now) == "halt":
        return "halt"
    for q in list(s.quotes.values()):
        st = feed.state(q.oid, q.pm) if feed is not None else None
        if q.dead:
            if st is None or (not _confirmed(st) and q.unbooked(st) <= 1e-9):
                if not force and now - q.polled_at < DEAD_POLL:
                    continue
                q.polled_at = now
                if force or now - q.cancel_at >= cfg.maker_poll:
                    q.cancel_at = now
                    st = _cancel(brokers, q.pm, q.oid, wait=0.0)      # not showing after a second: send it again
                else:
                    st = _read(brokers, q)
            if q.unbooked(st) > 1e-9:
                if _filled(db, cfg, brokers, q, st, log, cancelled=True) == "halt":
                    return "halt"
            elif _confirmed(st):
                _drop(db, q, (f"pulled: {q.why}" if q.why else "its cancel was confirmed late")
                      + (f"; {q.booked:g} had filled" if q.booked else ""))
                if q.booked:
                    log(f"  ✕ quote {q.title} {q.oid}: cancel confirmed, {q.booked:g} had filled")
            continue
        every = STREAM_POLL if st is not None and feed.alive() else cfg.maker_poll
        if (st is None or q.unbooked(st) <= 1e-9) and (force or now - q.polled_at >= every):
            q.polled_at = now
            st = _read(brokers, q)
            if st.get("error"):
                log(f"! maker: order read failed: {st['error']}")
                continue
        if not st:
            continue
        if q.unbooked(st) > 1e-9:
            if _filled(db, cfg, brokers, q, st, log) == "halt":
                return "halt"
        elif st.get("done"):                    # cancelled / expired by the exchange without a (new) fill
            _drop(db, q, f"closed by the exchange: {st.get('state')}")
            log(f"  ✕ quote {q.title} closed by the exchange ({st.get('state')})")
    return None


def _fill_since(q: Quote, st: dict) -> Fill | None:
    """The order's fills that no trade row holds yet, as one Fill (None when there are none)."""
    qty = round(q.unbooked(st), 6)
    if qty <= 1e-9:
        return None
    avg = ((st.get("cum") or 0.0) * (st.get("avg") or q.price) - q.booked_notional) / qty
    if not 0.0 < avg < 1.0:                     # an average that does not add up: our own price (a maker fills at it)
        avg = q.price
    fee = max(0.0, (st.get("fee") or 0.0) - q.booked_fee)
    return Fill("Polymarket", "buy", q.pm.label, qty, avg, fee, q.oid, "filled",
                raw={"state": st.get("state"), "maker": True})


def _filled(db, cfg, brokers, q: Quote, st: dict, log, cancelled: bool = False):
    """Hedge on Kalshi first (fast), then make sure the rest of the quote is gone, then the normal hedge path.
    Only the part of the order no trade row holds yet is hedged and booked (Quote.booked), so a fill seen twice --
    stream and REST, or again after a cancel that was not confirmed -- is never hedged twice."""
    from . import trader
    s = STATE
    if q.oid not in s.quotes:
        return None
    f1 = _fill_since(q, st)
    if f1 is None:
        return None
    s.stats["fills"] += 1
    mode = "live" if cfg.live else "dry"
    detected = time.time()
    pm, k = q.pm, q.k
    # Kalshi's book from the live feed right now (no network), before the hedge: the quote's own copy of k.book is
    # only as fresh as the last check of its game (11-05 #53 logged 0.32 cached while Kalshi was already at 0.51)
    kb = trader.live_book(k)
    k_live = ({"asks": [[p_, sz] for p_, sz in kb.asks[:3]], "age": round(detected - kb.fetched_at, 3)}
              if kb is not None else None)
    cost1 = (f1.notional + f1.fee) / f1.qty
    limit2 = max_price_within(q.guaranteed - cost1 - cfg.leg2_min_edge + cfg.hedge_max_loss, k)
    first_q = math.floor(f1.qty + 1e-9)
    f2 = None
    if first_q >= 1:
        try:
            f2 = brokers["Kalshi"].buy(k, first_q, limit2, notional_hint=first_q * limit2)
        except Exception as e:                  # a broker that raises must not leave the fill unbooked
            f2 = Fill("Kalshi", "buy", k.label, status="error", error=f"{type(e).__name__}: {e}")
    t_hedge = time.time()
    done = _confirmed(st)
    if not cancelled and not done:
        q.cancel_at = time.time()
        fin = _cancel(brokers, pm, q.oid, wait=0.0)        # sent and read once: poll() confirms it (see pull)
        more = None if fin.get("error") else _fill_since(q, fin)
        if more is not None and more.qty > f1.qty + 1e-9:           # more filled before the cancel landed
            f1 = more
        done = _confirmed(fin)
        st = fin
    # Booked from here on, whatever happens below: the next poll must never hedge these contracts again.
    q.booked += f1.qty
    q.booked_notional += f1.notional
    q.booked_fee += f1.fee
    if done:
        s.quotes.pop(q.oid, None)
    else:
        # The rest of the quote may still be resting. Until 11-05 it was dropped here with a log line: a later fill
        # had no hedge, no ledger row and no cap. Now it stays tracked: poll() cancels again and books what fills.
        q.dead, q.polled_at = True, time.time()
        log(f"  ! quote {q.oid} ({q.title}) cancel after its fill not confirmed yet: {st.get('error') or st.get('state')} "
            "-- still tracked until it is")
    a_is_pm = q.key.split("|")[0] == pm.id
    a, b = (pm, k) if a_is_pm else (k, pm)
    guaranteed, middle = engine.guaranteed_payout(a, b)
    plan_fills_pm, plan_fills_k = [(q.price, q.qty)], [(q.k_ask, q.qty)]
    plan = engine.Sizing(q.qty, plan_fills_pm if a_is_pm else plan_fills_k, plan_fills_k if a_is_pm else plan_fills_pm,
                         q.qty * (q.price + q.k_ask), q.qty * k.fee.per_contract(q.k_ask), q.qty * q.guaranteed)
    opp = engine.Opportunity(q.game, a, b, guaranteed, middle, plan)
    probe = {"maker": True, "quote": {"price": q.price, "qty": q.qty, "k_ask": q.k_ask, "age_s": round(detected - q.posted_at, 2)},
             "k_live_at_fill": k_live,
             "k_cached_at_fill": {"asks": [[p, sz] for p, sz in k.book.asks[:3]],
                                  "age": round(detected - k.book.fetched_at, 3)},
             "hedge1_ms": round((t_hedge - detected) * 1000, 1), "changed": None}
    fills = [f1] + ([f2] if f2 is not None else [])
    live_ask = k_live["asks"][0][0] if k_live and k_live["asks"] else None
    log(f"  ◉ QUOTE FILLED {q.title}: {f1.qty:g} {pm.label} @ {f1.avg_price:.3f} on Polymarket; hedging {k.label} "
        f"on Kalshi (limit {limit2:.3f}; Kalshi ask {q.k_ask:.2f} at the quote, {live_ask} live at the fill)")
    try:
        row = trader._after_leg1(db, mode, opp, plan, detected, fills, pm, k, f1, f2, "a" if a_is_pm else "b",
                                 f2 is not None, brokers, cfg, log, probe)
    except Exception as e:
        # The Polymarket leg (and maybe a Kalshi hedge) are real positions: book them from the fills so far, as
        # trader.execute does, so settle(), the caps, the cooldown and the halt streak all see them.
        held_pm, held_k = trader._net_held(fills, pm), trader._net_held(fills, k)
        q_a, q_b = (held_pm, held_k) if a_is_pm else (held_k, held_pm)
        row = trader._record(db, mode, opp, plan, detected, fills, "error", q_a, q_b,
                             note=f"{type(e).__name__}: {e} -- maker fill recorded from the fills so far; verify on "
                                  "the venues", log=log, probe=probe)
    note = f"filled -> trade #{row.get('id')} {row.get('outcome')}"
    if done:
        _close_row(db, q.oid, q.booked, note)
    else:
        _mark_row(db, q.oid, q.booked, note + "; cancel not confirmed, still tracked")
    trader.spend(brokers, opp, json.loads(row["fills"]))
    if trader.recent_failures(db, mode, cfg.max_failures) >= cfg.max_failures:
        return "halt"
    return None


def shutdown(db, cfg, brokers, log) -> None:
    """Never leave a quote resting when the bot stops. A cancel that is not confirmed is sent again, up to STOP_TRIES
    passes: 11-05 01:08Z one pass left a quote 'may still be resting', and after a HALT (a clean exit systemd does not
    restart) nothing would come back to cancel it."""
    if STATE.suspects:                          # a post that errored moments ago: one last look before leaving
        STATE.sweep_at = 0.0
        try:
            _sweep(db, cfg, brokers, log, time.time())
        except Exception as e:
            log(f"! maker: could not look for a failed post on exit: {e}")
        for sp in STATE.suspects:
            log(f"! maker: a quote on {sp['cand'].game.title} ({sp['cand'].pm.label} @ {sp['cand'].price:.2f}) may "
                "have been placed through an error reply -- check Polymarket for an open order")
    for _ in range(STOP_TRIES):
        for q in list(STATE.quotes.values()):
            try:
                pull(db, cfg, brokers, q, "the bot is stopping", log, wait=None)      # here the cancel IS waited for
            except Exception as e:
                log(f"! maker: could not cancel the resting quote {q.oid} on exit: {e} -- CANCEL IT ON POLYMARKET")
        if not STATE.quotes:
            break
    for q in STATE.quotes.values():
        log(f"! maker: quote {q.oid} ({q.title}) may still be resting on Polymarket -- check and cancel it")


def recover(db, cfg, brokers, log) -> int:
    """At start: cancel quotes a previous run left open. One that filled meanwhile was never hedged: book it
    as an `error` row (so it settles, counts against the caps and the halt streak, and is flagged for a review).
    Only what the row does not already show as booked (quotes.filled) is booked here."""
    from .fees import FeeModel
    mode = "live" if cfg.live else "dry"
    n = 0
    for r in db.execute("SELECT * FROM quotes WHERE closed_at IS NULL AND mode=?", (mode,)).fetchall():
        s = json.loads(r["pm"])
        pm = Contract("Polymarket", "", "", s["side"], s["line"], s["kind"], s["label"], s["how"],
                      FeeModel("Polymarket", s["fee_rate"]), s["ref"])
        st = _cancel(brokers, pm, r["order_id"])
        for _ in range(STOP_TRIES - 1):         # accepted but not showing yet: ask again before trusting "cancelled"
            if st.get("error") or st.get("done"):
                break
            st = _cancel(brokers, pm, r["order_id"])
        if not _confirmed(st):
            log(f"! maker: left-over quote {r['order_id']} ({r['game']}) could not be cancelled: "
                f"{st.get('error') or st.get('state')} -- CANCEL IT ON POLYMARKET")
            continue
        total = st.get("cum") or 0.0
        cum = max(0.0, total - (r["filled"] or 0.0))        # what no trade row holds yet
        _close_row(db, r["order_id"], total, "cancelled at start" + (f"; had filled {cum:g} while down" if cum else ""))
        n += 1
        if cum < 1e-9:
            log(f"  maker: cancelled a quote left open by the last run ({r['game']} {s['label']})")
            continue
        cash = cum * (st.get("avg") or r["price"]) + (st.get("fee") or 0.0)
        a_is_pm = r["opp_key"].split("|")[0].startswith("Polymarket")
        vals = dict(mode=mode, detected_at=time.time(), done_at=time.time(), game=r["game"], kickoff=r["kickoff"],
                    opp_key=r["opp_key"], leg_a=r["pm"] if a_is_pm else r["k"], leg_b=r["k"] if a_is_pm else r["pm"],
                    plan=json.dumps({"qty": r["qty"], "maker": True}), fills=json.dumps([]), outcome="error",
                    qty_a=cum if a_is_pm else 0.0, qty_b=0.0 if a_is_pm else cum, cash_out=cash, status="open",
                    note=f"maker quote {r['order_id']} filled {cum:g} @ {st.get('avg') or r['price']:.3f} while the bot "
                         "was down; NOT hedged -- held to settlement, verify on Polymarket")
        db.execute(f"INSERT INTO trades ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
        db.commit()
        log(f"  ✖ maker: {vals['note']}")
    _unknown_open(db, mode, brokers, log)
    return n


def _unknown_open(db, mode: str, brokers, log) -> int:
    """At start nothing of ours should rest. An order open on the account that the ledger has never seen is either a
    quote that was placed through an error reply right before a crash, or someone else's (the account is shared):
    it is reported, never cancelled here."""
    fn = getattr(brokers["Polymarket"], "open_orders", None)
    if fn is None:
        return 0
    try:
        known = {r[0] for r in db.execute("SELECT order_id FROM quotes WHERE mode=?", (mode,))}
        extra = [o for o in fn() or [] if o.get("id") and o["id"] not in known]
    except Exception as e:
        log(f"! maker: open-orders read failed at start: {e}")
        return 0
    for o in extra[:10]:
        log(f"! maker: order {o['id']} is OPEN on Polymarket and not in this ledger: {o.get('marketSlug')} "
            f"{str(o.get('intent', '')).replace('ORDER_INTENT_', '')} @ {(o.get('price') or {}).get('value')} "
            f"x{o.get('quantity')} -- not touched (the account is shared); cancel it there if it is not yours")
    return len(extra)


def stats_text() -> str:
    s = STATE.stats
    rest = "; ".join(f"{q.title} {q.pm.label} @ {q.price:.2f} x{q.qty:g}" + (" (CANCEL PENDING)" if q.dead else "")
                     for q in STATE.quotes.values() if q.pm)
    s["quotable"] = max(s["quotable"], sum(STATE.quotable.values()))
    out = (f"  maker, last 60s: {s['quotable']} pairs quotable · {s['posted']} posted · {s['modified']} re-priced by "
           f"modify · {s['repriced']} swapped · {s['pulled']} pulled · {s['fills']} filled · "
           f"resting {len(STATE.quotes)}" + (f": {rest}" if rest else "")
           + _gap_text()
           + (" · modify OFF (cancel + post)" if not STATE.modify_ok else "")
           + (f" · PAUSED: {STATE.paused}" if STATE.paused else ""))
    STATE.stats = {k: 0 for k in s}
    STATE.guard_gap, STATE.gaps = 0.0, []
    STATE.net = {k: 0.0 for k in STATE.net}
    return out


def _gap_text() -> str:
    """How far apart the live checks of the resting quotes were (the pull latency the bot owns), and how long the
    trading thread spent inside Polymarket calls."""
    g = sorted(STATE.gaps)
    if not g:
        return ""
    n = STATE.net
    return (f" · quotes checked live every {g[len(g) // 2] * 1000:.0f} ms (median), {g[int(len(g) * 0.95)] * 1000:.0f} ms "
            f"(95th), at most {g[-1] * 1000:.0f} ms apart · Polymarket calls: cancels {n['cancel']:.1f}s, posts "
            f"{n['post']:.1f}s, order reads {n['read']:.1f}s")
