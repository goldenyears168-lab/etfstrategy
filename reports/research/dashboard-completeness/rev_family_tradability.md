# Rev-Family Tradability — decisive net-of-cost, market-neutral test

**Date:** 2026-07-31 · **Verdict scripts:** `scripts/research/dashboard/rev_family_tradability_fetch.py` (data), `scripts/research/dashboard/rev_family_tradability_study.py` (backtest) · **Metrics:** `reports/research/dashboard-completeness/rev_family_tradability_metrics.csv`

## Question

The one project question still worth settling: after **real Taiwan trading cost** and in a **market-neutral (dollar-neutral) monthly-rebalanced** form, does the revenue family
(`rev_yoy_3m` / `rev_mom` / `rev_surprise` / equal-weight composite) keep a **positive net Sharpe** and **pass Deflated-Sharpe**? Is it a *tradable stock-selection tilt* or a *paper signal the costs eat*? And does the Phase-5 "edge concentrates in mid-small" self-cancel against mid-small's higher cost?

## Setup (iron rules honored)

- **Universe:** 418 names with FinMind `TaiwanStockPriceAdj` 還原收盤 (2021+, 0 missing) that also have TEJ EWSALE monthly revenue. Already the top-liquid set. Split by full-sample median daily 成交值 → **LARGE** = top tercile (140 names, cut ≈ 2.83 億/day), **MIDSMALL** = rest (278).
- **PIT:** every revenue feature indexed by EWSALE `annd` (公告日), daily forward-filled. `t+1` entry.
- **Form:** monthly rebalance (last trading day → weights, entered next day). Dollar-neutral top-decile long / bottom-decile short, equal weight, gross 1.0 (0.5/0.5), net 0.
- **Cost (signed, position-level, per rebalance):** commission 0.1425%×0.4折 = **0.0570%/side** on all traded notional; slippage **10bps/side LARGE, 25bps/side MIDSMALL**; **0.30% 賣方交易稅 on the sell notional only** (現股).
- **Judged on:** IS/OOS 70/30 split (IS 2021-04→2024-12, OOS 2024-12→2026-07); annualized net Sharpe; permutation (shuffle signal across stocks each rebalance, keep exposure); **Deflated-Sharpe with honest n_trials = 24** (4 forms × 3 universes × 2 directions); annual turnover; cost drag; capacity (single-name 10%-of-60d-ADV cap); market/champion collinearity (corr vs TAIEX return). Gross reported beside net.

## Results (net = after real cost)

| form | universe | Sh IS net | Sh OOS net | Sh OOS gross | turnover/yr | cost drag /yr | perm p | **DSR** | capacity | mkt ρ |
|---|---|---|---|---|---|---|---|---|---|---|
| **rev_yoy_3m** | ALL | +0.68 | **+1.67** | +1.96 | 2.6× | 2.2% | 0.003 | 0.015 | ~$0.8M | +0.30 |
| **rev_yoy_3m** | **LARGE** | **+1.37** | **+1.54** | +1.66 | 2.2× | 1.4% | 0.007 | **0.112** | **~$30M** | +0.26 |
| **rev_yoy_3m** | MIDSMALL | +0.03 | +1.36 | +1.68 | 2.8× | 2.6% | 0.003 | 0.000 | ~$0.5M | +0.19 |
| rev_mom | ALL | −1.49 | −1.60 | −0.06 | 10.3× | 8.8% | 0.52 | 0.000 | ~$0.8M | +0.03 |
| rev_mom | LARGE | −1.53 | +0.91 | +1.71 | 10.1× | 6.4% | 0.010 | 0.000 | ~$11M | +0.02 |
| rev_mom | MIDSMALL | −1.12 | −2.61 | −1.32 | 10.3× | 9.7% | 0.90 | 0.000 | ~$0.6M | +0.01 |
| rev_surprise | ALL | −1.02 | −1.04 | +0.05 | 6.9× | 5.7% | 0.23 | 0.000 | ~$1.1M | +0.01 |
| rev_surprise | LARGE | −1.17 | +0.35 | +0.89 | 6.8× | 4.3% | 0.12 | 0.000 | ~$74M | −0.03 |
| rev_surprise | MIDSMALL | −0.21 | −1.72 | −0.67 | 7.0× | 6.6% | 0.53 | 0.000 | ~$0.7M | +0.01 |
| composite | ALL | −0.37 | +0.14 | +1.17 | 7.5× | 6.3% | 0.017 | 0.000 | ~$1.1M | +0.24 |
| composite | LARGE | +0.02 | +1.29 | +1.78 | 7.2× | 4.5% | 0.010 | 0.002 | ~$75M | +0.22 |
| composite | MIDSMALL | −0.53 | −0.79 | +0.25 | 7.6× | 7.1% | 0.18 | 0.000 | ~$0.7M | +0.15 |

(DSR is the deflated-Sharpe probability; the ≥0.95 bar is the pass line. Capacity ≈ TWD figure / 32 for USD; full TWD in the CSV.)

## Verdict

**The rev family is NOT a deployable standalone market-neutral alpha net of real Taiwan cost.** Three findings, all decisive:

**1. Only the slow leg survives; the two "orthogonal" fast legs are paper signals the costs eat.**
`rev_mom` and `rev_surprise` flip monthly → **7–10× annual turnover → 4.3–9.7%/yr cost drag**. Their *gross* OOS Sharpe was already marginal (−0.06 and +0.05 on ALL); cost turns that into net **−1.0 to −2.6**. The equal-weight composite inherits the fast legs' turnover (7.5×) and is dragged from gross +1.17 to net **+0.14** on ALL. So the Phase-1–5 "three legs" thesis dies at the trading desk: the environmental (MoM) and surprise legs are un-tradable at these costs — their orthogonality never reaches net P&L. Only `rev_yoy_3m` (2.2–2.8× turnover, because 3-month YoY momentum is slow) keeps a positive net Sharpe (+1.36 to +1.67).

**2. `rev_yoy_3m` clears permutation but FAILS Deflated-Sharpe — a weak tilt, not robust alpha.**
It beats its exposure-matched null (perm p 0.003–0.007) but the best DSR is **0.112 (LARGE)**, nowhere near the 0.95 bar, once the honest 24-trial search penalty + fat tails + a single ~19-month OOS regime are charged. This is the same conclusion the productization already assumed ("weak selection tilt, not standalone alpha") — now confirmed under real cost and market-neutral form, and even harsher than the earlier DSR_oos 0.606. It also carries **+0.20 to +0.30 correlation to TAIEX** even dollar-neutral (a residual growth/high-beta tilt), echoing the prior +0.27 champion correlation — so part of its surviving edge co-moves with the risk-on regime the tech-gated champion already trades. Not clean orthogonal alpha.

**3. The mid-small concentration self-cancels against its cost — exactly the feared conflict.**
Phase-5's "edge集中中小型" is a **gross-return** artifact. Net of cost and capacity it **inverts to large-cap**:
- `rev_yoy_3m` **MIDSMALL**: net OOS +1.36 looks fine but **IS is +0.03 (no in-sample edge → the OOS is regime luck)**, DSR **0.000**, capacity **~$0.5M** (16M TWD ≈ 1,600 萬), drag 2.6%/yr. Un-investable.
- `rev_yoy_3m` **LARGE**: the *only* bucket positive in **both** windows (IS +1.37 / OOS +1.54), best DSR (0.112), lowest drag (1.4%/yr), capacity **~$30M**.

Mid-small's higher gross edge is precisely eaten by its 25bps slippage, 2.6%/yr drag, and a $0.5M capacity ceiling. The conflict the task flagged is real and it kills mid-small. (Un-modeled and worse still for mid-small: Taiwan 借券 borrow cost on the short leg, ~0.5–2%/yr, and hard/impossible-to-borrow constraints on small names — not charged here, so mid-small's true net is below what's shown.)

## Bottom line

- **rev_mom / rev_surprise / composite:** reject as tradable market-neutral strategies — turnover-driven cost annihilates them.
- **rev_yoy_3m:** the sole survivor with positive net Sharpe; usable **only** as a **large-cap** stock-selection **tilt** (consistent IS/OOS, ~$30M capacity, 1.4%/yr drag), but it **fails Deflated-Sharpe** and is ~+0.3 correlated to the market/champion. Best home remains a **long-side or overlay tilt inside the tech-gated champion (system C), in large caps — not a mid-small dollar-neutral book.**
- The mid-small × high-cost self-cancellation is **confirmed**.

Honest limits: single ~19-month OOS regime; short borrow cost and borrow availability not modeled (would further penalize mid-small shorts); capacity = single-name 10%-of-ADV, one-day, conservative. Not investment advice.
