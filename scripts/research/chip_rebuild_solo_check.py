#!/usr/bin/env python3
"""chip-orthogonal-rebuild 雙盲實作 B 方：八因子（F1~F8）單獨檢定。

規格 SSOT：config/research.yaml topic "chip-orthogonal-rebuild"。
本實作獨立自建（禁讀 A 方 chip_rebuild_panel.py / chip_rebuild_solo_test.py /
panel.pkl）；分點聚合使用預先許可的 chip-street-canon top15_daily.pkl 快取，
其餘一律從原始資料表自查。

因子定義沿用既有 pipeline（scripts/research/stock_chip_snapshot.py 的
z1/zp/zu/zf/z6、run_chip_daily_brief.py 的散戶持股來源），不重新發明：
  F1 z1   Δ借券賣出餘額 相對自身近60日 z（rolling 60, min_periods=30）
  F2 zp   借券佔股本(short_limit*4) 近243日分位（min_periods=60）→(pct-0.5)*4
  F3 zu   Δ券源使用率 bal/(bal+next_limit) 自身60日 z
  F4 zf   借券費率(deal_type='ALL' fee_rate_vw) 自身近60筆分位（min_periods=10）
  F5 z6   分點(買超家數-賣超家數)/家數 當日橫斷面分位；僅 2024-07-01~2026-07-16
  F6 retail 集保<50張(級距1-8)持股比水位；週頻 PIT=資料日後第2交易日才可行動
  F7 margin Δ融資餘額/股本（來源去重 twse_mi_margn 優先）
  F8 inst  三大法人合計買超/成交量（來源去重 twse_t86 優先）

共同規格：
  宇宙 close>=10 且 20日均量>300,000 股；窗 2024-07-01~2026-08-26（F5 至 07-16）
  報酬 open(T+1)→close(T+1) 可執行口徑（T+1=次一交易日，pivot 在官方日曆上
  自動保證「下一列=次一交易日」，缺日即 NaN）
  交易日曆＝stock_daily_bars 當日官方來源(twse_mi_index/tpex_daily/finmind)
  列數>500 的日子（排除 2026-07-10 幽靈日）
  風險中性＝Fama-MacBeth 逐日橫斷面回歸 控 vol60/當日跳空(報酬日)/週轉率，
  t=日係數序列 Newey-West(lag 5)；另報未控五分位價差(Q5-Q1 %/日)與 NW t
  存活判準 |t|>=3

用法::
    PYTHONPATH=src .venv/bin/python scripts/research/chip_rebuild_solo_check.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports" / "research" / "chip-orthogonal-rebuild"
CACHE_TOP15 = ROOT / "reports" / "research" / "chip-street-canon" / "cache" / "top15_daily.pkl"

WIN_START, WIN_END = "2024-07-01", "2026-08-26"
F5_END = "2026-07-16"
PRICE_LOAD_START = "2024-01-02"     # vol60 / vol20 暖身
SBL_LOAD_START = "2023-06-01"       # zp 243 日窗暖身
OFFICIAL = ("twse_mi_index", "tpex_daily", "finmind")
BAR_PRIORITY = {"twse_mi_index": 0, "tpex_daily": 1, "finmind": 2, "yfinance": 3}
MIN_CLOSE = 10.0
MIN_VOL20 = 300_000.0               # 股
MIN_N_DAY = 30                      # 單日橫斷面最低檔數
NW_LAG = 5

notes: dict[str, str] = {}
dedup_report: dict[str, dict] = {}


def connect():
    return sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)


# ---------------------------------------------------------------- 日曆與價格

def load_calendar(c, start: str) -> list[str]:
    df = pd.read_sql_query(
        """SELECT trade_date, COUNT(*) n FROM stock_daily_bars
            WHERE source IN (?,?,?) AND trade_date BETWEEN ? AND ?
            GROUP BY trade_date HAVING n > 500 ORDER BY trade_date""",
        c, params=(*OFFICIAL, start, WIN_END))
    cal = df.trade_date.tolist()
    assert "2026-07-10" not in cal, "幽靈交易日 2026-07-10 沒被排除"
    return cal


def load_price_panels(c, cal: list[str]):
    raw = pd.read_sql_query(
        """SELECT stock_id, trade_date, open, close, volume, source
             FROM stock_daily_bars WHERE trade_date BETWEEN ? AND ?""",
        c, params=(PRICE_LOAD_START, WIN_END))
    n0 = len(raw)
    raw = raw[raw.trade_date.isin(set(cal))]
    n1 = len(raw)
    raw["prio"] = raw.source.map(BAR_PRIORITY).fillna(9)
    raw = (raw.sort_values(["stock_id", "trade_date", "prio"])
              .drop_duplicates(["stock_id", "trade_date"], keep="first"))
    n2 = len(raw)
    dedup_report["stock_daily_bars"] = {
        "rows_loaded": n0, "rows_on_calendar": n1, "rows_after_dedup": n2}

    piv = lambda col: (raw.pivot(index="trade_date", columns="stock_id", values=col)
                          .reindex(cal))
    P = {"open": piv("open"), "close": piv("close"), "volume": piv("volume")}

    close, open_, vol = P["close"], P["open"], P["volume"]
    P["ret_exec"] = close.shift(-1) / open_.shift(-1) - 1          # T+1 open→close
    P["gap"] = open_.shift(-1) / close - 1                          # 報酬日跳空
    ret_cc = close / close.shift(1) - 1
    P["vol60"] = ret_cc.rolling(60, min_periods=40).std()
    vol20 = vol.rolling(20, min_periods=15).mean()
    P["turnover"] = vol / vol20                                     # 缺股數 → 均量比
    P["universe"] = (close >= MIN_CLOSE) & (vol20 > MIN_VOL20)
    return P


# ---------------------------------------------------------------- Newey-West

def nw_tstat(x: np.ndarray, lag: int = NW_LAG) -> tuple[float, float]:
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 20:
        return np.nan, np.nan
    m = x.mean()
    e = x - m
    g0 = (e @ e) / n
    s = g0
    for k in range(1, lag + 1):
        gk = (e[k:] @ e[:-k]) / n
        s += 2 * (1 - k / (lag + 1)) * gk
    se = np.sqrt(max(s, 1e-18) / n)
    return m, m / se


# ---------------------------------------------------------------- 因子建構

def sbl_factors(c, cal_full: list[str]) -> dict[str, pd.DataFrame]:
    """F1 z1 / F2 zp / F3 zu / F4 zf —— 完全沿用 stock_chip_snapshot.market_scores。

    ``cal_full``：自 SBL_LOAD_START 起的官方日曆（含暖身段），用於幽靈日過濾
    與 diff 相鄰性驗證。
    """
    h = pd.read_sql_query(
        """SELECT s.stock_id, s.trade_date, s.sbl_balance, s.sbl_next_limit,
                  s.short_limit, f.fee_rate_vw
             FROM stock_short_interest_daily s
             LEFT JOIN (SELECT stock_id, trade_date, fee_rate_vw
                          FROM stock_sbl_fee_daily WHERE deal_type='ALL') f
               ON f.stock_id=s.stock_id AND f.trade_date=s.trade_date
            WHERE s.trade_date BETWEEN ? AND ?""",
        c, params=(SBL_LOAD_START, WIN_END))
    n0 = len(h)
    h = h[h.trade_date.isin(set(cal_full))]
    n1 = len(h)
    h = h.drop_duplicates(["stock_id", "trade_date"], keep="first")
    n2 = len(h)
    dedup_report["stock_short_interest_daily"] = {
        "rows_loaded": n0, "rows_on_calendar": n1, "rows_after_dedup": n2}

    h = h.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    h["shares"] = (h.short_limit * 4).replace(0, np.nan)
    h["sbl_pct"] = h.sbl_balance / h.shares
    h["util"] = h.sbl_balance / (h.sbl_balance + h.sbl_next_limit)
    g = h.groupby("stock_id", group_keys=False)
    h["d_sbl"] = g.sbl_balance.diff()
    h["d_util"] = g.util.diff()
    # diff 斷檔驗證：前一列必須與本列在官方日曆上相鄰，不相鄰設 NaN
    idx = {d: i for i, d in enumerate(cal_full)}
    h["cal_i"] = h.trade_date.map(idx)
    prev_i = g.cal_i.shift(1)
    noncontig = (h.cal_i - prev_i) != 1
    h.loc[noncontig, ["d_sbl", "d_util"]] = np.nan

    g = h.groupby("stock_id", group_keys=False)

    def zself(col, win=60):
        mu = g[col].transform(lambda x: x.rolling(win, min_periods=30).mean())
        sd = g[col].transform(lambda x: x.rolling(win, min_periods=30).std())
        return (h[col] - mu) / sd.replace(0, np.nan)

    h["F1"] = zself("d_sbl")
    h["F3"] = zself("d_util")
    h["F2"] = (g.sbl_pct.transform(
        lambda x: x.rolling(243, min_periods=60).rank(pct=True)) - 0.5) * 4
    h["F4"] = (g.fee_rate_vw.transform(
        lambda x: x.rolling(60, min_periods=10).rank(pct=True)) - 0.5) * 4

    out = {}
    win = h[(h.trade_date >= WIN_START) & (h.trade_date <= WIN_END)]
    for f in ("F1", "F2", "F3", "F4"):
        d = win[["stock_id", "trade_date", f]].dropna()
        out[f] = d.rename(columns={f: "fval"})
    notes["F4"] = "當日無借券成交列為 NaN 剔除（pipeline 合成時 fillna(0)，單因子檢定改剔除）"
    notes["F1-F4"] = "stock_short_interest_daily 僅 TWSE(source=twse)，上櫃不在覆蓋內"
    return out


def branch_factor(cal: list[str]) -> pd.DataFrame:
    """F5 z6：top15_daily.pkl 快取（已驗證 nb/ns/n 與 pipeline net<>0 定義逐檔相等）。"""
    df = pd.read_pickle(CACHE_TOP15)
    n0 = len(df)
    df = df[(df.trade_date >= WIN_START) & (df.trade_date <= F5_END)
            & df.trade_date.isin(set(cal))]
    dedup_report["stock_broker_branch_daily(top15_cache)"] = {
        "rows_loaded": n0, "rows_in_f5_window": len(df)}
    d = df[df.n_branches > 0].copy()
    d["fval"] = (d.n_buy_houses - d.n_sell_houses) / d.n_branches
    notes["F5"] = f"截斷至 {F5_END}（2026-07-17 起 tape 崩壞 median 4 分點/股）"
    return d[["stock_id", "trade_date", "fval"]]


def retail_factor(c, cal: list[str]) -> pd.DataFrame:
    """F6：集保級距1-8(<50張)持股比水位；PIT=as_of 後第2個交易日才可行動。

    報酬口徑為 open(T+1)→close(T+1)，故 as_of 的因子最早掛在
    T = as_of 後第1個交易日（T+1 即第2個交易日＝可行動日）。
    """
    df = pd.read_sql_query(
        """SELECT stock_id, as_of_date, source, level, percent
             FROM stock_holding_dispersion_weekly
            WHERE as_of_date >= '2024-05-01'""", c)
    n0 = len(df)
    lv = {str(i) for i in range(1, 9)}
    agg = (df[df.level.isin(lv)]
           .groupby(["stock_id", "as_of_date", "source"], as_index=False)
           .percent.sum().rename(columns={"percent": "ret_pct"}))
    n1 = len(agg)
    agg["prio"] = np.where(agg.source == "tdcc", 0, 1)
    agg = (agg.sort_values(["stock_id", "as_of_date", "prio"])
              .drop_duplicates(["stock_id", "as_of_date"], keep="first"))
    n2 = len(agg)
    dedup_report["stock_holding_dispersion_weekly"] = {
        "rows_loaded": n0, "stockweek_source_agg": n1, "after_source_dedup(tdcc_first)": n2}
    bad = agg[agg.ret_pct > 101]
    assert len(bad) == 0, f"持股比 >101%（{len(bad)} 檔週），疑似雙計"

    cal_arr = np.array(cal)
    # as_of 後第 1 個交易日（掛因子日 T；T+1=第2交易日=可行動日）
    pos = np.searchsorted(cal_arr, agg.as_of_date.values, side="right")
    keep = pos < len(cal_arr)
    agg = agg[keep].copy()
    agg["signal_date"] = cal_arr[pos[keep]]
    # 同一 signal_date 撞多筆 as_of（連假週）時取最新一筆
    agg = (agg.sort_values("as_of_date")
              .drop_duplicates(["stock_id", "signal_date"], keep="last"))
    # 展開到每個交易日：對每個 T 取「最新一筆 signal_date<=T」的 ret_pct，
    # 限最多 10 個交易日新鮮度（週頻正常 5 日就換新）
    piv = (agg.pivot(index="signal_date", columns="stock_id", values="ret_pct")
              .reindex(cal).ffill(limit=10))
    long = piv.stack().rename("fval").reset_index()
    long.columns = ["trade_date", "stock_id", "fval"]
    long = long[(long.trade_date >= WIN_START) & (long.trade_date <= WIN_END)]
    notes["F6"] = ("歷史覆蓋實際上=finmind 893 檔（tdcc 只有 2026-08-21 一週，"
                   "去重時該週優先 tdcc）；PIT=as_of 後第2交易日開盤可行動")
    return long


def margin_factor(c, cal: list[str], sbl_shares: pd.DataFrame) -> pd.DataFrame:
    """F7：Δ融資餘額/股本。股本=short_limit*4（TWT93U 融券限額推導；
    stock_daily_bars.shares_outstanding 在窗內僅 2026-08-18 起有值，不可用）。"""
    df = pd.read_sql_query(
        """SELECT stock_id, trade_date, margin_balance, source
             FROM stock_margin_daily WHERE trade_date BETWEEN ? AND ?""",
        c, params=(PRICE_LOAD_START, WIN_END))
    n0 = len(df)
    df = df[df.trade_date.isin(set(cal))]
    n1 = len(df)
    df["prio"] = np.where(df.source == "twse_mi_margn", 0, 1)
    df = (df.sort_values(["stock_id", "trade_date", "prio"])
            .drop_duplicates(["stock_id", "trade_date"], keep="first"))
    n2 = len(df)
    dedup_report["stock_margin_daily"] = {
        "rows_loaded": n0, "rows_on_calendar": n1,
        "rows_after_dedup(twse_mi_margn_first)": n2}

    piv = df.pivot(index="trade_date", columns="stock_id",
                   values="margin_balance").reindex(cal)
    dmargin = piv.diff()                     # 日曆對齊：缺日自動 NaN（防斷檔 diff）
    long = dmargin.stack().rename("dm").reset_index()
    long.columns = ["trade_date", "stock_id", "dm"]
    long = long.merge(sbl_shares, on=["stock_id", "trade_date"], how="inner")
    long["fval"] = long.dm * 1000.0 / long.shares       # 融資單位=張
    long = long.dropna(subset=["fval"])
    long = long[(long.trade_date >= WIN_START) & (long.trade_date <= WIN_END)]
    notes["F7"] = ("股本=short_limit*4（僅 TWSE 有借券表 → 上櫃自然剔除）；"
                   "margin_balance 單位張、×1000 對齊股。⚠️ 覆蓋斷層："
                   "twse_mi_margn 全市場僅 2026-06-01~2026-08-19（56日），"
                   "其餘日子只剩 finmind 回補子集（~120-290 檔，選樣偏誤），"
                   "整段 t 由子集主導——見 f7_coverage_split")
    return long[["stock_id", "trade_date", "fval"]]


def inst_factor(c, cal: list[str], volume_panel: pd.DataFrame) -> pd.DataFrame:
    """F8：三大法人合計買超股數 / 當日成交股數。"""
    df = pd.read_sql_query(
        """SELECT stock_id, trade_date, three_institution_net, source
             FROM stock_institutional_daily WHERE trade_date BETWEEN ? AND ?""",
        c, params=(WIN_START, WIN_END))
    n0 = len(df)
    df = df[df.trade_date.isin(set(cal))]
    n1 = len(df)
    prio = {"twse_t86": 0, "tpex_insti": 1, "finmind": 2}
    df["prio"] = df.source.map(prio).fillna(9)
    df = (df.sort_values(["stock_id", "trade_date", "prio"])
            .drop_duplicates(["stock_id", "trade_date"], keep="first"))
    n2 = len(df)
    dedup_report["stock_institutional_daily"] = {
        "rows_loaded": n0, "rows_on_calendar": n1,
        "rows_after_dedup(twse_t86_first)": n2}

    vol_long = volume_panel.stack().rename("vol").reset_index()
    vol_long.columns = ["trade_date", "stock_id", "vol"]
    m = df.merge(vol_long, on=["stock_id", "trade_date"], how="inner")
    m = m[m.vol > 0].copy()
    m["fval"] = m.three_institution_net / m.vol
    m = m.dropna(subset=["fval"])
    notes["F8"] = "dedup 優先序 twse_t86 > tpex_insti > finmind；分母=去重後成交股數"
    return m[["stock_id", "trade_date", "fval"]]


# ---------------------------------------------------------------- 檢定

def build_base_long(P) -> pd.DataFrame:
    def flat(name):
        s = P[name].stack().rename(name)
        return s
    base = pd.concat([flat("ret_exec"), flat("gap"), flat("vol60"),
                      flat("turnover"), flat("universe")], axis=1).reset_index()
    base.columns = ["trade_date", "stock_id", "ret_exec", "gap", "vol60",
                    "turnover", "universe"]
    base = base[(base.trade_date >= WIN_START) & (base.trade_date <= WIN_END)]
    base = base[base.universe.astype(bool)]
    return base.dropna(subset=["ret_exec", "gap", "vol60", "turnover"])


def _zs(x: pd.Series) -> pd.Series:
    sd = x.std()
    if not np.isfinite(sd) or sd == 0:
        return x * 0.0
    return ((x - x.mean()) / sd).clip(-5, 5)


def test_factor(fid: str, fdf: pd.DataFrame, base: pd.DataFrame) -> dict:
    d = base.merge(fdf, on=["trade_date", "stock_id"], how="inner")
    spreads, betas, ns = [], [], []
    days = []
    for dt, gduf in d.groupby("trade_date", sort=True):
        gdu = gduf.dropna(subset=["fval"])
        if len(gdu) < MIN_N_DAY or gdu.fval.nunique() < 5:
            continue
        r = gdu.fval.rank(method="first")
        try:
            q = pd.qcut(r, 5, labels=False)
        except ValueError:
            continue
        sp = gdu.ret_exec[q == 4].mean() - gdu.ret_exec[q == 0].mean()
        # FM：ret ~ 1 + f_rank + vol60 + gap + turnover
        xf = (gdu.fval.rank(pct=True) - 0.5).values
        X = np.column_stack([
            np.ones(len(gdu)), xf,
            _zs(gdu.vol60).values, _zs(gdu.gap).values, _zs(gdu.turnover).values])
        y = gdu.ret_exec.values
        try:
            beta = np.linalg.lstsq(X, y, rcond=None)[0][1]
        except np.linalg.LinAlgError:
            continue
        spreads.append(sp)
        betas.append(beta)
        ns.append(len(gdu))
        days.append(dt)
    spreads, betas = np.array(spreads), np.array(betas)
    raw_mean, raw_t = nw_tstat(spreads)
    neu_mean, neu_t = nw_tstat(betas)
    return {
        "id": fid,
        "n_days": int(len(days)),
        "n_stocks_avg": float(np.mean(ns)) if ns else 0.0,
        "raw_spread_pct": float(raw_mean * 100) if np.isfinite(raw_mean) else None,
        "raw_t": float(raw_t) if np.isfinite(raw_t) else None,
        "neutral_slope_pct": float(neu_mean * 100) if np.isfinite(neu_mean) else None,
        "neutral_t": float(neu_t) if np.isfinite(neu_t) else None,
        "survives": bool(np.isfinite(neu_t) and abs(neu_t) >= 3.0),
        "date_range": [days[0], days[-1]] if days else None,
    }


def main() -> int:
    c = connect()
    cal_full = load_calendar(c, SBL_LOAD_START)   # 含 SBL 暖身段
    cal = [d for d in cal_full if d >= PRICE_LOAD_START]
    print(f"官方日曆 {len(cal)} 日：{cal[0]} ~ {cal[-1]}（2026-07-10 已排除；"
          f"SBL 暖身段另有 {len(cal_full) - len(cal)} 日）")
    P = load_price_panels(c, cal)
    base = build_base_long(P)
    print(f"基底長表（宇宙∩報酬∩控制變數齊備）：{len(base):,} stock-day")

    # 股本輔助表（F7 用）
    sbl_sh = pd.read_sql_query(
        """SELECT stock_id, trade_date, short_limit*4 AS shares
             FROM stock_short_interest_daily
            WHERE trade_date BETWEEN ? AND ? AND short_limit > 0""",
        connect(), params=(WIN_START, WIN_END))
    sbl_sh = sbl_sh[sbl_sh.trade_date.isin(set(cal))].drop_duplicates(
        ["stock_id", "trade_date"])

    factors: dict[str, pd.DataFrame] = {}
    factors.update(sbl_factors(connect(), cal_full))
    factors["F5"] = branch_factor(cal)
    factors["F6"] = retail_factor(connect(), cal)
    factors["F7"] = margin_factor(connect(), cal, sbl_sh)
    factors["F8"] = inst_factor(connect(), cal, P["volume"])

    results = []
    for fid in ("F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8"):
        r = test_factor(fid, factors[fid], base)
        r["note"] = notes.get(fid, notes.get("F1-F4", "") if fid in
                              ("F1", "F2", "F3") else "")
        results.append(r)
        print(f"{fid}: n_days={r['n_days']} n_avg={r['n_stocks_avg']:.0f} "
              f"raw={r['raw_spread_pct'] if r['raw_spread_pct'] is None else round(r['raw_spread_pct'],4)}%/日 "
              f"raw_t={r['raw_t'] and round(r['raw_t'],2)} "
              f"neutral_t={r['neutral_t'] and round(r['neutral_t'],2)} "
              f"survives={r['survives']}")

    # F7 覆蓋斷層敏感度：twse_mi_margn 全市場段僅 2026-06-01~2026-08-19（56日），
    # 之前是 finmind 回補子集（~120-290 檔，選樣偏誤）→ 分段各測一次
    f7_split = []
    for lab, lo, hi in (("finmind_subset_era", WIN_START, "2026-05-31"),
                        ("twse_fullmkt_era", "2026-06-01", "2026-08-19")):
        sub = factors["F7"][(factors["F7"].trade_date >= lo)
                            & (factors["F7"].trade_date <= hi)]
        rr = test_factor(f"F7[{lab}]", sub, base)
        f7_split.append(rr)
        print(f"  {rr['id']}: n_days={rr['n_days']} n_avg={rr['n_stocks_avg']:.0f} "
              f"neutral_t={rr['neutral_t'] and round(rr['neutral_t'], 2)}")

    out = {
        "impl": "B（盲實作；獨立自建，僅分點聚合用 chip-street-canon top15_daily.pkl 快取）",
        "spec": "config/research.yaml chip-orthogonal-rebuild（2026-08-27 預註記）",
        "window": [WIN_START, WIN_END],
        "f5_end": F5_END,
        "calendar_days": len(cal),
        "universe": "close>=10 且 20日均量>300k 股（vol20 min_periods=15）",
        "return": "open(T+1)→close(T+1)；pivot 官方日曆、缺日自動 NaN（shift 驗證結構化）",
        "neutralization": ("FM 逐日 ret ~ 1 + rank(f)-0.5 + z(vol60) + z(gap_T+1) + "
                           "z(turnover=vol/20日均量)；t=NW(lag5)"),
        "dedup_report": dedup_report,
        "notes": notes,
        "factors": results,
        "f7_coverage_split": f7_split,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "solo_check_B.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n→ {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
