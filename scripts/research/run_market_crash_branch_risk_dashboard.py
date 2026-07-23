#!/usr/bin/env python3
"""Market crash branch risk dashboard — 觀察名單分點·每日賣超強度儀表板.

Research / monitoring diagnostic. 非正式風控規則 · 非可下單訊號 ·
尚未登錄 `config/research.yaml`。建立在
`run_market_crash_branch_precursor_scan.py` 的結論之上：觀察名單分點由
`select_watched_panel()` 動態從 branch_scores.csv／案例 CSV 算出（見
--panel-mode），而非手刻清單，以 2026-07-17 大跌事件當作每個分點自己的
「賣超強度基準」，逐日追蹤：

  1. 今日（最新）前 5 日累計賣超 ÷ 該分點自己的 7/17 基準 = 強度比 ratio_pct
  2. 觀察名單合計金額 ÷ 合計 7/17 基準 = 綜合風險分數（composite ratio）
  3. 7/17 之後累計淨額（轉買）÷ |基準| = 買回比例 buyback_pct
  4. ratio 門檻回測：用歷史 10 個大跌事件（排除 7/17 本身）驗證各門檻的
     recall（事件命中率）與 false-alarm rate（非大跌前的日子誤報率）
  5. 大跌定義門檻敏感度：-2%~-5% 對事件數的影響（協助確認 -3% 是否合理）

輸出（`reports/research/branch-footprint-screen/`）：
  market_crash_branch_risk_dashboard.csv   （觀察名單今日狀態）
  market_crash_branch_risk_threshold_backtest.csv
  market_crash_branch_risk_dashboard.md
  _market_crash_branch_risk_dashboard_meta.json

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_market_crash_branch_risk_dashboard.py \\
    --db data/replica/stocks.db
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "run_market_crash_branch_precursor_scan",
    Path(__file__).resolve().parent / "run_market_crash_branch_precursor_scan.py",
)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)  # type: ignore[union-attr]

OUT = ROOT / "reports/research/branch-footprint-screen"
DB_PATH = ROOT / "data" / "replica" / "stocks.db"
BRANCH_SCORES_CSV = OUT / "market_crash_precursor_branch_scores.csv"
CASE_CSV = OUT / "market_crash_precursor_20260717_case.csv"

CRASH_THRESHOLD_GRID = [-0.02, -0.025, -0.03, -0.035, -0.04, -0.05]
RATIO_THRESHOLDS_PCT = [20, 30, 50, 80, 100, 150]
CONSENSUS_KS = [1, 2, 3]


def select_watched_panel(
    *,
    branch_scores_csv: Path,
    case_csv: Path,
    min_hit_count: int,
    max_p_value: float,
    panel_mode: str,
) -> dict[str, str]:
    """跨事件顯著分點名單 — 動態算，避免手刻名單漏人／不一致。

    strict_and：hit_count/p 顯著 **且** 在 7/17 案例本身也落在異常重賣超
      （flagged_tail=True）— 原始設計，較嚴格；
    significance_only：跨事件顯著 **且** 7/17 當時方向確實是賣超
      （lb_sum_ntd<0，不要求進自己的極端尾部）——會納入瑞銀等賣超金額大但
      未進自身歷史尾端的分點；仍會排除 7/17 當時其實淨買超的顯著分點
      （這種分點的「7/17 基準」是正值，套進 ratio/buyback 公式方向會反過來，
      不適合放進以賣超為前提的儀表板）。
    """
    scores = pd.read_csv(branch_scores_csv)
    scores["branch_id"] = scores["branch_id"].astype(str)
    sig = scores[(scores["hit_count"] >= min_hit_count) & (scores["p_value_one_sided"] < max_p_value)]

    case = pd.read_csv(case_csv)
    case["branch_id"] = case["branch_id"].astype(str)
    if panel_mode == "significance_only":
        sold_ids = set(case.loc[case["lb_sum_ntd"] < 0, "branch_id"])
        chosen = sig[sig["branch_id"].isin(sold_ids)]
    else:
        flagged_ids = set(case.loc[case["flagged_tail"] == True, "branch_id"])  # noqa: E712
        chosen = sig[sig["branch_id"].isin(flagged_ids)]

    chosen = chosen.sort_values(["hit_count", "hit_rate_pct"], ascending=False)
    return dict(zip(chosen["branch_id"], chosen["branch_name"]))


def compute_branch_tiers(
    branch_scores_csv: Path, *, robust_hit_count: int = 4
) -> dict[str, str]:
    """Tier A/B 分級 — 依 leave-one-episode-out 驗證結果分級（見
    `run_market_crash_precursor_loocv.py`）：hit_count==min_hit_count(3) 的分點是
    「數學上剛好卡在門檻邊界」——leave-one-out 時，只要拿掉牠3次命中裡的任何
    一次，剩下就只有2次，會直接被判定不顯著；也就是這些分點在OOS檢驗裡幾乎
    必然顯示「0命中」，這不代表牠們是假訊號，但代表牠們的顯著性單靠1個
    事件就會被推翻，比 hit_count>=4（拿掉任何一次命中仍有>=3次撐住）的
    分點脆弱得多。Tier A 可獨立視為訊號；Tier B 建議只作為多家一致時的
    輔助佐證，不要單獨依賴。
    """
    scores = pd.read_csv(branch_scores_csv)
    scores["branch_id"] = scores["branch_id"].astype(str)
    tiers = {}
    for _, r in scores.iterrows():
        tiers[r["branch_id"]] = "A" if r["hit_count"] >= robust_hit_count else "B"
    return tiers


def crash_threshold_sensitivity(
    conn, cal: list[str], start: str, end: str, episode_gap_days: int
) -> pd.DataFrame:
    rows = []
    for thr in CRASH_THRESHOLD_GRID:
        eps = m.find_crash_episodes(
            conn,
            cal,
            start=start,
            end=end,
            crash_threshold=thr,
            episode_gap_days=episode_gap_days,
        )
        rows.append(
            {
                "crash_threshold_pct": thr * 100,
                "n_raw_days": len(eps),
                "n_episodes": int(eps["episode_first"].sum()) if not eps.empty else 0,
            }
        )
    return pd.DataFrame(rows)


def dashboard_table(
    scored: pd.DataFrame,
    cal: list[str],
    *,
    case_date: str,
    watched_panel: dict[str, str],
    tiers: dict[str, str] | None = None,
) -> pd.DataFrame:
    latest_date = cal[-1]
    bench = scored[scored["trade_date"] == case_date].set_index("securities_trader_id")["lb_sum_ntd"]
    latest = scored[scored["trade_date"] == latest_date].set_index("securities_trader_id")["lb_sum_ntd"]

    post_mask = scored["trade_date"] > case_date
    post_sum = (
        scored[post_mask].groupby("securities_trader_id")["net_amt"].sum()
    )

    rows = []
    for bid, name in watched_panel.items():
        b = bench.get(bid)
        c = latest.get(bid)
        p = post_sum.get(bid, 0.0)
        ratio_pct = None if b in (None, 0) or pd.isna(b) else (c / b * 100.0 if c is not None else None)
        buyback_pct = None if b in (None, 0) or pd.isna(b) else (p / abs(b) * 100.0)
        rows.append(
            {
                "branch_id": bid,
                "branch_name": name,
                "tier": (tiers or {}).get(bid, ""),
                "benchmark_0717_5d_ntd": None if b is None else float(b),
                "latest_date": latest_date,
                "latest_5d_ntd": None if c is None or pd.isna(c) else float(c),
                "ratio_vs_0717_pct": None if ratio_pct is None or pd.isna(ratio_pct) else float(ratio_pct),
                "post_0717_cum_net_ntd": float(p),
                "buyback_pct": None if buyback_pct is None or pd.isna(buyback_pct) else float(buyback_pct),
            }
        )
    df = pd.DataFrame(rows)
    return df.sort_values("ratio_vs_0717_pct", ascending=False, na_position="last").reset_index(drop=True)


def aggregate_row(df: pd.DataFrame) -> dict:
    b_sum = df["benchmark_0717_5d_ntd"].sum()
    c_sum = df["latest_5d_ntd"].sum()
    p_sum = df["post_0717_cum_net_ntd"].sum()
    return {
        "sum_benchmark_0717_5d_ntd": float(b_sum),
        "sum_latest_5d_ntd": float(c_sum),
        "composite_ratio_pct": float(c_sum / b_sum * 100.0) if b_sum else None,
        "sum_post_0717_cum_net_ntd": float(p_sum),
        "composite_buyback_pct": float(p_sum / abs(b_sum) * 100.0) if b_sum else None,
    }


def threshold_status(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for thr in RATIO_THRESHOLDS_PCT:
        hit = df[df["ratio_vs_0717_pct"].fillna(-1e18) >= thr]
        rows.append(
            {
                "ratio_threshold_pct": thr,
                "n_branches_exceeding": int(len(hit)),
                "n_watched_total": int(len(df)),
                "branches": ", ".join(f"{r.branch_name}({r.branch_id})" for r in hit.itertuples()),
            }
        )
    return pd.DataFrame(rows)


def backtest_ratio_thresholds(
    scored: pd.DataFrame,
    episodes: pd.DataFrame,
    *,
    case_date: str,
    cal: list[str],
    lookback_days: int,
    watched_panel: dict[str, str],
) -> pd.DataFrame:
    """Using each branch's OWN 2026-07-17 benchmark scale, check whether the same
    ratio would have flagged the OTHER historical crash episodes (recall), and how
    often it would have fired on ordinary (non-crash-adjacent) days (false-alarm rate).
    """
    bench = scored[scored["trade_date"] == case_date].set_index("securities_trader_id")["lb_sum_ntd"]
    ids = list(watched_panel.keys())
    pivot = scored.pivot(index="trade_date", columns="securities_trader_id", values="lb_sum_ntd")
    pivot = pivot.reindex(columns=ids)
    ratio = pivot.div(bench, axis=1) * 100.0  # % of own 0717 benchmark, per day per branch

    ep_dates = set(episodes.loc[episodes["episode_first"], "trade_date"].tolist())
    other_ep_dates = sorted(d for d in ep_dates if d != case_date)

    idx_of = {d: i for i, d in enumerate(cal)}
    near_crash_dates: set[str] = set()
    for d in ep_dates:
        i = idx_of.get(d)
        if i is None:
            continue
        for j in range(max(0, i - lookback_days), i + 1):
            near_crash_dates.add(cal[j])
    normal_dates = [d for d in cal if d not in near_crash_dates and d in ratio.index]

    rows = []
    for thr in RATIO_THRESHOLDS_PCT:
        fired = ratio >= thr
        for k in CONSENSUS_KS:
            n_fire_per_day = fired.sum(axis=1)
            recall_hits = sum(
                1 for d in other_ep_dates if d in n_fire_per_day.index and n_fire_per_day.loc[d] >= k
            )
            recall = recall_hits / len(other_ep_dates) if other_ep_dates else None
            fp_days = sum(
                1
                for d in normal_dates
                if d in n_fire_per_day.index and n_fire_per_day.loc[d] >= k
            )
            fpr = fp_days / len(normal_dates) if normal_dates else None
            rows.append(
                {
                    "ratio_threshold_pct": thr,
                    "consensus_k": k,
                    "recall_n_of_10": f"{recall_hits}/{len(other_ep_dates)}",
                    "recall_pct": None if recall is None else round(recall * 100, 1),
                    "false_alarm_rate_pct": None if fpr is None else round(fpr * 100, 1),
                    "n_normal_days": len(normal_dates),
                }
            )
    return pd.DataFrame(rows)


def write_dashboard_md(
    path: Path,
    *,
    protocol: dict,
    dash: pd.DataFrame,
    agg: dict,
    agg_tier_a: dict | None,
    thr_status: pd.DataFrame,
    backtest: pd.DataFrame,
    sensitivity: pd.DataFrame,
) -> None:
    lines = [
        "# 觀察名單分點 · 每日賣超強度儀表板",
        "",
        "Research / monitoring diagnostic · 非正式風控規則 · 非可下單訊號 ·",
        "尚未登錄 `config/research.yaml`。基準事件："
        f"**{protocol['case_date']}**（大盤 {protocol.get('case_ret_pct', 'NA')}%）。",
        "",
        f"## 觀察名單（{len(dash)} 家 · panel_mode={protocol.get('panel_mode', 'strict_and')}）",
        "",
        "Tier 分級見 `market_crash_precursor_loocv.md`（leave-one-episode-out 驗證）："
        "**Tier A**＝跨事件命中≥4次，拿掉任何一次命中仍站得住腳，OOS 較穩健，"
        "可獨立視為訊號；**Tier B**＝剛好命中3次（門檻邊界），拿掉其中任何一次"
        "命中就會被判定不顯著，單獨一家不建議當作可信訊號，需多家一致才採信。",
        "",
        "| 分點 | Tier | 7/17 前5日基準(NT$) | 最新前5日淨額(NT$) | 強度比(%) | 7/17後累計淨額(NT$) | 買回比(%) |",
        "|------|:---:|--------------------:|---------------------:|----------:|-----------------------:|----------:|",
    ]
    for r in dash.itertuples():
        b = "NA" if r.benchmark_0717_5d_ntd is None or pd.isna(r.benchmark_0717_5d_ntd) else f"{r.benchmark_0717_5d_ntd:,.0f}"
        c = "NA" if r.latest_5d_ntd is None or pd.isna(r.latest_5d_ntd) else f"{r.latest_5d_ntd:,.0f}"
        ratio = "NA" if r.ratio_vs_0717_pct is None or pd.isna(r.ratio_vs_0717_pct) else f"{r.ratio_vs_0717_pct:+.1f}"
        p = f"{r.post_0717_cum_net_ntd:,.0f}"
        bb = "NA" if r.buyback_pct is None or pd.isna(r.buyback_pct) else f"{r.buyback_pct:+.1f}"
        lines.append(f"| {r.branch_name} (`{r.branch_id}`) | {r.tier or 'NA'} | {b} | {c} | {ratio} | {p} | {bb} |")

    lines += [
        "",
        f"（最新資料日：{dash['latest_date'].iloc[0] if len(dash) else 'NA'}）",
        "",
        "## 綜合風險分數（合計，全部分點）",
        "",
        f"- 合計 7/17 基準：{agg['sum_benchmark_0717_5d_ntd']:,.0f} NT$",
        f"- 合計最新前5日淨額：{agg['sum_latest_5d_ntd']:,.0f} NT$",
        f"- **綜合強度比：{agg['composite_ratio_pct']:+.1f}%**"
        if agg["composite_ratio_pct"] is not None
        else "- 綜合強度比：NA",
        f"- 合計 7/17 後累計淨額：{agg['sum_post_0717_cum_net_ntd']:,.0f} NT$",
        f"- **綜合買回比：{agg['composite_buyback_pct']:+.1f}%**"
        if agg["composite_buyback_pct"] is not None
        else "- 綜合買回比：NA",
        "",
    ]
    if agg_tier_a is not None:
        lines += [
            "## 綜合風險分數（只算 Tier A，較穩健的子集）",
            "",
            f"- 合計 7/17 基準：{agg_tier_a['sum_benchmark_0717_5d_ntd']:,.0f} NT$",
            f"- 合計最新前5日淨額：{agg_tier_a['sum_latest_5d_ntd']:,.0f} NT$",
            f"- **Tier A 綜合強度比：{agg_tier_a['composite_ratio_pct']:+.1f}%**"
            if agg_tier_a["composite_ratio_pct"] is not None
            else "- Tier A 綜合強度比：NA",
            f"- 合計 7/17 後累計淨額：{agg_tier_a['sum_post_0717_cum_net_ntd']:,.0f} NT$",
            f"- **Tier A 綜合買回比：{agg_tier_a['composite_buyback_pct']:+.1f}%**"
            if agg_tier_a["composite_buyback_pct"] is not None
            else "- Tier A 綜合買回比：NA",
            "",
        ]

    lines += [
        "## 目前有哪些分點已超過 7/17 基準的 X%",
        "",
        "| 門檻% | 超過家數/總家數 | 名單 |",
        "|------:|:---------------:|------|",
    ]
    for r in thr_status.itertuples():
        lines.append(f"| {r.ratio_threshold_pct} | {r.n_branches_exceeding}/{r.n_watched_total} | {r.branches or '（無）'} |")

    lines += [
        "",
        "## 門檻回測（用 10 個歷史大跌事件，排除 7/17 本身）",
        "",
        "以各分點自己的 7/17 基準為刻度，門檻 = 強度比達 X% 才算「觸發」；"
        "consensus_k = 至少 k 家觸發才算「名單發出警訊」。"
        "recall = 10 個歷史大跌事件的前5日視窗內有觸發的比例；"
        "false_alarm_rate = 非大跌前視窗的一般交易日中誤報的比例。",
        "",
        "| 門檻% | consensus_k | recall | recall% | false_alarm% |",
        "|------:|:-----------:|:------:|--------:|--------------:|",
    ]
    for r in backtest.itertuples():
        recall_pct = "NA" if r.recall_pct is None else f"{r.recall_pct:.1f}"
        fpr = "NA" if r.false_alarm_rate_pct is None else f"{r.false_alarm_rate_pct:.1f}"
        lines.append(
            f"| {r.ratio_threshold_pct} | {r.consensus_k} | {r.recall_n_of_10} | {recall_pct} | {fpr} |"
        )

    lines += [
        "",
        "## 大跌定義門檻敏感度（-2%~-5%，供對照）",
        "",
        "| 大跌門檻% | 原始大跌日數 | dedup 後事件數 |",
        "|----------:|-------------:|----------------:|",
    ]
    for r in sensitivity.itertuples():
        lines.append(f"| {r.crash_threshold_pct:.1f} | {r.n_raw_days} | {r.n_episodes} |")

    lines += [
        "",
        "## 限制與注意事項",
        "",
        f"- 觀察名單（{len(dash)} 家）與其 7/17 基準都是**單一事件**校準出來的，"
        "樣本仍小；門檻回測用同一批分點對其他 10 個事件重新驗證，"
        "屬於 in-sample 交叉檢驗（同名單、同基準日、不同事件），"
        "不是獨立 OOS，解讀時要保守。",
        "- 「買回比」只看 net_amt 由負轉正的方向與金額，不代表分點的真實庫存"
        "已回補到崩盤前水位（FinMind 分點資料本身無法還原庫存量）。",
        "- 若要正式排入每日風控，建議：(1) 先用未來 2-3 次真實大跌事件做"
        "純 OOS 驗證，(2) 決定 consensus_k 與門檻% 後再固定成規則，"
        "(3) 資料源改用同步排程更新的 replica（見下方 meta），"
        "而不是每次手動 rsync。",
        "- Tier B 分點目前用 leave-one-episode-out 驗證幾乎都是 0 命中"
        "（見 `market_crash_precursor_loocv.md`），但這主要是「剛好卡在"
        "hit_count=3 門檻邊界」的數學結構性結果，不是牠們必然是假訊號——"
        "累積更多大跌事件後應重新分級。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Watched-panel market crash risk dashboard")
    ap.add_argument("--start", default="2024-07-21")
    ap.add_argument("--end", default=None, help="default: latest available trade_date")
    ap.add_argument("--case-date", default="2026-07-17")
    ap.add_argument("--lookback-days", type=int, default=5)
    ap.add_argument("--episode-gap-days", type=int, default=3)
    ap.add_argument("--crash-threshold", type=float, default=-0.03)
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument(
        "--panel-mode",
        choices=["strict_and", "significance_only"],
        default="significance_only",
        help="significance_only（預設）：只要跨事件顯著即入選（含瑞銀等 7/17 未進"
        "自身尾端但歷史顯著的分點）；"
        "strict_and：跨事件顯著且 7/17 案例本身也進賣超尾端（較嚴格，會排除瑞銀）",
    )
    ap.add_argument("--min-hit-count", type=int, default=3)
    ap.add_argument("--max-p-value", type=float, default=0.10)
    ap.add_argument("--robust-hit-count", type=int, default=4, help="Tier A 門檻，見 compute_branch_tiers()")
    ap.add_argument("--branch-scores-csv", type=Path, default=BRANCH_SCORES_CSV)
    ap.add_argument("--case-csv", type=Path, default=CASE_CSV)
    ap.add_argument(
        "--explicit-branch-ids",
        default=None,
        help="逗號分隔的 branch_id 清單，若指定則覆蓋 --panel-mode 動態選取，"
        "直接用這份手選名單（例如跨兩種獨立驗證都過關的 double-passing 清單）；"
        "名稱仍從 --branch-scores-csv 查表帶出",
    )
    args = ap.parse_args()

    if args.explicit_branch_ids:
        scores_for_names = pd.read_csv(args.branch_scores_csv)
        scores_for_names["branch_id"] = scores_for_names["branch_id"].astype(str)
        name_lookup = dict(zip(scores_for_names["branch_id"], scores_for_names["branch_name"]))
        ids = [s.strip() for s in args.explicit_branch_ids.split(",") if s.strip()]
        watched_panel = {bid: name_lookup.get(bid, "") for bid in ids}
    else:
        watched_panel = select_watched_panel(
            branch_scores_csv=args.branch_scores_csv,
            case_csv=args.case_csv,
            min_hit_count=args.min_hit_count,
            max_p_value=args.max_p_value,
            panel_mode=args.panel_mode,
        )
    tiers = compute_branch_tiers(args.branch_scores_csv, robust_hit_count=args.robust_hit_count)
    panel_label = "explicit" if args.explicit_branch_ids else args.panel_mode
    print(f"watched panel ({panel_label}, n={len(watched_panel)}): {watched_panel}", flush=True)
    print(f"tiers: {tiers}", flush=True)

    OUT.mkdir(parents=True, exist_ok=True)
    conn = m.connect_ro(args.db)

    end = args.end
    if end is None:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM stock_daily_bars WHERE source='finmind' AND stock_id='2330'"
        ).fetchone()
        end = str(row[0])
    print(f"window: {args.start}..{end}", flush=True)

    print("building calendar …", flush=True)
    cal = m.build_calendar(conn, args.start, end)
    print(f"calendar n={len(cal)} {cal[0]}..{cal[-1]}", flush=True)

    print("finding crash episodes …", flush=True)
    episodes = m.find_crash_episodes(
        conn,
        cal,
        start=args.start,
        end=end,
        crash_threshold=args.crash_threshold,
        episode_gap_days=args.episode_gap_days,
    )
    n_ep = int(episodes["episode_first"].sum()) if not episodes.empty else 0
    print(f"crash days={len(episodes)} episodes={n_ep}", flush=True)
    case_row = episodes[episodes["trade_date"] == args.case_date]
    case_ret = float(case_row["mkt_ret_pct"].iloc[0]) if len(case_row) else None

    print("building watched-panel branch-day series …", flush=True)
    panel_ids = list(watched_panel.keys())
    panel = m.build_branch_day_panel(conn, panel_ids, start=args.start, end=end)
    grid = m.build_full_grid(panel, panel_ids, cal)
    scored = m.compute_lookback_percentiles(grid, args.lookback_days)
    print(f"panel rows={len(panel):,} scored rows={len(scored):,}", flush=True)

    print("crash-threshold sensitivity …", flush=True)
    sensitivity = crash_threshold_sensitivity(conn, cal, args.start, end, args.episode_gap_days)
    conn.close()

    print("building dashboard table …", flush=True)
    dash = dashboard_table(
        scored, cal, case_date=args.case_date, watched_panel=watched_panel, tiers=tiers
    )
    agg = aggregate_row(dash)
    tier_a_df = dash[dash["tier"] == "A"]
    agg_tier_a = aggregate_row(tier_a_df) if len(tier_a_df) else None
    thr_status = threshold_status(dash)

    print("backtesting ratio thresholds vs historical episodes …", flush=True)
    backtest = backtest_ratio_thresholds(
        scored,
        episodes,
        case_date=args.case_date,
        cal=cal,
        lookback_days=args.lookback_days,
        watched_panel=watched_panel,
    )

    dash_path = OUT / "market_crash_branch_risk_dashboard.csv"
    dash.to_csv(dash_path, index=False)
    bt_path = OUT / "market_crash_branch_risk_threshold_backtest.csv"
    backtest.to_csv(bt_path, index=False)
    print(f"wrote {dash_path}", flush=True)
    print(f"wrote {bt_path}", flush=True)

    protocol = {
        "start": args.start,
        "end": end,
        "case_date": args.case_date,
        "case_ret_pct": case_ret,
        "lookback_days": args.lookback_days,
        "episode_gap_days": args.episode_gap_days,
        "crash_threshold": args.crash_threshold,
        "panel_mode": panel_label,
        "watched_panel": watched_panel,
        "db": str(args.db),
    }
    md_path = OUT / "market_crash_branch_risk_dashboard.md"
    write_dashboard_md(
        md_path,
        protocol=protocol,
        dash=dash,
        agg=agg,
        agg_tier_a=agg_tier_a,
        thr_status=thr_status,
        backtest=backtest,
        sensitivity=sensitivity,
    )
    print(f"wrote {md_path}", flush=True)

    (OUT / "_market_crash_branch_risk_dashboard_meta.json").write_text(
        json.dumps({"protocol": protocol, "aggregate": agg}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("=== DONE ===", flush=True)


if __name__ == "__main__":
    main()
