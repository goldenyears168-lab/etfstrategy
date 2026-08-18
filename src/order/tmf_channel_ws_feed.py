"""Live futopt 1m-candle feed via Fubon's marketdata websocket (Normal mode).

2026-08-13: order.tmf_channel_marketdata.fetch_1m_bars() was hitting Fugle
429 rate limits intermittently all session (~11% of polls, self-recovering
but skipping that poll's flatten/exit check) from its REST
``intraday.candles(symbol, timeframe="1")`` call every ~20s poll. Confirmed
via a standalone shadow script (scripts/research/tmf_websocket_candle_
shadow.py) that Fubon's own futopt websocket ``candles`` channel pushes the
same 1m bars continuously with zero REST calls -- but only in
``Mode.Normal`` (the SDK default ``init_realtime()`` uses ``Mode.Speed``,
which explicitly rejects the ``candles``/``aggregates`` channels).

Safety design -- this is a *day-session REST call replacement only*, not a
wholesale rewrite:
  - Only the plain (no ``session="afterhours"``) REST call is eligible for
    replacement. The separate night-session REST call in fetch_1m_bars is
    left untouched, since websocket behavior across the night session was
    never observed/validated (the shadow smoke test only ran during day
    hours) -- no assumption is made about it either way.
  - "Prefer websocket, fall back to REST" via ``fresh()``: if no message has
    arrived within the TTL (covers cold start, disconnect, or an
    unconfirmed night-session gap), callers transparently get the exact
    REST call they'd have made anyway. A total websocket failure degrades
    to today's pre-existing behavior, not a worse one.
  - Kill switch: ``ORDER_TMF_CHANNEL_WS_MARKETDATA_ENABLED=0`` forces pure
    REST without a code change (still needs scripts/order/tmf_cutover.sh to
    take effect, .env is only read at worker start).
  - Raw rows pushed by the websocket are the *same shape* Fubon's REST
    ``intraday.candles`` already returns (date/open/high/low/close/volume
    -- confirmed field-for-field via the shadow smoke test), so they flow
    through fetch_1m_bars' existing _row_dt/_normalize_bar pipeline
    unchanged -- no new parsing path to trust.

Known, accepted limitation: a feed instance is tied to one (session, symbol)
pair; on session renewal (~58min, see tmf_channel.session_pool) or a
front-month contract roll, the old feed's websocket connection is simply
abandoned (idle thread + socket, no live impact) rather than explicitly
torn down -- a handful of times per trading day, not worth the lifecycle
wiring this would take to avoid.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


#: A day session is 300 minutes and a night session 840; 2,000 candles is
#: comfortably more than any single session while still bounding the dict.
_MAX_ROWS = 2000


class WebsocketCandleFeed:
    def __init__(self, session: Any, symbol: str) -> None:
        self._session = session
        self._symbol = symbol
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        self._last_msg_mono = 0.0
        self._started = False
        self._handlers_bound = False

    def start(self) -> None:
        if self._started:
            return
        from fubon_neo.adapter import Mode

        self._session.sdk.init_realtime(mode=Mode.Normal)
        ws = self._session.sdk.marketdata.websocket_client.futopt
        # Bind the callbacks ONCE per feed. _on_disconnect resets _started, so
        # every reconnect re-entered start() and appended another "message"
        # handler to the same websocket client — after N reconnects each
        # inbound candle was parsed N times, on the websocket thread, forever.
        # 2026-08-17 measurement: poll latency steps irreversibly from 0.16s to
        # 5.5s roughly 4.5h into a worker's life and never recovers, and the
        # 2026-08-14 hang happened out of exactly that degraded state.
        if not self._handlers_bound:
            ws.on("message", self._on_message)
            ws.on("disconnect", self._on_disconnect)
            self._handlers_bound = True
        ws.connect()
        ws.subscribe({"channel": "candles", "symbol": self._symbol})
        self._started = True

    def _prune_locked(self) -> None:
        """Keep only the newest _MAX_ROWS minutes.

        ``_rows`` was never pruned: it accumulated every candle the socket ever
        pushed, across days, while ``get_rows()`` re-sorted and rebuilt the
        whole dict on EVERY 20s poll. Unbounded input × per-poll full scan is
        the shape of the observed latency knee.
        """
        if len(self._rows) <= _MAX_ROWS:
            return
        for k in sorted(self._rows)[: len(self._rows) - _MAX_ROWS]:
            del self._rows[k]

    def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        data = msg.get("data")
        if not isinstance(data, dict):
            return
        event = msg.get("event")
        if event == "snapshot":
            rows = data.get("data")
            if isinstance(rows, list):
                with self._lock:
                    for row in rows:
                        d = row.get("date")
                        if d and isinstance(row, dict) and "open" in row:
                            self._rows[str(d)] = row
                    self._prune_locked()
                    self._last_msg_mono = time.monotonic()
        elif event == "data" and "date" in data and "open" in data:
            with self._lock:
                self._rows[str(data["date"])] = data
                self._prune_locked()
                self._last_msg_mono = time.monotonic()

    def _on_disconnect(self, code: Any, msg: Any) -> None:
        # Freshness-based fallback (see fresh()) makes a stuck reconnect
        # safe by construction -- worst case is a permanent REST fallback
        # for the rest of this feed's life, matching pre-websocket behavior.
        self._started = False

    def fresh(self, max_age_sec: float = 90.0) -> bool:
        with self._lock:
            if not self._started or self._last_msg_mono <= 0:
                return False
            return (time.monotonic() - self._last_msg_mono) < max_age_sec

    def get_rows(self) -> list[dict]:
        with self._lock:
            return [self._rows[k] for k in sorted(self._rows)]


_FEEDS: dict[int, WebsocketCandleFeed] = {}


def get_day_rows_via_ws(session: Any, symbol: str) -> list[dict] | None:
    """Fresh websocket-fed day-session rows, or None to signal REST fallback."""
    if not _env_flag("ORDER_TMF_CHANNEL_WS_MARKETDATA_ENABLED", "1"):
        return None
    key = id(session)
    feed = _FEEDS.get(key)
    if feed is None or feed._symbol != symbol:
        feed = WebsocketCandleFeed(session, symbol)
        _FEEDS[key] = feed
    if not feed._started:
        try:
            feed.start()
        except Exception:
            return None
    if not feed.fresh():
        return None
    rows = feed.get_rows()
    return rows or None
