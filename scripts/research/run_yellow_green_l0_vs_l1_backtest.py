#!/usr/bin/env python3
"""Yellow∧Green vs Green-only · L0 vs L1 entry backtest (expert-pool tiers).

Research only. Compares:
  - G_L0 / G_L1: every whale consensus (green) day
  - Y∧G_L0 / Y∧G_L1: green only if yellow precursor lit
  - Y_only: yellow without green (anti-baseline)

Convention: COST=0.003 · BETA=1.15 · bench=IX0001 · primary H=7 (expert-pool L1H7).
CLI: --tier A|B|AB (default A).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.adapters_db import connect_ro, load_bench, load_calendar, load_ohlc  # noqa: E402
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402
from research.chen_chip.whale_events import TIER_A, TIER_B, load_whale_events  # noqa: E402

OUT = ROOT / "reports/research/whale_chip_precursor"
CACHE = OUT / "cache"
HS_PATH = CACHE / "holding_shares_per_tier_a.csv"
GOV_PATH = CACHE / "gov_bank_net_tier_a.csv"

TIER_NAMES: dict[str, str] = {
    "2327": "國巨",
    "2303": "聯電",
    "3443": "創意",
    "6669": "緯穎",
    "2383": "台光電",
    "2408": "南亞科",
    "3189": "景碩",
    "3037": "欣興",
    "2337": "旺宏",
    "3481": "群創",
    "3653": "健策",
    "6239": "力成",
    "6223": "旺矽",
    "3665": "貿聯",
    "6515": "穎崴",
}

TIER_CFG: dict[str, dict] = {
    "A": {
        "sids": TIER_A,
        "label": "TierA",
        "title": "expert-pool Tier A",
        "md": "L0_VS_L1_BACKTEST.md",
        "trades": "l0_vs_l1_trades.csv",
        "summary": "l0_vs_l1_summary.csv",
        "status": "status_l0_vs_l1.json",
    },
    "B": {
        "sids": TIER_B,
        "label": "TierB",
        "title": "expert-pool Tier B",
        "md": "L0_VS_L1_BACKTEST_TIER_B.md",
        "trades": "l0_vs_l1_tier_b_trades.csv",
        "summary": "l0_vs_l1_tier_b_summary.csv",
        "status": "status_l0_vs_l1_tier_b.json",
    },
    "AB": {
        "sids": TIER_A + TIER_B,
        "label": "TierAB",
        "title": "expert-pool Tier A+B",
        "md": "L0_VS_L1_BACKTEST_TIER_AB.md",
        "trades": "l0_vs_l1_tier_ab_trades.csv",
        "summary": "l0_vs_l1_tier_ab_summary.csv",
        "status": "status_l0_vs_l1_tier_ab.json",
    },
}

D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
HOLDS = (7, 10)
PRIMARY_HOLD = 7
N_BOOT = 5000
RNG = np.random.default_rng(42)


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


@dataclass(frozen=True)
class YellowRule:
    id: str
    label: str
    lag_mode: str  # "fixed2" | "win5"
    three_streak_min: int = 0
    require_foreign_sum5: bool = False
    require_big_holder_chg: bool = False


RULES: list[YellowRule] = [
    YellowRule("W1_fixed", "W1 T−2 three_streak≥2", "fixed2", three_streak_min=2),
    YellowRule("W1_win5", "W1 any T−1..T−5 three_streak≥2", "win5", three_streak_min=2),
    YellowRule(
        "W3_fixed",
        "W3 T−2 foreign_sum5>0 ∧ three_streak≥2",
        "fixed2",
        three_streak_min=2,
        require_foreign_sum5=True,
    ),
    YellowRule(
        "W5_fixed",
        "W5 T−2 big_holder_pct_chg>0",
        "fixed2",
        require_big_holder_chg=True,
    ),
]


def _shift_forward(arr: np.ndarray, lag: int) -> np.ndarray:
    if lag <= 0:
        return arr.copy()
    out = np.zeros_like(arr)
    out[:, lag:] = arr[:, :-lag]
    return out


def yellow_ready(rule: YellowRule, arr: dict[str, np.ndarray]) -> np.ndarray:
    shape = arr["three_streak"].shape
    base = np.ones(shape, dtype=bool)
    if rule.three_streak_min:
        base &= np.nan_to_num(arr["three_streak"], nan=0.0) >= rule.three_streak_min
    if rule.require_foreign_sum5:
        base &= np.nan_to_num(arr["foreign_sum_5d"], nan=0.0) > 0
    if rule.require_big_holder_chg:
        chg = arr["big_holder_pct_chg"]
        base &= np.isfinite(chg) & (chg > 0)
    if rule.lag_mode == "fixed2":
        return _shift_forward(base, 2)
    out = np.zeros(shape, dtype=bool)
    for k in range(1, 6):
        out |= _shift_forward(base, k)
    return out


def _dense(feat: pd.DataFrame, sids: list[str], cal: list[str]) -> dict[str, np.ndarray]:
    n_s, n_d = len(sids), len(cal)
    sid_ix = {s: i for i, s in enumerate(sids)}
    di = {d: i for i, d in enumerate(cal)}
    cols = ["three_streak", "foreign_sum_5d", "big_holder_pct_chg"]
    arrays = {c: np.full((n_s, n_d), np.nan, dtype=np.float64) for c in cols}
    for r in feat.itertuples(index=False):
        s, d = str(r.sid), str(r.d)
        if s not in sid_ix or d not in di:
            continue
        i, j = sid_ix[s], di[d]
        for c in cols:
            v = getattr(r, c, np.nan)
            try:
                arrays[c][i, j] = float(v) if v is not None and pd.notna(v) else np.nan
            except (TypeError, ValueError):
                arrays[c][i, j] = np.nan
    return arrays


def excess_l0(
    sid: str,
    T: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
) -> tuple[float | None, str | None, str | None]:
    i = di.get(T)
    if i is None or i + hold >= len(cal):
        return None, None, None
    er = ohlc_px.get((sid, T))
    xd = cal[i + hold]
    xr = ohlc_px.get((sid, xd))
    be, bx = bench.get(T), bench.get(xd)
    if not er or not xr or not be or not bx:
        return None, None, None
    _, ep = er
    _, xp = xr
    if ep <= 0 or xp <= 0 or be <= 0 or bx <= 0:
        return None, None, None
    sr = xp / ep - 1.0 - COST
    br = bx / be - 1.0
    return (sr - BETA * br) * 100.0, T, xd


def excess_l1(
    sid: str,
    T: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
) -> tuple[float | None, str | None, str | None]:
    i = di.get(T)
    if i is None or i + 1 >= len(cal) or i + hold >= len(cal):
        return None, None, None
    ed = cal[i + 1]
    xd = cal[i + hold]
    er = ohlc_px.get((sid, ed))
    xr = ohlc_px.get((sid, xd))
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


def max_dd(xs: list[float]) -> float:
    if not xs:
        return float("nan")
    eq = np.cumsum(np.asarray(xs, dtype=float))
    peak = np.maximum.accumulate(eq)
    return float((eq - peak).min())


def summarize(xs: list[float], label: str) -> dict:
    a = np.asarray(xs, dtype=float)
    n = int(a.size)
    if n == 0:
        return {
            "arm": label,
            "n": 0,
            "hit_rate": np.nan,
            "med_excess": np.nan,
            "mean_excess": np.nan,
            "sum_excess": np.nan,
            "max_dd_cumsum": np.nan,
        }
    return {
        "arm": label,
        "n": n,
        "hit_rate": float((a > 0).mean()),
        "med_excess": float(np.median(a)),
        "mean_excess": float(a.mean()),
        "sum_excess": float(a.sum()),
        "max_dd_cumsum": max_dd(xs),
    }


def bootstrap_delta(a: list[float], b: list[float], n_boot: int = N_BOOT) -> dict:
    aa = np.asarray(a, dtype=float)
    bb = np.asarray(b, dtype=float)
    if aa.size < 5 or bb.size < 5:
        return {
            "delta_mean": float(aa.mean() - bb.mean()) if aa.size and bb.size else np.nan,
            "ci_lo": np.nan,
            "ci_hi": np.nan,
            "p_delta_le0": np.nan,
            "n_a": int(aa.size),
            "n_b": int(bb.size),
        }
    d0 = float(aa.mean() - bb.mean())
    idx_a = RNG.integers(0, aa.size, size=(n_boot, aa.size))
    idx_b = RNG.integers(0, bb.size, size=(n_boot, bb.size))
    deltas = aa[idx_a].mean(axis=1) - bb[idx_b].mean(axis=1)
    return {
        "delta_mean": d0,
        "ci_lo": float(np.percentile(deltas, 2.5)),
        "ci_hi": float(np.percentile(deltas, 97.5)),
        "p_delta_le0": float((deltas <= 0).mean()),
        "n_a": int(aa.size),
        "n_b": int(bb.size),
    }


def collect_trades(
    events: list[tuple[str, str]],
    hold: int,
    entry: str,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
    yellow_ok: dict[tuple[str, str], bool] | None = None,
    require_yellow: bool = False,
) -> list[dict]:
    rows: list[dict] = []
    fn = excess_l0 if entry == "L0" else excess_l1
    for sid, T in events:
        if require_yellow and yellow_ok is not None and not yellow_ok.get((sid, T), False):
            continue
        x, ed, xd = fn(sid, T, hold, cal, di, ohlc_px, bench)
        if x is None:
            continue
        rows.append(
            {
                "sid": sid,
                "signal_date": T,
                "entry_date": ed,
                "exit_date": xd,
                "entry": entry,
                "hold": hold,
                "excess_pct": x,
                "split": "IS" if T < OOS else "OOS",
                "yellow_ok": bool(yellow_ok.get((sid, T), False)) if yellow_ok else None,
            }
        )
    return rows


def yellow_only_trades(
    rule: YellowRule,
    ready: np.ndarray,
    whale_m: np.ndarray,
    sids: list[str],
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
    hold: int,
    day_lo: str,
    day_hi: str,
) -> list[dict]:
    rows: list[dict] = []
    for si, sid in enumerate(sids):
        for j, d in enumerate(cal):
            if d < day_lo or d > day_hi:
                continue
            if not ready[si, j] or whale_m[si, j]:
                continue
            x, ed, xd = excess_l0(sid, d, hold, cal, di, ohlc_px, bench)
            if x is None:
                continue
            rows.append(
                {
                    "sid": sid,
                    "signal_date": d,
                    "entry_date": ed,
                    "exit_date": xd,
                    "entry": "L0",
                    "hold": hold,
                    "excess_pct": x,
                    "split": "IS" if d < OOS else "OOS",
                    "yellow_ok": True,
                    "arm": f"Y_only_{rule.id}",
                    "yellow_rule": rule.id,
                }
            )
    return rows


def fmt_pct(v: float | None, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:+.{digits}f}"


def fmt_rate(v: float | None) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{100.0 * v:.0f}%"


def compute_universe(sids: list[str], label: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    log(f"load {label} n_sid={len(sids)}")
    conn = connect_ro()
    whale = load_whale_events(sids)
    whale = whale[(whale["signal_date"] >= D0) & (whale["signal_date"] <= D1)].copy()
    hs = HS_PATH if HS_PATH.exists() else None
    gov = GOV_PATH if GOV_PATH.exists() else None
    feat = build_chip_feature_frame(
        conn, sids, D0, D1, holding_shares_path=hs, gov_bank_path=gov
    )
    cal = load_calendar(conn, D0, "2026-08-31")
    di = {d: i for i, d in enumerate(cal)}
    bench = load_bench(conn, D0, D1)
    ohlc = load_ohlc(conn, sids, D0, D1)
    conn.close()

    ohlc_px = {
        (str(r.sid), str(r.d)): (float(r.open), float(r.close)) for r in ohlc.itertuples()
    }
    events = sorted(
        {
            (str(s), str(t))
            for s, t in zip(whale["sid"].astype(str), whale["signal_date"].astype(str))
            if D0 <= str(t) <= D1 and str(t) in di
        }
    )
    log(f"  events={len(events)} feat={len(feat)}")

    arr = _dense(feat, sids, cal)
    sid_ix = {s: i for i, s in enumerate(sids)}
    whale_m = np.zeros((len(sids), len(cal)), dtype=bool)
    for s, t in events:
        whale_m[sid_ix[s], di[t]] = True

    ready_by_rule = {r.id: yellow_ready(r, arr) for r in RULES}
    yellow_ok_maps = {
        r.id: {
            (s, t): bool(ready_by_rule[r.id][sid_ix[s], di[t]])
            for s, t in events
            if s in sid_ix and t in di
        }
        for r in RULES
    }

    all_trades: list[dict] = []
    summary_rows: list[dict] = []
    primary = RULES[0]

    for hold in HOLDS:
        for entry in ("L0", "L1"):
            tr = collect_trades(events, hold, entry, cal, di, ohlc_px, bench)
            for row in tr:
                row.update({"arm": f"G_{entry}", "yellow_rule": "", "universe": label})
                all_trades.append(row)
            for split in ("full", "IS", "OOS"):
                xs_ = [r["excess_pct"] for r in tr if split == "full" or r["split"] == split]
                sm = summarize(xs_, f"G_{entry}")
                sm.update(
                    {"hold": hold, "split": split, "yellow_rule": "", "universe": label}
                )
                summary_rows.append(sm)

        for rule in RULES:
            yok = yellow_ok_maps[rule.id]
            for entry in ("L0", "L1"):
                tr = collect_trades(
                    events, hold, entry, cal, di, ohlc_px, bench, yok, True
                )
                for row in tr:
                    row.update(
                        {
                            "arm": f"YandG_{entry}",
                            "yellow_rule": rule.id,
                            "universe": label,
                        }
                    )
                    all_trades.append(row)
                for split in ("full", "IS", "OOS"):
                    xs_ = [
                        r["excess_pct"]
                        for r in tr
                        if split == "full" or r["split"] == split
                    ]
                    sm = summarize(xs_, f"YandG_{entry}")
                    sm.update(
                        {
                            "hold": hold,
                            "split": split,
                            "yellow_rule": rule.id,
                            "universe": label,
                        }
                    )
                    summary_rows.append(sm)

        yonly = yellow_only_trades(
            primary,
            ready_by_rule[primary.id],
            whale_m,
            sids,
            cal,
            di,
            ohlc_px,
            bench,
            hold,
            D0,
            D1,
        )
        for row in yonly:
            row["universe"] = label
            all_trades.append(row)
        sm = summarize([r["excess_pct"] for r in yonly], f"Y_only_{primary.id}")
        sm.update(
            {
                "hold": hold,
                "split": "full",
                "yellow_rule": primary.id,
                "universe": label,
            }
        )
        summary_rows.append(sm)

    return pd.DataFrame(all_trades), pd.DataFrame(summary_rows)


def build_report_section(
    label: str,
    trades_df: pd.DataFrame,
    summary_df: pd.DataFrame,
    *,
    heading: str | None = None,
) -> list[str]:
    lines: list[str] = []
    sdf = summary_df[summary_df.universe == label]
    tdf = trades_df[trades_df.universe == label]
    g0 = tdf[(tdf.arm == "G_L0") & (tdf.hold == PRIMARY_HOLD)]
    n_ev = len(g0)
    n_is = int((g0.split == "IS").sum())
    n_oos = int((g0.split == "OOS").sum())
    primary = RULES[0]

    lines.append(f"## {heading or f'主宇宙 · {label}'}\n\n")
    lines.append(f"- 綠燈可交易筆（G_L0 H={PRIMARY_HOLD}）：n={n_ev}（IS {n_is} / OOS {n_oos}）\n")
    lines.append(f"- 黃燈主規則：**{primary.label}**\n\n")

    for hold in HOLDS:
        lines.append(
            f"### H={hold}"
            + (" · 主持有（expert-pool L1H7）" if hold == PRIMARY_HOLD else " · 敏感度（前兆慣用 H10）")
            + "\n\n"
        )
        lines.append(
            "| arm | yellow | split | n | hit | med% | mean% | sum% | maxDD |\n"
            "|-----|--------|-------|--:|----:|-----:|------:|-----:|------:|\n"
        )
        for arm, rid in [
            ("G_L0", ""),
            ("G_L1", ""),
            ("YandG_L0", primary.id),
            ("YandG_L1", primary.id),
            (f"Y_only_{primary.id}", primary.id),
        ]:
            for split in ("full", "IS", "OOS"):
                if arm.startswith("Y_only") and split != "full":
                    continue
                m = sdf[
                    (sdf.arm == arm)
                    & (sdf.hold == hold)
                    & (sdf.split == split)
                    & (sdf.yellow_rule.fillna("") == rid)
                ]
                if m.empty:
                    continue
                r = m.iloc[0]
                lines.append(
                    f"| {arm} | {rid or '—'} | {split} | {int(r.n)} | {fmt_rate(r.hit_rate)} | "
                    f"{fmt_pct(r.med_excess)} | {fmt_pct(r.mean_excess)} | "
                    f"{fmt_pct(r.sum_excess, 0)} | {fmt_pct(r.max_dd_cumsum, 0)} |\n"
                )
        lines.append("\n")

        lines.append(f"**Y∧G vs G · Δmean（{primary.label}）· bootstrap {N_BOOT}**\n\n")
        lines.append("| contrast | split | Δmean pp | 95% CI | P(Δ≤0) | n_filt / n_base |\n")
        lines.append("|----------|-------|---------:|-------:|-------:|----------------:|\n")
        for entry in ("L0", "L1"):
            for split in ("full", "IS", "OOS"):
                ya = tdf[
                    (tdf.arm == f"YandG_{entry}")
                    & (tdf.hold == hold)
                    & (tdf.yellow_rule == primary.id)
                    & ((split == "full") | (tdf.split == split))
                ]["excess_pct"].tolist()
                gb = tdf[
                    (tdf.arm == f"G_{entry}")
                    & (tdf.hold == hold)
                    & ((split == "full") | (tdf.split == split))
                ]["excess_pct"].tolist()
                boot = bootstrap_delta(ya, gb)
                p = "—" if np.isnan(boot["p_delta_le0"]) else f"{boot['p_delta_le0']:.2f}"
                lines.append(
                    f"| Y∧G_{entry} − G_{entry} | {split} | {fmt_pct(boot['delta_mean'])} | "
                    f"[{fmt_pct(boot['ci_lo'])}, {fmt_pct(boot['ci_hi'])}] | {p} | "
                    f"{boot['n_a']} / {boot['n_b']} |\n"
                )
        lines.append("\n")

        lines.append("**L0 vs L1 · Δmean（同 filter）**\n\n")
        lines.append("| contrast | split | Δmean pp | 95% CI | P(Δ≤0) | n_L0 / n_L1 |\n")
        lines.append("|----------|-------|---------:|-------:|-------:|------------:|\n")
        for filt_arm, rid in [("G", ""), ("YandG", primary.id)]:
            for split in ("full", "IS", "OOS"):
                a0 = tdf[
                    (tdf.arm == f"{filt_arm}_L0")
                    & (tdf.hold == hold)
                    & (tdf.yellow_rule.fillna("") == rid)
                    & ((split == "full") | (tdf.split == split))
                ]["excess_pct"].tolist()
                a1 = tdf[
                    (tdf.arm == f"{filt_arm}_L1")
                    & (tdf.hold == hold)
                    & (tdf.yellow_rule.fillna("") == rid)
                    & ((split == "full") | (tdf.split == split))
                ]["excess_pct"].tolist()
                boot = bootstrap_delta(a0, a1)
                p = "—" if np.isnan(boot["p_delta_le0"]) else f"{boot['p_delta_le0']:.2f}"
                lines.append(
                    f"| {filt_arm}_L0 − {filt_arm}_L1 | {split} | {fmt_pct(boot['delta_mean'])} | "
                    f"[{fmt_pct(boot['ci_lo'])}, {fmt_pct(boot['ci_hi'])}] | {p} | "
                    f"{boot['n_a']} / {boot['n_b']} |\n"
                )
        lines.append("\n")

    hold = PRIMARY_HOLD
    lines.append(f"### 黃燈敏感度（full · H={hold} · L0）\n\n")
    lines.append("| yellow | n | hit | med% | mean% | sum% | Δmean vs G_L0 | P(Δ≤0) |\n")
    lines.append("|--------|--:|----:|-----:|------:|-----:|--------------:|-------:|\n")
    g_base = tdf[(tdf.arm == "G_L0") & (tdf.hold == hold)]["excess_pct"].tolist()
    for rule in RULES:
        m = sdf[
            (sdf.arm == "YandG_L0")
            & (sdf.hold == hold)
            & (sdf.split == "full")
            & (sdf.yellow_rule == rule.id)
        ]
        if m.empty:
            continue
        r = m.iloc[0]
        ya = tdf[
            (tdf.arm == "YandG_L0")
            & (tdf.hold == hold)
            & (tdf.yellow_rule == rule.id)
        ]["excess_pct"].tolist()
        boot = bootstrap_delta(ya, g_base)
        p = "—" if np.isnan(boot["p_delta_le0"]) else f"{boot['p_delta_le0']:.2f}"
        lines.append(
            f"| {rule.id} | {int(r.n)} | {fmt_rate(r.hit_rate)} | {fmt_pct(r.med_excess)} | "
            f"{fmt_pct(r.mean_excess)} | {fmt_pct(r.sum_excess, 0)} | "
            f"{fmt_pct(boot['delta_mean'])} | {p} |\n"
        )
    lines.append("\n")
    return lines


def is_significant(boot: dict) -> bool:
    """Require CI>0, or (|Δ|≥1pp & P≤0.10 & n_filt≥20). Refuse if n_filt<15."""
    if np.isnan(boot.get("ci_lo", np.nan)) or boot["n_a"] < 15:
        return False
    if boot["ci_lo"] > 0:
        return True
    return (
        abs(boot["delta_mean"]) >= 1.0
        and boot["p_delta_le0"] <= 0.10
        and boot["n_a"] >= 20
    )


def _xs_from(
    tdf: pd.DataFrame,
    arm: str,
    hold: int,
    rid: str = "",
    split: str = "full",
) -> list[float]:
    return tdf[
        (tdf.arm == arm)
        & (tdf.hold == hold)
        & (tdf.yellow_rule.fillna("") == rid)
        & ((split == "full") | (tdf.split == split))
    ]["excess_pct"].tolist()


def primary_boots(tdf: pd.DataFrame) -> dict[str, dict]:
    hold = PRIMARY_HOLD
    primary = RULES[0]
    g0 = _xs_from(tdf, "G_L0", hold)
    g1 = _xs_from(tdf, "G_L1", hold)
    y0 = _xs_from(tdf, "YandG_L0", hold, primary.id)
    y1 = _xs_from(tdf, "YandG_L1", hold, primary.id)
    yo = _xs_from(tdf, f"Y_only_{primary.id}", hold, primary.id)
    return {
        "g0": summarize(g0, ""),
        "g1": summarize(g1, ""),
        "y0": summarize(y0, ""),
        "y1": summarize(y1, ""),
        "yo": summarize(yo, ""),
        "boot_yg": bootstrap_delta(y0, g0),
        "boot_l0l1_g": bootstrap_delta(g0, g1),
        "boot_l0l1_y": bootstrap_delta(y0, y1),
        "boot_yg_l1": bootstrap_delta(y1, g1),
    }


def verdict_block(trades_df: pd.DataFrame, label: str) -> list[str]:
    tdf = trades_df[trades_df.universe == label]
    hold = PRIMARY_HOLD
    m = primary_boots(tdf)
    boot_yg = m["boot_yg"]
    sg0, sg1 = m["g0"], m["g1"]
    sy0, sy1, syo = m["y0"], m["y1"], m["yo"]
    boot_l0l1_y, boot_l0l1_g, boot_yg_l1 = m["boot_l0l1_y"], m["boot_l0l1_g"], m["boot_yg_l1"]
    sig_yg = is_significant(boot_yg)

    lines = ["## 結論（先讀）\n\n"]
    if sig_yg:
        lines.append(
            f"**相對「無黃燈、只跟綠燈」：有證據顯示提升**"
            f"（W1 · Y∧G_L0 vs G_L0 · H={hold} · Δmean={fmt_pct(boot_yg['delta_mean'])} pp · "
            f"95%CI [{fmt_pct(boot_yg['ci_lo'])}, {fmt_pct(boot_yg['ci_hi'])}] · "
            f"P(Δ≤0)={boot_yg['p_delta_le0']:.2f} · n={boot_yg['n_a']}/{boot_yg['n_b']}）。\n\n"
        )
    else:
        p = "—" if np.isnan(boot_yg["p_delta_le0"]) else f"{boot_yg['p_delta_le0']:.2f}"
        lines.append(
            f"**相對「無黃燈、只跟綠燈」：尚不能稱為顯著提升**"
            f"（W1 · Y∧G_L0 vs G_L0 · H={hold} · Δmean={fmt_pct(boot_yg['delta_mean'])} pp · "
            f"95%CI [{fmt_pct(boot_yg['ci_lo'])}, {fmt_pct(boot_yg['ci_hi'])}] · "
            f"P(Δ≤0)={p} · n={boot_yg['n_a']}/{boot_yg['n_b']}）。"
            f"效果方向／幅度見下表；n 偏小或 CI 跨 0 時不做「顯著」宣稱。\n\n"
        )

    lines.append(
        f"- 黃∧綠、L0（我們的 T）：n={sy0['n']} · hit={fmt_rate(sy0['hit_rate'])} · "
        f"med={fmt_pct(sy0['med_excess'])}% · mean={fmt_pct(sy0['mean_excess'])}% · "
        f"sum={fmt_pct(sy0['sum_excess'], 0)}%\n"
    )
    lines.append(
        f"- 只綠、L0：n={sg0['n']} · hit={fmt_rate(sg0['hit_rate'])} · "
        f"med={fmt_pct(sg0['med_excess'])}% · mean={fmt_pct(sg0['mean_excess'])}% · "
        f"sum={fmt_pct(sg0['sum_excess'], 0)}%\n"
    )
    lines.append(
        f"- 黃∧綠、L1（大戶 T+1）：n={sy1['n']} · hit={fmt_rate(sy1['hit_rate'])} · "
        f"med={fmt_pct(sy1['med_excess'])}% · mean={fmt_pct(sy1['mean_excess'])}% · "
        f"sum={fmt_pct(sy1['sum_excess'], 0)}%\n"
    )
    lines.append(
        f"- 只綠、L1：n={sg1['n']} · hit={fmt_rate(sg1['hit_rate'])} · "
        f"med={fmt_pct(sg1['med_excess'])}% · mean={fmt_pct(sg1['mean_excess'])}% · "
        f"sum={fmt_pct(sg1['sum_excess'], 0)}%\n"
    )
    lines.append(
        f"- 只黃不綠（反例）：n={syo['n']} · mean={fmt_pct(syo['mean_excess'])}% "
        f"（應明顯弱於綠燈臂）\n\n"
    )
    p_y = "—" if np.isnan(boot_l0l1_y["p_delta_le0"]) else f"{boot_l0l1_y['p_delta_le0']:.2f}"
    p_g = "—" if np.isnan(boot_l0l1_g["p_delta_le0"]) else f"{boot_l0l1_g['p_delta_le0']:.2f}"
    p_yl1 = "—" if np.isnan(boot_yg_l1["p_delta_le0"]) else f"{boot_yg_l1['p_delta_le0']:.2f}"
    lines.append(
        f"- L0−L1（Y∧G）：Δmean={fmt_pct(boot_l0l1_y['delta_mean'])} pp · P(Δ≤0)={p_y}\n"
    )
    lines.append(
        f"- L0−L1（只綠）：Δmean={fmt_pct(boot_l0l1_g['delta_mean'])} pp · P(Δ≤0)={p_g}\n"
    )
    lines.append(
        f"- Y∧G_L1−G_L1：Δmean={fmt_pct(boot_yg_l1['delta_mean'])} pp · P(Δ≤0)={p_yl1}\n\n"
    )
    lines.append(
        "解讀：L0 vs L1 以點估計與 IS/OOS 穩定度為主；"
        "僅當 bootstrap CI 明顯排除 0 且 n 足夠才稱「顯著」。\n\n"
    )
    return lines


def universe_roster(sids: list[str]) -> list[str]:
    lines = ["## 宇宙標的\n\n", "| sid | 名稱 | 來源 |\n", "|-----|------|------|\n"]
    for s in sids:
        name = TIER_NAMES.get(s, "—")
        src = "TIER_A" if s in TIER_A else ("TIER_B" if s in TIER_B else "—")
        lines.append(f"| {s} | {name} | `{src}` |\n")
    lines.append(
        "\n交叉核對：`reports/research/branch-footprint-screen/expert_pool/knowledge/TIER_NOTES.md` "
        "· B 檔=共識池、超額仍佳（hard 2～3）。\n\n"
    )
    return lines


def compare_to_tier_a(trades_b: pd.DataFrame) -> list[str]:
    """Side-by-side vs prior Tier A numbers (reuse CSV if present; do not re-claim sig)."""
    lines = ["## 對照 · Tier A（既有結果，不重跑）\n\n"]
    a_trades_path = OUT / "l0_vs_l1_trades.csv"
    if not a_trades_path.exists():
        lines.append("- 找不到 `l0_vs_l1_trades.csv`，略過對照。\n\n")
        return lines

    a_all = pd.read_csv(a_trades_path)
    if "universe" not in a_all.columns:
        lines.append("- Tier A trades 缺 `universe` 欄，略過對照。\n\n")
        return lines
    tda = a_all[a_all.universe == "TierA"]
    if tda.empty:
        lines.append("- Tier A trades 無 `TierA` 列，略過對照。\n\n")
        return lines

    tdb = trades_b[trades_b.universe == "TierB"]
    ma, mb = primary_boots(tda), primary_boots(tdb)
    hold = PRIMARY_HOLD

    lines.append(
        f"來源：既有 `{a_trades_path.name}`（與 `L0_VS_L1_BACKTEST.md` 同規約 · H={hold} · W1）。"
        "下列僅並列點估計／n／P；**不因對照而宣稱跨宇宙顯著**。\n\n"
    )
    lines.append(
        "| metric | Tier A | Tier B |\n"
        "|--------|-------:|-------:|\n"
    )
    rows = [
        ("G_L0 n", ma["g0"]["n"], mb["g0"]["n"]),
        ("Y∧G_L0 n", ma["y0"]["n"], mb["y0"]["n"]),
        ("G_L0 mean%", fmt_pct(ma["g0"]["mean_excess"]), fmt_pct(mb["g0"]["mean_excess"])),
        ("Y∧G_L0 mean%", fmt_pct(ma["y0"]["mean_excess"]), fmt_pct(mb["y0"]["mean_excess"])),
        ("G_L0 hit", fmt_rate(ma["g0"]["hit_rate"]), fmt_rate(mb["g0"]["hit_rate"])),
        ("Y∧G_L0 hit", fmt_rate(ma["y0"]["hit_rate"]), fmt_rate(mb["y0"]["hit_rate"])),
        (
            "Y∧G−G Δmean (L0)",
            fmt_pct(ma["boot_yg"]["delta_mean"]),
            fmt_pct(mb["boot_yg"]["delta_mean"]),
        ),
        (
            "Y∧G−G P(Δ≤0)",
            "—"
            if np.isnan(ma["boot_yg"]["p_delta_le0"])
            else f"{ma['boot_yg']['p_delta_le0']:.2f}",
            "—"
            if np.isnan(mb["boot_yg"]["p_delta_le0"])
            else f"{mb['boot_yg']['p_delta_le0']:.2f}",
        ),
        (
            "G L0−L1 Δmean",
            fmt_pct(ma["boot_l0l1_g"]["delta_mean"]),
            fmt_pct(mb["boot_l0l1_g"]["delta_mean"]),
        ),
        (
            "G L0−L1 P(Δ≤0)",
            "—"
            if np.isnan(ma["boot_l0l1_g"]["p_delta_le0"])
            else f"{ma['boot_l0l1_g']['p_delta_le0']:.2f}",
            "—"
            if np.isnan(mb["boot_l0l1_g"]["p_delta_le0"])
            else f"{mb['boot_l0l1_g']['p_delta_le0']:.2f}",
        ),
    ]
    for name, va, vb in rows:
        lines.append(f"| {name} | {va} | {vb} |\n")

    lines.append("\n")
    lines.append(
        f"- Tier A 原 verdict：Y∧G 對 G **非**顯著提升"
        f"（Δmean={fmt_pct(ma['boot_yg']['delta_mean'])} · "
        f"n={ma['boot_yg']['n_a']}/{ma['boot_yg']['n_b']}）。\n"
    )
    sig_b = is_significant(mb["boot_yg"])
    lines.append(
        f"- Tier B 本跑：Y∧G 對 G "
        f"{'達門檻（仍建議獨立複核）' if sig_b else '**同樣不能**稱顯著提升'}"
        f"（Δmean={fmt_pct(mb['boot_yg']['delta_mean'])} · "
        f"n={mb['boot_yg']['n_a']}/{mb['boot_yg']['n_b']}）。\n\n"
    )
    return lines


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--tier",
        choices=("A", "B", "AB"),
        default="A",
        help="Universe: A=TierA, B=TierB, AB=A+B combined (default A)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = TIER_CFG[args.tier]
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)

    sids = list(cfg["sids"])
    label = str(cfg["label"])
    trades, summary = compute_universe(sids, label)

    trades_path = OUT / str(cfg["trades"])
    summary_path = OUT / str(cfg["summary"])
    trades.to_csv(trades_path, index=False)
    summary.to_csv(summary_path, index=False)

    md: list[str] = []
    md.append(f"# 黃∧綠 · L0 vs L1 兩年回測（{cfg['title']}）\n\n")
    md.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}\n")
    md.append(
        "- Runner：`scripts/research/run_yellow_green_l0_vs_l1_backtest.py "
        f"--tier {args.tier}`\n"
    )
    md.append(
        f"- 窗：{D0}..{D1}（約 2y）· IS/OOS 切在 {OOS} · Research only · **未採納**\n"
    )
    md.append(
        f"- 成本 {COST} · β={BETA}×IX0001 · 合計指標=**sum of trade excesses**（非複利）\n"
    )
    md.append(
        f"- 主規則黃燈：{RULES[0].label}；出場 H∈{list(HOLDS)}（主 H={PRIMARY_HOLD}）\n"
    )
    md.append(
        "- 註：`holding_shares` / `gov_bank` cache 目前僅 Tier A；"
        "W1/W3 用 DB 籌碼（three_streak／foreign），W5（big_holder）在 Tier B 可能無覆蓋。\n\n"
    )
    md.extend(universe_roster(sids))
    md.extend(verdict_block(trades, label))
    if args.tier == "B":
        md.extend(compare_to_tier_a(trades))
    md.extend(
        build_report_section(
            label,
            trades,
            summary,
            heading=f"主宇宙 · {label}",
        )
    )
    md.append("## 方法備註\n\n")
    md.append("- 綠燈 = expert-pool knowledge `trades/{sid}.json` 的 `signal_date`。\n")
    md.append("- L0：訊號日收盤進；L1：下一交易日開盤進（knowledge protocol `L1H7`）。\n")
    md.append("- 出場：日曆 T+H 收盤（L0/L1 同日出場，便於比價差）。\n")
    md.append("- Bootstrap：兩臂交易超額重抽 mean 差；P(Δ≤0)≈單側「無提升」機率。\n")
    md.append("- 黃燈不可單獨開倉；Y_only 僅作反例。\n")
    md.append(
        f"- 顯著門檻（與 Tier A 同）：CI 下沿>0，或 |Δ|≥1pp 且 P≤0.10 且 n_filt≥20；"
        f"n_filt<15 一律拒稱顯著。\n"
    )
    md.append(f"- 產物：`{trades_path.name}` · `{summary_path.name}`\n")

    out_md = OUT / str(cfg["md"])
    out_md.write_text("".join(md), encoding="utf-8")
    status = {
        "phase": "done",
        "tier": args.tier,
        "label": label,
        "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_min": round((time.time() - t0) / 60, 2),
        "trades": int(len(trades)),
        "n_sid": len(sids),
        "out": str(out_md),
    }
    (OUT / str(cfg["status"])).write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"DONE tier={args.tier} → {out_md} ({status['elapsed_min']} min)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
