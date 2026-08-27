#!/usr/bin/env python3
"""H-SC-FILL-INSIDE cheap check (2026-08-13).

config/research.yaml's H-SC-FILL-INSIDE: "真實成交（fills）多數發生在慢尺度
通道「內部」而非邊緣突破瞬間" -- status pending, evidence notes the Aug-5
night raw fill_legs needed to classify actual fill price vs oracle channel
edge at fill time had already rolled out of the live sim's 40-trade rolling
cache, so it was never independently re-verified.

This script does NOT reopen that exact question (real fill price vs live
oracle channel edge -- that data is gone for the one night it was observed).
Instead it uses tmf_walkforward_harness.py's _replay_day(), which fills
entries via a paper order book at EXACTLY the order layer's "want" (rail)
price whenever that price falls inside the next 1m bar's [low, high] range
(see _replay_day() lines ~263-271: `px = working.get(side)` is used directly
as `ep`, un-clamped). That means in this harness ep == want by construction
-- there is no recoverable "how far past the rail did the fill actually
happen" residual; it is always exactly 0. That null result is itself worth
recording: this harness cannot test entry-fill-vs-rail slippage at all.

Per the task's fallback instruction, this script instead uses MAE (maximum
adverse excursion in pts, tracked bar-by-bar from entry to close, added to
_replay_day() 2026-08-13) as the practical proxy for "how far past the
entry/rail price did the market move against the trade before reverting."
Since entry price IS the rail price in this model, MAE directly answers a
close cousin of H-SC-FILL-INSIDE: after a fill happens at the rail, does
price mostly stay close to it (small MAE, consistent with "fills sit near
channel interior / mean-revert quickly") or blow well past it toward the
150pt stop (consistent with "fills are at the edge and keep going")?

Run: PYTHONPATH=src .venv/bin/python scripts/research/tmf_fill_inside_mae_check.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

from tmf_walkforward_harness import FIT_SAMPLE, run_batch  # noqa: E402

STOP_PTS = 150.0
TRAIL_ARM_PTS = 50.0
OUT_PATH = Path("reports/research/channel_lab/h_sc_fill_inside_mae_check.json")


def pctl(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def main() -> None:
    result = run_batch(FIT_SAMPLE, label="h_sc_fill_inside:fit")
    trades = result["trades"]
    n = len(trades)
    maes = [float(t["mae"]) for t in trades]
    mfes = [float(t["mfe"]) for t in trades]

    entry_is_rail_note = (
        "_replay_day() fills entries at exactly the order-layer 'want' "
        "(rail) price when it falls inside the next bar's range -- ep==want "
        "always, by construction. So 'fill distance past want' is 0 for "
        "every trade in this harness; that residual is NOT informative and "
        "is not reported as a real finding. MAE (adverse excursion from "
        "that rail-price entry) is used as the practical proxy instead, "
        "per the fallback in the task."
    )

    thresholds = [10.0, 25.0, TRAIL_ARM_PTS, 75.0, 100.0, STOP_PTS]
    frac_mae_under = {
        f"le_{int(th)}pt": round(sum(1 for m in maes if m <= th) / n, 3) if n else None
        for th in thresholds
    }

    summary = {
        "hypothesis_id": "H-SC-FILL-INSIDE",
        "hypothesis_statement_zh": (
            "真實成交（fills）多數發生在慢尺度通道「內部」而非邊緣突破瞬間"
        ),
        "method": "tmf_walkforward_harness.run_batch(FIT_SAMPLE) -> per-trade mae/mfe",
        "sample": "FIT_SAMPLE (6 chronologically-stratified historical days, 2023-07..2024-10)",
        "days": FIT_SAMPLE,
        "n_trades": n,
        "entry_fill_vs_rail_note": entry_is_rail_note,
        "reference_scales_pts": {"trail_arm_pts": TRAIL_ARM_PTS, "stop_pts": STOP_PTS},
        "mae_pts": {
            "mean": round(statistics.mean(maes), 1) if n else None,
            "median": round(statistics.median(maes), 1) if n else None,
            "p25": round(pctl(maes, 0.25), 1) if n else None,
            "p75": round(pctl(maes, 0.75), 1) if n else None,
            "p90": round(pctl(maes, 0.90), 1) if n else None,
            "max": round(max(maes), 1) if n else None,
        },
        "mfe_pts": {
            "mean": round(statistics.mean(mfes), 1) if n else None,
            "median": round(statistics.median(mfes), 1) if n else None,
        },
        "frac_trades_with_mae_at_or_below_threshold": frac_mae_under,
        "frac_mae_reaches_stop_150pt": round(sum(1 for m in maes if m >= STOP_PTS) / n, 3) if n else None,
        "per_trade": [
            {"day": t["day"], "s": t["s"], "ep": t["ep"], "mae": t["mae"], "mfe": t["mfe"], "pnl": t["pnl"]}
            for t in trades
        ],
        "caveats": [
            "This is NOT a re-derivation of the original Aug-5 observation "
            "(real fill price vs live oracle channel edge at fill time) -- "
            "that raw fill_legs data already rolled out of the 40-trade "
            "live_v6_sim_state.json rolling cache and cannot be recovered.",
            "_replay_day() models entries as noiseless limit fills exactly "
            "at the rail ('want') price -- it structurally cannot show "
            "entry slippage past the rail, so it cannot directly confirm or "
            "reject the literal H-SC-FILL-INSIDE wording. MAE is a related "
            "but distinct proxy (post-entry adverse excursion, not "
            "fill-price-vs-edge-at-fill-time).",
            "FIT_SAMPLE is only 6 days / historical (2023-2024), not the "
            "live 2026-08-13 Fubon-confirmed fills referenced in the task; "
            "cross-checking against live_v6_sim_server.py's "
            "_live_intraday_fills was judged out of scope for this cheap "
            "check (requires a live Fubon session, not read-only replay).",
        ],
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(json.dumps({k: v for k, v in summary.items() if k != "per_trade"}, indent=2, ensure_ascii=False))
    print(f"\nwrote {OUT_PATH} ({n} trades)", file=sys.stderr)


if __name__ == "__main__":
    main()
