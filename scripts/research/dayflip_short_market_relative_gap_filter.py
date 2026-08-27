#!/usr/bin/env python3
"""短邊fgap門檻完全不看大盤——測試『扣掉大盤當天漲幅後的超額跳空』能不能篩掉
真正是beta造成、不是隔日沖客追價造成的假訊號.

背景：使用者問「已知週五大漲、週一也幾乎會漲，我們策略有考慮到嗎」——查證後
確認：dayflip-futures-short的build_candidates()/pick_signal()完全沒有任何
大盤相對強弱的濾網，fgap只跟股票自己前一天收盤比。普漲日更多股票會『順便』
跳空超過6%門檻，其中一部分可能只是beta，不是真正的隔日沖客情緒性追價——
這裡測試：excess_gap = 個股fgap - 大盤(0050)當天開盤跳空，能不能比原始fgap
更準確地篩出『真的是隔日沖客追價、後續會倒貨』的候選。

方法：對74筆短邊歷史交易，逐筆算當天0050的開盤跳空(用0050自己的open/前一日
close)，excess_gap=fgap-market_gap。用一貫的walk-forward流程：
  1) 訓練期(70%)/測試期(30%)照trade_date排序切分
  2) 訓練期算IC(excess_gap, pnl_pct)確認方向、掃excess_gap門檻(用MIN_TRAIN_N
     樣本數守門避免n<30小樣本overfitting，這條研究線自己的教訓)
  3) 測試期樣本外驗證：excess_gap篩選版 vs 原始fgap(smallest_qualifying_gap
     pick rule) baseline，比較勝率/平均報酬/sharpe_like
  4) 日聚集穩健性檢查

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_market_relative_gap_filter.py
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import numpy as np
from scipy import stats as sstats

import stock_db
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
MIN_TRAIN_N = 30
EXCESS_GAP_THRESHOLD_CANDIDATES_PCT = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0)


def load_short_trades() -> list[dict]:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def market_gap_pct(con: sqlite3.Connection, trade_date: str) -> float | None:
    """0050當天(T0+1)開盤 vs 前一交易日收盤的跳空% ——大盤代理."""
    row = con.execute(
        "SELECT trade_date, open FROM stock_daily_bars "
        "WHERE stock_id='0050' AND source='finmind' AND trade_date=? AND open>0",
        (trade_date,),
    ).fetchone()
    if not row:
        return None
    prior = con.execute(
        "SELECT MAX(trade_date) FROM stock_daily_bars "
        "WHERE stock_id='0050' AND source='finmind' AND trade_date<? AND close>0",
        (trade_date,),
    ).fetchone()
    if not prior or not prior[0]:
        return None
    prior_close_row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id='0050' AND source='finmind' AND trade_date=? AND close>0",
        (prior[0],),
    ).fetchone()
    if not prior_close_row:
        return None
    open_px, prior_close = float(row[1]), float(prior_close_row[0])
    if prior_close <= 0:
        return None
    return (open_px / prior_close - 1) * 100


def metrics(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    arr = np.array(rets)
    win_rate = float(np.mean(arr > 0))
    mean_ret = float(arr.mean())
    std_ret = float(arr.std())
    sharpe_like = mean_ret / std_ret if std_ret > 0 else float("nan")
    return {"n": len(arr), "win_rate": win_rate, "mean_ret_pct": mean_ret, "sharpe_like": sharpe_like}


def main() -> None:
    trades = load_short_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)

    prepared = []
    for t in trades:
        mg = market_gap_pct(con, t["trade_date"])
        if mg is None:
            continue
        fgap = float(t["fgap"])
        prepared.append({
            "trade_date": t["trade_date"], "stock": t["stock"], "fgap": fgap,
            "market_gap": mg, "excess_gap": fgap - mg, "pnl_pct": float(t["pnl_pct"]),
        })

    print("=== 短邊：excess_gap(超額跳空) vs 原始fgap 篩選效果 ===")
    print(f"可分析: {len(prepared)}/{len(trades)}\n")

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])

    train = [p for p in prepared if p["trade_date"] in train_dates]
    test = [p for p in prepared if p["trade_date"] in test_dates]

    print(f"訓練期: {len(train)}筆（{len(train_dates)}天）· 測試期: {len(test)}筆（{len(test_dates)}天）\n")

    # --- 訓練期：IC(excess_gap, pnl_pct) ---
    train_excess = np.array([p["excess_gap"] for p in train])
    train_pnl = np.array([p["pnl_pct"] for p in train])
    ic, ic_p = sstats.spearmanr(train_excess, train_pnl)
    print(f"訓練期 IC(excess_gap, pnl_pct) = {ic:+.3f} (p={ic_p:.3f})")
    print("（做空策略：excess_gap越大代表越可能是真的隔日沖客追價，理論上應該"
          "越可能後續倒貨、pnl_pct應該越正——IC應該要是正的才符合假說）\n")

    # --- 訓練期：掃excess_gap門檻，用MIN_TRAIN_N守門 ---
    train_by_th = {}
    for th in EXCESS_GAP_THRESHOLD_CANDIDATES_PCT:
        subset = [p["pnl_pct"] for p in train if p["excess_gap"] >= th]
        m = metrics(subset)
        train_by_th[th] = m
        print(f"[訓練期 excess_gap>={th:.1f}%] n={m.get('n',0)} "
              f"勝率={m.get('win_rate',0)*100:.0f}% 平均pnl={m.get('mean_ret_pct',0):+.3f}% "
              f"sharpe_like={m.get('sharpe_like',float('nan')):.3f}")

    eligible = [th for th in EXCESS_GAP_THRESHOLD_CANDIDATES_PCT if train_by_th[th].get("n", 0) >= MIN_TRAIN_N]
    if not eligible:
        eligible = [0.0]
        print(f"\n⚠️ 沒有門檻通過n>={MIN_TRAIN_N}守門，退回門檻0%（等同baseline）")
    best_th = max(eligible, key=lambda th: train_by_th[th].get("sharpe_like", -999) or -999)
    print(f"\n訓練期挑出（樣本數>={MIN_TRAIN_N}才列入候選）：excess_gap門檻 {best_th:.1f}%\n")

    # --- 測試期：樣本外驗證 ---
    baseline_test = [p["pnl_pct"] for p in test]
    filtered_test = [p["pnl_pct"] for p in test if p["excess_gap"] >= best_th]
    m_base = metrics(baseline_test)
    m_filt = metrics(filtered_test)
    print("--- 測試期樣本外比較 ---")
    print(f"[原始baseline，無excess_gap篩選] n={m_base.get('n',0)} "
          f"勝率={m_base.get('win_rate',0)*100:.0f}% 平均pnl={m_base.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={m_base.get('sharpe_like',float('nan')):.3f}")
    print(f"[excess_gap>={best_th:.1f}%篩選版] n={m_filt.get('n',0)} "
          f"勝率={m_filt.get('win_rate',0)*100:.0f}% 平均pnl={m_filt.get('mean_ret_pct',0):+.3f}% "
          f"sharpe_like={m_filt.get('sharpe_like',float('nan')):.3f}")

    excluded = [p for p in test if p["excess_gap"] < best_th]
    if excluded:
        excl_pnl = [p["pnl_pct"] for p in excluded]
        print(f"\n被篩掉的{len(excluded)}筆（excess_gap<{best_th:.1f}%）自己的平均pnl="
              f"{np.mean(excl_pnl):+.3f}%（如果篩選有效，這批應該比篩選版基準差）")

    # 日聚集穩健性
    by_date_base = {}
    by_date_filt = {}
    for p in test:
        by_date_base.setdefault(p["trade_date"], []).append(p["pnl_pct"])
    for p in test:
        if p["excess_gap"] >= best_th:
            by_date_filt.setdefault(p["trade_date"], []).append(p["pnl_pct"])
    date_base = np.array([np.mean(v) for v in by_date_base.values()])
    date_filt = np.array([np.mean(v) for v in by_date_filt.values()]) if by_date_filt else np.array([])
    print(f"\n日聚集後：baseline平均={date_base.mean():+.3f}%（{len(date_base)}天）"
          + (f" · 篩選版平均={date_filt.mean():+.3f}%（{len(date_filt)}天）" if len(date_filt) else " · 篩選版無資料"))

    print(
        "\n⚠️ 限制：\n"
        "  1) 大盤代理用0050自己的開盤跳空(vs前一日收盤)，不是台指期即時跳空，\n"
        "     跟short邊fgap用的『個股期貨開盤vs前一日收盤』基準不完全對齊\n"
        "     （一個是現貨ETF、一個是個股期貨），方向性參考、非精確對照。\n"
        "  2) 樣本數很小(74筆短邊交易切70/30後測試期可能n<20)，任何『有效/\n"
        "     無效』的結論統計把握度都低，這是探索性檢查不是定案研究。\n"
        "  3) 沒有測試『改變pick_rule用excess_gap取代fgap排序』這個更大的\n"
        "     改動，只測『在現有smallest_qualifying_gap基礎上加一層excess_gap\n"
        "     篩選』會不會篩掉明顯更差的那批。"
    )

    survives = (m_filt.get("mean_ret_pct", -999) > m_base.get("mean_ret_pct", -999)) and m_filt.get("n", 0) >= 5
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="short-market-relative-excess-gap-filter",
        ts="2026-08-10",
        params={"excess_gap_threshold_candidates_pct": list(EXCESS_GAP_THRESHOLD_CANDIDATES_PCT),
                "chosen_threshold_pct": best_th, "benchmark": "0050"},
        n_observations=m_filt.get("n", 0),
        metric_name="oos_mean_pnl_pct_filtered_vs_baseline",
        metric_value=m_filt.get("mean_ret_pct", float("nan")) - m_base.get("mean_ret_pct", float("nan")),
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"測試扣除大盤(0050)開盤跳空後的excess_gap能否比原始fgap更準確篩選\n"
            f"短邊候選。訓練期IC={ic:+.3f}(p={ic_p:.3f})，挑出門檻{best_th:.1f}%。"
            f"樣本外：baseline{m_base.get('mean_ret_pct',0):+.3f}% vs 篩選版"
            f"{m_filt.get('mean_ret_pct',0):+.3f}%（n={m_filt.get('n',0)}）——"
            f"{'篩選有改善' if survives else '沒有改善或樣本太小'}。"
        ),
        tags=["dayflip-short", "market-relative-gap", "regime-filter", "excess-gap"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
