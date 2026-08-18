#!/usr/bin/env python3
"""集保大戶持股比變化 → 前瞻超額報酬（多門檻 × 多天期 × 無條件基準對照）.

背景：本專案整個「持股存量」軸是空的（分點是流量、法人是流量）。這是第一條正交維度。
先導測試（46 檔專家池、β=1.15 調整、4 週）：大戶升 ≥+1.0pp → 超額 +10.51%／中位 +4.67%／
勝率 57.5%；大戶降 → 中位 −0.47%／勝率 48.5%。Spearman rho=+0.063 p=0.0000。
**方向在多方，不在空方**（與 Lakonishok & Lee 2001 RFS 14(1):79-111 的內部人買賣不對稱一致）。

本腳本把它擴到 251 檔期貨宇宙並補三個先導測試缺的護欄：
  1. 無條件基準對照（本專案反覆吃虧的那一條）
  2. 不重疊區塊（週頻資料算 4 週前瞻＝4 倍重疊，p 值會被高估）
  3. holdout：2025-10-01 之前為 IS、之後保留

PIT：集保是週五資料、下週一才可得 → 前瞻報酬從資料日之後**第 2 個交易日**起算。
"""
from __future__ import annotations
import argparse, sqlite3, statistics, sys
from pathlib import Path
import pandas as pd
import scipy.stats as ss
ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
from stock_db import DEFAULT_DB_PATH  # noqa: E402

CACHE = ROOT / "reports/research/chip-overlays/cache/holding_shares_per_futures_universe.csv"
OUT = ROOT / "reports/research/chip-overlays"
BETA = 1.15
HOLDOUT_FROM = "2025-10-01"


def load_prices(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    px = {}
    for sid, td, cl in c.execute(
        "select stock_id,trade_date,close from stock_daily_bars where source='finmind' and trade_date>='2024-05-01'"):
        if cl: px.setdefault(sid, {})[td] = float(cl)
    ix = {t: float(v) for t, v in c.execute(
        "select date,close from daily_bars where code='IX0001' and date>='2024-05-01'") if v}
    c.close()
    return px, ix


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    ap.add_argument("--tier", default="pct_200", help="pct_100/pct_200/pct_400/pct_1000")
    a = ap.parse_args()
    if not CACHE.exists():
        print(f"缺快取：{CACHE}（先跑 cache_holding_shares_futures_universe.py）"); return 1
    d = pd.read_csv(CACHE); d["sid"] = d.sid.astype(str)
    px, ix = load_prices(a.db)
    days = sorted({t for m in px.values() for t in m}); di = {t: i for i, t in enumerate(days)}

    def fwd(sid, d0, h):
        nxt = [t for t in days if t > d0][:1]
        if not nxt: return None
        i = di[nxt[0]] + 1; j = i + 5 * h          # 資料可得日的次一交易日進場
        if j >= len(days): return None
        p0, p1 = px.get(sid, {}).get(days[i]), px.get(sid, {}).get(days[j])
        i0, i1 = ix.get(days[i]), ix.get(days[j])
        if not p0 or not p1 or not i0 or not i1: return None
        return (p1 / p0 - 1) * 100 - BETA * ((i1 / i0 - 1) * 100)

    col, chg = a.tier, f"{a.tier}_chg"
    rows = []
    for _, r in d.iterrows():
        if pd.isna(r.get(chg)): continue
        rec = {"sid": r.sid, "d": r.d, "chg": float(r[chg]), "lvl": float(r[col])}
        for h in (1, 2, 4): rec[f"f{h}"] = fwd(r.sid, r.d, h)
        rows.append(rec)
    df = pd.DataFrame(rows)
    df["seq"] = df.groupby("sid").cumcount()
    print(f"門檻={col}（{col.split('_')[1]} 張以上）· {df.sid.nunique()} 檔 · {df.d.min()} ~ {df.d.max()} · {len(df):,} 週×股\n")

    def st(v, lab, ind="  "):
        v = [x for x in v if x is not None and pd.notna(x)]
        if len(v) < 15: print(f"{ind}{lab:<30} n={len(v):>5} ⚠不足"); return
        print(f"{ind}{lab:<30} n={len(v):>5}  mean={statistics.mean(v):+7.3f}%  "
              f"median={statistics.median(sorted(v)):+7.3f}%  勝率={sum(1 for x in v if x>0)/len(v):6.1%}")

    for tag, sub in (("全期", df), ("IS（<2025-10）", df[df.d < HOLDOUT_FROM]),
                     ("HOLDOUT（>=2025-10）", df[df.d >= HOLDOUT_FROM])):
        print(f"===== {tag} =====")
        for h in (1, 2, 4):
            f = f"f{h}"; print(f"  未來 {h} 週：")
            st(sub[f], "【基準】無條件", "    ")
            st(sub[sub.chg >= 1.0][f], "大戶升 >=+1.0pp", "    ")
            st(sub[sub.chg <= -1.0][f], "大戶降 <=-1.0pp", "    ")
            s = sub.dropna(subset=[f])
            if len(s) > 50:
                sp = ss.spearmanr(s["chg"], s[f])
                # 不重疊：每 4 週取一筆（4 週前瞻的重疊修正）
                nv = s[s.seq % 4 == 0]
                spn = ss.spearmanr(nv["chg"], nv[f]) if len(nv) > 50 else None
                extra = f" | 不重疊(n={len(nv)}) rho={spn.statistic:+.4f} p={spn.pvalue:.4f}" if spn else ""
                print(f"      Spearman rho={sp.statistic:+.4f} p={sp.pvalue:.4f}{extra}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
