#!/usr/bin/env python3
"""低換手變體檢定 —— v4 名單每日換掉 77%，成本是訊號的 8 倍。

**動機**（2026-08-25 發現）：v4 的五個分項有三項是「變動量」
（Δ借券、Δ使用率、分點家數差橫斷面），變動量本質上天天翻，
導致 Top30 名單每日換手 73–77%。以來回成本 1.17%/日 計，
實際成本 ≈ 0.90%/日，而多空價差歷史期望只有 0.115%/日。

**這不是運氣問題、是設計問題**，而且可以在既有資料上先算清楚，
不必等前瞻樣本累積。

**檢定的是 net，不是 gross**：任何只報 gross spread 的比較都會
系統性偏好高換手變體——那正是 v4 現在的問題。

⚠️ 本檔測多個變體，多重檢定會膨脹顯著性。所有變體一律全部列出，
不做事後挑選；勝出者仍須進前瞻紀錄才算數。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports" / "research" / "chip-signal-daily-horizon" / "turnover_panel.pkl"
MIN_VOL_LOTS = 500
MIN_CLOSE = 10.0
N_SIDE = 30


def build_panel(start: str) -> pd.DataFrame:
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, open, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date >= ? AND close IS NOT NULL""",
        c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))
    ch = pd.read_sql_query(
        """SELECT s.stock_id, s.trade_date, s.sbl_balance, s.sbl_next_limit,
                  s.short_limit, f.fee_rate_vw
             FROM stock_short_interest_daily s
             LEFT JOIN (SELECT stock_id, trade_date, fee_rate_vw FROM stock_sbl_fee_daily
                         WHERE deal_type='ALL') f
               ON f.stock_id=s.stock_id AND f.trade_date=s.trade_date
            WHERE s.trade_date >= ?""", c, params=(start,))
    # ⚠️ 分點表 2.24 億列，唯一可用索引是 (stock_id, trade_date)。
    # `WHERE trade_date >= ?` 會退化成全索引掃描 + 回表讀 net（>30 分鐘沒跑完）；
    # 逐檔 seek 也不行（2330 單檔就 67 秒）。逐「日」查才快（0~1.1 秒/日），
    # 因為 SQLite 能對 (stock_id, trade_date) 做 skip-scan。
    # 不建 (trade_date) 索引是刻意的 —— 生產 DB 有夜盤排程在跑，寫入會鎖住。
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM stock_short_interest_daily WHERE trade_date >= ?"
        " ORDER BY trade_date", (start,))]
    parts = []
    for k, dt in enumerate(dates):
        parts.append(pd.read_sql_query(
            """SELECT stock_id, trade_date,
                      SUM(CASE WHEN net>0 THEN 1 ELSE 0 END) AS nb,
                      SUM(CASE WHEN net<0 THEN 1 ELSE 0 END) AS ns, COUNT(*) AS n
                 FROM stock_broker_branch_daily
                WHERE trade_date = ? AND net IS NOT NULL AND net<>0
                GROUP BY stock_id""", c, params=(dt,)))
        if k % 100 == 0:
            print(f"  分點聚合 {k}/{len(dates)} 日…", flush=True)
    br = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(
        columns=["stock_id", "trade_date", "nb", "ns", "n"])

    d = px.merge(ch, on=["stock_id", "trade_date"], how="inner")
    dup = d.duplicated(["stock_id", "trade_date"]).sum()
    if dup:
        raise RuntimeError(f"panel 有 {dup:,} 筆重複 stock-day")
    d = d.merge(br, on=["stock_id", "trade_date"], how="left")
    d = d.sort_values(["stock_id", "trade_date"])
    g = d.groupby("stock_id", group_keys=False)
    d["shares"] = (d.short_limit * 4).replace(0, np.nan)
    d["sbl_pct"] = d.sbl_balance / d.shares
    d["util"] = d.sbl_balance / (d.sbl_balance + d.sbl_next_limit)
    d["d_sbl"] = g.sbl_balance.diff()
    d["d_util"] = g.util.diff()

    def zs(col, win=60):
        mu = g[col].transform(lambda x: x.rolling(win, min_periods=30).mean())
        sd = g[col].transform(lambda x: x.rolling(win, min_periods=30).std())
        return (d[col] - mu) / sd.replace(0, np.nan)

    d["z1"] = zs("d_sbl")
    d["zu"] = zs("d_util")
    d["zp"] = (g.sbl_pct.transform(lambda x: x.rolling(243, min_periods=60).rank(pct=True)) - .5) * 4
    d["zf"] = (g.fee_rate_vw.transform(lambda x: x.rolling(60, min_periods=10).rank(pct=True)) - .5) * 4
    d["brdiff"] = (d.nb - d.ns) / d.n
    d["z6"] = (d.groupby("trade_date").brdiff.rank(pct=True) - .5) * 4
    for z in ("z1", "zp", "zu", "zf", "z6"):
        d[z] = d[z].fillna(0).clip(-2.5, 2.5)
    # 流動性濾網（與正式名單同口徑），並排除 ETF
    d = d[(d.vol >= MIN_VOL_LOTS) & (d.close >= MIN_CLOSE)]
    d = d[~d.stock_id.str.startswith("00")].copy()
    # 報酬：訊號日 T → 隔日 T+1。開→收才是可執行口徑
    g2 = d.sort_values(["stock_id", "trade_date"]).groupby("stock_id", group_keys=False)
    d["nx_open"] = g2.open.shift(-1)
    d["nx_close"] = g2.close.shift(-1)
    d["cc"] = d.nx_close / d.close - 1
    d["oc"] = d.nx_close / d.nx_open - 1
    for col in ("cc", "oc"):                       # 橫斷面去均值 = 扣掉大盤
        d[col] = d[col] - d.groupby("trade_date")[col].transform("mean")
    return d.dropna(subset=["cc", "oc"])


# --------------------------------------------------------------- 變體


def smooth(d: pd.DataFrame, col: str, win: int) -> pd.Series:
    return (d.sort_values(["stock_id", "trade_date"]).groupby("stock_id", group_keys=False)[col]
             .transform(lambda s: s.rolling(win, min_periods=1).mean()))


def variants(d: pd.DataFrame) -> dict[str, pd.Series]:
    """全部列出，不事後挑選。分數一律『越大越偏空』。"""
    def dead(s):                                    # v4 的 0.1 死區
        return np.where(np.abs(s) >= 0.1, s, 0.0)
    v4 = sum(dead(d[z]) for z in ("z1", "zp", "zu", "zf", "z6"))
    out = {
        "A v4 五項（基準）":      pd.Series(v4, index=d.index),
        "B 只用水位 zp+zf":       pd.Series(dead(d.zp) + dead(d.zf), index=d.index),
        "C 只用 zp":             pd.Series(dead(d.zp), index=d.index),
        "D 只用變動 z1+zu+z6":   pd.Series(dead(d.z1) + dead(d.zu) + dead(d.z6), index=d.index),
    }
    d = d.copy()
    d["_v4"] = out["A v4 五項（基準）"]
    d["_lv"] = out["B 只用水位 zp+zf"]
    for w in (5, 10, 20):
        out[f"E v4 平滑{w}日"] = smooth(d, "_v4", w)
    out["F 水位平滑5日"] = smooth(d, "_lv", 5)
    return out


def evaluate(d: pd.DataFrame, score: pd.Series, n_side: int,
             hyst: int | None = None) -> dict:
    """組多空各 n_side 檔的等權組合，算 gross、換手、以及損益兩平成本。

    hyst 給定時採遲滯：已持有的標的要掉出前 ``hyst`` 名才換掉，
    新標的仍須進前 ``n_side`` 名才納入。這是降換手最直接的手段。
    """
    x = d[["trade_date", "stock_id", "cc", "oc"]].copy()
    x["s"] = score.values
    rows, prev_l, prev_s = [], set(), set()
    turn = []
    for t, g in x.groupby("trade_date", sort=True):
        g = g.sort_values("s")
        if len(g) < n_side * 4:
            continue
        cand_l = list(g.stock_id.head(n_side))          # 分數低 = 偏多
        cand_s = list(g.stock_id.tail(n_side))
        if hyst:
            keep_l = [s for s in g.stock_id.head(hyst) if s in prev_l]
            keep_s = [s for s in g.stock_id.tail(hyst) if s in prev_s]
            long_ = (keep_l + [s for s in cand_l if s not in keep_l])[:n_side]
            short_ = (keep_s + [s for s in cand_s if s not in keep_s])[:n_side]
        else:
            long_, short_ = cand_l, cand_s
        L, S = set(long_), set(short_)
        # 換手＝Σ|Δw|/2，多空兩腿各 1/n_side 權重
        tv = (len(L - prev_l) + len(S - prev_s)) / n_side / 2 if (prev_l or prev_s) else np.nan
        turn.append(tv)
        gi = g.set_index("stock_id")
        rows.append({
            "trade_date": t,
            "cc": gi.loc[list(L), "cc"].mean() - gi.loc[list(S), "cc"].mean(),
            "oc": gi.loc[list(L), "oc"].mean() - gi.loc[list(S), "oc"].mean(),
        })
        prev_l, prev_s = L, S
    r = pd.DataFrame(rows).set_index("trade_date")
    r["turn"] = turn
    tau = r.turn.mean()
    res = {"n_days": len(r), "turnover": tau}
    for k in ("cc", "oc"):
        v = r[k].dropna()
        res[f"{k}_gross"] = v.mean() * 100
        res[f"{k}_t"] = v.mean() / (v.std(ddof=1) / np.sqrt(len(v))) if len(v) > 2 else np.nan
        res[f"{k}_be"] = (v.mean() * 100 / tau) if tau and tau > 0 else np.nan
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--oos-from", default="2025-01-01", help="此日之後算樣本外")
    ap.add_argument("--n-side", type=int, default=N_SIDE)
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    if args.rebuild or not CACHE.exists():
        print("建立面板…", flush=True)
        d = build_panel(args.start)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        d.to_pickle(CACHE)
    else:
        d = pd.read_pickle(CACHE)
    print(f"面板 {len(d):,} stock-day · {d.trade_date.nunique():,} 日 · "
          f"{d.trade_date.min()}~{d.trade_date.max()} · 每日均 {len(d)/d.trade_date.nunique():.0f} 檔\n")

    vs = variants(d)
    specs = [(k, v, None) for k, v in vs.items()]
    specs += [(f"G v4 遲滯 30/{h}", vs["A v4 五項（基準）"], h) for h in (60, 90, 120)]
    specs += [(f"H 水位 遲滯 30/{h}", vs["B 只用水位 zp+zf"], h) for h in (90,)]

    for tag, sub in (("全樣本", d.index),
                     ("樣本內 <" + args.oos_from, d.index[d.trade_date < args.oos_from]),
                     ("樣本外 ≥" + args.oos_from, d.index[d.trade_date >= args.oos_from])):
        dd = d.loc[sub]
        print(f"═══ {tag}（{dd.trade_date.nunique()} 日）═══")
        print(f"{'變體':<22}{'換手':>7}{'開→收 gross':>13}{'t':>7}{'損益兩平成本':>13}"
              f"{'收→收 gross':>13}{'t':>7}")
        for name, sc, h in specs:
            r = evaluate(dd, sc.loc[sub], args.n_side, h)
            print(f"  {name:<20}{r['turnover']*100:>6.1f}%{r['oc_gross']:>+12.4f}%"
                  f"{r['oc_t']:>+7.2f}{r['oc_be']:>+12.3f}%{r['cc_gross']:>+12.4f}%{r['cc_t']:>+7.2f}")
        print()
    print("損益兩平成本＝gross ÷ 換手率：實際來回成本高於此數，該變體就是虧的。")
    print("台股現實參考：多頭腿來回 ≈ 0.47%（手續費 2×0.0855% + 證交稅 0.3%），")
    print("空頭腿另加借券費，多空合計來回 ≈ 1.17%（本研究線一貫採用的估計）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
