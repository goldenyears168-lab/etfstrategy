#!/usr/bin/env python3
"""全分點宇宙重掃 —— 用橫斷面去均值取代固定 β（修正 2026-08-18 查出的虛無假設錯誤）.

為什麼要重掃：既有的 774 家掃描結論是「BH-FDR 顯著 11 個、全部負向、無一正向」，
但那是拿各分點的 L1H7 β=1.15 超額報酬去跟**零**檢定。同協議的**無條件基準**是
median −1.829% / 勝率 36.4%（n=1,120,570，完全不挑股不挑日）——
拿 −2% 去跟 0 比，任何樣本夠大的分點都會「顯著為負」。
**那個掃描在結構上不可能找到正向候選。** 見 docs/research-integrity-checklist.md A16。

本腳本改用**橫斷面去均值**：報酬 = 個股 L1H7 − 同一訊號日全宇宙平均 L1H7。
基準因此在定義上為 0，正負號才有意義。

事件定義：某分點在某股單日淨買金額 >= --min-yi 億（預設 1.0）。
窗口：全市場 by-trader tape 完整期（2024-07-01 ~ 2026-07-16；之後塌縮成 ~9 席）。
"""
from __future__ import annotations
import argparse, json, sqlite3, statistics, sys
from collections import defaultdict
from pathlib import Path
import scipy.stats as ss
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
from stock_db import DEFAULT_DB_PATH  # noqa: E402

SOURCE = "finmind"
WS, WE = "2024-07-01", "2026-07-16"
OUT = ROOT / "reports/research/branch-footprint-screen"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    ap.add_argument("--min-yi", type=float, default=1.0, help="單日淨買金額門檻（億）")
    ap.add_argument("--min-n", type=int, default=30, help="分點最少事件數")
    ap.add_argument("--hold", type=int, default=7, help="持有交易日")
    ap.add_argument("--top", type=int, default=25)
    a = ap.parse_args()
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True); c.row_factory = sqlite3.Row

    px = {}
    for sid, td, o, cl in c.execute(
        "SELECT stock_id,trade_date,open,close FROM stock_daily_bars WHERE source=? "
        "AND trade_date BETWEEN ? AND date(?, '+40 day') AND length(stock_id)=4 "
        "AND stock_id GLOB '[0-9][0-9][0-9][0-9]' AND stock_id NOT GLOB '00*'", (SOURCE, WS, WE)):
        if o and cl: px.setdefault(sid, {})[td] = (float(o), float(cl))
    days = sorted({t for m in px.values() for t in m}); di = {t: i for i, t in enumerate(days)}
    print(f"價格宇宙 {len(px)} 檔 · {days[0]} ~ {days[-1]}")

    # 每個 (股,日) 的 L1H7 原始報酬，之後做同日橫斷面去均值
    fwd = {}
    for sid, dd in px.items():
        ds = sorted(dd)
        for j in range(len(ds) - a.hold):
            de, dx = ds[j + 0], ds[j + a.hold - 1]
            o = dd[ds[j]][0]
            if o > 0:
                fwd[(sid, ds[j])] = (dd[dx][1] / o - 1) * 100
    bym = defaultdict(list)
    for (sid, d), r in fwd.items(): bym[d].append(r)
    mean_by_day = {d: statistics.mean(v) for d, v in bym.items() if len(v) >= 30}

    thr = a.min_yi * 1e8
    ev = defaultdict(list)
    n_raw = 0
    for r in c.execute(
        """
        SELECT b.securities_trader_id AS bid, b.stock_id AS sid, b.trade_date AS d
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.source=? AND b.trade_date BETWEEN ? AND ?
          AND (b.buy-b.sell)*p.close >= ?
          AND length(b.stock_id)=4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        """, (SOURCE, SOURCE, WS, WE, thr)):
        n_raw += 1
        i = di.get(r["d"])
        if i is None or i + 1 >= len(days): continue
        nd = days[i + 1]                      # T+1 進場
        key = (r["sid"], nd)
        if key not in fwd or nd not in mean_by_day: continue
        ev[r["bid"]].append(fwd[key] - mean_by_day[nd])   # 橫斷面去均值
    print(f"事件（單日淨買 >= {a.min_yi} 億）原始 {n_raw:,} 筆 · 可評估 {sum(len(v) for v in ev.values()):,} 筆 · 分點 {len(ev)} 家")

    rows = []
    for bid, v in ev.items():
        if len(v) < a.min_n: continue
        v = sorted(v)
        rows.append({"bid": bid, "n": len(v), "mean": statistics.mean(v),
                     "median": statistics.median(v),
                     "win": sum(1 for x in v if x > 0) / len(v),
                     "p": ss.wilcoxon(v).pvalue if len(v) >= 10 else 1.0})
    m = len(rows)
    for r in rows:
        r["q"] = min(1.0, r["p"] * m / (sorted(x["p"] for x in rows).index(r["p"]) + 1))
    nm = {r[0]: r[1] for r in c.execute(
        "SELECT DISTINCT securities_trader_id, securities_trader FROM stock_broker_branch_daily")}
    rows.sort(key=lambda r: -r["median"])
    print(f"\n可測分點 {m} 家（n>={a.min_n}）· 基準在定義上為 0\n")
    print(f"{'分點':<7}{'名稱':<14}{'n':>6}{'mean%':>9}{'median%':>9}{'勝率':>8}{'Wilcoxon p':>12}{'q(BH)':>9}")
    print("── 最好的 %d 家 ──" % a.top)
    for r in rows[:a.top]:
        print(f"{r['bid']:<7}{(nm.get(r['bid']) or '')[:12]:<14}{r['n']:>6}{r['mean']:>9.3f}{r['median']:>9.3f}"
              f"{r['win']:>8.1%}{r['p']:>12.2e}{r['q']:>9.4f}")
    print("── 最差的 8 家 ──")
    for r in rows[-8:]:
        print(f"{r['bid']:<7}{(nm.get(r['bid']) or '')[:12]:<14}{r['n']:>6}{r['mean']:>9.3f}{r['median']:>9.3f}"
              f"{r['win']:>8.1%}{r['p']:>12.2e}{r['q']:>9.4f}")
    sig_pos = [r for r in rows if r["q"] <= 0.05 and r["median"] > 0]
    sig_neg = [r for r in rows if r["q"] <= 0.05 and r["median"] < 0]
    print(f"\nBH-FDR q<=0.05：正向 {len(sig_pos)} 家 · 負向 {len(sig_neg)} 家")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "branch_universe_rescan_demeaned.json").write_text(
        json.dumps({"window": [WS, WE], "min_yi": a.min_yi, "hold": a.hold, "min_n": a.min_n,
                    "n_branches": m, "sig_pos": len(sig_pos), "sig_neg": len(sig_neg),
                    "rows": sorted(rows, key=lambda r: -r["median"])}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"→ {OUT/'branch_universe_rescan_demeaned.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
