#!/usr/bin/env python3
"""WMA20 反彈確認濾器 · 單股 standalone generalization test（Research · book-only）。

Question：`run_2327_wma20_bounce_confirm_study.py` 只在 2327 一檔股票上驗證
「盤中 WMA20 反彈確認」(and_persist_and_buffer：連續2根站上 WMA20 且 緩衝幅度>0.3%，
edge-triggered) 優於 naive 單根穿越 baseline（full win_rate 36.8% vs 33.8%；
OOS 39.2% vs 34.2%）。這是不是 2327 特有的雜訊，還是一個能 generalize 的技術面型態？

與 `run_wma20_bounce_branch_follow_generalize.py`（同目錄）不同：那支測的是把這個
濾器「疊加在分點跟單事件上」當 entry-quality gate；本腳本測的是**同一訊號本身**
（不綁定任何分點事件，純吃該股自身價格行為）能否在其他流動性佳的個股上重現
2327 的勝率優勢。

Method（100% 沿用既有 SSOT，不重新發明）：直接 import
`run_2327_wma20_bounce_confirm_study.run_study`（WMA20、buffer=0.3%、
persistence=2根、edge-triggered、09:00–13:30 session、80/20 IS/OOS split），
對每檔股票原封不動跑一次，只換 stock_id。比較 baseline_naive_cross 與
and_persist_and_buffer 兩個 variant 的 win_rate_eod（full 與 OOS）。

樣本：`stock_kbar_5m` 中與 2327 同期（2024-01-02 起、>=590 個交易日覆蓋）的
流動性佳大型權值股子集，涵蓋半導體／電子代工／食品等產業。

Read-only DB. Research only；不寫 config/order.yaml 或 config/strategy.yaml。

用法::

  PYTHONPATH=src .venv/bin/python \
    scripts/research/run_wma20_bounce_standalone_generalize.py --write
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

import run_2327_wma20_bounce_confirm_study as base_study  # noqa: E402

OUT_DIR = ROOT / "reports" / "research" / "wma20_bounce_single_stock_generalize"
SCHEMA = "wma20_bounce_standalone_generalize-v1"

# Liquid large/mid-cap single stocks with long 5m-bar history overlapping the
# 2327 study window (2024-01-02 start, >=590 distinct trade_dates as of
# stock_kbar_5m coverage check on 2026-08-08). Excludes 2327 itself and the
# 0050 ETF (not a single stock). Spans semiconductor / EMS / electronics /
# food-conglomerate to avoid a single-sector bias.
STOCK_UNIVERSE = [
    "2330",  # TSMC
    "2317",  # Hon Hai
    "2454",  # MediaTek
    "2308",  # Delta Electronics
    "2382",  # Quanta Computer
    "2303",  # UMC
    "2357",  # Asus
    "2345",  # Accton
    "1216",  # Uni-President (food, non-tech control)
    "2408",  # Nanya Technology
    "2301",  # Lite-On Technology
    "2344",  # Winbond Electronics
    "2360",  # Chroma ATE
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WMA20 bounce-confirm standalone generalization test (research)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--stocks", nargs="*", default=None, help="override stock universe")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 2

    universe = args.stocks or STOCK_UNIVERSE
    conn = connect(args.db)
    per_stock: list[dict] = []
    errors: list[dict] = []
    try:
        for stock_id in universe:
            print(f"running {stock_id} …", flush=True)
            try:
                payload = base_study.run_study(conn, stock_id, start=None, end=None)
            except SystemExit as e:
                errors.append({"stock_id": stock_id, "error": str(e)})
                continue
            by_id = {v["id"]: v for v in payload["variants"]}
            baseline = by_id["baseline_naive_cross"]
            confirm = by_id["and_persist_and_buffer"]
            full_b = baseline["full"]["win_rate_eod"]
            full_c = confirm["full"]["win_rate_eod"]
            oos_b = baseline["oos"]["win_rate_eod"]
            oos_c = confirm["oos"]["win_rate_eod"]
            row = {
                "stock_id": stock_id,
                "n_trading_days": payload["n_trading_days"],
                "date_start": payload["date_start"],
                "date_end": payload["date_end"],
                "baseline_n_full": baseline["full"]["trigger_count"],
                "confirm_n_full": confirm["full"]["trigger_count"],
                "baseline_win_rate_full": full_b,
                "confirm_win_rate_full": full_c,
                "delta_full_pp": (
                    round(full_c - full_b, 2) if (full_c is not None and full_b is not None) else None
                ),
                "baseline_n_oos": baseline["oos"]["trigger_count"],
                "confirm_n_oos": confirm["oos"]["trigger_count"],
                "baseline_win_rate_oos": oos_b,
                "confirm_win_rate_oos": oos_c,
                "delta_oos_pp": (
                    round(oos_c - oos_b, 2) if (oos_c is not None and oos_b is not None) else None
                ),
                "baseline_mean_ret_eod_full": baseline["full"]["mean_ret_eod"],
                "confirm_mean_ret_eod_full": confirm["full"]["mean_ret_eod"],
            }
            per_stock.append(row)
    finally:
        conn.close()

    full_deltas = [r["delta_full_pp"] for r in per_stock if r["delta_full_pp"] is not None]
    oos_deltas = [r["delta_oos_pp"] for r in per_stock if r["delta_oos_pp"] is not None]

    def _agg(deltas: list[float]) -> dict:
        if not deltas:
            return {"n": 0}
        n_pos = sum(1 for d in deltas if d > 0)
        n_neg = sum(1 for d in deltas if d < 0)
        n_zero = len(deltas) - n_pos - n_neg
        # exact two-sided binomial sign test vs p=0.5 (excludes zeros)
        n_nonzero = n_pos + n_neg
        p_sign = None
        if n_nonzero > 0:
            try:
                from math import comb

                k = min(n_pos, n_neg)
                p_sign = round(
                    sum(comb(n_nonzero, i) for i in range(0, k + 1)) * 2 / (2**n_nonzero), 4
                )
                p_sign = min(p_sign, 1.0)
            except Exception:
                p_sign = None
        # paired t-test (mean delta vs 0), small-n informal
        t_stat = None
        if len(deltas) >= 2 and statistics.pstdev(deltas) > 0:
            mean_d = statistics.mean(deltas)
            sd_d = statistics.stdev(deltas)
            t_stat = round(mean_d / (sd_d / (len(deltas) ** 0.5)), 3)
        return {
            "n": len(deltas),
            "n_positive": n_pos,
            "n_negative": n_neg,
            "n_zero": n_zero,
            "mean_delta_pp": round(statistics.mean(deltas), 3),
            "median_delta_pp": round(statistics.median(deltas), 3),
            "stdev_delta_pp": round(statistics.stdev(deltas), 3) if len(deltas) >= 2 else None,
            "sign_test_p_two_sided": p_sign,
            "t_stat_mean_vs_zero": t_stat,
        }

    aggregate = {"full": _agg(full_deltas), "oos": _agg(oos_deltas)}

    verdict_full = (
        "generalizes (mostly positive)"
        if aggregate["full"].get("n", 0) > 0
        and aggregate["full"]["n_positive"] > aggregate["full"]["n_negative"]
        and aggregate["full"]["mean_delta_pp"] > 0
        else "does_not_generalize / 2327-specific"
    )

    payload = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reference": "2327 full win_rate_eod: naive=33.8%, confirm=36.8% (delta +3.0pp); OOS: naive=34.2%, confirm=39.2% (delta +5.0pp)",
        "universe": universe,
        "signal_definition": (
            "verbatim from run_2327_wma20_bounce_confirm_study.run_study: "
            "WMA20 (5m bars, session-continuous across days), buffer=0.3%, "
            "persistence=2 bars, edge-triggered (False->True), session 09:00-13:30, "
            "OOS = last 20% of trading days"
        ),
        "per_stock": per_stock,
        "errors": errors,
        "aggregate": aggregate,
        "verdict_full": verdict_full,
    }

    # ---- render report ----
    lines = [
        "# WMA20 反彈確認濾器 · 單股 standalone generalization test",
        "",
        f"- 對照組（2327 原始研究）：full win_rate_eod naive=33.8% vs confirm=36.8%"
        "（delta +3.0pp）；OOS naive=34.2% vs confirm=39.2%（delta +5.0pp）",
        f"- 樣本股票數：{len(universe)}（{', '.join(universe)}）",
        f"- 訊號定義：與 2327 study 完全相同（`and_persist_and_buffer` vs "
        "`baseline_naive_cross`，WMA20 / buffer=0.3% / persistence=2根 / edge-triggered）",
        "",
        "## 逐股結果",
        "",
        "| stock_id | 天數 | baseline win% (full) | confirm win% (full) | delta (full, pp) | "
        "baseline win% (OOS) | confirm win% (OOS) | delta (OOS, pp) | n_confirm(full) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in per_stock:
        lines.append(
            f"| {r['stock_id']} | {r['n_trading_days']} | {r['baseline_win_rate_full']} | "
            f"{r['confirm_win_rate_full']} | {r['delta_full_pp']} | {r['baseline_win_rate_oos']} | "
            f"{r['confirm_win_rate_oos']} | {r['delta_oos_pp']} | {r['confirm_n_full']} |"
        )
    lines += [
        "",
        "## Aggregate",
        "",
        f"- Full: n={aggregate['full'].get('n')}, positive={aggregate['full'].get('n_positive')}, "
        f"negative={aggregate['full'].get('n_negative')}, mean_delta={aggregate['full'].get('mean_delta_pp')}pp, "
        f"median_delta={aggregate['full'].get('median_delta_pp')}pp, "
        f"sign_test_p={aggregate['full'].get('sign_test_p_two_sided')}, "
        f"t_stat={aggregate['full'].get('t_stat_mean_vs_zero')}",
        f"- OOS: n={aggregate['oos'].get('n')}, positive={aggregate['oos'].get('n_positive')}, "
        f"negative={aggregate['oos'].get('n_negative')}, mean_delta={aggregate['oos'].get('mean_delta_pp')}pp, "
        f"median_delta={aggregate['oos'].get('median_delta_pp')}pp, "
        f"sign_test_p={aggregate['oos'].get('sign_test_p_two_sided')}, "
        f"t_stat={aggregate['oos'].get('t_stat_mean_vs_zero')}",
        "",
        f"## Verdict: **{verdict_full}**",
        "",
    ]
    if errors:
        lines += ["## Errors", ""] + [f"- {e['stock_id']}: {e['error']}" for e in errors] + [""]
    md = "\n".join(lines)
    print(md)

    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        out = OUT_DIR / f"{stamp}_wma20_bounce_standalone_generalize"
        out.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out.with_suffix(".md").write_text(md + "\n", encoding="utf-8")
        print(f"\nwrote {out.with_suffix('.json')}")
        print(f"wrote {out.with_suffix('.md')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
