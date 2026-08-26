#!/usr/bin/env python3
"""全市場分點的程式化交易指紋掃描（第一階段：只用 DB，不花 API）。

## 為什麼這樣設計

反向工程 9661 學到的教訓：
· 分點是**多客戶共用通道**，整體側寫只會得到容量約束
· 真正的程式藏在**小單**裡（用金額門檻篩會整個濾掉）
· 程式的指紋不在「交易什麼」而在「怎麼交易」——廣度、規律性、持續性

因此第一階段完全不看標的、只看**行為統計量**：

  breadth   同日交易檔數（程式跑篩選 → 多；人工 → 少）
  regular   同日檔數的穩定度 1−CV（程式天天一樣多）
  presence  活躍日佔比（程式每天都在）
  dt_ratio  當沖度中位（分辨當沖程式 vs 部位型）
  size_cv   單筆金額的離散度（程式規格化 → 低）
  lot_mode  最常見張數的佔比（程式常固定 1~2 張）
  hhi       同日金額集中度（程式分散 → 低）
  part_med  參與率中位（footprint）

⚠️ DB 的分點資料不完整（對 9661 少收 13.7% 標的），但這些都是**相對**統計量，
缺漏會壓低 breadth 的絕對值、不影響分點之間的排序。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

OUT = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"


EVERY = 1


def scan(start: str, min_days: int) -> pd.DataFrame:
    c = connect_ro()
    dates = [r[0] for r in c.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars WHERE trade_date>=? ORDER BY trade_date",
        (start,))]
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date>=? AND close>0""", c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))
    pmap = px.set_index(["stock_id", "trade_date"])
    parts = []
    t0 = time.time()
    dates = dates[::EVERY]
    for i, d in enumerate(dates):
        b = pd.read_sql_query(
            """SELECT securities_trader_id AS tid, stock_id, buy, sell
                 FROM stock_broker_branch_daily
                WHERE trade_date=? AND (buy>0 OR sell>0)""", c, params=(d,))
        if b.empty:
            continue
        p = pmap.xs(d, level="trade_date", drop_level=True)
        b = b.join(p, on="stock_id", how="inner")
        if b.empty:
            continue
        b["gross"] = (b.buy + b.sell) * b.close
        b["lots"] = (b.buy + b.sell) / 1000.0
        b["part"] = b.lots / b.vol.replace(0, np.nan)
        b["rt"] = np.minimum(b.buy, b.sell) / np.maximum(b.buy, b.sell).replace(0, np.nan)
        # ⚠️ 2026-08-26 修正：第一版把「廣度/規律/出席/分散」當程式指標，
        # 結果前 25 名全是大型散戶分點（廣度 600~1400、當沖度 0.000），
        # 而確定是程式的自營商（富邦自營/元大自營）排到最後。
        # 那四個指標全部是「客戶數多」的代理，不是「程式」的代理。
        # 正解：分點會混上千客戶，必須先在分點內切出**行為子群**再評分。
        # 當沖子群（rt>0.9）是最容易分離、也最可能是程式的那一群。
        b["is_dt"] = b.rt > 0.9
        g = b.groupby("tid")
        dtg = b[b.is_dt].groupby("tid")
        agg = pd.DataFrame({
            "n": g.stock_id.size(),
            "tot": g.gross.sum(),
            "size_med": g.gross.median(),
            "size_cv": g.gross.std() / g.gross.mean().replace(0, np.nan),
            "rt_med": g.rt.median(),
            "part_med": g.part.median(),
            "hhi": g.gross.apply(lambda s: ((s / s.sum()) ** 2).sum() if s.sum() else np.nan),
            "lot1": g.lots.apply(lambda s: (s.round() <= 2).mean()),
            # 當沖子群
            "dt_n": dtg.stock_id.size(),
            "dt_gross": dtg.gross.sum(),
            "dt_part": dtg.part.median(),
            "dt_size": dtg.gross.median(),
            "dt_size_cv": dtg.gross.std() / dtg.gross.mean().replace(0, np.nan),
        })
        agg["dt_n"] = agg.dt_n.fillna(0)
        agg["dt_share"] = agg.dt_gross.fillna(0) / agg.tot.replace(0, np.nan)
        agg["trade_date"] = d
        parts.append(agg.reset_index())
        if i % 100 == 99:
            print(f"  {i+1}/{len(dates)}　{(time.time()-t0)/60:.1f} 分", flush=True)
    raw = pd.concat(parts, ignore_index=True)
    raw.to_pickle(OUT / "branch_algo_daily.pkl")
    nd = len(dates)
    G = raw.groupby("tid")
    f = pd.DataFrame({
        "days": G.trade_date.nunique(),
        "breadth": G.n.median(),
        "breadth_cv": G.n.std() / G.n.mean().replace(0, np.nan),
        "size_med": G.size_med.median(),
        "size_cv": G.size_cv.median(),
        "rt_med": G.rt_med.median(),
        "part_med": G.part_med.median(),
        "hhi": G.hhi.median(),
        "lot1": G.lot1.median(),
        "tot": G.tot.sum(),
    })
    f["presence"] = f.days / nd
    f["regular"] = 1 - f.breadth_cv.clip(0, 2) / 2
    f["dt_n"] = G.dt_n.median()
    f["dt_share"] = G.dt_share.median()
    f["dt_part"] = G.dt_part.median()
    f["dt_size"] = G.dt_size.median()
    f["dt_size_cv"] = G.dt_size_cv.median()
    f["dt_n_cv"] = G.dt_n.std() / G.dt_n.mean().replace(0, np.nan)
    f = f[f.days >= min_days].copy()

    # 程式分數（修正版）：焦點放在**當沖子群**的規律性，而非分點整體的廣度。
    #   dt_n      當沖子群檔數：程式會同時做多檔（但不必像散戶分點那樣上千）
    #   dt_stable 當沖檔數的穩定度：程式天天差不多，人工忽多忽少
    #   dt_share  當沖佔分點總額比：程式主導這個分點的程度
    #   dt_part   當沖參與率：程式有實質 footprint，散戶碎單沒有
    #   dt_size_cv 當沖部位大小離散度：程式規格化
    #   presence  出席率
    f["dt_stable"] = 1 - f.dt_n_cv.clip(0, 2) / 2
    f["r_dt_n"] = np.log1p(f.dt_n).rank(pct=True)
    f["r_dt_stable"] = f.dt_stable.rank(pct=True)
    f["r_dt_share"] = f.dt_share.rank(pct=True)
    f["r_dt_part"] = f.dt_part.rank(pct=True)
    f["r_dt_size_cv"] = 1 - f.dt_size_cv.rank(pct=True)
    f["r_presence"] = f.presence.rank(pct=True)
    f["algo_score"] = f[["r_dt_n", "r_dt_stable", "r_dt_share",
                         "r_dt_part", "r_dt_size_cv", "r_presence"]].mean(axis=1)
    return f.sort_values("algo_score", ascending=False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--min-days", type=int, default=100)
    ap.add_argument("--every", type=int, default=1, help="每 N 個交易日取一日（加速）")
    args = ap.parse_args()
    globals()["EVERY"] = args.every
    f = scan(args.start, args.min_days)
    f.to_pickle(OUT / "branch_algo_fingerprint.pkl")
    print(f"\n掃到 {len(f)} 個分點（活躍 ≥{args.min_days} 日）\n")
    nm = {}
    c = connect_ro()
    # ⚠️ 用 trade_date>=? 查名稱會全表掃描 2.24 億列（第一版卡在這裡）。
    # 取幾個單日即可湊齊名稱對照。
    for d in ("2026-08-25", "2026-08-24", "2025-06-10", "2024-06-10"):
        for tid, n in c.execute(
                "SELECT DISTINCT securities_trader_id, securities_trader "
                "FROM stock_broker_branch_daily WHERE trade_date=?", (d,)):
            nm.setdefault(tid, n)
    print(f"{'分點':<7}{'名稱':<13}{'程式分':>7}{'當沖檔':>7}{'穩定':>6}{'當沖佔比':>9}"
          f"{'當沖參與':>9}{'規格化':>7}{'出席':>6}{'總廣度':>7}")
    for r in f.head(30).itertuples():
        print(f"{r.Index:<7}{str(nm.get(r.Index,''))[:12]:<13}{r.algo_score:>7.3f}"
              f"{r.dt_n:>7.0f}{r.dt_stable:>6.2f}{r.dt_share*100:>8.1f}%"
              f"{r.dt_part*100:>8.2f}%{1-r.dt_size_cv if pd.notna(r.dt_size_cv) else float('nan'):>7.2f}"
              f"{r.presence:>6.2f}{r.breadth:>7.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
