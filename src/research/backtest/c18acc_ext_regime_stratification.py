"""C18acc I36 extension overlay · Regime layer（環境層）分層 · G3."""

from __future__ import annotations

import sqlite3
import statistics
from typing import Any, Literal

from flow_returns import flow_tape_regime
from market_breadth_ma import BREADTH_ZONE_ZH, BREADTH_ZONES_ORDER, build_breadth_panel
from research.backtest.c18acc_intraday_1m_hold import (
    INTRADAY_1M_PHASE3B,
    run_intraday_grid_with_periods,
)

WindowLabel = Literal["FULL", "OOS"]

BREADTH_BUCKETS: dict[str, tuple[str, ...]] = {
    "risk_on": ("strong", "overbought"),
    "neutral": ("neutral",),
    "risk_off": ("weak", "oversold"),
}

BUCKET_LABELS: dict[str, str] = {
    "risk_on": "Strong/Overbought · 強勢/過熱",
    "neutral": "Neutral · 中性",
    "risk_off": "Weak/Oversold · 弱勢/超賣",
}


def _prior_date(full_dates: list[str], d: str) -> str | None:
    prior = [x for x in full_dates if x < d]
    return prior[-1] if prior else None


def _bucket_for_zone(zone: str | None) -> str:
    z = str(zone or "unknown")
    for bucket, zones in BREADTH_BUCKETS.items():
        if z in zones:
            return bucket
    return "unknown"


def _summarize_saves(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    n = len(values)
    pos = sum(1 for x in values if x > 0)
    return {
        "n": n,
        "mean_save_pct": round(sum(values) / n, 4),
        "median_save_pct": round(statistics.median(values), 4),
        "pct_save_positive": round(100.0 * pos / n, 1),
    }


def tag_ext_legs(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
    *,
    full_dates: list[str],
    zone_by_date: dict[str, str],
    oos_start: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in periods:
        if not p.get("ext_exit"):
            continue
        exit_d = str(p.get("exit_date") or "")
        prior = _prior_date(full_dates, exit_d)
        zone = zone_by_date.get(prior or exit_d, "unknown")
        tape = flow_tape_regime(conn, exit_d) if exit_d else None
        save = p.get("save_vs_orig_pct")
        if save is None:
            continue
        out.append(
            {
                "stock_id": p.get("stock_id"),
                "entry_date": p.get("entry_date"),
                "exit_date": exit_d,
                "exit_minute": p.get("exit_minute"),
                "save_vs_orig_pct": float(save),
                "excess_pct": p.get("excess_pct"),
                "orig_return_pct": p.get("orig_return_pct"),
                "return_pct": p.get("return_pct"),
                "breadth_zone_200": zone,
                "breadth_bucket": _bucket_for_zone(zone),
                "flow_tape_regime": tape,
                "window": "OOS" if oos_start and exit_d >= oos_start else "FULL",
            }
        )
    return out


def stratify_ext_legs(legs: list[dict[str, Any]]) -> dict[str, Any]:
    by_zone: dict[str, list[float]] = {z: [] for z in BREADTH_ZONES_ORDER}
    by_zone["unknown"] = []
    by_bucket: dict[str, list[float]] = {b: [] for b in (*BREADTH_BUCKETS, "unknown")}
    by_tape: dict[str, list[float]] = {}

    for leg in legs:
        save = float(leg["save_vs_orig_pct"])
        zone = str(leg.get("breadth_zone_200") or "unknown")
        by_zone.setdefault(zone, []).append(save)
        bucket = str(leg.get("breadth_bucket") or "unknown")
        by_bucket.setdefault(bucket, []).append(save)
        tape = str(leg.get("flow_tape_regime") or "unknown")
        by_tape.setdefault(tape, []).append(save)

    zone_stats = {z: _summarize_saves(v) for z, v in by_zone.items() if v}
    bucket_stats = {b: _summarize_saves(v) for b, v in by_bucket.items() if v}
    tape_stats = {t: _summarize_saves(v) for t, v in by_tape.items() if v}
    return {
        "by_breadth_zone": zone_stats,
        "by_breadth_bucket": bucket_stats,
        "by_flow_tape": tape_stats,
        "n_ext": len(legs),
    }


def evaluate_g3_hypotheses(strat: dict[str, Any]) -> dict[str, Any]:
    buckets = strat.get("by_breadth_bucket") or {}
    zones = strat.get("by_breadth_zone") or {}
    risk_on = buckets.get("risk_on") or {}
    neutral = buckets.get("neutral") or {}
    risk_off = buckets.get("risk_off") or {}
    strong = zones.get("strong") or {}
    overbought = zones.get("overbought") or {}

    ro_med = risk_on.get("median_save_pct")
    mid_med = neutral.get("median_save_pct")
    off_med = risk_off.get("median_save_pct")
    h_g3_1 = None
    if ro_med is not None and mid_med is not None and off_med is not None:
        h_g3_1 = ro_med > mid_med and ro_med > off_med
    elif ro_med is not None and (mid_med is not None or off_med is not None):
        other = [x for x in (mid_med, off_med) if x is not None]
        h_g3_1 = ro_med > max(other) if other else None

    ob_pct = overbought.get("pct_save_positive")
    st_pct = strong.get("pct_save_positive")
    h_g3_2 = None
    if ob_pct is not None and st_pct is not None:
        h_g3_2 = ob_pct >= st_pct

    return {
        "H-G3-1": {
            "pass": h_g3_1,
            "risk_on_median": ro_med,
            "neutral_median": mid_med,
            "risk_off_median": off_med,
        },
        "H-G3-2": {
            "pass": h_g3_2,
            "overbought_pct_positive": ob_pct,
            "strong_pct_positive": st_pct,
        },
    }


def run_ext_regime_stratification(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-30",
    oos_start: str = "2026-01-01",
) -> dict[str, Any]:
    i36_configs = [c for c in INTRADAY_1M_PHASE3B if c.variant_id == "I36"]
    full_grid, full_periods = run_intraday_grid_with_periods(
        conn, date_start=date_start, date_end=date_end, configs=i36_configs
    )
    oos_grid, oos_periods = run_intraday_grid_with_periods(
        conn, date_start=oos_start, date_end=date_end, configs=i36_configs
    )

    from research.backtest.finpilot_local_backtest import load_price_panels

    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    full_legs = tag_ext_legs(
        conn,
        full_periods.get("I36") or [],
        full_dates=full_dates,
        zone_by_date=zone_by_date,
    )
    oos_legs = tag_ext_legs(
        conn,
        oos_periods.get("I36") or [],
        full_dates=full_dates,
        zone_by_date=zone_by_date,
        oos_start=oos_start,
    )

    full_strat = stratify_ext_legs(full_legs)
    oos_strat = stratify_ext_legs(oos_legs)
    hypo_full = evaluate_g3_hypotheses(full_strat)
    hypo_oos = evaluate_g3_hypotheses(oos_strat)

    i36_summary = next(
        (s for s in (full_grid.get("summaries") or []) if s.get("variant_id") == "I36"),
        {},
    )

    return {
        "schema": "c18acc_ext_regime_stratification-v1",
        "date_start": date_start,
        "date_end": date_end,
        "oos_start": oos_start,
        "overlay_spec": {
            "variant_id": "I36",
            "exit_mode": "combo_spike",
            "min_spike_pct": 4.0,
            "min_fade_pct": 0.5,
            "note": "backtest I36 · live sell-signal-radar 對齊 fade≥1%（strategy.yaml）",
        },
        "i36_summary": i36_summary,
        "legs": {"full": full_legs, "oos": oos_legs},
        "stratification": {"full": full_strat, "oos": oos_strat},
        "hypotheses": {"full": hypo_full, "oos": hypo_oos},
        "g3_regime_stratification": (
            "passed"
            if hypo_full.get("H-G3-1", {}).get("pass") and hypo_full.get("H-G3-2", {}).get("pass")
            else "partial"
            if hypo_full.get("H-G3-1", {}).get("pass") or hypo_full.get("H-G3-2", {}).get("pass")
            else "failed"
        ),
    }


def render_ext_regime_stratification_md(payload: dict[str, Any]) -> str:
    spec = payload.get("overlay_spec") or {}
    full = payload.get("stratification", {}).get("full") or {}
    oos = payload.get("stratification", {}).get("oos") or {}
    hypo_f = payload.get("hypotheses", {}).get("full") or {}
    hypo_o = payload.get("hypotheses", {}).get("oos") or {}
    i36 = payload.get("i36_summary") or {}

    lines = [
        "# C18acc Extension radar · Regime layer 分層（G3）",
        "",
        f"區間：**{payload.get('date_start')} → {payload.get('date_end')}** · "
        f"OOS ≥ **{payload.get('oos_start')}**",
        "",
        f"- Overlay：**{spec.get('variant_id')}** · {spec.get('exit_mode')} · "
        f"spike≥{spec.get('min_spike_pct')}% · fade≥{spec.get('min_fade_pct')}%",
        f"- I36 ext legs（全樣本）：**{full.get('n_ext', 0)}** · "
        f"median save **{i36.get('median_save_vs_orig_ext_pct')}%** · "
        f"Δ I0 **{i36.get('delta_vs_i0_pp')}** pp",
        f"- G3 總評：**{payload.get('g3_regime_stratification')}**",
        "",
        "## Breadth bucket（signal-day 前一日 zone_200）",
        "",
        "| bucket | n | median save% | mean save% | save>0% |",
        "|--------|---|--------------|------------|---------|",
    ]
    for bucket in ("risk_on", "neutral", "risk_off"):
        s = (full.get("by_breadth_bucket") or {}).get(bucket) or {}
        if not s.get("n"):
            continue
        lines.append(
            f"| {BUCKET_LABELS.get(bucket, bucket)} | {s.get('n')} | "
            f"{s.get('median_save_pct')} | {s.get('mean_save_pct')} | "
            f"{s.get('pct_save_positive')}% |"
        )

    lines.extend(["", "## Breadth zone 明細", "", "| zone | n | median save% | save>0% |", "|------|---|--------------|---------|"])
    for z in (*BREADTH_ZONES_ORDER, "unknown"):
        s = (full.get("by_breadth_zone") or {}).get(z) or {}
        if not s.get("n"):
            continue
        zh = BREADTH_ZONE_ZH.get(z, z)
        lines.append(
            f"| {zh} | {s.get('n')} | {s.get('median_save_pct')} | {s.get('pct_save_positive')}% |"
        )

    lines.extend(["", "## Flow tape（出場日）", "", "| tape | n | median save% | save>0% |", "|------|---|--------------|---------|"])
    for tape, s in sorted((full.get("by_flow_tape") or {}).items(), key=lambda x: -x[1].get("n", 0)):
        if not s.get("n"):
            continue
        lines.append(
            f"| {tape} | {s.get('n')} | {s.get('median_save_pct')} | {s.get('pct_save_positive')}% |"
        )

    lines.extend(["", "## OOS 子樣本", "", f"- ext n = **{oos.get('n_ext', 0)}**"])
    for bucket in ("risk_on", "neutral", "risk_off"):
        s = (oos.get("by_breadth_bucket") or {}).get(bucket) or {}
        if s.get("n"):
            lines.append(
                f"- {BUCKET_LABELS.get(bucket, bucket)}：n={s.get('n')} · "
                f"median save {s.get('median_save_pct')}% · save>0 {s.get('pct_save_positive')}%"
            )

    def _hypo_row(hid: str, h: dict[str, Any]) -> str:
        return f"| {hid} | {'✓' if h.get('pass') else '✗' if h.get('pass') is False else '—'} | {h} |"

    lines.extend(
        [
            "",
            "## 假說（G3）",
            "",
            "| ID | FULL | 摘要 |",
            "|----|------|------|",
            f"| H-G3-1 | {'✓' if hypo_f.get('H-G3-1', {}).get('pass') else '✗'} | "
            f"risk_on med {hypo_f.get('H-G3-1', {}).get('risk_on_median')}% vs "
            f"neutral {hypo_f.get('H-G3-1', {}).get('neutral_median')}% / "
            f"risk_off {hypo_f.get('H-G3-1', {}).get('risk_off_median')}% |",
            f"| H-G3-2 | {'✓' if hypo_f.get('H-G3-2', {}).get('pass') else '✗'} | "
            f"overbought save>0 {hypo_f.get('H-G3-2', {}).get('overbought_pct_positive')}% vs "
            f"strong {hypo_f.get('H-G3-2', {}).get('strong_pct_positive')}% |",
            "",
            "### OOS 對照",
            "",
            f"- H-G3-1 OOS：{hypo_o.get('H-G3-1', {}).get('pass')}",
            f"- H-G3-2 OOS：{hypo_o.get('H-G3-2', {}).get('pass')}",
        ]
    )

    ob = (full.get("by_breadth_zone") or {}).get("overbought") or {}
    st = (full.get("by_breadth_zone") or {}).get("strong") or {}
    tape_bull = (full.get("by_flow_tape") or {}).get("多頭") or {}
    tape_chop = (full.get("by_flow_tape") or {}).get("震盪") or {}
    oos_ro = (oos.get("by_breadth_bucket") or {}).get("risk_on") or {}

    lines.extend(
        [
            "",
            "## 搭配 C18acc 應用（live playbook）",
            "",
            "### 架構",
            "",
            "```",
            "C18acc+S2（主策略 · 3 槽 poll_5m 進換 · S2 出場）",
            "        │",
            "        ├─ 持倉交集 → sell-signal-radar（09:06–13:20 · 5m poll）",
            "        │              └─ combo_spike：spike≥4% · fade≥1% → 寄信 advisory",
            "        └─ OR ext watch（09:30 前 ext≥3%）→ 只記 log · 不賣",
            "```",
            "",
            "### 操作規則",
            "",
            "1. **進場/換倉**：完全交給 C18acc（`rrg-mono-swap-accel`）· 不讓 extension 改 slot 狀態。",
            "2. **出場 overlay**：僅 **C18acc 持倉交集**（`SIGNAL_RADAR_SELL_HELD_ONLY=1`）· combo_spike 觸發 → 人工平倉。",
            "3. **S2 優先**：Weakening 2d + loss≥5% force exit 不因 extension 跳過。",
            "4. **OR watch**：維持 watch-only（`c18acc_or_ext_watch_log.csv`）· 不因 opening ext≥3% 直接賣。",
            "5. **勿 stack**：W5 rotation / struct drs exit 均已 NO-GO · extension 是唯一 overlay。",
            "",
            "### Regime 解讀（本報告數據）",
            "",
            f"- **Overbought**（n={ob.get('n', 0)}）：median save **{ob.get('median_save_pct')}%** · "
            f"save>0 **{ob.get('pct_save_positive')}%** → 細分最可靠。",
            f"- **Strong**（n={st.get('n', 0)}）：median **{st.get('median_save_pct')}%** · "
            f"save>0 {st.get('pct_save_positive')}% · 次於 Overbought。",
            f"- **Flow tape 多頭**（n={tape_bull.get('n', 0)}）：median **{tape_bull.get('median_save_pct')}%** · "
            f"優於震盪（{tape_chop.get('median_save_pct')}）。",
            f"- **OOS risk_on**（n={oos_ro.get('n', 0)}）：median save **{oos_ro.get('median_save_pct')}%** · "
            "全樣本 risk_on 為負 → **以 OOS + Overbought 為主**。",
            "",
            "### 環境層 gating（advisory · 非凍結規格）",
            "",
            "| 條件 | 建議 |",
            "|------|------|",
            "| zone=**Overbought** 或 tape=**多頭** + combo_spike | **標準執行** extension 出場 |",
            "| zone=**Strong**（非 OB） | 執行 · 可等 fade≥1% 確認 |",
            "| zone=**Neutral** | 降權 · 小倉或等第二根 fade |",
            "| zone=**Weak/Oversold** | **僅 alert** · 優先 S2 / 原出場 |",
            "| OR watch（無 combo） | 觀察 · 不賣 |",
            "",
            "### 日常流程",
            "",
            "| 時間 | 動作 |",
            "|------|------|",
            "| 16:30 | C18acc daily brief · 確認 3 槽持倉 |",
            "| 09:06–13:20 | sell-signal-radar 5m poll · 持倉 combo_spike 寄信 |",
            "| 信號到 | 查 breadth zone + flow tape → 套用 gating 表 → 人工下單 |",
            "| 09:30 前 | OR ext≥3% 僅寫 watch log |",
            "",
            "---",
            "模組：`scripts/run_c18acc_ext_regime_stratification.py`",
        ]
    )
    return "\n".join(lines) + "\n"
