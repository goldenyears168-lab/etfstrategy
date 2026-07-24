#!/usr/bin/env python3
"""Faithful FinMind InstitutionalInvestorsFollower replica (Abnormal Peak).

Source (kaneyen/FinMind Strategies/InstitutionalInvestorsFollower.py):
  lag=10, threshold=3, influence=0.35
  signal_info +1 = abnormal BUY peak → trade signal -1 (fade / sell)
  signal_info -1 = abnormal SELL peak → trade signal +1 (contrarian buy)

Protocol (professional event study + simple long/flat sleeve):
  - Signal on close of T (chip known EOD); enter T+1 open; exit T+1 close (t1oc)
    and optional hold to T+5 close from T+1 open.
  - Universe: liquid stocks with chip in DB (ADV20≥1e7) OR holdings / expert.
  - Benchmark: equal-weight same-universe day returns (not 0050 — sparse history).

Out: reports/research/chip-overlays/faithful_finmind_follower/
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "chip-overlays" / "faithful_finmind_follower"
REGISTRY = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "EVAL_REGISTRY.json"
)

START, WARMUP, END = "2024-07-01", "2024-01-01", "2026-07-24"
OOS = "2026-01-01"
LAG, THRESH, INFLUENCE = 10, 3.0, 0.35


def detect_abnormal_peak(
    y: np.ndarray, lag: int = LAG, threshold: float = THRESH, influence: float = INFLUENCE
) -> np.ndarray:
    """Exact FinMind loop (returns +1 buy-peak, -1 sell-peak, 0 else)."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    signals = np.zeros(n)
    if n <= lag:
        return signals
    filtered = np.array(y, dtype=float, copy=True)
    avg = np.zeros(n)
    std = np.zeros(n)
    avg[lag - 1] = float(np.mean(y[:lag]))
    std[lag - 1] = float(np.std(y[:lag]))
    for i in range(lag, n):
        if std[i - 1] > 0 and abs(y[i] - avg[i - 1]) > threshold * std[i - 1]:
            signals[i] = 1.0 if y[i] > avg[i - 1] else -1.0
            filtered[i] = influence * y[i] + (1.0 - influence) * filtered[i - 1]
        else:
            signals[i] = 0.0
            filtered[i] = y[i]
        avg[i] = float(np.mean(filtered[i - lag + 1 : i + 1]))
        std[i] = float(np.std(filtered[i - lag + 1 : i + 1]))
    return signals


def adopted_ids() -> list[str]:
    raw = json.loads(REGISTRY.read_text(encoding="utf-8"))
    return sorted(
        {
            r["stock_id"]
            for r in raw.get("stocks", [])
            if r.get("status") == "adopted" and str(r.get("stock_id", "")).isdigit()
        }
    )


def liquid_ids(min_adv: float = 1e7, min_days: int = 200) -> list[str]:
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    q = """
    SELECT stock_id FROM (
      SELECT b.stock_id,
             AVG(CASE WHEN amount>0 THEN amount ELSE close*volume END) AS adv
      FROM stock_daily_bars b
      WHERE source='finmind' AND trade_date>=? AND trade_date<=?
      GROUP BY stock_id
      HAVING COUNT(*)>=? AND adv>=?
    )
    """
    ids = [
        r[0]
        for r in conn.execute(q, (START, END, min_days, min_adv)).fetchall()
        if str(r[0]).isdigit() and len(str(r[0])) == 4
    ]
    conn.close()
    return sorted(ids)


def load_panel(sids: list[str]) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    ph = ",".join("?" * len(sids))
    bars = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,open,close,volume,amount
            FROM stock_daily_bars WHERE source='finmind' AND stock_id IN ({ph})
            AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*sids, WARMUP, END],
    )
    inst = pd.read_sql_query(
        f"""SELECT stock_id,trade_date,
                   foreign_net+investment_trust_net+dealer_self_net AS diff
            FROM stock_institutional_daily WHERE source='finmind'
            AND stock_id IN ({ph}) AND trade_date>=? AND trade_date<=?""",
        conn,
        params=[*sids, WARMUP, END],
    )
    conn.close()
    for d in (bars, inst):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    df = bars.merge(inst, on=["stock_id", "trade_date"], how="left")
    df["diff"] = df["diff"].fillna(0.0)
    df = df.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    return df


def annotate(df: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for sid, g in df.groupby("stock_id", sort=False):
        g = g.copy()
        peak = detect_abnormal_peak(g["diff"].to_numpy())
        g["peak"] = peak
        # FinMind invert
        g["signal"] = 0
        g.loc[g["peak"] == 1, "signal"] = -1  # fade buy climax
        g.loc[g["peak"] == -1, "signal"] = 1  # buy sell climax
        o1 = g["open"].shift(-1)
        c1 = g["close"].shift(-1)
        c5 = g["close"].shift(-5)
        g["t1oc"] = (c1 / o1 - 1.0) * 100.0
        g["h5_from_o1"] = (c5 / o1 - 1.0) * 100.0
        g["ret1"] = g["close"].pct_change() * 100.0
        parts.append(g)
    out = pd.concat(parts, ignore_index=True)
    for c in ("t1oc", "h5_from_o1"):
        out.loc[out[c].abs() > 35, c] = np.nan
    return out


def eval_universe(df: pd.DataFrame, label: str) -> pd.DataFrame:
    sub = df[df["trade_date"] >= START].copy()
    rows = []
    for split, ss in (
        ("ALL", sub),
        ("IS", sub[sub["trade_date"] < OOS]),
        ("OOS", sub[sub["trade_date"] >= OOS]),
    ):
        base_t1 = float(ss["t1oc"].mean())
        base_h5 = float(ss["h5_from_o1"].mean())
        for arm, mask, hyp in (
            ("fade_after_buy_peak", ss["signal"] == -1, "bear"),  # expect neg t1oc
            ("buy_after_sell_peak", ss["signal"] == 1, "bull"),
            ("any_contrarian_signed", ss["signal"] != 0, "signed"),
        ):
            ev = ss.loc[mask]
            for h, base in (("t1oc", base_t1), ("h5_from_o1", base_h5)):
                x = ev[h].dropna()
                if len(x) < 30:
                    rows.append(
                        dict(
                            universe=label,
                            split=split,
                            arm=arm,
                            horizon=h,
                            n=len(x),
                            mean=np.nan,
                            base=base,
                            excess=np.nan,
                        )
                    )
                    continue
                if arm == "any_contrarian_signed":
                    # signed: signal * return (long when +1, short when -1)
                    signed = (ev["signal"] * ev[h]).dropna()
                    mean = float(signed.mean())
                    excess = mean  # vs 0 for market-neutral style
                else:
                    mean = float(x.mean())
                    excess = mean - base
                rows.append(
                    dict(
                        universe=label,
                        split=split,
                        arm=arm,
                        horizon=h,
                        n=int(len(x) if arm != "any_contrarian_signed" else len(signed)),
                        mean=mean,
                        base=base if arm != "any_contrarian_signed" else 0.0,
                        excess=excess,
                    )
                )
    return pd.DataFrame(rows)


def per_stock_oos(df: pd.DataFrame, label: str) -> pd.DataFrame:
    sub = df[(df["trade_date"] >= OOS)].copy()
    rows = []
    for sid, g in sub.groupby("stock_id"):
        base = g["t1oc"].mean()
        fade = g.loc[g["signal"] == -1, "t1oc"].dropna()
        buy = g.loc[g["signal"] == 1, "t1oc"].dropna()
        rows.append(
            dict(
                universe=label,
                stock_id=sid,
                n_fade=len(fade),
                fade_mean=float(fade.mean()) if len(fade) >= 5 else np.nan,
                fade_ex=(float(fade.mean()) - base) if len(fade) >= 5 else np.nan,
                n_buy=len(buy),
                buy_mean=float(buy.mean()) if len(buy) >= 5 else np.nan,
                buy_ex=(float(buy.mean()) - base) if len(buy) >= 5 else np.nan,
                base_t1oc=float(base),
            )
        )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", choices=("holdings", "expert", "liquid", "all"), default="all")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)

    holdings = ["2327", "3189", "3653", "6505", "8046", "2492"]
    universes: list[tuple[str, list[str]]] = []
    if args.universe in ("holdings", "all"):
        universes.append(("holdings", holdings))
    if args.universe in ("expert", "all"):
        universes.append(("expert", adopted_ids()))
    if args.universe in ("liquid", "all"):
        lids = liquid_ids()
        # cap for runtime: top 300 by recent ADV already filtered
        universes.append(("liquid", lids[:400] if len(lids) > 400 else lids))

    summaries, details = [], []
    for label, sids in universes:
        print(f"load {label} n={len(sids)}")
        panel = annotate(load_panel(sids))
        summaries.append(eval_universe(panel, label))
        details.append(per_stock_oos(panel, label))
        # signal counts
        sub = panel[panel.trade_date >= START]
        print(
            label,
            "buy_peaks",
            int((sub.peak == 1).sum()),
            "sell_peaks",
            int((sub.peak == -1).sum()),
        )

    summary = pd.concat(summaries, ignore_index=True)
    detail = pd.concat(details, ignore_index=True)
    summary.to_csv(OUT / "follower_summary.csv", index=False)
    detail.to_csv(OUT / "follower_per_stock_oos.csv", index=False)
    meta = {
        "protocol": {
            "lag": LAG,
            "threshold": THRESH,
            "influence": INFLUENCE,
            "diff": "F+IT+Dealer_self (Book aggregate; FinMind sums buy-sell all names)",
            "entry": "T+1 open after EOD signal",
            "source": "kaneyen/FinMind InstitutionalInvestorsFollower.py",
        },
        "oos": OOS,
    }
    (OUT / "protocol.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(summary.to_string(index=False))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
