"""Pullback TV rules × Momentum Correction regime backtest (FinMind DB)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from .copytrade_backtest import bench_return_entry_to_exit
from .finpilot_local_backtest import load_price_panels, summarize_periods
from .finpilot_s04_layers import S04_LAYER_SPECS, _basket_return_from_panels, select_s04_layer
from flow_returns import trading_dates_after
from market_benchmark import load_benchmark_close
from market_breadth_ma import build_breadth_panel

REGIME_FILTER = "weak"  # 200MA breadth zone: corrective / weak participation
DEFAULT_TOP_N = 10
DEFAULT_MIN_VOL = 3_000_000
DEFAULT_HORIZONS = (30, 60)

STRATEGY_LABELS: dict[str, str] = {
    "minervini_pullback": "Minervini Pullback (Template + RSI + MACD)",
    "tomas_momentum_pb": "Swing Long Momentum Pullback + RS",
    "connors_rsi2": "RSI Pullback Finder (Connors RSI2)",
    "ema200_rsi_vol": "200 EMA + RSI Pullback + Volume Surge",
    "oos_ma200_rsi": "MA200 + RSI Pullback + Hard Close (bench filter)",
    "l1c_mom20_baseline": "L1c Mom20 Top30 (baseline)",
}


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()


def _sma(close: pd.Series, window: int) -> pd.Series:
    return close.rolling(window).mean()


def _macd_cross_up(close: pd.Series) -> pd.Series:
    macd_line = _ema(close, 12) - _ema(close, 26)
    signal = _ema(macd_line, 9)
    return (macd_line > signal) & (macd_line.shift(1) <= signal.shift(1))


from stage_analysis import minervini_pass_at_date

def _rs_rank(close: pd.DataFrame, bench: pd.Series, signal_date: str, lookback: int = 20) -> pd.Series:
    if signal_date not in close.index:
        return pd.Series(dtype=float)
    idx = close.index.get_loc(signal_date)
    if idx < lookback:
        return pd.Series(dtype=float)
    d0 = close.index[idx - lookback]
    stock_ret = close.loc[signal_date] / close.loc[d0] - 1.0
    b_ret = float(bench.loc[signal_date] / bench.loc[d0] - 1.0)
    return (stock_ret - b_ret).dropna()


def _liquid_universe(vol: pd.DataFrame, signal_date: str, min_vol: int) -> pd.Index:
    vol_ma20 = vol.rolling(20).mean()
    if signal_date not in vol_ma20.index:
        return pd.Index([])
    ok = vol_ma20.loc[signal_date] > min_vol
    return ok[ok].index


def picks_minervini_pullback(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    vol: pd.DataFrame,
    signal_date: str,
    *,
    top_n: int,
    min_vol: int,
) -> list[str]:
    del conn
    liquid = _liquid_universe(vol, signal_date, min_vol)
    if len(liquid) == 0 or signal_date not in close.index:
        return []
    template_ok = minervini_pass_at_date(close, signal_date, liquid, min_pass=7)
    rs = _rs_rank(close, bench, signal_date)
    idx = close.index.get_loc(signal_date)
    sub = close.iloc[: idx + 1]
    out: list[tuple[str, float]] = []
    for sid in template_ok:
        sid = str(sid)
        hist = sub[sid].dropna()
        if len(hist) < 200:
            continue
        if float(_rsi(hist, 14).tail(10).min()) >= 35:
            continue
        macd_line = _ema(hist, 12) - _ema(hist, 26)
        signal_line = _ema(macd_line, 9)
        macd_cross = bool(_macd_cross_up(hist).iloc[-1])
        macd_turn = float(macd_line.iloc[-1]) > float(signal_line.iloc[-1]) and float(
            macd_line.iloc[-1]
        ) > float(macd_line.iloc[-2])
        if not (macd_cross or macd_turn):
            continue
        rv = rs.get(sid)
        if rv is None or not np.isfinite(rv):
            continue
        out.append((sid, float(rv)))
    out.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in out[:top_n]]


def picks_tomas_momentum_pb(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    vol: pd.DataFrame,
    signal_date: str,
    *,
    top_n: int,
    min_vol: int,
) -> list[str]:
    del conn
    liquid = _liquid_universe(vol, signal_date, min_vol)
    if len(liquid) == 0 or signal_date not in close.index:
        return []
    idx = close.index.get_loc(signal_date)
    if idx < 200:
        return []
    rs = _rs_rank(close, bench, signal_date)
    rs_prev = _rs_rank(close, bench, close.index[idx - 1]) if idx > 0 else rs
    vol_ma20 = vol.rolling(20).mean()
    out: list[tuple[str, float]] = []
    for sid in liquid:
        sid = str(sid)
        if sid not in close.columns:
            continue
        hist = close.loc[:signal_date, sid].dropna()
        if len(hist) < 200:
            continue
        ema20 = _ema(hist, 20)
        ema50 = _ema(hist, 50)
        ema200 = _ema(hist, 200)
        px = float(hist.iloc[-1])
        if px <= float(ema50.iloc[-1]) or float(ema20.iloc[-1]) <= float(ema50.iloc[-1]):
            continue
        if px <= float(ema200.iloc[-1]):
            continue
        prev_px = float(hist.iloc[-2])
        if not (prev_px <= float(ema20.iloc[-2]) and px >= float(ema20.iloc[-1])):
            continue
        rsi = _rsi(hist, 14)
        r0, r1 = float(rsi.iloc[-2]), float(rsi.iloc[-1])
        if not (40 <= r1 <= 60 and r1 > r0):
            continue
        rv = rs.get(sid)
        rv_prev = rs_prev.get(sid)
        if rv is None or rv_prev is None or not np.isfinite(rv) or rv <= 0 or rv <= rv_prev:
            continue
        if signal_date not in vol.index or sid not in vol.columns:
            continue
        if float(vol.loc[signal_date, sid]) <= float(vol_ma20.loc[signal_date, sid]):
            continue
        out.append((sid, float(rv)))
    out.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in out[:top_n]]


def picks_connors_rsi2(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    vol: pd.DataFrame,
    signal_date: str,
    *,
    top_n: int,
    min_vol: int,
) -> list[str]:
    del conn
    liquid = _liquid_universe(vol, signal_date, min_vol)
    if len(liquid) == 0 or signal_date not in close.index:
        return []
    rs = _rs_rank(close, bench, signal_date)
    out: list[tuple[str, float]] = []
    for sid in liquid:
        sid = str(sid)
        if sid not in close.columns:
            continue
        hist = close.loc[:signal_date, sid].dropna()
        if len(hist) < 200:
            continue
        sma200 = _sma(hist, 200)
        sma5 = _sma(hist, 5)
        px = float(hist.iloc[-1])
        if px <= float(sma200.iloc[-1]) or px >= float(sma5.iloc[-1]):
            continue
        rsi2 = _rsi(hist, 2)
        if float(rsi2.iloc[-1]) >= 10:
            continue
        rv = rs.get(sid)
        if rv is None or not np.isfinite(rv):
            continue
        out.append((sid, float(rv)))
    out.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in out[:top_n]]


def picks_ema200_rsi_vol(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    vol: pd.DataFrame,
    signal_date: str,
    *,
    top_n: int,
    min_vol: int,
) -> list[str]:
    del conn
    liquid = _liquid_universe(vol, signal_date, min_vol)
    if len(liquid) == 0 or signal_date not in close.index:
        return []
    rs = _rs_rank(close, bench, signal_date)
    vol_ma20 = vol.rolling(20).mean()
    out: list[tuple[str, float]] = []
    for sid in liquid:
        sid = str(sid)
        if sid not in close.columns:
            continue
        hist = close.loc[:signal_date, sid].dropna()
        if len(hist) < 200:
            continue
        ema200 = _ema(hist, 200)
        px = float(hist.iloc[-1])
        if px <= float(ema200.iloc[-1]):
            continue
        if float(_rsi(hist, 14).iloc[-1]) >= 40:
            continue
        if signal_date not in vol.index:
            continue
        v = float(vol.loc[signal_date, sid])
        vma = float(vol_ma20.loc[signal_date, sid])
        if not np.isfinite(v) or not np.isfinite(vma) or v <= 1.5 * vma:
            continue
        rv = rs.get(sid)
        if rv is None or not np.isfinite(rv):
            continue
        out.append((sid, float(rv)))
    out.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in out[:top_n]]


def picks_oos_ma200_rsi(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    vol: pd.DataFrame,
    signal_date: str,
    *,
    top_n: int,
    min_vol: int,
) -> list[str]:
    del conn
    if signal_date not in bench.index:
        return []
    bhist = bench.loc[:signal_date].dropna()
    if len(bhist) < 200:
        return []
    if float(bhist.iloc[-1]) <= float(_sma(bhist, 200).iloc[-1]):
        return []
    liquid = _liquid_universe(vol, signal_date, min_vol)
    if len(liquid) == 0:
        return []
    rs = _rs_rank(close, bench, signal_date)
    out: list[tuple[str, float]] = []
    for sid in liquid:
        sid = str(sid)
        if sid not in close.columns:
            continue
        hist = close.loc[:signal_date, sid].dropna()
        if len(hist) < 200:
            continue
        sma200 = _sma(hist, 200)
        px = float(hist.iloc[-1])
        if px <= float(sma200.iloc[-1]):
            continue
        if float(_rsi(hist, 14).iloc[-1]) >= 40:
            continue
        rv = rs.get(sid)
        if rv is None or not np.isfinite(rv):
            continue
        out.append((sid, float(rv)))
    out.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in out[:top_n]]


def picks_l1c_mom20_baseline(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    bench: pd.Series,
    vol: pd.DataFrame,
    signal_date: str,
    *,
    top_n: int,
    min_vol: int,
) -> list[str]:
    del conn, bench, vol, min_vol
    spec = next(s for s in S04_LAYER_SPECS if s.layer_id == "L1c")
    fund_snap = pd.DataFrame({"stock_id": list(close.columns), "roe_latest_q": [None] * len(close.columns)})
    picks, _meta = select_s04_layer(
        spec,
        signal_date=signal_date,
        close=close,
        fund_snap=fund_snap,
        mom_lookback=20,
    )
    return picks[:top_n]


PICK_FNS: dict[str, Callable[..., list[str]]] = {
    "minervini_pullback": picks_minervini_pullback,
    "tomas_momentum_pb": picks_tomas_momentum_pb,
    "connors_rsi2": picks_connors_rsi2,
    "ema200_rsi_vol": picks_ema200_rsi_vol,
    "oos_ma200_rsi": picks_oos_ma200_rsi,
    "l1c_mom20_baseline": picks_l1c_mom20_baseline,
}


@dataclass(frozen=True)
class PullbackRegimeBacktestConfig:
    date_start: str = "2024-01-01"
    date_end: str = "2026-12-31"
    factor_mode: str = "rolling"
    top_n: int = DEFAULT_TOP_N
    min_vol: int = DEFAULT_MIN_VOL
    horizons: tuple[int, ...] = DEFAULT_HORIZONS


def eval_strategy_periods(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    opn: pd.DataFrame,
    bench: pd.Series,
    vol: pd.DataFrame,
    signal_dates: list[str],
    strategy_id: str,
    pick_fn: Callable[..., list[str]],
    hold_days: int,
    *,
    top_n: int,
    min_vol: int,
) -> tuple[list[dict], list[dict]]:
    periods: list[dict] = []
    skipped: list[dict] = []
    for signal_date in signal_dates:
        picks = pick_fn(
            conn,
            close,
            bench,
            vol,
            signal_date,
            top_n=top_n,
            min_vol=min_vol,
        )
        if not picks:
            skipped.append({"strategy_id": strategy_id, "signal_date": signal_date, "skip_reason": "no_picks"})
            continue
        entry_dates = trading_dates_after(conn, signal_date, count=1)
        if not entry_dates:
            skipped.append({"strategy_id": strategy_id, "signal_date": signal_date, "skip_reason": "no_entry"})
            continue
        entry_date = entry_dates[0]
        exit_dates = trading_dates_after(conn, entry_date, count=hold_days)
        if len(exit_dates) < hold_days:
            skipped.append({"strategy_id": strategy_id, "signal_date": signal_date, "skip_reason": f"incomplete_H{hold_days}"})
            continue
        exit_date = exit_dates[hold_days - 1]
        port_ret = _basket_return_from_panels(opn, close, picks, entry_date, exit_date)
        bench_ret = bench_return_entry_to_exit(conn, entry_date, exit_date, entry_price_mode="open")
        if port_ret is None or bench_ret is None:
            skipped.append({"strategy_id": strategy_id, "signal_date": signal_date, "skip_reason": "price_missing"})
            continue
        periods.append(
            {
                "strategy_id": strategy_id,
                "hold_days": hold_days,
                "signal_date": signal_date,
                "entry_date": entry_date,
                "exit_date": exit_date,
                "n_stocks": len(picks),
                "picks_json": json.dumps(picks, ensure_ascii=False),
                "return_pct": round(port_ret, 4),
                "bench_return_pct": round(bench_ret, 4),
                "excess_pct": round(port_ret - bench_ret, 4),
                "beat_bench": int(port_ret > bench_ret),
                "gross_win": int(port_ret > 0),
            }
        )
    return periods, skipped


def run_pullback_regime_backtest(
    conn: sqlite3.Connection,
    cfg: PullbackRegimeBacktestConfig,
) -> dict:
    close, opn, vol = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index).ffill()

    panel = build_breadth_panel(
        conn,
        date_start=cfg.date_start,
        date_end=cfg.date_end,
    )
    signal_dates = panel.loc[
        panel["zone_200"] == REGIME_FILTER, "trade_date"
    ].tolist()

    summaries: list[dict] = []
    all_periods: list[dict] = []
    skip_stats: list[dict] = []

    for strategy_id, label in STRATEGY_LABELS.items():
        pick_fn = PICK_FNS[strategy_id]
        for hold in cfg.horizons:
            complete, skipped = eval_strategy_periods(
                conn,
                close,
                opn,
                bench,
                vol,
                signal_dates,
                strategy_id,
                pick_fn,
                hold,
                top_n=cfg.top_n,
                min_vol=cfg.min_vol,
            )
            all_periods.extend(complete)
            skip_stats.append(
                {
                    "strategy_id": strategy_id,
                    "hold_days": hold,
                    "n_signal_days": len(signal_dates),
                    "n_complete": len(complete),
                    "n_skipped": len(skipped),
                }
            )
            if not complete:
                summaries.append(
                    {
                        "strategy_id": strategy_id,
                        "label": label,
                        "hold_days": hold,
                        "n_periods": 0,
                        "win_rate_vs_bench_pct": None,
                        "mean_excess_pct": None,
                        "median_excess_pct": None,
                        "mean_return_pct": None,
                    }
                )
                continue
            stats = summarize_periods(complete)
            excess = [p["excess_pct"] for p in complete]
            summaries.append(
                {
                    "strategy_id": strategy_id,
                    "label": label,
                    "hold_days": hold,
                    "n_periods": stats["n_periods"],
                    "win_rate_vs_bench_pct": stats["win_rate_vs_bench_pct"],
                    "mean_excess_pct": round(float(np.mean(excess)), 4),
                    "median_excess_pct": round(float(np.median(excess)), 4),
                    "mean_return_pct": stats["mean_return_pct"],
                    "mean_bench_return_pct": stats["mean_bench_pct"],
                }
            )

    return {
        "config": {
            "date_start": cfg.date_start,
            "date_end": cfg.date_end,
            "regime": REGIME_FILTER,
            "n_correction_days": len(signal_dates),
            "correction_dates": signal_dates,
            "top_n": cfg.top_n,
            "min_vol": cfg.min_vol,
            "horizons": list(cfg.horizons),
        },
        "summaries": summaries,
        "periods": all_periods,
        "skip_stats": skip_stats,
    }


def render_pullback_regime_markdown(result: dict) -> str:
    cfg = result["config"]
    lines = [
        "# Pullback 策略 × Momentum Correction 回測",
        "",
        f"> 區間 {cfg['date_start']} – {cfg['date_end']} · regime `{cfg['regime']}` · "
        f"修正日 **{cfg['n_correction_days']}** 天 · Top{cfg['top_n']} 等權 · "
        f"流動性 vol_ma20 > {cfg['min_vol']:,} · T+1 開盤進 · vs IX0001",
        "",
        "## 超額報酬摘要",
        "",
    ]
    for hold in cfg["horizons"]:
        lines.append(f"### H{hold}")
        lines.append("")
        lines.append("| 策略 | n | 勝台指% | 均超額% | 中位超額% | 均報酬% |")
        lines.append("|------|---|---------|---------|-----------|---------|")
        rows = sorted(
            [s for s in result["summaries"] if s["hold_days"] == hold],
            key=lambda x: (x["mean_excess_pct"] is None, -(x["mean_excess_pct"] or -999)),
        )
        for s in rows:
            wr = "—" if s["win_rate_vs_bench_pct"] is None else f"{s['win_rate_vs_bench_pct']}%"
            me = "—" if s["mean_excess_pct"] is None else f"{s['mean_excess_pct']:.2f}"
            med = "—" if s["median_excess_pct"] is None else f"{s['median_excess_pct']:.2f}"
            mr = "—" if s["mean_return_pct"] is None else f"{s['mean_return_pct']:.2f}"
            lines.append(
                f"| {s['label']} | {s['n_periods']} | {wr} | {me} | {med} | {mr} |"
            )
        lines.append("")

    lines.extend(["## 訊號覆蓋率", ""])
    lines.append("| 策略 | H | 修正日 | 成交 | 略過 |")
    lines.append("|------|---|--------|------|------|")
    for sk in result["skip_stats"]:
        label = STRATEGY_LABELS.get(sk["strategy_id"], sk["strategy_id"])
        lines.append(
            f"| {label} | H{sk['hold_days']} | {sk['n_signal_days']} | {sk['n_complete']} | {sk['n_skipped']} |"
        )
    lines.append("")
    lines.append("## 規則摘要")
    lines.append("")
    lines.append("- **Minervini**: Stage2 Template 7/7 + 10日內 RSI14 曾<35 + MACD 金叉或動能轉強")
    lines.append("- **Tomas**: 價>50/200EMA、20>50EMA、回測 reclaim 20EMA、RSI14 40–60 轉強、RS↑、量>均量")
    lines.append("- **Connors**: 價>200SMA 且 <5SMA + RSI2<10")
    lines.append("- **200EMA+Vol**: 價>200EMA + RSI14<40 + 量>1.5×20均量")
    lines.append("- **OOS Hard Close**: IX0001>200SMA 才進場 + 個股>200SMA + RSI14<40")
    lines.append("- **L1c baseline**: Mom20 Top30（對照組）")
    return "\n".join(lines)
