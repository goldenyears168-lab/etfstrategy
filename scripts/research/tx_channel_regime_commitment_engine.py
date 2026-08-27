#!/usr/bin/env python3
"""RESEARCH FORK of ``src/tmf_channel/causal_engine.py`` — regime-commitment
hysteresis experiment (2026-08-08, TestFix phase).

NOT the live engine. Frozen live SSOT is ``src/tmf_channel/causal_engine.py``
(never edited by this fork). This copy adds ONE new mechanism on top of an
otherwise byte-identical ``simulate()``:

  ``p["regime_commit_n"]`` (default 1 = byte-identical to upstream): a new
  PV8 classification from ``classify_pv()`` only becomes the "committed"
  regime used for every downstream gating decision (session_pv_book cell
  lookup → block list / hang_lo·hi / skip_quiet_mode / early_fill_gamma /
  max_hold_bars, trend_hang_dampen, contract-amend cadence, and the
  regime tag recorded on newly opened lots) once it has appeared for N
  *consecutive* raw bars; until then, the PREVIOUSLY committed
  classification keeps driving those decisions. ``regime[t]`` in the
  return tuple is UNCHANGED (still the raw per-bar classification, exactly
  as upstream) — a new 8th return value ``committed_regime`` carries the
  smoothed series so raw-vs-committed can be diffed bar-for-bar for the
  safety-cost accounting this experiment needs. See
  ``scripts/research/tx_channel_regime_commitment_walkforward.py`` for the
  driver that re-simulates all 4 sanctioned windows with N=1 (baseline),
  N=2, N=3.

Original upstream docstring follows unmodified for context/provenance.

---

Promoted from ``reports/research/channel_lab/hang_anchor_causal_lab.py``
(2026-08-06 architecture cut). Live Order imports ``tmf_channel.engine``.
Lab path keeps a thin shim for historical research scripts.

H-CAUSAL-ANCHOR (2026-08-05): hang-placement anchor is configurable via
``p["hang_anchor"]`` + ``build_anchor_series()``:

  - "O"   — bar open (baseline; re-run here for apples-to-apples, already
            validated negative by the other thread as round 4b / R15).
  - "C"   — bar close (the ORIGINAL BUG; kept only as a sanity-check
            tripwire — if a candidate's P&L converges toward the C[t]
            number, that's a red flag the candidate leaked look-ahead too).
  - "ema" with hang_anchor_ema_n=N — causal EMA of closes strictly through
            bar t-1 (bar t's own O/H/L/C never enter the anchor), N in bars.
            N=1 degenerates to exactly C[t-1] (the "strictest fix" from
            H-RECOVERY's own 建議下一刀). N=10/15/20 = short causal
            trend/channel line.

Nothing else changes: fill-determination logic (_tick_walk_bar's real-tick
walk, same-bar-only-against-PREVIOUS-bar's-rail convention, exit checks)
is byte-identical to the upstream file. Only the anchor feeding
birth_wants()/structure_wants()/far_cover/sticky-cancel changes.

Original file's docstring follows for context:

---
[historical — no longer applies; this file IS the live SSOT now]
RESEARCH-ONLY FORK of jack_channel_v6_pv.py.

One-off variant for H-LOT-FIXED2: instead of staged scale-in (L1 enters 1
lot; L2 only adds if price re-touches the rail at/beyond the L1 entry price),
this fork opens BOTH lots at once on the very first touch when
params["fixed_double_entry"] is truthy. Everything else (exits, regime,
VIX bias) is byte-identical to the shared engine. See lot_sizing_compare.py.

jack-spec v6 — sticky hangs + trend-ride exits + VIX lag1 + morning gap.

Defaults (day/night same):
  - Sticky absolute pivot rails; when *flat* hang S+L; when *in position*
    only same-side hang (no near opp_cover chop).
  - Protective stop_pts=100; touch fill honors limit (no thermo veto).
  - Reverse = flatten (stop/EOD/session) then the other side can fill — not
    passive opp_cover from a near opposite rail.
  - VIXTWN lag1 session bias; morning gap_bias 08:45–09:15 only.
  - Optional day_dir_filter (prior/overnight) available but OFF by default
    (did not improve IS/tail bake-off 2026-08).
  - OOS night cache still truncated 23:59 — blended OOS cautious.

Observe-only · no live submit.
"""
from __future__ import annotations

import csv
import json
import math
import sqlite3
import statistics as st
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

# Fork lives under scripts/research/, not src/tmf_channel/ — fix path
# resolution accordingly (upstream assumes __file__ is two dirs under repo
# root; here it's also two dirs under repo root — scripts/research/ — but
# the sibling "src" is a cousin, not the parent, so point at it explicitly).
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
_SRC = _ROOT / "src"
_LAB = _ROOT / "reports" / "research" / "channel_lab"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from tmf_channel.legacy_helpers import (  # noqa: E402
    COST,
    arrays,
    load_merged_tmf,
    slope_at,
    summarize,
    teacher_match,
)

DIR = _LAB  # research JSON / PNG still under channel_lab
ROOT = _ROOT
SRC = _SRC

from research.intraday_direction_thermometer import (  # noqa: E402
    Bar,
    fuse_ta30m_bias,
    short_temp_from_bars,
    ta30m_factors_from_bars,
)

VOL_WIN = 20
EXPAND = 1.50
CONTRACT = 0.70
CLIMAX = 2.50
DRY = 0.45

HANG_LO = 30.0
HANG_HI = 60.0
STRUCT_LOOK = 30
STRUCT_PAD = 2.0
NOISE_FLOOR = 12.0
PIVOT_W = 2  # local swing half-width (bars)

VIX_EPS = 0.2


def build_anchor_series(O, C, mode: str | None, n: int = 15) -> list[float]:
    """PIT hang-placement anchor, computed once over the full bar array.

    mode:
      "O"   — O[t] (known at the instant bar t begins).
      "C"   — C[t] (the ORIGINAL LOOK-AHEAD BUG; sanity tripwire only).
      "ema" — causal EMA of closes strictly through t-1 (n=1 == C[t-1]).
              At t, only O[0..t] and C[0..t-1] have been observed by the
              time bar t's own ticks start printing; the EMA state is
              updated with C[t] only AFTER computing out[t], so out[t]
              never depends on anything from bar t itself.
    """
    mode = str(mode or "O")
    n_bars = len(C)
    if mode == "O":
        return [float(x) for x in O]
    if mode == "C":
        return [float(x) for x in C]
    if mode == "ema":
        eff_n = max(1, int(n))
        alpha = 2.0 / (eff_n + 1.0)
        out = [0.0] * n_bars
        ema_state = None
        for t in range(n_bars):
            out[t] = float(O[t]) if ema_state is None else float(ema_state)
            c_t = float(C[t])
            ema_state = c_t if ema_state is None else ema_state + alpha * (c_t - ema_state)
        return out
    raise ValueError(f"unknown hang_anchor mode: {mode!r}")


def rvol_series(V, win=VOL_WIN):
    out = [None] * len(V)
    for t in range(len(V)):
        a = max(0, t - win + 1)
        med = st.median(V[a : t + 1]) or 1.0
        out[t] = V[t] / med
    return out


def vol_off(H, L, t, look=60, lo=HANG_LO, hi=HANG_HI):
    a = max(0, t - look + 1)
    rng = max(H[a : t + 1]) - min(L[a : t + 1])
    span = hi - lo
    off = lo + min(span, max(0.0, (rng - 80.0) * 0.2))
    return min(hi, max(lo, off))


def range_hang_band(
    H,
    L,
    t,
    *,
    look: int = 60,
    k: float = 0.35,
    floor: float = 12.0,
    cap: float = 60.0,
    lo_frac: float = 0.55,
    min_span: float = 8.0,
    measure: str = "envelope",
    a_start: int | None = None,
) -> tuple[float, float]:
    """Realtime hang band from PIT price activity (session-agnostic formula).

    measure:
      envelope — max(H)-min(L) over window (outlier-sensitive)
      tr_median — median(H-L) per bar (typical activity)
      tr_p75 — 75th pct of per-bar ranges
    a_start: optional inclusive left index (e.g. session open); still capped by look
      unless look<=0 (then use full [a_start, t]).
    """
    if a_start is None:
        a = max(0, t - max(look, 1) + 1)
    elif look and look > 0:
        a = max(int(a_start), max(0, t - look + 1))
    else:
        a = max(0, int(a_start))
    if a > t:
        a = t
    measure = str(measure or "envelope")
    if measure == "tr_median":
        trs = [float(H[i] - L[i]) for i in range(a, t + 1)]
        scale = float(st.median(trs)) if trs else 0.0
    elif measure == "tr_p75":
        trs = sorted(float(H[i] - L[i]) for i in range(a, t + 1))
        if not trs:
            scale = 0.0
        else:
            scale = float(trs[int(0.75 * (len(trs) - 1))])
    else:
        window_h = H[a : t + 1]
        window_l = L[a : t + 1]
        scale = float(max(window_h) - min(window_l)) if window_h else 0.0
    hi = min(cap, max(floor + min_span, k * scale))
    lo = min(hi - min_span, max(floor, hi * lo_frac))
    if lo < floor:
        lo = floor
    if hi < lo + min_span:
        hi = lo + min_span
    return float(lo), float(hi)


def session_open_index(T: list, t: int) -> int:
    """First bar index of the current TAIFEX day or night segment (PIT walk-back)."""
    hm = _hhmm(T[t])
    in_day = "08:45" <= hm <= "13:45"
    in_night = hm >= "15:00" or hm < "05:00"
    i = t
    while i > 0:
        prev = _hhmm(T[i - 1])
        if in_day:
            if not ("08:45" <= prev <= "13:45"):
                break
        elif in_night:
            if not (prev >= "15:00" or prev < "05:00"):
                break
        else:
            break
        i -= 1
    return i


def classify_pv(C, O, rvol, t, look=5):
    if rvol[t] is None or t < 1:
        return "na", 0.0
    rv = rvol[t]
    a = max(0, t - look)
    impulse = C[t] - C[a]
    up = impulse > 0
    if rv >= CLIMAX:
        return ("climax_up" if up else "climax_dn"), impulse
    if rv >= EXPAND:
        return ("expand_up" if up else "expand_dn"), impulse
    if rv <= DRY:
        return "dry", impulse
    if rv <= CONTRACT:
        return "contract", impulse
    hh = C[t] >= max(C[a : t + 1]) - 1e-9
    if hh and rv < 1.0 and impulse > 0:
        return "div_hh_weak_vol", impulse
    return "normal", impulse


def _round_level(px: float, step: float = 100.0) -> float:
    return round(px / step) * step


def load_gap_bias_map(days: list[str] | None = None) -> dict[str, str]:
    """Load gap_bias from reports/daily/morning-gate/morning_gate_{day}.json."""
    try:
        from report_paths import REPORTS_ROOT

        gate_dir = REPORTS_ROOT / "daily" / "morning-gate"
    except Exception:
        gate_dir = ROOT / "reports" / "daily" / "morning-gate"
    out: dict[str, str] = {}
    paths = list(gate_dir.glob("morning_gate_*.json")) if days is None else [
        gate_dir / f"morning_gate_{d}.json" for d in days
    ]
    for path in paths:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        day = str(payload.get("session") or path.stem.replace("morning_gate_", ""))
        bias = str(payload.get("gap_bias") or "flat")
        if bias not in ("S", "L", "flat"):
            bias = "flat"
        out[day] = bias
    return out


def load_vixtwn_delta(db_path: Path | None = None, source: str = "computed") -> dict[str, float]:
    """date → Δclose vs prior session (PIT when using lag1)."""
    if db_path is None:
        for cand in (
            Path.home() / "goldenstocks-data/data/stocks.db",
            ROOT / "data/stocks.db",
        ):
            if cand.exists():
                db_path = cand
                break
    if db_path is None or not db_path.exists():
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT date, close FROM market_vix_daily WHERE symbol='VIXTWN' AND source=? ORDER BY date",
        (source,),
    ).fetchall()
    closes = {d: float(c) for d, c in rows if c is not None}
    dates = sorted(closes)
    out: dict[str, float] = {}
    for i, d in enumerate(dates):
        if i == 0:
            continue
        out[d] = closes[d] - closes[dates[i - 1]]
    return out


def load_vixtwn_1m(db_path: Path | None = None) -> dict[str, dict[str, float]]:
    """day → {HH:MM → close} for VIXTWN 1m (day session only)."""
    if db_path is None:
        for cand in (
            Path.home() / "goldenstocks-data/data/stocks.db",
            ROOT / "data/stocks.db",
        ):
            if cand.exists():
                db_path = cand
                break
    if db_path is None or not db_path.exists():
        return {}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out: dict[str, dict[str, float]] = {}
    for ts, c in con.execute(
        "SELECT ts, close FROM market_vix_1m WHERE symbol='VIXTWN' ORDER BY ts"
    ):
        d, hm = str(ts)[:10], str(ts)[11:16]
        out.setdefault(d, {})[hm] = float(c)
    con.close()
    return out


def vixtwn_hang_adj(
    *,
    mode: str,
    d1: float | None,
    d5: float | None,
    dopen: float | None,
    gamma: float,
    cap: float,
    eps: float,
    ext_thr: float,
) -> float:
    """Symmetric hang offset in pts. adj>0 → closer short / farther long.

    Modes (research):
      none
      reverse_dopen — VIX↑ from open → favor S (session-end reverse IC)
      reverse_dopen_ext — same, only |dopen|≥ext_thr
      mom_d5 — VIX↑ over 5m → favor L (short-horizon same-sign IC)
      blend — 0.7*reverse_dopen + 0.3*(-mom_d5 signal as favor-S)
    """
    if mode in ("", "none", "off"):
        return 0.0

    def _clip(x: float) -> float:
        return max(-cap, min(cap, x))

    if mode == "reverse_dopen":
        if dopen is None or abs(dopen) < eps:
            return 0.0
        return _clip(gamma * float(dopen))
    if mode == "reverse_dopen_ext":
        if dopen is None or abs(dopen) < max(eps, ext_thr):
            return 0.0
        return _clip(gamma * float(dopen))
    if mode == "mom_d5":
        if d5 is None or abs(d5) < eps:
            return 0.0
        # VIX↑ → favor L → adj < 0
        return _clip(-gamma * float(d5))
    if mode == "reverse_d1":
        if d1 is None or abs(d1) < eps:
            return 0.0
        return _clip(gamma * float(d1))
    if mode == "blend":
        a = 0.0
        if dopen is not None and abs(dopen) >= eps:
            a += 0.7 * gamma * float(dopen)
        if d5 is not None and abs(d5) >= eps:
            a += 0.3 * (-gamma * float(d5))  # mom → favor L
        return _clip(a) if abs(a) >= eps * gamma * 0.25 else 0.0
    return 0.0


def us_vix_hang_adj(
    *,
    mode: str,
    d_on: float | None,
    d_sess: float | None,
    gamma: float,
    cap: float,
    eps: float,
    is_night: bool,
    nq_pct: float | None = None,
    beta: float = -0.8,
    eps_nq: float = 0.05,
    resid: float | None = None,
) -> float:
    """US VIX hang offset for TW night (no VIXTWN prints). adj>0 → favor S.

    Modes:
      none
      reverse_don — raw Δ vs prior US RTH close (NOT NQ-calibrated; research reject)
      reverse_dsess / blend_night — session-relative variants of raw Δ
      resid_nq — use precomputed/orthogonal residual (d_on − β·nq_pct)
      resid_nq_live — same residual computed here with fixed β
      vix_when_nq_flat — raw d_on only when |nq| < eps_nq
      resid_when_nq_moved — residual only when |nq| ≥ eps_nq
    """
    if mode in ("", "none", "off") or not is_night:
        return 0.0

    def _clip(x: float) -> float:
        return max(-cap, min(cap, x))

    def _resid() -> float | None:
        if resid is not None:
            return float(resid)
        if d_on is None or nq_pct is None:
            return None
        return float(d_on) - float(beta) * float(nq_pct)

    if mode == "reverse_don":
        if d_on is None or abs(d_on) < eps:
            return 0.0
        return _clip(gamma * float(d_on))
    if mode == "reverse_dsess":
        if d_sess is None or abs(d_sess) < eps:
            return 0.0
        return _clip(gamma * float(d_sess))
    if mode == "blend_night":
        a = 0.0
        if d_on is not None and abs(d_on) >= eps:
            a += 0.7 * gamma * float(d_on)
        if d_sess is not None and abs(d_sess) >= eps:
            a += 0.3 * gamma * float(d_sess)
        return _clip(a) if abs(a) >= eps * gamma * 0.25 else 0.0
    if mode in ("resid_nq", "resid_nq_live"):
        r = _resid()
        if r is None or abs(r) < eps:
            return 0.0
        return _clip(gamma * float(r))
    if mode == "vix_when_nq_flat":
        if nq_pct is not None and abs(float(nq_pct)) >= eps_nq:
            return 0.0
        if d_on is None or abs(d_on) < eps:
            return 0.0
        return _clip(gamma * float(d_on))
    if mode == "resid_when_nq_moved":
        if nq_pct is None or abs(float(nq_pct)) < eps_nq:
            return 0.0
        r = _resid()
        if r is None or abs(r) < eps:
            return 0.0
        return _clip(gamma * float(r))
    return 0.0


def load_us_vix_1h_cache(path: Path | None = None) -> dict[str, dict[str, float]]:
    """Load Yahoo ^VIX 1h cache → day(TW) → {HH:MM → close} for TW night hours.

    Also attaches '_prior_close' per day = last US RTH close before that TW calendar day.
    Stored as day → {"__prior__": float, "15:00": ..., ...}.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    TZ_TW = ZoneInfo("Asia/Taipei")
    TZ_ET = ZoneInfo("America/New_York")
    if path is None:
        path = DIR / "us_vix_intraday_1h_cache.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    recs = raw.get("records") or []
    # ET series
    series: list[tuple[datetime, float]] = []
    for r in recs:
        ts = datetime.fromisoformat(str(r["Datetime"]))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=TZ_ET)
        else:
            ts = ts.astimezone(TZ_ET)
        series.append((ts, float(r["Close"])))
    series.sort(key=lambda x: x[0])
    # US RTH close per US date
    us_close: dict[str, float] = {}
    for ts, px in series:
        if ts.hour < 16 or (ts.hour == 16 and ts.minute == 0):
            us_close[ts.date().isoformat()] = px
    # Map to TW calendar day + hm
    out: dict[str, dict[str, float]] = {}
    for ts, px in series:
        tw = ts.astimezone(TZ_TW)
        d = tw.date().isoformat()
        hm = f"{tw.hour:02d}:{tw.minute:02d}"
        # keep night-relevant + late day (US morning)
        if not (hm >= "15:00" or hm < "05:00" or hm >= "20:00"):
            # still store — engine filters by night
            pass
        out.setdefault(d, {})[hm] = px
    # attach prior US close
    us_dates = sorted(us_close)
    for d in list(out.keys()):
        prior = [x for x in us_dates if x < d]
        if prior:
            out[d]["__prior__"] = us_close[prior[-1]]
    return out


def nq_hang_adj(
    *,
    mode: str,
    nq_pct: float | None,
    tx_from_open: float | None,
    gamma: float,
    cap: float,
    eps_nq: float,
    eps_tx: float,
    stretch_tx: float = 0.0,
    day_only: bool = False,
    is_night: bool = False,
) -> float:
    """NQ hang offset. adj>0 → closer short / farther long.

    Modes:
      none
      always_nq — soft pull toward NQ overnight sign (even if TX aligned)
      div_tx — only when NQ sign ≠ TX-from-session-open sign (both directed)
      same_tx — only when aligned (reinforce; expect weaker)
      div_stretch — div_tx and |TX-from-open| ≥ stretch_tx (stretched against NQ)
    """
    if mode in ("", "none", "off") or nq_pct is None:
        return 0.0
    if day_only and is_night:
        return 0.0

    def _clip(x: float) -> float:
        return max(-cap, min(cap, x))

    nq = float(nq_pct)
    nq_s = 1 if nq >= eps_nq else (-1 if nq <= -eps_nq else 0)
    if nq_s == 0:
        return 0.0
    mag = min(2.0, abs(nq) / max(eps_nq, 1e-6))
    pull = -nq_s * gamma * mag

    if mode == "always_nq":
        return _clip(pull)

    tx = float(tx_from_open) if tx_from_open is not None else 0.0
    tx_s = 1 if tx >= eps_tx else (-1 if tx <= -eps_tx else 0)
    if tx_s == 0:
        return 0.0
    if mode in ("div_tx", "div_stretch"):
        if nq_s == tx_s:
            return 0.0
        if mode == "div_stretch" and abs(tx) < max(stretch_tx, eps_tx):
            return 0.0
        return _clip(pull)
    if mode == "same_tx":
        if nq_s != tx_s:
            return 0.0
        return _clip(0.5 * pull)
    return 0.0


def nq_side_conf(
    side: str,
    nq_pct: float | None,
    tx_from_open: float | None,
    *,
    eps_nq: float = 0.05,
    eps_tx: float = 20.0,
) -> tuple[int, int, int]:
    """Return (conf, nq_s, tx_s).

    conf: +1 side with NQ, -1 against, 0 flat NQ.
    nq_s/tx_s: +1 up, -1 down, 0 flat.
    """
    if nq_pct is None:
        return 0, 0, 0
    nq = float(nq_pct)
    nq_s = 1 if nq >= eps_nq else (-1 if nq <= -eps_nq else 0)
    tx = float(tx_from_open) if tx_from_open is not None else 0.0
    tx_s = 1 if tx >= eps_tx else (-1 if tx <= -eps_tx else 0)
    if nq_s == 0:
        return 0, 0, tx_s
    if side == "L":
        conf = 1 if nq_s > 0 else -1
    elif side == "S":
        conf = 1 if nq_s < 0 else -1
    else:
        conf = 0
    return conf, nq_s, tx_s


def nq_early_gamma_scale(
    conf: int,
    nq_s: int,
    tx_s: int,
    *,
    k: float,
    div_bonus: float,
    same_penalty: float,
) -> float:
    """Multiplier for early_fill_gamma. >1 more eager, <1 less."""
    scale = 1.0 + float(k) * float(conf)
    if conf > 0 and nq_s != 0 and tx_s != 0:
        if tx_s != nq_s:
            scale += float(div_bonus)  # TX stretched against NQ → fill sooner with-NQ
        else:
            scale -= float(same_penalty)  # already extended with NQ → don't chase
    return max(0.0, scale)


def _parse_ts(ts: str) -> datetime:
    s = str(ts).replace("T", " ")[:16]
    for fmt in ("%Y-%m-%d %H:%M", "%H:%M"):
        try:
            return datetime.strptime(s if len(s) > 5 else f"2000-01-01 {s}", fmt if len(s) > 5 else "%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return datetime(2000, 1, 1)


def _local_pivots(vals, a: int, t: int, w: int = PIVOT_W, *, kind: str) -> list[float]:
    """PIT local swing extremes in [a, t) — completed bars only."""
    out: list[float] = []
    if t - a < 2 * w + 1:
        return out
    for i in range(a + w, t - w):
        window = vals[i - w : i + w + 1]
        v = vals[i]
        if kind == "high" and v >= max(window) - 1e-12:
            out.append(float(v))
        elif kind == "low" and v <= min(window) + 1e-12:
            out.append(float(v))
    return out


def _pick_hang_above(spot: float, levels: list[float], *, lo: float, hi: float, pad: float) -> float:
    """Choose short hang on a touchable rail above spot.

    Prefer real structure in [lo, hi] from spot; if none, nearest intermediate
    pivot still above noise; last resort = spot+lo (never float mid-air at +hi
    without a structure reason).
    """
    cands = sorted({round(x + pad, 1) for x in levels if x + pad > spot + NOISE_FLOOR * 0.5})
    in_band = [x for x in cands if lo <= x - spot <= hi]
    if in_band:
        # Prefer nearer rails (more likely to be touched on pullback)
        return min(in_band, key=lambda x: x - spot)
    # Too close only → step out to lo; too far → use nearest still ≤ hi if any
    below_hi = [x for x in cands if x - spot <= hi]
    if below_hi:
        return max(below_hi)  # highest reachable structure inside band
    above = [x for x in cands if x - spot > hi]
    if above:
        # Structure beyond band: hang at hi toward that rail (price path often tags it)
        return spot + hi
    return spot + lo


def _pick_hang_below(spot: float, levels: list[float], *, lo: float, hi: float, pad: float) -> float:
    cands = sorted({round(x - pad, 1) for x in levels if x - pad < spot - NOISE_FLOOR * 0.5}, reverse=True)
    in_band = [x for x in cands if lo <= spot - x <= hi]
    if in_band:
        return max(in_band)  # nearest rail below spot
    inside = [x for x in cands if spot - x <= hi]
    if inside:
        return min(inside)  # deepest reachable structure inside band
    if any(spot - x > hi for x in cands):
        return spot - hi
    return spot - lo


def structure_wants(H, L, C, t, *, look=STRUCT_LOOK, lo=HANG_LO, hi=HANG_HI, pad=STRUCT_PAD):
    """Hang on prior pivot rails (touchable), not floating C+offset."""
    a = max(0, t - look)
    piv_h = _local_pivots(H, a, t, PIVOT_W, kind="high")
    piv_l = _local_pivots(L, a, t, PIVOT_W, kind="low")
    # Always include rolling extremes + recent micro extremes as candidates
    if t > a:
        sh = max(H[a:t])
        sl = min(L[a:t])
        piv_h = piv_h + [sh]
        piv_l = piv_l + [sl]
        a8 = max(a, t - 8)
        if t > a8:
            piv_h.append(max(H[a8:t]))
            piv_l.append(min(L[a8:t]))
    else:
        sh, sl = H[t], L[t]
        piv_h, piv_l = [sh], [sl]

    want_s = _pick_hang_above(C[t], piv_h, lo=lo, hi=hi, pad=pad)
    want_l = _pick_hang_below(C[t], piv_l, lo=lo, hi=hi, pad=pad)
    return want_s, want_l, sh, sl


def _bars_1m_upto(O, H, L, C, V, T, t) -> list[Bar]:
    """Completed 1m bars with index < t (PIT)."""
    out = []
    for i in range(max(0, t)):
        out.append(
            Bar(
                ts=_parse_ts(T[i]),
                open=float(O[i]),
                high=float(H[i]),
                low=float(L[i]),
                close=float(C[i]),
                volume=float(V[i] or 0),
            )
        )
    return out


def _resample_5m(bars_1m: list[Bar]) -> list[Bar]:
    if not bars_1m:
        return []
    buckets: dict[str, list[Bar]] = {}
    order: list[str] = []
    for b in bars_1m:
        # floor to 5m
        m = (b.ts.minute // 5) * 5
        key = b.ts.replace(minute=m, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M")
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(b)
    out = []
    # drop incomplete last bucket if wall clock still inside it — caller passes completed only
    for key in order:
        g = buckets[key]
        out.append(
            Bar(
                ts=g[0].ts.replace(minute=(g[0].ts.minute // 5) * 5, second=0, microsecond=0),
                open=g[0].open,
                high=max(x.high for x in g),
                low=min(x.low for x in g),
                close=g[-1].close,
                volume=sum(x.volume for x in g),
            )
        )
    return out


def precompute_thermo(O, H, L, C, V, T, step: int = 5) -> list[dict]:
    """O(n) PIT thermo tilt: incremental 5m buckets + thermometer formulas."""
    n = len(C)
    out: list[dict] = [dict(tilt=0, short_temp=0, ta30m=0, raw_mom=0, fade_ext=0) for _ in range(n)]
    bars5: list[Bar] = []
    bucket: list[Bar] = []
    bucket_key = None
    last = out[0]

    def flush_bucket():
        nonlocal bucket
        if len(bucket) < 5:
            return
        g = bucket
        bars5.append(
            Bar(
                ts=g[0].ts.replace(minute=(g[0].ts.minute // 5) * 5, second=0, microsecond=0),
                open=g[0].open,
                high=max(x.high for x in g),
                low=min(x.low for x in g),
                close=g[-1].close,
                volume=sum(x.volume for x in g),
            )
        )

    for t in range(n):
        b = Bar(
            ts=_parse_ts(T[t]),
            open=float(O[t]),
            high=float(H[t]),
            low=float(L[t]),
            close=float(C[t]),
            volume=float(V[t] or 0),
        )
        key = (b.ts.date(), b.ts.hour, (b.ts.minute // 5) * 5)
        if bucket_key is None:
            bucket_key = key
            bucket = [b]
        elif key == bucket_key:
            bucket.append(b)
        else:
            flush_bucket()
            bucket_key = key
            bucket = [b]

        # signal uses completed bars only → exclude current incomplete 1m from mom
        # evaluate using bars5 already flushed + raw mom from closes[:t]
        if t % step != 0 and t > 0:
            out[t] = last
            continue

        short = short_temp_from_bars(bars5) if bars5 else None
        factors = ta30m_factors_from_bars(bars5) if len(bars5) >= 6 else {"ready": False}
        ta30 = (
            fuse_ta30m_bias(
                factors,
                mode="score",
                keys=("mom30", "vwap", "or_break", "short_mom", "fade_ext"),
                score_thresh=2,
            )
            if factors.get("ready")
            else 0
        )
        raw_mom = 0
        if t >= 3 and C[t - 3]:
            r = (C[t - 1] / C[t - 3] - 1.0) * 100.0
            if r > 0.02:
                raw_mom = 1
            elif r < -0.02:
                raw_mom = -1
        stemp = int(short.temp) if short and short.ready and short.temp is not None else 0
        tilt = 0
        tilt += 1 if stemp >= 1 else (-1 if stemp <= -1 else 0)
        tilt += int(ta30)
        tilt += raw_mom
        tilt = max(-2, min(2, tilt))
        last = dict(
            tilt=tilt,
            short_temp=stemp,
            ta30m=int(ta30),
            raw_mom=raw_mom,
            fade_ext=int(factors.get("fade_ext") or 0),
        )
        out[t] = last
    return out


def thermo_tilt(O, H, L, C, V, T, t) -> dict:
    series = precompute_thermo(O, H, L, C, V, T, step=5)
    return series[min(t, len(series) - 1)]


def _hhmm(ts: str) -> str:
    """Return HH:MM. Supports 'YYYY-mm-dd HH:MM:SS' and ISO '...T08:45:00+08:00'."""
    s = str(ts)
    if "T" in s:
        return s.split("T", 1)[1][:5]
    return s.split()[-1][:5]


def _day(ts: str) -> str:
    return str(ts)[:10]


def _is_boundary(prev_t, cur_t) -> str | None:
    if not prev_t or not cur_t:
        return None
    p, c = _hhmm(prev_t), _hhmm(cur_t)
    if p <= "13:45" and c >= "15:00":
        return "day_to_night"
    if p >= "04:50" and c.startswith("08:"):
        return "night_to_day"
    return None


def hang_process_metrics(ws, wl, events) -> dict:
    """Process metrics: stickiness / amend churn (not PnL)."""
    from collections import Counter

    kinds = Counter(e["kind"] for e in events)

    def _lens(series):
        out = []
        i = 0
        n = len(series)
        while i < n:
            if series[i] is None:
                i += 1
                continue
            j = i + 1
            while j < n and series[j] == series[i]:
                j += 1
            out.append(j - i)
            i = j
        return out

    lens = _lens(ws) + _lens(wl)
    return dict(
        place=kinds.get("place", 0),
        amend=kinds.get("amend", 0),
        cancel=kinds.get("cancel", 0),
        fill=kinds.get("fill", 0),
        horiz_len_med=round(st.median(lens), 1) if lens else None,
        horiz_len_mean=round(st.mean(lens), 1) if lens else None,
        horiz_len_p90=sorted(lens)[int(0.9 * (len(lens) - 1))] if lens else None,
        n_unique_s=len({round(x, 1) for x in ws if x is not None}),
        n_unique_l=len({round(x, 1) for x in wl if x is not None}),
    )


def simulate(
    O,
    H,
    L,
    C,
    V,
    T,
    params=None,
    vix_delta: dict[str, float] | None = None,
    tick_index=None,
):
    p = dict(
        hang_lo=HANG_LO,
        hang_hi=HANG_HI,
        struct_look=STRUCT_LOOK,
        struct_pad=STRUCT_PAD,
        amend=3,  # chase mode only; sticky ignores reprice clock
        verify=6,
        tip_keep=80.0,
        machine_shave=0.0,
        cover_boost=8.0,
        climax_cover_extra=6.0,
        cover_boost_cap=80.0,
        expand_tip_extra=5.0,
        contract_both_extra=5.0,
        dry_park_extra=10.0,
        allow_flip=False,
        max_lots=2,
        dry_block=False,
        contract_no_flip=False,
        contract_amend_slow=1,
        scale_only_expand=False,
        no_scale_contract=False,
        scale_min_ext_pts=0.0,
        scale_min_rvol=0.0,
        shared_cover=False,
        round_cover=False,
        round_step=100.0,
        round_cover_after_pts=150.0,
        # VIXTWN session bias (default ON · lag1 · not same-day close)
        vix_session_bias=True,
        vix_eps=VIX_EPS,
        vix_pit="lag1",
        # Research-only overlay (default OFF · live recipe must not set these):
        # when lag1 ΔVIXTWN ≤ crash_max AND prior session ret ≥ rally_min at a
        # session boundary → flatten LONGs only (short-carry bias). Does not
        # change the default vix_up/vix_dn/vix_flat rules unless the joint
        # condition fires (then that boundary skips the default VIX block).
        vix_crash_rally_short=False,
        vix_crash_max_delta=-2.0,  # pts (more negative = deeper crash)
        vix_rally_min_pts=80.0,  # prior day session net pts
        prior_day_ret_pts=None,  # optional dict day→prior session ret pts
        # thermo: side selection only under sticky
        use_thermo=True,
        thermo_closer=12.0,
        thermo_farther=8.0,
        skip_counter_tilt=True,
        # Dual hang is the jack-spec default (day/night same). Thermo may still
        # reject *entries* via skip_counter_tilt; it must not leave only one rail.
        favored_side_hang=False,
        # P1 sticky horizontal rails
        sticky=True,
        rehang_min_pts=0.0,  # allow immediate rehang after cancel (keep dual rails)
        sticky_max_dist=150.0,  # cancel when |rail-spot| exceeds (still horizontal until then)
        cross_bar_only=True,
        place_every=3,  # how often to try PLACE when empty (not amend)
        eod_flatten=True,  # live paper: False so last bar ≠ session end
        # P2 morning gap window + P3 first-hour thermo weaken
        use_gap_bias=True,
        gap_bias_by_day=None,  # optional dict day→{S,L,flat}; else load JSON
        gap_window_start="08:45",
        gap_window_end="09:15",
        thermo_h1_until="09:45",  # weaken thermo side bias until this hhmm
        thermo_h1_scale=0.0,  # 0 = off favored_side from thermo in H1
        # Bidirectional recipe: dual hang, no open_bias (up-days still short),
        # hang 30–60 (less near opp_cover chop than 20–40), stop150+min12.
        # in_pos_hang=both keeps opp cover liquidity; same_only worse on bake-off net.
        in_pos_hang="both",
        stop_pts=150.0,
        min_hold_before_stop=12,
        max_hold_bars=0,
        night_entries=True,
        night_hang_scale=0.5,
        # Optional absolute night band (research / day-only tighten).
        # When both set, replaces night_hang_scale mapping for session_scale.
        night_hang_lo=None,
        night_hang_hi=None,
        # hang_band_mode:
        #   session_scale — fixed hang_lo/hi + night_hang_scale (Final v1.1.1)
        #   range_realtime — PIT rolling range → (lo,hi); ignores night_hang_scale
        hang_band_mode="session_scale",
        hang_band_measure="envelope",  # envelope | tr_median | tr_p75
        hang_band_scope="rolling",  # rolling | session
        range_band_look=60,
        range_band_k=0.35,
        range_band_floor=12.0,
        range_band_cap=60.0,
        range_band_lo_frac=0.55,
        open_bias_pts=0.0,
        skip_quiet_regime=True,  # no new hangs in contract/dry when flat
        # Research: "both" (contract+dry) | "dry" | "none" — overrides bool if set
        skip_quiet_mode=None,
        # pv_side_block: {"day": {"expand_up": ["S"], ...}, "night": {...}}
        # Blocks new place (and cancels that-side flat rails) for session×pv×side.
        pv_side_block=None,
        day_dir_filter=False,
        day_dir_thresh_pts=80.0,
        day_dir_map=None,
        # Final v1.1: hybrid exits + symmetric regime hang dampen
        # opp | trail | structure | far_opp | hybrid_trail
        exit_mode="hybrid_trail",
        trail_arm_pts=50.0,  # need this much favorable MFE before trail arms
        trail_giveback_pts=40.0,  # exit when giveback from peak ≥ this
        struct_exit_look=12,  # bars for adverse swing break
        far_cover_lo=80.0,  # opposite rail band when exit_mode=far_opp / hybrid
        far_cover_hi=120.0,
        min_hold_before_smart=3,  # bars before trail/structure may fire
        # Flat-only: expand/climax_up → no new S hang; expand/climax_dn → no new L
        trend_hang_dampen="regime",
        dampen_tilt=1,
        dampen_mom_pts=40.0,
        dampen_mom_look=15,
        dampen_night_open_pts=80.0,
        # Price improvement on gap-through: fill at open (better than limit).
        # Set False to force fill-at-limit (research counterfactual).
        gap_fill_improve=True,
        # After gap-improve entry: delay structure-break exits so gift isn't
        # immediately given back (trail/stop/opp still active). 0 = off.
        # Bake-off: grace=5 small +net; long grace raises keep% but hurts net.
        improv_struct_grace_bars=5,
        improv_struct_min_pts=5.0,  # only if entry gift ≥ this
        # If true: also skip struct while trail not yet armed (peak_fav < arm)
        improv_struct_until_trail=False,
        # v1.2 candidate: session-open greedy lottery tickets (default OFF)
        # Flat-only; absolute rails from session open; expire if unfilled.
        open_greed=False,
        open_greed_bars=15,  # bars after day 08:45 / night 15:00
        open_greed_lo=80.0,
        open_greed_hi=120.0,
        open_greed_night_lo=40.0,
        open_greed_night_hi=60.0,
        open_greed_asym="none",  # none | buy_favor (deeper L, nearer S)
        open_greed_sides="both",  # both | L | S — which greed tickets to place
        open_greed_s_cap=60.0,  # buy_favor: cap S offset from open
        open_greed_force_place=True,  # try PLACE every bar in window when flat
        open_greed_bypass_quiet=True,  # allow greed hangs in contract/dry
        open_greed_cancel_unfilled=True,  # cancel greed rails at window end if flat
        open_greed_sticky_max=250.0,  # loosen too_far while greed ticket lives
        # H-CAUSAL-ANCHOR: hang-placement anchor mode (None = legacy default,
        # i.e. O if tick_native else C, matching upstream lot_sizing_variant_
        # tick_native.py exactly). Set explicitly to "O" | "C" | "ema".
        hang_anchor=None,
        hang_anchor_ema_n=15,
        # Research overlays (default OFF) — NQ bias / near-rail early fill
        # session_side_gate: dict day→{"L","S","none"} applied when flat (night/day).
        session_side_gate=None,
        # early_fill_gamma: tighten birth hang toward spot by γ pts when
        # approach+speed+rvol fire (R6); 0 = off.
        early_fill_gamma=0.0,
        early_speed_min=6.0,
        early_rvol_min=1.5,
        early_rvol_look=20,
        # Blindspot fixes (default OFF)
        day_entries=True,  # False = no new day-session entries
        day_block_long=False,  # True = reject day L enters (day L toxic)
        struct_disabled=False,  # True = skip struct_break exits
        day_struct_disabled=False,  # True = skip struct_break only in day session
        entry_cid_blocklist=None,  # iterable of "U5","D3",... post-hoc 8-class at enter
        day_hang_lo=None,  # override day hang band (else hang_lo)
        day_hang_hi=None,
        # VIXTWN 1m hang calibration (research; default OFF). Symmetric L/S.
        # vixtwn_1m: day→{HH:MM→close}; modes in vixtwn_hang_adj().
        vixtwn_1m=None,
        vixtwn_calib="none",
        vixtwn_calib_gamma=5.0,  # hang pts per VIX point
        vixtwn_calib_cap=12.0,
        vixtwn_calib_eps=0.3,
        vixtwn_calib_ext_thr=1.0,
        # US VIX 1h hang calib for TW night (Yahoo ^VIX; default OFF)
        us_vix_1h=None,  # day→{HH:MM→close, __prior__: prior US close}
        us_vix_calib="none",
        us_vix_calib_gamma=6.0,
        us_vix_calib_cap=12.0,
        us_vix_calib_eps=0.3,
        us_vix_nq_beta=-0.8,  # VIX pts per 1% NQ overnight (neg: NQ↓ → VIX↑)
        us_vix_nq_eps=0.05,
        # session_pv_book / PV16 cells: session×PV8 → 16 recipes.
        # Each cell owns: hang_lo/hi, early_fill_gamma, max_hold_bars, block,
        # skip_quiet_mode, bias (bool; applies global session_side_gate map).
        # NOT per-cell: us_vix_* / nq_calib_* (global factor layer only).
        # Optional per-cell: vixtwn_calib / vixtwn_calib_gamma (day FinMind).
        session_pv_book=None,
        # NQ overnight hang calibration (research; default OFF). Symmetric L/S.
        # nq_on_1m: day→{HH:MM→overnight_pct}; modes in nq_hang_adj().
        nq_on_1m=None,
        nq_calib="none",
        nq_calib_gamma=4.0,  # hang pts × mag(|nq|%/eps)
        nq_calib_cap=12.0,
        nq_calib_eps_nq=0.05,  # % overnight
        nq_calib_eps_tx=20.0,  # TX pts from session open
        nq_calib_stretch_tx=40.0,  # for div_stretch mode
        nq_calib_day_only=False,  # True = skip night (US hours noisy for hang)
        calib_joint_cap=18.0,  # clip |vix_adj+nq_adj|
        # NQ confidence → EARLY / exit (research; default OFF)
        # conf(side)=+1 with-NQ, -1 against, 0 flat. Uses nq_on_1m.
        nq_conf_enable=False,
        nq_conf_early_k=0.5,  # eg_eff = eg * clamp(1 + k*conf + …)
        nq_conf_early_div_bonus=0.35,  # extra when with-NQ AND TX diverges
        nq_conf_early_same_penalty=0.25,  # less early when with-NQ AND TX already with
        nq_conf_exit_enable=False,
        nq_conf_hold_k=0.35,  # max_hold *= (1 + k*conf) ; against → shorter
        nq_conf_trail_k=0.25,  # against: arm↓ give↓ ; with: arm↑ give↑
        nq_conf_exit_mode="symmetric",  # symmetric|against_only|with_same_tighten
        nq_conf_soft_gate=False,  # True: do NOT hard-block counter-NQ; conf handles it
        nq_conf_min_eg=0.0,  # floor for eg_eff when against
        nq_conf_eps_nq=0.05,
        nq_conf_eps_tx=20.0,
        # REGIME-COMMITMENT FORK (research-only, not in upstream engine):
        # N consecutive identical raw classify_pv() labels required before a
        # new label becomes "committed" for gating; 1 = disabled (byte-
        # identical to upstream — committed == raw every bar).
        regime_commit_n=1,
    )
    if params:
        p.update(params)

    if vix_delta is None and p["vix_session_bias"]:
        vix_delta = load_vixtwn_delta()
    vix_delta = vix_delta or {}

    gap_map: dict[str, str] = dict(p.get("gap_bias_by_day") or {})
    if p.get("use_gap_bias") and not gap_map:
        gap_map = load_gap_bias_map()

    day_dir_map: dict[str, str] = dict(p.get("day_dir_map") or {})
    if p.get("day_dir_filter") and not day_dir_map and T:
        # PIT: classify each calendar day by *prior* day's open→close (no same-day peek)
        by_day: dict[str, list[int]] = {}
        for i, ts in enumerate(T):
            by_day.setdefault(_day(ts), []).append(i)
        days_sorted = sorted(by_day)
        thr = float(p.get("day_dir_thresh_pts") or 80.0)
        for j, d in enumerate(days_sorted):
            if j == 0:
                day_dir_map[d] = "flat"
                continue
            prev = by_day[days_sorted[j - 1]]
            ret = float(C[prev[-1]]) - float(C[prev[0]])
            if ret <= -thr:
                day_dir_map[d] = "S"
            elif ret >= thr:
                day_dir_map[d] = "L"
            else:
                day_dir_map[d] = "flat"

    # session day-open (first bar with hhmm>=08:45) for open_bias
    day_open: dict[str, float] = {}
    day_open_i: dict[str, int] = {}
    night_open_i: dict[str, int] = {}
    for i, ts in enumerate(T):
        d = _day(ts)
        hm0 = _hhmm(ts)
        if d not in day_open and hm0 >= "08:45":
            day_open[d] = float(C[i])
        if d not in day_open_i and "08:45" <= hm0 < "15:00":
            day_open_i[d] = i
        if d not in night_open_i and hm0 >= "15:00":
            night_open_i[d] = i

    lo, hi = float(p["hang_lo"]), float(p["hang_hi"])
    sticky = bool(p.get("sticky", True))
    rehang_min = float(p.get("rehang_min_pts", 0.0))
    sticky_max = float(p.get("sticky_max_dist", 150.0))
    gap_start = str(p.get("gap_window_start", "08:45"))
    gap_end = str(p.get("gap_window_end", "09:15"))
    h1_until = str(p.get("thermo_h1_until", "09:45"))
    h1_scale = float(p.get("thermo_h1_scale", 0.0))
    n = len(C)
    rvol = rvol_series(V)

    # Precompute VIXTWN 1m features per bar (PIT; night / missing → None)
    _v1m: dict[str, dict[str, float]] = dict(p.get("vixtwn_1m") or {})
    vix_d1: list[float | None] = [None] * n
    vix_d5: list[float | None] = [None] * n
    vix_dopen: list[float | None] = [None] * n
    if _v1m and str(p.get("vixtwn_calib") or "none") not in ("", "none", "off"):
        _day_hms: dict[str, list[str]] = {
            d: sorted(m.keys()) for d, m in _v1m.items()
        }
        _day_open_v: dict[str, float] = {}
        for d, hms in _day_hms.items():
            for hm in hms:
                if hm >= "09:00":
                    _day_open_v[d] = float(_v1m[d][hm])
                    break
        for i, ts in enumerate(T):
            d = _day(ts)
            hm = _hhmm(ts)
            dm = _v1m.get(d) or {}
            if hm not in dm or hm < "09:00" or hm > "13:45":
                continue
            vc = float(dm[hm])
            hms = _day_hms.get(d) or []
            try:
                j = hms.index(hm)
            except ValueError:
                continue
            if j >= 1:
                vix_d1[i] = vc - float(dm[hms[j - 1]])
            if j >= 5:
                vix_d5[i] = vc - float(dm[hms[j - 5]])
            if d in _day_open_v:
                vix_dopen[i] = vc - _day_open_v[d]

    # US VIX features for TW night (Δ vs prior US close / vs night-session first print)
    _uv1h: dict[str, dict[str, float]] = dict(p.get("us_vix_1h") or {})
    us_vix_don: list[float | None] = [None] * n
    us_vix_dsess: list[float | None] = [None] * n
    if _uv1h and str(p.get("us_vix_calib") or "none") not in ("", "none", "off"):
        _night_open_v: dict[str, float] = {}
        for d, m in _uv1h.items():
            hms = sorted(h for h in m if h != "__prior__" and (h >= "15:00" or h < "05:00"))
            if hms:
                # first night print chronologically: prefer >=15:00 then early morning
                eve = [h for h in hms if h >= "15:00"]
                _night_open_v[d] = float(m[eve[0] if eve else hms[0]])
        for i, ts in enumerate(T):
            d = _day(ts)
            hm = _hhmm(ts)
            is_n = hm >= "15:00" or hm < "05:00"
            if not is_n:
                continue
            dm = _uv1h.get(d) or {}
            # forward-fill last ≤ hm
            cands = [h for h in dm if h != "__prior__" and h <= hm]
            if not cands and hm < "05:00":
                # early morning: also allow previous evening keys same day
                cands = [h for h in dm if h != "__prior__" and h >= "15:00"]
            if not cands:
                continue
            vc = float(dm[max(cands)])
            prior = dm.get("__prior__")
            if prior is not None:
                us_vix_don[i] = vc - float(prior)
            if d in _night_open_v:
                us_vix_dsess[i] = vc - _night_open_v[d]

    # NQ overnight % + TX from session-open (day 08:45 / night 15:00)
    _nq1m: dict[str, dict[str, float]] = dict(p.get("nq_on_1m") or {})
    nq_on: list[float | None] = [None] * n
    tx_from_open: list[float | None] = [None] * n
    _uv_mode = str(p.get("us_vix_calib") or "none")
    _need_nq = (
        str(p.get("nq_calib") or "none") not in ("", "none", "off")
        or bool(p.get("nq_conf_enable"))
        or bool(p.get("nq_conf_exit_enable"))
        or _uv_mode
        in (
            "resid_nq",
            "resid_nq_live",
            "vix_when_nq_flat",
            "resid_when_nq_moved",
        )
    )
    if _need_nq:
        _sess_open_i: dict[str, int] = {}
        for i, ts in enumerate(T):
            d = _day(ts)
            hm = _hhmm(ts)
            if d not in _sess_open_i:
                if hm >= "08:45":
                    _sess_open_i[d] = i
            # prefer explicit night open when crossing 15:00
            if hm >= "15:00":
                key = f"{d}|N"
                if key not in _sess_open_i:
                    _sess_open_i[key] = i
        for i, ts in enumerate(T):
            d = _day(ts)
            hm = _hhmm(ts)
            dm = _nq1m.get(d) or {}
            if hm in dm:
                nq_on[i] = float(dm[hm])
            elif dm:
                # forward-fill last ≤ hm
                prev = [h for h in dm if h <= hm]
                if prev:
                    nq_on[i] = float(dm[max(prev)])
            is_night = hm >= "15:00" or hm < "05:00"
            oi = _sess_open_i.get(f"{d}|N") if is_night and hm >= "15:00" else _sess_open_i.get(d)
            if oi is not None and i >= oi:
                tx_from_open[i] = float(C[i]) - float(O[oi] if O is not None else C[oi])

    lots: list[dict] = []
    trades, events = [], []
    ws = [None] * n
    wl = [None] * n
    regime = ["na"] * n
    committed_regime = ["na"] * n
    _commit_n = max(1, int(p.get("regime_commit_n") or 1))
    _commit_last_raw: str | None = None
    _commit_run = 0
    _commit_current: str | None = None
    short_px = long_px = None
    short_born = long_born = None
    short_greed = long_greed = False  # open-greed lottery rails
    last_short_rail = last_long_rail = None
    last_place_check = -int(p["place_every"])
    last_amend = -p["amend"]
    last_entry_bar = -10**9
    # per-side peak favorable extreme while in position (for trail exits)
    peak_fav: float | None = None
    thermo_series = (
        precompute_thermo(O, H, L, C, V, T, step=5) if p["use_thermo"] else None
    )
    thermo = dict(tilt=0, short_temp=0, ta30m=0, raw_mom=0, fade_ext=0)

    def open_greed_info(t: int) -> tuple[str, int] | None:
        """Return (session, open_bar_i) if t is inside open-greed window."""
        if not p.get("open_greed"):
            return None
        hm = _hhmm(T[t])
        day = _day(T[t])
        bars = max(1, int(p.get("open_greed_bars") or 15))
        if "08:45" <= hm < "15:00":
            oi = day_open_i.get(day)
            if oi is not None and oi <= t < oi + bars:
                return ("day", oi)
        if hm >= "15:00":
            oi = night_open_i.get(day)
            if oi is not None and oi <= t < oi + bars:
                return ("night", oi)
        return None

    def greed_offsets(sess: str) -> tuple[float, float]:
        if sess == "night":
            glo = float(p.get("open_greed_night_lo") or 40.0)
            ghi = float(p.get("open_greed_night_hi") or 60.0)
        else:
            glo = float(p.get("open_greed_lo") or 80.0)
            ghi = float(p.get("open_greed_hi") or 120.0)
        if str(p.get("open_greed_asym") or "none") == "buy_favor":
            ghi = min(ghi, float(p.get("open_greed_s_cap") or 60.0))
        return glo, ghi

    def log(t, kind, side, px, note=""):
        events.append(
            dict(
                t=t,
                time=T[t],
                kind=kind,
                side=side,
                px=None if px is None else round(float(px), 1),
                note=note,
            )
        )

    def close_side(t, px, why, side=None, close_all_side=False):
        nonlocal lots, peak_fav
        closed = 0
        i = 0
        while i < len(lots):
            if side is not None and lots[i]["s"] != side:
                i += 1
                continue
            lot = lots.pop(i)
            s, ep, eb = lot["s"], lot["ep"], lot["eb"]
            pnl = ((px - ep) if s == "L" else (ep - px)) - COST
            trades.append(
                dict(
                    s=s,
                    ep=round(ep, 1),
                    xp=round(px, 1),
                    pnl=round(pnl, 1),
                    why=why,
                    eb=eb,
                    xb=t,
                    et=T[eb],
                    xt=T[t],
                    hold=t - eb,
                    open_lots=len(lots),
                    regime_e=lot.get("regime", "?"),
                    rvol_e=lot.get("rvol"),
                    how=lot.get("how"),
                    hung=lot.get("hung"),
                    improv=lot.get("improv") or 0.0,
                )
            )
            log(t, "cover", s, px, why)
            closed += 1
            if not close_all_side:
                break
        if not lots:
            peak_fav = None
        return closed > 0

    def close_one(t, px, why, prefer_side=None, close_all=False):
        if prefer_side is None and lots:
            return close_side(t, px, why, lots[0]["s"], close_all)
        return close_side(t, px, why, prefer_side, close_all)

    def can_scale(side, px, reg, rv) -> bool:
        if not lots:
            return True
        if lots[0]["s"] != side:
            return False
        if p["no_scale_contract"] and reg in ("contract", "dry"):
            return False
        first = lots[0]["ep"]
        if side == "S" and px < first + p["scale_min_ext_pts"]:
            return False
        if side == "L" and px > first - p["scale_min_ext_pts"]:
            return False
        return True

    def open_lot(t, side, px, how, reg, rv, *, honor_limit: bool = False, hung_limit=None):
        nonlocal last_entry_bar, peak_fav
        if len(lots) >= p["max_lots"]:
            log(t, "reject", side, px, f"max_lots|{len(lots)}")
            return False
        if lots and lots[0]["s"] != side:
            return False
        if lots and not can_scale(side, px, reg, rv):
            log(t, "reject", side, px, f"scale_gate|{reg}")
            return False
        if not lots and reg == "dry" and p["dry_block"]:
            return False
        # thermo may gate discretionary entries — never veto an already-touched limit
        if p["skip_counter_tilt"] and not lots and not honor_limit:
            hm0 = _hhmm(T[t])
            if not (hm0 <= h1_until and hm0 >= "08:45" and h1_scale <= 0):
                tilt = thermo.get("tilt", 0)
                if tilt <= -2 and side == "L":
                    log(t, "reject", side, px, f"thermo_tilt={tilt}")
                    return False
                if tilt >= 2 and side == "S":
                    log(t, "reject", side, px, f"thermo_tilt={tilt}")
                    return False
        how_final = "scale_in" if lots else how
        # night observe: block brand-new entries (scale of existing day pos still allowed)
        hm_n = _hhmm(T[t])
        if (
            how_final == "enter"
            and (not p.get("night_entries", True))
            and (hm_n >= "15:00" or hm_n < "05:00")
        ):
            log(t, "reject", side, px, "night_observe")
            return False
        # Blindspot: block brand-new day entries
        if (
            how_final == "enter"
            and (not p.get("day_entries", True))
            and not (hm_n >= "15:00" or hm_n < "05:00")
        ):
            log(t, "reject", side, px, "day_observe")
            return False
        # Blindspot: day long toxic under NQ-bias stack
        if (
            how_final == "enter"
            and p.get("day_block_long")
            and side == "L"
            and not (hm_n >= "15:00" or hm_n < "05:00")
        ):
            log(t, "reject", side, px, "day_block_long")
            return False
        # Research: reject enter if current pv×session×side is blocked
        _psb = p.get("pv_side_block") or {}
        if how_final == "enter":
            _sess = "night" if (hm_n >= "15:00" or hm_n < "05:00") else "day"
            _blocked = list((_psb.get(_sess) or {}).get(reg) or [])
            _spb0 = p.get("session_pv_book") or {}
            for s in list(
                ((_spb0.get(_sess) or {}).get(reg) or {}).get("block") or []
            ):
                if s not in _blocked:
                    _blocked.append(s)
            if side in _blocked:
                log(t, "reject", side, px, f"pvBlock_enter:{_sess}|{reg}")
                return False
        # Blindspot: block toxic 8-class bins (lin20 snap at enter, PIT)
        bl = p.get("entry_cid_blocklist")
        if how_final == "enter" and bl:
            look = min(20, t)
            if look >= 1:
                m8 = float((C[t] - C[t - look]) / look)
            else:
                m8 = 0.0
            ups = [3.0, 5.0, 7.0, 10.0, 13.0, 16.0, 20.0, 28.0]
            dns = [-x for x in ups]
            cat = ups if m8 >= 0 else dns
            pref = "U" if m8 >= 0 else "D"
            ii = min(range(8), key=lambda k: abs(cat[k] - m8))
            cid8 = f"{pref}{ii + 1}"
            if cid8 in set(bl):
                log(t, "reject", side, px, f"cid_block|{cid8}")
                return False
        was_flat = not lots
        improv = 0.0
        hung_f = None if hung_limit is None else float(hung_limit)
        if hung_f is not None:
            if side == "L":
                improv = max(0.0, hung_f - float(px))
            else:
                improv = max(0.0, float(px) - hung_f)
        _mh = int(p.get("max_hold_bars") or 0)
        _spb_e = p.get("session_pv_book") or {}
        _sess_e = "night" if (hm_n >= "15:00" or hm_n < "05:00") else "day"
        _cell_e = dict((_spb_e.get(_sess_e) or {}).get(reg) or {})
        if _cell_e.get("max_hold_bars") is not None:
            _mh = int(_cell_e["max_hold_bars"])
        lots.append(
            dict(
                s=side,
                ep=px,
                eb=t,
                regime=reg,
                rvol=None if rv is None else round(rv, 2),
                how=how_final,
                hung=hung_f,
                improv=round(improv, 1),
                max_hold=_mh if _mh > 0 else None,
            )
        )
        if was_flat:
            peak_fav = 0.0
        note_fill = f"{how_final}|{reg}|tilt={thermo.get('tilt', 0)}"
        if improv >= 0.5:
            note_fill += f"|improv={improv:.0f}"
        log(t, "fill", side, px, note_fill)
        last_entry_bar = t
        # H-LOT-FIXED2: first touch opens the full max_lots size immediately —
        # no waiting for a second rail touch. Same price/bar as lot 1, so it
        # does not change avg entry price, only removes the "confirm again"
        # gate that can_scale() otherwise enforces.
        if was_flat and p.get("fixed_double_entry") and p["max_lots"] >= 2:
            for _ in range(int(p["max_lots"]) - 1):
                lots.append(
                    dict(
                        s=side,
                        ep=px,
                        eb=t,
                        regime=reg,
                        rvol=None if rv is None else round(rv, 2),
                        how="fixed2",
                        hung=hung_f,
                        improv=round(improv, 1),
                    )
                )
            log(t, "fill", side, px, f"fixed2|{reg}")
        return True

    def _bar_tick_range(t: int) -> tuple[int, int]:
        if tick_index is None or t >= len(tick_index.T):
            return (0, 0)
        start = tick_index.minute_start_idx.get(tick_index.T[t])
        if start is None:
            return (0, 0)
        end = tick_index.n_tk
        if t + 1 < len(tick_index.T):
            nxt = tick_index.minute_start_idx.get(tick_index.T[t + 1])
            if nxt is not None:
                end = nxt
        return (start, end)

    def _tick_walk_bar(t: int) -> None:
        """Single real-tick pass over bar t's ticks: exit checks (stop/
        trail/struct_break) then entry/scale-in/opp_cover/flip checks, at
        EVERY tick, using short_px/long_px exactly as left by the PREVIOUS
        bar's PLACE step.

        Deliberately does NOT allow a rail placed by THIS bar's own PLACE
        step (which runs after this function, and is centered on C[t] —
        this bar's own close) to fill against this bar's ticks. That
        same-bar convention is standard and harmless at 1-min bar
        granularity (O[t] and C[t] rarely differ much in 60s), but checked
        against real ticks it becomes genuine look-ahead: C[t] is only
        knowable once the bar closes, yet ticks earlier in that same
        minute could "confirm" a rail chosen with hindsight of where the
        bar ended up closing. A rail this bar computes only becomes
        fillable starting next bar's walk (<=60s latency, in line with the
        live reconciler's own poll cadence). H-TICK-NATIVE-FIX-3: an
        earlier two-pass version restored same-bar fill to fix an
        unrelated scale-in-starvation bug and reintroduced exactly this
        leak — results swung back to a suspiciously perfect ~100% win-day /
        near-zero-drawdown pattern, the same red flag this whole
        investigation exists to catch."""
        nonlocal peak_fav, short_px, long_px, short_born, long_born
        nonlocal last_short_rail, last_long_rail
        start, end = _bar_tick_range(t)
        if start >= end:
            return
        exit_mode = str(p.get("exit_mode") or "opp")
        stop_pts = float(p.get("stop_pts") or 0.0)
        min_hold_stop = int(p.get("min_hold_before_stop") or 0)
        min_smart = int(p.get("min_hold_before_smart") or 0)
        arm = float(p.get("trail_arm_pts") or 50.0)
        give = float(p.get("trail_giveback_pts") or 40.0)
        look = int(p.get("struct_exit_look") or 12)
        reg_t = regime[t]
        rv_t = rvol[t]
        allow_flip = p["allow_flip"] and not (reg_t == "contract" and p["contract_no_flip"])
        block_exit = p.get("cross_bar_only") and t == last_entry_bar
        em = str(p.get("exit_mode") or "opp")
        allow_opp_cover = em in ("opp", "far_opp", "hybrid_trail")
        tk_px = tick_index.tk_px

        for k in range(start, end):
            px = tk_px[k]

            # --- exit checks (stop / trail / struct_break) ---
            if lots and exit_mode in ("trail", "structure", "hybrid_trail"):
                side0 = lots[0]["s"]
                ep0 = float(lots[0]["ep"])
                hold_b = t - int(lots[0]["eb"])
                fav_now = (px - ep0) if side0 == "L" else (ep0 - px)
                peak_fav = fav_now if peak_fav is None else max(peak_fav, fav_now)

                if hold_b >= min_hold_stop and stop_pts > 0:
                    adverse = -fav_now
                    if adverse >= stop_pts:
                        while lots:
                            close_side(t, px, f"stop|{adverse:.0f}", lots[0]["s"], True)
                        short_px = long_px = short_born = long_born = None

                if lots and hold_b >= min_smart and exit_mode in ("trail", "hybrid_trail"):
                    if peak_fav >= arm and (peak_fav - fav_now) >= give:
                        while lots:
                            close_side(
                                t, px, f"trail|{peak_fav:.0f}->{fav_now:.0f}", lots[0]["s"], True
                            )
                        short_px = long_px = short_born = long_born = None

                if lots and hold_b >= min_smart and exit_mode in ("structure", "hybrid_trail"):
                    _hm_s = _hhmm(T[t])
                    _day_s = not (_hm_s >= "15:00" or _hm_s < "05:00")
                    if p.get("struct_disabled") or (
                        p.get("day_struct_disabled") and _day_s
                    ):
                        pass
                    else:
                        grace = int(p.get("improv_struct_grace_bars") or 0)
                        gmin = float(p.get("improv_struct_min_pts") or 5.0)
                        improv0 = float(lots[0].get("improv") or 0.0)
                        skip_struct = bool(grace > 0 and improv0 >= gmin and hold_b < grace)
                        if (
                            p.get("improv_struct_until_trail")
                            and improv0 >= gmin
                            and peak_fav is not None
                            and peak_fav < arm
                        ):
                            skip_struct = True
                        a0 = max(0, t - look)
                        if (not skip_struct) and t - a0 >= 3:
                            if side0 == "L":
                                window = L[a0:t]
                                swing = min(window) if window else None
                                if swing is not None and px < swing:
                                    while lots:
                                        close_side(t, px, f"struct_break|{swing:.0f}", lots[0]["s"], True)
                                    short_px = long_px = short_born = long_born = None
                            else:
                                window = H[a0:t]
                                swing = max(window) if window else None
                                if swing is not None and px > swing:
                                    while lots:
                                        close_side(t, px, f"struct_break|{swing:.0f}", lots[0]["s"], True)
                                    short_px = long_px = short_born = long_born = None

            # --- entry / scale-in / opp_cover / flip checks (same tick) ---
            hit_s = short_px is not None and px >= short_px
            hit_l = long_px is not None and px <= long_px
            if hit_s and hit_l:
                continue
            if hit_s:
                hung_s = short_px
                if lots and lots[0]["s"] == "L":
                    if allow_opp_cover and not block_exit:
                        close_one(t, px, "opp_cover", prefer_side="L")
                        if allow_flip and not lots:
                            open_lot(t, "S", px, "flip", reg_t, rv_t, honor_limit=True, hung_limit=hung_s)
                        short_px = short_born = None
                elif not lots:
                    if open_lot(t, "S", px, "enter", reg_t, rv_t, honor_limit=True, hung_limit=hung_s):
                        short_px = short_born = None
                elif lots[0]["s"] == "S":
                    if open_lot(t, "S", px, "scale_in", reg_t, rv_t, honor_limit=True, hung_limit=hung_s):
                        short_px = short_born = None
                    elif len(lots) >= p["max_lots"]:
                        log(t, "cancel", "S", short_px, "max_lots_rail")
                        last_short_rail = short_px
                        short_px = short_born = None
                else:
                    short_px = short_born = None
            if hit_l:
                hung_l = long_px
                if lots and lots[0]["s"] == "S":
                    if allow_opp_cover and not block_exit:
                        close_one(t, px, "opp_cover", prefer_side="S")
                        if allow_flip and not lots:
                            open_lot(t, "L", px, "flip", reg_t, rv_t, honor_limit=True, hung_limit=hung_l)
                        long_px = long_born = None
                elif not lots:
                    if open_lot(t, "L", px, "enter", reg_t, rv_t, honor_limit=True, hung_limit=hung_l):
                        long_px = long_born = None
                elif lots[0]["s"] == "L":
                    if open_lot(t, "L", px, "scale_in", reg_t, rv_t, honor_limit=True, hung_limit=hung_l):
                        long_px = long_born = None
                    elif len(lots) >= p["max_lots"]:
                        log(t, "cancel", "L", long_px, "max_lots_rail")
                        last_long_rail = long_px
                        long_px = long_born = None
                else:
                    long_px = long_born = None

    def vix_lag1_delta(day: str) -> float | None:
        prevs = [d for d in vix_delta if d < day]
        if not prevs:
            return None
        return vix_delta.get(prevs[-1])

    def birth_wants(t, reg, tilt, fade, lo_=None, hi_=None):
        """Absolute rails at PLACE time only (band = eligibility filter)."""
        lo_ = float(lo if lo_ is None else lo_)
        hi_ = float(hi if hi_ is None else hi_)
        want_s, want_l, sh, slv = structure_wants(
            H, L, C_anchor, t, look=int(p["struct_look"]), lo=lo_, hi=hi_, pad=float(p["struct_pad"])
        )
        if not sticky:
            if reg in ("expand_up", "climax_up", "div_hh_weak_vol"):
                want_s = min(want_s, max(C[t] + lo_, sh + p["struct_pad"]))
            if reg in ("expand_dn", "climax_dn"):
                want_l = max(want_l, min(C[t] - lo_, slv - p["struct_pad"]))
            if reg == "contract":
                want_s = min(C[t] + hi_, want_s + p["contract_both_extra"])
                want_l = max(C[t] - hi_, want_l - p["contract_both_extra"])
            if tilt <= -1 or fade == -1:
                want_s = max(C[t] + NOISE_FLOOR, want_s - p["thermo_closer"])
                want_l = max(C[t] - hi_, want_l - p["thermo_farther"])
            elif tilt >= 1 or fade == 1:
                want_l = min(C[t] - NOISE_FLOOR, want_l + p["thermo_closer"])
                want_s = min(C[t] + hi_, want_s + p["thermo_farther"])
            want_s = min(C[t] + hi_, max(C[t] + lo_, want_s))
            want_l = max(C[t] - hi_, min(C[t] - lo_, want_l))
            boost = p["cover_boost"]
            if lots and lots[0].get("regime") in ("climax_up", "climax_dn"):
                boost += p["climax_cover_extra"]
            if lots and lots[0]["s"] == "S":
                want_l = min(want_l, C[t] - min(p["cover_boost_cap"], max(lo_, C[t] - want_l) + boost * 0.5))
                want_l = max(C[t] - hi_, min(C[t] - lo_, want_l))
            if lots and lots[0]["s"] == "L":
                want_s = max(want_s, C[t] + min(p["cover_boost_cap"], max(lo_, want_s - C[t]) + boost * 0.5))
                want_s = min(C[t] + hi_, max(C[t] + lo_, want_s))
        else:
            want_s = min(C_anchor[t] + hi_, max(C_anchor[t] + lo_, want_s))
            want_l = max(C_anchor[t] - hi_, min(C_anchor[t] - lo_, want_l))
        if lots and len(lots) == 1:
            if lots[0]["s"] == "S":
                want_s = max(want_s, lots[0]["ep"] + p["scale_min_ext_pts"])
            else:
                want_l = min(want_l, lots[0]["ep"] - p["scale_min_ext_pts"])
        return want_s, want_l, sh, slv

    def trend_dampen_flags(t, reg, tilt) -> tuple[bool, bool]:
        """Flat-only hang dampen. Returns (strong_up, strong_dn). PIT ≤ t."""
        if lots:
            return False, False
        su = str(p.get("trend_hang_dampen") or "none")
        if su == "none":
            return False, False
        thr_mom = float(p.get("dampen_mom_pts") or 40.0)
        thr_night = float(p.get("dampen_night_open_pts") or 80.0)
        look = int(p.get("dampen_mom_look") or 15)
        strong_up = strong_dn = False
        if su in ("tilt", "combo"):
            if tilt >= int(p.get("dampen_tilt") or 1):
                strong_up = True
            if tilt <= -int(p.get("dampen_tilt") or 1):
                strong_dn = True
        if su in ("regime", "combo"):
            if reg in ("expand_up", "climax_up"):
                strong_up = True
            if reg in ("expand_dn", "climax_dn"):
                strong_dn = True
        if su in ("mom", "combo"):
            if t >= look:
                m = float(C[t]) - float(C[t - look])
                if m >= thr_mom:
                    strong_up = True
                if m <= -thr_mom:
                    strong_dn = True
        if su in ("night_open", "combo"):
            hm0 = _hhmm(T[t])
            if hm0 >= "15:00" or hm0 < "05:00":
                j0 = t
                while j0 > 0:
                    hm_p = _hhmm(T[j0 - 1])
                    hm_c = _hhmm(T[j0])
                    prev_n = hm_p >= "15:00" or hm_p < "05:00"
                    cur_n = hm_c >= "15:00" or hm_c < "05:00"
                    if cur_n and prev_n:
                        j0 -= 1
                    else:
                        break
                if _hhmm(T[j0]) >= "15:00" or _hhmm(T[j0]) < "05:00":
                    no = float(C[j0])
                    dlt = float(C[t]) - no
                    if dlt >= thr_night:
                        strong_up = True
                    if dlt <= -thr_night:
                        strong_dn = True
        return strong_up, strong_dn

    # H-TICK-NATIVE-FIX-4: resting orders are placed in advance and just
    # wait — same-bar fills are fine (jack is right). The real bug was
    # narrower: birth_wants/trend_dampen/gap-bias/sticky-cancel all anchor
    # their "current price" on C[t], bar t's own CLOSE, which isn't known
    # until the bar ends — yet the resulting rail was being checked against
    # ticks from EARLIER in that same bar. Anchoring those same decisions on
    # O[t] (bar t's open — known before any of bar t's ticks happen) instead
    # removes that leak while keeping same-bar fills fully legitimate, since
    # O[t] carries zero foreknowledge of how the bar unfolds.
    anchor_mode = p.get("hang_anchor")
    if anchor_mode is None:
        anchor_mode = "O" if p.get("tick_native") else "C"  # legacy default, byte-identical
    C_anchor = build_anchor_series(O, C, anchor_mode, n=int(p.get("hang_anchor_ema_n") or 15))

    for t in range(n):
        sl = slope_at(C, t)
        if sl is None:
            # Cache days often start at 08:45 (no prior bars) → slope needs ~10 bars.
            # Still allow open-greed lottery / fills in the open window.
            if not (p.get("open_greed") and open_greed_info(t)):
                continue
            sl = 0.0
        reg, _ = classify_pv(C, O, rvol, t)
        regime[t] = reg
        # REGIME-COMMITMENT FORK: greg is the value every downstream gating
        # decision this bar actually uses (block/hang/quiet/cell lookup,
        # amend cadence, opened-lot regime tag) — reg (raw) only feeds
        # regime[t] for reporting/safety-diff, never gating below this point.
        if reg == _commit_last_raw:
            _commit_run += 1
        else:
            _commit_last_raw = reg
            _commit_run = 1
        if _commit_current is None or _commit_run >= _commit_n:
            _commit_current = reg
        greg = _commit_current
        committed_regime[t] = greg
        rv = rvol[t] or 1.0

        if thermo_series is not None:
            thermo = thermo_series[t]
        tilt = thermo.get("tilt", 0)
        fade = thermo.get("fade_ext", 0)

        # --- VIXTWN lag1 session bias (+ optional research crash∧rally short) ---
        if (p["vix_session_bias"] or p.get("vix_crash_rally_short")) and lots and t > 0:
            btype = _is_boundary(T[t - 1], T[t])
            if btype:
                dlt = vix_lag1_delta(_day(T[t]))
                eps = float(p["vix_eps"])
                crash_rally_hit = False
                if p.get("vix_crash_rally_short"):
                    ret_map = p.get("prior_day_ret_pts") or {}
                    prior_ret = ret_map.get(_day(T[t]))
                    crash_th = float(p.get("vix_crash_max_delta", -2.0))
                    rally_th = float(p.get("vix_rally_min_pts", 80.0))
                    if (
                        dlt is not None
                        and prior_ret is not None
                        and dlt <= crash_th
                        and float(prior_ret) >= rally_th
                    ):
                        close_side(
                            t,
                            C_anchor[t],
                            f"vix_crash_rally_flat_L|dlt={dlt:+.2f}|ret={float(prior_ret):+.0f}",
                            "L",
                            True,
                        )
                        short_px = long_px = short_born = long_born = None
                        crash_rally_hit = True
                if crash_rally_hit:
                    pass  # joint signal handled; skip default VIX flatten this bar
                elif p["vix_session_bias"]:
                    if dlt is None:
                        while lots:
                            close_side(t, C_anchor[t], "session_flat_novix", lots[0]["s"], True)
                        short_px = long_px = short_born = long_born = None
                    elif dlt > eps:
                        close_side(t, C_anchor[t], f"vix_up_flat_L|{dlt:+.2f}", "L", True)
                        short_px = long_px = short_born = long_born = None
                    elif dlt < -eps:
                        close_side(t, C_anchor[t], f"vix_dn_flat_S|{dlt:+.2f}", "S", True)
                        short_px = long_px = short_born = long_born = None
                    else:
                        while lots:
                            close_side(t, C_anchor[t], f"vix_flat|{dlt:+.2f}", lots[0]["s"], True)
                        short_px = long_px = short_born = long_born = None

        max_hold = int(p.get("max_hold_bars") or 0)
        if lots and lots[0].get("max_hold") is not None:
            max_hold = int(lots[0]["max_hold"])
        # NQ confidence → exit accel/decel (hold + trail) when in position
        _arm_save = _give_save = None
        if lots and bool(p.get("nq_conf_exit_enable")):
            _pc, _nq_s, _tx_s = nq_side_conf(
                lots[0]["s"],
                nq_on[t],
                tx_from_open[t],
                eps_nq=float(p.get("nq_conf_eps_nq") or 0.05),
                eps_tx=float(p.get("nq_conf_eps_tx") or 20.0),
            )
            _xmode = str(p.get("nq_conf_exit_mode") or "symmetric")
            _apply = False
            _sign = 0  # -1 tighten/accel exit, +1 give room
            if _xmode == "symmetric" and _pc != 0:
                _apply = True
                _sign = _pc  # with → room; against → accel
            elif _xmode == "against_only" and _pc < 0:
                _apply = True
                _sign = -1  # accel exit only
            elif _xmode == "with_same_tighten" and _pc > 0:
                # with-NQ: if TX already with NQ (extended) → tighten;
                # if TX diverges → give room for reversion
                if _nq_s != 0 and _tx_s != 0:
                    _apply = True
                    _sign = -1 if _tx_s == _nq_s else +1
            if _apply and _sign != 0:
                _hk = float(p.get("nq_conf_hold_k") or 0.35)
                _tk = float(p.get("nq_conf_trail_k") or 0.25)
                if max_hold > 0:
                    max_hold = max(3, int(round(max_hold * (1.0 + _hk * _sign))))
                _arm_save = p.get("trail_arm_pts")
                _give_save = p.get("trail_giveback_pts")
                _arm0 = float(_arm_save if _arm_save is not None else 50.0)
                _give0 = float(_give_save if _give_save is not None else 40.0)
                p["trail_arm_pts"] = max(15.0, _arm0 * (1.0 + _tk * _sign))
                p["trail_giveback_pts"] = max(10.0, _give0 * (1.0 + _tk * _sign))
        if lots and max_hold > 0 and (t - int(lots[0]["eb"])) >= max_hold:
            while lots:
                close_side(t, C[t], f"max_hold|{max_hold}", lots[0]["s"], True)
            short_px = long_px = short_born = long_born = None

        # H-TICK-NATIVE: exits AND fills both resolved here, before PLACE,
        # in one real-tick pass (_tick_walk_bar) — see its docstring for why
        # this must run against PREVIOUS-bar-placed rails only. The second
        # call site below (after PLACE) is a no-op in tick_native mode.
        if p.get("tick_native") and tick_index is not None:
            _tick_walk_bar(t)
        else:
            # protective stop (close all lots on adverse move from first ep)
            stop_pts = float(p.get("stop_pts") or 0.0)
            min_hold_stop = int(p.get("min_hold_before_stop") or 0)
            if lots and stop_pts > 0 and (t - int(lots[0]["eb"])) >= min_hold_stop:
                side0 = lots[0]["s"]
                ep0 = float(lots[0]["ep"])
                # H-LOT-TOUCH: intrabar touch (H/L), not close — a real stop order
                # fires on the worst print of the bar, not on where it happened
                # to close (README lot 鐵律 #1: 曾+665→−517 fixing this exact bug).
                if p.get("touch_mode"):
                    adverse = (H[t] - ep0) if side0 == "S" else (ep0 - L[t])
                else:
                    adverse = (C[t] - ep0) if side0 == "S" else (ep0 - C[t])
                if adverse >= stop_pts:
                    xp = (ep0 + stop_pts if side0 == "S" else ep0 - stop_pts) if p.get("touch_mode") else C[t]
                    while lots:
                        close_side(t, xp, f"stop|{adverse:.0f}", lots[0]["s"], True)
                    short_px = long_px = short_born = long_born = None

            # --- smart exits (trail / structure); run after stop, before hang mgmt ---
            exit_mode = str(p.get("exit_mode") or "opp")
            if lots and exit_mode in ("trail", "structure", "hybrid_trail"):
                side0 = lots[0]["s"]
                ep0 = float(lots[0]["ep"])
                hold_b = t - int(lots[0]["eb"])
                min_smart = int(p.get("min_hold_before_smart") or 0)
                # update peak favorable from first entry price extremes
                if side0 == "L":
                    fav_now = float(H[t]) - ep0
                else:
                    fav_now = ep0 - float(L[t])
                if peak_fav is None:
                    peak_fav = fav_now
                else:
                    peak_fav = max(peak_fav, fav_now)

                if hold_b >= min_smart and exit_mode in ("trail", "hybrid_trail"):
                    arm = float(p.get("trail_arm_pts") or 50.0)
                    give = float(p.get("trail_giveback_pts") or 40.0)
                    if peak_fav >= arm and (peak_fav - fav_now) >= give:
                        while lots:
                            close_side(
                                t,
                                C[t],
                                f"trail|{peak_fav:.0f}->{fav_now:.0f}",
                                lots[0]["s"],
                                True,
                            )
                        short_px = long_px = short_born = long_born = None

                if lots and hold_b >= min_smart and exit_mode in ("structure", "hybrid_trail"):
                    _hm_s = _hhmm(T[t])
                    _day_s = not (_hm_s >= "15:00" or _hm_s < "05:00")
                    if p.get("struct_disabled") or (
                        p.get("day_struct_disabled") and _day_s
                    ):
                        pass  # research: leave trail/opp/stop only
                    else:
                        # After gap-improve entry: delay struct so gift isn't instantly leaked
                        grace = int(p.get("improv_struct_grace_bars") or 0)
                        gmin = float(p.get("improv_struct_min_pts") or 5.0)
                        improv0 = float(lots[0].get("improv") or 0.0)
                        skip_struct = bool(grace > 0 and improv0 >= gmin and hold_b < grace)
                        if (
                            p.get("improv_struct_until_trail")
                            and improv0 >= gmin
                            and peak_fav is not None
                            and peak_fav < float(p.get("trail_arm_pts") or 50.0)
                        ):
                            skip_struct = True
                        # adverse swing break: L exits if close < min(low of lookback excl. current)
                        look = int(p.get("struct_exit_look") or 12)
                        a0 = max(0, t - look)
                        if (not skip_struct) and t - a0 >= 3:
                            touch = bool(p.get("touch_mode"))
                            if side0 == "L":
                                swing = min(L[a0:t])  # prior bars only (PIT)
                                broke = L[t] < swing if touch else C[t] < swing
                                if broke:
                                    xp = min(swing, C[t]) if touch else C[t]
                                    while lots:
                                        close_side(
                                            t, xp, f"struct_break|{swing:.0f}", lots[0]["s"], True
                                        )
                                    short_px = long_px = short_born = long_born = None
                            else:
                                swing = max(H[a0:t])
                                broke = H[t] > swing if touch else C[t] > swing
                                if broke:
                                    xp = max(swing, C[t]) if touch else C[t]
                                    while lots:
                                        close_side(
                                            t, xp, f"struct_break|{swing:.0f}", lots[0]["s"], True
                                        )
                                    short_px = long_px = short_born = long_born = None

        # restore trail params if NQ conf temporarily scaled them
        if _arm_save is not None:
            p["trail_arm_pts"] = _arm_save
        if _give_save is not None:
            p["trail_giveback_pts"] = _give_save

        tip_s = short_px is not None and (short_px - C[t]) >= p["tip_keep"]
        tip_l = long_px is not None and (C[t] - long_px) >= p["tip_keep"]
        ap_s = short_px is not None and (short_px - H[t]) <= 20
        ap_l = long_px is not None and (L[t] - long_px) <= 20
        # stale / wrong-side vs prior close
        if t > 0 and short_px is not None and short_px <= C[t - 1]:
            log(t, "cancel", "S", short_px, f"stale_through|{greg}")
            last_short_rail = short_px
            short_px = short_born = None
            short_greed = False
        if t > 0 and long_px is not None and long_px >= C[t - 1]:
            log(t, "cancel", "L", long_px, f"stale_through|{greg}")
            last_long_rail = long_px
            long_px = long_born = None
            long_greed = False
        # sticky: rail walked beyond touchable horizon → cancel (PLACE new absolute later)
        eff_sticky_max = sticky_max
        if short_greed or long_greed:
            eff_sticky_max = max(sticky_max, float(p.get("open_greed_sticky_max") or 250.0))
        if sticky and short_px is not None and (short_px - C_anchor[t]) > eff_sticky_max:
            log(t, "cancel", "S", short_px, f"too_far|{short_px - C_anchor[t]:.0f}")
            last_short_rail = short_px
            short_px = short_born = None
            short_greed = False
        if sticky and long_px is not None and (C_anchor[t] - long_px) > eff_sticky_max:
            log(t, "cancel", "L", long_px, f"too_far|{C_anchor[t] - long_px:.0f}")
            last_long_rail = long_px
            long_px = long_born = None
            long_greed = False
        if not sticky:
            if short_px is not None and short_born is not None and (t - short_born) >= p["verify"]:
                if not tip_s and not ap_s:
                    log(t, "cancel", "S", short_px, f"verify|{greg}")
                    last_short_rail = short_px
                    short_px = short_born = None
                    short_greed = False
            if long_px is not None and long_born is not None and (t - long_born) >= p["verify"]:
                if not tip_l and not ap_l:
                    log(t, "cancel", "L", long_px, f"verify|{greg}")
                    last_long_rail = long_px
                    long_px = long_born = None
                    long_greed = False
        else:
            # sticky dual-hang: do NOT noise_close.
            # Cancelling when price approaches without touching drops the rail
            # one bar before a fill (e.g. 2026-08-04 15:33 cancel S@43524,
            # then 15:34 H=43544). Limit hangs wait for touch / too_far / stale.
            pass

        # Open-greed: expire unfilled lottery tickets at window end (flat only)
        greed_expired = False
        if (
            p.get("open_greed")
            and p.get("open_greed_cancel_unfilled", True)
            and not lots
        ):
            day = _day(T[t])
            bars_g = max(1, int(p.get("open_greed_bars") or 15))
            for oi in (day_open_i.get(day), night_open_i.get(day)):
                if oi is None or t != oi + bars_g:
                    continue
                if short_greed and short_px is not None:
                    log(t, "cancel", "S", short_px, "open_greed_expire")
                    last_short_rail = short_px
                    short_px = short_born = None
                    short_greed = False
                    greed_expired = True
                if long_greed and long_px is not None:
                    log(t, "cancel", "L", long_px, "open_greed_expire")
                    last_long_rail = long_px
                    long_px = long_born = None
                    long_greed = False
                    greed_expired = True

        # PLACE / (chase-only) AMEND
        do_place = False
        do_amend = False
        ginfo = open_greed_info(t)
        if sticky:
            if t - last_place_check >= int(p["place_every"]):
                last_place_check = t
                do_place = True
            if (
                ginfo
                and not lots
                and p.get("open_greed_force_place", True)
                and (short_px is None or long_px is None)
            ):
                do_place = True
            if greed_expired and not lots and (short_px is None or long_px is None):
                do_place = True
        else:
            amend_every = p["amend"] * (p["contract_amend_slow"] if greg == "contract" else 1)
            if t - last_amend >= amend_every:
                last_amend = t
                do_place = True
                do_amend = True

        if do_place or do_amend:
            hm = _hhmm(T[t])
            day = _day(T[t])
            is_night = hm >= "15:00" or hm < "05:00"
            mode = str(p.get("hang_band_mode") or "session_scale")
            if mode == "range_realtime":
                scope = str(p.get("hang_band_scope") or "rolling")
                a0 = session_open_index(T, t) if scope == "session" else None
                look = int(p.get("range_band_look") or 60)
                if scope == "session" and int(p.get("range_band_look") or 60) < 0:
                    look = 0  # full session so far
                use_lo, use_hi = range_hang_band(
                    H,
                    L,
                    t,
                    look=look,
                    k=float(p.get("range_band_k") or 0.35),
                    floor=float(p.get("range_band_floor") or 12.0),
                    cap=float(p.get("range_band_cap") or hi),
                    lo_frac=float(p.get("range_band_lo_frac") or 0.55),
                    measure=str(p.get("hang_band_measure") or "envelope"),
                    a_start=a0,
                )
            else:
                use_lo, use_hi = lo, hi
                if is_night:
                    nlo = p.get("night_hang_lo")
                    nhi = p.get("night_hang_hi")
                    if nlo is not None and nhi is not None:
                        use_lo = float(nlo)
                        use_hi = max(use_lo + 8.0, float(nhi))
                    else:
                        sc = float(p.get("night_hang_scale") or 1.0)
                        use_lo = max(8.0, lo * sc)
                        use_hi = max(use_lo + 8.0, hi * sc)
                else:
                    dlo = p.get("day_hang_lo")
                    dhi = p.get("day_hang_hi")
                    if dlo is not None and dhi is not None:
                        use_lo = float(dlo)
                        use_hi = max(use_lo + 8.0, float(dhi))
            # Per session×pv recipe book (16-regime research)
            _spb = p.get("session_pv_book") or {}
            _sess_b = "night" if is_night else "day"
            _cell = dict((_spb.get(_sess_b) or {}).get(greg) or {})
            if _cell.get("hang_lo") is not None and _cell.get("hang_hi") is not None:
                use_lo = float(_cell["hang_lo"])
                use_hi = max(use_lo + 8.0, float(_cell["hang_hi"]))
            want_s, want_l, sh, slv = birth_wants(t, greg, tilt, fade, use_lo, use_hi)
            place_s = place_l = True
            in_gap = bool(p.get("use_gap_bias")) and gap_start <= hm <= gap_end
            in_h1 = hm <= h1_until and hm >= "08:45"
            gap_bias = gap_map.get(day, "flat") if in_gap else "flat"
            note = (
                f"sh={sh:.0f}|tilt={tilt}|ta30={thermo.get('ta30m')}|"
                f"sm={thermo.get('short_temp')}|sticky={int(sticky)}|"
                f"gap={gap_bias if in_gap else '-'}|h1={int(in_h1)}|"
                f"band={use_lo:.0f}-{use_hi:.0f}"
            )

            # Open-greed lottery: absolute rails from session open (flat only)
            using_greed = False
            if ginfo and not lots:
                sess_g, oi_g = ginfo
                glo, ghi = greed_offsets(sess_g)
                ref = float(O[oi_g])
                want_s = ref + ghi
                want_l = ref - glo
                use_lo, use_hi = glo, ghi
                using_greed = True
                note = (
                    f"sh={sh:.0f}|tilt={tilt}|ta30={thermo.get('ta30m')}|"
                    f"sm={thermo.get('short_temp')}|sticky={int(sticky)}|"
                    f"gap={gap_bias if in_gap else '-'}|h1={int(in_h1)}|"
                    f"band={use_lo:.0f}-{use_hi:.0f}|open_greed|{sess_g}|ref={ref:.0f}"
                )

            # VIXTWN 1m hang calibration (symmetric; day only; after birth/greed)
            # Cell may override mode/gamma (session_pv_book); night cells typically none.
            _vmode = _cell.get("vixtwn_calib")
            if _vmode is None:
                _vmode = str(p.get("vixtwn_calib") or "none")
            _vgamma = _cell.get("vixtwn_calib_gamma")
            if _vgamma is None:
                _vgamma = float(p.get("vixtwn_calib_gamma") or 5.0)
            else:
                _vgamma = float(_vgamma)
            if is_night and _cell.get("vixtwn_calib") is None:
                # default: no FinMind VIXTWN on night unless cell explicitly sets it
                _vmode = "none"
            _adj_v = vixtwn_hang_adj(
                mode=str(_vmode),
                d1=vix_d1[t],
                d5=vix_d5[t],
                dopen=vix_dopen[t],
                gamma=_vgamma,
                cap=float(p.get("vixtwn_calib_cap") or 12.0),
                eps=float(p.get("vixtwn_calib_eps") or 0.3),
                ext_thr=float(p.get("vixtwn_calib_ext_thr") or 1.0),
            )
            _is_n = bool(hm >= "15:00" or hm < "05:00")
            _adj_uv = us_vix_hang_adj(
                mode=str(p.get("us_vix_calib") or "none"),
                d_on=us_vix_don[t],
                d_sess=us_vix_dsess[t],
                gamma=float(p.get("us_vix_calib_gamma") or 6.0),
                cap=float(p.get("us_vix_calib_cap") or 12.0),
                eps=float(p.get("us_vix_calib_eps") or 0.3),
                is_night=_is_n,
                nq_pct=nq_on[t],
                beta=float(p.get("us_vix_nq_beta") or -0.8),
                eps_nq=float(p.get("us_vix_nq_eps") or 0.05),
            )
            _adj_n = nq_hang_adj(
                mode=str(p.get("nq_calib") or "none"),
                nq_pct=nq_on[t],
                tx_from_open=tx_from_open[t],
                gamma=float(p.get("nq_calib_gamma") or 4.0),
                cap=float(p.get("nq_calib_cap") or 12.0),
                eps_nq=float(p.get("nq_calib_eps_nq") or 0.05),
                eps_tx=float(p.get("nq_calib_eps_tx") or 20.0),
                stretch_tx=float(p.get("nq_calib_stretch_tx") or 40.0),
                day_only=bool(p.get("nq_calib_day_only")),
                is_night=_is_n,
            )
            _adj = _adj_v + _adj_uv + _adj_n
            _jcap = float(p.get("calib_joint_cap") or 18.0)
            if _adj:
                _adj = max(-_jcap, min(_jcap, _adj))
            if _adj:
                _anch = float(C_anchor[t])
                want_s = want_s - _adj
                want_l = want_l - _adj
                want_s = min(_anch + use_hi, max(_anch + use_lo, want_s))
                want_l = max(_anch - use_hi, min(_anch - use_lo, want_l))
                note = note + f"|cal={_adj:+.1f}(v{_adj_v:+.1f}+u{_adj_uv:+.1f}+n{_adj_n:+.1f})"

            thermo_place_s = thermo_place_l = True
            if p.get("favored_side_hang") and not lots:
                effective_tilt = tilt
                effective_fade = fade
                if in_h1 and h1_scale <= 0:
                    effective_tilt = effective_fade = 0
                elif in_h1:
                    effective_tilt = int(round(tilt * h1_scale))
                    effective_fade = 0 if h1_scale < 0.5 else fade
                if effective_tilt <= -1 or effective_fade == -1:
                    thermo_place_l = False
                elif effective_tilt >= 1 or effective_fade == 1:
                    thermo_place_s = False

            if in_gap and gap_bias == "S":
                place_s, place_l = True, False
            elif in_gap and gap_bias == "L":
                place_s, place_l = False, True
            else:
                place_s, place_l = thermo_place_s, thermo_place_l

            # Causal strong-up / strong-down hang dampen (flat only; PIT)
            # Modes: none | tilt | regime | mom | night_open | combo
            strong_up, strong_dn = trend_dampen_flags(t, greg, tilt)
            if strong_up:
                place_s = False
                note = note + "|dampen_S"
            if strong_dn:
                place_l = False
                note = note + "|dampen_L"

            # skip quiet regimes when flat (dashboard: avoid contract/dry spam)
            # Research: skip_quiet_mode overrides — "dry" keeps profitable day contract
            # Cell recipe may override (session_pv_book[sess][pv].skip_quiet_mode).
            _sq = _cell.get("skip_quiet_mode")
            if _sq is None:
                _sq = p.get("skip_quiet_mode")
            if _sq is None:
                _sq = "both" if p.get("skip_quiet_regime") else "none"
            _sq = str(_sq)
            if _sq != "none" and not lots:
                _quiet = ("contract", "dry") if _sq == "both" else (("dry",) if _sq == "dry" else ())
                if greg in _quiet:
                    if not (using_greed and p.get("open_greed_bypass_quiet", True)):
                        place_s = place_l = False
                        note = note + f"|quiet_skip:{_sq}"
                        # Cancel resting rails — prior-regime hangs must not fill
                        # after we enter quiet (2026-08-06 live: dry fill from
                        # leftover S hang). Mirror pvBlock cancel path.
                        if short_px is not None:
                            log(t, "cancel", "S", short_px, f"quiet_skip:{_sq}")
                            last_short_rail = short_px
                            short_px = short_born = None
                        if long_px is not None:
                            log(t, "cancel", "L", long_px, f"quiet_skip:{_sq}")
                            last_long_rail = long_px
                            long_px = long_born = None

            # Research: session × pv × side place block (matrix toxic cells)
            # Also cancel resting rails — toxic fills often come from earlier hangs.
            _psb = p.get("pv_side_block") or {}
            if (_psb or _cell.get("block")) and not lots:
                _sess = "night" if (hm >= "15:00" or hm < "05:00") else "day"
                _sides = list((_psb.get(_sess) or {}).get(greg) or [])
                for s in list(_cell.get("block") or []):
                    if s not in _sides:
                        _sides.append(s)
                if "S" in _sides:
                    place_s = False
                    if short_px is not None:
                        log(t, "cancel", "S", short_px, f"pvBlock:{_sess}|{greg}")
                        last_short_rail = short_px
                        short_px = short_born = None
                    note = note + f"|pvBlock_S:{greg}"
                if "L" in _sides:
                    place_l = False
                    if long_px is not None:
                        log(t, "cancel", "L", long_px, f"pvBlock:{_sess}|{greg}")
                        last_long_rail = long_px
                        long_px = long_born = None
                    note = note + f"|pvBlock_L:{greg}"

            # Research: if IN position whose entry-regime is now toxic for this side,
            # optionally force faster exit via shortened max_hold (pv_pos_exit_k).
            # Applied in max_hold section via lot regime_e — see below.
            # Open-greed side mask (after dampen/quiet so L-only lottery stays L-only)
            if using_greed and not lots:
                _gs = str(p.get("open_greed_sides") or "both")
                if _gs == "L":
                    place_s = False
                    note = note + "|greed_L_only"
                elif _gs == "S":
                    place_l = False
                    note = note + "|greed_S_only"

            # PIT day direction (prior session): when flat, hang with-trend only
            if p.get("day_dir_filter") and not lots:
                ddir = day_dir_map.get(day, "flat")
                if ddir == "S":
                    place_s, place_l = True, False
                elif ddir == "L":
                    place_s, place_l = False, True
                note = note + f"|ddir={ddir}"

            # Research: NQ BIAS via session_side_gate — cell-layer switch.
            # When session_pv_book is set, cell.bias must be true to apply gate
            # (freeze: bias=True on all 16 cells). Global us_vix/nq_calib stay
            # outside the cell recipe.
            ssg = p.get("session_side_gate")
            _bias_on = True
            if p.get("session_pv_book"):
                _bias_on = bool(_cell.get("bias", False))
            if (
                ssg
                and _bias_on
                and not lots
                and not bool(p.get("nq_conf_soft_gate"))
            ):
                g = ssg.get(day, "none")
                if g == "L":
                    place_s, place_l = False, True
                    note = note + "|ssg_L"
                elif g == "S":
                    place_s, place_l = True, False
                    note = note + "|ssg_S"
                elif g == "none":
                    place_s = place_l = False
                    note = note + "|ssg_none"
                if _bias_on and p.get("session_pv_book"):
                    note = note + "|bias_cell"

            # Research: early_fill — tighten hang toward spot when fast+wet
            # Optional NQ conf: with → more γ; against → less γ; div bonus / same penalty
            eg = float(p.get("early_fill_gamma") or 0.0)
            if _cell.get("early_fill_gamma") is not None:
                eg = float(_cell["early_fill_gamma"])
            if eg > 0 and not lots and t >= 3:
                look = int(p.get("early_rvol_look") or 20)
                speed = 0.0
                for k in (1, 2, 3):
                    if t >= k:
                        speed = max(speed, abs(float(C[t]) - float(C[t - k])) / k)
                rvol_e = None
                if t >= look and V is not None:
                    base = float(sum(V[t - look : t]) / look)
                    if base > 0:
                        rvol_e = float(V[t - 1]) / base if t >= 1 else None
                if (
                    speed >= float(p.get("early_speed_min") or 6.0)
                    and rvol_e is not None
                    and rvol_e >= float(p.get("early_rvol_min") or 1.5)
                ):
                    spot = float(C_anchor[t])
                    eg_s = eg_l = eg
                    if bool(p.get("nq_conf_enable")):
                        _eps_n = float(p.get("nq_conf_eps_nq") or 0.05)
                        _eps_t = float(p.get("nq_conf_eps_tx") or 20.0)
                        _k = float(p.get("nq_conf_early_k") or 0.5)
                        _db = float(p.get("nq_conf_early_div_bonus") or 0.35)
                        _sp = float(p.get("nq_conf_early_same_penalty") or 0.25)
                        _min_eg = float(p.get("nq_conf_min_eg") or 0.0)
                        c_s, nq_s0, tx_s0 = nq_side_conf(
                            "S", nq_on[t], tx_from_open[t], eps_nq=_eps_n, eps_tx=_eps_t
                        )
                        c_l, _, _ = nq_side_conf(
                            "L", nq_on[t], tx_from_open[t], eps_nq=_eps_n, eps_tx=_eps_t
                        )
                        eg_s = max(
                            _min_eg,
                            eg
                            * nq_early_gamma_scale(
                                c_s, nq_s0, tx_s0, k=_k, div_bonus=_db, same_penalty=_sp
                            ),
                        )
                        eg_l = max(
                            _min_eg,
                            eg
                            * nq_early_gamma_scale(
                                c_l, nq_s0, tx_s0, k=_k, div_bonus=_db, same_penalty=_sp
                            ),
                        )
                        note = note + f"|earlyγS={eg_s:.1f}/L={eg_l:.1f}"
                    else:
                        note = note + f"|earlyγ={eg:.0f}"
                    want_s = max(spot + use_lo, float(want_s) - eg_s)
                    want_l = min(spot - use_lo, float(want_l) + eg_l)

            # intraday open bias (vs first day bar): follow session direction when flat
            ob = float(p.get("open_bias_pts") or 0.0)
            if ob > 0 and not lots and day in day_open:
                delta = float(C[t]) - float(day_open[day])
                if delta >= ob:
                    place_s, place_l = False, True
                    note = note + f"|obias=L:{delta:.0f}"
                elif delta <= -ob:
                    place_s, place_l = True, False
                    note = note + f"|obias=S:{delta:.0f}"

            # night: observe-only for new risk (no resting rails when flat)
            is_night = hm >= "15:00" or hm < "05:00"
            if (not p.get("night_entries", True)) and is_night and not lots:
                place_s = place_l = False
                if short_px is not None:
                    log(t, "cancel", "S", short_px, "night_observe")
                    last_short_rail = short_px
                    short_px = short_born = None
                if long_px is not None:
                    log(t, "cancel", "L", long_px, "night_observe")
                    last_long_rail = long_px
                    long_px = long_born = None
                note = note + "|night=observe"

            if lots:
                mode = str(p.get("in_pos_hang", "both"))
                em = str(p.get("exit_mode") or "opp")
                # trail/structure: no near opposite rail (exits are smart, not opp_cover)
                if em in ("trail", "structure"):
                    mode = "same_only"
                if mode == "same_only":
                    if lots[0]["s"] == "S":
                        place_s, place_l = True, False
                        if long_px is not None:
                            log(t, "cancel", "L", long_px, "same_only_in_S")
                            last_long_rail = long_px
                            long_px = long_born = None
                    else:
                        place_s, place_l = False, True
                        if short_px is not None:
                            log(t, "cancel", "S", short_px, "same_only_in_L")
                            last_short_rail = short_px
                            short_px = short_born = None
                else:
                    # both: cover opposite + scale same-side
                    place_s = True
                    place_l = True
                    # far opposite band for far_opp / hybrid_trail
                    if em in ("far_opp", "hybrid_trail"):
                        flo = float(p.get("far_cover_lo") or 80.0)
                        fhi = float(p.get("far_cover_hi") or 120.0)
                        if lots[0]["s"] == "L":
                            # opposite = short hang farther above
                            want_s = min(C_anchor[t] + fhi, max(C_anchor[t] + flo, want_s))
                            # Blind spot fix: flat-time near S must not survive as cover rail
                            if short_px is not None and (short_px - C_anchor[t]) < flo:
                                log(t, "cancel", "S", short_px, f"far_pull|{short_px - C_anchor[t]:.0f}<{flo:.0f}")
                                last_short_rail = short_px
                                short_px = short_born = None
                        else:
                            want_l = max(C_anchor[t] - fhi, min(C_anchor[t] - flo, want_l))
                            if long_px is not None and (C_anchor[t] - long_px) < flo:
                                log(t, "cancel", "L", long_px, f"far_pull|{C_anchor[t] - long_px:.0f}<{flo:.0f}")
                                last_long_rail = long_px
                                long_px = long_born = None

            # no new PLACE same bar as a fill (except far_pull rehang of opposite)
            allow_new = t != last_entry_bar
            far_rehang = False
            if lots:
                em2 = str(p.get("exit_mode") or "opp")
                if em2 in ("far_opp", "hybrid_trail"):
                    flo = float(p.get("far_cover_lo") or 80.0)
                    if lots[0]["s"] == "L" and short_px is None and place_s:
                        far_rehang = True
                    if lots[0]["s"] == "S" and long_px is None and place_l:
                        far_rehang = True

            if place_s:
                if short_px is None and (allow_new or far_rehang):
                    if last_short_rail is None or abs(want_s - last_short_rail) >= rehang_min or far_rehang:
                        short_px, short_born = want_s, t
                        last_short_rail = short_px
                        short_greed = bool(using_greed and not lots and not far_rehang)
                        log(t, "place", "S", short_px, note + ("|far_rehang" if far_rehang else ""))
                elif do_amend and short_px is not None and abs(want_s - short_px) >= 3 and short_px > H[t]:
                    short_px, short_born = want_s, t
                    short_greed = bool(using_greed and not lots)
                    log(t, "amend", "S", short_px, note)
            elif short_px is not None and (not lots or lots[0]["s"] != "L"):
                # sticky keeps rails unless strongly unfavored — dampen must still pull
                force = "|dampen_S" in note
                if force or (not sticky) or tilt >= 2:
                    log(t, "cancel", "S", short_px, ("dampen_pull|" if force else "unfavored|") + note)
                    last_short_rail = short_px
                    short_px = short_born = None
                    short_greed = False

            if place_l:
                if long_px is None and (allow_new or far_rehang):
                    if last_long_rail is None or abs(want_l - last_long_rail) >= rehang_min or far_rehang:
                        long_px, long_born = want_l, t
                        last_long_rail = long_px
                        long_greed = bool(using_greed and not lots and not far_rehang)
                        log(t, "place", "L", long_px, note + ("|far_rehang" if far_rehang else ""))
                elif do_amend and long_px is not None and abs(want_l - long_px) >= 3 and long_px < L[t]:
                    long_px, long_born = want_l, t
                    long_greed = bool(using_greed and not lots)
                    log(t, "amend", "L", long_px, note)
            elif long_px is not None and (not lots or lots[0]["s"] != "S"):
                force = "|dampen_L" in note
                if force or (not sticky) or tilt <= -2:
                    log(t, "cancel", "L", long_px, ("dampen_pull|" if force else "unfavored|") + note)
                    last_long_rail = long_px
                    long_px = long_born = None
                    long_greed = False

        # Every bar (even between place_every): pull dampened opposite rails while flat
        if not lots:
            su_f, sd_f = trend_dampen_flags(t, greg, tilt)
            if su_f and short_px is not None:
                log(t, "cancel", "S", short_px, "dampen_sanitize")
                last_short_rail = short_px
                short_px = short_born = None
                short_greed = False
            if sd_f and long_px is not None:
                log(t, "cancel", "L", long_px, "dampen_sanitize")
                last_long_rail = long_px
                long_px = long_born = None
                long_greed = False

        ws[t] = short_px
        wl[t] = long_px
        if short_px is None:
            short_greed = False
        if long_px is None:
            long_greed = False

        # Every bar: enforce far opposite distance (don't wait for place_every)
        em_san = str(p.get("exit_mode") or "opp")
        if lots and em_san in ("far_opp", "hybrid_trail"):
            flo = float(p.get("far_cover_lo") or 80.0)
            fhi = float(p.get("far_cover_hi") or 120.0)
            if lots[0]["s"] == "L":
                if short_px is not None and (short_px - C_anchor[t]) < flo:
                    log(t, "cancel", "S", short_px, f"far_sanitize|{short_px - C_anchor[t]:.0f}")
                    last_short_rail = short_px
                    short_px = min(C_anchor[t] + fhi, max(C_anchor[t] + flo, C_anchor[t] + flo))
                    short_born = t
                    last_short_rail = short_px
                    log(t, "place", "S", short_px, "far_sanitize")
            else:
                if long_px is not None and (C_anchor[t] - long_px) < flo:
                    log(t, "cancel", "L", long_px, f"far_sanitize|{C_anchor[t] - long_px:.0f}")
                    last_long_rail = long_px
                    long_px = max(C_anchor[t] - fhi, min(C_anchor[t] - flo, C_anchor[t] - flo))
                    long_born = t
                    last_long_rail = long_px
                    log(t, "place", "L", long_px, "far_sanitize")
            ws[t] = short_px
            wl[t] = long_px

        # H-TICK-NATIVE: already resolved above by _tick_walk_bar — a rail
        # placed just now (this bar's PLACE, above) intentionally is NOT
        # checked here; it becomes fillable starting next bar's walk.
        if p.get("tick_native") and tick_index is not None:
            pass
        else:
            # limit fill
            hit_s = False
            hit_l = False
            fill_s = fill_l = None
            gap_improve = bool(p.get("gap_fill_improve", True))
            if short_px is not None and short_px > O[t]:
                if H[t] >= short_px:
                    hit_s, fill_s = True, float(short_px)
            elif short_px is not None and O[t] >= short_px:
                hit_s, fill_s = True, float(O[t] if gap_improve else short_px)
            if long_px is not None and long_px < O[t]:
                if L[t] <= long_px:
                    hit_l, fill_l = True, float(long_px)
            elif long_px is not None and O[t] <= long_px:
                hit_l, fill_l = True, float(O[t] if gap_improve else long_px)
            if hit_s and hit_l:
                continue

            allow_flip = p["allow_flip"] and not (greg == "contract" and p["contract_no_flip"])
            # cross_bar_only: no cover/exit on entry bar
            block_exit = p.get("cross_bar_only") and t == last_entry_bar
            # Near opp_cover only in opp / far_opp / hybrid modes (trail/structure use smart exits)
            em = str(p.get("exit_mode") or "opp")
            allow_opp_cover = em in ("opp", "far_opp", "hybrid_trail")

            if hit_s:
                px = fill_s if fill_s is not None else short_px
                hung_s = short_px
                if lots and lots[0]["s"] == "L":
                    if allow_opp_cover and not block_exit:
                        close_one(t, px, "opp_cover", prefer_side="L")
                        if allow_flip and not lots:
                            open_lot(t, "S", px, "flip", greg, rv, honor_limit=True, hung_limit=hung_s)
                        short_px = short_born = None
                elif not lots:
                    if open_lot(
                        t, "S", px, "enter", greg, rv, honor_limit=True, hung_limit=hung_s
                    ):
                        short_px = short_born = None
                    # reject must NOT pull the resting limit (real exchange keeps it)
                elif lots[0]["s"] == "S":
                    if open_lot(
                        t, "S", px, "scale_in", greg, rv, honor_limit=True, hung_limit=hung_s
                    ):
                        short_px = short_born = None
                    elif len(lots) >= p["max_lots"]:
                        # cannot add — pull same-side rail (would never fill)
                        log(t, "cancel", "S", short_px, "max_lots_rail")
                        last_short_rail = short_px
                        short_px = short_born = None
                else:
                    short_px = short_born = None

            if hit_l:
                px = fill_l if fill_l is not None else long_px
                hung_l = long_px
                if lots and lots[0]["s"] == "S":
                    if allow_opp_cover and not block_exit:
                        close_one(t, px, "opp_cover", prefer_side="S")
                        if allow_flip and not lots:
                            open_lot(t, "L", px, "flip", greg, rv, honor_limit=True, hung_limit=hung_l)
                        long_px = long_born = None
                elif not lots:
                    if open_lot(
                        t, "L", px, "enter", greg, rv, honor_limit=True, hung_limit=hung_l
                    ):
                        long_px = long_born = None
                elif lots[0]["s"] == "L":
                    if open_lot(
                        t, "L", px, "scale_in", greg, rv, honor_limit=True, hung_limit=hung_l
                    ):
                        long_px = long_born = None
                    elif len(lots) >= p["max_lots"]:
                        log(t, "cancel", "L", long_px, "max_lots_rail")
                        last_long_rail = long_px
                        long_px = long_born = None
                else:
                    long_px = long_born = None

        ws[t] = short_px
        wl[t] = long_px

    if p.get("eod_flatten", True):
        while lots:
            close_one(n - 1, C[-1], "EOD", prefer_side=lots[0]["s"])
    open_pos = None
    if lots and C:
        side = lots[0]["s"]
        ep0 = float(lots[0]["ep"])
        spot = float(C[-1])
        open_pos = {
            "s": side,
            "ep": round(ep0, 1),
            "n": len(lots),
            "et": T[lots[0]["eb"]],
            "u_pnl": round((spot - ep0) if side == "L" else (ep0 - spot), 1),
            "spot": round(spot, 1),
        }
    return trades, events, ws, wl, rvol, regime, open_pos, committed_regime


def draw(O, H, L, C, V, T, trades, events, ws, wl, rvol, regime, path, title):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.patches import Patch

    for fp in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Light.ttc"):
        if Path(fp).exists():
            font_manager.fontManager.addfont(fp)
            plt.rcParams["font.family"] = font_manager.FontProperties(fname=fp).get_name()
            break
    plt.rcParams["axes.unicode_minus"] = False

    n = len(C)
    s = summarize(trades)
    xs = list(range(n))

    def series(a):
        return [math.nan if a[i] is None else a[i] for i in xs]

    fig, (ax, axv) = plt.subplots(
        2,
        1,
        figsize=(16, 8.5),
        dpi=140,
        gridspec_kw={"height_ratios": [3.0, 1.15], "hspace": 0.08},
        sharex=True,
    )
    fig.patch.set_facecolor("#0f1419")
    for a in (ax, axv):
        a.set_facecolor("#0f1419")

    colors = {
        "expand_up": "#3d2a2a",
        "expand_dn": "#2a3d2a",
        "climax_up": "#5a2020",
        "climax_dn": "#205a20",
        "contract": "#2a2a3d",
        "dry": "#1a1a1a",
        "div_hh_weak_vol": "#3d2a3d",
        "normal": None,
        "na": None,
    }
    i0 = 0
    while i0 < n:
        r0 = regime[i0]
        i1 = i0 + 1
        while i1 < n and regime[i1] == r0:
            i1 += 1
        col = colors.get(r0)
        if col:
            ax.axvspan(i0, i1, color=col, alpha=0.35, lw=0)
        i0 = i1

    for i in range(n):
        col = "#d64545" if C[i] >= O[i] else "#2ea043"
        ax.plot([i, i], [L[i], H[i]], color="#4b5563", lw=0.35)
        ax.plot([i, i], [min(O[i], C[i]), max(O[i], C[i])], color=col, lw=1.0)
    ax.step(xs, series(ws), where="post", color="#ffb020", lw=1.35, label="空限價")
    ax.step(xs, series(wl), where="post", color="#4aa3ff", lw=1.35, label="多限價")
    for e in events:
        if e["kind"] == "fill" and e["px"] is not None:
            ax.scatter(
                [e["t"]],
                [e["px"]],
                marker="v" if e["side"] == "S" else "^",
                s=36,
                c="#ff6b6b" if e["side"] == "S" else "#5dffa0",
                zorder=6,
            )
        if e["kind"] == "cover" and e["px"] is not None:
            ax.scatter([e["t"]], [e["px"]], marker="o", s=16, c="#ffe08a", zorder=6)
    ax.axhline(43700, color="#c084fc", lw=0.6, alpha=0.5)
    ax.set_title(
        f"{title}\n"
        f"{s['n']}筆 淨{s['net']:+.0f} 勝率{s['wr']}% · 結構掛單+VIX lag1+thermo · 觀測不下單",
        color="#e7ecf1",
        loc="left",
        fontsize=11,
    )
    ax.legend(
        handles=[
            Patch(facecolor="#3d2a2a", label="量增上"),
            Patch(facecolor="#5a2020", label="爆量上"),
            Patch(facecolor="#2a3d2a", label="量增下"),
            Patch(facecolor="#2a2a3d", label="量縮"),
            Patch(facecolor="#1a1a1a", label="乾量"),
        ],
        loc="upper left",
        facecolor="#1a222c",
        edgecolor="#2a3542",
        labelcolor="#e7ecf1",
        fontsize=8,
        ncol=3,
    )
    ax.tick_params(colors="#8b9aab", labelbottom=False)
    for sp in ax.spines.values():
        sp.set_color("#2a3542")

    for i in range(n):
        r = regime[i]
        if r in ("expand_up", "climax_up", "div_hh_weak_vol"):
            c = "#e05a5a"
        elif r in ("expand_dn", "climax_dn"):
            c = "#3ecf8e"
        elif r == "contract":
            c = "#6b7cff"
        elif r == "dry":
            c = "#555555"
        else:
            c = "#3d4a5c"
        axv.bar([i], [V[i]], color=c, width=1.0, align="center")
    axr = axv.twinx()
    axr.plot(xs, [math.nan if rvol[i] is None else rvol[i] for i in xs], color="#f0b35a", lw=0.9, alpha=0.9)
    axr.axhline(EXPAND, color="#e05a5a", ls="--", lw=0.6, alpha=0.7)
    axr.axhline(CONTRACT, color="#6b7cff", ls="--", lw=0.6, alpha=0.7)
    axr.set_ylabel("RVOL", color="#f0b35a", fontsize=8)
    axr.tick_params(colors="#f0b35a")
    axv.set_ylabel("量", color="#8b9aab", fontsize=9)
    axv.tick_params(colors="#8b9aab")
    for sp in axv.spines.values():
        sp.set_color("#2a3542")
    ticks, labs = [], []
    for i, t in enumerate(T):
        if str(t).endswith(":00") and (not ticks or i - ticks[-1] >= 40):
            ticks.append(i)
            labs.append(str(t)[5:])
    axv.set_xticks(ticks)
    axv.set_xticklabels(labs, color="#8b9aab", fontsize=7, rotation=30)
    ax.set_xlim(0, n - 1)
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)


def _oos(tx, days, params=None, vix_delta=None):
    nets, ns = [], []
    for d in days:
        b = tx[d]
        trd, *_ = simulate(
            [x["o"] for x in b],
            [x["h"] for x in b],
            [x["l"] for x in b],
            [x["c"] for x in b],
            [x["v"] for x in b],
            [f"{d} {x['t']}" for x in b],
            params,
            vix_delta=vix_delta,
        )
        s = summarize(trd)
        nets.append(s["net"])
        ns.append(s["n"])
    return dict(
        avg=round(st.mean(nets), 1),
        med=round(st.median(nets), 1),
        wr=round(100 * sum(1 for x in nets if x > 0) / len(nets)),
        n_avg=round(st.mean(ns), 1),
        net=round(sum(nets), 1),
    )


def main():
    bars = load_merged_tmf()
    O, H, L, C, V, T = arrays(bars)
    tx = json.load(open(DIR / "tx_1m_daynight_cache.json"))
    oos_days = sorted(d for d in tx if d <= "2026-06-25")
    vix = load_vixtwn_delta()
    gap_map = load_gap_bias_map()
    print(f"VIXTWN delta days={len(vix)} last={max(vix) if vix else None}")
    print(f"gap_bias map={gap_map}")
    print("NOTE: OOS night cache truncated 23:59 — blended OOS untrusted (P0)")

    def _off_near(ws, wl):
        offs = []
        for i in range(len(C)):
            if ws[i] is not None and ws[i] > C[i]:
                offs.append(ws[i] - C[i])
            if wl[i] is not None and wl[i] < C[i]:
                offs.append(C[i] - wl[i])
        med = round(st.median(offs), 1) if offs else None
        near = sum(1 for x in offs if x <= 40) / max(1, len(offs))
        return med, near

    print("=== sticky + gap + H1 thermo (process first) ===")
    rows = []
    for name, kw in [
        ("chase", dict(sticky=False, tip_keep=45.0, verify=4, use_gap_bias=False, thermo_h1_scale=1.0)),
        ("sticky", dict(sticky=True, use_gap_bias=False, thermo_h1_scale=1.0)),
        ("sticky+H1", dict(sticky=True, use_gap_bias=False, thermo_h1_scale=0.0)),
        ("sticky+gap+H1", dict(sticky=True, use_gap_bias=True, thermo_h1_scale=0.0)),  # defaults
        ("gap_to_10am", dict(sticky=True, use_gap_bias=True, gap_window_end="10:00", thermo_h1_scale=0.0)),  # negative A3
    ]:
        tr, ev, ws, wl, rvol, regime, _op, _cregime = simulate(O, H, L, C, V, T, kw, vix_delta=vix)
        s = summarize(tr)
        oos = _oos(tx, oos_days, kw, vix)
        med_off, near = _off_near(ws, wl)
        hm = hang_process_metrics(ws, wl, ev)
        # first-hour PnL share
        fh = [t for t in tr if "08:45" <= str(t["et"]).split()[-1][:5] <= "09:45"]
        fh_net = round(sum(t["pnl"] for t in fh), 1)
        print(
            f"{name:14s} IS n={s['n']:3d} net={s['net']:+6.0f} wr={s['wr']} "
            f"H1n={len(fh)} H1net={fh_net:+.0f} "
            f"horiz_med={hm['horiz_len_med']} amend={hm['amend']} "
            f"near40={near:.0%} OOS avg={oos['avg']:+.1f} med={oos['med']:+.1f} n={oos['n_avg']}"
        )
        rows.append(
            dict(
                name=name,
                kw={k: v for k, v in kw.items() if k != "gap_bias_by_day"},
                is_sum=s,
                oos=oos,
                med_off=med_off,
                near40=round(near, 3),
                hang=hm,
                h1_n=len(fh),
                h1_net=fh_net,
            )
        )

    # fill stress: no_same_bar already default; adverse gap toggle
    print("=== fill stress (IS sticky+gap+H1) ===")
    for name, kw in [
        ("optimistic", dict(sticky=True, use_gap_bias=True, thermo_h1_scale=0.0, cross_bar_only=True)),
        ("no_cross_bar", dict(sticky=True, use_gap_bias=True, thermo_h1_scale=0.0, cross_bar_only=False)),
    ]:
        tr, *_ = simulate(O, H, L, C, V, T, kw, vix_delta=vix)
        s = summarize(tr)
        print(f"{name:14s} IS n={s['n']} net={s['net']:+.0f} wr={s['wr']}")

    tr, ev, ws, wl, rvol, regime, _op, _cregime = simulate(O, H, L, C, V, T, None, vix_delta=vix)
    s = summarize(tr)
    oos = _oos(tx, oos_days, None, vix)
    med_off, near = _off_near(ws, wl)
    hm = hang_process_metrics(ws, wl, ev)
    print("defaults(sticky+gap+H1)", s, oos, f"med_off={med_off} near40={near:.0%}", hm)
    title = (
        f"v6 · sticky+gap08:45-09:15+H1弱thermo · horiz_med={hm['horiz_len_med']} amend={hm['amend']} "
        f"· IS n={s['n']} net={s['net']:+.0f} · 觀測不下單"
    )
    draw(O, H, L, C, V, T, tr, ev, ws, wl, rvol, regime, DIR / "jack_channel_v6_pv_0803_0804.png", title)
    draw(O, H, L, C, V, T, tr, ev, ws, wl, rvol, regime, DIR / "jack_channel_v6_sticky_0803_0804.png", title)

    payload = {
        "playbook": {
            "hang": "sticky absolute pivot rails; band[40,75] birth-only; too_far=150",
            "vix": "lag1 Δ, eps=0.2 (not same-day close)",
            "thermo": "side bias; weakened 08:45–09:45 (thermo_h1_scale=0)",
            "gap": "morning_gate JSON gap_bias only 08:45–09:15",
            "fill": "stale-through; gap@open; cross_bar_only; dual_hang; rehang_min_pts=0",
            "caveat": "OOS night cache incomplete; blended OOS untrusted until night backfill",
        },
        "gap_bias_map": gap_map,
        "is_sum": s,
        "oos": oos,
        "med_off": med_off,
        "near40": round(near, 3),
        "hang": hm,
        "how": dict(Counter(t.get("how") for t in tr)),
        "why": dict(Counter(t.get("why") for t in tr)),
        "variants": rows,
        "trades": tr,
    }
    (DIR / "jack_channel_v6_pv_analysis.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    )
    print("wrote", DIR / "jack_channel_v6_pv_0803_0804.png")
    for i, t in enumerate(tr[:25], 1):
        print(
            f"{i:02d} {t['s']} {t['et']}@{t['ep']}->{t['xt']}@{t['xp']} "
            f"{t['pnl']:+.1f} {t.get('how')} {t.get('why')} reg={t.get('regime_e')}"
        )
    if len(tr) > 25:
        print(f"... +{len(tr)-25} more")


if __name__ == "__main__":
    main()
