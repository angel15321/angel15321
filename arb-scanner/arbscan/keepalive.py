"""Reused HTTPS connections for the order calls (review 11, 11-07).

Every Kalshi and Polymarket order call went through urllib.request.urlopen, which opens a new TCP + TLS connection
per call and closes it: ~12-15 ms of handshake on the box (TLS 11-15 ms to both venues' edges) on every cancel,
post, modify, order read and Kalshi hedge. A Pool keeps the connections open and hands the most recently used one
to the next call, from any thread (the trading thread and the maker's cancel/read workers share one pool per venue).

Safety rules, because a POST that places an order must never be sent twice:
- Before reusing a connection it is checked for a close from the server (readable socket = FIN or stray data) and
  dropped if so, or if it sat idle longer than idle_max. That catches the usual idle close before anything is sent.
- If a REUSED connection still fails without a reply, the call is retried once on a fresh connection ONLY when the
  caller says it is safe (retry=True: reads and cancels). An order POST is never retried: the error goes up exactly
  as before, so the caller books it as a failed call, never as a double order.
- A timeout is never retried (the server may be working on it).
"""
from __future__ import annotations

import http.client
import select
import threading
import time
import urllib.parse

IDLE_MAX = 50.0          # s a free connection may sit before it is dropped instead of reused
MAX_FREE = 8             # free connections kept per pool (6 maker workers + the trading thread + one spare)


def dropped(conn) -> bool:
    """True when the server has closed the connection (or it has no socket): it must not be reused."""
    sock = getattr(conn, "sock", None)
    if sock is None:
        return True
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    return bool(readable)        # nothing is owed to us between calls: readable = EOF or garbage


class Pool:
    def __init__(self, base: str, timeout: float = 15.0, idle_max: float = IDLE_MAX, factory=None,
                 clock=time.monotonic, is_dropped=dropped):
        u = urllib.parse.urlsplit(base)
        self.host, self.port = u.hostname, u.port or 443
        self.prefix = u.path.rstrip("/")
        self.timeout, self.idle_max = timeout, idle_max
        self._factory = factory or (lambda: http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout))
        self._clock, self._is_dropped = clock, is_dropped
        self._free: list[tuple[object, float]] = []
        self._lock = threading.Lock()
        self.stats = {"new": 0, "reused": 0, "dropped": 0, "retried": 0}

    def _get(self):
        now = self._clock()
        stale = []
        with self._lock:
            while self._free:
                conn, at = self._free.pop()              # most recently used first: the warmest connection
                if now - at > self.idle_max or self._is_dropped(conn):
                    self.stats["dropped"] += 1
                    stale.append(conn)
                    continue
                self.stats["reused"] += 1
                hit = conn
                break
            else:
                hit = None
                self.stats["new"] += 1
        for conn in stale:
            _close(conn)
        if hit is not None:
            return hit, True
        return self._factory(), False

    def _put(self, conn) -> None:
        extra = None
        with self._lock:
            self._free.append((conn, self._clock()))
            if len(self._free) > MAX_FREE:
                extra = self._free.pop(0)[0]             # the coldest one goes
        if extra is not None:
            _close(extra)

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                retry: bool = False) -> tuple[int, bytes]:
        """(HTTP status, body bytes). Errors without a reply are raised as they come (http.client / OSError)."""
        for attempt in (0, 1):
            conn, reused = self._get()
            try:
                conn.request(method, self.prefix + path, body=body, headers=headers or {})
                resp = conn.getresponse()
                data = resp.read()
            except (http.client.HTTPException, ConnectionError):
                _close(conn)
                if reused and retry and attempt == 0:
                    with self._lock:
                        self.stats["retried"] += 1
                    continue
                raise
            except BaseException:
                _close(conn)                             # timeouts included: never retried
                raise
            if resp.will_close:
                _close(conn)
            else:
                self._put(conn)
            return resp.status, data
        raise RuntimeError("unreachable")                # pragma: no cover

    def close(self) -> None:
        with self._lock:
            free, self._free = self._free, []
        for conn, _ in free:
            _close(conn)


def _close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass
