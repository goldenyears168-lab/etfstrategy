#!/usr/bin/env python3
"""Read-only diagnostic (2026-08-10): trace exactly why night|div_hh_weak_vol
(block=["L","S"]) let want_l=45038.0 leak through on tonight's live session,
using the SAME fetch_1m_bars() + desired_from_simulate() path production
uses. No orders placed -- market data query only.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

from order.fubon_session import connect_fubon  # noqa: E402
from order.tmf_channel_config import load_tmf_channel_order_config  # noqa: E402
from order.tmf_channel_marketdata import bars_to_arrays, fetch_1m_bars, resolve_front_symbol  # noqa: E402
from order.tmf_channel_order import _drop_forming_last_bar  # noqa: E402
from order.tmf_channel_ledger import trading_day_str  # noqa: E402
from tmf_channel.engine import simulate  # noqa: E402


def main():
    cfg = load_tmf_channel_order_config()
    session = connect_fubon(realtime=True)
    sym, name, end = resolve_front_symbol(session, product=cfg.product)
    print(f"symbol={sym} name={name} end={end}")

    bars = fetch_1m_bars(session, sym)
    bars = _drop_forming_last_bar(bars)
    print(f"bars={len(bars)} last_t={bars[-1].get('t')}")

    day = trading_day_str()
    O, H, L, C, V, T = bars_to_arrays(day, bars)
    run_recipe = dict(cfg.recipe)
    run_recipe["hang_anchor"] = "O"
    run_recipe["eod_flatten"] = False

    trades, events, ws, wl, rvol, regime, open_pos = simulate(
        O, H, L, C, V, T, run_recipe, vix_delta={}
    )
    print(f"\nfinal: open_pos={open_pos} ws[-1]={ws[-1]} wl[-1]={wl[-1]} regime[-1]={regime[-1]}")
    print(f"trades count={len(trades)}, last 3 trades: {trades[-3:]}")

    print("\n--- last 40 bars: t | regime | ws | wl ---")
    for i in range(max(0, len(T) - 40), len(T)):
        print(f"{T[i]} | {regime[i]} | ws={ws[i]} | wl={wl[i]}")

    print(f"\n--- last 60 events (of {len(events)}) ---")
    for ev in events[-60:]:
        print(ev)


if __name__ == "__main__":
    main()
