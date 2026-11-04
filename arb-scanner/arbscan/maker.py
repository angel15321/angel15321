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
            s -= q.qty
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

    @property
    def score(self) -> float:
        return self.edge * self.qty


def price_pair(cfg, g, pm: Contract, k: Contract, budget: float, own) -> Cand | None:
    """The quote for one pair, or None when it cannot be quoted at the top of the Polymarket book.
    `own` = {pm contract id: Quote} (or one Quote / None)."""
    if isinstance(own, Quote):
        own = {own.pm_id: own}
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
    ceiling = floor_to(guaranteed - unit_k - cfg.maker_edge + 1e-9, tick)
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
    if edge < cfg.maker_edge - 1e-9:
        return None
    return Cand(f"{a.id}|{b.id}", g, pm, k, pm is a, guaranteed, middle, price, qty, ka, edge)


def candidates(cfg, games, budget: float, own) -> list[Cand]:
    if isinstance(own, Quote):
        own = {own.pm_id: own}
    out = []
    for g in games:
        pms = [c for c in g.contracts if c.venue == "Polymarket"]
        ks = [c for c in g.contracts if c.venue == "Kalshi"]
        for pm in pms:
            for k in ks:
                if k.axis != pm.axis or k.side == pm.side:
                    continue
                c = price_pair(cfg, g, pm, k, budget, own)
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
        cash = trader.balances(brokers)
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
    """Not started by the schedule, and Polymarket does not flag it live either (a belt to the start-time braces)."""
    return not g.started(now_dt) and not any(c.ref.get("live") for c in g.contracts if c.venue == "Polymarket")


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
        if q.pm is not None and (q.pm_id in by_id or full):
            why = _unsafe(cfg, q, q.pm_id in by_id, now, now_dt)
            if why and pull(db, cfg, brokers, q, why, log) == "halt":
                return "halt"
    if s.paused:
        return None
    ok = [g for g in games if _pregame(g, now_dt) or cfg.maker_in_play]
    if not ok or not _feeds_ok():
        return None
    from . import trader
    room, pm_pot, shards, k_all = _room(db, cfg, brokers, log)
    slots = max(1, cfg.maker_quotes)
    per = min(cfg.max_per_trade, room / slots, pm_pot / slots)
    if per < 2:
        return None
    own = {q.pm_id: q for q in s.quotes.values()}
    cands = [c for c in candidates(cfg, ok, per, own)
             if now - c.k.book.fetched_at <= cfg.max_book_age
             and not trader.recently_taken(db, c.key, cfg.cooldown_min)]
    if full:
        s.stats["quotable"] = max(s.stats["quotable"], len(cands))
    by_key = {}
    for c in cands:
        by_key.setdefault(c.key, c)

    # 1. our quotes in view: keep, re-price (same pair), or pull
    for q in list(s.quotes.values()):
        if q.game_key not in in_view or now - q.priced_at < cfg.maker_reprice:
            continue
        cur = by_key.get(q.key)
        if cur is None:
            if pull(db, cfg, brokers, q, "no longer quotable at the top of the book", log) == "halt":
                return "halt"
        elif abs(cur.price - q.price) > 1e-6:
            if reprice(db, cfg, brokers, q, cur, log) == "halt":
                return "halt"

    # 2. room shrank (a fill booked spend): the newest quotes go until the rest fit. 15% slack so a re-price a
    #    tick or two up does not pull-and-re-post a quote every second (box paper run 11-04 01:46Z)
    committed = sum(q.outlay() for q in s.quotes.values())
    for q in sorted(s.quotes.values(), key=lambda x: -x.posted_at):
        if committed <= room * 1.15 + 0.01:
            break
        committed -= q.outlay()
        if pull(db, cfg, brokers, q, f"day room ${room:.2f} no longer covers every resting quote", log) == "halt":
            return "halt"

    # 3. an empty slot takes the best pair of a game we are not quoting yet (one new post per step)
    if now - s.last_post < cfg.maker_reprice / max(1, slots):
        return None
    games_used = {q.game_key for q in s.quotes.values()}
    ks_used = {q.k_id for q in s.quotes.values()}
    free = [c for c in cands if c.game.key not in games_used and c.k.id not in ks_used and c.key not in
            {q.key for q in s.quotes.values()}]
    if not free:
        return None
    best = free[0]
    if len(s.quotes) >= slots:
        # full: swap out the weakest quote only for a clearly better pair, and only one older than maker_reprice
        def score(q):
            c = by_key.get(q.key)
            return c.score if c is not None else 0.0
        weak = min(s.quotes.values(), key=score)
        if weak.game_key not in in_view or best.score <= score(weak) * 1.5 or now - weak.priced_at < cfg.maker_reprice:
            return None
        s.stats["repriced"] += 1
        if pull(db, cfg, brokers, weak, f"swapped for {best.game.title} {best.pm.label} @ {best.price:.2f}", log) == "halt":
            return "halt"
        if weak.oid in s.quotes:
            return None
    # a new quote is sized from the room the resting ones leave, split over the free slots
    committed = sum(q.outlay() for q in s.quotes.values())
    free_slots = max(1, slots - len(s.quotes))
    per_new = min(cfg.max_per_trade, (room - committed) / free_slots, pm_pot / slots)
    if per_new < 2:
        return None
    best = price_pair(cfg, best.game, best.pm, best.k, per_new, own)
    if best is None:
        return None
    k_pot = _k_pot(best.k, shards, k_all) - sum(q.qty * (q.k_ask + q.k.fee.per_contract(q.k_ask))
                                                for q in s.quotes.values() if q.k is not None and
                                                q.k.ref.get("shard") == best.k.ref.get("shard"))
    if k_pot < best.qty * (best.k_ask + best.k.fee.per_contract(best.k_ask)) - 1e-9:
        return None
    return post(db, cfg, brokers, best, log)


def _unsafe(cfg, q: Quote, seen: bool, now: float, now_dt) -> str:
    if not _feeds_ok():
        return "a book feed is down"
    if not seen:
        return "its market left the scan"
    if q.game is not None and not _pregame(q.game, now_dt) and not cfg.maker_in_play:
        return "the game started"
    if now - q.posted_at > cfg.maker_ttl:
        return f"older than {cfg.maker_ttl:g}s (refresh)"
    k = q.k
    if k is None or not k.book.asks:
        return "Kalshi hedge side is empty"
    if now - k.book.fetched_at > cfg.max_book_age:
        return "Kalshi book went stale"
    ka, size = k.book.asks[0]
    unit_k = ka + k.fee.per_contract(ka)
    if q.guaranteed - q.price - unit_k < cfg.maker_edge / 2 - 1e-9:
        return f"Kalshi ask moved {q.k_ask:.2f} -> {ka:.2f}"
    if size + 1e-9 < q.qty:
        return f"Kalshi ask thinned to {size:g} < {q.qty:g}"
    return ""


def post(db, cfg, brokers, c: Cand, log):
    s = STATE
    mode = "live" if cfg.live else "dry"
    s.last_post = time.time()
    f = brokers["Polymarket"].post(c.pm, c.qty, c.price)
    if f.status != "resting" or not f.order_id:
        s.rejects += 1
        log(f"  ! quote not placed {c.game.title}: {c.pm.label} @ {c.price:.2f} -- {f.status}: {f.error}")
        if s.rejects >= cfg.maker_max_rejects:
            s.paused = f"Polymarket refused {s.rejects} quotes in a row (last: {f.status} {f.error})"
            log(f"  ! MAKER PAUSED: {s.paused}. Fix, then restart.")
        if f.order_id:                         # an id with no resting state: make sure it is gone
            _cancel(brokers, c.pm, f.order_id)
        return None
    s.rejects = 0
    now = time.time()
    db.execute("INSERT OR REPLACE INTO quotes (order_id, mode, posted_at, game, kickoff, opp_key, pm, k, price, qty, "
               "k_ask, guaranteed, filled) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)",
               (f.order_id, mode, now, c.game.title, c.game.start.isoformat() if c.game.start else None, c.key,
                _snap(c.pm), _snap(c.k), c.price, c.qty, c.k_ask, c.guaranteed))
    db.commit()
    s.quotes[f.order_id] = Quote(f.order_id, c.key, c.pm.id, c.k.id, c.game.key, c.game.title, c.price, c.qty,
                                 c.k_ask, c.guaranteed, now, now, now, c.pm, c.k, c.game)
    s.stats["posted"] += 1
    log(f"  ◷ QUOTE {c.game.title}: bid {c.pm.label} @ {c.price:.2f} x{c.qty:g} on Polymarket; hedge "
        f"{c.k.label} @ {c.k_ask:.2f} on Kalshi; edge {c.edge * 100:.1f} c/pair after fees "
        f"[{len(s.quotes)}/{max(1, cfg.maker_quotes)} resting]")
    return None


def reprice(db, cfg, brokers, q: Quote, c: Cand, log):
    """New price for the same pair: modify in place (quantity kept); cancel + post when modify is not usable."""
    s = STATE
    if not s.modify_ok:
        s.stats["repriced"] += 1
        r = pull(db, cfg, brokers, q, f"re-price to {c.price:.2f}", log)
        if r == "halt" or q.oid in s.quotes:
            return r
        return post(db, cfg, brokers, c, log)
    b = brokers["Polymarket"]
    ok, err = b.modify(q.pm, q.oid, c.price, q.qty)
    now = time.time()
    if ok and s.modify_checks < MODIFY_CHECKS:
        # read the order back: the reply is empty on success, so the first few are confirmed from the order itself
        s.modify_checks += 1
        try:
            st = b.order_state(q.pm, q.oid)
            if st.get("cum"):
                return _filled(db, cfg, brokers, q, st, log)
            if st.get("done") or abs((st.get("price") or c.price) - c.price) > 1e-6:
                ok, err = False, f"order reads {st.get('state')} @ {st.get('price')} after the modify"
        except Exception as e:
            ok, err = False, f"read-back failed: {e}"
    if not ok:
        s.modify_ok = False
        log(f"  ! modify did not take ({q.title} {q.oid}): {err} -- re-pricing by cancel + post from now on")
        return reprice(db, cfg, brokers, q, c, log)
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


def _cancel(brokers, c: Contract, oid: str) -> dict:
    try:
        return brokers["Polymarket"].cancel(c, oid) or {}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def pull(db, cfg, brokers, q: Quote, why: str, log):
    """Cancel one resting quote; a fill that happened meanwhile is hedged."""
    s = STATE
    if q.oid not in s.quotes:
        return None
    st = _cancel(brokers, q.pm, q.oid)
    if st.get("error") or not st.get("done"):
        log(f"  ! quote cancel not confirmed ({q.title} {q.oid}): {st.get('error') or st.get('state')}; retrying")
        if not st.get("cum"):
            return None                         # still ours, still resting: the next step tries again
    if st.get("cum"):
        return _filled(db, cfg, brokers, q, st, log, cancelled=True)
    s.quotes.pop(q.oid, None)
    s.stats["pulled"] += 1
    _close_row(db, q.oid, 0.0, f"pulled: {why}")
    log(f"  ✕ pulled {q.title} {q.pm.label} @ {q.price:.2f}: {why}")
    return None


def poll(db, cfg, brokers, log, force: bool = False):
    """Fill check per quote: the private order stream when it has the order, else a REST read every maker_poll s."""
    s = STATE
    now = time.time()
    feed = polymarket._orders
    for q in list(s.quotes.values()):
        st = feed.state(q.oid, q.pm) if feed is not None else None
        if (st is None or not st.get("cum")) and (force or now - q.polled_at >= cfg.maker_poll):
            q.polled_at = now
            try:
                st = brokers["Polymarket"].order_state(q.pm, q.oid)
            except Exception as e:
                log(f"! maker: order read failed: {e}")
                continue
        if not st:
            continue
        if st.get("cum"):
            if _filled(db, cfg, brokers, q, st, log) == "halt":
                return "halt"
        elif st.get("done"):                    # cancelled / expired by the exchange without a fill
            s.quotes.pop(q.oid, None)
            _close_row(db, q.oid, 0.0, f"closed by the exchange: {st.get('state')}")
            log(f"  ✕ quote {q.title} closed by the exchange ({st.get('state')})")
    return None


def _filled(db, cfg, brokers, q: Quote, st: dict, log, cancelled: bool = False):
    """Hedge on Kalshi first (fast), then make sure the rest of the quote is gone, then the normal hedge path."""
    from . import trader
    s = STATE
    if q.oid not in s.quotes:
        return None
    s.stats["fills"] += 1
    mode = "live" if cfg.live else "dry"
    detected = time.time()
    pm, k = q.pm, q.k
    f1 = Fill("Polymarket", "buy", pm.label, st["cum"], st["avg"] or q.price, st.get("fee") or 0.0, q.oid, "filled",
              raw={"state": st.get("state"), "maker": True})
    cost1 = (f1.notional + f1.fee) / f1.qty
    limit2 = max_price_within(q.guaranteed - cost1 - cfg.leg2_min_edge + cfg.hedge_max_loss, k)
    first_q = math.floor(f1.qty + 1e-9)
    f2 = brokers["Kalshi"].buy(k, first_q, limit2, notional_hint=first_q * limit2) if first_q >= 1 else None
    t_hedge = time.time()
    if not cancelled and not st.get("done"):
        fin = _cancel(brokers, pm, q.oid)
        if fin.get("cum") and fin["cum"] > f1.qty + 1e-9:          # more filled before the cancel landed
            f1 = Fill("Polymarket", "buy", pm.label, fin["cum"], fin["avg"] or q.price, fin.get("fee") or 0.0,
                      q.oid, "filled", raw={"state": fin.get("state"), "maker": True})
        if fin.get("error") or not fin.get("done"):
            log(f"  ! quote {q.oid} cancel after fill not confirmed: {fin.get('error') or fin.get('state')} -- "
                "check Polymarket for a resting order")
    s.quotes.pop(q.oid, None)
    a_is_pm = q.key.split("|")[0] == pm.id
    a, b = (pm, k) if a_is_pm else (k, pm)
    guaranteed, middle = engine.guaranteed_payout(a, b)
    plan_fills_pm, plan_fills_k = [(q.price, q.qty)], [(q.k_ask, q.qty)]
    plan = engine.Sizing(q.qty, plan_fills_pm if a_is_pm else plan_fills_k, plan_fills_k if a_is_pm else plan_fills_pm,
                         q.qty * (q.price + q.k_ask), q.qty * k.fee.per_contract(q.k_ask), q.qty * q.guaranteed)
    opp = engine.Opportunity(q.game, a, b, guaranteed, middle, plan)
    probe = {"maker": True, "quote": {"price": q.price, "qty": q.qty, "k_ask": q.k_ask, "age_s": round(detected - q.posted_at, 2)},
             "k_book_at_fill": [[p, sz] for p, sz in k.book.asks[:3]], "hedge1_ms": round((t_hedge - detected) * 1000, 1),
             "changed": None}
    fills = [f1] + ([f2] if f2 is not None else [])
    log(f"  ◉ QUOTE FILLED {q.title}: {f1.qty:g} {pm.label} @ {f1.avg_price:.3f} on Polymarket; hedging {k.label} "
        f"on Kalshi (limit {limit2:.3f})")
    row = trader._after_leg1(db, mode, opp, plan, detected, fills, pm, k, f1, f2, "a" if a_is_pm else "b",
                             f2 is not None, brokers, cfg, log, probe)
    _close_row(db, q.oid, f1.qty, f"filled -> trade #{row.get('id')} {row.get('outcome')}")
    trader.spend(brokers, opp, json.loads(row["fills"]))
    if trader.recent_failures(db, mode, cfg.max_failures) >= cfg.max_failures:
        return "halt"
    return None


def shutdown(db, cfg, brokers, log) -> None:
    """Never leave a quote resting when the bot stops."""
    for q in list(STATE.quotes.values()):
        try:
            pull(db, cfg, brokers, q, "the bot is stopping", log)
        except Exception as e:
            log(f"! maker: could not cancel the resting quote {q.oid} on exit: {e} -- CANCEL IT ON POLYMARKET")
    for q in STATE.quotes.values():
        log(f"! maker: quote {q.oid} ({q.title}) may still be resting on Polymarket -- check and cancel it")


def recover(db, cfg, brokers, log) -> int:
    """At start: cancel quotes a previous run left open. One that filled meanwhile was never hedged: book it
    as an `error` row (so it settles, counts against the caps and halts the bot for a review)."""
    from .fees import FeeModel
    mode = "live" if cfg.live else "dry"
    n = 0
    for r in db.execute("SELECT * FROM quotes WHERE closed_at IS NULL AND mode=?", (mode,)).fetchall():
        s = json.loads(r["pm"])
        pm = Contract("Polymarket", "", "", s["side"], s["line"], s["kind"], s["label"], s["how"],
                      FeeModel("Polymarket", s["fee_rate"]), s["ref"])
        st = _cancel(brokers, pm, r["order_id"])
        if st.get("error"):
            log(f"! maker: left-over quote {r['order_id']} ({r['game']}) could not be cancelled: {st['error']} -- "
                "CANCEL IT ON POLYMARKET")
            continue
        cum = st.get("cum") or 0.0
        _close_row(db, r["order_id"], cum, "cancelled at start" + (f"; had filled {cum:g} while down" if cum else ""))
        n += 1
        if not cum:
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
    return n


def stats_text() -> str:
    s = STATE.stats
    rest = "; ".join(f"{q.title} {q.pm.label} @ {q.price:.2f} x{q.qty:g}" for q in STATE.quotes.values() if q.pm)
    out = (f"  maker, last 60s: {s['quotable']} pairs quotable · {s['posted']} posted · {s['modified']} re-priced by "
           f"modify · {s['repriced']} swapped · {s['pulled']} pulled · {s['fills']} filled · "
           f"resting {len(STATE.quotes)}" + (f": {rest}" if rest else "")
           + (" · modify OFF (cancel + post)" if not STATE.modify_ok else "")
           + (f" · PAUSED: {STATE.paused}" if STATE.paused else ""))
    STATE.stats = {k: 0 for k in s}
    return out
