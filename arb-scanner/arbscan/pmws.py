"""Live Polymarket US order books over the markets websocket.

The REST book endpoints (gateway and api) serve cached snapshots that can lag
the exchange by 10-30 s, with flickering sizes at the top of book; trading off
them in-play means chasing quotes that are already gone. The websocket
(wss://api.polymarket.us/v1/ws/markets, SUBSCRIPTION_TYPE_MARKET_DATA) pushes
the full book for each subscribed market every ~0.1-0.2 s, so this feed keeps
the latest one per market in memory and the scanner reads from it.

It runs an asyncio loop on a daemon thread, signs the handshake with the same
Ed25519 key as REST orders, subscribes in chunks of 100 markets (the server's
limit), and reconnects with backoff, re-subscribing everything it was asked for.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
import time

URL = "wss://api.polymarket.us/v1/ws/markets"
PATH = "/v1/ws/markets"
CHUNK = 100              # markets per subscription (server limit)
DEAD_AFTER = 5.0         # seconds without any message -> connection presumed dead
IDLE_EVICT = 3600.0      # seconds a market may go unrequested before it leaves wanted/books


class BookFeed:
    recv_timeout = DEAD_AFTER * 3          # s of silence before the connection is dropped and re-made

    def __init__(self, key_id: str, secret_key_b64: str, url: str = URL):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.key_id = key_id
        self.key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret_key_b64)[:32])
        self.url = url
        self.books: dict[str, tuple[float, dict]] = {}   # slug -> (received at, marketData)
        self.wanted: set[str] = set()
        self._asked: dict[str, float] = {}                # slug -> last want() time, for eviction
        self._evicted_at = time.time()
        self._sent: set[str] = set()                      # slugs subscribed on the current connection
        self._lock = threading.Lock()
        self.connected = False
        self.last_msg = 0.0
        self.reconnects = 0
        self.error = ""
        self.on_update = None                   # callable(slug) after each book message (event trigger)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._thread = threading.Thread(target=self._run, name="pm-books", daemon=True)
        self._thread.start()

    @classmethod
    def from_env(cls) -> "BookFeed | None":
        kid, sec = os.environ.get("POLYMARKET_KEY_ID"), os.environ.get("POLYMARKET_SECRET_KEY")
        return cls(kid, sec) if kid and sec else None

    # ------------------------------------------------------------ public

    def want(self, slugs) -> set[str]:
        """Make sure these markets are subscribed; returns the ones that were not yet."""
        now = time.time()
        slugs = set(slugs)
        asked = self._asked
        for s in slugs:
            asked[s] = now
        new = slugs - self.wanted
        with self._lock:
            self.wanted |= new
            if now - self._evicted_at > IDLE_EVICT:
                # markets nobody asked for in an hour (settled games) leave wanted/books, so a reconnect
                # does not re-subscribe every market ever seen
                self._evicted_at = now
                idle = {s for s in self.wanted if now - asked.get(s, 0.0) > IDLE_EVICT}
                self.wanted -= idle
                for s in idle:
                    self.books.pop(s, None)
                    asked.pop(s, None)
        if not new:
            return new
        if self._loop and self.connected:
            asyncio.run_coroutine_threadsafe(self._subscribe_missing(), self._loop)
        return new

    def alive(self) -> bool:
        return self.connected and time.time() - self.last_msg < DEAD_AFTER

    def get(self, slug: str) -> dict | None:
        """Latest marketData for `slug`, or None if the feed has none or is not alive."""
        hit = self.books.get(slug)
        if hit is None or not self.alive():
            return None
        return hit[1]

    def get_at(self, slug: str) -> tuple[float, dict] | None:
        """(received at, marketData) for `slug`, or None if the feed has none or is not alive."""
        hit = self.books.get(slug)
        return hit if hit is not None and self.alive() else None

    def wait_for(self, slugs, timeout: float = 2.0) -> None:
        """Block briefly until every slug has a first book (or timeout)."""
        end = time.time() + timeout
        while time.time() < end and any(s not in self.books for s in slugs):
            time.sleep(0.05)

    # ------------------------------------------------------------ internals

    def _headers(self) -> dict:
        ts = str(int(time.time() * 1000))
        sig = self.key.sign(f"{ts}GET{PATH}".encode())
        return {"X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts,
                "X-PM-Signature": base64.b64encode(sig).decode()}

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._forever())

    async def _forever(self) -> None:
        import websockets
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, additional_headers=self._headers(), open_timeout=15,
                                              max_size=2 ** 23, ping_interval=20) as ws:
                    self._ws, self.connected, self._sent = ws, True, set()
                    self.last_msg = time.time()
                    backoff = 1.0
                    await self._subscribe_missing()
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=self.recv_timeout)
                        self.last_msg = time.time()
                        self._handle(raw)
            except Exception as e:  # reconnect on anything: network, auth hiccup, server close
                self.error = f"{type(e).__name__}: {e}"[:200]
            self.connected, self._ws = False, None
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _subscribe_missing(self) -> None:
        ws = self._ws
        if ws is None:
            return
        with self._lock:
            todo = sorted(self.wanted - self._sent)
            self._sent |= set(todo)
        for i in range(0, len(todo), CHUNK):
            chunk = todo[i:i + CHUNK]
            await ws.send(json.dumps({"subscribe": {
                "requestId": f"md-{int(time.time() * 1000)}-{i}", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                "marketSlugs": chunk}}))

    def _handle(self, raw) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        md = m.get("marketData")
        if md and md.get("marketSlug"):
            self.books[md["marketSlug"]] = (time.time(), md)
            cb = getattr(self, "on_update", None)
            if cb is not None:
                cb(md["marketSlug"])
        elif m.get("error"):
            self.error = str(m["error"])[:200]


PRIVATE_URL = "wss://api.polymarket.us/v1/ws/private"
PRIVATE_PATH = "/v1/ws/private"


class OrderFeed(BookFeed):
    """Our own order updates (maker mode) over the private websocket, SUBSCRIPTION_TYPE_ORDER: the fastest
    word that a resting quote filled. maker.poll() falls back to a REST read when this has nothing, so a
    message shape this does not parse costs speed, not safety. The first messages are kept in `samples`
    for the log (the wire format is read from the SDK, not yet seen live)."""
    recv_timeout = 3600.0                  # a quiet account is silent for long; websocket pings check the link

    def __init__(self, key_id: str, secret_key_b64: str, url: str = PRIVATE_URL):
        self.orders: dict[str, tuple[float, dict]] = {}     # order id -> (received at, order)
        self.samples: list[str] = []
        super().__init__(key_id, secret_key_b64, url)
        self.wanted = {"orders"}

    def _headers(self) -> dict:
        ts = str(int(time.time() * 1000))
        sig = self.key.sign(f"{ts}GET{PRIVATE_PATH}".encode())
        return {"X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts,
                "X-PM-Signature": base64.b64encode(sig).decode()}

    def want(self, slugs) -> set[str]:
        return set()

    def alive(self) -> bool:
        return self.connected          # a quiet account sends little: connected is the signal

    async def _subscribe_missing(self) -> None:
        ws = self._ws
        if ws is None or "orders" in self._sent:
            return
        self._sent.add("orders")
        await ws.send(json.dumps({"subscribe": {"requestId": f"ord-{int(time.time() * 1000)}",
                                                "subscriptionType": "SUBSCRIPTION_TYPE_ORDER"}}))

    def _handle(self, raw) -> None:
        if len(self.samples) < 5:
            self.samples.append(str(raw)[:400])
        try:
            m = json.loads(raw)
        except ValueError:
            return
        if m.get("error"):
            self.error = str(m["error"])[:200]
        up = m.get("orderSubscriptionUpdate") or m.get("orderUpdate") or {}
        ex = up.get("execution") or m.get("execution") or {}
        o = ex.get("order") or up.get("order")
        if not isinstance(o, dict) or not o.get("id"):
            return
        self.orders[o["id"]] = (time.time(), o)
        cb = getattr(self, "on_update", None)
        if cb is not None:
            cb("pmorder:" + o["id"])

    def state(self, oid: str, c) -> dict | None:
        """maker's state dict for an order this stream has seen, else None."""
        hit = self.orders.get(oid)
        if hit is None or c is None:
            return None
        o = hit[1]
        cum = float(o.get("cumQuantity") or 0)
        avg_yes = float((o.get("avgPx") or {}).get("value") or 0)
        avg = (avg_yes if c.ref["side"] == "long" else 1 - avg_yes) if cum and avg_yes else 0.0   # no avgPx = unknown
        fee_v = (o.get("commissionNotionalTotalCollected") or {}).get("value")
        done = o.get("state") in ("ORDER_STATE_FILLED", "ORDER_STATE_CANCELED", "ORDER_STATE_REJECTED",
                                  "ORDER_STATE_EXPIRED")
        return {"state": o.get("state", ""), "done": done, "cum": cum, "avg": avg,
                "fee": float(fee_v) if fee_v not in (None, "") else 0.0}
