#!/usr/bin/env python3
"""F-BRANCH-CONSISTENCY（分點跨股一致性評分）。

問題意識：`whale_buy_concentration_branch_track_record.csv` 裡的候選分點（例如
980T 元大自營）證據集中在少數幾檔股票的「集中度事件」（Top1+2 占比≥80% 等嚴格
門檻）。這無法回答：這個分點的優勢是「選股能力本身」，還是「只是剛好在那幾天
集中度特別高、可能只是雜訊子集」？

方法：對每個候選分點（n_campaigns≥2），在 2024-07-01 起的全樣本裡，只要求它是
「當日該股 Top1 買方分點」（不套用 Top1+2≥80%、Top3<10%、出手分點數≥15 等集中
度門檻），且金額≥500萬（避免雜訊），就算一筆觀察。同一 (stock_id, trader_id) 在
≤10 個交易日內的連續 Top1 日合併成一個「行情」，只取首日進場（跟
study_whale_buy_concentration_forward_return.py 的合併邏輯一致，避免偽獨立樣本）。
算 H9 excess return（vs IX0001），跟「集中度事件子集」（即
whale_buy_concentration_campaigns.csv 裡屬於該分點的行情）比較。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/study_whale_branch_consistency.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from stock_db import DEFAULT_DB_PATH, connect

_TZ = ZoneInfo("Asia/Taipei")

DATE_START = "2024-07-01"  # stock_broker_branch_daily 資料涵蓋率限制（見 memory/CLAUDE.md）
DATE_END = "2026-07-22"  # branch daily 資料實際最後一天
TOP1_AMT_FLOOR = 5_000_000  # 500萬，僅用來濾掉雜訊，不是集中度門檻
CAMPAIGN_GAP_TRADING_DAYS = 10  # 跟 study_whale_buy_concentration_forward_return.py 一致
MIN_N_CAMPAIGNS = 2  # 候選分點篩選門檻（跟 track record 一致）
HORIZON = 9  # 只做 H9（比照專案常用 H9 持有慣例，資料量大不做全部 horizon）

OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
TRACK_RECORD_CSV = OUT_DIR / "whale_buy_concentration_branch_track_record.csv"
CONCENTRATION_CAMPAIGNS_CSV = OUT_DIR / "whale_buy_concentration_campaigns.csv"
MEGA_BLACKLIST_PATH = OUT_DIR / "ab58_xMega_copytrade/mega_blacklist_v1.json"

OUT_ALLTOP1_CAMPAIGNS_CSV = OUT_DIR / "branch_consistency_alltop1_campaigns.csv"
OUT_COMPARISON_CSV = OUT_DIR / "branch_consistency_comparison.csv"
OUT_REPORT_MD = OUT_DIR / "whale_branch_consistency.md"


def load_mega_blacklist() -> set[str]:
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def load_candidates() -> pd.DataFrame:
    df = pd.read_csv(TRACK_RECORD_CSV, dtype={"top1_trader_id": str})
    cand = df[df["n_campaigns"] >= MIN_N_CAMPAIGNS].copy()
    return cand.sort_values("n_campaigns", ascending=False).reset_index(drop=True)


def load_full_market_frame(conn, mega: set[str]) -> pd.DataFrame:
    """跟 scan_whale_buy_concentration.py 相同的 query，用來算全市場每日 Top1 買方
    分點（沒有套用任何集中度門檻），才能判斷候選分點在「非事件日」是否也常態性地
    是 Top1 買方。"""
    query = """
        SELECT
            b.trade_date,
            b.stock_id,
            b.securities_trader_id,
            b.buy,
            p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id
         AND p.trade_date = b.trade_date
         AND p.source = 'finmind'
        WHERE b.source = 'finmind'
          AND b.buy > 0
          AND b.trade_date >= ?
          AND b.trade_date <= ?
    """
    df = pd.read_sql_query(query, conn, params=(DATE_START, DATE_END))
    df = df[~df["stock_id"].isin(mega)]
    df = df[~df["stock_id"].str.startswith("00")].copy()
    df["buy_amt"] = df["buy"] * df["close"]
    return df


def compute_daily_top1(df: pd.DataFrame) -> pd.DataFrame:
    """向量化算每個 (trade_date, stock_id) 的 Top1 買方分點與金額（不套任何集中度
    門檻，純粹「當日買超金額最大的分點是誰」）。"""
    group_cols = ["trade_date", "stock_id"]
    df = df.copy()
    df["rank"] = df.groupby(group_cols)["buy_amt"].rank(method="first", ascending=False)
    top1 = df[df["rank"] == 1][group_cols + ["securities_trader_id", "buy_amt"]].rename(
        columns={"securities_trader_id": "top1_trader_id", "buy_amt": "top1_amt"}
    )
    return top1


def merge_campaigns(events: pd.DataFrame, trading_dates: list[str]) -> pd.DataFrame:
    """同一 (stock_id, trader_id) 短窗（≤CAMPAIGN_GAP_TRADING_DAYS 交易日）內連續
    Top1 日合併成一個「行情」，只取首日進場。跟
    study_whale_buy_concentration_forward_return.py 的邏輯一致，確保兩種樣本的
    合併規則相同、比較才公平。"""
    idx = {d: i for i, d in enumerate(trading_dates)}
    campaigns: list[dict] = []
    for (stock_id, trader_id), g in events.groupby(["stock_id", "top1_trader_id"]):
        g = g.sort_values("trade_date")
        dates = g["trade_date"].tolist()
        cur_start = dates[0]
        cur_last_idx = idx.get(dates[0])
        n_in_campaign = 1
        for d in dates[1:]:
            di = idx.get(d)
            if cur_last_idx is not None and di is not None and di - cur_last_idx <= CAMPAIGN_GAP_TRADING_DAYS:
                cur_last_idx = di
                n_in_campaign += 1
                continue
            campaigns.append(
                {
                    "stock_id": stock_id,
                    "top1_trader_id": trader_id,
                    "entry_date": cur_start,
                    "n_events_in_campaign": n_in_campaign,
                }
            )
            cur_start = d
            cur_last_idx = di
            n_in_campaign = 1
        campaigns.append(
            {
                "stock_id": stock_id,
                "top1_trader_id": trader_id,
                "entry_date": cur_start,
                "n_events_in_campaign": n_in_campaign,
            }
        )
    return pd.DataFrame(campaigns).sort_values("entry_date").reset_index(drop=True)


def load_close_panel(conn, stock_ids: list[str]) -> pd.DataFrame:
    if not stock_ids:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT trade_date, stock_id, close
        FROM stock_daily_bars
        WHERE source = 'finmind' AND stock_id IN ({placeholders})
        ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=stock_ids)
    return df.pivot(index="trade_date", columns="stock_id", values="close")


def forward_returns_h9(campaigns: pd.DataFrame, close: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    if campaigns.empty:
        return campaigns.assign(**{f"excess_h{HORIZON}": pd.Series(dtype=float)})
    dates = close.index.tolist()
    idx = {d: i for i, d in enumerate(dates)}
    bench = bench.reindex(dates)
    rows = []
    for _, c in campaigns.iterrows():
        entry = c["entry_date"]
        if entry not in idx or c["stock_id"] not in close.columns:
            continue
        i0 = idx[entry]
        j = i0 + HORIZON
        row = dict(c)
        if j >= len(dates):
            row[f"excess_h{HORIZON}"] = np.nan
            rows.append(row)
            continue
        s0 = close[c["stock_id"]].iloc[i0]
        s1 = close[c["stock_id"]].iloc[j]
        b0 = bench.iloc[i0]
        b1 = bench.iloc[j]
        if pd.isna(s0) or pd.isna(s1) or pd.isna(b0) or pd.isna(b1) or s0 <= 0 or b0 <= 0:
            row[f"excess_h{HORIZON}"] = np.nan
        else:
            stock_ret = s1 / s0 - 1.0
            bench_ret = b1 / b0 - 1.0
            row[f"excess_h{HORIZON}"] = stock_ret - bench_ret
        rows.append(row)
    return pd.DataFrame(rows)


def summarize(vals: pd.Series) -> dict:
    vals = vals.dropna()
    n = len(vals)
    if n == 0:
        return {"n": 0, "mean_pct": None, "win_rate_pct": None, "sharpe": None}
    mean = float(vals.mean()) * 100
    win = float((vals > 0).mean()) * 100
    sharpe = float(vals.mean() / vals.std()) if vals.std() > 0 else None
    return {
        "n": n,
        "mean_pct": round(mean, 3),
        "win_rate_pct": round(win, 1),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top1-amt-floor", type=float, default=TOP1_AMT_FLOOR)
    args = ap.parse_args()

    candidates = load_candidates()
    candidate_ids = candidates["top1_trader_id"].tolist()
    print(f"[INFO] 候選分點（n_campaigns>=2）：{len(candidate_ids)} 個 -> {candidate_ids}")

    mega = load_mega_blacklist()
    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")
    trading_dates = sorted(bench.index.astype(str).unique().tolist())

    print(f"[INFO] 載入全市場 {DATE_START}..{DATE_END} 買超原始資料（不套集中度門檻）...")
    raw = load_full_market_frame(conn, mega)
    print(f"[INFO] raw rows: {len(raw):,}")

    daily_top1 = compute_daily_top1(raw)
    print(f"[INFO] 全市場 (date,stock) Top1 買方紀錄數：{len(daily_top1):,}")

    alltop1 = daily_top1[
        daily_top1["top1_trader_id"].isin(candidate_ids)
        & (daily_top1["top1_amt"] >= args.top1_amt_floor)
    ].copy()
    print(f"[INFO] 候選分點作為 Top1 買方（金額>={args.top1_amt_floor/1e4:.0f}萬）的原始日數：{len(alltop1):,}")

    alltop1_campaigns = merge_campaigns(alltop1, trading_dates)
    print(f"[INFO] 合併行情後：{len(alltop1_campaigns)} 筆")

    stock_ids = sorted(alltop1_campaigns["stock_id"].unique().tolist()) if not alltop1_campaigns.empty else []
    close = load_close_panel(conn, stock_ids)

    alltop1_result = forward_returns_h9(alltop1_campaigns, close, bench)
    alltop1_result.to_csv(OUT_ALLTOP1_CAMPAIGNS_CSV, index=False)
    print(f"[OK] 全 Top1 買方日行情+forward return 明細寫入 {OUT_ALLTOP1_CAMPAIGNS_CSV}")

    # 集中度事件子集（既有研究的行情，只取屬於候選分點的列）
    conc = pd.read_csv(CONCENTRATION_CAMPAIGNS_CSV, dtype={"stock_id": str, "top1_trader_id": str})
    conc_col = f"excess_h{HORIZON}"

    rows = []
    for trader_id, cand_row in zip(candidate_ids, candidates.itertuples()):
        branch_name = cand_row.branch_name
        known_expert = cand_row.known_expert if isinstance(cand_row.known_expert, str) else ""

        conc_g = conc[conc["top1_trader_id"] == trader_id]
        conc_stats = summarize(conc_g[conc_col])

        all_g = alltop1_result[alltop1_result["top1_trader_id"] == trader_id] if not alltop1_result.empty else pd.DataFrame()
        all_stats = summarize(all_g[f"excess_h{HORIZON}"]) if not all_g.empty else summarize(pd.Series(dtype=float))

        n_stocks_conc = conc_g["stock_id"].nunique()
        n_stocks_all = all_g["stock_id"].nunique() if not all_g.empty else 0

        delta_mean = None
        if conc_stats["mean_pct"] is not None and all_stats["mean_pct"] is not None:
            delta_mean = round(conc_stats["mean_pct"] - all_stats["mean_pct"], 3)

        rows.append(
            {
                "top1_trader_id": trader_id,
                "branch_name": branch_name,
                "known_expert": known_expert,
                "conc_n": conc_stats["n"],
                "conc_n_stocks": n_stocks_conc,
                "conc_h9_mean_pct": conc_stats["mean_pct"],
                "conc_h9_win_rate_pct": conc_stats["win_rate_pct"],
                "conc_h9_sharpe": conc_stats["sharpe"],
                "alltop1_n": all_stats["n"],
                "alltop1_n_stocks": n_stocks_all,
                "alltop1_h9_mean_pct": all_stats["mean_pct"],
                "alltop1_h9_win_rate_pct": all_stats["win_rate_pct"],
                "alltop1_h9_sharpe": all_stats["sharpe"],
                "delta_mean_pct_conc_minus_all": delta_mean,
            }
        )

    comparison = pd.DataFrame(rows).sort_values("conc_h9_mean_pct", ascending=False, na_position="last")
    comparison.to_csv(OUT_COMPARISON_CSV, index=False)
    print(f"[OK] 比較表寫入 {OUT_COMPARISON_CSV}")
    print(comparison.to_string(index=False))

    conn.close()

    render_report(comparison)
    return 0


def render_report(comparison: pd.DataFrame) -> None:
    now = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    lines = [
        "# F-BRANCH-CONSISTENCY · 分點跨股一致性評分",
        "",
        f"產出時間：{now}",
        "",
        "## 研究問題",
        "",
        "`whale_buy_concentration_branch_track_record.csv` 裡的候選分點（例如 980T "
        "元大自營）證據集中在少數幾檔股票的「集中度事件」。這是「分點真的有選股能力」"
        "還是「剛好很會挑某一檔」？方法：把每個候選分點在 2024-07-01 起、**不套用任何"
        "集中度門檻**（Top1+2占比≥80%、Top3占比<10%、出手分點數≥15 全部拿掉），只要求"
        f"它是當日該股 Top1 買方分點且金額≥{TOP1_AMT_FLOOR/1e4:.0f}萬（避免雜訊）的全部交易日"
        "都納入，同 (stock_id, trader_id) ≤10 交易日內連續 Top1 日合併成一個行情（跟既有"
        "研究合併規則一致），算 H9 excess return，跟原本的「集中度事件子集」比較。",
        "",
        "- **集中度事件子集（conc_*）**：來自 `whale_buy_concentration_campaigns.csv`，"
        "即符合嚴格集中度門檻的行情（原始候選分點篩選來源）。",
        "- **全 Top1 買方日（alltop1_*）**：本次新算的、不套用集中度門檻的全樣本。",
        "- delta = conc_h9_mean_pct − alltop1_h9_mean_pct。"
        "**delta 明顯為正 → 集中度篩選本身有增量價值**（事件子集優於分點的一般水準）。"
        "**delta 接近 0 或為負 → 分點本身的選股能力才是重點，集中度篩選沒有額外幫助，"
        "甚至可能只是雜訊子集**。",
        "",
        "## 比較表（依 conc_h9_mean_pct 排序）",
        "",
        "| 分點 | 名稱 | 已知專家 | conc n(檔) | conc H9% | conc 勝率% | conc Sharpe | "
        "alltop1 n(檔) | alltop1 H9% | alltop1 勝率% | alltop1 Sharpe | delta(conc−all) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, r in comparison.iterrows():
        lines.append(
            f"| {r['top1_trader_id']} | {r['branch_name']} | {r['known_expert'] or '—'} | "
            f"{r['conc_n']}({r['conc_n_stocks']}) | {r['conc_h9_mean_pct']} | {r['conc_h9_win_rate_pct']} | "
            f"{r['conc_h9_sharpe']} | "
            f"{r['alltop1_n']}({r['alltop1_n_stocks']}) | {r['alltop1_h9_mean_pct']} | {r['alltop1_h9_win_rate_pct']} | "
            f"{r['alltop1_h9_sharpe']} | {r['delta_mean_pct_conc_minus_all']} |"
        )

    lines += [
        "",
        "## 逐分點解讀（誠實回報，不美化）",
        "",
    ]
    for _, r in comparison.iterrows():
        tid = r["top1_trader_id"]
        name = r["branch_name"]
        all_n = r["alltop1_n"]
        conc_n = r["conc_n"]
        delta = r["delta_mean_pct_conc_minus_all"]
        if all_n < 10:
            verdict = f"全 Top1 樣本仍太小（n={all_n}），不具統計意義，暫無法下結論。"
        elif delta is None:
            verdict = "資料不足以計算 delta。"
        elif delta > 2.0:
            verdict = (
                f"delta=+{delta}%，集中度篩選看起來有明顯增量價值——分點在「一般 Top1 買方日」"
                f"（n={all_n}）表現平庸甚至為負（H9%={r['alltop1_h9_mean_pct']}），"
                f"但在集中度事件子集（n={conc_n}）表現好得多（H9%={r['conc_h9_mean_pct']}）。"
                "換句話說，訊號的價值主要來自「集中度」這個篩選條件，不是這個分點本身特別會選股。"
            )
        elif delta < -2.0:
            verdict = (
                f"delta={delta}%，反而是全樣本（一般 Top1 買方日）表現優於集中度事件子集，"
                "顯示集中度篩選對這個分點沒有幫助，甚至可能篩掉了雜訊反而保留了較弱的子集。"
                f"分點本身的整體 Top1 買方訊號（n={all_n}, H9%={r['alltop1_h9_mean_pct']}, "
                f"勝率={r['alltop1_h9_win_rate_pct']}%）才是更可信的參考。"
            )
        else:
            verdict = (
                f"delta={delta}%，兩個子集表現接近，集中度篩選沒有明顯增量價值。分點整體 Top1 "
                f"買方訊號（n={all_n}, H9%={r['alltop1_h9_mean_pct']}, 勝率={r['alltop1_h9_win_rate_pct']}%）"
                "本身就是判斷這個分點是否值得追蹤的主要依據——"
                + (
                    "而這個水準本身看起來還算穩健。"
                    if (r["alltop1_h9_mean_pct"] or 0) > 0 and (r["alltop1_h9_win_rate_pct"] or 0) >= 50
                    else "而這個水準本身也偏弱或不穩定，整體訊號可信度存疑。"
                )
            )
        lines.append(f"### {tid}（{name}）")
        lines.append("")
        lines.append(verdict)
        lines.append("")

    lines += [
        "## 方法論摘要",
        "",
        f"- 候選分點來源：`whale_buy_concentration_branch_track_record.csv` 的 n_campaigns≥{MIN_N_CAMPAIGNS} 名單",
        f"- 全 Top1 樣本：{DATE_START}~{DATE_END}，`stock_broker_branch_daily`（source=finmind）join "
        "`stock_daily_bars`（source=finmind），排除 mega 黑名單 + ETF（00開頭），"
        "**不套用**任何 Top1+2占比／Top3占比／出手分點數門檻，只要求該分點是當日該股"
        f"買超金額最大者且金額≥{TOP1_AMT_FLOOR/1e4:.0f}萬",
        "- 行情合併：同一 (stock_id, top1_trader_id) 在 ≤10 個交易日內的連續 Top1 日視為"
        "同一波吃貨，僅取首個事件日為進場點，避免偽獨立樣本灌水統計量（跟集中度事件子集"
        "採同一合併規則，確保兩者可比較）",
        "- Excess return：進場日收盤 → T+9 收盤，個股報酬減 IX0001 同期報酬",
        "- 集中度事件子集數據直接沿用 `whale_buy_concentration_campaigns.csv`"
        "（`study_whale_buy_concentration_forward_return.py` 產出），未重算",
        "- 資料涵蓋率限制：`stock_broker_branch_daily` 2024-07-01 前僅追蹤極少數分點，"
        "本研究樣本起點固定為 2024-07-01",
        "- 樣本數警語：alltop1_n（或 conc_n）< 30 時，勝率/均值的抽樣誤差很大，"
        "結論僅供參考方向，不構成統計顯著結論",
    ]

    OUT_REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[OK] 報告寫入 {OUT_REPORT_MD}")


if __name__ == "__main__":
    raise SystemExit(main())
