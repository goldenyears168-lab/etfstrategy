#!/usr/bin/env python3
"""做多/做空歷史損益 跟 美國VIX 的相關性（IC test，非單純散點目視）.

DB裡的market_vix_daily有兩個symbol：'VIX'(美股CBOE，2003~2026-07-31，yahoo源)
跟'VIXTWN'(台指選擇權推算，mini自算，2003~2026-08-07)——這裡用使用者問的
『美國VIX』，也就是symbol='VIX'那個。

因果性：訊號日T0收盤後才知道分點買超事件，交易在T0+1早盤執行——用T0（決策前
最後可得）的VIX收盤當作『當時的regime』，不是用T0+1（事後才知道）的VIX，
避免用未來資訊。

短邊：single_pick_tradelog.csv 74筆真實交易，pnl_pct已經算好。
長邊：重新跑一次已驗證的find_entry_price+多日5%移動停利（跟前面好幾輪同一套
邏輯），算net_ret_pct配對VIX。

方法比照前一輪excess_gap篩選：訓練/測試70/30切分、IC先看方向、
day-clustered穩健性檢查，不用小樣本武斷下結論。

PYTHONPATH=src:scripts/research .venv/bin/python scripts/research/dayflip_short_long_vix_correlation.py
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import numpy as np
from scipy import stats as sstats

import stock_db
from trial_registry import append_trial

from dayflip_short_post_dump_long_capital_simulation import (
    FUT_CACHE_PATH,
    MAX_HOLD_DAYS,
    ROUND_TRIP_COST_PCT,
    TRAIL_PCT,
    _t01_stock_close,
    find_entry_price,
    load_trades,
)

ROOT = Path(__file__).resolve().parents[2]
SHORT_TRADELOG_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/single_pick_tradelog.csv"


def load_short_trades() -> list[dict]:
    with SHORT_TRADELOG_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def vix_on_or_before(con: sqlite3.Connection, symbol: str, as_of: str) -> float | None:
    """T0當天若有VIX收盤就用當天；沒有(美股VIX跟台股交易日曆有時差)就往前找最近一筆。"""
    row = con.execute(
        "SELECT date, close FROM market_vix_daily WHERE symbol=? AND date<=? AND close IS NOT NULL "
        "ORDER BY date DESC LIMIT 1",
        (symbol, as_of),
    ).fetchone()
    return float(row[1]) if row else None


def long_side_net_ret(con: sqlite3.Connection, fut_cache: dict, stock_id: str, t01: str) -> float | None:
    entry = find_entry_price(con, stock_id, t01)
    if entry is None:
        return None
    entry_px, _kind = entry
    day_close = _t01_stock_close(con, stock_id, t01)
    if day_close is None or day_close <= 0:
        return None
    entry_frac = entry_px / day_close
    m = fut_cache.get(stock_id) or {}
    dates = sorted(m)
    if t01 not in dates:
        return None
    i0 = dates.index(t01)
    fut_close_t01 = float(m[t01][1])
    if fut_close_t01 <= 0:
        return None
    fut_entry = fut_close_t01 * entry_frac
    peak = fut_entry
    for h in range(1, MAX_HOLD_DAYS + 1):
        if i0 + h >= len(dates):
            return None
        d = dates[i0 + h]
        px = float(m[d][1])
        if px <= 0:
            return None
        peak = max(peak, px)
        pullback = (peak - px) / peak * 100
        if pullback >= TRAIL_PCT or h == MAX_HOLD_DAYS:
            return (px / fut_entry - 1) * 100 - ROUND_TRIP_COST_PCT
    return None


def walk_forward_ic(pairs: list[tuple[str, float, float]], label: str) -> None:
    """pairs: (trade_date, vix, ret_pct) 排序後切70/30，訓練期看IC方向，
    day-clustered版本再驗證一次（同一天多筆訊號時，避免高估樣本數）。"""
    if len(pairs) < 20:
        print(f"[{label}] 樣本數{len(pairs)}太小，跳過walk-forward，只報整體IC供參考")
        vix_arr = np.array([p[1] for p in pairs])
        ret_arr = np.array([p[2] for p in pairs])
        ic, p = sstats.spearmanr(vix_arr, ret_arr)
        print(f"  整體IC(VIX, ret) = {ic:+.3f} (p={p:.3f}, n={len(pairs)})")
        return

    dates_sorted = sorted({p[0] for p in pairs})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])

    train = [p for p in pairs if p[0] in train_dates]
    test = [p for p in pairs if p[0] in test_dates]

    train_vix = np.array([p[1] for p in train])
    train_ret = np.array([p[2] for p in train])
    ic_train, p_train = sstats.spearmanr(train_vix, train_ret)
    print(f"[{label}] 訓練期 IC(VIX水位, ret) = {ic_train:+.3f} (p={p_train:.3f}, n={len(train)})")

    test_vix = np.array([p[1] for p in test])
    test_ret = np.array([p[2] for p in test])
    ic_test, p_test = sstats.spearmanr(test_vix, test_ret)
    print(f"[{label}] 測試期(樣本外) IC(VIX水位, ret) = {ic_test:+.3f} (p={p_test:.3f}, n={len(test)})")

    # 日聚集：同一天多筆訊號時，先在該天內取均值，避免高估獨立樣本數
    by_date: dict[str, list[tuple[float, float]]] = {}
    for d, v, r in pairs:
        by_date.setdefault(d, []).append((v, r))
    date_level = [(np.mean([x[0] for x in v]), np.mean([x[1] for x in v])) for v in by_date.values()]
    dl_vix = np.array([x[0] for x in date_level])
    dl_ret = np.array([x[1] for x in date_level])
    ic_dl, p_dl = sstats.spearmanr(dl_vix, dl_ret)
    print(f"[{label}] 日聚集後 IC(VIX水位, ret) = {ic_dl:+.3f} (p={p_dl:.3f}, n_days={len(date_level)})")

    # 高VIX vs 低VIX 兩組比較（用整體中位數切）
    median_vix = float(np.median([p[1] for p in pairs]))
    high = [p[2] for p in pairs if p[1] >= median_vix]
    low = [p[2] for p in pairs if p[1] < median_vix]
    print(f"[{label}] VIX>=中位數({median_vix:.1f}) n={len(high)} 平均ret={np.mean(high):+.3f}% "
          f"vs VIX<中位數 n={len(low)} 平均ret={np.mean(low):+.3f}%")
    if len(high) >= 5 and len(low) >= 5:
        t, p_t = sstats.ttest_ind(high, low, equal_var=False)
        print(f"  兩組t-test: t={t:+.2f} p={p_t:.3f}")


def main() -> None:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = __import__("json").loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    print("=== 短邊/長邊歷史損益 vs 美國VIX 相關性 ===\n")

    # --- 短邊 ---
    short_trades = load_short_trades()
    short_pairs = []
    for t in short_trades:
        vix = vix_on_or_before(con, "VIX", t["trade_date"])
        if vix is None:
            continue
        short_pairs.append((t["trade_date"], vix, float(t["pnl_pct"])))
    print(f"短邊可配對: {len(short_pairs)}/{len(short_trades)}")
    walk_forward_ic(short_pairs, "短邊(dayflip-futures-short)")

    print()

    # --- 長邊 ---
    long_trades = load_trades()
    long_pairs = []
    for t in long_trades:
        sid, t01 = t["stock"], t["trade_date"]
        ret = long_side_net_ret(con, fut_cache, sid, t01)
        if ret is None:
            continue
        vix = vix_on_or_before(con, "VIX", t01)
        if vix is None:
            continue
        long_pairs.append((t01, vix, ret))
    print(f"長邊可配對: {len(long_pairs)}/{len(long_trades)}")
    walk_forward_ic(long_pairs, "長邊(dayflip-post-dump-long)")

    print(
        "\n⚠️ 限制：\n"
        "  1) VIX用T0（訊號日，決策前最後可得資料）收盤，不是T0+1（交易執行日）\n"
        "     ——避免用交易當下才知道的VIX當『預測』因子，是因果正確的設計，\n"
        "     但也代表這個相關性測的是『訊號觸發前一晚的VIX regime』，不是\n"
        "     『交易當下的VIX』。\n"
        "  2) 短邊樣本(74筆)天生偏小，測試期切完可能n<25，統計把握度低。\n"
        "  3) 這是探索性相關性分析，不是要直接拿VIX當交易濾網的定案研究——\n"
        "     即使發現顯著相關，套用前還需要跟前一輪excess_gap濾網一樣的\n"
        "     walk-forward門檻選擇+樣本外驗證，這裡只回答『有沒有相關』。"
    )

    survives_short = len(short_pairs) >= 20
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="short-long-vix-correlation-check",
        ts="2026-08-10",
        params={"vix_symbol": "VIX", "vix_timing": "T0_close"},
        n_observations=len(short_pairs) + len(long_pairs),
        metric_name="short_n_pairs",
        metric_value=len(short_pairs),
        status="kept" if survives_short else "rejected",
        source=__file__,
        notes=(
            f"美國VIX(symbol='VIX')跟短邊({len(short_pairs)}筆)/長邊({len(long_pairs)}筆)"
            f"歷史損益的IC相關性探索性分析，詳細數字見腳本輸出（train/test/"
            f"day-clustered三組IC + 中位數高低VIX分組t-test）。"
        ),
        tags=["dayflip-short", "post-dump", "vix-correlation", "regime"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
