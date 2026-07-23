#!/usr/bin/env python3
"""Tier A · 分點早買足跡 × 籌碼 W1 前兆（T−3/T−2/T−1 · Research only）.

Uses expert-pool green events (knowledge/trades) + stock_broker_branch_daily
+ build_chip_feature_frame (three_streak). Book-only; no strategy.yaml / launchd.

    PYTHONPATH=src .venv/bin/python scripts/research/run_tier_a_branch_chip_precursor.py
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.adapters_db import (  # noqa: E402
    connect_ro,
    load_calendar,
    load_ohlc,
)
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402
from research.chen_chip.whale_events import TIER_A, load_whale_events  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402

OUT = ROOT / "reports/research/whale_chip_precursor"
TRADES_DIR = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
)
SOURCE = "finmind"
D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
LAGS = (3, 2, 1, 0)  # T−3 .. T
AMT_FLOORS = {"net_gt0": 0.0, "amt_0p5yi": 5e7, "amt_1yi": 1e8}
FOREIGN_KW = (
    "美商",
    "摩根",
    "瑞銀",
    "美林",
    "港商",
    "港麥",
    "麥格理",
    "野村",
    "瑞士",
    "花旗",
    "渣打",
    "香港",
    "新加坡",
    "大和",
    "法銀",
    "德意志",
    "巴克萊",
    "高盛",
    "美銀",
)
UNIVERSE_TOP_N = 40
CTRL_MULT = 2
RNG_SEED = 42


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


def load_cores(trades_dir: Path) -> dict[str, dict[str, str]]:
    cores: dict[str, dict[str, str]] = {}
    for sid in TIER_A:
        path = trades_dir / f"{sid}.json"
        if not path.exists():
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        cores[sid] = {str(k): str(v) for k, v in (doc.get("core") or {}).items()}
    return cores


def lag_date(cal: list[str], di: dict[str, int], sig: str, k: int) -> str | None:
    i = di.get(sig)
    if i is None or i < k:
        return None
    return cal[i - k]


def fetch_tape_for_dates(
    conn,
    stock_ids: list[str],
    dates: list[str],
    *,
    chunk: int = 25,
) -> pd.DataFrame:
    """Index-friendly: trade_date IN (...) ∧ stock_id IN (...)."""
    if not dates or not stock_ids:
        return pd.DataFrame()
    uniq = sorted(set(dates))
    ph_s = ",".join("?" * len(stock_ids))
    parts: list[pd.DataFrame] = []
    for i in range(0, len(uniq), chunk):
        batch = uniq[i : i + chunk]
        ph_d = ",".join("?" * len(batch))
        df = pd.read_sql_query(
            f"""
            SELECT trade_date AS d, stock_id AS sid,
                   securities_trader_id AS tid, securities_trader AS tname,
                   buy, sell, net
            FROM stock_broker_branch_daily
            WHERE source=? AND trade_date IN ({ph_d}) AND stock_id IN ({ph_s})
            """,
            conn,
            params=[SOURCE, *batch, *stock_ids],
        )
        if not df.empty:
            parts.append(df)
    if not parts:
        return pd.DataFrame(
            columns=["d", "sid", "tid", "tname", "buy", "sell", "net", "amt"]
        )
    out = pd.concat(parts, ignore_index=True)
    for c in ("buy", "sell", "net"):
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)
    out["sid"] = out["sid"].astype(str)
    out["tid"] = out["tid"].astype(str)
    out["d"] = out["d"].astype(str)
    return out


def attach_amounts(tape: pd.DataFrame, px: dict[tuple[str, str], float]) -> pd.DataFrame:
    if tape.empty:
        tape = tape.copy()
        tape["amt"] = []
        return tape
    tape = tape.copy()
    tape["close"] = [px.get((r.sid, r.d), np.nan) for r in tape.itertuples()]
    tape["amt"] = tape["net"] * tape["close"]
    tape["buy_amt"] = tape["buy"] * tape["close"]
    tape["sell_amt"] = tape["sell"] * tape["close"]
    return tape


def active_mask(df: pd.DataFrame, floor_key: str) -> pd.Series:
    floor = AMT_FLOORS[floor_key]
    if floor <= 0:
        return df["net"] > 0
    return df["amt"].fillna(0) >= floor


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"db={DEFAULT_DB_PATH}")
    cores = load_cores(TRADES_DIR)
    whale = load_whale_events(TIER_A)
    whale = whale[
        (whale["signal_date"] >= D0) & (whale["signal_date"] <= D1)
    ].copy()
    log(f"green events Tier A n={len(whale)} cores={ {s: len(c) for s, c in cores.items()} }")

    conn = connect_ro(DEFAULT_DB_PATH)
    cal = [d for d in load_calendar(conn, D0, D1) if D0 <= d <= D1]
    di = {d: i for i, d in enumerate(cal)}
    ohlc = load_ohlc(conn, TIER_A, D0, D1)
    px = {(r.sid, r.d): float(r.close) for r in ohlc.itertuples()}
    log("build chip features (W1 three_streak)")
    feat = build_chip_feature_frame(conn, TIER_A, D0, D1)
    feat_idx = feat.set_index(["sid", "d"]) if not feat.empty else feat

    # --- event lag dates + control sample dates ---
    whale_by_sid: dict[str, set[str]] = defaultdict(set)
    event_meta: list[dict] = []
    needed_dates: set[str] = set()
    for r in whale.itertuples():
        lags: dict[int, str | None] = {}
        for k in LAGS:
            d = lag_date(cal, di, r.signal_date, k)
            lags[k] = d
            if d:
                needed_dates.add(d)
        i = di.get(r.signal_date)
        if i is not None:
            for j in range(max(0, i - 2), min(len(cal), i + 3)):
                whale_by_sid[r.sid].add(cal[j])
        event_meta.append(
            {
                "sid": r.sid,
                "name": r.name,
                "signal_date": r.signal_date,
                "mode": r.mode,
                "floor": r.floor,
                "floor_amt": r.floor_amt,
                "r_adj_pct_l1": r.r_adj_pct_l1,
                "thin": r.thin,
                **{f"d_lag{k}": lags[k] for k in LAGS},
            }
        )
    events = pd.DataFrame(event_meta)
    n_events = len(events)

    # control: same-stock non-event days (exclude ±2 session window)
    rng = np.random.default_rng(RNG_SEED)
    ctrl_needed: list[tuple[str, str]] = []
    for sid in TIER_A:
        sid_days = [d for d in cal if (sid, d) in px]
        quiet = [d for d in sid_days if d not in whale_by_sid.get(sid, set())]
        n_sid_ev = int((events["sid"] == sid).sum())
        take = min(len(quiet), max(n_sid_ev * CTRL_MULT, 8))
        if take <= 0:
            continue
        pick = list(rng.choice(quiet, size=take, replace=False))
        for d in pick:
            ctrl_needed.append((sid, d))
            needed_dates.add(d)
    log(f"needed dates={len(needed_dates)} ctrl_pairs={len(ctrl_needed)}")

    log("load branch tape (date-chunked)")
    tape = fetch_tape_for_dates(conn, TIER_A, sorted(needed_dates))
    conn.close()
    tape = attach_amounts(tape, px)
    tape["is_foreign"] = tape["tname"].map(is_foreign)
    tape_dom = tape[~tape["is_foreign"]].copy()
    log(f"tape rows={len(tape)} domestic={len(tape_dom)}")

    # coverage warning per sid
    tape_cov = (
        tape_dom.groupby("sid")["d"]
        .nunique()
        .reindex(TIER_A)
        .fillna(0)
        .astype(int)
    )
    for sid, nd in tape_cov.items():
        log(f"  tape distinct-days sid={sid}: {nd}")

    name_map: dict[str, str] = {}
    for r in tape.itertuples():
        if r.tid not in name_map and r.tname:
            name_map[r.tid] = str(r.tname)
    for sid, core in cores.items():
        for tid, nm in core.items():
            name_map.setdefault(tid, nm)

    # index: (sid, d, tid) -> amt/net
    tape_key = tape_dom.set_index(["sid", "d", "tid"], drop=False)

    def branch_rows_on(sid: str, d: str | None) -> pd.DataFrame:
        if not d:
            return pd.DataFrame()
        try:
            sub = tape_key.xs((sid, d), level=("sid", "d"), drop_level=False)
        except KeyError:
            return pd.DataFrame()
        if isinstance(sub, pd.Series):
            sub = sub.to_frame().T
        return sub.reset_index(drop=True)

    def chip_w1(sid: str, d: str | None) -> bool:
        if not d or feat_idx.empty:
            return False
        try:
            row = feat_idx.loc[(sid, d)]
        except KeyError:
            return False
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return float(row.get("three_streak", 0) or 0) >= 2

    def chip_w3(sid: str, d: str | None) -> bool:
        if not d or feat_idx.empty:
            return False
        try:
            row = feat_idx.loc[(sid, d)]
        except KeyError:
            return False
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return bool(
            float(row.get("foreign_sum_5d", 0) or 0) > 0
            and float(row.get("three_streak", 0) or 0) >= 2
        )

    # ---------- event panel ----------
    panel_rows: list[dict] = []
    champ_hit: dict[tuple[int, str, str], int] = defaultdict(int)  # lag,tid,floor
    champ_events: dict[tuple[int, str, str], set[str]] = defaultdict(set)
    univ_hit: dict[tuple[int, str, str], int] = defaultdict(int)
    univ_events: dict[tuple[int, str, str], set[str]] = defaultdict(set)
    # early uniqueness @ T−2: active then, but tid NOT among champion seats firing on T
    # keys: (tid, floor, scope) scope=champ|univ
    early_only_hit: dict[tuple[str, str, str], int] = defaultdict(int)
    early_any_hit: dict[tuple[str, str, str], int] = defaultdict(int)

    for ev in events.itertuples():
        ekey = f"{ev.sid}|{ev.signal_date}"
        core = cores.get(ev.sid, {})
        d_t = ev.d_lag0
        sub_t = branch_rows_on(ev.sid, d_t)
        champ_t = set()
        if not sub_t.empty and core:
            m = active_mask(sub_t, "amt_0p5yi") & sub_t["tid"].isin(core.keys())
            champ_t = set(sub_t.loc[m, "tid"].astype(str))

        w1_t2 = chip_w1(ev.sid, ev.d_lag2)
        w3_t2 = chip_w3(ev.sid, ev.d_lag2)

        for k in LAGS:
            d = getattr(ev, f"d_lag{k}")
            sub = branch_rows_on(ev.sid, d)
            for floor_key in AMT_FLOORS:
                if sub.empty:
                    continue
                act = sub[active_mask(sub, floor_key)]
                # champion seats
                if core:
                    cact = act[act["tid"].isin(core.keys())]
                    for tid in cact["tid"].astype(str).unique():
                        champ_hit[(k, tid, floor_key)] += 1
                        champ_events[(k, tid, floor_key)].add(ekey)
                # universe domestic
                for tid in act["tid"].astype(str).unique():
                    univ_hit[(k, tid, floor_key)] += 1
                    univ_events[(k, tid, floor_key)].add(ekey)

            # T−2 early uniqueness
            if k == 2 and not sub.empty:
                for floor_key in ("net_gt0", "amt_0p5yi"):
                    act_f = sub[active_mask(sub, floor_key)]
                    for tid in act_f["tid"].astype(str).unique():
                        early_any_hit[(tid, floor_key, "univ")] += 1
                        if tid not in champ_t:
                            early_only_hit[(tid, floor_key, "univ")] += 1
                        if tid in core:
                            early_any_hit[(tid, floor_key, "champ")] += 1
                            if tid not in champ_t:
                                early_only_hit[(tid, floor_key, "champ")] += 1

        # champion any / best at each lag for panel
        def champ_summary(d: str | None, floor_key: str) -> tuple[int, float, str]:
            sub = branch_rows_on(ev.sid, d)
            if sub.empty or not core:
                return 0, 0.0, ""
            c = sub[sub["tid"].isin(core.keys()) & active_mask(sub, floor_key)]
            if c.empty:
                return 0, 0.0, ""
            best = c.sort_values("amt", ascending=False).iloc[0]
            return (
                int(len(c)),
                float(best["amt"]) if pd.notna(best["amt"]) else 0.0,
                f"{best['tid']}:{name_map.get(str(best['tid']), best['tname'])}",
            )

        c2_n, c2_amt, c2_best = champ_summary(ev.d_lag2, "amt_0p5yi")
        c2_n0, _, _ = champ_summary(ev.d_lag2, "net_gt0")
        c3_n, _, _ = champ_summary(ev.d_lag3, "amt_0p5yi")
        c1_n, _, _ = champ_summary(ev.d_lag1, "amt_0p5yi")
        ct_n, _, _ = champ_summary(ev.d_lag0, "amt_0p5yi")

        # top universe movers at T−2
        sub2 = branch_rows_on(ev.sid, ev.d_lag2)
        top_univ = ""
        if not sub2.empty:
            u = sub2[active_mask(sub2, "amt_0p5yi")].sort_values(
                "amt", ascending=False
            )
            top_univ = ";".join(
                f"{r.tid}:{name_map.get(str(r.tid), r.tname)}={r.amt/1e8:.2f}億"
                for r in u.head(5).itertuples()
            )

        panel_rows.append(
            {
                "sid": ev.sid,
                "name": ev.name,
                "signal_date": ev.signal_date,
                "split": "OOS" if ev.signal_date >= OOS else "IS",
                "mode": ev.mode,
                "r_adj_pct_l1": ev.r_adj_pct_l1,
                "d_t3": ev.d_lag3,
                "d_t2": ev.d_lag2,
                "d_t1": ev.d_lag1,
                "d_t": ev.d_lag0,
                "chip_w1_t2": int(w1_t2),
                "chip_w3_t2": int(w3_t2),
                "champ_netgt0_t2": c2_n0,
                "champ_0p5yi_t3": c3_n,
                "champ_0p5yi_t2": c2_n,
                "champ_0p5yi_t1": c1_n,
                "champ_0p5yi_t": ct_n,
                "champ_best_t2": c2_best,
                "champ_best_amt_t2": round(c2_amt, 0),
                "champ_on_t": ",".join(sorted(champ_t)),
                "top_univ_0p5yi_t2": top_univ,
                "combo_champ0p5_w1_t2": int(c2_n > 0 and w1_t2),
                "combo_champ_net_w1_t2": int(c2_n0 > 0 and w1_t2),
            }
        )

    panel = pd.DataFrame(panel_rows)
    panel.to_csv(OUT / "branch_event_panel.csv", index=False)
    log(f"wrote event panel n={len(panel)}")

    # ---------- control activity rates ----------
    # For each (sid, d) control pair, which tids active
    ctrl_active: dict[str, dict[str, int]] = {
        fk: defaultdict(int) for fk in AMT_FLOORS
    }  # floor -> tid -> count active days
    ctrl_champ_active: dict[str, dict[str, int]] = {
        fk: defaultdict(int) for fk in AMT_FLOORS
    }
    ctrl_n_by_sid: dict[str, int] = defaultdict(int)
    ctrl_champ_n_by_sid: dict[str, int] = defaultdict(int)
    # also any-champ-active rates for combo baselines
    ctrl_any_champ: dict[str, int] = defaultdict(int)  # floor -> count
    ctrl_chip_w1 = 0
    ctrl_combo_champ0p5_w1 = 0
    ctrl_combo_any_univ0p5_w1 = 0
    ctrl_total = len(ctrl_needed)

    for sid, d in ctrl_needed:
        ctrl_n_by_sid[sid] += 1
        core = cores.get(sid, {})
        if core:
            ctrl_champ_n_by_sid[sid] += 1
        sub = branch_rows_on(sid, d)
        w1 = chip_w1(sid, d)
        if w1:
            ctrl_chip_w1 += 1
        for floor_key in AMT_FLOORS:
            if sub.empty:
                continue
            act = sub[active_mask(sub, floor_key)]
            tids = set(act["tid"].astype(str))
            for tid in tids:
                ctrl_active[floor_key][tid] += 1
            if core:
                ct = tids & set(core.keys())
                for tid in ct:
                    ctrl_champ_active[floor_key][tid] += 1
                if ct:
                    ctrl_any_champ[floor_key] += 1
            if floor_key == "amt_0p5yi" and w1:
                if core and (tids & set(core.keys())):
                    ctrl_combo_champ0p5_w1 += 1
                if len(tids) > 0:
                    ctrl_combo_any_univ0p5_w1 += 1

    # ---------- rank tables ----------
    def build_rank(
        hit: dict[tuple[int, str, str], int],
        ev_sets: dict[tuple[int, str, str], set[str]],
        ctrl_counts: dict[str, dict[str, int]],
        *,
        view: str,
        restrict_core: bool,
    ) -> pd.DataFrame:
        rows = []
        for (k, tid, floor_key), n_hit in hit.items():
            # eligible events: for champion view, only events on stocks where tid in core
            if restrict_core:
                eligible = [
                    f"{sid}|{sig}"
                    for sid, sig in zip(events["sid"], events["signal_date"])
                    if tid in cores.get(sid, {})
                ]
                n_elig = len(eligible)
                # recount hits only on eligible (hit dict already only increments when in core)
            else:
                n_elig = n_events
            if n_elig < 3:
                continue
            cov = n_hit / n_elig
            # control rate: among control days on stocks where branch can appear
            if restrict_core:
                sids_with = [s for s, c in cores.items() if tid in c]
                n_ctrl = sum(ctrl_champ_n_by_sid[s] for s in sids_with)
            else:
                n_ctrl = ctrl_total
            c_hit = ctrl_counts[floor_key].get(tid, 0)
            c_rate = c_hit / n_ctrl if n_ctrl > 0 else np.nan
            lift = cov / c_rate if c_rate and c_rate > 0 else np.nan
            # per-stock event frequency for universe cap
            rows.append(
                {
                    "view": view,
                    "lag": k,
                    "lag_label": f"T-{k}" if k else "T",
                    "tid": tid,
                    "tname": name_map.get(tid, ""),
                    "floor": floor_key,
                    "n_hit_events": n_hit,
                    "n_eligible_events": n_elig,
                    "coverage": round(cov, 4),
                    "ctrl_hit_days": c_hit,
                    "ctrl_days": n_ctrl,
                    "ctrl_rate": round(c_rate, 4) if pd.notna(c_rate) else np.nan,
                    "lift": round(lift, 3) if pd.notna(lift) else np.nan,
                    "early_uniq_t2_not_champT": early_only_hit.get(
                        (tid, floor_key, "champ" if restrict_core else "univ"), 0
                    )
                    if k == 2
                    else np.nan,
                    "early_any_t2": early_any_hit.get(
                        (tid, floor_key, "champ" if restrict_core else "univ"), 0
                    )
                    if k == 2
                    else np.nan,
                }
            )
        return pd.DataFrame(rows)

    rank_champ = build_rank(
        champ_hit, champ_events, ctrl_champ_active, view="champion", restrict_core=True
    )
    rank_univ = build_rank(
        univ_hit, univ_events, ctrl_active, view="universe", restrict_core=False
    )

    # universe: keep top N by event frequency at each lag×floor (net_gt0 / 0.5yi)
    if not rank_univ.empty:
        keep_parts = []
        for (lag, floor_key), g in rank_univ.groupby(["lag", "floor"]):
            g2 = g.sort_values(
                ["n_hit_events", "lift", "coverage"], ascending=False
            ).head(UNIVERSE_TOP_N)
            keep_parts.append(g2)
        rank_univ = pd.concat(keep_parts, ignore_index=True)

    rank = pd.concat([rank_champ, rank_univ], ignore_index=True)
    rank = rank.sort_values(
        ["view", "lag", "floor", "coverage", "lift"],
        ascending=[True, True, True, False, False],
    )
    rank.to_csv(OUT / "branch_rank_by_lag.csv", index=False)
    log(f"wrote branch_rank_by_lag rows={len(rank)}")

    # ---------- T−2 combo metrics ----------
    combo_rows = []

    def _rate(mask: pd.Series) -> float:
        return float(mask.mean()) if len(mask) else np.nan

    # event-conditioned
    e_w1 = panel["chip_w1_t2"] == 1
    e_w3 = panel["chip_w3_t2"] == 1
    e_champ0 = panel["champ_0p5yi_t2"] > 0
    e_champ_net = panel["champ_netgt0_t2"] > 0
    e_combo = panel["combo_champ0p5_w1_t2"] == 1
    e_combo_net = panel["combo_champ_net_w1_t2"] == 1

    ctrl_w1_rate = ctrl_chip_w1 / ctrl_total if ctrl_total else np.nan
    ctrl_champ0_rate = (
        ctrl_any_champ["amt_0p5yi"] / ctrl_total if ctrl_total else np.nan
    )
    ctrl_champ_net_rate = (
        ctrl_any_champ["net_gt0"] / ctrl_total if ctrl_total else np.nan
    )
    ctrl_combo_rate = (
        ctrl_combo_champ0p5_w1 / ctrl_total if ctrl_total else np.nan
    )

    def add_combo(name: str, ev_rate: float, ctrl_rate: float, n_ev: int, note: str):
        lift = ev_rate / ctrl_rate if ctrl_rate and ctrl_rate > 0 else np.nan
        combo_rows.append(
            {
                "rule": name,
                "event_rate": round(ev_rate, 4),
                "ctrl_rate": round(ctrl_rate, 4) if pd.notna(ctrl_rate) else np.nan,
                "lift": round(lift, 3) if pd.notna(lift) else np.nan,
                "n_event_hit": n_ev,
                "n_events": n_events,
                "ctrl_total": ctrl_total,
                "note": note,
            }
        )

    add_combo(
        "T-2 chip W1 three_streak>=2",
        _rate(e_w1),
        ctrl_w1_rate,
        int(e_w1.sum()),
        "chip-only yellow",
    )
    add_combo(
        "T-2 chip W3 foreign_sum5d>0 & three_streak>=2",
        _rate(e_w3),
        np.nan,  # not tracked on ctrl in this thin pass
        int(e_w3.sum()),
        "chip W3; ctrl lift skipped",
    )
    add_combo(
        "T-2 any champion amt>=0.5億",
        _rate(e_champ0),
        ctrl_champ0_rate,
        int(e_champ0.sum()),
        "branch-only (champion seats)",
    )
    add_combo(
        "T-2 any champion net>0",
        _rate(e_champ_net),
        ctrl_champ_net_rate,
        int(e_champ_net.sum()),
        "branch-only net>0",
    )
    add_combo(
        "T-2 champion>=0.5億 AND W1",
        _rate(e_combo),
        ctrl_combo_rate,
        int(e_combo.sum()),
        "branch∧chip co-precursor",
    )
    add_combo(
        "T-2 champion net>0 AND W1",
        _rate(e_combo_net),
        np.nan,
        int(e_combo_net.sum()),
        "looser branch∧chip",
    )

    # per-champion-branch combo with W1 at T−2
    for tid in sorted({t for c in cores.values() for t in c}):
        # events where tid is in that stock's core
        elig_mask = panel["sid"].map(lambda s, t=tid: t in cores.get(s, {}))
        elig = panel[elig_mask]
        if len(elig) < 5:
            continue
        hits = 0
        for ev in elig.itertuples():
            if not chip_w1(ev.sid, ev.d_t2):
                continue
            sub = branch_rows_on(ev.sid, ev.d_t2)
            if sub.empty:
                continue
            m = (sub["tid"] == tid) & active_mask(sub, "amt_0p5yi")
            if m.any():
                hits += 1
        # ctrl: days on stocks where tid in core
        c_hits = 0
        c_n = 0
        for sid, d in ctrl_needed:
            if tid not in cores.get(sid, {}):
                continue
            c_n += 1
            if not chip_w1(sid, d):
                continue
            sub = branch_rows_on(sid, d)
            if sub.empty:
                continue
            if ((sub["tid"] == tid) & active_mask(sub, "amt_0p5yi")).any():
                c_hits += 1
        ev_rate = hits / len(elig)
        c_rate = c_hits / c_n if c_n else np.nan
        add_combo(
            f"T-2 {tid}:{name_map.get(tid, '')} >=0.5億 AND W1",
            ev_rate,
            c_rate,
            hits,
            "per champion seat ∧ chip",
        )

    combo_df = pd.DataFrame(combo_rows).sort_values(
        ["lift", "event_rate"], ascending=False
    )
    combo_df.to_csv(OUT / "t2_branch_chip_combo.csv", index=False)
    log(f"wrote t2 combo rows={len(combo_df)}")

    # ---------- thin L1 stratification (among green events) ----------
    def l1_stats(mask: pd.Series) -> dict:
        xs = panel.loc[mask, "r_adj_pct_l1"].dropna().astype(float)
        if len(xs) < 5:
            return {"n": len(xs), "med": np.nan, "win": np.nan}
        return {
            "n": int(len(xs)),
            "med": round(float(xs.median()), 2),
            "win": round(float((xs > 0).mean() * 100), 1),
        }

    l1_rows = [
        {"bucket": "all_green", **l1_stats(panel.index == panel.index)},
        {"bucket": "T2_W1_only_no_champ0p5", **l1_stats(e_w1 & ~e_champ0)},
        {"bucket": "T2_champ0p5_only_no_W1", **l1_stats(e_champ0 & ~e_w1)},
        {"bucket": "T2_combo_champ0p5_W1", **l1_stats(e_combo)},
        {"bucket": "T2_neither", **l1_stats(~e_w1 & ~e_champ0)},
    ]
    l1_df = pd.DataFrame(l1_rows)
    l1_df.to_csv(OUT / "t2_precursor_l1_strata.csv", index=False)

    # ---------- briefing ----------
    gen = datetime.now().isoformat(timespec="seconds")
    # top tables for briefing
    def top_table(view: str, lag: int, floor_key: str, n: int = 12) -> pd.DataFrame:
        g = rank[
            (rank["view"] == view)
            & (rank["lag"] == lag)
            & (rank["floor"] == floor_key)
        ]
        return g.head(n)

    lines = [
        "# Tier A · 分點早買足跡 × 籌碼前兆（T−3 / T−2 / T−1）",
        "",
        f"- 生成：{gen}",
        f"- Runner：`scripts/research/run_tier_a_branch_chip_precursor.py`（Research only）",
        f"- 宇宙：Tier A {TIER_A}",
        f"- 綠燈事件：expert-pool knowledge trades n={n_events}"
        f"（IS={(panel.split=='IS').sum()} / OOS={(panel.split=='OOS').sum()}）",
        f"- 窗：{D0}..{D1} · OOS≥{OOS} · 對照：同股非事件日×{CTRL_MULT}（排除事件±2 交易日）",
        "- 分點 SSOT：`stock_broker_branch_daily` source=`finmind`；金額=股數淨買×當日收",
        "- 籌碼 W1：`three_streak≥2`（`build_chip_feature_frame`）",
        "- **未採納**進 Strategy／Order；未裝 launchd",
        "",
        "## 資料覆蓋",
        "",
        "| sid | 事件 n | 冠軍席 | tape 有資料日數（本輪拉取日） |",
        "|-----|-------:|--------|------------------------------:|",
    ]
    for sid in TIER_A:
        n_e = int((events["sid"] == sid).sum())
        core = cores.get(sid, {})
        core_s = "、".join(f"{k} {v}" for k, v in core.items()) or "—"
        lines.append(
            f"| {sid} | {n_e} | {core_s} | {int(tape_cov.get(sid, 0))} |"
        )
    lines += [
        "",
        "### 注意",
        "",
        "- **3443 創意** 綠燈僅 4 筆，分點排名噪音大。",
        "- 冠軍席名稱以 knowledge JSON `core` 為準；宇宙名來自 tape `securities_trader`（同分點 id 跨券商更名時可能不一致）。",
        "- 本輪 tape 採 **事件 lag 日 + 對照日** 拉取，非整段 2y 全表掃描；覆蓋不足時 coverage 會低估。",
        "- 外資分點已排除（關鍵字含港麥／麥格理等）；門檻對齊專家池：**≥0.5億** 為主、net>0 / ≥1億 對照。",
        "- 宇宙榜常見 **總公司席**（如 5850 統一、9800 元大）：頻次高但專科訊號較噪，解讀宜降權。",
        "- 冠軍席 lift 若對照日 0.5億 幾乎不出現 → 表中為空；可視為「事件窗相對稀有」。",
        "",
        "## A) 冠軍席 · 各 lag 早買覆蓋（≥0.5億）",
        "",
    ]
    for lag in (3, 2, 1):
        t = top_table("champion", lag, "amt_0p5yi", 10)
        lines.append(f"### T−{lag}")
        lines.append("")
        if t.empty:
            lines.append("（無）")
        else:
            lines.append(
                "| tid | 分點 | coverage | lift | n_hit / n_elig | T−2 早現且非當日冠軍席 |"
            )
            lines.append("|-----|------|--------:|-----:|---------------:|----------------------:|")
            for r in t.itertuples():
                eu = r.early_uniq_t2_not_champT if lag == 2 else "—"
                lines.append(
                    f"| {r.tid} | {r.tname} | {r.coverage:.0%} | "
                    f"{r.lift if pd.notna(r.lift) else '—'} | "
                    f"{r.n_hit_events}/{r.n_eligible_events} | {eu} |"
                )
        lines.append("")

    lines += [
        "## B) 宇宙早動分點 · Top（≥0.5億 · 各 lag 取頻次 Top40 後再列前 10）",
        "",
    ]
    for lag in (3, 2, 1):
        t = top_table("universe", lag, "amt_0p5yi", 10)
        lines.append(f"### T−{lag}")
        lines.append("")
        if t.empty:
            lines.append("（無）")
        else:
            lines.append(
                "| tid | 分點 | coverage | lift | n_hit |"
            )
            lines.append("|-----|------|--------:|-----:|------:|")
            for r in t.itertuples():
                lines.append(
                    f"| {r.tid} | {r.tname} | {r.coverage:.0%} | "
                    f"{r.lift if pd.notna(r.lift) else '—'} | {r.n_hit_events} |"
                )
        lines.append("")

    lines += [
        "## T−2 · 分點 ∧ 籌碼 W1 共前兆",
        "",
        "| rule | event 命中率 | ctrl | lift | n_hit |",
        "|------|------------:|-----:|-----:|------:|",
    ]
    show = combo_df.head(25)
    for r in show.itertuples():
        lines.append(
            f"| `{r.rule}` | {r.event_rate:.0%} | "
            f"{r.ctrl_rate if pd.notna(r.ctrl_rate) else '—'} | "
            f"{r.lift if pd.notna(r.lift) else '—'} | {r.n_event_hit} |"
        )

    lines += [
        "",
        "### 解讀（前兆研究 · 非 PnL 宣稱）",
        "",
        f"- 綠燈前 T−2，籌碼 W1 命中 **{_rate(e_w1):.0%}**；冠軍席 ≥0.5億 命中 **{_rate(e_champ0):.0%}**；"
        f"兩者同時 **{_rate(e_combo):.0%}**（lift vs 對照 "
        f"**{combo_df.loc[combo_df.rule=='T-2 champion>=0.5億 AND W1','lift'].iloc[0] if (combo_df.rule=='T-2 champion>=0.5億 AND W1').any() else '—'}**）。",
        "- 組合抬升的是「相對安靜日更常一起出現」，**不是**單獨開倉依據；綠燈仍要等分點共識。",
        "- `t2_branch_chip_combo.csv` 另列各冠軍席 individually ∧ W1。",
        "",
        "## 薄切 L1（僅綠燈事件分層 · 超額 r_adj 已扣成本）",
        "",
        "| bucket | n | med r_adj% | win% |",
        "|--------|--:|----------:|-----:|",
    ]
    for r in l1_df.itertuples():
        lines.append(
            f"| {r.bucket} | {r.n} | {r.med if pd.notna(r.med) else '—'} | "
            f"{r.win if pd.notna(r.win) else '—'} |"
        )

    lines += [
        "",
        "此表**不**證明因果；樣本小、且 conditioning on green。只回答："
        "有黃燈共前兆的綠燈，L1 分佈是否看起來不同。",
        "",
        "## 產出檔",
        "",
        "- `branch_rank_by_lag.csv`",
        "- `t2_branch_chip_combo.csv`",
        "- `branch_event_panel.csv`",
        "- `t2_precursor_l1_strata.csv`",
        "- `BRANCH_EARLY_T2_BRIEFING.md`（本檔）",
        "",
        "## 限制",
        "",
        "- PIT：訊號日 T 只用 ≤T 分點／籌碼；本腳本看的是 **T 之前** 的 footprint。",
        "- Look-ahead：knowledge 綠燈本身是回看共識規則的事後標籤；前兆研究可接受，但不可把 coverage 當未來可交易 precision。",
        "- n=115（3443 僅 4）；單一分點 lift 波動大。",
        "- 未做完整日曆級「黃燈→隔日綠燈」掃描（需全日 tape）；本輪以事件窗 + 對照 lift 為主。",
        "",
    ]
    briefing_path = OUT / "BRANCH_EARLY_T2_BRIEFING.md"
    briefing_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"wrote {briefing_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
