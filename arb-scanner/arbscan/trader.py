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
import threading
import time
from dataclasses import dataclass

from . import engine, kalshi, polymarket, scanner
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
    trigger: str = "scan"             # scan (price once per scan) | event (re-check a game on each book change)
    event_gap: float = 0.05           # s: a game is re-checked at most this often on the event path
    confirm_s: float = 0.03           # s: an event-path arb must still be there this long after first sighting
    stale_pm: str = "skip"            # in-play arb whose STALE leg (leg 1) is Polymarket: skip (PM-first IOCs went 0/38
                                      # on 11-03: its API takes 85-385 ms, the quote is gone in <100 ms) | first (try it)
    hedge_max_loss: float = 0.0       # $/contract leg 2 may pay ABOVE break-even to get hedged (0 = break-even only);
                                      # a bounded locked loss instead of a $20 one-sided hold -- operator's call, off by default
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


def first_leg(opp: engine.Opportunity, cfg: Config, moves: dict[str, float] | None = None,
              changed: set[str] | None = None) -> str:
    """'a' or 'b': which leg to buy first (see module docstring). `changed` = the venues of THIS pair whose book
    change triggered the check (event path): the venue that did NOT just move holds the stale quote and goes
    first. The mid-move fallback cannot tell: both 11-03 held legs (rows 38, 41) had it send Kalshi first while
    Polymarket's book was the stale one (PM mid-reprice looked like 'PM moved more')."""
    a, b = opp.a, opp.b
    if a.venue == b.venue:
        top = {k: c.book.asks[0][1] if c.book.asks else 0 for k, c in (("a", a), ("b", b))}
        return "a" if top["a"] <= top["b"] else "b"
    if cfg.leg1 in ("polymarket", "kalshi"):
        want = "Polymarket" if cfg.leg1 == "polymarket" else "Kalshi"
        return "a" if a.venue == want else "b"
    if changed and len(changed) == 1:
        return "b" if a.venue in changed else "a"
    live = opp.game.started(dt.datetime.now(dt.timezone.utc))
    ma, mb = (moves or {}).get(a.id), (moves or {}).get(b.id)
    if live and ma is not None and mb is not None and abs(ma - mb) >= 0.005:
        return "a" if ma < mb else "b"
    return "a" if a.venue == "Polymarket" else "b"


def _top(book, n: int = 3) -> list:
    return [[p, s] for p, s in book.asks[:n]]


def book_now(c: Contract, rest: bool = False) -> dict:
    """Leg's ask book re-read from the source right now (feed for Polymarket, REST for Kalshi). `rest` adds the
    Polymarket gateway REST snapshot next to the feed book: no Polymarket IOC has filled yet (0/38), so the
    feed-book -> order-price mapping is still unconfirmed and the two are logged side by side on every miss."""
    try:
        if c.venue == "Polymarket":
            books = polymarket.fetch_books([c.ref["slug"]]).get(c.ref["slug"])
            book = books and books[0 if c.ref["side"] == "long" else 1]
        else:
            books = kalshi.fetch_books([c.ref["ticker"]]).get(c.ref["ticker"])
            book = books and books[0 if c.ref["side"] == "yes" else 1]
        out = {"asks": None} if not book else {"asks": _top(book), "bid": book.best_bid,
                                               "age": round(time.time() - book.fetched_at, 3)}
        if rest and c.venue == "Polymarket":
            try:
                rb = polymarket.fetch_book(c.ref["slug"])[0 if c.ref["side"] == "long" else 1]
                out["rest"] = {"asks": _top(rb), "bid": rb.best_bid}
            except Exception as e:
                out["rest"] = {"error": f"{type(e).__name__}: {e}"}
        return out
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def live_book(c: Contract):
    """The leg's book from the in-memory feed right now (no network), or None when no live feed has it."""
    try:
        if c.venue == "Polymarket":
            feed = polymarket._feed
            hit = feed.get_at(c.ref["slug"]) if feed is not None else None
            if hit is None:
                return None
            long, short = polymarket.books_from(hit[1], now=hit[0])
            return long if c.ref["side"] == "long" else short
        feed = kalshi._feed
        b = feed.get(c.ref["ticker"]) if feed is not None else None
        if b is None:
            return None
        yes, no = kalshi._pair(*b.levels(), time.time())
        return yes if c.ref["side"] == "yes" else no
    except Exception:
        return None


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

def _net_held(fills: list[Fill], c: Contract) -> float:
    """Contracts of `c` held after these fills (buys minus sells)."""
    return sum(f.qty if f.action == "buy" else -f.qty for f in fills if f.venue == c.venue and f.label == c.label)


def execute(db, opp: engine.Opportunity, brokers: dict, cfg: Config, budget: float, log=print,
            moves: dict[str, float] | None = None, leg_budgets: dict[str, float] | None = None,
            changed: set[str] | None = None) -> dict:
    mode = "live" if cfg.live else "dry"
    detected = time.time()
    lb = leg_budgets or {}     # per-leg cash caps ("a"/"b"): a leg is paid from its own venue / Kalshi shard
    plan = engine.size(opp.a, opp.b, opp.guaranteed, max_outlay=budget, max_outlay_a=lb.get("a"), max_outlay_b=lb.get("b"))
    if plan.qty <= 0 or plan.profit < cfg.min_profit:
        return {}

    legs = {"a": (opp.a, plan.fills_a), "b": (opp.b, plan.fills_b)}
    first = first_leg(opp, cfg, moves, changed)
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
             "age1": round(detected - c1.book.fetched_at, 3), "age2": round(detected - c2.book.fetched_at, 3),
             "changed": sorted(changed) if changed else None}
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
            probe["after1"] = book_now(c1, rest=True)
        what = "neither leg filled" if parallel else "leg 1 no fill"
        return _record(db, mode, opp, plan, detected, fills, "missed", 0, 0,
                       note=f"{what}: {f1.error or f1.status} · {probe_text(probe)}", log=log, probe=probe)

    try:
        return _after_leg1(db, mode, opp, plan, detected, fills, c1, c2, f1, f2, first, parallel, brokers, cfg, log, probe)
    except Exception as e:
        # Leg 1 is a real position: record it from the fills so far before the error leaves execute, so
        # settle(), the open-exposure cap, the cooldown and the halt streak all see it.
        held1, held2 = _net_held(fills, c1), _net_held(fills, c2)
        q_a, q_b = (held1, held2) if first == "a" else (held2, held1)
        _record(db, mode, opp, plan, detected, fills, "error", q_a, q_b,
                note=f"{type(e).__name__}: {e} -- position recorded from the fills so far; verify on the venues",
                log=log, probe=probe)
        raise


def _after_leg1(db, mode, opp, plan, detected, fills, c1, c2, f1, f2, first, parallel, brokers, cfg, log, probe) -> dict:
    """Hedge leg 1, unwind or hold what cannot be hedged, record the row."""
    q1 = f1.qty
    cost1 = (f1.notional + f1.fee) / q1
    # Leg-2 limit: break-even for the cost already paid (minus any required edge).
    limit2 = max_price_within(opp.guaranteed - cost1 - cfg.leg2_min_edge + cfg.hedge_max_loss, c2)
    # Kalshi trades whole contracts; a Polymarket leg 1 may have filled a fraction (min 0.01).
    step = 1.0 if c2.venue == "Kalshi" else 0.01
    min2 = 1.0 if c2.venue == "Kalshi" else max(c2.min_size, 0.01)
    hedged = f2.qty if f2 is not None else 0.0
    remaining = floor_to(q1 - hedged, step)
    hedge_books, gone = [], ""
    for attempt in range(cfg.hedge_attempts - (1 if parallel else 0)):
        if remaining < min2:
            break
        if attempt > 0:
            if "429" in (fills[-1].error or ""):
                time.sleep(1.0)            # a rate-limited hedge needs a real pause before the retry, or it 429s again
            # Before a retry look at the feed's in-memory book (no network: this is not the pre-trade REST re-check).
            # 11-03 rows 38/41: three IOCs at one limit over ~0.8 s into a book that had moved past it; each PM call
            # is 100-400 ms of extra one-sided exposure. A quote gone at the source ends the retries at once.
            book = live_book(c2)
            if book is None:
                time.sleep(0.3)            # nothing to consult: the old blind pause
            else:
                hedge_books.append({"asks": _top(book), "age": round(time.time() - book.fetched_at, 3)})
                if book.best_ask is None or book.best_ask > limit2 + 1e-9:
                    gone = f"; hedge quote gone at the source (ask {book.best_ask}), retries stopped"
                    break
        f2 = brokers[c2.venue].buy(c2, remaining, limit2, notional_hint=remaining * limit2)
        fills.append(f2)
        hedged += f2.qty
        remaining = floor_to(q1 - hedged, step)
        if remaining <= 0:
            break
    if hedge_books:
        probe["hedge_books"] = hedge_books

    residual = q1 - hedged
    qty1_held = q1
    outcome = "hedged" if residual < 0.01 else ("partial" if hedged > 0 else "exposed")
    note = f"leg1 {q1:g} @ {f1.avg_price:.3f}; leg2 limit {limit2:.3f}, hedged {hedged:g}" + gone

    if residual >= 0.01 and cfg.on_fail == "unwind":
        best_bid = c1.book.best_bid or 0
        if cfg.live:
            # the scan-time bid is 1-2 s old after the fill and the hedge tries: decide on the live one
            now1 = book_now(c1)
            if now1.get("bid") is not None:
                note += f"; bid {best_bid:.3f} at scan, {now1['bid']:.3f} now"
                best_bid = now1["bid"]
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

    if residual >= 0.01 and cfg.live:
        probe["after2"] = book_now(c2, rest=True)     # after the decision: the hedge side, feed and REST, for the read

    leftover = qty1_held - hedged
    if 0.01 <= leftover < 1.0 and c2.venue == "Kalshi" and hedged > 0 and outcome != "hedged":
        # the fraction under one Kalshi contract can never be hedged there: dust, not a failed hedge
        outcome = "hedged"
        note += f"; {leftover:g} ct dust (under one Kalshi contract) held"

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

SETTLE_GAP = 60.0      # s between exchange settlement reads from the trading loop (`status` reads at once)
_settled_at = 0.0


def settle(db, log=print, min_gap: float = 0.0) -> int:
    """Price open rows from each venue's official result. No fixed 'kickoff + N h' wait: NBA games end
    in ~2h15m and tennis start times are schedule slots, so the exchange's own status decides; only
    rows whose game has not started yet are skipped. `min_gap` paces the reads from the 1-s loop."""
    global _settled_at
    if min_gap and time.time() - _settled_at < min_gap:
        return 0
    _settled_at = time.time()
    now = dt.datetime.now(dt.timezone.utc)
    n = 0
    for r in db.execute("SELECT * FROM trades WHERE status='open'").fetchall():
        if r["kickoff"] and dt.datetime.fromisoformat(r["kickoff"]) > now:
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


class Trigger:
    """Markets whose book just changed, filled by the feed threads, drained by the trading loop."""

    def __init__(self):
        self._lock = threading.Lock()
        self._dirty: set[str] = set()
        self.event = threading.Event()

    def mark(self, key: str) -> None:
        with self._lock:
            self._dirty.add(key)
        self.event.set()

    def take(self) -> set[str]:
        with self._lock:
            d, self._dirty = self._dirty, set()
            self.event.clear()
        return d


def run(db, cfg: Config, brokers: dict, days: float, interval: float, once: bool = False, log=print):
    """Full scan every `interval` s (market lists, game state, balances, settlement). With --trigger event,
    between scans every book change re-checks just its game (at most every cfg.event_gap), and an arb trades
    only if it is still there cfg.confirm_s later (a quote moved as delete-then-add looks like a gap for a moment)."""
    mode = "live" if cfg.live else "dry"
    state = {"games": [], "index": {}, "moves": {}, "confirm": {}, "due": {}, "last_eval": {},
             "stats": {"wakes": 0, "evals": 0, "seen": 0, "blips": 0, "confirmed": 0, "pm_skipped": 0},
             "stats_at": time.time()}
    trig = None
    if cfg.trigger == "event":
        trig = Trigger()
        for feed in (polymarket._feed, kalshi._feed):
            if feed is not None:
                feed.on_update = trig.mark
    next_scan = 0.0
    while True:
        if time.time() >= next_scan:
            if os.path.exists(cfg.kill_file):
                log(f"Kill switch: '{cfg.kill_file}' exists — stopping.")
                return
            try:
                settle(db, log, min_gap=SETTLE_GAP)     # before the halt check: held positions settle while halted
            except Exception as e:
                log(f"! settle error: {e}")
            streak = recent_failures(db, mode, cfg.max_failures)
            if streak >= cfg.max_failures:
                log(f"Halting: last {streak} trades failed to hedge. Review the log, then run again.")
                return
            t0 = time.time()
            try:
                _tick(db, cfg, brokers, days, log, state if trig is not None else None)
            except Exception as e:  # keep the bot alive across transient API errors
                log(f"! scan error: {type(e).__name__}: {e}")
            if once:
                return
            next_scan = t0 + max(1.0, interval)
            if trig is not None and time.time() - state["stats_at"] >= 60:
                s = state["stats"]
                log(f"  event trigger, last {time.time() - state['stats_at']:.0f}s: {s['wakes']:,} wakes · "
                    f"{s['evals']:,} game checks · {s['seen']} arbs seen · {s['blips']} gone within "
                    f"{cfg.confirm_s * 1000:.0f} ms · {s['confirmed']} confirmed · "
                    f"{s.get('pm_skipped', 0)} stale-PM skipped")
                state["stats"] = {k: 0 for k in s}
                state["stats_at"] = time.time()
        if trig is None:
            time.sleep(max(0.0, next_scan - time.time()))
            continue
        due = min([next_scan] + list(state["due"].values()))
        trig.event.wait(max(0.0, due - time.time()))
        try:
            if _on_events(db, cfg, brokers, state, trig.take(), log) == "halt":
                next_scan = 0.0                          # the halt check at the top of the loop reports it
        except Exception as e:
            log(f"! event error: {type(e).__name__}: {e}")


def _on_events(db, cfg: Config, brokers: dict, state: dict, keys: set[str], log) -> str | None:
    """Re-check the games whose books changed (or whose parked arb is due its confirm look)."""
    now = time.time()
    state["stats"]["wakes"] += 1
    games = {}
    for k in keys:
        g = state["index"].get(k)
        if g is not None:
            games[g.key] = g
    by_key = {g.key: g for g in state["games"]}
    for gk, at in list(state["due"].items()):
        if at <= now and gk in by_key:
            games[gk] = by_key[gk]
    run_now = []
    for gk, g in games.items():
        wait = state["last_eval"].get(gk, 0.0) + cfg.event_gap - now
        if wait > 0:
            state["due"][gk] = min(state["due"].get(gk, float("inf")), now + wait)   # throttled: look again soon
            continue
        state["due"].pop(gk, None)
        state["last_eval"][gk] = now
        run_now.append(g)
    if not run_now:
        return None
    state["stats"]["evals"] += len(run_now)
    return _evaluate(db, cfg, brokers, run_now, state["moves"], log, state=state, dirty=keys)


def market_key(c: Contract) -> str:
    """The feed key of a contract's market: Polymarket slug, Kalshi ticker (what on_update reports)."""
    return c.ref["slug"] if c.venue == "Polymarket" else c.ref["ticker"]


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


_cash: dict = {"at": 0.0, "values": None, "dirty": False}
CASH_TTL = 20.0        # s: Kalshi rate-limits balance reads per account, shared with any other bot on it
CASH_STALE_OK = 120.0  # s: on a failed read, an older balance still caps the size


def balances(brokers: dict, force: bool = False) -> dict[str, float]:
    """Venue cash, read at most every CASH_TTL seconds. After a trade spend() adjusts the cached pots from the
    fills and marks them dirty: the next call re-reads, but a failed re-read (Kalshi 429) still returns the
    adjusted copy while it is under CASH_STALE_OK old -- zeroing `at` instead (until 11-03) made that copy
    unusable and one 429 right after a trade dropped every remaining arb of the evaluation."""
    now = time.time()
    if not force and not _cash["dirty"] and _cash["values"] is not None and now - _cash["at"] < CASH_TTL:
        return _cash["values"]
    try:
        vals = {v: b.balance() for v, b in brokers.items()}
    except Exception:
        if _cash["values"] is not None and now - _cash["at"] < CASH_STALE_OK:
            return _cash["values"]
        raise
    _cash.update(at=now, values=vals, dirty=False)
    return vals


def spend(brokers: dict, opp, fills: list[dict]) -> None:
    """Debit a trade's fills from the cached venue pots and the Kalshi shard pots, then mark the cache dirty."""
    vals = _cash["values"]
    shards = getattr(brokers.get("Kalshi"), "shards", None)
    legs = {(x.venue, x.label): x for x in (opp.a, opp.b)}
    for f in fills:
        q = f.get("qty") or 0
        if not q:
            continue
        cash = -(q * f["avg_price"] + f["fee"]) if f["action"] == "buy" else q * f["avg_price"] - f["fee"]
        if vals is not None and f["venue"] in vals:
            vals[f["venue"]] = max(0.0, vals[f["venue"]] + cash)
        leg = legs.get((f["venue"], f["label"]))
        if f["venue"] == "Kalshi" and isinstance(shards, dict) and leg is not None and leg.ref.get("shard") is not None:
            sh = int(leg.ref["shard"])
            if sh in shards:
                shards[sh] = max(0.0, shards[sh] + cash)
    _cash["dirty"] = True


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


def _tick(db, cfg: Config, brokers: dict, days: float, log, state: dict | None = None):
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
    if state is not None:
        # what the event trigger needs between scans: the games, and which game each market belongs to
        state["games"], state["moves"] = games, moves
        state["index"] = {market_key(c): g for g in games for c in g.contracts}
        live = {g.key for g in games}
        for d in (state["due"], state["last_eval"]):
            for gk in [k for k in d if k not in live]:
                d.pop(gk)
    return _evaluate(db, cfg, brokers, games, moves, log, scan_errors=snap.errors, state=state)


_skip_said: dict[str, float] = {}   # opp key -> last time its stale-Polymarket skip was logged


def _evaluate(db, cfg: Config, brokers: dict, games, moves, log, scan_errors=None, state: dict | None = None,
              dirty: set[str] | None = None):
    """Price `games` from the live books and trade what qualifies. scan_errors is not None on a full scan
    (which logs its status line); state is the event trigger's (arbs confirm before trading) or None;
    dirty = the market keys whose book change triggered this check (event path), used to pick leg 1."""
    mode = "live" if cfg.live else "dry"
    feed = polymarket._feed
    if feed is not None:
        # The Polymarket books were copied from the feed at the START of the scan; the Kalshi REST
        # fetch then takes 1-3 s (11-03: every miss was priced from a 1.3-3.6 s old Polymarket book).
        # Re-read them from the feed now: memory only, no network.
        polymarket.refresh([c for g in games for c in g.contracts if c.venue == "Polymarket"])
    if kalshi._feed is not None and kalshi._feed.alive():
        # same for Kalshi when its books are live (otherwise they are this scan's REST reads)
        kalshi.refresh([c for g in games for c in g.contracts if c.venue == "Kalshi"])
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
    if scan_errors is not None:
        log(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {len(games)} games · "
            f"{sum(len(g.contracts) for g in games):,} contracts · {len(fresh)} arb(s) ≥ {cfg.min_roi * 100:.2f}%"
            + stale_note + feed_note + (" · " + "; ".join(scan_errors) if scan_errors else ""))
    # which venue of each pair just moved: the OTHER one holds the stale quote and is leg 1 (first_leg)
    changed_of = {o.key: {x.venue for x in (o.a, o.b) if market_key(x) in dirty} for o in fresh} if dirty else {}
    if state is not None:
        fresh = _confirmed(cfg, state, games, fresh, t_now, changed_of)
    now_dt = dt.datetime.now(dt.timezone.utc)
    for o in fresh:
        changed = changed_of.get(o.key) or None
        leg1 = o.a if first_leg(o, cfg, moves, changed) == "a" else o.b
        if cfg.stale_pm == "skip" and o.cross_venue and leg1.venue == "Polymarket" and o.game.started(now_dt):
            # the stale quote is Polymarket's: its API takes 85-385 ms and in-play that quote is gone first
            # (38/38 PM-first IOCs expired 11-02/03); a miss here would also start the pair's cooldown
            if state is not None:
                state["stats"]["pm_skipped"] = state["stats"].get("pm_skipped", 0) + 1
            if t_now - _skip_said.get(o.key, 0.0) >= 60:
                _skip_said[o.key] = t_now
                log(f"  skip {o.game.title}: stale leg is Polymarket ({leg1.label}; moved: "
                    f"{', '.join(sorted(changed)) if changed else 'unknown'}), PM-first does not fill in-play")
            continue
        if recently_taken(db, o.key, cfg.cooldown_min):
            continue
        room_day = cfg.max_daily_spend - spent_today(db, mode)
        room_open = cfg.max_open - open_exposure(db, mode)
        try:
            cash = balances(brokers)
        except Exception as e:
            log(f"! balance check failed: {e}")
            return
        # Each leg is paid from its own pot -- a Kalshi leg from its market's shard -- and legs are rarely
        # equal halves of the pair (0.85 + 0.12), so each pot caps ITS leg's outlay, not just the pair's.
        shards = getattr(brokers.get("Kalshi"), "shards", None)
        pots = {}
        for k, leg in (("a", o.a), ("b", o.b)):
            pot = cash[leg.venue]
            if leg.venue == "Kalshi" and shards is not None and leg.ref.get("shard") is not None:
                pot = shards.get(int(leg.ref["shard"]), 0.0)
            pots[k] = pot * 0.95
        cross = o.a.venue != o.b.venue
        cash_cap = sum(pots.values()) if cross else min(pots.values())
        leg_budgets = pots if cross else None
        budget = min(cfg.max_per_trade, room_day, room_open, cash_cap)
        if budget < 5 or (cross and min(pots.values()) < 1):
            log(f"  skip {o.game.title}: limit reached (day ${room_day:.0f}, open ${room_open:.0f}, cash ${cash_cap:.0f}"
                + (f", legs ${pots['a']:.0f}/${pots['b']:.0f}" if cross else "") + ")")
            continue
        try:
            done = execute(db, o, brokers, cfg, budget, log, moves=moves, leg_budgets=leg_budgets, changed=changed)
        except Exception as e:
            done = True
            log(f"! execution error on {o.game.title}: {type(e).__name__}: {e}")
        if done:
            # debit the cached pots from the fills and re-read at the next call; a 429 then falls back to them
            spend(brokers, o, json.loads(done["fills"]) if isinstance(done, dict) and done.get("fills") else [])
        if recent_failures(db, mode, cfg.max_failures) >= cfg.max_failures:
            return "halt"
    return None


def _confirmed(cfg: Config, state: dict, games, fresh, now: float, changed_of: dict | None = None) -> list:
    """Arbs seen for at least cfg.confirm_s on the event path. A first sighting is parked and its game
    re-checked when the confirm time is up; an arb that is gone at that look was a blip. The venues that
    moved at the FIRST sighting (what created the arb) are kept and handed back in `changed_of` on confirm."""
    confirm, out = state["confirm"], []
    changed_of = changed_of if changed_of is not None else {}
    here = {o.key for o in fresh}
    checked = {g.key for g in games}
    for k in [k for k, v in confirm.items() if v[1] in checked and k not in here]:
        confirm.pop(k)                                   # parked arb of a re-checked game that vanished
        state["stats"]["blips"] += 1
    for o in fresh:
        first = confirm.get(o.key)
        if first is None:
            confirm[o.key] = (now, o.game.key, set(changed_of.get(o.key) or ()))
            state["stats"]["seen"] += 1
            state["due"][o.game.key] = min(state["due"].get(o.game.key, float("inf")), now + cfg.confirm_s)
        elif now - first[0] >= cfg.confirm_s:
            confirm.pop(o.key)
            state["stats"]["confirmed"] += 1
            if len(first) > 2 and first[2]:
                changed_of[o.key] = first[2]
            out.append(o)
    return out
