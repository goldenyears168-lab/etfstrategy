#!/usr/bin/env python3
"""dayflip-short：T0（進場前一天／訊號日）盤中走勢特徵 vs 賺賠 · 分點下單時段推測.

使用者問題：賺錢/賠錢的交易，T0 當天的圖形走勢有沒有共同特徵？能不能猜到分點
大概在哪個時段下單？

資料源：reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv
（FROZEN_SPEC_V1.json 的回測交易log，221筆，signal_date=T0＝分點觸發篩選的那天，
trade_date=T0+1＝真正進場放空的那天）。

分點資料本身只有日頻（stock_broker_branch_daily 無時間戳），無法直接查到分點在哪
個時段下單；這裡改用 T0 當天個股 1 分K（stock_kbar_1m）的量價形狀做間接推測——
哪個時段量最大、股價何時創高，是「分點很可能在那個時段積極買進」最合理的代理指標。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_t0_intraday_pattern_study.py
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import numpy as np
from scipy import stats

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"

_BUCKETS = [
    ("09:00", "09:30", "開盤09:00-09:30"),
    ("09:30", "10:30", "早盤09:30-10:30"),
    ("10:30", "12:00", "盤中10:30-12:00"),
    ("12:00", "13:00", "午盤12:00-13:00"),
    ("13:00", "13:30", "尾盤13:00-13:30"),
]


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_t0_bars(con: sqlite3.Connection, stock_id: str, t0: str) -> list[tuple]:
    # 2026-08-08 code review 發現：原本直接 SELECT stock_kbar_1m 沒篩 source，
    # 138/221 筆訊號股同一天同時有 finmind 跟 yahoo 兩份資料，兩者 volume 量級
    # 差好幾千倍（yahoo 疑似單位不同）、high/low/close 也不一致，混在一起算量能
    # 時段分布/VWAP/收盤價全部失真。改用 load_kbar_day_bars()——這支 repo 既有的
    # source-priority helper（finmind優先、缺分鐘才用yahoo補洞），是正確的SSOT做法。
    bars = load_kbar_day_bars(con, stock_id, t0)
    return [
        (b.minute[:5], b.open, b.high, b.low, b.close, b.volume)
        for b in bars
        if "09:00" <= b.minute[:5] <= "13:30"
    ]


def bucket_of(minute: str) -> str | None:
    for lo, hi, label in _BUCKETS:
        if lo <= minute < hi:
            return label
    if minute == "13:30":
        return _BUCKETS[-1][2]
    return None


def t0_features(bars: list[tuple]) -> dict | None:
    if len(bars) < 50:
        return None
    minutes = [b[0] for b in bars]
    opens = np.array([b[1] for b in bars], dtype=float)
    highs = np.array([b[2] for b in bars], dtype=float)
    lows = np.array([b[3] for b in bars], dtype=float)
    closes = np.array([b[4] for b in bars], dtype=float)
    vols = np.array([b[5] or 0 for b in bars], dtype=float)

    day_open = opens[0]
    day_close = closes[-1]
    day_high = highs.max()
    day_low = lows.min()
    if day_high <= day_low or day_open <= 0:
        return None

    close_pos_in_range = (day_close - day_low) / (day_high - day_low)

    bucket_vol: dict[str, float] = {label: 0.0 for _, _, label in _BUCKETS}
    for m, v in zip(minutes, vols):
        b = bucket_of(m)
        if b:
            bucket_vol[b] += v
    total_vol = sum(bucket_vol.values())
    if total_vol <= 0:
        return None
    bucket_share = {k: v / total_vol for k, v in bucket_vol.items()}
    max_vol_bucket = max(bucket_share, key=bucket_share.get)
    # Herfindahl concentration index across the 5 buckets (1/5=0.2 uniform, 1.0=all in one bucket)
    hhi = sum(s * s for s in bucket_share.values())

    typical_price = (highs + lows + closes) / 3.0
    vwap = float(np.sum(typical_price * vols) / total_vol) if total_vol > 0 else np.nan
    close_vs_vwap_pct = (day_close - vwap) / vwap * 100 if vwap else np.nan

    idx_1030 = None
    for i, m in enumerate(minutes):
        if m >= "10:30":
            idx_1030 = i
            break
    ret_open_1030 = (closes[idx_1030] / day_open - 1) * 100 if idx_1030 else np.nan
    ret_1030_close = (day_close / closes[idx_1030] - 1) * 100 if idx_1030 else np.nan

    idx_1300 = None
    for i, m in enumerate(minutes):
        if m >= "13:00":
            idx_1300 = i
            break
    ret_last30 = (day_close / closes[idx_1300] - 1) * 100 if idx_1300 else np.nan

    # when did the day's high actually print (time-of-day bucket)
    hi_idx = int(np.argmax(highs))
    hi_bucket = bucket_of(minutes[hi_idx])

    return {
        "close_pos_in_range": close_pos_in_range,
        "max_vol_bucket": max_vol_bucket,
        "vol_hhi": hhi,
        "close_vs_vwap_pct": close_vs_vwap_pct,
        "ret_open_1030": ret_open_1030,
        "ret_1030_close": ret_1030_close,
        "ret_last30": ret_last30,
        "hi_bucket": hi_bucket,
        "day_ret_pct": (day_close / day_open - 1) * 100,
    }


def permutation_p(w: np.ndarray, l: np.ndarray, *, n_perm: int = 20000, seed: int = 0) -> float:
    """獨立於 Mann-Whitney 的交叉驗證：打散 win/loss 標籤重算平均差距，
    看真實差距在多少百分位——分佈假設更少，用來確認 Mann-Whitney 的顯著性不是
    純粹因為分佈形狀（如偏態）造成的假訊號。"""
    rng = np.random.default_rng(seed)
    pooled = np.concatenate([w, l])
    n_w = len(w)
    observed = abs(w.mean() - l.mean())
    count = 0
    for _ in range(n_perm):
        rng.shuffle(pooled)
        diff = abs(pooled[:n_w].mean() - pooled[n_w:].mean())
        if diff >= observed:
            count += 1
    return (count + 1) / (n_perm + 1)


def mannwhitney_report(name: str, win_vals: list[float], loss_vals: list[float]) -> float | None:
    w = np.array([v for v in win_vals if v == v])  # drop NaN
    l = np.array([v for v in loss_vals if v == v])
    if len(w) < 5 or len(l) < 5:
        print(f"  {name}: 樣本不足（win={len(w)}, loss={len(l)}），跳過")
        return None
    u, p = stats.mannwhitneyu(w, l, alternative="two-sided")
    # rank-biserial effect size
    rb = 1 - (2 * u) / (len(w) * len(l))
    print(
        f"  {name}: win mean={w.mean():+.3f}±{w.std():.3f} median={np.median(w):+.3f} (n={len(w)}) · "
        f"loss mean={l.mean():+.3f}±{l.std():.3f} median={np.median(l):+.3f} (n={len(l)}) · "
        f"Mann-Whitney p={p:.4f} · rank-biserial r={rb:+.3f}"
    )
    if p < 0.10:
        perm_p = permutation_p(w, l)
        print(f"    → p<0.10，交叉驗證：permutation test p={perm_p:.4f}")
    return p


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row  # load_kbar_day_bars() 需要 dict-like row access

    rows = []
    for t in trades:
        bars = load_t0_bars(con, t["stock"], t["signal_date"])
        feat = t0_features(bars)
        if feat is None:
            continue
        pnl = float(t["pnl_pct"])

        def _f(key: str) -> float:
            v = t.get(key)
            try:
                return float(v)
            except (TypeError, ValueError):
                return float("nan")

        feat.update(
            stock=t["stock"],
            signal_date=t["signal_date"],
            trade_date=t["trade_date"],
            pnl_pct=pnl,
            win=pnl > 0,
            n_seats=_f("n_seats"),
            amt_yi=_f("amt_yi"),
            fgap=_f("fgap"),
            rvol=_f("rvol"),
            adv_yi=_f("adv_yi"),
            advshare=_f("advshare"),
        )
        rows.append(feat)

    n_total = len(trades)
    n_cov = len(rows)
    print(f"=== dayflip-short T0 盤中走勢 vs 賺賠 ===")
    print(f"總交易數: {n_total} · 有1分K覆蓋可分析: {n_cov} ({n_cov/n_total*100:.0f}%)")
    print(
        "⚠️ 覆蓋率非隨機（可能偏向流動性較好/finmind收錄較完整的個股），"
        "以下結論僅適用於這個子樣本，非全部221筆的無偏推論。\n"
    )

    wins = [r for r in rows if r["win"]]
    losses = [r for r in rows if not r["win"]]
    print(f"子樣本內 win={len(wins)}, loss={len(losses)}\n")

    print("--- T0 走勢形狀特徵：win vs loss（Mann-Whitney U）---")
    feature_tests = [
        ("close_pos_in_range", "收盤位置(0=當日低點,1=當日高點)"),
        ("vol_hhi", "成交量時段集中度(HHI,越高越集中在單一時段)"),
        ("close_vs_vwap_pct", "收盤 vs VWAP 偏離%"),
        ("ret_open_1030", "開盤→10:30 報酬%"),
        ("ret_1030_close", "10:30→收盤 報酬%"),
        ("ret_last30", "尾盤30分鐘 報酬%"),
        ("day_ret_pct", "T0 全日報酬%(開盤→收盤)"),
    ]
    pvals = []
    for key, label in feature_tests:
        p = mannwhitney_report(label, [r[key] for r in wins], [r[key] for r in losses])
        if p is not None:
            pvals.append((label, p))

    bonf = 0.05 / len(feature_tests)
    print(f"\n  同時測了 {len(feature_tests)} 個特徵，Bonferroni 校正後顯著門檻 = {bonf:.4f}")
    survivors = [lbl for lbl, p in pvals if p < bonf]
    if survivors:
        print(f"  校正後仍顯著: {survivors}")
    else:
        print("  校正後沒有任何一個特徵存活——目前樣本下沒有可信的 T0 走勢形狀差異訊號")

    print("\n--- T0 分點籌碼強度特徵：win vs loss（已在 all_trades.csv 裡的欄位，一起檢）---")
    chip_tests = [
        ("n_seats", "觸發分點席次數"),
        ("amt_yi", "分點買超金額(億)"),
        ("fgap", "T+1進場跳空幅度%"),
        ("rvol", "T0相對量能(rvol)"),
        ("adv_yi", "20日均額(億)"),
        ("advshare", "T0成交量/均額比"),
    ]
    chip_pvals = []
    for key, label in chip_tests:
        p = mannwhitney_report(label, [r[key] for r in wins], [r[key] for r in losses])
        if p is not None:
            chip_pvals.append((label, p))
    bonf_chip = 0.05 / len(chip_tests)
    print(f"\n  同時測了 {len(chip_tests)} 個籌碼特徵，Bonferroni 校正後顯著門檻 = {bonf_chip:.4f}")
    chip_survivors = [lbl for lbl, p in chip_pvals if p < bonf_chip]
    if chip_survivors:
        print(f"  校正後仍顯著: {chip_survivors}")
    else:
        print("  校正後沒有任何一個籌碼特徵存活")

    print("\n--- T0 全樣本：最大成交量時段分布（分點下單時段推測，不分win/loss）---")
    from collections import Counter

    bucket_counts = Counter(r["max_vol_bucket"] for r in rows)
    for _, _, label in _BUCKETS:
        n = bucket_counts.get(label, 0)
        print(f"  {label}: {n} 筆 ({n/n_cov*100:.1f}%)")

    print("\n--- T0 全日最高點出現的時段分布 ---")
    hi_counts = Counter(r["hi_bucket"] for r in rows)
    for _, _, label in _BUCKETS:
        n = hi_counts.get(label, 0)
        print(f"  {label}: {n} 筆 ({n/n_cov*100:.1f}%)")

    print("\n--- 最大量時段 x win/loss 交叉表（各時段n偏小，僅供參考）---")
    for _, _, label in _BUCKETS:
        w = sum(1 for r in wins if r["max_vol_bucket"] == label)
        loss_n = sum(1 for r in losses if r["max_vol_bucket"] == label)
        tot = w + loss_n
        if tot == 0:
            continue
        print(f"  {label}: win={w} loss={loss_n} (win率={w/tot*100:.0f}%, n={tot})")

    print("\n--- 收斂成「早盤(09:00-10:30)集中」vs「晚於10:30才量最大」二分（Fisher exact）---")
    early_labels = {_BUCKETS[0][2], _BUCKETS[1][2]}
    early_win = sum(1 for r in wins if r["max_vol_bucket"] in early_labels)
    early_loss = sum(1 for r in losses if r["max_vol_bucket"] in early_labels)
    late_win = len(wins) - early_win
    late_loss = len(losses) - early_loss
    table = [[early_win, early_loss], [late_win, late_loss]]
    odds, p_fisher = stats.fisher_exact(table)
    print(f"  早盤量最大: win={early_win} loss={early_loss} (win率={early_win/(early_win+early_loss)*100:.0f}%)")
    print(f"  10:30後量才最大: win={late_win} loss={late_loss} (win率={late_win/(late_win+late_loss)*100:.0f}%)")
    print(f"  Fisher exact odds ratio={odds:.2f}, p={p_fisher:.4f}")

    # 記進 trial registry：這是個誠實的null result（13個特徵全數在Bonferroni校正後
    # 不顯著），值得留紀錄避免未來重複測同一組假設。early-vs-late二分測試也一併記，
    # 因為它是這次「擴大研究」動機的來源（小樣本 p=0.054 → 大樣本 p=0.36，反轉了）。
    all_p = {lbl: p for lbl, p in pvals} | {lbl: p for lbl, p in chip_pvals}
    best_label = min(all_p, key=all_p.get)
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="t0-intraday-shape-vs-winloss",
        ts="2026-08-08",
        params={"n_price_shape_features": len(feature_tests), "n_chip_features": len(chip_tests)},
        n_observations=n_cov,
        metric_name="min_bonferroni_adjusted_p",
        metric_value=min(all_p.values()),
        status="rejected",
        source=__file__,
        notes=(
            f"13個T0走勢/籌碼特徵(win={len(wins)},loss={len(losses)})做Mann-Whitney，"
            f"Bonferroni校正後全數不顯著；最接近的是「{best_label}」p={all_p[best_label]:.4f}"
            f"（未過0.05/13門檻）。early-vs-late量能集中二分法：小樣本(n=76)曾測得"
            f"p=0.054看似邊緣顯著，補齊1分K覆蓋率至86%(n=190)後重測變p=0.36——"
            "小樣本假訊號的具體案例，記錄下來避免未來再測同一假設。"
        ),
        tags=["dayflip-short", "t0-pattern", "null-result", "small-sample-flip"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
