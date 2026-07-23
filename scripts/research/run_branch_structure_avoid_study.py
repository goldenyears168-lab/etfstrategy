#!/usr/bin/env python3
"""Expert-pool branch-structure avoid factors (complement to price morphology).

Tests n_today, yesterday-dominant, single-seat extremes, yday-dump suspicion,
and cross rules with HOT5 / T_REJECT.

Research only · 未採納.

  PYTHONPATH=src .venv/bin/python scripts/research/run_branch_structure_avoid_study.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402

TRADES_DIR = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
)
OUT_DIR = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/hypotheses/avoid_morphology"
)
OUT_JSON = OUT_DIR / "H_BRANCH_STRUCTURE_AVOID.json"
OUT_MD = OUT_DIR / "H_BRANCH_STRUCTURE_AVOID.md"
FOCUS_SIDS = ("2303", "2327", "6515", "2344", "8046")
SOURCE = "finmind"
YDAY_DUMP_YI = 3.0  # 779Z-type: large yday amt on 昨 seat


def med(xs: list[float]) -> float | None:
    return float(median(xs)) if xs else None


def summarize_group(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "med_r_adj": None, "mean_r_adj": None, "win_rate": None}
    return {
        "n": len(xs),
        "med_r_adj": round(med(xs), 2),
        "mean_r_adj": round(sum(xs) / len(xs), 2),
        "win_rate": round(sum(1 for x in xs if x > 0) / len(xs), 3),
    }


def compare_skip_keep(all_r: list[float], mask_skip: list[bool]) -> dict:
    skip = [r for r, m in zip(all_r, mask_skip) if m]
    keep = [r for r, m in zip(all_r, mask_skip) if not m]
    sk = summarize_group(skip)
    kp = summarize_group(keep)
    d_med = None
    if sk["med_r_adj"] is not None and kp["med_r_adj"] is not None:
        d_med = round(sk["med_r_adj"] - kp["med_r_adj"], 2)
    return {
        "skip": sk,
        "keep": kp,
        "delta_med_skip_minus_keep": d_med,
        "skip_share": round(len(skip) / len(all_r), 3) if all_r else 0.0,
    }


def branch_feats(branches: list[dict]) -> dict:
    nt = sum(1 for b in branches if "今" in str(b.get("role", "")))
    ny = sum(1 for b in branches if "昨" in str(b.get("role", "")))
    ny_only = sum(
        1
        for b in branches
        if "昨" in str(b.get("role", "")) and "今" not in str(b.get("role", ""))
    )
    today_amts = [
        float(b.get("amt_yi") or 0)
        for b in branches
        if "今" in str(b.get("role", ""))
    ]
    max_today_yi = max(today_amts) if today_amts else 0.0
    yday_dump = any(
        "昨" in str(b.get("role", ""))
        and float(b.get("amt_yday_yi") or 0) >= YDAY_DUMP_YI
        for b in branches
    )
    max_yday_yi = max(
        (float(b.get("amt_yday_yi") or 0) for b in branches if "昨" in str(b.get("role", ""))),
        default=0.0,
    )
    return {
        "n_today": nt,
        "n_yday": ny,
        "n_yday_only": ny_only,
        "max_today_yi": round(max_today_yi, 3),
        "yday_dump_ge3yi": yday_dump,
        "max_yday_yi": round(max_yday_yi, 3),
    }


def load_daily_ohlc(conn) -> dict[tuple[str, str], dict]:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, open, high, low, close
        FROM stock_daily_bars
        WHERE source=? AND trade_date BETWEEN '2024-05-01' AND '2026-08-31'
          AND close>0
        ORDER BY stock_id, trade_date
        """,
        (SOURCE,),
    ).fetchall()
    out: dict[tuple[str, str], dict] = {}
    by_sid: dict[str, list[tuple[str, float]]] = {}
    for sid, d, o, h, lo, c in rows:
        o = float(o) if o else float(c)
        h, lo, c = float(h), float(lo), float(c)
        out[(sid, d)] = {"o": o, "h": h, "l": lo, "c": c}
        by_sid.setdefault(sid, []).append((d, c))
    # ret5 lookup
    ret5: dict[tuple[str, str], float] = {}
    for sid, series in by_sid.items():
        series.sort()
        for i, (d, c) in enumerate(series):
            if i < 5:
                continue
            c5 = series[i - 5][1]
            if c5 > 0:
                ret5[(sid, d)] = (c / c5 - 1.0) * 100.0
    for k, v in out.items():
        v["ret5_pct"] = ret5.get(k)
    return out


def t_day_morph(ohlc: dict | None) -> dict:
    if not ohlc:
        return {
            "body_pct": None,
            "close_pos": None,
            "t_green": None,
            "hot5": None,
            "t_reject": None,
        }
    o, h, lo, c = ohlc["o"], ohlc["h"], ohlc["l"], ohlc["c"]
    body = (c / o - 1.0) * 100.0 if o > 0 else None
    span = h - lo
    close_pos = (c - lo) / span if span > 0 else None
    black = c < o if o else False
    t_reject = False
    if body is not None:
        if body < -3.0:
            t_reject = True
        elif black and close_pos is not None and close_pos < 0.33:
            t_reject = True
    ret5 = ohlc.get("ret5_pct")
    hot5 = ret5 is not None and ret5 >= 20.0
    return {
        "body_pct": round(body, 2) if body is not None else None,
        "close_pos": round(close_pos, 3) if close_pos is not None else None,
        "t_green": c > o,
        "ret5_pct": round(ret5, 2) if ret5 is not None else None,
        "hot5": hot5,
        "t_reject": t_reject,
    }


def load_all_trades() -> list[dict]:
    rows = []
    for p in sorted(TRADES_DIR.glob("*.json")):
        doc = json.loads(p.read_text())
        sid = doc["stock_id"]
        for t in doc.get("trades") or []:
            rows.append(
                {
                    "sid": sid,
                    "name": doc.get("stock_name", sid),
                    "tier": doc.get("tier"),
                    "signal_date": t["signal_date"],
                    "r_adj_pct": float(t["r_adj_pct"]),
                    "branches": t.get("branches") or [],
                }
            )
    return rows


def eval_rules(rows: list[dict], p90_max_yi: float, p75_max_yi: float) -> dict[str, list[bool]]:
    masks: dict[str, list[bool]] = {}
    for label, fn in [
        ("n_today_eq1", lambda r: r["bf"]["n_today"] == 1),
        ("n_today_ge2", lambda r: r["bf"]["n_today"] >= 2),
        ("n_today_ge3", lambda r: r["bf"]["n_today"] >= 3),
        ("n_today_eq0", lambda r: r["bf"]["n_today"] == 0),
        ("yday_dom_t1_y1", lambda r: r["bf"]["n_today"] == 1 and r["bf"]["n_yday"] >= 1),
        ("yday_dom_t0", lambda r: r["bf"]["n_today"] == 0),
        ("max_today_ge_p90", lambda r: r["bf"]["max_today_yi"] >= p90_max_yi),
        ("max_today_ge_p75", lambda r: r["bf"]["max_today_yi"] >= p75_max_yi),
        ("max_today_ge2yi", lambda r: r["bf"]["max_today_yi"] >= 2.0),
        ("max_today_ge5yi", lambda r: r["bf"]["max_today_yi"] >= 5.0),
        ("max_today_ge10yi", lambda r: r["bf"]["max_today_yi"] >= 10.0),
        (
            "yday_dump_t_green",
            lambda r: r["bf"]["yday_dump_ge3yi"] and r["tm"]["t_green"] is True,
        ),
        (
            "yday_dump_t_green_ge2yi",
            lambda r: r["bf"]["max_yday_yi"] >= 2.0
            and r["bf"]["n_yday"] >= 1
            and r["tm"]["t_green"] is True,
        ),
        ("hot5_and_n_today_ge3", lambda r: r["tm"]["hot5"] and r["bf"]["n_today"] >= 3),
        ("t_reject_and_n_today_eq1", lambda r: r["tm"]["t_reject"] and r["bf"]["n_today"] == 1),
        (
            "branch_union_soft",
            lambda r: r["bf"]["n_today"] >= 3
            or r["bf"]["max_today_yi"] >= 5.0
            or (
                r["bf"]["yday_dump_ge3yi"]
                and r["tm"]["t_green"] is True
            ),
        ),
    ]:
        masks[label] = [fn(r) for r in rows]
    return masks


def verdict(delta: float | None, skip_n: int, skip_share: float) -> str:
    if skip_n < 3:
        return "n不足"
    if delta is None:
        return "—"
    if delta <= -3.0 and skip_share >= 0.05:
        return "避險有效"
    if delta <= -1.5:
        return "方向對"
    if delta >= 2.0:
        return "反向（skip更差）"
    return "弱／無"


def render_md(payload: dict) -> str:
    lines = [
        "# 分點結構應避免因子 · H_BRANCH_STRUCTURE_AVOID",
        "",
        f"- 生成：{payload['generated_at']}",
        "- Runner：`scripts/research/run_branch_structure_avoid_study.py`",
        "- 資料：expert-pool `knowledge/trades/*.json` branches + finmind 日K（T 日形態）",
        f"- 樣本：{payload['n_trades']} 筆 · {payload['n_stocks']} 檔 · L1H7 `r_adj_pct`",
        "- Research only · **未採納**",
        "",
        "## 結論（先讀）",
        "",
        payload["headline"],
        "",
        "### 全池規則表（skip 組 med r_adj 更低 → 避險方向對）",
        "",
        "| 規則 | skip n | 覆蓋 | skip med | keep med | Δmed | 判定 |",
        "|------|-------:|-----:|---------:|---------:|-----:|------|",
    ]
    for r in payload["pool_rules"]:
        lines.append(
            f"| `{r['rule']}` | {r['skip']['n']} | {r['skip_share']:.0%} | "
            f"{r['skip']['med_r_adj']} | {r['keep']['med_r_adj']} | "
            f"{r['delta_med_skip_minus_keep']} | {r['verdict']} |"
        )
    lines += [
        "",
        "### 重點五檔（2303／2327／6515／2344／8046）",
        "",
    ]
    for sid, block in payload["focus"].items():
        lines.append(f"#### {sid} {block['name']}（n={block['n']}）")
        lines.append("")
        lines.append("| 規則 | skip n | skip med | keep med | Δmed | 判定 |")
        lines.append("|------|-------:|---------:|---------:|-----:|------|")
        for r in block["rules"]:
            lines.append(
                f"| `{r['rule']}` | {r['skip']['n']} | {r['skip']['med_r_adj']} | "
                f"{r['keep']['med_r_adj']} | {r['delta_med_skip_minus_keep']} | {r['verdict']} |"
            )
        lines.append("")
    lines += [
        "## 因子定義",
        "",
        "- **n_today**：branches 中 role 含「今」的席數",
        "- **yday_dom**：今=0 或（今=1 且 昨席≥1）",
        "- **yday_dump_t_green**：某席 role 含昨且 amt_yday≥3億，且 T 日收紅（close>open）",
        "- **HOT5**：訊號前 5 交易日報酬 ≥+20%",
        "- **T_REJECT**：T 日收黑且 close_pos<0.33，或 body<−3%",
        "",
        "## 備註",
        "",
        payload["notes"],
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    ohlc_map = load_daily_ohlc(conn)
    conn.close()

    raw = load_all_trades()
    rows = []
    for t in raw:
        bf = branch_feats(t["branches"])
        tm = t_day_morph(ohlc_map.get((t["sid"], t["signal_date"])))
        rows.append({**t, "bf": bf, "tm": tm})

    max_yis = [r["bf"]["max_today_yi"] for r in rows if r["bf"]["max_today_yi"] > 0]
    max_yis.sort()
    p90 = max_yis[int(0.9 * (len(max_yis) - 1))] if max_yis else 2.0
    p75 = max_yis[int(0.75 * (len(max_yis) - 1))] if max_yis else 1.0

    masks = eval_rules(rows, p90, p75)
    all_r = [r["r_adj_pct"] for r in rows]

    pool_rules = []
    for rule, mask in masks.items():
        cmp = compare_skip_keep(all_r, mask)
        pool_rules.append(
            {
                "rule": rule,
                **cmp,
                "verdict": verdict(
                    cmp["delta_med_skip_minus_keep"],
                    cmp["skip"]["n"],
                    cmp["skip_share"],
                ),
            }
        )
    pool_rules.sort(
        key=lambda x: (
            x["delta_med_skip_minus_keep"]
            if x["delta_med_skip_minus_keep"] is not None
            else 999
        )
    )

    focus = {}
    for sid in FOCUS_SIDS:
        sub = [r for r in rows if r["sid"] == sid]
        sub_r = [r["r_adj_pct"] for r in sub]
        fr = []
        for rule, mask in masks.items():
            idx = [i for i, r in enumerate(rows) if r["sid"] == sid]
            sub_mask = [mask[i] for i in idx]
            cmp = compare_skip_keep(sub_r, sub_mask)
            fr.append(
                {
                    "rule": rule,
                    **cmp,
                    "verdict": verdict(
                        cmp["delta_med_skip_minus_keep"],
                        cmp["skip"]["n"],
                        cmp["skip_share"],
                    ),
                }
            )
        name = sub[0]["name"] if sub else sid
        focus[sid] = {"name": name, "n": len(sub), "rules": fr}

    # headline synthesis
    best = [r for r in pool_rules if r["skip"]["n"] >= 5][:3]
    weak_rev = [r for r in pool_rules if (r["delta_med_skip_minus_keep"] or 0) > 1.5]
    hl_parts = []
    if best:
        hl_parts.append(
            "全池上，"
            + "、".join(
                f"`{b['rule']}` skip med {b['skip']['med_r_adj']}% vs keep {b['keep']['med_r_adj']}%"
                f"（Δ{b['delta_med_skip_minus_keep']}pp）"
                for b in best
            )
            + " 方向最像有效避險。"
        )
    else:
        hl_parts.append("全池樣本下，分點結構單因子未見強烈跨組避險信號。")
    if weak_rev:
        hl_parts.append(
            "注意反向：`"
            + "`、`".join(r["rule"] for r in weak_rev[:2])
            + "` skip 組反而較佳——可能誤殺好單。"
        )
    n_today0 = sum(1 for r in rows if r["bf"]["n_today"] == 0)
    hl_parts.append(
        f"主訊號今席=0 僅 {n_today0} 筆（應極少）；"
        f"今≥3 席覆蓋 {sum(masks['n_today_ge3'])/len(rows):.0%}。"
        f"779Z 型（昨≥3億+T收紅）{sum(masks['yday_dump_t_green'])} 筆。"
    )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "runner": "scripts/research/run_branch_structure_avoid_study.py",
        "n_trades": len(rows),
        "n_stocks": len({r["sid"] for r in rows}),
        "thresholds": {
            "yday_dump_yi": YDAY_DUMP_YI,
            "max_today_p75_yi": round(p75, 3),
            "max_today_p90_yi": round(p90, 3),
        },
        "distribution": {
            "n_today_hist": {
                str(k): sum(1 for r in rows if r["bf"]["n_today"] == k)
                for k in sorted({r["bf"]["n_today"] for r in rows})
            },
            "n_today_eq0": n_today0,
        },
        "headline": " ".join(hl_parts),
        "pool_rules": pool_rules,
        "focus": focus,
        "notes": (
            "分點結構軸與價量圖形軸正交：多席共識（今≥3）在本池 skip 組未必更差，"
            "需與 HOT5 交叉才較像華邦敘事。T_REJECT∧今=1 樣本薄，僅作探索。"
            "單席≥5億／≥10億多為權值／記憶體大單，skip 可能混產業β。"
        ),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_md(payload), encoding="utf-8")
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_MD}")
    print(payload["headline"])


if __name__ == "__main__":
    main()
