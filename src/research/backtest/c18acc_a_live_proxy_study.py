"""C18acc · can we live-replicate A (PRIOR_1300)?

A = EOD selection + fill prior day 13:00 → look-ahead · not live-executable.

Proxies backtested here:
- B0_BT: backtest SSOT · same-day EOD pool · fill ≥09:30
- A_ILLEGAL: post-hoc prior-day 13:00 on B0 legs (ceiling)
- SAME_DAY_1300: post-hoc same-day 13:00 on B0 legs (still LA · 「收盤名單下午買」)
- LIVE_PIT_0930: prior-day EOD pool · fill ≥09:30 (live-aligned)
- LIVE_PIT_1300: prior-day EOD pool · fill ≥13:00 (legal afternoon · stale vs A)
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

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
    apply_prior_day_1300_fills,
    apply_same_day_1300_fills,
    lag_rows_by_prior_day,
)
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_score_swap_c import (
    _poll5m_needs_spread_rs_panel,
    build_daily_spread_rs_panel,
    champion_score_swap_c_config,
)
from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar
from rrg_mono_daily_brief import LENGTH
from rrg_rotation import compute_rrg_panel


def _adopt(*, n_oos: int, d_oos: float | None, d_is: float | None) -> str:
    ok = (
        n_oos >= ADOPT_OOS_MIN_N
        and d_oos is not None
        and float(d_oos) >= ADOPT_OOS_MIN_DELTA_PP
        and d_is is not None
        and float(d_is) >= ADOPT_IS_MIN_DELTA_PP
    )
    return "GO" if ok else "NO_GO"


def run_a_live_proxy_study(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2025-01-02",
    date_end: str | None = None,
    is_end: str = "2025-12-31",
    confirm_bars: int = 2,
    n_slots: int = 3,
    gate_cache_path: str | Path | None = DEFAULT_GATE_CACHE,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 20:
        raise ValueError(f"need ≥20 trade dates, got {len(trade_dates)}")

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
    ctx_bt: dict[str, Any] = {
        "close": close,
        "bench": bench,
        "rs_ratio": rs_ratio,
        "rs_mom": rs_mom,
        "full_dates": full_dates,
        "fresh_by_date": fresh_by_date,
        "mono_by_date": mono_by_date,
        "zone_by_date": zone_by_date,
        "kbar_cache": {},
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

    print("  post-hoc A_ILLEGAL …", flush=True)
    p_a, a_meta = apply_prior_day_1300_fills(
        conn, p_b0, full_dates=full_dates, kbar_cache=ctx_bt["kbar_cache"]
    )
    print(f"    filled={a_meta['n_filled']}/{a_meta['n_baseline']}", flush=True)

    print("  post-hoc SAME_DAY_1300 …", flush=True)
    p_sd, sd_meta = apply_same_day_1300_fills(
        conn, p_b0, full_dates=full_dates, kbar_cache=ctx_bt["kbar_cache"]
    )
    print(f"    filled={sd_meta['n_filled']}/{sd_meta['n_baseline']}", flush=True)

    cfg_l0930 = replace(
        champ,
        variant_id="LIVE_PIT_0930",
        label="live-aligned · prior EOD · ≥09:30",
        no_trade_before="09:30",
    )
    cfg_l1300 = replace(
        champ,
        variant_id="LIVE_PIT_1300",
        label="live-aligned · prior EOD · ≥13:00",
        no_trade_before="13:00",
    )
    p_l09, s_l09, _ = _run_sim(
        conn,
        ctx=ctx_live,
        trade_dates=trade_dates,
        cfg=cfg_l0930,
        confirm_bars=confirm_bars,
        gate=gate,
        label="LIVE_PIT_0930",
    )
    p_l13, s_l13, _ = _run_sim(
        conn,
        ctx=ctx_live,
        trade_dates=trade_dates,
        cfg=cfg_l1300,
        confirm_bars=confirm_bars,
        gate=gate,
        label="LIVE_PIT_1300",
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

    variants = [
        base,
        _variant_row(
            variant_id="A_ILLEGAL",
            label="A · 當日 EOD 名單 · 前一日 13:00 成交（前瞻 · 不可 live）",
            periods=p_a,
            summary=None,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=bf,
            base_is=bi,
            base_oos=bo,
            extra={"pit_clean": False, "live_executable": False, "fill_meta": a_meta},
        ),
        _variant_row(
            variant_id="SAME_DAY_1300",
            label="同日 13:00 成交 · 仍用當日 EOD 名單（前瞻 · 不可 live）",
            periods=p_sd,
            summary=None,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=bf,
            base_is=bi,
            base_oos=bo,
            extra={"pit_clean": False, "live_executable": False, "fill_meta": sd_meta},
        ),
        _variant_row(
            variant_id="LIVE_PIT_0930",
            label="live 對齊 · 昨收池 · ≥09:30（現行 live 語意）",
            periods=p_l09,
            summary=s_l09,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=bf,
            base_is=bi,
            base_oos=bo,
            extra={"pit_clean": True, "live_executable": True},
        ),
        _variant_row(
            variant_id="LIVE_PIT_1300",
            label="live 對齊 · 昨收池 · ≥13:00（可上線的下午單）",
            periods=p_l13,
            summary=s_l13,
            date_start=date_start,
            is_end=is_end,
            oos_start=oos_start,
            end=end,
            base_full=bf,
            base_is=bi,
            base_oos=bo,
            extra={"pit_clean": True, "live_executable": True},
        ),
    ]

    # Also delta LIVE_PIT_1300 vs LIVE_PIT_0930
    live09 = next(v for v in variants if v["variant_id"] == "LIVE_PIT_0930")
    live13 = next(v for v in variants if v["variant_id"] == "LIVE_PIT_1300")
    for win, key, dkey in (
        ("full", "full", "delta_vs_live0930_full_pp"),
        ("is", "is", "delta_vs_live0930_is_pp"),
        ("oos", "oos", "delta_vs_live0930_oos_pp"),
    ):
        a = (live13["slices"][key] or {}).get("mean_excess_pct")
        b = (live09["slices"][key] or {}).get("mean_excess_pct")
        live13[dkey] = (
            round(float(a) - float(b), 4) if a is not None and b is not None else None
        )

    adopt = {}
    notes = []
    for vid in ("A_ILLEGAL", "SAME_DAY_1300", "LIVE_PIT_1300"):
        v = next(x for x in variants if x["variant_id"] == vid)
        # LIVE_PIT_1300 adopt vs LIVE_PIT_0930 not B0_BT
        if vid == "LIVE_PIT_1300":
            d_oos = v.get("delta_vs_live0930_oos_pp")
            d_is = v.get("delta_vs_live0930_is_pp")
        else:
            d_oos = v.get("delta_oos_excess_pp")
            d_is = v.get("delta_is_excess_pp")
        n_oos = int((v["slices"]["oos"] or {}).get("n_legs") or 0)
        adopt[vid] = _adopt(n_oos=n_oos, d_oos=d_oos, d_is=d_is)
        notes.append(f"{vid}: OOS Δ={d_oos}pp n={n_oos} → {adopt[vid]}")

    return {
        "schema": "c18acc_a_live_proxy-v1",
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": oos_start,
        "confirm_bars": confirm_bars,
        "n_slots": n_slots,
        "gate_meta": gate_meta,
        "variants": variants,
        "verdict": {
            "a_live_replicable": False,
            "reason": (
                "A needs day-T EOD selection at day-(T-1) 13:00 — information not available. "
                "LIVE_PIT_1300 is the legal afternoon path (yesterday EOD → today 13:00)."
            ),
            "adopt": adopt,
            "summary": " · ".join(notes),
            "live_recommendation": (
                "KEEP_LIVE_PIT_0930"
                if adopt.get("LIVE_PIT_1300") != "GO"
                else "CONSIDER_LIVE_PIT_1300"
            ),
        },
    }


def render_a_live_proxy_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# C18acc · 復刻 A 到 live？",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}** · "
        f"IS ≤ **{payload.get('is_end')}** · OOS ≥ **{payload.get('oos_start')}**",
        "",
        "## 結論（先講）",
        "",
        f"- **A 不能原樣上 live**：{v.get('reason')}",
        f"- live 建議：**{v.get('live_recommendation')}**",
        f"- {v.get('summary')}",
        "",
        "## 變體",
        "",
        "| id | 意思 | PIT | 可 live |",
        "|----|------|-----|---------|",
        "| B0_BT | 回測 SSOT：當日 EOD 池 + 09:30 | 否（相對 live） | 否（語意） |",
        "| A_ILLEGAL | 當日 EOD 名單 → 前一日 13:00 | 否 | **否** |",
        "| SAME_DAY_1300 | 當日 EOD 名單 → 當日 13:00 | 否 | **否** |",
        "| LIVE_PIT_0930 | 昨收池 → 今日 09:30（現行 live） | 是 | 是 |",
        "| LIVE_PIT_1300 | 昨收池 → 今日 13:00（可上線下午單） | 是 | 是 |",
        "",
        "## 結果",
        "",
        "| variant | window | n | mean_excess% | Δ vs B0_BT |",
        "|---------|--------|--:|-------------:|-----------:|",
    ]
    for row in payload.get("variants") or []:
        vid = row["variant_id"]
        for win, key in (("FULL", "full"), ("IS", "is"), ("OOS", "oos")):
            s = (row.get("slices") or {}).get(key) or {}
            if win == "FULL":
                d = row.get("delta_full_excess_pp")
            elif win == "IS":
                d = row.get("delta_is_excess_pp")
            else:
                d = row.get("delta_oos_excess_pp")
            d_s = "—" if d is None else f"{d:+.4f}pp"
            lines.append(
                f"| {vid} | {win} | {s.get('n_legs')} | {s.get('mean_excess_pct')} | {d_s} |"
            )

    live13 = next(
        (x for x in (payload.get("variants") or []) if x.get("variant_id") == "LIVE_PIT_1300"),
        None,
    )
    if live13:
        lines.extend(
            [
                "",
                "## LIVE_PIT_1300 vs LIVE_PIT_0930（真·可上線對照）",
                "",
                f"- FULL Δ：**{live13.get('delta_vs_live0930_full_pp')}pp**",
                f"- IS Δ：**{live13.get('delta_vs_live0930_is_pp')}pp**",
                f"- OOS Δ：**{live13.get('delta_vs_live0930_oos_pp')}pp**",
                "",
            ]
        )

    lines.extend(
        [
            "## live 若要「下午下單」要改哪裡",
            "",
            "1. Screen／launchd：在 **13:00** 觸發（或 `no_trade_before=13:00`），不是 09:30。",
            "2. 訊號：維持 **昨收 PIT**（`pool_as_of` = 昨交易日）——這是 LIVE_PIT_1300，**不是 A**。",
            "3. 不要用「今日收盤名單」在 13:00 下單——那是 A／SAME_DAY_1300，live 做不到。",
            "4. 若堅持 T−1 13:00 用「即將收盤那天」的資訊 → 只能走 **C（13:00 重算 RRG）**，已另測 NO_GO。",
            "",
            "模組：`src/research/backtest/c18acc_a_live_proxy_study.py`",
            "",
        ]
    )
    return "\n".join(lines)
