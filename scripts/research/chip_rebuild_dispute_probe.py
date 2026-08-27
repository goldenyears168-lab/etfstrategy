#!/usr/bin/env python3
"""chip-orthogonal-rebuild 綜合階段：DISPUTED 因子（F1/F2/F3/F4）分歧來源定位。

A 方 neutral_t：F1 −5.20 / F2 −3.29 / F3 +1.04 / F4 −1.30
B 方 neutral_t：F1 −3.79 / F2 −2.78 / F3 −1.24 / F4 −1.81

兩邊 raw_t 互差 <3%（factor 建構幾乎相同），分歧集中在中性化控制變數。
已知實作差異（讀碼比對）：
  (a) turnover 控制：A=volume/股本(short_limit×4，缺→vol/vol20)；B=一律 volume/vol20
  (b) 控制變數 z 分數：B clip(-5,5)；A 不 clip
  (c) F1/F3 diff：B 加了「前一列須為前一交易日，否則 NaN」；A 照 SSOT 純 diff
  (d) F4：A 依 SSOT fillna(0)；B 剔除無成交日（已知定義差，非 bug）
本 probe 在 A 面板上逐一疊加 B 的選擇，看哪一項把 A 的 t 推向 B 的 t。
輸出：reports/research/chip-orthogonal-rebuild/dispute_probe.json
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports/research/chip-orthogonal-rebuild"
PANEL = OUT_DIR / "panel.pkl"
NW_LAG, MIN_N = 5, 30
HIST_START, WIN_START, WIN_END = "2023-06-01", "2024-07-01", "2026-08-26"


def nw_t(x):
    x = np.asarray(x, float); x = x[~np.isnan(x)]; n = len(x)
    if n < 20: return np.nan
    m = x.mean(); e = x - m; s = (e @ e) / n
    for k in range(1, NW_LAG + 1):
        s += 2 * (1 - k / (NW_LAG + 1)) * ((e[k:] @ e[:-k]) / n)
    return m / np.sqrt(max(s, 1e-18) / n)


def zs(v, clip=None):
    sd = np.nanstd(v)
    z = np.zeros_like(v) if (not np.isfinite(sd) or sd == 0) else (v - np.nanmean(v)) / sd
    return np.clip(z, -clip, clip) if clip else z


def fm_t(df, col, tcol, clip=None):
    betas = []
    for _, g in df.groupby("trade_date", sort=True):
        g = g.dropna(subset=[col, "r_oc", "vol60", "gap", tcol])
        if len(g) < MIN_N: continue
        n = len(g)
        fr = (g[col].rank(method="average").to_numpy() - 1) / (n - 1) - 0.5
        X = np.column_stack([np.ones(n), fr,
                             zs(g["vol60"].to_numpy(float), clip),
                             zs(g["gap"].to_numpy(float), clip),
                             zs(g[tcol].to_numpy(float), clip)])
        try:
            betas.append(np.linalg.lstsq(X, g["r_oc"].to_numpy(float), rcond=None)[0][1])
        except np.linalg.LinAlgError:
            continue
    return float(nw_t(betas)), len(betas)


def main():
    panel = pd.read_pickle(PANEL)
    uni = panel[panel["in_universe"]].copy()

    # B 式 turnover：一律 volume / vol20（自 DB 重算 vol20，含暖身）
    c = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    prio = {"twse_mi_index": 0, "tpex_daily": 1, "finmind": 2, "yfinance": 3}
    bars = pd.read_sql_query(
        """SELECT stock_id, trade_date, volume, source FROM stock_daily_bars
            WHERE trade_date BETWEEN ? AND ?""", c, params=("2024-01-02", WIN_END))
    caldf = pd.read_sql_query(
        """SELECT trade_date, COUNT(*) n FROM stock_daily_bars
            WHERE source IN ('twse_mi_index','tpex_daily','finmind')
              AND trade_date BETWEEN ? AND ?
            GROUP BY trade_date HAVING n > 500""", c, params=("2024-01-02", WIN_END))
    cal = sorted(caldf.trade_date)
    bars = bars[bars.trade_date.isin(set(cal))]
    bars["prio"] = bars.source.map(prio).fillna(9)
    bars = (bars.sort_values(["stock_id", "trade_date", "prio"])
                .drop_duplicates(["stock_id", "trade_date"], keep="first"))
    bars = bars.sort_values(["stock_id", "trade_date"])
    bars["vol20b"] = bars.groupby("stock_id", group_keys=False).volume.transform(
        lambda x: x.rolling(20, min_periods=15).mean())
    bars["turnover_B"] = bars.volume / bars.vol20b.where(bars.vol20b > 0)
    uni = uni.merge(bars[["stock_id", "trade_date", "turnover_B"]],
                    on=["stock_id", "trade_date"], how="left")

    # (c) B 式 contiguity z1/zu 重算
    h = pd.read_sql_query(
        """SELECT stock_id, trade_date, sbl_balance, sbl_next_limit
             FROM stock_short_interest_daily WHERE trade_date BETWEEN ? AND ?""",
        c, params=(HIST_START, WIN_END))
    c.close()
    calf = [d for d in cal if d >= "2024-01-02"]
    # 完整日曆（含 2023 暖身）：直接用面板日曆邏輯——以 SBL 表本身在官方日曆內的日期
    h = h[h.trade_date.isin(set(pd.read_pickle(PANEL).trade_date) | set(cal) |
                            set(h.trade_date[h.trade_date < "2024-01-02"]))]
    h = h.drop_duplicates(["stock_id", "trade_date"]).sort_values(["stock_id", "trade_date"])
    h["util"] = h.sbl_balance / (h.sbl_balance + h.sbl_next_limit)
    g = h.groupby("stock_id", group_keys=False)
    h["d_sbl"] = g.sbl_balance.diff()
    h["d_util"] = g.util.diff()
    # contiguity：前一列日期必須是全體 SBL 日曆上的前一日
    all_days = sorted(h.trade_date.unique())
    idx = {d: i for i, d in enumerate(all_days)}
    h["ci"] = h.trade_date.map(idx)
    noncontig = (h.ci - g.ci.shift(1)) != 1
    n_noncontig = int((noncontig & h.d_sbl.notna()).sum())
    h2 = h.copy()
    h2.loc[noncontig, ["d_sbl", "d_util"]] = np.nan
    for hh, suf in ((h, ""), (h2, "_ctg")):
        gg = hh.groupby("stock_id", group_keys=False)
        for col, src in (("z1", "d_sbl"), ("zu", "d_util")):
            mu = gg[src].transform(lambda x: x.rolling(60, min_periods=30).mean())
            sd = gg[src].transform(lambda x: x.rolling(60, min_periods=30).std())
            hh[col + suf] = (hh[src] - mu) / sd.replace(0, np.nan)
    uni = uni.merge(h2[["stock_id", "trade_date", "z1_ctg", "zu_ctg"]],
                    on=["stock_id", "trade_date"], how="left")

    out = {"n_noncontig_diff_rows": n_noncontig, "variants": {}}
    grid = [
        ("F1_z1", "z1"), ("F2_zp", "zp"), ("F3_zu", "zu"), ("F4_zf", "zf"),
    ]
    for fid, col in grid:
        row = {}
        row["A_baseline(turnover=vol/shares, noclip)"] = fm_t(uni, col, "turnover")
        row["swap_turnover_B(vol/vol20)"] = fm_t(uni, col, "turnover_B")
        row["swap_turnover_B+clip5"] = fm_t(uni, col, "turnover_B", clip=5)
        row["A_turnover+clip5"] = fm_t(uni, col, "turnover", clip=5)
        if col in ("z1", "zu"):
            row["A_turnover+contig_diff"] = fm_t(uni, col + "_ctg", "turnover")
            row["turnover_B+contig_diff"] = fm_t(uni, col + "_ctg", "turnover_B")
        out["variants"][fid] = {k: {"t": round(v[0], 3), "n_days": v[1]}
                                for k, v in row.items()}
        print(fid, json.dumps(out["variants"][fid], ensure_ascii=False))

    (OUT_DIR / "dispute_probe.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2))
    print("→", OUT_DIR / "dispute_probe.json")


if __name__ == "__main__":
    main()
