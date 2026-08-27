"""AE: does a unified detach-gate + US-TW-overnight + VIX-regime gate flag risk on
songshan-copytrade's actual historical overnight holding nights?

Read-only. Reuses:
  - songshan-copytrade trade legs (H7): reports/research/branch-footprint-screen/
    ab58_xMega_copytrade/legs/consensus_solo_songshan_core_R_song_H7.csv
  - US-TW overnight day panel (PIT, 2020-01-02..2026-07-20):
    reports/research/prob_risk/20260721_overnight_open_risk_day_panel.csv
  - VIX (US) daily close from stock_db market_vix_daily (symbol='VIX')

Detach-gate exact frozen rule (config/order.yaml `freeze_rule`) is
s5_w-0.6_c1_down-0.5_arm0940-1230: intraday 5m TWII-vs-NQ spread_30m<=-0.6% AND
tw_from_open<=-0.5% inside 09:40-12:30. Yahoo 5m history is only cached for a
recent ~48-60d window (2026-05-05..2026-07-15, see
reports/research/rrg/20260715_us_tw_sell_only_live_readiness.json) which barely
overlaps songshan's Nov-2024..Jul-2026 trade history, and there is no local
intraday TWII table to reconstruct spread_30m further back. So we use a
transparent NECESSARY-CONDITION proxy for detach-gate: tw_open_to_low_pct <=
-0.5 that day (the down-0.5 leg of the frozen rule; strictly looser than the
real rule since it drops the concurrent spread_30m<=-0.6 confirm leg -> proxy
over-fires relative to the live rule, biasing AGAINST finding a null result).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
from scipy import stats

REPO = Path("/Users/jackm4/goldenstocks")
SONGSHAN_CSV = REPO / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/legs/consensus_solo_songshan_core_R_song_H7.csv"
DAY_PANEL_CSV = REPO / "reports/research/prob_risk/20260721_overnight_open_risk_day_panel.csv"
OUT_DIR = REPO / "reports/research/unified_overnight_risk_gate"
DB_PATH = Path("/Users/jackm4/goldenstocks-data/data/stocks.db")

DETACH_PROXY_THR = -0.5   # tw_open_to_low_pct <= this => detach necessary-leg proxy fires
NQ_OVERNIGHT_THR = -0.5   # component (b): nq_overnight_pct <= this fires (matches BAN_NEW_MORNING primary leg's NQ side)
VIX_ELEVATED_THR = 22.0   # regime split only, not a trigger


def load_vix(conn: sqlite3.Connection) -> pd.DataFrame:
    df = pd.read_sql_query(
        "select date, close from market_vix_daily where symbol='VIX' order by date", conn
    )
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["close"]


def main() -> None:
    trades = pd.read_csv(SONGSHAN_CSV, parse_dates=["signal_date", "entry_date", "exit_date"])
    panel = pd.read_csv(DAY_PANEL_CSV, parse_dates=["session_date", "us_trade_date"])
    panel = panel.set_index("session_date").sort_index()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    vix = load_vix(conn)
    conn.close()

    all_sessions = panel.index  # trading-day calendar per the day panel (TW sessions with US-anchor data)

    night_rows = []
    trade_rows = []
    for _, tr in trades.iterrows():
        entry, exit_ = tr["entry_date"], tr["exit_date"]
        held_sessions = all_sessions[(all_sessions > entry) & (all_sessions <= exit_)]
        # last session (exit_date) morning is still an "overnight held into" morning
        n_gate = 0
        n_nights = 0
        for d in held_sessions:
            row = panel.loc[d]
            detach_fire = bool(row["tw_open_to_low_pct"] <= DETACH_PROXY_THR)
            ustw_fire = bool(row["nq_overnight_pct"] <= NQ_OVERNIGHT_THR)
            unified_fire = detach_fire or ustw_fire
            vix_val = vix.reindex([row["us_trade_date"]], method="ffill").iloc[0]
            n_nights += 1
            n_gate += int(unified_fire)
            night_rows.append(
                {
                    "stock_id": tr["stock_id"],
                    "entry_date": entry.date().isoformat(),
                    "exit_date": exit_.date().isoformat(),
                    "session_date": d.date().isoformat(),
                    "tw_open_to_low_pct": row["tw_open_to_low_pct"],
                    "tw_open_to_close_pct": row["tw_open_to_close_pct"],
                    "nq_overnight_pct": row["nq_overnight_pct"],
                    "detach_proxy_fire": detach_fire,
                    "ustw_overnight_fire": ustw_fire,
                    "unified_fire": unified_fire,
                    "vix_close": vix_val,
                    "vix_elevated": bool(vix_val >= VIX_ELEVATED_THR) if pd.notna(vix_val) else None,
                }
            )
        trade_rows.append(
            {
                "stock_id": tr["stock_id"],
                "entry_date": entry.date().isoformat(),
                "exit_date": exit_.date().isoformat(),
                "n_held_nights": n_nights,
                "n_gate_fire_nights": n_gate,
                "any_gate_fire": n_gate > 0,
                "bare_excess_pct": tr["bare_excess_pct"],
                "radj_pct": tr["radj_pct"],
            }
        )

    nights_df = pd.DataFrame(night_rows)
    trades_df = pd.DataFrame(trade_rows)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    nights_df.to_csv(OUT_DIR / "songshan_overnight_nights_gate_flags.csv", index=False)
    trades_df.to_csv(OUT_DIR / "songshan_trades_gate_summary.csv", index=False)

    print("=== Night-level ===")
    print("n_nights total:", len(nights_df))
    print("n_nights unified_fire:", int(nights_df["unified_fire"].sum()))
    print("n_nights detach_proxy_fire:", int(nights_df["detach_proxy_fire"].sum()))
    print("n_nights ustw_overnight_fire:", int(nights_df["ustw_overnight_fire"].sum()))
    print()
    print("Next-day market outcome (tw_open_to_low_pct) fired vs not:")
    fired = nights_df.loc[nights_df["unified_fire"], "tw_open_to_low_pct"]
    not_fired = nights_df.loc[~nights_df["unified_fire"], "tw_open_to_low_pct"]
    print("  fired: n=%d mean=%.3f median=%.3f" % (len(fired), fired.mean(), fired.median()))
    print("  not_fired: n=%d mean=%.3f median=%.3f" % (len(not_fired), not_fired.mean(), not_fired.median()))
    if len(fired) > 1 and len(not_fired) > 1:
        u, p = stats.mannwhitneyu(fired, not_fired, alternative="less")
        print(f"  Mann-Whitney U (fired < not_fired): U={u:.1f} p={p:.4f}")

    print()
    print("VIX regime split among fired nights:")
    print(nights_df.loc[nights_df["unified_fire"], "vix_elevated"].value_counts(dropna=False))
    print("VIX mean fired vs not:")
    print("  fired vix mean:", nights_df.loc[nights_df["unified_fire"], "vix_close"].mean())
    print("  not_fired vix mean:", nights_df.loc[~nights_df["unified_fire"], "vix_close"].mean())

    print()
    print("=== Trade-level (n=%d) ===" % len(trades_df))
    print(trades_df[["stock_id", "entry_date", "exit_date", "n_held_nights", "n_gate_fire_nights", "any_gate_fire", "bare_excess_pct"]].to_string(index=False))
    g1 = trades_df.loc[trades_df["any_gate_fire"], "bare_excess_pct"]
    g0 = trades_df.loc[~trades_df["any_gate_fire"], "bare_excess_pct"]
    print()
    print("bare_excess_pct any_gate_fire=True: n=%d mean=%.2f median=%.2f" % (len(g1), g1.mean(), g1.median()))
    print("bare_excess_pct any_gate_fire=False: n=%d mean=%.2f median=%.2f" % (len(g0), g0.mean(), g0.median()))
    print("(note: with the loose detach-necessary-condition proxy, any_gate_fire is ~always True"
          " over a 6-night hold -> binary split above is close to degenerate; see continuous/AND"
          " analyses below for real discrimination.)")

    print()
    print("=== Trade-level continuous: n_gate_fire_nights (out of 6) vs bare_excess_pct ===")
    rho, p_rho = stats.spearmanr(trades_df["n_gate_fire_nights"], trades_df["bare_excess_pct"])
    print(f"Spearman rho={rho:.3f} p={p_rho:.4f} (n={len(trades_df)})")
    median_fire = trades_df["n_gate_fire_nights"].median()
    hi = trades_df.loc[trades_df["n_gate_fire_nights"] > median_fire, "bare_excess_pct"]
    lo = trades_df.loc[trades_df["n_gate_fire_nights"] <= median_fire, "bare_excess_pct"]
    print(f"median n_gate_fire_nights={median_fire}")
    print("high-fire-count trades: n=%d mean=%.2f median=%.2f" % (len(hi), hi.mean(), hi.median()))
    print("low-fire-count trades:  n=%d mean=%.2f median=%.2f" % (len(lo), lo.mean(), lo.median()))
    if len(hi) > 1 and len(lo) > 1:
        u, p = stats.mannwhitneyu(hi, lo, alternative="two-sided")
        print(f"Mann-Whitney U (hi vs lo, two-sided): U={u:.1f} p={p:.4f}")

    print()
    print("=== Stricter AND-gate (detach_proxy AND ustw_overnight both fire same night) ===")
    nights_df["and_fire"] = nights_df["detach_proxy_fire"] & nights_df["ustw_overnight_fire"]
    print("n_nights and_fire:", int(nights_df["and_fire"].sum()), "/", len(nights_df))
    and_by_trade = nights_df.groupby(["stock_id", "entry_date", "exit_date"])["and_fire"].sum().reset_index()
    and_by_trade = and_by_trade.rename(columns={"and_fire": "n_and_fire_nights"})
    trades_df2 = trades_df.merge(and_by_trade, on=["stock_id", "entry_date", "exit_date"])
    g1b = trades_df2.loc[trades_df2["n_and_fire_nights"] > 0, "bare_excess_pct"]
    g0b = trades_df2.loc[trades_df2["n_and_fire_nights"] == 0, "bare_excess_pct"]
    print("AND-gate any_fire=True: n=%d mean=%.2f median=%.2f" % (len(g1b), g1b.mean(), g1b.median()))
    print("AND-gate any_fire=False: n=%d mean=%.2f median=%.2f" % (len(g0b), g0b.mean(), g0b.median()))
    if len(g1b) > 1 and len(g0b) > 1:
        u, p = stats.mannwhitneyu(g1b, g0b, alternative="two-sided")
        print(f"Mann-Whitney U (two-sided): U={u:.1f} p={p:.4f}")
        t, pt = stats.ttest_ind(g1b, g0b, equal_var=False)
        print(f"Welch t-test: t={t:.3f} p={pt:.4f}")

    print()
    print("=== ustw_overnight_fire only (component b alone, most selective/validated leg) ===")
    ustw_by_trade = nights_df.groupby(["stock_id", "entry_date", "exit_date"])["ustw_overnight_fire"].sum().reset_index()
    ustw_by_trade = ustw_by_trade.rename(columns={"ustw_overnight_fire": "n_ustw_fire_nights"})
    trades_df3 = trades_df.merge(ustw_by_trade, on=["stock_id", "entry_date", "exit_date"])
    g1c = trades_df3.loc[trades_df3["n_ustw_fire_nights"] > 0, "bare_excess_pct"]
    g0c = trades_df3.loc[trades_df3["n_ustw_fire_nights"] == 0, "bare_excess_pct"]
    print("ustw any_fire=True: n=%d mean=%.2f median=%.2f" % (len(g1c), g1c.mean(), g1c.median()))
    print("ustw any_fire=False: n=%d mean=%.2f median=%.2f" % (len(g0c), g0c.mean(), g0c.median()))
    if len(g1c) > 1 and len(g0c) > 1:
        u, p = stats.mannwhitneyu(g1c, g0c, alternative="two-sided")
        print(f"Mann-Whitney U (two-sided): U={u:.1f} p={p:.4f}")
        t, pt = stats.ttest_ind(g1c, g0c, equal_var=False)
        print(f"Welch t-test: t={t:.3f} p={pt:.4f}")


if __name__ == "__main__":
    main()
