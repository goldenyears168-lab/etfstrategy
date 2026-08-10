#!/usr/bin/env python3
"""聯電分點訊號 — 大盤校正版掃描（research only）.

延續 umc_branch_overnight_signal_sweep.py。前一支的結論是：分點訊號沒有獨立資訊，
它們只是在偵測「聯電當日大漲」，而「當日大漲→隔夜跳空」本身是 2025-09 之後才出現的
regime（訓練期完全平坦）。

本支加上兩層大盤校正：
  A. 中性化變數改用「聯電專屬當日漲幅」r0_idio = r0 − beta×tx_r0（beta 由訓練期估），
     而非原始 r0；並可再與大盤當日三分位做雙重分組。
  B. 新增一族「分點火力集中度」訊號 —— 該分點當日在聯電的成交量 ÷ 該分點當日在
     全市場的總成交量，再對該分點自身歷史做 z-score。這是 2303-only 資料推不出來的
     新資訊：同樣買 2 億，一家把八成火力壓在聯電、和一家順手掃一筆，意義不同。
     （股數口徑；因為對「分點自身歷史」做 z，各分點的股價結構偏誤會被抵銷。）

前置：/tmp/branch_mktwide.pkl、/tmp/mkt_tv.pkl（由 sweep 前的聚合步驟產生）
  PYTHONPATH=src .venv/bin/python scripts/research/umc_branch_market_calibrated_sweep.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import stock_db  # noqa: E402

STOCK = "2303"
SPLIT = "2025-09-01"
MIN_HIST = 30


def build_focus_signals() -> pd.DataFrame:
    """分點火力集中度訊號：聯電成交量佔該分點全市場成交量比重，對自身歷史 z-score。"""
    mw = pd.read_pickle("/tmp/branch_mktwide.pkl")
    conn = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    br = pd.read_sql(
        "select trade_date, securities_trader_id tid, buy, sell, net "
        f"from stock_broker_branch_daily where stock_id='{STOCK}' and trade_date>='2024-07-01'", conn)
    px = pd.read_sql(
        "select trade_date, close, volume from stock_daily_bars "
        f"where stock_id='{STOCK}' and trade_date>='2024-06-01'", conn).set_index("trade_date")
    conn.close()

    d = br.merge(mw, on=["trade_date", "tid"], how="left")
    d["gross_umc"] = d.buy + d.sell
    d["focus"] = d.gross_umc / d.gross_all.replace(0, np.nan)       # 火力集中度
    d["net_share_of_all"] = d.net / d.gross_all.replace(0, np.nan)  # 淨買相對自身總火力
    d = d.sort_values(["tid", "trade_date"])
    g = d.groupby("tid", sort=False)
    d["pn"] = g.cumcount()
    for c in ["focus", "net_share_of_all"]:
        mu = g[c].apply(lambda s: s.expanding().mean().shift(1)).values
        sd = g[c].apply(lambda s: s.expanding().std().shift(1)).values
        d[f"z_{c}"] = (d[c] - mu) / sd
    d = d[d.pn >= MIN_HIST]

    close = px.close
    d["amt"] = d.net * d.trade_date.map(close)
    tv = (px.volume * px.close)
    out = {}
    big = d[d.amt >= 1e8]
    out["focus_z_max"] = big.groupby("trade_date").z_focus.max()
    out["focus_z_mean"] = big.groupby("trade_date").z_focus.mean()
    out["n_focus_z2"] = d[(d.z_focus >= 2) & (d.amt >= 5e7)].groupby("trade_date").size()
    out["n_focus_z2_1e8"] = d[(d.z_focus >= 2) & (d.amt >= 1e8)].groupby("trade_date").size()
    out["netshare_z_max"] = big.groupby("trade_date").z_net_share_of_all.max()
    out["netshare_z_sum"] = d[d.z_net_share_of_all > 0].groupby("trade_date").z_net_share_of_all.sum()
    out["focus_wtd_net"] = (d.assign(w=d.z_focus.clip(lower=0) * d.amt)
                            .groupby("trade_date").w.sum()) / tv
    s = pd.DataFrame(out).reindex(sorted(d.trade_date.unique()))
    for c in ["n_focus_z2", "n_focus_z2_1e8"]:
        s[c] = s[c].fillna(0)
    # 聯電相對大盤的熱度
    mkt = pd.read_pickle("/tmp/mkt_tv.pkl").set_index("trade_date")
    s["umc_share_of_mkt"] = (tv / mkt.mkt_tv)
    s["umc_share_z"] = ((s.umc_share_of_mkt - s.umc_share_of_mkt.rolling(60, min_periods=20).mean().shift(1))
                        / s.umc_share_of_mkt.rolling(60, min_periods=20).std().shift(1))
    return s


def main() -> int:
    base = pd.read_pickle("/tmp/umc_mkt.pkl")     # 前一支產出，含 r0_idio / hedged / naked
    new = build_focus_signals()
    df = base.join(new, how="left")
    tr = df.index < SPLIT
    te = ~tr
    DROP = {"r0", "r_gap", "r_oc", "close", "tx_ov", "tx_r0", "naked", "hedged",
            "hedged_c", "b", "y_n", "r0_mkt", "r0_idio", "composite", "comp_tr",
            "vol", "tv", "vol_z", "turnover_z", "hit", "r0b", "umc_share_of_mkt"}
    SIG = [c for c in df.columns if c not in DROP]
    NEW = [c for c in new.columns if c not in DROP]
    print(f"樣本 {len(df)} 天　訓練 {tr.sum()} / 測試 {te.sum()}　新增大盤校正訊號 {len(NEW)} 支：{NEW}")

    def sweep(target, label, double=False):
        d = df.copy()
        d["q"] = pd.qcut(d.r0_idio, 5, labels=False).astype(str)
        if double:
            d["q"] = d.q + "_" + pd.qcut(d.tx_r0, 3, labels=False).astype(str)
        d["y"] = d[target] - d.groupby("q")[target].transform("mean")
        rows = []
        for s in SIG:
            x = d[s]
            if x.notna().sum() < 200 or x.nunique() < 5:
                continue
            ic = x[tr].corr(d.y[tr], method="spearman")
            side = 1 if (ic or 0) >= 0 else -1
            thr = (x[tr] * side).quantile(0.8)
            h = te & x.notna() & ((x * side) >= thr)
            r = te & x.notna() & ((x * side) < thr)
            if h.sum() < 15 or r.sum() < 15:
                continue
            dd = d.y[h].mean() - d.y[r].mean()
            sp = np.sqrt(d.y[h].var() / h.sum() + d.y[r].var() / r.sum())
            rows.append(dict(訊號=s, new="★" if s in NEW else "", dir="+" if side > 0 else "-",
                             ic_tr=round(ic, 3), ic_te=round(x[te].corr(d.y[te], method="spearman"), 3),
                             n=int(h.sum()), 超額=round(dd * 100, 2), t=round(dd / sp, 2),
                             勝率=round((d.y[h] > 0).mean() * 100, 0)))
        R = pd.DataFrame(rows).sort_values("t", ascending=False).reset_index(drop=True)
        print(f"\n=== {label}（{len(R)} 支，★=大盤校正新訊號）===")
        print(R.head(12).to_string(index=False))
        print(f"  t>2: {(R.t > 2).sum()} 支　最佳 t={R.t.max():.2f}　隨機期望 {len(R)*0.023:.1f} 支")
        return R

    r1 = sweep("hedged", "① hedged ｜ 聯電專屬當日漲幅五分位內中性化")
    sweep("naked", "② naked ｜ 同上")
    sweep("hedged", "③ hedged ｜ 雙重分組（聯電專屬 ×5 × 大盤 ×3）", double=True)

    # 存活者的測試期切半
    top = r1[r1.t > 1.5].訊號.tolist()
    if top:
        print("\n=== 測試期切半（t>1.5 者）===")
        d = df.copy()
        d["q"] = pd.qcut(d.r0_idio, 5, labels=False)
        d["y"] = d.hedged - d.groupby("q").hedged.transform("mean")
        for s in top:
            x = d[s]
            ic = x[tr].corr(d.y[tr], method="spearman")
            side = 1 if (ic or 0) >= 0 else -1
            thr = (x[tr] * side).quantile(0.8)
            out = []
            for lo, hi in [(SPLIT, "2026-03-01"), ("2026-03-01", "2027-01-01")]:
                m = (d.index >= lo) & (d.index < hi)
                h = m & ((x * side) >= thr)
                r = m & ((x * side) < thr)
                out.append(f"n={h.sum():3d} 超額{(d.y[h].mean()-d.y[r].mean())*100:+6.2f}%"
                           if h.sum() >= 8 and r.sum() >= 8 else "樣本不足")
            print(f"  {s:20s} H1 {out[0]}   H2 {out[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
