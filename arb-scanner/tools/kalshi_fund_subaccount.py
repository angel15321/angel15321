"""One-off: move cash from the PRIMARY Kalshi account (subaccount 0) into the arb subaccount, on one shard.

Runs where the unrestricted key lives (the trading box, ~/kalshi/.env). Shows every
subaccount's per-shard cash and the plan; nothing moves without --execute. Each sent
transfer is written to --log BEFORE it is sent, and a failed transfer is reported,
never retried.

    python tools/kalshi_fund_subaccount.py --amount 25                       # plan only
    python tools/kalshi_fund_subaccount.py --amount 25 --execute             # primary shard 0 -> sub 2 shard 0

Endpoint as used by kalshi_subaccount_setup.py (11-01, HTTP 200): POST /portfolio/subaccounts/transfer
{"client_transfer_id", "from_subaccount": 0, "to_subaccount": N, "amount_cents", "exchange_index"}.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kalshi_subaccount_setup import Kalshi, read_env  # noqa: E402


def shard_table(k: Kalshi) -> dict[tuple[int, int], float]:
    code, d = k.req("GET", "/portfolio/subaccounts/balances")
    if code != 200:
        sys.exit(f"subaccount balances -> HTTP {code}: {d}")
    out = {(int(r["subaccount_number"]), int(r["exchange_index"])): float(r["balance"])
           for r in d.get("subaccount_balances") or []}
    for (sub, shard), v in sorted(out.items()):
        print(f"  subaccount {sub:>2}  shard {shard}  ${v:,.2f}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--amount", type=float, required=True, help="dollars to move")
    ap.add_argument("--to-subaccount", type=int, default=2)
    ap.add_argument("--shard", type=int, default=0, help="exchange shard, same on both sides (default 0 = NFL)")
    ap.add_argument("--env", default="~/kalshi/.env", help="file with the UNRESTRICTED key")
    ap.add_argument("--log", default=os.path.expanduser("~/arbscan-fund-log.jsonl"))
    ap.add_argument("--execute", action="store_true", help="actually move the money")
    a = ap.parse_args()
    env = read_env(a.env)
    pem = env.get("KALSHI_RSA_KEY_PATH") or env.get("KALSHI_PRIVATE_KEY_PATH") or "private_key.pem"
    if not os.path.isabs(os.path.expanduser(pem)):
        pem = os.path.join(os.path.dirname(os.path.abspath(os.path.expanduser(a.env))), pem)
    k = Kalshi(env["KALSHI_API_KEY_ID"], pem)
    print("before:")
    before = shard_table(k)
    src = before.get((0, a.shard), 0.0)
    if a.amount <= 0 or a.amount > src + 1e-9 or a.to_subaccount == 0:
        sys.exit(f"cannot move ${a.amount:,.2f} to subaccount {a.to_subaccount}: primary shard {a.shard} holds ${src:,.2f}")
    cents = round(a.amount * 100)
    body = {"client_transfer_id": str(uuid.uuid4()), "from_subaccount": 0, "to_subaccount": a.to_subaccount,
            "amount_cents": cents, "exchange_index": a.shard}
    print(f"plan: ${a.amount:,.2f} primary (0) shard {a.shard} -> subaccount {a.to_subaccount} shard {a.shard}")
    if not a.execute:
        print("DRY RUN - nothing moved. Add --execute to move it.")
        return
    with open(a.log, "a") as f:            # recorded BEFORE sending: a crash can never hide a sent transfer
        f.write(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(), **body}) + "\n")
    code, d = k.req("POST", "/portfolio/subaccounts/transfer", body)
    print(f"transfer -> HTTP {code}: {d}")
    print("after:")
    shard_table(k)
    if code not in (200, 201):
        sys.exit("✖ transfer failed. NOT retried. Check the balances above before doing anything else.")


if __name__ == "__main__":
    main()
