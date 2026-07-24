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
