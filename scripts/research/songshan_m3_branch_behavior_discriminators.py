#!/usr/bin/env python3
"""M3 · 凱基松山（9217）跟單訊號的分點行為判別（純研究 · DB 唯讀 · 不下單）.

母體 SSOT = scripts/research/run_songshan_follow_watch.py::scan_5d_net95
    滾動 5 交易日 buy_5d>=0.5億 ∩ net_ratio=(buy_5d-sell_5d)/buy_5d>=0.95 ∩ !mega
評估協議 L1H7（SSOT = round10_drought_songshan_signal_diagnosis.py）：
    T+1 開盤進 / 第7個交易日收盤出 / 30bps / r_adj = r_s - 1.15 * r_IX0001

H-C1  建倉 vs 隔日沖判別式（反向使用 dayflip FROZEN_SPEC_V1 的 accumulation_exclusion）
H-C2  多席共識（live-faithful 5d net95 定義重測 round-3 被 reject 的 consensus）

用法：
    PYTHONPATH=src .venv/bin/python \
        scripts/research/songshan_m3_branch_behavior_discriminators.py
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
    outlier_trim_sensitivity,
    permutation_test,
)
from stock_db import DEFAULT_DB_PATH  # noqa: E402

SOURCE = "finmind"
TRADER_ID = "9217"
BENCH_CODE = "IX0001"
STUDY_START = "2024-07-01"
STUDY_END = "2026-08-14"
COST, HOLD, BETA = 0.003, 7, 1.15
BASE_FLOOR, BASE_NET = 0.5e8, 0.95

# FROZEN_SPEC_V1 · signal.step2_seat_filters.accumulation_exclusion
ACC_WINDOW_DAYS = 60
ACC_NET_THRESHOLD = 0.30
ACC_MIN_WINDOW_BUY = 1.0e8

# 多席共識：全市場 by-trader tape 只到 2026-07-16（之後每日僅 ~10 席）
MULTISEAT_TAPE_END = "2026-07-16"

N_PERM = 5000
PERM_SEED = 20260817

BFS = ROOT / "reports" / "research" / "branch-footprint-screen"
MEGA_PATH = BFS / "ab58_xMega_copytrade" / "mega_blacklist_v1.json"
MM_EXCL_PATH = BFS / "market_maker_branch_exclusion_v1.json"
POOL_PATH = ROOT / "config" / "second_disp_expert_pool_watch.json"
OUT_DIR = BFS
PREFIX = "songshan_m3"

PEER_SEATS_MANUAL = {
    "9801": "元大-松江",
    "9661": "富邦-新店",
    "9227": "凱基-城中",
}


def ro_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def section(title: str) -> None:
    print(f"\n{'=' * 96}\n{title}\n{'=' * 96}")


def load_mega() -> set[str]:
    return {str(s) for s in json.loads(MEGA_PATH.read_text())["symbols"]}


def load_mm_exclusion() -> dict[str, str]:
    raw = json.loads(MM_EXCL_PATH.read_text())
    return {str(s["trader_id"]): s.get("name", "") for s in raw["symbols"]}


def load_pool_seats() -> dict[str, str]:
    raw = json.loads(POOL_PATH.read_text())
    return {str(s["id"]): s.get("name", "") for s in raw["seats"]}


# ------------------------------------------------------------------ base panel
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


RAW_CACHE = BFS / f"_{PREFIX}_raw9217.parquet"


def load_raw_9217(conn) -> pd.DataFrame:
    """9217 tape × close，同時保留股數（flip 需要）。"""
    if RAW_CACHE.exists():
        print(f"[CACHE] {RAW_CACHE.name}")
        return pd.read_parquet(RAW_CACHE)
    raw = pd.read_sql_query(
        """
        SELECT b.stock_id, b.trade_date, b.buy AS buy_sh, b.sell AS sell_sh
        FROM stock_broker_branch_daily b
        WHERE b.source=? AND b.securities_trader_id=?
          AND b.trade_date BETWEEN ? AND ?
          AND length(b.stock_id)=4
          AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        """,
        conn,
        params=(SOURCE, TRADER_ID, STUDY_START, STUDY_END),
    )
    sids = sorted(raw["stock_id"].unique())
    print(f"[INFO] raw tape rows={len(raw)} stocks={len(sids)} · 取 close ...")
    frames = []
    for i in range(0, len(sids), 400):
        chunk = sids[i : i + 400]
        ph = ",".join("?" * len(chunk))
        frames.append(
            pd.read_sql_query(
                f"""
                SELECT stock_id, trade_date, close FROM stock_daily_bars
                WHERE source=? AND close>0 AND trade_date BETWEEN ? AND ?
                  AND stock_id IN ({ph})
                """,
                conn,
                params=(SOURCE, STUDY_START, STUDY_END, *chunk),
            )
        )
    px = pd.concat(frames, ignore_index=True)
    raw = raw.merge(px, on=["stock_id", "trade_date"], how="inner")
    raw["buy_amt"] = raw["buy_sh"] * raw["close"]
    raw["sell_amt"] = raw["sell_sh"] * raw["close"]
    RAW_CACHE.parent.mkdir(parents=True, exist_ok=True)
    raw.to_parquet(RAW_CACHE, index=False)
    return raw


def _wide(raw: pd.DataFrame, col: str, cal: list[str], stocks: list[str]) -> pd.DataFrame:
    w = raw.pivot_table(index="trade_date", columns="stock_id", values=col, aggfunc="sum")
    return w.reindex(index=cal, columns=stocks).fillna(0.0)


def build_panel(conn) -> tuple[pd.DataFrame, list[str]]:
    cal = load_calendar(conn)
    raw = load_raw_9217(conn)
    print(f"[INFO] calendar {len(cal)} days {cal[0]}..{cal[-1]}")
    print(f"[INFO] 9217 tape rows={len(raw)} stocks={raw['stock_id'].nunique()}")
    stocks = sorted(raw["stock_id"].unique())
    W = {c: _wide(raw, c, cal, stocks) for c in ("buy_sh", "sell_sh", "buy_amt", "sell_amt")}
    active = _wide(raw.assign(_one=1.0), "_one", cal, stocks) > 0

    out = {c: W[c] for c in W}
    out["buy_5d"] = W["buy_amt"].rolling(5, min_periods=5).sum()
    out["sell_5d"] = W["sell_amt"].rolling(5, min_periods=5).sum()
    out["net_ratio"] = (out["buy_5d"] - out["sell_5d"]) / out["buy_5d"].where(out["buy_5d"] > 0)

    rb = W["buy_amt"].rolling(ACC_WINDOW_DAYS, min_periods=1).sum()
    rs = W["sell_amt"].rolling(ACC_WINDOW_DAYS, min_periods=1).sum()
    rd = active.astype(float).rolling(ACC_WINDOW_DAYS, min_periods=1).count()
    # 口徑 A（faithful FROZEN_SPEC）：T0 前 60 交易日，含訊號 5d 窗的前 4 天
    out["acc_buy_a"], out["acc_sell_a"], out["acc_days_a"] = rb.shift(1), rs.shift(1), rd.shift(1)
    # 口徑 B（去重疊）：訊號 5d 窗開始前的 60 交易日
    out["acc_buy_b"], out["acc_sell_b"], out["acc_days_b"] = rb.shift(5), rs.shift(5), rd.shift(5)
    for tag in ("a", "b"):
        out[f"acc_net_{tag}"] = (out[f"acc_buy_{tag}"] - out[f"acc_sell_{tag}"]) / out[
            f"acc_buy_{tag}"
        ].where(out[f"acc_buy_{tag}"] > 0)
    out["sell_sh_next"] = W["sell_sh"].shift(-1)
    out["flip"] = out["sell_sh_next"] / W["buy_sh"].where(W["buy_sh"] > 0)

    m = pd.concat(
        {k: v.stack(future_stack=True) for k, v in out.items()}, axis=1
    ).reset_index()
    m = m.rename(columns={"level_0": "trade_date", "level_1": "stock_id"})
    m["ym"] = m["trade_date"].str[:7]
    return m, cal


def rising_edge(panel: pd.DataFrame, trig: pd.Series, keep: list[str]) -> pd.DataFrame:
    df = panel.assign(triggered=trig.to_numpy())
    prev = df.groupby("stock_id", sort=False)["triggered"].shift(1).fillna(False)
    ev = df[df["triggered"] & (~prev)].copy()
    return (
        ev.rename(columns={"trade_date": "signal_date"})[["stock_id", "signal_date", *keep]]
        .sort_values(["signal_date", "stock_id"])
        .reset_index(drop=True)
    )


# ------------------------------------------------------------------ L1H7 engine
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
    extra = [c for c in events.columns if c not in ("stock_id", "signal_date")]
    for row in events.itertuples(index=False):
        rec = row._asdict()
        b = bars.stock(rec["stock_id"])
        if len(b) < 10:
            drops["no_bars"] += 1
            continue
        ed, eo = _next_open(b, rec["signal_date"])
        if not ed:
            drops["no_entry"] += 1
            continue
        xd, xc = _exit_close(b, ed)
        if not xd:
            drops["no_exit"] += 1
            continue
        be, bo = _next_open(ix, rec["signal_date"])
        if not be:
            drops["no_bench"] += 1
            continue
        _, bc = _exit_close(ix, be)
        if not bc:
            drops["no_bench"] += 1
            continue
        r_s = xc / eo - 1 - COST
        r_ix = bc / bo - 1
        d = {
            "signal_date": rec["signal_date"],
            "stock_id": rec["stock_id"],
            "entry_date": ed,
            "exit_date": xd,
            "r_pct": round(r_s * 100, 3),
            "r_ix_pct": round(r_ix * 100, 3),
            "r_adj_pct": round((r_s - BETA * r_ix) * 100, 3),
        }
        for c in extra:
            d[c] = rec[c]
        out.append(d)
    return pd.DataFrame(out), drops


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
        "sum_pct": round(float(v.sum()) * 100, 1),
    }
    if n >= 3 and v.std() > 0:
        t, p = stats.ttest_1samp(v, 0)
        d["t_stat"] = round(float(t), 2)
        d["t_p"] = round(float(p), 4)
    if n < 15:
        d["small_sample_warning"] = "樣本不足（n<15）"
    return d


def group_compare(a: pd.Series, b: pd.Series, label_a: str, label_b: str) -> dict:
    av = pd.Series(a).dropna().to_numpy()
    bv = pd.Series(b).dropna().to_numpy()
    out = {label_a: stat_block(av), label_b: stat_block(bv)}
    if len(av) >= 3 and len(bv) >= 3:
        t, p = stats.ttest_ind(av, bv, equal_var=False)
        out["welch_t"] = round(float(t), 2)
        out["welch_p"] = round(float(p), 4)
        u, pu = stats.mannwhitneyu(av, bv, alternative="two-sided")
        out["mwu_p"] = round(float(pu), 4)
        out["diff_mean_pp"] = round(float(av.mean() - bv.mean()), 2)
        out["diff_median_pp"] = round(float(np.median(av) - np.median(bv)), 2)
    return out


def run_perm(events_df: pd.DataFrame, bars: Bars, tag: str) -> dict:
    if len(events_df) == 0:
        return {"n_events": 0, "note": "no events"}
    ix_dict = build_l1h7_signal_dict(bars.ix(), HOLD)
    sdicts = {
        sid: build_l1h7_signal_dict(bars.stock(sid), HOLD)
        for sid in events_df["stock_id"].astype(str).unique()
    }
    res = permutation_test(
        events_df[["stock_id", "signal_date"]],
        sdicts,
        ix_dict,
        n_perm=N_PERM,
        seed=PERM_SEED,
        cost=COST,
        beta=BETA,
    )
    res["tag"] = tag
    return res


# ------------------------------------------------------------------ H-C2 peers
def load_peer_tape(conn, stocks: list[str], seats: list[str]) -> pd.DataFrame:
    phs = ",".join("?" * len(stocks))
    phb = ",".join("?" * len(seats))
    tape = pd.read_sql_query(
        f"""
        SELECT securities_trader_id AS seat, securities_trader AS seat_name,
               stock_id, trade_date, buy AS buy_sh, sell AS sell_sh
        FROM stock_broker_branch_daily
        WHERE source=? AND stock_id IN ({phs}) AND securities_trader_id IN ({phb})
          AND trade_date BETWEEN ? AND ?
        """,
        conn,
        params=(SOURCE, *stocks, *seats, STUDY_START, STUDY_END),
    )
    if tape.empty:
        return tape
    px = pd.read_sql_query(
        f"""
        SELECT stock_id, trade_date, close FROM stock_daily_bars
        WHERE source=? AND close>0 AND trade_date BETWEEN ? AND ?
          AND stock_id IN ({phs})
        """,
        conn,
        params=(SOURCE, STUDY_START, STUDY_END, *stocks),
    )
    tape = tape.merge(px, on=["stock_id", "trade_date"], how="inner")
    tape["buy_amt"] = tape["buy_sh"] * tape["close"]
    tape["sell_amt"] = tape["sell_sh"] * tape["close"]
    return tape


def peer_5d_panel(tape: pd.DataFrame, cal: list[str], seats: list[str]) -> pd.DataFrame:
    t = tape[tape["seat"].isin(seats)].copy()
    if t.empty:
        return pd.DataFrame()
    pairs = t[["seat", "stock_id"]].drop_duplicates()
    frames = []
    cal_df = pd.DataFrame({"trade_date": cal})
    for seat, sid in pairs.itertuples(index=False):
        sub = t[(t["seat"] == seat) & (t["stock_id"] == sid)][
            ["trade_date", "buy_amt", "sell_amt"]
        ]
        m = cal_df.merge(sub, on="trade_date", how="left").fillna(0.0)
        m["seat"] = seat
        m["stock_id"] = sid
        m["buy_5d"] = m["buy_amt"].rolling(5, min_periods=5).sum()
        m["sell_5d"] = m["sell_amt"].rolling(5, min_periods=5).sum()
        frames.append(m)
    out = pd.concat(frames, ignore_index=True)
    out["net_ratio"] = np.where(
        out["buy_5d"] > 0, (out["buy_5d"] - out["sell_5d"]) / out["buy_5d"].replace(0, np.nan), np.nan
    )
    out["sell_net_ratio"] = np.where(
        out["sell_5d"] > 0,
        (out["sell_5d"] - out["buy_5d"]) / out["sell_5d"].replace(0, np.nan),
        np.nan,
    )
    return out


def main() -> None:
    conn = ro_connect()
    mega = load_mega()
    mm_excl = load_mm_exclusion()
    pool = load_pool_seats()
    summary: dict = {
        "generated_for": "M3 songshan branch-behavior discriminators",
        "study_window": [STUDY_START, STUDY_END],
        "protocol": {"cost": COST, "hold": HOLD, "beta": BETA},
    }

    # ---------------------------------------------------------- base population
    section("§0 基準母體重算（255 檔補價後）")
    panel, cal = build_panel(conn)
    trig = (
        (panel["buy_5d"] >= BASE_FLOOR)
        & (panel["net_ratio"] >= BASE_NET)
        & (~panel["stock_id"].isin(mega))
    ).fillna(False)
    keep = [
        "buy_5d", "sell_5d", "net_ratio",
        "acc_buy_a", "acc_sell_a", "acc_days_a", "acc_net_a",
        "acc_buy_b", "acc_sell_b", "acc_days_b", "acc_net_b",
        "buy_sh", "sell_sh_next", "flip",
    ]
    events = rising_edge(panel, trig, keep)
    print(f"[BASE] events n={len(events)}  {events['signal_date'].min()}..{events['signal_date'].max()}")
    bars = Bars(conn)
    trades, drops = build_trades(bars, events)
    print(f"[BASE] trades n={len(trades)} drops={drops}")
    base_stats = stat_block(trades["r_adj_pct"])
    print(f"[BASE] r_adj {base_stats}")
    summary["base"] = {
        "n_events": int(len(events)),
        "n_trades": int(len(trades)),
        "drops": drops,
        "stats": base_stats,
        "first_signal": events["signal_date"].min(),
        "last_signal": events["signal_date"].max(),
    }
    base_perm = run_perm(trades, bars, "base")
    print(f"[BASE] perm {base_perm}")
    summary["base"]["permutation"] = base_perm
    summary["base"]["outlier_trim"] = outlier_trim_sensitivity(
        trades["r_adj_pct"].to_numpy()
    )

    # ---------------------------------------------------------------- H-C1
    section("§1 H-C1 建倉 vs 進出型（FROZEN_SPEC accumulation_exclusion 反向使用）")
    t = trades.copy()
    for tag in ("a", "b"):
        t[f"win_ok_{tag}"] = t[f"acc_buy_{tag}"] >= ACC_MIN_WINDOW_BUY
        t[f"grp_{tag}"] = np.where(
            ~t[f"win_ok_{tag}"],
            "insufficient",
            np.where(t[f"acc_net_{tag}"] >= ACC_NET_THRESHOLD, "accum", "churn"),
        )
    hc1: dict = {}
    for tag, label in (("a", "口徑A·T0前60日(含訊號窗前4日)"), ("b", "口徑B·5d窗前60日(去重疊)")):
        print(f"\n--- {label}")
        blk: dict = {"label": label, "groups": {}}
        for g in ("accum", "churn", "insufficient"):
            sub = t[t[f"grp_{tag}"] == g]
            blk["groups"][g] = stat_block(sub["r_adj_pct"])
            blk["groups"][g]["n_stocks"] = int(sub["stock_id"].nunique())
            print(f"  {g:13s} {blk['groups'][g]}")
        cmp_ = group_compare(
            t.loc[t[f"grp_{tag}"] == "accum", "r_adj_pct"],
            t.loc[t[f"grp_{tag}"] == "churn", "r_adj_pct"],
            "accum", "churn",
        )
        blk["accum_vs_churn"] = cmp_
        print("  cmp", {k: v for k, v in cmp_.items() if k not in ("accum", "churn")})
        for g in ("accum", "churn"):
            sub = t[t[f"grp_{tag}"] == g]
            if len(sub) >= 5:
                blk[f"perm_{g}"] = run_perm(sub, bars, f"{tag}_{g}")
                print(f"  perm[{g}] p_mean={blk[f'perm_{g}']['p_value_mean_onesided']:.4f} "
                      f"p_median={blk[f'perm_{g}']['p_value_median_onesided']:.4f}")
        # 有效窗口（>=60 交易日）子樣本
        full = t[(t[f"acc_days_{tag}"] >= ACC_WINDOW_DAYS)]
        blk["full_window_only"] = {
            "n": int(len(full)),
            "accum": stat_block(full.loc[full[f"grp_{tag}"] == "accum", "r_adj_pct"]),
            "churn": stat_block(full.loc[full[f"grp_{tag}"] == "churn", "r_adj_pct"]),
        }
        hc1[tag] = blk

    # --- flip 分布與分層
    section("§1b flip = T+1 賣出股數 / T0 買進股數")
    fl = t[t["flip"].notna()].copy()
    print(f"flip 可算 n={len(fl)} / {len(t)}")
    q = fl["flip"].describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9])
    print(q.to_string())
    flip_blk: dict = {
        "n_with_flip": int(len(fl)),
        "distribution": {k: round(float(v), 4) for k, v in q.items()},
        "frozen_spec_seat_flip_median_9217": 0.446,
    }
    if len(fl) >= 6:
        try:
            fl["flip_tert"] = pd.qcut(fl["flip"], 3, labels=["low", "mid", "high"], duplicates="drop")
        except ValueError:
            fl["flip_tert"] = "n/a"
        for g in fl["flip_tert"].dropna().unique():
            sub = fl[fl["flip_tert"] == g]
            flip_blk[f"tert_{g}"] = stat_block(sub["r_adj_pct"])
            flip_blk[f"tert_{g}"]["flip_range"] = [
                round(float(sub["flip"].min()), 3), round(float(sub["flip"].max()), 3)
            ]
            print(f"  {g}: {flip_blk[f'tert_{g}']}")
        rho, prho = stats.spearmanr(fl["flip"], fl["r_adj_pct"])
        flip_blk["spearman_flip_vs_radj"] = {"rho": round(float(rho), 3), "p": round(float(prho), 4)}
        print(f"  spearman(flip, r_adj) rho={rho:.3f} p={prho:.4f}")
        # 低 flip（< 0.40 = FROZEN_SPEC high_flip 門檻）vs 高 flip
        flip_blk["lt040_vs_ge040"] = group_compare(
            fl.loc[fl["flip"] < 0.40, "r_adj_pct"], fl.loc[fl["flip"] >= 0.40, "r_adj_pct"],
            "flip_lt040", "flip_ge040",
        )
        print("  lt040 vs ge040:", flip_blk["lt040_vs_ge040"])
        lo = fl[fl["flip"] < 0.40]
        if len(lo) >= 5:
            flip_blk["perm_flip_lt040"] = run_perm(lo, bars, "flip_lt040")
    hc1["flip"] = flip_blk

    # --- 反向檢查：是不是 buy_5d 的代理變數
    section("§1c 反向檢查 · 控制 buy_5d")
    rev: dict = {}
    t["log_buy5d"] = np.log10(t["buy_5d"].clip(lower=1))
    for tag in ("a", "b"):
        sub = t[t[f"grp_{tag}"] != "insufficient"].copy()
        if len(sub) < 8:
            rev[tag] = {"note": "樣本不足"}
            continue
        sub["is_accum"] = (sub[f"grp_{tag}"] == "accum").astype(float)
        rho, prho = stats.spearmanr(sub[f"acc_net_{tag}"], sub["buy_5d"])
        # OLS: r_adj ~ 1 + is_accum + log_buy5d
        X = np.column_stack([np.ones(len(sub)), sub["is_accum"], sub["log_buy5d"]])
        y = sub["r_adj_pct"].to_numpy()
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ coef
        dof = len(sub) - X.shape[1]
        s2 = float(resid @ resid) / dof if dof > 0 else np.nan
        try:
            cov = s2 * np.linalg.inv(X.T @ X)
            se = np.sqrt(np.diag(cov))
            tvals = coef / se
            pvals = 2 * (1 - stats.t.cdf(np.abs(tvals), dof))
        except np.linalg.LinAlgError:
            tvals = pvals = [np.nan] * 3
        blk = {
            "n": int(len(sub)),
            "spearman_accnet_vs_buy5d": {"rho": round(float(rho), 3), "p": round(float(prho), 4)},
            "ols": {
                "const": {"coef": round(float(coef[0]), 3)},
                "is_accum": {
                    "coef_pp": round(float(coef[1]), 3),
                    "t": round(float(tvals[1]), 2),
                    "p": round(float(pvals[1]), 4),
                },
                "log_buy5d": {
                    "coef_pp": round(float(coef[2]), 3),
                    "t": round(float(tvals[2]), 2),
                    "p": round(float(pvals[2]), 4),
                },
            },
        }
        # 分層：buy_5d 中位數上下各自比較
        med = sub["buy_5d"].median()
        for half, mask in (("buy5d_low", sub["buy_5d"] <= med), ("buy5d_high", sub["buy_5d"] > med)):
            h = sub[mask]
            blk[half] = group_compare(
                h.loc[h[f"grp_{tag}"] == "accum", "r_adj_pct"],
                h.loc[h[f"grp_{tag}"] == "churn", "r_adj_pct"],
                "accum", "churn",
            )
        rev[tag] = blk
        print(f"--- 口徑{tag.upper()} n={len(sub)} "
              f"spearman(acc_net,buy5d)={rho:.3f} p={prho:.4f}")
        print(f"    OLS is_accum coef={coef[1]:+.2f}pp t={tvals[1]:.2f} p={pvals[1]:.4f} | "
              f"log_buy5d coef={coef[2]:+.2f}pp t={tvals[2]:.2f} p={pvals[2]:.4f}")
    hc1["reverse_check_buy5d"] = rev

    # --- 關鍵反例：6449（協調者指定）
    section("§1d 關鍵反例 6449 逐筆分類")
    case = t[t["stock_id"].astype(str) == "6449"].copy()
    case_cols = [
        "signal_date", "entry_date", "exit_date", "r_adj_pct", "buy_5d", "net_ratio",
        "acc_buy_a", "acc_net_a", "acc_days_a", "grp_a",
        "acc_buy_b", "acc_net_b", "acc_days_b", "grp_b", "flip",
    ]
    if len(case):
        show = case[case_cols].copy()
        for c in ("buy_5d", "acc_buy_a", "acc_buy_b"):
            show[c] = (show[c] / 1e8).round(3)
        for c in ("acc_net_a", "acc_net_b", "net_ratio", "flip"):
            show[c] = show[c].round(3)
        pd.set_option("display.width", 240)
        pd.set_option("display.max_columns", 40)
        print(show.to_string(index=False))
        hc1["case_6449"] = json.loads(case[case_cols].to_json(orient="records"))
    else:
        print("  無 6449 事件")
        hc1["case_6449"] = []
    # 每檔股票層級的貢獻
    per_stock = (
        t.groupby("stock_id")["r_adj_pct"]
        .agg(["count", "mean", "median", "sum"])
        .sort_values("sum")
        .round(2)
    )
    print("\n--- 各股票貢獻（最差 8 / 最好 5）")
    print(per_stock.head(8).to_string())
    print(per_stock.tail(5).to_string())
    hc1["per_stock_contribution"] = json.loads(per_stock.to_json(orient="index"))
    summary["H_C1"] = hc1

    # ---------------------------------------------------------------- H-C2
    section("§2 H-C2 多席共識（live-faithful 5d net95 定義）")
    ev_stocks = sorted(trades["stock_id"].astype(str).unique())
    cand_seats: dict[str, str] = {}
    cand_seats.update(PEER_SEATS_MANUAL)
    cand_seats.update(pool)
    cand_seats.pop(TRADER_ID, None)
    excluded = {s: mm_excl[s] for s in list(cand_seats) if s in mm_excl}
    for s in excluded:
        cand_seats.pop(s, None)
    print(f"[PEER] 候選席位 {sorted(cand_seats)}  (MM排除掉: {excluded})")
    peer_tape = load_peer_tape(conn, ev_stocks, sorted(cand_seats))
    print(
        f"[PEER] tape rows={len(peer_tape)} "
        f"seats={peer_tape['seat'].nunique() if len(peer_tape) else 0} stocks={len(ev_stocks)}"
    )
    hc2: dict = {
        "candidate_seats": cand_seats,
        "mm_excluded_from_candidates": excluded,
        "multiseat_tape_end": MULTISEAT_TAPE_END,
    }

    ppanel = peer_5d_panel(peer_tape, cal, sorted(cand_seats))
    tr = trades.copy()
    tr["in_tape_window"] = tr["signal_date"] <= MULTISEAT_TAPE_END
    tr_w = tr[tr["in_tape_window"]].copy()
    print(f"[PEER] 可用共識檢定的事件 n={len(tr_w)} / {len(tr)} (signal_date <= {MULTISEAT_TAPE_END})")
    hc2["n_events_in_tape_window"] = int(len(tr_w))
    hc2["n_events_total"] = int(len(tr))

    if not ppanel.empty and len(tr_w):
        idx = ppanel.set_index(["seat", "stock_id", "trade_date"])
        # (a) 共現頻率
        co_rows = []
        for row in tr_w.itertuples(index=False):
            for seat in sorted(cand_seats):
                rec = idx.loc[(seat, str(row.stock_id), str(row.signal_date))] \
                    if (seat, str(row.stock_id), str(row.signal_date)) in idx.index else None
                if rec is None:
                    continue
                b5 = float(rec["buy_5d"]) if pd.notna(rec["buy_5d"]) else 0.0
                s5 = float(rec["sell_5d"]) if pd.notna(rec["sell_5d"]) else 0.0
                nr = float(rec["net_ratio"]) if pd.notna(rec["net_ratio"]) else np.nan
                snr = float(rec["sell_net_ratio"]) if pd.notna(rec["sell_net_ratio"]) else np.nan
                co_rows.append({
                    "signal_date": row.signal_date, "stock_id": row.stock_id, "seat": seat,
                    "buy_5d": b5, "sell_5d": s5, "net_ratio": nr, "sell_net_ratio": snr,
                    "strict_net95": bool(b5 >= BASE_FLOOR and nr >= BASE_NET),
                    "loose_buy": bool(b5 >= 0.2e8 and nr >= 0.50),
                    "any_netbuy": bool(b5 - s5 >= 0.1e8),
                    "netsell": bool(s5 >= 0.2e8 and snr >= 0.50),
                    "r_adj_pct": row.r_adj_pct,
                })
        co = pd.DataFrame(co_rows)
        hc2["cooccurrence"] = {}
        for lvl in ("strict_net95", "loose_buy", "any_netbuy", "netsell"):
            per_seat = co.groupby("seat")[lvl].sum().astype(int).to_dict()
            n_ev = int(co.groupby(["signal_date", "stock_id"])[lvl].any().sum())
            hc2["cooccurrence"][lvl] = {
                "events_with_any_seat": n_ev,
                "events_total": int(len(tr_w)),
                "rate_pct": round(100 * n_ev / max(len(tr_w), 1), 1),
                "per_seat_hits": per_seat,
            }
            print(f"  [{lvl}] 有任一席共現的事件 {n_ev}/{len(tr_w)} ({100*n_ev/max(len(tr_w),1):.1f}%)  per-seat={per_seat}")

        # (b) 有共識 vs 無共識
        hc2["consensus_split"] = {}
        for lvl in ("strict_net95", "loose_buy", "any_netbuy", "netsell"):
            flag = co.groupby(["signal_date", "stock_id"])[lvl].any().rename(lvl).reset_index()
            merged = tr_w.merge(flag, on=["signal_date", "stock_id"], how="left")
            merged[lvl] = merged[lvl].fillna(False)
            cmp_ = group_compare(
                merged.loc[merged[lvl], "r_adj_pct"],
                merged.loc[~merged[lvl], "r_adj_pct"],
                "with", "without",
            )
            hc2["consensus_split"][lvl] = cmp_
            print(f"\n  --- {lvl}")
            print(f"     with   : {cmp_['with']}")
            print(f"     without: {cmp_['without']}")
            for k in ("welch_p", "mwu_p", "diff_median_pp"):
                if k in cmp_:
                    print(f"     {k}={cmp_[k]}")
            for arm, mask in (("with", merged[lvl]), ("without", ~merged[lvl])):
                sub = merged[mask]
                if len(sub) >= 5:
                    p = run_perm(sub, bars, f"{lvl}_{arm}")
                    hc2["consensus_split"][lvl][f"perm_{arm}"] = p
                    print(f"     perm[{arm}] n={p['n_events']} p_mean={p['p_value_mean_onesided']:.4f} "
                          f"p_median={p['p_value_median_onesided']:.4f}")
        co.to_csv(OUT_DIR / f"{PREFIX}_consensus_pairs.csv", index=False)

        # (c) 全期共現基準率（不限事件日）：這些席位跟 9217 同日同股的整體重疊
        base_overlap = {}
        p9217 = panel[["stock_id", "trade_date", "buy_5d", "net_ratio"]].copy()
        p9217["trig"] = (
            (p9217["buy_5d"] >= BASE_FLOOR) & (p9217["net_ratio"] >= BASE_NET)
        ).fillna(False)
        for seat in sorted(cand_seats):
            sp = ppanel[ppanel["seat"] == seat]
            st = sp[(sp["buy_5d"] >= BASE_FLOOR) & (sp["net_ratio"] >= BASE_NET)]
            st = st[st["trade_date"] <= MULTISEAT_TAPE_END]
            base_overlap[seat] = int(len(st))
        hc2["peer_own_net95_stockdays_on_event_stocks"] = base_overlap
        print(f"\n  各席在事件股票上自己觸發 5d-net95 的 (股,日) 數（<= {MULTISEAT_TAPE_END}）: {base_overlap}")
    else:
        hc2["note"] = "peer panel 為空或無可用事件"

    summary["H_C2"] = hc2

    # ---------------------------------------------------------------- outputs
    events.to_csv(OUT_DIR / f"{PREFIX}_events.csv", index=False)
    t.to_csv(OUT_DIR / f"{PREFIX}_trades_labeled.csv", index=False)
    (OUT_DIR / f"{PREFIX}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
    )
    print(f"\n[OUT] {OUT_DIR / (PREFIX + '_summary.json')}")


if __name__ == "__main__":
    main()
