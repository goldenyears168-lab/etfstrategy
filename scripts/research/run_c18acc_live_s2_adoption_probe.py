#!/usr/bin/env python3
"""C18acc · live stack ± day-cut S2 · adopt-or-converge probe (simple).

Question: on the current live-aligned entry stack (fresh · confirm=1 · avoid_mixed ·
G_R5_12 · poll_5m swap/max_hold), does adding day-cut S2 (weakening 2d + loss≥5%)
lift OOS mean excess enough to wire into live — or converge (keep live without S2)?

Not a full exit sweep. Not XEL intraday loop (already rejected).

  PYTHONPATH=src python scripts/research/run_c18acc_live_s2_adoption_probe.py \
    --gate-cache reports/research/rrg/20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from report_paths import RESEARCH_RRG  # noqa: E402
from research.backtest.archive.c18acc_hold3d_event_study import (  # noqa: E402
    _prepare_c18acc_context,
)
from research.backtest.archive.c18acc_pool1_showcase import (  # noqa: E402
    _ensure_avoid_mixed_gate,
)
from research.backtest.finpilot_local_backtest import (  # noqa: E402
    load_price_panels,
    summarize_periods,
)
from research.backtest.rrg_mono_intraday_ab import champion_entry_c_config  # noqa: E402
from research.backtest.rrg_mono_score_swap_c import (  # noqa: E402
    champion_score_swap_c_config,
    simulate_score_swap_c,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

PASS_OOS_PP = 0.3
IS_NON_INFERIOR_PP = -0.3
MIN_OOS_N = 8
IS_END_DEFAULT = "2025-12-31"
OOS_START_DEFAULT = "2026-01-02"
DEFAULT_GATE = (
    RESEARCH_RRG
    / "20260711_c18acc_avoid_mixed_gate_live_poll5m_2025-01-02_2026-07-09.json"
)


def _slice(periods: list[dict[str, Any]], *, start: str, end: str) -> list[dict[str, Any]]:
    return [
        p
        for p in periods
        if start <= str(p.get("entry_date") or p.get("signal_date") or "") <= end
    ]


def _stats(periods: list[dict[str, Any]], summary: dict[str, Any] | None = None) -> dict[str, Any]:
    s = summarize_periods(periods)
    s["n_legs"] = len(periods)
    mr, mb = s.get("mean_return_pct"), s.get("mean_bench_pct")
    s["mean_excess_pct"] = (
        round(float(mr) - float(mb), 4) if mr is not None and mb is not None else None
    )
    if summary:
        s["swaps"] = summary.get("swaps_total")
        s["force_exits"] = summary.get("force_exits")
        s["max_hold_exits"] = summary.get("max_hold_exits")
    else:
        s["force_exits"] = sum(1 for p in periods if p.get("exit_reason") == "quad_force_exit")
        s["max_hold_exits"] = sum(1 for p in periods if p.get("exit_reason") == "max_hold")
    return s


def _live_base_cfg(*, n_slots: int, with_s2: bool):
    champ = champion_score_swap_c_config()
    cfg = replace(
        champ,
        n_slots=max(1, int(n_slots)),
        candidate_pool="fresh",
        variant_id="LIVE_NO_S2" if not with_s2 else "LIVE_PLUS_S2",
        label=(
            "live stack · swap/max_hold · no S2"
            if not with_s2
            else "live stack · + day-cut S2 (weakening 2d · loss≥5%)"
        ),
    )
    if with_s2:
        cfg = replace(
            cfg,
            quad_force_exit_mode="weakening",
            quad_force_exit_min_days=2,
            quad_force_exit_loss_pct=-0.05,
            quad_force_exit_requires_min_hold=True,
        )
    else:
        cfg = replace(
            cfg,
            quad_force_exit_mode="none",
            quad_force_exit_min_days=1,
            quad_force_exit_loss_pct=None,
            quad_force_exit_requires_min_hold=True,
        )
    return cfg


def run_probe(
    conn,
    *,
    date_start: str,
    date_end: str | None,
    is_end: str,
    oos_start: str,
    n_slots: int,
    gate_cache: Path | None,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    trade_dates = [d for d in full_dates if date_start <= d <= end]
    if len(trade_dates) < 20:
        raise ValueError(f"insufficient trade dates: {len(trade_dates)}")

    print(f"prepare context {trade_dates[0]}..{trade_dates[-1]} ({len(trade_dates)}d) …", flush=True)
    ctx = _prepare_c18acc_context(conn, trade_dates)
    gate, gate_meta = _ensure_avoid_mixed_gate(
        conn,
        trade_dates,
        gate_cache_path=str(gate_cache) if gate_cache else None,
        live_aligned=True,
        n_slots=n_slots,
    )
    entry_c = champion_entry_c_config(confirm_bars=1)

    variants: list[dict[str, Any]] = []
    for with_s2 in (False, True):
        cfg = _live_base_cfg(n_slots=n_slots, with_s2=with_s2)
        print(f"  sim {cfg.variant_id} …", flush=True)
        periods, summary = simulate_score_swap_c(
            conn,
            trade_dates=trade_dates,
            full_dates=ctx["full_dates"],
            close=ctx["close"],
            bench=ctx["bench"],
            fresh_by_date=ctx["fresh_by_date"],
            zone_by_date=ctx["zone_by_date"],
            config=cfg,
            mono_by_date=ctx["mono_by_date"],
            mono_up_by_date=ctx["mono_up_by_date"],
            mono_up_fresh_by_date=ctx["mono_up_fresh_by_date"],
            kbar_cache=ctx["kbar_cache"],
            rs_mom=ctx["rs_mom"],
            rs_ratio=ctx["rs_ratio"],
            entry_gate_by_date=gate,
            swap_gate_by_date=gate,
            entry_c_config=entry_c,
        )
        full = _stats(periods, summary)
        is_p = _slice(periods, start=date_start, end=is_end)
        oos_p = _slice(periods, start=oos_start, end=end)
        variants.append(
            {
                "variant_id": cfg.variant_id,
                "label": cfg.label,
                "with_s2": with_s2,
                "config": {
                    "quad_force_exit_mode": cfg.quad_force_exit_mode,
                    "quad_force_exit_min_days": cfg.quad_force_exit_min_days,
                    "quad_force_exit_loss_pct": cfg.quad_force_exit_loss_pct,
                    "candidate_pool": cfg.candidate_pool,
                    "entry_prior_ret_max": cfg.entry_prior_ret_max,
                    "confirm_bars": 1,
                    "avoid_mixed": gate is not None,
                },
                "full": full,
                "is": _stats(is_p),
                "oos": _stats(oos_p),
            }
        )

    live, s2 = variants[0], variants[1]
    oos_d = None
    is_d = None
    if live["oos"]["mean_excess_pct"] is not None and s2["oos"]["mean_excess_pct"] is not None:
        oos_d = round(float(s2["oos"]["mean_excess_pct"]) - float(live["oos"]["mean_excess_pct"]), 4)
    if live["is"]["mean_excess_pct"] is not None and s2["is"]["mean_excess_pct"] is not None:
        is_d = round(float(s2["is"]["mean_excess_pct"]) - float(live["is"]["mean_excess_pct"]), 4)

    oos_n = int(s2["oos"]["n_legs"] or 0)
    force_oos = int(s2["oos"].get("force_exits") or 0)
    adopt = (
        oos_d is not None
        and is_d is not None
        and oos_n >= MIN_OOS_N
        and oos_d >= PASS_OOS_PP
        and is_d >= IS_NON_INFERIOR_PP
        and force_oos >= 1
    )
    verdict = {
        "decision": "GO_ADOPT_LIVE_S2" if adopt else "CONVERGE_KEEP_LIVE_NO_S2",
        "oos_delta_excess_pp": oos_d,
        "is_delta_excess_pp": is_d,
        "oos_n": oos_n,
        "s2_oos_force_exits": force_oos,
        "gates": {
            "oos_delta_ge": PASS_OOS_PP,
            "is_delta_ge": IS_NON_INFERIOR_PP,
            "min_oos_n": MIN_OOS_N,
            "require_oos_force_exit": True,
        },
        "summary": (
            f"{'GO_ADOPT' if adopt else 'CONVERGE'} · OOS Δexcess={oos_d}pp "
            f"(n={oos_n} · force={force_oos}) · IS Δ={is_d}pp"
        ),
    }

    return {
        "schema": "c18acc_live_s2_adoption_probe-v1",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "hypothesis": "H-LIVE-S2-1",
        "question": (
            "On live-aligned stack, does day-cut S2 beat no-S2 by OOS ≥+0.3pp "
            "without IS damage — enough to wire into live screen?"
        ),
        "date_start": trade_dates[0],
        "date_end": trade_dates[-1],
        "is_end": is_end,
        "oos_start": oos_start,
        "n_slots": n_slots,
        "gate_meta": {k: gate_meta.get(k) for k in ("n_dates", "n_blocked", "mode") if k in (gate_meta or {})},
        "note": (
            "XEL intraday exit loop already rejected (hxel2_verdict). "
            "This probe only tests day-cut S2 on top of live swap/max_hold."
        ),
        "variants": variants,
        "verdict": verdict,
    }


def render_md(payload: dict[str, Any]) -> str:
    v = payload["verdict"]
    lines = [
        "# C18acc · live ± day-cut S2 · adopt-or-converge",
        "",
        f"- Window：`{payload['date_start']}` → `{payload['date_end']}` · IS≤`{payload['is_end']}` · OOS≥`{payload['oos_start']}`",
        f"- Stack：fresh · confirm=1 · avoid_mixed · G_R5_12 · poll_5m swap/max_hold",
        f"- **Verdict：`{v['decision']}`** — {v['summary']}",
        "",
        "| Variant | FULL excess% | IS excess% | OOS excess% | OOS n | force | max_hold |",
        "|---------|-------------:|-----------:|------------:|------:|------:|---------:|",
    ]
    for row in payload["variants"]:
        lines.append(
            f"| {row['variant_id']} | {row['full'].get('mean_excess_pct')} | "
            f"{row['is'].get('mean_excess_pct')} | {row['oos'].get('mean_excess_pct')} | "
            f"{row['oos'].get('n_legs')} | {row['oos'].get('force_exits')} | "
            f"{row['oos'].get('max_hold_exits')} |"
        )
    lines.extend(
        [
            "",
            f"- OOS Δ (S2−live)：**{v.get('oos_delta_excess_pp')}pp** · gate ≥{PASS_OOS_PP}",
            f"- IS Δ：**{v.get('is_delta_excess_pp')}pp** · gate ≥{IS_NON_INFERIOR_PP}",
            f"- S2 OOS force_exits：{v.get('s2_oos_force_exits')}（須≥1 才算有作用）",
            "",
            "## 結論用法",
            "",
        ]
    )
    if v["decision"] == "GO_ADOPT_LIVE_S2":
        lines.append("- 可把 day-cut S2 收進 live screen / Order 出場契約。")
    else:
        lines.extend(
            [
                "- **收斂**：live 維持 swap / max_hold / pyramid；**不**把 S2 接進 5m screen。",
                "- Strategy 規格裡的 S2 保留為 **日切回測契約**（已畢業 rotation-exit），與 live 出場分離。",
                "- 不重開 exit sweep；XEL intraday loop 維持 rejected。",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="C18acc live ± S2 adopt-or-converge probe")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--date-start", default="2025-01-02")
    ap.add_argument("--date-end", default="2026-07-09")
    ap.add_argument("--is-end", default=IS_END_DEFAULT)
    ap.add_argument("--oos-start", default=OOS_START_DEFAULT)
    ap.add_argument("--n-slots", type=int, default=3)
    ap.add_argument("--gate-cache", type=Path, default=DEFAULT_GATE)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        payload = run_probe(
            conn,
            date_start=args.date_start,
            date_end=args.date_end,
            is_end=args.is_end,
            oos_start=args.oos_start,
            n_slots=args.n_slots,
            gate_cache=args.gate_cache if args.gate_cache.is_file() else None,
        )
    finally:
        conn.close()

    stamp = date.today().strftime("%Y%m%d")
    out_json = args.out or RESEARCH_RRG / f"{stamp}_c18acc_live_s2_adoption_probe.json"
    out_md = out_json.with_suffix(".md")
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(render_md(payload), encoding="utf-8")
    print(payload["verdict"]["summary"])
    print(f"Wrote {out_json}\nWrote {out_md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
