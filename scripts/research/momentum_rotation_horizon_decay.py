"""動能延續的「持有多久」衰減曲線 —— 訊號成立後，edge 在哪個時間尺度消失？

使用者問題（2026-08-20）：花 3 秒確認動能可能延續，那延續到多久就沒有差異了？
該持有多久平倉？

方法上的兩個關鍵決定：
1. **一律以「每秒 VWAP」建序列，不用該秒的第一筆/最後一筆。** archive CSV 的秒內
   順序是按價格排序不是時間序（2026-08-20 實測，見 memory
   taifex-tick-archive-within-second-ordering），所以「該秒最後一筆」系統性等於該秒
   最高價（遞增排序時）。VWAP 是**順序無關**的統計量，秒內怎麼排都不影響——這讓
   秒級以上的量測重新變得可信，代價是放棄次秒級解析度（本來也測不準）。
2. **每個訊號都配一組同標的同日的隨機時點對照。** 只看訊號後的絕對報酬會混進
   「那天那檔本來就在漲」的成分；要回答「延續有沒有 edge」必須跟基準相減。

輸出：各持有期的 訊號組 vs 對照組 差值（bps）、t 值、以及與成本地板的比較。
"""
from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ARCHIVE = Path.home() / "goldenstocks-data/cache/momentum_rotation/taifex_tick_daily_broad"
EXCLUDE = {"TMF"}
MIN_ROWS = 1500
SEED = 42

# 訊號參數沿用 broad-universe 那組（coil 收縮 + 趨勢延續 + 量能倍數）
COIL_SEC = 3          # 使用者說的「花 3 秒確認」
CONTRACTION = 0.4     # 最近 3 秒的振幅 <= 前 3 秒 × 0.4
TREND_SEC = 180       # 3 分鐘趨勢方向一致
VOL_MULT = 2.5
MOVE_BPS = 15.0       # 觸發那一秒的價格移動門檻（0.15%）
COOLDOWN_SEC = 10
MATCH_WIN = 900        # 對照組取樣視窗：訊號時刻 ±15 分鐘

HORIZONS = [1, 2, 3, 5, 8, 15, 30, 60, 120, 300, 600]


def per_second_series(times: list[str], prices: np.ndarray, vols: np.ndarray):
    """回傳 (秒索引 array, VWAP array, 每秒量 array)。秒索引 = 當日 08:45 起的秒數。"""
    sec_key: dict[int, list[int]] = defaultdict(list)
    for i, t in enumerate(times):
        hh, mm, ss = int(t[11:13]), int(t[14:16]), int(t[17:19])
        sec_key[hh * 3600 + mm * 60 + ss].append(i)
    secs = sorted(sec_key)
    vwap = np.empty(len(secs))
    svol = np.empty(len(secs))
    for k, s in enumerate(secs):
        idx = sec_key[s]
        p = prices[idx]
        v = vols[idx]
        tot = v.sum()
        vwap[k] = float((p * v).sum() / tot) if tot > 0 else float(p.mean())
        svol[k] = float(tot)
    return np.array(secs), vwap, svol


def find_signals(secs: np.ndarray, vwap: np.ndarray, svol: np.ndarray) -> list[tuple[int, int]]:
    """回傳 [(在 secs 裡的位置, 方向 +1/-1)]。"""
    out: list[tuple[int, int]] = []
    last_sig = -10**9
    vol_hist: list[float] = []
    pos_of_sec = {s: i for i, s in enumerate(secs)}
    for i in range(len(secs)):
        s = secs[i]
        vol_hist.append(svol[i])
        if i == 0:
            continue
        if s - last_sig < COOLDOWN_SEC:
            continue
        # 量能倍數：跟當日至今每秒量的中位數比
        base = max(statistics.median(vol_hist[:-1]), 1e-9) if len(vol_hist) > 1 else 1.0
        if svol[i] < VOL_MULT * base:
            continue
        # 價格移動（相對前一秒 VWAP）
        move = (vwap[i] - vwap[i - 1]) / vwap[i - 1] * 1e4
        if abs(move) < MOVE_BPS:
            continue
        direction = 1 if move > 0 else -1
        # coil：最近 COIL_SEC 秒的振幅 <= 再往前 COIL_SEC 秒 × CONTRACTION
        a0, a1 = pos_of_sec.get(s - COIL_SEC), i
        b0 = pos_of_sec.get(s - 2 * COIL_SEC)
        if a0 is None or b0 is None or a0 <= b0:
            continue
        recent = vwap[a0:a1]
        prior = vwap[b0:a0]
        if len(recent) < 2 or len(prior) < 2:
            continue
        if (recent.max() - recent.min()) > CONTRACTION * (prior.max() - prior.min()):
            continue
        # 趨勢一致：3 分鐘前到現在的方向要跟觸發方向相同
        t0 = pos_of_sec.get(s - TREND_SEC)
        if t0 is None:
            continue
        if np.sign(vwap[i] - vwap[t0]) != direction:
            continue
        out.append((i, direction))
        last_sig = s
    return out


def fwd_bps(secs: np.ndarray, vwap: np.ndarray, i: int, hz: int, direction: int) -> float | None:
    """持有 hz 秒後的方向調整報酬（bps）。用秒索引對齊，找不到就取下一個有成交的秒。"""
    target = secs[i] + hz
    j = int(np.searchsorted(secs, target))
    if j >= len(secs):
        return None
    return float((vwap[j] - vwap[i]) / vwap[i] * 1e4 * direction)


def main() -> None:
    rng = random.Random(SEED)
    print("載入 archive（只取 outright 合約）...", flush=True)
    sig_rows: list[dict] = []
    ctl_rows: list[dict] = []
    within_sec_bps: list[float] = []
    n_codes = 0
    for path in sorted(ARCHIVE.glob("*.csv")):
        code = path.stem
        if code in EXCLUDE:
            continue
        by_day: dict[str, tuple[list, list, list]] = defaultdict(lambda: ([], [], []))
        total = 0
        with path.open() as f:
            for r in csv.DictReader(f):
                if "/" in (r.get("contract_date") or ""):
                    continue
                d = (r.get("date") or "")[:10]
                if not d:
                    continue
                try:
                    px = float(r["price"]); vv = float(r["volume"])
                except (KeyError, ValueError, TypeError):
                    continue
                if px <= 0:
                    continue
                t, p, v = by_day[d]
                t.append(r["date"]); p.append(px); v.append(vv)
                total += 1
        if total < MIN_ROWS:
            continue
        n_codes += 1
        for d, (t, p, v) in by_day.items():
            order = sorted(range(len(t)), key=lambda k: t[k])
            t = [t[k] for k in order]
            p = np.array([p[k] for k in order]); v = np.array([v[k] for k in order])
            secs, vwap, svol = per_second_series(t, p, v)
            if len(secs) < 400:
                continue
            # 量測噪音地板：秒內價格全距（順序無關）
            sec_key: dict[int, list[float]] = defaultdict(list)
            for k, tt in enumerate(t):
                sec_key[int(tt[11:13]) * 3600 + int(tt[14:16]) * 60 + int(tt[17:19])].append(p[k])
            for arr in sec_key.values():
                if len(arr) >= 3:
                    within_sec_bps.append((max(arr) - min(arr)) / statistics.median(arr) * 1e4)
            sigs = find_signals(secs, vwap, svol)
            if not sigs:
                continue
            # 對照組：同一天同一檔，隨機挑同樣數量的秒（避開開盤前 TREND_SEC 秒）
            # ⚠️ 時刻配對：訊號有 72% 集中在開盤後 30 分鐘（實測分布見輸出），而不同時段
            # 本來就有不同的漂移結構。從整天隨機抽對照，量到的會是「開盤後價格怎麼走」
            # 而不是「訊號後價格怎麼走」。只在每個訊號時刻 ±MATCH_WIN 秒內抽對照。
            lo = int(np.searchsorted(secs, secs[0] + 2 * TREND_SEC))
            ctl = []
            for _i, _d in sigs:
                a = int(np.searchsorted(secs, secs[_i] - MATCH_WIN))
                b = int(np.searchsorted(secs, secs[_i] + MATCH_WIN))
                cand = [k for k in range(max(a, lo), min(b, len(secs) - 1)) if k != _i]
                if cand:
                    ctl.extend(rng.sample(cand, min(5, len(cand))))
            for i, dr in sigs:
                row = {"code": code, "date": d, "sec": int(secs[i]), "dir": dr,
                       "move_bps": float(abs(vwap[i] - vwap[i - 1]) / vwap[i - 1] * 1e4),
                       "vol_ratio": float(svol[i] / max(statistics.median(svol[:i]), 1e-9)) if i > 1 else 0.0,
                       "px": float(vwap[i])}
                for hz in HORIZONS:
                    row[str(hz)] = fwd_bps(secs, vwap, i, hz, dr)
                sig_rows.append(row)
            for i in ctl:
                dr = rng.choice([1, -1])
                row = {"code": code, "date": d}
                for hz in HORIZONS:
                    row[str(hz)] = fwd_bps(secs, vwap, i, hz, dr)
                ctl_rows.append(row)
    print(f"  {n_codes} 檔通過門檻 · 訊號 {len(sig_rows)} 個 · 對照 {len(ctl_rows)} 個")
    print(f"  量測噪音地板（秒內價格全距）中位 {statistics.median(within_sec_bps):.1f}bps"
          f" · p75 {sorted(within_sec_bps)[len(within_sec_bps)*3//4]:.1f}bps\n")

    print("=" * 96)
    print("持有期  訊號組均值   對照組均值      差值(edge)      t值    訊號n    判定")
    print("-" * 96)
    res = []
    for hz in HORIZONS:
        s = [r[str(hz)] for r in sig_rows if r.get(str(hz)) is not None]
        c = [r[str(hz)] for r in ctl_rows if r.get(str(hz)) is not None]
        if len(s) < 20 or len(c) < 20:
            continue
        ms, mc = statistics.mean(s), statistics.mean(c)
        edge = ms - mc
        se = math.sqrt(statistics.variance(s) / len(s) + statistics.variance(c) / len(c))
        t = edge / se if se > 0 else float("nan")
        mark = "顯著" if abs(t) >= 2 else ("邊緣" if abs(t) >= 1.5 else "無差異")
        print(f"{hz:>5d}s {ms:>+11.2f} {mc:>+12.2f} {edge:>+15.2f}bps {t:>+8.2f} {len(s):>7d}    {mark}")
        res.append({"hz": hz, "sig": ms, "ctl": mc, "edge": edge, "t": t, "n": len(s)})
    print("\n=== 訊號時刻分布（確認對照組配對是否必要）===")
    hh = defaultdict(int)
    for r in sig_rows:
        hh[f"{r['sec']//3600:02d}:{(r['sec']%3600)//60//15*15:02d}"] += 1
    for k in sorted(hh):
        print(f"  {k}  {hh[k]:4d}  {'#' * (hh[k] * 50 // max(hh.values()))}")

    print("\n=== 依方向拆解（edge, bps / t 值）===")
    print(f"{'持有':>6s}{'做多訊號':>16s}{'做空訊號':>16s}")
    for hz in HORIZONS:
        cells = []
        for want in (1, -1):
            s_ = [r[str(hz)] for r in sig_rows if r["dir"] == want and r.get(str(hz)) is not None]
            c_ = [r[str(hz)] for r in ctl_rows if r.get(str(hz)) is not None]
            if len(s_) < 20:
                cells.append("      n/a       ")
                continue
            e = statistics.mean(s_) - statistics.mean(c_)
            se = math.sqrt(statistics.variance(s_) / len(s_) + statistics.variance(c_) / len(c_))
            cells.append(f"{e:>+9.2f} (t{e/se:>+5.2f})" if se > 0 else "      n/a       ")
        print(f"{hz:>5d}s{cells[0]:>16s}{cells[1]:>16s}")

    rec = Path("reports/research/momentum_rotation_signal_records.json")
    rec.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"signals": sig_rows, "controls": ctl_rows}, open(rec, "w"))
    print(f"訊號層級明細已存 {rec}（{len(sig_rows)} 訊號 / {len(ctl_rows)} 對照）")

    out = Path("reports/research/momentum_rotation_horizon_decay.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    json.dump({"rows": res, "noise_floor_bps": statistics.median(within_sec_bps)}, open(out, "w"))
    print(f"\n已存 {out}")


if __name__ == "__main__":
    main()
