"""五維因子檢核：RS、籌碼連續、盈餘 proxy、回測、R:R（讀 DB · 不打 API）。"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass

PULLBACK_MA20_BAND_PCT = 5.0
from investment_policy import InvestmentPolicy, load_investment_policy
from market_labels import ENTRY_TAG_RETEST, PM_OBSERVE
from stock_context import TechnicalSnapshot, compute_technical, load_daily_bars, load_tej_daily_bars
from stock_db import load_latest_fundamental_map, load_latest_tech_risk

TW_SPOT_CODE = "IX0001"
RS_WINDOWS = (20, 60)
CHIP_STREAK_VERIFY = 3
RETEST_LOOKBACK = 20
RETEST_BREAKOUT_POS = 88.0


@dataclass(frozen=True)
class StockAnalytics:
    stock_id: str
    rs_20d: float | None = None
    rs_60d: float | None = None
    rs_percentile: float | None = None
    chip_streak_foreign: int = 0
    chip_streak_trust: int = 0
    chip_verify: str | None = None
    eps_qoq_pct: float | None = None
    eps_revision: str | None = None
    revenue_accel_pp: float | None = None
    retest: bool = False
    risk_reward: float | None = None
    ref_price: float | None = None

    def to_dict(self) -> dict:
        out = {k: v for k, v in asdict(self).items() if v is not None and v != 0}
        if self.chip_streak_foreign == 0 and "chip_streak_foreign" in out:
            del out["chip_streak_foreign"]
        if self.chip_streak_trust == 0 and "chip_streak_trust" in out:
            del out["chip_streak_trust"]
        if not self.retest:
            out.pop("retest", None)
        return out


def _aligned_closes(
    stock_rows: list[sqlite3.Row],
    index_rows: list[sqlite3.Row],
) -> list[tuple[str, float, float]]:
    idx_map = {str(r["trade_date"]): float(r["close"]) for r in index_rows}
    pairs: list[tuple[str, float, float]] = []
    for r in stock_rows:
        d = str(r["trade_date"])
        ic = idx_map.get(d)
        if ic is None:
            continue
        pairs.append((d, float(r["close"]), ic))
    return pairs


def _period_return_pct(closes: list[float], window: int) -> float | None:
    if len(closes) < window + 1:
        return None
    start = closes[-(window + 1)]
    end = closes[-1]
    if start <= 0:
        return None
    return (end / start - 1.0) * 100.0


def compute_relative_strength(
    conn: sqlite3.Connection,
    stock_id: str,
) -> tuple[float | None, float | None]:
    stock_rows = list(reversed(load_daily_bars(conn, stock_id, limit=280)))
    index_rows = list(reversed(load_tej_daily_bars(conn, TW_SPOT_CODE, limit=280)))
    pairs = _aligned_closes(stock_rows, index_rows)
    if len(pairs) < RS_WINDOWS[1] + 2:
        return None, None
    stock_closes = [p[1] for p in pairs]
    index_closes = [p[2] for p in pairs]
    rs: dict[int, float | None] = {}
    for w in RS_WINDOWS:
        sr = _period_return_pct(stock_closes, w)
        ir = _period_return_pct(index_closes, w)
        if sr is not None and ir is not None:
            rs[w] = round(sr - ir, 2)
        else:
            rs[w] = None
    return rs.get(20), rs.get(60)


def _streak_positive(rows: list[sqlite3.Row], field: str) -> int:
    streak = 0
    for row in reversed(rows):
        val = row[field]
        if val is None:
            break
        if float(val) > 0:
            streak += 1
        else:
            break
    return streak


def compute_chip_verify(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    lookback: int = 10,
) -> tuple[int, int, str | None]:
    try:
        rows = conn.execute(
            """
            SELECT trade_date, foreign_net, investment_trust_net
            FROM stock_institutional_daily
            WHERE stock_id = ? AND source = 'finmind'
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            (stock_id, lookback),
        ).fetchall()
    except sqlite3.OperationalError:
        return 0, 0, None
    if not rows:
        return 0, 0, None
    f_streak = _streak_positive(rows, "foreign_net")
    t_streak = _streak_positive(rows, "investment_trust_net")
    label: str | None = None
    if f_streak >= CHIP_STREAK_VERIFY and t_streak >= CHIP_STREAK_VERIFY:
        label = f"外資投信連{f_streak}日買超"
    elif f_streak >= CHIP_STREAK_VERIFY:
        label = f"外資連{f_streak}日買超"
    elif t_streak >= CHIP_STREAK_VERIFY:
        label = f"投信連{t_streak}日買超"
    elif f_streak >= 2 and t_streak >= 2:
        label = f"雙法人{f_streak}日買超"
    return f_streak, t_streak, label


def _position_52w_at(series: list[sqlite3.Row], end_idx: int) -> float | None:
    start = max(0, end_idx - 251)
    window = series[start : end_idx + 1]
    if not window:
        return None
    close = float(window[-1]["close"])
    highs = [float(r["high"] or r["close"]) for r in window]
    lows = [float(r["low"] or r["close"]) for r in window]
    hi, lo = max(highs), min(lows)
    if hi <= lo:
        return None
    return (close - lo) / (hi - lo) * 100.0


def detect_breakout_retest(
    conn: sqlite3.Connection,
    stock_id: str,
    tech: TechnicalSnapshot | None,
) -> bool:
    if tech is None or tech.dist_ma20_pct is None:
        return False
    if abs(tech.dist_ma20_pct) > PULLBACK_MA20_BAND_PCT + 1.0:
        return False
    rows = list(reversed(load_daily_bars(conn, stock_id, limit=280)))
    if len(rows) < RETEST_LOOKBACK + 5:
        return False
    had_breakout = False
    end = len(rows) - 1
    for i in range(end - RETEST_LOOKBACK, end):
        pos = _position_52w_at(rows, i)
        if pos is not None and pos >= RETEST_BREAKOUT_POS:
            had_breakout = True
            break
    if not had_breakout:
        return False
    if tech.position_52w_pct is not None and tech.position_52w_pct < 40.0:
        return False
    return True


def compute_eps_revision_proxy(
    conn: sqlite3.Connection,
    stock_id: str,
) -> tuple[float | None, str | None, float | None]:
    revenue_accel: float | None = None
    fund_map = load_latest_fundamental_map(conn)
    frow = fund_map.get(stock_id)
    if frow is not None and frow["revenue_mom_accel_pp"] is not None:
        revenue_accel = float(frow["revenue_mom_accel_pp"])

    try:
        eps_rows = conn.execute(
            """
            SELECT period_date, value
            FROM stock_financial_history
            WHERE stock_id = ? AND metric = 'eps' AND period_type = 'quarter'
            ORDER BY period_date DESC
            LIMIT 3
            """,
            (stock_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        eps_rows = []

    eps_qoq: float | None = None
    revision: str | None = None
    if len(eps_rows) >= 2:
        latest = float(eps_rows[0]["value"])
        prior = float(eps_rows[1]["value"])
        denom = max(abs(prior), 0.01)
        eps_qoq = round((latest - prior) / denom * 100.0, 1)
        if eps_qoq >= 8.0:
            revision = "上修"
        elif eps_qoq <= -8.0:
            revision = "下修"
        else:
            revision = "持平"
    elif revenue_accel is not None:
        if revenue_accel >= 3.0:
            revision = "營收加速"
        elif revenue_accel <= -3.0:
            revision = "營收減速"

    return eps_qoq, revision, revenue_accel


def compute_risk_reward(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    entry_signal: str,
    tech: TechnicalSnapshot | None,
    investment_score: float | None = None,
    ips: InvestmentPolicy | None = None,
) -> tuple[float | None, float | None]:
    if tech is None:
        tech = compute_technical(conn, stock_id)
    if tech is None or tech.close is None:
        return None, None
    ref = float(tech.close)
    stop = tech.ma20 or tech.ma60
    if stop is None or ref <= stop:
        return ref, None
    rr = round((ref - stop) / max(ref - stop, 1e-6), 2)
    return ref, rr


def build_stock_analytics(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    tech: TechnicalSnapshot | None = None,
    entry_signal: str | None = None,
    investment_score: float | None = None,
    ips: InvestmentPolicy | None = None,
) -> StockAnalytics:
    tech = tech or compute_technical(conn, stock_id)
    rs_20, rs_60 = compute_relative_strength(conn, stock_id)
    f_streak, t_streak, chip_verify = compute_chip_verify(conn, stock_id)
    eps_qoq, eps_rev, rev_accel = compute_eps_revision_proxy(conn, stock_id)
    retest = detect_breakout_retest(conn, stock_id, tech)
    ref_price, rr = None, None
    if entry_signal:
        ref_price, rr = compute_risk_reward(
            conn,
            stock_id,
            entry_signal=entry_signal,
            tech=tech,
            investment_score=investment_score,
            ips=ips,
        )
    return StockAnalytics(
        stock_id=stock_id,
        rs_20d=rs_20,
        rs_60d=rs_60,
        chip_streak_foreign=f_streak,
        chip_streak_trust=t_streak,
        chip_verify=chip_verify,
        eps_qoq_pct=eps_qoq,
        eps_revision=eps_rev,
        revenue_accel_pp=rev_accel,
        retest=retest,
        risk_reward=rr,
        ref_price=ref_price,
    )


def analytics_entry_tags(analytics: StockAnalytics) -> list[str]:
    tags: list[str] = []
    if analytics.retest:
        tags.append(ENTRY_TAG_RETEST)
    return tags


def _percentile_rank(value: float, pool: list[float]) -> float:
    if not pool:
        return 50.0
    below = sum(1 for v in pool if v < value)
    equal = sum(1 for v in pool if v == value)
    return round((below + 0.5 * equal) / len(pool) * 100.0, 1)


def compute_rs_percentile_map(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    window: int = 60,
) -> dict[str, float]:
    """Universe 橫截面 RS 分位（以 rs_60d 為準）。"""
    rs_by_id: dict[str, float] = {}
    for sid in stock_ids:
        _, rs_60 = compute_relative_strength(conn, sid)
        if rs_60 is not None:
            rs_by_id[sid] = rs_60
    if not rs_by_id:
        return {}
    pool = list(rs_by_id.values())
    return {sid: _percentile_rank(val, pool) for sid, val in rs_by_id.items()}


def build_analytics_map(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    entry_by_id: dict[str, str] | None = None,
    score_by_id: dict[str, float] | None = None,
) -> dict[str, StockAnalytics]:
    entry_by_id = entry_by_id or {}
    score_by_id = score_by_id or {}
    ips = load_investment_policy()
    rs_pct = compute_rs_percentile_map(conn, stock_ids)
    out: dict[str, StockAnalytics] = {}
    for sid in stock_ids:
        base = build_stock_analytics(
            conn,
            sid,
            entry_signal=entry_by_id.get(sid),
            investment_score=score_by_id.get(sid),
            ips=ips,
        )
        pct = rs_pct.get(sid)
        out[sid] = StockAnalytics(
            stock_id=base.stock_id,
            rs_20d=base.rs_20d,
            rs_60d=base.rs_60d,
            rs_percentile=pct,
            chip_streak_foreign=base.chip_streak_foreign,
            chip_streak_trust=base.chip_streak_trust,
            chip_verify=base.chip_verify,
            eps_qoq_pct=base.eps_qoq_pct,
            eps_revision=base.eps_revision,
            revenue_accel_pp=base.revenue_accel_pp,
            retest=base.retest,
            risk_reward=base.risk_reward,
            ref_price=base.ref_price,
        )
    return out
