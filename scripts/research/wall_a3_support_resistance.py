#!/usr/bin/env python3
"""A3 — 牆（thick book level）到底擋不擋得住價格？帶對照組的直接檢定。

核心問題：使用者直覺「厚牆有支撐／壓力意義」。本腳本用 live 五檔快照直接檢定，
並且**強制帶同快照、同距離、同檔位的薄牆對照組**——因為價格本來就有很大機率在
任何一個價位附近徘徊，只報牆組的絕對「被擋回率」毫無資訊量。

兩個檢定：
  Test A（contact）  t0 當下 mid 距離某個價位 <= APPROACH 點 → 往後看 H 秒。
                     同一個 (session, side, tier, 距離) 格內，比較「該價位量體屬
                     該格前 20%（厚）」與「後 40%（薄）」兩組的結果。
  Test B（deep）     t0 當下該價位還離 mid 有 DEEP_LO..DEEP_HI 點 → 往後看 H 秒，
                     問「價格有沒有碰到它」「碰到之後有沒有穿過去」。
                     這才是「牆遠遠擋在前面」的版本。

因果切片（硬規則）：牆的身分、厚薄分組、距離分組，**全部凍結在 t0 的簿子**，
forward 視窗只用 t > t0 的資料。任何用到未來的量都不得回頭影響分組。

殭屍過濾：有 ``stale`` 欄位直接信；沒有的舊資料用 ts - book_time > 5 秒判定。
（日／夜盤 book 交錯寫進同一檔，收盤那側會凍結重送，不濾會量到假的「牆撐很久」。）

叢集單位：session（= 一段連續的日盤或夜盤），不是快照。只有 5 個 session
（2 個日盤 + 3 個夜盤），所以跨 session 的**符號一致性**才是證據，p 值不是。
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np

TZ = timezone(timedelta(hours=8))
MAX_BOOK_AGE_SEC = 5.0
DAYS = ["2026-08-14", "2026-08-15", "2026-08-17", "2026-08-18", "2026-08-19"]
GAP_MAX_SEC = 60.0          # forward 視窗內若有超過這個秒數的資料斷檔就丟棄事件
END_TOL_SEC = 15.0          # 視窗尾端必須有快照落在 t0+H 的這個容差內
NTIER = 5


def books_dir() -> Path:
    try:
        import stock_db

        return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_books"
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data" / "cache" / "tmf_books"


# --------------------------------------------------------------------------- load
def load_live_books(days: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """回傳 {session_id: arrays}；session_id 例 'day/2026-08-17'、'night/2026-08-17'。"""
    stats: Counter[str] = Counter()
    raw: dict[str, list[tuple]] = defaultdict(list)
    d = books_dir()
    for day in days:
        path = d / f"tmf_books_{day}.jsonl"
        if not path.exists():
            stats["missing_file"] += 1
            continue
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                stats["rows"] += 1
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    stats["bad_json"] += 1
                    continue
                bids, asks = r.get("bids") or [], r.get("asks") or []
                if len(bids) < NTIER or len(asks) < NTIER:
                    stats["short_book"] += 1
                    continue
                try:
                    wall = datetime.fromisoformat(str(r["ts"])).astimezone(TZ)
                    bt = datetime.fromtimestamp(float(r["book_time"]) / 1e6, tz=TZ)
                except (KeyError, TypeError, ValueError):
                    stats["bad_ts"] += 1
                    continue
                if "stale" in r:
                    is_stale = bool(r["stale"])
                else:
                    is_stale = (wall - bt).total_seconds() > MAX_BOOK_AGE_SEC
                if is_stale:
                    stats["stale_zombie"] += 1
                    continue
                qt = str(r.get("quote_type") or "")
                if qt == "FUTURE":
                    sess = f"day/{bt.date().isoformat()}"
                elif qt == "FUTURE_AH":
                    anchor = bt.date() if bt.hour >= 12 else (bt - timedelta(days=1)).date()
                    sess = f"night/{anchor.isoformat()}"
                else:
                    stats["bad_quote_type"] += 1
                    continue
                stats["live"] += 1
                raw[sess].append(
                    (
                        bt.timestamp(),
                        tuple(float(b["price"]) for b in bids[:NTIER]),
                        tuple(float(b["size"]) for b in bids[:NTIER]),
                        tuple(float(a["price"]) for a in asks[:NTIER]),
                        tuple(float(a["size"]) for a in asks[:NTIER]),
                    )
                )

    out: dict[str, dict[str, Any]] = {}
    for sess, rows in raw.items():
        rows.sort(key=lambda x: x[0])
        # 同一 book_time 只留最後一筆（重送／續約）
        ded: list[tuple] = []
        for r in rows:
            if ded and r[0] == ded[-1][0]:
                ded[-1] = r
            else:
                ded.append(r)
        if len(ded) < 500:
            stats["session_too_small"] += 1
            continue
        t = np.array([r[0] for r in ded], dtype=np.float64)
        bp = np.array([r[1] for r in ded], dtype=np.float64)
        bs = np.array([r[2] for r in ded], dtype=np.float64)
        ap = np.array([r[3] for r in ded], dtype=np.float64)
        asz = np.array([r[4] for r in ded], dtype=np.float64)
        mid = (bp[:, 0] + ap[:, 0]) / 2.0
        gaps = np.diff(t)
        bad_gap_prefix = np.concatenate([[0], np.cumsum(gaps > GAP_MAX_SEC)])
        out[sess] = {
            "t": t, "mid": mid, "bp": bp, "bs": bs, "ap": ap, "asz": asz,
            "gapfix": bad_gap_prefix,
            "n": len(t),
            "t0": datetime.fromtimestamp(t[0], tz=TZ).isoformat(),
            "t1": datetime.fromtimestamp(t[-1], tz=TZ).isoformat(),
        }
    return out, dict(stats)


# ------------------------------------------------------------------- forward window
def past_range(S: dict[str, Any], idx: np.ndarray, lookback: float) -> np.ndarray:
    """t0 之前 lookback 秒的 mid 全距（**只用過去**，用來控制「厚簿子＝安靜盤」）。"""
    t, mid = S["t"], S["mid"]
    a0 = np.searchsorted(t, t[idx] - lookback, side="left")
    out = np.full(len(idx), np.nan)
    for k in range(len(idx)):
        a, b = a0[k], idx[k] + 1
        if b - a < 2:
            continue
        seg = mid[a:b]
        out[k] = seg.max() - seg.min()
    return out


def forward_outcomes(S: dict[str, Any], idx: np.ndarray, horizon: float):
    """回傳 (ok, min_mid, max_mid, end_mid)。ok=False 的事件必須丟棄。"""
    t, mid = S["t"], S["mid"]
    t0 = t[idx]
    j0 = np.searchsorted(t, t0, side="right")
    j1 = np.searchsorted(t, t0 + horizon, side="right")
    ok = j1 > j0
    ok &= (j1 - 1) < len(t)
    last_t = t[np.clip(j1 - 1, 0, len(t) - 1)]
    ok &= (t0 + horizon - last_t) <= END_TOL_SEC
    gf = S["gapfix"]
    ok &= (gf[np.clip(j1 - 1, 0, len(t) - 1)] - gf[idx]) == 0
    mn = np.full(len(idx), np.nan)
    mx = np.full(len(idx), np.nan)
    en = np.full(len(idx), np.nan)
    for k in np.nonzero(ok)[0]:
        a, b = j0[k], j1[k]
        seg = mid[a:b]
        mn[k] = seg.min()
        mx[k] = seg.max()
        en[k] = seg[-1]
    return ok, mn, mx, en


def pick_event_rows(S: dict[str, Any], spacing: float) -> np.ndarray:
    """以 spacing 秒為最小間隔挑事件列（降低日內自相關）。"""
    t = S["t"]
    keep = [0]
    last = t[0]
    for i in range(1, len(t)):
        if t[i] - last >= spacing:
            keep.append(i)
            last = t[i]
    return np.array(keep, dtype=np.int64)


# ------------------------------------------------------------------------ statistics
def _q(a: np.ndarray, p: float) -> float:
    return float(np.quantile(a, p)) if a.size else float("nan")


def _summ(pen: np.ndarray, mv: np.ndarray, breach_pt: float, away: np.ndarray | None = None) -> dict[str, Any]:
    """pen = sgn*(P - extreme)，>0 表示穿過牆；mv = sgn*(end - m0)，>0 表示被推離牆。"""
    n = int(pen.size)
    if n == 0:
        return {"n": 0}
    breached = pen >= breach_pt
    repelled = (~breached) & (mv > 0)
    res = {
        "n": n,
        "p_breach": float(breached.mean()),
        "p_repelled": float(repelled.mean()),
        "p_neither": float((~breached & ~repelled).mean()),
        "mean_move_away_pt": float(mv.mean()),
        "se_move_away_pt": float(mv.std(ddof=1) / math.sqrt(n)) if n > 1 else float("nan"),
        "mean_penetration_pt": float(pen.mean()),
        "mean_continuation_if_breach_pt": float(-mv[breached].mean()) if breached.any() else None,
        "mean_bounce_if_repelled_pt": float(mv[repelled].mean()) if repelled.any() else None,
    }
    if away is not None and away.size == n:
        # placebo：往「遠離牆」的方向走同樣 breach_pt 點的機率。牆若只是「安靜盤」
        # 的代理變數，這個機率會跟 p_breach 一起掉；真的支撐才會只掉 p_breach。
        res["p_move_away_ge_breach_pt"] = float((away >= breach_pt).mean())
    return res


def _grp_summ(recs: list[dict], grp: str, breach_pt: float) -> dict[str, Any]:
    sub = [r for r in recs if r["grp"] == grp]
    return _summ(
        np.array([r["pen"] for r in sub]),
        np.array([r["mv"] for r in sub]),
        breach_pt,
        np.array([r["away"] for r in sub]),
    )


def strat_diff(recs: list[dict], key_fields: tuple[str, ...], breach_pt: float,
               min_cell: int = 20) -> dict[str, Any]:
    """分層（cell）內 wall−thin 差，再依 cell 內較小組人數加權平均。"""
    cells: dict[tuple, dict[str, list]] = defaultdict(lambda: {"wall": [], "thin": []})
    for r in recs:
        k = tuple(r[f] for f in key_fields)
        if r["grp"] in ("wall", "thin"):
            cells[k][r["grp"]].append(r)
    num_b = num_m = num_a = den = 0.0
    ncell = 0
    nw = nt = 0
    for k, g in cells.items():
        if len(g["wall"]) < min_cell or len(g["thin"]) < min_cell:
            continue
        wp = np.array([r["pen"] for r in g["wall"]])
        wm = np.array([r["mv"] for r in g["wall"]])
        tp = np.array([r["pen"] for r in g["thin"]])
        tm = np.array([r["mv"] for r in g["thin"]])
        wa = np.array([r["away"] for r in g["wall"]])
        ta = np.array([r["away"] for r in g["thin"]])
        w = min(len(wp), len(tp))
        num_b += w * (float((wp >= breach_pt).mean()) - float((tp >= breach_pt).mean()))
        num_a += w * (float((wa >= breach_pt).mean()) - float((ta >= breach_pt).mean()))
        num_m += w * (float(wm.mean()) - float(tm.mean()))
        den += w
        ncell += 1
        nw += len(wp)
        nt += len(tp)
    if den == 0:
        return {"cells": 0}
    return {
        "cells": ncell,
        "n_wall": nw,
        "n_thin": nt,
        "d_p_breach_wall_minus_thin": num_b / den,
        "d_p_move_away_wall_minus_thin": num_a / den,
        "d_move_away_pt_wall_minus_thin": num_m / den,
        "asymmetry_breach_minus_away": (num_b / den) - (num_a / den),
    }


# ------------------------------------------------------------------------- main test
def run_test(
    sessions: dict[str, dict[str, Any]],
    horizons: list[float],
    spacing: float,
    mode: str,
    approach: float,
    deep_lo: float,
    deep_hi: float,
    breach_pt: float,
    q_wall: float,
    q_thin: float,
    obi_control: bool,
    min_cell: int = 20,
    prevol_lookback: float = 60.0,
) -> dict[str, Any]:
    out: dict[str, Any] = {"per_horizon": {}}
    # ---- 依 session/side/tier 算量體分位（分組門檻只用該 session 自己的分布；
    #      這是「同一天內的相對厚度」，不含跨日 look-ahead 疑慮以外的未來資訊，
    #      但仍是同一 session 全域分位——分位門檻不用到 forward 視窗的價格，
    #      只用簿子量體，故不會造成報酬洩漏。）
    thr: dict[tuple, tuple[float, float, float]] = {}
    for sess, S in sessions.items():
        for side, arr in (("bid", S["bs"]), ("ask", S["asz"])):
            for k in range(NTIER):
                col = arr[:, k]
                thr[(sess, side, k)] = (_q(col, q_thin), _q(col, q_wall), float(np.median(col)))

    for H in horizons:
        recs_all: list[dict] = []
        per_sess: dict[str, Any] = {}
        for sess, S in sessions.items():
            idx = pick_event_rows(S, spacing)
            ok, mn, mx, en = forward_outcomes(S, idx, H)
            keep = np.nonzero(ok)[0]
            if keep.size == 0:
                continue
            idx = idx[keep]
            mn, mx, en = mn[keep], mx[keep], en[keep]
            m0 = S["mid"][idx]
            pv = past_range(S, idx, prevol_lookback)
            pv_edges = [np.nanquantile(pv, 0.33), np.nanquantile(pv, 0.67)]
            pv_bin = np.digitize(np.nan_to_num(pv, nan=-1.0), pv_edges)
            recs_s: list[dict] = []
            for side in ("bid", "ask"):
                sgn = 1.0 if side == "bid" else -1.0
                prices = S["bp"] if side == "bid" else S["ap"]
                sizes = S["bs"] if side == "bid" else S["asz"]
                opp1 = S["asz"][:, 0] if side == "bid" else S["bs"][:, 0]
                extreme = mn if side == "bid" else mx
                opp_extreme = mx if side == "bid" else mn
                for k in range(NTIER):
                    P = prices[idx, k]
                    Sz = sizes[idx, k]
                    dist = sgn * (m0 - P)            # 正 = 牆在前方
                    if mode == "contact":
                        sel = (dist > 0) & (dist <= approach)
                    else:
                        sel = (dist >= deep_lo) & (dist <= deep_hi)
                    if not sel.any():
                        continue
                    lo, hi, _med = thr[(sess, side, k)]
                    grp = np.where(Sz >= hi, "wall", np.where(Sz <= lo, "thin", "mid"))
                    pen = sgn * (P - extreme)        # >0 = 穿過牆
                    mv = sgn * (en - m0)             # >0 = 被推離牆
                    away = sgn * (opp_extreme - m0)  # 往遠離牆方向的最大位移（placebo）
                    ob = np.digitize(opp1[idx], [_q(opp1, 0.33), _q(opp1, 0.67)])
                    dbin = np.round(dist * 2) / 2.0
                    for j in np.nonzero(sel)[0]:
                        recs_s.append(
                            {
                                "sess": sess,
                                "side": side,
                                "tier": k,
                                "dbin": float(dbin[j]),
                                "obibin": int(ob[j]),
                                "grp": str(grp[j]),
                                "volbin": int(pv_bin[j]),
                                "pen": float(pen[j]),
                                "mv": float(mv[j]),
                                "away": float(away[j]),
                                "size": float(Sz[j]),
                            }
                        )
            recs_all.extend(recs_s)
            key = ("sess", "side", "tier", "dbin")
            per_sess[sess] = {
                "n_events": len(recs_s),
                "wall": _grp_summ(recs_s, "wall", breach_pt),
                "thin": _grp_summ(recs_s, "thin", breach_pt),
                "stratified_diff": strat_diff(recs_s, key, breach_pt, min_cell),
                "stratified_diff_volctl": strat_diff(
                    recs_s, key + ("volbin",), breach_pt, min_cell
                ),
            }

        pooled_key = ("sess", "side", "tier", "dbin")
        block: dict[str, Any] = {
            "per_session": per_sess,
            "pooled_raw_wall": _grp_summ(recs_all, "wall", breach_pt),
            "pooled_raw_thin": _grp_summ(recs_all, "thin", breach_pt),
            "pooled_stratified_diff": strat_diff(recs_all, pooled_key, breach_pt, min_cell),
            "pooled_stratified_diff_volctl": strat_diff(
                recs_all, pooled_key + ("volbin",), breach_pt, min_cell
            ),
        }
        for sd in ("day", "night"):
            sub = [r for r in recs_all if r["sess"].startswith(sd)]
            if sub:
                block[f"stratified_diff_{sd}"] = strat_diff(sub, pooled_key, breach_pt, min_cell)
        for sd in ("bid", "ask"):
            sub = [r for r in recs_all if r["side"] == sd]
            if sub:
                block[f"stratified_diff_{sd}side"] = strat_diff(sub, pooled_key, breach_pt, min_cell)
        if obi_control:
            block["pooled_stratified_diff_obi_controlled"] = strat_diff(
                recs_all, ("sess", "side", "tier", "dbin", "obibin"), breach_pt, min_cell
            )
        # 跨 session 一致性
        db = [
            per_sess[s]["stratified_diff"].get("d_p_breach_wall_minus_thin")
            for s in per_sess
            if per_sess[s]["stratified_diff"].get("cells")
        ]
        dm = [
            per_sess[s]["stratified_diff"].get("d_move_away_pt_wall_minus_thin")
            for s in per_sess
            if per_sess[s]["stratified_diff"].get("cells")
        ]
        da = [
            per_sess[s]["stratified_diff"].get("d_p_move_away_wall_minus_thin")
            for s in per_sess
            if per_sess[s]["stratified_diff"].get("cells")
        ]
        block["consistency"] = {
            "session_away_diffs": da,
            "sessions_with_estimate": len(db),
            "n_sessions_breach_diff_negative": int(sum(1 for x in db if x is not None and x < 0)),
            "n_sessions_move_away_diff_positive": int(sum(1 for x in dm if x is not None and x > 0)),
            "session_breach_diffs": db,
            "session_move_away_diffs": dm,
        }
        # 厚度分層（wall 內部再切）
        walls = [r for r in recs_all if r["grp"] == "wall"]
        if walls:
            sz = np.array([r["size"] for r in walls])
            cut = _q(sz, 0.5)
            pen = np.array([r["pen"] for r in walls])
            mvv = np.array([r["mv"] for r in walls])
            awy = np.array([r["away"] for r in walls])
            for label, mask in (("wall_lower_half", sz < cut), ("wall_upper_half", sz >= cut)):
                block[label] = _summ(pen[mask], mvv[mask], breach_pt, awy[mask])
            block["wall_size_split_pt"] = float(cut)
        out["per_horizon"][str(int(H))] = block
    return out


def strategy_reach(sessions: dict[str, dict[str, Any]], q_wall: float) -> dict[str, Any]:
    """牆到底出現在離 mid 多遠的地方？——決定「牆資訊能不能餵給掛單距離」的關鍵數字。

    策略掛單距離 hang_lo–hang_hi = 12–33 點（新方向 ×2 → 24–66 點）。
    這裡量「任一檔既是牆、又落在 >= X 點」的快照占比。
    """
    out: dict[str, Any] = {}
    for label, X in (("ge_6pt", 6.0), ("ge_12pt", 12.0), ("ge_24pt", 24.0)):
        hit = tot = 0
        for sess, S in sessions.items():
            mid = S["mid"]
            for side, prices, sizes in (
                ("bid", S["bp"], S["bs"]),
                ("ask", S["ap"], S["asz"]),
            ):
                thr = np.array([np.quantile(sizes[:, k], q_wall) for k in range(NTIER)])
                sgn = 1.0 if side == "bid" else -1.0
                dist = sgn * (mid[:, None] - prices)
                iswall = sizes >= thr[None, :]
                hit += int(((dist >= X) & iswall).any(axis=1).sum())
                tot += len(mid)
        out[label] = {"n_snapshot_side": tot, "hit": hit, "share": hit / tot if tot else None}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizons", default="30,60,300")
    ap.add_argument("--approach", type=float, default=2.0)
    ap.add_argument("--deep-lo", type=float, default=3.0)
    ap.add_argument("--deep-hi", type=float, default=6.0)
    ap.add_argument("--breach-pt", type=float, default=1.0)
    ap.add_argument("--q-wall", type=float, default=0.80)
    ap.add_argument("--q-thin", type=float, default=0.40)
    ap.add_argument("--out", default="reports/research/channel_lab/wall_a3_support_resistance.json")
    args = ap.parse_args()

    horizons = [float(x) for x in args.horizons.split(",")]
    sessions, load_stats = load_live_books(DAYS)
    print("load:", load_stats)
    for s, S in sorted(sessions.items()):
        print(f"  {s:<22} n={S['n']:>7}  {S['t0'][:19]} → {S['t1'][:19]}")

    result: dict[str, Any] = {
        "generated_at": datetime.now(TZ).isoformat(),
        "question": "厚牆（book 某檔量體異常大）是否比同距離的薄檔更能擋住價格？",
        "params": vars(args),
        "load_stats": load_stats,
        "sessions": {s: {"n": S["n"], "start": S["t0"], "end": S["t1"]} for s, S in sessions.items()},
        "tests": {},
    }
    for spacing_label, spacing, min_cell in (
        ("dense_5s", 5.0, 20),
        ("nonoverlap_60s", 60.0, 12),
        ("nonoverlap_300s", 300.0, 8),
    ):
        result["tests"][f"contact/{spacing_label}"] = run_test(
            sessions, horizons, spacing, "contact", args.approach, args.deep_lo,
            args.deep_hi, args.breach_pt, args.q_wall, args.q_thin,
            obi_control=(spacing_label == "dense_5s"), min_cell=min_cell,
        )
        result["tests"][f"deep/{spacing_label}"] = run_test(
            sessions, horizons, spacing, "deep", args.approach, args.deep_lo,
            args.deep_hi, args.breach_pt, args.q_wall, args.q_thin,
            obi_control=False, min_cell=min_cell,
        )
    result["strategy_reach"] = strategy_reach(sessions, args.q_wall)
    result["size_thresholds"] = {
        f"{sess}/{side}/tier{k}": {
            "thin_q40": float(np.quantile(arr[:, k], args.q_thin)),
            "median": float(np.median(arr[:, k])),
            "wall_q80": float(np.quantile(arr[:, k], args.q_wall)),
            "p99": float(np.quantile(arr[:, k], 0.99)),
        }
        for sess, S in sessions.items()
        for side, arr in (("bid", S["bs"]), ("ask", S["asz"]))
        for k in range(NTIER)
    }
    # 經濟意義換算：TMF 一點 = NT$10；已知成本線 4.05 點／筆；中位價差 3 點。
    hh = result["tests"]["contact/dense_5s"]["per_horizon"]
    result["economics"] = {
        "tmf_point_value_ntd": 10,
        "known_cost_line_pt_per_trade": 4.05,
        "median_spread_pt": 3,
        "strategy_hang_distance_pt": [12, 33],
        "strategy_hang_distance_x2_pt": [24, 66],
        "best_case_edge_pt_h30": hh["30"]["pooled_stratified_diff"]["d_move_away_pt_wall_minus_thin"],
        "edge_pt_h60": hh["60"]["pooled_stratified_diff"]["d_move_away_pt_wall_minus_thin"],
        "edge_pt_h300": hh["300"]["pooled_stratified_diff"]["d_move_away_pt_wall_minus_thin"],
        "note": (
            "牆的『擋』效應只存在於離 mid 1–2 點的接觸處；策略掛單在 12–33（或 24–66）點，"
            "那個距離上有牆的快照占比 = strategy_reach.ge_12pt / ge_24pt。"
        ),
    }

    outp = Path(args.out)
    if not outp.is_absolute():
        outp = Path(__file__).resolve().parents[2] / outp
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("wrote", outp)

    for name, blk in result["tests"].items():
        for H, b in blk["per_horizon"].items():
            sd = b["pooled_stratified_diff"]
            c = b["consistency"]
            if not sd.get("cells"):
                continue
            print(
                f"{name:<26} H={H:>4}s  cells={sd['cells']:>4} "
                f"nW={sd['n_wall']:>6} nT={sd['n_thin']:>6}  "
                f"dP(breach)={sd['d_p_breach_wall_minus_thin']:+.4f}  "
                f"dP(away)={sd['d_p_move_away_wall_minus_thin']:+.4f}  "
                f"asym={sd['asymmetry_breach_minus_away']:+.4f}  "
                f"dMoveAway={sd['d_move_away_pt_wall_minus_thin']:+.4f}pt  "
                f"consist breach-neg {c['n_sessions_breach_diff_negative']}/{c['sessions_with_estimate']} "
                f"move-pos {c['n_sessions_move_away_diff_positive']}/{c['sessions_with_estimate']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
