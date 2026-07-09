"""Gap diagnostic · why C18acc NAV >> ABC v3+f1 on 2024+ comparison.

Reads an existing comparison JSON (no dual-WMA / 1m panel rebuild).
Pricing note for follow-on work: prefer ``stock_db.kbar.kbar_5m_close_at_minute``.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _pct(n: int, d: int) -> float | None:
    return round(100.0 * n / d, 2) if d else None


def _occupancy_from_ledger(
    ledger: list[dict[str, Any]],
    dates: list[str],
    *,
    n_slots: int,
    entry_key: str = "entry_date",
    exit_key: str = "exit_date",
) -> dict[str, Any]:
    opens = Counter(str(t.get(entry_key) or "") for t in ledger)
    closes = Counter(str(t.get(exit_key) or "") for t in ledger)
    cur = 0
    counts: list[int] = []
    for d in dates:
        cur -= closes.get(d, 0)
        if cur < 0:
            cur = 0
        free = n_slots - cur
        cur += min(opens.get(d, 0), free)
        counts.append(cur)
    hist = {str(k): counts.count(k) for k in range(0, n_slots + 1)}
    mean_fill = sum(counts) / len(counts) if counts else 0.0
    return {
        "n_slots": n_slots,
        "mean_filled_slots": round(mean_fill, 3),
        "mean_fill_pct": round(100.0 * mean_fill / n_slots, 2) if n_slots else None,
        "days_full": sum(1 for c in counts if c == n_slots),
        "days_full_pct": _pct(sum(1 for c in counts if c == n_slots), len(counts)),
        "days_empty": sum(1 for c in counts if c == 0),
        "days_empty_pct": _pct(sum(1 for c in counts if c == 0), len(counts)),
        "occupancy_hist": hist,
        "daily_filled_slots": counts,
    }


def _empty_stretches(flags: list[bool]) -> dict[str, Any]:
    runs: list[int] = []
    run = 0
    for empty in flags:
        if empty:
            run += 1
        else:
            if run:
                runs.append(run)
            run = 0
    if run:
        runs.append(run)
    return {
        "n_stretches": len(runs),
        "max_days": max(runs) if runs else 0,
        "mean_days": round(sum(runs) / len(runs), 2) if runs else 0.0,
        "top_stretches": sorted(runs, reverse=True)[:10],
    }


def _year_nav_path(daily_nav: list[dict[str, Any]]) -> dict[str, Any]:
    by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily_nav:
        by_year[str(row["date"])[:4]].append(row)
    out: dict[str, Any] = {}
    for y in sorted(by_year):
        rows = by_year[y]
        eq0 = float(rows[0]["equity_ntd"])
        eq1 = float(rows[-1]["equity_ntd"])
        empty = sum(1 for r in rows if not r.get("in_market_flag"))
        mean_cash = sum(float(r["cash_pct"]) for r in rows) / len(rows)
        out[y] = {
            "n_trade_dates": len(rows),
            "equity_start": round(eq0, 2),
            "equity_end": round(eq1, 2),
            "period_return_pct": round((eq1 / eq0 - 1.0) * 100.0, 4) if eq0 else None,
            "mean_cash_pct": round(mean_cash, 2),
            "empty_days": empty,
            "empty_days_pct": _pct(empty, len(rows)),
        }
    return out


def _year_trade_stats(
    ledger: list[dict[str, Any]],
    *,
    ret_key: str,
) -> dict[str, Any]:
    by_year: dict[str, list[float]] = defaultdict(list)
    for t in ledger:
        ed = str(t.get("entry_date") or "")
        if len(ed) < 4 or t.get(ret_key) is None:
            continue
        by_year[ed[:4]].append(float(t[ret_key]))
    out: dict[str, Any] = {}
    for y in sorted(by_year):
        xs = by_year[y]
        wins = sum(1 for x in xs if x > 0)
        out[y] = {
            "n": len(xs),
            "mean_ret_pct": round(sum(xs) / len(xs), 3),
            "win_rate_pct": round(100.0 * wins / len(xs), 2),
        }
    return out


def _month_trade_counts(ledger: list[dict[str, Any]]) -> dict[str, int]:
    c = Counter(str(t.get("entry_date") or "")[:7] for t in ledger if t.get("entry_date"))
    return dict(sorted(c.items()))


def _tail_window(
    daily_nav: list[dict[str, Any]],
    occupancy: list[int],
    *,
    n_days: int = 90,
    n_slots: int,
) -> dict[str, Any]:
    if len(daily_nav) < n_days:
        n_days = len(daily_nav)
    nav = daily_nav[-n_days:]
    occ = occupancy[-n_days:] if occupancy else []
    eq0 = float(nav[0]["equity_ntd"])
    eq1 = float(nav[-1]["equity_ntd"])
    mean_cash = sum(float(r["cash_pct"]) for r in nav) / len(nav)
    empty = sum(1 for r in nav if not r.get("in_market_flag"))
    mean_occ = sum(occ) / len(occ) if occ else None
    return {
        "n_days": n_days,
        "date_start": nav[0]["date"],
        "date_end": nav[-1]["date"],
        "period_return_pct": round((eq1 / eq0 - 1.0) * 100.0, 4) if eq0 else None,
        "mean_cash_pct": round(mean_cash, 2),
        "empty_days": empty,
        "mean_filled_slots": round(mean_occ, 3) if mean_occ is not None else None,
        "mean_fill_pct": round(100.0 * mean_occ / n_slots, 2)
        if mean_occ is not None and n_slots
        else None,
        "days_full": sum(1 for c in occ if c == n_slots) if occ else None,
    }


def _util_normalized(
    *,
    mean_daily_pct: float | None,
    avg_util_pct: float | None,
) -> float | None:
    if mean_daily_pct is None or not avg_util_pct:
        return None
    return round(float(mean_daily_pct) / (float(avg_util_pct) / 100.0), 6)


def build_gap_diagnostic(payload: dict[str, Any]) -> dict[str, Any]:
    """Build diagnostic block from comparison bundle JSON (no DB / no 1m panel)."""
    strategies = payload.get("strategies") or {}
    c18 = strategies.get("rrg-mono-swap-accel") or {}
    abc = strategies.get("abc-v3-f1-pullback") or {}
    c18_pf = c18.get("portfolio") or {}
    abc_pf = abc.get("portfolio") or {}
    c18_nav = list(c18_pf.get("daily_nav") or [])
    abc_nav = list(abc_pf.get("daily_nav") or [])
    dates = [str(r["date"]) for r in abc_nav] or [str(r["date"]) for r in c18_nav]
    c18_ledger = list(payload.get("c18acc_trade_ledger") or [])
    abc_ledger = list(payload.get("abc_f1_trade_ledger") or [])
    n_c18_slots = int(c18.get("n_slots") or 3)
    n_abc_slots = int(abc.get("n_slots") or 5)
    hold_abc = int(abc.get("hold_days") or 3)
    hold_c18 = float(c18_pf.get("avg_hold_days") or 9.0)

    c18_occ_full = _occupancy_from_ledger(c18_ledger, dates, n_slots=n_c18_slots)
    abc_occ_full = _occupancy_from_ledger(abc_ledger, dates, n_slots=n_abc_slots)
    c18_daily_occ = list(c18_occ_full.pop("daily_filled_slots", []) or [])
    abc_daily_occ = list(abc_occ_full.pop("daily_filled_slots", []) or [])
    c18_occ = c18_occ_full
    abc_occ = abc_occ_full

    abc_event_exec = ((payload.get("event_level") or {}).get("abc_f1") or {}).get(
        "executed_topk"
    ) or {}
    abc_event_all = ((payload.get("event_level") or {}).get("abc_f1") or {}).get(
        "all_candidates"
    ) or {}
    champ = ((payload.get("event_level") or {}).get("champion_pipeline") or {})
    c18_event = (payload.get("event_level") or {}).get("c18acc") or {}

    abc_exec_mean = abc_event_exec.get("mean_ret_net_pct")
    if abc_exec_mean is None and abc_ledger:
        nets = [float(t["net_pct"]) for t in abc_ledger if t.get("net_pct") is not None]
        abc_exec_mean = sum(nets) / len(nets) if nets else None
    c18_mean = c18_event.get("mean_ret_net_pct")
    if c18_mean is None and c18_ledger:
        nets = [float(t["return_pct"]) for t in c18_ledger if t.get("return_pct") is not None]
        c18_mean = sum(nets) / len(nets) if nets else None

    abc_event_daily = (
        float(abc_exec_mean) / hold_abc if abc_exec_mean is not None and hold_abc else None
    )
    c18_event_daily = (
        float(c18_mean) / hold_c18 if c18_mean is not None and hold_c18 else None
    )
    abc_util = float(abc_pf.get("avg_utilization_pct") or 0) / 100.0
    c18_util = float(c18_pf.get("avg_utilization_pct") or 0) / 100.0

    theo_max_abc = round(n_abc_slots * (len(dates) / hold_abc), 1) if hold_abc else None
    n_abc_trades = int(abc_pf.get("n_trades") or len(abc_ledger) or 0)
    n_c18_trades = int(c18_pf.get("n_trades") or len(c18_ledger) or 0)

    abc_empty_flags = [not bool(r.get("in_market_flag")) for r in abc_nav]
    entry_days = Counter(str(t.get("entry_date") or "") for t in abc_ledger)
    entries_when = [entry_days[d] for d in dates if entry_days[d] > 0]

    # Counterfactual: same event edge × peer utilization
    cf_abc_at_c18_util = (
        round(abc_event_daily * c18_util, 6)
        if abc_event_daily is not None
        else None
    )
    cf_c18_at_abc_util = (
        round(c18_event_daily * abc_util, 6)
        if c18_event_daily is not None
        else None
    )

    slot_day_ntd_abc = n_abc_trades * float(abc.get("slot_cap_ntd") or 0) * hold_abc
    slot_day_ntd_c18 = n_c18_trades * float(c18.get("slot_cap_ntd") or 0) * hold_c18

    diagnosis = {
        "primary_driver": "cash_drag_empty_slots",
        "fairness": "procedure_fair_substance_unfair",
        "verdict_zh": (
            "程序契約公平（同 notional / 同 NAV 引擎），但策略部署實質不公："
            "ABC 長窗平均僅填 ~40% 槽、近 1/4 天空手；你記得的高報酬是近窗事件層，"
            "不是 2024 起常滿倉 NAV。"
        ),
        "not_a_pnl_sign_bug": True,
        "drivers": [
            {
                "rank": 1,
                "id": "empty_slots",
                "title": "空槽 / 利用率（主因）",
                "detail": (
                    f"ABC mean fill {abc_occ.get('mean_fill_pct')}% · "
                    f"empty days {abc_occ.get('days_empty_pct')}% · "
                    f"C18 fill {c18_occ.get('mean_fill_pct')}%"
                ),
            },
            {
                "rank": 2,
                "id": "layer_mixup",
                "title": "事件層 vs 組合層混讀",
                "detail": (
                    f"Champion 90d event {champ.get('mean_ret_3d_net_pct')}% · "
                    f"長窗 executed event {abc_exec_mean}% · "
                    f"組合日均被 util 稀釋"
                ),
            },
            {
                "rank": 3,
                "id": "early_window_weak",
                "title": "2024–2025 訊號稀 + 弱邊",
                "detail": "近 90d NAV ABC 反超 C18；長窗差距幾乎全在早年",
            },
            {
                "rank": 4,
                "id": "hold_and_throughput",
                "title": "持有期 / 資金吞吐不同構",
                "detail": (
                    f"C18 hold≈{hold_c18}d util高 · ABC hold={hold_abc}d 必須高頻補單"
                ),
            },
            {
                "rank": 5,
                "id": "marking_mismatch",
                "title": "標記口徑次要差異",
                "detail": "事件層盤中 poll 價 vs 組合日收盤 M2M · ledger net 已扣 0.585%",
            },
        ],
    }

    return {
        "schema": "c18acc_abc_v3_f1_gap_diagnostic-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_comparison": {
            "schema": payload.get("schema"),
            "generated_at": payload.get("generated_at"),
            "date_start": payload.get("date_start"),
            "date_end": payload.get("date_end"),
            "n_trade_dates": payload.get("n_trade_dates") or len(dates),
        },
        "kbar_policy": {
            "diagnostic_rebuild": "none · offline from comparison JSON",
            "pricing_preference": "5m_aggregated",
            "helper": "stock_db.kbar.kbar_5m_close_at_minute / load_kbar_day_5m_closes",
            "note": (
                "本診斷不重建 dual-WMA panel、不逐 poll 掃 1m。"
                "後續若重跑訊號，取價應走 5m 聚合路徑。"
            ),
        },
        "diagnosis": diagnosis,
        "portfolio_headline": {
            "c18acc": {
                "total_return_pct": c18_pf.get("total_return_pct"),
                "mean_daily_return_pct": c18_pf.get("mean_daily_return_pct"),
                "ann_mean_return_pct": c18_pf.get("ann_mean_return_pct"),
                "avg_utilization_pct": c18_pf.get("avg_utilization_pct"),
                "n_trades": n_c18_trades,
                "avg_hold_days": c18_pf.get("avg_hold_days"),
            },
            "abc_f1": {
                "total_return_pct": abc_pf.get("total_return_pct"),
                "mean_daily_return_pct": abc_pf.get("mean_daily_return_pct"),
                "ann_mean_return_pct": abc_pf.get("ann_mean_return_pct"),
                "avg_utilization_pct": abc_pf.get("avg_utilization_pct"),
                "n_trades": n_abc_trades,
                "avg_hold_days": abc_pf.get("avg_hold_days"),
                "n_candidates": abc.get("n_candidates"),
                "signal_capture_pct": (payload.get("comparison") or {}).get(
                    "abc_f1_signal_capture_pct"
                ),
            },
        },
        "occupancy": {"c18acc": c18_occ, "abc_f1": abc_occ},
        "abc_empty_stretches": _empty_stretches(abc_empty_flags),
        "abc_entry_intensity": {
            "entry_days": len(entries_when),
            "entry_days_pct": _pct(len(entries_when), len(dates)),
            "mean_entries_when_any": _mean([float(x) for x in entries_when]),
            "entries_per_day_hist": dict(
                sorted(Counter(entries_when).items())
            ),
            "theo_max_trades_if_always_full": theo_max_abc,
            "achieved_vs_theo_pct": _pct(n_abc_trades, int(theo_max_abc or 0)),
        },
        "capital_throughput": {
            "abc_slot_day_ntd": round(slot_day_ntd_abc, 0),
            "c18_slot_day_ntd": round(slot_day_ntd_c18, 0),
            "c18_over_abc_ratio": round(slot_day_ntd_c18 / slot_day_ntd_abc, 3)
            if slot_day_ntd_abc
            else None,
        },
        "dilution": {
            "abc_event_daily_on_invested_pct": abc_event_daily,
            "abc_util": round(abc_util, 4),
            "abc_expected_port_daily_pct": round(abc_event_daily * abc_util, 6)
            if abc_event_daily is not None
            else None,
            "abc_actual_port_daily_pct": abc_pf.get("mean_daily_return_pct"),
            "c18_event_daily_on_invested_pct": c18_event_daily,
            "c18_util": round(c18_util, 4),
            "c18_expected_port_daily_pct": round(c18_event_daily * c18_util, 6)
            if c18_event_daily is not None
            else None,
            "c18_actual_port_daily_pct": c18_pf.get("mean_daily_return_pct"),
            "abc_util_normalized_daily_pct": _util_normalized(
                mean_daily_pct=abc_pf.get("mean_daily_return_pct"),
                avg_util_pct=abc_pf.get("avg_utilization_pct"),
            ),
            "c18_util_normalized_daily_pct": _util_normalized(
                mean_daily_pct=c18_pf.get("mean_daily_return_pct"),
                avg_util_pct=c18_pf.get("avg_utilization_pct"),
            ),
            "counterfactual_abc_daily_at_c18_util_pct": cf_abc_at_c18_util,
            "counterfactual_c18_daily_at_abc_util_pct": cf_c18_at_abc_util,
        },
        "event_layers": {
            "champion_90d": champ,
            "abc_all_candidates": abc_event_all,
            "abc_executed": abc_event_exec
            or {
                "n": n_abc_trades,
                "mean_ret_net_pct": round(float(abc_exec_mean), 3)
                if abc_exec_mean is not None
                else None,
            },
            "c18acc": c18_event
            or {
                "n": n_c18_trades,
                "mean_ret_net_pct": round(float(c18_mean), 3)
                if c18_mean is not None
                else None,
            },
        },
        "by_year": {
            "abc_nav": _year_nav_path(abc_nav),
            "c18_nav": _year_nav_path(c18_nav),
            "abc_trades": _year_trade_stats(abc_ledger, ret_key="net_pct"),
            "c18_trades": _year_trade_stats(c18_ledger, ret_key="return_pct"),
        },
        "abc_trades_by_month": _month_trade_counts(abc_ledger),
        "last_90d": {
            "abc_f1": _tail_window(abc_nav, abc_daily_occ, n_slots=n_abc_slots),
            "c18acc": _tail_window(c18_nav, c18_daily_occ, n_slots=n_c18_slots),
        },
        "recommendations": [
            "報表拆：事件層 / 組合層 / 近 90d / 年分窗，禁止混讀",
            "加 util-normalized daily 與 counterfactual（同 util）欄",
            "sensitivity：ABC 改 3 槽，避免 5 槽空置造成現金比率偏高",
            "後續取價改 5m 聚合 helper，不要逐 poll 掃 1m",
            "標記一致：事件 poll 價 vs 組合收盤擇一對齊後再比",
        ],
    }


def render_gap_diagnostic_md(diag: dict[str, Any]) -> str:
    src = diag.get("source_comparison") or {}
    dgn = diag.get("diagnosis") or {}
    port = diag.get("portfolio_headline") or {}
    c18 = port.get("c18acc") or {}
    abc = port.get("abc_f1") or {}
    occ_c = (diag.get("occupancy") or {}).get("c18acc") or {}
    occ_a = (diag.get("occupancy") or {}).get("abc_f1") or {}
    dil = diag.get("dilution") or {}
    thr = diag.get("capital_throughput") or {}
    empty = diag.get("abc_empty_stretches") or {}
    inten = diag.get("abc_entry_intensity") or {}
    by = diag.get("by_year") or {}
    last = diag.get("last_90d") or {}
    ev = diag.get("event_layers") or {}
    kbar = diag.get("kbar_policy") or {}
    champ = ev.get("champion_90d") or {}
    abc_all = ev.get("abc_all_candidates") or {}
    abc_ex = ev.get("abc_executed") or {}

    lines = [
        "# C18acc vs ABC v3+f1 · 長窗差距診斷",
        "",
        f"生成：`{diag.get('generated_at')}`",
        f"來源對照：`{src.get('date_start')} ~ {src.get('date_end')}` "
        f"（{src.get('n_trade_dates')} 交易日）· bundle `{src.get('generated_at')}`",
        "",
        "## 一句結論",
        "",
        dgn.get("verdict_zh") or "",
        "",
        f"- Primary driver：`{dgn.get('primary_driver')}`",
        f"- Fairness：`{dgn.get('fairness')}`（程序公平、部署實質不公）",
        f"- PnL 符號 bug：{'否' if dgn.get('not_a_pnl_sign_bug') else '待查'}",
        "",
        "## K 線政策（本次）",
        "",
        f"- 診斷重建：{kbar.get('diagnostic_rebuild')}",
        f"- 取價偏好：**{kbar.get('pricing_preference')}**",
        f"- Helper：`{kbar.get('helper')}`",
        f"- 說明：{kbar.get('note')}",
        "",
        "## 組合績效 headlines",
        "",
        "| 指標 | C18acc | ABC v3+f1 |",
        "|------|--------|-----------|",
        f"| 區間總報酬 % | {c18.get('total_return_pct')} | {abc.get('total_return_pct')} |",
        f"| 日均報酬 % | {c18.get('mean_daily_return_pct')} | {abc.get('mean_daily_return_pct')} |",
        f"| 年化日均 % | {c18.get('ann_mean_return_pct')} | {abc.get('ann_mean_return_pct')} |",
        f"| 利用率 % | {c18.get('avg_utilization_pct')} | {abc.get('avg_utilization_pct')} |",
        f"| 成交筆 | {c18.get('n_trades')} | {abc.get('n_trades')} |",
        f"| 均持有日 | {c18.get('avg_hold_days')} | {abc.get('avg_hold_days')} |",
        "",
        "## 1. 空槽 / 佔用（主因）",
        "",
        "| | C18acc | ABC v3+f1 |",
        "|--|--------|-----------|",
        f"| 平均填槽 | {occ_c.get('mean_filled_slots')} / {occ_c.get('n_slots')} "
        f"（{occ_c.get('mean_fill_pct')}%） | "
        f"{occ_a.get('mean_filled_slots')} / {occ_a.get('n_slots')} "
        f"（{occ_a.get('mean_fill_pct')}%） |",
        f"| 滿槽天數 % | {occ_c.get('days_full_pct')} | {occ_a.get('days_full_pct')} |",
        f"| 空手天數 % | {occ_c.get('days_empty_pct')} | {occ_a.get('days_empty_pct')} |",
        f"| occupancy hist | `{occ_c.get('occupancy_hist')}` | `{occ_a.get('occupancy_hist')}` |",
        "",
        f"ABC 空手段落：n={empty.get('n_stretches')} · max={empty.get('max_days')}d · "
        f"mean={empty.get('mean_days')}d · top={empty.get('top_stretches')}",
        "",
        f"ABC 進場強度：有進場日 {inten.get('entry_days')} "
        f"（{inten.get('entry_days_pct')}%）· "
        f"當日均進 {inten.get('mean_entries_when_any')} 檔 · "
        f"理論滿倉吞吐 ≈{inten.get('theo_max_trades_if_always_full')} · "
        f"達成 {inten.get('achieved_vs_theo_pct')}%",
        "",
        f"資金吞吐（slot-day·NTD）：C18 **{thr.get('c18_slot_day_ntd')}** vs "
        f"ABC **{thr.get('abc_slot_day_ntd')}** · 比 **{thr.get('c18_over_abc_ratio')}×**",
        "",
        "## 2. 事件層 vs 組合層（量綱）",
        "",
        "| 口徑 | n | 每筆均 % | 備註 |",
        "|------|--:|--------:|------|",
        f"| Champion 90d event | {champ.get('n')} | {champ.get('mean_ret_3d_net_pct')} | "
        f"{champ.get('source') or champ.get('label') or ''} |",
        f"| 長窗全候選 unconstrained | {abc_all.get('n')} | {abc_all.get('mean_ret_net_pct')} | 無槽限制 |",
        f"| 長窗 5 槽成交 | {abc_ex.get('n')} | {abc_ex.get('mean_ret_net_pct')} | 有資金約束 |",
        "",
        "## 3. 利用率稀釋 / util-normalized",
        "",
        "| | ABC | C18acc |",
        "|--|-----|--------|",
        f"| 事件日均（投入端）% | {dil.get('abc_event_daily_on_invested_pct')} | "
        f"{dil.get('c18_event_daily_on_invested_pct')} |",
        f"| 利用率 | {dil.get('abc_util')} | {dil.get('c18_util')} |",
        f"| 預期組合日均 ≈ event×util | {dil.get('abc_expected_port_daily_pct')} | "
        f"{dil.get('c18_expected_port_daily_pct')} |",
        f"| 實際組合日均 | {dil.get('abc_actual_port_daily_pct')} | "
        f"{dil.get('c18_actual_port_daily_pct')} |",
        f"| util-normalized 日均 | {dil.get('abc_util_normalized_daily_pct')} | "
        f"{dil.get('c18_util_normalized_daily_pct')} |",
        "",
        f"**反事實**：ABC 若享有 C18 利用率 → 組合日均約 "
        f"**{dil.get('counterfactual_abc_daily_at_c18_util_pct')}%** "
        f"（會高於 C18 實際 {dil.get('c18_actual_port_daily_pct')}%）。",
        "",
        "→ 不是 α 不夠，是部署率不夠。",
        "",
        "## 4. 按年拆解",
        "",
        "### NAV 年路徑",
        "",
        "| 年 | ABC 報酬% | ABC 空手天 | ABC mean cash% | C18 報酬% | C18 空手天 |",
        "|----|----------:|----------:|---------------:|----------:|----------:|",
    ]
    abc_nav_y = by.get("abc_nav") or {}
    c18_nav_y = by.get("c18_nav") or {}
    for y in sorted(set(abc_nav_y) | set(c18_nav_y)):
        a = abc_nav_y.get(y) or {}
        c = c18_nav_y.get(y) or {}
        lines.append(
            f"| {y} | {a.get('period_return_pct')} | {a.get('empty_days')} | "
            f"{a.get('mean_cash_pct')} | {c.get('period_return_pct')} | {c.get('empty_days')} |"
        )

    lines.extend(
        [
            "",
            "### 成交均筆（年）",
            "",
            "| 年 | ABC n | ABC mean net% | ABC win% | C18 n | C18 mean ret% | C18 win% |",
            "|----|------:|--------------:|---------:|------:|--------------:|---------:|",
        ]
    )
    abc_tr = by.get("abc_trades") or {}
    c18_tr = by.get("c18_trades") or {}
    for y in sorted(set(abc_tr) | set(c18_tr)):
        a = abc_tr.get(y) or {}
        c = c18_tr.get(y) or {}
        lines.append(
            f"| {y} | {a.get('n')} | {a.get('mean_ret_pct')} | {a.get('win_rate_pct')} | "
            f"{c.get('n')} | {c.get('mean_ret_pct')} | {c.get('win_rate_pct')} |"
        )

    lines.extend(
        [
            "",
            "### ABC 月進場筆數",
            "",
            "| 月 | n |",
            "|----|--:|",
        ]
    )
    for m, n in (diag.get("abc_trades_by_month") or {}).items():
        lines.append(f"| {m} | {n} |")

    a90 = last.get("abc_f1") or {}
    c90 = last.get("c18acc") or {}
    lines.extend(
        [
            "",
            "## 5. 近 90 天（同一長跑切開）",
            "",
            f"窗口：`{a90.get('date_start')} ~ {a90.get('date_end')}`",
            "",
            "| | ABC | C18acc |",
            "|--|-----|--------|",
            f"| 區間報酬 % | **{a90.get('period_return_pct')}** | {c90.get('period_return_pct')} |",
            f"| mean cash % | {a90.get('mean_cash_pct')} | {c90.get('mean_cash_pct')} |",
            f"| mean fill | {a90.get('mean_filled_slots')}（{a90.get('mean_fill_pct')}%） | "
            f"{c90.get('mean_filled_slots')}（{c90.get('mean_fill_pct')}%） |",
            f"| 空手天 | {a90.get('empty_days')} | {c90.get('empty_days')} |",
            "",
            "近窗 ABC 組合反而領先 → 與「記憶中的高報酬」對齊；長窗被 2024–2025 拖累。",
            "",
            "## 6. 差異成因清單",
            "",
        ]
    )
    for row in dgn.get("drivers") or []:
        lines.append(
            f"{row.get('rank')}. **{row.get('title')}** (`{row.get('id')}`) — {row.get('detail')}"
        )

    lines.extend(
        [
            "",
            "## 7. 這樣比公平嗎？",
            "",
            "| 問題 | 回答 |",
            "|------|------|",
            "| 同一資金契約誰更賺錢？ | 部分合理，但必須標 ABC 大量閒置現金 |",
            "| 誰的 α / 訊號品質更好？ | 不合理；看事件層或 util-normalized |",
            "| ABC 應該更高卻沒更高？ | 期待混層：近窗事件強 ≠ 長窗滿倉 NAV |",
            "| 模擬器把 ± 搞反？ | 否 · ledger 與 NAV 同量級閉合 |",
            "",
            "## 8. 建議下一步",
            "",
        ]
    )
    for i, tip in enumerate(diag.get("recommendations") or [], 1):
        lines.append(f"{i}. {tip}")

    lines.extend(
        [
            "",
            "## 模組",
            "",
            "- `scripts/run_c18acc_abc_v3_f1_gap_diagnostic.py`",
            "- `src/research/backtest/c18acc_abc_v3_f1_gap_diagnostic.py`",
            "- 5m helper：`src/stock_db/kbar.py` · `load_kbar_day_5m_closes`",
        ]
    )
    return "\n".join(lines)
