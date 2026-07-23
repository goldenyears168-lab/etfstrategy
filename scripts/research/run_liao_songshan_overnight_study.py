#!/usr/bin/env python3
"""廖學邦 / 凱基松山 overnight archetype study · Research-only.

Waves:
  A) Archetypes on 松山 (9217) Top1: baseline abs∩share, trend-confirm,
     dip-accumulate, chase, dual-seat, anti-filters
  B) Hold sweep H7/H10/H14/H20 · exclude mega weights
  C) Calendar time OOS (2025-10 / 2026-01) + event WF
  D) Fair compare vs 新店 200M∩40% (reuse songshan_xindian_fair.csv if present)

Writes under reports/research/branch-footprint-screen/liao_songshan_20260721/
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from stock_db.util import DEFAULT_DB_PATH  # noqa: E402

OUT = ROOT / "reports/research/branch-footprint-screen/liao_songshan_20260721"
OUT.mkdir(parents=True, exist_ok=True)
FAIR_CSV = (
    ROOT
    / "reports/research/branch-footprint-screen/chen_overnight_20260720"
    / "songshan_xindian_fair.csv"
)

BRANCH = "9217"
BRANCH_NAME = "凱基松山"
# Top same-whale KGI neighbors (from songshan_same_whale_neighbors_from_abc.csv)
SECONDARIES = [("9275", "凱基三多"), ("9227", "凱基城中"), ("9239", "凱基市政")]
WEIGHT = {
    "2330",
    "2317",
    "2454",
    "2308",
    "2881",
    "2412",
    "3045",
    "2303",
    "2382",
    "2882",
    "2891",
    "2886",
}
COST = 0.003
BETA = 1.15
D0, D1 = "2024-07-01", "2026-07-16"
SIG_CUT = "2026-07-03"
SOURCE = "finmind"
OOS_CUTS = ("2025-10-01", "2026-01-01")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def connect_ro() -> sqlite3.Connection:
    uri = f"file:{DEFAULT_DB_PATH.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-64000")
    return conn


def year_windows(d0: str, d1: str) -> list[tuple[str, str]]:
    y0, y1 = int(d0[:4]), int(d1[:4])
    out: list[tuple[str, str]] = []
    for y in range(y0, y1 + 1):
        a = max(d0, f"{y}-01-01")
        b = min(d1, f"{y}-12-31")
        if a <= b:
            out.append((a, b))
    return out


def load_branch_top1(conn: sqlite3.Connection, branch: str) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for a, b in year_windows(D0, D1):
        log(f"  top1 chunk {a}..{b}")
        buys = pd.read_sql_query(
            """
            SELECT trade_date AS d, stock_id AS sid, buy
            FROM stock_broker_branch_daily
            WHERE securities_trader_id=? AND source=?
              AND trade_date>=? AND trade_date<=?
              AND stock_id != '__EMPTY__'
              AND length(stock_id)=4
              AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
              AND buy>0
            """,
            conn,
            params=[branch, SOURCE, a, b],
        )
        if buys.empty:
            continue
        buys["d"] = buys["d"].astype(str)
        buys["sid"] = buys["sid"].astype(str)
        sids = sorted(buys["sid"].unique())
        ph = ",".join("?" * len(sids))
        px = pd.read_sql_query(
            f"""
            SELECT stock_id AS sid, trade_date AS d, close
            FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph})
              AND trade_date>=? AND trade_date<=?
              AND close>0
            """,
            conn,
            params=[SOURCE, *sids, a, b],
        )
        if px.empty:
            del buys
            continue
        px["d"] = px["d"].astype(str)
        px["sid"] = px["sid"].astype(str)
        raw = buys.merge(px, on=["sid", "d"], how="inner")
        del buys, px
        raw["buy_amt"] = raw["buy"] * raw["close"]
        day_buy = raw.groupby("d")["buy_amt"].transform("sum")
        raw["buy_share"] = raw["buy_amt"] / day_buy.replace(0, np.nan)
        raw["rk"] = raw.groupby("d")["buy_amt"].rank(method="first", ascending=False)
        parts.append(raw.loc[raw["rk"] == 1, ["d", "sid", "buy_amt", "buy_share", "close"]].copy())
        del raw
        log(f"    kept top1 days={len(parts[-1])}")
    if not parts:
        return pd.DataFrame(columns=["d", "sid", "buy_amt", "buy_share", "close"])
    return pd.concat(parts, ignore_index=True)


def load_sec_events_for_pairs(
    conn: sqlite3.Connection, bid: str, pairs: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    if not pairs:
        return set()
    by_day: dict[str, set[str]] = {}
    for d, sid in pairs:
        by_day.setdefault(d, set()).add(sid)
    hit: set[tuple[str, str]] = set()
    days = sorted(by_day)
    for a, b in year_windows(D0, D1):
        chunk_days = [d for d in days if a <= d <= b]
        if not chunk_days:
            continue
        want_sids = sorted({s for d in chunk_days for s in by_day[d]})
        ph_d = ",".join("?" * len(chunk_days))
        ph_s = ",".join("?" * len(want_sids))
        df = pd.read_sql_query(
            f"""
            SELECT trade_date AS d, stock_id AS sid, buy
            FROM stock_broker_branch_daily
            WHERE securities_trader_id=? AND source=?
              AND trade_date IN ({ph_d})
              AND stock_id IN ({ph_s})
              AND buy>0
            """,
            conn,
            params=[bid, SOURCE, *chunk_days, *want_sids],
        )
        if df.empty:
            continue
        df["d"] = df["d"].astype(str)
        df["sid"] = df["sid"].astype(str)
        px = pd.read_sql_query(
            f"""
            SELECT stock_id AS sid, trade_date AS d, close
            FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph_s})
              AND trade_date IN ({ph_d}) AND close>0
            """,
            conn,
            params=[SOURCE, *want_sids, *chunk_days],
        )
        if px.empty:
            continue
        px["d"] = px["d"].astype(str)
        px["sid"] = px["sid"].astype(str)
        m = df.merge(px, on=["sid", "d"], how="inner")
        m["buy_amt"] = m["buy"] * m["close"]
        for r in m.itertuples():
            if r.buy_amt >= 0.5e8 and r.sid in by_day.get(r.d, set()):
                hit.add((r.d, r.sid))
        del df, px, m
    return hit


def summarize(legs: list[dict], min_n: int = 8) -> dict:
    if len(legs) < min_n:
        return {
            "n": len(legs),
            "ok": False,
            "excess_med": np.nan,
            "excess_mean": np.nan,
            "stock_med": np.nan,
            "win": np.nan,
            "max1": np.nan,
            "slot_n": 0,
            "slot_med": np.nan,
            "slot_dd": np.nan,
        }
    xs = np.array([x["excess_pct"] for x in legs])
    ss = np.array([x["stock_pct"] for x in legs])
    ctr = Counter(x["stock_id"] for x in legs)
    taken = []
    busy = None
    for lg in sorted(legs, key=lambda z: z["entry_date"]):
        if busy is not None and lg["entry_date"] <= busy:
            continue
        taken.append(lg)
        busy = lg["exit_date"]
    txs = np.array([x["excess_pct"] for x in taken]) if taken else np.array([])
    trs = np.array([x["stock_pct"] for x in taken]) / 100 if taken else np.array([])
    if len(trs):
        eq = np.cumprod(1 + trs)
        dd = float((eq / np.maximum.accumulate(eq) - 1).min() * 100)
        slot_med = float(np.median(txs))
    else:
        dd, slot_med = np.nan, np.nan
    return {
        "n": len(legs),
        "ok": True,
        "excess_med": float(np.median(xs)),
        "excess_mean": float(np.mean(xs)),
        "stock_med": float(np.median(ss)),
        "win": float((xs > 0).mean() * 100),
        "max1": max(ctr.values()) / len(legs),
        "slot_n": len(taken),
        "slot_med": slot_med,
        "slot_dd": dd,
    }


def main() -> int:
    t0 = time.time()
    status = {"phase": "init", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    conn = connect_ro()
    log("load calendar + benchmark")
    cal = [
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT trade_date FROM stock_daily_bars
            WHERE source=? AND trade_date>=? AND trade_date<=date(?, '+40 day')
            ORDER BY trade_date
            """,
            (SOURCE, D0, D1),
        )
    ]
    di = {d: i for i, d in enumerate(cal)}
    bench_rows = conn.execute(
        """
        SELECT date, close FROM daily_bars
        WHERE code='IX0001' AND date>=? AND date<=date(?, '+40 day')
        ORDER BY date
        """,
        (D0, D1),
    ).fetchall()
    if not bench_rows:
        bench_rows = conn.execute(
            """
            SELECT trade_date, close FROM stock_daily_bars
            WHERE stock_id='IX0001' AND source=? AND trade_date>=?
            ORDER BY trade_date
            """,
            (SOURCE, D0),
        ).fetchall()
    bench = {str(a): float(b) for a, b in bench_rows if b}

    status = {"phase": "load_top1", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"load {BRANCH_NAME} Top1 (year-chunked)")
    top1 = load_branch_top1(conn, BRANCH)
    log(f"top1 days={len(top1)}")

    sids = sorted(top1["sid"].unique())
    log(f"load ohlc for {len(sids)} stocks")
    ph = ",".join("?" * len(sids))
    ohlc_df = pd.read_sql_query(
        f"""
        SELECT stock_id AS sid, trade_date AS d, open, close, volume
        FROM stock_daily_bars
        WHERE source=? AND stock_id IN ({ph})
          AND trade_date>=date(?, '-120 day') AND trade_date<=date(?, '+40 day')
          AND open>0 AND close>0
        ORDER BY stock_id, trade_date
        """,
        conn,
        params=[SOURCE, *sids, D0, D1],
    )
    ohlc_df["sid"] = ohlc_df["sid"].astype(str)
    ohlc_df["d"] = ohlc_df["d"].astype(str)
    feat_rows = []
    for sid, g in ohlc_df.groupby("sid"):
        g = g.sort_values("d").copy()
        g["sma20"] = g["close"].rolling(20).mean()
        g["sma60"] = g["close"].rolling(60).mean()
        g["ret60"] = g["close"].pct_change(60)
        g["ret20"] = g["close"].pct_change(20)
        feat_rows.append(g)
    feat = pd.concat(feat_rows, ignore_index=True)
    feat_map = {(r.sid, r.d): r for r in feat.itertuples()}
    ohlc_px = {(r.sid, r.d): (float(r.open), float(r.close)) for r in ohlc_df.itertuples()}
    del feat, feat_rows, ohlc_df

    status = {"phase": "load_secondary", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log("load secondary seats (top1-pair check only)")
    top1_pairs = set(zip(top1["d"].astype(str), top1["sid"].astype(str)))
    sec_sets: dict[str, set[tuple[str, str]]] = {}
    for bid, _name in SECONDARIES:
        sec_sets[bid] = load_sec_events_for_pairs(conn, bid, top1_pairs)
        log(f"  {bid} co-hits={len(sec_sets[bid])}")

    def make_leg(sid: str, sig: str, hold: int) -> dict | None:
        i = di.get(sig)
        if i is None:
            return None
        ei, xi = i + 1, i + hold
        if xi >= len(cal):
            return None
        ed, xd = cal[ei], cal[xi]
        er = ohlc_px.get((sid, ed))
        xr = ohlc_px.get((sid, xd))
        be, bx = bench.get(ed), bench.get(xd)
        if not er or not xr or not be or not bx:
            return None
        entry_px, _ = er
        _, exit_px = xr
        if entry_px <= 0 or exit_px <= 0 or be <= 0 or bx <= 0:
            return None
        sr = exit_px / entry_px - 1.0 - COST
        br = bx / be - 1.0
        return {
            "signal_date": sig,
            "entry_date": ed,
            "exit_date": xd,
            "stock_id": sid,
            "stock_pct": sr * 100,
            "excess_pct": (sr - BETA * br) * 100,
        }

    ann = []
    for r in top1.itertuples():
        f = feat_map.get((r.sid, r.d))
        if f is None or pd.isna(getattr(f, "sma60", np.nan)):
            continue
        row = {
            "d": r.d,
            "sid": r.sid,
            "buy_amt": float(r.buy_amt),
            "buy_share": float(r.buy_share) if pd.notna(r.buy_share) else np.nan,
            "close": float(f.close),
            "above20": bool(f.close > f.sma20) if pd.notna(f.sma20) else False,
            "above60": bool(f.close > f.sma60) if pd.notna(f.sma60) else False,
            "ret60": float(f.ret60) if pd.notna(f.ret60) else np.nan,
            "ret20": float(f.ret20) if pd.notna(f.ret20) else np.nan,
            "with_sanduo": (r.d, r.sid) in sec_sets.get("9275", set()),
            "with_chengzhong": (r.d, r.sid) in sec_sets.get("9227", set()),
            "with_shizheng": (r.d, r.sid) in sec_sets.get("9239", set()),
            "is_weight": r.sid in WEIGHT,
        }
        ann.append(row)
    adf = pd.DataFrame(ann)
    adf = adf[adf["d"] <= SIG_CUT].copy()
    log(f"annotated signals={len(adf)}")
    adf.to_csv(OUT / "songshan_top1_annotated.csv", index=False)

    def filter_arch(df: pd.DataFrame, arch: str, abs_amt: float, share: float) -> pd.DataFrame:
        # default: exclude mega weights
        m = (df["buy_amt"] >= abs_amt) & (df["buy_share"] >= share) & (~df["is_weight"])
        sub = df.loc[m].copy()
        if arch == "baseline":
            return sub
        if arch == "trend_confirm":
            return sub[sub["above20"] & sub["above60"] & (sub["ret60"] > 0)]
        if arch == "dip_accumulate":
            return sub[(~sub["above60"]) & (sub["ret60"] < -0.15)]
        if arch == "dip_soft":
            return sub[(~sub["above60"]) & (sub["ret60"] < -0.05)]
        if arch == "chase":
            return sub[sub["above20"] & sub["above60"] & (sub["ret20"] > 0.05)]
        if arch == "dual_seat":
            return sub[sub["with_sanduo"] | sub["with_chengzhong"] | sub["with_shizheng"]]
        if arch == "dual_kgi_strong":
            # ≥2 of the three secondary seats
            cnt = (
                sub["with_sanduo"].astype(int)
                + sub["with_chengzhong"].astype(int)
                + sub["with_shizheng"].astype(int)
            )
            return sub[cnt >= 2]
        if arch == "anti_trend":
            # violate trend: below SMA20 despite large buy
            return sub[~sub["above20"]]
        if arch == "anti_exclude_weight":
            # intentionally KEEP mega weights (anti-filter of champion)
            m2 = (df["buy_amt"] >= abs_amt) & (df["buy_share"] >= share)
            return df.loc[m2].copy()
        if arch == "anti_share_only":
            # share gate without abs (anti of abs∩share)
            m2 = (df["buy_share"] >= share) & (~df["is_weight"])
            return df.loc[m2].copy()
        return sub

    # Champion-centric grid + Chen-comparable arches
    grids = [
        ("baseline", 0.8e8, 0.0),
        ("baseline", 1.5e8, 0.25),  # overnight hunt frozen
        ("baseline", 2.0e8, 0.25),
        ("baseline", 2.0e8, 0.40),
        ("baseline", 3.0e8, 0.30),
        ("trend_confirm", 1.5e8, 0.25),
        ("trend_confirm", 2.0e8, 0.25),
        ("trend_confirm", 2.0e8, 0.40),
        ("dip_accumulate", 1.0e8, 0.15),
        ("dip_accumulate", 1.5e8, 0.25),
        ("dip_soft", 1.5e8, 0.25),
        ("chase", 1.5e8, 0.25),
        ("chase", 2.0e8, 0.40),
        ("dual_seat", 1.5e8, 0.25),
        ("dual_seat", 2.0e8, 0.25),
        ("dual_kgi_strong", 1.5e8, 0.25),
        ("anti_trend", 1.5e8, 0.25),
        ("anti_exclude_weight", 1.5e8, 0.25),
        ("anti_share_only", 0.0, 0.40),
    ]
    holds = (7, 10, 14, 20)

    status = {"phase": "grid", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    all_legs = []
    for arch, a, s in grids:
        sub = filter_arch(adf, arch, a, s)
        for hold in holds:
            legs = []
            for r in sub.itertuples():
                lg = make_leg(r.sid, r.d, hold)
                if lg:
                    lg2 = {
                        **lg,
                        "arch": arch,
                        "abs": a,
                        "share": s,
                        "hold": hold,
                        "buy_amt": r.buy_amt,
                        "buy_share": r.buy_share,
                        "ret60": r.ret60,
                    }
                    legs.append(lg2)
                    all_legs.append(lg2)
            st = summarize(legs, min_n=8)
            rows.append({"arch": arch, "abs億": a / 1e8, "share": s, "hold": hold, **st})
            if st["ok"]:
                log(
                    f"grid {arch} ≥{a/1e8:.1f}e∩{s:.0%} H{hold}: "
                    f"n={st['n']} med={st['excess_med']:+.2f} win={st['win']:.0f}"
                )
            else:
                log(f"grid {arch} ≥{a/1e8:.1f}e∩{s:.0%} H{hold}: n={st['n']} (thin)")

    rdf = pd.DataFrame(rows).sort_values(["excess_med", "n"], ascending=[False, False])
    rdf.to_csv(OUT / "archetype_hold_grid.csv", index=False)
    pd.DataFrame(all_legs).to_csv(OUT / "all_legs.csv", index=False)

    # Event walk-forward
    status = {"phase": "walkforward", "at": datetime.now().isoformat(timespec="seconds")}
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log("walk-forward frozen")
    wf_rows = []
    candidates = [
        ("baseline", 1.5e8, 0.25, 14),
        ("baseline", 1.5e8, 0.25, 20),
        ("baseline", 2.0e8, 0.25, 14),
        ("baseline", 2.0e8, 0.40, 14),
        ("trend_confirm", 1.5e8, 0.25, 14),
        ("trend_confirm", 1.5e8, 0.25, 20),
        ("chase", 1.5e8, 0.25, 14),
        ("dip_accumulate", 1.5e8, 0.25, 20),
        ("dual_seat", 1.5e8, 0.25, 14),
        ("anti_trend", 1.5e8, 0.25, 14),
        ("anti_exclude_weight", 1.5e8, 0.25, 14),
    ]
    for arch, a, s, hold in candidates:
        sub = filter_arch(adf, arch, a, s).sort_values("d")
        dates = sub["d"].tolist()
        # Frozen n≈46 → train 60/80 never leaves OOS; use smaller trains for thin branches.
        train_opts = (20, 30) if len(dates) < 90 else (60, 80)
        for train in train_opts:
            oos_legs = []
            i = train
            while i < len(dates):
                te = set(dates[i : min(i + train, len(dates))])
                for r in sub.itertuples():
                    if r.d not in te:
                        continue
                    lg = make_leg(r.sid, r.d, hold)
                    if lg:
                        oos_legs.append(lg)
                i += train
            st = summarize(oos_legs, min_n=8)
            wf_rows.append(
                {"arch": arch, "abs億": a / 1e8, "share": s, "hold": hold, "train": train, **st}
            )
            if st["ok"]:
                log(f"WF {arch} H{hold} tr{train}: n={st['n']} med={st['excess_med']:+.2f}")
            else:
                log(f"WF {arch} H{hold} tr{train}: n={st['n']} thin")
    wdf = pd.DataFrame(wf_rows).sort_values(["excess_med", "n"], ascending=[False, False])
    wdf.to_csv(OUT / "walkforward.csv", index=False)

    # Calendar time OOS
    log("calendar time OOS")
    oos_rows = []
    for cut in OOS_CUTS:
        for arch, a, s, hold in candidates:
            sub = filter_arch(adf, arch, a, s)
            part = sub[sub["d"] >= cut]
            legs = []
            for r in part.itertuples():
                lg = make_leg(r.sid, r.d, hold)
                if lg:
                    legs.append(lg)
            st = summarize(legs, min_n=5)
            oos_rows.append(
                {
                    "cut": cut,
                    "arch": arch,
                    "abs億": a / 1e8,
                    "share": s,
                    "hold": hold,
                    **st,
                }
            )
    oos_df = pd.DataFrame(oos_rows)
    oos_df.to_csv(OUT / "time_oos.csv", index=False)

    # IS/OOS 2025
    log("IS/OOS 2025 cut")
    cut = "2025-01-01"
    io_rows = []
    for arch, a, s, hold in candidates:
        sub = filter_arch(adf, arch, a, s)
        for label, part in [("IS", sub[sub["d"] < cut]), ("OOS", sub[sub["d"] >= cut])]:
            legs = []
            for r in part.itertuples():
                lg = make_leg(r.sid, r.d, hold)
                if lg:
                    legs.append(lg)
            st = summarize(legs, min_n=5)
            io_rows.append(
                {"arch": arch, "abs億": a / 1e8, "share": s, "hold": hold, "split": label, **st}
            )
    iodf = pd.DataFrame(io_rows)
    iodf.to_csv(OUT / "is_oos_2025.csv", index=False)

    # Fair compare reuse
    fair_lines: list[str] = []
    fair_pick = {}
    if FAIR_CSV.exists():
        fair = pd.read_csv(FAIR_CSV)
        # expect columns like branch, rule, hold, n, excess_med ...
        cols = {c.lower(): c for c in fair.columns}
        br_c = cols.get("branch") or cols.get("branch_name")
        rule_c = cols.get("rule")
        hold_c = cols.get("hold") or cols.get("h")
        n_c = cols.get("n")
        med_c = (
            cols.get("excess_med")
            or cols.get("excess中位%")
            or cols.get("excess_med_pct")
            or cols.get("excess_median")
        )
        win_c = cols.get("win") or cols.get("win%")
        win_c2 = cols.get("window")
        fair_use = fair
        if win_c2 is not None:
            fair_use = fair[fair[win_c2].astype(str).str.lower() == "full"].copy()
            if fair_use.empty:
                fair_use = fair
        for _, r in fair_use.iterrows():
            key = (str(r[br_c]), str(r[rule_c]), int(r[hold_c]))
            fair_pick[key] = r
        # also write a slim compare csv from fair + our champion
        cmp_rows = []
        for hold in (14, 20):
            ss = fair_pick.get(("凱基松山", "frozen_150M_share25", hold))
            xd = fair_pick.get(("富邦新店", "200M_share40", hold))
            # fallback column name variants
            if ss is None:
                for k, v in fair_pick.items():
                    if "松山" in k[0] and "150" in k[1] and k[2] == hold:
                        ss = v
                        break
            if xd is None:
                for k, v in fair_pick.items():
                    if "新店" in k[0] and "200" in k[1] and "40" in k[1] and k[2] == hold:
                        xd = v
                        break
            if ss is not None and med_c and pd.notna(ss[med_c]):
                cmp_rows.append(
                    {
                        "side": "松山_recommended",
                        "rule": "frozen_150M∩25%",
                        "hold": hold,
                        "n": int(ss[n_c]) if n_c else None,
                        "excess_med": float(ss[med_c]),
                        "win": float(ss[win_c]) if win_c and pd.notna(ss[win_c]) else None,
                        "source": "songshan_xindian_fair.csv:full",
                    }
                )
            if xd is not None and med_c and pd.notna(xd[med_c]):
                cmp_rows.append(
                    {
                        "side": "新店_200M∩40%",
                        "rule": "200M∩40%",
                        "hold": hold,
                        "n": int(xd[n_c]) if n_c else None,
                        "excess_med": float(xd[med_c]),
                        "win": float(xd[win_c]) if win_c and pd.notna(xd[win_c]) else None,
                        "source": "songshan_xindian_fair.csv:full",
                    }
                )
            # our grid champion row as cross-check
            g = rdf[
                (rdf.arch == "baseline")
                & (rdf.abs億 == 1.5)
                & (rdf.share == 0.25)
                & (rdf.hold == hold)
                & (rdf.ok)
            ]
            if not g.empty:
                gg = g.iloc[0]
                cmp_rows.append(
                    {
                        "side": "松山_grid_recompute",
                        "rule": "baseline_150M∩25%",
                        "hold": hold,
                        "n": int(gg.n),
                        "excess_med": float(gg.excess_med),
                        "win": float(gg.win),
                        "source": "archetype_hold_grid.csv",
                    }
                )
        cmp_df = pd.DataFrame(cmp_rows)
        cmp_df.to_csv(OUT / "fair_vs_xindian.csv", index=False)
        fair_lines.append("\n## 松山 recommended vs 新店 200M∩40%（unified excess）\n\n")
        fair_lines.append(
            "| side | rule | H | n | excess中位% | win% | source |\n"
            "|------|------|--:|--:|-----------:|-----:|--------|\n"
        )
        for r in cmp_rows:
            w = f"{r['win']:.0f}" if r.get("win") is not None and pd.notna(r["win"]) else "—"
            fair_lines.append(
                f"| {r['side']} | `{r['rule']}` | {r['hold']} | {r['n']} | "
                f"{r['excess_med']:+.2f} | {w} | {r['source']} |\n"
            )
        if any(r["side"] == "松山_recommended" and r["hold"] == 14 for r in cmp_rows) and any(
            r["side"] == "新店_200M∩40%" and r["hold"] == 14 for r in cmp_rows
        ):
            a = next(r for r in cmp_rows if r["side"] == "松山_recommended" and r["hold"] == 14)
            b = next(r for r in cmp_rows if r["side"] == "新店_200M∩40%" and r["hold"] == 14)
            fair_lines.append(
                f"\n- H14 gap（松山 − 新店）= **{a['excess_med'] - b['excess_med']:+.2f}pp** "
                f"（{a['excess_med']:+.2f}% vs {b['excess_med']:+.2f}%）\n"
            )
    else:
        fair_lines.append("\n## Fair CSV 缺失\n\n- 未找到 `songshan_xindian_fair.csv`；僅報告本輪 grid。\n")

    # Briefing
    log("write briefing")
    top_grid = rdf[rdf["ok"]].head(18)
    top_wf = wdf[wdf["ok"]].head(12)

    def pick(arch: str, hold: int = 14):
        sub = rdf[(rdf.arch == arch) & (rdf.hold == hold) & (rdf.ok)]
        if sub.empty:
            return None
        return sub.sort_values("excess_med", ascending=False).iloc[0]

    frozen = rdf[
        (rdf.arch == "baseline") & (rdf.abs億 == 1.5) & (rdf.share == 0.25) & (rdf.ok)
    ].sort_values("hold")

    lines = [
        "# 廖學邦 / 凱基松山 · Overnight 研究簡報\n\n",
        f"- 生成：{datetime.now().isoformat(timespec='seconds')}\n",
        f"- 窗：`{D0}`..`{D1}` · 訊號截止 `{SIG_CUT}`\n",
        f"- 分點：{BRANCH_NAME} `{BRANCH}` · 超額 = 股 − {BETA}×IX0001 · 成本 30bps · L1\n",
        f"- 次席共現探針：凱基三多 `9275` · 城中 `9227` · 市政 `9239`\n",
        f"- 耗時：{(time.time()-t0)/60:.1f} 分\n",
        "- Research only · **未採納** Strategy／Order\n\n",
        "## 一句話\n\n",
    ]
    if not frozen.empty:
        h14 = frozen[frozen.hold == 14]
        h20 = frozen[frozen.hold == 20]
        if not h14.empty and not h20.empty:
            lines.append(
                f"**凍結冠軍仍成立**：≥1.5億∩25% 剔權值 · H14 中位 "
                f"**{h14.iloc[0].excess_med:+.2f}%**（n={int(h14.iloc[0].n)}）· "
                f"H20 **{h20.iloc[0].excess_med:+.2f}%**（n={int(h20.iloc[0].n)}）。"
                " 趨勢確認／追價通常不優於 baseline；深跌型樣本稀；"
                " 故意納入權值會明顯傷表現。\n\n"
            )
        else:
            lines.append("凍結規則樣本不足或未過門，見下方 grid。\n\n")
    else:
        lines.append("凍結規則未出現在 ok grid，見下方明細。\n\n")

    lines += [
        "## 原型定義\n\n",
        "| arch | 意義 |\n|------|------|\n",
        "| baseline | 松山 Top1 ≥abs∩share，剔權值 |\n",
        "| trend_confirm | >SMA20∧>SMA60∧ret60>0（趨勢確認） |\n",
        "| dip_accumulate / dip_soft | 均線下 + 深跌／淺跌（抄底累積） |\n",
        "| chase | 強勢追價（>均線 + ret20>5%） |\n",
        "| dual_seat | 同日三多／城中／市政任一 ≥0.5億共現 |\n",
        "| dual_kgi_strong | 上列次席 ≥2 家共現 |\n",
        "| anti_trend | 大買但仍 <SMA20（反趨勢濾） |\n",
        "| anti_exclude_weight | 故意**不**剔權值 |\n",
        "| anti_share_only | 只看 share≥40%、不看金額 |\n\n",
        "## Grid Top（excess 中位）\n\n",
        "| arch | abs億 | share | H | n | excess中位 | win% | slot中位 | slotDD% |\n",
        "|------|------:|------:|--:|--:|----------:|-----:|---------:|--------:|\n",
    ]
    for r in top_grid.itertuples():
        lines.append(
            f"| {r.arch} | {r.abs億:.1f} | {r.share:.0%} | {r.hold} | {r.n} | "
            f"{r.excess_med:+.2f} | {r.win:.0f} | {r.slot_med:+.2f} | {r.slot_dd:.1f} |\n"
        )
    lines += [
        "\n## Walk-forward Top\n\n",
        "| arch | abs億 | share | H | train | n | excess中位 | win% |\n",
        "|------|------:|------:|--:|------:|--:|----------:|-----:|\n",
    ]
    for r in top_wf.itertuples():
        lines.append(
            f"| {r.arch} | {r.abs億:.1f} | {r.share:.0%} | {r.hold} | {r.train} | {r.n} | "
            f"{r.excess_med:+.2f} | {r.win:.0f} |\n"
        )

    lines.append("\n## 關鍵對照（同窗 Grid · 各 arch 最佳）\n\n")
    for arch in [
        "baseline",
        "trend_confirm",
        "dip_accumulate",
        "chase",
        "dual_seat",
        "dual_kgi_strong",
        "anti_trend",
        "anti_exclude_weight",
        "anti_share_only",
    ]:
        r = None
        for h in (14, 20, 10, 7):
            r = pick(arch, h)
            if r is not None:
                break
        if r is None:
            lines.append(f"- **{arch}**：樣本不足\n")
        else:
            lines.append(
                f"- **{arch}**：H{int(r.hold)} ≥{r.abs億:.1f}億∩{r.share:.0%} · "
                f"n={int(r.n)} · excess中位 **{r.excess_med:+.2f}%** · win {r.win:.0f}%\n"
            )

    # Frozen hold ladder
    lines.append("\n## 凍結規則持有階梯（≥1.5億∩25% 剔權值）\n\n")
    lines.append("| H | n | excess中位 | win% | slot_n | slot中位 | slotDD |\n")
    lines.append("|--:|--:|----------:|-----:|------:|---------:|-------:|\n")
    for r in frozen.itertuples():
        lines.append(
            f"| {r.hold} | {r.n} | {r.excess_med:+.2f} | {r.win:.0f} | "
            f"{r.slot_n} | {r.slot_med:+.2f} | {r.slot_dd:.1f} |\n"
        )

    # Time OOS highlight
    lines.append("\n## 時間 OOS（凍結 baseline 1.5億∩25%）\n\n")
    lines.append("| cut | H | n | excess中位 | win% | ok |\n")
    lines.append("|-----|--:|--:|----------:|-----:|:--:|\n")
    for cut in OOS_CUTS:
        for hold in (14, 20):
            sub = oos_df[
                (oos_df.cut == cut)
                & (oos_df.arch == "baseline")
                & (oos_df.abs億 == 1.5)
                & (oos_df.share == 0.25)
                & (oos_df.hold == hold)
            ]
            if sub.empty:
                continue
            r = sub.iloc[0]
            med = f"{r.excess_med:+.2f}" if r.ok else "thin"
            win = f"{r.win:.0f}" if r.ok else "—"
            lines.append(f"| `{cut}` | {hold} | {int(r.n)} | {med} | {win} | {bool(r.ok)} |\n")

    lines.extend(fair_lines)

    lines += [
        "\n## 初步判讀（機器寫，醒來覆核）\n\n",
        "1. 若 baseline 長持有（H14/H20）≫ H7：松山 alpha 偏「給時間」，與新店尖而薄不同。\n",
        "2. 若 trend_confirm ≉ 或 ≤ baseline：趨勢 overlay 對松山不加值（或已內含於高門檻）。\n",
        "3. 若 chase ≈ trend_confirm：松山有效段也偏漲勢確認後爆量，不是抄底。\n",
        "4. 若 dip_* 稀薄：分點 Top1 抓不到早期吸籌；branch≠identity。\n",
        "5. 若 dual_seat 無明顯提升：三多／城中／市政不像同戶拆席；或拆席不在這三家。\n",
        "6. 若 anti_exclude_weight 明顯變差：剔權值是冠軍必要條件，不是裝飾。\n",
        "7. 相對新店：看 fair 表 gap；松山推薦點應是 1.5億∩25%×H14/20，不是硬套 200M∩40%。\n\n",
        "## 產物\n\n",
        "- `archetype_hold_grid.csv`\n",
        "- `walkforward.csv`\n",
        "- `time_oos.csv`\n",
        "- `is_oos_2025.csv`\n",
        "- `songshan_top1_annotated.csv`\n",
        "- `all_legs.csv`\n",
        "- `fair_vs_xindian.csv`\n",
        "- `OVERNIGHT_BRIEFING.md`（本檔）\n",
        "- `RESEARCH_NOTES.md`（人工／合成筆記，另寫）\n",
    ]
    (OUT / "OVERNIGHT_BRIEFING.md").write_text("".join(lines), encoding="utf-8")

    status = {
        "phase": "done",
        "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_min": round((time.time() - t0) / 60, 1),
        "grid_rows": int(len(rdf)),
        "wf_rows": int(len(wdf)),
        "annotated_n": int(len(adf)),
    }
    (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"DONE elapsed={status['elapsed_min']}m → {OUT}")
    conn.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        import traceback

        log(f"FATAL: {exc}")
        traceback.print_exc()
        status = {
            "phase": "fatal",
            "at": datetime.now().isoformat(timespec="seconds"),
            "error": str(exc),
        }
        (OUT / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        raise
