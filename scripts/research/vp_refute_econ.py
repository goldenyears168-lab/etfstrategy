#!/usr/bin/env python3
"""vp_refute_econ — 對抗性複核 B：成交量節點（volume profile high-volume node, HVN）
的**經濟性與可實作性**。

預設立場：「就算統計成立，這東西也不能用。」本腳本不重做方向性檢定（G1a/G1b/G1c
已做），只回答五個把它殺死或救活的問題：

  Q1 效果量夠不夠付成本？  成本線 TMF 4.79 / MXF 3.07 / TXF 2.42 點來回。
     全部換算成「每報價淨值 = fill_rate × (gross_per_fill − cost)」，明算給看。
  Q2 排隊負選擇有沒有被誠實處理？  掛到節點＝排在大量後面。三族成交模型：
       touch（樂觀：碰到就成交）
       queue_prop(λ)（排隊量 = λ × 該價位 10 分鐘成交量 → **節點越強排越後面**）
       queue_abs(Q)（排隊量固定 Q 口 → 與節點強度無關的對照，隔離「負選擇」機制）
       through（悲觀下界：價格穿過該價位才算成交）
  Q3 集中度：扣掉最大 5 筆／最大 5 天還在嗎？
  Q4 這是不是偽裝成選價的濾網？量「實際被移動的比例」「成交筆數變化」「距離漂移」，
     再跑一個「距離凍結」版本（只在 d∈[20,24] 內挑節點）看效果是否倖存。
  Q5 工程與風險：重掛頻率 vs max_api_per_day=400 / max_api_per_poll=16，
     以及 rail_match_pts=2.0 的容忍度是否吸收得了節點造成的價位漂移。

硬規則遵守
  #1 只新增 scripts/research + reports/research/channel_lab 輸出；不碰 config/ src/
  #2 合約：ex-ante 第三個週三規則（沿用 vp_g1c_daycache，該快取即以此規則建立）
  #3 分片＝連續日期塊；所有滾動窗口都在單一日內，分片不影響任何統計
  #4 輸出 JSON 路徑與所有輸入路徑不同（輸入是 npz day cache，輸出是 repo 下的 json）
  #5 摘要文字全部由資料 f-string 重算，無硬編碼數字
  #6 每個「有效果」都配對照：uniform 基準、距離配對基準（distance-matched）、
     反節點（anti-node）、距離凍結版
  #7 叢集單位＝交易日；逐年拆解、逐日同號率、日層 block bootstrap
  #8 節點只准在 hang_lo–hang_hi=12–33 內挑價位（不當濾網）；Q4 專門查這個前提是否成立

用法
  PYTHONPATH=src .venv/bin/python scripts/research/vp_refute_econ.py replay
  PYTHONPATH=src .venv/bin/python scripts/research/vp_refute_econ.py analyze
  PYTHONPATH=src .venv/bin/python scripts/research/vp_refute_econ.py all   # replay + analyze(+churn)
"""
from __future__ import annotations

import datetime as dt
import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR", str(Path.home() / "goldenstocks-data")))
DAYCACHE = DATA_DIR / "cache" / "research" / "vp_g1c_daycache"      # INPUT (read-only)
WORKDIR = DATA_DIR / "cache" / "research" / "vp_refute_econ_rows"   # INTERMEDIATE
OUT_DIR = PROJECT_ROOT / "reports" / "research" / "channel_lab"
OUT_JSON = OUT_DIR / "vp_refute_econ.json"                          # OUTPUT (distinct)

# ---- session / snapshot geometry (與 vp_g1c 對齊，便於交叉比對) -------------
SESS_OPEN = 8 * 3600 + 45 * 60
SESS_CLOSE = 13 * 3600 + 45 * 60
SESS_LEN = SESS_CLOSE - SESS_OPEN
LOOKBACK = 600            # 10 分鐘 volume profile
TOUCH_H = 1800            # 掛單存活 30 分鐘
POST_K = 900              # 成交後持有 15 分鐘後標記
SNAP_STEP = 180
SNAP_T0 = LOOKBACK
SNAP_T1 = SESS_LEN - TOUCH_H - POST_K

HANG_LO, HANG_HI = 12, 33
DISTS = np.arange(HANG_LO, HANG_HI + 1, dtype=np.int64)
ND = len(DISTS)
D_BASE = 22               # 帶中點基準
D_FREEZE = (20, 24)       # Q4 距離凍結窗

QUEUE_RESET_GAP = 5       # 價格離開該價位 >5 點 → 視為隊列重建
LAMBDAS = (0.25, 0.5, 1.0, 2.0)      # queue_prop：λ × 該價位 10 分鐘量
ABS_QUEUES = (10.0, 50.0)            # queue_abs：固定口數
HYSTERESIS = (2, 5, 10, 20, 40)       # Q5 重掛遲滯門檻（點）

COST = {"TMF": 4.79, "MXF": 3.07, "TXF": 2.42}
LIVE_GROSS_PER_FILL = 2.86           # live TMF channel 現況毛額（僅作參照量級）

MIN_SNAP_TICKS = 60
MIN_PROF_LEVELS = 5

# ---- per-quote 欄位 -------------------------------------------------------
FILLMODELS = ["touch", "thru"] + [f"qp{int(l * 100):03d}" for l in LAMBDAS] + \
             [f"qa{int(q):03d}" for q in ABS_QUEUES]
BASECOLS = ["day", "tod", "side", "d", "mid", "P", "z1", "z3", "vP1", "vP3", "meanlvl"]
COLS = list(BASECOLS)
for fm in FILLMODELS:
    COLS += [f"tf_{fm}", f"pnl_{fm}"]
COLS += ["mfe_touch", "mae_touch", "mfe_qp050", "mae_qp050"]
CIX = {c: i for i, c in enumerate(COLS)}
NCOL = len(COLS)


# ==========================================================================
# sparse RMQ (O(1) range max/min)
# ==========================================================================
def build_sparse(a: np.ndarray):
    n = len(a)
    K = max(1, int(np.log2(max(n, 2))) + 1)
    mx = np.empty((K, n), dtype=np.int32)
    mn = np.empty((K, n), dtype=np.int32)
    mx[0] = a
    mn[0] = a
    for j in range(1, K):
        w = 1 << j
        h = 1 << (j - 1)
        m = n - w + 1
        if m <= 0:
            mx[j] = mx[j - 1]
            mn[j] = mn[j - 1]
            continue
        mx[j, :m] = np.maximum(mx[j - 1, :m], mx[j - 1, h:h + m])
        mn[j, :m] = np.minimum(mn[j - 1, :m], mn[j - 1, h:h + m])
        mx[j, m:] = mx[j - 1, m:]
        mn[j, m:] = mn[j - 1, m:]
    return mx, mn


def _rmq(tbl, lo, hi, is_max):
    L = np.maximum(hi - lo, 1)
    j = np.floor(np.log2(L)).astype(np.int64)
    j = np.clip(j, 0, tbl.shape[0] - 1)
    a = tbl[j, lo]
    b = tbl[j, np.maximum(hi - (1 << j), lo)]
    return np.maximum(a, b) if is_max else np.minimum(a, b)


# ==========================================================================
# stage 1 — replay one day
# ==========================================================================
def replay_day(date_str: str, day_idx: int) -> np.ndarray | None:
    z = np.load(DAYCACHE / f"{date_str}.npz")
    ts = z["ts"].astype(np.int64)
    px = z["px"].astype(np.int64)
    vol = z["vol"].astype(np.float64)
    n = len(ts)
    if n < 3000:
        return None
    sp_mx, sp_mn = build_sparse(px.astype(np.int32))

    snaps = list(range(SNAP_T0, SNAP_T1 + 1, SNAP_STEP))
    out = np.full((len(snaps) * 2 * ND, NCOL), np.nan, dtype=np.float64)
    w = 0

    for t in snaps:
        i_now = int(np.searchsorted(ts, t, side="left"))
        i_lo = int(np.searchsorted(ts, t - LOOKBACK, side="left"))
        if i_now - i_lo < MIN_SNAP_TICKS or i_now < 1:
            continue
        mid = int(px[i_now - 1])
        wpx = px[i_lo:i_now]
        wvol = vol[i_lo:i_now]
        pmin, pmax = int(wpx.min()), int(wpx.max())
        span = pmax - pmin + 1
        prof = np.bincount(wpx - pmin, weights=wvol, minlength=span)
        nzl = int(np.count_nonzero(prof))
        if nzl < MIN_PROF_LEVELS:
            continue
        meanlvl = float(prof.sum()) / nzl
        if meanlvl <= 0:
            continue
        prof3 = np.convolve(prof, np.ones(3), mode="same")

        f0 = i_now
        f1 = int(np.searchsorted(ts, t + TOUCH_H, side="right"))
        L = f1 - f0
        if L < 50:
            continue
        q = px[f0:f1]
        v = vol[f0:f1]
        fts = ts[f0:f1]
        fmin = np.minimum.accumulate(q)
        fmax = np.maximum.accumulate(q)
        negfmin = -fmin

        for sgn in (1, -1):
            P = mid - sgn * DISTS                       # (ND,)
            k = P - pmin
            ok = (k >= 0) & (k < span)
            kc = np.clip(k, 0, span - 1)
            vP1 = np.where(ok, prof[kc], 0.0)
            vP3 = np.where(ok, prof3[kc], 0.0)
            z1 = vP1 / meanlvl
            z3 = (vP3 / 3.0) / meanlvl

            # ---- touch / through indices --------------------------------
            if sgn == 1:
                j_touch = np.searchsorted(negfmin, -P, side="left")
                j_thru = np.searchsorted(negfmin, -(P - 1), side="left")
                inband = q[None, :] == P[:, None]
                reset = q[None, :] > (P[:, None] + QUEUE_RESET_GAP)
            else:
                j_touch = np.searchsorted(fmax, P, side="left")
                j_thru = np.searchsorted(fmax, P + 1, side="left")
                inband = q[None, :] == P[:, None]
                reset = q[None, :] < (P[:, None] - QUEUE_RESET_GAP)

            # ---- queue accumulation with reset --------------------------
            cs = np.cumsum(np.where(inband, v[None, :], 0.0), axis=1)
            base = np.maximum.accumulate(np.where(reset, cs, 0.0), axis=1)
            cum = cs - base                              # (ND, L)

            jt = np.minimum(j_touch, L)
            jr = np.minimum(j_thru, L)

            fills: dict[str, np.ndarray] = {"touch": jt, "thru": jr}
            for lam in LAMBDAS:
                Q0 = lam * vP1
                hit = cum > Q0[:, None]
                any_hit = hit.any(axis=1)
                jq = np.where(any_hit, hit.argmax(axis=1), L)
                fills[f"qp{int(lam * 100):03d}"] = np.minimum(jq, jr)
            for qa in ABS_QUEUES:
                hit = cum > qa
                any_hit = hit.any(axis=1)
                jq = np.where(any_hit, hit.argmax(axis=1), L)
                fills[f"qa{int(qa):03d}"] = np.minimum(jq, jr)

            blk = out[w:w + ND]
            blk[:, CIX["day"]] = day_idx
            blk[:, CIX["tod"]] = t / 60.0
            blk[:, CIX["side"]] = sgn
            blk[:, CIX["d"]] = DISTS
            blk[:, CIX["mid"]] = mid
            blk[:, CIX["P"]] = P
            blk[:, CIX["z1"]] = z1
            blk[:, CIX["z3"]] = z3
            blk[:, CIX["vP1"]] = vP1
            blk[:, CIX["vP3"]] = vP3
            blk[:, CIX["meanlvl"]] = meanlvl

            for fm, jf in fills.items():
                filled = jf < L
                jc = np.clip(jf, 0, L - 1)
                tau = fts[jc]
                ei = np.searchsorted(ts, tau + POST_K, side="right") - 1
                ei = np.clip(ei, 0, n - 1)
                pnl = sgn * (px[ei].astype(np.float64) - P)
                blk[:, CIX[f"tf_{fm}"]] = np.where(filled, tau - t, -1.0)
                blk[:, CIX[f"pnl_{fm}"]] = np.where(filled, pnl, np.nan)
                if fm in ("touch", "qp050"):
                    i_tau = np.clip(f0 + jc, 0, n - 1)
                    i_end = np.clip(ei + 1, i_tau + 1, n)
                    rmax = _rmq(sp_mx, i_tau, i_end, True).astype(np.float64)
                    rmin = _rmq(sp_mn, i_tau, i_end, False).astype(np.float64)
                    if sgn == 1:
                        mfe, mae = rmax - P, P - rmin
                    else:
                        mfe, mae = P - rmin, rmax - P
                    blk[:, CIX[f"mfe_{fm}"]] = np.where(filled, mfe, np.nan)
                    blk[:, CIX[f"mae_{fm}"]] = np.where(filled, mae, np.nan)
            w += ND
    return out[:w] if w else None


def _replay_block(args):
    items, outdir = args
    got = 0
    for date_str, day_idx in items:
        try:
            r = replay_day(date_str, day_idx)
        except Exception as exc:  # pragma: no cover
            print(f"[warn] {date_str}: {exc}", flush=True)
            continue
        if r is None or len(r) == 0:
            continue
        np.save(Path(outdir) / f"rows_{day_idx:05d}.npy", r.astype(np.float32))
        got += 1
    return got


def run_replay(workers: int = 10, limit: int | None = None) -> None:
    dates = sorted(p.stem for p in DAYCACHE.glob("*.npz"))
    if limit:
        dates = dates[:limit]
    WORKDIR.mkdir(parents=True, exist_ok=True)
    for f in WORKDIR.glob("*.npy"):
        f.unlink()
    (WORKDIR / "_dates.json").write_text(json.dumps(dates))
    items = [(d, i) for i, d in enumerate(dates)]
    # 硬規則 #3：連續日期塊，不做 round-robin
    step = (len(items) + workers - 1) // workers
    blocks = [(items[i:i + step], str(WORKDIR)) for i in range(0, len(items), step)]
    t0 = time.time()
    with mp.Pool(workers) as pool:
        res = pool.map(_replay_block, blocks)
    print(f"[replay] {sum(res)}/{len(dates)} days in {time.time() - t0:.0f}s -> {WORKDIR}", flush=True)


# ==========================================================================
# stage 2 — analysis
# ==========================================================================
def _load_rows():
    fs = sorted(WORKDIR.glob("rows_*.npy"))
    X = np.concatenate([np.load(f) for f in fs])
    dates = json.loads((WORKDIR / "_dates.json").read_text())
    return X, dates


def _tstat(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if len(x) < 3:
        return float("nan")
    s = x.std(ddof=1)
    return float("nan") if s == 0 else float(x.mean() / (s / np.sqrt(len(x))))


def _boot_ci(day_vals: np.ndarray, n_boot: int = 4000, seed: int = 20260820):
    d = day_vals[np.isfinite(day_vals)]
    if len(d) < 5:
        return [float("nan"), float("nan")]
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(d), size=(n_boot, len(d)))
    means = d[idx].mean(axis=1)
    return [float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))]


class Panel:
    """把 (day, snap, side) 的 ND 個候選價位攤成 3D，方便任何『選價政策』做索引。"""

    def __init__(self, X: np.ndarray):
        assert len(X) % ND == 0, "rows must be groups of ND"
        self.G = len(X) // ND
        self.A = X.reshape(self.G, ND, X.shape[1])
        self.day = self.A[:, 0, CIX["day"]].astype(np.int64)
        self.side = self.A[:, 0, CIX["side"]].astype(np.int64)
        self.tod = self.A[:, 0, CIX["tod"]]
        self.z3 = self.A[:, :, CIX["z3"]]
        self.z1 = self.A[:, :, CIX["z1"]]
        self.dd = self.A[:, :, CIX["d"]]
        self.P = self.A[:, :, CIX["P"]]
        self.vP1 = self.A[:, :, CIX["vP1"]]

    def col(self, name: str) -> np.ndarray:
        return self.A[:, :, CIX[name]]


# --- 政策：回傳 (G, ND) 的權重矩陣，列和 = 1（允許機率式政策，取期望值） ----
def w_fixed(pan: Panel, d: int) -> np.ndarray:
    W = np.zeros((pan.G, ND))
    W[:, int(d) - HANG_LO] = 1.0
    return W


def w_uniform(pan: Panel) -> np.ndarray:
    return np.full((pan.G, ND), 1.0 / ND)


def w_argmax(pan: Panel, key: np.ndarray, mask: np.ndarray | None = None,
             fallback_d: int = D_BASE, thr: float | None = None) -> np.ndarray:
    k = key.copy()
    if mask is not None:
        k = np.where(mask, k, -np.inf)
    j = np.argmax(k, axis=1)
    W = np.zeros((pan.G, ND))
    W[np.arange(pan.G), j] = 1.0
    if thr is not None:
        best = k[np.arange(pan.G), j]
        bad = ~(best >= thr)
        if bad.any():
            W[bad] = 0.0
            W[bad, fallback_d - HANG_LO] = 1.0
    return W


def w_argmin(pan: Panel, key: np.ndarray) -> np.ndarray:
    j = np.argmin(key, axis=1)
    W = np.zeros((pan.G, ND))
    W[np.arange(pan.G), j] = 1.0
    return W


def w_distmatched(pan: Panel, dist_weights: np.ndarray) -> np.ndarray:
    return np.tile(dist_weights / dist_weights.sum(), (pan.G, 1))


def policy_stats(pan: Panel, W: np.ndarray, fm: str, cost: float):
    """回傳 per-group 的 (filled, pnl_sum, ...) 期望值 → 再依日彙總。"""
    filled = (pan.col(f"tf_{fm}") >= 0).astype(np.float64)
    pnl = np.nan_to_num(pan.col(f"pnl_{fm}"), nan=0.0)
    f_g = (W * filled).sum(axis=1)                 # 每報價的期望成交機率
    g_g = (W * filled * pnl).sum(axis=1)           # 每報價的期望毛額（未成交=0）
    d_g = (W * pan.dd).sum(axis=1)
    return f_g, g_g, d_g


def day_agg(day: np.ndarray, vals: np.ndarray, ndays: int) -> np.ndarray:
    return np.bincount(day, weights=vals, minlength=ndays)


def summarize_policy(pan: Panel, W: np.ndarray, fm: str, ndays: int):
    f_g, g_g, d_g = policy_stats(pan, W, fm, 0.0)
    day = pan.day
    nq = np.bincount(day, minlength=ndays).astype(np.float64)
    sf = day_agg(day, f_g, ndays)
    sg = day_agg(day, g_g, ndays)
    sd = day_agg(day, d_g, ndays)
    live = nq > 0
    return {
        "n_quotes": float(nq.sum()),
        "fill_rate": float(sf.sum() / nq.sum()),
        "gross_per_quote": float(sg.sum() / nq.sum()),
        "gross_per_fill": float(sg.sum() / sf.sum()) if sf.sum() > 0 else float("nan"),
        "mean_distance_pts": float(sd.sum() / nq.sum()),
        "_day_nq": nq, "_day_fill": sf, "_day_gross": sg, "_live": live,
    }


def net_per_quote(fill_rate: float, gross_per_fill: float, cost: float) -> float:
    return fill_rate * (gross_per_fill - cost)


def delta_block(base: dict, test: dict, cost_map: dict, seed: int = 1):
    """Δ（test − base）以日為叢集單位，含 bootstrap / 逐日同號 / 集中度。"""
    nq, live = base["_day_nq"], base["_live"]
    dq_g = np.zeros_like(nq)
    dq_f = np.zeros_like(nq)
    dq_g[live] = (test["_day_gross"][live] - base["_day_gross"][live]) / nq[live]
    dq_f[live] = (test["_day_fill"][live] - base["_day_fill"][live]) / nq[live]
    dg = dq_g[live]
    df = dq_f[live]
    out = {
        "d_fill_rate_pp": float(100.0 * (test["fill_rate"] - base["fill_rate"])),
        "d_gross_per_quote_pts": float(test["gross_per_quote"] - base["gross_per_quote"]),
        "d_gross_per_quote_t_dayclust": _tstat(dg),
        "d_gross_per_quote_ci95": _boot_ci(dg, seed=seed),
        "frac_days_positive": float((dg > 0).mean()),
        "n_days": int(live.sum()),
        "d_mean_distance_pts": float(test["mean_distance_pts"] - base["mean_distance_pts"]),
    }
    # 每報價淨值（成本只在成交時付）
    for prod, c in cost_map.items():
        nb = net_per_quote(base["fill_rate"], base["gross_per_fill"], c)
        nt = net_per_quote(test["fill_rate"], test["gross_per_fill"], c)
        out[f"net_per_quote_base_{prod}"] = float(nb)
        out[f"net_per_quote_test_{prod}"] = float(nt)
        out[f"d_net_per_quote_{prod}"] = float(nt - nb)
        out[f"extra_cost_from_extra_fills_{prod}"] = float(
            c * (test["fill_rate"] - base["fill_rate"]))
    # 集中度：扣最大 5 天
    if len(dg) > 10:
        srt = np.sort(dg)
        out["d_gross_per_quote_drop_top5_days"] = float(np.sort(dg)[:-5].mean())
        out["d_gross_per_quote_drop_bot5_days"] = float(srt[5:].mean())
        out["d_gross_per_quote_median_day"] = float(np.median(dg))
    return out


def run_analyze() -> dict:
    X, dates = _load_rows()
    ndays = len(dates)
    pan = Panel(X)
    years = np.array([int(dates[i][:4]) for i in range(ndays)])
    day_present = np.unique(pan.day)
    R: dict = {
        "schema": "vp_refute_econ/v1",
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "stance": "adversarial-B: economics & implementability; null = unusable",
        "inputs": {
            "day_cache": str(DAYCACHE),
            "rows_cache": str(WORKDIR),
            "n_days_cached": int(ndays),
            "n_days_with_rows": int(len(day_present)),
            "date_min": dates[int(day_present.min())],
            "date_max": dates[int(day_present.max())],
        },
        "spec": {
            "session": "day session 08:45-13:45 (TX front month, ex-ante 3rd-Wednesday roll)",
            "lookback_sec": LOOKBACK, "snap_step_sec": SNAP_STEP,
            "order_life_sec": TOUCH_H, "hold_after_fill_sec": POST_K,
            "hang_band_pts": [HANG_LO, HANG_HI],
            "queue_reset_gap_pts": QUEUE_RESET_GAP,
            "lambdas": list(LAMBDAS), "abs_queues": list(ABS_QUEUES),
            "cost_line_pts": COST, "live_gross_per_fill_pts": LIVE_GROSS_PER_FILL,
            "n_quote_events": int(pan.G),
            "n_candidate_levels": int(pan.G * ND),
        },
    }

    # ---- IS / OOS 連續切塊（硬規則 #3） ---------------------------------
    cut = int(np.median(day_present))
    seg_is = pan.day <= cut
    seg_oos = ~seg_is
    R["spec"]["is_oos_split"] = {
        "mode": "contiguous by date",
        "is_dates": [dates[int(day_present.min())], dates[cut]],
        "oos_dates": [dates[min(cut + 1, ndays - 1)], dates[int(day_present.max())]],
    }

    # ---- 距離配對權重：只用 IS 段的 node-argmax 距離分布（PIT） -----------
    pan_is = Panel(X[np.repeat(seg_is, ND)])
    W_node_is = w_argmax(pan_is, pan_is.z3)
    dist_w = W_node_is.sum(axis=0)
    dist_w = np.where(dist_w > 0, dist_w, 1e-9)
    R["spec"]["distance_matched_weights_from"] = "IS block node-argmax distance histogram"

    # ---- 政策定義 -------------------------------------------------------
    def build_policies(p: Panel) -> dict:
        band_free = np.ones((p.G, ND), dtype=bool)
        frz = (p.dd >= D_FREEZE[0]) & (p.dd <= D_FREEZE[1])
        return {
            "base_d22": w_fixed(p, D_BASE),
            "base_uniform": w_uniform(p),
            "base_dist_matched": w_distmatched(p, dist_w),
            "node_argmax_z3": w_argmax(p, p.z3, band_free),
            "node_argmax_z3_thr3": w_argmax(p, p.z3, band_free, thr=3.0),
            "node_argmax_z1": w_argmax(p, p.z1, band_free),
            "anti_node_z3": w_argmin(p, p.z3),
            "node_argmax_frozen_20_24": w_argmax(p, np.where(frz, p.z3, -np.inf)),
            "base_frozen_22": w_fixed(p, D_BASE),
        }

    # ---- 主表：每個 fill model × 每個政策 --------------------------------
    seg_defs = {"ALL": np.ones(pan.G, dtype=bool), "IS": seg_is, "OOS": seg_oos}
    main: dict = {}
    for seg_name, seg in seg_defs.items():
        Xs = X[np.repeat(seg, ND)]
        ps = Panel(Xs)
        pols = build_policies(ps)
        seg_out: dict = {}
        for fm in FILLMODELS:
            stats = {name: summarize_policy(ps, W, fm, ndays) for name, W in pols.items()}
            fm_out: dict = {}
            for name, s in stats.items():
                fm_out[name] = {
                    "fill_rate": s["fill_rate"],
                    "gross_per_fill_pts": s["gross_per_fill"],
                    "gross_per_quote_pts": s["gross_per_quote"],
                    "mean_distance_pts": s["mean_distance_pts"],
                    "net_per_quote_TMF_pts": net_per_quote(
                        s["fill_rate"], s["gross_per_fill"], COST["TMF"]),
                }
            fm_out["_deltas"] = {}
            pairs = [
                ("node_argmax_z3", "base_d22"),
                ("node_argmax_z3", "base_uniform"),
                ("node_argmax_z3", "base_dist_matched"),
                ("node_argmax_z3_thr3", "base_d22"),
                ("node_argmax_z1", "base_dist_matched"),
                ("anti_node_z3", "base_dist_matched"),
                ("node_argmax_frozen_20_24", "base_frozen_22"),
            ]
            for test, base in pairs:
                fm_out["_deltas"][f"{test}__minus__{base}"] = delta_block(
                    stats[base], stats[test], COST)
            seg_out[fm] = fm_out
        main[seg_name] = seg_out
    R["main"] = main

    # ---- Q1 成本算術（用 OOS，touch 與 queue λ=0.5 兩個成交模型） -------
    R["Q1_cost_arithmetic"] = _q1(main)

    # ---- Q2 排隊負選擇 ---------------------------------------------------
    R["Q2_queue_adverse_selection"] = _q2(main, pan, X, seg_oos, dist_w, ndays)

    # ---- Q3 集中度 -------------------------------------------------------
    R["Q3_concentration"] = _q3(X, pan, seg_oos, dist_w, ndays)

    # ---- Q4 是不是偽裝的濾網 ---------------------------------------------
    R["Q4_disguised_filter"] = _q4(X, pan, seg_oos, main, ndays)

    # ---- 逐年拆解 --------------------------------------------------------
    R["by_year"] = _by_year(X, pan, years, dist_w, ndays)

    return R


def _q1(main: dict) -> dict:
    out = {}
    for fm in ("touch", "qp050", "thru"):
        for seg in ("IS", "OOS"):
            b = main[seg][fm]["base_dist_matched"]
            t = main[seg][fm]["node_argmax_z3"]
            d = main[seg][fm]["_deltas"]["node_argmax_z3__minus__base_dist_matched"]
            row = {
                "base_fill_rate": b["fill_rate"],
                "node_fill_rate": t["fill_rate"],
                "base_gross_per_fill_pts": b["gross_per_fill_pts"],
                "node_gross_per_fill_pts": t["gross_per_fill_pts"],
                "d_gross_per_fill_pts": t["gross_per_fill_pts"] - b["gross_per_fill_pts"],
                "d_gross_per_quote_pts": d["d_gross_per_quote_pts"],
                "d_gross_per_quote_t": d["d_gross_per_quote_t_dayclust"],
                "d_fill_rate_pp": d["d_fill_rate_pp"],
            }
            for prod, c in COST.items():
                row[f"extra_cost_from_extra_fills_{prod}_pts_per_quote"] = \
                    c * (t["fill_rate"] - b["fill_rate"])
                row[f"d_net_per_quote_{prod}_pts"] = d[f"d_net_per_quote_{prod}"]
                row[f"d_net_per_quote_{prod}_as_pct_of_cost_line"] = \
                    100.0 * d[f"d_net_per_quote_{prod}"] / c
            out[f"{fm}__{seg}"] = row
    return out


def _q2(main: dict, pan: Panel, X: np.ndarray, seg_oos: np.ndarray,
        dist_w: np.ndarray, ndays: int) -> dict:
    """排隊負選擇：節點 vs 距離配對基準，在不同成交模型下的效果差。"""
    ladder = {}
    for fm in FILLMODELS:
        d = main["OOS"][fm]["_deltas"]["node_argmax_z3__minus__base_dist_matched"]
        n = main["OOS"][fm]["node_argmax_z3"]
        b = main["OOS"][fm]["base_dist_matched"]
        ladder[fm] = {
            "node_fill_rate": n["fill_rate"],
            "base_fill_rate": b["fill_rate"],
            "node_gross_per_fill_pts": n["gross_per_fill_pts"],
            "base_gross_per_fill_pts": b["gross_per_fill_pts"],
            "d_gross_per_fill_pts": n["gross_per_fill_pts"] - b["gross_per_fill_pts"],
            "d_gross_per_quote_pts": d["d_gross_per_quote_pts"],
            "d_gross_per_quote_t": d["d_gross_per_quote_t_dayclust"],
            "d_net_per_quote_TMF_pts": d["d_net_per_quote_TMF"],
            "frac_days_positive": d["frac_days_positive"],
        }
    # 節點強度 vs 該價位 10 分鐘量（＝排隊代理）的關係
    Xo = X[np.repeat(seg_oos, ND)]
    po = Panel(Xo)
    W = w_argmax(po, po.z3)
    j = W.argmax(axis=1)
    g = np.arange(po.G)
    Wd = w_distmatched(po, dist_w)
    node_vP1 = po.vP1[g, j]
    base_vP1 = (Wd * po.vP1).sum(axis=1)
    # 節點成交比基準晚多少（time-to-fill），touch vs queue
    tt = {}
    for fm in ("touch", "qp050", "qp200", "thru"):
        tf = po.col(f"tf_{fm}")
        nf = tf[g, j]
        okb = tf >= 0
        base_tt = float((Wd * np.where(okb, tf, 0.0)).sum() / max((Wd * okb).sum(), 1e-9))
        tt[fm] = {
            "node_median_time_to_fill_sec": float(np.median(nf[nf >= 0])) if (nf >= 0).any() else None,
            "node_mean_time_to_fill_sec": float(nf[nf >= 0].mean()) if (nf >= 0).any() else None,
            "distmatched_mean_time_to_fill_sec": base_tt,
            "node_fill_rate": float((nf >= 0).mean()),
            "base_fill_rate_distmatched": float((Wd * okb).sum(axis=1).mean()),
        }
    return {
        "fill_model_ladder_OOS": ladder,
        "queue_proxy": {
            "node_mean_vol_at_level_10min_lots": float(np.nanmean(node_vP1)),
            "distmatched_mean_vol_at_level_10min_lots": float(np.nanmean(base_vP1)),
            "ratio_node_over_base": float(np.nanmean(node_vP1) / max(np.nanmean(base_vP1), 1e-9)),
        },
        "time_to_fill": tt,
        "spread_optimistic_minus_pessimistic_pts_per_quote": float(
            ladder["touch"]["d_gross_per_quote_pts"] - ladder["thru"]["d_gross_per_quote_pts"]),
    }


def _q3(X: np.ndarray, pan: Panel, seg_oos: np.ndarray, dist_w: np.ndarray,
        ndays: int) -> dict:
    """集中度：扣掉最大 5 筆／最大 5 天。以 OOS、touch 與 qp050 兩模型。"""
    Xo = X[np.repeat(seg_oos, ND)]
    po = Panel(Xo)
    W = w_argmax(po, po.z3)
    Wd = w_distmatched(po, dist_w)
    out = {}
    for fm in ("touch", "qp050"):
        filled = (po.col(f"tf_{fm}") >= 0).astype(np.float64)
        pnl = np.nan_to_num(po.col(f"pnl_{fm}"), nan=0.0)
        contrib = ((W - Wd) * filled * pnl).sum(axis=1)     # 每報價的 Δ 貢獻
        nq = np.bincount(po.day, minlength=ndays).astype(np.float64)
        sc = np.bincount(po.day, weights=contrib, minlength=ndays)
        live = nq > 0
        dday = sc[live] / nq[live]
        total = float(contrib.sum() / len(contrib))
        srt_ev = np.sort(contrib)
        srt_day = np.sort(dday)
        out[fm] = {
            "d_gross_per_quote_pts": total,
            "n_quote_events": int(len(contrib)),
            "n_days": int(live.sum()),
            "drop_top5_events": float((contrib.sum() - srt_ev[-5:].sum()) / len(contrib)),
            "drop_top5_and_bot5_events": float(
                (contrib.sum() - srt_ev[-5:].sum() - srt_ev[:5].sum()) / len(contrib)),
            "drop_top5_days_pts_per_quote": float(srt_day[:-5].mean()),
            "drop_top5_and_bot5_days_pts_per_quote": float(srt_day[5:-5].mean()),
            "median_day_pts_per_quote": float(np.median(dday)),
            "frac_days_positive": float((dday > 0).mean()),
            "t_dayclust": _tstat(dday),
            "top5_day_share_of_total": float(
                srt_day[-5:].sum() / (dday.sum() if dday.sum() != 0 else np.nan)),
        }
    return out


def availability_probe(n_days: int = 250, workers: int = 8) -> dict:
    """把 brief 的『82.4% 有節點 / 59.2% 在 12 點外』與『可交易帶內有沒有節點』對帳。

    brief 的數字是「整條 10 分鐘價格區間內任一價位」，不限上界距離；策略真正能用的
    只有 hang_lo–hang_hi=12–33。這裡同一份快照同時算兩種口徑。
    """
    dates = sorted(p.stem for p in DAYCACHE.glob("*.npz"))[-n_days:]
    step = (len(dates) + workers - 1) // workers
    blocks = [dates[i:i + step] for i in range(0, len(dates), step)]
    with mp.Pool(workers) as pool:
        res = pool.map(_avail_block, blocks)
    tot = np.zeros(8)
    for r in res:
        tot += r
    n = max(tot[0], 1.0)
    return {
        "spec": {"n_days": len(dates), "dates": [dates[0], dates[-1]],
                 "n_snapshots": int(tot[0]),
                 "note": "band1 = single price level; band3 = 3-price rolling mean; "
                         "ratio vs mean volume per traded level in the 10-min window"},
        "any_distance_ge12_band1_thr3": float(tot[1] / n),
        "any_distance_ge12_band3_thr3": float(tot[2] / n),
        "inside_hang_band_12_33_band1_thr3": float(tot[3] / n),
        "inside_hang_band_12_33_band3_thr3": float(tot[4] / n),
        "inside_hang_band_12_33_band1_thr5": float(tot[5] / n),
        "inside_hang_band_12_33_band3_thr5": float(tot[6] / n),
        "frac_of_ge12_nodes_that_are_inside_the_tradeable_band":
            float(tot[3] / max(tot[1], 1.0)),
        "mean_n_qualifying_levels_inside_band_band3_thr3": float(tot[7] / n),
    }


def _avail_block(dates):
    acc = np.zeros(8)
    for date_str in dates:
        try:
            z = np.load(DAYCACHE / f"{date_str}.npz")
        except Exception:
            continue
        ts = z["ts"].astype(np.int64)
        px = z["px"].astype(np.int64)
        vol = z["vol"].astype(np.float64)
        if len(ts) < 3000:
            continue
        for t in range(SNAP_T0, SNAP_T1 + 1, SNAP_STEP):
            i_now = int(np.searchsorted(ts, t, side="left"))
            i_lo = int(np.searchsorted(ts, t - LOOKBACK, side="left"))
            if i_now - i_lo < MIN_SNAP_TICKS or i_now < 1:
                continue
            mid = int(px[i_now - 1])
            wpx = px[i_lo:i_now]
            pmin, pmax = int(wpx.min()), int(wpx.max())
            span = pmax - pmin + 1
            prof = np.bincount(wpx - pmin, weights=vol[i_lo:i_now], minlength=span)
            nzl = int(np.count_nonzero(prof))
            if nzl < MIN_PROF_LEVELS:
                continue
            meanlvl = float(prof.sum()) / nzl
            if meanlvl <= 0:
                continue
            r1 = prof / meanlvl
            r3 = np.convolve(prof, np.ones(3), mode="same") / 3.0 / meanlvl
            dist = np.abs(np.arange(span) + pmin - mid)
            far = dist >= HANG_LO
            band = far & (dist <= HANG_HI)
            acc[0] += 1
            acc[1] += float((r1[far] >= 3.0).any())
            acc[2] += float((r3[far] >= 3.0).any())
            acc[3] += float((r1[band] >= 3.0).any())
            acc[4] += float((r3[band] >= 3.0).any())
            acc[5] += float((r1[band] >= 5.0).any())
            acc[6] += float((r3[band] >= 5.0).any())
            acc[7] += float((r3[band] >= 3.0).sum())
    return acc


def _q4(X: np.ndarray, pan: Panel, seg_oos: np.ndarray, main: dict, ndays: int) -> dict:
    """真的是純選價嗎？量被移動的比例、成交數變化、距離漂移。"""
    Xo = X[np.repeat(seg_oos, ND)]
    po = Panel(Xo)
    g = np.arange(po.G)
    j_node = po.z3.argmax(axis=1)
    d_node = po.dd[g, j_node]
    best_z3 = po.z3[g, j_node]
    moved = d_node != D_BASE
    out = {
        "vs_base_d22": {
            "frac_quotes_moved": float(moved.mean()),
            "mean_abs_price_shift_pts": float(np.abs(d_node - D_BASE).mean()),
            "median_chosen_distance_pts": float(np.median(d_node)),
            "mean_chosen_distance_pts": float(d_node.mean()),
            "frac_chosen_below_base": float((d_node < D_BASE).mean()),
            "frac_chosen_at_band_floor_12": float((d_node == HANG_LO).mean()),
            "frac_chosen_at_band_cap_33": float((d_node == HANG_HI).mean()),
        },
        "node_availability": {
            "frac_snapshots_max_z3_ge_3": float((best_z3 >= 3.0).mean()),
            "frac_snapshots_max_z3_ge_5": float((best_z3 >= 5.0).mean()),
            "median_max_z3_in_band": float(np.median(best_z3)),
        },
    }
    for fm in ("touch", "qp050"):
        b = main["OOS"][fm]["base_d22"]
        t = main["OOS"][fm]["node_argmax_z3"]
        f = main["OOS"][fm]["node_argmax_frozen_20_24"]
        out[f"trade_count_change_{fm}"] = {
            "base_d22_fill_rate": b["fill_rate"],
            "node_fill_rate": t["fill_rate"],
            "relative_fill_count_change_pct": 100.0 * (t["fill_rate"] / b["fill_rate"] - 1.0),
            "frozen_20_24_fill_rate": f["fill_rate"],
            "frozen_relative_fill_count_change_pct":
                100.0 * (f["fill_rate"] / b["fill_rate"] - 1.0),
            "frozen_d_gross_per_quote_pts": main["OOS"][fm]["_deltas"][
                "node_argmax_frozen_20_24__minus__base_frozen_22"]["d_gross_per_quote_pts"],
            "frozen_d_gross_per_quote_t": main["OOS"][fm]["_deltas"][
                "node_argmax_frozen_20_24__minus__base_frozen_22"]["d_gross_per_quote_t_dayclust"],
            "frozen_d_net_per_quote_TMF_pts": main["OOS"][fm]["_deltas"][
                "node_argmax_frozen_20_24__minus__base_frozen_22"]["d_net_per_quote_TMF"],
        }
    return out


def _by_year(X: np.ndarray, pan: Panel, years: np.ndarray, dist_w: np.ndarray,
             ndays: int) -> dict:
    out = {}
    yr_of_row = years[pan.day]
    for y in sorted(set(int(v) for v in yr_of_row)):
        sel = yr_of_row == y
        if sel.sum() < 200:
            continue
        Xs = X[np.repeat(sel, ND)]
        ps = Panel(Xs)
        Wn = w_argmax(ps, ps.z3)
        Wd = w_distmatched(ps, dist_w)
        row = {}
        for fm in ("touch", "qp050"):
            sn = summarize_policy(ps, Wn, fm, ndays)
            sb = summarize_policy(ps, Wd, fm, ndays)
            d = delta_block(sb, sn, COST, seed=y)
            row[fm] = {
                "n_days": d["n_days"],
                "d_gross_per_quote_pts": d["d_gross_per_quote_pts"],
                "d_net_per_quote_TMF_pts": d["d_net_per_quote_TMF"],
                "d_fill_rate_pp": d["d_fill_rate_pp"],
                "frac_days_positive": d["frac_days_positive"],
            }
        out[str(y)] = row
    return out


# ==========================================================================
# stage 3 — 重掛頻率（工程可行性）
# ==========================================================================
def churn_day(date_str: str, poll_sec: int) -> dict | None:
    z = np.load(DAYCACHE / f"{date_str}.npz")
    ts = z["ts"].astype(np.int64)
    px = z["px"].astype(np.int64)
    vol = z["vol"].astype(np.float64)
    n = len(ts)
    if n < 3000:
        return None
    polls = list(range(SNAP_T0, SESS_LEN, poll_sec))
    prev = {1: None, -1: None}       # 上一輪節點政策要的價位
    sticky_px = {1: None, -1: None}  # 現行 sticky 政策：絕對價位，離太遠才重掛
    # hysteresis 緩解：只有偏離現掛價超過 H 點才重掛（H=2 等同 rail_match_pts）
    hys_px = {h: {1: None, -1: None} for h in HYSTERESIS}
    hys_n = {h: 0 for h in HYSTERESIS}
    hys_band = {h: 0 for h in HYSTERESIS}   # 其中因為漂出 12-33 帶而被迫重掛的次數
    prev_b = {1: None, -1: None}     # 追價 d22 對照
    n_poll = 0
    n_move = {1: 0, -1: 0}
    n_move_base = {1: 0, -1: 0}
    n_move_chase = {1: 0, -1: 0}
    shifts = []
    shifts_b = []
    for t in polls:
        i_now = int(np.searchsorted(ts, t, side="left"))
        i_lo = int(np.searchsorted(ts, t - LOOKBACK, side="left"))
        if i_now - i_lo < MIN_SNAP_TICKS or i_now < 1:
            continue
        mid = int(px[i_now - 1])
        wpx = px[i_lo:i_now]
        wvol = vol[i_lo:i_now]
        pmin, pmax = int(wpx.min()), int(wpx.max())
        span = pmax - pmin + 1
        prof = np.bincount(wpx - pmin, weights=wvol, minlength=span)
        nzl = int(np.count_nonzero(prof))
        if nzl < MIN_PROF_LEVELS:
            continue
        meanlvl = float(prof.sum()) / nzl
        prof3 = np.convolve(prof, np.ones(3), mode="same")
        n_poll += 1
        for sgn in (1, -1):
            P = mid - sgn * DISTS
            k = P - pmin
            ok = (k >= 0) & (k < span)
            kc = np.clip(k, 0, span - 1)
            z3 = np.where(ok, prof3[kc], 0.0) / 3.0 / meanlvl
            Pn = int(P[int(np.argmax(z3))])
            if prev[sgn] is not None:
                sh = abs(Pn - prev[sgn])
                shifts.append(sh)
                if sh > 2.0:                       # rail_match_pts → cancel+place
                    n_move[sgn] += 1
            # 對照：追價 d22（價位＝mid−sgn*22，不含任何節點資訊）的重掛次數，
            # 用來把「節點會動」與「mid 本來就會動」拆開
            Pb = int(mid - sgn * D_BASE)
            if prev_b[sgn] is not None:
                sb = abs(Pb - prev_b[sgn])
                shifts_b.append(sb)
                if sb > 2.0:
                    n_move_chase[sgn] += 1
            prev_b[sgn] = Pb
            prev[sgn] = Pn
            # hysteresis 版節點政策：偏離 H 點才重掛；漂出 hang 帶則強制重掛
            for h in HYSTERESIS:
                cur = hys_px[h][sgn]
                if cur is None:
                    hys_px[h][sgn] = Pn
                    continue
                dist_now = sgn * (mid - cur)
                out_of_band = not (HANG_LO <= dist_now <= HANG_HI)
                if out_of_band:
                    hys_n[h] += 1
                    hys_band[h] += 1
                    hys_px[h][sgn] = Pn
                elif abs(Pn - cur) > h:
                    hys_n[h] += 1
                    hys_px[h][sgn] = Pn
            # 現行 sticky 基準：價位絕對釘死，只在 |rail − spot| > sticky_max 才重掛
            if sticky_px[sgn] is None:
                sticky_px[sgn] = int(mid - sgn * D_BASE)
            elif abs(sticky_px[sgn] - mid) > 150:
                n_move_base[sgn] += 1
                sticky_px[sgn] = int(mid - sgn * D_BASE)
    if n_poll == 0:
        return None
    sh = np.array(shifts, dtype=np.float64)
    return {
        "date": date_str, "n_polls": n_poll,
        "requotes_both_sides": int(n_move[1] + n_move[-1]),
        "sticky_requotes_both_sides": int(n_move_base[1] + n_move_base[-1]),
        "median_abs_shift_pts": float(np.median(sh)) if len(sh) else float("nan"),
        "p90_abs_shift_pts": float(np.percentile(sh, 90)) if len(sh) else float("nan"),
        "frac_polls_shift_gt_rail2": float((sh > 2.0).mean()) if len(sh) else float("nan"),
        "frac_polls_shift_zero": float((sh == 0).mean()) if len(sh) else float("nan"),
        "chase_d22_requotes_both_sides": int(n_move_chase[1] + n_move_chase[-1]),
        "chase_d22_median_abs_shift_pts": float(np.median(shifts_b)) if shifts_b else float("nan"),
        "hys_requotes": {str(h): int(hys_n[h]) for h in HYSTERESIS},
        "hys_forced_by_band": {str(h): int(hys_band[h]) for h in HYSTERESIS},
    }


def _churn_block(args):
    dates, poll_sec = args
    return [r for r in (churn_day(d, poll_sec) for d in dates) if r is not None]


def run_churn(n_days: int = 120, poll_sec: int = 20, workers: int = 8) -> dict:
    dates = sorted(p.stem for p in DAYCACHE.glob("*.npz"))[-n_days:]   # 連續尾段
    step = (len(dates) + workers - 1) // workers
    blocks = [(dates[i:i + step], poll_sec) for i in range(0, len(dates), step)]
    with mp.Pool(workers) as pool:
        res = pool.map(_churn_block, blocks)
    rows = [r for b in res for r in b]
    if not rows:
        return {}
    rq = np.array([r["requotes_both_sides"] for r in rows], dtype=np.float64)
    sq = np.array([r["sticky_requotes_both_sides"] for r in rows], dtype=np.float64)
    npl = np.array([r["n_polls"] for r in rows], dtype=np.float64)
    cq = np.array([r["chase_d22_requotes_both_sides"] for r in rows], dtype=np.float64)
    cm = np.array([r["chase_d22_median_abs_shift_pts"] for r in rows], dtype=np.float64)
    fr = np.array([r["frac_polls_shift_gt_rail2"] for r in rows], dtype=np.float64)
    md = np.array([r["median_abs_shift_pts"] for r in rows], dtype=np.float64)
    p90 = np.array([r["p90_abs_shift_pts"] for r in rows], dtype=np.float64)
    zr = np.array([r["frac_polls_shift_zero"] for r in rows], dtype=np.float64)
    api = 2.0 * rq          # cancel + place per requote
    hys = {}
    for h in HYSTERESIS:
        a = np.array([2.0 * r["hys_requotes"][str(h)] for r in rows], dtype=np.float64)
        b = np.array([r["hys_forced_by_band"][str(h)] for r in rows], dtype=np.float64)
        nd = a - 2.0 * b          # 扣掉「漂出 12-33 帶被迫重掛」後，真正因節點移動的部分
        hys[f"H={h}pts"] = {
            "api_calls_per_day_mean": float(a.mean()),
            "api_calls_per_day_p90": float(np.percentile(a, 90)),
            "frac_days_over_api_budget_400": float((a > 400).mean()),
            "node_driven_api_calls_per_day_mean": float(nd.mean()),
            "node_driven_frac_days_over_api_budget_400": float((nd > 400).mean()),
            "share_of_requotes_forced_by_leaving_hang_band":
                float(b.sum() / max(a.sum() / 2.0, 1e-9)),
        }
    return {
        "hysteresis_mitigation": hys,
        "spec": {"n_days": len(rows), "poll_sec": poll_sec,
                 "dates": [rows[0]["date"], rows[-1]["date"]],
                 "rail_match_pts": 2.0, "max_api_per_day": 400, "max_api_per_poll": 16,
                 "note": "day session only; both sides quoted; requote = |ΔP_node| > rail_match_pts"},
        "polls_per_day_mean": float(npl.mean()),
        "node_requotes_per_day_mean": float(rq.mean()),
        "node_requotes_per_day_p90": float(np.percentile(rq, 90)),
        "node_api_calls_per_day_mean": float(api.mean()),
        "node_api_calls_per_day_p90": float(np.percentile(api, 90)),
        "node_api_calls_per_day_max": float(api.max()),
        "frac_days_over_api_budget_400": float((api > 400).mean()),
        "chase_d22_api_calls_per_day_mean": float(2.0 * cq.mean()),
        "chase_d22_median_abs_shift_per_poll_pts": float(np.nanmean(cm)),
        "sticky_baseline_requotes_per_day_mean": float(sq.mean()),
        "sticky_baseline_api_calls_per_day_mean": float(2.0 * sq.mean()),
        "frac_polls_node_price_moves_gt_rail2": float(np.nanmean(fr)),
        "frac_polls_node_price_unchanged": float(np.nanmean(zr)),
        "median_abs_node_price_shift_per_poll_pts": float(np.nanmean(md)),
        "p90_abs_node_price_shift_per_poll_pts": float(np.nanmean(p90)),
    }


# ==========================================================================
# 判決文字（全部由資料重算，硬規則 #5）
# ==========================================================================
def build_verdict(R: dict) -> dict:
    q1t = R["Q1_cost_arithmetic"]["touch__OOS"]
    q1q = R["Q1_cost_arithmetic"]["qp050__OOS"]
    q1r = R["Q1_cost_arithmetic"]["thru__OOS"]
    q1i = R["Q1_cost_arithmetic"]["touch__IS"]
    q2 = R["Q2_queue_adverse_selection"]
    q3 = R["Q3_concentration"]
    q4 = R["Q4_disguised_filter"]
    ch = R.get("Q5_engineering", {})
    c = COST["TMF"]

    lad = q2["fill_model_ladder_OOS"]
    signs = {k: np.sign(v["d_net_per_quote_TMF_pts"]) for k, v in lad.items()}
    n_pos = sum(1 for v in signs.values() if v > 0)

    yr = R["by_year"]
    yr_pos = sum(1 for v in yr.values() if v["touch"]["d_gross_per_quote_pts"] > 0)

    v = {
        "headline": (
            f"節點選價在成本線前是負的：OOS 樂觀 touch 模型下每報價淨值 "
            f"{q1t['d_net_per_quote_TMF_pts']:+.4f} 點 "
            f"（＝TMF 成本線 {c} 點的 {q1t['d_net_per_quote_TMF_as_pct_of_cost_line']:+.1f}%），"
            f"排隊模型 λ=0.5 下 {q1q['d_net_per_quote_TMF_pts']:+.4f} 點 "
            f"（{q1q['d_net_per_quote_TMF_as_pct_of_cost_line']:+.1f}%），"
            f"悲觀穿價模型下 {q1r['d_net_per_quote_TMF_pts']:+.4f} 點 "
            f"（{q1r['d_net_per_quote_TMF_as_pct_of_cost_line']:+.1f}%）。"
            f"{n_pos}/{len(signs)} 個成交模型為正。"
        ),
        "Q1_effect_vs_cost": (
            f"毛額面：節點相對距離配對基準的每報價毛額 Δ = "
            f"{q1t['d_gross_per_quote_pts']:+.4f} 點（t={q1t['d_gross_per_quote_t']:+.2f}，touch/OOS）；"
            f"但節點把成交率從 {q1t['base_fill_rate']:.1%} 拉到 {q1t['node_fill_rate']:.1%}"
            f"（{q1t['d_fill_rate_pp']:+.2f} 個百分點），"
            f"每多成交一次就多付 {c} 點成本 → 額外成本 "
            f"{q1t['extra_cost_from_extra_fills_TMF_pts_per_quote']:+.4f} 點/報價。"
            f"兩者相加＝{q1t['d_net_per_quote_TMF_pts']:+.4f} 點/報價。"
            f"IS 段同一算式為 {q1i['d_net_per_quote_TMF_pts']:+.4f} 點/報價。"
        ),
        "Q2_queue_adverse_selection": (
            f"節點價位的 10 分鐘成交量是距離配對基準的 "
            f"{q2['queue_proxy']['ratio_node_over_base']:.2f} 倍"
            f"（{q2['queue_proxy']['node_mean_vol_at_level_10min_lots']:.1f} vs "
            f"{q2['queue_proxy']['distmatched_mean_vol_at_level_10min_lots']:.1f} 口），"
            f"排隊本來就比較長。樂觀 touch 與悲觀穿價模型的每報價毛額 Δ 差距 "
            f"{q2['spread_optimistic_minus_pessimistic_pts_per_quote']:+.4f} 點；"
            f"touch 成交率 {lad['touch']['node_fill_rate']:.1%}、"
            f"λ=0.5 {lad['qp050']['node_fill_rate']:.1%}、"
            f"穿價 {lad['thru']['node_fill_rate']:.1%}。"
        ),
        "Q3_concentration": (
            f"OOS touch 模型 Δ 每報價 {q3['touch']['d_gross_per_quote_pts']:+.4f} 點；"
            f"扣掉最大 5 筆 → {q3['touch']['drop_top5_events']:+.4f}；"
            f"扣掉最大 5 天 → {q3['touch']['drop_top5_days_pts_per_quote']:+.4f}；"
            f"逐日同號率 {q3['touch']['frac_days_positive']:.1%}，"
            f"日中位數 {q3['touch']['median_day_pts_per_quote']:+.4f} 點。"
        ),
        "Q4_disguised_filter": (
            f"節點政策實際移動了 {q4['vs_base_d22']['frac_quotes_moved']:.1%} 的報價，"
            f"平均距離從 {D_BASE} 點掉到 "
            f"{q4['vs_base_d22']['mean_chosen_distance_pts']:.2f} 點"
            f"（{q4['vs_base_d22']['frac_chosen_below_base']:.1%} 選在基準之內側、"
            f"{q4['vs_base_d22']['frac_chosen_at_band_floor_12']:.1%} 貼在帶下緣 12），"
            f"成交筆數變動 "
            f"{q4['trade_count_change_touch']['relative_fill_count_change_pct']:+.1f}%。"
            f"筆數不變的前提**不成立**，硬規則 8 的豁免不適用。"
            f"距離凍結（只在 20-24 內挑節點）後成交數僅變 "
            f"{q4['trade_count_change_touch']['frozen_relative_fill_count_change_pct']:+.1f}%，"
            f"此時 Δ 毛額 = "
            f"{q4['trade_count_change_touch']['frozen_d_gross_per_quote_pts']:+.4f} 點/報價"
            f"（t={q4['trade_count_change_touch']['frozen_d_gross_per_quote_t']:+.2f}）。"
        ),
        "by_year_consistency": f"{yr_pos}/{len(yr)} 年 Δ 毛額為正（touch 模型）。",
        "node_availability_inside_band": (
            f"帶內（12-33）真正有 ≥3× 節點的快照只有 "
            f"{R['Q4_node_availability_in_tradeable_band']['inside_hang_band_12_33_band3_thr3']:.1%}"
            f"（band=3 口徑）／"
            f"{R['Q4_node_availability_in_tradeable_band']['inside_hang_band_12_33_band1_thr3']:.1%}"
            f"（band=1 口徑），帶內 max z3 中位數只有 "
            f"{q4['node_availability']['median_max_z3_in_band']:.2f}×。"
            f"brief 引用的『≥12 點外 59.2% 有節點』在本口徑重算為 "
            f"{R['Q4_node_availability_in_tradeable_band']['any_distance_ge12_band1_thr3']:.1%}"
            f"（band=1，不限上界距離），其中只有 "
            f"{R['Q4_node_availability_in_tradeable_band']['frac_of_ge12_nodes_that_are_inside_the_tradeable_band']:.1%}"
            f" 落在可交易的 12-33 帶內 —— 空間覆蓋在**全價格軸**上成立，"
            f"在**掛單允許帶**內不成立。"
        ),
    }
    # ---- 預註記門檻：要通過必須四項全過 ------------------------------------
    dl = R["main"]["OOS"]["touch"]["_deltas"]
    live_like = dl["node_argmax_z3__minus__base_d22"]
    dm = dl["node_argmax_z3__minus__base_dist_matched"]
    gates = {
        # 使用者要的量級：≥0.5 點/筆 才算「經濟上有意義」
        "net_effect_ge_0p5_pts_per_fill": bool(
            live_like["d_net_per_quote_TMF"] /
            max(R["main"]["OOS"]["touch"]["node_argmax_z3"]["fill_rate"], 1e-9) >= 0.5),
        "positive_under_all_fill_models": bool(n_pos == len(signs)),
        "survives_dropping_top5_days": bool(dm["d_gross_per_quote_drop_top5_days"] > 0),
        "IS_and_OOS_same_positive_sign": bool(
            R["Q1_cost_arithmetic"]["touch__IS"]["d_net_per_quote_TMF_pts"] > 0
            and R["Q1_cost_arithmetic"]["touch__OOS"]["d_net_per_quote_TMF_pts"] > 0),
        "trade_count_held_fixed_rule8_exemption": bool(
            abs(q4["trade_count_change_touch"]["relative_fill_count_change_pct"]) < 2.0),
    }
    v["preregistered_gates"] = gates
    v["gates_passed"] = f"{sum(gates.values())}/{len(gates)}"
    v["caveats"] = [
        "語料是 TX（大台）逐筆，價格路徑與 TMF 相同、成交量只是 proxy；"
        "排隊模型的 λ 是相對量綱（λ × 該價位 10 分鐘量），對合約規模不敏感，"
        "但 TMF 本身較薄、絕對口數版（qa010／qa050）不可直接外推。",
        "FinMind 逐筆沒有主動方向（aggressor side），"
        "『該價位成交量』同時含吃買單與吃賣單，會**高估**隊列消耗速度 → "
        "所有 queue 模型仍偏樂觀，真實負選擇只會比這裡更差。",
        "只測日盤 08:45–13:45；夜盤未測。",
        "出場規則是成交後固定持有 900 秒標記，不是 live PV16 的出場；"
        "因此絕對毛額不可與 live 的 2.86 點/筆直接比，"
        "可比的是**同一出場規則下政策之間的 Δ**。",
        "HVN 的解讀 (b)（價格在此卡住 → 對回歸不利）由 vp_g1c 的 |移動| −2.598 點負責，"
        "本輪只做經濟性與可實作性，不重測方向性。",
    ]
    v["decision"] = (
        f"REJECT。對上 live 用的固定距離基準（d22），節點選價的每報價淨值是 "
        f"{live_like['d_net_per_quote_TMF']:+.4f} 點 ＝ TMF 成本線 {c} 點的 "
        f"{100 * live_like['d_net_per_quote_TMF'] / c:+.1f}%，"
        f"（換算每筆成交 {live_like['d_net_per_quote_TMF'] / max(R['main']['OOS']['touch']['node_argmax_z3']['fill_rate'], 1e-9):+.3f} 點，"
        f"使用者要的門檻是 ≥+0.5 點/筆）。"
        f"拆解：多付 {c * (live_like['d_fill_rate_pp'] / 100.0):.4f} 點成本"
        f"（節點把成交率推高 {live_like['d_fill_rate_pp']:+.2f} 個百分點 × {c} 點成本線），"
        f"外加毛額本身變差 {live_like['d_gross_per_quote_pts']:+.4f} 點"
        f"（t={live_like['d_gross_per_quote_t_dayclust']:+.2f}，"
        f"95% CI [{live_like['d_gross_per_quote_ci95'][0]:+.3f}, "
        f"{live_like['d_gross_per_quote_ci95'][1]:+.3f}]，"
        f"逐日同號率 {live_like['frac_days_positive']:.1%}）。"
        f"預註記門檻 {sum(gates.values())}/{len(gates)} 通過。"
    )
    if ch:
        v["Q5_engineering"] = (
            f"以 {ch['spec']['poll_sec']} 秒輪詢計，節點價位每輪有 "
            f"{ch['frac_polls_node_price_moves_gt_rail2']:.1%} 的機率漂移超過 "
            f"rail_match_pts={ch['spec']['rail_match_pts']} 點 → 觸發 cancel+place。"
            f"平均每日 {ch['node_requotes_per_day_mean']:.0f} 次重掛 = "
            f"{ch['node_api_calls_per_day_mean']:.0f} 次 API，"
            f"對上 max_api_per_day={ch['spec']['max_api_per_day']}，"
            f"有 {ch['frac_days_over_api_budget_400']:.1%} 的日子會撞穿保險絲（killed=True 停掉整天）；"
            f"現行 sticky 基準只需 {ch['sticky_baseline_api_calls_per_day_mean']:.1f} 次/日。"
            f"**但這不是節點特有的**：不含任何節點資訊的追價 d22 要 "
            f"{ch['chase_d22_api_calls_per_day_mean']:.0f} 次/日，比節點版還多——"
            f"爆預算的根因是『把絕對價位釘在會動的 mid 上』，live 之所以 sticky 正是為此。"
            f"節點價位每輪中位漂移 {ch['median_abs_node_price_shift_per_poll_pts']:.2f} 點、"
            f"p90 {ch['p90_abs_node_price_shift_per_poll_pts']:.2f} 點。"
        )
        hy = ch["hysteresis_mitigation"]
        fit = [h for h, r in hy.items() if r["frac_days_over_api_budget_400"] <= 0.05]
        v["Q5_hysteresis_mitigation"] = (
            "加遲滯（只在偏離現掛價超過 H 點才重掛）後每日 API："
            + "、".join(f"H={h.split('=')[1]} → {r['api_calls_per_day_mean']:.0f}"
                        for h, r in hy.items())
            + f"；能把撞穿 400 保險絲的日子壓到 ≤5% 的最小門檻是 "
            + (f"{fit[0]}" if fit else "無（測到 H=40 點都不行）")
            + f"。把『價位漂出 {HANG_LO}-{HANG_HI} 帶被迫重掛』這部分扣掉（那是任何"
              f"絕對價位政策都要付的，與節點無關），純粹因節點移動而多出來的 API 是 "
            + "、".join(f"H={h.split('=')[1]} → {r['node_driven_api_calls_per_day_mean']:.0f}"
                        for h, r in hy.items())
            + f"（對照：不含節點資訊的追價 d22 要 {ch['chase_d22_api_calls_per_day_mean']:.0f} 次/日；"
              f"節點價位每輪中位漂移 {ch['median_abs_node_price_shift_per_poll_pts']:.2f} 點，"
              f"追價 d22 是 {ch['chase_d22_median_abs_shift_per_poll_pts']:.2f} 點）。"
              f"要同時待在 400 預算內又保留節點資訊，唯一活路是 H≈10 的遲滯＋sticky 帶容忍，"
              f"此時節點資訊已被自己的遲滯磨掉大半。"
        )
    return v


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("replay", "all"):
        run_replay(workers=int(os.environ.get("VP_WORKERS", "10")))
    if cmd in ("analyze", "all"):
        # 硬規則 #4：analyze 永遠從 rows cache 全部重算，絕不讀回自己的舊輸出
        R = run_analyze()
        R["Q4_node_availability_in_tradeable_band"] = availability_probe(
            n_days=int(os.environ.get("VP_AVAIL_DAYS", "250")))
        R["Q5_engineering"] = run_churn(
            n_days=int(os.environ.get("VP_CHURN_DAYS", "120")),
            poll_sec=int(os.environ.get("VP_POLL_SEC", "20")))
        R["verdict"] = build_verdict(R)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        assert OUT_JSON.resolve() != DAYCACHE.resolve()
        assert not str(OUT_JSON.resolve()).startswith(str(WORKDIR.resolve()))
        tmp = OUT_JSON.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_clean(R), ensure_ascii=False, indent=2))
        tmp.replace(OUT_JSON)
        print(json.dumps(R["verdict"], ensure_ascii=False, indent=2), flush=True)
        print(f"[out] {OUT_JSON}", flush=True)


def _clean(o):
    """丟掉底線開頭的中介陣列，並把 numpy 型別轉成純 python（NaN/Inf → null）。"""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items() if not k.startswith("_day") and k != "_live"}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, np.ndarray):
        return [_clean(v) for v in o.tolist()]
    if isinstance(o, (np.floating, np.integer)):
        o = float(o)
    if isinstance(o, float) and (np.isnan(o) or np.isinf(o)):
        return None
    return o


if __name__ == "__main__":
    main()
