#!/usr/bin/env python3
"""Chip↔Price divergence v2 · market-relative, cumulative-line, expert-protocol.

Fixes the blind spots of v1 (`run_2492_chip_price_divergence.py`):
  B1  v1 compared day-to-day Δchip vs Δprice. Practitioners' 籌碼背離 compares a
      CUMULATIVE chip line against PRICE STRUCTURE (price makes new low, chip line
      refuses to) — an OBV/A-D style swing comparison. v2 uses the cumulative line.
  B2  v1 ignored the in-repo rule (skill `chip-relative-vs-market`): institutional
      flow is largely market-wide. v2 de-means chip vs the cross-sectional market
      tone and measures EXCESS return vs a market proxy.
  B3  v1 entered AT the divergence. Practitioners explicitly say to wait for the
      divergence to END plus 量增價漲 confirmation. v2 tests both entries.
  B4  v1 was one melt-up stock with ~130 OOS days. v2 runs a 167-stock long-history
      universe (2492 kept as a deep-dive slice).
  B5  v1 used a single 3d chip window. Literature's most robust institutional-flow
      fact is PERSISTENCE, so v2 tests consecutive-day persistence explicitly.
  B6  v1 added no second chip dimension. v2 adds margin (融資餘額) for the
      "chip up ∧ margin down = 主力鎖碼" case.

Protocol
  - Signal day T uses only data with date ≤ T.
  - Market proxy = cross-sectional median close-to-close return of the universe.
  - Outcome = EXCESS forward return (stock − market proxy), from T close over H days.
  - IS = [IS_START, OOS); OOS = [OOS, end]. OOS is read ONCE per frozen recipe.

Out: reports/research/chip-overlays/divergence_v2/
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "research" / "chip-overlays" / "divergence_v2"
SOURCE = "finmind"
MIN_BARS = 1500
IS_START = "2019-01-01"
OOS = "2026-01-01"
HORIZONS = (5, 10, 20)
PRIMARY_H = 20  # practitioners' stated hold for chip-concentration entries
STRUCT_WIN = 20  # window for "new low / new high" structure
ABS_WIN = 20  # chip normalisation window
DEEP_DIVE_SID = "2492"
GATE_EXCESS_PP = 1.0
GATE_N = 100


def load_universe() -> pd.DataFrame:
    uri = f"file:{DB.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        sids = [
            r[0]
            for r in conn.execute(
                "SELECT stock_id FROM stock_daily_bars WHERE source=? "
                "GROUP BY stock_id HAVING COUNT(*)>=? ",
                (SOURCE, MIN_BARS),
            )
        ]
        ph = ",".join("?" * len(sids))
        bars = pd.read_sql_query(
            f"SELECT stock_id, trade_date, close, volume FROM stock_daily_bars "
            f"WHERE source=? AND stock_id IN ({ph}) ORDER BY stock_id, trade_date",
            conn,
            params=[SOURCE, *sids],
        )
        inst = pd.read_sql_query(
            f"SELECT stock_id, trade_date, "
            f"COALESCE(foreign_net,0)+COALESCE(investment_trust_net,0) AS ft "
            f"FROM stock_institutional_daily WHERE source=? AND stock_id IN ({ph})",
            conn,
            params=[SOURCE, *sids],
        )
        margin = pd.read_sql_query(
            f"SELECT stock_id, trade_date, margin_balance FROM stock_margin_daily "
            f"WHERE stock_id IN ({ph})",
            conn,
            params=sids,
        )
    for d in (bars, inst, margin):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    df = bars.merge(inst, on=["stock_id", "trade_date"], how="left")
    df = df.merge(margin, on=["stock_id", "trade_date"], how="left")
    return df.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    """Per-stock causal features + cross-sectional market de-meaning."""
    out = df.copy()
    g = out.groupby("stock_id", sort=False)
    out["ret1"] = g["close"].pct_change() * 100.0

    # market proxy = cross-sectional median daily return
    mkt = out.groupby("trade_date")["ret1"].median().rename("mkt_ret1")
    out = out.merge(mkt, on="trade_date", how="left")

    # chip intensity normalised by the stock's own recent institutional activity
    g = out.groupby("stock_id", sort=False)
    out["abs_win"] = g["ft"].transform(
        lambda s: s.abs().rolling(ABS_WIN, min_periods=10).sum()
    )
    out["ft_norm"] = out["ft"] / out["abs_win"].replace(0, np.nan)

    # market chip tone = cross-sectional median of ft_norm that day (B2 fix)
    tone = out.groupby("trade_date")["ft_norm"].median().rename("mkt_ft_norm")
    out = out.merge(tone, on="trade_date", how="left")
    out["ft_rel"] = out["ft_norm"] - out["mkt_ft_norm"]

    g = out.groupby("stock_id", sort=False)
    # B1 fix: cumulative chip line (OBV-style) built from market-relative intensity
    out["chip_line"] = g["ft_rel"].transform(lambda s: s.fillna(0).cumsum())
    # structure: is today a STRUCT_WIN low/high for price vs for the chip line?
    out["px_min"] = g["close"].transform(
        lambda s: s.rolling(STRUCT_WIN, min_periods=STRUCT_WIN).min()
    )
    out["px_max"] = g["close"].transform(
        lambda s: s.rolling(STRUCT_WIN, min_periods=STRUCT_WIN).max()
    )
    out["chip_min"] = g["chip_line"].transform(
        lambda s: s.rolling(STRUCT_WIN, min_periods=STRUCT_WIN).min()
    )
    out["chip_max"] = g["chip_line"].transform(
        lambda s: s.rolling(STRUCT_WIN, min_periods=STRUCT_WIN).max()
    )
    out["px_at_low"] = out["close"] <= out["px_min"] * 1.0001
    out["px_at_high"] = out["close"] >= out["px_max"] * 0.9999
    out["chip_at_low"] = out["chip_line"] <= out["chip_min"] + 1e-12
    out["chip_at_high"] = out["chip_line"] >= out["chip_max"] - 1e-12

    # B5 fix: persistence of relative institutional buying
    out["persist3"] = g.apply(
        lambda x: (x["ft_rel"] > 0).rolling(3, min_periods=3).sum(), include_groups=False
    ).reset_index(level=0, drop=True)
    out["persist5"] = g.apply(
        lambda x: (x["ft_rel"] > 0).rolling(5, min_periods=5).sum(), include_groups=False
    ).reset_index(level=0, drop=True)

    # B6 fix: margin dimension (融資餘額 5d change). Falling margin + chip up = 鎖碼
    out["margin_chg5"] = g["margin_balance"].transform(
        lambda s: s / s.shift(5) - 1.0
    )

    # volume confirmation (量增) for the expert entry
    out["vol_ma5"] = g["volume"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    out["vol_surge"] = out["volume"] > out["vol_ma5"] * 1.5

    # forward EXCESS return vs market proxy (B2 fix), from T close
    mkt_cum = (1.0 + mkt / 100.0).cumprod().rename("mkt_cum")
    out = out.merge(mkt_cum, on="trade_date", how="left")
    g = out.groupby("stock_id", sort=False)
    for h in HORIZONS:
        fwd_stock = (g["close"].shift(-h) / out["close"] - 1.0) * 100.0
        fwd_mkt = (out.groupby("stock_id", sort=False)["mkt_cum"].shift(-h) / out["mkt_cum"] - 1.0) * 100.0
        out[f"xfwd{h}"] = fwd_stock - fwd_mkt
    return out


def build_signals(df: pd.DataFrame) -> dict[str, pd.Series]:
    """PIT boolean signals. All use only date ≤ T."""
    d = df
    # bullish structural divergence: price at 20d low, chip line refuses to be
    bull_div = d["px_at_low"] & (~d["chip_at_low"])
    bear_div = d["px_at_high"] & (~d["chip_at_high"])
    # divergence "ended" + 量增價漲 confirmation (expert entry, B3 fix)
    div_recent = (
        d.groupby("stock_id", sort=False)["px_at_low"]
        .transform(lambda s: s.rolling(10, min_periods=1).max())
        .astype(bool)
    )
    chip_ok = ~d["chip_at_low"]
    expert_entry = (
        div_recent & chip_ok & (~d["px_at_low"]) & d["vol_surge"] & (d["ret1"] > 0)
    )
    return {
        "baseline_all": pd.Series(True, index=d.index),
        "bull_divergence_at_signal": bull_div,
        "bear_divergence_at_signal": bear_div,
        "expert_entry_after_divergence": expert_entry,
        "persistence3_rel_buy": d["persist3"] >= 3,
        "persistence5_rel_buy": d["persist5"] >= 5,
        "lockup_chip_up_margin_down": (d["persist3"] >= 3) & (d["margin_chg5"] < 0),
        "chip_up_price_down_naive_v1": (d["ft_rel"] > 0) & (d["ret1"] < 0),
        # ── price-only controls: is the "chip" signal just price momentum? ──
        "CTRL_price_at_20d_low_only": d["px_at_low"],
        "CTRL_price_at_20d_high_only": d["px_at_high"],
        "CTRL_price_down_day_only": d["ret1"] < 0,
        "CTRL_vol_surge_up_day_only": d["vol_surge"] & (d["ret1"] > 0),
        # ── matched contrast INSIDE price-at-low: chip refuses vs chip confirms ──
        "MATCH_low_chip_refuses": d["px_at_low"] & (~d["chip_at_low"]),
        "MATCH_low_chip_also_low": d["px_at_low"] & d["chip_at_low"],
        # ── matched contrast INSIDE down days: chip up vs chip down ──
        "MATCH_down_chip_up": (d["ret1"] < 0) & (d["ft_rel"] > 0),
        "MATCH_down_chip_down": (d["ret1"] < 0) & (d["ft_rel"] <= 0),
    }


def evaluate(df: pd.DataFrame, sigs: dict[str, pd.Series], label: str) -> dict:
    res: dict = {}
    base = {h: df[f"xfwd{h}"].dropna() for h in HORIZONS}
    for name, sig in sigs.items():
        m = sig.fillna(False) if sig.dtype == bool else sig.astype("boolean").fillna(False)
        row: dict = {}
        for h in HORIZONS:
            x = df.loc[m, f"xfwd{h}"].dropna()
            if len(x) == 0:
                row[f"xfwd{h}"] = {"n": 0}
                continue
            row[f"xfwd{h}"] = {
                "n": int(len(x)),
                "med": round(float(x.median()), 3),
                "excess_vs_all": round(float(x.median() - base[h].median()), 3),
                "winrate": round(float((x > 0).mean() * 100.0), 1),
            }
        res[name] = row
    res["_split"] = label
    res["_baseline_med"] = {f"xfwd{h}": round(float(base[h].median()), 3) for h in HORIZONS}
    return res


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    raw = load_universe()
    df = add_features(raw)
    sigs = build_signals(df)

    is_mask = (df["trade_date"] >= IS_START) & (df["trade_date"] < OOS)
    oos_mask = df["trade_date"] >= OOS

    payload: dict = {
        "universe": {
            "n_stocks": int(df["stock_id"].nunique()),
            "start": str(df["trade_date"].min().date()),
            "end": str(df["trade_date"].max().date()),
            "rows": int(len(df)),
        },
        "protocol": {
            "is_start": IS_START,
            "oos": OOS,
            "struct_window": STRUCT_WIN,
            "abs_window": ABS_WIN,
            "primary_h": PRIMARY_H,
            "outcome": "excess forward return vs cross-sectional median market proxy",
            "gate": {"excess_pp": GATE_EXCESS_PP, "n": GATE_N},
        },
        "IS": evaluate(df[is_mask], {k: v[is_mask] for k, v in sigs.items()}, "IS"),
        "OOS": evaluate(df[oos_mask], {k: v[oos_mask] for k, v in sigs.items()}, "OOS"),
    }

    # 2492 deep-dive slice under the identical spec
    deep = df["stock_id"] == DEEP_DIVE_SID
    payload["deep_dive_2492"] = {
        "IS": evaluate(
            df[deep & is_mask], {k: v[deep & is_mask] for k, v in sigs.items()}, "IS"
        ),
        "OOS": evaluate(
            df[deep & oos_mask], {k: v[deep & oos_mask] for k, v in sigs.items()}, "OOS"
        ),
    }

    # Verdict requires the SAME sign in IS and OOS (regime-artifact guard) plus
    # beating its matched price-only control, otherwise it is just momentum.
    control_of = {
        "bull_divergence_at_signal": "CTRL_price_at_20d_low_only",
        "bear_divergence_at_signal": "CTRL_price_at_20d_high_only",
        "chip_up_price_down_naive_v1": "CTRL_price_down_day_only",
        "MATCH_low_chip_refuses": "CTRL_price_at_20d_low_only",
        "MATCH_down_chip_up": "CTRL_price_down_day_only",
        "expert_entry_after_divergence": "CTRL_vol_surge_up_day_only",
    }
    verdicts = {}
    key = f"xfwd{PRIMARY_H}"
    for name in sigs:
        if name == "baseline_all" or name.startswith("CTRL_"):
            continue
        i = payload["IS"][name][key]
        o = payload["OOS"][name][key]
        ctrl = control_of.get(name)
        lift_vs_ctrl = None
        if ctrl:
            lift_vs_ctrl = {
                "IS": round(i.get("med", np.nan) - payload["IS"][ctrl][key].get("med", np.nan), 3),
                "OOS": round(o.get("med", np.nan) - payload["OOS"][ctrl][key].get("med", np.nan), 3),
            }
        n_ok = o.get("n", 0) >= GATE_N and i.get("n", 0) >= GATE_N
        sign_ok = (i.get("excess_vs_all", 0) > 0) and (o.get("excess_vs_all", 0) > 0)
        size_ok = o.get("excess_vs_all", -99) >= GATE_EXCESS_PP
        ctrl_ok = lift_vs_ctrl is None or (
            lift_vs_ctrl["IS"] > 0 and lift_vs_ctrl["OOS"] > 0
        )
        if not n_ok:
            v = "INSUFFICIENT_N"
        elif not sign_ok:
            v = "REJECT_IS_OOS_SIGN_FLIP"
        elif not ctrl_ok:
            v = "REJECT_NO_LIFT_VS_PRICE_CONTROL"
        elif not size_ok:
            v = "WEAK_BELOW_GATE"
        else:
            v = "KEEP"
        verdicts[name] = {
            "IS": i,
            "OOS": o,
            "control": ctrl,
            "lift_vs_control": lift_vs_ctrl,
            "verdict": v,
        }
    payload["verdicts_primary_h"] = verdicts

    # Robustness for the two matched contrasts that survived the controls:
    # is the spread stable year-by-year and broad across stocks, or one-off?
    contrasts = {
        "price_at_20d_low": ("MATCH_low_chip_refuses", "MATCH_low_chip_also_low"),
        "down_day": ("MATCH_down_chip_up", "MATCH_down_chip_down"),
    }
    rob: dict = {}
    df_y = df.assign(year=df["trade_date"].dt.year)
    for label, (a, b) in contrasts.items():
        ma, mb = sigs[a].fillna(False), sigs[b].fillna(False)
        by_year = {}
        for y, chunk in df_y.groupby("year"):
            if y < int(IS_START[:4]):
                continue
            xa = chunk.loc[ma.reindex(chunk.index, fill_value=False), f"xfwd{PRIMARY_H}"].dropna()
            xb = chunk.loc[mb.reindex(chunk.index, fill_value=False), f"xfwd{PRIMARY_H}"].dropna()
            if len(xa) < 20 or len(xb) < 20:
                continue
            by_year[int(y)] = {
                "spread": round(float(xa.median() - xb.median()), 3),
                "n_a": int(len(xa)),
                "n_b": int(len(xb)),
            }
        spreads = [v["spread"] for v in by_year.values()]
        # breadth: fraction of stocks with positive spread (need enough obs each)
        pos = tot = 0
        for sid, chunk in df.groupby("stock_id"):
            xa = chunk.loc[ma.reindex(chunk.index, fill_value=False), f"xfwd{PRIMARY_H}"].dropna()
            xb = chunk.loc[mb.reindex(chunk.index, fill_value=False), f"xfwd{PRIMARY_H}"].dropna()
            if len(xa) < 30 or len(xb) < 30:
                continue
            tot += 1
            pos += int(xa.median() > xb.median())
        rob[label] = {
            "by_year": by_year,
            "years_positive": f"{sum(s > 0 for s in spreads)}/{len(spreads)}",
            "median_year_spread": round(float(np.median(spreads)), 3) if spreads else None,
            "breadth_stocks_positive": f"{pos}/{tot}",
            "breadth_pct": round(100.0 * pos / tot, 1) if tot else None,
        }
    payload["robustness"] = rob
    print("\n--- robustness (spread of matched contrast, xfwd20) ---")
    for label, r in rob.items():
        print(
            f"  {label:18s} years+={r['years_positive']} med_year_spread={r['median_year_spread']} "
            f"breadth={r['breadth_stocks_positive']} ({r['breadth_pct']}%)"
        )
        print(f"      by_year={ {y: v['spread'] for y, v in r['by_year'].items()} }")

    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[divergence v2] wrote {OUT / 'results.json'}")
    print(f"universe: {payload['universe']}")
    for name, v in verdicts.items():
        i, o = v["IS"], v["OOS"]
        lift = v["lift_vs_control"]
        lift_s = f"lift IS={lift['IS']:+.2f} OOS={lift['OOS']:+.2f}" if lift else "no control"
        print(
            f"  {name:34s} IS_ex={i.get('excess_vs_all','—'):>6} "
            f"OOS_ex={o.get('excess_vs_all','—'):>6} n={o.get('n',0):5d} "
            f"{lift_s:28s} → {v['verdict']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
