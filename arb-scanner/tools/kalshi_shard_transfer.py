"""One-off: move the arb subaccount's cash between Kalshi exchange shards.

Kalshi markets live on numbered shards (exchange_index): NFL on 0, NBA and all
tennis on 3 (11-2026). A subaccount trades a shard only with cash on that shard;
on 11-03 both orders the bot sent to shard-3 markets came back
403 PERMISSION_DENIED_FOR_SUBTRADER while the arb subaccount held its cash on shard 0 only.

Uses the bot's own restricted key (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH),
so it can only move money inside its own subaccount. Nothing moves without
--execute, and a failed transfer is reported, never retried.

    python tools/kalshi_shard_transfer.py --amount 15                 # balances + plan, moves nothing
    python tools/kalshi_shard_transfer.py --amount 15 --execute       # shard 0 -> shard 3

Endpoint (verified 2026-08-12 on the primary account): POST /portfolio/intra_exchange_instance_transfer
{"source": "event_contract", "destination": "event_contract", "amount": <units>,
 "source_exchange_shard": 0, "destination_exchange_shard": 3, + source/destination_subaccount}
UNITS ARE CENTICENTS: $1 = 10,000 units (the doc's "1 cent = 1 unit" is wrong).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_subaccount_setup import Kalshi  # noqa: E402


def balances(k: Kalshi, sub: int) -> dict[int, float]:
    """shard -> $ for this subaccount. A shard the subaccount has never held cash on is absent (= $0)."""
    for attempt in range(3):
        code, d = k.req("GET", "/portfolio/subaccounts/balances")
        if code != 429:
            break
        time.sleep(2 * (attempt + 1))         # the bot reads balances on the same key
    if code != 200:
        sys.exit(f"subaccount balances -> HTTP {code}: {d}")
    out = {int(r["exchange_index"]): float(r["balance"]) for r in d.get("subaccount_balances") or []
           if int(r.get("subaccount_number", -1)) == sub}
    print(f"subaccount {sub} shard balances:", {i: f"${v:,.2f}" for i, v in sorted(out.items())} or "none")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--amount", type=float, required=True, help="dollars to move")
    ap.add_argument("--from-shard", type=int, default=0)
    ap.add_argument("--to-shard", type=int, default=3)
    ap.add_argument("--subaccount", type=int, default=int(os.environ.get("KALSHI_SUBACCOUNT", "2")))
    ap.add_argument("--execute", action="store_true", help="actually move the money")
    a = ap.parse_args()
    key_id, pem = os.environ.get("KALSHI_API_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not pem:
        sys.exit("set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (the arb bot's .env)")
    k = Kalshi(key_id, pem)
    before = balances(k, a.subaccount)
    src = before.get(a.from_shard, 0.0)
    if a.amount <= 0 or a.amount > src + 1e-9:
        sys.exit(f"cannot move ${a.amount:,.2f}: shard {a.from_shard} holds ${src:,.2f}")
    units = round(a.amount * 10_000)
    body = {"source": "event_contract", "destination": "event_contract", "amount": units,
            "source_exchange_shard": a.from_shard, "destination_exchange_shard": a.to_shard,
            "source_subaccount": a.subaccount, "destination_subaccount": a.subaccount}
    print(f"plan: subaccount {a.subaccount}: ${a.amount:,.2f} ({units:,} units) shard {a.from_shard} -> shard {a.to_shard}")
    if not a.execute:
        print("DRY RUN - nothing moved. Add --execute to move it.")
        return
    code, d = k.req("POST", "/portfolio/intra_exchange_instance_transfer", body)
    print(f"transfer -> HTTP {code}: {d}")
    balances(k, a.subaccount)
    if code not in (200, 201):
        sys.exit("✖ transfer failed. NOT retried. Check the balances above before doing anything else.")


if __name__ == "__main__":
    main()
