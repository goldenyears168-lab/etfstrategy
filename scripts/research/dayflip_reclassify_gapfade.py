#!/usr/bin/env python3
"""Step2：把 Step1（per_event_flip.json，全分點大買事件）跟期貨跳空/回測疊起來.

對每個大買事件 (tid, sid, d0)，若 sid 屬期貨可空宇宙：
  - fgap = T+1 08:45 期貨開盤 / T0 期貨收盤 - 1（跟 production 同口徑）
  - fgap >= 6% 才算「候選跳空空單」
  - 用日線 OHLC 近似出場（無 tick 資料）：T+1 最低價若觸及 進場*(1-2%) 視為
    觸價回補 (+2%-cost)；否則收盤平倉。這是 daily-bar 近似（非 tick 級），
    只用於篩選候選，不是最終定案用的精確回測（見 checklist 附錄 A12）。
  - ADV20（期貨20日均量）>=800 口 liquidity gate，同 production。

輸出每個分點的「真的會觸發空單」交易清單與彙總統計，供人工篩選 + permutation 檢定。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

OUT = Path("/Users/jackm4/goldenstocks/reports/research/branch_dayflip_reclassify")
FGAP_MIN = 0.06
TARGET = 0.02
COST = 0.0005  # 5bps round-trip, matches FROZEN_SPEC
ADV_MIN_LOTS = 800.0

known24 = {
    "920M", "9227", "7008", "5851", "989X", "989g", "9217", "981j", "913R",
    "9661", "779Z", "918e", "980h", "585Y", "920F", "9875", "9A9R", "918X",
    "5383", "9216", "9325", "1360", "9A81", "779n",
}
known2 = {"9217", "9801"}


def main() -> None:
    events = json.load((OUT / "per_event_flip.json").open())
    panel = json.load((OUT / "futures_panel.json").open())
    print(f"events={len(events)} panel_stocks={len(panel)}")

    by_stock_dates = {sid: sorted(m) for sid, m in panel.items()}
    di = {sid: {d: i for i, d in enumerate(ds)} for sid, ds in by_stock_dates.items()}

    trades = []
    skipped_no_panel = 0
    skipped_no_gap_data = 0
    for ev in events:
        sid = ev["sid"]
        m = panel.get(sid)
        if not m:
            skipped_no_panel += 1
            continue
        ds = by_stock_dates[sid]
        idx = di[sid]
        i0 = idx.get(ev["d0"])
        if i0 is None or i0 + 1 >= len(ds):
            skipped_no_gap_data += 1
            continue
        d1 = ds[i0 + 1]
        o0, c0 = m[ev["d0"]][0], m[ev["d0"]][1]
        o1, c1, mn1, mx1, vol1 = m[d1]
        if c0 <= 0 or o1 <= 0:
            continue
        fgap = o1 / c0 - 1
        if fgap < FGAP_MIN:
            continue
        if i0 < 20:
            continue
        adv = mean([m[ds[j]][4] for j in range(i0 - 20, i0)])
        if adv < ADV_MIN_LOTS:
            continue
        entry = o1
        tp_price = entry * (1 - TARGET)
        if mn1 > 0 and mn1 <= tp_price:
            pnl = TARGET - COST
            how = "tp_hit_daily_low"
        else:
            pnl = -(c1 / entry - 1) - COST
            how = "close"
        trades.append({
            "tid": ev["tid"], "sid": sid, "d0": ev["d0"], "d1": d1,
            "fgap_pct": round(100 * fgap, 2), "entry": entry,
            "pnl_pct": round(100 * pnl, 3), "how": how,
            "amt_ntd": ev["amt_ntd"], "flip_10d": ev.get("flip_10d"),
        })

    print(f"skipped_no_panel={skipped_no_panel} skipped_no_gap_calendar_data={skipped_no_gap_data}")
    print(f"qualifying gap-short trades (fgap>=6%, ADV ok): {len(trades)}")

    (OUT / "gapfade_trades_all.json").write_text(json.dumps(trades, ensure_ascii=False, indent=1))

    by_tid = defaultdict(list)
    for t in trades:
        by_tid[t["tid"]].append(t)

    summary = []
    for tid, ts in by_tid.items():
        pnls = [t["pnl_pct"] for t in ts]
        summary.append({
            "tid": tid,
            "n_trades": len(ts),
            "mean_pnl_pct": round(mean(pnls), 3),
            "median_pnl_pct": round(median(pnls), 3),
            "win_rate_pct": round(100 * mean(p > 0 for p in pnls), 1),
            "is_known24": tid in known24,
            "is_known2_live": tid in known2,
            "stocks": sorted({t["sid"] for t in ts}),
        })
    summary.sort(key=lambda r: (-r["n_trades"]))
    (OUT / "gapfade_summary_by_branch.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1)
    )
    print(f"branches with >=1 qualifying trade: {len(summary)}")
    print(f"branches with >=5 qualifying trades: {sum(1 for r in summary if r['n_trades']>=5)}")
    print(f"branches with >=8 qualifying trades: {sum(1 for r in summary if r['n_trades']>=8)}")

    print("\n=== known24 seats in this gap-fade universe ===")
    for r in sorted(summary, key=lambda r: -r["n_trades"]):
        if r["is_known24"]:
            print(r["tid"], r["n_trades"], r["mean_pnl_pct"], r["median_pnl_pct"], r["win_rate_pct"])

    print("\n=== top 40 non-known24 branches by n_trades ===")
    for r in [r for r in summary if not r["is_known24"]][:40]:
        print(r["tid"], r["n_trades"], r["mean_pnl_pct"], r["median_pnl_pct"], r["win_rate_pct"], r["stocks"][:5])


def unconditional_baseline() -> None:
    """對照組（沒有任何分點條件）：227 檔期貨標的裡，所有 fgap>=6% 的日子
    （不管前一天有沒有分點大買）套用同一套 proxy 出場邏輯，看純跳空本身
    是不是就有這個「日中位1.95%/勝率70%+」的表現——用來檢驗 Step2 的
    branch-conditioned 結果是不是被分點身份解釋，還是只是跳空門檻本身的效果。
    """
    panel = json.load((OUT / "futures_panel.json").open())
    all_trades = []
    for sid, m in panel.items():
        ds = sorted(m)
        for i in range(20, len(ds) - 1):
            d0, d1 = ds[i], ds[i + 1]
            o0, c0 = m[d0][0], m[d0][1]
            o1, c1, mn1, mx1, vol1 = m[d1]
            if c0 <= 0 or o1 <= 0:
                continue
            fgap = o1 / c0 - 1
            if fgap < FGAP_MIN:
                continue
            adv = mean([m[ds[j]][4] for j in range(i - 20, i)])
            if adv < ADV_MIN_LOTS:
                continue
            entry = o1
            tp_price = entry * (1 - TARGET)
            if mn1 > 0 and mn1 <= tp_price:
                pnl = TARGET - COST
            else:
                pnl = -(c1 / entry - 1) - COST
            all_trades.append({"sid": sid, "d0": d0, "d1": d1, "pnl_pct": round(100 * pnl, 3)})
    pnls = [t["pnl_pct"] for t in all_trades]
    print(f"\n=== UNCONDITIONAL baseline (no branch condition at all): "
          f"n={len(pnls)} mean={mean(pnls):.3f} median={median(pnls):.3f} "
          f"win_rate={100*mean(p>0 for p in pnls):.1f}% ===")
    (OUT / "gapfade_unconditional_baseline.json").write_text(
        json.dumps(all_trades, ensure_ascii=False, indent=1)
    )


if __name__ == "__main__":
    main()
    unconditional_baseline()
