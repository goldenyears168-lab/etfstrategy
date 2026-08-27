#!/usr/bin/env python3
"""把「大戶單日大買 ∩ 當日低量」規則套到全分點宇宙 —— 檢驗是市場機制還是 9217 的過擬合.

規則（在 9217 上 in-sample 找到，此處原封不動套用，不調任何參數）：
  訊號 = 該分點單日淨買 >= --min-yi 億  ∩  該股當日成交量 < 20日均量 × --vol-mult
  進場 = T+1 收盤 · 出場 = T0+8 收盤 · 成本 30bps · 報酬做同日橫斷面去均值

機制假說：Barclay & Warner 1993 (JFE 34(3):281-305) 的 stealth trading——
知情交易者刻意把單子藏在不驚動市場的規模裡。若成立，這條規則應該在**多數分點**都有效，
而不只在 9217。若只在 9217 成立，就是過擬合。

⚠️ 這條規則是兩階段 in-sample 搜尋的產物（7 個條件挑 1、8 種進場挑 1，有效組合 ~56），
本腳本是它的第一次外部檢驗。
"""
from __future__ import annotations
import argparse, json, sqlite3, statistics, sys
from collections import defaultdict
from pathlib import Path
import scipy.stats as ss
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
from stock_db import DEFAULT_DB_PATH  # noqa: E402

SOURCE="finmind"; WS,WE="2024-07-01","2026-07-16"; COST=0.30; EXIT_OFF=8
OUT=ROOT/"reports/research/branch-footprint-screen"

def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",type=Path,default=Path(DEFAULT_DB_PATH))
    ap.add_argument("--min-yi",type=float,default=1.0)
    ap.add_argument("--vol-mult",type=float,default=1.5)
    ap.add_argument("--min-n",type=int,default=25)
    a=ap.parse_args()
    c=sqlite3.connect(f"file:{a.db}?mode=ro",uri=True); c.row_factory=sqlite3.Row
    px={}
    for sid,td,cl,v in c.execute(
        "SELECT stock_id,trade_date,close,volume FROM stock_daily_bars WHERE source=? "
        "AND trade_date>='2024-05-01' AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]' "
        "AND stock_id NOT GLOB '00*'",(SOURCE,)):
        if cl: px.setdefault(sid,{})[td]=(float(cl),float(v or 0))
    days=sorted({t for m in px.values() for t in m}); di={t:i for i,t in enumerate(days)}
    # 每個 (股,訊號日)：T+1 收盤進 → T0+8 收盤出，以及當日是否低量
    fwd={}; lowv={}
    for sid,dd in px.items():
        ds=sorted(dd)
        for j in range(19,len(ds)-EXIT_OFF):
            d=ds[j]
            ma=sum(dd[x][1] for x in ds[j-19:j+1])/20
            lowv[(sid,d)] = ma>0 and dd[d][1] < a.vol_mult*ma
            i=di[d]
            if i+EXIT_OFF>=len(days): continue
            e=dd.get(days[i+1]); x=dd.get(days[i+EXIT_OFF])
            if e and x and e[0]>0: fwd[(sid,d)]=(x[0]/e[0]-1)*100-COST
    bym=defaultdict(list)
    for (sid,d),r in fwd.items(): bym[d].append(r)
    md={d:statistics.mean(v) for d,v in bym.items() if len(v)>=30}
    print(f"價格宇宙 {len(px)} 檔 · 可評估 (股,日) {len(fwd):,} 組")

    lo=defaultdict(list); hi=defaultdict(list)
    n_raw=0
    for r in c.execute(
        """SELECT b.securities_trader_id bid, b.stock_id sid, b.trade_date d
           FROM stock_broker_branch_daily b
           JOIN stock_daily_bars p ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
           WHERE b.source=? AND b.trade_date BETWEEN ? AND ? AND (b.buy-b.sell)*p.close>=?
             AND length(b.stock_id)=4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
             AND b.stock_id NOT GLOB '00*'""",(SOURCE,SOURCE,WS,WE,a.min_yi*1e8)):
        n_raw+=1
        k=(r["sid"],r["d"])
        if k not in fwd or r["d"] not in md: continue
        (lo if lowv.get(k) else hi)[r["bid"]].append(fwd[k]-md[r["d"]])
    print(f"事件（單日淨買>={a.min_yi}億）{n_raw:,} 筆 · 低量組 {sum(len(v) for v in lo.values()):,} · 爆量組 {sum(len(v) for v in hi.values()):,}\n")

    rows=[]
    for bid in set(lo)|set(hi):
        L,H=sorted(lo.get(bid,[])),sorted(hi.get(bid,[]))
        if len(L)<a.min_n: continue
        rows.append(dict(bid=bid,nL=len(L),mL=statistics.median(L),wL=sum(1 for x in L if x>0)/len(L),
                         nH=len(H),mH=(statistics.median(H) if len(H)>=10 else None),
                         wH=(sum(1 for x in H if x>0)/len(H) if len(H)>=10 else None),
                         p=ss.wilcoxon(L).pvalue,
                         diff=(statistics.median(L)-statistics.median(H)) if len(H)>=10 else None))
    nm={r[0]:r[1] for r in c.execute("SELECT DISTINCT securities_trader_id,securities_trader FROM stock_broker_branch_daily")}
    print(f"=== 可測分點 {len(rows)} 家（低量組 n>={a.min_n}）· 基準在定義上為 0 ===")
    mL=[r["mL"] for r in rows]; wL=[r["wL"] for r in rows]
    print(f"  低量組 median 的分布：中位 {statistics.median(sorted(mL)):+.3f}% · "
          f"正值 {sum(1 for x in mL if x>0)}/{len(mL)} 家 ({sum(1 for x in mL if x>0)/len(mL):.0%})")
    print(f"  低量組 勝率 的分布：中位 {statistics.median(sorted(wL)):.1%} · "
          f">50% 者 {sum(1 for x in wL if x>0.5)}/{len(wL)} 家")
    dif=[r["diff"] for r in rows if r["diff"] is not None]
    if dif:
        st=ss.wilcoxon(dif)
        print(f"\n  **低量 − 爆量** 的 median 差：中位 {statistics.median(sorted(dif)):+.3f}pp · "
              f"正值 {sum(1 for x in dif if x>0)}/{len(dif)} 家 ({sum(1 for x in dif if x>0)/len(dif):.0%})"
              f" · Wilcoxon p={st.pvalue:.2e}")
    rows.sort(key=lambda r:-(r["diff"] if r["diff"] is not None else -99))
    print(f"\n{'分點':<7}{'名稱':<13}{'低量n':>7}{'低量med%':>10}{'低量勝率':>10}{'爆量n':>7}{'爆量med%':>10}{'差(pp)':>9}{'p':>10}")
    for r in rows[:12]:
        print(f"{r['bid']:<7}{(nm.get(r['bid']) or '')[:11]:<13}{r['nL']:>7}{r['mL']:>10.3f}{r['wL']:>10.1%}"
              f"{r['nH']:>7}{(r['mH'] if r['mH'] is not None else 0):>10.3f}{(r['diff'] or 0):>9.3f}{r['p']:>10.4f}")
    print("  ...")
    for r in rows[-5:]:
        print(f"{r['bid']:<7}{(nm.get(r['bid']) or '')[:11]:<13}{r['nL']:>7}{r['mL']:>10.3f}{r['wL']:>10.1%}"
              f"{r['nH']:>7}{(r['mH'] if r['mH'] is not None else 0):>10.3f}{(r['diff'] or 0):>9.3f}{r['p']:>10.4f}")
    for r in rows:
        if r["bid"] in ("9217","9661","9227","9801"):
            print(f"\n  [對照] {r['bid']} {nm.get(r['bid'],'')}: 低量 n={r['nL']} med={r['mL']:+.3f}% 勝率={r['wL']:.1%}"
                  f" | 爆量 med={(r['mH'] if r['mH'] is not None else float('nan')):+.3f}% | 差={(r['diff'] or 0):+.3f}pp")
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"branch_lowvol_rule_generalization.json").write_text(
        json.dumps(dict(window=[WS,WE],min_yi=a.min_yi,vol_mult=a.vol_mult,rows=rows),
                   ensure_ascii=False,indent=2,default=float),encoding="utf-8")
    print(f"\n→ {OUT/'branch_lowvol_rule_generalization.json'}")
    return 0

if __name__=="__main__": raise SystemExit(main())
