"""NQ/ES overnight-session signal for TMF cell.bias — frozen in-package copy.

Logic ported verbatim from the channel_lab research script
``reports/research/channel_lab/r5_synth_p0p1_vs_baseline.py``
(``load_futures_1h`` / ``futures_overnight_at`` / ``bias_side``) so the live
order path no longer imports research code at runtime. The research file stays
untouched in the lab; numeric parity is pinned by ``tests/test_tmf_nq_gate.py``.

Data: reads the same 1h NQ/ES cache file the R5 script reads (on mini it is a
symlink into ``${GOLDENSTOCKS_DATA_DIR}/cache/tmf_channel/``). Missing/broken
cache raises here — callers (``tmf_channel.nq_gate``) catch and fail safe.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import numpy as np

from stock_db import PROJECT_ROOT
from us_futures_overnight import (
    TZ_ET,
    US_RTH_CLOSE,
    overnight_pct,
    prior_us_rth_close_date,
    price_at_or_before,
)

# NQ_ES_1H_INTERVAL: matches fetch_yahoo_intraday_closes(..., interval="1h")
# in nq_gate.py's _load_futures_bundle -- an hourly bar is only settled once
# a full hour has elapsed since it started (see price_at_or_before's
# min_age docstring for the live incident this fixes).
NQ_ES_1H_MIN_AGE = timedelta(hours=1)

# Same file R5 reads (LAB / "nikkei_us_intraday_1h_cache.json").
NQ_ES_1H_CACHE = (
    PROJECT_ROOT / "reports/research/channel_lab/nikkei_us_intraday_1h_cache.json"
)

FLAT_EPS = 0.15


def load_futures_1h() -> tuple[Any, ...]:
    import pandas as pd

    raw = json.loads(NQ_ES_1H_CACHE.read_text())

    def to_series(key: str):
        recs = raw[key]["records"]
        idx, vals = [], []
        for r in recs:
            ts = pd.Timestamp(r["Datetime"])
            if ts.tzinfo is None:
                ts = ts.tz_localize(TZ_ET)
            else:
                ts = ts.tz_convert(TZ_ET)
            idx.append(ts)
            vals.append(float(r["Close"]))
        return pd.Series(vals, index=pd.DatetimeIndex(idx), dtype=float).sort_index()

    nq_1h = to_series("nq_futures_1h")
    es_1h = to_series("es_futures_1h")

    def daily_from_rth(intra):
        rows: dict[str, float] = {}
        for ts, px in intra.items():
            t = ts.timetz().replace(tzinfo=None)
            if t <= US_RTH_CLOSE:
                rows[ts.date().isoformat()] = float(px)
        return pd.Series(rows, dtype=float)

    nq_d = daily_from_rth(nq_1h)
    es_d = daily_from_rth(es_1h)
    us_dates = sorted(set(nq_d.index.astype(str)) | set(es_d.index.astype(str)))
    return nq_1h, es_1h, nq_d, es_d, us_dates


def futures_overnight_at(dt_tw, *, nq_1h, es_1h, nq_d, es_d, us_dates):
    dt_et = dt_tw.astimezone(TZ_ET)
    us_prior = prior_us_rth_close_date(dt_et, us_dates)
    if not us_prior:
        return None
    nq_prior = float(nq_d[us_prior]) if us_prior in nq_d.index else None
    es_prior = float(es_d[us_prior]) if us_prior in es_d.index else None
    nq_px = price_at_or_before(nq_1h, dt_et, min_age=NQ_ES_1H_MIN_AGE)
    es_px = price_at_or_before(es_1h, dt_et, min_age=NQ_ES_1H_MIN_AGE)
    if nq_px is None and es_px is None:
        return None
    return {
        "nq_overnight_pct": overnight_pct(nq_px, nq_prior),
        "es_overnight_pct": overnight_pct(es_px, es_prior),
    }


def bias_side(nq_ret: float | None, eps: float = FLAT_EPS) -> str:
    if nq_ret is None or not np.isfinite(nq_ret):
        return "missing"
    if nq_ret >= eps:
        return "up"
    if nq_ret <= -eps:
        return "down"
    return "flat"
