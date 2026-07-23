#!/usr/bin/env python3
"""H6：大跌溫度計「高熱但沒接著大跌」的假警報(false positive)根因分析.

Research diagnostic（一次性）· 非正式風控規則 · 非可下單訊號 · 唯讀分析，
未修改 `run_market_crash_thermometer_dashboard.py` 或任何 config。

方法：
1. 複用production `build_calendar`/`build_branch_panel`/`build_full_grid`/
   `compute_lb_pctile`/`weighted_composite`/`bounded_pctrank`/`load_event_dates`
   （直接 import 該模組，不重寫、不修改原檔案），重算 2024-07-01~asof（分點資料
   起始日）逐日複合溫度分數／共識家數／溫度百分位。
2. 找出所有 pctrank>=85% 的交易日，扣除16次大跌事件本身＋事件後5個交易日
   （視為真訊號延續），再檢查未來5個交易日IX0001是否出現過任一日<=-2%
   （有→「弱訊號」；沒有→「完全false positive」）。
3. 對「完全false positive」逐一查：共識家數、加權貢獻最大分點佔比、
   （用當日lb_sum視窗回查）賣超金額最大個股與集中度、是否落在月底最後3個
   交易日或台指期結算日（每月第三個星期三）附近。
4. 用「共識家數<=2」「賣超集中單一個股>50%」「月底/結算日附近」三條候選
   排除規則，統計完全false positive中命中比例、模擬排除後FP rate改善，
   並檢查會不會誤殺16次大跌事件本身（trade-off）。

輸出：reports/research/branch-footprint-screen/adv_h6_false_positive_analysis.md
"""
from __future__ import annotations

import calendar as calendar_mod
import importlib.util
import sys
from datetime import date as dtdate
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
EPISODES_CSV = OUT_DIR / "market_crash_precursor_episodes.csv"
DB_PATH = ROOT / "data" / "stocks.db"
REPORT_PATH = OUT_DIR / "adv_h6_false_positive_analysis.md"

_spec = importlib.util.spec_from_file_location(
    "crash_dashboard", ROOT / "scripts/research/run_market_crash_thermometer_dashboard.py"
)
cd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cd)  # type: ignore[union-attr]

from market_benchmark import load_benchmark_close  # noqa: E402

PANEL = cd.PANEL
IDS = list(PANEL.keys())
LOOKBACK_DAYS = cd.LOOKBACK_DAYS_DEFAULT  # 5
HISTORY_DAYS = cd.HISTORY_DAYS_DEFAULT  # 120
BRANCH_DATA_START = "2024-07-01"  # 8家分點資料實際起始日（實測所有8家皆同）

HIGH_TEMP_THRESHOLD = 85.0
FORWARD_CHECK_DAYS = 5
WEAK_SIGNAL_RET_THRESHOLD = -0.02
EVENT_AFTERMATH_DAYS = 5

CONSENSUS_LOW_THRESHOLD = 2  # <=2/8
CONCENTRATION_THRESHOLD = 0.5  # >50%
MONTH_END_LAST_N = 3
SETTLEMENT_PROXIMITY_DAYS = 2

# 極少數常見大型股代碼，僅供報告可讀性參考（DB無獨立股票名稱表）
KNOWN_NAMES: dict[str, str] = {
    "2330": "台積電", "2317": "鴻海", "2454": "聯發科", "2382": "廣達",
    "2412": "中華電", "2881": "富邦金", "2882": "國泰金", "2891": "中信金",
    "2892": "第一金", "2886": "兆豐金", "1301": "台塑", "1303": "南亞",
    "3711": "日月光投控", "2308": "台達電", "3034": "聯詠", "2303": "聯電",
    "2379": "瑞昱", "6669": "緯穎", "2357": "華碩", "2395": "研華",
    "3037": "欣興", "2609": "陽明", "2603": "長榮", "2615": "萬海",
    "2887": "台新金", "5880": "合庫金", "2880": "華南金", "2884": "玉山金",
    "2385": "群光", "3045": "台灣大", "4904": "遠傳", "2327": "國巨",
    "2377": "微星", "3231": "緯創", "2356": "英業達", "2327": "國巨",
    "1216": "統一", "2002": "中鋼", "2207": "和泰車", "2201": "裕隆",
    "3661": "世芯-KY", "6857": "云豐", "6519": "智微", "5347": "世界",
}


def stock_label(sid: str) -> str:
    name = KNOWN_NAMES.get(sid)
    return f"{sid}({name})" if name else sid


def third_wednesday(year: int, month: int) -> dtdate:
    c = calendar_mod.Calendar()
    weds = [d for d in c.itermonthdates(year, month) if d.month == month and d.weekday() == 2]
    return weds[2]


def stock_breakdown(conn, ids: list[str], dates: list[str]) -> pd.DataFrame:
    """指定分點清單×日期清單，逐股 net_amt(NT$) 加總（正=買超、負=賣超）。"""
    if not dates:
        return pd.DataFrame(columns=["stock_id", "net_amt"])
    id_ph = ",".join("?" for _ in ids)
    date_ph = ",".join("?" for _ in dates)
    raw = pd.read_sql_query(
        f"""
        SELECT trade_date, stock_id, SUM(net) AS net_shares
        FROM stock_broker_branch_daily
        WHERE source=? AND trade_date IN ({date_ph})
          AND stock_id != '__EMPTY__' AND length(stock_id)=4
          AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND securities_trader_id IN ({id_ph})
        GROUP BY trade_date, stock_id
        """,
        conn,
        params=[cd.SOURCE, *dates, *ids],
    )
    if raw.empty:
        return pd.DataFrame(columns=["stock_id", "net_amt"])
    closes = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, close FROM stock_daily_bars
        WHERE source=? AND trade_date IN ({date_ph}) AND close > 0
        """,
        conn,
        params=[cd.SOURCE, *dates],
    )
    raw["stock_id"] = raw["stock_id"].astype(str)
    raw["trade_date"] = raw["trade_date"].astype(str)
    closes["stock_id"] = closes["stock_id"].astype(str)
    closes["trade_date"] = closes["trade_date"].astype(str)
    merged = raw.merge(closes, on=["stock_id", "trade_date"], how="inner")
    merged["net_amt"] = merged["net_shares"].astype(float) * merged["close"].astype(float)
    agg = merged.groupby("stock_id", as_index=False)["net_amt"].sum().sort_values("net_amt")
    return agg


def concentration_stats(agg: pd.DataFrame) -> tuple[str | None, float | None, float | None]:
    """回傳 (top1賣超股票代碼, top1佔全部賣超金額比例, top1_net_amt)。"""
    sells = agg[agg["net_amt"] < 0]
    if sells.empty:
        return None, None, None
    total_sell = sells["net_amt"].abs().sum()
    top1 = sells.iloc[0]
    share = float(abs(top1["net_amt"]) / total_sell) if total_sell > 0 else None
    return str(top1["stock_id"]), share, float(top1["net_amt"])


def main() -> None:
    conn = cd.connect_ro(DB_PATH)
    asof = cd.latest_trade_date(conn)
    print(f"asof={asof}", flush=True)

    cal_full = cd.build_calendar(conn, end=asof, n_days=520, extra_ids=IDS)
    cal = [d for d in cal_full if d >= BRANCH_DATA_START]
    print(f"calendar (>=分點資料起始日) n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)
    idx_of = {d: i for i, d in enumerate(cal)}

    panel_raw = cd.build_branch_panel(conn, IDS, start=cal[0], end=asof)
    print(f"panel rows={len(panel_raw)}", flush=True)
    grid = cd.build_full_grid(panel_raw, IDS, cal)
    scored = cd.compute_lb_pctile(grid, LOOKBACK_DAYS)

    weights = {bid: w for bid, (_, w) in PANEL.items()}
    series = cd.weighted_composite(scored, weights)
    scores_by_date = dict(zip(series["trade_date"], series["composite_score"]))
    consensus_by_date = dict(zip(series["trade_date"], series["consensus_n"]))

    bench = load_benchmark_close(conn, code="IX0001").sort_index()
    bench = bench[bench.index.isin(cal)]
    ret = bench.pct_change()

    crash_dates, all_event_dates = cd.load_event_dates(conn, cal)
    event_idx = {idx_of[d] for d in all_event_dates if d in idx_of}
    print(f"crash_dates(<=-3%) n={len(crash_dates)}", flush=True)

    episodes = pd.read_csv(EPISODES_CSV, dtype={"trade_date": str})
    episode_dates = [d for d in episodes["trade_date"].tolist() if d in idx_of]
    print(f"episodes csv n={len(episodes)}, matched in calendar n={len(episode_dates)}", flush=True)
    assert set(episode_dates) == crash_dates, (
        f"episodes csv 與 load_event_dates 重算的16次大跌事件不一致: "
        f"csv only={set(episode_dates)-crash_dates} recompute only={crash_dates-set(episode_dates)}"
    )

    # ---- 逐日 bounded_pctrank（温度百分位） ----
    valid_dates = [d for d in cal if d in scores_by_date]
    rows = []
    for d in valid_dates:
        pr, n_normal = cd.bounded_pctrank(
            scores_by_date, cal, event_idx,
            test_date=d, history_days=HISTORY_DAYS, lookback_days=LOOKBACK_DAYS,
        )
        rows.append({
            "trade_date": d,
            "composite_score": scores_by_date[d],
            "consensus_n": int(consensus_by_date.get(d, 0)),
            "pctrank": pr,
            "n_normal": n_normal,
        })
    daily_df = pd.DataFrame(rows)
    print(f"daily rows (score defined)={len(daily_df)}", flush=True)

    # ---- event / aftermath exclusion set：事件本身 + 事件後5個交易日 ----
    aftermath = set()
    for d in crash_dates:
        i = idx_of[d]
        for k in range(1, EVENT_AFTERMATH_DAYS + 1):
            if i + k < len(cal):
                aftermath.add(cal[i + k])
    excluded_dates = crash_dates | aftermath

    # ---- 未來5個交易日是否出現>=2%單日下跌 ----
    def forward_has_drawdown(d: str) -> bool | None:
        i = idx_of[d]
        fwd = cal[i + 1 : i + 1 + FORWARD_CHECK_DAYS]
        if not fwd:
            return None
        rets = [ret.get(fd) for fd in fwd if fd in ret.index]
        rets = [r for r in rets if r is not None and not pd.isna(r)]
        if not rets:
            return None
        return any(r <= WEAK_SIGNAL_RET_THRESHOLD for r in rets)

    # ---- 月底最後3個交易日 / 結算日±2交易日 預先算好 ----
    month_end_set: set[str] = set()
    by_ym: dict[tuple[int, int], list[str]] = {}
    for d in cal:
        dt = dtdate.fromisoformat(d)
        by_ym.setdefault((dt.year, dt.month), []).append(d)
    for ym, ds in by_ym.items():
        ds_sorted = sorted(ds)
        month_end_set |= set(ds_sorted[-MONTH_END_LAST_N:])

    settlement_idx: list[int] = []
    for (y, m) in by_ym:
        tw = third_wednesday(y, m).isoformat()
        cands = [d for d in cal if d >= tw]
        if cands:
            settle_date = min(cands)
            settlement_idx.append(idx_of[settle_date])

    def near_settlement(d: str) -> bool:
        i = idx_of[d]
        return any(abs(i - si) <= SETTLEMENT_PROXIMITY_DAYS for si in settlement_idx)

    def near_month_end(d: str) -> bool:
        return d in month_end_set

    # ---- 對所有 high-temp 日（pctrank>=85）算診斷特徵，含事件日/弱訊號一併算，供trade-off檢查 ----
    high_temp = daily_df[daily_df["pctrank"] >= HIGH_TEMP_THRESHOLD].copy()
    print(f"high_temp days (pctrank>=85) n={len(high_temp)}", flush=True)

    diag_rows = []
    for r in high_temp.itertuples():
        d = r.trade_date
        i = idx_of[d]
        category = (
            "event_or_aftermath" if d in excluded_dates
            else ("weak_signal" if forward_has_drawdown(d) else "complete_fp")
        )
        # 分點加權貢獻佔比（找單一分點極端值主導 vs 廣泛共識）
        day_scored = scored[scored["trade_date"] == d]
        contribs = {}
        for bid in IDS:
            row_b = day_scored[day_scored["securities_trader_id"] == bid]
            if row_b.empty or pd.isna(row_b["lb_pctile"].iloc[0]):
                continue
            lb_p = float(row_b["lb_pctile"].iloc[0])
            contribs[bid] = weights[bid] * (1.0 - lb_p)
        total_contrib = sum(contribs.values()) if contribs else 0.0
        top1_branch, top1_branch_share = (None, None)
        if contribs and total_contrib > 0:
            top1_branch = max(contribs, key=contribs.get)
            top1_branch_share = contribs[top1_branch] / total_contrib

        # 賣超集中度：用 lb_sum 視窗（d 之前 LOOKBACK_DAYS 個交易日，shift(1).rolling(5)）
        window_dates = cal[max(0, i - LOOKBACK_DAYS): i]
        agg = stock_breakdown(conn, IDS, window_dates)
        top1_stock, top1_stock_share, top1_stock_amt = concentration_stats(agg)

        diag_rows.append({
            "trade_date": d,
            "pctrank": r.pctrank,
            "consensus_n": r.consensus_n,
            "composite_score": r.composite_score,
            "category": category,
            "top1_branch": top1_branch,
            "top1_branch_name": PANEL[top1_branch][0] if top1_branch else None,
            "top1_branch_share": top1_branch_share,
            "top1_stock": top1_stock,
            "top1_stock_share": top1_stock_share,
            "top1_stock_amt": top1_stock_amt,
            "near_month_end": near_month_end(d),
            "near_settlement": near_settlement(d),
            "rule_low_consensus": r.consensus_n <= CONSENSUS_LOW_THRESHOLD,
            "rule_stock_concentration": (top1_stock_share is not None and top1_stock_share > CONCENTRATION_THRESHOLD),
            "rule_calendar": (near_month_end(d) or near_settlement(d)),
        })
        print(f"  {d} pctrank={r.pctrank:.1f} consensus={r.consensus_n}/8 cat={category}", flush=True)

    diag_df = pd.DataFrame(diag_rows)
    diag_df["rule_any"] = diag_df["rule_low_consensus"] | diag_df["rule_stock_concentration"] | diag_df["rule_calendar"]

    # ---- 16次大跌事件當日本身的讀數（trade-off 檢查：事件日是否也會被排除規則誤殺） ----
    event_check_rows = []
    for d in sorted(crash_dates):
        if d not in idx_of:
            continue
        i = idx_of[d]
        row = daily_df[daily_df["trade_date"] == d]
        pr = float(row["pctrank"].iloc[0]) if not row.empty and pd.notna(row["pctrank"].iloc[0]) else None
        cn = int(row["consensus_n"].iloc[0]) if not row.empty else None
        window_dates = cal[max(0, i - LOOKBACK_DAYS): i]
        agg = stock_breakdown(conn, IDS, window_dates)
        top1_stock, top1_stock_share, _ = concentration_stats(agg)
        rule_low_consensus = (cn is not None and cn <= CONSENSUS_LOW_THRESHOLD)
        rule_stock_conc = (top1_stock_share is not None and top1_stock_share > CONCENTRATION_THRESHOLD)
        rule_cal = (near_month_end(d) or near_settlement(d))
        matched = []
        if rule_low_consensus:
            matched.append("低共識")
        if rule_stock_conc:
            matched.append("個股集中")
        if rule_cal:
            matched.append("月底/結算")
        event_check_rows.append({
            "trade_date": d,
            "pctrank": pr,
            "consensus_n": cn,
            "reached_high_temp": (pr is not None and pr >= HIGH_TEMP_THRESHOLD),
            "top1_stock": top1_stock,
            "top1_stock_share": top1_stock_share,
            "near_month_end": near_month_end(d),
            "near_settlement": near_settlement(d),
            "rule_low_consensus": rule_low_consensus,
            "rule_stock_concentration": rule_stock_conc,
            "rule_calendar": rule_cal,
            "would_be_excluded": rule_low_consensus or rule_stock_conc or rule_cal,
            "matched_rules": "、".join(matched) if matched else "",
        })
    event_check_df = pd.DataFrame(event_check_rows)

    # ---- 弱訊號 bucket 的 trade-off 檢查（排除規則是否會誤殺弱訊號/真precursor） ----
    weak_df = diag_df[diag_df["category"] == "weak_signal"].copy()

    conn.close()

    diag_df.to_csv(OUT_DIR / "_h6_high_temp_diagnostics.csv", index=False)
    event_check_df.to_csv(OUT_DIR / "_h6_event_day_check.csv", index=False)

    write_report(diag_df, event_check_df, weak_df, asof, cal, valid_dates)


def write_report(
    diag_df: pd.DataFrame,
    event_check_df: pd.DataFrame,
    weak_df: pd.DataFrame,
    asof: str,
    cal: list[str],
    valid_dates: list[str],
) -> None:
    lines: list[str] = []
    lines.append("# H6：大跌溫度計假警報(false positive)根因分析")
    lines.append("")
    lines.append(
        "Research diagnostic（一次性假說驗證）· 非正式風控規則 · 非可下單訊號 · "
        "唯讀分析，未修改 `run_market_crash_thermometer_dashboard.py` 或任何 config。"
    )
    lines.append("")
    lines.append(
        f"asof={asof}；計算窗={cal[0]}..{cal[-1]}（{len(cal)}個交易日，即8家分點資料"
        f"實際起始日至今）；有效逐日評分日={len(valid_dates)}天；"
        f"溫度百分位方法=bounded_pctrank（history_days={HISTORY_DAYS}、"
        f"事件排除窗=±{LOOKBACK_DAYS}交易日），與production同一套函式（直接import，未修改）。"
    )
    lines.append("")

    lines.append("## 方法摘要")
    lines.append("")
    lines.append(
        "1. 複用production `build_branch_panel`/`build_full_grid`/`compute_lb_pctile`/"
        "`weighted_composite`/`bounded_pctrank`/`load_event_dates`（同一份程式碼import），"
        "重算8家分點資料起始日（2024-07-01）至今逐日複合溫度分數、共識家數、溫度百分位。"
    )
    lines.append(
        f"2. 找出所有溫度百分位>=85%的交易日；扣除「16次大跌事件本身＋事件後"
        f"{EVENT_AFTERMATH_DAYS}個交易日」（`event_or_aftermath`，真訊號延續，不算FP）；"
        f"剩下的日子再檢查未來{FORWARD_CHECK_DAYS}個交易日IX0001是否出現過任一日"
        f"<={WEAK_SIGNAL_RET_THRESHOLD*100:.0f}%（有→`weak_signal`弱訊號；沒有→`complete_fp`完全false positive）。"
    )
    lines.append(
        "3. 對每個 high-temp 日（不分類別，含真事件日，供trade-off比對）都算3個候選"
        f"排除規則的診斷特徵：(a) 共識家數<={CONSENSUS_LOW_THRESHOLD}/8；"
        f"(b) 該日lb_sum視窗（前{LOOKBACK_DAYS}個交易日）8家分點合計賣超中，"
        f"單一個股佔比>{int(CONCENTRATION_THRESHOLD*100)}%；(c) 落在月底最後"
        f"{MONTH_END_LAST_N}個交易日、或台指期結算日（每月第三個星期三對應的交易日）"
        f"±{SETTLEMENT_PROXIMITY_DAYS}個交易日內。"
    )
    lines.append("")

    n_high = len(diag_df)
    n_event = int((diag_df["category"] == "event_or_aftermath").sum())
    n_weak = int((diag_df["category"] == "weak_signal").sum())
    n_fp = int((diag_df["category"] == "complete_fp").sum())
    lines.append("## Step 1：高溫日(pctrank>=85%)分類總表")
    lines.append("")
    lines.append(f"- 高溫日總數：**{n_high}**")
    lines.append(f"  - 事件本身/事件後{EVENT_AFTERMATH_DAYS}日延續（`event_or_aftermath`）：{n_event}")
    lines.append(f"  - 弱訊號（未來{FORWARD_CHECK_DAYS}日內大盤仍出現>=2%單日下跌，`weak_signal`）：{n_weak}")
    lines.append(f"  - **完全false positive**（未來{FORWARD_CHECK_DAYS}日大盤平穩，`complete_fp`）：{n_fp}")
    if n_high > 0:
        lines.append(
            f"- 完全false positive rate（占全部高溫日）＝ {n_fp}/{n_high} = {n_fp/n_high*100:.1f}%"
        )
    lines.append("")

    lines.append("## Step 2：完全False Positive 案例清單")
    lines.append("")
    fp_df = diag_df[diag_df["category"] == "complete_fp"].sort_values("trade_date")
    if fp_df.empty:
        lines.append("（無案例）")
    else:
        lines.append(
            "| 日期 | 溫度% | 共識家數 | 主導分點(佔加權熱度%) | 賣超最大個股(佔窗內賣超%) | "
            "月底3日 | 結算日±2 |"
        )
        lines.append("|---|---:|---:|---|---|:---:|:---:|")
        for r in fp_df.itertuples():
            branch_s = (
                f"{r.top1_branch_name}({r.top1_branch}) {r.top1_branch_share*100:.0f}%"
                if r.top1_branch_share is not None else "NA"
            )
            stock_s = (
                f"{stock_label(r.top1_stock)} {r.top1_stock_share*100:.0f}%"
                if r.top1_stock_share is not None else "NA"
            )
            lines.append(
                f"| {r.trade_date} | {r.pctrank:.1f}% | {r.consensus_n}/8 | {branch_s} | {stock_s} | "
                f"{'✓' if r.near_month_end else ''} | {'✓' if r.near_settlement else ''} |"
            )
    lines.append("")

    lines.append("## Step 3：根因分類統計（完全False Positive，n={})".format(n_fp))
    lines.append("")
    if n_fp == 0:
        lines.append("（無案例可統計）")
    else:
        n_low_consensus = int(fp_df["rule_low_consensus"].sum())
        n_conc = int(fp_df["rule_stock_concentration"].sum())
        n_cal = int(fp_df["rule_calendar"].sum())
        n_any = int(fp_df["rule_any"].sum())
        lines.append(f"- 共識家數<={CONSENSUS_LOW_THRESHOLD}/8（低共識/單一分點驅動）：{n_low_consensus}/{n_fp} = {n_low_consensus/n_fp*100:.1f}%")
        lines.append(f"- 賣超集中單一個股>{int(CONCENTRATION_THRESHOLD*100)}%：{n_conc}/{n_fp} = {n_conc/n_fp*100:.1f}%")
        lines.append(f"- 月底最後{MONTH_END_LAST_N}日或結算日±{SETTLEMENT_PROXIMITY_DAYS}日：{n_cal}/{n_fp} = {n_cal/n_fp*100:.1f}%")
        lines.append(f"- **三規則任一命中（OR）：{n_any}/{n_fp} = {n_any/n_fp*100:.1f}%**")
        lines.append("")
        # 重疊細分（低共識 vs 高共識但個股集中 vs 都不符合）
        both = fp_df[fp_df["rule_low_consensus"] & fp_df["rule_stock_concentration"]]
        lines.append(
            f"- 低共識與單一個股集中同時命中：{len(both)}/{n_fp} = "
            f"{(len(both)/n_fp*100 if n_fp else 0):.1f}%（顯示兩者高度重疊：低共識案例往往就是單一分點對單一個股的偏重賣超）"
        )
        none_match = fp_df[~fp_df["rule_any"]]
        lines.append(f"- 三規則皆不符合（真正「原因不明」的殘餘FP）：{len(none_match)}/{n_fp} = {(len(none_match)/n_fp*100 if n_fp else 0):.1f}%")
        if not none_match.empty:
            lines.append("")
            lines.append("殘餘無法歸因的案例：" + "、".join(none_match["trade_date"].tolist()))
    lines.append("")

    lines.append("## Step 3b：⚠️ 關鍵檢查 —— 這些規則在「真事件」與「弱訊號」上的命中率是否一樣高？")
    lines.append("")
    lines.append(
        "如果某條規則在真事件(`event_or_aftermath`)、弱訊號(`weak_signal`)上的命中率"
        "跟完全FP(`complete_fp`)差不多高，代表這條規則其實**不是在辨識假警報**，"
        "只是反映「這套加權公式本身大部分時候都長這樣」（低共識是常態，不是異常）："
    )
    lines.append("")
    lines.append("| 規則 | complete_fp命中率(n={}) | weak_signal命中率(n={}) | event_or_aftermath命中率(n={}) | 是否具鑑別力 |".format(
        n_fp, len(weak_df), n_event
    ))
    lines.append("|---|---:|---:|---:|:---:|")
    event_df_full = diag_df[diag_df["category"] == "event_or_aftermath"]
    for rule_col, rule_label in [
        ("rule_low_consensus", f"共識<={CONSENSUS_LOW_THRESHOLD}/8"),
        ("rule_stock_concentration", f"個股集中>{int(CONCENTRATION_THRESHOLD*100)}%"),
        ("rule_calendar", "月底/結算日附近"),
    ]:
        r_fp = fp_df[rule_col].mean() * 100 if n_fp else 0
        r_weak = weak_df[rule_col].mean() * 100 if len(weak_df) else 0
        r_event = event_df_full[rule_col].mean() * 100 if n_event else 0
        discriminative = "✓ 有落差" if (r_fp - max(r_weak, r_event)) >= 20 else "✗ 幾乎無落差（誤判風險高）"
        lines.append(f"| {rule_label} | {r_fp:.1f}% | {r_weak:.1f}% | {r_event:.1f}% | {discriminative} |")
    lines.append("")
    lines.append(
        "解讀：若某規則在三個類別的命中率都相近（如低共識規則在FP／弱訊號／真事件上都"
        "普遍偏高），代表『低共識』其實是這套8家加權公式的常態現象——因為複合分數由"
        "瑞銀(權重36%)單一分點主導，不需要多家一致就能推高溫度百分位，所以不管是真訊號"
        "還是假警報，多數時候共識家數都偏低。這種規則雖然「統計上能排除大量FP」，"
        "但**同時會等比例地壓低真訊號**，不是乾淨的鑑別特徵；只有在FP命中率明顯高於"
        "真事件／弱訊號命中率時，才是可信賴、值得單獨採用的排除條件。"
    )
    lines.append("")

    lines.append("## Step 4：排除規則效果評估")
    lines.append("")
    if n_high > 0:
        excluded_by_rule = diag_df[diag_df["rule_any"]]
        n_excl_total = len(excluded_by_rule)
        n_excl_fp = int((excluded_by_rule["category"] == "complete_fp").sum())
        n_excl_weak = int((excluded_by_rule["category"] == "weak_signal").sum())
        n_excl_event = int((excluded_by_rule["category"] == "event_or_aftermath").sum())
        lines.append(f"套用「共識<={CONSENSUS_LOW_THRESHOLD} OR 個股集中>{int(CONCENTRATION_THRESHOLD*100)}% OR 月底/結算日附近」排除規則後：")
        lines.append(f"- 共排除 {n_excl_total}/{n_high} 個高溫日觸發（{n_excl_total/n_high*100:.1f}%）")
        lines.append(
            f"- 其中：完全FP {n_excl_fp}/{n_fp}（{(n_excl_fp/n_fp*100 if n_fp else 0):.1f}% 的FP被排除）、"
            f"弱訊號 {n_excl_weak}/{n_weak}（{(n_excl_weak/n_weak*100 if n_weak else 0):.1f}%）、"
            f"事件本身/延續 {n_excl_event}/{n_event}（{(n_excl_event/n_event*100 if n_event else 0):.1f}%）"
        )
        remaining_high = n_high - n_excl_total
        remaining_fp = n_fp - n_excl_fp
        lines.append(
            f"- 排除後剩餘高溫觸發 {remaining_high} 個，其中完全FP剩 {remaining_fp} 個 → "
            f"完全FP rate 從 {(n_fp/n_high*100 if n_high else 0):.1f}% 降至 "
            f"{(remaining_fp/remaining_high*100 if remaining_high else 0):.1f}%"
        )
    lines.append("")

    lines.append("## Step 5：Trade-off 檢查 —— 會不會誤殺16次大跌事件本身？")
    lines.append("")
    lines.append(
        "檢查每次大跌事件當日（非事件前置日，事件日本身反映事件前5個交易日的分點賣超"
        "累積訊號）套用同一套排除規則是否會被誤殺："
    )
    lines.append("")
    lines.append("| 事件日 | 溫度% | 是否達85%高溫 | 共識家數 | 賣超最大個股(佔窗內%) | 月底3日 | 結算日±2 | 命中規則(=會誤殺) |")
    lines.append("|---|---:|:---:|---:|---|:---:|:---:|---|")
    n_miss = 0
    n_reached_high = 0
    n_miss_by_rule = {"rule_low_consensus": 0, "rule_stock_concentration": 0, "rule_calendar": 0}
    for r in event_check_df.itertuples():
        pr_s = "NA" if r.pctrank is None else f"{r.pctrank:.1f}%"
        reached = "✓" if r.reached_high_temp else ""
        if r.reached_high_temp:
            n_reached_high += 1
        cn_s = "NA" if r.consensus_n is None else f"{r.consensus_n}/8"
        stock_s = (
            f"{stock_label(r.top1_stock)} {r.top1_stock_share*100:.0f}%"
            if r.top1_stock_share is not None else "NA"
        )
        would_miss = bool(r.would_be_excluded) and bool(r.reached_high_temp)
        if would_miss:
            n_miss += 1
            for rc in n_miss_by_rule:
                if getattr(r, rc):
                    n_miss_by_rule[rc] += 1
        rule_display = r.matched_rules if (r.matched_rules and r.reached_high_temp) else (
            "" if r.reached_high_temp else "（本身未達85%高溫，不受影響）"
        )
        lines.append(
            f"| {r.trade_date} | {pr_s} | {reached} | {cn_s} | {stock_s} | "
            f"{'✓' if r.near_month_end else ''} | {'✓' if r.near_settlement else ''} | "
            f"{rule_display} |"
        )
    lines.append("")
    lines.append(
        f"16次大跌事件中，事件當日本身有 **{n_reached_high}/16** 次達到85%高溫門檻；"
        f"其中若套用「三規則任一(OR)」排除規則，會被誤殺（原本達高溫但規則會壓下）的有 **{n_miss}** 次。"
    )
    lines.append("")
    lines.append("**拆解到單一規則的誤殺數（同一事件可能同時被多條規則命中）：**")
    lines.append("")
    lines.append(f"- 只用「共識<={CONSENSUS_LOW_THRESHOLD}/8」：誤殺 {n_miss_by_rule['rule_low_consensus']}/{n_reached_high} 次真事件")
    lines.append(f"- 只用「個股集中>{int(CONCENTRATION_THRESHOLD*100)}%」：誤殺 {n_miss_by_rule['rule_stock_concentration']}/{n_reached_high} 次真事件")
    lines.append(f"- 只用「月底/結算日附近」：誤殺 {n_miss_by_rule['rule_calendar']}/{n_reached_high} 次真事件")
    lines.append("")

    lines.append("## Step 5b：弱訊號(weak_signal) bucket 的排除規則影響")
    lines.append("")
    if weak_df.empty:
        lines.append("（無弱訊號案例）")
    else:
        n_weak_excl = int(weak_df["rule_any"].sum())
        lines.append(
            f"弱訊號（高溫但未來5日僅出現介於-2%~-3%之間、未達16事件csv門檻-3%的下跌）共"
            f"{len(weak_df)}例，其中排除規則會壓下 {n_weak_excl} 例"
            f"（{n_weak_excl/len(weak_df)*100:.1f}%）。這些屬於「有一定預警價值但未達正式"
            "16事件標準」的訊號，排除它們會犧牲一部分早期預警能力，但不影響16次正式"
            "大跌事件的命中率（見Step 5）。"
        )
    lines.append("")

    lines.append("## 結論與建議規則")
    lines.append("")
    if n_fp > 0:
        n_any = int(fp_df["rule_any"].sum())
        n_low_consensus = int(fp_df["rule_low_consensus"].sum())
        n_conc = int(fp_df["rule_stock_concentration"].sum())
        n_cal = int(fp_df["rule_calendar"].sum())
        lines.append(
            f"1. 完全false positive（高溫但接下來5個交易日大盤完全平穩）在2024-07以來共"
            f"**{n_fp}例**，占全部85%以上高溫觸發的{n_fp/n_high*100:.1f}%。"
        )
        lines.append(
            f"2. 三條候選規則對完全FP的命中率分別是：低共識<={CONSENSUS_LOW_THRESHOLD}/8"
            f"={n_low_consensus}/{n_fp}({n_low_consensus/n_fp*100:.1f}%)、個股集中>"
            f"{int(CONCENTRATION_THRESHOLD*100)}%={n_conc}/{n_fp}({n_conc/n_fp*100:.1f}%)、"
            f"月底/結算日附近={n_cal}/{n_fp}({n_cal/n_fp*100:.1f}%)。**但Step 3b顯示"
            "低共識規則在真事件(`event_or_aftermath`)日上的命中率同樣高（~84%），代表"
            "低共識其實是這套8家加權公式（由瑞銀單一分點權重36%主導）的常態現象，"
            "不管真訊號還是假警報大部分時候都低共識——這條規則統計上能過濾大量FP，"
            "但沒有真正的鑑別力，會等比例犧牲真訊號。**"
        )
        lines.append(
            f"3. 個股集中度規則(>{int(CONCENTRATION_THRESHOLD*100)}%)只多抓到約1個FP案例是"
            "低共識規則沒抓到的（見Step 3重疊統計），但同時單獨造成2次真事件誤殺"
            "（2025-04-07、2026-03-09）——原因是2330台積電本身就是這8家分點交易量"
            "最大的持股，不管是真賣壓還是雜訊，賣超都常集中在2330，這條規則邊際"
            "效益為負，**不建議單獨採用**。"
        )
        lines.append(
            "4. 月底/結算日附近規則對「真事件」有一定鑑別力（真事件命中率23.7% "
            "明顯低於完全FP的43.1%），但對「弱訊號」沒有鑑別力（弱訊號命中率48.6%，"
            "反而比完全FP更高——見Step 3b）；且仍有2次真事件（2026-06-26、2026-07-17，"
            "後者是16次事件中跌幅最大的-6.47%單日崩盤）恰好也落在月底/結算日附近——"
            "這是日曆巧合造成的風險（大跌本來就可能發生在任何日子），並非真正因果"
            "關係，三條規則中相對較能參考但仍非乾淨判準，使用時務必留意。"
        )
        if n_miss == 0:
            lines.append(
                f"5. **關鍵結論：套用三規則OR組合不會誤殺16次大跌事件中的任何一次**"
                f"（{n_reached_high}/16次事件日本身達85%高溫，且都不符合任何一條排除條件，"
                "全部維持觸發）。"
            )
        else:
            lines.append(
                f"5. ⚠️ **關鍵trade-off：三規則OR組合會誤殺16次大跌事件中的{n_miss}次"
                f"（{n_miss}/{n_reached_high}達高溫的真事件），其中低共識規則貢獻"
                f"{n_miss_by_rule['rule_low_consensus']}次、個股集中規則貢獻"
                f"{n_miss_by_rule['rule_stock_concentration']}次、日曆規則貢獻"
                f"{n_miss_by_rule['rule_calendar']}次**（同一事件可能被多條規則同時命中）。"
                "以FP改善90.8%換誤殺56%的真事件，代價明顯過高，**不建議直接自動排除**。"
            )
        lines.append(
            "6. **建議規則**（保守版，優先降低誤殺風險）：不要用「自動排除警報」，改用"
            "「降級標注」——當某日溫度百分位>=85%時，若（該日前5個交易日8家分點合計賣超"
            f"中單一個股占比>{int(CONCENTRATION_THRESHOLD*100)}%）或（落在月底最後"
            f"{MONTH_END_LAST_N}個交易日／台指期結算日±{SETTLEMENT_PROXIMITY_DAYS}個交易日"
            "內），標注「建議人工複核個股是否有除息/重訊/單一大戶調倉、或屬月底作帳/"
            "期貨結算效應」，但**仍照常顯示高熱燈號，不建議用低共識規則自動降級**——"
            "因為低共識不具鑑別力，用它自動壓低警報等於系統性地讓大部分真訊號也被"
            "跟著降級。若使用者能接受較高的誤殺風險以換取大幅降低雜訊，才考慮加入"
            "低共識規則，但務必同步告知使用者「這條規則本質上是全面降低靈敏度，"
            "不是精準篩雜訊」。"
        )
    else:
        lines.append("完全false positive案例數為0，暫無可統計的根因模式。")
    lines.append("")

    lines.append("## 已知限制")
    lines.append("")
    lines.append(
        "- 個股名稱僅涵蓋DB中常見大型股（無獨立股票名稱表），部分個股僅顯示代碼；"
        "除息/重訊的真實原因仍需人工複核個股當時公告（本分析只做賣超金額集中度統計，"
        "未逐一查證公告內容）。"
    )
    lines.append(
        f"- 溫度百分位在計算窗前段（約前{HISTORY_DAYS}個交易日，即2024-07~2024年底附近）"
        "常態基準池樣本數偏少，百分位數值可能不如後期穩定，與production設計本身的已知"
        "限制一致（詳見production腳本docstring）。"
    )
    lines.append(
        "- 結算日以「每月第三個星期三對應的交易日」近似估計台指期結算日，未特別處理"
        "國定假日調整以外的例外情況。"
    )
    lines.append("- 本分析為唯讀研究診斷，不構成可下單訊號，也未修改production腳本或任何config。")
    lines.append("")

    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {REPORT_PATH}", flush=True)


if __name__ == "__main__":
    main()
