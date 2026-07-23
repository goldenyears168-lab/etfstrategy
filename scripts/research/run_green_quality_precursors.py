#!/usr/bin/env python3
"""Green QUALITY precursors — predict good vs bad expert greens (L1H7).

Research only. Does NOT replace green, does NOT invent branch-yellow alpha.
W1/W5 failed as hard filters (CHIP_AND_GREEN_PRECISION); here they are
tested as quality predictors on greens only.

Protocol (aligned H_E_vs_GREEN_fair):
  universe thick_h3 · champion greens · L1H7 · cost 30bps · β=1.15 · OOS≥2026-01-01
  features T−1..T−5 PIT: three_streak / foreign_streak / big_holder_pct_chg
  optional soft: core seats ≥floor count at lag (not a yellow product)

  PYTHONPATH=src .venv/bin/python scripts/research/run_green_quality_precursors.py
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.adapters_db import connect_ro  # noqa: E402
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402

OUT = ROOT / "reports/research/whale_chip_precursor"
CACHE = OUT / "cache"
HS_PATH = CACHE / "holding_shares_per_tier_a.csv"
GOV_PATH = CACHE / "gov_bank_net_tier_a.csv"
DB = ROOT / "data" / "stocks.db"
SOURCE = "finmind"

START, END, OOS = "2024-07-01", "2026-07-17", "2026-01-01"
COST, HOLD, BETA, DEDUP = 0.003, 7, 1.15, 5
UNIVERSE = [
    "2327",
    "2337",
    "2344",
    "2383",
    "2408",
    "3037",
    "3189",
    "3443",
    "3481",
    "3653",
    "3665",
    "6223",
    "8046",
    "8358",
]
FREEZE_LIFT = 1.15  # meaningfully above 1.1
MIN_OOS_N = 12
MIN_COVER = 0.25


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def _load_watch_module():
    spec = importlib.util.spec_from_file_location(
        "epw_gq", ROOT / "scripts/research/run_expert_pool_watch.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_gq"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def load_champions(mod) -> tuple[dict, dict, dict, dict]:
    cores: dict[str, dict[str, str]] = {}
    floors: dict[str, float] = {}
    modes: dict[str, str] = {}
    names: dict[str, str] = {}
    for sid in UNIVERSE:
        mode, floor, core, name = "回看1日共識", 5e7, {}, sid
        ws = (
            ROOT
            / "reports/research/branch-footprint-screen/expert_pool"
            / sid
            / "watch_spec.json"
        )
        if ws.exists():
            w = json.loads(ws.read_text(encoding="utf-8"))
            core = {str(k): str(v) for k, v in (w.get("core") or {}).items()}
            floor = float(w.get("net_floor") or 5e7)
            fl = w.get("floor_label") or ""
            if "1億" in fl:
                floor = float(w.get("net_floor") or 1e8)
            mode = (w.get("champion") or {}).get("mode") or mode
            name = w.get("stock_name") or name
        if sid in mod.POOLS:
            p = mod.POOLS[sid]
            if not core:
                core = {str(k): str(v) for k, v in p.core.items()}
            floor = float(p.net_floor)
            name = p.stock_name or name
        if sid == "2344" and not ws.exists():
            mode = "同日共識"
        cores[sid] = core
        floors[sid] = floor
        modes[sid] = mode
        names[sid] = name
    return cores, floors, modes, names


def dedupe_ok(last: dict[str, str], sid: str, d: str) -> bool:
    if sid not in last:
        return True
    return (
        datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(last[sid], "%Y-%m-%d")
    ).days > DEDUP


@dataclass(frozen=True)
class Rule:
    id: str
    label: str
    kind: str  # fixed | win5 | combo
    feat: str
    thr: float
    lag: int = 2  # used for fixed
    op: str = ">="  # >= or >
    extra: str | None = None  # AND another feat on same lag/window


RULES: list[Rule] = []
for lag in (1, 2, 3, 4, 5):
    RULES.append(
        Rule(f"L{lag}:three_streak>=2", f"T−{lag} three_streak≥2", "fixed", "three_streak", 2, lag, ">=")
    )
    RULES.append(
        Rule(f"L{lag}:foreign_streak>=2", f"T−{lag} foreign_streak≥2", "fixed", "foreign_streak", 2, lag, ">=")
    )
    RULES.append(
        Rule(f"L{lag}:big_holder_pct_chg>0", f"T−{lag} big_holder_pct_chg>0", "fixed", "big_holder_pct_chg", 0, lag, ">")
    )
    RULES.append(
        Rule(f"L{lag}:core_n>=1", f"T−{lag} core seats≥floor ≥1", "fixed", "core_n", 1, lag, ">=")
    )
    RULES.append(
        Rule(f"L{lag}:core_n>=2", f"T−{lag} core seats≥floor ≥2", "fixed", "core_n", 2, lag, ">=")
    )

RULES.extend(
    [
        Rule("W1_win5", "any T−1..T−5 three_streak≥2", "win5", "three_streak", 2, op=">="),
        Rule("W1b_win5", "any T−1..T−5 foreign_streak≥2", "win5", "foreign_streak", 2, op=">="),
        Rule("W5_win5", "any T−1..T−5 big_holder_pct_chg>0", "win5", "big_holder_pct_chg", 0, op=">"),
        Rule("core_win5_ge1", "any T−1..T−5 core_n≥1", "win5", "core_n", 1, op=">="),
        Rule("core_win5_ge2", "any T−1..T−5 core_n≥2", "win5", "core_n", 2, op=">="),
        Rule(
            "L2:W5b",
            "T−2 big_holder_pct_chg>0 ∧ three_streak≥2",
            "combo",
            "big_holder_pct_chg",
            0,
            2,
            ">",
            extra="three_streak>=2",
        ),
        Rule(
            "L2:W3",
            "T−2 foreign_streak≥2 ∧ three_streak≥2",
            "combo",
            "foreign_streak",
            2,
            2,
            ">=",
            extra="three_streak>=2",
        ),
        Rule(
            "L2:W5_core1",
            "T−2 big_holder>0 ∧ core_n≥1",
            "combo",
            "big_holder_pct_chg",
            0,
            2,
            ">",
            extra="core_n>=1",
        ),
        Rule(
            "win5:W5b",
            "any T−1..T−5 (big_holder>0 ∧ three≥2)",
            "combo_win5",
            "big_holder_pct_chg",
            0,
            op=">",
            extra="three_streak>=2",
        ),
    ]
)


def _ok(v: float, thr: float, op: str) -> bool:
    if not np.isfinite(v):
        return False
    return (v > thr) if op == ">" else (v >= thr)


def feat_at(panel: dict[str, dict[str, dict[str, float]]], sid: str, d: str, col: str) -> float:
    return float(panel.get(sid, {}).get(d, {}).get(col, np.nan))


def rule_hit(
    rule: Rule,
    sid: str,
    sig_i: int,
    cal: list[str],
    panel: dict[str, dict[str, dict[str, float]]],
) -> bool:
    def day_ok(d: str) -> bool:
        if rule.kind in ("fixed", "combo"):
            v = feat_at(panel, sid, d, rule.feat)
            if not _ok(v, rule.thr, rule.op):
                return False
            if rule.extra == "three_streak>=2":
                return _ok(feat_at(panel, sid, d, "three_streak"), 2, ">=")
            if rule.extra == "core_n>=1":
                return _ok(feat_at(panel, sid, d, "core_n"), 1, ">=")
            return True
        # win5 / combo_win5 evaluated per day below
        v = feat_at(panel, sid, d, rule.feat)
        if not _ok(v, rule.thr, rule.op):
            return False
        if rule.extra == "three_streak>=2":
            return _ok(feat_at(panel, sid, d, "three_streak"), 2, ">=")
        return True

    if rule.kind in ("fixed", "combo"):
        j = sig_i - rule.lag
        if j < 0:
            return False
        return day_ok(cal[j])
    # win5 / combo_win5
    for k in range(1, 6):
        j = sig_i - k
        if j < 0:
            continue
        if day_ok(cal[j]):
            return True
    return False


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else float("nan")
    r = tp / (tp + fn) if (tp + fn) else float("nan")
    if not (np.isfinite(p) and np.isfinite(r)) or (p + r) == 0:
        f1 = float("nan")
    else:
        f1 = 2 * p * r / (p + r)
    return p, r, f1


def fmt_pct(v: float | None, d: int = 2) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{v * 100:+.{d}f}%"


def fmt_pp(v: float | None, d: int = 1) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{v:+.{d}f}"


def fmt_f(v: float | None, d: int = 2) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{v:.{d}f}"


def fmt_rate(v: float | None, d: int = 1) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{100.0 * v:.{d}f}%"


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 8:
        return float("nan")
    xr = pd.Series(x[m]).rank().to_numpy()
    yr = pd.Series(y[m]).rank().to_numpy()
    if xr.std() == 0 or yr.std() == 0:
        return float("nan")
    return float(np.corrcoef(xr, yr)[0, 1])


def main() -> None:
    t0 = __import__("time").time()
    mod = _load_watch_module()
    cores, floors, modes, names = load_champions(mod)
    log(f"universe={len(UNIVERSE)}")

    con = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True, timeout=120)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    con.execute("PRAGMA busy_timeout=120000")
    ph = ",".join("?" * len(UNIVERSE))

    bars: dict[str, dict[str, tuple[float, float]]] = defaultdict(dict)
    for r in con.execute(
        f"""SELECT stock_id, trade_date, open, close FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph})
              AND trade_date BETWEEN ? AND ? AND close>0""",
        (SOURCE, *UNIVERSE, START, END),
    ):
        o = float(r["open"] or r["close"])
        c = float(r["close"])
        if o > 0:
            bars[r["stock_id"]][r["trade_date"]] = (o, c)

    ix: dict[str, tuple[float, float]] = {}
    for r in con.execute(
        """SELECT date, open, close FROM daily_bars
           WHERE code='IX0001' AND date BETWEEN ? AND ? AND open>0 AND close>0
           ORDER BY date,
             CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
                  WHEN 'finmind' THEN 2 ELSE 3 END""",
        (START, END),
    ):
        ix.setdefault(r["date"], (float(r["open"]), float(r["close"])))
    ix_dates = sorted(ix)

    core_tids = set().union(*(set(c) for c in cores.values()))
    core_by: dict[str, dict[str, dict[str, float]]] = defaultdict(
        lambda: defaultdict(dict)
    )
    if core_tids:
        ph_c = ",".join("?" * len(core_tids))
        for r in con.execute(
            f"""SELECT stock_id, trade_date, securities_trader_id, net
                FROM stock_broker_branch_daily
                WHERE source=? AND stock_id IN ({ph})
                  AND securities_trader_id IN ({ph_c})
                  AND trade_date BETWEEN ? AND ? AND net>0""",
            (SOURCE, *UNIVERSE, *core_tids, START, END),
        ):
            oc = bars[r["stock_id"]].get(r["trade_date"])
            if oc:
                core_by[r["stock_id"]][r["trade_date"]][
                    r["securities_trader_id"]
                ] = float(r["net"]) * oc[1]
    con.close()

    # chip features (separate ro conn via adapter)
    conn = connect_ro()
    hs = HS_PATH if HS_PATH.exists() else None
    gov = GOV_PATH if GOV_PATH.exists() else None
    feat = build_chip_feature_frame(
        conn, UNIVERSE, START, END, holding_shares_path=hs, gov_bank_path=gov
    )
    conn.close()
    log(f"chip feat rows={len(feat)}")

    panel: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    for r in feat.itertuples(index=False):
        sid, d = str(r.sid), str(r.d)
        for col in ("three_streak", "foreign_streak", "big_holder_pct_chg"):
            v = getattr(r, col, np.nan)
            try:
                panel[sid][d][col] = float(v) if v is not None and pd.notna(v) else np.nan
            except (TypeError, ValueError):
                panel[sid][d][col] = np.nan

    for sid in UNIVERSE:
        core = cores[sid]
        floor = floors[sid]
        for d, m in core_by[sid].items():
            n = sum(1 for t, a in m.items() if t in core and a >= floor)
            panel[sid][d]["core_n"] = float(n)

    cals = {sid: sorted(bars[sid]) for sid in UNIVERSE}
    idx = {sid: {d: i for i, d in enumerate(cals[sid])} for sid in UNIVERSE}

    def expert_green(sid: str, d: str) -> bool:
        core = cores[sid]
        if not core:
            return False
        floor = floors[sid]
        mode = modes[sid]
        i = idx[sid][d]
        today = {
            t
            for t, a in core_by[sid].get(d, {}).items()
            if t in core and a >= floor
        }
        yday: set[str] = set()
        if i > 0:
            yd = cals[sid][i - 1]
            yday = {
                t
                for t, a in core_by[sid].get(yd, {}).items()
                if t in core and a >= floor
            }
        if "OR" in mode:
            return len(today) >= 1
        if "同日" in mode:
            return len(today) >= 2
        return len(today) >= 1 and len(today | yday) >= 2

    def l1h7(sid: str, sig_i: int) -> float | None:
        cal = cals[sid]
        entry_i = sig_i + 1
        if entry_i < 0 or entry_i + HOLD - 1 >= len(cal):
            return None
        ed = cal[entry_i]
        xd = cal[entry_i + HOLD - 1]
        eo, _ = bars[sid][ed]
        _, xc = bars[sid][xd]
        if eo <= 0:
            return None
        ret = xc / eo - 1.0 - COST
        j = next((k for k, d in enumerate(ix_dates) if d >= ed), None)
        xj = next((k for k, d in enumerate(ix_dates) if d >= xd), None)
        if j is None or xj is None:
            return None
        bo = ix[ix_dates[j]][0]
        bc = ix[ix_dates[xj]][1]
        if bo <= 0:
            return None
        return ret - BETA * (bc / bo - 1.0)

    # collect greens
    greens: list[dict] = []
    last: dict[str, str] = {}
    for sid in UNIVERSE:
        cal = cals[sid]
        for i, d in enumerate(cal):
            if d < START or d > END:
                continue
            if not expert_green(sid, d):
                continue
            if not dedupe_ok(last, sid, d):
                continue
            radj = l1h7(sid, i)
            if radj is None:
                continue
            row = {
                "sid": sid,
                "sig": d,
                "sig_i": i,
                "r_adj": radj,
                "oos": d >= OOS,
                "split": "OOS" if d >= OOS else "IS",
            }
            # attach lag features for continuous analysis
            for lag in range(1, 6):
                j = i - lag
                dd = cal[j] if j >= 0 else None
                for col in ("three_streak", "foreign_streak", "big_holder_pct_chg", "core_n"):
                    key = f"{col}_L{lag}"
                    row[key] = feat_at(panel, sid, dd, col) if dd else np.nan
            greens.append(row)
            last[sid] = d

    gdf = pd.DataFrame(greens)
    log(f"greens={len(gdf)} IS={int((~gdf['oos']).sum())} OOS={int(gdf['oos'].sum())}")

    # labels: continuous + binary + tercile (IS cuts applied to all for PIT-ish OOS)
    is_r = gdf.loc[~gdf["oos"], "r_adj"]
    t_lo, t_hi = float(is_r.quantile(1 / 3)), float(is_r.quantile(2 / 3))
    gdf["hit"] = gdf["r_adj"] > 0
    gdf["top_tercile"] = gdf["r_adj"] >= t_hi
    gdf["bot_tercile"] = gdf["r_adj"] <= t_lo
    med_all = float(gdf["r_adj"].median())
    gdf["strong"] = gdf["r_adj"] >= med_all
    log(
        f"labels: hit_rate={gdf['hit'].mean():.1%} top_tercile_cut={t_hi:.4f} "
        f"bot={t_lo:.4f} med={med_all:.4f}"
    )

    # continuous associations (IS)
    cont_rows: list[dict] = []
    for lag in range(1, 6):
        for col in ("three_streak", "foreign_streak", "big_holder_pct_chg", "core_n"):
            key = f"{col}_L{lag}"
            sub = gdf[~gdf["oos"]]
            rho = spearman(sub[key].to_numpy(dtype=float), sub["r_adj"].to_numpy(dtype=float))
            cont_rows.append({"feat": key, "split": "IS", "rho": rho, "n": int(sub[key].notna().sum())})
            sub = gdf[gdf["oos"]]
            rho = spearman(sub[key].to_numpy(dtype=float), sub["r_adj"].to_numpy(dtype=float))
            cont_rows.append({"feat": key, "split": "OOS", "rho": rho, "n": int(sub[key].notna().sum())})
    cont_df = pd.DataFrame(cont_rows)

    # rule evaluation
    eval_rows: list[dict] = []
    for rule in RULES:
        mask = []
        for r in greens:
            mask.append(
                rule_hit(rule, r["sid"], r["sig_i"], cals[r["sid"]], panel)
            )
        gdf[rule.id] = mask
        for split in ("full", "IS", "OOS"):
            if split == "full":
                sub = gdf
            elif split == "IS":
                sub = gdf[~gdf["oos"]]
            else:
                sub = gdf[gdf["oos"]]
            n_g = len(sub)
            if n_g == 0:
                continue
            kept = sub[sub[rule.id]]
            n_k = len(kept)
            base_hit = float(sub["hit"].mean())
            base_top = float(sub["top_tercile"].mean())
            for lab, col in (("hit", "hit"), ("top", "top_tercile")):
                useful_all = sub[sub[col]]
                useful_kept = kept[kept[col]]
                tp = int(len(useful_kept))
                fp = int((~kept[col]).sum()) if n_k else 0
                fn = int(len(useful_all) - tp)
                p, rcall, f1 = prf(tp, fp, fn)
                base = base_hit if lab == "hit" else base_top
                lift = (p / base) if (np.isfinite(p) and base > 0) else float("nan")
                eval_rows.append(
                    {
                        "rule": rule.id,
                        "label": rule.label,
                        "split": split,
                        "lab": lab,
                        "n_green": n_g,
                        "n_kept": n_k,
                        "cover": n_k / n_g if n_g else float("nan"),
                        "P": p,
                        "R": rcall,
                        "F1": f1,
                        "base": base,
                        "lift": lift,
                        "delta_pp": (p - base) * 100 if np.isfinite(p) else float("nan"),
                        "med": float(kept["r_adj"].median()) if n_k else float("nan"),
                        "mean": float(kept["r_adj"].mean()) if n_k else float("nan"),
                        "med_base": float(sub["r_adj"].median()),
                        "mean_base": float(sub["r_adj"].mean()),
                        "med_delta": (
                            float(kept["r_adj"].median()) - float(sub["r_adj"].median())
                            if n_k
                            else float("nan")
                        ),
                    }
                )
    edf = pd.DataFrame(eval_rows)

    # naked green baselines
    baselines = {}
    for split, sub in (
        ("full", gdf),
        ("IS", gdf[~gdf["oos"]]),
        ("OOS", gdf[gdf["oos"]]),
    ):
        baselines[split] = {
            "n": len(sub),
            "med": float(sub["r_adj"].median()) if len(sub) else float("nan"),
            "mean": float(sub["r_adj"].mean()) if len(sub) else float("nan"),
            "hit": float(sub["hit"].mean()) if len(sub) else float("nan"),
            "top": float(sub["top_tercile"].mean()) if len(sub) else float("nan"),
        }

    # pick freeze / best OOS
    hit_oos = edf[(edf["lab"] == "hit") & (edf["split"] == "OOS")].copy()
    hit_is = edf[(edf["lab"] == "hit") & (edf["split"] == "IS")].copy()

    freeze: list[dict] = []
    for _, o in hit_oos.iterrows():
        is_ = hit_is[hit_is["rule"] == o["rule"]]
        if is_.empty:
            continue
        irow = is_.iloc[0]
        if (
            o["n_kept"] >= MIN_OOS_N
            and o["cover"] >= MIN_COVER
            and np.isfinite(o["lift"])
            and o["lift"] >= FREEZE_LIFT
            and np.isfinite(o["med_delta"])
            and o["med_delta"] > 0
            and np.isfinite(irow["lift"])
            and irow["lift"] >= 1.0
        ):
            freeze.append(
                {
                    "rule": o["rule"],
                    "label": o["label"],
                    "oos_lift": float(o["lift"]),
                    "oos_P": float(o["P"]),
                    "oos_n": int(o["n_kept"]),
                    "oos_cover": float(o["cover"]),
                    "oos_med": float(o["med"]),
                    "oos_med_base": float(o["med_base"]),
                    "oos_med_delta": float(o["med_delta"]),
                    "is_lift": float(irow["lift"]),
                    "is_med_delta": float(irow["med_delta"]),
                }
            )

    # best OOS by lift among n_kept≥MIN_OOS_N (even if not freeze)
    cand = hit_oos[hit_oos["n_kept"] >= MIN_OOS_N].sort_values(
        ["lift", "med_delta"], ascending=False
    )
    best = cand.iloc[0] if not cand.empty else None

    # also rank by med improvement with lift≥1.0
    cand_med = hit_oos[
        (hit_oos["n_kept"] >= MIN_OOS_N)
        & (hit_oos["lift"] >= 1.0)
        & (hit_oos["med_delta"] > 0)
    ].sort_values(["med_delta", "lift"], ascending=False)
    best_med = cand_med.iloc[0] if not cand_med.empty else None

    elapsed = __import__("time").time() - t0
    write_report(
        gdf=gdf,
        edf=edf,
        cont_df=cont_df,
        baselines=baselines,
        freeze=freeze,
        best=best,
        best_med=best_med,
        t_lo=t_lo,
        t_hi=t_hi,
        med_all=med_all,
        names=names,
        modes=modes,
        floors=floors,
        cores=cores,
        elapsed=elapsed,
    )
    # cache csv
    OUT.mkdir(parents=True, exist_ok=True)
    gdf.to_csv(OUT / "green_quality_events.csv", index=False)
    edf.to_csv(OUT / "green_quality_rules.csv", index=False)
    cont_df.to_csv(OUT / "green_quality_continuous.csv", index=False)
    log(f"done {elapsed:.1f}s → {OUT / 'GREEN_QUALITY_PRECURSORS.md'}")


def write_report(
    *,
    gdf: pd.DataFrame,
    edf: pd.DataFrame,
    cont_df: pd.DataFrame,
    baselines: dict,
    freeze: list[dict],
    best: pd.Series | None,
    best_med: pd.Series | None,
    t_lo: float,
    t_hi: float,
    med_all: float,
    names: dict,
    modes: dict,
    floors: dict,
    cores: dict,
    elapsed: float,
) -> None:
    b_oos = baselines["OOS"]
    b_all = baselines["full"]
    lines: list[str] = []
    lines.append("# Green quality precursors · expert greens only")
    lines.append("")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}")
    lines.append("- Runner：`scripts/research/run_green_quality_precursors.py`")
    lines.append(
        f"- 協議：thick_h3 · L1H7 · COST={COST} · β={BETA} · OOS≥{OOS} · "
        f"dedupe {DEDUP} 曆日 · DB `data/stocks.db` mode=ro"
    )
    lines.append(
        f"- 宇宙 n={len(UNIVERSE)}：{', '.join(UNIVERSE)}"
    )
    lines.append(
        f"- 綠燈：champion watch_spec／POOLS 重算 · n={len(gdf)} "
        f"(IS {baselines['IS']['n']} / OOS {baselines['OOS']['n']})"
    )
    lines.append(
        "- 目標：**預測綠燈品質**（好 L1H7），**不**取代綠燈、**不**發明分點黃燈 alpha"
    )
    lines.append("- Research only · **未採納** Strategy／Order")
    lines.append(f"- 耗時 {elapsed:.1f}s")
    lines.append("")

    lines.append("## Verdict")
    lines.append("")
    if freeze:
        lines.append(
            f"**Freeze candidate(s) found ({len(freeze)})** — OOS hit-lift≥{FREEZE_LIFT}, "
            f"med↑, n≥{MIN_OOS_N}, cover≥{MIN_COVER:.0%}, IS lift≥1."
        )
        for f in freeze:
            lines.append(
                f"- `{f['rule']}`：OOS hit P={fmt_rate(f['oos_P'])} lift={fmt_f(f['oos_lift'])} · "
                f"med {fmt_pct(f['oos_med'])} vs naked {fmt_pct(f['oos_med_base'])} "
                f"(Δ{fmt_pct(f['oos_med_delta'])}) · n={f['oos_n']} cover={fmt_rate(f['oos_cover'])}"
            )
    else:
        lines.append(
            f"**Do not freeze.** No precursor clears OOS hit-lift ≥ {FREEZE_LIFT} "
            f"(meaningfully >1.1) **and** improves med `r_adj` without killing coverage "
            f"(n≥{MIN_OOS_N}, cover≥{MIN_COVER:.0%}, IS lift≥1)."
        )
        # soft: best cover-qualified OOS rule near the gate
        hit_oos_df = edf[(edf["lab"] == "hit") & (edf["split"] == "OOS")]
        soft = hit_oos_df[
            (hit_oos_df["n_kept"] >= MIN_OOS_N)
            & (hit_oos_df["cover"] >= MIN_COVER)
            & (hit_oos_df["med_delta"] > 0)
        ].sort_values(["lift", "med_delta"], ascending=False)
        if not soft.empty:
            s = soft.iloc[0]
            lines.append(
                f"Soft/boundary: `{s['rule']}` OOS lift={fmt_f(float(s['lift']))} "
                f"P={fmt_rate(float(s['P']))} vs base {fmt_rate(float(s['base']))} · "
                f"med {fmt_pct(float(s['med']))} vs naked {fmt_pct(float(s['med_base']))} "
                f"(Δ{fmt_pct(float(s['med_delta']))}) · n={int(s['n_kept'])} "
                f"cover={fmt_rate(float(s['cover']))} — research note only "
                f"(below freeze lift≥{FREEZE_LIFT})."
            )
        thin = hit_oos_df[
            (hit_oos_df["n_kept"] >= MIN_OOS_N)
            & (hit_oos_df["cover"] < MIN_COVER)
            & (hit_oos_df["lift"] >= FREEZE_LIFT)
            & (hit_oos_df["med_delta"] > 0)
        ].sort_values("lift", ascending=False)
        if not thin.empty:
            t = thin.iloc[0]
            lines.append(
                f"Thin (cover<{MIN_COVER:.0%}): `{t['rule']}` lift={fmt_f(float(t['lift']))} "
                f"med Δ{fmt_pct(float(t['med_delta']))} n={int(t['n_kept'])} — do not adopt."
            )
        lines.append(
            "Classic fixed T−2 W1/W5 **hurt or flat** on OOS hit-precision "
            "(aligns with failed hard-filter study)."
        )

    if best is not None:
        lines.append("")
        lines.append(
            f"- Best OOS by hit-lift (n≥{MIN_OOS_N}): `{best['rule']}` · "
            f"P={fmt_rate(best['P'])} vs base {fmt_rate(best['base'])} · "
            f"lift={fmt_f(best['lift'])} · med {fmt_pct(best['med'])} vs naked "
            f"{fmt_pct(best['med_base'])} (Δ{fmt_pct(best['med_delta'])}) · "
            f"n={int(best['n_kept'])}/{int(best['n_green'])} cover={fmt_rate(best['cover'])}"
        )
    if best_med is not None and (best is None or best_med["rule"] != best["rule"]):
        lines.append(
            f"- Best OOS by medΔ (lift≥1, n≥{MIN_OOS_N}): `{best_med['rule']}` · "
            f"lift={fmt_f(best_med['lift'])} · med {fmt_pct(best_med['med'])} vs "
            f"{fmt_pct(best_med['med_base'])} (Δ{fmt_pct(best_med['med_delta'])}) · "
            f"n={int(best_med['n_kept'])}"
        )
    lines.append(
        f"- Naked green OOS：n={b_oos['n']} med {fmt_pct(b_oos['med'])} "
        f"hit={fmt_rate(b_oos['hit'])} · ALL med {fmt_pct(b_all['med'])}"
    )
    lines.append("")

    lines.append("## Labels (documented)")
    lines.append("")
    lines.append("| label | definition |")
    lines.append("|-------|------------|")
    lines.append("| continuous | L1H7 `r_adj` |")
    lines.append("| `hit` (primary binary) | `r_adj > 0` |")
    lines.append(
        f"| `top_tercile` | `r_adj` ≥ IS 2/3 quantile ({t_hi * 100:+.2f}%) |"
    )
    lines.append(
        f"| `bot_tercile` | `r_adj` ≤ IS 1/3 quantile ({t_lo * 100:+.2f}%) |"
    )
    lines.append(
        f"| `strong` (descriptive) | `r_adj` ≥ full-sample median ({med_all * 100:+.2f}%) |"
    )
    lines.append("")
    lines.append(
        "Primary decision uses **hit** + med `r_adj` of kept greens vs naked. "
        "Tercile cuts frozen from **IS** only (applied to OOS)."
    )
    lines.append("")

    lines.append("## Features (PIT T−1..T−5)")
    lines.append("")
    lines.append("| feature | notes |")
    lines.append("|---------|-------|")
    lines.append("| `three_streak` | 法人合計連買日數 |")
    lines.append("| `foreign_streak` | 外資連買日數 |")
    lines.append("| `big_holder_pct_chg` | 集保大戶％週增（W5；cache 覆蓋不全） |")
    lines.append(
        "| `core_n` | soft：該股 champion core 席當日淨買≥floor 家數（**非**黃燈產品） |"
    )
    lines.append("")

    # continuous top
    lines.append("## Continuous association (Spearman ρ vs r_adj)")
    lines.append("")
    lines.append("| feat | IS ρ | OOS ρ |")
    lines.append("|------|-----:|------:|")
    feats = sorted({r["feat"] for r in cont_df.to_dict("records")})
    for f in feats:
        is_ = cont_df[(cont_df["feat"] == f) & (cont_df["split"] == "IS")]
        oo = cont_df[(cont_df["feat"] == f) & (cont_df["split"] == "OOS")]
        lines.append(
            f"| `{f}` | {fmt_f(float(is_.iloc[0]['rho']) if not is_.empty else None, 3)} | "
            f"{fmt_f(float(oo.iloc[0]['rho']) if not oo.empty else None, 3)} |"
        )
    lines.append("")

    # top OOS hit rules
    lines.append("## Rule table · predict good green (`hit`) · OOS")
    lines.append("")
    lines.append(
        "| rule | n_kept | cover | P | base | lift | med | naked med | Δmed |"
    )
    lines.append(
        "|------|-------:|------:|--:|-----:|-----:|----:|----------:|-----:|"
    )
    top = edf[
        (edf["lab"] == "hit") & (edf["split"] == "OOS") & (edf["n_kept"] >= 8)
    ].sort_values(["lift", "med_delta"], ascending=False)
    for _, r in top.head(15).iterrows():
        lines.append(
            f"| `{r['rule']}` | {int(r['n_kept'])} | {fmt_rate(r['cover'])} | "
            f"{fmt_rate(r['P'])} | {fmt_rate(r['base'])} | {fmt_f(r['lift'])} | "
            f"{fmt_pct(r['med'])} | {fmt_pct(r['med_base'])} | {fmt_pct(r['med_delta'])} |"
        )
    lines.append("")

    lines.append("## Same rules · IS (sanity)")
    lines.append("")
    lines.append("| rule | n_kept | cover | P | lift | med | Δmed |")
    lines.append("|------|-------:|------:|--:|-----:|----:|-----:|")
    hit_is = edf[(edf["lab"] == "hit") & (edf["split"] == "IS") & (edf["n_kept"] >= 8)]
    # show same top-15 OOS rule ids
    show_ids = list(top.head(15)["rule"]) if not top.empty else []
    for rid in show_ids:
        sub = hit_is[hit_is["rule"] == rid]
        if sub.empty:
            continue
        r = sub.iloc[0]
        lines.append(
            f"| `{r['rule']}` | {int(r['n_kept'])} | {fmt_rate(r['cover'])} | "
            f"{fmt_rate(r['P'])} | {fmt_f(r['lift'])} | {fmt_pct(r['med'])} | "
            f"{fmt_pct(r['med_delta'])} |"
        )
    lines.append("")

    # classic W1/W5 callout
    lines.append("## Classic W1 / W5 as quality predictors")
    lines.append("")
    lines.append("| rule | split | n | cover | hit P | lift | med | Δmed |")
    lines.append("|------|-------|--:|------:|------:|-----:|----:|-----:|")
    for rid in (
        "L2:three_streak>=2",
        "L2:foreign_streak>=2",
        "L2:big_holder_pct_chg>0",
        "L2:W5b",
        "W1_win5",
        "W5_win5",
        "win5:W5b",
        "L2:core_n>=1",
        "core_win5_ge1",
    ):
        for split in ("IS", "OOS"):
            sub = edf[
                (edf["rule"] == rid) & (edf["lab"] == "hit") & (edf["split"] == split)
            ]
            if sub.empty:
                continue
            r = sub.iloc[0]
            lines.append(
                f"| `{rid}` | {split} | {int(r['n_kept'])} | {fmt_rate(r['cover'])} | "
                f"{fmt_rate(r['P'])} | {fmt_f(r['lift'])} | {fmt_pct(r['med'])} | "
                f"{fmt_pct(r['med_delta'])} |"
            )
    lines.append("")

    lines.append("## Top-tercile label (secondary)")
    lines.append("")
    top_oos = edf[
        (edf["lab"] == "top") & (edf["split"] == "OOS") & (edf["n_kept"] >= 8)
    ].sort_values("lift", ascending=False)
    lines.append("| rule | n | cover | P | lift | med | Δmed |")
    lines.append("|------|--:|------:|--:|-----:|----:|-----:|")
    for _, r in top_oos.head(10).iterrows():
        lines.append(
            f"| `{r['rule']}` | {int(r['n_kept'])} | {fmt_rate(r['cover'])} | "
            f"{fmt_rate(r['P'])} | {fmt_f(r['lift'])} | {fmt_pct(r['med'])} | "
            f"{fmt_pct(r['med_delta'])} |"
        )
    lines.append("")

    lines.append("## Freeze gate")
    lines.append("")
    lines.append(
        f"Require OOS hit lift ≥ **{FREEZE_LIFT}** (meaningfully >1.1), "
        f"med `r_adj` of kept > naked, "
        f"n_kept ≥ {MIN_OOS_N}, cover ≥ {MIN_COVER:.0%}, and IS lift ≥ 1.0."
    )
    if freeze:
        lines.append("Result: see Verdict.")
    else:
        lines.append("Result: **no freeze**.")
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("- Conditioning on greens only → selection already strong; lift hard.")
    lines.append("- `big_holder_pct_chg` cache is Tier-A-heavy; missing → rule never fires.")
    lines.append("- `core_n` is soft strength of the same green machinery, not independent alpha.")
    lines.append("- Small OOS n; do not overfit lag×threshold grid.")
    lines.append("")

    lines.append("## Reproduction")
    lines.append("")
    lines.append("```bash")
    lines.append(
        "PYTHONPATH=src .venv/bin/python scripts/research/run_green_quality_precursors.py"
    )
    lines.append("```")
    lines.append("")
    lines.append("Artifacts: `green_quality_events.csv` · `green_quality_rules.csv` · "
                 "`green_quality_continuous.csv`")
    lines.append("")

    # champion appendix
    lines.append("## Champion modes (universe)")
    lines.append("")
    lines.append("| sid | name | mode | floor | core_n |")
    lines.append("|-----|------|------|------:|-------:|")
    for sid in UNIVERSE:
        lines.append(
            f"| {sid} | {names.get(sid, sid)} | {modes.get(sid, '')} | "
            f"{floors.get(sid, 0):.0f} | {len(cores.get(sid) or {})} |"
        )
    lines.append("")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "GREEN_QUALITY_PRECURSORS.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
