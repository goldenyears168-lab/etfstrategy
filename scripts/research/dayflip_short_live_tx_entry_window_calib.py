#!/usr/bin/env python3
"""測試『當下台指期』（進場當下08:45-09:05即時走勢）當校正訊號，不是夜盤結束
時的靜態數字——這是使用者最新的精確化要求：「要跟當下台指期校正才對吧只是
校正模型要調」，跟前一輪多agent測過的『夜盤動能』(T0日盤收盤→T0夜盤結束，
最晚05:00就固定了)是不同的東西：台指期日盤跟個股一樣08:45才開盤，
08:45-09:05這段短邊entry window本身有即時台指期資料，之前完全沒用過。

⚠️ 先講清楚今天的多重比較風險：前一輪5個agent總共掃了超過900組參數，
permutation test顯示這個樣本量(74筆)光靠亂數就有18~88%機率做出『看起來贏』
的假象——這裡用的是同一份74筆資料，所以這次的檢驗標準要更嚴，主要看
permutation test而不是單純train/test方向一致。

方法：
  1) TX即時進場窗訊號 = T0+1日盤08:45(開盤)到09:00(或09:05，短邊entry window
     結束)這段台指期報酬% ——這是短邊真正下單當下『看得到』的即時資訊，
     不是像夜盤動能那樣是幾小時前就已經定案的數字。
  2) IC(TX進場窗報酬, 短邊pnl_pct) walk-forward，並且直接做permutation test
     （這是這次最重要的把關，前一輪已經證明train/test方向一致本身不夠）。
  3) 如果IC本身有東西，才進一步測用這個訊號校正6%門檻的效果；如果IC本身
     就沒有，直接誠實回報，不做後面的門檻校正測試（避免做更多無意義的
     多重比較）。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_live_tx_entry_window_calib.py
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
ENTRY_WINDOW_END = "09:05"  # 跟dayflip_short_order.py的ENTRY_WINDOW_END一致
N_PERMUTATIONS = 3000


def load_short_trades() -> list[dict]:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def tx_entry_window_return_pct(con: sqlite3.Connection, t01: str) -> float | None:
    """T0+1當天台指期日盤08:45開盤 → entry window結束(09:05)這段報酬%——
    短邊真正送單決策當下已經即時可得的台指期走勢，不是隔夜就定案的舊數字。"""
    rows = con.execute(
        "SELECT t, o, c FROM bars WHERE source=? AND sess='day' AND day=? "
        "AND t>='08:45' AND t<=? ORDER BY t",
        (TX_SOURCE, t01, ENTRY_WINDOW_END),
    ).fetchall()
    if len(rows) < 5:
        return None
    open_px = float(rows[0][1])
    close_px = float(rows[-1][2])
    if open_px <= 0:
        return None
    return (close_px / open_px - 1) * 100


def permutation_test(x: np.ndarray, y: np.ndarray, n_perm: int = N_PERMUTATIONS) -> tuple[float, float]:
    """真實IC vs n_perm次隨機打亂x後的IC分布——回傳(真實IC, 雙尾p值)。"""
    real_ic, _ = sstats.spearmanr(x, y)
    rng = np.random.default_rng(20260810)
    perm_ics = []
    for _ in range(n_perm):
        shuffled = rng.permutation(x)
        ic, _ = sstats.spearmanr(shuffled, y)
        perm_ics.append(ic)
    perm_ics = np.array(perm_ics)
    p_value = float(np.mean(np.abs(perm_ics) >= abs(real_ic)))
    return float(real_ic), p_value


def main() -> None:
    trades = load_short_trades()
    con = sqlite3.connect(f"file:{TX_BARS_DB}?mode=ro", uri=True)

    prepared = []
    for t in trades:
        r = tx_entry_window_return_pct(con, t["trade_date"])
        if r is None:
            continue
        prepared.append({
            "trade_date": t["trade_date"], "stock": t["stock"], "fgap": float(t["fgap"]),
            "tx_entry_window_ret": r, "pnl_pct": float(t["pnl_pct"]),
        })

    print("=== 『當下台指期』（08:45-09:05進場窗即時走勢）vs 短邊歷史績效 ===")
    print(f"可分析: {len(prepared)}/{len(trades)}\n")

    x = np.array([p["tx_entry_window_ret"] for p in prepared])
    y = np.array([p["pnl_pct"] for p in prepared])

    print(f"台指期進場窗報酬分布: min={x.min():.3f}% p25={np.percentile(x,25):.3f}% "
          f"median={np.median(x):.3f}% p75={np.percentile(x,75):.3f}% max={x.max():.3f}%\n")

    print("--- 全樣本(74筆) IC + permutation test（這是最重要的把關）---")
    real_ic, p_perm = permutation_test(x, y)
    _, p_asymptotic = sstats.spearmanr(x, y)
    print(f"Spearman IC = {real_ic:+.3f}")
    print(f"漸進p值(scipy) = {p_asymptotic:.3f}")
    print(f"Permutation p值({N_PERMUTATIONS}次隨機打亂) = {p_perm:.3f}  ← 這次最重要看這個\n")

    # walk-forward也做，但只當輔助參考，不是主要判準
    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])
    train = [p for p in prepared if p["trade_date"] in train_dates]
    test = [p for p in prepared if p["trade_date"] in test_dates]
    train_x = np.array([p["tx_entry_window_ret"] for p in train])
    train_y = np.array([p["pnl_pct"] for p in train])
    test_x = np.array([p["tx_entry_window_ret"] for p in test])
    test_y = np.array([p["pnl_pct"] for p in test])
    ic_train, p_train = sstats.spearmanr(train_x, train_y)
    ic_test, p_test = sstats.spearmanr(test_x, test_y)
    print("--- 輔助參考：train/test切分(前一輪已證明這個單獨看不夠、只當參考)---")
    print(f"訓練期({len(train)}筆): IC={ic_train:+.3f} (p={p_train:.3f})")
    print(f"測試期({len(test)}筆): IC={ic_test:+.3f} (p={p_test:.3f})\n")

    if p_perm >= 0.10:
        print(
            "=== 結論：全樣本permutation test不顯著(p={:.3f}>=0.10) ===\n"
            "『當下台指期進場窗走勢』本身跟短邊績效沒有站得住腳的關係，不繼續\n"
            "往下測門檻校正——避免像前一輪一樣，在沒有真訊號的基礎上硬掃參數\n"
            "做出更多假陽性。".format(p_perm)
        )
        survives = False
    else:
        print(f"=== permutation test顯著(p={p_perm:.3f}<0.10)，值得進一步驗證 ===")
        survives = True

    print(
        "\n⚠️ 限制：\n"
        "  1) TX日盤08:45開盤是用tick重建的1分K第一根，可能跟真實逐筆成交\n"
        "     的『開盤瞬間』有些微落差，不是絕對精確。\n"
        "  2) permutation test雖然比單純train/test嚴謹，但74筆樣本量本身\n"
        "     還是小，就算這次通過p<0.10，也只代表『值得再驗證』，不是\n"
        "     『已經證實可以上線』——前一輪已經展示過這個資料集有多容易\n"
        "     製造出看起來像的假訊號。\n"
        "  3) 這裡只測了『TX進場窗即時走勢』這一個訊號，沒有測試把它跟\n"
        "     fgap結合成校正公式的效果（如果IC本身就不顯著，做這步是浪費\n"
        "     多重比較的『額度』，故意不做）。"
    )

    append_trial(
        "dayflip_short_gapup_short",
        topic_id="short-live-tx-entry-window-calibration",
        ts="2026-08-10",
        params={"entry_window_end": ENTRY_WINDOW_END, "n_permutations": N_PERMUTATIONS},
        n_observations=len(prepared),
        metric_name="permutation_p_value",
        metric_value=p_perm,
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"使用者精確化要求：測試『當下台指期』(08:45-09:05進場窗即時走勢，"
            f"不是前一輪已測過的夜盤動能靜態數字)當校正訊號。全樣本IC={real_ic:+.3f}、"
            f"permutation p值(3000次)={p_perm:.3f}——這次以permutation test為主要"
            f"判準(前一輪多agent已證明train/test切分本身在這個資料集上不夠嚴謹)。"
        ),
        tags=["dayflip-short", "tx-real", "live-calibration", "permutation-test"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
