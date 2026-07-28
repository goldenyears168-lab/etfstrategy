#!/usr/bin/env python3
"""H1-WHALE-BUY-PERSISTENCE · 持續累積偵測（Persistence-based signal）.

方向 D：原始買方集中度研究（scan_whale_buy_concentration.py）只看「觸發當天」的
集中度，沒驗證觸發之後這個分點是不是「真的持續在買」（部位持續加碼）還是「一日
單子」（打完就跑）。本研究把每個觸發事件拆成「持續型」vs「一次性」兩組，分別做
forward-return event study，比較兩組表現有沒有差異。

流程：
  1. 讀既有事件清單 whale_buy_concentration_calibration_events.csv（trade_date,
     stock_id, top1_trader_id, top1_amt, ...）。若不存在則報錯提示先跑
     scan_whale_buy_concentration.py。
  2. 對每個事件，往後看 5 個交易日：同一 (stock_id, top1_trader_id) 在
     T+1..T+5 的 net（買-賣股數）× 當日收盤 加總 = 5 日累計淨買進金額。
     若「5 日累計淨買進金額 > 觸發當天 top1_amt 的 30%」→ 判定「持續型」，否則
     「一次性」。門檻選擇說明見報告內文。
  3. 兩組事件分別套用既有的 campaign 合併邏輯（同 stock/trader ≤10 個交易日內
     連續事件視為一波，只取首日）與 forward return 計算（H1/H5/H9/H20 vs
     IX0001），依 default_is_oos（70/30）拆 IS/OOS。
  4. 輸出兩組的敘述統計比較與明細 CSV。

用法：
  source .venv/bin/activate && PYTHONPATH=src python3 \
    scripts/research/scan_whale_buy_persistence.py
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
MIN_OOS_N = 80

PERSIST_LOOKAHEAD_DAYS = 5
PERSIST_RATIO_THRESHOLD = 0.30  # 5日累計淨買進金額 需 > 觸發當天 top1_amt 的 30%

OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
EVENTS_CSV = OUT_DIR / "whale_buy_concentration_calibration_events.csv"

KNOWN_EXPERT_BRANCHES = {
    "9217": "凱基-松山（跟單松山 songshan-copytrade 現行 live）",
    "9801": "元大-松江（yuanta-songjiang-copytrade research）",
    "9661": "富邦-新店（fubon-xindian branch_signals）",
    "9227": "凱基-城中（kgi-chengzhong branch_signals）",
}


def ensure_events_csv() -> None:
    if not EVENTS_CSV.exists():
        raise SystemExit(
            f"[FATAL] 找不到事件清單 {EVENTS_CSV}\n"
            "請先跑：PYTHONPATH=src python3 scripts/research/scan_whale_buy_concentration.py"
        )


def load_events() -> pd.DataFrame:
    df = pd.read_csv(EVENTS_CSV, dtype={"stock_id": str, "top1_trader_id": str})
    return df.sort_values(["stock_id", "top1_trader_id", "trade_date"]).reset_index(drop=True)


def load_net_panel(conn, stock_ids: list[str], trader_ids: list[str]) -> pd.DataFrame:
    """同一批 (stock_id, securities_trader_id) 的全部 net 明細（含 close 金額換算），
    用於算觸發事件之後的持續買超。一次撈全部再在記憶體裡切窗，避免逐事件查 DB。"""
    stock_ph = ",".join("?" * len(stock_ids))
    trader_ph = ",".join("?" * len(trader_ids))
    q = f"""
        SELECT
            b.trade_date,
            b.stock_id,
            b.securities_trader_id AS trader_id,
            b.net,
            p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id
         AND p.trade_date = b.trade_date
         AND p.source = 'finmind'
        WHERE b.source = 'finmind'
          AND b.stock_id IN ({stock_ph})
          AND b.securities_trader_id IN ({trader_ph})
    """
    df = pd.read_sql_query(q, conn, params=stock_ids + trader_ids)
    df["net_amt"] = df["net"] * df["close"]
    return df


def classify_persistence(
    events: pd.DataFrame, net_panel: pd.DataFrame, trading_dates: list[str]
) -> pd.DataFrame:
    idx = {d: i for i, d in enumerate(trading_dates)}
    net_lookup = net_panel.set_index(["stock_id", "trader_id", "trade_date"])["net_amt"]

    rows = []
    for _, ev in events.iterrows():
        trade_date = ev["trade_date"]
        stock_id = ev["stock_id"]
        trader_id = ev["top1_trader_id"]
        i0 = idx.get(trade_date)
        row = ev.to_dict()
        if i0 is None:
            row["persist_status"] = "no_date_idx"
            row["cum_net_amt_5d"] = np.nan
            row["persist_ratio"] = np.nan
            rows.append(row)
            continue

        future_idx = [i0 + k for k in range(1, PERSIST_LOOKAHEAD_DAYS + 1)]
        if future_idx[-1] >= len(trading_dates):
            # 資料尾端不足 5 個未來交易日，無法可靠分類（近期事件常見）
            row["persist_status"] = "insufficient_future_data"
            row["cum_net_amt_5d"] = np.nan
            row["persist_ratio"] = np.nan
            rows.append(row)
            continue

        future_dates = [trading_dates[i] for i in future_idx]
        cum = 0.0
        for d in future_dates:
            try:
                cum += float(net_lookup.loc[(stock_id, trader_id, d)])
            except KeyError:
                continue  # 該分點當天沒有出手這檔股票，視為 0
        row["cum_net_amt_5d"] = cum
        ratio = cum / ev["top1_amt"] if ev["top1_amt"] else np.nan
        row["persist_ratio"] = ratio
        row["persist_status"] = (
            "persistent" if ratio > PERSIST_RATIO_THRESHOLD else "one_time"
        )
        rows.append(row)

    return pd.DataFrame(rows)


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
        vals = df[col].dropna() if col in df.columns else pd.Series(dtype=float)
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


def group_forward_returns(group_events: pd.DataFrame, trading_dates: list[str], close, bench):
    """把一組（persistent 或 one_time）事件跑 campaign 合併 + forward return，
    回傳 (result_df, is_summary, oos_summary, campaigns_n)。"""
    if group_events.empty:
        empty_summary = {"label": "IS", "n": 0}
        empty_summary_oos = {"label": "OOS", "n": 0}
        for h in HORIZONS:
            empty_summary[f"h{h}_mean_pct"] = None
            empty_summary[f"h{h}_win_rate_pct"] = None
            empty_summary[f"h{h}_sharpe"] = None
            empty_summary[f"h{h}_n"] = 0
            empty_summary_oos[f"h{h}_mean_pct"] = None
            empty_summary_oos[f"h{h}_win_rate_pct"] = None
            empty_summary_oos[f"h{h}_sharpe"] = None
            empty_summary_oos[f"h{h}_n"] = 0
        return pd.DataFrame(), empty_summary, empty_summary_oos, 0

    campaigns = merge_campaigns(group_events[["trade_date", "stock_id", "top1_trader_id"]], trading_dates)
    result = forward_returns_for_campaigns(campaigns, close, bench)
    if result.empty:
        is_df, oos_df = result, result
    else:
        is_df, oos_df = split_is_oos(result)
    is_summary = summarize(is_df, "IS")
    oos_summary = summarize(oos_df, "OOS")
    return result, is_summary, oos_summary, len(campaigns)


def render_summary_table(is_s: dict, oos_s: dict) -> list[str]:
    lines = [
        "| 集合 | H1%(n) | H1勝率% | H5%(n) | H5勝率% | H9%(n) | H9勝率% | H20%(n) | H20勝率% |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in (is_s, oos_s):
        def fmt(h):
            m = s.get(f"h{h}_mean_pct")
            n = s.get(f"h{h}_n", 0)
            w = s.get(f"h{h}_win_rate_pct")
            return f"{m}({n}) | {w}" if m is not None else f"—({n}) | —"

        lines.append(f"| {s['label']} | " + " | ".join(fmt(h) for h in HORIZONS) + " |")
    return lines


def render_report(
    events_classified: pd.DataFrame,
    persist_is: dict,
    persist_oos: dict,
    persist_campaigns_n: int,
    onetime_is: dict,
    onetime_oos: dict,
    onetime_campaigns_n: int,
    n_excluded: int,
) -> str:
    now = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    n_total = len(events_classified)
    n_persist = int((events_classified["persist_status"] == "persistent").sum())
    n_onetime = int((events_classified["persist_status"] == "one_time").sum())

    lines = [
        "# H1-WHALE-BUY-PERSISTENCE · 持續累積偵測 (方向 D)",
        "",
        f"產出時間：{now}",
        "",
        "## 假說",
        "",
        "原始買方集中度事件（`scan_whale_buy_concentration.py`）只驗證了「觸發當天」的",
        "集中度，沒有驗證觸發之後這個分點是不是持續在買（部位持續加碼）還是打完就跑的",
        "一日單子。假說：「持續型」事件的 forward return 應該優於「一次性」事件——如果",
        "屬實，持續性可以當成篩選訊號的額外維度；如果沒差異，至少排除了這個假說。",
        "",
        "## 持續性判定方法",
        "",
        f"- 對每個觸發事件 (trade_date, stock_id, top1_trader_id)，往後看 "
        f"T+1..T+{PERSIST_LOOKAHEAD_DAYS} 個交易日，把該分點在該股票上的 `net`"
        "（買-賣股數）× 當日收盤價 加總，得到「5日累計淨買進金額」。",
        f"- 門檻：5日累計淨買進金額 > 觸發當天 Top1 買超金額（`top1_amt`）的 "
        f"{PERSIST_RATIO_THRESHOLD:.0%}。",
        "  選擇理由：用「觸發當天金額的相對比例」而非絕對金額門檻，是因為觸發事件的",
        "  規模本身跨度很大（1億~數十億都有），相對比例才能公平比較「這波後續加碼力道",
        "  相對起手式有多大」。30% 是一個中性但不算寬鬆的門檻——如果分點只是把當天單子",
        "  尾盤拆單、或隔天小幅獲利了結，5日淨買進通常會遠低於這個比例（甚至轉負）；",
        "  真正在建倉的分點，後續加碼力道通常會顯著超過起手式的三成。這是研究者主觀",
        "  設定的合理值，非最優化搜出來的門檻，讀者可自行用 `--persist-ratio` 調整重跑。",
        f"- 若事件發生在資料尾端、未來不足 {PERSIST_LOOKAHEAD_DAYS} 個交易日，則標記為",
        "  `insufficient_future_data`，不納入持續型/一次性分組（避免用不完整窗口誤判）。",
        "",
        "## 分組統計",
        "",
        f"- 總事件數：{n_total}",
        f"- 持續型（persistent）：{n_persist}",
        f"- 一次性（one_time）：{n_onetime}",
        f"- 排除（資料尾端未來窗口不足，無法分類）：{n_excluded}",
        "",
    ]

    if n_persist < 30 or n_onetime < 30:
        lines.append(
            f"**樣本數警告：拆成兩組後，持續型 n={n_persist}、一次性 n={n_onetime}，"
            "遠低於一般統計顯著性所需的樣本量，以下比較僅供方向性參考，不構成統計"
            "顯著結論。**"
        )
        lines.append("")

    lines += [
        "## 持續型（persistent）forward return（excess vs IX0001）",
        "",
        f"合併行情後（同 stock/trader ≤{CAMPAIGN_GAP_TRADING_DAYS} 交易日內連續事件視為 1 筆）："
        f"{persist_campaigns_n} 筆",
        "",
    ]
    lines += render_summary_table(persist_is, persist_oos)
    lines += [
        "",
        "## 一次性（one_time）forward return（excess vs IX0001）",
        "",
        f"合併行情後：{onetime_campaigns_n} 筆",
        "",
    ]
    lines += render_summary_table(onetime_is, onetime_oos)

    lines += ["", "## 結論（誠實回報，不美化）", ""]

    def h9_win(s):
        return s.get("h9_win_rate_pct")

    p_win = h9_win(persist_is)
    o_win = h9_win(onetime_is)
    p_mean = persist_is.get("h9_mean_pct")
    o_mean = onetime_is.get("h9_mean_pct")

    if p_win is None or o_win is None:
        lines.append(
            "其中一組（或兩組）IS 樣本在 H9 完全沒有可計算的資料（n=0），無法比較，"
            "見上方分組統計是否某組事件數過少。"
        )
    else:
        diff_win = p_win - o_win
        diff_mean = (p_mean or 0) - (o_mean or 0)
        if n_persist < 30 or n_onetime < 30:
            lines.append(
                f"IS H9：持續型勝率 {p_win}% vs 一次性勝率 {o_win}%（差 {diff_win:+.1f}pp），"
                f"持續型均值 {p_mean}% vs 一次性均值 {o_mean}%（差 {diff_mean:+.3f}pp）。"
                "但兩組樣本數都很小，這個差異**不足以下結論**，只能說「方向上」持續型"
                f"{'看起來較優' if diff_win > 0 and diff_mean > 0 else '看起來較弱' if diff_win < 0 and diff_mean < 0 else '結果不一致（勝率與均值方向矛盾），沒有清楚訊號'}"
                "。需要更長資料期間（更多 2024-07 之後的樣本累積）才能驗證。"
            )
        elif diff_win > 5 and diff_mean > 0:
            lines.append(
                f"IS H9：持續型勝率 {p_win}% 明顯優於一次性 {o_win}%（差 {diff_win:+.1f}pp），"
                f"均值也是持續型較高（{p_mean}% vs {o_mean}%）。持續性維度看起來是有意義的"
                "額外篩選條件，值得在原始集中度訊號基礎上疊加使用。"
            )
        else:
            lines.append(
                f"IS H9：持續型勝率 {p_win}% vs 一次性 {o_win}%（差 {diff_win:+.1f}pp），"
                f"均值 {p_mean}% vs {o_mean}%。沒有看到持續型明顯優於一次性的訊號——"
                "至少確認了「持續性」這個維度本身，在目前樣本下不是原始訊號沒有 edge"
                "的解釋，原始問題（全市場 pooled 沒有穩健 edge）看起來不是因為把"
                "「一日單子」和「真的在建倉」混在一起算的緣故。"
            )

    lines += [
        "",
        "## 方法論摘要",
        "",
        "- 事件來源：`whale_buy_concentration_calibration_events.csv`"
        "（Top1+2 買超占比≥80%、金額≥1億、Top3占比<10%、出手分點數≥15、"
        "排除 mega 黑名單與 ETF——沿用 `scan_whale_buy_concentration.py` 預設）",
        f"- 持續性判定：見上方「持續性判定方法」（{PERSIST_LOOKAHEAD_DAYS}日窗口、"
        f"{PERSIST_RATIO_THRESHOLD:.0%} 門檻）",
        "- 行情合併：同一 (stock_id, top1_trader_id) 在 ≤10 個交易日內的連續事件"
        "視為同一波吃貨，僅取首個事件日為進場點，避免偽獨立樣本灌水統計量",
        "- Excess return：進場日收盤 → T+N 收盤，個股報酬減 IX0001 同期報酬",
        "- IS/OOS：`default_is_oos` 70/30 date_ratio（`config/research_splits.yaml`），"
        f"min_oos_n={MIN_OOS_N}——本研究拆兩組後 OOS 樣本數大概率遠低於此標準，"
        "報告內明確標註不具統計意義",
        "- 資料涵蓋率限制：`stock_broker_branch_daily` 僅自 2024-07-01 起有穩定分點",
        "覆蓋，事件與持續性判定皆受此限制",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    global PERSIST_RATIO_THRESHOLD, PERSIST_LOOKAHEAD_DAYS
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist-ratio", type=float, default=PERSIST_RATIO_THRESHOLD)
    ap.add_argument("--lookahead-days", type=int, default=PERSIST_LOOKAHEAD_DAYS)
    args = ap.parse_args()

    PERSIST_RATIO_THRESHOLD = args.persist_ratio
    PERSIST_LOOKAHEAD_DAYS = args.lookahead_days

    ensure_events_csv()
    events = load_events()
    print(f"[INFO] 讀取觸發事件：{len(events)} 筆（{EVENTS_CSV.name}）")

    conn = connect(DEFAULT_DB_PATH)
    bench = load_benchmark_close(conn, code="IX0001")
    trading_dates = sorted(bench.index.astype(str).unique().tolist())

    stock_ids = sorted(events["stock_id"].unique().tolist())
    trader_ids = sorted(events["top1_trader_id"].unique().tolist())
    print(f"[INFO] 涉及 {len(stock_ids)} 檔股票、{len(trader_ids)} 個分點，撈取 net 明細...")
    net_panel = load_net_panel(conn, stock_ids, trader_ids)
    print(f"[INFO] net 明細列數：{len(net_panel):,}")

    classified = classify_persistence(events, net_panel, trading_dates)
    print("\n=== 持續性分類分布 ===")
    print(classified["persist_status"].value_counts())

    classified_out = OUT_DIR / "whale_buy_persistence_events_classified.csv"
    classified.sort_values(["persist_status", "trade_date"]).to_csv(classified_out, index=False)
    print(f"[OK] 分類明細寫入 {classified_out}")

    n_excluded = int(
        classified["persist_status"].isin(["insufficient_future_data", "no_date_idx"]).sum()
    )
    persist_events = classified[classified["persist_status"] == "persistent"].copy()
    onetime_events = classified[classified["persist_status"] == "one_time"].copy()

    all_stock_ids = sorted(classified["stock_id"].unique().tolist())
    close = load_close_panel(conn, all_stock_ids)
    conn.close()

    print("\n[INFO] 持續型（persistent）forward return study...")
    persist_result, persist_is, persist_oos, persist_campaigns_n = group_forward_returns(
        persist_events, trading_dates, close, bench
    )
    print(f"  campaigns={persist_campaigns_n}, IS={persist_is}, OOS={persist_oos}")

    print("\n[INFO] 一次性（one_time）forward return study...")
    onetime_result, onetime_is, onetime_oos, onetime_campaigns_n = group_forward_returns(
        onetime_events, trading_dates, close, bench
    )
    print(f"  campaigns={onetime_campaigns_n}, IS={onetime_is}, OOS={onetime_oos}")

    if not persist_result.empty:
        persist_result.to_csv(OUT_DIR / "whale_buy_persistence_persistent_campaigns.csv", index=False)
    if not onetime_result.empty:
        onetime_result.to_csv(OUT_DIR / "whale_buy_persistence_onetime_campaigns.csv", index=False)
    print(f"[OK] 兩組行情+forward return 明細寫入 {OUT_DIR}/whale_buy_persistence_{{persistent,onetime}}_campaigns.csv")

    report = render_report(
        classified,
        persist_is,
        persist_oos,
        persist_campaigns_n,
        onetime_is,
        onetime_oos,
        onetime_campaigns_n,
        n_excluded,
    )
    report_path = OUT_DIR / "whale_buy_persistence_study.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n[OK] 報告寫入 {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
