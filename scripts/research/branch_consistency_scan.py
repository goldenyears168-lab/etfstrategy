#!/usr/bin/env python3
"""一致性掃描 —— 程式的本質是「一致」不是「頻繁」。

先前的偵測全部押在**頻率**上（廣度、日筆數、出席率），因此只找得到
外資執行演算法與大型散戶分點。但單壓型程式可能每天只做一兩筆，
其指紋在**一致性**：

  A 固定張數    反覆用同一個張數（程式化下單單位）
  B 固定金額    反覆用同一個名目金額（部位規模制）
  C 籌碼一致    挑的股票在籌碼因子空間中高度集中
  D 方向一致    net_bias 明顯偏多或偏空（決策型，非執行型）
  E 節奏一致    每日筆數穩定（即使只有 1~3 筆）

## 籌碼一致性怎麼量（本檔的核心）

隨機選股的分點，其標的的**當日橫斷面百分位**應為均勻分布 → SD ≈ 28.87。
有選股條件的程式，SD 會顯著低於 28.87。

  集中度 = 1 − SD(標的百分位) / 28.87        （0=隨機、越高越有條件）

並用 bootstrap 給出該分點在其樣本數下的隨機基準，避免小樣本假陽性
（樣本少時 SD 本來就會偏低）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

DIR = Path(__file__).resolve().parents[2] / "reports" / "research" / "chip-signal-daily-horizon"
UNIF_SD = 100 / np.sqrt(12)          # 均勻分布 U(0,100) 的標準差 = 28.87

FACTORS = [("mcap", "市值"), ("vol60", "波動"), ("turn", "週轉"),
           ("sbl_pct", "借券佔股本"), ("util", "券源使用率"), ("fee_rate_vw", "借券費率"),
           ("ret_pct", "散戶持股"), ("f_for", "外資買超"), ("f_itc", "投信買超"),
           ("f_conc", "分點集中度"), ("f_brdiff", "分點家數差")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-n", type=int, default=100)
    ap.add_argument("--boot", type=int, default=200)
    args = ap.parse_args()

    d = pd.read_pickle(DIR / "branch_band_trades.pkl")
    lab = pd.read_pickle(DIR / "chip_horizon_panel.pkl")
    rk = {c: (lab.groupby("trade_date")[c].rank(pct=True) * 100)
          for c, _ in FACTORS if c in lab.columns}
    key = pd.MultiIndex.from_arrays([lab.stock_id, lab.trade_date])
    pos = pd.Series(np.arange(len(lab)), index=key)

    d = d.copy()
    d["lots"] = d.gross / d.close / 1000.0
    idx = pd.MultiIndex.from_arrays([d.stock_id, d.trade_date])
    d["_p"] = pos.reindex(idx).to_numpy()
    d = d.dropna(subset=["_p"])
    d["_p"] = d._p.astype(int)
    print(f"千萬帶 {len(d):,} 筆可對到籌碼面板　{d.tid.nunique()} 個分點\n")

    rng = np.random.default_rng(20260827)
    rows = []
    for tid, g in d.groupby("tid"):
        if len(g) < args.min_n:
            continue
        r = {"tid": tid, "n": len(g), "days": g.trade_date.nunique(),
             "per_day": g.groupby("trade_date").size().median(),
             "stocks": g.stock_id.nunique(),
             "rt": g.rt.median(), "part": g.part.median(),
             "gross_med": g.gross.median(),
             "net_bias": g.net.sum() / g.gross.sum()}
        # A 固定張數：眾數佔比（四捨五入到整數張）
        lots = g.lots.round().astype(int)
        r["lot_mode"] = lots.value_counts().iloc[0] / len(lots)
        r["lot_top5"] = lots.value_counts().head(5).sum() / len(lots)
        # B 固定金額：對數金額的離散度（越小越規格化）
        r["gross_logsd"] = np.log10(g.gross).std()
        # C 籌碼一致：各因子百分位的集中度，並扣掉同樣本數的隨機基準
        cons, names = [], []
        for c, nmm in FACTORS:
            if c not in rk:
                continue
            v = rk[c].to_numpy()[g._p.to_numpy()]
            v = v[~np.isnan(v)]
            if len(v) < 50:
                continue
            sd = v.std()
            # 隨機基準用解析式，不用 bootstrap：
            # rank(pct=True) 逐日產生 U(0,1)，池化後仍是 U(0,100)，母體 SD = 28.87。
            # 樣本 SD 的期望值 ≈ σ·c4(n)，n≥50 時 c4 ≈ 1 − 1/(4n)，修正 <0.5%。
            # 原本對 376k 序列 bootstrap 27 萬次，跑 20 分鐘未完 —— 沒必要。
            base = UNIF_SD * (1 - 1 / (4 * len(v)))
            cons.append(1 - sd / base)
            names.append(nmm)
            r[f"c_{nmm}"] = 1 - sd / base
        r["chip_conc"] = max(cons) if cons else np.nan
        r["chip_conc_top"] = names[int(np.argmax(cons))] if cons else ""
        r["chip_conc_mean"] = float(np.mean(cons)) if cons else np.nan
        rows.append(r)
    f = pd.DataFrame(rows).set_index("tid")
    f.to_pickle(DIR / "branch_consistency.pkl")

    c = connect_ro()
    nm = {}
    for dd in ("2026-08-25", "2025-06-10"):
        for t, n in c.execute("SELECT DISTINCT securities_trader_id, securities_trader "
                              "FROM stock_broker_branch_daily WHERE trade_date=?", (dd,)):
            nm.setdefault(t, n)
    f["name"] = [nm.get(i, "?") for i in f.index]

    print(f"納入 {len(f)} 個分點（≥{args.min_n} 筆）\n")
    for col, lab_, asc in (("lot_mode", "A 固定張數（眾數佔比）", False),
                           ("gross_logsd", "B 固定金額（log10 金額 SD，越小越規格化）", True),
                           ("chip_conc", "C 籌碼一致（最集中的因子，扣隨機基準）", False),
                           ("net_bias", "D 方向一致（|net_bias| 大＝決策型）", False)):
        print(f"【{lab_}】")
        s = f.reindex(f[col].abs().sort_values(ascending=asc).index) if col == "net_bias" \
            else f.sort_values(col, ascending=asc)
        for r in s.head(8).itertuples():
            extra = f"　最集中於 {r.chip_conc_top}" if col == "chip_conc" else ""
            print(f"  {r.Index:<7}{str(r.name)[:12]:<13}{getattr(r, col):>+8.3f}"
                  f"　n={r.n:>6,}　日筆數 {r.per_day:>5.1f}　檔數 {r.stocks:>4}"
                  f"　中位額 {r.gross_med/1e4:>5.0f}萬{extra}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
