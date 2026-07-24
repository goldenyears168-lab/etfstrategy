"""Intraday direction thermometer（研究用 · 非 Order 規則）.

Layers
------
1. **Short (5m)**: temp ∈ [-2,+2] from completed 5m bars only (PIT).
2. **Open guard**: first ``open_guard_bars`` (default 6 ≈ 30m) → do not chase.
3. **Swing 1h**: ready after ``swing_ready_bars`` (default 12 ≈ 60m);
   direction bias from lookback ``swing_lookback_bars`` (default 8–12).
4. **Trend 3d**: daily TA + benchmark relative + 3d institutional chips.

Research only · observe / email-ready snapshot · not graduated.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping, Sequence

# Frozen defaults from 2026-07-23 Focus4 5m probe (Book research)
OPEN_GUARD_BARS = 6
SWING_READY_BARS = 12
SWING_LOOKBACK_BARS = 8
SWING_HORIZON_BARS = 12  # ~60m @ 5m
TREND_DAYS = 3

TEMP_LABELS = {
    -2: "偏冷／弱",
    -1: "偏弱",
    0: "中性震盪",
    1: "偏暖／強",
    2: "偏熱追價區",
}


@dataclass(frozen=True)
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class ThermoConfig:
    open_guard_bars: int = OPEN_GUARD_BARS
    swing_ready_bars: int = SWING_READY_BARS
    swing_lookback_bars: int = SWING_LOOKBACK_BARS
    swing_horizon_bars: int = SWING_HORIZON_BARS
    trend_days: int = TREND_DAYS


@dataclass
class LayerOut:
    temp: int | None
    label: str
    action: str
    reason: str
    ready: bool = True
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def _clamp_temp(x: int) -> int:
    return max(-2, min(2, int(x)))


def short_temp_from_bars(prev: Sequence[Bar]) -> LayerOut:
    """Short thermometer using ONLY completed bars in ``prev``."""
    if len(prev) < 1:
        return LayerOut(
            temp=0,
            label="開盤觀察",
            action="不重倉",
            reason="尚無足夠5分K",
            ready=False,
        )
    last = prev[-1]
    window = list(prev[-6:]) if len(prev) >= 6 else list(prev)
    closes = [b.close for b in window]
    vols = [b.volume for b in window]
    day_hi = max(b.high for b in prev)
    day_lo = min(b.low for b in prev)
    up = last.close > last.open
    vol_peak = max(vols) if vols else 1.0
    vol_ratio = (last.volume / vol_peak) if vol_peak else 0.0
    near_high = last.high >= day_hi * 0.998 or last.close >= day_hi * 0.995
    failed = near_high and last.close < last.high * 0.995 and (
        not up or last.close < (last.open + last.high) / 2
    )
    lower_high = higher_low = False
    if len(prev) >= 2:
        p2 = prev[-2]
        lower_high = last.high < p2.high and last.close < p2.close
        higher_low = last.low > p2.low and last.close > p2.close
    if len(closes) >= 3:
        slope = closes[-1] - closes[-3]
    else:
        slope = closes[-1] - closes[0]

    temp = 0
    reasons: list[str] = []
    if slope > 0 and up:
        temp += 1
        reasons.append("近段收盤上行")
    if slope < 0 and not up:
        temp -= 1
        reasons.append("近段收盤下行")
    if failed:
        temp -= 1
        reasons.append("逼近日高後收弱")
    if lower_high:
        temp -= 1
        reasons.append("低高點結構")
    if higher_low:
        temp += 1
        reasons.append("高低點抬升")
    if last.close <= day_lo * 1.002 and vol_ratio > 0.7:
        temp -= 1
        reasons.append("貼日低且量偏大")
    if last.close >= day_hi * 0.998 and vol_ratio > 0.7 and up:
        temp += 1
        reasons.append("放量近新高")
    temp = _clamp_temp(temp)
    if temp >= 2:
        action = "不追高；最多輕倉動能"
    elif temp == 1:
        action = "可顺勢做多但勿追尖"
    elif temp == 0:
        action = "觀望／區間邊做"
    elif temp == -1:
        action = "不接刀；等止跌"
    else:
        action = "偏空／不抄底"
    return LayerOut(
        temp=temp,
        label=TEMP_LABELS[temp],
        action=action,
        reason="；".join(reasons) or "結構不足，中性",
        ready=True,
        meta={
            "last_close": last.close,
            "day_hi": day_hi,
            "day_lo": day_lo,
            "bars_used": len(prev),
        },
    )


def apply_open_guard(short: LayerOut, bars_elapsed: int, cfg: ThermoConfig) -> LayerOut:
    """Overlay: first N bars after open → block chase longs."""
    active = bars_elapsed < cfg.open_guard_bars
    meta = {
        **short.meta,
        "open_guard_active": active,
        "open_guard_bars": cfg.open_guard_bars,
        "bars_elapsed": bars_elapsed,
    }
    if not active:
        return LayerOut(
            temp=short.temp,
            label=short.label,
            action=short.action,
            reason=short.reason,
            ready=short.ready,
            meta=meta,
        )
    # Soften bullish chase during guard window
    temp = short.temp if short.temp is not None else 0
    if temp >= 1:
        return LayerOut(
            temp=temp,
            label=f"{short.label}＋開盤守衛",
            action=f"開盤後{cfg.open_guard_bars}根內不追高（觀察）",
            reason=f"{short.reason}｜open_guard N={cfg.open_guard_bars}",
            ready=short.ready,
            meta=meta,
        )
    return LayerOut(
        temp=short.temp,
        label=short.label,
        action=short.action,
        reason=f"{short.reason}｜open_guard觀察中",
        ready=short.ready,
        meta=meta,
    )


def swing_1h_from_bars(prev: Sequence[Bar], cfg: ThermoConfig) -> LayerOut:
    """~1h direction bias; ready only after swing_ready_bars.

    Research note (2026-07-24): frozen momentum+short fusion has OOS directed
    hit ≈46–48% on liquid 5m universe — below coin-flip usefulness. Kept for
    observe snapshot continuity; see ``fade_near_ext_from_bars`` for the
    stronger ~30m mean-reversion candidate (still research-only).
    """
    n = len(prev)
    if n < cfg.swing_ready_bars:
        return LayerOut(
            temp=None,
            label="波段未就緒",
            action="短線溫度可看；1h層暫不決策",
            reason=f"需滿 {cfg.swing_ready_bars} 根5分K（約{cfg.swing_ready_bars * 5}分）",
            ready=False,
            meta={"bars": n, "need": cfg.swing_ready_bars},
        )
    L = min(cfg.swing_lookback_bars, n)
    w = list(prev[-L:])
    slope = w[-1].close - w[0].close
    short = short_temp_from_bars(prev)
    st = short.temp or 0
    score = (1 if slope > 0 else -1 if slope < 0 else 0) + (
        1 if st > 0 else -1 if st < 0 else 0
    )
    if score > 0:
        temp = 1 if score == 1 else 2
    elif score < 0:
        temp = -1 if score == -1 else -2
    else:
        temp = 0
    temp = _clamp_temp(temp)
    return LayerOut(
        temp=temp,
        label=f"1h偏置·{TEMP_LABELS[temp]}",
        action={
            2: "1h偏多但近熱：回檔再加，不追",
            1: "1h偏多：顺勢，控制追價",
            0: "1h中性：等結構",
            -1: "1h偏空：反彈減／不接刀",
            -2: "1h偏弱：避免抄底",
        }[temp],
        reason=f"lookback={L}根斜率{'↑' if slope > 0 else '↓' if slope < 0 else '→'}＋短溫{st}",
        ready=True,
        meta={
            "lookback_bars": L,
            "horizon_bars": cfg.swing_horizon_bars,
            "slope_pct": round(100.0 * slope / w[0].close, 3) if w[0].close else None,
            "short_temp": st,
        },
    )


# Research candidate (2026-07-24 IS-champion): ~30m fade at day extreme · midday
FADE_LOOKBACK_BARS = 4
FADE_READY_BARS = 8
FADE_HORIZON_BARS = 6  # ≈30m @ 5m — NOT the primary Swing 1h (~60m) metric
FADE_MIN_SLOPE_PCT = 0.3
FADE_NEAR_TOL = 0.001  # within 0.1% of session high/low so far
FADE_MIDDAY_START_HM = 10 * 60
FADE_MIDDAY_END_HM = 12 * 60 + 30


def fade_near_ext_from_bars(
    prev: Sequence[Bar],
    *,
    lookback: int = FADE_LOOKBACK_BARS,
    ready: int = FADE_READY_BARS,
    min_slope_pct: float = FADE_MIN_SLOPE_PCT,
    near_tol: float = FADE_NEAR_TOL,
    midday_only: bool = True,
    midday_start_hm: int = FADE_MIDDAY_START_HM,
    midday_end_hm: int = FADE_MIDDAY_END_HM,
) -> LayerOut:
    """Mean-reversion fade when last close is near session extreme (research).

    Signal (PIT): if close near day-high → temp=-1 (fade); near day-low → +1.
    Requires lookback slope magnitude ≥ ``min_slope_pct``. Optional midday
    clock filter. Horizon for evaluation is ``FADE_HORIZON_BARS`` (≈30m),
    distinct from Swing 1h's 12-bar / ~60m primary metric.
    """
    n = len(prev)
    if n < ready:
        return LayerOut(
            temp=None,
            label="fade未就緒",
            action="等滿就緒根數",
            reason=f"需滿 {ready} 根5分K",
            ready=False,
            meta={"bars": n, "need": ready},
        )
    last = prev[-1]
    hm = last.ts.hour * 60 + last.ts.minute
    if midday_only and not (midday_start_hm <= hm <= midday_end_hm):
        return LayerOut(
            temp=0,
            label="fade·非午盤窗",
            action="非10:00–12:30不發 fade 訊號",
            reason="midday_only",
            ready=True,
            meta={"hhmm": last.ts.strftime("%H:%M"), "midday_only": True},
        )
    L = min(lookback, n)
    w = list(prev[-L:])
    slope = w[-1].close - w[0].close
    slope_pct = (100.0 * slope / w[0].close) if w[0].close else 0.0
    if abs(slope_pct) < min_slope_pct:
        return LayerOut(
            temp=0,
            label="fade·斜率不足",
            action="觀望",
            reason=f"|slope|={slope_pct:.3f}% < {min_slope_pct}%",
            ready=True,
            meta={"slope_pct": round(slope_pct, 3)},
        )
    day_hi = max(b.high for b in prev)
    day_lo = min(b.low for b in prev)
    near_hi = last.close >= day_hi * (1.0 - near_tol)
    near_lo = last.close <= day_lo * (1.0 + near_tol)
    if near_hi and not near_lo:
        temp = -1
        reason = "貼近日前高→偏空淡化"
    elif near_lo and not near_hi:
        temp = 1
        reason = "貼近日前低→偏多反彈"
    else:
        return LayerOut(
            temp=0,
            label="fade·非極值",
            action="觀望",
            reason="收盤未單獨贴近日高或日低",
            ready=True,
            meta={
                "day_hi": day_hi,
                "day_lo": day_lo,
                "near_tol": near_tol,
                "slope_pct": round(slope_pct, 3),
            },
        )
    return LayerOut(
        temp=temp,
        label=f"fade30m·{TEMP_LABELS[temp]}",
        action={
            1: "近低淡化空：反彈偏多（研究）",
            -1: "近高淡化多：回落偏空（研究）",
        }[temp],
        reason=reason,
        ready=True,
        meta={
            "lookback_bars": L,
            "horizon_bars": FADE_HORIZON_BARS,
            "slope_pct": round(slope_pct, 3),
            "day_hi": day_hi,
            "day_lo": day_lo,
            "near_tol": near_tol,
            "midday_only": midday_only,
            "research_candidate": "fade_near_ext_30m",
        },
    )


# --- 5m EMA + rolling beta residual (research · 2026-07-24) ---------------
# PIT: EMA/beta use completed same-session bars only (no prior-day stitch).
EMA_FAST = 9
EMA_SLOW = 21
BETA_LOOKBACK_BARS = 26  # ~2h of 5m pairs
BETA_RET_BARS = 6  # residual horizon ≈30m
BETA_EPS = 0.05  # quiet floor on |beta|
RESID_EPS_PCT = 0.10  # quiet zone for residual % points
EMA_EXT_EPS_PCT = 0.10  # |close/ema-1|*100 quiet


def ema_last(closes: Sequence[float], span: int) -> float | None:
    """Last EMA(span) over ``closes`` (same-session · seed = first close)."""
    if span < 1 or len(closes) < span:
        return None
    alpha = 2.0 / (span + 1.0)
    ema = float(closes[0])
    for c in closes[1:]:
        ema = alpha * float(c) + (1.0 - alpha) * ema
    return ema


def ema_factors_from_bars(
    prev: Sequence[Bar],
    *,
    fast: int = EMA_FAST,
    slow: int = EMA_SLOW,
    ext_eps_pct: float = EMA_EXT_EPS_PCT,
) -> dict[str, Any]:
    """PIT 5m EMA factors from completed bars only."""
    out: dict[str, Any] = {
        "ready": False,
        "ema_fast": None,
        "ema_slow": None,
        "price_vs_ema_fast": 0,
        "price_vs_ema_slow": 0,
        "ema_cross": 0,
        "vs_ema_fast_pct": None,
        "vs_ema_slow_pct": None,
    }
    need = max(fast, slow)
    if len(prev) < need:
        return out
    closes = [b.close for b in prev]
    ef = ema_last(closes, fast)
    es = ema_last(closes, slow)
    if ef is None or es is None or ef <= 0 or es <= 0:
        return out
    last = closes[-1]
    vs_f = 100.0 * (last / ef - 1.0)
    vs_s = 100.0 * (last / es - 1.0)
    out.update(
        {
            "ready": True,
            "ema_fast": round(ef, 6),
            "ema_slow": round(es, 6),
            "vs_ema_fast_pct": round(vs_f, 4),
            "vs_ema_slow_pct": round(vs_s, 4),
            "price_vs_ema_fast": _sign_eps(vs_f, ext_eps_pct),
            "price_vs_ema_slow": _sign_eps(vs_s, ext_eps_pct),
            "ema_cross": _sign_eps(ef - es, ef * (ext_eps_pct / 100.0)),
        }
    )
    return out


def _bar_rets(bars: Sequence[Bar]) -> list[float]:
    out: list[float] = []
    for i in range(1, len(bars)):
        c0, c1 = bars[i - 1].close, bars[i].close
        if c0 and c0 > 0 and c1 is not None:
            out.append(c1 / c0 - 1.0)
    return out


def rolling_beta_residual(
    stock_prev: Sequence[Bar],
    bench_prev: Sequence[Bar],
    *,
    lookback: int = BETA_LOOKBACK_BARS,
    ret_bars: int = BETA_RET_BARS,
    beta_eps: float = BETA_EPS,
    resid_eps_pct: float = RESID_EPS_PCT,
) -> dict[str, Any]:
    """Rolling OLS beta (stock vs bench) + residual over last ``ret_bars``.

    Beta uses the last ``lookback`` paired 1-bar returns ending at last bar.
    Residual% = 100 * (r_s − beta * r_b) over the last ``ret_bars`` return.
    """
    out: dict[str, Any] = {
        "ready": False,
        "beta": None,
        "resid_pct": None,
        "resid_sign": 0,
        "bench_ret_pct": None,
        "stock_ret_pct": None,
    }
    need = max(lookback + 1, ret_bars + 1)
    if len(stock_prev) < need or len(bench_prev) < need:
        return out
    # Align by timestamp ≤ last stock bar (caller should pass aligned prefix).
    t_end = stock_prev[-1].ts
    b_al = [b for b in bench_prev if b.ts <= t_end]
    if len(b_al) < need:
        return out
    s_rets = _bar_rets(stock_prev[-(lookback + 1) :])
    b_rets = _bar_rets(b_al[-(lookback + 1) :])
    n = min(len(s_rets), len(b_rets))
    if n < max(10, lookback // 2):
        return out
    s_rets = s_rets[-n:]
    b_rets = b_rets[-n:]
    mean_b = sum(b_rets) / n
    mean_s = sum(s_rets) / n
    var_b = sum((x - mean_b) ** 2 for x in b_rets)
    if var_b <= 1e-18:
        return out
    cov = sum((s_rets[i] - mean_s) * (b_rets[i] - mean_b) for i in range(n))
    beta = cov / var_b
    # Horizon residual from last ret_bars closes.
    s0, s1 = stock_prev[-ret_bars - 1].close, stock_prev[-1].close
    b0, b1 = b_al[-ret_bars - 1].close, b_al[-1].close
    if not s0 or not b0:
        return out
    r_s = s1 / s0 - 1.0
    r_b = b1 / b0 - 1.0
    resid = 100.0 * (r_s - beta * r_b)
    out.update(
        {
            "ready": True,
            "beta": round(beta, 4),
            "resid_pct": round(resid, 4),
            "resid_sign": _sign_eps(resid, resid_eps_pct),
            "bench_ret_pct": round(100.0 * r_b, 4),
            "stock_ret_pct": round(100.0 * r_s, 4),
            "beta_abs_ge1": abs(beta) >= 1.0 - beta_eps,
            "beta_abs_lt1": abs(beta) < 1.0 - beta_eps,
        }
    )
    return out


# --- Short-horizon residual momentum (research · H3 · 1m bars) -------------
# Live short-mom uses 1–2 completed 1m bars; H3 tests residual vs raw on
# forward 5–15 minutes. Defaults are 1m-oriented (not fade30 / 5m).
SHORT_MOM_BARS = 2
SHORT_BETA_LOOKBACK = 30  # ~30m of 1-bar returns for β
SHORT_RESID_EPS_PCT = 0.02  # quiet zone (% points) on 1–2m residual
SHORT_MOM_EPS_PCT = 0.02


def short_residual_mom_from_bars(
    stock_prev: Sequence[Bar],
    bench_prev: Sequence[Bar],
    *,
    mom_bars: int = SHORT_MOM_BARS,
    beta_lookback: int = SHORT_BETA_LOOKBACK,
    resid_eps_pct: float = SHORT_RESID_EPS_PCT,
    mom_eps_pct: float = SHORT_MOM_EPS_PCT,
    beta_eps: float = BETA_EPS,
) -> dict[str, Any]:
    """PIT short-mom + β-adjusted residual on completed bars (typically 1m).

    Residual% = 100 * (r_s − β × r_b) over the last ``mom_bars`` closes.
    Also returns raw stock / bench short-mom signs for baselines.
    """
    out: dict[str, Any] = {
        "ready": False,
        "mom_bars": int(mom_bars),
        "raw_mom": 0,
        "mkt_mom": 0,
        "resid_mom": 0,
        "resid_beta1_mom": 0,
        "raw_pct": None,
        "mkt_pct": None,
        "resid_pct": None,
        "resid_beta1_pct": None,
        "beta": None,
    }
    mb = int(mom_bars)
    if mb < 1:
        return out
    need = max(beta_lookback + 1, mb + 1)
    if len(stock_prev) < need or len(bench_prev) < need:
        return out
    t_end = stock_prev[-1].ts
    b_al = [b for b in bench_prev if b.ts <= t_end]
    if len(b_al) < need:
        return out
    s0, s1 = stock_prev[-mb - 1].close, stock_prev[-1].close
    b0, b1 = b_al[-mb - 1].close, b_al[-1].close
    if not s0 or not b0:
        return out
    r_s = s1 / s0 - 1.0
    r_b = b1 / b0 - 1.0
    raw_pct = 100.0 * r_s
    mkt_pct = 100.0 * r_b
    beta_pack = rolling_beta_residual(
        stock_prev,
        b_al,
        lookback=beta_lookback,
        ret_bars=mb,
        beta_eps=beta_eps,
        resid_eps_pct=resid_eps_pct,
    )
    if not beta_pack.get("ready"):
        return out
    resid_pct = float(beta_pack["resid_pct"])
    resid_b1 = raw_pct - mkt_pct  # fixed β=1 residual
    out.update(
        {
            "ready": True,
            "raw_pct": round(raw_pct, 4),
            "mkt_pct": round(mkt_pct, 4),
            "resid_pct": round(resid_pct, 4),
            "resid_beta1_pct": round(resid_b1, 4),
            "beta": beta_pack.get("beta"),
            "raw_mom": _sign_eps(raw_pct, mom_eps_pct),
            "mkt_mom": _sign_eps(mkt_pct, mom_eps_pct),
            "resid_mom": _sign_eps(resid_pct, resid_eps_pct),
            "resid_beta1_mom": _sign_eps(resid_b1, resid_eps_pct),
            "beta_abs_ge1": beta_pack.get("beta_abs_ge1"),
            "beta_abs_lt1": beta_pack.get("beta_abs_lt1"),
        }
    )
    return out


# --- Multi-factor ~30m bias (research · 2026-07-24) -------------------------
# Horizon for evaluation: H=6 (~30m). Factors are PIT signed votes ∈ {-1,0,+1}.
TA30M_HORIZON_BARS = 6
TA30M_READY_BARS = 6  # first closed half-hour
TA30M_OR_BARS = 6  # 09:00–09:30 opening range
TA30M_MOM_BARS = 6
TA30M_SHORT_BARS = 3
TA30M_BIAS_EPS_PCT = 0.15  # align with Live quiet zone (% points)
TA30M_NEAR_EXT_TOL = 0.002  # 0.2% of session extreme


def _sign_eps(x: float, eps: float = 0.0) -> int:
    if x > eps:
        return 1
    if x < -eps:
        return -1
    return 0


def session_vwap(prev: Sequence[Bar]) -> float | None:
    """Session VWAP from completed bars only (typical price × volume)."""
    num = den = 0.0
    for b in prev:
        tp = (b.high + b.low + b.close) / 3.0
        v = float(b.volume or 0.0)
        if v <= 0:
            v = 1.0  # equal-weight fallback when volume missing
        num += tp * v
        den += v
    if den <= 0:
        return None
    return num / den


def opening_range_hl(
    prev: Sequence[Bar], *, n_bars: int = TA30M_OR_BARS
) -> tuple[float, float] | None:
    """09:00-ish opening-range high/low from the first ``n_bars`` session bars."""
    if len(prev) < n_bars:
        return None
    w = list(prev[:n_bars])
    return max(b.high for b in w), min(b.low for b in w)


def or_fail_break_temp(
    prev: Sequence[Bar],
    *,
    or_bars: int = TA30M_OR_BARS,
    reclaim_within: int = 2,
    weak_vol_max_rvol: float | None = None,
    midday_only: bool = False,
    midday_start_hm: int = FADE_MIDDAY_START_HM,
    midday_end_hm: int = FADE_MIDDAY_END_HM,
    require_beyond_or_mid: bool = False,
    require_vwap_side: bool = False,
) -> int:
    """Explicit failed OR break → fade toward OR mid / VWAP (research · PIT).

    Event (completed bars ``≤`` last only · **first reclaim only**):

    - Same-bar rejection: wick beyond ORH/ORL and close back inside; or
    - Multi-bar: a prior bar **closed** outside OR within ``reclaim_within``,
      intervening closes stayed outside, and **this** bar is the first close
      back inside.

    Temp = opposite of pierce (−1 after upside poke, +1 after downside).
    Optional weak poke volume (session-mean RVOL) and still-beyond-OR-mid /
    VWAP-side filters.
    """
    n = len(prev)
    if n <= or_bars:
        return 0
    or_hl = opening_range_hl(prev, n_bars=or_bars)
    if or_hl is None:
        return 0
    or_hi, or_lo = or_hl
    if or_hi <= or_lo:
        return 0
    last = prev[-1]
    hm = last.ts.hour * 60 + last.ts.minute
    if midday_only and not (midday_start_hm <= hm <= midday_end_hm):
        return 0
    if not (or_lo <= last.close <= or_hi):
        return 0

    direction: int | None = None
    pierce_i: int | None = None

    # Same-bar rejection (wick beyond + close inside).
    if last.high > or_hi and not (last.low < or_lo):
        direction, pierce_i = -1, n - 1
    elif last.low < or_lo and not (last.high > or_hi):
        direction, pierce_i = 1, n - 1
    else:
        # First close back inside after a closed excursion within N bars.
        for k in range(1, reclaim_within + 1):
            j = n - 1 - k
            if j < or_bars:
                break
            b = prev[j]
            if b.close > or_hi:
                if all(prev[t].close > or_hi for t in range(j + 1, n - 1)):
                    direction, pierce_i = -1, j
                break
            if b.close < or_lo:
                if all(prev[t].close < or_lo for t in range(j + 1, n - 1)):
                    direction, pierce_i = 1, j
                break
            if or_lo <= b.close <= or_hi:
                break

    if direction is None or pierce_i is None:
        return 0

    if weak_vol_max_rvol is not None:
        pierce_vol = float(prev[pierce_i].volume or 0.0)
        prior = [
            float(b.volume or 0.0)
            for b in prev[:pierce_i]
            if float(b.volume or 0.0) > 0.0
        ]
        if pierce_vol <= 0.0 or not prior:
            return 0
        mean_v = sum(prior) / len(prior)
        if mean_v <= 0.0 or (pierce_vol / mean_v) > weak_vol_max_rvol:
            return 0

    or_mid = 0.5 * (or_hi + or_lo)
    if require_beyond_or_mid:
        if direction < 0 and last.close < or_mid:
            return 0
        if direction > 0 and last.close > or_mid:
            return 0

    if require_vwap_side:
        vwap = session_vwap(prev)
        if vwap is None or vwap <= 0:
            return 0
        if direction < 0 and last.close < vwap:
            return 0
        if direction > 0 and last.close > vwap:
            return 0

    return direction


def ta30m_factors_from_bars(
    prev: Sequence[Bar],
    *,
    ready: int = TA30M_READY_BARS,
    mom_bars: int = TA30M_MOM_BARS,
    short_bars: int = TA30M_SHORT_BARS,
    bias_eps_pct: float = TA30M_BIAS_EPS_PCT,
    near_tol: float = TA30M_NEAR_EXT_TOL,
) -> dict[str, Any]:
    """PIT factor votes for ~30m directional bias research.

    Returns signed factors (``+1`` 偏多 / ``-1`` 偏空 / ``0`` 中性) plus
    continuous helpers. Does **not** claim predictive hit rate.
    """
    n = len(prev)
    out: dict[str, Any] = {
        "ready": False,
        "mom30": 0,
        "vwap": 0,
        "or_break": 0,
        "short_mom": 0,
        "fade_ext": 0,
        "fade_ext_anytod": 0,
        "vol_confirm": 0,  # +1 if last-bar vol ≥ median of session so far
        "ret30_pct": None,
        "vs_vwap_pct": None,
        "vs_or": None,  # "above" / "below" / "inside"
        "hhmm": None,
        "bars_elapsed": n,
        "hm": None,
    }
    if n < ready:
        return out
    last = prev[-1]
    hm = last.ts.hour * 60 + last.ts.minute
    out["ready"] = True
    out["hhmm"] = last.ts.strftime("%H:%M")
    out["hm"] = hm

    L = min(mom_bars, n)
    w = list(prev[-L:])
    c0, c1 = w[0].close, w[-1].close
    ret30 = (c1 / c0 - 1.0) if c0 else 0.0
    ret30_pct = 100.0 * ret30
    out["ret30_pct"] = round(ret30_pct, 4)
    out["mom30"] = _sign_eps(ret30_pct, bias_eps_pct)

    vwap = session_vwap(prev)
    if vwap and vwap > 0:
        vs_vwap_pct = 100.0 * (last.close / vwap - 1.0)
        out["vs_vwap_pct"] = round(vs_vwap_pct, 4)
        out["vwap"] = _sign_eps(vs_vwap_pct, bias_eps_pct)

    or_hl = opening_range_hl(prev)
    if or_hl is not None:
        or_hi, or_lo = or_hl
        if last.close > or_hi:
            out["or_break"] = 1
            out["vs_or"] = "above"
        elif last.close < or_lo:
            out["or_break"] = -1
            out["vs_or"] = "below"
        else:
            out["or_break"] = 0
            out["vs_or"] = "inside"

    Ls = min(short_bars, n)
    if Ls >= 2:
        s0, s1 = prev[-Ls].close, prev[-1].close
        short_pct = 100.0 * (s1 / s0 - 1.0) if s0 else 0.0
        out["short_mom"] = _sign_eps(short_pct, bias_eps_pct * 0.5)

    day_hi = max(b.high for b in prev)
    day_lo = min(b.low for b in prev)
    near_hi = last.close >= day_hi * (1.0 - near_tol)
    near_lo = last.close <= day_lo * (1.0 + near_tol)
    fade = 0
    if near_hi and not near_lo:
        fade = -1
    elif near_lo and not near_hi:
        fade = 1
    out["fade_ext_anytod"] = fade
    # Midday window matches fade_near_ext defaults.
    if FADE_MIDDAY_START_HM <= hm <= FADE_MIDDAY_END_HM:
        out["fade_ext"] = fade

    vols = [float(b.volume or 0.0) for b in prev]
    if vols and vols[-1] > 0:
        ordered = sorted(v for v in vols if v > 0)
        if ordered:
            med = ordered[len(ordered) // 2]
            out["vol_confirm"] = 1 if vols[-1] >= med else 0

    return out


def fuse_ta30m_bias(
    factors: Mapping[str, Any],
    *,
    mode: str,
    keys: Sequence[str],
    score_thresh: int = 2,
    require_vol: bool = False,
    midday_only: bool = False,
    after_or: bool = False,
    min_abs_ret30_pct: float = 0.0,
) -> int:
    """Fuse factor votes → temp ∈ {-1,0,+1}. Neutral when filters fail."""
    if not factors.get("ready"):
        return 0
    hm = factors.get("hm")
    if midday_only and isinstance(hm, int):
        if not (FADE_MIDDAY_START_HM <= hm <= FADE_MIDDAY_END_HM):
            return 0
    if after_or and int(factors.get("bars_elapsed") or 0) < TA30M_OR_BARS:
        return 0
    ret30 = factors.get("ret30_pct")
    if min_abs_ret30_pct > 0 and (
        ret30 is None or abs(float(ret30)) < min_abs_ret30_pct
    ):
        return 0
    if require_vol and int(factors.get("vol_confirm") or 0) < 1:
        return 0

    votes = [int(factors.get(k) or 0) for k in keys]
    if mode == "first":
        # Use the first key only (baseline).
        return votes[0] if votes else 0
    if mode == "and":
        nz = [v for v in votes if v != 0]
        if len(nz) < len(keys):
            return 0
        if all(v == nz[0] for v in nz):
            return nz[0]
        return 0
    if mode == "or_agree":
        # Fire if ≥1 non-zero and all non-zero agree (conflicts → 0).
        nz = [v for v in votes if v != 0]
        if not nz:
            return 0
        if all(v == nz[0] for v in nz):
            return nz[0]
        return 0
    if mode == "score":
        s = sum(votes)
        if s >= score_thresh:
            return 1
        if s <= -score_thresh:
            return -1
        return 0
    if mode == "live_confluence":
        # Mirror Live ``compute_ta_30m_snapshot`` bias (descriptive).
        mom = int(factors.get("mom30") or 0)
        vwap = int(factors.get("vwap") or 0)
        if mom > 0 and vwap >= 0:
            return 1
        if mom < 0 and vwap <= 0:
            return -1
        if mom == 0 and vwap != 0:
            return vwap
        return 0
    if mode == "fade_layer":
        # Delegate to full fade_near_ext (slope + midday) when keys unused.
        return int(factors.get("fade_ext") or 0)
    raise ValueError(f"unknown fuse mode: {mode}")


def _sma(xs: Sequence[float], n: int) -> float | None:
    if len(xs) < n:
        return None
    return sum(xs[-n:]) / n


def trend_3d_from_daily(
    *,
    stock_daily: Sequence[Mapping[str, Any]],
    bench_daily: Sequence[Mapping[str, Any]] | None,
    chip_daily: Sequence[Mapping[str, Any]] | None,
    cfg: ThermoConfig | None = None,
) -> LayerOut:
    """3-session trend: price TA + vs benchmark + institutional chips.

    Each daily row needs: trade_date/date, open, high, low, close, volume (optional).
    Chip rows: trade_date, foreign_net, investment_trust_net, three_institution_net.
    """
    cfg = cfg or ThermoConfig()
    if len(stock_daily) < cfg.trend_days:
        return LayerOut(
            temp=None,
            label="三日未就緒",
            action="日K不足",
            reason=f"需要至少 {cfg.trend_days} 根日K",
            ready=False,
        )

    def _close(r: Mapping[str, Any]) -> float:
        return float(r["close"])

    def _date(r: Mapping[str, Any]) -> str:
        return str(r.get("trade_date") or r.get("date"))

    s = list(stock_daily)
    closes = [_close(r) for r in s]
    last = s[-1]
    c0 = closes[-1]
    c1 = closes[-2] if len(closes) >= 2 else c0
    c3 = closes[-cfg.trend_days]
    r1 = c0 / c1 - 1.0 if c1 else 0.0
    r3 = c0 / c3 - 1.0 if c3 else 0.0
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    # higher-low / lower-high over last 3
    lows = [float(r["low"]) for r in s[-cfg.trend_days :]]
    highs = [float(r["high"]) for r in s[-cfg.trend_days :]]
    hl_up = len(lows) >= 2 and lows[-1] > min(lows[:-1])
    hh_down = len(highs) >= 2 and highs[-1] < max(highs[:-1])

    temp = 0
    reasons: list[str] = []
    if r3 > 0.02:
        temp += 1
        reasons.append(f"三日漲{r3*100:.1f}%")
    elif r3 < -0.02:
        temp -= 1
        reasons.append(f"三日跌{r3*100:.1f}%")
    if r1 > 0 and r3 > 0:
        temp += 1
        reasons.append("短＋三日同向多")
    if r1 < 0 and r3 < 0:
        temp -= 1
        reasons.append("短＋三日同向空")
    if ma5 is not None and c0 > ma5:
        temp += 1
        reasons.append("收盤>MA5")
    elif ma5 is not None and c0 < ma5:
        temp -= 1
        reasons.append("收盤<MA5")
    if ma20 is not None:
        if c0 > ma20:
            reasons.append("收盤>MA20")
        else:
            temp -= 1
            reasons.append("收盤<MA20")
    if hl_up:
        temp += 1
        reasons.append("近三日低點抬升")
    if hh_down and r3 < 0:
        temp -= 1
        reasons.append("近三日高點下移")

    # vs benchmark 3d relative
    rel = None
    if bench_daily and len(bench_daily) >= cfg.trend_days:
        b_closes = [_close(r) for r in bench_daily]
        b3 = b_closes[-1] / b_closes[-cfg.trend_days] - 1.0
        rel = r3 - b3
        if rel > 0.01:
            temp += 1
            reasons.append(f"相對大盤三日+{rel*100:.1f}pp")
        elif rel < -0.01:
            temp -= 1
            reasons.append(f"相對大盤三日{rel*100:.1f}pp")

    # chips aligned to last N stock sessions (PIT: dates ≤ last stock date)
    chip_score = 0
    chip_meta: dict[str, Any] = {}
    if chip_daily:
        asof = _date(last)
        want_dates = [_date(r) for r in s[-cfg.trend_days :] if _date(r) <= asof]
        by_d = {_date(r): r for r in chip_daily if _date(r) <= asof}
        chips = [by_d[d] for d in want_dates if d in by_d]
        if chips:
            three = sum(float(r.get("three_institution_net") or 0) for r in chips)
            foreign = sum(float(r.get("foreign_net") or 0) for r in chips)
            trust = sum(float(r.get("investment_trust_net") or 0) for r in chips)
            chip_meta = {
                "days": len(chips),
                "need": len(want_dates),
                "three_sum": three,
                "foreign_sum": foreign,
                "trust_sum": trust,
                "from": _date(chips[0]),
                "to": _date(chips[-1]),
                "coverage": f"{len(chips)}/{len(want_dates)}",
            }
            if len(chips) < len(want_dates):
                reasons.append(f"籌碼覆蓋{len(chips)}/{len(want_dates)}日")
            if three > 0:
                chip_score += 1
                reasons.append("三日三大法人淨買")
            elif three < 0:
                chip_score -= 1
                reasons.append("三日三大法人淨賣")
            if trust < 0 and three < 0:
                chip_score -= 1
                reasons.append("投信＋法人同向賣")
            elif trust > 0 and foreign > 0:
                chip_score += 1
                reasons.append("外資＋投信同向買")
            temp += chip_score
        else:
            chip_meta = {"days": 0, "need": len(want_dates), "coverage": f"0/{len(want_dates)}"}
            reasons.append("近三日籌碼缺漏")

    temp = _clamp_temp(temp)
    if temp >= 2:
        action = "三日偏多：回檔低接優先於追高"
    elif temp == 1:
        action = "三日偏暖：顺勢，留意短線過熱"
    elif temp == 0:
        action = "三日中性：看盤中溫度"
    elif temp == -1:
        action = "三日偏弱：反彈減碼／慎加碼"
    else:
        action = "三日偏空：不加碼；等止跌＋籌碼轉正"
    return LayerOut(
        temp=temp,
        label=f"三日·{TEMP_LABELS[temp]}",
        action=action,
        reason="；".join(reasons) or "中性",
        ready=True,
        meta={
            "asof": _date(last),
            "r1_pct": round(r1 * 100, 2),
            "r3_pct": round(r3 * 100, 2),
            "ma5": ma5,
            "ma20": ma20,
            "rel_vs_bench_3d_pp": round(rel * 100, 2) if rel is not None else None,
            "chip": chip_meta,
        },
    )


def combine_hint(
    *,
    guarded_short: LayerOut,
    swing: LayerOut,
    trend3: LayerOut,
) -> str:
    parts: list[str] = []
    if guarded_short.meta.get("open_guard_active"):
        parts.append("開盤守衛ON（不追高）")
    if guarded_short.temp is not None:
        parts.append(f"短線{guarded_short.temp:+d}")
    if swing.ready and swing.temp is not None:
        parts.append(f"1h{swing.temp:+d}")
    else:
        parts.append("1h未就緒")
    if trend3.ready and trend3.temp is not None:
        parts.append(f"三日{trend3.temp:+d}")
    # conflict flags
    st = guarded_short.temp
    td = trend3.temp if trend3.ready else None
    if st is not None and td is not None and st >= 1 and td <= -1:
        parts.append("衝突：短多／日弱→降權追價")
    if st is not None and td is not None and st <= -1 and td >= 1:
        parts.append("衝突：短弱／日多→等止跌再顺勢")
    return "｜".join(parts)


def build_snapshot(
    *,
    stock_id: str,
    bars_5m: Sequence[Bar],
    stock_daily: Sequence[Mapping[str, Any]],
    bench_daily: Sequence[Mapping[str, Any]] | None = None,
    chip_daily: Sequence[Mapping[str, Any]] | None = None,
    cfg: ThermoConfig | None = None,
    asof: datetime | None = None,
) -> dict[str, Any]:
    """Full thermometer snapshot at the end of ``bars_5m`` (all completed)."""
    cfg = cfg or ThermoConfig()
    prev = list(bars_5m)
    bars_elapsed = len(prev)
    short = short_temp_from_bars(prev)
    guarded = apply_open_guard(short, bars_elapsed, cfg)
    swing = swing_1h_from_bars(prev, cfg)
    trend3 = trend_3d_from_daily(
        stock_daily=stock_daily,
        bench_daily=bench_daily,
        chip_daily=chip_daily,
        cfg=cfg,
    )
    return {
        "stock_id": stock_id,
        "asof": (asof or (prev[-1].ts if prev else datetime.now())).isoformat(
            timespec="seconds"
        ),
        "config": {
            "open_guard_bars": cfg.open_guard_bars,
            "swing_ready_bars": cfg.swing_ready_bars,
            "swing_lookback_bars": cfg.swing_lookback_bars,
            "swing_horizon_bars": cfg.swing_horizon_bars,
            "trend_days": cfg.trend_days,
            "layer": "research",
            "status": "observe_only",
        },
        "intraday_short": guarded.to_dict(),
        "swing_1h": swing.to_dict(),
        "trend_3d": trend3.to_dict(),
        "combined_hint": combine_hint(
            guarded_short=guarded, swing=swing, trend3=trend3
        ),
    }


# --- DB helpers -------------------------------------------------------------

def load_stock_daily(
    conn: sqlite3.Connection, stock_id: str, *, limit: int = 60
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close, volume
        FROM stock_daily_bars
        WHERE stock_id = ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (stock_id, limit),
    ).fetchall()
    out = [
        {
            "trade_date": r[0],
            "open": r[1],
            "high": r[2],
            "low": r[3],
            "close": r[4],
            "volume": r[5],
        }
        for r in reversed(rows)
    ]
    return out


def load_bench_daily(
    conn: sqlite3.Connection, *, code: str = "IX0001", limit: int = 60
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT date, open, high, low, close, volume
        FROM daily_bars
        WHERE code = ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (code, limit),
    ).fetchall()
    return [
        {
            "trade_date": r[0],
            "open": r[1],
            "high": r[2],
            "low": r[3],
            "close": r[4],
            "volume": r[5],
        }
        for r in reversed(rows)
    ]


def load_chip_daily(
    conn: sqlite3.Connection, stock_id: str, *, limit: int = 30
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT trade_date, foreign_net, investment_trust_net,
               dealer_self_net, three_institution_net
        FROM stock_institutional_daily
        WHERE stock_id = ?
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (stock_id, limit),
    ).fetchall()
    return [
        {
            "trade_date": r[0],
            "foreign_net": r[1],
            "investment_trust_net": r[2],
            "dealer_self_net": r[3],
            "three_institution_net": r[4],
        }
        for r in reversed(rows)
    ]
