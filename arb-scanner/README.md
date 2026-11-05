# NFL Auto-Arb — Kalshi × Polymarket US

A bot that finds **arbitrage between Kalshi and Polymarket US on NFL moneylines
and full-game spreads** and **executes both legs automatically**, with exact
venue fees, order-book-aware sizing, and hard risk limits. It can trade
pre-game, in-play, or both.

**It starts in dry-run mode.** It runs every step against live order books
but sends no orders until you add `--live`.

```bash
cd arb-scanner
```

```bash
python -m arbscan run
```

(On macOS/Linux use `python3`.)

## How a trade happens

1. **Scan** (every 15 s, or `--interval`). It pulls every NFL moneyline and full-game
   spread on both venues with full order books. **Polymarket US books stream over its
   markets websocket** (fresh every ~0.1–0.2 s) whenever `POLYMARKET_KEY_ID` /
   `POLYMARKET_SECRET_KEY` are set; an in-play scan then takes ~1 s. Without the key it
   falls back to the REST book endpoint, which is a cached snapshot that can lag the
   exchange by 10–30 s: fine for a look, useless for trading in-play (live mode refuses it).
2. **Find.** It pairs any two contracts that together pay at least $1 on every possible
   score (same line on two venues, middles like KC -2.5 + DEN +3.5, moneyline vs
   spread). A pair counts only if it still profits after both venues' taker fees.
3. **Size.** It walks both order books to the largest profitable size, then caps that
   by your limits and the cash on each venue.
4. **Leg 1.** The leg most likely to vanish goes first, as an immediate-or-cancel order
   with a max price, so nothing rests on the book. **In a game in progress** that is the
   **stale** venue: after a score one venue reprices first and the other's old quotes get
   pulled, so the leg whose price moved least since the last scan goes first. Pre-game
   (or with no history yet) the Polymarket leg goes first. `--leg1 polymarket|kalshi` forces it.
   With `--trigger event` the venue whose book change triggered the check is the FRESH one, so the other
   venue's leg goes first. An in-play arb whose stale leg is Polymarket is skipped by default
   (`--stale-pm skip`): its order API takes 85-385 ms and such quotes were gone every time (0/38 fills).
5. **Leg 2.** It buys exactly what leg 1 filled, as an immediate-or-cancel order capped
   at the **break-even price** (plus `--hedge-max-loss` $/contract if you allow a bounded locked loss),
   and retries up to 3 times; before each retry it checks the feed's in-memory book and stops when the quote is gone.
6. **If the hedge fails,** it sells leg 1 back (`--on-fail unwind`, the default) or
   keeps it (`--on-fail hold`).
   **A held leg is watched from then on** (`rescue.py`): on every scan / book event it is closed the moment that
   locks a profit -- a late hedge of the other leg when the pair then makes at least 1¢ (`--rescue-edge-c`), or a
   sell-back clearing at least 3¢ a contract over cost (`--rescue-sell-c`), whichever pays more. Never at a loss;
   `--no-rescue` turns it off. (11-05 #53: held at 0.54, worth 0.74-0.96 a minute later, settled at -$4.32.)
7. **Log and settle.** Every order goes into `trades.db` and `arbscan.log`. After
   each game it reads the official result and books the real P&L.

## Maker mode (`--maker`)

As a taker the bot lost every race in-play (11-02/03: 50 attempts, 0 hedged; the quotes were pulled before
even a 10 ms Kalshi order landed). `--maker` turns it around: it **rests one post-only bid on Polymarket US**
and lets the fill come to it, then hedges on Kalshi, the fast venue.

- **Price:** for each Polymarket contract and the Kalshi contract covering the other outcomes, the most it
  may pay is `payout - (Kalshi ask + Kalshi taker fee) - edge` (Polymarket makers pay no fee). It bids one
  tick over the best Polymarket bid (its own excluded), never above that ceiling and never at or above the
  ask (post-only: a crossing quote is rejected, not taken). `--maker-edge-c` (default 1¢ a pair).
- **Pull:** the quote is cancelled when the Kalshi ask it is priced from gets worse or thinner, a feed
  stops, the game starts (schedule or Polymarket's live flag), or after `--maker-ttl` (120 s). A new price
  for the same pair is one modify call (`POST /v1/order/{id}/modify`, no gap with nothing resting); the first
  three are read back, and one that does not take switches the run to cancel + post.
- **Several quotes:** up to `--maker-quotes` (3) at once, one per game. Each new quote is sized from the day's
  room the resting ones leave, so if all of them filled together the total still fits `--max-daily`.
- **Fill:** seen on the private order stream (REST poll every 1 s as backup), hedged on Kalshi at once, the
  rest of the quote cancelled, then the usual hedge retries and sell-back-or-hold rule.
- **Safety:** every quote is written to the `quotes` table as soon as its id is known; quotes a crash left open
  are cancelled at the next start (a fill meanwhile becomes an `error` row). Every exit cancels the resting
  quotes, and a cancel that is not confirmed is sent again. Three refused quotes in a row pause maker mode.
  A fill under one Kalshi contract cannot be hedged: it is booked as `dust`, which is not a failed hedge.
- **A post that comes back as an error** (a timeout, a 5xx; a 4xx is a refusal) may have been placed all the
  same. For 30 s the bot looks for an order of its own it never tracked on that market and side -- still open
  (`GET /v1/orders/open`) or seen by the private order stream, which also catches one that already filled --
  takes it over and pulls it; whatever it filled goes through the normal hedge path. No new quote on that
  game meanwhile. At start, an open order the ledger does not know is reported, never cancelled (the
  account may be shared).
- **Speed:** with `--trigger event` the full scan (market lists, game state, first book reads) runs on its own
  thread and the trading thread only applies the finished snapshot, re-reading every book from the live feeds
  (never REST) before it prices anything; `--scan-inline` puts the scan back on the trading thread. If no scan
  finishes for 10 s the game list is dropped and every quote pulled until scans are back. Every minute the log
  shows `trading thread: busy N% · longest block M ms` -- the longest time a quote could not be pulled.
- **Scope:** games starting within `--maker-horizon-h` (24 h). `--maker-in-play nhl,tennis,nba,nfl` (no value =
  every `--sports`) also quotes games in progress of those sports, with a bigger required edge
  (`--maker-live-edge-c`, 4¢ a pair), at most `--maker-live-quotes` (2) of the slots, and a pull on ANY Kalshi
  tick against the hedge (in play a tick is news). A pre-game quote never carries into play: it is pulled at
  the start. Riskier: news moves Kalshi before a Polymarket cancel lands. The taker path is off while it runs.
- **Soccer, college football / basketball, UFC** (`--sports soccer,cfb,cbb,ufc`, `winners.py`): winner markets
  paired by NAME -- the game's date (or the day before: Kalshi uses the US day) and BOTH sides' names must map
  one-to-one onto exactly one Kalshi event, and a pair whose two venues price the same side more than 15¢ apart
  is dropped. "X wins" + "X does not win" (or both "does not win" sides) cover every result, a draw included;
  the soccer draw market itself is not used.
- **NHL** (`--sports nhl`): moneylines only. A shootout adds a goal to the final score and whether each
  venue's puck line / total counts it is not checked, so those are not paired.

## Setup for live trading

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Fill in `.env` with:

- **Kalshi:** your API key ID and the path to the `.pem` file you downloaded.
  Get them at kalshi.com → Account & security → API Keys.
- **Polymarket US:** a Key ID and Secret Key from **polymarket.us/developer**
  (sign in the same way as in the app; the secret is shown only once).

**Recommended: a Kalshi subaccount of its own.** If the same Kalshi account runs another
bot, `tools/kalshi_subaccount_setup.py` (run where that account's key already lives) creates
a numbered subaccount, moves a set amount into it, and generates an API key locked to it. The
arb bot then trades only that cash, and the key cannot move money or touch the rest of the
account. It does nothing without `--execute` and never retries a transfer.

Then load the keys and check that both accounts connect. This places no orders.

bash:

```bash
set -a && . ./.env && set +a
```

PowerShell:

```powershell
Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' } | ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item "env:$k" $v }
```

```bash
python -m arbscan check
```

Run the dry run for a while first. Once its results look right, go live with small limits:

```bash
python -m arbscan run --live --max-per-trade 20 --max-daily 25
```

In-play only, scanning faster:

```bash
python -m arbscan run --live --in-play-only --interval 3 --max-per-trade 20 --max-daily 25
```

NFL and tennis together (`--sports`, default `nfl`):

```bash
python -m arbscan run --live --in-play-only --interval 3 --max-per-trade 20 --max-daily 25 --sports nfl,tennis
```

**Tennis** = ATP, WTA and ATP Challenger match winners (Polymarket US has no ITF). A match is paired
across venues by date + both players' 3-letter codes + the last name, and only when exactly one
Kalshi event fits. In-play for tennis follows Polymarket's own live flag, not the schedule slot
(matches often start hours late). The limits are shared with NFL. A retirement settles both venues
to the player who advances; a walkover (match never started) is not checked here, which is one more
reason to keep tennis `--in-play-only`.

`--kalshi-demo` sends the Kalshi side to Kalshi's demo exchange instead.

## Risk controls (all on by default)

| Control | Default | Flag |
| --- | --- | --- |
| Dry run unless told otherwise | on | `--live` |
| Max $ per arb (both legs) | $100 | `--max-per-trade` |
| Max $ spent per UTC day | $500 | `--max-daily` |
| Max $ in unsettled trades | $1,000 | `--max-open` |
| Minimum ROI after fees | 0.5% | `--min-roi` |
| Minimum $ locked per arb | $0.25 | `--min-profit` |
| Pre-game only (no in-play) | on | `--include-live-games` / `--in-play-only` |
| Leg order | stale venue first in-play | `--leg1` |
| Failed hedge → sell leg 1 back | unwind | `--on-fail hold` |
| Halt after N failed hedges in a row | 2 | `--max-failures` |
| While halted | no quotes, no arbs; stays up only while a held leg can still be closed at a profit, then exits | `--no-rescue` = exit at once |
| Don't re-take the same pair for | 10 min | `--cooldown` |
| Kill switch | create a file named `STOP` | `--kill-file` |
| Balance check before every trade | always | |
| Orders never rest on a book | always (IOC) | |
| Paused/suspended Polymarket US market | treated as an empty book | |

**In-play is where the arbs are, and where hedges fail.** On 2026-11-01, a public-data
watch of the Sunday slate found every profitable window in-play, right after scores, each
gone within ~20 s; none pre-game. A failed hedge is sold back at a loss (fees in and out
plus the spread), so keep `--max-per-trade` small and let `--max-failures` halt the bot.

## Other commands

| Command | |
| --- | --- |
| `python -m arbscan status` | Settle finished games and print the ledger and P&L |
| `python -m arbscan scan` | One scan: current arbs and the closest misses |
| `python -m arbscan liquidity` | Bid/ask, spread and $ depth within 2¢ for each game's main lines |
| `python -m arbscan fees` | Fee formulas and tables for both venues |

## Fees (from each venue's docs, applied to every decision)

| | Taker fee | Notes |
| --- | --- | --- |
| Kalshi | `0.07 × C × p × (1−p)` | Read live from each series. A regular account's order outlay rounds **up** to the cent. |
| Polymarket US | `0.0695 × C × p × (1−p)` | Read from each market's `feeCoefficient`. Each fill's fee rounds to the cent. Tier-1 makers get no rebate. |

Both peak at 50¢. A 50¢/50¢ pair needs **~3.5¢ of raw edge** just to break even;
a 90¢/10¢ pair needs ~1.3¢.

## Things to know

- **Polymarket US has one instrument per market.** Buying "the other team" or NO is a
  short (`ORDER_INTENT_BUY_SHORT`); its order price is always the YES price, so the bot
  sends 1 − your NO limit. It costs the same buying power as buying NO.
- **What's covered.** Polymarket US lists NFL moneylines and spreads (full game, halves,
  quarters) but no totals. Only moneylines and full-game spreads have a Kalshi match.
- **Rate limits.** Polymarket US allows 25 requests/s per IP, shared by data and orders; with
  the websocket the bot makes almost no REST reads. Kalshi rate-limits balance reads per
  account (shared with any other bot on it), so balances are read at most every 20 s and
  re-read after each trade; a rate-limited hedge waits 1 s before its retry.
- **Why the websocket matters.** On 2026-11-01 the first live run priced Polymarket from REST
  books: 4 orders, 4 expired unfilled, because the quotes were already gone.
- **Cash sits on both venues** until settlement. Keep both accounts funded; the
  smaller balance limits each arb's size.
- **Legality.** Kalshi and Polymarket US are both CFTC-regulated US exchanges. Check both
  venues' terms for automated trading.
- **No guarantee.** Arbs are rare and short-lived, and a failed hedge costs fees.
  Not financial advice.

## Tests

```bash
python -m unittest discover -s tests
```

There are 53 offline tests. They cover:

- **Math:** fee formulas (both venues' docs tables), coverage and tie logic, middles, depth sizing.
- **Execution:** leg order (stale venue first in-play), the leg-1 price cap,
  hedged/missed/unwound/held outcomes, settlement.
- **Safety:** kill switch, halt after failed hedges, spend tracking.
- **Kalshi orders:** request signing, and YES/NO → bid/ask mapping including
  reduce-only sells.
- **Polymarket US:** market parsing (moneyline/spread → score axis), long/short book
  mirroring, suspended markets, settlement, signing, BUY/SELL LONG/SHORT with YES-price
  conversion, partial fills, rejections, cancelling an order left open; books served only from the live feed (never a stale REST one), feed staleness, the balance cache.
