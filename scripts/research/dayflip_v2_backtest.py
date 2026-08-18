#!/usr/bin/env python3
"""dayflip v2 回測 —— 「相對延伸度 × 隔日沖席位淨買」放空規則的網格與 walk-forward.

⚠️ 這是 in-sample 網格。任何單一格子的漂亮數字都不構成證據；本腳本刻意輸出**全部**格子
（含難看的），並附 walk-forward 與隨機日 null 對照。

窗口硬約束：全市場分點 tape 只完整到 2026-07-16（之後塌縮成 ~9 席，見 job_registry
ops-console-evening-sync 條目），所以回測止於該日。

交易協議（比照 v1 的日內結構，但用現股日線近似）：
  T0 收盤產生訊號 → T+1 開盤放空 → T+1 收盤回補（日內，不留倉）
  成本預設 5bps（個股期貨口徑；現股放空另有借券成本，見 --cost）
  報酬 = −(close/open − 1)  ← 放空
"""
from __future__ import annotations
import argparse, json, statistics, sys, bisect
from collections import defaultdict
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
import sqlite3
from stock_db import DEFAULT_DB_PATH  # noqa: E402

SOURCE="finmind"
SPEC=ROOT/"reports/research/branch-footprint-screen/dayflip_gapup_short/FROZEN_SPEC_V1.json"
FUT=ROOT/"reports/research/branch-footprint-screen/dayflip_gapup_short/stock_futures_universe.json"
OUT=ROOT/"reports/research/branch-footprint-screen/dayflip_v2_shadow"
WIN_END="2026-07-16"; WIN_START="2024-07-01"
ADV_MIN=3e8; SEAT_WIN=5; ACC_WIN=60; ACC_NR=0.30; ACC_BUY=1e8

def ro(db): 
    c=sqlite3.connect(f"file:{db}?mode=ro",uri=True); c.row_factory=sqlite3.Row; return c

def main()->int:
    ap=argparse.ArgumentParser()
    ap.add_argument("--db",type=Path,default=Path(DEFAULT_DB_PATH))
    ap.add_argument("--cost",type=float,default=0.05,help="來回成本 %%（預設 5bps 期貨口徑）")
    ap.add_argument("--no-accum-filter",action="store_true")
    a=ap.parse_args()
    c=ro(a.db)
    seats=[k for k,v in json.loads(SPEC.read_text())["seat_flip_table_frozen"]["values"].items() if float(v)>=0.40]
    fut=set(json.loads(FUT.read_text())["map"].keys())
    days=[r[0] for r in c.execute("select distinct trade_date from stock_daily_bars where source=? and trade_date between ? and ? order by 1",(SOURCE,WIN_START,WIN_END))]
    di={d:i for i,d in enumerate(days)}
    print(f"窗口 {days[0]} ~ {days[-1]}（{len(days)} 交易日）· 高沖席 {len(seats)} · 期貨宇宙 {len(fut)}")

    ph=",".join("?"*len(fut))
    bars=defaultdict(dict)
    for r in c.execute(f"select stock_id,trade_date,open,close,volume from stock_daily_bars where source=? and trade_date between ? and ? and stock_id in ({ph})",(SOURCE,WIN_START,WIN_END,*fut)):
        if r["close"] and r["open"]: bars[r["stock_id"]][r["trade_date"]]={"o":float(r["open"]),"c":float(r["close"]),"v":float(r["volume"] or 0)}
    print(f"價格載入 {len(bars)} 檔")

    phs=",".join("?"*len(seats))
    tape=defaultdict(lambda: defaultdict(lambda:[0.0,0.0]))   # (seat,stock)->date->[buy,sell]
    n=0
    for r in c.execute(f"""select b.securities_trader_id sid,b.stock_id sk,b.trade_date d,
        sum(b.buy*p.close) bn, sum(b.sell*p.close) sn
        from stock_broker_branch_daily b join stock_daily_bars p on p.stock_id=b.stock_id and p.trade_date=b.trade_date and p.source=?
        where b.source=? and b.securities_trader_id in ({phs}) and b.trade_date between ? and ? and b.stock_id in ({ph})
        group by 1,2,3""",(SOURCE,SOURCE,*seats,WIN_START,WIN_END,*fut)):
        tape[(r["sid"],r["sk"])][r["d"]]=[float(r["bn"] or 0),float(r["sn"] or 0)]; n+=1
    print(f"席位 tape 載入 {n:,} 個 (席,股,日) 格")

    # 每日每股：高沖席 5 日淨買（扣建倉腿）
    press=defaultdict(dict)
    for (sid,sk),dd in tape.items():
        ds=sorted(dd); idx=[di[x] for x in ds if x in di]
        for j,d in enumerate(ds):
            if d not in di: continue
            i=di[d]
            b5=s5=0.0
            for k in range(j,-1,-1):
                if di.get(ds[k],-99) <= i-SEAT_WIN: break
                b5+=dd[ds[k]][0]; s5+=dd[ds[k]][1]
            if not a.no_accum_filter:
                b60=s60=0.0
                for k in range(j,-1,-1):
                    if di.get(ds[k],-99) <= i-ACC_WIN: break
                    b60+=dd[ds[k]][0]; s60+=dd[ds[k]][1]
                if b60>=ACC_BUY and (b60-s60)/b60>=ACC_NR: continue   # 建倉腿，不計倒貨壓力
            e=press[d].setdefault(sk,[0.0,0.0]); e[0]+=b5; e[1]+=s5

    # 每日橫斷面：延伸度 + ADV
    feats=defaultdict(dict)
    for sk,dd in bars.items():
        ds=sorted(dd)
        for j in range(19,len(ds)):
            d=ds[j]; w=[dd[x]["c"] for x in ds[j-19:j+1]]
            adv=sum(dd[x]["v"]*dd[x]["c"] for x in ds[j-19:j+1])/20
            if adv<ADV_MIN: continue
            ma=sum(w)/20
            feats[d][sk]={"ext":(dd[d]["c"]/ma-1)*100,"r5":(dd[d]["c"]/dd[ds[j-5]]["c"]-1)*100,"adv":adv}

    def run(ext_p,net_min_yi):
        trades=[]; sig_days=set()
        for j,d in enumerate(days[:-1]):
            f=feats.get(d)
            if not f: continue
            pool=sorted(v["ext"] for v in f.values())
            if len(pool)<30: continue
            cut=pool[min(len(pool)-1,int(len(pool)*ext_p/100))]
            nxt=days[j+1]
            for sk,v in f.items():
                if v["ext"]<cut: continue
                p=press.get(d,{}).get(sk)
                if not p: continue
                net=(p[0]-p[1])/1e8
                if net<net_min_yi: continue
                b=bars[sk].get(nxt)
                if not b or b["o"]<=0: continue
                trades.append(-((b["c"]/b["o"]-1)*100)-a.cost)
                sig_days.add(d)
        return trades,sig_days

    print(f"\n=== 網格（in-sample · 全部格子都列）· 成本 {a.cost}%／來回 ===")
    print(f"{'延伸分位':>8}{'席淨≥(億)':>11}{'訊號日':>7}{'交易':>7}{'mean%':>9}{'median%':>9}{'勝率':>8}{'累積%':>10}")
    grid=[]
    for ep in (90,95,98):
        for nm in (0.0,0.5,1.0,2.0):
            t,sd=run(ep,nm)
            if not t:
                print(f"{ep:>8}{nm:>11.1f}{0:>7}{0:>7}{'—':>9}{'—':>9}{'—':>8}{'—':>10}"); continue
            m=statistics.mean(t); md=statistics.median(t); w=sum(1 for x in t if x>0)/len(t)
            grid.append({"ext_pctl":ep,"net_min_yi":nm,"signal_days":len(sd),"n":len(t),
                         "mean":round(m,3),"median":round(md,3),"win":round(w,3),"cum":round(sum(t),1)})
            print(f"{ep:>8}{nm:>11.1f}{len(sd):>7}{len(t):>7}{m:>9.3f}{md:>9.3f}{w:>8.1%}{sum(t):>10.1f}")
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"v2_backtest_grid.json").write_text(json.dumps({"window":[days[0],days[-1]],"cost_pct":a.cost,
        "accum_filter":not a.no_accum_filter,"grid":grid},ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"\n輸出：{OUT/'v2_backtest_grid.json'}")
    return 0

if __name__=="__main__": raise SystemExit(main())
