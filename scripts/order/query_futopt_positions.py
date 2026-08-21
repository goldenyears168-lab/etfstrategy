#!/usr/bin/env python3
"""列出富邦期貨帳戶的所有未平倉部位（**唯讀**，不送任何委託）。

現有的 ``query_tmf_broker_net`` 只回傳 TMF 淨部位，本工具不做標的過濾，
用於盤點整個期貨帳戶（含個股期貨）。

⚠️ 會執行一次 ``sdk.login()``。若 TMF 常駐 worker 正在跑，重複登入是否會踢掉
既有 session 尚未驗證——先確認 worker 狀態再跑。斷線復原用
``scripts/order/tmf_cutover.sh``。

用法::

    PYTHONPATH=src .venv-fubon/bin/python scripts/order/query_futopt_positions.py
"""
from __future__ import annotations

import sys

from order.fubon_futopt_orders import pick_futopt_account
from order.fubon_session import connect_fubon


def _attr(row, *names):
    for n in names:
        v = getattr(row, n, None)
        if v is not None:
            return v
    return None


def main() -> int:
    session = connect_fubon()
    acc = pick_futopt_account(session)
    print(f"期貨帳號：{getattr(acc, 'account', '?')} / {getattr(acc, 'branch_no', '?')}")

    fa = getattr(session.sdk, "futopt_accounting", None)
    if fa is None:
        print("SDK 無 futopt_accounting")
        return 1
    res = fa.query_hybrid_position(acc)
    rows = getattr(res, "data", None)
    if rows is None:
        print(f"查詢失敗：{getattr(res, 'message', res)}")
        return 1
    if not rows:
        print("目前無未平倉部位")
        return 0

    print(f"\n未平倉部位 {len(rows)} 筆")
    print(f"{'symbol':<16}{'買賣':<6}{'口數':>6}{'均價':>12}  其他")
    print("-" * 78)
    for r in rows:
        sym = str(_attr(r, "symbol") or "")
        bs = str(_attr(r, "buy_sell") or "")
        bs = bs.split(".")[-1] if "." in bs else bs
        lots = _attr(r, "orig_lots", "lots", "qty")
        px = _attr(r, "price", "avg_price")
        extra = {k: v for k, v in vars(r).items()
                 if k not in {"symbol", "buy_sell", "orig_lots", "price"}} \
            if hasattr(r, "__dict__") else {}
        print(f"{sym:<16}{bs:<6}{str(lots):>6}{str(px):>12}  {extra if extra else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
