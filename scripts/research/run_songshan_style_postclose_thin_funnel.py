#!/usr/bin/env python3
"""Post-close thin funnel: T0 × 大盤 × 籌碼 × 夜盤 on Songshan-style legs.

Question: after signal is known at T0 close, can light gates (high keep-rate)
lift median r_adj before T+1 open entry?

PIT / ops timing (adjusted):
  - T0 OHLC / IX0001 / institutional: known at/after T0 close (evening)
  - NQ overnight: checkable pre-open (~08:50+); research DB uses 09:30 label as
    backtest proxy (≈09:00 on 1h bars; not a live hard wait until 09:30)
  - Stock gap / chase vs T0 close: known at 09:00 open (soft note; hard chase skip hurts)
  - Songshan Order window 09:25–09:40 is for entry_25m_nonfail, unrelated to NQ

  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_songshan_style_postclose_thin_funnel.py \\
    --db data/replica/stocks.db
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from statistics import median

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = (
    ROOT
    / "reports/research/branch-footprint-screen"
    / "songshan_style_adaptive_top30"
)
LEGS = OUT / "t0_open_legs.csv"
OOS_CUT = "2026-01-01"
ANCHOR = "9217"
# Thin = keep at least this fraction (unless marked exploratory).
THIN_KEEP = 0.60


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def fmt(x: float | None) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return ""
    return f"{x:+.2f}"


def connect_ro(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=120)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    return c


def pack(df: pd.DataFrame) -> dict:
    if df is None or len(df) == 0:
        return {
            "n": 0,
            "med": float("nan"),
            "mean": float("nan"),
            "win": float("nan"),
            "n_oos": 0,
            "med_oos": float("nan"),
        }
    xs = df["radj_pct"].astype(float).tolist()
    oos = df[df["signal_date"] >= OOS_CUT]
    ox = oos["radj_pct"].astype(float).tolist()
    return {
        "n": len(df),
        "med": float(median(xs)),
        "mean": float(np.mean(xs)),
        "win": float(np.mean([v > 0 for v in xs])),
        "n_oos": len(oos),
        "med_oos": float(median(ox)) if ox else float("nan"),
    }


def load_ix_day_ret(conn: sqlite3.Connection) -> pd.Series:
    rows = conn.execute(
        """
        SELECT date, close FROM daily_bars
        WHERE code='IX0001' AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
               WHEN 'finmind' THEN 2 ELSE 3 END
        """
    ).fetchall()
    by: dict[str, float] = {}
    for r in rows:
        by.setdefault(str(r[0]), float(r[1]))
    dates = sorted(by)
    closes = [by[d] for d in dates]
    rets = [float("nan")] + [
        closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))
    ]
    return pd.Series(rets, index=dates, name="ix_t0_ret")


def load_spy_day_ret(conn: sqlite3.Connection) -> pd.Series:
    rows = conn.execute(
        """
        SELECT trade_date, close FROM us_daily_bars
        WHERE ticker='SPY' AND close>0
        ORDER BY trade_date,
          CASE source WHEN 'yahoo' THEN 0 ELSE 1 END
        """
    ).fetchall()
    by: dict[str, float] = {}
    for r in rows:
        by.setdefault(str(r[0]), float(r[1]))
    dates = sorted(by)
    closes = [by[d] for d in dates]
    rets = [float("nan")] + [
        closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))
    ]
    return pd.Series(rets, index=dates, name="spy_t0_ret")


def load_nq_overnight_into_entry(conn: sqlite3.Connection) -> pd.DataFrame:
    """NQ/ES overnight % for entry morning.

    Research proxy label=09:30 (DB default). Live ops may preview ~08:50;
    agreement vs 09:30 is high but not perfect near zero.
    """
    df = pd.read_sql_query(
        """
        SELECT tw_session_date AS entry_date,
               nq_overnight_pct, es_overnight_pct
        FROM us_futures_overnight_snapshot
        WHERE capture_label='09:30'
        ORDER BY tw_session_date,
          CASE source WHEN 'yahoo_1h' THEN 0 ELSE 1 END
        """,
        conn,
    )
    if df.empty:
        return df
    df = df.drop_duplicates("entry_date", keep="first")
    df["entry_date"] = df["entry_date"].astype(str)
    # stored as percent points already (e.g. -0.90 = -0.90%)
    df["nq_ovn"] = df["nq_overnight_pct"].astype(float) / 100.0
    df["es_ovn"] = df["es_overnight_pct"].astype(float) / 100.0
    return df[["entry_date", "nq_ovn", "es_ovn"]]


def load_chip(conn: sqlite3.Connection, sids: list[str], start: str, end: str) -> pd.DataFrame:
    if not sids:
        return pd.DataFrame()
    parts = []
    for i in range(0, len(sids), 400):
        chunk = sids[i : i + 400]
        ph = ",".join("?" * len(chunk))
        parts.append(
            pd.read_sql_query(
                f"""
                SELECT stock_id, trade_date,
                       foreign_net, investment_trust_net, three_institution_net
                FROM stock_institutional_daily
                WHERE source='finmind'
                  AND stock_id IN ({ph})
                  AND trade_date>=? AND trade_date<=?
                """,
                conn,
                params=[*chunk, start, end],
            )
        )
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if df.empty:
        return df
    df["stock_id"] = df["stock_id"].astype(str)
    df["trade_date"] = df["trade_date"].astype(str)
    for c in ("foreign_net", "investment_trust_net", "three_institution_net"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def eval_filter(df: pd.DataFrame, name: str, mask: pd.Series, base: dict) -> dict:
    keep = df.loc[mask.fillna(False)]
    drop = df.loc[~mask.fillna(False)]
    sk, sd = pack(keep), pack(drop)
    return {
        "filter": name,
        "n_keep": sk["n"],
        "keep_rate": sk["n"] / base["n"] if base["n"] else float("nan"),
        "med_keep": sk["med"],
        "win_keep": sk["win"],
        "delta_med": sk["med"] - base["med"] if sk["n"] else float("nan"),
        "n_drop": sd["n"],
        "med_drop": sd["med"],
        "n_oos_keep": sk["n_oos"],
        "med_oos_keep": sk["med_oos"],
        "thin": int(sk["n"] / base["n"] >= THIN_KEEP) if base["n"] else 0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=ROOT / "data/replica/stocks.db")
    ap.add_argument("--legs", type=Path, default=LEGS)
    args = ap.parse_args()
    t0 = time.time()

    if not args.legs.exists():
        raise SystemExit(f"missing legs: {args.legs} (run t0_open_funnel first)")

    legs = pd.read_csv(args.legs)
    legs["signal_date"] = legs["signal_date"].astype(str)
    legs["entry_date"] = legs["entry_date"].astype(str)
    legs["stock_id"] = legs["stock_id"].astype(str)
    legs["branch_id"] = legs["branch_id"].astype(str)
    log(f"legs n={len(legs)} songshan={int((legs.branch_id==ANCHOR).sum())}")

    conn = connect_ro(args.db)
    try:
        ix = load_ix_day_ret(conn)
        spy = load_spy_day_ret(conn)
        ovn = load_nq_overnight_into_entry(conn)
        log(f"ix days={len(ix)} spy={len(spy)} ovn={len(ovn)}")

        sids = sorted(legs["stock_id"].unique())
        chip = load_chip(
            conn,
            sids,
            start=str(legs["signal_date"].min()),
            end=str(legs["signal_date"].max()),
        )
        log(f"chip rows={len(chip):,}")
    finally:
        conn.close()

    # Enrich
    legs["ix_t0_ret"] = legs["signal_date"].map(ix)
    legs["spy_t0_ret"] = legs["signal_date"].map(spy)
    legs = legs.merge(ovn, on="entry_date", how="left")
    if len(chip):
        chip = chip.rename(
            columns={
                "trade_date": "signal_date",
                "foreign_net": "fr_net",
                "investment_trust_net": "it_net",
                "three_institution_net": "tri_net",
            }
        )
        legs = legs.merge(chip, on=["stock_id", "signal_date"], how="left")
    else:
        legs["fr_net"] = np.nan
        legs["it_net"] = np.nan
        legs["tri_net"] = np.nan

    # Derived thin features
    legs["t0_green"] = legs["t0_green"].astype(int)
    legs["ix_up"] = legs["ix_t0_ret"] > 0
    legs["ix_not_crash"] = legs["ix_t0_ret"] > -0.01
    legs["ix_gt_m05"] = legs["ix_t0_ret"] > -0.005
    legs["spy_up"] = legs["spy_t0_ret"] > 0
    legs["spy_not_crash"] = legs["spy_t0_ret"] > -0.01
    legs["nq_ovn_ok"] = legs["nq_ovn"] > -0.005  # > -0.5%
    legs["nq_ovn_ok08"] = legs["nq_ovn"] > -0.008
    legs["nq_ovn_pos"] = legs["nq_ovn"] > 0
    legs["es_ovn_ok"] = legs["es_ovn"] > -0.005
    legs["fr_pos"] = legs["fr_net"] > 0
    legs["it_pos"] = legs["it_net"] > 0
    legs["tri_pos"] = legs["tri_net"] > 0
    legs["fr_or_it"] = (legs["fr_net"] > 0) | (legs["it_net"] > 0)
    legs["close_up"] = legs["t0_close_loc"] >= 0.5
    legs["t0_not_melt"] = legs["t0_ret"] > -0.03
    legs["chase_soft5"] = legs["chase"] <= 0.05

    # Coverage diagnostics
    cov = {
        "n": int(len(legs)),
        "ix_ok": int(legs["ix_t0_ret"].notna().sum()),
        "spy_ok": int(legs["spy_t0_ret"].notna().sum()),
        "nq_ovn_ok": int(legs["nq_ovn"].notna().sum()),
        "fr_ok": int(legs["fr_net"].notna().sum()),
        "nq_ovn_range": [
            str(legs["entry_date"][legs["nq_ovn"].notna()].min())
            if legs["nq_ovn"].notna().any()
            else None,
            str(legs["entry_date"][legs["nq_ovn"].notna()].max())
            if legs["nq_ovn"].notna().any()
            else None,
        ],
    }
    log(f"coverage {cov}")

    filters: list[tuple[str, pd.Series]] = [
        ("baseline", pd.Series(True, index=legs.index)),
        # T0 thin
        ("T0 綠K", legs["t0_green"] == 1),
        ("T0 close_loc≥0.5", legs["close_up"]),
        ("T0 ret>-3%", legs["t0_not_melt"]),
        ("T0 綠K ∩ close_loc≥0.5", (legs["t0_green"] == 1) & legs["close_up"]),
        # 大盤
        ("大盤 T0>0", legs["ix_up"]),
        ("大盤 T0>-1%", legs["ix_not_crash"]),
        ("大盤 T0>-0.5%", legs["ix_gt_m05"]),
        # 籌碼
        ("外資 T0>0", legs["fr_pos"]),
        ("投信 T0>0", legs["it_pos"]),
        ("三大 T0>0", legs["tri_pos"]),
        ("外資或投信>0", legs["fr_or_it"]),
        # 夜盤 / US
        ("NQ夜盤>-0.5%（開盤前；研究proxy=09:30）", legs["nq_ovn_ok"]),
        ("NQ夜盤>-0.8%", legs["nq_ovn_ok08"]),
        ("NQ夜盤>0（開盤前；研究proxy=09:30）", legs["nq_ovn_pos"]),
        ("ES夜盤>-0.5%", legs["es_ovn_ok"]),
        ("SPY同日>0", legs["spy_up"]),
        ("SPY同日>-1%", legs["spy_not_crash"]),
        # thin combos
        (
            "薄: 綠K ∩ 大盤>-1%",
            (legs["t0_green"] == 1) & legs["ix_not_crash"],
        ),
        (
            "薄: close上半 ∩ 大盤>-1%",
            legs["close_up"] & legs["ix_not_crash"],
        ),
        (
            "薄: 外資>0 ∩ 大盤>-1%",
            legs["fr_pos"] & legs["ix_not_crash"],
        ),
        (
            "薄: 綠K ∩ 外資>0",
            (legs["t0_green"] == 1) & legs["fr_pos"],
        ),
        (
            "薄: 綠K ∩ NQ>-0.5%",
            (legs["t0_green"] == 1) & legs["nq_ovn_ok"],
        ),
        (
            "薄: 大盤>-1% ∩ NQ>-0.5%",
            legs["ix_not_crash"] & legs["nq_ovn_ok"],
        ),
        (
            "薄: close上半 ∩ 外資>0 ∩ 大盤>-1%",
            legs["close_up"] & legs["fr_pos"] & legs["ix_not_crash"],
        ),
        (
            "薄: 綠K ∩ close上半 ∩ 大盤>-1%",
            (legs["t0_green"] == 1) & legs["close_up"] & legs["ix_not_crash"],
        ),
        (
            "薄+: 綠K ∩ 大盤>-1% ∩ NQ>-0.5%",
            (legs["t0_green"] == 1)
            & legs["ix_not_crash"]
            & legs["nq_ovn_ok"],
        ),
        (
            "薄+: close上半 ∩ 外資>0 ∩ NQ>-0.5%",
            legs["close_up"] & legs["fr_pos"] & legs["nq_ovn_ok"],
        ),
    ]

    rows = []
    for scope, sub in (
        ("songshan", legs[legs["branch_id"] == ANCHOR].copy()),
        ("pool_robust", legs.copy()),
    ):
        base = pack(sub)
        for name, mask in filters:
            # align mask to sub index
            m = mask.reindex(sub.index)
            row = eval_filter(sub, name, m, base)
            row["scope"] = scope
            row["base_med"] = base["med"]
            row["base_n"] = base["n"]
            rows.append(row)

    funnel = pd.DataFrame(rows)

    # Q1 vs Q4 contrast on enriched features (pool)
    pool = legs
    radj = pool["radj_pct"].astype(float)
    lo, hi = radj.quantile(0.25), radj.quantile(0.75)
    bad, good = pool[radj <= lo], pool[radj >= hi]

    def avg(df: pd.DataFrame, col: str) -> float:
        s = pd.to_numeric(df[col], errors="coerce")
        return float(s.mean()) if s.notna().any() else float("nan")

    contrast_cols = [
        "t0_ret",
        "t0_green",
        "t0_close_loc",
        "chase",
        "ix_t0_ret",
        "spy_t0_ret",
        "nq_ovn",
        "es_ovn",
        "fr_net",
        "it_net",
        "tri_net",
    ]

    # Write artifacts
    OUT.mkdir(parents=True, exist_ok=True)
    legs_path = OUT / "postclose_thin_legs.csv"
    funnel_path = OUT / "postclose_thin_funnel.csv"
    md_path = OUT / "POSTCLOSE_THIN_FUNNEL.md"
    legs.to_csv(legs_path, index=False)
    funnel.to_csv(funnel_path, index=False)

    lines = [
        "# Songshan-style · 收盤後薄篩（T0 × 大盤 × 籌碼 × 夜盤）",
        "",
        f"> Generated {datetime.now().isoformat(timespec='seconds')} · Research only",
        "",
        "## 一句話",
        "",
        "先前 Top30 **沒有**收盤後薄篩。本表在已有 T0 腿上加上大盤／籌碼／夜盤，"
        f"並標 **thin=keep≥{THIN_KEEP:.0%}**；看 Δmed 是否真的抬升。",
        "",
        "**時序已調整**：籌碼在 T0 晚間判；NQ／跳空在開盤前～09:00 判；"
        "09:25 只為松山 `entry_25m_nonfail`，**不是**為了等 NQ。",
        "",
        "## 操作時序（調整後 · 不必為 NQ 等到 09:30）",
        "",
        "| 步驟 | 時間 | 做什麼 |",
        "|---|---|---|",
        "| 1 | **T0 收盤後晚上** | 查訊號股外資／投信是否買超（籌碼薄篩） |",
        "| 2 | **T+1 約 08:50 起** | 預覽 NQ 隔夜；卡在 0 附近再看到開盤前後 |",
        "| 3 | **T+1 09:00** | 看個股相對昨收有無暴衝（軟註記；勿硬砍 chase≤3%） |",
        "| 4 | **T+1 09:25–09:40** | 松山 Order 窗 + `entry_25m_nonfail`（與 NQ 無關） |",
        "",
        "> 研究庫 NQ 欄位仍用 **09:30 snapshot 當回測代理**（1h K 下與 09:00 幾乎相同）。"
        " live **不必**為了 NQ 硬等到 09:30；08:50 與 09:30 在 `NQ>0` 約 82% 一致、"
        "`>−0.5%` 約 88% 一致。",
        "",
        "## PIT",
        "",
        "| 特徵 | 何時可知 |",
        "|---|---|",
        "| T0 日K／IX0001 當日 | T0 收盤 |",
        "| 三大法人 T0 | T0 收盤後晚上（live 確認發布） |",
        "| SPY 同曆日收 | T+1 開盤前（美股收後） |",
        "| NQ/ES 隔夜 | **開盤前即可**（約 08:50+）；DB 對照標籤 09:30 |",
        "| 個股跳空／追價 | T+1 **09:00** 開盤 |",
        "| entry_25m_nonfail | T+1 **09:25** 起（Order 盤中閘門） |",
        "",
        f"Coverage: legs={cov['n']} · ix={cov['ix_ok']} · spy={cov['spy_ok']} · "
        f"nq_ovn={cov['nq_ovn_ok']} · chip={cov['fr_ok']} · "
        f"nq_ovn window={cov['nq_ovn_range']}",
        "",
    ]

    for scope in ("songshan", "pool_robust"):
        sub = legs if scope == "pool_robust" else legs[legs.branch_id == ANCHOR]
        st = pack(sub)
        lines += [
            f"### baseline · {scope}",
            "",
            f"- n={st['n']} · med={fmt(st['med'])}% · win={st['win']:.0%} · "
            f"oos_n={st['n_oos']} · oos_med={fmt(st['med_oos'])}%",
            "",
        ]

    lines += [
        "## 差筆 vs 好筆（pool Q1/Q4 平均）",
        "",
        f"Q1 n={len(bad)} r_adj≤{lo:+.2f}% · Q4 n={len(good)} r_adj≥{hi:+.2f}%",
        "",
        "| feature | 差 | 好 | Δ |",
        "|---|---:|---:|---:|",
    ]
    for c in contrast_cols:
        a, b = avg(bad, c), avg(good, c)
        if c in ("t0_ret", "chase", "ix_t0_ret", "spy_t0_ret", "nq_ovn", "es_ovn"):
            lines.append(
                f"| {c} | {fmt(a*100) if not math.isnan(a) else ''}% | "
                f"{fmt(b*100) if not math.isnan(b) else ''}% | "
                f"{fmt((a-b)*100) if not (math.isnan(a) or math.isnan(b)) else ''}pp |"
            )
        elif c in ("fr_net", "it_net", "tri_net"):
            lines.append(
                f"| {c} | {a:,.0f} | {b:,.0f} | {a-b:,.0f} |"
                if not (math.isnan(a) or math.isnan(b))
                else f"| {c} |  |  |  |"
            )
        else:
            lines.append(
                f"| {c} | {a:.3f} | {b:.3f} | {a-b:+.3f} |"
                if not (math.isnan(a) or math.isnan(b))
                else f"| {c} |  |  |  |"
            )
    lines.append("")

    for scope in ("songshan", "pool_robust"):
        subf = funnel[funnel.scope == scope].copy()
        lines += [f"## 漏斗 · {scope}", ""]
        # thin winners first
        thin = subf[(subf.thin == 1) & (subf["filter"] != "baseline")].sort_values(
            "delta_med", ascending=False
        )
        lines += [
            f"### thin keep≥{THIN_KEEP:.0%} · 按 Δmed",
            "",
            "| filter | keep% | n | med | Δmed | drop_med | oos_med |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for r in thin.itertuples(index=False):
            lines.append(
                f"| {r.filter} | {r.keep_rate:.0%} | {int(r.n_keep)} | "
                f"{fmt(r.med_keep)} | {fmt(r.delta_med)} | {fmt(r.med_drop)} | "
                f"{fmt(r.med_oos_keep)} |"
            )
        lines.append("")
        lines += [
            "### 全表",
            "",
            "| filter | thin | keep% | n | med | Δmed | drop_med | oos_med |",
            "|---|:---:|---:|---:|---:|---:|---:|---:|",
        ]
        for r in subf.itertuples(index=False):
            lines.append(
                f"| {r.filter} | {'Y' if r.thin else ''} | "
                f"{'' if pd.isna(r.keep_rate) else f'{r.keep_rate:.0%}'} | "
                f"{int(r.n_keep)} | {fmt(r.med_keep)} | {fmt(r.delta_med)} | "
                f"{fmt(r.med_drop)} | {fmt(r.med_oos_keep)} |"
            )
        lines.append("")

    # Verdict
    lines += ["## 判決", ""]
    for scope in ("songshan", "pool_robust"):
        subf = funnel[funnel.scope == scope]
        thin = subf[(subf.thin == 1) & (subf["filter"] != "baseline")].sort_values(
            "delta_med", ascending=False
        )
        lines.append(f"**{scope}**")
        if thin.empty:
            lines.append("- 無 thin 規則抬升 med")
        else:
            top = thin.head(5)
            for r in top.itertuples(index=False):
                lines.append(
                    f"- `{r.filter}`：keep {r.keep_rate:.0%} · "
                    f"med {fmt(r.med_keep)}% (Δ {fmt(r.delta_med)}pp) · "
                    f"oos_med {fmt(r.med_oos_keep)}%"
                )
            best = thin.iloc[0]
            if best["delta_med"] < 0.5:
                lines.append(
                    f"- 最佳 thin Δmed 僅 {fmt(best['delta_med'])}pp → "
                    "**沒有明顯抬升**（薄篩幾乎無效）"
                )
            elif best["delta_med"] < 1.5:
                lines.append(
                    f"- 最佳 thin Δmed {fmt(best['delta_med'])}pp → "
                    "**輕微有用**，可當軟註記"
                )
            else:
                lines.append(
                    f"- 最佳 thin Δmed {fmt(best['delta_med'])}pp → "
                    "**有實質抬升**，可考慮進觀測規則"
                )
        lines.append("")

    lines += [
        "## 檔案",
        "",
        f"- `{legs_path.relative_to(ROOT)}`",
        f"- `{funnel_path.relative_to(ROOT)}`",
        "",
        f"elapsed {time.time()-t0:.1f}s",
        "",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"wrote {md_path}")

    # stdout digest
    for scope in ("songshan", "pool_robust"):
        print(f"\n=== {scope} thin top ===")
        subf = funnel[funnel.scope == scope]
        thin = subf[(subf.thin == 1) & (subf["filter"] != "baseline")].sort_values(
            "delta_med", ascending=False
        )
        for r in thin.head(8).itertuples(index=False):
            print(
                f"{r.filter}: keep={r.keep_rate:.0%} n={int(r.n_keep)} "
                f"med={fmt(r.med_keep)} Δ={fmt(r.delta_med)} "
                f"oos={fmt(r.med_oos_keep)}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
