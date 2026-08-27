#!/usr/bin/env python3
"""測試用真台指期夜盤動能（T0收盤到T0+1盤前）當『適應性跳空門檻』的依據，
而不是像前一輪那樣用0050當天開盤跳空(同期、非領先)當濾網.

背景：使用者問「夜盤大漲，明天大盤本來就會開高，個股開高也很正常...難道
放空的6%門檻不應該自適應校正嗎」。這是跟8/9那輪excess_gap濾網（用0050
『當天開盤跳空』、跟個股跳空同一時間點，嚴格說不算領先指標）不同、更好的
版本：夜盤在T0+1開盤『之前』就結束了，是真正因果正確、決策時已知的資訊。

方法：
  1) 對74筆短邊歷史交易，算T0夜盤動能 = (T0夜盤最後一根收盤 / T0日盤最後一根
     收盤 - 1)，這是T0+1開盤前就已知的『大盤隔夜移動』。
  2) IC(夜盤動能, 短邊pnl_pct) walk-forward——先看夜盤動能本身能不能預測
     短邊績效。
  3) 『自適應門檻』測試：excess_gap_v2 = 個股fgap - 夜盤動能，用一貫的
     walk-forward流程（訓練期選門檻、n>=30守門、測試期樣本外驗證）看用這個
     版本篩選/校正6%門檻，比原始fgap或前一輪用0050的版本表現如何。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_night_session_adaptive_gap.py
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import numpy as np
from scipy import stats as sstats

from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"
TX_BARS_DB = Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"
MIN_TRAIN_N = 30
EXCESS_GAP_THRESHOLD_CANDIDATES_PCT = (0.0, 1.0, 2.0, 3.0, 4.0, 5.0)


def load_short_trades() -> list[dict]:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def night_return_pct(con: sqlite3.Connection, t0: str) -> float | None:
    """T0夜盤動能：T0日盤最後一根收盤 → T0夜盤(15:00開始,跨夜到05:00)最後一根收盤."""
    day_close = con.execute(
        "SELECT c FROM bars WHERE source=? AND sess='day' AND day=? ORDER BY t DESC LIMIT 1",
        (TX_SOURCE, t0),
    ).fetchone()
    if not day_close:
        return None
    night_close = con.execute(
        "SELECT c FROM bars WHERE source=? AND sess='night' AND day=? ORDER BY t DESC LIMIT 1",
        (TX_SOURCE, t0),
    ).fetchone()
    if not night_close:
        return None
    dc, nc = float(day_close[0]), float(night_close[0])
    if dc <= 0:
        return None
    return (nc / dc - 1) * 100


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
    con = sqlite3.connect(f"file:{TX_BARS_DB}?mode=ro", uri=True)

    prepared = []
    for t in trades:
        nr = night_return_pct(con, t["trade_date"])
        if nr is None:
            continue
        prepared.append({
            "trade_date": t["trade_date"], "stock": t["stock"], "fgap": float(t["fgap"]),
            "night_return_pct": nr, "excess_gap_v2": float(t["fgap"]) - nr,
            "pnl_pct": float(t["pnl_pct"]),
        })

    print("=== 台指期夜盤動能（T0收盤→T0夜盤結束）vs 短邊歷史績效 ===")
    print(f"可分析: {len(prepared)}/{len(trades)}\n")

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])
    train = [p for p in prepared if p["trade_date"] in train_dates]
    test = [p for p in prepared if p["trade_date"] in test_dates]
    print(f"訓練期: {len(train)}筆（{len(train_dates)}天）· 測試期: {len(test)}筆（{len(test_dates)}天）\n")

    # --- 1) 夜盤動能本身能不能預測短邊績效 ---
    train_night = np.array([p["night_return_pct"] for p in train])
    train_pnl = np.array([p["pnl_pct"] for p in train])
    ic_train, p_train = sstats.spearmanr(train_night, train_pnl)
    test_night = np.array([p["night_return_pct"] for p in test])
    test_pnl = np.array([p["pnl_pct"] for p in test])
    ic_test, p_test = sstats.spearmanr(test_night, test_pnl)
    print("--- 1) 夜盤動能 vs 短邊pnl_pct（IC）---")
    print(f"訓練期: IC={ic_train:+.3f} (p={p_train:.3f}, n={len(train)})")
    print(f"測試期(樣本外): IC={ic_test:+.3f} (p={p_test:.3f}, n={len(test)})\n")

    # 日聚集
    by_date = {}
    for p in prepared:
        by_date.setdefault(p["trade_date"], []).append((p["night_return_pct"], p["pnl_pct"]))
    dl = [(np.mean([x[0] for x in v]), np.mean([x[1] for x in v])) for v in by_date.values()]
    dl_night = np.array([x[0] for x in dl])
    dl_pnl = np.array([x[1] for x in dl])
    ic_dl, p_dl = sstats.spearmanr(dl_night, dl_pnl)
    print(f"全樣本日聚集後: IC={ic_dl:+.3f} (p={p_dl:.3f}, n_days={len(dl)})\n")

    # --- 2) 自適應門檻：excess_gap_v2 = fgap - 夜盤動能 ---
    train_by_th = {}
    for th in EXCESS_GAP_THRESHOLD_CANDIDATES_PCT:
        subset = [p["pnl_pct"] for p in train if p["excess_gap_v2"] >= th]
        m = metrics(subset)
        train_by_th[th] = m
        print(f"[訓練期 excess_gap_v2>={th:.1f}%] n={m.get('n',0)} "
              f"勝率={m.get('win_rate',0)*100:.0f}% 平均pnl={m.get('mean_ret_pct',0):+.3f}% "
              f"sharpe_like={m.get('sharpe_like',float('nan')):.3f}")

    eligible = [th for th in EXCESS_GAP_THRESHOLD_CANDIDATES_PCT if train_by_th[th].get("n", 0) >= MIN_TRAIN_N]
    if not eligible:
        eligible = [0.0]
        print(f"\n⚠️ 沒有門檻通過n>={MIN_TRAIN_N}守門，退回門檻0%")
    best_th = max(eligible, key=lambda th: train_by_th[th].get("sharpe_like", -999) or -999)
    print(f"\n訓練期挑出：excess_gap_v2門檻 {best_th:.1f}%\n")

    baseline_test = [p["pnl_pct"] for p in test]
    filtered_test = [p["pnl_pct"] for p in test if p["excess_gap_v2"] >= best_th]
    m_base = metrics(baseline_test)
    m_filt = metrics(filtered_test)
    print("--- 2) 測試期樣本外：baseline vs excess_gap_v2篩選版 ---")
    print(f"[baseline] n={m_base.get('n',0)} 勝率={m_base.get('win_rate',0)*100:.0f}% "
          f"平均pnl={m_base.get('mean_ret_pct',0):+.3f}% sharpe_like={m_base.get('sharpe_like',float('nan')):.3f}")
    print(f"[excess_gap_v2>={best_th:.1f}%] n={m_filt.get('n',0)} 勝率={m_filt.get('win_rate',0)*100:.0f}% "
          f"平均pnl={m_filt.get('mean_ret_pct',0):+.3f}% sharpe_like={m_filt.get('sharpe_like',float('nan')):.3f}")

    print(
        "\n⚠️ 限制：\n"
        "  1) 夜盤動能用『T0日盤最後一根收盤→T0夜盤最後一根收盤』，這段涵蓋\n"
        "     整個夜盤(15:00~05:00)，比只看夜盤『開盤』更完整，但也代表這個\n"
        "     數字要等到05:00夜盤結束才完全確定——08:45決策時該用的是『當下\n"
        "     可得的夜盤走勢』，不是夜盤結束後才知道的最終值，這裡用最終值\n"
        "     是簡化，實際上線要注意這個時間點落差（不過通常05:00到08:45之間\n"
        "     夜盤已經走完，這個簡化影響應該不大，只是要誠實列出）。\n"
        "  2) 樣本數依然小(74筆切70/30)，統計把握度有限。\n"
        "  3) 這裡驗證的是『能不能篩選』，不是『怎麼設計動態門檻公式』——\n"
        "     使用者問的『自適應校正』更完整的版本應該是門檻本身隨夜盤動能\n"
        "     連續調整(例如門檻=6%+0.5×夜盤動能)，不是這裡測的『固定excess_gap\n"
        "     門檻篩選』，兩者概念相關但不完全一樣，這裡先驗證方向性。"
    )

    survives = (ic_test > 0.15 and p_test < 0.10) or (
        m_filt.get("mean_ret_pct", -999) > m_base.get("mean_ret_pct", -999) and m_filt.get("n", 0) >= 5
    )
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="short-night-session-adaptive-gap-threshold",
        ts="2026-08-10",
        params={
            "excess_gap_threshold_candidates_pct": list(EXCESS_GAP_THRESHOLD_CANDIDATES_PCT),
            "chosen_threshold_pct": best_th, "benchmark": "tx_1m_tick_built_582d_night_session",
        },
        n_observations=len(prepared),
        metric_name="ic_night_return_test_vs_short_pnl",
        metric_value=float(ic_test),
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"用真台指期夜盤動能(T0收盤→T0夜盤結束)測試短邊自適應跳空門檻的\n"
            f"想法。夜盤動能本身IC：訓練期{ic_train:+.3f}(p={p_train:.3f})、測試期"
            f"{ic_test:+.3f}(p={p_test:.3f})、日聚集{ic_dl:+.3f}(p={p_dl:.3f})。"
            f"excess_gap_v2篩選版(門檻{best_th:.1f}%)樣本外平均pnl"
            f"{m_filt.get('mean_ret_pct',0):+.3f}% vs baseline"
            f"{m_base.get('mean_ret_pct',0):+.3f}%（n={m_filt.get('n',0)}）。"
        ),
        tags=["dayflip-short", "night-session", "adaptive-threshold", "tx-real"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
