#!/usr/bin/env python3
"""H-9217-POSITION-MOMENTUM · 9217（凱基-松山）既有持倉「消長動能」→未來40交易日(~2個月)前瞻力.

背景（見 MEMORY「Multiscale Signal Research」「Branch footprint expanded screen」教訓）：
  * 先前 9A81「安靜建倉」研究用「事後最高點」定義報酬代理指標，被證實是徹底的事後諸葛幻覺
    （因果重寫後 18 組參數 16 組中位數翻負）。這支腳本從頭到尾只用「訊號當天以前（含當天）」
    的資料做任何判斷，訊號之後的資料只用在事後算 forward return，不回頭去挑「最佳」窗口/門檻。
  * 這次問的不是「有沒有新訊號」，是「9217 對某檔股票既有的淨部位，最近是加速加碼、持平、
    還是開始減碼，這個動能本身能不能預測接下來 40 個交易日的股價表現」。

方法（完全因果）：
  對 9217 有效交易過的每一檔股票（歷史活躍天數 >= MIN_ACTIVE_DAYS，排除 mega 權值股與 00* ETF），
  在該股票自己的完整交易日曆上：
    cum_net(t)      = 截至 t（含）為止的累積淨買超股數（分點淨部位，因果）
    short_chg(t)    = cum_net(t) - cum_net(t-SW)   （近 SW=20 個交易日部位變化量）
    long_chg(t)     = cum_net(t) - cum_net(t-LW)   （近 LW=60 個交易日部位變化量，僅供診斷）
    z(t)            = ( short_chg(t) - hist_mean(short_chg 在 t 之前的歷史) )
                       / hist_std(short_chg 在 t 之前的歷史)
                      hist_mean/std 用 expanding()，而且整個序列先 shift(1) 才拿來當 t 的基準，
                      確保 t 當天的基準完全不包含 t 當天的資訊。
  分類（每個交易日一個狀態，只用 t 及之前資訊）：
    "accel"  (加速加碼) : z(t) >= Z_THRESH  且 cum_net(t) > 0
    "reduce" (開始減碼) : z(t) <= -Z_THRESH 且 cum_net(t-SW) > 0 （SW 天前確實有淨多頭部位才算「減碼」，
                          避免把「原本就沒部位、持續小賣」誤判成減碼訊號）
    否則 "flat" (持平)

  訊號日 = 狀態由非該類轉為該類的第一天（rising edge），同一 (股票, 訊號類型, 參數組合) 兩次訊號
  之間至少間隔 DEDUP_GAP=15 個交易日。

  對每個訊號日套用嚴謹協議：次日開盤進場、第 H 個交易日(H=40 主要，20 次要)收盤出場，
  30bps 成本，β=1.15 用 IX0001 調整（build_hold_lookup 邏輯沿用
  scripts/research/study_whale_9A81_forward_tradeable.py）。

  參數穩健性：(SW,LW) in [(20,60),(10,40)] × Z_THRESH in [0.75,1.0,1.5]，共 6 組合，避免像 9A81
  舊研究那樣只挑一組好看的參數。

對照組：既有 live 規則（songshan-copytrade：滾動5日 buy_5d>=5000萬 且 net_ratio>=0.95，
  見 src/order/songshan_copytrade_config.py），直接讀取已生成的
  whale_9217_5dnet95_events.csv（study_whale_9217_5d_net95_live_signal_validation.py 產出），
  用同一套 H=40/H=20 forward-return 管線重算，跟本研究的動能訊號在同一 horizon 上比較。

現況快照：9217 目前（最新可得資料日）每一筆 net_shares>0 的持倉，用移動加權平均成本法(moving-
  average cost)算截至目前為止的持倉成本（因果，只用歷史買進），以及該股票在最新一天的動能分類。

用法（必須在 mini 上跑，DB 在 mini 是新的、9217 本身資料回溯到 2021）：
  PYTHONPATH=src .venv/bin/python scripts/research/study_whale_9217_position_momentum_forward.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import DEFAULT_DB_PATH, connect

SOURCE = "finmind"
TRADER_ID = "9217"
COST = 0.003
BETA = 1.15
BENCH_CODE = "IX0001"
HORIZONS = [40, 20]  # 40 = 主要 (~2個月)；20 供對照
MIN_ACTIVE_DAYS = 40  # 分點-股票配對至少要有這麼多出手日，才進入動能訊號宇宙
WINDOW_PAIRS = [(20, 60), (10, 40)]  # (SW, LW)
Z_THRESH_SET = [0.75, 1.0, 1.5]
Z_HIST_MIN = 40  # 至少要有這麼多筆歷史 short_chg 樣本才開始算 z-score（避免用不穩定的早期統計量）
DEDUP_GAP = 15  # 交易日，同一(股票,訊號類型,參數)之間至少間隔這麼多天
N_BOOTSTRAP = 5000
RNG_SEED = 20260728
MIN_HOLD_SHARES_REPORT = 1000  # 現況持倉報表門檻

MATERIALITY_THRESHOLDS_NTD = [10_000_000.0, 20_000_000.0, 50_000_000.0]

MEGA_BLACKLIST_PATH = (
    ROOT / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
)
EXISTING_RULE_EVENTS_CSV = (
    ROOT / "reports/research/branch-footprint-screen/whale_9217_5dnet95_events.csv"
)
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
PREFIX = "whale_9217_posmom_"


def section(title: str) -> None:
    print(f"\n{'='*90}\n{title}\n{'='*90}")


# ---------------------------------------------------------------------------
# 1) 資料載入
# ---------------------------------------------------------------------------

def load_mega() -> set[str]:
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(str(s) for s in data.get("symbols", []))


def load_branch_all(conn) -> pd.DataFrame:
    """9217 全歷史逐日 buy/sell/net（不限日期，9217 資料回溯到 2021）。"""
    q = """
        SELECT trade_date, stock_id, buy, sell, net
        FROM stock_broker_branch_daily
        WHERE source = ? AND securities_trader_id = ?
        ORDER BY trade_date
    """
    df = pd.read_sql_query(q, conn, params=(SOURCE, TRADER_ID))
    return df


def load_ix(conn) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code=? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (BENCH_CODE,),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for d, o, c in rows:
        out.setdefault(d, (float(o), float(c)))
    dates = sorted(out)
    return pd.DataFrame(
        {"date": dates, "open": [out[d][0] for d in dates], "close": [out[d][1] for d in dates]}
    )


def load_stock_bars(conn, stock_ids: list[str]) -> pd.DataFrame:
    placeholders = ",".join("?" * len(stock_ids))
    q = f"""
        SELECT stock_id, trade_date, open, close
        FROM stock_daily_bars
        WHERE source='{SOURCE}' AND stock_id IN ({placeholders}) AND close > 0
        ORDER BY stock_id, trade_date
    """
    df = pd.read_sql_query(q, conn, params=stock_ids)
    df["open"] = df["open"].where(df["open"] > 0, df["close"])
    return df


# ---------------------------------------------------------------------------
# 2) 因果動能分類
# ---------------------------------------------------------------------------

def compute_momentum_states(
    bars: pd.DataFrame, branch: pd.DataFrame, sw: int, lw: int, z_thresh: float
) -> pd.DataFrame:
    """對單一股票，回傳其完整交易日曆上逐日的 cum_net / z / accel|reduce|flat 狀態。全因果。"""
    g = bars.sort_values("trade_date").reset_index(drop=True)
    b = branch.set_index("trade_date")[["buy", "sell", "net"]]
    b = b.reindex(g["trade_date"]).fillna(0.0).reset_index(drop=True)
    net = b["net"].to_numpy(dtype=float)
    buy = b["buy"].to_numpy(dtype=float)
    sell = b["sell"].to_numpy(dtype=float)

    cum_net = pd.Series(net).cumsum()
    short_chg = cum_net - cum_net.shift(sw)
    long_chg = cum_net - cum_net.shift(lw)

    hist_mean_raw = short_chg.expanding(min_periods=Z_HIST_MIN).mean()
    hist_std_raw = short_chg.expanding(min_periods=Z_HIST_MIN).std()
    hist_mean = hist_mean_raw.shift(1)  # 只用 t-1 及之前的 short_chg 分布
    hist_std = hist_std_raw.shift(1)
    z = (short_chg - hist_mean) / hist_std.replace(0, np.nan)

    had_position_sw_ago = cum_net.shift(sw) > 0
    state = pd.Series(np.full(len(g), "flat", dtype=object))
    accel_mask = (z >= z_thresh) & (cum_net > 0)
    reduce_mask = (z <= -z_thresh) & had_position_sw_ago
    state[accel_mask.fillna(False)] = "accel"
    state[reduce_mask.fillna(False) & ~accel_mask.fillna(False)] = "reduce"

    out = pd.DataFrame(
        {
            "trade_date": g["trade_date"],
            "cum_net": cum_net,
            "short_chg": short_chg,
            "long_chg": long_chg,
            "z": z,
            "state": state,
            "buy": buy,
            "sell": sell,
            "net": net,
            "close": g["close"],
        }
    )
    return out


def extract_rising_edges(diag: pd.DataFrame, state_name: str) -> list[int]:
    events = []
    last_i = None
    prev = False
    states = diag["state"].to_numpy()
    for i, s in enumerate(states):
        is_state = s == state_name
        rising = is_state and not prev
        if rising and (last_i is None or (i - last_i) >= DEDUP_GAP):
            events.append(i)
            last_i = i
        prev = is_state
    return events


# ---------------------------------------------------------------------------
# 3) 前瞻回測（因果：訊號日 t 只用 t 及之前資訊；報酬用 t 之後的資料，但不回頭挑訊號）
# ---------------------------------------------------------------------------

def build_hold_lookup(dates: np.ndarray, opens: np.ndarray, closes: np.ndarray, hold: int) -> pd.DataFrame:
    n = len(dates)
    rows = []
    for sig_i in range(n - 1):
        entry_i = sig_i + 1
        exit_i = entry_i + hold - 1
        if exit_i >= n:
            continue
        rows.append((dates[sig_i], dates[entry_i], opens[entry_i], dates[exit_i], closes[exit_i]))
    return pd.DataFrame(rows, columns=["signal_date", "entry_date", "entry_open", "exit_date", "exit_close"])


def bootstrap_ci(vals: np.ndarray, stat_fn, n_boot=N_BOOTSTRAP, seed=RNG_SEED) -> tuple[float, float]:
    if len(vals) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    n = len(vals)
    boots = np.empty(n_boot)
    for i in range(n_boot):
        sample = vals[rng.integers(0, n, n)]
        boots[i] = stat_fn(sample)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return float(lo), float(hi)


def full_stats(vals_pct: pd.Series, label: str) -> dict:
    vals = vals_pct.dropna().to_numpy() / 100.0
    n = len(vals)
    out = {"label": label, "n": n}
    if n < 2 or np.std(vals) == 0:
        out.update(
            mean_pct=round(float(np.mean(vals)) * 100, 3) if n else None,
            median_pct=round(float(np.median(vals)) * 100, 3) if n else None,
            win_rate_pct=round(float((vals > 0).mean()) * 100, 1) if n else None,
            t_stat=None, t_p=None, wilcoxon_stat=None, wilcoxon_p=None,
            mean_ci95_pct=None, median_ci95_pct=None,
        )
        return out
    t_stat, t_p = stats.ttest_1samp(vals, 0)
    try:
        w_stat, w_p = stats.wilcoxon(vals)
    except ValueError:
        w_stat, w_p = None, None
    mean_lo, mean_hi = bootstrap_ci(vals, np.mean)
    med_lo, med_hi = bootstrap_ci(vals, np.median)
    out.update(
        mean_pct=round(float(np.mean(vals)) * 100, 3),
        median_pct=round(float(np.median(vals)) * 100, 3),
        win_rate_pct=round(float((vals > 0).mean()) * 100, 1),
        t_stat=round(float(t_stat), 3),
        t_p=round(float(t_p), 4),
        wilcoxon_stat=round(float(w_stat), 3) if w_stat is not None else None,
        wilcoxon_p=round(float(w_p), 4) if w_p is not None else None,
        mean_ci95_pct=[round(mean_lo * 100, 3), round(mean_hi * 100, 3)],
        median_ci95_pct=[round(med_lo * 100, 3), round(med_hi * 100, 3)],
    )
    return out


# ---------------------------------------------------------------------------
# 4) 現況持倉快照（移動加權平均成本法，因果）
# ---------------------------------------------------------------------------

def reindex_branch_on_bars(g_all: pd.DataFrame, br: pd.DataFrame) -> pd.DataFrame | None:
    """把 (buy,sell,net) 對齊到某股票自己的完整交易日曆 g_all 上（缺的日子填0）。
    若 g_all 為空（該股票沒有可用的 OHLC bars，例如下市或代碼不在 stock_daily_bars），回傳 None。"""
    if g_all.empty:
        return None
    b = br.set_index("trade_date")[["buy", "sell", "net"]]
    b = b.reindex(g_all["trade_date"]).fillna(0.0).reset_index()
    b["close"] = g_all["close"].to_numpy()
    return b


def moving_avg_cost_snapshot(g: pd.DataFrame) -> tuple[float, float]:
    """對單一股票已依日期排序的 (buy,sell,close) 序列，算移動加權平均成本法下，
    走到最後一天為止的剩餘部位與平均成本。純因果（逐日往前滾動，不看未來）。"""
    position = 0.0
    avg_cost = 0.0
    for buy, sell, close in zip(g["buy"].to_numpy(), g["sell"].to_numpy(), g["close"].to_numpy()):
        if buy > 0:
            new_position = position + buy
            if new_position > 0:
                avg_cost = (avg_cost * position + buy * close) / new_position
            position = new_position
        if sell > 0:
            position -= sell
            if position <= 0:
                avg_cost = 0.0
                position = max(position, 0.0)
    return position, avg_cost


def main() -> int:
    t0 = time.time()
    conn = connect(DEFAULT_DB_PATH)
    mega = load_mega()
    print(f"[INFO] mega/權值股排除清單 n={len(mega)}")

    section("(1) 載入 9217 全歷史逐股 buy/sell/net")
    branch_all = load_branch_all(conn)
    branch_all["trade_date"] = branch_all["trade_date"].astype(str)
    print(
        f"[INFO] 9217 全歷史列數={len(branch_all):,}, "
        f"日期範圍 {branch_all['trade_date'].min()} ~ {branch_all['trade_date'].max()}, "
        f"涉及股票數={branch_all['stock_id'].nunique()}"
    )

    active_days = (
        branch_all[(branch_all["buy"] > 0) | (branch_all["sell"] > 0)]
        .groupby("stock_id").size().rename("active_days")
    )
    universe = sorted(active_days[active_days >= MIN_ACTIVE_DAYS].index.tolist())
    universe = [s for s in universe if s not in mega and not s.startswith("00")]
    print(f"[INFO] 動能訊號宇宙（活躍天數>={MIN_ACTIVE_DAYS}, 排除mega/00*ETF）: {len(universe)} 檔股票")

    section("(2) 載入宇宙內股票的完整 OHLC + IX0001 benchmark")
    bars_all = load_stock_bars(conn, universe)
    ix = load_ix(conn)
    print(f"[INFO] bars rows={len(bars_all):,}, elapsed={time.time()-t0:.1f}s")

    ix_lookups = {h: build_hold_lookup(ix["date"].to_numpy(), ix["open"].to_numpy(), ix["close"].to_numpy(), h).set_index("signal_date") for h in HORIZONS}

    section("(3) 逐股計算因果動能狀態 + rising-edge 訊號 + forward-return lookup")
    t1 = time.time()
    stock_hold_lookups: dict[str, dict[int, pd.DataFrame]] = {}
    all_signal_rows = []
    diag_last_row: dict[str, pd.DataFrame] = {}  # 供現況快照使用（存(SW,LW)=(20,60), Z=1.0 的最後一天狀態)
    materiality_rows = []  # 每檔股票歷史上「淨部位市值」曾經達到的最大絕對值（NTD），用來區分
    # 「真正大額建倉」vs「分點日常大量小額進出的雜訊股票」（見 (5.5) 對照）

    for sid in universe:
        g = bars_all[bars_all["stock_id"] == sid].sort_values("trade_date").reset_index(drop=True)
        if len(g) < 2 * MIN_ACTIVE_DAYS:
            continue
        br = branch_all[branch_all["stock_id"] == sid][["trade_date", "buy", "sell", "net"]]

        aligned = reindex_branch_on_bars(g, br)
        if aligned is not None:
            value_series = aligned["net"].cumsum() * aligned["close"]
            materiality_rows.append({"stock_id": sid, "max_abs_value_ntd": float(value_series.abs().max())})

        lookups = {}
        for h in HORIZONS:
            lk = build_hold_lookup(g["trade_date"].to_numpy(), g["open"].to_numpy(), g["close"].to_numpy(), h)
            if not lk.empty:
                lookups[h] = lk.set_index("signal_date")
        stock_hold_lookups[sid] = lookups

        for sw, lw in WINDOW_PAIRS:
            for z_thresh in Z_THRESH_SET:
                diag = compute_momentum_states(g, br, sw, lw, z_thresh)
                if sw == 20 and lw == 60 and z_thresh == 1.0:
                    diag_last_row[sid] = diag.iloc[[-1]].copy()
                for state_name in ("accel", "reduce"):
                    idxs = extract_rising_edges(diag, state_name)
                    for i in idxs:
                        row = diag.iloc[i]
                        all_signal_rows.append(
                            {
                                "stock_id": sid,
                                "sw": sw,
                                "lw": lw,
                                "z_thresh": z_thresh,
                                "signal_type": state_name,
                                "signal_date": row["trade_date"],
                                "z": row["z"],
                                "cum_net": row["cum_net"],
                                "short_chg": row["short_chg"],
                                "long_chg": row["long_chg"],
                            }
                        )
    signals_df = pd.DataFrame(all_signal_rows)
    print(f"[INFO] 全部 (參數組合) rising-edge 訊號事件數 = {len(signals_df):,}, elapsed={time.time()-t1:.1f}s")
    signals_df.to_csv(OUT_DIR / f"{PREFIX}signal_events_raw.csv", index=False)

    section("(4) 掛上 forward return")
    t2 = time.time()
    records = []
    for row in signals_df.itertuples(index=False):
        sid = row.stock_id
        sid_lookups = stock_hold_lookups.get(sid)
        if not sid_lookups:
            continue
        rec = row._asdict()
        ok = True
        for h in HORIZONS:
            lk = sid_lookups.get(h)
            ixlk = ix_lookups.get(h)
            if lk is None or row.signal_date not in lk.index or row.signal_date not in ixlk.index:
                ok = False
                break
        if not ok:
            continue
        for h in HORIZONS:
            lk = sid_lookups[h].loc[row.signal_date]
            ixlk = ix_lookups[h].loc[row.signal_date]
            r_s = lk["exit_close"] / lk["entry_open"] - 1 - COST
            r_ix = ixlk["exit_close"] / ixlk["entry_open"] - 1
            r_adj = r_s - BETA * r_ix
            rec[f"h{h}_entry_date"] = lk["entry_date"]
            rec[f"h{h}_exit_date"] = lk["exit_date"]
            rec[f"h{h}_r_pct"] = r_s * 100
            rec[f"h{h}_r_adj_pct"] = r_adj * 100
        records.append(rec)
    events_final = pd.DataFrame(records)
    print(f"[INFO] 有完整 forward-return 的訊號事件數 = {len(events_final):,}, elapsed={time.time()-t2:.1f}s")
    events_final.to_csv(OUT_DIR / f"{PREFIX}signal_events_with_returns.csv", index=False)

    section("(5) 統計檢定：每個(參數組合,訊號類型,horizon) — all events 版 + 每股僅取第一筆版")
    rng = np.random.default_rng(RNG_SEED)
    summary_rows = []
    for (sw, lw, z_thresh, sig_type), sub in events_final.groupby(["sw", "lw", "z_thresh", "signal_type"]):
        first_only = sub.sort_values("signal_date").drop_duplicates("stock_id", keep="first")
        for h in HORIZONS:
            col = f"h{h}_r_adj_pct"
            s_all = full_stats(sub[col], f"SW{sw}LW{lw}_Z{z_thresh}_{sig_type}_H{h}_all")
            s_first = full_stats(first_only[col], f"SW{sw}LW{lw}_Z{z_thresh}_{sig_type}_H{h}_firstpersstock")
            s_all.update(sw=sw, lw=lw, z_thresh=z_thresh, signal_type=sig_type, horizon=h, sample="all_events")
            s_first.update(sw=sw, lw=lw, z_thresh=z_thresh, signal_type=sig_type, horizon=h, sample="first_per_stock")
            s_all["n_stocks"] = sub["stock_id"].nunique()
            s_first["n_stocks"] = first_only["stock_id"].nunique()
            summary_rows.append(s_all)
            summary_rows.append(s_first)
    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(OUT_DIR / f"{PREFIX}summary_stats.csv", index=False)
    print(
        summary_df[
            ["sw", "lw", "z_thresh", "signal_type", "horizon", "sample", "n", "n_stocks",
             "mean_pct", "median_pct", "win_rate_pct", "t_p", "wilcoxon_p"]
        ].to_string()
    )

    section("(5.5) 材質性(materiality)敏感度：把訊號宇宙限縮到「歷史上部位市值曾達門檻」的股票")
    materiality_df = pd.DataFrame(materiality_rows)
    materiality_df.to_csv(OUT_DIR / f"{PREFIX}stock_materiality.csv", index=False)
    print(f"[INFO] 訊號宇宙 {len(materiality_df)} 檔股票，歷史最大絕對部位市值(NTD)分布：")
    print(materiality_df["max_abs_value_ntd"].describe())
    material_summary_rows = []
    for thresh in MATERIALITY_THRESHOLDS_NTD:
        material_set = set(materiality_df.loc[materiality_df["max_abs_value_ntd"] >= thresh, "stock_id"])
        sub_all = events_final[
            (events_final["sw"] == 20) & (events_final["lw"] == 60) & (events_final["z_thresh"] == 1.0)
            & (events_final["stock_id"].isin(material_set))
        ]
        for sig_type in ("accel", "reduce"):
            sub = sub_all[sub_all["signal_type"] == sig_type]
            first_only = sub.sort_values("signal_date").drop_duplicates("stock_id", keep="first")
            for h in HORIZONS:
                col = f"h{h}_r_adj_pct"
                s_all = full_stats(sub[col], f"material>={thresh/1e6:.0f}M_{sig_type}_H{h}_all")
                s_first = full_stats(first_only[col], f"material>={thresh/1e6:.0f}M_{sig_type}_H{h}_first")
                s_all.update(materiality_threshold_ntd=thresh, n_stocks_in_material_set=len(material_set),
                              signal_type=sig_type, horizon=h, sample="all_events", n_stocks=sub["stock_id"].nunique())
                s_first.update(materiality_threshold_ntd=thresh, n_stocks_in_material_set=len(material_set),
                                signal_type=sig_type, horizon=h, sample="first_per_stock", n_stocks=first_only["stock_id"].nunique())
                material_summary_rows.append(s_all)
                material_summary_rows.append(s_first)
    material_summary_df = pd.DataFrame(material_summary_rows)
    material_summary_df.to_csv(OUT_DIR / f"{PREFIX}materiality_summary_stats.csv", index=False)
    print(
        material_summary_df[
            ["materiality_threshold_ntd", "n_stocks_in_material_set", "signal_type", "horizon", "sample",
             "n", "n_stocks", "mean_pct", "median_pct", "win_rate_pct", "t_p", "wilcoxon_p"]
        ].to_string()
    )

    section("(6) 對照組：既有 live 規則（5日 buy>=5000萬 & net_ratio>=0.95）同 horizon 比較")
    if EXISTING_RULE_EVENTS_CSV.exists():
        existing = pd.read_csv(EXISTING_RULE_EVENTS_CSV, dtype={"stock_id": str})
        existing_recs = []
        for row in existing.itertuples(index=False):
            sid = row.stock_id
            sid_lookups = stock_hold_lookups.get(sid)
            if sid_lookups is None:
                # 該股票不在動能訊號宇宙（活躍天數<40），臨時抓它的 bars
                extra_bars = load_stock_bars(conn, [sid])
                if extra_bars.empty:
                    continue
                extra_bars = extra_bars.sort_values("trade_date").reset_index(drop=True)
                sid_lookups = {}
                for h in HORIZONS:
                    lk = build_hold_lookup(
                        extra_bars["trade_date"].to_numpy(), extra_bars["open"].to_numpy(),
                        extra_bars["close"].to_numpy(), h,
                    )
                    if not lk.empty:
                        sid_lookups[h] = lk.set_index("signal_date")
                stock_hold_lookups[sid] = sid_lookups
            rec = {"stock_id": sid, "signal_date": row.signal_date, "net_ratio": row.net_ratio}
            ok = True
            for h in HORIZONS:
                lk = sid_lookups.get(h)
                ixlk = ix_lookups.get(h)
                if lk is None or row.signal_date not in lk.index or ixlk is None or row.signal_date not in ixlk.index:
                    ok = False
                    break
            if not ok:
                continue
            for h in HORIZONS:
                lk = sid_lookups[h].loc[row.signal_date]
                ixlk = ix_lookups[h].loc[row.signal_date]
                r_s = lk["exit_close"] / lk["entry_open"] - 1 - COST
                r_ix = ixlk["exit_close"] / ixlk["entry_open"] - 1
                r_adj = r_s - BETA * r_ix
                rec[f"h{h}_r_adj_pct"] = r_adj * 100
            existing_recs.append(rec)
        existing_events = pd.DataFrame(existing_recs)
        existing_events.to_csv(OUT_DIR / f"{PREFIX}existing_rule_events_with_returns.csv", index=False)
        print(f"[INFO] 既有規則(5d net95)事件數 n={len(existing)}, 有完整forward-return的 n={len(existing_events)}")
        existing_summary = []
        for h in HORIZONS:
            existing_summary.append(full_stats(existing_events[f"h{h}_r_adj_pct"], f"existing_5dnet95_H{h}"))
        print(json.dumps(existing_summary, ensure_ascii=False, indent=2))
    else:
        existing_summary = []
        print(f"[WARN] 找不到 {EXISTING_RULE_EVENTS_CSV}，跳過既有規則對照")

    section("(7) 現況快照：9217 目前每一筆 net_shares>0 的持倉 + 移動加權平均成本 + 最新動能分類（SW20/LW60/Z1.0）")
    holding_rows = []
    for sid in universe:
        g_all = bars_all[bars_all["stock_id"] == sid].sort_values("trade_date").reset_index(drop=True)
        br = branch_all[branch_all["stock_id"] == sid][["trade_date", "buy", "sell", "net"]]
        b = reindex_branch_on_bars(g_all, br)
        if b is None or b.empty:
            continue
        cum_net_last = b["net"].cumsum().iloc[-1]
        if cum_net_last < MIN_HOLD_SHARES_REPORT:
            continue
        position, avg_cost = moving_avg_cost_snapshot(b)
        last_close = float(g_all["close"].iloc[-1])
        last_date = g_all["trade_date"].iloc[-1]
        state_row = diag_last_row.get(sid)
        momentum_state = state_row["state"].iloc[0] if state_row is not None else None
        z_now = float(state_row["z"].iloc[0]) if state_row is not None and pd.notna(state_row["z"].iloc[0]) else None
        holding_rows.append(
            {
                "stock_id": sid,
                "net_shares": position,
                "last_close": last_close,
                "last_date": last_date,
                "est_position_value_ntd": position * last_close,
                "moving_avg_cost": avg_cost,
                "unrealized_pnl_pct": (last_close / avg_cost - 1) * 100 if avg_cost > 0 else None,
                "momentum_state_now_SW20LW60Z1.0": momentum_state,
                "z_now_SW20LW60": z_now,
                "is_mega_or_etf": False,
            }
        )
    holdings_df = pd.DataFrame(holding_rows).sort_values("est_position_value_ntd", ascending=False)
    holdings_df.to_csv(OUT_DIR / f"{PREFIX}current_holdings_with_momentum.csv", index=False)
    print(f"[INFO] 現有持倉數（信號宇宙內, net_shares>={MIN_HOLD_SHARES_REPORT}）= {len(holdings_df)}")
    print(holdings_df.head(40).to_string(index=False))
    if momentum_state is not None:
        print("\n[INFO] 現況動能分類分布:")
        print(holdings_df["momentum_state_now_SW20LW60Z1.0"].value_counts())

    conn.close()

    summary_out = {
        "trader_id": TRADER_ID,
        "protocol": {
            "sw_lw_pairs": WINDOW_PAIRS,
            "z_thresh_set": Z_THRESH_SET,
            "z_hist_min": Z_HIST_MIN,
            "dedup_gap": DEDUP_GAP,
            "min_active_days_universe": MIN_ACTIVE_DAYS,
            "horizons": HORIZONS,
            "cost": COST, "beta": BETA, "bench": BENCH_CODE,
        },
        "universe_size": len(universe),
        "n_signal_events_raw": len(signals_df),
        "n_signal_events_with_returns": len(events_final),
        "existing_rule_comparison": existing_summary,
        "n_current_holdings_gt_1000shares": len(holdings_df),
    }
    (OUT_DIR / f"{PREFIX}run_summary.json").write_text(
        json.dumps(summary_out, ensure_ascii=False, indent=2, default=str)
    )
    print(f"\n[DONE] total elapsed {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
