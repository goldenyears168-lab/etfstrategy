#!/usr/bin/env python3
"""補齊 9217 逐筆重建缺的分層（上一輪撞到 FinMind 每小時 6000 次上限）。

會先等配額回復，再只跑尚未成功的 stock-day，並與既有 pickle 合併。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parent))
from branch_9217_tick_recon import one  # noqa: E402

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
OUT = DIR / "branch_9217_tickrecon_pure.pkl"
TID = "9217"


def quota() -> int:
    tok = os.environ.get("FINMIND_TOKEN") or os.environ.get("FINMIND_API_TOKEN")
    try:
        j = requests.get("https://api.web.finmindtrade.com/v2/user_info",
                         params={"token": tok}, timeout=20).json()
        return int(j.get("user_count", 99999))
    except Exception:  # noqa: BLE001
        return 99999


def wait_quota(budget: int) -> None:
    """配額被別的 process 共用，只等到有一點空隙就走，並容忍失敗重試。"""
    for _ in range(20):
        u = quota()
        if 6000 - u > budget:
            return
        print(f"  配額 {u}/6000，等 60 秒", flush=True)
        time.sleep(60)


def main() -> int:
    m = pd.read_pickle(DIR / f"branch_{TID}_joined.pkl")
    d = m[(m.rt > 0.95) & (m.dt_noti > 5e6)].dropna(subset=["spread"]).sort_values("spread")
    n, mid = 70, len(d) // 2
    strata = {"最賠": d.head(n), "中位": d.iloc[mid - n // 2: mid + n // 2], "最賺": d.tail(n)}
    prev = pd.read_pickle(OUT) if OUT.exists() else pd.DataFrame()
    done = set(zip(prev.stock_id, prev.trade_date)) if len(prev) else set()
    todo = [(k, r) for k, v in strata.items() for r in v.itertuples()
            if (r.stock_id, r.trade_date) not in done]
    print(f"已完成 {len(prev)}，待補 {len(todo)}", flush=True)
    # 先補「最賺」層（上一輪整層缺失，不補會讓分層表與相關係數有選擇偏誤）
    order = {"最賺": 0, "中位": 1, "最賠": 2}
    todo.sort(key=lambda z: order.get(z[0], 9))
    rows, t0 = [], time.time()
    pending = list(todo)
    deadline = time.time() + 55 * 60
    while pending and time.time() < deadline:
        wait_quota(30)
        again = []
        for i, (lab, r) in enumerate(pending):
            if time.time() > deadline:
                again += pending[i:]
                break
            try:
                a = one(TID, r.stock_id, r.trade_date)
                if a:
                    rows.append({"stratum": lab, "spread_pct": r.spread,
                                 "dt_noti": r.dt_noti, "part": r.part, **a})
                    if len(rows) % 15 == 0:
                        print(f"  新增 {len(rows)}／剩 {len(pending)-i-1}　"
                              f"{(time.time()-t0)/60:.1f} 分", flush=True)
            except Exception:  # noqa: BLE001
                again.append((lab, r))
                if len(again) % 25 == 1:
                    u = quota()
                    print(f"    失敗 {len(again)}（配額 {u}/6000），稍後重試", flush=True)
                    if 6000 - u < 10:
                        again += pending[i + 1:]
                        break
            time.sleep(0.45)
        if len(again) == len(pending):
            time.sleep(120)
        pending = again
    print(f"  未補齊 {len(pending)} 個", flush=True)
    out = pd.concat([prev, pd.DataFrame(rows)], ignore_index=True) if rows else prev
    out = out.drop_duplicates(["stock_id", "trade_date"])
    out.to_pickle(OUT)
    print(f"\n合計 {len(out)} 個案例\n")
    print(f"{'層':<6}{'n':>4}{'價差%':>9}{'買時點':>8}{'賣時點':>8}{'買內盤%':>9}"
          f"{'賣內盤%':>9}{'市場內盤%':>10}{'b_rel':>8}{'s_rel':>8}{'liq':>8}{'佔量%':>8}")
    for k in ("最賠", "中位", "最賺"):
        g = out[out.stratum == k]
        if g.empty:
            continue
        print(f"  {k:<4}{len(g):>4}{g.spread_pct.median():>+8.3f}%{g.buy_t.median():>8.3f}"
              f"{g.sell_t.median():>8.3f}{g.buy_inner.median()*100:>8.1f}%"
              f"{g.sell_inner.median()*100:>8.1f}%{g.mkt_inner.median()*100:>9.1f}%"
              f"{g.b_rel.median():>+8.2f}{g.s_rel.median():>+8.2f}{g.liq.median():>+8.2f}"
              f"{g.buy_share.median()*100:>7.2f}%")
    for x, y, nm in (("liq", "day_range_pct", "liq vs 當日價差"),
                     ("liq", "spread_pct", "liq vs 損益價差"),
                     ("b_rel", "spread_pct", "b_rel vs 損益價差"),
                     ("s_rel", "spread_pct", "s_rel vs 損益價差")):
        v = out.dropna(subset=[x, y])
        if len(v) > 10:
            rho, p = stats.spearmanr(v[x], v[y])
            print(f"Spearman({nm})　rho={rho:+.3f}　p={p:.2e}　n={len(v)}")
    print(f"\nliq 總體：中位 {out.liq.median():+.2f}　平均 {out.liq.mean():+.2f}　"
          f"t={out.liq.mean()/(out.liq.std(ddof=1)/np.sqrt(len(out))):+.2f}　n={len(out)}")
    print(f"買時點中位 {out.buy_t.median():.3f}　賣時點中位 {out.sell_t.median():.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
