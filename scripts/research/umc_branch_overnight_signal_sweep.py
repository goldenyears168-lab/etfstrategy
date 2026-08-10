#!/usr/bin/env python3
"""聯電(2303) 分點訊號 → T0 夜盤做多 的系統性訊號掃描（research only）.

問題：18:00 分點資料公告後進場做多 CCF（聯電個股期），到 T+1 09:00 開盤平倉，
      哪些分點訊號對「隔夜跳空」有 edge？

設計要點：
  * 一次定義完整訊號族（21 支分點訊號 + 5 支輔助），全部一起評估，避免逐一挑選的
    多重檢定偏誤；最後套 Benjamini-Hochberg FDR。
  * 兩種標的：
      naked  = 裸多 CCF，報酬 = 完整跳空
      hedged = 多 CCF + 空 TMF（beta 中性），報酬 = 聯電專屬跳空
    hedge beta 只用訓練期估計，不回頭看測試期。
  * 所有訊號在 T 日 18:00 皆為已知（PIT）；分點自身 z-score 一律 shift(1)。
  * 時間切分：訓練 ≤2025-08-31 / 測試 ≥2025-09-01。

  PYTHONPATH=src .venv/bin/python scripts/research/umc_branch_overnight_signal_sweep.py
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
TX_CACHE = Path.home() / "goldenstocks-data/cache/tmf_channel/bars.sqlite"
TX_SOURCE = "tx_1m_tick_built_582d"
SPLIT = "2025-09-01"
MIN_HIST = 30  # 分點自身歷史最少天數，才允許算 z

FOREIGN_KEYS = ("美林", "摩根", "高盛", "瑞銀", "野村", "花旗", "麥格理", "匯豐",
                "大和", "瑞士", "德意志", "星展", "港商", "美商", "法商", "英商",
                "日商", "新加坡", "星洲", "瑞信")


def load_branch() -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    px = pd.read_sql(
        "select trade_date,open,close,volume from stock_daily_bars "
        f"where stock_id='{STOCK}' and trade_date>='2024-06-01' order by trade_date", conn)
    px["r0"] = px.close.pct_change()
    px["r_gap"] = px.open.shift(-1) / px.close - 1
    px["r_oc"] = px.close.shift(-1) / px.open.shift(-1) - 1
    br = pd.read_sql(
        "select trade_date, securities_trader_id tid, securities_trader tname, buy, sell, net "
        f"from stock_broker_branch_daily where stock_id='{STOCK}' and trade_date>='2024-07-01' "
        "order by trade_date", conn)
    aux = {}
    for tbl, cols in [("stock_institutional_daily", "foreign_net,investment_trust_net,dealer_self_net"),
                      ("stock_daytrade_daily", "daytrade_ratio_pct"),
                      ("stock_margin_daily", "margin_change,short_change")]:
        aux[tbl] = pd.read_sql(
            f"select trade_date,{cols} from {tbl} where stock_id='{STOCK}' and trade_date>='2024-06-01'", conn)
    conn.close()

    d = br.merge(px, on="trade_date", how="left")
    d["amt"] = d.net * d.close
    d["gross"] = (d.buy + d.sell) * d.close
    d["turnover"] = d.volume * d.close
    d = d.sort_values(["tid", "trade_date"]).reset_index(drop=True)
    g = d.groupby("tid", sort=False)
    d["pn"] = g.cumcount()
    d["pmu"] = g.amt.apply(lambda s: s.expanding().mean().shift(1)).values
    d["psd"] = g.amt.apply(lambda s: s.expanding().std().shift(1)).values
    d["pgr"] = g.gross.apply(lambda s: s.expanding().mean().shift(1)).values
    d["pmax"] = g.amt.apply(lambda s: s.expanding().max().shift(1)).values
    d["z"] = (d.amt - d.pmu) / d.psd
    d["prev_amt"] = g.amt.shift(1)
    d["is_fgn"] = d.tname.fillna("").str.contains("|".join(FOREIGN_KEYS))
    return d, px, aux


def build_signals(d: pd.DataFrame, px: pd.DataFrame, aux: dict) -> pd.DataFrame:
    """每個交易日一列，全部欄位在該日 18:00 已知。"""
    d = d[d.pn >= MIN_HIST].copy()
    out = {}
    by = d.groupby("trade_date")
    tv = by.turnover.first()

    out["n_z3_1e8"] = d[(d.z >= 3) & (d.amt >= 1e8)].groupby("trade_date").size()
    out["n_2e8"] = d[d.amt >= 2e8].groupby("trade_date").size()
    out["n_5e8"] = d[d.amt >= 5e8].groupby("trade_date").size()
    out["n_sell_z3"] = d[(d.z <= -3) & (d.amt <= -1e8)].groupby("trade_date").size()
    out["top1_z"] = by.z.max()
    out["sum_z_pos"] = d[d.z > 0].groupby("trade_date").z.sum()
    out["sum_z_net"] = by.z.sum()
    out["top1_net_pct"] = by.amt.max() / tv
    out["top5_net_pct"] = d.groupby("trade_date").amt.apply(lambda s: s.nlargest(5).sum()) / tv
    out["top15_net_pct"] = d.groupby("trade_date").amt.apply(lambda s: s.nlargest(15).sum()) / tv
    out["buy_sell_br_ratio"] = (d[d.amt > 0].groupby("trade_date").size()
                                / d[d.amt < 0].groupby("trade_date").size())
    pos = d[d.amt > 0]
    out["hhi_buy"] = pos.groupby("trade_date").amt.apply(lambda s: ((s / s.sum()) ** 2).sum())
    out["net_disp"] = by.amt.std() / tv
    fg = d[d.is_fgn].groupby("trade_date").amt.sum()
    lo = d[~d.is_fgn].groupby("trade_date").amt.sum()
    out["fgn_br_net_pct"] = fg / tv
    out["loc_br_net_pct"] = lo / tv
    out["fgn_minus_loc"] = (fg - lo) / tv
    out["newmax_n"] = d[(d.amt > d.pmax) & (d.amt >= 5e7)].groupby("trade_date").size()
    out["persist_n"] = d[(d.amt >= 1e8) & (d.prev_amt >= 1e8)].groupby("trade_date").size()
    small = d[(d.pgr < 1e8) & (d.amt >= 3e7)]
    out["small_br_net_pct"] = small.groupby("trade_date").amt.sum() / tv
    big = d[(d.pgr >= 3e8)]
    out["big_br_net_pct"] = big.groupby("trade_date").amt.sum() / tv

    s = pd.DataFrame(out).reindex(sorted(d.trade_date.unique()))
    for c in ["n_z3_1e8", "n_2e8", "n_5e8", "n_sell_z3", "newmax_n", "persist_n"]:
        s[c] = s[c].fillna(0)

    p = px.set_index("trade_date")
    s["daytrade_ratio"] = aux["stock_daytrade_daily"].set_index("trade_date").daytrade_ratio_pct
    inst = aux["stock_institutional_daily"].set_index("trade_date")
    s["inst_fgn_pct"] = inst.foreign_net * p.close / (p.volume * p.close)
    s["inst_trust_pct"] = inst.investment_trust_net * p.close / (p.volume * p.close)
    s["inst_dealer_pct"] = inst.dealer_self_net * p.close / (p.volume * p.close)
    mg = aux["stock_margin_daily"].set_index("trade_date")
    s["margin_chg_pct"] = mg.margin_change / p.volume * 1000
    return s


def main() -> int:
    d, px, aux = load_branch()
    sig = build_signals(d, px, aux)

    c = sqlite3.connect(f"file:{TX_CACHE}?mode=ro", uri=True)
    b = pd.read_sql(f"select day,t,o,c,sess from bars where source='{TX_SOURCE}'", c)
    c.close()
    dy = b[b.sess == "day"]
    tx = pd.DataFrame({"d1344": dy.sort_values("t").groupby("day").c.last(),
                       "d0845": dy.sort_values("t").groupby("day").o.first()}).sort_index()
    tx["tx_ov"] = tx.d0845.shift(-1) / tx.d1344 - 1
    tx["tx_r0"] = tx.d1344.pct_change()

    p = px.set_index("trade_date")
    df = sig.join(p[["r0", "r_gap", "r_oc", "close"]]).join(tx[["tx_ov", "tx_r0"]])
    df = df.dropna(subset=["r_gap", "tx_ov", "r0"])
    tr, te = df.index < SPLIT, df.index >= SPLIT

    # hedge beta 與當日控制係數：只用訓練期
    bh = np.polyfit(df.loc[tr, "tx_ov"], df.loc[tr, "r_gap"], 1)
    df["naked"] = df.r_gap
    df["hedged"] = df.r_gap - bh[0] * df.tx_ov          # beta 中性後的聯電專屬跳空
    b0 = np.polyfit(df.loc[tr, "r0"], df.loc[tr, "hedged"], 1)
    df["hedged_c"] = df.hedged - (b0[1] + b0[0] * df.r0)  # 再扣掉「今天自己就在噴」

    names = [c for c in sig.columns]
    print(f"樣本 {len(df)} 天  {df.index.min()} ~ {df.index.max()}"
          f"   訓練 {tr.sum()} / 測試 {te.sum()}   切點 {SPLIT}")
    print(f"hedge beta (CCF vs TX) = {bh[0]:.2f}   訓練期估計")
    for tgt in ["naked", "hedged", "hedged_c"]:
        v = df[tgt]
        print(f"  基準 {tgt:9s}: 全體均 {v.mean()*100:+.2f}%  訓練 {v[tr].mean()*100:+.2f}%  測試 {v[te].mean()*100:+.2f}%")

    rows = []
    for n in names:
        x = df[n]
        if x.notna().sum() < 200 or x.nunique() < 5:
            continue
        for tgt in ["hedged_c", "naked"]:
            y = df[tgt]
            m = x.notna() & y.notna()
            ic_tr = x[m & tr].corr(y[m & tr], method="spearman")
            ic_te = x[m & te].corr(y[m & te], method="spearman")
            # 訓練期決定方向，測試期只驗證
            side = 1 if (ic_tr or 0) >= 0 else -1
            thr = (x[m & tr] * side).quantile(0.8)
            hit = m & te & ((x * side) >= thr)
            rest = m & te & ((x * side) < thr)
            if hit.sum() < 15:
                continue
            diff = y[hit].mean() - y[rest].mean()
            sp = np.sqrt(y[hit].var() / hit.sum() + y[rest].var() / rest.sum())
            rows.append(dict(signal=n, target=tgt, dir="+" if side > 0 else "-",
                             ic_tr=ic_tr, ic_te=ic_te, n_hit=int(hit.sum()),
                             hit_mean=y[hit].mean() * 100, rest_mean=y[rest].mean() * 100,
                             excess=diff * 100, t=diff / sp,
                             winrate=(y[hit] > 0).mean() * 100))
    r = pd.DataFrame(rows)
    from scipy import stats as st
    r["p"] = 2 * (1 - st.norm.cdf(r.t.abs()))
    r = r.sort_values("t", ascending=False).reset_index(drop=True)
    # Benjamini-Hochberg FDR
    m = len(r)
    ro = r.sort_values("p").reset_index()
    ro["bh"] = ro.p * m / (ro.index + 1)
    ro["bh"] = ro.bh[::-1].cummin()[::-1].clip(upper=1)
    r = r.merge(ro[["index", "bh"]].rename(columns={"index": "lvl"}), left_index=True, right_on="lvl").drop(columns="lvl")

    pd.set_option("display.width", 220)
    for tgt in ["hedged_c", "naked"]:
        s = r[r.target == tgt].copy()
        for cc in ["ic_tr", "ic_te", "hit_mean", "rest_mean", "excess", "t", "winrate", "p", "bh"]:
            s[cc] = s[cc].round(3)
        print(f"\n=== 標的 {tgt}（共測 {len(s)} 支訊號；測試期 {te.sum()} 天，前 20% 進場）===")
        print(s[["signal", "dir", "ic_tr", "ic_te", "n_hit", "hit_mean", "rest_mean",
                 "excess", "t", "winrate", "p", "bh"]].to_string(index=False))
        sur = s[(s.bh < 0.10) & (s.t > 0)]
        print(f"  → BH-FDR<0.10 且方向正確者：{len(sur)} 支"
              + ("　" + ", ".join(sur.signal) if len(sur) else "（無）"))
    # ---- 第二階段：r0 五分位內中性化後重掃 ----
    # 第一階段的 hedged_c 只做了「線性」扣除當日漲幅，係數由訓練期估。實測發現
    # 「當日漲幅→隔夜跳空」的關係在訓練期是平的、測試期才出現且高度非線性，
    # 線性控制擋不住 → 任何與當日漲幅相關的訊號都會假陽性。這裡改用分組中性化。
    df["b"] = pd.qcut(df.r0, 5, labels=False)
    df["y_n"] = df.hedged - df.groupby("b").hedged.transform("mean")
    print("\n=== 當日漲幅 → 隔夜跳空(hedged) 的關係是否穩定 ===")
    for lab, mask in [("訓練", tr), ("測試", te)]:
        s = df[mask].copy()
        s["bb"] = pd.cut(s.r0, [-1, -.02, 0, .02, 1], labels=["跌>2%", "跌0~2%", "漲0~2%", "漲>2%"])
        g = s.groupby("bb", observed=True).hedged.agg(["size", "mean"])
        print("  " + lab + ": " + " | ".join(
            "{} n={} {:+.2f}%".format(i, int(v["size"]), v["mean"] * 100) for i, v in g.iterrows()))

    rows2 = []
    for n in names:
        x = df[n]
        if x.notna().sum() < 200 or x.nunique() < 5:
            continue
        ic = x[tr].corr(df.y_n[tr], method="spearman")
        side = 1 if (ic or 0) >= 0 else -1
        thr = (x[tr] * side).quantile(0.8)
        hit = te & x.notna() & ((x * side) >= thr)
        rest = te & x.notna() & ((x * side) < thr)
        if hit.sum() < 15 or rest.sum() < 15:
            continue
        diff = df.y_n[hit].mean() - df.y_n[rest].mean()
        sp = np.sqrt(df.y_n[hit].var() / hit.sum() + df.y_n[rest].var() / rest.sum())
        rows2.append(dict(signal=n, dir="+" if side > 0 else "-", ic_tr=round(ic, 3),
                          ic_te=round(x[te].corr(df.y_n[te], method="spearman"), 3),
                          n=int(hit.sum()), excess=round(diff * 100, 2), t=round(diff / sp, 2),
                          winrate=round((df.y_n[hit] > 0).mean() * 100, 0)))
    r2 = pd.DataFrame(rows2).sort_values("t", ascending=False).reset_index(drop=True)
    print(f"\n=== 最嚴格：r0 五分位內中性化 + 訓練期定方向 + 測試期驗證（{len(r2)} 支）===")
    print(r2.to_string(index=False))
    print(f"  t>2 者：{(r2.t > 2).sum()} 支　（純隨機期望 ≈ {len(r2)*0.023:.1f} 支）")

    r.to_csv("/tmp/umc_sweep.csv", index=False)
    r2.to_csv("/tmp/umc_sweep_neutralized.csv", index=False)
    df.to_pickle("/tmp/umc_sig.pkl")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
