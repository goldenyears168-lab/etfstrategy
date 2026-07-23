#!/usr/bin/env python3
"""2327 國巨 · 盤中 WMA20 反彈確認濾器研究（Research · Book-only）。

Question: 對單股 2327 的 5 分K，比較 5 種訊號變體──
naive 單根穿越 WMA20 / 只用「持續性」(連續2根站上) / 只用「緩衝幅度」
(>0.3%) / 兩者 AND / 兩者 OR──哪個變體在前向報酬與假訊號率上表現較好。

Entry-only：只量測進場訊號後的前向報酬，不含出場／停損，也不代表可直接
採納為 Strategy（單股樣本小，見 config/research.yaml
topics.2327-wma20-bounce-confirm）。

用法::

  PYTHONPATH=src .venv/bin/python scripts/research/run_2327_wma20_bounce_confirm_study.py --write
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import REPORTS_RESEARCH  # noqa: E402
from rrg_rotation import wma  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

DEFAULT_STOCK_ID = "2327"
WMA_LENGTH = 20
BUFFER_PCT = 0.003  # 0.3%
PERSISTENCE_BARS = 2
HORIZONS_BARS = (3, 6, 12)  # ~15 / 30 / 60 分鐘（5分K）
SESSION_MINUTE_LO = "09:00"
SESSION_MINUTE_HI = "13:30"
OOS_FRACTION = 0.2  # 最後 20% 交易日作為 OOS holdout

OUT_DIR = REPORTS_RESEARCH / "2327-wma20-bounce"
SCHEMA = "2327_wma20_bounce_confirm-v1"

VARIANT_ORDER = (
    "baseline_naive_cross",
    "persistence_only",
    "buffer_only",
    "and_persist_and_buffer",
    "or_persist_or_buffer",
)

VARIANT_LABEL: dict[str, str] = {
    "baseline_naive_cross": "Baseline · 單根瞬間穿越 WMA20",
    "persistence_only": "持續性單獨：連續2根站上 WMA20",
    "buffer_only": "緩衝幅度單獨：穿越 WMA20×1.003",
    "and_persist_and_buffer": "AND：持續性 且 緩衝幅度",
    "or_persist_or_buffer": "OR：持續性 或 緩衝幅度",
}


@dataclass(frozen=True)
class Trigger:
    trade_date: str
    entry_pos: int
    day_end_pos: int


def load_close_bars(
    conn,
    stock_id: str,
    *,
    start: str | None = None,
    end: str | None = None,
) -> pd.DataFrame:
    """主線 5m close（09:00–13:30），跨日連續排序。

    同 ``research.backtest.leading_dip_sleeve_validate._load_kbar_by_day`` 的
    session window + drop_duplicates 慣例，但回傳單一連續 DataFrame（保留跨日
    序列供 WMA20 rolling 使用），而非切成逐日 dict。
    """
    q = (
        "SELECT trade_date, substr(minute,1,5) AS minute, close "
        "FROM stock_kbar_5m WHERE stock_id=?"
    )
    params: list[str] = [stock_id]
    if start:
        q += " AND trade_date>=?"
        params.append(start)
    if end:
        q += " AND trade_date<=?"
        params.append(end)
    q += " ORDER BY trade_date, minute"
    df = pd.read_sql(q, conn, params=params)
    df = df[(df["minute"] >= SESSION_MINUTE_LO) & (df["minute"] <= SESSION_MINUTE_HI)]
    df = df.drop_duplicates(["trade_date", "minute"], keep="last")
    df = df.sort_values(["trade_date", "minute"]).reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    return df


def _first_true_consecutive_end(mask: pd.Series, *, bars: int) -> pd.Series:
    """對每個 bar，回傳「往前數 ``bars`` 根（含自己）是否全為 True」。"""
    k = max(1, int(bars))
    if k == 1:
        return mask.fillna(False).astype(bool)
    m = mask.fillna(False).astype(bool)
    run = m.copy()
    for shift in range(1, k):
        run = run & m.shift(shift, fill_value=False)
    return run


def _edge(state: pd.Series) -> pd.Series:
    """False→True transition mask (fresh event, not a carried-over state)."""
    s = state.fillna(False).astype(bool)
    return s & ~s.shift(1, fill_value=False)


def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["wma20"] = wma(out["close"], WMA_LENGTH)
    above = out["close"] > out["wma20"]
    buffer_state = out["close"] > out["wma20"] * (1.0 + BUFFER_PCT)
    persist_state = _first_true_consecutive_end(above, bars=PERSISTENCE_BARS)
    and_state = persist_state & buffer_state
    or_state = persist_state | buffer_state

    out["above"] = above
    out["buffer_state"] = buffer_state
    out["persist_state"] = persist_state
    # 全部變體都用「狀態剛由 False 轉 True」的 edge，而不是狀態本身──
    # 否則長多頭延續期間，每天開盤第一根都會誤判成「新的反彈確認」。
    out["baseline_naive_cross"] = _edge(above)
    out["persistence_only"] = _edge(persist_state)
    out["buffer_only"] = _edge(buffer_state)
    out["and_persist_and_buffer"] = _edge(and_state)
    out["or_persist_or_buffer"] = _edge(or_state)
    return out


def day_end_lookup(df: pd.DataFrame) -> dict[str, int]:
    out: dict[str, int] = {}
    for trade_date, group in df.groupby("trade_date", sort=True):
        out[str(trade_date)] = int(group.index.to_numpy()[-1])
    return out


def collect_triggers(df: pd.DataFrame, variant_col: str, day_end: dict[str, int]) -> list[Trigger]:
    triggers: list[Trigger] = []
    hit_positions = df.index[df[variant_col]]
    for pos in hit_positions:
        trade_date = str(df["trade_date"].iloc[pos])
        triggers.append(
            Trigger(trade_date=trade_date, entry_pos=int(pos), day_end_pos=day_end[trade_date])
        )
    return triggers


def evaluate_trigger(df: pd.DataFrame, trig: Trigger) -> dict:
    entry_price = float(df["close"].iloc[trig.entry_pos])
    rec: dict = {"trade_date": trig.trade_date, "entry_pos": trig.entry_pos}
    for h in HORIZONS_BARS:
        target_pos = trig.entry_pos + h
        if target_pos <= trig.day_end_pos:
            rec[f"ret_h{h}"] = (float(df["close"].iloc[target_pos]) / entry_price - 1.0) * 100.0
        else:
            rec[f"ret_h{h}"] = None
    rec["ret_eod"] = (float(df["close"].iloc[trig.day_end_pos]) / entry_price - 1.0) * 100.0
    seg = df.iloc[trig.entry_pos + 1 : trig.day_end_pos + 1]
    if seg.empty:
        rec["washout"] = False
    else:
        rec["washout"] = bool((seg["close"] <= seg["wma20"]).any())
    return rec


def summarize(records: list[dict]) -> dict:
    n = len(records)
    out: dict = {"trigger_count": n}
    for h in HORIZONS_BARS:
        col = f"ret_h{h}"
        vals = [r[col] for r in records if r[col] is not None]
        out[f"n_h{h}"] = len(vals)
        out[f"win_rate_h{h}"] = round(100.0 * sum(v > 0 for v in vals) / len(vals), 2) if vals else None
        out[f"mean_ret_h{h}"] = round(sum(vals) / len(vals), 4) if vals else None
    eod_vals = [r["ret_eod"] for r in records]
    out["win_rate_eod"] = round(100.0 * sum(v > 0 for v in eod_vals) / len(eod_vals), 2) if eod_vals else None
    out["mean_ret_eod"] = round(sum(eod_vals) / len(eod_vals), 4) if eod_vals else None
    washout_vals = [r["washout"] for r in records]
    out["washout_rate"] = (
        round(100.0 * sum(washout_vals) / len(washout_vals), 2) if washout_vals else None
    )
    return out


def run_study(conn, stock_id: str, *, start: str | None, end: str | None) -> dict:
    bars = load_close_bars(conn, stock_id, start=start, end=end)
    if bars.empty:
        raise SystemExit(f"no stock_kbar_5m rows for stock_id={stock_id!r}")
    df = build_indicators(bars)

    trade_dates = sorted(df["trade_date"].unique())
    oos_cut = max(1, int(len(trade_dates) * (1.0 - OOS_FRACTION)))
    oos_cut = min(oos_cut, len(trade_dates) - 1) if len(trade_dates) > 1 else len(trade_dates)
    is_dates = set(trade_dates[:oos_cut])
    oos_dates = set(trade_dates[oos_cut:])
    is_end = trade_dates[oos_cut - 1] if oos_cut >= 1 else None
    oos_start = trade_dates[oos_cut] if oos_cut < len(trade_dates) else None
    day_end = day_end_lookup(df)

    variants_out = []
    for variant_id in VARIANT_ORDER:
        triggers = collect_triggers(df, variant_id, day_end)
        records = [evaluate_trigger(df, t) for t in triggers]
        is_records = [r for r in records if r["trade_date"] in is_dates]
        oos_records = [r for r in records if r["trade_date"] in oos_dates]
        variants_out.append(
            {
                "id": variant_id,
                "label": VARIANT_LABEL[variant_id],
                "full": summarize(records),
                "is": summarize(is_records),
                "oos": summarize(oos_records),
            }
        )

    verdict = build_verdict(variants_out)

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stock_id": stock_id,
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "n_trading_days": len(trade_dates),
        "is_end": is_end,
        "oos_start": oos_start,
        "params": {
            "wma_length": WMA_LENGTH,
            "buffer_pct": BUFFER_PCT,
            "persistence_bars": PERSISTENCE_BARS,
            "horizons_bars": list(HORIZONS_BARS),
            "session": f"{SESSION_MINUTE_LO}-{SESSION_MINUTE_HI}",
            "wma_continuous_across_days": True,
        },
        "variants": variants_out,
        "verdict": verdict,
    }


def build_verdict(variants_out: list[dict]) -> dict:
    by_id = {v["id"]: v["full"] for v in variants_out}
    baseline = by_id["baseline_naive_cross"]
    persist = by_id["persistence_only"]
    buffer_ = by_id["buffer_only"]
    and_v = by_id["and_persist_and_buffer"]
    or_v = by_id["or_persist_or_buffer"]

    def _rate(d: dict, key: str) -> float:
        v = d.get(key)
        return v if v is not None else float("-inf")

    h1_supported = (
        _rate(and_v, "win_rate_eod") >= _rate(baseline, "win_rate_eod")
        and (and_v.get("washout_rate") or 0.0) <= (baseline.get("washout_rate") or 100.0)
    )
    h2_supported = _rate(and_v, "win_rate_eod") >= _rate(or_v, "win_rate_eod") and (
        and_v.get("washout_rate") or 0.0
    ) <= (or_v.get("washout_rate") or 100.0)

    return {
        "primary_metric": "win_rate_eod (同日收盤前向報酬勝率) + washout_rate",
        "H1_and_beats_baseline": "supported" if h1_supported else "not_supported",
        "H2_and_beats_or": "supported" if h2_supported else "not_supported",
        "trigger_counts": {
            "baseline_naive_cross": baseline["trigger_count"],
            "persistence_only": persist["trigger_count"],
            "buffer_only": buffer_["trigger_count"],
            "and_persist_and_buffer": and_v["trigger_count"],
            "or_persist_or_buffer": or_v["trigger_count"],
        },
        "caveat": (
            "單股 2327 樣本，不同 variant 的觸發日多有重疊，差異可能落在小樣本"
            "噪音範圍內；僅供訊號品質探索，G2 未過前不得寫入 strategy/order。"
        ),
    }


def render_md(payload: dict) -> str:
    lines = [
        f"# 2327 WMA20 反彈確認濾器研究（{payload['date_start']} ~ {payload['date_end']}）",
        "",
        f"- 交易日數：{payload['n_trading_days']}（IS 截至 {payload['is_end']} · OOS 自 {payload['oos_start']}）",
        f"- 參數：WMA{payload['params']['wma_length']} · buffer={payload['params']['buffer_pct']*100:.1f}%"
        f" · persistence={payload['params']['persistence_bars']}根 · session={payload['params']['session']}",
        "",
        "## 全期（Full）比較",
        "",
        "| Variant | 觸發次數 | win_rate_eod | mean_ret_eod | win_rate_h12 | mean_ret_h12 | washout_rate |",
        "|---|---|---|---|---|---|---|",
    ]
    for v in payload["variants"]:
        f = v["full"]
        lines.append(
            f"| {v['label']} | {f['trigger_count']} | {f.get('win_rate_eod')} | "
            f"{f.get('mean_ret_eod')} | {f.get('win_rate_h12')} | {f.get('mean_ret_h12')} | "
            f"{f.get('washout_rate')} |"
        )
    lines += [
        "",
        "## OOS holdout 比較",
        "",
        "| Variant | 觸發次數(OOS) | win_rate_eod | mean_ret_eod | washout_rate |",
        "|---|---|---|---|---|",
    ]
    for v in payload["variants"]:
        o = v["oos"]
        lines.append(
            f"| {v['label']} | {o['trigger_count']} | {o.get('win_rate_eod')} | "
            f"{o.get('mean_ret_eod')} | {o.get('washout_rate')} |"
        )
    verdict = payload["verdict"]
    lines += [
        "",
        "## Verdict",
        "",
        f"- H1（AND 優於 baseline 單根穿越）：**{verdict['H1_and_beats_baseline']}**",
        f"- H2（AND 優於 OR）：**{verdict['H2_and_beats_or']}**",
        f"- {verdict['caveat']}",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="2327 WMA20 bounce confirm study (research)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--stock-id", default=DEFAULT_STOCK_ID)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--write", action="store_true", help="Write JSON+MD under reports/research/2327-wma20-bounce/")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"BLOCKER: database not found: {args.db}", file=sys.stderr)
        return 2

    conn = connect(args.db)
    try:
        payload = run_study(conn, args.stock_id, start=args.start, end=args.end)
    finally:
        conn.close()

    md = render_md(payload)
    print(md)

    if args.write:
        stamp = datetime.now().strftime("%Y%m%d")
        out = args.out or (OUT_DIR / f"{stamp}_2327_wma20_bounce_confirm")
        out.parent.mkdir(parents=True, exist_ok=True)
        json_path = out.with_suffix(".json")
        md_path = out.with_suffix(".md")
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md_path.write_text(md + "\n", encoding="utf-8")
        print(f"\nwrote {json_path}")
        print(f"wrote {md_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
