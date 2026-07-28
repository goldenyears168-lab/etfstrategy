#!/usr/bin/env python3
"""方向 C（分點風格分類）· 把買方集中度候選名單裡的分點分成自營商／外資圈／一般分點，
分開彙總 campaign 層級的 forward excess return，比較各類是否有獨立於個別分點的 edge。

沿用既有兩支腳本的輸出，不重新掃描資料庫：
  scripts/research/scan_whale_buy_concentration.py
  scripts/research/study_whale_buy_concentration_forward_return.py
（預設參數版本，讀取 whale_buy_concentration_campaigns.csv）

用法：
  source .venv/bin/activate && PYTHONPATH=src python3 \
    scripts/research/classify_whale_buy_branch_style.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from stock_db import DEFAULT_DB_PATH, connect

OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
CAMPAIGNS_CSV = OUT_DIR / "whale_buy_concentration_campaigns.csv"
HORIZONS = (1, 5, 9, 20)

FOREIGN_KEYWORDS = [
    "摩根", "大通", "高盛", "瑞銀", "瑞士信貸", "美林", "花旗", "麥格理", "野村",
    "法巴", "德意志", "匯豐", "摩根士丹利", "大摩", "小摩", "里昂", "法興", "巴克萊", "UBS",
    "大和",  # 大和國泰＝日商大和證券與國泰合資（Daiwa-Cathay），外資圈
]

# "大和" 會誤中「元大和平」（元大證券和平分點，非外資），此清單中若出現這類本土
# 分點名稱，須排除，避免關鍵字誤判。目前實際候選名單中未出現，先留守門。
_FOREIGN_KEYWORD_EXCEPTIONS = {"元大和平"}


def classify(name: str) -> str:
    if not isinstance(name, str):
        return "一般分點"
    if name in _FOREIGN_KEYWORD_EXCEPTIONS:
        return "一般分點"
    if "自營" in name:
        return "自營商"
    for kw in FOREIGN_KEYWORDS:
        if kw in name:
            return "外資圈"
    return "一般分點"


def load_branch_names(conn, trader_ids: list[str]) -> dict[str, str]:
    if not trader_ids:
        return {}
    placeholders = ",".join("?" * len(trader_ids))
    q = f"""
        SELECT securities_trader_id, securities_trader
        FROM stock_broker_branch_daily
        WHERE source = 'finmind' AND securities_trader_id IN ({placeholders})
        GROUP BY securities_trader_id
    """
    rows = conn.execute(q, trader_ids).fetchall()
    return {r["securities_trader_id"]: r["securities_trader"] for r in rows}


def main() -> int:
    if not CAMPAIGNS_CSV.exists():
        print(f"[ERROR] 找不到 {CAMPAIGNS_CSV}，請先跑 scan_whale_buy_concentration.py + "
              "study_whale_buy_concentration_forward_return.py（預設參數）")
        return 1

    camp = pd.read_csv(CAMPAIGNS_CSV, dtype={"top1_trader_id": str, "stock_id": str})
    print(f"[INFO] 讀取 campaign 明細：{len(camp)} 筆，{camp['top1_trader_id'].nunique()} 個分點")

    conn = connect(DEFAULT_DB_PATH)
    names = load_branch_names(conn, sorted(camp["top1_trader_id"].unique().tolist()))
    conn.close()

    camp["branch_name"] = camp["top1_trader_id"].map(names)
    camp["style"] = camp["branch_name"].apply(classify)

    mapping = (
        camp[["top1_trader_id", "branch_name", "style"]]
        .drop_duplicates()
        .sort_values(["style", "top1_trader_id"])
    )
    print("\n=== 分點 → 分類對照 ===")
    print(mapping.to_string(index=False))

    rows = []
    for style, g in camp.groupby("style"):
        row = {
            "style": style,
            "n_traders": g["top1_trader_id"].nunique(),
            "n_campaigns": len(g),
        }
        for h in HORIZONS:
            vals = g[f"excess_h{h}"].dropna()
            if len(vals) == 0:
                row[f"h{h}_mean_pct"] = None
                row[f"h{h}_win_pct"] = None
                row[f"h{h}_n"] = 0
                continue
            row[f"h{h}_mean_pct"] = round(float(vals.mean()) * 100, 3)
            row[f"h{h}_win_pct"] = round(float((vals > 0).mean()) * 100, 1)
            row[f"h{h}_n"] = int(len(vals))
        rows.append(row)
    summary = pd.DataFrame(rows)
    print("\n=== 三類彙總（campaign 層級） ===")
    print(summary.to_string(index=False))

    mapping_out = OUT_DIR / "whale_branch_style_mapping.csv"
    summary_out = OUT_DIR / "whale_branch_style_summary.csv"
    mapping.to_csv(mapping_out, index=False)
    summary.to_csv(summary_out, index=False)
    print(f"\n[OK] 對照表寫入 {mapping_out}")
    print(f"[OK] 彙總表寫入 {summary_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
