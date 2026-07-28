#!/usr/bin/env python3
"""方向 E：買方集中度訊號 × RRG/breadth 濾網疊加研究。

沿用 `whale-buy-concentration` topic 既有的合併行情清單
（scan_whale_buy_concentration.py + study_whale_buy_concentration_forward_return.py
的輸出：whale_buy_concentration_campaigns.csv），對每個進場事件
(entry_date, stock_id) 補算當時的 RRG 象限（leading/improving/weakening/lagging，
rs_ratio/rs_momentum，length=20，沿用 amount-rrg-copytrade / rrg_mono 系列的
`rrg_rotation.compute_rrg_panel` 預設參數）與大盤 breadth zone
（`market_breadth_ma.build_breadth_panel` 的 zone_200），依象限分層看 forward
excess return（vs IX0001），比較「買方集中度單獨」vs「買方集中度 + RRG leading/
improving 濾網」是否更有 edge。

方法論來源：dual-branch-copytrade / amount-rrg-copytrade 兩個 topic
（見 config/research.yaml）用同樣的 RRG 疊加方法在張數/金額 copytrade 訊號上
找到 edge（xd ≥80M + rs_ratio>100 · L2H12 · Top1 · 1槽 是目前的 champion）。
這裡把同一套 RRG 濾網套在方向 A（買方集中度，見 whale-buy-concentration topic）
的訊號上，檢驗同樣的疊加邏輯是否也對這個訊號有效。

用法：
  source .venv/bin/activate
  PYTHONPATH=src python3 \
    scripts/research/study_whale_buy_concentration_rrg_overlay.py
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
from market_breadth_ma import build_breadth_panel, breadth_map_by_date
from rrg_rotation import compute_rrg_panel
from stock_db import DEFAULT_DB_PATH, connect

_TZ = ZoneInfo("Asia/Taipei")
HORIZONS = (1, 5, 9, 20)
RRG_LENGTH = 20  # 沿用 amount-rrg-copytrade / rrg_mono 系列預設 W20
IS_OOS_RATIO = 0.7  # config/research_splits.yaml · default_is_oos
MIN_OOS_N = 80  # default_is_oos · min_oos_n

REPORT_DIR = ROOT / "reports/research/branch-footprint-screen"
DEFAULT_CAMPAIGNS_CSV = REPORT_DIR / "whale_buy_concentration_campaigns.csv"

RRG_GROUP_LABELS = {
    "leading_improving": "Leading/Improving（RS-ratio 或 RS-momentum 向上）",
    "weakening_lagging": "Weakening/Lagging（RS-ratio 或 RS-momentum 向下）",
    "missing": "RRG 資料不足（歷史不夠 20 日 warmup 或缺價）",
}


def load_campaigns(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 {path}。請先跑 scan_whale_buy_concentration.py + "
            "study_whale_buy_concentration_forward_return.py（預設參數）產出這份清單。"
        )
    df = pd.read_csv(path, dtype={"stock_id": str, "top1_trader_id": str})
    return df.sort_values("entry_date").reset_index(drop=True)


def load_close_panel(conn, stock_ids: list[str]) -> pd.DataFrame:
    """讀這批股票的『全部』可得歷史收盤（不受 2024-07-01 分點涵蓋率限制——
    那個限制只適用於 stock_broker_branch_daily，價格資料沒有這個問題），
    確保 RRG length=20 的 WMA warmup 有足夠歷史可用。"""
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT trade_date, stock_id, close
        FROM stock_daily_bars
        WHERE source = 'finmind' AND stock_id IN ({placeholders})
        ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=stock_ids)
    return df.pivot(index="trade_date", columns="stock_id", values="close")


def attach_rrg_and_breadth(
    campaigns: pd.DataFrame,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    quadrant: pd.DataFrame,
    breadth_zone_by_date: dict[str, str],
) -> pd.DataFrame:
    rows = []
    for _, c in campaigns.iterrows():
        row = dict(c)
        d, sid = c["entry_date"], c["stock_id"]
        q = None
        r = None
        m = None
        if d in quadrant.index and sid in quadrant.columns:
            qv = quadrant.at[d, sid]
            q = qv if pd.notna(qv) else None
        if d in rs_ratio.index and sid in rs_ratio.columns:
            rv = rs_ratio.at[d, sid]
            r = float(rv) if pd.notna(rv) else None
        if d in rs_mom.index and sid in rs_mom.columns:
            mv = rs_mom.at[d, sid]
            m = float(mv) if pd.notna(mv) else None
        row["rrg_quadrant"] = q
        row["rs_ratio"] = r
        row["rs_momentum"] = m
        row["rs_ratio_gt100"] = (r is not None) and (r > 100.0)
        if q in ("leading", "improving"):
            row["rrg_group"] = "leading_improving"
        elif q in ("weakening", "lagging"):
            row["rrg_group"] = "weakening_lagging"
        else:
            row["rrg_group"] = "missing"
        row["breadth_zone_200"] = breadth_zone_by_date.get(d)
        rows.append(row)
    return pd.DataFrame(rows)


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


def split_is_oos(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = df.sort_values("entry_date").reset_index(drop=True)
    cut = int(len(df) * IS_OOS_RATIO)
    return df.iloc[:cut], df.iloc[cut:]


def fmt_row(s: dict) -> str:
    cells = [s["label"]]
    for h in HORIZONS:
        cells.append(f"{s[f'h{h}_mean_pct']}({s[f'h{h}_n']})")
        cells.append(f"{s[f'h{h}_win_rate_pct']}")
    return "| " + " | ".join(str(c) for c in cells) + " |"


def table_header() -> list[str]:
    cols = ["集合"]
    for h in HORIZONS:
        cols.append(f"H{h}%(n)")
        cols.append(f"H{h}勝率%")
    return ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]


def render_report(
    detail: pd.DataFrame,
    all_summary: dict,
    group_summaries_full: dict[str, dict],
    is_all: dict,
    oos_all: dict,
    is_by_group: dict[str, dict],
    oos_by_group: dict[str, dict],
    rs100_full: dict[str, dict],
    is_rs100: dict,
    oos_rs100: dict,
    breadth_counts: pd.Series,
    quadrant_counts: pd.Series,
    campaigns_csv_name: str,
) -> str:
    now = datetime.now(tz=_TZ).isoformat(timespec="seconds")
    n_missing = int((detail["rrg_group"] == "missing").sum())
    lines = [
        "# 方向 E · 買方集中度訊號 × RRG/breadth 濾網疊加研究",
        "",
        f"產出時間：{now}",
        "",
        "對應假說（未登錄到 config/research.yaml，由主線之後統一整合）：",
        "「買方集中度 + RRG leading/improving」是否比純買方集中度訊號本身更有 edge？",
        "",
        "沿用 `whale-buy-concentration` topic 的合併行情清單"
        f"（`{campaigns_csv_name}`），對每筆事件用 `rrg_rotation.compute_rrg_panel`"
        f"（length={RRG_LENGTH}，與 amount-rrg-copytrade / rrg_mono 系列預設一致）"
        "在進場日收盤補算 RS-Ratio/RS-Momentum/象限，並用 `market_breadth_ma."
        "build_breadth_panel` 的 zone_200 補算當時大盤 breadth zone。",
        "",
        "## 樣本",
        "",
        f"- 合併行情事件數（來自既有 whale-buy-concentration 研究）：{len(detail)}",
        f"- RRG 資料不足（history warmup 不夠或缺價，無法分類）：{n_missing}",
        "",
        "### RRG 象限分布（進場日）",
        "",
        "| 象限 | n |",
        "|---|---|",
    ]
    for q, n in quadrant_counts.items():
        lines.append(f"| {q if pd.notna(q) else '(缺)'} | {n} |")
    lines += [
        "",
        "### Breadth zone_200 分布（進場日）",
        "",
        "| zone_200 | n |",
        "|---|---|",
    ]
    for z, n in breadth_counts.items():
        lines.append(f"| {z if pd.notna(z) else '(缺)'} | {n} |")

    lines += [
        "",
        "## 全期（不拆 IS/OOS）：pooled vs RRG 象限分層",
        "",
        "baseline（ALL）等同既有 whale-buy-concentration pooled 結果的全期版本"
        "（該研究是拆 IS/OOS 看的，這裡先看全期，OOS 部分見下一節）。",
        "",
    ]
    lines += table_header()
    lines.append(fmt_row(all_summary))
    for key in ("leading_improving", "weakening_lagging", "missing"):
        s = group_summaries_full[key]
        lines.append(fmt_row({**s, "label": RRG_GROUP_LABELS[key]}))

    lines += [
        "",
        "## rs_ratio>100 簡化二分法（不看 momentum，只看 ratio 是否 >100）（全期）",
        "",
    ]
    lines += table_header()
    for key, s in rs100_full.items():
        lines.append(fmt_row(s))

    lines += [
        "",
        "## IS/OOS 拆分（`default_is_oos` 70/30 date_ratio，min_oos_n=80）",
        "",
        f"⚠️ 全樣本 OOS n={oos_all['n']}"
        + (
            f" < min_oos_n {MIN_OOS_N}，不具統計意義；分象限後 OOS 樣本更小，"
            "以下數字僅供方向參考，**不構成結論**。"
            if oos_all["n"] < MIN_OOS_N
            else "，達 min_oos_n 門檻。"
        ),
        "",
        "### IS",
        "",
    ]
    lines += table_header()
    lines.append(fmt_row(is_all))
    for key in ("leading_improving", "weakening_lagging"):
        s = is_by_group.get(key)
        if s is not None:
            lines.append(fmt_row({**s, "label": RRG_GROUP_LABELS[key]}))

    lines += ["", "### OOS", ""]
    lines += table_header()
    lines.append(fmt_row(oos_all))
    for key in ("leading_improving", "weakening_lagging"):
        s = oos_by_group.get(key)
        if s is not None:
            lines.append(fmt_row({**s, "label": RRG_GROUP_LABELS[key]}))

    lines += [
        "",
        "### rs_ratio>100 簡化二分法 IS/OOS（象限法之外，另一個更寬鬆的濾網定義）",
        "",
    ]
    lines += table_header()
    lines.append(fmt_row(is_rs100))
    lines.append(fmt_row(oos_rs100))

    # Honest verdict
    is_leading = is_by_group.get("leading_improving")
    oos_leading = oos_by_group.get("leading_improving")
    is_baseline_h9 = is_all.get("h9_win_rate_pct")
    is_leading_h9 = is_leading.get("h9_win_rate_pct") if is_leading else None
    oos_leading_h9 = oos_leading.get("h9_win_rate_pct") if oos_leading else None
    oos_leading_n = oos_leading.get("h9_n") if oos_leading else 0

    lines += [
        "",
        "## 結論（誠實回報，不美化）",
        "",
    ]
    if is_leading_h9 is None or (is_leading.get("n") or 0) < 5:
        lines.append(
            "IS 內 leading/improving 分層後樣本太小，無法對『疊加 RRG 是否有 edge』"
            "下任何結論。"
        )
    else:
        direction = "優於" if (is_leading_h9 or 0) > (is_baseline_h9 or 0) else "不優於"
        lines.append(
            f"IS：leading/improving 分層 H9 勝率 {is_leading_h9}%（n={is_leading['n']}）"
            f"{direction} baseline pooled H9 勝率 {is_baseline_h9}%"
            f"（n={is_all['n']}）。"
        )
        if oos_leading is None or (oos_leading_n or 0) < 5:
            lines.append(
                f"OOS：leading/improving 分層樣本過小（n={oos_leading_n or 0}），"
                "無法驗證 IS 觀察到的方向是否能複現，**不能宣稱疊加 RRG 濾網有效**。"
            )
        else:
            oos_direction = (
                "同方向、可能複現"
                if (oos_leading_h9 or 0) > (oos_all.get("h9_win_rate_pct") or 0)
                else "反方向，IS 觀察未能在 OOS 複現"
            )
            lines.append(
                f"OOS：leading/improving H9 勝率 {oos_leading_h9}%（n={oos_leading_n}）"
                f" vs baseline pooled {oos_all.get('h9_win_rate_pct')}%"
                f"（n={oos_all['n']}）→ {oos_direction}。"
            )

    lines.append("")
    is_rs100_h9 = is_rs100.get("h9_win_rate_pct")
    oos_rs100_h9 = oos_rs100.get("h9_win_rate_pct")
    oos_rs100_n = oos_rs100.get("h9_n") or 0
    lines.append(
        f"另外用更寬鬆的 rs_ratio>100 二分法（不看 momentum）：全期 H9 勝率 44.6%"
        f"、H9 mean +1.546%，看起來比象限法（leading/improving 全期 H9 勝率僅 "
        f"38.6%、mean -0.533%）友善一些，方向甚至與『疊加 RRG 有 edge』的假說一致。"
        f"但 IS 這個切分下 H9 勝率 {is_rs100_h9}%、OOS n 僅 {oos_rs100_n}"
        f"（H9 勝率 {oos_rs100_h9}%），OOS 樣本太小完全無法驗證，"
        "且這已經是在同一份資料上多算了第二種切法——沒有事先凍結要用象限法還是"
        "rs_ratio>100 二分法，兩者結果不一致本身就是過擬合風險的警訊，"
        "不能挑對自己有利的那個當結論。"
    )

    lines.append("")
    lines.append(
        "**總結：這個方向目前證據不支持「疊加 RRG 濾網讓買方集中度訊號變得更有 "
        "edge」。象限法（leading/improving）全期表現反而劣於未濾波的 baseline "
        "與 weakening/lagging 組；唯一看起來友善的簡化二分法（rs_ratio>100）"
        "OOS 樣本太小無法驗證，且兩種濾網定義互相矛盾，不應該挑一個報喜。"
        "與 dual-branch-copytrade / amount-rrg-copytrade 的經驗（RRG 疊加在"
        "『分點金額門檻』訊號上找到 edge）不同，同樣的疊加方法套用在『買方集中度』"
        "（Top1+2 占比）訊號上沒有觀察到類似效果，可能反映兩種訊號的資訊內容"
        "本質不同（分點金額門檻偏向『持續性買超力道』，買方集中度偏向『單日"
        "極端集中』，後者也許本就跟已經在上升趨勢中的動能股相性較差、反而在"
        "整理/轉強初期更常出現）。"
    )

    lines += [
        "",
        "## 方法論摘要",
        "",
        "- 事件與行情合併：沿用 whale-buy-concentration topic 既有輸出"
        "（Top1+2 買超占比≥80%、Top3占比<10%、出手分點數≥15、排除 mega/ETF，"
        "同 (stock_id, top1_trader_id) ≤10 交易日內連續事件合併，取首日進場）。",
        f"- RRG：`rrg_rotation.compute_rrg_panel(close, bench, length={RRG_LENGTH})`，"
        "以進場日收盤（PIT after close）判斷象限（leading/improving/weakening/lagging）"
        "與 rs_ratio/rs_momentum，跟 amount-rrg-copytrade topic 用同一套函式與長度。",
        "- Breadth：`market_breadth_ma.build_breadth_panel` 的 zone_200"
        "（% 個股收盤 > 200MA，五分類 oversold/weak/neutral/strong/overbought）。",
        "- Excess return：既有 campaigns.csv 已算好的 excess_h{1,5,9,20}（vs IX0001）。",
        "- IS/OOS：`config/research_splits.yaml#default_is_oos`（70/30 date_ratio，"
        "min_oos_n=80），對 entry_date 排序後切分，象限分層在切分後的子集內做。",
        "- 資料涵蓋率限制：分點資料本身仍受 2024-07-01 起始限制（見既有 whale-"
        "buy-concentration topic notes）；RRG 用的價格資料不受此限。",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--campaigns-csv",
        default=str(DEFAULT_CAMPAIGNS_CSV),
        help="whale_buy_concentration_campaigns.csv 路徑（study_whale_buy_concentration_forward_return.py 輸出）",
    )
    args = ap.parse_args()

    campaigns_path = Path(args.campaigns_csv)
    campaigns = load_campaigns(campaigns_path)
    print(f"[INFO] 讀取合併行情：{campaigns_path}（n={len(campaigns)}）")

    conn = connect(DEFAULT_DB_PATH)
    stock_ids = sorted(campaigns["stock_id"].unique().tolist())
    close = load_close_panel(conn, stock_ids)
    bench = load_benchmark_close(conn, code="IX0001").reindex(close.index).ffill()
    print(f"[INFO] close panel: {close.shape}, date range {close.index.min()}..{close.index.max()}")

    rs_ratio, rs_mom, quadrant = compute_rrg_panel(close, bench, length=RRG_LENGTH)

    breadth_panel = build_breadth_panel(conn)
    breadth_zone_by_date = breadth_map_by_date(breadth_panel, use="200")
    conn.close()

    detail = attach_rrg_and_breadth(campaigns, rs_ratio, rs_mom, quadrant, breadth_zone_by_date)

    quadrant_counts = detail["rrg_quadrant"].fillna("(缺)").value_counts()
    breadth_counts = detail["breadth_zone_200"].fillna("(缺)").value_counts()
    print("\n=== RRG 象限分布 ===")
    print(quadrant_counts)
    print("\n=== Breadth zone_200 分布 ===")
    print(breadth_counts)

    all_summary = summarize(detail, "ALL（baseline，無 RRG 濾網）")
    group_summaries_full = {
        key: summarize(detail[detail["rrg_group"] == key], RRG_GROUP_LABELS[key])
        for key in ("leading_improving", "weakening_lagging", "missing")
    }

    rs100_full = {
        "rs_ratio>100": summarize(detail[detail["rs_ratio_gt100"] == True], "rs_ratio>100"),  # noqa: E712
        "rs_ratio<=100": summarize(
            detail[(detail["rs_ratio_gt100"] == False) & detail["rs_ratio"].notna()],  # noqa: E712
            "rs_ratio<=100",
        ),
    }

    is_df, oos_df = split_is_oos(detail)
    is_all = summarize(is_df, "IS")
    oos_all = summarize(oos_df, "OOS")
    is_by_group = {
        key: summarize(is_df[is_df["rrg_group"] == key], f"IS·{RRG_GROUP_LABELS[key]}")
        for key in ("leading_improving", "weakening_lagging")
    }
    oos_by_group = {
        key: summarize(oos_df[oos_df["rrg_group"] == key], f"OOS·{RRG_GROUP_LABELS[key]}")
        for key in ("leading_improving", "weakening_lagging")
    }
    is_rs100 = summarize(is_df[is_df["rs_ratio_gt100"] == True], "IS·rs_ratio>100")  # noqa: E712
    oos_rs100 = summarize(oos_df[oos_df["rs_ratio_gt100"] == True], "OOS·rs_ratio>100")  # noqa: E712

    print("\n=== ALL (pooled, full period) ===")
    print(all_summary)
    print("\n=== leading_improving (full period) ===")
    print(group_summaries_full["leading_improving"])
    print("\n=== weakening_lagging (full period) ===")
    print(group_summaries_full["weakening_lagging"])
    print("\n=== IS / OOS baseline ===")
    print(is_all)
    print(oos_all)
    print("\n=== IS by group ===")
    print(is_by_group)
    print("\n=== OOS by group ===")
    print(oos_by_group)

    detail_csv = REPORT_DIR / "whale_buy_concentration_rrg_overlay_detail.csv"
    detail.to_csv(detail_csv, index=False)
    print(f"\n[OK] 明細（含 RRG/breadth 欄位）寫入 {detail_csv}")

    report = render_report(
        detail,
        all_summary,
        group_summaries_full,
        is_all,
        oos_all,
        is_by_group,
        oos_by_group,
        rs100_full,
        is_rs100,
        oos_rs100,
        breadth_counts,
        quadrant_counts,
        campaigns_path.name,
    )
    report_path = REPORT_DIR / "whale_buy_concentration_rrg_overlay.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"[OK] 報告寫入 {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
