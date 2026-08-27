#!/usr/bin/env python3
"""chip-orthogonal-rebuild · 步驟 1：共用面板（純 ETL，不做檢定）。

預註記 SSOT：config/research.yaml topic "chip-orthogonal-rebuild"。
產出 reports/research/chip-orthogonal-rebuild/panel.pkl，每列 (stock_id, trade_date)：

  F1 z1  Δ借券賣出餘額(自身60日z)          — stock_chip_snapshot.market_scores 同款
  F2 zp  借券佔股本243日分位×4−2           — 同上（股本 = short_limit×4）
  F3 zu  Δ券源使用率(自身60日z)            — 同上
  F4 zf  借券費率自身60筆分位×4−2；無成交→0 — 同上
  F5 z6  分點(買超家數−賣超家數)/家數 當日橫斷面分位×4−2
         — 用 chip-street-canon 聚合快取 top15_daily.pkl（net<>0 家數，同 snapshot SQL）
         — tape 深度 2026-07-17 崩壞 → 截斷至 2026-07-16
  F6 retail 集保<50張(級距1–8)持股比水位（週頻）
         — run_chip_daily_brief.holding_structure 同款（tdcc 優先單一 source）
         — PIT：as_of 後第 2 個交易日才可行動；本面板執行日=T+1 →
           取「第 1 個交易日(as_of 之後) <= T」的最新一週，並限 14 天新鮮度
  F7 margin Δ融資餘額(張→股)/股本（stock_margin_daily 去重 twse_mi_margn 優先）
  F8 inst  三大法人合計買超股數/當日成交股數
         （stock_institutional_daily 去重 twse_t86 > tpex_insti > finmind）

  控制變數：vol60（60日收收報酬std）、gap（報酬日 T+1 的 open/prev_close−1）、
            turnover（T 日 volume/股本；缺股本 → volume/20日均量）
  報酬：r_oc = open(T+1)→close(T+1)、r_cc = close(T)→close(T+1)
  宇宙旗標 in_universe：close(T)>=10 且 20日均量(T)>300,000 股

資料坑防護：
  (1) 交易日曆＝stock_daily_bars 當日官方來源(twse_mi_index/tpex_daily/finmind)
      列數>500 的日子；2026-07-10 幽靈日自動排除。
  (2) 多來源同日多列顯式去重，去重前後列數寫進 panel_summary.json。
  (5) shift 類前瞻（r_oc/r_cc/gap）一律驗「下一列＝次一交易日」，不合設 NaN。

用法：PYTHONPATH=src .venv/bin/python scripts/research/chip_rebuild_panel.py
DB 唯讀；只寫 reports/research/chip-orthogonal-rebuild/。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports/research/chip-orthogonal-rebuild"
BRANCH_CACHE = ROOT / "reports/research/chip-street-canon/cache/top15_daily.pkl"

WIN_START = "2024-07-01"          # 面板訊號窗
WIN_END = "2026-08-26"
HIST_START = "2023-06-01"         # 滾動窗暖機（zp 需 243 筆、min 60）
F5_CUTOFF = "2026-07-16"          # 分點 tape 深度 2026-07-17 崩壞
CAL_MIN_ROWS = 500                # 官方來源列數 > 500 才算交易日
OFFICIAL_SOURCES = ("twse_mi_index", "tpex_daily", "finmind")
RETAIL_MAX_STALE_DAYS = 14        # 週頻資料新鮮度上限（calendar days）

FACTORS = ["z1", "zp", "zu", "zf", "z6", "retail", "margin", "inst"]


def connect_ro() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)


# ---------------------------------------------------------------- 交易日曆


def trading_calendar(c: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """回傳 (交易日 list, 被剔除的幽靈日 list)。"""
    df = pd.read_sql_query(
        f"""SELECT trade_date, COUNT(*) n
              FROM stock_daily_bars
             WHERE trade_date BETWEEN ? AND ?
               AND source IN ({','.join('?' * len(OFFICIAL_SOURCES))})
             GROUP BY trade_date""",
        c, params=(HIST_START, WIN_END, *OFFICIAL_SOURCES))
    good = sorted(df.loc[df.n > CAL_MIN_ROWS, "trade_date"])
    # 幽靈日 = bars 有任何列（含 yfinance）但官方列數不足的日子
    allday = pd.read_sql_query(
        "SELECT DISTINCT trade_date FROM stock_daily_bars WHERE trade_date BETWEEN ? AND ?",
        c, params=(HIST_START, WIN_END))
    ghost = sorted(set(allday.trade_date) - set(good))
    return good, ghost


# ---------------------------------------------------------------- 去重工具


def dedupe(df: pd.DataFrame, priority: list[str], keys=("stock_id", "trade_date"),
           stats: dict | None = None, name: str = "") -> pd.DataFrame:
    """多來源同 (stock_id, trade_date) 多列 → 依 source priority 取一列。"""
    before = len(df)
    rank = {s: i for i, s in enumerate(priority)}
    df = df.assign(_rk=df.source.map(rank).fillna(len(priority)))
    df = (df.sort_values([*keys, "_rk"])
            .drop_duplicates(list(keys), keep="first")
            .drop(columns="_rk"))
    if stats is not None:
        stats[name] = {"rows_before": int(before), "rows_after": int(len(df))}
    return df


# ---------------------------------------------------------------- 價量骨架


def load_bars(c, cal: list[str], stats: dict) -> pd.DataFrame:
    bars = pd.read_sql_query(
        """SELECT stock_id, trade_date, open, close, volume, source, shares_outstanding
             FROM stock_daily_bars
            WHERE trade_date BETWEEN ? AND ?
              AND source IN ('twse_mi_index','tpex_daily','finmind','yfinance')""",
        c, params=(HIST_START, WIN_END))
    bars = bars[bars.trade_date.isin(cal)]
    bars = dedupe(bars, ["twse_mi_index", "tpex_daily", "finmind", "yfinance"],
                  stats=stats, name="stock_daily_bars")
    return bars.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)


def build_skeleton(bars: pd.DataFrame, cal: list[str], stats: dict) -> pd.DataFrame:
    """報酬 / 控制變數 / 宇宙旗標。shift 前瞻一律驗「下一列＝次一交易日」。"""
    nxt = {d: cal[i + 1] for i, d in enumerate(cal[:-1])}   # 次一交易日
    b = bars.copy()
    g = b.groupby("stock_id", group_keys=False)
    b["next_date"] = g.trade_date.shift(-1)
    b["next_open"] = g.open.shift(-1)
    b["next_close"] = g.close.shift(-1)
    ok = b.next_date == b.trade_date.map(nxt)               # 坑 (5)
    stats["shift_validation"] = {
        "rows_with_next_row": int(b.next_date.notna().sum()),
        "rows_next_row_not_next_trading_day": int((b.next_date.notna() & ~ok).sum()),
    }
    for col in ("next_open", "next_close"):
        b.loc[~ok, col] = np.nan
    b.loc[b.next_open <= 0, "next_open"] = np.nan

    b["r_cc"] = b.next_close / b.close - 1.0
    b["r_oc"] = b.next_close / b.next_open - 1.0
    b["gap"] = b.next_open / b.close - 1.0                  # 報酬日(T+1)的跳空

    b["ret1"] = g.close.pct_change()
    b["vol60"] = g.ret1.transform(lambda x: x.rolling(60, min_periods=40).std())
    b["vol20"] = g.volume.transform(lambda x: x.rolling(20, min_periods=20).mean())
    return b


# ---------------------------------------------------------------- F1–F4 借券系


def load_sbl_factors(c, cal: list[str], stats: dict) -> pd.DataFrame:
    """z1/zp/zu/zf —— 逐行沿用 stock_chip_snapshot.market_scores 的定義。"""
    hist = pd.read_sql_query(
        """SELECT s.stock_id, s.trade_date, s.sbl_balance, s.sbl_next_limit,
                  s.short_limit, f.fee_rate_vw
             FROM stock_short_interest_daily s
             LEFT JOIN (SELECT stock_id, trade_date, fee_rate_vw FROM stock_sbl_fee_daily
                         WHERE deal_type='ALL') f
               ON f.stock_id=s.stock_id AND f.trade_date=s.trade_date
            WHERE s.trade_date BETWEEN ? AND ?""",
        c, params=(HIST_START, WIN_END))
    before = len(hist)
    hist = hist[hist.trade_date.isin(cal)]
    hist = hist.drop_duplicates(["stock_id", "trade_date"], keep="first")
    stats["stock_short_interest_daily"] = {
        "rows_before": int(before), "rows_after": int(len(hist)),
        "note": "單一 source(twse)，去重僅防禦性；另過濾非交易日曆列",
    }
    h = hist.sort_values(["stock_id", "trade_date"]).copy()
    h["shares"] = (h.short_limit * 4).replace(0, np.nan)    # 上市股本代理（TWT93U 限額×4）
    h["sbl_pct"] = h.sbl_balance / h.shares
    h["util"] = h.sbl_balance / (h.sbl_balance + h.sbl_next_limit)
    g = h.groupby("stock_id", group_keys=False)
    h["d_sbl"] = g.sbl_balance.diff()
    h["d_util"] = g.util.diff()

    def zself(col, win=60):
        mu = g[col].transform(lambda x: x.rolling(win, min_periods=30).mean())
        sd = g[col].transform(lambda x: x.rolling(win, min_periods=30).std())
        return (h[col] - mu) / sd.replace(0, np.nan)

    h["z1"] = zself("d_sbl")
    h["zu"] = zself("d_util")
    h["zp"] = ((g.sbl_pct.transform(lambda x: x.rolling(243, min_periods=60).rank(pct=True))
                - 0.5) * 4)
    h["zf"] = ((g.fee_rate_vw.transform(lambda x: x.rolling(60, min_periods=10).rank(pct=True))
                - 0.5) * 4)
    h["zf"] = h.zf.fillna(0.0)          # 當日無成交 → 0（pipeline 文件明訂）
    return h[["stock_id", "trade_date", "z1", "zp", "zu", "zf", "shares"]]


# ---------------------------------------------------------------- F5 分點


MIN_BRANCH_DEPTH_MEDIAN = 40   # 當日全市場每股分點數中位數 < 40 → 整日停用（同 snapshot）
MIN_BRANCH_DEPTH_STOCK = 20    # 個股分點數 < 20 → 不進 rank（同 snapshot）


def load_branch_factor(stats: dict) -> pd.DataFrame:
    br = pd.read_pickle(BRANCH_CACHE)
    before = len(br)
    br = br[(br.trade_date >= WIN_START) & (br.trade_date <= F5_CUTOFF)].copy()
    # 深度規則沿用 stock_chip_snapshot（2026-08-27 硬化版）：
    #   日層級 median 深度 < 40 → 整日 NaN；個股 < 20 分點 → 不進 rank
    day_med = br.groupby("trade_date").n_branches.median()
    bad_days = sorted(day_med[day_med < MIN_BRANCH_DEPTH_MEDIAN].index)
    br = br[~br.trade_date.isin(bad_days) & (br.n_branches >= MIN_BRANCH_DEPTH_STOCK)].copy()
    stats["branch_cache_top15_daily"] = {
        "rows_before": int(before), "rows_after": int(len(br)),
        "note": (f"聚合快取（net<>0 家數，同 snapshot SQL）；F5 截斷至 {F5_CUTOFF}；"
                 f"深度規則 day_median>={MIN_BRANCH_DEPTH_MEDIAN}·stock>="
                 f"{MIN_BRANCH_DEPTH_STOCK}；深度不足整日停用 {len(bad_days)} 日"),
        "depth_disabled_days": bad_days,
    }
    br["brdiff"] = (br.n_buy_houses - br.n_sell_houses) / br.n_branches
    br["z6"] = (br.groupby("trade_date").brdiff.rank(pct=True) - 0.5) * 4
    return br[["stock_id", "trade_date", "z6"]]


# ---------------------------------------------------------------- F6 集保散戶


def load_retail_weekly(c, stats: dict) -> pd.DataFrame:
    """每 (stock_id, as_of_date) 一列 ret_pct —— run_chip_daily_brief 同款單一 source。"""
    df = pd.read_sql_query(
        """WITH pick AS (
              SELECT stock_id, as_of_date, source,
                     ROW_NUMBER() OVER (
                       PARTITION BY stock_id, as_of_date
                       ORDER BY CASE source WHEN 'tdcc' THEN 0 ELSE 1 END) AS rn
                FROM (SELECT DISTINCT stock_id, as_of_date, source
                        FROM stock_holding_dispersion_weekly
                       WHERE as_of_date >= ?))
           SELECT p.stock_id, p.as_of_date,
                  SUM(CASE WHEN w.level IN ('1','2','3','4','5','6','7','8')
                           THEN w.percent ELSE 0 END) AS ret_pct,
                  SUM(CASE WHEN w.level IN ('12','13','14','15')
                           THEN w.percent ELSE 0 END) AS big_pct
             FROM pick p
             JOIN stock_holding_dispersion_weekly w
               ON w.stock_id=p.stock_id AND w.as_of_date=p.as_of_date
              AND w.source=p.source
            WHERE p.rn = 1
            GROUP BY p.stock_id, p.as_of_date""",
        c, params=(HIST_START,))
    before = len(df)
    bad = df[(df.ret_pct + df.big_pct) > 101]
    if len(bad):
        raise RuntimeError(f"集保持股百分比異常（{len(bad)} 檔週 >101%），疑似雙計")
    df = df[df.ret_pct > 0]
    stats["stock_holding_dispersion_weekly"] = {
        "rows_before": int(before), "rows_after": int(len(df)),
        "note": "rows=stock-week 聚合後；單一 source(tdcc 優先)於 SQL 內先選定",
    }
    return df[["stock_id", "as_of_date", "ret_pct"]]


def attach_retail(panel: pd.DataFrame, weekly: pd.DataFrame,
                  cal: list[str]) -> pd.DataFrame:
    """PIT：as_of 後第 2 個交易日才可行動；本面板行動日＝open(T+1) →
    可用條件為「as_of 之後的第 1 個交易日 <= T」（則第 2 個交易日 <= T+1）。"""
    cal_dt = pd.to_datetime(cal)
    asof = pd.to_datetime(weekly.as_of_date)
    idx = cal_dt.searchsorted(asof, side="right")            # 第 1 個 > as_of 的交易日
    ok = idx < len(cal_dt)
    w = weekly.loc[ok.tolist()].copy()
    w["usable_from"] = cal_dt[idx[ok]]
    w = (w.sort_values(["usable_from", "as_of_date"])
          .drop_duplicates(["stock_id", "usable_from"], keep="last"))

    p = panel.copy()
    p["_dt"] = pd.to_datetime(p.trade_date)
    p = p.sort_values("_dt", kind="mergesort")
    w = w.sort_values("usable_from", kind="mergesort")
    merged = pd.merge_asof(
        p, w[["stock_id", "usable_from", "as_of_date", "ret_pct"]],
        left_on="_dt", right_on="usable_from", by="stock_id",
        tolerance=pd.Timedelta(days=RETAIL_MAX_STALE_DAYS))
    merged = merged.rename(columns={"ret_pct": "retail",
                                    "as_of_date": "retail_as_of"})
    return merged.drop(columns=["_dt", "usable_from"])


# ---------------------------------------------------------------- F7 融資


def load_margin(c, cal: list[str], stats: dict) -> pd.DataFrame:
    m = pd.read_sql_query(
        """SELECT stock_id, trade_date, margin_balance, source
             FROM stock_margin_daily WHERE trade_date BETWEEN ? AND ?""",
        c, params=(HIST_START, WIN_END))
    m = m[m.trade_date.isin(cal)]
    m = dedupe(m, ["twse_mi_margn", "finmind"], stats=stats, name="stock_margin_daily")
    m = m.sort_values(["stock_id", "trade_date"])
    m["d_margin_lots"] = m.groupby("stock_id", group_keys=False).margin_balance.diff()
    return m[["stock_id", "trade_date", "d_margin_lots"]]


# ---------------------------------------------------------------- F8 法人


def load_inst(c, cal: list[str], stats: dict) -> pd.DataFrame:
    t = pd.read_sql_query(
        """SELECT stock_id, trade_date, three_institution_net, source
             FROM stock_institutional_daily WHERE trade_date BETWEEN ? AND ?""",
        c, params=(WIN_START, WIN_END))
    t = t[t.trade_date.isin(cal)]
    t = dedupe(t, ["twse_t86", "tpex_insti", "finmind"],
               stats=stats, name="stock_institutional_daily")
    return t[["stock_id", "trade_date", "three_institution_net"]]


# ---------------------------------------------------------------- main


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dedupe_stats: dict = {}
    c = connect_ro()

    cal_all, ghost = trading_calendar(c)
    print(f"交易日曆：{len(cal_all)} 日（{cal_all[0]}~{cal_all[-1]}），"
          f"剔除幽靈日 {len(ghost)} 個：{ghost}")

    bars = load_bars(c, cal_all, dedupe_stats)
    skel = build_skeleton(bars, cal_all, dedupe_stats)

    sbl = load_sbl_factors(c, cal_all, dedupe_stats)
    br = load_branch_factor(dedupe_stats)
    weekly = load_retail_weekly(c, dedupe_stats)
    margin = load_margin(c, cal_all, dedupe_stats)
    inst = load_inst(c, cal_all, dedupe_stats)
    c.close()

    # ---- 訊號窗面板 ----
    p = skel[(skel.trade_date >= WIN_START) & (skel.trade_date <= WIN_END)].copy()
    p = p.merge(sbl, on=["stock_id", "trade_date"], how="left")
    p = p.merge(br, on=["stock_id", "trade_date"], how="left")
    p = p.merge(margin, on=["stock_id", "trade_date"], how="left")
    p = p.merge(inst, on=["stock_id", "trade_date"], how="left")
    p = attach_retail(p, weekly, cal_all)

    # F7：Δ融資(張→股)/股本；股本 = short_limit×4，缺則用 tpex 日行情發行股數
    shares = p.shares.where(p.shares > 0)
    shares = shares.fillna(p.shares_outstanding.where(p.shares_outstanding > 0))
    p["margin"] = p.d_margin_lots * 1000.0 / shares
    # F8：三大法人合計買超股數 / 當日成交股數
    vol = p.volume.where(p.volume > 0)
    p["inst"] = p.three_institution_net / vol
    # F5 有效段之外強制 NaN（快取已截斷，這裡防禦性再設一次）
    p.loc[p.trade_date > F5_CUTOFF, "z6"] = np.nan
    # 控制變數 turnover：T 日 volume/股本，缺股本 → volume/20日均量
    p["turnover"] = np.where(shares.notna(), p.volume / shares,
                             p.volume / p.vol20.where(p.vol20 > 0))
    # 宇宙旗標
    p["in_universe"] = (p.close >= 10) & (p.vol20 > 300_000)

    cols = ["stock_id", "trade_date", "z1", "zp", "zu", "zf", "z6",
            "retail", "retail_as_of", "margin", "inst",
            "vol60", "gap", "turnover", "close", "volume",
            "r_oc", "r_cc", "in_universe"]
    panel = p[cols].sort_values(["trade_date", "stock_id"]).reset_index(drop=True)
    panel.to_pickle(OUT_DIR / "panel.pkl")

    # ---- summary ----
    cal_win = [d for d in cal_all if WIN_START <= d <= WIN_END]
    uni = panel[panel.in_universe]
    f5_days = [d for d in cal_win if d <= F5_CUTOFF]

    def cov(col: str, valid_days: list[str] | None = None) -> dict:
        base = uni if valid_days is None else uni[uni.trade_date.isin(valid_days)]
        s = base[col]
        daily = base.assign(_ok=s.notna()).groupby("trade_date")._ok.sum()
        return {
            "nonnan_share_universe": round(float(s.notna().mean()), 4),
            "mean_daily_count_universe": round(float(daily.mean()), 1),
            "nonnan_share_all_rows": round(float(panel[col].notna().mean()), 4),
        }

    summary = {
        "spec": {
            "window": [WIN_START, WIN_END], "hist_start": HIST_START,
            "f5_cutoff": F5_CUTOFF,
            "universe": "close>=10 AND vol20>300000 shares",
            "returns": "r_oc=open(T+1)->close(T+1); r_cc=close(T)->close(T+1)",
            "retail_pit": ("as_of 後第2交易日可行動；面板取"
                           "「as_of 後第1交易日<=T」最新一週，新鮮度<=14天"),
        },
        "calendar": {
            "n_trading_days_window": len(cal_win),
            "first": cal_win[0], "last": cal_win[-1],
            "n_trading_days_f5_segment": len(f5_days),
            "ghost_days_excluded": ghost,
        },
        "dedupe": dedupe_stats,
        "panel": {
            "rows": int(len(panel)),
            "stocks": int(panel.stock_id.nunique()),
            "universe_rows": int(len(uni)),
            "mean_daily_universe_count":
                round(float(uni.groupby("trade_date").size().mean()), 1),
        },
        "factor_coverage": {
            "F1_z1": cov("z1"), "F2_zp": cov("zp"), "F3_zu": cov("zu"),
            "F4_zf": cov("zf"), "F5_z6": cov("z6", f5_days),
            "F6_retail": cov("retail"), "F7_margin": cov("margin"),
            "F8_inst": cov("inst"),
        },
        "controls_returns_coverage": {
            k: cov(k) for k in ("vol60", "gap", "turnover", "r_oc", "r_cc")
        },
        "shift_validation": dedupe_stats.pop("shift_validation", None),
        "data_reality_notes": {
            "F1_F4": "stock_short_interest_daily 只覆蓋可借券股（上市 twse 來源），"
                     "宇宙內 OTC／不可券股為 NaN——非 ETL 缺陷",
            "F6_retail": "集保週頻歷史只有 finmind 回補的 893 檔；tdcc 全市場"
                         "（4034 檔）僅最新一週（2026-08-21）——歷史覆蓋率受限於此",
            "F7_margin": "全市場融資餘額（twse_mi_margn ~1290 檔/日）僅 2026-06-01 起；"
                         "之前只有 finmind 子集 ~150 檔/日——歷史覆蓋率受限於此",
            "F5_z6": "僅 2024-07-01~2026-07-16 有效段；深度規則同 snapshot 硬化版",
        },
    }
    # shift_validation 是 build_skeleton 塞進 dedupe_stats 的，搬到頂層
    with open(OUT_DIR / "panel_summary.json", "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"panel.pkl：{len(panel):,} 列 × {len(cols)} 欄 · "
          f"宇宙列 {len(uni):,}（日均 {summary['panel']['mean_daily_universe_count']} 檔）")
    for k, v in summary["factor_coverage"].items():
        print(f"  {k}: 宇宙內非NaN {v['nonnan_share_universe']:.1%} · "
              f"日均 {v['mean_daily_count_universe']} 檔")
    print(f"summary → {OUT_DIR / 'panel_summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
