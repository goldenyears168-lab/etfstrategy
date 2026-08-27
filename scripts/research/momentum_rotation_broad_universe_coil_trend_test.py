"""2026-08-13：用剛累積的TAIFEX全市場個股期貨資料（326檔，~22個交易日）驗證
micro VCP（量縮盤整+趨勢方向一致+爆量）——12檔/75天版本一路加條件就撞上樣本
量瓶頸（4折留一窗口交叉驗證常有3折崩潰到個位數~十幾筆）。這裡改用**按股票
切分**的train/holdout（不是按時間窗口切）：326檔（過濾掉tick太稀疏的）
隨機但可重現地分成兩組，用train組股票統計最佳coil+趨勢參數，套到完全沒看過
的holdout組股票上驗證——這樣測的是「訊號能不能跨股票類化」，同時繞開歷史
天數不夠的限制（用股票數量的廣度換時間深度）。

先用訊號層級測試（不套完整單槽位輪動+搶佔，避免326檔互搶變成極端競爭、
也避免完整state machine在326檔上跑太慢）：對每個訊號，量測 continuation_sec
秒後方向對不對，命中率>50%代表訊號本身有正確方向的資訊量。

跟今天稍早12檔版本用同一組訊號定義：5秒(或1秒)滾動視窗爆量 + coil量縮盤整
前提 + N分鐘趨勢方向一致。
"""

from __future__ import annotations

import csv
import os
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, "scripts/research")

_DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or os.path.expanduser("~/goldenstocks-data"))
ARCHIVE_DIR = _DATA_DIR / "cache" / "momentum_rotation" / "taifex_tick_daily_broad"

MIN_TOTAL_ROWS = 1500  # 過濾掉太不流動、拿來驗證coil沒有意義的標的
MAX_TOTAL_ROWS = 80_000  # 排除極端量大的標的(見下)，避免單一標的拖慢整體sweep
# TMF=微型台指期貨(大盤指數期貨，447萬行、佔全部資料44%)——跟regex pattern
# 湊巧撞在一起被收進來，概念上不屬於「個股」，且量體大到單獨會拖慢整個
# sweep(2026-08-13實測光算一個baseline點就卡了90分鐘CPU還沒算完)，明確排除。
EXCLUDE_CODES = {"TMF"}
WINDOW_SEC = 1.0
MOVE_THRESH_PCT = 0.15
VOL_MULT = 2.5
CONTINUATION_SEC = 8.0
MIN_TICKS_IN_COIL = 5
CONTRACTION_RATIO_GRID = [0.5, 0.7]
MIN_COIL_SEC_GRID = [3.0, 10.0]
TREND_LOOKBACK_MIN = 3.0
RANDOM_SEED = 42


def load_broad_universe() -> dict[str, dict[str, tuple[list, list, list]]]:
    universe: dict[str, dict[str, tuple[list, list, list]]] = {}
    for csv_path in sorted(ARCHIVE_DIR.glob("*.csv")):
        code = csv_path.stem
        if code in EXCLUDE_CODES:
            continue
        by_day: dict[str, tuple[list, list, list]] = defaultdict(lambda: ([], [], []))
        total = 0
        with csv_path.open() as f:
            for row in csv.DictReader(f):
                date_str = row.get("date", "")[:10]
                if not date_str:
                    continue
                # 2026-08-14發現：同一個商品代號(如RAF/LCF)底下混著兩種截然
                # 不同的成交類型——單一到期月(outright，contract_date="202608")
                # 跟跨月價差組合(calendar spread，contract_date="202608/202609"，
                # 用"/"分隔)。價差合約的"價格"是兩個月份的價差，常常接近0甚至
                # 負值(例如-.01、-.04)，混進個股期貨的價格序列會嚴重污染統計
                # (甚至讓fill=0觸發除以0的例外)。這裡只收outright列。
                if "/" in row.get("contract_date", ""):
                    continue
                try:
                    price = float(row["price"])
                    volume = float(row["volume"])
                except (KeyError, ValueError, TypeError):
                    continue
                if price <= 0:
                    continue
                times, prices, volumes = by_day[date_str]
                times.append(row["date"])
                prices.append(price)
                volumes.append(volume)
                total += 1
                if total > MAX_TOTAL_ROWS:
                    break  # 提早停止讀取，避免極端量大的標的拖慢整體
        if total < MIN_TOTAL_ROWS or total > MAX_TOTAL_ROWS:
            continue
        # 確保每天的資料按時間排序（TAIFEX檔案本身應該已排序，這裡保險）
        # prices/volumes轉np.array比照momentum_breakout_strategy.
        # load_day_bars_with_times同一個回傳慣例(baseline_simulate內部用
        # prices.size，純list沒有這個屬性)。
        sorted_by_day = {}
        for d, (t, p, v) in by_day.items():
            order = sorted(range(len(t)), key=lambda i: t[i])
            sorted_by_day[d] = (
                [t[i] for i in order],
                np.array([p[i] for i in order], dtype=float),
                np.array([v[i] for i in order], dtype=float),
            )
        universe[code] = sorted_by_day
    return universe


def coil_trend_hit_rate(
    days: dict[str, tuple[list, list, list]], *,
    contraction_ratio: float, min_coil_sec: float, require_trend: bool,
    trend_lookback_min: float = TREND_LOOKBACK_MIN,
    vol_mult: float = VOL_MULT, move_thresh_pct: float = MOVE_THRESH_PCT,
) -> tuple[float, int]:
    """回傳(命中率, 訊號數)——訊號=爆量+coil(+趨勢)成立，量測continuation_sec秒後
    方向是否正確，不含部位管理。"""
    hits: list[int] = []
    coil_buf_span = min_coil_sec * 2.0
    trend_buf_span = trend_lookback_min * 60.0

    for _d, (times, prices, volumes) in days.items():
        dts = [datetime.fromisoformat(t) for t in times]
        n = len(dts)
        buf: list[tuple] = []
        coil_buf: list[tuple] = []
        trend_buf: list[tuple] = []
        vol_hist: list[float] = []
        last_signal: datetime | None = None
        win_td = timedelta(seconds=WINDOW_SEC)
        coil_td = timedelta(seconds=coil_buf_span)
        trend_td = timedelta(seconds=trend_buf_span)
        cool_td = timedelta(seconds=10.0)

        for k in range(n):
            t, p, v = dts[k], float(prices[k]), float(volumes[k])
            buf.append((t, p, v))
            while buf and (t - buf[0][0]) > win_td:
                buf.pop(0)
            coil_buf.append((t, p, v))
            while coil_buf and (t - coil_buf[0][0]) > coil_td:
                coil_buf.pop(0)
            trend_buf.append((t, p, v))
            while trend_buf and (t - trend_buf[0][0]) > trend_td:
                trend_buf.pop(0)

            window_vol = sum(r[2] for r in buf)
            if len(buf) < 2:
                vol_hist.append(window_vol)
                continue
            baseline = max(np.median(vol_hist), 1e-9) if vol_hist else 1.0
            vol_hist.append(window_vol)

            if last_signal is not None and (t - last_signal) < cool_td:
                continue
            oldest_p = buf[0][1]
            if oldest_p <= 0:
                continue
            move_pct = (p - oldest_p) / oldest_p * 100.0
            vol_burst = window_vol / baseline
            if abs(move_pct) < move_thresh_pct or vol_burst < vol_mult:
                continue

            quiet_start = t - timedelta(seconds=min_coil_sec)
            ref_start = t - timedelta(seconds=min_coil_sec * 2.0)
            recent = [r for r in coil_buf if r[0] >= quiet_start]
            reference = [r for r in coil_buf if ref_start <= r[0] < quiet_start]
            if len(recent) < MIN_TICKS_IN_COIL or len(reference) < MIN_TICKS_IN_COIL:
                continue
            recent_range = max(r[1] for r in recent) - min(r[1] for r in recent)
            ref_range = max(r[1] for r in reference) - min(r[1] for r in reference)
            recent_vol = sum(r[2] for r in recent)
            ref_vol = sum(r[2] for r in reference)
            if ref_range <= 0 or ref_vol <= 0:
                continue
            if not (recent_range <= ref_range * contraction_ratio and recent_vol <= ref_vol * contraction_ratio):
                continue

            direction = 1 if move_pct > 0 else -1
            if require_trend:
                if len(trend_buf) < 2:
                    continue
                span = (trend_buf[-1][0] - trend_buf[0][0]).total_seconds()
                if span < trend_buf_span * 0.5:
                    continue
                trend_dir = 1 if trend_buf[-1][1] > trend_buf[0][1] else (-1 if trend_buf[-1][1] < trend_buf[0][1] else 0)
                if trend_dir != direction:
                    continue

            last_signal = t
            deadline = t + timedelta(seconds=CONTINUATION_SEC)
            future_idx = None
            for j in range(k + 1, n):
                if dts[j] >= deadline:
                    future_idx = j
                    break
            if future_idx is None:
                continue
            p_future = float(prices[future_idx])
            ret = (p_future - p) * direction
            hits.append(1 if ret > 0 else 0)

    return (float(np.mean(hits)) if hits else 0.0, len(hits))


def aggregate_hit_rate(universe_subset: dict, **kwargs) -> tuple[float, int]:
    total_hits, total_n = 0.0, 0
    for _code, days in universe_subset.items():
        hr, n = coil_trend_hit_rate(days, **kwargs)
        total_hits += hr * n
        total_n += n
    return (total_hits / total_n if total_n else 0.0, total_n)


def main() -> None:
    print("載入TAIFEX全市場個股期貨archive...")
    universe = load_broad_universe()
    print(f"  {len(universe)}檔通過流動性門檻(>={MIN_TOTAL_ROWS}行)")

    codes = sorted(universe.keys())
    rng = random.Random(RANDOM_SEED)
    shuffled = codes[:]
    rng.shuffle(shuffled)
    split = len(shuffled) // 2
    train_codes, holdout_codes = shuffled[:split], shuffled[split:]
    print(f"  train組{len(train_codes)}檔 / holdout組{len(holdout_codes)}檔（種子{RANDOM_SEED}固定可重現）")

    train_universe = {c: universe[c] for c in train_codes}
    holdout_universe = {c: universe[c] for c in holdout_codes}

    print("\n=== 對照：無coil無趨勢的純爆量訊號命中率（train組）===")
    hr0, n0 = aggregate_hit_rate(train_universe, contraction_ratio=1.0, min_coil_sec=3.0, require_trend=False)
    print(f"  純爆量: 命中率={hr0*100:.1f}% n={n0}")

    print(f"\nsweep grid: contraction_ratio={CONTRACTION_RATIO_GRID} x min_coil_sec={MIN_COIL_SEC_GRID}（僅coil, 不含趨勢）")
    best_coil = None
    for cr in CONTRACTION_RATIO_GRID:
        for mc in MIN_COIL_SEC_GRID:
            hr, n = aggregate_hit_rate(train_universe, contraction_ratio=cr, min_coil_sec=mc, require_trend=False)
            print(f"  ratio={cr} min_coil={mc}s: 命中率={hr*100:.1f}% n={n}")
            if n >= 30 and (best_coil is None or hr > best_coil[2]):
                best_coil = (cr, mc, hr, n)

    print(f"\nsweep grid: 同上組合 + 趨勢方向一致(trend_lookback={TREND_LOOKBACK_MIN}min)")
    best_coil_trend = None
    for cr in CONTRACTION_RATIO_GRID:
        for mc in MIN_COIL_SEC_GRID:
            hr, n = aggregate_hit_rate(train_universe, contraction_ratio=cr, min_coil_sec=mc, require_trend=True)
            print(f"  ratio={cr} min_coil={mc}s: 命中率={hr*100:.1f}% n={n}")
            if n >= 30 and (best_coil_trend is None or hr > best_coil_trend[2]):
                best_coil_trend = (cr, mc, hr, n)

    print("\n" + "=" * 90)
    print("=== TRAIN組最佳點 -> HOLDOUT組驗證（完全沒看過的股票）===")
    print(f"對照(無濾網) train命中率={hr0*100:.1f}%")

    if best_coil:
        cr, mc, train_hr, train_n = best_coil
        hold_hr, hold_n = aggregate_hit_rate(holdout_universe, contraction_ratio=cr, min_coil_sec=mc, require_trend=False)
        print(f"僅coil最佳點(ratio={cr},min_coil={mc}s): train命中率={train_hr*100:.1f}%(n={train_n}) "
              f"-> HOLDOUT命中率={hold_hr*100:.1f}%(n={hold_n})")
    else:
        print("僅coil：train組樣本數不足(<30)，無法選出可信最佳點")

    if best_coil_trend:
        cr, mc, train_hr, train_n = best_coil_trend
        hold_hr, hold_n = aggregate_hit_rate(holdout_universe, contraction_ratio=cr, min_coil_sec=mc, require_trend=True)
        print(f"coil+趨勢最佳點(ratio={cr},min_coil={mc}s): train命中率={train_hr*100:.1f}%(n={train_n}) "
              f"-> HOLDOUT命中率={hold_hr*100:.1f}%(n={hold_n})")
    else:
        print("coil+趨勢：train組樣本數不足(<30)，無法選出可信最佳點")

    hr0_hold, n0_hold = aggregate_hit_rate(holdout_universe, contraction_ratio=1.0, min_coil_sec=3.0, require_trend=False)
    print(f"對照(無濾網) HOLDOUT命中率={hr0_hold*100:.1f}%(n={n0_hold})")


if __name__ == "__main__":
    main()
