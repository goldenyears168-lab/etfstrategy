#!/usr/bin/env python3
"""M3 addendum · 建倉判準的覆蓋偏誤修正（純研究 · DB 唯讀）.

主檔 songshan_m3_branch_behavior_discriminators.py 用「金額口徑」算 60 日 net_ratio，
但金額 = 股數 × close，而 stock_daily_bars 對部分標的只有近期價格（6449 的 9217 tape
從 2024-07-01 就有 325 天，價格卻只從 2026-04-20 開始）——於是「9217 沒有前期部位」
與「我們看不到前期部位」被混為一談，`insufficient` 桶被污染。

本檔做三件事：
  A. 事件清單與 round10（補價前 n=48）比對
  B. 加上 price_hist_ok 旗標，只用價格史完整的事件重做 H-C1 分層
  C. 改用「股數口徑」的 60 日 net_ratio（不需要歷史價格）重新分類，含 6449 逐筆

用法：
    PYTHONPATH=src .venv/bin/python \
        scripts/research/songshan_m3_accumulation_robustness.py
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

SOURCE = "finmind"
TRADER_ID = "9217"
BENCH_CODE = "IX0001"
STUDY_START, STUDY_END = "2024-07-01", "2026-08-14"
COST, HOLD, BETA = 0.003, 7, 1.15
ACC_WINDOW_DAYS, ACC_NET_THRESHOLD, ACC_MIN_WINDOW_BUY = 60, 0.30, 1.0e8
N_PERM, PERM_SEED = 5000, 20260817

BFS = ROOT / "reports" / "research" / "branch-footprint-screen"
LABELED = BFS / "songshan_m3_trades_labeled.csv"
ROUND10 = BFS / "whale_9217_round10_events.csv"
PREFIX = "songshan_m3_robust"


def ro_connect():
    c = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def section(t: str) -> None:
    print(f"\n{'=' * 96}\n{t}\n{'=' * 96}")


def stat_block(vals_pct) -> dict:
    v = pd.Series(vals_pct).dropna().to_numpy() / 100.0
    n = len(v)
    if n == 0:
        return {"n": 0}
    d = {
        "n": n,
        "mean_pct": round(float(v.mean()) * 100, 2),
        "median_pct": round(float(np.median(v)) * 100, 2),
        "win_rate_pct": round(float((v > 0).mean()) * 100, 1),
    }
    if n >= 3 and v.std() > 0:
        t, p = stats.ttest_1samp(v, 0)
        d["t_stat"], d["t_p"] = round(float(t), 2), round(float(p), 4)
    if n < 15:
        d["small_sample_warning"] = "樣本不足（n<15）"
    return d


def compare(a, b, la, lb) -> dict:
    av, bv = pd.Series(a).dropna().to_numpy(), pd.Series(b).dropna().to_numpy()
    out = {la: stat_block(av), lb: stat_block(bv)}
    if len(av) >= 3 and len(bv) >= 3:
        t, p = stats.ttest_ind(av, bv, equal_var=False)
        _, pu = stats.mannwhitneyu(av, bv, alternative="two-sided")
        out["welch_t"], out["welch_p"], out["mwu_p"] = round(float(t), 2), round(float(p), 4), round(float(pu), 4)
        out["diff_mean_pp"] = round(float(av.mean() - bv.mean()), 2)
        out["diff_median_pp"] = round(float(np.median(av) - np.median(bv)), 2)
    return out


class Bars:
    def __init__(self, conn):
        self.conn, self.cache = conn, {}

    def stock(self, sid):
        if sid not in self.cache:
            rows = self.conn.execute(
                """SELECT trade_date,open,close FROM stock_daily_bars
                   WHERE stock_id=? AND source=? AND trade_date BETWEEN ? AND ? AND close>0
                   ORDER BY trade_date""",
                (sid, SOURCE, "2024-05-01", "2026-12-31"),
            ).fetchall()
            self.cache[sid] = [(r[0], float(r[1]) if r[1] else float(r[2]), float(r[2])) for r in rows]
        return self.cache[sid]

    def ix(self):
        if "__IX__" not in self.cache:
            rows = self.conn.execute(
                """SELECT date,open,close FROM daily_bars
                   WHERE code=? AND date BETWEEN ? AND ? AND open>0 AND close>0
                   ORDER BY date, CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
                                              WHEN 'finmind' THEN 2 ELSE 3 END""",
                (BENCH_CODE, "2024-05-01", "2026-12-31"),
            ).fetchall()
            d = {}
            for r in rows:
                d.setdefault(r[0], (float(r[1]), float(r[2])))
            self.cache["__IX__"] = [(k, v[0], v[1]) for k, v in sorted(d.items())]
        return self.cache["__IX__"]


def run_perm(ev: pd.DataFrame, bars: Bars, tag: str) -> dict:
    if len(ev) < 3:
        return {"n_events": len(ev), "note": "too few"}
    ixd = build_l1h7_signal_dict(bars.ix(), HOLD)
    sd = {s: build_l1h7_signal_dict(bars.stock(s), HOLD) for s in ev["stock_id"].astype(str).unique()}
    r = permutation_test(ev[["stock_id", "signal_date"]], sd, ixd, n_perm=N_PERM,
                         seed=PERM_SEED, cost=COST, beta=BETA)
    r["tag"] = tag
    return r


def main() -> None:
    conn = ro_connect()
    bars = Bars(conn)
    out: dict = {}
    t = pd.read_csv(LABELED, dtype={"stock_id": str})
    stocks = sorted(t["stock_id"].unique())

    # ---------------------------------------------------------------- A. diff
    section("§A 事件清單 vs round10（補價前 n=48）")
    old = pd.read_csv(ROUND10, dtype={"stock_id": str})
    key = lambda d: set(zip(d["stock_id"], d["signal_date"]))  # noqa: E731
    new_k, old_k = key(t), key(old)
    added = sorted(new_k - old_k)
    removed = sorted(old_k - new_k)
    print(f"round10 n={len(old)} · 本輪 n={len(t)}")
    print(f"新增 {len(added)}: {added}")
    print(f"消失 {len(removed)}: {removed}")
    add_r = t[t.apply(lambda r: (r["stock_id"], r["signal_date"]) in set(added), axis=1)]
    print("\n新增事件的 r_adj:")
    print(add_r[["signal_date", "stock_id", "r_adj_pct", "grp_a", "grp_b"]].to_string(index=False))
    out["diff_vs_round10"] = {
        "n_round10": int(len(old)), "n_now": int(len(t)),
        "added": [list(x) for x in added], "removed": [list(x) for x in removed],
        "added_stats": stat_block(add_r["r_adj_pct"]),
        "kept_stats": stat_block(t.loc[~t.index.isin(add_r.index), "r_adj_pct"]),
    }
    print(f"新增事件 stats: {out['diff_vs_round10']['added_stats']}")
    print(f"原有事件 stats: {out['diff_vs_round10']['kept_stats']}")

    # ------------------------------------------------------- B. price history
    section("§B 價格史完整度旗標")
    cal = [r[0] for r in conn.execute(
        "SELECT trade_date FROM stock_daily_bars WHERE stock_id='2330' AND source=? "
        "AND trade_date BETWEEN ? AND ? AND close>0 ORDER BY trade_date",
        (SOURCE, STUDY_START, STUDY_END)).fetchall()]
    cal_idx = {d: i for i, d in enumerate(cal)}
    px_start = {}
    for sid in stocks:
        r = conn.execute(
            "SELECT MIN(trade_date) FROM stock_daily_bars WHERE stock_id=? AND source=? AND close>0",
            (sid, SOURCE)).fetchone()
        px_start[sid] = r[0]
    t["px_start"] = t["stock_id"].map(px_start)
    # 需要：訊號 5d 窗起點之前還有 60 個交易日的價格
    def hist_ok(row) -> bool:
        i = cal_idx.get(row["signal_date"])
        if i is None:
            return False
        need_i = i - 4 - ACC_WINDOW_DAYS
        if need_i < 0:
            return False  # tape/calendar 自 2024-07-01 起，窗口被截斷
        return row["px_start"] <= cal[need_i]

    t["price_hist_ok"] = t.apply(hist_ok, axis=1)
    print(t.groupby(["price_hist_ok", "grp_b"]).size().to_string())
    bad = t[~t["price_hist_ok"]][["signal_date", "stock_id", "px_start", "grp_a", "grp_b", "r_adj_pct"]]
    print(f"\n價格史不足以支撐 60 日窗的事件 n={len(bad)}:")
    print(bad.to_string(index=False))
    out["price_hist"] = {
        "n_ok": int(t["price_hist_ok"].sum()),
        "n_bad": int((~t["price_hist_ok"]).sum()),
        "bad_events": json.loads(bad.to_json(orient="records")),
        "stats_ok": stat_block(t.loc[t["price_hist_ok"], "r_adj_pct"]),
        "stats_bad": stat_block(t.loc[~t["price_hist_ok"], "r_adj_pct"]),
    }
    print(f"\nhist_ok stats : {out['price_hist']['stats_ok']}")
    print(f"hist_bad stats: {out['price_hist']['stats_bad']}")

    ok = t[t["price_hist_ok"]]
    out["hc1_clean_subsample"] = {}
    for tag in ("a", "b"):
        blk = {g: stat_block(ok.loc[ok[f"grp_{tag}"] == g, "r_adj_pct"]) for g in
               ("accum", "churn", "insufficient")}
        blk["accum_vs_churn"] = compare(
            ok.loc[ok[f"grp_{tag}"] == "accum", "r_adj_pct"],
            ok.loc[ok[f"grp_{tag}"] == "churn", "r_adj_pct"], "accum", "churn")
        out["hc1_clean_subsample"][tag] = blk
        print(f"\n--- 口徑{tag.upper()} · 只用價格史完整事件 (n={len(ok)})")
        for g in ("accum", "churn", "insufficient"):
            print(f"  {g:13s} {blk[g]}")
        print("  cmp", {k: v for k, v in blk["accum_vs_churn"].items() if k not in ("accum", "churn")})

    # ------------------------------------------- C. 股數口徑（覆蓋偏誤免疫）
    section("§C 股數口徑 60 日 net_ratio（不需要歷史價格）")
    raw = pd.read_sql_query(
        """SELECT stock_id, trade_date, buy AS buy_sh, sell AS sell_sh
           FROM stock_broker_branch_daily
           WHERE source=? AND securities_trader_id=? AND trade_date BETWEEN ? AND ?""",
        conn, params=(SOURCE, TRADER_ID, STUDY_START, STUDY_END))
    raw = raw[raw["stock_id"].isin(stocks)]
    piv_b = raw.pivot_table(index="trade_date", columns="stock_id", values="buy_sh", aggfunc="sum")
    piv_s = raw.pivot_table(index="trade_date", columns="stock_id", values="sell_sh", aggfunc="sum")
    piv_b = piv_b.reindex(index=cal, columns=stocks).fillna(0.0)
    piv_s = piv_s.reindex(index=cal, columns=stocks).fillna(0.0)
    rb = piv_b.rolling(ACC_WINDOW_DAYS, min_periods=1).sum().shift(5)
    rs = piv_s.rolling(ACC_WINDOW_DAYS, min_periods=1).sum().shift(5)
    net_sh = (rb - rs) / rb.where(rb > 0)
    t["acc_buy_sh_60"] = [rb.at[d, s] if d in rb.index else np.nan
                          for d, s in zip(t["signal_date"], t["stock_id"])]
    t["acc_net_sh_60"] = [net_sh.at[d, s] if d in net_sh.index else np.nan
                          for d, s in zip(t["signal_date"], t["stock_id"])]
    # 用訊號日收盤價把股數換成金額門檻（近似 1 億）
    close_t0 = {}
    for sid in stocks:
        rows = conn.execute(
            "SELECT trade_date, close FROM stock_daily_bars WHERE stock_id=? AND source=? AND close>0",
            (sid, SOURCE)).fetchall()
        close_t0[sid] = {r[0]: float(r[1]) for r in rows}
    t["close_t0"] = [close_t0.get(s, {}).get(d, np.nan) for s, d in zip(t["stock_id"], t["signal_date"])]
    t["acc_buy_ntd_proxy"] = t["acc_buy_sh_60"] * t["close_t0"]
    t["grp_sh"] = np.where(
        t["acc_buy_ntd_proxy"] < ACC_MIN_WINDOW_BUY, "insufficient",
        np.where(t["acc_net_sh_60"] >= ACC_NET_THRESHOLD, "accum", "churn"))
    blk = {g: stat_block(t.loc[t["grp_sh"] == g, "r_adj_pct"]) for g in ("accum", "churn", "insufficient")}
    blk["accum_vs_churn"] = compare(
        t.loc[t["grp_sh"] == "accum", "r_adj_pct"], t.loc[t["grp_sh"] == "churn", "r_adj_pct"],
        "accum", "churn")
    for g in ("accum", "churn", "insufficient"):
        print(f"  {g:13s} {blk[g]}")
    print("  cmp", {k: v for k, v in blk["accum_vs_churn"].items() if k not in ("accum", "churn")})
    for g in ("accum", "churn"):
        sub = t[t["grp_sh"] == g]
        if len(sub) >= 5:
            p = run_perm(sub, bars, f"sh_{g}")
            blk[f"perm_{g}"] = p
            print(f"  perm[{g}] n={p['n_events']} p_mean={p['p_value_mean_onesided']:.4f} "
                  f"p_median={p['p_value_median_onesided']:.4f}")
    out["hc1_share_based"] = blk

    print("\n--- 6449 在股數口徑下的分類")
    c = t[t["stock_id"] == "6449"][
        ["signal_date", "r_adj_pct", "acc_buy_sh_60", "acc_buy_ntd_proxy", "acc_net_sh_60",
         "grp_sh", "grp_a", "grp_b"]].copy()
    c["acc_buy_ntd_proxy"] = (c["acc_buy_ntd_proxy"] / 1e8).round(3)
    c["acc_net_sh_60"] = c["acc_net_sh_60"].round(3)
    print(c.to_string(index=False))
    out["case_6449_share_based"] = json.loads(c.to_json(orient="records"))

    # 「排除高建倉」 vs 「只跟建倉」兩種用法的實際 P&L
    section("§D 兩種用法的實際結果（股數口徑 · 全母體）")
    usage = {}
    for name, mask in (
        ("只跟 accum（H-C1 提議）", t["grp_sh"] == "accum"),
        ("排除 accum（dayflip 原用法方向）", t["grp_sh"] != "accum"),
        ("只跟 net_sh>=0.30 或 insufficient", t["grp_sh"] != "churn"),
        ("全母體（對照）", pd.Series(True, index=t.index)),
    ):
        usage[name] = stat_block(t.loc[mask, "r_adj_pct"])
        usage[name]["kept_pct"] = round(100 * float(mask.mean()), 1)
        print(f"  {name:34s} {usage[name]}")
    out["usage_comparison"] = usage

    t.to_csv(BFS / f"{PREFIX}_trades.csv", index=False)
    (BFS / f"{PREFIX}_summary.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n[OUT] {BFS / (PREFIX + '_summary.json')}")


if __name__ == "__main__":
    main()
