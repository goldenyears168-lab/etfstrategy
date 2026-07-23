"""POOL1 graduation showcase · S2 vs POOL1 poll_5m diff bundle for HTML."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from research.backtest.rrg_mono_score_swap_c import (
    build_c18acc_poll5m_s2_timeline_legs,
)
from research.backtest.archive.c18acc_hold3d_event_study import C18ACC_UNCONSTRAINED_SLOTS

DEFAULT_AVOID_MIXED_GATE_CACHE = (
    Path(__file__).resolve().parents[4]
    / "reports"
    / "research"
    / "rrg"
    / "20260711_c18acc_avoid_mixed_gate_cache.json"
)


def _live_gate_cache_path(
    dates: list[str],
    *,
    gate_anchor_minute: str = "13:00",
) -> Path:
    if not dates:
        raise ValueError("dates required for live gate cache path")
    root = DEFAULT_AVOID_MIXED_GATE_CACHE.parent
    anchor = str(gate_anchor_minute or "13:00").replace(":", "")
    return root / (
        f"c18acc_avoid_mixed_gate_live_poll5m_a{anchor}_{dates[0]}_{dates[-1]}.json"
    )


def _leg_fingerprint(leg: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(leg.get("stock_id", "")),
        str(leg.get("entry_date", "")),
        str(leg.get("exit_date", "")),
        str(leg.get("exit_reason") or ""),
    )


def _pct_delta(a: Any, b: Any) -> float | None:
    if a is None or b is None:
        return None
    return round(float(b) - float(a), 4)


def build_pool1_showcase_bundle(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
) -> dict[str, Any]:
    """Run S2-P5M vs POOL1-P5M on same window · diff legs + KPI rows."""
    s2_legs, s2_exec, _, s2_meta = build_c18acc_poll5m_s2_timeline_legs(
        conn,
        dates,
        candidate_pool="fresh",
        variant_id="S2-P5M",
        n_slots=n_slots,
        capital_ntd=capital_ntd,
        accel_sell_bypass_on_spread_lost=False,
    )
    p1_legs, p1_exec, _, p1_meta = build_c18acc_poll5m_s2_timeline_legs(
        conn,
        dates,
        candidate_pool="fresh_union_accel",
        variant_id="POOL1-P5M",
        n_slots=n_slots,
        capital_ntd=capital_ntd,
        accel_sell_bypass_on_spread_lost=False,
    )

    s2_fp = {_leg_fingerprint(lg) for lg in s2_legs}
    p1_fp = {_leg_fingerprint(lg) for lg in p1_legs}
    pool1_only: list[dict[str, Any]] = []
    s2_only: list[dict[str, Any]] = []
    tagged_p1_legs: list[dict[str, Any]] = []
    for lg in p1_legs:
        row = dict(lg)
        fp = _leg_fingerprint(row)
        row["showcase_tag"] = "pool1_only" if fp not in s2_fp else "shared"
        tagged_p1_legs.append(row)
        if row["showcase_tag"] == "pool1_only":
            pool1_only.append(row)
    for lg in s2_legs:
        fp = _leg_fingerprint(lg)
        if fp not in p1_fp:
            s2_only.append(dict(lg))

    comparison = {
        "s2_mean_excess_pct": s2_meta.get("mean_excess_pct"),
        "pool1_mean_excess_pct": p1_meta.get("mean_excess_pct"),
        "delta_excess_pp": _pct_delta(s2_meta.get("mean_excess_pct"), p1_meta.get("mean_excess_pct")),
        "s2_swaps": s2_meta.get("swaps_total"),
        "pool1_swaps": p1_meta.get("swaps_total"),
        "delta_swaps": int(p1_meta.get("swaps_total") or 0) - int(s2_meta.get("swaps_total") or 0),
        "s2_force_exits": s2_meta.get("force_exits"),
        "pool1_force_exits": p1_meta.get("force_exits"),
    }

    case_cards = []
    for lg in pool1_only:
        case_cards.append(
            {
                "stock_id": lg["stock_id"],
                "stock_name": lg.get("stock_name", ""),
                "pool_tag": lg.get("pool_tag", "—"),
                "entry_date": lg["entry_date"],
                "exit_date": lg["exit_date"],
                "entry_px": lg.get("entry_px"),
                "exit_px": lg.get("exit_px"),
                "entry_minute": lg.get("entry_minute"),
                "exit_minute": lg.get("exit_minute"),
                "exit_reason": lg.get("exit_reason") or "—",
                "return_pct": lg.get("return_pct"),
                "pool_tag_note": (
                    "fresh mono 新进"
                    if lg.get("pool_tag") == "fresh"
                    else "mono_tier2 · 4d avg accel>0 · 非 fresh"
                ),
            }
        )

    return {
        "schema": "c18acc_pool1_showcase-v1",
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "n_trade_dates": len(dates),
        "timing_mode": "poll_5m",
        "comparison": comparison,
        "s2_meta": s2_meta,
        "pool1_meta": p1_meta,
        "s2_legs": s2_legs,
        "pool1_legs": tagged_p1_legs,
        "pool1_only_legs": pool1_only,
        "s2_only_legs": s2_only,
        "case_cards": case_cards,
    }


def build_bypass_showcase_bundle(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
) -> dict[str, Any]:
    """Run POOL1-P5M vs POOL1+bypass on same window · diff legs + KPI rows."""
    base_legs, _, _, base_meta = build_c18acc_poll5m_s2_timeline_legs(
        conn,
        dates,
        candidate_pool="fresh_union_accel",
        variant_id="POOL1-P5M",
        n_slots=n_slots,
        capital_ntd=capital_ntd,
        accel_sell_bypass_on_spread_lost=False,
    )
    bypass_legs, _, _, bypass_meta = build_c18acc_poll5m_s2_timeline_legs(
        conn,
        dates,
        candidate_pool="fresh_union_accel",
        variant_id="POOL1-bypass-P5M",
        n_slots=n_slots,
        capital_ntd=capital_ntd,
        accel_sell_bypass_on_spread_lost=True,
    )

    base_fp = {_leg_fingerprint(lg) for lg in base_legs}
    bypass_fp = {_leg_fingerprint(lg) for lg in bypass_legs}
    bypass_only: list[dict[str, Any]] = []
    base_only: list[dict[str, Any]] = []
    tagged_bypass_legs: list[dict[str, Any]] = []
    for lg in bypass_legs:
        row = dict(lg)
        fp = _leg_fingerprint(row)
        row["showcase_tag"] = "pool1_only" if fp not in base_fp else "shared"
        tagged_bypass_legs.append(row)
        if row["showcase_tag"] == "pool1_only":
            bypass_only.append(row)
    for lg in base_legs:
        fp = _leg_fingerprint(lg)
        if fp not in bypass_fp:
            base_only.append(dict(lg))

    comparison = {
        "base_mean_excess_pct": base_meta.get("mean_excess_pct"),
        "bypass_mean_excess_pct": bypass_meta.get("mean_excess_pct"),
        "delta_excess_pp": _pct_delta(
            base_meta.get("mean_excess_pct"), bypass_meta.get("mean_excess_pct")
        ),
        "base_swaps": base_meta.get("swaps_total"),
        "bypass_swaps": bypass_meta.get("swaps_total"),
        "delta_swaps": int(bypass_meta.get("swaps_total") or 0)
        - int(base_meta.get("swaps_total") or 0),
        "base_force_exits": base_meta.get("force_exits"),
        "bypass_force_exits": bypass_meta.get("force_exits"),
        "base_n_legs": base_meta.get("n_executed"),
        "bypass_n_legs": bypass_meta.get("n_executed"),
        "delta_n_legs": int(bypass_meta.get("n_executed") or 0)
        - int(base_meta.get("n_executed") or 0),
    }

    case_cards = []
    for lg in bypass_only:
        case_cards.append(
            {
                "stock_id": lg["stock_id"],
                "stock_name": lg.get("stock_name", ""),
                "pool_tag": lg.get("pool_tag", "—"),
                "entry_date": lg["entry_date"],
                "exit_date": lg["exit_date"],
                "entry_px": lg.get("entry_px"),
                "exit_px": lg.get("exit_px"),
                "entry_minute": lg.get("entry_minute"),
                "exit_minute": lg.get("exit_minute"),
                "exit_reason": lg.get("exit_reason") or "—",
                "return_pct": lg.get("return_pct"),
                "pool_tag_note": (
                    "bypass 獨有 leg · spread≤0 加速換倉"
                    if lg.get("exit_reason") == "swap"
                    else "bypass 獨有 leg"
                ),
            }
        )
    for lg in base_only:
        case_cards.append(
            {
                "stock_id": lg["stock_id"],
                "stock_name": lg.get("stock_name", ""),
                "pool_tag": lg.get("pool_tag", "—"),
                "entry_date": lg["entry_date"],
                "exit_date": lg["exit_date"],
                "entry_px": lg.get("entry_px"),
                "exit_px": lg.get("exit_px"),
                "entry_minute": lg.get("entry_minute"),
                "exit_minute": lg.get("exit_minute"),
                "exit_reason": lg.get("exit_reason") or "—",
                "return_pct": lg.get("return_pct"),
                "pool_tag_note": "base POOL1 獨有 leg（bypass 未走這條路）",
                "base_only": True,
            }
        )

    return {
        "schema": "c18acc_bypass_showcase-v1",
        "showcase_mode": "bypass_diff",
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "n_trade_dates": len(dates),
        "timing_mode": "poll_5m",
        "comparison": comparison,
        "base_meta": base_meta,
        "bypass_meta": bypass_meta,
        "base_legs": base_legs,
        "bypass_legs": tagged_bypass_legs,
        "bypass_only_legs": bypass_only,
        "base_only_legs": base_only,
        "case_cards": case_cards,
        # aliases for shared render helpers
        "pool1_meta": bypass_meta,
        "pool1_legs": tagged_bypass_legs,
        "pool1_only_legs": bypass_only,
    }


def _meta_row(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "mean_excess_pct": meta.get("mean_excess_pct"),
        "swaps_total": meta.get("swaps_total"),
        "force_exits": meta.get("force_exits"),
        "n_executed": meta.get("n_executed"),
        "display_code": meta.get("display_code"),
        "variant_id": meta.get("variant_id"),
    }


def _load_avoid_mixed_gate(
    dates: list[str],
    *,
    gate_cache_path: str | Path | None,
) -> dict[str, set[str]] | None:
    if not dates or gate_cache_path is None:
        return None
    path = Path(gate_cache_path)
    if not path.is_file():
        return None
    from research.backtest.archive.c18acc_avoid_mixed_slot_resim import load_gate_cache

    gate, _meta, c0, c1 = load_gate_cache(str(path))
    if c0 != dates[0] or c1 != dates[-1]:
        raise ValueError(
            f"avoid_mixed gate cache window {c0}..{c1} != showcase {dates[0]}..{dates[-1]}"
        )
    return gate


def _ensure_avoid_mixed_gate(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    gate_cache_path: str | Path | None,
    live_aligned: bool,
    n_slots: int = 3,
) -> tuple[dict[str, set[str]] | None, dict[str, Any]]:
    if not dates or gate_cache_path is None:
        return None, {}
    path = Path(gate_cache_path)
    if path.is_file():
        from research.backtest.archive.c18acc_avoid_mixed_slot_resim import load_gate_cache

        gate, meta, c0, c1 = load_gate_cache(str(path))
        if c0 != dates[0] or c1 != dates[-1]:
            raise ValueError(
                f"avoid_mixed gate cache window {c0}..{c1} != showcase {dates[0]}..{dates[-1]}"
            )
        return gate, meta

    if not live_aligned:
        return None, {}

    from research.backtest.archive.c18acc_avoid_mixed_slot_resim import (
        build_avoid_mixed_gate_lookup_live,
        save_gate_cache,
    )
    from research.backtest.archive.c18acc_hold3d_event_study import (
        _prepare_c18acc_context,
        c18acc_live_s2_config,
    )

    cfg = c18acc_live_s2_config(n_slots=n_slots)
    ntb = str(cfg.no_trade_before or "13:00")
    print(
        f"live-aligned spread gate: building {dates[0]}→{dates[-1]} "
        f"({len(dates)} td · poll_5m · pool={cfg.candidate_pool} · "
        f"anchor≥{ntb}) …",
        flush=True,
    )
    ctx = _prepare_c18acc_context(conn, dates)
    gate, meta = build_avoid_mixed_gate_lookup_live(
        conn,
        trade_dates=dates,
        ctx=ctx,
        cfg=cfg,
        no_trade_before=ntb,
        gate_anchor_minute=ntb,
        passthrough_pool=True,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    save_gate_cache(
        str(path),
        gate=gate,
        meta=meta,
        date_start=dates[0],
        date_end=dates[-1],
    )
    print(f"live-aligned spread gate saved → {path}", flush=True)
    return gate, meta


def build_triple_champion_showcase_bundle(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
    avoid_spread_mixed: bool = True,
    avoid_mixed_gate_cache: str | Path | None = None,
    live_aligned: bool = False,
    confirm_bars: int | None = None,
    champion_only: bool = False,
) -> dict[str, Any]:
    """Champion vs W4P4-dual vs S2-fresh · poll_5m · same window.

    ``live_aligned=True`` follows current Order / strategy SSOT:
    fresh-only · E@13:00 · avg_accel entry · confirm_bars=1 · avoid mixed @ 13:00.
    """
    from research.backtest.rrg_mono_score_swap_c import champion_score_swap_c_config

    champ_ssot = champion_score_swap_c_config()
    # live SSOT confirm=1（ops alpha）；舊 research baseline 常為 2
    eff_confirm = int(
        confirm_bars
        if confirm_bars is not None
        else (1 if live_aligned else 1)
    )
    ntb = str(champ_ssot.no_trade_before or "13:00")
    gate_cache = (
        Path(avoid_mixed_gate_cache)
        if avoid_mixed_gate_cache is not None
        else (
            _live_gate_cache_path(dates, gate_anchor_minute=ntb)
            if live_aligned
            else DEFAULT_AVOID_MIXED_GATE_CACHE
        )
    )
    gate_meta: dict[str, Any] = {}
    if avoid_spread_mixed:
        champ_gate, gate_meta = _ensure_avoid_mixed_gate(
            conn,
            dates,
            gate_cache_path=gate_cache,
            live_aligned=live_aligned,
            n_slots=n_slots,
        )
    else:
        champ_gate = None

    if live_aligned:
        champ_pool: str = str(champ_ssot.candidate_pool or "fresh")
        champ_vid = "C18acc-live-P5M"
        champ_spec = (
            f"現行 Order SSOT · fresh · E@{ntb} · entry avg_accel · "
            f"confirm_bars={eff_confirm} · avoid spread_mixed @{ntb} · "
            f"G_R5_12 prior5d≤{float(champ_ssot.entry_prior_ret_max or 0.12):.0%}"
        )
        champ_short = "採納版 · 現行 live"
    else:
        champ_pool = "fresh_union_accel"
        champ_vid = "champion-P5M"
        champ_spec = "新進領先＋四日加速雙池 · 價差轉弱可換倉 · avoid spread_mixed · 歷史 Champion"
        champ_short = "採納版 · 雙池＋價差放行"

    if champion_only:
        s2_legs: list[dict[str, Any]] = []
        s2_meta: dict[str, Any] = {"variant_id": "S2-fresh-P5M", "display_code": "S2-fresh-P5M"}
        dual_legs: list[dict[str, Any]] = []
        dual_meta: dict[str, Any] = {"variant_id": "W4P4-dual", "display_code": "W4P4-dual"}
    else:
        s2_legs, _, _, s2_meta = build_c18acc_poll5m_s2_timeline_legs(
            conn,
            dates,
            candidate_pool="fresh",
            variant_id="S2-fresh-P5M",
            n_slots=n_slots,
            capital_ntd=capital_ntd,
            accel_sell_bypass_on_spread_lost=False,
        )
        dual_legs, _, _, dual_meta = build_c18acc_poll5m_s2_timeline_legs(
            conn,
            dates,
            candidate_pool="fresh_union_accel",
            variant_id="W4P4-dual",
            n_slots=n_slots,
            capital_ntd=capital_ntd,
            accel_sell_bypass_on_spread_lost=True,
            extra_config={
                "variant_id": "W4P4-dual",
                "quad_force_exit_pause_spread_rs_positive": True,
                "max_swaps_per_day": 2,
                "fill_mode": "batch_topk",
                "fill_repeat": True,
            },
        )

    champ_legs, _, _, champ_meta = build_c18acc_poll5m_s2_timeline_legs(
        conn,
        dates,
        candidate_pool=champ_pool,  # type: ignore[arg-type]
        variant_id=champ_vid,
        n_slots=n_slots,
        capital_ntd=capital_ntd,
        accel_sell_bypass_on_spread_lost=True,
        entry_gate_by_date=champ_gate,
        confirm_bars=eff_confirm,
    )
    if live_aligned:
        champ_meta["live_aligned"] = True
        champ_meta["confirm_bars"] = eff_confirm
        champ_meta["confirm_bars_note"] = f"{eff_confirm} · live Order SSOT"
        champ_meta["no_trade_before"] = ntb
        champ_meta["pyramid_add_note"] = (
            "Order layer underwater_rebound pyramid add 已採納；"
            "本頁 cinema NAV 不含第二單位加碼（見 c18acc_pyramid_add）"
        )
        if gate_meta:
            champ_meta["gate_meta"] = gate_meta

    tracks = {
        "s2_fresh": {
            "key": "s2_fresh",
            "label": "基準版（S2-fresh）",
            "short_label": "基準版 · 僅新進領先",
            "spec_note": "僅 fresh 新進領先池 · 無加速補充 · 無價差放行",
            "legs": s2_legs,
            "meta": s2_meta,
        },
        "champion": {
            "key": "champion",
            "label": "採納版（Champion）",
            "short_label": champ_short,
            "spec_note": champ_spec,
            "legs": champ_legs,
            "meta": champ_meta,
        },
        "dual": {
            "key": "dual",
            "label": "對照版（W4P4-dual）",
            "short_label": "對照版 · 進階換倉堆疊",
            "spec_note": "採納版＋價差暫停強制賣出 · 每日最多換倉 2 次 · 可重複補倉",
            "legs": dual_legs,
            "meta": dual_meta,
        },
    }

    fp_by_track = {
        key: {_leg_fingerprint(lg) for lg in tr["legs"]} for key, tr in tracks.items()
    }
    for key, tr in tracks.items():
        others = {k: fp for k, fp in fp_by_track.items() if k != key}
        union_others = set().union(*others.values()) if others else set()
        tr["only_legs"] = [
            dict(lg)
            for lg in tr["legs"]
            if _leg_fingerprint(lg) not in union_others
        ]

    s2_row = _meta_row(s2_meta)
    champ_row = _meta_row(champ_meta)
    dual_row = _meta_row(dual_meta)

    def _rank_excess() -> list[str]:
        ranked = sorted(
            tracks.items(),
            key=lambda kv: float(kv[1]["meta"].get("mean_excess_pct") or -999),
            reverse=True,
        )
        return [k for k, _ in ranked]

    excess_rank = _rank_excess()
    best_excess_key = excess_rank[0] if excess_rank else "champion"

    return {
        "schema": "c18acc_triple_champion_showcase-v1",
        "showcase_mode": "triple_champion",
        "live_aligned": live_aligned,
        "confirm_bars": eff_confirm,
        "gate_cache_path": str(gate_cache) if avoid_spread_mixed else None,
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "n_trade_dates": len(dates),
        "timing_mode": "poll_5m",
        "tracks": tracks,
        "comparison": {
            "s2_fresh": s2_row,
            "champion": champ_row,
            "dual": dual_row,
            "best_excess_track": best_excess_key,
            "champion_vs_s2_excess_pp": _pct_delta(
                s2_meta.get("mean_excess_pct"), champ_meta.get("mean_excess_pct")
            ),
            "dual_vs_champion_excess_pp": _pct_delta(
                champ_meta.get("mean_excess_pct"), dual_meta.get("mean_excess_pct")
            ),
            "dual_vs_s2_excess_pp": _pct_delta(
                s2_meta.get("mean_excess_pct"), dual_meta.get("mean_excess_pct")
            ),
        },
        # default timeline focus track
        "pool1_legs": champ_legs,
        "pool1_meta": champ_meta,
    }


def build_unconstrained_champion_showcase_bundle(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    capital_ntd: float = 10_000.0,
    avoid_spread_mixed: bool = True,
    avoid_mixed_gate_cache: str | Path | None = None,
    confirm_bars: int = 2,
    n_slots: int = C18ACC_UNCONSTRAINED_SLOTS,
) -> dict[str, Any]:
    """All qualifying POOL1 entries · no slot cap · no score swap · live-aligned gate."""
    gate_cache = (
        Path(avoid_mixed_gate_cache)
        if avoid_mixed_gate_cache is not None
        else _live_gate_cache_path(dates)
    )
    gate_meta: dict[str, Any] = {}
    if avoid_spread_mixed:
        champ_gate, gate_meta = _ensure_avoid_mixed_gate(
            conn,
            dates,
            gate_cache_path=gate_cache,
            live_aligned=True,
            n_slots=3,
        )
    else:
        champ_gate = None

    legs, _, _, meta = build_c18acc_poll5m_s2_timeline_legs(
        conn,
        dates,
        candidate_pool="fresh_union_accel",
        variant_id="unconstrained-P5M",
        n_slots=n_slots,
        capital_ntd=capital_ntd,
        accel_sell_bypass_on_spread_lost=True,
        entry_gate_by_date=champ_gate,
        confirm_bars=int(confirm_bars),
        extra_config={"max_swaps_per_day": 0},
    )
    meta["live_aligned"] = True
    meta["unconstrained"] = True
    meta["confirm_bars"] = int(confirm_bars)
    meta["n_slots_effective"] = n_slots
    if gate_meta:
        meta["gate_meta"] = gate_meta

    spec = (
        f"全訊號 · {n_slots} slots · max_swaps=0 · POOL1 passthrough · "
        f"confirm_bars={confirm_bars} · avoid spread_mixed @ 09:30"
    )
    track = {
        "key": "champion",
        "label": "全訊號版（無槽位 · 無換倉）",
        "short_label": "全訊號 · 無槽位限制",
        "spec_note": spec,
        "legs": legs,
        "meta": meta,
    }
    champ_row = _meta_row(meta)
    return {
        "schema": "c18acc_unconstrained_showcase-v1",
        "showcase_mode": "unconstrained_champion",
        "unconstrained": True,
        "live_aligned": True,
        "confirm_bars": int(confirm_bars),
        "gate_cache_path": str(gate_cache) if avoid_spread_mixed else None,
        "date_start": dates[0] if dates else None,
        "date_end": dates[-1] if dates else None,
        "n_trade_dates": len(dates),
        "timing_mode": "poll_5m",
        "tracks": {"champion": track},
        "comparison": {"champion": champ_row},
        "pool1_legs": legs,
        "pool1_meta": meta,
    }
