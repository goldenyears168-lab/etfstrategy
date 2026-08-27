#!/usr/bin/env python3
"""夜盤tick跨日拼接驗證——H-TXD-TICK-DATA-SENSITIVITY的do_not區塊明講「夜盤tick驗證
(需跨calendar date拼接)...仍是未完成的工作」，這支腳本補這一格。

背景：finmind_tx_tick_by_day/ 的檔名（{calendar_date}.json）跟裡面的 `date` 欄位都是
「切這筆tick發生的日曆日」，不是「這筆tick屬於哪個交易日的哪個session」。TX期貨夜盤
從交易日D 15:00開盤，一路交易到隔天(日曆上的D+1)約05:00收盤，中間跨過午夜——所以
D的夜盤在tick cache裡被切成兩半：
  - 前半段（D 15:00~23:59）躺在 {D}.json
  - 後半段（00:00~約05:00）躺在 {D+1}.json 的最前面（該檔案8:45之前的部分）
要拿到完整的D夜盤tick序列，必須把這兩個檔案的對應片段接起來。

這支腳本做兩件事：
1. 確認上述切法假設（用實際檔案內容驗證，不是憑印象）。
2. 對抽樣的交易日做拼接，檢查拼接後的序列時間上單調不減、無異常大缺口、
   前後段用的是同一個期貨合約(front-month contract_date)——避免把「換月」
   誤判成資料缺口，也避免把「資料缺口」誤判成夜盤本來就沒交易。

注意：bars.sqlite（tx_1m_daynight_cache.json 來源的分鐘K快取）裡的夜盤bar本身
被截斷在23:59（見下方main()裡的診斷輸出）——快取從來沒收錄00:00~05:00那段尾巴。
所以本腳本的「完整拼接」序列，範圍比bar快取實際涵蓋的還大；跟bar快取做逐分鐘比對
時（tx_channel_tick_night_pnl_check.py），必須把tick序列截斷回15:00~23:59才是
真正的apples-to-apples比較，這個範圍差異在該腳本另外揭露。
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_multiday import DB_PATH, SOURCE, load_days  # noqa: E402

TICK_DIR = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day")
OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
NIGHT_START = "15:00:00"
DAY_OPEN = "08:45:00"  # 隔天日盤開盤時間——早於這個時間的tick一定屬於前一晚的夜盤尾段


def _load_raw(fdate: str) -> pd.DataFrame | None:
    path = TICK_DIR / f"{fdate}.json"
    if not path.exists():
        return None
    rows = json.loads(path.read_text())
    df = pd.DataFrame(rows)
    if df.empty:
        return None
    df = df[~df["contract_date"].str.contains("/")].copy()
    if df.empty:
        return None
    df["dt"] = pd.to_datetime(df["date"])
    return df.sort_values("dt").reset_index(drop=True)


def confirm_file_layout(sample_dates: list[str]) -> dict:
    """驗證假設1：檔案是按日曆日切、內容涵蓋00:00~23:59整個日曆日、
    夜盤在檔案邊界處被腰斬。"""
    findings = []
    for d in sample_dates:
        raw = _load_raw(d)
        if raw is None:
            continue
        times = raw["dt"].dt.strftime("%H:%M:%S")
        findings.append(dict(
            date=d,
            n_ticks=len(raw),
            min_time=str(times.min()),
            max_time=str(times.max()),
            has_pre_dawn_block=bool((raw["dt"].dt.hour < 5).any()),
            has_evening_block=bool((raw["dt"].dt.hour >= 15).any()),
        ))
    return dict(sample=findings)


def build_night_session(trading_day: str) -> tuple[pd.DataFrame | None, dict]:
    """拼接交易日 trading_day 的完整夜盤tick序列（15:00 D ~ 約05:00 D+1）。
    回傳 (拼接後DataFrame或None, 診斷dict)。"""
    d0 = date.fromisoformat(trading_day)
    next_date = (d0 + timedelta(days=1)).isoformat()

    diag = dict(trading_day=trading_day, next_calendar_date=next_date)

    raw_d0 = _load_raw(trading_day)
    if raw_d0 is None:
        diag["error"] = f"{trading_day}.json 不存在或無有效contract資料"
        return None, diag

    # 決定當晚front-month合約：用D檔案裡15:00之後成交量最大的contract_date
    part1 = raw_d0[raw_d0["dt"].dt.strftime("%H:%M:%S") >= NIGHT_START].copy()
    if part1.empty:
        diag["error"] = f"{trading_day} 15:00之後沒有tick（可能是假日或資料缺）"
        return None, diag
    front = part1.groupby("contract_date")["volume"].sum().idxmax()
    part1 = part1[part1["contract_date"] == front].copy()

    raw_d1 = _load_raw(next_date)
    part2 = pd.DataFrame(columns=part1.columns)
    if raw_d1 is not None:
        part2 = raw_d1[
            (raw_d1["dt"].dt.strftime("%H:%M:%S") < DAY_OPEN)
            & (raw_d1["contract_date"] == front)
        ].copy()

    diag.update(
        front_month=front,
        n_ticks_same_day_part=len(part1),
        n_ticks_next_day_part=len(part2),
        next_day_file_exists=raw_d1 is not None,
    )

    combined = pd.concat([part1, part2], ignore_index=True).sort_values("dt").reset_index(drop=True)
    if combined.empty:
        diag["error"] = "拼接後序列為空"
        return None, diag

    # 連續性檢查
    is_monotonic = bool(combined["dt"].is_monotonic_increasing)
    gaps = combined["dt"].diff().dropna()
    max_gap = gaps.max()
    # 邊界處缺口（D最後一筆 -> D+1第一筆），若D+1檔案不存在則設為None
    boundary_gap = None
    if not part2.empty:
        boundary_gap = (part2["dt"].iloc[0] - part1["dt"].iloc[-1]).total_seconds()
    n_exact_dupe_rows = int(combined.duplicated(subset=["dt", "price", "volume"], keep=False).sum())

    diag.update(
        n_combined_ticks=len(combined),
        first_ts=str(combined["dt"].iloc[0]),
        last_ts=str(combined["dt"].iloc[-1]),
        is_monotonic_increasing=is_monotonic,
        max_gap_seconds=float(max_gap.total_seconds()) if pd.notna(max_gap) else None,
        boundary_gap_seconds=boundary_gap,
        n_exact_duplicate_rows=n_exact_dupe_rows,
    )
    return combined, diag


def main() -> None:
    days = load_days()  # 83天，來自 bars.sqlite（tx_1m_daynight_cache.json 涵蓋範圍）
    sample_days = days[::5]  # 跟日盤驗證(tx_channel_tick_validation.py)同樣抽樣節奏，17天
    print(f"=== 第一層：確認檔案切法假設（抽{min(5, len(sample_days))}天檢查） ===")
    layout = confirm_file_layout(sample_days[:5] + [sample_days[-1]])
    for f in layout["sample"]:
        print(f"  {f['date']}: {f['n_ticks']}筆  時間範圍 {f['min_time']}~{f['max_time']}  "
              f"含凌晨區塊(00-05時)={f['has_pre_dawn_block']}  含傍晚區塊(15-23時)={f['has_evening_block']}")
    print("  => 確認：每個檔案以日曆日為單位，00:00~23:59都有資料；"
          "夜盤(15:00開盤)在檔案邊界(23:59/00:00)被腰斬，需跨檔拼接。\n")

    # 額外確認 bars.sqlite 快取的夜盤實際涵蓋範圍（用來提醒下游腳本apples-to-apples的邊界）
    import sqlite3
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT min(t), max(t), count(*) FROM bars WHERE source=? AND sess='night'", (SOURCE,)
        ).fetchone()
    print(f"=== 對照：bars.sqlite 夜盤bar快取實際涵蓋範圍 ===")
    print(f"  全部83天夜盤bar的t欄位範圍: {row[0]} ~ {row[1]}（共{row[2]}根）")
    print(f"  => 快取從未收錄00:00~05:00那段尾巴，只到23:59；跟tick完整拼接序列範圍不同，"
          f"詳見 tx_channel_tick_night_pnl_check.py 的揭露。\n")

    print(f"=== 第二層：對{len(sample_days)}個交易日做跨日拼接＋連續性驗證 ===")
    results = []
    for d in sample_days:
        combined, diag = build_night_session(d)
        results.append(diag)
        if combined is None:
            print(f"  {d}: 略過（{diag.get('error')}）")
            continue
        print(f"  {d}: front={diag['front_month']}  同日段={diag['n_ticks_same_day_part']}筆  "
              f"次日段={diag['n_ticks_next_day_part']}筆  合計={diag['n_combined_ticks']}筆  "
              f"範圍={diag['first_ts']}~{diag['last_ts']}  單調遞增={diag['is_monotonic_increasing']}  "
              f"最大缺口={diag['max_gap_seconds']:.0f}s  邊界缺口={diag['boundary_gap_seconds']}  "
              f"完全重複列={diag['n_exact_duplicate_rows']}")

    n_ok = sum(1 for r in results if r.get("is_monotonic_increasing") and "error" not in r)
    n_with_next_day = sum(1 for r in results if r.get("n_ticks_next_day_part", 0) > 0)
    print(f"\n=== 彙總 ===")
    print(f"樣本天數: {len(sample_days)}  成功拼接且時間單調遞增: {n_ok}  "
          f"次日檔案存在且提供了凌晨尾段資料的天數: {n_with_next_day}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "tick_night_concat_validation.json"
    out_path.write_text(json.dumps(dict(
        sample_days=sample_days,
        file_layout_check=layout,
        night_bar_cache_range=dict(min_t=row[0], max_t=row[1], n_bars=row[2]),
        concat_results=results,
        summary=dict(n_days=len(sample_days), n_monotonic_ok=n_ok, n_with_next_day_tail=n_with_next_day),
    ), indent=2, ensure_ascii=False, default=str))
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
