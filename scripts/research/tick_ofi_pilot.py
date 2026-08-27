#!/usr/bin/env python3
"""個股逐筆微結構先導試驗 —— 委託流不平衡（OFI）等因子有沒有隔日預測力。

## 為什麼值得試

repo 已有三條逐筆研究線（CCF micro-price、分鐘內 tick 排列、TAIFEX 秒內），
結論都是負的 —— **但那三條都是期貨與 CCF，不是個股**。
FinMind `TaiwanStockPriceTick` 帶 `TickType`（1=外盤主動買 / 2=內盤主動賣），
**個股層級的 OFI 從沒被測過**，是真正的空白。

## 先導規模

完整橫斷面面板要 450 檔 × 250 日 ≈ 25 小時，太貴。
先導：70 檔 × 55 日 ≈ 3,850 次呼叫 ≈ 55 分鐘。
靈敏度：每日 IC 的 SE ≈ 1/√70 = 0.12，55 日平均後 SE ≈ 0.016
→ 可偵測 |IC| > 0.032（t=2）。典型因子 IC 是 0.02~0.05，夠用。
若連方向都看不到，就不值得投 25 小時。

## 因子（全部只用當日 T 的逐筆，預測 T+1 開→收）

  ofi          (外盤量 − 內盤量) / 總量
  ofi_last30   最後 30 分鐘的 OFI（尾盤壓力）
  ofi_first30  前 30 分鐘的 OFI
  ofi_big      大單（前 5% 大的成交）的 OFI
  ofi_trend    後半場 OFI − 前半場 OFI（盤中轉向）
  vwap_dev     收盤 / 全日 VWAP − 1（收在均價之上還是之下）
  tsize_hhi    成交量在單筆之間的集中度（大單主導程度）
  n_trades     成交筆數（正規化後）
  kyle         |價格變動| / 成交量（價格衝擊，Kyle lambda 代理）
  rv_tick      逐筆報酬的實現波動

⚠️ 口徑與 chip_lab 一致：報酬用 open(T+1)→close(T+1)，
   並對波動/跳空/市值/週轉率做非線性中性化。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from finmind_client import fetch_finmind

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib.machinery import SourceFileLoader

LAB = SourceFileLoader("lab", str(Path(__file__).resolve().parent / "chip_lab.py")).load_module()
DIR = LAB.DIR

FEATS = ["ofi", "ofi_last30", "ofi_first30", "ofi_big", "ofi_trend",
         "vwap_dev", "tsize_hhi", "n_trades", "kyle", "rv_tick"]


def secs(t: str) -> float:
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def features(tk: pd.DataFrame) -> dict | None:
    tk = tk.copy()
    tk["p"] = pd.to_numeric(tk.deal_price, errors="coerce")
    tk["v"] = pd.to_numeric(tk.volume, errors="coerce")
    tk["t"] = tk.Time.map(secs)
    tk["side"] = np.where(tk.TickType.astype(str) == "1", 1, -1)   # 1=外盤 2=內盤
    tk = tk.dropna(subset=["p", "v", "t"])
    tk = tk[tk.v > 0]
    if len(tk) < 50:
        return None
    tot = tk.v.sum()
    if tot <= 0:
        return None

    def ofi(x):
        s = x.v.sum()
        return (x.v * x.side).sum() / s if s > 0 else np.nan

    t0, t1 = tk.t.min(), tk.t.max()
    mid = (t0 + t1) / 2
    last30 = tk[tk.t >= t1 - 1800]
    first30 = tk[tk.t <= t0 + 1800]
    big = tk[tk.v >= tk.v.quantile(0.95)]
    vwap = (tk.p * tk.v).sum() / tot
    ret = tk.p.pct_change().dropna()
    dp = abs(tk.p.iloc[-1] / tk.p.iloc[0] - 1)
    return {
        "ofi": ofi(tk),
        "ofi_last30": ofi(last30) if len(last30) > 10 else np.nan,
        "ofi_first30": ofi(first30) if len(first30) > 10 else np.nan,
        "ofi_big": ofi(big) if len(big) > 5 else np.nan,
        "ofi_trend": (ofi(tk[tk.t > mid]) - ofi(tk[tk.t <= mid])
                      if min((tk.t > mid).sum(), (tk.t <= mid).sum()) > 10 else np.nan),
        "vwap_dev": tk.p.iloc[-1] / vwap - 1,
        "tsize_hhi": ((tk.v / tot) ** 2).sum(),
        "n_trades": len(tk),
        "kyle": dp / tot if tot > 0 else np.nan,
        "rv_tick": ret.std() * np.sqrt(len(ret)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stocks", type=int, default=70)
    ap.add_argument("--dates", type=int, default=55)
    ap.add_argument("--start", default="2025-06-01")
    args = ap.parse_args()

    d = LAB.load()
    d = d[d.trade_date >= args.start]
    # 分層抽樣：依成交量三分層各抽 1/3，避免全是權值股
    liq = d.groupby("stock_id").vol.median()
    q = pd.qcut(liq.rank(method="first"), 3, labels=False)
    rng = np.random.default_rng(20260827)
    sids = []
    for k in range(3):
        pool = liq[q == k].index.to_numpy()
        sids += list(rng.choice(pool, min(args.stocks // 3, len(pool)), replace=False))
    dates = np.sort(d.trade_date.unique())
    picks = list(dates[np.linspace(0, len(dates) - 1, args.dates).astype(int)])
    cases = [(s, t) for t in picks for s in sids]
    print(f"先導：{len(sids)} 檔 × {len(picks)} 日 = {len(cases):,} 次呼叫", flush=True)

    out = DIR / "tick_ofi_pilot.pkl"
    rows, done = [], set()
    if out.exists():
        prev = pd.read_pickle(out)
        rows = prev.to_dict("records")
        done = set(zip(prev.stock_id, prev.trade_date))
        print(f"  已有 {len(done):,} 筆，續抓")
    t0, bad = time.time(), 0
    for i, (sid, day) in enumerate(cases):
        if (sid, day) in done:
            continue
        try:
            tk = pd.DataFrame(fetch_finmind("TaiwanStockPriceTick", sid,
                                            date.fromisoformat(day), date.fromisoformat(day)))
            f = features(tk) if not tk.empty else None
            if f:
                rows.append({"stock_id": sid, "trade_date": day, **f})
        except Exception:  # noqa: BLE001
            bad += 1
            time.sleep(1.0)
        time.sleep(0.2)
        if i % 300 == 299:
            el = (time.time() - t0) / 60
            print(f"  {i+1}/{len(cases)}　{el:.1f} 分　預估剩 "
                  f"{el / (i + 1) * (len(cases) - i - 1):.1f} 分　失敗 {bad}", flush=True)
            pd.DataFrame(rows).to_pickle(out)
    f = pd.DataFrame(rows).drop_duplicates(["stock_id", "trade_date"])
    f.to_pickle(out)
    print(f"\n完成 {len(f):,} 個 stock-day · {f.trade_date.nunique()} 日 · "
          f"{f.stock_id.nunique()} 檔　失敗 {bad}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
