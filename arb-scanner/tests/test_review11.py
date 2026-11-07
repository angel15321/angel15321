"""Review 11 (11-07): reused HTTPS connections for the order calls.

Every Kalshi / Polymarket order call opened a new TCP + TLS connection (urllib urlopen): ~12-15 ms of handshake on
each cancel, post, read and Kalshi hedge. keepalive.Pool reuses them. The rules tested here are the safety ones:
an order POST is never sent twice, a dropped or stale connection is never reused, a timeout is never retried.

Run:  python3 -m unittest discover -s tests
"""
import http.client
import http.server
import json
import os
import socket
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from arbscan import brokers, keepalive  # noqa: E402


class Resp:
    def __init__(self, status=200, body=b"{}", will_close=False):
        self.status, self._body, self.will_close = status, body, will_close

    def read(self):
        return self._body


class Conn:
    """A fake connection: `script` = what each request does (a Resp, or an exception to raise)."""
    made = 0

    def __init__(self, script):
        Conn.made += 1
        self.script, self.sent, self.closed = list(script), [], False

    def request(self, method, path, body=None, headers=None):
        self.sent.append((method, path, body))
        self._next = self.script.pop(0) if self.script else Resp()
        if isinstance(self._next, BaseException) and getattr(self._next, "on_send", False):
            raise self._next

    def getresponse(self):
        if isinstance(self._next, BaseException):
            raise self._next
        return self._next

    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def pool_with(scripts, clock=None, is_dropped=lambda c: False):
    made = []

    def factory():
        c = Conn(scripts.pop(0) if scripts else [])
        made.append(c)
        return c
    p = keepalive.Pool("https://x.example/trade-api/v2", factory=factory, clock=clock or Clock(),
                       is_dropped=is_dropped)
    return p, made


class TestPool(unittest.TestCase):
    def test_reuses_one_connection(self):
        p, made = pool_with([[Resp(), Resp(), Resp()]])
        for _ in range(3):
            self.assertEqual(p.request("GET", "/a")[0], 200)
        self.assertEqual(len(made), 1)
        self.assertEqual(p.stats["reused"], 2)
        self.assertEqual(made[0].sent[0][1], "/trade-api/v2/a")         # the base URL's path is kept

    def test_idle_too_long_is_not_reused(self):
        clock = Clock()
        p, made = pool_with([[Resp()], [Resp()]], clock=clock)
        p.request("GET", "/a")
        clock.t += keepalive.IDLE_MAX + 1
        p.request("GET", "/a")
        self.assertEqual(len(made), 2)
        self.assertTrue(made[0].closed)

    def test_dropped_by_server_is_not_reused(self):
        p, made = pool_with([[Resp()], [Resp()]], is_dropped=lambda c: True)
        p.request("POST", "/orders", b"{}")
        p.request("POST", "/orders", b"{}")
        self.assertEqual(len(made), 2)
        self.assertEqual(p.stats["dropped"], 1)
        self.assertEqual(len(made[0].sent), 1)

    def test_order_post_on_a_reused_connection_is_never_sent_twice(self):
        p, made = pool_with([[Resp(), http.client.RemoteDisconnected("gone")], [Resp()]])
        p.request("POST", "/orders", b"{}")
        with self.assertRaises(http.client.RemoteDisconnected):
            p.request("POST", "/orders", b"{}", retry=False)
        sent = sum(len(c.sent) for c in made)
        self.assertEqual(sent, 2)                                         # first order + the failed one, no resend
        self.assertEqual(p.stats["retried"], 0)
        self.assertTrue(made[0].closed)

    def test_cancel_on_a_reused_connection_is_retried_once(self):
        p, made = pool_with([[Resp(), ConnectionResetError("reset")], [Resp(body=b'{"ok":1}')]])
        p.request("GET", "/a")
        status, body = p.request("POST", "/order/1/cancel", b"{}", retry=True)
        self.assertEqual((status, json.loads(body)), (200, {"ok": 1}))
        self.assertEqual(p.stats["retried"], 1)
        self.assertEqual(len(made), 2)

    def test_fresh_connection_failure_is_not_retried(self):
        p, made = pool_with([[ConnectionResetError("reset")], [Resp()]])
        with self.assertRaises(ConnectionResetError):
            p.request("GET", "/a", retry=True)
        self.assertEqual(len(made), 1)

    def test_timeout_is_never_retried(self):
        p, made = pool_with([[Resp(), socket.timeout("slow")], [Resp()]])
        p.request("GET", "/a")
        with self.assertRaises(socket.timeout):
            p.request("POST", "/order/1/cancel", b"{}", retry=True)
        self.assertEqual(len(made), 1)
        self.assertTrue(made[0].closed)
        p.request("GET", "/a")                                            # a closed connection is not reused
        self.assertEqual(len(made), 2)

    def test_will_close_reply_is_not_pooled(self):
        p, made = pool_with([[Resp(will_close=True)], [Resp()]])
        p.request("GET", "/a")
        p.request("GET", "/a")
        self.assertEqual(len(made), 2)

    def test_http_error_keeps_the_connection(self):
        p, made = pool_with([[Resp(404, b"nope"), Resp()]])
        self.assertEqual(p.request("GET", "/a"), (404, b"nope"))
        p.request("GET", "/a")
        self.assertEqual(len(made), 1)

    def test_free_list_is_capped(self):
        p, made = pool_with([[Resp()] for _ in range(keepalive.MAX_FREE + 3)])
        conns = [p._get()[0] for _ in range(keepalive.MAX_FREE + 3)]
        for c in conns:
            p._put(c)
        self.assertEqual(len(p._free), keepalive.MAX_FREE)
        self.assertEqual(sum(c.closed for c in conns), 3)

    def test_threads_share_the_pool(self):
        p, made = pool_with([[Resp()] * 50 for _ in range(10)])
        errs = []

        def run():
            try:
                for _ in range(20):
                    p.request("GET", "/a")
            except Exception as e:                                        # pragma: no cover
                errs.append(e)
        ts = [threading.Thread(target=run) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errs, [])
        self.assertLessEqual(len(made), 6)
        self.assertEqual(p.stats["new"] + p.stats["reused"], 120)


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    peers = set()
    hang_up = False          # True: close the TCP connection after this reply without saying so (an idle close)

    def do_GET(self):
        Handler.peers.add(self.client_address[1])
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if Handler.hang_up:
            self.close_connection = True

    def log_message(self, *a):
        pass


class Server(http.server.ThreadingHTTPServer):
    closed = None            # threading.Event: set once the server has really closed a client connection

    def shutdown_request(self, request):
        super().shutdown_request(request)
        if Server.closed is not None:
            Server.closed.set()


class TestRealSockets(unittest.TestCase):
    """Real loopback connections: reuse works, and a server-side close is seen before the next send."""

    def setUp(self):
        Handler.peers, Handler.hang_up = set(), False
        Server.closed = threading.Event()
        self.srv = Server(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        port = self.srv.server_address[1]
        self.made = []

        def factory():
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            self.made.append(c)
            return c
        self.pool = keepalive.Pool("http://127.0.0.1", factory=factory)

    def tearDown(self):
        self.pool.close()
        self.srv.shutdown()
        self.srv.server_close()

    def test_reuse_and_server_close(self):
        for _ in range(3):
            self.assertEqual(self.pool.request("GET", "/x")[0], 200)
        self.assertEqual(len(Handler.peers), 1)                           # one TCP connection for three calls
        conn = self.pool._free[-1][0]
        self.assertFalse(keepalive.dropped(conn))
        Handler.hang_up = True
        self.assertEqual(self.pool.request("GET", "/x")[0], 200)          # the server closes after this reply
        Handler.hang_up = False
        conn = self.pool._free[-1][0]
        self.assertTrue(Server.closed.wait(5))                            # the server side is closed now
        for _ in range(500):                                              # and its FIN reaches our socket
            if keepalive.dropped(conn):
                break
            select_wait(0.01)
        self.assertTrue(keepalive.dropped(conn))
        self.assertEqual(self.pool.request("GET", "/x")[0], 200)          # dropped one skipped, new one opened
        self.assertEqual(self.pool.stats["dropped"], 1)
        self.assertEqual(len(self.made), 2)
        self.assertEqual(len(Handler.peers), 2)


def select_wait(s):
    import select
    select.select([], [], [], s) if os.name != "nt" else threading.Event().wait(s)


class FakePool:
    def __init__(self, status=200, body=b'{"ok": 1}'):
        self.status, self.body, self.calls = status, body, []

    def request(self, method, path, body=None, headers=None, retry=False):
        self.calls.append((method, path, body, retry))
        return self.status, self.body


def kalshi(pool):
    b = object.__new__(brokers.KalshiBroker)
    b.base, b.key_id = brokers.KALSHI_TRADE_API, "k"
    b._sign = lambda m, p: {"KALSHI-ACCESS-KEY": "k"}
    b._http_pool, pool.for_base = pool, b.base
    return b


def poly(pool):
    b = object.__new__(brokers.PolymarketBroker)
    b.base, b.key_id = brokers.POLY_US_API, "k"
    b._sign = lambda m, p: {"X-PM-Access-Key": "k"}
    b._http_pool, pool.for_base = pool, b.base
    return b


class TestBrokers(unittest.TestCase):
    def test_kalshi_retry_only_on_reads(self):
        fp = FakePool()
        b = kalshi(fp)
        self.assertEqual(b._req("GET", "/portfolio/balance"), {"ok": 1})
        b._req("POST", "/portfolio/events/orders", {"x": 1})
        self.assertEqual([(m, p, r) for m, p, _, r in fp.calls],
                         [("GET", "/portfolio/balance", True), ("POST", "/portfolio/events/orders", False)])
        self.assertEqual(fp.calls[1][2], b'{"x": 1}')

    def test_kalshi_http_error_message_unchanged(self):
        b = kalshi(FakePool(409, b'{"error":"trading_is_paused"}'))
        with self.assertRaises(RuntimeError) as e:
            b._req("POST", "/portfolio/events/orders", {})
        self.assertEqual(str(e.exception),
                         'Kalshi POST /portfolio/events/orders → HTTP 409: {"error":"trading_is_paused"}')

    def test_polymarket_retry_only_reads_and_cancels(self):
        fp = FakePool()
        b = poly(fp)
        b._req("GET", "/v1/order/abc")
        b._req("POST", "/v1/order/abc/cancel", {"marketSlug": "s"})
        b._req("POST", "/v1/orders", {"q": 1})
        b._req("POST", "/v1/order/abc/modify", {"q": 1})
        self.assertEqual([r for *_, r in fp.calls], [True, True, False, False])

    def test_polymarket_http_error_message_unchanged(self):
        b = poly(FakePool(404, b"Order not found"))
        with self.assertRaises(RuntimeError) as e:
            b._req("GET", "/v1/order/abc")
        self.assertEqual(str(e.exception), "Polymarket GET /v1/order/abc → HTTP 404: Order not found")

    def test_order_error_is_a_failed_fill_not_a_resend(self):
        class Boom(FakePool):
            def request(self, *a, **k):
                self.calls.append(a)
                raise http.client.RemoteDisconnected("closed")
        fp = Boom()
        b = kalshi(fp)
        c = mock.Mock(label="X", ref={"side": "yes", "ticker": "T"})
        f = b.buy(c, 5, 0.5)
        self.assertEqual(f.status, "error")
        self.assertEqual(len(fp.calls), 1)

    def test_pool_made_per_base(self):
        b = object.__new__(brokers.PolymarketBroker)
        b.base = brokers.POLY_US_API
        p1 = brokers._pool(b)
        self.assertIs(brokers._pool(b), p1)
        self.assertEqual((p1.host, p1.prefix), ("api.polymarket.us", ""))
        k = object.__new__(brokers.KalshiBroker)
        k.base = brokers.KALSHI_TRADE_API
        self.assertEqual(brokers._pool(k).prefix, "/trade-api/v2")


if __name__ == "__main__":
    unittest.main()
