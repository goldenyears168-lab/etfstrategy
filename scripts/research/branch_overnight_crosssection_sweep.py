#!/usr/bin/env python3
"""方案 B：分點隔夜訊號的橫斷面掃描（research only）.

前兩支腳本的結論：在聯電單股上，分點訊號沒有可驗證的 edge —— 32 支訊號、三層校正
（大盤／台指期／全市場火力）之後 0 支通過 t>2。根本限制是單股只有 477 個交易日，
而且「當日大漲」這個 confound 在時間序列設計裡怎麼控都控不乾淨。

橫斷面設計解決兩件事：
  1. 樣本從 477 天 × 1 檔 → 477 天 × N 檔
  2. 同日多空對沖天然中性化掉「當日大漲」「大盤走勢」「隔夜市場風險」——
     那正是時間序列版本裡殺死一切的東西，這裡是結構性消掉，不是統計上扣掉。

宇宙：個股期貨日均量 >= 2,000 口者（可實際做多空的標的）。
標的：T 收盤 → T+1 開盤的跳空，當日橫斷面 demean（= 多空對沖後的報酬）。
評估：每日橫斷面 Spearman IC → IC 序列的 Newey-West t 值；訓練/測試切分。

  PYTHONPATH=src .venv/bin/python scripts/research/branch_overnight_crosssection_sweep.py
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

SPLIT = "2025-09-01"
MIN_HIST = 30
MIN_NAMES = 12          # 當日至少要有這麼多檔才計橫斷面 IC
OUT = ROOT / "reports/research/branch-footprint-screen"


def universe() -> list[str]:
    return sorted(pd.read_pickle("/tmp/futuniv.pkl").index.tolist())


def load(univ: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    conn = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    ids = ",".join(f"'{s}'" for s in univ)
    px = pd.read_sql(
        f"select stock_id,trade_date,open,close,volume,source from stock_daily_bars "
        f"where stock_id in ({ids}) and trade_date>='2024-06-01' order by stock_id,trade_date", conn)
    br = pd.read_sql(
        f"select trade_date,stock_id,securities_trader_id tid,buy,sell,net "
        f"from stock_broker_branch_daily where stock_id in ({ids}) and trade_date>='2024-07-01'", conn)
    conn.close()
    # ⚠️ stock_daily_bars 同一 (stock_id, trade_date) 可能有多個 source（finmind / yfinance）。
    # 不去重會讓該股在橫斷面被重複加權——2330 幾乎全期雙份、另 12 檔各多 17 天，
    # 35 檔宇宙裡共 700/18,555 個 (股,日) 組合重複（2026-08-09 複核發現）。
    # 以 finmind 為主源（收盤管線 SSOT），其餘為 fallback。
    n_raw = len(px)
    px = (px.assign(_p=(px.source != "finmind").astype(int))
            .sort_values(["stock_id", "trade_date", "_p"])
            .drop_duplicates(["stock_id", "trade_date"], keep="first")
            .drop(columns="_p"))
    print(f"價量去重：{n_raw} → {len(px)} 列（移除 {n_raw - len(px)} 筆重複 source）")
    px = px.sort_values(["stock_id", "trade_date"])
    g = px.groupby("stock_id")
    px["r0"] = g.close.pct_change()
    px["gap"] = g.open.shift(-1) / px.close - 1
    return px, br


def signals(br: pd.DataFrame, px: pd.DataFrame) -> pd.DataFrame:
    p = px.set_index(["stock_id", "trade_date"])
    br = br.merge(px[["stock_id", "trade_date", "close", "volume"]], on=["stock_id", "trade_date"], how="left")
    br["amt"] = br.net * br.close
    br["gross"] = (br.buy + br.sell) * br.close
    br["tv"] = br.volume * br.close
    br = br.sort_values(["stock_id", "tid", "trade_date"])
    g = br.groupby(["stock_id", "tid"], sort=False)
    br["pn"] = g.cumcount()
    br["pmu"] = g.amt.transform(lambda s: s.expanding().mean().shift(1))
    br["psd"] = g.amt.transform(lambda s: s.expanding().std().shift(1))
    br["pgr"] = g.gross.transform(lambda s: s.expanding().mean().shift(1))
    br["z"] = (br.amt - br.pmu) / br.psd
    br = br[br.pn >= MIN_HIST]

    key = ["stock_id", "trade_date"]
    by = br.groupby(key)
    tv = by.tv.first()
    out = {}
    out["net_disp"] = by.amt.std() / tv
    out["top1_net_pct"] = by.amt.max() / tv
    out["top5_net_pct"] = br.groupby(key).amt.apply(lambda s: s.nlargest(5).sum()) / tv
    out["top1_z"] = by.z.max()
    out["sum_z_pos"] = br[br.z > 0].groupby(key).z.sum()
    out["sum_z_net"] = by.z.sum()
    out["n_z3"] = br[(br.z >= 3)].groupby(key).size()
    out["n_sell_z3"] = br[br.z <= -3].groupby(key).size()
    out["big_br_net_pct"] = br[br.pgr >= br.pgr.groupby(br.stock_id).transform("median") * 3] \
        .groupby(key).amt.sum() / tv
    out["hhi_buy"] = br[br.amt > 0].groupby(key).amt.apply(lambda s: ((s / s.sum()) ** 2).sum())
    out["buy_sell_br_ratio"] = (br[br.amt > 0].groupby(key).size()
                                / br[br.amt < 0].groupby(key).size())
    s = pd.DataFrame(out)
    for c in ["n_z3", "n_sell_z3"]:
        s[c] = s[c].fillna(0)
    return s.join(p[["r0", "gap"]], how="inner")


def nw_t(x: pd.Series, lag: int = 5) -> float:
    x = x.dropna().values
    n = len(x)
    if n < 20:
        return np.nan
    mu = x.mean()
    e = x - mu
    v = (e ** 2).sum() / n
    for L in range(1, lag + 1):
        w = 1 - L / (lag + 1)
        v += 2 * w * (e[L:] * e[:-L]).sum() / n
    return mu / np.sqrt(v / n)


def main() -> int:
    univ = universe()
    print(f"宇宙 {len(univ)} 檔（個股期日均量 ≥2,000 口）: {univ}")
    px, br = load(univ)
    print(f"價量 {len(px)} 列　分點 {len(br):,} 列")
    df = signals(br, px).reset_index()
    print(f"訊號面板 {len(df)} 列　{df.trade_date.nunique()} 天　{df.stock_id.nunique()} 檔")

    # 橫斷面 demean = 同日等權多空對沖後的報酬（結構性中性化掉大盤與當日市場動能）
    cnt = df.groupby("trade_date").gap.transform("size")
    df = df[cnt >= MIN_NAMES].copy()
    df["y"] = df.gap - df.groupby("trade_date").gap.transform("mean")
    # 也做一版「同日再扣掉當日漲跌的橫斷面關係」，擋掉當日動能
    df["r0_cs"] = df.r0 - df.groupby("trade_date").r0.transform("mean")
    df["y2"] = df.groupby("trade_date", group_keys=False).apply(
        lambda d: d.y - np.polyval(np.polyfit(d.r0_cs, d.y, 1), d.r0_cs) if len(d) >= 8 else d.y * np.nan)

    SIG = ["net_disp", "top1_net_pct", "top5_net_pct", "top1_z", "sum_z_pos", "sum_z_net",
           "n_z3", "n_sell_z3", "big_br_net_pct", "hhi_buy", "buy_sell_br_ratio"]
    tr = df.trade_date < SPLIT
    rows = []
    for s in SIG:
        for tgt in ["y", "y2"]:
            ic = df.dropna(subset=[s, tgt]).groupby("trade_date").apply(
                lambda d: d[s].corr(d[tgt], method="spearman") if len(d) >= MIN_NAMES else np.nan).dropna()
            ic_tr = ic[ic.index < SPLIT]
            ic_te = ic[ic.index >= SPLIT]
            if len(ic_tr) < 50 or len(ic_te) < 50:
                continue
            rows.append(dict(訊號=s, 標的=tgt, IC訓練=round(ic_tr.mean(), 4), IC測試=round(ic_te.mean(), 4),
                             t訓練=round(nw_t(ic_tr), 2), t測試=round(nw_t(ic_te), 2),
                             同號="✓" if np.sign(ic_tr.mean()) == np.sign(ic_te.mean()) else "",
                             天數訓練=len(ic_tr), 天數測試=len(ic_te)))
    R = pd.DataFrame(rows).sort_values("t測試", key=abs, ascending=False)
    print("\n=== 橫斷面 IC（y=同日等權對沖後跳空；y2=再扣當日橫斷面動能）===")
    print(R.to_string(index=False))
    surv = R[(R.同號 == "✓") & (R.t測試.abs() > 2) & (R.t訓練.abs() > 1)]
    print(f"\n  訓練/測試同號 且 |t測試|>2 且 |t訓練|>1 者：{len(surv)} 支"
          + ("　" + ", ".join(surv.訊號 + "/" + surv.標的) if len(surv) else "（無）"))

    # 存活者的多空組合報酬
    for _, row in surv.iterrows():
        s, tgt = row.訊號, row.標的
        side = np.sign(row.IC訓練)
        d = df.dropna(subset=[s, tgt]).copy()
        d["rk"] = d.groupby("trade_date")[s].rank(pct=True) * side
        te = d[d.trade_date >= SPLIT]
        lo = te[te.rk <= te.rk.quantile(0.2)][tgt].mean()
        hi = te[te.rk >= te.rk.quantile(0.8)][tgt].mean()
        print(f"  {s}/{tgt}: 測試期 多頭組 {hi*100:+.2f}% / 空頭組 {lo*100:+.2f}% / 價差 {(hi-lo)*100:+.2f}%")

    OUT.mkdir(parents=True, exist_ok=True)
    R.to_csv(OUT / "branch_overnight_crosssection_ic.csv", index=False)
    df.to_pickle("/tmp/cs_panel.pkl")
    print(f"\n結果寫入 {OUT/'branch_overnight_crosssection_ic.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
