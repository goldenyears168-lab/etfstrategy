#!/usr/bin/env python3
"""大跌溫度計 · 每日監控 dashboard（加權8家「聰明錢」分點 + 共識家數）.

Research / monitoring diagnostic · 非正式風控規則 · 非可下單訊號 ·
尚未登錄 `config/research.yaml`。

## 背景（凍結來源）

沿用 `run_market_crash_branch_precursor_scan.py` 的訊號定義（分點對全市場
net(股)×close 加總，前 5 個交易日累計，對分點自身歷史取百分位），但把
「哪些分點、怎麼合成一個分數」換成經 walk-forward 驗證過的**加權**組合，
取代原本的具名 panel／單純 2 家平權版本：

- 加權8家（權重＝各分點在 bounded-rolling walk-forward 驗證的個別判別力−50，
  越強的分點權重越高）：平均判別力 84.5%、最差一次 46.3%（15個滾動大跌
  事件，2024-07~2026-07），優於等權2家（80.3%／38.3%）與等權8家
  （78.6%／5.6%，混入弱分點反而拖累）。
- 逐家個別判別力見下方 `PANEL`／`WEIGHT_SOURCE_NOTE`。

## 兩個讀數

1. **溫度百分位**（0~100%）：加權複合分數，對「最近 `--history-days`
   個交易日」同一複合分數的歷史百分位排名。50%=跟平常沒差別，
   ≥90%=近期最熱的等級之一。
2. **共識家數**（0~8）：8家裡有幾家「當日自己前5日累計賣超」落在自己
   歷史後10%（自我常態化異常重賣超；統一仁愛例外用5%，見 `TAIL_PCT_OVERRIDE`
   ／2026-07-23 LOOCV n=16與n=30兩輪驗證：其餘7家放寬到20-30%只是拿誤報率
   換命中率不划算，唯獨統一仁愛收緊到5%命中率不變、誤報率減半）。共識越高，
   代表不是單一分點的個別行為，是廣泛的機構賣壓。

## 已知限制（务必读）

- 大跌溫度計亮紅燈≠明天必崩盤：24個大漲事件回測中，約4成（9/23）在
  溫度計也讀到高分，其中多數（多於一半）是「前次大跌事件10個交易日內
  的反彈」殘留訊號，非獨立警訊；詳見對話記錄或
  `reports/research/branch-footprint-screen/breadth_walkforward_summary.md`
  系列研究。若目前 asof_date 落在最近一次大跌事件後 10 個交易日內，
  dashboard 會額外標注「可能為前次大跌延續訊號」。
- 本溫度計的分點組合是用全部歷史資料篩選＋加權出來的（非嚴格逐日
  walk-forward 重新選股），84.5% 判別力反映「這個固定公式套用在更大
  樣本事件上是否還站得住腳」，不是「從零開始只用過去資料會不會選中
  這8家」。
- 兩個計算步驟都需要長歷史才能貼近研究驗證數字（2026-07-23 修正）：
  (1) 各分點「自我百分位」用 `--self-rank-days`（預設500個交易日≈2年）
  計算，太短會稀釋 top10%尾端定義；(2) 溫度百分位用 bounded-rolling
  常態日基準池（`--history-days`，預設120天）＋排除任一大跌/大漲事件
  ±`--lookback-days`個交易日內的日子，與
  `run_market_precursor_thermometer_candidates.py` 驗證 84.5% 判別力
  同一套方法。

輸出：
  reports/daily/{YYYYMMDD}_crash_thermometer.md
  reports/daily/crash_thermometer.md（穩定路徑）

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_crash_thermometer_dashboard.py [--notify]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

DB_PATH = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "daily"
SOURCE = "finmind"

# 分點ID → (名稱, 權重＝個別 walk-forward 判別力% − 50)
PANEL: dict[str, tuple[str, float]] = {
    "1650": ("瑞銀", 36.0),
    "1560": ("港商野村", 22.7),
    "1480": ("美商高盛亞", 20.7),
    "1360": ("港麥格理", 12.1),
    "984K": ("元大館前", 10.0),
    "1261": ("宏遠民生", 7.8),
    "5850": ("統一", 4.5),
    "585c": ("統一仁愛", 4.5),
}
WEIGHT_SOURCE_NOTE = (
    "權重來自 2026-07-22 bounded-rolling walk-forward 個別分點判別力 "
    "(瑞銀86.0%/港商野村72.7%/美商高盛亞70.7%/港麥格理62.1%/元大館前60.0%/"
    "宏遠民生57.8%/統一54.5%/統一仁愛54.5%，權重=各自−50)。"
)
LOOKBACK_DAYS_DEFAULT = 5
HISTORY_DAYS_DEFAULT = 120
SELF_RANK_DAYS_DEFAULT = 500  # ~2年交易日，貼近研究驗證窗（2024-07..now）
TAIL_PCT = 0.10
# 逐家客製化尾端比例的例外清單（2026-07-23 驗證：LOOCV n=16／n=30 兩輪測試，
# 多數分點把門檻放寬到20-30%只是拿誤報率換命中率（FPR同步跳升3-5倍），不划算；
# 但統一仁愛(585c)收緊到5%後命中率不變、FPR減半（8.2%→3.7%），是唯一乾淨的
# 改善，其餘7家維持統一10%。
TAIL_PCT_OVERRIDE: dict[str, float] = {"585c": 0.05}


def tail_pct_for(branch_id: str) -> float:
    return TAIL_PCT_OVERRIDE.get(branch_id, TAIL_PCT)
REBOUND_WINDOW_DAYS = 10
CRASH_THRESHOLD = -0.03
RALLY_THRESHOLD = 0.03


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_calendar(
    conn: sqlite3.Connection, *, end: str, n_days: int, extra_ids: list[str] | None = None
) -> list[str]:
    """交易日曆。以 2330 收盤日為主幹，但 `stock_daily_bars`（收盤價表）偶爾會有
    整批缺漏（例如2026-07-14~16 EOD同步不完整，見2026-07-23已知問題），此時仍
    可能有真實分點成交資料。因此再補進 `extra_ids`（分點清單）在
    `stock_broker_branch_daily` 有出現、但 2330 收盤表沒有的日期，避免漏掉真實
    交易日。"""
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE source=? AND stock_id='2330' AND trade_date <= ?
        ORDER BY trade_date DESC LIMIT ?
        """,
        (SOURCE, end, n_days),
    ).fetchall()
    dates = {str(r[0]) for r in rows}
    if dates and extra_ids:
        floor_date = min(dates)
        placeholders = ",".join("?" for _ in extra_ids)
        extra_rows = conn.execute(
            f"""
            SELECT DISTINCT trade_date FROM stock_broker_branch_daily
            WHERE source=? AND trade_date >= ? AND trade_date <= ?
              AND securities_trader_id IN ({placeholders})
            """,
            (SOURCE, floor_date, end, *extra_ids),
        ).fetchall()
        dates |= {str(r[0]) for r in extra_rows}
    return sorted(dates)


def latest_trade_date(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT MAX(trade_date) FROM stock_daily_bars WHERE source=? AND stock_id='2330'",
        (SOURCE,),
    ).fetchone()
    if not row or not row[0]:
        raise SystemExit("no stock_daily_bars data found")
    return str(row[0])


def build_branch_panel(conn: sqlite3.Connection, ids: list[str], *, start: str, end: str) -> pd.DataFrame:
    """net_amt(NT$) = net(股) × close。收盤價用「當日或最近一個有值的前一交易日」
    （逐股 forward-fill），而非要求精確同日 join——`stock_daily_bars` 偶爾整批
    缺漏（見2026-07-23已知問題：曾經因為 inner join 精確配對，把有真實分點
    賣超的交易日靠 close 缺漏悄悄清成 0），forward-fill 只在缺漏當天用近似
    （前一日）價格，不會整天憑空消失。"""
    placeholders = ",".join("?" for _ in ids)
    raw = pd.read_sql_query(
        f"""
        SELECT securities_trader_id, trade_date, stock_id, net
        FROM stock_broker_branch_daily
        WHERE source=? AND trade_date >= ? AND trade_date <= ?
          AND stock_id != '__EMPTY__' AND length(stock_id)=4
          AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND securities_trader_id IN ({placeholders})
        """,
        conn,
        params=[SOURCE, start, end, *ids],
    )
    if raw.empty:
        return pd.DataFrame(columns=["securities_trader_id", "trade_date", "net_amt"])
    closes = pd.read_sql_query(
        """
        SELECT stock_id, trade_date, close FROM stock_daily_bars
        WHERE source=? AND trade_date >= ? AND trade_date <= ? AND close > 0
          AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
        """,
        conn,
        params=[SOURCE, start, end],
    )
    raw["securities_trader_id"] = raw["securities_trader_id"].astype(str)
    raw["stock_id"] = raw["stock_id"].astype(str)
    raw["trade_date"] = raw["trade_date"].astype(str)
    closes["stock_id"] = closes["stock_id"].astype(str)
    closes["trade_date"] = closes["trade_date"].astype(str)

    # 逐股把收盤價鋪到 raw 出現過的所有 trade_date，再 forward-fill 補上整批
    # 缺漏的日子（如某天全市場 close 都沒進 DB），避免 inner join 把那天的
    # 分點資料整批丟掉。
    all_dates = sorted(set(raw["trade_date"]) | set(closes["trade_date"]))
    stocks_needed = sorted(set(raw["stock_id"]) & set(closes["stock_id"]))
    closes = closes[closes["stock_id"].isin(stocks_needed)]
    wide = closes.pivot(index="trade_date", columns="stock_id", values="close")
    wide = wide.reindex(all_dates).ffill()
    long_closes = wide.stack().rename("close").reset_index()
    long_closes.columns = ["trade_date", "stock_id", "close"]

    merged = raw.merge(long_closes, on=["stock_id", "trade_date"], how="inner")
    merged["net_amt"] = merged["net"].astype(float) * merged["close"].astype(float)
    grp = merged.groupby(["securities_trader_id", "trade_date"], as_index=False).agg(net_amt=("net_amt", "sum"))
    return grp


def build_full_grid(panel: pd.DataFrame, ids: list[str], cal: list[str]) -> pd.DataFrame:
    full_idx = pd.MultiIndex.from_product([ids, cal], names=["securities_trader_id", "trade_date"])
    p = panel.set_index(["securities_trader_id", "trade_date"])
    grid = p.reindex(full_idx).fillna(0.0).reset_index()
    grid["trade_date"] = grid["trade_date"].astype(str)
    return grid


def compute_lb_pctile(grid: pd.DataFrame, lookback_days: int) -> pd.DataFrame:
    grid = grid.sort_values(["securities_trader_id", "trade_date"]).copy()

    def _per_branch(g: pd.DataFrame) -> pd.DataFrame:
        lb_sum = g["net_amt"].shift(1).rolling(lookback_days).sum()
        lb_pctile = lb_sum.rank(pct=True, na_option="keep")
        return pd.DataFrame({"lb_sum_ntd": lb_sum, "lb_pctile": lb_pctile})

    out = grid.groupby("securities_trader_id", group_keys=False).apply(_per_branch, include_groups=False)
    return pd.concat([grid.reset_index(drop=True), out.reset_index(drop=True)], axis=1)


def weighted_composite(scored: pd.DataFrame, weights: dict[str, float]) -> pd.DataFrame:
    """Per trade_date: weighted mean lb_pctile across panel -> composite_score (crash-direction, high=hot)."""
    sub = scored.dropna(subset=["lb_pctile"]).copy()
    sub["w"] = sub["securities_trader_id"].map(weights)
    sub["w_lb_pctile"] = sub["w"] * sub["lb_pctile"]
    grp = sub.groupby("trade_date").agg(
        w_sum=("w", "sum"),
        w_lb_pctile_sum=("w_lb_pctile", "sum"),
        n_branches=("securities_trader_id", "nunique"),
    )
    grp["composite_score"] = 1.0 - (grp["w_lb_pctile_sum"] / grp["w_sum"])
    sub["tail_pct"] = sub["securities_trader_id"].map(tail_pct_for)
    consensus = (
        sub[sub["lb_pctile"] <= sub["tail_pct"]].groupby("trade_date")["securities_trader_id"].nunique()
    )
    grp["consensus_n"] = consensus.reindex(grp.index).fillna(0).astype(int)
    return grp.reset_index().sort_values("trade_date")


def band_for(pctrank: float | None) -> str:
    if pctrank is None:
        return "⚪ 資料不足"
    if pctrank >= 90:
        return "🔴 高熱"
    if pctrank >= 70:
        return "🟠 偏熱"
    if pctrank >= 50:
        return "🟡 微溫"
    return "🟢 正常"


def load_event_dates(conn: sqlite3.Connection, cal: list[str]) -> tuple[set[str], set[str]]:
    """回傳 (crash_dates, all_event_dates)。all_event_dates=大跌(≤-3%)∪大漲(≥+3%)，
    用於下方 bounded normal pool 排除「事件鄰近日」——避免用大跌/大漲當天附近
    的異常日去稀釋「正常基準」，這是研究驗證 84.5% 判別力用的同一套
    bounded-rolling + 事件排除方法（見
    `run_market_precursor_thermometer_candidates.py`）。"""
    from market_benchmark import load_benchmark_close

    bench = load_benchmark_close(conn, code="IX0001").sort_index()
    ret = bench.pct_change()
    ret = ret[ret.index.isin(cal)]
    crash_dates = {str(d) for d in ret[ret <= CRASH_THRESHOLD].index}
    rally_dates = {str(d) for d in ret[ret >= RALLY_THRESHOLD].index}
    return crash_dates, crash_dates | rally_dates


def find_recent_crash_episode(crash_dates: set[str], cal: list[str], asof: str) -> str | None:
    """離 asof 最近一次大跌日（單日 IX0001 ≤ -3%），僅供反彈延續訊號標注。"""
    candidates = [d for d in crash_dates if d in cal and d != asof]
    return max(candidates) if candidates else None


def bounded_pctrank(
    scores_by_date: dict[str, float],
    cal: list[str],
    event_idx: set[int],
    *,
    test_date: str,
    history_days: int,
    lookback_days: int,
) -> tuple[float | None, int]:
    """對 test_date 的複合分數，對「test_date 之前 history_days 個交易日、且距任一
    事件日 > lookback_days 個交易日」的 bounded normal pool 取百分位排名。
    與研究驗證（`run_market_precursor_thermometer_candidates.py` walk_forward_candidate）
    同一套方法，kind='mean' 百分位。"""
    idx_of = {d: i for i, d in enumerate(cal)}
    if test_date not in idx_of:
        return None, 0
    test_idx = idx_of[test_date]

    def is_far(day_idx: int) -> bool:
        return all(abs(day_idx - ei) > lookback_days for ei in event_idx)

    floor_idx = max(0, test_idx - history_days)
    normal_pool = [
        d for d in cal
        if floor_idx <= idx_of[d] < test_idx and is_far(idx_of[d]) and d in scores_by_date
    ]
    test_score = scores_by_date.get(test_date)
    if test_score is None:
        return None, len(normal_pool)
    normal_scores = [scores_by_date[d] for d in normal_pool]
    if not normal_scores:
        return None, 0
    combined = normal_scores + [test_score]
    pr = float(stats.percentileofscore(combined, test_score, kind="mean"))
    return pr, len(normal_scores)


def rolling_pctrank_trend(
    series: pd.DataFrame,
    cal: list[str],
    event_idx: set[int],
    *,
    upto: str,
    trend_days: int,
    history_days: int,
    lookback_days: int,
) -> pd.DataFrame:
    """對最近 trend_days 個交易日，逐日回算 bounded+事件排除的溫度百分位，
    供 30 天趨勢表使用。"""
    s = series.dropna(subset=["composite_score"]).copy()
    scores_by_date = dict(zip(s["trade_date"], s["composite_score"]))
    consensus_by_date = dict(zip(s["trade_date"], s["consensus_n"]))
    cal_upto = [d for d in cal if d <= upto and d in scores_by_date]
    target_dates = cal_upto[-trend_days:]
    rows = []
    for d in target_dates:
        pr, n_normal = bounded_pctrank(
            scores_by_date, cal, event_idx,
            test_date=d, history_days=history_days, lookback_days=lookback_days,
        )
        rows.append(
            {
                "trade_date": d,
                "composite_score": scores_by_date[d],
                "pctrank": pr,
                "n_normal_days": n_normal,
                "consensus_n": int(consensus_by_date[d]),
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily crash thermometer dashboard")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--asof-date", default=None, help="預設：資料庫最新可得交易日")
    ap.add_argument("--lookback-days", type=int, default=LOOKBACK_DAYS_DEFAULT)
    ap.add_argument("--history-days", type=int, default=HISTORY_DAYS_DEFAULT)
    ap.add_argument("--trend-days", type=int, default=30, help="信中30天趨勢表的天數")
    ap.add_argument(
        "--self-rank-days",
        type=int,
        default=SELF_RANK_DAYS_DEFAULT,
        help="各分點「自我百分位」所用的歷史天數（須貼近研究驗證用的~2年窗，"
        "太短會稀釋 top10%%尾端定義，見已知限制）",
    )
    ap.add_argument("--notify", action="store_true", help="寄送 Gmail 通知")
    args = ap.parse_args()

    conn = connect_ro(args.db)
    asof = args.asof_date or latest_trade_date(conn)
    print(f"asof_date={asof}", flush=True)

    ids = list(PANEL.keys())

    # 自我百分位（lb_pctile）的計算需要長期歷史（貼近研究驗證的 ~2 年窗），
    # 否則 tail-10% 門檻會被稀釋而低估（見 2026-07-23 修正：7/17 大跌日原本
    # 只讀到 76%/3家共識，用長窗重算後對齊研究驗證的 95%/4家共識）。
    cal = build_calendar(
        conn, end=asof, n_days=args.self_rank_days + args.lookback_days + 5, extra_ids=ids
    )
    if asof not in cal:
        raise SystemExit(f"asof_date {asof} not in trading calendar (check DB sync)")
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)

    panel_raw = build_branch_panel(conn, ids, start=cal[0], end=asof)
    print(f"panel rows={len(panel_raw)}", flush=True)

    grid = build_full_grid(panel_raw, ids, cal)
    scored = compute_lb_pctile(grid, args.lookback_days)

    weights = {bid: w for bid, (_, w) in PANEL.items()}
    series = weighted_composite(scored, weights)

    today_row = series[series["trade_date"] == asof]
    if today_row.empty or pd.isna(today_row["composite_score"].iloc[0]):
        raise SystemExit(f"insufficient history to score {asof} (need >= {args.lookback_days} prior days)")
    today_score = float(today_row["composite_score"].iloc[0])
    today_consensus = int(today_row["consensus_n"].iloc[0])

    crash_dates, all_event_dates = load_event_dates(conn, cal)
    event_idx = {cal.index(d) for d in all_event_dates if d in cal}
    scores_by_date = dict(zip(series["trade_date"], series["composite_score"]))
    pctrank, n_normal = bounded_pctrank(
        scores_by_date, cal, event_idx,
        test_date=asof, history_days=args.history_days, lookback_days=args.lookback_days,
    )
    band = band_for(pctrank)

    recent_crash = find_recent_crash_episode(crash_dates, cal, asof)
    rebound_note = ""
    if recent_crash:
        gap = cal.index(asof) - cal.index(recent_crash)
        if 0 < gap <= REBOUND_WINDOW_DAYS:
            rebound_note = (
                f"⚠️ 距上次大跌事件（{recent_crash}）僅 {gap} 個交易日內——"
                "目前讀數可能是延續訊號，非獨立新警訊。"
            )

    per_branch_lines = []
    day_scored = scored[scored["trade_date"] == asof]
    for bid, (name, w) in sorted(PANEL.items(), key=lambda kv: -kv[1][1]):
        row = day_scored[day_scored["securities_trader_id"] == bid]
        if row.empty or pd.isna(row["lb_pctile"].iloc[0]):
            per_branch_lines.append((name, bid, w, None, None))
            continue
        lb_p = float(row["lb_pctile"].iloc[0])
        lb_sum = float(row["lb_sum_ntd"].iloc[0])
        per_branch_lines.append((name, bid, w, lb_p, lb_sum))

    trend = rolling_pctrank_trend(
        series, cal, event_idx,
        upto=asof, trend_days=args.trend_days, history_days=args.history_days,
        lookback_days=args.lookback_days,
    )
    conn.close()

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = asof.replace("-", "")

    pctrank_s = "NA" if pctrank is None else f"{pctrank:.1f}%"
    lines = [
        f"大跌溫度計 · {asof}",
        "",
        "Research / monitoring diagnostic · 非正式風控規則 · 非可下單訊號。",
        "",
        "===== 今日讀數 =====",
        "",
        f"溫度百分位：{pctrank_s} {band}",
        f"共識家數：{today_consensus}/8"
        f"（分點自身前{args.lookback_days}日累計賣超落在自己歷史後{int(TAIL_PCT*100)}%，"
        f"統一仁愛為{int(TAIL_PCT_OVERRIDE['585c']*100)}%，見下方明細註記）",
        f"複合分數（原始值）：{today_score:.4f}",
    ]
    if rebound_note:
        lines.append(rebound_note)

    lines += [
        "",
        f"===== 近{args.trend_days}個交易日趨勢 =====",
        "",
        "日期        溫度%   燈號     共識  分數",
    ]
    for r in trend.sort_values("trade_date", ascending=False).itertuples():
        pr_s = "  NA" if r.pctrank is None else f"{r.pctrank:5.1f}"
        mark = " ←今日" if r.trade_date == asof else ""
        lines.append(
            f"{r.trade_date}  {pr_s}%  {band_for(r.pctrank):8s} {r.consensus_n}/8  "
            f"{r.composite_score:.4f}{mark}"
        )

    lines += [
        "",
        "===== 8家明細（依權重排序） =====",
        "",
        "分點(ID)              權重  前5日累計淨額(NT$)     自我百分位  異常重賣超(門檻)",
    ]
    for name, bid, w, lb_p, lb_sum in per_branch_lines:
        lb_p_s = "  NA" if lb_p is None else f"{lb_p*100:5.1f}%"
        lb_sum_s = "NA" if lb_sum is None else f"{lb_sum:>18,.0f}"
        tp = tail_pct_for(bid)
        flag = "✓" if (lb_p is not None and lb_p <= tp) else ""
        tp_note = f"({int(tp*100)}%)" if bid in TAIL_PCT_OVERRIDE else ""
        label = f"{name}({bid})"
        lines.append(f"{label:<22} {w:4.1f}  {lb_sum_s}    {lb_p_s}      {flag}{tp_note}")

    lines += [
        "",
        "===== 怎麼看 =====",
        "",
        "1) 溫度百分位（0~100%）：把8家分點的「自我異常重賣超程度」加權合成一個",
        "   分數，再對最近120個交易日同一分數排名。50%＝跟平常沒兩樣；",
        "   🟢<50 正常 ／ 🟡50-70 微溫 ／ 🟠70-90 偏熱 ／ 🔴≥90 高熱。",
        "2) 共識家數（0~8）：這8家裡有幾家「當日自己前5日累計賣超」創自己歷史",
        "   後10%的異常重值（統一仁愛用5%，見明細表註記，2026-07-23驗證：",
        "   收緊到5%對它命中率沒影響、但誤報率減半，其餘7家維持10%）。家數",
        "   越多（尤其≥5/8），代表賣壓是廣泛性、多家機構同時出現，比只有",
        "   1-2家單獨異常時可信度更高。",
        "3) 看趨勢比看單日更有意義：連續多日維持在70%以上，比單日衝到90%又",
        "   隔天掉回50%更值得留意；後者常是單一分點單日雜訊。",
        "4) 「反彈延續訊號」提示：亮紅/橙燈如果剛好在前一次大跌事件10個交易日",
        "   內，很可能只是上次賣壓的殘留讀數，不是新的獨立警訊（見上方提示）。",
        "",
        "===== 已知限制 =====",
        "",
        "- 這是「風險升溫提醒」，不是「精準預警」——研究驗證的判別力約84.5%",
        "  （50%=亂猜），紅燈時仍有相當比例是誤報或前次大跌的延續訊號。",
        f"- {WEIGHT_SOURCE_NOTE}",
        "- 溫度百分位計算：bounded-rolling常態日基準池（最近120個交易日）＋",
        "  排除任一大跌/大漲事件前後5個交易日內的日子，與研究驗證84.5%同一套",
        "  方法；自我百分位另用500個交易日（≈2年）長窗計算，避免tail-10%定義",
        "  被稀釋（2026-07-23修正：舊版用短窗曾把7/17大跌日低估為76%/3家）。",
        "- 非可下單訊號；不作為單獨買賣依據。",
        "",
    ]
    report_text = "\n".join(lines) + "\n"
    stamped_path = OUT / f"{stamp}_crash_thermometer.md"
    stable_path = OUT / "crash_thermometer.md"
    stamped_path.write_text(report_text, encoding="utf-8")
    stable_path.write_text(report_text, encoding="utf-8")
    print(f"wrote {stamped_path}", flush=True)
    print(f"wrote {stable_path}", flush=True)

    print(
        f"pctrank={pctrank_s} band={band} consensus={today_consensus}/8 n_normal_days={n_normal}",
        flush=True,
    )

    if args.notify:
        sys.path.insert(0, str(ROOT / "src"))
        from project_dotenv import load_project_dotenv
        from notify_email import send_alert

        load_project_dotenv()
        subject = f"[股票研究] 大跌溫度計 {asof} · {band} ({pctrank_s})"
        send_alert(subject, report_text)
        print("email sent", flush=True)


if __name__ == "__main__":
    main()
