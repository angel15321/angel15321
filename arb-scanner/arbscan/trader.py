"""The auto-arb loop: scan → pick arbs → execute both legs → log → settle.

Execution of one arb
--------------------
1. Size it against live depth, capped by --max-per-trade and the cash on
   each venue (and the daily / open-exposure limits).
2. Leg 1 = the Polymarket leg when there is one (its sports markets match
   after a short delay, so we must see that fill before hedging); otherwise
   the thinner leg. Leg 1 is an immediate-or-cancel buy whose limit keeps the
   pair profitable against leg 2's current price.
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

import datetime as dt
import json
import os
import sqlite3
import time
from dataclasses import dataclass

from . import engine, scanner
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
  payout REAL, pnl REAL, settled_at REAL, note TEXT
);
CREATE INDEX IF NOT EXISTS trades_key ON trades(opp_key, detected_at);
"""


def connect(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


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
    cooldown_min: float = 10.0
    include_live_games: bool = False  # in-play arbs move fastest; off by default
    cross_venue_only: bool = False
    max_failures: int = 2             # halt after this many unhedged/unwound trades in a row
    kill_file: str = "STOP"


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
    rows = db.execute("SELECT outcome FROM trades WHERE mode=? AND outcome!='missed' ORDER BY id DESC LIMIT ?",
                      (mode, n)).fetchall()
    streak = 0
    for r in rows:
        if r["outcome"] in ("unwound", "exposed", "partial", "error"):
            streak += 1
        else:
            break
    return streak


def recently_taken(db, key: str, minutes: float) -> bool:
    return db.execute("SELECT 1 FROM trades WHERE opp_key=? AND detected_at>? LIMIT 1",
                      (key, time.time() - minutes * 60)).fetchone() is not None


# ---------------------------------------------------------------- execution

def execute(db, opp: engine.Opportunity, brokers: dict, cfg: Config, budget: float, log=print) -> dict:
    mode = "live" if cfg.live else "dry"
    detected = time.time()
    plan = engine.size(opp.a, opp.b, opp.guaranteed, max_outlay=budget)
    if plan.qty <= 0 or plan.profit < cfg.min_profit:
        return {}

    legs = {"a": (opp.a, plan.fills_a), "b": (opp.b, plan.fills_b)}
    if opp.a.venue != opp.b.venue:
        first = "a" if opp.a.venue == "Polymarket" else "b"
    else:
        top = {k: c.book.asks[0][1] if c.book.asks else 0 for k, (c, _) in legs.items()}
        first = "a" if top["a"] <= top["b"] else "b"
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

    f1 = brokers[c1.venue].buy(c1, plan.qty, limit1, notional_hint=notional1 + plan.qty * cfg.slippage)
    fills.append(f1)
    if f1.qty < max(c1.min_size, 0.01):
        return _record(db, mode, opp, plan, detected, fills, "missed", 0, 0,
                       note=f"leg 1 no fill: {f1.error or f1.status}", log=log)

    q1 = f1.qty
    cost1 = (f1.notional + f1.fee) / q1
    # Leg-2 limit: break-even for the cost already paid (minus any required edge).
    limit2 = max_price_within(opp.guaranteed - cost1 - cfg.leg2_min_edge, c2)
    step = 0.01 if c2.venue == "Kalshi" else 0.01
    remaining = floor_to(q1, step)
    hedged = 0.0
    for attempt in range(cfg.hedge_attempts):
        if remaining < max(c2.min_size if c2.venue == "Polymarket" else 0.01, 0.01):
            break
        f2 = brokers[c2.venue].buy(c2, remaining, limit2, notional_hint=remaining * limit2)
        fills.append(f2)
        hedged += f2.qty
        remaining = floor_to(q1 - hedged, step)
        if remaining <= 0:
            break
        if attempt + 1 < cfg.hedge_attempts:
            time.sleep(0.3)

    residual = q1 - hedged
    qty1_held = q1
    outcome = "hedged" if residual < 0.01 else ("partial" if hedged > 0 else "exposed")
    note = f"leg1 {q1:g} @ {f1.avg_price:.3f}; leg2 limit {limit2:.3f}, hedged {hedged:g}"

    if residual >= 0.01 and cfg.on_fail == "unwind":
        best_bid = c1.book.best_bid or 0
        fu = brokers[c1.venue].sell(c1, residual, best_bid - cfg.unwind_slippage)
        fills.append(fu)
        qty1_held = q1 - fu.qty
        note += f"; unwound {fu.qty:g} @ {fu.avg_price:.3f}" + (f" ({fu.error})" if fu.error else "")
        if qty1_held - hedged < 0.01:
            outcome = "unwound" if hedged == 0 else "hedged"
            if hedged > 0:
                note += " (partial size, rest unwound)"

    q_a, q_b = (qty1_held, hedged) if first == "a" else (hedged, qty1_held)
    return _record(db, mode, opp, plan, detected, fills, outcome, q_a, q_b, note=note, log=log)


def _record(db, mode, opp, plan, detected, fills, outcome, q_a, q_b, note="", log=print) -> dict:
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
        pnl=(-cash_out if status == "closed" else None), note=note)
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


def _tick(db, cfg: Config, brokers: dict, days: float, log):
    mode = "live" if cfg.live else "dry"
    snap = scanner.snapshot(days)
    now = dt.datetime.now(dt.timezone.utc)
    games = snap.games if cfg.include_live_games else [g for g in snap.games if g.start and g.start > now]
    arbs, _ = engine.find(games, min_roi=cfg.min_roi, cross_venue_only=cfg.cross_venue_only)
    arbs = [o for o in engine.non_overlapping(arbs) if o.max.profit >= cfg.min_profit]
    log(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {len(games)} games · "
        f"{sum(len(g.contracts) for g in games):,} contracts · {len(arbs)} arb(s) ≥ {cfg.min_roi * 100:.2f}%"
        + (" · " + "; ".join(snap.errors) if snap.errors else ""))
    for o in arbs:
        if recently_taken(db, o.key, cfg.cooldown_min):
            continue
        room_day = cfg.max_daily_spend - spent_today(db, mode)
        room_open = cfg.max_open - open_exposure(db, mode)
        try:
            cash = {v: b.balance() for v, b in brokers.items()}
        except Exception as e:
            log(f"! balance check failed: {e}")
            return
        # Each leg is paid on its own venue: the smaller venue balance caps the pair.
        venues = {o.a.venue, o.b.venue}
        cash_cap = min(cash[v] for v in venues) * (2 if len(venues) == 2 else 1) * 0.95
        budget = min(cfg.max_per_trade, room_day, room_open, cash_cap)
        if budget < 5:
            log(f"  skip {o.game.title}: limit reached (day ${room_day:.0f}, open ${room_open:.0f}, cash ${cash_cap:.0f})")
            continue
        try:
            execute(db, o, brokers, cfg, budget, log)
        except Exception as e:
            log(f"! execution error on {o.game.title}: {type(e).__name__}: {e}")
        if recent_failures(db, mode, cfg.max_failures) >= cfg.max_failures:
            return
