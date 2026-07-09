"""S2 Setup core → 隔日 S3 burst 案例研究 · 32 筆 hit 剖析。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from research.backtest.rrg_improving_lifecycle_backtest import BURST_PCT, build_lifecycle_events
from research.backtest.archive.rrg_improving_lifecycle_rule_sweep import two_proportion_ztest
from stock_db import connect


@dataclass(frozen=True)
class S2BurstCase:
    date: str
    stock_id: str
    stock_name: str
    sig_ret: float
    excess: float
    nxt_ret: float
    alpha_nxt: float
    rv: float
    improve_streak: int
    disp_d: float
    bench_today_ret: float
    bench_nxt_ret: float
    inst3_net: float | None
    archetype: str


def _stock_names(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT stock_id, MAX(stock_name) FROM etf_holdings GROUP BY stock_id"
    ).fetchall()
    return {str(r[0]): str(r[1] or "") for r in rows}


def _inst3(conn, sid: str, d: str) -> float | None:
    row = conn.execute(
        """
        SELECT three_institution_net FROM stock_institutional_daily
        WHERE stock_id=? AND trade_date=? LIMIT 1
        """,
        (sid, d),
    ).fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def classify_archetype(row: pd.Series) -> str:
    """Three burst paths from historical 32-case cluster analysis."""
    from rrg_improving_watch import classify_s2_archetype
    from research.backtest.rrg_improving_lifecycle_backtest import ImprovingDayRow

    stub = ImprovingDayRow(
        date=str(row["date"]),
        stock_id=str(row["sid"]),
        stock_name="",
        sig_ret=float(row["sig_ret"]),
        excess=float(row["excess"]),
        end_q=str(row.get("end_q", "improving")),
        improve_streak=int(row["improve_streak"]),
        rv=float(row["rv"]),
        rs_momentum=0.0,
        disp=0.0,
        disp_d=float(row["disp_d"]) if not pd.isna(row.get("disp_d")) else float("nan"),
        session_gap_days=int(row.get("session_gap_days", 1)),
        base_setup=bool(row.get("base_setup", False)),
        s2_setup_core=True,
        s3_burst=False,
        s2_rv_98_99=False,
        s3_rv_98_99=False,
        watch_setup=False,
        disp_contract=bool(row.get("disp_contract", False)),
        quadrants=tuple(),
        bench_today_ret=float(row.get("bench_today_ret", 0)),
        signal_close=0.0,
    )
    return classify_s2_archetype(stub) or "D_idiosyncratic"


def run_s2_burst_case_study(
    *,
    burst_pct: float = BURST_PCT,
) -> dict[str, Any]:
    conn = connect()
    df, meta = build_lifecycle_events(burst_pct=burst_pct)
    names = _stock_names(conn)

    s2 = df[df["s2_setup_core"]].copy()
    hits = s2[s2["nxt_ret"] >= burst_pct].copy().sort_values(["date", "sid"])
    miss = s2[s2["nxt_ret"] < burst_pct].copy()
    hits["alpha_nxt"] = hits["nxt_ret"] - hits["bench_nxt_ret"]
    miss["alpha_nxt"] = miss["nxt_ret"] - miss["bench_nxt_ret"]

    cases: list[S2BurstCase] = []
    for _, r in hits.iterrows():
        inst = _inst3(conn, r["sid"], r["date"])
        arch = classify_archetype(r)
        cases.append(
            S2BurstCase(
                date=r["date"],
                stock_id=r["sid"],
                stock_name=names.get(r["sid"], ""),
                sig_ret=round(float(r["sig_ret"]), 4),
                excess=round(float(r["excess"]), 4),
                nxt_ret=round(float(r["nxt_ret"]), 4),
                alpha_nxt=round(float(r["alpha_nxt"]), 4),
                rv=round(float(r["rv"]), 4),
                improve_streak=int(r["improve_streak"]),
                disp_d=round(float(r["disp_d"]), 4) if not np.isnan(r["disp_d"]) else float("nan"),
                bench_today_ret=round(float(r["bench_today_ret"]), 4),
                bench_nxt_ret=round(float(r["bench_nxt_ret"]), 4),
                inst3_net=round(inst, 0) if inst is not None else None,
                archetype=arch,
            )
        )

    # Rule B: best predictive PIT combo (no mkt oracle)
    rule_b = (
        s2["sig_ret"].between(-0.5, 0.5)
        & s2["disp_contract"]
        & (s2["rv"] < 99)
        & (s2["improve_streak"] >= 6)
    )
    sub_b = s2[rule_b]
    b_hits = int((sub_b["nxt_ret"] >= burst_pct).sum())
    _, p_b = two_proportion_ztest(b_hits, len(sub_b), int(len(hits)), len(s2))

    rule_g = (s2["sig_ret"] < 1) & (s2["rv"] < 99.5) & (
        s2["disp_contract"] | s2["sig_ret"].between(0.3, 0.999)
    )
    sub_g = s2[rule_g]

    arch_counts = hits.apply(classify_archetype, axis=1).value_counts().to_dict()

    repeat_sids = hits["sid"].value_counts()
    repeat_stocks = [
        {"stock_id": sid, "stock_name": names.get(sid, ""), "hit_count": int(n)}
        for sid, n in repeat_sids.items()
        if n >= 2
    ]

    insights = {
        "hit_rate_pct": round(len(hits) / len(s2) * 100, 3),
        "alpha_mean_hit": round(float(hits["alpha_nxt"].mean()), 3),
        "alpha_ge_4pct": int((hits["alpha_nxt"] >= 4).sum()),
        "mkt_down_alpha_ge_5": int(((hits["bench_nxt_ret"] < 0) & (hits["alpha_nxt"] >= 5)).sum()),
        "inst3_positive_rate_hit": round(
            float((hits.apply(lambda r: (_inst3(conn, r["sid"], r["date"]) or 0) > 0, axis=1)).mean()),
            3,
        ),
        "inst3_positive_rate_miss": round(
            float((miss.apply(lambda r: (_inst3(conn, r["sid"], r["date"]) or 0) > 0, axis=1)).mean()),
            3,
        ),
        "archetype_counts": arch_counts,
        "rule_b": {
            "label": "mild sig + disp↓ + RV<99 + streak≥6",
            "n": len(sub_b),
            "hits": b_hits,
            "burst_rate_pct": round(b_hits / len(sub_b) * 100, 3) if len(sub_b) else 0,
            "p_vs_s2_baseline": round(p_b, 6),
            "capture_of_32": int(len(set(hits.index) & set(sub_b.index))),
        },
        "rule_g": {
            "label": "(disp↓ OR sig 0.3–1) + RV<99.5 + sig<1",
            "n": len(sub_g),
            "hits": int((sub_g["nxt_ret"] >= burst_pct).sum()),
            "burst_rate_pct": round(float((sub_g["nxt_ret"] >= burst_pct).mean()) * 100, 3),
            "capture_of_32": int(len(set(hits.index) & set(sub_g.index))),
        },
        "bench_nxt_lift": {
            "hit_mean": round(float(hits["bench_nxt_ret"].mean()), 3),
            "miss_mean": round(float(miss["bench_nxt_ret"].mean()), 3),
            "burst_rate_mkt_up_1pct": round(
                float((s2[s2["bench_nxt_ret"] >= 1]["nxt_ret"] >= burst_pct).mean()) * 100, 3
            ),
            "burst_rate_mkt_down": round(
                float((s2[s2["bench_nxt_ret"] <= -0.5]["nxt_ret"] >= burst_pct).mean()) * 100, 3
            ),
        },
    }

    return {
        "meta": {**meta, "n_s2": len(s2), "n_hits": len(hits), "generated_on": date.today().isoformat()},
        "cases": [asdict(c) for c in cases],
        "repeat_stocks": repeat_stocks,
        "insights": insights,
    }


def render_s2_burst_case_markdown(result: dict[str, Any]) -> str:
    meta = result["meta"]
    ins = result["insights"]
    lines = [
        "# S2 → 隔日 S3 burst 案例研究",
        "",
        f"- Universe: ETF constituents · **{meta['universe_size']}** 檔",
        f"- 區間: **{meta['date_start']}** → **{meta['date_end']}**",
        f"- S2 Setup core: **{meta['n_s2']}** 筆 · 隔日 ≥5% hit: **{meta['n_hits']}**（**{ins['hit_rate_pct']:.2f}%**）",
        f"- Generated: {meta.get('generated_on', '')}",
        "",
        "## 核心洞察",
        "",
        "### 1. 「外力」主要是個股 idiosyncratic alpha，不是 beta",
        "",
        f"- Hit 隔日 **alpha（個股−台指）均值 +{ins['alpha_mean_hit']:.2f}%** · miss 約 −0.07%",
        f"- **{ins['alpha_ge_4pct']}/32** 筆 alpha ≥4% · **{ins['mkt_down_alpha_ge_5']}** 筆在台指下跌日仍 alpha≥5%",
        "- 結論：Setup 後隔日 burst **不是**單純跟著大盤；需 **題材/籌碼/個股事件**（統計上不可從 RRG 單獨還原）。",
        "",
        "### 2. 台指隔日仍是必要條件之一（非充分）",
        "",
        f"- Hit 日隔日台指均值 **+{ins['bench_nxt_lift']['hit_mean']:.2f}%** vs miss **+{ins['bench_nxt_lift']['miss_mean']:.2f}%**",
        f"- S2 全體 · 隔日台指 ≥1% 時 burst **{ins['bench_nxt_lift']['burst_rate_mkt_up_1pct']:.2f}%** · 台指 ≤−0.5% 時 **{ins['bench_nxt_lift']['burst_rate_mkt_down']:.2f}%**",
        "- 台指順風 **抬高機率** · 但約 **1/3 hit** 在台指跌日靠個股 alpha 完成 burst。",
        "",
        "### 3. 三種 burst 原型（歷史 32 筆聚類）",
        "",
        "| 原型 | 條件 | 32 筆占比 | 機制 |",
        "|------|------|----------|------|",
    ]
    arch_labels = {
        "A_coiled_spring": "A 蓄力簧：sig∈[−0.5,0.5] + disp↓ + RV<99",
        "A_coiled_variant": "A' 蓄力變體：disp↓ 但 RV 已偏高",
        "B_leading_edge": "B 貼 Leading：RV≥99.5 · 即將/已在 Leading 門檻",
        "C_pre_chase_spark": "C 預追價火花：sig∈[0.5,1) · 蓄力末段小陽",
        "D_idiosyncratic": "D 其餘 idiosyncratic",
    }
    for key, label in arch_labels.items():
        n = ins["archetype_counts"].get(key, 0)
        pct = n / meta["n_hits"] * 100 if meta["n_hits"] else 0
        mech = {
            "A_coiled_spring": "RRG 位移收斂 · RV 仍在 Improving 深處 · 隔日題材點火",
            "A_coiled_variant": "disp↓ 但 RV 已 99+ · 半 Leading 釋放",
            "B_leading_edge": "2344/6223 類 · RV=99.6–100 · **非 98–99 帶**",
            "C_pre_chase_spark": "S2 邊界內最後小追 · 隔日事件放大",
            "D_idiosyncratic": "深跌 sig 或 disp 擴張 · 純事件驅動",
        }.get(key, "")
        lines.append(f"| {label} | — | {n}（{pct:.0f}%） | {mech} |")

    rb = ins["rule_b"]
    rg = ins["rule_g"]
    lines.extend(
        [
            "",
            "### 4. 可 PIT 篩選的最強組合（仍僅覆蓋部分 hit）",
            "",
            f"- **Rule B**（{rb['label']}）: n={rb['n']} · burst **{rb['burst_rate_pct']:.2f}%** · "
            f"capture **{rb['capture_of_32']}/32** · p vs S2 baseline **{rb['p_vs_s2_baseline']}**",
            f"- **Rule G**（{rg['label']}）: n={rg['n']} · burst **{rg['burst_rate_pct']:.2f}%** · "
            f"capture **{rg['capture_of_32']}/32**",
            "",
            "Rule B 統計顯著（≈4.8% vs 1%）但 **漏掉 26/32** → 其餘需原型 B/C + 個股追蹤。",
            "",
            "### 5. 重複 burst 股（個股「記憶」）",
            "",
        ]
    )
    for rs in result["repeat_stocks"]:
        lines.append(f"- **{rs['stock_id']}** {rs['stock_name']} · {rs['hit_count']} 次")
    lines.extend(
        [
            "",
            "2344 華邦 **3 次** · 題材/記憶體 cycle 與 RRG 疊加 · **非**通用因子。",
            "",
            "### 6. 三大法人（weak signal）",
            "",
            f"- Hit 日三大法人淨買率 **{ins['inst3_positive_rate_hit']:.1%}** vs miss **{ins['inst3_positive_rate_miss']:.1%}**",
            "- 2344 2018 hit 前日淨買 **+1114k/+1532k 張** · 個案有效 · 橫截面 lift 弱。",
            "",
            "## 32 筆案例表",
            "",
            "| 信號日 | 代號 | 名稱 | 原型 | sig% | excess% | RV | streak | dispΔ | 台指D | 台指D+1 | 隔日% | alpha | 法人(張) |",
            "|--------|------|------|------|------|---------|-----|--------|-------|-------|---------|-------|-------|---------|",
        ]
    )
    for c in result["cases"]:
        inst = "—" if c["inst3_net"] is None else f"{c['inst3_net']:,.0f}"
        arch = arch_labels.get(c["archetype"], c["archetype"])
        lines.append(
            f"| {c['date']} | {c['stock_id']} | {c['stock_name']} | {arch.split('：')[0]} | "
            f"{c['sig_ret']:+.2f} | {c['excess']:+.2f} | {c['rv']:.1f} | {c['improve_streak']} | "
            f"{c['disp_d']:+.2f} | {c['bench_today_ret']:+.2f} | {c['bench_nxt_ret']:+.2f} | "
            f"**{c['nxt_ret']:+.2f}** | {c['alpha_nxt']:+.2f} | {inst} |"
        )

    lines.extend(
        [
            "",
            "## 實務含意（research）",
            "",
            "1. **勿期待 S2 隔日穩定 burst** · 絕對率 ~1% · 外力在 **個股 catalyst**",
            "2. **watch 分三軌**：A 蓄力簧（Rule B）· B RV≥99.5 貼 Leading · C sig 0.5–1% 火花",
            "3. **隔日操作**：台指開盤方向作 **倉位放大器** · 非唯一條件",
            "4. **優先追蹤 repeat burst 股** + 當日法人異常（需逐案）",
            "5. **6/30 七檔 S3** 多為 **當日直接 S3**（非 S2→隔日）· 與本表 32 筆路徑不同",
            "",
            "> 重現：`PYTHONPATH=src .venv/bin/python scripts/run_rrg_s2_burst_case_study.py`",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
