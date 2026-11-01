"""Tiny JSON-over-HTTP client with retries and a per-host rate limit."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = "nfl-arbscan/2.0 (personal research)"
# Kalshi basic tier: ~20 reads/s. Polymarket US: 25 req/s per IP, shared with
# order traffic, so public reads keep to ~16/s and leave room for orders.
_MIN_INTERVAL = {"api.elections.kalshi.com": 0.08, "gateway.polymarket.us": 0.06}
_last: dict[str, float] = {}
_lock = threading.Lock()


def _throttle(url: str) -> None:
    host = urllib.parse.urlparse(url).hostname or ""
    gap = _MIN_INTERVAL.get(host)
    if not gap:
        return
    with _lock:
        wait = _last.get(host, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.monotonic()


def request(url: str, params=None, body=None, timeout: float = 20):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    err: Exception | None = None
    for attempt in range(4):
        _throttle(url)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                err = e
                time.sleep(1.0 * 2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
            time.sleep(1.0 * 2 ** attempt)
    raise err  # type: ignore[misc]


def get(url: str, params=None, timeout: float = 20):
    return request(url, params=params, timeout=timeout)


def post(url: str, body, timeout: float = 20):
    return request(url, body=body, timeout=timeout)
