#!/usr/bin/env python3
"""開盤動能延續效應 vs 籌碼結構（三大法人／融資／借券／分點集中度）的關係.

拿 momentum_continuation_universe_scan.py 算出的67檔動能排名，對照同一段
（IS+OOS共47天）窗口的籌碼資料，看排名跟哪些籌碼特徵相關——不只看頂尖幾檔的
絕對水準，要跟後段比較才知道是不是「共通點」而非巧合。

用法：
  PYTHONPATH=src .venv/bin/python \
    scripts/research/momentum_chip_profile_scan.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db

DAYS = json.loads(Path("/tmp/chip_days.json").read_text())
DAY_LO, DAY_HI = min(DAYS), max(DAYS)

# sid -> mean_ret%（來自前一輪 momentum_continuation_universe_scan.py 輸出）
MOMENTUM_RANK = {
    "3665": 2.356, "2395": 1.964, "3661": 1.882, "6510": 1.650, "2360": 1.293,
    "3653": 1.157, "6139": 1.077, "3017": 1.056, "5371": 0.992, "6278": 0.933,
    "2367": 0.932, "3081": 0.911, "2059": 0.894, "5274": 0.887, "2404": 0.885,
    "6213": 0.884, "2308": 0.838, "3008": 0.835, "2383": 0.796, "2345": 0.796,
    "6285": 0.746, "3264": 0.739, "6274": 0.727, "8150": 0.724, "6223": 0.706,
    "2379": 0.703, "3189": 0.688, "1560": 0.683, "8358": 0.682, "2368": 0.678,
    "5483": 0.659, "2313": 0.655, "6669": 0.600, "8299": 0.588, "2449": 0.571,
    "2301": 0.553, "1802": 0.549, "8046": 0.525, "2357": 0.514, "3037": 0.507,
    "6239": 0.500, "3711": 0.491, "3532": 0.488, "3006": 0.450, "4958": 0.444,
    "8069": 0.424, "3443": 0.384, "3260": 0.379, "2408": 0.350, "3481": 0.321,
    "3231": 0.302, "2382": 0.293, "6147": 0.293, "1303": 0.258, "2327": 0.254,
    "2353": 0.232, "1101": 0.162, "2454": 0.154, "3105": 0.154, "2317": 0.085,
    "2303": 0.066, "2337": 0.037, "2891": 0.001, "2603": -0.001, "6770": -0.074,
    "2344": -0.101, "2330": -0.222,
}


def main() -> int:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    sids = list(MOMENTUM_RANK.keys())
    placeholders = ",".join("?" * len(sids))

    # 三大法人
    inst = {}
    for sid, cp, fn, itn, dn, tn in con.execute(
        f"SELECT stock_id, close_price, foreign_net, investment_trust_net, dealer_self_net, three_institution_net "
        f"FROM stock_institutional_daily WHERE stock_id IN ({placeholders}) AND trade_date BETWEEN ? AND ?",
        (*sids, DAY_LO, DAY_HI),
    ):
        inst.setdefault(sid, []).append((fn, itn, dn, tn))

    # 成交金額（法人參與度分母）
    amt = {}
    for sid, amount in con.execute(
        f"SELECT stock_id, amount FROM stock_daily_bars WHERE stock_id IN ({placeholders}) "
        f"AND trade_date BETWEEN ? AND ? AND source='finmind'",
        (*sids, DAY_LO, DAY_HI),
    ):
        if amount:
            amt.setdefault(sid, []).append(amount)

    # 市值（正規化分母）
    mv = {}
    for sid, market_value in con.execute(
        f"SELECT stock_id, market_value_ntd FROM stock_market_value_daily WHERE stock_id IN ({placeholders}) "
        f"AND trade_date BETWEEN ? AND ?",
        (*sids, DAY_LO, DAY_HI),
    ):
        if market_value:
            mv.setdefault(sid, []).append(market_value)

    # 融資餘額（散戶代理）+融資變化
    margin = {}
    for sid, bal, chg in con.execute(
        f"SELECT stock_id, margin_balance, margin_change FROM stock_margin_daily WHERE stock_id IN ({placeholders}) "
        f"AND trade_date BETWEEN ? AND ?",
        (*sids, DAY_LO, DAY_HI),
    ):
        margin.setdefault(sid, []).append((bal, chg))

    # 借券餘額（放空/法人代理）
    lending = {}
    for sid, bal in con.execute(
        f"SELECT stock_id, lending_balance FROM stock_lending_daily WHERE stock_id IN ({placeholders}) "
        f"AND trade_date BETWEEN ? AND ?",
        (*sids, DAY_LO, DAY_HI),
    ):
        if bal is not None:
            lending.setdefault(sid, []).append(bal)

    # 分點集中度：期間內 top5 分點買賣量佔全部分點買賣量比例
    branch_vol = {}
    for sid, br, buy, sell in con.execute(
        f"SELECT stock_id, securities_trader_id, SUM(buy), SUM(sell) FROM stock_broker_branch_daily "
        f"WHERE stock_id IN ({placeholders}) AND trade_date BETWEEN ? AND ? GROUP BY stock_id, securities_trader_id",
        (*sids, DAY_LO, DAY_HI),
    ):
        branch_vol.setdefault(sid, []).append((buy or 0) + (sell or 0))

    rows = []
    for sid in sids:
        mom = MOMENTUM_RANK[sid]
        avg_amt = np.mean(amt.get(sid, [np.nan]))
        avg_mv = np.mean(mv.get(sid, [np.nan]))

        inst_rows = inst.get(sid, [])
        if inst_rows and avg_amt and not np.isnan(avg_amt):
            fn_sum = sum(r[0] or 0 for r in inst_rows)
            itn_sum = sum(r[1] or 0 for r in inst_rows)
            dn_sum = sum(r[2] or 0 for r in inst_rows)
            tn_sum = sum(r[3] or 0 for r in inst_rows)
            n_days_amt = len(amt.get(sid, []))
            total_amt = sum(amt.get(sid, []))
            foreign_net_pct = fn_sum / total_amt * 100 if total_amt else np.nan
            it_net_pct = itn_sum / total_amt * 100 if total_amt else np.nan
            dealer_net_pct = dn_sum / total_amt * 100 if total_amt else np.nan
            three_net_pct = tn_sum / total_amt * 100 if total_amt else np.nan
        else:
            foreign_net_pct = it_net_pct = dealer_net_pct = three_net_pct = np.nan

        margin_rows = margin.get(sid, [])
        avg_margin_bal = np.mean([r[0] for r in margin_rows if r[0] is not None]) if margin_rows else np.nan
        margin_to_mv = avg_margin_bal * 1000 / avg_mv * 100 if avg_mv and not np.isnan(avg_margin_bal) else np.nan
        # margin_balance 單位是「張」，*1000股/張*股價概估用市值/流通量太複雜，這裡先用「金額」近似：
        # 改用市值直接比例做量級對照即可，不追求精確持股比例

        lending_rows = lending.get(sid, [])
        avg_lending_bal = np.mean(lending_rows) if lending_rows else np.nan

        bvols = branch_vol.get(sid, [])
        if bvols:
            bvols_sorted = sorted(bvols, reverse=True)
            total_b = sum(bvols_sorted)
            top5_share = sum(bvols_sorted[:5]) / total_b * 100 if total_b else np.nan
            n_branches = len(bvols_sorted)
        else:
            top5_share = np.nan
            n_branches = 0

        rows.append(
            {
                "sid": sid, "mom": mom, "avg_mv_億": avg_mv / 1e8 if avg_mv else np.nan,
                "foreign_net_pct": foreign_net_pct, "it_net_pct": it_net_pct,
                "dealer_net_pct": dealer_net_pct, "three_net_pct": three_net_pct,
                "avg_margin_bal_張": avg_margin_bal, "avg_lending_bal": avg_lending_bal,
                "top5_branch_share_pct": top5_share, "n_branches": n_branches,
            }
        )

    print(f"{'sid':>6} {'mom%':>7} {'市值億':>8} {'外資淨%':>8} {'投信淨%':>8} {'自營淨%':>8} {'三大法人淨%':>10} {'借券餘額':>10} {'top5分點%':>9} {'分點數':>6}")
    for r in sorted(rows, key=lambda r: r["mom"], reverse=True):
        print(
            f"{r['sid']:>6} {r['mom']:>7.3f} {r['avg_mv_億']:>8.0f} {r['foreign_net_pct']:>8.3f} "
            f"{r['it_net_pct']:>8.3f} {r['dealer_net_pct']:>8.3f} {r['three_net_pct']:>10.3f} "
            f"{r['avg_lending_bal']:>10.0f} {r['top5_branch_share_pct']:>9.1f} {r['n_branches']:>6}"
        )

    # correlations
    def corr(key):
        xs, ys = [], []
        for r in rows:
            v = r[key]
            if v is not None and not (isinstance(v, float) and np.isnan(v)):
                xs.append(v)
                ys.append(r["mom"])
        if len(xs) < 5:
            return None, 0
        return float(np.corrcoef(xs, ys)[0, 1]), len(xs)

    print("\n=== 相關係數（vs 動能效應 mean%）===")
    for key, label in [
        ("avg_mv_億", "市值"),
        ("foreign_net_pct", "外資淨買/成交額%"),
        ("it_net_pct", "投信淨買/成交額%"),
        ("dealer_net_pct", "自營淨買/成交額%"),
        ("three_net_pct", "三大法人合計淨買/成交額%"),
        ("avg_lending_bal", "借券餘額"),
        ("top5_branch_share_pct", "top5分點集中度%"),
        ("n_branches", "參與分點數"),
    ]:
        c, n = corr(key)
        print(f"  {label:24s}: corr={c if c is None else f'{c:.3f}':>7} (n={n})")

    # top15 vs bottom15
    ranked = sorted(rows, key=lambda r: r["mom"], reverse=True)
    top15, bot15 = ranked[:15], ranked[-15:]

    def group_mean(group, key):
        vals = [r[key] for r in group if r[key] is not None and not (isinstance(r[key], float) and np.isnan(r[key]))]
        return np.mean(vals) if vals else float("nan")

    print("\n=== Top15 vs Bottom15 平均值對照 ===")
    for key, label in [
        ("avg_mv_億", "市值(億)"),
        ("foreign_net_pct", "外資淨買/成交額%"),
        ("it_net_pct", "投信淨買/成交額%"),
        ("dealer_net_pct", "自營淨買/成交額%"),
        ("three_net_pct", "三大法人合計淨買/成交額%"),
        ("avg_lending_bal", "借券餘額"),
        ("top5_branch_share_pct", "top5分點集中度%"),
        ("n_branches", "參與分點數"),
    ]:
        print(f"  {label:24s}: top15={group_mean(top15,key):>10.2f}  bottom15={group_mean(bot15,key):>10.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
