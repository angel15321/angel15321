# NFL Arb Scanner

Finds NFL moneyline arbitrage between **Kalshi**, **Polymarket** and (optionally)
**US sportsbooks**. For every open game it finds the cheapest way to back each
team on any venue, **fees included**, and flags games where backing both teams
costs less than the $1 you're guaranteed to get back.

It pulls data straight from the venues' public APIs, so it doesn't depend on any
third-party odds site. Python 3.9+, standard library only — nothing to install.

## Run it

```bash
python3 arb_scanner.py
```

Useful options:

```bash
python3 arb_scanner.py --html report.html --watch 60
```

| Option | What it does |
| --- | --- |
| `--bankroll 500` | Total $ to split across both legs when showing stakes (default 100) |
| `--min-roi 0.005` | Only report arbs of at least 0.5% |
| `--html FILE` | Write a dark/light HTML report (auto-refreshes with `--watch`) |
| `--json FILE` | Write current arbs as JSON (for alerts / other scripts) |
| `--watch 60` | Rescan every 60 seconds (minimum 30) |
| `--days 3` | Only games kicking off in the next 3 days (default 8) |
| `--cross-venue-only` | Ignore combos where both legs are on the same venue |
| `--books draftkings fanduel` | Only use these sportsbooks |
| `--kalshi-fee`, `--poly-fee` | Override fee assumptions |

### Adding sportsbooks

Get a free key at [the-odds-api.com](https://the-odds-api.com) (500 requests/month
on the free tier; each scan uses 1), then:

```bash
export ODDS_API_KEY=your_key_here
```

Without a key, the scanner just compares Kalshi and Polymarket.

## How it works

- **Kalshi** (`KXNFLGAME` markets): you can back a team by buying YES on it or
  NO on the opponent; both are checked. Fee: `0.07 × P × (1−P)` per contract.
- **Polymarket**: reads the live CLOB order book for each game's moneyline
  market (not the slower cached price). Fee is read from each market's own fee
  schedule.
- **Sportsbooks**: decimal odds converted to cost per $1 payout (`1 / odds`);
  the vig is already in the price.
- **Arb**: `cost(team A) + cost(team B) < 1`. ROI = `1 / total − 1`. Stakes are
  split so you get the same payout whichever team wins.

## Read before betting real money

- **Prices move in seconds**, especially on live games. An arb you see may be gone
  by the time you place the second leg. Place the thinner/faster leg first.
- **Depth**: the report shows how much is available at the top of the book. Bigger
  stakes eat into worse prices and can erase the edge.
- **Ties**: Kalshi and Polymarket both settle an NFL tie 50/50, so an exchange-vs-
  exchange arb is still safe. Sportsbooks usually treat a tie as a push (refund),
  so a book-vs-exchange arb roughly breaks even on a tie rather than profiting.
- **Fees are estimates**: Kalshi rounds its fee up to the cent per order, so very
  small edges (< ~0.5%) can disappear on small orders. Use `--min-roi`.
- **Money is tied up** until the game settles, and moving funds between venues
  takes time and sometimes costs money.
- Sportsbooks may limit accounts that consistently arb. Check each venue's terms
  and whether it's legal where you live.

This is an informational tool, not financial advice.
