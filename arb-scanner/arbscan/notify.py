"""Telegram for the arb bot: one line per hedge outcome, sent LATE and never from the trading thread.

Operator 11-04: "a notification when we get a hedge fill; it does not have to be live, so it does not interfere with
the speed of it". So:
  * push() only appends to a deque: no lock, no I/O, no formatting beyond the caller's string. The trading thread
    calls it AFTER the hedge, the cancel and the ledger commit.
  * a daemon thread sends what is queued as ONE message once the queue has been still for SETTLE_S (30 s), so nothing
    goes out between a fill, its hedge retries, the cancel and the held-leg watcher.
  * quiet hours 21:00-05:00 MST (the operator sleeps): nothing is sent; the night's lines go out as one message at
    5 AM. urgent=True (the bot halted) is sent at any hour.
Off unless TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are in the environment (the box's box.sh sources .env; the repo
holds no secret). Standard library only. Nothing here raises into the caller.
"""
import collections
import datetime as dt
import json
import os
import threading
import time
import urllib.request

MST = dt.timezone(dt.timedelta(hours=-7))      # Arizona: no DST
QUIET_START_H, QUIET_END_H = 21, 5
SETTLE_S = 30.0          # send only after the queue has been still this long
TICK_S = 5.0
MAX_AGE_S = 12 * 3600.0  # a line that could not be sent for this long is dropped
MAX_LINES = 25
LABEL = "[ARB]"

_q: collections.deque = collections.deque(maxlen=500)      # (ts, text, urgent)
_cfg = {"token": "", "chat": "", "post": None, "thread": None}
stats = {"pushed": 0, "sent": 0, "failed": 0}


def configure(token: str = "", chat: str = "", post=None) -> None:
    """Set the destination (tests pass `post`, a callable(text) -> bool, in place of Telegram)."""
    _cfg.update(token=token or "", chat=chat or "", post=post)
    _q.clear()


def enabled() -> bool:
    return bool(_cfg["post"] or (_cfg["token"] and _cfg["chat"]))


def start(log=print) -> bool:
    """Read the token from the environment and start the sender thread. False (and silent pushes) without one."""
    configure(os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", ""))
    if not enabled():
        log("Telegram: off (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID in the environment)")
        return False
    if _cfg["thread"] is None:
        _cfg["thread"] = threading.Thread(target=_loop, name="tg-send", daemon=True)
        _cfg["thread"].start()
    log(f"Telegram: hedge outcomes are sent ~{SETTLE_S:.0f} s after the last one (never from the trading thread); "
        f"held {QUIET_START_H}:00-0{QUIET_END_H}:00 MST and sent at 5 AM; a halt is sent at once")
    push("started · hedge fills, failed hedges and a halt are reported here")     # proves the path after a restart
    return True


def push(text: str, urgent: bool = False) -> None:
    """Queue one line. Safe to call from the trading thread: an append, nothing else."""
    if not enabled():
        return
    try:
        _q.append((time.time(), str(text), bool(urgent)))
        stats["pushed"] += 1
    except Exception:
        pass


def quiet(ts: float) -> bool:
    h = dt.datetime.fromtimestamp(ts, MST).hour
    return h >= QUIET_START_H or h < QUIET_END_H


def cents(p: float) -> str:
    return ("%.1f" % (float(p) * 100)).rstrip("0").rstrip(".") + "¢"


def money(x: float) -> str:
    return ("+" if x >= 0 else "-") + "$%.2f" % abs(x)


def legs_text(fills) -> str:
    """'8 Boulais @ 54¢ on Polymarket + 8 Giron @ 41¢ on Kalshi' from a row's fills (orders that got nothing are left out)."""
    out = []
    for f in fills or []:
        if not f.get("qty"):
            continue
        out.append(f"{'sold ' if f.get('action') == 'sell' else ''}{f['qty']:g} {f.get('label', '?')} @ "
                   f"{cents(f.get('avg_price') or 0)} on {f.get('venue', '?')}")
    return " + ".join(out)


HEAD = {"hedged": "🛡 HEDGE FILLED", "partial": "⚠️ PART HEDGED", "exposed": "⚠️ NOT HEDGED", "unwound": "↺ UNWOUND",
        "error": "✖ ERROR", "sold": "✔ SOLD BACK"}


def trade_text(row: dict) -> str:
    """One ledger row in the operator's words: what filled where, and what it locked or leaves at risk."""
    fills = row.get("fills")
    if isinstance(fills, str):
        try:
            fills = json.loads(fills)
        except ValueError:
            fills = []
    o = row.get("outcome") or "?"
    s = f"{HEAD.get(o, o.upper())} #{row.get('id', '?')} · {row.get('game', '?')}"
    legs = legs_text(fills)
    if legs:
        s += f" · {legs}"
    locked, cash = row.get("locked_profit"), row.get("cash_out") or 0.0
    if o == "hedged" and locked is not None:
        s += f" · locked {money(locked)}"
    elif o in ("partial", "exposed", "error"):
        s += f" · ${max(cash, 0.0):.2f} at risk, one side held"
    return s


def trade(row: dict) -> None:
    """Queue a ledger row (trader._record): live rows that traded. A missed arb (nothing filled) is not a message."""
    try:
        if row.get("mode") != "live" or row.get("outcome") == "missed" or not enabled():
            return
        push(trade_text(row))
    except Exception:
        pass


def _post(text: str) -> bool:
    if _cfg["post"] is not None:
        return bool(_cfg["post"](text))
    body = json.dumps({"chat_id": _cfg["chat"], "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{_cfg['token']}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status == 200


def drain(now: float | None = None, force: bool = False) -> int:
    """Send what is due as one message; returns the lines sent. force=True (the bot is exiting) sends everything."""
    now = time.time() if now is None else now
    items = [x for x in list(_q)]
    for x in items:
        if now - x[0] > MAX_AGE_S:
            _remove(x)
    items = [x for x in items if now - x[0] <= MAX_AGE_S]
    if not items:
        return 0
    night = quiet(now) and not force
    if night:
        send = [x for x in items if x[2]]                    # asleep: only a halt goes out
    elif not force and not any(x[2] for x in items) and now - items[-1][0] < SETTLE_S:
        return 0                                             # still moving: wait until it has been still
    else:
        send = items
    if not send:
        return 0
    lines = [x[1] for x in send]
    more = len(lines) - MAX_LINES
    if more > 0:
        lines = lines[:MAX_LINES] + [f"+ {more} more (see the ledger)"]
    held = not night and any(quiet(x[0]) for x in send)
    head = f"🌙 {LABEL} overnight · {len(send)}" if held else LABEL if len(send) == 1 else f"{LABEL} {len(send)} updates"
    text = f"{head} {lines[0]}" if len(send) == 1 and not held else head + "\n" + "\n".join(lines)
    try:
        ok = _post(text)
    except Exception:
        ok = False
    if not ok:
        stats["failed"] += 1                                 # left in the queue: the next tick tries again
        return 0
    for x in send:
        _remove(x)
    stats["sent"] += len(send)
    return len(send)


def _remove(x) -> None:
    try:
        _q.remove(x)
    except ValueError:
        pass


def flush() -> None:
    """On exit: send whatever is queued now (the sender thread dies with the process)."""
    try:
        if enabled():
            drain(force=True)
    except Exception:
        pass


def _loop() -> None:
    while True:
        time.sleep(TICK_S)
        try:
            drain()
        except Exception:
            pass
