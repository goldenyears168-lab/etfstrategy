#!/usr/bin/env python3
"""Direction A · H-WHALE-SELL-FORWARD · sell-side concentration forward-return event study.

跟 study_whale_buy_concentration_forward_return.py 邏輯完全對稱（合併行情、
forward excess return、IS/OOS 拆分、分點層級追蹤紀錄），讀的是
scan_whale_sell_concentration.py 產出的賣方集中度事件清單。

假說（使用者原始舉例）：單一（或兩位）分點主導賣超、其他大戶未同步賣出
（Top3 占比低、出手分點數不少）→ 對手盤（買方）分散、疑似 retail 承接 →
事件後 forward excess return 顯著為負。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/study_whale_sell_concentration_forward_return.py
"""

from __future__ import annotations

import argparse
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
HORIZONS = (1, 5, 9, 20)
CAMPAIGN_GAP_TRADING_DAYS = 10
IS_OOS_RATIO = 0.7  # config/research_splits.yaml · default_is_oos
MIN_OOS_N = 80  # default_is_oos · min_oos_n（樣本量若不足會明確標註）

OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
MIN_N_FOR_CANDIDATE = 2  # 至少出現 2 次才算「有重複證據」，n=1 只是單一事件不算分點track record
PRIMARY_HORIZON = 9  # 排序候選分點用的主要 horizon（比照專案常用 H9/H7 持有慣例）

# 既有「已知專家分點」（跟單松山/元大松江/富邦新店/凱基城中），用來對照新發現的分點是否重疊
KNOWN_EXPERT_BRANCHES = {
    "9217": "凱基-松山（跟單松山 songshan-copytrade 現行 live）",
    "9801": "元大-松江（yuanta-songjiang-copytrade research）",
    "9661": "富邦-新店（fubon-xindian branch_signals）",
    "9227": "凱基-城中（kgi-chengzhong branch_signals）",
}


def load_branch_names(conn, trader_ids: list[str]) -> dict[str, str]:
    if not trader_ids:
        return {}
    placeholders = ",".join("?" * len(trader_ids))
    q = f"""
        SELECT securities_trader_id, securities_trader, MAX(trade_date) as last_seen
        FROM stock_broker_branch_daily
        WHERE securities_trader_id IN ({placeholders})
        GROUP BY securities_trader_id
    """
    rows = conn.execute(q, trader_ids).fetchall()
    return {r["securities_trader_id"]: r["securities_trader"] for r in rows}


def load_events(events_csv: Path) -> pd.DataFrame:
    df = pd.read_csv(events_csv, dtype={"stock_id": str, "top1_trader_id": str})
    return df.sort_values(["stock_id", "top1_trader_id", "trade_date"])


def merge_campaigns(events: pd.DataFrame, trading_dates: list[str]) -> pd.DataFrame:
    """同一 (stock_id, top1_trader_id) 短窗內連續事件 → 一個行情，取首日。"""
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
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT trade_date, stock_id, close
        FROM stock_daily_bars
        WHERE source = 'finmind' AND stock_id IN ({placeholders})
        ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=stock_ids)
    return df.pivot(index="trade_date", columns="stock_id", values="close")


def forward_returns_for_campaigns(
    campaigns: pd.DataFrame, close: pd.DataFrame, bench: pd.Series
) -> pd.DataFrame:
    dates = close.index.tolist()
    idx = {d: i for i, d in enumerate(dates)}
    bench = bench.reindex(dates)
    rows = []
    for _, c in campaigns.iterrows():
        entry = c["entry_date"]
        if entry not in idx or c["stock_id"] not in close.columns:
            continue
        i0 = idx[entry]
        row = dict(c)
        s0 = close[c["stock_id"]].iloc[i0]
        b0 = bench.iloc[i0]
        if pd.isna(s0) or pd.isna(b0) or s0 <= 0 or b0 <= 0:
            continue
        for h in HORIZONS:
            j = i0 + h
            if j >= len(dates):
                row[f"excess_h{h}"] = np.nan
                continue
            s1 = close[c["stock_id"]].iloc[j]
            b1 = bench.iloc[j]
            if pd.isna(s1) or pd.isna(b1):
                row[f"excess_h{h}"] = np.nan
                continue
            stock_ret = s1 / s0 - 1.0
            bench_ret = b1 / b0 - 1.0
            row[f"excess_h{h}"] = stock_ret - bench_ret
        rows.append(row)
    return pd.DataFrame(rows)


def split_is_oos(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.sort_values("entry_date").reset_index(drop=True)
    cut = int(len(df) * IS_OOS_RATIO)
    return df.iloc[:cut], df.iloc[cut:]


def summarize(df: pd.DataFrame, label: str) -> dict:
    out = {"label": label, "n": len(df)}
    for h in HORIZONS:
        col = f"excess_h{h}"
        vals = df[col].dropna()
        if len(vals) == 0:
            out[f"h{h}_mean_pct"] = None
            out[f"h{h}_win_rate_pct"] = None
            out[f"h{h}_sharpe"] = None
            out[f"h{h}_n"] = 0
            continue
        mean = float(vals.mean()) * 100
        win = float((vals > 0).mean()) * 100
        sharpe = float(vals.mean() / vals.std()) if vals.std() > 0 else None
        out[f"h{h}_mean_pct"] = round(mean, 3)
        out[f"h{h}_win_rate_pct"] = round(win, 1)
        out[f"h{h}_sharpe"] = round(sharpe, 3) if sharpe is not None else None
        out[f"h{h}_n"] = int(len(vals))
    return out


def branch_track_record(result: pd.DataFrame, branch_names: dict[str, str]) -> pd.DataFrame:
    """依 top1_trader_id 分組（非彙總全市場），找出哪些分點自己的賣超集中度
    事件有「顯著負向」forward return 代表性（即真的疑似出貨成功）——這是
    「記住哪些分點賣超值得避開/反向操作」的核心輸出，跟全市場 pooled 假說
    是分開的兩件事：全市場沒 edge 不代表每個分點都沒有 edge，可能只是少數
    分點真的有出貨資訊優勢，被其他雜訊分點稀釋掉。

    排序依 H{PRIMARY_HORIZON} 平均 excess return **由小到大（最負的在前）**，
    因為賣方集中度假說預期的訊號方向是負報酬。"""
    rows = []
    for trader_id, g in result.groupby("top1_trader_id"):
        row: dict = {
            "top1_trader_id": trader_id,
            "branch_name": branch_names.get(trader_id, ""),
            "known_expert": KNOWN_EXPERT_BRANCHES.get(trader_id, ""),
            "n_campaigns": len(g),
            "stocks": ",".join(sorted(g["stock_id"].unique().tolist())),
        }
        for h in HORIZONS:
            vals = g[f"excess_h{h}"].dropna()
            if len(vals) == 0:
                row[f"h{h}_mean_pct"] = None
                row[f"h{h}_win_rate_pct"] = None
                row[f"h{h}_n"] = 0
                continue
            row[f"h{h}_mean_pct"] = round(float(vals.mean()) * 100, 3)
            row[f"h{h}_win_rate_pct"] = round(float((vals > 0).mean()) * 100, 1)
            row[f"h{h}_n"] = int(len(vals))
        rows.append(row)
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    sort_col = f"h{PRIMARY_HORIZON}_mean_pct"
    # 由小到大：賣方集中度假說找的是「最負」的分點（疑似出貨得逞）
    return out.sort_values(sort_col, ascending=True, na_position="last").reset_index(drop=True)


def render_branch_section(track_record: pd.DataFrame, branch_csv_name: str) -> list[str]:
    lines = [
        "",
        "## 分點層級追蹤紀錄（非彙總 · 找值得記住的分點）",
        "",
        f"依 Top1 賣方分點（{'/'.join(f'{h}日' for h in HORIZONS)} horizon）分組，"
        f"用 H{PRIMARY_HORIZON} 平均 excess return **由小到大**排序（賣方集中度假說"
        "找的是最負的分點，即疑似出貨得逞）。"
        f"**n_campaigns ≥ {MIN_N_FOR_CANDIDATE} 才算有重複證據的候選**，"
        "n=1 只是單一事件、不構成分點層級的判斷基礎。",
        "",
    ]
    candidates = track_record[track_record["n_campaigns"] >= MIN_N_FOR_CANDIDATE]
    if candidates.empty:
        lines.append(f"目前沒有任何分點累積到 ≥{MIN_N_FOR_CANDIDATE} 次事件，樣本還太小、需要繼續累積資料。")
    else:
        lines.append(
            "| 分點代號 | 分點名稱 | 已知專家分點？ | n | H1% | H5% | H9% | H20% | H9勝率% | 涉及股票 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for _, r in candidates.iterrows():
            lines.append(
                f"| {r['top1_trader_id']} | {r['branch_name']} | {r['known_expert'] or '—'} | "
                f"{r['n_campaigns']} | {r['h1_mean_pct']} | {r['h5_mean_pct']} | "
                f"{r['h9_mean_pct']} | {r['h20_mean_pct']} | {r['h9_win_rate_pct']} | "
                f"{r['stocks']} |"
            )
    n_single = int((track_record["n_campaigns"] == 1).sum())
    lines.append("")
    lines.append(
        f"（另有 {n_single} 個分點只出現 1 次事件，樣本不足以判斷，完整明細見 "
        f"`{branch_csv_name}`）"
    )
    known_ids = set(KNOWN_EXPERT_BRANCHES)
    seen_known = set(track_record["top1_trader_id"]) & known_ids
    lines.append("")
    lines.append(
        f"已知專家分點池（9217/9801/9661/9227）中，這次有出現在賣方集中度事件裡的："
        f"{', '.join(sorted(seen_known)) if seen_known else '無'}。"
    )
    return lines


def render_report(
    raw_n: int,
    campaigns_n: int,
    is_summary: dict,
    oos_summary: dict,
    branch_lines: list[str],
    amt_floor: float,
) -> str:
    now = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    lines = [
        "# Direction A（H-WHALE-SELL-FORWARD）· 分點賣超集中度 forward return 事件研究",
        "",
        f"產出時間：{now}",
        "",
        "topic：`whale-sell-concentration`（本研究產出，未登錄 config/research.yaml——"
        "由主線之後統一整合）",
        "",
        "## 假說",
        "",
        "單一（或兩位）分點主導賣超、其他大戶未同步賣出（Top3 占比低、出手分點數不少）"
        "→ 對手盤（買方）分散、疑似 retail 承接 → 事件後 forward excess return **顯著為負**。",
        "",
        "## 樣本",
        "",
        f"- 金額門檻：Top1+2 賣超 ≥ {amt_floor/1e8:.1f}億",
        f"- 原始事件（單日）：{raw_n}",
        f"- 合併行情後（同分點連續事件視為 1 筆，gap≤{CAMPAIGN_GAP_TRADING_DAYS} 交易日）：{campaigns_n}",
        f"- IS/OOS（全市場 pooled）：`default_is_oos` 70/30 date_ratio · min_oos_n={MIN_OOS_N}",
    ]
    if oos_summary["n"] < MIN_OOS_N:
        lines.append(
            f"- ⚠️ **OOS 樣本數 {oos_summary['n']} < min_oos_n {MIN_OOS_N}**，"
            "未達 split_spec 設計門檻，以下 pooled OOS 結果僅供參考、不構成統計顯著結論。"
        )
    lines += [
        "",
        "## 全市場 pooled 結果（excess return vs IX0001，n 為該 horizon 實際可算的樣本數）",
        "",
    ]
    lines.append("| 集合 | H1%(n) | H1勝率% | H5%(n) | H5勝率% | H9%(n) | H9勝率% | H20%(n) | H20勝率% |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for s in (is_summary, oos_summary):
        lines.append(
            f"| {s['label']} | "
            f"{s['h1_mean_pct']}({s['h1_n']}) | {s['h1_win_rate_pct']} | "
            f"{s['h5_mean_pct']}({s['h5_n']}) | {s['h5_win_rate_pct']} | "
            f"{s['h9_mean_pct']}({s['h9_n']}) | {s['h9_win_rate_pct']} | "
            f"{s['h20_mean_pct']}({s['h20_n']}) | {s['h20_win_rate_pct']} |"
        )

    is_h9_mean = is_summary.get("h9_mean_pct")
    is_h9_win = is_summary.get("h9_win_rate_pct")
    # 賣方假說期望的方向是「負報酬」：mean<0 且 win_rate<50% 才算方向一致
    is_confirmed = is_h9_mean is not None and is_h9_win is not None and is_h9_mean < 0 and is_h9_win < 50.0
    lines += [
        "",
        "## 全市場 pooled 結論（誠實回報，不美化）",
        "",
    ]
    if is_confirmed:
        lines.append(
            f"IS H{PRIMARY_HORIZON} 平均 excess return {is_h9_mean}%、勝率 {is_h9_win}%，"
            "方向與假說一致（負報酬、勝率<50%）。仍需 OOS 樣本補足才能下定論"
            "（見上方 OOS 樣本數警告），且需與下方分點層級結果交叉確認不是被少數分點主導。"
        )
    else:
        lines.append(
            f"**全市場 pooled 版本看不出跟假說方向一致的穩健 edge**（IS H{PRIMARY_HORIZON} "
            f"平均 {is_h9_mean}%、勝率 {is_h9_win}%，未同時滿足「負報酬且勝率<50%」）。"
            "這不代表所有分點都沒有 edge——見下方「分點層級追蹤紀錄」，可能是少數分點"
            "真的有出貨資訊優勢，被其他雜訊分點稀釋掉了。pooled 版本暫不建議往 "
            "Strategy/Order 層推進。"
        )
    lines += branch_lines
    lines += [
        "",
        "## 方法論摘要",
        "",
        "- 事件定義：`scripts/research/scan_whale_sell_concentration.py`"
        f"（Top1+2 賣超占比≥80%、金額≥{amt_floor/1e8:.1f}億、Top3占比<10%、"
        "出手分點數≥15、排除 mega 黑名單與 ETF）",
        "- 行情合併：同一 (stock_id, top1_trader_id) 在 ≤10 個交易日內的連續"
        "事件視為同一波出貨，僅取首個事件日為進場點，避免偽獨立樣本灌水統計量",
        "- Excess return：進場日收盤 → T+N 收盤，個股報酬減 IX0001 同期報酬",
        "- 資料涵蓋率限制：僅涵蓋 2024-07-01 起（見 MEMORY.md / research.yaml notes）",
        "- 對照：買方集中度版本（`whale_buy_concentration_*`）同門檻下 pooled 也沒有穩健 "
        "edge，但分點層級有候選（980T、8440 正向；1480/5850/8890/1360/9227/1560 負向）——"
        "見下方分點層級表是否出現同一批分點、方向是否一致。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amt-floor", type=float, default=1e8, help="僅用於報告文字標註，需與 scan 腳本門檻一致")
    ap.add_argument(
        "--out-tag",
        default="",
        help="需與 scan_whale_sell_concentration.py 的 --out-tag 一致，讀取對應 variant 的事件檔",
    )
    args = ap.parse_args()

    fname = "whale_sell_concentration_calibration_events"
    suffix = f"_{args.out_tag}" if args.out_tag else ""
    events_csv = OUT_DIR / f"{fname}{suffix}.csv"
    report_path = OUT_DIR / f"whale_sell_concentration_study{suffix}.md"
    branch_track_record_csv = OUT_DIR / f"whale_sell_concentration_branch_track_record{suffix}.csv"
    campaigns_out = OUT_DIR / f"whale_sell_concentration_campaigns{suffix}.csv"

    events = load_events(events_csv)
    print(f"[INFO] 讀取事件檔：{events_csv}")
    print(f"[INFO] 原始事件數：{len(events)}")

    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")
    trading_dates = sorted(bench.index.astype(str).unique().tolist())

    campaigns = merge_campaigns(events, trading_dates)
    print(f"[INFO] 合併後行情數：{len(campaigns)}")
    print(campaigns[["stock_id", "top1_trader_id", "entry_date", "n_events_in_campaign"]])

    stock_ids = sorted(campaigns["stock_id"].unique().tolist())
    close = load_close_panel(conn, stock_ids)

    result = forward_returns_for_campaigns(campaigns, close, bench)
    print(f"[INFO] 可計算 forward return 的行情數：{len(result)}")

    branch_names = load_branch_names(conn, sorted(result["top1_trader_id"].unique().tolist()))
    conn.close()

    is_df, oos_df = split_is_oos(result)
    is_summary = summarize(is_df, "IS")
    oos_summary = summarize(oos_df, "OOS")

    print("\n=== IS ===")
    print(is_summary)
    print("\n=== OOS ===")
    print(oos_summary)

    track_record = branch_track_record(result, branch_names)
    track_record.to_csv(branch_track_record_csv, index=False)
    print(f"\n[OK] 分點追蹤紀錄寫入 {branch_track_record_csv}")
    candidates = track_record[track_record["n_campaigns"] >= MIN_N_FOR_CANDIDATE]
    print(f"[INFO] 候選分點數（n≥{MIN_N_FOR_CANDIDATE}）：{len(candidates)}")
    if not candidates.empty:
        print(
            candidates[
                ["top1_trader_id", "branch_name", "n_campaigns", f"h{PRIMARY_HORIZON}_mean_pct", f"h{PRIMARY_HORIZON}_win_rate_pct"]
            ]
        )

    branch_lines = render_branch_section(track_record, branch_track_record_csv.name)
    report = render_report(len(events), len(campaigns), is_summary, oos_summary, branch_lines, args.amt_floor)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(f"\n[OK] 報告寫入 {report_path}")

    result.to_csv(campaigns_out, index=False)
    print(f"[OK] 行情+forward return 明細寫入 {campaigns_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
