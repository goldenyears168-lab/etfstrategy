#!/usr/bin/env python3
"""Stage 2-4 · branch-footprint-screen 擴大版 · 買賣雙側 × hard/soft × 全 universe.

讀股數版 master parts（build_branch_footprint_master_expanded.py），對 4 象限跑
L1H7 forward-return 分點層級篩選：soft_buy / hard_buy / soft_sell / hard_sell。
  * soft = 無集中度門檻，只要當日該股 Top1（買/賣）分點且金額≥SOFT_FLOOR。
  * hard = Top1+2占比≥0.80、Top1+2額≥HARD_FLOOR、Top3占比<0.10、出手分點數≥15。

盲點修正：
  * 分點來源 = replica（完整 548→1010 家）；價格 = local∪replica finmind（取最大覆蓋，
    ~1331 檔 vs 原研究 346 檔）。集中度比率用股數（price-invariant）不受價格覆蓋截斷。
  * n-floor：n≥MIN_N 且 n_stocks≥MIN_STOCKS 才進 FDR（殺薄樣本假冠軍）。
  * 排序主鍵 = median_r_adj + win_rate。BH-FDR。快照日期寫進報告。
  * 標注每家分點：已在專家池 / 已知專家 / 新候選。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/run_branch_footprint_screen_expanded.py
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[2]
REPLICA_DB = ROOT / "data/replica/stocks.db"
LOCAL_DB = ROOT / "data/stocks.db"
OUT_DIR = ROOT / "reports/research/branch-footprint-screen/expanded"
PARTS_DIR = OUT_DIR / "master_parts"
MEGA_BLACKLIST_PATH = (
    ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
)

COST, HOLD, DEDUP_DAYS, BETA = 0.003, 7, 5, 1.15
SOFT_FLOOR, HARD_FLOOR = 5_000_000.0, 1e8
HARD_TOP12_SHARE, HARD_TOP3_CEIL, HARD_MIN_BRANCHES = 0.80, 0.10, 15
MIN_N, MIN_STOCKS = 10, 3
KNOWN_EXPERTS = {"9217", "9801", "9661", "9227"}


def load_seats() -> set[str]:
    d = json.loads((ROOT / "config/second_disp_expert_pool_watch.json").read_text("utf-8"))
    return {str(s["id"]).upper() for s in d.get("seats", [])}


def load_mega() -> set[str]:
    return set(json.loads(MEGA_BLACKLIST_PATH.read_text("utf-8"))["symbols"])


def load_master() -> pd.DataFrame:
    parts = sorted(PARTS_DIR.glob("*.parquet"))
    if not parts:
        raise SystemExit(f"no master parts in {PARTS_DIR}")
    df = pd.concat([pd.read_parquet(p) for p in parts], ignore_index=True)
    df["stock_id"] = df["stock_id"].astype(str)
    df["top12_sh"] = df["top1_sh"] + df["top2_sh"]
    df["top1_share"] = df["top1_sh"] / df["total_shares"]
    df["top12_share"] = df["top12_sh"] / df["total_shares"]
    df["top3_share"] = df["top3_sh"] / df["total_shares"]
    return df


def load_prices() -> pd.DataFrame:
    """local ∪ replica finmind 個股 open/close，dedup (stock,date) 優先 local。"""
    frames = []
    for db, pref in [(LOCAL_DB, 0), (REPLICA_DB, 1)]:
        if not db.exists():
            continue
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        d = pd.read_sql_query(
            """SELECT stock_id, trade_date, open, close FROM stock_daily_bars
               WHERE source='finmind' AND trade_date>='2024-05-01' AND close>0""", c)
        c.close()
        d["pref"] = pref
        frames.append(d)
    px = pd.concat(frames, ignore_index=True)
    px["stock_id"] = px["stock_id"].astype(str)
    px["open"] = px["open"].where(px["open"] > 0, px["close"])
    px = px.sort_values("pref").drop_duplicates(["stock_id", "trade_date"], keep="first")
    return px[["stock_id", "trade_date", "open", "close"]]


def load_ix() -> pd.DataFrame:
    c = sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True)
    rows = c.execute(
        """SELECT date, open, close FROM daily_bars WHERE code='IX0001'
           AND date>='2024-05-01' AND open>0 AND close>0
           ORDER BY date, CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
             WHEN 'finmind' THEN 2 ELSE 3 END""").fetchall()
    c.close()
    out = {}
    for d, o, cl in rows:
        out.setdefault(d, (float(o), float(cl)))
    ds = sorted(out)
    return pd.DataFrame({"date": ds, "open": [out[d][0] for d in ds], "close": [out[d][1] for d in ds]})


def build_l1h7_lookup(dates, opens, closes) -> pd.DataFrame:
    n = len(dates)
    rows = []
    for i in range(n - 1):
        e, x = i + 1, i + HOLD
        if x >= n:
            continue
        rows.append((dates[i], dates[e], opens[e], dates[x], closes[x]))
    return pd.DataFrame(rows, columns=["signal_date", "entry_date", "entry_open", "exit_date", "exit_close"])


def make_events(master, side, mode, mega, close_map) -> pd.DataFrame:
    d = master[master["side"] == side]
    d = d[~d["stock_id"].isin(mega) & ~d["stock_id"].str.startswith("00")]
    if mode == "hard":
        d = d[(d["top12_share"] >= HARD_TOP12_SHARE) & (d["top3_share"] < HARD_TOP3_CEIL)
              & (d["n_branches"] >= HARD_MIN_BRANCHES)]
    d = d.dropna(subset=["top1_id"]).copy()
    key = list(zip(d["stock_id"], d["trade_date"]))
    d["close"] = [close_map.get(k) for k in key]
    d = d[d["close"].notna()]
    d["top1_amt"] = d["top1_sh"] * d["close"]
    d["top12_amt"] = d["top12_sh"] * d["close"]
    if mode == "soft":
        d = d[d["top1_amt"] >= SOFT_FLOOR]
    else:
        d = d[d["top12_amt"] >= HARD_FLOOR]
    return d.rename(columns={"top1_id": "securities_trader_id", "trade_date": "signal_date"})[
        ["signal_date", "stock_id", "securities_trader_id", "top1_amt"]]


def forward_returns(events, bars_lookup, ix_lookup) -> pd.DataFrame:
    recs = []
    for r in events.itertuples(index=False):
        lk = bars_lookup.get(r.stock_id)
        if lk is None or r.signal_date not in lk.index or r.signal_date not in ix_lookup.index:
            continue
        s = lk.loc[r.signal_date]
        b = ix_lookup.loc[r.signal_date]
        r_s = s["exit_close"] / s["entry_open"] - 1 - COST
        r_ix = b["exit_close"] / b["entry_open"] - 1
        recs.append({"signal_date": r.signal_date, "stock_id": r.stock_id,
                     "securities_trader_id": r.securities_trader_id, "amt": r.top1_amt,
                     "r_adj_pct": (r_s - BETA * r_ix) * 100})
    ev = pd.DataFrame(recs)
    if ev.empty:
        return ev
    ev = ev.sort_values(["stock_id", "securities_trader_id", "signal_date"])
    keep = []
    for _, g in ev.groupby(["stock_id", "securities_trader_id"]):
        last = None
        for _, r in g.iterrows():
            if last is None or (pd.Timestamp(r["signal_date"]) - pd.Timestamp(last)).days > DEDUP_DAYS:
                keep.append(r); last = r["signal_date"]
    return pd.DataFrame(keep)


def aggregate(ev) -> pd.DataFrame:
    rows = []
    for tid, g in ev.groupby("securities_trader_id"):
        ra = g["r_adj_pct"].to_numpy()
        n, nst = len(ra), g["stock_id"].nunique()
        row = {"securities_trader_id": tid, "n": n, "n_stocks": nst,
               "median_r_adj_pct": float(np.median(ra)), "mean_r_adj_pct": float(np.mean(ra)),
               "win_rate_pct": float((ra > 0).mean() * 100),
               "first_sig": g["signal_date"].min(), "last_sig": g["signal_date"].max()}
        if n >= MIN_N and nst >= MIN_STOCKS:
            t, p = stats.ttest_1samp(ra, 0.0)
            row["t_stat"], row["p_value"] = float(t), float(p)
        else:
            row["t_stat"], row["p_value"] = None, None
        rows.append(row)
    bs = pd.DataFrame(rows)
    testable = bs[bs["p_value"].notna()].sort_values("p_value").copy()
    m = len(testable)
    if m:
        testable["bh_rank"] = np.arange(1, m + 1)
        testable["bh_critical"] = testable["bh_rank"] / m * 0.05
        testable["bh_significant"] = testable["p_value"] <= testable["bh_critical"]
        bs = bs.merge(testable[["securities_trader_id", "bh_rank", "bh_critical", "bh_significant"]],
                      on="securities_trader_id", how="left")
    else:
        bs["bh_significant"] = False
    bs["bh_significant"] = bs["bh_significant"].fillna(False)
    return bs.sort_values(["median_r_adj_pct", "win_rate_pct"], ascending=False)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    seats, mega = load_seats(), load_mega()
    manifest = json.loads((OUT_DIR / "master_manifest.json").read_text("utf-8"))
    print(f"[SNAPSHOT] branch={manifest['snapshot_file_mtime']} data_max={manifest['data_max_trade_date']}")

    master = load_master()
    print(f"[INFO] master rows={len(master):,} buy={int((master.side=='buy').sum()):,} sell={int((master.side=='sell').sum()):,}")

    px = load_prices()
    print(f"[INFO] price stocks={px.stock_id.nunique()} rows={len(px):,}")
    close_map = {(s, d): c for s, d, c in zip(px["stock_id"], px["trade_date"], px["close"])}

    c = sqlite3.connect(f"file:{REPLICA_DB}?mode=ro", uri=True)
    names = dict(c.execute("SELECT DISTINCT securities_trader_id, securities_trader FROM stock_broker_branch_daily").fetchall())
    c.close()

    ixdf = load_ix()
    ix_lookup = build_l1h7_lookup(ixdf["date"].to_numpy(), ixdf["open"].to_numpy(), ixdf["close"].to_numpy()).set_index("signal_date")

    bars_lookup = {}
    for sid, g in px.sort_values("trade_date").groupby("stock_id"):
        lk = build_l1h7_lookup(g["trade_date"].to_numpy(), g["open"].to_numpy(), g["close"].to_numpy())
        if not lk.empty:
            bars_lookup[sid] = lk.set_index("signal_date")

    summary = []
    for side, mode in [("buy", "soft"), ("buy", "hard"), ("sell", "soft"), ("sell", "hard")]:
        tag = f"{side}_{mode}"
        e = make_events(master, side, mode, mega, close_map)
        print(f"[EVENTS] {tag}: priced_raw={len(e):,} stocks={e['stock_id'].nunique()} branches={e['securities_trader_id'].nunique()}")
        ev = forward_returns(e, bars_lookup, ix_lookup)
        if ev.empty:
            print(f"[WARN] {tag} empty"); continue
        ev.to_csv(OUT_DIR / f"events_{tag}.csv", index=False)
        bs = aggregate(ev)
        bs["name"] = bs["securities_trader_id"].map(lambda x: names.get(x, "?"))
        bs["in_pool"] = bs["securities_trader_id"].str.upper().isin(seats)
        bs["known"] = bs["securities_trader_id"].isin(KNOWN_EXPERTS)
        bs["status"] = np.where(bs["in_pool"], "已在池", np.where(bs["known"], "已知專家", "新"))
        bs.to_csv(OUT_DIR / f"leaderboard_{tag}.csv", index=False)
        n_test = int(bs["p_value"].notna().sum()); n_sig = int(bs["bh_significant"].sum())
        summary.append(f"{tag}: dedup事件={len(ev):,} 可檢定分點={n_test} BH顯著={n_sig}")
        print("[LEADER] " + summary[-1])

    (OUT_DIR / "_screen_summary.txt").write_text(
        f"branch_snapshot={manifest['snapshot_file_mtime']} data_max={manifest['data_max_trade_date']}\n"
        f"price_stocks={px.stock_id.nunique()} MIN_N={MIN_N} MIN_STOCKS={MIN_STOCKS}\n" + "\n".join(summary) + "\n", "utf-8")
    print("[DONE]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
