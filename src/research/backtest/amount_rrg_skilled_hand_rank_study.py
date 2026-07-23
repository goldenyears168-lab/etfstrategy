"""富邦新店加總帶裡的「高手指紋」軟排序研究。

目標不是還原帳戶 ID，而是：在同一日多檔過門檻候選中，
用「持續卡倉／當日集中度／乾淨買超」分數重排 Top1，
看能否在 OOS 單槽複利上優於「純金額 Top1」基準。

基準：xd ≥80M · rs_ratio>100 · L2H12 · 1槽 · capital 10k
複利 SSOT：Π(1+r)−1（與 C18acc equal-slot compound 同構）
選參：IS max compound；OOS replay only。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from copytrade.dual_branch_signals import DualBranchTapeCache
from copytrade.signals import CopytradeSignal
from report_paths import REPORTS_RESEARCH
from research.backtest.amount_rrg_2day_sell_funnel_study import (
    C18_OVERLAP_START,
    DEFAULT_AMOUNT_NTD,
    _enrich_metrics_with_compound,
    _oos_gate,
    build_tape_feature,
    load_xd_tape_bs,
)
from research.backtest.amount_rrg_copytrade_funnel_study import (
    DEFAULT_OOS_CUT,
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
)
from research.backtest.amount_rrg_copytrade_study import (
    apply_rrg_gate,
    build_rrg_ffill_index,
    build_signals_fixed,
    load_rrg_close_index,
)
from research.backtest.copytrade_backtest import DEFAULT_SIGNAL_CAPITAL_NTD

FILE_STEM = "amount_rrg_skilled_hand_rank_study"
CANDIDATE_TOP_N = 5  # pool before soft re-rank


@dataclass(frozen=True)
class RankSpec:
    id: str
    family: str
    label: str
    # Higher score wins; ties broken by amount then stock_id.
    score_fn: Callable[[dict[str, Any]], float]


def _safe_log1p(x: float) -> float:
    return math.log1p(max(0.0, float(x)))


def build_hand_features(
    *,
    day: str,
    stock_id: str,
    tape_bs: dict[tuple[str, str], dict[str, float]],
    closes: dict[tuple[str, str], float],
    calendar: list[str],
    amount_thr: float,
    day_pos_net: dict[str, float] | None = None,
) -> dict[str, Any] | None:
    base = build_tape_feature(
        day=day,
        stock_id=stock_id,
        tape_bs=tape_bs,
        closes=closes,
        calendar=calendar,
        amount_thr=amount_thr,
    )
    if base is None:
        return None
    if day not in calendar:
        return None
    idx = calendar.index(day)
    prior = calendar[max(0, idx - 20) : idx]
    prior5 = calendar[max(0, idx - 5) : idx]
    prior_net = sum(
        float(tape_bs[(d, stock_id)]["net"])
        for d in prior
        if (d, stock_id) in tape_bs
    )
    prior5_net = sum(
        float(tape_bs[(d, stock_id)]["net"])
        for d in prior5
        if (d, stock_id) in tape_bs
    )
    prior_hits = sum(
        1
        for d in prior
        if (d, stock_id) in tape_bs and float(tape_bs[(d, stock_id)]["net"]) > 0
    )
    # streak of consecutive prior sessions with net>0 ending at t-1
    streak = 0
    for d in reversed(prior):
        row = tape_bs.get((d, stock_id))
        if row is None or float(row["net"]) <= 0:
            break
        streak += 1
    if day_pos_net is not None:
        day_pos = float(day_pos_net.get(day) or 0.0)
    else:
        day_pos = sum(
            float(v["net"])
            for (d, _s), v in tape_bs.items()
            if d == day and float(v["net"]) > 0
        )
    net_t = float(base["net_t"])
    conc = (net_t / day_pos) if day_pos > 0 else 0.0
    amount = float(base["amount_t"] or 0.0)
    sell_ratio = base.get("sell_ratio")
    clean = 1.0 - min(1.0, float(sell_ratio)) if sell_ratio is not None else 0.5
    return {
        **base,
        "amount": amount,
        "net_lots": net_t / 1000.0,
        "prior_net_lots_20": prior_net / 1000.0,
        "prior_net_lots_5": prior5_net / 1000.0,
        "prior_hits_20": float(prior_hits),
        "buy_streak": float(streak),
        "branch_conc": conc,
        "clean_buy": clean,
        "log_amount": _safe_log1p(amount / 1e6),
        "log_prior20": _safe_log1p(prior_net / 1000.0),
        "log_prior5": _safe_log1p(prior5_net / 1000.0),
    }


def default_rank_specs() -> list[RankSpec]:
    return [
        RankSpec(
            "amount_only",
            "baseline",
            "純金額 Top1（冠軍基準）",
            lambda f: float(f["amount"]),
        ),
        RankSpec(
            "prior20",
            "relationship",
            "近20日同分點同股累計買超",
            lambda f: float(f["prior_net_lots_20"]),
        ),
        RankSpec(
            "prior5",
            "relationship",
            "近5日同分點同股累計買超",
            lambda f: float(f["prior_net_lots_5"]),
        ),
        RankSpec(
            "streak",
            "relationship",
            "連續買超天數",
            lambda f: float(f["buy_streak"]),
        ),
        RankSpec(
            "conc",
            "dominance",
            "當日佔分點買超比重",
            lambda f: float(f["branch_conc"]),
        ),
        RankSpec(
            "clean",
            "dominance",
            "乾淨買超（1−sell/buy）",
            lambda f: float(f["clean_buy"]),
        ),
        RankSpec(
            "amt_x_prior20",
            "hybrid",
            "log(金額)×log(近20日累計)",
            lambda f: float(f["log_amount"]) * (1.0 + float(f["log_prior20"])),
        ),
        RankSpec(
            "amt_plus_prior20",
            "hybrid",
            "z-ish：log金額 + 0.7·log近20日",
            lambda f: float(f["log_amount"]) + 0.7 * float(f["log_prior20"]),
        ),
        RankSpec(
            "amt_plus_conc",
            "hybrid",
            "log金額 + 2·集中度",
            lambda f: float(f["log_amount"]) + 2.0 * float(f["branch_conc"]),
        ),
        RankSpec(
            "hand_score_v1",
            "hybrid",
            "高手分 v1：log金額 + 0.5·log近20 + 1.5·集中度 + 0.3·連續天",
            lambda f: (
                float(f["log_amount"])
                + 0.5 * float(f["log_prior20"])
                + 1.5 * float(f["branch_conc"])
                + 0.3 * float(f["buy_streak"])
            ),
        ),
        RankSpec(
            "hand_score_v2",
            "hybrid",
            "高手分 v2：偏關係（累計權重更高）",
            lambda f: (
                0.6 * float(f["log_amount"])
                + 1.0 * float(f["log_prior20"])
                + 1.0 * float(f["branch_conc"])
                + 0.5 * float(f["buy_streak"])
                + 0.3 * float(f["clean_buy"])
            ),
        ),
        RankSpec(
            "hand_score_v3",
            "hybrid",
            "高手分 v3：金額門檻內用關係打破平手",
            lambda f: (
                float(f["log_amount"])
                + 0.15 * float(f["log_prior20"])
                + 0.5 * float(f["branch_conc"])
            ),
        ),
        # Switch rule: keep amount champ unless challenger within 80% amount
        # and clearly better relationship — implemented as special family below.
    ]


def rerank_day(
    legs: list[CopytradeSignal],
    *,
    day: str,
    spec: RankSpec,
    tape_bs: dict[tuple[str, str], dict[str, float]],
    closes: dict[tuple[str, str], float],
    calendar: list[str],
    amount_thr: float,
    day_pos_net: dict[str, float] | None = None,
    switch_rule: str | None = None,
) -> tuple[list[CopytradeSignal], dict[str, Any]]:
    """Return Top1 (list len 0/1) after soft rank; meta for diagnostics."""
    scored: list[tuple[float, float, str, CopytradeSignal, dict[str, Any]]] = []
    for leg in legs:
        feat = build_hand_features(
            day=day,
            stock_id=str(leg.stock_id),
            tape_bs=tape_bs,
            closes=closes,
            calendar=calendar,
            amount_thr=amount_thr,
            day_pos_net=day_pos_net,
        )
        if feat is None:
            continue
        score = float(spec.score_fn(feat))
        scored.append(
            (score, float(feat["amount"]), str(leg.stock_id), leg, feat)
        )
    if not scored:
        return [], {"n_cands": 0, "switched": False}

    # Amount-primary ordering for switch baseline
    by_amount = sorted(scored, key=lambda r: (-r[1], r[2]))
    amount_champ = by_amount[0]
    by_score = sorted(scored, key=lambda r: (-r[0], -r[1], r[2]))
    score_champ = by_score[0]

    switched = False
    chosen = score_champ
    if switch_rule == "within_80pct_amount":
        # Only override amount Top1 if challenger amount >= 80% of champ
        # and relationship edge is material.
        ac_amt = amount_champ[1]
        challenger = None
        for row in by_score:
            if row[3].stock_id == amount_champ[3].stock_id:
                continue
            if row[1] >= 0.80 * ac_amt:
                challenger = row
                break
        if challenger is not None:
            # require clearly better prior or conc
            ac_f, ch_f = amount_champ[4], challenger[4]
            better_prior = float(ch_f["prior_net_lots_20"]) >= float(
                ac_f["prior_net_lots_20"]
            ) * 2.0 + 200.0
            better_conc = float(ch_f["branch_conc"]) >= float(ac_f["branch_conc"]) + 0.10
            if better_prior or better_conc:
                chosen = challenger
                switched = True
            else:
                chosen = amount_champ
        else:
            chosen = amount_champ
    elif switch_rule == "always_score":
        chosen = score_champ
        switched = chosen[3].stock_id != amount_champ[3].stock_id
    else:
        # default: pure score
        switched = chosen[3].stock_id != amount_champ[3].stock_id

    return [chosen[3]], {
        "n_cands": len(scored),
        "switched": switched,
        "pick": str(chosen[3].stock_id),
        "amount_champ": str(amount_champ[3].stock_id),
        "score": round(chosen[0], 4),
        "amount_m": round(chosen[1] / 1e6, 2),
        "prior20": round(float(chosen[4]["prior_net_lots_20"]), 1),
        "conc": round(float(chosen[4]["branch_conc"]), 3),
    }


def build_ranked_grouped(
    pool: dict[str, list[CopytradeSignal]],
    *,
    spec: RankSpec,
    tape_bs: dict[tuple[str, str], dict[str, float]],
    closes: dict[tuple[str, str], float],
    calendar: list[str],
    amount_thr: float,
    day_pos_net: dict[str, float] | None = None,
    switch_rule: str | None = None,
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
    out: dict[str, list[CopytradeSignal]] = {}
    n_switch = 0
    n_days = 0
    n_multi = 0
    for day, legs in pool.items():
        n_days += 1
        if len(legs) > 1:
            n_multi += 1
        picked, meta = rerank_day(
            legs,
            day=day,
            spec=spec,
            tape_bs=tape_bs,
            closes=closes,
            calendar=calendar,
            amount_thr=amount_thr,
            day_pos_net=day_pos_net,
            switch_rule=switch_rule,
        )
        if meta.get("switched"):
            n_switch += 1
        if picked:
            out[day] = picked
    return out, {
        "signal_days": len(out),
        "pool_days": n_days,
        "multi_cand_days": n_multi,
        "switch_days": n_switch,
        "switch_pct": round(100.0 * n_switch / n_days, 2) if n_days else None,
    }


def run_skilled_hand_rank_study(
    conn: sqlite3.Connection,
    *,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str = DEFAULT_WINDOW_END,
    oos_cut: str = DEFAULT_OOS_CUT,
    amount_ntd: float = DEFAULT_AMOUNT_NTD,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    candidate_top_n: int = CANDIDATE_TOP_N,
) -> dict[str, Any]:
    tape = DualBranchTapeCache.load(
        conn, window_start=window_start, window_end=window_end, need_closes=True
    )
    calendar = sorted(tape.xd.keys())
    union_cal = sorted(set(tape.sj) | set(tape.xd))
    tape_bs = load_xd_tape_bs(
        conn, window_start=window_start, window_end=window_end
    )
    day_pos_net: dict[str, float] = {}
    for (d, _s), v in tape_bs.items():
        net = float(v["net"])
        if net > 0:
            day_pos_net[d] = day_pos_net.get(d, 0.0) + net
    raw_rrg = load_rrg_close_index(
        conn, window_start=window_start, window_end=window_end
    )
    rrg_ffill = build_rrg_ffill_index(raw_rrg, union_cal)

    # Wide pool then soft pick Top1
    raw_pool = build_signals_fixed(
        conn,
        mode="xd",
        min_threshold=amount_ntd,
        top_n=candidate_top_n,
        window_start=window_start,
        window_end=window_end,
        tape=tape,
        min_avg_volume_shares=200_000.0,
    )
    pool, _, gate_drops = apply_rrg_gate(raw_pool, rrg_ffill, "rs_ratio_gt100")

    # True champion day set: raw amount-Top1 must itself pass RRG.
    # Soft re-rank must NOT recover "backup days" (Top1 fails RRG → pick Top2).
    raw_top1 = build_signals_fixed(
        conn,
        mode="xd",
        min_threshold=amount_ntd,
        top_n=1,
        window_start=window_start,
        window_end=window_end,
        tape=tape,
        min_avg_volume_shares=200_000.0,
    )
    champ_days, _, _ = apply_rrg_gate(raw_top1, rrg_ffill, "rs_ratio_gt100")
    pool_on_champ = {d: legs for d, legs in pool.items() if d in champ_days}

    specs = default_rank_specs()
    arms: list[dict[str, Any]] = []

    def _run_specs(
        pool_map: dict[str, list[CopytradeSignal]],
        *,
        pool_tag: str,
    ) -> list[dict[str, Any]]:
        out_arms: list[dict[str, Any]] = []
        for spec in specs:
            grouped, meta = build_ranked_grouped(
                pool_map,
                spec=spec,
                tape_bs=tape_bs,
                closes=tape.closes,
                calendar=calendar,
                amount_thr=amount_ntd,
                day_pos_net=day_pos_net,
                switch_rule="always_score",
            )
            metrics = _enrich_metrics_with_compound(
                conn,
                grouped,
                label=f"hand_rank:{pool_tag}:{spec.id}",
                capital_ntd=capital_ntd,
                window_start=window_start,
                window_end=window_end,
                oos_cut=oos_cut,
            )
            out_arms.append(
                {
                    "rank_id": spec.id if pool_tag == "champ_days" else f"{pool_tag}__{spec.id}",
                    "family": spec.family if pool_tag == "champ_days" else f"ablation_{pool_tag}",
                    "label": spec.label
                    if pool_tag == "champ_days"
                    else f"[{pool_tag}] {spec.label}",
                    "switch_rule": "always_score",
                    "pool_tag": pool_tag,
                    "rank_meta": meta,
                    **metrics,
                }
            )
        hand_v2 = next(s for s in specs if s.id == "hand_score_v2")
        grouped, meta = build_ranked_grouped(
            pool_map,
            spec=hand_v2,
            tape_bs=tape_bs,
            closes=tape.closes,
            calendar=calendar,
            amount_thr=amount_ntd,
            day_pos_net=day_pos_net,
            switch_rule="within_80pct_amount",
        )
        metrics = _enrich_metrics_with_compound(
            conn,
            grouped,
            label=f"hand_rank:{pool_tag}:switch_80",
            capital_ntd=capital_ntd,
            window_start=window_start,
            window_end=window_end,
            oos_cut=oos_cut,
        )
        out_arms.append(
            {
                "rank_id": "switch_80"
                if pool_tag == "champ_days"
                else f"{pool_tag}__switch_80",
                "family": "switch"
                if pool_tag == "champ_days"
                else f"ablation_{pool_tag}",
                "label": "金額冠軍為主，僅在金額≥80%且關係明顯更強時換股"
                if pool_tag == "champ_days"
                else f"[{pool_tag}] conservative switch",
                "switch_rule": "within_80pct_amount",
                "pool_tag": pool_tag,
                "rank_meta": meta,
                **metrics,
            }
        )
        return out_arms

    # Primary protocol: champ days only
    arms.extend(_run_specs(pool_on_champ, pool_tag="champ_days"))
    # Ablation: expanded pool (documents backup-day contamination)
    abl_amount = next(s for s in specs if s.id == "amount_only")
    g_abl, meta_abl = build_ranked_grouped(
        pool,
        spec=abl_amount,
        tape_bs=tape_bs,
        closes=tape.closes,
        calendar=calendar,
        amount_thr=amount_ntd,
        day_pos_net=day_pos_net,
        switch_rule="always_score",
    )
    m_abl = _enrich_metrics_with_compound(
        conn,
        g_abl,
        label="hand_rank:expanded_pool:amount_only",
        capital_ntd=capital_ntd,
        window_start=window_start,
        window_end=window_end,
        oos_cut=oos_cut,
    )
    arms.append(
        {
            "rank_id": "expanded_pool__amount_only",
            "family": "ablation_expanded_pool",
            "label": "[expanded] 金額Top1（含Top1未過RRG時的備胎日）",
            "switch_rule": "always_score",
            "pool_tag": "expanded_pool",
            "rank_meta": {
                **meta_abl,
                "recovered_days_vs_champ": len(set(pool) - set(champ_days)),
            },
            **m_abl,
        }
    )

    baseline = next(a for a in arms if a["rank_id"] == "amount_only")
    # IS select only among primary (champ_days) arms
    primary = [a for a in arms if a.get("pool_tag") == "champ_days"]
    base_is_n = int(baseline["is"].get("n_cycles") or 0)
    viable = [
        a
        for a in primary
        if int(a["is"].get("n_cycles") or 0) >= max(6, int(base_is_n * 0.70))
    ]
    selected = max(
        viable or [baseline],
        key=lambda a: (
            float(a["is"].get("compound_slot_pct") or -1e18),
            float(a["is"].get("sum_return_pct") or -1e18),
            int(a["is"].get("n_cycles") or 0),
        ),
    )
    gate = _oos_gate(selected, baseline)
    # Robustness peek: best OOS among primary (not for adoption)
    oos_best = max(
        primary,
        key=lambda a: float(a["oos"].get("compound_slot_pct") or -1e18),
    )
    surviving = (
        selected if gate["status"] in {"passed", "redundant"} else baseline
    )

    family_summary: dict[str, Any] = {}
    for fam in sorted({a["family"] for a in arms}):
        fam_arms = [a for a in arms if a["family"] == fam]
        best = max(
            fam_arms,
            key=lambda a: float(a["is"].get("compound_slot_pct") or -1e18),
        )
        family_summary[fam] = {
            "best_rank_id": best["rank_id"],
            "is_compound": best["is"].get("compound_slot_pct"),
            "oos_compound": best["oos"].get("compound_slot_pct"),
            "full_compound": best["full"].get("compound_slot_pct"),
            "oos_gate": _oos_gate(best, baseline),
            "switch_pct": best["rank_meta"].get("switch_pct"),
        }

    c18_path = (
        REPORTS_RESEARCH / "rrg" / "20260717_c18acc_vs_radar_c0_slot_fair.json"
    )
    c18_fair = None
    if c18_path.exists():
        c18 = json.loads(c18_path.read_text(encoding="utf-8"))
        b0 = next(
            (
                v
                for v in c18.get("variants") or []
                if v.get("variant_id") == "B0_live"
            ),
            None,
        )
        if b0:
            sl = (b0.get("slices") or {}).get("full") or {}
            c18_fair = {
                "source": c18_path.name,
                "compound_slot_pct": sl.get("compound_slot_pct"),
                "n_periods": sl.get("n_periods"),
            }

    def _arm(rid: str) -> dict[str, Any]:
        return next(a for a in arms if a["rank_id"] == rid)

    # Fix relationship hypothesis against explicit prior20 arm (OOS-interesting)
    prior20_gate = _oos_gate(_arm("prior20"), baseline)
    hand_v2_gate = _oos_gate(_arm("hand_score_v2"), baseline)
    expanded_gate = _oos_gate(_arm("expanded_pool__amount_only"), baseline)

    hypotheses = {
        "H_NO_BACKUP_DAYS": {
            "statement": (
                "當金額Top1未過 RRG 時改用 Top2–5 備胎，會污染複利"
            ),
            "status": "passed"
            if expanded_gate["status"] == "rejected"
            and float(
                _arm("expanded_pool__amount_only")["oos"].get("compound_slot_pct")
                or 0
            )
            < float(baseline["oos"].get("compound_slot_pct") or 0) * 0.5
            else expanded_gate["status"],
            "expanded_oos_compound": _arm("expanded_pool__amount_only")["oos"].get(
                "compound_slot_pct"
            ),
            "champ_oos_compound": baseline["oos"].get("compound_slot_pct"),
            "recovered_days": len(set(pool) - set(champ_days)),
        },
        "H_RELATIONSHIP_RANK": {
            "statement": (
                "在冠軍日集合內，用近20日同分點同股累計買超重排 Top1，"
                "OOS 複利優於純金額（≥+10%）"
            ),
            "status": prior20_gate["status"],
            "is_compound": _arm("prior20")["is"].get("compound_slot_pct"),
            "oos_compound": _arm("prior20")["oos"].get("compound_slot_pct"),
            "note": (
                "IS 劣於金額Top1，故不作為 IS-adopted survivor；"
                "列為 OOS robustness 觀察候選"
            ),
        },
        "H_DOMINANCE_RANK": {
            "statement": "用當日集中度／乾淨買超重排 Top1，OOS 複利優於純金額",
            "status": family_summary.get("dominance", {})
            .get("oos_gate", {})
            .get("status"),
            "best": family_summary.get("dominance", {}).get("best_rank_id"),
        },
        "H_HAND_SCORE": {
            "statement": "高手綜合分 v2（偏關係）重排可通過 OOS 複利閘門",
            "status": hand_v2_gate["status"],
            "oos_compound": _arm("hand_score_v2")["oos"].get("compound_slot_pct"),
        },
        "H_CONSERVATIVE_SWITCH": {
            "statement": "僅在金額接近且關係明顯更強時換股，可提升 OOS 複利",
            "status": _oos_gate(_arm("switch_80"), baseline)["status"],
        },
        "H_SURVIVOR_BEATS_AMOUNT_TOP1": {
            "statement": "IS 複利冠軍在 OOS 相對金額 Top1 加值 ≥10%",
            "status": gate["status"],
            "selected": selected["rank_id"],
            "surviving": surviving["rank_id"],
        },
    }

    return {
        "schema": "amount_rrg_skilled_hand_rank_study-v2",
        "contract": {
            "window_start": window_start,
            "window_end": window_end,
            "oos_cut": oos_cut,
            "baseline": (
                "xd ≥80M · raw amount-Top1 passes rs_ratio>100 · L2H12 · 1槽"
            ),
            "pool": (
                f"top{candidate_top_n} amount → RRG → soft re-rank，"
                "但只保留 raw amount-Top1 已過 RRG 的冠軍日（禁止備胎日）"
            ),
            "capital_ntd_per_slot": capital_ntd,
            "compound_definition": "equal_slot_compound Π(1+r)-1 (1-slot)",
            "selection": "IS max compound_slot_pct among champ-day arms; OOS robustness",
            "identity_note": (
                "Cannot recover account ID from branch aggregates; "
                "scores approximate skilled-hand morphology only."
            ),
            "champ_days": len(champ_days),
            "pool_days_expanded": len(pool),
            "multi_cand_champ_days": sum(
                1 for legs in pool_on_champ.values() if len(legs) > 1
            ),
            "rrg_gate_drops": gate_drops,
        },
        "baseline": {
            "rank_id": "amount_only",
            "full": baseline["full"],
            "is": baseline["is"],
            "oos": baseline["oos"],
            "overlap_from_202501": baseline["overlap_from_202501"],
        },
        "arms": [
            {
                "rank_id": a["rank_id"],
                "family": a["family"],
                "label": a["label"],
                "pool_tag": a.get("pool_tag"),
                "switch_rule": a["switch_rule"],
                "rank_meta": a["rank_meta"],
                "full": a["full"],
                "is": a["is"],
                "oos": a["oos"],
                "overlap_from_202501": a["overlap_from_202501"],
                "oos_gate_vs_baseline": _oos_gate(a, baseline),
            }
            for a in arms
        ],
        "is_selected": {
            "rank_id": selected["rank_id"],
            "is": selected["is"],
            "oos": selected["oos"],
            "oos_gate": gate,
        },
        "gate_surviving": {
            "rank_id": surviving["rank_id"],
            "full": surviving["full"],
            "is": surviving["is"],
            "oos": surviving["oos"],
            "overlap_from_202501": surviving["overlap_from_202501"],
            "rank_meta": surviving["rank_meta"],
        },
        "oos_robustness_candidate": {
            "rank_id": oos_best["rank_id"],
            "label": oos_best["label"],
            "is": oos_best["is"],
            "oos": oos_best["oos"],
            "full": oos_best["full"],
            "overlap_from_202501": oos_best["overlap_from_202501"],
            "rank_meta": oos_best["rank_meta"],
            "oos_gate_vs_baseline": _oos_gate(oos_best, baseline),
            "adoption": "observe_only · IS underperforms amount Top1",
        },
        "family_summary": family_summary,
        "fair_compound_vs_c18acc": {
            "c18acc": c18_fair,
            "copytrade_baseline_overlap": baseline["overlap_from_202501"],
            "copytrade_survivor_overlap": surviving["overlap_from_202501"],
            "copytrade_oos_robustness_overlap": oos_best["overlap_from_202501"],
        },
        "hypotheses": hypotheses,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    c = payload["contract"]
    lines = [
        "# 金額×RRG · 高手指紋軟排序 · 單槽複利",
        "",
        f"- window: `{c['window_start']}` → `{c['window_end']}` · OOS `{c['oos_cut']}`",
        f"- baseline: `{c['baseline']}`",
        f"- pool: `{c['pool']}`",
        f"- note: {c['identity_note']}",
        "",
        "## Hypotheses",
        "",
    ]
    for hid, h in payload["hypotheses"].items():
        lines.append(f"- **{hid}**: `{h.get('status')}` — {h.get('statement')}")
    lines.extend(["", "## Arms", ""])
    for a in payload["arms"]:
        g = a["oos_gate_vs_baseline"]["status"]
        rm = a["rank_meta"]
        lines.append(
            f"- `{a['rank_id']}` · {a['label']} · "
            f"switch%={rm.get('switch_pct')} · "
            f"IS c={a['is'].get('compound_slot_pct')} n={a['is'].get('n_cycles')} · "
            f"OOS c={a['oos'].get('compound_slot_pct')} n={a['oos'].get('n_cycles')} · "
            f"`{g}`"
        )
    surv = payload["gate_surviving"]
    lines.extend(
        [
            "",
            f"## Survivor (IS-selected): `{surv['rank_id']}`",
            f"- full compound={surv['full'].get('compound_slot_pct')}",
            f"- OOS compound={surv['oos'].get('compound_slot_pct')}",
            f"- overlap compound={surv['overlap_from_202501'].get('compound_slot_pct')}",
            "",
        ]
    )
    rob = payload.get("oos_robustness_candidate") or {}
    if rob:
        lines.extend(
            [
                f"## OOS robustness observe: `{rob.get('rank_id')}`",
                f"- {rob.get('label')}",
                f"- IS compound={ (rob.get('is') or {}).get('compound_slot_pct') } "
                f"OOS compound={ (rob.get('oos') or {}).get('compound_slot_pct') }",
                f"- gate={ (rob.get('oos_gate_vs_baseline') or {}).get('status') }",
                f"- adoption: {rob.get('adoption')}",
                "",
            ]
        )
    lines.extend(
        [
            "Research only · not Strategy/Order · not account identification.",
            "",
        ]
    )
    return "\n".join(lines)


def write_artifacts(
    payload: dict[str, Any],
    *,
    out_dir: Path | None = None,
    stamp: str | None = None,
) -> dict[str, Path]:
    out_dir = out_dir or (REPORTS_RESEARCH / "dual-branch-copytrade")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    stem = f"{stamp}_{FILE_STEM}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return {"json": json_path, "md": md_path}
