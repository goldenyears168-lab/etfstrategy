"""Leading Dip sleeve · Research frozen-spec self-validator.

Frozen ids:
  * ``leading_dip`` — **quality** track (forced T2+)
  * ``leading_dip_cov`` — **coverage** track (same structure + slots, drop T2 only)

Trader-facing rule (quality · one breath)::

    昨收 Leading ∧ gap3≤−0.80 ∩ excess≤−4
    ∧ T−1 W20_4d ≠ up_right ∧ T−1 W5_MV<100
    ∧ minute>09:30
    → 日槽 deepest-excess ≤2（忙≥3 ∧ rB_min≤−1.5 → ≤4；硬帽 4）
    → 總在外≤6（滿則跳過新進；同日 deepest 填剩餘槽）
    → hold T+3 close
    觀測：T−1 bench 20d ∈ [−5%, 0)（watch-only · 非硬暫停）

Coverage track drops only ``minute>09:30``; all other legs identical (incl. total-open cap).

Validation was Research-layer; **採納 2026-07-16** → ``config/strategy.yaml`` ·
``leading-dip`` (coverage) + ``leading-dip-mid`` (open-anchor ≥10:00).
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Sequence

import numpy as np
import pandas as pd

from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_mono_daily_brief import LOOKBACK, _feat
from rrg_rotation import compute_rrg_panel, wma

# --- Frozen constants ------------------------------------------------------------

FROZEN_SPEC_ID = "leading_dip"
FROZEN_SPEC_ALIASES = ("Spec C''", "Spec C′′", "Spec C'", "Spec C′", "Spec C")
COVERAGE_SPEC_ID = "leading_dip_cov"  # dual-track coverage: drop T2 only

GAP3_MAX = -0.80  # % · RS/WMA3(RS)−1
EXCESS_MAX = -4.0  # pp vs yesterday close
T2_MINUTE = "09:30"  # exclusive: minute > T2_MINUTE
DEFAULT_DAY_CAP = 2
BUSY_N = 3
CRASH_RB_MIN = -1.5  # % · intraday bench low vs prior close
EXPAND_CAP = 4
HARD_CAP = 4
HOLD_DAYS = 3
TOTAL_OPEN_CAP = 6  # unit-notional concurrent names; skip new when full
MAX_ABS_EX0 = 25.0
MAX_ABS_EX3 = 30.0
OOS_CUT_DEFAULT = "2025-10-01"
START_DEFAULT = "2025-01-02"
KBAR_LOOKBACK_START = "2024-12-01"
MILD_DOWN_LO = -5.0
MILD_DOWN_HI = 0.0
MILD_DOWN_POLICY = "watch_only"  # not hard_pause (n small; Regime breadth still Strong)

# Soft gates (fail loudly if violated after cleaning)
GATE_OOS_HIT_MIN = 0.70
GATE_OOS_MED_MIN = 0.0
GATE_BUSY_CRASH_RHO_MAX = 0.35
GATE_CONCURRENT_PEAK_MAX = TOTAL_OPEN_CAP  # after total-open cap
GATE_EXPAND_MED_SLACK = 0.75  # expand OOS med may be this many pp below fixed≤2
GATE_CAP_OOS_MED_SLACK = 0.50  # capped may lose this many pp vs day-slots uncapped


@dataclass(frozen=True)
class SlotPolicy:
    """Daily slot selection after structure + T2 filter."""

    name: str
    default_cap: int = DEFAULT_DAY_CAP
    busy_n: int = BUSY_N
    crash_rb_min: float = CRASH_RB_MIN
    expand_cap: int = EXPAND_CAP
    hard_cap: int = HARD_CAP
    require_busy_and_crash: bool = True


FROZEN_SLOT_POLICY = SlotPolicy(name="busy_crash_expand")


@dataclass
class GateResult:
    name: str
    ok: bool
    detail: str
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    spec_id: str
    oos_cut: str
    n_events_structure: int
    n_t2: int
    n_frozen: int
    gates: list[GateResult]
    summaries: dict[str, dict[str, Any]]
    taxonomy: dict[str, Any]
    passed: bool
    n_coverage: int = 0
    effective_sample: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "oos_cut": self.oos_cut,
            "n_events_structure": self.n_events_structure,
            "n_t2": self.n_t2,
            "n_frozen": self.n_frozen,
            "n_coverage": self.n_coverage,
            "passed": self.passed,
            "gates": [asdict(g) for g in self.gates],
            "summaries": self.summaries,
            "taxonomy": self.taxonomy,
            "effective_sample": self.effective_sample,
        }


# --- Pure helpers (unit-tested) -------------------------------------------------


def day_slot_cap(
    n_signals: int,
    rb_min: float,
    *,
    policy: SlotPolicy = FROZEN_SLOT_POLICY,
) -> int:
    """Return day entry cap under busy∧crash expand policy."""
    n = max(0, int(n_signals))
    if n == 0:
        return 0
    cap = int(policy.default_cap)
    if (
        policy.require_busy_and_crash
        and n >= int(policy.busy_n)
        and float(rb_min) <= float(policy.crash_rb_min)
    ):
        cap = int(policy.expand_cap)
    elif not policy.require_busy_and_crash and n >= int(policy.busy_n):
        cap = int(policy.expand_cap)
    return min(cap, int(policy.hard_cap), n)


def rank_deepest_excess(
    rows: Sequence[dict[str, Any]],
    *,
    depth_key: str = "ex0",
) -> list[dict[str, Any]]:
    """Sort by depth ascending (most negative first), stable on sid."""
    return sorted(
        rows,
        key=lambda r: (
            float(r[depth_key]),
            str(r.get("sid", "")),
            str(r.get("minute", "")),
        ),
    )


def select_day_slots(
    day_rows: Sequence[dict[str, Any]],
    *,
    policy: SlotPolicy = FROZEN_SLOT_POLICY,
    rb_min_key: str = "rB_min",
    depth_key: str = "ex0",
) -> list[dict[str, Any]]:
    """Apply deepest ranking + day cap for one trade date."""
    if not day_rows:
        return []
    ranked = rank_deepest_excess(day_rows, depth_key=depth_key)
    rb = float(ranked[0].get(rb_min_key, 0.0))
    k = day_slot_cap(len(ranked), rb, policy=policy)
    return ranked[:k]


def apply_slot_policy(
    events: pd.DataFrame,
    *,
    policy: SlotPolicy = FROZEN_SLOT_POLICY,
    require_t2: bool = True,
    depth_key: str = "ex0",
) -> pd.DataFrame:
    """Filter T2+ (optional) then per-day deepest slot policy."""
    if events.empty:
        return events.copy()
    df = events
    if require_t2:
        df = df[df["T2"].astype(bool)].copy()
    if df.empty:
        return df
    picked: list[pd.DataFrame] = []
    for _, g in df.groupby("date", sort=True):
        rows = g.to_dict("records")
        sel = select_day_slots(rows, policy=policy, depth_key=depth_key)
        if sel:
            picked.append(pd.DataFrame(sel))
    if not picked:
        return df.iloc[0:0].copy()
    return pd.concat(picked, ignore_index=True)


def apply_total_open_cap(
    picks: pd.DataFrame,
    trading_dates: Sequence[str],
    *,
    max_open: int = TOTAL_OPEN_CAP,
    exit_col: str = "exit_date",
) -> pd.DataFrame:
    """Skip new entries when concurrent open (unit-notional) is already at ``max_open``.

    Occupancy is entry_date..exit_date inclusive. On each day, prior opens still
    held consume slots; remaining room is filled by deepest-excess day candidates
    already ranked in ``picks`` (do not re-rank across days).
    """
    if picks.empty or max_open <= 0:
        return picks.iloc[0:0].copy() if hasattr(picks, "iloc") else picks.copy()
    by_date: dict[str, list[dict[str, Any]]] = {
        str(d): g.sort_values("ex0").to_dict("records")
        for d, g in picks.groupby("date", sort=True)
    }
    accepted: list[dict[str, Any]] = []
    open_exits: list[str] = []
    for d in trading_dates:
        open_exits = [e for e in open_exits if str(e) >= str(d)]
        room = max(0, int(max_open) - len(open_exits))
        for r in by_date.get(str(d), [])[:room]:
            accepted.append(r)
            open_exits.append(str(r[exit_col]))
    if not accepted:
        return picks.iloc[0:0].copy()
    return pd.DataFrame(accepted)


def apply_frozen_policy(
    events: pd.DataFrame,
    trading_dates: Sequence[str],
    *,
    policy: SlotPolicy = FROZEN_SLOT_POLICY,
    require_t2: bool = True,
    max_open: int = TOTAL_OPEN_CAP,
) -> pd.DataFrame:
    """Day slots then total-open cap (frozen Research path)."""
    day = apply_slot_policy(events, policy=policy, require_t2=require_t2)
    return apply_total_open_cap(day, trading_dates, max_open=max_open)


def mild_down_stratum(picks: pd.DataFrame, *, oos_cut: str = OOS_CUT_DEFAULT) -> dict[str, Any]:
    """Split picks on mild_down flag; Research watch leaf (not a hard gate)."""
    if picks is None or len(picks) == 0 or "mild_down" not in picks.columns:
        return {"mild_down": summarize_trades(picks, oos_cut=oos_cut), "other": summarize_trades(picks, oos_cut=oos_cut), "policy": MILD_DOWN_POLICY}
    md = picks[picks["mild_down"].astype(bool)]
    other = picks[~picks["mild_down"].astype(bool)]
    return {
        "mild_down": summarize_trades(md, oos_cut=oos_cut),
        "other": summarize_trades(other, oos_cut=oos_cut),
        "policy": MILD_DOWN_POLICY,
        "mild_down_dates": sorted(md["date"].astype(str).unique().tolist()) if len(md) else [],
    }


def classify_busy_day(
    n: int,
    rb_min: float,
    rb_eod: float,
    *,
    busy_n: int = BUSY_N,
    crash_rb_min: float = CRASH_RB_MIN,
) -> str:
    """Taxonomy label for a signal-busy day."""
    if n < busy_n:
        return "quiet"
    tags: list[str] = []
    if rb_min <= crash_rb_min:
        tags.append("CRASH")
    elif rb_min <= -1.0:
        tags.append("soft_crash")
    if rb_eod >= 0.5:
        tags.append("green_eod")
    if abs(rb_min) < 0.5 and rb_eod > -0.5:
        tags.append("DETACH")
    return "+".join(tags) if tags else "mixed"


def summarize_trades(df: pd.DataFrame, *, oos_cut: str = OOS_CUT_DEFAULT) -> dict[str, Any]:
    if df is None or len(df) == 0:
        return {"n": 0, "hit": None, "med": None, "oos_n": 0, "oos_hit": None, "oos_med": None}
    oos = df[df["date"] >= oos_cut] if "half" not in df.columns else df[df["half"] == "OOS"]
    # Prefer explicit half if present and consistent
    if "half" in df.columns:
        oos = df[df["half"] == "OOS"]
    else:
        oos = df[df["date"] >= oos_cut]
    ex = df["ex3"].astype(float)
    out: dict[str, Any] = {
        "n": int(len(df)),
        "hit": float((ex > 0).mean()),
        "med": float(ex.median()),
        "mean": float(ex.mean()),
        "days": int(df["date"].nunique()),
        "oos_n": int(len(oos)),
    }
    if len(oos):
        ox = oos["ex3"].astype(float)
        out["oos_hit"] = float((ox > 0).mean())
        out["oos_med"] = float(ox.median())
    else:
        out["oos_hit"] = None
        out["oos_med"] = None
    return out


def concurrent_open_peak(
    picks: pd.DataFrame,
    trading_dates: Sequence[str],
    *,
    hold_days: int = HOLD_DAYS,
    exit_col: str = "exit_date",
) -> dict[str, float]:
    """Peak concurrent open names under hold-through-exit occupancy."""
    if picks.empty or not trading_dates:
        return {"peak": 0.0, "p95": 0.0, "days_ge4": 0.0, "days_ge6": 0.0}
    idx = {d: i for i, d in enumerate(trading_dates)}
    occ = np.zeros(len(trading_dates), dtype=float)
    for r in picks.itertuples(index=False):
        a = idx.get(getattr(r, "date"))
        b = idx.get(getattr(r, exit_col, None))
        if a is None:
            continue
        if b is None:
            b = min(a + hold_days, len(trading_dates) - 1)
        occ[a : b + 1] += 1.0
    return {
        "peak": float(occ.max()) if len(occ) else 0.0,
        "p95": float(np.percentile(occ, 95)) if len(occ) else 0.0,
        "days_ge4": float((occ >= 4).sum()),
        "days_ge6": float((occ >= 6).sum()),
    }


def spearman_rho(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman ρ without SciPy."""
    if len(x) < 3:
        return float("nan")
    xs = pd.Series(list(x), dtype=float)
    ys = pd.Series(list(y), dtype=float)
    mask = xs.notna() & ys.notna()
    xs, ys = xs[mask], ys[mask]
    if len(xs) < 3:
        return float("nan")
    rx = xs.rank()
    ry = ys.rank()
    return float(rx.corr(ry))


def first_hit_index(mask: pd.Series) -> int | None:
    hits = mask.fillna(False)
    if not bool(hits.any()):
        return None
    return int(hits.to_numpy().nonzero()[0][0])


def build_path_frame(
    m: pd.DataFrame,
    s0: float,
    b0: float,
    *,
    bench_wma_len: int = 5,
    b_warm: pd.Series | None = None,
) -> pd.DataFrame:
    """Attach gap3 (%) · excess vs yday · excess vs session open · optional depth.

    ``ex``: additive excess vs yesterday close (s0, b0).
    ``ex_open``: additive excess vs first aligned bar of the day (open).
    """
    f = m.copy()
    f["RS"] = f["S"] / f["B"]
    f["W"] = wma(f["RS"], 3)
    f["gap"] = (f["RS"] / f["W"] - 1.0) * 100.0
    f["ex"] = (f["S"] / s0 - 1.0) * 100.0 - (f["B"] / b0 - 1.0) * 100.0
    s_open = float(f["S"].iloc[0])
    b_open = float(f["B"].iloc[0])
    f["ex_open"] = (f["S"] / s_open - 1.0) * 100.0 - (f["B"] / b_open - 1.0) * 100.0
    b = f["B"].astype(float)
    n = max(1, int(bench_wma_len))
    if b_warm is not None and len(b_warm) > 0:
        warm = pd.Series(b_warm, dtype=float)
        bw_full = wma(pd.concat([warm, b], ignore_index=True), n)
        f["Bw"] = bw_full.iloc[len(warm) :].to_numpy()
    else:
        f["Bw"] = wma(b, n)
    f["Rw"] = f["S"] / f["Bw"]
    rw0 = float(f["Rw"].dropna().iloc[0]) if f["Rw"].notna().any() else float("nan")
    rs0 = float(f["RS"].iloc[0])
    f["d_open"] = (f["Rw"] / rw0 - 1.0) * 100.0
    f["d_rs_open"] = (f["RS"] / rs0 - 1.0) * 100.0
    return f


def passes_structure_filters(
    *,
    not_ur: bool,
    w5_weak: bool,
    require_not_ur: bool = True,
    require_w5_weak: bool = True,
) -> bool:
    if require_not_ur and not not_ur:
        return False
    if require_w5_weak and not w5_weak:
        return False
    return True


def is_t2_minute(minute: str, *, cutoff: str = T2_MINUTE) -> bool:
    return str(minute) > str(cutoff)


def mild_down_flag(bret20: float | None) -> bool:
    if bret20 is None or (isinstance(bret20, float) and math.isnan(bret20)):
        return False
    return MILD_DOWN_LO <= float(bret20) < MILD_DOWN_HI


def effective_sample_stats(picks: pd.DataFrame) -> dict[str, Any]:
    """Report trade-n, active days, and day-cluster independence proxies."""
    if picks is None or len(picks) == 0:
        return {
            "n_trades": 0,
            "n_days": 0,
            "n_day_clusters": 0,
            "mean_per_active_day": None,
            "max_same_day": 0,
            "per_year": {},
            "day_hit": None,
            "day_med_ex3": None,
        }
    day = picks.groupby("date", sort=True).agg(n=("sid", "size"), ex3=("ex3", "mean"))
    years = picks["date"].astype(str).str[:4].value_counts().sort_index().to_dict()
    return {
        "n_trades": int(len(picks)),
        "n_days": int(day.shape[0]),
        "n_day_clusters": int(day.shape[0]),  # same-day = 1 independent cluster
        "mean_per_active_day": float(day["n"].mean()),
        "max_same_day": int(day["n"].max()),
        "per_year": {str(k): int(v) for k, v in years.items()},
        "day_hit": float((day["ex3"] > 0).mean()),
        "day_med_ex3": float(day["ex3"].median()),
    }


def t1_soft_rank_key(row: dict[str, Any], *, t1_ex_penalty_pp: float = 1.0) -> tuple:
    """Deepest-excess ranking with optional soft penalty for T1 (minute≤09:30).

    T1 rows are treated as ``ex0 + penalty`` (less deep) so T2 wins ties on depth;
    a T1 still enters if it is deeper than T2 by more than the penalty.
    """
    ex = float(row["ex0"])
    if not bool(row.get("T2", False)):
        ex = ex + float(t1_ex_penalty_pp)
    return (ex, str(row.get("sid", "")), str(row.get("minute", "")))


def apply_slot_policy_t1_soft(
    events: pd.DataFrame,
    *,
    policy: SlotPolicy = FROZEN_SLOT_POLICY,
    t1_ex_penalty_pp: float = 1.0,
) -> pd.DataFrame:
    """Slot policy allowing T1, but soft-penalized vs T2 on deepest-excess rank."""
    if events.empty:
        return events.copy()
    picked: list[pd.DataFrame] = []
    for _, g in events.groupby("date", sort=True):
        rows = g.to_dict("records")
        ranked = sorted(rows, key=lambda r: t1_soft_rank_key(r, t1_ex_penalty_pp=t1_ex_penalty_pp))
        rb = float(ranked[0].get("rB_min", 0.0))
        k = day_slot_cap(len(ranked), rb, policy=policy)
        if k > 0:
            picked.append(pd.DataFrame(ranked[:k]))
    if not picked:
        return events.iloc[0:0].copy()
    return pd.concat(picked, ignore_index=True)


# --- Event collection ------------------------------------------------------------


def _load_kbar_by_day(con: sqlite3.Connection, sid: str, start: str) -> dict[str, pd.Series]:
    df = pd.read_sql(
        """SELECT trade_date, substr(minute,1,5) AS minute, close FROM stock_kbar_5m
           WHERE stock_id=? AND trade_date>=? ORDER BY trade_date, minute""",
        con,
        params=(sid, start),
    )
    df = df[(df.minute >= "09:00") & (df.minute <= "13:30")].drop_duplicates(
        ["trade_date", "minute"], keep="last"
    )
    return {d: g.set_index("minute")["close"] for d, g in df.groupby("trade_date")}


def load_universe_by_baseline(
    con: sqlite3.Connection,
    *,
    universe_mode: str = "leading",
    baseline_floor: str = "2024-11-01",
) -> dict[str, set[str]]:
    """PIT universe keyed by ``data_baseline_date`` from ``rrg_universe_scores``.

    Modes:
      * ``leading`` — WMA20 Leading only (frozen Dip)
      * ``leading_improving`` — Leading ∪ Improving
      * ``rv20_ge_100`` — ``rs_ratio >= 100`` any quadrant
    """
    mode = str(universe_mode or "leading").strip().lower()
    if mode == "leading":
        df = pd.read_sql(
            """SELECT data_baseline_date AS d, stock_id FROM rrg_universe_scores
               WHERE quadrant='leading' AND data_baseline_date>=?""",
            con,
            params=(baseline_floor,),
        )
    elif mode in ("leading_improving", "leading_or_improving"):
        df = pd.read_sql(
            """SELECT data_baseline_date AS d, stock_id FROM rrg_universe_scores
               WHERE quadrant IN ('leading','improving') AND data_baseline_date>=?""",
            con,
            params=(baseline_floor,),
        )
    elif mode in ("rv20_ge_100", "rv20>=100"):
        df = pd.read_sql(
            """SELECT data_baseline_date AS d, stock_id FROM rrg_universe_scores
               WHERE rs_ratio>=100 AND data_baseline_date>=?""",
            con,
            params=(baseline_floor,),
        )
    else:
        raise ValueError(f"unknown universe_mode={universe_mode!r}")
    return {str(d): set(g.stock_id.astype(str)) for d, g in df.groupby("d")}


def collect_leading_dip_events(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
    max_abs_ex0: float = MAX_ABS_EX0,
    max_abs_ex3: float = MAX_ABS_EX3,
    apply_structure: bool = True,
    gap3_max: float = GAP3_MAX,
    excess_max: float = EXCESS_MAX,
    w5_mv_max: float = 100.0,
    require_not_ur: bool = True,
    require_w5_weak: bool = True,
    lead_baseline_floor: str = "2024-11-01",
    kbar_lookback_start: str | None = None,
    universe_mode: str = "leading",
    trigger: str = "gap_excess",
    depth_max: float = -2.0,
    depth_sweep: Sequence[float] | None = None,
    bench_wma_len: int = 5,
    depth_col: str = "d_open",
    excess_anchor: str = "prev_close",
    minute_lo: str | None = None,
    minute_hi: str | None = None,
    hold_days: int = HOLD_DAYS,
    min_path_bars: int = 12,
    require_exit_anchor: bool = True,
) -> tuple[pd.DataFrame, list[str]]:
    """Build cleaned first-hit events for Leading ∧ gap∩excess (+ optional Spec structure).

    Thresholds default to frozen ``leading_dip`` (−0.80∩−4 · notUR · W5_MV<100).
    Ablation callers may loosen ``gap3_max`` / ``excess_max`` / ``w5_mv_max`` or drop
    structure legs; cleaning bounds stay hard.
    ``universe_mode`` widens the scan universe only (trigger rules unchanged).

    ``excess_anchor`` (for gap_excess / depth_excess):
      * ``prev_close`` — frozen excess vs yesterday close
      * ``open`` — excess vs first aligned bar of the session

    ``minute_lo`` / ``minute_hi``: inclusive HH:MM window for first-hit search
    (bars outside the window cannot trigger).

    ``hold_days``: exit at trading day T+N close (default frozen T+3). Outcome
    columns stay ``ex3`` / ``px3`` / ``exit_date`` for caller compatibility.

    Live Order poll should pass ``min_path_bars=3`` (WMA3) and
    ``require_exit_anchor=False`` so same-day incomplete sessions can trigger
    before 12 bars / before T+3 exists in the kbar calendar.
    """
    hold_n = max(1, int(hold_days))
    min_bars = max(3, int(min_path_bars))
    kbar_start = kbar_lookback_start or (
        "2024-06-01" if start < "2025-01-01" else KBAR_LOOKBACK_START
    )
    close, _, _ = load_price_panels(con)
    bench = load_benchmark_close(con).reindex(close.index).astype(float)
    rv20, mv20, _ = compute_rrg_panel(close, bench, length=20)
    rv5, mv5, _ = compute_rrg_panel(close, bench, length=5)
    full_dates = close.index.astype(str).tolist()
    fidx = {d: i for i, d in enumerate(full_dates)}

    ix = _load_kbar_by_day(con, "IX0001", kbar_start)
    p50 = _load_kbar_by_day(con, "0050", kbar_start)
    # Keep a short bench history before ``start`` so live polls can resolve
    # ``bprev`` / WMA warmstart when ``start`` is the session date.
    try:
        bench_floor = (date.fromisoformat(str(start)[:10]) - timedelta(days=21)).isoformat()
    except ValueError:
        bench_floor = start
    bench_days: dict[str, pd.Series] = {}
    bench_src: dict[str, str] = {}
    for d in sorted(set(ix) | set(p50)):
        if d < bench_floor:
            continue
        # Complete historical days need a full session; live same-day uses min_bars.
        need = 20 if d < start else min_bars
        if d in ix and len(ix[d]) >= need:
            bench_days[d] = ix[d]
            bench_src[d] = "IX0001"
        elif d in p50 and len(p50[d]) >= need:
            bench_days[d] = p50[d]
            bench_src[d] = "0050"
    trading_all = sorted(bench_days)
    trading = [d for d in trading_all if d >= start]
    td_idx = {d: i for i, d in enumerate(trading_all)}

    lead_by = load_universe_by_baseline(
        con, universe_mode=universe_mode, baseline_floor=lead_baseline_floor
    )
    bases = sorted(lead_by)

    def leading(td: str) -> set[str]:
        c = [b for b in bases if b < td]
        return lead_by[c[-1]] if c else set()

    daily_floor = min(lead_baseline_floor, start[:7] + "-01" if len(start) >= 7 else start)
    daily = pd.read_sql(
        "SELECT stock_id,trade_date,close FROM stock_daily_bars WHERE trade_date>=?",
        con,
        params=(daily_floor,),
    )
    dm: dict[str, dict[str, float]] = defaultdict(dict)
    for r in daily.itertuples(index=False):
        dm[r.stock_id][r.trade_date] = float(r.close)

    def prev_close(sid: str, td: str) -> float | None:
        p = sorted(d for d in dm.get(sid, {}) if d < td)
        return dm[sid][p[-1]] if p else None

    def bprev(td: str) -> float | None:
        src = bench_src[td]
        pr = [d for d in trading_all if d < td and bench_src[d] == src]
        return float(bench_days[pr[-1]].iloc[-1]) if pr else None

    def fwd(td: str, n: int) -> str | None:
        i = td_idx.get(td)
        return trading_all[i + n] if i is not None and i + n < len(trading_all) else None

    def tm1(td: str) -> str | None:
        i = fidx.get(td)
        if i is None or i < 1:
            c = [d for d in full_dates if d < td]
            return c[-1] if c else None
        return full_dates[i - 1]

    def struct(sid: str, td: str) -> dict[str, Any] | None:
        d = tm1(td)
        if d is None or sid not in rv5.columns:
            return None
        si = fidx[d]
        if si < LOOKBACK - 1:
            return None
        f20 = _feat(rv20, mv20, full_dates, si, sid, lb=LOOKBACK)
        if f20 is None:
            return None
        m5 = float(mv5.at[d, sid])
        if m5 != m5:
            return None
        return {
            "notUR": f20["trend"] != "up_right",
            "W5w": m5 < float(w5_mv_max),
            "trend": f20["trend"],
            "m5": m5,
        }

    ever: set[str] = set()
    for td in trading:
        ever |= leading(td)
    if not ever:
        return pd.DataFrame(), trading

    qmarks = ",".join("?" * len(ever))
    stock = pd.read_sql(
        f"""SELECT stock_id,trade_date,substr(minute,1,5) AS minute,close FROM stock_kbar_5m
            WHERE trade_date>=? AND stock_id IN ({qmarks})""",
        con,
        params=(start, *sorted(ever)),
    )
    stock = stock[(stock.minute >= "09:00") & (stock.minute <= "13:30")].drop_duplicates(
        ["stock_id", "trade_date", "minute"], keep="last"
    )
    by = {sid: g for sid, g in stock.groupby("stock_id")}

    bclose = {d: float(bench_days[d].iloc[-1]) for d in trading}
    bser = pd.Series(bclose).sort_index()
    bret20 = bser.pct_change(20) * 100.0

    if trigger not in ("gap_excess", "bench_wma_open", "depth_excess"):
        raise ValueError(f"unknown trigger={trigger!r}")
    if depth_col not in ("d_open", "d_rs_open"):
        raise ValueError(f"unknown depth_col={depth_col!r}")
    if excess_anchor not in ("prev_close", "open"):
        raise ValueError(f"unknown excess_anchor={excess_anchor!r}")
    ex_col = "ex_open" if excess_anchor == "open" else "ex"
    thr_list: list[float] | None = None
    if trigger in ("bench_wma_open", "depth_excess"):
        thr_list = list(depth_sweep) if depth_sweep is not None else [float(depth_max)]

    def _bench_warm(td: str) -> pd.Series | None:
        """Last (wma−1) same-source bench bars from prior trading day."""
        src = bench_src[td]
        pr = [d for d in trading_all if d < td and bench_src[d] == src]
        if not pr:
            return None
        need = max(0, int(bench_wma_len) - 1)
        if need <= 0:
            return None
        prev = bench_days[pr[-1]].astype(float)
        if len(prev) < need:
            return prev
        return prev.iloc[-need:]

    rows: list[dict[str, Any]] = []
    for td in trading:
        leads = leading(td)
        bp = bprev(td)
        if not leads or bp is None:
            continue
        d3e = fwd(td, hold_n)
        if require_exit_anchor:
            if not d3e or bench_src[td] != bench_src.get(d3e):
                continue
            B3 = float(bench_days[d3e].iloc[-1])
        elif d3e and bench_src[td] == bench_src.get(d3e):
            B3 = float(bench_days[d3e].iloc[-1])
        else:
            d3e = None
            B3 = float("nan")
        btoday = bench_days[td]
        Beod = float(btoday.iloc[-1])
        rb_min = (float(btoday.min()) / bp - 1.0) * 100.0
        rb_eod = (Beod / bp - 1.0) * 100.0
        t_prev = tm1(td)
        br20 = float(bret20.get(t_prev, float("nan"))) if t_prev is not None else float("nan")
        b_warm = _bench_warm(td) if trigger in ("bench_wma_open", "depth_excess") else None

        for sid in leads:
            g = by.get(sid)
            if g is None:
                continue
            today = g[g.trade_date == td].sort_values("minute").drop_duplicates("minute")
            if len(today) < min_bars:
                continue
            S0 = prev_close(sid, td)
            if not S0:
                continue
            st = struct(sid, td)
            if st is None:
                continue
            if apply_structure and not passes_structure_filters(
                not_ur=bool(st["notUR"]),
                w5_weak=bool(st["W5w"]),
                require_not_ur=require_not_ur,
                require_w5_weak=require_w5_weak,
            ):
                continue
            Sd: dict[str, float] = {}
            need_days = (td,) if d3e is None else (td, d3e)
            for d in need_days:
                if d in dm.get(sid, {}):
                    Sd[d] = dm[sid][d]
                else:
                    gg = g[g.trade_date == d]
                    if len(gg):
                        Sd[d] = float(gg.sort_values("minute").iloc[-1]["close"])
            if td not in Sd:
                continue
            if require_exit_anchor and (d3e is None or d3e not in Sd):
                continue
            m = (
                pd.DataFrame({"S": today.set_index("minute")["close"], "B": btoday})
                .dropna()
                .reset_index(names="minute")
            )
            if len(m) < min_bars:
                continue
            f = build_path_frame(
                m, S0, bp, bench_wma_len=bench_wma_len, b_warm=b_warm
            )
            if float(f[ex_col].abs().max()) > max_abs_ex0 * 3:
                continue

            in_win = pd.Series(True, index=f.index)
            if minute_lo is not None:
                in_win &= f["minute"].astype(str) >= str(minute_lo)
            if minute_hi is not None:
                in_win &= f["minute"].astype(str) <= str(minute_hi)

            hit_specs: list[tuple[float | None, int]] = []
            if trigger == "gap_excess":
                i = first_hit_index(
                    in_win
                    & (f["gap"] <= float(gap3_max))
                    & (f[ex_col] <= float(excess_max))
                )
                if i is not None:
                    hit_specs.append((None, i))
            elif trigger == "bench_wma_open":
                assert thr_list is not None
                series = f[depth_col]
                for thr in thr_list:
                    i = first_hit_index(in_win & (series <= float(thr)))
                    if i is not None:
                        hit_specs.append((float(thr), i))
            else:
                # depth_excess: keep excess≤−4, swap gap for open-depth
                assert thr_list is not None
                series = f[depth_col]
                ex_ok = f[ex_col] <= float(excess_max)
                for thr in thr_list:
                    i = first_hit_index(in_win & (series <= float(thr)) & ex_ok)
                    if i is not None:
                        hit_specs.append((float(thr), i))

            for thr_v, i in hit_specs:
                ex0 = float(f.loc[i, ex_col])
                if abs(ex0) > max_abs_ex0:
                    continue
                S_in = float(f.S.iloc[i])
                B_in = float(f.B.iloc[i])
                if (
                    d3e is not None
                    and d3e in Sd
                    and B3 == B3
                ):
                    ex3 = (Sd[d3e] / S_in - 1.0) * 100.0 - (B3 / B_in - 1.0) * 100.0
                    if abs(ex3) > max_abs_ex3:
                        continue
                    px3 = (Sd[d3e] / S_in - 1.0) * 100.0
                    exit_date = d3e
                elif require_exit_anchor:
                    continue
                else:
                    ex3 = float("nan")
                    px3 = float("nan")
                    exit_date = ""
                mm = str(f.loc[i, "minute"])
                rS = (S_in / S0 - 1.0) * 100.0
                rB = (B_in / bp - 1.0) * 100.0
                d0 = float(f.loc[i, depth_col]) if depth_col in f.columns else float("nan")
                row: dict[str, Any] = {
                    "date": td,
                    "sid": sid,
                    "minute": mm,
                    "T2": is_t2_minute(mm),
                    "ex0": ex0,
                    "ex0_yday": float(f.loc[i, "ex"]),
                    "ex0_open": float(f.loc[i, "ex_open"]),
                    "gap0": float(f.loc[i, "gap"]),
                    "d0": d0,
                    "ex3": float(ex3),
                    "px3": float(px3),
                    "rB_trig": rB,
                    "rS_trig": rS,
                    "rB_eod": rb_eod,
                    "rB_min": rb_min,
                    "exit_date": exit_date,
                    "notUR": bool(st["notUR"]),
                    "W5w": bool(st["W5w"]),
                    "m5": float(st["m5"]),
                    "bret20": br20,
                    "mild_down": mild_down_flag(br20),
                    "half": "IS" if td < oos_cut else "OOS",
                    "bench_src": bench_src[td],
                    "trigger": trigger,
                    "excess_anchor": excess_anchor,
                    "minute_lo": minute_lo,
                    "minute_hi": minute_hi,
                    "depth_col": (
                        depth_col if trigger in ("bench_wma_open", "depth_excess") else "gap_excess"
                    ),
                }
                if thr_v is not None:
                    row["depth_thr"] = thr_v
                rows.append(row)

    events = pd.DataFrame(rows)
    return events, trading


def evaluate_gates(
    events: pd.DataFrame,
    trading_dates: Sequence[str],
    *,
    oos_cut: str = OOS_CUT_DEFAULT,
) -> ValidationReport:
    """Run self-validation gates; ``passed`` is False if any gate fails."""
    struct = events.copy()
    t2 = struct[struct["T2"].astype(bool)].copy()
    day_slots = apply_slot_policy(struct, policy=FROZEN_SLOT_POLICY, require_t2=True)
    frozen = apply_total_open_cap(day_slots, trading_dates, max_open=TOTAL_OPEN_CAP)
    cov_day = apply_slot_policy(struct, policy=FROZEN_SLOT_POLICY, require_t2=False)
    coverage = apply_total_open_cap(cov_day, trading_dates, max_open=TOTAL_OPEN_CAP)

    fixed2_rows: list[pd.DataFrame] = []
    for _, g in t2.groupby("date", sort=True):
        ranked = rank_deepest_excess(g.to_dict("records"))
        fixed2_rows.append(pd.DataFrame(ranked[: min(2, len(ranked))]))
    fixed2 = pd.concat(fixed2_rows, ignore_index=True) if fixed2_rows else t2.iloc[0:0].copy()

    # T2 take-all (no slot cap) — rejected as Strategy policy; peak-bound contrast only
    take_all = t2.copy()

    summaries = {
        "structure_allday": summarize_trades(struct, oos_cut=oos_cut),
        "structure_t2": summarize_trades(t2, oos_cut=oos_cut),
        "frozen_day_slots_uncapped": summarize_trades(day_slots, oos_cut=oos_cut),
        "frozen_leading_dip": summarize_trades(frozen, oos_cut=oos_cut),
        "coverage_leading_dip_cov": summarize_trades(coverage, oos_cut=oos_cut),
        "fixed_le2_t2": summarize_trades(fixed2, oos_cut=oos_cut),
        "t2_take_all": summarize_trades(take_all, oos_cut=oos_cut),
    }
    eff_frozen = effective_sample_stats(frozen)
    eff_cov = effective_sample_stats(coverage)
    md_stratum = mild_down_stratum(frozen, oos_cut=oos_cut)

    # Busy ≠ crash correlation on structure events (day level)
    day = (
        struct.groupby("date")
        .agg(n=("sid", "size"), rB_min=("rB_min", "first"), rB_eod=("rB_eod", "first"))
        .reset_index()
    )
    rho = spearman_rho(day["n"].tolist(), day["rB_min"].tolist()) if len(day) else float("nan")
    busy = day[day["n"] >= BUSY_N]
    taxonomy_counts: dict[str, int] = defaultdict(int)
    for r in busy.itertuples(index=False):
        taxonomy_counts[
            classify_busy_day(int(r.n), float(r.rB_min), float(r.rB_eod))
        ] += 1

    peak_uncapped = concurrent_open_peak(day_slots, trading_dates)
    peak_frozen = concurrent_open_peak(frozen, trading_dates)
    peak_cov = concurrent_open_peak(coverage, trading_dates)
    peak_all = concurrent_open_peak(take_all, trading_dates)

    gates: list[GateResult] = []

    fr = summaries["frozen_leading_dip"]
    unc = summaries["frozen_day_slots_uncapped"]
    oos_n = int(fr.get("oos_n") or 0)
    oos_hit = fr.get("oos_hit")
    oos_med = fr.get("oos_med")
    gates.append(
        GateResult(
            name="oos_med_positive",
            ok=bool(oos_n >= 20 and oos_med is not None and oos_med > GATE_OOS_MED_MIN),
            detail=f"OOS n={oos_n} med={oos_med}",
            metrics={"oos_n": oos_n, "oos_med": oos_med},
        )
    )
    gates.append(
        GateResult(
            name="oos_hit_ge_70",
            ok=bool(oos_n >= 20 and oos_hit is not None and oos_hit >= GATE_OOS_HIT_MIN),
            detail=f"OOS hit={oos_hit}",
            metrics={"oos_n": oos_n, "oos_hit": oos_hit},
        )
    )

    t2s = summaries["structure_t2"]
    alls = summaries["structure_allday"]
    t2_ok = (
        t2s.get("oos_med") is not None
        and alls.get("oos_med") is not None
        and float(t2s["oos_med"]) >= float(alls["oos_med"]) - 0.25
    )
    gates.append(
        GateResult(
            name="t2_improves_vs_allday",
            ok=bool(t2_ok),
            detail=(
                f"T2 OOS med={t2s.get('oos_med')} hit={t2s.get('oos_hit')} | "
                f"all-day OOS med={alls.get('oos_med')} hit={alls.get('oos_hit')}"
            ),
            metrics={"t2": t2s, "allday": alls},
        )
    )

    f2 = summaries["fixed_le2_t2"]
    expand_ok = (
        fr.get("oos_med") is not None
        and f2.get("oos_med") is not None
        and float(fr["oos_med"]) >= float(f2["oos_med"]) - GATE_EXPAND_MED_SLACK
    )
    gates.append(
        GateResult(
            name="busy_crash_expand_preserves_oos_med",
            ok=bool(expand_ok),
            detail=(
                f"expand OOS med={fr.get('oos_med')} vs fixed≤2 OOS med={f2.get('oos_med')} "
                f"(slack {GATE_EXPAND_MED_SLACK})"
            ),
            metrics={"expand": fr, "fixed2": f2},
        )
    )

    rho_ok = not (isinstance(rho, float) and math.isnan(rho)) and abs(float(rho)) <= GATE_BUSY_CRASH_RHO_MAX
    # Also require that not all busy days are crash (DETACH/green exists or mixed)
    non_crash_busy = sum(v for k, v in taxonomy_counts.items() if "CRASH" not in k)
    taxonomy_ok = bool(len(busy) == 0 or non_crash_busy >= 1 or abs(float(rho)) <= 0.2)
    gates.append(
        GateResult(
            name="busy_neq_crash",
            ok=bool(rho_ok and taxonomy_ok),
            detail=f"|ρ(n,rB_min)|={abs(rho) if rho==rho else float('nan'):.3f} taxonomy={dict(taxonomy_counts)}",
            metrics={"rho": rho, "taxonomy": dict(taxonomy_counts), "busy_days": int(len(busy))},
        )
    )

    peak_ok = peak_frozen["peak"] <= GATE_CONCURRENT_PEAK_MAX and peak_frozen["peak"] < peak_all["peak"]
    gates.append(
        GateResult(
            name="concurrent_peak_bounded",
            ok=bool(peak_ok),
            detail=(
                f"frozen peak={peak_frozen['peak']:.0f} "
                f"(≤{GATE_CONCURRENT_PEAK_MAX}=TOTAL_OPEN_CAP) "
                f"uncapped_day_slots={peak_uncapped['peak']:.0f} "
                f"vs take-all peak={peak_all['peak']:.0f}"
            ),
            metrics={
                "frozen": peak_frozen,
                "uncapped_day_slots": peak_uncapped,
                "take_all": peak_all,
            },
        )
    )

    cap_ok = (
        fr.get("oos_med") is not None
        and unc.get("oos_med") is not None
        and float(fr["oos_med"]) >= float(unc["oos_med"]) - GATE_CAP_OOS_MED_SLACK
        and int(fr.get("n") or 0) <= int(unc.get("n") or 0)
    )
    gates.append(
        GateResult(
            name="total_open_cap_preserves_oos_med",
            ok=bool(cap_ok),
            detail=(
                f"cap{TOTAL_OPEN_CAP} OOS med={fr.get('oos_med')} n={fr.get('n')} "
                f"vs day_slots OOS med={unc.get('oos_med')} n={unc.get('n')} "
                f"(slack {GATE_CAP_OOS_MED_SLACK}; skipped={int(unc.get('n') or 0) - int(fr.get('n') or 0)})"
            ),
            metrics={"capped": fr, "day_slots": unc, "total_open_cap": TOTAL_OPEN_CAP},
        )
    )

    clean_ok = bool(
        len(struct) == 0
        or (
            (struct["ex0"].abs() <= MAX_ABS_EX0 + 1e-9).all()
            and (struct["ex3"].abs() <= MAX_ABS_EX3 + 1e-9).all()
        )
    )
    gates.append(
        GateResult(
            name="cleaning_bounds",
            ok=bool(clean_ok),
            detail=f"|ex0|≤{MAX_ABS_EX0} · |ex3|≤{MAX_ABS_EX3} · same-source bench on T→T+3",
            metrics={"n": int(len(struct))},
        )
    )

    # Dual-track coverage sleeve (drop T2 only): more n, OOS still healthy, not diluting quality track
    cov = summaries["coverage_leading_dip_cov"]
    cov_n = int(cov.get("n") or 0)
    cov_oos_n = int(cov.get("oos_n") or 0)
    cov_oos_hit = cov.get("oos_hit")
    cov_oos_med = cov.get("oos_med")
    cov_ok = bool(
        cov_n > int(fr.get("n") or 0)
        and cov_oos_n >= 20
        and cov_oos_hit is not None
        and cov_oos_hit >= GATE_OOS_HIT_MIN
        and cov_oos_med is not None
        and cov_oos_med > GATE_OOS_MED_MIN
        and oos_med is not None
        and float(cov_oos_med) >= float(oos_med) - 1.0  # ≤1pp OOS-med give-up vs quality
    )
    gates.append(
        GateResult(
            name="coverage_track_oos_healthy",
            ok=bool(cov_ok),
            detail=(
                f"{COVERAGE_SPEC_ID} n={cov_n} OOS n={cov_oos_n} "
                f"hit={cov_oos_hit} med={cov_oos_med} "
                f"(vs quality n={fr.get('n')} OOS med={oos_med}; slack 1.0pp)"
            ),
            metrics={"coverage": cov, "quality": fr, "peak_cov": peak_cov},
        )
    )

    passed = all(g.ok for g in gates)
    return ValidationReport(
        spec_id=FROZEN_SPEC_ID,
        oos_cut=oos_cut,
        n_events_structure=int(len(struct)),
        n_t2=int(len(t2)),
        n_frozen=int(len(frozen)),
        n_coverage=int(len(coverage)),
        gates=gates,
        summaries=summaries,
        taxonomy={
            "rho_n_vs_rb_min": rho,
            "busy_day_labels": dict(taxonomy_counts),
            "concurrent_frozen": peak_frozen,
            "concurrent_day_slots_uncapped": peak_uncapped,
            "concurrent_coverage": peak_cov,
            "concurrent_take_all": peak_all,
            "total_open_cap": TOTAL_OPEN_CAP,
            "mild_down_n": int(struct["mild_down"].sum()) if len(struct) and "mild_down" in struct else 0,
            "mild_down_stratum": md_stratum,
            "mild_down_policy": MILD_DOWN_POLICY,
        },
        effective_sample={"quality": eff_frozen, "coverage": eff_cov},
        passed=passed,
    )


def run_validation(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
) -> tuple[ValidationReport, pd.DataFrame]:
    events, trading = collect_leading_dip_events(con, start=start, oos_cut=oos_cut, apply_structure=True)
    report = evaluate_gates(events, trading, oos_cut=oos_cut)
    return report, events


def observe_day(
    con: sqlite3.Connection,
    day: str,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
    max_open: int = TOTAL_OPEN_CAP,
    require_t2: bool = True,
) -> dict[str, Any]:
    """Paper-observe one calendar day under frozen rules (no orders).

    Reconstructs open inventory from historical day-slots + total-open cap through
    ``day``, then reports eligible T2 triggers, day-slot picks, accepts, and skips.
    """
    events, trading = collect_leading_dip_events(
        con, start=start, oos_cut=oos_cut, apply_structure=True
    )
    day = str(day)
    if events.empty:
        return {
            "day": day,
            "ok": False,
            "reason": "no structure events in window",
            "spec_id": FROZEN_SPEC_ID if require_t2 else COVERAGE_SPEC_ID,
        }

    through_dates = [d for d in trading if d <= day]
    if day not in trading and not (events["date"].astype(str) == day).any():
        return {
            "day": day,
            "ok": False,
            "reason": f"day not in trading calendar / no kbar bench ({trading[0]}…{trading[-1]})",
            "spec_id": FROZEN_SPEC_ID if require_t2 else COVERAGE_SPEC_ID,
        }

    hist = events[events["date"].astype(str) <= day].copy()
    slots = apply_slot_policy(hist, policy=FROZEN_SLOT_POLICY, require_t2=require_t2)
    capped = apply_total_open_cap(slots, through_dates, max_open=max_open)

    day_struct = events[events["date"].astype(str) == day].copy()
    day_elig = day_struct[day_struct["T2"].astype(bool)].copy() if require_t2 else day_struct
    day_slot = slots[slots["date"].astype(str) == day].copy()
    day_acc = capped[capped["date"].astype(str) == day].copy()
    open_before = capped[
        (capped["date"].astype(str) < day) & (capped["exit_date"].astype(str) >= day)
    ].copy()
    skipped = day_slot[~day_slot["sid"].isin(set(day_acc["sid"].tolist()))].copy() if len(day_slot) else day_slot

    busy = bool(len(day_elig) >= BUSY_N)
    rb_min = float(day_elig["rB_min"].iloc[0]) if len(day_elig) else float("nan")
    day_cap = day_slot_cap(len(day_elig), rb_min if rb_min == rb_min else 0.0)
    room = max(0, int(max_open) - int(len(open_before)))
    mild = bool(day_elig["mild_down"].astype(bool).any()) if len(day_elig) and "mild_down" in day_elig else False

    def _rows(df: pd.DataFrame) -> list[dict[str, Any]]:
        if df is None or len(df) == 0:
            return []
        cols = [
            c
            for c in (
                "sid",
                "minute",
                "ex0",
                "gap0",
                "ex3",
                "T2",
                "mild_down",
                "rB_min",
                "exit_date",
                "bret20",
            )
            if c in df.columns
        ]
        return df[cols].to_dict("records")

    return {
        "ok": True,
        "day": day,
        "spec_id": FROZEN_SPEC_ID if require_t2 else COVERAGE_SPEC_ID,
        "require_t2": require_t2,
        "total_open_cap": int(max_open),
        "open_before_n": int(len(open_before)),
        "room": int(room),
        "day_slot_cap": int(day_cap),
        "busy": busy,
        "crash": bool(rb_min == rb_min and rb_min <= CRASH_RB_MIN),
        "rB_min": rb_min if rb_min == rb_min else None,
        "mild_down": mild,
        "mild_down_policy": MILD_DOWN_POLICY,
        "n_structure": int(len(day_struct)),
        "n_eligible": int(len(day_elig)),
        "n_day_slot": int(len(day_slot)),
        "n_accepted": int(len(day_acc)),
        "n_skipped_by_cap": int(len(skipped)),
        "open_before": _rows(open_before),
        "eligible": _rows(day_elig.sort_values("ex0") if len(day_elig) else day_elig),
        "day_slots": _rows(day_slot),
        "accepted": _rows(day_acc),
        "skipped_by_cap": _rows(skipped),
        "note": "paper-observe only · no Order-layer submit",
    }


def format_observe_text(obs: dict[str, Any]) -> str:
    lines = [
        f"=== Leading Dip paper-observe · day={obs.get('day')} · spec={obs.get('spec_id')} ===",
    ]
    if not obs.get("ok"):
        lines.append(f"NO ACTION: {obs.get('reason')}")
        return "\n".join(lines)
    lines.extend(
        [
            (
                f"open_before={obs.get('open_before_n')}/{obs.get('total_open_cap')} "
                f"room={obs.get('room')} · day_slot_cap={obs.get('day_slot_cap')} "
                f"(busy={obs.get('busy')} crash={obs.get('crash')} rB_min={obs.get('rB_min')})"
            ),
            (
                f"mild_down={obs.get('mild_down')} policy={obs.get('mild_down_policy')} · "
                f"eligible={obs.get('n_eligible')} day_slots={obs.get('n_day_slot')} "
                f"accepted={obs.get('n_accepted')} skipped_cap={obs.get('n_skipped_by_cap')}"
            ),
            "",
            "Accepted:",
        ]
    )
    acc = obs.get("accepted") or []
    if not acc:
        lines.append("  (none)")
    else:
        for r in acc:
            lines.append(
                f"  {r.get('sid')} @{r.get('minute')} ex0={r.get('ex0'):+.2f} "
                f"exit={r.get('exit_date')} mild={r.get('mild_down')}"
            )
    skip = obs.get("skipped_by_cap") or []
    if skip:
        lines.append("Skipped (total-open full):")
        for r in skip:
            lines.append(f"  {r.get('sid')} @{r.get('minute')} ex0={r.get('ex0'):+.2f}")
    if obs.get("mild_down"):
        lines.append("")
        lines.append("NOTE: mild_down leaf — watch-only / 慎加碼（非硬暫停）")
    lines.append("")
    lines.append(str(obs.get("note") or ""))
    return "\n".join(lines)


def _ablation_gate_snapshot(
    picks: pd.DataFrame,
    *,
    oos_cut: str,
    label: str,
) -> dict[str, Any]:
    s = summarize_trades(picks, oos_cut=oos_cut)
    eff = effective_sample_stats(picks)
    oos_n = int(s.get("oos_n") or 0)
    oos_hit = s.get("oos_hit")
    oos_med = s.get("oos_med")
    gate_oos_med = bool(oos_n >= 20 and oos_med is not None and oos_med > GATE_OOS_MED_MIN)
    gate_oos_hit = bool(oos_n >= 20 and oos_hit is not None and oos_hit >= GATE_OOS_HIT_MIN)
    return {
        "lever": label,
        "n": s.get("n"),
        "hit": s.get("hit"),
        "med": s.get("med"),
        "days": s.get("days"),
        "oos_n": oos_n,
        "oos_hit": oos_hit,
        "oos_med": oos_med,
        "n_day_clusters": eff["n_day_clusters"],
        "max_same_day": eff["max_same_day"],
        "per_year": eff["per_year"],
        "day_hit": eff["day_hit"],
        "day_med_ex3": eff["day_med_ex3"],
        "gate_oos_med": gate_oos_med,
        "gate_oos_hit": gate_oos_hit,
        "gates_ok": bool(gate_oos_med and gate_oos_hit),
    }


def run_n_expansion_ablations(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
) -> list[dict[str, Any]]:
    """Controlled sample-expansion levers vs frozen ``leading_dip`` (same cleaning)."""
    rows: list[dict[str, Any]] = []

    def add(label: str, picks: pd.DataFrame) -> None:
        rows.append(_ablation_gate_snapshot(picks, oos_cut=oos_cut, label=label))

    # --- collections (gap/excess variants need re-scan) ---
    specs: list[tuple[str, dict[str, Any]]] = [
        ("frozen_gap080_ex4", {"gap3_max": -0.80, "excess_max": -4.0}),
        ("gap070_ex4", {"gap3_max": -0.70, "excess_max": -4.0}),
        ("gap065_ex5", {"gap3_max": -0.65, "excess_max": -5.0}),
        ("gap065_ex4", {"gap3_max": -0.65, "excess_max": -4.0}),
        ("gap080_ex5", {"gap3_max": -0.80, "excess_max": -5.0}),
    ]
    cache: dict[str, tuple[pd.DataFrame, list[str]]] = {}
    for key, kw in specs:
        cache[key] = collect_leading_dip_events(
            con, start=start, oos_cut=oos_cut, apply_structure=True, **kw
        )

    struct, trading = cache["frozen_gap080_ex4"]
    frozen = apply_frozen_policy(struct, trading, require_t2=True)
    add("A0_frozen_T2_slots_cap6", frozen)
    add("A1_drop_T2_slots_cap6", apply_frozen_policy(struct, trading, require_t2=False))
    add("A1b_structure_allday_no_slots", struct)
    add(
        "A2_T1_soft_penalty_1pp",
        apply_total_open_cap(
            apply_slot_policy_t1_soft(struct, t1_ex_penalty_pp=1.0),
            trading,
            max_open=TOTAL_OPEN_CAP,
        ),
    )
    add(
        "A2b_T1_soft_penalty_0.5pp",
        apply_total_open_cap(
            apply_slot_policy_t1_soft(struct, t1_ex_penalty_pp=0.5),
            trading,
            max_open=TOTAL_OPEN_CAP,
        ),
    )

    # W5 threshold / notUR alone — recollect with same gap/excess
    for w5, lab in ((105.0, "B1_W5lt105"), (110.0, "B2_W5lt110")):
        ev, _ = collect_leading_dip_events(
            con,
            start=start,
            oos_cut=oos_cut,
            apply_structure=True,
            w5_mv_max=w5,
        )
        add(lab + "_T2_slots", apply_slot_policy(ev, require_t2=True))

    ev_notur, _ = collect_leading_dip_events(
        con,
        start=start,
        oos_cut=oos_cut,
        apply_structure=True,
        require_not_ur=True,
        require_w5_weak=False,
    )
    add("B3_notUR_only_T2_slots", apply_slot_policy(ev_notur, require_t2=True))

    ev_w5only, _ = collect_leading_dip_events(
        con,
        start=start,
        oos_cut=oos_cut,
        apply_structure=True,
        require_not_ur=False,
        require_w5_weak=True,
    )
    add("B4_W5lt100_only_T2_slots", apply_slot_policy(ev_w5only, require_t2=True))

    for key, lab in (
        ("gap070_ex4", "C1_gap070_ex4"),
        ("gap065_ex5", "C2_gap065_ex5"),
        ("gap065_ex4", "C3_gap065_ex4"),
        ("gap080_ex5", "C4_gap080_ex5"),
    ):
        ev, _ = cache[key]
        add(lab + "_T2_slots", apply_slot_policy(ev, require_t2=True))
        add(lab + "_dropT2_slots", apply_slot_policy(ev, require_t2=False))

    # Longer history probe (5m universe sparse before 2025-01)
    ev_long, trading_long = collect_leading_dip_events(
        con,
        start="2024-07-01",
        oos_cut=oos_cut,
        apply_structure=True,
        lead_baseline_floor="2024-05-01",
        kbar_lookback_start="2024-06-01",
    )
    fr_long = apply_slot_policy(ev_long, require_t2=True)
    snap = _ablation_gate_snapshot(fr_long, oos_cut=oos_cut, label="D1_start_2024-07_T2_slots")
    snap["trading_days"] = len(trading_long)
    snap["structure_n"] = int(len(ev_long))
    # split pre-2025 vs post
    if len(fr_long):
        pre = fr_long[fr_long["date"] < "2025-01-01"]
        snap["n_pre_2025"] = int(len(pre))
        snap["n_from_2025"] = int(len(fr_long[fr_long["date"] >= "2025-01-01"]))
    rows.append(snap)

    # Slot policy reporting on frozen structure+T2
    t2 = struct[struct["T2"].astype(bool)].copy()
    for cap, lab in ((1, "E1_fixed_le1"), (2, "E2_fixed_le2"), (3, "E3_fixed_le3"), (4, "E4_fixed_le4")):
        pol = SlotPolicy(
            name=f"fixed_{cap}",
            default_cap=cap,
            expand_cap=cap,
            hard_cap=cap,
            require_busy_and_crash=False,
        )
        add(lab + "_T2", apply_slot_policy(t2, policy=pol, require_t2=False))
    add("E5_T2_take_all", t2)

    return rows


def format_ablation_table(rows: Sequence[dict[str, Any]]) -> str:
    lines = [
        f"{'lever':40s} {'n':>4} {'days':>4} {'hit':>5} {'med':>7} "
        f"{'OOS_n':>5} {'OOS_hit':>7} {'OOS_med':>8} gates"
    ]
    for r in rows:
        lines.append(
            f"{str(r['lever'])[:40]:40s} "
            f"{int(r.get('n') or 0):4d} "
            f"{int(r.get('days') or 0):4d} "
            f"{_pct(r.get('hit')):>5s} "
            f"{_pp(r.get('med')):>7s} "
            f"{int(r.get('oos_n') or 0):5d} "
            f"{_pct(r.get('oos_hit')):>7s} "
            f"{_pp(r.get('oos_med')):>8s} "
            f"{'PASS' if r.get('gates_ok') else 'fail'}"
        )
    return "\n".join(lines)


def format_report_text(report: ValidationReport) -> str:
    lines = [
        f"=== Leading Dip self-validate · spec={report.spec_id} · OOS cut {report.oos_cut} ===",
        (
            f"events: structure={report.n_events_structure}  "
            f"T2={report.n_t2}  frozen(quality)={report.n_frozen}  "
            f"cov({COVERAGE_SPEC_ID})={report.n_coverage}"
        ),
        "",
        "Summaries:",
    ]
    for k, s in report.summaries.items():
        lines.append(
            f"  {k:26s} n={s.get('n')} hit={_pct(s.get('hit'))} med={_pp(s.get('med'))} | "
            f"OOS n={s.get('oos_n')} hit={_pct(s.get('oos_hit'))} med={_pp(s.get('oos_med'))}"
        )
    lines.append("")
    lines.append("Gates:")
    for g in report.gates:
        mark = "PASS" if g.ok else "FAIL"
        lines.append(f"  [{mark}] {g.name}: {g.detail}")
    lines.append("")
    rho = report.taxonomy.get("rho_n_vs_rb_min")
    lines.append(
        f"Taxonomy: ρ(n,rB_min)={rho if rho==rho else 'nan'} "
        f"busy_labels={report.taxonomy.get('busy_day_labels')}"
    )
    lines.append(
        f"Concurrent: frozen={report.taxonomy.get('concurrent_frozen')} "
        f"uncapped_day_slots={report.taxonomy.get('concurrent_day_slots_uncapped')} "
        f"coverage={report.taxonomy.get('concurrent_coverage')} "
        f"take_all={report.taxonomy.get('concurrent_take_all')} "
        f"TOTAL_OPEN_CAP={report.taxonomy.get('total_open_cap')}"
    )
    md = report.taxonomy.get("mild_down_stratum") or {}
    if md:
        lines.append(
            f"mild_down({md.get('policy')}): "
            f"md={md.get('mild_down')} other={md.get('other')}"
        )
    if report.effective_sample:
        lines.append(f"Effective sample: {report.effective_sample}")
    lines.append("")
    lines.append(f"VERDICT: {'PASS' if report.passed else 'FAIL'}")
    return "\n".join(lines)


def _pct(x: Any) -> str:
    if x is None:
        return "n/a"
    return f"{100.0 * float(x):.0f}%"


def _pp(x: Any) -> str:
    if x is None:
        return "n/a"
    return f"{float(x):+.2f}"


# --- Research: S / WMA5(B) vs open depth --------------------------------------

BENCH_WMA5_DEPTH_SWEEP = (-1.0, -1.5, -2.0, -2.5, -3.0, -3.5, -4.0, -5.0)
# With excess≤−4 kept: depth thr near gap3 scale (−0.80) plus deeper
BENCH_WMA5_EXCESS_DEPTH_SWEEP = (-0.50, -0.80, -1.0, -1.5, -2.0, -2.5, -3.0)


def _event_key_set(df: pd.DataFrame) -> set[tuple[str, str]]:
    if df is None or len(df) == 0:
        return set()
    return set(zip(df["date"].astype(str), df["sid"].astype(str)))


def _subset_by_keys(df: pd.DataFrame, keys: set[tuple[str, str]]) -> pd.DataFrame:
    if df is None or len(df) == 0 or not keys:
        return df.iloc[0:0].copy() if df is not None else pd.DataFrame()
    mask = [((str(r.date), str(r.sid)) in keys) for r in df.itertuples(index=False)]
    return df.loc[mask].copy()


def _noise_partition(
    frozen_picks: pd.DataFrame,
    alt_picks: pd.DataFrame,
    *,
    oos_cut: str,
) -> dict[str, Any]:
    """Overlap / only-new / only-frozen on (date, sid) after policy picks."""
    fk = _event_key_set(frozen_picks)
    ak = _event_key_set(alt_picks)
    both = fk & ak
    only_new = ak - fk
    only_fr = fk - ak
    parts = {
        "both": _subset_by_keys(alt_picks if len(alt_picks) else frozen_picks, both),
        "only_new": _subset_by_keys(alt_picks, only_new),
        "only_frozen": _subset_by_keys(frozen_picks, only_fr),
    }
    # Prefer frozen rows for both when available (same sid-day, trigger minute may differ)
    if len(frozen_picks) and both:
        parts["both"] = _subset_by_keys(frozen_picks, both)

    out: dict[str, Any] = {
        "n_frozen": len(fk),
        "n_alt": len(ak),
        "n_both": len(both),
        "n_only_new": len(only_new),
        "n_only_frozen": len(only_fr),
        "jaccard": (len(both) / len(fk | ak)) if (fk or ak) else None,
    }
    for name, part in parts.items():
        sm = summarize_trades(part, oos_cut=oos_cut)
        out[name] = sm
        if len(part):
            mins = part["minute"].astype(str)
            out[f"{name}_pct_t1"] = float((mins <= "09:30").mean())
            out[f"{name}_median_ex0"] = float(part["ex0"].astype(float).median())
            out[f"{name}_median_gap0"] = (
                float(part["gap0"].astype(float).median()) if "gap0" in part.columns else None
            )
            out[f"{name}_median_d0"] = (
                float(part["d0"].astype(float).median()) if "d0" in part.columns else None
            )
            out[f"{name}_median_rB_min"] = float(part["rB_min"].astype(float).median())
            out[f"{name}_pct_crash_day"] = float(
                (part["rB_min"].astype(float) <= CRASH_RB_MIN).mean()
            )
        else:
            out[f"{name}_pct_t1"] = None
            out[f"{name}_median_ex0"] = None
            out[f"{name}_median_gap0"] = None
            out[f"{name}_median_d0"] = None
            out[f"{name}_median_rB_min"] = None
            out[f"{name}_pct_crash_day"] = None
    return out


def _summarize_policy_arm(
    events: pd.DataFrame,
    trading: Sequence[str],
    *,
    oos_cut: str,
    require_t2: bool,
    depth_key: str = "ex0",
) -> dict[str, Any]:
    slots = apply_slot_policy(events, require_t2=require_t2, depth_key=depth_key)
    capped = apply_total_open_cap(slots, trading, max_open=TOTAL_OPEN_CAP)
    s = summarize_trades(capped, oos_cut=oos_cut)
    s["structure_n"] = int(len(events))
    s["slots_n"] = int(len(slots))
    s["eff"] = effective_sample_stats(capped)
    return s


def run_bench_wma5_compare(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
    depth_sweep: Sequence[float] = BENCH_WMA5_DEPTH_SWEEP,
    bench_wma_len: int = 5,
) -> dict[str, Any]:
    """Compare frozen gap∩excess vs S/WMA_n(B) depth-vs-open (and raw RS control).

    Same structure filters · busy∧crash slots · total-open≤6 · hold T+3 excess.
    Ranking: frozen by ex0; depth arms by d0.
    """
    baseline, trading = collect_leading_dip_events(
        con, start=start, oos_cut=oos_cut, apply_structure=True, trigger="gap_excess"
    )
    wma_ev, _ = collect_leading_dip_events(
        con,
        start=start,
        oos_cut=oos_cut,
        apply_structure=True,
        trigger="bench_wma_open",
        depth_sweep=depth_sweep,
        bench_wma_len=bench_wma_len,
        depth_col="d_open",
    )
    rs_ev, _ = collect_leading_dip_events(
        con,
        start=start,
        oos_cut=oos_cut,
        apply_structure=True,
        trigger="bench_wma_open",
        depth_sweep=depth_sweep,
        bench_wma_len=bench_wma_len,
        depth_col="d_rs_open",
    )

    arms: list[dict[str, Any]] = []

    def _add(family: str, thr: float | None, ev: pd.DataFrame, *, depth_key: str) -> None:
        for track, t2 in (("quality", True), ("coverage", False)):
            sm = _summarize_policy_arm(
                ev, trading, oos_cut=oos_cut, require_t2=t2, depth_key=depth_key
            )
            arms.append(
                {
                    "family": family,
                    "track": track,
                    "depth_thr": thr,
                    "bench_wma_len": bench_wma_len if family == "wma5_b" else None,
                    **sm,
                }
            )

    _add("frozen_gap_excess", None, baseline, depth_key="ex0")

    for thr in depth_sweep:
        if len(wma_ev) and "depth_thr" in wma_ev.columns:
            sub = wma_ev[wma_ev["depth_thr"] == float(thr)].copy()
        else:
            sub = wma_ev.iloc[0:0].copy()
        _add("wma5_b", float(thr), sub, depth_key="d0")

        if len(rs_ev) and "depth_thr" in rs_ev.columns:
            sub_rs = rs_ev[rs_ev["depth_thr"] == float(thr)].copy()
        else:
            sub_rs = rs_ev.iloc[0:0].copy()
        _add("raw_rs", float(thr), sub_rs, depth_key="d0")

    # Pick wma5 quality arm closest in n to frozen quality (minimize |n−n0|).
    frozen_q = next(a for a in arms if a["family"] == "frozen_gap_excess" and a["track"] == "quality")
    wma_q = [a for a in arms if a["family"] == "wma5_b" and a["track"] == "quality"]
    matched = None
    if wma_q and frozen_q.get("n"):
        matched = min(wma_q, key=lambda a: abs(int(a["n"]) - int(frozen_q["n"])))

    verdict = "inconclusive"
    note = ""
    if matched is None or not frozen_q.get("n"):
        verdict = "no_data"
        note = "no frozen or wma5 events"
    else:
        fo = frozen_q.get("oos_med")
        mo = matched.get("oos_med")
        fh = frozen_q.get("oos_hit")
        mh = matched.get("oos_hit")
        if fo is None or mo is None or fh is None or mh is None:
            verdict = "thin_oos"
            note = "missing OOS stats"
        elif mh + 1e-9 < GATE_OOS_HIT_MIN or mo + 1e-9 < GATE_OOS_MED_MIN:
            verdict = "fail_gates"
            note = (
                f"matched thr={matched['depth_thr']} fails soft gates "
                f"(oos_hit={mh:.0%} oos_med={mo:+.2f})"
            )
        elif mo >= fo - 1.0 and mh >= fh - 0.05:
            verdict = "competitive"
            note = (
                f"wma5 thr={matched['depth_thr']} n≈frozen "
                f"(Δoos_med={mo - fo:+.2f}pp Δoos_hit={100 * (mh - fh):+.1f}pt)"
            )
        elif mo > fo + 0.5 and mh >= fh - 0.02:
            verdict = "improve"
            note = f"wma5 thr={matched['depth_thr']} beats frozen OOS med"
        else:
            verdict = "worse"
            note = (
                f"wma5 thr={matched['depth_thr']} trails frozen "
                f"(Δoos_med={mo - fo:+.2f}pp Δoos_hit={100 * (mh - fh):+.1f}pt)"
            )

    return {
        "spec": "leading_dip_bench_wma5_compare",
        "start": start,
        "oos_cut": oos_cut,
        "bench_wma_len": bench_wma_len,
        "depth_sweep": list(depth_sweep),
        "contract": {
            "structure": "Leading ∧ notUR ∧ W5_MV<100",
            "baseline_trigger": "gap3≤−0.80 ∩ excess≤−4",
            "wma5_trigger": f"d_open=(S/WMA{bench_wma_len}(B))/open−1 ≤ thr",
            "raw_rs_control": "d_rs_open=(S/B)/open−1 ≤ thr",
            "slots": "deepest ≤2 (busy∧crash→≤4) · total_open≤6 · hold T+3",
            "outcome": "interval excess vs same-source bench",
        },
        "arms": arms,
        "matched_wma5_quality": matched,
        "verdict": verdict,
        "note": note,
    }


def run_bench_wma5_excess_compare(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
    depth_sweep: Sequence[float] = BENCH_WMA5_EXCESS_DEPTH_SWEEP,
    bench_wma_len: int = 5,
    excess_max: float = EXCESS_MAX,
) -> dict[str, Any]:
    """Keep excess≤−4 · same structure/slots; swap gap3 for S/WMA_n(B) open-depth.

    Also runs raw S/B∩excess control and dissects only-new noise vs frozen picks.
    Ranking stays deepest **ex0** (same as frozen).
    """
    baseline, trading = collect_leading_dip_events(
        con,
        start=start,
        oos_cut=oos_cut,
        apply_structure=True,
        trigger="gap_excess",
        excess_max=excess_max,
    )
    wma_ev, _ = collect_leading_dip_events(
        con,
        start=start,
        oos_cut=oos_cut,
        apply_structure=True,
        trigger="depth_excess",
        depth_sweep=depth_sweep,
        bench_wma_len=bench_wma_len,
        depth_col="d_open",
        excess_max=excess_max,
    )
    rs_ev, _ = collect_leading_dip_events(
        con,
        start=start,
        oos_cut=oos_cut,
        apply_structure=True,
        trigger="depth_excess",
        depth_sweep=depth_sweep,
        bench_wma_len=bench_wma_len,
        depth_col="d_rs_open",
        excess_max=excess_max,
    )

    frozen_q = apply_total_open_cap(
        apply_slot_policy(baseline, require_t2=True, depth_key="ex0"),
        trading,
        max_open=TOTAL_OPEN_CAP,
    )

    arms: list[dict[str, Any]] = []
    noise_rows: list[dict[str, Any]] = []

    def _add(family: str, thr: float | None, ev: pd.DataFrame) -> pd.DataFrame:
        """Append quality+coverage summaries; return quality capped picks."""
        q_slots = apply_slot_policy(ev, require_t2=True, depth_key="ex0")
        q = apply_total_open_cap(q_slots, trading, max_open=TOTAL_OPEN_CAP)
        c_slots = apply_slot_policy(ev, require_t2=False, depth_key="ex0")
        c = apply_total_open_cap(c_slots, trading, max_open=TOTAL_OPEN_CAP)
        for track, picks, slots_n, struct_n in (
            ("quality", q, len(q_slots), len(ev)),
            ("coverage", c, len(c_slots), len(ev)),
        ):
            sm = summarize_trades(picks, oos_cut=oos_cut)
            arms.append(
                {
                    "family": family,
                    "track": track,
                    "depth_thr": thr,
                    "excess_max": excess_max,
                    "bench_wma_len": bench_wma_len if "wma5" in family else None,
                    "structure_n": int(struct_n),
                    "slots_n": int(slots_n),
                    "eff": effective_sample_stats(picks),
                    **sm,
                }
            )
        return q

    _add("frozen_gap_excess", None, baseline)

    for thr in depth_sweep:
        sub = (
            wma_ev[wma_ev["depth_thr"] == float(thr)].copy()
            if len(wma_ev) and "depth_thr" in wma_ev.columns
            else wma_ev.iloc[0:0].copy()
        )
        q_wma = _add("wma5_b_x_ex4", float(thr), sub)
        noise = _noise_partition(frozen_q, q_wma, oos_cut=oos_cut)
        noise["family"] = "wma5_b_x_ex4"
        noise["depth_thr"] = float(thr)
        noise_rows.append(noise)

        sub_rs = (
            rs_ev[rs_ev["depth_thr"] == float(thr)].copy()
            if len(rs_ev) and "depth_thr" in rs_ev.columns
            else rs_ev.iloc[0:0].copy()
        )
        _add("raw_rs_x_ex4", float(thr), sub_rs)

    frozen_arm = next(a for a in arms if a["family"] == "frozen_gap_excess" and a["track"] == "quality")
    wma_q = [a for a in arms if a["family"] == "wma5_b_x_ex4" and a["track"] == "quality"]
    # Prefer thr≈−0.80 (gap-scale); else closest n.
    matched = next((a for a in wma_q if a.get("depth_thr") == -0.80), None)
    if matched is None and wma_q and frozen_arm.get("n"):
        matched = min(wma_q, key=lambda a: abs(int(a["n"]) - int(frozen_arm["n"])))

    matched_noise = None
    if matched is not None:
        matched_noise = next(
            (n for n in noise_rows if n.get("depth_thr") == matched.get("depth_thr")),
            None,
        )

    verdict = "inconclusive"
    note = ""
    if matched is None or not frozen_arm.get("n"):
        verdict = "no_data"
        note = "no frozen or wma5∩ex4 events"
    else:
        fo, mo = frozen_arm.get("oos_med"), matched.get("oos_med")
        fh, mh = frozen_arm.get("oos_hit"), matched.get("oos_hit")
        only_new = (matched_noise or {}).get("only_new") or {}
        only_n = int((matched_noise or {}).get("n_only_new") or 0)
        if fo is None or mo is None or fh is None or mh is None:
            verdict = "thin_oos"
            note = "missing OOS stats"
        elif mh + 1e-9 < GATE_OOS_HIT_MIN or mo + 1e-9 < GATE_OOS_MED_MIN:
            verdict = "fail_gates"
            note = (
                f"wma5∩ex4 thr={matched['depth_thr']} fail soft gates "
                f"(oos_hit={mh:.0%} oos_med={mo:+.2f}); "
                f"only_new n={only_n} med={_pp(only_new.get('med'))}"
            )
        elif mo >= fo - 1.0 and mh >= fh - 0.05:
            verdict = "competitive"
            note = (
                f"wma5∩ex4 thr={matched['depth_thr']} ≈ frozen "
                f"(Δoos_med={mo - fo:+.2f}pp); only_new n={only_n}"
            )
        else:
            verdict = "worse"
            note = (
                f"wma5∩ex4 thr={matched['depth_thr']} trails frozen "
                f"(Δoos_med={mo - fo:+.2f}pp Δoos_hit={100 * (mh - fh):+.1f}pt); "
                f"only_new n={only_n} hit={_pct(only_new.get('hit'))} med={_pp(only_new.get('med'))}"
            )

    return {
        "spec": "leading_dip_bench_wma5_excess_compare",
        "start": start,
        "oos_cut": oos_cut,
        "bench_wma_len": bench_wma_len,
        "excess_max": excess_max,
        "depth_sweep": list(depth_sweep),
        "contract": {
            "structure": "Leading ∧ notUR ∧ W5_MV<100",
            "baseline_trigger": f"gap3≤−0.80 ∩ excess≤{excess_max}",
            "wma5_trigger": (
                f"d_open=(S/WMA{bench_wma_len}(B))/open−1 ≤ thr ∩ excess≤{excess_max}"
            ),
            "raw_rs_control": f"d_rs_open=(S/B)/open−1 ≤ thr ∩ excess≤{excess_max}",
            "slots": "deepest ex0 ≤2 (busy∧crash→≤4) · total_open≤6 · hold T+3",
            "outcome": "interval excess vs same-source bench",
        },
        "arms": arms,
        "noise": noise_rows,
        "matched_wma5_quality": matched,
        "matched_noise": matched_noise,
        "verdict": verdict,
        "note": note,
    }


def format_bench_wma5_excess_table(payload: dict[str, Any]) -> str:
    lines = [
        "=== Leading Dip · WMA5(B) depth ∩ excess≤−4 · noise study ===",
        f"verdict: {payload.get('verdict')} · {payload.get('note')}",
        "",
        "| family | thr | track | n | hit | med | OOS n/hit/med | struct_n |",
        "|--|--:|--|--:|--:|--:|--|--:|",
    ]
    for a in payload.get("arms") or []:
        thr = a.get("depth_thr")
        thr_s = "—" if thr is None else f"{float(thr):.2f}".rstrip("0").rstrip(".")
        oos = (
            f"{a.get('oos_n')}/{_pct(a.get('oos_hit'))}/{_pp(a.get('oos_med'))}"
            if a.get("oos_n")
            else "—"
        )
        lines.append(
            f"| {a.get('family')} | {thr_s} | {a.get('track')} | {a.get('n')} | "
            f"{_pct(a.get('hit'))} | {_pp(a.get('med'))} | {oos} | {a.get('structure_n')} |"
        )
    lines.extend(["", "--- noise partitions (quality picks vs frozen) ---", ""])
    lines.append(
        "| thr | both | only_new n/hit/med | only_fr n/hit/med | "
        "new T1% | new crash% | jaccard |"
    )
    lines.append("|--:|--:|--|--|--:|--:|--:|")
    for n in payload.get("noise") or []:
        on = n.get("only_new") or {}
        of = n.get("only_frozen") or {}
        lines.append(
            f"| {float(n['depth_thr']):.2f} | {n.get('n_both')} | "
            f"{n.get('n_only_new')}/{_pct(on.get('hit'))}/{_pp(on.get('med'))} | "
            f"{n.get('n_only_frozen')}/{_pct(of.get('hit'))}/{_pp(of.get('med'))} | "
            f"{_pct(n.get('only_new_pct_t1'))} | {_pct(n.get('only_new_pct_crash_day'))} | "
            f"{(n.get('jaccard') or 0):.2f} |"
        )
    return "\n".join(lines)


def render_bench_wma5_excess_md(payload: dict[str, Any]) -> str:
    matched = payload.get("matched_wma5_quality") or {}
    noise = payload.get("matched_noise") or {}
    on = noise.get("only_new") or {}
    of = noise.get("only_frozen") or {}
    lines = [
        "# Leading Dip · WMA5(大盤)深度 ∩ excess≤−4 · 雜訊研究",
        "",
        "日期：2026-07-15 · Research only · **未採納**",
        "",
        "## Verdict",
        "",
        f"**{payload.get('verdict')}** — {payload.get('note')}",
        "",
        "- 契約與凍結相同：Leading 結構核 · deepest **ex0** 日槽 · 總在外≤6 · hold T+3",
        f"- 凍結：`gap3≤−0.80 ∩ excess≤{payload.get('excess_max')}`",
        f"- 實驗：把 gap3 換成 `S/WMA{payload.get('bench_wma_len')}(B)` 相對今開深度；**仍保留 excess≤−4**",
        "- 控制：裸 `S/B` 相對今開 ∩ excess≤−4",
        "",
        "## Matched (quality · thr≈−0.80 gap-scale)",
        "",
    ]
    if matched:
        lines.append(
            f"- thr={matched.get('depth_thr')} · n={matched.get('n')} · "
            f"OOS {_pct(matched.get('oos_hit'))} / {_pp(matched.get('oos_med'))}"
        )
        lines.append(
            f"- overlap both={noise.get('n_both')} · only_new={noise.get('n_only_new')} "
            f"(hit={_pct(on.get('hit'))} med={_pp(on.get('med'))}) · "
            f"only_frozen={noise.get('n_only_frozen')} "
            f"(hit={_pct(of.get('hit'))} med={_pp(of.get('med'))})"
        )
        lines.append(
            f"- only_new 特徵：T1%={_pct(noise.get('only_new_pct_t1'))} · "
            f"crash日%={_pct(noise.get('only_new_pct_crash_day'))} · "
            f"med_gap0={_pp(noise.get('only_new_median_gap0'))} · "
            f"med_d0={_pp(noise.get('only_new_median_d0'))}"
        )
        lines.append("")
    lines.extend(
        [
            "## Full table",
            "",
            "| family | thr | track | n | hit | med | OOS n/hit/med | struct_n |",
            "|--|--:|--|--:|--:|--:|--|--:|",
        ]
    )
    for a in payload.get("arms") or []:
        thr = a.get("depth_thr")
        thr_s = "—" if thr is None else f"{float(thr):.2f}".rstrip("0").rstrip(".")
        oos = (
            f"{a.get('oos_n')}/{_pct(a.get('oos_hit'))}/{_pp(a.get('oos_med'))}"
            if a.get("oos_n")
            else "—"
        )
        lines.append(
            f"| {a.get('family')} | {thr_s} | {a.get('track')} | {a.get('n')} | "
            f"{_pct(a.get('hit'))} | {_pp(a.get('med'))} | {oos} | {a.get('structure_n')} |"
        )
    lines.extend(
        [
            "",
            "## Noise partitions",
            "",
            "| thr | both | only_new | only_frozen | new T1% | new crash% | jaccard |",
            "|--:|--:|--|--|--:|--:|--:|",
        ]
    )
    for n in payload.get("noise") or []:
        on_ = n.get("only_new") or {}
        of_ = n.get("only_frozen") or {}
        lines.append(
            f"| {float(n['depth_thr']):.2f} | {n.get('n_both')} | "
            f"{n.get('n_only_new')}/{_pct(on_.get('hit'))}/{_pp(on_.get('med'))} | "
            f"{n.get('n_only_frozen')}/{_pct(of_.get('hit'))}/{_pp(of_.get('med'))} | "
            f"{_pct(n.get('only_new_pct_t1'))} | {_pct(n.get('only_new_pct_crash_day'))} | "
            f"{(n.get('jaccard') or 0):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Re-run",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_bench_wma5_study.py --with-excess",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def format_bench_wma5_table(payload: dict[str, Any]) -> str:
    lines = [
        "=== Leading Dip · S/WMA5(B) vs open · compare ===",
        f"verdict: {payload.get('verdict')} · {payload.get('note')}",
        "",
        "| family | thr | track | n | hit | med | OOS n/hit/med | struct_n |",
        "|--|--:|--|--:|--:|--:|--|--:|",
    ]
    for a in payload.get("arms") or []:
        thr = a.get("depth_thr")
        thr_s = "—" if thr is None else f"{float(thr):.1f}"
        oos = (
            f"{a.get('oos_n')}/{_pct(a.get('oos_hit'))}/{_pp(a.get('oos_med'))}"
            if a.get("oos_n")
            else "—"
        )
        lines.append(
            f"| {a.get('family')} | {thr_s} | {a.get('track')} | {a.get('n')} | "
            f"{_pct(a.get('hit'))} | {_pp(a.get('med'))} | {oos} | {a.get('structure_n')} |"
        )
    return "\n".join(lines)


def render_bench_wma5_md(payload: dict[str, Any]) -> str:
    matched = payload.get("matched_wma5_quality") or {}
    lines = [
        "# Leading Dip · S/WMA5(大盤) vs 開盤深度 · 成效對照",
        "",
        f"日期：2026-07-15 · Research only · **未採納**",
        "",
        "## Verdict",
        "",
        f"**{payload.get('verdict')}** — {payload.get('note')}",
        "",
        "- 契約：同 Leading 結構核 · 日槽 deepest · 總在外≤6 · hold T+3 excess",
        "- 凍結對照：`gap3≤−0.80 ∩ excess≤−4`（rank by ex0）",
        f"- 實驗：`d_open = S/WMA{payload.get('bench_wma_len')}(B)` 相對今開；rank by d0",
        "- 控制：裸 `S/B` 相對今開（同門檻掃）",
        "- WMA(B) 用昨同來源末 (n−1) 棒 warmstart，開盤即可算",
        "",
        "## Matched density (quality · n≈frozen)",
        "",
    ]
    if matched:
        lines.extend(
            [
                f"- thr = **{matched.get('depth_thr')}** · n={matched.get('n')} · "
                f"OOS hit={_pct(matched.get('oos_hit'))} · med={_pp(matched.get('oos_med'))}",
                "",
            ]
        )
    else:
        lines.append("- （無匹配）\n")
    lines.extend(
        [
            "## Full table",
            "",
            "| family | thr | track | n | hit | med | OOS n/hit/med | struct_n |",
            "|--|--:|--|--:|--:|--:|--|--:|",
        ]
    )
    for a in payload.get("arms") or []:
        thr = a.get("depth_thr")
        thr_s = "—" if thr is None else f"{float(thr):.1f}"
        oos = (
            f"{a.get('oos_n')}/{_pct(a.get('oos_hit'))}/{_pp(a.get('oos_med'))}"
            if a.get("oos_n")
            else "—"
        )
        lines.append(
            f"| {a.get('family')} | {thr_s} | {a.get('track')} | {a.get('n')} | "
            f"{_pct(a.get('hit'))} | {_pp(a.get('med'))} | {oos} | {a.get('structure_n')} |"
        )
    lines.extend(
        [
            "",
            "## Re-run",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_bench_wma5_study.py",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def run_gap_excess_open_compare(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
    gap3_max: float = GAP3_MAX,
    excess_max: float = EXCESS_MAX,
) -> dict[str, Any]:
    """Frozen gap∩excess(prev_close) vs gap∩excess(session open); same structure/slots."""
    base, trading = collect_leading_dip_events(
        con,
        start=start,
        oos_cut=oos_cut,
        apply_structure=True,
        trigger="gap_excess",
        excess_anchor="prev_close",
        gap3_max=gap3_max,
        excess_max=excess_max,
    )
    open_ev, _ = collect_leading_dip_events(
        con,
        start=start,
        oos_cut=oos_cut,
        apply_structure=True,
        trigger="gap_excess",
        excess_anchor="open",
        gap3_max=gap3_max,
        excess_max=excess_max,
    )

    arms: list[dict[str, Any]] = []

    def _add(family: str, ev: pd.DataFrame) -> pd.DataFrame:
        q = apply_total_open_cap(
            apply_slot_policy(ev, require_t2=True, depth_key="ex0"),
            trading,
            max_open=TOTAL_OPEN_CAP,
        )
        c = apply_total_open_cap(
            apply_slot_policy(ev, require_t2=False, depth_key="ex0"),
            trading,
            max_open=TOTAL_OPEN_CAP,
        )
        for track, picks in (("quality", q), ("coverage", c)):
            sm = summarize_trades(picks, oos_cut=oos_cut)
            arms.append(
                {
                    "family": family,
                    "track": track,
                    "structure_n": int(len(ev)),
                    **sm,
                    "eff": effective_sample_stats(picks),
                }
            )
        return q

    frozen_q = _add("gap_ex_yday", base)
    open_q = _add("gap_ex_open", open_ev)
    noise = _noise_partition(frozen_q, open_q, oos_cut=oos_cut)

    fq = next(a for a in arms if a["family"] == "gap_ex_yday" and a["track"] == "quality")
    oq = next(a for a in arms if a["family"] == "gap_ex_open" and a["track"] == "quality")
    on = noise.get("only_new") or {}
    of = noise.get("only_frozen") or {}

    fo, mo = fq.get("oos_med"), oq.get("oos_med")
    fh, mh = fq.get("oos_hit"), oq.get("oos_hit")
    if fo is None or mo is None or fh is None or mh is None:
        verdict, note = "thin_oos", "missing OOS"
    elif mh + 1e-9 < GATE_OOS_HIT_MIN or mo + 1e-9 < GATE_OOS_MED_MIN:
        verdict = "fail_gates"
        note = (
            f"open-anchor OOS hit={mh:.0%} med={mo:+.2f}; "
            f"only_new n={noise.get('n_only_new')} med={_pp(on.get('med'))}"
        )
    elif mo >= fo - 1.0 and mh >= fh - 0.05:
        verdict = "competitive"
        note = (
            f"open≈yday (Δoos_med={mo - fo:+.2f}pp); "
            f"only_new={noise.get('n_only_new')} only_fr={noise.get('n_only_frozen')}"
        )
    else:
        verdict = "worse"
        note = (
            f"open trails yday (Δoos_med={mo - fo:+.2f}pp Δhit={100 * (mh - fh):+.1f}pt); "
            f"only_new n={noise.get('n_only_new')} hit={_pct(on.get('hit'))} med={_pp(on.get('med'))}; "
            f"only_fr n={noise.get('n_only_frozen')} hit={_pct(of.get('hit'))} med={_pp(of.get('med'))}"
        )

    return {
        "spec": "leading_dip_gap_excess_open_anchor",
        "start": start,
        "oos_cut": oos_cut,
        "contract": {
            "structure": "Leading ∧ notUR ∧ W5_MV<100",
            "yday": f"gap3≤{gap3_max} ∩ excess_yday≤{excess_max}",
            "open": f"gap3≤{gap3_max} ∩ excess_open≤{excess_max}",
            "excess_open": "(S/S_open−1)−(B/B_open−1) · first aligned 5m bar",
            "slots": "deepest ex0 · ≤2 (busy∧crash→≤4) · total_open≤6 · hold T+3",
        },
        "arms": arms,
        "noise": noise,
        "verdict": verdict,
        "note": note,
    }


def format_gap_excess_open_table(payload: dict[str, Any]) -> str:
    lines = [
        "=== Leading Dip · gap∩excess · yday vs open anchor ===",
        f"verdict: {payload.get('verdict')} · {payload.get('note')}",
        "",
        "| family | track | n | hit | med | OOS n/hit/med | struct_n |",
        "|--|--|--:|--:|--:|--|--:|",
    ]
    for a in payload.get("arms") or []:
        oos = (
            f"{a.get('oos_n')}/{_pct(a.get('oos_hit'))}/{_pp(a.get('oos_med'))}"
            if a.get("oos_n")
            else "—"
        )
        lines.append(
            f"| {a.get('family')} | {a.get('track')} | {a.get('n')} | "
            f"{_pct(a.get('hit'))} | {_pp(a.get('med'))} | {oos} | {a.get('structure_n')} |"
        )
    n = payload.get("noise") or {}
    on = n.get("only_new") or {}
    of = n.get("only_frozen") or {}
    lines.extend(
        [
            "",
            "--- noise (quality picks) ---",
            (
                f"both={n.get('n_both')} · only_new={n.get('n_only_new')}/"
                f"{_pct(on.get('hit'))}/{_pp(on.get('med'))} · only_frozen={n.get('n_only_frozen')}/"
                f"{_pct(of.get('hit'))}/{_pp(of.get('med'))} · jaccard={(n.get('jaccard') or 0):.2f}"
            ),
            (
                f"only_new T1%={_pct(n.get('only_new_pct_t1'))} "
                f"crash%={_pct(n.get('only_new_pct_crash_day'))} "
                f"med_gap={_pp(n.get('only_new_median_gap0'))}"
            ),
            (
                f"only_fr T1%={_pct(n.get('only_frozen_pct_t1'))} "
                f"crash%={_pct(n.get('only_frozen_pct_crash_day'))} "
                f"med_gap={_pp(n.get('only_frozen_median_gap0'))}"
            ),
        ]
    )
    return "\n".join(lines)


def render_gap_excess_open_md(payload: dict[str, Any]) -> str:
    n = payload.get("noise") or {}
    on = n.get("only_new") or {}
    of = n.get("only_frozen") or {}
    arm_lines = []
    for a in payload.get("arms") or []:
        if a.get("oos_n"):
            oos = f"{a.get('oos_n')}/{_pct(a.get('oos_hit'))}/{_pp(a.get('oos_med'))}"
        else:
            oos = "—"
        arm_lines.append(
            f"| {a.get('family')} | {a.get('track')} | {a.get('n')} | "
            f"{_pct(a.get('hit'))} | {_pp(a.get('med'))} | {oos} | {a.get('structure_n')} |"
        )
    return "\n".join(
        [
            "# Leading Dip · gap∩excess · 昨收錨 vs 今開錨",
            "",
            "日期：2026-07-15 · Research only · **未採納**",
            "",
            "## Verdict",
            "",
            f"**{payload.get('verdict')}** — {payload.get('note')}",
            "",
            "- gap3 不變（RS / WMA3(RS) − 1）",
            "- 只改 excess 錨：昨收 → 當日首根對齊 5m 棒",
            "- 其餘結構／日槽／總在外／hold T+3 相同",
            "",
            "## Arms",
            "",
            "| family | track | n | hit | med | OOS n/hit/med | struct_n |",
            "|--|--|--:|--:|--:|--|--:|",
            *arm_lines,
            "",
            "## Noise",
            "",
            (
                f"- both={n.get('n_both')} · only_new={n.get('n_only_new')} "
                f"(hit={_pct(on.get('hit'))} med={_pp(on.get('med'))}) · "
                f"only_frozen={n.get('n_only_frozen')} "
                f"(hit={_pct(of.get('hit'))} med={_pp(of.get('med'))}) · "
                f"jaccard={(n.get('jaccard') or 0):.2f}"
            ),
            "",
            "## Re-run",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_bench_wma5_study.py --excess-open",
            "```",
            "",
        ]
    )


def _minute_bucket(m: str) -> str:
    m = str(m)
    if m <= "09:30":
        return "T1_≤09:30"
    if m <= "10:00":
        return "09:35–10:00"
    if m <= "11:00":
        return "10:05–11:00"
    if m <= "12:00":
        return "11:05–12:00"
    return "12:05–13:30"


def _tod_shares(picks: pd.DataFrame) -> dict[str, Any]:
    order = ["T1_≤09:30", "09:35–10:00", "10:05–11:00", "11:05–12:00", "12:05–13:30"]
    if picks is None or len(picks) == 0:
        return {b: {"n": 0, "share": 0.0} for b in order}
    b = picks["minute"].map(_minute_bucket)
    out: dict[str, Any] = {}
    n = len(picks)
    for name in order:
        k = int((b == name).sum())
        out[name] = {"n": k, "share": k / n if n else 0.0}
    return out


def run_time_sleeve_expand(
    con: sqlite3.Connection,
    *,
    start: str = START_DEFAULT,
    oos_cut: str = OOS_CUT_DEFAULT,
) -> dict[str, Any]:
    """Map TOD density of frozen Dip, then probe time-window variants for more n.

    Variants keep Leading structure; change window / excess anchor / soft thresholds.
    Complement = only_new vs frozen **coverage** picks (superset of quality+T1).
    """
    # --- baselines (one collect reused) ---
    base, trading = collect_leading_dip_events(
        con, start=start, oos_cut=oos_cut, apply_structure=True, trigger="gap_excess"
    )
    frozen_q = apply_total_open_cap(
        apply_slot_policy(base, require_t2=True, depth_key="ex0"),
        trading,
        max_open=TOTAL_OPEN_CAP,
    )
    frozen_c = apply_total_open_cap(
        apply_slot_policy(base, require_t2=False, depth_key="ex0"),
        trading,
        max_open=TOTAL_OPEN_CAP,
    )

    tod = {
        "structure": _tod_shares(base),
        "quality": _tod_shares(frozen_q),
        "coverage": _tod_shares(frozen_c),
        "n_structure": int(len(base)),
        "n_quality": int(len(frozen_q)),
        "n_coverage": int(len(frozen_c)),
    }

    # Variant specs: complementary time sleeves
    specs: list[dict[str, Any]] = [
        {
            "id": "mid_am_same",
            "label": "10:00–11:30 · frozen gap∩ex(yday)",
            "kwargs": {
                "trigger": "gap_excess",
                "excess_anchor": "prev_close",
                "minute_lo": "10:00",
                "minute_hi": "11:30",
            },
        },
        {
            "id": "pm_same",
            "label": "11:30–13:30 · frozen gap∩ex(yday)",
            "kwargs": {
                "trigger": "gap_excess",
                "excess_anchor": "prev_close",
                "minute_lo": "11:30",
                "minute_hi": "13:30",
            },
        },
        {
            "id": "mid_am_open_anchor",
            "label": "10:00–11:30 · gap∩ex(open)",
            "kwargs": {
                "trigger": "gap_excess",
                "excess_anchor": "open",
                "minute_lo": "10:00",
                "minute_hi": "11:30",
            },
        },
        {
            "id": "pm_open_anchor",
            "label": "11:30–13:30 · gap∩ex(open)",
            "kwargs": {
                "trigger": "gap_excess",
                "excess_anchor": "open",
                "minute_lo": "11:30",
                "minute_hi": "13:30",
            },
        },
        {
            "id": "pm_soft065_ex35",
            "label": "11:00–13:30 · gap≤−0.65 ∩ ex≤−3.5 (yday)",
            "kwargs": {
                "trigger": "gap_excess",
                "excess_anchor": "prev_close",
                "gap3_max": -0.65,
                "excess_max": -3.5,
                "minute_lo": "11:00",
                "minute_hi": "13:30",
            },
        },
        {
            "id": "late_morn_soft",
            "label": "10:00–12:00 · gap≤−0.70 ∩ ex≤−3.5 (yday)",
            "kwargs": {
                "trigger": "gap_excess",
                "excess_anchor": "prev_close",
                "gap3_max": -0.70,
                "excess_max": -3.5,
                "minute_lo": "10:00",
                "minute_hi": "12:00",
            },
        },
        {
            "id": "post_1000_open",
            "label": "≥10:00 · gap∩ex(open) full afternoon from 10",
            "kwargs": {
                "trigger": "gap_excess",
                "excess_anchor": "open",
                "minute_lo": "10:00",
                "minute_hi": "13:30",
            },
        },
    ]

    arms: list[dict[str, Any]] = []
    # baseline rows
    for fam, picks in (("frozen_quality", frozen_q), ("frozen_coverage", frozen_c)):
        sm = summarize_trades(picks, oos_cut=oos_cut)
        arms.append(
            {
                "id": fam,
                "label": fam,
                "n": sm["n"],
                "hit": sm["hit"],
                "med": sm["med"],
                "oos_n": sm["oos_n"],
                "oos_hit": sm["oos_hit"],
                "oos_med": sm["oos_med"],
                "structure_n": int(len(base)) if fam.startswith("frozen") else None,
                "n_only_vs_cov": 0 if fam == "frozen_coverage" else None,
                "only_vs_cov": None,
                "tod": _tod_shares(picks),
            }
        )

    for sp in specs:
        ev, _ = collect_leading_dip_events(
            con,
            start=start,
            oos_cut=oos_cut,
            apply_structure=True,
            **sp["kwargs"],
        )
        # No T2 filter for time sleeves (window already defines TOD); still slot+cap
        slots = apply_slot_policy(ev, require_t2=False, depth_key="ex0")
        picks = apply_total_open_cap(slots, trading, max_open=TOTAL_OPEN_CAP)
        sm = summarize_trades(picks, oos_cut=oos_cut)
        noise = _noise_partition(frozen_c, picks, oos_cut=oos_cut)
        on = noise.get("only_new") or {}
        arms.append(
            {
                "id": sp["id"],
                "label": sp["label"],
                "kwargs": sp["kwargs"],
                "structure_n": int(len(ev)),
                "n": sm["n"],
                "hit": sm["hit"],
                "med": sm["med"],
                "oos_n": sm["oos_n"],
                "oos_hit": sm["oos_hit"],
                "oos_med": sm["oos_med"],
                "n_only_vs_cov": int(noise.get("n_only_new") or 0),
                "only_vs_cov_hit": on.get("hit"),
                "only_vs_cov_med": on.get("med"),
                "n_both_vs_cov": int(noise.get("n_both") or 0),
                "jaccard_vs_cov": noise.get("jaccard"),
                "tod": _tod_shares(picks),
                "noise": noise,
            }
        )

    # Rank satellites by complementary OOS med among only_new with n_only≥8
    sats = [a for a in arms if a["id"] not in ("frozen_quality", "frozen_coverage")]
    viable = [
        a
        for a in sats
        if int(a.get("n_only_vs_cov") or 0) >= 8
        and a.get("only_vs_cov_med") is not None
        and float(a["only_vs_cov_med"]) > 0
        and a.get("only_vs_cov_hit") is not None
        and float(a["only_vs_cov_hit"]) >= 0.55
    ]
    viable_sorted = sorted(
        viable,
        key=lambda a: (
            float(a.get("only_vs_cov_med") or -999),
            int(a.get("n_only_vs_cov") or 0),
        ),
        reverse=True,
    )

    if not viable_sorted:
        verdict = "no_viable_complement"
        note = "no time-sleeve with only_new≥8 · med>0 · hit≥55% vs coverage"
    else:
        best = viable_sorted[0]
        verdict = "satellite_candidates"
        note = (
            f"best complement={best['id']} only_new={best['n_only_vs_cov']} "
            f"hit={_pct(best.get('only_vs_cov_hit'))} med={_pp(best.get('only_vs_cov_med'))}"
        )

    return {
        "spec": "leading_dip_time_sleeve_expand",
        "start": start,
        "oos_cut": oos_cut,
        "tod_baseline": tod,
        "arms": arms,
        "viable_complements": viable_sorted,
        "verdict": verdict,
        "note": note,
    }


def format_time_sleeve_table(payload: dict[str, Any]) -> str:
    tod = payload.get("tod_baseline") or {}
    lines = [
        "=== Leading Dip · time-sleeve expand (complementary n) ===",
        f"verdict: {payload.get('verdict')} · {payload.get('note')}",
        "",
        "--- TOD share (frozen) ---",
        f"quality n={tod.get('n_quality')} · coverage n={tod.get('n_coverage')} · structure n={tod.get('n_structure')}",
    ]
    for track in ("quality", "coverage", "structure"):
        shares = tod.get(track) or {}
        bits = [f"{b}:{shares[b]['n']}({100*shares[b]['share']:.0f}%)" for b in shares]
        lines.append(f"  {track}: " + " · ".join(bits))
    lines.extend(
        [
            "",
            "| id | n | hit | med | OOS | only_vs_cov n/hit/med | jaccard |",
            "|--|--:|--:|--:|--|--|--:|",
        ]
    )
    for a in payload.get("arms") or []:
        oos = (
            f"{a.get('oos_n')}/{_pct(a.get('oos_hit'))}/{_pp(a.get('oos_med'))}"
            if a.get("oos_n")
            else "—"
        )
        if a["id"] in ("frozen_quality", "frozen_coverage"):
            only = "—"
            jac = "—"
        else:
            only = (
                f"{a.get('n_only_vs_cov')}/{_pct(a.get('only_vs_cov_hit'))}/"
                f"{_pp(a.get('only_vs_cov_med'))}"
            )
            jac = f"{(a.get('jaccard_vs_cov') or 0):.2f}"
        lines.append(
            f"| {a.get('id')} | {a.get('n')} | {_pct(a.get('hit'))} | {_pp(a.get('med'))} | "
            f"{oos} | {only} | {jac} |"
        )
    return "\n".join(lines)


def render_time_sleeve_md(payload: dict[str, Any]) -> str:
    tod = payload.get("tod_baseline") or {}
    lines = [
        "# Leading Dip · 時段袖 · 互補擴樣",
        "",
        "日期：2026-07-15 · Research only · **未採納**",
        "",
        "## Verdict",
        "",
        f"**{payload.get('verdict')}** — {payload.get('note')}",
        "",
        "## 凍結樣本時段分布",
        "",
        f"- quality n={tod.get('n_quality')}（**無 T1**，由規規格強制）",
        f"- coverage n={tod.get('n_coverage')}",
        f"- structure n={tod.get('n_structure')}",
        "",
        "| track | T1≤09:30 | 09:35–10:00 | 10:05–11:00 | 11:05–12:00 | 12:05–13:30 |",
        "|--|--:|--:|--:|--:|--:|",
    ]
    for track in ("quality", "coverage", "structure"):
        s = tod.get(track) or {}
        cells = []
        for b in ["T1_≤09:30", "09:35–10:00", "10:05–11:00", "11:05–12:00", "12:05–13:30"]:
            cell = s.get(b) or {"n": 0, "share": 0}
            cells.append(f"{cell['n']}({100*cell['share']:.0f}%)")
        lines.append(f"| {track} | " + " | ".join(cells) + " |")
    lines.extend(
        [
            "",
            "## 變體（互補相對 coverage）",
            "",
            "| id | label | n | OOS | only_vs_cov | jaccard |",
            "|--|--|--:|--|--|--:|",
        ]
    )
    for a in payload.get("arms") or []:
        if a["id"] in ("frozen_quality", "frozen_coverage"):
            continue
        oos = f"{_pct(a.get('oos_hit'))}/{_pp(a.get('oos_med'))}"
        only = (
            f"{a.get('n_only_vs_cov')}/{_pct(a.get('only_vs_cov_hit'))}/"
            f"{_pp(a.get('only_vs_cov_med'))}"
        )
        lines.append(
            f"| {a.get('id')} | {a.get('label')} | {a.get('n')} | {oos} | {only} | "
            f"{(a.get('jaccard_vs_cov') or 0):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Re-run",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_bench_wma5_study.py --time-sleeves",
            "```",
            "",
        ]
    )
    return "\n".join(lines)
