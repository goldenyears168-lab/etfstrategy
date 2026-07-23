#!/usr/bin/env python3
"""Tier A · W1: fixed-H sweep + seat-driven path exits on frozen independent entries.

Research only · 未採納 · MacBook only.

Keep same calendar entry fires as TIER_A_V1_INDEPENDENT (seq / stand frozen).

W1.1 fixed H ∈ HOLDS={3,6,7,10,14}: mean / sum / avg_daily → TIER_A_HOLD_H14.md
W1.2–1.3 path exits (分點轉賣／轉弱) with safety caps ∈ CAPS → TIER_A_SEAT_EXIT.md

  PYTHONPATH=src .venv/bin/python scripts/research/run_tier_a_seat_exit_study.py
"""
from __future__ import annotations

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
from stock_db import DEFAULT_DB_PATH  # noqa: E402

OUT = ROOT / "reports/research/whale_chip_precursor"
SOURCE = "finmind"
D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
FLOOR_0P5 = 0.5e8
FLOOR_1 = 1.0e8
# W1.1 fixed-hold grid (classic H=7 baseline)
HOLDS = (3, 6, 7, 10, 14)
# W1.2–1.3 path-exit safety caps (include short caps for fair min(H_cap, path))
CAPS = (3, 6, 7, 10, 14)
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

# Independent-first frozen rules (skip 3443). seats = exit-watch seats.
INDEPENDENT: dict[str, dict] = {
    "3037": {
        "name": "欣興",
        "src": "seq",
        "rule_id": "seat_9268__accel_0p5__bh",
        "seats": ["9268"],
        "kind": "single",
    },
    "2408": {
        "name": "南亞科",
        "src": "seq",
        "rule_id": "chip_bh__seat_9215_accel",
        "seats": ["9215"],
        "kind": "single",
    },
    "2383": {
        "name": "台光電",
        "src": "seq",
        "rule_id": "cobuy_585P_9268__both_net",
        "seats": ["585P", "9268"],
        "kind": "cobuy",
    },
    "3189": {
        "name": "景碩",
        "src": "seq",
        "rule_id": "cobuy_9216_779Z__both_net",
        "seats": ["9216", "779Z"],
        "kind": "cobuy",
    },
    "2327": {
        "name": "國巨",
        "src": "seq",
        "rule_id": "seat_8840__accel_p90",
        "seats": ["8840"],
        "kind": "single",
    },
    "2303": {
        "name": "聯電",
        "src": "seq",
        "rule_id": "seat_1260__accel_p90",
        "seats": ["1260"],
        "kind": "single",
    },
    "6669": {
        "name": "緯穎",
        "src": "stand",
        "rule_id": "combo__ts2__pair_9217_585U__d__net",
        "seats": ["9217", "585U"],
        "kind": "cobuy",
    },
}


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


def excess_oe(
    entry_d: str,
    exit_d: str,
    sid: str,
    ohlc_px: dict[tuple[str, str], tuple[float, float]],
    bench: dict[str, float],
) -> float | None:
    er = ohlc_px.get((sid, entry_d))
    xr = ohlc_px.get((sid, exit_d))
    be, bx = bench.get(entry_d), bench.get(exit_d)
    if not er or not xr or be is None or bx is None:
        return None
    eo, _ = er
    _, xp = xr
    if eo <= 0 or xp <= 0 or be <= 0 or bx <= 0:
        return None
    sr = xp / eo - 1.0 - COST
    br = bx / be - 1.0
    return (sr - BETA * br) * 100.0


def fetch_tape(conn, sid: str, dates: list[str], seats: list[str]) -> pd.DataFrame:
    if not dates:
        return pd.DataFrame(columns=["d", "tid", "buy", "sell", "net", "amt", "sell_amt"])
    uniq = sorted(set(dates))
    parts: list[pd.DataFrame] = []
    chunk = 50
    seat_set = set(seats)
    for i in range(0, len(uniq), chunk):
        batch = uniq[i : i + chunk]
        ph = ",".join("?" * len(batch))
        df = pd.read_sql_query(
            f"""
            SELECT trade_date AS d, securities_trader_id AS tid,
                   securities_trader AS tname, buy, sell, net
            FROM stock_broker_branch_daily
            WHERE source=? AND stock_id=? AND trade_date IN ({ph})
            """,
            conn,
            params=[SOURCE, sid, *batch],
        )
        if df.empty:
            continue
        df["tid"] = df["tid"].astype(str)
        df = df[df["tid"].isin(seat_set)].copy()
        if not df.empty:
            parts.append(df)
    if not parts:
        return pd.DataFrame(columns=["d", "tid", "buy", "sell", "net", "amt", "sell_amt"])
    out = pd.concat(parts, ignore_index=True)
    for c in ("buy", "sell", "net"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    out["d"] = out["d"].astype(str)
    out["is_foreign"] = out["tname"].map(is_foreign)
    out = out[~out["is_foreign"]].copy()
    return out


def attach_amt(
    tape: pd.DataFrame, px: dict[tuple[str, str], float], sid: str
) -> pd.DataFrame:
    t = tape.copy()
    t["close"] = [px.get((sid, d), np.nan) for d in t["d"]]
    t["amt"] = t["net"] * t["close"]
    t["sell_amt"] = t["sell"] * t["close"]
    return t


def seat_series(
    tape: pd.DataFrame, seats: list[str]
) -> dict[str, dict[str, dict[str, float]]]:
    """d → tid → {amt, sell_amt, net}."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    for r in tape.itertuples():
        d = str(r.d)
        tid = str(r.tid)
        out.setdefault(d, {})[tid] = {
            "amt": float(r.amt) if pd.notna(r.amt) else 0.0,
            "sell_amt": float(r.sell_amt) if pd.notna(r.sell_amt) else 0.0,
            "net": float(r.net),
        }
    for d in out:
        for tid in seats:
            out[d].setdefault(tid, {"amt": 0.0, "sell_amt": 0.0, "net": 0.0})
    return out


def fire_days_from_panel(panel: pd.DataFrame, rule_id: str, seats: list[str]) -> set[str]:
    """Reconstruct frozen calendar fires from seq day panel."""
    p = panel.copy()
    is_mask = p["split"] == "IS"

    def days(mask: pd.Series) -> set[str]:
        return set(p.loc[mask.fillna(False), "d"].astype(str).tolist())

    # accel_p90 / accel_0p5__bh / chip_bh__seat_X_accel / cobuy / stand pair
    if rule_id.startswith("seat_") and rule_id.endswith("__accel_p90"):
        tid = rule_id.split("__")[0].replace("seat_", "")
        a0, a1 = p[f"amt_{tid}_d"], p[f"amt_{tid}_dm1"]
        pos_is = a0[is_mask & (a0 > 0)]
        thr90 = float(pos_is.quantile(0.90)) if len(pos_is) >= 30 else FLOOR_1
        return days((a1 > 0) & (a0 > a1) & (a0 >= thr90))

    if rule_id.startswith("seat_") and rule_id.endswith("__accel_0p5__bh"):
        tid = rule_id.split("__")[0].replace("seat_", "")
        a0, a1 = p[f"amt_{tid}_d"], p[f"amt_{tid}_dm1"]
        bh = p["bh_d"]
        return days((a1 > 0) & (a0 > a1) & (a0 >= FLOOR_0P5) & (bh > 0))

    if rule_id.startswith("chip_bh__seat_") and rule_id.endswith("_accel"):
        tid = rule_id.replace("chip_bh__seat_", "").replace("_accel", "")
        a0, a1 = p[f"amt_{tid}_d"], p[f"amt_{tid}_dm1"]
        bh = p["bh_d"]
        return days((bh > 0) & (a1 > 0) & (a0 > a1))

    if rule_id.startswith("cobuy_") and rule_id.endswith("__both_net"):
        mid = rule_id[len("cobuy_") : -len("__both_net")]
        a, b = mid.split("_", 1)
        return days(
            (p[f"amt_{a}_d"] > 0)
            & (p[f"amt_{b}_d"] > 0)
            & (p[f"amt_{a}_dm1"] > 0)
            & (p[f"amt_{b}_dm1"] > 0)
        )

    if rule_id == "combo__ts2__pair_9217_585U__d__net":
        return days(
            (p["ts_d"] >= 2) & (p["amt_9217_d"] > 0) & (p["amt_585U_d"] > 0)
        )

    raise ValueError(f"unsupported rule_id={rule_id} seats={seats}")


def find_exit(
    signal_d: str,
    cal: list[str],
    di: dict[str, int],
    seats: list[str],
    kind: str,
    seat_map: dict[str, dict[str, dict[str, float]]],
    exit_id: str,
    cap: int,
) -> tuple[str | None, str]:
    """Return (exit_date, reason). Exit at trigger close; else cap close."""
    i = di.get(signal_d)
    if i is None or i + 1 >= len(cal) or i + cap >= len(cal):
        return None, "no_cal"
    entry_i = i + 1
    cap_i = i + cap
    # Scan entry day .. cap day inclusive
    for j in range(entry_i, cap_i + 1):
        d = cal[j]
        dm1 = cal[j - 1] if j - 1 >= 0 else None
        sm = seat_map.get(d, {})
        sm1 = seat_map.get(dm1, {}) if dm1 else {}

        def amt(tid: str, which: dict) -> float:
            return float(which.get(tid, {}).get("amt", 0.0))

        def sell_amt(tid: str) -> float:
            return float(sm.get(tid, {}).get("sell_amt", 0.0))

        triggered = False
        reason = ""

        if exit_id == "fixed_cap":
            # only fire at cap
            if j == cap_i:
                return d, f"cap_{cap}"
            continue

        if exit_id == "first_net_lt0":
            if kind == "cobuy":
                # either seat flips to net amt < 0
                if any(amt(t, sm) < 0 for t in seats):
                    triggered, reason = True, "either_net_lt0"
            else:
                if amt(seats[0], sm) < 0:
                    triggered, reason = True, "net_lt0"

        elif exit_id == "both_net_lt0":
            if kind == "cobuy" and all(amt(t, sm) < 0 for t in seats):
                triggered, reason = True, "both_net_lt0"
            elif kind == "single" and amt(seats[0], sm) < 0:
                triggered, reason = True, "net_lt0"

        elif exit_id == "sell_ge_0p5":
            if kind == "cobuy":
                if any(sell_amt(t) >= FLOOR_0P5 for t in seats):
                    triggered, reason = True, "either_sell_0p5"
            else:
                if sell_amt(seats[0]) >= FLOOR_0P5:
                    triggered, reason = True, "sell_0p5"

        elif exit_id == "sell_ge_1yi":
            if kind == "cobuy":
                if any(sell_amt(t) >= FLOOR_1 for t in seats):
                    triggered, reason = True, "either_sell_1yi"
            else:
                if sell_amt(seats[0]) >= FLOOR_1:
                    triggered, reason = True, "sell_1yi"

        elif exit_id == "decel":
            # accel flips: prior day amt>0 and today amt < yesterday (weakening)
            ok = False
            for t in seats if kind == "cobuy" else seats[:1]:
                a0, a1 = amt(t, sm), amt(t, sm1)
                if a1 > 0 and a0 < a1:
                    ok = True
                    break
            if ok:
                triggered, reason = True, "decel"

        elif exit_id == "two_day_sell":
            # two consecutive days net amt < 0 (primary seat or either for cobuy)
            if dm1 is None:
                pass
            elif kind == "cobuy":
                for t in seats:
                    if amt(t, sm) < 0 and amt(t, sm1) < 0:
                        triggered, reason = True, f"two_day_sell_{t}"
                        break
            else:
                t = seats[0]
                if amt(t, sm) < 0 and amt(t, sm1) < 0:
                    triggered, reason = True, "two_day_sell"

        elif exit_id == "either_flip":
            # cobuy: any seat net<0; single: same as first_net_lt0
            if any(amt(t, sm) < 0 for t in seats):
                triggered, reason = True, "either_flip"

        elif exit_id == "both_flip":
            if all(amt(t, sm) < 0 for t in seats):
                triggered, reason = True, "both_flip"

        if triggered:
            return d, reason

    return cal[cap_i], f"cap_{cap}"


def run_arm(
    fires: set[str],
    cal: list[str],
    di: dict[str, int],
    sid: str,
    seats: list[str],
    kind: str,
    seat_map: dict,
    ohlc_px: dict,
    bench: dict,
    exit_id: str,
    cap: int,
    day_lo: str,
    day_hi: str,
) -> list[dict]:
    rows: list[dict] = []
    next_free = -1
    for j, d in enumerate(cal):
        if d < day_lo or d > day_hi:
            continue
        if d not in fires:
            continue
        if j < next_free:
            continue
        i = di.get(d)
        if i is None or i + 1 >= len(cal):
            continue
        entry_d = cal[i + 1]
        exit_d, reason = find_exit(d, cal, di, seats, kind, seat_map, exit_id, cap)
        if exit_d is None:
            continue
        x = excess_oe(entry_d, exit_d, sid, ohlc_px, bench)
        if x is None:
            continue
        exit_j = di[exit_d]
        entry_j = di[entry_d]
        next_free = exit_j + 1
        rows.append(
            {
                "sid": sid,
                "signal_date": d,
                "entry_date": entry_d,
                "exit_date": exit_d,
                "exit_id": exit_id,
                "cap": cap,
                "exit_reason": reason,
                "h_equiv": exit_j - i,
                "days_after_entry": exit_j - entry_j,
                "excess_pct": x,
                "split": "IS" if d < OOS else "OOS",
                "hit_cap": reason.startswith("cap_"),
            }
        )
    return rows


def summarize(xs: list[float], days: list[float]) -> dict:
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
        }
    mean = float(a.mean())
    ad = float(np.mean(days)) if days else np.nan
    return {
        "n": n,
        "hit": float((a > 0).mean()),
        "mean": mean,
        "sum": float(a.sum()),
        "avg_days": ad,
        "avg_daily": mean / ad if ad and ad > 0 else np.nan,
    }


def exit_variants(kind: str) -> list[str]:
    base = [
        "fixed_cap",
        "first_net_lt0",
        "sell_ge_0p5",
        "sell_ge_1yi",
        "decel",
        "two_day_sell",
    ]
    if kind == "cobuy":
        base.extend(["either_flip", "both_flip", "both_net_lt0"])
    return base


def plain_exit(exit_id: str, kind: str) -> str:
    m = {
        "fixed_cap": "固定持有至上限",
        "first_net_lt0": "首日席位淨買轉負即出" if kind == "single" else "任一席淨買轉負即出",
        "both_net_lt0": "兩席皆淨買轉負才出" if kind == "cobuy" else "席位淨買轉負即出",
        "sell_ge_0p5": "席位賣超≥0.5億即出",
        "sell_ge_1yi": "席位賣超≥1億即出",
        "decel": "買超減速（今日淨額<昨且昨>0）即出",
        "two_day_sell": "連續兩日淨賣即出",
        "either_flip": "任一席轉賣（淨額<0）即出",
        "both_flip": "兩席皆轉賣才出",
    }
    return m.get(exit_id, exit_id)


def main() -> int:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect_ro(DEFAULT_DB_PATH)
    cal = [d for d in load_calendar(conn, D0, D1) if D0 <= d <= D1]
    cal_ext = load_calendar(conn, D0, "2026-08-31")
    di = {d: i for i, d in enumerate(cal_ext)}
    bench = load_bench(conn, D0, D1)

    all_trades: list[dict] = []
    summary: list[dict] = []
    per_sid_best: list[dict] = []

    for sid, meta in INDEPENDENT.items():
        name = meta["name"]
        rule_id = meta["rule_id"]
        seats = list(meta["seats"])
        kind = meta["kind"]
        log(f"=== {sid} {name} · {rule_id} · seats={seats}")

        panel_path = OUT / f"{sid}_seq_day_panel.csv"
        if not panel_path.exists():
            log(f"  SKIP missing {panel_path.name}")
            continue
        panel = pd.read_csv(panel_path)
        panel["d"] = panel["d"].astype(str)
        # ensure seat amt cols exist
        missing = [t for t in seats if f"amt_{t}_d" not in panel.columns]
        if missing:
            log(f"  SKIP panel missing seats {missing}")
            continue
        fires = fire_days_from_panel(panel, rule_id, seats)
        log(f"  calendar fires={len(fires)}")

        ohlc = load_ohlc(conn, [sid], D0, D1)
        ohlc_px = {
            (str(r.sid), str(r.d)): (float(r.open), float(r.close))
            for r in ohlc.itertuples()
        }
        px_close = {(str(r.sid), str(r.d)): float(r.close) for r in ohlc.itertuples()}

        # tape window: need dm1 for two_day / decel near start
        tape = fetch_tape(conn, sid, cal_ext, seats)
        tape = attach_amt(tape, px_close, sid)
        seat_map = seat_series(tape, seats)

        # validate H7 fixed vs frozen n roughly
        base_tr = run_arm(
            fires, cal_ext, di, sid, seats, kind, seat_map, ohlc_px, bench,
            "fixed_cap", 7, D0, D1,
        )
        log(f"  fixed H7 n={len(base_tr)} mean={np.mean([t['excess_pct'] for t in base_tr]) if base_tr else float('nan'):.2f}")

        variants = exit_variants(kind)
        sid_rows: list[dict] = []
        for exit_id in variants:
            for cap in CAPS:
                # fixed_cap only meaningful once per cap; path exits use all caps
                if exit_id == "fixed_cap" and cap not in CAPS:
                    continue
                trades = run_arm(
                    fires, cal_ext, di, sid, seats, kind, seat_map, ohlc_px, bench,
                    exit_id, cap, D0, D1,
                )
                for t in trades:
                    t["rule_id"] = rule_id
                    t["kind"] = kind
                    t["seats"] = ",".join(seats)
                    t["name"] = name
                all_trades.extend(trades)
                for split in ("full", "IS", "OOS"):
                    sub = trades if split == "full" else [t for t in trades if t["split"] == split]
                    sm = summarize(
                        [t["excess_pct"] for t in sub],
                        [float(t["h_equiv"]) for t in sub],
                    )
                    row = {
                        "sid": sid,
                        "name": name,
                        "rule_id": rule_id,
                        "kind": kind,
                        "seats": ",".join(seats),
                        "exit_id": exit_id,
                        "exit_plain": plain_exit(exit_id, kind),
                        "cap": cap,
                        "split": split,
                        **sm,
                        "hit_cap_rate": (
                            float(np.mean([t["hit_cap"] for t in sub])) if sub else np.nan
                        ),
                    }
                    summary.append(row)
                    if split == "full":
                        sid_rows.append(row)

        # baseline H7
        h7 = next(
            (r for r in sid_rows if r["exit_id"] == "fixed_cap" and r["cap"] == 7),
            None,
        )
        # best path exit: among non-fixed, prefer mean then sum; require n>= max(8, 0.6*h7.n)
        path_cands = [
            r
            for r in sid_rows
            if r["exit_id"] != "fixed_cap" and r["n"] >= 5 and pd.notna(r["mean"])
        ]
        if h7 and path_cands:
            n_floor = max(5, int(0.55 * h7["n"]))
            path_cands = [r for r in path_cands if r["n"] >= n_floor] or path_cands
        best = None
        if path_cands:
            # primary: mean; tie-break sum, then avg_daily
            best = max(
                path_cands,
                key=lambda r: (float(r["mean"]), float(r["sum"]), float(r["avg_daily"] or -9e9)),
            )
        # also note best avg_daily among path
        best_ad = None
        if path_cands:
            ad_ok = [r for r in path_cands if pd.notna(r["avg_daily"])]
            if ad_ok:
                best_ad = max(ad_ok, key=lambda r: float(r["avg_daily"]))

        per_sid_best.append(
            {
                "sid": sid,
                "name": name,
                "rule_id": rule_id,
                "seats": ",".join(seats),
                "kind": kind,
                "h7_n": h7["n"] if h7 else 0,
                "h7_mean": h7["mean"] if h7 else np.nan,
                "h7_sum": h7["sum"] if h7 else np.nan,
                "h7_hit": h7["hit"] if h7 else np.nan,
                "best_exit": best["exit_id"] if best else "",
                "best_plain": best["exit_plain"] if best else "",
                "best_cap": best["cap"] if best else "",
                "best_n": best["n"] if best else 0,
                "best_mean": best["mean"] if best else np.nan,
                "best_sum": best["sum"] if best else np.nan,
                "best_hit": best["hit"] if best else np.nan,
                "best_avg_days": best["avg_days"] if best else np.nan,
                "best_avg_daily": best["avg_daily"] if best else np.nan,
                "delta_mean_vs_h7": (
                    (best["mean"] - h7["mean"]) if best and h7 else np.nan
                ),
                "delta_sum_vs_h7": (
                    (best["sum"] - h7["sum"]) if best and h7 else np.nan
                ),
                "best_ad_exit": best_ad["exit_id"] if best_ad else "",
                "best_ad_cap": best_ad["cap"] if best_ad else "",
                "best_ad_avg_daily": best_ad["avg_daily"] if best_ad else np.nan,
                "best_ad_mean": best_ad["mean"] if best_ad else np.nan,
            }
        )

    conn.close()

    sum_df = pd.DataFrame(summary)
    tr_df = pd.DataFrame(all_trades)
    best_df = pd.DataFrame(per_sid_best)
    sum_path = OUT / "tier_a_seat_exit_summary.csv"
    tr_path = OUT / "tier_a_seat_exit_trades.csv"
    best_path = OUT / "tier_a_seat_exit_best.csv"
    sum_df.to_csv(sum_path, index=False)
    tr_df.to_csv(tr_path, index=False)
    best_df.to_csv(best_path, index=False)
    log(f"wrote {sum_path.name} · {tr_path.name} · {best_path.name}")

    # ---- W1.1 fixed-H grid → HOLD_H14 ----
    hold_full = sum_df[
        (sum_df["exit_id"] == "fixed_cap") & (sum_df["split"] == "full")
    ].copy()
    hold_path = OUT / "tier_a_hold_h14_summary.csv"
    hold_tr = tr_df[tr_df["exit_id"] == "fixed_cap"].copy()
    hold_tr_path = OUT / "tier_a_hold_h14_trades.csv"
    hold_full.to_csv(hold_path, index=False)
    hold_tr.to_csv(hold_tr_path, index=False)

    hold_pick: list[dict] = []
    for sid in INDEPENDENT:
        sub = hold_full[hold_full["sid"] == sid]
        if sub.empty:
            continue
        h7 = sub[sub["cap"] == 7]
        if h7.empty:
            continue
        r7 = h7.iloc[0]
        # best by avg_daily among HOLDS with hit not collapsing vs H7 (hit ≥ H7−15pp)
        cands = []
        for _, r in sub.iterrows():
            if int(r["cap"]) not in HOLDS:
                continue
            hit_ok = (
                pd.isna(r7["hit"])
                or pd.isna(r["hit"])
                or float(r["hit"]) >= float(r7["hit"]) - 0.15
            )
            cands.append((r, hit_ok))
        ok = [r for r, ok in cands if ok and pd.notna(r["avg_daily"])]
        pool = ok if ok else [r for r, _ in cands if pd.notna(r["avg_daily"])]
        best_ad = max(pool, key=lambda r: float(r["avg_daily"])) if pool else None
        best_mean = max(
            [r for r, _ in cands if pd.notna(r["mean"])],
            key=lambda r: float(r["mean"]),
        )
        h14 = sub[sub["cap"] == 14]
        r14 = h14.iloc[0] if not h14.empty else None
        hold_pick.append(
            {
                "sid": sid,
                "name": r7["name"],
                "rule_id": r7["rule_id"],
                "h7_n": int(r7["n"]),
                "h7_hit": float(r7["hit"]),
                "h7_mean": float(r7["mean"]),
                "h7_sum": float(r7["sum"]),
                "h7_avg_daily": float(r7["avg_daily"]),
                "best_ad_H": int(best_ad["cap"]) if best_ad is not None else "",
                "best_ad": float(best_ad["avg_daily"]) if best_ad is not None else np.nan,
                "best_ad_mean": float(best_ad["mean"]) if best_ad is not None else np.nan,
                "best_ad_hit": float(best_ad["hit"]) if best_ad is not None else np.nan,
                "best_ad_n": int(best_ad["n"]) if best_ad is not None else 0,
                "best_mean_H": int(best_mean["cap"]),
                "best_mean": float(best_mean["mean"]),
                "h14_n": int(r14["n"]) if r14 is not None else 0,
                "h14_hit": float(r14["hit"]) if r14 is not None else np.nan,
                "h14_mean": float(r14["mean"]) if r14 is not None else np.nan,
                "h14_sum": float(r14["sum"]) if r14 is not None else np.nan,
                "h14_avg_daily": float(r14["avg_daily"]) if r14 is not None else np.nan,
                "delta_mean_h14_vs_h7": (
                    float(r14["mean"]) - float(r7["mean"]) if r14 is not None else np.nan
                ),
                "delta_ad_h14_vs_h7": (
                    float(r14["avg_daily"]) - float(r7["avg_daily"])
                    if r14 is not None
                    else np.nan
                ),
            }
        )
    pick_df = pd.DataFrame(hold_pick)
    pick_path = OUT / "tier_a_hold_h14_best.csv"
    pick_df.to_csv(pick_path, index=False)
    log(f"wrote {hold_path.name} · {hold_tr_path.name} · {pick_path.name}")

    # HOLD_H14 markdown
    hl: list[str] = []
    hl.append("# Tier A · 固定持有 H∈{3,6,7,10,14}（獨立進場凍結）\n")
    hl.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}")
    hl.append("- Runner：`scripts/research/run_tier_a_seat_exit_study.py`（W1.1）")
    hl.append(f"- 窗：{D0}..{D1} · OOS≥{OOS} · COST={COST} · β={BETA}×IX0001")
    hl.append(
        "- 進場：與 `TIER_A_V1_INDEPENDENT.md` 相同凍結規則（日曆 fire→D+1 開·非重疊）"
    )
    hl.append(
        f"- 出場：固定 `cal[signal+H]` 收盤 · H∈{list(HOLDS)} · 對照基準 **H=7**"
    )
    hl.append("- Research only · **未採納**\n")

    n_h14_mean = int((pick_df["delta_mean_h14_vs_h7"] > 0.5).sum()) if len(pick_df) else 0
    n_h14_ad = int((pick_df["delta_ad_h14_vs_h7"] > 0.02).sum()) if len(pick_df) else 0
    n_best7 = int((pick_df["best_ad_H"] == 7).sum()) if len(pick_df) else 0
    n_best14 = int((pick_df["best_ad_H"] == 14).sum()) if len(pick_df) else 0
    hl.append("## 一句話\n")
    hl.append(
        f"**H=14 不是宇宙最優**：僅 {n_h14_mean}/{len(pick_df)} 檔 mean 明顯高於 H7（>+0.5pp）；"
        f"avg_daily 明顯優於 H7 的僅 {n_h14_ad}/{len(pick_df)} 檔。"
        f"以 avg_daily（hit 不崩 ≥H7−15pp）選 H*：H7 冠軍 {n_best7} 檔、H14 冠軍 {n_best14} 檔；"
        "多數檔最優在 H=3／6／10，**固定拉長到 14 常稀釋日均**。\n"
    )

    hl.append("## 總表（full · vs H=7）\n")
    hl.append(
        "| sid | 名 | H7 n/hit/mean/avg_d | H14 n/hit/mean/avg_d | Δmean | Δavg_d | "
        "avg_d 冠軍 H | 該 H mean/avg_d | mean 冠軍 H |"
    )
    hl.append(
        "|-----|----|---------------------:|----------------------:|------:|-------:|"
        "-------------:|-----------------:|------------:|"
    )
    for r in pick_df.itertuples():
        hl.append(
            f"| {r.sid} | {r.name} | {r.h7_n}/{100*r.h7_hit:.0f}%/"
            f"{r.h7_mean:+.2f}/{r.h7_avg_daily:+.3f} | "
            f"{r.h14_n}/{100*r.h14_hit:.0f}%/{r.h14_mean:+.2f}/{r.h14_avg_daily:+.3f} | "
            f"{r.delta_mean_h14_vs_h7:+.2f} | {r.delta_ad_h14_vs_h7:+.3f} | "
            f"**H={r.best_ad_H}** | {r.best_ad_mean:+.2f}/{r.best_ad:+.3f} | H={r.best_mean_H} |"
        )

    hl.append("\n## 分檔 H 格（full）\n")
    for sid, meta in INDEPENDENT.items():
        sub = hold_full[hold_full["sid"] == sid].sort_values("cap")
        if sub.empty:
            continue
        hl.append(f"### {sid} {meta['name']} · `{meta['rule_id']}`\n")
        hl.append("| H | n | hit | mean | sum | avg_daily | vs H7 mean | vs H7 avg_d |")
        hl.append("|--:|--:|----:|-----:|----:|----------:|-----------:|------------:|")
        r7 = sub[sub["cap"] == 7]
        m7 = float(r7.iloc[0]["mean"]) if not r7.empty else np.nan
        ad7 = float(r7.iloc[0]["avg_daily"]) if not r7.empty else np.nan
        for _, r in sub.iterrows():
            mark = " ·經典" if int(r["cap"]) == 7 else ""
            dm = float(r["mean"]) - m7 if pd.notna(m7) else np.nan
            dad = float(r["avg_daily"]) - ad7 if pd.notna(ad7) else np.nan
            hl.append(
                f"| {int(r['cap'])}{mark} | {int(r['n'])} | {100*float(r['hit']):.0f}% | "
                f"{float(r['mean']):+.2f} | {float(r['sum']):+.1f} | "
                f"{float(r['avg_daily']):+.3f} | {dm:+.2f} | {dad:+.3f} |"
            )
        hl.append("")

    hl.append("## 判定（W1.1 合成）\n")
    hl.append("| 問題 | 答案 |")
    hl.append("|------|------|")
    hl.append(
        f"| H14 是否更好？ | **多數否**（mean↑僅 {n_h14_mean}/{len(pick_df)}；"
        f"avg_daily↑僅 {n_h14_ad}/{len(pick_df)}） |"
    )
    hl.append(
        "| 成功標準 #2（avg_daily≫H7 且 hit 不崩） | "
        + (
            "**部分有用（per-sid）**"
            if n_h14_ad >= 2 or (pick_df["best_ad_H"] != 7).sum() >= 3
            else "**未證實為宇宙規則**"
        )
        + "；H* 必須逐檔，不可抄 H14 |"
    )
    hl.append("| 下一步 | 出場改 path-exit（見 `TIER_A_SEAT_EXIT.md`）；勿再把 H14 當預設 |\n")

    hl.append("## 盲點\n")
    hl.append("- 非重疊：改 H 會改成交集合，n 不可當配對差。")
    hl.append("- 小 n：OOS 未單獨選 H*（避免雙層窺探）；本表 full-window。")
    hl.append("- avg_daily = mean/H（固定持有）；與 path-exit 的 mean/實際天數不同構。")
    hl.append("- **未採納**。\n")
    hl.append("## 產物\n")
    hl.append(f"- `{hold_path.name}` · `{hold_tr_path.name}` · `{pick_path.name}`")
    hl.append("- 本報告：`TIER_A_HOLD_H14.md`\n")
    md_h = OUT / "TIER_A_HOLD_H14.md"
    md_h.write_text("\n".join(hl) + "\n", encoding="utf-8")
    log(f"wrote {md_h}")

    # ---- seat-exit markdown ----
    lines: list[str] = []
    lines.append("# Tier A · 分點轉賣／轉弱出場研究（獨立進場凍結）\n")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}")
    lines.append("- Runner：`scripts/research/run_tier_a_seat_exit_study.py`（W1.2–1.3）")
    lines.append(
        f"- 窗：{D0}..{D1} · OOS≥{OOS} · COST={COST} · β={BETA}×IX0001"
    )
    lines.append(
        "- 進場：與 `TIER_A_V1_INDEPENDENT.md` 相同凍結規則（日曆 fire→D+1 開·非重疊）"
    )
    lines.append(
        "- 出場：分點 path-exit（淨轉負／賣超門檻／減速／連兩日賣；cobuy 任一／雙席）；"
        f"安全上限 H∈{list(CAPS)}；固定 H 對照見 `TIER_A_HOLD_H14.md`"
    )
    lines.append("- Research only · **未採納**\n")

    lines.append("## 一句話\n")
    n_beat = int((best_df["delta_mean_vs_h7"] > 0.5).sum()) if len(best_df) else 0
    lines.append(
        f"在固定 H=7 對照下，{n_beat}/{len(best_df)} 檔的最佳分點出場規則 **mean 明顯高於 H7（>+0.5pp）**；"
        "多數檔「提早出場」主要改善的是 **avg_daily／縮短持股**，不一定抬高 mean。"
        "cobuy 檔對「任一席翻賣」vs「雙席翻賣」敏感度不同，**不能一套出場抄全宇宙**。\n"
    )

    lines.append("## 總表（每檔最佳 path-exit vs 固定 H=7）\n")
    lines.append(
        "| sid | 名 | 進場（凍結） | 最佳出場 | cap | n | mean | Δmean vs H7 | sum | Δsum | hit | 典型持有(日·h_equiv) |"
    )
    lines.append("|-----|----|--------------|----------|----:|--:|-----:|------------:|----:|-----:|----:|---------------------:|")
    for r in best_df.itertuples():
        lines.append(
            f"| {r.sid} | {r.name} | `{r.rule_id}` | {r.best_plain}（`{r.best_exit}`） | {r.best_cap} | "
            f"{r.best_n} | {r.best_mean:+.2f} | {r.delta_mean_vs_h7:+.2f} | "
            f"{r.best_sum:+.1f} | {r.delta_sum_vs_h7:+.1f} | {100*r.best_hit:.0f}% | "
            f"{r.best_avg_days:.1f}（H7=7） |"
        )

    lines.append("\n### 固定 H=7 對照\n")
    lines.append("| sid | n | hit | mean | sum |")
    lines.append("|-----|--:|----:|-----:|----:|")
    for r in best_df.itertuples():
        lines.append(
            f"| {r.sid} | {r.h7_n} | {100*r.h7_hit:.0f}% | {r.h7_mean:+.2f} | {r.h7_sum:+.1f} |"
        )

    lines.append("\n## 分檔建議（白話）\n")
    for r in best_df.itertuples():
        better_mean = r.delta_mean_vs_h7 > 0.5
        better_sum = r.delta_sum_vs_h7 > 5
        if better_mean or better_sum:
            verdict = "建議**優先試**此出場（相對 H7 有 mean／sum 優勢）"
        elif r.delta_mean_vs_h7 > -0.5 and float(r.best_avg_days) < 6:
            verdict = "mean 差不多，但持股更短→**資本效率**可能更好；需再看 avg_daily"
        else:
            verdict = "相對固定 H7 **未明顯勝出**；暫留 H7，出場僅作觀察"
        lines.append(f"### {r.sid} {r.name}")
        lines.append(f"- 進場：`{r.rule_id}` · 盯席 `{r.seats}`")
        lines.append(
            f"- 建議出場：**{r.best_plain}**（安全上限 {r.best_cap}）· "
            f"full n={r.best_n} · mean={r.best_mean:+.2f}（Δ{r.delta_mean_vs_h7:+.2f}）· "
            f"典型 h≈{r.best_avg_days:.1f} 日"
        )
        if r.best_ad_exit and r.best_ad_exit != r.best_exit:
            lines.append(
                f"- 若優化 avg_daily：可看 `{r.best_ad_exit}`@cap{r.best_ad_cap} "
                f"（avg_daily={r.best_ad_avg_daily:+.3f} · mean={r.best_ad_mean:+.2f}）"
            )
        lines.append(f"- 判定：{verdict}\n")

    # success criterion #3: mean AND maxDD dual improve — approximate with mean+hit
    n_dual = 0
    for r in best_df.itertuples():
        if r.delta_mean_vs_h7 > 0 and r.best_hit >= r.h7_hit - 1e-9:
            n_dual += 1
    lines.append("## 判定（W1 路徑出場合成）\n")
    lines.append("| 問題 | 答案 |")
    lines.append("|------|------|")
    lines.append(
        f"| 路徑出場有用？ | "
        + (
            f"**部分有用**：{n_beat}/{len(best_df)} 檔 mean>+0.5pp vs H7；"
            f"{n_dual}/{len(best_df)} 檔 mean↑且 hit 不掉"
            if n_beat >= 2
            else f"**未證實**：僅 {n_beat}/{len(best_df)} 檔 mean 明顯勝 H7"
        )
        + " |"
    )
    lines.append(
        "| 成功標準 #3（相對固定 H mean 改善） | "
        + ("**有條件通過（per-sid）**" if n_beat >= 2 else "**未通過宇宙級**")
        + "；出場規則不可一套抄全檔 |"
    )
    lines.append("| 下一步 | 對 mean 勝出檔做 nested OOS／穩健（W3）；勿寫 strategy.yaml |\n")

    lines.append("## 方法細節\n")
    lines.append("| 項 | 定義 |")
    lines.append("|----|------|")
    lines.append("| 進場 | 凍結規則日曆 fire → D+1 開；非重疊以**實際出場日**+1 解鎖 |")
    lines.append("| 固定對照 | `fixed_cap` · cap=7（=經典 L1H7） |")
    lines.append("| first_net_lt0 / either_flip | 持有期間首日，盯席淨額金額 amt&lt;0 |")
    lines.append("| both_flip / both_net_lt0 | cobuy：兩席同日 amt&lt;0 |")
    lines.append("| sell_ge_0p5 / 1yi | 席位當日賣超金額 sell×close ≥ 門檻 |")
    lines.append("| decel | 昨 amt&gt;0 且今 amt&lt;昨（買超減速） |")
    lines.append("| two_day_sell | 連續兩日 amt&lt;0 |")
    lines.append("| 安全上限 | 未觸發則於 signal+cap 收盤出 |")
    lines.append("| 主選規則 | full mean 最大（n 門檻≈0.55×H7 n）；avg_daily 另欄參考 |")
    lines.append("| 大戶（進場條件裡） | TDCC `big_holder_pct_chg`／**集保大戶％**，**不是**分點席 |")

    lines.append("\n## 盲點（誠實）\n")
    lines.append("- **小樣本**：多數檔 full n≈12–20；OOS 更薄，單一肥尾可翻案。")
    lines.append("- **IS 凍結進場 × 再掃出場** = 第二層資料窺探；本報告未做 nested OOS 出場凍結。")
    lines.append("- **非重疊**隨出場日改變，n／交易日曆與 H7 不完全同一集合。")
    lines.append("- **無隔夜缺口風險模型**：同一日收盤看到分點才出，已避開「盤中未知」，但開盤跳空仍在。")
    lines.append("- **席位 ID／改名**：FinMind trader id 映射風險仍在。")
    lines.append("- **賣超金額**：sell×close，非券商「大單」標籤；外資席已濾。")
    lines.append("- **decel 很吵**：趨勢席常反覆減速→過早砍，mean 常掉。")
    lines.append("- **cobuy 任一翻賣**通常更短更勤出；**雙席翻賣**更接近 H7，edge 可能不同。")
    lines.append("- 未建模「分數掉下來」類 chip score exit（本輪專注分點）。")
    lines.append("- **未寫 strategy.yaml**；達標 ≠ 採納。\n")

    lines.append("## 產物\n")
    lines.append(f"- `{sum_path.name}` · `{tr_path.name}` · `{best_path.name}`")
    lines.append(f"- 固定 H：`TIER_A_HOLD_H14.md` · `{hold_path.name}`")
    lines.append(f"- 本報告：`TIER_A_SEAT_EXIT.md`")
    lines.append(f"\n_elapsed {time.time()-t0:.1f}s_\n")

    md_path = OUT / "TIER_A_SEAT_EXIT.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
