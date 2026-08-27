#!/usr/bin/env python3
"""dayflip-short：分點觸發跳空、隔日沖倒貨之後，有沒有適合做多的相對低點？

使用者問題：「那隔天隔日沖倒貨之後，會有適合做多的相對低點嗎」——
dayflip-short 抓的是分點T0買超、T0+1跳空後被隔日沖客倒貨（放空這段）。這裡反過來
問：倒貨完（T0+1當天低點之後，或T0+1之後的幾天）股價會不會反彈，值得找點做多？

兩段分析：
  (1) 當天（T0+1）：低點出現在哪個時段？從當天低點反彈到收盤，平均能拉回多少？
  (2) 隔日沖倒貨之後的多日走勢：從T0+1收盤起算，未來1/2/3/5/10個交易日的遠期報酬
      是正的（有反彈/均值回歸）還是繼續破底（隔日沖只是賣壓的開始，不是結束）？
      同時抓「未來N天內的最低點」，量後續有沒有再破底、破底之後反彈幅度多少。

資料源同 dayflip_short_t0_intraday_pattern_study.py：all_trades.csv（221筆）。
T0+1 1分K需先跑：
  PYTHONPATH=src .venv/bin/python scripts/research/backfill_dayflip_t0_kbar.py --date-field trade_date

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_post_dump_bounce_study.py
"""

from __future__ import annotations

import csv
import sqlite3
from collections import Counter
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
FWD_HORIZONS = (1, 2, 3, 5, 10)


def bucket_of(minute: str) -> str | None:
    for lo, hi, label in _BUCKETS:
        if lo <= minute < hi:
            return label
    if minute == "13:30":
        return _BUCKETS[-1][2]
    return None


def load_trades() -> list[dict]:
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def intraday_bounce_features(con: sqlite3.Connection, stock_id: str, t01: str) -> dict | None:
    # 2026-08-08 code review 發現：見 dayflip_short_t0_intraday_pattern_study.py 的
    # load_t0_bars() 同一則註解——原本沒篩 source，finmind/yahoo 混在一起量級對不上。
    raw = load_kbar_day_bars(con, stock_id, t01)
    bars = [
        (b.minute[:5], b.open, b.high, b.low, b.close, b.volume)
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30"
    ]
    if len(bars) < 50:
        return None
    minutes = [b[0] for b in bars]
    lows = np.array([b[3] for b in bars], dtype=float)
    closes = np.array([b[4] for b in bars], dtype=float)

    day_close = closes[-1]
    lo_idx = int(np.argmin(lows))
    day_low = lows[lo_idx]
    if day_low <= 0:
        return None
    low_bucket = bucket_of(minutes[lo_idx])
    ret_low_to_close = (day_close / day_low - 1) * 100
    return {
        "low_bucket": low_bucket,
        "ret_low_to_close_pct": ret_low_to_close,
        "day_close": day_close,
    }


_BENCH = "0050"  # 元大台灣50 ETF，當大盤 beta 代理，扣掉一般多頭飄移


def _close_on(con: sqlite3.Connection, stock_id: str, trade_date: str) -> float | None:
    row = con.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source='finmind' AND close>0",
        (stock_id, trade_date),
    ).fetchone()
    return float(row[0]) if row else None


def forward_daily_returns(con: sqlite3.Connection, stock_id: str, t01: str) -> dict | None:
    base_close = _close_on(con, stock_id, t01)
    if base_close is None:
        return None

    fwd_rows = con.execute(
        """
        SELECT trade_date, close FROM stock_daily_bars
        WHERE stock_id=? AND trade_date>? AND source='finmind' AND close>0
        ORDER BY trade_date LIMIT 15
        """,
        (stock_id, t01),
    ).fetchall()
    if len(fwd_rows) < max(FWD_HORIZONS):
        return None
    dates = [str(r[0]) for r in fwd_rows]
    closes = [float(r[1]) for r in fwd_rows]

    # 0050 當大盤 beta 基準：用「跟個股完全相同的實際交易日序列」查 0050 收盤，
    # 排除掉「這段回測窗剛好是大盤多頭」造成的假反彈訊號。
    bench_base = _close_on(con, _BENCH, t01)
    if bench_base is None:
        return None
    bench_closes = [_close_on(con, _BENCH, d) for d in dates]
    if any(c is None for c in bench_closes[: max(FWD_HORIZONS)]):
        return None

    out: dict[str, float] = {}
    for h in FWD_HORIZONS:
        stock_ret = (closes[h - 1] / base_close - 1) * 100
        bench_ret = (bench_closes[h - 1] / bench_base - 1) * 100
        out[f"fwd_ret_{h}d_pct"] = stock_ret
        out[f"fwd_excess_ret_{h}d_pct"] = stock_ret - bench_ret

    window = closes[: max(FWD_HORIZONS)]
    trough_idx = int(np.argmin(window))
    trough_close = window[trough_idx]
    out["max_drawdown_pct"] = (trough_close / base_close - 1) * 100  # negative if it fell further
    out["trough_day_offset"] = trough_idx + 1
    out["bounce_from_trough_to_10d_pct"] = (window[-1] / trough_close - 1) * 100 if trough_close > 0 else np.nan
    return out


def onesample_report(name: str, vals: list[float]) -> float | None:
    arr = np.array([v for v in vals if v == v])
    if len(arr) < 10:
        print(f"  {name}: 樣本不足(n={len(arr)})，跳過")
        return None
    t, p = stats.wilcoxon(arr) if len(arr) < 500 else stats.ttest_1samp(arr, 0.0)
    print(
        f"  {name}: mean={arr.mean():+.3f}±{arr.std():.3f} median={np.median(arr):+.3f} "
        f"(n={len(arr)}, %>0={np.mean(arr>0)*100:.0f}%) · one-sample p={p:.4f}"
    )
    return p


def main() -> None:
    trades = load_trades()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row  # load_kbar_day_bars() 需要 dict-like row access

    intraday_rows = []
    fwd_rows = []
    for t in trades:
        ib = intraday_bounce_features(con, t["stock"], t["trade_date"])
        if ib:
            ib["win"] = float(t["pnl_pct"]) > 0
            intraday_rows.append(ib)
        fd = forward_daily_returns(con, t["stock"], t["trade_date"])
        if fd:
            fd["win"] = float(t["pnl_pct"]) > 0
            fd["trade_date"] = t["trade_date"]
            fwd_rows.append(fd)

    print("=== (1) T0+1 當天：倒貨低點出現在哪個時段 ===")
    print(f"可分析樣本: {len(intraday_rows)}/{len(trades)}\n")

    low_bucket_counts = Counter(r["low_bucket"] for r in intraday_rows)
    print("--- 當日低點出現時段分布 ---")
    for _, _, label in _BUCKETS:
        n = low_bucket_counts.get(label, 0)
        print(f"  {label}: {n} 筆 ({n/len(intraday_rows)*100:.1f}%)")

    print(
        "\n⚠️ 「低點→收盤報酬」這個統計刻意不報：定義上就幾乎保證是正的（收盤價\n"
        "   幾乎不可能低於當天最低價的定義本身），且盤中當下不可能知道「現在是\n"
        "   今天的最低點」才進場——這不是可交易的訊號，只是回頭看的定義性產物，\n"
        "   報出來會誤導。"
    )

    print("\n=== (2) 隔日沖倒貨之後：多日遠期報酬 vs 0050（大盤beta基準）===")
    print(f"可分析樣本: {len(fwd_rows)}/{len(trades)}")
    print("原始報酬看起來全部強烈正值，但這段回測窗（2024-07~2026-07）大盤本身可能")
    print("就是多頭，所以同時報「扣掉0050同期報酬」的超額報酬，才能知道是不是真的")
    print("有股票特定的反彈訊號，還是只是搭到大盤的順風車。\n")
    pvals = []
    for h in FWD_HORIZONS:
        raw = [r[f"fwd_ret_{h}d_pct"] for r in fwd_rows]
        excess = [r[f"fwd_excess_ret_{h}d_pct"] for r in fwd_rows]
        print(f"[T0+1+{h}日]")
        onesample_report("  原始報酬%(未扣大盤)", raw)
        p = onesample_report("  超額報酬%(已扣0050同期)", excess)
        if p is not None:
            pvals.append(p)

    n_dates = len({r["trade_date"] for r in fwd_rows})
    print(
        f"\n⚠️ 獨立性檢查：{len(fwd_rows)}筆交易只落在 {n_dates} 個不同的 trade_date"
        f"（{sum(1 for d in Counter(r['trade_date'] for r in fwd_rows).values() if d>1)}天有"
        "超過1筆同天交易）——上面的 p 值假設樣本互相獨立，但同一天觸發的多檔股票\n"
        "   报酬高度相關（同一天大盤/市場情緒共同驅動），實際獨立樣本數遠小於"
        f"{len(fwd_rows)}，p 值被低估（看起來比實際更顯著）。改成「每個 trade_date"
        "先取當天平均超額報酬」再測一次，這樣的獨立樣本數才等於實際天數："
    )
    by_date: dict[str, list[dict]] = {}
    for r in fwd_rows:
        by_date.setdefault(r["trade_date"], []).append(r)
    clustered_pvals = []
    for h in FWD_HORIZONS:
        day_means = [
            float(np.mean([r[f"fwd_excess_ret_{h}d_pct"] for r in recs]))
            for recs in by_date.values()
        ]
        p = onesample_report(f"  [日層級,n={len(day_means)}] T0+1+{h}日 超額報酬%", day_means)
        if p is not None:
            clustered_pvals.append(p)

    print("\n--- 未來10個交易日內的路徑形狀（描述性統計，非交易訊號）---")
    onesample_report("未來10日內最大跌幅(相對T0+1收盤)%", [r["max_drawdown_pct"] for r in fwd_rows])
    offsets = Counter(r["trough_day_offset"] for r in fwd_rows)
    print("  區間最低點發生在第幾個交易日的分布:")
    for d in range(1, 11):
        n = offsets.get(d, 0)
        if n:
            print(f"    T0+1+{d}日: {n} 筆 ({n/len(fwd_rows)*100:.1f}%)")
    print(
        "  ⚠️ 下面「從區間最低點反彈到第10日」踩到跟(1)一樣的問題——事後才知道哪天\n"
        "     是最低點，不能當進場依據，這裡只是描述路徑形狀，不是可執行訊號："
    )
    onesample_report("    從區間最低點反彈到第10日收盤%", [r["bounce_from_trough_to_10d_pct"] for r in fwd_rows])

    print("\n--- 多重比較校正（用日層級的p值，不是被同天多筆交易灌水的逐筆p值）---")
    if clustered_pvals:
        bonf = 0.05 / len(clustered_pvals)
        print(f"  {len(clustered_pvals)}個遠期報酬窗口，Bonferroni門檻={bonf:.4f}")
        survivors_h = [h for h, p in zip(FWD_HORIZONS, clustered_pvals) if p < bonf]
        print(f"  校正後仍顯著的窗口: {survivors_h if survivors_h else '無'}")

    # trial registry：記「已扣大盤+已做日層級聚合」的超額報酬p值——逐筆p值會被
    # 同一天多檔股票的相關性灌水，不能拿來當「這個訊號本身有沒有edge」的判準。
    excess_5d = [
        r["fwd_excess_ret_5d_pct"] for r in fwd_rows if r["fwd_excess_ret_5d_pct"] == r["fwd_excess_ret_5d_pct"]
    ]
    bonf_final = 0.05 / len(clustered_pvals) if clustered_pvals else float("nan")
    best_p = min(clustered_pvals) if clustered_pvals else float("nan")
    survives = best_p == best_p and best_p < bonf_final
    append_trial(
        "dayflip_short_gapup_short",
        topic_id="post-dump-bounce-long-opportunity",
        ts="2026-08-08",
        params={"horizons_days": list(FWD_HORIZONS), "benchmark": _BENCH},
        n_observations=n_dates,
        metric_name="fwd_excess_ret_5d_mean_pct_vs_0050",
        metric_value=float(np.mean(excess_5d)) if excess_5d else float("nan"),
        status="kept" if survives else "rejected",
        source=__file__,
        notes=(
            f"問：T0+1隔日沖倒貨之後有沒有適合做多的相對低點。逐筆n={len(fwd_rows)}但只跨"
            f"{n_dates}個不同trade_date（87%的交易跟別的交易同一天，非獨立）——用日層級"
            f"聚合後重測，5個窗口(1/2/3/5/10日)超額報酬(已扣0050)最小p={best_p:.4f}，"
            f"Bonferroni門檻={bonf_final:.4f}。詳見腳本輸出，含逐筆版本(較不保守)當對照。"
        ),
        tags=["dayflip-short", "post-dump", "mean-reversion", "long-side"],
    )
    print("\n(已記入 reports/research/_trial_registry/dayflip_short_gapup_short.jsonl)")


if __name__ == "__main__":
    main()
