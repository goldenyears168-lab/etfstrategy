#!/usr/bin/env python3
"""micro-VCP 假設驗證（TX/TMF 分鐘/秒級，2024-04-09 單日）——

Minervini 經典 VCP：價格收縮進更窄區間、成交量在最窄點乾涸（dry-up），
接著放量突破延續原趨勢。這支腳本把同一結構搬到秒級 tick 資料上做因果式檢查：

  (a) 3分鐘趨勢方向（trailing slope，因果）
  (b) coil：trailing 3 真實秒 成交量 相對 trailing 60秒 baseline 收縮（<50%）
  (c) spike：coil 剛成立後的 1 真實秒 volume 放大（>3x baseline）

對每個 coil+spike 事件，量測未來 1/3/5 分鐘報酬是否「順著」3分鐘趨勢方向（continuation），
對比隨機非事件時點的 baseline hit rate。

因果性：所有 rolling window 用 shift(1) 只看事件當下之前的資料（volume(t) 本身用來判斷
spike 是否成立是唯一用到「當下」的地方——這是事件定義本身，不是預測用的未來資訊）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_tick_validation import load_front_month_ticks  # noqa: E402

DAY = "2024-04-09"
BASELINE_WINDOW_S = 60
COIL_WINDOW_S = 3
COIL_RATIO = 0.5
SPIKE_MULT = 3.0
TREND_WINDOW_S = 180  # 3 分鐘
HORIZONS_S = {"1min": 60, "3min": 180, "5min": 300}
GAP_THRESHOLD_S = 300  # 超過此秒數視為 session 斷點，分段處理避免 baseline 被斷點污染
RNG_SEED = 20240409


def split_raw_ticks_by_gap(ticks: pd.DataFrame) -> list[pd.DataFrame]:
    """先在「原始成交」時間戳上找斷點（>GAP_THRESHOLD_S），切成連續 session 區段
    （日盤 08:45-13:45 / 夜盤空窗前後），避免之後 resample 補零把大段空窗當成
    真實的低量秒灌進 rolling baseline。"""
    ticks = ticks.sort_values("dt")
    gaps = ticks["dt"].diff().dt.total_seconds()
    seg_id = (gaps > GAP_THRESHOLD_S).cumsum()
    return [ticks[seg_id == g] for g in seg_id.unique()]


def build_1s_bars(ticks: pd.DataFrame) -> pd.DataFrame:
    """把單一連續區段的 tick 攤成「每個真實秒」一列：volume=該秒成交量加總、
    price=該秒最後成交價，區段內沒有成交的秒用 volume=0、price 前值填補（因果，
    不使用未來價；區段本身已經是去除大段空窗後的連續盤中/夜盤片段）。"""
    ticks = ticks.set_index("dt").sort_index()
    vol = ticks["volume"].resample("1s").sum()
    px = ticks["price"].resample("1s").last().ffill()
    bars = pd.DataFrame({"volume": vol, "price": px}).dropna(subset=["price"])
    return bars


def detect_events(seg: pd.DataFrame) -> pd.DataFrame:
    v = seg["volume"]
    px = seg["price"]

    baseline60 = v.shift(1).rolling(BASELINE_WINDOW_S, min_periods=BASELINE_WINDOW_S).mean()
    coil3 = v.shift(1).rolling(COIL_WINDOW_S, min_periods=COIL_WINDOW_S).mean()
    coil_flag = (coil3 < COIL_RATIO * baseline60) & (baseline60 > 0)
    spike_flag = (v > SPIKE_MULT * baseline60) & (baseline60 > 0)
    event = coil_flag & spike_flag

    # 3 分鐘趨勢：因果 slope，用 trailing 180 秒價格線性回歸符號（因果，不含未來）
    def trailing_slope(s: pd.Series) -> pd.Series:
        n = TREND_WINDOW_S
        x = np.arange(n)
        x_mean = x.mean()
        x_var = ((x - x_mean) ** 2).sum()

        def _slope(y: np.ndarray) -> float:
            return float(((x - x_mean) * (y - y.mean())).sum() / x_var)

        return s.rolling(n, min_periods=n).apply(_slope, raw=True)

    trend_slope = trailing_slope(px)

    out = pd.DataFrame({
        "price": px, "volume": v, "baseline60": baseline60, "coil3": coil3,
        "coil_flag": coil_flag, "spike_flag": spike_flag, "event": event,
        "trend_slope": trend_slope,
    })

    for name, h in HORIZONS_S.items():
        fut_px = px.shift(-h)
        out[f"fwd_ret_{name}"] = fut_px - px

    return out


def continuation_hits(df: pd.DataFrame, rows: pd.Index) -> dict:
    res = {}
    sub = df.loc[rows]
    trend_sign = np.sign(sub["trend_slope"])
    valid_trend = trend_sign != 0
    for name in HORIZONS_S:
        ret_sign = np.sign(sub[f"fwd_ret_{name}"])
        mask = valid_trend & sub[f"fwd_ret_{name}"].notna() & sub["trend_slope"].notna()
        if mask.sum() == 0:
            res[name] = dict(hit_rate=None, avg_abs_ret=None, n=0)
            continue
        hits = (ret_sign[mask] == trend_sign[mask]).mean()
        avg_abs = sub.loc[mask, f"fwd_ret_{name}"].abs().mean()
        res[name] = dict(hit_rate=float(hits), avg_abs_ret=float(avg_abs), n=int(mask.sum()))
    return res


def main() -> None:
    ticks = load_front_month_ticks(DAY)
    if ticks is None or ticks.empty:
        print(f"{DAY}: 無 tick 資料")
        return

    raw_segments = split_raw_ticks_by_gap(ticks)
    bars_segments = [build_1s_bars(s) for s in raw_segments if not s.empty]
    total_seconds = sum(len(b) for b in bars_segments)
    print(f"{DAY}: {total_seconds} 真實秒（含成交/補值），切成 {len(bars_segments)} 個連續區段"
          f"（依日夜盤空窗切點分段，避免空窗污染 baseline）")

    all_feat = []
    for seg in bars_segments:
        if len(seg) < BASELINE_WINDOW_S + TREND_WINDOW_S:
            continue
        feat = detect_events(seg)
        all_feat.append(feat)
    if not all_feat:
        print("所有區段長度不足，無法計算")
        return
    feat = pd.concat(all_feat)

    event_rows = feat.index[feat["event"]]
    n_events = len(event_rows)
    print(f"\ncoil+spike 事件數（全日）：{n_events}")

    if n_events == 0:
        print("0 個事件 — 當天無法做任何 signal-check（不是「no edge」，是樣本不存在）。")
        return

    event_stats = continuation_hits(feat, event_rows)

    valid_rows = feat.index[feat["trend_slope"].notna() & feat["baseline60"].notna()]
    rng = np.random.default_rng(RNG_SEED)
    n_baseline = min(len(valid_rows), max(500, n_events * 50))
    baseline_rows = pd.Index(rng.choice(valid_rows, size=n_baseline, replace=False))
    baseline_stats = continuation_hits(feat, baseline_rows)

    print(f"\n{'horizon':<8}{'event_hit':<12}{'event_n':<9}{'event_|ret|':<13}"
          f"{'base_hit':<11}{'base_n':<9}{'base_|ret|':<11}")
    for name in HORIZONS_S:
        e, b = event_stats[name], baseline_stats[name]
        e_hit = f"{e['hit_rate']:.3f}" if e["hit_rate"] is not None else "NA"
        b_hit = f"{b['hit_rate']:.3f}" if b["hit_rate"] is not None else "NA"
        e_ret = f"{e['avg_abs_ret']:.2f}" if e["avg_abs_ret"] is not None else "NA"
        b_ret = f"{b['avg_abs_ret']:.2f}" if b["avg_abs_ret"] is not None else "NA"
        print(f"{name:<8}{e_hit:<12}{e['n']:<9}{e_ret:<13}{b_hit:<11}{b['n']:<9}{b_ret:<11}")

    if n_events < 5:
        print(f"\n⚠️ n={n_events} 個事件 < 5：單日樣本不足以做任何有意義的 signal-check，"
              f"以下數字僅供參考，不能得出「有/無 edge」結論。")

    out_path = Path(__file__).resolve().parents[2] / "reports" / "research" / \
        "tx-donchian-regime" / f"microvcp_coil_spike_{DAY}.json"
    import json
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "day": DAY, "n_events": n_events,
        "event_stats": event_stats, "baseline_stats": baseline_stats,
        "n_baseline_sample": n_baseline,
    }, indent=2, default=str))
    print(f"\n結果寫入 {out_path}")


if __name__ == "__main__":
    main()
