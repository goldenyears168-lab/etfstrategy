"""Rule catalogue + IS/OOS learning for relative-chip day signals."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Callable

import numpy as np
import pandas as pd

MaskFn = Callable[[pd.DataFrame], pd.Series]


@dataclass
class RuleSpec:
    rule_id: str
    side: str  # "bull" | "bear"
    horizon: str  # "t1" | "t5"
    description: str
    mask: MaskFn


@dataclass
class RuleScore:
    rule_id: str
    side: str
    horizon: str
    description: str
    target: str
    n_is: int
    hit_is: float
    med_is: float
    n_oos: int
    hit_oos: float
    med_oos: float
    base_up_oos: float
    lift_oos: float
    kept: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def _hit_rate(y: pd.Series, side: str) -> float:
    y = y.dropna()
    if y.empty:
        return float("nan")
    if side == "bull":
        return float((y > 0).mean())
    return float((y < 0).mean())


def _base_rate(y: pd.Series, side: str) -> float:
    y = y.dropna()
    if y.empty:
        return float("nan")
    if side == "bull":
        return float((y > 0).mean())
    return float((y < 0).mean())


def candidate_rules() -> list[RuleSpec]:
    """Hypotheses from 2492 relative-chip study (v2) + adjacent variants."""
    rules: list[RuleSpec] = []

    def add(
        rid: str,
        side: str,
        horizon: str,
        desc: str,
        mask: MaskFn,
    ) -> None:
        rules.append(RuleSpec(rid, side, horizon, desc, mask))

    # --- T+1 bearish candidates ---
    add(
        "rel_dump",
        "bear",
        "t1",
        "市場FT淨買、個股FT淨賣（相對出貨）",
        lambda d: d["rel_dump"].fillna(False),
    )
    add(
        "rel_dump_hi_part",
        "bear",
        "t1",
        "相對出貨且|占市場|擴展分位≥0.5",
        lambda d: d["rel_dump"].fillna(False)
        & d["FT_part_ok"].fillna(False)
        & d["FT_part_abs_pct"].ge(0.5).fillna(False),
    )
    add(
        "bull_div",
        "bear",
        "t1",
        "價漲籌賣（當日背離）",
        lambda d: d["bull_div"].fillna(False) & d["FT_part_ok"].fillna(False),
    )
    add(
        "bull_div_rel_dump",
        "bear",
        "t1",
        "價漲籌賣 ∧ 相對出貨",
        lambda d: d["bull_div"].fillna(False) & d["rel_dump"].fillna(False),
    )
    add(
        "ft_z_le_m1",
        "bear",
        "t1",
        "FT_z≤−1（自身歷史偏弱）",
        lambda d: d["FT_z"].le(-1.0).fillna(False),
    )
    add(
        "ft_z_le_m1_align_sell",
        "bear",
        "t1",
        "FT_z≤−1 ∧ 與市場同向賣",
        lambda d: d["FT_z"].le(-1.0).fillna(False) & d["aligned_sell"].fillna(False),
    )
    add(
        "resid_z_le_m1",
        "bear",
        "t1",
        "FT殘差z≤−1（比跟大盤更弱）",
        lambda d: d["FT_resid_z"].le(-1.0).fillna(False),
    )
    add(
        "strong2_like",
        "bear",
        "t1",
        "連續兩日 FT_z≥1（高潮風險；預期隔日勿追）",
        lambda d: d["FT_z"].ge(1.0).fillna(False)
        & d["FT_z"].shift(1).ge(1.0).fillna(False),
    )
    add(
        "part_q1",
        "bear",
        "t1",
        "FT占市場總買賣擴展分位≤0.2",
        lambda d: d["FT_part_ok"].fillna(False) & d["FT_part_pct"].le(0.2).fillna(False),
    )

    # --- T+1 bullish candidates ---
    add(
        "aligned_buy",
        "bull",
        "t1",
        "市場與個股同向買",
        lambda d: d["aligned_buy"].fillna(False),
    )
    add(
        "aligned_buy_hi_part",
        "bull",
        "t1",
        "同向買且占市場擴展分位≥0.8",
        lambda d: d["aligned_buy"].fillna(False)
        & d["FT_part_ok"].fillna(False)
        & d["FT_part_pct"].ge(0.8).fillna(False),
    )
    add(
        "bear_div",
        "bull",
        "t1",
        "價跌籌買（承接）",
        lambda d: d["bear_div"].fillna(False),
    )
    add(
        "bear_div_aligned",
        "bull",
        "t1",
        "價跌籌買 ∧ 與市場同向買",
        lambda d: d["bear_div"].fillna(False) & d["aligned_buy"].fillna(False),
    )
    add(
        "rel_scoop",
        "bull",
        "t1",
        "市場賣、個股買（相對吸籌）",
        lambda d: d["rel_scoop"].fillna(False),
    )
    add(
        "ft_z_ge_1",
        "bull",
        "t1",
        "FT_z≥1（單日偏強；可能只共現）",
        lambda d: d["FT_z"].ge(1.0).fillna(False),
    )
    add(
        "resid_z_ge_1",
        "bull",
        "t1",
        "FT殘差z≥1",
        lambda d: d["FT_resid_z"].ge(1.0).fillna(False),
    )
    add(
        "part_q5",
        "bull",
        "t1",
        "FT占市場總買賣擴展分位≥0.8",
        lambda d: d["FT_part_ok"].fillna(False) & d["FT_part_pct"].ge(0.8).fillna(False),
    )

    # --- T+5 variants (same masks, longer horizon) ---
    t5_from = [
        "rel_dump",
        "bull_div_rel_dump",
        "ft_z_le_m1_align_sell",
        "resid_z_le_m1",
        "aligned_buy_hi_part",
        "bear_div_aligned",
        "rel_scoop",
        "part_q5",
        "part_q1",
        "strong2_like",
        "rs_mom_weak_dump",
        "rel_dump_bull_div_z",
        "rel_dump_rs_weak",
        "part_q1_align_sell",
        "resid_neg_rel_dump",
        "aligned_buy_bear_div_hi",
        "scoop_resid_pos",
        "part_q5_not_strong2",
    ]
    add(
        "rs_mom_weak_dump",
        "bear",
        "t1",
        "RS_mom10≤−3 ∧ 相對出貨",
        lambda d: d["RS_mom10"].le(-3.0).fillna(False) & d["rel_dump"].fillna(False),
    )
    add(
        "rs_mom_strong_scoop",
        "bull",
        "t1",
        "RS_mom10≥3 ∧ 相對吸籌",
        lambda d: d["RS_mom10"].ge(3.0).fillna(False) & d["rel_scoop"].fillna(False),
    )
    # --- Iteration 2: tighter combinations for hit-rate ---
    add(
        "rel_dump_bull_div_z",
        "bear",
        "t1",
        "相對出貨 ∧ 價漲籌賣 ∧ FT_z≤0",
        lambda d: d["rel_dump"].fillna(False)
        & d["bull_div"].fillna(False)
        & d["FT_z"].le(0).fillna(False),
    )
    add(
        "rel_dump_rs_weak",
        "bear",
        "t1",
        "相對出貨 ∧ RS_mom10≤0",
        lambda d: d["rel_dump"].fillna(False) & d["RS_mom10"].le(0).fillna(False),
    )
    add(
        "part_q1_align_sell",
        "bear",
        "t1",
        "占比分位≤0.2 ∧ 同向賣",
        lambda d: d["FT_part_pct"].le(0.2).fillna(False) & d["aligned_sell"].fillna(False),
    )
    add(
        "resid_neg_rel_dump",
        "bear",
        "t1",
        "殘差z≤−0.5 ∧ 相對出貨",
        lambda d: d["FT_resid_z"].le(-0.5).fillna(False) & d["rel_dump"].fillna(False),
    )
    add(
        "aligned_buy_bear_div_hi",
        "bull",
        "t1",
        "同向買 ∧ 價跌承接 ∧ 占比分位≥0.6",
        lambda d: d["aligned_buy"].fillna(False)
        & d["bear_div"].fillna(False)
        & d["FT_part_pct"].ge(0.6).fillna(False),
    )
    add(
        "scoop_resid_pos",
        "bull",
        "t1",
        "相對吸籌 ∧ 殘差z≥0.5",
        lambda d: d["rel_scoop"].fillna(False) & d["FT_resid_z"].ge(0.5).fillna(False),
    )
    add(
        "part_q5_not_strong2",
        "bull",
        "t1",
        "占比分位≥0.8 且非連續雙強",
        lambda d: d["FT_part_pct"].ge(0.8).fillna(False)
        & ~(
            d["FT_z"].ge(1.0).fillna(False) & d["FT_z"].shift(1).ge(1.0).fillna(False)
        ),
    )

    base = {r.rule_id: r for r in rules}
    for rid in t5_from:
        if rid not in base:
            continue
        b = base[rid]
        rules.append(
            RuleSpec(
                f"{rid}__t5",
                b.side,
                "t5",
                b.description + " → 未來5日",
                b.mask,
            )
        )
    # also promote a few extra to t5
    for rid in ("rs_mom_strong_scoop", "bear_div", "aligned_buy"):
        b = base[rid]
        rules.append(
            RuleSpec(
                f"{rid}__t5",
                b.side,
                "t5",
                b.description + " → 未來5日",
                b.mask,
            )
        )
    return rules


def evaluate_rules(
    panel: pd.DataFrame,
    *,
    start: str,
    oos_cut: str,
    end: str,
    min_n_is: int = 12,
    min_n_oos: int = 8,
    min_lift: float = 0.08,
    min_hit_oos: float = 0.55,
    target_mode: str = "raw",  # raw | excess
) -> list[RuleScore]:
    show = panel[
        (panel["date"] >= pd.Timestamp(start)) & (panel["date"] <= pd.Timestamp(end))
    ].copy()
    is_m = show["date"] < pd.Timestamp(oos_cut)
    oos_m = show["date"] >= pd.Timestamp(oos_cut)

    scores: list[RuleScore] = []
    for spec in candidate_rules():
        if spec.horizon == "t1":
            target = "fwd1_ex" if target_mode == "excess" else "fwd1_r"
        else:
            target = "fwd5_ex" if target_mode == "excess" else "fwd5_r"
        mask = spec.mask(show).fillna(False)
        y_is = show.loc[is_m & mask, target]
        y_oos = show.loc[oos_m & mask, target]
        base_oos = show.loc[oos_m, target]
        hit_is = _hit_rate(y_is, spec.side)
        hit_oos = _hit_rate(y_oos, spec.side)
        base = _base_rate(base_oos, spec.side)
        lift = hit_oos - base if np.isfinite(hit_oos) and np.isfinite(base) else float("nan")
        med_is = float(y_is.median()) if len(y_is.dropna()) else float("nan")
        med_oos = float(y_oos.median()) if len(y_oos.dropna()) else float("nan")
        n_is = int(y_is.notna().sum())
        n_oos = int(y_oos.notna().sum())

        kept = False
        reason = "reject"
        rid = spec.rule_id if target_mode == "raw" else f"{spec.rule_id}__ex"
        desc = spec.description if target_mode == "raw" else spec.description + "〔β超額〕"
        if n_is < min_n_is:
            reason = f"thin_is n_is={n_is}<{min_n_is}"
        elif n_oos < min_n_oos:
            reason = f"thin_oos n_oos={n_oos}<{min_n_oos}"
        elif not np.isfinite(hit_oos) or hit_oos < min_hit_oos:
            reason = f"hit_oos={hit_oos:.2f}<{min_hit_oos}"
        elif not np.isfinite(lift) or lift < min_lift:
            reason = f"lift_oos={lift:.2f}<{min_lift}"
        else:
            if spec.side == "bull" and not (np.isfinite(med_oos) and med_oos > 0):
                reason = "oos_median_not_positive"
            elif spec.side == "bear" and not (np.isfinite(med_oos) and med_oos < 0):
                reason = "oos_median_not_negative"
            else:
                kept = True
                reason = "pass_oos"

        scores.append(
            RuleScore(
                rule_id=rid,
                side=spec.side,
                horizon=spec.horizon,
                description=desc,
                target=target,
                n_is=n_is,
                hit_is=hit_is,
                med_is=med_is,
                n_oos=n_oos,
                hit_oos=hit_oos,
                med_oos=med_oos,
                base_up_oos=base,
                lift_oos=lift,
                kept=kept,
                reason=reason,
            )
        )
    return scores


def learn_rules(
    panel: pd.DataFrame,
    *,
    start: str,
    oos_cut: str,
    end: str,
) -> tuple[list[RuleScore], list[RuleScore]]:
    """Evaluate raw + excess targets; merge unique passes (prefer strict)."""
    bundles: list[list[RuleScore]] = []
    for mode in ("raw", "excess"):
        strict = evaluate_rules(
            panel,
            start=start,
            oos_cut=oos_cut,
            end=end,
            min_n_is=12,
            min_n_oos=8,
            min_lift=0.08,
            min_hit_oos=0.55,
            target_mode=mode,
        )
        bundles.append(strict)
        if sum(1 for s in strict if s.kept) < 1:
            relaxed = evaluate_rules(
                panel,
                start=start,
                oos_cut=oos_cut,
                end=end,
                min_n_is=10,
                min_n_oos=6,
                min_lift=0.05,
                min_hit_oos=0.52,
                target_mode=mode,
            )
            strict_pass = {s.rule_id for s in strict if s.kept}
            for s in relaxed:
                if s.kept and s.rule_id not in strict_pass:
                    s.reason = "pass_oos_relaxed"
            bundles.append(relaxed)

    # Prefer first bundle with any pass; else last
    chosen = bundles[0]
    for b in bundles:
        if any(s.kept for s in b):
            chosen = b
            break
    # Merge all kept across bundles for knowledge (dedupe by rule_id)
    merged: dict[str, RuleScore] = {}
    all_scores: list[RuleScore] = []
    for b in bundles:
        for s in b:
            all_scores.append(s)
            if s.kept:
                prev = merged.get(s.rule_id)
                if prev is None or (s.lift_oos or 0) > (prev.lift_oos or 0):
                    merged[s.rule_id] = s
    # Final list = non-kept from chosen + all merged kept
    final_map = {s.rule_id: s for s in chosen}
    for rid, s in merged.items():
        final_map[rid] = s
    final = list(final_map.values())
    return chosen, final


def daily_signal_table(
    panel: pd.DataFrame,
    kept: list[RuleScore],
    *,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Per-day fired kept rules + realized fwd returns."""
    show = panel[
        (panel["date"] >= pd.Timestamp(start)) & (panel["date"] <= pd.Timestamp(end))
    ].copy()
    catalogue = {r.rule_id: r for r in candidate_rules()}
    rows = []
    for _, row in show.iterrows():
        day = show.loc[[row.name]]
        fired = []
        for ks in kept:
            base_id = ks.rule_id.removesuffix("__ex")
            spec = catalogue.get(base_id)
            if spec is None:
                continue
            if bool(spec.mask(day).iloc[0]):
                fired.append(ks.rule_id)
        rows.append(
            {
                "date": row["date"].date().isoformat(),
                "close": row["close"],
                "r": row["r"],
                "excess_beta": row["excess_beta"],
                "FT_part_bp": row.get("FT_part_bp"),
                "align_FT": row.get("align_FT"),
                "FT_z": row.get("FT_z"),
                "rel_dump": bool(row.get("rel_dump")),
                "aligned_buy": bool(row.get("aligned_buy")),
                "bull_div": bool(row.get("bull_div")),
                "fwd1_r": row.get("fwd1_r"),
                "fwd5_r": row.get("fwd5_r"),
                "fwd1_oc": row.get("fwd1_oc"),
                "fired_rules": "|".join(fired) if fired else "",
                "n_fired": len(fired),
            }
        )
    return pd.DataFrame(rows)
