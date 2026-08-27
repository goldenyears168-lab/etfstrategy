#!/usr/bin/env python3
"""進場當下的早期特徵能不能預測這筆交易最後會是「陡峭」（賺）還是「平緩」（賠）——
呼應上一輪16分類發現的結構、以及 H-SC-CAUSAL-LAG 的教訓（事後分類準、即時分類難）。

候選特徵（全部只用「訊號確認那一根bar」以前/當下的資訊，不看之後）：
  - breakout_dist_atr：突破幅度相對ATR正規化（(收盤-前一根通道邊界)/ATR）——
    衝破通道多遠，理論上「衝得越狠」代表越果斷的動能，猜測跟後續走勢陡峭度正相關
  - momentum_10bar：進場前10根bar的動能（訊號bar收盤 - 10bar前收盤）
  - volume_ratio：訊號bar成交量 / 前20bar均量——量大的突破理論上更有支撐
  - rsi_at_entry：進場當下RSI水準
  - atr_at_entry：進場當下ATR絕對值（波動度本身）
  - n_sleeves_agree：同一時間窗內，34/55/89三個sleeve裡有幾個同方向觸發
    （呼應第十輪「72.7%同方向重疊」的發現，猜測多sleeve同意可能代表更強的訊號）

驗證方式：83天依時間序切60/40（訓練期找規律、held-out期純驗證），只在訓練期上找
「這個特徵能不能分出好交易/壞交易」的規則，然後在held-out期上原封不動套用同一組門檻，
看關係是否還在——不能只看樣本內相關性就下結論。
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tx_channel_daynight_split import compute_atr_threshold_for_days, load_day_bars_with_sess  # noqa: E402
from tx_channel_geometry_control import ATR_PERIOD, RSI_PERIOD, calculate_atr, calculate_rsi  # noqa: E402
from tx_channel_geometry_multiday import COST_PTS_PER_TRADE, FILL_LAG_BARS, load_days  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "tx-donchian-regime"
COOLDOWN = 8
SLEEVES = [34, 55, 89]
TRAIN_FRACTION = 0.60


def run_sleeve_with_features(df: pd.DataFrame, window: int, atr_threshold: float, sess: str, day: str) -> list[dict]:
    if len(df) < window + ATR_PERIOD + RSI_PERIOD:
        return []
    dataset = calculate_rsi(df, RSI_PERIOD)
    dataset["Upper"] = dataset["High"].rolling(window).max()
    dataset["Lower"] = dataset["Low"].rolling(window).min()
    dataset[["Upper", "Lower"]] = dataset[["Upper", "Lower"]].shift(1)
    dataset = calculate_atr(dataset, ATR_PERIOD)
    dataset["VolAvg20"] = dataset["Volume"].rolling(20).mean().shift(1)
    dataset["Mom10"] = dataset["Close"] - dataset["Close"].shift(10)
    dataset = dataset.dropna(subset=["Upper", "Lower", "RSI", "ATR"]).reset_index(drop=True)
    if len(dataset) < 20:
        return []

    dataset["Signal"] = 1
    short, last_entry = False, -COOLDOWN
    entries = []  # 記錄每次訊號翻轉當下的特徵快照

    for i in range(1, len(dataset)):
        price = dataset["Close"].iat[i]
        if dataset["ATR"].iat[i] < atr_threshold:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]
            continue
        if (not short) and (i - last_entry >= COOLDOWN) and price > dataset["Upper"].iat[i - 1]:
            dataset.iat[i, dataset.columns.get_loc("Signal")] = -1
            short = True
            last_entry = i
            entries.append(dict(
                idx=i, direction="short",
                breakout_dist=price - dataset["Upper"].iat[i - 1],
                atr=dataset["ATR"].iat[i], rsi=dataset["RSI"].iat[i],
                momentum_10bar=dataset["Mom10"].iat[i],
                volume_ratio=(dataset["Volume"].iat[i] / dataset["VolAvg20"].iat[i]
                              if dataset["VolAvg20"].iat[i] else np.nan),
                signal_time=dataset["Datetime"].iat[i],
            ))
        else:
            exit_cond = price < dataset["Lower"].iat[i - 1]
            if short and exit_cond:
                short = False
                dataset.iat[i, dataset.columns.get_loc("Signal")] = 1
                entries.append(dict(
                    idx=i, direction="long",
                    breakout_dist=dataset["Lower"].iat[i - 1] - price,
                    atr=dataset["ATR"].iat[i], rsi=dataset["RSI"].iat[i],
                    momentum_10bar=dataset["Mom10"].iat[i],
                    volume_ratio=(dataset["Volume"].iat[i] / dataset["VolAvg20"].iat[i]
                                  if dataset["VolAvg20"].iat[i] else np.nan),
                    signal_time=dataset["Datetime"].iat[i],
                ))
            else:
                dataset.iat[i, dataset.columns.get_loc("Signal")] = dataset["Signal"].iat[i - 1]

    # ---- 用跟 simulate_pnl_realistic 相同的延遲1根開盤成交邏輯，把 entries 轉成完整trade（含pnl）----
    trades = []
    n = len(dataset)
    pos_dir = dataset["Signal"].iat[0]
    fill_idx0 = min(FILL_LAG_BARS, n - 1)
    entry_price = dataset["Open"].iat[fill_idx0]
    entry_time = dataset["Datetime"].iat[fill_idx0]
    entry_feat = None
    entry_ptr = 0
    pending = None

    for i in range(1, n):
        sig = dataset["Signal"].iat[i]
        if pending is not None and i == pending["fill_at"]:
            fill_price = dataset["Open"].iat[i]
            pnl_pts = (fill_price - entry_price) if pos_dir == 1 else (entry_price - fill_price)
            pnl = pnl_pts - COST_PTS_PER_TRADE
            trades.append(dict(
                window=window, sess=sess, day=day, direction="long" if pos_dir == 1 else "short",
                entry_time=entry_time, exit_time=dataset["Datetime"].iat[i], pnl=pnl,
                **(entry_feat or {}),
            ))
            pos_dir = pending["new_dir"]
            entry_price = fill_price
            entry_time = dataset["Datetime"].iat[i]
            entry_feat = pending["feat"]
            pending = None
        if sig != pos_dir and pending is None:
            while entry_ptr < len(entries) and entries[entry_ptr]["idx"] < i:
                entry_ptr += 1
            feat = None
            if entry_ptr < len(entries) and entries[entry_ptr]["idx"] == i:
                e = entries[entry_ptr]
                feat = dict(breakout_dist=e["breakout_dist"], atr_at_entry=e["atr"],
                            rsi_at_entry=e["rsi"], momentum_10bar=e["momentum_10bar"],
                            volume_ratio=e["volume_ratio"])
                feat["breakout_dist_atr"] = feat["breakout_dist"] / feat["atr_at_entry"] if feat["atr_at_entry"] else np.nan
            pending = dict(fill_at=min(i + FILL_LAG_BARS, n - 1), new_dir=sig, feat=feat)

    return trades


def main() -> None:
    days = load_days()
    all_bars = {d: load_day_bars_with_sess(d) for d in days}
    atr_threshold = compute_atr_threshold_for_days(days, all_bars)

    print("收集含進場特徵的完整交易明細...")
    all_trades = []
    for day in days:
        for sess in ("day", "night"):
            seg = all_bars[day][all_bars[day]["sess"] == sess].reset_index(drop=True)
            for w in SLEEVES:
                all_trades.extend(run_sleeve_with_features(seg, w, atr_threshold, sess, day))

    df = pd.DataFrame(all_trades).dropna(subset=["breakout_dist_atr", "momentum_10bar", "volume_ratio", "rsi_at_entry"])
    print(f"總筆數(含完整特徵): {len(df)}")

    # ---- 多sleeve同意度：同一天+session+方向，訊號時間相差<=5分鐘內算「同意」----
    df = df.sort_values(["day", "sess", "entry_time"]).reset_index(drop=True)
    agree_counts = []
    for _, row in df.iterrows():
        same = df[(df["day"] == row["day"]) & (df["sess"] == row["sess"]) & (df["direction"] == row["direction"])]
        close_by = same[(same["entry_time"] - row["entry_time"]).abs() <= pd.Timedelta(minutes=5)]
        agree_counts.append(len(close_by))
    df["n_sleeves_agree"] = agree_counts

    # ---- 60/40 時間序切分 ----
    split_day = days[int(len(days) * TRAIN_FRACTION)]
    train = df[df["day"] < split_day].copy()
    test = df[df["day"] >= split_day].copy()
    print(f"train={len(train)}筆({train['day'].min()}~{train['day'].max()})  "
          f"test={len(test)}筆({test['day'].min()}~{test['day'].max()})")

    features = ["breakout_dist_atr", "momentum_10bar", "volume_ratio", "rsi_at_entry", "atr_at_entry", "n_sleeves_agree"]

    print("\n=== 各特徵：train期切4分位，看每組平均pnl是否單調；同一組門檻套到test期驗證 ===")
    results = {}
    for feat in features:
        try:
            train_q = pd.qcut(train[feat], 4, labels=["Q1(最低)", "Q2", "Q3", "Q4(最高)"], duplicates="drop")
        except ValueError:
            print(f"{feat}: 值太集中無法切4分位，跳過")
            continue
        bin_edges = pd.qcut(train[feat], 4, retbins=True, duplicates="drop")[1]
        train_summary = train.groupby(train_q, observed=True)["pnl"].agg(["mean", "count"])

        test_q = pd.cut(test[feat], bins=bin_edges, labels=train_summary.index[:len(bin_edges) - 1], include_lowest=True)
        test_summary = test.groupby(test_q, observed=True)["pnl"].agg(["mean", "count"])

        print(f"\n--- {feat} ---")
        print("train:", dict(train_summary["mean"].round(1)))
        print("test :", dict(test_summary["mean"].round(1)))
        results[feat] = dict(train=train_summary, test=test_summary)

    # ---- baseline 對照 ----
    print(f"\n整體 train 平均pnl: {train['pnl'].mean():.2f}  test 平均pnl: {test['pnl'].mean():.2f}")

    # 圖
    _use_cjk = False
    available = {f.name for f in fm.fontManager.ttflist}
    for name in ("PingFang TC", "PingFang HK", "Heiti TC", "Arial Unicode MS", "Noto Sans CJK TC"):
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            break

    fig, axes = plt.subplots(2, 3, figsize=(17, 9))
    for ax, feat in zip(axes.flat, features):
        if feat not in results:
            continue
        tr = results[feat]["train"]["mean"]
        te = results[feat]["test"]["mean"]
        x = np.arange(len(tr))
        ax.bar(x - 0.2, tr.values, 0.4, label="train", color="#2C3E50")
        ax.bar(x + 0.2, te.reindex(tr.index).values, 0.4, label="test(held-out)", color="#C0392B")
        ax.axhline(0, color="gray", linewidth=0.8)
        ax.set_xticks(x)
        ax.set_xticklabels(tr.index, fontsize=8)
        ax.set_title(feat)
        ax.legend(fontsize=8)
    fig.suptitle("進場早期特徵 vs 平均pnl（train找規律 → test驗證是否還在）")
    fig.tight_layout()
    fig_dir = OUT_DIR / "figs"
    fig_dir.mkdir(parents=True, exist_ok=True)
    chart_path = fig_dir / "entry_features_train_test.png"
    fig.savefig(chart_path, dpi=120)
    plt.close(fig)
    print(f"\nchart saved: {chart_path}")

    df.to_csv(OUT_DIR / "entry_features_trades.csv", index=False)


if __name__ == "__main__":
    main()
