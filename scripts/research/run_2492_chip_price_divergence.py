#!/usr/bin/env python3
"""2492 華新科 · Chip↔Price divergence / regression study (Track 0–3).

Question (user): on down-price days, is the chip move already "offset" into
price, or is there a residual chip→price regression opportunity? Draft idea was
"draw ft3_rel_20 2-day-change line vs MA3 price-change line; price below chip =
cheap = buy".

This runner tests that idea honestly on a single stock with strict PIT and a
frozen IS→OOS protocol. It is diagnostic research, NOT an Order signal.

Tracks
  0  Diagnostics: same-day & lead/lag corr, PIT-β stability, residual autocorr.
  1  H-B state transition × price weakness event study (未過→通過 ∧ 價弱).
  2  H-A PIT rolling-regression residual quintiles (falsify "price<chip=buy").
  3  Down-day "offset vs divergence" classifier (matches user's decision语言).

Protocol
  - Signal day T uses only data with date ≤ T (close, institutional).
  - IS  = [IS_START, OOS)   used to pick ONE frozen recipe per track.
  - OOS = [OOS, end]        evaluated ONCE (no re-tuning).
  - Forward return from T close over H trading days (long-only, gross).

Out: reports/research/chip-overlays/2492_knowledge/chip_price_divergence/
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = (
    ROOT
    / "reports"
    / "research"
    / "chip-overlays"
    / "2492_knowledge"
    / "chip_price_divergence"
)
SID = "2492"
SOURCE = "finmind"
IS_START = "2019-01-01"
OOS = "2026-01-01"
FWD_HORIZONS = (1, 3, 5, 10)
PRIMARY_H = 5
REG_WINDOW = 120
REG_MIN = 60
Z_WINDOW = 60
Z_MIN = 40
GATE_EXCESS_PP = 1.5  # OOS med excess vs all-day baseline to "keep"
GATE_N = 30


def load_panel() -> pd.DataFrame:
    uri = f"file:{DB.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        bars = pd.read_sql_query(
            "SELECT trade_date, close, volume FROM stock_daily_bars "
            "WHERE source=? AND stock_id=? ORDER BY trade_date",
            conn,
            params=[SOURCE, SID],
        )
        inst = pd.read_sql_query(
            "SELECT trade_date, "
            "COALESCE(foreign_net,0)+COALESCE(investment_trust_net,0) AS ft "
            "FROM stock_institutional_daily WHERE source=? AND stock_id=? "
            "ORDER BY trade_date",
            conn,
            params=[SOURCE, SID],
        )
    for d in (bars, inst):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    df = bars.merge(inst, on="trade_date", how="inner").sort_values("trade_date")
    return df.reset_index(drop=True)


def build_features(df: pd.DataFrame, chip_diff: int, price_mode: str) -> pd.DataFrame:
    """Add chip / price features. All rolling ops are causal (use ≤T)."""
    out = df.copy()
    out["FT3"] = out["ft"].rolling(3, min_periods=2).sum()
    out["abs20"] = out["ft"].abs().rolling(20, min_periods=10).sum()
    out["ft3_rel"] = out["FT3"] / out["abs20"].replace(0, np.nan)
    out["confirm"] = out["ft3_rel"] > 0
    out["confirm_t1"] = out["ft3_rel"].shift(1) > 0
    out["new_confirm"] = (~out["confirm_t1"]) & out["confirm"]
    out["lost_confirm"] = out["confirm_t1"] & (~out["confirm"])
    out["d_chip"] = out["ft3_rel"].diff(chip_diff)
    # price change series to compare against chip change
    out["ma3"] = out["close"].rolling(3).mean()
    if price_mode == "ret2":
        out["d_price"] = out["close"].pct_change(2) * 100.0
    elif price_mode == "dma3_2":
        out["d_price"] = (out["ma3"] / out["ma3"].shift(2) - 1.0) * 100.0
    elif price_mode == "dma3_1":
        out["d_price"] = (out["ma3"] / out["ma3"].shift(1) - 1.0) * 100.0
    else:
        raise ValueError(price_mode)
    out["ret2"] = out["close"].pct_change(2) * 100.0
    # rolling z (causal)
    for col in ("d_chip", "d_price"):
        mu = out[col].rolling(Z_WINDOW, min_periods=Z_MIN).mean()
        sd = out[col].rolling(Z_WINDOW, min_periods=Z_MIN).std()
        out[f"z_{col}"] = (out[col] - mu) / sd.replace(0, np.nan)
    # forward returns from T close
    for h in FWD_HORIZONS:
        out[f"fwd{h}"] = (out["close"].shift(-h) / out["close"] - 1.0) * 100.0
    return out


def pit_residual(df: pd.DataFrame) -> pd.DataFrame:
    """Rolling PIT OLS d_price ~ a + b*d_chip using ONLY prior REG_WINDOW rows.

    Residual_T = d_price_T - (a + b*d_chip_T). Negative = price below the level
    the chip move would predict ("cheap vs chip"). Also expose beta for stability.
    """
    out = df.copy()
    resid = np.full(len(out), np.nan)
    beta = np.full(len(out), np.nan)
    d_chip = out["d_chip"].to_numpy()
    d_price = out["d_price"].to_numpy()
    for i in range(len(out)):
        lo = i - REG_WINDOW
        if lo < 0:
            continue
        x = d_chip[lo:i]
        y = d_price[lo:i]
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() < REG_MIN:
            continue
        xx = x[m]
        yy = y[m]
        vx = np.var(xx, ddof=1)
        if vx <= 1e-12:
            continue
        b1 = np.cov(xx, yy, ddof=1)[0, 1] / vx
        b0 = yy.mean() - b1 * xx.mean()
        beta[i] = b1
        xi = d_chip[i]
        yi = d_price[i]
        if np.isfinite(xi) and np.isfinite(yi):
            resid[i] = yi - (b0 + b1 * xi)
    out["reg_beta"] = beta
    out["reg_resid"] = resid
    return out


def spearman(a: pd.Series, b: pd.Series) -> tuple[float, int]:
    s = pd.concat([a, b], axis=1).dropna()
    if len(s) < 30:
        return float("nan"), len(s)
    return float(s.iloc[:, 0].corr(s.iloc[:, 1], method="spearman")), len(s)


def split_masks(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    is_mask = (df["trade_date"] >= IS_START) & (df["trade_date"] < OOS)
    oos_mask = df["trade_date"] >= OOS
    return is_mask, oos_mask


def med_excess(sub: pd.DataFrame, mask: pd.Series, h: int) -> dict:
    """Median forward-h of masked days minus baseline median over same window."""
    base = sub[f"fwd{h}"].dropna()
    x = sub.loc[mask, f"fwd{h}"].dropna()
    if len(x) == 0 or len(base) == 0:
        return {"n": int(len(x)), "med": float("nan"), "excess": float("nan"),
                "winrate": float("nan"), "base_med": float("nan")}
    return {
        "n": int(len(x)),
        "med": float(x.median()),
        "excess": float(x.median() - base.median()),
        "winrate": float((x > 0).mean() * 100.0),
        "base_med": float(base.median()),
    }


# ── Track 0 ─────────────────────────────────────────────────────────────────
def track0_diagnostics(df: pd.DataFrame) -> dict:
    res: dict = {"same_day": {}, "lead_lag": {}, "beta": {}, "resid_autocorr": {}}
    r, n = spearman(df["d_chip"], df["d_price"])
    res["same_day"]["d_chip_vs_d_price"] = {"spearman": r, "n": n}
    r, n = spearman(df["ft3_rel"], df["ret2"])
    res["same_day"]["ft3_rel_vs_ret2"] = {"spearman": r, "n": n}
    # lead/lag: chip[t] vs price-change[t+k]
    for k in range(-3, 4):
        r, n = spearman(df["d_chip"], df["d_price"].shift(-k))
        res["lead_lag"][f"k={k:+d}"] = {"spearman": r, "n": n}
    b = df["reg_beta"].dropna()
    if len(b):
        res["beta"] = {
            "median": float(b.median()),
            "p25": float(b.quantile(0.25)),
            "p75": float(b.quantile(0.75)),
            "frac_positive": float((b > 0).mean()),
        }
    rr = df["reg_resid"].dropna()
    if len(rr) > 30:
        res["resid_autocorr"]["lag1"] = float(rr.autocorr(1))
        res["resid_autocorr"]["lag5"] = float(rr.autocorr(5))
    # variance explained (PIT residual vs raw price change)
    m = df[["d_price", "reg_resid"]].dropna()
    if len(m) > 30:
        res["r2_pit"] = float(1 - np.var(m["reg_resid"]) / np.var(m["d_price"]))
    return res


# ── Track 1 ─────────────────────────────────────────────────────────────────
def track1_state_transition(df: pd.DataFrame) -> dict:
    is_mask, oos_mask = split_masks(df)
    events = {
        "new_confirm_any": df["new_confirm"],
        "new_confirm_price_down": df["new_confirm"] & (df["ret2"] < 0),
        "confirm_continued": df["confirm"] & df["confirm_t1"],
        "lost_confirm": df["lost_confirm"],
        "confirm_and_down2": df["confirm"] & (df["ret2"] < -1),
    }
    out: dict = {"IS": {}, "OOS": {}}
    for split, smask in (("IS", is_mask), ("OOS", oos_mask)):
        sub = df[smask]
        for name, ev in events.items():
            evs = ev[smask].fillna(False)
            out[split][name] = {
                f"fwd{h}": med_excess(sub, evs, h) for h in FWD_HORIZONS
            }
    return out


# ── Track 2 ─────────────────────────────────────────────────────────────────
def track2_residual_quintiles(df: pd.DataFrame) -> dict:
    is_mask, oos_mask = split_masks(df)
    out: dict = {"IS": {}, "OOS": {}}
    for split, smask in (("IS", is_mask), ("OOS", oos_mask)):
        sub = df[smask].dropna(subset=["reg_resid", f"fwd{PRIMARY_H}"]).copy()
        if len(sub) < 25:
            out[split] = {"note": f"thin n={len(sub)}"}
            continue
        try:
            sub["q"] = pd.qcut(sub["reg_resid"], 5, labels=["Q1_cheap", "Q2", "Q3", "Q4", "Q5_rich"])
        except ValueError:
            out[split] = {"note": "qcut failed (ties)"}
            continue
        g = sub.groupby("q", observed=True)[f"fwd{PRIMARY_H}"]
        out[split] = {
            str(k): {"n": int(v.count()), "med": float(v.median()), "mean": float(v.mean())}
            for k, v in g
        }
        # monotonic check: cheap (Q1) should beat rich (Q5) if H-A holds
        q1 = sub.loc[sub["q"] == "Q1_cheap", f"fwd{PRIMARY_H}"].median()
        q5 = sub.loc[sub["q"] == "Q5_rich", f"fwd{PRIMARY_H}"].median()
        out[split]["cheap_minus_rich"] = float(q1 - q5)
    return out


# ── Track 3 ─────────────────────────────────────────────────────────────────
def track3_downday_classifier(df: pd.DataFrame) -> dict:
    """On down days (ret2<0): offset vs divergence vs double-kill → fwd."""
    is_mask, oos_mask = split_masks(df)
    down = df["ret2"] < 0
    labels = {
        "divergence_chip_up": down & (df["d_chip"] > 0),   # price down, chip still rising
        "offset_chip_flat_down": down & (df["d_chip"] <= 0) & (~df["lost_confirm"].fillna(False)),
        "double_kill_lost": down & df["lost_confirm"].fillna(False),
        "divergence_new_confirm": down & df["new_confirm"].fillna(False),
    }
    out: dict = {"IS": {}, "OOS": {}}
    for split, smask in (("IS", is_mask), ("OOS", oos_mask)):
        sub = df[smask]
        down_sub = sub[sub["ret2"] < 0]
        base_down = {
            f"fwd{h}": (float(down_sub[f"fwd{h}"].median()) if down_sub[f"fwd{h}"].notna().any() else float("nan"))
            for h in FWD_HORIZONS
        }
        out[split] = {"_down_baseline": base_down}
        for name, lab in labels.items():
            m = lab[smask].fillna(False)
            out[split][name] = {
                f"fwd{h}": med_excess(sub, m, h) for h in FWD_HORIZONS
            }
    return out


# ── IS sweep → freeze → OOS once ─────────────────────────────────────────────
def sweep_and_freeze(df_raw: pd.DataFrame) -> dict:
    """Small IS grid over (chip_diff, price_mode) for Track1 primary event.

    Objective: IS med excess of `new_confirm_price_down` at PRIMARY_H. Freeze the
    best recipe, then report its OOS once.
    """
    grid_chip = (1, 2)
    grid_price = ("ret2", "dma3_2", "dma3_1")
    is_rows = []
    for cd in grid_chip:
        for pm in grid_price:
            feat = pit_residual(build_features(df_raw, cd, pm))
            is_mask, _ = split_masks(feat)
            sub = feat[is_mask]
            ev = (feat["new_confirm"] & (feat["ret2"] < 0))[is_mask].fillna(False)
            r = med_excess(sub, ev, PRIMARY_H)
            is_rows.append({"chip_diff": cd, "price_mode": pm, **r})
    is_df = pd.DataFrame(is_rows)
    # freeze: require n>=GATE_N in IS, maximize IS excess
    eligible = is_df[is_df["n"] >= GATE_N]
    pick = (eligible if len(eligible) else is_df).sort_values("excess", ascending=False).iloc[0]
    cd = int(pick["chip_diff"])
    pm = str(pick["price_mode"])
    feat = pit_residual(build_features(df_raw, cd, pm))
    is_mask, oos_mask = split_masks(feat)
    ev_is = (feat["new_confirm"] & (feat["ret2"] < 0))[is_mask].fillna(False)
    ev_oos = (feat["new_confirm"] & (feat["ret2"] < 0))[oos_mask].fillna(False)
    frozen = {
        "chip_diff": cd,
        "price_mode": pm,
        "primary_h": PRIMARY_H,
        "gate_excess_pp": GATE_EXCESS_PP,
        "gate_n": GATE_N,
        "IS": med_excess(feat[is_mask], ev_is, PRIMARY_H),
        "OOS": med_excess(feat[oos_mask], ev_oos, PRIMARY_H),
        "IS_grid": is_rows,
    }
    keep = (
        frozen["OOS"]["n"] >= GATE_N
        and np.isfinite(frozen["OOS"]["excess"])
        and frozen["OOS"]["excess"] >= GATE_EXCESS_PP
    )
    frozen["verdict"] = "KEEP" if keep else "REJECT/INSUFFICIENT"
    return frozen


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    df_raw = load_panel()
    # Default features for Track 0/1/2/3 reporting: chip_diff=2, price=dma3_2
    feat = pit_residual(build_features(df_raw, chip_diff=2, price_mode="dma3_2"))
    payload = {
        "stock_id": SID,
        "coverage": {
            "start": str(df_raw["trade_date"].min().date()),
            "end": str(df_raw["trade_date"].max().date()),
            "n": int(len(df_raw)),
        },
        "protocol": {
            "is_start": IS_START,
            "oos": OOS,
            "reg_window": REG_WINDOW,
            "z_window": Z_WINDOW,
            "primary_h": PRIMARY_H,
            "default_chip_diff": 2,
            "default_price_mode": "dma3_2",
        },
        "track0_diagnostics": track0_diagnostics(feat),
        "track1_state_transition": track1_state_transition(feat),
        "track2_residual_quintiles": track2_residual_quintiles(feat),
        "track3_downday_classifier": track3_downday_classifier(feat),
        "frozen_recipe": sweep_and_freeze(df_raw),
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[2492 chip-price divergence] wrote {OUT / 'results.json'}")
    # console summary
    d0 = payload["track0_diagnostics"]
    print("Track0 same-day d_chip↔d_price:", d0["same_day"]["d_chip_vs_d_price"])
    print("Track0 R2_pit:", d0.get("r2_pit"))
    fr = payload["frozen_recipe"]
    print(f"Frozen recipe chip_diff={fr['chip_diff']} price={fr['price_mode']} "
          f"IS_excess={fr['IS']['excess']:.2f}(n={fr['IS']['n']}) "
          f"OOS_excess={fr['OOS']['excess']:.2f}(n={fr['OOS']['n']}) → {fr['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
