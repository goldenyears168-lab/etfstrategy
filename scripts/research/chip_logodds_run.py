#!/usr/bin/env python3
import sys, time, itertools
sys.argv = ['x']
src = open('/Users/jackm4/goldenstocks/scripts/research/chip_logodds_combine.py').read().split('if __name__')[0]
exec(src)
import numpy as np, pandas as pd

OUT = DIR / "logodds_combine_results.csv"
print(lab.HEADER, flush=True)

# ---------------- 0. 診斷：logodds vs linear 的實質差異 ----------------
def diag(names, mode="icpos", clip=CLIP):
    a = combine(names, "lin", mode)
    b = combine(names, "logodds", mode, clip)
    x = d.assign(_a=a, _b=b).dropna(subset=["_a", "_b"])
    rho, ov = [], []
    for t, g in x.groupby("trade_date"):
        if len(g) < 120: continue
        rho.append(g._a.rank().corr(g._b.rank(), method="pearson"))
        n = max(3, int(round(len(g) * lab.FR)))
        A = set(g.nlargest(n, "_a").stock_id); B = set(g.nlargest(n, "_b").stock_id)
        ov.append(len(A & B) / n)
    return float(np.mean(rho)), float(np.mean(ov))

DIAG = {}
for nm in (["A2","B","C","E","F"], ["A3","B","C","E","F"], ["A3","E"], ["A3","B"],
           ["A3","B","E"], ["A2","C"], ["A3","B","E","F"]):
    r, o = diag(nm)
    DIAG["+".join(nm)] = (r, o)
    print(f"[diag] {'+'.join(nm):<24} 逐日 rank corr(lin, logodds)={r:.4f}  前5.8%名單重疊={o:.3f}", flush=True)

# ---------------- 1. 單區塊基準 ----------------
print("\n== 1. 單區塊基準（lin 與 logodds 對單一區塊完全同序，故只跑一次）==", flush=True)
for n in ["A", "A2", "A3", "B", "B2", "C", "C2", "D", "E", "F"]:
    run(f"1blk {n} [icpos]", combine([n], "logodds"))
# 證明單區塊同序
_a = combine(["A2"], "lin"); _b = combine(["A2"], "logodds")
print("  單區塊 lin/logodds 同序檢查 spearman =",
      round(pd.Series(_a).corr(pd.Series(_b), method="spearman"), 6), flush=True)

# ---------------- 2. 主家族：5 區塊 A2/B/C/E/F ----------------
BASE1 = ["A2", "B", "C", "E", "F"]
BASE3 = ["A3", "B", "C", "E", "F"]
for fam, base in (("F1", BASE1), ("F3", BASE3)):
    print(f"\n== 2.{fam} 區塊={base} 全子集（k=2..5）× {{lin, logodds}} ==", flush=True)
    for k in (2, 3, 4, 5):
        for sub in itertools.combinations(base, k):
            for how in ("lin", "logodds"):
                run(f"{fam} k={k} {'+'.join(sub)} [{how}]", combine(list(sub), how))

# ---------------- 3. 含 fee 的 A ----------------
print("\n== 3. 以 A（含 fee）取代 A2 ==", flush=True)
for sub in (("A","B"), ("A","C"), ("A","E"), ("A","F"), ("A","B","E"), ("A","B","C","E","F")):
    for how in ("lin", "logodds"):
        run(f"Afee {'+'.join(sub)} [{how}]", combine(list(sub), how))

# ---------------- 4. 群內 EW（對照 icpos） ----------------
print("\n== 4. 群內權重改 EW ==", flush=True)
for sub in (BASE1, BASE3, ["A3","B"], ["A3","E"], ["A3","B","E"]):
    for how in ("lin", "logodds"):
        run(f"EWmode {'+'.join(sub)} [{how}]", combine(sub, how, mode="ew"))

# ---------------- 5. 加權 log-odds（區塊層 walk-forward ICpos 權重） ----------------
print("\n== 5. 加權版 Σ w_i·logit_i（區塊層 WF ICpos 權重）==", flush=True)
ALLB = ["A", "A2", "A3", "B", "B2", "C", "C2", "D", "E", "F"]
BIC = block_ic(ALLB)
BW = wf_weights(BIC, "icpos")
print("  區塊 IC 均值:", BIC.mean().round(4).to_dict(), flush=True)
print("  OOS 平均權重(5區塊 F1):",
      (BW[BASE1].div(BW[BASE1].sum(axis=1), axis=0)).iloc[FORM:].mean().round(3).to_dict(), flush=True)
for sub in (BASE1, BASE3, ["A3","B","E"], ["A3","E"], ["A3","B"], ["A2","B","E","F"], ["A3","B","E","F"]):
    W = BW[sub].div(BW[sub].sum(axis=1).replace(0, np.nan), axis=0).fillna(1.0/len(sub))
    for how in ("lin", "logodds"):
        run(f"Wblk {'+'.join(sub)} [{how}·ICpos權重]", combine(sub, how, wblock=W))

# ---------------- 6. 非獨立分群（結構階段判定被否決）對照 ----------------
print("\n== 6. 對照：把結構階段判定「非獨立」的群也拆開來相乘 ==", flush=True)
BLOCKS["G3"] = ["d_sbl", "d_util"]
BLOCKS["G4"] = ["d_retail", "d_holders"]
BLOCKS["G5"] = ["for_1", "for_5", "for_20"]
BLOCKS["G7"] = ["br_diff", "br_main", "br_main5"]
SIX = ["A2", "B", "C2", "D", "E", "F"]
EIGHT = ["A2", "B", "G3", "G4", "G5", "G7", "E", "F"]
for tag, sub in (("6區塊(C拆D·corr0.20)", SIX), ("8區塊(G3~G7·corr0.19~0.33)", EIGHT)):
    for how in ("lin", "logodds"):
        run(f"{tag} [{how}]", combine(sub, how))

# ---------------- 7. clip 敏感度（極端放大程度） ----------------
print("\n== 7. clip 敏感度：clip 越小 → logit 尾端越爆，極端值放大越強 ==", flush=True)
for sub in (BASE1, BASE3, ["A3","B","E"]):
    for cl in (0.001, 0.01, 0.02, 0.05, 0.15, 0.30):
        run(f"clip={cl:<5} {'+'.join(sub)} [logodds]", combine(sub, "logodds", clip=cl))

# ---------------- 8. 冪次族：直接測「放大極端值」是不是有效成分 ----------------
print("\n== 8. 冪次族 z=sign(s)|s|^γ（γ<1 壓縮尾端、γ=1 線性、γ>1 放大尾端）==", flush=True)
def combine_pow(names, gamma, mode="icpos"):
    S = pd.DataFrame({n: BS(n, mode) for n in names})
    Z = np.sign(S) * S.abs() ** gamma
    tot = Z.sum(axis=1, min_count=1)
    den = Z.notna().sum(axis=1)
    return tot * np.where(den > 0, len(names) / den.replace(0, np.nan), np.nan)
for sub in (BASE1, BASE3, ["A3","B","E"]):
    for gm in (0.33, 0.5, 1.0, 2.0, 3.0, 5.0):
        run(f"gamma={gm:<4} {'+'.join(sub)} [pow]", combine_pow(sub, gm))

# ---------------- 9. 安慰劑與覆蓋率對照 ----------------
print("\n== 9. 安慰劑 / 覆蓋率對照 ==", flush=True)
MEM = sorted(set(sum([BLOCKS[b] for b in BASE1], [])))
kfac = FDF[MEM].notna().sum(axis=1).astype(float)
run("placebo: 可得因子數 k_i", kfac)
run("placebo: -k_i", -kfac)
full = kfac == len(MEM)
print(f"  完整覆蓋比例 = {full.mean():.3f}", flush=True)
dsub = d[full].copy()
print(f"  子宇宙 {len(dsub):,} 列 · {dsub.trade_date.nunique()} 日 · 每日均 {len(dsub)/dsub.trade_date.nunique():.0f} 檔", flush=True)

def run_sub(tag, score, dd, note=""):
    r = lab.evaluate(dd, score.reindex(dd.index))
    if "error" in r:
        print(f"  {tag:<52}{r['error']}", flush=True); return
    RES.append(dict(tag=tag, note=note, **{k: r[k] for k in
        ("n_days","turnover","long_gross","long_t","long_net_ann","spread_gross","spread_t","breakeven_cost")}))
    print(f"  {tag:<52}{r['turnover']*100:>6.1f}%{r['long_gross']:>+9.4f}%{r['long_t']:>+7.2f}"
          f"{r['long_net_ann']:>+9.2f}%{r['spread_gross']:>+9.4f}%{r['spread_t']:>+7.2f}{r['n_days']:>6}", flush=True)

for sub in (BASE1, BASE3, ["A3","B","E"], ["A3"], ["A2"]):
    for how in ("lin", "logodds"):
        if len(sub) == 1 and how == "lin": continue
        run_sub(f"[完整覆蓋子宇宙·沿用全宇宙oc_n] {'+'.join(sub)} [{how}]", combine(sub, how), dsub)
dsub2 = dsub.drop(columns=["oc_n"]).copy()
dsub2["oc_n"] = lab._neutral(dsub2)
for sub in (BASE1, BASE3, ["A3"]):
    how = "logodds"
    run_sub(f"[完整覆蓋子宇宙·重算oc_n] {'+'.join(sub)} [{how}]", combine(sub, how), dsub2)

pd.DataFrame(RES).to_csv(OUT, index=False)
print(f"\n寫出 {OUT}  共 {len(RES)} 組態  總耗時 {time.time()-t0:.0f}s", flush=True)
r = pd.DataFrame(RES).sort_values("long_net_ann", ascending=False)
print("\n== 淨值排序前 20 ==", flush=True)
print(r.head(20)[["tag","turnover","long_gross","long_t","long_net_ann","spread_gross","spread_t"]].to_string(index=False), flush=True)
