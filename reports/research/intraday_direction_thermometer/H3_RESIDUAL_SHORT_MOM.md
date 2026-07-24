# H3 · Residual short momentum (fwd 5–15m)

**Verdict: REJECT**

Residual short-mom is **anti-predictive** on fwd 5–15m (mean OOS directed ≈39.6% ≪ 50%). It beats raw by >0.5pp on 6/6 cells but never approaches the ≥55% gate. Same qualitative failure mode as TRACK_BETA_EMA on fade30: β-residual is not a free follow edge — here both raw and residual look like short-horizon mean-reversion (fade short-mom ≈ 58–65% if inverted).

## Setup

| Item | Value |
|---|---|
| Bars | `stock_kbar_1m` · bench `0050` |
| Universe | 2327, 8046, 3189, 6451, 2330, 2303, 2454 |
| Window | 2024-01-02 → max · IS ≤ 2025-09-30 |
| Mom lookback | 1 and 2 completed 1m bars (Live-aligned) |
| β | rolling OLS · lookback=30 1-bar returns · same session PIT |
| Forward | H ∈ {5, 10, 15} minutes |
| Flat eps | 0.0003 (3.0 bp) |
| Skip open | first 5 1m bars |
| Gate | OOS directed ≥55.0% · n≥1000 · ≥70% names ≥52.0% · max name share ≤40% |

PIT: signal at bar `i` uses only completed same-session bars `≤ i`.
No Order · research only. Prior fade30 `TRACK_BETA_EMA` residual filters
are **out of scope** here (different horizon / path).

## Best OOS by horizon

- **H=5m**: best resid `resid_mom_2` OOS 38.72% n=118428 (gates=fail); best raw `raw_mom_2` OOS 36.87% n=95459
- **H=10m**: best resid `resid_mom_2` OOS 40.24% n=128264 (gates=fail); best raw `raw_mom_2` OOS 38.61% n=102365
- **H=15m**: best resid `resid_mom_2` OOS 41.54% n=133080 (gates=fail); best raw `raw_mom_2` OOS 39.92% n=105639

Residual beat raw by >0.5pp on **6/6** (mom_bars×H) cells.

## Leaderboard (OOS sorted)

| variant | H(m) | OOS hit% | OOS n | vs coin | vs AL | IS hit% | gates |
|---|---:|---:|---:|---:|---:|---:|:---:|
| `mkt_mom_1` | 5 | 52.01 | 70291 | 2.01 | 4.02 | 52.69 | fail |
| `mkt_mom_2` | 5 | 51.6 | 91752 | 1.6 | 3.35 | 52.65 | fail |
| `mkt_mom_1` | 10 | 51.41 | 77152 | 1.41 | 4.04 | 51.96 | fail |
| `mkt_mom_1` | 15 | 51.27 | 80423 | 1.27 | 4.01 | 51.6 | fail |
| `mkt_mom_2` | 10 | 51.07 | 100234 | 1.07 | 3.44 | 51.93 | fail |
| `mkt_mom_2` | 15 | 50.83 | 104286 | 0.83 | 3.47 | 51.58 | fail |
| `resid_beta1_mom_2` | 15 | 41.85 | 142948 | -8.15 | -5.51 | 38.33 | fail |
| `resid_mom_2` | 15 | 41.54 | 133080 | -8.46 | -5.79 | 37.03 | fail |
| `resid_beta1_mom_1` | 15 | 40.98 | 120335 | -9.02 | -6.43 | 37.7 | fail |
| `resid_beta1_mom_2` | 10 | 40.64 | 137510 | -9.36 | -6.87 | 36.72 | fail |
| `resid_mom_1` | 15 | 40.5 | 110957 | -9.5 | -6.94 | 35.75 | fail |
| `resid_mom_2` | 10 | 40.24 | 128264 | -9.76 | -7.2 | 35.42 | fail |
| `resid_beta1_mom_1` | 10 | 40.0 | 115802 | -10.0 | -7.59 | 36.07 | fail |
| `raw_mom_2` | 15 | 39.92 | 105639 | -10.08 | -7.44 | 33.09 | fail |
| `resid_mom_1` | 10 | 39.41 | 106881 | -10.59 | -8.2 | 33.93 | fail |
| `resid_beta1_mom_2` | 5 | 38.97 | 126456 | -11.03 | -9.14 | 33.64 | fail |
| `resid_mom_2` | 5 | 38.72 | 118428 | -11.28 | -9.37 | 32.22 | fail |
| `raw_mom_2` | 10 | 38.61 | 102365 | -11.39 | -8.86 | 30.93 | fail |
| `raw_mom_1` | 15 | 38.55 | 85997 | -11.45 | -9.04 | 31.27 | fail |
| `resid_beta1_mom_1` | 5 | 38.06 | 106534 | -11.94 | -10.05 | 32.73 | fail |
| `resid_mom_1` | 5 | 37.48 | 98800 | -12.52 | -10.59 | 30.16 | fail |
| `raw_mom_1` | 10 | 37.35 | 83232 | -12.65 | -10.38 | 28.82 | fail |
| `raw_mom_2` | 5 | 36.87 | 95459 | -13.13 | -11.36 | 27.18 | fail |
| `raw_mom_1` | 5 | 35.17 | 77742 | -14.83 | -13.02 | 24.46 | fail |

## Implication for Live short-mom columns

Keep Live short-mom as **raw** 1–2m diagnostic columns (`mom_1bar_pct` / `mom_2bar_pct`). Do **not** add β-residual short-mom as a follow-signal for 5–15m. Both raw and residual are mean-reverting on this horizon (directed hit ≪ 50%); if Live uses short-mom for action, treat it as a **fade / exhaustion** hint, not continuation — and still research-only until a separate fade gate is frozen.

## Repro

```bash
PYTHONPATH=src .venv/bin/python scripts/research/run_h3_residual_short_mom.py
```

Artifacts: `h3_residual_short_mom.json` · this file.
