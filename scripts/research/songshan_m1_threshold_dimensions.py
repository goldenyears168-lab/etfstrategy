#!/usr/bin/env python3
"""songshan_m1：9217（凱基-松山）跟單訊號的**門檻維度**檢定（H-A1 buy_floor / H-A2 net_ratio 結構）.

純研究 · DB 唯讀 · 不改 config/.env/launchd · 不 commit。

訊號 SSOT = scripts/research/run_songshan_follow_watch.py::scan_5d_net95
  滾動 5 交易日  buy_5d >= floor  ∩  net_ratio=(buy_5d-sell_5d)/buy_5d >= net_min  ∩ !mega
協議 SSOT = L1H7（T+1 開盤進 / 第 7 個交易日收盤出 / COST=30bps / BETA=1.15 / bench=IX0001）
去重     = rising-edge（per stock，not-triggered → triggered 的第一天）
           + campaign merge（同股票內交易日距離 <= gap 合併為一波，只留第一筆）

═══════════════════════════════════════════════════════════════════════════
⚠️  預先宣告（PRE-DECLARED）—— 在看到任何結果之前寫死，跑完不得回頭修改
═══════════════════════════════════════════════════════════════════════════
WF-A  單一 IS/OOS 切點：交易日曆前 60% = IS，後 40% = OOS
      （沿用第六~八輪既有慣例，不是本輪挑的）
WF-B  擴張窗 walk-forward：把交易日曆切成 5 等份（等交易日數）B1..B5
      fold k = 1..4：train = B1..Bk，test = B(k+1)
      選擇規則：在 train 上取 mean(r_adj) 最大的 floor，且要求 train n_trades >= 10；
               若沒有任何 floor 滿足 n>=10，該 fold 回退到 baseline floor 0.5 億。
      WF 產出 = 4 個 test 區段的成交串接。對照組 = 同樣 4 個 test 區段固定 floor 0.5 億。
FLOORS  = (0.5, 0.75, 1.0, 1.5, 2.0) 億
NET_STRATA = [0.95,0.98) / [0.98,1.00) / ==1.000（sell_5d==0）
CONC    = buy_day_max / buy_5d（5 日窗內單日最大買進佔比），高低組以**中位數**切
PERM    = 20000 × 3 seeds（20260728 / 20260817 / 424242），mean 與 median 兩統計量
MIN_N   = 15（任何切片 n<15 一律標註「樣本不足、不下結論」，不進結論）
═══════════════════════════════════════════════════════════════════════════

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m1_threshold_dimensions.py
輸出：
  reports/research/branch-footprint-screen/songshan_m1_*.csv|json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats  # noqa: E402

from research.branch_signal_validation import (  # noqa: E402
    build_l1h7_signal_dict,
    permutation_test,
)
from stock_db import DEFAULT_DB_PATH  # noqa: E402

# ---- 協議常數（與 round10 完全一致）----------------------------------------
TRADER_ID = "9217"
SOURCE = "finmind"
STUDY_START = "2024-07-01"
STUDY_END = "2026-08-14"  # 與第十輪同窗，才能跟 n=48 做逐筆比對
COST, HOLD, BETA = 0.003, 7, 1.15
BENCH_CODE = "IX0001"
BAR_PAD_START, BAR_PAD_END = "2024-05-01", "2026-08-31"

# ---- 預先宣告的實驗參數 ------------------------------------------------------
FLOORS_YI = (0.5, 0.75, 1.0, 1.5, 2.0)
BASE_FLOOR_YI = 0.5
NET_MIN = 0.95
CAMPAIGN_GAP = 10
N_PERM = 20_000
PERM_SEEDS = (20260728, 20260817, 424242)
WF_A_IS_FRAC = 0.60
WF_B_BLOCKS = 5
WF_B_MIN_TRAIN_N = 10
MIN_N_CONCLUDE = 15

MEGA_PATH = (
    ROOT / "reports" / "research" / "branch-footprint-screen"
    / "ab58_xMega_copytrade" / "mega_blacklist_v1.json"
)
ROUND10_EVENTS = (
    ROOT / "reports" / "research" / "branch-footprint-screen"
    / "whale_9217_round10_events.csv"
)
OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
PREFIX = "songshan_m1_"


def section(title: str) -> None:
    print(f"\n{'=' * 96}\n{title}\n{'=' * 96}")


# ---------------------------------------------------------------------------
# 資料載入（唯讀）
# ---------------------------------------------------------------------------

def connect_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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


def load_raw_activity(conn) -> pd.DataFrame:
    return pd.read_sql_query(
        """
        SELECT b.stock_id, b.trade_date,
               b.buy * p.close  AS buy_amt,
               b.sell * p.close AS sell_amt
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id = b.stock_id AND p.trade_date = b.trade_date AND p.source = ?
        WHERE b.source = ?
          AND b.securities_trader_id = ?
          AND b.trade_date BETWEEN ? AND ?
          AND p.close > 0
          AND length(b.stock_id) = 4
          AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        """,
        conn,
        params=(SOURCE, SOURCE, TRADER_ID, STUDY_START, STUDY_END),
    )


def load_stock_bars(conn, sid: str) -> list[tuple[str, float, float]]:
    rows = conn.execute(
        """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date BETWEEN ? AND ? AND close>0
        ORDER BY trade_date
        """,
        (sid, SOURCE, BAR_PAD_START, BAR_PAD_END),
    ).fetchall()
    return [(str(r[0]), float(r[1]) if r[1] else float(r[2]), float(r[2])) for r in rows]


def load_ix(conn) -> list[tuple[str, float, float]]:
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code=? AND date BETWEEN ? AND ? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (BENCH_CODE, BAR_PAD_START, BAR_PAD_END),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for r in rows:
        out.setdefault(str(r[0]), (float(r[1]), float(r[2])))
    return [(d, o, c) for d, (o, c) in sorted(out.items())]


# ---------------------------------------------------------------------------
# 滾動 5 日 panel（一次算好，各 floor 從這裡切）
# ---------------------------------------------------------------------------

def build_panel(raw: pd.DataFrame, calendar: list[str]) -> pd.DataFrame:
    stocks = sorted(raw["stock_id"].unique())
    grid = pd.MultiIndex.from_product(
        [stocks, calendar], names=["stock_id", "trade_date"]
    ).to_frame(index=False)
    m = grid.merge(raw, on=["stock_id", "trade_date"], how="left")
    m["buy_amt"] = m["buy_amt"].fillna(0.0)
    m["sell_amt"] = m["sell_amt"].fillna(0.0)
    m = m.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)
    g = m.groupby("stock_id", sort=False)
    m["buy_5d"] = g["buy_amt"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    m["sell_5d"] = g["sell_amt"].transform(lambda s: s.rolling(5, min_periods=5).sum())
    m["buy_day_max"] = g["buy_amt"].transform(lambda s: s.rolling(5, min_periods=5).max())
    m["net_ratio"] = np.where(
        m["buy_5d"] > 0, (m["buy_5d"] - m["sell_5d"]) / m["buy_5d"].replace(0, np.nan), np.nan
    )
    return m


def events_for(panel: pd.DataFrame, mega: set[str], floor: float, net_min: float) -> pd.DataFrame:
    trig = (
        (panel["buy_5d"] >= floor)
        & (panel["net_ratio"] >= net_min)
        & (~panel["stock_id"].isin(mega))
    )
    p = panel.assign(triggered=trig)
    prev = p.groupby("stock_id", sort=False)["triggered"].shift(1).fillna(False)
    ev = p[p["triggered"] & (~prev)].copy()
    ev = ev.rename(columns={"trade_date": "signal_date"})
    ev["conc"] = np.where(ev["buy_5d"] > 0, ev["buy_day_max"] / ev["buy_5d"], np.nan)
    cols = ["stock_id", "signal_date", "buy_5d", "sell_5d", "net_ratio", "buy_day_max", "conc"]
    return ev[cols].sort_values(["signal_date", "stock_id"]).reset_index(drop=True)


def merge_campaigns(dates: list[str], idx: dict[str, int], gap: int) -> list[str]:
    ds = sorted(d for d in dates if d in idx)
    if not ds:
        return []
    out, cur, last = [], ds[0], idx[ds[0]]
    for d in ds[1:]:
        di = idx[d]
        if di - last <= gap:
            last = di
            continue
        out.append(cur)
        cur, last = d, di
    out.append(cur)
    return out


def campaign_dedup(events: pd.DataFrame, calendar: list[str], gap: int) -> pd.DataFrame:
    if events.empty:
        return events
    idx = {d: i for i, d in enumerate(calendar)}
    keep = set()
    for sid, sub in events.groupby("stock_id"):
        for d in merge_campaigns(sub["signal_date"].tolist(), idx, gap):
            keep.add((sid, d))
    mask = [(r.stock_id, r.signal_date) in keep for r in events.itertuples(index=False)]
    return events[pd.Series(mask, index=events.index)].reset_index(drop=True)


# ---------------------------------------------------------------------------
# L1H7 trades
# ---------------------------------------------------------------------------

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


def build_trades(events: pd.DataFrame, bars_cache: dict, ix: list) -> tuple[pd.DataFrame, dict]:
    rows, drop = [], {"no_bars": 0, "no_entry": 0, "no_exit": 0, "no_bench": 0}
    for r in events.itertuples(index=False):
        bars = bars_cache.get(r.stock_id) or []
        if len(bars) < 10:
            drop["no_bars"] += 1
            continue
        ed, eo = _next_open(bars, r.signal_date)
        if not ed or not eo:
            drop["no_entry"] += 1
            continue
        xd, xc = _exit_close(bars, ed)
        if not xd or not xc:
            drop["no_exit"] += 1
            continue
        be, bo = _next_open(ix, r.signal_date)
        if not be or not bo:
            drop["no_bench"] += 1
            continue
        _, bc = _exit_close(ix, be)
        if not bc:
            drop["no_bench"] += 1
            continue
        r_s = xc / eo - 1 - COST
        r_ix = bc / bo - 1
        rows.append({
            "signal_date": r.signal_date, "stock_id": r.stock_id,
            "buy_5d": float(r.buy_5d), "net_ratio": float(r.net_ratio),
            "conc": float(r.conc) if r.conc == r.conc else None,
            "entry_date": ed, "entry_open": round(eo, 4),
            "exit_date": xd, "exit_close": round(xc, 4),
            "r_pct": round(r_s * 100, 3), "r_ix_pct": round(r_ix * 100, 3),
            "r_adj_pct": round((r_s - BETA * r_ix) * 100, 3),
        })
    drop["n_events"] = len(events)
    drop["n_trades"] = len(rows)
    return pd.DataFrame(rows), drop


def desc(tr: pd.DataFrame) -> dict:
    if tr.empty:
        return {"n": 0, "mean": None, "median": None, "win_rate": None, "t_p": None, "wil_p": None,
                "insufficient_sample": True, "trim3_mean": None, "trim5_mean": None,
                "n_6449": 0, "sum_6449": 0.0}
    v = tr["r_adj_pct"].to_numpy(float)
    d = {
        "n": int(len(v)),
        "mean": round(float(np.mean(v)), 3),
        "median": round(float(np.median(v)), 3),
        "win_rate": round(float(np.mean(v > 0) * 100), 1),
        "sum": round(float(np.sum(v)), 1),
    }
    # 去極值敏感度（|r_adj| 最大的 3/5 筆）
    order = np.argsort(-np.abs(v))
    for k in (3, 5):
        keep = np.delete(v, order[:k]) if len(v) > k else np.array([])
        d[f"trim{k}_n"] = int(len(keep))
        d[f"trim{k}_mean"] = round(float(np.mean(keep)), 3) if len(keep) else None
        d[f"trim{k}_median"] = round(float(np.median(keep)), 3) if len(keep) else None
    # 6449 曝險（2026-08-17 補檔後才現形的災難股）
    if "stock_id" in tr.columns:
        s6449 = tr[tr["stock_id"] == "6449"]
        d["n_6449"] = int(len(s6449))
        d["sum_6449"] = round(float(s6449["r_adj_pct"].sum()), 2) if len(s6449) else 0.0
        d["dates_6449"] = s6449["signal_date"].tolist()
    if len(v) >= 3:
        d["t_p"] = round(float(stats.ttest_1samp(v, 0).pvalue), 4)
        try:
            d["wil_p"] = round(float(stats.wilcoxon(v).pvalue), 4)
        except ValueError:
            d["wil_p"] = None
    else:
        d["t_p"] = d["wil_p"] = None
    d["insufficient_sample"] = bool(d["n"] < MIN_N_CONCLUDE)
    return d


def multi_seed_perm(tr: pd.DataFrame, stock_dicts: dict, ix_dict: dict) -> dict:
    if tr.empty or len(tr) < 3:
        return {"note": "too few trades", "p_mean_worst": None, "p_median_worst": None}
    per = []
    for seed in PERM_SEEDS:
        res = permutation_test(
            tr[["stock_id", "signal_date"]], stock_dicts, ix_dict, n_perm=N_PERM, seed=seed
        )
        res["seed"] = seed
        per.append(res)
    return {
        "n_perm": N_PERM,
        "seeds": list(PERM_SEEDS),
        "obs_mean": round(per[0]["observed_mean_pct"], 3),
        "obs_median": round(per[0]["observed_median_pct"], 3),
        "placebo_mean": round(float(np.mean([r["placebo_mean_of_means_pct"] for r in per])), 3),
        "placebo_median": round(float(np.mean([r["placebo_mean_of_medians_pct"] for r in per])), 3),
        "edge_vs_placebo_mean": round(
            per[0]["observed_mean_pct"]
            - float(np.mean([r["placebo_mean_of_means_pct"] for r in per])), 3),
        "edge_vs_placebo_median": round(
            per[0]["observed_median_pct"]
            - float(np.mean([r["placebo_mean_of_medians_pct"] for r in per])), 3),
        "p_mean_seeds": [round(r["p_value_mean_onesided"], 5) for r in per],
        "p_median_seeds": [round(r["p_value_median_onesided"], 5) for r in per],
        "p_mean_worst": round(max(r["p_value_mean_onesided"] for r in per), 5),
        "p_median_worst": round(max(r["p_value_median_onesided"] for r in per), 5),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect_ro()
    out: dict = {
        "meta": {
            "study_start": STUDY_START, "study_end": STUDY_END,
            "protocol": "L1H7 cost=0.003 hold=7 beta=1.15 bench=IX0001",
            "pre_declared": {
                "floors_yi": list(FLOORS_YI), "net_min": NET_MIN,
                "campaign_gap": CAMPAIGN_GAP, "wf_a_is_frac": WF_A_IS_FRAC,
                "wf_b_blocks": WF_B_BLOCKS, "wf_b_min_train_n": WF_B_MIN_TRAIN_N,
                "n_perm": N_PERM, "seeds": list(PERM_SEEDS),
                "min_n_conclude": MIN_N_CONCLUDE,
            },
        }
    }

    mega = load_mega()
    calendar = load_calendar(conn)
    raw = load_raw_activity(conn)
    section("0) 資料狀態")
    print(f"calendar {len(calendar)} 天 {calendar[0]} ~ {calendar[-1]}")
    print(f"9217 有價活動 (stock,day) = {len(raw)}，涵蓋 {raw['stock_id'].nunique()} 檔")
    panel = build_panel(raw, calendar)
    ix = load_ix(conn)
    ix_dict = build_l1h7_signal_dict(ix)

    # ---------- 基準母體重算 ----------
    section("1) 基準母體重算（floor 0.5 億 / net 0.95 / rising-edge）vs 第十輪 n=48")
    base_ev = events_for(panel, mega, BASE_FLOOR_YI * 1e8, NET_MIN)
    all_sids = sorted(set(base_ev["stock_id"]))
    # 各 floor 的股票集合是 base 的子集（floor 提高只會減少），先一次把 bars 撈好
    bars_cache = {sid: load_stock_bars(conn, sid) for sid in all_sids}
    base_tr, base_drop = build_trades(base_ev, bars_cache, ix)
    print(f"重算 events n = {len(base_ev)}；trades n = {len(base_tr)}；drop = {base_drop}")

    old = pd.read_csv(ROUND10_EVENTS, dtype={"stock_id": str})
    old_keys = set(zip(old["stock_id"], old["signal_date"]))
    new_keys = set(zip(base_ev["stock_id"], base_ev["signal_date"]))
    added = sorted(new_keys - old_keys)
    removed = sorted(old_keys - new_keys)
    print(f"round10 events n = {len(old)}；新增 {len(added)}；消失 {len(removed)}")
    for k in added:
        print(f"  + {k[1]} {k[0]}")
    for k in removed:
        print(f"  - {k[1]} {k[0]}")

    added_tr = base_tr[
        base_tr.apply(lambda r: (r["stock_id"], r["signal_date"]) in set(added), axis=1)
    ] if added else base_tr.iloc[0:0]
    out["baseline_recompute"] = {
        "n_events_now": len(base_ev), "n_trades_now": len(base_tr),
        "n_events_round10": len(old),
        "added": [{"stock_id": a, "signal_date": b} for a, b in added],
        "removed": [{"stock_id": a, "signal_date": b} for a, b in removed],
        "added_trades_r_adj_pct": added_tr["r_adj_pct"].tolist() if len(added_tr) else [],
        "stats_now": desc(base_tr),
        "drop_stats": base_drop,
    }
    print("重算後 L1H7：", desc(base_tr))
    base_ev.to_csv(OUT_DIR / f"{PREFIX}baseline_events.csv", index=False)
    base_tr.to_csv(OUT_DIR / f"{PREFIX}baseline_trades.csv", index=False)

    # permutation stock dicts
    stock_dicts = {sid: build_l1h7_signal_dict(bars_cache[sid]) for sid in all_sids}

    # ---------- H-A1: floor sweep ----------
    section("2) H-A1  buy_floor sweep")
    n_years = (len(calendar)) / 244.0

    # 兩種去重都跑：gap=1（rising-edge，與 n=48/53 基準同慣例，PRIMARY）
    #               gap=10（campaign 合併，SECONDARY robustness）
    floors_by_gap: dict[int, dict[float, pd.DataFrame]] = {}
    for gap in (1, CAMPAIGN_GAP):
        d: dict[float, pd.DataFrame] = {}
        for f in FLOORS_YI:
            ev = events_for(panel, mega, f * 1e8, NET_MIN)
            if gap > 1:
                ev = campaign_dedup(ev, calendar, gap)
            tr, _ = build_trades(ev, bars_cache, ix)
            d[f] = tr
        floors_by_gap[gap] = d
    floor_trades = floors_by_gap[1]  # PRIMARY

    print("\n  -- gap=10（campaign 合併）對照 --")
    g10_rows = []
    for f in FLOORS_YI:
        st10 = desc(floors_by_gap[CAMPAIGN_GAP][f])
        g10_rows.append({"floor_yi": f, **{k: st10.get(k) for k in
                        ("n", "mean", "median", "win_rate", "t_p", "trim3_mean", "n_6449")}})
        print(f"    floor {f}億: n={st10['n']} mean={st10['mean']} med={st10['median']} "
              f"win={st10['win_rate']} trim3_mean={st10['trim3_mean']} n_6449={st10['n_6449']}")
    pd.DataFrame(g10_rows).to_csv(OUT_DIR / f"{PREFIX}ha1_floor_sweep_gap10.csv", index=False)
    out["ha1_floor_sweep_gap10"] = g10_rows

    print("\n  -- gap=1（rising-edge · PRIMARY）--")
    floor_res: dict[str, dict] = {}
    rows_tbl = []
    for f in FLOORS_YI:
        ev = events_for(panel, mega, f * 1e8, NET_MIN)
        tr = floor_trades[f]
        dr = {"n_events": len(ev), "n_trades": len(tr)}
        st = desc(tr)
        # 組成反向檢查
        comp = {}
        if not tr.empty:
            yr = tr["signal_date"].str[:4]
            comp["by_year"] = yr.value_counts().sort_index().to_dict()
            comp["frac_2026"] = round(float((yr == "2026").mean()), 3)
            half = tr["signal_date"].str[:4] + "H" + np.where(
                tr["signal_date"].str[5:7].astype(int) <= 6, "1", "2")
            comp["by_half"] = pd.Series(half).value_counts().sort_index().to_dict()
            vc = tr["stock_id"].value_counts()
            comp["n_stocks"] = int(len(vc))
            comp["top_stock"] = f"{vc.index[0]}×{int(vc.iloc[0])}"
            comp["hhi_stock"] = round(float(((vc / vc.sum()) ** 2).sum()), 3)
            comp["events_per_year"] = round(len(tr) / n_years, 1)
            comp["first"], comp["last"] = tr["signal_date"].min(), tr["signal_date"].max()
        perm = multi_seed_perm(tr, stock_dicts, ix_dict)
        floor_res[str(f)] = {"stats": st, "composition": comp, "perm": perm, "drop": dr,
                             "n_events_pre_campaign": len(ev)}
        rows_tbl.append({
            "floor_yi": f, "n_events_rising_edge": len(ev), "n_trades": st["n"],
            "mean": st["mean"], "median": st["median"], "win_rate": st["win_rate"],
            "trim3_mean": st["trim3_mean"], "trim5_mean": st["trim5_mean"],
            "t_p": st["t_p"], "wil_p": st["wil_p"],
            "per_year": comp.get("events_per_year"), "frac_2026": comp.get("frac_2026"),
            "n_stocks": comp.get("n_stocks"), "hhi": comp.get("hhi_stock"),
            "n_6449": st["n_6449"], "sum_6449": st["sum_6449"],
            "p_mean_worst": perm.get("p_mean_worst"), "p_median_worst": perm.get("p_median_worst"),
            "edge_vs_placebo_mean": perm.get("edge_vs_placebo_mean"),
        })
        print(f"  floor {f}億: n_ev={len(ev)} n_tr={st['n']} mean={st['mean']} "
              f"med={st['median']} win={st['win_rate']} trim3={st['trim3_mean']} "
              f"trim5={st['trim5_mean']} t_p={st['t_p']} "
              f"p_perm_mean={perm.get('p_mean_worst')} p_perm_med={perm.get('p_median_worst')} "
              f"/yr={comp.get('events_per_year')} 2026占比={comp.get('frac_2026')} "
              f"6449留{st['n_6449']}筆({st['sum_6449']})")
    pd.DataFrame(rows_tbl).to_csv(OUT_DIR / f"{PREFIX}ha1_floor_sweep.csv", index=False)
    out["ha1_floor_sweep"] = floor_res

    # ---------- H-A1 反向檢查：期間分割 ----------
    section("3) H-A1 反向檢查：floor 單調性在各期間是否都成立")
    period_defs = {
        "2024H2": ("2024-07-01", "2024-12-31"),
        "2025": ("2025-01-01", "2025-12-31"),
        "2026": ("2026-01-01", "2026-08-14"),
        "pre2026": ("2024-07-01", "2025-12-31"),
    }
    per_period = {}
    for pname, (ps, pe) in period_defs.items():
        row = {}
        for f in FLOORS_YI:
            tr = floor_trades[f]
            sub = tr[(tr["signal_date"] >= ps) & (tr["signal_date"] <= pe)]
            row[str(f)] = desc(sub)
        per_period[pname] = row
        print(f"  {pname}: " + " | ".join(
            f"{f}億 n={row[str(f)]['n']} mean={row[str(f)]['mean']}" for f in FLOORS_YI))
    out["ha1_by_period"] = per_period

    # ---------- H-A1 WF-A: 60/40 ----------
    section("4) H-A1 WF-A（前60%交易日=IS / 後40%=OOS · 預先宣告）")
    cut_i = int(len(calendar) * WF_A_IS_FRAC)
    cut_date = calendar[cut_i]
    print(f"  切點 = {cut_date}（IS {calendar[0]}~{calendar[cut_i-1]} / OOS {cut_date}~{calendar[-1]}）")
    wfa = {"cut_date": cut_date, "per_floor": {}}
    for f in FLOORS_YI:
        tr = floor_trades[f]
        is_tr = tr[tr["signal_date"] < cut_date]
        oos_tr = tr[tr["signal_date"] >= cut_date]
        wfa["per_floor"][str(f)] = {"IS": desc(is_tr), "OOS": desc(oos_tr)}
        print(f"  floor {f}億  IS n={len(is_tr)} mean={desc(is_tr)['mean']} | "
              f"OOS n={len(oos_tr)} mean={desc(oos_tr)['mean']} med={desc(oos_tr)['median']}")
    # OOS-only permutation for each floor
    for f in FLOORS_YI:
        oos_tr = floor_trades[f][floor_trades[f]["signal_date"] >= cut_date]
        wfa["per_floor"][str(f)]["OOS_perm"] = multi_seed_perm(oos_tr, stock_dicts, ix_dict)
    out["ha1_wf_a"] = wfa

    # ---------- H-A1 WF-B: 擴張窗 ----------
    section("5) H-A1 WF-B（擴張窗 5 等分交易日 · fold k: train=B1..Bk, test=B(k+1)）")
    bnds = [calendar[int(len(calendar) * k / WF_B_BLOCKS)] for k in range(WF_B_BLOCKS)] + [
        calendar[-1]]
    print("  區塊界線:", bnds)
    wfb_rows, wf_sel_trades, wf_base_trades = [], [], []
    for k in range(1, WF_B_BLOCKS):
        tr_end = bnds[k]           # train = [start, tr_end)
        te_start, te_end = bnds[k], bnds[k + 1]
        is_last = (k == WF_B_BLOCKS - 1)
        best_f, best_mean, cand = None, -1e9, {}
        for f in FLOORS_YI:
            tr = floor_trades[f]
            trn = tr[tr["signal_date"] < tr_end]
            cand[str(f)] = {"n": len(trn),
                            "mean": round(float(trn["r_adj_pct"].mean()), 3) if len(trn) else None}
            if len(trn) >= WF_B_MIN_TRAIN_N and trn["r_adj_pct"].mean() > best_mean:
                best_mean, best_f = float(trn["r_adj_pct"].mean()), f
        if best_f is None:
            best_f = BASE_FLOOR_YI
        sel = floor_trades[best_f]
        bas = floor_trades[BASE_FLOOR_YI]
        m_sel = (sel["signal_date"] >= te_start) & (
            (sel["signal_date"] <= te_end) if is_last else (sel["signal_date"] < te_end))
        m_bas = (bas["signal_date"] >= te_start) & (
            (bas["signal_date"] <= te_end) if is_last else (bas["signal_date"] < te_end))
        te_sel, te_bas = sel[m_sel], bas[m_bas]
        wf_sel_trades.append(te_sel)
        wf_base_trades.append(te_bas)
        wfb_rows.append({
            "fold": k, "train_end": tr_end, "test": f"{te_start}~{te_end}",
            "picked_floor_yi": best_f, "train_mean": round(best_mean, 3) if best_f else None,
            "test_n_selected": len(te_sel),
            "test_mean_selected": round(float(te_sel["r_adj_pct"].mean()), 3) if len(te_sel) else None,
            "test_n_base05": len(te_bas),
            "test_mean_base05": round(float(te_bas["r_adj_pct"].mean()), 3) if len(te_bas) else None,
            "train_candidates": cand,
        })
        print(f"  fold{k} train<{tr_end} → pick {best_f}億 (train mean {best_mean:.2f}) | "
              f"test {te_start}~{te_end}: sel n={len(te_sel)} mean="
              f"{te_sel['r_adj_pct'].mean() if len(te_sel) else float('nan'):.2f} vs "
              f"base0.5 n={len(te_bas)} mean="
              f"{te_bas['r_adj_pct'].mean() if len(te_bas) else float('nan'):.2f}")
    sel_all = pd.concat(wf_sel_trades) if wf_sel_trades else pd.DataFrame()
    bas_all = pd.concat(wf_base_trades) if wf_base_trades else pd.DataFrame()
    out["ha1_wf_b"] = {
        "block_bounds": bnds, "folds": wfb_rows,
        "aggregate_selected": desc(sel_all), "aggregate_base05": desc(bas_all),
        "aggregate_selected_perm": multi_seed_perm(sel_all, stock_dicts, ix_dict),
    }
    print("  WF-B 串接（選擇性 floor）:", desc(sel_all))
    print("  WF-B 串接（固定 0.5 億）  :", desc(bas_all))
    pd.DataFrame([{k: v for k, v in r.items() if k != "train_candidates"} for r in wfb_rows]
                 ).to_csv(OUT_DIR / f"{PREFIX}ha1_wf_b_folds.csv", index=False)

    # ---------- H-A2 ----------
    section("6) H-A2  net_ratio 分層 + 單日集中度（基準 floor 0.5 億 · rising-edge PRIMARY）")
    base_ev_d, base_tr_d = base_ev, base_tr
    print(f"  母體（rising-edge）n_events={len(base_ev_d)} n_trades={len(base_tr_d)}")
    ha2: dict = {"population": desc(base_tr_d), "n_events": len(base_ev_d)}

    strata = {
        "net_[0.95,0.98)": (base_tr_d["net_ratio"] >= 0.95) & (base_tr_d["net_ratio"] < 0.98),
        "net_[0.98,1.00)": (base_tr_d["net_ratio"] >= 0.98) & (base_tr_d["net_ratio"] < 0.99999),
        "net_==1.000": base_tr_d["net_ratio"] >= 0.99999,
    }
    ha2["net_strata"] = {}
    for name, m in strata.items():
        sub = base_tr_d[m]
        st = desc(sub)
        pm = multi_seed_perm(sub, stock_dicts, ix_dict)
        ha2["net_strata"][name] = {"stats": st, "perm": pm}
        print(f"  {name}: n={st['n']} mean={st['mean']} med={st['median']} win={st['win_rate']} "
              f"t_p={st['t_p']} p_perm_mean={pm.get('p_mean_worst')} "
              f"{'⚠樣本不足' if st['insufficient_sample'] else ''}")

    conc = base_tr_d["conc"].dropna()
    conc_med = float(conc.median()) if len(conc) else float("nan")
    print(f"  單日集中度 buy_day_max/buy_5d 中位數 = {conc_med:.3f} "
          f"(min {conc.min():.3f} max {conc.max():.3f})")
    conc_groups = {
        f"conc_high(>={conc_med:.3f})": base_tr_d["conc"] >= conc_med,
        f"conc_low(<{conc_med:.3f})": base_tr_d["conc"] < conc_med,
    }
    ha2["conc_median"] = round(conc_med, 4)
    ha2["conc_groups"] = {}
    for name, m in conc_groups.items():
        sub = base_tr_d[m]
        st = desc(sub)
        pm = multi_seed_perm(sub, stock_dicts, ix_dict)
        ha2["conc_groups"][name] = {"stats": st, "perm": pm}
        print(f"  {name}: n={st['n']} mean={st['mean']} med={st['median']} win={st['win_rate']} "
              f"t_p={st['t_p']} p_perm_mean={pm.get('p_mean_worst')} "
              f"{'⚠樣本不足' if st['insufficient_sample'] else ''}")
    # 兩組差異檢定
    hi = base_tr_d[base_tr_d["conc"] >= conc_med]["r_adj_pct"].to_numpy(float)
    lo = base_tr_d[base_tr_d["conc"] < conc_med]["r_adj_pct"].to_numpy(float)
    if len(hi) >= 3 and len(lo) >= 3:
        ha2["conc_hi_vs_lo"] = {
            "welch_t_p": round(float(stats.ttest_ind(hi, lo, equal_var=False).pvalue), 4),
            "mannwhitney_p": round(float(stats.mannwhitneyu(hi, lo, alternative="two-sided").pvalue), 4),
        }
        print("  高 vs 低 集中度差異:", ha2["conc_hi_vs_lo"])
    # 連續版：conc 與 r_adj 的 Spearman
    if len(base_tr_d) >= 10:
        sp = stats.spearmanr(base_tr_d["conc"], base_tr_d["r_adj_pct"], nan_policy="omit")
        ha2["conc_spearman"] = {"rho": round(float(sp.statistic), 3), "p": round(float(sp.pvalue), 4)}
        sp2 = stats.spearmanr(base_tr_d["net_ratio"], base_tr_d["r_adj_pct"], nan_policy="omit")
        ha2["net_spearman"] = {"rho": round(float(sp2.statistic), 3), "p": round(float(sp2.pvalue), 4)}
        sp3 = stats.spearmanr(base_tr_d["buy_5d"], base_tr_d["r_adj_pct"], nan_policy="omit")
        ha2["buy5d_spearman"] = {"rho": round(float(sp3.statistic), 3), "p": round(float(sp3.pvalue), 4)}
        print("  Spearman conc~r_adj:", ha2["conc_spearman"],
              " net~r_adj:", ha2["net_spearman"], " buy5d~r_adj:", ha2["buy5d_spearman"])
    # H-A2 的 OOS（WF-A 切點後）
    ha2["oos_after_wfa_cut"] = {}
    for name, m in {**strata, **conc_groups}.items():
        sub = base_tr_d[m & (base_tr_d["signal_date"] >= cut_date)]
        ha2["oos_after_wfa_cut"][name] = desc(sub)
    # gap=10 robustness for H-A2
    ev10 = campaign_dedup(base_ev, calendar, CAMPAIGN_GAP)
    tr10, _ = build_trades(ev10, bars_cache, ix)
    st10 = {
        "population": desc(tr10),
        "net_[0.95,0.98)": desc(tr10[(tr10["net_ratio"] >= 0.95) & (tr10["net_ratio"] < 0.98)]),
        "net_[0.98,1.00)": desc(tr10[(tr10["net_ratio"] >= 0.98) & (tr10["net_ratio"] < 0.99999)]),
        "net_==1.000": desc(tr10[tr10["net_ratio"] >= 0.99999]),
    }
    ha2["gap10_robustness"] = st10
    print("\n  -- gap=10 對照 --")
    for k, v in st10.items():
        print(f"    {k}: n={v['n']} mean={v['mean']} med={v['median']} win={v['win_rate']} "
              f"trim3={v['trim3_mean']} 6449留{v['n_6449']}筆")
    out["ha2"] = ha2
    base_tr_d.to_csv(OUT_DIR / f"{PREFIX}ha2_base_trades.csv", index=False)

    # ---------- 7) 價格覆蓋率體檢（母體會不會再變？）----------
    section("7) 9217 tape 價格覆蓋率（決定母體還會再長多少）")
    cov = pd.read_sql_query(
        """
        SELECT substr(b.trade_date,1,7) AS ym,
               COUNT(*) AS tape_rows,
               SUM(CASE WHEN p.close>0 THEN 1 ELSE 0 END) AS priced_rows,
               COUNT(DISTINCT b.stock_id) AS tape_stocks,
               COUNT(DISTINCT CASE WHEN p.close>0 THEN b.stock_id END) AS priced_stocks
        FROM stock_broker_branch_daily b
        LEFT JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.source=? AND b.securities_trader_id=?
          AND b.trade_date BETWEEN ? AND ?
          AND length(b.stock_id)=4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        GROUP BY ym ORDER BY ym
        """,
        conn, params=(SOURCE, SOURCE, TRADER_ID, STUDY_START, STUDY_END),
    )
    cov["cov_rows_pct"] = (cov["priced_rows"] / cov["tape_rows"] * 100).round(1)
    cov["cov_stocks_pct"] = (cov["priced_stocks"] / cov["tape_stocks"] * 100).round(1)
    print(cov.to_string(index=False))
    cov.to_csv(OUT_DIR / f"{PREFIX}price_coverage_by_month.csv", index=False)
    out["price_coverage_by_month"] = cov.to_dict("records")
    out["price_coverage_overall_pct"] = round(
        float(cov["priced_rows"].sum() / cov["tape_rows"].sum() * 100), 1)
    print(f"  整體 row 覆蓋率 = {out['price_coverage_overall_pct']}%")

    (OUT_DIR / f"{PREFIX}summary.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\n[OK] wrote {OUT_DIR / (PREFIX + 'summary.json')}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
