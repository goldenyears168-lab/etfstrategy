#!/usr/bin/env python3
"""籌碼因子實驗室 —— 統一因子庫 ＋ **固定評估協定**。

**為什麼要有這支**：前三天的教訓是，每次比較都可能因為口徑不同而得出相反
結論（線性 vs 分格中性化差了 3 倍 t；成本取平均 vs 相加讓多空從 +13.8%
變成 −6.7%）。多路探索若讓每個人自訂衡量方式，最後只會選出「最會挑指標
的那一路」。**因此評估器在此凍結，探索者只能寫分數構造。**

## 統一的標準化基準（舊公式最大的毛病是混用）

  basis='xs'  純橫斷面：當日全市場百分位 → [-1,+1]
  basis='ts'  純時序：該股自身 243 日百分位 → [-1,+1]
  basis='tsxs' 兩段式：先時序百分位，再橫斷面百分位（兩者皆統一套用）

一份分數只能用一種 basis，不可混用。

## 固定的評估協定

  · 報酬 = open(T+1) → close(T+K)，**必須是真次一交易日**（面板有流動性洞）
  · 風險中性化 = 對 波動/跳空/市值/週轉率 五分位虛擬變數 + 波動×跳空交互
    逐日迴歸取殘差（非線性；線性控制只移除一半）
  · 成本 = 證交稅 0.30% + 手續費 2×0.0855%（6 折）= 0.471%/來回
  · 換手 = 多頭腿名單每日變動比例；淨值 = gross − 換手×成本
  · **walk-forward**：形成期 250 日、評估期 60 日、滾動前進，只報 OOS
  · t 值按日聚類

## 已知且已修的坑（勿重蹈）

  · stock_institutional_daily 同 stock-day 有 finmind+twse_t86 兩列 → 必去重
  · groupby.shift(-1) 不保證是次一交易日 → 必檢查
  · 分點表 2.24 億列，索引是 (stock_id, trade_date) → 只能逐日查
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

ROOT = Path(__file__).resolve().parents[2]
DIR = ROOT / "reports" / "research" / "chip-signal-daily-horizon"
PANEL = DIR / "chip_lab_panel.pkl"
COST = 0.471
FR = 0.058                      # 單邊取全市場 5.8%（約 30 檔）
CONTROLS = ("vol60", "gap", "mcap", "turn")

# 因子清單：name -> (原始欄, 方向 +1=值大偏多, 分類)
FACTORS = {
    # ---- 借券複合體 ----
    "sbl_pct":   ("sbl_pct", -1, "borrow_level"),
    "sbl_util":  ("util", -1, "borrow_level"),
    "fee":       ("fee_rate_vw", -1, "borrow_level"),
    "d_sbl":     ("d_sbl", -1, "borrow_flow"),
    "d_util":    ("d_util", -1, "borrow_flow"),
    "sbl_volr":  ("sbl_volr", -1, "borrow_flow"),
    # ---- 集保股權結構（週頻）----
    "retail":    ("ret_pct", -1, "ownership"),
    "big":       ("big_pct", +1, "ownership"),
    "d_retail":  ("d_ret_pct", -1, "ownership_flow"),
    "d_holders": ("d_holders", -1, "ownership_flow"),
    # ---- 法人流量 ----
    "for_1":     ("f_for", +1, "inst_flow"),
    "for_5":     ("f_for5", +1, "inst_flow"),
    "for_20":    ("f_for20", +1, "inst_flow"),
    "itc_1":     ("f_itc", +1, "inst_flow"),
    "itc_5":     ("f_itc5", +1, "inst_flow"),
    "dlr_1":     ("f_dlr", +1, "inst_flow"),
    "inst3_1":   ("f_3i", +1, "inst_flow"),
    "inst3_5":   ("f_3i5", +1, "inst_flow"),
    # ---- 分點 ----
    "br_diff":   ("f_brdiff", -1, "branch"),
    "br_main":   ("f_main", +1, "branch"),
    "br_main5":  ("f_main5", +1, "branch"),
    "br_conc":   ("f_conc", +1, "branch"),
    "br_net":    ("f_brnet", +1, "branch"),
}


# ------------------------------------------------------------------ 面板


def build_panel(start: str = "2023-01-01") -> pd.DataFrame:
    c = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, source, open, close, volume/1000.0 AS vol
             FROM stock_daily_bars WHERE trade_date >= ? AND close IS NOT NULL""",
        c, params=(start,))
    px["rk"] = px.source.map({"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}).fillna(9)
    px = (px.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
            .drop(columns=["rk", "source"]))
    si = pd.read_sql_query(
        """SELECT s.stock_id, s.trade_date, s.sbl_balance, s.sbl_next_limit,
                  s.short_limit, f.fee_rate_vw, f.volume AS sbl_vol
             FROM stock_short_interest_daily s
             LEFT JOIN (SELECT stock_id, trade_date, fee_rate_vw, volume
                          FROM stock_sbl_fee_daily WHERE deal_type='ALL') f
               ON f.stock_id=s.stock_id AND f.trade_date=s.trade_date
            WHERE s.trade_date >= ?""", c, params=(start,))
    inst = pd.read_sql_query(
        """SELECT stock_id, trade_date, foreign_net, investment_trust_net,
                  dealer_self_net, source FROM stock_institutional_daily
            WHERE trade_date >= ?""", c, params=(start,))
    inst["rk"] = inst.source.map({"twse_t86": 0, "finmind": 1}).fillna(9)
    inst = (inst.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
                .drop(columns=["rk", "source"]))
    br = pd.read_pickle(DIR / "branch_flow_cache.pkl").drop_duplicates(
        ["stock_id", "trade_date"])

    d = px.merge(si, on=["stock_id", "trade_date"], how="inner")
    d["shares"] = (d.short_limit * 4).replace(0, np.nan)
    d = d[(d.vol >= 500) & (d.close >= 10) & d.shares.notna()]
    d = d[~d.stock_id.str.startswith("00")].copy()
    for nm, f in (("inst", inst), ("br", br)):
        assert not f.duplicated(["stock_id", "trade_date"]).any(), f"{nm} 有重複"
        d = d.merge(f, on=["stock_id", "trade_date"], how="left")
        assert not d.duplicated(["stock_id", "trade_date"]).any(), f"merge {nm} 後重複"

    # 集保（週頻，PIT +4 天）
    disp = _dispersion()
    d["_k"] = pd.to_datetime(d.trade_date)
    d = pd.merge_asof(d.sort_values("_k"), disp.sort_values("_k"), on="_k",
                      by="stock_id", direction="backward", allow_exact_matches=False)
    d = d.drop(columns=["_k"])
    assert not d.duplicated(["stock_id", "trade_date"]).any(), "merge 集保後重複"

    d = d.sort_values(["stock_id", "trade_date"])
    g = d.groupby("stock_id", group_keys=False)
    volsh = d.vol * 1000
    d["sbl_pct"] = d.sbl_balance / d.shares
    d["util"] = d.sbl_balance / (d.sbl_balance + d.sbl_next_limit)
    d["d_sbl"] = g.sbl_balance.diff() / d.shares
    d["d_util"] = g.util.diff()
    d["sbl_volr"] = d.sbl_vol / d.vol
    for src, dst in (("foreign_net", "f_for"), ("investment_trust_net", "f_itc"),
                     ("dealer_self_net", "f_dlr")):
        d[dst] = d[src] / volsh
    d["f_3i"] = (d.foreign_net + d.investment_trust_net + d.dealer_self_net) / volsh
    for base, win in (("f_for", 5), ("f_for", 20), ("f_itc", 5), ("f_3i", 5)):
        d[f"{base}{win}"] = g[base].transform(lambda s: s.rolling(win, min_periods=win//2).mean())
    d["f_main"] = (d.top5_buy - d.top5_sell) / volsh
    d["f_main5"] = g.f_main.transform(lambda s: s.rolling(5, min_periods=3).mean())
    d["f_conc"] = (d.top5_buy + d.top5_sell) / (d.buy_amt + d.sell_amt).replace(0, np.nan)
    d["f_brnet"] = d.net_all / volsh
    d["f_brdiff"] = (d.nb - d.ns) / d.n_br.replace(0, np.nan)
    d["turn"] = d.vol / (d.shares / 1000)

    # 報酬：必須是真次一交易日
    dates = np.sort(d.trade_date.unique())
    nxt = dict(zip(dates[:-1], dates[1:]))
    g = d.groupby("stock_id", group_keys=False)
    d["nx_open"] = g.open.shift(-1)
    d["nx_close"] = g.close.shift(-1)
    d["nx_date"] = g.trade_date.shift(-1)
    ok = d.nx_date == d.trade_date.map(nxt)
    d.loc[~ok, ["nx_open", "nx_close"]] = np.nan
    d["oc"] = d.nx_close / d.nx_open - 1
    d["gap"] = d.nx_open / d.close - 1
    d["vol60"] = g.close.transform(lambda s: s.pct_change().rolling(60, min_periods=30).std())
    d["mcap"] = d.close * d.shares
    d = d.dropna(subset=["oc", "gap", "vol60", "mcap", "turn"])
    return d.reset_index(drop=True)


def _dispersion() -> pd.DataFrame:
    c = connect_ro()
    df = pd.read_sql_query(
        """WITH pick AS (
              SELECT stock_id, as_of_date, source,
                     ROW_NUMBER() OVER (PARTITION BY stock_id, as_of_date
                       ORDER BY CASE source WHEN 'tdcc' THEN 0 ELSE 1 END) rn
                FROM (SELECT DISTINCT stock_id, as_of_date, source
                        FROM stock_holding_dispersion_weekly))
           SELECT p.stock_id, p.as_of_date,
                  SUM(CASE WHEN w.level IN ('1','2','3','4','5','6','7','8')
                           THEN w.percent ELSE 0 END) AS ret_pct,
                  SUM(CASE WHEN w.level IN ('12','13','14','15')
                           THEN w.percent ELSE 0 END) AS big_pct,
                  MAX(CASE WHEN w.level='17' THEN w.people END) AS holders
             FROM pick p JOIN stock_holding_dispersion_weekly w
               ON w.stock_id=p.stock_id AND w.as_of_date=p.as_of_date
              AND w.source=p.source
            WHERE p.rn=1 GROUP BY p.stock_id, p.as_of_date""", c)
    df = df[df.ret_pct > 0].sort_values(["stock_id", "as_of_date"])
    g = df.groupby("stock_id", group_keys=False)
    df["d_ret_pct"] = g.ret_pct.diff()
    df["d_holders"] = g.holders.pct_change()
    df["_k"] = pd.to_datetime(df.as_of_date) + pd.Timedelta(days=4)   # PIT
    return df.drop(columns=["as_of_date"])


def load(rebuild: bool = False) -> pd.DataFrame:
    if rebuild or not PANEL.exists():
        d = build_panel()
        DIR.mkdir(parents=True, exist_ok=True)
        d.to_pickle(PANEL)
        return d
    return pd.read_pickle(PANEL)


# ------------------------------------------------------------------ 標準化


def norm(d: pd.DataFrame, col: str, basis: str = "xs", ts_win: int = 243) -> pd.Series:
    """統一標準化到 [-1,+1]。一份分數只能用同一個 basis。"""
    if basis == "xs":
        return (d.groupby("trade_date")[col].rank(pct=True) - 0.5) * 2
    if basis == "ts":
        g = d.sort_values(["stock_id", "trade_date"]).groupby("stock_id", group_keys=False)
        r = g[col].transform(lambda s: s.rolling(ts_win, min_periods=60).rank(pct=True))
        return (r.reindex(d.index) - 0.5) * 2
    if basis == "tsxs":
        t = norm(d, col, "ts", ts_win)
        tmp = d.assign(_t=t)
        return (tmp.groupby("trade_date")._t.rank(pct=True) - 0.5) * 2
    raise ValueError(f"未知 basis: {basis}")


def signed(d: pd.DataFrame, name: str, basis: str = "xs") -> pd.Series:
    """回傳「越大越偏多」的標準化因子（方向已統一）。"""
    col, sign, _ = FACTORS[name]
    if col not in d.columns:
        return pd.Series(np.nan, index=d.index)
    return sign * norm(d, col, basis)


# ------------------------------------------------------------------ 評估（凍結）


def _neutral(d: pd.DataFrame) -> pd.Series:
    x = d.copy()
    for c in CONTROLS:
        x[f"q_{c}"] = x.groupby("trade_date")[c].transform(
            lambda s: pd.qcut(s.rank(method="first"), 5, labels=False, duplicates="drop"))
    x = x.dropna(subset=[f"q_{c}" for c in CONTROLS])
    out = pd.Series(np.nan, index=d.index)
    for _, g in x.groupby("trade_date"):
        if len(g) < 120:
            continue
        P = [np.ones((len(g), 1))]
        for c in CONTROLS:
            P.append(pd.get_dummies(g[f"q_{c}"].astype(int), drop_first=True).to_numpy(float))
        P.append(pd.get_dummies(g.q_vol60.astype(int) * 5 + g.q_gap.astype(int),
                                drop_first=True).to_numpy(float))
        X = np.column_stack(P)
        y = g.oc.to_numpy()
        try:
            b, *_ = np.linalg.lstsq(X, y, rcond=None)
            out.loc[g.index] = y - X @ b
        except np.linalg.LinAlgError:
            pass
    return out


def evaluate(d: pd.DataFrame, score: pd.Series, *, oos_only: bool = True,
             form: int = 250, step: int = 60) -> dict:
    """凍結的評估協定。score 越大越偏多。回傳 OOS 的多頭腿與多空表現。

    walk-forward：前 ``form`` 日只用於形成（此處分數已給定，故僅作為
    暖機與時間切分），之後每 ``step`` 日為一個 OOS 區塊，只彙總 OOS。
    """
    x = d.assign(_s=score).dropna(subset=["_s"]).copy()
    if "oc_n" not in x.columns:
        x["oc_n"] = _neutral(x)
    x = x.dropna(subset=["oc_n"])
    dates = np.sort(x.trade_date.unique())
    if len(dates) < form + step:
        return {"error": "樣本不足", "n_days": len(dates)}
    oos = set(dates[form:]) if oos_only else set(dates)

    prev_l = set()
    rows = []
    for t, g in x.groupby("trade_date", sort=True):
        if len(g) < 120:
            continue
        n = max(3, int(round(len(g) * FR)))
        q = g.sort_values("_s", ascending=False)      # 分數大 = 偏多 → 放前面
        L = list(q.stock_id.head(n))
        S = list(q.stock_id.tail(n))
        turn = len(set(L) - prev_l) / n if prev_l else np.nan
        prev_l = set(L)
        if t in oos:
            rows.append({"trade_date": t,
                         "long": q.oc_n.head(n).mean(),
                         "short": -q.oc_n.tail(n).mean(),
                         "turn": turn})
    r = pd.DataFrame(rows)
    if len(r) < 30:
        return {"error": "OOS 日數不足", "n_days": len(r)}

    def stat(v):
        v = v.dropna()
        m = v.mean() * 100
        return m, m / (v.std(ddof=1) / np.sqrt(len(v)) * 100)

    gl, tl = stat(r.long)
    gs, ts = stat(r.short)
    sp, tsp = stat(r.long + r.short)
    tau = r.turn.mean()
    return {
        "n_days": len(r), "turnover": tau,
        "long_gross": gl, "long_t": tl,
        "long_net_day": gl - tau * COST, "long_net_ann": (gl - tau * COST) * 242,
        "short_gross": gs, "short_t": ts,
        "spread_gross": sp, "spread_t": tsp,
        "breakeven_cost": gl / tau if tau and tau > 0 else np.nan,
    }


def report(name: str, res: dict) -> str:
    if "error" in res:
        return f"  {name:<34}{res['error']}"
    return (f"  {name:<34}{res['turnover']*100:>6.1f}%{res['long_gross']:>+10.4f}%"
            f"{res['long_t']:>+7.2f}{res['long_net_ann']:>+9.2f}%"
            f"{res['spread_gross']:>+10.4f}%{res['spread_t']:>+7.2f}{res['n_days']:>6}")


HEADER = (f"{'組態':<36}{'換手':>6}{'多頭gross':>10}{'t':>7}{'淨值/年':>9}"
          f"{'多空gross':>10}{'t':>7}{'OOS日':>6}")


if __name__ == "__main__":
    d = load(rebuild="--rebuild" in sys.argv)
    print(f"面板 {len(d):,} stock-day · {d.trade_date.nunique()} 日 · "
          f"{d.trade_date.min()}~{d.trade_date.max()} · 每日均 {len(d)/d.trade_date.nunique():.0f} 檔")
    cov = {k: d[v[0]].notna().mean() * 100 for k, v in FACTORS.items() if v[0] in d.columns}
    print("因子覆蓋率：")
    for k, v in sorted(cov.items(), key=lambda z: -z[1]):
        print(f"  {k:<12}{v:>6.1f}%")
