#!/usr/bin/env python3
"""G1b — 成交量節點（volume profile high-volume node, HVN）觸價檢定。

問題：價格走到「最近 10 分鐘成交量明顯高於平均」的價位（節點）時，會不會被擋回？
對日內回歸策略而言「被擋回」是好事、「穿過」是壞事、「卡住不動」也是壞事
（成交後走不動、被 max_hold 平掉），所以三類分開報。

設計
--------------------------------------------------------------------
每 GRID 秒一個快照 t0：
  * 用 [t0-600, t0) 的成交建 volume profile（**不含當下這一刻**）。S0 = t0 前最後成交價。
  * 對每個距離 d ∈ [DMIN, DMAX]、每一側（下方＝買進做多、上方＝賣出做空），
    價位 ℓ = S0 ∓ d 依 band 寬度 b 的節點強度 ratio 分成六類：
       0: ratio ≥ 10x   1: ≥5x   2: ≥3x        → 節點（三種強度）
       3: 1.5x ≤ ratio < 3x                      → 中間帶（不用）
       4: ratio < 1.5x 且該價位有量               → **對照層 2**：同距離、價格最近曾到過、量普通
       5: ratio < 1.5x 且該價位無量               → 對照層 3：同距離、10 分鐘內沒走過
    ratio = (含 ℓ 的最佳 b 寬度區間成交量) / (b × 窗內每有量價位平均量)
  * 「同一快照、同一側、同一距離」只有唯一一個價位，所以配對只能跨快照做：
    一律在 stratum =(session, side, **精確距離 d**) 內比較，再以 stratum 事件數加權彙總。
    距離／可達性的機械效應被完全 match 掉。
  * 觸價 = 該價位在 (t0, t0+W_REACH] 被成交（下方：成交價 ≤ ℓ；上方：≥ ℓ）。
    成交當下那一筆**排除**在後續報酬之外（避免同棒偏誤）。
  * 觸價後 60/300/900 秒：fav = sign×(P(τ+h) − ℓ) 為回歸方向毛報酬（點）；
    triple barrier 三分類：先碰 +K = 擋回、先碰 −K = 穿過、都沒碰 = 卡住。

三個對照／安慰劑（硬規則 #6）
  A. 對照層 2「有量但普通」——排除「節點只是價格剛從那裡走過來」。
  B. 對照層 3「10 分鐘內沒走過」。
  C. **區間位置配對**：再把 stratum 加上「ℓ 在該 10 分鐘價格區間中的相對位置」
     （區間外下方 / 下 1/3 / 中 1/3 / 上 1/3 / 區間外上方），排除
     「節點多半落在區間中央、回到中央本來就比較會反彈」這個純幾何解釋。
  D. **陳舊節點安慰劑**：改用 [t0-4200, t0-3600) 的 profile 標記同一批事件。
     若陳舊節點也有一樣的效果，代表跟「最近 10 分鐘的量」無關。

叢集單位 = 交易日（硬規則 #7）。日層 block bootstrap + 逐年拆解 + 連續 IS/OOS 切塊。

合約選擇（硬規則 #2）：ex-ante 第三個週三規則，**禁止** argmax 主力月。
  交易日 D 的日盤與夜盤都用 front(D)；front(D) = 當月 if D ≤ 第三個週三 else 次月。
  夜盤定義為「前一交易日 15:00 → 本交易日 05:00」，所以在結算日 15:00 自動提前一天轉倉。

分片（硬規則 #3）：連續日期塊；沒有任何跨塊滾動狀態（回看窗口全在單一 session 內）。
"""
from __future__ import annotations

import argparse
import calendar as calendar_mod
import datetime as dt
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

try:
    import orjson as _fastjson

    def _load_json(path: str):
        with open(path, "rb") as fh:
            return _fastjson.loads(fh.read())
except Exception:  # pragma: no cover
    def _load_json(path: str):
        with open(path) as fh:
            return json.load(fh)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR", str(Path.home() / "goldenstocks-data")))
TICK_DIR = DATA_DIR / "cache" / "tmf_channel" / "finmind_tx_tick_by_day"
OUT_PATH = PROJECT_ROOT / "reports" / "research" / "channel_lab" / "vp_g1b_node_touch.json"

# ---- 參數 ----------------------------------------------------------------
W_LOOKBACK = 600
GRID = 240
W_REACH = 600
STALE_LAG = 3600          # 安慰劑：往前推一小時的 profile
HORIZONS = (60, 300, 900)
H_POS = 1                 # 區間位置配對只做 h=300（HORIZONS[1]）
K_MAIN = 4.0
K_ALT = 2.0
DMIN, DMAX = 3, 45
N_D = DMAX - DMIN + 1
BANDS = (1, 2, 3, 5)
N_CLS = 6
N_LABEL = len(BANDS) * N_CLS
N_POS = 5
MIN_WINDOW_TICKS = 30
MIN_SESSION_TICKS = 500
HANG_LO, HANG_HI = 12, 33

DAY_START, DAY_END = 8 * 3600 + 45 * 60, 13 * 3600 + 45 * 60
NIGHT_START, NIGHT_END = 15 * 3600, 86400 + 5 * 3600

S_N, S_FAV, S_B1, S_T1, S_S1, S_B2, S_T2, S_S2, S_MFE, S_MAE = range(10)
N_STAT = 10
OUT_SHAPE = (2, 2, N_D, N_LABEL, len(HORIZONS), N_STAT)
REACH_SHAPE = (2, 2, N_D, N_LABEL, 3)          # offered, reached, sum_reach_seconds
POS_SHAPE = (2, 2, N_D, N_LABEL, N_POS, 2)     # n, sum_fav  (h=300 only)
PLC_SHAPE = (2, 2, N_D, N_LABEL, len(HORIZONS), 2)  # n, sum_fav  (陳舊節點標記)


# ---- ex-ante 合約 --------------------------------------------------------
def third_wednesday(year: int, month: int) -> dt.date:
    c = calendar_mod.Calendar()
    weds = [d for d in c.itermonthdates(year, month) if d.month == month and d.weekday() == 2]
    return weds[2]


def front_contract(d: dt.date) -> str:
    w3 = third_wednesday(d.year, d.month)
    if d <= w3:
        return f"{d.year:04d}{d.month:02d}"
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return f"{y:04d}{m:02d}"


# ---- 檔案載入 ------------------------------------------------------------
class FileCache:
    def __init__(self, maxn: int = 5):
        self.maxn = maxn
        self.store: dict[str, dict | None] = {}
        self.order: list[str] = []

    def get(self, datestr: str):
        if datestr in self.store:
            return self.store[datestr]
        path = TICK_DIR / f"{datestr}.json"
        parsed = _parse_file(str(path)) if path.exists() else None
        self.store[datestr] = parsed
        self.order.append(datestr)
        while len(self.order) > self.maxn:
            self.store.pop(self.order.pop(0), None)
        return parsed


def _parse_file(path: str):
    rows = _load_json(path)
    n = len(rows)
    sec = np.empty(n, dtype=np.int32)
    price = np.empty(n, dtype=np.int32)
    vol = np.empty(n, dtype=np.int32)
    contracts: list[str] = []
    k = 0
    for r in rows:
        cd = r["contract_date"]
        if "/" in cd:          # 價差單，不是 outright
            continue
        if r.get("futures_id") != "TX":
            continue
        s = r["date"]
        sec[k] = int(s[11:13]) * 3600 + int(s[14:16]) * 60 + int(s[17:19])
        price[k] = int(r["price"])
        vol[k] = int(r["volume"])
        contracts.append(cd)
        k += 1
    sec, price, vol = sec[:k], price[:k], vol[:k]
    carr = np.array(contracts, dtype=object)
    order = np.argsort(sec, kind="stable")
    return {"sec": sec[order], "price": price[order], "vol": vol[order], "con": carr[order]}


def _slice(parsed, con: str, lo: int, hi: int, offset: int = 0):
    if parsed is None:
        return None
    m = (parsed["con"] == con) & (parsed["sec"] >= lo) & (parsed["sec"] < hi)
    if not m.any():
        return None
    return (parsed["sec"][m].astype(np.int64) + offset,
            parsed["price"][m].astype(np.int64),
            parsed["vol"][m].astype(np.float64))


# ---- 節點分類 ------------------------------------------------------------
def _classify(r, v):
    cls = np.full(r.shape, 5, dtype=np.int64)
    cls = np.where(v > 0, 4, cls)
    cls = np.where(r >= 1.5, 3, cls)
    cls = np.where(r >= 3.0, 2, cls)
    cls = np.where(r >= 5.0, 1, cls)
    cls = np.where(r >= 10.0, 0, cls)
    return cls


def _profile_ratios(pidx, vol, i_lo, i_hi, span):
    """回傳 (prof, ratios[nB,span], lo_lvl, hi_lvl) 或 None。"""
    prof = np.bincount(pidx[i_lo:i_hi], weights=vol[i_lo:i_hi], minlength=span)
    nz = np.flatnonzero(prof)
    if nz.size < 5:
        return None
    mean_lvl = float(prof.sum()) / nz.size
    if mean_lvl <= 0:
        return None
    csum = np.concatenate(([0.0], np.cumsum(prof)))
    ratios = np.zeros((len(BANDS), span))
    for bi, b in enumerate(BANDS):
        L = span - b + 1
        if L <= 0:
            continue
        bandsum = csum[b:] - csum[:-b]
        sc = np.zeros(span)
        for k in range(b):
            sc[k:k + L] = np.maximum(sc[k:k + L], bandsum)
        ratios[bi] = sc / (b * mean_lvl)
    return prof, ratios, int(nz[0]), int(nz[-1])


def _labels_for(ratios, prof, span, levels, pmin):
    lidx = levels - pmin
    inside = (lidx >= 0) & (lidx < span)
    li_c = np.clip(lidx, 0, span - 1)
    v_here = np.where(inside, prof[li_c], 0.0)
    r_here = np.where(inside[None, :], ratios[:, li_c], 0.0)
    cls = _classify(r_here, np.broadcast_to(v_here, r_here.shape))
    return cls


# ---- 單一 session 掃描 ---------------------------------------------------
def scan_session(ts, price, vol, sess_idx, out, reach, posa, plc, diag):
    n = len(ts)
    if n < MIN_SESSION_TICKS:
        return
    pmin = int(price.min())
    span = int(price.max()) - pmin + 1
    if span < 20:
        return
    pidx = (price - pmin).astype(np.int64)

    t_start = int(ts[0]) + W_LOOKBACK
    t_end = int(ts[-1]) - W_REACH - HORIZONS[-1]
    if t_end <= t_start:
        return

    out2 = out.reshape(-1, N_STAT)
    reach2 = reach.reshape(-1, 3)
    pos2 = posa.reshape(-1, 2)
    plc2 = plc.reshape(-1, 2)
    nB, nH = len(BANDS), len(HORIZONS)
    d_arr = np.arange(DMIN, DMAX + 1, dtype=np.int64)
    bi_off = (np.arange(nB) * N_CLS).reshape(nB, 1)

    for t0 in range(t_start, t_end + 1, GRID):
        i_lo = int(np.searchsorted(ts, t0 - W_LOOKBACK, "left"))
        i_hi = int(np.searchsorted(ts, t0, "left"))
        if i_hi - i_lo < MIN_WINDOW_TICKS:
            continue
        cur = _profile_ratios(pidx, vol, i_lo, i_hi, span)
        if cur is None:
            continue
        prof, ratios, lo_i, hi_i = cur
        S0 = int(price[i_hi - 1])
        diag["snaps"] += 1
        diag["sum_levels"] += float(np.count_nonzero(prof))
        diag["sum_range"] += float(hi_i - lo_i)
        rng = float(hi_i - lo_i)

        # 陳舊節點安慰劑
        s_lo = int(np.searchsorted(ts, t0 - STALE_LAG - W_LOOKBACK, "left"))
        s_hi = int(np.searchsorted(ts, t0 - STALE_LAG, "left"))
        stale = None
        if s_hi - s_lo >= MIN_WINDOW_TICKS:
            st = _profile_ratios(pidx, vol, s_lo, s_hi, span)
            if st is not None:
                stale = st
                diag["snaps_with_stale"] += 1

        f_lo = i_hi
        f_hi = int(np.searchsorted(ts, t0 + W_REACH, "right"))
        if f_hi - f_lo < 5:
            continue
        fp = price[f_lo:f_hi]
        cmin = np.minimum.accumulate(fp)
        cmax = np.maximum.accumulate(fp)
        nf = f_hi - f_lo

        for side in (0, 1):
            sign = 1.0 if side == 0 else -1.0
            levels = (S0 - d_arr) if side == 0 else (S0 + d_arr)
            cls = _labels_for(ratios, prof, span, levels, pmin)
            lab = bi_off + cls
            cell_base = ((sess_idx * 2 + side) * N_D + np.arange(N_D)) * N_LABEL
            flat_lab = cell_base[None, :] + lab

            reach2[flat_lab.ravel(), 0] += 1.0

            # 區間位置桶
            if rng > 0:
                rel = (levels - (lo_i + pmin)) / rng
                pbucket = np.where(rel < 0, 0,
                          np.where(rel < 1.0 / 3, 1,
                          np.where(rel < 2.0 / 3, 2,
                          np.where(rel <= 1.0, 3, 4)))).astype(np.int64)
            else:
                pbucket = np.full(N_D, 2, dtype=np.int64)

            if stale is not None:
                cls_s = _labels_for(stale[1], stale[0], span, levels, pmin)
                flat_lab_s = cell_base[None, :] + (bi_off + cls_s)
            else:
                flat_lab_s = None

            ridx = (np.searchsorted(-cmin, -levels, side="left") if side == 0
                    else np.searchsorted(cmax, levels, side="left"))
            hit = ridx < nf
            if not hit.any():
                continue
            hit_d = np.flatnonzero(hit)
            jg_all = f_lo + ridx[hit_d]
            tau_all = ts[jg_all]
            fl_hit = flat_lab[:, hit_d]
            reach2[fl_hit.ravel(), 1] += 1.0
            reach2[fl_hit.ravel(), 2] += np.broadcast_to(
                (tau_all - t0).astype(float), fl_hit.shape).ravel()

            nd = hit_d[cls[0, hit_d] <= 2]
            diag["node_hits"] += int(nd.size)
            if nd.size:
                diag["node_dist_sum"] += float((DMIN + nd).sum())

            hz = np.asarray(HORIZONS, dtype=np.int64)
            sv3 = np.zeros((nH, N_STAT))
            for kk in range(hit_d.size):
                di = int(hit_d[kk])
                lv = float(levels[di])
                jg = int(jg_all[kk])
                tau = int(tau_all[kk])
                base = jg + 1
                ends = np.searchsorted(ts, tau + hz, "right")
                seg_end = int(ends[-1])
                if seg_end <= base:
                    continue
                fs = sign * (price[base:seg_end] - lv)
                cmx = np.maximum.accumulate(fs)
                cmn = np.minimum.accumulate(fs)
                ncmn = -cmn
                # 首次觸及各門檻的位置（cmx 非遞減、-cmn 非遞減，可直接二分）
                iu1 = int(np.searchsorted(cmx, K_MAIN, "left"))
                id1 = int(np.searchsorted(ncmn, K_MAIN, "left"))
                iu2 = int(np.searchsorted(cmx, K_ALT, "left"))
                id2 = int(np.searchsorted(ncmn, K_ALT, "left"))
                sv3[:] = 0.0
                nvalid = 0
                for hi_ in range(nH):
                    Lh = int(ends[hi_]) - base
                    if Lh <= 0:
                        continue
                    nvalid += 1
                    row = sv3[hi_]
                    row[S_N] = 1.0
                    row[S_FAV] = float(fs[Lh - 1])
                    row[S_MFE] = float(cmx[Lh - 1])
                    row[S_MAE] = float(cmn[Lh - 1])
                    u1, d1 = iu1 < Lh, id1 < Lh
                    row[S_B1 + (0 if (u1 and (not d1 or iu1 < id1)) else (1 if d1 else 2))] = 1.0
                    u2, d2 = iu2 < Lh, id2 < Lh
                    row[S_B2 + (0 if (u2 and (not d2 or iu2 < id2)) else (1 if d2 else 2))] = 1.0
                if nvalid == 0:
                    continue
                rows_all = (flat_lab[:, di, None] * nH + np.arange(nH)).ravel()
                vals = np.broadcast_to(sv3, (nB, nH, N_STAT)).reshape(nB * nH, N_STAT)
                out2[rows_all] += vals
                if flat_lab_s is not None:
                    rows_s = (flat_lab_s[:, di, None] * nH + np.arange(nH)).ravel()
                    plc2[rows_s] += vals[:, [S_N, S_FAV]]
                if sv3[H_POS, S_N] > 0:
                    rp = flat_lab[:, di] * N_POS + int(pbucket[di])
                    pos2[rp, 0] += 1.0
                    pos2[rp, 1] += sv3[H_POS, S_FAV]


# ---- 每個交易日 ----------------------------------------------------------
def process_day(day: str, prev_day: str | None, cache: FileCache):
    d = dt.date.fromisoformat(day)
    con = front_contract(d)
    out = np.zeros(OUT_SHAPE)
    reach = np.zeros(REACH_SHAPE)
    posa = np.zeros(POS_SHAPE)
    plc = np.zeros(PLC_SHAPE)
    diag = {"snaps": 0, "snaps_with_stale": 0, "sum_range": 0.0, "sum_levels": 0.0,
            "node_hits": 0, "node_dist_sum": 0.0, "day_ticks": 0, "night_ticks": 0,
            "con_share": 0.0, "sessions": 0, "contract": con, "median_px": float("nan")}

    f_today = cache.get(day)
    if f_today is None:
        return None
    tot_all = len(f_today["con"])
    diag["con_share"] = float((f_today["con"] == con).sum() / tot_all) if tot_all else 0.0

    s = _slice(f_today, con, DAY_START, DAY_END)
    if s is not None and len(s[0]) >= MIN_SESSION_TICKS:
        diag["day_ticks"] = len(s[0])
        diag["median_px"] = float(np.median(s[1]))
        diag["sessions"] += 1
        scan_session(s[0], s[1], s[2], 0, out, reach, posa, plc, diag)

    if prev_day is not None:
        p1 = _slice(cache.get(prev_day), con, NIGHT_START, 86400)
        nxt = (dt.date.fromisoformat(prev_day) + dt.timedelta(days=1)).isoformat()
        p2 = _slice(cache.get(nxt), con, 0, 5 * 3600, offset=86400)
        parts = [p for p in (p1, p2) if p is not None]
        if parts:
            ts = np.concatenate([p[0] for p in parts])
            pr = np.concatenate([p[1] for p in parts])
            vl = np.concatenate([p[2] for p in parts])
            o = np.argsort(ts, kind="stable")
            if len(ts) >= MIN_SESSION_TICKS:
                diag["night_ticks"] = len(ts)
                diag["sessions"] += 1
                scan_session(ts[o], pr[o], vl[o], 1, out, reach, posa, plc, diag)

    return (day, out.astype(np.float32), reach.astype(np.float32),
            posa.astype(np.float32), plc.astype(np.float32), diag)


def worker(chunk):
    cache = FileCache(maxn=5)
    res = []
    for day, prev in chunk:
        try:
            r = process_day(day, prev, cache)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[warn] {day}: {exc!r}\n")
            r = None
        if r is not None:
            res.append(r)
    return res


# ---- 統計工具 ------------------------------------------------------------
CLS_SETS = {"n3": (0, 1, 2), "n5": (0, 1), "n10": (0,), "mid": (3,), "ord": (4,), "unt": (5,)}


def lab_idx(bi, name):
    return [bi * N_CLS + c for c in CLS_SETS[name]]


def pick_full(arr, bi, name):
    """(2,2,N_D,N_LABEL,H,STAT) → (2,2,N_D,H,STAT)"""
    return arr[:, :, :, lab_idx(bi, name), :, :].sum(axis=3)


def stratified_diff(arr, bi, arm, ctrl, hi_, dlo, dhi, sess=None, side=None, stat_fav=S_FAV):
    A = pick_full(arr, bi, arm)
    C = pick_full(arr, bi, ctrl)
    ss = range(2) if sess is None else [sess]
    sd = range(2) if side is None else [side]
    num = den = na = nc = sa = sc = 0.0
    nstrat = 0
    for s_ in ss:
        for e_ in sd:
            for di in range(N_D):
                d = DMIN + di
                if not (dlo <= d <= dhi):
                    continue
                a = A[s_, e_, di, hi_]
                c = C[s_, e_, di, hi_]
                if a[S_N] < 1 or c[S_N] < 1:
                    continue
                w = a[S_N] * c[S_N] / (a[S_N] + c[S_N])
                num += w * (a[stat_fav] / a[S_N] - c[stat_fav] / c[S_N])
                den += w
                na += a[S_N]; nc += c[S_N]; sa += a[stat_fav]; sc += c[stat_fav]
                nstrat += 1
    if den <= 0:
        return None
    return {"diff_pts": float(num / den), "n_arm": int(na), "n_ctrl": int(nc),
            "n_strata": nstrat,
            "mean_arm_pts": float(sa / na) if na else None,
            "mean_ctrl_pts": float(sc / nc) if nc else None}


def stratified_diff_pos(arr, bi, arm, ctrl, dlo, dhi):
    """區間位置也進 stratum：(session, side, 精確 d, 位置桶)。arr = POS_SHAPE 累積。"""
    A = arr[:, :, :, lab_idx(bi, arm), :, :].sum(axis=3)   # (2,2,N_D,N_POS,2)
    C = arr[:, :, :, lab_idx(bi, ctrl), :, :].sum(axis=3)
    num = den = na = nc = 0.0
    nstrat = 0
    for s_ in range(2):
        for e_ in range(2):
            for di in range(N_D):
                d = DMIN + di
                if not (dlo <= d <= dhi):
                    continue
                for pb in range(N_POS):
                    a = A[s_, e_, di, pb]
                    c = C[s_, e_, di, pb]
                    if a[0] < 1 or c[0] < 1:
                        continue
                    w = a[0] * c[0] / (a[0] + c[0])
                    num += w * (a[1] / a[0] - c[1] / c[0])
                    den += w
                    na += a[0]; nc += c[0]; nstrat += 1
    if den <= 0:
        return None
    return {"diff_pts": float(num / den), "n_arm": int(na), "n_ctrl": int(nc),
            "n_strata": nstrat}


def stratified_diff_small(arr, bi, arm, ctrl, hi_, dlo, dhi):
    """arr = PLC_SHAPE (n, sum_fav)。"""
    A = arr[:, :, :, lab_idx(bi, arm), :, :].sum(axis=3)
    C = arr[:, :, :, lab_idx(bi, ctrl), :, :].sum(axis=3)
    num = den = na = nc = 0.0
    for s_ in range(2):
        for e_ in range(2):
            for di in range(N_D):
                d = DMIN + di
                if not (dlo <= d <= dhi):
                    continue
                a = A[s_, e_, di, hi_]
                c = C[s_, e_, di, hi_]
                if a[0] < 1 or c[0] < 1:
                    continue
                w = a[0] * c[0] / (a[0] + c[0])
                num += w * (a[1] / a[0] - c[1] / c[0])
                den += w
                na += a[0]; nc += c[0]
    if den <= 0:
        return None
    return {"diff_pts": float(num / den), "n_arm": int(na), "n_ctrl": int(nc)}


def class_mix(arr, bi, name, hi_, dlo, dhi):
    A = pick_full(arr, bi, name)
    n = f = mfe = mae = 0.0
    b1 = t1 = s1 = b2 = t2 = s2 = 0.0
    for s_ in range(2):
        for e_ in range(2):
            for di in range(N_D):
                d = DMIN + di
                if not (dlo <= d <= dhi):
                    continue
                c = A[s_, e_, di, hi_]
                n += c[S_N]; f += c[S_FAV]; mfe += c[S_MFE]; mae += c[S_MAE]
                b1 += c[S_B1]; t1 += c[S_T1]; s1 += c[S_S1]
                b2 += c[S_B2]; t2 += c[S_T2]; s2 += c[S_S2]
    if n <= 0:
        return None
    return {"n": int(n), "mean_fav_pts": float(f / n),
            "mean_mfe_pts": float(mfe / n), "mean_mae_pts": float(mae / n),
            f"K{K_MAIN:g}": {"bounce": float(b1 / n), "through": float(t1 / n), "stuck": float(s1 / n)},
            f"K{K_ALT:g}": {"bounce": float(b2 / n), "through": float(t2 / n), "stuck": float(s2 / n)}}


def day_diff_series(per_day_small, strat_mean, bi, arm, ctrl, hi_, dlo, dhi):
    a_idx = lab_idx(bi, arm)
    c_idx = lab_idx(bi, ctrl)
    keep = [i for i in range(N_D) if dlo <= DMIN + i <= dhi]
    rows = []
    for day, arr in per_day_small:      # (2,2,N_D,N_LABEL,H,2)
        na = nc = sa = sc = 0.0
        for s_ in range(2):
            for e_ in range(2):
                sub = arr[s_, e_]
                for di in keep:
                    m = strat_mean[s_, e_, di, hi_]
                    if not np.isfinite(m):
                        continue
                    an = sub[di, a_idx, hi_, 0].sum(); af = sub[di, a_idx, hi_, 1].sum()
                    cn = sub[di, c_idx, hi_, 0].sum(); cf = sub[di, c_idx, hi_, 1].sum()
                    na += an; sa += af - an * m
                    nc += cn; sc += cf - cn * m
        if na >= 1 and nc >= 1:
            rows.append((day, float(sa / na - sc / nc), float(min(na, nc))))
    return rows


def boot_ci(vals, wts, n_boot=2000, seed=17):
    if len(vals) < 10:
        return None
    v = np.asarray(vals, float); w = np.asarray(wts, float)
    rng = np.random.default_rng(seed)
    n = len(v)
    est = float((v * w).sum() / w.sum())
    idx = rng.integers(0, n, (n_boot, n))
    ww = w[idx]
    draws = (v[idx] * ww).sum(axis=1) / ww.sum(axis=1)
    lo, hi = np.percentile(draws, [2.5, 97.5])
    p_two = 2 * min(float((draws <= 0).mean()), float((draws >= 0).mean()))
    # 以日為叢集的 t 統計（未加權）
    tstat = float(v.mean() / (v.std(ddof=1) / math.sqrt(n))) if v.std(ddof=1) > 0 else None
    contrib = v * w
    order = np.argsort(-np.abs(contrib))
    top5 = max(1, int(round(n * 0.05)))
    conc = float(contrib[order[:top5]].sum() / contrib.sum()) if contrib.sum() != 0 else None
    return {"est_pts": est, "ci95": [float(lo), float(hi)], "p_boot": float(min(1.0, p_two)),
            "n_days": n, "share_days_positive": float((v > 0).mean()),
            "t_day_cluster": tstat,
            "share_of_effect_from_top5pct_days": conc}


# ---- 主流程 --------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-days", type=int, default=0)
    ap.add_argument("--workers", type=int, default=9)
    ap.add_argument("--chunks", type=int, default=45)
    ap.add_argument("--out", default=str(OUT_PATH))
    ap.add_argument("--date-start", default=None, help="只掃描此日(含)之後的交易日")
    ap.add_argument("--date-end", default=None, help="只掃描此日(含)之前的交易日")
    ap.add_argument("--part-out", default=None, help="把逐日陣列存成 npz（分段跑用）")
    ap.add_argument("--reduce", default=None, help="glob：合併所有 npz 分段並輸出 JSON")
    ap.add_argument("--min-front-share", type=float, default=0.0,
                    help="穩健性：丟掉 ex-ante 近月成交佔比低於此值的日子（結算日）")
    args = ap.parse_args()

    if args.reduce:
        reduce_parts(args.reduce, args.out, args.min_front_share)
        return

    all_files = sorted(p.stem for p in TICK_DIR.glob("*.json"))
    cand = [f for f in all_files if dt.date.fromisoformat(f).weekday() <= 4]
    if args.limit_days:
        cand = cand[-args.limit_days:]
    days_pairs = [(d, cand[i - 1] if i > 0 else None) for i, d in enumerate(cand)]
    if args.date_start:
        days_pairs = [x for x in days_pairs if x[0] >= args.date_start]
    if args.date_end:
        days_pairs = [x for x in days_pairs if x[0] <= args.date_end]

    nchunk = max(1, min(args.chunks, len(days_pairs)))
    csize = math.ceil(len(days_pairs) / nchunk)
    chunks = [days_pairs[i:i + csize] for i in range(0, len(days_pairs), csize)]  # 連續塊

    if args.part_out:
        run_part(chunks, args.workers, args.part_out)
        return

    # 段落成員（事前決定，才能邊跑邊折疊）
    all_days = [d for d, _ in days_pairs]
    cut = int(len(all_days) * 0.6)
    seg_days = {"ALL": set(all_days), "IS": set(all_days[:cut]), "OOS": set(all_days[cut:])}
    for y in sorted({d[:4] for d in all_days}):
        seg_days[f"Y{y}"] = {d for d in all_days if d[:4] == y}
    seg_out = {k: np.zeros(OUT_SHAPE) for k in seg_days}
    seg_pos = {k: np.zeros(POS_SHAPE) for k in seg_days}
    seg_plc = {k: np.zeros(PLC_SHAPE) for k in seg_days}
    seg_reach = {k: np.zeros(REACH_SHAPE) for k in seg_days}
    reach_all = np.zeros(REACH_SHAPE)
    per_day_small: list[tuple[str, np.ndarray]] = []
    diags: dict[str, dict] = {}

    t0 = time.time()
    ndone = 0
    it = (map(worker, chunks) if args.workers <= 1 else None)
    if it is None:
        ex = ProcessPoolExecutor(max_workers=args.workers)
        it = ex.map(worker, chunks)
    else:
        ex = None
    for res in it:
        for day, out, reach, posa, plc, diag in res:
            if diag["sessions"] == 0:
                continue
            for k, ds in seg_days.items():
                if day in ds:
                    seg_out[k] += out
                    seg_pos[k] += posa
                    seg_plc[k] += plc
                    seg_reach[k] += reach
            reach_all += reach
            per_day_small.append((day, out[..., [S_N, S_FAV]].astype(np.float64)))
            diags[day] = diag
            ndone += 1
        sys.stderr.write(f"[prog] days={ndone} t={time.time()-t0:.0f}s\n")
    if ex is not None:
        ex.shutdown()
    per_day_small.sort(key=lambda x: x[0])
    sys.stderr.write(f"[done] scan days={ndone} in {time.time()-t0:.0f}s\n")

    payload = summarise(per_day_small, diags, seg_days, seg_out, seg_pos, seg_plc, reach_all,
                        cut, seg_reach)
    outp = Path(args.out)
    assert not str(outp.resolve()).startswith(str(TICK_DIR.resolve())), "輸出不可與輸入同路徑"
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"wrote {outp}")


def run_part(chunks, nworkers, part_out):
    """分段掃描：把逐日陣列原樣存成 npz，之後用 --reduce 合併。"""
    t0 = time.time()
    days, outs, reaches, poss, plcs, diags = [], [], [], [], [], {}
    it = map(worker, chunks) if nworkers <= 1 else None
    ex = None
    if it is None:
        ex = ProcessPoolExecutor(max_workers=nworkers)
        it = ex.map(worker, chunks)
    for res in it:
        for day, out, reach, posa, plc, diag in res:
            if diag["sessions"] == 0:
                continue
            days.append(day); outs.append(out); reaches.append(reach)
            poss.append(posa); plcs.append(plc); diags[day] = diag
        sys.stderr.write(f"[prog] days={len(days)} t={time.time()-t0:.0f}s\n")
    if ex is not None:
        ex.shutdown()
    np.savez(part_out, days=np.array(days), out=np.stack(outs), reach=np.stack(reaches),
             pos=np.stack(poss), plc=np.stack(plcs),
             diags=np.array(json.dumps(diags, ensure_ascii=False)))
    sys.stderr.write(f"[part] wrote {part_out} days={len(days)} in {time.time()-t0:.0f}s\n")


def reduce_parts(pattern: str, out_path: str, min_front_share: float = 0.0):
    import glob as _glob
    files = sorted(_glob.glob(pattern))
    assert files, f"no partial matched {pattern}"
    all_days, diags = [], {}
    store = {}
    for f in files:
        z = np.load(f, allow_pickle=False)
        dd = [str(x) for x in z["days"]]
        diags.update(json.loads(str(z["diags"])))
        zo, zr, zp, zq = z["out"], z["reach"], z["pos"], z["plc"]  # 一次讀出，勿在迴圈內重讀
        for i, day in enumerate(dd):
            store[day] = (zo[i], zr[i], zp[i], zq[i])
        all_days.extend(dd)
        sys.stderr.write(f"[reduce] loaded {f} days={len(dd)}\n")
    all_days = sorted(set(all_days))
    if min_front_share > 0:
        dropped = [d for d in all_days if diags[d]["con_share"] < min_front_share]
        all_days = [d for d in all_days if d not in set(dropped)]
        sys.stderr.write(f"[reduce] dropped {len(dropped)} days with front share < {min_front_share}\n")
    cut = int(len(all_days) * 0.6)
    seg_days = {"ALL": set(all_days), "IS": set(all_days[:cut]), "OOS": set(all_days[cut:])}
    for y in sorted({d[:4] for d in all_days}):
        seg_days[f"Y{y}"] = {d for d in all_days if d[:4] == y}
    seg_out = {k: np.zeros(OUT_SHAPE) for k in seg_days}
    seg_pos = {k: np.zeros(POS_SHAPE) for k in seg_days}
    seg_plc = {k: np.zeros(PLC_SHAPE) for k in seg_days}
    seg_reach = {k: np.zeros(REACH_SHAPE) for k in seg_days}
    reach_all = np.zeros(REACH_SHAPE)
    per_day_small = []
    for day in all_days:
        out, reach, posa, plc = store[day]
        for k, ds in seg_days.items():
            if day in ds:
                seg_out[k] += out; seg_pos[k] += posa; seg_plc[k] += plc
                seg_reach[k] += reach
        reach_all += reach
        per_day_small.append((day, out[..., [S_N, S_FAV]].astype(np.float64)))
    payload = summarise(per_day_small, diags, seg_days, seg_out, seg_pos, seg_plc, reach_all,
                        cut, seg_reach)
    outp = Path(out_path)
    assert not str(outp.resolve()).startswith(str(TICK_DIR.resolve())), "輸出不可與輸入同路徑"
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"wrote {outp}")


def stratified_diff_placed(arr, rch, bi, arm, ctrl, hi_, dlo, dhi):
    """每「掛出去一張單」的期望毛額（點）：未成交記 0，避開 reach 條件化造成的選擇問題。"""
    A = pick_full(arr, bi, arm); C = pick_full(arr, bi, ctrl)
    RA = rch[:, :, :, lab_idx(bi, arm), :].sum(axis=3)
    RC = rch[:, :, :, lab_idx(bi, ctrl), :].sum(axis=3)
    num = den = oa = oc = sa = sc = 0.0
    for s_ in range(2):
        for e_ in range(2):
            for di in range(N_D):
                d = DMIN + di
                if not (dlo <= d <= dhi):
                    continue
                na, nc = RA[s_, e_, di, 0], RC[s_, e_, di, 0]
                if na < 1 or nc < 1:
                    continue
                fa = A[s_, e_, di, hi_, S_FAV]
                fc = C[s_, e_, di, hi_, S_FAV]
                w = na * nc / (na + nc)
                num += w * (fa / na - fc / nc)
                den += w
                oa += na; oc += nc; sa += fa; sc += fc
    if den <= 0:
        return None
    return {"diff_pts_per_placed_order": float(num / den),
            "n_placed_arm": int(oa), "n_placed_ctrl": int(oc),
            "mean_arm_pts_per_placed": float(sa / oa) if oa else None,
            "mean_ctrl_pts_per_placed": float(sc / oc) if oc else None}


def summarise(per_day_small, diags, seg_days, seg_out, seg_pos, seg_plc, reach_all, cut,
              seg_reach=None):
    days = [d for d, _ in per_day_small]
    A = seg_out["ALL"]
    years = sorted({d[:4] for d in days})

    tot_n = A[..., S_N].sum(axis=3)
    tot_f = A[..., S_FAV].sum(axis=3)
    with np.errstate(invalid="ignore", divide="ignore"):
        strat_mean = np.where(tot_n > 0, tot_f / np.maximum(tot_n, 1e-9), np.nan)

    dist_bands = {"d3_6": (3, 6), "d6_12": (6, 12), "d12_24": (12, 24), "d24_45": (24, 45),
                  "HANG_12_33": (HANG_LO, HANG_HI)}

    snaps = sum(v["snaps"] for v in diags.values())
    out: dict = {
        "meta": {
            "script": "scripts/research/vp_g1b_node_touch.py",
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "tick_source": str(TICK_DIR),
            "instrument": "TX (大台) tick；價格路徑等同 TMF，成交量為 proxy",
            "n_trading_days": len(days),
            "date_range": [days[0], days[-1]] if days else None,
            "params": {"lookback_s": W_LOOKBACK, "grid_s": GRID, "reach_window_s": W_REACH,
                       "stale_lag_s": STALE_LAG, "horizons_s": list(HORIZONS),
                       "K_main_pts": K_MAIN, "K_alt_pts": K_ALT, "d_range": [DMIN, DMAX],
                       "bands": list(BANDS), "hang_band": [HANG_LO, HANG_HI],
                       "contract_rule": "ex-ante third-Wednesday front month; "
                                        "night = prev trading day 15:00 -> 05:00"},
            "is_oos_split": {"IS": [days[0], days[cut - 1]] if cut > 0 and len(days) > cut else None,
                             "OOS": [days[cut], days[-1]] if len(days) > cut else None},
            "cluster_unit": "trading day",
        },
        "diagnostics": {
            "snapshots": int(snaps),
            "snapshots_with_stale_placebo": int(sum(v["snaps_with_stale"] for v in diags.values())),
            "day_session_ticks": int(sum(v["day_ticks"] for v in diags.values())),
            "night_session_ticks": int(sum(v["night_ticks"] for v in diags.values())),
            "sessions_with_data": int(sum(v["sessions"] for v in diags.values())),
            "front_contract_tick_share_p10_p50_p90": [
                float(np.percentile([v["con_share"] for v in diags.values()], q)) for q in (10, 50, 90)],
            "front_contract_tick_share_min": float(min(v["con_share"] for v in diags.values())),
            "n_days_front_share_below_0p5": int(sum(1 for v in diags.values() if v["con_share"] < 0.5)),
            "mean_distinct_price_levels_per_10min_window": float(
                sum(v["sum_levels"] for v in diags.values()) / max(snaps, 1)),
            "mean_10min_price_range_pts": float(
                sum(v["sum_range"] for v in diags.values()) / max(snaps, 1)),
            "by_year": {
                y: {
                    "n_days": sum(1 for d in days if d[:4] == y),
                    "median_index_level": float(np.nanmedian(
                        [diags[d]["median_px"] for d in days if d[:4] == y])),
                    "mean_10min_range_pts": float(
                        sum(diags[d]["sum_range"] for d in days if d[:4] == y)
                        / max(sum(diags[d]["snaps"] for d in days if d[:4] == y), 1)),
                    "snapshots": int(sum(diags[d]["snaps"] for d in days if d[:4] == y)),
                }
                for y in sorted({d[:4] for d in days})
            },
        },
        "coverage": {}, "reach_rate": {}, "main": {}, "by_segment": {},
        "class_mix": {}, "position_matched": {}, "stale_node_placebo": {}, "splits": {},
    }

    keep_hang = [i for i in range(N_D) if HANG_LO <= DMIN + i <= HANG_HI]
    for bi, b in enumerate(BANDS):
        tot_off = reach_all[:, :, :, bi * N_CLS:(bi + 1) * N_CLS, 0].sum()
        for nm in ("n3", "n5", "n10", "mid", "ord", "unt"):
            idxs = lab_idx(bi, nm)
            offered = float(reach_all[:, :, :, idxs, 0].sum())
            reached = float(reach_all[:, :, :, idxs, 1].sum())
            sumt = float(reach_all[:, :, :, idxs, 2].sum())
            out["coverage"][f"band{b}_{nm}"] = {
                "offered_level_slots": int(offered),
                "share_of_slots": float(offered / tot_off) if tot_off else None}
            out["reach_rate"][f"band{b}_{nm}"] = {
                "offered": int(offered), "reached": int(reached),
                "reach_rate": float(reached / offered) if offered else None,
                "mean_seconds_to_reach": float(sumt / reached) if reached else None}
            offh = float(reach_all[:, :, keep_hang][:, :, :, idxs, 0].sum())
            rehh = float(reach_all[:, :, keep_hang][:, :, :, idxs, 1].sum())
            out["reach_rate"][f"HANG12_33_band{b}_{nm}"] = {
                "offered": int(offh), "reached": int(rehh),
                "reach_rate": float(rehh / offh) if offh else None}

    for bi, b in enumerate(BANDS):
        for arm in ("n3", "n5", "n10"):
            for ctrl in ("ord", "unt"):
                for hi_, h in enumerate(HORIZONS):
                    for bn, (dlo, dhi) in dist_bands.items():
                        st = stratified_diff(A, bi, arm, ctrl, hi_, dlo, dhi)
                        if st is None or st["n_arm"] < 200 or st["n_ctrl"] < 200:
                            continue
                        rows = day_diff_series(per_day_small, strat_mean, bi, arm, ctrl, hi_, dlo, dhi)
                        bs = boot_ci([r[1] for r in rows], [r[2] for r in rows]) if rows else None
                        out["main"][f"band{b}|{arm}_vs_{ctrl}|h{h}|{bn}"] = {
                            "pooled": st, "day_cluster_bootstrap": bs}

    for bi, b in enumerate(BANDS):
        for arm in ("n3", "n5"):
            for ctrl in ("ord", "unt"):
                for bn, (dlo, dhi) in dist_bands.items():
                    segd = {}
                    for sname in ["IS", "OOS"] + [f"Y{y}" for y in years]:
                        st = stratified_diff(seg_out[sname], bi, arm, ctrl, 1, dlo, dhi)
                        if st is None or st["n_arm"] < 100 or st["n_ctrl"] < 100:
                            continue
                        segd[sname] = st
                    if segd:
                        out["by_segment"][f"band{b}|{arm}_vs_{ctrl}|h300|{bn}"] = segd

    for bi, b in enumerate(BANDS):
        for nm in ("n3", "n5", "n10", "ord", "unt"):
            for hi_, h in enumerate(HORIZONS):
                for bn, (dlo, dhi) in dist_bands.items():
                    cm = class_mix(A, bi, nm, hi_, dlo, dhi)
                    if cm is None or cm["n"] < 200:
                        continue
                    out["class_mix"][f"band{b}|{nm}|h{h}|{bn}"] = cm

    for bi, b in enumerate(BANDS):
        for arm in ("n3", "n5"):
            for ctrl in ("ord", "unt"):
                for bn, (dlo, dhi) in dist_bands.items():
                    st = stratified_diff_pos(seg_pos["ALL"], bi, arm, ctrl, dlo, dhi)
                    if st is None or st["n_arm"] < 200 or st["n_ctrl"] < 200:
                        continue
                    segd = {}
                    for sname in ["IS", "OOS"]:
                        s2 = stratified_diff_pos(seg_pos[sname], bi, arm, ctrl, dlo, dhi)
                        if s2:
                            segd[sname] = s2
                    out["position_matched"][f"band{b}|{arm}_vs_{ctrl}|h300|{bn}"] = {
                        "pooled": st, "segments": segd}

    for bi, b in enumerate(BANDS):
        for arm in ("n3", "n5"):
            for hi_, h in enumerate(HORIZONS):
                for bn, (dlo, dhi) in dist_bands.items():
                    st = stratified_diff_small(seg_plc["ALL"], bi, arm, "ord", hi_, dlo, dhi)
                    if st is None or st["n_arm"] < 200 or st["n_ctrl"] < 200:
                        continue
                    out["stale_node_placebo"][f"band{b}|{arm}_vs_ord|h{h}|{bn}"] = st

    for bi, b in enumerate(BANDS):
        for sess_i, sess_nm in ((0, "day"), (1, "night")):
            st = stratified_diff(A, bi, "n3", "ord", 1, HANG_LO, HANG_HI, sess=sess_i)
            if st:
                out["splits"][f"band{b}|h300|HANG|sess_{sess_nm}"] = st
        for side_i, side_nm in ((0, "below_long"), (1, "above_short")):
            st = stratified_diff(A, bi, "n3", "ord", 1, HANG_LO, HANG_HI, side=side_i)
            if st:
                out["splits"][f"band{b}|h300|HANG|side_{side_nm}"] = st

    if seg_reach is not None:
        po: dict = {}
        for bi, b in enumerate(BANDS):
            for arm in ("n3", "n5"):
                for ctrl in ("ord", "unt"):
                    for bn, (dlo, dhi) in dist_bands.items():
                        st = stratified_diff_placed(seg_out["ALL"], seg_reach["ALL"], bi, arm,
                                                    ctrl, 1, dlo, dhi)
                        if st is None or st["n_placed_arm"] < 200:
                            continue
                        segd = {}
                        for sname in ["IS", "OOS"] + [f"Y{y}" for y in years]:
                            s2 = stratified_diff_placed(seg_out[sname], seg_reach[sname], bi, arm,
                                                        ctrl, 1, dlo, dhi)
                            if s2 and s2["n_placed_arm"] >= 100:
                                segd[sname] = s2
                        po[f"band{b}|{arm}_vs_{ctrl}|h300|{bn}"] = {"pooled": st, "segments": segd}
        out["per_placed_order"] = po

    out["verdict"] = build_verdict(out)
    return out


def _year_table(out, key):
    seg = out["by_segment"].get(key) or {}
    yr = out["diagnostics"]["by_year"]
    rows = {}
    for k, v in seg.items():
        if not k.startswith("Y"):
            continue
        y = k[1:]
        rng = yr.get(y, {}).get("mean_10min_range_pts")
        rows[y] = {"diff_pts": v["diff_pts"], "n_arm": v["n_arm"],
                   "mean_10min_range_pts": rng,
                   "diff_as_share_of_range": (v["diff_pts"] / rng) if rng else None}
    return rows


def build_verdict(out):
    """所有數字由 out 內容重算（硬規則 #5：禁止硬編碼）。"""
    cost_line = 4.79
    gross_per_trade = 2.86
    rows = []
    for k, m in out["main"].items():
        if "|h300|HANG_12_33" not in k or "_vs_ord" not in k:
            continue
        bs = m.get("day_cluster_bootstrap") or {}
        pos_key = k.replace("_vs_ord|h300", "_vs_ord|h300")
        pm = out["position_matched"].get(k)
        plc = out["stale_node_placebo"].get(k)
        rows.append({
            "key": k, "diff_pts": m["pooled"]["diff_pts"], "n_arm": m["pooled"]["n_arm"],
            "n_ctrl": m["pooled"]["n_ctrl"],
            "boot_est_pts": bs.get("est_pts"), "boot_ci95": bs.get("ci95"),
            "p_boot": bs.get("p_boot"), "t_day_cluster": bs.get("t_day_cluster"),
            "share_days_positive": bs.get("share_days_positive"),
            "position_matched_diff_pts": (pm or {}).get("pooled", {}).get("diff_pts"),
            "stale_placebo_diff_pts": (plc or {}).get("diff_pts"),
            "by_year": _year_table(out, k),
            "per_placed_order_diff_pts": (out.get("per_placed_order", {}).get(k, {})
                                          .get("pooled", {}).get("diff_pts_per_placed_order")),
            "share_of_effect_from_top5pct_days": bs.get("share_of_effect_from_top5pct_days"),
        })
    rows.sort(key=lambda r: -abs(r["diff_pts"]))
    sig = [r for r in rows if r["p_boot"] is not None and r["p_boot"] < 0.05]
    consistent = []
    for r in sig:
        seg = out["by_segment"].get(r["key"])
        if not seg:
            continue
        vals = [v["diff_pts"] for kk, v in seg.items() if kk in ("IS", "OOS")]
        yrs = [v["diff_pts"] for kk, v in seg.items() if kk.startswith("Y")]
        same_sign_isoos = len(vals) == 2 and (vals[0] > 0) == (vals[1] > 0)
        yr_share = (sum(1 for v in yrs if (v > 0) == (r["diff_pts"] > 0)) / len(yrs)) if yrs else None
        pm_same = (r["position_matched_diff_pts"] is not None
                   and (r["position_matched_diff_pts"] > 0) == (r["diff_pts"] > 0))
        consistent.append({"key": r["key"], "is_oos_same_sign": same_sign_isoos,
                           "year_sign_agreement": yr_share,
                           "survives_position_match": pm_same,
                           "position_matched_diff_pts": r["position_matched_diff_pts"],
                           "stale_placebo_diff_pts": r["stale_placebo_diff_pts"]})
    best = rows[0] if rows else None
    primary_key = "band1|n3_vs_ord|h300|HANG_12_33"
    primary = next((r for r in rows if r["key"] == primary_key), None)
    all_p = [(k, (v.get("day_cluster_bootstrap") or {}).get("p_boot"))
             for k, v in out["main"].items()]
    all_p = [(k, p) for k, p in all_p if p is not None]
    n_all = len(all_p)
    n_all_sig = sum(1 for _, p in all_p if p < 0.05)
    return {
        "primary_prespecified_config": primary_key,
        "primary": primary,
        "multiple_testing": {
            "n_main_configs_with_bootstrap": n_all,
            "n_p_lt_0p05": n_all_sig,
            "expected_under_null_at_5pct": round(0.05 * n_all, 1),
            "note": "configs 高度重疊（同一批事件的不同切法），這只是粗略對照，不是嚴格 FDR",
        },
        "cost_line_tmf_pts": cost_line, "live_gross_per_trade_pts": gross_per_trade,
        "n_configs_hang_h300_node_vs_ord": len(rows),
        "n_configs_p_boot_lt_0p05": len(sig),
        "largest_abs_effect": best,
        "largest_abs_effect_share_of_cost": (abs(best["diff_pts"]) / cost_line) if best else None,
        "significant_configs_consistency": consistent,
        "all_configs_hang_h300": rows,
    }


if __name__ == "__main__":
    main()
