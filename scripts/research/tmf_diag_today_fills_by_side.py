#!/usr/bin/env python3
"""Read-only diagnostic: count today's actual TMF fills by side (L=bottom
rail touched, S=top rail touched), from the real broker order-results feed.
No orders placed -- market/account data query only.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

from order.fubon_futopt_orders import (  # noqa: E402
    _bs_to_side,
    get_futopt_order_results,
    is_tmf_acct_symbol,
)
from order.fubon_session import connect_fubon  # noqa: E402
from order.tmf_channel_config import load_tmf_channel_order_config  # noqa: E402
from order.tmf_channel_marketdata import resolve_front_symbol  # noqa: E402


def main():
    cfg = load_tmf_channel_order_config()
    session = connect_fubon(realtime=True)
    sym, name, end = resolve_front_symbol(session, product=cfg.product)
    print(f"symbol={sym} name={name} end={end}")

    results = get_futopt_order_results(session, market_type=None)
    print(f"total order records today: {len(results)}")

    rows = []
    for item in results:
        symbol = str(getattr(item, "symbol", "") or "")
        if not is_tmf_acct_symbol(symbol, front_symbol=sym):
            continue
        side = _bs_to_side(getattr(item, "buy_sell", None))
        filled_lot = getattr(item, "filled_lot", None)
        try:
            filled_lot = int(filled_lot or 0)
        except (TypeError, ValueError):
            filled_lot = 0
        status = getattr(item, "status", None)
        status_name = str(getattr(status, "name", status) or "")
        price = getattr(item, "after_price", None) or getattr(item, "price", None)
        order_no = getattr(item, "order_no", None) or getattr(item, "seq_no", None)
        last_time = getattr(item, "last_time", None)
        rows.append(dict(
            order_no=order_no, side=side, filled_lot=filled_lot,
            status=status_name, price=price, last_time=last_time,
            order_type=str(getattr(item, "order_type", "") or ""),
        ))

    filled = [r for r in rows if r["filled_lot"] > 0]
    print(f"\nTMF order records: {len(rows)}, with filled_lot>0: {len(filled)}")
    for r in filled:
        print(r)

    l_fills = sum(r["filled_lot"] for r in filled if r["side"] == "L")
    s_fills = sum(r["filled_lot"] for r in filled if r["side"] == "S")
    print(f"\n=== FILL COUNT BY SIDE (lots) ===")
    print(f"L (bottom rail touched / buy fills): {l_fills}")
    print(f"S (top rail touched / sell fills): {s_fills}")


if __name__ == "__main__":
    main()
