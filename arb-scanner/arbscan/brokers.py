"""Order execution for each venue, plus a paper broker for dry runs.

Every broker exposes the same three calls:

    buy(contract, qty, limit, notional_hint) -> Fill   # immediate-or-cancel
    sell(contract, qty, min_price)           -> Fill   # immediate-or-cancel, reduce only
    balance()                                -> float  # available cash in $

Nothing ever rests on a book: Kalshi orders are `immediate_or_cancel`, and
Polymarket US orders are immediate-or-cancel limit orders (anything the
exchange reports as still open afterwards is cancelled). The one exception is
maker mode (maker.py): post() rests a post-only Polymarket bid, and
order_state() / cancel() follow it, each returning
{"state", "done", "cum" (contracts filled), "avg" (our side's price), "fee"}.

Credentials (live mode only) come from environment variables:

    KALSHI_API_KEY_ID              Kalshi → Account & security → API Keys
    KALSHI_PRIVATE_KEY_PATH        path to the downloaded .pem (or KALSHI_PRIVATE_KEY = PEM text)

    POLYMARKET_KEY_ID              polymarket.us/developer → Create API key → Key ID
    POLYMARKET_SECRET_KEY          the Secret Key shown once at creation (base64)
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
POLY_US_API = "https://api.polymarket.us"


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
        if self.latency:
            time.sleep(self.latency)
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

    # maker mode: a paper quote fills in full once the live ask reaches its price (someone sold into it)
    def post(self, c: Contract, qty: float, limit: float) -> Fill:
        oid = f"paper-q-{uuid.uuid4().hex[:8]}"
        self.quotes = getattr(self, "quotes", {})
        self.quotes[oid] = {"qty": qty, "price": limit, "state": "resting", "cum": 0.0}
        return Fill(self.venue, "buy", c.label, order_id=oid, status="resting")

    def order_state(self, c: Contract, oid: str) -> dict:
        from . import trader
        q = getattr(self, "quotes", {}).get(oid)
        if q is None:
            return {"state": "unknown", "done": True, "cum": 0.0, "avg": 0.0, "fee": 0.0}
        if q["state"] == "resting":
            book = trader.live_book(c) or c.book
            if book.best_ask is not None and book.best_ask <= q["price"] + 1e-9:
                q.update(state="filled", cum=q["qty"])
                self.cash -= q["qty"] * q["price"]
        return {"state": q["state"], "done": q["state"] != "resting", "cum": q["cum"], "avg": q["price"], "fee": 0.0}

    def cancel(self, c: Contract, oid: str) -> dict:
        st = self.order_state(c, oid)
        q = getattr(self, "quotes", {}).get(oid)
        if q is not None and q["state"] == "resting":
            q["state"] = "canceled"
        return {**st, "state": q["state"] if q else st["state"], "done": True}


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

    shards: dict[int, float] | None = None   # exchange shard -> $, from the last balance() read

    def balance(self) -> float:
        """Cash across all shards, keeping `shards` = {exchange shard: $}. A market trades only against cash
        on its own shard (NFL = 0, NBA and tennis = 3); a subaccount with none there gets 403
        PERMISSION_DENIED_FOR_SUBTRADER (11-03)."""
        try:
            d = self._req("GET", "/portfolio/subaccounts/balances")
        except RuntimeError as e:
            if "429" in str(e):
                raise
            d = None
        if d is not None:
            rows = d.get("subaccount_balances") or []
            # A restricted key sees only its own subaccount; an unrestricted one sees all -> its primary (0).
            want = int(os.environ.get("KALSHI_SUBACCOUNT", "-1"))
            if want < 0 and len({r.get("subaccount_number") for r in rows}) > 1:
                want = 0
            rows = [r for r in rows if want < 0 or int(r.get("subaccount_number", -1)) == want]
            self.shards = {int(r["exchange_index"]): float(r["balance"]) for r in rows}
            return sum(self.shards.values())
        self.shards = None
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
        try:
            filled = float(r.get("fill_count") or 0)
            avg_yes = float(r.get("average_fill_price") or 0)
            avg = avg_yes if is_yes else (1 - avg_yes if filled else 0)
            fee = float(r.get("average_fee_paid") or 0) * filled
        except (TypeError, ValueError, AttributeError) as e:   # an unreadable reply must not escape execute()
            return Fill(self.venue, action, c.label, status="error", order_id=str((r or {}).get("order_id", "")),
                        error=f"unreadable order reply ({e}): {str(r)[:300]}", raw={"request_side": book_side})
        return Fill(self.venue, action, c.label, filled, avg, fee, r.get("order_id", ""),
                    "filled" if filled else "no fill", raw={**r, "request_side": book_side, "request_price": body["price"]})

    def buy(self, c: Contract, qty: float, limit: float, notional_hint: float | None = None) -> Fill:
        return self._order(c, "buy", qty, limit)

    def sell(self, c: Contract, qty: float, min_price: float) -> Fill:
        return self._order(c, "sell", qty, max(0.01, min_price))


# ---------------------------------------------------------------- Polymarket US

def ceil_to(x: float, step: float) -> float:
    return math.ceil(round(x / step, 6)) * step


class PolymarketBroker:
    """Polymarket US (api.polymarket.us). One instrument per market: buying the
    contract's `long` side is ORDER_INTENT_BUY_LONG, its `short` side (NO) is
    ORDER_INTENT_BUY_SHORT. Order prices are ALWAYS the YES price, also for
    NO orders, so a NO bought for at most q is a YES sale at no less than 1-q."""
    venue = "Polymarket"
    done = ("ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED")

    def __init__(self, key_id: str | None = None, secret_key_b64: str | None = None, base: str = POLY_US_API):
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("live Polymarket trading needs `pip install cryptography`") from e
        self.key_id = key_id or os.environ.get("POLYMARKET_KEY_ID")
        secret = secret_key_b64 or os.environ.get("POLYMARKET_SECRET_KEY")
        if not self.key_id or not secret:
            raise RuntimeError("set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY (polymarket.us/developer)")
        # The secret is base64; the Ed25519 seed is its first 32 bytes.
        self.key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret)[:32])
        self.base = base

    def _sign(self, method: str, path: str) -> dict:
        ts = str(int(time.time() * 1000))
        sig = self.key.sign(f"{ts}{method}{path.split('?')[0]}".encode())
        return {"X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts,
                "X-PM-Signature": base64.b64encode(sig).decode()}

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "nfl-arbscan/2.0", **self._sign(method, path)}
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            raise RuntimeError(f"Polymarket {method} {path} → HTTP {e.code}: {detail}") from None

    def balance(self) -> float:
        b = (self._req("GET", "/v1/account/balances").get("balances") or [{}])[0]
        return float(b.get("buyingPower") or 0)

    def _order(self, c: Contract, action: str, qty: float, yes_px: float, intent: str) -> Fill:
        tick = c.tick or 0.01
        yes_px = min(max(round(yes_px, 4), tick), 1 - tick)
        body = {"marketSlug": c.ref["slug"], "intent": intent, "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{yes_px:.4f}", "currency": "USD"}, "quantity": floor_to(qty, 0.01),
                "tif": "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL", "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                "synchronousExecution": True, "maxBlockTime": "5"}
        try:
            r = self._req("POST", "/v1/orders", body)
        except Exception as e:
            return Fill(self.venue, action, c.label, status="error", error=str(e))
        try:
            return self._result(c, action, r, body)
        except Exception as e:   # an unreadable reply must not escape execute(): the order may have filled
            return Fill(self.venue, action, c.label, status="error", order_id=str((r or {}).get("id", "")),
                        error=f"unreadable order reply ({type(e).__name__}: {e}): {str(r)[:300]}")

    def _result(self, c: Contract, action: str, r: dict, body: dict) -> Fill:
        oid = r.get("id", "")
        execs = r.get("executions") or []
        order = next((x["order"] for x in reversed(execs) if x.get("order")), None)
        rejected = next((x for x in execs if x.get("type") == "EXECUTION_TYPE_REJECTED"), None)
        if order is None or order.get("state") not in self.done:
            order = self._final(oid, c) or order or {}
        state = order.get("state", "")
        filled = float(order.get("cumQuantity") or 0)
        if not filled:
            fills = [(float(x["lastPx"]["value"]), float(x.get("lastShares") or 0)) for x in execs
                     if x.get("type") in ("EXECUTION_TYPE_FILL", "EXECUTION_TYPE_PARTIAL_FILL") and x.get("lastPx")]
            filled = sum(q for _, q in fills)
            avg_yes = sum(p * q for p, q in fills) / filled if filled else 0.0
        else:
            avg_yes = float((order.get("avgPx") or {}).get("value") or 0)
        if not filled:
            err = ""
            if rejected:
                err = f"{rejected.get('orderRejectReason', '')} {rejected.get('text', '')}".strip()
            return Fill(self.venue, action, c.label, order_id=oid, status="rejected" if rejected else "no fill",
                        error=err, raw={"state": state, "request_price": body["price"]["value"],
                                                 "create_time": order.get("createTime")})
        holds_long = c.ref["side"] == "long"
        avg = avg_yes if holds_long else 1 - avg_yes
        fee_v = (order.get("commissionNotionalTotalCollected") or {}).get("value")
        fee = float(fee_v) if fee_v not in (None, "") else c.fee.fill_fee(avg, filled)
        return Fill(self.venue, action, c.label, filled, avg, fee, oid, "filled",
                    raw={"state": state, "avg_yes": avg_yes, "request_price": body["price"]["value"],
                         "intent": body["intent"], "create_time": order.get("createTime")})

    def _final(self, oid: str, c: Contract, wait: float = 3.0) -> dict | None:
        """Poll an order that came back without a final state; cancel anything left open."""
        if not oid:
            return None
        deadline, o = time.time() + wait, None
        while time.time() < deadline:
            try:
                o = self._req("GET", f"/v1/order/{oid}").get("order") or {}
            except Exception:
                o = None
            if o and o.get("state") in self.done:
                return o
            time.sleep(0.25)
        try:
            self._req("POST", f"/v1/order/{oid}/cancel", {"marketSlug": c.ref["slug"]})  # never leave anything resting
        except Exception:
            pass
        return o

    # ------------------------------------------------------------ maker mode

    def post(self, c: Contract, qty: float, limit: float) -> Fill:
        """Rest a post-only bid for `qty` of c at `limit` (our side's price). Fill(status="resting", order_id)
        when it rests; "rejected"/"error" otherwise (a crossing quote is rejected, never taken)."""
        tick = c.tick or 0.01
        if c.ref["side"] == "long":
            yes_px, intent = floor_to(limit, tick), "ORDER_INTENT_BUY_LONG"
        else:
            yes_px, intent = ceil_to(1 - limit, tick), "ORDER_INTENT_BUY_SHORT"
        yes_px = min(max(round(yes_px, 4), tick), 1 - tick)
        body = {"marketSlug": c.ref["slug"], "intent": intent, "type": "ORDER_TYPE_LIMIT",
                "price": {"value": f"{yes_px:.4f}", "currency": "USD"}, "quantity": floor_to(qty, 0.01),
                "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL", "participateDontInitiate": True,
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC"}
        try:
            r = self._req("POST", "/v1/orders", body)
        except Exception as e:
            return Fill(self.venue, "buy", c.label, status="error", error=str(e))
        oid = r.get("id") or (r.get("order") or {}).get("id") or ""
        execs = r.get("executions") or []
        rej = next((x for x in execs if x.get("type") == "EXECUTION_TYPE_REJECTED"), None)
        if rej:
            return Fill(self.venue, "buy", c.label, order_id=oid, status="rejected",
                        error=f"{rej.get('orderRejectReason', '')} {rej.get('text', '')}".strip())
        if not oid:
            return Fill(self.venue, "buy", c.label, status="error", error=f"no order id in reply: {str(r)[:300]}")
        return Fill(self.venue, "buy", c.label, order_id=oid, status="resting", raw={"request_price": body["price"]["value"]})

    def _state(self, c: Contract, o: dict) -> dict:
        cum = float(o.get("cumQuantity") or 0)
        avg_yes = float((o.get("avgPx") or {}).get("value") or 0)
        avg = (avg_yes if c.ref["side"] == "long" else 1 - avg_yes) if cum else 0.0
        fee_v = (o.get("commissionNotionalTotalCollected") or {}).get("value")
        return {"state": o.get("state", ""), "done": o.get("state") in self.done, "cum": cum, "avg": avg,
                "fee": float(fee_v) if fee_v not in (None, "") else 0.0}

    def order_state(self, c: Contract, oid: str) -> dict:
        return self._state(c, self._req("GET", f"/v1/order/{oid}").get("order") or {})

    def cancel(self, c: Contract, oid: str, wait: float = 1.5) -> dict:
        """Cancel and read the final state (filled quantity included). {"error"} when the cancel failed."""
        err = ""
        try:
            self._req("POST", f"/v1/order/{oid}/cancel", {"marketSlug": c.ref["slug"]})
        except Exception as e:
            err = str(e)        # often "already filled / cancelled": the read below decides
        deadline, st = time.time() + wait, None
        while True:
            try:
                st = self.order_state(c, oid)
            except Exception as e:
                st = {"error": f"{err} / read: {e}" if err else f"read: {e}"}
            if st.get("done") or time.time() >= deadline:
                break
            time.sleep(0.1)
        if not st.get("done") and err and not st.get("error"):
            st["error"] = err
        return st

    def buy(self, c: Contract, qty: float, limit: float, notional_hint: float | None = None) -> Fill:
        tick = c.tick or 0.01
        if c.ref["side"] == "long":
            return self._order(c, "buy", qty, floor_to(limit, tick), "ORDER_INTENT_BUY_LONG")
        return self._order(c, "buy", qty, ceil_to(1 - limit, tick), "ORDER_INTENT_BUY_SHORT")

    def sell(self, c: Contract, qty: float, min_price: float) -> Fill:
        tick = c.tick or 0.01
        min_price = max(tick, min_price)
        if c.ref["side"] == "long":
            return self._order(c, "sell", qty, ceil_to(min_price, tick), "ORDER_INTENT_SELL_LONG")
        return self._order(c, "sell", qty, floor_to(1 - min_price, tick), "ORDER_INTENT_SELL_SHORT")
