#!/usr/bin/env python3
"""富邦新店 · 主契約 A × 大戶市佔指紋 W 搭配研究（Research-only）。

A（主契約，paper_forward_required）:
  ≥50M net · Leading · Top1 · L1H8 · 1槽 · vol≥200k · 60bps

W（大戶市佔指紋；≠ 舊 B 的 buy/(buy+sell)）:
  buy_amount = buy_shares × signal-day close（毛買金額）
  mkt_share  = buy_shares / day_volume（市場成交股數占比；PIT 訊號日）
  day_volume ← stock_daily_bars.volume（source=finmind）

搭配模式（1槽 · 60bps · L1H8）:
  A alone | W alone+RRG | A then W tag | A priority+W fill
  | W quality gate on A | Soft re-rank within A eligibles

  PYTHONPATH=src .venv/bin/python scripts/research/run_xd_whale_mktshare_combo.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from copytrade.branch_signals import (  # noqa: E402
    BRANCH_BUY_ACTION,
    DEFAULT_TRADER_ID_FUBON_XINDIAN,
    _is_listed_equity_id,
)
from copytrade.signals import CopytradeSignal  # noqa: E402
from report_paths import REPORTS_RESEARCH  # noqa: E402
from research.backtest.amount_rrg_copytrade_funnel_study import (  # noqa: E402
    DEFAULT_OOS_CUT,
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
    build_funnel_feature_index,
)
from research.backtest.copytrade_backtest import (  # noqa: E402
    DEFAULT_SIGNAL_CAPITAL_NTD,
    run_copytrade_backtest,
)
from research.backtest.fubon_xindian_filter_study import (  # noqa: E402
    ETF_CODE_PROXY,
    FORWARD_START,
    FilterConfig,
    _readiness_slice,
    build_config_grouped,
    build_raw_xindian_amount_grouped,
)
from research.backtest.yuanta_songjiang_copytrade_sweep import (  # noqa: E402
    day_results_to_dicts,
)
from stock_db import DEFAULT_DB_PATH, connect, load_broker_branch_nets  # noqa: E402
from stock_db import normalize_stock_name  # noqa: E402

OUT_DIR = REPORTS_RESEARCH / "fubon-xindian-copytrade"
FILE_STEM = "xd_whale_mktshare_combo"
HOLD = 8
MIN_N_WARN = 20
MIN_OOS_WARN = 8

RrgMode = Literal["none", "leading", "rs_ratio_gt100"]
SoftMode = Literal["mkt_share", "prior20", "buy_amt_x_mkt"]

CONTRACT_A = FilterConfig(
    metric="amount",
    threshold=50_000_000.0,
    min_avg_volume_shares=200_000.0,
    w20_momentum_min=None,
    rs_ratio_min=None,
    rrg_quadrant="leading",
    top_n=1,
    entry_lag=0,
    hold=HOLD,
    n_slots=1,
)

# Density-aware: raw ≥300M mkt_share p50≈3.8% p90≈9.5%; ≥0.20 nearly empty.
AMOUNT_GRID_NTD = (300e6, 500e6, 800e6)
MKT_SHARE_GRID = (0.05, 0.07, 0.10, 0.15, 0.20, 0.30, 0.40)
RRG_GRID: tuple[RrgMode, ...] = ("none", "leading", "rs_ratio_gt100")
# Focus params for combo modes (avoid combinatorial explosion).
COMBO_AMOUNTS = (300e6, 500e6, 800e6)
COMBO_SHARES = (0.05, 0.07, 0.10, 0.15)
COMBO_RRG: tuple[RrgMode, ...] = ("none", "leading")


def load_tape_mktshare(
    conn,
    *,
    trader_id: str,
    window_start: str,
    window_end: str,
) -> dict[tuple[str, str], dict[str, float]]:
    """(date, stock_id) → buy/sell/net/close/volume/buy_amt/mkt_share."""
    rows = load_broker_branch_nets(
        conn,
        trader_id,
        window_start=window_start,
        window_end=window_end,
        min_net=None,
    )
    bars = {
        (str(r[0]), str(r[1])): (float(r[2]), float(r[3]) if r[3] is not None else None)
        for r in conn.execute(
            """
            SELECT trade_date, stock_id, close, volume
            FROM stock_daily_bars
            WHERE source = 'finmind'
              AND close IS NOT NULL AND close > 0
              AND trade_date >= ? AND trade_date <= ?
            """,
            (window_start, window_end),
        ).fetchall()
    }
    out: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        day = str(r["trade_date"])
        sid = str(r["stock_id"])
        if not _is_listed_equity_id(sid):
            continue
        bar = bars.get((day, sid))
        if bar is None:
            continue
        close, volume = bar
        if volume is None or volume <= 0:
            continue
        buy = float(r["buy"])
        sell = float(r["sell"])
        net = float(r["net"])
        out[(day, sid)] = {
            "buy": buy,
            "sell": sell,
            "net": net,
            "close": close,
            "volume": float(volume),
            "buy_amt": buy * close,
            "net_amt": net * close if net > 0 else 0.0,
            "mkt_share": buy / float(volume),
        }
    return out


def _calendar_from_tape(tape: dict[tuple[str, str], dict[str, float]]) -> list[str]:
    return sorted({d for d, _ in tape.keys()})


def prior_net_lots_20(
    tape: dict[tuple[str, str], dict[str, float]],
    calendar: list[str],
    day: str,
    sid: str,
) -> float:
    if day not in calendar:
        return 0.0
    idx = calendar.index(day)
    prior = calendar[max(0, idx - 20) : idx]
    return sum(
        float(tape[(d, sid)]["net"]) / 1000.0
        for d in prior
        if (d, sid) in tape
    )


def build_w_grouped(
    tape: dict[tuple[str, str], dict[str, float]],
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    min_buy_amt_ntd: float,
    min_mkt_share: float,
    rrg_mode: RrgMode,
    top_n: int = 1,
    min_avg_volume_shares: float = 200_000.0,
    hold: int = HOLD,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
    """W: buy_amt ∩ mkt_share, then RRG, rank by buy_amt Top-N."""
    by_day: dict[str, list[tuple[float, str, CopytradeSignal]]] = defaultdict(list)
    n_raw = n_amt = n_share = n_liq = n_rrg = missing = 0
    for (day, sid), meta in tape.items():
        n_raw += 1
        if float(meta["buy_amt"]) < float(min_buy_amt_ntd):
            continue
        n_amt += 1
        if float(meta["mkt_share"]) < float(min_mkt_share):
            continue
        n_share += 1
        feat = features.get((day, sid))
        if feat is None:
            missing += 1
            continue
        avg_vol = feat.get("avg_volume20")
        if avg_vol is None or float(avg_vol) < float(min_avg_volume_shares):
            continue
        n_liq += 1
        if rrg_mode == "leading":
            if str(feat.get("quadrant") or "").lower() != "leading":
                continue
        elif rrg_mode == "rs_ratio_gt100":
            rs = feat.get("rs_ratio")
            if rs is None or float(rs) < 100.0:
                continue
        n_rrg += 1
        leg = CopytradeSignal(
            signal_date=day,
            stock_id=sid,
            stock_name=normalize_stock_name(sid),
            action=BRANCH_BUY_ACTION,
            share_delta=float(meta["buy"]),
            weight_delta=None,
            weight_pct_curr=None,
        )
        by_day[day].append((float(meta["buy_amt"]), sid, leg))

    out: dict[str, list[CopytradeSignal]] = {}
    for day, rows in by_day.items():
        rows.sort(key=lambda x: (-x[0], x[1]))
        kept = [row[2] for row in rows[: int(top_n)]]
        if kept:
            out[day] = kept
    funnel = {
        "raw_legs": n_raw,
        "buy_amt_pass": n_amt,
        "mkt_share_pass": n_share,
        "liquidity_pass": n_liq,
        "rrg_pass": n_rrg,
        "signal_days": len(out),
        "missing_features": missing,
        "min_buy_amt_ntd": min_buy_amt_ntd,
        "min_mkt_share": min_mkt_share,
        "rrg_mode": rrg_mode,
        "hold": hold,
        "top_n": top_n,
    }
    return out, funnel


def ensure_features_for_tape(
    conn,
    features: dict[tuple[str, str], dict[str, Any]],
    tape: dict[tuple[str, str], dict[str, float]],
    *,
    min_buy_amt_ntd: float,
) -> dict[tuple[str, str], dict[str, Any]]:
    need: dict[str, list[CopytradeSignal]] = defaultdict(list)
    for (day, sid), meta in tape.items():
        if float(meta["buy_amt"]) < float(min_buy_amt_ntd):
            continue
        if (day, sid) in features:
            continue
        need[day].append(
            CopytradeSignal(
                signal_date=day,
                stock_id=sid,
                stock_name=normalize_stock_name(sid),
                action=BRANCH_BUY_ACTION,
                share_delta=float(meta["net"]) if meta["net"] > 0 else float(meta["buy"]),
                weight_delta=None,
                weight_pct_curr=None,
            )
        )
    if need:
        extra = build_funnel_feature_index(conn, need)
        features = {**features, **extra}
    return features


def _arm_summary(arm: dict[str, Any], hold: int = HOLD) -> dict[str, Any]:
    full = arm["metrics"]["full"]
    is_ = arm["metrics"]["is"]
    oos = arm["metrics"]["oos"]
    med = full.get("median_return_pct")
    return {
        "config_id": arm.get("config_id"),
        "cost_bps": arm["cost_bps"],
        "funnel": arm.get("funnel"),
        "n": full.get("n_cycles"),
        "win_rate_pct": full.get("win_rate_pct"),
        "median_pct": full.get("median_return_pct"),
        "mean_pct": full.get("mean_return_pct"),
        "median_per_hold_day_pct": (
            round(float(med) / hold, 4) if med is not None else None
        ),
        "mean_excess_pct": full.get("mean_excess_pct"),
        "max_drawdown_pct": full.get("max_drawdown_pct"),
        "is_n": is_.get("n_cycles"),
        "is_median_pct": is_.get("median_return_pct"),
        "is_win_rate_pct": is_.get("win_rate_pct"),
        "oos_n": oos.get("n_cycles"),
        "oos_median_pct": oos.get("median_return_pct"),
        "oos_win_rate_pct": oos.get("win_rate_pct"),
        "oos_mean_excess_pct": oos.get("mean_excess_pct"),
        "n_warn": int(full.get("n_cycles") or 0) < MIN_N_WARN,
        "oos_n_warn": int(oos.get("n_cycles") or 0) < MIN_OOS_WARN,
    }


def run_prebuilt(
    conn,
    grouped: dict[str, list[CopytradeSignal]],
    *,
    label: str,
    window_start: str,
    window_end: str,
    oos_cut: str,
    hold: int = HOLD,
    cost_bps: float = 60.0,
) -> dict[str, Any]:
    result = run_copytrade_backtest(
        conn,
        ETF_CODE_PROXY,
        strategy_id=f"L1H{hold}",
        strategy_label=label,
        entry_lag_days=0,
        hold_trading_days=hold,
        entry_price_mode="open",
        capital_ntd=DEFAULT_SIGNAL_CAPITAL_NTD,
        cost_bps=cost_bps,
        window_start=window_start,
        window_end=window_end,
        grouped=grouped,
    )
    day_dicts = day_results_to_dicts(result)
    end_exclusive = (date.fromisoformat(window_end) + timedelta(days=1)).isoformat()
    arm = {
        "config_id": label,
        "cost_bps": cost_bps,
        "funnel": {
            "signal_days": len(grouped),
            "topn_legs": sum(len(v) for v in grouped.values()),
            "prebuilt": True,
        },
        "metrics": {
            "full": _readiness_slice(
                conn, day_dicts, start=window_start, end=end_exclusive
            ),
            "is": _readiness_slice(conn, day_dicts, start=window_start, end=oos_cut),
            "oos": _readiness_slice(
                conn, day_dicts, start=oos_cut, end=end_exclusive
            ),
        },
    }
    return {"label": label, "hold": hold, "summary": _arm_summary(arm, hold=hold), "arm": arm}


def intersect_days(
    a: dict[str, list[CopytradeSignal]],
    w: dict[str, list[CopytradeSignal]],
    *,
    same_stock: bool = True,
) -> dict[str, list[CopytradeSignal]]:
    out: dict[str, list[CopytradeSignal]] = {}
    for day, legs in a.items():
        if day not in w:
            continue
        if not same_stock:
            out[day] = legs
            continue
        if str(legs[0].stock_id) == str(w[day][0].stock_id):
            out[day] = legs
    return out


def a_priority_w_fill(
    a: dict[str, list[CopytradeSignal]],
    w: dict[str, list[CopytradeSignal]],
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
    """Same-day conflict → A; else fill with W."""
    out = {day: list(legs) for day, legs in a.items()}
    filled = 0
    for day, legs in w.items():
        if day not in out:
            out[day] = list(legs)
            filled += 1
    stats = {
        "a_days": len(a),
        "w_days": len(w),
        "filled_days": filled,
        "overlap_days": len(set(a) & set(w)),
        "combo_days": len(out),
    }
    return out, stats


def stratify_a_by_w(
    a: dict[str, list[CopytradeSignal]],
    tape: dict[tuple[str, str], dict[str, float]],
    *,
    min_buy_amt_ntd: float,
    min_mkt_share: float,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, list[CopytradeSignal]], dict[str, Any]]:
    pass_: dict[str, list[CopytradeSignal]] = {}
    fail: dict[str, list[CopytradeSignal]] = {}
    for day, legs in a.items():
        sid = str(legs[0].stock_id)
        meta = tape.get((day, sid))
        ok = (
            meta is not None
            and float(meta["buy_amt"]) >= float(min_buy_amt_ntd)
            and float(meta["mkt_share"]) >= float(min_mkt_share)
        )
        if ok:
            pass_[day] = legs
        else:
            fail[day] = legs
    stats = {
        "a_days": len(a),
        "a_and_w": len(pass_),
        "a_not_w": len(fail),
        "w_tag_rate_pct": round(100.0 * len(pass_) / len(a), 2) if a else None,
    }
    return pass_, fail, stats


def soft_rerank_a(
    raw_a: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    tape: dict[tuple[str, str], dict[str, float]],
    calendar: list[str],
    *,
    mode: SoftMode,
    pool_top_n: int = 5,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
    """Within A-eligible (Leading≥50M·liq), re-rank Top1 by soft score."""
    pool_cfg = FilterConfig(
        metric="amount",
        threshold=50_000_000.0,
        min_avg_volume_shares=200_000.0,
        w20_momentum_min=None,
        rs_ratio_min=None,
        rrg_quadrant="leading",
        top_n=int(pool_top_n),
        entry_lag=0,
        hold=HOLD,
        n_slots=1,
    )
    pool, funnel = build_config_grouped(raw_a, features, pool_cfg)
    out: dict[str, list[CopytradeSignal]] = {}
    flipped = 0
    for day, legs in pool.items():
        scored: list[tuple[float, str, CopytradeSignal]] = []
        for leg in legs:
            sid = str(leg.stock_id)
            meta = tape.get((day, sid))
            mkt = float(meta["mkt_share"]) if meta else 0.0
            buy_amt = float(meta["buy_amt"]) if meta else 0.0
            prior = prior_net_lots_20(tape, calendar, day, sid)
            if mode == "mkt_share":
                score = mkt
            elif mode == "prior20":
                score = prior
            else:  # buy_amt_x_mkt
                score = math.log1p(buy_amt / 1e6) * (1.0 + 10.0 * mkt)
            scored.append((score, sid, leg))
        scored.sort(key=lambda x: (-x[0], x[1]))
        pick = scored[0][2]
        out[day] = [pick]
        if str(pick.stock_id) != str(legs[0].stock_id):
            flipped += 1
    return out, {
        **funnel,
        "soft_mode": mode,
        "pool_top_n": pool_top_n,
        "flipped_vs_amount_top1": flipped,
        "flip_rate_pct": round(100.0 * flipped / len(pool), 2) if pool else None,
    }


def mkt_share_density(
    tape: dict[tuple[str, str], dict[str, float]],
    *,
    min_buy_amt_ntd: float = 300e6,
) -> dict[str, Any]:
    shares = [
        float(m["mkt_share"])
        for m in tape.values()
        if float(m["buy_amt"]) >= float(min_buy_amt_ntd)
    ]
    if not shares:
        return {"n": 0}
    shares.sort()

    def _q(p: float) -> float:
        i = int(round((len(shares) - 1) * p))
        return round(shares[i], 6)

    thr_counts = {
        f">={ thr:.2f}": int(sum(1 for s in shares if s >= thr))
        for thr in MKT_SHARE_GRID
    }
    return {
        "n_legs_buy_amt_ge": len(shares),
        "min_buy_amt_ntd": min_buy_amt_ntd,
        "p10": _q(0.10),
        "p25": _q(0.25),
        "p50": _q(0.50),
        "p75": _q(0.75),
        "p90": _q(0.90),
        "p95": _q(0.95),
        "max": round(max(shares), 6),
        "threshold_counts": thr_counts,
        "note": (
            "User-suggested 10–40% is sparse at ≥300M; "
            "primary combo grid uses 5–15%."
        ),
    }


def _delta_vs_a(row: dict[str, Any], a_base: dict[str, Any]) -> dict[str, Any]:
    def _d(k: str) -> float | None:
        a = a_base.get(k)
        b = row.get(k)
        if a is None or b is None:
            return None
        return round(float(b) - float(a), 4)

    return {
        "oos_median_delta_vs_A": _d("oos_median_pct"),
        "median_delta_vs_A": _d("median_pct"),
        "oos_n_delta_vs_A": (
            None
            if a_base.get("oos_n") is None or row.get("oos_n") is None
            else int(row["oos_n"]) - int(a_base["oos_n"])
        ),
        "n_delta_vs_A": (
            None
            if a_base.get("n") is None or row.get("n") is None
            else int(row["n"]) - int(a_base["n"])
        ),
    }


def _rank_key(row: dict[str, Any]) -> tuple[float, float, int]:
    oos_med = row.get("oos_median_pct")
    med = row.get("median_pct")
    n = int(row.get("n") or 0)
    oos_n = int(row.get("oos_n") or 0)
    penalty = 0.0
    if n < MIN_N_WARN:
        penalty -= 100.0
    if oos_n < MIN_OOS_WARN:
        penalty -= 50.0
    return (
        float(oos_med) + penalty if oos_med is not None else -1e9,
        float(med) if med is not None else -1e9,
        n,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="XD A×W whale mkt_share combo study")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=DEFAULT_WINDOW_START)
    ap.add_argument("--end", default=DEFAULT_WINDOW_END)
    ap.add_argument("--oos-cut", default=DEFAULT_OOS_CUT)
    ap.add_argument("--trader-id", default=DEFAULT_TRADER_ID_FUBON_XINDIAN)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    hist_end = min(
        args.end,
        (date.fromisoformat(FORWARD_START) - timedelta(days=1)).isoformat(),
    )
    conn = connect(args.db)
    try:
        print(
            f"window={args.start}..{hist_end} oos_cut={args.oos_cut} "
            f"(FORWARD_START={FORWARD_START}; research observe only)"
        )
        print("loading A mother (≥30M net)…")
        raw_a = build_raw_xindian_amount_grouped(
            conn,
            window_start=args.start,
            window_end=hist_end,
            min_net_amount_ntd=30_000_000.0,
            trader_id=str(args.trader_id),
        )
        features = build_funnel_feature_index(conn, raw_a)
        print(f"A mother_days={len(raw_a)} features={len(features)}")

        print("loading tape + day_volume (mkt_share)…")
        tape = load_tape_mktshare(
            conn,
            trader_id=str(args.trader_id),
            window_start=args.start,
            window_end=hist_end,
        )
        calendar = _calendar_from_tape(tape)
        density = mkt_share_density(tape, min_buy_amt_ntd=300e6)
        print(f"tape keys={len(tape)} density={density}")
        features = ensure_features_for_tape(
            conn, features, tape, min_buy_amt_ntd=min(AMOUNT_GRID_NTD)
        )
        print(f"features after W fill={len(features)}")

        a_grouped, a_funnel = build_config_grouped(raw_a, features, CONTRACT_A)
        print(f"A alone days={len(a_grouped)}")

        # --- Mode H2H with default W (500M · mkt≥0.07 · Leading) ---
        # 0.07 ≈ p75–p90 neighborhood with usable n; 0.10 reserved for sparse arm.
        w_default_amt, w_default_share, w_default_rrg = 500e6, 0.07, "leading"
        w_lead, w_lead_f = build_w_grouped(
            tape,
            features,
            min_buy_amt_ntd=w_default_amt,
            min_mkt_share=w_default_share,
            rrg_mode="leading",
        )
        w_none, w_none_f = build_w_grouped(
            tape,
            features,
            min_buy_amt_ntd=w_default_amt,
            min_mkt_share=w_default_share,
            rrg_mode="none",
        )
        a_cap_w = intersect_days(a_grouped, w_none, same_stock=True)
        a_fill_w, fill_stats = a_priority_w_fill(a_grouped, w_lead)
        a_tag_pass, a_tag_fail, tag_stats = stratify_a_by_w(
            a_grouped,
            tape,
            min_buy_amt_ntd=w_default_amt,
            min_mkt_share=w_default_share,
        )
        soft_mkt, soft_mkt_f = soft_rerank_a(
            raw_a, features, tape, calendar, mode="mkt_share"
        )
        soft_prior, soft_prior_f = soft_rerank_a(
            raw_a, features, tape, calendar, mode="prior20"
        )
        soft_combo, soft_combo_f = soft_rerank_a(
            raw_a, features, tape, calendar, mode="buy_amt_x_mkt"
        )

        mode_specs: list[tuple[str, dict[str, list[CopytradeSignal]], dict[str, Any]]] = [
            ("A_alone_50M_Leading", a_grouped, a_funnel),
            ("W_alone_500M_ms07_noRRG", w_none, w_none_f),
            ("W_alone_500M_ms07_Leading", w_lead, w_lead_f),
            (
                "A_priority_W_fill_500M_ms07_Leading",
                a_fill_w,
                {"signal_days": len(a_fill_w), **fill_stats},
            ),
            (
                "W_gate_on_A_same_stock_500M_ms07",
                a_cap_w,
                {"signal_days": len(a_cap_w), "a_days": len(a_grouped)},
            ),
            (
                "A_tag_W_pass_500M_ms07",
                a_tag_pass,
                {"signal_days": len(a_tag_pass), **tag_stats},
            ),
            (
                "A_tag_W_fail_500M_ms07",
                a_tag_fail,
                {"signal_days": len(a_tag_fail), **tag_stats},
            ),
            ("Soft_A_rerank_mkt_share", soft_mkt, soft_mkt_f),
            ("Soft_A_rerank_prior20", soft_prior, soft_prior_f),
            ("Soft_A_rerank_buy_amt_x_mkt", soft_combo, soft_combo_f),
        ]

        print("mode head-to-head…")
        mode_rows: list[dict[str, Any]] = []
        a_base_60: dict[str, Any] | None = None
        for label, grouped, funnel in mode_specs:
            print(f"  {label} days={len(grouped)}…")
            got = run_prebuilt(
                conn,
                grouped,
                label=label,
                window_start=args.start,
                window_end=hist_end,
                oos_cut=args.oos_cut,
                hold=HOLD,
                cost_bps=60.0,
            )
            s = got["summary"]
            s["funnel"] = funnel
            s["mode"] = label
            if label == "A_alone_50M_Leading":
                a_base_60 = s
            mode_rows.append(s)

        assert a_base_60 is not None
        for s in mode_rows:
            s["vs_A"] = _delta_vs_a(s, a_base_60)

        # --- W alone grid ---
        print("W alone grid…")
        w_grid: list[dict[str, Any]] = []
        for amt in AMOUNT_GRID_NTD:
            for share in MKT_SHARE_GRID:
                for rrg in RRG_GRID:
                    grouped, funnel = build_w_grouped(
                        tape,
                        features,
                        min_buy_amt_ntd=float(amt),
                        min_mkt_share=float(share),
                        rrg_mode=rrg,
                    )
                    if not grouped:
                        w_grid.append(
                            {
                                "amount_m": amt / 1e6,
                                "mkt_share_min": share,
                                "rrg_mode": rrg,
                                "n": 0,
                                "oos_n": 0,
                                "funnel": funnel,
                                "empty": True,
                            }
                        )
                        continue
                    label = f"W_{amt/1e6:g}M_ms{share:g}_{rrg}"
                    got = run_prebuilt(
                        conn,
                        grouped,
                        label=label,
                        window_start=args.start,
                        window_end=hist_end,
                        oos_cut=args.oos_cut,
                        hold=HOLD,
                        cost_bps=60.0,
                    )
                    s = got["summary"]
                    s["funnel"] = funnel
                    s["amount_m"] = amt / 1e6
                    s["mkt_share_min"] = share
                    s["rrg_mode"] = rrg
                    s["vs_A"] = _delta_vs_a(s, a_base_60)
                    w_grid.append(s)
                    print(
                        f"  {label}: n={s.get('n')} med={s.get('median_pct')} "
                        f"oos_n={s.get('oos_n')} oos_med={s.get('oos_median_pct')}"
                    )

        # --- Combo grids: fill / gate / tag ---
        print("A-priority+W-fill grid…")
        fill_grid: list[dict[str, Any]] = []
        gate_grid: list[dict[str, Any]] = []
        tag_grid: list[dict[str, Any]] = []
        for amt in COMBO_AMOUNTS:
            for share in COMBO_SHARES:
                for rrg in COMBO_RRG:
                    w_g, w_f = build_w_grouped(
                        tape,
                        features,
                        min_buy_amt_ntd=float(amt),
                        min_mkt_share=float(share),
                        rrg_mode=rrg,
                    )
                    # fill
                    filled, st = a_priority_w_fill(a_grouped, w_g)
                    got = run_prebuilt(
                        conn,
                        filled,
                        label=f"fill_{amt/1e6:g}M_ms{share:g}_{rrg}",
                        window_start=args.start,
                        window_end=hist_end,
                        oos_cut=args.oos_cut,
                        cost_bps=60.0,
                    )
                    s = got["summary"]
                    s.update(
                        {
                            "amount_m": amt / 1e6,
                            "mkt_share_min": share,
                            "rrg_mode": rrg,
                            "funnel": {**w_f, **st},
                            "vs_A": _delta_vs_a(got["summary"], a_base_60),
                        }
                    )
                    fill_grid.append(s)

                    # gate (same stock)
                    gated = intersect_days(a_grouped, w_g, same_stock=True)
                    got_g = run_prebuilt(
                        conn,
                        gated,
                        label=f"gate_{amt/1e6:g}M_ms{share:g}_{rrg}",
                        window_start=args.start,
                        window_end=hist_end,
                        oos_cut=args.oos_cut,
                        cost_bps=60.0,
                    )
                    sg = got_g["summary"]
                    sg.update(
                        {
                            "amount_m": amt / 1e6,
                            "mkt_share_min": share,
                            "rrg_mode": rrg,
                            "funnel": {
                                **w_f,
                                "gate_days": len(gated),
                                "a_days": len(a_grouped),
                            },
                            "vs_A": _delta_vs_a(got_g["summary"], a_base_60),
                        }
                    )
                    gate_grid.append(sg)

                # tag (W threshold on A's pick; RRG of W not required — tag is on A stock)
                pass_g, fail_g, st_tag = stratify_a_by_w(
                    a_grouped,
                    tape,
                    min_buy_amt_ntd=float(amt),
                    min_mkt_share=float(share),
                )
                for tag, g in (("pass", pass_g), ("fail", fail_g)):
                    got_t = run_prebuilt(
                        conn,
                        g,
                        label=f"tag_{tag}_{amt/1e6:g}M_ms{share:g}",
                        window_start=args.start,
                        window_end=hist_end,
                        oos_cut=args.oos_cut,
                        cost_bps=60.0,
                    )
                    stg = got_t["summary"]
                    stg.update(
                        {
                            "amount_m": amt / 1e6,
                            "mkt_share_min": share,
                            "strat": tag,
                            "strat_stats": st_tag,
                            "vs_A": _delta_vs_a(got_t["summary"], a_base_60),
                        }
                    )
                    tag_grid.append(stg)

        w_sorted = sorted(
            [r for r in w_grid if not r.get("empty")], key=_rank_key, reverse=True
        )
        fill_sorted = sorted(fill_grid, key=_rank_key, reverse=True)
        gate_sorted = sorted(gate_grid, key=_rank_key, reverse=True)

        # Best practical combo: prefer fill with oos_n ok and positive oos delta
        def _adoptable(row: dict[str, Any]) -> bool:
            return (
                not row.get("n_warn")
                and not row.get("oos_n_warn")
                and (row.get("vs_A") or {}).get("oos_median_delta_vs_A") is not None
                and float((row["vs_A"]["oos_median_delta_vs_A"])) > 0
            )

        fill_adoptable = [r for r in fill_sorted if _adoptable(r)]
        gate_adoptable = [r for r in gate_sorted if _adoptable(r)]
        w_adoptable = [r for r in w_sorted if _adoptable(r)]

        # Verdict helpers
        best_fill = fill_adoptable[0] if fill_adoptable else (
            fill_sorted[0] if fill_sorted else None
        )
        best_gate = gate_adoptable[0] if gate_adoptable else (
            gate_sorted[0] if gate_sorted else None
        )
        best_w = w_adoptable[0] if w_adoptable else (
            w_sorted[0] if w_sorted else None
        )

        # Tag quality: pass vs fail at each threshold (observe only)
        tag_contrast: list[dict[str, Any]] = []
        for amt in COMBO_AMOUNTS:
            for share in COMBO_SHARES:
                pass_row = next(
                    (
                        r
                        for r in tag_grid
                        if r.get("strat") == "pass"
                        and r.get("amount_m") == amt / 1e6
                        and r.get("mkt_share_min") == share
                    ),
                    None,
                )
                fail_row = next(
                    (
                        r
                        for r in tag_grid
                        if r.get("strat") == "fail"
                        and r.get("amount_m") == amt / 1e6
                        and r.get("mkt_share_min") == share
                    ),
                    None,
                )
                if not pass_row or not fail_row:
                    continue
                tag_contrast.append(
                    {
                        "amount_m": amt / 1e6,
                        "mkt_share_min": share,
                        "pass_n": pass_row.get("n"),
                        "pass_med": pass_row.get("median_pct"),
                        "pass_oos_n": pass_row.get("oos_n"),
                        "pass_oos_med": pass_row.get("oos_median_pct"),
                        "fail_n": fail_row.get("n"),
                        "fail_med": fail_row.get("median_pct"),
                        "fail_oos_n": fail_row.get("oos_n"),
                        "fail_oos_med": fail_row.get("oos_median_pct"),
                        "pass_minus_fail_oos_med": (
                            None
                            if pass_row.get("oos_median_pct") is None
                            or fail_row.get("oos_median_pct") is None
                            else round(
                                float(pass_row["oos_median_pct"])
                                - float(fail_row["oos_median_pct"]),
                                4,
                            )
                        ),
                        "tag_rate_pct": (pass_row.get("strat_stats") or {}).get(
                            "w_tag_rate_pct"
                        ),
                        "n_warn": pass_row.get("n_warn") or fail_row.get("n_warn"),
                    }
                )

        payload = {
            "schema": "xd_whale_mktshare_combo-v1",
            "generated_at": date.today().isoformat(),
            "status": "research_observe_only",
            "contract_frozen": {
                "branch": "9661 富邦-新店",
                "spec": "≥50M · Leading · Top1 · L1H8 · 1槽 · vol≥200k",
                "decision": "paper_forward_required",
                "forward_start": FORWARD_START,
                "hypothesis": "H-XD-50M-LEADING-L1H8-ADOPTION",
                "note": (
                    "Do not adopt W / fill / gate into Strategy/Order; "
                    "A stays paper_forward_required."
                ),
            },
            "window": {
                "start": args.start,
                "historical_end": hist_end,
                "oos_cut": args.oos_cut,
                "end_arg": args.end,
            },
            "w_definition": {
                "buy_amount": "buy_shares × signal-day close (NTD, gross)",
                "mkt_share": "buy_shares / day_volume",
                "day_volume_source": {
                    "table": "stock_daily_bars",
                    "column": "volume",
                    "source_filter": "finmind",
                    "units": "shares (INTEGER); same unit as branch buy",
                    "pit": "signal day T bar only (date ≤ T for features; volume same-day)",
                },
                "not": "buy/(buy+sell) branch-internal share (failed prior B study)",
                "vs_skilled_hand_prior20": (
                    "Soft arm reuses prior20 net-lots score within A eligibles; "
                    "hard W filter is independent dominance (market share)."
                ),
            },
            "mkt_share_density_ge_300M": density,
            "default_w_probe": {
                "buy_amount_ntd": w_default_amt,
                "mkt_share_min": w_default_share,
                "rrg": w_default_rrg,
                "rationale": "≈p75–p90 of ≥300M legs; denser than user 10–40%",
            },
            "mode_head_to_head_60bps_H8": mode_rows,
            "w_alone_grid_60bps_H8": w_grid,
            "w_alone_top": w_sorted[:8],
            "fill_grid_60bps_H8": fill_grid,
            "fill_top": fill_sorted[:8],
            "gate_grid_60bps_H8": gate_grid,
            "gate_top": gate_sorted[:8],
            "tag_grid_60bps_H8": tag_grid,
            "tag_pass_vs_fail": tag_contrast,
            "adoptable_candidates": {
                "fill": fill_adoptable[:5],
                "gate": gate_adoptable[:5],
                "w_alone": w_adoptable[:5],
                "criteria": (
                    f"n≥{MIN_N_WARN}, oos_n≥{MIN_OOS_WARN}, "
                    "oos_median_delta_vs_A > 0"
                ),
            },
            "best_by_mode": {
                "fill": best_fill,
                "gate": best_gate,
                "w_alone": best_w,
                "soft_from_h2h": [
                    r
                    for r in mode_rows
                    if str(r.get("mode", "")).startswith("Soft_")
                ],
            },
            "warnings": {
                "min_n": MIN_N_WARN,
                "min_oos_n": MIN_OOS_WARN,
                "sparse_mkt_share": (
                    "mkt_share≥0.15 at ≥300M is n≤2 legs; ≥0.30 empty. "
                    "Do not adopt high thresholds."
                ),
                "gate_small_n": "A∩W same-stock often collapses n; treat as tag not live gate.",
                "fill_caution": (
                    "A-priority+W-fill mixes generators; only claim edge if "
                    "OOS median rises with adequate oos_n."
                ),
            },
            "verdict": {
                "adoptable": bool(
                    fill_adoptable or gate_adoptable or w_adoptable
                ),
                "best_usage": (
                    "fill"
                    if fill_adoptable
                    else (
                        "w_alone"
                        if w_adoptable
                        else ("gate" if gate_adoptable else "none_vs_A_alone")
                    )
                ),
                "a_baseline_60bps": {
                    k: a_base_60.get(k)
                    for k in (
                        "n",
                        "median_pct",
                        "oos_n",
                        "oos_median_pct",
                        "win_rate_pct",
                        "max_drawdown_pct",
                        "median_per_hold_day_pct",
                    )
                },
                "mode_findings": {
                    "A_alone": "baseline; compare all arms here",
                    "W_alone_RRG": "often IS-pretty / OOS-thin; check adoptable_candidates.w_alone",
                    "A_then_W_tag": "compare tag_pass_vs_fail; small-n pass arm is observational only",
                    "A_priority_W_fill": "1-slot may absorb fills; inspect n_delta_vs_A",
                    "W_gate_on_A": "hard intersect usually n-starved",
                    "Soft_rerank": "see Soft_* rows in mode_head_to_head",
                },
                "practical_rules_zh": [
                    "主契約 A 不動（paper_forward_required）",
                    "高 mkt_share 僅作觀察標籤，不作硬閘／加碼，除非 forward 否證翻轉",
                    "1槽下勿預期 W 補缺能增厚 OOS；要測補缺需空檔或 ≥2 槽設計",
                ],
            },
        }

        print("\n=== Mode H2H (60bps · L1H8) vs A ===")
        for s in mode_rows:
            d = s.get("vs_A") or {}
            print(
                f"  {str(s.get('mode')):42s} n={s.get('n')} "
                f"win={s.get('win_rate_pct')} med={s.get('median_pct')} "
                f"med/H={s.get('median_per_hold_day_pct')} "
                f"MaxDD={s.get('max_drawdown_pct')} "
                f"IS_n={s.get('is_n')} IS_med={s.get('is_median_pct')} "
                f"OOS_n={s.get('oos_n')} OOS_med={s.get('oos_median_pct')} "
                f"ΔOOS={d.get('oos_median_delta_vs_A')}"
                f"{' ⚠n' if s.get('n_warn') else ''}"
                f"{' ⚠oos' if s.get('oos_n_warn') else ''}"
            )

        print("\n=== Fill top5 ===")
        for s in fill_sorted[:5]:
            d = s.get("vs_A") or {}
            print(
                f"  fill {s.get('amount_m')}M ms≥{s.get('mkt_share_min')} "
                f"{s.get('rrg_mode')}: n={s.get('n')} "
                f"oos_n={s.get('oos_n')} oos_med={s.get('oos_median_pct')} "
                f"ΔOOS={d.get('oos_median_delta_vs_A')} "
                f"filled={(s.get('funnel') or {}).get('filled_days')}"
            )

        print("\n=== Gate top5 ===")
        for s in gate_sorted[:5]:
            d = s.get("vs_A") or {}
            print(
                f"  gate {s.get('amount_m')}M ms≥{s.get('mkt_share_min')} "
                f"{s.get('rrg_mode')}: n={s.get('n')} "
                f"oos_n={s.get('oos_n')} oos_med={s.get('oos_median_pct')} "
                f"ΔOOS={d.get('oos_median_delta_vs_A')}"
            )

        print("\n=== Tag pass−fail OOS (selected) ===")
        for row in tag_contrast:
            if row["mkt_share_min"] in (0.05, 0.07, 0.10) and row["amount_m"] in (
                300.0,
                500.0,
            ):
                print(
                    f"  {row['amount_m']}M ms≥{row['mkt_share_min']}: "
                    f"pass n={row['pass_n']} oos_med={row['pass_oos_med']} | "
                    f"fail n={row['fail_n']} oos_med={row['fail_oos_med']} | "
                    f"Δ={row['pass_minus_fail_oos_med']} "
                    f"tag%={row['tag_rate_pct']}"
                )

        if not args.no_write:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = date.today().strftime("%Y%m%d")
            path = OUT_DIR / f"{stamp}_{FILE_STEM}.json"
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"wrote {path}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
