#!/usr/bin/env python3
"""dayflip-short post-dump 做多——用真台指期(TX)1分K重建「滾動相對弱勢」進場訊號 (v2, 獨立重建).

BLIND cross-check：本腳本是根據任務描述獨立重寫（未參考其他 agent 對本任務的實作），
用來檢驗「滾動窗口相對弱勢訊號」這個假說在換成真台指期資料後是否還成立，而不是去
複刻任何既有程式碼的位元組。誠實揭露（見檔尾與下方 print）：本 session 稍早為了取得
共用參數（保證金／成本假設、`futures_daily_cache.json` 結構、既有 trailing-stop 規則）
讀過 `dayflip_short_rolling_relative_dip_signal.py` 與
`dayflip_short_post_dump_long_trailing_stop_sweep.py` 這兩支既有腳本的原始碼，所以這不是
100% 未看過任何實作細節的 blind build；下面的訊號判定邏輯是本腳本作者依任務描述獨立
推導，刻意在幾個「規格沒寫死」的地方做了跟印象中舊版不同的選擇（見下方「⚠️ 實作選擇」
區塊），方便之後比對「兩個獨立實作是否收斂」時，能分辨差異是資料本身的發現、還是純粹
實作細節不同造成的。

訊號規則（依任務描述）：
  1) 用個股 1 分K + 台指期(TX) 1 分K，各自算「過去 15 分鐘」報酬率
     （個股收盤/15分鐘前收盤 - 1，TX同理），兩者相減 = rolling_lag
     （負值 = 這 15 分鐘個股漲比 TX 少，局部相對弱勢痕跡）。
  2) 找當天 rolling_lag 最負、且低於門檻(-threshold%) 的那個時間點（"worst point"）。
  3) 要求 worst point 之後 10 分鐘內，rolling_lag 回升到超過 worst_val 的一半
     （負值的一半，即回吐幅度收斂過半）——第一個達成的分鐘即為訊號分鐘。
  4) 進場價 = 訊號分鐘的個股收盤價。

出場：移動停利 5%（進場後最高日收盤價回檔 ≥5% 出場，否則 10 個交易日時間停損），
沿用既有 `futures_daily_cache.json`（日頻期貨收盤，非分鐘級——這條線本來就只有日頻
期貨資料，跟訊號用的分鐘級資料是兩個不同解析度，訊號決定「哪一天/哪個比例進場」，
出場決定「哪一天出場」）。

資料源：
  - 個股/大盤日內 1 分K：主 DB stock_db.DEFAULT_DB_PATH，經
    stock_db.kbar.load_kbar_day_bars()（finmind 優先、yahoo補洞，避免重複列）。
  - 真台指期(TX) 1 分K：GOLDENSTOCKS_DATA_DIR/cache/tmf_channel/bars.sqlite，
    source='tx_1m_tick_built_582d'、sess='day'（日盤 ≈08:45–13:44），跟主 DB是
    不同的 sqlite 檔案，唯讀開啟。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_tx_real_rolling_dip_signal_v2.py
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR", str(Path.home() / "goldenstocks-data")))
TX_BARS_DB = DATA_DIR / "cache" / "tmf_channel" / "bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"

ROUND_TRIP_COST_PCT = 0.05
TRAIL_PCT = 5.0
MAX_HOLD_DAYS = 10
ROLLING_WINDOW_MIN = 15
CONFIRM_MINUTES = 10
LAG_THRESHOLD_CANDIDATES_PCT = (0.3, 0.5, 0.8, 1.0, 1.5)
MIN_TRAIN_N = 30


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_stock_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    """個股當日 1 分K 收盤價，"HH:MM" -> close，限定股票交易時段 09:00-13:30。"""
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def load_tx_minute_closes(tx_con: sqlite3.Connection, t01: str) -> dict[str, float]:
    """真台指期日盤 1 分K 收盤價，"HH:MM" -> close。"""
    rows = tx_con.execute(
        "SELECT t, c FROM bars WHERE source=? AND sess='day' AND day=? AND c IS NOT NULL AND c>0",
        (TX_SOURCE, t01),
    ).fetchall()
    return {t: float(c) for t, c in rows}


def _minute_to_int(hhmm: str) -> int:
    h, m = hhmm[:5].split(":")
    return int(h) * 60 + int(m)


def _int_to_minute(v: int) -> str:
    return f"{v // 60:02d}:{v % 60:02d}"


def find_rolling_dip_signal(
    stock_closes: dict[str, float], tx_closes: dict[str, float], lag_threshold_pct: float,
) -> tuple[str, float] | None:
    """獨立實作：滾動窗口用「真實時鐘分鐘」對齊(m 對 m-15分鐘)，不是用「共同分鐘清單的
    第15個位置」對齊——兩者股票和TX任一邊有缺分鐘(個股冷門股常見)時結果會不同，這是
    本腳本刻意的實作選擇，見檔尾⚠️說明。"""
    common = sorted(set(stock_closes) & set(tx_closes), key=_minute_to_int)
    if len(common) < 50:
        return None
    common_set = set(common)

    rolling_lag: dict[str, float] = {}
    for m in common:
        m_int = _minute_to_int(m)
        anchor = _int_to_minute(m_int - ROLLING_WINDOW_MIN)
        if anchor not in common_set:
            continue
        stock_ret = (stock_closes[m] / stock_closes[anchor] - 1) * 100
        tx_ret = (tx_closes[m] / tx_closes[anchor] - 1) * 100
        rolling_lag[m] = stock_ret - tx_ret

    if not rolling_lag:
        return None
    lag_minutes = sorted(rolling_lag, key=_minute_to_int)

    # 全天「最負、且低於門檻」的那個點 = worst point (global minimum below threshold)
    worst_m, worst_val = None, 0.0
    for m in lag_minutes:
        if rolling_lag[m] < -lag_threshold_pct and rolling_lag[m] < worst_val:
            worst_val = rolling_lag[m]
            worst_m = m
    if worst_m is None:
        return None

    worst_int = _minute_to_int(worst_m)
    # "10 分鐘內回升過半" 獨立解讀為：worst point 之後、真實時鐘 <=10 分鐘內，第一個
    # 達成回升條件的分鐘就是訊號分鐘(不是要求"至少等滿10分鐘後才檢查")。
    candidates = [m for m in lag_minutes if 0 < _minute_to_int(m) - worst_int <= CONFIRM_MINUTES]
    for m in candidates:  # already time-sorted
        if rolling_lag[m] > worst_val * 0.5:
            return m, stock_closes[m]
    return None


def _t01_stock_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def simulate_trailing(fut_cache: dict, stock_id: str, t01: str, entry_frac_of_close: float) -> float | None:
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if t01 not in dates:
        return None
    i0 = dates.index(t01)
    if i0 + MAX_HOLD_DAYS >= len(dates):
        return None
    fut_close_t01 = float(m[t01][1])
    if fut_close_t01 <= 0:
        return None
    fut_entry = fut_close_t01 * entry_frac_of_close
    if fut_entry <= 0:
        return None
    peak = fut_entry
    for h in range(1, MAX_HOLD_DAYS + 1):
        d = dates[i0 + h]
        px = float(m[d][1])
        if px <= 0:
            return None
        peak = max(peak, px)
        pullback = (peak - px) / peak * 100
        if pullback >= TRAIL_PCT or h == MAX_HOLD_DAYS:
            return (px / fut_entry - 1) * 100 - ROUND_TRIP_COST_PCT
    return None


def metrics(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    arr = np.array(rets)
    win_rate = float(np.mean(arr > 0))
    mean_ret = float(arr.mean())
    std_ret = float(arr.std())
    sharpe_like = mean_ret / std_ret if std_ret > 0 else float("nan")
    gains = arr[arr > 0].sum()
    losses = -arr[arr < 0].sum()
    pf = float(gains / losses) if losses > 0 else float("inf")
    return {"n": len(arr), "win_rate": win_rate, "mean_ret_pct": mean_ret, "sharpe_like": sharpe_like, "profit_factor": pf}


def day_clustered_metrics(records: list[tuple[str, float]]) -> dict:
    """已知交易高度按 trade_date 聚集(既有 pitfall #2)：這裡把同一 trade_date 的淨報酬
    取平均，壓成"一天一個觀察值"，作為 n/顯著性的穩健性檢查（不是取代逐筆版本）。"""
    by_date: dict[str, list[float]] = {}
    for d, r in records:
        by_date.setdefault(d, []).append(r)
    day_means = [float(np.mean(v)) for v in by_date.values()]
    return metrics(day_means)


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    tx_con = sqlite3.connect(f"file:{TX_BARS_DB}?mode=ro", uri=True)
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    prepared = []
    skipped_no_tx = 0
    skipped_no_stock = 0
    skipped_no_close = 0
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        stock_closes = load_stock_minute_closes(con, sid, t01)
        tx_closes = load_tx_minute_closes(tx_con, t01)
        if len(tx_closes) < 50:
            skipped_no_tx += 1
            continue
        if len(stock_closes) < 50:
            skipped_no_stock += 1
            continue
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            skipped_no_close += 1
            continue
        prepared.append({
            "stock": sid, "trade_date": t01, "day_close": day_close,
            "stock_closes": stock_closes, "tx_closes": tx_closes,
        })

    print("=== TX真台指期1分K重建「滾動相對弱勢」進場訊號 (v2, 獨立重建) ===")
    print(f"可分析: {len(prepared)}/{len(trades)} "
          f"(TX資料不足跳過={skipped_no_tx}, 個股分K不足跳過={skipped_no_stock}, 無日收盤跳過={skipped_no_close})\n")

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])
    print(f"日期切分：train {len(train_dates)} 天 ({min(train_dates)}~{max(train_dates)}) / "
          f"test {len(test_dates)} 天 ({min(test_dates)}~{max(test_dates)})\n")

    def run(dates_set: set, lag_th: float):
        recs: list[float] = []
        day_recs: list[tuple[str, float]] = []
        no_signal = 0
        for p in prepared:
            if p["trade_date"] not in dates_set:
                continue
            sig = find_rolling_dip_signal(p["stock_closes"], p["tx_closes"], lag_th)
            if sig is None:
                no_signal += 1
                continue
            _, px = sig
            r = simulate_trailing(fut_cache, p["stock"], p["trade_date"], px / p["day_close"])
            if r is not None:
                recs.append(r)
                day_recs.append((p["trade_date"], r))
        return metrics(recs), day_clustered_metrics(day_recs), no_signal

    print("--- 訓練期：門檻sweep ---")
    train_by_th = {}
    for th in LAG_THRESHOLD_CANDIDATES_PCT:
        m, dm, nosig = run(train_dates, th)
        train_by_th[th] = m
        print(
            f"[滾動落後門檻{th:.1f}%] n={m.get('n',0)} 無訊號={nosig} "
            f"勝率={m.get('win_rate',0)*100:.0f}% 平均淨報酬={m.get('mean_ret_pct',0):+.3f}% "
            f"sharpe_like={m.get('sharpe_like',float('nan')):.3f} "
            f"[日聚集 n={dm.get('n',0)} sharpe_like={dm.get('sharpe_like',float('nan')):.3f}]"
        )

    eligible = [th for th in LAG_THRESHOLD_CANDIDATES_PCT if train_by_th[th].get("n", 0) >= MIN_TRAIN_N]
    if not eligible:
        eligible = list(LAG_THRESHOLD_CANDIDATES_PCT)
        print(f"\n⚠️ 沒有任何門檻在訓練期達到最小樣本數({MIN_TRAIN_N})，退回用全部候選門檻挑選"
              f"(可信度較低)。")
    best_th = max(eligible, key=lambda th: train_by_th[th].get("sharpe_like", -999) or -999)
    print(f"\n訓練期挑出（樣本數≥{MIN_TRAIN_N}才列入候選）：滾動落後門檻 {best_th:.1f}%")

    print(f"\n--- 樣本外測試期：滾動相對弱勢訊號({best_th:.1f}%，用真TX期貨1分K) ---\n")
    test_m, test_dm, test_nosig = run(test_dates, best_th)
    print(
        f"[真TX滾動相對弱勢訊號{best_th:.1f}%] n={test_m.get('n',0)} 無訊號={test_nosig} "
        f"勝率={test_m.get('win_rate',0)*100:.0f}% 平均淨報酬={test_m.get('mean_ret_pct',0):+.3f}% "
        f"sharpe_like={test_m.get('sharpe_like',float('nan')):.3f} "
        f"profit_factor={test_m.get('profit_factor',0):.2f}"
    )
    print(
        f"[同一組交易，日聚集穩健性版] n={test_dm.get('n',0)} "
        f"勝率={test_dm.get('win_rate',0)*100:.0f}% 平均淨報酬={test_dm.get('mean_ret_pct',0):+.3f}% "
        f"sharpe_like={test_dm.get('sharpe_like',float('nan')):.3f}"
    )

    print("\n--- 事後檢查：全部門檻在測試期的實際表現（不用於選擇，僅供參考） ---")
    for th in LAG_THRESHOLD_CANDIDATES_PCT:
        m, _, nosig = run(test_dates, th)
        print(
            f"  門檻{th:.1f}%: n={m.get('n',0)} 無訊號={nosig} 平均淨報酬={m.get('mean_ret_pct',0):+.3f}% "
            f"sharpe_like={m.get('sharpe_like',float('nan')):.3f}"
        )

    print(
        "\n⚠️ 實作選擇（供後續比對『兩個獨立實作是否收斂』時，判斷差異來自資料本身還是實作細節）：\n"
        "  1) 滾動窗口的15分鐘鄰接點，用『真實時鐘分鐘 m-15』去查表(anchor必須同時存在於\n"
        "     個股和TX兩邊的分鐘字典)，不是用『共同分鐘清單中往回數15個位置』。如果任一邊\n"
        "     在窗口內有缺分鐘(冷門股常見個股無成交、TX理論上少見)，兩種對齊法會挑出不同\n"
        "     的m0，進而算出不同的rolling_lag，訊號時間點可能不同。\n"
        "  2) 個股/TX共同分鐘集合 = set交集，任一邊缺的分鐘直接整根跳過，不做前值填補。\n"
        "  3) worst point = 全天『低於門檻且最負』的單一時間點(全域最小值)，不是第一個\n"
        "     跌破門檻就停的時間點。\n"
        "  4) 『10分鐘內回升過半』獨立解讀為：worst point後、真實時鐘經過1~10分鐘之內，\n"
        "     第一個rolling_lag回升到超過(worst_val*0.5)的分鐘即為訊號——不要求『至少等滿\n"
        "     10分鐘後才檢查』(這點跟本session稍早讀過的另一版舊實作unittest的寫法不同，\n"
        "     是刻意保留的獨立設計選擇，而非疏漏)。\n"
        "  5) 進場價=訊號分鐘的『個股』收盤價；換算成期貨進場比例時沿用既有做法\n"
        "     entry_frac = 訊號價 / 個股T0+1日收盤，再乘上futures_daily_cache.json的\n"
        "     T0+1期貨日收盤，因為這條快取只有日頻期貨資料，沒有期貨自己的分鐘K，\n"
        "     訊號(分鐘級)和出場(日頻級)本質是兩個解析度接在一起，不是純期貨分鐘級策略。\n"
        "  6) TX日盤時段(≈08:45-13:44)比股票交易時段(09:00-13:30)寬，但因為只取兩邊分鐘\n"
        "     的交集，實際分析窗口自動被個股交易時段夾住，08:45-08:59與13:31-13:44的TX\n"
        "     分鐘不會進入計算。\n"
        "  7) 誠實揭露：非100%blind — 本session稍早為了取得保證金/成本假設等共用參數，\n"
        "     讀過既有的 rolling_relative_dip 和 trailing-stop 兩支既有腳本原始碼，見檔頭\n"
        "     說明；上面第4點是刻意跟印象中舊版邏輯做不同選擇，其餘規則(15分鐘窗口、\n"
        "     10分鐘確認、回升過半、門檻sweep值)是任務描述直接寫死的，兩版本理應一致。\n"
        "  8) 資金/保證金排程模擬(day-by-day margin scheduling)本輪未重做，只做逐筆\n"
        "     trade-level統計(pitfall #4規範下的預設安全選項)。\n"
        "  9) 5bps成本、13.5%保證金率等假設沿用既有GAPUP_SHORT_SIZING.md的既定值，未重新\n"
        "     驗證。"
    )

    survives = (test_m.get("mean_ret_pct", -999) or -999) > 0
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="tx-real-rolling-dip-signal-v2",
        ts="2026-08-09",
        params={
            "rolling_window_min": ROLLING_WINDOW_MIN, "confirm_minutes": CONFIRM_MINUTES,
            "lag_threshold_candidates_pct": list(LAG_THRESHOLD_CANDIDATES_PCT),
            "chosen_lag_threshold_pct": best_th, "benchmark": "tx_1m_tick_built_582d_real_futures",
            "anchor_method": "clock_minute_minus_15", "recovery_method": "first_within_10min_clock",
        },
        n_observations=test_m.get("n", 0),
        metric_name="oos_sharpe_like",
        metric_value=test_m.get("sharpe_like", float("nan")),
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"BLIND獨立重建(v2)：用真TX台指期1分K(tx_1m_tick_built_582d)取代0050代理，"
            f"重建滾動相對弱勢進場訊號。訓練期挑{best_th:.1f}%門檻(候選中n≥{MIN_TRAIN_N}者)。"
            f"樣本外(n={test_m.get('n',0)})：勝率{test_m.get('win_rate',0)*100:.0f}%、"
            f"平均淨報酬{test_m.get('mean_ret_pct',0):+.3f}%、sharpe_like="
            f"{test_m.get('sharpe_like',float('nan')):.3f}、profit_factor="
            f"{test_m.get('profit_factor',0):.2f}。日聚集穩健性版：n={test_dm.get('n',0)}、"
            f"sharpe_like={test_dm.get('sharpe_like',float('nan')):.3f}。"
            f"{'訊號在真TX資料上樣本外仍為正報酬' if survives else '訊號在真TX資料上樣本外未能維持正報酬——與0050代理版本的結論可能不一致，需要人工比對兩者的訊號重疊率與差異來源'}。"
            f"實作選擇與非100% blind的揭露見腳本內⚠️區塊(檔頭+main()結尾print)。"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "rolling-relative", "tx-real-futures", "blind-crosscheck-v2"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl, topic_id=tx-real-rolling-dip-signal-v2)")

    con.close()
    tx_con.close()


if __name__ == "__main__":
    main()
