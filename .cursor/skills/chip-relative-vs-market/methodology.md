# Relative chip methodology (v2)

## Why v1 failed

`stock_FT_net / market_FT_net` explodes when market net ≈ 0 (hundreds of %). Spearmans vs excess collapse.

## Features (PIT)

| Feature | Definition | Notes |
|---------|------------|-------|
| `FT_ntd` | `(foreign_net + investment_trust_net) * close` | Dealer_self excluded from FT |
| `gross_FT` | market Foreign+IT buy+sell NTD | FinMind `TaiwanStockTotalInstitutionalInvestors` |
| `FT_part_gross` | `FT_ntd / gross_FT` | report as bp ×1e4 |
| `FT_part_ok` | `gross_FT ≥ 5e10` | else missing participation |
| `FT_z` | z vs prior 21d `FT_ntd` | shift(1) mean/std |
| `align_FT` | `sign(FT_ntd)*sign(net_FT)` | +1 aligned |
| `FT_resid_z` | residual of `FT_ntd ~ net_FT` then z | rolling 60 fit, past only |
| `beta` | rolling 60 stock~market | clipped [0.2, 3] |
| `excess_beta` | `r - beta*rm` | preferred excess |
| `RS_mom10` | Δ log(close/ix) ×100 over 10d | relative momentum |
| `FT_part_pct` | expanding percentile of part | **not** full-sample qcut |

Flags: `rel_dump`, `rel_scoop`, `aligned_buy/sell`, `bull_div`, `bear_div`.

## Labels

| Label | Meaning |
|-------|---------|
| `fwd1_r` | close_T → close_{T+1} |
| `fwd5_r` | close_T → close_{T+5} |
| `fwd1_oc` | open_{T+1} → close_{T+1} |
| `fwd*_ex` | compounded next-k `excess_beta` |

## OOS gates (strict)

- `n_is ≥ 12`, `n_oos ≥ 8`
- `hit_oos ≥ 55%`, `lift_oos ≥ +8pp` vs unconditional same-side base
- OOS median return sign agrees with side

One automatic relax pass if <2 rules survive (see `learn_rules`).

## Code

- `src/research/chip_relative/panel.py`
- `src/research/chip_relative/rules.py`
- `scripts/research/run_2492_chip_relative_day_backtest.py`
