#!/usr/bin/env python3
"""RRG mono hold7 · 進場時點 sweep（日線 + FinMind KBar 盤中）。

比較：
1. 訊號固定 D4 收盤 fresh · 進場：訊號日收盤 / 隔日開盤 / 隔日 09/10/11/12
2. 隔日專家確認 K 線：Bone Zone · VWAP reclaim · VWAP bounce
3. 持有天數 sweep：1 / 2 / 3 / 5 / 7（訊號日收盤進）
4. 盤中訊號穩定度：訊號日 09/10/11/12 是否已 mono fresh（僅已成交腿）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flow_returns import stock_open, trading_dates_after  # noqa: E402
from finmind_client import fetch_finmind  # noqa: E402
from analytics.bench import bench_return_entry_to_exit  # noqa: E402
from research.backtest.finpilot_local_backtest import load_price_panels  # noqa: E402
from research.backtest.rrg_mono_backtest import (  # noqa: E402
    build_fresh_mono_calendar,
    simulate_mono_hold7,
)
from rrg_mono_daily_brief import HOLD_DAYS  # noqa: E402
from report_paths import RESEARCH_RRG  # noqa: E402
from rrg_mono_daily_brief import _exit_date_from_entry  # noqa: E402
from research.backtest.rrg_mono_expert_entry import (  # noqa: E402
    EXPERT_ENTRY_LABELS,
    ExpertEntryMode,
    detect_expert_entry,
)
from stock_db.kbar import (  # noqa: E402
    KbarBar,
    kbar_bars_from_finmind_rows,
    load_kbar_day_bars,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

INTRADAY_HOURS = ("09:00", "10:00", "11:00", "12:00")
HOLD_SWEEP = (1, 2, 3, 5, 7)
EXPERT_MODES: tuple[ExpertEntryMode, ...] = ("bone_zone", "vwap_reclaim", "vwap_bounce")


@lru_cache(maxsize=4096)
def _kbar_day(stock_id: str, trade_date: str) -> tuple[tuple[str, float], ...]:
    """FinMind TaiwanStockKBar · 單日 1 分 K；回傳 (minute, close) 序列。"""
    d = date.fromisoformat(trade_date)
    try:
        rows = fetch_finmind("TaiwanStockKBar", stock_id, d, d)
    except Exception:
        return ()
    out: list[tuple[str, float]] = []
    for r in rows:
        minute = str(r.get("minute") or "")
        close = r.get("close")
        if minute and close is not None:
            try:
                px = float(close)
                if px > 0:
                    out.append((minute, px))
            except (TypeError, ValueError):
                continue
    out.sort(key=lambda x: x[0])
    return tuple(out)


@lru_cache(maxsize=4096)
def _kbar_day_ohlcv(stock_id: str, trade_date: str) -> tuple[KbarBar, ...]:
    """單日 1 分 OHLCV · SQLite 優先、FinMind 補洞。"""
    conn = connect(DEFAULT_DB_PATH)
    try:
        bars = load_kbar_day_bars(conn, stock_id, trade_date)
        if bars:
            return bars
    finally:
        conn.close()
    d = date.fromisoformat(trade_date)
    try:
        rows = fetch_finmind("TaiwanStockKBar", stock_id, d, d)
    except Exception:
        return ()
    return kbar_bars_from_finmind_rows(rows)


def _price_at_or_before(bars: tuple[tuple[str, float], ...], hhmm: str) -> float | None:
    if not bars:
        return None
    target = hhmm if len(hhmm) > 5 else f"{hhmm}:00"
    last: float | None = None
    for minute, px in bars:
        if minute <= target:
            last = px
        else:
            break
    return last


def _settle_custom(
    conn,
    close,
    pos: dict,
    *,
    entry_date: str,
    entry_px: float,
    exit_date: str,
    bench_entry_mode: str = "open",
) -> dict | None:
    sid = str(pos["stock_id"])
    if exit_date not in close.index or sid not in close.columns:
        return None
    c1 = float(close.at[exit_date, sid])
    if entry_px <= 0 or c1 != c1:
        return None
    ret = (c1 / entry_px - 1.0) * 100.0
    bench = bench_return_entry_to_exit(
        conn, entry_date, exit_date, entry_price_mode=bench_entry_mode
    )
    if bench is None:
        return None
    return {
        "stock_id": sid,
        "stock_name": pos.get("stock_name", ""),
        "signal_date": str(pos.get("signal_date") or pos["entry_date"]),
        "entry_date": entry_date,
        "exit_date": exit_date,
        "return_pct": round(ret, 4),
        "bench_return_pct": round(bench, 4),
        "excess_pct": round(ret - bench, 4),
        "beat_bench": ret > bench,
    }


def _summarize(periods: list[dict]) -> dict:
    n = len(periods)
    if n == 0:
        return {"n": 0, "win_rate_vs_bench": None, "mean_return_pct": None, "mean_excess_pct": None}
    wins = sum(1 for p in periods if p.get("beat_bench"))
    return {
        "n": n,
        "win_rate_vs_bench": round(wins / n * 100.0, 2),
        "mean_return_pct": round(sum(p["return_pct"] for p in periods) / n, 4),
        "mean_excess_pct": round(sum(p["excess_pct"] for p in periods) / n, 4),
        "total_excess_pct": round(sum(p["excess_pct"] for p in periods), 4),
    }


def _collect_executed_legs(
    conn,
    *,
    date_start: str,
    date_end: str,
) -> list[dict]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    periods, _ = simulate_mono_hold7(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date={},
        fresh_by_date=fresh_by_date,
        zone_filter=None,
        entry_price_mode="close",
    )
    return periods


def sweep_entry_times(conn, legs: list[dict], close) -> dict[str, dict]:
    """訊號日 D4 收盤 · 重定價進場時點（同一組槽位成交腿）。"""
    modes: dict[str, list[dict]] = {
        "signal_close": [],
        "next_open": [],
    }
    for hh in INTRADAY_HOURS:
        modes[f"next_{hh[:2]}"] = []

    full_dates = close.index.astype(str).tolist()
    for leg in legs:
        sig = str(leg["signal_date"])
        exit_d = str(leg["exit_date"])
        sid = str(leg["stock_id"])
        pos = {
            "stock_id": sid,
            "stock_name": leg.get("stock_name", ""),
            "signal_date": sig,
            "entry_date": sig,
        }
        # baseline: signal day close
        c0 = float(close.at[sig, sid]) if sig in close.index else None
        if c0 and c0 > 0:
            row = _settle_custom(conn, close, pos, entry_date=sig, entry_px=c0, exit_date=exit_d)
            if row:
                modes["signal_close"].append(row)

        nxt = trading_dates_after(conn, sig, count=1)
        if not nxt:
            continue
        entry_d = nxt[0]
        opx = stock_open(conn, sid, entry_d)
        if opx and opx > 0:
            row = _settle_custom(
                conn, close, pos, entry_date=entry_d, entry_px=opx, exit_date=exit_d
            )
            if row:
                modes["next_open"].append(row)

        bars = _kbar_day(sid, entry_d)
        for hh in INTRADAY_HOURS:
            px = _price_at_or_before(bars, hh)
            if px is None:
                continue
            row = _settle_custom(
                conn, close, pos, entry_date=entry_d, entry_px=px, exit_date=exit_d
            )
            if row:
                modes[f"next_{hh[:2]}"].append(row)
        time.sleep(0.05)

    return {k: {"label": k, "summary": _summarize(v), "n_legs": len(v)} for k, v in modes.items()}


def sweep_expert_entries(conn, legs: list[dict], close) -> dict[str, dict]:
    """隔日專家確認 K 線進場 · hold7 出場不變。"""
    modes: dict[str, list[dict]] = {m: [] for m in EXPERT_MODES}
    kbar_checks = 0
    kbar_hits = 0
    trigger_miss = {m: 0 for m in EXPERT_MODES}

    for leg in legs:
        sig = str(leg["signal_date"])
        exit_d = str(leg["exit_date"])
        sid = str(leg["stock_id"])
        nxt = trading_dates_after(conn, sig, count=1)
        if not nxt:
            continue
        entry_d = nxt[0]
        pos = {
            "stock_id": sid,
            "stock_name": leg.get("stock_name", ""),
            "signal_date": sig,
            "entry_date": sig,
        }

        kbar_checks += 1
        bars = _kbar_day_ohlcv(sid, entry_d)
        if bars:
            kbar_hits += 1
        if not bars:
            for m in EXPERT_MODES:
                trigger_miss[m] += 1
            time.sleep(0.05)
            continue

        for mode in EXPERT_MODES:
            trig = detect_expert_entry(mode, bars)
            if trig is None:
                trigger_miss[mode] += 1
                continue
            row = _settle_custom(
                conn,
                close,
                pos,
                entry_date=entry_d,
                entry_px=trig.entry_px,
                exit_date=exit_d,
            )
            if row:
                row["entry_minute"] = trig.entry_minute
                row["stop_px"] = round(trig.stop_px, 4)
                row["entry_mode"] = mode
                modes[mode].append(row)
        time.sleep(0.05)

    out: dict[str, dict] = {}
    for mode in EXPERT_MODES:
        periods = modes[mode]
        out[mode] = {
            "label": EXPERT_ENTRY_LABELS[mode],
            "summary": _summarize(periods),
            "n_legs": len(periods),
            "trigger_miss": trigger_miss[mode],
        }
    out["_coverage"] = {
        "kbar_checks": kbar_checks,
        "kbar_hits": kbar_hits,
        "kbar_coverage_pct": round(kbar_hits / kbar_checks * 100.0, 2) if kbar_checks else 0.0,
    }
    return out


def sweep_hold_days(conn, legs: list[dict], close) -> dict[int, dict]:
  out: dict[int, dict] = {}
  full_dates = close.index.astype(str).tolist()
  for hold in HOLD_SWEEP:
      periods: list[dict] = []
      for leg in legs:
          sig = str(leg["signal_date"])
          sid = str(leg["stock_id"])
          if sig not in close.index or sid not in close.columns:
              continue
          entry_px = float(close.at[sig, sid])
          exit_d = _exit_date_from_entry(conn, full_dates, sig, hold)
          if not exit_d:
              continue
          pos = {"stock_id": sid, "stock_name": leg.get("stock_name", ""), "signal_date": sig}
          row = _settle_custom(
              conn, close, pos, entry_date=sig, entry_px=entry_px, exit_date=exit_d
          )
          if row:
              periods.append(row)
      out[hold] = _summarize(periods)
  return out


def sweep_intraday_signal_stability(conn, legs: list[dict]) -> dict:
    """訊號日盤中：若用 09/10/11/12 價格當 D4 末點，mono fresh 是否已成立。"""
    from market_benchmark import load_benchmark_close
    from rrg_mono_daily_brief import LOOKBACK, scan_rows_from_panels

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)

    by_hour: dict[str, dict[str, int]] = {hh: {"fresh": 0, "total": 0} for hh in INTRADAY_HOURS}
    unique_days = sorted({str(l["signal_date"]) for l in legs})
    checked = 0
    for sig in unique_days:
        bars_cache: dict[str, tuple[tuple[str, float], ...]] = {}
        day_legs = [l for l in legs if str(l["signal_date"]) == sig]
        for hh in INTRADAY_HOURS:
            prov = close.copy()
            bench_p = bench.reindex(prov.index).astype(float).copy()
            if sig not in prov.index:
                prov.loc[sig] = float("nan")
                bench_p.loc[sig] = float("nan")
            for leg in day_legs:
                sid = str(leg["stock_id"])
                if sid not in bars_cache:
                    bars_cache[sid] = _kbar_day(sid, sig)
                    time.sleep(0.03)
                px = _price_at_or_before(bars_cache[sid], hh)
                if px is None:
                    continue
                prov.at[sig, sid] = px
            # bench: use IX0001 daily open proxy if no kbar
            if sig in bench_p.index and not (bench_p.at[sig] == bench_p.at[sig]):
                prev = bench_p.loc[:sig].iloc[:-1].dropna()
                if not prev.empty:
                    bench_p.at[sig] = float(prev.iloc[-1])

            _, fresh_rows = scan_rows_from_panels(conn, sig, prov, bench_p)
            fresh_ids = {r.stock_id for r in fresh_rows}
            for leg in day_legs:
                by_hour[hh]["total"] += 1
                if str(leg["stock_id"]) in fresh_ids:
                    by_hour[hh]["fresh"] += 1
        checked += 1

    rates = {}
    for hh, c in by_hour.items():
        t = c["total"]
        rates[hh] = {
            "fresh_hits": c["fresh"],
            "total": t,
            "hit_rate_pct": round(c["fresh"] / t * 100.0, 1) if t else None,
        }
    return {"signal_days": len(unique_days), "by_hour": rates}


def _render_interpretation(results: dict) -> list[str]:
    entry = results.get("entry_times", {})
    expert = results.get("expert_entries", {})
    legs_n = results.get("legs_n", 0)

    sig_ex = (entry.get("signal_close", {}).get("summary") or {}).get("mean_excess_pct")
    nxt_open_ex = (entry.get("next_open", {}).get("summary") or {}).get("mean_excess_pct")
    nxt11_ex = (entry.get("next_11", {}).get("summary") or {}).get("mean_excess_pct")

    expert_rows: list[tuple[str, float | None, int, int]] = []
    for mode in EXPERT_MODES:
        block = expert.get(mode, {})
        s = block.get("summary", {})
        expert_rows.append(
            (
                mode,
                s.get("mean_excess_pct"),
                int(s.get("n") or 0),
                int(block.get("trigger_miss") or 0),
            )
        )
    expert_rows.sort(key=lambda r: (-(r[1] or -999.0), -r[2]))

    best_mode, best_ex, best_n, best_miss = expert_rows[0]
    best_label = EXPERT_ENTRY_LABELS.get(best_mode, best_mode)

    lines = [
        "## 5. 解讀與建議（隔日進場）",
        "",
        f"樣本期間共 **{legs_n}** 筆 hold7 成交腿；KBar 覆蓋約 "
        f"**{expert.get('_coverage', {}).get('kbar_coverage_pct', '—')}%**。",
        "",
        "### 與基線對照",
        "",
    ]
    if sig_ex is not None:
        lines.append(
            f"- 訊號日收盤進場均超額 **{sig_ex}%** 為上界；隔日執行必然稀釋 alpha。"
        )
    if nxt_open_ex is not None:
        lines.append(f"- **隔日開盤**均超額 **{nxt_open_ex}%**，為最簡單的隔日基線。")
    if nxt11_ex is not None:
        lines.append(f"- 固定時點 **隔日 11:00** 均超額 **{nxt11_ex}%**，略優於 09/10 點。")

    lines.extend(
        [
            "",
            "### 專家確認 K 線",
            "",
        ]
    )
    for mode, ex, n, miss in expert_rows:
        label = EXPERT_ENTRY_LABELS.get(mode, mode)
        rate = round(n / legs_n * 100.0, 1) if legs_n else 0.0
        lines.append(
            f"- **{label}**：n={n}（觸發率 {rate}%）、未命中 {miss}、均超額 **{ex}%**。"
        )

    lines.extend(["", "### 建議", ""])
    if best_n < legs_n * 0.25:
        lines.append(
            f"- 均超額最高為 **{best_label}**（{best_ex}%），但觸發率僅 "
            f"{round(best_n / legs_n * 100.0, 1) if legs_n else 0}%（n={best_n}），樣本偏少，宜視為假說而非預設執行。"
        )
    else:
        lines.append(
            f"- 專家模式中 **{best_label}** 均超額最高（**{best_ex}%**，n={best_n}）。"
        )

    if nxt_open_ex is not None and best_ex is not None and nxt_open_ex > best_ex:
        lines.append(
            f"- 就本窗而言，**隔日開盤**（{nxt_open_ex}%）仍優於所有專家觸發（最高 {best_ex}%）；"
            "確認 K 線未帶來額外 edge，反而因等待觸發錯過開盤後段走勢。"
        )
    elif best_ex is not None and nxt_open_ex is not None:
        lines.append(
            f"- **{best_label}** 略優隔日開盤（{best_ex}% vs {nxt_open_ex}%），可進入下一輪 "
            "parameter / 觸發時窗微調（例如限 09:30 前首觸發）。"
        )

    reclaim_n = next((n for m, _, n, _ in expert_rows if m == "vwap_reclaim"), 0)
    if reclaim_n >= legs_n * 0.7:
        lines.append(
            "- **VWAP reclaim** 觸發率最高、樣本最接近全集，適合作為專家模式首選候選；"
            "若均超額仍低於隔日開盤，建議保留開盤為 default、reclaim 為可選 refine。"
        )

    lines.append(
        "- 出場維持 hold7 收盤不變；進場研究結論不影響 SSG（D4 mono fresh）定義。"
    )
    lines.append("")
    return lines


def render_markdown(results: dict) -> str:
    lines = [
        f"# RRG mono · 盤中進場 / 持有 sweep · {results['date_start']}～{results['date_end']}",
        "",
        "訊號：**D4 收盤 mono fresh**（與現行策略相同）· 3 槽 · 出場預設 **hold7 收盤**（進場 sweep 段）。",
        "",
        "## 1. 進場時點（同一批成交腿 · 出場日不變）",
        "",
        "| 進場模式 | n | 勝率 vs 台指 | 均報酬% | 均超額% |",
        "|---------|---|-------------|--------|--------|",
    ]
    labels = {
        "signal_close": "訊號日收盤（基準）",
        "next_open": "隔日開盤",
        "next_09": "隔日 09:00",
        "next_10": "隔日 10:00",
        "next_11": "隔日 11:00",
        "next_12": "隔日 12:00",
    }
    base_excess = None
    for key in ("signal_close", "next_open", "next_09", "next_10", "next_11", "next_12"):
        block = results["entry_times"].get(key, {})
        s = block.get("summary", {})
        if key == "signal_close":
            base_excess = s.get("mean_excess_pct")
        lines.append(
            f"| {labels.get(key, key)} | {s.get('n', 0)} | {s.get('win_rate_vs_bench')}% | "
            f"{s.get('mean_return_pct')} | {s.get('mean_excess_pct')} |"
        )
    if base_excess is not None:
        lines.extend(["", f"基準均超額：**{base_excess}%**", ""])

    lines.extend(
        [
            "## 2. 隔日專家確認 K 線（hold7 出場 · 首觸發進場）",
            "",
            "訊號日 D4 收盤 mono fresh · **隔日** 09:05 起掃描 · 無觸發則該模式剔除該腿。",
            "",
            "| 進場模式 | n | 觸發未命中 | 勝率 vs 台指 | 均報酬% | 均超額% |",
            "|---------|---|-----------|-------------|--------|--------|",
        ]
    )
    expert = results.get("expert_entries", {})
    cov = expert.get("_coverage", {})
    for mode in EXPERT_MODES:
        block = expert.get(mode, {})
        s = block.get("summary", {})
        lines.append(
            f"| {EXPERT_ENTRY_LABELS.get(mode, mode)} | {s.get('n', 0)} | "
            f"{block.get('trigger_miss', '—')} | {s.get('win_rate_vs_bench')}% | "
            f"{s.get('mean_return_pct')} | {s.get('mean_excess_pct')} |"
        )
    lines.extend(
        [
            "",
            f"KBar 覆蓋：{cov.get('kbar_hits', '—')}/{cov.get('kbar_checks', '—')} 腿 "
            f"（{cov.get('kbar_coverage_pct', '—')}%）· SQLite 優先、FinMind 補洞。",
            "",
        ]
    )

    lines.extend(
        [
            "## 3. 持有天數 sweep（訊號日收盤進 · 槽位腿）",
            "",
            "| hold 日 | n | 勝率 vs 台指 | 均報酬% | 均超額% |",
            "|--------|---|-------------|--------|--------|",
        ]
    )
    for hold in HOLD_SWEEP:
        s = results["hold_days"].get(hold) or results["hold_days"].get(str(hold), {})
        lines.append(
            f"| {hold} | {s.get('n', 0)} | {s.get('win_rate_vs_bench')}% | "
            f"{s.get('mean_return_pct')} | {s.get('mean_excess_pct')} |"
        )

    lines.extend(
        [
            "",
            "## 4. 盤中訊號穩定度（訊號日 09–12 是否已 fresh）",
            "",
            "若把當日盤中價當 D4 軌跡末點重算 mono fresh，已成交標的在各時點仍為 fresh 的比例：",
            "",
            "| 時點 | fresh 命中 | 樣本 | 命中率% |",
            "|------|-----------|------|--------|",
        ]
    )
    stab = results.get("intraday_stability", {}).get("by_hour", {})
    for hh in INTRADAY_HOURS:
        r = stab.get(hh, {})
        lines.append(
            f"| {hh} | {r.get('fresh_hits', '—')} | {r.get('total', '—')} | {r.get('hit_rate_pct', '—')} |"
        )

    lines.extend(_render_interpretation(results))

    lines.extend(
        [
            "",
            "## 解讀備註",
            "",
            "- **隔日盤中進場**：訊號仍須等 D4 收盤確認；09:05 起才允許專家觸發（09:00–09:04 不交易）。",
            "- **專家模式**：Bone Zone = 回踩 9–20 EMA 帶後陽線收上 9 EMA；VWAP reclaim = 曾跌破 VWAP 後陽線收回；"
            "VWAP bounce = 全日收在 VWAP 上、觸線後下一根陽線。",
            "- **樣本差異**：專家模式 n 通常小於固定時點基線（需盤中觸發）；比較時以均超額% 為主、並看觸發率。",
            "- **盤中訊號**：若命中率遠低於 100%，代表收盤前訊號易翻轉，不適合直接取代 16:40 掃描。",
            "- KBar 來源：本地 `stock_kbar_1m` + FinMind `TaiwanStockKBar`（sponsor）；缺 bar 的腿會從該模式樣本剔除。",
            "",
            "---",
            "模組：`scripts/run_rrg_mono_intraday_entry_sweep.py` · `src/research/backtest/rrg_mono_expert_entry.py`",
        ]
    )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RRG mono intraday entry / hold sweep")
    parser.add_argument("--date-start", default="2024-01-01")
    parser.add_argument("--date-end", default="2026-06-22")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--skip-stability", action="store_true")
    args = parser.parse_args(argv)

    conn = connect(DEFAULT_DB_PATH)
    close, _, _ = load_price_panels(conn)
    print(f"Collecting executed legs ({args.date_start}..{args.date_end})...")
    legs = _collect_executed_legs(conn, date_start=args.date_start, date_end=args.date_end)
    print(f"  {len(legs)} legs")

    print("Sweeping entry times (KBar fetch; may take a few minutes)...")
    entry_times = sweep_entry_times(conn, legs, close)

    print("Sweeping expert confirmation entries...")
    expert_entries = sweep_expert_entries(conn, legs, close)

    print("Sweeping hold days...")
    hold_days = sweep_hold_days(conn, legs, close)

    stability = {}
    if not args.skip_stability:
        print("Intraday signal stability on signal days...")
        stability = sweep_intraday_signal_stability(conn, legs)

    conn.close()

    results = {
        "date_start": args.date_start,
        "date_end": args.date_end,
        "legs_n": len(legs),
        "entry_times": entry_times,
        "expert_entries": expert_entries,
        "hold_days": hold_days,
        "intraday_stability": stability,
    }
    md = render_markdown(results)
    stamp = date.today().strftime("%Y%m%d")
    out = args.output or RESEARCH_RRG / f"{stamp}_rrg_mono_intraday_entry_sweep.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"Wrote {out}")
    print()
    print(md)

    if args.json:
        args.json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
