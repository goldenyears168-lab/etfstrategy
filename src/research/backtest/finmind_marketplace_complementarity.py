"""FinMind marketplace · daily / weekly complementarity analysis."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from backtest_standard_config import load_backtest_standard_config
from market_benchmark import load_benchmark_close
from research.backtest.finmind_it_cold_stove_backtest import (
    build_periods as build_it_periods,
    enrich_period_returns as enrich_it,
)
from research.backtest.finmind_it_cold_stove_common import ItColdStoveParams
from research.backtest.finmind_low_rebound_backtest import (
    LowReboundExitRule,
    build_periods as build_lr_periods,
    enrich_period_returns as enrich_lr,
)
from research.backtest.finmind_ma20_breakout_backtest import (
    build_periods as build_ma20_periods,
    enrich_period_returns as enrich_ma20,
)
from research.backtest.finmind_ma20_breakout_common import Ma20BreakoutParams
from research.backtest.finmind_marketplace_comparison import (
    CMP_N_SLOTS_SLOT,
    CMP_UNIVERSE,
    MARKETPLACE_STRATEGIES,
    WINDOW_FULL_COMMON,
)
from research.backtest.finmind_ps_growth_backtest import (
    build_periods as build_ps_periods,
    enrich_period_returns as enrich_ps,
)
from research.backtest.finmind_ps_growth_common import PsGrowthParams
from research.backtest.finmind_screener_common import LowReboundParams, load_price_panels
from research.backtest.finmind_tx_foreign_backtest import (
    build_timing_periods,
    enrich_period_returns as enrich_tx,
)
from research.backtest.finmind_tx_foreign_common import TxForeignParams
from research.backtest.slot_portfolio_metrics import simulate_slot_portfolio
from stock_db import DEFAULT_DB_PATH, connect

SHORT_IDS = {
    "finmind-low-rebound-foreign-it-sync": "S1_lr",
    "finmind-low-ps-growth": "S2_ps",
    "finmind-tx-foreign-net-long-3d": "S3_tx",
    "finmind-ma20-breakout-volume": "S4_ma20",
    "finmind-it-cold-stove": "S5_it",
}

CLUSTER_LABELS = {
    "inst_flow": "籌碼/法人流",
    "fundamental": "基本面慢篩",
    "macro_timing": "總經擇時",
    "momentum": "技術動能",
}


@dataclass
class StrategyDailyPanel:
    topic_id: str
    label: str
    short_id: str
    cluster: str
    engine: str
    dates: list[str]
    daily_return_pct: list[float]
    signal_flag: list[int]
    in_market_flag: list[int]
    signal_dates: set[str]
    in_market_dates: set[str]
    n_periods: int
    signal_days: int
    exposure_pct: float


def _strategy_cluster(topic_id: str) -> str:
    return {
        "finmind-low-rebound-foreign-it-sync": "inst_flow",
        "finmind-it-cold-stove": "inst_flow",
        "finmind-low-ps-growth": "fundamental",
        "finmind-tx-foreign-net-long-3d": "macro_timing",
        "finmind-ma20-breakout-volume": "momentum",
    }[topic_id]


def _load_periods(
    conn: sqlite3.Connection,
    topic_id: str,
    *,
    date_start: str,
    date_end: str,
) -> tuple[list[dict[str, Any]], list[str], pd.DataFrame]:
    if topic_id == "finmind-low-rebound-foreign-it-sync":
        raw, _ = build_lr_periods(
            conn,
            universe=CMP_UNIVERSE,
            date_start=date_start,
            date_end=date_end,
            n_slots=CMP_N_SLOTS_SLOT,
            hold_days=20,
            params=LowReboundParams(),
        )
        periods = enrich_lr(conn, raw)
        close, _, _ = load_price_panels(conn)
        trade_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
        return periods, trade_dates, close

    if topic_id == "finmind-low-ps-growth":
        raw, _ = build_ps_periods(
            conn,
            universe=CMP_UNIVERSE,
            date_start=date_start,
            date_end=date_end,
            n_slots=CMP_N_SLOTS_SLOT,
            hold_days=60,
            params=PsGrowthParams(),
        )
        periods = enrich_ps(conn, raw)
        close, _, _ = load_price_panels(conn)
        trade_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
        return periods, trade_dates, close

    if topic_id == "finmind-ma20-breakout-volume":
        raw, _ = build_ma20_periods(
            conn,
            universe=CMP_UNIVERSE,
            date_start=date_start,
            date_end=date_end,
            n_slots=CMP_N_SLOTS_SLOT,
            hold_days=10,
            params=Ma20BreakoutParams(),
        )
        periods = enrich_ma20(conn, raw)
        close, _, _ = load_price_panels(conn)
        trade_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
        return periods, trade_dates, close

    if topic_id == "finmind-it-cold-stove":
        raw, _ = build_it_periods(
            conn,
            universe=CMP_UNIVERSE,
            date_start=date_start,
            date_end=date_end,
            n_slots=CMP_N_SLOTS_SLOT,
            params=ItColdStoveParams(),
        )
        periods = enrich_it(conn, raw)
        close, _, _ = load_price_panels(conn)
        trade_dates = [d for d in close.index.astype(str).tolist() if date_start <= d <= date_end]
        return periods, trade_dates, close

    if topic_id == "finmind-tx-foreign-net-long-3d":
        p = TxForeignParams()
        raw, _ = build_timing_periods(conn, date_start=date_start, date_end=date_end, params=p)
        periods = enrich_tx(conn, raw)
        bench = load_benchmark_close(conn, code=p.proxy_code)
        trade_dates = [d for d in bench.index.astype(str).tolist() if date_start <= d <= date_end]
        close = pd.DataFrame({p.proxy_code: bench.reindex(trade_dates).astype(float)})
        return periods, trade_dates, close

    raise ValueError(topic_id)


def _in_market_dates_from_periods(periods: list[dict[str, Any]], trade_dates: list[str]) -> set[str]:
    idx = {d: i for i, d in enumerate(trade_dates)}
    out: set[str] = set()
    for p in periods:
        ed = str(p.get("entry_date") or "")
        ex = str(p.get("exit_date") or "")
        if not ed or not ex:
            continue
        i0 = idx.get(ed)
        i1 = idx.get(ex)
        if i0 is None or i1 is None:
            continue
        for d in trade_dates[i0 : i1 + 1]:
            out.add(d)
    return out


def load_strategy_daily_panel(
    conn: sqlite3.Connection,
    spec: Any,
    *,
    date_start: str,
    date_end: str,
) -> StrategyDailyPanel:
    periods, trade_dates, close = _load_periods(
        conn, spec.topic_id, date_start=date_start, date_end=date_end
    )
    std = load_backtest_standard_config()
    n_slots = 1 if spec.engine == "timing" else CMP_N_SLOTS_SLOT
    portfolio = simulate_slot_portfolio(
        conn,
        close,
        trade_dates,
        periods,
        total_capital=std.comparison_notional_ntd,
        n_slots=n_slots,
        cost_model=std.cost_model if std.cost_model.get("enabled") else None,
        return_daily=True,
    )
    nav = portfolio.get("daily_nav") or []
    if nav:
        equities = [float(r["equity_ntd"]) for r in nav]
        daily_returns: list[float] = []
        for i in range(1, len(equities)):
            prev = equities[i - 1]
            daily_returns.append((equities[i] / prev - 1.0) * 100.0 if prev > 0 else 0.0)
        if equities:
            daily_returns = [0.0] + daily_returns
        signal_flag = [int(r.get("in_market_flag", 0)) for r in nav]
        in_market_flag = signal_flag[:]
        dates = [str(r["date"]) for r in nav]
    else:
        dates = trade_dates
        daily_returns = [0.0] * len(dates)
        in_market_flag = [0] * len(dates)
        signal_flag = [0] * len(dates)

    signal_dates = {str(p["signal_date"]) for p in periods if p.get("signal_date")}
    in_market_dates = _in_market_dates_from_periods(periods, trade_dates)
    exposure_pct = round(len(in_market_dates) / len(trade_dates) * 100.0, 2) if trade_dates else 0.0

    return StrategyDailyPanel(
        topic_id=spec.topic_id,
        label=spec.label,
        short_id=SHORT_IDS[spec.topic_id],
        cluster=_strategy_cluster(spec.topic_id),
        engine=spec.engine,
        dates=dates,
        daily_return_pct=daily_returns,
        signal_flag=[1 if d in signal_dates else 0 for d in dates],
        in_market_flag=[1 if d in in_market_dates else 0 for d in dates],
        signal_dates=signal_dates,
        in_market_dates=in_market_dates,
        n_periods=len(periods),
        signal_days=len(signal_dates),
        exposure_pct=exposure_pct,
    )


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx = statistics.mean(xs)
    my = statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den_x = math.sqrt(sum((x - mx) ** 2 for x in xs))
    den_y = math.sqrt(sum((y - my) ** 2 for y in ys))
    if den_x == 0 or den_y == 0:
        return None
    return round(num / (den_x * den_y), 4)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return round(len(a & b) / len(a | b), 4)


def _iso_week(d: str) -> str:
    y, m, day = (int(x) for x in d.split("-"))
    return f"{date(y, m, day).isocalendar().year}-W{date(y, m, day).isocalendar().week:02d}"


def _weekly_returns(panel: StrategyDailyPanel) -> dict[str, float]:
    by_week: dict[str, list[float]] = {}
    for d, r in zip(panel.dates, panel.daily_return_pct):
        by_week.setdefault(_iso_week(d), []).append(r)
    return {w: round(sum(rs), 4) for w, rs in by_week.items()}


def _weekly_signal(panel: StrategyDailyPanel) -> set[str]:
    by_week: set[str] = set()
    for d in panel.signal_dates:
        by_week.add(_iso_week(d))
    return by_week


def _quartile_threshold(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(len(s) * q)))
    return s[idx]


def pairwise_metrics(a: StrategyDailyPanel, b: StrategyDailyPanel) -> dict[str, Any]:
    daily_corr = _pearson(a.daily_return_pct, b.daily_return_pct)
    active_corr = _pearson(
        [r for r, fa, fb in zip(a.daily_return_pct, a.in_market_flag, b.in_market_flag) if fa or fb],
        [r for r, fa, fb in zip(b.daily_return_pct, a.in_market_flag, b.in_market_flag) if fa or fb],
    )
    sig_j = _jaccard(a.signal_dates, b.signal_dates)
    mkt_j = _jaccard(a.in_market_dates, b.in_market_dates)

    wa = _weekly_returns(a)
    wb = _weekly_returns(b)
    weeks = sorted(set(wa) & set(wb))
    w_rets_a = [wa[w] for w in weeks]
    w_rets_b = [wb[w] for w in weeks]
    week_corr = _pearson(w_rets_a, w_rets_b)

    q1_a = _quartile_threshold(w_rets_a, 0.25)
    med_b = statistics.median(w_rets_b) if w_rets_b else 0.0
    rescue_b_when_a_weak = 0
    weak_a_weeks = 0
    for w in weeks:
        if wa[w] <= q1_a:
            weak_a_weeks += 1
            if wb[w] > med_b:
                rescue_b_when_a_weak += 1
    rescue_rate = round(rescue_b_when_a_weak / weak_a_weeks, 4) if weak_a_weeks else None

    sig_week_a = _weekly_signal(a)
    sig_week_b = _weekly_signal(b)
    sig_week_j = _jaccard(sig_week_a, sig_week_b)

    co_signal_days = a.signal_dates & b.signal_dates

    similarity_score = round((daily_corr or 0) * 0.4 + sig_j * 0.4 + (mkt_j or 0) * 0.2, 4)
    complement_score = round(
        (1 - abs(daily_corr or 0)) * 0.35
        + (1 - sig_j) * 0.35
        + (rescue_rate or 0) * 0.30,
        4,
    )

    pattern = "neutral"
    if similarity_score >= 0.25 or (sig_j >= 0.08 and (daily_corr or 0) > 0.15):
        pattern = "similar"
    elif complement_score >= 0.55 and (daily_corr or 0) < 0.12 and sig_j < 0.04:
        pattern = "diagonal_complement"
    elif complement_score >= 0.45 and sig_j < 0.06:
        pattern = "weak_complement"

    return {
        "a": a.short_id,
        "b": b.short_id,
        "a_label": a.label,
        "b_label": b.label,
        "daily_return_corr": daily_corr,
        "active_daily_corr": active_corr,
        "weekly_return_corr": week_corr,
        "signal_day_jaccard": sig_j,
        "signal_week_jaccard": sig_week_j,
        "in_market_jaccard": mkt_j,
        "co_signal_days": len(co_signal_days),
        "rescue_rate_b_when_a_weak_week": rescue_rate,
        "similarity_score": similarity_score,
        "complement_score": complement_score,
        "pattern": pattern,
    }


def find_triangle_complements(panels: list[StrategyDailyPanel], pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key = {(p["a"], p["b"]): p for p in pairs}
    by_key.update({(p["b"], p["a"]): p for p in pairs})
    short_map = {p.short_id: p for p in panels}

    triangles: list[dict[str, Any]] = []
    ids = [p.short_id for p in panels]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            for k in range(j + 1, len(ids)):
                a, b, c = ids[i], ids[j], ids[k]
                ab = by_key.get((a, b)) or by_key.get((b, a))
                ac = by_key.get((a, c)) or by_key.get((c, a))
                bc = by_key.get((b, c)) or by_key.get((c, b))
                if not ab or not ac or not bc:
                    continue
                avg_sig_j = statistics.mean(
                    [ab["signal_day_jaccard"], ac["signal_day_jaccard"], bc["signal_day_jaccard"]]
                )
                avg_comp = statistics.mean(
                    [ab["complement_score"], ac["complement_score"], bc["complement_score"]]
                )
                weeks_union = (
                    _weekly_signal(short_map[a])
                    | _weekly_signal(short_map[b])
                    | _weekly_signal(short_map[c])
                )
                weeks_any = sum(
                    1
                    for w in weeks_union
                    if w
                    in (
                        _weekly_signal(short_map[a])
                        | _weekly_signal(short_map[b])
                        | _weekly_signal(short_map[c])
                    )
                )
                if avg_sig_j > 0.06:
                    continue
                triangles.append(
                    {
                        "triangle": [a, b, c],
                        "labels": [short_map[a].label, short_map[b].label, short_map[c].label],
                        "clusters": [short_map[a].cluster, short_map[b].cluster, short_map[c].cluster],
                        "avg_signal_jaccard": round(avg_sig_j, 4),
                        "avg_complement_score": round(avg_comp, 4),
                        "weekly_signal_coverage": len(weeks_union),
                        "score": round((1 - avg_sig_j) * 0.5 + avg_comp * 0.5, 4),
                    }
                )
    triangles.sort(key=lambda t: t["score"], reverse=True)
    return triangles[:5]


def build_complementarity_analysis(
    *,
    date_start: str = WINDOW_FULL_COMMON[1],
    date_end: str | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    end = date_end or date.today().isoformat()
    conn = connect(Path(db_path) if db_path else DEFAULT_DB_PATH)
    try:
        panels = [
            load_strategy_daily_panel(conn, spec, date_start=date_start, date_end=end)
            for spec in MARKETPLACE_STRATEGIES
        ]
    finally:
        conn.close()

    n_trade_days = len(panels[0].dates) if panels else 0
    rhythm = []
    for p in panels:
        gaps: list[int] = []
        sig_sorted = sorted(p.signal_dates)
        for i in range(1, len(sig_sorted)):
            i0 = p.dates.index(sig_sorted[i - 1]) if sig_sorted[i - 1] in p.dates else None
            i1 = p.dates.index(sig_sorted[i]) if sig_sorted[i] in p.dates else None
            if i0 is not None and i1 is not None:
                gaps.append(i1 - i0)
        rhythm.append(
            {
                "short_id": p.short_id,
                "label": p.label,
                "cluster": p.cluster,
                "cluster_label": CLUSTER_LABELS[p.cluster],
                "engine": p.engine,
                "n_periods": p.n_periods,
                "signal_days": p.signal_days,
                "signal_days_per_year": round(p.signal_days / max(1, (n_trade_days / 252)), 1),
                "pct_calendar_signal": round(p.signal_days / n_trade_days * 100, 2) if n_trade_days else 0,
                "exposure_pct": p.exposure_pct,
                "median_gap_signal_days": int(statistics.median(gaps)) if gaps else None,
            }
        )

    pairs: list[dict[str, Any]] = []
    for i in range(len(panels)):
        for j in range(i + 1, len(panels)):
            pairs.append(pairwise_metrics(panels[i], panels[j]))

    similar = [p for p in pairs if p["pattern"] == "similar"]
    diagonal = [p for p in pairs if p["pattern"] == "diagonal_complement"]
    weak_comp = [p for p in pairs if p["pattern"] == "weak_complement"]
    similar.sort(key=lambda x: x["similarity_score"], reverse=True)
    diagonal.sort(key=lambda x: x["complement_score"], reverse=True)
    weak_comp.sort(key=lambda x: x["complement_score"], reverse=True)

    triangles = find_triangle_complements(panels, pairs)

    all_weeks = set()
    for p in panels:
        all_weeks |= _weekly_signal(p)
    weekly_coverage = {
        p.short_id: round(len(_weekly_signal(p)) / len(all_weeks) * 100, 1) if all_weeks else 0
        for p in panels
    }

    return {
        "schema_version": "finmind_marketplace_complementarity-v1",
        "window": {"date_start": date_start, "date_end": end, "n_trade_days": n_trade_days},
        "rhythm": rhythm,
        "weekly_signal_coverage_pct": weekly_coverage,
        "pairwise": pairs,
        "similar_pairs": similar,
        "diagonal_complements": diagonal,
        "weak_complements": weak_comp,
        "triangle_complements": triangles,
        "generated_at": date.today().isoformat(),
    }


def render_complementarity_markdown(data: dict[str, Any]) -> str:
    w = data["window"]
    lines = [
        "# FinMind 市集策略 · 每日拆解與互補組隊",
        "",
        f"窗口：**{w['date_start']}** ～ **{w['date_end']}** · {w['n_trade_days']} 交易日",
        "",
        "> 比喻：五隻策略像神奇寶貝 — 不是天天上場，而是依**訊號日/持倉日**輪流發力。",
        "",
        "## 一、每日節奏（不是天天用）",
        "",
        "| ID | 策略 | 類型 | 新訊號日 | 佔交易日% | 持倉覆蓋% | 年均訊號日 | 訊號中位間隔(日) |",
        "|----|------|------|----------|-----------|------------|------------|------------------|",
    ]
    for r in data["rhythm"]:
        lines.append(
            f"| {r['short_id']} | {r['label']} | {r['cluster_label']} | {r['signal_days']} | "
            f"{r['pct_calendar_signal']}% | {r['exposure_pct']}% | {r['signal_days_per_year']} | "
            f"{r['median_gap_signal_days'] or '—'} |"
        )

    lines.extend(
        [
            "",
            "**解讀**：",
            "- **S2 低 P/S**、**S1 低檔回升** 訊號最稀疏 → 適合當「慢篩核心」，不必每日看。",
            "- **S4 MA20**、**S5 投信冷灶** 訊號密集但持倉重疊高 → 別當兩隻主力同時滿倉。",
            "- **S3 TX** 持倉覆蓋低、持倉極短 → 像「先發寶可夢」，觸發才上。",
            "",
            "## 二、相似策略（同隊會搶位）",
            "",
            "相似 = 訊號日重疊高 **或** 日報酬相關高 → 組隊時**擇一放大**即可。",
            "",
        ]
    )

    if data["similar_pairs"]:
        lines.append("| 配對 | 訊號日 Jaccard | 日報酬 corr | 持倉 Jaccard | 判定 |")
        lines.append("|------|----------------|-------------|--------------|------|")
        for p in data["similar_pairs"]:
            lines.append(
                f"| {p['a']} × {p['b']} | {p['signal_day_jaccard']} | "
                f"{p['daily_return_corr']} | {p['in_market_jaccard']} | 相似 |"
            )
    else:
        lines.append("_無強相似配對（訊號日重疊皆低）· 但同屬「籌碼流」的 S1/S5 仍建議不要雙滿倉。_")

    lines.extend(
        [
            "",
            "### 類型聚類",
            "",
            "| 聚類 | 成員 | 關係 |",
            "|------|------|------|",
            "| 籌碼/法人流 | S1 低檔回升、S5 投信冷灶 | **最相似** · 同看投信/法人 · 訊號日重疊相對最高 |",
            "| 技術動能 | S4 MA20 突破 | 與 S1/S5 部分重疊（熱門股）但觸發更頻繁 |",
            "| 基本面慢篩 | S2 低 P/S | **獨立** · 訊號稀少 · 與其他幾乎不同日 |",
            "| 總經擇時 | S3 TX 外資淨多 | **獨立** · 指數級 · 與個股策略不同維度 |",
            "",
            "## 三、互補關係",
            "",
            "### 對角互補（A 弱週時 B 常強）",
            "",
            "對角 = 訊號日幾乎不重疊 **且** 週報酬相關低 **且** 「A 弱週 B 救援率高」。",
            "",
        ]
    )

    if data["diagonal_complements"]:
        lines.append("| 配對 | 訊號 Jaccard | 週 corr | 救援率 | 互補分 |")
        lines.append("|------|--------------|---------|--------|--------|")
        for p in data["diagonal_complements"]:
            lines.append(
                f"| {p['a']} × {p['b']} | {p['signal_day_jaccard']} | "
                f"{p['weekly_return_corr']} | {p['rescue_rate_b_when_a_weak_week']} | "
                f"{p['complement_score']} |"
            )
    else:
        lines.append("_嚴格對角互補配對見下表「準對角」· 全樣本週期相關不完全為負。_")

    lines.extend(["", "### 準對角 / 弱互補（全配對排序）", ""])
    lines.append("| 配對 | 訊號 Jaccard | 日 corr | 救援率 | 互補分 | 判定 |")
    lines.append("|------|--------------|---------|--------|--------|------|")
    for p in sorted(data["pairwise"], key=lambda x: x["complement_score"], reverse=True)[:6]:
        lines.append(
            f"| {p['a']} × {p['b']} | {p['signal_day_jaccard']} | {p['daily_return_corr']} | "
            f"{p['rescue_rate_b_when_a_weak_week']} | {p['complement_score']} | {p['pattern']} |"
        )

    lines.extend(
        [
            "",
            "### 三角形互補（三隻輪流補位）",
            "",
            "三角 = 三策略兩兩訊號不重疊，但合起來**覆蓋更多交易週**。",
            "",
        ]
    )
    if data["triangle_complements"]:
        lines.append("| 三角 | 類型組合 | 平均訊號 Jaccard | 互補分 | 聯合訊號週 |")
        lines.append("|------|----------|------------------|--------|------------|")
        for t in data["triangle_complements"][:3]:
            tri = " + ".join(t["triangle"])
            clusters = " / ".join(CLUSTER_LABELS[c] for c in t["clusters"])
            lines.append(
                f"| {tri} | {clusters} | {t['avg_signal_jaccard']} | "
                f"{t['score']} | {t['weekly_signal_coverage']} 週 |"
            )
    else:
        lines.append("_（計算中無高分三角）_")

    lines.extend(
        [
            "",
            "## 四、推薦組隊（寶可夢編隊）",
            "",
            "### 🟢 主力三角（推薦）",
            "",
            "**S2 低 P/S（慢篩核心） + S3 TX 外資淨多（大盤先發） + S4 MA20 突破（短線突擊）**",
            "",
            "- 三者分屬 **基本面 / 總經 / 動能** · 訊號日幾乎不重疊",
            "- S2 負責「少而精」的中期持股 · S3 在總經轉多時快速上場",
            "- S4 填補 S2 等訊號的空窗期 · 提供短週期交易",
            "",
            "### 🟡 備援對角",
            "",
            "- **S3 × S2**：最乾淨的對角（指數 vs 個股基本面）",
            "- **S3 × S4**：總經擇時 + 技術突破 · 適合多頭初段",
            "- **S2 × S4**：S2 空窗時用 S4 維持市場曝險",
            "",
            "### 🔴 避免同隊雙滿倉",
            "",
            "- **S1 + S5**：同屬籌碼流 · 訊號/持倉重疊最高 → 選一個代表即可",
            "- **S4 + S5**：訊號都偏頻繁 · 同時滿倉易超買熱門股",
            "",
            "### 實操節奏（每週檢查）",
            "",
            "| 日曆 | 看什麼 |",
            "|------|--------|",
            "| 每日收盤後 | S3 TX 外資 OI 是否淨多 3 日 → 觸發則隔日開盤 IX0001",
            "| 每日 | S4 / S5 掃描（頻繁）· 有槽才進",
            "| 每週 1 次 | S2 低 P/S 慢篩 · S1 低檔回升",
            "",
            "## 五、全配對矩陣（訊號日 Jaccard）",
            "",
        ]
    )

    ids = [r["short_id"] for r in data["rhythm"]]
    header = "| | " + " | ".join(ids) + " |"
    sep = "|---|" + "|".join(["---"] * len(ids)) + "|"
    lines.append(header)
    lines.append(sep)
    pair_map = {(p["a"], p["b"]): p["signal_day_jaccard"] for p in data["pairwise"]}
    pair_map.update({(p["b"], p["a"]): p["signal_day_jaccard"] for p in data["pairwise"]})
    for row_id in ids:
        cells = [row_id]
        for col_id in ids:
            if row_id == col_id:
                cells.append("—")
            else:
                cells.append(str(pair_map.get((row_id, col_id), "—")))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def run_complementarity_analysis(
    *,
    db_path: str | Path | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    out = out_dir or Path("reports/research/finmind-marketplace")
    out.mkdir(parents=True, exist_ok=True)
    data = build_complementarity_analysis(db_path=db_path)
    stamp = date.today().strftime("%Y%m%d")
    json_path = out / f"finmind_marketplace_complementarity_{stamp}.json"
    md_path = out / f"finmind_marketplace_complementarity_{stamp}.md"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_complementarity_markdown(data), encoding="utf-8")
    return {"json_path": json_path, "md_path": md_path, "data": data}
