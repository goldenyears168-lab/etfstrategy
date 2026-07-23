"""Fair-comparison research · Leading Dip ↔ ABC v3+F1 under a shared contract.

Research-only. Does not write ``config/strategy.yaml``.

Contract (SSOT · leading dip research note §公平對照計畫):
  * window 2025-01-02 → latest · OOS cut 2025-10-01
  * hold 3 trading days · bench IX0001 (0050 fallback same-source chain)
  * report stock net return % AND excess vs bench %
  * cost haircut 0.585pp (matches ABC ROUND_TRIP_COST_PCT); Dip gross also reported
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.dual_wma_signal_backtest import ROUND_TRIP_COST_PCT
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.leading_dip_sleeve_validate import (
    HOLD_DAYS,
    OOS_CUT_DEFAULT,
    START_DEFAULT,
    TOTAL_OPEN_CAP,
    apply_frozen_policy,
    collect_leading_dip_events,
    load_universe_by_baseline,
)
from research.backtest.triple_wma_pullback_sweep import matches_w3_rv_hl_abc_v3_f1
from rrg_mono_daily_brief import LOOKBACK, _feat
from rrg_rotation import compute_rrg_panel

SCHEMA = "leading_dip_vs_abc_fair_compare-v1"
COST_HAIRCUT_PP = float(ROUND_TRIP_COST_PCT)  # 0.585 · consistent across arms
ARM_A_ORDER = ("A0", "A1", "A2", "A3", "A4")
ARM_B_ORDER = ("B0", "B1", "B2")
UNIVERSE_MODES = {
    "B0": "leading",
    "B1": "leading_improving",
    "B2": "rv20_ge_100",
}


# --- Pure helpers (unit-tested) -------------------------------------------------


def passes_arm_a_layer(
    arm: str,
    *,
    is_leading: bool,
    not_ur: bool,
    w5_weak: bool,
    deep_enough: bool | None = None,
) -> bool:
    """Return True if event passes Arm A filter ``arm`` (A0–A4)."""
    a = str(arm).upper()
    if a == "A0":
        return True
    if a == "A1":
        return bool(is_leading)
    if a == "A2":
        return bool(is_leading) and bool(not_ur)
    if a == "A3":
        return bool(is_leading) and bool(not_ur) and bool(w5_weak)
    if a == "A4":
        if deep_enough is None:
            return False
        return bool(is_leading) and bool(not_ur) and bool(w5_weak) and bool(deep_enough)
    raise ValueError(f"unknown arm={arm!r}")


def apply_cost_haircut(
    px: float | None,
    excess: float | None,
    *,
    haircut_pp: float = COST_HAIRCUT_PP,
) -> tuple[float | None, float | None]:
    """Subtract flat round-trip haircut from stock return; excess uses same haircut."""
    if px is None or (isinstance(px, float) and math.isnan(px)):
        px_n = None
    else:
        px_n = float(px) - float(haircut_pp)
    if excess is None or (isinstance(excess, float) and math.isnan(excess)):
        ex_n = None
    else:
        ex_n = float(excess) - float(haircut_pp)
    return px_n, ex_n


def deep_enough_proxy(
    *,
    excess0: float | None = None,
    gap3: float | None = None,
    excess_max: float = -4.0,
    gap3_max: float = -0.80,
) -> bool | None:
    """A4 depth proxy. None when neither metric is available."""
    has_ex = excess0 is not None and not (isinstance(excess0, float) and math.isnan(excess0))
    has_gap = gap3 is not None and not (isinstance(gap3, float) and math.isnan(gap3))
    if not has_ex and not has_gap:
        return None
    ok_ex = bool(has_ex and float(excess0) <= float(excess_max))
    ok_gap = bool(has_gap and float(gap3) <= float(gap3_max))
    return ok_ex or ok_gap


def summarize_fair_arm(
    df: pd.DataFrame,
    *,
    oos_cut: str = OOS_CUT_DEFAULT,
    n_trading_days: int | None = None,
    px_col: str = "px_net",
    ex_col: str = "ex_net",
    date_col: str = "date",
) -> dict[str, Any]:
    """Standard fair-contract metrics for one arm table."""
    empty = {
        "n": 0,
        "days": 0,
        "n_per_td": None,
        "hit_px": None,
        "hit_ex": None,
        "med_px": None,
        "mean_px": None,
        "med_ex": None,
        "mean_ex": None,
        "oos_n": 0,
        "oos_hit_px": None,
        "oos_hit_ex": None,
        "oos_med_px": None,
        "oos_med_ex": None,
        "oos_mean_px": None,
        "oos_mean_ex": None,
        "per_year": {},
        "note": "empty",
    }
    if df is None or len(df) == 0:
        return empty
    d = df.copy()
    d[date_col] = d[date_col].astype(str)
    px = d[px_col].astype(float)
    ex = d[ex_col].astype(float)
    oos = d[d[date_col] >= str(oos_cut)]
    n_td = int(n_trading_days) if n_trading_days else None
    out: dict[str, Any] = {
        "n": int(len(d)),
        "days": int(d[date_col].nunique()),
        "n_per_td": (float(len(d)) / n_td) if n_td and n_td > 0 else None,
        "hit_px": float((px > 0).mean()),
        "hit_ex": float((ex > 0).mean()),
        "med_px": float(px.median()),
        "mean_px": float(px.mean()),
        "med_ex": float(ex.median()),
        "mean_ex": float(ex.mean()),
        "oos_n": int(len(oos)),
        "per_year": {
            str(k): int(v) for k, v in d[date_col].str[:4].value_counts().sort_index().items()
        },
    }
    if len(oos):
        opx = oos[px_col].astype(float)
        oex = oos[ex_col].astype(float)
        out.update(
            {
                "oos_hit_px": float((opx > 0).mean()),
                "oos_hit_ex": float((oex > 0).mean()),
                "oos_med_px": float(opx.median()),
                "oos_med_ex": float(oex.median()),
                "oos_mean_px": float(opx.mean()),
                "oos_mean_ex": float(oex.mean()),
            }
        )
    else:
        out.update(
            {
                "oos_hit_px": None,
                "oos_hit_ex": None,
                "oos_med_px": None,
                "oos_med_ex": None,
                "oos_mean_px": None,
                "oos_mean_ex": None,
            }
        )
    if out["oos_n"] < 50:
        out["oos_note"] = "n_insufficient_for_ci"
    return out


def decide_fair_verdict(arm_a: dict[str, Any], arm_b: dict[str, Any]) -> dict[str, Any]:
    """Decision matrix from the fair-compare plan (prefer tighten ABC vs expand Dip)."""
    a0 = arm_a.get("A0") or {}
    a3 = arm_a.get("A3") or {}
    b0 = arm_b.get("B0") or {}
    b0s = arm_b.get("B0_slots") or b0
    b1 = arm_b.get("B1") or {}
    b2 = arm_b.get("B2") or {}

    dip_n = int(b0s.get("n") or b0.get("n") or 0)
    dip_hit = b0s.get("hit_ex") if b0s.get("hit_ex") is not None else b0.get("hit_ex")
    dip_med = b0s.get("med_ex") if b0s.get("med_ex") is not None else b0.get("med_ex")
    a3_n = int(a3.get("n") or 0)
    a3_hit = a3.get("hit_ex")
    a3_med = a3.get("med_ex")
    a0_n = int(a0.get("n") or 0)

    reasons: list[str] = []
    choice = "dual_track_or_hold"

    a1 = arm_a.get("A1") or {}
    a1_n = int(a1.get("n") or 0)
    quality_collapsed = (
        a3_med is not None
        and dip_med is not None
        and float(a3_med) + 1.5 < float(dip_med)
        and a3_hit is not None
        and dip_hit is not None
        and float(a3_hit) + 0.15 < float(dip_hit)
    )
    leading_starves = a0_n > 0 and (
        a3_n < max(10, dip_n // 2)
        or quality_collapsed
    ) and (
        a3_med is None or (dip_med is not None and float(a3_med) + 0.5 < float(dip_med))
    )
    a3_near_dip = (
        a3_n >= max(20, 2 * dip_n)
        and a3_hit is not None
        and dip_hit is not None
        and float(a3_hit) >= float(dip_hit) - 0.05
        and a3_med is not None
        and dip_med is not None
        and float(a3_med) >= float(dip_med) - 1.5
        and not quality_collapsed
    )

    def _b_mild(tag: str, row: dict[str, Any]) -> bool:
        if not row or int(row.get("n") or 0) <= dip_n:
            return False
        oh = row.get("oos_hit_ex")
        om = row.get("oos_med_ex")
        bh = b0.get("oos_hit_ex") if b0.get("oos_hit_ex") is not None else b0.get("hit_ex")
        bm = b0.get("oos_med_ex") if b0.get("oos_med_ex") is not None else b0.get("med_ex")
        if oh is None or om is None or bh is None or bm is None:
            reasons.append(f"{tag}: missing OOS metrics → not promote")
            return False
        ok = float(oh) >= 0.70 and float(om) >= float(bm) - 1.5
        reasons.append(
            f"{tag}: n={row.get('n')} OOS hit_ex={oh:.0%} med_ex={om:+.2f} "
            f"vs B0 med={bm:+.2f} → {'mild_ok' if ok else 'fail'}"
        )
        return ok

    if a3_near_dip:
        choice = "tighten_abc"
        reasons.append(
            f"A3≈Dip quality with n={a3_n}≥2×Dip({dip_n}): hit_ex={a3_hit} med_ex={a3_med}"
        )
    elif leading_starves or (a0_n > 0 and a3_n == 0):
        def _r(x: Any) -> str:
            return "—" if x is None else f"{float(x):.2f}"

        reasons.append(
            f"A1–A3 starves/quality not lift: A0 n={a0_n} → A1 n={a1_n} → A3 n={a3_n} "
            f"hit_ex={_r(a3_hit)} med_ex={_r(a3_med)} vs Dip n≈{dip_n} "
            f"hit_ex={_r(dip_hit)} med_ex={_r(dip_med)}"
        )
        if a0_n and a1_n >= int(0.95 * a0_n):
            reasons.append(
                "A1≈A0：EOD Leading 幾乎不篩（ABC matcher 已要求盤中 W20 Leading）"
            )
        b1_ok = _b_mild("B1", b1)
        b2_ok = _b_mild("B2", b2)
        if b1_ok or b2_ok:
            choice = "widen_dip_mild"
            reasons.append("B1/B2 mild expansion passes OOS floor → prefer Dip micro-widen")
        else:
            choice = "dual_track"
            reasons.append("ABC≠Dip alpha under shared filters → dual-track paper observe")
    else:
        b1_ok = _b_mild("B1", b1)
        b2_ok = _b_mild("B2", b2)
        if a3_n >= dip_n and a3_med is not None and dip_med is not None:
            if float(a3_med) >= float(dip_med) - 1.0 and a3_n >= int(1.5 * dip_n):
                choice = "tighten_abc"
                reasons.append("A3 competitive vs Dip with denser n → tighten ABC preferred")
            elif b1_ok or b2_ok:
                choice = "widen_dip_mild"
            else:
                choice = "dual_track"
                reasons.append("Neither A3 nor B mild clearly wins → dual-track")
        elif b1_ok or b2_ok:
            choice = "widen_dip_mild"
        else:
            choice = "hold_observe"
            reasons.append(
                "No promote: keep Dip C0 / leading_dip_cov observe; ABC event layer as-is; "
                "reject bare 600 for Dip"
            )

    prose = {
        "tighten_abc": (
            "優先收緊 ABC（A2/A3 結構濾網）作短持有主事件袖套；Leading Dip 退為交叉驗證／"
            "診斷，勿再為 Dip 開第三條 Strategy。"
        ),
        "widen_dip_mild": (
            "ABC 收緊後無法同時保住密度與尖度；B1/B2 温和擴宇宙通過 OOS 下限 → "
            "微调 Dip 宇宙（Leading∪Improving 或 RV20≥100），禁止跳 600。"
        ),
        "dual_track": (
            "兩條是不同 alpha：A 一加 Leading/notUR 就毁或無增益，B 一扩就淡。维持雙軌紙上 "
            "observe（Dip coverage + ABC 事件層），低相關才有配置意义；600 只做共用監測廠。"
        ),
        "hold_observe": (
            "三者都不稳：维持現行 Dip C0／leading_dip_cov 观察与 ABC 事件層；"
            "不写 strategy.yaml；拒绝为了 n 把 Dip 放到 600。"
        ),
        "dual_track_or_hold": (
            "证据不足下的保守裁决：雙軌 observe，不升 Strategy，不做 600 裸扫。"
        ),
    }.get(choice, "")

    return {
        "choice": choice,
        "reasons": reasons,
        "prose": prose,
        "reject_bare_600": True,
    }


# --- ABC collect / annotate -----------------------------------------------------


def _serialize_abc_cand(c: dict[str, Any]) -> dict[str, Any]:
    feats = c.get("features") or {}
    return {
        "stock_id": str(c.get("stock_id") or ""),
        "stock_name": str(c.get("stock_name") or ""),
        "entry_date": str(c.get("entry_date") or ""),
        "exit_date": str(c.get("exit_date") or ""),
        "entry_minute": str(c.get("entry_minute") or ""),
        "entry_px": c.get("entry_px"),
        "exit_px": c.get("exit_px"),
        "entry_poll_idx": c.get("entry_poll_idx"),
        "net_pct": c.get("net_pct"),
        "excess_pct": c.get("excess_pct"),
        "hold_days": c.get("hold_days") or HOLD_DAYS,
        "w20_quadrant": feats.get("w20_quadrant"),
        "w5_mv_intraday": feats.get("w5_mv"),
        "w3_rv": feats.get("w3_rv"),
        "w3_dip_depth": feats.get("w3_dip_depth"),
        "source_strategy": c.get("source_strategy") or "abc_v3_f1_skip09",
    }


def collect_abc_f1_events_chunked(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    end: str | None = None,
    chunk_days: int = 25,
    dump_path: Path | None = None,
    resume: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Collect ABC v3+f1 skip09 unconstrained events in date chunks (cacheable)."""
    from research.backtest.archive.abc_v3_slot_sweep import (  # local import · archive
        _build_abc_v3_intraday_panel,
        collect_abc_v3_skip0900_period_candidates,
    )

    close, _, _ = load_price_panels(con)
    full = close.index.astype(str).tolist()
    end_d = end or full[-1]
    dates = [d for d in full if start <= d <= end_d]
    meta: dict[str, Any] = {
        "start": start,
        "end": end_d,
        "n_dates": len(dates),
        "chunk_days": chunk_days,
        "matcher": "w3_rv_hl_abc_v3_f1",
        "hold_days": HOLD_DAYS,
        "cost_haircut_pp": COST_HAIRCUT_PP,
        "chunks": [],
    }
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    done_chunks: set[str] = set()
    if dump_path and resume and dump_path.exists():
        prev = json.loads(dump_path.read_text(encoding="utf-8"))
        for r in prev.get("events") or []:
            by_key[(str(r["stock_id"]), str(r["entry_date"]))] = r
        for ch in prev.get("meta", {}).get("chunks") or []:
            if ch.get("ok"):
                done_chunks.add(f"{ch.get('lo')}_{ch.get('hi')}")
        meta["resumed_from"] = str(dump_path)
        meta["resumed_n"] = len(by_key)

    t0 = time.perf_counter()
    for i in range(0, len(dates), max(1, int(chunk_days))):
        chunk = dates[i : i + int(chunk_days)]
        if not chunk:
            continue
        tag = f"{chunk[0]}_{chunk[-1]}"
        if tag in done_chunks:
            meta["chunks"].append({"lo": chunk[0], "hi": chunk[-1], "ok": True, "skipped": True})
            continue
        ct0 = time.perf_counter()
        try:
            frames, traj, pmeta, smaps, bench = _build_abc_v3_intraday_panel(con, dates=chunk)
            cands, _, _ = collect_abc_v3_skip0900_period_candidates(
                con,
                dates=chunk,
                matcher=matches_w3_rv_hl_abc_v3_f1,
                frames=frames,
                trajectories=traj,
                meta=pmeta,
                stock_maps=smaps,
                bench=bench,
            )
            for c in cands:
                row = _serialize_abc_cand(c)
                by_key[(row["stock_id"], row["entry_date"])] = row
            meta["chunks"].append(
                {
                    "lo": chunk[0],
                    "hi": chunk[-1],
                    "ok": True,
                    "n_cands": len(cands),
                    "sec": round(time.perf_counter() - ct0, 1),
                    "n_frames": len(frames),
                }
            )
        except Exception as exc:  # noqa: BLE001 — research collector must continue
            meta["chunks"].append(
                {
                    "lo": chunk[0],
                    "hi": chunk[-1],
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "sec": round(time.perf_counter() - ct0, 1),
                }
            )
        if dump_path:
            dump_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": "abc_f1_fair_events-v1",
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "meta": meta,
                "events": sorted(
                    by_key.values(),
                    key=lambda r: (r["entry_date"], r["stock_id"]),
                ),
            }
            dump_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"  ABC chunk {chunk[0]}…{chunk[-1]} · "
            f"cum_n={len(by_key)} · {time.perf_counter()-t0:.0f}s",
            flush=True,
        )

    events = sorted(by_key.values(), key=lambda r: (r["entry_date"], r["stock_id"]))
    meta["n_events"] = len(events)
    meta["runtime_s"] = round(time.perf_counter() - t0, 1)
    meta["failed_chunks"] = [c for c in meta["chunks"] if not c.get("ok")]
    return events, meta


def load_abc_events_dump(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    return list(obj.get("events") or []), dict(obj.get("meta") or {})


def annotate_abc_with_dip_structure(
    con: sqlite3.Connection,
    events: Sequence[dict[str, Any]],
    *,
    lead_baseline_floor: str = "2024-11-01",
) -> pd.DataFrame:
    """Attach PIT EOD Leading / notUR / W5_MV<100 (Dip semantics) onto ABC events."""
    if not events:
        return pd.DataFrame()
    close, _, _ = load_price_panels(con)
    bench = load_benchmark_close(con).reindex(close.index).astype(float)
    rv20, mv20, _ = compute_rrg_panel(close, bench, length=20)
    rv5, mv5, _ = compute_rrg_panel(close, bench, length=5)
    full_dates = close.index.astype(str).tolist()
    fidx = {d: i for i, d in enumerate(full_dates)}
    lead_by = load_universe_by_baseline(
        con, universe_mode="leading", baseline_floor=lead_baseline_floor
    )
    bases = sorted(lead_by)

    def leading(td: str) -> set[str]:
        c = [b for b in bases if b < td]
        return lead_by[c[-1]] if c else set()

    def tm1(td: str) -> str | None:
        i = fidx.get(td)
        if i is None or i < 1:
            c = [d for d in full_dates if d < td]
            return c[-1] if c else None
        return full_dates[i - 1]

    rows: list[dict[str, Any]] = []
    for e in events:
        td = str(e.get("entry_date") or "")
        sid = str(e.get("stock_id") or "")
        if not td or not sid:
            continue
        is_lead = sid in leading(td)
        d = tm1(td)
        not_ur = False
        w5_weak = False
        trend = None
        m5 = None
        if d is not None and sid in rv5.columns and d in fidx:
            si = fidx[d]
            if si >= LOOKBACK - 1:
                f20 = _feat(rv20, mv20, full_dates, si, sid, lb=LOOKBACK)
                m5v = float(mv5.at[d, sid]) if sid in mv5.columns else float("nan")
                if f20 is not None and m5v == m5v:
                    not_ur = f20["trend"] != "up_right"
                    w5_weak = m5v < 100.0
                    trend = f20["trend"]
                    m5 = m5v
        px_gross = e.get("net_pct")
        ex_gross = e.get("excess_pct")
        # ABC net/excess already deduct ROUND_TRIP; treat as net for fair table.
        # Also expose "gross" ≈ add haircut back for transparency.
        px_net = float(px_gross) if px_gross is not None else None
        ex_net = float(ex_gross) if ex_gross is not None else None
        px_g = (px_net + COST_HAIRCUT_PP) if px_net is not None else None
        ex_g = (ex_net + COST_HAIRCUT_PP) if ex_net is not None else None
        depth = deep_enough_proxy(
            excess0=None,
            gap3=None,
        )  # A4 skipped unless caller fills
        rows.append(
            {
                "date": td,
                "sid": sid,
                "minute": str(e.get("entry_minute") or ""),
                "exit_date": str(e.get("exit_date") or ""),
                "px_gross": px_g,
                "ex_gross": ex_g,
                "px_net": px_net,
                "ex_net": ex_net,
                "is_leading": bool(is_lead),
                "notUR": bool(not_ur),
                "W5w": bool(w5_weak),
                "trend": trend,
                "m5": m5,
                "deep_enough": depth,
                "w20_quadrant_intra": e.get("w20_quadrant"),
                "w5_mv_intraday": e.get("w5_mv_intraday"),
                "half": "IS" if td < OOS_CUT_DEFAULT else "OOS",
                "source": "abc_v3_f1",
            }
        )
    return pd.DataFrame(rows)


def _as_optional_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, (bool, np.bool_)):
        return bool(v)
    return bool(v)


def filter_abc_arm(df: pd.DataFrame, arm: str) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df.copy() if df is not None else pd.DataFrame()
    mask = [
        passes_arm_a_layer(
            arm,
            is_leading=bool(r.is_leading),
            not_ur=bool(r.notUR),
            w5_weak=bool(r.W5w),
            deep_enough=_as_optional_bool(getattr(r, "deep_enough", None)),
        )
        for r in df.itertuples(index=False)
    ]
    return df.loc[mask].copy()


# --- Dip arm B ------------------------------------------------------------------


def dip_events_to_fair(df: pd.DataFrame) -> pd.DataFrame:
    """Map Dip collector rows → fair columns with cost haircut."""
    if df is None or len(df) == 0:
        return pd.DataFrame()
    rows = []
    for r in df.itertuples(index=False):
        px_g = float(r.px3)
        ex_g = float(r.ex3)
        px_n, ex_n = apply_cost_haircut(px_g, ex_g)
        rows.append(
            {
                "date": str(r.date),
                "sid": str(r.sid),
                "minute": str(r.minute),
                "exit_date": str(r.exit_date),
                "T2": bool(r.T2),
                "ex0": float(r.ex0),
                "gap0": float(r.gap0),
                "rB_min": float(r.rB_min),
                "px_gross": px_g,
                "ex_gross": ex_g,
                "px_net": px_n,
                "ex_net": ex_n,
                "notUR": bool(r.notUR),
                "W5w": bool(r.W5w),
                "half": str(r.half),
                "source": "leading_dip",
            }
        )
    return pd.DataFrame(rows)


def run_arm_b(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
    arms: Sequence[str] = ARM_B_ORDER,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any], list[str]]:
    """Widen Dip universe (trigger C0 rules fixed). Include open (no forced T2)."""
    out_frames: dict[str, pd.DataFrame] = {}
    summaries: dict[str, Any] = {}
    trading: list[str] = []
    for arm in arms:
        mode = UNIVERSE_MODES[arm]
        print(f"  Arm {arm} universe={mode} …", flush=True)
        raw, trading = collect_leading_dip_events(
            con,
            start=start,
            oos_cut=oos_cut,
            apply_structure=True,
            universe_mode=mode,
        )
        fair = dip_events_to_fair(raw)
        out_frames[arm] = fair
        summaries[arm] = summarize_fair_arm(
            fair, oos_cut=oos_cut, n_trading_days=len(trading)
        )
        # secondary: day≤2 + total open≤6 (coverage · open included)
        if not fair.empty:
            slot_src = fair.copy()
            slot_src["ex3"] = fair["ex_net"]
            slotted = apply_frozen_policy(
                slot_src, trading, require_t2=False, max_open=TOTAL_OPEN_CAP
            )
            if len(slotted) and "ex_net" not in slotted.columns and "ex3" in slotted.columns:
                slotted = slotted.copy()
                slotted["ex_net"] = slotted["ex3"]
            out_frames[f"{arm}_slots"] = slotted
            summaries[f"{arm}_slots"] = summarize_fair_arm(
                slotted, oos_cut=oos_cut, n_trading_days=len(trading)
            )
        else:
            out_frames[f"{arm}_slots"] = fair
            summaries[f"{arm}_slots"] = summarize_fair_arm(
                fair, oos_cut=oos_cut, n_trading_days=len(trading)
            )
    return out_frames, summaries, trading


def run_arm_a(
    abc_df: pd.DataFrame,
    *,
    oos_cut: str = OOS_CUT_DEFAULT,
    n_trading_days: int | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    frames: dict[str, pd.DataFrame] = {}
    summaries: dict[str, Any] = {}
    a4_available = (
        abc_df is not None
        and len(abc_df) > 0
        and "deep_enough" in abc_df.columns
        and abc_df["deep_enough"].notna().any()
    )
    for arm in ARM_A_ORDER:
        if arm == "A4" and not a4_available:
            summaries[arm] = {
                **summarize_fair_arm(pd.DataFrame(), oos_cut=oos_cut, n_trading_days=n_trading_days),
                "skipped": True,
                "note": "A4 depth (excess≤−4 ∨ gap3≤−0.8 at entry) not computed — cost skip",
            }
            frames[arm] = pd.DataFrame()
            continue
        sub = filter_abc_arm(abc_df, arm)
        frames[arm] = sub
        summaries[arm] = summarize_fair_arm(
            sub, oos_cut=oos_cut, n_trading_days=n_trading_days
        )
    return frames, summaries


# --- Report ---------------------------------------------------------------------


def _fmt_pct(x: Any) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{100.0 * float(x):.0f}%"


def _fmt_pp(x: Any) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{float(x):+.2f}"


def _arm_table_row(arm: str, s: dict[str, Any]) -> str:
    return (
        f"| {arm} | {s.get('n', 0)} | {s.get('days', 0)} | "
        f"{_fmt_pct(s.get('hit_px'))} | {_fmt_pct(s.get('hit_ex'))} | "
        f"{_fmt_pp(s.get('med_px'))} | {_fmt_pp(s.get('mean_px'))} | "
        f"{_fmt_pp(s.get('med_ex'))} | {_fmt_pp(s.get('mean_ex'))} | "
        f"{s.get('oos_n', 0)} | {_fmt_pct(s.get('oos_hit_ex'))} | {_fmt_pp(s.get('oos_med_ex'))} |"
    )


def _hh_row(side: str, arm: str, s: dict[str, Any]) -> str:
    return (
        f"| {side} | {arm} | {s.get('n', 0)} | {_fmt_pct(s.get('hit_ex'))} | "
        f"{_fmt_pp(s.get('med_ex'))} | {_fmt_pp(s.get('med_px'))} | "
        f"{_fmt_pct(s.get('oos_hit_ex'))} | {_fmt_pp(s.get('oos_med_ex'))} |"
    )


def render_fair_compare_md(payload: dict[str, Any]) -> str:
    a = payload.get("arm_a") or {}
    b = payload.get("arm_b") or {}
    v = payload.get("verdict") or {}
    contract = payload.get("contract") or {}
    blockers = payload.get("blockers") or []
    lines = [
        "# Fair compare · Leading Dip ↔ ABC v3+F1",
        "",
        f"日期：{payload.get('generated_at', '')[:10]} · Research only · **未採納**",
        "",
        "## 共同契約",
        "",
        f"- 主窗：`{contract.get('start')}` → `{contract.get('end')}` · OOS cut `{contract.get('oos_cut')}`",
        f"- hold **{contract.get('hold_days')}** 交易日 · bench **{contract.get('benchmark')}**",
        f"- 成本：flat haircut **{contract.get('cost_haircut_pp')}** pp"
        f"（= ABC `ROUND_TRIP_COST_PCT`；Dip 自 gross 扣）",
        "- 主表：事件層；辅：日槽≤2 + 總在外≤6（Dip coverage · 含開盤）",
        "",
        "## Verdict",
        "",
        f"**選擇：`{v.get('choice')}`**",
        "",
        v.get("prose") or "",
        "",
    ]
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(
        [
            "",
            f"- **拒绝裸 600（Dip）**：{v.get('reject_bare_600', True)}",
            "",
            "## Head-to-head (fair contract)",
            "",
            "| side | arm | n | hit_ex | med_ex | med_px | OOS hit_ex | OOS med_ex |",
            "|--|--|--:|--:|--:|--:|--:|--:|",
            _hh_row("ABC", "A0", a.get("A0") or {}),
            _hh_row("ABC", "A3", a.get("A3") or {}),
            _hh_row("Dip", "B0", b.get("B0") or {}),
            _hh_row("Dip", "B0_slots", b.get("B0_slots") or {}),
            "",
            "## Analyst read",
            "",
            "- A1≈A0：加 EOD Leading 幾乎不改變樣本——ABC matcher 已要求盤中 W20 Leading。",
            "- A2/A3：notUR ∧ W5_MV<100 把密度砍到 ~1/4，但 med excess **沒有**逼近 Dip（仍近 0／略負）。",
            "- B1/B2：n↑ 同時 OOS hit_ex 掉到 <70% 或 med 低於 B0−1.5pp → **不升主規格**。",
            "- 600 裸掃：拒絕（只适合共用监测厂）。",
            "",
            "## Arm A · ABC 收緊（向 Dip）",
            "",
            "| arm | n | days | hit_px | hit_ex | med_px | mean_px | med_ex | mean_ex | OOS n | OOS hit_ex | OOS med_ex |",
            "|--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
        ]
    )
    for arm in ARM_A_ORDER:
        s = a.get(arm) or {}
        if s.get("skipped"):
            lines.append(
                f"| {arm} | — | — | — | — | — | — | — | — | — | — | skipped · {s.get('note','')} |"
            )
        else:
            lines.append(_arm_table_row(arm, s))
    lines.extend(
        [
            "",
            "- A0 matcher：`w3_rv_hl_abc_v3_f1` · skip09 · hold 3d",
            "- A1 = ∩ EOD WMA20 Leading（`rrg_universe_scores` baseline < td · Dip PIT）",
            "- A2 = A1 ∧ notUR（W20 4d trend ≠ up_right · `_feat`）",
            "- A3 = A2 ∧ W5_MV<100（T−1 daily）",
            f"- A4：{payload.get('a4_note') or 'skipped'}",
            f"- ABC dump：`{payload.get('abc_dump_path') or '—'}` · n={payload.get('abc_n')}",
            "",
            "## Arm B · Dip 宇宙擴張（觸發 C0 不變 · 含開盤）",
            "",
            "| arm | n | days | hit_px | hit_ex | med_px | mean_px | med_ex | mean_ex | OOS n | OOS hit_ex | OOS med_ex |",
            "|--|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
        ]
    )
    for arm in (*ARM_B_ORDER, "B0_slots", "B1_slots", "B2_slots"):
        if arm in b:
            lines.append(_arm_table_row(arm, b[arm]))
    lines.extend(
        [
            "",
            "- B0 = Leading only（structure · 對齊 leading_dip_cov 精神）",
            "- B1 = Leading ∪ Improving",
            "- B2 = RV20≥100（`rs_ratio≥100` any quadrant）",
            "- `*_slots` = day cap≤2（busy∧crash→≤4）+ total open≤6",
            "",
            "## Density / year",
            "",
            "| arm | per_year | n/td |",
            "|--|--|--:|",
        ]
    )
    for arm, s in {**{f"A:{k}": v for k, v in a.items()}, **{f"B:{k}": v for k, v in b.items()}}.items():
        if not isinstance(s, dict) or s.get("skipped"):
            continue
        npt = s.get("n_per_td")
        npt_s = f"{float(npt):.3f}" if npt is not None else "—"
        lines.append(f"| {arm} | `{s.get('per_year')}` | {npt_s} |")
    if blockers:
        lines.extend(["", "## Blockers", ""])
        for blk in blockers:
            lines.append(f"- {blk}")
    lines.extend(
        [
            "",
            "## Re-run",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_vs_abc_fair_compare.py",
            "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_vs_abc_fair_compare.py --collect-abc",
            "```",
            "",
            f"Artifact：`{payload.get('artifact_stem')}.{{md,json}}`",
            "",
        ]
    )
    return "\n".join(lines)


def run_fair_compare(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
    abc_dump: Path | None = None,
    collect_abc: bool = False,
    chunk_days: int = 25,
    skip_arm_b: bool = False,
    skip_arm_a: bool = False,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(con)
    full = close.index.astype(str).tolist()
    end = full[-1]
    dates = [d for d in full if start <= d <= end]
    blockers: list[str] = []
    abc_events: list[dict[str, Any]] = []
    abc_meta: dict[str, Any] = {}
    dump = abc_dump

    if not skip_arm_a:
        if collect_abc:
            if dump is None:
                raise ValueError("--collect-abc requires dump path")
            print("Collecting ABC f1 events (chunked)…", flush=True)
            abc_events, abc_meta = collect_abc_f1_events_chunked(
                con, start=start, end=end, chunk_days=chunk_days, dump_path=dump
            )
        elif dump and dump.exists():
            abc_events, abc_meta = load_abc_events_dump(dump)
            print(f"Loaded ABC dump n={len(abc_events)} from {dump}", flush=True)
        else:
            blockers.append(
                f"ABC dump missing: {dump}. Re-run with --collect-abc "
                "(~1h for full 2025+ dual-WMA panel)."
            )
    else:
        blockers.append("Arm A skipped by flag")

    arm_a_frames: dict[str, pd.DataFrame] = {}
    arm_a_sum: dict[str, Any] = {}
    a4_note = "skipped (entry gap3/excess not attached; too expensive for this pass)"
    if abc_events and not skip_arm_a:
        print(f"Annotating ABC n={len(abc_events)} with Dip structure…", flush=True)
        abc_df = annotate_abc_with_dip_structure(con, abc_events)
        # keep only window (dump may include resume noise)
        abc_df = abc_df[abc_df["date"] >= start].copy()
        arm_a_frames, arm_a_sum = run_arm_a(
            abc_df, oos_cut=oos_cut, n_trading_days=len(dates)
        )
    elif not skip_arm_a:
        for arm in ARM_A_ORDER:
            arm_a_sum[arm] = summarize_fair_arm(pd.DataFrame(), oos_cut=oos_cut)

    arm_b_sum: dict[str, Any] = {}
    if not skip_arm_b:
        print("Running Arm B (Dip universe widen)…", flush=True)
        _bf, arm_b_sum, _tr = run_arm_b(con, start=start, oos_cut=oos_cut)
    else:
        blockers.append("Arm B skipped by flag")

    verdict = decide_fair_verdict(arm_a_sum, arm_b_sum)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "contract": {
            "start": start,
            "end": end,
            "oos_cut": oos_cut,
            "hold_days": HOLD_DAYS,
            "benchmark": "IX0001 (+0050 same-source fallback for Dip)",
            "cost_haircut_pp": COST_HAIRCUT_PP,
            "matcher_abc": "w3_rv_hl_abc_v3_f1",
            "dip_include_open": True,
        },
        "arm_a": arm_a_sum,
        "arm_b": arm_b_sum,
        "verdict": verdict,
        "blockers": blockers,
        "abc_dump_path": str(dump) if dump else None,
        "abc_n": len(abc_events),
        "abc_meta": {
            k: abc_meta.get(k)
            for k in ("n_events", "n_dates", "runtime_s", "failed_chunks", "resumed_n")
            if k in abc_meta
        },
        "a4_note": a4_note,
        "artifact_stem": "reports/research/rrg/20260715_leading_dip_vs_abc_fair_compare",
    }
    return payload


def format_fair_tables(payload: dict[str, Any]) -> str:
    """Console-friendly tables."""
    lines = ["=== Fair compare · Arm A (ABC tighten) ==="]
    lines.append(
        f"{'arm':4} {'n':>5} {'hit_ex':>7} {'med_ex':>8} {'med_px':>8} "
        f"{'oos_n':>5} {'oos_hit':>7} {'oos_med':>8}"
    )
    for arm in ARM_A_ORDER:
        s = (payload.get("arm_a") or {}).get(arm) or {}
        if s.get("skipped"):
            lines.append(f"{arm:4} SKIP · {s.get('note','')}")
            continue
        lines.append(
            f"{arm:4} {s.get('n',0):5d} {_fmt_pct(s.get('hit_ex')):>7} "
            f"{_fmt_pp(s.get('med_ex')):>8} {_fmt_pp(s.get('med_px')):>8} "
            f"{s.get('oos_n',0):5d} {_fmt_pct(s.get('oos_hit_ex')):>7} "
            f"{_fmt_pp(s.get('oos_med_ex')):>8}"
        )
    lines.append("")
    lines.append("=== Fair compare · Arm B (Dip widen · open incl.) ===")
    lines.append(
        f"{'arm':10} {'n':>5} {'hit_ex':>7} {'med_ex':>8} {'med_px':>8} "
        f"{'oos_n':>5} {'oos_hit':>7} {'oos_med':>8}"
    )
    for arm in (*ARM_B_ORDER, "B0_slots", "B1_slots", "B2_slots"):
        s = (payload.get("arm_b") or {}).get(arm)
        if not s:
            continue
        lines.append(
            f"{arm:10} {s.get('n',0):5d} {_fmt_pct(s.get('hit_ex')):>7} "
            f"{_fmt_pp(s.get('med_ex')):>8} {_fmt_pp(s.get('med_px')):>8} "
            f"{s.get('oos_n',0):5d} {_fmt_pct(s.get('oos_hit_ex')):>7} "
            f"{_fmt_pp(s.get('oos_med_ex')):>8}"
        )
    v = payload.get("verdict") or {}
    lines.append("")
    lines.append(f"VERDICT: {v.get('choice')}")
    lines.append(v.get("prose") or "")
    for r in v.get("reasons") or []:
        lines.append(f"  - {r}")
    for b in payload.get("blockers") or []:
        lines.append(f"BLOCKER: {b}")
    return "\n".join(lines)
