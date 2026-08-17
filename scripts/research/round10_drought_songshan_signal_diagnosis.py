#!/usr/bin/env python3
"""Round10 · 凱基松山（9217）跟單訊號乾涸診斷（純研究 · DB 唯讀 · 不下單）.

live 訊號 SSOT = scripts/research/run_songshan_follow_watch.py::scan_5d_net95
  滾動5交易日 buy_5d>=0.5億 ∩ net_ratio=(buy_5d-sell_5d)/buy_5d>=0.95 ∩ !mega
評估協議沿用 L1H7 SSOT（study_whale_branch_5d_net95_live_signal_validation.py）：
  T+1 開盤進場 / 持有7交易日收盤出場 / 30bps 成本 / r_adj = r_s - 1.15 * r_IX0001

輸出 reports/research/branch-footprint-screen/round10_drought_*.{csv,json}

用法：
    PYTHONPATH=src .venv/bin/python \
        scripts/research/round10_drought_songshan_signal_diagnosis.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import DEFAULT_DB_PATH

SOURCE = "finmind"
TRADER_ID = "9217"
BENCH_CODE = "IX0001"
STUDY_START = "2024-07-01"
STUDY_END = "2026-08-14"
COST, HOLD, BETA = 0.003, 7, 1.15  # L1H7 SSOT
BASE_FLOOR, BASE_NET = 0.5e8, 0.95
MEGA_PATH = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "ab58_xMega_copytrade"
    / "mega_blacklist_v1.json"
)
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
PREFIX = "round10_drought"
RNG_SEED = 20260817
N_BOOT = 20_000


def ro_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def section(title: str) -> None:
    print(f"\n{'=' * 92}\n{title}\n{'=' * 92}")


def load_mega() -> set[str]:
    return {str(s) for s in json.loads(MEGA_PATH.read_text())["symbols"]}


def load_calendar(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT trade_date FROM stock_daily_bars
        WHERE stock_id='2330' AND source=? AND trade_date BETWEEN ? AND ? AND close>0
        ORDER BY trade_date
        """,
        (SOURCE, STUDY_START, STUDY_END),
    ).fetchall()
    return [str(r[0]) for r in rows]


def load_raw(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT b.stock_id, b.trade_date,
               b.buy  * p.close AS buy_amt,
               b.sell * p.close AS sell_amt
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.source=? AND b.securities_trader_id=?
          AND b.trade_date BETWEEN ? AND ?
          AND p.close>0
          AND length(b.stock_id)=4
          AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        """,
        conn,
        params=(SOURCE, SOURCE, TRADER_ID, STUDY_START, STUDY_END),
    )


def build_panel(conn) -> pd.DataFrame:
    """(stock,day) full grid with rolling 5d buy/sell/net_ratio."""
    cal = load_calendar(conn)
    raw = load_raw(conn)
    print(f"[INFO] calendar {len(cal)} days {cal[0]}..{cal[-1]}")
    print(f"[INFO] 9217 activity rows={len(raw)} stocks={raw['stock_id'].nunique()}")
    stocks = sorted(raw["stock_id"].unique())
    grid = pd.MultiIndex.from_product(
        [stocks, cal], names=["stock_id", "trade_date"]
    ).to_frame(index=False)
    m = grid.merge(raw, on=["stock_id", "trade_date"], how="left")
    m[["buy_amt", "sell_amt"]] = m[["buy_amt", "sell_amt"]].fillna(0.0)
    m = m.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    g = m.groupby("stock_id", sort=False)
    m["buy_5d"] = g["buy_amt"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    m["sell_5d"] = g["sell_amt"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    m["net_ratio"] = np.where(
        m["buy_5d"] > 0, (m["buy_5d"] - m["sell_5d"]) / m["buy_5d"].replace(0, np.nan), np.nan
    )
    m["ym"] = m["trade_date"].str[:7]
    return m


def rising_edge(panel: pd.DataFrame, trig: pd.Series) -> pd.DataFrame:
    df = panel.assign(triggered=trig.to_numpy())
    prev = df.groupby("stock_id", sort=False)["triggered"].shift(1).fillna(False)
    ev = df[df["triggered"] & (~prev)].copy()
    return (
        ev.rename(columns={"trade_date": "signal_date"})[
            ["stock_id", "signal_date", "buy_5d", "sell_5d", "net_ratio"]
        ]
        .sort_values(["signal_date", "stock_id"])
        .reset_index(drop=True)
    )


def make_trigger(
    panel: pd.DataFrame, mega: set[str], floor: float, net_min: float, *, use_mega: bool = True
) -> pd.Series:
    t = (panel["buy_5d"] >= floor) & (panel["net_ratio"] >= net_min)
    if use_mega:
        t &= ~panel["stock_id"].isin(mega)
    return t.fillna(False)


# ---------------------------------------------------------------- L1H7 engine
class Bars:
    def __init__(self, conn):
        self.conn = conn
        self.cache: dict[str, list[tuple[str, float, float]]] = {}

    def stock(self, sid: str):
        if sid not in self.cache:
            rows = self.conn.execute(
                """
                SELECT trade_date, open, close FROM stock_daily_bars
                WHERE stock_id=? AND source=? AND trade_date BETWEEN ? AND ? AND close>0
                ORDER BY trade_date
                """,
                (sid, SOURCE, "2024-05-01", "2026-12-31"),
            ).fetchall()
            self.cache[sid] = [
                (r[0], float(r[1]) if r[1] else float(r[2]), float(r[2])) for r in rows
            ]
        return self.cache[sid]

    def ix(self):
        if "__IX__" not in self.cache:
            rows = self.conn.execute(
                """
                SELECT date, open, close FROM daily_bars
                WHERE code=? AND date BETWEEN ? AND ? AND open>0 AND close>0
                ORDER BY date,
                  CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
                              WHEN 'finmind' THEN 2 ELSE 3 END
                """,
                (BENCH_CODE, "2024-05-01", "2026-12-31"),
            ).fetchall()
            d: dict[str, tuple[float, float]] = {}
            for r in rows:
                d.setdefault(r[0], (float(r[1]), float(r[2])))
            self.cache["__IX__"] = [(k, v[0], v[1]) for k, v in sorted(d.items())]
        return self.cache["__IX__"]


def _next_open(bars, sig):
    for d, o, _c in bars:
        if d > sig and o > 0:
            return d, o
    return None, None


def _exit_close(bars, entry, hold=HOLD):
    ordered = [x for x in bars if x[0] >= entry]
    if len(ordered) < hold:
        return None, None
    return ordered[hold - 1][0], ordered[hold - 1][2]


def build_trades(bars: Bars, events: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    ix = bars.ix()
    out, drops = [], {"no_bars": 0, "no_entry": 0, "no_exit": 0, "no_bench": 0}
    for row in events.itertuples(index=False):
        b = bars.stock(row.stock_id)
        if len(b) < 10:
            drops["no_bars"] += 1
            continue
        ed, eo = _next_open(b, row.signal_date)
        if not ed:
            drops["no_entry"] += 1
            continue
        xd, xc = _exit_close(b, ed)
        if not xd:
            drops["no_exit"] += 1
            continue
        be, bo = _next_open(ix, row.signal_date)
        if not be:
            drops["no_bench"] += 1
            continue
        _, bc = _exit_close(ix, be)
        if not bc:
            drops["no_bench"] += 1
            continue
        r_s = xc / eo - 1 - COST
        r_ix = bc / bo - 1
        out.append(
            {
                "signal_date": row.signal_date,
                "stock_id": row.stock_id,
                "buy_5d": round(float(row.buy_5d), 0),
                "net_ratio": round(float(row.net_ratio), 4),
                "entry_date": ed,
                "exit_date": xd,
                "r_pct": round(r_s * 100, 3),
                "r_ix_pct": round(r_ix * 100, 3),
                "r_adj_pct": round((r_s - BETA * r_ix) * 100, 3),
            }
        )
    return pd.DataFrame(out), drops


def stat_block(vals_pct: pd.Series) -> dict:
    v = pd.Series(vals_pct).dropna().to_numpy() / 100.0
    n = len(v)
    if n == 0:
        return {"n": 0}
    d = {
        "n": n,
        "mean_pct": round(float(v.mean()) * 100, 2),
        "median_pct": round(float(np.median(v)) * 100, 2),
        "win_rate_pct": round(float((v > 0).mean()) * 100, 1),
        "sum_pct": round(float(v.sum()) * 100, 1),
    }
    if n >= 3 and v.std() > 0:
        t, p = stats.ttest_1samp(v, 0)
        d["t_stat"] = round(float(t), 2)
        d["t_p"] = round(float(p), 4)
    return d


# ---------------------------------------------------------------- Q1 monthly
def q1_monthly(panel: pd.DataFrame, mega: set[str], events_base: pd.DataFrame) -> pd.DataFrame:
    p = panel.copy()
    p["is_mega"] = p["stock_id"].isin(mega)
    valid = p["buy_5d"].notna()

    cand = p[valid & (p["buy_5d"] >= BASE_FLOOR)]
    cand_nm = cand[~cand["is_mega"]]

    rows = []
    for ym, sub in p.groupby("ym"):
        day_rows = sub[sub["buy_amt"] > 0]
        c = cand[cand["ym"] == ym]
        cnm = cand_nm[cand_nm["ym"] == ym]
        near = cnm[(cnm["net_ratio"] >= 0.85) & (cnm["net_ratio"] < BASE_NET)]
        hit = cnm[cnm["net_ratio"] >= BASE_NET]
        n_days = sub["trade_date"].nunique()
        tot_buy = float(sub["buy_amt"].sum())
        mega_buy = float(sub.loc[sub["is_mega"], "buy_amt"].sum())
        rows.append(
            {
                "ym": ym,
                "n_trading_days": n_days,
                "buy_total_yi": round(tot_buy / 1e8, 1),
                "buy_per_day_yi": round(tot_buy / max(n_days, 1) / 1e8, 2),
                "mega_buy_share_pct": round(100 * mega_buy / tot_buy, 1) if tot_buy else None,
                "stocks_touched_per_day": round(len(day_rows) / max(n_days, 1), 0),
                # funnel on (stock,day) level
                "sd_buy5d_ge_floor": len(c),
                "sd_nonmega_ge_floor": len(cnm),
                "sd_mega_ge_floor": len(c) - len(cnm),
                "sd_net_ge_095": len(hit),
                "sd_net_085_094": len(near),
                "net_p50_of_cand": round(float(cnm["net_ratio"].median()), 3) if len(cnm) else None,
                "net_p90_of_cand": (
                    round(float(cnm["net_ratio"].quantile(0.90)), 3) if len(cnm) else None
                ),
                "max_buy5d_nonmega_yi": round(
                    float(sub.loc[~sub["is_mega"], "buy_5d"].max() or 0) / 1e8, 2
                ),
                "cand_per_day": round(len(cnm) / max(n_days, 1), 1),
                "pass95_rate_pct": round(100 * len(hit) / len(cnm), 1) if len(cnm) else None,
            }
        )
    out = pd.DataFrame(rows)
    ev = events_base.copy()
    ev["ym"] = ev["signal_date"].str[:7]
    out = out.merge(
        ev.groupby("ym").size().rename("events_base").reset_index(), on="ym", how="left"
    )
    out["events_base"] = out["events_base"].fillna(0).astype(int)
    return out


def near_miss_events(panel: pd.DataFrame, mega: set[str]) -> pd.DataFrame:
    trig = (
        (panel["buy_5d"] >= BASE_FLOOR)
        & (panel["net_ratio"] >= 0.85)
        & (panel["net_ratio"] < BASE_NET)
        & (~panel["stock_id"].isin(mega))
    ).fillna(False)
    return rising_edge(panel, trig)


# ---------------------------------------------------------------- Q4 runs
def runs_analysis(trades: pd.DataFrame) -> dict:
    t = trades.sort_values(["signal_date", "stock_id"]).reset_index(drop=True)
    signs = (t["r_adj_pct"].to_numpy() > 0).astype(int)
    n = len(signs)
    n_win = int(signs.sum())
    p_win = n_win / n

    def max_neg_run(s):
        best = cur = 0
        for x in s:
            cur = 0 if x == 1 else cur + 1
            best = max(best, cur)
        return best

    def count_windows_all_neg(s, k=3):
        return int(sum(1 for i in range(len(s) - k + 1) if s[i : i + k].sum() == 0))

    obs_max_run = max_neg_run(signs)
    obs_w3 = count_windows_all_neg(signs, 3)
    n_windows = max(n - 2, 0)

    rng = np.random.default_rng(RNG_SEED)
    boot_max, boot_w3, boot_tail3 = [], [], []
    for _ in range(N_BOOT):
        s = rng.permutation(signs)
        boot_max.append(max_neg_run(s))
        boot_w3.append(count_windows_all_neg(s, 3))
        boot_tail3.append(int(s[-3:].sum() == 0))
    boot_max = np.array(boot_max)
    boot_w3 = np.array(boot_w3)

    # Wald-Wolfowitz runs test for independence
    n1, n0 = n_win, n - n_win
    runs = 1 + int((signs[1:] != signs[:-1]).sum())
    mu = 2 * n1 * n0 / n + 1
    var = (2 * n1 * n0 * (2 * n1 * n0 - n)) / (n**2 * (n - 1))
    z = (runs - mu) / np.sqrt(var) if var > 0 else float("nan")

    return {
        "n_trades": n,
        "win_rate_pct": round(100 * p_win, 1),
        "observed_max_consecutive_negative": obs_max_run,
        "observed_3in_a_row_windows": obs_w3,
        "total_3_windows": n_windows,
        "freq_3in_a_row_pct": round(100 * obs_w3 / n_windows, 1) if n_windows else None,
        "iid_prob_3_consecutive_neg_pct": round(100 * (1 - p_win) ** 3, 1),
        "shuffle_p_at_least_one_run_ge3_pct": round(100 * float((boot_max >= 3).mean()), 1),
        "shuffle_expected_3windows": round(float(boot_w3.mean()), 2),
        "shuffle_p_windows_ge_observed_pct": round(100 * float((boot_w3 >= obs_w3).mean()), 1),
        "shuffle_p_last3_all_neg_pct": round(100 * float(np.mean(boot_tail3)), 1),
        "runs_test": {"runs": runs, "expected": round(float(mu), 2), "z": round(float(z), 2),
                      "p_two_sided": round(float(2 * (1 - stats.norm.cdf(abs(z)))), 4)},
        "last3": t.tail(3)[["signal_date", "stock_id", "r_adj_pct"]].to_dict("records"),
    }


# ---------------------------------------------------------------- Q5 2492
def q5_2492(conn) -> pd.DataFrame:
    df = pd.read_sql_query(
        """
        SELECT b.trade_date, b.buy, b.sell, p.close,
               b.buy*p.close AS buy_amt, b.sell*p.close AS sell_amt
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.source=? AND b.securities_trader_id=? AND b.stock_id='2492'
          AND b.trade_date BETWEEN '2026-06-25' AND ?
        ORDER BY b.trade_date
        """,
        conn,
        params=(SOURCE, SOURCE, TRADER_ID, STUDY_END),
    )
    return df


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = ro_connect()
    mega = load_mega()
    panel = build_panel(conn)
    bars = Bars(conn)
    summary: dict = {
        "protocol": {
            "signal": "rolling 5d buy_5d>=0.5e8 & net_ratio>=0.95 & !mega (rising-edge dedup)",
            "window": f"{STUDY_START}..{STUDY_END}",
            "l1h7": {"cost": COST, "hold": HOLD, "beta": BETA, "bench": BENCH_CODE},
            "mega_n": len(mega),
            "note": "DIAGNOSTIC ONLY · grids below are in-sample tuning · not adoption evidence",
        }
    }

    # ---- base events
    ev_base = rising_edge(panel, make_trigger(panel, mega, BASE_FLOOR, BASE_NET))
    tr_base, drops = build_trades(bars, ev_base)
    print(f"[INFO] base events={len(ev_base)} trades={len(tr_base)} drops={drops}")
    ev_base.to_csv(OUT_DIR / f"{PREFIX}_base_events.csv", index=False)
    tr_base.to_csv(OUT_DIR / f"{PREFIX}_base_trades.csv", index=False)

    # ---- Q1
    section("Q1 · 每月觸發／近門檻／活躍度分解")
    monthly = q1_monthly(panel, mega, ev_base)
    ev_near = near_miss_events(panel, mega)
    ev_near["ym"] = ev_near["signal_date"].str[:7]
    monthly = monthly.merge(
        ev_near.groupby("ym").size().rename("near_events_085_094").reset_index(),
        on="ym", how="left",
    )
    monthly["near_events_085_094"] = monthly["near_events_085_094"].fillna(0).astype(int)
    # counterfactual monthly counts
    for label, (fl, nm, um) in {
        "ev_nomega": (BASE_FLOOR, BASE_NET, False),
        "ev_net090": (BASE_FLOOR, 0.90, True),
        "ev_net085": (BASE_FLOOR, 0.85, True),
        "ev_floor03": (0.3e8, BASE_NET, True),
    }.items():
        e = rising_edge(panel, make_trigger(panel, mega, fl, nm, use_mega=um))
        e["ym"] = e["signal_date"].str[:7]
        monthly = monthly.merge(
            e.groupby("ym").size().rename(label).reset_index(), on="ym", how="left"
        )
        monthly[label] = monthly[label].fillna(0).astype(int)
    monthly.to_csv(OUT_DIR / f"{PREFIX}_monthly.csv", index=False)
    print(monthly.to_string(index=False))

    # ---- Q2 grid
    section("Q2 · 3x3 門檻網格（IN-SAMPLE 調參 · 診斷用 · 不可當採納依據）")
    grid_rows = []
    for nm in (0.85, 0.90, 0.95):
        for fl in (0.3e8, 0.5e8, 1.0e8):
            ev = rising_edge(panel, make_trigger(panel, mega, fl, nm))
            tr, _ = build_trades(bars, ev)
            s = stat_block(tr["r_adj_pct"]) if len(tr) else {"n": 0}
            last = ev["signal_date"].max() if len(ev) else None
            grid_rows.append(
                {
                    "net_min": nm,
                    "buy_floor_yi": round(fl / 1e8, 1),
                    "n_events": len(ev),
                    "n_trades": s.get("n", 0),
                    "mean_r_adj_pct": s.get("mean_pct"),
                    "median_r_adj_pct": s.get("median_pct"),
                    "win_rate_pct": s.get("win_rate_pct"),
                    "sum_r_adj_pct": s.get("sum_pct"),
                    "t_p": s.get("t_p"),
                    "last_event": last,
                }
            )
    grid = pd.DataFrame(grid_rows)
    grid.to_csv(OUT_DIR / f"{PREFIX}_threshold_grid.csv", index=False)
    print(grid.to_string(index=False))
    summary["q2_grid_IN_SAMPLE_TUNING"] = grid_rows

    # ---- Q3 mega
    section("Q3 · mega 黑名單影響")
    ev_nm = rising_edge(panel, make_trigger(panel, mega, BASE_FLOOR, BASE_NET, use_mega=False))
    tr_nm, _ = build_trades(bars, ev_nm)
    blocked = ev_nm[ev_nm["stock_id"].isin(mega)].copy()
    tr_blocked, _ = build_trades(bars, blocked)
    q3 = {
        "with_mega_filter": {"n_events": len(ev_base), **stat_block(tr_base["r_adj_pct"])},
        "without_mega_filter": {"n_events": len(ev_nm), **stat_block(tr_nm["r_adj_pct"])},
        "blocked_by_mega_only": {"n_events": len(blocked), **stat_block(tr_blocked["r_adj_pct"])},
        "blocked_by_symbol": blocked["stock_id"].value_counts().to_dict(),
        "blocked_by_month": blocked["signal_date"].str[:7].value_counts().sort_index().to_dict(),
    }
    print(json.dumps(q3, ensure_ascii=False, indent=2))
    summary["q3_mega"] = q3
    blocked.to_csv(OUT_DIR / f"{PREFIX}_mega_blocked_events.csv", index=False)

    # ---- Q1b gap distribution + regime split
    section("Q1b · 事件間隔（交易日）分布 vs 當前乾涸長度")
    cal = load_calendar(conn)
    idx = {d: i for i, d in enumerate(cal)}
    sig_days = sorted(ev_base["signal_date"].unique())
    gaps = [idx[sig_days[i]] - idx[sig_days[i - 1]] for i in range(1, len(sig_days))]
    cur_gap = idx[cal[-1]] - idx[sig_days[-1]]
    era_a = [g for i, g in enumerate(gaps) if sig_days[i + 1] <= "2025-08-31"]
    era_b = [g for i, g in enumerate(gaps) if sig_days[i + 1] >= "2025-09-01"]
    q1b = {
        "n_signal_days": len(sig_days),
        "last_signal_date": sig_days[-1],
        "current_gap_trading_days": cur_gap,
        "gap_all": {
            "n": len(gaps), "median": float(np.median(gaps)), "p75": float(np.percentile(gaps, 75)),
            "p90": float(np.percentile(gaps, 90)), "max": int(max(gaps)),
            "pct_gaps_ge_current": round(100 * float(np.mean([g >= cur_gap for g in gaps])), 1),
        },
        "gap_eraA_2024_07_to_2025_08": {
            "n": len(era_a), "median": float(np.median(era_a)) if era_a else None,
            "max": int(max(era_a)) if era_a else None,
            "pct_ge_current": round(100 * float(np.mean([g >= cur_gap for g in era_a])), 1) if era_a else None,
        },
        "gap_eraB_2025_09_onward": {
            "n": len(era_b), "median": float(np.median(era_b)) if era_b else None,
            "max": int(max(era_b)) if era_b else None,
            "pct_ge_current": round(100 * float(np.mean([g >= cur_gap for g in era_b])), 1) if era_b else None,
        },
    }
    for lab, lo, hi in (("eraA", "2024-07-01", "2025-08-31"), ("eraB", "2025-09-01", STUDY_END)):
        n_days = sum(1 for d in cal if lo <= d <= hi)
        sub = tr_base[(tr_base["signal_date"] >= lo) & (tr_base["signal_date"] <= hi)]
        rate = len(sub) / n_days if n_days else 0.0
        q1b[f"{lab}_rate"] = {
            "trading_days": n_days, "n_events": len(sub),
            "events_per_trading_day": round(rate, 4),
            "poisson_p_zero_in_current_gap_pct": round(100 * float(np.exp(-rate * cur_gap)), 1),
            **stat_block(sub["r_adj_pct"]),
        }
    print(json.dumps(q1b, ensure_ascii=False, indent=2))
    summary["q1b_gaps"] = q1b

    # ---- Q4 runs
    section("Q4 · 連續三筆負是雜訊還是轉折")
    q4 = runs_analysis(tr_base)
    print(json.dumps(q4, ensure_ascii=False, indent=2))
    summary["q4_runs"] = q4

    # ---- Q5 2492
    section("Q5 · 2492 現況")
    d2492 = q5_2492(conn)
    p2492 = panel[(panel["stock_id"] == "2492") & (panel["trade_date"] >= "2026-06-25")]
    d2492 = d2492.merge(
        p2492[["trade_date", "buy_5d", "sell_5d", "net_ratio"]], on="trade_date", how="left"
    )
    d2492["net_amt_yi"] = (d2492["buy_amt"] - d2492["sell_amt"]) / 1e8
    d2492["buy_yi"] = d2492["buy_amt"] / 1e8
    d2492["sell_yi"] = d2492["sell_amt"] / 1e8
    d2492["buy5d_yi"] = d2492["buy_5d"] / 1e8
    d2492["sell5d_yi"] = d2492["sell_5d"] / 1e8
    # gap: how much extra sell reduction needed for net>=0.95
    d2492["max_sell5d_allowed_yi"] = (1 - BASE_NET) * d2492["buy5d_yi"]
    d2492["excess_sell5d_yi"] = d2492["sell5d_yi"] - d2492["max_sell5d_allowed_yi"]
    cols = ["trade_date", "close", "buy_yi", "sell_yi", "net_amt_yi", "buy5d_yi",
            "sell5d_yi", "net_ratio", "excess_sell5d_yi"]
    show = d2492[d2492["trade_date"] >= "2026-07-01"][cols].round(3)
    show.to_csv(OUT_DIR / f"{PREFIX}_2492_daily.csv", index=False)
    print(show.to_string(index=False))
    summary["q5_2492_tail"] = show.tail(12).to_dict("records")

    # ---- Q5b: 2492 forward window decay projection (zero new activity)
    section("Q5b · 2492 五日窗前推（假設後續零活動）")
    hist = {
        r["trade_date"]: (float(r["buy_amt"]), float(r["sell_amt"]))
        for _, r in d2492[d2492["trade_date"] >= "2026-08-10"].iterrows()
    }
    seq = sorted(hist) + ["+1", "+2", "+3", "+4"]
    proj = []
    for i, day in enumerate(seq):
        if not str(day).startswith("+"):
            continue
        win = seq[i - 4 : i + 1]
        b = sum(hist.get(x, (0.0, 0.0))[0] for x in win)
        s = sum(hist.get(x, (0.0, 0.0))[1] for x in win)
        proj.append(
            {
                "session": day,
                "window": f"{win[0]}..{win[-1]}",
                "buy5d_yi": round(b / 1e8, 3),
                "sell5d_yi": round(s / 1e8, 3),
                "net_ratio": round((b - s) / b, 4) if b > 0 else None,
                "floor_ok": bool(b >= BASE_FLOOR),
                "extra_buy_needed_for_net95_yi": round((s / (1 - BASE_NET) - b) / 1e8, 2),
            }
        )
    print(json.dumps(proj, ensure_ascii=False, indent=2))
    summary["q5b_2492_projection"] = proj

    # ---- drought-window state (trigger ON but deduped?)
    section("附錄 · 乾涸窗內每日候選最高 net_ratio")
    w = panel[
        (panel["trade_date"] > "2026-07-22")
        & (panel["buy_5d"] >= BASE_FLOOR)
        & (~panel["stock_id"].isin(mega))
    ].copy()
    top = (
        w.sort_values(["trade_date", "net_ratio"], ascending=[True, False])
        .groupby("trade_date")
        .head(1)[["trade_date", "stock_id", "buy_5d", "net_ratio"]]
    )
    top["buy5d_yi"] = (top["buy_5d"] / 1e8).round(2)
    top["net_ratio"] = top["net_ratio"].round(3)
    top = top[["trade_date", "stock_id", "buy5d_yi", "net_ratio"]]
    top.to_csv(OUT_DIR / f"{PREFIX}_drought_window_top.csv", index=False)
    print(top.to_string(index=False))
    summary["drought_window_daily_top"] = top.to_dict("records")

    # ---- extra: current near-threshold leaderboard on the last day
    section("附錄 · 最新交易日 buy_5d 榜（含 mega 標記）")
    last_day = panel["trade_date"].max()
    lb = panel[(panel["trade_date"] == last_day) & (panel["buy_5d"] >= 0.3e8)].copy()
    lb["is_mega"] = lb["stock_id"].isin(mega)
    lb["buy5d_yi"] = (lb["buy_5d"] / 1e8).round(2)
    lb["sell5d_yi"] = (lb["sell_5d"] / 1e8).round(2)
    lb["net_ratio"] = lb["net_ratio"].round(3)
    lb = lb.sort_values("buy_5d", ascending=False)[
        ["stock_id", "buy5d_yi", "sell5d_yi", "net_ratio", "is_mega"]
    ]
    lb.to_csv(OUT_DIR / f"{PREFIX}_last_day_leaderboard.csv", index=False)
    print(f"asof {last_day}")
    print(lb.head(25).to_string(index=False))
    summary["asof"] = last_day
    summary["q1_monthly"] = monthly.to_dict("records")
    summary["base"] = {"n_events": len(ev_base), **stat_block(tr_base["r_adj_pct"]),
                       "last_event": ev_base["signal_date"].max(), "drops": drops}

    (OUT_DIR / f"{PREFIX}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    )
    print(f"\n[OK] wrote {OUT_DIR}/{PREFIX}_*.csv|json")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
