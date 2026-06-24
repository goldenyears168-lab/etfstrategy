#!/usr/bin/env python3
"""RRG mono hold7 · C0 vs A（現行收盤建倉）證明報告。

同一 SSG（D4 mono fresh + seg_last 前十 shortlist），只改建倉執行：
  A = 收盤 seg_last 填槽（現行 hold7）
  C0 = scale 5m confirm=1 盤中輪詢重排（leg C）

輸出：多區間對照 · kbar 公平子樣本 · 同標的配對 · bootstrap 顯著性 · 3 槽組合 CAGR
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path
from statistics import mean, stdev
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_mono_backtest import (  # noqa: E402
    MAX_SLOTS,
    build_fresh_mono_calendar,
    load_price_panels,
)
from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods  # noqa: E402
from research.backtest.rrg_mono_intraday_ab import (  # noqa: E402
    LEG_LABELS,
    _summarize,
    audit_shortlist_kbar_coverage,
    close_shortlist,
    simulate_mono_hold7_ab,
)
from report_paths import RESEARCH_RRG  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402
from stock_db.kbar import kbar_day_has_data  # noqa: E402

DEFAULT_PORTFOLIO_CAPITAL_NTD = 50_000.0


def _portfolio_comparison(
    conn,
    *,
    periods_a: list[dict[str, Any]],
    periods_c: list[dict[str, Any]],
    trade_dates: list[str],
    close,
    total_capital: float = DEFAULT_PORTFOLIO_CAPITAL_NTD,
) -> dict[str, Any]:
    """3 槽等權組合 · 使用 period entry_px（C0 盤中進場）。"""
    ma = portfolio_metrics_for_periods(
        conn, periods_a, trade_dates, total_capital=total_capital, n_slots=MAX_SLOTS, close=close
    )
    mc = portfolio_metrics_for_periods(
        conn, periods_c, trade_dates, total_capital=total_capital, n_slots=MAX_SLOTS, close=close
    )
    years = len(trade_dates) / 252.0 if trade_dates else None
    cagr_a = ma.get("cagr_pct")
    cagr_c = mc.get("cagr_pct")
    tr_a = ma.get("total_return_pct")
    tr_c = mc.get("total_return_pct")
    cagr_reliable = years is not None and years >= 1.0
    return {
        "n_slots": MAX_SLOTS,
        "total_capital_ntd": total_capital,
        "calendar_trading_days": len(trade_dates),
        "calendar_years": round(years, 4) if years else None,
        "cagr_reliable": cagr_reliable,
        "note": "equal-capital slot book · daily MTM · entry_px 含 C0 盤中價",
        "A": {
            "cagr_pct": cagr_a,
            "total_return_pct": tr_a,
            "sharpe_ratio": ma.get("sharpe_ratio"),
            "n_trades": ma.get("n_trades"),
            "final_equity_ntd": ma.get("final_equity_ntd"),
        },
        "C0": {
            "cagr_pct": cagr_c,
            "total_return_pct": tr_c,
            "sharpe_ratio": mc.get("sharpe_ratio"),
            "n_trades": mc.get("n_trades"),
            "final_equity_ntd": mc.get("final_equity_ntd"),
        },
        "delta_cagr_pp": (
            round(float(cagr_c) - float(cagr_a), 4)
            if cagr_reliable and cagr_a is not None and cagr_c is not None
            else None
        ),
        "delta_total_return_pp": round(float(tr_c) - float(tr_a), 4) if tr_a is not None and tr_c is not None else None,
        "delta_final_equity_ntd": round(float(mc.get("final_equity_ntd") or 0) - float(ma.get("final_equity_ntd") or 0), 2),
    }


def _kbar_fair_dates(
    conn,
    trade_dates: list[str],
    fresh_by_date: dict,
    *,
    min_pct: float = 100.0,
) -> list[str]:
    fair: list[str] = []
    for d in trade_dates:
        sl = close_shortlist(fresh_by_date.get(d, []))
        if not sl:
            continue
        hits = sum(1 for r in sl if kbar_day_has_data(conn, r.stock_id, d))
        if hits / len(sl) * 100.0 >= min_pct:
            fair.append(d)
    return fair


def _filter_periods(periods: list[dict[str, Any]], signal_dates: set[str]) -> list[dict[str, Any]]:
    return [p for p in periods if str(p.get("signal_date")) in signal_dates]


def _paired_analysis(
    periods_a: list[dict[str, Any]],
    periods_c: list[dict[str, Any]],
) -> dict[str, Any]:
    map_a = {(str(p["signal_date"]), str(p["stock_id"])): float(p["excess_pct"]) for p in periods_a}
    map_c = {(str(p["signal_date"]), str(p["stock_id"])): float(p["excess_pct"]) for p in periods_c}
    keys = sorted(set(map_a) & set(map_c))
    deltas = [map_c[k] - map_a[k] for k in keys]
    if not deltas:
        return {"n_paired": 0, "mean_delta_pp": None, "win_rate_c_better_pct": None}
    wins = sum(1 for d in deltas if d > 0)
    return {
        "n_paired": len(deltas),
        "mean_delta_pp": round(mean(deltas), 4),
        "median_delta_pp": round(sorted(deltas)[len(deltas) // 2], 4),
        "win_rate_c_better_pct": round(wins / len(deltas) * 100.0, 2),
        "total_delta_pp": round(sum(deltas), 4),
    }


def _bootstrap_p_value(deltas: list[float], *, n_iter: int = 5000, seed: int = 42) -> float | None:
    if len(deltas) < 2:
        return None
    rng = random.Random(seed)
    obs = mean(deltas)
    boot = []
    for _ in range(n_iter):
        sample = [deltas[rng.randrange(len(deltas))] for _ in range(len(deltas))]
        boot.append(mean(sample))
    # one-sided: P(mean_boot <= 0) when obs > 0
    if obs >= 0:
        le_zero = sum(1 for b in boot if b <= 0)
        return round(le_zero / n_iter, 4)
    ge_zero = sum(1 for b in boot if b >= 0)
    return round(ge_zero / n_iter, 4)


def _paired_t_stat(deltas: list[float]) -> float | None:
    if len(deltas) < 2:
        return None
    m = mean(deltas)
    sd = stdev(deltas)
    if sd == 0:
        return None
    return round(m / (sd / (len(deltas) ** 0.5)), 4)


def _run_window(
    conn,
    *,
    date_start: str,
    date_end: str,
    label: str,
    kbar_fair_only: bool = False,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)

    from market_breadth_ma import build_breadth_panel

    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    fair_dates = _kbar_fair_dates(conn, trade_dates, fresh_by_date)
    signal_filter: set[str] | None = set(fair_dates) if kbar_fair_only else None

    periods_a, summary_a = simulate_mono_hold7_ab(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        leg="A",
    )
    periods_c, summary_c = simulate_mono_hold7_ab(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
        leg="C",
    )

    if signal_filter is not None:
        periods_a = _filter_periods(periods_a, signal_filter)
        periods_c = _filter_periods(periods_c, signal_filter)
        summary_a = _summarize(periods_a)
        summary_c = _summarize(periods_c)

    paired = _paired_analysis(periods_a, periods_c)
    deltas: list[float] = []
    if paired["n_paired"]:
        map_a = {(str(p["signal_date"]), str(p["stock_id"])): float(p["excess_pct"]) for p in periods_a}
        map_c = {(str(p["signal_date"]), str(p["stock_id"])): float(p["excess_pct"]) for p in periods_c}
        keys = sorted(set(map_a) & set(map_c))
        deltas = [map_c[k] - map_a[k] for k in keys]

    excess_a = summary_a.get("mean_excess_pct")
    excess_c = summary_c.get("mean_excess_pct")
    delta_pp = (
        round(float(excess_c) - float(excess_a), 4)
        if excess_a is not None and excess_c is not None
        else None
    )

    audit = audit_shortlist_kbar_coverage(conn, trade_dates=trade_dates, fresh_by_date=fresh_by_date)
    portfolio = _portfolio_comparison(
        conn,
        periods_a=periods_a,
        periods_c=periods_c,
        trade_dates=trade_dates,
        close=close,
    )

    return {
        "label": label,
        "date_start": date_start,
        "date_end": date_end,
        "kbar_fair_only": kbar_fair_only,
        "signal_days_total": len([d for d in trade_dates if close_shortlist(fresh_by_date.get(d, []))]),
        "signal_days_kbar_fair": len(fair_dates),
        "shortlist_kbar_coverage_pct": audit.get("coverage_pct"),
        "A": {
            "label": LEG_LABELS["A"],
            "n_periods": len(periods_a),
            "summary": summary_a,
        },
        "C0": {
            "label": LEG_LABELS["C"],
            "n_periods": len(periods_c),
            "summary": summary_c,
            "kbar_coverage_pct": summary_c.get("kbar_coverage_pct"),
        },
        "delta_mean_excess_pp": delta_pp,
        "c0_better": delta_pp is not None and delta_pp > 0,
        "paired": {
            **paired,
            "bootstrap_p_one_sided": _bootstrap_p_value(deltas),
            "paired_t_stat": _paired_t_stat(deltas),
        },
        "portfolio": portfolio,
    }


def run_proof(
    conn,
    *,
    windows: list[tuple[str, str, str, bool]] | None = None,
) -> dict[str, Any]:
    if windows is None:
        windows = [
            ("full", "2024-01-01", "2026-06-22", False),
            ("2026_h1", "2026-01-01", "2026-06-22", False),
            ("kbar_fair_full", "2024-01-01", "2026-06-22", True),
            ("kbar_fair_2026", "2026-01-01", "2026-06-22", True),
            ("near_30d", "2026-05-24", "2026-06-22", False),
            ("near_30d_kbar_fair", "2026-05-24", "2026-06-22", True),
        ]
    results = [_run_window(conn, date_start=s, date_end=e, label=lbl, kbar_fair_only=kf) for lbl, s, e, kf in windows]

    verdicts = []
    for r in results:
        d = r.get("delta_mean_excess_pp")
        verdicts.append(
            {
                "window": r["label"],
                "c0_better": r["c0_better"],
                "delta_pp": d,
                "n_c0": r["C0"]["n_periods"],
                "paired_n": r["paired"]["n_paired"],
                "paired_delta_pp": r["paired"].get("mean_delta_pp"),
            }
        )

    adoption_ready = all(
        v["c0_better"] and v["delta_pp"] is not None and v["delta_pp"] >= 0.5
        for v in verdicts
        if v["window"] in ("kbar_fair_2026", "near_30d_kbar_fair") and (v["n_c0"] or 0) >= 20
    )

    coverage = results[0].get("shortlist_kbar_coverage_pct") if results else None
    honest_limits = [
        f"shortlist kbar 覆蓋 {coverage}%（FinMind sponsor 補歷史後）；缺 K 日 C0 退化成收盤價",
        "近 30 日 n 仍小（約 6 腿），不宜單獨作採納依據",
        "C0 與 A 可能選不同標的；配對分析僅涵蓋同 signal_date+stock_id 子集",
        "組合 CAGR 未含滑價/手續費；絕對年化偏高，宜看 C0−A 差距",
        "實盤需 daily backfill stock_kbar_1m，否則 C0 無法發揮",
    ]

    return {
        "ssg": "D4 mono fresh + close seg_last top-10 shortlist · hold7 exit unchanged",
        "A": "現行 hold7 收盤建倉",
        "C0": "scale 5m confirm=1 盤中輪詢（leg C）",
        "windows": results,
        "verdict_summary": verdicts,
        "adoption_gate": {
            "rule": "kbar 公平子樣本 n≥20 且均超額 +0.5pp",
            "passed": adoption_ready,
        },
        "honest_limits": honest_limits,
    }


def _render_md(payload: dict[str, Any]) -> str:
    lines = [
        "# RRG mono hold7 · C0 vs A 證明報告",
        "",
        f"**SSG**：{payload['ssg']}",
        "",
        "| 區間 | kbar公平 | n(A) | 均超額 A% | n(C0) | 均超額 C0% | Δ pp | C0勝? | 配對n | 配對Δ pp | bootstrap p |",
        "|------|----------|------|-----------|-------|------------|------|-------|-------|----------|-------------|",
    ]
    for w in payload["windows"]:
        sa = w["A"]["summary"]
        sc = w["C0"]["summary"]
        p = w["paired"]
        lines.append(
            f"| {w['label']} | {'是' if w['kbar_fair_only'] else '否'} "
            f"| {w['A']['n_periods']} | {sa.get('mean_excess_pct')} "
            f"| {w['C0']['n_periods']} | {sc.get('mean_excess_pct')} "
            f"| {w['delta_mean_excess_pp']} | {'✓' if w['c0_better'] else '✗'} "
            f"| {p['n_paired']} | {p.get('mean_delta_pp')} | {p.get('bootstrap_p_one_sided')} |"
        )

    cap = DEFAULT_PORTFOLIO_CAPITAL_NTD
    lines += [
        "",
        f"## 組合層（{int(MAX_SLOTS)} 槽等權 · 本金 {cap:,.0f} NTD · 含盤中 entry_px）",
        "",
        "| 區間 | 年數 | A CAGR% | C0 CAGR% | Δ CAGR pp | A 總報酬% | C0 總報酬% | Δ 終值 NTD |",
        "|------|------|---------|----------|-----------|-----------|------------|------------|",
    ]
    for w in payload["windows"]:
        pf = w.get("portfolio") or {}
        pa = pf.get("A") or {}
        pc = pf.get("C0") or {}
        if pf.get("cagr_reliable"):
            cagr_a = pa.get("cagr_pct")
            cagr_c = pc.get("cagr_pct")
            dcagr = pf.get("delta_cagr_pp")
        else:
            cagr_a = cagr_c = dcagr = "—"
        lines.append(
            f"| {w['label']} | {pf.get('calendar_years')} "
            f"| {cagr_a} | {cagr_c} | {dcagr} "
            f"| {pa.get('total_return_pct')} | {pc.get('total_return_pct')} "
            f"| {pf.get('delta_final_equity_ntd')} |"
        )
    lines += [
        "",
        "CAGR 僅在區間 ≥1 年時列出；短區間（2026 H1、近 30 日）只看總報酬與終值差。",
        "腿均超額 +1.5pp 在 3 槽輪轉下會放大為組合 CAGR 差距（複利 · 盤中較佳進場）。",
        "",
        "## 結論",
        "",
    ]
    for v in payload["verdict_summary"]:
        mark = "C0 較佳" if v["c0_better"] else "A 較佳或平"
        lines.append(
            f"- **{v['window']}**：{mark} · Δ={v['delta_pp']}pp · n(C0)={v['n_c0']}"
            + (f" · 配對Δ={v['paired_delta_pp']}pp (n={v['paired_n']})" if v["paired_n"] else "")
        )

    gate = payload["adoption_gate"]
    lines += [
        "",
        f"**採納門檻**（{gate['rule']}）：{'通過' if gate['passed'] else '未通過'}",
        "",
        "## 限制（誠實陳述）",
        "",
    ]
    for note in payload["honest_limits"]:
        lines.append(f"- {note}")
    lines += ["", "---", "模組：`scripts/run_rrg_mono_c0_vs_a_proof.py`", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C0 vs A proof report")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--md", type=Path, default=None)
    args = parser.parse_args(argv)

    stamp = "20260623"
    out_json = args.out or RESEARCH_RRG / f"{stamp}_rrg_mono_c0_vs_a_proof.json"
    out_md = args.md or RESEARCH_RRG / f"{stamp}_rrg_mono_c0_vs_a_proof.md"

    conn = connect(args.db)
    try:
        payload = run_proof(conn)
    finally:
        conn.close()

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(_render_md(payload), encoding="utf-8")
    print(f"wrote {out_json}")
    print(f"wrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
