"""金額×RRG 1 槽冠軍上，測試連續兩日分點變化與賣方動靜漏斗。

基準契約（舊報 period-return 冠軍）：
  xd ≥80M · rs_ratio>100 · Top1 · L2H12 · 1槽 · capital 10k

複利 SSOT（與 C18acc `_equal_slot_compound_pct` 同構）：
  單槽：依進出場順序 Π(1+r_i)−1。
  多槽：各槽分別複利後取平均。

選參只看 IS 單槽複利；OOS 僅 replay。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from copytrade.branch_signals import (
    DEFAULT_TRADER_ID_FUBON_XINDIAN,
    _is_listed_equity_id,
)
from copytrade.dual_branch_signals import DualBranchTapeCache
from copytrade.signals import CopytradeSignal
from report_paths import REPORTS_RESEARCH
from research.backtest.amount_rrg_copytrade_funnel_study import (
    DEFAULT_ENTRY_LAG,
    DEFAULT_HOLD,
    DEFAULT_OOS_CUT,
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
    _run_arm,
)
from research.backtest.amount_rrg_copytrade_study import (
    ETF_CODE_PROXY,
    apply_rrg_gate,
    build_rrg_ffill_index,
    build_signals_fixed,
    load_rrg_close_index,
)
from research.backtest.copytrade_backtest import (
    DEFAULT_SIGNAL_CAPITAL_NTD,
    run_copytrade_backtest,
    select_executed_signal_days,
)
from research.backtest.yuanta_songjiang_copytrade_sweep import day_results_to_dicts
from stock_db import load_broker_branch_nets

FILE_STEM = "amount_rrg_2day_sell_funnel_study"
DEFAULT_AMOUNT_NTD = 80_000_000.0
C18_OVERLAP_START = "2025-01-02"


@dataclass(frozen=True)
class TapeFunnel:
    id: str
    family: str
    label: str
    predicate: Callable[[dict[str, Any]], bool]


def _equal_slot_compound_pct(
    returns_by_slot: dict[Any, list[float]],
    *,
    capital: float = DEFAULT_SIGNAL_CAPITAL_NTD,
) -> float | None:
    """Match C18acc equal-slot compound: avg over slots of Π(1+r)−1."""
    if not returns_by_slot:
        return None
    comps: list[float] = []
    for rets in returns_by_slot.values():
        if not rets:
            continue
        eq = float(capital)
        for r in rets:
            eq *= 1.0 + float(r) / 100.0
        comps.append((eq / capital - 1.0) * 100.0)
    if not comps:
        return None
    return round(sum(comps) / len(comps), 4)


def compound_from_executed(executed: list[dict[str, Any]]) -> float | None:
    """1-slot sequential compound on basket return_pct."""
    rets = [
        float(d["return_pct"])
        for d in sorted(
            executed,
            key=lambda d: (str(d.get("entry_date") or ""), str(d.get("signal_date") or "")),
        )
        if d.get("return_pct") is not None
    ]
    if not rets:
        return None
    return _equal_slot_compound_pct({0: rets})


def load_xd_tape_bs(
    conn: sqlite3.Connection,
    *,
    window_start: str,
    window_end: str,
    trader_id: str = DEFAULT_TRADER_ID_FUBON_XINDIAN,
) -> dict[tuple[str, str], dict[str, float]]:
    """(date, stock_id) → buy/sell/net shares (all signs; net may be ≤0)."""
    # Widen start by ~15 calendar days so t-1 exists near window edge.
    rows = load_broker_branch_nets(
        conn,
        trader_id,
        window_start=None,
        window_end=window_end,
        min_net=None,
    )
    out: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        day = str(r["trade_date"])
        if day > window_end:
            continue
        sid = str(r["stock_id"])
        if not _is_listed_equity_id(sid):
            continue
        out[(day, sid)] = {
            "buy": float(r["buy"] or 0.0),
            "sell": float(r["sell"] or 0.0),
            "net": float(r["net"] or 0.0),
        }
    # Filter keys before window_start only after we have prev-day lookback built.
    return out


def _prev_trading_date(calendar: list[str], day: str) -> str | None:
    i = 0
    # calendar sorted
    lo, hi = 0, len(calendar)
    while lo < hi:
        mid = (lo + hi) // 2
        if calendar[mid] < day:
            lo = mid + 1
        else:
            hi = mid
    idx = lo - 1
    return calendar[idx] if idx >= 0 else None


def build_tape_feature(
    *,
    day: str,
    stock_id: str,
    tape_bs: dict[tuple[str, str], dict[str, float]],
    closes: dict[tuple[str, str], float],
    calendar: list[str],
    amount_thr: float,
) -> dict[str, Any] | None:
    cur = tape_bs.get((day, stock_id))
    if cur is None:
        return None
    prev_day = _prev_trading_date(calendar, day)
    prev = tape_bs.get((prev_day, stock_id)) if prev_day else None
    close_t = closes.get((stock_id, day))
    close_p = closes.get((stock_id, prev_day)) if prev_day else None
    amount_t = (
        float(cur["net"]) * float(close_t)
        if close_t is not None and float(cur["net"]) > 0
        else None
    )
    amount_p = (
        float(prev["net"]) * float(close_p)
        if prev is not None and close_p is not None and float(prev["net"]) > 0
        else None
    )
    buy_t, sell_t, net_t = float(cur["buy"]), float(cur["sell"]), float(cur["net"])
    buy_p = float(prev["buy"]) if prev else None
    sell_p = float(prev["sell"]) if prev else None
    net_p = float(prev["net"]) if prev else None
    sell_ratio = (sell_t / buy_t) if buy_t > 0 else None
    return {
        "prev_day": prev_day,
        "buy_t": buy_t,
        "sell_t": sell_t,
        "net_t": net_t,
        "buy_p": buy_p,
        "sell_p": sell_p,
        "net_p": net_p,
        "amount_t": amount_t,
        "amount_p": amount_p,
        "sell_ratio": sell_ratio,
        "net_delta_shares": (net_t - net_p) if net_p is not None else None,
        "amount_delta": (
            (amount_t - amount_p)
            if amount_t is not None and amount_p is not None
            else None
        ),
        "amount_thr": amount_thr,
        "has_prev": prev is not None,
    }


def default_funnels(amount_thr: float = DEFAULT_AMOUNT_NTD) -> list[TapeFunnel]:
    thr = float(amount_thr)

    def _amt_ge(feat: dict[str, Any], key: str) -> bool:
        v = feat.get(key)
        return v is not None and float(v) >= thr

    return [
        TapeFunnel(
            "baseline",
            "baseline",
            "無額外漏斗（冠軍基準）",
            lambda _f: True,
        ),
        TapeFunnel(
            "net_2day_pass",
            "two_day",
            "連續兩日金額皆 ≥ 門檻（續航）",
            lambda f: _amt_ge(f, "amount_t") and _amt_ge(f, "amount_p"),
        ),
        TapeFunnel(
            "net_fresh_cross",
            "two_day",
            "首日跨越金額門檻（昨未達標）",
            lambda f: _amt_ge(f, "amount_t")
            and (f.get("amount_p") is None or float(f["amount_p"]) < thr),
        ),
        TapeFunnel(
            "net_accel",
            "two_day",
            "金額較昨增加（加速買超）",
            lambda f: f.get("amount_delta") is not None
            and float(f["amount_delta"]) > 0,
        ),
        TapeFunnel(
            "net_decel_still_pass",
            "two_day",
            "金額仍達標但較昨減少（減速）",
            lambda f: _amt_ge(f, "amount_t")
            and f.get("amount_delta") is not None
            and float(f["amount_delta"]) < 0,
        ),
        TapeFunnel(
            "sell_down_10",
            "sell_side",
            "賣出股數較昨降 ≥10%",
            lambda f: f.get("sell_p") is not None
            and float(f["sell_p"]) > 0
            and float(f["sell_t"]) <= float(f["sell_p"]) * 0.90,
        ),
        TapeFunnel(
            "buy_up_sell_down",
            "sell_side",
            "買增且賣減（買賣分歧偏多）",
            lambda f: f.get("buy_p") is not None
            and f.get("sell_p") is not None
            and float(f["buy_t"]) > float(f["buy_p"])
            and float(f["sell_t"]) < float(f["sell_p"]),
        ),
        TapeFunnel(
            "sell_ratio_le_0_35",
            "sell_side",
            "當日 sell/buy ≤ 0.35",
            lambda f: f.get("sell_ratio") is not None
            and float(f["sell_ratio"]) <= 0.35,
        ),
        TapeFunnel(
            "sell_ratio_le_0_50",
            "sell_side",
            "當日 sell/buy ≤ 0.50",
            lambda f: f.get("sell_ratio") is not None
            and float(f["sell_ratio"]) <= 0.50,
        ),
        TapeFunnel(
            "2day_pass_and_sell_down",
            "combo",
            "連續兩日達標 ∧ 賣出降 ≥10%",
            lambda f: _amt_ge(f, "amount_t")
            and _amt_ge(f, "amount_p")
            and f.get("sell_p") is not None
            and float(f["sell_p"]) > 0
            and float(f["sell_t"]) <= float(f["sell_p"]) * 0.90,
        ),
        TapeFunnel(
            "fresh_cross_and_sell_ratio_le_0_50",
            "combo",
            "首日跨越 ∧ sell/buy ≤ 0.50",
            lambda f: _amt_ge(f, "amount_t")
            and (f.get("amount_p") is None or float(f["amount_p"]) < thr)
            and f.get("sell_ratio") is not None
            and float(f["sell_ratio"]) <= 0.50,
        ),
        TapeFunnel(
            "accel_and_buy_up_sell_down",
            "combo",
            "金額加速 ∧ 買增賣減",
            lambda f: f.get("amount_delta") is not None
            and float(f["amount_delta"]) > 0
            and f.get("buy_p") is not None
            and f.get("sell_p") is not None
            and float(f["buy_t"]) > float(f["buy_p"])
            and float(f["sell_t"]) < float(f["sell_p"]),
        ),
    ]


def filter_grouped_by_funnel(
    grouped: dict[str, list[CopytradeSignal]],
    *,
    funnel: TapeFunnel,
    tape_bs: dict[tuple[str, str], dict[str, float]],
    closes: dict[tuple[str, str], float],
    calendar: list[str],
    amount_thr: float,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
    out: dict[str, list[CopytradeSignal]] = {}
    n_in = 0
    n_pass = 0
    n_miss = 0
    for day, legs in grouped.items():
        kept: list[CopytradeSignal] = []
        for leg in legs:
            n_in += 1
            feat = build_tape_feature(
                day=day,
                stock_id=str(leg.stock_id),
                tape_bs=tape_bs,
                closes=closes,
                calendar=calendar,
                amount_thr=amount_thr,
            )
            if feat is None:
                n_miss += 1
                continue
            if funnel.predicate(feat):
                n_pass += 1
                kept.append(leg)
        if kept:
            out[day] = kept
    return out, {
        "legs_in": n_in,
        "legs_pass": n_pass,
        "legs_missing_feat": n_miss,
        "pass_pct": round(100.0 * n_pass / n_in, 2) if n_in else None,
        "signal_days": len(out),
    }


def _enrich_metrics_with_compound(
    conn: sqlite3.Connection,
    grouped: dict[str, list[CopytradeSignal]],
    *,
    label: str,
    capital_ntd: float,
    window_start: str,
    window_end: str,
    oos_cut: str,
) -> dict[str, Any]:
    base = _run_arm(
        conn,
        grouped,
        label=label,
        n_slots=1,
        capital_ntd=capital_ntd,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
        entry_lag=DEFAULT_ENTRY_LAG,
        hold=DEFAULT_HOLD,
        etf_code_proxy=ETF_CODE_PROXY,
    )
    # Recompute compound from the same backtest path for full/is/oos.
    result = run_copytrade_backtest(
        conn,
        ETF_CODE_PROXY,
        strategy_id=f"L{DEFAULT_ENTRY_LAG + 1}H{DEFAULT_HOLD}",
        strategy_label=label,
        entry_lag_days=DEFAULT_ENTRY_LAG,
        hold_trading_days=DEFAULT_HOLD,
        entry_price_mode="open",
        capital_ntd=capital_ntd,
        cost_bps=0.0,
        window_start=window_start,
        window_end=window_end,
        grouped=grouped,
    )
    day_dicts = day_results_to_dicts(result)
    complete = [d for d in day_dicts if d.get("status") == "complete"]

    def _slice(start: str, end: str) -> dict[str, Any]:
        rows = [
            d
            for d in complete
            if start <= str(d.get("signal_date") or "") < end
        ]
        executed, meta = select_executed_signal_days(rows, n_slots=1)
        compound = compound_from_executed(executed)
        sum_ret = round(
            sum(float(d["return_pct"]) for d in executed if d.get("return_pct") is not None),
            4,
        )
        return {
            "n_cycles": len(executed),
            "sum_return_pct": sum_ret,
            "compound_slot_pct": compound,
            "period_return_pct": round(
                100.0
                * sum(float(d.get("pnl_ntd") or 0) for d in executed)
                / capital_ntd,
                4,
            )
            if capital_ntd
            else None,
            "win_rate_pct": (
                round(
                    100.0
                    * sum(float(d.get("pnl_ntd") or 0) > 0 for d in executed)
                    / len(executed),
                    2,
                )
                if executed
                else None
            ),
            "capture_pct": meta.get("signal_capture_pct"),
            "alpha_ntd": round(
                sum(float(d.get("alpha_ntd") or 0) for d in executed), 2
            ),
            "pnl_ntd": round(
                sum(float(d.get("pnl_ntd") or 0) for d in executed), 2
            ),
        }

    from datetime import timedelta

    # Backtest window_end is inclusive; slices use [start, end).
    end_open = (date.fromisoformat(window_end) + timedelta(days=1)).isoformat()
    full = _slice(window_start, end_open)
    is_m = _slice(window_start, oos_cut)
    oos_m = _slice(oos_cut, end_open)
    overlap = _slice(C18_OVERLAP_START, end_open)
    return {
        "label": label,
        "legacy_arm": base,
        "full": full,
        "is": is_m,
        "oos": oos_m,
        "overlap_from_202501": overlap,
    }


def _oos_gate(
    arm: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    b, a = baseline["oos"], arm["oos"]
    b_n = int(b.get("n_cycles") or 0)
    a_n = int(a.get("n_cycles") or 0)
    b_c = float(b.get("compound_slot_pct") or 0.0)
    a_c = float(a.get("compound_slot_pct") or -1e18)
    checks = {
        "retain_oos_cycles_ge_50pct": a_n >= max(5, int(b_n * 0.50)),
        "compound_not_worse_10pct": a_c >= b_c * 0.90 if b_c > 0 else a_c >= b_c,
        "compound_improves_10pct": a_c >= b_c * 1.10 if b_c > 0 else a_c > b_c,
        "win_rate_not_worse_10pt": float(a.get("win_rate_pct") or 0)
        >= float(b.get("win_rate_pct") or 0) - 10.0,
    }
    if not checks["retain_oos_cycles_ge_50pct"]:
        status = "rejected"
    elif checks["compound_improves_10pct"] and checks["win_rate_not_worse_10pt"]:
        status = "passed"
    elif checks["compound_not_worse_10pct"] and checks["win_rate_not_worse_10pt"]:
        status = "redundant"
    else:
        status = "rejected"
    return {"status": status, "checks": checks}


def run_2day_sell_funnel_study(
    conn: sqlite3.Connection,
    *,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str = DEFAULT_WINDOW_END,
    oos_cut: str = DEFAULT_OOS_CUT,
    amount_ntd: float = DEFAULT_AMOUNT_NTD,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
) -> dict[str, Any]:
    tape = DualBranchTapeCache.load(
        conn, window_start=window_start, window_end=window_end, need_closes=True
    )
    calendar = sorted(set(tape.sj) | set(tape.xd))
    # Prefer XD trading calendar for prev-day; fall back to union.
    xd_days = sorted(tape.xd.keys())
    cal = xd_days if xd_days else calendar

    tape_bs = load_xd_tape_bs(
        conn, window_start=window_start, window_end=window_end
    )
    raw_rrg = load_rrg_close_index(
        conn, window_start=window_start, window_end=window_end
    )
    rrg_ffill = build_rrg_ffill_index(raw_rrg, calendar)

    raw = build_signals_fixed(
        conn,
        mode="xd",
        min_threshold=amount_ntd,
        top_n=1,
        window_start=window_start,
        window_end=window_end,
        tape=tape,
        min_avg_volume_shares=200_000.0,
    )
    baseline_grouped, _, gate_drops = apply_rrg_gate(
        raw, rrg_ffill, "rs_ratio_gt100"
    )

    funnels = default_funnels(amount_ntd)
    arms: list[dict[str, Any]] = []
    for funnel in funnels:
        grouped, funnel_meta = filter_grouped_by_funnel(
            baseline_grouped,
            funnel=funnel,
            tape_bs=tape_bs,
            closes=tape.closes,
            calendar=cal,
            amount_thr=amount_ntd,
        )
        metrics = _enrich_metrics_with_compound(
            conn,
            grouped,
            label=f"80M rs>100 L2H12 S1 · {funnel.id}",
            capital_ntd=capital_ntd,
            window_start=window_start,
            window_end=window_end,
            oos_cut=oos_cut,
        )
        arms.append(
            {
                "funnel_id": funnel.id,
                "family": funnel.family,
                "label": funnel.label,
                "funnel_meta": funnel_meta,
                **metrics,
            }
        )

    baseline = next(a for a in arms if a["funnel_id"] == "baseline")
    # IS select: max compound among arms with enough IS cycles
    base_is_n = int(baseline["is"].get("n_cycles") or 0)
    viable = [
        a
        for a in arms
        if int(a["is"].get("n_cycles") or 0) >= max(6, int(base_is_n * 0.50))
    ]
    selected = max(
        viable or [baseline],
        key=lambda a: (
            float(a["is"].get("compound_slot_pct") or -1e18),
            float(a["is"].get("sum_return_pct") or -1e18),
            int(a["is"].get("n_cycles") or 0),
        ),
    )
    gate = _oos_gate(selected, baseline)
    surviving = (
        selected
        if gate["status"] in {"passed", "redundant"}
        else baseline
    )

    # Per-family best on IS + gate status
    family_summary: dict[str, Any] = {}
    for fam in sorted({a["family"] for a in arms}):
        fam_arms = [a for a in arms if a["family"] == fam]
        best = max(
            fam_arms,
            key=lambda a: float(a["is"].get("compound_slot_pct") or -1e18),
        )
        family_summary[fam] = {
            "best_funnel_id": best["funnel_id"],
            "is_compound": best["is"].get("compound_slot_pct"),
            "oos_gate": _oos_gate(best, baseline),
            "oos_compound": best["oos"].get("compound_slot_pct"),
            "full_compound": best["full"].get("compound_slot_pct"),
        }

    # Fair compound vs C18acc (from published fair study artifact if present)
    c18_path = (
        REPORTS_RESEARCH
        / "rrg"
        / "20260717_c18acc_vs_radar_c0_slot_fair.json"
    )
    c18_fair: dict[str, Any] | None = None
    if c18_path.exists():
        c18 = json.loads(c18_path.read_text(encoding="utf-8"))
        b0 = next(
            (v for v in c18.get("variants") or [] if v.get("variant_id") == "B0_live"),
            None,
        )
        if b0:
            sl = (b0.get("slices") or {}).get("full") or {}
            c18_fair = {
                "source": c18_path.name,
                "window": f"{c18.get('date_start')}→{c18.get('date_end')}",
                "n_slots": c18.get("n_slots"),
                "n_periods": sl.get("n_periods"),
                "compound_slot_pct": sl.get("compound_slot_pct"),
                "mean_return_pct": sl.get("mean_return_pct"),
                "mean_excess_pct": sl.get("mean_excess_pct"),
                "win_rate_gross_pct": sl.get("win_rate_gross_pct"),
                "note": (
                    "C18acc = avg of 3 slot compounds; "
                    "Copytrade below = 1 slot compound on overlap_from_202501"
                ),
            }

    def _arm(fid: str) -> dict[str, Any]:
        return next(a for a in arms if a["funnel_id"] == fid)

    hypotheses: dict[str, Any] = {
        "H_2DAY_CONTINUATION": {
            "statement": "連續兩日金額達標優於基準（OOS 複利加值）",
            "status": _oos_gate(_arm("net_2day_pass"), baseline)["status"],
        },
        "H_2DAY_FRESH_CROSS": {
            "statement": "首日跨越門檻優於基準",
            "status": _oos_gate(_arm("net_fresh_cross"), baseline)["status"],
        },
        "H_SELL_SIDE": {
            "statement": "賣方降溫／低 sell-buy 比可提升 OOS 單槽複利",
            "status": family_summary.get("sell_side", {})
            .get("oos_gate", {})
            .get("status", "rejected"),
            "best": family_summary.get("sell_side", {}).get("best_funnel_id"),
        },
        "H_COMBO": {
            "statement": "兩日訊號 ∧ 賣方漏斗組合可通過 OOS gate",
            "status": family_summary.get("combo", {})
            .get("oos_gate", {})
            .get("status", "rejected"),
            "best": family_summary.get("combo", {}).get("best_funnel_id"),
        },
        "H_SURVIVOR_BEATS_BASELINE": {
            "statement": "IS 複利冠軍在 OOS 相對基準加值 ≥10%",
            "status": gate["status"],
            "selected": selected["funnel_id"],
            "surviving": surviving["funnel_id"],
        },
    }

    return {
        "schema": "amount_rrg_2day_sell_funnel_study-v1",
        "contract": {
            "window_start": window_start,
            "window_end": window_end,
            "oos_cut": oos_cut,
            "baseline": "xd ≥80M · rs_ratio>100 · Top1 · L2H12 · 1槽",
            "capital_ntd_per_slot": capital_ntd,
            "compound_definition": (
                "equal_slot_compound = avg_slots Π(1+r_i)-1; "
                "1-slot ⇒ sequential product (same as C18acc helper)"
            ),
            "selection": "IS max compound_slot_pct; OOS robustness only",
            "rrg_gate_drops": gate_drops,
        },
        "baseline": {
            "funnel_id": "baseline",
            "full": baseline["full"],
            "is": baseline["is"],
            "oos": baseline["oos"],
            "overlap_from_202501": baseline["overlap_from_202501"],
        },
        "arms": [
            {
                "funnel_id": a["funnel_id"],
                "family": a["family"],
                "label": a["label"],
                "funnel_meta": a["funnel_meta"],
                "full": a["full"],
                "is": a["is"],
                "oos": a["oos"],
                "overlap_from_202501": a["overlap_from_202501"],
                "oos_gate_vs_baseline": _oos_gate(a, baseline),
            }
            for a in arms
        ],
        "is_selected": {
            "funnel_id": selected["funnel_id"],
            "full": selected["full"],
            "is": selected["is"],
            "oos": selected["oos"],
            "oos_gate": gate,
        },
        "gate_surviving": {
            "funnel_id": surviving["funnel_id"],
            "full": surviving["full"],
            "is": surviving["is"],
            "oos": surviving["oos"],
            "overlap_from_202501": surviving["overlap_from_202501"],
        },
        "family_summary": family_summary,
        "fair_compound_vs_c18acc": {
            "c18acc": c18_fair,
            "copytrade_baseline_overlap": baseline["overlap_from_202501"],
            "copytrade_survivor_overlap": surviving["overlap_from_202501"],
            "verdict_note": (
                "Compare compound_slot_pct only. "
                "C18acc uses 3-slot average; copytrade uses 1-slot product. "
                "Overlap window starts 2025-01-02."
            ),
        },
        "hypotheses": hypotheses,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    c = payload["contract"]
    lines = [
        "# 金額×RRG · 連續兩日／賣方漏斗 · 單槽複利",
        "",
        f"- window: `{c['window_start']}` → `{c['window_end']}` · OOS `{c['oos_cut']}`",
        f"- baseline: `{c['baseline']}`",
        f"- compound: `{c['compound_definition']}`",
        "",
        "## Fair compound vs C18acc",
        "",
    ]
    fair = payload["fair_compound_vs_c18acc"]
    c18 = fair.get("c18acc") or {}
    base_ov = fair.get("copytrade_baseline_overlap") or {}
    surv_ov = fair.get("copytrade_survivor_overlap") or {}
    lines.extend(
        [
            f"- C18acc compound_slot%: **{c18.get('compound_slot_pct')}** "
            f"(n={c18.get('n_periods')}, {c18.get('window')})",
            f"- Copytrade baseline overlap compound%: **{base_ov.get('compound_slot_pct')}** "
            f"(n={base_ov.get('n_cycles')}, sum_return={base_ov.get('sum_return_pct')})",
            f"- Copytrade survivor overlap compound%: **{surv_ov.get('compound_slot_pct')}** "
            f"(n={surv_ov.get('n_cycles')})",
            f"- note: {fair.get('verdict_note')}",
            "",
            "## Hypotheses",
            "",
        ]
    )
    for hid, h in payload["hypotheses"].items():
        lines.append(f"- **{hid}**: `{h.get('status')}` — {h.get('statement')}")
    lines.extend(["", "## Arms (IS compound → OOS gate)", ""])
    for a in payload["arms"]:
        g = a["oos_gate_vs_baseline"]
        lines.append(
            f"- `{a['funnel_id']}` · {a['label']} · "
            f"IS compound={a['is'].get('compound_slot_pct')} n={a['is'].get('n_cycles')} · "
            f"OOS compound={a['oos'].get('compound_slot_pct')} n={a['oos'].get('n_cycles')} · "
            f"gate=`{g['status']}` · pass%={a['funnel_meta'].get('pass_pct')}"
        )
    surv = payload["gate_surviving"]
    lines.extend(
        [
            "",
            "## Gate-surviving",
            "",
            f"- `{surv['funnel_id']}`",
            f"- full compound={surv['full'].get('compound_slot_pct')} "
            f"sum_return={surv['full'].get('sum_return_pct')}",
            f"- OOS compound={surv['oos'].get('compound_slot_pct')}",
            "",
            "Research only · not Strategy/Order.",
            "",
        ]
    )
    return "\n".join(lines)


def write_artifacts(
    payload: dict[str, Any],
    *,
    out_dir: Path | None = None,
    stamp: str | None = None,
) -> dict[str, Path]:
    out_dir = out_dir or (REPORTS_RESEARCH / "dual-branch-copytrade")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    stem = f"{stamp}_{FILE_STEM}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return {"json": json_path, "md": md_path}
