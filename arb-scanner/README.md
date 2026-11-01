# NFL Auto-Arb — Kalshi × Polymarket

A bot that finds **arbitrage between Kalshi and Polymarket on NFL moneylines,
spreads and totals** and **executes both legs automatically**, with exact
venue fees, order-book-aware sizing, and hard risk limits.

**It starts in dry-run mode.** It runs every step against live order books
but sends no orders until you add `--live`.

```bash
cd arb-scanner
```

```bash
python3 -m arbscan run
```

## How a trade happens

1. **Scan** (every 15 s). It pulls every NFL moneyline, spread and total on both venues
   (about 4,500 contracts) with full order books in about 5 s.
2. **Find.** It pairs any two contracts that together pay at least $1 on every possible
   score (same line on two venues, middles like Over 44.5 + Under 45.5, moneyline vs
   spread). A pair counts only if it still profits after both venues' taker fees.
3. **Size.** It walks both order books to the largest profitable size, then caps that
   by your limits and the cash on each venue.
4. **Leg 1: Polymarket.** Polymarket sports markets match after a 1 s delay, so this
   leg goes first and the bot waits for the confirmed fill. It's a Fill-And-Kill
   order with a max price, so nothing rests on the book.
5. **Leg 2: Kalshi.** It buys exactly what leg 1 filled, as an immediate-or-cancel
   order capped at the **break-even price**, and retries up to 3 times.
6. **If the hedge fails,** it sells leg 1 back (`--on-fail unwind`, the default) or
   keeps it (`--on-fail hold`).
7. **Log and settle.** Every order goes into `trades.db` and `arbscan.log`. After
   each game it reads the official result and books the real P&L.

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
- **Polymarket:** the signer private key, your wallet address, and a Relayer API
  key. Get them at polymarket.com → Settings → API Keys.

Then load the keys and check that both accounts connect. This places no orders:

```bash
set -a && . ./.env && set +a
```

```bash
python3 -m arbscan check
```

Run the dry run for a while first. Once its results look right, go live with small limits:

```bash
python3 -m arbscan run --live --max-per-trade 50 --max-daily 200
```

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
| Pre-game only (no in-play) | on | `--include-live-games` |
| Failed hedge → sell leg 1 back | unwind | `--on-fail hold` |
| Halt after N failed hedges in a row | 2 | `--max-failures` |
| Don't re-take the same pair for | 10 min | `--cooldown` |
| Kill switch | create a file named `STOP` | `--kill-file` |
| Balance check before every trade | always | |
| Orders never rest on a book | always (IOC / FAK) | |

**Why pre-game only by default.** In a 4-minute paper test during live games,
the bot saw 4 arbs. One hedged (+$0.52), one was gone within a second, and **two
left a leg unhedged**, because the second venue's price moved before the hedge
reached it. In-play arbs are the most common, but also by far the riskiest.

## Other commands

| Command | |
| --- | --- |
| `python3 -m arbscan status` | Settle finished games and print the ledger and P&L |
| `python3 -m arbscan scan` | One scan: current arbs and the closest misses |
| `python3 -m arbscan liquidity` | Bid/ask, spread and $ depth within 2¢ for each game's main lines |
| `python3 -m arbscan fees` | Fee formulas and tables for both venues |

## Fees (from each venue's docs, applied to every decision)

| | Taker fee | Notes |
| --- | --- | --- |
| Kalshi | `0.07 × C × p × (1−p)` | Read live from each series. A regular account's order outlay rounds **up** to the cent. |
| Polymarket | `rate × C × p × (1−p)` | `rate` comes from each market (NFL currently **0.03**). Makers pay 0. |

Both peak at 50¢. A 50¢/50¢ pair needs **2.5¢ of raw edge** just to break even;
a 90¢/10¢ pair needs 0.9¢.

## Things to know

- **Polymarket fees on fills are estimated** with the published formula. The
  share count comes from the actual fill, so realized P&L uses what you really got.
- **Redeeming on Polymarket.** Winning Polymarket shares may need to be redeemed
  to turn back into cash. Kalshi pays out automatically.
- **Cash sits on both venues** until settlement. Keep both accounts funded; the
  smaller balance limits each arb's size.
- **Legality.** Kalshi is CFTC-regulated and US-only. Whether you can use
  Polymarket depends on where you live. Check both venues' terms.
- **No guarantee.** Arbs are rare and short-lived, and a failed hedge costs fees
  (about $1.50 per 100 contracts in and out). Not financial advice.

## Tests

```bash
python3 -m unittest discover -s tests
```

There are 34 offline tests. They cover:

- **Math:** fee formulas, coverage and tie logic, middles, depth sizing.
- **Execution:** leg order, the leg-1 price cap, hedged/missed/unwound/held
  outcomes, settlement.
- **Safety:** kill switch, halt after failed hedges, spend tracking.
- **Kalshi orders:** request signing, and YES/NO → bid/ask mapping including
  reduce-only sells.
- **Polymarket orders:** delayed-match polling, cancelling an unmatched order, rejections.
