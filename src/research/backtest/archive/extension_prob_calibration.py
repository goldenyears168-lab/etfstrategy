"""Extension radar · 機率校準（Phase 2 · 🟢🟡🔴 歷史 P）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from intraday_extension_radar import build_bar_snapshots
from stock_db.kbar import load_kbar_day_bars

ExtensionZone = Literal["green", "yellow", "red"]


@dataclass
class ZoneOutcome:
    zone: ExtensionZone
    n: int = 0
    fade_30m_pct: float = 0.0
    fade_eod_pct: float = 0.0
    close_below_vwap_pct: float = 0.0
    continue_30m_pct: float = 0.0


def _prior_close(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    full_dates: list[str],
) -> float | None:
    prior = [d for d in full_dates if d < trade_date]
    if not prior:
        return None
    row = conn.execute(
        "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=?",
        (stock_id, prior[-1]),
    ).fetchone()
    return float(row[0]) if row else None


def _ma20(
    conn: sqlite3.Connection,
    stock_id: str,
    trade_date: str,
    full_dates: list[str],
) -> float | None:
    dates = [d for d in full_dates if d <= trade_date][-20:]
    if len(dates) < 10:
        return None
    rows = conn.execute(
        f"""
        SELECT close FROM stock_daily_bars
        WHERE stock_id=? AND trade_date IN ({",".join("?" * len(dates))})
        ORDER BY trade_date
        """,
        (stock_id, *dates),
    ).fetchall()
    if len(rows) < 10:
        return None
    return sum(float(r[0]) for r in rows) / len(rows)


def _poll_minutes(snaps: tuple, interval: int) -> set[str]:
    out: set[str] = set()
    for s in snaps:
        parts = s.minute.split(":")
        hh, mm = int(parts[0]), int(parts[1])
        total = (hh * 60 + mm) // interval * interval
        out.add(f"{total // 60:02d}:{total % 60:02d}:00")
    return out


def calibrate_extension_probs(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    poll_interval_min: int = 5,
    min_bars: int = 50,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    full_dates = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT trade_date FROM stock_daily_bars ORDER BY trade_date"
        ).fetchall()
    ]
    sessions = conn.execute(
        """
        SELECT DISTINCT stock_id, trade_date FROM stock_kbar_1m
        WHERE trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date, stock_id
        """,
        (date_start, date_end),
    ).fetchall()

    buckets: dict[str, dict[str, list[float]]] = {
        "green": {"fade_30m": [], "fade_eod": [], "below_vwap": [], "continue_30m": []},
        "yellow": {"fade_30m": [], "fade_eod": [], "below_vwap": [], "continue_30m": []},
        "red": {"fade_30m": [], "fade_eod": [], "below_vwap": [], "continue_30m": []},
    }
    spike_buckets: dict[str, dict[str, list[float]]] = {
        z: {k: [] for k in ("fade_30m", "fade_eod", "below_vwap", "continue_30m")}
        for z in ("green", "yellow", "red")
    }
    n_sessions = 0
    n_obs = 0

    for row in sessions:
        sid, td = str(row["stock_id"]), str(row["trade_date"])
        prev = _prior_close(conn, sid, td, full_dates)
        if prev is None:
            continue
        bars = load_kbar_day_bars(conn, sid, td)
        if len(bars) < min_bars:
            continue
        ma20 = _ma20(conn, sid, td, full_dates)
        snaps = build_bar_snapshots(bars, prev_close=prev, ma20=ma20)
        if len(snaps) < 30:
            continue
        n_sessions += 1
        poll_set = _poll_minutes(snaps, poll_interval_min)
        last_close = snaps[-1].close
        last_vwap = snaps[-1].vwap
        for i, s in enumerate(snaps):
            if s.minute not in poll_set:
                continue
            n_obs += 1
            future = snaps[i + 1 : i + 31]
            if not future:
                continue
            min_fwd = min(x.close for x in future)
            max_fwd = max(x.close for x in future)
            fade_30m = (min_fwd / s.close - 1.0) * 100.0
            fade_eod = (last_close / s.close - 1.0) * 100.0
            below_vwap = 1.0 if last_close < last_vwap else 0.0
            cont_30m = 1.0 if max_fwd > s.close * 1.01 else 0.0
            zone = s.zone
            for key, val in (
                ("fade_30m", fade_30m),
                ("fade_eod", fade_eod),
                ("below_vwap", below_vwap),
                ("continue_30m", cont_30m),
            ):
                buckets[zone][key].append(val)
                if s.ext_prev_pct >= 5.0:
                    spike_buckets[zone][key].append(val)

    def _summarize(data: dict[str, list[float]]) -> dict[str, float | int]:
        n = len(data["fade_30m"])
        if n == 0:
            return {"n": 0}
        return {
            "n": n,
            "P_fade_30m_ge2": round(
                sum(1 for x in data["fade_30m"] if x <= -2.0) / n * 100, 1
            ),
            "mean_fade_30m": round(sum(data["fade_30m"]) / n, 2),
            "mean_fade_eod": round(sum(data["fade_eod"]) / n, 2),
            "P_close_below_vwap": round(sum(data["below_vwap"]) / n * 100, 1),
            "P_continue_30m": round(sum(data["continue_30m"]) / n * 100, 1),
        }

    zones = {z: _summarize(buckets[z]) for z in ("green", "yellow", "red")}
    spike_zones = {z: _summarize(spike_buckets[z]) for z in ("green", "yellow", "red")}

    return {
        "date_start": date_start,
        "date_end": date_end,
        "poll_interval_min": poll_interval_min,
        "n_sessions": n_sessions,
        "n_observations": n_obs,
        "zones": zones,
        "spike_subset_ext_prev_ge5": spike_zones,
        "interpretation": {
            "green": "ext_vwap <1.5σ 且 ext_prev <4% · 趨勢延續機率較高",
            "yellow": "ext_vwap >1.5σ 或 ext_ma20>12% · 短線回撤機率上升",
            "red": "climax / heat≥70 / >2σ · fade 高機率（3711 型）",
        },
    }


def render_calibration_md(payload: dict[str, Any]) -> str:
    lines = [
        "# Extension radar · 機率校準（Phase 2）",
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        f"5m poll 觀測：{payload['n_observations']} · sessions：{payload['n_sessions']}",
        "",
        "## 全樣本 · 分區機率",
        "",
        "| 區間 | n | P(30m fade≥2%) | mean 30m | mean EOD | P(收<VWAP) | P(30m续强) |",
        "|------|---|----------------|----------|----------|------------|-------------|",
    ]
    for z, label in (("green", "🟢"), ("yellow", "🟡"), ("red", "🔴")):
        s = payload["zones"].get(z) or {}
        if not s.get("n"):
            lines.append(f"| {label} {z} | 0 | — | — | — | — | — |")
            continue
        lines.append(
            f"| {label} {z} | {s['n']} | {s.get('P_fade_30m_ge2')}% "
            f"| {s.get('mean_fade_30m')}% | {s.get('mean_fade_eod')}% "
            f"| {s.get('P_close_below_vwap')}% | {s.get('P_continue_30m')}% |"
        )
    lines += [
        "",
        "## spike 子樣本（ext_prev ≥ +5%）",
        "",
        "| 區間 | n | P(30m fade≥2%) | P(收<VWAP) | P(30m续强) |",
        "|------|---|----------------|------------|-------------|",
    ]
    spike = payload.get("spike_subset_ext_prev_ge5") or {}
    for z, label in (("green", "🟢"), ("yellow", "🟡"), ("red", "🔴")):
        s = spike.get(z) or {}
        if not s.get("n"):
            lines.append(f"| {label} {z} | 0 | — | — | — |")
            continue
        lines.append(
            f"| {label} {z} | {s['n']} | {s.get('P_fade_30m_ge2')}% "
            f"| {s.get('P_close_below_vwap')}% | {s.get('P_continue_30m')}% |"
        )
    lines += [
        "",
        "---",
        "模組：`scripts/tools/calibrate_extension_probs.py`",
        "",
    ]
    return "\n".join(lines)
