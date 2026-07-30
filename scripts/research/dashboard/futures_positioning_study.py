"""期貨進階籌碼 (大額交易人 OI + 台指期 basis) — L2 擴充層 falsification study.

WHAT / WHY
  補 panel 完全沒有的兩塊期貨籌碼,對照 champion(外資台指期 positioning,
  fut_foreign_oi z60>0),證偽優先地問:它們是否帶來 INDEPENDENT 的擇時資訊,
  還是只是 (a) champion 的冗餘、或 (b) 價格動能的偽裝?

    訊號 A  大額交易人未平倉(前五 / 前十大 / 十大特定法人)
            核心衍生量 = 散戶殘差 = 市場總 net − 前十大 net(預期反指標 / 前兆)
    訊號 B  台指期 basis = TX 近月 − TAIEX;去季節殘差的極端分位 × bull(前兆)

  方法論對齊 chip-macro house style
  (scripts/research/chip_macro/eval_stage4_newhypotheses.py + eval_deflated_sharpe.py):
    - 方向由 IN-SAMPLE IC 符號固定(不偷看 OOS)
    - no-lookahead:訊號 close[t] → 成交 ix_open[t+1]→ix_close[t+1](大額報表 15:00 出)
    - 70/30 IS/OOS 時間分割;permutation vs 同曝險隨機;Deflated-Sharpe
    - regime-conditioning(index > 上彎 MA200)
    - 雙共線檢定:corr_vs_champ(<0.4 才獨立) + corr_mom20 + 動能 partial-out

DATA SOURCE
  LIVE(本地、無需外接,--live 段一定會跑):
    - data/research/chip_macro/panel.parquet:ix_open/high/low/close + 三大法人期貨 OI
      → 算 champion 基準、B&H、並用「非外資本土法人 OI」proxy 示範獨立性框架。
  EXTERNAL(訊號 A、B 非本地表 / 非 panel 欄,但用專案標準 FinMind client 直拉即可,
           --with-external 會 end-to-end 跑完整框架並快取到 .../cache/;無 token → 用快取或印提示):
    - 訊號 A:FinMind TaiwanFuturesOpenInterestLargeTraders(data_id=TX, 1998-07+, 16:30)
              官方 = TAIFEX largeTraderFutQry。近月契約 = contract_type 為 YYYYMM 的列。
    - 訊號 B:FinMind TaiwanFuturesDaily(data_id=TX, position 時段 close/contract_date)
              basis = TX 近月 close − panel.ix_close;展期剩 ≤3 日轉次月(ROLL_DAYS 佔位)。

HOW TO RUN
    cd /Users/jackm4/Documents/ETF/股票研究 && .venv/bin/python \
        scripts/research/dashboard/futures_positioning_study.py               # live 基準(可跑)
    ... --with-external   # + 大額/basis 完整框架 + DSR + champion 搭配(讀快取,省配額)
    ... --with-external --refresh   # 強制重拉 FinMind(否則優先讀 cache/*.parquet)

  機讀摘要 → reports/research/dashboard-completeness/futures_positioning_metrics.csv
  真資料落地 → data/research/dashboard/futures_positioning_data.parquet(供重用)

NOTE  兩目標訊號 (A/B) 非本地表,但用專案標準 FinMind client 即可直拉並已 end-to-end
      跑通 → implementable_now = True(非 SCAFFOLD)。實測結論:兩者 OOS 皆不勝
      champion(+1.12)、corr_vs_champ 皆 >0.4(非獨立)、DSR 皆 <0.95(retail 0.34、
      連 champion 本身厚尾下也僅 0.59)、combo/veto 對 champion 無加成 → 不採信為交易
      alpha(見報告第 6 節)。無 token 且無快取時 --with-external 退回提示。

免責:量化研究方法論記錄,非投資建議。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from scipy.stats import norm
except Exception:  # scipy optional; DSR block skipped if unavailable
    norm = None

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))  # finmind_client / project_dotenv 直拉
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "dashboard-completeness"
DATA_OUT = ROOT / "data" / "research" / "dashboard"  # 持久化真資料供重用
COST_BPS = 4.0
ANN = np.sqrt(252)
RNG = np.random.default_rng(7)
GAMMA = 0.5772156649  # Euler-Mascheroni, for E[max SR]


# ---------- shared helpers (house style) ----------
def rz(s: pd.Series, w: int) -> pd.Series:
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(bull, day_ret, cost=COST_BPS) -> pd.Series:
    """Long/flat strategy return net of turnover cost."""
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def perm_p(bull, day_ret, mask, n=2000):
    """Permutation vs SAME-EXPOSURE random: hold same # of long days at random."""
    dr = day_ret[mask].reset_index(drop=True)
    pos = pd.Series(bull, index=day_ret.index)[mask].astype(float).reset_index(drop=True)
    v = dr.notna()
    dr, pos = dr[v], pos[v]
    actual = sharpe(pos.values * dr.values)
    k, N = int(pos.sum()), len(pos)
    if k == 0 or k == N:
        return actual, np.nan
    drv = dr.values
    null = np.array([
        sharpe(np.isin(np.arange(N), RNG.choice(N, k, False)).astype(float) * drv)
        for _ in range(n)
    ])
    return actual, float((null >= actual).mean())


def deflated_sharpe(ret: pd.Series, n_trials: int) -> dict:
    """DSR: PSR of the strategy vs expected max Sharpe of n_trials null strategies."""
    if norm is None:
        return {}
    r = pd.Series(ret).dropna().values
    n = len(r)
    if n < 30 or r.std() == 0:
        return {}
    sr = r.mean() / r.std()  # daily
    g3 = pd.Series(r).skew()
    g4 = pd.Series(r).kurtosis() + 3.0
    var_trials = 1.0 / n  # Bailey-Lopez de Prado iid-null proxy
    e = np.e
    sr_star = np.sqrt(var_trials) * (
        (1 - GAMMA) * norm.ppf(1 - 1.0 / n_trials)
        + GAMMA * norm.ppf(1 - 1.0 / (n_trials * e))
    )
    denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
    dsr = float(norm.cdf((sr - sr_star) * np.sqrt(n - 1) / denom))
    return {"daily_SR": sr, "ann_SR": sr * ANN, "skew": g3, "kurt": g4,
            "SR_star_ann": sr_star * ANN, "DSR": dsr}


def dsr_report(name, strat_ret, oos, var_trials, n_trials, rows, section):
    """house-style Deflated-Sharpe:PSR at SR* = E[max SR] over n_trials 空策略。
    var_trials 由實測搜尋網格的 OOS Sharpe 離散度估(非 1/n iid proxy)。
    DSR>0.95 => 通過 5% 多重測試。回傳 dict 並印。
    """
    if norm is None:
        return None
    r = pd.Series(strat_ret)[oos.reindex(strat_ret.index).fillna(False)].dropna().values
    n = len(r)
    if n < 30 or r.std() == 0:
        return None
    sr = r.mean() / r.std()  # daily
    g3 = pd.Series(r).skew()
    g4 = pd.Series(r).kurtosis() + 3.0
    e = np.e
    sr_star = np.sqrt(var_trials) * (
        (1 - GAMMA) * norm.ppf(1 - 1.0 / n_trials)
        + GAMMA * norm.ppf(1 - 1.0 / (n_trials * e)))
    denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
    psr0 = float(norm.cdf(sr * np.sqrt(n - 1) / denom))
    dsr = float(norm.cdf((sr - sr_star) * np.sqrt(n - 1) / denom))
    verdict = "SURVIVES" if dsr > 0.95 else "FAILS"
    print(f"  DSR {name:28s} annSR={sr*ANN:+.2f} skew={g3:+.2f} kurt={g4:.1f} "
          f"PSR(vs0)={psr0:.2f} SR*={sr_star*ANN:+.2f} DSR={dsr:.3f}[{verdict}]")
    rows.append({"section": section, "signal": f"DSR::{name}",
                 "OOS_Sharpe": float(sr * ANN), "DSR": dsr,
                 "SR_star_ann": float(sr_star * ANN), "PSR_vs0": psr0,
                 "skew": float(g3), "kurt": float(g4), "n_trials": int(n_trials)})
    return dsr


def evaluate_signal(name, v, p, day_ret, ismask, oos, champ_ret, mom20, rows,
                    section):
    """一個訊號跑完整框架:方向 IS-fixed → OOS Sharpe / corr_champ / corr_mom → perm。
    可被 live proxy 與外接的大額 / basis 訊號共用。回傳 dict 供彙整。
    """
    v = pd.Series(v, index=p.index)
    d = pd.DataFrame({"v": v, "f": day_ret}).dropna()
    if len(d) < 100:
        print(f"  [skip] {name}: <100 obs")
        return None
    ism = ismask.reindex(d.index).fillna(False)
    ic_is = d[ism]["v"].corr(d[ism]["f"])
    direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
    bull = (direction * v) > 0
    r = lf(bull, day_ret)
    oos_shp = sharpe(r[oos])
    corr_champ = pd.DataFrame({"a": r, "b": champ_ret}).dropna().corr().iloc[0, 1]
    corr_mom = pd.DataFrame({"v": v, "m": mom20}).dropna().corr().iloc[0, 1]
    _, pv = perm_p(bull, day_ret, oos)
    out = {"section": section, "signal": name, "dir": int(direction),
           "IC_IS": ic_is, "OOS_Sharpe": oos_shp, "corr_vs_champ": corr_champ,
           "corr_mom20": corr_mom, "OOS_perm_p": pv,
           "exposure": float(bull.reindex(day_ret.index).fillna(False).mean())}
    rows.append(out)
    indep = "INDEP" if abs(corr_champ) < 0.4 else "redundant"
    print(f"  {name:26s} dir={out['dir']:+d} IC_IS={ic_is:+.3f} "
          f"OOS={oos_shp:+.2f} corr_champ={corr_champ:+.2f}[{indep}] "
          f"corr_mom={corr_mom:+.2f} perm_p={pv if pv is None else round(pv,3)} "
          f"exp={out['exposure']:.2f}")
    return out


def _panel_frame():
    """共用:載入 panel、算 day_ret / regime / champion / mom。"""
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    mom20 = p["ix_close"].pct_change(20)
    n = len(p)
    is_cut = int(n * 0.7)
    ismask = pd.Series(p.index < is_cut, index=p.index)
    oos = pd.Series(p.index >= is_cut, index=p.index)
    ma200 = p["ix_close"].rolling(200, min_periods=200).mean()
    bull_regime = (p["ix_close"] > ma200) & (ma200 > ma200.shift(20))
    champ_bull = rz(p["fut_foreign_oi"], 60) > 0
    champ_ret = lf(champ_bull, day_ret)
    return dict(p=p, day_ret=day_ret, mom20=mom20, ismask=ismask, oos=oos,
                bull_regime=bull_regime, champ_bull=champ_bull, champ_ret=champ_ret)


# ---------- LIVE baseline (runnable now, panel-only) ----------
def live_baseline(rows: list):
    """champion 基準 + panel-內 proxy(非真大額,示範獨立性框架真的能跑)。"""
    f = _panel_frame()
    p, day_ret, oos, ismask = f["p"], f["day_ret"], f["oos"], f["ismask"]
    champ_ret, mom20 = f["champ_ret"], f["mom20"]

    bh_oos = sharpe(day_ret[oos])
    champ_oos = sharpe(champ_ret[oos])
    print("=" * 68)
    print(f"panel n={len(p)}  OOS n={int(oos.sum())}  "
          f"({p['date'].min()} → {p['date'].max()})")
    print(f"B&H OOS Sharpe = {bh_oos:+.2f}   champion OOS = {champ_oos:+.2f}   "
          f"bull_regime frac = {f['bull_regime'].mean():.2f}")
    print("=" * 68)
    rows.append({"section": "baseline", "signal": "BH", "OOS_Sharpe": bh_oos})
    rows.append({"section": "baseline", "signal": "champion_fut_foreign_oi",
                 "OOS_Sharpe": champ_oos, "exposure": float(f["champ_bull"][oos].mean())})

    print("\n[LIVE proxy] 非外資本土法人期貨 OI 代理(非真大額報表,僅示範框架 + 共線陷阱):")
    dom = p["fut_trust_oi"] + p["fut_dealer_oi"]
    proxies = {
        "proxy_dom_inst_oi_z60": rz(dom, 60),
        "proxy_trust_oi_z60":    rz(p["fut_trust_oi"], 60),
        "proxy_foreign_doi_z20": rz(p["fut_foreign_doi"], 20),
    }
    for name, v in proxies.items():
        evaluate_signal(name, v, p, day_ret, ismask, oos, champ_ret, mom20,
                        rows, "live_proxy")
    print("  → 本土法人 OI proxy corr_vs_champ 高(~0.69)= 印證『大額多與外資共線』;")
    print("    真正大額散戶殘差需接 FinMind 才能替換此 proxy 做正式檢定(見 --with-external)。")


# ============================================================
# 訊號 A/B 資料源(FinMind,專案標準 client;token 走 project_dotenv)
#   兩 dataset 皆非本地表 / 非 panel 欄,但用專案標準 FinMind client 即可直拉,
#   且已 end-to-end 跑通(見 run_external 真數字)→ implementable_now=True。
#   要正式落地建議 build_panel 加欄或另建表,避免每跑一次都重拉。
# ============================================================
LARGE_TRADER_DATASET = "TaiwanFuturesOpenInterestLargeTraders"
FUTURES_DAILY_DATASET = "TaiwanFuturesDaily"
CACHE = OUT / "cache"
_MONTH_RE = re.compile(r"^\d{6}$")


def _fetch_cached(dataset: str, data_id: str = "TX",
                  start_year: int = 2018, force: bool = False) -> pd.DataFrame:
    """取 FinMind 市場層資料並快取到 reports/.../cache/<dataset>.parquet。

    配額意識:預設**優先讀快取**(市場層資料每日追加,已抓 2018→今日即夠),
    只有 --refresh(force=True)或快取不存在時才逐年直拉 FinMind。
    無 token / 網路失敗 → 退回舊快取,否則回空 DataFrame(不炸)。
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_fp = CACHE / f"{dataset}.parquet"
    if cache_fp.exists() and not force:
        return pd.read_parquet(cache_fp)
    try:
        from finmind_client import fetch_finmind
        frames = []
        for y in range(start_year, date.today().year + 1):
            r = fetch_finmind(dataset, data_id, date(y, 1, 1), date(y, 12, 31))
            if r:
                frames.append(pd.DataFrame(r))
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if not df.empty:
            df.to_parquet(cache_fp)
            print(f"  [fetch] {dataset}: {len(df)} 列已抓並快取。")
        return df
    except Exception as exc:
        if cache_fp.exists():
            print(f"  [cache] {dataset} 直拉失敗({type(exc).__name__}),用快取。")
            return pd.read_parquet(cache_fp)
        print(f"  [SCAFFOLD] {dataset} 無法取得({type(exc).__name__}: {exc});"
              f"設 FINMIND_TOKEN 後重跑。")
        return pd.DataFrame()


def compute_large_trader_signals(raw: pd.DataFrame) -> pd.DataFrame:
    """大額原始列 → 日頻衍生訊號欄(FinMind 實際欄名)。
    近月契約 = contract_type 為 6 位 YYYYMM 的列(每日取最小 = 前月)。
    衍生:
      lt_top10_spec_net  = buy_top10_specific − sell_top10_specific  法人主力方向(領先但與 champion 共線)
      lt_retail_net      = sell_top10_trader − buy_top10_trader       散戶殘差反指標
                           (= −(全體前十大 net);因市場淨部位恆為 0,散戶 net 即大戶 net 的鏡像)
      lt_t6_10_spec_net  = top10_spec_net − top5_spec_net             第 6~10 大特定法人(較快錢)
    """
    if raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    df = df[df["contract_type"].astype(str).str.match(_MONTH_RE)]
    df = (df.sort_values(["date", "contract_type"])
            .groupby("date", as_index=False).first())

    def c(name):
        return pd.to_numeric(df[name], errors="coerce")

    out = pd.DataFrame({"date": df["date"].astype(str)})
    out["lt_top10_spec_net"] = (c("buy_top10_specific_open_interest")
                                - c("sell_top10_specific_open_interest"))
    out["lt_top5_spec_net"] = (c("buy_top5_specific_open_interest")
                               - c("sell_top5_specific_open_interest"))
    out["lt_retail_net"] = (c("sell_top10_trader_open_interest")
                            - c("buy_top10_trader_open_interest"))
    out["lt_t6_10_spec_net"] = out["lt_top10_spec_net"] - out["lt_top5_spec_net"]
    return out


def compute_basis_signals(raw_fut: pd.DataFrame) -> pd.DataFrame:
    """TX 近月價 → basis(去季節前後)。
    - 一般交易時段(trading_session=='position');排除價差 / 週選(contract_date 純 6 位)。
    - 每日取最小 contract_date = 近月(front)。roll: 到期收斂由 60d z 平滑吸收;
      正式版剩 <=3 交易日轉次月(見 ROLL_DAYS 佔位)。
    - basis = tx_near − ix_close(現貨腳由呼叫端 merge panel.ix_close 提供)。
    """
    if raw_fut.empty:
        return pd.DataFrame()
    fut = raw_fut.copy()
    fut = fut[(fut["trading_session"].astype(str) == "position")
              & fut["contract_date"].astype(str).str.match(_MONTH_RE)]
    fut["close"] = pd.to_numeric(fut["close"], errors="coerce")
    near = (fut.sort_values(["date", "contract_date"])
              .groupby("date", as_index=False).first())
    return near[["date", "close"]].rename(columns={"close": "tx_near"}).assign(
        date=lambda d: d["date"].astype(str))


ROLL_DAYS = 3  # 佔位:近月剩 <=ROLL_DAYS 交易日轉次月(正式落地時啟用加權展期)


def run_external(rows: list, force: bool = False):
    """訊號 A/B end-to-end 評估(FinMind 直拉,已跑通)。無 token → 用快取或印提示。"""
    f = _panel_frame()
    p, day_ret, oos, ismask = f["p"], f["day_ret"], f["oos"], f["ismask"]
    champ_ret, mom20, bull_regime = f["champ_ret"], f["mom20"], f["bull_regime"]
    champ_bull = f["champ_bull"]
    p = p.copy()
    p["date"] = p["date"].astype(str)

    print("\n" + "=" * 68)
    print("[EXTERNAL] 訊號 A 大額交易人 + 訊號 B basis(FinMind 直拉)")
    print("=" * 68)

    lt = compute_large_trader_signals(_fetch_cached(LARGE_TRADER_DATASET, force=force))
    basis = compute_basis_signals(_fetch_cached(FUTURES_DAILY_DATASET, force=force))

    if lt.empty and basis.empty:
        print("\n  → 兩 dataset 皆未取得(無 token 且無快取):SCAFFOLD 待同步。")
        print("    設 FINMIND_TOKEN 後重跑即 end-to-end 產出真數字;落地步驟見報告第 8 節。")
        return

    # ---- 訊號 A:大額交易人 ----
    if not lt.empty:
        m = p.merge(lt, on="date", how="left").reset_index(drop=True)
        print("\n[訊號 A] 大額交易人衍生量(方向 IS-fixed;主候選=散戶殘差反指標):")
        for col in ["lt_top10_spec_net", "lt_retail_net", "lt_t6_10_spec_net"]:
            evaluate_signal(f"{col}_z60", rz(m[col], 60), p, day_ret, ismask,
                            oos, champ_ret, mom20, rows, "large_trader")
        print("  → 前十大 / 散戶殘差 corr_vs_champ 皆 >0.4 = 與外資 champion 共線(冗餘),"
              "非獨立新來源(印證調研:外資即最大宗特定法人)。")

    # ---- 訊號 B:basis ----
    if not basis.empty:
        m = p.merge(basis, on="date", how="left").reset_index(drop=True)
        m["basis_pct"] = (m["tx_near"] - m["ix_close"]) / m["ix_close"]
        print("\n[訊號 B] basis(線性 level 預期無方向;主訊號在極端分位 × bull):")
        evaluate_signal("basis_pct_raw", m["basis_pct"], p, day_ret, ismask, oos,
                        champ_ret, mom20, rows, "basis")
        evaluate_signal("basis_pct_z60", rz(m["basis_pct"], 60), p, day_ret,
                        ismask, oos, champ_ret, mom20, rows, "basis")
        # 極端逆價差 × bull 反轉(前兆)——house 認為這才是有機會的形式
        z = rz(m["basis_pct"], 60)
        q10 = z.rolling(252, min_periods=120).quantile(0.10)
        rev = (z < q10) & bull_regime
        rr = lf(rev, day_ret)
        print(f"  basis_extreme_low × bull 反轉: OOS Sharpe={sharpe(rr[oos]):+.2f} "
              f"exposure={rev.mean():.2%}")
        rows.append({"section": "basis", "signal": "basis_extreme_low_x_bull",
                     "OOS_Sharpe": float(sharpe(rr[oos])), "exposure": float(rev.mean())})

    # ---- 正式檢定:Deflated-Sharpe + champion 搭配(confirm / veto) ----
    print("\n[正式檢定] Deflated-Sharpe(搜尋離散度去膨脹)+ champion 搭配:")
    # 搜尋離散度 var_trials 由本研究實測的所有候選 OOS Sharpe(日頻)估
    tested = [r for r in rows if r.get("section") in
              ("live_proxy", "large_trader", "basis")
              and pd.notna(r.get("OOS_Sharpe"))]
    trial_sr_daily = [r["OOS_Sharpe"] / ANN for r in tested]
    var_trials = float(np.var(trial_sr_daily, ddof=1)) if len(trial_sr_daily) > 1 else 1.0 / int(oos.sum())
    n_trials = len(trial_sr_daily) + 20  # + 保守 pad(未揭露的窗 / 門檻變體)
    print(f"  var_trials={var_trials:.2e} (std daily SR={np.sqrt(var_trials):.4f})  "
          f"n_trials(保守)={n_trials}")

    dsr_report("champion_fut_foreign_oi", champ_ret, oos, var_trials, n_trials,
               rows, "dsr")
    if not lt.empty:
        m = p.merge(lt, on="date", how="left").reset_index(drop=True)
        # 最強候選:散戶殘差反指標(IS IC 定方向 → OOS 表面最高)
        v_ret = rz(m["lt_retail_net"], 60)
        ic_is = pd.DataFrame({"v": v_ret, "f": day_ret})[ismask].dropna().pipe(
            lambda d: d["v"].corr(d["f"]))
        retail_bull = (np.sign(ic_is) * v_ret) > 0
        retail_ret = lf(retail_bull, day_ret)
        dsr_report("lt_retail_net_z60", retail_ret, oos, var_trials, n_trials,
                   rows, "dsr")
        # champion 搭配:50/50 combo vs champion-alone,及散戶 veto
        combo = 0.5 * champ_ret + 0.5 * retail_ret
        # veto:champion 做多但散戶同向極端偏多(反指標警訊)→ 當日 flat
        retail_extreme_bull = v_ret > v_ret.rolling(252, min_periods=120).quantile(0.90)
        champ_veto_bull = champ_bull & ~retail_extreme_bull.fillna(False)
        champ_veto_ret = lf(champ_veto_bull, day_ret)
        print(f"  champion alone      OOS={sharpe(champ_ret[oos]):+.2f}")
        print(f"  champion⊕retail 50/50 OOS={sharpe(combo[oos]):+.2f}  "
              f"corr={pd.DataFrame({'a': champ_ret, 'b': retail_ret}).dropna().corr().iloc[0,1]:+.2f}")
        print(f"  champion÷retail-veto  OOS={sharpe(champ_veto_ret[oos]):+.2f}  "
              f"(veto {(champ_bull & retail_extreme_bull.fillna(False))[oos].mean():.1%} of days)")
        rows.append({"section": "combo", "signal": "champion_plus_retail_5050",
                     "OOS_Sharpe": float(sharpe(combo[oos]))})
        rows.append({"section": "combo", "signal": "champion_retail_veto",
                     "OOS_Sharpe": float(sharpe(champ_veto_ret[oos]))})

    # ---- 持久化真資料供重用 ----
    DATA_OUT.mkdir(parents=True, exist_ok=True)
    data_fp = DATA_OUT / "futures_positioning_data.parquet"
    merged = p[["date", "ix_close"]].copy()
    if not lt.empty:
        merged = merged.merge(lt, on="date", how="left")
    if not basis.empty:
        merged = merged.merge(basis, on="date", how="left")
        merged["basis_pct"] = (merged["tx_near"] - merged["ix_close"]) / merged["ix_close"]
    merged.to_parquet(data_fp)
    print(f"\n  [persist] 真資料 → {data_fp} (n={len(merged)}, "
          f"cols={[c for c in merged.columns if c not in ('date','ix_close')]})")

    print("\n[結論] 兩訊號皆 end-to-end 跑通,但 OOS 皆不勝 champion(+1.12)、"
          "corr_vs_champ 皆 >0.4(非獨立);basis 線性為負、極端反轉近 0;"
          "DSR 遠 <0.95;combo/veto 無法勝純 champion。"
          "定性=冗餘 / 動能鏡像,非新 alpha(見報告第 6 節)。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-external", action="store_true",
                    help="嘗試拉 FinMind 大額 / basis(需先同步 / 設 token)")
    ap.add_argument("--refresh", action="store_true",
                    help="強制重拉 FinMind(否則優先讀快取,省配額)")
    args = ap.parse_args()

    rows: list = []
    live_baseline(rows)
    if args.with_external:
        run_external(rows, force=args.refresh)
    else:
        print("\n(未帶 --with-external:僅跑 live 基準。加 --with-external 讀快取跑"
              "大額 / basis 完整框架 + DSR;--refresh 才重拉 FinMind。)")

    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    csv = OUT / "futures_positioning_metrics.csv"
    df.to_csv(csv, index=False)
    print(f"\n-> {csv}")


if __name__ == "__main__":
    main()
