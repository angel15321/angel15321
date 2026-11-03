"""One-off: give the arb bot its own Kalshi subaccount, cash and restricted API key.

Runs wherever an unrestricted Kalshi key already lives (the trading box), so that
key never has to be copied anywhere. Standard library + `cryptography` only.

    python kalshi_subaccount_setup.py status                 # read-only: balances + API keys
    python kalshi_subaccount_setup.py apply --amount 25      # shows the plan, changes nothing
    python kalshi_subaccount_setup.py apply --amount 25 --execute

`apply --execute` does three things, once each, recording progress in a state file
(default ~/arbscan-subaccount.json) so a re-run skips finished steps:

  1. POST /portfolio/subaccounts           create a numbered subaccount (or reuse --subaccount N)
  2. POST /portfolio/subaccounts/transfer  move --amount dollars from the primary account
                                           (shard 0) into it. NEVER retried: if it fails or
                                           its result is unknown, the script stops.
  3. POST /api_keys/generate               an ed25519 key restricted to that subaccount. Its
                                           private key goes to --out (mode 600) and is never
                                           printed; --env-out gets the two .env lines.

A restricted key can only read and trade its own subaccount: it cannot move money,
create subaccounts or manage keys (docs.kalshi.com/getting_started/subaccounts).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "https://external-api.kalshi.com/trade-api/v2"


def read_env(path: str) -> dict:
    out = {}
    for line in open(os.path.expanduser(path), encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Kalshi:
    def __init__(self, key_id: str, pem_path: str, base: str = BASE):
        from cryptography.hazmat.primitives import serialization
        with open(os.path.expanduser(pem_path), "rb") as f:
            self.key = serialization.load_pem_private_key(f.read(), password=None)
        self.key_id, self.base = key_id, base

    def _sign(self, method: str, path: str) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        ts = str(int(time.time() * 1000))
        msg = f"{ts}{method}{path.split('?')[0]}".encode()
        if isinstance(self.key, Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def req(self, method: str, endpoint: str, body: dict | None = None) -> tuple[int, dict]:
        path = "/trade-api/v2" + endpoint
        headers = {"Content-Type": "application/json", "Accept": "application/json", **self._sign(method, path)}
        r = urllib.request.Request(self.base + endpoint, method=method, headers=headers,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")[:500]
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"raw": raw}


def show_status(k: Kalshi) -> list[dict]:
    code, d = k.req("GET", "/portfolio/subaccounts/balances")
    if code != 200:
        sys.exit(f"subaccount balances -> HTTP {code}: {d}")
    rows = sorted(d.get("subaccount_balances") or [], key=lambda r: (r["subaccount_number"], r["exchange_index"]))
    print("Subaccount balances (subaccount / shard / $):")
    for r in rows:
        print(f"  {r['subaccount_number']:>3}  shard {r['exchange_index']}  ${float(r['balance']):,.2f}")
    code, d = k.req("GET", "/api_keys")
    if code == 200:
        keys = d.get("api_keys") or []
        print(f"API keys on this login: {len(keys)}")
        for x in keys:
            print(f"  {x.get('name') or '(no name)'}  id {x.get('api_key_id')}  "
                  f"subaccount {x.get('subaccount', 'all')}  scopes {x.get('scopes', 'full')}")
    else:
        print(f"API key list -> HTTP {code}: {d}")
    return rows


def load_state(path: str) -> dict:
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return {}


def save_state(path: str, st: dict) -> None:
    with open(path, "w") as f:
        json.dump(st, f, indent=1)


def write_private(path: str, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def apply(k: Kalshi, a) -> None:
    rows = show_status(k)
    st = load_state(a.state)
    sub = st.get("subaccount") or a.subaccount
    cents = int(round(a.amount * 100))
    primary0 = next((float(r["balance"]) for r in rows if r["subaccount_number"] == 0 and r["exchange_index"] == 0), 0.0)
    print("\nPlan:")
    print(f"  1. subaccount: {'reuse ' + str(sub) if sub else 'create a new numbered subaccount'}")
    if st.get("transfer"):
        print(f"  2. transfer: already attempted ({st['transfer']['result']}) -> skipped, never retried")
    else:
        print(f"  2. transfer ${cents / 100:,.2f}: primary (0) shard 0 -> subaccount {sub or 'new'} shard 0"
              f"  [primary shard 0 has ${primary0:,.2f}]")
    print(f"  3. API key: {'already made (' + st['key_id'] + ')' if st.get('key_id') else 'ed25519, restricted to that subaccount -> ' + a.out}")
    if not a.execute:
        print("\nNothing changed. Re-run with --execute to do it.")
        return
    if not st.get("transfer") and cents > primary0 * 100:
        sys.exit(f"primary shard 0 holds ${primary0:,.2f} < ${cents / 100:,.2f}; nothing changed")
    if not st.get("key_id") and os.path.exists(a.out):
        sys.exit(f"{a.out} already exists; move it away first (refusing to overwrite a key)")

    if not sub:
        code, d = k.req("POST", "/portfolio/subaccounts", {})
        if code not in (200, 201) or "subaccount_number" not in d:
            sys.exit(f"create subaccount -> HTTP {code}: {d}  (needs the Advanced API tier or above)")
        sub = int(d["subaccount_number"])
        print(f"✔ created subaccount {sub}")
    st["subaccount"] = sub
    save_state(a.state, st)

    if not st.get("transfer"):
        tid = str(uuid.uuid4())
        st["transfer"] = {"client_transfer_id": tid, "amount_cents": cents, "result": "sent, no reply yet"}
        save_state(a.state, st)            # recorded BEFORE sending: a crash can never cause a second transfer
        code, d = k.req("POST", "/portfolio/subaccounts/transfer",
                        {"client_transfer_id": tid, "from_subaccount": 0, "to_subaccount": sub,
                         "amount_cents": cents, "exchange_index": 0})
        st["transfer"]["result"] = f"HTTP {code}"
        save_state(a.state, st)
        if code != 200:
            sys.exit(f"✖ transfer -> HTTP {code}: {d}\n  NOT retried. Check `status` before doing anything else.")
        print(f"✔ moved ${cents / 100:,.2f} into subaccount {sub}")

    if not st.get("key_id"):
        code, d = k.req("POST", "/api_keys/generate",
                        {"name": a.name, "key_type": "ed25519", "subaccount": sub, "scopes": ["read", "write"]})
        if code not in (200, 201) or not d.get("private_key"):
            sys.exit(f"✖ generate key -> HTTP {code}: {d.get('message') or d.get('code') or 'no key returned'}")
        write_private(a.out, d["private_key"])
        st["key_id"] = d["api_key_id"]
        save_state(a.state, st)
        print(f"✔ restricted key {d['api_key_id']} -> {a.out} (mode 600)")

    with open(a.env_out, "w") as f:
        f.write(f"# arb bot: Kalshi subaccount {sub}, key restricted to it\n"
                f"KALSHI_API_KEY_ID={st['key_id']}\n"
                f"KALSHI_PRIVATE_KEY_PATH={a.laptop_pem}\n")
    print(f"✔ .env lines -> {a.env_out}")
    print("\nAfter:")
    show_status(k)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "apply"])
    ap.add_argument("--env", default="~/kalshi/.env", help="file with KALSHI_API_KEY_ID + KALSHI_RSA_KEY_PATH")
    ap.add_argument("--amount", type=float, default=25.0, help="dollars to move into the subaccount")
    ap.add_argument("--subaccount", type=int, help="reuse this existing numbered subaccount instead of creating one")
    ap.add_argument("--name", default="arbscan-laptop")
    ap.add_argument("--out", default=os.path.expanduser("~/arbscan-kalshi.pem"))
    ap.add_argument("--env-out", default=os.path.expanduser("~/arbscan-kalshi.env"))
    ap.add_argument("--laptop-pem", default="~/.secrets/kalshi-arb.pem",
                    help="where the .pem will live on the machine running the bot (written into --env-out as is)")
    ap.add_argument("--state", default=os.path.expanduser("~/arbscan-subaccount.json"))
    ap.add_argument("--execute", action="store_true")
    a = ap.parse_args()
    env = read_env(a.env)
    pem = env.get("KALSHI_RSA_KEY_PATH") or env.get("KALSHI_PRIVATE_KEY_PATH") or "private_key.pem"
    if not os.path.isabs(os.path.expanduser(pem)):
        pem = os.path.join(os.path.dirname(os.path.abspath(os.path.expanduser(a.env))), pem)
    k = Kalshi(env["KALSHI_API_KEY_ID"], pem)
    if a.cmd == "status":
        show_status(k)
    else:
        apply(k, a)


if __name__ == "__main__":
    main()
