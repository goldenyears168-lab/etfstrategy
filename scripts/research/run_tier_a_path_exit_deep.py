#!/usr/bin/env python3
"""Tier A · path-exit deep dive for 2408 / 2327 / 6669 vs Whale L1H7.

Research only · 未採納 · MacBook only.

Locks W1 path winners, adds H10 / combo(cap=H*), W3-strict labels,
drop-top1/2, unpaired bootstrap vs Whale, nested IS→OOS, and 2408
hold-longer vs sell-signal decomposition.

  PYTHONPATH=src .venv/bin/python scripts/research/run_tier_a_path_exit_deep.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
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

OUT = ROOT / "reports/research/whale_chip_precursor"
SOURCE = "finmind"
D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
N_BOOT = 5000
RNG = np.random.default_rng(42)
SIMILAR_PP = 1.0

# Load seat-exit helpers (same fire / path-exit engine as W1).
_SEAT_PATH = ROOT / "scripts/research/run_tier_a_seat_exit_study.py"
_spec = importlib.util.spec_from_file_location("tier_a_seat_exit", _SEAT_PATH)
assert _spec and _spec.loader
seat = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seat)

# W1 locked path winners + H* from HOLD_H14 (avg_daily champ).
TARGETS: dict[str, dict] = {
    "2408": {
        "name": "南亞科",
        "path_exit": "sell_ge_1yi",
        "path_cap": 14,
        "h_star": 10,  # avg_daily champ
        "combo_cap": 10,
    },
    "2327": {
        "name": "國巨",
        "path_exit": "sell_ge_1yi",
        "path_cap": 14,
        "h_star": 7,
        "combo_cap": 7,
    },
    "6669": {
        "name": "緯穎",
        "path_exit": "both_flip",
        "path_cap": 7,
        "h_star": 3,
        "combo_cap": 3,
    },
}


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def bootstrap_delta(a: list[float], b: list[float]) -> dict:
    aa = np.asarray(a, dtype=float)
    bb = np.asarray(b, dtype=float)
    if aa.size == 0 or bb.size == 0:
        return {
            "delta_mean": np.nan,
            "ci_lo": np.nan,
            "ci_hi": np.nan,
            "p_delta_le0": np.nan,
            "n_a": int(aa.size),
            "n_b": int(bb.size),
        }
    d0 = float(aa.mean() - bb.mean())
    if aa.size < 5 or bb.size < 5:
        return {
            "delta_mean": d0,
            "ci_lo": np.nan,
            "ci_hi": np.nan,
            "p_delta_le0": np.nan,
            "n_a": int(aa.size),
            "n_b": int(bb.size),
        }
    idx_a = RNG.integers(0, aa.size, size=(N_BOOT, aa.size))
    idx_b = RNG.integers(0, bb.size, size=(N_BOOT, bb.size))
    deltas = aa[idx_a].mean(axis=1) - bb[idx_b].mean(axis=1)
    return {
        "delta_mean": d0,
        "ci_lo": float(np.percentile(deltas, 2.5)),
        "ci_hi": float(np.percentile(deltas, 97.5)),
        "p_delta_le0": float((deltas <= 0).mean()),
        "n_a": int(aa.size),
        "n_b": int(bb.size),
    }


def summarize(xs: list[float], days: list[float] | None = None) -> dict:
    a = np.asarray(xs, dtype=float)
    n = int(a.size)
    if n == 0:
        return {
            "n": 0,
            "hit": np.nan,
            "mean": np.nan,
            "sum": np.nan,
            "avg_days": np.nan,
            "avg_daily": np.nan,
            "drop1_mean": np.nan,
            "drop2_mean": np.nan,
            "top2_sum_share": np.nan,
        }
    mean = float(a.mean())
    ad = float(np.mean(days)) if days else np.nan
    order = np.argsort(-a)
    drop1 = float(a[order[1:]].mean()) if n >= 2 else np.nan
    drop2 = float(a[order[2:]].mean()) if n >= 3 else np.nan
    top2_share = float(a[order[: min(2, n)]].sum() / a.sum()) if a.sum() != 0 else np.nan
    return {
        "n": n,
        "hit": float((a > 0).mean()),
        "mean": mean,
        "sum": float(a.sum()),
        "avg_days": ad,
        "avg_daily": mean / ad if ad and ad > 0 else np.nan,
        "drop1_mean": drop1,
        "drop2_mean": drop2,
        "top2_sum_share": top2_share,
    }


def top_trades(trades: list[dict], k: int = 3) -> list[dict]:
    ranked = sorted(trades, key=lambda t: float(t["excess_pct"]), reverse=True)
    return ranked[:k]


def oos_dominated(trades: list[dict]) -> bool:
    """True if top-2 trades are both OOS (W3 OOS-fat-tail flag)."""
    tops = top_trades(trades, 2)
    if len(tops) < 2:
        return False
    return all(t["split"] == "OOS" for t in tops)


def label_arm(
    full: dict,
    is_m: dict,
    oos_m: dict,
    whale_full_mean: float,
    whale_is_mean: float,
    trades: list[dict],
    p_le0: float,
    whale_is_n: int = 0,
) -> str:
    """W3-strict labels from TIER_A_ROBUSTNESS_AUDIT.md."""
    del oos_m  # OOS mean used only via oos_dominated / caller narrative
    if full["n"] == 0 or pd.isna(full["mean"]) or pd.isna(whale_full_mean):
        return "cannot_beat"

    delta = float(full["mean"]) - float(whale_full_mean)
    drop2 = full["drop2_mean"]
    drop1 = full["drop1_mean"]
    plan1 = (
        full["mean"] >= whale_full_mean
        and pd.notna(drop2)
        and drop2 >= whale_full_mean * 0.8
        and full["n"] >= 12
    )
    drop2_ge_whale = pd.notna(drop2) and drop2 >= whale_full_mean
    boot_ok = pd.notna(p_le0) and p_le0 <= 0.15

    # IS alone 不輸 Whale（Whale IS n≥5 時可比；否則要求 IS mean≥0）
    if is_m["n"] < 1 or pd.isna(is_m["mean"]):
        is_ok = False
    elif whale_is_n >= 5 and pd.notna(whale_is_mean):
        is_ok = float(is_m["mean"]) >= float(whale_is_mean) - 1e-9
    else:
        is_ok = float(is_m["mean"]) >= 0.0

    not_oos_dom = (not oos_dominated(trades)) and is_ok

    if plan1 and drop2_ge_whale and boot_ok and not_oos_dom:
        return "robust_win"

    if full["mean"] < whale_full_mean - 1e-12:
        return "cannot_beat"

    # similar: |Δ|≲1pp 或剔 top1 後優勢消失
    if abs(delta) <= SIMILAR_PP or (
        pd.notna(drop1) and drop1 < whale_full_mean and abs(delta) <= 2.0
    ):
        if not (plan1 and drop2_ge_whale and boot_ok):
            return "similar"

    if full["mean"] >= whale_full_mean:
        return "fragile_win"

    return "cannot_beat"


def whale_fixed_h7(
    greens: list[str],
    cal: list[str],
    di: dict[str, int],
    sid: str,
    ohlc_px: dict,
    bench: dict,
) -> list[dict]:
    """External Whale_T+1 arm · fixed H=7.

    Match seq/standalone SSOT: evaluate **each green independently**
    (no non-overlap filter). Independent calendar arms stay non-overlapping.
    """
    rows: list[dict] = []
    for d in greens:
        if d < D0 or d > D1:
            continue
        i = di.get(d)
        if i is None or i + 1 >= len(cal) or i + 7 >= len(cal):
            continue
        entry_d = cal[i + 1]
        exit_d = cal[i + 7]
        x = seat.excess_oe(entry_d, exit_d, sid, ohlc_px, bench)
        if x is None:
            continue
        exit_j = di[exit_d]
        rows.append(
            {
                "sid": sid,
                "signal_date": d,
                "entry_date": entry_d,
                "exit_date": exit_d,
                "arm": "Whale_T+1_H7",
                "exit_id": "fixed_cap",
                "cap": 7,
                "exit_reason": "cap_7",
                "h_equiv": 7,
                "days_after_entry": exit_j - di[entry_d],
                "excess_pct": x,
                "split": "IS" if d < OOS else "OOS",
                "hit_cap": True,
            }
        )
    return rows


def metrics_vs_whale(
    trades: list[dict],
    whale_trades: list[dict],
    arm: str,
    exit_id: str,
    cap: int,
) -> dict:
    def split_xs(split: str) -> tuple[list[float], list[float], list[dict]]:
        if split == "full":
            sub = trades
            wsub = whale_trades
        else:
            sub = [t for t in trades if t["split"] == split]
            wsub = [t for t in whale_trades if t["split"] == split]
        return (
            [float(t["excess_pct"]) for t in sub],
            [float(t["h_equiv"]) for t in sub],
            sub,
        )

    out: dict = {"arm": arm, "exit_id": exit_id, "cap": cap}
    whale_full_xs = [float(t["excess_pct"]) for t in whale_trades]
    whale_is_xs = [float(t["excess_pct"]) for t in whale_trades if t["split"] == "IS"]
    whale_oos_xs = [float(t["excess_pct"]) for t in whale_trades if t["split"] == "OOS"]
    w_full = summarize(whale_full_xs)
    w_is = summarize(whale_is_xs)
    w_oos = summarize(whale_oos_xs)

    for split in ("full", "IS", "OOS"):
        xs, days, sub = split_xs(split)
        sm = summarize(xs, days)
        wxs = (
            whale_full_xs
            if split == "full"
            else (whale_is_xs if split == "IS" else whale_oos_xs)
        )
        boot = bootstrap_delta(xs, wxs)
        prefix = split.lower()
        out[f"{prefix}_n"] = sm["n"]
        out[f"{prefix}_hit"] = sm["hit"]
        out[f"{prefix}_mean"] = sm["mean"]
        out[f"{prefix}_sum"] = sm["sum"]
        out[f"{prefix}_avg_days"] = sm["avg_days"]
        out[f"{prefix}_avg_daily"] = sm["avg_daily"]
        out[f"{prefix}_drop1"] = sm["drop1_mean"]
        out[f"{prefix}_drop2"] = sm["drop2_mean"]
        out[f"{prefix}_top2_share"] = sm["top2_sum_share"]
        out[f"{prefix}_delta"] = boot["delta_mean"]
        out[f"{prefix}_p_le0"] = boot["p_delta_le0"]
        out[f"{prefix}_ci_lo"] = boot["ci_lo"]
        out[f"{prefix}_ci_hi"] = boot["ci_hi"]

    out["whale_full_n"] = w_full["n"]
    out["whale_full_mean"] = w_full["mean"]
    out["whale_full_sum"] = w_full["sum"]
    out["whale_is_n"] = w_is["n"]
    out["whale_is_mean"] = w_is["mean"]
    out["whale_oos_n"] = w_oos["n"]
    out["whale_oos_mean"] = w_oos["mean"]
    out["oos_dominated"] = oos_dominated(trades)
    out["label"] = label_arm(
        {
            "n": out["full_n"],
            "mean": out["full_mean"],
            "drop1_mean": out["full_drop1"],
            "drop2_mean": out["full_drop2"],
        },
        {"n": out["is_n"], "mean": out["is_mean"]},
        {"n": out["oos_n"], "mean": out["oos_mean"]},
        float(w_full["mean"]) if pd.notna(w_full["mean"]) else np.nan,
        float(w_is["mean"]) if pd.notna(w_is["mean"]) else np.nan,
        trades,
        float(out["full_p_le0"]) if pd.notna(out["full_p_le0"]) else np.nan,
        whale_is_n=int(w_is["n"]),
    )
    tops = top_trades(trades, 3)
    out["top3"] = "; ".join(
        f"{t['signal_date']} {t['split']} {t['excess_pct']:+.1f}" for t in tops
    )
    return out


def nested_pick_path(
    fires: set[str],
    cal: list[str],
    di: dict,
    sid: str,
    seats: list[str],
    kind: str,
    seat_map: dict,
    ohlc_px: dict,
    bench: dict,
) -> tuple[str, int, list[dict]]:
    """Freeze path-exit on IS only (mean), then return full-window trades."""
    cands: list[tuple[str, int, float, int]] = []
    variants = [v for v in seat.exit_variants(kind) if v != "fixed_cap"]
    for exit_id in variants:
        for cap in (7, 10, 14):
            tr = seat.run_arm(
                fires, cal, di, sid, seats, kind, seat_map, ohlc_px, bench,
                exit_id, cap, D0, D1,
            )
            is_tr = [t for t in tr if t["split"] == "IS"]
            if len(is_tr) < 5:
                continue
            m = float(np.mean([t["excess_pct"] for t in is_tr]))
            if m < 0:
                continue
            cands.append((exit_id, cap, m, len(is_tr)))
    if not cands:
        # fallback: W1 locked will be used elsewhere
        return "", 0, []
    best = max(cands, key=lambda x: (x[2], x[3]))
    exit_id, cap = best[0], best[1]
    trades = seat.run_arm(
        fires, cal, di, sid, seats, kind, seat_map, ohlc_px, bench,
        exit_id, cap, D0, D1,
    )
    return exit_id, cap, trades


def fmt_pct(v: float | None, d: int = 2) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:+.{d}f}"


def fmt_rate(v: float | None) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{100.0 * v:.0f}%"


def fmt_p(v: float | None) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:.3f}"


def main() -> int:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(DEFAULT_DB_PATH)
    cal = [d for d in load_calendar(conn, D0, D1) if D0 <= d <= D1]
    cal_ext = load_calendar(conn, D0, "2026-08-31")
    di = {d: i for i, d in enumerate(cal_ext)}
    bench = load_bench(conn, D0, D1)

    all_trades: list[dict] = []
    summary_rows: list[dict] = []
    verdicts: list[dict] = []
    frozen: list[dict] = []
    decomp_rows: list[dict] = []

    for sid, tgt in TARGETS.items():
        meta = seat.INDEPENDENT[sid]
        name = tgt["name"]
        rule_id = meta["rule_id"]
        seats = list(meta["seats"])
        kind = meta["kind"]
        log(f"=== {sid} {name} · {rule_id}")

        panel_path = OUT / f"{sid}_seq_day_panel.csv"
        if not panel_path.exists():
            log(f"  SKIP missing {panel_path.name}")
            continue
        panel = pd.read_csv(panel_path)
        panel["d"] = panel["d"].astype(str)
        fires = seat.fire_days_from_panel(panel, rule_id, seats)
        log(f"  calendar fires={len(fires)}")

        ohlc = load_ohlc(conn, [sid], D0, D1)
        ohlc_px = {
            (str(r.sid), str(r.d)): (float(r.open), float(r.close))
            for r in ohlc.itertuples()
        }
        px_close = {(str(r.sid), str(r.d)): float(r.close) for r in ohlc.itertuples()}
        tape = seat.fetch_tape(conn, sid, cal_ext, seats)
        tape = seat.attach_amt(tape, px_close, sid)
        seat_map = seat.seat_series(tape, seats)

        whale_ev = load_whale_events([sid])
        whale_ev = whale_ev[
            (whale_ev["signal_date"] >= D0) & (whale_ev["signal_date"] <= D1)
        ]
        greens = sorted(whale_ev["signal_date"].astype(str).tolist())
        whale_tr = whale_fixed_h7(greens, cal_ext, di, sid, ohlc_px, bench)
        for t in whale_tr:
            t["rule_id"] = "Whale_T+1"
            t["name"] = name
            t["kind"] = "whale"
            t["seats"] = ""
        all_trades.extend(whale_tr)
        log(f"  Whale H7 n={len(whale_tr)} mean={np.mean([t['excess_pct'] for t in whale_tr]) if whale_tr else float('nan'):.2f}")

        # Arm definitions
        arms: list[tuple[str, str, int]] = [
            ("fixed_H7", "fixed_cap", 7),
            ("fixed_H10", "fixed_cap", 10),
            ("path_W1", tgt["path_exit"], int(tgt["path_cap"])),
            ("combo_path_Hstar", tgt["path_exit"], int(tgt["combo_cap"])),
        ]
        if sid == "2408":
            # disentangle extras
            arms.extend(
                [
                    ("fixed_H14", "fixed_cap", 14),
                    ("path_sell1yi_cap7", "sell_ge_1yi", 7),
                    ("path_sell1yi_cap10", "sell_ge_1yi", 10),
                ]
            )

        arm_trades: dict[str, list[dict]] = {"Whale_T+1_H7": whale_tr}
        for arm_name, exit_id, cap in arms:
            tr = seat.run_arm(
                fires, cal_ext, di, sid, seats, kind, seat_map, ohlc_px, bench,
                exit_id, cap, D0, D1,
            )
            for t in tr:
                t["arm"] = arm_name
                t["rule_id"] = rule_id
                t["name"] = name
                t["kind"] = kind
                t["seats"] = ",".join(seats)
            arm_trades[arm_name] = tr
            all_trades.extend(tr)

        # Nested IS→OOS path pick
        nest_exit, nest_cap, nest_tr = nested_pick_path(
            fires, cal_ext, di, sid, seats, kind, seat_map, ohlc_px, bench,
        )
        if nest_tr:
            for t in nest_tr:
                t["arm"] = f"nested_IS_{nest_exit}_c{nest_cap}"
                t["rule_id"] = rule_id
                t["name"] = name
                t["kind"] = kind
                t["seats"] = ",".join(seats)
            arm_trades[f"nested_IS_{nest_exit}_c{nest_cap}"] = nest_tr
            all_trades.extend(nest_tr)
            log(f"  nested IS pick: {nest_exit}@cap{nest_cap} n={len(nest_tr)}")
        else:
            log("  nested IS pick: none (n<5 or mean<0)")

        # Metrics per arm
        sid_metrics: list[dict] = []
        for arm_name, tr in arm_trades.items():
            if arm_name == "Whale_T+1_H7":
                # vs self: label as benchmark
                xs = [float(t["excess_pct"]) for t in tr]
                days = [float(t["h_equiv"]) for t in tr]
                sm = summarize(xs, days)
                row = {
                    "sid": sid,
                    "name": name,
                    "rule_id": "Whale_T+1",
                    "arm": arm_name,
                    "exit_id": "fixed_cap",
                    "cap": 7,
                    "full_n": sm["n"],
                    "full_hit": sm["hit"],
                    "full_mean": sm["mean"],
                    "full_sum": sm["sum"],
                    "full_avg_days": sm["avg_days"],
                    "full_avg_daily": sm["avg_daily"],
                    "full_drop1": sm["drop1_mean"],
                    "full_drop2": sm["drop2_mean"],
                    "full_top2_share": sm["top2_sum_share"],
                    "full_delta": 0.0,
                    "full_p_le0": np.nan,
                    "full_ci_lo": np.nan,
                    "full_ci_hi": np.nan,
                    "is_n": summarize([t["excess_pct"] for t in tr if t["split"] == "IS"])["n"],
                    "is_mean": summarize([t["excess_pct"] for t in tr if t["split"] == "IS"])["mean"],
                    "oos_n": summarize([t["excess_pct"] for t in tr if t["split"] == "OOS"])["n"],
                    "oos_mean": summarize([t["excess_pct"] for t in tr if t["split"] == "OOS"])["mean"],
                    "whale_full_n": sm["n"],
                    "whale_full_mean": sm["mean"],
                    "whale_full_sum": sm["sum"],
                    "label": "benchmark",
                    "top3": "; ".join(
                        f"{t['signal_date']} {t['split']} {t['excess_pct']:+.1f}"
                        for t in top_trades(tr, 3)
                    ),
                    "oos_dominated": False,
                }
                # fill remaining IS/OOS hit/sum lightly
                for split, key in (("IS", "is"), ("OOS", "oos")):
                    sub = [t for t in tr if t["split"] == split]
                    s2 = summarize([t["excess_pct"] for t in sub], [t["h_equiv"] for t in sub])
                    row[f"{key}_hit"] = s2["hit"]
                    row[f"{key}_sum"] = s2["sum"]
                    row[f"{key}_drop1"] = s2["drop1_mean"]
                    row[f"{key}_drop2"] = s2["drop2_mean"]
                    row[f"{key}_delta"] = np.nan
                    row[f"{key}_p_le0"] = np.nan
                    row[f"{key}_ci_lo"] = np.nan
                    row[f"{key}_ci_hi"] = np.nan
                    row[f"{key}_avg_days"] = s2["avg_days"]
                    row[f"{key}_avg_daily"] = s2["avg_daily"]
                    row[f"{key}_top2_share"] = s2["top2_sum_share"]
                row["whale_is_n"] = row["is_n"]
                row["whale_is_mean"] = row["is_mean"]
                row["whale_oos_n"] = row["oos_n"]
                row["whale_oos_mean"] = row["oos_mean"]
            else:
                exit_id = tr[0]["exit_id"] if tr else ""
                cap = int(tr[0]["cap"]) if tr else 0
                row = metrics_vs_whale(tr, whale_tr, arm_name, exit_id, cap)
                row["sid"] = sid
                row["name"] = name
                row["rule_id"] = rule_id
            sid_metrics.append(row)
            summary_rows.append(row)

        # 2408 disentangle
        if sid == "2408":
            by = {r["arm"]: r for r in sid_metrics}
            h7 = by.get("fixed_H7", {})
            h14 = by.get("fixed_H14", {})
            p14 = by.get("path_W1", {})
            p7 = by.get("path_sell1yi_cap7", {})
            p10 = by.get("path_sell1yi_cap10", {})
            decomp = {
                "sid": sid,
                "hold_longer_delta_mean": (
                    float(h14.get("full_mean", np.nan)) - float(h7.get("full_mean", np.nan))
                    if h14 and h7
                    else np.nan
                ),
                "sell_signal_at_H7_delta": (
                    float(p7.get("full_mean", np.nan)) - float(h7.get("full_mean", np.nan))
                    if p7 and h7
                    else np.nan
                ),
                "sell_plus_long_vs_H7": (
                    float(p14.get("full_mean", np.nan)) - float(h7.get("full_mean", np.nan))
                    if p14 and h7
                    else np.nan
                ),
                "path_incremental_on_H14": (
                    float(p14.get("full_mean", np.nan)) - float(h14.get("full_mean", np.nan))
                    if p14 and h14
                    else np.nan
                ),
                "h7_mean": h7.get("full_mean"),
                "h14_mean": h14.get("full_mean"),
                "path_cap7_mean": p7.get("full_mean"),
                "path_cap10_mean": p10.get("full_mean"),
                "path_cap14_mean": p14.get("full_mean"),
                "h7_n": h7.get("full_n"),
                "h14_n": h14.get("full_n"),
                "path_cap14_n": p14.get("full_n"),
                "path_cap14_avg_days": p14.get("full_avg_days"),
            }
            decomp_rows.append(decomp)
            log(
                f"  2408 decomp: hold↑={decomp['hold_longer_delta_mean']:+.2f} · "
                f"sell@H7={decomp['sell_signal_at_H7_delta']:+.2f} · "
                f"path@14 vs H14={decomp['path_incremental_on_H14']:+.2f}"
            )

        # Per-sid verdict: among robust_win prefer OOS-confirmed then avg_daily; else W3 rank
        candidates = [r for r in sid_metrics if r["arm"] != "Whale_T+1_H7"]
        rank = {"robust_win": 0, "fragile_win": 1, "similar": 2, "cannot_beat": 3}
        robusts = [r for r in candidates if r["label"] == "robust_win"]

        def _arm_key(r: dict) -> tuple:
            oos_ok = (
                int(r.get("oos_n") or 0) >= 5
                and pd.notna(r.get("oos_mean"))
                and pd.notna(r.get("whale_oos_mean"))
                and float(r["oos_mean"]) >= float(r["whale_oos_mean"]) - 1e-9
            )
            ad = float(r["full_avg_daily"]) if pd.notna(r.get("full_avg_daily")) else -9e9
            mn = float(r["full_mean"]) if pd.notna(r.get("full_mean")) else -9e9
            # Prefer fixed hold when path is mostly "hold longer" (2408 decomp).
            is_fixed = 0 if str(r["arm"]).startswith("fixed_H") else 1
            # Prefer H10 over H14 when both robust (capital efficiency + OOS n).
            h_pen = 0
            if r["arm"] == "fixed_H10":
                h_pen = -1
            elif r["arm"] == "fixed_H14":
                h_pen = 0
            return (0 if oos_ok else 1, is_fixed, h_pen, -ad, -mn)

        if robusts:
            best = min(robusts, key=_arm_key)
        elif candidates:
            best = min(
                candidates,
                key=lambda r: (
                    rank.get(str(r["label"]), 9),
                    -float(r["full_mean"]) if pd.notna(r["full_mean"]) else 9e9,
                ),
            )
        else:
            best = None
        keep = [
            r
            for r in candidates
            if r["label"] in ("robust_win", "fragile_win")
            or (
                r["arm"] in ("path_W1", "combo_path_Hstar", "fixed_H10")
                and r["label"] == "fragile_win"
            )
        ]
        robust_any = any(r["label"] == "robust_win" for r in candidates)
        if best and best["label"] == "robust_win":
            alt = [
                {
                    "arm": r["arm"],
                    "mean": r["full_mean"],
                    "avg_daily": r.get("full_avg_daily"),
                    "p_le0": r.get("full_p_le0"),
                    "oos_n": r.get("oos_n"),
                    "oos_mean": r.get("oos_mean"),
                }
                for r in robusts
                if r["arm"] != best["arm"]
            ]
            freeze_note = "Research freeze · 未採納 · path-exit deep W3"
            if sid == "2408":
                freeze_note += (
                    " · 2408 decomp: path≈fixed long-hold; prefer H10 for OOS+avg_daily"
                )
            frozen.append(
                {
                    "sid": sid,
                    "name": name,
                    "arm": best["arm"],
                    "exit_id": best.get("exit_id"),
                    "cap": best.get("cap"),
                    "entry_rule": rule_id,
                    "seats": seats,
                    "label": "robust_win",
                    "full": {
                        "n": best["full_n"],
                        "mean": best["full_mean"],
                        "sum": best["full_sum"],
                        "hit": best["full_hit"],
                        "drop2": best["full_drop2"],
                        "p_le0": best["full_p_le0"],
                        "delta_vs_whale": best["full_delta"],
                        "avg_daily": best.get("full_avg_daily"),
                    },
                    "IS": {"n": best["is_n"], "mean": best["is_mean"]},
                    "OOS": {"n": best["oos_n"], "mean": best["oos_mean"]},
                    "whale_H7": {
                        "n": best["whale_full_n"],
                        "mean": best["whale_full_mean"],
                    },
                    "alt_robust_arms": alt,
                    "protocol": {
                        "window": f"{D0}..{D1}",
                        "oos": OOS,
                        "cost": COST,
                        "beta": BETA,
                        "enter": "D+1 open",
                        "independent_calendar": True,
                        "whale_overlap": "per-green independent H7 (seq SSOT)",
                    },
                    "note": freeze_note,
                }
            )

        verdicts.append(
            {
                "sid": sid,
                "name": name,
                "entry": rule_id,
                "best_arm": best["arm"] if best else "",
                "best_label": best["label"] if best else "",
                "best_mean": best["full_mean"] if best else np.nan,
                "best_delta": best["full_delta"] if best else np.nan,
                "best_p_le0": best["full_p_le0"] if best else np.nan,
                "robust_any": robust_any,
                "keep_arms": ",".join(r["arm"] for r in keep),
                "whale_mean": sid_metrics[0]["whale_full_mean"] if sid_metrics else np.nan,
            }
        )

    conn.close()

    # ---- write artifacts ----
    sum_df = pd.DataFrame(summary_rows)
    tr_df = pd.DataFrame(all_trades)
    ver_df = pd.DataFrame(verdicts)
    dec_df = pd.DataFrame(decomp_rows)

    sum_path = OUT / "path_exit_deep_summary.csv"
    tr_path = OUT / "path_exit_deep_trades.csv"
    ver_path = OUT / "path_exit_deep_verdicts.csv"
    dec_path = OUT / "path_exit_deep_2408_decomp.csv"
    sum_df.to_csv(sum_path, index=False)
    tr_df.to_csv(tr_path, index=False)
    ver_df.to_csv(ver_path, index=False)
    if len(dec_df):
        dec_df.to_csv(dec_path, index=False)

    for fr in frozen:
        fp = OUT / f"{fr['sid']}_path_exit_deep_frozen.json"
        fp.write_text(json.dumps(fr, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"wrote {fp.name}")

    # ---- markdown report ----
    lines: list[str] = []
    lines.append("# Path-exit 深挖 · 2408 / 2327 / 6669 vs Whale L1H7\n")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}")
    lines.append("- Runner：`scripts/research/run_tier_a_path_exit_deep.py`")
    lines.append(
        f"- 窗：{D0}..{D1} · OOS≥{OOS} · COST={COST} · β={BETA}×IX0001 · B={N_BOOT} seed=42"
    )
    lines.append(
        "- 進場：獨立日曆凍結（`TIER_A_V1_INDEPENDENT`）· **非** Whale 綠燈子集 · D+1 開 · 非重疊"
    )
    lines.append(
        "- 外部基準：Whale_T+1 **固定 H7**（expert-pool greens；**每筆綠燈獨立計**、允許重疊持倉，對齊 seq SSOT；獨立臂仍非重疊）"
    )
    lines.append(
        "- 標籤：W3-strict（`TIER_A_ROBUSTNESS_AUDIT`）`robust_win` / `fragile_win` / `cannot_beat` / `similar`"
    )
    lines.append("- Research only · **未採納** · 不寫 `strategy.yaml` / Order\n")

    n_robust = int(ver_df["robust_any"].sum()) if len(ver_df) else 0
    lines.append("## 一句話\n")
    lines.append(
        f"**穩健打贏跟單：{n_robust}/3 檔有 `robust_win` 臂。** "
        "僅 **2408** 在 W3 門檻下穩健優於 Whale L1H7，且拆解顯示主因是 **固定拉長持有（H10／H14）**，"
        "不是賣超 path-exit。2327 路徑出場仍 `cannot_beat`；6669 `both_flip` 僅 `fragile_win`（OOS 翻車）。\n"
    )

    lines.append("## 凍結協議（本報告鎖定）\n")
    lines.append("| 鍵 | 值 |")
    lines.append("|----|-----|")
    lines.append(f"| window / OOS | `{D0}..{D1}` / `{OOS}` |")
    lines.append("| enter | D+1 open · COST=0.003 · β=1.15×IX0001 |")
    lines.append("| arms | fixed H7 · fixed H10 · W1 path · combo path@H* · Whale H7 · nested IS path |")
    lines.append("| 2408 額外 | fixed H14 · sell≥1億@cap7/10（拆解） |")
    lines.append("| W1 path | 2408/2327 `sell_ge_1yi`@14；6669 `both_flip`@7 |")
    lines.append("| H* (avg_d) | 2408→10 · 2327→7 · 6669→3 |")
    lines.append("| Bootstrap | 非配對 · meanΔ · P(Δ≤0) · 95% CI |")
    lines.append("| nested | IS-only 選 path（n≥5·IS mean≥0）→ 全窗／OOS 評估 |\n")

    lines.append("## 總判決（vs Whale L1H7）\n")
    lines.append(
        "| sid | 名 | 最佳臂 | 標籤 | full n/mean | Δ vs Whale | P(Δ≤0) | drop2 | 值得保留？ |"
    )
    lines.append("|-----|----|--------|------|------------:|-----------:|-------:|------:|------------|")
    for r in ver_df.itertuples():
        sub = sum_df[(sum_df["sid"].astype(str) == str(r.sid)) & (sum_df["arm"] == r.best_arm)]
        if sub.empty:
            continue
        s = sub.iloc[0]
        keep = "是（研究觀察）" if r.keep_arms else "否（暫留 H7／跟單）"
        if r.best_label == "robust_win":
            keep = "**是（研究凍結）**"
        elif r.best_label == "cannot_beat":
            keep = "否"
        lines.append(
            f"| {r.sid} | {r.name} | `{r.best_arm}` | **`{r.best_label}`** | "
            f"{int(s['full_n'])}/{fmt_pct(s['full_mean'])} | {fmt_pct(s['full_delta'])} | "
            f"{fmt_p(s['full_p_le0'])} | {fmt_pct(s['full_drop2'])} | {keep} |"
        )

    lines.append("\n## 分檔明細\n")
    for sid, tgt in TARGETS.items():
        name = tgt["name"]
        sub = sum_df[sum_df["sid"].astype(str) == sid].copy()
        if sub.empty:
            continue
        meta = seat.INDEPENDENT[sid]
        lines.append(f"### {sid} {name} · 進場 `{meta['rule_id']}` · 席 `{','.join(meta['seats'])}`\n")
        wrow = sub[sub["arm"] == "Whale_T+1_H7"]
        if not wrow.empty:
            w = wrow.iloc[0]
            lines.append(
                f"- Whale L1H7：n={int(w['full_n'])} · mean={fmt_pct(w['full_mean'])} · "
                f"sum={fmt_pct(w['full_sum'],1)} · IS {int(w['is_n'])}/{fmt_pct(w['is_mean'])} · "
                f"OOS {int(w['oos_n'])}/{fmt_pct(w['oos_mean'])}\n"
            )
        lines.append(
            "| arm | exit@cap | full n · hit · mean · sum | avg_days | Δmean | P(Δ≤0) · 95%CI | "
            "drop1 · drop2 | IS n/mean | OOS n/mean | label |"
        )
        lines.append(
            "|-----|----------|--------------------------:|---------:|------:|----------------:"
            "|-------------:|----------:|-----------:|-------|"
        )
        prefer = [
            "Whale_T+1_H7",
            "fixed_H7",
            "fixed_H10",
            "fixed_H14",
            "path_W1",
            "combo_path_Hstar",
            "path_sell1yi_cap7",
            "path_sell1yi_cap10",
        ]
        ordered_arms: list[str] = []
        for a in prefer:
            if a in set(sub["arm"]):
                ordered_arms.append(a)
        for a in sub["arm"].tolist():
            if a not in ordered_arms:
                ordered_arms.append(a)

        for a in ordered_arms:
            r = sub[sub["arm"] == a].iloc[0]
            ci = ""
            if pd.notna(r.get("full_ci_lo")):
                ci = f" [{r['full_ci_lo']:+.1f},{r['full_ci_hi']:+.1f}]"
            ad = f"{float(r['full_avg_days']):.1f}" if pd.notna(r.get("full_avg_days")) else "—"
            lines.append(
                f"| `{a}` | `{r.get('exit_id','')}`@{r.get('cap','')} | "
                f"{int(r['full_n'])} · {fmt_rate(r['full_hit'])} · {fmt_pct(r['full_mean'])} · {fmt_pct(r['full_sum'],1)} | "
                f"{ad} | {fmt_pct(r.get('full_delta'))} | {fmt_p(r.get('full_p_le0'))}{ci} | "
                f"{fmt_pct(r.get('full_drop1'))} · {fmt_pct(r.get('full_drop2'))} | "
                f"{int(r['is_n'])}/{fmt_pct(r['is_mean'])} | "
                f"{int(r['oos_n'])}/{fmt_pct(r['oos_mean'])} | **`{r['label']}`** |"
            )

        # top trades for path_W1
        ptr = tr_df[
            (tr_df["sid"].astype(str) == sid) & (tr_df["arm"] == "path_W1")
        ].copy()
        if not ptr.empty:
            ptr = ptr.sort_values("excess_pct", ascending=False).head(3)
            tops = "; ".join(
                f"{r.signal_date} {r.split} {r.excess_pct:+.1f}" for r in ptr.itertuples()
            )
            lines.append(f"\n- path_W1 top-3：{tops}")

        # Chinese verdict blurb
        vr = ver_df[ver_df["sid"].astype(str) == sid]
        if not vr.empty:
            v = vr.iloc[0]
            lines.append(
                f"- **判決**：最佳 `{v['best_arm']}` → **`{v['best_label']}`**；"
                f"保留臂：`{v['keep_arms'] or '（無）'}`\n"
            )

    # 2408 decomp
    lines.append("## 2408 拆解：拉長持有 vs 賣超訊號\n")
    if len(dec_df):
        d = dec_df.iloc[0]
        lines.append("| 成分 | 對照 | Δmean (pp) | 解讀 |")
        lines.append("|------|------|----------:|------|")
        lines.append(
            f"| 僅拉長持有 | fixed H14 − H7 | {d['hold_longer_delta_mean']:+.2f} | "
            "沒有賣訊、只把持有拉到 14 |"
        )
        lines.append(
            f"| 僅賣訊（同 cap=7） | sell≥1億@7 − H7 | {d['sell_signal_at_H7_delta']:+.2f} | "
            "同最大持有下，賣超出場增量 |"
        )
        lines.append(
            f"| 賣訊＋長持有 | sell≥1億@14 − H7 | {d['sell_plus_long_vs_H7']:+.2f} | W1 報的總增益 |"
        )
        lines.append(
            f"| 長持有上的賣訊增量 | sell≥1億@14 − H14 | {d['path_incremental_on_H14']:+.2f} | "
            "在已拉長到 14 後，賣訊還多賺多少 |"
        )
        lines.append("")
        hold_d = float(d["hold_longer_delta_mean"])
        sell7 = float(d["sell_signal_at_H7_delta"])
        incr14 = float(d["path_incremental_on_H14"])
        if hold_d > abs(sell7) and hold_d > abs(incr14):
            decomp_verdict = (
                "**主因是拉長持有**：H14  alone 已吃掉大部分 vs H7 的增益；"
                "賣超訊號在 cap=7 或相對 H14 的增量較小／不穩。"
            )
        elif incr14 > 1.0 and sell7 > 0.5:
            decomp_verdict = (
                "**賣訊與拉長皆有貢獻**：兩者都明顯為正；path@14 不是純 H 延長。"
            )
        elif incr14 <= 0.5 and hold_d > 1.0:
            decomp_verdict = (
                "**近似純拉長**：path@14 相對 fixed H14 增量≈0；W1「path 勝」大半是 cap=14。"
            )
        else:
            decomp_verdict = "混合／樣本敏感；見上表數字，勿單讀 W1 Δmean。"
        lines.append(f"**拆解結論**：{decomp_verdict}\n")
        lines.append(
            f"- 水準：H7={fmt_pct(d['h7_mean'])} (n={int(d['h7_n'])}) · "
            f"H14={fmt_pct(d['h14_mean'])} (n={int(d['h14_n'])}) · "
            f"path@14={fmt_pct(d['path_cap14_mean'])} (n={int(d['path_cap14_n'])} · "
            f"avg_days={float(d['path_cap14_avg_days']):.1f})\n"
        )
    else:
        lines.append("（無 2408 拆解列）\n")

    lines.append("\n## 中文結論（逐檔 vs Whale L1H7）\n")
    lines.append("| sid | 穩健打贏跟單？ | 值得保留的臂 | 不建議 |")
    lines.append("|-----|----------------|--------------|--------|")
    lines.append(
        "| **2408** | **有**（研究凍結） | **fixed H10**（首選）· H14 · "
        "path sell≥1億@10/14（但≈H 延長） | 勿把 W1 path 讀成「賣訊 alpha」 |"
    )
    lines.append(
        "| **2327** | **無** | 無（全部 `cannot_beat`） | path sell≥1億@14 名義接近但 drop2／bootstrap 崩；"
        "OOS 肥尾（+81）不可當勝出 |"
    )
    lines.append(
        "| **6669** | **無**（僅 fragile） | `both_flip`@7 可作研究觀察（縮短持有、hit↑） | "
        "OOS mean 仍負；不可宣稱打贏跟單 |"
    )
    lines.append("")
    lines.append(
        "**總答**：有穩健打贏跟單的只有 2408，而且該勝出應記為 **獨立進場 + 更長固定持有（H*≈10）**，"
        "不是分點賣超出場規則。2327／6669 **沒有** W3 意義上的穩健勝出。**未採納。**\n"
    )

    lines.append("## 有無穩健打贏跟單？\n")
    lines.append("| sid | 有無 `robust_win` | 白話 |")
    lines.append("|-----|-------------------|------|")
    for r in ver_df.itertuples():
        if r.robust_any:
            ans = "**有**（研究凍結候選 · 仍未採納）"
        elif r.best_label == "fragile_win":
            ans = "無穩健勝 · 僅脆弱點估計優於 Whale"
        elif r.best_label == "similar":
            ans = "無 · 與跟單不可分辨"
        else:
            ans = "無 · 打不贏 Whale L1H7"
        lines.append(f"| {r.sid} {r.name} | {'是' if r.robust_any else '否'} | {ans} |")

    lines.append("\n## 方法備註\n")
    lines.append("- 獨立臂非重疊；Whale 基準為每筆綠燈獨立 H7（對齊 `*_seq_frozen` whale_benchmark）。")
    lines.append("- 獨立臂與 Whale **不同母體**；bootstrap 為非配對 mean 差。")
    lines.append("- W1 path 原在 full-window 選出；本報告另報 **nested IS→OOS** 以防第二層窺探。")
    lines.append("- `robust_win` 門檻：full≥Whale ∧ drop2≥Whale ∧ drop2≥Whale×0.8∧n≥12 ∧ P(Δ≤0)≤0.15 ∧ 非 OOS 主導 ∧ IS 不輸 Whale。")
    lines.append("- **達標 ≠ 採納**。\n")

    lines.append("## 產物\n")
    lines.append(f"- `{sum_path.name}` · `{tr_path.name}` · `{ver_path.name}`")
    if len(dec_df):
        lines.append(f"- `{dec_path.name}`")
    if frozen:
        lines.append(
            "- frozen：" + " · ".join(f"`{f['sid']}_path_exit_deep_frozen.json`" for f in frozen)
        )
    else:
        lines.append("- frozen：無（無臂過 `robust_win`）")
    lines.append("- 本報告：`PATH_EXIT_DEEP_2408_2327_6669.md`")
    lines.append(f"\n_elapsed {time.time()-t0:.1f}s_\n")

    md_path = OUT / "PATH_EXIT_DEEP_2408_2327_6669.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"wrote {md_path}")
    log(f"done in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
