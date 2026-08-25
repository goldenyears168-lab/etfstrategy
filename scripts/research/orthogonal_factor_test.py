#!/usr/bin/env python3
"""正交因子檢定：集保股權分散（週）＋ 月營收（月）。

**為什麼測這兩個**：2026-08-26 證明借券複合體（v4 五項）在波動×跳空雙中性
後 t=+0.05，等於空的。這兩個因子是唯二不在那個資訊源裡的候選，且低頻＝
天然低換手，正好避開 v4 每日 75% 換手的死穴。

**紀律（上一輪的教訓）**：新因子從第一天就在雙中性下檢定。
若上週就這樣做，籌碼那條線兩天前就該收掉。中性化用逐日橫斷面迴歸
``oc ~ vol60 + gap`` 取殘差，保留完整橫斷面（不切格子，避免小樣本雜訊）。

**PIT 一律保守**：
  · 股權分散 as_of_date 是週五結算，集保下週一二才公布 → +4 個日曆天後才可用
  · 月營收 date 是公布月首日，法定期限為當月 10 日 → +10 個日曆天後才可用
    （create_time 早期為空，不能只靠它）
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
D = ROOT / "reports" / "research" / "chip-signal-daily-horizon"

BIG = ["400,001-600,000", "600,001-800,000", "800,001-1,000,000", "more than 1,000,001"]
RETAIL = ["1-999", "1,000-5,000", "5,001-10,000", "10,001-15,000",
          "15,001-20,000", "20,001-30,000", "30,001-40,000", "40,001-50,000"]


def dispersion_factors() -> pd.DataFrame:
    d = pd.read_pickle(D / "factor_dispersion.pkl")
    d["date"] = pd.to_datetime(d.date)
    big = (d[d.HoldingSharesLevel.isin(BIG)].groupby(["stock_id", "date"])
             .agg(big_pct=("percent", "sum"), big_people=("people", "sum")).reset_index())
    ret = (d[d.HoldingSharesLevel.isin(RETAIL)].groupby(["stock_id", "date"])
             .agg(ret_pct=("percent", "sum")).reset_index())
    tot = (d[d.HoldingSharesLevel == "total"][["stock_id", "date", "people"]]
             .rename(columns={"people": "holders"}))
    x = big.merge(ret, on=["stock_id", "date"]).merge(tot, on=["stock_id", "date"])
    x = x.sort_values(["stock_id", "date"])
    g = x.groupby("stock_id", group_keys=False)
    x["big_chg1"] = g.big_pct.diff()
    x["big_chg4"] = g.big_pct.diff(4)
    x["ret_chg1"] = g.ret_pct.diff()
    x["holder_chg1"] = g.holders.pct_change()          # 股東人數減少＝籌碼集中
    x["big_people_chg1"] = g.big_people.diff()
    # PIT：週五結算 + 4 天後才公布
    x["avail"] = (x.date + pd.Timedelta(days=4)).dt.strftime("%Y-%m-%d")
    return x


def revenue_factors() -> pd.DataFrame:
    r = pd.read_pickle(D / "factor_revenue.pkl")
    r["ym"] = r.revenue_year * 12 + r.revenue_month
    r = r.sort_values(["stock_id", "ym"])
    g = r.groupby("stock_id", group_keys=False)
    r["mom"] = g.revenue.pct_change()
    # 去年同月：用 self-merge 取 ym-12，比 groupby.apply+concatenate 穩健
    ly = r[["stock_id", "ym", "revenue"]].rename(columns={"revenue": "rev_ly"})
    ly["ym"] = ly.ym + 12
    r = r.merge(ly, on=["stock_id", "ym"], how="left")
    r["yoy"] = r.revenue / r.rev_ly - 1
    # merge 之後要重新分組：舊的 groupby 物件看不到新欄位
    r = r.sort_values(["stock_id", "ym"])
    g = r.groupby("stock_id", group_keys=False)
    r["yoy_prev"] = g.yoy.shift(1)
    r["yoy_accel"] = r.yoy - r.yoy_prev
    # PIT：法定公布期限為次月 10 日；date 已是次月首日
    r["avail"] = (pd.to_datetime(r.date) + pd.Timedelta(days=10)).dt.strftime("%Y-%m-%d")
    return r


def attach(panel: pd.DataFrame, fac: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """把低頻因子貼到日頻面板：每個交易日取「已公布的最新一筆」。"""
    # merge_asof 不接受字串鍵；兩邊都轉 datetime，合完把面板的日期字串還原。
    f = fac[["stock_id", "avail", *cols]].dropna(subset=["avail"]).copy()
    f["_k"] = pd.to_datetime(f.avail)
    f = f.drop(columns=["avail"]).sort_values("_k")
    p = panel.copy()
    p["_k"] = pd.to_datetime(p.trade_date)
    p = p.sort_values("_k")
    out = pd.merge_asof(p, f, on="_k", by="stock_id",
                        direction="backward", allow_exact_matches=False)
    return out.drop(columns=["_k"])


def neutral_resid(d: pd.DataFrame, y: str) -> pd.Series:
    """逐日橫斷面對 vol60 與 gap 迴歸取殘差 —— 波動×跳空雙中性。"""
    out = pd.Series(np.nan, index=d.index)
    for _, g in d.groupby("trade_date"):
        g2 = g.dropna(subset=[y, "vol60", "gap"])
        if len(g2) < 80:
            continue
        X = np.column_stack([np.ones(len(g2)), g2.vol60, g2.gap])
        b = np.linalg.lstsq(X, g2[y].to_numpy(), rcond=None)[0]
        out.loc[g2.index] = g2[y].to_numpy() - X @ b
    return out


def test(d: pd.DataFrame, col: str, label: str, n_frac: float = 0.058) -> None:
    """col 一律轉成『越大越偏空』後排序；報原始與雙中性兩個版本。"""
    def sp(g, y):
        g = g.dropna(subset=[col, y])
        n = max(3, int(round(len(g) * n_frac)))
        if len(g) < 60:
            return np.nan
        s = g.sort_values(col)
        return s[y].head(n).mean() - s[y].tail(n).mean()

    def stat(s):
        s = s.dropna()
        if len(s) < 30:
            return np.nan, np.nan, len(s)
        return s.mean() * 100, s.mean() / (s.std(ddof=1) / np.sqrt(len(s))), len(s)

    raw = d.groupby("trade_date").apply(lambda g: sp(g, "oc"), include_groups=False)
    neu = d.groupby("trade_date").apply(lambda g: sp(g, "oc_n"), include_groups=False)
    # 換手：每日兩端名單的變動比例
    prev_l = prev_s = set()
    turn = []
    for _, g in d.groupby("trade_date", sort=True):
        g = g.dropna(subset=[col])
        if len(g) < 60:
            continue
        n = max(3, int(round(len(g) * n_frac)))
        s = g.sort_values(col)
        L, S = set(s.stock_id.head(n)), set(s.stock_id.tail(n))
        if prev_l:
            turn.append((len(L - prev_l) + len(S - prev_s)) / (2 * n))
        prev_l, prev_s = L, S
    m1, t1, n1 = stat(raw)
    m2, t2, _ = stat(neu)
    tau = np.mean(turn) * 100 if turn else np.nan
    print(f"  {label:<26}{tau:>6.1f}%{m1:>+10.4f}%{t1:>+7.2f}{m2:>+11.4f}%{t2:>+7.2f}{n1:>7}")


def main() -> int:
    import chip_score_vol_beta_control as vb  # noqa: F401
    sys.path.insert(0, str(ROOT / "scripts" / "research"))
    from importlib.machinery import SourceFileLoader
    m = SourceFileLoader("m", str(ROOT / "scripts/research/chip_score_vol_beta_control.py")).load_module()
    d = m.add_risk(pd.read_pickle(m.PANEL)).dropna(subset=["vol60"])
    d["gap"] = d.nx_open / d.close - 1
    d = d[d.trade_date >= "2024-06-15"].copy()          # 分散表起點 + PIT 緩衝
    print(f"面板 {len(d):,} stock-day · {d.trade_date.nunique()} 日 · "
          f"{d.trade_date.min()}~{d.trade_date.max()}")

    disp = dispersion_factors()
    rev = revenue_factors()
    d = attach(d, disp, ["big_pct", "big_chg1", "big_chg4", "ret_pct",
                         "ret_chg1", "holder_chg1", "big_people_chg1"])
    d = attach(d, rev, ["yoy", "mom", "yoy_accel"])
    print(f"  股權分散覆蓋 {d.big_pct.notna().mean()*100:.0f}%　"
          f"月營收覆蓋 {d.yoy.notna().mean()*100:.0f}%\n")
    d["oc_n"] = neutral_resid(d, "oc")

    # 一律轉成「越大越偏空」：大戶增加/營收成長 → 偏多 → 取負號
    d["f_big_chg1"] = -d.big_chg1
    d["f_big_chg4"] = -d.big_chg4
    d["f_big_pct"] = -d.big_pct
    d["f_ret_chg1"] = d.ret_chg1                 # 散戶增加＝偏空
    d["f_holder"] = d.holder_chg1                # 股東人數增加＝分散＝偏空
    d["f_bigppl"] = -d.big_people_chg1
    d["f_yoy"] = -d.yoy
    d["f_mom"] = -d.mom
    d["f_accel"] = -d.yoy_accel
    dead = lambda s: np.where(np.abs(s) >= 0.1, s, 0.0)  # noqa: E731
    d["f_zp"] = dead(d.zp)                       # 對照組

    print(f"{'因子':<28}{'換手':>6}{'原始價差':>11}{'t':>7}{'雙中性後':>11}{'t':>7}{'日數':>7}")
    for col, lab in (
        ("f_zp", "【對照】zp 借券水位"),
        ("f_big_chg1", "大戶持股 週變化"),
        ("f_big_chg4", "大戶持股 4週變化"),
        ("f_big_pct", "大戶持股 水位"),
        ("f_ret_chg1", "散戶持股 週變化"),
        ("f_holder", "股東人數 週變化"),
        ("f_bigppl", "大戶人數 週變化"),
        ("f_yoy", "月營收 YoY"),
        ("f_mom", "月營收 MoM"),
        ("f_accel", "月營收 YoY 加速度"),
    ):
        test(d, col, lab)
    print("\n『雙中性後』＝逐日對 vol60 與 gap 迴歸取殘差再排序。")
    print("本輪同時測 10 個因子，多重檢定會膨脹顯著性：全部列出，不事後挑選。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
