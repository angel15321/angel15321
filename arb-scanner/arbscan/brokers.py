"""Order execution for each venue, plus a paper broker for dry runs.

Every broker exposes the same three calls:

    buy(contract, qty, limit, notional_hint) -> Fill   # immediate-or-cancel
    sell(contract, qty, min_price)           -> Fill   # immediate-or-cancel, reduce only
    balance()                                -> float  # available cash in $

Nothing ever rests on a book: Kalshi orders are `immediate_or_cancel`,
Polymarket orders are Fill-And-Kill market orders with a price bound.

Credentials (live mode only) come from environment variables:

    KALSHI_API_KEY_ID              Kalshi → Account & security → API Keys
    KALSHI_PRIVATE_KEY_PATH        path to the downloaded .pem (or KALSHI_PRIVATE_KEY = PEM text)

    POLYMARKET_PRIVATE_KEY         signer private key
    POLYMARKET_WALLET_ADDRESS      account wallet (polymarket.com profile menu)
    POLYMARKET_RELAYER_API_KEY     polymarket.com → Settings → API Keys → Relayer
    POLYMARKET_RELAYER_API_KEY_ADDRESS
"""
from __future__ import annotations

import base64
import json
import math
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field

from . import scanner
from .model import Contract

KALSHI_TRADE_API = "https://external-api.kalshi.com/trade-api/v2"
KALSHI_DEMO_API = "https://external-api.demo.kalshi.co/trade-api/v2"


@dataclass
class Fill:
    venue: str
    action: str            # "buy" | "sell"
    label: str
    qty: float = 0.0
    avg_price: float = 0.0  # price per contract of the outcome bought/sold
    fee: float = 0.0
    order_id: str = ""
    status: str = ""
    error: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def notional(self) -> float:
        return self.qty * self.avg_price

    @property
    def cash(self) -> float:
        """Signed cash flow: negative when buying."""
        return -(self.notional + self.fee) if self.action == "buy" else self.notional - self.fee

    def as_dict(self) -> dict:
        d = asdict(self)
        d["raw"] = {k: v for k, v in self.raw.items() if isinstance(v, (str, int, float, bool, type(None)))}
        return d


def floor_to(x: float, step: float) -> float:
    return math.floor(round(x / step, 6)) * step


def max_price_within(budget: float, contract: Contract) -> float:
    """Highest price p (on the contract's tick) with p + fee(p) <= budget."""
    lo, hi = 0.0, 0.999
    if budget <= 0:
        return 0.0
    for _ in range(40):
        mid = (lo + hi) / 2
        if mid + contract.fee.per_contract(mid) <= budget:
            lo = mid
        else:
            hi = mid
    return floor_to(lo, contract.tick or 0.01)


# ---------------------------------------------------------------- paper

class PaperBroker:
    """Fills against freshly fetched live order books; places nothing."""

    def __init__(self, venue: str, latency: float = 0.5, cash: float = 1e9):
        self.venue = venue
        self.latency = latency
        self.cash = cash

    def balance(self) -> float:
        return self.cash

    def _wait(self, c: Contract) -> None:
        delay = self.latency + (c.ref.get("seconds_delay", 0) or 0)
        if delay:
            time.sleep(delay)
        scanner.refresh([c])

    def buy(self, c: Contract, qty: float, limit: float, notional_hint: float | None = None) -> Fill:
        self._wait(c)
        fills, left = [], qty
        for p, s in c.book.asks:
            if p > limit + 1e-9 or left <= 1e-9:
                break
            take = min(s, left)
            fills.append((p, take))
            left -= take
        q = sum(x for _, x in fills)
        if q < c.min_size:
            return Fill(self.venue, "buy", c.label, status="no fill",
                        error=f"best ask {c.book.best_ask} > limit {limit:.3f}" if c.book.best_ask else "empty book")
        notional, fee = c.fee.order_total(fills)
        self.cash -= notional + fee
        return Fill(self.venue, "buy", c.label, q, notional / q, fee, f"paper-{uuid.uuid4().hex[:8]}", "filled")

    def sell(self, c: Contract, qty: float, min_price: float) -> Fill:
        self._wait(c)
        fills, left = [], qty
        for p, s in c.book.bids:
            if p < min_price - 1e-9 or left <= 1e-9:
                break
            take = min(s, left)
            fills.append((p, take))
            left -= take
        q = sum(x for _, x in fills)
        if q <= 0:
            return Fill(self.venue, "sell", c.label, status="no fill", error="no bids above min price")
        notional = sum(p * x for p, x in fills)
        fee = sum(c.fee.fill_fee(p, x) for p, x in fills)
        self.cash += notional - fee
        return Fill(self.venue, "sell", c.label, q, notional / q, fee, f"paper-{uuid.uuid4().hex[:8]}", "filled")


# ---------------------------------------------------------------- Kalshi

class KalshiBroker:
    venue = "Kalshi"

    def __init__(self, key_id: str | None = None, private_key_pem: bytes | None = None, demo: bool = False):
        try:
            from cryptography.hazmat.primitives import serialization
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("live Kalshi trading needs `pip install cryptography`") from e
        self.key_id = key_id or os.environ.get("KALSHI_API_KEY_ID")
        if private_key_pem is None:
            if os.environ.get("KALSHI_PRIVATE_KEY"):
                private_key_pem = os.environ["KALSHI_PRIVATE_KEY"].encode()
            elif os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
                with open(os.path.expanduser(os.environ["KALSHI_PRIVATE_KEY_PATH"]), "rb") as f:
                    private_key_pem = f.read()
        if not self.key_id or not private_key_pem:
            raise RuntimeError("set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY)")
        self.key = serialization.load_pem_private_key(private_key_pem, password=None)
        self.base = KALSHI_DEMO_API if demo else KALSHI_TRADE_API

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

    def _req(self, method: str, endpoint: str, body: dict | None = None) -> dict:
        url = self.base + endpoint
        path = "/trade-api/v2" + endpoint
        headers = {"Content-Type": "application/json", "Accept": "application/json", **self._sign(method, path)}
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Kalshi {method} {endpoint} → HTTP {e.code}: {detail}") from None

    def balance(self) -> float:
        d = self._req("GET", "/portfolio/balance")
        if d.get("balance_dollars") is not None:
            return float(d["balance_dollars"])
        return float(d.get("balance", 0)) / 100

    def _order(self, c: Contract, action: str, qty: float, price: float) -> Fill:
        """price is for the contract we hold/want (YES or NO)."""
        qty = floor_to(qty, 0.01)
        is_yes = c.ref["side"] == "yes"
        # V2 single book, quoted on YES: bid = buy YES, ask = sell YES (= buy NO).
        if action == "buy":
            book_side, yes_px = ("bid", price) if is_yes else ("ask", 1 - price)
        else:
            book_side, yes_px = ("ask", price) if is_yes else ("bid", 1 - price)
        body = {"ticker": c.ref["ticker"], "client_order_id": str(uuid.uuid4()), "side": book_side,
                "count": f"{qty:.2f}", "price": f"{round(yes_px, 4):.4f}", "time_in_force": "immediate_or_cancel",
                "self_trade_prevention_type": "taker_at_cross", "post_only": False,
                "reduce_only": action == "sell", "cancel_order_on_pause": True}
        try:
            r = self._req("POST", "/portfolio/events/orders", body)
        except Exception as e:
            return Fill(self.venue, action, c.label, status="error", error=str(e))
        filled = float(r.get("fill_count") or 0)
        avg_yes = float(r.get("average_fill_price") or 0)
        avg = avg_yes if is_yes else (1 - avg_yes if filled else 0)
        fee = float(r.get("average_fee_paid") or 0) * filled
        return Fill(self.venue, action, c.label, filled, avg, fee, r.get("order_id", ""),
                    "filled" if filled else "no fill", raw={**r, "request_side": book_side, "request_price": body["price"]})

    def buy(self, c: Contract, qty: float, limit: float, notional_hint: float | None = None) -> Fill:
        return self._order(c, "buy", qty, limit)

    def sell(self, c: Contract, qty: float, min_price: float) -> Fill:
        return self._order(c, "sell", qty, max(0.01, min_price))


# ---------------------------------------------------------------- Polymarket

class PolymarketBroker:
    venue = "Polymarket"

    def __init__(self):
        try:
            from polymarket import RelayerApiKey, SecureClient
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("live Polymarket trading needs `pip install polymarket-client`") from e
        need = ["POLYMARKET_PRIVATE_KEY", "POLYMARKET_WALLET_ADDRESS"]
        missing = [k for k in need if not os.environ.get(k)]
        if missing:
            raise RuntimeError("set " + ", ".join(missing))
        api_key = None
        if os.environ.get("POLYMARKET_RELAYER_API_KEY"):
            api_key = RelayerApiKey(key=os.environ["POLYMARKET_RELAYER_API_KEY"],
                                    address=os.environ.get("POLYMARKET_RELAYER_API_KEY_ADDRESS", ""))
        self.client = SecureClient.create(private_key=os.environ["POLYMARKET_PRIVATE_KEY"],
                                          wallet=os.environ["POLYMARKET_WALLET_ADDRESS"], api_key=api_key)

    def balance(self) -> float:
        b = self.client.get_balance_allowance(asset_type="COLLATERAL")
        return b.balance / 1e6

    def _settle(self, resp, c: Contract, action: str, wait: float = 8.0) -> Fill:
        if not getattr(resp, "ok", False):
            return Fill(self.venue, action, c.label, status="rejected",
                        error=f"{getattr(resp, 'code', '')} {getattr(resp, 'message', '')}".strip())
        oid = resp.order_id
        making, taking = float(resp.making_amount or 0), float(resp.taking_amount or 0)
        status = (resp.status or "").lower()
        # Sports markets apply a matching delay: the order is accepted as
        # "delayed" and matches after it. Poll until it is no longer pending.
        deadline = time.time() + wait
        while status in ("delayed", "live") and time.time() < deadline:
            time.sleep(0.4)
            try:
                o = self.client.get_order(order_id=oid)
            except Exception:
                continue
            status = (o.status or "").lower()
            matched = float(o.size_matched or 0)
            if matched:
                taking = matched if action == "buy" else taking
                making = matched * float(o.price) if action == "buy" else matched
            if status not in ("delayed", "live", "unmatched"):
                break
        if status in ("delayed", "live"):
            try:
                self.client.cancel_order(order_id=oid)  # never leave anything resting
            except Exception:
                pass
        # BUY: making = USDC paid, taking = shares. SELL: making = shares, taking = USDC.
        shares, usdc = (taking, making) if action == "buy" else (making, taking)
        if shares <= 0:
            return Fill(self.venue, action, c.label, order_id=oid, status=status or "no fill")
        avg = usdc / shares
        return Fill(self.venue, action, c.label, shares, avg, c.fee.fill_fee(avg, shares), oid, status,
                    raw={"status": status, "making": making, "taking": taking})

    def buy(self, c: Contract, qty: float, limit: float, notional_hint: float | None = None) -> Fill:
        spend = round(notional_hint if notional_hint else qty * limit, 2)
        try:
            resp = self.client.place_market_order(asset_id=c.ref["token_id"], side="BUY", amount=f"{spend:.2f}",
                                                  max_price=f"{limit:.4f}", order_type="FAK")
        except Exception as e:
            return Fill(self.venue, "buy", c.label, status="error", error=f"{type(e).__name__}: {e}")
        return self._settle(resp, c, "buy")

    def sell(self, c: Contract, qty: float, min_price: float) -> Fill:
        try:
            resp = self.client.place_market_order(asset_id=c.ref["token_id"], side="SELL",
                                                  shares=f"{floor_to(qty, 0.01):.2f}",
                                                  min_price=f"{max(0.001, min_price):.4f}", order_type="FAK")
        except Exception as e:
            return Fill(self.venue, "sell", c.label, status="error", error=f"{type(e).__name__}: {e}")
        return self._settle(resp, c, "sell")
