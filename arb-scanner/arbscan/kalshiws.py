"""Live Kalshi order books over the trade websocket (orderbook_delta channel).

Polling REST every scan left Kalshi prices up to a scan (~3 s) old at pricing time. The
websocket pushes a snapshot per market and then every change (11-03 on the box: all live
NBA markets, ~2,300 deltas/s, exchange->box 7 ms median, 17 ms p99, no gaps; ~17% of
one core), so this feed keeps the current book per market in memory and the scanner
reads it instead of polling.

Wire format and its traps (V3 exchange/ws_orderbook.py, learned live):
  subscribe -> {"id":N,"cmd":"subscribe","params":{"channels":["orderbook_delta"],"market_tickers":[...]}}
  ack       -> {"type":"subscribed","id":N,"msg":{"channel":"orderbook_delta","sid":S}}
  grow      -> {"id":N,"cmd":"update_subscription","params":{"sids":[S],"market_tickers":[...],"action":"add_markets"}}
               answered by {"type":"ok","sid":S,"seq":Q,...}
  snapshot  -> {"type":"orderbook_snapshot","sid":S,"seq":Q,"msg":{"market_ticker":T,"yes_dollars_fp":[...],"no_dollars_fp":[...]}}
  delta     -> {"type":"orderbook_delta","sid":S,"seq":Q,"msg":{"market_ticker":T,"price_dollars":"0.2800",
               "delta_fp":"-200.00","side":"yes","ts_ms":...}}
- ONE subscription, grown in place: several subscriptions share one seq counter and fake gaps.
- `seq` advances on EVERY frame of the subscription, control acks ('ok', 'error') included;
  skipping them reads as a gap (the gap source on V3 and on this feed's first version).
- Kalshi can kill the subscription and keep the socket ("Subscription buffer overflow",
  code 25; then code 7 "Unknown subscription ID"), so those errors, a real seq gap, or
  deltas lagging the exchange clock by > LAG_MAX all reconnect for fresh snapshots.
Levels are resting BIDS on each side (YES bids, NO bids), the same shape as REST orderbook_fp.
"""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time

URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
SIGN_PATH = "/trade-api/ws/v2"
DEAD_AFTER = 10.0        # seconds without an in-order book frame -> books presumed stale
LAG_MAX = 2.0            # seconds a delta may trail the exchange clock before the feed is rebuilt
DEAD_SUB_CODES = (7, 25)


class Book:
    __slots__ = ("yes", "no", "valid", "updated")

    def __init__(self):
        self.yes: dict[str, float] = {}     # price string -> size (resting YES bids)
        self.no: dict[str, float] = {}      # resting NO bids
        self.valid = False
        self.updated = 0.0

    def snapshot(self, msg: dict) -> None:
        self.yes = {p: float(s) for p, s in msg.get("yes_dollars_fp") or [] if float(s) > 0}
        self.no = {p: float(s) for p, s in msg.get("no_dollars_fp") or [] if float(s) > 0}
        self.valid, self.updated = True, time.time()

    def delta(self, msg: dict) -> None:
        side = self.yes if msg.get("side") == "yes" else self.no if msg.get("side") == "no" else None
        if side is None:
            return
        p = msg.get("price_dollars")
        try:
            size = round(side.get(p, 0.0) + float(msg.get("delta_fp")), 2)   # 2-dp sizes: quantize the residue away
        except (TypeError, ValueError):
            return
        if size >= 0.005:
            side[p] = size
        else:
            side.pop(p, None)
        self.updated = time.time()

    def levels(self) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """(YES bids, NO bids) best first, as (price, size)."""
        return (sorted(((float(p), s) for p, s in self.yes.items()), reverse=True),
                sorted(((float(p), s) for p, s in self.no.items()), reverse=True))


class BookFeed:
    def __init__(self, key_id: str, private_key, url: str = URL):
        self.key_id, self.key, self.url = key_id, private_key, url
        self.books: dict[str, Book] = {}
        self.wanted: set[str] = set()
        self._lock = threading.Lock()
        self._reset_session()
        self.connected = False
        self.last_msg = self.last_book = 0.0
        self.reconnects = self.gaps = self.resyncs = 0
        self.error = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._thread = threading.Thread(target=self._run, name="kalshi-books", daemon=True)
        self._thread.start()

    def _reset_session(self) -> None:
        self.sid: int | None = None
        self._seq: int | None = None
        self._sent: set[str] = set()       # tickers on the subscription (or in the initial subscribe)
        self._next_id = 1
        self._resync = False

    @classmethod
    def from_env(cls) -> "BookFeed | None":
        import os
        from cryptography.hazmat.primitives import serialization
        kid = os.environ.get("KALSHI_API_KEY_ID")
        pem = os.environ.get("KALSHI_PRIVATE_KEY")
        if pem:
            pem = pem.encode()
        elif os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
            with open(os.path.expanduser(os.environ["KALSHI_PRIVATE_KEY_PATH"]), "rb") as f:
                pem = f.read()
        if not kid or not pem:
            return None
        return cls(kid, serialization.load_pem_private_key(pem, password=None))

    # ------------------------------------------------------------ public

    def want(self, tickers) -> set[str]:
        new = set(tickers) - self.wanted
        if not new:
            return new
        with self._lock:
            self.wanted |= new
        if self._loop and self.connected:
            asyncio.run_coroutine_threadsafe(self._grow(), self._loop)
        return new

    def alive(self) -> bool:
        """Connected AND the subscription is delivering in-order book frames."""
        return self.connected and not self._resync and time.time() - self.last_book < DEAD_AFTER

    def get(self, ticker: str) -> Book | None:
        """Current book for `ticker`, or None while it has no snapshot / the feed is down or rebuilding."""
        b = self.books.get(ticker)
        return b if b is not None and b.valid and self.alive() else None

    def wait_for(self, tickers, timeout: float = 2.0) -> None:
        end = time.time() + timeout
        while time.time() < end and any(self.get(t) is None for t in tickers):
            time.sleep(0.05)

    # ------------------------------------------------------------ internals

    def _headers(self) -> dict:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        ts = str(int(time.time() * 1000))
        msg = f"{ts}GET{SIGN_PATH}".encode()
        if isinstance(self.key, Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._forever())

    def _invalidate_all(self) -> None:
        for b in self.books.values():
            b.valid = False

    async def _forever(self) -> None:
        import websockets
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, additional_headers=self._headers(), open_timeout=15,
                                              max_size=2 ** 24, ping_interval=20) as ws:
                    with self._lock:
                        self._reset_session()
                        self._invalidate_all()
                        self._ws, self.connected = ws, True
                    self.last_msg = time.time()
                    await self._grow()
                    while not self._resync:
                        raw = await asyncio.wait_for(ws.recv(), timeout=DEAD_AFTER * 3)
                        self.last_msg = time.time()
                        self._handle(raw)
                        if self.wanted - self._sent and self.sid is not None:
                            await self._grow()
                    self.resyncs += 1
                    backoff = 1.0             # a resync is a deliberate rebuild, not a failure
            except Exception as e:  # network, auth hiccup, server close: reconnect and re-subscribe everything
                self.error = f"{type(e).__name__}: {e}"[:200]
                backoff = min(backoff * 2, 30.0)
            with self._lock:
                self.connected, self._ws = False, None
                self._invalidate_all()
            self.reconnects += 1
            await asyncio.sleep(min(backoff, 1.0) if self._resync else backoff)

    async def _grow(self) -> None:
        """Initial subscribe once; afterwards add new markets to the same subscription."""
        ws = self._ws
        if ws is None:
            return
        with self._lock:
            todo = sorted(self.wanted - self._sent)
            if not todo or (self._sent and self.sid is None):
                return                         # nothing new, or the initial subscribe is not acked yet
            self._sent |= set(todo)
            cid = self._next_id
            self._next_id += 1
            if self.sid is None:
                cmd = {"id": cid, "cmd": "subscribe",
                       "params": {"channels": ["orderbook_delta"], "market_tickers": todo}}
            else:
                cmd = {"id": cid, "cmd": "update_subscription",
                       "params": {"sids": [self.sid], "market_tickers": todo, "action": "add_markets"}}
        await ws.send(json.dumps(cmd))

    def _handle(self, raw) -> None:
        try:
            m = json.loads(raw)
        except ValueError:
            return
        typ, msg = m.get("type"), m.get("msg") or {}
        if typ == "subscribed":
            if msg.get("channel") == "orderbook_delta" and msg.get("sid") is not None:
                self.sid = msg["sid"]
            return
        if typ == "error":
            self.error = str(msg)[:200]
            if msg.get("code") in DEAD_SUB_CODES:
                self._resync = True            # subscription dead on a live socket: rebuild it
                return
        sid, seq = m.get("sid"), m.get("seq")
        if sid is not None and self.sid is not None and sid != self.sid:
            return
        if seq is not None:                    # every frame on the subscription counts, control acks included
            if self._seq is not None and seq != self._seq + 1:
                self.gaps += 1
                self._resync = True
                return
            self._seq = seq
        if typ not in ("orderbook_snapshot", "orderbook_delta"):
            return
        t = msg.get("market_ticker")
        if not t:
            return
        if typ == "orderbook_snapshot":
            self.books.setdefault(t, Book()).snapshot(msg)
        else:
            b = self.books.get(t)
            if b is not None and b.valid:
                b.delta(msg)
            ts = msg.get("ts_ms")
            if ts and time.time() - ts / 1000 > LAG_MAX:
                self.error = f"deltas {time.time() - ts / 1000:.1f}s behind the exchange"
                self._resync = True
                return
        self.last_book = time.time()
