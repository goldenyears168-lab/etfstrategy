#!/usr/bin/env python3
"""M4：9217（凱基-松山）跟單 · Regime layer（環境層）條件化 H-D1 ＋ 標的品質濾網 H-D2.

母體＝scan_5d_net95 live 訊號（rolling 5 交易日 buy_5d>=0.5億 ∩ net_ratio>=0.95 ∩ !mega），
協議＝L1H7（T+1 開盤進 / 第7交易日收盤出 / 30bps / β=1.15 / bench IX0001）。
2026-08-17 補了 255 檔缺價股票，母體會在腳本內即時重算（不吃舊 CSV）。

H-D1：把事件依 Regime layer 各軸的 PIT 值分兩層（中位數或自然門檻），比較 L1H7。
      每一軸都報，含無效的；BH-FDR 校正。
H-D2：標的品質濾網（Minervini 階段 / Weinstein 階段 / 延伸度 / 流動性 / 股價 /
      產業集中度去重規則）。

所有特徵一律 PIT（訊號日 T 只能用 date<=T 的資料）。DB 唯讀。不寫 DB/config/.env。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m4_regime_quality_filters.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd

from stock_db import DEFAULT_DB_PATH
from stock_db.connection import connect_ro
from stage_analysis import (
    stage_series_daily,
    vectorized_minervini_criteria_count,
    vectorized_minervini_pass_pct,
)
from research.branch_signal_validation import build_l1h7_signal_dict, permutation_test

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
SCRIPTS = ROOT / "scripts" / "research"
SOURCE = "finmind"

STUDY_START = "2024-07-01"
STUDY_END = "2026-08-17"
PANEL_START = "2022-06-01"  # 需 200 日 MA + 252 日 52 週高低 → 往前多抓
N_PERM = 20_000
SEED = 20260817
RNG = np.random.default_rng(SEED)

# FinMind 官方分類裡屬於「電子複合體」的大類（用於較寬的族群定義）
ELEC_BUCKET = {
    "半導體業", "電子零組件業", "電子工業", "光電業", "電腦及週邊設備業",
    "通信網路業", "其他電子業", "電子通路業", "資訊服務業",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M9217 = _load_module("m9217", SCRIPTS / "study_whale_9217_5d_net95_live_signal_validation.py")
MGEN = _load_module("mgen", SCRIPTS / "study_whale_branch_5d_net95_live_signal_validation.py")


def section(t: str) -> None:
    print(f"\n{'=' * 96}\n{t}\n{'=' * 96}")


# ---------------------------------------------------------------------------
# 1) 母體重算
# ---------------------------------------------------------------------------
def build_base(conn):
    M9217.STUDY_START = MGEN.STUDY_START = STUDY_START
    M9217.STUDY_END = MGEN.STUDY_END = STUDY_END
    events, _grid = M9217.build_5d_net95_events(conn, MGEN.load_mega(MGEN.MEGA_PATH))
    trades, drop = MGEN.build_trades(conn, events)
    return trades, drop


# ---------------------------------------------------------------------------
# 2) PIT 特徵
# ---------------------------------------------------------------------------
def load_close_panel(conn) -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_sql_query(
        """
        SELECT stock_id, trade_date, close, amount
        FROM stock_daily_bars
        WHERE source=? AND trade_date BETWEEN ? AND ? AND close>0
        """,
        conn, params=(SOURCE, PANEL_START, STUDY_END),
    )
    close = df.pivot_table(index="trade_date", columns="stock_id", values="close", aggfunc="last")
    amount = df.pivot_table(index="trade_date", columns="stock_id", values="amount", aggfunc="last")
    close = close.sort_index()
    amount = amount.sort_index()
    return close, amount


def load_ix_ohlc(conn) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT date, open, high, low, close FROM daily_bars
        WHERE code='IX0001' AND date BETWEEN ? AND ? AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (PANEL_START, STUDY_END),
    ).fetchall()
    seen: dict[str, tuple] = {}
    for d, o, h, lo, c in rows:
        seen.setdefault(d, (o, h, lo, c))
    out = pd.DataFrame(
        [(d, *v) for d, v in sorted(seen.items())],
        columns=["date", "open", "high", "low", "close"],
    )
    for c in ("open", "high", "low"):
        out[c] = out[c].fillna(out["close"])
    return out


def market_features(conn, close: pd.DataFrame) -> pd.DataFrame:
    """所有市場層（Regime layer）PIT 特徵，index=trade_date。"""
    feats = pd.DataFrame(index=close.index)

    # R1 breadth: 收盤價在 MA200 之上的比例（分母只計 ma200 可算者）
    ma200 = close.rolling(200, min_periods=200).mean()
    valid = close.notna() & ma200.notna()
    feats["breadth_ma200_pct"] = (
        (close > ma200).where(valid).sum(axis=1) / valid.sum(axis=1).replace(0, np.nan) * 100
    )

    # R4 stage2 participation：Minervini 7 條（不含 RS）全過的比例
    feats["stage2_part_pct"] = vectorized_minervini_pass_pct(close, min_pass=7) * 100

    # R2/R3 大盤
    ix = load_ix_ohlc(conn)
    ixs = ix.set_index("date")["close"]
    ix_ma200 = ixs.rolling(200, min_periods=200).mean()
    feats["ix_above_ma200"] = (
        (ixs > ix_ma200).where(ix_ma200.notna()).astype("float").reindex(feats.index, method="ffill")
    )
    st = stage_series_daily(ix.rename(columns=str.lower))
    st["date"] = pd.to_datetime(st["date"]).dt.strftime("%Y-%m-%d")
    st = st.set_index("date").sort_index()
    feats["ix_stage"] = st["stage"].reindex(feats.index, method="ffill")
    feats["ix_extension_pct"] = st["extension_pct"].reindex(feats.index, method="ffill")

    # R5/R6 VIXTWN
    vix = pd.read_sql_query(
        "SELECT date, close FROM market_vix_daily WHERE symbol='VIXTWN' AND date BETWEEN ? AND ? ORDER BY date",
        conn, params=(PANEL_START, STUDY_END),
    ).groupby("date")["close"].last().sort_index()
    feats["vixtwn"] = vix.reindex(feats.index, method="ffill")
    feats["vixtwn_chg20_pct"] = (feats["vixtwn"] / feats["vixtwn"].shift(20) - 1) * 100

    # R7 外資台指期未平倉 z60（EOD · PIT）
    fut = pd.read_sql_query(
        """
        SELECT trade_date, net_oi_vol FROM futures_institutional_daily
        WHERE futures_id='TX' AND inst_name='外資' AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        conn, params=(PANEL_START, STUDY_END),
    ).groupby("trade_date")["net_oi_vol"].last().astype(float).sort_index()
    z = (fut - fut.rolling(60, min_periods=60).mean()) / fut.rolling(60, min_periods=60).std()
    feats["fut_foreign_oi_z60"] = z.reindex(feats.index, method="ffill")

    # R8 融資餘額（278 檔 panel 合計）20 日變化
    mg = pd.read_sql_query(
        """
        SELECT trade_date, SUM(margin_balance) AS bal
        FROM stock_margin_daily WHERE trade_date BETWEEN ? AND ? GROUP BY trade_date ORDER BY trade_date
        """,
        conn, params=(PANEL_START, STUDY_END),
    ).groupby("trade_date")["bal"].last().astype(float).sort_index()
    mg = mg.reindex(feats.index, method="ffill")
    feats["margin_chg20_pct"] = (mg / mg.shift(20) - 1) * 100
    return feats


def stock_features(close: pd.DataFrame, amount: pd.DataFrame, trades: pd.DataFrame,
                   conn) -> pd.DataFrame:
    """個股層 PIT 特徵（Minervini 條件數、Weinstein 日更階段、延伸度、流動性、股價）。"""
    crit = vectorized_minervini_criteria_count(close)
    adv20 = amount.rolling(20, min_periods=10).mean()
    ret60 = close / close.shift(60) - 1

    stage_cache: dict[str, pd.DataFrame] = {}
    rows = []
    for r in trades.itertuples(index=False):
        sid, d = r.stock_id, r.signal_date
        if sid not in stage_cache:
            bars = pd.read_sql_query(
                """
                SELECT trade_date AS date, open, high, low, close, volume FROM stock_daily_bars
                WHERE stock_id=? AND source=? AND trade_date<=? AND close>0 ORDER BY trade_date
                """,
                conn, params=(sid, SOURCE, STUDY_END),
            )
            s = stage_series_daily(bars)
            s["date"] = pd.to_datetime(s["date"]).dt.strftime("%Y-%m-%d")
            stage_cache[sid] = s.set_index("date")
        s = stage_cache[sid]
        srow = s.loc[d] if d in s.index else None
        rows.append({
            "stock_id": sid, "signal_date": d,
            "minervini_crit": float(crit.loc[d, sid]) if (d in crit.index and sid in crit.columns) else np.nan,
            "wstage": float(srow["stage"]) if srow is not None else np.nan,
            "extension_pct": float(srow["extension_pct"]) if srow is not None else np.nan,
            "ma_slope_pct": float(srow["ma_slope_pct"]) if srow is not None else np.nan,
            "adv20_amt": float(adv20.loc[d, sid]) if (d in adv20.index and sid in adv20.columns) else np.nan,
            "close_px": float(close.loc[d, sid]) if (d in close.index and sid in close.columns) else np.nan,
            "ret60_pct": float(ret60.loc[d, sid]) * 100 if (d in ret60.index and sid in ret60.columns) else np.nan,
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 3) 分層檢定
# ---------------------------------------------------------------------------
def layer_stats(vals: np.ndarray) -> dict:
    if len(vals) == 0:
        return {"n": 0, "mean_pct": None, "median_pct": None, "win_rate_pct": None}
    return {
        "n": int(len(vals)),
        "mean_pct": round(float(np.mean(vals)), 3),
        "median_pct": round(float(np.median(vals)), 3),
        "win_rate_pct": round(float((vals > 0).mean() * 100), 1),
    }


def perm_diff_test(vals: np.ndarray, mask: np.ndarray, n_perm: int = N_PERM) -> dict:
    """兩層平均數差的 label-permutation 檢定（雙尾）。"""
    hi, lo = vals[mask], vals[~mask]
    if len(hi) < 2 or len(lo) < 2:
        return {"p_mean_two_sided": None, "p_median_two_sided": None, "note": "layer too small"}
    obs_m = float(np.mean(hi) - np.mean(lo))
    obs_md = float(np.median(hi) - np.median(lo))
    n_hi = len(hi)
    rng = np.random.default_rng(SEED)
    cm = cmd = 0
    for _ in range(n_perm):
        idx = rng.permutation(len(vals))
        a, b = vals[idx[:n_hi]], vals[idx[n_hi:]]
        if abs(np.mean(a) - np.mean(b)) >= abs(obs_m) - 1e-12:
            cm += 1
        if abs(np.median(a) - np.median(b)) >= abs(obs_md) - 1e-12:
            cmd += 1
    return {
        "diff_mean_pp": round(obs_m, 3),
        "diff_median_pp": round(obs_md, 3),
        "p_mean_two_sided": round((cm + 1) / (n_perm + 1), 5),
        "p_median_two_sided": round((cmd + 1) / (n_perm + 1), 5),
    }


def bh_fdr(pairs: list[tuple[str, float]], alpha: float = 0.05) -> dict[str, float]:
    items = sorted([(k, p) for k, p in pairs if p is not None], key=lambda x: x[1])
    n = len(items)
    raw = [(k, p * n / (i + 1)) for i, (k, p) in enumerate(items)]
    out: dict[str, float] = {}
    run = float("inf")
    for k, q in reversed(raw):
        run = min(run, q)
        out[k] = round(min(run, 1.0), 5)
    return out


def run_axis(name: str, df: pd.DataFrame, col: str, *, kind: str = "median",
             threshold: float | None = None, hi_label: str = "high", lo_label: str = "low") -> dict:
    sub = df[df[col].notna()]
    n_missing = len(df) - len(sub)
    if len(sub) < 6:
        return {"axis": name, "column": col, "note": "資料不足", "n_missing": n_missing}
    if kind == "median":
        thr = float(sub[col].median())
    else:
        thr = float(threshold)
    mask = (sub[col] > thr).to_numpy() if kind == "median" else (sub[col] >= thr).to_numpy()
    vals = sub["r_adj_pct"].to_numpy()
    res = perm_diff_test(vals, mask)
    return {
        "axis": name, "column": col, "split": kind, "threshold": round(thr, 4),
        "n_missing": n_missing,
        f"layer_{hi_label}": layer_stats(vals[mask]),
        f"layer_{lo_label}": layer_stats(vals[~mask]),
        **res,
    }


# ---------------------------------------------------------------------------
# 4) 產業集中度 / 去重規則
# ---------------------------------------------------------------------------
def corr_clusters(close: pd.DataFrame, stocks: list[str], asof: str, thr: float = 0.6) -> dict[str, int]:
    """用 asof 之前 120 日日報酬相關係數做單連結分群（PIT）。"""
    sub = close.loc[close.index <= asof, [s for s in stocks if s in close.columns]].tail(121)
    rets = sub.pct_change().dropna(how="all")
    c = rets.corr()
    ids = list(c.columns)
    parent = {s: s for s in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            v = c.loc[a, b]
            if pd.notna(v) and v >= thr:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
    roots = {}
    out = {}
    for s in ids:
        r = find(s)
        out[s] = roots.setdefault(r, len(roots))
    return out


def apply_dedup(trades: pd.DataFrame, group_of: dict[str, str], gap_days: int,
                calendar: list[str]) -> pd.DataFrame:
    """規則：同族群在過去 gap_days 個交易日內已進場過，就不再進場。"""
    cal_idx = {d: i for i, d in enumerate(calendar)}
    last_taken: dict[str, int] = {}
    keep = []
    for r in trades.sort_values("signal_date").itertuples(index=False):
        g = group_of.get(r.stock_id, f"__{r.stock_id}")
        i = cal_idx.get(r.signal_date)
        if i is None:
            keep.append(True)
            continue
        prev = last_taken.get(g)
        if prev is not None and (i - prev) <= gap_days:
            keep.append(False)
            continue
        keep.append(True)
        last_taken[g] = i
    t = trades.sort_values("signal_date").copy()
    t["kept"] = keep
    return t


def random_prune_pvalue(vals: np.ndarray, n_keep: int, obs_mean: float,
                        n_perm: int = N_PERM) -> float:
    """隨機剪掉同樣筆數的 null：規則挑的子集是否優於隨機剪枝。"""
    rng = np.random.default_rng(SEED)
    cnt = 0
    for _ in range(n_perm):
        s = rng.choice(len(vals), size=n_keep, replace=False)
        if np.mean(vals[s]) >= obs_mean - 1e-12:
            cnt += 1
    return round((cnt + 1) / (n_perm + 1), 5)


# ---------------------------------------------------------------------------
def main() -> int:
    print(f"[INFO] DB(read-only) = {DEFAULT_DB_PATH}")
    conn = connect_ro(DEFAULT_DB_PATH)

    section("(0) 母體重算（scan_5d_net95 + L1H7，2026-08-17 補價後）")
    trades, drop = build_base(conn)
    print(json.dumps(drop, ensure_ascii=False))
    base = MGEN.full_stats(trades["r_adj_pct"], f"base_n{len(trades)}")
    print("[BASE]", json.dumps(base, ensure_ascii=False))
    vals_all = trades["r_adj_pct"].to_numpy()

    section("(1) 建 PIT 特徵面板")
    close, amount = load_close_panel(conn)
    print(f"[INFO] close panel {close.shape}  {close.index[0]} ~ {close.index[-1]}")
    mfeat = market_features(conn, close)
    sfeat = stock_features(close, amount, trades, conn)

    df = trades.merge(sfeat, on=["stock_id", "signal_date"], how="left")
    df = df.merge(mfeat.reset_index().rename(columns={"index": "trade_date"}),
                  left_on="signal_date", right_on="trade_date", how="left")

    imap = json.loads((OUT_DIR / "songshan_m4_industry_map.json").read_text(encoding="utf-8"))
    df["industry"] = df["stock_id"].map(imap["industry"]).fillna("未分類")
    df["stock_name"] = df["stock_id"].map(imap["name"]).fillna("")
    df["elec_bucket"] = np.where(df["industry"].isin(ELEC_BUCKET), "電子複合體", df["industry"])
    df.to_csv(OUT_DIR / "songshan_m4_event_features.csv", index=False)
    print(f"[OK] 特徵表 {df.shape} → songshan_m4_event_features.csv")
    print(df[["signal_date", "stock_id", "stock_name", "industry", "r_adj_pct"]].to_string(index=False))

    # ------------------- H-D1 -------------------
    section("(2) H-D1 · Regime layer（環境層）條件化 — 全部軸")
    axes = [
        ("A1 Breadth zone（>MA200 家數佔比）", "breadth_ma200_pct", "median", None, "breadth_hi", "breadth_lo"),
        ("A2 大盤 MA200 閘門（IX0001）", "ix_above_ma200", "threshold", 0.5, "above_ma200", "below_ma200"),
        ("A3 Trend posture（IX0001 日更 Weinstein 階段=2）", "ix_stage", "threshold", 2.0, "stage_ge2", "stage_lt2"),
        ("A4 Stage 2 participation（Minervini 7/7 佔比）", "stage2_part_pct", "median", None, "part_hi", "part_lo"),
        ("A5 VIXTWN 水位", "vixtwn", "median", None, "vix_hi", "vix_lo"),
        ("A6 VIXTWN 20 日變化", "vixtwn_chg20_pct", "median", None, "vix_rising", "vix_falling"),
        ("A7 外資台指期 OI z60（EOD）", "fut_foreign_oi_z60", "median", None, "z_hi", "z_lo"),
        ("A8 融資餘額 20 日變化（278 檔 panel）", "margin_chg20_pct", "median", None, "margin_up", "margin_dn"),
    ]
    hd1 = []
    for name, col, kind, thr, hi, lo in axes:
        r = run_axis(name, df, col, kind=kind, threshold=thr, hi_label=hi, lo_label=lo)
        hd1.append(r)
        print(json.dumps(r, ensure_ascii=False))
    q1 = bh_fdr([(r["axis"], r.get("p_mean_two_sided")) for r in hd1])
    for r in hd1:
        r["q_bh_mean"] = q1.get(r["axis"])

    # ------------------- H-D2 -------------------
    section("(3) H-D2 · 標的品質濾網")
    filters = [
        ("F1 Minervini 條件數（7 條全過）", "minervini_crit", "threshold", 7.0, "pass7", "fail"),
        ("F2 Minervini 條件數（中位數分層）", "minervini_crit", "median", None, "crit_hi", "crit_lo"),
        ("F3 Weinstein 日更階段=2（30W MA）", "wstage", "threshold", 2.0, "wstage_ge2", "wstage_lt2"),
        ("F4 延伸度 extension_pct（離 30W MA）", "extension_pct", "median", None, "ext_hi", "ext_lo"),
        ("F5 20 日均額 ADV（流動性）", "adv20_amt", "median", None, "adv_hi", "adv_lo"),
        ("F6 訊號日股價", "close_px", "median", None, "px_hi", "px_lo"),
        ("F7 前 60 日報酬（已漲多少）", "ret60_pct", "median", None, "ret60_hi", "ret60_lo"),
    ]
    hd2 = []
    for name, col, kind, thr, hi, lo in filters:
        r = run_axis(name, df, col, kind=kind, threshold=thr, hi_label=hi, lo_label=lo)
        hd2.append(r)
        print(json.dumps(r, ensure_ascii=False))
    q2 = bh_fdr([(r["axis"], r.get("p_mean_two_sided")) for r in hd2])
    for r in hd2:
        r["q_bh_mean"] = q2.get(r["axis"])

    # 合併家族 BH-FDR
    qall = bh_fdr([(r["axis"], r.get("p_mean_two_sided")) for r in hd1 + hd2])
    for r in hd1 + hd2:
        r["q_bh_mean_combined"] = qall.get(r["axis"])

    # ------------------- 產業集中度 -------------------
    section("(4) 產業集中度與去重規則")
    calendar = MGEN.load_calendar(conn, STUDY_START, STUDY_END)
    conc = {}
    for key in ("stock_id", "industry", "elec_bucket"):
        vc = df[key].value_counts()
        share = vc / vc.sum()
        conc[key] = {
            "n_groups": int(len(vc)),
            "hhi": round(float((share ** 2).sum()), 4),
            "top1": [str(vc.index[0]), int(vc.iloc[0]), round(float(share.iloc[0]) * 100, 1)],
            "top3_share_pct": round(float(share.head(3).sum()) * 100, 1),
            "counts": {str(k): int(v) for k, v in vc.items()},
        }
        print(key, json.dumps(conc[key], ensure_ascii=False))

    # 產業別績效
    ind_perf = []
    for ind, sub in df.groupby("industry"):
        ind_perf.append({"industry": ind, **layer_stats(sub["r_adj_pct"].to_numpy())})
    ind_perf.sort(key=lambda x: -x["n"])
    print("\n[產業別 L1H7]")
    print(pd.DataFrame(ind_perf).to_string(index=False))

    # 相關係數分群（用全窗最後一日的 PIT 視角作描述；規則模擬用逐事件 PIT）
    stocks = sorted(df["stock_id"].unique())
    cc = corr_clusters(close, stocks, STUDY_END, thr=0.6)
    df["corr_cluster"] = df["stock_id"].map(lambda s: f"C{cc.get(s, -1)}")
    cl = df.groupby("corr_cluster")["stock_id"].apply(lambda s: sorted(set(s))).to_dict()
    print("\n[120 日報酬相關 >=0.6 單連結分群]")
    for k, v in sorted(cl.items()):
        if len(v) > 1:
            print(" ", k, [f"{s}{imap['name'].get(s, '')}" for s in v])

    dedup = []
    group_defs = {
        "same_stock": {s: s for s in stocks},
        "official_industry": {s: imap["industry"].get(s, "未分類") for s in stocks},
        "elec_bucket": {s: ("電子複合體" if imap["industry"].get(s) in ELEC_BUCKET
                            else imap["industry"].get(s, "未分類")) for s in stocks},
        "corr_cluster": {s: f"C{cc.get(s, -1)}" for s in stocks},
    }
    for gname, gmap in group_defs.items():
        for gap in (7, 10, 20):
            t = apply_dedup(df, gmap, gap, calendar)
            kept = t[t["kept"]]["r_adj_pct"].to_numpy()
            dropped = t[~t["kept"]]["r_adj_pct"].to_numpy()
            if len(kept) == 0 or len(kept) == len(t):
                p = None
            else:
                p = random_prune_pvalue(vals_all, len(kept), float(np.mean(kept)))
            rec = {
                "group": gname, "gap_trading_days": gap,
                "kept": layer_stats(kept), "dropped": layer_stats(dropped),
                "p_vs_random_prune": p,
                "dropped_events": [
                    f"{r.stock_id}@{r.signal_date}:{r.r_adj_pct}"
                    for r in t[~t["kept"]].itertuples(index=False)
                ],
            }
            dedup.append(rec)
            print(json.dumps({k: v for k, v in rec.items() if k != "dropped_events"}, ensure_ascii=False))
    q3 = bh_fdr([(f"{r['group']}_gap{r['gap_trading_days']}", r["p_vs_random_prune"]) for r in dedup])
    for r in dedup:
        r["q_bh"] = q3.get(f"{r['group']}_gap{r['gap_trading_days']}")

    # ------------------- 6449 專項 -------------------
    section("(5) 6449 鈺邦 4 筆事件在各軸落點")
    cols = ["signal_date", "stock_id", "r_adj_pct", "close_px", "minervini_crit", "wstage",
            "extension_pct", "ret60_pct", "adv20_amt", "breadth_ma200_pct", "stage2_part_pct",
            "ix_stage", "ix_above_ma200", "vixtwn", "vixtwn_chg20_pct", "fut_foreign_oi_z60",
            "margin_chg20_pct"]
    focus = df[df["stock_id"].isin(["6449", "2337", "6271", "2492"])][cols]
    print(focus.to_string(index=False))
    print("\n[全母體各特徵中位數]")
    print(df[[c for c in cols if c not in ("signal_date", "stock_id")]].median().to_string())

    # ------------------- 組合濾網 -------------------
    section("(6) 組合濾網（在最有訊號的幾道上疊加）")
    combos = {
        "mega_only(baseline)": pd.Series(True, index=df.index),
        "F1 minervini7": df["minervini_crit"] >= 7,
        "F4 ext<=median": df["extension_pct"] <= df["extension_pct"].median(),
        "F6 px<=median": df["close_px"] <= df["close_px"].median(),
        "F1+F4": (df["minervini_crit"] >= 7) & (df["extension_pct"] <= df["extension_pct"].median()),
        "F1+F6": (df["minervini_crit"] >= 7) & (df["close_px"] <= df["close_px"].median()),
        "px<=150": df["close_px"] <= 150,
        "px<=200": df["close_px"] <= 200,
        "ext<=20pct": df["extension_pct"] <= 20,
        "ext<=30pct": df["extension_pct"] <= 30,
    }
    combo_rows = []
    for name, m in combos.items():
        m = m.fillna(False)
        v = df.loc[m, "r_adj_pct"].to_numpy()
        keeps_6449 = int(((df["stock_id"] == "6449") & m).sum())
        rec = {"filter": name, **layer_stats(v),
               "kept_pct": round(float(m.mean()) * 100, 1),
               "n_6449_kept": keeps_6449}
        if 0 < len(v) < len(df):
            rec["p_vs_random_prune"] = random_prune_pvalue(vals_all, len(v), float(np.mean(v)))
        combo_rows.append(rec)
    combo_df = pd.DataFrame(combo_rows)
    print(combo_df.to_string(index=False))
    combo_df.to_csv(OUT_DIR / "songshan_m4_combo_filters.csv", index=False)

    # ------------------- 存檔 -------------------
    payload = {
        "generated_for": "songshan 9217 · H-D1 regime conditioning + H-D2 quality filters",
        "study_window": f"{STUDY_START}..{STUDY_END}",
        "protocol": "scan_5d_net95 rising-edge → L1H7 (T+1 open / H7 close / 30bps / beta=1.15 / IX0001)",
        "base": base,
        "drop_stats": drop,
        "hd1_regime_axes": hd1,
        "hd2_quality_filters": hd2,
        "concentration": conc,
        "industry_perf": ind_perf,
        "corr_clusters": {k: v for k, v in cl.items()},
        "dedup_rules": dedup,
        "combo_filters": combo_rows,
        "n_perm": N_PERM,
        "seed": SEED,
    }
    (OUT_DIR / "songshan_m4_regime_quality_summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n[OK] → {OUT_DIR / 'songshan_m4_regime_quality_summary.json'}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
