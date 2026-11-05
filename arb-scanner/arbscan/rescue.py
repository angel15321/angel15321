"""Held-leg watcher: close a one-sided position the moment it can be closed at a PROFIT.

A failed hedge leaves leg 1 held to settlement (the sell-back rule only looks once, right after the fill).
11-05 #53: Boulais bought at 0.54 was worth 0.49 at the fill, then 0.74-0.96 a minute later for ~2 minutes,
then lost (-$4.32). A late Kalshi hedge at 22:14 would have locked +$1.6..3.4. Now every open `exposed` /
`partial` row whose two contracts are in view is re-checked on every scan / book event:

  late hedge   buy the pair's other leg when  payout - (cost of the held leg) - (ask + fee)  >= rescue_edge
               (the pair is then hedged: a locked profit whatever the result)
  sell back    sell the held leg when  bid - fee  >= cost + rescue_sell

whichever locks more per contract. Both are immediate-or-cancel at a price that keeps the profit, so a miss
costs nothing. Nothing is ever closed at a loss here; a leg with no profitable exit still rides to settlement.
"""
from __future__ import annotations

import json
import time

from . import engine, hours, notify
from .brokers import Fill, floor_to, max_price_within

_next_try: dict[int, float] = {}       # trade id -> earliest next attempt (a miss waits RETRY_S)
RETRY_S = 2.0


def _cid(s: dict) -> str:
    """Contract.id from a ledger snapshot."""
    ref = s["ref"]
    return f"{s['venue']}:{ref.get('ticker') or ref.get('token_id')}:{ref.get('side', '')}"


def _leg_cost(fills: list[dict], venue: str, label: str) -> tuple[float, float]:
    """(contracts held, average cost per contract incl. fees) of one leg from its fills."""
    q_buy = cost = q_sold = 0.0
    for f in fills:
        if f.get("venue") != venue or f.get("label") != label or not f.get("qty"):
            continue
        if f["action"] == "buy":
            q_buy += f["qty"]
            cost += f["qty"] * f["avg_price"] + (f.get("fee") or 0.0)
        else:
            q_sold += f["qty"]
    return q_buy - q_sold, (cost / q_buy if q_buy else 0.0)


def _cash_changed() -> None:
    """A rescue fill moved cash on a venue: the next quote is sized from a fresh balance read, not the 20 s cache."""
    from . import trader
    trader._cash["dirty"] = True


def check(db, cfg, brokers, games, log) -> int:
    """Try every open one-sided row whose contracts are in `games`. Returns how many were closed."""
    if not cfg.rescue:
        return 0
    mode = "live" if cfg.live else "dry"
    rows = db.execute("SELECT * FROM trades WHERE mode=? AND status='open' AND outcome IN "
                      "('exposed','partial','error','dust')", (mode,)).fetchall()
    if not rows:
        return 0
    by_id = {c.id: c for g in games for c in g.contracts}
    now, done = time.time(), 0
    for r in rows:
        if now < _next_try.get(r["id"], 0.0):
            continue
        sa, sb = json.loads(r["leg_a"]), json.loads(r["leg_b"])
        ca, cb = by_id.get(_cid(sa)), by_id.get(_cid(sb))
        if ca is None or cb is None:
            continue
        qa, qb = r["qty_a"] or 0.0, r["qty_b"] or 0.0
        held, other, q_other = (ca, cb, qb) if qa > qb else (cb, ca, qa)
        fills = json.loads(r["fills"] or "[]")
        q_held, cost = _leg_cost(fills, held.venue, held.label)
        if q_held <= 0 or cost <= 0:        # a recovered row has no fills: its cost is cash_out / qty
            q_held = max(qa, qb)
            cost = (r["cash_out"] or 0.0) / q_held if q_held else 0.0
        residual = max(qa, qb) - q_other
        if residual < 0.01 or cost <= 0:
            continue
        guaranteed, _ = engine.guaranteed_payout(ca, cb)
        hedge_lock = sell_lock = None
        if other.book.best_ask is not None:
            ask = other.book.best_ask
            hedge_lock = guaranteed - cost - (ask + other.fee.per_contract(ask))
        if held.book.best_bid is not None:
            bid = held.book.best_bid
            sell_lock = bid - held.fee.per_contract(bid) - cost
        hedge_ok = hedge_lock is not None and hedge_lock >= cfg.rescue_edge - 1e-9
        if other.venue == "Kalshi" and residual < 1.0:
            hedge_ok = False                # under one Kalshi contract: only a sell-back can close it
        sell_ok = sell_lock is not None and sell_lock >= cfg.rescue_sell - 1e-9
        # a venue that is closed takes no order: 11-05 08:04Z nine late hedges in 10 s all came back "paused"
        if hedge_ok and hours.closed(other.venue, other.ref.get("shard"), now, pad=False):
            hedge_ok = False
        if sell_ok and hours.closed(held.venue, held.ref.get("shard"), now, pad=False):
            sell_ok = False
        if not hedge_ok and not sell_ok:
            continue
        _next_try[r["id"]] = now + RETRY_S
        if hedge_ok and (not sell_ok or hedge_lock >= sell_lock):
            done += _late_hedge(db, r, brokers, held, other, residual, cost, guaranteed, cfg, log, fills, qa > qb)
        else:
            done += _sell_back(db, r, brokers, held, residual, cost, cfg, log, fills, qa > qb, q_other)
    return done


def _late_hedge(db, r, brokers, held, other, residual, cost, guaranteed, cfg, log, fills, a_held) -> int:
    step = 1.0 if other.venue == "Kalshi" else 0.01
    q = floor_to(residual, step)
    if q < max(other.min_size if other.venue != "Kalshi" else 1.0, 0.01):
        return 0
    limit = max_price_within(guaranteed - cost - cfg.rescue_edge, other)
    f = brokers[other.venue].buy(other, q, limit, notional_hint=q * limit)
    if not f.qty:
        log(f"  · late hedge missed #{r['id']} {r['game']}: {other.label} {f.error or f.status} (limit {limit:.3f})")
        hours.refused(other, f.error)           # "paused": no more tries (and no quotes) until the venue trades
        return 0
    fills.append(f.as_dict())
    _cash_changed()
    qa, qb = (r["qty_a"], (r["qty_b"] or 0) + f.qty) if a_held else ((r["qty_a"] or 0) + f.qty, r["qty_b"])
    cash_out = (r["cash_out"] or 0.0) + f.qty * f.avg_price + f.fee
    hedged = min(qa, qb)
    left = max(qa, qb) - hedged
    outcome = "hedged" if left < 0.01 or (left < 1.0 and other.venue == "Kalshi") else "partial"
    locked = hedged * guaranteed - cash_out if outcome == "hedged" else None
    note = (r["note"] or "") + (f"; LATE HEDGE {f.qty:g} {other.label} @ {f.avg_price:.3f} on {other.venue} "
                                f"({time.time() - r['detected_at']:.0f}s after the fill)"
                                + (f", locked +${locked:.2f}" if locked is not None else f", {left:g} still held"))
    db.execute("UPDATE trades SET qty_a=?, qty_b=?, cash_out=?, fills=?, outcome=?, locked_profit=?, note=? WHERE id=?",
               (qa, qb, cash_out, json.dumps(fills), outcome, locked, note, r["id"]))
    db.commit()
    log(f"    ✔ LATE HEDGE #{r['id']} {r['game']}: {f.qty:g} {other.label} @ {f.avg_price:.3f} on {other.venue}"
        + (f" -- locked +${locked:.2f}" if locked is not None else f" -- {left:g} still held"))
    if r["mode"] == "live":
        notify.push(f"🛡 LATE HEDGE FILLED #{r['id']} · {r['game']} · {f.qty:g} {other.label} @ "
                    f"{notify.cents(f.avg_price)} on {other.venue}"
                    + (f" · locked {notify.money(locked)}" if locked is not None else f" · {left:g} still held"))
    return 1


def _sell_back(db, r, brokers, held, residual, cost, cfg, log, fills, a_held, q_other) -> int:
    step = 1.0 if held.venue == "Kalshi" else 0.01
    q = floor_to(residual, step)
    if q <= 0:
        return 0
    bid = held.book.best_bid
    min_price = cost + cfg.rescue_sell + held.fee.per_contract(bid)       # proceeds after the sell fee still >= cost + margin
    f = brokers[held.venue].sell(held, q, min_price)
    if not f.qty:
        log(f"  · profit sell-back missed #{r['id']} {r['game']}: {held.label} {f.error or f.status} (min {min_price:.3f})")
        hours.refused(held, f.error)
        return 0
    fills.append(f.as_dict())
    _cash_changed()
    qa, qb = ((r["qty_a"] or 0) - f.qty, r["qty_b"]) if a_held else (r["qty_a"], (r["qty_b"] or 0) - f.qty)
    cash_out = (r["cash_out"] or 0.0) - (f.qty * f.avg_price - f.fee)
    left = max(qa, qb) - min(qa, qb)
    if max(qa, qb) < 0.01:
        outcome, status, pnl = "sold", "closed", -cash_out          # flat: the profit is realized now
    elif left < 0.01:
        outcome, status, pnl = "hedged", "open", None                 # what is left is the hedged part of the pair
    else:
        outcome, status, pnl = r["outcome"], "open", None
    note = (r["note"] or "") + (f"; SOLD BACK {f.qty:g} {held.label} @ {f.avg_price:.3f} at a profit "
                                f"({time.time() - r['detected_at']:.0f}s after the fill)")
    locked = (min(qa, qb) * engine.guaranteed_payout(*_pair_of(r))[0] - cash_out) if outcome == "hedged" else None
    db.execute("UPDATE trades SET qty_a=?, qty_b=?, cash_out=?, fills=?, outcome=?, status=?, pnl=?, locked_profit=?, "
               "note=? WHERE id=?", (qa, qb, cash_out, json.dumps(fills), outcome, status, pnl, locked, note, r["id"]))
    db.commit()
    log(f"    ✔ SOLD BACK #{r['id']} {r['game']}: {f.qty:g} {held.label} @ {f.avg_price:.3f} on {held.venue}"
        + (f" -- realized +${pnl:.2f}" if pnl is not None else ""))
    if r["mode"] == "live":
        notify.push(f"✔ SOLD BACK #{r['id']} · {r['game']} · {f.qty:g} {held.label} @ {notify.cents(f.avg_price)} on "
                    f"{held.venue}" + (f" · realized {notify.money(pnl)}" if pnl is not None else ""))
    return 1


class _Leg:
    """Minimal payout view of a ledger snapshot (for guaranteed_payout after a sell-back)."""
    def __init__(self, s):
        self.line, self.side, self.tie_half = s["line"], s["side"], s.get("tie_half", False)

    def payout(self, x: int) -> float:
        if x == self.line:
            return 0.5 if self.tie_half else 0.0
        return 1.0 if (x > self.line) == (self.side == "gt") else 0.0


def _pair_of(r):
    return _Leg(json.loads(r["leg_a"])), _Leg(json.loads(r["leg_b"]))

