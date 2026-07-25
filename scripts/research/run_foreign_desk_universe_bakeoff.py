#!/usr/bin/env python3
"""Cross-stock foreign-desk bakeoff: which allowlisted desks beat official 外資?

Extends ``run_2492_foreign_desk_bakeoff.py`` from one melt-up name to the
long-history universe. Same strict foreign-broker allowlist (no keyword match).

Protocol
  - Branch load is date-chunked + ``securities_trader_id IN (…)`` so SQLite can
    use the PK prefix on trade_date (full-table scans hang).
  - Outcome = stock forward return − 1.15 × IX0001 (fixed β; expert-pool convention).
  - Desk / foreign signal = net shares × close (NTD). Buy day = signal > 0.
  - IS = [2022-01-01, 2026); OOS = [2026, end]. Rank by IS fwd Spearman;
    KEEP needs IS lift≥+0.3pp, OOS same sign, and breadth ≥55% of stocks.

Out: reports/research/chip-overlays/foreign_desk_universe/
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports/research/chip-overlays/foreign_desk_universe"
SOURCE = "finmind"
MIN_BARS = 1500
BRANCH_START = "2022-01-01"
IS_START = "2022-01-01"
OOS = "2026-01-01"
PRIMARY_H = 5
BETA = 1.15
MIN_STOCK_DAYS = 60  # desk must appear on a stock this many days to enter breadth

FOREIGN_DESKS: dict[str, str] = {
    "1360": "港商麥格理",
    "1440": "美林",
    "1470": "摩根士丹利",
    "1480": "美商高盛",
    "1560": "港商野村",
    "1590": "花旗環球",
    "1650": "新加坡商瑞銀",
    "8440": "摩根大通",
}


def load_universe_panel() -> pd.DataFrame:
    uri = f"file:{DB.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        sids = [
            r[0]
            for r in conn.execute(
                "SELECT stock_id FROM stock_daily_bars WHERE source=? "
                "GROUP BY stock_id HAVING COUNT(*)>=?",
                (SOURCE, MIN_BARS),
            )
        ]
        ph = ",".join("?" * len(sids))
        bars = pd.read_sql_query(
            f"SELECT stock_id, trade_date, close, volume FROM stock_daily_bars "
            f"WHERE source=? AND stock_id IN ({ph}) AND trade_date>=? "
            f"ORDER BY stock_id, trade_date",
            conn,
            params=[SOURCE, *sids, "2020-01-01"],
        )
        inst = pd.read_sql_query(
            f"SELECT stock_id, trade_date, COALESCE(foreign_net,0) AS foreign_net "
            f"FROM stock_institutional_daily WHERE source=? AND stock_id IN ({ph}) "
            f"AND trade_date>=?",
            conn,
            params=[SOURCE, *sids, "2020-01-01"],
        )
        ix = pd.read_sql_query(
            """SELECT date AS trade_date, close AS ix FROM daily_bars
               WHERE code='IX0001' AND date>=?
               ORDER BY date,
                 CASE source WHEN 'yahoo' THEN 1 WHEN 'finmind' THEN 2 ELSE 9 END""",
            conn,
            params=["2020-01-01"],
        ).drop_duplicates("trade_date", keep="first")
    for d in (bars, inst, ix):
        d["trade_date"] = pd.to_datetime(d["trade_date"])
    bars = bars[bars["close"] > 0]
    df = bars.merge(inst, on=["stock_id", "trade_date"], how="left")
    df = df.merge(ix, on="trade_date", how="left")
    return df.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)


def load_foreign_branches(sids: list[str]) -> pd.DataFrame:
    """Chunk by calendar month; filter allowlist IDs in SQL."""
    uri = f"file:{DB.resolve()}?mode=ro"
    ids = list(FOREIGN_DESKS)
    id_ph = ",".join("?" * len(ids))
    sid_set = set(sids)
    months = pd.date_range(BRANCH_START, "2026-08-01", freq="MS")
    pieces: list[pd.DataFrame] = []
    with sqlite3.connect(uri, uri=True) as conn:
        for i, start in enumerate(months[:-1]):
            end = months[i + 1]
            q = f"""
              SELECT trade_date, stock_id, securities_trader_id AS bid, net
              FROM stock_broker_branch_daily
              WHERE source=? AND trade_date>=? AND trade_date<?
                AND securities_trader_id IN ({id_ph})
            """
            chunk = pd.read_sql_query(
                q,
                conn,
                params=[SOURCE, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), *ids],
            )
            if chunk.empty:
                continue
            chunk = chunk[chunk["stock_id"].isin(sid_set)]
            pieces.append(chunk)
            if (i + 1) % 12 == 0:
                print(f"  branches through {start.date()} rows={sum(len(p) for p in pieces)}", flush=True)
    if not pieces:
        return pd.DataFrame(columns=["trade_date", "stock_id", "bid", "net"])
    out = pd.concat(pieces, ignore_index=True)
    out["trade_date"] = pd.to_datetime(out["trade_date"])
    return out


def add_outcomes(df: pd.DataFrame) -> pd.DataFrame:
    g = df.groupby("stock_id", sort=False)
    df = df.copy()
    df["f_ntd"] = df["foreign_net"] * df["close"]
    # IX path return
    ix = df.drop_duplicates("trade_date").sort_values("trade_date")[["trade_date", "ix"]]
    ix["ix_fwd"] = ix["ix"].shift(-PRIMARY_H) / ix["ix"] - 1.0
    df = df.merge(ix[["trade_date", "ix_fwd"]], on="trade_date", how="left")
    fwd_s = g["close"].shift(-PRIMARY_H) / df["close"] - 1.0
    df[f"x{PRIMARY_H}"] = fwd_s - BETA * df["ix_fwd"]
    return df


def _spearman(a: pd.Series, b: pd.Series) -> float | None:
    x = pd.concat([a, b], axis=1).dropna()
    if len(x) < 100:
        return None
    return round(float(x.iloc[:, 0].corr(x.iloc[:, 1], method="spearman")), 4)


def _lift(sig: pd.Series, tgt: pd.Series, min_n: int = 100) -> dict:
    y = tgt.dropna()
    m = (sig.reindex(y.index) > 0).fillna(False)
    buy = y[m]
    if len(buy) < min_n:
        return {"n": int(len(buy))}
    return {
        "n": int(len(buy)),
        "med_pct": round(float(buy.median() * 100), 3),
        "lift_pp": round(float((buy.median() - y.median()) * 100), 3),
        "win": round(float((buy > 0).mean() * 100), 1),
    }


def evaluate_signal(panel: pd.DataFrame, col: str, label: str) -> dict:
    target = f"x{PRIMARY_H}"
    is_m = (panel["trade_date"] >= IS_START) & (panel["trade_date"] < OOS)
    oos_m = panel["trade_date"] >= OOS
    row: dict = {"id": col, "label": label}
    row["IS_fwd_ic"] = _spearman(panel.loc[is_m, col], panel.loc[is_m, target])
    row["OOS_fwd_ic"] = _spearman(panel.loc[oos_m, col], panel.loc[oos_m, target])
    row["IS_sign"] = _lift(panel.loc[is_m, col], panel.loc[is_m, target])
    row["OOS_sign"] = _lift(panel.loc[oos_m, col], panel.loc[oos_m, target], min_n=40)
    # breadth: among stocks with enough desk activity, does desk IC beat foreign?
    breadth_pos = breadth_tot = beat_foreign = 0
    for sid, chunk in panel[is_m].groupby("stock_id"):
        if col != "f_ntd" and (chunk[col] != 0).sum() < MIN_STOCK_DAYS:
            continue
        ic = chunk[col].corr(chunk[target], method="spearman")
        if not np.isfinite(ic):
            continue
        breadth_tot += 1
        breadth_pos += int(ic > 0)
        if col != "f_ntd":
            fic = chunk["f_ntd"].corr(chunk[target], method="spearman")
            if np.isfinite(fic) and ic > fic:
                beat_foreign += 1
    row["breadth"] = {
        "stocks": breadth_tot,
        "ic_positive": f"{breadth_pos}/{breadth_tot}" if breadth_tot else None,
        "ic_positive_pct": round(100.0 * breadth_pos / breadth_tot, 1) if breadth_tot else None,
        "beat_foreign_ic": (
            f"{beat_foreign}/{breadth_tot}" if breadth_tot and col != "f_ntd" else None
        ),
        "beat_foreign_pct": (
            round(100.0 * beat_foreign / breadth_tot, 1)
            if breadth_tot and col != "f_ntd"
            else None
        ),
    }
    # yearly IS lifts
    by_year = {}
    is_panel = panel.loc[is_m].copy()
    is_panel["year"] = is_panel["trade_date"].dt.year
    for y, chunk in is_panel.groupby("year"):
        by_year[int(y)] = _lift(chunk[col], chunk[target], min_n=50)
    row["by_year_IS"] = by_year
    # verdict
    i_lift = row["IS_sign"].get("lift_pp")
    o_lift = row["OOS_sign"].get("lift_pp")
    i_ic = row["IS_fwd_ic"]
    b_pct = row["breadth"].get("ic_positive_pct") or 0
    if i_lift is None or i_ic is None:
        row["verdict"] = "INSUFFICIENT"
    elif i_lift >= 0.3 and o_lift is not None and o_lift > 0 and i_ic > 0 and b_pct >= 55:
        row["verdict"] = "KEEP"
    elif i_ic is not None and i_ic <= -0.02 and (row["OOS_fwd_ic"] or 0) < 0:
        row["verdict"] = "REVERSE"
    elif i_lift is not None and i_lift <= -0.3 and o_lift is not None and o_lift < 0:
        row["verdict"] = "REVERSE"
    else:
        row["verdict"] = "WEAK_OR_FLIP"
    return row


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("[1/4] universe panel…", flush=True)
    panel = load_universe_panel()
    sids = sorted(panel["stock_id"].unique())
    print(f"  stocks={len(sids)} rows={len(panel)}", flush=True)
    print("[2/4] foreign branches (monthly chunks)…", flush=True)
    br = load_foreign_branches(sids)
    print(f"  branch rows={len(br)} desks={br['bid'].nunique() if len(br) else 0}", flush=True)
    print("[3/4] merge desk NTD…", flush=True)
    panel = add_outcomes(panel)
    br = br.merge(
        panel[["stock_id", "trade_date", "close"]],
        on=["stock_id", "trade_date"],
        how="left",
    )
    br["ntd"] = br["net"] * br["close"]
    piv = br.pivot_table(
        index=["stock_id", "trade_date"], columns="bid", values="ntd", aggfunc="sum"
    )
    for bid in FOREIGN_DESKS:
        col = f"desk_{bid}"
        if bid in piv.columns:
            s = piv[bid].rename(col)
            panel = panel.merge(s.reset_index(), on=["stock_id", "trade_date"], how="left")
        else:
            panel[col] = np.nan
        panel[col] = panel[col].fillna(0.0)
    desk_cols = [f"desk_{b}" for b in FOREIGN_DESKS]
    panel["foreign_desks_sum"] = panel[desk_cols].sum(axis=1)

    print("[4/4] evaluate…", flush=True)
    # restrict to branch era
    panel = panel[panel["trade_date"] >= BRANCH_START].copy()
    results = [
        evaluate_signal(panel, "f_ntd", "官方外資 foreign_net×P"),
        evaluate_signal(panel, "foreign_desks_sum", "外資分點合計(白名單)"),
    ]
    for bid, name in FOREIGN_DESKS.items():
        results.append(evaluate_signal(panel, f"desk_{bid}", f"{bid} {name}"))

    # rank desks (not aggregates)
    def _rank_key(r: dict) -> tuple:
        ic = r["IS_fwd_ic"]
        bad = ic is None or not isinstance(ic, (int, float)) or not np.isfinite(ic)
        return (bad, -(ic if not bad else 0.0), -(r["IS_sign"].get("lift_pp") or -99))

    desks = sorted(
        [r for r in results if r["id"].startswith("desk_")],
        key=_rank_key,
    )

    payload = {
        "protocol": {
            "universe_n": int(panel["stock_id"].nunique()),
            "branch_start": BRANCH_START,
            "is": [IS_START, OOS],
            "oos": [OOS, str(panel["trade_date"].max().date())],
            "primary_h": PRIMARY_H,
            "excess": f"stock − {BETA}×IX0001",
            "allowlist": FOREIGN_DESKS,
            "gate": {
                "IS_lift_pp": 0.3,
                "OOS_same_sign": True,
                "breadth_ic_positive_pct": 55,
            },
        },
        "results": results,
        "desk_ranking": [
            {
                "rank": i + 1,
                "label": r["label"],
                "IS_fwd_ic": r["IS_fwd_ic"],
                "OOS_fwd_ic": r["OOS_fwd_ic"],
                "IS_lift": r["IS_sign"].get("lift_pp"),
                "OOS_lift": r["OOS_sign"].get("lift_pp"),
                "breadth_ic_pos_pct": r["breadth"].get("ic_positive_pct"),
                "beat_foreign_pct": r["breadth"].get("beat_foreign_pct"),
                "verdict": r["verdict"],
            }
            for i, r in enumerate(desks)
        ],
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT / 'results.json'}")
    for r in results:
        if r["id"] in ("f_ntd", "foreign_desks_sum") or r["id"].startswith("desk_"):
            print(
                f"  {r['label'][:26]:26s} IS_ic={r['IS_fwd_ic']} "
                f"IS_lift={r['IS_sign'].get('lift_pp')} "
                f"breadth={r['breadth'].get('ic_positive_pct')} "
                f"beatF={r['breadth'].get('beat_foreign_pct')} → {r['verdict']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
