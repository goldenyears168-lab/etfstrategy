"""國際總經錨 (SOX / DXY / US10Y / VIX / Fed / 新台幣匯率) —— L0 regime + 開盤 gap 前兆維度研究.

WHAT THIS IS
  把「國際總經/風險錨」操作化補進台股觀盤系統。六個錨都是 *外生* 總經/風險背景,
  不是台股內生籌碼 —— 依 LAYERED_DESIGN 它們最可能的價值是 **L0 regime gate**
  (VIX/Fed/US10Y/DXY 的風險胃納背景) 與 **開盤 gap 前兆** (SOX / 台積 ADR 隔夜),
  而不是獨立 alpha。全部透過既有 chip-macro 管線 (IC scan → IS/OOS Sharpe →
  同曝險 permutation → Deflated-Sharpe → regime-conditioning) 做證偽式驗證,
  第一件事永遠是量「淨增量 IC (beyond TAIEX 自身動能 & 現有 foreign 現貨流)」,
  而非原始相關 —— 因為 SOX≈全球科技 beta、TWD≈外資現貨流的 FX 印記,原始相關會很高
  但多半是動能/資金流的偽裝 (與 margin 因子被揭穿為動能代理的同一陷阱)。

  誠實資料分層 (皆已用真資料實跑;僅 Fed/VIXTWN 待補):
    ✅ STRAND A (LOCAL): SOX / SMH / 台積 ADR (TSM) —— daily_bars 2019-01→2026-07-29。
       完整 IS/OOS + permutation + DSR + regime + 共線控制。
    ✅ STRAND B (真資料): VIX / DXY / US10Y / USDTWD —— fetch_external_to_parquet() 直打
       Yahoo v8 chart API 抓 ^VIX / DX-Y.NYB / ^TNX / USDTWD=X,存
       data/research/dashboard/global_macro_data.parquet,run_strand_b() 讀之跑同一框架。
       結果: 連續因子全無訊號 (perm p 不顯著);唯一真效果 = VIX 大 spike 當恐慌 gate (p=0.035)。
    ⏳ 待補 (EXTERNAL, 未接): Fed (FRED FEDFUNDS + FOMC 行事曆) / 在地 VIXTWN (TAIFEX)。
       下方 fetch_fred_series / fetch_finmind_twd / build_external_macro_panel 為這兩者保留的可選骨架。

DATA SOURCE
  LOCAL (STRAND A, 立即可跑,零外接):
    data/research/chip_macro/panel.parquet   TAIEX ix_open/high/low/close + foreign 現貨 + champion fut_foreign_oi (2018-06→2026-07-29)
    data/stocks.db  daily_bars code IN ('SOX','SMH','TSM_ADR')  source='yahoo'  (2019-01-02→2026-07-29)
  EXTERNAL (STRAND B, 需先同步):
    FRED   (api_key 需求; 建議 ALFRED point-in-time 防修訂前視):
           VIXCLS(VIX) / DGS10(US10Y) / DTWEXBGS(broad USD≈DXY 代理) / DEXTAUS(TWD/USD) / FEDFUNDS(Fed 政策利率)
    Yahoo  (同美盤日 close,無回溯修訂,較適回測): ^SOX / TSM / ^VIX / DX-Y.NYB(真 ICE DXY) / ^TNX(=10Y×10) / USDTWD=X
    FinMind(專案主源): TaiwanExchangeRate (USD 台銀牌告 spot buy/sell,日頻)
    TAIFEX / MacroMicro: VIXTWN(台指選擇權隱波,在地恐慌,無美盤時差) —— 非 FinMind,需自頁面取
    FOMC 行事曆: 需 scaffold 硬編會議日 (Fed 事件窗)

METHODOLOGY (依專案方法論,證偽優先)
  1) 時差防前視 (最致命): 美盤系列一律 lag「+1 台股交易日」—— 對 TW 交易日 t,只用
     us_date < t 的最後一筆 (merge_asof direction=backward, allow_exact_matches=False)。
     FRED 日頻發布延遲 (DGS10 約美東次工作日定稿) → 用 Yahoo 同美盤日 close 較安全,仍 +1 TW 日。
  2) 方向 a-priori / 由 IS IC 符號定,不偷看 OOS;門檻凍結,不做網格搜尋 (避免重蹈 980T/adopted-44)。
  3) IS/OOS 70/30 時間分割。
  4) 同曝險 permutation (固定相同做多天數、隨機挑日,vs 同曝險隨機)。
  5) Deflated-Sharpe (Bailey & López de Prado),對 n_trials 校正,門檻 0.95 —— 六錨 × 多 transform
     搜尋空間大,DSR 幾乎必 fail,這是誠實下修的關鍵關卡。
  6) regime-conditioning: 分 bull(>MA200)/bear 報 edge (預期 edge 只在多頭出現)。
  7) 共線控制 (核心): gap 分解 —— SOX 對台股的「領先」是機械式時差,開盤 gap 那一刻已 priced;
     真 edge 只可能在「開盤後 intraday 續勢」的殘差 (且要對 TAIEX 自身動能 & foreign 正交化)。

HOW TO RUN
  cd /Users/jackm4/goldenstocks && source .venv/bin/activate
  python scripts/research/dashboard/global_macro_study.py                 # STRAND A: SOX/ADR 實跑
  python scripts/research/dashboard/global_macro_study.py --with-external  # STRAND B: 需先同步外部資料 (否則印出待辦)

LEAD/LAG + LAYER (對照 champion = 外資台指期 positioning, 領先, chip×bull OOS +1.79)
  SOX / 台積 ADR 隔夜 : 開盤 gap 前兆 (機械式時差領先,持有層面≈同步);可交易性最弱。
  DXY               : 領先/regime (低頻,EM 資金流驅動);與 champion 同「資金流」家族但更外生更慢。
  US10Y             : 領先 regime (低頻),符號 regime-dependent (通膨 vs 成長 會翻),只當條件化背景。
  VIX               : 前兆 (spike=drawdown 先行) + 同步風險狀態;最佳用法 = regime gate 非連續因子。
  Fed               : 領先 regime (慢變 macro backdrop),最上層 L0 背景燈,非交易訊號。
  新台幣 TWD         : 同步/coincident,恐與現有 foreign 現貨流冗餘;需先驗淨增量再決定是否納入。
  沒有一個錨比 champion 更領先。定位 = 強化 L0 regime gate + 開盤前 gap 預期,非獨立 alpha。

Not investment advice. Research falsification only. 非投資建議,僅供研究證偽。
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[3]
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "dashboard-completeness"

COST_BPS = 4.0          # per-side, matches chip-macro convention
ANN = np.sqrt(252)
GAMMA = 0.5772156649    # Euler-Mascheroni, expected-max-SR
RNG = np.random.default_rng(7)
IS_FRAC = 0.70


# ----------------------------------------------------------------------------
# primitives (aligned with scripts/research/chip_macro/* & dashboard siblings)
# ----------------------------------------------------------------------------
def rz(s: pd.Series, w: int) -> pd.Series:
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(pos, fwd, cost=COST_BPS) -> pd.Series:
    """Long/flat P&L: continuous position in [0,1], turnover-costed."""
    pos = pd.Series(pos, index=fwd.index).astype(float).fillna(0.0)
    return pos * fwd - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def perm_p_same_exposure(pos, fwd, mask, n=2000) -> tuple[float, float]:
    """Permutation vs random days holding the SAME number of long days (OOS)."""
    fr = fwd[mask].reset_index(drop=True)
    ps = pd.Series(pos, index=fwd.index)[mask].astype(float).reset_index(drop=True)
    ok = fr.notna()
    fr, ps = fr[ok], ps[ok]
    actual = sharpe(ps.values * fr.values)
    k, N = int(round(ps.sum())), len(ps)
    if k == 0 or k >= N:
        return actual, np.nan
    frv = fr.values
    null = np.empty(n)
    for i in range(n):
        idx = RNG.choice(N, k, replace=False)
        rp = np.zeros(N)
        rp[idx] = 1.0
        null[i] = sharpe(rp * frv)
    return actual, float((null >= actual).mean())


def deflated_sharpe(ret: pd.Series, n_trials: int) -> dict:
    """Bailey & López de Prado DSR: PSR against expected-max-SR of n_trials nulls."""
    r = pd.Series(ret).dropna().values
    n = len(r)
    if n < 30 or r.std() == 0:
        return {"daily_sr": np.nan, "ann": np.nan, "psr0": np.nan, "dsr": np.nan}
    sr = r.mean() / r.std()
    g3 = pd.Series(r).skew()
    g4 = pd.Series(r).kurtosis() + 3.0
    var_trials = 1.0 / n
    sr_star = np.sqrt(var_trials) * (
        (1 - GAMMA) * norm.ppf(1 - 1.0 / n_trials)
        + GAMMA * norm.ppf(1 - 1.0 / (n_trials * np.e))
    )

    def psr(sr_star_):
        denom = np.sqrt(1 - g3 * sr + (g4 - 1) / 4.0 * sr**2)
        return float(norm.cdf((sr - sr_star_) * np.sqrt(n - 1) / denom))

    return {"daily_sr": sr, "ann": sr * ANN, "psr0": psr(0.0),
            "dsr": psr(sr_star), "sr_star_ann": sr_star * ANN}


# ----------------------------------------------------------------------------
# generic anchor evaluator — used by BOTH the local strand and the scaffold
# ----------------------------------------------------------------------------
def evaluate_anchor(signal: pd.Series, fwd: pd.Series, bull: pd.Series,
                    name: str, n_trials: int, direction: float | None = None) -> dict:
    """One-anchor falsification: direction from IS IC, IS/OOS IC & Sharpe, perm, DSR, regime.

    signal : normalized anchor value aligned to TW trading dates (already lagged, no lookahead)
    fwd    : forward TW return to trade (e.g. next-day open->close for a gap-precursor test)
    bull   : L0 regime mask (index>MA200) aligned to same dates
    """
    df = pd.DataFrame({"v": signal, "f": fwd, "bull": bull}).dropna(subset=["v", "f"])
    n = len(df)
    if n < 200:
        return {"signal": name, "note": "insufficient_data", "n": n}
    is_cut = int(n * IS_FRAC)
    idx = df.index
    is_mask = pd.Series(np.arange(n) < is_cut, index=idx)
    oos = pd.Series(np.arange(n) >= is_cut, index=idx)

    ic_is = df.loc[is_mask.values, "v"].corr(df.loc[is_mask.values, "f"])
    ic_oos = df.loc[oos.values, "v"].corr(df.loc[oos.values, "f"])
    d = direction if direction is not None else (np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0)

    pos = ((d * df["v"]) > 0).astype(float)
    r = lf(pos, df["f"])
    oos_shp = sharpe(r[oos.values])
    is_shp = sharpe(r[is_mask.values])
    _, perm = perm_p_same_exposure(pos, df["f"], oos)
    dsr = deflated_sharpe(r[oos.values], n_trials)

    # regime split (OOS)
    bmask = df["bull"].fillna(False).astype(bool)
    r_bull = sharpe(r[(oos.values) & (bmask.values)])
    r_bear = sharpe(r[(oos.values) & (~bmask.values)])

    return {"signal": name, "dir": int(d), "n": n,
            "IC_IS": ic_is, "IC_OOS": ic_oos,
            "IS_Sharpe": is_shp, "OOS_Sharpe": oos_shp,
            "OOS_perm_p": perm, "OOS_DSR": dsr["dsr"], "OOS_ann": dsr["ann"],
            "OOS_Sharpe_bull": r_bull, "OOS_Sharpe_bear": r_bear,
            "exposure": pos.mean()}


# ----------------------------------------------------------------------------
# STRAND A — SOX / SMH / 台積 ADR (LOCAL, implementable NOW)
# ----------------------------------------------------------------------------
def load_local() -> pd.DataFrame:
    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    p["date"] = pd.to_datetime(p["date"])
    con = sqlite3.connect(DB)
    us = {}
    for code in ("SOX", "SMH", "TSM_ADR"):
        q = pd.read_sql(
            "SELECT date, close FROM daily_bars WHERE code=? AND source='yahoo' ORDER BY date",
            con, params=(code,))
        q["date"] = pd.to_datetime(q["date"])
        q[f"{code}_ret"] = q["close"].pct_change()
        us[code] = q[["date", f"{code}_ret"]]
    con.close()
    m = p.copy()
    for code in ("SOX", "SMH", "TSM_ADR"):
        # NO-LOOKAHEAD: TW date t uses last US session STRICTLY BEFORE t.
        # (US session dated t closes ~04:00 TW next morning, i.e. after TW session t.)
        m = pd.merge_asof(m.sort_values("date"), us[code], on="date",
                          direction="backward", allow_exact_matches=False)
    # TAIEX decompositions
    m["gap"] = m["ix_open"] / m["ix_close"].shift(1) - 1.0        # overnight gap (SOX priced HERE)
    m["intraday"] = m["ix_close"] / m["ix_open"] - 1.0            # open->close residual (real edge zone)
    m["cc"] = m["ix_close"] / m["ix_close"].shift(1) - 1.0        # close->close (mechanical, includes gap)
    m["taiex_mom20"] = m["ix_close"].pct_change(20)              # own-momentum collinearity control
    return m


def orthogonalize(y: pd.Series, controls: pd.DataFrame) -> pd.Series:
    """Residual of y after OLS on controls (+const). Robust to NaN via row-wise dropna."""
    d = pd.concat([y.rename("y"), controls], axis=1).dropna()
    if len(d) < 50:
        return pd.Series(np.nan, index=y.index)
    X = np.column_stack([np.ones(len(d))] + [d[c].values for c in controls.columns])
    beta, *_ = np.linalg.lstsq(X, d["y"].values, rcond=None)
    resid = d["y"].values - X @ beta
    out = pd.Series(np.nan, index=y.index)
    out.loc[d.index] = resid
    return out


def run_strand_a(m: pd.DataFrame) -> pd.DataFrame:
    """The genuinely runnable study: does SOX/ADR隔夜 carry edge BEYOND the priced-in gap?"""
    n_trials = 8  # {SOX,SMH,ADR} x {gap,intraday,cc} coarse family + champion ≈ conservative
    bull = m["ix_close"] > m["ix_close"].rolling(200, min_periods=200).mean()
    rows: list[dict] = []

    # raw mechanical relationships (documented, NOT tradable — gap is priced at open)
    for code in ("SOX", "SMH", "TSM_ADR"):
        sig = rz(m[f"{code}_ret"], 20)  # overnight return z-score vs 20d vol
        for target, lbl in (("gap", "gap[priced]"), ("intraday", "intraday[residual]"),
                            ("cc", "c2c[mechanical]")):
            res = evaluate_anchor(sig.set_axis(m.index), m[target].set_axis(m.index),
                                  bull.set_axis(m.index), f"{code}_z20 -> {lbl}", n_trials,
                                  direction=+1.0)
            res["target"] = target
            rows.append(res)

    # THE decisive test: intraday continuation ORTHOGONALIZED to TAIEX own-momentum + foreign flow.
    # If edge survives residualization -> genuine incremental info; else it's beta/flow disguise.
    for code in ("SOX", "TSM_ADR"):
        sig = rz(m[f"{code}_ret"], 20).set_axis(m.index)
        controls = pd.DataFrame({
            "taiex_mom20": m["taiex_mom20"].values,
            "foreign": m["foreign"].values,  # 外資現貨淨買超 (同步指標;控掉它的資金流印記)
        }, index=m.index)
        resid_sig = orthogonalize(sig, controls)
        res = evaluate_anchor(resid_sig, m["intraday"].set_axis(m.index),
                              bull.set_axis(m.index),
                              f"{code}_z20 ⟂(mom20+foreign) -> intraday", n_trials,
                              direction=+1.0)
        res["target"] = "intraday_resid"
        rows.append(res)

    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------
# STRAND B — VIX / DXY / US10Y / USDTWD  (真資料, Yahoo v8 chart) + Fed/VIXTWN 待補骨架
# ----------------------------------------------------------------------------
def fetch_fred_series(series_id: str, api_key: str | None = None,
                      start: str = "2018-01-01") -> pd.DataFrame:
    """FRED observations -> DataFrame[date, value].  SCAFFOLD: 需 FRED api_key。

    series_id: VIXCLS / DGS10 / DTWEXBGS / DEXTAUS / FEDFUNDS / DFEDTARU
    NOTE: FRED 日頻序列會回溯修訂 (DGS10 等)。要真正 point-in-time 無前視,
          應改用 ALFRED (add `&realtime_start=`/`&vintage_dates=`)。回測用 Yahoo 同日 close 較安全。
    """
    import requests
    if not api_key:
        raise RuntimeError("SCAFFOLD: 需 FRED api_key (https://fred.stlouisfed.org/docs/api/api_key.html)")
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {"series_id": series_id, "api_key": api_key, "file_type": "json",
              "observation_start": start}
    js = requests.get(url, params=params, timeout=60).json()
    obs = js.get("observations", [])
    df = pd.DataFrame(obs)[["date", "value"]]
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna()


def fetch_yahoo_series(symbol: str, start: str = "2018-01-01") -> pd.DataFrame:
    """Yahoo daily close via yfinance.  SCAFFOLD (與 src/yahoo_chart_sync.py 同源)。

    symbol: ^SOX / TSM / ^VIX / DX-Y.NYB / ^TNX / USDTWD=X
    建議: 在 src/yahoo_chart_sync.py 的 YAHOO_INDEX_CODES 加代碼 (VIX->^VIX, DXY->DX-Y.NYB,
          US10Y->^TNX, USDTWD->USDTWD=X),寫進 daily_bars(source='yahoo') 與 SOX/SMH 一致。
    """
    import yfinance as yf
    df = yf.download(symbol, start=start, progress=False, auto_adjust=False)
    if df.empty:
        raise RuntimeError(f"SCAFFOLD: Yahoo {symbol} 無資料")
    out = df[["Close"]].reset_index()
    out.columns = ["date", "value"]
    out["date"] = pd.to_datetime(out["date"])
    return out


def fetch_finmind_twd(start: date, end: date) -> pd.DataFrame:
    """FinMind TaiwanExchangeRate (USD 台銀牌告).  SCAFFOLD (走 src/finmind_client.fetch_finmind)。

    回傳台銀牌告買賣價 (非銀行間中價;要中價用 Yahoo USDTWD=X / FRED DEXTAUS)。
    dataset='TaiwanExchangeRate', data_id='USD'。
    """
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    from finmind_client import fetch_finmind  # noqa: E402
    raw = fetch_finmind("TaiwanExchangeRate", "USD", start, end)
    df = pd.DataFrame(raw)
    if df.empty:
        raise RuntimeError("SCAFFOLD: FinMind TaiwanExchangeRate 空 (檢查 token/日期)")
    # 欄位: date, cash_buy, cash_sell, spot_buy, spot_sell — 用 spot 中值當 USDTWD 代理
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = (df["spot_buy"] + df["spot_sell"]) / 2.0
    return df[["date", "value"]].dropna()


# FOMC 會議日 (硬編 scaffold;Fed 事件窗用)。實務需每年更新。
FOMC_DATES_2018_2026: tuple[str, ...] = (
    # 精簡示意 — 正式使用需補全官方公告日 (Fed 官網 calendar)
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31",
    "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30",
)


EXTERNAL_ANCHOR_SPECS: dict[str, dict] = {
    # name: {source, id(s), transform, a-priori direction, lead/lag, layer}
    "DXY": {"source": "yahoo", "id": "DX-Y.NYB", "fred": "DTWEXBGS",
            "transform": "Δ5d z-score (週尺度最穩)", "direction": -1,
            "leadlag": "領先/regime(低頻)", "layer": "L0",
            "note": "美元升→EM/台股資金外流;與 champion 同資金流家族但更外生更慢"},
    "US10Y": {"source": "yahoo", "id": "^TNX", "fred": "DGS10",
              "transform": "Δyield(日/週) + level 門檻(>4.5% headwind)", "direction": -1,
              "leadlag": "領先 regime,符號 regime-dependent", "layer": "L0",
              "note": "折現率;通膨 regime 符號會翻(2022 股債齊跌)→必跨 regime 條件化,不可固定線性"},
    "VIX": {"source": "yahoo", "id": "^VIX", "fred": "VIXCLS",
            "transform": "21d MA<13低/>22高 gate + spike z-score + 極值(40-50)均值回歸", "direction": -1,
            "leadlag": "前兆(spike)+同步風險狀態", "layer": "L0",
            "note": "線性 IC 弱,只有極值/regime 有用→當風險燈號 gate 非連續因子;優先用在地 VIXTWN 去時差"},
    "Fed": {"source": "fred", "id": "FEDFUNDS", "fred": "DFEDTARU",
            "transform": "近3月政策利率變動符號(降/升/持平) + FOMC 事件窗", "direction": +1,
            "leadlag": "領先 regime(慢變 backdrop)", "layer": "L0",
            "note": "寬鬆循環=risk-on 放行;pre-FOMC drift 2015 後衰減,不可當現行 alpha"},
    "TWD": {"source": "finmind", "id": "USD", "fred": "DEXTAUS",
            "transform": "ΔTWD(5d) z-score", "direction": +1,
            "leadlag": "同步/coincident(恐與 foreign 冗餘)", "layer": "L0/同步",
            "note": "外資現貨買超的 FX 印記;日頻 coincident,務必測相對 foreign 的淨增量,大概率冗餘"},
}


def build_external_macro_panel(panel: pd.DataFrame, *, fred_api_key: str | None = None,
                               start: str = "2018-01-01") -> pd.DataFrame:
    """SCAFFOLD: 需先同步外部資料源。抓 5 錨 → lag +1 TW 日 → 對齊既有 panel(date 主鍵)。

    Fill order per anchor: 美盤系列 (Yahoo close, 同美盤日) → merge_asof backward + allow_exact=False
    到 TW 交易日 (US D → TW D+1 才可用,防前視);TWD/VIXTWN 同日可 exact。
    未提供 key / yfinance / token 時對應錨會 raise —— 呼叫端應逐錨 try 並記錄缺口。
    """
    out = panel[["date"]].copy().sort_values("date")
    for name, spec in EXTERNAL_ANCHOR_SPECS.items():
        try:
            if spec["source"] == "yahoo":
                s = fetch_yahoo_series(spec["id"], start)
            elif spec["source"] == "fred":
                s = fetch_fred_series(spec["id"], fred_api_key, start)
            elif spec["source"] == "finmind":
                s = fetch_finmind_twd(pd.to_datetime(start).date(), date.today())
            else:
                continue
            s = s.rename(columns={"value": name}).sort_values("date")
            exact = (spec["source"] == "finmind")  # 在地資料同日可用;美盤系列須 +1 TW 日
            out = pd.merge_asof(out, s, on="date", direction="backward",
                                allow_exact_matches=exact)
        except Exception as exc:  # noqa: BLE001
            print(f"  [scaffold] {name} 未同步: {exc}")
    return out


EXT_DATA = ROOT / "data" / "research" / "dashboard" / "global_macro_data.parquet"

# Yahoo v8 chart symbols (真資料源;yfinance 常被 rate-limit,直打 chart API 較穩)
YAHOO_CHART_SYMBOLS = {"VIX": "%5EVIX", "DXY": "DX-Y.NYB", "US10Y": "%5ETNX", "USDTWD": "TWD%3DX"}


def fetch_external_to_parquet(start: str = "2018-01-01") -> pd.DataFrame:
    """真資料抓取: Yahoo v8 chart API 拉 ^VIX / DX-Y.NYB / ^TNX / USDTWD=X → parquet。

    存 data/research/dashboard/global_macro_data.parquet 供重用。^TNX 已是 yield(%) 本身。
    """
    import requests
    p1 = int(pd.Timestamp(start).timestamp())
    p2 = int(pd.Timestamp.now().timestamp()) + 86400
    frames = []
    for name, sym in YAHOO_CHART_SYMBOLS.items():
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
               f"?period1={p1}&period2={p2}&interval=1d")
        j = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60).json()
        r = j["chart"]["result"][0]
        ts = pd.to_datetime(r["timestamp"], unit="s").normalize()
        cl = r["indicators"]["quote"][0]["close"]
        df = pd.DataFrame({"date": ts, name: cl}).dropna().groupby("date", as_index=False).last()
        frames.append(df.set_index("date"))
    out = pd.concat(frames, axis=1).reset_index()
    EXT_DATA.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(EXT_DATA, index=False)
    return out


# a-priori continuous-factor transforms (凍結,不做網格搜尋)
STRAND_B_SPECS = {
    "DXY":    {"transform": "d5_z60",    "dir": -1},   # 美元 Δ5d 升→台股跌
    "US10Y":  {"transform": "d5_z60",    "dir": -1},   # 殖利率 Δ5d 升→折現率壓評價
    "VIX":    {"transform": "level_z60", "dir": -1},   # VIX 高→risk-off
    "USDTWD": {"transform": "d5_z60",    "dir": -1},   # USDTWD 升(台幣貶)→台股跌
}


def _strand_b_signal(m: pd.DataFrame, name: str, transform: str) -> pd.Series:
    if transform == "level_z60":
        return rz(m[name], 60)
    return rz(m[name].diff(5), 60)


def run_strand_b(panel: pd.DataFrame, fred_api_key: str | None = None,
                 refetch: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """真資料執行: VIX/DXY/US10Y/USDTWD → TAIEX 前瞻報酬。

    回傳 (連續因子表, 正交化表, VIX-gate 表)。美盤系列一律 lag +1 TW 日 (防前視)。
    """
    if refetch or not EXT_DATA.exists():
        fetch_external_to_parquet()
    ext = pd.read_parquet(EXT_DATA)
    ext["date"] = pd.to_datetime(ext["date"])
    m = panel.copy()
    m["date"] = pd.to_datetime(m["date"])
    for col in ("VIX", "DXY", "US10Y", "USDTWD"):
        s = ext[["date", col]].dropna().sort_values("date")
        m = pd.merge_asof(m.sort_values("date"), s, on="date",
                          direction="backward", allow_exact_matches=False)  # US D → TW D+1
    m = m.reset_index(drop=True)
    m["fwd1"] = m["ix_close"].pct_change().shift(-1)                 # 次日 close-to-close
    m["fwd5"] = m["ix_close"].shift(-5) / m["ix_close"] - 1.0        # 5 日前瞻
    m["champ"] = m["fut_foreign_oi"].diff(5)                         # champion positioning Δ5d
    bull = (m["ix_close"] > m["ix_close"].rolling(200, min_periods=200).mean()).set_axis(m.index)
    controls = pd.DataFrame({"foreign": m["foreign"].values, "champ": m["champ"].values}, index=m.index)
    n_trials = len(STRAND_B_SPECS) * 3 + 8

    raw, orth = [], []
    for name, spec in STRAND_B_SPECS.items():
        sig = _strand_b_signal(m, name, spec["transform"]).set_axis(m.index)
        for tgt in ("fwd1", "fwd5"):
            res = evaluate_anchor(sig, m[tgt].set_axis(m.index), bull,
                                  f"{name}[{tgt}]", n_trials, direction=float(spec["dir"]))
            res["leadlag"] = EXTERNAL_ANCHOR_SPECS.get(
                {"USDTWD": "TWD"}.get(name, name), {}).get("leadlag", "")
            raw.append(res)
        # 正交化 ⟂ (foreign + champion) + 對 champion 共線
        resid = orthogonalize(sig, controls)
        rr = evaluate_anchor(resid, m["fwd1"].set_axis(m.index), bull,
                             f"{name} ⟂(foreign+champ)", n_trials, direction=float(spec["dir"]))
        d = pd.concat([sig.rename("s"), m["champ"].rename("c"), m["foreign"].rename("f")], axis=1).dropna()
        rr["corr_champ"] = d["s"].corr(d["c"])
        rr["corr_foreign"] = d["s"].corr(d["f"])
        orth.append(rr)

    # VIX 當 L0 恐慌 gate (事件式,同曝險 permutation)
    vix_z = rz(m["VIX"], 60)
    gate_rows = []
    for gname, gmask, tgt in (("VIX_z60>+1.5", vix_z > 1.5, "fwd1"),
                              ("VIX_z60>+2.0", vix_z > 2.0, "fwd1"),
                              ("VIX>30", m["VIX"] > 30, "fwd5"),
                              ("VIX>40", m["VIX"] > 40, "fwd5")):
        gm = gmask.fillna(False).values
        f = m[tgt].values
        ok = gm & ~np.isnan(f)
        k = int(ok.sum())
        fv = f[~np.isnan(f)]
        if k < 20:
            gate_rows.append({"gate": gname, "target": tgt, "n": k, "note": "n_lt_20"})
            continue
        obs = np.nanmean(f[gm])
        null = np.array([fv[RNG.choice(len(fv), k, replace=False)].mean() for _ in range(5000)])
        gate_rows.append({"gate": gname, "target": tgt, "n": k,
                          "mean_in_pct": obs * 100, "mean_out_pct": np.nanmean(f[~gm]) * 100,
                          "perm_p_low": float((null <= obs).mean())})
    return pd.DataFrame(raw), pd.DataFrame(orth), pd.DataFrame(gate_rows)


# ----------------------------------------------------------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="國際總經錨維度研究 (SOX/DXY/US10Y/VIX/Fed/TWD)")
    ap.add_argument("--with-external", action="store_true",
                    help="跑 STRAND B (VIX/DXY/US10Y/USDTWD 真資料;讀本地 parquet,缺則自動抓)")
    ap.add_argument("--refetch", action="store_true", help="重抓 Yahoo 外部資料覆寫 parquet")
    ap.add_argument("--fred-key", default=None, help="(保留) FRED api_key")
    args = ap.parse_args()

    pd.set_option("display.float_format", lambda x: f"{x:+.3f}" if isinstance(x, float) else str(x))
    pd.set_option("display.width", 200)

    print("=" * 78)
    print("STRAND A — SOX / SMH / 台積 ADR 隔夜 → TAIEX  (LOCAL, 實跑)")
    print("=" * 78)
    m = load_local()
    print(f"對齊後可用 {m['SOX_ret'].notna().sum()} 個 TW 交易日  "
          f"({m['date'].min().date()} → {m['date'].max().date()})\n")
    a = run_strand_a(m)
    show = ["signal", "IC_IS", "IC_OOS", "IS_Sharpe", "OOS_Sharpe",
            "OOS_perm_p", "OOS_DSR", "OOS_Sharpe_bull", "OOS_Sharpe_bear", "exposure"]
    print(a[show].to_string(index=False))
    a.to_csv(OUT / "global_macro_metrics.csv", index=False)
    print(f"\n-> {OUT/'global_macro_metrics.csv'}")

    print("\n解讀 (STRAND A):")
    print("  · gap[priced]     欄位高相關是機械式的 —— 開盤那一刻 SOX 資訊已反映,不可交易。")
    print("  · intraday[residual] 才是真 edge 區;⟂(mom20+foreign) 列檢驗它是否只是台股自身動能/外資流的偽裝。")
    print("  · OOS_DSR<0.95 且 bear regime 失效 → 定性為『開盤 gap 前兆 / L0 風險背景』,非獨立 alpha。")

    if args.with_external:
        print("\n" + "=" * 78)
        print("STRAND B — VIX / DXY / US10Y / USDTWD  (真資料, Yahoo v8 chart)")
        print("=" * 78)
        panel = pd.read_parquet(PANEL)
        panel["date"] = pd.to_datetime(panel["date"])
        raw, orth, gate = run_strand_b(panel, refetch=args.refetch)
        showb = ["signal", "IC_IS", "IC_OOS", "IS_Sharpe", "OOS_Sharpe",
                 "OOS_perm_p", "OOS_DSR", "OOS_Sharpe_bull", "OOS_Sharpe_bear"]
        print("\n[連續因子 → TAIEX 前瞻報酬]")
        print(raw[showb].to_string(index=False))
        print("\n[正交化 ⟂ (foreign 現貨流 + champion fut_foreign_oi Δ5d)]")
        print(orth[["signal", "IC_IS", "IC_OOS", "OOS_Sharpe", "OOS_perm_p",
                    "OOS_DSR", "corr_champ", "corr_foreign"]].to_string(index=False))
        print("\n[VIX 當 L0 恐慌 gate — 事件式,同曝險 permutation]")
        print(gate.to_string(index=False))
        pd.concat([raw.assign(block="raw"), orth.assign(block="orth")], ignore_index=True) \
            .to_csv(OUT / "global_macro_strandB_metrics.csv", index=False)
        gate.to_csv(OUT / "global_macro_vix_gate.csv", index=False)
        print(f"\n-> {OUT/'global_macro_strandB_metrics.csv'}  ·  {OUT/'global_macro_vix_gate.csv'}")
    else:
        print("\n[STRAND B 未執行] 加 --with-external 跑 VIX/DXY/US10Y/USDTWD 真資料 (Yahoo)。")


if __name__ == "__main__":
    main()
