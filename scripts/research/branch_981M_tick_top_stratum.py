#!/usr/bin/env python3
"""補跑 core33 子群的「最賺」層，與既有結果合併後重算 Spearman。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pandas as pd
from scipy import stats as sps

sys.path.insert(0, str(Path(__file__).resolve().parent))
from branch_981M_tick_liquidity import one  # noqa: E402

D = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
TID, N = "981M", 70


def main() -> int:
    m = pd.read_pickle(D / f"branch_{TID}_joined.pkl")
    att = m.groupby("stock_id").trade_date.nunique()
    m = m[m.stock_id.isin(att[att >= 300].index)]
    d = m[(m.rt > 0.3) & (m.dt_noti > 2e6)].dropna(subset=["spread"]).sort_values("spread")
    prev = pd.read_pickle(D / f"branch_{TID}_core33_tick_liq.pkl")
    have = set(zip(prev.stock_id, prev.trade_date))
    rows = []
    todo = [r for r in d.tail(N).itertuples() if (r.stock_id, r.trade_date) not in have]
    print(f"補跑最賺層 {len(todo)} 個")
    for i, r in enumerate(todo):
        try:
            a = one(TID, r.stock_id, r.trade_date)
            if a:
                rows.append({"stratum": "最賺", "spread_agg": r.spread,
                             "noti": r.dt_noti, "rt": r.rt, **a})
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {r.stock_id} {r.trade_date}: {str(exc)[:80]}")
        time.sleep(0.5)
        if i % 20 == 19:
            print(f"  {i+1}/{len(todo)} ok={len(rows)}", flush=True)
    out = pd.concat([prev, pd.DataFrame(rows)], ignore_index=True)
    out = out.drop_duplicates(["stock_id", "trade_date"])
    out.to_pickle(D / f"branch_{TID}_core33_tick_liq.pkl")
    print(f"\n合併後 {len(out)} 個案例")
    print(f"{'層':<6}{'n':>4}{'價差%':>9}{'買偏離':>9}{'賣偏離':>9}{'流動性':>9}"
          f"{'市場內盤%':>11}{'買時點':>8}{'賣時點':>8}{'佔量%':>8}")
    for k in ("最賠", "中位", "最賺"):
        g = out[out.stratum == k]
        if g.empty:
            continue
        print(f"  {k:<4}{len(g):>4}{g.spread_lv.median():>+8.3f}%{g.b_rel.median():>+9.2f}"
              f"{g.s_rel.median():>+9.2f}{g.liq.median():>+9.2f}{g.mkt_inner.median():>10.1f}%"
              f"{g.buy_t.median():>8.3f}{g.sell_t.median():>8.3f}{g.share.median():>7.2f}%")
    v = out.dropna(subset=["liq", "spread_lv"])
    print(f"\nSpearman(流動性指標, 逐筆重算價差) = {sps.spearmanr(v.liq, v.spread_lv)[0]:+.3f}"
          f"　p={sps.spearmanr(v.liq, v.spread_lv)[1]:.2e}　n={len(v)}")
    print(f"Spearman(流動性指標, 分價聚合價差) = {sps.spearmanr(v.liq, v.spread_agg)[0]:+.3f}"
          f"　p={sps.spearmanr(v.liq, v.spread_agg)[1]:.2e}")
    print(f"流動性指標中位 {v.liq.median():+.2f}　平均 {v.liq.mean():+.2f}　"
          f">0 比例 {(v.liq>0).mean()*100:.1f}%")
    print(f"買偏離中位 {v.b_rel.median():+.2f}　賣偏離中位 {v.s_rel.median():+.2f}")
    print(f"買時點中位 {v.buy_t.median():.3f}　賣時點中位 {v.sell_t.median():.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
