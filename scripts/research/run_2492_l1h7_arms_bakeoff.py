#!/usr/bin/env python3
"""2492 華新科 · L1H7 copytrade arms bakeoff vs RAISE median r_adj.

Research only · Book · finmind · readonly DB.
Protocol: signal T → T+1 open → H7 close −30bps; r_adj = r − 1.15×IX0001.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

OUT = ROOT / "reports/research/chip-overlays/2492_knowledge/surge_branch"
PANEL = ROOT / "reports/research/chip-overlays/2492_knowledge/panel_daily.csv"
DB = ROOT / "data" / "stocks.db"
SID = "2492"
START, END, OOS = "2024-07-01", "2026-07-22", "2026-01-01"
COST, HOLD, BETA, DEDUP = 0.003, 7, 1.15, 5
TOP_N = 10
BUY_FLOOR = 3e6  # 300萬

FOREIGN_KW = (
    "美商", "摩根", "瑞銀", "美林", "港商", "野村", "瑞士", "花旗", "渣打",
    "香港", "新加坡", "大和", "法銀", "德意志", "巴克萊", "高盛", "美銀",
)

SOFT = ["9135", "9623", "9A89", "9239", "980w"]
WATCH = ["9135", "9623", "9A89", "9239", "980w", "9654", "9692", "9875"]
DOM_A = ["9239", "983Z", "5850", "9654", "9666"]
EARLY = ["9A99", "9A9Q", "9875", "9692"]


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


def episode_dedup(dates: list[pd.Timestamp], gap_days: int) -> list[pd.Timestamp]:
    kept: list[pd.Timestamp] = []
    last: pd.Timestamp | None = None
    for d in sorted(dates):
        if last is None or (d - last).days > gap_days:
            kept.append(d)
            last = d
    return kept


def l1h7_radj(
    sig: pd.Timestamp,
    cal: list[pd.Timestamp],
    idx: dict[pd.Timestamp, int],
    stock_oc: dict[pd.Timestamp, tuple[float, float]],
    ix_oc: dict[pd.Timestamp, tuple[float, float]],
) -> tuple[float, float, str, str] | None:
    i = idx.get(sig)
    if i is None:
        return None
    entry_i = i + 1
    exit_i = entry_i + HOLD - 1
    if exit_i >= len(cal):
        return None
    ed, xd = cal[entry_i], cal[exit_i]
    so = stock_oc.get(ed)
    sx = stock_oc.get(xd)
    io = ix_oc.get(ed)
    ix = ix_oc.get(xd)
    if not so or not sx or not io or not ix:
        return None
    if so[0] <= 0 or sx[1] <= 0 or io[0] <= 0 or ix[1] <= 0:
        return None
    r = sx[1] / so[0] - 1.0 - COST
    r_ix = ix[1] / io[0] - 1.0
    return r - BETA * r_ix, r, ed.date().isoformat(), xd.date().isoformat()


def summarize(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {
            "n": 0,
            "n_is": 0,
            "n_oos": 0,
            "med_radj": np.nan,
            "mean_radj": np.nan,
            "med_r": np.nan,
            "win_radj": np.nan,
            "med_is": np.nan,
            "med_oos": np.nan,
            "mean_oos": np.nan,
            "win_oos": np.nan,
        }
    is_m = trades["split"] == "IS"
    oos_m = trades["split"] == "OOS"
    def _med(s: pd.Series) -> float:
        return float(s.median()) if len(s) else float("nan")

    def _mean(s: pd.Series) -> float:
        return float(s.mean()) if len(s) else float("nan")

    def _win(s: pd.Series) -> float:
        return float((s > 0).mean()) if len(s) else float("nan")

    return {
        "n": int(len(trades)),
        "n_is": int(is_m.sum()),
        "n_oos": int(oos_m.sum()),
        "med_radj": _med(trades["r_adj"]),
        "mean_radj": _mean(trades["r_adj"]),
        "med_r": _med(trades["r"]),
        "win_radj": _win(trades["r_adj"]),
        "med_is": _med(trades.loc[is_m, "r_adj"]),
        "med_oos": _med(trades.loc[oos_m, "r_adj"]),
        "mean_oos": _mean(trades.loc[oos_m, "r_adj"]),
        "win_oos": _win(trades.loc[oos_m, "r_adj"]),
    }


def score_arm(
    name: str,
    family: str,
    signal_dates: list[pd.Timestamp],
    *,
    use_episode_dedup: bool,
    cal: list[pd.Timestamp],
    idx: dict[pd.Timestamp, int],
    stock_oc: dict[pd.Timestamp, tuple[float, float]],
    ix_oc: dict[pd.Timestamp, tuple[float, float]],
) -> tuple[dict, pd.DataFrame]:
    dates = sorted(set(signal_dates))
    if use_episode_dedup:
        dates = episode_dedup(dates, DEDUP)
    rows = []
    for d in dates:
        out = l1h7_radj(d, cal, idx, stock_oc, ix_oc)
        if out is None:
            continue
        radj, r, ed, xd = out
        rows.append(
            {
                "arm": name,
                "family": family,
                "signal_date": d.date().isoformat(),
                "entry_date": ed,
                "exit_date": xd,
                "r": r,
                "r_adj": radj,
                "split": "OOS" if d >= pd.Timestamp(OOS) else "IS",
            }
        )
    trades = pd.DataFrame(rows)
    stats = summarize(trades)
    stats["arm"] = name
    stats["family"] = family
    stats["n_signals"] = len(dates)
    stats["dedup"] = f"{DEDUP}d" if use_episode_dedup else "day"
    return stats, trades


def pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and (np.isnan(x) or np.isinf(x))):
        return "—"
    return f"{x * 100:+.2f}%"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(f"file:{DB.resolve()}?mode=ro", uri=True, timeout=120)
    conn.execute("PRAGMA query_only=ON")

    bars = pd.read_sql_query(
        """
        SELECT trade_date AS date, open, close
        FROM stock_daily_bars
        WHERE source='finmind' AND stock_id=?
          AND trade_date BETWEEN ? AND ?
          AND close>0
        ORDER BY 1
        """,
        conn,
        params=[SID, START, END],
    )
    br = pd.read_sql_query(
        """
        SELECT trade_date AS date, securities_trader_id AS tid,
               securities_trader AS tname, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE source='finmind' AND stock_id=?
          AND trade_date BETWEEN ? AND ?
        """,
        conn,
        params=[SID, START, END],
    )
    ix_raw = pd.read_sql_query(
        """
        SELECT date, open, close, source
        FROM daily_bars
        WHERE code='IX0001' AND date BETWEEN ? AND ?
          AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
               WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        conn,
        params=[START, END],
    )
    conn.close()

    bars["date"] = pd.to_datetime(bars["date"])
    br["date"] = pd.to_datetime(br["date"])
    ix_raw["date"] = pd.to_datetime(ix_raw["date"])
    bars = bars.sort_values("date").drop_duplicates("date")
    # prefer first source per date (yahoo>tej>finmind)
    ix = ix_raw.drop_duplicates("date", keep="first")

    stock_oc = {
        r.date: (float(r.open if r.open and r.open > 0 else r.close), float(r.close))
        for r in bars.itertuples()
    }
    ix_oc = {r.date: (float(r.open), float(r.close)) for r in ix.itertuples()}
    cal = bars["date"].tolist()
    idx = {d: i for i, d in enumerate(cal)}

    br = br.merge(bars[["date", "close"]], on="date", how="left")
    br["tid"] = br["tid"].astype(str)
    br["buy_amt"] = br["buy"] * br["close"]
    br["foreign"] = br["tname"].map(is_foreign)
    br["domestic"] = ~br["foreign"]

    # daily buy-amt top10 (all seats)
    top_rows = []
    for d, day in br.groupby("date"):
        top = day.nlargest(TOP_N, "buy_amt")
        for rank, (_, r) in enumerate(top.iterrows(), 1):
            top_rows.append(
                {
                    "date": d,
                    "tid": str(r["tid"]),
                    "tname": r["tname"],
                    "buy_amt": float(r["buy_amt"]),
                    "rank": rank,
                    "domestic": bool(r["domestic"]),
                }
            )
    top10 = pd.DataFrame(top_rows)
    dom_top = top10[top10["domestic"]]

    # name map for soft seats
    name_map = (
        br.dropna(subset=["tname"])
        .groupby("tid")["tname"]
        .agg(lambda s: s.mode().iloc[0] if len(s.mode()) else s.iloc[0])
        .to_dict()
    )

    # chip panel (optional)
    chip_note = ""
    chip_ok = False
    scoop_by_date: dict[pd.Timestamp, bool] = {}
    dump_by_date: dict[pd.Timestamp, bool] = {}
    if PANEL.exists():
        panel = pd.read_csv(PANEL)
        panel["date"] = pd.to_datetime(panel["date"])
        need = {"rel_dump", "FT_ntd", "net_FT", "FT_resid_z"}
        missing = sorted(need - set(panel.columns))
        if missing:
            chip_note = f"panel missing columns {missing}; chip arms skipped"
        else:
            # scoop_resid_pos = rel_scoop ∧ FT_resid_z≥0.5
            # rel_scoop not always exported → rebuild from net_FT / FT_ntd
            if "rel_scoop" in panel.columns:
                scoop = panel["rel_scoop"].fillna(False).astype(bool)
            else:
                scoop = (panel["net_FT"] < 0) & (panel["FT_ntd"] > 0)
            scoop = scoop & panel["FT_resid_z"].ge(0.5).fillna(False)
            dump = panel["rel_dump"].fillna(False).astype(bool)
            scoop_by_date = dict(zip(panel["date"], scoop.astype(bool)))
            dump_by_date = dict(zip(panel["date"], dump.astype(bool)))
            chip_ok = True
            chip_note = (
                f"chip panel overlap {panel['date'].min().date()}.."
                f"{panel['date'].max().date()} (n={len(panel)}); "
                "scoop_resid = (net_FT<0 & FT_ntd>0 & FT_resid_z≥0.5)"
            )
    else:
        chip_note = f"panel CSV missing at {PANEL}; chip arms skipped"

    # --- signal date sets ---
    a0_dates = sorted(dom_top["date"].unique().tolist())

    soft_dates: dict[str, list[pd.Timestamp]] = {}
    for tid in SOFT:
        soft_dates[tid] = sorted(
            top10.loc[top10["tid"] == tid, "date"].unique().tolist()
        )

    def consensus_dates(seat_list: list[str], k: int) -> list[pd.Timestamp]:
        sub = top10[top10["tid"].isin(seat_list)]
        if sub.empty:
            return []
        cnt = sub.groupby("date")["tid"].nunique()
        return sorted(cnt[cnt >= k].index.tolist())

    a2_dates = consensus_dates(WATCH, 2)
    a3_dates = consensus_dates(DOM_A, 2)

    # A4: ≥3 domestic seats each buy_amt≥300萬 same day
    dom_big = br[(br["domestic"]) & (br["buy_amt"] >= BUY_FLOOR)]
    a4_cnt = dom_big.groupby("date")["tid"].nunique()
    a4_dates = sorted(a4_cnt[a4_cnt >= 3].index.tolist())

    # A5 / A6 chip
    a5_dates: list[pd.Timestamp] = []
    a6_dates: list[pd.Timestamp] = []
    if chip_ok:
        scoop_days = {d for d, v in scoop_by_date.items() if v}
        dump_days = {d for d, v in dump_by_date.items() if v}
        a2_set = set(a2_dates)
        a5_dates = sorted(scoop_days | a2_set)
        a6_dates = sorted(d for d in a2_set if d not in dump_days)

    # A7 precursor: early seat in top10 on any of T-5..T-2 → signal T
    early_top = top10[top10["tid"].isin(EARLY)]
    early_by_date = (
        early_top.groupby("date")["tid"].apply(lambda s: set(s.astype(str))).to_dict()
    )
    a7_dates: list[pd.Timestamp] = []
    for i, t in enumerate(cal):
        if i < 5:
            continue
        lookback = cal[i - 5 : i - 1]  # T-5..T-2 inclusive (4 days)
        hit = False
        for lb in lookback:
            if early_by_date.get(lb):
                hit = True
                break
        if hit:
            a7_dates.append(t)

    arms_spec: list[tuple[str, str, list[pd.Timestamp], bool]] = [
        ("A0_dom_top10_any", "baseline", a0_dates, False),
    ]
    for tid in SOFT:
        label = name_map.get(tid, tid)
        arms_spec.append(
            (f"A1_{tid}_{label}", "A1_soft_single", soft_dates[tid], True)
        )
    arms_spec += [
        ("A2_watch_k2", "consensus", a2_dates, False),
        ("A3_DOM_A_k2", "consensus", a3_dates, False),
        ("A4_dom_ge3_300w", "breadth", a4_dates, False),
    ]
    if chip_ok:
        arms_spec += [
            ("A5_scoop_OR_A2", "chip_or", a5_dates, False),
            ("A6_A2_NOT_rel_dump", "chip_and", a6_dates, False),
        ]
    else:
        arms_spec += [
            ("A5_scoop_OR_A2", "chip_or_SKIPPED", [], False),
            ("A6_A2_NOT_rel_dump", "chip_and_SKIPPED", [], False),
        ]
    arms_spec.append(("A7_early_precursor", "precursor", a7_dates, False))

    score_rows = []
    all_trades = []
    for name, family, dates, dedup in arms_spec:
        st, tr = score_arm(
            name,
            family,
            dates,
            use_episode_dedup=dedup,
            cal=cal,
            idx=idx,
            stock_oc=stock_oc,
            ix_oc=ix_oc,
        )
        score_rows.append(st)
        if not tr.empty:
            all_trades.append(tr)

    scores = pd.DataFrame(score_rows)
    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()

    a0 = scores.loc[scores["arm"] == "A0_dom_top10_any"].iloc[0]
    scores["lift_oos_vs_a0"] = scores["med_oos"] - a0["med_oos"]
    scores["lift_is_vs_a0"] = scores["med_is"] - a0["med_is"]
    scores["lift_full_vs_a0"] = scores["med_radj"] - a0["med_radj"]

    scores_path = OUT / "l1h7_arms_bakeoff_scores.csv"
    trades_path = OUT / "l1h7_arms_bakeoff_trades.csv"
    md_path = OUT / "L1H7_ARMS_BAKEOFF.md"
    scores.to_csv(scores_path, index=False)
    trades.to_csv(trades_path, index=False)

    # best OOS with n_oos>=8
    eligible = scores[scores["n_oos"] >= 8].copy()
    if not eligible.empty:
        best = eligible.sort_values("med_oos", ascending=False).iloc[0]
    else:
        best = None

    lines = [
        "# 華新科(2492) · L1H7 Copytrade Arms Bakeoff",
        "",
        "> Research only · **未採納** · finmind · Book",
        "",
        "## 協議",
        "",
        f"- 窗 `{START}`..`{END}` · OOS cut `{OOS}`",
        "- 訊號日 T（淨買／席位事件）→ **T+1 開**進場 → 持有 **7** 交易日收盤 − **30bps**",
        f"- **r_adj** = r − {BETA}×IX0001（IX 不加成本）",
        f"- 單席臂（A1）episode 去重 **{DEDUP}** 曆日；其餘臂一日一筆（不重疊去重）",
        "- top10 = 當日 **buy_amt=buy×close** 全席排名；本土以券商名關鍵字排除外資",
        "",
        f"- Chip：{chip_note}",
        "",
        "## Arms",
        "",
        "| Arm | 定義 |",
        "|-----|------|",
        "| A0 | ≥1 本土席在當日 buy-amt top10（基線） |",
        f"| A1 | soft 單席各別：{', '.join(SOFT)} 當日進 top10 |",
        f"| A2 | watchlist {{{','.join(WATCH)}}} 同日 ≥2 席 top10 |",
        f"| A3 | DOM_A {{{','.join(DOM_A)}}} 同日 ≥2 席 top10 |",
        "| A4 | 同日 ≥3 本土席各 buy_amt≥300萬 |",
        "| A5 | scoop_resid **OR** A2（需 panel） |",
        "| A6 | A2 **AND NOT** rel_dump（需 panel） |",
        f"| A7 | early {{{','.join(EARLY)}}} 在 T−5..T−2 任一進 top10 → 訊號 T |",
        "",
        "## 分數總表（按 OOS med r_adj）",
        "",
        "| Arm | n | n_IS | n_OOS | med r_adj | med IS | med OOS | win OOS | vs A0 OOS | dedup |",
        "|-----|--:|-----:|------:|----------:|-------:|--------:|--------:|----------:|-------|",
    ]
    show = scores.sort_values("med_oos", ascending=False, na_position="last")
    for _, r in show.iterrows():
        lines.append(
            f"| `{r['arm']}` | {r['n']} | {r['n_is']} | {r['n_oos']} | "
            f"{pct(r['med_radj'])} | {pct(r['med_is'])} | {pct(r['med_oos'])} | "
            f"{pct(r['win_oos']) if not np.isnan(r['win_oos']) else '—'} | "
            f"{pct(r['lift_oos_vs_a0'])} | {r['dedup']} |"
        )

    lines += ["", "## 判決", ""]
    if best is not None:
        lines += [
            f"- **最佳 OOS med r_adj（n_oos≥8）**: `{best['arm']}`",
            f"  - OOS med **{pct(best['med_oos'])}** · n_oos={int(best['n_oos'])}",
            f"  - IS med {pct(best['med_is'])} · n_is={int(best['n_is'])}",
            f"  - vs A0 OOS lift **{pct(best['lift_oos_vs_a0'])}** "
            f"(A0 OOS med {pct(a0['med_oos'])}, n_oos={int(a0['n_oos'])})",
            f"  - full-sample med r_adj {pct(best['med_radj'])}",
        ]
    else:
        lines.append("- 無臂達 n_oos≥8")

    # A1 soft ranking note
    a1 = scores[scores["family"] == "A1_soft_single"].sort_values(
        "med_oos", ascending=False
    )
    if not a1.empty:
        lines += ["", "### A1 soft 單席（OOS）", ""]
        for _, r in a1.iterrows():
            lines.append(
                f"- `{r['arm']}`: OOS {pct(r['med_oos'])} (n={int(r['n_oos'])}) · "
                f"IS {pct(r['med_is'])} · vs A0 {pct(r['lift_oos_vs_a0'])}"
            )

    lines += [
        "",
        "## 產出",
        "",
        f"- `{scores_path.relative_to(ROOT)}`",
        f"- `{trades_path.relative_to(ROOT)}`",
        "",
        f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(md_path)
    print(scores_path)
    if best is not None:
        print(
            f"BEST OOS: {best['arm']} med_oos={best['med_oos']:.4f} "
            f"n_oos={int(best['n_oos'])} vsA0={best['lift_oos_vs_a0']:.4f}"
        )
    print(scores[["arm", "n", "n_oos", "med_is", "med_oos", "lift_oos_vs_a0"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
