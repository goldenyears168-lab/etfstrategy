#!/usr/bin/env python3
"""富邦新店 · 大戶指紋 × 主契約 A 搭配優勢研究（Research-only）。

問題：如何搭配 9661 分點「大戶指紋」創造相對 A alone 的策略優勢？
（非「硬併主契約門檻」；主契約維持 paper_forward_required。）

A（主契約）:
  ≥50M net · Leading · Top1 · L1H8 · 1槽 · vol≥200k · 0bps+60bps

大戶指紋（PIT，訊號日 T）:
  mkt_share      = buy_shares / day_volume
  net_amt        = max(0, net_shares) × close
  buy_amt        = buy_shares × close
  buy_share      = buy / (buy+sell)          # 乾淨度輔指標（先前 B 失敗）
  prior{N}_net   = Σ_{t∈prior N sessions} net_shares（同分點同股；張數風格）
  prior{N}_amt   = Σ net_amt（金額風格）
  streak_ge300M  = 含 T 連續 buy_amt≥300M 日數
  next_day_cont  = T+1 是否買超（僅事後診斷，不作進場）

搭配用法:
  Tag only | Soft rank | Priority slot (A-first / W-first)
  | Satellite observe | Entry refine | Hold hint (H8/H10/H12)

  PYTHONPATH=src .venv/bin/python scripts/research/run_xd_whale_advantage_combo.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Literal

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
FILE_STEM = "xd_whale_advantage_combo"
HOLD = 8
MIN_N_WARN = 20
MIN_OOS_WARN = 8
POOL_TOP_N = 5

RrgMode = Literal["none", "leading"]
SoftMode = Literal[
    "mkt_share",
    "buy_amt",
    "net_amt",
    "prior20_net",
    "prior20_amt",
    "buy_share",
    "composite_mkt",
]

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

AMOUNT_FLOORS = (300e6, 500e6, 800e6)
MKT_SHARE_TAG = (0.05, 0.07, 0.10)
BUY_SHARE_TAG = (0.40, 0.50)
PRIOR_WINDOWS = (5, 10, 20)
HOLD_HINTS = (8, 10, 12)

# Focus whale arm for pairing (density-aware; mirrors mktshare study).
DEFAULT_W = {
    "min_buy_amt_ntd": 500e6,
    "min_mkt_share": 0.07,
    "rrg_mode": "leading",
}


def _buy_share(buy: float, sell: float) -> float | None:
    denom = float(buy) + float(sell)
    if denom <= 0:
        return None
    return float(buy) / denom


def load_tape_full(
    conn,
    *,
    trader_id: str,
    window_start: str,
    window_end: str,
) -> dict[tuple[str, str], dict[str, float]]:
    """(date, stock_id) → buy/sell/net/close/volume + derived amounts/shares."""
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
        bs = _buy_share(buy, sell)
        out[(day, sid)] = {
            "buy": buy,
            "sell": sell,
            "net": net,
            "close": close,
            "volume": float(volume),
            "buy_amt": buy * close,
            "net_amt": net * close if net > 0 else 0.0,
            "mkt_share": buy / float(volume),
            "buy_share": float(bs) if bs is not None else float("nan"),
            "sell_buy_ratio": (sell / buy) if buy > 0 else float("nan"),
        }
    return out


def _calendar_from_tape(tape: dict[tuple[str, str], dict[str, float]]) -> list[str]:
    return sorted({d for d, _ in tape.keys()})


def enrich_persistence(
    tape: dict[tuple[str, str], dict[str, float]],
    calendar: list[str],
) -> None:
    """In-place: prior{N}_net / prior{N}_amt / streak_ge300M / next_day_cont."""
    day_index = {d: i for i, d in enumerate(calendar)}
    for (day, sid), meta in list(tape.items()):
        idx = day_index.get(day)
        if idx is None:
            continue
        for n in PRIOR_WINDOWS:
            prior_days = calendar[max(0, idx - n) : idx]
            prior_net = 0.0
            prior_amt = 0.0
            for d in prior_days:
                row = tape.get((d, sid))
                if row is None:
                    continue
                prior_net += float(row["net"]) / 1000.0  # lots (skilled-hand style)
                prior_amt += float(row["net_amt"])
            meta[f"prior{n}_net"] = prior_net
            meta[f"prior{n}_amt"] = prior_amt

        streak = 0
        j = idx
        while j >= 0:
            row = tape.get((calendar[j], sid))
            if row is None or float(row["buy_amt"]) < 300e6:
                break
            streak += 1
            j -= 1
        meta["streak_ge300M"] = float(streak)

        # Post-hoc only (not PIT entry).
        if idx + 1 < len(calendar):
            nxt = tape.get((calendar[idx + 1], sid))
            meta["next_day_cont"] = (
                1.0 if nxt is not None and float(nxt["net"]) > 0 else 0.0
            )
        else:
            meta["next_day_cont"] = float("nan")


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
    score_key: str = "buy_amt",
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
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
        score = float(meta.get(score_key, meta["buy_amt"]))
        by_day[day].append((score, sid, leg))

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
        "score_key": score_key,
        "hold": hold,
        "top_n": top_n,
    }
    return out, funnel


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
        "oos_max_drawdown_pct": oos.get("max_drawdown_pct"),
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
    return {
        "label": label,
        "hold": hold,
        "summary": _arm_summary(arm, hold=hold),
        "arm": arm,
        "day_dicts": day_dicts,
    }


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


def priority_fill(
    primary: dict[str, list[CopytradeSignal]],
    secondary: dict[str, list[CopytradeSignal]],
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
    """Conflict → primary; else fill secondary."""
    out = {day: list(legs) for day, legs in primary.items()}
    filled = 0
    for day, legs in secondary.items():
        if day not in out:
            out[day] = list(legs)
            filled += 1
    stats = {
        "primary_days": len(primary),
        "secondary_days": len(secondary),
        "filled_days": filled,
        "overlap_days": len(set(primary) & set(secondary)),
        "combo_days": len(out),
        "same_stock_overlap": sum(
            1
            for d in set(primary) & set(secondary)
            if str(primary[d][0].stock_id) == str(secondary[d][0].stock_id)
        ),
    }
    return out, stats


def soft_rerank_a(
    raw_a: dict[str, list[CopytradeSignal]],
    features: dict[tuple[str, str], dict[str, Any]],
    tape: dict[tuple[str, str], dict[str, float]],
    *,
    mode: SoftMode,
    pool_top_n: int = POOL_TOP_N,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
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
    multi_leg_days = 0
    for day, legs in pool.items():
        if len(legs) > 1:
            multi_leg_days += 1
        scored: list[tuple[float, str, CopytradeSignal]] = []
        for leg in legs:
            sid = str(leg.stock_id)
            meta = tape.get((day, sid)) or {}
            mkt = float(meta.get("mkt_share") or 0.0)
            buy_amt = float(meta.get("buy_amt") or 0.0)
            net_amt = float(meta.get("net_amt") or 0.0)
            prior20_net = float(meta.get("prior20_net") or 0.0)
            prior20_amt = float(meta.get("prior20_amt") or 0.0)
            bs = meta.get("buy_share")
            buy_share = float(bs) if bs == bs else 0.0  # type: ignore[operator]
            if mode == "mkt_share":
                score = mkt
            elif mode == "buy_amt":
                score = buy_amt
            elif mode == "net_amt":
                score = net_amt
            elif mode == "prior20_net":
                score = prior20_net
            elif mode == "prior20_amt":
                score = prior20_amt
            elif mode == "buy_share":
                score = buy_share
            else:  # composite_mkt
                score = math.log1p(buy_amt / 1e6) * (1.0 + 10.0 * mkt) * (
                    0.5 + 0.5 * buy_share
                )
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
        "multi_leg_days": multi_leg_days,
        "flipped_vs_amount_top1": flipped,
        "flip_rate_pct": round(100.0 * flipped / len(pool), 2) if pool else None,
    }


FingerprintPred = Callable[[dict[str, float] | None], bool]


def _fp_defs() -> list[tuple[str, str, FingerprintPred]]:
    """(id, formula_zh, predicate on tape meta of A's pick)."""
    defs: list[tuple[str, str, FingerprintPred]] = []

    for thr in AMOUNT_FLOORS:
        m = thr / 1e6
        defs.append(
            (
                f"net_amt_ge_{m:g}M",
                f"net×close ≥ {m:g}M NTD",
                lambda meta, t=thr: meta is not None and float(meta["net_amt"]) >= t,
            )
        )
        defs.append(
            (
                f"buy_amt_ge_{m:g}M",
                f"buy×close ≥ {m:g}M NTD",
                lambda meta, t=thr: meta is not None and float(meta["buy_amt"]) >= t,
            )
        )

    for share in MKT_SHARE_TAG:
        defs.append(
            (
                f"mkt_share_ge_{share:g}",
                f"buy_shares/day_volume ≥ {share:g}",
                lambda meta, s=share: meta is not None and float(meta["mkt_share"]) >= s,
            )
        )

    for share in BUY_SHARE_TAG:
        defs.append(
            (
                f"buy_share_ge_{share:g}",
                f"buy/(buy+sell) ≥ {share:g}（乾淨度輔）",
                lambda meta, s=share: (
                    meta is not None
                    and meta["buy_share"] == meta["buy_share"]
                    and float(meta["buy_share"]) >= s
                ),
            )
        )

    for n in PRIOR_WINDOWS:
        # Median-ish floors chosen for density (lots / amt); absolute, PIT.
        defs.append(
            (
                f"prior{n}_net_ge_0",
                f"prior{n} Σ net_lots > 0（持續性·張數）",
                lambda meta, k=f"prior{n}_net": (
                    meta is not None and float(meta.get(k) or 0.0) > 0.0
                ),
            )
        )
        defs.append(
            (
                f"prior{n}_amt_ge_100M",
                f"prior{n} Σ net_amt ≥ 100M（持續性·金額）",
                lambda meta, k=f"prior{n}_amt": (
                    meta is not None and float(meta.get(k) or 0.0) >= 100e6
                ),
            )
        )

    defs.append(
        (
            "streak_ge300M_ge_2",
            "連續 buy_amt≥300M 日數 ≥ 2（含訊號日）",
            lambda meta: meta is not None and float(meta.get("streak_ge300M") or 0) >= 2,
        )
    )
    defs.append(
        (
            "streak_ge300M_ge_3",
            "連續 buy_amt≥300M 日數 ≥ 3（含訊號日）",
            lambda meta: meta is not None and float(meta.get("streak_ge300M") or 0) >= 3,
        )
    )
    # Compound: buy floor + mkt share (primary whale candidate from prior study).
    defs.append(
        (
            "whale_500M_ms07",
            "buy_amt≥500M 且 mkt_share≥0.07",
            lambda meta: (
                meta is not None
                and float(meta["buy_amt"]) >= 500e6
                and float(meta["mkt_share"]) >= 0.07
            ),
        )
    )
    defs.append(
        (
            "whale_300M_ms05",
            "buy_amt≥300M 且 mkt_share≥0.05",
            lambda meta: (
                meta is not None
                and float(meta["buy_amt"]) >= 300e6
                and float(meta["mkt_share"]) >= 0.05
            ),
        )
    )
    return defs


def stratify_a(
    a: dict[str, list[CopytradeSignal]],
    tape: dict[tuple[str, str], dict[str, float]],
    pred: FingerprintPred,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, list[CopytradeSignal]], dict[str, Any]]:
    pass_: dict[str, list[CopytradeSignal]] = {}
    fail: dict[str, list[CopytradeSignal]] = {}
    for day, legs in a.items():
        sid = str(legs[0].stock_id)
        meta = tape.get((day, sid))
        if pred(meta):
            pass_[day] = legs
        else:
            fail[day] = legs
    stats = {
        "a_days": len(a),
        "pass_days": len(pass_),
        "fail_days": len(fail),
        "tag_rate_pct": round(100.0 * len(pass_) / len(a), 2) if a else None,
    }
    return pass_, fail, stats


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
        "oos_win_delta_vs_A": _d("oos_win_rate_pct"),
        "maxdd_delta_vs_A": _d("max_drawdown_pct"),
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


def _advantage_flags(row: dict[str, Any], a_base: dict[str, Any]) -> dict[str, Any]:
    """Success vs A alone (honest small-n flags)."""
    vs = row.get("vs_A") or _delta_vs_a(row, a_base)
    oos_med_d = vs.get("oos_median_delta_vs_A")
    oos_win_d = vs.get("oos_win_delta_vs_A")
    maxdd_d = vs.get("maxdd_delta_vs_A")
    med_d = vs.get("median_delta_vs_A")
    n_ok = not row.get("n_warn")
    oos_ok = not row.get("oos_n_warn")
    n_retain = (
        a_base.get("n") is not None
        and row.get("n") is not None
        and int(row["n"]) >= max(MIN_N_WARN, int(0.7 * int(a_base["n"])))
    )
    edge_oos_med = (
        oos_ok and oos_med_d is not None and float(oos_med_d) > 0 and n_retain
    )
    edge_oos_win = (
        oos_ok and oos_win_d is not None and float(oos_win_d) > 0 and n_retain
    )
    # MaxDD: lower magnitude is better (values are typically positive pct).
    edge_dd = (
        n_ok
        and maxdd_d is not None
        and float(maxdd_d) < 0
        and (med_d is None or float(med_d) >= -0.5)
    )
    return {
        "vs_A": vs,
        "n_retained_ge_70pct": n_retain,
        "edge_oos_median": bool(edge_oos_med),
        "edge_oos_win": bool(edge_oos_win),
        "edge_maxdd_with_stable_med": bool(edge_dd),
        "any_edge": bool(edge_oos_med or edge_oos_win or edge_dd),
        "small_sample": bool(row.get("n_warn") or row.get("oos_n_warn")),
    }


def monthly_complement(
    a_days: dict[str, list[CopytradeSignal]],
    w_days: dict[str, list[CopytradeSignal]],
    a_day_dicts: list[dict[str, Any]],
    w_day_dicts: list[dict[str, Any]],
) -> dict[str, Any]:
    """When A monthly median ≤0, does W satellite help? Stock overlap."""
    def _by_month(day_dicts: list[dict[str, Any]]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = defaultdict(list)
        for row in day_dicts:
            if row.get("status") != "complete":
                continue
            ret = row.get("return_pct")
            if ret is None:
                continue
            m = str(row.get("signal_date") or "")[:7]
            if m:
                out[m].append(float(ret))
        return out

    a_m = _by_month(a_day_dicts)
    w_m = _by_month(w_day_dicts)
    months = sorted(set(a_m) | set(w_m))
    weak_a = []
    for m in months:
        a_rets = a_m.get(m) or []
        if not a_rets:
            continue
        a_med = sorted(a_rets)[len(a_rets) // 2]
        if a_med > 0:
            continue
        w_rets = w_m.get(m) or []
        w_med = sorted(w_rets)[len(w_rets) // 2] if w_rets else None
        weak_a.append(
            {
                "month": m,
                "a_n": len(a_rets),
                "a_median_pct": round(a_med, 4),
                "w_n": len(w_rets),
                "w_median_pct": round(w_med, 4) if w_med is not None else None,
                "w_helps": w_med is not None and w_med > 0,
            }
        )

    a_pairs = {(d, str(legs[0].stock_id)) for d, legs in a_days.items()}
    w_pairs = {(d, str(legs[0].stock_id)) for d, legs in w_days.items()}
    day_overlap = len(set(a_days) & set(w_days))
    stock_day_overlap = len(a_pairs & w_pairs)
    return {
        "a_signal_days": len(a_days),
        "w_signal_days": len(w_days),
        "day_overlap": day_overlap,
        "same_stock_day_overlap": stock_day_overlap,
        "overlap_rate_vs_a_pct": (
            round(100.0 * day_overlap / len(a_days), 2) if a_days else None
        ),
        "same_stock_overlap_vs_a_pct": (
            round(100.0 * stock_day_overlap / len(a_days), 2) if a_days else None
        ),
        "a_weak_months": weak_a,
        "a_weak_months_w_helps": sum(1 for r in weak_a if r.get("w_helps")),
        "a_weak_months_n": len(weak_a),
    }


def size_weight_hypothetical(
    day_dicts: list[dict[str, Any]],
    whale_days: set[str],
    *,
    whale_weight: float = 1.5,
) -> dict[str, Any]:
    """If whale-tagged cycles got higher capital weight (research only)."""
    complete = [r for r in day_dicts if r.get("status") == "complete"]
    if not complete:
        return {"n": 0}
    equal = [float(r["return_pct"]) for r in complete]
    weighted = []
    wsum = 0.0
    rsum = 0.0
    for r in complete:
        ret = float(r["return_pct"])
        w = whale_weight if str(r.get("signal_date")) in whale_days else 1.0
        weighted.append(ret * w)
        wsum += w
        rsum += ret * w
    equal_sorted = sorted(equal)
    # Weighted median approx via expanded list is messy; report mean blend.
    return {
        "n": len(complete),
        "whale_tagged_n": sum(
            1 for r in complete if str(r.get("signal_date")) in whale_days
        ),
        "whale_weight": whale_weight,
        "equal_weight_median_pct": round(
            equal_sorted[len(equal_sorted) // 2], 4
        ),
        "equal_weight_mean_pct": round(sum(equal) / len(equal), 4),
        "size_weighted_mean_pct": round(rsum / wsum, 4) if wsum else None,
        "delta_mean_vs_equal": (
            round(rsum / wsum - sum(equal) / len(equal), 4) if wsum else None
        ),
        "note": "研究層假想加權，不改 order / size 模組",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="XD whale advantage pairing study")
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

        print("loading full tape + persistence…")
        tape = load_tape_full(
            conn,
            trader_id=str(args.trader_id),
            window_start=args.start,
            window_end=hist_end,
        )
        calendar = _calendar_from_tape(tape)
        enrich_persistence(tape, calendar)
        features = ensure_features_for_tape(
            conn, features, tape, min_buy_amt_ntd=min(AMOUNT_FLOORS)
        )
        print(f"tape={len(tape)} calendar={len(calendar)} features={len(features)}")

        a_grouped, a_funnel = build_config_grouped(raw_a, features, CONTRACT_A)
        print(f"A alone signal_days={len(a_grouped)}")

        # --- Baseline A (0 + 60 bps) ---
        print("baseline A…")
        a_costs: dict[str, Any] = {}
        a_got_60 = None
        for cost in (0.0, 60.0):
            got = run_prebuilt(
                conn,
                a_grouped,
                label="A_alone_50M_Leading",
                window_start=args.start,
                window_end=hist_end,
                oos_cut=args.oos_cut,
                hold=HOLD,
                cost_bps=cost,
            )
            got["summary"]["funnel"] = a_funnel
            a_costs[f"{cost:g}bps"] = got["summary"]
            if cost == 60.0:
                a_got_60 = got
        assert a_got_60 is not None
        a_base = a_got_60["summary"]

        # --- Fingerprint information (Tag only on A) ---
        print("fingerprint tag screen…")
        fp_rows: list[dict[str, Any]] = []
        for fp_id, formula, pred in _fp_defs():
            pass_g, fail_g, st = stratify_a(a_grouped, tape, pred)
            row: dict[str, Any] = {
                "fingerprint": fp_id,
                "formula": formula,
                "strat_stats": st,
            }
            for tag, g in (("pass", pass_g), ("fail", fail_g)):
                if not g:
                    row[tag] = {"n": 0, "empty": True}
                    continue
                got = run_prebuilt(
                    conn,
                    g,
                    label=f"tag_{fp_id}_{tag}",
                    window_start=args.start,
                    window_end=hist_end,
                    oos_cut=args.oos_cut,
                    cost_bps=60.0,
                )
                s = got["summary"]
                s["vs_A"] = _delta_vs_a(s, a_base)
                row[tag] = s
            p = row.get("pass") or {}
            f = row.get("fail") or {}
            row["pass_minus_fail"] = {
                "oos_median": (
                    None
                    if p.get("oos_median_pct") is None or f.get("oos_median_pct") is None
                    else round(
                        float(p["oos_median_pct"]) - float(f["oos_median_pct"]), 4
                    )
                ),
                "median": (
                    None
                    if p.get("median_pct") is None or f.get("median_pct") is None
                    else round(float(p["median_pct"]) - float(f["median_pct"]), 4)
                ),
                "is_median": (
                    None
                    if p.get("is_median_pct") is None or f.get("is_median_pct") is None
                    else round(
                        float(p["is_median_pct"]) - float(f["is_median_pct"]), 4
                    )
                ),
                "oos_win": (
                    None
                    if p.get("oos_win_rate_pct") is None
                    or f.get("oos_win_rate_pct") is None
                    else round(
                        float(p["oos_win_rate_pct"]) - float(f["oos_win_rate_pct"]),
                        4,
                    )
                ),
            }
            # Info score: IS separation (select) + OOS confirmation (honest).
            is_sep = row["pass_minus_fail"]["is_median"]
            oos_sep = row["pass_minus_fail"]["oos_median"]
            pass_n = int(p.get("n") or 0)
            fail_n = int(f.get("n") or 0)
            row["info_score"] = {
                "is_sep_median": is_sep,
                "oos_sep_median": oos_sep,
                "pass_n": pass_n,
                "fail_n": fail_n,
                "usable": pass_n >= 5 and fail_n >= 8,
                "oos_confirmed": (
                    is_sep is not None
                    and oos_sep is not None
                    and float(is_sep) > 0
                    and float(oos_sep) > 0
                    and pass_n >= 5
                ),
            }
            fp_rows.append(row)
            print(
                f"  {fp_id}: pass_n={pass_n} fail_n={fail_n} "
                f"ΔIS={is_sep} ΔOOS={oos_sep}"
            )

        # Rank fingerprints: prefer OOS-confirmed positive separation; else IS sep.
        def _fp_rank(r: dict[str, Any]) -> tuple:
            info = r["info_score"]
            oos_sep = info.get("oos_sep_median")
            is_sep = info.get("is_sep_median")
            confirmed = 1 if info.get("oos_confirmed") else 0
            usable = 1 if info.get("usable") else 0
            return (
                confirmed,
                usable,
                float(oos_sep) if oos_sep is not None else -1e9,
                float(is_sep) if is_sep is not None else -1e9,
                int(info.get("pass_n") or 0),
            )

        fp_ranked = sorted(fp_rows, key=_fp_rank, reverse=True)
        best_fp = fp_ranked[0] if fp_ranked else None
        best_fp_id = best_fp["fingerprint"] if best_fp else "whale_500M_ms07"
        best_pred = next(p for i, _, p in _fp_defs() if i == best_fp_id)

        # Next-day continuation diagnostic (post-hoc on A picks).
        next_day_stats = {"a_days": 0, "continued": 0, "stopped": 0, "unknown": 0}
        for day, legs in a_grouped.items():
            meta = tape.get((day, str(legs[0].stock_id)))
            next_day_stats["a_days"] += 1
            if meta is None or meta.get("next_day_cont") != meta.get("next_day_cont"):
                next_day_stats["unknown"] += 1
            elif float(meta["next_day_cont"]) >= 1.0:
                next_day_stats["continued"] += 1
            else:
                next_day_stats["stopped"] += 1
        if next_day_stats["a_days"]:
            known = next_day_stats["continued"] + next_day_stats["stopped"]
            next_day_stats["continue_rate_pct"] = (
                round(100.0 * next_day_stats["continued"] / known, 2) if known else None
            )

        # --- Build W satellite with DEFAULT_W ---
        w_lead, w_lead_f = build_w_grouped(
            tape,
            features,
            min_buy_amt_ntd=float(DEFAULT_W["min_buy_amt_ntd"]),
            min_mkt_share=float(DEFAULT_W["min_mkt_share"]),
            rrg_mode="leading",
        )
        w_none, w_none_f = build_w_grouped(
            tape,
            features,
            min_buy_amt_ntd=float(DEFAULT_W["min_buy_amt_ntd"]),
            min_mkt_share=float(DEFAULT_W["min_mkt_share"]),
            rrg_mode="none",
        )

        # Soft ranks
        print("soft rank…")
        soft_arms: list[tuple[str, dict[str, list[CopytradeSignal]], dict[str, Any]]] = []
        for mode in (
            "mkt_share",
            "buy_amt",
            "net_amt",
            "prior20_net",
            "prior20_amt",
            "buy_share",
            "composite_mkt",
        ):
            g, f = soft_rerank_a(raw_a, features, tape, mode=mode)  # type: ignore[arg-type]
            soft_arms.append((f"Soft_{mode}", g, f))

        # Priority slots
        a_first, a_first_st = priority_fill(a_grouped, w_lead)
        w_first, w_first_st = priority_fill(w_lead, a_grouped)
        entry_refine = intersect_days(a_grouped, w_none, same_stock=True)
        a_tag_pass, a_tag_fail, tag_st = stratify_a(a_grouped, tape, best_pred)

        mode_specs: list[tuple[str, str, dict[str, list[CopytradeSignal]], dict[str, Any]]] = [
            ("baseline", "A_alone", a_grouped, a_funnel),
            ("satellite", "W_alone_500M_ms07_Leading", w_lead, w_lead_f),
            ("satellite", "W_alone_500M_ms07_noRRG", w_none, w_none_f),
            (
                "tag_only",
                f"A_tag_pass_{best_fp_id}",
                a_tag_pass,
                {"signal_days": len(a_tag_pass), **tag_st},
            ),
            (
                "tag_only",
                f"A_tag_fail_{best_fp_id}",
                a_tag_fail,
                {"signal_days": len(a_tag_fail), **tag_st},
            ),
            (
                "priority_slot",
                "Priority_A_first_W_fill",
                a_first,
                {"signal_days": len(a_first), **a_first_st},
            ),
            (
                "priority_slot",
                "Priority_W_first_A_fill",
                w_first,
                {"signal_days": len(w_first), **w_first_st},
            ),
            (
                "entry_refine",
                "Entry_refine_A_cap_W_same_stock",
                entry_refine,
                {
                    "signal_days": len(entry_refine),
                    "a_days": len(a_grouped),
                    "caution_small_n": True,
                },
            ),
        ]
        for label, g, f in soft_arms:
            mode_specs.append(("soft_rank", label, g, f))

        print("pairing modes H2H…")
        usage_rows: list[dict[str, Any]] = []
        mode_day_dicts: dict[str, list[dict[str, Any]]] = {
            "A_alone": a_got_60["day_dicts"],
        }
        for usage, label, grouped, funnel in mode_specs:
            if label == "A_alone":
                s = dict(a_base)
                s["usage"] = usage
                s["mode"] = label
                s["funnel"] = funnel
                s["advantage"] = _advantage_flags(s, a_base)
                usage_rows.append(s)
                continue
            print(f"  {label} days={len(grouped)}…")
            if not grouped:
                usage_rows.append(
                    {
                        "usage": usage,
                        "mode": label,
                        "n": 0,
                        "empty": True,
                        "funnel": funnel,
                    }
                )
                continue
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
            s["usage"] = usage
            s["mode"] = label
            s["funnel"] = funnel
            s["advantage"] = _advantage_flags(s, a_base)
            usage_rows.append(s)
            mode_day_dicts[label] = got["day_dicts"]

        # Complementarity: A vs W satellite
        print("complementarity…")
        complement = monthly_complement(
            a_grouped,
            w_lead,
            a_got_60["day_dicts"],
            mode_day_dicts.get("W_alone_500M_ms07_Leading")
            or run_prebuilt(
                conn,
                w_lead,
                label="W_tmp",
                window_start=args.start,
                window_end=hist_end,
                oos_cut=args.oos_cut,
                cost_bps=60.0,
            )["day_dicts"],
        )

        # Hold hint on best-fp tagged days
        print("hold hint on whale-tagged A days…")
        hold_rows: list[dict[str, Any]] = []
        for h in HOLD_HINTS:
            if not a_tag_pass:
                break
            got = run_prebuilt(
                conn,
                a_tag_pass,
                label=f"hold_hint_pass_H{h}",
                window_start=args.start,
                window_end=hist_end,
                oos_cut=args.oos_cut,
                hold=h,
                cost_bps=60.0,
            )
            s = got["summary"]
            s["hold"] = h
            s["subset"] = f"A_tag_pass_{best_fp_id}"
            hold_rows.append(s)
            print(
                f"  H{h}: n={s.get('n')} med={s.get('median_pct')} "
                f"med/H={s.get('median_per_hold_day_pct')} "
                f"oos_med={s.get('oos_median_pct')}"
            )

        # Size hint hypothetical on A cycles
        whale_day_set = set(a_tag_pass.keys())
        size_hyp = size_weight_hypothetical(
            a_got_60["day_dicts"], whale_day_set, whale_weight=1.5
        )

        # Also tag with whale_500M_ms07 for size if different
        alt_pass, _, _ = stratify_a(
            a_grouped,
            tape,
            next(p for i, _, p in _fp_defs() if i == "whale_500M_ms07"),
        )
        size_hyp_alt = size_weight_hypothetical(
            a_got_60["day_dicts"], set(alt_pass.keys()), whale_weight=1.5
        )

        # Rank usages that claim edge
        edged = [
            r
            for r in usage_rows
            if (r.get("advantage") or {}).get("any_edge")
            and not r.get("empty")
            and r.get("mode") != "A_alone"
        ]
        edged_sorted = sorted(
            edged,
            key=lambda r: (
                float((r.get("vs_A") or r.get("advantage", {}).get("vs_A") or {}).get(
                    "oos_median_delta_vs_A"
                )
                or -1e9),
                int(r.get("oos_n") or 0),
            ),
            reverse=True,
        )
        # Fix vs_A nesting — advantage embeds vs_A
        for r in usage_rows:
            adv = r.get("advantage") or {}
            if "vs_A" in adv and "vs_A" not in r:
                r["vs_A"] = adv["vs_A"]

        # Practical best usage selection
        def _pick_best_usage() -> dict[str, Any]:
            # Prefer soft/priority/satellite with any_edge & adequate n.
            for r in edged_sorted:
                if r.get("usage") in ("soft_rank", "priority_slot", "satellite"):
                    if not r.get("small_sample") and (r.get("advantage") or {}).get(
                        "any_edge"
                    ):
                        return {
                            "best_usage": r["usage"],
                            "best_mode": r["mode"],
                            "reason": "OOS edge vs A with adequate n",
                            "row": {
                                k: r.get(k)
                                for k in (
                                    "n",
                                    "oos_n",
                                    "median_pct",
                                    "oos_median_pct",
                                    "win_rate_pct",
                                    "max_drawdown_pct",
                                    "vs_A",
                                )
                            },
                        }
            # Tag-only value: informative fingerprint even if not trading edge
            if best_fp and (best_fp.get("info_score") or {}).get("oos_confirmed"):
                return {
                    "best_usage": "tag_only",
                    "best_mode": best_fp_id,
                    "reason": (
                        "No pairing arm beats A with adequate n; "
                        "best actionable edge is observe-tag on informative fingerprint"
                    ),
                    "fingerprint": {
                        "id": best_fp_id,
                        "formula": best_fp.get("formula"),
                        "pass_minus_fail": best_fp.get("pass_minus_fail"),
                        "strat_stats": best_fp.get("strat_stats"),
                    },
                }
            # Complementary satellite
            if complement.get("a_weak_months_w_helps", 0) >= 2 and (
                complement.get("same_stock_overlap_vs_a_pct") or 100
            ) < 40:
                return {
                    "best_usage": "satellite_observe",
                    "best_mode": "W_alone_500M_ms07_Leading",
                    "reason": "A weak months sometimes helped by W; low same-stock overlap",
                    "complement": {
                        k: complement.get(k)
                        for k in (
                            "day_overlap",
                            "same_stock_day_overlap",
                            "a_weak_months_n",
                            "a_weak_months_w_helps",
                        )
                    },
                }
            return {
                "best_usage": "keep_A_alone_observe_whale",
                "best_mode": "A_alone",
                "reason": (
                    "No pairing usage creates adoptable OOS advantage vs A; "
                    "keep A paper_forward_required; whale as research tag/satellite only"
                ),
            }

        best_usage = _pick_best_usage()

        # Top informative fingerprints for report
        fp_top = []
        for r in fp_ranked[:12]:
            fp_top.append(
                {
                    "fingerprint": r["fingerprint"],
                    "formula": r["formula"],
                    "tag_rate_pct": (r.get("strat_stats") or {}).get("tag_rate_pct"),
                    "pass_n": (r.get("pass") or {}).get("n"),
                    "fail_n": (r.get("fail") or {}).get("n"),
                    "pass_oos_median": (r.get("pass") or {}).get("oos_median_pct"),
                    "fail_oos_median": (r.get("fail") or {}).get("oos_median_pct"),
                    "pass_minus_fail": r.get("pass_minus_fail"),
                    "info_score": r.get("info_score"),
                }
            )

        # Usage summary table
        usage_table = []
        for r in usage_rows:
            usage_table.append(
                {
                    "usage": r.get("usage"),
                    "mode": r.get("mode"),
                    "n": r.get("n"),
                    "win_rate_pct": r.get("win_rate_pct"),
                    "median_pct": r.get("median_pct"),
                    "max_drawdown_pct": r.get("max_drawdown_pct"),
                    "oos_n": r.get("oos_n"),
                    "oos_median_pct": r.get("oos_median_pct"),
                    "oos_win_rate_pct": r.get("oos_win_rate_pct"),
                    "vs_A": r.get("vs_A") or (r.get("advantage") or {}).get("vs_A"),
                    "any_edge": (r.get("advantage") or {}).get("any_edge"),
                    "small_sample": r.get("n_warn") or r.get("oos_n_warn") or r.get("empty"),
                    "funnel_snip": {
                        k: (r.get("funnel") or {}).get(k)
                        for k in (
                            "signal_days",
                            "flip_rate_pct",
                            "filled_days",
                            "overlap_days",
                            "tag_rate_pct",
                            "soft_mode",
                        )
                        if (r.get("funnel") or {}).get(k) is not None
                    },
                }
            )

        payload = {
            "schema": "xd_whale_advantage_combo-v1",
            "generated_at": date.today().isoformat(),
            "status": "research_observe_only",
            "contract_frozen": {
                "branch": "9661 富邦-新店",
                "spec": "≥50M · Leading · Top1 · L1H8 · 1槽 · vol≥200k",
                "decision": "paper_forward_required",
                "forward_start": FORWARD_START,
                "hypothesis": "H-XD-50M-LEADING-L1H8-ADOPTION",
                "note": "Do not change strategy.yaml / order.yaml from this study.",
            },
            "window": {
                "start": args.start,
                "historical_end": hist_end,
                "oos_cut": args.oos_cut,
                "end_arg": args.end,
            },
            "definitions": {
                "fingerprints": {
                    "mkt_share": "branch buy_shares / stock_daily_bars.volume (finmind)",
                    "net_amt": "max(0, net_shares) × signal-day close",
                    "buy_amt": "buy_shares × signal-day close",
                    "buy_share": "buy/(buy+sell); auxiliary cleanliness (prior B failed alone)",
                    "priorN_net": "Σ prior N sessions net_shares/1000 (lots; skilled-hand style)",
                    "priorN_amt": "Σ prior N sessions net_amt (amount persistence)",
                    "streak_ge300M": "consecutive days buy_amt≥300M including T",
                    "next_day_cont": "T+1 net>0; POST-HOC only, not entry",
                },
                "vs_prior_studies": {
                    "failed_B_buy_share": (
                        "buy_share alone / hard A∩B hurt OOS — kept as aux only"
                    ),
                    "skilled_hand_prior20": (
                        "Soft re-rank within A eligibles (champ-day style); "
                        "H-RELATIONSHIP-RANK passed OOS compound on dual-branch champ "
                        "but IS weaker → observe_only; here tested under A 50M Leading L1H8"
                    ),
                    "mktshare_combo": (
                        "Prior xd_whale_mktshare_combo: hard gate/fill not adoptable; "
                        "this study expands fingerprints + priority/hold/size usages"
                    ),
                },
                "success_criteria": {
                    "oos_median_or_win_up_n_ok": (
                        f"oos_n≥{MIN_OOS_WARN}, n retain ≥70% of A, "
                        "oos median or win Δ>0"
                    ),
                    "maxdd_down_med_stable": "MaxDD↓ and median not worse by >0.5pp",
                    "complement": "A weak months + W helps + low same-stock overlap",
                },
                "min_n_warn": MIN_N_WARN,
                "min_oos_n_warn": MIN_OOS_WARN,
            },
            "a_baseline": a_costs,
            "fingerprint_screen_60bps_H8": fp_rows,
            "fingerprint_top": fp_top,
            "best_fingerprint": {
                "id": best_fp_id,
                "formula": best_fp.get("formula") if best_fp else None,
                "info_score": best_fp.get("info_score") if best_fp else None,
                "pass_minus_fail": best_fp.get("pass_minus_fail") if best_fp else None,
                "strat_stats": best_fp.get("strat_stats") if best_fp else None,
            },
            "next_day_continuation_posthoc": next_day_stats,
            "usage_table_60bps_H8": usage_table,
            "usage_rows_detail": usage_rows,
            "edged_usages": edged_sorted[:10],
            "complementarity_A_vs_W": complement,
            "hold_hint_on_best_fp_pass": hold_rows,
            "size_weight_hypothetical": {
                "best_fp": size_hyp,
                "whale_500M_ms07": size_hyp_alt,
            },
            "default_w_arm": DEFAULT_W,
            "best_usage_pick": best_usage,
            "playbook_zh": [
                "主契約 A（≥50M·Leading·Top1·L1H8·1槽）照跑；狀態維持 paper_forward_required。",
                "大戶指紋僅作研究層標註／獨立 paper 觀察臂，不改 A 進場門檻、不硬交集。",
                f"優先觀察指紋：{best_fp_id}"
                + (f"（{(best_fp or {}).get('formula')}）" if best_fp else ""),
                "Soft rank／Priority／Entry refine：僅在 OOS 有優勢且 n 足夠時才考慮；"
                "本檔結果見 usage_table 與 best_usage_pick。",
                "Hold/size：若 hold_hint 顯示延長 H 有利，只標記更高信念；不改 order size。",
                "分點合計 ≠ 單一帳戶；不可宣稱還原某位大戶 ID。",
            ],
            "next_steps": [
                "對 best_usage_pick 做 untouched forward（≥2026-07-18）旁觀，不改 Strategy/Order。",
                "若要測「空檔補缺」優勢，需另開 ≥2 槽或明確空槽設計（1槽幾乎被 A 佔滿）。",
                "持續性指紋（prior20）可與 amount-rrg skilled-hand 冠軍日協議對齊再測一次 soft-only。",
            ],
            "warnings": {
                "overfit": (
                    "Fingerprint thresholds scanned on full history; "
                    "IS separation used for ranking but OOS must confirm."
                ),
                "small_n": f"n<{MIN_N_WARN} or oos_n<{MIN_OOS_WARN} → observational only",
                "hard_gate": "Entry refine / hard intersect usually collapses n",
                "attribution": "Branch tape aggregate ≠ single whale identity",
            },
        }

        # Console summary
        print("\n=== A baseline 60bps ===")
        print(
            f"  n={a_base.get('n')} med={a_base.get('median_pct')} "
            f"win={a_base.get('win_rate_pct')} MaxDD={a_base.get('max_drawdown_pct')} "
            f"oos_n={a_base.get('oos_n')} oos_med={a_base.get('oos_median_pct')}"
        )
        print(f"\n=== Best fingerprint: {best_fp_id} ===")
        if best_fp:
            print(
                f"  {best_fp.get('formula')} | "
                f"ΔIS={best_fp['pass_minus_fail'].get('is_median')} "
                f"ΔOOS={best_fp['pass_minus_fail'].get('oos_median')} "
                f"tag%={(best_fp.get('strat_stats') or {}).get('tag_rate_pct')}"
            )
        print("\n=== Usage table (60bps) ===")
        for r in usage_table:
            d = r.get("vs_A") or {}
            print(
                f"  [{r.get('usage')}] {r.get('mode')}: "
                f"n={r.get('n')} med={r.get('median_pct')} "
                f"oos_n={r.get('oos_n')} oos_med={r.get('oos_median_pct')} "
                f"ΔOOS={d.get('oos_median_delta_vs_A')} "
                f"edge={r.get('any_edge')}"
                f"{' ⚠' if r.get('small_sample') else ''}"
            )
        print(f"\n=== Best usage: {best_usage.get('best_usage')} / {best_usage.get('best_mode')} ===")
        print(f"  {best_usage.get('reason')}")

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
