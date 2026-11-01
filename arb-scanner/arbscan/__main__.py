"""Command line.

    python3 -m arbscan run            auto-arb loop (dry run until you add --live)
    python3 -m arbscan check          verify API keys + show balances (no trading)
    python3 -m arbscan status         settle finished games, print P&L
    python3 -m arbscan scan           one scan, print arbs and closest misses
    python3 -m arbscan liquidity      bid/ask, spread and depth per game
    python3 -m arbscan fees           fee formulas and tables
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys

from . import engine, fees, report, scanner, trader


def _log_to(path: str | None):
    fh = open(path, "a", encoding="utf-8") if path else None

    def log(msg: str = ""):
        print(msg, flush=True)
        if fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
            fh.flush()
    return log


def cmd_run(a):
    cfg = trader.Config(
        live=a.live, max_per_trade=a.max_per_trade, max_daily_spend=a.max_daily, max_open=a.max_open,
        min_roi=a.min_roi, min_profit=a.min_profit, slippage=a.slippage, hedge_attempts=a.hedge_attempts,
        on_fail=a.on_fail, cooldown_min=a.cooldown, include_live_games=a.include_live_games,
        cross_venue_only=a.cross_venue_only, max_failures=a.max_failures, kill_file=a.kill_file)
    log = _log_to(a.log)
    try:
        brokers = trader.make_brokers(cfg, kalshi_demo=a.kalshi_demo)
    except RuntimeError as e:
        sys.exit(f"Cannot start live trading: {e}")
    db = trader.connect(a.db)
    if cfg.live:
        bal = {v: b.balance() for v, b in brokers.items()}
        log("=" * 72)
        log("LIVE TRADING — real orders will be placed.")
        log(f"Balances: Kalshi ${bal['Kalshi']:,.2f} · Polymarket ${bal['Polymarket']:,.2f}")
    else:
        log("DRY RUN — fills are simulated against live order books; nothing is sent. Add --live to trade.")
    log(f"Limits: ${cfg.max_per_trade:,.0f}/arb · ${cfg.max_daily_spend:,.0f}/day · ${cfg.max_open:,.0f} open · "
        f"min ROI {cfg.min_roi * 100:.2f}% · min profit ${cfg.min_profit:.2f} · on failed hedge: {cfg.on_fail} · "
        f"{'pre-game + live games' if cfg.include_live_games else 'pre-game only'}")
    log(f"Stop: Ctrl-C, or create a file named '{cfg.kill_file}' here. Ledger: {a.db}")
    try:
        trader.run(db, cfg, brokers, a.days, a.interval, once=a.once, log=log)
    except KeyboardInterrupt:
        log("\nstopped.")
    log(trader.summary_text(trader.summary(db, "live" if cfg.live else "dry")))


def cmd_check(a):
    ok = True
    for name, make in (("Kalshi", lambda: __import__("arbscan.brokers", fromlist=["x"]).KalshiBroker(demo=a.kalshi_demo)),
                       ("Polymarket", lambda: __import__("arbscan.brokers", fromlist=["x"]).PolymarketBroker())):
        try:
            b = make()
            print(f"✔ {name}: connected, available ${b.balance():,.2f}")
        except Exception as e:
            ok = False
            print(f"✖ {name}: {e}")
    sys.exit(0 if ok else 1)


def cmd_status(a):
    db = trader.connect(a.db)
    trader.settle(db)
    for mode in ("live", "dry"):
        s = trader.summary(db, mode)
        if s["seen"]:
            print(f"{mode.upper():>4}: {trader.summary_text(s)}")
    rows = trader.summary(db)["rows"][: a.last]
    for r in rows:
        la, lb = json.loads(r["leg_a"]), json.loads(r["leg_b"])
        pnl = r["pnl"] if r["pnl"] is not None else r["locked_profit"]
        print(f"  #{r['id']:<4} {r['mode']:<4} {dt.datetime.fromtimestamp(r['detected_at']).strftime('%m-%d %H:%M:%S')} "
              f"{r['game']:<10} {la['label']} {la['venue'][0]} + {lb['label']} {lb['venue'][0]:<3} "
              f"{r['outcome']:<8} {r['status']:<8} ${r['cash_out'] or 0:>8,.2f} "
              f"{('%+.2f' % pnl) if pnl is not None else '':>8}  {r['note'] or ''}")


def cmd_scan(a):
    snap = scanner.snapshot(a.days)
    arbs, closest = engine.find(snap.games, a.min_roi, a.cross_venue_only, near=0.03)
    print(report.text(snap, engine.non_overlapping(arbs), closest))


def cmd_liquidity(a):
    snap = scanner.snapshot(a.days)
    print(f"{'GAME':<10} {'VENUE':<10} {'MARKET':<9} {'MAIN LINE':<11} {'BID/ASK':>13} {'SPRD':>5} "
          f"{'DEPTH≤2¢':>11} {'LINES':>5} {'24H VOL':>9}")
    for g in snap.games:
        for venue, name, c, n, bid, ask, spr, dep, vol in report.liquidity_rows(g):
            if not c:
                continue
            print(f"{g.title:<10} {venue:<10} {name:<9} {c.label:<11} {report.cents(bid):>6}/{report.cents(ask):<6} "
                  f"{report.cents(spr):>5} {report.money(dep):>11} {n:>5} {report.compact(vol, venue):>9}")


def cmd_fees(a):
    k = fees.kalshi_model(None, None)
    p = fees.FeeModel("Polymarket", a.poly_rate, round_fee_to=0.00001)
    print(f"Kalshi taker:     {k.describe()}")
    print(f"Polymarket taker: {p.describe()}  (makers pay 0)\n")
    print(f"{'price':>6} {'Kalshi/100':>11} {'%':>6} {'Poly/100':>9} {'%':>6} {'pair break-even':>16}")
    for (pr, kf, kp), (_, pf, pp) in zip(fees.fee_table(k), fees.fee_table(p)):
        be = (k.per_contract(pr) + p.per_contract(1 - pr)) * 100
        print(f"{pr * 100:5.0f}¢ {kf:11.2f} {kp * 100:5.2f}% {pf:9.2f} {pp * 100:5.2f}% {be:14.2f}¢")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="arbscan", description="Automated NFL arbitrage between Kalshi and Polymarket "
                                 "(moneyline, spreads, totals).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="auto-arb loop (dry run unless --live)")
    r.add_argument("--live", action="store_true", help="place REAL orders (needs API keys in env)")
    r.add_argument("--max-per-trade", type=float, default=100, help="$ cap per arb, both legs (default 100)")
    r.add_argument("--max-daily", type=float, default=500, help="$ spend cap per UTC day (default 500)")
    r.add_argument("--max-open", type=float, default=1000, help="$ cap in unsettled trades (default 1000)")
    r.add_argument("--min-roi", type=float, default=0.005, help="min ROI after fees (default 0.005 = 0.5%%)")
    r.add_argument("--min-profit", type=float, default=0.25, help="min $ locked per arb (default 0.25)")
    r.add_argument("--slippage", type=float, default=0.01, help="extra room on leg-1 limit, $ (default 0.01)")
    r.add_argument("--hedge-attempts", type=int, default=3)
    r.add_argument("--on-fail", choices=["unwind", "hold"], default="unwind",
                   help="if leg 2 can't fill: sell leg 1 back (default) or keep it")
    r.add_argument("--include-live-games", action="store_true",
                   help="also trade games already in progress (fastest-moving, highest legging risk)")
    r.add_argument("--cross-venue-only", action="store_true")
    r.add_argument("--cooldown", type=float, default=10, help="minutes before re-taking the same pair")
    r.add_argument("--max-failures", type=int, default=2, help="halt after N failed hedges in a row")
    r.add_argument("--interval", type=float, default=15, help="seconds between scans (default 15)")
    r.add_argument("--days", type=float, default=8)
    r.add_argument("--db", default="trades.db")
    r.add_argument("--log", default="arbscan.log")
    r.add_argument("--kill-file", default="STOP")
    r.add_argument("--kalshi-demo", action="store_true", help="send Kalshi orders to the demo exchange")
    r.add_argument("--once", action="store_true", help="single scan then exit")
    r.set_defaults(fn=cmd_run)

    c = sub.add_parser("check", help="verify API keys and show balances")
    c.add_argument("--kalshi-demo", action="store_true")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("status", help="settle finished games and show the trade ledger")
    s.add_argument("--db", default="trades.db")
    s.add_argument("--last", type=int, default=20)
    s.set_defaults(fn=cmd_status)

    sc = sub.add_parser("scan", help="one scan: arbs and closest misses")
    sc.add_argument("--days", type=float, default=8)
    sc.add_argument("--min-roi", type=float, default=0.0)
    sc.add_argument("--cross-venue-only", action="store_true")
    sc.set_defaults(fn=cmd_scan)

    lq = sub.add_parser("liquidity", help="main-line bid/ask, spread and depth per game")
    lq.add_argument("--days", type=float, default=3)
    lq.set_defaults(fn=cmd_liquidity)

    f = sub.add_parser("fees", help="fee formulas and tables")
    f.add_argument("--poly-rate", type=float, default=0.03)
    f.set_defaults(fn=cmd_fees)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
