#!/usr/bin/env python3
"""資券籌碼在「日頻 · 單一個股」尺度的預測力檢定 —— 三個 null 結果。

**問題**：T 日收盤後的融資融券／借券籌碼，能不能判斷 T+1 日漲跌？

**結論（2026-08-20 · 全市場 234 個交易日）**：不能，而且不是判讀技巧問題。
訊號的真實傾斜約 0.13%/日，但單一個股的隔日離散是 3.13%——**離散是傾斜的
37 倍**；而每日換手成本 1.17%/日 又是毛利的 9 倍。同一個訊號在月頻橫斷面上
有 t = −3.32（Cohen-Diether-Malloy DOUT），在日頻單股上實測 t = −0.09。

**PIT 紀律**：融資融券與借券餘額於 T 日盤後約 21:00 發布，最早只能在 T+1
開盤動作。故 ``close(T)→close(T+1)`` 是**偷看**口徑（隔夜跳空抓不到），
``open(T+1)→close(T+1)`` 才是可執行的。三個檢定一律兩種都報。

三個檢定
--------
``--test spread``
    6 個訊號各自做十分位多空，兩種報酬口徑。發現 Δ借券賣出餘額的效果幾乎
    全在隔夜跳空裡（cc t=−6.32 → oc t=−1.89）；DTC 兩口徑正負號相反（雜訊
    的典型徵兆）；CDM DOUT 在日頻是零。

``--test control``
    對唯一在可執行口徑下顯著的 Δ融資餘額（t=5.52）做控制檢定。控制當日報酬
    後 **t 掉到 1.03、只剩 19% 的效果**——它 81% 是「當天漲跌」的代理，不是
    籌碼資訊。與 chip-macro 舊結論一致（margin_bal_z60 約 70% 是價格動能代理）。

``--test score``
    把 5 個方向明確的券訊號做成一致性評分，看「訊號全部指同一邊」時的基準率。
    一致偏空 6,793 次中 44.1% 隔日仍上漲、其中 400 次漲超過 +5%；一致偏多
    10,951 次中 47.5% 上漲。扣掉當日大盤後每日多空超額價差 +0.130%、t=2.16
    ——方向是對的，但沒過 t>=3.0 的多重檢定門檻，也遠低於成本。

⚠️ **一個事後的方法論選擇**：``--test score`` 把「當日無借券成交」算成偏多
訊號（空方零興趣）。這是可辯護的但很粗糙，且**未經預先登錄**；改成中性會讓
一致偏多組樣本少約三分之一。**偏多側的數字要打折看，偏空側不受影響。**

用法::

    PYTHONPATH=src .venv/bin/python scripts/research/chip_daily_horizon_null_test.py --test all
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

START = "2025-07-01"
MIN_CLOSE = 10
MIN_VOL20 = 300_000
# 手續費雙邊 0.1425%×2 ＋ 賣出證交稅 0.3%；多空兩腳每日全額換手 = ×2
ROUND_TRIP_COST = 0.001425 * 2 + 0.003


def load_panel(start: str = START) -> pd.DataFrame:
    conn = connect_ro()
    px = pd.read_sql_query(
        """SELECT stock_id, trade_date, open, close, volume FROM stock_daily_bars
            WHERE source='finmind' AND trade_date>=? AND close>0 AND volume>0""",
        conn, params=(start,),
    )
    si = pd.read_sql_query(
        """SELECT stock_id, trade_date, sbl_balance, sbl_next_limit,
                  short_balance, short_limit
             FROM stock_short_interest_daily WHERE trade_date>=?""",
        conn, params=(start,),
    )
    # ⚠️ stock_margin_daily 同一 stock-day 可能同時有 finmind 與 twse_mi_margn 兩列
    # （2026-06 起兩來源重疊）。不去重會產生重複列、把樣本灌大並污染統計量。
    # 兩來源已逐欄對帳一致，優先取涵蓋全市場的 twse_mi_margn。
    mg = pd.read_sql_query(
        """SELECT stock_id, trade_date, margin_balance, mg_short FROM (
               SELECT stock_id, trade_date, margin_balance,
                      short_balance AS mg_short,
                      ROW_NUMBER() OVER (
                          PARTITION BY stock_id, trade_date
                          ORDER BY CASE source WHEN 'twse_mi_margn' THEN 0 ELSE 1 END
                      ) AS rn
                 FROM stock_margin_daily WHERE trade_date>=?
           ) WHERE rn=1""",
        conn, params=(start,),
    )
    fee = pd.read_sql_query(
        """SELECT stock_id, trade_date, fee_rate_vw FROM stock_sbl_fee_daily
            WHERE deal_type='ALL' AND trade_date>=?""",
        conn, params=(start,),
    )
    d = (
        px.merge(si, on=["stock_id", "trade_date"])
        .merge(mg, on=["stock_id", "trade_date"], how="left")
        .merge(fee, on=["stock_id", "trade_date"], how="left")
        .sort_values(["stock_id", "trade_date"])
        .copy()
    )
    g = d.groupby("stock_id", group_keys=False)
    d["vol20"] = g.volume.transform(lambda s: s.rolling(20, min_periods=10).mean())
    # 融券限額 = 已發行股份的 25%（2330 實測比值 0.2498~0.2500）。
    # ⚠️ 停止融券的標的（處置股／警示股，如 2208 台船、6443 元晶、6919 康霈）
    # 融券限額為 0，直接相除會得到 inf，而 inf 在分位排序會排到最高、被誤判成
    # 偏空訊號。實測 2026-08-18 有 8/442 檔（1.8%）踩到，一律排除。
    d["shares"] = (d.short_limit * 4).replace(0, np.nan)
    d["sbl_pct"] = d.sbl_balance / d.shares
    d["util"] = d.sbl_balance / (d.sbl_balance + d.sbl_next_limit)
    d["ret_t"] = g.close.pct_change()
    d["d_sbl"] = g.sbl_balance.diff()
    d["d_margin"] = g.margin_balance.diff()
    d["d_util"] = g.util.diff()
    d["d_short"] = g.short_balance.diff()
    d["d_fee"] = g.fee_rate_vw.diff()
    d["fee_med20"] = g.fee_rate_vw.transform(
        lambda s: s.rolling(20, min_periods=5).median()
    )
    d["pct_rank"] = g.sbl_pct.transform(
        lambda s: s.rolling(243, min_periods=60).rank(pct=True)
    )
    d["fwd_cc"] = g.close.shift(-1) / d.close - 1          # 偷看
    d["fwd_oc"] = g.close.shift(-1) / g.open.shift(-1) - 1  # 可執行
    dup = d.duplicated(subset=["stock_id", "trade_date"]).sum()
    if dup:
        raise RuntimeError(
            f"panel 有 {dup:,} 筆重複的 stock-day——多半是某張表新增了第二個 source。"
            "先去重再跑，否則統計量會被污染。"
        )
    d = d[(d.close >= MIN_CLOSE) & (d.vol20 > MIN_VOL20) & d.shares.notna()]
    return d[np.isfinite(d.sbl_pct)].copy()


def _decile_spread(x: pd.DataFrame, col: str, ret: str, n: int = 10) -> float:
    if len(x) < 50:
        return np.nan
    q = pd.qcut(x[col].rank(method="first"), n, labels=False, duplicates="drop")
    return x[ret][q == n - 1].mean() - x[ret][q == 0].mean()


def _tstat(s: pd.Series) -> float:
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s)))


def test_spread(d: pd.DataFrame) -> None:
    d = d.copy()
    d["sig_dsbl"] = d.d_sbl / d.vol20
    d["sig_sbl"] = d.sbl_balance / d.vol20
    d["sig_dmg"] = d.d_margin * 1000 / d.vol20
    d["sig_srat"] = d.mg_short / d.margin_balance.replace(0, np.nan)
    d["sig_fee"] = d.fee_rate_vw
    d["sig_dout"] = ((d.d_fee > 0) & (d.d_sbl > 0)).astype(float)
    sigs = [
        ("Δ借券賣出餘額÷20日均量", "sig_dsbl"),
        ("借券賣出餘額÷均量 (DTC)", "sig_sbl"),
        ("Δ融資餘額÷20日均量", "sig_dmg"),
        ("券資比 融券/融資", "sig_srat"),
        ("借券費率水位", "sig_fee"),
        ("CDM DOUT (費率↑且量↑)", "sig_dout"),
    ]
    print(f"\n{'訊號':<26}{'口徑':<22}{'D10−D1 日均':>12}{'t':>8}{'n日':>6}")
    print("-" * 76)
    for label, col in sigs:
        for ret, rname in (("fwd_cc", "close→close 偷看"), ("fwd_oc", "open→close 可執行")):
            sub = d.dropna(subset=[col, ret])
            if col == "sig_dout":
                sp = sub.groupby("trade_date").apply(
                    lambda x: x[x[col] == 1][ret].mean() - x[x[col] == 0][ret].mean(),
                    include_groups=False,
                ).dropna()
            else:
                sp = sub.groupby("trade_date").apply(
                    lambda x: _decile_spread(x, col, ret), include_groups=False
                ).dropna()
            if len(sp) < 30:
                continue
            print(f"{label:<26}{rname:<22}{sp.mean() * 100:>11.3f}%{_tstat(sp):>8.2f}{len(sp):>6}")


def test_control(d: pd.DataFrame) -> None:
    d = d.copy()
    d["sig"] = d.d_margin * 1000 / d.vol20
    d = d.dropna(subset=["sig", "fwd_oc", "ret_t"])
    sp = d.groupby("trade_date").apply(
        lambda x: _decile_spread(x, "sig", "fwd_oc"), include_groups=False
    ).dropna()
    print(f"\nA. 未控制              Δ融資 D10−D1 = {sp.mean() * 100:+.3f}%/日  t={_tstat(sp):.2f}")

    def bucketed(x):
        x = x.copy()
        x["rb"] = pd.qcut(x.ret_t.rank(method="first"), 5, labels=False, duplicates="drop")
        vals = [_decile_spread(s, "sig", "fwd_oc") for _, s in x.groupby("rb")]
        vals = [v for v in vals if not np.isnan(v)]
        return np.mean(vals) if vals else np.nan

    sp2 = d.groupby("trade_date").apply(bucketed, include_groups=False).dropna()
    print(f"B. 控制當日報酬        Δ融資 D10−D1 = {sp2.mean() * 100:+.3f}%/日  t={_tstat(sp2):.2f}")
    print(f"   → 控制後只保留 {sp2.mean() / sp.mean() * 100:.0f}% 的原始效果")

    sp3 = d.groupby("trade_date").apply(
        lambda x: _decile_spread(x, "ret_t", "fwd_oc"), include_groups=False
    ).dropna()
    print(f"C. 對照：當日報酬本身   D10−D1 = {sp3.mean() * 100:+.3f}%/日  t={_tstat(sp3):.2f}")
    print(f"   Δ融資 與 當日報酬 相關 = {d.sig.corr(d.ret_t):.3f}")
    print(f"\nD. 扣成本（多空兩腳每日全換手 ≈ {ROUND_TRIP_COST * 2 * 100:.2f}%/日）")
    for nm, s in (("未控制", sp), ("控制後", sp2)):
        net = s.mean() - ROUND_TRIP_COST * 2
        print(f"   {nm:<8}毛 {s.mean() * 100:+.3f}%/日 → 淨 {net * 100:+.3f}%/日 "
              f"({'仍為正' if net > 0 else '轉負'})")


def build_score(d: pd.DataFrame) -> pd.DataFrame:
    """5 個方向明確的券訊號，各 +1 空 / −1 多。"""
    d = d.copy()

    def sgn(bear, bull):
        return np.where(bear, 1, np.where(bull, -1, 0))

    d["S1"] = sgn(d.d_sbl > 0, d.d_sbl < 0)                       # Δ借券賣出餘額
    d["S2"] = sgn(d.pct_rank >= 0.8, d.pct_rank <= 0.2)            # 佔股本近一年分位
    d["S3"] = sgn(d.d_util > 0, d.d_util < 0)                      # 券源使用率變化
    d["S4"] = sgn(d.d_short > 0, d.d_short < 0)                    # 融券餘額變化
    # ⚠️ 事後選擇：當日無借券成交（NaN）算偏多。見模組 docstring 警語。
    d["S5"] = sgn(d.fee_rate_vw > d.fee_med20,
                  d.fee_rate_vw.isna() | (d.fee_rate_vw < d.fee_med20))
    cols = ["S1", "S2", "S3", "S4", "S5"]
    d["bear"] = (d[cols] == 1).sum(axis=1)
    d["bull"] = (d[cols] == -1).sum(axis=1)
    return d


def test_score(d: pd.DataFrame) -> None:
    d = build_score(d).dropna(subset=["fwd_cc"]).copy()
    d["r"] = d.fwd_cc * 100
    mkt = d.groupby("trade_date")["r"].mean().rename("mkt")
    d = d.join(mkt, on="trade_date")
    d["excess"] = d.r - d.mkt
    A = d[(d.bear >= 4) & (d.bull == 0)]
    B = d[(d.bull >= 4) & (d.bear == 0)]
    print(f"\n{'分組':<28}{'n':>8}{'隔日平均':>10}{'上漲比例':>10}{'σ':>8}{'超額平均':>10}")
    print("-" * 76)
    for nm, x in (("一致偏空 bear≥4 bull=0", A), ("一致偏多 bull≥4 bear=0", B)):
        print(f"{nm:<28}{len(x):>8,}{x.r.mean():>9.3f}%{(x.r > 0).mean() * 100:>9.1f}%"
              f"{x.r.std():>7.2f}%{x.excess.mean():>9.3f}%")
    print(f"\n訊號完全反向的次數：")
    print(f"  一致偏空卻隔日漲 >+5%：{(A.r > 5).sum():,} 次（{(A.r > 5).mean() * 100:.1f}%）")
    print(f"  一致偏多卻隔日跌 >−5%：{(B.r < -5).sum():,} 次（{(B.r < -5).mean() * 100:.1f}%）")
    sp = d.groupby("trade_date").apply(
        lambda x: x[(x.bull >= 4) & (x.bear == 0)].excess.mean()
        - x[(x.bear >= 4) & (x.bull == 0)].excess.mean(),
        include_groups=False,
    ).dropna()
    tilt = sp.mean()
    print(f"\n每日（一致偏多 − 一致偏空）超額價差 = {tilt:+.3f}%/日  t={_tstat(sp):.2f}  n={len(sp)} 日")
    print(f"個股離散 σ = {A.r.std():.2f}%  →  離散/傾斜 = {A.r.std() / abs(tilt):.0f} 倍")
    print(f"成本 {ROUND_TRIP_COST * 2 * 100:.2f}%/日 = 毛利的 {ROUND_TRIP_COST * 2 / abs(tilt) * 100:.0f} 倍")


def test_streak(d: pd.DataFrame) -> None:
    """連續三天訊號一致、卻連續三天超額報酬反向的機率 —— 與無訊號基準對照。

    用超額報酬（個股當日報酬 − 當日全宇宙平均），避免把大盤漲跌算進訊號帳上。
    基準不是 12.5%：超額報酬右偏、中位數為負，實測 56.0% 的個股-日超額為負，
    故連三天為負的銅板機率是 0.56^3 = 17.5%、連三天為正是 0.44^3 = 8.5%。
    """
    d = d.dropna(subset=["fwd_cc"]).copy()
    d["r"] = d.fwd_cc
    d["ex"] = d.r - d.groupby("trade_date").r.transform("mean")
    d = d.sort_values(["stock_id", "trade_date"])
    g = d.groupby("stock_id", group_keys=False)
    d["BULL"] = ((d.bull >= 3) & (d.bear <= 1)).astype(int)
    d["BEAR"] = ((d.bear >= 3) & (d.bull <= 1)).astype(int)
    for k in (1, 2):
        for c in ("BULL", "BEAR", "ex", "trade_date"):
            d[f"{c}_{k}"] = g[c].shift(-k)
    d = d[d.trade_date_2.notna()].copy()

    E = np.vstack([d.ex.values, d.ex_1.values, d.ex_2.values])
    d["neg3"] = (E < 0).all(axis=0)
    d["pos3"] = (E > 0).all(axis=0)
    d["cum"] = np.nansum(E, axis=0) * 100
    d["S_BULL"] = d.BULL.astype(bool) & d.BULL_1.astype(bool) & d.BULL_2.astype(bool)
    d["S_BEAR"] = d.BEAR.astype(bool) & d.BEAR_1.astype(bool) & d.BEAR_2.astype(bool)

    p_neg = (d.ex < 0).mean()
    print(f"\n三日窗總數 {len(d):,}；單日超額為負的機率 {p_neg * 100:.1f}%"
          f" → 銅板基準 連三負 {p_neg ** 3 * 100:.2f}% / 連三正 {(1 - p_neg) ** 3 * 100:.2f}%")

    for label, sig, out, thr_col, thrs in (
        ("看多三連 → 連三天超額為負", "S_BULL", "neg3", "cum", (-5, -10, -15)),
        ("看空三連 → 連三天超額為正", "S_BEAR", "pos3", "cum", (5, 10, 15)),
    ):
        m_ = d[d[sig]]
        print(f"\n【{label}】訊號 {len(m_):,} 次（{len(m_) / len(d) * 100:.1f}%）")
        print(f"  連三天反向：訊號組 {m_[out].mean() * 100:.2f}%  vs 基準 {d[out].mean() * 100:.2f}%"
              f"  lift {m_[out].mean() / d[out].mean():.2f}×")
        for th in thrs:
            f = (lambda x: x < th) if th < 0 else (lambda x: x > th)
            print(f"  三日累計超額反向 >{abs(th):>2}%：訊號組 {f(m_[thr_col]).mean() * 100:5.2f}%"
                  f"  vs 基準 {f(d[thr_col]).mean() * 100:5.2f}%  （{f(m_[thr_col]).sum():,} 次）")

    print("\n=== 逐日比率差的 t 檢定（已處理同日橫斷面相關）===")

    def daily(sigcol, outcol):
        def rate(x):
            a, b = x[x[sigcol]], x[~x[sigcol]]
            return np.nan if len(a) < 10 or len(b) < 10 else a[outcol].mean() - b[outcol].mean()
        s_ = d.groupby("trade_date").apply(rate, include_groups=False).dropna()
        return s_.mean(), _tstat(s_), len(s_)

    d["big_neg10"] = d.cum < -10
    d["big_pos10"] = d.cum > 10
    for lab, sig, pairs in (
        ("看多三連", "S_BULL", (("連三天超額為負", "neg3"), ("三日累計超額 < −10%", "big_neg10"))),
        ("看空三連", "S_BEAR", (("連三天超額為正", "pos3"), ("三日累計超額 > +10%", "big_pos10"))),
    ):
        print(f"  {lab}")
        for nm2, col in pairs:
            mu, t, n = daily(sig, col)
            print(f"    {nm2:<22}{mu * 100:+6.2f} 個百分點  t={t:+.2f}  n={n} 日")
        sp = d.groupby("trade_date").apply(
            lambda x: x[x[sig]].cum.mean() - x[~x[sig]].cum.mean() if x[sig].sum() >= 10 else np.nan,
            include_groups=False,
        ).dropna()
        print(f"    {'三日累計超額本身':<22}{sp.mean():+6.3f}%          t={_tstat(sp):+.2f}")
    print("\n⚠️ 重疊窗（同一檔連續起算日）的序列相關未處理，真實 |t| 應再打折。")


def list_cases(d: pd.DataFrame, day: str, names: dict[str, str] | None = None) -> None:
    """列出指定日「訊號一致卻隔日反向」的個股（先前 8/18 兩張表的來源）。"""
    d = build_score(d)
    s = d[d.trade_date == day].dropna(subset=["fwd_cc"]).copy()
    if s.empty:
        print(f"{day} 無可評分標的")
        return
    s["名稱"] = s.stock_id.map(names or {}).fillna("")
    s["隔日%"] = (s.fwd_cc * 100).round(2)
    s["佔股本%"] = (s.sbl_pct * 100).round(2)
    s["使用率%"] = (s.util * 100).round(1)
    s["費率"] = s.fee_rate_vw.round(2)
    s["借券餘額張"] = (s.sbl_balance / 1000).round(0)
    cols = ["stock_id", "名稱", "bear", "bull", "借券餘額張", "佔股本%", "使用率%", "費率", "隔日%"]
    print(f"\n{day} 可評分標的 {len(s):,} 檔")
    for title, sub, asc, wrong in (
        ("訊號一致偏空（bear>=3, bull=0）→ 隔日反而漲",
         s[(s.bear >= 3) & (s.bull == 0)], False, lambda x: x["隔日%"] > 0),
        ("訊號一致偏多（bull>=3, bear<=1）→ 隔日反而跌",
         s[(s.bull >= 3) & (s.bear <= 1)], True, lambda x: x["隔日%"] < 0),
    ):
        if sub.empty:
            print(f"\n【{title}】無符合標的")
            continue
        hit = sub[wrong(sub)]
        print(f"\n【{title}】")
        print(f"  符合訊號一致的 {len(sub)} 檔，其中反向 {len(hit)} 檔"
              f"（{len(hit) / max(len(sub), 1) * 100:.0f}%）· 該組平均 {sub['隔日%'].mean():+.2f}%")
        print(sub.sort_values("隔日%", ascending=asc).head(12)[cols].to_string(index=False))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", choices=["spread", "control", "score", "streak", "all"],
                    default="all")
    ap.add_argument("--start", default=START)
    ap.add_argument("--dump-panel", type=Path, default=None, help="存 pickle 供後續案例查詢")
    ap.add_argument("--cases", metavar="YYYY-MM-DD", default=None,
                    help="列出該日『訊號一致卻隔日反向』的個股")
    ap.add_argument("--names", type=Path, default=None,
                    help="代號→名稱 JSON（可選；沒有就只顯示代號）")
    args = ap.parse_args()

    d = load_panel(args.start)
    print(f"樣本：{len(d):,} 個「個股-日」· {d.stock_id.nunique():,} 檔 · "
          f"{d.trade_date.nunique()} 個交易日 · {d.trade_date.min()}~{d.trade_date.max()}")
    if args.dump_panel:
        build_score(d).to_pickle(args.dump_panel)
        print(f"panel → {args.dump_panel}")
    if args.cases:
        names = json.loads(args.names.read_text(encoding="utf-8")) if args.names else None
        list_cases(d, args.cases, names)
        return 0
    if args.test in ("spread", "all"):
        print("\n" + "=" * 76 + "\n【檢定 1】各訊號十分位多空 → 隔日報酬\n" + "=" * 76)
        test_spread(d)
    if args.test in ("control", "all"):
        print("\n" + "=" * 76 + "\n【檢定 2】Δ融資餘額是不是只是當日報酬的代理\n" + "=" * 76)
        test_control(d)
    if args.test in ("score", "all"):
        print("\n" + "=" * 76 + "\n【檢定 3】訊號一致性評分的基準率\n" + "=" * 76)
        test_score(d)
    if args.test in ("streak", "all"):
        print("\n" + "=" * 76 + "\n【檢定 4】連續三天訊號一致卻三天反向的機率\n" + "=" * 76)
        test_streak(build_score(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
