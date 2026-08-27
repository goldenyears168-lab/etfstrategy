#!/usr/bin/env python3
"""進場早期特徵 v2 — 突破盲點輪。

上一輪(entry_features.py + atr_filter_walkforward.py)的三個盲點，本輪逐一修正：

盲點1（最關鍵）：**veto濾網 vs sizing 的路徑依賴矛盾**。這是 always-in 翻倉系統——
  「擋掉一次訊號」會讓部位延後轉換，之後每一筆的進出場價全部改變（Fold1 veto版
  218筆→167筆，且剩下的167筆不是原218筆的子集）。60/40特徵分析的「+34%」是
  **事後挑交易**（trade list固定、只是排除），veto 5摺的「+0.9%」是**路徑改變**——
  兩者不是同一個操作，前者的預測力無法用veto收割。**正確收割方式是部位大小**：
  訊號照翻（路徑完全不變），特徵好的訊號1.5倍、差的0.5倍（均值≈1倍，總曝險不變）
  ——sizing不改變交易序列，per-trade預測力可以直接兌現。

盲點2：舊 n_sleeves_agree 用 ±5分鐘窗（abs），把「未來5分鐘才觸發的sleeve」也算進
  「同意數」——look-ahead。本輪改為只算 [t-5min, t]（過去與同分鐘）。

盲點3：舊流程是「60/40單切篩特徵→只對贏家做5摺」——贏家(atr)後來5摺現形。本輪
  **所有特徵從第一步就用5摺OOS Spearman IC篩**，要求≥4/5摺同號才算候選。

新特徵（全部進場當下可知）：
  mins_since_open / mins_to_end     — 場內時間位置（日盤300根、夜盤540根，時程已知）
  width_pts / width_atr             — 通道寬度（絕對點數 / ATR正規化）：寬=剛走完大波段
  overext_z                         — 突破方向的過度延伸((C-SMA60)/ATR，方向化)
  atr_slope                         — ATR相對30根前的斜率（波動擴張中vs收縮中）
  width_ratio                       — 通道寬度相對30根前（收縮=壓縮後突破）
  prior_pnl                         — 同(day,sess)上一筆已平倉交易的pnl（策略自身動能）
  confirmed_by_other（因果版）       — 其他sleeve在[t-5min,t]內已同向觸發
  direction / window / sess         — 類別變數
舊特徵一併重篩：breakout_dist_atr / momentum_10bar / volume_ratio / rsi_at_entry / atr_at_entry
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_daynight_split import load_day_bars_with_sess  # noqa: E402
from tx_channel_geometry_control import ATR_PERIOD, RSI_PERIOD, calculate_atr, calculate_rsi  # noqa: E402
from tx_channel_geometry_multiday import COST_PTS_PER_TRADE, FILL_LAG_BARS, load_days  # noqa: E402
from tx_channel_geometry_realism_check import simulate_pnl_realistic  # noqa: E402
from tx_channel_recalibrate import compute_global_atr_threshold  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
COOLDOWN = 8
SLEEVES = [34, 55, 89]

NUMERIC_FEATURES = [
    "mins_since_open", "mins_to_end", "width_pts", "width_atr", "overext_z",
    "atr_slope", "width_ratio", "prior_pnl",
    "breakout_dist_atr", "momentum_10bar", "volume_ratio", "rsi_at_entry", "atr_at_entry",
]


def run_sleeve_v2(df: pd.DataFrame, window: int, atr_threshold: float, sess: str, day: str) -> list[dict]:
    if len(df) < window + ATR_PERIOD + RSI_PERIOD:
        return []
    ds = calculate_rsi(df, RSI_PERIOD)
    ds["Upper"] = ds["High"].rolling(window).max()
    ds["Lower"] = ds["Low"].rolling(window).min()
    ds[["Upper", "Lower"]] = ds[["Upper", "Lower"]].shift(1)
    ds = calculate_atr(ds, ATR_PERIOD)
    ds["VolAvg20"] = ds["Volume"].rolling(20).mean().shift(1)
    ds["Mom10"] = ds["Close"] - ds["Close"].shift(10)
    ds["SMA60"] = ds["Close"].rolling(60).mean().shift(1)
    ds["bar_pos"] = np.arange(len(ds))
    n_sess_bars = len(ds)
    ds["Width"] = ds["Upper"] - ds["Lower"]
    ds["Width_prev30"] = ds["Width"].shift(30)
    ds["ATR_prev30"] = ds["ATR"].shift(30)
    # dropna 條件跟 baseline 完全一致（新特徵欄位不加入，NaN留給IC階段各自處理），
    # 確保交易路徑與第十一輪 bit-for-bit 相同
    ds = ds.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)
    if len(ds) < 20:
        return []

    def snap_features(i: int, is_short_entry: bool) -> dict:
        price = ds["Close"].iat[i]
        atr = ds["ATR"].iat[i]
        sma = ds["SMA60"].iat[i]
        if is_short_entry:
            bdist = price - ds["Upper"].iat[i - 1]
            overext = (price - sma) / atr if (pd.notna(sma) and atr) else np.nan
        else:
            bdist = ds["Lower"].iat[i - 1] - price
            overext = (sma - price) / atr if (pd.notna(sma) and atr) else np.nan
        vol_avg = ds["VolAvg20"].iat[i]
        w = ds["Width"].iat[i]
        w30 = ds["Width_prev30"].iat[i]
        a30 = ds["ATR_prev30"].iat[i]
        return dict(
            breakout_dist_atr=bdist / atr if atr else np.nan,
            momentum_10bar=ds["Mom10"].iat[i],
            volume_ratio=ds["Volume"].iat[i] / vol_avg if (pd.notna(vol_avg) and vol_avg) else np.nan,
            rsi_at_entry=ds["RSI"].iat[i],
            atr_at_entry=atr,
            mins_since_open=float(ds["bar_pos"].iat[i]),
            mins_to_end=float(n_sess_bars - ds["bar_pos"].iat[i]),
            width_pts=w,
            width_atr=w / atr if atr else np.nan,
            overext_z=overext,
            atr_slope=atr / a30 if (pd.notna(a30) and a30) else np.nan,
            width_ratio=w / w30 if (pd.notna(w30) and w30) else np.nan,
        )

    ds["Signal"] = 1
    short, last_entry = False, -COOLDOWN
    feats_by_idx: dict[int, dict] = {}
    for i in range(1, len(ds)):
        price = ds["Close"].iat[i]
        if ds["ATR"].iat[i] < atr_threshold:
            ds.iat[i, ds.columns.get_loc("Signal")] = ds["Signal"].iat[i - 1]
            continue
        if (not short) and (i - last_entry >= COOLDOWN) and price > ds["Upper"].iat[i - 1]:
            ds.iat[i, ds.columns.get_loc("Signal")] = -1
            short = True
            last_entry = i
            feats_by_idx[i] = snap_features(i, True)
        else:
            exit_cond = price < ds["Lower"].iat[i - 1]
            if short and exit_cond:
                short = False
                ds.iat[i, ds.columns.get_loc("Signal")] = 1
                feats_by_idx[i] = snap_features(i, False)
            else:
                ds.iat[i, ds.columns.get_loc("Signal")] = ds["Signal"].iat[i - 1]

    # 損益結算與 baseline 同一支函式；再把訊號bar的特徵貼回對應的下一筆交易
    _, trades = simulate_pnl_realistic(ds, fill_lag_bars=FILL_LAG_BARS, cost_pts_per_trade=COST_PTS_PER_TRADE)

    # simulate_pnl_realistic 的翻倉順序 == Signal 翻轉順序；逐筆對應訊號索引
    flip_idxs = sorted(feats_by_idx.keys())
    # 第一筆 trade 的「進場」是初始合成部位（沒有對應訊號特徵）；第k筆(k>=2)的進場
    # 對應第k-1個flip。exit對應第k個flip。特徵掛「進場」那個flip。
    out = []
    for k, tr in enumerate(trades):
        feat = feats_by_idx.get(flip_idxs[k - 1]) if 1 <= k <= len(flip_idxs) else None
        out.append(dict(window=window, sess=sess, day=day, **tr, **(feat or {})))
    return out


def collect(days, all_bars, atr_threshold) -> pd.DataFrame:
    rows = []
    for day in days:
        for sess in ("day", "night"):
            seg = all_bars[day][all_bars[day]["sess"] == sess].reset_index(drop=True)
            for w in SLEEVES:
                rows.extend(run_sleeve_v2(seg, w, atr_threshold, sess, day))
    df = pd.DataFrame(rows)

    # prior_pnl：同(day,sess)裡「exit_time <= 本筆entry_time」的最後一筆已平倉交易pnl
    df = df.sort_values(["day", "sess", "entry_time"]).reset_index(drop=True)
    prior = []
    for _, row in df.iterrows():
        grp = df[(df["day"] == row["day"]) & (df["sess"] == row["sess"]) & (df["exit_time"] <= row["entry_time"])]
        prior.append(grp["pnl"].iloc[-1] if len(grp) else np.nan)
    df["prior_pnl"] = prior

    # confirmed_by_other（因果版）：其他window、同向、entry_time ∈ [t-5min, t]
    confirmed = []
    for _, row in df.iterrows():
        others = df[(df["day"] == row["day"]) & (df["sess"] == row["sess"]) &
                    (df["direction"] == row["direction"]) & (df["window"] != row["window"])]
        past5 = others[(others["entry_time"] <= row["entry_time"]) &
                       (others["entry_time"] >= row["entry_time"] - pd.Timedelta(minutes=5))]
        confirmed.append(int(len(past5) > 0))
    df["confirmed_by_other"] = confirmed
    return df


def main() -> None:
    days = load_days()
    all_bars = {d: load_day_bars_with_sess(d) for d in days}
    stripped = {d: all_bars[d][["Datetime", "Open", "High", "Low", "Close", "Volume"]] for d in days}
    atr_threshold = compute_global_atr_threshold(days, stripped)

    print("收集含v2特徵的完整交易明細...")
    df = collect(days, all_bars, atr_threshold)
    print(f"總筆數: {len(df)}  （有進場特徵的: {df['atr_at_entry'].notna().sum()}）")

    n = len(days)
    bounds = [30, 40, 50, 60, 70, n]
    folds = [(days[:bounds[i]], days[bounds[i]:bounds[i + 1]]) for i in range(len(bounds) - 1)]

    # ---- 5摺 OOS Spearman IC 篩選（所有特徵一視同仁，不先用單切分挑贏家）----
    print("\n=== 5摺 OOS Spearman IC（特徵 vs 每筆pnl）===")
    print(f"{'feature':>18s}  " + "  ".join(f"F{i+1:>6d}" for i in range(5)) + "   同號摺數")
    ic_table = {}
    for feat in NUMERIC_FEATURES:
        ics = []
        for is_days, oos_days in folds:
            sub = df[df["day"].isin(oos_days)].dropna(subset=[feat, "pnl"])
            if len(sub) < 30:
                ics.append(np.nan)
                continue
            ic, _ = stats.spearmanr(sub[feat], sub["pnl"])
            ics.append(ic)
        arr = np.array(ics)
        valid = arr[~np.isnan(arr)]
        sign_consist = max((valid > 0).sum(), (valid < 0).sum()) if len(valid) else 0
        ic_table[feat] = dict(ics=ics, consist=int(sign_consist))
        print(f"{feat:>18s}  " + "  ".join(f"{x:+.3f}" if not np.isnan(x) else "   nan" for x in ics)
              + f"      {sign_consist}/5")

    # ---- 類別特徵：逐摺OOS分組均pnl ----
    print("\n=== 類別特徵：逐摺 OOS 分組均pnl ===")
    for cat in ("direction", "sess", "window", "confirmed_by_other"):
        lines = {}
        for fi, (is_days, oos_days) in enumerate(folds):
            sub = df[df["day"].isin(oos_days)]
            g = sub.groupby(cat)["pnl"].mean()
            for k, v in g.items():
                lines.setdefault(k, [np.nan] * 5)[fi] = v
        for k, vals in lines.items():
            print(f"  {cat}={k}: " + "  ".join(f"{x:+7.1f}" if not np.isnan(x) else "    nan" for x in vals))

    # ---- sizing 收割測試：對同號摺數>=4的候選，加上atr_at_entry（veto失敗的平反測試）----
    candidates = [f for f, v in ic_table.items() if v["consist"] >= 4]
    if "atr_at_entry" not in candidates:
        candidates.append("atr_at_entry")
    print(f"\n=== sizing收割測試（路徑不變，特徵好1.5倍/差0.5倍，IS決定方向與門檻）===")
    print(f"候選: {candidates}")

    sizing_results = {}
    for feat in candidates:
        fold_rows = []
        for fi, (is_days, oos_days) in enumerate(folds):
            is_sub = df[df["day"].isin(is_days)].dropna(subset=[feat, "pnl"])
            oos_sub = df[df["day"].isin(oos_days)].copy()
            if len(is_sub) < 30:
                continue
            is_ic, _ = stats.spearmanr(is_sub[feat], is_sub["pnl"])
            sign = 1.0 if is_ic >= 0 else -1.0
            med = is_sub[feat].median()

            def size_of(x):
                if pd.isna(x):
                    return 1.0
                return 1.5 if (x - med) * sign > 0 else 0.5

            oos_sub["size"] = oos_sub[feat].map(size_of)
            base = oos_sub["pnl"].sum()
            sized = (oos_sub["size"] * oos_sub["pnl"]).sum()
            fold_rows.append(dict(fold=fi + 1, base=base, sized=sized,
                                    mean_size=oos_sub["size"].mean(),
                                    lift_pct=(sized / base - 1) * 100 if base else np.nan))
        n_improved = sum(1 for r in fold_rows if r["sized"] > r["base"])
        tot_base = sum(r["base"] for r in fold_rows)
        tot_sized = sum(r["sized"] for r in fold_rows)
        sizing_results[feat] = dict(rows=fold_rows, n_improved=n_improved,
                                      tot_base=tot_base, tot_sized=tot_sized)
        detail = "  ".join(f"F{r['fold']}:{r['lift_pct']:+.1f}%" for r in fold_rows)
        print(f"{feat:>18s}: {n_improved}/5摺改善  合計 {tot_base:,.0f}→{tot_sized:,.0f}pt "
              f"({100 * (tot_sized / tot_base - 1):+.1f}%)   [{detail}]")

    df.to_csv(OUT_DIR / "entry_features_v2_trades.csv", index=False)

    # 圖：IC一致性 + 最佳sizing候選逐摺
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            break
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(17, 6))
    feats = list(ic_table.keys())
    mean_ics = [np.nanmean(ic_table[f]["ics"]) for f in feats]
    consist = [ic_table[f]["consist"] for f in feats]
    colors = ["#1F8A65" if c >= 4 else "#7F8C8D" for c in consist]
    ax1.barh(feats, mean_ics, color=colors)
    ax1.axvline(0, color="gray", linewidth=0.8)
    ax1.set_title("5摺OOS平均Spearman IC（綠=同號≥4/5摺）")
    ax1.set_xlabel("mean IC")

    if sizing_results:
        best = max(sizing_results, key=lambda f: sizing_results[f]["n_improved"])
        rows = sizing_results[best]["rows"]
        x = np.arange(len(rows))
        width_bar = 0.35
        ax2.bar(x - width_bar / 2, [r["base"] for r in rows], width_bar, label="均一倉位", color="#7F8C8D")
        ax2.bar(x + width_bar / 2, [r["sized"] for r in rows], width_bar, label=f"{best} sizing", color="#1F8A65")
        ax2.axhline(0, color="gray", linewidth=0.8)
        ax2.set_xticks(x)
        ax2.set_xticklabels([f"Fold{r['fold']}" for r in rows])
        ax2.set_title(f"sizing收割：{best}（路徑不變，只調口數權重）")
        ax2.set_ylabel("OOS總損益(pt)")
        ax2.legend()
    fig.tight_layout()
    fig_dir = OUT_DIR / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)
    chart = fig_dir / "entry_features_v2_ic_sizing.png"
    fig.savefig(chart, dpi=120)
    plt.close(fig)
    print(f"\nchart saved: {chart}")


if __name__ == "__main__":
    main()
