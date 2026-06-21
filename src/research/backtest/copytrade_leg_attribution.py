"""冠军/垫底日归因：Leg 级 gap × prior_5d 交互分桶（H-G1～G5）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date

from .copytrade_backtest import backfill_copytrade_overnight_gaps
from .copytrade_regime_horizon import classify_regime_pit
from flow_returns import sector_for_stock
from investment_themes import stock_theme

ENTRY_LAG_DAYS = 0
ENTRY_PRICE_MODE = "open"
GAP_DEEP_PCT = -6.0
GAP_MILD_LO_PCT = -2.0
P5D_HOT_PCT = 8.0
P5D_MID_PCT = 3.0
CASE_CHAMPION_DATE = "2026-03-06"
CASE_CELLAR_DATE = "2026-03-12"


@dataclass(frozen=True)
class LegMomentumObs:
    signal_date: str
    stock_id: str
    action: str
    entry_date: str
    exit_date: str
    allocated_ntd: float
    return_pct: float
    bench_return_pct: float
    excess_pct: float
    alpha_ntd: float
    overnight_gap_pct: float | None
    prior_5d_pct: float | None
    prior_10d_pct: float | None
    position_52w_pct: float | None
    skip_overextended: int | None
    n_legs_day: int
    sector: str
    theme: str


def _prior_returns(
    conn: sqlite3.Connection,
    stock_id: str,
    signal_date: str,
    *,
    windows: tuple[int, ...] = (5, 10),
) -> dict[int, float]:
    bars = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND source = 'finmind' AND trade_date <= ?
        ORDER BY trade_date DESC
        LIMIT 25
        """,
        (stock_id, signal_date),
    ).fetchall()
    if len(bars) < 2:
        return {}
    bars = list(reversed(bars))
    c0 = float(bars[-1]["close"])
    if c0 <= 0:
        return {}
    out: dict[int, float] = {}
    for n in windows:
        if len(bars) > n:
            c_n = float(bars[-1 - n]["close"])
            if c_n > 0:
                out[n] = round((c0 / c_n - 1) * 100.0, 4)
    return out


def _gap_band(gap: float | None) -> str:
    if gap is None:
        return "missing"
    if gap < GAP_DEEP_PCT:
        return "deep_down_lt_-6"
    if gap < GAP_MILD_LO_PCT:
        return "mild_down_-6_-2"
    if gap <= 2.0:
        return "flat_-2_2"
    return "gap_up_gt_2"


def _p5d_band(p5: float | None) -> str:
    if p5 is None:
        return "missing"
    if p5 >= P5D_HOT_PCT:
        return "hot_ge_8"
    if p5 >= P5D_MID_PCT:
        return "mid_3_8"
    return "low_lt_3"


def _interaction_band(gap: float | None, p5: float | None) -> str:
    if gap is None or p5 is None:
        return "missing"
    deep = gap < GAP_DEEP_PCT
    hot = p5 >= P5D_HOT_PCT
    if deep and not hot:
        return "deep_gap_cool_p5"
    if deep and hot:
        return "deep_gap_hot_p5"
    if not deep and hot:
        return "shallow_gap_hot_p5"
    return "shallow_gap_cool_p5"


def collect_leg_momentum_observations(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    etf_code: str,
    entry_lag_days: int = ENTRY_LAG_DAYS,
) -> list[LegMomentumObs]:
    legs = conn.execute(
        """
        SELECT l.*, d.bench_return_pct
        FROM copytrade_legs l
        JOIN copytrade_signal_days d
          ON d.run_id = l.run_id AND d.signal_date = l.signal_date
        WHERE l.run_id = ? AND l.status = 'complete' AND d.status = 'complete'
        ORDER BY l.signal_date, l.stock_id
        """,
        (run_id,),
    ).fetchall()
    day_counts: dict[str, int] = {}
    for lg in legs:
        sd = str(lg["signal_date"])
        day_counts[sd] = day_counts.get(sd, 0) + 1

    out: list[LegMomentumObs] = []
    for lg in legs:
        sd = str(lg["signal_date"])
        sid = str(lg["stock_id"])
        gap_row = conn.execute(
            """
            SELECT overnight_gap_pct FROM copytrade_leg_overnight_gaps
            WHERE etf_code = ? AND signal_date = ? AND stock_id = ?
              AND entry_lag_days = ? AND status = 'complete'
            """,
            (etf_code, sd, sid, entry_lag_days),
        ).fetchone()
        ta = conn.execute(
            """
            SELECT position_52w_pct, skip_overextended
            FROM copytrade_leg_ta_snapshots
            WHERE etf_code = ? AND signal_date = ? AND stock_id = ?
            """,
            (etf_code, sd, sid),
        ).fetchone()
        prior = _prior_returns(conn, sid, sd)
        ret = float(lg["return_pct"] or 0)
        bench = float(lg["bench_return_pct"] or 0)
        excess = ret - bench
        alloc = float(lg["allocated_ntd"] or 0)
        gap = (
            float(gap_row["overnight_gap_pct"])
            if gap_row and gap_row["overnight_gap_pct"] is not None
            else None
        )
        out.append(
            LegMomentumObs(
                signal_date=sd,
                stock_id=sid,
                action=str(lg["action"] or ""),
                entry_date=str(lg["entry_date"] or ""),
                exit_date=str(lg["exit_date"] or ""),
                allocated_ntd=alloc,
                return_pct=ret,
                bench_return_pct=bench,
                excess_pct=excess,
                alpha_ntd=alloc * excess / 100.0,
                overnight_gap_pct=gap,
                prior_5d_pct=prior.get(5),
                prior_10d_pct=prior.get(10),
                position_52w_pct=(
                    float(ta["position_52w_pct"])
                    if ta and ta["position_52w_pct"] is not None
                    else None
                ),
                skip_overextended=(
                    int(ta["skip_overextended"]) if ta else None
                ),
                n_legs_day=day_counts.get(sd, 1),
                sector=sector_for_stock(sid),
                theme=stock_theme(sid),
            )
        )
    return out


def _wilcoxon_vs_zero(values: list[float]) -> float | None:
    if len(values) < 20:
        return None
    try:
        from scipy.stats import wilcoxon

        nz = [v for v in values if abs(v) > 1e-12]
        if len(nz) < 15:
            return None
        _, p = wilcoxon(nz)
        return float(p)
    except Exception:
        return None


def _wilcoxon_paired(a: list[float], b: list[float]) -> float | None:
    if len(a) < 20 or len(a) != len(b):
        return None
    diffs = [x - y for x, y in zip(a, b)]
    return _wilcoxon_vs_zero(diffs)


def _pearson_r(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 10 or len(xs) != len(ys):
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = (
        sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)
    ) ** 0.5
    return num / den if den else None


def aggregate_momentum_buckets(
    observations: list[LegMomentumObs],
    *,
    etf_code: str,
    strategy_id: str,
    bucket_fields: tuple[str, ...] = ("gap_band", "p5d_band", "interaction"),
) -> list[dict]:
    rows: list[dict] = []

    def bucket_val(obs: LegMomentumObs, field: str) -> str:
        if field == "gap_band":
            return _gap_band(obs.overnight_gap_pct)
        if field == "p5d_band":
            return _p5d_band(obs.prior_5d_pct)
        if field == "interaction":
            return _interaction_band(obs.overnight_gap_pct, obs.prior_5d_pct)
        raise ValueError(field)

    for bf in bucket_fields:
        groups: dict[str, list[LegMomentumObs]] = {}
        for obs in observations:
            groups.setdefault(bucket_val(obs, bf), []).append(obs)
        for bucket, obs_list in sorted(groups.items()):
            rets = [o.return_pct for o in obs_list]
            excess = [o.excess_pct for o in obs_list]
            alphas = [o.alpha_ntd for o in obs_list]
            n = len(obs_list)
            rows.append(
                {
                    "etf_code": etf_code,
                    "strategy_id": strategy_id,
                    "bucket_field": bf,
                    "bucket_value": bucket,
                    "n_legs": n,
                    "mean_return_pct": round(sum(rets) / n, 4) if n else None,
                    "mean_excess_pct": round(sum(excess) / n, 4) if n else None,
                    "mean_alpha_ntd": round(sum(alphas) / n, 2) if n else None,
                    "sum_alpha_ntd": round(sum(alphas), 2),
                    "win_rate_return_pct": round(
                        sum(1 for r in rets if r > 0) / n * 100.0, 2
                    )
                    if n
                    else None,
                    "win_rate_excess_pct": round(
                        sum(1 for e in excess if e > 0) / n * 100.0, 2
                    )
                    if n
                    else None,
                    "p_value_wilcoxon_excess": _wilcoxon_vs_zero(excess),
                }
            )
    return rows


def evaluate_momentum_hypotheses(
    observations: list[LegMomentumObs],
) -> list[dict]:
    complete = [
        o
        for o in observations
        if o.overnight_gap_pct is not None and o.prior_5d_pct is not None
    ]

    def subset(pred) -> list[LegMomentumObs]:
        return [o for o in complete if pred(o)]

    def summary(
        hid: str,
        label: str,
        a: list[LegMomentumObs],
        b: list[LegMomentumObs],
        *,
        expect_a_better: bool = True,
    ) -> dict:
        if not a or not b:
            return {
                "hypothesis_id": hid,
                "label": label,
                "verdict": "insufficient",
                "n_a": len(a),
                "n_b": len(b),
                "mean_excess_a": None,
                "mean_excess_b": None,
                "p_value_wilcoxon": None,
                "summary_zh": "样本不足",
            }
        ex_a = [o.excess_pct for o in a]
        ex_b = [o.excess_pct for o in b]
        ma = sum(ex_a) / len(ex_a)
        mb = sum(ex_b) / len(ex_b)
        # Mann-Whitney approximation via paired sizes - use two-sample via ranks if scipy
        p = None
        try:
            from scipy.stats import mannwhitneyu

            _, p = mannwhitneyu(ex_a, ex_b, alternative="two-sided")
            p = float(p)
        except Exception:
            pass
        better = ma > mb if expect_a_better else ma < mb
        sig = p is not None and p < 0.05
        if sig and better:
            verdict = "support"
        elif sig and not better:
            verdict = "reject"
        else:
            verdict = "inconclusive"
        return {
            "hypothesis_id": hid,
            "label": label,
            "verdict": verdict,
            "n_a": len(a),
            "n_b": len(b),
            "mean_excess_a": round(ma, 4),
            "mean_excess_b": round(mb, 4),
            "p_value_wilcoxon": round(p, 4) if p is not None else None,
            "summary_zh": (
                f"A mean超额 {ma:+.2f}% vs B {mb:+.2f}%"
                f" · p={p:.4f}" if p is not None else f"A {ma:+.2f}% vs B {mb:+.2f}%"
            ),
        }

    deep = subset(lambda o: o.overnight_gap_pct < GAP_DEEP_PCT)
    shallow = subset(lambda o: o.overnight_gap_pct >= GAP_MILD_LO_PCT)
    hot = subset(lambda o: o.prior_5d_pct >= P5D_HOT_PCT)
    cool = subset(lambda o: o.prior_5d_pct < P5D_MID_PCT)
    deep_cool = subset(
        lambda o: o.overnight_gap_pct < GAP_DEEP_PCT and o.prior_5d_pct < P5D_HOT_PCT
    )
    shallow_hot = subset(
        lambda o: o.overnight_gap_pct >= GAP_MILD_LO_PCT and o.prior_5d_pct >= P5D_HOT_PCT
    )

    # H-G3: signal days with multi leg + mean gap deep
    by_day: dict[str, list[LegMomentumObs]] = {}
    for o in complete:
        by_day.setdefault(o.signal_date, []).append(o)

    champ_days: list[str] = []
    trap_days: list[str] = []
    for sd, legs in by_day.items():
        gaps = [lg.overnight_gap_pct for lg in legs if lg.overnight_gap_pct is not None]
        if not gaps:
            continue
        mean_gap = sum(gaps) / len(gaps)
        p5s = [lg.prior_5d_pct for lg in legs if lg.prior_5d_pct is not None]
        mean_p5 = sum(p5s) / len(p5s) if p5s else 0.0
        if len(legs) >= 3 and mean_gap < GAP_DEEP_PCT:
            champ_days.append(sd)
        if len(legs) == 1 and mean_gap > GAP_MILD_LO_PCT and mean_p5 >= P5D_HOT_PCT:
            trap_days.append(sd)

    def day_alpha(sd: str) -> float:
        return sum(o.alpha_ntd for o in by_day[sd])

    hg3_a = [day_alpha(sd) for sd in champ_days]
    hg3_b = [day_alpha(sd) for sd in trap_days]

    out = [
        summary(
            "H-G1",
            f"深跳空 gap<{GAP_DEEP_PCT}% vs 浅/平 gap≥{GAP_MILD_LO_PCT}%",
            deep,
            shallow,
            expect_a_better=True,
        ),
        summary(
            "H-G2",
            f"讯号前5日急涨 p5d≥{P5D_HOT_PCT}% vs 低动量 p5d<{P5D_MID_PCT}%",
            hot,
            cool,
            expect_a_better=False,
        ),
        summary(
            "H-G4",
            "深gap+低p5 (冠军型) vs 浅gap+高p5 (陷阱型)",
            deep_cool,
            shallow_hot,
            expect_a_better=True,
        ),
    ]
    hg3 = {
        "hypothesis_id": "H-G3",
        "label": "多leg深gap讯号日 vs 单leg浅gap高p5日",
        "verdict": "inconclusive",
        "n_a": len(champ_days),
        "n_b": len(trap_days),
        "mean_excess_a": round(sum(hg3_a) / len(hg3_a), 2) if hg3_a else None,
        "mean_excess_b": round(sum(hg3_b) / len(hg3_b), 2) if hg3_b else None,
        "p_value_wilcoxon": _wilcoxon_paired(hg3_a, hg3_b)
        if len(hg3_a) == len(hg3_b) and len(hg3_a) >= 5
        else None,
        "summary_zh": (
            f"冠军型日 n={len(champ_days)} mean_α={sum(hg3_a)/len(hg3_a):+,.0f}"
            if hg3_a
            else "冠军型日 n=0"
        )
        + (
            f" · 陷阱型 n={len(trap_days)} mean_α={sum(hg3_b)/len(hg3_b):+,.0f}"
            if hg3_b
            else ""
        ),
    }
    if hg3_a and hg3_b:
        ma, mb = sum(hg3_a) / len(hg3_a), sum(hg3_b) / len(hg3_b)
        hg3["verdict"] = "support" if ma > mb else "reject"
    out.append(hg3)

    # H-G5: skip_overextended doesn't separate 6510 vs 6223 type
    oe = [o for o in complete if o.skip_overextended == 1]
    not_oe = [o for o in complete if o.skip_overextended == 0]
    out.append(
        summary(
            "H-G5",
            "TA skip_overextended=1 vs 0（静态过热标签）",
            oe,
            not_oe,
            expect_a_better=False,
        )
    )
    return out


def build_case_study(
    conn: sqlite3.Connection,
    observations: list[LegMomentumObs],
    *,
    champion_date: str = CASE_CHAMPION_DATE,
    cellar_date: str = CASE_CELLAR_DATE,
) -> list[dict]:
    rows: list[dict] = []
    for label, sd in (("champion", champion_date), ("cellar", cellar_date)):
        legs = [o for o in observations if o.signal_date == sd]
        if not legs:
            continue
        lab = classify_regime_pit(conn, sd)
        day_alpha = sum(o.alpha_ntd for o in legs)
        rows.append(
            {
                "case_type": label,
                "signal_date": sd,
                "trend_posture": lab.trend_posture if lab else None,
                "tx_gap_pct": lab.tx_gap_pct if lab else None,
                "n_legs": len(legs),
                "day_alpha_ntd": round(day_alpha, 2),
            }
        )
        for o in sorted(legs, key=lambda x: -x.return_pct):
            rows.append(
                {
                    "case_type": label,
                    "signal_date": sd,
                    "stock_id": o.stock_id,
                    "sector": o.sector,
                    "theme": o.theme,
                    "return_pct": round(o.return_pct, 4),
                    "alpha_ntd": round(o.alpha_ntd, 2),
                    "overnight_gap_pct": o.overnight_gap_pct,
                    "prior_5d_pct": o.prior_5d_pct,
                    "prior_10d_pct": o.prior_10d_pct,
                    "position_52w_pct": o.position_52w_pct,
                    "skip_overextended": o.skip_overextended,
                }
            )
    return rows


def correlation_summary(observations: list[LegMomentumObs]) -> list[dict]:
    complete = [
        o
        for o in observations
        if o.overnight_gap_pct is not None and o.prior_5d_pct is not None
    ]
    ys = [o.return_pct for o in complete]
    out = []
    for field, xs in (
        ("prior_10d_pct", [o.prior_10d_pct for o in complete if o.prior_10d_pct is not None]),
        ("prior_5d_pct", [o.prior_5d_pct for o in complete]),
        ("overnight_gap_pct", [o.overnight_gap_pct for o in complete]),
        ("position_52w_pct", [o.position_52w_pct for o in complete if o.position_52w_pct is not None]),
    ):
        if field == "prior_10d_pct":
            y = [o.return_pct for o in complete if o.prior_10d_pct is not None]
            x = xs
        elif field == "position_52w_pct":
            y = [o.return_pct for o in complete if o.position_52w_pct is not None]
            x = xs
        else:
            x, y = xs, ys
        out.append(
            {
                "feature": field,
                "n": len(x),
                "pearson_r": round(_pearson_r(x, y) or 0.0, 4)
                if _pearson_r(x, y) is not None
                else None,
            }
        )
    return out


def format_leg_attribution_markdown(
    *,
    etf_code: str,
    strategy_id: str,
    batch_id: str,
    bucket_rows: list[dict],
    hypotheses: list[dict],
    correlations: list[dict],
    case_rows: list[dict],
    n_obs: int,
    n_with_features: int,
) -> str:
    today = date.today().strftime("%Y%m%d")
    lines = [
        f"# {etf_code} Leg 动量归因（gap × p5d · {strategy_id}）",
        "",
        f"> batch `{batch_id}` · 报告日 {today}",
        "",
        "## 方法",
        "",
        f"- **样本**：{strategy_id} complete legs · 有 gap+p5d 者 {n_with_features}/{n_obs}",
        f"- **gap**：T 收盘 → T+1 开盘（`copytrade_leg_overnight_gaps`）",
        "- **p5d**：讯号日 T 收盘相对前 5 交易日涨幅",
        f"- **深跳空**：gap < {GAP_DEEP_PCT}% · **急涨**：p5d ≥ {P5D_HOT_PCT}%",
        "",
        "## 个案：2026-03-06 vs 2026-03-12",
        "",
    ]
    for cr in case_rows:
        if "stock_id" not in cr:
            lines.append(
                f"- **{cr['case_type']}** {cr['signal_date']}: "
                f"α={cr['day_alpha_ntd']:+,.0f} · {cr['n_legs']} legs · "
                f"trend_posture={cr.get('trend_posture') or cr.get('regime_name')} tx={cr.get('tx_gap_pct')}"
            )
        else:
            lines.append(
                f"  - {cr['stock_id']} {cr['theme']} ret={cr['return_pct']:+.1f}% "
                f"gap={cr['overnight_gap_pct']}% p5={cr['prior_5d_pct']}% "
                f"p10={cr['prior_10d_pct']}% pos52w={cr['position_52w_pct']} "
                f"skip_oe={cr['skip_overextended']}"
            )

    lines.extend(["", "## 假说检验", "", "| ID | 假说 | 判决 | A vs B mean超额% | p |", "|----|------|------|-----------------|---|"])
    for h in hypotheses:
        ma = h.get("mean_excess_a")
        mb = h.get("mean_excess_b")
        vs = "—"
        if ma is not None and mb is not None:
            vs = f"{ma:+.2f} vs {mb:+.2f}"
        p = h.get("p_value_wilcoxon")
        lines.append(
            f"| {h['hypothesis_id']} | {h['label']} | **{h['verdict']}** | {vs} | "
            f"{p if p is not None else '—'} |"
        )

    lines.extend(["", "## 特征 vs H9 return 相关", ""])
    for c in correlations:
        lines.append(f"- `{c['feature']}`: r={c['pearson_r']} (n={c['n']})")

    for bf in ("gap_band", "p5d_band", "interaction"):
        sub = [r for r in bucket_rows if r["bucket_field"] == bf]
        if not sub:
            continue
        lines.extend(["", f"## 分桶：{bf}", ""])
        lines.append("| 桶 | n | mean超额% | 胜超额% | sum α | p(W) |")
        lines.append("|----|---|---------|--------|-------|------|")
        for r in sorted(sub, key=lambda x: -float(x["mean_excess_pct"] or 0)):
            p = r.get("p_value_wilcoxon_excess")
            lines.append(
                f"| {r['bucket_value']} | {r['n_legs']} | "
                f"{r['mean_excess_pct'] or 0:+.2f} | {r['win_rate_excess_pct'] or 0:.1f}% | "
                f"{r['sum_alpha_ntd']:+,.0f} | {p if p is not None else '—'} |"
            )

    lines.extend(
        [
            "",
            "## 解读",
            "",
            "- **p10d / pos52w** 相关弱 → 静态动量难事前分辨冠军/垫底日。",
            f"- **深 gap（<{GAP_DEEP_PCT}%）** 若 H-G1 support → 「低开续涨」优于追平开。",
            f"- **高 p5d（≥{P5D_HOT_PCT}%）** 若 H-G2 support → 反弹后加码有均值回归风险。",
            "- **交互桶 deep_gap_cool_p5** 富集高 α → 轨 D 加权候选，非 skip。",
            "",
        ]
    )
    return "\n".join(lines)


def run_leg_attribution_analysis(
    conn: sqlite3.Connection,
    *,
    etf_code: str,
    strategy_id: str = "L1H9",
    batch_id: str | None = None,
    entry_lag_days: int = ENTRY_LAG_DAYS,
    backfill_gaps: bool = True,
    persist: bool = True,
) -> dict[str, object]:
    from stock_db import persist_copytrade_leg_attribution

    run = conn.execute(
        """
        SELECT run_id, batch_id FROM copytrade_runs
        WHERE etf_code = ? AND strategy_id = ?
        ORDER BY synced_at DESC LIMIT 1
        """,
        (etf_code, strategy_id),
    ).fetchone()
    if run is None:
        raise ValueError(f"no run for {etf_code} {strategy_id}")

    run_id = str(run["run_id"])
    bid = batch_id or f"{etf_code.lower()}-leg-attrib-{strategy_id.lower()}-{date.today().strftime('%Y%m%d')}"

    if backfill_gaps:
        backfill_copytrade_overnight_gaps(
            conn, etf_code, entry_lag_days=entry_lag_days
        )

    observations = collect_leg_momentum_observations(
        conn, run_id, etf_code=etf_code, entry_lag_days=entry_lag_days
    )
    n_with = sum(
        1
        for o in observations
        if o.overnight_gap_pct is not None and o.prior_5d_pct is not None
    )
    bucket_rows = aggregate_momentum_buckets(
        observations, etf_code=etf_code, strategy_id=strategy_id
    )
    hypotheses = evaluate_momentum_hypotheses(observations)
    correlations = correlation_summary(observations)
    case_rows = build_case_study(conn, observations)

    if persist:
        persist_copytrade_leg_attribution(
            conn,
            bid,
            bucket_rows=bucket_rows,
            hypotheses=hypotheses,
            correlations=correlations,
            case_rows=case_rows,
            meta={
                "etf_code": etf_code,
                "strategy_id": strategy_id,
                "run_id": run_id,
                "n_obs": len(observations),
                "n_with_features": n_with,
            },
        )

    return {
        "batch_id": bid,
        "run_id": run_id,
        "strategy_id": strategy_id,
        "observations": observations,
        "bucket_rows": bucket_rows,
        "hypotheses": hypotheses,
        "correlations": correlations,
        "case_rows": case_rows,
        "n_obs": len(observations),
        "n_with_features": n_with,
    }
