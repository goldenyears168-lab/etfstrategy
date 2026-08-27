#!/usr/bin/env python3
"""dayflip-short post-dump 做多——成本敏感度壓力測試（round-trip cost sweep）.

背景：目前為止的 post-dump-long 研究全程假設個股期貨 round-trip cost = 5bps
（沿用 GAPUP_SHORT_SIZING.md §五-C「成本：個股期貨 5bps」的口徑），但這個數字
從未經過實測滑價驗證。同一份 sizing 文件裡另外有兩個更保守的參照點：
  1) §一：現股當沖版本用 cost=30bps 當標準假設，並做過 30/50/80bps 敏感度
     （日均 +1.64% → +1.44% → +1.14%，80bps 仍正——但那是「現股」不是「個股期貨」）。
  2) §五-D：實測「個股期貨 vs 現股」基差後，按當日期貨成交量分層的
     `期貨−現貨` 中位差距從流動性最好一層 −0.015%（≈1.5bps）一路惡化到
     流動性最差一層 −2.819%（≈281.9bps）——這是基差／執行落差、不是嚴格定義
     的滑價，但同樣顯示「5bps 是流動性最佳情境」。
  3) `config/order.yaml`（dayflip-short 上線 notes）明講：「已知問題：真實滑價
     未實測（估計13~76bps區間）」——這是本次壓力測試要覆蓋的目標區間，
     取自這一支姊妹（做空）策略、同一種工具（個股期貨）的 prod notes，
     而非 GAPUP_SHORT_SIZING.md 本文（該文件本文用的是 5bps 固定假設 + 現股
     30/50/80bps 敏感度，並未針對個股期貨本身做滑價 sweep——本腳本補這一塊）。

方法：直接沿用兩支已驗證腳本的邏輯，不重新挑參數——
  - 進場訊號＝`dayflip_short_rolling_relative_dip_signal.py` 的
    rolling_relative_dip（滾動15分鐘窗口相對大盤(0050)弱勢段 + 10分鐘收斂確認），
    訓練期用 sharpe_like 挑落後門檻（要求候選 n≥30，跟原腳本一致），固定用
    5bps 成本口徑挑（沿用原腳本行為——成本不是這一步在測的東西）。
  - 出場＝`dayflip_short_post_dump_long_trailing_stop_sweep.py` 驗證過、勝出的
    移動停利 5%（peak-since-entry回檔5%出場，否則10個交易日時間停損兜底）。
  - 兩者都在訓練期（70%，依日期切分）挑好、鎖住，唯一在這裡變動的變數＝
    round-trip cost，只在樣本外測試期（30%）評估。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_cost_sensitivity_stress.py
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars
from trial_registry import append_trial

ROOT = Path(__file__).resolve().parents[2]
TRADES_CSV = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/all_trades.csv"
FUT_CACHE_PATH = ROOT / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"

TRAIL_PCT = 5.0  # 已驗證勝出的移動停利門檻（trailing_stop_sweep.py）
MAX_HOLD_DAYS = 10
BENCH = "0050"
ROLLING_WINDOW_MIN = 15
LAG_THRESHOLD_CANDIDATES_PCT = (0.3, 0.5, 0.8, 1.0, 1.5)
CONFIRM_MINUTES = 10
MIN_TRAIN_N = 30
ENTRY_SELECTION_COST_PCT = 0.05  # 挑進場門檻這一步固定用5bps，不受本次cost sweep影響

# 本次真正在測的變數：round-trip cost，單位 bps。5bps=先前假設；
# 13/76bps=config/order.yaml「已知問題」估計區間的上下界；
# 10/20/30/50/100bps=補齊區間內外的其他常見錨點（30bps=現股口徑、
# 100bps=超出已知估計區間的極端情境，看邊界外還剩多少緩衝）。
COST_SWEEP_BPS = (5, 10, 20, 30, 50, 76, 100)


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_minute_closes(con: sqlite3.Connection, stock_id: str, t01: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, t01)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def find_rolling_dip_signal(
    stock_closes: dict[str, float], bench_closes: dict[str, float], lag_threshold_pct: float,
) -> tuple[str, float] | None:
    minutes = sorted(set(stock_closes) & set(bench_closes))
    if len(minutes) < 50:
        return None

    rolling_lag = {}
    for i, m in enumerate(minutes):
        if i < ROLLING_WINDOW_MIN:
            continue
        m0 = minutes[i - ROLLING_WINDOW_MIN]
        stock_ret = (stock_closes[m] / stock_closes[m0] - 1) * 100
        bench_ret = (bench_closes[m] / bench_closes[m0] - 1) * 100
        rolling_lag[m] = stock_ret - bench_ret

    lag_minutes = sorted(rolling_lag)
    if not lag_minutes:
        return None

    worst_idx = None
    worst_val = 0.0
    for i, m in enumerate(lag_minutes):
        if rolling_lag[m] < -lag_threshold_pct and rolling_lag[m] < worst_val:
            worst_val = rolling_lag[m]
            worst_idx = i
    if worst_idx is None:
        return None

    worst_minute = lag_minutes[worst_idx]
    for i in range(worst_idx + 1, len(lag_minutes)):
        m = lag_minutes[i]
        elapsed = i - worst_idx
        if elapsed >= CONFIRM_MINUTES and rolling_lag[m] > rolling_lag[worst_minute] * 0.5:
            return m, stock_closes[m]
    return None


def _t01_stock_close(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def simulate_trailing_raw(
    fut_cache: dict, stock_id: str, t01: str, entry_frac_of_close: float,
) -> float | None:
    """回傳未扣成本的原始報酬(%)，成本另外在evaluate階段按各cost水準扣."""
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
    peak = fut_entry
    for h in range(1, MAX_HOLD_DAYS + 1):
        d = dates[i0 + h]
        px = float(m[d][1])
        if px <= 0:
            return None
        peak = max(peak, px)
        pullback = (peak - px) / peak * 100
        if pullback >= TRAIL_PCT or h == MAX_HOLD_DAYS:
            return (px / fut_entry - 1) * 100
    return None


def metrics(raw_rets: list[float], cost_pct: float) -> dict:
    """raw_rets = 未扣成本報酬；套用指定cost_pct(單邊已經是round-trip%)算net."""
    if not raw_rets:
        return {"n": 0}
    net = np.array(raw_rets) - cost_pct
    win_rate = float(np.mean(net > 0))
    mean_ret = float(net.mean())
    std_ret = float(net.std())
    sharpe_like = mean_ret / std_ret if std_ret > 0 else float("nan")
    gains = net[net > 0].sum()
    losses = -net[net < 0].sum()
    pf = float(gains / losses) if losses > 0 else float("inf")
    return {
        "n": len(net), "win_rate": win_rate, "mean_ret_pct": mean_ret,
        "std_ret_pct": std_ret, "sharpe_like": sharpe_like, "profit_factor": pf,
    }


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    fut_cache = json.loads(FUT_CACHE_PATH.read_text(encoding="utf-8"))

    prepared = []
    for t in trades:
        sid, t01 = t["stock"], t["trade_date"]
        stock_closes = load_minute_closes(con, sid, t01)
        bench_closes = load_minute_closes(con, BENCH, t01)
        if len(stock_closes) < 50 or len(bench_closes) < 50:
            continue
        day_close = _t01_stock_close(con, sid, t01)
        if day_close is None or day_close <= 0:
            continue
        prepared.append({"stock": sid, "trade_date": t01, "day_close": day_close,
                          "stock_closes": stock_closes, "bench_closes": bench_closes})

    print(f"=== 成本敏感度壓力測試（進場=rolling_relative_dip，出場=移動停利{TRAIL_PCT:.0f}%）===")
    print(f"可分析: {len(prepared)}/{len(trades)}\n")

    dates_sorted = sorted({p["trade_date"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])
    print(f"訓練期日數={len(train_dates)} 測試期日數={len(test_dates)}\n")

    def raw_rets_for(dates_set, lag_th) -> list[float]:
        recs = []
        for p in prepared:
            if p["trade_date"] not in dates_set:
                continue
            sig = find_rolling_dip_signal(p["stock_closes"], p["bench_closes"], lag_th)
            if sig is None:
                continue
            _, px = sig
            r = simulate_trailing_raw(fut_cache, p["stock"], p["trade_date"], px / p["day_close"])
            if r is not None:
                recs.append(r)
        return recs

    # 步驟1：只在訓練期、用5bps口徑挑進場落後門檻（沿用原腳本行為，這一步不變）
    print("--- 訓練期：挑進場落後門檻（沿用原腳本，固定5bps成本） ---")
    train_by_th = {}
    for th in LAG_THRESHOLD_CANDIDATES_PCT:
        raw = raw_rets_for(train_dates, th)
        m = metrics(raw, ENTRY_SELECTION_COST_PCT)
        train_by_th[th] = m
        print(
            f"[落後門檻{th:.1f}%] n={m.get('n',0)} sharpe_like={m.get('sharpe_like',float('nan')):.3f}"
        )
    eligible = [th for th in LAG_THRESHOLD_CANDIDATES_PCT if train_by_th[th].get("n", 0) >= MIN_TRAIN_N]
    if not eligible:
        eligible = list(LAG_THRESHOLD_CANDIDATES_PCT)
    best_th = max(eligible, key=lambda th: train_by_th[th].get("sharpe_like", -999) or -999)
    print(f"\n訓練期挑出（n≥{MIN_TRAIN_N}才列入候選）：落後門檻 {best_th:.1f}%、移動停利固定{TRAIL_PCT:.0f}%\n")

    # 步驟2：鎖住訊號與出場邏輯，只在測試期把 round-trip cost 當唯一變數 sweep
    test_raw_rets = raw_rets_for(test_dates, best_th)
    print(f"=== 樣本外測試期（n={len(test_raw_rets)}）：round-trip cost sweep（唯一變數） ===\n")
    print(f"{'cost(bps)':>10} {'n':>5} {'win_rate':>9} {'mean_net_ret%':>14} {'sharpe_like':>12} {'profit_factor':>14}")

    sweep_results = {}
    for bps in COST_SWEEP_BPS:
        cost_pct = bps / 100.0  # bps -> %
        m = metrics(test_raw_rets, cost_pct)
        sweep_results[bps] = m
        pf = m.get("profit_factor", float("nan"))
        pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
        print(
            f"{bps:>10d} {m.get('n',0):>5d} {m.get('win_rate',0)*100:>8.1f}% "
            f"{m.get('mean_ret_pct',0):>+13.3f}% {m.get('sharpe_like',float('nan')):>12.3f} {pf_str:>14}"
        )

    # breakeven：mean_net_ret_pct 由正轉負的線性內插（相鄰兩個sweep點之間）
    bps_sorted = sorted(sweep_results)
    means = [sweep_results[b]["mean_ret_pct"] for b in bps_sorted]
    breakeven_bps = None
    for i in range(len(bps_sorted) - 1):
        b0, b1 = bps_sorted[i], bps_sorted[i + 1]
        m0, m1 = means[i], means[i + 1]
        if m0 >= 0 and m1 < 0:
            frac = m0 / (m0 - m1) if (m0 - m1) != 0 else 0.0
            breakeven_bps = b0 + frac * (b1 - b0)
            break
    breakeven_extrapolated_bps = None
    if breakeven_bps is None:
        if means[0] < 0:
            breakeven_bps = f"<{bps_sorted[0]}（連最低5bps都已淨虧）"
        else:
            # 成本是flat減項，mean_net對bps完全線性（同一批raw報酬、只換扣除量），
            # 用頭尾兩點外推交叉點，估計「若滑價遠超sweep上限，大約要多少bps才會轉負」
            b0, b1 = bps_sorted[0], bps_sorted[-1]
            m0, m1 = means[0], means[-1]
            slope = (m1 - m0) / (b1 - b0) if (b1 - b0) != 0 else 0.0
            breakeven_extrapolated_bps = b0 - m0 / slope if slope != 0 else None
            extra = (
                f"（線性外推≈{breakeven_extrapolated_bps:.0f}bps）"
                if breakeven_extrapolated_bps is not None
                else ""
            )
            breakeven_bps = f">{bps_sorted[-1]}（在整個sweep區間內均未轉負）{extra}"

    sharpe_breakeven_bps = None
    sharpes = [sweep_results[b]["sharpe_like"] for b in bps_sorted]
    for i in range(len(bps_sorted) - 1):
        if sharpes[i] >= 0 and sharpes[i + 1] < 0:
            b0, b1 = bps_sorted[i], bps_sorted[i + 1]
            s0, s1 = sharpes[i], sharpes[i + 1]
            frac = s0 / (s0 - s1) if (s0 - s1) != 0 else 0.0
            sharpe_breakeven_bps = b0 + frac * (b1 - b0)
            break

    print(f"\nmean_net_ret 轉負的約略breakeven成本 ≈ {breakeven_bps if isinstance(breakeven_bps, str) else f'{breakeven_bps:.1f}bps'}")
    if sharpe_breakeven_bps is not None:
        print(f"sharpe_like 轉負的約略breakeven成本 ≈ {sharpe_breakeven_bps:.1f}bps")
    else:
        print("sharpe_like 在整個sweep區間內未轉負" if sharpes[0] >= 0 else "sharpe_like 在最低成本點就已為負")

    m13 = None
    m76 = sweep_results.get(76)
    print(
        "\n=== 對照：config/order.yaml「已知問題：真實滑價未實測（估計13~76bps區間）」==="
    )
    print(f"（此區間下界13bps未在sweep清單中，內插估計）")
    # 13bps 用線性內插（10, 20之間)
    if 10 in sweep_results and 20 in sweep_results:
        m10, m20 = sweep_results[10]["mean_ret_pct"], sweep_results[20]["mean_ret_pct"]
        m13 = m10 + (m20 - m10) * (13 - 10) / (20 - 10)
        print(f"  13bps（內插）：mean_net_ret ≈ {m13:+.3f}%")
    if m76:
        print(f"  76bps（實測sweep點）：mean_net_ret={m76.get('mean_ret_pct',0):+.3f}% sharpe_like={m76.get('sharpe_like',float('nan')):.3f}")

    edge_robust = (m13 is not None and m13 > 0) and (m76 is not None and m76.get("mean_ret_pct", -1) > 0)
    print(
        f"\n判定：策略邊際在「已知問題」估計區間(13-76bps)"
        f"{'兩端都仍為正（相對穩健）' if edge_robust else '不是全區間都為正（邊際依賴接近5bps的樂觀假設）'}。"
    )

    print(
        "\n⚠️ 限制：\n"
        "  1) 用0050取代台指期做進場訊號（同前幾輪，FinMind無期貨1分K資料集）；\n"
        "     出場用日頻期貨收盤，同前幾輪未做盤中即時停利。\n"
        "  2) 進場門檻(落後門檻)、移動停利(5%)、進場門檻挑選時用的5bps成本口徑，\n"
        "     這三者均沿用先前腳本已驗證/已鎖定的結果，本腳本刻意只變動cost，\n"
        "     沒有針對每個cost水準重新挑一次落後門檻或移動停利——這代表本結果\n"
        "     低估了「真實高滑價環境下，策略設計者原本就會採取更保守參數」的\n"
        "     可能調整空間，是保守但非最保守的估計。\n"
        "  3) 樣本外測試期n偏小（day-clustered，見trial registry既有pitfall記錄），\n"
        "     單點sharpe_like/mean_ret估計本身有雜訊，breakeven只是點估計非信賴區間。\n"
        "  4) 13bps本身是內插值（sweep清單沒有精確13這一點），76bps是sweep清單裡的實測點。"
    )

    robustness_note = (
        "區間兩端仍為正、相對穩健"
        if edge_robust
        else "未能在整個13-76bps區間維持正邊際，edge依賴接近5bps的樂觀成本假設，須列為未解決的部署前提"
    )
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="cost-sensitivity-breakeven",
        ts="2026-08-09",
        params={
            "cost_sweep_bps": list(COST_SWEEP_BPS),
            "entry_lag_threshold_pct": best_th,
            "trail_pct": TRAIL_PCT,
            "max_hold_days": MAX_HOLD_DAYS,
            "benchmark": BENCH,
        },
        n_observations=len(test_raw_rets),
        metric_name="oos_mean_ret_breakeven_cost_bps",
        metric_value=(
            breakeven_bps
            if isinstance(breakeven_bps, (int, float))
            else (breakeven_extrapolated_bps if breakeven_extrapolated_bps is not None else float("nan"))
        ),
        status="kept" if edge_robust else "rejected",
        source=__file__,
        notes=(
            f"成本敏感度壓力測試（進場=rolling_relative_dip {best_th:.1f}%門檻、"
            f"出場=移動停利{TRAIL_PCT:.0f}%，兩者均沿用先前腳本已鎖定結果，僅變動"
            f"round-trip cost）。樣本外(n={len(test_raw_rets)})：cost=5bps時mean_net_ret="
            f"{sweep_results[5].get('mean_ret_pct',0):+.3f}%，cost=76bps時="
            f"{sweep_results.get(76,{}).get('mean_ret_pct',float('nan')):+.3f}%。"
            f"mean_net_ret breakeven≈"
            f"{breakeven_bps if isinstance(breakeven_bps,str) else f'{breakeven_bps:.1f}bps'}。"
            f"對照config/order.yaml『已知問題：真實滑價未實測（估計13~76bps區間）』：{robustness_note}"
        ),
        tags=["dayflip-short", "post-dump", "long-side", "cost-sensitivity", "slippage-stress"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
