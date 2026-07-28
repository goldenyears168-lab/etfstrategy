#!/usr/bin/env python3
"""方向 B（對倒/換手偵測）· forward-return event study.

讀 scan_whale_crossing.py 產出的「買方分點≠賣方分點、雙方金額量級相近」事件
清單，做兩件事：

  (a) 這類「對倒」事件本身後續股價怎麼走——事件沒有天生的買賣方向（跟
      whale-buy-concentration 不同，那邊是「有人在買」，這邊是「有人在轉手」），
      所以這裡算的 forward excess return 只能看「事件後是否傾向漲/跌/變盤」，
      不能直接當成「做多訊號」或「做空訊號」來用。

  (b) 找出重複出現在對倒案例裡的「賣方分點」（以及買方分點、買賣配對），
      這些重複出現的分點很可能是常態性的持股移轉／信託帳戶／庫存調撥，而不是
      真正的看空訊號——如果某分點反覆出現、但事件後 forward return 接近 0
      或方向不一致，就是支持「這是雜訊、不是訊號」的證據；反之如果某分點的
      對倒事件後續走勢一致，才值得記錄成候選訊號來源。

同一 (stock_id, buy_trader_id, sell_trader_id) 短窗內連續事件先合併成一個
「行情」（只取首日進場），避免同一組對倒的連續交易日被當成多筆獨立樣本
灌水統計量（沿用 study_whale_buy_concentration_forward_return.py 的做法）。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/study_whale_crossing_forward_return.py
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
MIN_OOS_N = 80  # default_is_oos · min_oos_n
MIN_EVENTS_FOR_MEANING = 30  # 任務要求：對倒事件至少要 ≥30 筆才有討論意義

OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
MIN_N_FOR_CANDIDATE = 2  # 分點（或配對）至少出現 2 次才算「有重複證據」
PRIMARY_HORIZON = 9

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
    df = pd.read_csv(
        events_csv,
        dtype={"stock_id": str, "buy_trader_id": str, "sell_trader_id": str},
    )
    return df.sort_values(["stock_id", "buy_trader_id", "sell_trader_id", "trade_date"])


def merge_campaigns(events: pd.DataFrame, trading_dates: list[str]) -> pd.DataFrame:
    """同一 (stock_id, buy_trader_id, sell_trader_id) 短窗內連續事件 → 一個行情，取首日。"""
    idx = {d: i for i, d in enumerate(trading_dates)}
    campaigns: list[dict] = []
    key_cols = ["stock_id", "buy_trader_id", "sell_trader_id"]
    for (stock_id, buy_id, sell_id), g in events.groupby(key_cols):
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
                    "buy_trader_id": buy_id,
                    "sell_trader_id": sell_id,
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
                "buy_trader_id": buy_id,
                "sell_trader_id": sell_id,
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
            out[f"h{h}_abs_mean_pct"] = None
            out[f"h{h}_sharpe"] = None
            out[f"h{h}_n"] = 0
            continue
        mean = float(vals.mean()) * 100
        win = float((vals > 0).mean()) * 100
        abs_mean = float(vals.abs().mean()) * 100
        sharpe = float(vals.mean() / vals.std()) if vals.std() > 0 else None
        out[f"h{h}_mean_pct"] = round(mean, 3)
        out[f"h{h}_win_rate_pct"] = round(win, 1)
        out[f"h{h}_abs_mean_pct"] = round(abs_mean, 3)  # 變盤幅度（不看方向）的參考指標
        out[f"h{h}_sharpe"] = round(sharpe, 3) if sharpe is not None else None
        out[f"h{h}_n"] = int(len(vals))
    return out


def branch_track_record(
    result: pd.DataFrame, group_col: str, branch_names: dict[str, str]
) -> pd.DataFrame:
    """依 group_col（buy_trader_id 或 sell_trader_id）分組，看哪些分點重複出現在
    對倒案例裡、且事件後 forward return 是否有一致方向（有方向 → 可能真的是有
    資訊含量的一方；接近 0 或方向不一致 → 較可能是常態性轉倉/信託帳戶等雜訊）。"""
    rows = []
    for trader_id, g in result.groupby(group_col):
        row: dict = {
            group_col: trader_id,
            "branch_name": branch_names.get(trader_id, ""),
            "known_expert": KNOWN_EXPERT_BRANCHES.get(trader_id, ""),
            "n_campaigns": len(g),
            "stocks": ",".join(sorted(g["stock_id"].unique().tolist())),
            "n_counterparties": g[
                "sell_trader_id" if group_col == "buy_trader_id" else "buy_trader_id"
            ].nunique(),
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
    return out.sort_values("n_campaigns", ascending=False).reset_index(drop=True)


def pair_track_record(result: pd.DataFrame, branch_names: dict[str, str]) -> pd.DataFrame:
    """(買方分點, 賣方分點) 配對是否反覆出現——固定 A→B pair 重複出現，比單一
    分點重複出現更接近「兩個固定帳戶間的例行轉手」的證據。"""
    rows = []
    for (buy_id, sell_id), g in result.groupby(["buy_trader_id", "sell_trader_id"]):
        row = {
            "buy_trader_id": buy_id,
            "buy_branch_name": branch_names.get(buy_id, ""),
            "sell_trader_id": sell_id,
            "sell_branch_name": branch_names.get(sell_id, ""),
            "n_campaigns": len(g),
            "stocks": ",".join(sorted(g["stock_id"].unique().tolist())),
        }
        for h in HORIZONS:
            vals = g[f"excess_h{h}"].dropna()
            row[f"h{h}_mean_pct"] = round(float(vals.mean()) * 100, 3) if len(vals) else None
            row[f"h{h}_n"] = int(len(vals))
        rows.append(row)
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("n_campaigns", ascending=False).reset_index(drop=True)


def render_report(
    raw_n: int,
    campaigns_n: int,
    is_summary: dict,
    oos_summary: dict,
    sell_track: pd.DataFrame,
    buy_track: pd.DataFrame,
    pair_track: pd.DataFrame,
    amt_floor: float,
    share_floor: float,
    cross_ratio_floor: float,
) -> str:
    now = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    lines = [
        "# 方向B-WHALE-CROSSING-FORWARD · 對倒/換手事件 forward return 研究",
        "",
        f"產出時間：{now}",
        "",
        "## 樣本與事件定義",
        "",
        f"- 事件定義（`scan_whale_crossing.py`）：Top1 買方分點金額≥{amt_floor/1e8:.1f}億 且占當日"
        f"總買方金額≥{share_floor:.0%}；Top1 賣方分點金額≥{amt_floor/1e8:.1f}億 且占當日總賣方金額"
        f"≥{share_floor:.0%}；買方分點≠賣方分點；min(買,賣)/max(買,賣)≥{cross_ratio_floor:.0%}",
        f"- 原始事件（單日）：{raw_n}",
        f"- 合併行情後（同 (stock_id, buy_trader_id, sell_trader_id) 連續事件視為 1 筆，"
        f"gap≤{CAMPAIGN_GAP_TRADING_DAYS} 交易日）：{campaigns_n}",
    ]
    if raw_n < MIN_EVENTS_FOR_MEANING:
        lines.append(
            f"- **⚠️ 誠實回報：原始事件數 {raw_n} < {MIN_EVENTS_FOR_MEANING}，"
            "樣本量太小，以下所有統計（包含分點追蹤紀錄）都只能當成觀察線索，"
            "不具統計意義，不建議依此下任何交易決策。**"
        )
    else:
        lines.append(f"- 原始事件數 {raw_n} ≥ {MIN_EVENTS_FOR_MEANING}，達到任務要求的最低樣本門檻。")
    if oos_summary["n"] < MIN_OOS_N:
        lines.append(
            f"- ⚠️ OOS 樣本數 {oos_summary['n']} < min_oos_n {MIN_OOS_N}（`default_is_oos` 70/30"
            " date_ratio 標準），pooled OOS 結果僅供參考。"
        )
    lines += [
        "",
        "## (a) 對倒事件本身：後續股價怎麼走（無天生方向，僅供變盤觀察）",
        "",
        "**重要：這類事件沒有天生買賣方向**——不是「有人在買」也不是「有人在賣」，"
        "是「兩個大戶同一天在同一檔股票的相反方向都很集中」。以下 mean/win_rate 是"
        "「事件後個股相對大盤是漲是跌」的敘述統計，不能直接當成做多或做空訊號；"
        "`abs_mean_pct` 是不看方向的變動幅度，用來判斷這類事件是否傾向伴隨較大波動"
        "（變盤訊號的判準）。",
        "",
        "| 集合 | H1%(n) | H1勝率% | H1|幅度|% | H9%(n) | H9勝率% | H9|幅度|% | H20%(n) | H20勝率% | H20|幅度|% |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in (is_summary, oos_summary):
        lines.append(
            f"| {s['label']} | "
            f"{s['h1_mean_pct']}({s['h1_n']}) | {s['h1_win_rate_pct']} | {s['h1_abs_mean_pct']} | "
            f"{s['h9_mean_pct']}({s['h9_n']}) | {s['h9_win_rate_pct']} | {s['h9_abs_mean_pct']} | "
            f"{s['h20_mean_pct']}({s['h20_n']}) | {s['h20_win_rate_pct']} | {s['h20_abs_mean_pct']} |"
        )

    lines += [
        "",
        "## (b) 賣方分點是否重複出現（常態性轉倉/信託帳戶偵測）",
        "",
        "邏輯：如果某賣方分點反覆出現在對倒案例裡，但這些事件後 forward return"
        "接近 0、方向不一致，代表這個分點的「賣超」很可能只是例行的持股移轉/"
        "庫存調撥，不是真正的看空訊號——用這個分點的歷史對倒事件去判斷未來的"
        "賣超訊號時要小心，不能直接當成看空訊號。",
        "",
    ]
    sell_repeat = sell_track[sell_track["n_campaigns"] >= MIN_N_FOR_CANDIDATE]
    if sell_repeat.empty:
        lines.append(f"目前沒有任何賣方分點重複出現 ≥{MIN_N_FOR_CANDIDATE} 次，樣本太小、無法判斷是否有常態性帳戶。")
    else:
        lines.append("| 賣方分點 | 分點名稱 | 已知專家分點？ | n | 對手買方數 | H1% | H9% | H20% | H9勝率% | 涉及股票 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for _, r in sell_repeat.iterrows():
            lines.append(
                f"| {r['sell_trader_id']} | {r['branch_name']} | {r['known_expert'] or '—'} | "
                f"{r['n_campaigns']} | {r['n_counterparties']} | {r['h1_mean_pct']} | "
                f"{r['h9_mean_pct']} | {r['h20_mean_pct']} | {r['h9_win_rate_pct']} | {r['stocks']} |"
            )

    lines += [
        "",
        "## (b') 買方分點是否重複出現",
        "",
    ]
    buy_repeat = buy_track[buy_track["n_campaigns"] >= MIN_N_FOR_CANDIDATE]
    if buy_repeat.empty:
        lines.append(f"目前沒有任何買方分點重複出現 ≥{MIN_N_FOR_CANDIDATE} 次。")
    else:
        lines.append("| 買方分點 | 分點名稱 | 已知專家分點？ | n | 對手賣方數 | H1% | H9% | H20% | H9勝率% | 涉及股票 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for _, r in buy_repeat.iterrows():
            lines.append(
                f"| {r['buy_trader_id']} | {r['branch_name']} | {r['known_expert'] or '—'} | "
                f"{r['n_campaigns']} | {r['n_counterparties']} | {r['h1_mean_pct']} | "
                f"{r['h9_mean_pct']} | {r['h20_mean_pct']} | {r['h9_win_rate_pct']} | {r['stocks']} |"
            )

    lines += [
        "",
        "## (b'') 買賣分點配對是否反覆出現（固定 A→B pair）",
        "",
        "固定 (買方分點, 賣方分點) 配對反覆出現，比單一分點重複出現更接近"
        "「兩個固定帳戶之間的例行轉手」的直接證據。",
        "",
    ]
    pair_repeat = pair_track[pair_track["n_campaigns"] >= MIN_N_FOR_CANDIDATE]
    if pair_repeat.empty:
        lines.append(f"目前沒有任何 (買方,賣方) 配對重複出現 ≥{MIN_N_FOR_CANDIDATE} 次。")
    else:
        lines.append("| 買方分點 | 賣方分點 | n | H1% | H9% | H20% | 涉及股票 |")
        lines.append("|---|---|---|---|---|---|---|")
        for _, r in pair_repeat.iterrows():
            lines.append(
                f"| {r['buy_trader_id']}（{r['buy_branch_name']}） | "
                f"{r['sell_trader_id']}（{r['sell_branch_name']}） | {r['n_campaigns']} | "
                f"{r['h1_mean_pct']} | {r['h9_mean_pct']} | {r['h20_mean_pct']} | {r['stocks']} |"
            )

    lines += [
        "",
        "## 方法論摘要",
        "",
        "- 事件定義：`scripts/research/scan_whale_crossing.py`",
        "- 行情合併：同一 (stock_id, buy_trader_id, sell_trader_id) 在 ≤10 個交易日內的連續"
        "事件視為同一組對倒，僅取首個事件日為進場點，避免偽獨立樣本灌水統計量",
        "- Excess return：進場日收盤 → T+N 收盤，個股報酬減 IX0001 同期報酬；"
        "因事件無天生方向，win_rate 僅描述「相對大盤是漲是跌」，不是策略勝率",
        "- 資料涵蓋率限制：僅涵蓋 2024-07-01 起（見 research.yaml notes）",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amt-floor", type=float, default=1e8, help="僅用於報告文字標註，需與 scan 腳本門檻一致")
    ap.add_argument("--share-floor", type=float, default=0.50, help="僅用於報告文字標註")
    ap.add_argument("--cross-ratio-floor", type=float, default=0.50, help="僅用於報告文字標註")
    ap.add_argument(
        "--out-tag",
        default="",
        help="需與 scan_whale_crossing.py 的 --out-tag 一致，讀取對應 variant 的事件檔",
    )
    args = ap.parse_args()

    fname = "whale_crossing_events"
    suffix = f"_{args.out_tag}" if args.out_tag else ""
    events_csv = OUT_DIR / f"{fname}{suffix}.csv"
    report_path = OUT_DIR / f"whale_crossing_study{suffix}.md"
    sell_track_csv = OUT_DIR / f"whale_crossing_sell_branch_track_record{suffix}.csv"
    buy_track_csv = OUT_DIR / f"whale_crossing_buy_branch_track_record{suffix}.csv"
    pair_track_csv = OUT_DIR / f"whale_crossing_pair_track_record{suffix}.csv"
    campaigns_out = OUT_DIR / f"whale_crossing_campaigns{suffix}.csv"

    events = load_events(events_csv)
    print(f"[INFO] 讀取事件檔：{events_csv}")
    print(f"[INFO] 原始事件數：{len(events)}")
    if len(events) < MIN_EVENTS_FOR_MEANING:
        print(
            f"[WARN] 原始事件數 {len(events)} < {MIN_EVENTS_FOR_MEANING}，"
            "樣本太小，後續統計僅供觀察，不具統計意義。"
        )

    if events.empty:
        print("[INFO] 無事件，無法進行 forward return 分析，結束。")
        return 0

    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")
    trading_dates = sorted(bench.index.astype(str).unique().tolist())

    campaigns = merge_campaigns(events, trading_dates)
    print(f"[INFO] 合併後行情數：{len(campaigns)}")
    print(campaigns[["stock_id", "buy_trader_id", "sell_trader_id", "entry_date", "n_events_in_campaign"]])

    stock_ids = sorted(campaigns["stock_id"].unique().tolist())
    close = load_close_panel(conn, stock_ids)

    result = forward_returns_for_campaigns(campaigns, close, bench)
    print(f"[INFO] 可計算 forward return 的行情數：{len(result)}")

    all_trader_ids = sorted(
        set(result["buy_trader_id"].unique().tolist()) | set(result["sell_trader_id"].unique().tolist())
    )
    branch_names = load_branch_names(conn, all_trader_ids)
    conn.close()

    is_df, oos_df = split_is_oos(result)
    is_summary = summarize(is_df, "IS")
    oos_summary = summarize(oos_df, "OOS")

    print("\n=== IS ===")
    print(is_summary)
    print("\n=== OOS ===")
    print(oos_summary)

    sell_track = branch_track_record(result, "sell_trader_id", branch_names)
    buy_track = branch_track_record(result, "buy_trader_id", branch_names)
    pair_track = pair_track_record(result, branch_names)

    sell_track.to_csv(sell_track_csv, index=False)
    buy_track.to_csv(buy_track_csv, index=False)
    pair_track.to_csv(pair_track_csv, index=False)
    print(f"\n[OK] 賣方分點追蹤紀錄寫入 {sell_track_csv}")
    print(f"[OK] 買方分點追蹤紀錄寫入 {buy_track_csv}")
    print(f"[OK] 買賣配對追蹤紀錄寫入 {pair_track_csv}")

    sell_candidates = sell_track[sell_track["n_campaigns"] >= MIN_N_FOR_CANDIDATE] if not sell_track.empty else sell_track
    print(f"[INFO] 重複出現的賣方分點數（n≥{MIN_N_FOR_CANDIDATE}）：{len(sell_candidates)}")
    if not sell_candidates.empty:
        print(sell_candidates[["sell_trader_id", "branch_name", "n_campaigns", f"h{PRIMARY_HORIZON}_mean_pct", f"h{PRIMARY_HORIZON}_win_rate_pct"]])

    report = render_report(
        len(events),
        len(campaigns),
        is_summary,
        oos_summary,
        sell_track,
        buy_track,
        pair_track,
        args.amt_floor,
        args.share_floor,
        args.cross_ratio_floor,
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    print(f"\n[OK] 報告寫入 {report_path}")

    result.to_csv(campaigns_out, index=False)
    print(f"[OK] 行情+forward return 明細寫入 {campaigns_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
