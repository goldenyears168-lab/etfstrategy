"""Back-adjusted close price construction from official ex-date reference prices.

Standard backward cumulative-factor method (matches CRSP/WRDS convention): walk
from the most recent trading day backward; each time an ex-date event is crossed,
multiply the running factor by (after_price / before_price) taken from the
exchange-published reference price pair — not a self-derived dividend-yield
formula, to avoid compounding rounding error across many events. Applies to both
dividend ex-dates (`TaiwanStockDividendResult`) and capital-reduction ex-dates
(`TaiwanStockCapitalReductionReferencePrice`) uniformly, since both datasets
already publish the official before/after reference price.

Diagnosis note (2026-07-28, residual error investigation for 2603 and others):
the after/before ratio-chaining logic itself is correct -- for every verifiable
event across the full capital-reduction universe, `before_price` matches the
actual raw close of the prior trading day to the penny (spot-checked 2603's all
12 usable events exactly; bulk-checked ~6600 event/stock pairs with a close
trading day available within 5 calendar days, only one genuine anomaly found,
see `load_adjustment_events` below). The large residual gap previously observed
between `adj_close_v2` and the legacy `stock_daily_bars.adj_close` column for
2603 pre-2022 dates (up to -16.7% at 2015-01) is NOT a bug here: it traces to
FinMind's own legacy `adj_close` (sourced from `TaiwanStockPriceAdj`) being
internally inconsistent with FinMind's *current* `TaiwanStockDividendResult`
values for that stock's older dividends -- e.g. for 2603's 2021-08-18 dividend,
the legacy adj_close series implies only a ~0.85% price-impact factor, while the
officially published reference prices (before=126.5, after=124.01, cash
dividend=2.49) imply the correct ~1.97% factor (126.5-2.49=124.01, exact). The
legacy column is already documented elsewhere as unreliable (~40% NULL rate);
this is further evidence it should not be treated as ground truth. adj_close_v2
matches it exactly for all of 2603's 2022-06-29-onward history (mismatch=1.000000
to 6 decimal places for every event from the present back through that date),
diverging only where the legacy column itself goes stale further back in time.
"""
from __future__ import annotations

import sqlite3
from datetime import date as _date

from stock_db.util import utc_now_iso

# Cross-check tolerance/window for load_adjustment_events' raw_closes sanity
# check (see docstring there). Kept small and conservative on purpose: a wider
# lookback window (e.g. 45 days) was tried first and produced false positives
# on stocks with an *unrelated*, separately-tracked raw stock_daily_bars
# coverage gap in mid-2026 (e.g. 2478 has no bars at all between 2026-06-05 and
# 2026-07-14) -- those aren't bad events, just a stale reference close. 5
# calendar days safely covers ordinary weekends/single-day holidays (so still
# catches same-week duplicate-record bugs like 6235's) without misfiring on
# legitimate multi-week capital-reduction trading halts (which, when data is
# actually present, match `before_price` to the penny even across 60+ day
# gaps -- see e.g. stock 3536's 2015-03-20 event, gap=63 days, exact match).
_EVENT_SANITY_MAX_LOOKBACK_DAYS = 5
_EVENT_SANITY_TOLERANCE = 0.15


def _parse_iso_date(s: str) -> _date:
    y, m, d = s.split("-")
    return _date(int(y), int(m), int(d))


def _nearest_prior_close(
    raw_closes: dict[str, float], ex_date: str, max_lookback_days: int
) -> tuple[str, float] | None:
    """Most recent raw close strictly before ex_date, but only if that trading
    day is within max_lookback_days calendar days -- otherwise we can't tell
    whether a mismatch means bad event data or just a raw-bars coverage gap,
    so we return None (caller then skips the cross-check for that event)."""
    candidates = [d for d in raw_closes if d < ex_date]
    if not candidates:
        return None
    prior_date = max(candidates)
    try:
        gap = (_parse_iso_date(ex_date) - _parse_iso_date(prior_date)).days
    except ValueError:
        return None
    if gap > max_lookback_days:
        return None
    return prior_date, raw_closes[prior_date]


def upsert_stock_price_adjustment_events(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_price_adjustment_events (
            stock_id, ex_date, event_type, before_price, after_price, detail, source, synced_at
        ) VALUES (
            :stock_id, :ex_date, :event_type, :before_price, :after_price, :detail, :source, :synced_at
        )
        ON CONFLICT(stock_id, ex_date, event_type, source) DO UPDATE SET
            before_price=excluded.before_price,
            after_price=excluded.after_price,
            detail=excluded.detail,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def upsert_stock_close_adjusted(conn: sqlite3.Connection, rows: list[dict]) -> int:
    if not rows:
        return 0
    synced_at = utc_now_iso()
    sql = """
        INSERT INTO stock_close_adjusted (
            stock_id, trade_date, adj_close_v2, cum_factor, n_events_applied, source, synced_at
        ) VALUES (
            :stock_id, :trade_date, :adj_close_v2, :cum_factor, :n_events_applied, :source, :synced_at
        )
        ON CONFLICT(stock_id, trade_date, source) DO UPDATE SET
            adj_close_v2=excluded.adj_close_v2,
            cum_factor=excluded.cum_factor,
            n_events_applied=excluded.n_events_applied,
            synced_at=excluded.synced_at
    """
    payload = [{**r, "synced_at": synced_at} for r in rows]
    conn.executemany(sql, payload)
    conn.commit()
    return len(payload)


def load_adjustment_events(
    conn: sqlite3.Connection,
    stock_id: str,
    raw_closes: dict[str, float] | None = None,
) -> list[dict]:
    """Events sorted descending by ex_date (most recent first), one factor per
    (ex_date, event_type) pair. If before_price/after_price is missing or
    non-positive, the event is skipped (factor=1, no-op) rather than raising,
    since a small number of malformed FinMind rows shouldn't kill the whole
    stock's adjustment chain.

    If `raw_closes` (stock_id's own {trade_date: close}) is supplied, each
    event's before_price is additionally cross-checked against the actual raw
    close of the nearest prior trading day, when that day is within
    _EVENT_SANITY_MAX_LOOKBACK_DAYS calendar days of ex_date (see module-level
    comment for why the window is kept short). Events that deviate by more
    than _EVENT_SANITY_TOLERANCE are dropped as unreliable.

    This guards against a real FinMind upstream data bug found for stock 6235:
    TaiwanStockCapitalReductionReferencePrice returned the *same* record
    (before=5.86, after=11.71, "Making up losses") twice -- once correctly
    dated 2011-08-31, and once bogusly re-dated 2020-07-22 -- even though
    6235's actual close on 2020-07-21 (one trading day before) was 15.4, not
    5.86. Applying the duplicate would have roughly doubled the backward-
    adjustment factor for every pre-2020 date. A full-universe scan (~6600
    event/stock pairs with a raw close available within 5 days of ex_date)
    found this to be the only case exceeding the tolerance -- i.e. this is not
    a systematic issue, just this one bad upstream record."""
    rows = conn.execute(
        """
        SELECT ex_date, event_type, before_price, after_price
        FROM stock_price_adjustment_events
        WHERE stock_id = ?
        ORDER BY ex_date DESC
        """,
        (stock_id,),
    ).fetchall()
    events = []
    for ex_date, event_type, before_price, after_price in rows:
        if not before_price or not after_price or before_price <= 0:
            continue
        if raw_closes is not None:
            prior = _nearest_prior_close(raw_closes, ex_date, _EVENT_SANITY_MAX_LOOKBACK_DAYS)
            if prior is not None:
                prior_date, prior_close = prior
                if prior_close > 0:
                    deviation = abs(before_price - prior_close) / prior_close
                    if deviation > _EVENT_SANITY_TOLERANCE:
                        print(
                            f"  SKIP suspect adjustment event {stock_id} {ex_date} "
                            f"{event_type}: before_price={before_price} vs actual "
                            f"close {prior_close} on {prior_date} (deviation "
                            f"{deviation:.1%} > {_EVENT_SANITY_TOLERANCE:.0%} tolerance, "
                            f"gap={ (_parse_iso_date(ex_date)-_parse_iso_date(prior_date)).days }d)"
                        )
                        continue
        factor = after_price / before_price
        events.append({"ex_date": ex_date, "event_type": event_type, "factor": factor})
    return events


def compute_adjusted_close(
    raw_closes: dict[str, float],
    events: list[dict],
) -> dict[str, tuple[float, float, int]]:
    """raw_closes: {trade_date (YYYY-MM-DD str): close}.
    events: as returned by load_adjustment_events (sorted descending by ex_date).
    Returns {trade_date: (adj_close_v2, cum_factor, n_events_applied)}.

    Anchor = most recent date (cum_factor=1.0 there); walking backward, each
    time we cross an event's ex_date (ex_date > current date, i.e. the event
    happened strictly after this trading day), multiply the running factor.
    """
    dates_desc = sorted(raw_closes.keys(), reverse=True)
    out: dict[str, tuple[float, float, int]] = {}
    cum_factor = 1.0
    n_applied = 0
    ev_idx = 0
    n_events = len(events)
    for d in dates_desc:
        while ev_idx < n_events and events[ev_idx]["ex_date"] > d:
            cum_factor *= events[ev_idx]["factor"]
            n_applied += 1
            ev_idx += 1
        raw = raw_closes[d]
        out[d] = (raw * cum_factor, cum_factor, n_applied)
    return out
