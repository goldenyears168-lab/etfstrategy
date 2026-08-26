#!/usr/bin/env python3
"""真正的「流量」因子檢定 —— 存量水位那側已經走到底了。

**動機**：2026-08-26 診斷發現現行 HS 分數裡 75% 的權重給了散戶持股水位，
而它的排名 120 日自我相關高達 +0.956 —— 那不是訊號，是宇宙篩選。
真正的籌碼概念（誰在買、誰在賣、部位怎麼變）全在流量側，而流量側
先前只測過六個（Δ借券／Δ使用率／分點家數差／大戶週變化／股東人數／月營收），全滅。

**這輪補上三個從沒測過的，其中兩個是台股最經典的**：
  A. 三大法人買賣超（外資／投信／自營）—— 最被廣泛使用的籌碼指標，這條線竟然沒測過
  B. 分點**淨額**集中度 —— 先前只用了「買賣家數差」（廣度），
     從沒用過淨額（強度）。市面上講的「主力買超」是這個，不是家數。
  C. 借券**當日成交量** —— 先前只用餘額（存量），t13sa710 其實有當日量（流量）

**紀律**：一律在 波動×跳空×市值×週轉率 中性化下檢定，並同時報換手與淨值。
只報 gross 會系統性偏好高換手因子——那正是 v4 當初的死因。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

ROOT = Path(__file__).resolve().parents[2]
CACHE = ROOT / "reports" / "research" / "chip-signal-daily-horizon" / "flow_panel.pkl"
COST = 0.471
FR = 0.058


def branch_flow(dates: list[str]) -> pd.DataFrame:
    """逐日聚合分點淨額。⚠️ 必須逐日查：索引是 (stock_id, trade_date)，
    用 trade_date 範圍會退化成 2.24 億筆全掃（實測 >30 分鐘跑不完）。"""
    c = connect_ro()
    out = []
    for k, d in enumerate(dates):
        df = pd.read_sql_query(
            """SELECT stock_id, net FROM stock_broker_branch_daily
                WHERE trade_date=? AND net IS NOT NULL AND net<>0""", c, params=(d,))
        if df.empty:
            continue
        g = df.groupby("stock_id").net
        pos = df[df.net > 0].groupby("stock_id").net
        neg = df[df.net < 0].groupby("stock_id").net
        agg = pd.DataFrame({
            "n_br": g.count(),
            "net_all": g.sum(),
            "buy_amt": pos.sum(),
            "sell_amt": neg.sum().abs(),
            # 主力：前 5 大買超／賣超分點的淨額
            "top5_buy": pos.apply(lambda s: s.nlargest(5).sum()),
            "top5_sell": neg.apply(lambda s: s.nsmallest(5).sum().__abs__()),
            "nb": pos.count(), "ns": neg.count(),
        }).fillna(0.0)
        agg["trade_date"] = d
        out.append(agg.reset_index())
        if k % 100 == 0:
            print(f"  分點 {k}/{len(dates)}…", flush=True)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def build(start: str) -> pd.DataFrame:
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, open, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date >= ? AND close IS NOT NULL""",
        c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))
    si = pd.read_sql_query(
        """SELECT stock_id, trade_date, sbl_balance, short_limit
             FROM stock_short_interest_daily WHERE trade_date >= ?""", c, params=(start,))
    # ⚠️ stock_institutional_daily 同一 stock-day 同時有 finmind 與 twse_t86 兩列。
    # 不去重 → merge 後面板出現重複列 → 後面的 shift(-1) 會抓到「同一天的另一列」，
    # 讓 nx_open/nx_close 變成訊號當天的開收盤 = 純未來函數（實測 t 從 2 暴衝到 22）。
    # 這與 margin-daytrade-dual-source-trap 是同一類坑。
    inst = pd.read_sql_query(
        """SELECT stock_id, trade_date, foreign_net, investment_trust_net,
                  dealer_self_net, source FROM stock_institutional_daily
            WHERE trade_date >= ?""", c, params=(start,))
    inst["rk"] = inst.source.map({"twse_t86": 0, "finmind": 1}).fillna(9)
    inst = (inst.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
                .drop(columns=["rk", "source"]))
    sbl = pd.read_sql_query(
        """SELECT stock_id, trade_date, volume AS sbl_vol FROM stock_sbl_fee_daily
            WHERE deal_type='ALL' AND trade_date >= ?""", c, params=(start,))

    d = px.merge(si, on=["stock_id", "trade_date"], how="inner")
    d["shares"] = (d.short_limit * 4).replace(0, np.nan)
    d = d[(d.vol >= 500) & (d.close >= 10) & d.shares.notna()]
    d = d[~d.stock_id.str.startswith("00")].copy()
    dates = sorted(d.trade_date.unique())
    print(f"基礎面板 {len(d):,} stock-day · {len(dates)} 日", flush=True)

    cache = CACHE.parent / "branch_flow_cache.pkl"
    br = pd.read_pickle(cache) if cache.exists() else branch_flow(dates)
    if not cache.exists() and not br.empty:
        br.to_pickle(cache)
    for name, f in (("inst", inst), ("sbl", sbl), ("br", br)):
        if f.empty:
            continue
        dup = f.duplicated(["stock_id", "trade_date"]).sum()
        if dup:
            raise RuntimeError(f"{name} 有 {dup:,} 筆重複 stock-day，先去重再 merge")
        d = d.merge(f, on=["stock_id", "trade_date"], how="left")
        dup = d.duplicated(["stock_id", "trade_date"]).sum()
        if dup:
            raise RuntimeError(f"merge {name} 後面板出現 {dup:,} 筆重複")
    d = d.sort_values(["stock_id", "trade_date"])
    g = d.groupby("stock_id", group_keys=False)
    # 全部除以成交量正規化 —— 否則就是在測「哪檔成交量大」
    volsh = d.vol * 1000
    for src, dst in (("foreign_net", "f_for"), ("investment_trust_net", "f_itc"),
                     ("dealer_self_net", "f_dlr")):
        d[dst] = d[src] / volsh
        d[f"{dst}5"] = g[dst].transform(lambda s: s.rolling(5, min_periods=3).mean())
        d[f"{dst}20"] = g[dst].transform(lambda s: s.rolling(20, min_periods=10).mean())
    d["f_3i"] = (d.foreign_net + d.investment_trust_net + d.dealer_self_net) / volsh
    d["f_3i5"] = g.f_3i.transform(lambda s: s.rolling(5, min_periods=3).mean())
    d["f_main"] = (d.top5_buy - d.top5_sell) / volsh          # 主力淨額（前5大）
    d["f_main5"] = g.f_main.transform(lambda s: s.rolling(5, min_periods=3).mean())
    d["f_conc"] = (d.top5_buy + d.top5_sell) / (d.buy_amt + d.sell_amt)  # 集中度
    d["f_brnet"] = d.net_all / volsh
    d["f_brdiff"] = (d.nb - d.ns) / d.n_br.replace(0, np.nan)  # 舊的家數差（對照）
    d["f_sblvol"] = d.sbl_vol / d.vol                          # 借券當日量/成交量
    d["f_sblvol5"] = g.f_sblvol.transform(lambda s: s.rolling(5, min_periods=3).mean())
    d["turn"] = d.vol / (d.shares / 1000)
    # shift(-1) 抓的是「面板裡的下一列」，但面板被流動性濾網打過洞，
    # 下一列不見得是下一個交易日。不檢查的話等於偷偷把 T+3 的報酬當成 T+1。
    g2 = d.groupby("stock_id", group_keys=False)
    d["nx_open"] = g2.open.shift(-1)
    d["nx_close"] = g2.close.shift(-1)
    d["nx_date"] = g2.trade_date.shift(-1)
    nxt = dict(zip(dates, dates[1:]))
    ok = d.nx_date == d.trade_date.map(nxt)
    print(f"  下一列確為次一交易日的比例 {ok.mean()*100:.1f}%（其餘剔除）", flush=True)
    d.loc[~ok, ["nx_open", "nx_close"]] = np.nan
    d["oc"] = d.nx_close / d.nx_open - 1
    d["gap"] = d.nx_open / d.close - 1
    d["vol60"] = g2.close.transform(lambda s: s.pct_change().rolling(60, min_periods=30).std())
    d["mcap"] = d.close * d.shares
    return d.dropna(subset=["oc", "gap", "vol60", "mcap", "turn"])


def neutral(d: pd.DataFrame) -> pd.DataFrame:
    d = d.copy()
    for c, s_ in (("vq", "vol60"), ("gq", "gap"), ("mq", "mcap"), ("tq", "turn")):
        d[c] = d.groupby("trade_date")[s_].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    d = d.dropna(subset=["vq", "gq", "mq", "tq"])
    out = pd.Series(np.nan, index=d.index)
    for _, g in d.groupby("trade_date"):
        if len(g) < 120:
            continue
        P = [np.ones((len(g), 1))]
        for c in ("vq", "gq", "mq", "tq"):
            P.append(pd.get_dummies(g[c].astype(int), drop_first=True).to_numpy(float))
        P.append(pd.get_dummies(g.vq.astype(int) * 5 + g.gq.astype(int),
                                drop_first=True).to_numpy(float))
        X = np.column_stack(P)
        y = g.oc.to_numpy()
        try:
            b, *_ = np.linalg.lstsq(X, y, rcond=None)
            out.loc[g.index] = y - X @ b
        except np.linalg.LinAlgError:
            pass
    d["oc_n"] = out
    return d


def evaluate(d: pd.DataFrame, col: str, lab: str, sign: int) -> None:
    """sign=+1 表示『值越大越偏多』，統一轉成越大越偏空後排序。"""
    x = d.dropna(subset=[col, "oc_n"]).copy()
    x["s"] = -sign * x[col]
    if x.trade_date.nunique() < 100:
        print(f"  {lab:<26} 樣本不足（{x.trade_date.nunique()} 日）")
        return
    prev_l = prev_s = set()
    rl, rs, tl = [], [], []
    for _, g in x.groupby("trade_date", sort=True):
        if len(g) < 60:
            continue
        n = max(3, int(round(len(g) * FR)))
        q = g.sort_values("s")
        L, S = list(q.stock_id.head(n)), list(q.stock_id.tail(n))
        rl.append(q.oc_n.head(n).mean())
        rs.append(-q.oc_n.tail(n).mean())
        if prev_l:
            tl.append(len(set(L) - prev_l) / n)
        prev_l, prev_s = set(L), set(S)
    L_ = pd.Series(rl).dropna()
    S_ = pd.Series(rs).dropna()
    tau = np.mean(tl)
    def st(v):
        return v.mean() * 100, v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))
    gl, tlt = st(L_)
    gs, tst = st(S_)
    sp, tsp = st(L_ + S_)
    print(f"  {lab:<26}{tau*100:>6.1f}%{gl:>+9.4f}%{tlt:>+7.2f}"
          f"{(gl - tau * COST) * 242:>+8.2f}%{gs:>+9.4f}%{tst:>+7.2f}{sp:>+9.4f}%{tsp:>+7.2f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()
    if args.rebuild or not CACHE.exists():
        d = build(args.start)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        d.to_pickle(CACHE)
    else:
        d = pd.read_pickle(CACHE)
    d = neutral(d)
    print(f"\n面板 {len(d):,} · {d.trade_date.nunique()} 日 · "
          f"{d.trade_date.min()}~{d.trade_date.max()}")
    print("中性化＝波動／跳空／市值／週轉率 五分位虛擬變數 ＋ 波動×跳空交互\n")
    print(f"{'因子':<28}{'換手':>6}{'多頭腿':>9}{'t':>7}{'淨值/年':>8}"
          f"{'空頭腿':>9}{'t':>7}{'多空':>9}{'t':>7}")
    for col, lab, sign in (
        ("f_for", "外資買賣超/量 1日", +1),
        ("f_for5", "外資買賣超/量 5日", +1),
        ("f_for20", "外資買賣超/量 20日", +1),
        ("f_itc", "投信買賣超/量 1日", +1),
        ("f_itc5", "投信買賣超/量 5日", +1),
        ("f_dlr", "自營買賣超/量 1日", +1),
        ("f_3i", "三大法人合計 1日", +1),
        ("f_3i5", "三大法人合計 5日", +1),
        ("f_main", "主力淨額（前5大）1日", +1),
        ("f_main5", "主力淨額（前5大）5日", +1),
        ("f_conc", "分點集中度", +1),
        ("f_brnet", "分點淨額合計/量", +1),
        ("f_brdiff", "【對照】分點家數差", -1),
        ("f_sblvol", "借券當日量/成交量", -1),
        ("f_sblvol5", "借券當日量 5日", -1),
    ):
        if col in d.columns:
            evaluate(d, col, lab, sign)
    print("\n本輪同時測 15 個因子，多重檢定會膨脹顯著性：全部列出，不事後挑選。")
    print(f"損益兩平 gross = 換手 × {COST}%；只做多腿免借券費。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
