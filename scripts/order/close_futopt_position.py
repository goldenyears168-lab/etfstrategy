#!/usr/bin/env python3
"""平掉指定期貨／個股期貨部位（預設 dry-run，實彈需明確開旗標）。

通用版：``--acct-symbol`` 指定要平哪個商品，``--expect-lots`` 可省略（省略時
採用實際查到的可平倉口數，但仍受 ``--max-lots`` 上限保護）。``--list`` 可先
唯讀列出目前所有部位。

**設計原則：執行時重新查部位，絕不寫死口數。** 若部位已不存在、方向或口數與
預期不符，一律**不送單**並回報——寧可漏平也不要開出反向新倉。

安全層（與 CLAUDE.md 的下單層規範一致）：

1. ``--dry-run`` 為預設；實彈要同時 ``--live`` 與環境變數
   ``ORDER_CLOSE_FUTOPT_LIVE=1``（雙鎖，避免手滑）
2. ``--expect-symbol/--expect-side/--expect-lots`` 三者全部比對通過才送單
3. lockdir 防重複送單。**鎖在送單前一刻才建立**——連線或查部位階段失敗不會
   燒掉當日機會，可以直接重跑；但只要送出過就不會再送第二次
4. ``--session-window`` 限制只在指定時段內執行

⚠️ **已知限制**：``build_futopt_order`` 對限價做 ``int(round(price))``。台指期是
整數跳動所以無影響，但個股期貨在 100~500 元區間最小跳動為 0.5，限價會被抹成
整數（最多差 0.5 點）。要精確控價請改用 market。

用法::

    # 乾跑（預設）
    PYTHONPATH=src .venv-fubon/bin/python scripts/order/close_futopt_position.py \
        --acct-symbol FIHBF --expect-side Buy --expect-lots 2 --price-type market

    # 實彈（雙鎖）
    ORDER_CLOSE_FUTOPT_LIVE=1 PYTHONPATH=src .venv-fubon/bin/python \
        scripts/order/close_futopt_position.py --acct-symbol FIHBF \
        --expect-side Buy --expect-lots 2 --price-type market --live
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from order.fubon_futopt_orders import (
    FutOptResolvedOrder,
    pick_futopt_account,
    place_futopt_order,
)
from order.fubon_session import connect_fubon


def _side_of(row) -> str | None:
    v = str(getattr(row, "buy_sell", "") or "")
    v = v.split(".")[-1].lower()
    return "Buy" if v.startswith("b") else ("Sell" if v.startswith("s") else None)


def read_position(session, acct_symbol: str) -> dict | None:
    acc = pick_futopt_account(session)
    fa = getattr(session.sdk, "futopt_accounting", None)
    if fa is None:
        return None
    res = fa.query_hybrid_position(acc)
    for row in getattr(res, "data", None) or []:
        if str(getattr(row, "symbol", "")).upper() != acct_symbol.upper():
            continue
        side = _side_of(row)
        try:
            lots = int(getattr(row, "orig_lots", 0) or 0)
            tradable = int(getattr(row, "tradable_lot", 0) or 0)
        except (TypeError, ValueError):
            continue
        if side and lots > 0:
            return {
                "symbol": str(getattr(row, "symbol", "")),
                "side": side,
                "lots": lots,
                "tradable_lot": tradable,
                "avg_price": getattr(row, "price", None),
                "market_price": getattr(row, "market_price", None),
                "expiry": str(getattr(row, "expiry_date", "") or ""),
            }
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--acct-symbol", help="帳務 symbol，例如 FIHBF；配 --list 時可省略")
    ap.add_argument("--list", action="store_true",
                    help="只列出目前所有未平倉部位並結束（唯讀，不送單）")
    ap.add_argument("--trade-symbol", default=None,
                    help="下單用 symbol（預設沿用帳務回報的 symbol）")
    ap.add_argument("--expect-side", choices=["Buy", "Sell"],
                    help="預期部位方向；配 --list 時可省略。平倉會送出反向單")
    ap.add_argument("--expect-lots", type=int, default=None,
                    help="不給則採用實際查到的口數；給了就必須完全相符才送單")
    ap.add_argument("--expect-expiry", default=None, help="例如 202609")
    ap.add_argument("--max-lots", type=int, default=10,
                    help="安全上限：送單口數超過此值一律中止（預設 10）")
    ap.add_argument("--price-type", default="market", choices=["market", "limit"])
    ap.add_argument("--price", type=float, default=None)
    ap.add_argument("--tif", default="ioc", choices=["rod", "ioc", "fok"])
    ap.add_argument("--session-window", default="08:45-13:40",
                    help="只在此時窗內執行（HH:MM-HH:MM）")
    ap.add_argument("--lockdir", default=None)
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    if args.list:
        session = connect_fubon()
        acc = pick_futopt_account(session)
        fa = getattr(session.sdk, "futopt_accounting", None)
        rows = getattr(fa.query_single_position(acc), "data", None) or [] if fa else []
        for r in rows:
            side = _side_of(r)
            print(json.dumps({
                "symbol": str(getattr(r, "symbol", "")),
                "side": side,
                "lots": getattr(r, "orig_lots", None),
                "avg_price": getattr(r, "price", None),
                "market_price": getattr(r, "market_price", None),
                "expiry": str(getattr(r, "expiry_date", "") or ""),
                "pnl": getattr(r, "profit_or_loss", None),
                "order_no": getattr(r, "order_no", None),
            }, ensure_ascii=False, default=str))
        if not rows:
            print(json.dumps({"note": "無未平倉部位"}, ensure_ascii=False))
        return 0
    if not args.acct_symbol or not args.expect_side:
        raise SystemExit("--acct-symbol 與 --expect-side 為必填（除非用 --list）")

    now = datetime.now()
    lo, hi = args.session_window.split("-")
    if not (lo <= now.strftime("%H:%M") <= hi):
        print(json.dumps({"ok": False, "skip": "outside_session_window",
                          "now": now.strftime("%H:%M"), "window": args.session_window},
                         ensure_ascii=False))
        return 0

    lock = Path(args.lockdir or f"/tmp/close_futopt_{args.acct_symbol}_{now:%Y%m%d}.lock")
    if lock.exists():
        print(json.dumps({"ok": False, "skip": "already_submitted_today",
                          "lock": str(lock)}, ensure_ascii=False))
        return 0

    live = bool(args.live) and os.environ.get("ORDER_CLOSE_FUTOPT_LIVE") == "1"
    session = connect_fubon()
    pos = read_position(session, args.acct_symbol)
    out: dict = {"ts": now.isoformat(timespec="seconds"), "live": live,
                 "acct_symbol": args.acct_symbol, "position": pos}

    if pos is None:
        out.update(ok=True, action="no_position", note="部位不存在，不送單")
        print(json.dumps(out, ensure_ascii=False))
        return 0

    mismatch = []
    if pos["side"] != args.expect_side:
        mismatch.append(f"side {pos['side']} != {args.expect_side}")
    if args.expect_lots is not None and pos["lots"] != args.expect_lots:
        mismatch.append(f"lots {pos['lots']} != {args.expect_lots}")
    if args.expect_expiry and pos["expiry"] != args.expect_expiry:
        mismatch.append(f"expiry {pos['expiry']} != {args.expect_expiry}")
    if mismatch:
        out.update(ok=False, action="abort_mismatch", mismatch=mismatch,
                   note="部位與預期不符，拒絕送單")
        print(json.dumps(out, ensure_ascii=False))
        return 1

    lots = pos["tradable_lot"] or pos["lots"]
    if lots > args.max_lots:
        out.update(ok=False, action="abort_exceeds_max_lots",
                   note=f"可平倉 {lots} 口 > --max-lots {args.max_lots}，拒絕送單")
        print(json.dumps(out, ensure_ascii=False))
        return 1
    if lots <= 0:
        out.update(ok=True, action="no_tradable_lot", note="可平倉口數為 0")
        print(json.dumps(out, ensure_ascii=False))
        return 0

    resolved = FutOptResolvedOrder(
        symbol=args.trade_symbol or pos["symbol"],
        buy_sell="Sell" if pos["side"] == "Buy" else "Buy",   # 平倉＝反向
        lot=lots,
        price=args.price if args.price_type == "limit" else None,
        price_type=args.price_type,
        time_in_force=args.tif,
        order_type="close",                                    # 必須是平倉，不是新倉
        market_type="future",
        user_def="closepos",
    )
    # 送單前才上鎖：連線／查詢階段失敗不燒掉當日的機會，但送出後絕不重送。
    try:
        lock.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        out.update(ok=False, action="race_locked", note="另一個行程已送出")
        print(json.dumps(out, ensure_ascii=False))
        return 0
    res = place_futopt_order(session, resolved, dry_run=not live)
    out.update(ok=True, action="submitted" if live else "dry_run", order=res)
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
