#!/usr/bin/env python3
"""WMA20 反彈確認濾器 · 出場 + 交易成本模型（Research · book-only · G2 follow-up）。

Question：`run_2327_wma20_bounce_confirm_study.py` 與
`run_wma20_bounce_standalone_generalize.py`（原始 14 檔含 2327 的 entry-only
研究）明確 caveat「entry-signal quality only（無出場／停損／部位大小／交易
成本）」。本腳本補上三類 pre-registered 出場規則、扣除保守 round-trip 成本，
回答：`and_persist_and_buffer` 對 `baseline_naive_cross` 的優勢，在計入真實
出場與成本後是否仍然存在？

Method（100% 沿用既有 SSOT，只加出場層，不重寫訊號邏輯）：
直接 import `run_2327_wma20_bounce_confirm_study`（下稱 base_study）的
`load_close_bars`／`build_indicators`／`day_end_lookup`／`collect_triggers`，
並直接 import `run_wma20_bounce_standalone_generalize` 的 STOCK_UNIVERSE
（13 檔）＋外加 2327 本尊＝共 14 檔。**不修改**這兩支原始腳本任何一行。

新增的出場規則（皆為進場後、同日盤中出場，不跨日持有——與原始引擎「僅量測
同日 forward return」的設計一致）：

  (a) Fixed N-bar holding period：N=3/6/12 根（原本 evaluate_trigger 已算過
      ret_h3/h6/h12，但原引擎「若 N 根超出當日收盤則記 None（丟棄）」；本腳本
      改成「若 N 根超出當日剩餘根數，強制在當日收盤平倉」——這才是可執行的
      真實出場邏輯（收盤前一定要平倉，呼應本 repo `dayflip-short` 等 intraday
      sleeve 的 force_close 慣例），而非丟棄樣本。
  (b) Trailing stop @ 1x/1.5x/2x ATR14（5m bar True Range 14 根滾動均值，
      跨日連續，與 WMA20 的「連續序列」慣例一致）。初始 high-water mark =
      進場價（不用進場當根的 high，避免用到訊號當根尚未確定的資訊）；停損
      以「該根 low 是否觸及停損位」判定，成交價＝停損位（無滑價假設，保守
      但非最保守——真實滑價會更差）；全程未觸發則收盤強制平倉。
  (c) EOD-only exit：等同原引擎的 ret_eod（進場後同日收盤出場），僅重新標記
      以便三規則並列比較。

成本模型：flat round-trip cost = 30bps（0.30%，本 repo 標準假設——見
`config/research.yaml` 中 15+ 處「T+1開盤進場/H7收盤出場/30bps成本」的
一致慣例；比 `dayflip-short`（個股期貨隔日沖）prod notes 中「滑價未實測
估計13~76bps」更保守，取 range 上緣 0.30% 而非中位數，避免高估本濾器的
存活率）。淨報酬 = 毛報酬(%) − 0.30 個百分點。

Read-only DB. Research only；不寫 config/order.yaml 或 config/strategy.yaml；
不修改 run_2327_wma20_bounce_confirm_study.py 或
run_wma20_bounce_standalone_generalize.py。

用法::

  PYTHONPATH=src .venv/bin/python \
    scripts/research/wma20_bounce_exit_cost_model.py --write
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

import run_2327_wma20_bounce_confirm_study as base_study  # noqa: E402
import run_wma20_bounce_standalone_generalize as generalize_study  # noqa: E402

OUT_DIR = ROOT / "reports" / "research" / "wma20_bounce_exit_cost_model"
SCHEMA = "wma20_bounce_exit_cost_model-v1"

UNIVERSE = list(generalize_study.STOCK_UNIVERSE) + [base_study.DEFAULT_STOCK_ID]  # 13 + 2327 = 14

VARIANTS_TO_TEST = ("baseline_naive_cross", "and_persist_and_buffer")

ATR_LENGTH = 14
N_BAR_HOLDS = (3, 6, 12)
ATR_STOP_MULTS = (1.0, 1.5, 2.0)
ROUND_TRIP_COST_PCT = 0.30  # percentage points, i.e. 30bps flat round trip (repo standard)

RULE_ORDER = (
    "nbar_h3",
    "nbar_h6",
    "nbar_h12",
    "atr_stop_1.0x",
    "atr_stop_1.5x",
    "atr_stop_2.0x",
    "eod_only",
)


def load_bars_with_hl(conn, stock_id: str) -> pd.DataFrame:
    """同 base_study.load_close_bars，但多帶 high/low 供 ATR 用（獨立實作，
    不改動原始腳本）。"""
    q = (
        "SELECT trade_date, substr(minute,1,5) AS minute, high, low, close "
        "FROM stock_kbar_5m WHERE stock_id=? "
        "ORDER BY trade_date, minute"
    )
    df = pd.read_sql(q, conn, params=[stock_id])
    df = df[
        (df["minute"] >= base_study.SESSION_MINUTE_LO)
        & (df["minute"] <= base_study.SESSION_MINUTE_HI)
    ]
    df = df.drop_duplicates(["trade_date", "minute"], keep="last")
    df = df.sort_values(["trade_date", "minute"]).reset_index(drop=True)
    for col in ("high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


def add_atr(df: pd.DataFrame, length: int) -> pd.DataFrame:
    out = df.copy()
    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr"] = tr.rolling(length, min_periods=length).mean()
    return out


def is_oos_split(trade_dates: list[str]) -> tuple[set[str], set[str], str | None, str | None]:
    """與 base_study.run_study 完全相同的切分公式（80/20，尾端 OOS）。"""
    oos_cut = max(1, int(len(trade_dates) * (1.0 - base_study.OOS_FRACTION)))
    oos_cut = min(oos_cut, len(trade_dates) - 1) if len(trade_dates) > 1 else len(trade_dates)
    is_dates = set(trade_dates[:oos_cut])
    oos_dates = set(trade_dates[oos_cut:])
    is_end = trade_dates[oos_cut - 1] if oos_cut >= 1 else None
    oos_start = trade_dates[oos_cut] if oos_cut < len(trade_dates) else None
    return is_dates, oos_dates, is_end, oos_start


def exit_nbar(df: pd.DataFrame, entry_pos: int, day_end_pos: int, n: int) -> tuple[float, int]:
    target_pos = min(entry_pos + n, day_end_pos)
    return float(df["close"].iloc[target_pos]), target_pos


def exit_eod(df: pd.DataFrame, day_end_pos: int) -> tuple[float, int]:
    return float(df["close"].iloc[day_end_pos]), day_end_pos


def exit_trailing_atr(
    df: pd.DataFrame, entry_pos: int, day_end_pos: int, atr_entry: float, mult: float
) -> tuple[float, int] | None:
    if atr_entry is None or (isinstance(atr_entry, float) and math.isnan(atr_entry)) or atr_entry <= 0:
        return None
    entry_price = float(df["close"].iloc[entry_pos])
    highwater = entry_price  # 不用進場當根 high，避免用到訊號當根尚未收斂的資訊
    stop = highwater - mult * atr_entry
    for j in range(entry_pos + 1, day_end_pos + 1):
        low_j = float(df["low"].iloc[j])
        high_j = float(df["high"].iloc[j])
        if low_j <= stop:
            return stop, j
        if high_j > highwater:
            highwater = high_j
            stop = highwater - mult * atr_entry
    return float(df["close"].iloc[day_end_pos]), day_end_pos


def summarize_returns(vals: list[float]) -> dict:
    n = len(vals)
    if n == 0:
        return {"n": 0, "win_rate": None, "mean_net_ret": None, "median_net_ret": None}
    return {
        "n": n,
        "win_rate": round(100.0 * sum(v > 0 for v in vals) / n, 2),
        "mean_net_ret": round(statistics.mean(vals), 4),
        "median_net_ret": round(statistics.median(vals), 4),
    }


def run_stock(conn, stock_id: str) -> dict | None:
    bars = load_bars_with_hl(conn, stock_id)
    if bars.empty:
        return None
    df = base_study.build_indicators(bars)
    df = add_atr(df, ATR_LENGTH)
    trade_dates = sorted(df["trade_date"].unique())
    is_dates, oos_dates, is_end, oos_start = is_oos_split(trade_dates)
    day_end = base_study.day_end_lookup(df)

    per_variant: dict[str, dict] = {}
    for variant_id in VARIANTS_TO_TEST:
        triggers = base_study.collect_triggers(df, variant_id, day_end)
        # rule_id -> {"full": [...], "is": [...], "oos": [...]}
        by_rule: dict[str, dict[str, list[float]]] = {
            r: {"full": [], "is": [], "oos": []} for r in RULE_ORDER
        }
        atr_skipped = 0
        for trig in triggers:
            entry_price = float(df["close"].iloc[trig.entry_pos])
            atr_entry = df["atr"].iloc[trig.entry_pos]
            bucket = "is" if trig.trade_date in is_dates else ("oos" if trig.trade_date in oos_dates else None)

            for n in N_BAR_HOLDS:
                exit_price, _ = exit_nbar(df, trig.entry_pos, trig.day_end_pos, n)
                net = (exit_price / entry_price - 1.0) * 100.0 - ROUND_TRIP_COST_PCT
                rule_id = f"nbar_h{n}"
                by_rule[rule_id]["full"].append(net)
                if bucket:
                    by_rule[rule_id][bucket].append(net)

            exit_price, _ = exit_eod(df, trig.day_end_pos)
            net = (exit_price / entry_price - 1.0) * 100.0 - ROUND_TRIP_COST_PCT
            by_rule["eod_only"]["full"].append(net)
            if bucket:
                by_rule["eod_only"][bucket].append(net)

            for mult in ATR_STOP_MULTS:
                res = exit_trailing_atr(df, trig.entry_pos, trig.day_end_pos, atr_entry, mult)
                rule_id = f"atr_stop_{mult}x"
                if res is None:
                    atr_skipped += 1
                    continue
                exit_price, _ = res
                net = (exit_price / entry_price - 1.0) * 100.0 - ROUND_TRIP_COST_PCT
                by_rule[rule_id]["full"].append(net)
                if bucket:
                    by_rule[rule_id][bucket].append(net)

        per_variant[variant_id] = {
            "trigger_count": len(triggers),
            "atr_skipped_no_atr": atr_skipped,
            "rules": {
                rule_id: {
                    "full": summarize_returns(buckets["full"]),
                    "is": summarize_returns(buckets["is"]),
                    "oos": summarize_returns(buckets["oos"]),
                }
                for rule_id, buckets in by_rule.items()
            },
        }

    return {
        "stock_id": stock_id,
        "n_trading_days": len(trade_dates),
        "is_end": is_end,
        "oos_start": oos_start,
        "variants": per_variant,
    }


def sign_test(deltas: list[float]) -> dict:
    n_pos = sum(1 for d in deltas if d > 0)
    n_neg = sum(1 for d in deltas if d < 0)
    n_zero = len(deltas) - n_pos - n_neg
    n_nonzero = n_pos + n_neg
    p_sign = None
    if n_nonzero > 0:
        from math import comb

        k = min(n_pos, n_neg)
        p_sign = round(sum(comb(n_nonzero, i) for i in range(0, k + 1)) * 2 / (2**n_nonzero), 4)
        p_sign = min(p_sign, 1.0)
    return {
        "n": len(deltas),
        "n_positive": n_pos,
        "n_negative": n_neg,
        "n_zero": n_zero,
        "mean_delta": round(statistics.mean(deltas), 4) if deltas else None,
        "median_delta": round(statistics.median(deltas), 4) if deltas else None,
        "sign_test_p_two_sided": p_sign,
    }


def build_report(per_stock: list[dict]) -> dict:
    rule_summary: dict[str, dict] = {}
    for rule_id in RULE_ORDER:
        win_deltas_full, mean_deltas_full = [], []
        win_deltas_oos, mean_deltas_oos = [], []
        baseline_means_full, confirm_means_full = [], []
        for row in per_stock:
            b = row["variants"]["baseline_naive_cross"]["rules"][rule_id]
            c = row["variants"]["and_persist_and_buffer"]["rules"][rule_id]
            if b["full"]["win_rate"] is not None and c["full"]["win_rate"] is not None:
                win_deltas_full.append(c["full"]["win_rate"] - b["full"]["win_rate"])
            if b["full"]["mean_net_ret"] is not None and c["full"]["mean_net_ret"] is not None:
                mean_deltas_full.append(c["full"]["mean_net_ret"] - b["full"]["mean_net_ret"])
                baseline_means_full.append(b["full"]["mean_net_ret"])
                confirm_means_full.append(c["full"]["mean_net_ret"])
            if b["oos"]["win_rate"] is not None and c["oos"]["win_rate"] is not None:
                win_deltas_oos.append(c["oos"]["win_rate"] - b["oos"]["win_rate"])
            if b["oos"]["mean_net_ret"] is not None and c["oos"]["mean_net_ret"] is not None:
                mean_deltas_oos.append(c["oos"]["mean_net_ret"] - b["oos"]["mean_net_ret"])
        rule_summary[rule_id] = {
            "full_win_rate_delta_sign_test": sign_test(win_deltas_full),
            "full_mean_net_ret_delta_sign_test": sign_test(mean_deltas_full),
            "oos_win_rate_delta_sign_test": sign_test(win_deltas_oos),
            "oos_mean_net_ret_delta_sign_test": sign_test(mean_deltas_oos),
            "baseline_mean_net_ret_avg_across_stocks": (
                round(statistics.mean(baseline_means_full), 4) if baseline_means_full else None
            ),
            "confirm_mean_net_ret_avg_across_stocks": (
                round(statistics.mean(confirm_means_full), 4) if confirm_means_full else None
            ),
        }
    return rule_summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="WMA20 bounce-confirm exit + cost model (research, G2 follow-up)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--stocks", nargs="*", default=None)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 2

    universe = args.stocks or UNIVERSE
    conn = connect(args.db)
    per_stock: list[dict] = []
    errors: list[dict] = []
    try:
        for stock_id in universe:
            print(f"running {stock_id} …", flush=True)
            try:
                row = run_stock(conn, stock_id)
            except Exception as e:  # pragma: no cover - defensive
                errors.append({"stock_id": stock_id, "error": str(e)})
                continue
            if row is None:
                errors.append({"stock_id": stock_id, "error": "no bars"})
                continue
            per_stock.append(row)
    finally:
        conn.close()

    rule_summary = build_report(per_stock)

    payload = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe": universe,
        "params": {
            "atr_length": ATR_LENGTH,
            "n_bar_holds": list(N_BAR_HOLDS),
            "atr_stop_mults": list(ATR_STOP_MULTS),
            "round_trip_cost_pct": ROUND_TRIP_COST_PCT,
            "cost_source": "repo-standard 30bps round trip (see config/research.yaml T+1/H7/30bps convention); "
            "conservative vs dayflip-short prod slippage estimate 13-76bps",
        },
        "per_stock": per_stock,
        "errors": errors,
        "rule_summary": rule_summary,
    }

    lines = [
        "# WMA20 反彈確認濾器 · 出場 + 交易成本模型（G2 follow-up）",
        "",
        f"- 樣本股票數：{len(universe)}（{', '.join(universe)}）",
        f"- 成本假設：flat round-trip {ROUND_TRIP_COST_PCT}pp（repo 標準 30bps 慣例）",
        f"- ATR14（5m bar，跨日連續）；N-bar 持有＝{N_BAR_HOLDS}；trailing stop 倍數＝{ATR_STOP_MULTS}",
        "",
        "## Rule-level aggregate（14 檔 delta = confirm − baseline，sign test）",
        "",
        "| Rule | Full winrate Δ(mean/p) | Full mean_net_ret Δ(mean/p) | OOS winrate Δ(mean/p) | "
        "OOS mean_net_ret Δ(mean/p) | baseline avg net | confirm avg net |",
        "|---|---|---|---|---|---|---|",
    ]
    for rule_id in RULE_ORDER:
        s = rule_summary[rule_id]
        wf = s["full_win_rate_delta_sign_test"]
        mf = s["full_mean_net_ret_delta_sign_test"]
        wo = s["oos_win_rate_delta_sign_test"]
        mo = s["oos_mean_net_ret_delta_sign_test"]
        lines.append(
            f"| {rule_id} | {wf['mean_delta']}pp / p={wf['sign_test_p_two_sided']} | "
            f"{mf['mean_delta']}pp / p={mf['sign_test_p_two_sided']} | "
            f"{wo['mean_delta']}pp / p={wo['sign_test_p_two_sided']} | "
            f"{mo['mean_delta']}pp / p={mo['sign_test_p_two_sided']} | "
            f"{s['baseline_mean_net_ret_avg_across_stocks']} | {s['confirm_mean_net_ret_avg_across_stocks']} |"
        )
    lines.append("")
    if errors:
        lines += ["## Errors", ""] + [f"- {e['stock_id']}: {e['error']}" for e in errors] + [""]
    md = "\n".join(lines)
    print(md)

    if args.write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d")
        out = OUT_DIR / f"{stamp}_wma20_bounce_exit_cost_model"
        out.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out.with_suffix(".md").write_text(md + "\n", encoding="utf-8")
        print(f"\nwrote {out.with_suffix('.json')}")
        print(f"wrote {out.with_suffix('.md')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
