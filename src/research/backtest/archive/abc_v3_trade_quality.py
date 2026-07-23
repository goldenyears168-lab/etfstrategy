"""ABC v3 skip09 · good/bad trade profiling · RRG + FinMind factor scan."""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime
from typing import Any, Callable

import pandas as pd

from project_config import DEFAULT_ETF_CODES
from research.backtest.archive.abc_v3_slot_sweep import (
    ABC_V3_HOLD_DAYS,
    collect_abc_v3_skip0900_period_candidates,
    split_train_val_dates,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.triple_wma_pullback_sweep import _summarize_trades
from research.backtest.w3_rv_hl_winner_profile import (
    _cohen_d,
    _factor_specs,
    _mean,
    _numeric_lifts,
    profile_winners_losers,
    scan_factor_discriminators,
)

from stock_db.market import load_us_futures_overnight_snapshots

US_FUTURES_NUMERIC_FIELDS: tuple[str, ...] = (
    "es_overnight_pct",
    "nq_overnight_pct",
    "es_price",
    "nq_price",
)

CROSS_ASSET_NUMERIC_FIELDS: tuple[str, ...] = US_FUTURES_NUMERIC_FIELDS

FINMIND_NUMERIC_FIELDS: tuple[str, ...] = (
    "foreign_net",
    "investment_trust_net",
    "dealer_self_net",
    "three_institution_net",
    "foreign_net_20d",
    "trust_net_20d",
    "three_inst_net_20d",
    "margin_change",
    "short_change",
    "margin_chg_20d",
    "short_chg_20d",
    "lending_change",
    "daytrade_ratio_pct",
    "foreign_ratio",
    "return_1d_pct",
    "return_5d_pct",
    "vol_ratio_5d",
    "kd_k",
    "pe",
    "pb",
    "roe_ttm",
    "dividend_yield",
    "branch_smart_net",
    "branch_retail_net",
)

RRG_NUMERIC_FIELDS: tuple[str, ...] = (
    "w20_rv",
    "w20_mv",
    "w5_rv",
    "w5_mv",
    "w3_rv",
    "w3_mv",
    "w2_rv",
    "w2_mv",
    "w20_w3_rv_spread",
    "w5_w20_rv_spread",
    "w5_w3_rv_spread",
    "spread_dist",
    "spread_rs",
    "spread_mom",
    "hook_retrace_pct",
    "etf_hold_count",
    "entry_poll_idx",
)


def flatten_abc_v3_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for c in candidates:
        feats = dict(c.get("features") or {})
        net = float(c["net_pct"])
        rows.append(
            {
                **{k: v for k, v in c.items() if k != "features"},
                **feats,
                "outcome": "win" if net > 0 else "loss",
                "big_win": net >= 2.0,
                "big_loss": net <= -2.0,
                "daily_ret_net_pct": round(net / ABC_V3_HOLD_DAYS, 4),
            }
        )
    return rows


def collect_abc_v3_skip0900_featured_trades(
    conn: sqlite3.Connection,
    *,
    dates: list[str],
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> list[dict[str, Any]]:
    candidates, _frames, _meta = collect_abc_v3_skip0900_period_candidates(
        conn, dates=dates, etf_codes=etf_codes, hold_days=ABC_V3_HOLD_DAYS
    )
    return flatten_abc_v3_candidates(candidates)


# FinMind chip / technical rows are post-close · intraday entry on T may only use ≤ T-1.
FINMIND_PIT_LAG_DAYS = 1


def _prior_trade_date(cal: list[str], trade_date: str) -> str | None:
    if trade_date in cal:
        i = cal.index(trade_date)
        return cal[i - 1] if i > 0 else None
    prior = [d for d in cal if d < trade_date]
    return prior[-1] if prior else None


def pit_as_of_date(entry_date: str, cal: list[str], *, lag_days: int = FINMIND_PIT_LAG_DAYS) -> str | None:
    """Last trade_date with data knowable before entry_date open/polls."""
    d = entry_date
    for _ in range(lag_days):
        prev = _prior_trade_date(cal, d)
        if prev is None:
            return None
        d = prev
    return d


def _pivot_daily(
    rows: list[sqlite3.Row],
    *,
    date_col: str,
    value_cols: tuple[str, ...],
) -> dict[str, pd.DataFrame]:
    if not rows:
        return {c: pd.DataFrame() for c in value_cols}
    df = pd.DataFrame([dict(r) for r in rows])
    df[date_col] = df[date_col].astype(str)
    out: dict[str, pd.DataFrame] = {}
    for col in value_cols:
        wide = df.pivot_table(
            index=date_col, columns="stock_id", values=col, aggfunc="last"
        )
        wide.index = pd.to_datetime(wide.index)
        out[col] = wide.sort_index()
    return out


def _rolling_sum_lookup(wide: pd.DataFrame, stock_id: str, as_of: str, window: int = 20) -> float | None:
    if wide.empty or stock_id not in wide.columns:
        return None
    s = wide[stock_id].dropna()
    if s.empty:
        return None
    rolled = s.rolling(window=window, min_periods=max(5, window // 2)).sum()
    ts = pd.Timestamp(as_of)
    if ts not in rolled.index:
        sub = rolled.loc[:ts]
        if sub.empty:
            return None
        val = sub.iloc[-1]
    else:
        val = rolled.loc[ts]
    return float(val) if pd.notna(val) else None


def _lookup(wide: pd.DataFrame, stock_id: str, as_of: str) -> float | None:
    if wide.empty or stock_id not in wide.columns:
        return None
    ts = pd.Timestamp(as_of)
    s = wide[stock_id].dropna()
    if s.empty:
        return None
    if ts in s.index:
        val = s.loc[ts]
    else:
        sub = s.loc[:ts]
        if sub.empty:
            return None
        val = sub.iloc[-1]
    return float(val) if pd.notna(val) else None


def _pit_fundamental_lookup(
    fund: pd.DataFrame,
    stock_id: str,
    as_of: str,
    col: str,
) -> float | None:
    if fund.empty:
        return None
    sub = fund[(fund["stock_id"] == stock_id) & (fund["as_of_date"] <= as_of)].sort_values("as_of_date")
    if sub.empty:
        return None
    val = sub.iloc[-1][col]
    return float(val) if val is not None and pd.notna(val) else None


def enrich_trades_finmind(
    conn: sqlite3.Connection,
    trades: list[dict[str, Any]],
    *,
    lookback_days: int = 30,
    pit_lag_days: int = FINMIND_PIT_LAG_DAYS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach FinMind chip / technical / fundamental fields · PIT lagged to prior close.

    Intraday ABC v3 entries (09:30–13:30) must not use same-day institutional / margin /
    daytrade / branch rows (published after close). All FinMind joins use entry_date minus
    ``pit_lag_days`` trading days (default 1 = T-1 close).
    """
    if not trades:
        return [], {"n_trades": 0}

    stock_ids = sorted({str(t["stock_id"]) for t in trades})
    entry_dates = sorted({str(t["entry_date"]) for t in trades})
    close, _, _ = load_price_panels(conn)
    cal = close.index.astype(str).tolist()
    start = entry_dates[0]
    if entry_dates[0] in cal:
        i0 = cal.index(entry_dates[0])
        start = cal[max(0, i0 - lookback_days)]
    end = entry_dates[-1]
    ph = ",".join("?" * len(stock_ids))
    args = [*stock_ids, start, end]

    inst_rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, foreign_net, investment_trust_net,
               dealer_self_net, three_institution_net
        FROM stock_institutional_daily
        WHERE stock_id IN ({ph}) AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        args,
    ).fetchall()
    inst = _pivot_daily(
        inst_rows,
        date_col="trade_date",
        value_cols=("foreign_net", "investment_trust_net", "dealer_self_net", "three_institution_net"),
    )
    inst["three_inst_net_20d"] = inst["three_institution_net"]  # alias for rolling

    margin_rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, margin_change, short_change
        FROM stock_margin_daily
        WHERE stock_id IN ({ph}) AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        args,
    ).fetchall()
    margin = _pivot_daily(margin_rows, date_col="trade_date", value_cols=("margin_change", "short_change"))

    lend_rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, lending_change
        FROM stock_lending_daily
        WHERE stock_id IN ({ph}) AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        args,
    ).fetchall()
    lending = _pivot_daily(lend_rows, date_col="trade_date", value_cols=("lending_change",))

    dt_rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, daytrade_ratio_pct
        FROM stock_daytrade_daily
        WHERE stock_id IN ({ph}) AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        args,
    ).fetchall()
    daytrade = _pivot_daily(dt_rows, date_col="trade_date", value_cols=("daytrade_ratio_pct",))

    sh_rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, foreign_remaining_ratio
        FROM stock_shareholding_daily
        WHERE stock_id IN ({ph}) AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        args,
    ).fetchall()
    shareholding = _pivot_daily(sh_rows, date_col="trade_date", value_cols=("foreign_remaining_ratio",))

    tech_rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, return_1d_pct, return_5d_pct, vol_ratio_5d, kd_k
        FROM stock_technical_daily
        WHERE stock_id IN ({ph}) AND trade_date >= ? AND trade_date <= ?
        """,
        args,
    ).fetchall()
    technical = _pivot_daily(
        tech_rows,
        date_col="trade_date",
        value_cols=("return_1d_pct", "return_5d_pct", "vol_ratio_5d", "kd_k"),
    )

    branch_rows = conn.execute(
        f"""
        SELECT stock_id, trade_date, smart_net, retail_net
        FROM stock_branch_daily
        WHERE stock_id IN ({ph}) AND trade_date >= ? AND trade_date <= ?
          AND source = 'finmind'
        """,
        args,
    ).fetchall()
    branch = _pivot_daily(branch_rows, date_col="trade_date", value_cols=("smart_net", "retail_net"))

    fund_rows = conn.execute(
        f"""
        SELECT stock_id, as_of_date, pe, pb, roe_ttm, dividend_yield
        FROM stock_fundamental
        WHERE stock_id IN ({ph}) AND as_of_date <= ?
          AND source = 'finmind'
        ORDER BY as_of_date
        """,
        [*stock_ids, end],
    ).fetchall()
    fund = pd.DataFrame([dict(r) for r in fund_rows]) if fund_rows else pd.DataFrame()

    coverage: dict[str, int] = {f: 0 for f in FINMIND_NUMERIC_FIELDS}
    enriched: list[dict[str, Any]] = []

    for t in trades:
        sid = str(t["stock_id"])
        entry_d = str(t["entry_date"])
        pit_d = pit_as_of_date(entry_d, cal, lag_days=pit_lag_days)
        row = dict(t)
        row["finmind_pit_date"] = pit_d
        if pit_d is None:
            enriched.append(row)
            continue
        row["foreign_net"] = _lookup(inst["foreign_net"], sid, pit_d)
        row["investment_trust_net"] = _lookup(inst["investment_trust_net"], sid, pit_d)
        row["dealer_self_net"] = _lookup(inst["dealer_self_net"], sid, pit_d)
        row["three_institution_net"] = _lookup(inst["three_institution_net"], sid, pit_d)
        row["foreign_net_20d"] = _rolling_sum_lookup(inst["foreign_net"], sid, pit_d)
        row["trust_net_20d"] = _rolling_sum_lookup(inst["investment_trust_net"], sid, pit_d)
        row["three_inst_net_20d"] = _rolling_sum_lookup(inst["three_institution_net"], sid, pit_d)
        row["margin_change"] = _lookup(margin["margin_change"], sid, pit_d)
        row["short_change"] = _lookup(margin["short_change"], sid, pit_d)
        row["margin_chg_20d"] = _rolling_sum_lookup(margin["margin_change"], sid, pit_d)
        row["short_chg_20d"] = _rolling_sum_lookup(margin["short_change"], sid, pit_d)
        row["lending_change"] = _lookup(lending["lending_change"], sid, pit_d)
        row["daytrade_ratio_pct"] = _lookup(daytrade["daytrade_ratio_pct"], sid, pit_d)
        row["foreign_ratio"] = _lookup(shareholding["foreign_remaining_ratio"], sid, pit_d)
        row["return_1d_pct"] = _lookup(technical["return_1d_pct"], sid, pit_d)
        row["return_5d_pct"] = _lookup(technical["return_5d_pct"], sid, pit_d)
        row["vol_ratio_5d"] = _lookup(technical["vol_ratio_5d"], sid, pit_d)
        row["kd_k"] = _lookup(technical["kd_k"], sid, pit_d)
        row["branch_smart_net"] = _lookup(branch["smart_net"], sid, pit_d)
        row["branch_retail_net"] = _lookup(branch["retail_net"], sid, pit_d)
        row["pe"] = _pit_fundamental_lookup(fund, sid, pit_d, "pe")
        row["pb"] = _pit_fundamental_lookup(fund, sid, pit_d, "pb")
        row["roe_ttm"] = _pit_fundamental_lookup(fund, sid, pit_d, "roe_ttm")
        row["dividend_yield"] = _pit_fundamental_lookup(fund, sid, pit_d, "dividend_yield")
        for f in FINMIND_NUMERIC_FIELDS:
            if row.get(f) is not None:
                coverage[f] += 1
        enriched.append(row)

    meta = {
        "n_trades": len(enriched),
        "pit_lag_days": pit_lag_days,
        "pit_rule": "FinMind chip/technical/fundamental · as_of = entry_date minus lag trading days",
        "finmind_coverage_pct": {
            f: round(100.0 * coverage[f] / len(enriched), 1) for f in FINMIND_NUMERIC_FIELDS
        },
    }
    return enriched, meta


def _finmind_factor_specs() -> list[tuple[str, str, Callable[[dict[str, Any]], bool]]]:
    def _num(field: str, op: str, thr: float) -> Callable[[dict[str, Any]], bool]:
        def _fn(r: dict[str, Any]) -> bool:
            v = r.get(field)
            if v is None:
                return False
            return float(v) >= thr if op == "gte" else float(v) <= thr

        return _fn

    return [
        ("fm_foreign_net_pos", "外資當日淨買>0", _num("foreign_net", "gte", 1.0)),
        ("fm_foreign_net_neg", "外資當日淨賣<0", _num("foreign_net", "lte", -1.0)),
        ("fm_trust_net_pos", "投信當日淨買>0", _num("investment_trust_net", "gte", 1.0)),
        ("fm_three_inst_pos", "三大法人淨買>0", _num("three_institution_net", "gte", 1.0)),
        ("fm_foreign_20d_pos", "外資20d累計淨買>0", _num("foreign_net_20d", "gte", 1.0)),
        ("fm_foreign_20d_neg", "外資20d累計淨賣<0", _num("foreign_net_20d", "lte", -1.0)),
        ("fm_trust_20d_pos", "投信20d累計淨買>0", _num("trust_net_20d", "gte", 1.0)),
        ("fm_margin_chg_pos", "融資增", _num("margin_change", "gte", 1.0)),
        ("fm_margin_chg_neg", "融資減", _num("margin_change", "lte", -1.0)),
        ("fm_margin_20d_pos", "融資20d累增>0", _num("margin_chg_20d", "gte", 1.0)),
        ("fm_short_20d_pos", "融券20d累增>0", _num("short_chg_20d", "gte", 1.0)),
        ("fm_daytrade_high", "當沖比≥30%", _num("daytrade_ratio_pct", "gte", 30.0)),
        ("fm_daytrade_low", "當沖比<20%", _num("daytrade_ratio_pct", "lte", 20.0)),
        ("fm_ret5d_pos", "5日報酬>0", _num("return_5d_pct", "gte", 0.01)),
        ("fm_ret5d_neg", "5日報酬<0", _num("return_5d_pct", "lte", -0.01)),
        ("fm_vol_ratio_high", "量比≥1.2", _num("vol_ratio_5d", "gte", 1.2)),
        ("fm_kd_low", "KD K≤50", _num("kd_k", "lte", 50.0)),
        ("fm_kd_high", "KD K≥70", _num("kd_k", "gte", 70.0)),
        ("fm_foreign_ratio_high", "外資持股≥30%", _num("foreign_ratio", "gte", 30.0)),
        ("fm_pe_low", "PE≤20", _num("pe", "lte", 20.0)),
        ("fm_pe_high", "PE≥40", _num("pe", "gte", 40.0)),
        ("fm_pb_low", "PB≤2", _num("pb", "lte", 2.0)),
        ("fm_roe_high", "ROE≥10", _num("roe_ttm", "gte", 10.0)),
        ("fm_branch_smart_pos", "分點smart淨買>0", _num("branch_smart_net", "gte", 1.0)),
        ("fm_branch_retail_neg", "分點retail淨賣<0", _num("branch_retail_net", "lte", -1.0)),
    ]


def _us_futures_factor_specs() -> list[tuple[str, str, Callable[[dict[str, Any]], bool]]]:
    def _num(field: str, op: str, thr: float) -> Callable[[dict[str, Any]], bool]:
        def _fn(r: dict[str, Any]) -> bool:
            v = r.get(field)
            if v is None:
                return False
            return float(v) >= thr if op == "gte" else float(v) <= thr

        return _fn

    return [
        ("us_es_on_pos", "ES overnight>0", _num("es_overnight_pct", "gte", 0.05)),
        ("us_es_on_neg", "ES overnight<0", _num("es_overnight_pct", "lte", -0.05)),
        ("us_es_on_strong", "ES overnight≥0.3%", _num("es_overnight_pct", "gte", 0.3)),
        ("us_es_on_weak", "ES overnight≤−0.3%", _num("es_overnight_pct", "lte", -0.3)),
        ("us_nq_on_pos", "NQ overnight>0", _num("nq_overnight_pct", "gte", 0.05)),
        ("us_nq_on_neg", "NQ overnight<0", _num("nq_overnight_pct", "lte", -0.05)),
        ("us_nq_on_strong", "NQ overnight≥0.4%", _num("nq_overnight_pct", "gte", 0.4)),
        ("us_nq_on_weak", "NQ overnight≤−0.4%", _num("nq_overnight_pct", "lte", -0.4)),
        (
            "us_es_nq_both_pos",
            "ES&NQ overnight>0",
            lambda r: (
                r.get("es_overnight_pct") is not None
                and float(r["es_overnight_pct"]) > 0
                and r.get("nq_overnight_pct") is not None
                and float(r["nq_overnight_pct"]) > 0
            ),
        ),
    ]


def _snapshot_lookup_index(rows: list[Any]) -> dict[tuple[str, str], dict[str, Any]]:
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        d = dict(row)
        out[(str(d["tw_session_date"]), str(d["capture_label"]))] = d
    return out


def _resolve_us_futures_snapshot(
    idx: dict[tuple[str, str], dict[str, Any]],
    entry_date: str,
    entry_minute: str,
    *,
    poll_labels: tuple[str, ...],
) -> dict[str, Any] | None:
    minute = entry_minute or ""
    if (entry_date, minute) in idx:
        return idx[(entry_date, minute)]
    prior = [m for m in poll_labels if m <= minute]
    if prior:
        return idx.get((entry_date, prior[-1]))
    return None


def enrich_trades_us_futures_overnight(
    conn: sqlite3.Connection,
    trades: list[dict[str, Any]],
    *,
    poll_labels: tuple[str, ...] = (
        "09:30",
        "10:00",
        "10:30",
        "11:00",
        "11:30",
        "12:00",
        "12:30",
        "13:00",
        "13:30",
    ),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach ES/NQ overnight% at entry poll (PIT · Yahoo 1h snapshot)."""
    if not trades:
        return [], {"n_trades": 0}
    dates = sorted({str(t["entry_date"]) for t in trades})
    try:
        rows = load_us_futures_overnight_snapshots(conn, start=dates[0], end=dates[-1])
    except sqlite3.OperationalError:
        rows = []
    idx = _snapshot_lookup_index(rows)
    coverage = 0
    enriched: list[dict[str, Any]] = []
    for t in trades:
        d = str(t["entry_date"])
        minute = str(t.get("entry_minute") or "")
        snap = _resolve_us_futures_snapshot(idx, d, minute, poll_labels=poll_labels)
        row = dict(t)
        if snap:
            row["us_futures_capture_label"] = snap.get("capture_label")
            row["us_prior_trade_date"] = snap.get("us_prior_trade_date")
            row["es_price"] = snap.get("es_price")
            row["nq_price"] = snap.get("nq_price")
            row["es_overnight_pct"] = snap.get("es_overnight_pct")
            row["nq_overnight_pct"] = snap.get("nq_overnight_pct")
            if row.get("es_overnight_pct") is not None:
                coverage += 1
        enriched.append(row)
    return enriched, {
        "n_trades": len(enriched),
        "es_overnight_coverage_pct": round(100.0 * coverage / len(enriched), 1) if enriched else 0,
        "pit_rule": "ES/NQ price at or before entry poll (1h bar) vs prior US RTH close",
    }


def scan_discriminators(
    trades: list[dict[str, Any]],
    specs: list[tuple[str, str, Callable[[dict[str, Any]], bool]]],
    *,
    min_pos: int = 25,
    min_neg: int = 25,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for fid, label, pred in specs:
        pos = [t for t in trades if pred(t)]
        neg = [t for t in trades if not pred(t)]
        if len(pos) < min_pos or len(neg) < min_neg:
            continue
        pos_rets = [float(t["net_pct"]) for t in pos]
        neg_rets = [float(t["net_pct"]) for t in neg]
        mp, mn = _mean(pos_rets), _mean(neg_rets)
        rows.append(
            {
                "id": fid,
                "label": label,
                "n_pos": len(pos),
                "n_neg": len(neg),
                "mean_ret_pos": mp,
                "mean_ret_neg": mn,
                "lift": round((mp or 0) - (mn or 0), 3),
                "win_rate_pos": round(100.0 * sum(1 for x in pos_rets if x > 0) / len(pos_rets), 1),
                "win_rate_neg": round(100.0 * sum(1 for x in neg_rets if x > 0) / len(neg_rets), 1),
                "cohen_d": _cohen_d(pos_rets, neg_rets),
            }
        )
    rows.sort(key=lambda x: abs(float(x.get("lift") or 0)), reverse=True)
    return rows


def scan_numeric_median_splits(
    trades: list[dict[str, Any]],
    fields: tuple[str, ...],
    *,
    min_side: int = 25,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for field in fields:
        vals = [(t, float(t[field])) for t in trades if t.get(field) is not None]
        if len(vals) < min_side * 2:
            continue
        med = statistics.median([v for _, v in vals])
        lo = [t for t, v in vals if v <= med]
        hi = [t for t, v in vals if v > med]
        if len(lo) < min_side or len(hi) < min_side:
            continue
        lo_rets = [float(t["net_pct"]) for t in lo]
        hi_rets = [float(t["net_pct"]) for t in hi]
        ml, mh = _mean(lo_rets), _mean(hi_rets)
        better = "hi" if (mh or 0) >= (ml or 0) else "lo"
        rows.append(
            {
                "field": field,
                "median": round(med, 4),
                "mean_ret_lo": ml,
                "mean_ret_hi": mh,
                "lift_hi_minus_lo": round((mh or 0) - (ml or 0), 3),
                "better_side": better,
                "n_lo": len(lo),
                "n_hi": len(hi),
                "win_rate_lo": round(100.0 * sum(1 for x in lo_rets if x > 0) / len(lo_rets), 1),
                "win_rate_hi": round(100.0 * sum(1 for x in hi_rets if x > 0) / len(hi_rets), 1),
            }
        )
    rows.sort(key=lambda x: abs(float(x.get("lift_hi_minus_lo") or 0)), reverse=True)
    return rows


def classify_trade_tiers(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {"tiers": [], "good_trades": [], "bad_trades": []}
    sorted_trades = sorted(trades, key=lambda t: float(t["net_pct"]), reverse=True)
    n = len(sorted_trades)
    t1 = max(1, n // 3)
    t2 = max(1, (2 * n) // 3)
    good = sorted_trades[:t1]
    mid = sorted_trades[t1:t2]
    bad = sorted_trades[t2:]
    return {
        "n": n,
        "good_n": len(good),
        "mid_n": len(mid),
        "bad_n": len(bad),
        "good_mean_ret": _mean([float(t["net_pct"]) for t in good]),
        "mid_mean_ret": _mean([float(t["net_pct"]) for t in mid]),
        "bad_mean_ret": _mean([float(t["net_pct"]) for t in bad]),
        "good_trades": [_trade_brief(t) for t in good[:15]],
        "bad_trades": [_trade_brief(t) for t in bad[:15]],
        "good_win_rate_pct": round(100.0 * sum(1 for t in good if t["outcome"] == "win") / len(good), 1),
        "bad_win_rate_pct": round(100.0 * sum(1 for t in bad if t["outcome"] == "win") / len(bad), 1),
    }


def _trade_brief(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "stock_id": t.get("stock_id"),
        "stock_name": t.get("stock_name"),
        "entry_date": t.get("entry_date"),
        "entry_minute": t.get("entry_minute"),
        "net_pct": t.get("net_pct"),
        "outcome": t.get("outcome"),
        "w20_rv": t.get("w20_rv"),
        "w3_rv": t.get("w3_rv"),
        "w20_w3_rv_spread": t.get("w20_w3_rv_spread"),
        "foreign_net_20d": t.get("foreign_net_20d"),
        "return_5d_pct": t.get("return_5d_pct"),
        "margin_chg_20d": t.get("margin_chg_20d"),
    }


def search_half_filters(
    trades: list[dict[str, Any]],
    specs: list[tuple[str, str, Callable[[dict[str, Any]], bool]]],
    *,
    keep_lo: float = 0.35,
    keep_hi: float = 0.65,
    min_kept: int = 40,
) -> list[dict[str, Any]]:
    """Find filters that keep ~half the trades with best mean ret lift."""
    if not trades:
        return []
    baseline = _summarize_trades(trades)
    base_mean = float(baseline.get("mean_ret_3d_net_pct") or 0)
    n_all = len(trades)
    rows: list[dict[str, Any]] = []

    for fid, label, pred in specs:
        for side in ("keep_pos", "keep_neg"):
            kept = [t for t in trades if pred(t)] if side == "keep_pos" else [t for t in trades if not pred(t)]
            dropped = [t for t in trades if t not in kept]
            if len(kept) < min_kept or not dropped:
                continue
            keep_pct = len(kept) / n_all
            if keep_pct < keep_lo or keep_pct > keep_hi:
                continue
            s_kept = _summarize_trades(kept)
            s_drop = _summarize_trades(dropped)
            kept_mean = float(s_kept.get("mean_ret_3d_net_pct") or 0)
            drop_mean = float(s_drop.get("mean_ret_3d_net_pct") or 0)
            rows.append(
                {
                    "id": fid,
                    "label": label,
                    "side": side,
                    "keep_pct": round(keep_pct * 100, 1),
                    "n_kept": len(kept),
                    "n_drop": len(dropped),
                    "mean_ret_kept": kept_mean,
                    "mean_ret_drop": drop_mean,
                    "lift_vs_drop": round(kept_mean - drop_mean, 3),
                    "lift_vs_baseline": round(kept_mean - base_mean, 3),
                    "win_rate_kept": s_kept.get("win_rate_pct"),
                    "win_rate_drop": s_drop.get("win_rate_pct"),
                    "score": round((kept_mean - drop_mean) * (1.0 - abs(keep_pct - 0.5) / 0.5), 4),
                }
            )
    rows.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    return rows


def backtest_filter_combo(
    trades: list[dict[str, Any]],
    specs: list[tuple[str, str, Callable[[dict[str, Any]], bool]]],
    filter_ids: list[str],
    *,
    side_by_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    spec_map = {fid: (lbl, fn) for fid, lbl, fn in specs}
    side_by_id = side_by_id or {}

    def _keep(t: dict[str, Any]) -> bool:
        for fid in filter_ids:
            _lbl, fn = spec_map[fid]
            pos = fn(t)
            side = side_by_id.get(fid, "keep_pos")
            if side == "keep_pos" and not pos:
                return False
            if side == "keep_neg" and pos:
                return False
        return True

    kept = [t for t in trades if _keep(t)]
    dropped = [t for t in trades if not _keep(t)]
    return {
        "filter_ids": filter_ids,
        "n_kept": len(kept),
        "n_drop": len(dropped),
        "keep_pct": round(100.0 * len(kept) / len(trades), 1) if trades else 0,
        "kept": _summarize_trades(kept),
        "dropped": _summarize_trades(dropped),
        "baseline": _summarize_trades(trades),
    }


def profile_finmind_win_loss(trades: list[dict[str, Any]]) -> dict[str, Any]:
    wins = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]
    fin_lifts = _numeric_lifts(wins, losses, FINMIND_NUMERIC_FIELDS)
    rrg_lifts = _numeric_lifts(wins, losses, RRG_NUMERIC_FIELDS)
    return {
        "finmind_numeric_lifts": fin_lifts,
        "rrg_numeric_lifts": rrg_lifts,
    }


def _oos_focus_eval(
    train_trades: list[dict[str, Any]],
    val_trades: list[dict[str, Any]],
    specs: list[tuple[str, str, Callable[[dict[str, Any]], bool]]],
    filter_ids: list[str],
    side_by_id: dict[str, str],
    *,
    min_n: int = 20,
) -> dict[str, Any]:
    """Freeze a named filter (or combo) on train, then measure it on val.

    Unlike the auto-picked champion, the filter is fixed a priori so we can tell
    whether a specific in-sample hypothesis holds out of sample.
    """
    tr = backtest_filter_combo(train_trades, specs, filter_ids, side_by_id=side_by_id)
    vl = backtest_filter_combo(val_trades, specs, filter_ids, side_by_id=side_by_id)
    vk = vl.get("kept") or {}
    vb = vl.get("baseline") or {}
    kept_mean = float(vk.get("mean_ret_3d_net_pct") or 0)
    base_mean = float(vb.get("mean_ret_3d_net_pct") or 0)
    val_passed = kept_mean > base_mean and int(vk.get("n") or 0) >= max(10, min_n // 2)
    label = " + ".join(
        f"{next((l for i, l, _ in specs if i == fid), fid)}[{side_by_id.get(fid, 'keep_pos')}]"
        for fid in filter_ids
    )
    return {
        "filter_ids": filter_ids,
        "side_by_id": side_by_id,
        "label": label,
        "train": tr,
        "val": vl,
        "val_passed": val_passed,
    }


def validate_filter_oos(
    trades: list[dict[str, Any]],
    specs: list[tuple[str, str, Callable[[dict[str, Any]], bool]]],
    *,
    train_days: int = 60,
    val_days: int = 30,
    min_n: int = 20,
    focus_filters: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Pick ~half filter on train entry dates · apply frozen rule on val.

    ``focus_filters`` is an optional list of ``(filter_id, side)`` pairs that are
    frozen a priori (not auto-picked) and evaluated individually on train/val,
    plus a combined AND-gate when two or more are given.
    """
    entry_dates = sorted({str(t["entry_date"]) for t in trades})
    train_dates, val_dates = split_train_val_dates(
        entry_dates, train_days=train_days, val_days=val_days
    )
    train_set, val_set = set(train_dates), set(val_dates)
    train_trades = [t for t in trades if str(t["entry_date"]) in train_set]
    val_trades = [t for t in trades if str(t["entry_date"]) in val_set]

    train_half = search_half_filters(
        train_trades, specs, min_kept=max(min_n, 15), keep_lo=0.30, keep_hi=0.70
    )
    best = train_half[0] if train_half else None
    val_apply: dict[str, Any] | None = None
    if best and val_trades:
        val_apply = backtest_filter_combo(
            val_trades,
            specs,
            [str(best["id"])],
            side_by_id={str(best["id"]): str(best["side"])},
        )

    val_passed = False
    if val_apply and best:
        ck = val_apply.get("kept") or {}
        bl = val_apply.get("baseline") or {}
        kept_mean = float(ck.get("mean_ret_3d_net_pct") or 0)
        base_mean = float(bl.get("mean_ret_3d_net_pct") or 0)
        val_passed = kept_mean > base_mean and int(ck.get("n") or 0) >= max(10, min_n // 2)

    focus_eval: list[dict[str, Any]] = []
    focus_combo: dict[str, Any] | None = None
    if focus_filters and train_trades and val_trades:
        for fid, side in focus_filters:
            focus_eval.append(
                _oos_focus_eval(train_trades, val_trades, specs, [fid], {fid: side}, min_n=min_n)
            )
        if len(focus_filters) >= 2:
            side_map = {fid: side for fid, side in focus_filters}
            focus_combo = _oos_focus_eval(
                train_trades,
                val_trades,
                specs,
                [fid for fid, _ in focus_filters],
                side_map,
                min_n=min_n,
            )

    return {
        "train_days": len(train_dates),
        "val_days": len(val_dates),
        "n_train_trades": len(train_trades),
        "n_val_trades": len(val_trades),
        "train_baseline": _summarize_trades(train_trades),
        "val_baseline": _summarize_trades(val_trades),
        "train_champion_filter": best,
        "train_half_candidates": train_half[:8],
        "val_apply": val_apply,
        "val_passed": val_passed,
        "focus_eval": focus_eval,
        "focus_combo": focus_combo,
    }


def run_abc_v3_trade_quality_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-01",
    date_end: str | None = None,
    intraday_tail_days: int = 90,
    min_n: int = 25,
    etf_codes: tuple[str, ...] = DEFAULT_ETF_CODES,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    sample_dates = (
        trade_dates[-intraday_tail_days:]
        if intraday_tail_days > 0 and len(trade_dates) > intraday_tail_days
        else trade_dates
    )

    trades = collect_abc_v3_skip0900_featured_trades(conn, dates=sample_dates, etf_codes=etf_codes)
    trades, finmind_meta = enrich_trades_finmind(conn, trades)
    trades, us_futures_meta = enrich_trades_us_futures_overnight(conn, trades)

    baseline = _summarize_trades(trades)
    profile = profile_winners_losers(trades)
    tiers = classify_trade_tiers(trades)
    finmind_profile = profile_finmind_win_loss(trades)
    cross_wins = [t for t in trades if t.get("outcome") == "win"]
    cross_losses = [t for t in trades if t.get("outcome") == "loss"]
    cross_asset_lifts = _numeric_lifts(cross_wins, cross_losses, CROSS_ASSET_NUMERIC_FIELDS)

    rrg_specs = _factor_specs()
    fin_specs = _finmind_factor_specs()
    us_specs = _us_futures_factor_specs()
    pit_specs = rrg_specs + us_specs
    all_specs = rrg_specs + fin_specs + us_specs

    rrg_disc = scan_discriminators(trades, rrg_specs, min_pos=min_n, min_neg=min_n)
    fin_disc = scan_discriminators(trades, fin_specs, min_pos=min_n, min_neg=min_n)
    us_disc = scan_discriminators(trades, us_specs, min_pos=min_n, min_neg=min_n)
    pit_disc = scan_discriminators(trades, pit_specs, min_pos=min_n, min_neg=min_n)
    all_disc = scan_discriminators(trades, all_specs, min_pos=min_n, min_neg=min_n)

    rrg_med = scan_numeric_median_splits(trades, RRG_NUMERIC_FIELDS, min_side=min_n)
    fin_med = scan_numeric_median_splits(trades, FINMIND_NUMERIC_FIELDS, min_side=min_n)
    us_med = scan_numeric_median_splits(trades, US_FUTURES_NUMERIC_FIELDS, min_side=min_n)

    half_filters = search_half_filters(trades, all_specs, min_kept=max(min_n, 40))
    half_pit = search_half_filters(trades, pit_specs, min_kept=max(min_n, 25))

    oos = validate_filter_oos(trades, all_specs, train_days=60, val_days=30, min_n=min_n)
    oos_pit = validate_filter_oos(trades, pit_specs, train_days=60, val_days=30, min_n=min_n)
    oos_us = validate_filter_oos(trades, rrg_specs + us_specs, train_days=60, val_days=30, min_n=min_n)
    oos_rrg = validate_filter_oos(
        trades,
        rrg_specs,
        train_days=60,
        val_days=30,
        min_n=min_n,
        focus_filters=[("w3_rv_shallow", "keep_pos"), ("w5_w3_mv_pos", "keep_neg")],
    )

    combo: dict[str, Any] | None = None
    if len(half_filters) >= 2:
        top2 = half_filters[:2]
        combo = backtest_filter_combo(
            trades,
            all_specs,
            [str(r["id"]) for r in top2],
            side_by_id={str(r["id"]): str(r["side"]) for r in top2},
        )

    best_half = half_filters[0] if half_filters else None
    verdict_reasons: list[str] = []
    if best_half:
        verdict_reasons.append(
            f"best ~half filter **{best_half.get('label')}** ({best_half.get('side')}) · "
            f"keep {best_half.get('keep_pct')}% · ret {best_half.get('mean_ret_kept')}% vs drop {best_half.get('mean_ret_drop')}%"
        )
    if fin_disc:
        top_fin = fin_disc[0]
        verdict_reasons.append(
            f"top FinMind discriminator **{top_fin.get('label')}** · lift {top_fin.get('lift')}% · "
            f"pos ret {top_fin.get('mean_ret_pos')}% vs neg {top_fin.get('mean_ret_neg')}%"
        )
    if rrg_disc:
        top_rrg = rrg_disc[0]
        verdict_reasons.append(
            f"top RRG discriminator **{top_rrg.get('label')}** · lift {top_rrg.get('lift')}%"
        )
    if us_disc:
        top_us = us_disc[0]
        verdict_reasons.append(
            f"top US futures **{top_us.get('label')}** · lift {top_us.get('lift')}% · "
            f"ES coverage {us_futures_meta.get('es_overnight_coverage_pct')}%"
        )
    oos_us_val = oos_us.get("val_apply")
    oos_us_champ = oos_us.get("train_champion_filter")
    if oos_us_champ and oos_us_val:
        vk = oos_us_val.get("kept") or {}
        vb = oos_us_val.get("baseline") or {}
        verdict_reasons.append(
            f"OOS RRG+US · train **{oos_us_champ.get('label')}** · val ret {vk.get('mean_ret_3d_net_pct')}% "
            f"vs {vb.get('mean_ret_3d_net_pct')}% · "
            f"{'**passed**' if oos_us.get('val_passed') else 'failed'}"
        )
    pit_val = oos_pit.get("val_apply")
    pit_champ = oos_pit.get("train_champion_filter")
    if pit_champ and pit_val:
        vk = pit_val.get("kept") or {}
        vb = pit_val.get("baseline") or {}
        verdict_reasons.append(
            f"OOS PIT (RRG+US) · train **{pit_champ.get('label')}** · val ret {vk.get('mean_ret_3d_net_pct')}% "
            f"vs {vb.get('mean_ret_3d_net_pct')}% · "
            f"{'**passed**' if oos_pit.get('val_passed') else 'failed'}"
        )
    if combo:
        ck = combo.get("kept") or {}
        verdict_reasons.append(
            f"combo top-2 filters · keep {combo.get('keep_pct')}% · ret {ck.get('mean_ret_3d_net_pct')}% "
            f"vs baseline {baseline.get('mean_ret_3d_net_pct')}%"
        )
    for fe in oos_rrg.get("focus_eval") or []:
        vk = (fe.get("val") or {}).get("kept") or {}
        vb = (fe.get("val") or {}).get("baseline") or {}
        verdict_reasons.append(
            f"OOS RRG-only · **{fe.get('label')}** · val ret {vk.get('mean_ret_3d_net_pct')}% "
            f"vs {vb.get('mean_ret_3d_net_pct')}% · "
            f"{'**passed**' if fe.get('val_passed') else 'failed'}"
        )
    rrg_combo = oos_rrg.get("focus_combo")
    if rrg_combo:
        vk = (rrg_combo.get("val") or {}).get("kept") or {}
        vb = (rrg_combo.get("val") or {}).get("baseline") or {}
        verdict_reasons.append(
            f"OOS RRG-only combo · **{rrg_combo.get('label')}** · val ret {vk.get('mean_ret_3d_net_pct')}% "
            f"vs {vb.get('mean_ret_3d_net_pct')}% · "
            f"{'**passed**' if rrg_combo.get('val_passed') else 'failed'}"
        )

    champ = oos.get("train_champion_filter")
    val_apply = oos.get("val_apply")
    if champ and val_apply:
        vk = val_apply.get("kept") or {}
        vb = val_apply.get("baseline") or {}
        verdict_reasons.append(
            f"OOS val · train pick **{champ.get('label')}** ({champ.get('side')}) · "
            f"val keep {val_apply.get('keep_pct')}% · ret {vk.get('mean_ret_3d_net_pct')}% "
            f"vs val baseline {vb.get('mean_ret_3d_net_pct')}% · "
            f"{'**passed**' if oos.get('val_passed') else 'failed'}"
        )

    rrg_focus_passed = any(
        fe.get("val_passed") for fe in (oos_rrg.get("focus_eval") or [])
    ) or bool((oos_rrg.get("focus_combo") or {}).get("val_passed"))
    filterable = bool(
        oos_us.get("val_passed")
        or oos_pit.get("val_passed")
        or oos.get("val_passed")
        or oos_rrg.get("val_passed")
        or rrg_focus_passed
        or (
            best_half
            and float(best_half.get("lift_vs_drop") or 0) >= 0.5
            and float(best_half.get("mean_ret_kept") or 0) > float(baseline.get("mean_ret_3d_net_pct") or 0)
        )
    )

    return {
        "schema": "abc_v3_trade_quality-v3",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "intraday_tail_days": intraday_tail_days,
        "intraday_sample_dates": sample_dates,
        "matcher": "w3_rv_hl_abc_v3",
        "execution_gate": "min_poll_idx>=1 (skip 09:00)",
        "exit_rule": f"hold_{ABC_V3_HOLD_DAYS}d",
        "baseline": baseline,
        "baseline_daily_ret_pct": round(
            float(baseline.get("mean_ret_3d_net_pct") or 0) / ABC_V3_HOLD_DAYS, 4
        ),
        "finmind_meta": finmind_meta,
        "us_futures_meta": us_futures_meta,
        "cross_asset_lifts": cross_asset_lifts,
        "profile": profile,
        "tiers": tiers,
        "finmind_profile": finmind_profile,
        "rrg_discriminators": rrg_disc[:20],
        "finmind_discriminators": fin_disc[:20],
        "us_futures_discriminators": us_disc[:20],
        "pit_discriminators": pit_disc[:20],
        "all_discriminators": all_disc[:25],
        "rrg_median_splits": rrg_med[:15],
        "finmind_median_splits": fin_med[:15],
        "us_futures_median_splits": us_med[:15],
        "half_filters": half_filters[:15],
        "half_pit_filters": half_pit[:15],
        "oos_validation": oos,
        "oos_pit_validation": oos_pit,
        "oos_us_futures_validation": oos_us,
        "oos_rrg_validation": oos_rrg,
        "combo_top2": combo,
        "verdict": {
            "filterable": filterable,
            "reasons": verdict_reasons,
            "summary": (
                f"ABC v3 trade quality · n={baseline.get('n')} · baseline ret {baseline.get('mean_ret_3d_net_pct')}% · "
                f"ES cov {us_futures_meta.get('es_overnight_coverage_pct')}% · "
                f"OOS US {'**passed**' if oos_us.get('val_passed') else 'failed'} · "
                f"{'**filterable**' if filterable else 'weak lift'}"
            ),
        },
    }


def render_abc_v3_trade_quality_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    base = payload.get("baseline") or {}
    prof = payload.get("profile") or {}
    tiers = payload.get("tiers") or {}
    fin = payload.get("finmind_profile") or {}
    cov = (payload.get("finmind_meta") or {}).get("finmind_coverage_pct") or {}
    pit_meta = payload.get("finmind_meta") or {}
    us_meta = payload.get("us_futures_meta") or {}
    oos = payload.get("oos_validation") or {}
    oos_us = payload.get("oos_us_futures_validation") or {}
    oos_rrg = payload.get("oos_rrg_validation") or {}
    oos_pit = payload.get("oos_pit_validation") or {}

    lines = [
        "# ABC v3 · 好单 / 烂单 · RRG + FinMind",
        "",
        f"> {v.get('summary', '')}",
        "",
        "## PIT · 交易前可得性",
        "",
        f"- FinMind chip / 技术 / 基本面：**T-{pit_meta.get('pit_lag_days', 1)} 收盘后**（`finmind_pit_date`）",
        f"- 规则：{pit_meta.get('pit_rule', '')}",
        "- RRG 进场快照（w20_rv、spread、quadrant、entry_poll）：**进场 poll 当时可得** ✓",
        "- 三大法人 / 融资券 / 当冲比 / 分点：**当日盘后公布** → 盘中进场只能用前一日 ✓",
        f"- **ES/NQ overnight**：进场 poll 当时（Yahoo 1h · vs 前 US RTH 收盘）· coverage **{us_meta.get('es_overnight_coverage_pct')}%** ✓",
        "",
        "## 规格",
        "",
        f"- Matcher: **{payload.get('matcher')}** · exit **{payload.get('exit_rule')}**",
        f"- Execution: **{payload.get('execution_gate')}**",
        f"- Sample: **{len(payload.get('intraday_sample_dates') or [])}** days "
        f"(tail **{payload.get('intraday_tail_days')}**)",
        f"- Baseline: n=**{base.get('n')}** · mean total **{base.get('mean_ret_3d_net_pct')}%** · "
        f"daily **{payload.get('baseline_daily_ret_pct')}%** · win **{base.get('win_rate_pct')}%**",
        "",
        "## 好单 / 烂单（tertile by net_pct）",
        "",
        f"- Top third (好单): n={tiers.get('good_n')} · mean ret **{tiers.get('good_mean_ret')}%** · "
        f"win {tiers.get('good_win_rate_pct')}%",
        f"- Bottom third (烂单): n={tiers.get('bad_n')} · mean ret **{tiers.get('bad_mean_ret')}%** · "
        f"win {tiers.get('bad_win_rate_pct')}%",
        "",
        "### 好单样本（top 15）",
        "",
        "| stock | date | minute | net% | w20 RV | w3 RV | spread | foreign 20d | ret5d | margin 20d |",
        "|-------|------|--------|-----:|-------:|------:|-------:|------------:|------:|-----------:|",
    ]
    for t in tiers.get("good_trades") or []:
        lines.append(
            f"| {t.get('stock_id')} | {t.get('entry_date')} | {t.get('entry_minute')} | "
            f"**{t.get('net_pct')}** | {t.get('w20_rv')} | {t.get('w3_rv')} | {t.get('w20_w3_rv_spread')} | "
            f"{t.get('foreign_net_20d')} | {t.get('return_5d_pct')} | {t.get('margin_chg_20d')} |"
        )

    lines.extend(
        [
            "",
            "### 烂单样本（bottom 15）",
            "",
            "| stock | date | minute | net% | w20 RV | w3 RV | spread | foreign 20d | ret5d | margin 20d |",
            "|-------|------|--------|-----:|-------:|------:|-------:|------------:|------:|-----------:|",
        ]
    )
    for t in tiers.get("bad_trades") or []:
        lines.append(
            f"| {t.get('stock_id')} | {t.get('entry_date')} | {t.get('entry_minute')} | "
            f"**{t.get('net_pct')}** | {t.get('w20_rv')} | {t.get('w3_rv')} | {t.get('w20_w3_rv_spread')} | "
            f"{t.get('foreign_net_20d')} | {t.get('return_5d_pct')} | {t.get('margin_chg_20d')} |"
        )

    lines.extend(["", "## Win vs Loss · RRG numeric lift", ""])
    lines.append("| field | mean win | mean loss | lift |")
    lines.append("|-------|--------:|---------:|-----:|")
    for row in (fin.get("rrg_numeric_lifts") or prof.get("numeric_lifts") or [])[:12]:
        lines.append(
            f"| {row.get('field')} | {row.get('mean_win')} | {row.get('mean_loss')} | **{row.get('lift')}** |"
        )

    lines.extend(["", "## Win vs Loss · FinMind numeric lift", ""])
    lines.append("| field | mean win | mean loss | lift |")
    lines.append("|-------|--------:|---------:|-----:|")
    for row in (fin.get("finmind_numeric_lifts") or [])[:12]:
        lines.append(
            f"| {row.get('field')} | {row.get('mean_win')} | {row.get('mean_loss')} | **{row.get('lift')}** |"
        )

    lines.extend(["", "## FinMind 数据覆盖率", ""])
    for k, pct in sorted(cov.items(), key=lambda x: -x[1])[:10]:
        lines.append(f"- `{k}`: **{pct}%**")

    lines.extend(["", "## RRG 因子 discriminator（top 12）", ""])
    lines.append("| factor | n+ | mean+ | mean− | lift | Cohen d |")
    lines.append("|--------|---:|------:|------:|-----:|--------:|")
    for row in (payload.get("rrg_discriminators") or [])[:12]:
        lines.append(
            f"| {row.get('label')} | {row.get('n_pos')} | {row.get('mean_ret_pos')} | "
            f"{row.get('mean_ret_neg')} | **{row.get('lift')}** | {row.get('cohen_d')} |"
        )

    lines.extend(["", "## US ES/NQ overnight discriminator（top 8）", ""])
    lines.append("| factor | n+ | mean+ | mean− | lift | Cohen d |")
    lines.append("|--------|---:|------:|------:|-----:|--------:|")
    for row in (payload.get("us_futures_discriminators") or [])[:8]:
        lines.append(
            f"| {row.get('label')} | {row.get('n_pos')} | {row.get('mean_ret_pos')} | "
            f"{row.get('mean_ret_neg')} | **{row.get('lift')}** | {row.get('cohen_d')} |"
        )

    lines.extend(["", "## FinMind 因子 discriminator（top 12）", ""])
    lines.append("| factor | n+ | mean+ | mean− | lift | Cohen d |")
    lines.append("|--------|---:|------:|------:|-----:|--------:|")
    for row in (payload.get("finmind_discriminators") or [])[:12]:
        lines.append(
            f"| {row.get('label')} | {row.get('n_pos')} | {row.get('mean_ret_pos')} | "
            f"{row.get('mean_ret_neg')} | **{row.get('lift')}** | {row.get('cohen_d')} |"
        )

    lines.extend(["", "## ~保留一半 的过滤器（top 10）", ""])
    lines.append("| filter | side | keep% | ret kept | ret drop | lift | win kept |")
    lines.append("|--------|------|------:|---------:|---------:|-----:|---------:|")
    for row in (payload.get("half_filters") or [])[:10]:
        lines.append(
            f"| {row.get('label')} | {row.get('side')} | {row.get('keep_pct')} | "
            f"**{row.get('mean_ret_kept')}** | {row.get('mean_ret_drop')} | {row.get('lift_vs_drop')} | "
            f"{row.get('win_rate_kept')} |"
        )

    combo = payload.get("combo_top2")
    if combo:
        ck = combo.get("kept") or {}
        bl = combo.get("baseline") or {}
        lines.extend(
            [
                "",
                "## Combo top-2 filters (in-sample)",
                "",
                f"- Keep **{combo.get('keep_pct')}%** · n={combo.get('n_kept')} · "
                f"ret **{ck.get('mean_ret_3d_net_pct')}%** vs baseline **{bl.get('mean_ret_3d_net_pct')}%** · "
                f"win **{ck.get('win_rate_pct')}%**",
            ]
        )

    champ = oos.get("train_champion_filter")
    val_apply = oos.get("val_apply")
    if champ:
        lines.extend(
            [
                "",
                "## OOS · 60d train / 30d val",
                "",
                f"- Train champion: **{champ.get('label')}** ({champ.get('side')}) · "
                f"train keep {champ.get('keep_pct')}% · train ret {champ.get('mean_ret_kept')}%",
            ]
        )
    if val_apply:
        vk = val_apply.get("kept") or {}
        vb = val_apply.get("baseline") or {}
        lines.append(
            f"- Val apply (all specs): keep **{val_apply.get('keep_pct')}%** · n={val_apply.get('n_kept')} · "
            f"ret **{vk.get('mean_ret_3d_net_pct')}%** vs val baseline **{vb.get('mean_ret_3d_net_pct')}%** · "
            f"win **{vk.get('win_rate_pct')}%** · "
            f"{'**passed**' if oos.get('val_passed') else 'failed'}"
        )

    us_champ = oos_us.get("train_champion_filter")
    us_val = oos_us.get("val_apply")
    if us_champ:
        lines.extend(
            [
                "",
                "## OOS · RRG + US ES/NQ (PIT)",
                "",
                f"- Train champion: **{us_champ.get('label')}** ({us_champ.get('side')}) · "
                f"train keep {us_champ.get('keep_pct')}% · train ret {us_champ.get('mean_ret_kept')}%",
            ]
        )
    if us_val:
        vk = us_val.get("kept") or {}
        vb = us_val.get("baseline") or {}
        lines.append(
            f"- Val apply: keep **{us_val.get('keep_pct')}%** · n={us_val.get('n_kept')} · "
            f"ret **{vk.get('mean_ret_3d_net_pct')}%** vs val baseline **{vb.get('mean_ret_3d_net_pct')}%** · "
            f"win **{vk.get('win_rate_pct')}%** · "
            f"{'**passed**' if oos_us.get('val_passed') else 'failed'}"
        )

    pit_champ = oos_pit.get("train_champion_filter")
    pit_val = oos_pit.get("val_apply")
    if pit_champ and pit_val and pit_champ.get("id") != (us_champ or {}).get("id"):
        vk = pit_val.get("kept") or {}
        vb = pit_val.get("baseline") or {}
        lines.extend(
            [
                "",
                "## OOS · RRG + US (combined PIT pool)",
                "",
                f"- Train champion: **{pit_champ.get('label')}** · val ret **{vk.get('mean_ret_3d_net_pct')}%** "
                f"vs **{vb.get('mean_ret_3d_net_pct')}%** · "
                f"{'**passed**' if oos_pit.get('val_passed') else 'failed'}",
            ]
        )

    focus_eval = oos_rrg.get("focus_eval") or []
    focus_combo = oos_rrg.get("focus_combo")
    if focus_eval or focus_combo:
        lines.extend(
            [
                "",
                "## OOS · RRG-only 指定 filter（60d train / 30d val · a priori）",
                "",
                "| filter | train keep% | train ret | val keep% | val ret | val baseline | val win | 判定 |",
                "|--------|-----------:|----------:|---------:|--------:|------------:|--------:|:----:|",
            ]
        )

        def _focus_row(fe: dict[str, Any]) -> str:
            tr = fe.get("train") or {}
            vl = fe.get("val") or {}
            tk = tr.get("kept") or {}
            vk = vl.get("kept") or {}
            vb = vl.get("baseline") or {}
            return (
                f"| {fe.get('label')} | {tr.get('keep_pct')} | {tk.get('mean_ret_3d_net_pct')}% | "
                f"{vl.get('keep_pct')} | **{vk.get('mean_ret_3d_net_pct')}%** | {vb.get('mean_ret_3d_net_pct')}% | "
                f"{vk.get('win_rate_pct')}% | {'✅ passed' if fe.get('val_passed') else '❌ failed'} |"
            )

        for fe in focus_eval:
            lines.append(_focus_row(fe))
        if focus_combo:
            lines.append(_focus_row(focus_combo))

    lines.extend(["", "## Verdict", ""])
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(["", "模组：`scripts/run_abc_v3_trade_quality.py` · sync `scripts/tools/sync_us_overnight_futures.py`"])
    return "\n".join(lines)
