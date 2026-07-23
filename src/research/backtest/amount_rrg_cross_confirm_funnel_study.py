"""金額×RRG 1 槽冠軍上：跨分點確認 + 三大法人賣超漏斗。

基準：xd ≥80M · rs_ratio>100 · Top1 · L2H12 · 1槽。
複利 SSOT：與 `amount_rrg_2day_sell_funnel_study` / C18acc 同構 Π(1+r)−1。
選參：IS max compound；OOS replay only。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from copytrade.branch_signals import (
    DEFAULT_TRADER_ID_KGI_CHENGZHONG,
    DEFAULT_TRADER_ID_YUANTA_SONGJIANG,
    _is_listed_equity_id,
)
from copytrade.dual_branch_signals import DualBranchTapeCache
from copytrade.signals import CopytradeSignal
from report_paths import REPORTS_RESEARCH
from research.backtest.amount_rrg_2day_sell_funnel_study import (
    DEFAULT_AMOUNT_NTD,
    C18_OVERLAP_START,
    TapeFunnel,
    _enrich_metrics_with_compound,
    _oos_gate,
    load_xd_tape_bs,
)
from research.backtest.amount_rrg_copytrade_funnel_study import (
    DEFAULT_OOS_CUT,
    DEFAULT_WINDOW_END,
    DEFAULT_WINDOW_START,
)
from research.backtest.amount_rrg_copytrade_study import (
    apply_rrg_gate,
    build_rrg_ffill_index,
    build_signals_fixed,
    load_rrg_close_index,
)
from research.backtest.copytrade_backtest import DEFAULT_SIGNAL_CAPITAL_NTD
from stock_db import load_broker_branch_nets

FILE_STEM = "amount_rrg_cross_confirm_funnel_study"


def load_inst_nets(
    conn: sqlite3.Connection,
    *,
    window_start: str,
    window_end: str,
) -> dict[tuple[str, str], dict[str, float]]:
    rows = conn.execute(
        """
        SELECT trade_date, stock_id,
               foreign_net, investment_trust_net, dealer_self_net,
               three_institution_net
        FROM stock_institutional_daily
        WHERE source = 'finmind'
          AND trade_date >= ? AND trade_date <= ?
        """,
        (window_start, window_end),
    ).fetchall()
    out: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        sid = str(r[1])
        if not _is_listed_equity_id(sid):
            continue
        out[(str(r[0]), sid)] = {
            "foreign_net": float(r[2] or 0.0),
            "it_net": float(r[3] or 0.0),
            "dealer_net": float(r[4] or 0.0),
            "three_net": float(r[5] or 0.0),
        }
    return out


def _amount(
    net_shares: float | None,
    close: float | None,
) -> float | None:
    if net_shares is None or close is None or close <= 0:
        return None
    if float(net_shares) <= 0:
        return 0.0
    return float(net_shares) * float(close)


def build_cross_feature(
    *,
    day: str,
    stock_id: str,
    closes: dict[tuple[str, str], float],
    sj_bs: dict[tuple[str, str], dict[str, float]],
    kgi_bs: dict[tuple[str, str], dict[str, float]],
    inst: dict[tuple[str, str], dict[str, float]],
) -> dict[str, Any]:
    close = closes.get((stock_id, day))
    sj = sj_bs.get((day, stock_id))
    kgi = kgi_bs.get((day, stock_id))
    ii = inst.get((day, stock_id))
    sj_net = float(sj["net"]) if sj else None
    sj_buy = float(sj["buy"]) if sj else None
    sj_sell = float(sj["sell"]) if sj else None
    kgi_net = float(kgi["net"]) if kgi else None
    return {
        "close": close,
        "sj_net": sj_net,
        "sj_buy": sj_buy,
        "sj_sell": sj_sell,
        "sj_amount": _amount(sj_net, close),
        "sj_sell_ratio": (
            (sj_sell / sj_buy) if sj_buy is not None and sj_buy > 0 else None
        ),
        "kgi_net": kgi_net,
        "kgi_amount": _amount(kgi_net, close),
        "foreign_net": ii.get("foreign_net") if ii else None,
        "it_net": ii.get("it_net") if ii else None,
        "three_net": ii.get("three_net") if ii else None,
        "has_sj_row": sj is not None,
        "has_inst": ii is not None,
    }


def default_cross_funnels() -> list[TapeFunnel]:
    def sj_amt_ge(thr: float) -> Callable[[dict[str, Any]], bool]:
        return lambda f: f.get("sj_amount") is not None and float(f["sj_amount"]) >= thr

    return [
        TapeFunnel("baseline", "baseline", "無額外漏斗", lambda _f: True),
        TapeFunnel(
            "sj_net_pos",
            "cross_branch",
            "松江同日淨買超 > 0",
            lambda f: f.get("sj_net") is not None and float(f["sj_net"]) > 0,
        ),
        TapeFunnel(
            "sj_amount_ge_10M",
            "cross_branch",
            "松江同日金額 ≥ 10M",
            sj_amt_ge(10_000_000.0),
        ),
        TapeFunnel(
            "sj_amount_ge_20M",
            "cross_branch",
            "松江同日金額 ≥ 20M",
            sj_amt_ge(20_000_000.0),
        ),
        TapeFunnel(
            "sj_amount_ge_50M",
            "cross_branch",
            "松江同日金額 ≥ 50M",
            sj_amt_ge(50_000_000.0),
        ),
        TapeFunnel(
            "sj_not_net_seller",
            "cross_branch",
            "松江同日非淨賣超（net≥0 或無列視為通過）",
            lambda f: (not f.get("has_sj_row"))
            or (f.get("sj_net") is not None and float(f["sj_net"]) >= 0),
        ),
        TapeFunnel(
            "sj_sell_ratio_le_0_50",
            "cross_branch",
            "松江同日 sell/buy ≤ 0.50（須有松江列）",
            lambda f: f.get("sj_sell_ratio") is not None
            and float(f["sj_sell_ratio"]) <= 0.50,
        ),
        TapeFunnel(
            "kgi_net_pos",
            "cross_branch",
            "凱基城中同日淨買超 > 0",
            lambda f: f.get("kgi_net") is not None and float(f["kgi_net"]) > 0,
        ),
        TapeFunnel(
            "kgi_amount_ge_10M",
            "cross_branch",
            "凱基城中同日金額 ≥ 10M",
            lambda f: f.get("kgi_amount") is not None
            and float(f["kgi_amount"]) >= 10_000_000.0,
        ),
        TapeFunnel(
            "sj_or_kgi_net_pos",
            "cross_branch",
            "松江或凱基同日淨買超 > 0",
            lambda f: (
                (f.get("sj_net") is not None and float(f["sj_net"]) > 0)
                or (f.get("kgi_net") is not None and float(f["kgi_net"]) > 0)
            ),
        ),
        TapeFunnel(
            "foreign_net_pos",
            "institutional",
            "外資同日淨買超 > 0",
            lambda f: f.get("foreign_net") is not None and float(f["foreign_net"]) > 0,
        ),
        TapeFunnel(
            "foreign_net_not_heavy_sell",
            "institutional",
            "外資同日淨賣不重（foreign_net ≥ −500 張）",
            lambda f: f.get("foreign_net") is not None
            and float(f["foreign_net"]) >= -500_000.0,
        ),
        TapeFunnel(
            "three_net_pos",
            "institutional",
            "三大法人同日合計淨買 > 0",
            lambda f: f.get("three_net") is not None and float(f["three_net"]) > 0,
        ),
        TapeFunnel(
            "three_net_not_heavy_sell",
            "institutional",
            "三大法人同日合計淨賣不重（≥ −1000 張）",
            lambda f: f.get("three_net") is not None
            and float(f["three_net"]) >= -1_000_000.0,
        ),
        TapeFunnel(
            "sj_pos_and_foreign_not_heavy",
            "combo",
            "松江淨買 > 0 ∧ 外資非重賣",
            lambda f: f.get("sj_net") is not None
            and float(f["sj_net"]) > 0
            and f.get("foreign_net") is not None
            and float(f["foreign_net"]) >= -500_000.0,
        ),
        TapeFunnel(
            "sj_10M_and_three_pos",
            "combo",
            "松江 ≥10M ∧ 三大法人淨買 > 0",
            lambda f: f.get("sj_amount") is not None
            and float(f["sj_amount"]) >= 10_000_000.0
            and f.get("three_net") is not None
            and float(f["three_net"]) > 0,
        ),
    ]


def filter_grouped(
    grouped: dict[str, list[CopytradeSignal]],
    *,
    funnel: TapeFunnel,
    closes: dict[tuple[str, str], float],
    sj_bs: dict[tuple[str, str], dict[str, float]],
    kgi_bs: dict[tuple[str, str], dict[str, float]],
    inst: dict[tuple[str, str], dict[str, float]],
) -> tuple[dict[str, list[CopytradeSignal]], dict[str, Any]]:
    out: dict[str, list[CopytradeSignal]] = {}
    n_in = n_pass = n_miss = 0
    for day, legs in grouped.items():
        kept: list[CopytradeSignal] = []
        for leg in legs:
            n_in += 1
            feat = build_cross_feature(
                day=day,
                stock_id=str(leg.stock_id),
                closes=closes,
                sj_bs=sj_bs,
                kgi_bs=kgi_bs,
                inst=inst,
            )
            if funnel.predicate(feat):
                n_pass += 1
                kept.append(leg)
            else:
                n_miss += 1
        if kept:
            out[day] = kept
    return out, {
        "legs_in": n_in,
        "legs_pass": n_pass,
        "legs_fail": n_miss,
        "pass_pct": round(100.0 * n_pass / n_in, 2) if n_in else None,
        "signal_days": len(out),
    }


def run_cross_confirm_funnel_study(
    conn: sqlite3.Connection,
    *,
    window_start: str = DEFAULT_WINDOW_START,
    window_end: str = DEFAULT_WINDOW_END,
    oos_cut: str = DEFAULT_OOS_CUT,
    amount_ntd: float = DEFAULT_AMOUNT_NTD,
    capital_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
) -> dict[str, Any]:
    tape = DualBranchTapeCache.load(
        conn, window_start=window_start, window_end=window_end, need_closes=True
    )
    calendar = sorted(set(tape.sj) | set(tape.xd))
    sj_bs = load_xd_tape_bs(
        conn,
        window_start=window_start,
        window_end=window_end,
        trader_id=DEFAULT_TRADER_ID_YUANTA_SONGJIANG,
    )
    kgi_bs = load_xd_tape_bs(
        conn,
        window_start=window_start,
        window_end=window_end,
        trader_id=DEFAULT_TRADER_ID_KGI_CHENGZHONG,
    )
    # load_xd_tape_bs name is generic; works for any trader_id.
    inst = load_inst_nets(conn, window_start=window_start, window_end=window_end)

    raw_rrg = load_rrg_close_index(
        conn, window_start=window_start, window_end=window_end
    )
    rrg_ffill = build_rrg_ffill_index(raw_rrg, calendar)
    raw = build_signals_fixed(
        conn,
        mode="xd",
        min_threshold=amount_ntd,
        top_n=1,
        window_start=window_start,
        window_end=window_end,
        tape=tape,
        min_avg_volume_shares=200_000.0,
    )
    baseline_grouped, _, gate_drops = apply_rrg_gate(
        raw, rrg_ffill, "rs_ratio_gt100"
    )

    arms: list[dict[str, Any]] = []
    for funnel in default_cross_funnels():
        grouped, meta = filter_grouped(
            baseline_grouped,
            funnel=funnel,
            closes=tape.closes,
            sj_bs=sj_bs,
            kgi_bs=kgi_bs,
            inst=inst,
        )
        metrics = _enrich_metrics_with_compound(
            conn,
            grouped,
            label=f"80M rs>100 L2H12 S1 · {funnel.id}",
            capital_ntd=capital_ntd,
            window_start=window_start,
            window_end=window_end,
            oos_cut=oos_cut,
        )
        arms.append(
            {
                "funnel_id": funnel.id,
                "family": funnel.family,
                "label": funnel.label,
                "funnel_meta": meta,
                **metrics,
            }
        )

    baseline = next(a for a in arms if a["funnel_id"] == "baseline")
    base_is_n = int(baseline["is"].get("n_cycles") or 0)
    viable = [
        a
        for a in arms
        if int(a["is"].get("n_cycles") or 0) >= max(6, int(base_is_n * 0.50))
    ]
    selected = max(
        viable or [baseline],
        key=lambda a: (
            float(a["is"].get("compound_slot_pct") or -1e18),
            float(a["is"].get("sum_return_pct") or -1e18),
            int(a["is"].get("n_cycles") or 0),
        ),
    )
    gate = _oos_gate(selected, baseline)
    surviving = (
        selected if gate["status"] in {"passed", "redundant"} else baseline
    )

    family_summary: dict[str, Any] = {}
    for fam in sorted({a["family"] for a in arms}):
        fam_arms = [a for a in arms if a["family"] == fam]
        best = max(
            fam_arms,
            key=lambda a: float(a["is"].get("compound_slot_pct") or -1e18),
        )
        family_summary[fam] = {
            "best_funnel_id": best["funnel_id"],
            "is_compound": best["is"].get("compound_slot_pct"),
            "oos_compound": best["oos"].get("compound_slot_pct"),
            "full_compound": best["full"].get("compound_slot_pct"),
            "oos_gate": _oos_gate(best, baseline),
        }

    c18_path = (
        REPORTS_RESEARCH / "rrg" / "20260717_c18acc_vs_radar_c0_slot_fair.json"
    )
    c18_fair = None
    if c18_path.exists():
        c18 = json.loads(c18_path.read_text(encoding="utf-8"))
        b0 = next(
            (v for v in c18.get("variants") or [] if v.get("variant_id") == "B0_live"),
            None,
        )
        if b0:
            sl = (b0.get("slices") or {}).get("full") or {}
            c18_fair = {
                "source": c18_path.name,
                "compound_slot_pct": sl.get("compound_slot_pct"),
                "n_periods": sl.get("n_periods"),
                "window": f"{c18.get('date_start')}→{c18.get('date_end')}",
            }

    def _arm(fid: str) -> dict[str, Any]:
        return next(a for a in arms if a["funnel_id"] == fid)

    hypotheses = {
        "H_SJ_CONFIRM": {
            "statement": "松江同日買超確認可提升 OOS 單槽複利",
            "status": family_summary.get("cross_branch", {})
            .get("oos_gate", {})
            .get("status"),
            "best": family_summary.get("cross_branch", {}).get("best_funnel_id"),
        },
        "H_INST_SELL_FILTER": {
            "statement": "外資／三大法人非重賣或淨買可提升 OOS 單槽複利",
            "status": family_summary.get("institutional", {})
            .get("oos_gate", {})
            .get("status"),
            "best": family_summary.get("institutional", {}).get("best_funnel_id"),
        },
        "H_CROSS_INST_COMBO": {
            "statement": "跨分點 ∧ 法人組合可通過 OOS 複利閘門",
            "status": family_summary.get("combo", {})
            .get("oos_gate", {})
            .get("status"),
            "best": family_summary.get("combo", {}).get("best_funnel_id"),
        },
        "H_SURVIVOR_BEATS_BASELINE": {
            "statement": "IS 複利冠軍 OOS 相對基準加值 ≥10%",
            "status": gate["status"],
            "selected": selected["funnel_id"],
            "surviving": surviving["funnel_id"],
        },
        "H_SJ_NET_POS_SPECIFIC": {
            "statement": "松江淨買>0 單一漏斗通過 OOS",
            "status": _oos_gate(_arm("sj_net_pos"), baseline)["status"],
        },
        "H_FOREIGN_NOT_HEAVY": {
            "statement": "外資非重賣通過 OOS",
            "status": _oos_gate(
                _arm("foreign_net_not_heavy_sell"), baseline
            )["status"],
        },
    }

    return {
        "schema": "amount_rrg_cross_confirm_funnel_study-v1",
        "contract": {
            "window_start": window_start,
            "window_end": window_end,
            "oos_cut": oos_cut,
            "baseline": "xd ≥80M · rs_ratio>100 · Top1 · L2H12 · 1槽",
            "capital_ntd_per_slot": capital_ntd,
            "compound_definition": "equal_slot_compound Π(1+r)-1 (1-slot)",
            "selection": "IS max compound_slot_pct; OOS robustness only",
            "rrg_gate_drops": gate_drops,
            "confirm_sources": {
                "songjiang": DEFAULT_TRADER_ID_YUANTA_SONGJIANG,
                "kgi": DEFAULT_TRADER_ID_KGI_CHENGZHONG,
                "institutional": "stock_institutional_daily",
            },
        },
        "baseline": {
            "full": baseline["full"],
            "is": baseline["is"],
            "oos": baseline["oos"],
            "overlap_from_202501": baseline["overlap_from_202501"],
        },
        "arms": [
            {
                "funnel_id": a["funnel_id"],
                "family": a["family"],
                "label": a["label"],
                "funnel_meta": a["funnel_meta"],
                "full": a["full"],
                "is": a["is"],
                "oos": a["oos"],
                "overlap_from_202501": a["overlap_from_202501"],
                "oos_gate_vs_baseline": _oos_gate(a, baseline),
            }
            for a in arms
        ],
        "is_selected": {
            "funnel_id": selected["funnel_id"],
            "is": selected["is"],
            "oos": selected["oos"],
            "oos_gate": gate,
        },
        "gate_surviving": {
            "funnel_id": surviving["funnel_id"],
            "full": surviving["full"],
            "is": surviving["is"],
            "oos": surviving["oos"],
            "overlap_from_202501": surviving["overlap_from_202501"],
        },
        "family_summary": family_summary,
        "fair_compound_vs_c18acc": {
            "c18acc": c18_fair,
            "copytrade_baseline_overlap": baseline["overlap_from_202501"],
            "copytrade_survivor_overlap": surviving["overlap_from_202501"],
        },
        "hypotheses": hypotheses,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    c = payload["contract"]
    lines = [
        "# 金額×RRG · 跨分點／法人確認漏斗 · 單槽複利",
        "",
        f"- window: `{c['window_start']}` → `{c['window_end']}` · OOS `{c['oos_cut']}`",
        f"- baseline: `{c['baseline']}`",
        "",
        "## Fair compound vs C18acc",
        "",
    ]
    fair = payload["fair_compound_vs_c18acc"]
    c18 = fair.get("c18acc") or {}
    ov = fair.get("copytrade_baseline_overlap") or {}
    lines.extend(
        [
            f"- C18acc compound%: **{c18.get('compound_slot_pct')}** (n={c18.get('n_periods')})",
            f"- Copytrade overlap compound%: **{ov.get('compound_slot_pct')}** "
            f"(n={ov.get('n_cycles')})",
            "",
            "## Hypotheses",
            "",
        ]
    )
    for hid, h in payload["hypotheses"].items():
        lines.append(f"- **{hid}**: `{h.get('status')}` — {h.get('statement')}")
    lines.extend(["", "## Arms", ""])
    for a in payload["arms"]:
        g = a["oos_gate_vs_baseline"]["status"]
        lines.append(
            f"- `{a['funnel_id']}` · {a['label']} · "
            f"pass%={a['funnel_meta'].get('pass_pct')} · "
            f"IS c={a['is'].get('compound_slot_pct')} n={a['is'].get('n_cycles')} · "
            f"OOS c={a['oos'].get('compound_slot_pct')} n={a['oos'].get('n_cycles')} · "
            f"`{g}`"
        )
    surv = payload["gate_surviving"]
    lines.extend(
        [
            "",
            f"## Survivor: `{surv['funnel_id']}`",
            f"- full compound={surv['full'].get('compound_slot_pct')}",
            f"- OOS compound={surv['oos'].get('compound_slot_pct')}",
            "",
            "Research only · not Strategy/Order.",
            "",
        ]
    )
    return "\n".join(lines)


def write_artifacts(
    payload: dict[str, Any],
    *,
    out_dir: Path | None = None,
    stamp: str | None = None,
) -> dict[str, Path]:
    out_dir = out_dir or (REPORTS_RESEARCH / "dual-branch-copytrade")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or date.today().strftime("%Y%m%d")
    stem = f"{stamp}_{FILE_STEM}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    return {"json": json_path, "md": md_path}
