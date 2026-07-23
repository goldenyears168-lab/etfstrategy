#!/usr/bin/env python3
"""富邦新店 · 主契約 A × 大戶足跡 B 搭配研究（Research-only）。

A（主契約，paper_forward_required）:
  ≥50M net · Leading · Top1 · L1H8 · 1槽 · vol≥200k · 60bps 心智

B（大戶足跡假說）:
  buy_amount ≥ thr · buy_share ≥ min · Top1
  buy_amount = buy_shares × signal-day close（毛買金額，非淨買）
  buy_share  = buy / (buy + sell)（分點當日買方佔比；≠ 市佔）

與 skilled-hand prior20／clean_buy 異同：
  - clean_buy ≈ 1 − min(1, sell/buy)；buy_share = buy/(buy+sell)
    ⇔ sell/buy ≤ (1−s)/s（例如 s=0.4 → sell_ratio≤1.5）
  - 本腳本用硬門檻篩日，非冠軍日內 soft re-rank
  - 不做帳戶 ID 還原；分點合計 ≠ 單一交易者

  PYTHONPATH=src .venv/bin/python scripts/research/run_xd_whale_footprint_combo.py
"""

from __future__ import annotations

import argparse
import json
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
FILE_STEM = "xd_whale_footprint_combo"
HOLD = 8
MIN_N_WARN = 20
MIN_OOS_WARN = 8

RrgMode = Literal["none", "leading", "rs_ratio_gt100"]

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

AMOUNT_GRID_NTD = (300e6, 500e6, 800e6, 1000e6)
BUY_SHARE_GRID = (0.30, 0.40, 0.50)
RRG_GRID: tuple[RrgMode, ...] = ("none", "leading", "rs_ratio_gt100")


def _buy_share(buy: float, sell: float) -> float | None:
    denom = float(buy) + float(sell)
    if denom <= 0:
        return None
    return float(buy) / denom


def load_tape_meta(
    conn,
    *,
    trader_id: str,
    window_start: str,
    window_end: str,
) -> dict[tuple[str, str], dict[str, float]]:
    """(date, stock_id) → buy/sell/net/close/buy_amt/net_amt/buy_share."""
    rows = load_broker_branch_nets(
        conn,
        trader_id,
        window_start=window_start,
        window_end=window_end,
        min_net=None,
    )
    closes = {
        (str(r[0]), str(r[1])): float(r[2])
        for r in conn.execute(
            """
            SELECT trade_date, stock_id, close
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
        close = closes.get((day, sid))
        if close is None:
            continue
        buy = float(r["buy"])
        sell = float(r["sell"])
        net = float(r["net"])
        bs = _buy_share(buy, sell)
        out[(day, sid)] = {
            "buy": buy,
            "sell": sell,
            "net": net,
            "close": close,
            "buy_amt": buy * close,
            "net_amt": net * close if net > 0 else 0.0,
            "buy_share": float(bs) if bs is not None else float("nan"),
        }
    return out


def build_b_grouped(
    tape: dict[tuple[str, str], dict[str, float]],
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    min_buy_amt_ntd: float,
    min_buy_share: float,
    rrg_mode: RrgMode,
    top_n: int = 1,
    min_avg_volume_shares: float = 200_000.0,
    hold: int = HOLD,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
    """B signal: buy_amt ∩ buy_share, then RRG gate, rank by buy_amt Top-N."""
    by_day: dict[str, list[tuple[float, str, CopytradeSignal, dict[str, float]]]] = (
        defaultdict(list)
    )
    n_raw = 0
    n_amt = 0
    n_share = 0
    n_liq = 0
    n_rrg = 0
    missing = 0
    for (day, sid), meta in tape.items():
        n_raw += 1
        if float(meta["buy_amt"]) < float(min_buy_amt_ntd):
            continue
        n_amt += 1
        bs = meta["buy_share"]
        if bs != bs or float(bs) < float(min_buy_share):  # NaN check
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
        # Use gross buy shares so prebuilt backtest (threshold=0 amount score)
        # does not drop days where buy_share≥floor but net≤0.
        leg = CopytradeSignal(
            signal_date=day,
            stock_id=sid,
            stock_name=normalize_stock_name(sid),
            action=BRANCH_BUY_ACTION,
            share_delta=float(meta["buy"]),
            weight_delta=None,
            weight_pct_curr=None,
        )
        by_day[day].append((float(meta["buy_amt"]), sid, leg, meta))

    out: dict[str, list[CopytradeSignal]] = {}
    for day, rows in by_day.items():
        rows.sort(key=lambda x: (-x[0], x[1]))
        kept = [row[2] for row in rows[: int(top_n)]]
        if kept:
            out[day] = kept
    funnel = {
        "raw_legs": n_raw,
        "buy_amt_pass": n_amt,
        "buy_share_pass": n_share,
        "liquidity_pass": n_liq,
        "rrg_pass": n_rrg,
        "signal_days": len(out),
        "missing_features": missing,
        "min_buy_amt_ntd": min_buy_amt_ntd,
        "min_buy_share": min_buy_share,
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
    """Fill RRG/vol features for B candidates missing from A mother."""
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
    features: dict[tuple[str, str], dict[str, Any]],
    *,
    label: str,
    window_start: str,
    window_end: str,
    oos_cut: str,
    hold: int = HOLD,
    cost_bps: float = 60.0,
) -> dict[str, Any]:
    """Backtest a pre-filtered day→legs map (no second gate pass)."""
    del features  # features already applied upstream
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
    summary = _arm_summary(arm, hold=hold)
    return {"label": label, "hold": hold, "summary": summary, "arm": arm}


def intersect_days(
    a: dict[str, list[CopytradeSignal]],
    b: dict[str, list[CopytradeSignal]],
    *,
    same_stock: bool = True,
) -> dict[str, list[CopytradeSignal]]:
    out: dict[str, list[CopytradeSignal]] = {}
    for day, legs in a.items():
        if day not in b:
            continue
        if not same_stock:
            out[day] = legs
            continue
        a_sid = str(legs[0].stock_id)
        b_sid = str(b[day][0].stock_id)
        if a_sid == b_sid:
            out[day] = legs
    return out


def union_days(
    a: dict[str, list[CopytradeSignal]],
    b: dict[str, list[CopytradeSignal]],
) -> dict[str, list[CopytradeSignal]]:
    """Prefer A pick on overlap; else B. (union is exploratory — slot contention)."""
    out = {day: list(legs) for day, legs in a.items()}
    for day, legs in b.items():
        if day not in out:
            out[day] = list(legs)
    return out


def stratify_a_by_b(
    a: dict[str, list[CopytradeSignal]],
    tape: dict[tuple[str, str], dict[str, float]],
    *,
    min_buy_amt_ntd: float,
    min_buy_share: float,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, list[CopytradeSignal]], dict[str, Any]]:
    pass_: dict[str, list[CopytradeSignal]] = {}
    fail: dict[str, list[CopytradeSignal]] = {}
    for day, legs in a.items():
        sid = str(legs[0].stock_id)
        meta = tape.get((day, sid))
        ok = False
        if meta is not None:
            bs = meta["buy_share"]
            ok = (
                float(meta["buy_amt"]) >= float(min_buy_amt_ntd)
                and bs == bs
                and float(bs) >= float(min_buy_share)
            )
        if ok:
            pass_[day] = legs
        else:
            fail[day] = legs
    stats = {
        "a_days": len(a),
        "a_and_b": len(pass_),
        "a_not_b": len(fail),
        "b_pass_rate_pct": (
            round(100.0 * len(pass_) / len(a), 2) if a else None
        ),
    }
    return pass_, fail, stats


def main() -> int:
    ap = argparse.ArgumentParser(description="XD A×B whale footprint combo study")
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

        print("loading tape meta (buy/sell)…")
        tape = load_tape_meta(
            conn,
            trader_id=str(args.trader_id),
            window_start=args.start,
            window_end=hist_end,
        )
        print(f"tape keys={len(tape)}")
        features = ensure_features_for_tape(
            conn, features, tape, min_buy_amt_ntd=min(AMOUNT_GRID_NTD)
        )
        print(f"features after B fill={len(features)}")

        # --- Head-to-head ---
        a_grouped, a_funnel = build_config_grouped(raw_a, features, CONTRACT_A)
        b_none, b_none_funnel = build_b_grouped(
            tape,
            features,
            min_buy_amt_ntd=500e6,
            min_buy_share=0.40,
            rrg_mode="none",
        )
        b_lead, b_lead_funnel = build_b_grouped(
            tape,
            features,
            min_buy_amt_ntd=500e6,
            min_buy_share=0.40,
            rrg_mode="leading",
        )
        a_cap_b = intersect_days(a_grouped, b_none, same_stock=True)
        a_cup_b = union_days(a_grouped, b_none)
        a_in_b, a_out_b, strat_stats = stratify_a_by_b(
            a_grouped, tape, min_buy_amt_ntd=500e6, min_buy_share=0.40
        )

        head: dict[str, Any] = {"definitions": {
            "buy_amount_ntd": "branch buy_shares × signal-day close (gross)",
            "buy_share": "branch buy / (buy + sell); NOT market volume share",
            "net_amount_ntd_A": "branch net_shares × close (existing amount SSOT)",
            "relation_to_skilled_hand": (
                "buy_share mirrors clean_buy/sell_ratio family; "
                "hard day filter ≠ prior20 soft re-rank on champ days"
            ),
            "min_n_warn": MIN_N_WARN,
            "min_oos_n_warn": MIN_OOS_WARN,
        }}
        arms_spec = [
            ("A_alone_50M_Leading", a_grouped, a_funnel),
            ("B_alone_500M_bs40_noRRG", b_none, b_none_funnel),
            ("B_alone_500M_bs40_Leading", b_lead, b_lead_funnel),
            ("A_intersect_B_same_stock", a_cap_b, {"signal_days": len(a_cap_b)}),
            ("A_union_B_prefer_A", a_cup_b, {"signal_days": len(a_cup_b), "caution": True}),
            ("A_strat_B_pass", a_in_b, {"signal_days": len(a_in_b), **strat_stats}),
            ("A_strat_B_fail", a_out_b, {"signal_days": len(a_out_b), **strat_stats}),
        ]
        head["stratify_stats"] = strat_stats
        head["overlap_days_A_cap_B"] = sorted(a_cap_b.keys())
        head_rows: list[dict[str, Any]] = []
        for label, grouped, funnel in arms_spec:
            print(f"  H2H {label} days={len(grouped)}…")
            row_costs: dict[str, Any] = {}
            for cost_bps in (0.0, 60.0):
                got = run_prebuilt(
                    conn,
                    grouped,
                    features,
                    label=label,
                    window_start=args.start,
                    window_end=hist_end,
                    oos_cut=args.oos_cut,
                    hold=HOLD,
                    cost_bps=cost_bps,
                )
                got["summary"]["funnel"] = funnel
                row_costs[f"{cost_bps:g}bps"] = got["summary"]
            head_rows.append({"label": label, "funnel": funnel, "costs": row_costs})
        head["arms"] = head_rows

        # --- Small grid on B footprint dims ---
        print("grid sweep…")
        grid_rows: list[dict[str, Any]] = []
        for amt in AMOUNT_GRID_NTD:
            for share in BUY_SHARE_GRID:
                for rrg in RRG_GRID:
                    grouped, funnel = build_b_grouped(
                        tape,
                        features,
                        min_buy_amt_ntd=float(amt),
                        min_buy_share=float(share),
                        rrg_mode=rrg,
                        hold=HOLD,
                    )
                    label = f"B_{amt/1e6:g}M_bs{share:g}_{rrg}_H{HOLD}"
                    got = run_prebuilt(
                        conn,
                        grouped,
                        features,
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
                    s["buy_share_min"] = share
                    s["rrg_mode"] = rrg
                    grid_rows.append(s)
                    print(
                        f"  {label}: n={s.get('n')} med={s.get('median_pct')} "
                        f"oos_n={s.get('oos_n')} oos_med={s.get('oos_median_pct')}"
                    )

        # Intersection grid: A ∩ B(params) same-stock, 60bps H8
        print("A∩B grid…")
        inter_rows: list[dict[str, Any]] = []
        for amt in AMOUNT_GRID_NTD:
            for share in BUY_SHARE_GRID:
                for rrg in ("none", "leading"):
                    b_g, b_f = build_b_grouped(
                        tape,
                        features,
                        min_buy_amt_ntd=float(amt),
                        min_buy_share=float(share),
                        rrg_mode=rrg,  # type: ignore[arg-type]
                    )
                    inter = intersect_days(a_grouped, b_g, same_stock=True)
                    label = f"A∩B_{amt/1e6:g}M_bs{share:g}_{rrg}"
                    got = run_prebuilt(
                        conn,
                        inter,
                        features,
                        label=label,
                        window_start=args.start,
                        window_end=hist_end,
                        oos_cut=args.oos_cut,
                        hold=HOLD,
                        cost_bps=60.0,
                    )
                    s = got["summary"]
                    s["funnel"] = {
                        **b_f,
                        "intersect_days": len(inter),
                        "a_days": len(a_grouped),
                    }
                    s["amount_m"] = amt / 1e6
                    s["buy_share_min"] = share
                    s["rrg_mode"] = rrg
                    inter_rows.append(s)

        # A stratify by B params (only default + best-looking neighbors)
        print("A stratify grid…")
        strat_rows: list[dict[str, Any]] = []
        for amt in (300e6, 500e6, 800e6):
            for share in (0.30, 0.40, 0.50):
                pass_g, fail_g, st = stratify_a_by_b(
                    a_grouped, tape, min_buy_amt_ntd=amt, min_buy_share=share
                )
                for tag, g in (("pass", pass_g), ("fail", fail_g)):
                    label = f"A_strat_{tag}_{amt/1e6:g}M_bs{share:g}"
                    got = run_prebuilt(
                        conn,
                        g,
                        features,
                        label=label,
                        window_start=args.start,
                        window_end=hist_end,
                        oos_cut=args.oos_cut,
                        hold=HOLD,
                        cost_bps=60.0,
                    )
                    s = got["summary"]
                    s["amount_m"] = amt / 1e6
                    s["buy_share_min"] = share
                    s["strat"] = tag
                    s["strat_stats"] = st
                    strat_rows.append(s)

        # H10 one-line contrast for A and default B
        print("H10 contrast…")
        h10_rows: list[dict[str, Any]] = []
        for label, grouped, hold in (
            ("A_50M_Leading_H10", a_grouped, 10),
            ("B_500M_bs40_none_H10", b_none, 10),
            ("B_500M_bs40_Leading_H10", b_lead, 10),
        ):
            got = run_prebuilt(
                conn,
                grouped,
                features,
                label=label,
                window_start=args.start,
                window_end=hist_end,
                oos_cut=args.oos_cut,
                hold=hold,
                cost_bps=60.0,
            )
            h10_rows.append(got["summary"])

        def _rank_key(row: dict[str, Any]) -> tuple[float, float, int]:
            # Prefer OOS median, then full median, then n (discourage tiny-n luck).
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

        grid_sorted = sorted(grid_rows, key=_rank_key, reverse=True)
        inter_sorted = sorted(inter_rows, key=_rank_key, reverse=True)

        payload = {
            "schema": "xd_whale_footprint_combo-v1",
            "generated_at": date.today().isoformat(),
            "status": "research_observe_only",
            "contract_frozen": {
                "branch": "9661 富邦-新店",
                "spec": "≥50M · Leading · Top1 · L1H8 · 1槽 · vol≥200k",
                "decision": "paper_forward_required",
                "forward_start": FORWARD_START,
                "hypothesis": "H-XD-50M-LEADING-L1H8-ADOPTION",
                "note": "Do not adopt B or A∩B into Strategy/Order from this study.",
            },
            "window": {
                "start": args.start,
                "historical_end": hist_end,
                "oos_cut": args.oos_cut,
                "end_arg": args.end,
            },
            "buy_share_definition": {
                "formula": "buy_share = buy / (buy + sell)",
                "units": "shares from stock_broker_branch_daily",
                "buy_amount": "buy × signal-day close (NTD)",
                "not": "buy / market_volume (市佔); that is a different hypothesis",
                "source_table": "stock_broker_branch_daily + stock_daily_bars",
                "pit": "signal day T uses only date ≤ T bars and same-day branch tape (after close)",
                "vs_skilled_hand": (
                    "Related to clean_buy / sell_ratio in "
                    "amount_rrg_skilled_hand_rank_study; here hard filter, "
                    "not soft re-rank within 80M×rs>100 champ days"
                ),
            },
            "head_to_head": head,
            "b_param_grid_60bps_H8": grid_rows,
            "b_grid_top": grid_sorted[:5],
            "a_intersect_b_grid_60bps_H8": inter_rows,
            "a_intersect_top": inter_sorted[:5],
            "a_stratify_by_b_60bps_H8": strat_rows,
            "h10_contrast_60bps": h10_rows,
            "warnings": {
                "min_n": MIN_N_WARN,
                "min_oos_n": MIN_OOS_WARN,
                "union_caution": (
                    "A∪B mixes two generators; slot timing / selection leakage risk"
                ),
                "small_n": (
                    "B≥500M days are sparse (~50 signal days mother); "
                    "treat medians with n<"
                    f"{MIN_N_WARN} as directional only"
                ),
            },
        }

        # stdout digest
        print("\n=== Head-to-head (60bps · L1H8) ===")
        for row in head_rows:
            s = row["costs"]["60bps"]
            print(
                f"  {row['label']:32s} n={s.get('n')} "
                f"win={s.get('win_rate_pct')} med={s.get('median_pct')} "
                f"med/H={s.get('median_per_hold_day_pct')} "
                f"MaxDD={s.get('max_drawdown_pct')} "
                f"IS_n={s.get('is_n')} IS_med={s.get('is_median_pct')} "
                f"OOS_n={s.get('oos_n')} OOS_med={s.get('oos_median_pct')}"
                f"{' ⚠n' if s.get('n_warn') else ''}"
                f"{' ⚠oos' if s.get('oos_n_warn') else ''}"
            )
        print("\n=== B grid top5 (60bps) ===")
        for s in grid_sorted[:5]:
            print(
                f"  {s.get('amount_m')}M bs≥{s.get('buy_share_min')} "
                f"{s.get('rrg_mode')}: n={s.get('n')} med={s.get('median_pct')} "
                f"oos_n={s.get('oos_n')} oos_med={s.get('oos_median_pct')} "
                f"win={s.get('win_rate_pct')}"
            )
        print("\n=== A∩B top5 (60bps) ===")
        for s in inter_sorted[:5]:
            print(
                f"  ∩ {s.get('amount_m')}M bs≥{s.get('buy_share_min')} "
                f"{s.get('rrg_mode')}: n={s.get('n')} med={s.get('median_pct')} "
                f"oos_n={s.get('oos_n')} oos_med={s.get('oos_median_pct')}"
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
