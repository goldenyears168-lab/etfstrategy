"""ETF 申贖 / 受益人數 / 折溢價 維度 — 證偽式研究(真資料已跑 S1+S3).

真資料: S1(申贖 flow via SHOUT) + S3(受益人數/人均持有) 由 FinMind
`TaiwanStockHoldingSharesPer` 週頻回填 2018→2026、8 檔主要 ETF, 落地
`data/research/dashboard/etf_flows_data.parquet`(先跑 etf_flows_fetch.py)。
直接執行 = 跑真 ETF 研究(real_study); `--selftest` = 舊機器自檢(佔位訊號)。
S2(折溢價)需官方 NAV, FinMind 無 dataset(422 實測), 待補。
Verdict: 大盤層非真 alpha(IS IC≈0, OOS 唯一正值 perm p=0.077 未過、與 champion
正相關非鏡像); 動能偽裝已排除(pcMom≈0)。詳見 reports/.../etf_flows.md。

維度（同一套 ETF 一/二級市場套利機制的三個觀測面，量測「非基本面/笨錢需求」擁擠）：
  S1  申贖 flow      etf_flow = ΔSHOUT_t / SHOUT_{t-1}   (領先, CONTRARIAN 反向)
        SHOUT = 在外受益權單位數。申購→單位增(正flow)、贖回→單位減(負flow)。
        Brown-Davies-Ringgenberg(2021 RoF): short-high-flow/long-low-flow 月超額
        1.1-2.0%，1-6 月 reversal。方向反向：高申購=非基本面買壓→後續回吐。
  S2  折溢價       prem = (Price_t - NAV_t) / NAV_t     (前兆/超短線, mean-revert)
        高溢價後 1-5 日回吐→短空；折價→短多。單一熱門 ETF 溢價擴大=froth 前兆。
        亦當 S1 的閘門：溢價日的申購 / 折價日的贖回才有 flow 訊號（Xu 2022 FM）。
  S3  受益人數     holders / 人均持有 = SHOUT / holders  (落後→極端處轉前兆, 週頻)
        受益人數創高 + 人均持有下降 = 散戶擁擠頂部前兆（2023-26 高股息 ETF 教科書案例）。

聚合到大盤(TAIEX)用法：把全市場(或高股息類) ETF 的 net flow / 受益人數增速加總成
一條「散戶擁擠指數」，當 L0 regime 的**反向情緒 overlay**：極端擁擠 → 對 champion
多頭訊號打折。定位=champion(外資期貨 positioning=聰明錢領先)的鏡像/對立腳。

DATA SOURCE（誠實區分 本地 vs 需接）
  本地已有（不足以直接建因子）：
    - stock_daily_bars: 僅 0050/0056 兩檔 ETF 日 K，且窗短（2025-05-26→2026-07-21）。
    - etf_holdings_meta: 8 檔「主動式」ETF 的 nav+holding_count，nav 是「持股加總淨值」
      (source=ezmoney/kgifund) 非官方每日淨值，且與上面兩檔價格不重疊，窗短(2025-05→)。
      holding_count = 持股「檔數」不是受益人數。→ 折溢價無法乾淨計算。
    - etf_daily_signal_snapshot: 6 檔 ETF 三大法人淨買超(186列, 2026-05→07)，僅輔助。
    - panel.parquet: 大盤層，無任何 ETF flow/受益人數/折溢價欄。
  需接新資料源（三訊號本地都缺，需 backfill）：
    - S1 申贖/SHOUT: FinMind 無專用 dataset（TaiwanStockShareholding 的
      NumberOfSharesIssued 針對個股非 ETF 單位）。需爬 SITCA(投信投顧公會)
      每日 ETF 規模/單位數，或各投信每日公告；ΔSHOUT 即 net 申贖。
    - S2 折溢價/官方NAV: FinMind 無 ETF NAV/折溢價 dataset(已查證)。官方每日淨值走
      TPEX ETF 專區 info.tpex.org.tw/ETF/ 或 TWSE ETF 訊息中心 / 各投信官網；
      盤中 IOPV 由 TWSE 每 15 秒發布。價格分子用 stock_daily_bars(需補全 universe)。
    - S3 受益人數: 兩條路——
      (a) FinMind `TaiwanStockHoldingSharesPer`(集保股權分散,含 ETF 代號,週頻,2010起)
          → 各分級 people 加總 = 總受益人數（最省事、可回溯；cache_holding_shares_expert.py
          已證此 dataset 可抓，惟本檔目前 FINMIND_TOKEN 未設）。
      (b) 集保 TDCC opendata(data.gov.tw dataset 11452) / SITCA etf_beneficial.aspx。

建議 scaffold 順序：先用 (a) TaiwanStockHoldingSharesPer 建「ETF 受益人數 + 人均持有」
週頻面板（最低成本），再補 SITCA 單位數(申贖)、官方 NAV(折溢價)，三者對齊成
etf_sentiment_panel，套本檔 eval_signal 驗證。

HOW TO RUN
  cd /Users/jackm4/Documents/ETF/股票研究 && source .venv/bin/activate
  export FINMIND_TOKEN=...            # S3 週頻受益人數抓取需要（S1/S2 需先寫爬蟲）
  python scripts/research/dashboard/etf_flows_study.py            # 機器自檢 + scaffold 狀態
  python scripts/research/dashboard/etf_flows_study.py --probe    # 額外試抓 S3 受益人數(需 token)

METHODOLOGY（專案標準，證偽優先）
  - forward return: fwd(t) = ix_close[t+1]/ix_open[t+1]-1（訊號盤後已知，最早可交易=次開盤）。
    週頻訊號(S3)前推到「實際公布日」對齊後，fwd 從下一可交易日起算。cost 4 bps/換手。
  - 方向由 IN-SAMPLE IC 符號固定（不偷看 OOS）。反指標訊號 direction 天生為負。
  - IS/OOS 70/30 時序切分。permutation vs 同曝險隨機擇時。
  - regime-conditioning: 另報 bull(ix_close>MA200 且 MA200 上彎) 限定 OOS。
  - Deflated-Sharpe: 印出試驗次數供 DSR 套用（單一市場序列 + 少數訊號，OOS 很薄，
    任何正值只當弱先驗）。
  - **共線防呆(最高危)**: 受益人數/申購幾乎必跟漲，是動能偽裝。任何採信前必先對
    價格動能(過去 20/60 日報酬)做偏相關/殘差化，證明 flow 殘差仍有增量 IC，否則
    就是重新發明 TSMOM。本檔 partial_corr() 提供此檢定接口。

非投資建議 — research scaffold only.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
DB = ROOT / "data" / "stocks.db"
CACHE = ROOT / "data" / "research" / "dashboard"
CACHE.mkdir(parents=True, exist_ok=True)
OUT = ROOT / "reports" / "research" / "dashboard-completeness"

COST_BPS = 4.0
ANN = np.sqrt(252)
RNG = np.random.default_rng(7)
START = date(2018, 6, 1)
END = date.today()


# ---------------------------------------------------------------- helpers ----
def rz(s: pd.Series, w: int) -> pd.Series:
    return (s - s.rolling(w, min_periods=w).mean()) / s.rolling(w, min_periods=w).std()


def sharpe(x) -> float:
    x = pd.Series(x).dropna()
    return x.mean() / x.std() * ANN if len(x) > 20 and x.std() > 0 else np.nan


def lf(bull, day_ret, cost=COST_BPS):
    pos = pd.Series(bull, index=day_ret.index).astype(float).fillna(0.0)
    return pos * day_ret - pos.diff().abs().fillna(pos.abs()) * (cost / 1e4)


def perm_p(bull, day_ret, mask, n=2000):
    dr = day_ret[mask].reset_index(drop=True)
    pos = pd.Series(bull, index=day_ret.index)[mask].astype(float).reset_index(drop=True)
    v = dr.notna(); dr, pos = dr[v], pos[v]
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


def partial_corr(sig: pd.Series, fwd: pd.Series, mom: pd.Series) -> float:
    """共線防呆: sig 與 fwd 在控制價格動能 mom 後的偏相關.

    近 0 = 訊號的預測力其實來自動能(動能偽裝);顯著非 0 才是增量資訊。
    """
    d = pd.DataFrame({"s": sig, "f": fwd, "m": mom}).dropna()
    if len(d) < 50:
        return np.nan
    s_res = d["s"] - np.polyval(np.polyfit(d["m"], d["s"], 1), d["m"])
    f_res = d["f"] - np.polyval(np.polyfit(d["m"], d["f"], 1), d["m"])
    return float(np.corrcoef(s_res, f_res)[0, 1])


def bull_regime(p: pd.DataFrame) -> pd.Series:
    ma = p["ix_close"].rolling(200, min_periods=200).mean()
    return (p["ix_close"] > ma) & (ma > ma.shift(20))


# ----------------------------------------------------- generic evaluator ----
def eval_signal(name, raw_val, p, day_ret, ismask, oos, champ_bull, champ_ret,
                contrarian: bool):
    """把任一訊號序列(對齊 p.index)餵進專案標準證偽管線。資料同步後直接呼叫。"""
    v = pd.Series(raw_val, index=p.index)
    d = pd.DataFrame({"v": v, "f": day_ret}).dropna()
    if len(d) < 100:
        return {"signal": name, "status": "INSUFFICIENT_DATA", "n": int(len(d))}
    im = ismask.reindex(d.index).fillna(False)
    ic_is = d[im]["v"].corr(d[im]["f"])
    direction = np.sign(ic_is) if pd.notna(ic_is) and ic_is != 0 else 1.0
    bull = (direction * v) > 0
    r = lf(bull, day_ret)
    breg = bull_regime(p)
    r_bull = lf(bull & breg, day_ret)
    _, pv = perm_p(bull, day_ret, oos)
    comb = (champ_bull.astype(float) + bull.astype(float)) / 2
    cr = comb * day_ret - comb.diff().abs().fillna(comb.abs()) * COST_BPS / 1e4
    corr_champ = pd.DataFrame({"a": r, "b": champ_ret}).dropna().corr().iloc[0, 1]
    mom = p["ix_close"].pct_change(20)
    pcorr = partial_corr(v, day_ret, mom)
    return {
        "signal": name, "status": "OK", "contrarian_prior": contrarian,
        "dir_from_IS": int(direction), "IC_IS": round(float(ic_is), 4),
        "OOS_Sharpe": round(float(sharpe(r[oos])), 3),
        "OOS_bull_only": round(float(sharpe(r_bull[oos])), 3),
        "OOS_perm_p": None if pd.isna(pv) else round(pv, 3),
        "partial_corr_vs_mom": None if pd.isna(pcorr) else round(pcorr, 4),
        "corr_vs_champ": round(float(corr_champ), 3),
        "combo50_OOS": round(float(sharpe(cr[oos])), 3),
        "exposure": round(float(bull.reindex(day_ret.index).fillna(False).mean()), 3),
        "n": int(len(d)),
    }


# --------------------------------------------------- data-source scaffolds ----
def fetch_beneficiary_count(etf_code: str, start: date = START, end: date = END) -> pd.DataFrame:
    """S3 受益人數 scaffold — FinMind TaiwanStockHoldingSharesPer 各級距 people 加總.

    週頻(集保每週五結算)。回傳 columns=[date, etf_code, holders]。需 FINMIND_TOKEN。
    dataset 驗證來源: scripts/research/cache_holding_shares_expert.py 已用同 dataset 抓個股。
    備援: 集保 TDCC opendata data.gov.tw dataset 11452 / SITCA etf_beneficial.aspx。
    """
    from finmind_client import fetch_finmind  # noqa: WPS433

    frames = []
    for yr in range(start.year, end.year + 1):
        s = max(start, date(yr, 1, 1)); e = min(end, date(yr, 12, 31))
        raw = fetch_finmind("TaiwanStockHoldingSharesPer", etf_code, s, e, timeout=120)
        if raw:
            frames.append(pd.DataFrame(raw))
    if not frames:
        return pd.DataFrame(columns=["date", "etf_code", "holders"])
    d = pd.concat(frames, ignore_index=True)
    # 排除「合計」列（若有），逐級距 people 加總 = 總受益人數
    ppl_col = "people" if "people" in d.columns else "HoldingSharesLevel"  # 依實際欄名
    if "HoldingSharesLevel" in d.columns:
        d = d[~d["HoldingSharesLevel"].astype(str).str.contains("合計|Total", na=False)]
    grp = d.groupby("date")[ppl_col].sum().rename("holders")
    out = grp.reset_index()
    out["etf_code"] = etf_code
    out["date"] = out["date"].astype(str).str[:10]
    return out[["date", "etf_code", "holders"]]


def fetch_units_outstanding_scaffold(etf_code: str) -> pd.DataFrame:
    """S1 申贖/SHOUT scaffold — 需自建爬蟲，FinMind 無對應 dataset.

    來源選項:
      - SITCA 投信投顧公會 每日 ETF 規模/單位數 (需爬 sitca.org.tw 表單 POST)。
      - 各投信官網每日公告(如 元大/富邦 ETF 每日申購買回申報單位數)。
      - TWSE ETF 每日申購買回清單 rwd/zh/ETF/etfInout (含發行單位數)。
    回傳目標 columns=[date, etf_code, shout]；ΔSHOUT/SHOUT_{t-1} = etf_flow。
    """
    raise NotImplementedError(
        "SHOUT 需先寫 SITCA / 投信 / TWSE etfInout 爬蟲；FinMind 無此 dataset。"
        " 見 docstring 來源清單。"
    )


def fetch_official_nav_scaffold(etf_code: str) -> pd.DataFrame:
    """S2 官方每日淨值 scaffold — 需接 TPEX/TWSE ETF 專區，FinMind 無 NAV dataset.

    來源選項:
      - TPEX ETF 專區 info.tpex.org.tw/ETF/ (上櫃 ETF 每日淨值)。
      - TWSE ETF 訊息中心 / 各投信官網 PremiumDiscount 頁。
      - 盤中 IOPV: TWSE 每 15 秒發布(即時預估淨值，≠收盤官方 NAV)。
    折溢價 prem = (Price/NAV - 1)；Price 用 stock_daily_bars（需補全 ETF universe）。
    """
    # gap-fill 2026-07-30 窮盡調查(見 reports/.../etf_flows.md §7): 免費/已授權管道皆無台股歷史 NAV。
    #   FinMind: 103 dataset enum 無任何 NAV(僅 ActiveETFHolding 非 NAV);
    #   FinMind TaiwanStockTotalReturnIndex 僅 TAIEX、無台灣50指數(無法建 0050 proxy);
    #   TEJ E-SHOP: 無 NAV 表, EWIPRCD 僅 16 檔寬基指數無台灣50;
    #   TWSE OpenAPI: 僅 ETFRank(交易戶數)+當日快照無歷史; rwd/etfInout 回 HTML;
    #   TPEX OpenAPI: 本環境 SSL CERTIFICATE_VERIFY_FAILED;
    #   本地 etf_holdings_meta.nav 僅主動式 ETF(00980A…)與本宇宙零交集。
    # => S2 折溢價 verdict=待補。唯一殘存路徑=爬玩股網/MoneyDJ HTML(易碎、未做)。
    raise NotImplementedError(
        "官方每日 NAV 不可得(FinMind/TEJ/TWSE-OpenAPI/TPEX/本地 DB 皆無, 已窮盡 2026-07-30)。"
        " 見 reports/research/dashboard-completeness/etf_flows.md §7。S2=待補。"
    )


# ------------------------------------------- local availability inspection ----
def inspect_local() -> dict:
    """誠實盤點本地 ETF 資料，回傳可用性判定。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        bars = pd.read_sql(
            "SELECT stock_id, COUNT(*) c, MIN(trade_date) lo, MAX(trade_date) hi "
            "FROM stock_daily_bars WHERE stock_id IN "
            "('0050','0056','00878','00919','00929','00713') GROUP BY stock_id", con)
        meta = pd.read_sql(
            "SELECT COUNT(*) rows, COUNT(DISTINCT etf_code) etfs, "
            "SUM(nav IS NOT NULL) nav_rows, MIN(snapshot_date) lo, MAX(snapshot_date) hi "
            "FROM etf_holdings_meta", con).iloc[0].to_dict()
    finally:
        con.close()
    return {"etf_price_bars": bars, "etf_holdings_meta": meta}


# ------------------------------------------------------------------ study ----
def machinery_self_test(p, day_ret, ismask, oos, champ_bull, champ_ret):
    """證明評估管線可運作(NOT ETF 結果)。用 panel margin_bal 當佔位訊號跑一遍。

    這裡的數字是「機器自檢」，僅示範 IC/IS-OOS/permutation/regime/共線 函式接得上；
    ETF 訊號(S1/S2/S3)需資料同步後才有真結果。
    """
    placeholder = rz(p["margin_bal"], 60)  # 佔位：融資餘額 z60（落後代理，非 ETF 訊號）
    r = eval_signal("PLACEHOLDER margin_bal_z60 (self-test, NOT ETF)", placeholder,
                    p, day_ret, ismask, oos, champ_bull, champ_ret, contrarian=True)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="額外試抓 S3 受益人數(TaiwanStockHoldingSharesPer, 需 FINMIND_TOKEN)")
    ap.add_argument("--selftest", action="store_true",
                    help="只跑評估管線自檢(佔位訊號), 不跑真 ETF 研究")
    args = ap.parse_args()

    print("=" * 78)
    print("ETF 申贖/受益人數/折溢價 維度 — 資料盤點 + 評估管線自檢")
    print("=" * 78)

    inv = inspect_local()
    print("\n[本地資料盤點]")
    print("  stock_daily_bars ETF 價格:")
    print(inv["etf_price_bars"].to_string(index=False) if not inv["etf_price_bars"].empty
          else "    (無)")
    print(f"  etf_holdings_meta: {inv['etf_holdings_meta']}")
    print("  → 判定: 折溢價需官方NAV(缺乾淨源)、申贖需SHOUT(本地/FinMind皆無)、"
          "受益人數本地無。implementable_now = FALSE (scaffold)。")

    p = pd.read_parquet(PANEL).sort_values("date").reset_index(drop=True)
    p["date"] = p["date"].astype(str).str[:10]
    day_ret = (p["ix_close"] / p["ix_open"] - 1.0).shift(-1)
    n = len(p); is_cut = int(n * 0.7)
    ismask = pd.Series(p.index < is_cut, index=p.index)
    oos = pd.Series(p.index >= is_cut, index=p.index)
    champ_bull = rz(p["fut_foreign_oi"], 60) > 0
    champ_ret = lf(champ_bull, day_ret)

    print(f"\n[底層基準] panel {p['date'].iloc[0]}..{p['date'].iloc[-1]} n={n} "
          f"IS/OOS cut @ {p['date'].iloc[is_cut]}")
    print(f"  B&H OOS Sharpe   = {sharpe(day_ret[oos]):+.3f}")
    print(f"  champion OOS     = {sharpe(champ_ret[oos]):+.3f}  "
          f"(fut_foreign_oi_z60>0；ETF 情緒訊號將當其反向 overlay)")

    if args.selftest:
        print("\n[評估管線自檢 — 佔位訊號，非 ETF 結果]")
        st = machinery_self_test(p, day_ret, ismask, oos, champ_bull, champ_ret)
        for k, v in st.items():
            print(f"  {k:24s}: {v}")
        print("\n非投資建議 — research scaffold only.")
        return

    # ---- 真資料研究 (S1 flow + S3 holders, 來自 etf_flows_data.parquet) ----
    real_study(p, day_ret, ismask, oos, champ_bull, champ_ret)
    print("\nS2 折溢價: FinMind 無 NAV dataset(已查證 422 enum)，待補 TPEX/投信官方每日淨值。")
    print("非投資建議 — research only.")


# --------------------------------------------------- real ETF-flow study ----
ETF_DATA = CACHE / "etf_flows_data.parquet"
CORE4 = ["0050", "006208", "0056", "00713"]  # 2018 起連續, 無成分跳動
PUBLISH_LAG_DAYS = 4  # 集保週五結算 → 保守假設次週可交易後才知


def build_weekly_signals() -> pd.DataFrame:
    """由 ETF holders/SHOUT 週頻面板建大盤散戶擁擠訊號.

    回傳 index=settle_date(週) 的 DataFrame, 欄:
      hg_core   : CORE4 總受益人數週增率 z26 (擁擠加速)
      flow_core : CORE4 總在外單位週增率 z26 (淨申購=非基本面買壓)
      hg_all    : 全宇宙 per-ETF 受益人數增率的等權均值 z26
      flow_all  : 全宇宙 per-ETF 單位增率的等權均值 z26
    方向先驗皆 CONTRARIAN (擁擠↑ → 後續回吐)。
    """
    d = pd.read_parquet(ETF_DATA)
    d = d.sort_values(["etf_code", "date"])
    # CORE4 aggregate (固定成分, 加總後算增率)
    core = d[d["etf_code"].isin(CORE4)]
    agg = core.groupby("date")[["holders", "shout"]].sum()
    agg = agg[core.groupby("date")["etf_code"].nunique() == len(CORE4)]  # 4 檔齊全的週
    hg_core = agg["holders"].pct_change()
    flow_core = agg["shout"].pct_change()
    # ALL universe: per-ETF 增率再等權平均 (含高息新兵擁擠, 但用中位數抗新上市爆量)
    d["hg"] = d.groupby("etf_code")["holders"].pct_change()
    d["fl"] = d.groupby("etf_code")["shout"].pct_change()
    hg_all = d.groupby("date")["hg"].median()
    flow_all = d.groupby("date")["fl"].median()
    out = pd.DataFrame({
        "hg_core": hg_core, "flow_core": flow_core,
        "hg_all": hg_all, "flow_all": flow_all,
    }).sort_index()
    # z26 去趨勢 (結構性長期申購潮 → 用季度窗去掉 level, 只留相對加速)
    for c in list(out.columns):
        out[c] = rz(out[c], 26)
    return out


def map_weekly_to_panel(weekly: pd.Series, panel: pd.DataFrame) -> pd.Series:
    """週頻訊號 → 日頻 panel.index, 用 publish_date backward merge_asof (無前視)."""
    w = weekly.dropna().reset_index()
    w.columns = ["settle", "val"]
    w["settle"] = pd.to_datetime(w["settle"])
    w["publish"] = w["settle"] + pd.Timedelta(days=PUBLISH_LAG_DAYS)
    w = w.sort_values("publish")
    pnl = pd.DataFrame({"i": panel.index, "dt": pd.to_datetime(panel["date"])}).sort_values("dt")
    m = pd.merge_asof(pnl, w[["publish", "val"]], left_on="dt", right_on="publish",
                      direction="backward")
    return m.set_index("i")["val"].reindex(panel.index)


def real_study(p, day_ret, ismask, oos, champ_bull, champ_ret):
    if not ETF_DATA.exists():
        print("\n[真資料研究] etf_flows_data.parquet 缺 — 先跑 etf_flows_fetch.py")
        return
    wk = build_weekly_signals()
    print(f"\n[真資料研究] ETF 面板 {ETF_DATA.name}  週訊號 {wk.index.min()}..{wk.index.max()}")
    print(f"  宇宙: CORE4={CORE4} (固定成分) + 全8檔(含高息新兵, 用中位數)")
    print(f"  訊號: 受益人數增率 z26 / 在外單位(SHOUT)增率 z26 → 大盤散戶擁擠指數 (CONTRARIAN)")
    print(f"  對齊: 集保週五結算 +{PUBLISH_LAG_DAYS}d publish, backward merge_asof (無前視)")
    n_trials = len(wk.columns)
    print(f"\n{'signal':16s} {'dir':>3} {'IC_IS':>7} {'OOS_Sh':>7} {'bull':>6} "
          f"{'permP':>6} {'pcMom':>7} {'vsChmp':>7} {'combo':>6} {'expo':>5} {'n':>5}")
    results = {}
    for col in wk.columns:
        sig = map_weekly_to_panel(wk[col], p)
        r = eval_signal(f"etf_{col}", sig, p, day_ret, ismask, oos,
                        champ_bull, champ_ret, contrarian=True)
        results[col] = r
        if r.get("status") != "OK":
            print(f"{col:16s} {r}")
            continue
        print(f"{col:16s} {r['dir_from_IS']:>3d} {r['IC_IS']:>7.3f} "
              f"{r['OOS_Sharpe']:>7.3f} {r['OOS_bull_only']:>6.2f} "
              f"{str(r['OOS_perm_p']):>6} {str(r['partial_corr_vs_mom']):>7} "
              f"{r['corr_vs_champ']:>7.3f} {r['combo50_OOS']:>6.2f} "
              f"{r['exposure']:>5.2f} {r['n']:>5d}")
    print(f"\n  DSR 參照: n_trials={n_trials} (4 訊號變體), OOS 樣本薄 → 任何正值僅弱先驗。")
    print(f"  共線防呆: pcMom=控制大盤20日動能後偏相關(近0=動能偽裝); "
          f"vsChmp=與 champion 報酬相關(負=鏡像互補如預期)。")
    return results


if __name__ == "__main__":
    main()
