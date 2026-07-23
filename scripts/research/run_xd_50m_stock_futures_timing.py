#!/usr/bin/env python3
"""新店 → 有夜盤個股期作多時點（Research only）。

凍結：分點約 15–16 點可知；個股期夜盤 17:25 起（非 15:00）。
FinMind 有 after_market 的個股期：2330→CDF/QFF、2303→CCF。

宇宙：
  - leading_top1（預設）：50M×Leading×Top1，主測 2330
  - night50：當天新店該股金額≥50M 且標的有夜盤（2330|2303）→ 盤後作多

進場臂：
  - AH1725：訊號日 ≥17:25 第一筆近月 tick（缺 tick 則 after_market open 代理）
  - D1_OPEN：下一交易日日盤 open（tick≥08:45 或 position open）

出場：訊號日後第 H 個交易日之日盤 close（H∈{1,3,5,8}）。
附錄：僅持有該夜盤至夜盤末筆。

  PYTHONPATH=src .venv/bin/python scripts/research/run_xd_50m_stock_futures_timing.py
  PYTHONPATH=src .venv/bin/python scripts/research/run_xd_50m_stock_futures_timing.py \\
    --universe night50 --start 2024-07-17
  PYTHONPATH=src .venv/bin/python scripts/research/run_xd_50m_stock_futures_timing.py --no-write
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from finmind_client import fetch_finmind, fetch_finmind_json  # noqa: E402
from rank_stats import max_drawdown_pct  # noqa: E402
from report_paths import REPORTS_RESEARCH  # noqa: E402
from research.backtest.amount_rrg_copytrade_funnel_study import (  # noqa: E402
    DEFAULT_OOS_CUT,
    DEFAULT_WINDOW_START,
    build_funnel_feature_index,
)
from research.backtest.fubon_xindian_filter_study import (  # noqa: E402
    FilterConfig,
    build_config_grouped,
    build_raw_xindian_amount_grouped,
)
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

OUT_DIR = REPORTS_RESEARCH / "fubon-xindian-copytrade"
FILE_STEM_BY_UNIVERSE = {
    "leading_top1": "xd_50m_stock_futures_timing",
    "night50": "xd_night50_stock_futures_timing",
}
REQUEST_DELAY_SEC = 0.35
HOLDS = (1, 3, 5, 8)
COST_BPS_LIST = (0.0, 10.0)
NIGHT_START = "17:25:00"
DAY_OPEN = "08:45:00"
DAY_END = "13:45:00"
NIGHT_END_NEXT = "05:00:00"
NIGHT50_STOCK_IDS = frozenset({"2330", "2303"})
NIGHT50_AMOUNT_NTD = 50_000_000.0

# FinMind futures_id · multiplier = 契約股數（PnL NTD = Δprice × multiplier）
PRODUCTS: tuple[dict[str, Any], ...] = (
    {
        "product": "CDF",
        "label": "台積電期貨",
        "stock_id": "2330",
        "multiplier": 2000,
        "has_night": True,
    },
    {
        "product": "QFF",
        "label": "小型台積電期貨",
        "stock_id": "2330",
        "multiplier": 100,
        "has_night": True,
    },
    {
        "product": "CCF",
        "label": "聯電期貨",
        "stock_id": "2303",
        "multiplier": 2000,
        "has_night": True,
    },
)

CONTRACT = FilterConfig(
    metric="amount",
    threshold=50_000_000.0,
    min_avg_volume_shares=200_000.0,
    w20_momentum_min=None,
    rs_ratio_min=None,
    rrg_quadrant="leading",
    top_n=1,
    entry_lag=0,
    hold=8,
    n_slots=1,
)


@dataclass(frozen=True)
class Tick:
    ts: datetime
    contract: str
    price: float
    volume: int


def _is_outright(contract_date: str) -> bool:
    return "/" not in contract_date and len(contract_date) >= 6


def _parse_tick_ts(raw: str) -> datetime | None:
    text = str(raw).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    return None


def list_trading_days(conn, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date
        FROM stock_daily_bars
        WHERE source = 'finmind'
          AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date
        """,
        (start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def nth_trading_day_after(calendar: list[str], signal_date: str, n: int) -> str | None:
    if n < 1:
        raise ValueError("n must be >= 1")
    try:
        idx = calendar.index(signal_date)
    except ValueError:
        # signal may exist on tape but missing from bars calendar
        later = [d for d in calendar if d > signal_date]
        if len(later) < n:
            return None
        return later[n - 1]
    target = idx + n
    if target >= len(calendar):
        return None
    return calendar[target]


def build_signal_days(
    conn,
    *,
    window_start: str,
    window_end: str,
    stock_id: str = "2330",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    raw = build_raw_xindian_amount_grouped(
        conn,
        window_start=window_start,
        window_end=window_end,
        min_net_amount_ntd=30_000_000.0,
    )
    features = build_funnel_feature_index(conn, raw)
    grouped, funnel = build_config_grouped(raw, features, CONTRACT)
    out: list[dict[str, Any]] = []
    for day in sorted(grouped):
        leg = grouped[day][0]
        if str(leg.stock_id) != stock_id:
            continue
        feat = features.get((day, stock_id), {})
        close = feat.get("signal_close")
        amount = (
            float(leg.share_delta or 0.0) * float(close)
            if close is not None
            else None
        )
        out.append(
            {
                "signal_date": day,
                "stock_id": stock_id,
                "share_delta": float(leg.share_delta or 0.0),
                "signal_close": close,
                "amount_ntd": amount,
                "quadrant": feat.get("quadrant"),
            }
        )
    return out, funnel


def build_night50_signal_days(
    conn,
    *,
    window_start: str,
    window_end: str,
    stock_ids: frozenset[str] = NIGHT50_STOCK_IDS,
    min_amount_ntd: float = NIGHT50_AMOUNT_NTD,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Days where Xindian net amount ≥ threshold on a night-session equity."""
    raw = build_raw_xindian_amount_grouped(
        conn,
        window_start=window_start,
        window_end=window_end,
        min_net_amount_ntd=min_amount_ntd,
    )
    features = build_funnel_feature_index(conn, raw)
    out: list[dict[str, Any]] = []
    by_stock: dict[str, int] = {sid: 0 for sid in sorted(stock_ids)}
    for day in sorted(raw):
        if not (window_start <= day <= window_end):
            continue
        for leg in raw[day]:
            sid = str(leg.stock_id)
            if sid not in stock_ids:
                continue
            feat = features.get((day, sid), {})
            close = feat.get("signal_close")
            amount = (
                float(leg.share_delta or 0.0) * float(close)
                if close is not None
                else None
            )
            out.append(
                {
                    "signal_date": day,
                    "stock_id": sid,
                    "share_delta": float(leg.share_delta or 0.0),
                    "signal_close": close,
                    "amount_ntd": amount,
                    "quadrant": feat.get("quadrant"),
                }
            )
            by_stock[sid] = by_stock.get(sid, 0) + 1
    funnel = {
        "universe": "night50",
        "min_amount_ntd": min_amount_ntd,
        "night_stock_ids": sorted(stock_ids),
        "n_signal_rows": len(out),
        "by_stock": by_stock,
        "n_unique_days": len({r["signal_date"] for r in out}),
    }
    return out, funnel


def products_for_universe(universe: str) -> tuple[dict[str, Any], ...]:
    if universe == "night50":
        return PRODUCTS
    if universe == "leading_top1":
        return tuple(p for p in PRODUCTS if p["stock_id"] == "2330")
    raise ValueError(f"unknown universe: {universe}")


def fetch_futures_daily(futures_id: str, start: str, end: str) -> list[dict]:
    return fetch_finmind(
        "TaiwanFuturesDaily",
        futures_id,
        date.fromisoformat(start),
        date.fromisoformat(end),
        timeout=120,
    )


def fetch_futures_ticks(futures_id: str, trade_date: str) -> list[Tick]:
    payload = fetch_finmind_json(
        {
            "dataset": "TaiwanFuturesTick",
            "data_id": futures_id,
            "start_date": trade_date,
            "end_date": trade_date,
        },
        timeout=180,
    )
    rows = payload.get("data") or []
    out: list[Tick] = []
    for row in rows:
        contract = str(row.get("contract_date") or "")
        if not _is_outright(contract):
            continue
        ts = _parse_tick_ts(str(row.get("date") or ""))
        price = float(row.get("price") or 0.0)
        if ts is None or price <= 0:
            continue
        out.append(
            Tick(
                ts=ts,
                contract=contract,
                price=price,
                volume=int(row.get("volume") or 0),
            )
        )
    out.sort(key=lambda t: (t.ts, t.contract))
    return out


def pick_near_contract_from_ticks(ticks: list[Tick]) -> str | None:
    if not ticks:
        return None
    vol: dict[str, int] = defaultdict(int)
    for tick in ticks:
        vol[tick.contract] += max(tick.volume, 1)
    return max(vol.items(), key=lambda kv: kv[1])[0]


def first_tick_at_or_after(
    ticks: list[Tick],
    *,
    day: str,
    hhmmss: str,
    contract: str | None = None,
) -> Tick | None:
    threshold = datetime.strptime(f"{day} {hhmmss}", "%Y-%m-%d %H:%M:%S")
    for tick in ticks:
        if contract is not None and tick.contract != contract:
            continue
        if tick.ts.date().isoformat() != day:
            continue
        if tick.ts >= threshold:
            return tick
    return None


def last_night_tick(
    ticks_signal_day: list[Tick],
    ticks_next_calendar: list[Tick],
    *,
    signal_day: str,
    contract: str,
) -> Tick | None:
    """Night session: signal_day ≥17:25 through next calendar ≤05:00."""
    next_day = (
        date.fromisoformat(signal_day) + timedelta(days=1)
    ).isoformat()
    night_start = datetime.strptime(
        f"{signal_day} {NIGHT_START}", "%Y-%m-%d %H:%M:%S"
    )
    night_end = datetime.strptime(
        f"{next_day} {NIGHT_END_NEXT}", "%Y-%m-%d %H:%M:%S"
    )
    pool = [
        t
        for t in (ticks_signal_day + ticks_next_calendar)
        if t.contract == contract and night_start <= t.ts <= night_end
    ]
    return pool[-1] if pool else None


def pick_daily_row(
    rows: list[dict],
    *,
    session_date: str,
    session: str,
) -> dict | None:
    candidates = [
        r
        for r in rows
        if str(r.get("date")) == session_date
        and str(r.get("trading_session")) == session
        and float(r.get("close") or 0) > 0
        and _is_outright(str(r.get("contract_date") or ""))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: int(r.get("volume") or 0))


def _trim_mean(xs: list[float]) -> float | None:
    if len(xs) < 5:
        return mean(xs) if xs else None
    ordered = sorted(xs)
    return mean(ordered[1:-1])


def summarize_returns(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {
            "n": 0,
            "median_return_pct": None,
            "mean_return_pct": None,
            "trim_return_pct": None,
            "win_rate_pct": None,
            "max_drawdown_pct": None,
        }
    return {
        "n": len(returns),
        "median_return_pct": round(median(returns), 4),
        "mean_return_pct": round(mean(returns), 4),
        "trim_return_pct": (
            round(_trim_mean(returns), 4) if _trim_mean(returns) is not None else None
        ),
        "win_rate_pct": round(100.0 * sum(r > 0 for r in returns) / len(returns), 2),
        "max_drawdown_pct": max_drawdown_pct(returns),
    }


def trade_return_pct(entry: float, exit_: float, cost_bps: float) -> float:
    gross = (exit_ - entry) / entry * 100.0
    return gross - cost_bps / 100.0


def build_trades_for_product(
    *,
    product: dict[str, Any],
    signals: list[dict[str, Any]],
    calendar: list[str],
    daily_rows: list[dict],
    tick_cache: dict[tuple[str, str], list[Tick]],
    delay_sec: float,
    ah_source: str = "tick",
) -> dict[str, Any]:
    """ah_source: ``tick`` (prefer ≥17:25 tick) or ``daily`` (after_market open only)."""
    futures_id = str(product["product"])
    multiplier = int(product["multiplier"])
    trades: list[dict[str, Any]] = []
    skips: list[dict[str, str]] = []
    use_ticks = ah_source != "daily"

    def ticks_for(day: str) -> list[Tick]:
        key = (futures_id, day)
        if key not in tick_cache:
            tick_cache[key] = fetch_futures_ticks(futures_id, day)
            time.sleep(max(0.0, delay_sec))
        return tick_cache[key]

    for sig in signals:
        signal_date = str(sig["signal_date"])
        exit_dates = {
            h: nth_trading_day_after(calendar, signal_date, h) for h in HOLDS
        }
        d1 = nth_trading_day_after(calendar, signal_date, 1)
        if d1 is None:
            skips.append({"signal_date": signal_date, "reason": "no_d1_trading_day"})
            continue

        day_ticks: list[Tick] = ticks_for(signal_date) if use_ticks else []
        # next calendar day ticks for night-session end (may be weekend)
        next_cal = (date.fromisoformat(signal_date) + timedelta(days=1)).isoformat()
        next_cal_ticks: list[Tick] = (
            ticks_for(next_cal) if use_ticks and next_cal <= calendar[-1] else []
        )

        night_pool = [
            t
            for t in day_ticks
            if t.ts.date().isoformat() == signal_date
            and t.ts.strftime("%H:%M:%S") >= NIGHT_START
        ]
        near = pick_near_contract_from_ticks(night_pool) or pick_near_contract_from_ticks(
            [
                t
                for t in day_ticks
                if DAY_OPEN <= t.ts.strftime("%H:%M:%S") <= DAY_END
            ]
        )
        ah_tick = (
            first_tick_at_or_after(
                day_ticks, day=signal_date, hhmmss=NIGHT_START, contract=near
            )
            if use_ticks and near
            else None
        )
        ah_proxy = pick_daily_row(
            daily_rows, session_date=signal_date, session="after_market"
        )
        ah_entry_price: float | None = None
        ah_entry_source = None
        ah_contract = None
        if ah_tick is not None:
            # Guard: never accept pre-17:25
            if ah_tick.ts.strftime("%H:%M:%S") < NIGHT_START:
                skips.append(
                    {
                        "signal_date": signal_date,
                        "reason": "ah_tick_before_1725",
                    }
                )
            else:
                ah_entry_price = ah_tick.price
                ah_entry_source = "tick_1725"
                ah_contract = ah_tick.contract
        elif ah_proxy is not None and float(ah_proxy.get("open") or 0) > 0:
            ah_entry_price = float(ah_proxy["open"])
            ah_entry_source = "daily_after_market_open_proxy"
            ah_contract = str(ah_proxy.get("contract_date"))

        d1_ticks: list[Tick] = ticks_for(d1) if use_ticks else []
        d1_near = near or pick_near_contract_from_ticks(
            [
                t
                for t in d1_ticks
                if DAY_OPEN <= t.ts.strftime("%H:%M:%S") <= DAY_END
            ]
        )
        d1_tick = (
            first_tick_at_or_after(
                d1_ticks, day=d1, hhmmss=DAY_OPEN, contract=d1_near
            )
            if use_ticks and d1_near
            else None
        )
        d1_daily = pick_daily_row(daily_rows, session_date=d1, session="position")
        d1_entry_price: float | None = None
        d1_entry_source = None
        d1_contract = None
        if d1_tick is not None:
            d1_entry_price = d1_tick.price
            d1_entry_source = "tick_day_open"
            d1_contract = d1_tick.contract
        elif d1_daily is not None and float(d1_daily.get("open") or 0) > 0:
            d1_entry_price = float(d1_daily["open"])
            d1_entry_source = "daily_position_open"
            d1_contract = str(d1_daily.get("contract_date"))

        night_exit_tick = None
        if use_ticks and ah_contract:
            night_exit_tick = last_night_tick(
                day_ticks,
                next_cal_ticks,
                signal_day=signal_date,
                contract=ah_contract,
            )
        elif (
            not use_ticks
            and ah_entry_price is not None
            and ah_proxy is not None
            and float(ah_proxy.get("close") or 0) > 0
        ):
            # daily appendix: same-night exit ≈ after_market close
            night_exit_tick = Tick(
                ts=datetime.strptime(
                    f"{signal_date} 23:59:59", "%Y-%m-%d %H:%M:%S"
                ),
                contract=str(ah_proxy.get("contract_date") or ah_contract or ""),
                price=float(ah_proxy["close"]),
                volume=int(ah_proxy.get("volume") or 0),
            )

        for hold in HOLDS:
            exit_day = exit_dates[hold]
            if exit_day is None:
                continue
            exit_row = pick_daily_row(
                daily_rows, session_date=exit_day, session="position"
            )
            if exit_row is None or float(exit_row.get("close") or 0) <= 0:
                continue
            exit_px = float(exit_row["close"])
            exit_contract = str(exit_row.get("contract_date"))

            for entry_arm, entry_px, entry_src, entry_contract in (
                ("AH1725", ah_entry_price, ah_entry_source, ah_contract),
                ("D1_OPEN", d1_entry_price, d1_entry_source, d1_contract),
            ):
                if entry_px is None or entry_src is None:
                    continue
                for cost_bps in COST_BPS_LIST:
                    ret = trade_return_pct(entry_px, exit_px, cost_bps)
                    pnl = (exit_px - entry_px) * multiplier
                    # rough cost in NTD: bps on notional
                    notional = entry_px * multiplier
                    pnl_net = pnl - notional * (cost_bps / 10000.0)
                    trades.append(
                        {
                            "signal_date": signal_date,
                            "product": futures_id,
                            "entry_arm": entry_arm,
                            "hold_trading_days": hold,
                            "cost_bps": cost_bps,
                            "entry_price": entry_px,
                            "entry_source": entry_src,
                            "entry_contract": entry_contract,
                            "exit_date": exit_day,
                            "exit_price": exit_px,
                            "exit_contract": exit_contract,
                            "return_pct": round(ret, 6),
                            "pnl_ntd_per_contract": round(pnl_net, 2),
                            "multiplier": multiplier,
                        }
                    )

        # Night-only appendix (cost 0 and 10)
        if ah_entry_price is not None and night_exit_tick is not None:
            for cost_bps in COST_BPS_LIST:
                ret = trade_return_pct(
                    ah_entry_price, night_exit_tick.price, cost_bps
                )
                notional = ah_entry_price * multiplier
                pnl = (night_exit_tick.price - ah_entry_price) * multiplier
                pnl_net = pnl - notional * (cost_bps / 10000.0)
                trades.append(
                    {
                        "signal_date": signal_date,
                        "product": futures_id,
                        "entry_arm": "AH1725",
                        "hold_trading_days": 0,
                        "hold_label": "night_only",
                        "cost_bps": cost_bps,
                        "entry_price": ah_entry_price,
                        "entry_source": ah_entry_source,
                        "entry_contract": ah_contract,
                        "exit_date": night_exit_tick.ts.date().isoformat(),
                        "exit_price": night_exit_tick.price,
                        "exit_contract": night_exit_tick.contract,
                        "return_pct": round(ret, 6),
                        "pnl_ntd_per_contract": round(pnl_net, 2),
                        "multiplier": multiplier,
                    }
                )

        if ah_entry_price is None and d1_entry_price is None:
            skips.append(
                {"signal_date": signal_date, "reason": "no_entry_price"}
            )

    return {"trades": trades, "skips": skips}


def aggregate_arms(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        key = (
            trade["product"],
            trade["entry_arm"],
            trade.get("hold_label") or trade["hold_trading_days"],
            trade["cost_bps"],
        )
        groups[key].append(trade)
    def _sort_key(
        item: tuple[tuple[Any, ...], list[dict[str, Any]]],
    ) -> tuple[Any, ...]:
        (product, arm, hold, cost), _ = item
        hold_key = (1, str(hold)) if not isinstance(hold, int) else (0, hold)
        return (str(product), str(arm), hold_key, float(cost))

    rows: list[dict[str, Any]] = []
    for (product, arm, hold, cost), items in sorted(
        groups.items(), key=_sort_key
    ):
        rets = [float(t["return_pct"]) for t in items]
        pnls = [float(t["pnl_ntd_per_contract"]) for t in items]
        proxy_n = sum(
            1 for t in items if "proxy" in str(t.get("entry_source") or "")
        )
        summary = summarize_returns(rets)
        summary.update(
            {
                "product": product,
                "entry_arm": arm,
                "hold": hold,
                "cost_bps": cost,
                "median_pnl_ntd": round(median(pnls), 2) if pnls else None,
                "mean_pnl_ntd": round(mean(pnls), 2) if pnls else None,
                "proxy_entries": proxy_n,
            }
        )
        rows.append(summary)
    return rows


def pick_winner(arm_rows: list[dict[str, Any]], *, cost_bps: float = 10.0) -> dict[str, Any] | None:
    """Primary: cost stress, exclude night_only, rank by OOS-like full median then n."""
    pool = [
        r
        for r in arm_rows
        if float(r["cost_bps"]) == cost_bps
        and r["hold"] != "night_only"
        and int(r.get("n") or 0) >= 5
        and r.get("median_return_pct") is not None
    ]
    if not pool:
        return None
    return max(
        pool,
        key=lambda r: (
            float(r["median_return_pct"]),
            float(r.get("trim_return_pct") or -1e9),
            int(r["n"]),
        ),
    )


def slice_arm_rows(
    trades: list[dict[str, Any]],
    *,
    start: str,
    end_exclusive: str,
) -> list[dict[str, Any]]:
    subset = [
        t
        for t in trades
        if start <= str(t["signal_date"]) < end_exclusive
    ]
    return aggregate_arms(subset)


def render_markdown(payload: dict[str, Any]) -> str:
    c = payload["contract"]
    winner = payload.get("winner_cost10")
    title = (
        "# 新店 night50（≥50M × 有夜盤個股）× 個股期作多時點"
        if c.get("universe") == "night50"
        else "# 新店 50M×Leading × 個股期作多時點"
    )
    lines = [
        title,
        "",
        f"- window: `{c['window_start']}` → `{c['window_end']}` · OOS `{c['oos_cut']}`",
        f"- universe: `{c.get('universe')}` · signal: `{c['signal']}`",
        f"- focus stocks: `{c.get('focus_stock_ids') or c.get('focus_stock_id')}`",
        f"- products: `{c['products']}`",
        f"- timing: AH1725 = night ≥17:25; D1_OPEN = next RTH open",
        f"- note: `{c['data_note']}`",
        "",
        "## Verdict",
        "",
    ]
    if winner:
        lines.append(
            f"- best @10bps (n≥5, excl night_only): "
            f"**{winner['product']} · {winner['entry_arm']} · H={winner['hold']}** "
            f"median={winner['median_return_pct']}% "
            f"mean={winner['mean_return_pct']}% "
            f"win={winner['win_rate_pct']}% n={winner['n']}"
        )
    else:
        lines.append("- no arm with n≥5 under 10bps")
    lines.extend(
        [
            "",
            f"- n_signal_rows: `{payload['n_signal_days']}`",
            f"- by_stock: `{payload.get('signals_by_stock')}`",
            f"- decision_note: `{payload['decision_note']}`",
            "",
            "## Arms @10bps (full)",
            "",
            "| product | entry | hold | n | median% | mean% | win% | MaxDD% | median PnL |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload["arms_full_cost10"]:
        lines.append(
            f"| {row['product']} | {row['entry_arm']} | {row['hold']} | {row['n']} | "
            f"{row['median_return_pct']} | {row['mean_return_pct']} | "
            f"{row['win_rate_pct']} | {row['max_drawdown_pct']} | "
            f"{row['median_pnl_ntd']} |"
        )
    lines.extend(
        [
            "",
            "## Arms @10bps (OOS)",
            "",
            "| product | entry | hold | n | median% | mean% | win% |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload["arms_oos_cost10"]:
        lines.append(
            f"| {row['product']} | {row['entry_arm']} | {row['hold']} | {row['n']} | "
            f"{row['median_return_pct']} | {row['mean_return_pct']} | {row['win_rate_pct']} |"
        )
    lines.extend(
        [
            "",
            "## Example · 2026-07-17",
            "",
            f"`{payload.get('example_20260717')}`",
            "",
            "Research observe only · no Strategy / Order change.",
            "",
        ]
    )
    return "\n".join(lines)


def run_study(
    conn,
    *,
    window_start: str,
    window_end: str,
    oos_cut: str,
    universe: str = "leading_top1",
    ah_source: str = "tick",
    delay_sec: float = REQUEST_DELAY_SEC,
) -> dict[str, Any]:
    products = products_for_universe(universe)
    if universe == "night50":
        signals, funnel = build_night50_signal_days(
            conn, window_start=window_start, window_end=window_end
        )
        signal_label = (
            f"xd ≥{int(NIGHT50_AMOUNT_NTD / 1e6)}M on night-equity "
            f"({','.join(sorted(NIGHT50_STOCK_IDS))}) · no Leading/Top1"
        )
        focus_ids = sorted(NIGHT50_STOCK_IDS)
    else:
        signals, funnel = build_signal_days(
            conn,
            window_start=window_start,
            window_end=window_end,
            stock_id="2330",
        )
        signal_label = "xd ≥50M · Leading · Top1 · vol≥200k"
        focus_ids = ["2330"]

    signals_by_stock: dict[str, int] = {}
    for sig in signals:
        sid = str(sig["stock_id"])
        signals_by_stock[sid] = signals_by_stock.get(sid, 0) + 1

    # extend calendar a bit past window for exits
    cal_end = (
        date.fromisoformat(window_end) + timedelta(days=40)
    ).isoformat()
    calendar = list_trading_days(conn, window_start, cal_end)
    tick_cache: dict[tuple[str, str], list[Tick]] = {}
    all_trades: list[dict[str, Any]] = []
    all_skips: list[dict[str, str]] = []
    daily_by_product: dict[str, list[dict]] = {}

    for product in products:
        fid = str(product["product"])
        stock_id = str(product["stock_id"])
        product_signals = [s for s in signals if str(s["stock_id"]) == stock_id]
        if not product_signals:
            continue
        daily_by_product[fid] = fetch_futures_daily(fid, window_start, cal_end)
        time.sleep(max(0.0, delay_sec))
        block = build_trades_for_product(
            product=product,
            signals=product_signals,
            calendar=calendar,
            daily_rows=daily_by_product[fid],
            tick_cache=tick_cache,
            delay_sec=delay_sec,
            ah_source=ah_source,
        )
        all_trades.extend(block["trades"])
        all_skips.extend([{**s, "product": fid} for s in block["skips"]])

    end_exclusive = (
        date.fromisoformat(window_end) + timedelta(days=1)
    ).isoformat()
    arms_full = aggregate_arms(all_trades)
    arms_is = slice_arm_rows(all_trades, start=window_start, end_exclusive=oos_cut)
    arms_oos = slice_arm_rows(
        all_trades, start=oos_cut, end_exclusive=end_exclusive
    )
    arms_full_10 = [r for r in arms_full if float(r["cost_bps"]) == 10.0]
    arms_oos_10 = [r for r in arms_oos if float(r["cost_bps"]) == 10.0]
    winner = pick_winner(arms_full_10, cost_bps=10.0)

    example = {
        t["product"]: t
        for t in all_trades
        if t["signal_date"] == "2026-07-17"
        and t["entry_arm"] in ("AH1725", "D1_OPEN")
        and t["hold_trading_days"] == 1
        and float(t["cost_bps"]) == 10.0
    }

    proxy_share = (
        round(
            100.0
            * sum(1 for t in all_trades if "proxy" in str(t.get("entry_source")))
            / len(all_trades),
            2,
        )
        if all_trades
        else None
    )
    oos_winner = pick_winner(arms_oos_10, cost_bps=10.0)
    decision_note = (
        "個股期得知分點後最早可下在 17:25 夜盤（非 15:00）；"
        "AH1725 vs D1_OPEN 以單筆中位比較。"
    )
    if winner:
        decision_note += (
            f" Full@10bps 最佳：{winner['product']} {winner['entry_arm']} "
            f"H={winner['hold']} med={winner['median_return_pct']}%."
        )
    else:
        decision_note += " Full 樣本不足或定價失敗。"
    if oos_winner:
        decision_note += (
            f" OOS@10bps 最佳：{oos_winner['product']} {oos_winner['entry_arm']} "
            f"H={oos_winner['hold']} med={oos_winner['median_return_pct']}%."
        )
    else:
        decision_note += " OOS@10bps 無 n≥5 且中位可辨識勝者（偏弱／樣本薄）。"
    decision_note += " Observe only · 不採納 Strategy／Order。"

    schema = (
        "xd_night50_stock_futures_timing-v1"
        if universe == "night50"
        else "xd_50m_stock_futures_timing-v1"
    )
    return {
        "schema": schema,
        "contract": {
            "universe": universe,
            "window_start": window_start,
            "window_end": window_end,
            "oos_cut": oos_cut,
            "ah_source": ah_source,
            "signal": signal_label,
            "focus_stock_id": focus_ids[0] if len(focus_ids) == 1 else None,
            "focus_stock_ids": focus_ids,
            "products": [
                {
                    "futures_id": p["product"],
                    "label": p["label"],
                    "stock_id": p["stock_id"],
                    "multiplier": p["multiplier"],
                }
                for p in products
            ],
            "entry_arms": ["AH1725", "D1_OPEN"],
            "holds": list(HOLDS),
            "cost_bps": list(COST_BPS_LIST),
            "data_note": (
                f"ah_source={ah_source}; "
                "AH1725 prefers FinMind TaiwanFuturesTick ≥17:25 when ah_source=tick; "
                "falls back / uses TaiwanFuturesDaily after_market open (marked proxy). "
                f"proxy_share_of_trades={proxy_share}%"
            ),
            "pit": (
                "Branch tape + close are known by ~16:00–17:25; "
                "no entry before 17:25 on signal night. "
                "Night equity futures universe = 2330/2303 only (FinMind after_market)."
            ),
        },
        "funnel_signal": funnel,
        "n_signal_days": len(signals),
        "signals_by_stock": signals_by_stock,
        "signals": signals,
        "n_trades": len(all_trades),
        "skips": all_skips,
        "arms_full": arms_full,
        "arms_is": arms_is,
        "arms_oos": arms_oos,
        "arms_full_cost10": arms_full_10,
        "arms_oos_cost10": arms_oos_10,
        "winner_cost10": winner,
        "example_20260717": example,
        "decision_note": decision_note,
        "strategy_action": "Research observe only · no Strategy / Order change.",
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="XD → stock futures long timing (leading_top1 | night50)"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--oos-cut", default=DEFAULT_OOS_CUT)
    ap.add_argument(
        "--universe",
        choices=("leading_top1", "night50"),
        default="leading_top1",
        help="leading_top1=50M×Leading×Top1 2330; night50=≥50M on 2330|2303",
    )
    ap.add_argument(
        "--ah-source",
        choices=("tick", "daily"),
        default="",
        help="tick=≥17:25 tick (slow); daily=after_market open proxy. "
        "Default: daily for night50, tick for leading_top1",
    )
    ap.add_argument("--delay", type=float, default=REQUEST_DELAY_SEC)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    conn = connect(args.db)
    try:
        end = args.end
        if not end:
            row = conn.execute(
                """
                SELECT MAX(trade_date) FROM stock_broker_branch_daily
                WHERE securities_trader_id = '9661' AND source = 'finmind'
                """
            ).fetchone()
            end = str(row[0] or date.today().isoformat())
        start = args.start
        if not start:
            if args.universe == "night50":
                # ~2y ending at tape max
                start = (
                    date.fromisoformat(end) - timedelta(days=730)
                ).isoformat()
                start = max(DEFAULT_WINDOW_START, start)
            else:
                start = DEFAULT_WINDOW_START
        ah_source = args.ah_source or (
            "daily" if args.universe == "night50" else "tick"
        )
        file_stem = FILE_STEM_BY_UNIVERSE[args.universe]
        print(
            f"universe={args.universe} ah_source={ah_source} "
            f"window {start}..{end} oos_cut={args.oos_cut}"
        )
        payload = run_study(
            conn,
            window_start=start,
            window_end=end,
            oos_cut=args.oos_cut,
            universe=args.universe,
            ah_source=ah_source,
            delay_sec=float(args.delay),
        )
        print(
            f"signal_rows={payload['n_signal_days']} "
            f"by_stock={payload.get('signals_by_stock')} "
            f"trades={payload['n_trades']}"
        )
        print(f"decision: {payload['decision_note']}")
        winner = payload.get("winner_cost10")
        if winner:
            print(
                f"winner@10bps: {winner['product']} {winner['entry_arm']} "
                f"H={winner['hold']} med={winner['median_return_pct']} "
                f"n={winner['n']}"
            )
        for row in payload["arms_full_cost10"]:
            if row["hold"] == "night_only":
                continue
            print(
                f"  {row['product']:4} {row['entry_arm']:7} H={row['hold']!s:<3} "
                f"n={row['n']:<3} med={row['median_return_pct']} "
                f"mean={row['mean_return_pct']} win={row['win_rate_pct']}"
            )
        if not args.no_write:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            stem = OUT_DIR / f"{date.today().strftime('%Y%m%d')}_{file_stem}"
            json_path = Path(str(stem) + ".json")
            md_path = Path(str(stem) + ".md")
            json_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            md_path.write_text(render_markdown(payload), encoding="utf-8")
            print(f"wrote {json_path}")
            print(f"wrote {md_path}")
        return 0
    finally:
        conn.close()


# --- tiny pure helpers for unit tests ---
def assert_entry_not_before_1725(ts: datetime) -> None:
    if ts.strftime("%H:%M:%S") < NIGHT_START:
        raise AssertionError(f"entry before night open: {ts}")


if __name__ == "__main__":
    raise SystemExit(main())
