#!/usr/bin/env python3
"""Per-stock staged funnel calibration · enrich+rebuild (same as optimize.py).

Research only. For each focus sid: P0/P1/P3/P5 + stock-specific prior.
Verdict: 显著改善 | 找不到 | 伤害报酬 (honest when n thin).

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_pool_per_stock_funnel_calib.py
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

TRADES = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
)
OUT = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/hypotheses"
    / "avoid_morphology"
)
COST = 0.003
BETA = 1.15
SOURCE = "finmind"
FOCUS = ("2303", "2327", "6515", "2344", "8046")
NAMES = {
    "2303": "聯電",
    "2327": "國巨",
    "6515": "穎崴",
    "2344": "華邦電",
    "8046": "南電",
}
MIN_INTRA = 5  # below → 找不到 for policy comparison


def median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def mean(xs: list[float]) -> float | None:
    return float(statistics.mean(xs)) if xs else None


def pctl(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(math.floor(q * (len(ys) - 1)))))
    return float(ys[i])


def load_trades() -> list[dict]:
    rows = []
    for p in sorted(TRADES.glob("*.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        trades = data if isinstance(data, list) else data.get("trades") or []
        for t in trades:
            rows.append(
                {
                    "sid": p.stem,
                    "signal_date": str(t["signal_date"])[:10],
                    "entry_date": str(t["entry_date"])[:10],
                    "exit_date": str(t["exit_date"])[:10],
                    "entry_open": float(t["entry_open"]),
                    "exit_close": float(t["exit_close"]),
                    "r_pct": float(t["r_pct"]),
                    "r_ix_pct": float(t["r_ix_pct"]),
                    "r_adj": float(t["r_adj_pct"]),
                    "branches": t.get("branches") or [],
                }
            )
    return rows


def morning(conn, sid: str, d: str, end: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT minute, open, high, low, close, source FROM stock_kbar_1m
        WHERE stock_id=? AND trade_date=? AND minute>='09:00:00' AND minute<=?
        ORDER BY minute
        """,
        (sid, d, end),
    ).fetchall()
    if not rows:
        return []
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(str(r[5]), []).append(
            {
                "minute": str(r[0]),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
            }
        )
    if "finmind" in by and len(by["finmind"]) >= 3:
        return by["finmind"]
    return max(by.values(), key=len) if by else []


def agg(bars: list[dict]) -> dict | None:
    if len(bars) < 2:
        return None
    o = bars[0]["open"]
    if o <= 0:
        return None
    return {
        "open": o,
        "high": max(b["high"] for b in bars),
        "low": min(b["low"] for b in bars),
        "close": bars[-1]["close"],
        "ret": bars[-1]["close"] / o - 1,
        "dd": min(b["low"] for b in bars) / o - 1,
        "n": len(bars),
    }


def t_day_feats(conn, sid: str, sig: str) -> dict:
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date<=?
        ORDER BY trade_date DESC LIMIT 8
        """,
        (sid, SOURCE, sig),
    ).fetchall()
    bars = [
        {
            "d": str(r[0]),
            "o": float(r[1]),
            "h": float(r[2]),
            "l": float(r[3]),
            "c": float(r[4]),
        }
        for r in reversed(rows)
    ]
    out = {
        "hot5": False,
        "t_reject": False,
        "t_weak_close": False,
        "max_today_yi": 0.0,
        "ok": False,
    }
    if not bars or bars[-1]["d"] != sig:
        return out
    t = bars[-1]
    prev5 = bars[-6:-1] if len(bars) >= 6 else []
    rng = t["h"] - t["l"]
    close_pos = (t["c"] - t["l"]) / rng if rng > 0 else 0.5
    body = t["c"] / t["o"] - 1 if t["o"] else 0.0
    black = t["c"] < t["o"]
    ret5 = None
    if len(prev5) == 5 and prev5[0]["c"] > 0:
        ret5 = prev5[-1]["c"] / prev5[0]["c"] - 1
    out["hot5"] = bool(ret5 is not None and ret5 >= 0.20)
    out["t_reject"] = (black and close_pos < 0.33) or body < -0.03
    out["t_weak_close"] = bool(black and close_pos < 0.20)
    out["ok"] = True
    return out


def enrich(conn, rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        b5 = morning(conn, r["sid"], r["entry_date"], "09:05:00")
        b25 = morning(conn, r["sid"], r["entry_date"], "09:25:00")
        a5 = agg(b5)
        a25 = agg(b25)
        open_px = a5["open"] if a5 else (a25["open"] if a25 else r["entry_open"])
        px05 = a5["close"] if a5 else None
        px25 = a25["close"] if a25 else None
        t_close = conn.execute(
            "SELECT close FROM stock_daily_bars WHERE stock_id=? AND trade_date=? AND source=?",
            (r["sid"], r["signal_date"], SOURCE),
        ).fetchone()
        tc = float(t_close[0]) if t_close else None
        gap = (open_px / tc - 1) if tc and open_px else None
        td = t_day_feats(conn, r["sid"], r["signal_date"])
        max_yi = 0.0
        for b in r["branches"]:
            if "今" in str(b.get("role") or ""):
                max_yi = max(max_yi, float(b.get("amt_yi") or 0.0))
        td["max_today_yi"] = max_yi
        fail25 = False
        if a25:
            fail25 = a25["high"] > a25["open"] * 1.01 and a25["close"] < a25["open"]
        perfect = False
        if gap is not None and a5:
            perfect = gap <= 0.03 and a5["ret"] >= -0.005 and a5["dd"] > -0.02
        chase_weak = False
        if gap is not None and a5:
            chase_weak = gap >= 0.03 and a5["ret"] < -0.01 and a5["dd"] < -0.02
        out.append(
            {
                **r,
                "gap": gap,
                "open_px": open_px,
                "px05": px05,
                "px25": px25,
                "a5": a5,
                "a25": a25,
                "fail25": fail25,
                "perfect": perfect,
                "chase_weak": chase_weak,
                "td": td,
                "has_intra": a5 is not None and a25 is not None,
            }
        )
    return out


def rebuild_radj(row: dict, entry_px: float) -> float | None:
    if entry_px is None or entry_px <= 0:
        return None
    r_s = row["exit_close"] / entry_px - 1 - COST
    r_ix = row["r_ix_pct"] / 100.0
    return (r_s - BETA * r_ix) * 100.0


@dataclass
class Decision:
    action: str
    reason: str


PolicyFn = Callable[[dict], Decision]


def metrics(rows: list[dict], decisions: list[Decision]) -> dict:
    kept_adj = []
    skipped_base = []
    naive = [r["r_adj"] for r in rows]
    actions = {"SKIP": 0, "OPEN": 0, "E05": 0, "E25": 0}
    for r, d in zip(rows, decisions):
        actions[d.action] = actions.get(d.action, 0) + 1
        if d.action == "SKIP":
            skipped_base.append(r["r_adj"])
            continue
        px = {
            "OPEN": r["open_px"],
            "E05": r["px05"] or r["open_px"],
            "E25": r["px25"] or r["px05"] or r["open_px"],
        }[d.action]
        radj = rebuild_radj(r, px)
        if radj is not None:
            kept_adj.append(radj)
    return {
        "n": len(rows),
        "n_kept": len(kept_adj),
        "n_skip": actions.get("SKIP", 0),
        "skip_rate": actions.get("SKIP", 0) / len(rows) if rows else 0,
        "actions": actions,
        "naive_med": median(naive),
        "naive_p10": pctl(naive, 0.10),
        "kept_med": median(kept_adj),
        "kept_p10": pctl(kept_adj, 0.10),
        "skip_med_naive": median(skipped_base),
        "delta_med_vs_naive": (
            (median(kept_adj) - median(naive)) if kept_adj and naive else None
        ),
        "delta_p10_vs_naive": (
            (pctl(kept_adj, 0.10) - pctl(naive, 0.10))
            if kept_adj and naive
            else None
        ),
    }


def pol_naive(r: dict) -> Decision:
    return Decision("OPEN", "naive_L1")


def pol_user_tree(r: dict) -> Decision:
    if not r["has_intra"] or r["gap"] is None:
        return Decision("OPEN", "no_intra_fallback_open")
    g = r["gap"]
    if g >= 0.05:
        if r["perfect"]:
            return Decision("E05", "gap5_but_perfect05")
        if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
            return Decision("E25", "gap5_wait25_stable")
        return Decision("SKIP", "gap5_still_weak25")
    if g >= 0.02:
        if r["perfect"]:
            return Decision("E05", "gap2_5_perfect")
        if r["chase_weak"]:
            if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
                return Decision("E25", "gap2_5_chaseweak_then25ok")
            return Decision("SKIP", "gap2_5_chaseweak_skip")
        if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
            return Decision("E25", "gap2_5_ambiguous_25ok")
        return Decision("SKIP", "gap2_5_ambiguous_skip")
    if r["perfect"]:
        return Decision("E05", "lowgap_perfect")
    if r["chase_weak"]:
        if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
            return Decision("E25", "lowgap_chaseweak_25ok")
        return Decision("SKIP", "lowgap_chaseweak_skip")
    if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
        return Decision("E25", "lowgap_amb_25ok")
    return Decision("SKIP", "lowgap_amb_skip")


def pol_aggressive05(r: dict) -> Decision:
    if not r["has_intra"] or r["gap"] is None:
        return Decision("OPEN", "fallback")
    if r["gap"] >= 0.05 or r["chase_weak"]:
        if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
            return Decision("E25", "risk_wait25")
        return Decision("SKIP", "risk_skip")
    return Decision("E05", "aggr05")


def pol_ret25_m2_only(r: dict) -> Decision:
    if not r["has_intra"]:
        return Decision("OPEN", "fallback")
    if (r["a25"] and r["a25"]["ret"] <= -0.02) or r["fail25"]:
        return Decision("SKIP", "ret25_m2_or_fail")
    return Decision("OPEN", "ok_open")


def _wait25_or_skip(r: dict, tag: str) -> Decision:
    if r["a25"] and r["a25"]["ret"] > 0 and not r["fail25"]:
        return Decision("E25", f"{tag}_wait25")
    return Decision("SKIP", f"{tag}_skip")


def _downgrade_open05_to_25(r: dict, base: Decision, tag: str) -> Decision:
    if base.action in ("OPEN", "E05"):
        return _wait25_or_skip(r, tag)
    return base


def pol_2303_prior(r: dict) -> Decision:
    """T_REJECT → force wait25 / skip (never open@05 on reject day)."""
    base = pol_user_tree(r)
    if r["td"].get("t_reject"):
        return _downgrade_open05_to_25(r, base, "2303_treject")
    return base


def pol_2344_prior(r: dict) -> Decision:
    """HOT5 or MAX≥5億 → wait25 / skip."""
    base = pol_user_tree(r)
    risk = r["td"].get("hot5") or (r["td"].get("max_today_yi") or 0) >= 5
    if risk:
        return _downgrade_open05_to_25(r, base, "2344_hot5max5")
    return base


def pol_6515_prior(r: dict) -> Decision:
    """T弱收 (black ∧ close_pos<0.20) → wait25 / skip."""
    base = pol_user_tree(r)
    if r["td"].get("t_weak_close"):
        return _downgrade_open05_to_25(r, base, "6515_tweak")
    return base


def pol_2327_prior(r: dict) -> Decision:
    """Minimal: P5 ret25≤−2% only."""
    return pol_ret25_m2_only(r)


def pol_8046_prior(r: dict) -> Decision:
    """No NOT_PERFECT; gap≥5% + ret25≤−2% / fail25."""
    if not r["has_intra"] or r["gap"] is None:
        return Decision("OPEN", "fallback")
    if r["gap"] >= 0.05:
        return _wait25_or_skip(r, "8046_gap5")
    if (r["a25"] and r["a25"]["ret"] <= -0.02) or r["fail25"]:
        return Decision("SKIP", "8046_ret25_skip")
    return Decision("OPEN", "8046_ok_open")


COMMON = {
    "P0_naive_L1": pol_naive,
    "P1_user_tree": pol_user_tree,
    "P3_aggressive05": pol_aggressive05,
    "P5_ret25_m2_only": pol_ret25_m2_only,
}

PRIOR_KEY = {
    "2303": "P_prior_T_REJECT",
    "2327": "P_prior_minimal_P5",
    "6515": "P_prior_T_weak_close",
    "2344": "P_prior_HOT5_MAX5",
    "8046": "P_prior_gap5_ret25",
}

PRIOR_FN = {
    "2303": pol_2303_prior,
    "2327": pol_2327_prior,
    "6515": pol_6515_prior,
    "2344": pol_2344_prior,
    "8046": pol_8046_prior,
}

PRIOR_DESC = {
    "2303": "T_REJECT → 強制等25 / SKIP",
    "2327": "偏 P5 最小干預（ret25≤−2% skip）",
    "6515": "T弱收 → 等25 / SKIP",
    "2344": "HOT5 或 MAX≥5億 → 等25 / SKIP",
    "8046": "禁用 NOT_PERFECT；gap≥5% + ret25≤−2%",
}


def verdict(m: dict, *, n_intra: int) -> str:
    if n_intra < MIN_INTRA:
        return "找不到（n 不足）"
    if m["n_kept"] < 3:
        return "找不到（保留 n 不足）"
    dm = m["delta_med_vs_naive"]
    dp = m["delta_p10_vs_naive"]
    if dm is None:
        return "找不到"
    if dm <= -0.75:
        return "伤害报酬"
    if dm >= 0.50 and (dp is None or dp >= 0):
        skip_med = m["skip_med_naive"]
        kept_med = m["kept_med"]
        if skip_med is not None and kept_med is not None and skip_med < kept_med - 0.25:
            return "显著改善"
        if dm >= 1.0 or (dp is not None and dp >= 0.75):
            return "显著改善"
    if dm > 0 and dp is not None and dp > 0:
        return "显著改善"
    if dm < -0.25:
        return "伤害报酬"
    return "找不到"


def best_policy(sid: str, table: dict[str, dict]) -> dict:
    scored = []
    for name, m in table.items():
        if name == "P0_naive_L1":
            continue
        v = m["verdict"]
        if v == "显著改善":
            score = (m["delta_med_vs_naive"] or 0) + 0.5 * (m["delta_p10_vs_naive"] or 0)
            scored.append((score, name, m))
    if not scored:
        return {"pick": None, "reason": "本股無顯著改善政策"}
    scored.sort(reverse=True)
    _, name, m = scored[0]
    return {
        "pick": name,
        "verdict": m["verdict"],
        "delta_med": m["delta_med_vs_naive"],
        "delta_p10": m["delta_p10_vs_naive"],
        "skip_rate": m["skip_rate"],
    }


def run_calib(rows: list[dict]) -> dict:
    by_sid: dict[str, list[dict]] = {sid: [] for sid in FOCUS}
    for r in rows:
        if r["sid"] in by_sid:
            by_sid[r["sid"]].append(r)

    stocks = {}
    for sid in FOCUS:
        all_rows = by_sid[sid]
        intra = [r for r in all_rows if r["has_intra"] and r["gap"] is not None]
        policies: dict[str, dict] = {}
        for pname, fn in COMMON.items():
            m = metrics(intra, [fn(r) for r in intra])
            m["verdict"] = verdict(m, n_intra=len(intra))
            policies[pname] = m
        prior_name = PRIOR_KEY[sid]
        prior_fn = PRIOR_FN[sid]
        pm = metrics(intra, [prior_fn(r) for r in intra])
        pm["verdict"] = verdict(pm, n_intra=len(intra))
        pm["description"] = PRIOR_DESC[sid]
        policies[prior_name] = pm
        stocks[sid] = {
            "name": NAMES[sid],
            "n_all": len(all_rows),
            "n_intra": len(intra),
            "prior_description": PRIOR_DESC[sid],
            "policies": policies,
            "recommendation": best_policy(sid, policies),
        }
    return {
        "protocol": {
            "method": "enrich+rebuild (optimize.py)",
            "cost_bps": 30,
            "beta_ix": BETA,
            "min_intra_for_verdict": MIN_INTRA,
            "verdict_rules": "Δmed≥+0.50pp 且 Δp10≥0 → 显著改善；Δmed≤−0.75 → 伤害报酬；n<5 → 找不到",
        },
        "focus": list(FOCUS),
        "stocks": stocks,
    }


def fmt_pp(x: float | None) -> str:
    return f"{x:+.2f}" if x is not None else "—"


def fmt_med(x: float | None) -> str:
    return f"{x:.2f}" if x is not None else "—"


def write_md(res: dict) -> None:
    lines = [
        "# 專家池 · 逐股漏斗校準（H_FUNNEL_PER_STOCK_CALIB）",
        "",
        "Research only · enrich+rebuild 同 `run_expert_pool_staged_funnel_optimize.py`",
        "",
        f"- 判定門檻：intra n≥{MIN_INTRA} 才評 verdict；Δmed vs naive L1",
        "- **显著改善** = 中位超額↑且左尾不惡化；**找不到** = n 薄或無穩定 uplift；"
        "**伤害报酬** = 中位明顯變差",
        "",
    ]
    for sid in FOCUS:
        s = res["stocks"][sid]
        lines += [
            f"## {sid} {s['name']}",
            "",
            f"- 樣本：全 {s['n_all']} · 有 5+25 分K **{s['n_intra']}**",
            f"- 先驗：**{s['prior_description']}**",
            "",
            "| policy | n_kept | skip% | kept_med | Δmed | kept_p10 | Δp10 | skip組med | 判定 |",
            "|--------|-------:|------:|---------:|-----:|---------:|-----:|----------:|------|",
        ]
        order = [
            "P0_naive_L1",
            "P1_user_tree",
            "P3_aggressive05",
            "P5_ret25_m2_only",
            PRIOR_KEY[sid],
        ]
        for pname in order:
            m = s["policies"][pname]
            label = pname
            if pname == PRIOR_KEY[sid]:
                label = f"**{pname}**"
            lines.append(
                "| {name} | {nk} | {sr:.0%} | {km} | {dm} | {kp10} | {dp10} | {sm} | {v} |".format(
                    name=label,
                    nk=m["n_kept"],
                    sr=m["skip_rate"],
                    km=fmt_med(m["kept_med"]),
                    dm=fmt_pp(m["delta_med_vs_naive"]),
                    kp10=fmt_med(m["kept_p10"]),
                    dp10=fmt_pp(m["delta_p10_vs_naive"]),
                    sm=fmt_med(m["skip_med_naive"]),
                    v=m["verdict"],
                )
            )
        rec = s["recommendation"]
        if rec.get("pick"):
            lines += [
                "",
                f"**建議**：{rec['pick']}（{rec['verdict']} · Δmed {fmt_pp(rec['delta_med'])} · "
                f"Δp10 {fmt_pp(rec['delta_p10'])} · skip {rec['skip_rate']:.0%}）",
            ]
        else:
            lines += ["", f"**建議**：{rec['reason']}"]
        lines.append("")

    lines += [
        "## 跨股結論",
        "",
        "| sid | 名稱 | n_intra | 最佳非 naive | 判定 | Δmed | Δp10 |",
        "|-----|------|--------:|--------------|------|-----:|-----:|",
    ]
    for sid in FOCUS:
        s = res["stocks"][sid]
        rec = s["recommendation"]
        pick = rec.get("pick") or "—"
        v = rec.get("verdict") or rec.get("reason", "—")
        lines.append(
            f"| {sid} | {s['name']} | {s['n_intra']} | {pick} | {v} | "
            f"{fmt_pp(rec.get('delta_med'))} | {fmt_pp(rec.get('delta_p10'))} |"
        )
    lines.append("")
    (OUT / "H_FUNNEL_PER_STOCK_CALIB.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = connect(DEFAULT_DB_PATH)
    rows = enrich(conn, load_trades())
    conn.close()
    res = run_calib(rows)
    (OUT / "H_FUNNEL_PER_STOCK_CALIB.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_md(res)
    print(f"wrote {OUT / 'H_FUNNEL_PER_STOCK_CALIB.md'}")
    for sid in FOCUS:
        s = res["stocks"][sid]
        rec = s["recommendation"]
        print(
            sid,
            "n_intra",
            s["n_intra"],
            "best",
            rec.get("pick"),
            rec.get("verdict") or rec.get("reason"),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
