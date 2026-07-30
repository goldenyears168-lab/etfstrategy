"""信用交易 x 微結構籌碼三訊號研究:借券賣出(SBL) / 當沖比重 / 融資使用率。

維度定位
    L2 籌碼核心 ~ L3 微結構。三者都是「散戶/機構部位代理」,與價格動能高度共線,
    屬 regime 濾網/部位條件,非獨立 alpha。對照 champion = 外資台指期 positioning
    (fut_foreign_oi z60>0, chip x bull OOS +1.79,領先)。

實作狀態(誠實區分,見 report short_daytrade.md)
    * REAL(本地可跑): 市場層 券資比 = short_bal / margin_bal(panel.parquet,2018->2026,
      無融資限額)。這是本家族「信用擁擠」中唯一 local 就能算的訊號;拿它當 anchor,
      跑 IS/OOS IC + Sharpe + permutation(vs 同曝險隨機)+ 與 champion 相關。
      注意:券資比走的是「融券(散戶)」而非「借券賣出(機構)」口徑,結果僅為同家族 proxy。
    * SCAFFOLD(需接外部資料,import 不報錯,呼叫才抓): 三個 headline 訊號本地都不可算:
        - 借券賣出 SBL      -> FinMind TaiwanDailyShortSaleBalances(SBLShortSalesCurrentDayBalance/Quota)
                              (本地 stock_lending_daily = 總借券餘額,方向中性含避險套利,是陷阱,勿用)
        - 當沖比重 daytrade -> FinMind TaiwanStockDayTrading + TaiwanStockPrice 當分母
                              (本地 stock_daytrade_daily: daytrade_volume 100% NULL,
                               ratio_pct 僅 13 個交易日,實質空表)
        - 融資使用率 util   -> FinMind TaiwanStockMarginPurchaseShortSale
                              (MarginPurchaseTodayBalance / MarginPurchaseLimit;
                               本地 stock_margin_daily 無 limit 欄 -> 無法算)

資料源
    本地: data/research/chip_macro/panel.parquet(市場層日頻)
    外部: FinMind base = https://api.finmindtrade.com/api/v4/data(見 .cursor/rules/finmind.mdc)
          官方備援: TWSE MI_MARGN(信用交易統計含限額)、TWSE/TAIFEX rwd endpoints

如何跑
    cd /Users/jackm4/Documents/ETF/股票研究 && source .venv/bin/activate
    python scripts/research/dashboard/short_daytrade_study.py            # 跑 anchor(券資比)實測
    python scripts/research/dashboard/short_daytrade_study.py --scaffold # 只印外接資料檢查清單

方法論對齊(專案凍結門檻紀律)
    方向由 IN-SAMPLE IC 符號固定(不偷看 OOS);IS/OOS 70/30 時間分割;permutation vs 同曝險
    隨機;regime-conditioning(多頭 ix_close>MA200 內分開看);報 corr_vs_champion 判斷是否
    只是動能/champion 重述。先假設無效,一次性誠實 OOS,不網格搜尋(呼應 980T/adopted-44 過擬合史)。

*** 本檔為研究方法論工具,輸出非投資建議。***
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
OUT = ROOT / "reports" / "research" / "dashboard-completeness"
COST_BPS = 4.0
ANN = np.sqrt(252)
RNG = np.random.default_rng(7)


# ------------------------- shared eval helpers -------------------------
def rz(s: pd.Series, w: int) -> pd.Series:
    """rolling z-score (min_periods=w so no partial-window peeking)."""
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def deflated_sharpe(rets, n_trials: int, sr_trials_std: float | None = None) -> float:
    """Deflated Sharpe Ratio prob (Bailey & Lopez de Prado 2014).

    Returns P(true SR>0) after correcting for #trials + non-normal returns.
    <0.95 = not significant after multiple-testing/tail correction.
    """
    from scipy.stats import norm, skew, kurtosis
    r = pd.Series(rets).dropna().values
    T = len(r)
    if T < 30 or r.std() == 0:
        return np.nan
    sr = r.mean() / r.std()                      # per-period (not annualized)
    g3 = float(skew(r)); g4 = float(kurtosis(r, fisher=False))
    # expected max SR under n_trials null (variance of trial SRs)
    v = sr_trials_std if sr_trials_std and sr_trials_std > 0 else (1.0 / np.sqrt(T))
    e = 0.5772156649
    z = norm.ppf(1 - 1.0 / max(n_trials, 2)) if n_trials > 1 else 0.0
    z2 = norm.ppf(1 - 1.0 / max(n_trials, 2) * np.e) if n_trials > 1 else 0.0
    sr0 = v * ((1 - e) * z + e * z2)             # expected max SR under null
    num = (sr - sr0) * np.sqrt(T - 1)
    den = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr ** 2)
    if den <= 0:
        return np.nan
    return float(norm.cdf(num / den))


def long_flat(bull: pd.Series, day_ret: pd.Series, cost: float = COST_BPS) -> pd.Series:
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def perm_p(bull: pd.Series, day_ret: pd.Series, mask: pd.Series, n: int = 2000):
    """permutation vs same-exposure random: keep #long-days fixed, shuffle which days."""
    dr = day_ret[mask].reset_index(drop=True)
    pos = pd.Series(bull, index=day_ret.index)[mask].astype(float).reset_index(drop=True)
    v = dr.notna()
    dr, pos = dr[v], pos[v]
    actual = sharpe(pos.values * dr.values)
    k, N = int(pos.sum()), len(pos)
    if k == 0 or k == N or N == 0:
        return actual, np.nan
    drv = dr.values
    null = np.array([
        sharpe(np.isin(np.arange(N), RNG.choice(N, k, False)).astype(float) * drv)
        for _ in range(n)
    ])
    return actual, float((null >= actual).mean())


def directed_signal(value: pd.Series, day_ret: pd.Series, ismask: pd.Series):
    """Fix direction from IN-SAMPLE IC sign only. Return (bull, direction, ic_is)."""
    d = pd.DataFrame({"v": value, "f": day_ret}).dropna()
    dm = ismask.reindex(d.index).fillna(False)
    ic_is = d[dm]["v"].corr(d[dm]["f"]) if dm.sum() > 20 else np.nan
    direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
    bull = (direction * value) > 0
    return bull, float(direction), (float(ic_is) if pd.notna(ic_is) else np.nan)


# ------------------------- REAL anchor: 市場券資比 -------------------------
def run_anchor():
    """市場層 券資比 (short_bal/margin_bal) 的一次性誠實 IS/OOS 研究。

    這是「信用擁擠」家族中 local 唯一能算的訊號,拿來給 report 錨定真實數字。
    口徑=融券(散戶),非借券賣出(機構),僅同家族 proxy。
    """
    if not PANEL.exists():
        print(f"[anchor] panel not found: {PANEL}")
        return None
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)   # 隔日開->收,避免前視
    n = len(p)
    is_cut = int(n * 0.7)
    ismask = pd.Series(p.index < is_cut, index=p.index)
    oos = pd.Series(p.index >= is_cut, index=p.index)

    # champion reference (foreign futures positioning)
    champ_bull = rz(p["fut_foreign_oi"], 60) > 0
    champ_ret = long_flat(champ_bull, day_ret)

    # regime: ix_close > MA200 (up-trend gate)
    ma200 = p["ix_close"].rolling(200, min_periods=200).mean()
    bull_regime = (p["ix_close"] > ma200)

    signals = {
        # 券資比 z60:空方擁擠代理。方向由 IS IC 固定。
        "short_margin_ratio_z60": rz(p["short_bal"] / p["margin_bal"], 60),
        # 融券水位本身 z60(對照:研究結論說單獨無效)
        "short_bal_z60": rz(p["short_bal"], 60),
        # 融資餘額 z60(落後/動能代理,對照組)
        "margin_bal_z60": rz(p["margin_bal"], 60),
    }

    print("=" * 74)
    print("REAL anchor study — 市場層信用擁擠 proxy (panel.parquet, 2018->2026)")
    print(f"  n={n}  IS={is_cut} ({p['date'].iloc[0]}~{p['date'].iloc[is_cut-1]})"
          f"  OOS={n-is_cut} ({p['date'].iloc[is_cut]}~{p['date'].iloc[-1]})")
    bh_oos = sharpe(day_ret[oos])
    print(f"  B&H OOS Sharpe = {bh_oos:+.2f} ; champion(fut_foreign_oi z60>0) OOS = "
          f"{sharpe(champ_ret[oos]):+.2f}")
    print("-" * 74)

    rows = []
    for name, v in signals.items():
        v = pd.Series(v, index=p.index)
        bull, direction, ic_is = directed_signal(v, day_ret, ismask)
        r = long_flat(bull, day_ret)
        oos_shp = sharpe(r[oos])
        _, pv = perm_p(bull, day_ret, oos)
        corr_champ = pd.DataFrame({"a": r, "b": champ_ret}).dropna().corr().iloc[0, 1]
        # regime split: OOS bull-regime only
        oos_bull = oos & bull_regime.fillna(False)
        oos_bear = oos & (~bull_regime.fillna(True))
        shp_bull = sharpe(r[oos_bull])
        shp_bear = sharpe(r[oos_bear])
        rows.append({
            "signal": name, "dir": int(direction), "IC_IS": ic_is,
            "OOS_Sharpe": oos_shp, "OOS_perm_p": pv,
            "corr_vs_champ": corr_champ,
            "OOS_bull_regime": shp_bull, "OOS_bear_regime": shp_bear,
            "exposure": float(bull.reindex(day_ret.index).fillna(False).mean()),
        })
    res = pd.DataFrame(rows)
    pd.set_option("display.float_format", lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x))
    print(res.to_string(index=False))
    print("-" * 74)
    print("讀法: OOS_perm_p 大(>0.1)= 贏不過同曝險隨機; corr_vs_champ 高 = 只是 champion/動能重述;")
    print("      bull>>bear = regime-conditioned(僅多頭有效)。三訊號皆預期為弱濾網,非獨立 alpha。")

    out_csv = OUT / "short_daytrade_anchor.csv"
    res.to_csv(out_csv, index=False)
    print(f"\n-> {out_csv}")
    return res


# ------------------------- REAL headline signals (FinMind, fetched) -------------------------
DATA = ROOT / "data" / "research" / "dashboard" / "short_daytrade_data.parquet"


def run_real():
    """三個 headline 訊號的一次性誠實 IS/OOS 研究,用 FinMind 真資料。

    借券賣出 SBL (機構做空,方向性) / 當沖比重 (散戶過熱) / 融資使用率 proxy。
    市場層日序列已由 fetch_short_daytrade_data.py 抓好存 parquet。
    """
    if not (DATA.exists() and PANEL.exists()):
        print(f"[real] missing data: {DATA.exists()=} {PANEL.exists()=}")
        return None
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    d = pd.read_parquet(DATA)
    p["date"] = p["date"].astype(str)
    d["date"] = d["date"].astype(str)
    m = p.merge(d, on="date", how="left").sort_values("date").reset_index(drop=True)

    day_ret = (m["ix_close"] / m["ix_open"] - 1.0).shift(-1)   # 隔日開->收,T+1,避免前視

    # champion + regime
    champ_bull = rz(m["fut_foreign_oi"], 60) > 0
    champ_ret = long_flat(champ_bull, day_ret)
    ma200 = m["ix_close"].rolling(200, min_periods=200).mean()
    bull_regime = (m["ix_close"] > ma200)

    # REAL signals (raw levels -> z60; 訊號延一日 T+1 使用,盤後公布)
    sbl_sir = (m["sbl_short_bal"] / m["sbl_quota"]).shift(1)      # SBL 使用率(額度制)
    sbl_lvl = m["sbl_short_bal"].shift(1)                         # SBL 空單水位
    dt_ratio = (m["daytrade_val"] / m["ix_money"]).shift(1)       # 當沖成交值/市場成交值
    margin_util = m["tot_margin_bal"].shift(1)                    # 融資餘額(無限額→水位proxy)

    signals = {
        "sbl_short_util_z60": rz(sbl_sir, 60),      # 借券賣出額度使用率 (機構做空擁擠)
        "sbl_short_bal_z60": rz(sbl_lvl, 60),       # 借券賣出空單水位
        "daytrade_ratio_z60": rz(dt_ratio, 60),     # 當沖比重 (散戶過熱)
        "margin_util_z60": rz(margin_util, 60),     # 融資使用率 proxy
    }
    n_trials = len(signals)

    # eval window = rows where at least sbl/daytrade real data exists
    have = m["sbl_short_bal"].notna() | m["daytrade_val"].notna()
    eval_idx = m.index[have]
    lo, hi = eval_idx.min(), eval_idx.max()
    sub = (m.index >= lo) & (m.index <= hi)
    n_eval = int(sub.sum())
    is_cut = lo + int(n_eval * 0.7)
    ismask = pd.Series((m.index >= lo) & (m.index < is_cut), index=m.index)
    oos = pd.Series((m.index >= is_cut) & (m.index <= hi), index=m.index)

    print("=" * 78)
    print("REAL headline study — 借券賣出/當沖/融資使用率 (FinMind, market-level)")
    print(f"  eval n={n_eval} ({m['date'].iloc[lo]}~{m['date'].iloc[hi]})")
    print(f"  IS={int(is_cut-lo)} ({m['date'].iloc[lo]}~{m['date'].iloc[is_cut-1]})"
          f"  OOS={int(hi-is_cut+1)} ({m['date'].iloc[is_cut]}~{m['date'].iloc[hi]})")
    print(f"  B&H OOS Sharpe={sharpe(day_ret[oos]):+.2f} ; "
          f"champion(fut_foreign_oi z60>0) OOS={sharpe(champ_ret[oos]):+.2f}")
    print("-" * 78)

    rows = []
    for name, v in signals.items():
        v = pd.Series(v, index=m.index)
        bull, direction, ic_is = directed_signal(v, day_ret, ismask)
        r = long_flat(bull, day_ret)
        # OOS IC (raw signal vs fwd ret, sign should match IS)
        dd = pd.DataFrame({"v": v, "f": day_ret}).dropna()
        om = oos.reindex(dd.index).fillna(False)
        ic_oos = dd[om]["v"].corr(dd[om]["f"]) if om.sum() > 20 else np.nan
        oos_shp = sharpe(r[oos])
        _, pv = perm_p(bull, day_ret, oos)
        dsr = deflated_sharpe(r[oos], n_trials)
        corr_champ = pd.DataFrame({"a": r, "b": champ_ret}).dropna().corr().iloc[0, 1]
        oos_bull = oos & bull_regime.fillna(False)
        oos_bear = oos & (~bull_regime.fillna(True))
        rows.append({
            "signal": name, "dir": int(direction),
            "IC_IS": ic_is, "IC_OOS": ic_oos,
            "OOS_Sharpe": oos_shp, "OOS_perm_p": pv, "OOS_DSR": dsr,
            "corr_champ": corr_champ,
            "OOS_bull": sharpe(r[oos_bull]), "OOS_bear": sharpe(r[oos_bear]),
            "expo": float(bull.reindex(day_ret.index).fillna(False).mean()),
        })
    res = pd.DataFrame(rows)
    pd.set_option("display.float_format",
                  lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x))
    print(res.to_string(index=False))
    print("-" * 78)
    print("讀法: OOS_perm_p>0.1=贏不過同曝險隨機; OOS_DSR<0.95=多重檢定後不顯著;")
    print("      corr_champ 高=只是 champion/動能重述; bull>>bear=僅多頭有效(regime濾網)。")
    out_csv = OUT / "short_daytrade_real.csv"
    res.to_csv(out_csv, index=False)
    print(f"\n-> {out_csv}")
    return res


# ------------------------- SCAFFOLD: 外接資料抓取 -------------------------
# SCAFFOLD: 需先同步 FinMind 三個 dataset,以下函式呼叫才會發網路請求;import 安全。
FINMIND_BASE = "https://api.finmindtrade.com/api/v4/data"


def _finmind_get(dataset: str, data_id: str | None, start: str, end: str,
                 token: str | None = None) -> pd.DataFrame:
    """通用 FinMind /data 抓取。token 從環境變數 FINMIND_TOKEN(見 finmind.mdc)。"""
    import os
    import requests
    params = {"dataset": dataset, "start_date": start, "end_date": end}
    if data_id:
        params["data_id"] = data_id
    headers = {}
    tok = token or os.environ.get("FINMIND_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = requests.get(FINMIND_BASE, params=params, headers=headers, timeout=60)
    r.raise_for_status()
    return pd.DataFrame(r.json().get("data", []))


def fetch_sbl_shortsale(data_id: str, start: str, end: str) -> pd.DataFrame:
    """借券賣出 SBL —— FinMind TaiwanDailyShortSaleBalances(信用額度總量管制餘額表)。

    directional 空方部位 = SBLShortSalesCurrentDayBalance(已賣出且未回補)。
    額度 = SBLShortSalesQuota。**勿用** TaiwanStockSecuritiesLending(=借券成交明細,
    含避險/套利/除息借券,方向中性 = 陷阱)。
    T 日餘額約 21:00 公布 -> 回測只可 T+1 用。
    訊號: sbl_sir = SBLShortSalesCurrentDayBalance / 流通股數,或
          sbl_ratio = 當日借券賣出金額 / 當日成交金額(需搭 TaiwanStockPrice)。方向=負。
    """
    df = _finmind_get("TaiwanDailyShortSaleBalances", data_id, start, end)
    # 期望欄位: date, stock_id, MarginShortSales*, SBLShortSalesCurrentDayBalance,
    #           SBLShortSalesQuota, ...(視 FinMind 版本)
    return df


def fetch_daytrade(data_id: str | None, start: str, end: str) -> pd.DataFrame:
    """當沖比重 —— FinMind TaiwanStockDayTrading(當日沖銷標的及成交量值,2014+)。

    欄位: date, stock_id, BuyAfterSale(盤前可當沖標記), Volume, BuyAmount, SellAmount。
    比重 = 當沖量 / 總量(分母取 TaiwanStockPrice 的 Trading_Volume),或金額版。
    **必須 rolling-normalize**: 2017-04 現股當沖稅減半(0.3%->0.15%)造成結構性跳升,
    原始 level 非平穩,直接用 = 前視 regime。方向=負(過熱代理)。盤後 ~21:30 補量值 -> T+1 用。
    """
    df = _finmind_get("TaiwanStockDayTrading", data_id, start, end)
    return df


def fetch_margin_with_limit(data_id: str, start: str, end: str) -> pd.DataFrame:
    """融資使用率 —— FinMind TaiwanStockMarginPurchaseShortSale(2001+,含限額)。

    使用率 = MarginPurchaseTodayBalance / MarginPurchaseLimit(融券版 = ShortSale*/ShortSaleLimit)。
    本地 stock_margin_daily 無 *Limit 欄 -> 這是啟用融資使用率的唯一路徑。
    官方對應 = TWSE MI_MARGN(selectType=MS 信用交易統計)。
    限額會被券商按股本/波動調整 -> 使用率有機械式跳動,非情緒,需注意。方向=極高反轉(contrarian)。
    """
    df = _finmind_get("TaiwanStockMarginPurchaseShortSale", data_id, start, end)
    return df


def build_signal_from_external(df: pd.DataFrame, value_col: str, norm_col: str | None,
                               direction_hint: int) -> pd.DataFrame:
    """把外部抓來的個股/市場資料整成 (date, raw, signal_z60) 供接 run_anchor 式評估。

    value_col: 分子(如 SBLShortSalesCurrentDayBalance / daytrade volume / margin balance)
    norm_col : 分母(流通股數 / 總量 / 限額);None 表示只做 rolling z60
    direction_hint 僅供人工核對,真正方向仍由 IS IC 決定(directed_signal)。
    輸出可 pivot 到市場層或逐檔,再丟進 directed_signal / perm_p。
    """
    if df.empty:
        return pd.DataFrame(columns=["date", "raw", "signal_z60"])
    g = df.copy()
    if norm_col and norm_col in g.columns:
        g["raw"] = g[value_col] / g[norm_col].replace(0, np.nan)
    else:
        g["raw"] = g[value_col]
    g = g.sort_values("date")
    g["signal_z60"] = rz(g["raw"], 60)
    return g[["date", "raw", "signal_z60"]]


def scaffold_report():
    print("SCAFFOLD — 三個 headline 訊號的外接資料清單(本地皆不可算):")
    print("  1) 借券賣出 SBL  : FinMind TaiwanDailyShortSaleBalances")
    print("       -> fetch_sbl_shortsale(data_id, start, end);用 SBLShortSalesCurrentDayBalance")
    print("       -> 陷阱: 本地 stock_lending_daily = 總借券餘額(方向中性),勿當空方訊號")
    print("  2) 當沖比重      : FinMind TaiwanStockDayTrading + TaiwanStockPrice(分母)")
    print("       -> fetch_daytrade(...);必 rolling-normalize(2017-04 稅改結構斷點)")
    print("       -> 本地 stock_daytrade_daily: daytrade_volume 100% NULL,ratio 僅 13 日,空表")
    print("  3) 融資使用率    : FinMind TaiwanStockMarginPurchaseShortSale(含 *Limit)")
    print("       -> fetch_margin_with_limit(...);使用率 = balance/limit")
    print("       -> 本地 stock_margin_daily 無 limit 欄,故無法算")
    print("  評估接口: build_signal_from_external(df,...) -> directed_signal / perm_p / long_flat")
    print("  三者皆盤後(15:00~21:30)公布 -> 回測務必 shift 到 T+1。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scaffold", action="store_true", help="只印外接資料清單")
    ap.add_argument("--anchor", action="store_true", help="只跑市場券資比 proxy anchor")
    args = ap.parse_args()
    if args.scaffold:
        scaffold_report()
        return
    if args.anchor:
        run_anchor()
        return
    run_real()          # default: REAL headline signals (FinMind)
    print()
    run_anchor()        # local proxy anchor for cross-check


if __name__ == "__main__":
    main()
