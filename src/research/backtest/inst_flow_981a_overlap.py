"""第五輪：sync_buy3 法人連買 × 00981A ETF flow · leg 重疊與互補性。"""

from __future__ import annotations

import sqlite3
from datetime import date

from .copytrade_backtest import (
    CopytradeRunResult,
    CopytradeSignal,
    group_signals_by_date,
    iter_copytrade_signals,
    select_executed_signal_days,
    simulate_capital_recycling,
    simulate_fixed_slots,
)
from .inst_flow_backtest import (
    ENTRY_LAG_DAYS,
    INST_FLOW_VERSION,
    _legs_per_day_stats,
    resolve_inst_flow_window,
    run_grouped_profiles_matrix,
    scan_inst_flow_signals,
)
from stock_db import ETF_CODES_INTRADAY_DEFAULT, load_etf_constituent_watchlist

DEFAULT_ETF_CODE = "00981A"
ROUND5_HORIZONS = (9, 12)
OVERLAP_BUCKET_SPECS: tuple[tuple[str, str], ...] = (
    ("981a_all", "00981A 跟單（全部新进/加码 leg）"),
    ("both", "交集 · 同日同股 inst∩981A"),
    ("inst_only", "法人獨有 · sync_buy3 無 981A 加碼"),
    ("etf_only", "981A 獨有 · 加碼無 sync_buy3"),
    ("union", "聯集 · 去重後合併 basket"),
)

LegKey = tuple[str, str]


def leg_keys(signals: list[CopytradeSignal]) -> set[LegKey]:
    return {(s.signal_date, s.stock_id) for s in signals}


def split_overlap_buckets(
    inst_signals: list[CopytradeSignal],
    etf_signals: list[CopytradeSignal],
) -> dict[str, list[CopytradeSignal]]:
    """依 (訊號日, 股號) 切分互補桶。"""
    inst_k = leg_keys(inst_signals)
    etf_k = leg_keys(etf_signals)
    both_k = inst_k & etf_k
    both = [s for s in inst_signals if (s.signal_date, s.stock_id) in both_k]
    inst_only = [s for s in inst_signals if (s.signal_date, s.stock_id) not in etf_k]
    etf_only = [s for s in etf_signals if (s.signal_date, s.stock_id) not in inst_k]
    union_by_key: dict[LegKey, CopytradeSignal] = {}
    for s in etf_signals:
        union_by_key[(s.signal_date, s.stock_id)] = s
    for s in inst_signals:
        union_by_key[(s.signal_date, s.stock_id)] = s
    return {
        "981a_all": list(etf_signals),
        "both": both,
        "inst_only": inst_only,
        "etf_only": etf_only,
        "union": list(union_by_key.values()),
    }


def compute_overlap_stats(
    inst_signals: list[CopytradeSignal],
    etf_signals: list[CopytradeSignal],
) -> dict[str, float | int]:
    buckets = split_overlap_buckets(inst_signals, etf_signals)
    inst_days = {s.signal_date for s in inst_signals}
    etf_days = {s.signal_date for s in etf_signals}
    both_days = inst_days & etf_days
    inst_only_days = inst_days - etf_days
    etf_only_days = etf_days - inst_days
    union_days = inst_days | etf_days
    n_inst = len(inst_signals)
    n_etf = len(etf_signals)
    n_both = len(buckets["both"])
    n_union = len(buckets["union"])
    return {
        "inst_legs": n_inst,
        "etf_legs": n_etf,
        "both_legs": n_both,
        "inst_only_legs": len(buckets["inst_only"]),
        "etf_only_legs": len(buckets["etf_only"]),
        "union_legs": n_union,
        "leg_jaccard_pct": round(100.0 * n_both / n_union, 1) if n_union else 0.0,
        "inst_overlap_pct": round(100.0 * n_both / n_inst, 1) if n_inst else 0.0,
        "etf_overlap_pct": round(100.0 * n_both / n_etf, 1) if n_etf else 0.0,
        "inst_days": len(inst_days),
        "etf_days": len(etf_days),
        "both_days": len(both_days),
        "inst_only_days": len(inst_only_days),
        "etf_only_days": len(etf_only_days),
        "union_days": len(union_days),
        "day_jaccard_pct": round(100.0 * len(both_days) / len(union_days), 1) if union_days else 0.0,
    }


def _day_result_dicts(day_results: list) -> list[dict]:
    return [
        {
            "signal_date": d.signal_date,
            "entry_date": d.entry_date,
            "exit_date": d.exit_date,
            "alpha_ntd": d.alpha_ntd,
            "pnl_ntd": d.pnl_ntd,
            "status": d.status,
            "source": getattr(d, "source", None),
        }
        for d in day_results
        if d.status == "complete"
    ]


def simulate_dual_track_allocation(
    conn: sqlite3.Connection,
    inst_result: CopytradeRunResult,
    etf_result: CopytradeRunResult,
    *,
    n_slots: int,
    capital_ntd: float,
) -> dict[str, float | int | None]:
    """合併兩軌訊號日，依 entry_date 排序做槽位模擬。"""
    merged: list[dict] = []
    for d in inst_result.signal_days:
        if d.status != "complete":
            continue
        merged.append(
            {
                "signal_date": d.signal_date,
                "entry_date": d.entry_date,
                "exit_date": d.exit_date,
                "alpha_ntd": d.alpha_ntd,
                "pnl_ntd": d.pnl_ntd,
                "status": d.status,
                "source": "inst",
            }
        )
    for d in etf_result.signal_days:
        if d.status != "complete":
            continue
        merged.append(
            {
                "signal_date": d.signal_date,
                "entry_date": d.entry_date,
                "exit_date": d.exit_date,
                "alpha_ntd": d.alpha_ntd,
                "pnl_ntd": d.pnl_ntd,
                "status": d.status,
                "source": "etf",
            }
        )
    merged.sort(key=lambda x: (str(x["entry_date"]), str(x["signal_date"]), str(x.get("source"))))
    if n_slots <= 1:
        sim = simulate_capital_recycling(conn, merged, capital_ntd=capital_ntd)
        return {**sim, "n_slots": n_slots, "model": "single_pool"}
    executed, slot_meta = select_executed_signal_days(merged, n_slots=n_slots)
    total_alpha = sum(float(d.get("alpha_ntd") or 0) for d in executed)
    total_pnl = sum(float(d.get("pnl_ntd") or 0) for d in executed)
    return {
        "model": f"{n_slots}_slot",
        "n_slots": n_slots,
        "n_signals": len(merged),
        "recycled_n_cycles": len(executed),
        "recycled_total_alpha_ntd": round(total_alpha, 2),
        "recycled_total_pnl_ntd": round(total_pnl, 2),
        "signal_capture_pct": slot_meta.get("signal_capture_pct"),
        "peak_concurrent_slots": slot_meta.get("peak_concurrent_slots"),
        "n_skipped": slot_meta.get("n_skipped"),
    }


def run_inst_flow_round5(
    conn: sqlite3.Connection,
    *,
    etf_code: str = DEFAULT_ETF_CODE,
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    top_k: int = 10,
    horizons: tuple[int, ...] = ROUND5_HORIZONS,
    window_start: str | None = None,
    window_end: str | None = None,
    batch_id: str | None = None,
) -> dict[str, object]:
    from .inst_flow_backtest import SIGNAL_PROFILES

    profile = next(p for p in SIGNAL_PROFILES if p.profile_id == "sync_buy3")
    watchlist = load_etf_constituent_watchlist(conn, ETF_CODES_INTRADAY_DEFAULT)
    stock_ids = [w["stock_id"] for w in watchlist]
    name_by_id = {w["stock_id"]: w.get("stock_name") or w["stock_id"] for w in watchlist}
    w_start, w_end = resolve_inst_flow_window(
        conn, stock_ids, window_start=window_start, window_end=window_end
    )

    inst_signals = scan_inst_flow_signals(
        conn,
        profile=profile,
        stock_ids=stock_ids,
        name_by_id=name_by_id,
        window_start=w_start,
        window_end=w_end,
        top_k=top_k,
    )
    etf_signals = iter_copytrade_signals(
        conn,
        etf_code,
        window_start=w_start,
        window_end=w_end,
    )
    overlap_stats = compute_overlap_stats(inst_signals, etf_signals)
    buckets = split_overlap_buckets(inst_signals, etf_signals)
    run_profiles = [
        (bid, label, group_signals_by_date(buckets[bid]))
        for bid, label in OVERLAP_BUCKET_SPECS
    ]
    bid = batch_id or f"inst-flow-r5-{date.today().strftime('%Y%m%d')}"
    bucket_results = run_grouped_profiles_matrix(
        conn,
        run_profiles,
        horizons=horizons,
        capital_ntd=capital_ntd,
        cost_bps=cost_bps,
        window_start=w_start,
        window_end=w_end,
        batch_id=bid,
        etf_code=etf_code,
    )
    by_id = {r.strategy_id: r for r in bucket_results}

    dual_models: dict[str, dict] = {}
    for h in horizons:
        both_r = by_id.get(f"both-L1H{h}")
        etf_r = by_id.get(f"981a_all-L1H{h}")
        if both_r is None or etf_r is None:
            continue
        both_dicts = _day_result_dicts(both_r.signal_days)
        etf_dicts = _day_result_dicts(etf_r.signal_days)
        dual_models[f"both_h{h}_1pool"] = {
            **simulate_capital_recycling(conn, both_dicts, capital_ntd=capital_ntd),
            "model": "both_1pool",
        }
        dual_models[f"981a_all_h{h}_1pool"] = {
            **simulate_capital_recycling(conn, etf_dicts, capital_ntd=capital_ntd),
            "model": "981a_1pool",
        }
        dual_models[f"merged_h{h}_2slot"] = simulate_dual_track_allocation(
            conn, both_r, etf_r, n_slots=2, capital_ntd=capital_ntd
        )
        dual_models[f"both_h{h}_fixed2"] = simulate_fixed_slots(
            conn, both_dicts, n_slots=2, capital_ntd=capital_ntd
        )
        dual_models[f"981a_all_h{h}_fixed2"] = simulate_fixed_slots(
            conn, etf_dicts, n_slots=2, capital_ntd=capital_ntd
        )

    leg_stats = {
        bid: _legs_per_day_stats(group_signals_by_date(buckets[bid]))
        for bid, _ in OVERLAP_BUCKET_SPECS
    }
    return {
        "batch_id": bid,
        "window_start": w_start,
        "window_end": w_end,
        "universe_n": len(stock_ids),
        "top_k": top_k,
        "horizons": horizons,
        "capital_ntd": capital_ntd,
        "overlap_stats": overlap_stats,
        "bucket_results": bucket_results,
        "leg_stats": leg_stats,
        "dual_models": dual_models,
    }


def format_inst_flow_round5_report(payload: dict[str, object]) -> str:
    overlap = payload["overlap_stats"]
    assert isinstance(overlap, dict)
    horizons = payload["horizons"]
    assert isinstance(horizons, tuple)
    results: list[CopytradeRunResult] = payload["bucket_results"]  # type: ignore[assignment]
    by_id = {r.strategy_id: r for r in results}
    leg_stats: dict[str, dict] = payload["leg_stats"]  # type: ignore[assignment]
    dual: dict[str, dict] = payload["dual_models"]  # type: ignore[assignment]

    lines = [
        "# 法人連買第五輪：sync_buy3 × 00981A · leg 重疊與互補性",
        "",
        f"> {INST_FLOW_VERSION} · batch `{payload['batch_id']}` · "
        f"universe **{payload['universe_n']}** 檔 · Top-{payload['top_k']}/日 · "
        f"每日 {float(payload['capital_ntd']):,.0f} NTD",
        "",
        "## 第五輪設計",
        "",
        "- **法人軌**：`sync_buy3` + Top-K（外資5日累計排序）",
        "- **ETF 軌**：00981A 全部新进/加码 leg（與 copytrade 主研究一致）",
        "- **互補桶**：both / inst_only / etf_only / union",
        f"- **H 焦點**：{', '.join(f'H{h}' for h in horizons)}（承接第四輪 Optimal hold (H*)）",
        "- **雙槽**：inst∩981A 與 981A 全 basket 合併時間軸 · 2 槽輪動",
        "",
        "## Leg 重疊率",
        "",
        "| 指標 | 數值 |",
        "|------|------|",
        f"| 法人 leg | {overlap['inst_legs']} |",
        f"| 981A leg | {overlap['etf_legs']} |",
        f"| 交集 both | {overlap['both_legs']} |",
        f"| 法人獨有 | {overlap['inst_only_legs']} |",
        f"| 981A 獨有 | {overlap['etf_only_legs']} |",
        f"| 聯集（去重） | {overlap['union_legs']} |",
        f"| Leg Jaccard | {overlap['leg_jaccard_pct']}% |",
        f"| 法人中被 981A 覆蓋 | {overlap['inst_overlap_pct']}% |",
        f"| 981A 中被法人覆蓋 | {overlap['etf_overlap_pct']}% |",
        "",
        "## 訊號日曆重疊",
        "",
        "| 指標 | 數值 |",
        "|------|------|",
        f"| 法人訊號日 | {overlap['inst_days']} |",
        f"| 981A 訊號日 | {overlap['etf_days']} |",
        f"| 同日皆有活動 | {overlap['both_days']} |",
        f"| 僅法人 | {overlap['inst_only_days']} |",
        f"| 僅 981A | {overlap['etf_only_days']} |",
        f"| 日曆 Jaccard | {overlap['day_jaccard_pct']}% |",
        "",
        "## 互補桶訊號密度",
        "",
        "| bucket | 訊號日 | 總 leg | 日均 leg |",
        "|--------|--------|--------|----------|",
    ]
    for bid, label in OVERLAP_BUCKET_SPECS:
        st = leg_stats.get(bid, {})
        lines.append(
            f"| `{bid}` | {st.get('n_days', 0)} | {st.get('n_signals', 0)} | "
            f"{st.get('avg_legs', 0)} |"
        )

    lines.extend(["", "## 分桶回測（L1）", ""])
    for h in horizons:
        lines.append(f"### H{h}")
        lines.append("")
        lines.append("| bucket | 訊號日 | 勝率% | 累計α | Wilcoxon p |")
        lines.append("|--------|--------|---------|-------|------------|")
        for bid, _ in OVERLAP_BUCKET_SPECS:
            r = by_id.get(f"{bid}-L1H{h}")
            if r is None:
                continue
            wr = getattr(r, "win_rate_vs_bench_pct", r.win_rate_pct)
            p_w = r.p_value_wilcoxon
            p_txt = f"{p_w:.4f}" if p_w is not None else "—"
            star = "*" if p_w is not None and p_w < 0.05 else ""
            lines.append(
                f"| `{bid}` | {r.n_complete_days} | {wr:.2f}% | "
                f"{r.total_alpha_ntd:+,.0f} | {p_txt}{star} |"
            )
        lines.append("")

    lines.extend(["", "## 雙軌資金配置", ""])
    for h in horizons:
        lines.append(f"### H{h}")
        lines.append("")
        lines.append("| 模型 | 實現超額 | 成交筆數 | 捕獲% | 峰值槽 |")
        lines.append("|------|-------|----------|-------|--------|")
        for key, title in (
            (f"both_h{h}_1pool", "inst∩981A · 單池 1 槽"),
            (f"981a_all_h{h}_1pool", "981A 全 basket · 單池 1 槽"),
            (f"merged_h{h}_2slot", "inst∩981A + 981A 全 · 合併 2 槽"),
            (f"both_h{h}_fixed2", "inst∩981A · 固定 2 槽"),
            (f"981a_all_h{h}_fixed2", "981A 全 · 固定 2 槽"),
        ):
            m = dual.get(key)
            if not m:
                continue
            peak = m.get("peak_concurrent_slots", "—")
            lines.append(
                f"| {title} | {m.get('recycled_total_alpha_ntd', 0):+,.0f} | "
                f"{m.get('recycled_n_cycles', 0)} | "
                f"{m.get('signal_capture_pct', '—')}% | {peak} |"
            )
        lines.append("")

    lines.extend(
        [
            "## 解讀",
            "",
            f"- 資料窗：**{payload['window_start']}** ～ **{payload['window_end']}**",
            "- `both` 與第四輪 `sync_buy3+00981a` 同義（leg 級交集）",
            "- `inst_only` / `etf_only` 衡量兩軌**互補**而非重複訊號",
            "- 若 `etf_only` α 仍顯著，981A 跟單在法人未確認時仍有獨立邊際",
            "- 2 槽合併適用於同時運行兩策略且資金可並行部署的情境",
            "",
        ]
    )
    return "\n".join(lines)
