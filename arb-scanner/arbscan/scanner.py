"""Fetch both venues in parallel and merge into one list of games."""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
from dataclasses import dataclass, field

from . import kalshi, polymarket, tennis, winners
from .model import Contract, Game


@dataclass
class Snapshot:
    games: list[Game]
    errors: list[str] = field(default_factory=list)
    taken_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
    seconds: float = 0.0


def snapshot(days: float = 8, poly_fee: float | None = None, kalshi_direct: bool = False,
             sports: tuple[str, ...] = ("nfl",)) -> Snapshot:
    t0 = dt.datetime.now(dt.timezone.utc)
    horizon = t0 + dt.timedelta(days=days)
    errors: list[str] = []
    results: dict[str, dict[str, Game]] = {}
    jobs = {}
    for sp in ("nfl", "nba", "nhl"):
        if sp in sports:
            jobs[f"Polymarket {sp}"] = lambda sp=sp: polymarket.fetch(horizon, poly_fee, sp)
            jobs[f"Kalshi {sp}"] = lambda sp=sp: kalshi.fetch(horizon, kalshi_direct, sp)
    if "tennis" in sports:
        jobs["Tennis"] = lambda: tennis.fetch(horizon, poly_fee, kalshi_direct)   # both venues, already paired
    for sp in winners.SPORTS:
        if sp in sports:                                                          # paired by name, like tennis
            jobs[f"Paired {sp}"] = lambda sp=sp: winners.fetch(horizon, sp, poly_fee, kalshi_direct)
    with cf.ThreadPoolExecutor(max_workers=len(jobs) or 1) as ex:
        futs = {ex.submit(fn): name for name, fn in jobs.items()}
        for f in cf.as_completed(futs):
            name = futs[f]
            try:
                results[name] = f.result()
            except Exception as exc:
                errors.append(f"{name} fetch failed: {exc}")
                results[name] = {}

    merged: dict[str, Game] = {}
    # Polymarket first: its kickoff times are exact; Kalshi's are estimated.
    for name in [n for n in results if n.startswith("Polymarket")] + [n for n in results if n.startswith("Kalshi")]:
        for key, g in results.get(name, {}).items():
            m = merged.setdefault(key, Game(g.away, g.home, g.start, sport=g.sport))
            if m.start is None:
                m.start = g.start
            m.links.update(g.links)
            m.contracts += g.contracts
    merged.update(results.get("Tennis", {}))
    for sp in winners.SPORTS:
        merged.update(results.get(f"Paired {sp}", {}))
    games = sorted(merged.values(), key=lambda g: (g.start or horizon, g.key))
    return Snapshot(games, errors, t0, (dt.datetime.now(dt.timezone.utc) - t0).total_seconds())


def refresh(contracts: list[Contract]) -> None:
    """Re-fetch order books for just these contracts (both venues)."""
    k = [c for c in contracts if c.venue == "Kalshi"]
    p = [c for c in contracts if c.venue == "Polymarket"]
    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        futs = []
        if k:
            futs.append(ex.submit(kalshi.refresh, k))
        if p:
            futs.append(ex.submit(polymarket.refresh, p))
        for f in futs:
            f.result()


def settlement(c_ref: dict, venue: str) -> float | None:
    if venue == "Kalshi":
        yes = kalshi.settlement(c_ref["ticker"])
        if yes is None:
            return None
        return yes if c_ref["side"] == "yes" else 1 - yes
    return polymarket.settlement(c_ref["slug"], c_ref["side"])
