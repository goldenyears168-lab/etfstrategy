#!/usr/bin/env python3
"""從 branch-footprint 擴大結果生成「分點 fade 否決名單」config（日報同日否決濾網）.

只留穩健核心：BH-FDR 顯著、median_r_adj_pct<0、n>=MIN_N（預設 100，濾掉小樣本雜訊）。
產出 config/branch_fade_veto.json（成員 + 門檻 + 快照），像處置池 config 一樣可日報調用。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/build_fade_veto_config.py \
    --run-dir reports/research/branch-footprint-screen/expanded_rerun_20260728
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "config/branch_fade_veto.json"
MIN_N = 100

FOREIGN_KW = ["摩根", "大通", "高盛", "瑞銀", "瑞士信貸", "美林", "花旗", "麥格理", "野村",
              "法巴", "德意志", "匯豐", "大摩", "小摩", "里昂", "法興", "巴克萊", "UBS",
              "大和", "港商", "星洲", "法銀", "巴黎"]


def classify(name: str) -> str:
    if not isinstance(name, str):
        return "本土"
    return "外資" if any(k in name for k in FOREIGN_KW) else "本土"


def side_list(run_dir: Path, side: str, min_n: int) -> list[dict]:
    d = pd.read_csv(run_dir / f"leaderboard_{side}_soft.csv", dtype={"securities_trader_id": str})
    fade = d[(d["bh_significant"] == True) & (d["median_r_adj_pct"] < 0) & (d["n"] >= min_n)]  # noqa: E712
    fade = fade.sort_values("median_r_adj_pct")
    out = []
    for _, r in fade.iterrows():
        out.append({
            "id": r["securities_trader_id"], "name": r["name"], "tag": classify(r["name"]),
            "n": int(r["n"]), "n_stocks": int(r["n_stocks"]),
            "median_r_adj_pct": round(float(r["median_r_adj_pct"]), 3),
            "win_rate_pct": round(float(r["win_rate_pct"]), 1),
            "p_value": float(r["p_value"]),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--min-n", type=int, default=MIN_N)
    args = ap.parse_args()
    run_dir = Path(args.run_dir).resolve()
    man = json.loads((run_dir / "master_manifest.json").read_text("utf-8"))

    buy = side_list(run_dir, "buy", args.min_n)
    sell = side_list(run_dir, "sell", args.min_n)
    ids = sorted({b["id"] for b in buy} | {s["id"] for s in sell})

    cfg = {
        "title": "分點 fade 否決名單（日報同日進場否決濾網）",
        "layer": "research",
        "status": "observe_only",
        "protocol": {
            "usage": "某股當日被名單內分點『主導買方或賣方』時，當日否決該股進場（veto filter）。這是「今天別碰這檔」的濾網，不是買賣方向訊號。",
            "basis": f"branch-footprint 擴大 L1H7 · BH-FDR 顯著負向 · n>={args.min_n}（濾掉小樣本雜訊）",
            "excess": "stock − 1.15 × IX0001 · L1H7（T+1 open → T+7 close · 30bps）",
            "why": "跟這些分點同方向買/賣，事件後個股顯著跑輸大盤；多為外資避險/造市對手盤與大型零售分點的 flow，非方向 alpha，故當否決濾網最穩。",
        },
        "caveat": {
            "regime": "此名單僅適用『通用 universe 進場否決』。名單內 1360（港商麥格理）、9227（凱基城中）等在『處置股主場』反而是正向 seat（見 second_disp_expert_pool_watch.json）——勿跨 regime 套用。",
            "core_vs_tail": "產品級核心 = 大樣本外資席（1440/1470/1480/1650/8440）+ 凱基 9268（n=20588, p=1.1e-57）。n 較小者穩健度較弱，用整體名單即可，勿逐一外推小樣本的極端 median。",
        },
        "snapshot": {
            "source_db": man.get("source_db"),
            "branch_snapshot_mtime": man.get("snapshot_file_mtime"),
            "data_max": man.get("data_max_trade_date"),
            "window": man.get("window"),
            "run_dir": str(run_dir.relative_to(ROOT)),
            "generated": "regenerate via scripts/research/build_fade_veto_config.py",
        },
        "filters": {"bh_significant": True, "median_r_adj_pct_lt": 0, "min_n": args.min_n},
        "counts": {"buy_fade": len(buy), "sell_fade": len(sell), "union_branches": len(ids)},
        "union_ids": ids,
        "buy_fade": buy,
        "sell_fade": sell,
    }
    OUT.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    print(f"[OK] {OUT.relative_to(ROOT)} · buy={len(buy)} sell={len(sell)} union={len(ids)} · data_max={man.get('data_max_trade_date')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
