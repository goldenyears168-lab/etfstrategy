#!/usr/bin/env python3
"""階段三：用 log-odds 相加（＝獨立事件相乘）把群分數結合成最終分數。

固定：lab.evaluate 凍結評估協定；basis 一律 'xs'（單一 basis 鐵律）。
"""
from __future__ import annotations
import sys, itertools, json, time
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, '/Users/jackm4/goldenstocks/scripts/research')
from importlib.machinery import SourceFileLoader
lab = SourceFileLoader('lab', '/Users/jackm4/goldenstocks/scripts/research/chip_lab.py').load_module()

DIR = lab.DIR
OCN = DIR / "combine_oc_n.pkl"

t0 = time.time()
d = lab.load()
print(f"面板 {len(d):,} · {d.trade_date.nunique()} 日 · {d.trade_date.min()}~{d.trade_date.max()}", flush=True)

# ---------- 中性化殘差（全面板算一次，所有組態共用） ----------
if OCN.exists():
    ocn = pd.read_pickle(OCN)
    assert len(ocn) == len(d)
else:
    ocn = lab._neutral(d)
    ocn.to_pickle(OCN)
d["oc_n"] = ocn
print(f"oc_n ready  {time.time()-t0:.0f}s  覆蓋 {ocn.notna().mean():.3f}", flush=True)

# ---------- signed 因子（xs） ----------
F = {k: lab.signed(d, k, 'xs') for k in lab.FACTORS}
FDF = pd.DataFrame(F)
print("因子覆蓋率:", {k: round(v.notna().mean(), 3) for k, v in F.items()}, flush=True)

dates = np.sort(d.trade_date.unique())
didx = d.trade_date.values

# ---------- 每日 IC（spearman，pairwise complete） ----------
IC_PKL = DIR / "combine_ic_daily.pkl"
if IC_PKL.exists():
    IC = pd.read_pickle(IC_PKL)
else:
    rows = {}
    y = d.oc_n.values
    for t, g in d.groupby("trade_date", sort=True).groups.items():
        pos = np.asarray(g)
        yy = y[pos]
        r = {}
        for k in lab.FACTORS:
            xx = FDF[k].values[pos]
            m = np.isfinite(xx) & np.isfinite(yy)
            if m.sum() < 60:
                r[k] = np.nan; continue
            a = pd.Series(xx[m]).rank().values
            b = pd.Series(yy[m]).rank().values
            r[k] = np.corrcoef(a, b)[0, 1]
        rows[t] = r
    IC = pd.DataFrame(rows).T.sort_index()
    IC.to_pickle(IC_PKL)
print(f"IC ready {time.time()-t0:.0f}s", flush=True)
print("全樣本 IC 均值:", IC.mean().round(4).to_dict(), flush=True)

FORM, MINP, LAG = 250, 120, 2


def wf_weights(ic: pd.DataFrame, mode: str = "icpos") -> pd.DataFrame:
    """walk-forward 權重：rolling250(min120) → shift(LAG) → 取正 → 正規化。暖機等權。"""
    r = ic.rolling(FORM, min_periods=MINP).mean().shift(LAG)
    if mode == "icpos":
        w = r.clip(lower=0)
    elif mode == "ew":
        w = pd.DataFrame(1.0, index=ic.index, columns=ic.columns)
    else:
        raise ValueError(mode)
    s = w.sum(axis=1)
    w = w.div(s.replace(0, np.nan), axis=0)
    w = w.fillna(pd.DataFrame(1.0 / ic.shape[1], index=ic.index, columns=ic.columns))
    return w


# ---------- 區塊定義（結構階段建議 5 區塊；已剔除 big/inst3_1/inst3_5/br_net） ----------
BLOCKS = {
    "A": ["sbl_pct", "fee", "retail", "br_conc"],
    "A2": ["sbl_pct", "retail", "br_conc"],          # 踢掉 fee（覆蓋 50%、行為像流量）
    "A3": ["sbl_pct"],                                # 組內階段唯一淨值為正的積木
    "B": ["sbl_util", "sbl_volr"],
    "B2": ["sbl_util"],                               # 100% 覆蓋成員
    "C": ["d_sbl", "d_util", "for_1", "for_5", "for_20", "br_diff", "br_main", "br_main5",
          "d_retail", "d_holders"],
    "C2": ["d_sbl", "d_util", "for_1", "for_5", "for_20", "br_diff", "br_main", "br_main5"],
    "D": ["d_retail", "d_holders"],                   # 6 群方案（結構階段判定與 C 相關 0.202，非獨立）
    "E": ["itc_1", "itc_5"],
    "F": ["dlr_1"],
}


def block_score(members, mode="icpos"):
    """群內加權平均（mask 重正規化）→ 當日重排名 → [-1,+1]。"""
    ic = IC[members]
    W = wf_weights(ic, mode)
    Wd = W.reindex(didx).values                       # (n_rows, n_members)
    X = FDF[members].values
    ok = np.isfinite(X)
    Xf = np.where(ok, X, 0.0)
    num = (Xf * np.where(ok, Wd, 0.0)).sum(axis=1)
    den = np.where(ok, Wd, 0.0).sum(axis=1)
    raw = np.where(den > 1e-12, num / np.maximum(den, 1e-12), np.nan)
    raw = np.where(ok.any(axis=1), raw, np.nan)
    s = pd.Series(raw, index=d.index)
    return (d.assign(_v=s).groupby("trade_date")._v.rank(pct=True) - 0.5) * 2


CACHE = {}
def BS(name, mode="icpos"):
    k = (name, mode)
    if k not in CACHE:
        CACHE[k] = block_score(BLOCKS[name], mode)
    return CACHE[k]


# ---------- 結合器 ----------
CLIP = 0.02
def logit(s, clip=CLIP):
    p = ((s + 1) / 2).clip(clip, 1 - clip)
    return np.log(p / (1 - p))


def combine(names, how="logodds", mode="icpos", clip=CLIP, wblock=None, knorm=True):
    S = pd.DataFrame({n: BS(n, mode) for n in names})
    if how == "logodds":
        Z = S.apply(lambda c: logit(c, clip))
    elif how == "lin":
        Z = S
    else:
        raise ValueError(how)
    if wblock is not None:
        Z = Z * wblock[names].reindex(didx).values
        wsum = pd.DataFrame(np.abs(wblock[names].reindex(didx).values), index=d.index,
                            columns=names).where(Z.notna())
    else:
        wsum = Z.notna().astype(float)
    tot = Z.sum(axis=1, min_count=1)
    if knorm:
        den = wsum.sum(axis=1)
        full = float(len(names)) if wblock is None else np.abs(wblock[names]).sum(axis=1).reindex(didx).values
        tot = tot * np.where(den > 1e-12, full / np.maximum(den, 1e-12), np.nan)
    return tot


# ---------- 區塊層 walk-forward 權重（用區塊複合分數自己的 IC） ----------
def block_ic(names, mode="icpos"):
    out = {}
    y = d.oc_n.values
    for n in names:
        s = BS(n, mode).values
        r = {}
        for t, g in d.groupby("trade_date", sort=True).groups.items():
            pos = np.asarray(g); xx = s[pos]; yy = y[pos]
            m = np.isfinite(xx) & np.isfinite(yy)
            r[t] = np.corrcoef(pd.Series(xx[m]).rank(), pd.Series(yy[m]).rank())[0, 1] if m.sum() >= 60 else np.nan
        out[n] = pd.Series(r)
    return pd.DataFrame(out).sort_index()


RES = []
def run(tag, score, note=""):
    r = lab.evaluate(d, score)
    if "error" in r:
        print(f"  {tag:<52}{r['error']}", flush=True)
        return
    RES.append(dict(tag=tag, note=note, **{k: r[k] for k in
        ("n_days", "turnover", "long_gross", "long_t", "long_net_ann", "spread_gross", "spread_t", "breakeven_cost")}))
    print(f"  {tag:<52}{r['turnover']*100:>6.1f}%{r['long_gross']:>+9.4f}%{r['long_t']:>+7.2f}"
          f"{r['long_net_ann']:>+9.2f}%{r['spread_gross']:>+9.4f}%{r['spread_t']:>+7.2f}{r['n_days']:>6}", flush=True)
    return r


if __name__ == "__main__":
    pass
