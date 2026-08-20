#!/usr/bin/env python3
"""成本線 v2 — 把出場參考點從 mid 改成「觸發那筆 tick 的成交價」，並用實測價差取代外推。

為什麼要改參考點
----------------
``causal_engine.close_side(t, px, ...)`` 把出場記在**觸發那筆 tick 的成交價** px
（``px = tk_px[k]``），不是 mid。而 76 個百分點的市價出場是**逆向觸發**
（trail 39.4% / struct_break 36.6%；opp_cover 21.8% 是順向），也就是說那筆觸發
成交**已經打在你要交易的那一側**。用「隨機時刻 mid 到對側」的半價差去估這個成本，
系統性高估。

2026-08-20 的對抗複核實測（08-17/08-18 五檔＋逐筆）：
    逆向觸發成本 = 0.234 × 價差
      · 用成交自帶的同時報價：0.29–0.35 點
      · 用 book 對齊（lag 0/1/5/20 秒都算過）：0.69–0.83 點，對延遲穩定
先前假設的 0.5 × 價差（1.414 / 1.618 點）遠高於這兩者。

後果不只是數字變好看：**MXF 的判決會翻號。**先前「fee 30 元/邊時數學上不可能
為正」是 mid 參考點造出來的假象。

成本式（每筆，點數）
--------------------
    fee   = 2 × (手續費TWD/邊 ÷ 每點價值)
    tax   = 2 × (0.00002 × 指數)          # 點數上尺度不變，三個商品相同
    entry = −limit_slip                    # 掛單成交相對委託價的優惠（單邊）
    exit  = market_share × k_adverse × spread + (1−market_share) × (−limit_slip)
    total = fee + tax + entry + exit

三個商品的每點價值：TMF NT$10、MXF NT$50、TXF NT$200。
交易稅在點數上尺度不變，所以**手續費是唯一能靠換工具大幅壓縮的項目**。

用法
----
    PYTHONPATH=src .venv/bin/python scripts/research/tmf_cost_line_v2_trigger_anchor.py \\
        --measure-spreads --days 2026-08-20 --json-out reports/research/channel_lab/cost_line_v2.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
MAX_BOOK_AGE_SEC = 5.0
TAX_RATE = 0.00002
#: 逆向觸發的市價出場成本係數（× 價差）。0.234 是 trade-attached quote 版；
#: book 對齊版換算約 0.24–0.29，兩者接近。悲觀端見 --k-adverse。
K_ADVERSE = 0.234
PT_VALUE = {"TMF": 10.0, "MXF": 50.0, "TXF": 200.0}
#: 出場組成（引擎 40 天 2,057 筆實跑）：trail 39.4 / struct_break 36.6
#: / opp_cover 21.8 / 其他 1.6 → 市價（非 opp_cover）78%，其中 76pp 逆向觸發。
MARKET_SHARE = 0.78
LIMIT_SLIP = 0.40      # 掛單成交相對委託價的優惠（單邊，25 筆存活成交實測）
GROSS_PTS = 2.86       # 掛單距離 ×2 之後的每筆毛額（三段 60 日窗最小值）


def books_dir(root: str) -> Path:
    try:
        import stock_db
        base = Path(stock_db.DATA_DIR).parent
    except Exception:  # noqa: BLE001
        base = Path.home() / "goldenstocks-data"
    return base / "cache" / f"{root.lower()}_books"


def measure(root: str, days: list[str]) -> dict | None:
    """時間加權的價差與 qty=1 市價滑價。加權＝到下一筆快照的持續時間，
    也就是「隨機時刻送單」的期望值，不是每列一票。"""
    rows: list[tuple[datetime, float, float, float]] = []
    for day in days:
        p = books_dir(root) / f"{root.lower()}_books_{day}.jsonl"
        if not p.exists():
            continue
        for line in p.open(encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            b, a = r.get("bids") or [], r.get("asks") or []
            if not b or not a or b[0].get("price") is None or a[0].get("price") is None:
                continue
            if r.get("stale"):
                continue
            try:
                t = datetime.fromtimestamp(float(r["book_time"]) / 1e6, tz=TZ)
                if "stale" not in r:
                    wall = datetime.fromisoformat(str(r["ts"])).astimezone(TZ)
                    if (wall - t).total_seconds() > MAX_BOOK_AGE_SEC:
                        continue
            except (KeyError, TypeError, ValueError):
                continue
            b1, a1 = float(b[0]["price"]), float(a[0]["price"])
            if a1 <= b1:
                continue
            mid = 0.5 * (a1 + b1)
            # qty=1 的市價滑價 = 吃第一檔，恆等於半個價差（深度在 qty=1 不構成限制）
            rows.append((t, a1 - b1, mid, 0.5 * (a1 - b1)))
    if len(rows) < 100:
        return None
    rows.sort(key=lambda r: r[0])
    tot_w = sw = sl = 0.0
    for i in range(len(rows) - 1):
        w = (rows[i + 1][0] - rows[i][0]).total_seconds()
        if w <= 0 or w > 60:      # 跨 session 缺口不計入
            continue
        tot_w += w
        sw += w * rows[i][1]
        sl += w * rows[i][3]
    if tot_w <= 0:
        return None
    return {
        "n_snapshots": len(rows),
        "weighted_sec": round(tot_w, 1),
        "window": f"{rows[0][0]:%F %T} → {rows[-1][0]:%F %T}",
        "tw_spread_pts": round(sw / tot_w, 4),
        "tw_market_slip_qty1_pts": round(sl / tot_w, 4),
        "index_level": round(sum(r[2] for r in rows) / len(rows), 1),
    }


def cost_line(*, root: str, spread: float, fee_twd: float, index: float,
              k_adverse: float = K_ADVERSE, market_share: float = MARKET_SHARE,
              limit_slip: float = LIMIT_SLIP) -> dict:
    fee = 2.0 * (fee_twd / PT_VALUE[root])
    tax = 2.0 * (TAX_RATE * index)
    entry = -limit_slip
    exit_ = market_share * k_adverse * spread + (1.0 - market_share) * (-limit_slip)
    total = fee + tax + entry + exit_
    return {
        "fee_pts": round(fee, 4), "tax_pts": round(tax, 4),
        "entry_pts": round(entry, 4), "exit_pts": round(exit_, 4),
        "roundtrip_pts": round(total, 4),
        "net_per_trade_pts": round(GROSS_PTS - total, 4),
        "net_per_trade_twd": round((GROSS_PTS - total) * PT_VALUE[root], 1),
    }


def breakeven_fee(*, root: str, spread: float, index: float, **kw) -> float:
    """讓 net = 0 的手續費（TWD/邊）。"""
    base = cost_line(root=root, spread=spread, fee_twd=0.0, index=index, **kw)
    slack = GROSS_PTS - base["roundtrip_pts"]          # 還能付多少點的手續費（來回）
    return slack * PT_VALUE[root] / 2.0


def breakeven_spread(*, root: str, fee_twd: float, index: float,
                     k_adverse: float = K_ADVERSE,
                     market_share: float = MARKET_SHARE, **kw) -> float:
    base = cost_line(root=root, spread=0.0, fee_twd=fee_twd, index=index,
                     k_adverse=k_adverse, market_share=market_share, **kw)
    slack = GROSS_PTS - base["roundtrip_pts"]
    denom = market_share * k_adverse
    return slack / denom if denom > 0 else float("inf")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", default="2026-08-20", help="逗號分隔")
    ap.add_argument("--measure-spreads", action="store_true")
    ap.add_argument("--index", type=float, default=None, help="不給就用實測 mid")
    ap.add_argument("--k-adverse", type=float, default=K_ADVERSE)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    days = [d.strip() for d in args.days.split(",") if d.strip()]

    measured: dict[str, dict] = {}
    if args.measure_spreads:
        print(f"=== 實測價差（時間加權）· days={days} ===")
        for root in ("TMF", "MXF", "TXF"):
            m = measure(root, days)
            if m:
                measured[root] = m
                print(f"  {root}: 價差 {m['tw_spread_pts']:.3f} 點 · "
                      f"qty1 市價滑價 {m['tw_market_slip_qty1_pts']:.3f} · "
                      f"指數 {m['index_level']:.0f} · "
                      f"加權 {m['weighted_sec']:.0f} 秒 · n={m['n_snapshots']:,}")
                print(f"        窗口 {m['window']}")
            else:
                print(f"  {root}: 資料不足")

    idx = args.index or (measured.get("TMF", {}).get("index_level") or 44900.0)
    print(f"\n=== 成本線 v2（觸發錨 · k_adverse={args.k_adverse} · 指數={idx:.0f} · "
          f"gross={GROSS_PTS} · 市價比例={MARKET_SHARE}）===")
    print(f"{'商品':<6}{'價差來源':<10}{'價差':>7}{'fee/邊':>8}{'手續費':>8}"
          f"{'稅':>7}{'進場':>7}{'出場':>7}{'來回':>8}{'每筆淨':>9}{'每筆NT$':>10}")
    rows_out = []
    for root, fees in (("TMF", (15.0,)), ("MXF", (30.0, 25.0, 20.0)), ("TXF", (30.0, 25.0))):
        m = measured.get(root)
        spread = m["tw_spread_pts"] if m else {"TMF": 2.83, "MXF": 1.0, "TXF": 1.0}[root]
        src = "實測" if m else "外推"
        for fee in fees:
            c = cost_line(root=root, spread=spread, fee_twd=fee, index=idx,
                          k_adverse=args.k_adverse)
            print(f"{root:<6}{src:<10}{spread:>7.3f}{fee:>8.0f}{c['fee_pts']:>8.3f}"
                  f"{c['tax_pts']:>7.3f}{c['entry_pts']:>7.3f}{c['exit_pts']:>7.3f}"
                  f"{c['roundtrip_pts']:>8.3f}{c['net_per_trade_pts']:>9.3f}"
                  f"{c['net_per_trade_twd']:>10.0f}")
            rows_out.append({"root": root, "spread_source": src, "spread": spread,
                             "fee_twd_per_side": fee, **c})

    print("\n=== 損益兩平門檻 ===")
    for root in ("TMF", "MXF", "TXF"):
        m = measured.get(root)
        spread = m["tw_spread_pts"] if m else {"TMF": 2.83, "MXF": 1.0, "TXF": 1.0}[root]
        bf = breakeven_fee(root=root, spread=spread, index=idx, k_adverse=args.k_adverse)
        bs = breakeven_spread(root=root, fee_twd=30.0 if root != "TMF" else 15.0,
                              index=idx, k_adverse=args.k_adverse)
        print(f"  {root}: 需手續費 < {bf:>7.1f} 元/邊（@價差 {spread:.3f}） · "
              f"可容忍價差 < {bs:>6.2f} 點（@fee {'15' if root == 'TMF' else '30'} 元）")

    if args.json_out:
        p = Path(args.json_out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "schema": "tmf-cost-line-v2",
            "reference_point": "trigger_tick_fill_price (not mid)",
            "k_adverse": args.k_adverse, "index": idx,
            "gross_pts_per_trade": GROSS_PTS, "market_exit_share": MARKET_SHARE,
            "limit_slip_pts_per_side": LIMIT_SLIP, "tax_rate": TAX_RATE,
            "measured_spreads": measured, "cost_rows": rows_out,
            "breakeven": {r: {
                "fee_twd_per_side": round(breakeven_fee(
                    root=r, index=idx, k_adverse=args.k_adverse,
                    spread=(measured.get(r, {}).get("tw_spread_pts")
                            or {"TMF": 2.83, "MXF": 1.0, "TXF": 1.0}[r])), 2),
                "spread_pts": round(breakeven_spread(
                    root=r, fee_twd=15.0 if r == "TMF" else 30.0,
                    index=idx, k_adverse=args.k_adverse), 3),
            } for r in ("TMF", "MXF", "TXF")},
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
