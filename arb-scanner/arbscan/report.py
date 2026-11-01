"""Terminal output."""
from __future__ import annotations

import datetime as dt

from .engine import Opportunity
from .model import Contract, Game
from .scanner import Snapshot

LADDER = (100, 1_000, 10_000)


def cents(p: float | None) -> str:
    return "—" if p is None else f"{p * 100:.1f}¢"


def money(x: float | None, sign: bool = False) -> str:
    if x is None:
        return "—"
    return f"{'+' if sign and x >= 0 else '-' if x < 0 else ''}${abs(x):,.2f}"


def compact(v: float | None, venue: str) -> str:
    """Kalshi reports volume in contracts, Polymarket in dollars."""
    if not v:
        return "—"
    for div, suf in ((1e6, "M"), (1e3, "k"), (1, "")):
        if v >= div:
            n = f"{v / div:.1f}{suf}" if div > 1 else f"{v:.0f}"
            return f"{n} ct" if venue == "Kalshi" else f"${n}"
    return "—"


def local(t: dt.datetime | None, fmt="%a %b %d %I:%M %p") -> str:
    return t.astimezone().strftime(fmt) if t else "?"


# ---------------------------------------------------------------- liquidity

def main_line(g: Game, venue: str, kind: str) -> Contract | None:
    cs = [c for c in g.contracts if c.venue == venue and c.kind == kind and c.side == "gt"
          and c.book.best_ask is not None and not c.how.startswith("NO")]
    if not cs:
        return None
    return min(cs, key=lambda c: abs(c.book.best_ask - 0.5))


def liquidity_rows(g: Game):
    rows = []
    for venue in ("Kalshi", "Polymarket"):
        for kind, name in (("ml", "Moneyline"), ("spread", "Spread"), ("total", "Total")):
            c = main_line(g, venue, kind)
            n_lines = len({x.line for x in g.contracts if x.venue == venue and x.kind == kind})
            if not c:
                rows.append((venue, name, None, 0, None, None, None, None, None))
                continue
            q2, n2 = c.book.depth_within(2)
            spread = (c.book.best_ask - c.book.best_bid) if c.book.best_bid is not None else None
            vol = sum((x.volume_24h or 0) for x in g.contracts if x.venue == venue and x.kind == kind
                      and (x.ref.get("side") in (None, "yes")) and x.ref.get("outcome", 0) == 0)
            rows.append((venue, name, c, n_lines, c.book.best_bid, c.book.best_ask, spread, n2, vol))
    return rows


# ---------------------------------------------------------------- text

def text(snap: Snapshot, arbs: list[Opportunity], closest: list[Opportunity]) -> str:
    out = [f"=== {local(snap.taken_at, '%H:%M:%S')} · {len(snap.games)} games · "
           f"{sum(len(g.contracts) for g in snap.games)} contracts · {snap.seconds:.1f}s ==="]
    for e in snap.errors:
        out.append("! " + e)
    if not arbs:
        out.append("No arbs after fees. Closest:")
        for o in closest[:5]:
            out.append(f"  {o.game.title:<10} {o.top_roi * 100:+.2f}%  {o.a.label} ({o.a.venue} {cents(o.a.book.best_ask)})"
                       f" + {o.b.label} ({o.b.venue} {cents(o.b.book.best_ask)})")
        return "\n".join(out)
    out.append(f"{len(arbs)} ARB(S):")
    for o in arbs:
        s = o.max
        out.append(f"\n  {o.game.title}  {local(o.game.start)}  top-of-book ROI {o.top_roi * 100:.2f}%")
        for leg, c in (("a", o.a), ("b", o.b)):
            out.append(f"    {c.label:<12} {c.venue:<10} {c.how}  ask {cents(c.book.best_ask)}"
                       f"  fee {cents(c.fee.per_contract(c.book.best_ask))}/ct  avg fill {cents(s.avg_price(leg))}")
        out.append(f"    max size {s.qty:,.0f} pairs · outlay {money(s.outlay)} (fees {money(s.fees)}) · "
                   f"locked profit {money(s.profit, True)} ({s.roi * 100:.2f}%)")
        ladder = []
        for d in LADDER:
            r = o.at_outlay(d)
            ladder.append(f"${d:,}: {(money(r.profit, True) + (' (max)' if r.qty >= s.qty else '')) if r else '—'}")
        out.append("    at size → " + " · ".join(ladder))
        if o.middle:
            out.append(f"    middle: {o.middle_text()}")
    return "\n".join(out)
