#!/usr/bin/env python3
"""1m ORB Phase 2 standalone backtest · H-TRAJ-6…9 pre-registered variants.

Topic: intraday-1m-path-predictor (config/research.yaml)
Phase: 2 – tradeable edge validation (IS/OOS split)

Pre-registered hypotheses:
  H-TRAJ-6: High-vol ORB (range≥0.3% + bullish + RR=2.0) OOS avg_net > 0
             → already rejected as CA variant (OOS n=127 avg_net=−0.138%)
  H-TRAJ-7: + P-C chop filter (first-bar range / ATR_5d < 0.8 excluded)
             → expected OOS improvement > 0.15pp vs H-TRAJ-6
  H-TRAJ-8: + I36 spike exit (ext≥3% + VWAP loss → early exit instead of EOD)
             → expected OOS improvement > 0.10pp vs H-TRAJ-6
  H-TRAJ-9: + VWAP gate (entry bar close > VWAP required)
             → expected OOS win rate improvement > 3pp, n reduction < 40%

Universe: stock_kbar_1m · source=finmind · high-vol subset (first-bar range median ≥ 0.8%)
Cost: 0.585% round-trip
IS/OOS split: 70/30 by date (≥ 80 OOS trades required to count as pass-eligible)
Pass threshold: OOS avg_net > 0 AND n_oos ≥ 80
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "stocks.db"
COST = 0.00585
MIN_STOCK_DAYS = 60
HIGH_VOL_THRESHOLD = 0.008  # first-bar range median ≥ 0.8%
RR = 2.0
MIN_RANGE_PCT = 0.003
OOS_MIN_N = 80


# ── Data loading ───────────────────────────────────────────────────────────────

def _load_universe() -> list[str]:
    con = sqlite3.connect(DB)
    df = pd.read_sql(
        """
        SELECT stock_id, COUNT(DISTINCT trade_date) as nd
        FROM stock_kbar_1m WHERE source='finmind'
        GROUP BY stock_id HAVING nd >= ?
        ORDER BY nd DESC
        """,
        con,
        params=(MIN_STOCK_DAYS,),
    )
    con.close()
    return df["stock_id"].tolist()


def _load_1m(stock_id: str) -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql(
        """
        SELECT trade_date, minute, open, high, low, close, volume
        FROM stock_kbar_1m WHERE stock_id=? AND source='finmind'
        ORDER BY trade_date, minute
        """,
        con,
        params=(stock_id,),
    )
    con.close()
    return df[(df["minute"] >= "09:00:00") & (df["minute"] <= "13:30:00")]


def _load_daily(stock_id: str) -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql(
        "SELECT trade_date, high, low, close FROM stock_daily_bars WHERE stock_id=? ORDER BY trade_date",
        con,
        params=(stock_id,),
    )
    con.close()
    return df.set_index("trade_date")


# ── Indicators ────────────────────────────────────────────────────────────────

def _first_bar_range_pct(day: pd.DataFrame) -> float:
    ob = day.sort_values("minute").iloc[0]
    if ob["open"] > 0:
        return (ob["high"] - ob["low"]) / ob["open"]
    return 0.0


def _vwap(day: pd.DataFrame) -> pd.Series:
    """1m VWAP anchored at session open."""
    day = day.sort_values("minute").copy()
    typical = (day["high"] + day["low"] + day["close"]) / 3
    vol = day["volume"].fillna(0)
    cum_pv = (typical * vol).cumsum()
    cum_v = vol.cumsum().replace(0, np.nan)
    return cum_pv / cum_v


def _atr5(daily: pd.DataFrame, date: str) -> float:
    """5-day ATR as % of close for chop filter."""
    idx = daily.index.get_loc(date) if date in daily.index else -1
    if idx < 5:
        return np.nan
    window = daily.iloc[idx - 5 : idx]
    ranges = (window["high"] - window["low"]) / window["close"]
    return float(ranges.mean())


# ── Strategy core ──────────────────────────────────────────────────────────────

def _orb_trade(
    day: pd.DataFrame,
    rr: float,
    min_range_pct: float,
    vwap_gate: bool = False,
    spike_exit: bool = False,
) -> float | None:
    """
    Run 1m ORB on a single session.

    Rules:
      - Opening range = first 1m bar (09:00)
      - Must be bullish (close > open) with range ≥ min_range_pct of open
      - Entry: first bar whose high breaks OR_high (trigger = OR_high)
      - Stop: OR_low, Target: entry + rr * range
      - vwap_gate: entry bar close must be > VWAP at that point
      - spike_exit: if price extends ≥ 3% from entry AND close < VWAP → exit at close

    Returns gross return (entry = OR_high) or None if no trade.
    """
    day = day.sort_values("minute").reset_index(drop=True)
    if len(day) < 8:
        return None

    ob = day.iloc[0]
    or_h, or_l = ob["high"], ob["low"]
    or_r = or_h - or_l
    if or_r <= 0 or ob["open"] <= 0 or or_r / ob["open"] < min_range_pct:
        return None
    if ob["close"] <= ob["open"]:
        return None

    vwap_vals = _vwap(day) if (vwap_gate or spike_exit) else None

    entry = stop = target = None
    for i, bar in day.iloc[1:].iterrows():
        if bar["minute"] > "13:00:00":
            break
        if entry is None:
            if bar["high"] > or_h:
                if vwap_gate and vwap_vals is not None:
                    if bar["close"] < vwap_vals.iloc[i]:
                        continue
                entry = or_h
                stop = or_l
                target = or_h + rr * or_r
        else:
            # I36-style spike exit: price ran ≥3% AND now below VWAP
            if spike_exit and vwap_vals is not None:
                spike_pct = (bar["high"] - entry) / entry
                if spike_pct >= 0.03 and bar["close"] < vwap_vals.iloc[i]:
                    return (bar["close"] - entry) / entry
            if bar["low"] <= stop:
                return (stop - entry) / entry
            if bar["high"] >= target:
                return (target - entry) / entry

    if entry is not None:
        return (day.iloc[-1]["close"] - entry) / entry
    return None


# ── High-vol universe filter ──────────────────────────────────────────────────

def _high_vol_stocks(universe: list[str]) -> list[str]:
    result = []
    for sid in universe:
        try:
            k = _load_1m(sid)
            if len(k) < 500:
                continue
            medians = []
            for _, day in k.groupby("trade_date"):
                r = _first_bar_range_pct(day)
                if r > 0:
                    medians.append(r)
            if medians and np.median(medians) >= HIGH_VOL_THRESHOLD:
                result.append(sid)
        except Exception:
            pass
    return result


# ── Backtest runner ────────────────────────────────────────────────────────────

def _run_variant(
    stocks: list[str],
    label: str,
    vwap_gate: bool = False,
    spike_exit: bool = False,
    chop_filter: bool = False,
) -> dict:
    trades: list[dict] = []

    for sid in stocks:
        try:
            k = _load_1m(sid)
            daily = _load_daily(sid) if chop_filter else None

            for date, day in k.groupby("trade_date"):
                # H-TRAJ-7 chop filter: skip sessions where first-bar range < 0.7 × ATR_5d
                if chop_filter and daily is not None:
                    atr = _atr5(daily, date)
                    if not np.isnan(atr):
                        fb_range = _first_bar_range_pct(day)
                        if fb_range < 0.7 * atr:
                            continue

                gross = _orb_trade(day, rr=RR, min_range_pct=MIN_RANGE_PCT,
                                   vwap_gate=vwap_gate, spike_exit=spike_exit)
                if gross is not None:
                    trades.append({"date": date, "stock": sid, "gross": gross})

        except Exception:
            pass

    return _summarize(trades, label)


def _summarize(trades: list[dict], label: str) -> dict:
    if not trades:
        return {"label": label, "n": 0, "verdict": "NO TRADES", "is": {}, "oos": {}}

    df = pd.DataFrame(trades).sort_values("date").reset_index(drop=True)
    df["net"] = df["gross"] - COST
    split_idx = int(len(df) * 0.7)
    split_date = df.iloc[split_idx]["date"]
    is_ = df[df["date"] < split_date]
    oos = df[df["date"] >= split_date]

    def _stats(t: pd.DataFrame) -> dict:
        if len(t) == 0:
            return {}
        net = t["net"]
        return {
            "n": len(t),
            "date_start": t["date"].iloc[0],
            "date_end": t["date"].iloc[-1],
            "win_rate": round(float((net > 0).mean() * 100), 1),
            "avg_gross": round(float(t["gross"].mean() * 100), 3),
            "avg_net": round(float(net.mean() * 100), 3),
            "sharpe": round(float(net.mean() / (net.std() + 1e-9) * (252 ** 0.5)), 2),
        }

    is_s = _stats(is_)
    oos_s = _stats(oos)

    n_oos = oos_s.get("n", 0)
    avg_net_oos = oos_s.get("avg_net", -99)
    verdict = "PASS" if n_oos >= OOS_MIN_N and avg_net_oos > 0 else "FAIL"

    return {
        "label": label,
        "n": len(df),
        "split_date": split_date,
        "is": is_s,
        "oos": oos_s,
        "verdict": verdict,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("H-TRAJ-6…9 · 1m ORB Phase 2 standalone backtest")
    print(f"DB: {DB}")
    print(f"Cost: {COST*100:.3f}% RT · Pass: OOS avg_net > 0 AND n ≥ {OOS_MIN_N}\n")

    print("Loading universe...")
    universe = _load_universe()
    print(f"  Total eligible stocks: {len(universe)}")

    print(f"Filtering high-vol stocks (first-bar range median ≥ {HIGH_VOL_THRESHOLD*100:.1f}%)...")
    hv = _high_vol_stocks(universe)
    print(f"  High-vol stocks: {len(hv)}\n")

    variants = [
        # H-TRAJ-6: baseline ORB (reference only – already rejected as CA)
        dict(label="H6 · ORB baseline (high-vol)", vwap_gate=False, spike_exit=False, chop_filter=False),
        # H-TRAJ-7: + chop session filter
        dict(label="H7 · ORB + chop filter",       vwap_gate=False, spike_exit=False, chop_filter=True),
        # H-TRAJ-8: + I36 spike exit
        dict(label="H8 · ORB + spike exit",         vwap_gate=False, spike_exit=True,  chop_filter=False),
        # H-TRAJ-9: + VWAP gate on entry
        dict(label="H9 · ORB + VWAP gate",          vwap_gate=True,  spike_exit=False, chop_filter=False),
    ]

    results = []
    for v in variants:
        label = v["label"]
        print(f"Running {label}...")
        r = _run_variant(
            hv,
            label=label,
            vwap_gate=v["vwap_gate"],
            spike_exit=v["spike_exit"],
            chop_filter=v["chop_filter"],
        )
        results.append(r)

    # ── Report ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 90)
    print(f"{'Variant':<35} {'n_total':>7}  {'IS avg_net':>10}  {'OOS n':>6}  {'OOS avg_net':>11}  {'OOS sh':>7}  verdict")
    print("-" * 90)
    for r in results:
        flag = "✅" if r["verdict"] == "PASS" else "❌"
        is_ = r.get("is", {})
        oos = r.get("oos", {})
        print(
            f"  {flag} {r['label']:<33}"
            f"  {r['n']:>6}"
            f"  {is_.get('avg_net', float('nan')):>+10.3f}%"
            f"  {oos.get('n', 0):>6}"
            f"  {oos.get('avg_net', float('nan')):>+11.3f}%"
            f"  {oos.get('sharpe', float('nan')):>7.2f}"
            f"  {r['verdict']}"
        )
    print("=" * 90)

    print("\nHypothesis verdicts:")
    hyp_map = {
        "H6": ("H-TRAJ-6", "Already rejected as CA (OOS n=127 avg_net=−0.138%). Reference only."),
        "H7": ("H-TRAJ-7", "OOS improvement > 0.15pp vs H6 baseline"),
        "H8": ("H-TRAJ-8", "OOS improvement > 0.10pp vs H6 baseline"),
        "H9": ("H-TRAJ-9", "OOS win rate +3pp vs H6 AND n reduction < 40%"),
    }
    h6_net = next((r["oos"].get("avg_net") for r in results if "H6" in r["label"]), None)
    for r in results:
        key = r["label"].split("·")[0].strip()
        hid, threshold = hyp_map.get(key, ("?", "?"))
        oos = r.get("oos", {})
        if key == "H6":
            print(f"  {hid}: {threshold}")
            continue
        if key == "H7" and h6_net is not None:
            delta = (oos.get("avg_net") or 0) - h6_net
            met = delta > 0.15
            print(f"  {hid}: delta vs H6 = {delta:+.3f}pp  (threshold >0.15pp) → {'✅' if met else '❌'}")
        elif key == "H8" and h6_net is not None:
            delta = (oos.get("avg_net") or 0) - h6_net
            met = delta > 0.10
            print(f"  {hid}: delta vs H6 = {delta:+.3f}pp  (threshold >0.10pp) → {'✅' if met else '❌'}")
        elif key == "H9":
            h6_n = next((r2["oos"].get("n") for r2 in results if "H6" in r2["label"]), None)
            h9_n = oos.get("n") or 0
            h9_wr = oos.get("win_rate") or 0
            h6_wr = next((r2["oos"].get("win_rate") for r2 in results if "H6" in r2["label"]), None)
            wr_delta = h9_wr - (h6_wr or 0) if h6_wr else float("nan")
            n_ratio = h9_n / h6_n if h6_n else float("nan")
            met = wr_delta > 3 and n_ratio >= 0.60
            print(
                f"  {hid}: win_rate delta = {wr_delta:+.1f}pp  n_ratio = {n_ratio:.2f}"
                f"  (need >3pp AND n≥60%) → {'✅' if met else '❌'}"
            )


if __name__ == "__main__":
    main()
