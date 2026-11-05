"""When a venue cannot trade: a scheduled maintenance window, an exchange pause, an order refused as "paused".

11-05 08:04Z: Kalshi was in its Thursday maintenance. Two quotes still resting on Polymarket filled, both hedges
came back `409 trading_is_paused`, the bot counted two failed hedges, halted and was down four hours. A maker quote
is only safe while its hedge venue takes orders, so nothing rests -- and nothing is posted -- while either venue
is closed:

  scheduled   Kalshi: GET /exchange/schedule (weekly hours in ET + maintenance windows), re-read hourly. Its weekly
              Thursday 03:00-05:00 ET window is built in for when that read has never worked.
              Polymarket US: the documented weekly window, Thursday 06:00-08:00 ET (its game lists are EMPTY then).
              Quotes come off LEAD_S before a window and go back TAIL_S after it.
  live        Kalshi: GET /exchange/status every POLL_S on its own thread (trading_active, per exchange index).
  refused     an order answered "paused": that venue is closed at once for HOLD_S, doubling on every repeat within
              the hour (a status that says "active" while orders are refused must not re-open it every minute).

Everything here reads public endpoints; closed() itself is memory only (the trading thread calls it on every pass).
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
import urllib.request

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
USER_AGENT = "nfl-arbscan/2.0 (personal research)"

POLL_S = 5.0             # s between Kalshi exchange-status reads
SCHEDULE_S = 3600.0      # s between Kalshi schedule reads
STATUS_STALE_S = 30.0    # a status older than this says nothing (the schedule and the book feeds still do)
LEAD_S = 120.0           # quotes come off this long BEFORE a scheduled window
TAIL_S = 60.0            # ... and go back this long after it
HOLD_S = 60.0            # closed this long after an order was refused as paused (doubles per repeat, <= HOLD_MAX_S)
HOLD_MAX_S = 1800.0
PAUSE_NOTE = "venue pause, not a failed-hedge strike"      # in a trade row's note: recent_failures skips the row

_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# weekly windows in ET, used as they stand for Polymarket and as the fallback for Kalshi: (weekday, from, to, what)
_WEEKLY = {"Kalshi": [(3, "03:00", "05:00", "weekly maintenance")],
           "Polymarket": [(3, "06:00", "08:00", "weekly maintenance")]}


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.status: dict | None = None        # last /exchange/status reply
        self.status_at = 0.0
        self.status_error = ""
        self.schedule: dict | None = None      # last /exchange/schedule reply
        self.schedule_at = 0.0
        self.refused: dict[str, float] = {}    # venue -> closed until
        self.refusals: dict[str, list[float]] = {}
        self.windows: dict[str, tuple[float, list]] = {}      # venue -> (computed at, [(from, to, what)])
        self.on = False                        # schedule + status are consulted only once start() / enable() ran
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()


S = _State()


def reset() -> None:
    global S
    stop()
    S = _State()


# ---------------------------------------------------------------- Eastern time, without a tz database

def _nth_sunday(year: int, month: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(6 - d.weekday()) % 7)          # first Sunday
    return d + dt.timedelta(weeks=n - 1)


def et_offset(utc: dt.datetime) -> int:
    """Hours ET is behind UTC at this instant: 4 in daylight time (2nd Sunday of March 07:00Z .. 1st Sunday of
    November 06:00Z), else 5."""
    y = utc.year
    start = dt.datetime.combine(_nth_sunday(y, 3, 2), dt.time(7), dt.timezone.utc)
    end = dt.datetime.combine(_nth_sunday(y, 11, 1), dt.time(6), dt.timezone.utc)
    return 4 if start <= utc < end else 5


def _et_to_ts(day: dt.date, minutes: int) -> float:
    """Unix time of `minutes` after midnight ET on `day`."""
    local = dt.datetime.combine(day, dt.time(), dt.timezone.utc) + dt.timedelta(minutes=minutes)
    utc = local + dt.timedelta(hours=5)
    if et_offset(utc) == 4:
        utc = local + dt.timedelta(hours=4)
    return utc.timestamp()


def _minutes(hhmm: str) -> int:
    h, m = str(hhmm).split(":")[:2]
    return int(h) * 60 + int(m)


def _iso(s: str) -> float:
    return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()


# ---------------------------------------------------------------- scheduled windows

def _weekly(venue: str, now: float) -> list:
    out = []
    today = (dt.datetime.fromtimestamp(now, dt.timezone.utc) - dt.timedelta(hours=5)).date()
    for off in range(-1, 9):
        day = today + dt.timedelta(days=off)
        for wd, a, b, what in _WEEKLY.get(venue, []):
            if day.weekday() == wd:
                out.append((_et_to_ts(day, _minutes(a)), _et_to_ts(day, _minutes(b)), what))
    return out


def _from_schedule(sched: dict, now: float) -> list:
    """Closed windows of the next days from Kalshi's schedule: the gaps in each weekday's open hours (ET), plus its
    announced maintenance windows. A weekday the schedule does not mention is taken as open."""
    out = []
    sc = sched.get("schedule") or {}
    for w in sc.get("maintenance_windows") or []:
        out.append((_iso(w["start_datetime"]), _iso(w["end_datetime"]), "maintenance"))
    today = (dt.datetime.fromtimestamp(now, dt.timezone.utc) - dt.timedelta(hours=5)).date()
    for hours in sc.get("standard_hours") or []:
        lo = _iso(hours["start_time"]) if hours.get("start_time") else float("-inf")
        hi = _iso(hours["end_time"]) if hours.get("end_time") else float("inf")
        for off in range(-1, 9):
            day = today + dt.timedelta(days=off)
            spans = hours.get(_DAYS[day.weekday()])
            if spans is None:
                continue
            opened = []
            for sp in spans:
                a, b = _minutes(sp["open_time"]), _minutes(sp["close_time"])
                opened.append((a, b if b > a else 24 * 60))          # close 00:00 = the end of the day
            at = 0
            for a, b in sorted(opened):
                if a > at:
                    t0, t1 = _et_to_ts(day, at), _et_to_ts(day, a)
                    if lo <= t0 < hi:
                        out.append((t0, t1, "scheduled closure"))
                at = max(at, b)
            if at < 24 * 60:
                t0, t1 = _et_to_ts(day, at), _et_to_ts(day, 24 * 60)
                if lo <= t0 < hi:
                    out.append((t0, t1, "scheduled closure"))
    return out


def windows(venue: str, now: float | None = None) -> list:
    """[(from, to, what)] in unix time: when `venue` is scheduled to be closed, around now."""
    now = time.time() if now is None else now
    hit = S.windows.get(venue)
    if hit is not None and abs(now - hit[0]) < 600.0:
        return hit[1]
    out = None
    if venue == "Kalshi" and S.schedule is not None:
        try:
            out = _from_schedule(S.schedule, now)
        except Exception:                      # a shape we do not read: the built-in weekly window still stands
            out = None
    if out is None:
        out = _weekly(venue, now)
    S.windows[venue] = (now, out)
    return out


def _hhmm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%H:%MZ")


# ---------------------------------------------------------------- the question the trading thread asks

def closed(venue: str, shard=None, now: float | None = None, pad: bool = True) -> str:
    """Why `venue` cannot take an order now ("" = it can). pad: also during LEAD_S before and TAIL_S after a
    scheduled window (resting quotes); pad=False is for closing a held position, which may run to the last second."""
    now = time.time() if now is None else now
    if now < S.refused.get(venue, 0.0):
        return f"{venue} refused an order as paused (closed here until {_hhmm(S.refused[venue])})"
    if not S.on:
        return ""                              # a library / test / one-shot use: only a refusal seen here counts
    lead, tail = (LEAD_S, TAIL_S) if pad else (0.0, 0.0)
    for a, b, what in windows(venue, now):
        if a - lead <= now < b + tail:
            return f"{venue} {what} until {_hhmm(b)}" if now >= a else f"{venue} {what} starts {_hhmm(a)}"
    if venue == "Kalshi":
        st = S.status
        if st is not None and now - S.status_at <= STATUS_STALE_S:
            if st.get("trading_active") is False or st.get("exchange_active") is False:
                return "Kalshi trading is paused (exchange status)"
            if shard is not None:
                for row in st.get("exchange_index_statuses") or []:
                    if row.get("exchange_index") == shard and (row.get("trading_active") is False
                                                               or row.get("exchange_active") is False):
                        return f"Kalshi trading is paused on exchange {shard} (exchange status)"
    return ""


def quoting(k_shard=None, now: float | None = None) -> str:
    """Why no maker quote may rest now: either venue closed (a quote needs Polymarket to rest on and Kalshi to
    hedge on)."""
    return closed("Kalshi", k_shard, now) or closed("Polymarket", None, now)


def is_pause(error) -> bool:
    """An order reply that means "the venue is not trading", not "this order was bad"."""
    e = str(error or "").lower()
    return "is_paused" in e or "trading is paused" in e or "exchange is paused" in e or "trading_paused" in e


def note_refused(venue: str, now: float | None = None) -> float:
    """An order on `venue` was refused as paused: closed from now, for HOLD_S doubling per repeat within the hour."""
    now = time.time() if now is None else now
    with S.lock:
        seen = [t for t in S.refusals.get(venue, []) if now - t < 3600.0]
        hold = min(HOLD_S * 2 ** len(seen), HOLD_MAX_S)
        if not seen or now - seen[-1] > 5.0:               # the retries of one hedge are one refusal
            seen.append(now)
        S.refusals[venue] = seen
        S.refused[venue] = max(S.refused.get(venue, 0.0), now + hold)
        return S.refused[venue]


def refused(contract, error) -> bool:
    """True (and the venue is closed from now) when `error` from an order on `contract` says the venue is paused."""
    if not is_pause(error):
        return False
    note_refused(contract.venue)
    return True


def problem(now: float | None = None) -> str:
    """The live read is not working (the reader thread runs but has no fresh status): said in the log, because then
    only the schedule, the book feeds and a refused order stand between a pause and a resting quote."""
    now = time.time() if now is None else now
    if S.thread is None or now - S.status_at <= STATUS_STALE_S:
        return ""
    return f"Kalshi exchange status not readable ({S.status_error or 'no reply yet'})"


def text(now: float | None = None) -> str:
    """One phrase for the status lines: which venue is closed and why ("" when both trade)."""
    parts = [w for w in (closed("Kalshi", None, now), closed("Polymarket", None, now)) if w]
    return "; ".join(parts)


# ---------------------------------------------------------------- the reads (own thread, never the trading thread)

def _fetch(url: str, timeout: float = 5.0) -> dict:
    """One plain GET, outside http.py on purpose: its per-host throttle queues a read behind a full scan's burst
    (11-05: settlement reads stood 1.6-2.3 s behind the scan thread) and its retries sleep for seconds."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def refresh(now: float | None = None) -> None:
    """One round of reads: the status always, the schedule when it is due. Errors are kept, not raised."""
    now = time.time() if now is None else now
    try:
        st = _fetch(f"{KALSHI_API}/exchange/status")
        if not isinstance(st, dict) or "trading_active" not in st:
            raise ValueError(f"unreadable status: {str(st)[:120]}")
        S.status, S.status_at, S.status_error = st, now, ""
    except Exception as e:
        S.status_error = f"{type(e).__name__}: {e}"[:160]
    if now - S.schedule_at >= SCHEDULE_S:
        try:
            sc = _fetch(f"{KALSHI_API}/exchange/schedule")
            if isinstance(sc, dict) and isinstance(sc.get("schedule"), dict):
                _from_schedule(sc, now)                    # parses, or it is not adopted
                S.schedule, S.schedule_at = sc, now
                S.windows.pop("Kalshi", None)
            else:
                S.schedule_at = now - SCHEDULE_S + 300.0   # look again in 5 min
        except Exception:
            S.schedule_at = now - SCHEDULE_S + 300.0


def _run() -> None:
    while not S.stop.is_set():
        refresh()
        S.stop.wait(POLL_S)


def enable() -> None:
    """Consult the schedule and the exchange status from now on (start() does this; tests call it alone)."""
    S.on = True


def start() -> None:
    """Start the reader thread (once). The first read is made here, before any quote can be posted."""
    enable()
    if S.thread is not None and S.thread.is_alive():
        return
    S.stop.clear()
    refresh()
    S.thread = threading.Thread(target=_run, name="venue-hours", daemon=True)
    S.thread.start()


def stop() -> None:
    S.stop.set()


def describe(now: float | None = None) -> str:
    """Start-up line: what is known and the next scheduled windows."""
    now = time.time() if now is None else now
    nxt = []
    for v in ("Kalshi", "Polymarket"):
        ahead = sorted(w for w in windows(v, now) if w[1] > now)
        if ahead:
            a, b, what = ahead[0]
            day = dt.datetime.fromtimestamp(a, dt.timezone.utc).strftime("%a")
            nxt.append(f"{v} {what} {day} {_hhmm(a)}-{_hhmm(b)}")
    src = "exchange schedule" if S.schedule is not None else "built-in weekly window (schedule not read)"
    live = ("Kalshi exchange status every %g s" % POLL_S if S.status is not None
            else f"Kalshi exchange status NOT readable ({S.status_error or 'no read yet'})")
    return (f"Venue hours: no quote rests from {LEAD_S:g} s before a closure to {TAIL_S:g} s after it, or while Kalshi "
            f"says trading is paused · {live} · Kalshi windows from its {src} · next: " + ("; ".join(nxt) or "none"))
