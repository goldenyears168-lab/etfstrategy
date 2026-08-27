#!/usr/bin/env python3
"""9217 子群逐筆重建：流動性指標 = 買進內盤偏離 − 賣出內盤偏離。

沿用 branch_liquidity_scan 的口徑（同一份指標定義才能跟全市場 baseline 比）：
  b_rel = (買進價位的市場內盤比 加權平均 − 當日市場內盤比) × 100
  s_rel = 賣出同理；liq = b_rel − s_rel（>0 = 提供流動性）

分層取樣：對指定子群按當日價差分最賠/中位/最賺三層各 N 個 stock-day。
⚠️ 分價「股」／逐筆「張」差 1000 倍；part 已在指標端除 1000。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from finmind_client import fetch_finmind, fetch_taiwan_stock_trading_daily_report

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"


def secs(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def tick_profile(tk: pd.DataFrame):
    tk = tk.copy()
    tk["p"] = pd.to_numeric(tk.deal_price, errors="coerce")
    tk["v"] = pd.to_numeric(tk.volume, errors="coerce")
    tk["t"] = tk.Time.map(secs)
    tk = tk.dropna(subset=["p", "v", "t"])
    if tk.empty or tk.v.sum() == 0:
        return None
    g = tk.groupby("p")
    inner = tk[tk.TickType.astype(str) == "2"].groupby("p").v.sum()
    prof = pd.DataFrame({
        "mv": g.v.sum(), "inner": inner,
        "t_mean": g.apply(lambda x: (x.t * x.v).sum() / x.v.sum(), include_groups=False),
    }).fillna({"inner": 0.0})
    prof["inner_ratio"] = prof.inner / prof.mv
    prof.attrs["t0"] = tk.t.min()
    prof.attrs["span"] = max(tk.t.max() - tk.t.min(), 1)
    prof.attrs["mkt_inner"] = tk[tk.TickType.astype(str) == "2"].v.sum() / tk.v.sum()
    prof.attrs["mkt_vol"] = tk.v.sum()
    prof.attrs["hi"] = tk.p.max()
    prof.attrs["lo"] = tk.p.min()
    prof.attrs["n_tick"] = len(tk)
    return prof


def one(tid: str, sid: str, day: str) -> dict | None:
    lv = pd.DataFrame(fetch_taiwan_stock_trading_daily_report(trade_date=day, data_id=sid))
    if lv.empty:
        return None
    for c in ("price", "buy", "sell"):
        lv[c] = pd.to_numeric(lv[c], errors="coerce")
    lv = lv.dropna(subset=["price"])
    mine = lv[lv.securities_trader_id == tid]
    if mine.empty or mine.buy.sum() == 0 or mine.sell.sum() == 0:
        return None
    tk = pd.DataFrame(fetch_finmind("TaiwanStockPriceTick", sid,
                                    date.fromisoformat(day), date.fromisoformat(day)))
    prof = tick_profile(tk) if not tk.empty else None
    if prof is None:
        return None
    j = mine.set_index("price").join(prof, how="inner")
    j["price"] = j.index
    if j.empty or j.buy.sum() == 0 or j.sell.sum() == 0:
        return None
    mi = prof.attrs["mkt_inner"]
    t0, span, mv = prof.attrs["t0"], prof.attrs["span"], prof.attrs["mkt_vol"]

    def w(col, wt):
        return (j[col] * j[wt]).sum() / j[wt].sum()

    b_rel = (w("inner_ratio", "buy") - mi) * 100
    s_rel = (w("inner_ratio", "sell") - mi) * 100
    # 當日價差（tick 口徑，%）
    rng_pct = (prof.attrs["hi"] / prof.attrs["lo"] - 1) * 100
    return {
        "stock_id": sid, "trade_date": day,
        "b_rel": b_rel, "s_rel": s_rel, "liq": b_rel - s_rel,
        "buy_inner": w("inner_ratio", "buy"), "sell_inner": w("inner_ratio", "sell"),
        "mkt_inner": mi,
        "buy_t": (w("t_mean", "buy") - t0) / span,
        "sell_t": (w("t_mean", "sell") - t0) / span,
        "buy_share": j.buy.sum() / 1000.0 / mv,
        "n_lvl": len(j), "day_range_pct": rng_pct,
        "n_branch": lv.securities_trader_id.nunique(),
        "my_vwap_spread": (w("price", "sell") / w("price", "buy") - 1) * 100,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trader", default="9217")
    ap.add_argument("--n", type=int, default=70)
    ap.add_argument("--rt", type=float, default=0.95)
    ap.add_argument("--minnoti", type=float, default=5e6)
    ap.add_argument("--tag", default="pure")
    args = ap.parse_args()

    m = pd.read_pickle(DIR / f"branch_{args.trader}_joined.pkl")
    d = m[(m.rt > args.rt) & (m.dt_noti > args.minnoti)].dropna(subset=["spread"]).copy()
    d = d.sort_values("spread")
    n, mid = args.n, len(d) // 2
    strata = {"最賠": d.head(n), "中位": d.iloc[mid - n // 2: mid + n // 2], "最賺": d.tail(n)}
    print(f"母體 {len(d):,} 個 stock-day（rt>{args.rt}、名目>{args.minnoti/1e4:.0f}萬）", flush=True)
    rows, t0 = [], time.time()
    todo = [(k, r) for k, v in strata.items() for r in v.itertuples()]
    for i, (lab, r) in enumerate(todo):
        try:
            a = one(args.trader, r.stock_id, r.trade_date)
            if a:
                rows.append({"stratum": lab, "spread_pct": r.spread,
                             "dt_noti": r.dt_noti, "part": r.part, **a})
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.45)
        if i % 30 == 29:
            print(f"  {i+1}/{len(todo)}　{(time.time()-t0)/60:.1f} 分　成功 {len(rows)}", flush=True)
    out = pd.DataFrame(rows)
    out.to_pickle(DIR / f"branch_{args.trader}_tickrecon_{args.tag}.pkl")
    print(f"\n成功重建 {len(out)} 個案例\n")
    if out.empty:
        return 0
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
    for xcol, ycol, nm in (("liq", "day_range_pct", "liq vs 當日價差"),
                           ("liq", "spread_pct", "liq vs 損益價差"),
                           ("b_rel", "spread_pct", "b_rel vs 損益價差")):
        v = out.dropna(subset=[xcol, ycol])
        if len(v) > 10:
            rho, p = stats.spearmanr(v[xcol], v[ycol])
            print(f"Spearman({nm})　rho={rho:+.3f}　p={p:.2e}　n={len(v)}")
    print(f"\nliq 總體：中位 {out.liq.median():+.2f}　平均 {out.liq.mean():+.2f}　"
          f"t={out.liq.mean()/(out.liq.std(ddof=1)/np.sqrt(len(out))):+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
