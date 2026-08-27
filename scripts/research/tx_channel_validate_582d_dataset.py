#!/usr/bin/env python3
"""驗證 `tx_channel_build_582d_dataset.py` 新建的 `tx_1m_tick_built_582d` source，跟既有
`tx_1m_daynight_cache.json`（83 天，2026-04-01~2026-07-31）在重疊區間是否一致。

延續 `tx_channel_tick_validation.py` 的做法，但這次涵蓋日盤＋夜盤兩者（前者受限於 tick cache
會跨午夜，只做了日盤；這次新資料集刻意不跨午夜，兩個 session 都在同一個 calendar-date 檔案
裡，所以夜盤也能直接比對，不需要跨檔拼接）。

比對對象是兩個都已經寫進 bars.sqlite 的 source，逐分鐘比較 Open/High/Low/Close。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

DB_PATH = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/bars.sqlite")
OLD_SOURCE = "tx_1m_daynight_cache.json"
NEW_SOURCE = "tx_1m_tick_built_582d"
OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
SAMPLE_STRIDE = 5  # 每5天抽1天，跟 tick_validation.py 一致的抽樣密度


def load_overlap_days() -> list[str]:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        old_days = {r[0] for r in conn.execute(
            "SELECT DISTINCT day FROM bars WHERE source=?", (OLD_SOURCE,)).fetchall()}
        new_days = {r[0] for r in conn.execute(
            "SELECT DISTINCT day FROM bars WHERE source=?", (NEW_SOURCE,)).fetchall()}
    return sorted(old_days & new_days)


def load_source_day(source: str, day: str, sess: str) -> pd.DataFrame:
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query(
            "SELECT t, o, h, l, c, v FROM bars WHERE source=? AND day=? AND sess=? ORDER BY t",
            conn, params=(source, day, sess),
        )
    return df


def compare_session(old: pd.DataFrame, new: pd.DataFrame, day: str, sess: str) -> dict:
    if old.empty or new.empty:
        return dict(day=day, sess=sess, n_old=len(old), n_new=len(new), n_matched=0)
    merged = old.merge(new, on="t", suffixes=("_old", "_new"))
    if merged.empty:
        return dict(day=day, sess=sess, n_old=len(old), n_new=len(new), n_matched=0)
    diffs = {}
    for col in ("o", "h", "l", "c"):
        d = (merged[f"{col}_old"] - merged[f"{col}_new"]).abs()
        diffs[col] = dict(max_abs_diff=float(d.max()), mean_abs_diff=float(d.mean()),
                           n_exact_match=int((d < 0.5).sum()), n_total=len(d))
    return dict(day=day, sess=sess, n_old=len(old), n_new=len(new), n_matched=len(merged), diffs=diffs)


def main() -> None:
    overlap_days = load_overlap_days()
    if not overlap_days:
        print("找不到重疊日期，無法驗證（檢查 build script 是否已跑過、日期範圍是否真的重疊）")
        sys.exit(1)

    sample_days = overlap_days[::SAMPLE_STRIDE]
    print(f"重疊區間: {overlap_days[0]} ~ {overlap_days[-1]}  共 {len(overlap_days)} 天")
    print(f"抽樣 {len(sample_days)} 天做日盤+夜盤比對\n")

    results = []
    for day in sample_days:
        for sess in ("day", "night"):
            old = load_source_day(OLD_SOURCE, day, sess)
            new = load_source_day(NEW_SOURCE, day, sess)
            cmp = compare_session(old, new, day, sess)
            results.append(cmp)
            if cmp["n_matched"] > 0:
                cd = cmp["diffs"]["c"]
                print(f"{day} [{sess}]: 配對{cmp['n_matched']}/{cmp['n_old']}(old)/{cmp['n_new']}(new)分鐘  "
                      f"Close最大差={cd['max_abs_diff']:.1f}pt  平均差={cd['mean_abs_diff']:.3f}pt  "
                      f"完全一致={cd['n_exact_match']}/{cd['n_total']}")
            else:
                print(f"{day} [{sess}]: 無法配對 (n_old={cmp['n_old']}, n_new={cmp['n_new']})")

    valid = [r for r in results if r.get("n_matched", 0) > 0]
    all_close_mean = [r["diffs"]["c"]["mean_abs_diff"] for r in valid]
    all_close_max = [r["diffs"]["c"]["max_abs_diff"] for r in valid]

    print(f"\n=== 彙總（{len(valid)}/{len(results)} session 有效比對，"
          f"涵蓋 {len(sample_days)} 天 × 2 session）===")
    if all_close_mean:
        print(f"Close 平均差的分布: mean={np.mean(all_close_mean):.3f}pt  "
              f"median={np.median(all_close_mean):.3f}pt  max={np.max(all_close_mean):.3f}pt")
        print(f"單一bar最大差: {np.max(all_close_max):.1f}pt")
        good_days = sum(1 for m in all_close_mean if m <= 2.0)
        print(f"平均差 <= 2pt 的 session 數: {good_days}/{len(all_close_mean)}")

    failed = [r for r in results if r.get("n_matched", 0) == 0]
    if failed:
        print(f"\n無法配對的 session（{len(failed)}）:")
        for r in failed:
            print(f"  {r}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "validate_582d_dataset_results.json"
    out_path.write_text(json.dumps(dict(
        overlap_range=[overlap_days[0], overlap_days[-1]],
        n_overlap_days=len(overlap_days),
        sample_days=sample_days,
        results=results,
        summary=dict(
            n_sessions_compared=len(valid),
            close_mean_diff_mean=float(np.mean(all_close_mean)) if all_close_mean else None,
            close_mean_diff_median=float(np.median(all_close_mean)) if all_close_mean else None,
            close_max_diff=float(np.max(all_close_max)) if all_close_max else None,
        ),
    ), indent=2, ensure_ascii=False, default=str))
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
