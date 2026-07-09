#!/usr/bin/env python3
"""Walk-forward / OOS · H5 开高后杀 vs H3 baseline · Stage-2 leading -6% entry."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

# Reuse event engine from sibling script
sys.path.insert(0, str(ROOT / "scripts"))
from run_leading_stage2_crash_entry_backtest import (  # noqa: E402
    BENCH,
    Event,
    IX,
    _pick,
    run_backtest,
)

REPORT_DIR = ROOT / "reports" / "research" / "leading-stage2-gap-fade"
_TZ = ZoneInfo("Asia/Taipei")

SPLIT_SPEC = "leading_stage2_gap_fade_wfa"
DEFAULT_DIP = 0.06
DEFAULT_HOLDS = (5, 10)
HOLDOUT_START = "2026-01-01"
IS_RATIO = 0.7
MIN_OOS_N_H3 = 25
MIN_OOS_N_H5 = 12


def _stats(rets: list[float]) -> dict[str, Any]:
    if not rets:
        return {"n": 0, "win_pct": None, "med_pct": None, "avg_pct": None}
    return {
        "n": len(rets),
        "win_pct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
        "med_pct": round(median(rets), 2),
        "avg_pct": round(sum(rets) / len(rets), 2),
    }


def _h3_events(events: list[Event], *, dip: float, hold: int) -> list[Event]:
    return _pick(
        events,
        dip=dip,
        hold=hold,
        leading=True,
        stage2=True,
        minervini7=True,
        gate_on=False,
    )


def _h5_events(h3: list[Event]) -> list[Event]:
    return [e for e in h3 if e.gap_up_fade]


def _by_session(events: list[Event]) -> dict[str, list[Event]]:
    out: dict[str, list[Event]] = defaultdict(list)
    for e in events:
        out[e.session].append(e)
    return out


def _unique_sessions(events: list[Event]) -> list[str]:
    return sorted({e.session for e in events})


def _split_ratio(sessions: list[str], ratio: float) -> tuple[list[str], list[str]]:
    if not sessions:
        return [], []
    cut = max(1, int(len(sessions) * ratio))
    return sessions[:cut], sessions[cut:]


def _filter_sessions(events: list[Event], sessions: set[str]) -> list[Event]:
    return [e for e in events if e.session in sessions]


def _compare(h3: list[Event], h5: list[Event]) -> dict[str, Any]:
    s3 = _stats([e.ret_pct for e in h3])
    s5 = _stats([e.ret_pct for e in h5])
    lift_med = None
    if s3["med_pct"] is not None and s5["med_pct"] is not None:
        lift_med = round(s5["med_pct"] - s3["med_pct"], 2)
    return {
        "H3_baseline": s3,
        "H5_gap_up_fade": s5,
        "lift_med_pct": lift_med,
        "h5_share_pct": round(len(h5) / len(h3) * 100, 1) if h3 else None,
    }


def _gate_g2(oos: dict[str, Any], *, min_h3: int, min_h5: int) -> dict[str, Any]:
    h3 = oos["H3_baseline"]
    h5 = oos["H5_gap_up_fade"]
    reasons: list[str] = []
    passed = True
    if (h3["n"] or 0) < min_h3:
        passed = False
        reasons.append(f"H3 OOS n={h3['n']} < {min_h3}")
    if (h5["n"] or 0) < min_h5:
        passed = False
        reasons.append(f"H5 OOS n={h5['n']} < {min_h5}")
    if h5["n"] and (h5["win_pct"] or 0) < 50:
        passed = False
        reasons.append(f"H5 OOS win={h5['win_pct']}% < 50%")
    if h5["n"] and (h5["med_pct"] or 0) <= 0:
        passed = False
        reasons.append(f"H5 OOS med={h5['med_pct']}% ≤ 0")
    if (
        h3["med_pct"] is not None
        and h5["med_pct"] is not None
        and h5["med_pct"] <= h3["med_pct"]
    ):
        passed = False
        reasons.append(f"H5 med {h5['med_pct']}% ≤ H3 med {h3['med_pct']}%")
    if passed:
        reasons.append("OOS H5 beats H3 on median · win≥50% · min_n met")
    return {"passed": passed, "reasons": reasons}


def _quarterly_wfa(h3_all: list[Event], h5_all: list[Event]) -> list[dict[str, Any]]:
    sessions = _unique_sessions(h3_all)
    if not sessions:
        return []
    by_q: dict[str, list[str]] = defaultdict(list)
    for s in sessions:
        dt = datetime.strptime(s, "%Y-%m-%d")
        key = f"{dt.year}-Q{(dt.month - 1) // 3 + 1}"
        by_q[key].append(s)
    quarters = sorted(by_q.keys())
    folds: list[dict[str, Any]] = []
    for i, q in enumerate(quarters):
        if i == 0:
            continue
        train_q = set(quarters[:i])
        test_q = {q}
        train_sess = {s for qq in train_q for s in by_q[qq]}
        test_sess = set(by_q[q])
        h3_tr = _filter_sessions(h3_all, train_sess)
        h5_tr = _filter_sessions(h5_all, train_sess)
        h3_te = _filter_sessions(h3_all, test_sess)
        h5_te = _filter_sessions(h5_all, test_sess)
        folds.append(
            {
                "fold": q,
                "train_quarters": sorted(train_q),
                "test_quarter": q,
                "train": _compare(h3_tr, h5_tr),
                "test": _compare(h3_te, h5_te),
            }
        )
    return folds


def run_wfa(
    events: list[Event],
    *,
    dip: float,
    hold: int,
) -> dict[str, Any]:
    h3 = _h3_events(events, dip=dip, hold=hold)
    h5 = _h5_events(h3)
    sessions = _unique_sessions(h3)
    is_sess, oos_sess = _split_ratio(sessions, IS_RATIO)
    is_set, oos_set = set(is_sess), set(oos_sess)
    holdout_set = {s for s in sessions if s >= HOLDOUT_START}

    is_cmp = _compare(_filter_sessions(h3, is_set), _filter_sessions(h5, is_set))
    oos_cmp = _compare(_filter_sessions(h3, oos_set), _filter_sessions(h5, oos_set))
    holdout_cmp = _compare(
        _filter_sessions(h3, holdout_set),
        _filter_sessions(h5, holdout_set),
    )
    full_cmp = _compare(h3, h5)

    return {
        "params": {
            "dip_pct": dip,
            "hold_days": hold,
            "runup_min_pct": 0.01,
            "dip_trigger_pct": 0.06,
            "split_spec": SPLIT_SPEC,
            "is_ratio": IS_RATIO,
            "holdout_start": HOLDOUT_START,
        },
        "universe_sessions": len(sessions),
        "full_sample": full_cmp,
        "in_sample": is_cmp,
        "out_of_sample_70_30": oos_cmp,
        "holdout_2026": holdout_cmp,
        "gate_G2_70_30": _gate_g2(oos_cmp, min_h3=MIN_OOS_N_H3, min_h5=MIN_OOS_N_H5),
        "gate_G2_holdout_2026": _gate_g2(holdout_cmp, min_h3=15, min_h5=8),
        "quarterly_walk_forward": _quarterly_wfa(h3, h5),
    }


def _md_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Stage-2 Leading 深跌加码 · H5 开高后杀 · Walk-forward / OOS",
        "",
        f"Generated: {datetime.now(_TZ).replace(microsecond=0).isoformat()}",
        f"Split spec: `{SPLIT_SPEC}`",
        "",
        "## 假说",
        "",
        "| ID | 陈述 |",
        "|----|------|",
        "| H-L2GF-1 | H3：leading+Stage2+MV≥7+gate OFF · -6% 进场 |",
        "| H-L2GF-2 | H5：H3 + 盘中 high≥昨收×1.01（开高后杀）· OOS 中位报酬 > H3 |",
        "",
    ]
    for hold in payload["holds"]:
        block = payload["by_hold"][str(hold)]
        p = block["params"]
        lines.extend(
            [
                f"## hold {hold}d · dip -{p['dip_pct']*100:.0f}%",
                "",
                "### 全样本",
                f"- H3: {block['full_sample']['H3_baseline']}",
                f"- H5: {block['full_sample']['H5_gap_up_fade']}",
                f"- lift med: {block['full_sample']['lift_med_pct']}%",
                "",
                "### IS 70% / OOS 30%",
                f"- IS H5 med: {block['in_sample']['H5_gap_up_fade']['med_pct']}%",
                f"- OOS H3: {block['out_of_sample_70_30']['H3_baseline']}",
                f"- OOS H5: {block['out_of_sample_70_30']['H5_gap_up_fade']}",
                f"- **G2 (70/30): {'PASS' if block['gate_G2_70_30']['passed'] else 'FAIL'}** — "
                + "; ".join(block["gate_G2_70_30"]["reasons"]),
                "",
                "### Hold-out ≥2026-01-01",
                f"- H3: {block['holdout_2026']['H3_baseline']}",
                f"- H5: {block['holdout_2026']['H5_gap_up_fade']}",
                f"- **G2 (2026): {'PASS' if block['gate_G2_holdout_2026']['passed'] else 'FAIL'}** — "
                + "; ".join(block["gate_G2_holdout_2026"]["reasons"]),
                "",
            ]
        )
    lines.append("## 进场规则（research · 未採纳）")
    lines.extend(
        [
            "",
            "1. 昨收 RRG leading · Weinstein Stage 2 · Minervini≥7/8",
            "2. 09:05 gate OFF",
            "3. 盘中 high ≥ 昨收×1.01",
            "4. 09:05 后首次 ≤ 昨收×0.94（-6%）· 分批限价",
            "5. 持有 5～10 日",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dip", type=float, default=DEFAULT_DIP)
    parser.add_argument("--hold", type=int, action="append", dest="holds")
    parser.add_argument("--out-json", type=Path, default=None)
    parser.add_argument("--out-md", type=Path, default=None)
    args = parser.parse_args()
    holds = tuple(args.holds) if args.holds else DEFAULT_HOLDS

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(_TZ).strftime("%Y%m%d")

    conn = connect(DEFAULT_DB_PATH)
    stock_ids = [
        r[0]
        for r in conn.execute("SELECT DISTINCT stock_id FROM stock_kbar_1m ORDER BY stock_id").fetchall()
        if r[0] not in {BENCH, IX}
    ]
    print(f"Collecting events · {len(stock_ids)} names …")
    events = run_backtest(conn, stock_ids)
    conn.close()

    payload: dict[str, Any] = {
        "topic_id": "leading-stage2-gap-fade",
        "generated_at": datetime.now(_TZ).replace(microsecond=0).isoformat(),
        "split_spec": SPLIT_SPEC,
        "holds": list(holds),
        "by_hold": {},
    }
    for hold in holds:
        payload["by_hold"][str(hold)] = run_wfa(events, dip=args.dip, hold=hold)

    json_path = args.out_json or (REPORT_DIR / f"leading_stage2_gap_fade_wfa_{stamp}.json")
    md_path = args.out_md or (REPORT_DIR / f"leading_stage2_gap_fade_wfa_{stamp}.md")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_md_report(payload) + "\n", encoding="utf-8")

    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    for hold in holds:
        b = payload["by_hold"][str(hold)]
        g2 = b["gate_G2_70_30"]
        print(
            f"hold {hold}d · OOS H5 n={b['out_of_sample_70_30']['H5_gap_up_fade']['n']} "
            f"med={b['out_of_sample_70_30']['H5_gap_up_fade']['med_pct']}% · "
            f"G2={'PASS' if g2['passed'] else 'FAIL'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
