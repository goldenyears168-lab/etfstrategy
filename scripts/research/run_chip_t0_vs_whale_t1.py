#!/usr/bin/env python3
"""Chip_T0 (standalone yellow) vs Whale_T+1 (green follow) — fair compare.

Research only. No whale required for Chip_T0.
  - Whale_T+1: expert-pool green day T → enter L1 (next open)
  - Chip_T0: yellow rule true on day D → enter D close (L0); non-overlap per sid
  - Chip_T1 (optional): yellow on D → enter D+1 open (still earlier than waiting whale)

Yellow on D itself (not precursor lag before whale):
  W1: three_streak≥2
  W3: foreign_sum_5d>0 ∧ three_streak≥2
  W5: big_holder_pct_chg>0

Window / cost / β reused from run_yellow_green_l0_vs_l1_backtest.py.
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

D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
HOLDS = (7, 10)
PRIMARY_HOLD = 7
N_BOOT = 5000
RNG = np.random.default_rng(42)
YEARS = (pd.Timestamp(D1) - pd.Timestamp(D0)).days / 365.25

TIER_CFG: dict[str, dict] = {
    "A": {"sids": TIER_A, "label": "TierA", "title": "Tier A"},
    "B": {"sids": TIER_B, "label": "TierB", "title": "Tier B"},
    "AB": {"sids": TIER_A + TIER_B, "label": "TierAB", "title": "Tier A+B"},
}


@dataclass(frozen=True)
class ChipRule:
    id: str
    label: str
    three_streak_min: int = 0
    require_foreign_sum5: bool = False
    require_big_holder_chg: bool = False


RULES: list[ChipRule] = [
    ChipRule("W1", "W1 three_streak≥2 @D", three_streak_min=2),
    ChipRule(
        "W3",
        "W3 foreign_sum5>0 ∧ three_streak≥2 @D",
        three_streak_min=2,
        require_foreign_sum5=True,
    ),
    ChipRule("W5", "W5 big_holder_pct_chg>0 @D", require_big_holder_chg=True),
]


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def chip_ready(rule: ChipRule, arr: dict[str, np.ndarray]) -> np.ndarray:
    """True on day D when chip condition holds (no lag shift)."""
    shape = arr["three_streak"].shape
    base = np.ones(shape, dtype=bool)
    if rule.three_streak_min:
        base &= np.nan_to_num(arr["three_streak"], nan=0.0) >= rule.three_streak_min
    if rule.require_foreign_sum5:
        base &= np.nan_to_num(arr["foreign_sum_5d"], nan=0.0) > 0
    if rule.require_big_holder_chg:
        chg = arr["big_holder_pct_chg"]
        base &= np.isfinite(chg) & (chg > 0)
    return base


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
            "trades_per_year": np.nan,
        }
    return {
        "arm": label,
        "n": n,
        "hit_rate": float((a > 0).mean()),
        "med_excess": float(np.median(a)),
        "mean_excess": float(a.mean()),
        "sum_excess": float(a.sum()),
        "max_dd_cumsum": max_dd(xs),
        "trades_per_year": float(n / YEARS) if YEARS > 0 else np.nan,
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


def is_significant_worse(boot: dict) -> bool:
    """Chip worse than whale: CI_hi < 0, or |Δ|≥1pp & P(Δ≤0)≥0.90 & n≥20."""
    if np.isnan(boot.get("ci_hi", np.nan)) or boot["n_a"] < 15 or boot["n_b"] < 15:
        return False
    if boot["ci_hi"] < 0:
        return True
    return (
        boot["delta_mean"] <= -1.0
        and boot["p_delta_le0"] >= 0.90
        and boot["n_a"] >= 20
        and boot["n_b"] >= 20
    )


def is_significant_better(boot: dict) -> bool:
    if np.isnan(boot.get("ci_lo", np.nan)) or boot["n_a"] < 15 or boot["n_b"] < 15:
        return False
    if boot["ci_lo"] > 0:
        return True
    return (
        boot["delta_mean"] >= 1.0
        and boot["p_delta_le0"] <= 0.10
        and boot["n_a"] >= 20
        and boot["n_b"] >= 20
    )


def whale_t1_trades(
    events: list[tuple[str, str]],
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
) -> list[dict]:
    rows: list[dict] = []
    for sid, T in events:
        x, ed, xd = excess_l1(sid, T, hold, cal, di, ohlc_px, bench)
        if x is None:
            continue
        rows.append(
            {
                "sid": sid,
                "signal_date": T,
                "entry_date": ed,
                "exit_date": xd,
                "arm": "Whale_T+1",
                "chip_rule": "",
                "hold": hold,
                "excess_pct": x,
                "split": "IS" if T < OOS else "OOS",
            }
        )
    return rows


def chip_trades(
    rule: ChipRule,
    ready: np.ndarray,
    sids: list[str],
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
    hold: int,
    entry_mode: str,
    day_lo: str,
    day_hi: str,
) -> list[dict]:
    """Non-overlapping per sid: skip while prior hold window still open."""
    rows: list[dict] = []
    arm = "Chip_T0" if entry_mode == "T0" else "Chip_T1"
    for si, sid in enumerate(sids):
        next_free_j = -1
        for j, d in enumerate(cal):
            if d < day_lo or d > day_hi:
                continue
            if not ready[si, j]:
                continue
            if j < next_free_j:
                continue
            if entry_mode == "T0":
                x, ed, xd = excess_l0(sid, d, hold, cal, di, ohlc_px, bench)
            else:
                x, ed, xd = excess_l1(sid, d, hold, cal, di, ohlc_px, bench)
            if x is None or ed is None or xd is None:
                continue
            exit_j = di.get(xd)
            if exit_j is None:
                continue
            # next free = day after exit (non-overlapping hold window)
            next_free_j = exit_j + 1
            rows.append(
                {
                    "sid": sid,
                    "signal_date": d,
                    "entry_date": ed,
                    "exit_date": xd,
                    "arm": arm,
                    "chip_rule": rule.id,
                    "hold": hold,
                    "excess_pct": x,
                    "split": "IS" if d < OOS else "OOS",
                }
            )
    return rows


def day_stats(
    ready: np.ndarray,
    whale_m: np.ndarray,
    sids: list[str],
    cal: list[str],
    day_lo: str,
    day_hi: str,
) -> dict:
    chip_days = 0
    whale_days = 0
    both = 0
    for si, _sid in enumerate(sids):
        for j, d in enumerate(cal):
            if d < day_lo or d > day_hi:
                continue
            c = bool(ready[si, j])
            w = bool(whale_m[si, j])
            if c:
                chip_days += 1
            if w:
                whale_days += 1
            if c and w:
                both += 1
    return {
        "chip_fire_days": chip_days,
        "whale_days": whale_days,
        "both_days": both,
        "chip_per_whale": (chip_days / whale_days) if whale_days else np.nan,
    }


def fmt_pct(v: float | None, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:+.{digits}f}"


def fmt_rate(v: float | None) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{100.0 * v:.0f}%"


def fmt_n(v: float | None, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "—"
    return f"{v:.{digits}f}"


def xs_of(
    tdf: pd.DataFrame,
    arm: str,
    hold: int,
    rule: str = "",
    split: str = "full",
) -> list[float]:
    m = (tdf.arm == arm) & (tdf.hold == hold) & (tdf.chip_rule.fillna("") == rule)
    if split != "full":
        m &= tdf.split == split
    return tdf.loc[m, "excess_pct"].tolist()


def run_universe(sids: list[str], label: str) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
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
    log(f"  whale_events={len(events)} feat_rows={len(feat)}")

    arr = _dense(feat, sids, cal)
    sid_ix = {s: i for i, s in enumerate(sids)}
    whale_m = np.zeros((len(sids), len(cal)), dtype=bool)
    for s, t in events:
        whale_m[sid_ix[s], di[t]] = True

    ready_by = {r.id: chip_ready(r, arr) for r in RULES}

    all_trades: list[dict] = []
    summary_rows: list[dict] = []
    day_rows: list[dict] = []

    for rule in RULES:
        ds = day_stats(ready_by[rule.id], whale_m, sids, cal, D0, D1)
        day_rows.append(
            {
                "universe": label,
                "chip_rule": rule.id,
                **ds,
            }
        )

    for hold in HOLDS:
        wt = whale_t1_trades(events, hold, cal, di, ohlc_px, bench)
        for row in wt:
            row["universe"] = label
            all_trades.append(row)
        for split in ("full", "IS", "OOS"):
            xs = [r["excess_pct"] for r in wt if split == "full" or r["split"] == split]
            sm = summarize(xs, "Whale_T+1")
            sm.update(
                {
                    "hold": hold,
                    "split": split,
                    "chip_rule": "",
                    "universe": label,
                }
            )
            summary_rows.append(sm)

        for rule in RULES:
            for mode in ("T0", "T1"):
                tr = chip_trades(
                    rule,
                    ready_by[rule.id],
                    sids,
                    cal,
                    di,
                    ohlc_px,
                    bench,
                    hold,
                    mode,
                    D0,
                    D1,
                )
                for row in tr:
                    row["universe"] = label
                    all_trades.append(row)
                arm = "Chip_T0" if mode == "T0" else "Chip_T1"
                for split in ("full", "IS", "OOS"):
                    xs = [
                        r["excess_pct"]
                        for r in tr
                        if split == "full" or r["split"] == split
                    ]
                    sm = summarize(xs, arm)
                    sm.update(
                        {
                            "hold": hold,
                            "split": split,
                            "chip_rule": rule.id,
                            "universe": label,
                        }
                    )
                    summary_rows.append(sm)

    return pd.DataFrame(all_trades), pd.DataFrame(summary_rows), pd.DataFrame(day_rows)


def verdict_zh(
    tdf: pd.DataFrame,
    primary_rule: ChipRule,
    hold: int = PRIMARY_HOLD,
) -> tuple[str, dict]:
    chip = xs_of(tdf, "Chip_T0", hold, primary_rule.id, "full")
    whale = xs_of(tdf, "Whale_T+1", hold, "", "full")
    boot = bootstrap_delta(chip, whale)
    sc = summarize(chip, "Chip_T0")
    sw = summarize(whale, "Whale_T+1")

    better = is_significant_better(boot)
    worse = is_significant_worse(boot)
    if better:
        word = "更好"
    elif worse:
        word = "更差"
    else:
        # soft similarity: |Δmean| < 1pp or CI crosses 0 with modest |Δ|
        if abs(boot["delta_mean"]) < 1.0 or (
            not np.isnan(boot["ci_lo"]) and boot["ci_lo"] <= 0 <= boot["ci_hi"]
        ):
            word = "差不多"
        else:
            word = "更差" if boot["delta_mean"] < 0 else "更好"

    return word, {"boot": boot, "chip": sc, "whale": sw, "word": word}


def build_md(
    trades: pd.DataFrame,
    summary: pd.DataFrame,
    days: pd.DataFrame,
    *,
    primary_label: str,
    appendix_labels: list[str],
) -> str:
    lines: list[str] = []
    primary = RULES[0]
    tdf = trades[trades.universe == primary_label]
    word, meta = verdict_zh(tdf, primary)
    boot = meta["boot"]
    sc, sw = meta["chip"], meta["whale"]

    lines.append("# 只靠籌碼 T0 vs 跟大戶 T+1\n\n")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}\n")
    lines.append("- Runner：`scripts/research/run_chip_t0_vs_whale_t1.py`\n")
    lines.append(
        f"- 窗：{D0}..{D1}（≈{YEARS:.2f}y）· IS/OOS 切在 {OOS} · Research only · **未採納**\n"
    )
    lines.append(
        f"- 成本 {COST} · β={BETA}×IX0001 · 主 H={PRIMARY_HOLD}（附 H=10）· "
        "合計=sum of trade excesses（非複利）\n"
    )
    lines.append(
        "- Chip 黃燈 = **當日 D 條件成立**（非大戶前 T−2）；"
        "持倉政策 = **同股非重疊**（仍在持倉窗內跳過新訊號）\n"
    )
    lines.append(
        "- Whale_T+1 = expert-pool 綠燈日 T → **L1 開盤**進場（不看籌碼）\n\n"
    )

    lines.append("## 結論（先讀）\n\n")
    p = "—" if np.isnan(boot["p_delta_le0"]) else f"{boot['p_delta_le0']:.2f}"
    lines.append(
        f"**只靠籌碼 T0 相對跟大戶 T+1：{word}**"
        f"（主規則 {primary.id} · H={PRIMARY_HOLD} · "
        f"Δmean Chip−Whale = {fmt_pct(boot['delta_mean'])} pp · "
        f"95%CI [{fmt_pct(boot['ci_lo'])}, {fmt_pct(boot['ci_hi'])}] · "
        f"P(Δ≤0)={p} · n_chip={boot['n_a']} / n_whale={boot['n_b']}）。\n\n"
    )
    if word == "更差":
        lines.append(
            "效果量與方向清楚偏向大戶臂；籌碼單獨開倉**不能**取代跟大戶 T+1。\n\n"
        )
    elif word == "更好":
        lines.append(
            "本窗點估計與 bootstrap 支持籌碼 T0 優於大戶 T+1；仍屬 Research，未採納。\n\n"
        )
    else:
        lines.append(
            "差異未達顯著門檻或幅度小；實務上仍應以大戶綠燈為主、籌碼僅作警戒。\n\n"
        )

    lines.append(
        f"| arm | n | hit | med% | mean% | sum% | trades/y | maxDD |\n"
        f"|-----|--:|----:|-----:|------:|-----:|---------:|------:|\n"
    )
    for sm, name in ((sc, f"Chip_T0/{primary.id}"), (sw, "Whale_T+1")):
        lines.append(
            f"| {name} | {sm['n']} | {fmt_rate(sm['hit_rate'])} | "
            f"{fmt_pct(sm['med_excess'])} | {fmt_pct(sm['mean_excess'])} | "
            f"{fmt_pct(sm['sum_excess'], 0)} | {fmt_n(sm['trades_per_year'])} | "
            f"{fmt_pct(sm['max_dd_cumsum'], 0)} |\n"
        )
    lines.append("\n")

    def _block(label: str, heading: str) -> None:
        tdu = trades[trades.universe == label]
        sdu = summary[summary.universe == label]
        ddu = days[days.universe == label]
        lines.append(f"## {heading}\n\n")

        lines.append("### 日曆級頻率（全窗）\n\n")
        lines.append(
            "| rule | chip_fire_days | whale_days | both | chip/whale |\n"
            "|------|---------------:|-----------:|-----:|-----------:|\n"
        )
        for rule in RULES:
            m = ddu[ddu.chip_rule == rule.id]
            if m.empty:
                continue
            r = m.iloc[0]
            lines.append(
                f"| {rule.id} | {int(r.chip_fire_days)} | {int(r.whale_days)} | "
                f"{int(r.both_days)} | {fmt_n(r.chip_per_whale)} |\n"
            )
        lines.append(
            "\n註：precision = 交易超額>0 的 hit_rate（非「對上大戶日」）。\n\n"
        )

        for hold in HOLDS:
            tag = " · 主" if hold == PRIMARY_HOLD else " · 敏感度"
            lines.append(f"### H={hold}{tag}\n\n")
            lines.append(
                "| arm | rule | split | n | hit | med% | mean% | sum% | trades/y |\n"
                "|-----|------|-------|--:|----:|-----:|------:|-----:|---------:|\n"
            )
            for split in ("full", "IS", "OOS"):
                m = sdu[
                    (sdu.arm == "Whale_T+1")
                    & (sdu.hold == hold)
                    & (sdu.split == split)
                    & (sdu.chip_rule.fillna("") == "")
                ]
                if not m.empty:
                    r = m.iloc[0]
                    lines.append(
                        f"| Whale_T+1 | — | {split} | {int(r.n)} | {fmt_rate(r.hit_rate)} | "
                        f"{fmt_pct(r.med_excess)} | {fmt_pct(r.mean_excess)} | "
                        f"{fmt_pct(r.sum_excess, 0)} | {fmt_n(r.trades_per_year)} |\n"
                    )
            for rule in RULES:
                for arm in ("Chip_T0", "Chip_T1"):
                    for split in ("full", "IS", "OOS"):
                        m = sdu[
                            (sdu.arm == arm)
                            & (sdu.hold == hold)
                            & (sdu.split == split)
                            & (sdu.chip_rule == rule.id)
                        ]
                        if m.empty:
                            continue
                        r = m.iloc[0]
                        lines.append(
                            f"| {arm} | {rule.id} | {split} | {int(r.n)} | "
                            f"{fmt_rate(r.hit_rate)} | {fmt_pct(r.med_excess)} | "
                            f"{fmt_pct(r.mean_excess)} | {fmt_pct(r.sum_excess, 0)} | "
                            f"{fmt_n(r.trades_per_year)} |\n"
                        )
            lines.append("\n")

            lines.append(
                f"**Bootstrap Δ = Chip_T0 − Whale_T+1（H={hold}）**\n\n"
            )
            lines.append(
                "| rule | split | Δmean pp | 95% CI | P(Δ≤0) | n_chip / n_whale |\n"
                "|------|-------|---------:|-------:|-------:|-----------------:|\n"
            )
            for rule in RULES:
                for split in ("full", "IS", "OOS"):
                    ca = xs_of(tdu, "Chip_T0", hold, rule.id, split)
                    wa = xs_of(tdu, "Whale_T+1", hold, "", split)
                    b = bootstrap_delta(ca, wa)
                    pv = "—" if np.isnan(b["p_delta_le0"]) else f"{b['p_delta_le0']:.2f}"
                    lines.append(
                        f"| {rule.id} | {split} | {fmt_pct(b['delta_mean'])} | "
                        f"[{fmt_pct(b['ci_lo'])}, {fmt_pct(b['ci_hi'])}] | {pv} | "
                        f"{b['n_a']} / {b['n_b']} |\n"
                    )
            lines.append("\n")

            # Chip_T1 sensitivity vs Whale (full only, primary rule)
            lines.append("**敏感度 · Chip_T1（D+1 開）− Whale_T+1 · full**\n\n")
            lines.append("| rule | Δmean pp | 95% CI | P(Δ≤0) | n_chip / n_whale |\n")
            lines.append("|------|---------:|-------:|-------:|-----------------:|\n")
            for rule in RULES:
                ca = xs_of(tdu, "Chip_T1", hold, rule.id, "full")
                wa = xs_of(tdu, "Whale_T+1", hold, "", "full")
                b = bootstrap_delta(ca, wa)
                pv = "—" if np.isnan(b["p_delta_le0"]) else f"{b['p_delta_le0']:.2f}"
                lines.append(
                    f"| {rule.id} | {fmt_pct(b['delta_mean'])} | "
                    f"[{fmt_pct(b['ci_lo'])}, {fmt_pct(b['ci_hi'])}] | {pv} | "
                    f"{b['n_a']} / {b['n_b']} |\n"
                )
            lines.append("\n")

    _block(primary_label, f"主宇宙 · {primary_label}")
    for lab in appendix_labels:
        _block(lab, f"附錄 · {lab}")

    lines.append("## 方法備註\n\n")
    lines.append("- 綠燈 = expert-pool knowledge `trades/{sid}.json` 的 `signal_date`。\n")
    lines.append("- Whale_T+1：綠燈日下一交易日開盤進、持 H 日收盤出（與 L1H7 協議同構）。\n")
    lines.append("- Chip_T0：籌碼條件當日收盤進；Chip_T1：條件日下一開盤進。\n")
    lines.append("- 同股非重疊：進出場日曆窗內不開新倉（避免重疊事件膨脹 n）。\n")
    lines.append(
        "- 顯著「更好」：CI 下沿>0，或 Δ≥1pp 且 P(Δ≤0)≤0.10 且兩邊 n≥20；"
        "「更差」對稱（CI 上沿<0 或 Δ≤−1pp 且 P≥0.90）。\n"
    )
    lines.append(
        "- W5 依賴 `holding_shares_per_tier_a.csv`；Tier B 可能覆蓋不足。\n"
    )
    lines.append(
        f"- 產物：`chip_t0_vs_whale_t1_trades.csv` · "
        f"`chip_t0_vs_whale_t1_summary.csv` · "
        f"`chip_t0_vs_whale_t1_daystats.csv`\n"
    )
    return "".join(lines)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--primary-tier",
        choices=("A", "B", "AB"),
        default="A",
        help="Primary universe for verdict (default A)",
    )
    p.add_argument(
        "--also",
        default="B",
        help="Comma tiers to append (default B; use '' to skip)",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)

    primary = str(args.primary_tier)
    also = [x.strip().upper() for x in (args.also or "").split(",") if x.strip()]
    tiers = [primary] + [t for t in also if t != primary and t in TIER_CFG]

    trade_parts: list[pd.DataFrame] = []
    sum_parts: list[pd.DataFrame] = []
    day_parts: list[pd.DataFrame] = []
    for t in tiers:
        cfg = TIER_CFG[t]
        tr, sm, dy = run_universe(list(cfg["sids"]), str(cfg["label"]))
        trade_parts.append(tr)
        sum_parts.append(sm)
        day_parts.append(dy)

    trades = pd.concat(trade_parts, ignore_index=True)
    summary = pd.concat(sum_parts, ignore_index=True)
    days = pd.concat(day_parts, ignore_index=True)

    trades_path = OUT / "chip_t0_vs_whale_t1_trades.csv"
    summary_path = OUT / "chip_t0_vs_whale_t1_summary.csv"
    days_path = OUT / "chip_t0_vs_whale_t1_daystats.csv"
    trades.to_csv(trades_path, index=False)
    summary.to_csv(summary_path, index=False)
    days.to_csv(days_path, index=False)

    primary_label = str(TIER_CFG[primary]["label"])
    appendix_labels = [str(TIER_CFG[t]["label"]) for t in tiers if t != primary]
    md = build_md(
        trades,
        summary,
        days,
        primary_label=primary_label,
        appendix_labels=appendix_labels,
    )
    out_md = OUT / "CHIP_T0_VS_WHALE_T1.md"
    out_md.write_text(md, encoding="utf-8")

    word, meta = verdict_zh(trades[trades.universe == primary_label], RULES[0])
    status = {
        "phase": "done",
        "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_min": round((time.time() - t0) / 60, 2),
        "primary": primary_label,
        "verdict": word,
        "delta_mean": meta["boot"]["delta_mean"],
        "p_delta_le0": meta["boot"]["p_delta_le0"],
        "n_chip": meta["boot"]["n_a"],
        "n_whale": meta["boot"]["n_b"],
        "out": str(out_md),
    }
    (OUT / "status_chip_t0_vs_whale_t1.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(
        f"DONE verdict={word} Δ={meta['boot']['delta_mean']:+.2f} "
        f"→ {out_md} ({status['elapsed_min']} min)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
