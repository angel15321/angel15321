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
import signal
import sys

from . import engine, fees, hours, kalshi, kalshiws, notify, pmws, polymarket, report, scanner, trader


def _log_to(path: str | None):
    fh = open(path, "a", encoding="utf-8") if path else None

    def log(msg: str = ""):
        print(msg, flush=True)
        if fh:
            fh.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {msg}\n")
            fh.flush()
    return log


def _attach_feed(log=print) -> bool:
    """Use live Polymarket US books from the websocket when the API key is in the environment."""
    feed = pmws.BookFeed.from_env()
    if feed is None:
        log("Polymarket books: REST snapshots (cached, can lag 10-30 s). Set POLYMARKET_KEY_ID / "
            "POLYMARKET_SECRET_KEY for live websocket books.")
        return False
    polymarket.use_feed(feed)
    log("Polymarket books: live websocket")
    try:
        kfeed = kalshiws.BookFeed.from_env()
    except Exception as e:  # a bad key path must not stop the Polymarket side
        kfeed = None
        log(f"Kalshi websocket not started: {type(e).__name__}: {e}")
    if kfeed is None:
        log("Kalshi books: REST each scan (set KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH for live websocket books)")
    else:
        kalshi.use_feed(kfeed)
        log("Kalshi books: live websocket (REST only for a market's first read)")
    return True


def cmd_run(a):
    cfg = trader.Config(
        live=a.live, max_per_trade=a.max_per_trade, max_daily_spend=a.max_daily, max_open=a.max_open,
        min_roi=a.min_roi, min_profit=a.min_profit, slippage=a.slippage, hedge_attempts=a.hedge_attempts,
        on_fail=a.on_fail, unwind_max_loss=a.unwind_max_loss, cooldown_min=a.cooldown, include_live_games=a.include_live_games or a.in_play_only,
        in_play_only=a.in_play_only, leg1=a.leg1,
        cross_venue_only=a.cross_venue_only, max_failures=a.max_failures, kill_file=a.kill_file,
        kill_trade_loss=a.kill_trade_loss, kill_day_loss=a.kill_day_loss, sports=a.sports,
        legs=a.legs, max_book_age=a.max_book_age, trigger=a.trigger, event_gap=a.event_gap_ms / 1000,
        confirm_s=a.confirm_ms / 1000, stale_pm=a.stale_pm, hedge_max_loss=a.hedge_max_loss,
        maker=a.maker, maker_edge=a.maker_edge_c / 100, maker_ttl=a.maker_ttl,
        maker_in_play=(tuple(a.sports) if a.maker_in_play == "all" else a.maker_in_play or ()),
        maker_live_edge=a.maker_live_edge_c / 100, maker_live_quotes=a.maker_live_quotes,
        rescue=not a.no_rescue, rescue_edge=a.rescue_edge_c / 100, rescue_sell=a.rescue_sell_c / 100,
        maker_horizon_h=a.maker_horizon_h, maker_max_qty=a.maker_max_qty, maker_quotes=a.maker_quotes,
        scan_thread=not a.scan_inline)
    log = _log_to(a.log)
    try:
        brokers = trader.make_brokers(cfg, kalshi_demo=a.kalshi_demo)
    except RuntimeError as e:
        sys.exit(f"Cannot start live trading: {e}")
    db = trader.connect(a.db)
    if cfg.live:
        bal = trader.startup_balances(brokers, log=log)
        log("=" * 72)
        log("LIVE TRADING — real orders will be placed.")
        shards = getattr(brokers["Kalshi"], "shards", None)
        by_shard = (" (" + ", ".join(f"shard {i} ${v:,.2f}" for i, v in sorted(shards.items())) + ")") if shards else ""
        log(f"Balances: Kalshi ${bal['Kalshi']:,.2f}{by_shard} · Polymarket US ${bal['Polymarket']:,.2f}")
    else:
        log("DRY RUN — fills are simulated against live order books; nothing is sent. Add --live to trade.")
    log(f"Limits: ${cfg.max_per_trade:,.0f}/arb · ${cfg.max_daily_spend:,.0f}/day · ${cfg.max_open:,.0f} open · "
        f"{f'loss kill ${cfg.kill_trade_loss:,.0f}/trade · ' if cfg.kill_trade_loss else ''}"
        f"{f'loss kill ${cfg.kill_day_loss:,.0f}/day · ' if cfg.kill_day_loss else ''}"
        f"min ROI {cfg.min_roi * 100:.2f}% · min profit ${cfg.min_profit:.2f} · on failed hedge: {cfg.on_fail}"
        f"{f' (loss <= fees + ${cfg.unwind_max_loss:.2f}/ct, else hold)' if cfg.on_fail == 'unwind' else ''} · "
        f"{'in-play only' if cfg.in_play_only else 'pre-game + live games' if cfg.include_live_games else 'pre-game only'}"
        f" · legs: {cfg.legs}{'' if cfg.legs == 'parallel' else f' (leg 1: {cfg.leg1})'}"
        f"{' · stale Polymarket leg: skip' if cfg.stale_pm == 'skip' else ' · stale Polymarket leg: try first'}"
        f"{f' · hedge may pay {cfg.hedge_max_loss * 100:.1f} c/ct over break-even' if cfg.hedge_max_loss else ''}"
        f" · Kalshi books <= {cfg.max_book_age:g}s old"
        + (f" · trigger: every book change (confirm {cfg.confirm_s * 1000:.0f} ms, per game <= 1/{cfg.event_gap * 1000:.0f} ms)"
           if cfg.trigger == "event" else f" · trigger: scan every {max(1.0, a.interval):g}s")
        + f" · sports: {', '.join(cfg.sports)}"
        + (f" · held legs: late hedge at >= {cfg.rescue_edge * 100:.0f} c/pair locked or sell back at >= "
           f"{cfg.rescue_sell * 100:.0f} c/ct profit" if cfg.rescue else " · held legs: ride to settlement")
        + (f" · MAKER: up to {cfg.maker_quotes} post-only Polymarket bids (1/game), edge >= {cfg.maker_edge * 100:.1f} c/pair, hedge on Kalshi, "
           f"pre-game (<= {cfg.maker_horizon_h:g} h)"
           + (f" + IN-PLAY {', '.join(cfg.maker_in_play)} (edge >= {cfg.maker_live_edge * 100:.1f} c/pair, <= "
              f"{cfg.maker_live_quotes} live quotes, pulled on any Kalshi tick against the hedge)" if cfg.maker_in_play else "")
           + ", "
           f"re-price by modify <= every {cfg.maker_reprice:g}s, refresh {cfg.maker_ttl:g}s; taker arbs OFF" if cfg.maker else ""))
    if not _attach_feed(log) and cfg.live:
        sys.exit("Live trading needs the Polymarket US websocket (POLYMARKET_KEY_ID / POLYMARKET_SECRET_KEY): "
                 "the REST books lag the exchange by 10-30 s.")
    if cfg.maker:
        of = pmws.OrderFeed.from_env() if cfg.live else None
        polymarket._orders = of
        log("Polymarket order updates: private websocket (REST poll every 1 s as backup)" if of is not None
            else "Polymarket order updates: REST poll every 1 s")
        hours.start()               # venue hours: its own thread reads Kalshi's exchange status and schedule
        log(hours.describe())
    # systemd stops the bot with SIGTERM: turn it into the Ctrl-C path so a resting quote is cancelled on the way out
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    log(f"Stop: Ctrl-C, or create a file named '{cfg.kill_file}' here. Ledger: {a.db}")
    if cfg.live:
        notify.start(log)           # hedge outcomes to Telegram, late and off the trading thread (notify.py)
    try:
        trader.run(db, cfg, brokers, a.days, a.interval, once=a.once, log=log)
    except KeyboardInterrupt:
        log("\nstopped.")
    notify.flush()                  # the sender thread dies with the process: send what is queued (a halt line)
    log(trader.summary_text(trader.summary(db, "live" if cfg.live else "dry")))


def cmd_check(a):
    ok = True
    for name, make in (("Kalshi", lambda: __import__("arbscan.brokers", fromlist=["x"]).KalshiBroker(demo=a.kalshi_demo)),
                       ("Polymarket US", lambda: __import__("arbscan.brokers", fromlist=["x"]).PolymarketBroker())):
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


def cmd_ack(a):
    db = trader.connect(a.db)
    n = trader.ack_failures(db, "dry" if a.dry else "live")
    print(f"acknowledged {n} failed or losing trade(s); the halt streak and the loss kill start again from zero. "
          "Held positions are unchanged and still settle.")


def cmd_scan(a):
    _attach_feed()
    snap = scanner.snapshot(a.days, sports=a.sports)
    arbs, closest = engine.find(snap.games, a.min_roi, a.cross_venue_only, near=0.03)
    print(report.text(snap, engine.non_overlapping(arbs), closest))


def cmd_liquidity(a):
    _attach_feed()
    snap = scanner.snapshot(a.days, sports=a.sports)
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
    p = fees.FeeModel("Polymarket", a.poly_rate, round_fee_to=0.01)
    print(f"Kalshi taker:     {k.describe()}")
    print(f"Polymarket taker: {p.describe()}  (makers pay 0)\n")
    print(f"{'price':>6} {'Kalshi/100':>11} {'%':>6} {'Poly/100':>9} {'%':>6} {'pair break-even':>16}")
    for (pr, kf, kp), (_, pf, pp) in zip(fees.fee_table(k), fees.fee_table(p)):
        be = (k.per_contract(pr) + p.per_contract(1 - pr)) * 100
        print(f"{pr * 100:5.0f}¢ {kf:11.2f} {kp * 100:5.2f}% {pf:9.2f} {pp * 100:5.2f}% {be:14.2f}¢")


SPORTS = ("nfl", "nba", "nhl", "tennis", "soccer", "cfb", "cbb", "ufc")


def _sports(text: str) -> tuple[str, ...]:
    out = tuple(dict.fromkeys(s.strip().lower() for s in text.split(",") if s.strip()))
    bad = [s for s in out if s not in SPORTS]
    if bad or not out:
        raise argparse.ArgumentTypeError(f"--sports takes a comma list of {', '.join(SPORTS)} (got {text!r})")
    return out


def main(argv=None):
    # Windows consoles default to cp1252, which cannot print the arrows/checkmarks/¢ used below.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog="arbscan", description="Automated NFL arbitrage between Kalshi and Polymarket US "
                                 "(moneylines and full-game spreads).")
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
    r.add_argument("--unwind-max-loss", type=float, default=0.0,
                   help="$/contract a sell-back may lose beyond the fees (default 0: only sell at or above the price "
                        "paid); a lower bid keeps leg 1 to settlement")
    r.add_argument("--include-live-games", action="store_true",
                   help="also trade games already in progress (fastest-moving, highest legging risk)")
    r.add_argument("--in-play-only", action="store_true",
                   help="ONLY games in progress; scans just those games, so pair it with a short --interval")
    r.add_argument("--leg1", choices=["auto", "polymarket", "kalshi"], default="auto",
                   help="which venue's leg to buy first; auto = the stale venue in-play, else Polymarket")
    r.add_argument("--legs", choices=["sequential", "parallel"], default="sequential",
                   help="cross-venue pairs: leg 1 first, then leg 2 (default), or both at once -- faster, but a "
                        "quote that vanishes leaves the other leg filled and held")
    r.add_argument("--stale-pm", choices=["skip", "first"], default="skip",
                   help="in-play arb whose stale leg (leg 1) is Polymarket: skip it (default; PM-first IOCs went 0/38, "
                        "its API takes 85-385 ms and the quote is gone first) or try it first")
    r.add_argument("--hedge-max-loss", type=float, default=0.0,
                   help="$/contract leg 2 may pay above break-even to get hedged (default 0: break-even only); a small "
                        "locked loss instead of a one-sided hold")
    r.add_argument("--trigger", choices=["scan", "event"], default="scan",
                   help="scan: price once per scan (default). event: also re-check a game the moment either feed "
                        "changes one of its books (needs both websockets)")
    r.add_argument("--event-gap-ms", type=float, default=50,
                   help="event trigger: re-check one game at most this often (default 50 ms)")
    r.add_argument("--confirm-ms", type=float, default=30,
                   help="event trigger: an arb must still be there this long after it first appears (default 30 ms)")
    r.add_argument("--scan-inline", action="store_true",
                   help="event trigger: run the full scan on the trading thread again (default: its own thread, so a "
                        "scan never delays pulling a quote)")
    r.add_argument("--maker", action="store_true",
                   help="MAKER mode: rest one post-only Polymarket bid priced off Kalshi's ask and hedge on Kalshi when it "
                        "fills, instead of taking arbs. Pre-game only unless --maker-in-play")
    r.add_argument("--maker-edge-c", type=float, default=1.0,
                   help="maker: cents per pair the quote must make after both venues' fees (default 1)")
    r.add_argument("--maker-ttl", type=float, default=120, help="maker: re-price a quote after this many seconds (default 120)")
    r.add_argument("--maker-in-play", nargs="?", const="all", default=None,
                   type=lambda t: t if t == "all" else _sports(t),
                   help="maker: also quote games in progress -- a comma list of sports (e.g. nhl,tennis,nba,nfl), or "
                        "no value for every --sports. Riskier: news moves Kalshi before a Polymarket cancel lands")
    r.add_argument("--maker-live-edge-c", type=float, default=4.0,
                   help="maker: cents per pair a quote on a game in progress must make after fees (default 4)")
    r.add_argument("--maker-live-quotes", type=int, default=2,
                   help="maker: at most this many of the quote slots on games in progress (default 2)")
    r.add_argument("--maker-horizon-h", type=float, default=24, help="maker: quote games starting within N hours (default 24)")
    r.add_argument("--maker-quotes", type=int, default=3,
                   help="maker: quotes resting at once, one per game; the day's room is split across them (default 3)")
    r.add_argument("--maker-max-qty", type=float, default=0, help="maker: contracts per quote cap (default 0 = $ caps only)")
    r.add_argument("--no-rescue", action="store_true",
                   help="do not watch held (unhedged) legs for a profitable late hedge / sell-back")
    r.add_argument("--rescue-edge-c", type=float, default=1.0,
                   help="held leg: late-hedge when the pair then locks at least this many cents a pair (default 1)")
    r.add_argument("--rescue-sell-c", type=float, default=3.0,
                   help="held leg: sell it back when that clears at least this many cents a contract over cost (default 3)")
    r.add_argument("--max-book-age", type=float, default=1.0,
                   help="skip an arb priced from a Kalshi book older than this many seconds (default 1.0); Polymarket books are re-read from the live feed right before pricing")
    r.add_argument("--cross-venue-only", action="store_true")
    r.add_argument("--cooldown", type=float, default=10, help="minutes before re-taking the same pair")
    r.add_argument("--max-failures", type=int, default=2, help="halt after N failed hedges in a row")
    r.add_argument("--kill-trade-loss", type=float, default=0.0,
                   help="halt when one trade realizes a loss above this many $ (default 0 = off)")
    r.add_argument("--kill-day-loss", type=float, default=0.0,
                   help="halt when the UTC day's realized P&L falls below -this many $ (default 0 = off)")
    r.add_argument("--interval", type=float, default=15, help="seconds between scans (default 15)")
    r.add_argument("--days", type=float, default=8)
    r.add_argument("--db", default="trades.db")
    r.add_argument("--log", default="arbscan.log")
    r.add_argument("--kill-file", default="STOP")
    r.add_argument("--kalshi-demo", action="store_true", help="send Kalshi orders to the demo exchange")
    r.add_argument("--once", action="store_true", help="single scan then exit")
    r.add_argument("--sports", type=_sports, default=("nfl",),
                   help="comma list: nfl (moneylines + spreads), nba (moneylines + spreads + totals), nhl (moneylines), tennis (ATP/WTA/Challenger match winners), soccer (EPL/MLS/La Liga/Bundesliga/UCL winners), cfb / cbb (college football / basketball winners), ufc (fight winners); default nfl")
    r.set_defaults(fn=cmd_run)

    c = sub.add_parser("check", help="verify API keys and show balances")
    c.add_argument("--kalshi-demo", action="store_true")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("status", help="settle finished games and show the trade ledger")
    s.add_argument("--db", default="trades.db")
    s.add_argument("--last", type=int, default=20)
    s.set_defaults(fn=cmd_status)

    ak = sub.add_parser("ack", help="acknowledge failed trades after reviewing them, so a halted bot can run again")
    ak.add_argument("--db", default="trades.db")
    ak.add_argument("--dry", action="store_true", help="the dry-run ledger rows instead of live ones")
    ak.set_defaults(fn=cmd_ack)

    sc = sub.add_parser("scan", help="one scan: arbs and closest misses")
    sc.add_argument("--days", type=float, default=8)
    sc.add_argument("--min-roi", type=float, default=0.0)
    sc.add_argument("--cross-venue-only", action="store_true")
    sc.add_argument("--sports", type=_sports, default=("nfl",))
    sc.set_defaults(fn=cmd_scan)

    lq = sub.add_parser("liquidity", help="main-line bid/ask, spread and depth per game")
    lq.add_argument("--days", type=float, default=3)
    lq.add_argument("--sports", type=_sports, default=("nfl",))
    lq.set_defaults(fn=cmd_liquidity)

    f = sub.add_parser("fees", help="fee formulas and tables")
    f.add_argument("--poly-rate", type=float, default=0.0695)
    f.set_defaults(fn=cmd_fees)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
