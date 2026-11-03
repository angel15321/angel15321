"""The auto-arb loop: scan → pick arbs → execute both legs → log → settle.

Execution of one arb
--------------------
1. Size it against live depth, capped by --max-per-trade and the cash on
   each venue (and the daily / open-exposure limits).
2. Leg 1 = the leg most likely to vanish. In a game in progress that is the
   STALE venue: after a score one venue reprices first and the other's old
   quotes get pulled, so the leg whose price moved least since the last scan
   goes first (--leg1 auto). Otherwise the Polymarket leg goes first, and for
   a same-venue pair the thinner leg. Leg 1 is an immediate-or-cancel buy
   whose limit keeps the pair profitable against leg 2's current price.
3. Leg 2 is bought for exactly what leg 1 filled, as immediate-or-cancel with
   a limit at the break-even price for the cost already paid. Retried
   --hedge-attempts times while the remainder is still hedgeable.
4. Anything still unhedged is either sold back (--on-fail unwind, default)
   or kept (--on-fail hold).
5. Every order and outcome goes to the SQLite ledger; settled games are
   priced from each venue's official result.

In dry-run mode the same steps run against freshly fetched live order books
with paper brokers, so you can see fill rates and slippage before going live.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import sqlite3
import time
from dataclasses import dataclass

from . import engine, polymarket, scanner
from .brokers import Fill, floor_to, max_price_within
from .model import Contract

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY,
  mode TEXT,                      -- live | dry
  detected_at REAL, done_at REAL, game TEXT, kickoff TEXT, opp_key TEXT,
  leg_a TEXT, leg_b TEXT,         -- contract snapshots (JSON)
  plan TEXT,                      -- planned qty / prices / ROI (JSON)
  fills TEXT,                     -- every order sent (JSON list)
  outcome TEXT,                   -- hedged | partial | unwound | exposed | missed | error
  qty_a REAL, qty_b REAL,         -- contracts held after execution
  cash_out REAL,                  -- net cash spent (fees included, unwind proceeds netted)
  locked_profit REAL,             -- for fully hedged trades
  status TEXT,                    -- open | settled | closed
  payout REAL, pnl REAL, settled_at REAL, note TEXT,
  probe TEXT,                     -- leg-1 timing + books seen / right after (JSON)
  acked_at REAL                   -- operator acknowledged this failure (`arbscan ack`): ends a halt streak
);
CREATE INDEX IF NOT EXISTS trades_key ON trades(opp_key, detected_at);
"""


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(trades)")}
    for col, typ in (("probe", "TEXT"), ("acked_at", "REAL")):       # ledgers from before these columns
        if col not in cols:
            db.execute(f"ALTER TABLE trades ADD COLUMN {col} {typ}")
            db.commit()
    return db


def ack_failures(db, mode: str = "live") -> int:
    """Mark every unacknowledged failed trade as reviewed, so the halt streak restarts from zero.
    The positions themselves are untouched (held ones still settle and book their P&L)."""
    n = db.execute("UPDATE trades SET acked_at=? WHERE mode=? AND acked_at IS NULL AND outcome IN "
                   "('unwound','exposed','partial','error')", (time.time(), mode)).rowcount
    db.commit()
    return n


@dataclass
class Config:
    live: bool = False
    max_per_trade: float = 100.0      # $ outlay cap per arb (both legs)
    max_daily_spend: float = 500.0    # $ across all arbs per UTC day
    max_open: float = 1000.0          # $ tied up in unsettled trades
    min_roi: float = 0.005            # 0.5% after fees
    min_profit: float = 0.25          # $ locked per arb
    slippage: float = 0.01            # extra room on leg-1 limit (still capped at break-even)
    leg2_min_edge: float = 0.0        # $ per pair leg 2 must still leave (0 = accept break-even)
    hedge_attempts: int = 3
    on_fail: str = "unwind"           # unwind | hold
    unwind_slippage: float = 0.03     # accept up to 3c below best bid when unwinding
    unwind_max_loss: float = 0.0      # $/contract a sell-back may lose beyond the fees; a worse bid -> hold to settlement
    cooldown_min: float = 10.0
    include_live_games: bool = False  # in-play arbs move fastest; off by default
    in_play_only: bool = False        # only games already in progress (implies include_live_games)
    leg1: str = "auto"                # auto (stale venue first in-play) | polymarket | kalshi
    cross_venue_only: bool = False
    max_failures: int = 2             # halt after this many unhedged/unwound trades in a row
    kill_file: str = "STOP"
    sports: tuple[str, ...] = ("nfl",)  # nfl | nba | tennis
    legs: str = "sequential"          # sequential (stale leg first, then hedge) | parallel (both at once; a vanished
                                      # quote then leaves the OTHER leg filled -- 11-03 every miss was a vanished quote)
    max_book_age: float = 1.0         # s: an arb priced from an older Kalshi (REST) book is skipped


# ---------------------------------------------------------------- helpers

def _snap(c: Contract) -> dict:
    return {"venue": c.venue, "label": c.label, "how": c.how, "ref": c.ref, "kind": c.kind,
            "side": c.side, "line": c.line, "tie_half": c.tie_half, "fee_rate": c.fee.rate}


def _today_start() -> float:
    now = dt.datetime.now(dt.timezone.utc)
    return dt.datetime(now.year, now.month, now.day, tzinfo=dt.timezone.utc).timestamp()


def spent_today(db, mode: str) -> float:
    r = db.execute("SELECT COALESCE(SUM(MAX(cash_out,0)),0) FROM trades WHERE mode=? AND detected_at>=?",
                   (mode, _today_start())).fetchone()
    return float(r[0])


def open_exposure(db, mode: str) -> float:
    r = db.execute("SELECT COALESCE(SUM(MAX(cash_out,0)),0) FROM trades WHERE mode=? AND status='open'",
                   (mode,)).fetchone()
    return float(r[0])


def recent_failures(db, mode: str, n: int) -> int:
    rows = db.execute("SELECT outcome, acked_at FROM trades WHERE mode=? AND outcome!='missed' ORDER BY id DESC LIMIT ?",
                      (mode, n)).fetchall()
    streak = 0
    for r in rows:
        if r["outcome"] in ("unwound", "exposed", "partial", "error") and r["acked_at"] is None:
            streak += 1
        else:
            break
    return streak


def mid(c: Contract) -> float | None:
    if c.book.best_bid is None or c.book.best_ask is None:
        return None
    return (c.book.best_bid + c.book.best_ask) / 2


def first_leg(opp: engine.Opportunity, cfg: Config, moves: dict[str, float] | None = None) -> str:
    """'a' or 'b': which leg to buy first (see module docstring)."""
    a, b = opp.a, opp.b
    if a.venue == b.venue:
        top = {k: c.book.asks[0][1] if c.book.asks else 0 for k, c in (("a", a), ("b", b))}
        return "a" if top["a"] <= top["b"] else "b"
    if cfg.leg1 in ("polymarket", "kalshi"):
        want = "Polymarket" if cfg.leg1 == "polymarket" else "Kalshi"
        return "a" if a.venue == want else "b"
    live = opp.game.started(dt.datetime.now(dt.timezone.utc))
    ma, mb = (moves or {}).get(a.id), (moves or {}).get(b.id)
    if live and ma is not None and mb is not None and abs(ma - mb) >= 0.005:
        return "a" if ma < mb else "b"
    return "a" if a.venue == "Polymarket" else "b"


def _top(book, n: int = 3) -> list:
    return [[p, s] for p, s in book.asks[:n]]


def book_now(c: Contract) -> dict:
    """Leg's ask book re-read from the source right now (feed for Polymarket, REST for Kalshi)."""
    try:
        if c.venue == "Polymarket":
            books = polymarket.fetch_books([c.ref["slug"]]).get(c.ref["slug"])
            book = books and books[0 if c.ref["side"] == "long" else 1]
        else:
            from . import kalshi
            books = kalshi.fetch_books([c.ref["ticker"]]).get(c.ref["ticker"])
            book = books and books[0 if c.ref["side"] == "yes" else 1]
        if not book:
            return {"asks": None}
        return {"asks": _top(book), "age": round(time.time() - book.fetched_at, 3)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def probe_text(p: dict) -> str:
    """One-line read of a leg-1 probe for the console."""
    ask = (p.get("book1") or [[None]])[0][0]
    s = f"saw {ask} ({p['age1']:.2f}s old), limit {p['limit1']:.3f}, sent +{p['sent_ms']:.0f}ms, back +{p['back_ms']:.0f}ms"
    after = p.get("after1") or {}
    if after.get("asks") is not None:
        s += f"; now {after['asks'][0][0] if after['asks'] else 'empty'}"
    return s


def recently_taken(db, key: str, minutes: float) -> bool:
    return db.execute("SELECT 1 FROM trades WHERE opp_key=? AND detected_at>? LIMIT 1",
                      (key, time.time() - minutes * 60)).fetchone() is not None


# ---------------------------------------------------------------- execution

def execute(db, opp: engine.Opportunity, brokers: dict, cfg: Config, budget: float, log=print,
            moves: dict[str, float] | None = None) -> dict:
    mode = "live" if cfg.live else "dry"
    detected = time.time()
    plan = engine.size(opp.a, opp.b, opp.guaranteed, max_outlay=budget)
    if plan.qty <= 0 or plan.profit < cfg.min_profit:
        return {}

    legs = {"a": (opp.a, plan.fills_a), "b": (opp.b, plan.fills_b)}
    first = first_leg(opp, cfg, moves)
    second = "b" if first == "a" else "a"
    c1, plan1 = legs[first]
    c2, plan2 = legs[second]
    worst2 = plan2[-1][0]
    fills: list[Fill] = []

    # Leg-1 limit: detected worst price + slippage, but never so high that
    # leg 2 at its planned worst price would lose money.
    budget1 = opp.guaranteed - (worst2 + c2.fee.per_contract(worst2))
    limit1 = min(plan1[-1][0] + cfg.slippage, max_price_within(budget1, c1))
    notional1 = sum(p * q for p, q in plan1)
    log(f"  → {mode.upper()} {opp.game.title}: {c1.label} ({c1.venue}) then {c2.label} ({c2.venue}), "
        f"{plan.qty:g} pairs, plan +${plan.profit:.2f} ({plan.roi * 100:.2f}%)")

    # Probe: what leg 1 was priced from and how long the order took, so a miss can be
    # read as "someone beat us" (quote gone right after) or "never takeable" (still there).
    probe = {"leg1": c1.venue, "limit1": limit1, "book1": _top(c1.book), "book2": _top(c2.book),
             "age1": round(detected - c1.book.fetched_at, 3), "age2": round(detected - c2.book.fetched_at, 3)}
    # Cross-venue pairs send BOTH legs at once (--legs parallel): leg 2 no longer waits a round trip for
    # leg 1. Each IOC is capped so that both legs filling at their limits still pays off.
    parallel = cfg.legs == "parallel" and c1.venue != c2.venue
    t_sent = time.time()
    f2 = None
    if parallel:
        unit1 = limit1 + c1.fee.per_contract(limit1)
        limit2p = min(plan2[-1][0] + cfg.slippage, max_price_within(opp.guaranteed - unit1, c2))
        probe["limit2"] = limit2p
        notional2 = sum(p * q for p, q in plan2)
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            j1 = ex.submit(brokers[c1.venue].buy, c1, plan.qty, limit1,
                           notional_hint=notional1 + plan.qty * cfg.slippage)
            j2 = ex.submit(brokers[c2.venue].buy, c2, plan.qty, limit2p,
                           notional_hint=notional2 + plan.qty * cfg.slippage)
            f1, f2 = j1.result(), j2.result()
    else:
        f1 = brokers[c1.venue].buy(c1, plan.qty, limit1, notional_hint=notional1 + plan.qty * cfg.slippage)
    t_back = time.time()
    probe.update(sent_ms=round((t_sent - detected) * 1000, 1), back_ms=round((t_back - detected) * 1000, 1),
                 exch_time=f1.raw.get("create_time") or f1.raw.get("created_time"))
    fills.append(f1)
    if f2 is not None:
        fills.append(f2)
        probe["fill2"] = f2.qty
        if f2.qty > f1.qty:
            # the leg that filled more is the one left to hedge: it plays "leg 1" from here on
            c1, c2, f1, f2, first, second = c2, c1, f2, f1, second, first
    if f1.qty < max(c1.min_size, 0.01):
        if cfg.live:
            probe["after1"] = book_now(c1)
        what = "neither leg filled" if parallel else "leg 1 no fill"
        return _record(db, mode, opp, plan, detected, fills, "missed", 0, 0,
                       note=f"{what}: {f1.error or f1.status} · {probe_text(probe)}", log=log, probe=probe)

    q1 = f1.qty
    cost1 = (f1.notional + f1.fee) / q1
    # Leg-2 limit: break-even for the cost already paid (minus any required edge).
    limit2 = max_price_within(opp.guaranteed - cost1 - cfg.leg2_min_edge, c2)
    step = 0.01 if c2.venue == "Kalshi" else 0.01
    hedged = f2.qty if f2 is not None else 0.0
    remaining = floor_to(q1 - hedged, step)
    for attempt in range(cfg.hedge_attempts - (1 if parallel else 0)):
        if remaining < max(c2.min_size if c2.venue == "Polymarket" else 0.01, 0.01):
            break
        f2 = brokers[c2.venue].buy(c2, remaining, limit2, notional_hint=remaining * limit2)
        fills.append(f2)
        hedged += f2.qty
        remaining = floor_to(q1 - hedged, step)
        if remaining <= 0:
            break
        if attempt + 1 < cfg.hedge_attempts:
            # a rate-limited hedge needs a real pause before the retry, or it 429s again
            time.sleep(1.0 if "429" in (f2.error or "") else 0.3)

    residual = q1 - hedged
    qty1_held = q1
    outcome = "hedged" if residual < 0.01 else ("partial" if hedged > 0 else "exposed")
    note = f"leg1 {q1:g} @ {f1.avg_price:.3f}; leg2 limit {limit2:.3f}, hedged {hedged:g}"

    if residual >= 0.01 and cfg.on_fail == "unwind":
        best_bid = c1.book.best_bid or 0
        # Selling at or above (price paid - unwind_max_loss) loses at most the fees plus that allowance.
        # Below it the sale would cost more than the operator accepts, so the leg is held to settlement.
        floor = f1.avg_price - cfg.unwind_max_loss
        if best_bid + 1e-9 < floor:
            note += f"; HELD to settlement: bid {best_bid:.3f} below sell-back floor {floor:.3f}"
        else:
            fu = brokers[c1.venue].sell(c1, residual, max(best_bid - cfg.unwind_slippage, floor))
            fills.append(fu)
            qty1_held = q1 - fu.qty
            note += f"; unwound {fu.qty:g} @ {fu.avg_price:.3f}" + (f" ({fu.error})" if fu.error else "")
            if qty1_held - hedged < 0.01:
                outcome = "unwound" if hedged == 0 else "hedged"
                if hedged > 0:
                    note += " (partial size, rest unwound)"

    q_a, q_b = (qty1_held, hedged) if first == "a" else (hedged, qty1_held)
    return _record(db, mode, opp, plan, detected, fills, outcome, q_a, q_b, note=note, log=log, probe=probe)


def _record(db, mode, opp, plan, detected, fills, outcome, q_a, q_b, note="", log=print,
            probe: dict | None = None) -> dict:
    cash_out = -sum(f.cash for f in fills)
    hedged = min(q_a, q_b)
    locked = (hedged * opp.guaranteed - cash_out) if outcome == "hedged" else None
    status = "closed" if outcome in ("missed", "unwound") and max(q_a, q_b) < 0.01 else "open"
    vals = dict(
        mode=mode, detected_at=detected, done_at=time.time(), game=opp.game.title,
        kickoff=opp.game.start.isoformat() if opp.game.start else None, opp_key=opp.key,
        leg_a=json.dumps(_snap(opp.a)), leg_b=json.dumps(_snap(opp.b)),
        plan=json.dumps({"qty": plan.qty, "outlay": plan.outlay, "profit": plan.profit, "roi": plan.roi,
                         "avg_a": plan.avg_price("a"), "avg_b": plan.avg_price("b")}),
        fills=json.dumps([f.as_dict() for f in fills]), outcome=outcome, qty_a=q_a, qty_b=q_b,
        cash_out=cash_out, locked_profit=locked, status=status,
        pnl=(-cash_out if status == "closed" else None), note=note,
        probe=json.dumps(probe) if probe else None)
    cur = db.execute(f"INSERT INTO trades ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})",
                     list(vals.values()))
    db.commit()
    vals["id"] = cur.lastrowid
    tag = {"hedged": "✔ HEDGED", "partial": "! PARTIAL", "exposed": "✖ EXPOSED", "unwound": "↺ UNWOUND",
           "missed": "· MISSED", "error": "✖ ERROR"}[outcome]
    extra = f", locked +${locked:.2f}" if locked is not None else ""
    log(f"    {tag} {opp.game.title}: cash out ${cash_out:,.2f}{extra} — {note}")
    return vals


# ---------------------------------------------------------------- settlement

def settle(db, log=print) -> int:
    now = dt.datetime.now(dt.timezone.utc)
    n = 0
    for r in db.execute("SELECT * FROM trades WHERE status='open'").fetchall():
        if r["kickoff"] and dt.datetime.fromisoformat(r["kickoff"]) > now - dt.timedelta(hours=3):
            continue
        payout, ok = 0.0, True
        for leg, q in (("leg_a", r["qty_a"]), ("leg_b", r["qty_b"])):
            if not q:
                continue
            s = json.loads(r[leg])
            try:
                v = scanner.settlement(s["ref"], s["venue"])
            except Exception:
                v = None
            if v is None:
                ok = False
                break
            payout += q * v
        if not ok:
            continue
        pnl = payout - r["cash_out"]
        db.execute("UPDATE trades SET status='settled', payout=?, pnl=?, settled_at=? WHERE id=?",
                   (payout, pnl, time.time(), r["id"]))
        db.commit()
        n += 1
        log(f"  $ SETTLED #{r['id']} {r['game']}: payout ${payout:,.2f}, P&L {pnl:+,.2f}")
    return n


def summary(db, mode: str | None = None) -> dict:
    q = "SELECT * FROM trades" + (" WHERE mode=?" if mode else "") + " ORDER BY id DESC"
    rows = [dict(r) for r in db.execute(q, (mode,) if mode else ())]
    attempted = [r for r in rows if r["outcome"] != "missed"]
    return {
        "rows": rows,
        "seen": len(rows),
        "missed": len(rows) - len(attempted),
        "hedged": sum(r["outcome"] == "hedged" for r in rows),
        "failed": sum(r["outcome"] in ("partial", "exposed", "unwound", "error") for r in rows),
        "spent": sum(max(r["cash_out"] or 0, 0) for r in attempted),
        "locked_open": sum(r["locked_profit"] or 0 for r in rows if r["status"] == "open"),
        "open_cash": sum(max(r["cash_out"] or 0, 0) for r in rows if r["status"] == "open"),
        "realized": sum(r["pnl"] or 0 for r in rows if r["status"] in ("settled", "closed")),
        "settled": sum(r["status"] == "settled" for r in rows),
    }


def summary_text(s: dict) -> str:
    return (f"{s['seen']} arbs acted on · {s['hedged']} hedged · {s['failed']} failed hedges · {s['missed']} missed | "
            f"open ${s['open_cash']:,.2f} (+${s['locked_open']:,.2f} locked) | "
            f"realized {'+' if s['realized'] >= 0 else '-'}${abs(s['realized']):,.2f} over {s['settled']} settled")


# ---------------------------------------------------------------- loop

def make_brokers(cfg: Config, kalshi_demo: bool = False) -> dict:
    from .brokers import KalshiBroker, PaperBroker, PolymarketBroker
    if not cfg.live:
        return {"Kalshi": PaperBroker("Kalshi", latency=0.3), "Polymarket": PaperBroker("Polymarket", latency=0.3)}
    return {"Kalshi": KalshiBroker(demo=kalshi_demo), "Polymarket": PolymarketBroker()}


def run(db, cfg: Config, brokers: dict, days: float, interval: float, once: bool = False, log=print):
    mode = "live" if cfg.live else "dry"
    while True:
        if os.path.exists(cfg.kill_file):
            log(f"Kill switch: '{cfg.kill_file}' exists — stopping.")
            return
        streak = recent_failures(db, mode, cfg.max_failures)
        if streak >= cfg.max_failures:
            log(f"Halting: last {streak} trades failed to hedge. Review the log, then run again.")
            return
        t0 = time.time()
        try:
            _tick(db, cfg, brokers, days, log)
        except Exception as e:  # keep the bot alive across transient API errors
            log(f"! scan error: {type(e).__name__}: {e}")
        try:
            settle(db, log)
        except Exception as e:
            log(f"! settle error: {e}")
        if once:
            return
        time.sleep(max(1.0, interval - (time.time() - t0)))


_last_mid: dict[str, float] = {}   # contract id -> mid at the previous scan


def _moves(games) -> dict[str, float]:
    """|mid now - mid last scan| per contract; remembers this scan's mids."""
    out = {}
    for g in games:
        for c in g.contracts:
            m = mid(c)
            if m is None:
                continue
            if c.id in _last_mid:
                out[c.id] = abs(m - _last_mid[c.id])
            _last_mid[c.id] = m
    return out


_cash: dict = {"at": 0.0, "values": None}
CASH_TTL = 20.0        # s: Kalshi rate-limits balance reads per account, shared with any other bot on it
CASH_STALE_OK = 120.0  # s: on a failed read, an older balance still caps the size


def balances(brokers: dict, force: bool = False) -> dict[str, float]:
    """Venue cash, read at most every CASH_TTL seconds (re-read after every trade)."""
    now = time.time()
    if not force and _cash["values"] is not None and now - _cash["at"] < CASH_TTL:
        return _cash["values"]
    try:
        vals = {v: b.balance() for v, b in brokers.items()}
    except Exception:
        if _cash["values"] is not None and now - _cash["at"] < CASH_STALE_OK:
            return _cash["values"]
        raise
    _cash.update(at=now, values=vals)
    return vals


def startup_balances(brokers: dict, tries: int = 5, wait: float = 2.0, log=print) -> dict[str, float]:
    """First balance read. A 429 here (e.g. right after `check` read the same account) is waited out
    with growing pauses instead of crashing the start; any other error is raised at once."""
    for i in range(tries):
        try:
            return balances(brokers, force=True)
        except Exception as e:
            if "429" not in str(e) or i == tries - 1:
                raise
            log(f"balance read rate-limited (429), retrying in {wait * (i + 1):.0f} s")
            time.sleep(wait * (i + 1))


def _tick(db, cfg: Config, brokers: dict, days: float, log):
    mode = "live" if cfg.live else "dry"
    # In-play scans only fetch games starting within 30 min (Kalshi kickoffs are
    # estimates, so the exact in-progress filter is applied below).
    snap = scanner.snapshot(min(days, 0.02) if cfg.in_play_only else days, sports=cfg.sports)
    now = dt.datetime.now(dt.timezone.utc)
    moves = _moves(snap.games)
    if cfg.in_play_only:
        # A game with a venue live flag (tennis) is in play exactly while the flag is on.
        games = [g for g in snap.games if g.started(now)
                 and (g.live is not None or now - dt.timedelta(hours=5) < g.start)]
    elif cfg.include_live_games:
        games = snap.games
    else:
        games = [g for g in snap.games if not g.started(now) and g.start is not None]
    feed = polymarket._feed
    if feed is not None:
        # The Polymarket books were copied from the feed at the START of the scan; the Kalshi REST
        # fetch then takes 1-3 s (11-03: every miss was priced from a 1.3-3.6 s old Polymarket book).
        # Re-read them from the feed now: memory only, no network.
        polymarket.refresh([c for g in games for c in g.contracts if c.venue == "Polymarket"])
    arbs, _ = engine.find(games, min_roi=cfg.min_roi, cross_venue_only=cfg.cross_venue_only)
    arbs = [o for o in engine.non_overlapping(arbs) if o.max.profit >= cfg.min_profit]
    # Kalshi books are REST snapshots: their age IS how stale they are. A Polymarket book is the feed's latest
    # message, just re-read above; a quiet market can go many seconds without one and still be current
    # (11-03 shadow: median 0.69 s, 44% > 1 s), so its age is not a staleness signal and is not gated.
    t_now = time.time()
    fresh = [o for o in arbs if all(t_now - x.book.fetched_at <= cfg.max_book_age
                                    for x in (o.a, o.b) if x.venue == "Kalshi")]
    stale_note = (f" · {len(arbs) - len(fresh)} skipped (Kalshi book > {cfg.max_book_age:g}s old)"
                  if len(fresh) < len(arbs) else "")
    feed_note = " · Polymarket feed DOWN" if feed is not None and not feed.alive() else ""
    log(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {len(games)} games · "
        f"{sum(len(g.contracts) for g in games):,} contracts · {len(fresh)} arb(s) ≥ {cfg.min_roi * 100:.2f}%"
        + stale_note + feed_note + (" · " + "; ".join(snap.errors) if snap.errors else ""))
    for o in fresh:
        if recently_taken(db, o.key, cfg.cooldown_min):
            continue
        room_day = cfg.max_daily_spend - spent_today(db, mode)
        room_open = cfg.max_open - open_exposure(db, mode)
        try:
            cash = balances(brokers)
        except Exception as e:
            log(f"! balance check failed: {e}")
            return
        # Each leg is paid on its own venue -- a Kalshi leg from its market's shard -- and the smaller
        # pot caps the pair.
        shards = getattr(brokers.get("Kalshi"), "shards", None)
        pots = []
        for leg in {o.a.venue: o.a, o.b.venue: o.b}.values():
            pot = cash[leg.venue]
            if leg.venue == "Kalshi" and shards is not None and leg.ref.get("shard") is not None:
                pot = shards.get(int(leg.ref["shard"]), 0.0)
            pots.append(pot)
        cash_cap = min(pots) * (2 if len(pots) == 2 else 1) * 0.95
        budget = min(cfg.max_per_trade, room_day, room_open, cash_cap)
        if budget < 5:
            log(f"  skip {o.game.title}: limit reached (day ${room_day:.0f}, open ${room_open:.0f}, cash ${cash_cap:.0f})")
            continue
        try:
            done = execute(db, o, brokers, cfg, budget, log, moves=moves)
        except Exception as e:
            done = True
            log(f"! execution error on {o.game.title}: {type(e).__name__}: {e}")
        if done:
            _cash["at"] = 0.0          # cash changed (or may have): re-read before the next trade
        if recent_failures(db, mode, cfg.max_failures) >= cfg.max_failures:
            return
