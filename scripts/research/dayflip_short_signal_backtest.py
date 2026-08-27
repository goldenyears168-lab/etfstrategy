#!/usr/bin/env python3
"""dayflip-short 合格訊號的 PIT 回測 —— 回答「這 82 筆的期望值是多少」。

背景：sleeve 2026-08-07 上線至今 0 筆成交，所以**期望報酬這個數字目前不存在**。
漏斗稽核（dayflip_short_signal_funnel_audit.py）已經產出歷史上會觸發的合格訊號，
本腳本把它們照 FROZEN_SPEC_V1 的執行規則回測一次。

執行規則（一字不改照 spec，不自己發明）
--------------------------------------
    entry         T+1 08:45 期貨開盤價放空
    exit_primary  同步掛限價買單於 進場價 × 0.98（−2%）
    exit_fallback 未觸價則 13:40 強制平倉（市價）
    stop_loss     無（spec：價格停損與時間停損實測皆有害）
    cost          5 bps 來回
    單日單筆      pick_signal 規則：0.75×fgap升冪rank + 0.25×席數降冪rank 取最小

三個必須講清楚的限制（不講就是 BUG-4 的雙重標準）
------------------------------------------------
1. **樣本幾乎全是 in-sample**。spec 的 IS 窗是 2024-07-01~2026-07-08，而 82 筆裡
   有 ~77 筆落在裡面。IS 段的結果**不是證據**，只能當作「我的管線有沒有重現 spec
   宣稱的 80.6% 當日勝率」的驗證。真 holdout（2026-07-09 後）只有個位數筆，
   檢定力不足以區分「有 edge」與「沒 edge」。

2. **觸價＝成交是樂觀假設**。用日線最低價判斷 −2% 限價單有沒有成交，等於假設
   「價格碰到就一定排得到隊」。CCF 秒級研究已經證實那是錯的（82% 排不到隊）。
   所以本腳本一律同時輸出**悲觀版**（假設限價單永遠不成交、全部抱到收盤），
   兩個數字夾出真實值的區間。只報樂觀版就是 BUG-5 的 fill clamp。

3. **收盤價是 13:40 的代理**。spec 的 fallback 是 13:40 市價單，日線只有 13:45
   收盤價。兩者不同，但沒有更好的免費資料。

用法：
    PYTHONPATH=src .venv/bin/python scripts/research/dayflip_short_signal_backtest.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import stock_db  # noqa: E402
from order.dayflip_short_order import COVER_TARGET_PCT, GAP_RANK_WEIGHT  # noqa: E402

BASE = (
    stock_db.PROJECT_ROOT
    / "reports/research/branch-footprint-screen/dayflip_gapup_short"
)
#: futures_daily_cache.json 的欄位順序（見 refresh_dayflip_futures_daily_cache.py:102）
IDX_OPEN, IDX_CLOSE, IDX_HIGH, IDX_LOW, IDX_VOL = 0, 1, 2, 3, 4
#: spec.execution.cost_bps_round_trip
COST_BPS_ROUND_TRIP = 5.0
#: spec.in_sample_window[1] —— 之後才是真 holdout
IS_END = "2026-07-08"
#: spec.forward_test_protocol.pass_criteria.day_win_rate_pct
PASS_DAY_WIN_PCT = 70.0


def pick_one_per_day(picks: list[dict]) -> list[dict]:
    """重現 live 的單日單筆選股（pick_signal）。"""
    by_day: dict[str, list[dict]] = defaultdict(list)
    for p in picks:
        by_day[p["t0"]].append(p)
    out = []
    for d, ps in sorted(by_day.items()):
        n = len(ps)
        gap_order = sorted(range(n), key=lambda i: ps[i]["fgap"])
        gap_rank = {i: r + 1 for r, i in enumerate(gap_order)}
        seat_order = sorted(range(n), key=lambda i: -ps[i].get("n_seats", 0))
        seat_rank = {i: r + 1 for r, i in enumerate(seat_order)}
        best = min(range(n), key=lambda i: GAP_RANK_WEIGHT * gap_rank[i]
                   + (1 - GAP_RANK_WEIGHT) * seat_rank[i])
        chosen = dict(ps[best])
        chosen["n_candidates_that_day"] = n
        out.append(chosen)
    return out


def simulate(trades: list[dict], fut: dict, *, optimistic: bool) -> list[dict]:
    """optimistic=True：日內最低價 ≤ 目標即視為 −2% 限價成交。
    optimistic=False：假設限價單永遠排不到，全部抱到收盤。"""
    out = []
    for t in trades:
        row = (fut.get(t["stock_id"]) or {}).get(t["t1"])
        if not row:
            continue
        f_open, f_close, f_high, f_low = (
            row[IDX_OPEN], row[IDX_CLOSE], row[IDX_HIGH], row[IDX_LOW]
        )
        if not f_open or f_open <= 0:
            continue
        entry = float(f_open)
        target = round(entry * (1 - COVER_TARGET_PCT), 1)
        hit = optimistic and float(f_low) <= target
        exit_px = target if hit else float(f_close)
        gross = (entry - exit_px) / entry           # 放空：跌才賺
        net = gross - COST_BPS_ROUND_TRIP / 10_000
        out.append({
            **t,
            "entry": entry, "exit": exit_px, "tp_hit": hit,
            "gross_pct": round(100 * gross, 4),
            "net_pct": round(100 * net, 4),
            "is_oos": t["t0"] > IS_END,
        })
    return out


def describe(rows: list[dict], label: str) -> dict:
    if not rows:
        print(f"\n【{label}】無交易")
        return {}
    net = [r["net_pct"] for r in rows]
    wins = sum(1 for x in net if x > 0)
    tp = sum(1 for r in rows if r["tp_hit"])
    stats = {
        "label": label,
        "n": len(rows),
        "day_win_rate_pct": round(100 * wins / len(rows), 1),
        "mean_net_pct": round(statistics.mean(net), 4),
        "median_net_pct": round(statistics.median(net), 4),
        "tp_hit_rate_pct": round(100 * tp / len(rows), 1),
        "total_net_pct": round(sum(net), 3),
        "worst_pct": round(min(net), 3),
        "best_pct": round(max(net), 3),
    }
    if len(net) > 1:
        sd = statistics.stdev(net)
        stats["stdev_pct"] = round(sd, 4)
        se = sd / (len(net) ** 0.5)
        stats["t_stat"] = round(statistics.mean(net) / se, 3) if se else None
    print(f"\n【{label}】n={stats['n']}")
    print(f"  當日勝率      {stats['day_win_rate_pct']:.1f}%"
          f"（spec 通過門檻 ≥{PASS_DAY_WIN_PCT:.0f}%）")
    print(f"  觸價成交率    {stats['tp_hit_rate_pct']:.1f}%")
    print(f"  平均淨報酬    {stats['mean_net_pct']:+.3f}%   中位 {stats['median_net_pct']:+.3f}%")
    print(f"  累計          {stats['total_net_pct']:+.2f}%"
          f"   最差 {stats['worst_pct']:+.2f}%  最佳 {stats['best_pct']:+.2f}%")
    if stats.get("t_stat") is not None:
        print(f"  t 統計量      {stats['t_stat']:+.2f}"
              f"（|t|<2 代表現有樣本無法區分有無 edge）")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--picks", default=str(BASE / "funnel_audit_20260819.json"))
    ap.add_argument("--json-out", default=str(BASE / "signal_backtest_20260819.json"))
    args = ap.parse_args()

    picks = json.loads(Path(args.picks).read_text())["picks"]
    fut = json.loads((BASE / "futures_daily_cache.json").read_text())
    trades = pick_one_per_day(picks)
    print(f"合格訊號 {len(picks)} 筆 → 依 pick_signal 單日選一筆後 {len(trades)} 個交易日")

    result = {"assumptions": {
        "cost_bps_round_trip": COST_BPS_ROUND_TRIP,
        "cover_target_pct": COVER_TARGET_PCT,
        "is_window_end": IS_END,
        "gap_rank_weight": GAP_RANK_WEIGHT,
    }}

    for optimistic in (True, False):
        tag = "樂觀（觸價即成交）" if optimistic else "悲觀（限價永不成交·全抱到收盤）"
        rows = simulate(trades, fut, optimistic=optimistic)
        print("\n" + "=" * 72)
        print(f"■ {tag}　可回測 {len(rows)}/{len(trades)} 個交易日")
        print("=" * 72)
        key = "optimistic" if optimistic else "pessimistic"
        result[key] = {
            "all": describe(rows, "全樣本（幾乎全是 in-sample · 非證據）"),
            "in_sample": describe([r for r in rows if not r["is_oos"]],
                                  f"in-sample（≤{IS_END}）— 用來驗證管線是否重現 spec"),
            "holdout": describe([r for r in rows if r["is_oos"]],
                                f"holdout（>{IS_END}）— 唯一有證據力的區段"),
        }
        if optimistic:
            result["trades"] = rows

    print("\n" + "=" * 72)
    print("■ 解讀")
    print("=" * 72)
    is_opt = result["optimistic"]["in_sample"]
    if is_opt:
        print(f"  spec 宣稱 in-sample 當日勝率 80.6%；本管線樂觀版重現 "
              f"{is_opt['day_win_rate_pct']:.1f}%")
        gap = abs(is_opt["day_win_rate_pct"] - 80.6)
        print("  → 差距 %.1fpp：%s" % (
            gap,
            "管線與 spec 一致，漏斗可信" if gap <= 8
            else "⚠️ 差距顯著，代表我的漏斗與當初產生 spec 的程式不是同一套，"
                 "在釐清之前不要用本回測的任何數字下決定"))
    ho = result["optimistic"]["holdout"]
    if ho:
        print(f"  holdout 只有 n={ho['n']}；即使數字好看也**不能**當成 edge 的證據——"
              "樣本量在這個離散度下無法區分真實 edge 與雜訊。")
    print("  樂觀↔悲觀兩版的差距 = 「限價單排不排得到隊」這個未知數的價值區間。")

    Path(args.json_out).write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n寫出 {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
