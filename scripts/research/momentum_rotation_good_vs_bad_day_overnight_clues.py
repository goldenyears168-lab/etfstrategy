"""momentum-rotation：好日子 vs 差日子，跟前一夜（VIXTWN收盤/US期貨隔夜%）有沒有關係.

2026-08-13 研究：重跑 momentum_breakout_strategy.simulate_portfolio_day 逐日
拆解（不是只看聚合統計），跟兩個「開盤前就拿得到」的信號做PIT-correct對照：
  1. VIXTWN 前一交易日收盤（market_vix_daily）
  2. US期貨隔夜% ES/NQ（us_futures_overnight_snapshot，capture_label='09:30'
     ——這是DB裡最早的snapshot，不是精確的05:00，誠實記錄這個落差）

只做觀察性分析，不回填任何交易邏輯、不影響今天已經在跑的live worker。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import numpy as np
import stock_db
from momentum_breakout_strategy import UNIVERSE, load_day_bars_with_times, simulate_portfolio_day

TICK_DIR = Path(__file__).resolve().parents[2] / "reports/research/expert_pool_futures_tick"


def load_all_days() -> dict[str, dict[str, tuple]]:
    by_stock: dict[str, dict[str, tuple]] = {}
    for sid in UNIVERSE:
        days: dict[str, tuple] = {}
        matches = sorted(TICK_DIR.glob(f"*{sid}_*_tick_*.csv"))
        for path in matches:
            days.update(load_day_bars_with_times(path))
        if days:
            by_stock[sid] = days
    return by_stock


def main() -> int:
    by_stock = load_all_days()
    all_days = sorted(set().union(*[set(d.keys()) for d in by_stock.values()]))
    print(f"載入 {len(by_stock)} 檔、{len(all_days)} 個交易日", file=sys.stderr)

    rows = []
    for d in all_days:
        day_data = {sid: days[d] for sid, days in by_stock.items() if d in days}
        if len(day_data) < 3:  # 當天有效標的太少，跳過（資料不全，不是策略問題）
            continue
        trades = simulate_portfolio_day(day_data)
        if not trades:
            rows.append({"date": d, "day_ret": 0.0, "n_trades": 0, "win_rate": None, "n_stocks": len(day_data)})
            continue
        rets = [t["ret_pct"] for t in trades]
        win = sum(1 for r in rets if r > 0) / len(rets) * 100
        rows.append({
            "date": d, "day_ret": sum(rets), "n_trades": len(rets),
            "win_rate": win, "n_stocks": len(day_data),
        })

    conn = stock_db.connect_ro()
    cur = conn.cursor()
    cur.execute("SELECT date, close FROM market_vix_daily WHERE symbol='VIXTWN' ORDER BY date")
    vixtwn = {r[0]: r[1] for r in cur.fetchall()}
    vix_dates = sorted(vixtwn)

    cur.execute(
        "SELECT tw_session_date, es_overnight_pct, nq_overnight_pct FROM us_futures_overnight_snapshot "
        "WHERE capture_label='09:30'"
    )
    overnight = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    def prior_vixtwn(date: str) -> float | None:
        idx = None
        for i, vd in enumerate(vix_dates):
            if vd >= date:
                idx = i
                break
        if idx is None or idx == 0:
            return None
        return vixtwn[vix_dates[idx - 1]]

    for row in rows:
        row["vixtwn_prev_close"] = prior_vixtwn(row["date"])
        es, nq = overnight.get(row["date"], (None, None))
        row["es_overnight_pct"] = es
        row["nq_overnight_pct"] = nq

    complete = [r for r in rows if r["vixtwn_prev_close"] is not None]
    print(f"\n總交易日 n={len(rows)}，有VIXTWN配對 n={len(complete)}", file=sys.stderr)

    complete.sort(key=lambda r: r["day_ret"])
    n = len(complete)
    tercile = max(1, n // 3)
    bad = complete[:tercile]
    good = complete[-tercile:]

    def _mean(vals):
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None

    print("\n=== 全部交易日（按day_ret排序） ===")
    print(f"{'date':<12}{'day_ret%':>10}{'n_trades':>10}{'win%':>8}{'VIXTWN(T-1)':>14}{'ES_on%':>10}{'NQ_on%':>10}")
    for r in complete:
        print(
            f"{r['date']:<12}{r['day_ret']:>10.2f}{r['n_trades']:>10}"
            f"{(r['win_rate'] if r['win_rate'] is not None else float('nan')):>8.1f}"
            f"{r['vixtwn_prev_close']:>14.2f}"
            f"{(r['es_overnight_pct'] if r['es_overnight_pct'] is not None else float('nan')):>10.3f}"
            f"{(r['nq_overnight_pct'] if r['nq_overnight_pct'] is not None else float('nan')):>10.3f}"
        )

    print(f"\n=== 差日子（後1/3, n={len(bad)}） vs 好日子（前1/3, n={len(good)}） ===")
    print(f"day_ret 均值：差={_mean([r['day_ret'] for r in bad]):.2f}%  好={_mean([r['day_ret'] for r in good]):.2f}%")
    print(f"VIXTWN(T-1) 均值：差={_mean([r['vixtwn_prev_close'] for r in bad]):.2f}  好={_mean([r['vixtwn_prev_close'] for r in good]):.2f}")
    es_bad = _mean([r["es_overnight_pct"] for r in bad])
    es_good = _mean([r["es_overnight_pct"] for r in good])
    nq_bad = _mean([r["nq_overnight_pct"] for r in bad])
    nq_good = _mean([r["nq_overnight_pct"] for r in good])
    print(f"ES隔夜% 均值：差={es_bad if es_bad is None else round(es_bad,3)}  好={es_good if es_good is None else round(es_good,3)}")
    print(f"NQ隔夜% 均值：差={nq_bad if nq_bad is None else round(nq_bad,3)}  好={nq_good if nq_good is None else round(nq_good,3)}")

    # 簡單相關係數（day_ret vs 各信號），n小僅供參考不是正式顯著性檢定
    def _corr(xs, ys):
        pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
        if len(pairs) < 5:
            return None
        xa = np.array([p[0] for p in pairs])
        ya = np.array([p[1] for p in pairs])
        if xa.std() == 0 or ya.std() == 0:
            return None
        return float(np.corrcoef(xa, ya)[0, 1])

    day_rets = [r["day_ret"] for r in complete]
    print("\n=== Pearson相關係數（n小，僅供觀察方向，不是正式檢定）===")
    print("day_ret vs VIXTWN(T-1):", _corr(day_rets, [r["vixtwn_prev_close"] for r in complete]))
    print("day_ret vs ES隔夜%:", _corr(day_rets, [r["es_overnight_pct"] for r in complete]))
    print("day_ret vs NQ隔夜%:", _corr(day_rets, [r["nq_overnight_pct"] for r in complete]))
    print("day_ret vs n_trades:", _corr(day_rets, [r["n_trades"] for r in complete]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
