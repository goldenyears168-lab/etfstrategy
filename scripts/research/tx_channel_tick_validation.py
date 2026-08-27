#!/usr/bin/env python3
"""tick級驗證——channel_lab 自己的研究史反覆證明「同根K棒/bar級近似」是這個 repo 裡
最常見的假edge來源（H-SC-TICK-TRIGGER 那輪光是把確認粒度從bar改tick就讓數字縮水42%；
ASC channel線更誇張，11輪、200k+組合，最後靠tick reweigh才找到唯一沒被推翻的版本）。

這支腳本對 w233/多window組合做兩層tick級檢查（範圍：日盤，因為夜盤在 tick cache 裡跨
午夜、要拼接兩個日期檔案，這輪先不做，於caveats揭露）：

1. 資料完整性：現有 tx_1m_daynight_cache.json 的1分K，跟直接從原始tick重新resample出來
   的1分K，Open/High/Low/Close 是否一致——如果快取本身就跟真實成交不符，後面所有回測都
   建立在錯的地基上。
2. 成交假設檢查：本策略架構是「訊號bar收盤觸發、下一根bar開盤成交」（不是像H-SC那條線
   靠intrabar觸價stop/target），所以不會有『同一根K棒內stop/target先後順序』這種傳統
   look-ahead陷阱；但驗證『下一根bar的Open』是否真的是一個可達成的單一tick價格（不是
   合成/內插值），確認next-bar-open這個成交假設本身站得住腳。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_geometry_multiday import load_days  # noqa: E402

TICK_DIR = Path("/Users/jackm4/goldenstocks-data/cache/tmf_channel/finmind_tx_tick_by_day")
OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
DAY_SESSION_START = "08:45:00"
DAY_SESSION_END = "13:45:00"


def load_front_month_ticks(date: str) -> pd.DataFrame | None:
    path = TICK_DIR / f"{date}.json"
    if not path.exists():
        return None
    rows = json.loads(path.read_text())
    df = pd.DataFrame(rows)
    df = df[~df["contract_date"].str.contains("/")]
    if df.empty:
        return None
    front = df["contract_date"].value_counts().idxmax()
    df = df[df["contract_date"] == front].copy()
    df["dt"] = pd.to_datetime(df["date"])
    df = df.sort_values("dt")
    return df[["dt", "price", "volume"]]


def resample_to_1min(ticks: pd.DataFrame) -> pd.DataFrame:
    ticks = ticks.set_index("dt")
    day_ticks = ticks.between_time(DAY_SESSION_START, DAY_SESSION_END)
    if day_ticks.empty:
        return pd.DataFrame()
    ohlc = day_ticks["price"].resample("1min").ohlc()
    vol = day_ticks["volume"].resample("1min").sum()
    out = ohlc.join(vol.rename("volume")).dropna(subset=["open"])
    out = out.reset_index().rename(columns={"dt": "Datetime", "open": "Open", "high": "High",
                                              "low": "Low", "close": "Close", "volume": "Volume"})
    return out


def load_cached_day_bars(date: str) -> pd.DataFrame:
    import sqlite3
    from tx_channel_geometry_multiday import DB_PATH, SOURCE
    with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True) as conn:
        df = pd.read_sql_query(
            "SELECT t, o, h, l, c, v FROM bars WHERE source=? AND day=? AND sess='day' ORDER BY t",
            conn, params=(SOURCE, date),
        )
    df = df.rename(columns={"t": "Datetime", "o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
    return df


def compare_bars(cached: pd.DataFrame, tick_derived: pd.DataFrame, date: str) -> dict:
    cached = cached.copy()
    cached["minute"] = cached["Datetime"]
    tick_derived = tick_derived.copy()
    tick_derived["minute"] = tick_derived["Datetime"].dt.strftime("%H:%M")

    merged = cached.merge(tick_derived, on="minute", suffixes=("_cache", "_tick"))
    if merged.empty:
        return dict(date=date, n_matched_minutes=0)

    diffs = {}
    for col in ("Open", "High", "Low", "Close"):
        d = (merged[f"{col}_cache"] - merged[f"{col}_tick"]).abs()
        diffs[col] = dict(max_abs_diff=float(d.max()), mean_abs_diff=float(d.mean()),
                           n_exact_match=int((d < 0.5).sum()), n_total=len(d))
    return dict(date=date, n_cache_minutes=len(cached), n_tick_minutes=len(tick_derived),
                n_matched_minutes=len(merged), diffs=diffs)


def main() -> None:
    days = load_days()
    sample_days = days[::5]  # 每5天抽1天，約17天樣本，涵蓋整個83天區間
    print(f"抽樣 {len(sample_days)} 天做tick級資料完整性驗證（日盤，08:45-13:45）\n")

    results = []
    for date in sample_days:
        ticks = load_front_month_ticks(date)
        if ticks is None:
            print(f"{date}: 無tick資料，略過")
            continue
        tick_bars = resample_to_1min(ticks)
        cache_bars = load_cached_day_bars(date)
        if tick_bars.empty or cache_bars.empty:
            print(f"{date}: 日盤bar數不足，略過")
            continue
        cmp = compare_bars(cache_bars, tick_bars, date)
        results.append(cmp)
        if cmp["n_matched_minutes"] > 0:
            close_diff = cmp["diffs"]["Close"]
            print(f"{date}: 配對{cmp['n_matched_minutes']}/{cmp['n_cache_minutes']}分鐘  "
                  f"Close最大差={close_diff['max_abs_diff']:.1f}pt  平均差={close_diff['mean_abs_diff']:.3f}pt  "
                  f"完全一致={close_diff['n_exact_match']}/{close_diff['n_total']}")

    # 彙總
    all_close_diffs = [r["diffs"]["Close"]["mean_abs_diff"] for r in results if r.get("n_matched_minutes", 0) > 0]
    all_max_diffs = [r["diffs"]["Close"]["max_abs_diff"] for r in results if r.get("n_matched_minutes", 0) > 0]
    print(f"\n=== 彙總（{len(all_close_diffs)}天有效比對）===")
    if all_close_diffs:
        print(f"Close平均差的分布: mean={np.mean(all_close_diffs):.3f}pt  "
              f"median={np.median(all_close_diffs):.3f}pt  max={np.max(all_close_diffs):.1f}pt")
        print(f"單一bar最大差: {np.max(all_max_diffs):.1f}pt")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "tick_validation_results.json"
    out_path.write_text(json.dumps(dict(
        sample_days=sample_days,
        results=results,
        summary=dict(
            n_days_compared=len(all_close_diffs),
            close_mean_diff_mean=float(np.mean(all_close_diffs)) if all_close_diffs else None,
            close_mean_diff_median=float(np.median(all_close_diffs)) if all_close_diffs else None,
            close_max_diff=float(np.max(all_max_diffs)) if all_max_diffs else None,
        ),
        caveats=[
            "只驗證日盤(08:45-13:45)——夜盤在tick cache裡會跨過午夜、分散在兩個calendar date"
            "檔案，需要跨檔拼接，這輪時間有限沒做，留待下一輪。",
            "tick resample用『每分鐘內第一/最高/最低/最後一筆成交價』標準OHLC定義，跟cache"
            "來源(FinMind TaiwanFuturesDaily/其他管道)的1分K定義如果不同，可能造成非"
            "資料錯誤的系統性微小差異(例如tick時間戳的秒數邊界對齊方式)。",
        ],
    ), indent=2, ensure_ascii=False, default=str))
    print(f"\nsaved: {out_path}")


if __name__ == "__main__":
    main()
