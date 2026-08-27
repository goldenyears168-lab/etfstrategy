#!/usr/bin/env python3
"""從五檔 book 實測「市價出場要付多少點」——TMF vs MXF vs TXF 的成本對照。

為什麼這支是目前整個系統裡最該做的一次量測
------------------------------------------
2026-08-19 的 TMF 調查得到第一個正期望值組合，但它整條押在**一個還沒量到的數字**上：

    微台 TMF ×2.0   毛 2.86 / 成本 4.05 = −1.19/筆
    小台 MXF ×2.0   毛 2.86 / 成本 1.65 = +1.21/筆   ← 唯一為正

那個 1.65 是把**微台量到的限價滑價**直接套到小台上算的，而 **78% 的出場走市價單、
其市價滑價從未被量過**。commit a1d50ce 自己寫明：「若市價出場付掉半個價差，小台成本
→ 2.85、每筆 → +0.01，剛好損益兩平。整個結論押在那一個還沒量到的數字上。」

因為 TMF 目前是 dry_run（trade journal 只有 hold 事件、沒有真實成交），無法從實際
fill 反推滑價。但**五檔 book 可以直接回答這個問題**：市價單的成本就是「吃掉對手方
掛單」相對中價的差距，而那完全由 book 決定，不需要真的送單。這比等幾個月累積 fill
更快、也不用拿真錢換資料。

量什麼
------
對每一筆 book 快照，計算送出 q 口市價單的實際成交均價與中價的差：

    市價買 q 口 → 依序吃 asks[0..k]，slippage = VWAP(asks, q) − mid
    市價賣 q 口 → 依序吃 bids[0..k]，slippage = mid − VWAP(bids, q)

回報時間加權分布（快照間距不均，用持續時間加權才是「隨機時刻送單」的期望值），
並分日盤／夜盤。同時回報「深度不足以吃滿 q 口」的比例——那是尾部風險，平均值看不到。

用法
----
    PYTHONPATH=src .venv/bin/python scripts/research/futopt_market_exit_slippage.py \\
        --root TMF --days 2026-08-18,2026-08-19 --qty 1

    # 收滿 MXF/TXF 之後做三方對照
    PYTHONPATH=src .venv/bin/python scripts/research/futopt_market_exit_slippage.py \\
        --root TMF,MXF,TXF --qty 1
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from bisect import bisect_left
from datetime import datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT_DIR / "src"))

import stock_db  # noqa: E402

CACHE = stock_db.DATA_DIR.parent / "cache" if (stock_db.DATA_DIR.name == "data") else Path.home() / "goldenstocks-data" / "cache"

#: 每點價值（NT$）與單邊手續費（NT$）——換算成「點」才能跟毛額比較
CONTRACT_SPEC = {
    "TMF": {"point_value": 10.0, "fee_ntd": 15.0, "label": "微型臺指"},
    "MXF": {"point_value": 50.0, "fee_ntd": 15.0, "label": "小型臺指"},
    "TXF": {"point_value": 200.0, "fee_ntd": 15.0, "label": "大型臺指"},
}
#: 交易稅（點/邊）。SSOT = reports/research/channel_lab/tmf_true_cost.json 的
#: tax_pts_per_side，來自 25 筆**真實成交**的券商回報，不是估的。
#: 點數上確實尺度不變：tax_NTD = rate × index × point_value，
#: tax_pts = tax_NTD / point_value = rate × index = 0.00002 × 46,000 ≈ 0.92。
#: 關鍵推論：**這 0.9236 對 TMF/MXF/TXF 完全相同**——換契約能省的只有手續費，
#: 省不掉稅。這正是「小台就會轉正」那個結論最容易算錯的地方。
TAX_PTS_PER_SIDE = 0.9236
#: 限價成交實測滑價（點/邊，負值＝對己方有利）。同一份 tmf_true_cost.json，
#: 25 筆全是 Limit、slip_pts 全 0，平均 −0.4。**只適用限價那一側**——先前
#: 成本模型把它也套到市價出場上，是「成本 1.65」那個數字的來源，也是本腳本
#: 要修正的核心：78% 的出場是市價，付的是半個價差，不是 −0.4 的優惠。
LIMIT_SLIP_PTS_PER_SIDE = -0.40
#: 出場走市價的比例（見 commit a1d50ce：78% 的出場是市價）
MARKET_EXIT_SHARE = 0.78
#: 三段 walk-forward 量到的每筆毛額（掛單距離 ×2.0）
GROSS_PTS_PER_TRADE = 2.86


def _books_dir(root: str) -> Path:
    return CACHE / f"{root.lower()}_books"


def _session_of(ts: datetime) -> str:
    """台指期日盤 08:45~13:45，其餘視為夜盤。"""
    hm = ts.strftime("%H:%M")
    return "day" if "08:45" <= hm <= "13:45" else "night"


def _walk(levels: list[dict], qty: int) -> tuple[float | None, bool]:
    """吃掉 qty 口後的成交均價；深度不足時回 (可成交部分均價, True)。"""
    filled = 0
    cost = 0.0
    for lv in levels:
        p, s = float(lv["price"]), int(lv["size"])
        take = min(s, qty - filled)
        if take <= 0:
            break
        cost += p * take
        filled += take
        if filled >= qty:
            return cost / filled, False
    return (cost / filled if filled else None), True


def measure(root: str, days: list[str] | None, qty: int) -> dict:
    d = _books_dir(root)
    if not d.exists():
        return {"root": root, "error": f"沒有 books 目錄：{d}"}
    files = sorted(d.glob(f"{root.lower()}_books_*.jsonl"))
    if days:
        files = [f for f in files if any(x in f.name for x in days)]
    if not files:
        return {"root": root, "error": "沒有符合條件的 books 檔"}

    # (session) -> list[(duration_sec, buy_slip, sell_slip, spread)]
    samples: dict[str, list[tuple[float, float, float, float]]] = {"day": [], "night": []}
    shallow = {"day": 0, "night": 0}
    total = {"day": 0, "night": 0}
    prev_ts: datetime | None = None
    prev_row: tuple[str, float, float, float] | None = None

    for f in files:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if o.get("event") != "data":
                    continue
                bids, asks = o.get("bids") or [], o.get("asks") or []
                if not bids or not asks:
                    continue
                try:
                    ts = datetime.fromisoformat(str(o["ts"]))
                except (KeyError, ValueError):
                    continue
                b1, a1 = float(bids[0]["price"]), float(asks[0]["price"])
                if a1 <= b1:
                    continue  # 交叉/鎖價，跳過
                mid = (a1 + b1) / 2
                buy_px, short_buy = _walk(asks, qty)
                sell_px, short_sell = _walk(bids, qty)
                if buy_px is None or sell_px is None:
                    continue
                sess = _session_of(ts)
                total[sess] += 1
                if short_buy or short_sell:
                    shallow[sess] += 1
                row = (sess, buy_px - mid, mid - sell_px, a1 - b1)

                # 上一筆快照的持續時間 = 到這一筆為止（時間加權用）
                if prev_ts is not None and prev_row is not None:
                    dur = (ts - prev_ts).total_seconds()
                    if 0 < dur <= 60:  # 超過 60 秒視為斷線缺口，不計入
                        samples[prev_row[0]].append((dur, prev_row[1], prev_row[2], prev_row[3]))
                prev_ts, prev_row = ts, row

    out: dict = {"root": root, "qty": qty, "files": [f.name for f in files], "sessions": {}}
    for sess, rows in samples.items():
        if not rows:
            continue
        w = sum(r[0] for r in rows)
        tw_buy = sum(r[0] * r[1] for r in rows) / w
        tw_sell = sum(r[0] * r[2] for r in rows) / w
        tw_spread = sum(r[0] * r[3] for r in rows) / w
        spreads = [r[3] for r in rows]
        buys = [r[1] for r in rows]
        out["sessions"][sess] = {
            "n_snapshots": total[sess],
            "n_weighted_sec": round(w),
            "tw_spread_pts": round(tw_spread, 4),
            "tw_market_buy_slip_pts": round(tw_buy, 4),
            "tw_market_sell_slip_pts": round(tw_sell, 4),
            "tw_market_exit_slip_pts": round((tw_buy + tw_sell) / 2, 4),
            "spread_median": statistics.median(spreads),
            "spread_p90": sorted(spreads)[int(0.9 * len(spreads))],
            "buy_slip_p90": round(sorted(buys)[int(0.9 * len(buys))], 4),
            "shallow_book_pct": round(100 * shallow[sess] / max(total[sess], 1), 3),
        }
    return out


def cost_model(root: str, market_exit_slip_pts: float,
               entry_slip_pts: float = LIMIT_SLIP_PTS_PER_SIDE) -> dict:
    """每筆來回成本（點）＝ 手續費 + 稅 + 進場滑價 + 混合出場滑價。

    進場永遠是限價（用實測 −0.4）；出場 78% 市價（用本腳本從 book 量到的半價差）、
    22% 限價（−0.4）。
    """
    spec = CONTRACT_SPEC[root]
    fee_pts = spec["fee_ntd"] / spec["point_value"]
    blended_exit = (
        MARKET_EXIT_SHARE * market_exit_slip_pts
        + (1 - MARKET_EXIT_SHARE) * LIMIT_SLIP_PTS_PER_SIDE
    )
    total = 2 * fee_pts + 2 * TAX_PTS_PER_SIDE + entry_slip_pts + blended_exit
    return {
        "label": spec["label"],
        "fee_pts_round_trip": round(2 * fee_pts, 4),
        "tax_pts_round_trip": round(2 * TAX_PTS_PER_SIDE, 4),
        "entry_slip_pts": round(entry_slip_pts, 4),
        "blended_exit_slip_pts": round(blended_exit, 4),
        "round_trip_cost_pts": round(total, 4),
        "gross_pts": GROSS_PTS_PER_TRADE,
        "net_pts_per_trade": round(GROSS_PTS_PER_TRADE - total, 4),
        "net_ntd_per_trade": round((GROSS_PTS_PER_TRADE - total) * spec["point_value"]),
    }


def breakeven_spread_pts(root: str, entry_slip_pts: float = 0.0) -> float:
    """反推：這個契約的價差要「差到多少點」才會讓每筆期望值歸零。

    這個問法比「猜 MXF 的價差是多少」穩健得多——它把結論變成一個可否證的門檻：
    只要實測價差低於這個值就是正的，不需要先知道確切數字。
    """
    spec = CONTRACT_SPEC[root]
    fee_pts = spec["fee_ntd"] / spec["point_value"]
    fixed = (
        2 * fee_pts
        + 2 * TAX_PTS_PER_SIDE
        + entry_slip_pts
        + (1 - MARKET_EXIT_SHARE) * LIMIT_SLIP_PTS_PER_SIDE
    )
    room = GROSS_PTS_PER_TRADE - fixed
    # 市價出場滑價 = 半價差，故 spread = 2 × room / MARKET_EXIT_SHARE
    return 2 * room / MARKET_EXIT_SHARE


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="TMF", help="逗號分隔，例如 TMF,MXF,TXF")
    ap.add_argument("--days", default=None, help="逗號分隔日期，例如 2026-08-18,2026-08-19")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--entry-slip", type=float, default=LIMIT_SLIP_PTS_PER_SIDE,
                    help="進場限價單的滑價假設（點）；預設 0＝完美限價成交")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    days = args.days.split(",") if args.days else None
    results = []
    for root in [r.strip().upper() for r in args.root.split(",")]:
        res = measure(root, days, args.qty)
        results.append(res)
        print("=" * 76)
        print(f"【{root}】{CONTRACT_SPEC.get(root, {}).get('label', '')}  qty={args.qty}")
        if res.get("error"):
            print(f"  ⚠️ {res['error']}")
            continue
        print(f"  檔案：{', '.join(res['files'])}")
        for sess, s in res["sessions"].items():
            print(f"  [{sess}] 快照 {s['n_snapshots']:,} 筆／時間加權 {s['n_weighted_sec']:,}s")
            print(f"      時間加權價差      {s['tw_spread_pts']:.3f} 點"
                  f"（中位 {s['spread_median']:.0f}／P90 {s['spread_p90']:.0f}）")
            print(f"      市價出場滑價      {s['tw_market_exit_slip_pts']:.3f} 點"
                  f"（買 {s['tw_market_buy_slip_pts']:.3f}／賣 {s['tw_market_sell_slip_pts']:.3f}）")
            print(f"      P90 買方滑價      {s['buy_slip_p90']:.3f} 點")
            print(f"      深度不足比例      {s['shallow_book_pct']:.2f}%")
        if root in CONTRACT_SPEC and res["sessions"]:
            print(f"  --- 成本模型（進場滑價假設 {args.entry_slip}）---")
            for sess, s in res["sessions"].items():
                cm = cost_model(root, s["tw_market_exit_slip_pts"], args.entry_slip)
                sign = "✅" if cm["net_pts_per_trade"] > 0 else "❌"
                print(f"      [{sess}] 手續費 {cm['fee_pts_round_trip']:.3f} +"
                      f" 稅 {cm['tax_pts_round_trip']:.3f} +"
                      f" 進場 {cm['entry_slip_pts']:+.3f} +"
                      f" 出場 {cm['blended_exit_slip_pts']:+.3f}"
                      f" = 來回成本 {cm['round_trip_cost_pts']:.3f} 點")
                print(f"             {sign} 毛 {cm['gross_pts']} − 成本 {cm['round_trip_cost_pts']:.3f}"
                      f" = {cm['net_pts_per_trade']:+.3f} 點/筆"
                      f"（{cm['net_ntd_per_trade']:+,} 元/筆）")

    print("=" * 76)
    print("【損益兩平反推】價差要差到多少點，這個契約才會由正轉負")
    print(f"  （毛額 {GROSS_PTS_PER_TRADE} 點/筆 · 出場 {MARKET_EXIT_SHARE:.0%} 走市價 ·"
          f" 進場滑價假設 {args.entry_slip}）")
    measured = {r["root"]: r for r in results if not r.get("error")}
    for root, spec in CONTRACT_SPEC.items():
        be = breakeven_spread_pts(root, args.entry_slip)
        obs = ""
        if root in measured and measured[root]["sessions"]:
            tw = [s["tw_spread_pts"] for s in measured[root]["sessions"].values()]
            avg = sum(tw) / len(tw)
            verdict = "❌ 已超過" if avg >= be else "✅ 仍在門檻內"
            obs = f"　實測價差 {avg:.2f} 點 → {verdict}"
        else:
            obs = "　（尚無實測價差資料）"
        print(f"  {root} {spec['label']}：價差 ≥ {be:.2f} 點才會轉負{obs}")
    print("\n  ⚠️ 這個門檻假設「毛額 2.86 點在換契約後不變」。毛額來自 TMF 的 queue-aware")
    print("     tick 回放，而進場是限價單——換到流動性更好的契約，同樣掛單距離的排隊競爭")
    print("     與成交率會不一樣。契約換過去之後必須重新量毛額，不能沿用。")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({
                "measurements": results,
                "breakeven_spread_pts": {
                    r: round(breakeven_spread_pts(r, args.entry_slip), 4)
                    for r in CONTRACT_SPEC
                },
                "assumptions": {
                    "gross_pts_per_trade": GROSS_PTS_PER_TRADE,
                    "market_exit_share": MARKET_EXIT_SHARE,
                    "tax_pts_per_side": TAX_PTS_PER_SIDE,
                    "entry_slip_pts": args.entry_slip,
                },
            }, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"\n寫出 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
