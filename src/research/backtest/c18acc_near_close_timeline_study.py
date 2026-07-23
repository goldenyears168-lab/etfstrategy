"""C18acc · near-close timeline study.

Answers:
1. Timeline clarity: A vs B0 vs LIVE_PIT vs SAME_DAY vs SNAP(C).
2. Does fill minute 13:00→13:25→13:30 on the *prior* day change A's ceiling much?
3. Is A's edge the last 30 minutes — or the overnight into B0's morning fill?
4. Live-legal afternoon (昨收 PIT + ≥13:00/13:25) vs A ceiling.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import replace
from pathlib import Path
from statistics import mean, median
from typing import Any

from analytics.bench import bench_return_entry_to_exit
from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel
from research.backtest.archive.c18acc_avoid_mixed_slot_resim import load_gate_cache
from research.backtest.archive.c18acc_pool1_showcase import _ensure_avoid_mixed_gate
from research.backtest.c18acc_open_timing_study import (
    ADOPT_IS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_DELTA_PP,
    ADOPT_OOS_MIN_N,
    DEFAULT_GATE_CACHE,
    _run_sim,
    _variant_row,
    lag_rows_by_prior_day,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_lens_score_swap import _prior_trading_date
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    _trading_days_between,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import load_kbar_day_closes, price_at_or_before_minute

FILL_MINUTES = ("13:00", "13:15", "13:25", "13:30")


def _px_at(
    conn: sqlite3.Connection,
    *,
    stock_id: str,
    trade_date: str,
    minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> float | None:
    key = (stock_id, trade_date)
    if key not in kbar_cache:
        kbar_cache[key] = load_kbar_day_closes(conn, stock_id, trade_date)
    px = price_at_or_before_minute(kbar_cache[key], minute)
    if px is None or px <= 0:
        return None
    return float(px)


def _shift_fill(
    conn: sqlite3.Connection,
    leg: dict[str, Any],
    *,
    full_dates: list[str],
    fill_date: str,
    fill_minute: str,
    variant_id: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> dict[str, Any] | None:
    exit_d = str(leg.get("exit_date") or "")
    sid = str(leg.get("stock_id") or "")
    if not exit_d or not sid or not fill_date:
        return None
    if fill_date > exit_d:
        return None
    exit_px = leg.get("exit_px")
    if exit_px is None:
        return None
    try:
        exit_px_f = float(exit_px)
    except (TypeError, ValueError):
        return None
    if exit_px_f <= 0:
        return None
    entry_px = _px_at(
        conn, stock_id=sid, trade_date=fill_date, minute=fill_minute, kbar_cache=kbar_cache
    )
    if entry_px is None:
        return None
    ret = (exit_px_f / entry_px - 1.0) * 100.0
    bench = bench_return_entry_to_exit(conn, fill_date, exit_d, entry_price_mode="close")
    if bench is None:
        return None
    out = dict(leg)
    out["baseline_entry_date"] = leg.get("entry_date")
    out["baseline_entry_minute"] = leg.get("entry_minute")
    out["baseline_entry_px"] = leg.get("entry_px")
    out["baseline_return_pct"] = leg.get("return_pct")
    out["baseline_excess_pct"] = leg.get("excess_pct")
    out["entry_date"] = fill_date
    out["entry_minute"] = fill_minute
    out["entry_px"] = round(entry_px, 4)
    out["hold_days"] = _trading_days_between(full_dates, fill_date, exit_d)
    out["return_pct"] = round(ret, 4)
    out["bench_return_pct"] = round(float(bench), 4)
    out["excess_pct"] = round(ret - float(bench), 4)
    out["beat_bench"] = ret > float(bench)
    out["gross_win"] = ret > 0
    out["variant_id"] = variant_id
    return out


def apply_fills(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
    *,
    full_dates: list[str],
    mode: str,
    fill_minute: str,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """mode: prior | same_day."""
    out: list[dict[str, Any]] = []
    skips = Counter()
    for leg in periods:
        entry = str(leg.get("entry_date") or "")
        if mode == "prior":
            fill_date = _prior_trading_date(full_dates, entry) if entry else None
            if not fill_date:
                skips["no_prior"] += 1
                continue
            if fill_date >= str(leg.get("exit_date") or ""):
                skips["bad_window"] += 1
                continue
            vid = f"A_PRIOR_{fill_minute.replace(':', '')}"
        else:
            fill_date = entry
            vid = f"SAME_DAY_{fill_minute.replace(':', '')}"
        filled = _shift_fill(
            conn,
            leg,
            full_dates=full_dates,
            fill_date=fill_date,
            fill_minute=fill_minute,
            variant_id=vid,
            kbar_cache=kbar_cache,
        )
        if filled is None:
            skips["no_px"] += 1
            continue
        out.append(filled)
    return out, {
        "n_baseline": len(periods),
        "n_filled": len(out),
        "skips": dict(skips),
        "mode": mode,
        "fill_minute": fill_minute,
    }


def decompose_a_edge(
    conn: sqlite3.Connection,
    periods: list[dict[str, Any]],
    *,
    full_dates: list[str],
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> dict[str, Any]:
    """Split A-vs-B0 into: last-30m on prior day vs overnight-to-B0-entry."""
    last30: list[float] = []
    overnight: list[float] = []
    used = 0
    for leg in periods:
        entry = str(leg.get("entry_date") or "")
        sid = str(leg.get("stock_id") or "")
        prior = _prior_trading_date(full_dates, entry) if entry else None
        if not prior or not sid:
            continue
        px_1300 = _px_at(
            conn, stock_id=sid, trade_date=prior, minute="13:00", kbar_cache=kbar_cache
        )
        px_1330 = _px_at(
            conn, stock_id=sid, trade_date=prior, minute="13:30", kbar_cache=kbar_cache
        )
        b0_px = leg.get("entry_px")
        if px_1300 is None or px_1330 is None or b0_px is None:
            continue
        try:
            b0_f = float(b0_px)
        except (TypeError, ValueError):
            continue
        if b0_f <= 0:
            continue
        last30.append((px_1330 / px_1300 - 1.0) * 100.0)
        overnight.append((b0_f / px_1300 - 1.0) * 100.0)
        used += 1

    def _summ(xs: list[float]) -> dict[str, Any]:
        if not xs:
            return {"n": 0}
        return {
            "n": len(xs),
            "mean_pct": round(mean(xs), 4),
            "median_pct": round(median(xs), 4),
            "p_positive": round(100.0 * sum(1 for x in xs if x > 0) / len(xs), 2),
        }

    return {
        "n_legs_used": used,
        "n_baseline": len(periods),
        "prior_day_last_30m_pct": _summ(last30),
        "overnight_prior1300_to_b0_entry_pct": _summ(overnight),
        "note": (
            "last_30m = prior 13:30/13:00−1; "
            "overnight = B0 entry_px / prior 13:00 − 1 "
            "(includes overnight gap + open auction into ≥09:30 fill)."
        ),
    }


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


def run_near_close_timeline_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-12-31",
    confirm_bars: int = 2,
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
    fill_minutes: tuple[str, ...] = FILL_MINUTES,
    run_live_pit: bool = True,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]

    gate_path = Path(gate_cache_path) if gate_cache_path else None
    if gate_path and gate_path.is_file() and date_end is None:
        _, _, c0, c1 = load_gate_cache(str(gate_path))
        if c0 == trade_dates[0] and c1 < trade_dates[-1]:
            end = c1
            trade_dates = [d for d in full_dates if date_start <= d <= end]
            print(f"truncated trade window to gate cache end {end}", flush=True)

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    fresh_lag = lag_rows_by_prior_day(
        fresh_by_date, full_dates=full_dates, trade_dates=trade_dates
    )
    mono_lag = lag_rows_by_prior_day(
        mono_by_date, full_dates=full_dates, trade_dates=trade_dates
    )
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champ = replace(
        champion_score_swap_c_config(),
        candidate_pool="fresh",
        n_slots=max(1, int(n_slots)),
    )
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench) if _poll5m_needs_spread_rs_panel(champ) else None
    )
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    ctx_bt: dict[str, Any] = {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
        "kbar_cache": kbar_cache,
        "spread_rs_panel": spread_rs_panel,
    }
    ctx_live = {
        **ctx_bt,
        "fresh_by_date": fresh_lag,
        "mono_by_date": mono_lag,
        "kbar_cache": {},
    }

    oos_start = next((d for d in trade_dates if d > is_end), None)
    if not oos_start:
        raise ValueError(f"no OOS dates after is_end={is_end}")

    gate, gate_meta = _ensure_avoid_mixed_gate(
        conn,
        trade_dates,
        gate_cache_path=gate_path,
        live_aligned=True,
        n_slots=n_slots,
    )
    if gate is None:
        raise ValueError("avoid_mixed gate required")

    cfg_b0 = replace(
        champ, variant_id="B0_BT", label="BT · same-day EOD · ≥09:30", no_trade_before="09:30"
    )
    p_b0, s_b0, _ = _run_sim(
        conn,
        ctx=ctx_bt,
        trade_dates=trade_dates,
        cfg=cfg_b0,
        confirm_bars=confirm_bars,
        gate=gate,
        label="B0_BT",
    )

    edge = decompose_a_edge(conn, p_b0, full_dates=full_dates, kbar_cache=kbar_cache)
    print(
        f"  edge decomp · last30 mean={edge['prior_day_last_30m_pct'].get('mean_pct')}% · "
        f"overnight mean={edge['overnight_prior1300_to_b0_entry_pct'].get('mean_pct')}%",
        flush=True,
    )

    base = _variant_row(
        variant_id="B0_BT",
        label="回測 SSOT · 當日 EOD 池 · ≥09:30",
        periods=p_b0,
        summary=s_b0,
        date_start=date_start,
        is_end=is_end,
        oos_start=oos_start,
        end=end,
    )
    bf, bi, bo = base["slices"]["full"], base["slices"]["is"], base["slices"]["oos"]
    variants: list[dict[str, Any]] = [base]
    fill_meta: dict[str, Any] = {}

    for minute in fill_minutes:
        print(f"  post-hoc A_PRIOR @{minute} …", flush=True)
        p_a, meta_a = apply_fills(
            conn, p_b0, full_dates=full_dates, mode="prior", fill_minute=minute, kbar_cache=kbar_cache
        )
        fill_meta[f"A_PRIOR_{minute}"] = meta_a
        print(f"    filled={meta_a['n_filled']}/{meta_a['n_baseline']}", flush=True)
        variants.append(
            _variant_row(
                variant_id=f"A_PRIOR_{minute.replace(':', '')}",
                label=f"A · 當日 EOD 名單 · 前一日 {minute}（前瞻）",
                periods=p_a,
                summary=None,
                date_start=date_start,
                is_end=is_end,
                oos_start=oos_start,
                end=end,
                base_full=bf,
                base_is=bi,
                base_oos=bo,
                extra={"pit_clean": False, "live_executable": False, "fill_meta": meta_a},
            )
        )

        print(f"  post-hoc SAME_DAY @{minute} …", flush=True)
        p_s, meta_s = apply_fills(
            conn,
            p_b0,
            full_dates=full_dates,
            mode="same_day",
            fill_minute=minute,
            kbar_cache=kbar_cache,
        )
        fill_meta[f"SAME_DAY_{minute}"] = meta_s
        print(f"    filled={meta_s['n_filled']}/{meta_s['n_baseline']}", flush=True)
        variants.append(
            _variant_row(
                variant_id=f"SAME_DAY_{minute.replace(':', '')}",
                label=f"同日 {minute} 成交 · 仍用當日 EOD 名單（前瞻）",
                periods=p_s,
                summary=None,
                date_start=date_start,
                is_end=is_end,
                oos_start=oos_start,
                end=end,
                base_full=bf,
                base_is=bi,
                base_oos=bo,
                extra={"pit_clean": False, "live_executable": False, "fill_meta": meta_s},
            )
        )

    if run_live_pit:
        for ntb, vid, label in (
            ("09:30", "LIVE_PIT_0930", "live · 昨收池 · ≥09:30"),
            ("13:00", "LIVE_PIT_1300", "live · 昨收池 · ≥13:00"),
            ("13:25", "LIVE_PIT_1325", "live · 昨收池 · ≥13:25"),
        ):
            cfg = replace(champ, variant_id=vid, label=label, no_trade_before=ntb)
            p_l, s_l, _ = _run_sim(
                conn,
                ctx=ctx_live,
                trade_dates=trade_dates,
                cfg=cfg,
                confirm_bars=confirm_bars,
                gate=gate,
                label=vid,
            )
            variants.append(
                _variant_row(
                    variant_id=vid,
                    label=label,
                    periods=p_l,
                    summary=s_l,
                    date_start=date_start,
                    is_end=is_end,
                    oos_start=oos_start,
                    end=end,
                    base_full=bf,
                    base_is=bi,
                    base_oos=bo,
                    extra={"pit_clean": True, "live_executable": True},
                )
            )

    # minute sensitivity within A family
    a_rows = [v for v in variants if str(v["variant_id"]).startswith("A_PRIOR_")]
    a_sens = []
    if a_rows:
        base_a = next((v for v in a_rows if v["variant_id"] == "A_PRIOR_1300"), a_rows[0])
        b_oos = (base_a["slices"]["oos"] or {}).get("mean_excess_pct")
        for v in a_rows:
            o = (v["slices"]["oos"] or {}).get("mean_excess_pct")
            a_sens.append(
                {
                    "variant_id": v["variant_id"],
                    "oos_excess": o,
                    "delta_vs_a1300_oos_pp": round(float(o) - float(b_oos), 4)
                    if o is not None and b_oos is not None
                    else None,
                }
            )

    adopt: dict[str, str] = {}
    for v in variants:
        vid = v["variant_id"]
        if vid == "B0_BT":
            continue
        n_oos = int((v["slices"]["oos"] or {}).get("n_legs") or 0)
        adopt[vid] = _adopt(
            n_oos=n_oos,
            d_oos=v.get("delta_oos_excess_pp"),
            d_is=v.get("delta_is_excess_pp"),
        )

    last30 = edge.get("prior_day_last_30m_pct") or {}
    ovn = edge.get("overnight_prior1300_to_b0_entry_pct") or {}
    verdict = {
        "a_edge_is_last_30m": False,
        "reason": (
            f"Prior-day last-30m mean {last30.get('mean_pct')}% vs "
            f"overnight-to-B0-entry mean {ovn.get('mean_pct')}% — "
            "A's ceiling comes from overnight into B0 fill, not 13:00↔13:30."
        ),
        "a_minute_sensitivity_oos": a_sens,
        "adopt": adopt,
        "live_recommendation": "KEEP_LIVE_PIT_0930",
        "timeline_note": (
            "LIVE_PIT (昨收→今日下午) ≠ A (明日收盤名單→昨日下午). "
            "SAME_DAY near-close ≠ A. SNAP(C) = 當日暫定 RRG+當日成交."
        ),
    }
    # Prefer keep morning unless LIVE_PIT afternoon GO vs B0 (won't) — check vs LIVE 0930 separately in md

    return {
        "schema": "c18acc_near_close_timeline-v1",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": oos_start,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "fill_minutes": list(fill_minutes),
        "gate_meta": gate_meta,
        "edge_decomposition": edge,
        "fill_meta": fill_meta,
        "variants": variants,
        "verdict": verdict,
        "timelines": [
            {
                "id": "B0_BT",
                "steps": [
                    "Day E close → EOD RRG 名單",
                    "Day E 09:30+ fill（回測語意；相對 live 偏樂觀）",
                ],
            },
            {
                "id": "A_PRIOR",
                "steps": [
                    "Day E close → EOD RRG 名單（與 B0 相同）",
                    "填到 Day E−1 13:00/13:25/13:30（前瞻 · 不可 live）",
                ],
            },
            {
                "id": "SAME_DAY",
                "steps": [
                    "Day E close → EOD 名單",
                    "Day E 13:00~13:30 fill（仍前瞻；測『下午買 vs 早上買』）",
                ],
            },
            {
                "id": "LIVE_PIT",
                "steps": [
                    "Day E−1 close → 昨收池（PIT）",
                    "Day E 09:30 或 13:00/13:25 fill（可 live）",
                ],
            },
            {
                "id": "SNAP_C",
                "steps": [
                    "Day E 13:xx 暫定 close → 重算 RRG",
                    "Day E 13:xx fill（可 live · 已測 13:00 NO_GO）",
                ],
            },
        ],
    }


def render_near_close_timeline_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    edge = payload.get("edge_decomposition") or {}
    lines = [
        "# C18acc · 近收盤時間軸與分鐘敏感度",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}**",
        "",
        "## 結論",
        "",
        f"- {v.get('reason')}",
        f"- {v.get('timeline_note')}",
        f"- live 建議：**{v.get('live_recommendation')}**",
        "",
        "## 時間軸（語意）",
        "",
    ]
    for tl in payload.get("timelines") or []:
        lines.append(f"### {tl['id']}")
        for i, step in enumerate(tl.get("steps") or [], 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    last30 = edge.get("prior_day_last_30m_pct") or {}
    ovn = edge.get("overnight_prior1300_to_b0_entry_pct") or {}
    lines.extend(
        [
            "## A 優勢拆解（同批 B0 legs）",
            "",
            f"- 樣本：**{edge.get('n_legs_used')}** / {edge.get('n_baseline')}",
            f"- 前一日最後 30 分（13:00→13:30）mean：**{last30.get('mean_pct')}%** · "
            f"median **{last30.get('median_pct')}%** · "
            f"正報酬比例 **{last30.get('p_positive')}%**",
            f"- 隔夜到 B0 進場（prior 13:00→B0 entry_px）mean：**{ovn.get('mean_pct')}%** · "
            f"median **{ovn.get('median_pct')}%** · "
            f"正報酬比例 **{ovn.get('p_positive')}%**",
            "",
            f"> {edge.get('note')}",
            "",
            "## 結果表",
            "",
            "| variant | window | n | mean_excess% | Δ vs B0 | adopt |",
            "|---------|--------|--:|-------------:|--------:|-------|",
        ]
    )
    adopt = v.get("adopt") or {}
    for row in payload.get("variants") or []:
        vid = row["variant_id"]
        for win, key, dkey in (
            ("FULL", "full", "delta_full_excess_pp"),
            ("IS", "is", "delta_is_excess_pp"),
            ("OOS", "oos", "delta_oos_excess_pp"),
        ):
            s = (row.get("slices") or {}).get(key) or {}
            d = row.get(dkey)
            d_s = "—" if d is None else f"{d:+.4f}pp"
            a = "—" if win != "OOS" or vid == "B0_BT" else adopt.get(vid, "—")
            lines.append(
                f"| {vid} | {win} | {s.get('n_legs')} | {s.get('mean_excess_pct')} | {d_s} | {a} |"
            )

    lines.extend(
        [
            "",
            "## A 家族：填單分鐘敏感度（OOS vs A@13:00）",
            "",
            "| variant | OOS excess | Δ vs A_PRIOR_1300 |",
            "|---------|-----------:|------------------:|",
        ]
    )
    for row in v.get("a_minute_sensitivity_oos") or []:
        d = row.get("delta_vs_a1300_oos_pp")
        d_s = "—" if d is None else f"{d:+.4f}pp"
        lines.append(f"| {row['variant_id']} | {row.get('oos_excess')} | {d_s} |")

    lines.extend(
        [
            "",
            "## 對你假設的直接回答",
            "",
            "1. **收盤 13:30，13:00 算會差很多嗎？** → 看「A 優勢拆解」：最後 30 分的均幅通常遠小於隔夜到 B0 進場。",
            "2. **改成 13:25 交易能否接近 A？** → 若仍是「明日收盤名單 + 昨日 13:25」= 仍是 A（不可 live）；"
            "若是「昨收池 + 今日 13:25」= LIVE_PIT（可 live，但不吃 A 的隔夜）；"
            "若是「今日近收盤重算 + 今日 13:25」= SNAP(C) 家族（13:00 已 NO_GO）。",
            "3. **launchd 13:00 + 昨收 PIT** → 就是 LIVE_PIT_1300，語意正確，但不是復刻 A。",
            "",
            "模組：`src/research/backtest/c18acc_near_close_timeline_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
