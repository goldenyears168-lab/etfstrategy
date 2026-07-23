#!/usr/bin/env python3
"""2327 國巨 · L1H* hold-horizon sweep (avg daily + stability).

Research only · 未採納 · Book only.

Arms (entry fixed → D+1 / T+1 open):
  - Whale_T+1 (primary): expert-pool greens → T+1 open
  - Seq_8840_accel_p90 (secondary): frozen sequence from 2327_seq_frozen.json
  - Prior_918e+9665 (optional): both seats net>0 on D∧D−1

Hold H ∈ {1,2,3,4,5,6,7,8,9,10,12,15}; COST=0.003; β=1.15×IX0001.
Exit: cal[signal_idx + H] close (same as prior 2327 L1H* scripts).

  PYTHONPATH=src .venv/bin/python scripts/research/run_2327_hold_horizon_sweep.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.adapters_db import (  # noqa: E402
    connect_ro,
    load_bench,
    load_calendar,
    load_ohlc,
)
from research.chen_chip.whale_events import load_whale_events  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402

SID = "2327"
OUT = ROOT / "reports/research/whale_chip_precursor"
TRADES_DIR = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
)
FROZEN_SEQ = OUT / f"{SID}_seq_frozen.json"
SOURCE = "finmind"
D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
HOLDS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 15)
N_BOOT = 5000
RNG = np.random.default_rng(42)
FOREIGN_KW = (
    "美商",
    "摩根",
    "瑞銀",
    "美林",
    "港商",
    "港麥",
    "麥格理",
    "野村",
    "瑞士",
    "花旗",
    "渣打",
    "香港",
    "新加坡",
    "大和",
    "法銀",
    "德意志",
    "巴克萊",
    "高盛",
    "美銀",
)
SEAT_8840 = "8840"
SEAT_918E = "918e"
SEAT_9665 = "9665"


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


def excess_l1(
    signal_d: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict[tuple[str, str], tuple[float, float]],
    bench: dict[str, float],
) -> tuple[float | None, str | None, str | None]:
    """Enter next open after signal_d; exit cal[i+hold] close."""
    i = di.get(signal_d)
    if i is None or i + 1 >= len(cal) or i + hold >= len(cal):
        return None, None, None
    ed = cal[i + 1]
    xd = cal[i + hold]
    er = ohlc_px.get((SID, ed))
    xr = ohlc_px.get((SID, xd))
    be, bx = bench.get(ed), bench.get(xd)
    if not er or not xr or not be or not bx:
        return None, None, None
    eo, _ = er
    _, xp = xr
    if eo <= 0 or xp <= 0 or be <= 0 or bx <= 0:
        return None, None, None
    sr = xp / eo - 1.0 - COST
    br = bx / be - 1.0
    return (sr - BETA * br) * 100.0, ed, xd


def bootstrap_mean_ci(xs: list[float]) -> tuple[float, float]:
    a = np.asarray(xs, dtype=float)
    if a.size < 5:
        return np.nan, np.nan
    idx = RNG.integers(0, a.size, size=(N_BOOT, a.size))
    means = a[idx].mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def summarize_hold(xs: list[float], hold: int, label: str = "") -> dict:
    a = np.asarray(xs, dtype=float)
    n = int(a.size)
    empty = {
        "arm": label,
        "hold": hold,
        "n": 0,
        "hit_rate": np.nan,
        "med_excess": np.nan,
        "mean_excess": np.nan,
        "sum_excess": np.nan,
        "std_excess": np.nan,
        "avg_daily": np.nan,
        "median_daily": np.nan,
        "pseudo_sharpe": np.nan,
        "mean_ci_lo": np.nan,
        "mean_ci_hi": np.nan,
    }
    if n == 0:
        return empty
    mean = float(a.mean())
    med = float(np.median(a))
    std = float(a.std(ddof=1)) if n >= 2 else np.nan
    ci_lo, ci_hi = bootstrap_mean_ci(xs)
    return {
        "arm": label,
        "hold": hold,
        "n": n,
        "hit_rate": float((a > 0).mean()),
        "med_excess": med,
        "mean_excess": mean,
        "sum_excess": float(a.sum()),
        "std_excess": std,
        "avg_daily": mean / hold,
        "median_daily": med / hold,
        "pseudo_sharpe": (mean / std) if (pd.notna(std) and std > 1e-12) else np.nan,
        "mean_ci_lo": ci_lo,
        "mean_ci_hi": ci_hi,
    }


def whale_trades(
    greens: list[str],
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
) -> list[dict]:
    rows: list[dict] = []
    for T in greens:
        x, ed, xd = excess_l1(T, hold, cal, di, ohlc_px, bench)
        if x is None:
            continue
        rows.append(
            {
                "sid": SID,
                "signal_date": T,
                "entry_date": ed,
                "exit_date": xd,
                "arm": "Whale_T+1",
                "rule": "Whale_T+1",
                "hold": hold,
                "excess_pct": x,
                "split": "IS" if T < OOS else "OOS",
            }
        )
    return rows


def standalone_trades(
    fire_days: set[str],
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
    arm: str,
    rule: str,
    day_lo: str,
    day_hi: str,
) -> list[dict]:
    """Non-overlapping calendar fires → D+1 open."""
    rows: list[dict] = []
    next_free = -1
    for j, d in enumerate(cal):
        if d < day_lo or d > day_hi:
            continue
        if d not in fire_days:
            continue
        if j < next_free:
            continue
        x, ed, xd = excess_l1(d, hold, cal, di, ohlc_px, bench)
        if x is None or ed is None or xd is None:
            continue
        exit_j = di.get(xd)
        if exit_j is None:
            continue
        next_free = exit_j + 1
        rows.append(
            {
                "sid": SID,
                "signal_date": d,
                "entry_date": ed,
                "exit_date": xd,
                "arm": arm,
                "rule": rule,
                "hold": hold,
                "excess_pct": x,
                "split": "IS" if d < OOS else "OOS",
            }
        )
    return rows


def fetch_tape(conn, dates: list[str]) -> pd.DataFrame:
    if not dates:
        return pd.DataFrame(
            columns=["d", "sid", "tid", "tname", "buy", "sell", "net", "amt"]
        )
    uniq = sorted(set(dates))
    parts: list[pd.DataFrame] = []
    chunk = 40
    for i in range(0, len(uniq), chunk):
        batch = uniq[i : i + chunk]
        ph = ",".join("?" * len(batch))
        df = pd.read_sql_query(
            f"""
            SELECT trade_date AS d, stock_id AS sid,
                   securities_trader_id AS tid, securities_trader AS tname,
                   buy, sell, net
            FROM stock_broker_branch_daily
            WHERE source=? AND stock_id=? AND trade_date IN ({ph})
            """,
            conn,
            params=[SOURCE, SID, *batch],
        )
        if not df.empty:
            parts.append(df)
    if not parts:
        return pd.DataFrame(
            columns=["d", "sid", "tid", "tname", "buy", "sell", "net", "amt"]
        )
    out = pd.concat(parts, ignore_index=True)
    for c in ("buy", "sell", "net"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    out["sid"] = out["sid"].astype(str)
    out["tid"] = out["tid"].astype(str)
    out["d"] = out["d"].astype(str)
    return out


def attach_amt(tape: pd.DataFrame, px: dict[tuple[str, str], float]) -> pd.DataFrame:
    t = tape.copy()
    t["close"] = [px.get((r.sid, r.d), np.nan) for r in t.itertuples()]
    t["amt"] = t["net"] * t["close"]
    return t


def build_branch_amt(
    tape: pd.DataFrame, seats: list[str]
) -> dict[str, dict[str, float]]:
    want = set(seats)
    out: dict[str, dict[str, float]] = {}
    for r in tape.itertuples():
        tid = str(r.tid)
        if tid not in want:
            continue
        d = str(r.d)
        amt = float(r.amt) if pd.notna(r.amt) else 0.0
        out.setdefault(d, {})[tid] = amt
    return out


def fire_8840_accel_p90(
    cal: list[str],
    di: dict[str, int],
    branch_amt: dict[str, dict[str, float]],
) -> tuple[set[str], float]:
    """Reconstruct frozen seq: 8840 accel ∧ D≥IS-p90(positive amt)."""
    amts_is: list[float] = []
    for d in cal:
        if d >= OOS:
            continue
        a0 = branch_amt.get(d, {}).get(SEAT_8840, 0.0)
        if a0 > 0:
            amts_is.append(a0)
    thr90 = float(np.quantile(amts_is, 0.90)) if len(amts_is) >= 30 else 1e8
    fires: set[str] = set()
    for d in cal:
        i = di.get(d)
        if i is None or i < 1:
            continue
        dm1 = cal[i - 1]
        a0 = branch_amt.get(d, {}).get(SEAT_8840, 0.0)
        a1 = branch_amt.get(dm1, {}).get(SEAT_8840, 0.0)
        if a1 > 0 and a0 > a1 and a0 >= thr90:
            fires.add(d)
    return fires, thr90


def fire_prior_918e_9665(
    cal: list[str],
    di: dict[str, int],
    branch_amt: dict[str, dict[str, float]],
) -> set[str]:
    fires: set[str] = set()
    for d in cal:
        i = di.get(d)
        if i is None or i < 1:
            continue
        dm1 = cal[i - 1]
        a918_d = branch_amt.get(d, {}).get(SEAT_918E, 0.0)
        a966_d = branch_amt.get(d, {}).get(SEAT_9665, 0.0)
        a918_m = branch_amt.get(dm1, {}).get(SEAT_918E, 0.0)
        a966_m = branch_amt.get(dm1, {}).get(SEAT_9665, 0.0)
        if a918_d > 0 and a966_d > 0 and a918_m > 0 and a966_m > 0:
            fires.add(d)
    return fires


def split_xs(trades: list[dict], split: str) -> list[float]:
    if split == "full":
        return [t["excess_pct"] for t in trades]
    return [t["excess_pct"] for t in trades if t["split"] == split]


def stability_score(full: dict, oos: dict) -> float:
    """Propose: avg_daily × hit × soft_sharpe × OOS_ratio.

    soft_sharpe = clip(mean/std, 0, 3)
    OOS_ratio = 1 if oos n<5; else clip(oos_avg_daily / full_avg_daily, 0, 1.25)
                when full_avg_daily>0; 0 if oos_avg_daily≤0.
    """
    ad = full.get("avg_daily")
    hit = full.get("hit_rate")
    sharpe = full.get("pseudo_sharpe")
    if pd.isna(ad) or pd.isna(hit) or full.get("n", 0) < 3:
        return np.nan
    soft = 1.0
    if pd.notna(sharpe):
        soft = float(np.clip(sharpe, 0.0, 3.0))
    oos_n = int(oos.get("n") or 0)
    oos_ad = oos.get("avg_daily")
    if oos_n >= 5 and pd.notna(oos_ad) and pd.notna(ad):
        if float(ad) <= 0:
            oos_ratio = 0.0 if float(oos_ad) <= 0 else 1.0
        elif float(oos_ad) <= 0:
            oos_ratio = 0.0
        else:
            oos_ratio = float(np.clip(float(oos_ad) / float(ad), 0.0, 1.25))
    else:
        oos_ratio = 1.0
    return float(ad) * float(hit) * soft * oos_ratio


def fmt_pct(x: float | None, digits: int = 2) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{x:+.{digits}f}"


def fmt_rate(x: float | None) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{100 * x:.0f}%"


def fmt_num(x: float | None, digits: int = 2) -> str:
    if x is None or pd.isna(x):
        return "—"
    return f"{x:.{digits}f}"


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def pick_best_avg_daily(rows: list[dict]) -> dict | None:
    cand = [r for r in rows if r["split"] == "full" and r["n"] >= 3 and pd.notna(r["avg_daily"])]
    if not cand:
        return None
    return max(cand, key=lambda r: float(r["avg_daily"]))


def pick_best_stable(rows: list[dict]) -> dict | None:
    cand = [
        r
        for r in rows
        if r["split"] == "full" and r["n"] >= 3 and pd.notna(r.get("stability_score"))
    ]
    if not cand:
        return None
    return max(cand, key=lambda r: float(r["stability_score"]))


def main() -> int:
    global SID, FROZEN_SEQ, SEAT_8840, SEAT_918E, SEAT_9665
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sid", default="2327", help="stock id (default 2327)")
    ap.add_argument("--d1", default=D1)
    ap.add_argument("--skip-secondary", action="store_true")
    args = ap.parse_args()
    SID = str(args.sid)
    FROZEN_SEQ = OUT / f"{SID}_seq_frozen.json"
    trades_path = TRADES_DIR / f"{SID}.json"
    if not trades_path.exists():
        raise SystemExit(f"missing trades/{SID}.json")
    doc = json.loads(trades_path.read_text(encoding="utf-8"))
    core = {str(k): str(v) for k, v in (doc.get("core") or {}).items()}
    if not core:
        raise SystemExit(f"empty core in trades/{SID}.json")
    # Primary secondary-arm seat: 2327 keeps 8840; else first core seat.
    if SID == "2327" and "8840" in core:
        SEAT_8840 = "8840"
    else:
        SEAT_8840 = next(iter(core.keys()))
    # Prior pair only meaningful for 2327; clear for others.
    if SID != "2327":
        SEAT_918E = ""
        SEAT_9665 = ""
    d1 = str(args.d1)

    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"{SID} hold-horizon sweep · bench_seat={SEAT_8840} · db={DEFAULT_DB_PATH}")

    conn = connect_ro(DEFAULT_DB_PATH)
    whale = load_whale_events([SID])
    whale = whale[(whale["signal_date"] >= D0) & (whale["signal_date"] <= d1)].copy()
    greens = sorted(whale["signal_date"].astype(str).tolist())
    log(f"Whale greens n={len(greens)}")

    cal = [d for d in load_calendar(conn, D0, d1) if D0 <= d <= d1]
    cal_ext = load_calendar(conn, D0, "2026-08-31")
    di = {d: i for i, d in enumerate(cal_ext)}
    bench = load_bench(conn, D0, d1)
    ohlc = load_ohlc(conn, [SID], D0, d1)
    ohlc_px = {
        (str(r.sid), str(r.d)): (float(r.open), float(r.close))
        for r in ohlc.itertuples()
    }
    px_close = {(str(r.sid), str(r.d)): float(r.close) for r in ohlc.itertuples()}

    fire_8840: set[str] = set()
    fire_prior: set[str] = set()
    thr90 = np.nan
    seq_label = ""
    if not args.skip_secondary:
        log("load branch tape for secondary arms")
        tape = fetch_tape(conn, cal)
        tape = attach_amt(tape, px_close)
        tape["is_foreign"] = tape["tname"].map(is_foreign)
        tape_dom = tape[~tape["is_foreign"]].copy()
        seats = [s for s in (SEAT_8840, SEAT_918E, SEAT_9665) if s]
        branch_amt = build_branch_amt(tape_dom, seats)
        fire_8840, thr90 = fire_8840_accel_p90(cal, di, branch_amt)
        fire_prior = fire_prior_918e_9665(cal, di, branch_amt) if SEAT_918E and SEAT_9665 else set()
        if FROZEN_SEQ.exists():
            fr = json.loads(FROZEN_SEQ.read_text(encoding="utf-8"))
            seq_label = (fr.get("freeze_rule") or {}).get("label", "")
        log(
            f"8840 accel≥p90 fires={len(fire_8840)} thr90={thr90/1e8:.2f}億 · "
            f"918e+9665 fires={len(fire_prior)}"
        )
    conn.close()

    all_trades: list[dict] = []
    summary_rows: list[dict] = []

    arms: list[tuple[str, str, str, Callable[[int], list[dict]]]] = [
        (
            "Whale_T+1",
            "Whale_T+1",
            "primary",
            lambda h: whale_trades(greens, h, cal_ext, di, ohlc_px, bench),
        ),
    ]
    if fire_8840:
        arms.append(
            (
                "Seq_8840_accel_p90",
                f"8840 accel≥IS-p90({thr90/1e8:.2f}億)",
                "secondary",
                lambda h, fd=fire_8840: standalone_trades(
                    fd, h, cal_ext, di, ohlc_px, bench,
                    "Seq_8840_accel_p90", "seat_8840__accel_p90", D0, d1,
                ),
            )
        )
    if fire_prior:
        arms.append(
            (
                "Prior_918e+9665",
                "918e+9665 D∧D−1 net>0",
                "optional",
                lambda h, fd=fire_prior: standalone_trades(
                    fd, h, cal_ext, di, ohlc_px, bench,
                    "Prior_918e+9665", "br__pair_918e_9665__both__net", D0, d1,
                ),
            )
        )

    for arm_id, rule, role, maker in arms:
        log(f"arm={arm_id} ({role})")
        by_hold: dict[int, dict[str, dict]] = {}
        for hold in HOLDS:
            trades = maker(hold)
            all_trades.extend(trades)
            full = summarize_hold(split_xs(trades, "full"), hold, arm_id)
            ism = summarize_hold(split_xs(trades, "IS"), hold, arm_id)
            oos = summarize_hold(split_xs(trades, "OOS"), hold, arm_id)
            by_hold[hold] = {"full": full, "IS": ism, "OOS": oos}
            for split, sm in (("full", full), ("IS", ism), ("OOS", oos)):
                row = {
                    "arm": arm_id,
                    "rule": rule,
                    "role": role,
                    "hold": hold,
                    "split": split,
                    **{k: sm[k] for k in (
                        "n", "hit_rate", "med_excess", "mean_excess", "sum_excess",
                        "std_excess", "avg_daily", "median_daily", "pseudo_sharpe",
                        "mean_ci_lo", "mean_ci_hi",
                    )},
                }
                summary_rows.append(row)
        for hold in HOLDS:
            full = by_hold[hold]["full"]
            oos = by_hold[hold]["OOS"]
            score = stability_score(full, oos)
            for row in summary_rows:
                if row["arm"] == arm_id and row["hold"] == hold:
                    row["stability_score"] = score

    sum_df = pd.DataFrame(summary_rows)
    tr_df = pd.DataFrame(all_trades)
    csv_path = OUT / f"{SID}_hold_horizon_sweep.csv"
    trades_path = OUT / f"{SID}_hold_horizon_trades.csv"
    sum_df.to_csv(csv_path, index=False)
    tr_df.to_csv(trades_path, index=False)
    log(f"wrote {csv_path.name} rows={len(sum_df)} · trades={len(tr_df)}")

    # ---- verdicts (Whale primary) ----
    whale_full = [
        r for r in summary_rows
        if r["arm"] == "Whale_T+1" and r["split"] == "full"
    ]
    best_ad = pick_best_avg_daily(whale_full)
    # attach stability onto whale_full view
    whale_full_scored = [
        r for r in summary_rows
        if r["arm"] == "Whale_T+1" and r["split"] == "full"
    ]
    best_st = pick_best_stable(whale_full_scored)
    h7 = next((r for r in whale_full if r["hold"] == 7), None)

    lines: list[str] = []
    lines.append("# 2327 國巨 · Hold horizon（L1H*）掃參\n")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- Runner：`scripts/research/run_2327_hold_horizon_sweep.py`")
    lines.append(
        f"- 窗：{D0}..{d1} · OOS≥{OOS} · COST={COST} · β={BETA}×IX0001"
    )
    lines.append(
        "- 進場：訊號日收盤判定 → **次一交易日開盤**（Whale=綠燈 T→T+1；"
        "序列臂=D→D+1）"
    )
    lines.append(
        "- 出場：`cal[signal_idx + H]` 收盤（與既有 L1H* 腳本同構；"
        "H=1 = 進場日開→收）"
    )
    lines.append(f"- Whale 綠燈 n={len(greens)}（IS={sum(1 for g in greens if g < OOS)} / "
                 f"OOS={sum(1 for g in greens if g >= OOS)}）")
    if seq_label:
        lines.append(f"- 凍結序列標籤：{seq_label}")
    if pd.notna(thr90):
        lines.append(f"- 8840 IS-p90 門檻：{thr90/1e8:.2f}億（正金額樣本）")
    lines.append("- Research only · **未採納**進 Strategy／Order\n")

    lines.append("## 結論（先讀）\n")
    if best_ad:
        lines.append(
            f"**平均日報酬率最高（Whale_T+1 full）：H={best_ad['hold']}** · "
            f"avg_daily={fmt_pct(best_ad['avg_daily'])}%/日 · "
            f"mean={fmt_pct(best_ad['mean_excess'])}% · hit={fmt_rate(best_ad['hit_rate'])} · "
            f"n={best_ad['n']}"
        )
    else:
        lines.append("**平均日報酬率**：樣本不足，無法判定。")
    if best_st:
        lines.append(
            f"**最穩定最好（Whale_T+1 · stability_score）：H={best_st['hold']}** · "
            f"score={fmt_num(best_st.get('stability_score'), 3)} · "
            f"avg_daily={fmt_pct(best_st['avg_daily'])}%/日 · "
            f"hit={fmt_rate(best_st['hit_rate'])} · "
            f"pseudo_Sharpe={fmt_num(best_st['pseudo_sharpe'])}"
        )
    if h7 and best_ad:
        gap = float(h7["avg_daily"]) - float(best_ad["avg_daily"])
        near = abs(gap) <= 0.15 * abs(float(best_ad["avg_daily"])) if best_ad["avg_daily"] else False
        note = "已接近最優" if near or h7["hold"] == best_ad["hold"] else "未達最優"
        lines.append(
            f"- **H=7 經典**：avg_daily={fmt_pct(h7['avg_daily'])}%/日 · "
            f"vs 冠軍 Δ={fmt_pct(gap)} pp/日 → **{note}**"
        )
    lines.append("")
    lines.append(
        "### 穩定分數公式\n\n"
        "```text\n"
        "stability_score = avg_daily × hit_rate × soft_sharpe × OOS_ratio\n"
        "  avg_daily     = mean_excess / H\n"
        "  soft_sharpe   = clip(mean_excess / std_excess, 0, 3)\n"
        "  OOS_ratio     = 1  if OOS n<5\n"
        "                = clip(oos_avg_daily / full_avg_daily, 0, 1.25)\n"
        "                  if full_avg_daily>0 and oos_avg_daily>0\n"
        "                = 0  if OOS n≥5 and oos_avg_daily≤0\n"
        "```\n"
    )

    # ---- Whale table ----
    lines.append("## Whale_T+1（主臂 · 綠燈 → T+1 開）\n")
    lines.append("### Full\n")
    rows_md: list[list[str]] = []
    for hold in HOLDS:
        r = next(
            (x for x in summary_rows
             if x["arm"] == "Whale_T+1" and x["hold"] == hold and x["split"] == "full"),
            None,
        )
        if not r:
            continue
        mark = ""
        if best_ad and r["hold"] == best_ad["hold"]:
            mark += " ★avg"
        if best_st and r["hold"] == best_st["hold"]:
            mark += " ★stable"
        if hold == 7:
            mark += " ·經典"
        rows_md.append(
            [
                str(hold) + mark,
                str(r["n"]),
                fmt_rate(r["hit_rate"]),
                fmt_pct(r["med_excess"]),
                fmt_pct(r["mean_excess"]),
                fmt_pct(r["avg_daily"]),
                fmt_pct(r["median_daily"]),
                fmt_num(r["std_excess"]),
                fmt_num(r["pseudo_sharpe"]),
                f"[{fmt_pct(r['mean_ci_lo'])}, {fmt_pct(r['mean_ci_hi'])}]",
                fmt_num(r.get("stability_score"), 3),
            ]
        )
    lines.append(
        md_table(
            [
                "H", "n", "hit", "med%", "mean%", "avg_daily%/日",
                "med_daily%/日", "std", "mean/std", "mean 95%CI", "stab_score",
            ],
            rows_md,
        )
    )
    lines.append("")

    # IS / OOS if n allows
    for split in ("IS", "OOS"):
        sub = [
            x for x in summary_rows
            if x["arm"] == "Whale_T+1" and x["split"] == split and x["n"] > 0
        ]
        if not sub:
            continue
        max_n = max(int(x["n"]) for x in sub)
        if max_n < 3 and split == "IS":
            lines.append(
                f"### {split}\n\n"
                f"樣本偏薄（max n={max_n}），僅附 H=7 參考，勿過度解讀。\n"
            )
            r7 = next((x for x in sub if x["hold"] == 7), None)
            if r7:
                lines.append(
                    f"- H=7 {split}：n={r7['n']} · hit={fmt_rate(r7['hit_rate'])} · "
                    f"mean={fmt_pct(r7['mean_excess'])}% · "
                    f"avg_daily={fmt_pct(r7['avg_daily'])}%/日\n"
                )
            continue
        lines.append(f"### {split}\n")
        rows_s: list[list[str]] = []
        for hold in HOLDS:
            r = next((x for x in sub if x["hold"] == hold), None)
            if not r or r["n"] == 0:
                continue
            rows_s.append(
                [
                    str(hold),
                    str(r["n"]),
                    fmt_rate(r["hit_rate"]),
                    fmt_pct(r["mean_excess"]),
                    fmt_pct(r["avg_daily"]),
                    fmt_num(r["pseudo_sharpe"]),
                ]
            )
        lines.append(
            md_table(
                ["H", "n", "hit", "mean%", "avg_daily%/日", "mean/std"],
                rows_s,
            )
        )
        lines.append("")

    # ---- Secondary arms ----
    for arm_id, title in (
        ("Seq_8840_accel_p90", "序列臂 · 8840 accel≥IS-p90 → D+1 開（非重疊）"),
        ("Prior_918e+9665", "可選對照 · 918e+9665 D∧D−1 → D+1 開（非重疊）"),
    ):
        arm_full = [
            r for r in summary_rows
            if r["arm"] == arm_id and r["split"] == "full"
        ]
        if not arm_full:
            continue
        lines.append(f"## {title}\n")
        bad = pick_best_avg_daily(arm_full)
        bst = pick_best_stable(arm_full)
        if bad:
            lines.append(
                f"- avg_daily 冠軍：**H={bad['hold']}** "
                f"({fmt_pct(bad['avg_daily'])}%/日 · n={bad['n']})"
            )
        if bst:
            lines.append(
                f"- stability 冠軍：**H={bst['hold']}** "
                f"(score={fmt_num(bst.get('stability_score'), 3)} · "
                f"n={bst['n']})"
            )
        lines.append("")
        rows_a: list[list[str]] = []
        for hold in HOLDS:
            r = next((x for x in arm_full if x["hold"] == hold), None)
            if not r:
                continue
            oos = next(
                (x for x in summary_rows
                 if x["arm"] == arm_id and x["hold"] == hold and x["split"] == "OOS"),
                None,
            )
            rows_a.append(
                [
                    str(hold),
                    str(r["n"]),
                    fmt_rate(r["hit_rate"]),
                    fmt_pct(r["mean_excess"]),
                    fmt_pct(r["avg_daily"]),
                    fmt_num(r["std_excess"]),
                    fmt_num(r["pseudo_sharpe"]),
                    fmt_pct(oos["avg_daily"]) if oos else "—",
                    fmt_num(r.get("stability_score"), 3),
                ]
            )
        lines.append(
            md_table(
                [
                    "H", "n", "hit", "mean%", "avg_daily%/日",
                    "std", "mean/std", "OOS avg_d%", "stab_score",
                ],
                rows_a,
            )
        )
        lines.append("")

    lines.append("## 方法備註\n")
    lines.append(
        "- 超額 = 個股(開→出場收 − COST) − β×大盤同期報酬；單位 %。\n"
        "- **avg_daily% = mean_excess / H**（持有天數簡單均攤；非幾何日化）。\n"
        "- Whale 綠燈稀疏，各 H 幾乎同 n；序列臂採非重疊，H 愈長 n 愈少屬預期。\n"
        "- 綠燈 IS 極薄時，IS 欄僅供對照；穩定分數的 OOS_ratio 在 OOS n≥5 才啟動。\n"
        f"- CSV：`{csv_path.name}` · trades：`{trades_path.name}`\n"
    )
    lines.append(f"\n耗時 {time.time() - t0:.1f}s\n")

    md_path = OUT / f"{SID}_HOLD_HORIZON.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"wrote {md_path}")

    # console verdict
    print("\n=== VERDICT (Whale_T+1) ===")
    if best_ad:
        print(
            f"最高 avg_daily: H={best_ad['hold']} "
            f"avg_daily={best_ad['avg_daily']:+.3f}%/d "
            f"mean={best_ad['mean_excess']:+.2f}% hit={best_ad['hit_rate']:.0%}"
        )
    if best_st:
        print(
            f"最穩定最好: H={best_st['hold']} "
            f"score={best_st.get('stability_score'):.3f} "
            f"avg_daily={best_st['avg_daily']:+.3f}%/d"
        )
    if h7:
        print(
            f"H=7: avg_daily={h7['avg_daily']:+.3f}%/d "
            f"mean={h7['mean_excess']:+.2f}% score={h7.get('stability_score')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
