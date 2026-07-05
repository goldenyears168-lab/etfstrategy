"""C18acc × Extension radar · 持倉提早出場 overlay 回測。

SSG：C18acc champion 進場 / 換倉 / max_hold 不變。
Overlay：持倉期間若盤中延伸雷達觸發 → 當日 poll 價提早平倉。

對照 quantive3/vwap-reclaim · VWAP σ 帶 · B combo climax fade。
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Literal

import pandas as pd

from analytics.bench import bench_return_entry_to_exit
from intraday_extension_radar import (
    ExitMode,
    ExtensionScanConfig,
    SessionScan,
    pick_exit_for_mode,
    scan_session,
)
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_intraday_exit import _trading_days_between
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import kbar_day_has_data, load_kbar_day_bars

ExtensionOverlayMode = ExitMode


@dataclass(frozen=True)
class ExtensionOverlayConfig:
    variant_id: str
    label: str
    exit_mode: ExtensionOverlayMode = "none"
    min_hold_days: int = 5
    poll_interval_min: int = 5
    heat_threshold: int = 70
    slip_pct: float = 0.5
    min_ext_prev_pct: float = 0.0
    require_2sigma_exit: bool = False

    def to_scan_config(self) -> ExtensionScanConfig:
        return ExtensionScanConfig(
            exit_mode=self.exit_mode,
            heat_threshold=self.heat_threshold,
            poll_interval_min=self.poll_interval_min,
            min_ext_prev_pct=self.min_ext_prev_pct,
            require_2sigma_exit=self.require_2sigma_exit,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_EXTENSION_OVERLAY_SWEEP: list[ExtensionOverlayConfig] = [
    ExtensionOverlayConfig("X0", "C18acc 基線 · 無延伸 overlay", "none"),
    ExtensionOverlayConfig(
        "X1",
        "A · VWAP 2σ / MA20 過熱 · yellow 5m",
        "yellow_vwap",
    ),
    ExtensionOverlayConfig(
        "X2",
        "A · red climax · 5m",
        "red_climax",
    ),
    ExtensionOverlayConfig(
        "X3",
        "B · opening spike + climax + VWAP loss · combo",
        "combo_b",
    ),
    ExtensionOverlayConfig(
        "X4",
        "A+B · heat≥70 · 5m",
        "heat_threshold",
        heat_threshold=70,
    ),
    ExtensionOverlayConfig(
        "X5",
        "A · stretch→reclaim VWAP（quantive3 風格）",
        "stretch_reclaim",
    ),
]

PHASE_2C_EXTENSION_SWEEP: list[ExtensionOverlayConfig] = [
    ExtensionOverlayConfig("X0", "C18acc 基線 · 無延伸 overlay", "none"),
    ExtensionOverlayConfig(
        "X4",
        "legacy · heat≥70 · 5m",
        "heat_threshold",
        heat_threshold=70,
    ),
    ExtensionOverlayConfig(
        "X3",
        "B · combo_b · opening spike",
        "combo_b",
        min_ext_prev_pct=5.0,
    ),
    ExtensionOverlayConfig(
        "X6",
        "Phase2b · climax+2σ sell-strength · alert",
        "climax_sell_strength",
        min_ext_prev_pct=5.0,
        require_2sigma_exit=True,
    ),
    ExtensionOverlayConfig(
        "X7",
        "Phase2b · spike climax 2σ · confirm fade/VWAP",
        "spike_climax_sigma",
        min_ext_prev_pct=5.0,
    ),
    ExtensionOverlayConfig(
        "X8",
        "Phase2b · post_peak VWAP · confirm",
        "post_peak_vwap",
        min_ext_prev_pct=5.0,
    ),
]

HYPOTHESES: dict[str, str] = {
    "H-EXT-1": "X3 combo_b 在 spike-fade 日子集均 save > 0（3711 型）",
    "H-EXT-2": "X4 heat≥70 觸發次數少但 save 中位 > X0",
    "H-EXT-3": "X1 yellow 全樣本均超額 ≥ X0（不傷 C18acc alpha）",
    "H-EXT-4": "X5 stretch_reclaim 勝率 > X2 red_climax",
    "H-EXT-5": "延伸 overlay 僅在 ext_prev≥5% 日子集改善 save",
}


def _prior_close(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    full_dates: list[str],
) -> float | None:
    prior = [d for d in full_dates if d < trade_date]
    if not prior:
        return None
    prev_d = prior[-1]
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date = ?
        """,
        (stock_id, prev_d),
    ).fetchone()
    return float(row[0]) if row else None


def _ma20(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    full_dates: list[str],
) -> float | None:
    dates = [d for d in full_dates if d <= trade_date][-20:]
    if len(dates) < 10:
        return None
    rows = conn.execute(
        f"""
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date IN ({",".join("?" * len(dates))})
        ORDER BY trade_date
        """,
        (stock_id, *dates),
    ).fetchall()
    if len(rows) < 10:
        return None
    return sum(float(r[0]) for r in rows) / len(rows)


def _hold_dates(full_dates: list[str], entry: str, last: str) -> list[str]:
    if entry not in full_dates or last not in full_dates:
        return []
    i0 = full_dates.index(entry)
    i1 = full_dates.index(last)
    return full_dates[i0 + 1 : i1 + 1]


def _apply_slip(px: float, slip_pct: float) -> float:
    return px * (1.0 - slip_pct / 100.0)


def apply_extension_overlay_to_periods(
    conn: sqlite3.Connection,
    *,
    base_periods: list[dict[str, Any]],
    full_dates: list[str],
    config: ExtensionOverlayConfig,
    kbar_cache: dict[tuple[str, str], tuple] | None = None,
    ma20_cache: dict[tuple[str, str], float | None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if config.exit_mode == "none":
        summary = summarize_periods(base_periods)
        if summary.get("mean_return_pct") is not None and summary.get("mean_bench_pct") is not None:
            summary["mean_excess_pct"] = round(
                float(summary["mean_return_pct"]) - float(summary["mean_bench_pct"]), 4
            )
        summary.update(
            {
                "variant_id": config.variant_id,
                "label": config.label,
                "exit_mode": config.exit_mode,
                "overlay_triggers": 0,
                "kbar_checks": 0,
                "kbar_hits": 0,
            }
        )
        return base_periods, summary

    cache = kbar_cache if kbar_cache is not None else {}
    ma_cache = ma20_cache if ma20_cache is not None else {}
    out: list[dict[str, Any]] = []
    triggers = 0
    kbar_checks = 0
    kbar_hits = 0
    save_deltas: list[float] = []
    spike_save_deltas: list[float] = []

    for pos in base_periods:
        sid = str(pos["stock_id"])
        entry = str(pos["entry_date"])
        orig_exit = str(pos["exit_date"])
        entry_px = float(pos["entry_px"])
        orig_exit_px = float(pos["exit_px"])
        orig_ret = float(pos["return_pct"])

        hold_days = _trading_days_between(full_dates, entry, orig_exit)
        if hold_days < config.min_hold_days:
            out.append(dict(pos))
            continue

        early_exit_d: str | None = None
        early_px: float | None = None
        early_minute: str | None = None
        early_reason = config.exit_mode
        trigger_spike_day = False

        for d in _hold_dates(full_dates, entry, orig_exit):
            if _trading_days_between(full_dates, entry, d) < config.min_hold_days:
                continue
            kbar_checks += 1
            key = (sid, d)
            if key not in cache:
                if not kbar_day_has_data(conn, sid, d):
                    cache[key] = ()
                else:
                    cache[key] = load_kbar_day_bars(conn, sid, d)
                    kbar_hits += 1
            bars = cache[key]
            if not bars:
                continue
            prev_close = _prior_close(conn, sid, d, full_dates)
            if prev_close is None:
                continue
            ma_key = (sid, d)
            if ma_key not in ma_cache:
                ma_cache[ma_key] = _ma20(conn, sid, d, full_dates)
            scan = scan_session(
                bars,
                trade_date=d,
                stock_id=sid,
                prev_close=prev_close,
                ma20=ma_cache[ma_key],
                config=config.to_scan_config(),
            )
            sig = pick_exit_for_mode(scan, config.exit_mode)
            if sig is None:
                continue
            early_exit_d = d
            early_px = _apply_slip(sig.price, config.slip_pct)
            early_minute = sig.minute
            triggers += 1
            if scan.bars:
                peak_snap = max(scan.bars, key=lambda b: b.close)
                trigger_spike_day = peak_snap.ext_prev_pct >= 5.0
            break

        if early_exit_d and early_px:
            ret = (early_px / entry_px - 1.0) * 100.0
            bench = bench_return_entry_to_exit(conn, entry, early_exit_d, entry_price_mode="close")
            if bench is None:
                out.append(dict(pos))
                continue
            save = (orig_exit_px / entry_px - 1.0) * 100.0 - ret
            save_deltas.append(save)
            if trigger_spike_day:
                spike_save_deltas.append(save)
            leg = dict(pos)
            leg.update(
                {
                    "exit_date": early_exit_d,
                    "exit_px": round(early_px, 4),
                    "exit_minute": early_minute,
                    "exit_reason": f"ext_{early_reason}",
                    "hold_days": _trading_days_between(full_dates, entry, early_exit_d),
                    "return_pct": round(ret, 4),
                    "bench_return_pct": round(bench, 4),
                    "excess_pct": round(ret - bench, 4),
                    "beat_bench": ret > bench,
                    "gross_win": ret > 0,
                    "overlay_variant": config.variant_id,
                    "orig_exit_date": orig_exit,
                    "orig_exit_px": orig_exit_px,
                    "orig_return_pct": orig_ret,
                    "save_vs_orig_pct": round(save, 4),
                }
            )
            out.append(leg)
        else:
            out.append(dict(pos))

    summary = summarize_periods(out)
    if summary.get("mean_return_pct") is not None and summary.get("mean_bench_pct") is not None:
        summary["mean_excess_pct"] = round(
            float(summary["mean_return_pct"]) - float(summary["mean_bench_pct"]), 4
        )
    summary.update(
        {
            "variant_id": config.variant_id,
            "label": config.label,
            "exit_mode": config.exit_mode,
            "overlay_triggers": triggers,
            "kbar_checks": kbar_checks,
            "kbar_hits": kbar_hits,
            "mean_save_vs_orig_pct": round(sum(save_deltas) / len(save_deltas), 4)
            if save_deltas
            else None,
            "median_save_vs_orig_pct": round(sorted(save_deltas)[len(save_deltas) // 2], 4)
            if save_deltas
            else None,
            "spike_overlay_triggers": len(spike_save_deltas),
            "mean_save_spike_pct": round(sum(spike_save_deltas) / len(spike_save_deltas), 4)
            if spike_save_deltas
            else None,
            "median_save_spike_pct": round(
                sorted(spike_save_deltas)[len(spike_save_deltas) // 2], 4
            )
            if spike_save_deltas
            else None,
        }
    )
    return out, summary


def run_c18acc_extension_exit_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    configs: list[ExtensionOverlayConfig] | None = None,
) -> dict[str, Any]:
    from market_breadth_ma import build_breadth_panel

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champion = champion_score_swap_c_config()

    from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP

    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    base_periods, base_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=champion,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        entry_c_config=c0_cfg,
    )

    grid = configs or DEFAULT_EXTENSION_OVERLAY_SWEEP
    kbar_cache: dict = {}
    ma_cache: dict = {}
    summaries: list[dict[str, Any]] = []
    for cfg in grid:
        _, summary = apply_extension_overlay_to_periods(
            conn,
            base_periods=base_periods,
            full_dates=full_dates,
            config=cfg,
            kbar_cache=kbar_cache,
            ma20_cache=ma_cache,
        )
        baseline_ex = base_summary.get("mean_excess_pct")
        if baseline_ex is not None and summary.get("mean_excess_pct") is not None:
            summary["delta_vs_baseline_pp"] = round(
                float(summary["mean_excess_pct"]) - float(baseline_ex), 4
            )
        summaries.append(summary)

    ranked = sorted(
        summaries,
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("overlay_triggers") or 0)),
    )
    best = ranked[0] if ranked else None

    return {
        "date_start": date_start,
        "date_end": date_end,
        "champion_variant": champion.variant_id,
        "baseline_summary": base_summary,
        "hypotheses": HYPOTHESES,
        "summaries": summaries,
        "best": best,
        "research_plan": _research_plan_text(),
    }


def case_study_session(
    conn: sqlite3.Connection,
    *,
    stock_id: str,
    trade_date: str,
) -> dict[str, Any]:
    """單日 case study · 如 3711 spike-fade。"""
    full_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_daily_bars ORDER BY trade_date"
        ).fetchall()
    ]
    prev_close = _prior_close(conn, stock_id, trade_date, full_dates)
    if prev_close is None:
        return {"error": "no prev_close"}
    ma20 = _ma20(conn, stock_id, trade_date, full_dates)
    bars = load_kbar_day_bars(conn, stock_id, trade_date)
    scan = scan_session(
        bars,
        trade_date=trade_date,
        stock_id=stock_id,
        prev_close=prev_close,
        ma20=ma20,
    )
    peak = max(scan.bars, key=lambda b: b.close) if scan.bars else None
    return {
        "stock_id": stock_id,
        "trade_date": trade_date,
        "prev_close": prev_close,
        "ma20": ma20,
        "session_high": peak.close if peak else None,
        "session_high_minute": peak.minute if peak else None,
        "peak_ext_prev_pct": peak.ext_prev_pct if peak else None,
        "peak_heat": peak.heat_score if peak else None,
        "signals": {
            mode: (
                {
                    "minute": getattr(sig, "minute", None),
                    "price": getattr(sig, "price", None),
                    "heat": getattr(sig, "heat_score", None),
                    "flags": list(getattr(sig, "flags", ()) or ()),
                }
                if (sig := pick_exit_for_mode(scan, mode))
                else None
            )
            for mode in (
                "yellow_vwap",
                "red_climax",
                "combo_b",
                "heat_threshold",
                "stretch_reclaim",
                "climax_sell_strength",
                "spike_climax_sigma",
                "post_peak_vwap",
            )
        },
    }


def _research_plan_text() -> str:
    return """
## C18acc × Extension Radar 研究計畫

### 目標
在 **C18acc champion 進場/換倉不變** 前提下，驗證盤中延伸 overlay 能否在
spike-fade 日（如 3711）**提早賣出 save 為正**，且全樣本不大幅傷均超額。

### Phase 0 · 基線（已完成模組）
- `intraday_extension_radar.py`：A/B 特徵 + 5m poll
- `run_c18acc_extension_exit_sweep.py`：X0–X5 overlay sweep

### Phase 1 · 全樣本 sweep（G1）
- 區間：2024-01-01 .. 2026-06-26 · champion C18acc periods
- 主指標：mean_excess_pct · overlay_triggers · median_save_vs_orig
- 子樣本：ext_prev≥5% 日 · combo_b 觸發日

### Phase 2 · 校準機率帶（G2）
- 對 X1/X3/X4 觸發後 30m / 收盤 fade 比例 → 填 🟢🟡🔴 歷史 P
- Bootstrap CI on save_vs_orig

### Phase 3 · 與 playbook 合併（G3）
- Extension overlay = **獲利了結**（tier=strong 持倉）
- playbook S1/S2 = **結構/環境停損**
- 09:06–09:30 改 1m poll（spike 時段）

### Phase 4 · graduation（G5）
- 若 X3 或 X4 在 spike 子樣本 save>0 且全樣本 Δexcess≥-0.3pp
  → 写入 config/order.yaml extension_exit 段 + live dry-run

### 3711 驗證標準（case study）
- 09:17 @692：heat≥70 · climax · yellow/red
- combo_b 應在尖峰後 15m 內 VWAP loss 觸發
- save vs 持倉至收盤：>+3%
""".strip()


def apply_rotation_exit_overlay(
    conn: sqlite3.Connection,
    *,
    base_periods: list[dict[str, Any]],
    full_dates: list[str],
    close: pd.DataFrame,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    min_hold_days: int = 5,
    target_quad: str = "lagging",
    variant_id: str = "C1",
    label: str = "force exit on lagging after min_hold",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Track C · close exit when held enters target RRG quadrant (after min_hold)."""
    from research.backtest.c18acc_swap_block_audit import _stock_end_q
    from research.backtest.rrg_mono_score_swap_c import _trading_days_between

    out: list[dict[str, Any]] = []
    triggers = 0
    save_deltas: list[float] = []
    tgt = target_quad.lower()

    for pos in base_periods:
        sid = str(pos["stock_id"])
        entry = str(pos["entry_date"])
        orig_exit = str(pos["exit_date"])
        entry_px = float(pos["entry_px"])
        orig_exit_px = float(pos["exit_px"])
        orig_ret = float(pos["return_pct"])

        early_exit_d: str | None = None
        early_px: float | None = None

        for d in _hold_dates(full_dates, entry, orig_exit):
            if _trading_days_between(full_dates, entry, d) < min_hold_days:
                continue
            q = _stock_end_q(rs_ratio, rs_mom, full_dates, d, sid)
            if q != tgt:
                continue
            if d not in close.index or sid not in close.columns:
                continue
            px = float(close.at[d, sid])
            if px != px or px <= 0:
                continue
            early_exit_d = d
            early_px = px
            triggers += 1
            break

        if early_exit_d and early_px:
            ret = (early_px / entry_px - 1.0) * 100.0
            bench = bench_return_entry_to_exit(conn, entry, early_exit_d, entry_price_mode="close")
            if bench is None:
                out.append(dict(pos))
                continue
            save = (orig_exit_px / entry_px - 1.0) * 100.0 - ret
            save_deltas.append(save)
            leg = dict(pos)
            leg.update(
                {
                    "exit_date": early_exit_d,
                    "exit_px": round(early_px, 4),
                    "exit_reason": f"rot_{tgt}",
                    "hold_days": _trading_days_between(full_dates, entry, early_exit_d),
                    "return_pct": round(ret, 4),
                    "bench_return_pct": round(bench, 4),
                    "excess_pct": round(ret - bench, 4),
                    "beat_bench": ret > bench,
                    "gross_win": ret > 0,
                    "overlay_variant": variant_id,
                    "orig_exit_date": orig_exit,
                    "orig_exit_px": orig_exit_px,
                    "orig_return_pct": orig_ret,
                    "save_vs_orig_pct": round(save, 4),
                }
            )
            out.append(leg)
        else:
            out.append(dict(pos))

    summary = summarize_periods(out)
    if summary.get("mean_return_pct") is not None and summary.get("mean_bench_pct") is not None:
        summary["mean_excess_pct"] = round(
            float(summary["mean_return_pct"]) - float(summary["mean_bench_pct"]), 4
        )
    summary.update(
        {
            "variant_id": variant_id,
            "label": label,
            "exit_mode": f"rot_{tgt}",
            "overlay_triggers": triggers,
            "median_save_vs_orig_pct": round(float(pd.Series(save_deltas).median()), 4)
            if save_deltas
            else None,
        }
    )
    return out, summary
