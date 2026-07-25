#!/usr/bin/env python3
"""2327 國巨 · expert-pool green × chip overlay (veto / confirm).

Research only. Does NOT write config/strategy.yaml or config/order.yaml.

Protocol SSOT (knowledge ledger):
  mode=回看1日共識 · floor≥0.5億 · H10_skip label · L1H7 r_adj · cost 30bps · β=1.15
  greens: expert_pool/knowledge/trades/2327.json via load_whale_events

Arms (priority):
  veto: rel_dump · ft3_rel_20 Q1 (expanding past-only) · optional FT climax (FT_z≥2)
  confirm: FT3>0
  soft only: rel_scoop ∧ FT_resid_z>0

Out: reports/research/chip-overlays/2327_ep_chip_overlay/
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from ops_live_ta import ft3_rel_20_from_ft_series  # noqa: E402
from research.chen_chip.whale_events import load_whale_events  # noqa: E402
from research.chip_relative.panel import (  # noqa: E402
    build_relative_chip_panel,
    load_market_ft_totals,
)

SID = "2327"
OOS = "2026-01-01"
WARMUP = "2024-07-01"
START = "2025-07-01"
END = "2026-07-17"
EPISODE_GAP_DAYS = 5
BOOT_N = 2000
RNG = np.random.default_rng(2327)

OUT = ROOT / "reports/research/chip-overlays/2327_ep_chip_overlay"
CACHE = (
    ROOT
    / "reports/research/chip-overlays/market_relative_chip/cache_market_ft_totals.csv"
)
DB = ROOT / "data" / "stocks.db"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _stats(s: pd.Series) -> dict:
    s = s.dropna().astype(float)
    if s.empty:
        return {"n": 0, "med": None, "mean": None, "win": None}
    return {
        "n": int(len(s)),
        "med": float(s.median()),
        "mean": float(s.mean()),
        "win": float((s > 0).mean()),
    }


def _delta(a: dict, b: dict) -> float | None:
    if a.get("med") is None or b.get("med") is None:
        return None
    return float(a["med"] - b["med"])


def _same_sign(x: float | None, y: float | None) -> bool | None:
    if x is None or y is None or (x == 0 and y == 0):
        return None
    if x == 0 or y == 0:
        return None
    return (x > 0) == (y > 0)


def episode_ids(dates: pd.Series, gap_days: int = EPISODE_GAP_DAYS) -> pd.Series:
    """Cluster signal dates within gap_days into episodes (first date keeps id)."""
    d = pd.to_datetime(dates).sort_values()
    ids = []
    ep = -1
    prev = None
    mapping: dict[pd.Timestamp, int] = {}
    for ts in d:
        if prev is None or (ts - prev).days > gap_days:
            ep += 1
        mapping[ts] = ep
        prev = ts
        ids.append(ep)
    return dates.map(lambda x: mapping[pd.Timestamp(x)])


def bootstrap_med_delta(
    kept: np.ndarray,
    base: np.ndarray,
    *,
    n: int = BOOT_N,
) -> dict:
    """Episode-level bootstrap of med(kept) − med(base)."""
    if len(kept) < 2 or len(base) < 2:
        return {"ci_lo": None, "ci_hi": None, "p_pos": None}
    deltas = np.empty(n)
    for i in range(n):
        kb = kept[RNG.integers(0, len(kept), len(kept))]
        bb = base[RNG.integers(0, len(base), len(base))]
        deltas[i] = float(np.median(kb) - np.median(bb))
    return {
        "ci_lo": float(np.quantile(deltas, 0.025)),
        "ci_hi": float(np.quantile(deltas, 0.975)),
        "p_pos": float((deltas > 0).mean()),
    }


def attach_ft3_features(panel: pd.DataFrame) -> pd.DataFrame:
    p = panel.copy()
    p["FT_shares"] = p["foreign_net"].fillna(0.0) + p["investment_trust_net"].fillna(0.0)
    p["FT3"] = p["FT_shares"].rolling(3, min_periods=2).sum()
    # expanding past-only ft3_rel_20 + Q1 flag
    ft = p["FT_shares"].tolist()
    rels: list[float | None] = []
    for i in range(len(ft)):
        rel, _, _ = ft3_rel_20_from_ft_series(ft[: i + 1])
        rels.append(rel)
    p["ft3_rel_20"] = rels
    # expanding quartile of past ft3_rel_20 (exclude today for threshold)
    q1 = []
    for i in range(len(p)):
        hist = [x for x in rels[:i] if x is not None]
        if len(hist) < 40:
            q1.append(False)
            continue
        thr = float(np.nanpercentile(hist, 25))
        cur = rels[i]
        q1.append(bool(cur is not None and cur <= thr))
    p["ft3_rel_q1"] = q1
    p["ft_climax"] = p["FT_z"].ge(2.0).fillna(False)
    # soft scoop resid
    p["scoop_resid_soft"] = (
        p["rel_scoop"].fillna(False) & p["FT_resid_z"].gt(0).fillna(False)
    )
    # simple tape regime from IX 20d return tercile (past expanding)
    p["m_r20"] = p["ix"].pct_change(20, fill_method=None)
    tape = []
    for i in range(len(p)):
        hist = p["m_r20"].iloc[:i].dropna()
        cur = p["m_r20"].iloc[i]
        if len(hist) < 60 or not np.isfinite(cur):
            tape.append("unknown")
            continue
        lo, hi = np.nanpercentile(hist, [33.3, 66.7])
        if cur <= lo:
            tape.append("cold")
        elif cur >= hi:
            tape.append("hot")
        else:
            tape.append("neutral")
    p["tape_regime"] = tape
    return p


def eval_arm(
    panel_g: pd.DataFrame,
    *,
    name: str,
    kind: str,
    mask: pd.Series,
) -> dict:
    """kind: confirm | veto | soft_confirm | baseline."""
    rows = {}
    for split, ss in (
        ("ALL", panel_g),
        ("IS", panel_g[panel_g["signal_date"].astype(str) < OOS]),
        ("OOS", panel_g[panel_g["signal_date"].astype(str) >= OOS]),
    ):
        base = _stats(ss["r_adj_pct_l1"])
        m = mask.reindex(ss.index).fillna(False)
        if kind == "baseline":
            kept = base
            dropped = {"n": 0, "med": None, "mean": None, "win": None}
            delta = 0.0
        elif kind in ("confirm", "soft_confirm"):
            kept = _stats(ss.loc[m, "r_adj_pct_l1"])
            dropped = _stats(ss.loc[~m, "r_adj_pct_l1"])
            delta = _delta(kept, base)
        else:  # veto: keep when mask is False (mask=True means veto fire)
            kept = _stats(ss.loc[~m, "r_adj_pct_l1"])
            dropped = _stats(ss.loc[m, "r_adj_pct_l1"])
            delta = _delta(kept, base)
        rows[split] = {
            "base": base,
            "kept": kept,
            "dropped_or_fail": dropped,
            "delta_med_vs_base": delta,
            "n_trigger": int(m.sum()) if kind != "baseline" else int(len(ss)),
        }
    # episode bootstrap on OOS kept vs OOS base (confirm) / OOS kept vs base (veto)
    oos = panel_g[panel_g["signal_date"].astype(str) >= OOS]
    m_oos = mask.reindex(oos.index).fillna(False)
    if kind == "baseline":
        boot = {"ci_lo": None, "ci_hi": None, "p_pos": None}
    elif kind in ("confirm", "soft_confirm"):
        kept_v = oos.loc[m_oos, "r_adj_pct_l1"].dropna().to_numpy()
        base_v = oos["r_adj_pct_l1"].dropna().to_numpy()
        # bootstrap at episode level
        ep = episode_ids(oos["signal_date"])
        oos = oos.assign(_ep=ep)
        kept_ep = (
            oos.loc[m_oos]
            .groupby("_ep")["r_adj_pct_l1"]
            .median()
            .dropna()
            .to_numpy()
        )
        base_ep = oos.groupby("_ep")["r_adj_pct_l1"].median().dropna().to_numpy()
        boot = bootstrap_med_delta(kept_ep, base_ep)
        _ = kept_v, base_v
    else:
        oos = oos.assign(_ep=episode_ids(oos["signal_date"]))
        kept_ep = (
            oos.loc[~m_oos]
            .groupby("_ep")["r_adj_pct_l1"]
            .median()
            .dropna()
            .to_numpy()
        )
        base_ep = oos.groupby("_ep")["r_adj_pct_l1"].median().dropna().to_numpy()
        boot = bootstrap_med_delta(kept_ep, base_ep)

    d_all = rows["ALL"]["delta_med_vs_base"]
    d_is = rows["IS"]["delta_med_vs_base"]
    d_oos = rows["OOS"]["delta_med_vs_base"]
    same_is_oos = _same_sign(d_is, d_oos)
    # thin-IS rule: if IS n_base < 3, primary gate is OOS delta>0 and ALL same sign as OOS
    is_thin = rows["IS"]["base"]["n"] < 3
    boot_ok = bool(
        boot.get("p_pos") is not None
        and boot["p_pos"] >= 0.70
        and boot.get("ci_lo") is not None
        and boot["ci_lo"] > -1.0  # allow mild noise, reject CI wholly straddling zero hard
    )
    # climax / 1-trigger arms need stricter: at least 2 triggers on OOS
    min_trig = 2 if kind != "baseline" else 0
    n_trig_oos = int(rows["OOS"]["n_trigger"])
    if kind == "baseline":
        pass_gate = False
        reason = "baseline"
    elif kind == "soft_confirm":
        pass_gate = False
        reason = "soft_confirm never freezes (observe only)"
    elif is_thin:
        pass_gate = bool(
            d_oos is not None
            and d_oos > 0
            and _same_sign(d_all, d_oos) is True
            and rows["OOS"]["kept"]["n"] >= 3
            and n_trig_oos >= min_trig
            and boot_ok
        )
        reason = (
            "thin_IS: OOSΔ>0 ∧ sign(ALL)=sign(OOS) ∧ n_kept≥3 ∧ n_trig≥2 "
            "∧ boot p(Δ>0)≥0.70 ∧ ci_lo>-1"
        )
    else:
        pass_gate = bool(
            d_oos is not None
            and d_oos > 0
            and same_is_oos is True
            and rows["OOS"]["kept"]["n"] >= 3
            and n_trig_oos >= min_trig
            and boot_ok
        )
        reason = (
            "IS/OOS same sign ∧ OOSΔ>0 ∧ n_kept≥3 ∧ n_trig≥2 "
            "∧ boot p(Δ>0)≥0.70 ∧ ci_lo>-1"
        )

    return {
        "arm": name,
        "kind": kind,
        "splits": rows,
        "bootstrap_oos_episode": boot,
        "same_sign_is_oos": same_is_oos,
        "is_thin": is_thin,
        "pass_gate": pass_gate,
        "gate_reason": reason,
    }


def regime_table(panel_g: pd.DataFrame, mask: pd.Series, kind: str) -> list[dict]:
    out = []
    for reg, ss in panel_g.groupby("tape_regime"):
        m = mask.reindex(ss.index).fillna(False)
        base = _stats(ss["r_adj_pct_l1"])
        if kind in ("confirm", "soft_confirm"):
            kept = _stats(ss.loc[m, "r_adj_pct_l1"])
        elif kind == "veto":
            kept = _stats(ss.loc[~m, "r_adj_pct_l1"])
        else:
            kept = base
        out.append(
            {
                "tape_regime": str(reg),
                "n_base": base["n"],
                "med_base": base["med"],
                "n_kept": kept["n"],
                "med_kept": kept["med"],
                "delta": _delta(kept, base),
            }
        )
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    log("load greens")
    greens = load_whale_events([SID]).copy()
    greens["signal_date"] = pd.to_datetime(greens["signal_date"])
    greens["episode"] = episode_ids(greens["signal_date"])

    log("build relative chip panel")
    mkt = load_market_ft_totals(
        start=date(2015, 1, 1),
        end=date(2026, 7, 24),
        cache_path=CACHE,
    )
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True)
    panel = build_relative_chip_panel(
        conn,
        sid=SID,
        start=START,
        end=END,
        warmup=WARMUP,
        market_totals=mkt,
    )
    conn.close()
    panel = attach_ft3_features(panel)

    feat_cols = [
        "date",
        "FT_shares",
        "FT3",
        "ft3_rel_20",
        "ft3_rel_q1",
        "rel_dump",
        "rel_scoop",
        "FT_z",
        "FT_resid_z",
        "ft_climax",
        "scoop_resid_soft",
        "tape_regime",
        "m_r20",
        "net_FT",
        "FT_ntd",
    ]
    g = greens.merge(
        panel[feat_cols].rename(columns={"date": "signal_date"}),
        on="signal_date",
        how="left",
    )
    g.to_csv(OUT / "green_chip_panel.csv", index=False)

    arms_def = [
        ("baseline_green", "baseline", pd.Series(True, index=g.index)),
        ("confirm_FT3_pos", "confirm", g["FT3"] > 0),
        ("veto_rel_dump", "veto", g["rel_dump"].fillna(False)),
        ("veto_ft3_rel_q1", "veto", g["ft3_rel_q1"].fillna(False)),
        ("veto_ft_climax", "veto", g["ft_climax"].fillna(False)),
        (
            "soft_scoop_resid",
            "soft_confirm",
            g["scoop_resid_soft"].fillna(False),
        ),
        # combo: confirm ∧ not rel_dump
        (
            "confirm_FT3_and_not_dump",
            "confirm",
            (g["FT3"] > 0) & (~g["rel_dump"].fillna(False)),
        ),
        (
            "veto_dump_or_q1",
            "veto",
            g["rel_dump"].fillna(False) | g["ft3_rel_q1"].fillna(False),
        ),
    ]

    results = []
    for name, kind, mask in arms_def:
        r = eval_arm(g, name=name, kind=kind, mask=mask)
        r["regime"] = regime_table(g, mask, kind)
        results.append(r)

    # summary flat table
    flat = []
    for r in results:
        for split in ("ALL", "IS", "OOS"):
            s = r["splits"][split]
            flat.append(
                {
                    "arm": r["arm"],
                    "kind": r["kind"],
                    "split": split,
                    "n_base": s["base"]["n"],
                    "med_base": s["base"]["med"],
                    "n_kept": s["kept"]["n"],
                    "med_kept": s["kept"]["med"],
                    "win_kept": s["kept"]["win"],
                    "n_trigger": s["n_trigger"],
                    "n_dropped": s["dropped_or_fail"]["n"],
                    "med_dropped": s["dropped_or_fail"]["med"],
                    "delta_med": s["delta_med_vs_base"],
                    "pass_gate": r["pass_gate"] if split == "OOS" else None,
                    "boot_ci_lo": r["bootstrap_oos_episode"]["ci_lo"]
                    if split == "OOS"
                    else None,
                    "boot_ci_hi": r["bootstrap_oos_episode"]["ci_hi"]
                    if split == "OOS"
                    else None,
                    "boot_p_pos": r["bootstrap_oos_episode"]["p_pos"]
                    if split == "OOS"
                    else None,
                    "same_sign_is_oos": r["same_sign_is_oos"],
                    "is_thin": r["is_thin"],
                }
            )
    flat_df = pd.DataFrame(flat)
    flat_df.to_csv(OUT / "arm_summary.csv", index=False)

    passed = [r for r in results if r["pass_gate"] and r["kind"] != "baseline"]
    # prefer hard veto/confirm over soft
    hard = [r for r in passed if r["kind"] != "soft_confirm"]
    freeze_arms = hard if hard else []

    freeze = {
        "spec_id": "2327_ep_chip_overlay",
        "status": "candidate_freeze" if freeze_arms else "no_arm_passed",
        "layer": "research",
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "stock_id": SID,
        "green_source": "expert_pool/knowledge/trades/2327.json",
        "champion": {
            "mode": "回看1日共識",
            "floor": "≥0.5億",
            "policy": "H10_skip",
            "hold_protocol": "L1H7",
            "cost_bps": 30,
            "beta": 1.15,
        },
        "oos_cut": OOS,
        "n_greens": int(len(g)),
        "n_is": int((g["signal_date"].astype(str) < OOS).sum()),
        "n_oos": int((g["signal_date"].astype(str) >= OOS).sum()),
        "n_episodes": int(g["episode"].nunique()),
        "caveat": (
            "IS n=1 under OOS cut 2026-01-01; thin_IS + episode bootstrap "
            "(p_pos≥0.70, ci_lo>-1, n_trig≥2). Priority arms rejected on 2327 greens. "
            "Not adopted to strategy/order."
        ),
        "passed_arms": [
            {
                "arm": r["arm"],
                "kind": r["kind"],
                "delta_oos": r["splits"]["OOS"]["delta_med_vs_base"],
                "delta_all": r["splits"]["ALL"]["delta_med_vs_base"],
                "n_kept_oos": r["splits"]["OOS"]["kept"]["n"],
                "med_kept_oos": r["splits"]["OOS"]["kept"]["med"],
                "boot": r["bootstrap_oos_episode"],
                "gate_reason": r["gate_reason"],
            }
            for r in freeze_arms
        ],
        "rejected_or_soft": [
            {
                "arm": r["arm"],
                "kind": r["kind"],
                "pass_gate": r["pass_gate"],
                "delta_oos": r["splits"]["OOS"]["delta_med_vs_base"],
                "n_kept_oos": r["splits"]["OOS"]["kept"]["n"],
                "boot": r["bootstrap_oos_episode"],
            }
            for r in results
            if r["arm"] not in {x["arm"] for x in freeze_arms}
            and r["kind"] != "baseline"
        ],
        "adoption": {
            "strategy_yaml": False,
            "order_yaml": False,
            "note": "等待明確「採納」指令後才寫入 strategy／order",
        },
    }
    (OUT / "2327_ep_chip_overlay_frozen.json").write_text(
        json.dumps(freeze, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (OUT / "arm_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )

    # brief report
    lines = [
        "# 2327 國巨 · EP 綠燈 × 籌碼 overlay",
        "",
        f"- Built: `{freeze['built_at']}`",
        f"- Greens: **{freeze['n_greens']}**（IS={freeze['n_is']} · OOS={freeze['n_oos']} · episodes={freeze['n_episodes']}）",
        f"- OOS cut: `{OOS}` · cost 30bps · β=1.15 · L1H7 `r_adj_pct_l1`",
        f"- Freeze status: **`{freeze['status']}`**（未寫 strategy／order）",
        "",
        "## Caveat",
        "",
        freeze["caveat"],
        "",
        "## Arm summary（OOS）",
        "",
        "| arm | kind | n_trig | n_kept | med_kept | Δmed | boot CI | p(Δ>0) | pass |",
        "|---|---|---:|---:|---:|---:|---|---:|---|",
    ]
    for r in results:
        if r["kind"] == "baseline":
            s = r["splits"]["OOS"]
            lines.append(
                f"| `{r['arm']}` | baseline | {s['n_trigger']} | {s['kept']['n']} | "
                f"{s['kept']['med']:.2f} | 0 | — | — | — |"
            )
            continue
        s = r["splits"]["OOS"]
        b = r["bootstrap_oos_episode"]
        ci = (
            f"[{b['ci_lo']:+.2f},{b['ci_hi']:+.2f}]"
            if b["ci_lo"] is not None
            else "—"
        )
        pp = f"{b['p_pos']:.2f}" if b["p_pos"] is not None else "—"
        dm = s["delta_med_vs_base"]
        dm_s = f"{dm:+.2f}" if dm is not None else "nan"
        mk = s["kept"]["med"]
        mk_s = f"{mk:.2f}" if mk is not None else "nan"
        lines.append(
            f"| `{r['arm']}` | {r['kind']} | {s['n_trigger']} | {s['kept']['n']} | "
            f"{mk_s} | {dm_s} | {ci} | {pp} | {'Y' if r['pass_gate'] else 'N'} |"
        )

    lines += [
        "",
        "## Passed → freeze candidate",
        "",
    ]
    if freeze_arms:
        for a in freeze["passed_arms"]:
            lines.append(
                f"- `{a['arm']}` ({a['kind']}): OOS Δmed={a['delta_oos']:+.2f}pp · "
                f"n_kept={a['n_kept_oos']} · med={a['med_kept_oos']:.2f}"
            )
    else:
        lines.append("_None under thin_IS gate._")

    lines += [
        "",
        "## Green × chip panel（逐筆）",
        "",
        "| signal | r_adj | FT3 | ft3_rel_20 | dump | q1 | climax | scoop_soft | tape |",
        "|---|---:|---:|---:|---|---|---|---|---|",
    ]
    for _, row in g.sort_values("signal_date").iterrows():
        lines.append(
            f"| {row['signal_date'].date()} | {row['r_adj_pct_l1']:.2f} | "
            f"{row['FT3'] if pd.notna(row['FT3']) else 'nan'} | "
            f"{row['ft3_rel_20'] if pd.notna(row['ft3_rel_20']) else 'nan'} | "
            f"{'Y' if bool(row['rel_dump']) else 'N'} | "
            f"{'Y' if bool(row['ft3_rel_q1']) else 'N'} | "
            f"{'Y' if bool(row['ft_climax']) else 'N'} | "
            f"{'Y' if bool(row['scoop_resid_soft']) else 'N'} | "
            f"{row['tape_regime']} |"
        )

    lines += [
        "",
        "## Next",
        "",
        "- **不採納**：本輪無過關臂；維持 EP 2327 裸綠燈",
        "- 可選後續（另開）：跨股 EP overlay（放大 n）／更早 OOS cut；**不要**為過關而放寬閘門",
        "",
    ]
    (OUT / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")

    log(f"status={freeze['status']} passed={len(freeze_arms)}")
    print(flat_df[flat_df.split == "OOS"].to_string(index=False))
    print("wrote", OUT)


if __name__ == "__main__":
    main()
