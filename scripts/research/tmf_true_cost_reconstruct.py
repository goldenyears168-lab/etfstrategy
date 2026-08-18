#!/usr/bin/env python3
"""TMF 真實來回成本反推 — 引擎裡的 COST=3.0 到底對不對？

為什麼這是現在最該算的數字：60 日 tick 回放顯示策略**毛額是正的**
（+5,866 pts / 4,073 筆 = +1.44 pts/筆），淨額卻是 −6,353 pts。差額 100%
來自那個假設出來的 COST=3.0 常數 × 筆數。整條策略的存亡就掛在這個常數上，
而它從來沒有用真實對帳資料驗證過。

三個成本成分，分別用不同方法取得：

  1. 期貨交易稅 — 法定，可精確計算。股價類期貨契約十萬分之二（0.00002）
     按契約金額課徵、單邊。微型臺指契約金額 = 指數 × NT$10，而 1 點 = NT$10，
     所以 tax_pts = 0.00002 × 指數（指數 46,000 時 = 0.92 點/邊）。

  2. 滑價 — 用真實資料量。Fubon 的帳戶推播事件同時給了
     ``FutOptFilledData.filled_price``（真成交價）與 ``FutOptOrderResult.price``
     ／``after_price``（下單意圖價），用 ``order_no`` 就能接起來，**不需要任何
     外部參考價**。這正是 tmf_futopt_fill_event_listener.py 當初的動機：
     市價出場（exit_market）從來沒有在任何地方留下確認成交價，等於在最不該
     假設零滑價的時候假設了零滑價。

  3. 手續費 — 資料裡沒有，也不可能有（那是券商合約）。當成參數掃描，
     輸出一張敏感度表，讓真實費率由你填。

資料：$GOLDENSTOCKS_DATA_DIR/cache/tmf_channel/tick_seconds/futopt_fill_events_*.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics as st
from collections import defaultdict
from pathlib import Path
from typing import Any

PT_VALUE_TWD = 10.0          # 微型臺指 1 點 = NT$10
FUTURES_TAX_RATE = 0.00002   # 股價類期貨契約交易稅（單邊）
ENGINE_COST_PTS = 3.0        # causal_engine legacy_helpers.COST
GROSS_PTS_PER_TRADE = 1.44   # 60 日 tick 回放實測（fill_model=through）


def events_dir() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_channel" / "tick_seconds"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_channel" / "tick_seconds"


def load_events(paths: list[Path]) -> tuple[list[dict], dict[str, list[dict]]]:
    """(fills, orders_by_order_no) — fills come from the separate
    ``futopt_filled`` callback, order state from ``order_futopt_changed`` /
    ``futopt_order``. Note ``status==50`` never appears in this stream; that
    value belongs to get_order_results_detail's schema, a different surface
    (order.fubon_futopt_orders.fetch_real_fill_price assumes it — worth a look)."""
    fills: list[dict] = []
    orders: dict[str, list[dict]] = defaultdict(list)
    for p in paths:
        if not p.exists():
            continue
        for line in p.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            pay = d.get("payload")
            if not isinstance(pay, dict):
                continue
            rec = {"ts": d.get("ts"), **pay}
            if d.get("payload_type") == "FutOptFilledData":
                fills.append(rec)
            elif d.get("payload_type") == "FutOptOrderResult":
                on = str(pay.get("order_no") or "")
                if on:
                    orders[on].append(rec)
    fills.sort(key=lambda r: str(r.get("ts")))
    return fills, orders


def side_of(bs: Any) -> str | None:
    s = str(bs or "").lower()
    if "buy" in s:
        return "B"
    if "sell" in s:
        return "S"
    return None


def intent_price(order_events: list[dict]) -> tuple[float | None, str | None]:
    """Last known working price before the fill, plus the order's price type.

    ``price`` is the original submitted price and ``after_price`` tracks
    amendments (the reconciler amends resting rails rather than cancel+place),
    so the *intent* at fill time is the latest non-zero ``after_price``.
    """
    ptype = None
    best = None
    for e in order_events:
        ptype = ptype or str(e.get("price_type") or "") or None
        for k in ("after_price", "price"):
            v = e.get(k)
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            if fv > 0:
                best = fv
                break
    return best, ptype


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fee-twd", type=float, nargs="+", default=[10, 15, 20, 25, 30],
                    help="每邊手續費 NT$（掃描；請填你對帳單上的真實費率）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    paths = sorted(events_dir().glob("futopt_fill_events_*.jsonl"))
    fills, orders = load_events(paths)
    print(f"事件檔 {len(paths)} 份：{', '.join(p.name for p in paths)}")
    print(f"真實成交（FutOptFilledData）{len(fills)} 筆 · 委託單 {len(orders)} 張\n")
    if not fills:
        print("沒有成交事件——listener 需要在有交易的時段跑著才會收到推播")
        return 1

    rows: list[dict[str, Any]] = []
    for f in fills:
        on = str(f.get("order_no") or "")
        side = side_of(f.get("buy_sell"))
        try:
            filled = float(f["filled_price"])
        except (KeyError, TypeError, ValueError):
            continue
        want, ptype = intent_price(orders.get(on, []))
        # 成本為正：買進成交價高於意圖 = 多付；賣出成交價低於意圖 = 少收
        slip = None
        if want and side:
            slip = (filled - want) if side == "B" else (want - filled)
        rows.append({"ts": f.get("ts"), "order_no": on, "side": side,
                     "filled": filled, "intent": want, "price_type": ptype,
                     "slip_pts": slip,
                     "tax_pts": FUTURES_TAX_RATE * filled})

    matched = [r for r in rows if r["slip_pts"] is not None]
    print("=== 1. 滑價（真實成交價 vs 下單意圖價，正值＝付出成本）===")
    print(f"   可配對 {len(matched)}/{len(rows)} 筆")
    if matched:
        xs = [r["slip_pts"] for r in matched]
        s = sorted(xs)
        print(f"   mean={st.mean(xs):+.3f} pts   median={s[len(s)//2]:+.3f}   "
              f"min={min(xs):+.1f}   max={max(xs):+.1f}   "
              f"「拿到意圖價或更好」占比={100.0*sum(1 for x in xs if x <= 0)/len(xs):.0f}%")
        by_type: dict[str, list[float]] = defaultdict(list)
        for r in matched:
            by_type[str(r["price_type"] or "?")].append(r["slip_pts"])
        for k, v in sorted(by_type.items()):
            print(f"     {k:<28} n={len(v):>3}  mean={st.mean(v):+.3f} pts")
        worst = sorted(matched, key=lambda r: -r["slip_pts"])[:3]
        for r in worst:
            if r["slip_pts"] > 0:
                print(f"     最差：{r['ts'][:19]} {r['side']} 意圖 {r['intent']:.0f} "
                      f"→ 成交 {r['filled']:.0f} = {r['slip_pts']:+.0f} pts")
    slip_mean = st.mean([r["slip_pts"] for r in matched]) if matched else 0.0

    print("\n=== 2. 期貨交易稅（法定 0.00002 × 契約金額，單邊）===")
    taxes = [r["tax_pts"] for r in rows]
    tax_side = st.mean(taxes)
    print(f"   平均成交指數 {st.mean([r['filled'] for r in rows]):,.0f}"
          f" → {tax_side:.3f} pts/邊 = NT${tax_side*PT_VALUE_TWD:.1f}/邊"
          f" → 來回 {2*tax_side:.3f} pts")

    print("\n=== 3. 來回總成本敏感度（手續費為未知數）===")
    print("   來回成本 = 2×手續費 + 2×交易稅 + 2×滑價")
    print(f"   {'手續費(NT$/邊)':<16}{'費(pts)':>10}{'稅(pts)':>10}{'滑價(pts)':>11}"
          f"{'來回總計':>10}{'vs COST=3.0':>13}{'毛額1.44夠付?':>15}")
    table = []
    for fee in args.fee_twd:
        fee_pts = 2 * fee / PT_VALUE_TWD
        tax_pts = 2 * tax_side
        slip_pts = 2 * slip_mean
        total = fee_pts + tax_pts + slip_pts
        ok = "是" if GROSS_PTS_PER_TRADE > total else "否"
        table.append({"fee_twd_per_side": fee, "total_pts": round(total, 3),
                      "fee_pts": round(fee_pts, 3), "tax_pts": round(tax_pts, 3),
                      "slip_pts": round(slip_pts, 3), "covers_gross": ok == "是"})
        print(f"   {fee:<16.0f}{fee_pts:>10.2f}{tax_pts:>10.2f}{slip_pts:>11.2f}"
              f"{total:>10.2f}{total-ENGINE_COST_PTS:>+13.2f}{ok:>15}")

    print("\n=== 4. 對策略的意涵 ===")
    lo, hi = table[0]["total_pts"], table[-1]["total_pts"]
    print(f"   真實來回成本區間 {lo:.2f} ~ {hi:.2f} pts（視手續費而定）")
    print(f"   引擎假設 COST = {ENGINE_COST_PTS:.2f} pts")
    print(f"   實測毛額 = {GROSS_PTS_PER_TRADE:.2f} pts/筆")
    need = GROSS_PTS_PER_TRADE
    fee_break = (need - 2 * tax_side - 2 * slip_mean) / 2 * PT_VALUE_TWD
    print(f"   → 損益兩平所需的手續費上限 = NT${fee_break:.1f}/邊"
          f"（{'不可能，稅+滑價已超過毛額' if fee_break <= 0 else '需低於此值'}）")
    daily = 67.9
    print(f"   → 以 {daily:.0f} 筆/日計，每多 1 點成本 = 每日多虧 "
          f"{daily:.0f} pts = NT${daily*PT_VALUE_TWD:,.0f}")

    if args.out:
        payload = {"schema": "tmf-true-cost-v1",
                   "n_fills": len(rows), "n_matched": len(matched),
                   "slip_pts_per_side_mean": round(slip_mean, 4),
                   "tax_pts_per_side": round(tax_side, 4),
                   "engine_cost_pts": ENGINE_COST_PTS,
                   "gross_pts_per_trade": GROSS_PTS_PER_TRADE,
                   "breakeven_fee_twd_per_side": round(fee_break, 2),
                   "sensitivity": table, "fills": rows}
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
