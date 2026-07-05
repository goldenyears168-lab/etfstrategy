#!/usr/bin/env python3
"""S3 + RV 98–99 subset backtest · tradable filters + equity curve.

Buy at signal-day close (D), sell next close (D+1), equal-weight within day.
Reports event stats and compounded equity (cash = 0% on no-signal days).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from research.backtest.rrg_improving_lifecycle_backtest import build_lifecycle_events  # noqa: E402
from stock_db import connect  # noqa: E402

REPORT_DIR = ROOT / "reports" / "research" / "rrg"


@dataclass(frozen=True)
class VariantResult:
    variant_id: str
    label: str
    tradable: bool
    n_events: int
    n_invested_days: int
    n_calendar_days: int
    event_mean_pct: float
    event_win_rate: float
    p_burst_pct: float
    daily_mean_pct: float
    daily_win_rate: float
    total_return_pct: float
    cagr_pct: float
    sharpe: float
    max_drawdown_pct: float
    avg_names_per_day: float


def _pit_sid_hist_mean(sub: pd.DataFrame) -> pd.Series:
    """PIT mean of prior S3+RV next-day returns per (date, sid)."""
    s3 = sub[sub["s3_rv_98_99"]].sort_values(["sid", "date"])
    out = pd.Series(index=s3.index, dtype=float)
    for sid, grp in s3.groupby("sid"):
        prior: list[float] = []
        for idx, row in grp.iterrows():
            if len(prior) >= 3:
                out.at[idx] = float(np.mean(prior))
            else:
                out.at[idx] = np.nan
            prior.append(float(row["nxt_ret"]))
    return out


def _apply_variants(df: pd.DataFrame) -> dict[str, pd.DataFrame]:
    s3 = df[df["s3_rv_98_99"]].copy()
    sid_mean = _pit_sid_hist_mean(df)
    s3["sid_hist_mean"] = sid_mean.reindex(s3.index)
    s3["sid_hist_ok"] = s3["sid_hist_mean"] > 0

    return {
        "s3rv_all": s3,
        "s3rv_sig_mkt_up": s3[s3["bench_today_ret"] >= 0],
        "s3rv_oracle_mkt_up": s3[s3["bench_nxt_ret"] >= 0],
        "s3rv_sid_pos": s3[s3["sid_hist_ok"]],
        "s3rv_tradable": s3[(s3["bench_today_ret"] >= 0) & (s3["sid_hist_ok"])],
        "s3rv_tradable_relaxed": s3[
            (s3["bench_today_ret"] >= 0)
            & (s3["sid_hist_ok"] | s3["sid_hist_mean"].isna())
        ],
        "s2_core": df[df["s2_setup_core"]],
    }


def _equity_curve(picks: pd.DataFrame, all_dates: list[str]) -> pd.Series:
    """Daily return series aligned to realization day (D+1)."""
    if picks.empty:
        return pd.Series(0.0, index=all_dates)
    realized = picks.copy()
    date_idx = {d: i for i, d in enumerate(all_dates)}
    realized["realize_date"] = realized["date"].map(
        lambda d: all_dates[date_idx[d] + 1] if date_idx.get(d, -1) + 1 < len(all_dates) else None
    )
    realized = realized.dropna(subset=["realize_date"])
    daily = realized.groupby("realize_date")["nxt_ret"].mean()
    out = pd.Series(0.0, index=all_dates)
    for d, r in daily.items():
        if d in out.index:
            out.at[d] = r
    return out


def _summarize_variant(
    variant_id: str,
    label: str,
    tradable: bool,
    picks: pd.DataFrame,
    all_dates: list[str],
) -> VariantResult:
    n_cal = len(all_dates)
    if picks.empty:
        return VariantResult(
            variant_id=variant_id,
            label=label,
            tradable=tradable,
            n_events=0,
            n_invested_days=0,
            n_calendar_days=n_cal,
            event_mean_pct=0.0,
            event_win_rate=0.0,
            p_burst_pct=0.0,
            daily_mean_pct=0.0,
            daily_win_rate=0.0,
            total_return_pct=0.0,
            cagr_pct=0.0,
            sharpe=0.0,
            max_drawdown_pct=0.0,
            avg_names_per_day=0.0,
        )

    daily_rets = _equity_curve(picks, all_dates)
    invested = daily_rets[daily_rets != 0]
    equity = (1 + daily_rets / 100).cumprod()
    peak = equity.cummax()
    dd = (equity / peak - 1) * 100
    years = max(n_cal / 252, 1 / 252)
    total_ret = (equity.iloc[-1] - 1) * 100
    cagr = ((equity.iloc[-1]) ** (1 / years) - 1) * 100 if equity.iloc[-1] > 0 else -100.0
    sharpe = 0.0
    if invested.std() > 0 and len(invested) > 1:
        sharpe = float(invested.mean() / invested.std() * np.sqrt(252))

    signal_days = picks["date"].nunique()
    return VariantResult(
        variant_id=variant_id,
        label=label,
        tradable=tradable,
        n_events=len(picks),
        n_invested_days=int(len(invested)),
        n_calendar_days=n_cal,
        event_mean_pct=round(float(picks["nxt_ret"].mean()), 4),
        event_win_rate=round(float((picks["nxt_ret"] > 0).mean()), 4),
        p_burst_pct=round(float((picks["nxt_ret"] >= 5).mean()), 4),
        daily_mean_pct=round(float(invested.mean()) if len(invested) else 0.0, 4),
        daily_win_rate=round(float((invested > 0).mean()) if len(invested) else 0.0, 4),
        total_return_pct=round(total_ret, 2),
        cagr_pct=round(cagr, 2),
        sharpe=round(sharpe, 2),
        max_drawdown_pct=round(float(dd.min()), 2),
        avg_names_per_day=round(len(picks) / signal_days, 2),
    )


def run_subset_backtest(*, oos_days: int = 252) -> dict:
    conn = connect()
    df, meta = build_lifecycle_events(conn)
    all_dates = sorted(df["date"].unique())
    variants = _apply_variants(df)

    results = [
        _summarize_variant(vid, _labels()[vid], _tradable()[vid], sub, all_dates)
        for vid, sub in variants.items()
    ]

    # OOS: last N signal days
    if len(all_dates) > oos_days:
        oos_start = all_dates[-oos_days]
        oos_dates = [d for d in all_dates if d >= oos_start]
        oos_results = [
            _summarize_variant(
                f"oos_{vid}",
                f"OOS · {_labels()[vid]}",
                _tradable()[vid],
                sub[sub["date"] >= oos_start],
                oos_dates,
            )
            for vid, sub in variants.items()
            if vid.startswith("s3rv")
        ]
    else:
        oos_results = []

    # Benchmark: TWII next-day on all calendar days
    bench = df.drop_duplicates("date")[["date", "bench_nxt_ret"]].set_index("date")["bench_nxt_ret"]
    bench = bench.reindex(all_dates).fillna(0)
    bench_equity = (1 + bench / 100).cumprod()
    bench_total = (bench_equity.iloc[-1] - 1) * 100
    years = max(len(all_dates) / 252, 1 / 252)
    bench_cagr = ((bench_equity.iloc[-1]) ** (1 / years) - 1) * 100

    return {
        "meta": {**meta, "oos_days": oos_days, "generated_on": date.today().isoformat()},
        "variants": [asdict(r) for r in results],
        "oos_variants": [asdict(r) for r in oos_results],
        "benchmark": {
            "label": "台指 IX0001 隔日 buy-hold",
            "total_return_pct": round(bench_total, 2),
            "cagr_pct": round(bench_cagr, 2),
        },
    }


def _labels() -> dict[str, str]:
    return {
        "s3rv_all": "S3+RV98-99 全部",
        "s3rv_sig_mkt_up": "S3 RV + 信號日台指≥0（可交易）",
        "s3rv_oracle_mkt_up": "S3 RV + 隔日台指≥0（oracle · 不可交易）",
        "s3rv_sid_pos": "S3 RV + 個股 S3 後史≥3 且 mean>0（PIT）",
        "s3rv_tradable": "S3 RV + 信號日台指≥0 + 個股史>0",
        "s3rv_tradable_relaxed": "S3 RV + 信號日台指≥0 +（個股史>0 或 史<3）",
        "s2_core": "S2 Setup core（對照）",
    }


def _tradable() -> dict[str, bool]:
    return {
        "s3rv_all": True,
        "s3rv_sig_mkt_up": True,
        "s3rv_oracle_mkt_up": False,
        "s3rv_sid_pos": True,
        "s3rv_tradable": True,
        "s3rv_tradable_relaxed": True,
        "s2_core": True,
    }


def render_markdown(result: dict) -> str:
    meta = result["meta"]
    lines = [
        "# S3 + RV 98–99 subset backtest",
        "",
        f"- Universe: **{meta['universe_size']}** · {meta['date_start']} → {meta['date_end']}",
        f"- 規則: D 收盤等權買入 · D+1 收盤賣出 · 無成本 · 無訊號日報酬 0%",
        f"- Generated: {result['meta']['generated_on']}",
        "",
        "## 全樣本",
        "",
        "| Variant | 可交易 | N | 投入日 | 每筆均值 | 勝率 | P(≥5%) | CAGR | 總報酬 | Sharpe | MaxDD |",
        "|---------|--------|---|--------|---------|------|--------|------|--------|--------|-------|",
    ]
    for v in result["variants"]:
        trad = "✓" if v["tradable"] else "oracle"
        lines.append(
            f"| {v['label']} | {trad} | {v['n_events']} | {v['n_invested_days']} | "
            f"{v['event_mean_pct']:+.3f}% | {v['event_win_rate']:.1%} | {v['p_burst_pct']:.1%} | "
            f"{v['cagr_pct']:+.2f}% | {v['total_return_pct']:+.1f}% | {v['sharpe']:.2f} | "
            f"{v['max_drawdown_pct']:.1f}% |"
        )
    b = result["benchmark"]
    lines.extend(
        [
            "",
            f"基準 **{b['label']}**: 總報酬 {b['total_return_pct']:+.1f}% · CAGR {b['cagr_pct']:+.2f}%",
            "",
            "> **解讀**: oracle 列使用隔日台指方向 · 僅作上界參考。CAGR 以全曆日 compounding（無訊號=0%）計。",
            "",
        ]
    )
    if result["oos_variants"]:
        lines.extend(["## OOS 近 252 交易日", ""])
        lines.append("| Variant | N | 每筆均值 | 勝率 | CAGR | 總報酬 | MaxDD |")
        lines.append("|---------|---|---------|------|------|--------|-------|")
        for v in result["oos_variants"]:
            lines.append(
                f"| {v['label']} | {v['n_events']} | {v['event_mean_pct']:+.3f}% | "
                f"{v['event_win_rate']:.1%} | {v['cagr_pct']:+.2f}% | "
                f"{v['total_return_pct']:+.1f}% | {v['max_drawdown_pct']:.1f}% |"
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="S3 RV subset backtest")
    parser.add_argument("--oos-days", type=int, default=252)
    args = parser.parse_args()

    result = run_subset_backtest(oos_days=args.oos_days)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    json_path = REPORT_DIR / f"s3rv_subset_backtest_{stamp}.json"
    md_path = REPORT_DIR / f"s3rv_subset_backtest_{stamp}.md"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    print(render_markdown(result))
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
