#!/usr/bin/env python3
"""借券費率／utilization／DTC／CDM DOUT 的月頻橫斷面檢定（Jegadeesh-Titman 重疊組合）。

對應 `config/research.yaml` 的 topic ``sbl-fee-cross-section``（G1 已預先登錄）。

**為什麼是月頻**：同一批訊號在日頻單股尺度已被證死（見 topic
``chip-signal-daily-horizon``，5 個檢定全 rejected，最佳毛利 0.096%/日 vs 成本
1.17%/日）。文獻的效果量是在**月頻橫斷面多空對沖**上量出來的——
Cohen-Diether-Malloy 的 DOUT 次月 −2.54%（t=−3.32）、
Engelberg et al. (2025 Mgmt Sci) 借券費率月多空 4.01%。本檢定測台股。

**JT 重疊組合**：持有期 K 個月時，每個月同時持有 K 個在過去 K 個月各自成形的
組合，當月報酬取其平均。這樣處理重疊持有期的序列相關，比對單一序列做
Newey-West 更貼近 Jegadeesh-Titman (1993) 原始做法。

**前處理**（Chen-Da-Huang 2022 JFE 配方）：訊號 winsorize 1%/99%、剔除低價股與
市值最小 10%、每月最少可交易標的數門檻。所有計算只用 t 月及以前的資料。

**成本**：賣出證交稅 0.3% ＋ 手續費 0.1425%×2。月頻換倉的實際換手率由組合
成分變動估算，不假設 100%。

⚠️ **覆蓋範圍**：借券賣出餘額來自 TWSE TWT93U，**只含上市股**。上櫃借券資料
（`tpex.org.tw/www/zh-tw/margin/sbl`）尚未回補，故本檢定的宇宙是上市股。
實測 t13sa710 有 18.8% 的借券標的是上櫃股，結論不可外推到上櫃。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

COST_ONE_WAY = 0.001425                 # 手續費
COST_SELL_TAX = 0.003                   # 賣出證交稅
SPLITS = [
    ("full", "2011-01", "2026-08"),
    ("pre_quota", "2011-01", "2011-11"),          # 借券量能管制前
    ("quota_era", "2011-12", "2013-09"),          # 管制期
    ("post_uptick_repeal", "2013-10", "2026-08"),  # 廢除平盤下不得放空後
]


def build_monthly(daily: pd.DataFrame, min_price: float = 10.0,
                  mcap_drop_pct: float = 0.10) -> pd.DataFrame:
    """日面板 → 月頻。訊號取月底值，報酬用當月日報酬複利（已含息還原）。"""
    d = daily.copy()
    d["ym"] = d.trade_date.str[:7]
    d = d.sort_values(["stock_id", "trade_date"])

    # 當月報酬 = 當月日報酬複利（ret 已在 panel 做過除權息還原）
    d["lr"] = np.log1p(d.ret.clip(-0.5, 0.5))
    g = d.groupby(["stock_id", "ym"], group_keys=False)
    m = g.agg(
        close=("close", "last"),
        mcap=("mcap", "last"),
        ret_m=("lr", lambda s: np.expm1(s.sum())),
        n_days=("ret", "size"),
        fee=("fee_rate_vw", "mean"),          # 當月量加權費率的簡單月均
        fee_vol=("fee_volume", "sum"),
        sbl=("sbl_balance", "last"),
        sbl_pct=("sbl_pct", "last"),
        util=("util", "last"),
        dtc=("dtc", "last"),
        vol20=("vol20", "last"),
    ).reset_index()

    m = m[m.n_days >= 10]
    m = m.sort_values(["stock_id", "ym"])
    gm = m.groupby("stock_id", group_keys=False)
    m["d_fee"] = gm.fee.diff()
    m["d_sbl"] = gm.sbl.diff()
    m["dout"] = ((m.d_fee > 0) & (m.d_sbl > 0)).astype(float)
    m["fwd_ret"] = gm.ret_m.shift(-1)          # 次月報酬（t 月訊號 → t+1 月報酬）

    m = m[(m.close >= min_price) & m.mcap.notna() & m.fwd_ret.notna()]
    # 每月剔除市值最小 mcap_drop_pct
    m["mcap_rank"] = m.groupby("ym").mcap.rank(pct=True)
    m = m[m.mcap_rank > mcap_drop_pct]
    return m


def winsorize(s: pd.Series, lo: float = 0.01, hi: float = 0.99) -> pd.Series:
    a, b = s.quantile(lo), s.quantile(hi)
    return s.clip(a, b)


def formation_returns(m: pd.DataFrame, sig: str, n_dec: int = 10,
                      weight: str = "equal", min_names: int = 50
                      ) -> pd.DataFrame:
    """每個成形月 → 該月十分位組合在**次月**的報酬（多 = 低訊號、空 = 高訊號）。"""
    out = []
    for ym, g in m.dropna(subset=[sig]).groupby("ym"):
        if len(g) < min_names:
            continue
        g = g.copy()
        g["_s"] = winsorize(g[sig])
        q = pd.qcut(g._s.rank(method="first"), n_dec, labels=False, duplicates="drop")
        if q.max() != n_dec - 1:
            continue
        g["_d"] = q
        w = g.mcap if weight == "cap" else pd.Series(1.0, index=g.index)
        lo = g[g._d == 0]
        hi = g[g._d == n_dec - 1]
        wl, wh = w[lo.index], w[hi.index]
        out.append({
            "form_ym": ym,
            "long": np.average(lo.fwd_ret, weights=wl),
            "short": np.average(hi.fwd_ret, weights=wh),
            "n": len(g),
            "long_names": frozenset(lo.stock_id),
            "short_names": frozenset(hi.stock_id),
        })
    r = pd.DataFrame(out)
    if not r.empty:
        r["ls"] = r.long - r.short
    return r


def jt_overlap(form: pd.DataFrame, k: int) -> pd.Series:
    """JT 重疊組合：持有 k 個月，每月報酬 = 過去 k 個成形月組合的平均。"""
    f = form.sort_values("form_ym").reset_index(drop=True)
    vals, idx = [], []
    for i in range(len(f)):
        lo = max(0, i - k + 1)
        vals.append(f.ls.iloc[lo:i + 1].mean())
        idx.append(f.form_ym.iloc[i])
    return pd.Series(vals, index=idx)


def turnover(form: pd.DataFrame, k: int) -> float:
    """估算月換手率：相鄰成形月的成分重疊度。持有 k 月則每月換 1/k。"""
    f = form.sort_values("form_ym").reset_index(drop=True)
    ts = []
    for i in range(1, len(f)):
        for col in ("long_names", "short_names"):
            a, b = f[col].iloc[i - 1], f[col].iloc[i]
            if a and b:
                ts.append(1 - len(a & b) / len(a | b) * len(a | b) / max(len(a), len(b)))
    base = np.mean(ts) if ts else 1.0
    return float(min(1.0, base) / k)


def tstat(s: pd.Series) -> float:
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))


def run(m: pd.DataFrame, signals: list[tuple[str, str, int]], ks: list[int],
        weights: list[str], split: tuple[str, str, str]) -> pd.DataFrame:
    name, lo, hi = split
    sub = m[(m.ym >= lo) & (m.ym <= hi)]
    rows = []
    for label, sig, direction in signals:
        for weight in weights:
            f = formation_returns(sub, sig, weight=weight)
            if f.empty or len(f) < 12:
                continue
            f = f.copy()
            if direction < 0:                    # 高訊號 → 預期低報酬，多空反向
                f["ls"] = -f.ls
            tover = turnover(f, 1)
            for k in ks:
                s = jt_overlap(f, k).dropna()
                if len(s) < 12:
                    continue
                cost = (COST_ONE_WAY * 2 + COST_SELL_TAX) * 2 * (tover / k)
                rows.append({
                    "期間": name, "訊號": label, "加權": weight, "K": k,
                    "月多空%": round(s.mean() * 100, 3),
                    "t": round(tstat(s), 2),
                    "年化%": round(s.mean() * 12 * 100, 1),
                    "月換手": round(tover / k, 3),
                    "月成本%": round(cost * 100, 3),
                    "淨月%": round((s.mean() - cost) * 100, 3),
                    "n月": len(s),
                })
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--panel", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    daily = pd.read_pickle(args.panel)
    print(f"日面板 {len(daily):,} 列 · {daily.trade_date.min()}~{daily.trade_date.max()}")
    m = build_monthly(daily)
    print(f"月面板 {len(m):,} 個 stock-month · {m.stock_id.nunique():,} 檔 · "
          f"{m.ym.nunique()} 個月 · {m.ym.min()}~{m.ym.max()}")
    print(f"每月標的數 中位 {m.groupby('ym').size().median():.0f} "
          f"（{m.groupby('ym').size().min()}~{m.groupby('ym').size().max()}）\n")

    signals = [
        ("借券費率（水位）", "fee", -1),
        ("utilization", "util", -1),
        ("days-to-cover", "dtc", -1),
        ("借券賣出餘額佔股本", "sbl_pct", -1),
        ("CDM DOUT", "dout", -1),
    ]
    res = pd.concat([run(m, signals, [1, 3, 6], ["equal", "cap"], sp) for sp in SPLITS],
                    ignore_index=True)
    pd.set_option("display.width", 220)
    print("=== 全期（2011-01~2026-08）===")
    print(res[res.期間 == "full"].to_string(index=False))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        res.to_csv(args.out, index=False)
        print(f"\n完整結果 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
