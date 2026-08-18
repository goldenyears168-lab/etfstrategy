#!/usr/bin/env python3
"""M5 · 9217（凱基-松山）跟單策略：出場規則（H-E1）與部位管理（H-E2）研究。

純研究 · DB 唯讀（mode=ro）· 不改 config/.env/launchd · 不下單。

訊號 SSOT：scan_5d_net95（rolling 5 交易日 buy_5d>=0.5e8 ∩ net_ratio>=0.95 ∩ !mega），
去重 = rising-edge，完全比照 scripts/research/study_whale_9217_5d_net95_live_signal_validation.py。

協議基準：L1H7（T+1 開盤進 / 第7個交易日收盤出 / COST=30bps / BETA=1.15 / bench=IX0001）。

H-E1 出場規則比較（同一批事件、同一進場點，只換出場）：
  A. fixed_H{3,5,7,10,14,20}    固定天期收盤出
  B. branch_sell_*              跟隨 9217 自己開始賣（PIT：分點資料 D 日盤後才可見 → D+1 開盤出）
  C. trail_{3,5,8,10}           從持有期最高「收盤」回撤 X% → 隔日開盤出（另附 intrabar 敏感度）

H-E2 部位管理：
  (a) 持有期內再次觸發加碼（並重跑 C18acc underwater_rebound 移植版對照）
  (b) 按訊號強度（buy_5d）加權：等權 vs in-sample ∝buy_5d vs walk-forward 三檔權重／篩選
  (c) 同時持有上限 1/2/3/∞ 與資金可行性（10 萬/檔 · 帳戶總額 30 萬且與其他 sleeve 共用）

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m5_exit_position_study.py

輸出：reports/research/branch-footprint-screen/songshan_m5_*.{csv,json}
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy import stats as sps  # noqa: E402

from stock_db import DEFAULT_DB_PATH  # noqa: E402
from stock_db.connection import connect_ro  # noqa: E402

SOURCE = "finmind"
BENCH_CODE = "IX0001"
STUDY_START = "2024-07-01"
STUDY_END = "2026-08-14"  # 2026-08-17 當日資料不完整（444/~660 檔），刻意排除
COST, BETA = 0.003, 1.15
BASE_HOLD = 7
MAXH = 20  # 所有 path-dependent 規則的最大持有天期（也是天期掃描上界）
BUY_FLOOR, NET_MIN = 50_000_000.0, 0.95
TRADER_ID = "9217"

N_PERM = 4000
PERM_SEEDS = (20260817, 20260818, 424242)
FDR_ALPHA = 0.05

BUDGET_PER_SLOT = 100_000.0  # config/order.yaml songshan-copytrade budget_twd
ACCOUNT_TOTAL = 300_000.0  # mini 帳戶總額（與 TMF / dayflip-short 共用）

OUT_DIR = ROOT / "reports" / "research" / "branch-footprint-screen"
SCRIPTS = ROOT / "scripts" / "research"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M9217 = _load_module("m9217", SCRIPTS / "study_whale_9217_5d_net95_live_signal_validation.py")
M9217.STUDY_START = STUDY_START
M9217.STUDY_END = STUDY_END


def section(t: str) -> None:
    print(f"\n{'=' * 100}\n{t}\n{'=' * 100}")


# ---------------------------------------------------------------------------
# 資料載入
# ---------------------------------------------------------------------------

def load_ohlc(conn, sids: list[str]) -> dict[str, dict]:
    """每檔股票的 OHLC path：{sid: {'cal': [dates], 'idx': {d:i}, 'o/h/l/c': np.array}}"""
    out: dict[str, dict] = {}
    for sid in sids:
        rows = conn.execute(
            """
            SELECT trade_date, open, high, low, close FROM stock_daily_bars
            WHERE stock_id=? AND source=? AND trade_date BETWEEN ? AND ? AND close>0
            ORDER BY trade_date
            """,
            (sid, SOURCE, "2024-05-01", "2026-08-31"),
        ).fetchall()
        cal, o, h, low, c = [], [], [], [], []
        for r in rows:
            cl = float(r[4])
            op = float(r[1]) if r[1] else cl
            hi = float(r[2]) if r[2] else max(op, cl)
            lo = float(r[3]) if r[3] else min(op, cl)
            if op <= 0:
                op = cl
            cal.append(str(r[0]))
            o.append(op)
            h.append(hi)
            low.append(lo)
            c.append(cl)
        out[sid] = {
            "cal": cal,
            "idx": {d: i for i, d in enumerate(cal)},
            "o": np.array(o),
            "h": np.array(h),
            "l": np.array(low),
            "c": np.array(c),
        }
    return out


def load_ix(conn) -> dict:
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code=? AND date BETWEEN ? AND ? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (BENCH_CODE, "2024-05-01", "2026-08-31"),
    ).fetchall()
    d: dict[str, tuple[float, float]] = {}
    for dt, o, c in rows:
        d.setdefault(str(dt), (float(o), float(c)))
    return d


def load_branch(conn) -> dict[tuple[str, str], tuple[float, float]]:
    """9217 每日 (buy_amt, sell_amt)，金額 = 股數 × 當日收盤（同 scan_5d_net95）。"""
    rows = conn.execute(
        """
        SELECT b.stock_id, b.trade_date, b.buy*p.close, b.sell*p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date
                               AND p.source=?
        WHERE b.source=? AND b.securities_trader_id=? AND b.trade_date BETWEEN ? AND ?
          AND p.close>0
        """,
        (SOURCE, SOURCE, TRADER_ID, "2024-05-01", STUDY_END),
    ).fetchall()
    return {(str(r[0]), str(r[1])): (float(r[2] or 0), float(r[3] or 0)) for r in rows}


# ---------------------------------------------------------------------------
# 出場規則模擬器
# ---------------------------------------------------------------------------

class Sim:
    def __init__(self, ohlc, ix, branch):
        self.ohlc = ohlc
        self.ix = ix
        self.branch = branch

    def entry_index(self, sid: str, signal_date: str) -> int | None:
        p = self.ohlc.get(sid)
        if not p:
            return None
        cal = p["cal"]
        # 第一個 > signal_date 的交易日
        lo, hi = 0, len(cal)
        while lo < hi:
            mid = (lo + hi) // 2
            if cal[mid] <= signal_date:
                lo = mid + 1
            else:
                hi = mid
        return lo if lo < len(cal) else None

    def bench(self, ed: str, xd: str, exit_px: str) -> float | None:
        be = self.ix.get(ed)
        bx = self.ix.get(xd)
        if be is None or bx is None:
            return None
        bo = be[0]
        bc = bx[1] if exit_px == "close" else bx[0]
        if bo <= 0:
            return None
        return bc / bo - 1

    def _pack(self, sid, sig, ei, ed, eo, xi, xd, xp, exit_px, reason):
        br = self.bench(ed, xd, exit_px)
        if br is None or eo <= 0 or xp <= 0:
            return None
        r = xp / eo - 1 - COST
        return {
            "stock_id": sid,
            "signal_date": sig,
            "entry_date": ed,
            "entry_open": eo,
            "exit_date": xd,
            "exit_price": xp,
            "exit_reason": reason,
            "hold_bars": xi - ei + 1,
            "r_pct": r * 100,
            "r_ix_pct": br * 100,
            "r_adj_pct": (r - BETA * br) * 100,
        }

    def fixed(self, sid: str, sig: str, hold: int) -> dict | None:
        p = self.ohlc.get(sid)
        ei = self.entry_index(sid, sig)
        if p is None or ei is None or ei + hold - 1 >= len(p["cal"]):
            return None
        xi = ei + hold - 1
        return self._pack(sid, sig, ei, p["cal"][ei], p["o"][ei], xi, p["cal"][xi],
                          p["c"][xi], "close", f"time_H{hold}")

    def branch_sell(self, sid: str, sig: str, buy_5d: float, frac: float,
                    mode: str = "daily", max_hold: int = MAXH) -> dict | None:
        """9217 賣出強度達門檻 → 隔日開盤出；未觸發 → max_hold 收盤出。

        mode='daily'  : 當日 sell_amt >= frac * buy_5d
        mode='cumnet' : 進場後累計 (sell-buy) >= frac * buy_5d
        PIT：分點 D 日資料盤後才可見 → 出場在 D+1 開盤。
        """
        p = self.ohlc.get(sid)
        ei = self.entry_index(sid, sig)
        if p is None or ei is None or ei + max_hold - 1 >= len(p["cal"]):
            return None
        cal, ed, eo = p["cal"], p["cal"][ei], p["o"][ei]
        thr = frac * buy_5d
        cum = 0.0
        for j in range(ei, ei + max_hold):
            b, s = self.branch.get((sid, cal[j]), (0.0, 0.0))
            fire = (s >= thr) if mode == "daily" else ((cum := cum + (s - b)) >= thr)
            if fire and j + 1 < len(cal) and j + 1 <= ei + max_hold - 1 + 1:
                xi = j + 1
                if xi >= len(cal):
                    break
                return self._pack(sid, sig, ei, ed, eo, xi, cal[xi], p["o"][xi], "open",
                                  f"branch_sell_d{j - ei}")
        xi = ei + max_hold - 1
        return self._pack(sid, sig, ei, ed, eo, xi, cal[xi], p["c"][xi], "close", "time_cap")

    def trail(self, sid: str, sig: str, pct: float, max_hold: int = MAXH,
              intrabar: bool = False) -> dict | None:
        """從持有期最高收盤回撤 pct% → 隔日開盤出（intrabar=True 改用 high/low 當日觸價成交）。"""
        p = self.ohlc.get(sid)
        ei = self.entry_index(sid, sig)
        if p is None or ei is None or ei + max_hold - 1 >= len(p["cal"]):
            return None
        cal, ed, eo = p["cal"], p["cal"][ei], p["o"][ei]
        peak = eo
        for j in range(ei, ei + max_hold):
            if intrabar:
                peak = max(peak, p["h"][j])
                stop = peak * (1 - pct / 100.0)
                if p["l"][j] <= stop and j > ei:
                    return self._pack(sid, sig, ei, ed, eo, j, cal[j], stop, "close",
                                      f"trail_intrabar_d{j - ei}")
            else:
                peak = max(peak, p["c"][j])
                if p["c"][j] <= peak * (1 - pct / 100.0):
                    if j + 1 >= len(cal):
                        break
                    return self._pack(sid, sig, ei, ed, eo, j + 1, cal[j + 1], p["o"][j + 1],
                                      "open", f"trail_d{j - ei}")
        xi = ei + max_hold - 1
        return self._pack(sid, sig, ei, ed, eo, xi, cal[xi], p["c"][xi], "close", "time_cap")


# ---------------------------------------------------------------------------
# 統計
# ---------------------------------------------------------------------------

def stats_block(vals: np.ndarray, label: str, hold_bars: np.ndarray | None = None) -> dict:
    v = np.asarray(vals, dtype=float)
    n = len(v)
    if n == 0:
        return {"label": label, "n": 0}
    sd = float(np.std(v, ddof=1)) if n > 1 else float("nan")
    t_stat, t_p = (sps.ttest_1samp(v, 0.0) if n > 1 else (float("nan"), float("nan")))
    out = {
        "label": label,
        "n": n,
        "mean_pct": float(np.mean(v)),
        "median_pct": float(np.median(v)),
        "std_pct": sd,
        "win_rate_pct": float(np.mean(v > 0) * 100),
        "max_loss_pct": float(np.min(v)),
        "max_gain_pct": float(np.max(v)),
        "sharpe_per_trade": float(np.mean(v) / sd) if sd and sd == sd else None,
        "t_stat": float(t_stat),
        "t_p_twosided": float(t_p),
        "sum_pct": float(np.sum(v)),
    }
    if hold_bars is not None and len(hold_bars) == n:
        hb = np.asarray(hold_bars, dtype=float)
        out["mean_hold_bars"] = float(np.mean(hb))
        out["mean_pct_per_bar"] = float(np.mean(v) / np.mean(hb))
        # 時間標準化：每筆報酬除以 sqrt(持有天期) 後再算 mean/std（研究筆記提到的天期假象防呆）
        vt = v / np.sqrt(np.maximum(hb, 1.0))
        out["time_normalized_mean"] = float(np.mean(vt))
        out["time_normalized_sharpe"] = (
            float(np.mean(vt) / np.std(vt, ddof=1)) if n > 1 and np.std(vt, ddof=1) > 0 else None
        )
    return out


def bh_fdr(pvals: dict[str, float], alpha: float = FDR_ALPHA, floor: float = 1.0 / N_PERM) -> list[dict]:
    items = sorted(((k, max(float(v), floor)) for k, v in pvals.items()), key=lambda x: x[1])
    n = len(items)
    raw = [(k, p * n / (i + 1)) for i, (k, p) in enumerate(items)]
    q_mono, running = [], float("inf")
    for k, q in reversed(raw):
        running = min(running, q)
        q_mono.append((k, min(running, 1.0)))
    q_mono.reverse()
    return [
        {"rule": k, "p": round(p, 6), "rank": i + 1, "q_bh": round(q, 6), "pass": q <= alpha}
        for i, ((k, p), (_, q)) in enumerate(zip(items, q_mono))
    ]


def permutation_rule(sim: Sim, events: pd.DataFrame, rule_fn, pools: dict[str, list[str]],
                     n_perm: int, seed: int) -> dict:
    """對每個真實事件，從同股票合法訊號日池抽假日期，重跑同一條出場規則建 null。

    buy_5d 沿用真實事件的值（只隨機化「時機」，不隨機化訊號強度）。
    """
    rng = np.random.default_rng(seed)
    real = []
    keys = []
    for r in events.itertuples(index=False):
        t = rule_fn(r.stock_id, r.signal_date, float(r.buy_5d))
        if t:
            real.append(t["r_adj_pct"])
            keys.append((r.stock_id, float(r.buy_5d)))
    if not real:
        return {"n": 0}
    obs_mean, obs_med = float(np.mean(real)), float(np.median(real))
    pm, pmd = [], []
    for _ in range(n_perm):
        vals = []
        for sid, b5 in keys:
            pool = pools.get(sid) or []
            if not pool:
                continue
            d = pool[rng.integers(len(pool))]
            t = rule_fn(sid, d, b5)
            if t:
                vals.append(t["r_adj_pct"])
        if vals:
            pm.append(np.mean(vals))
            pmd.append(np.median(vals))
    pm, pmd = np.array(pm), np.array(pmd)
    return {
        "n": len(real),
        "n_perm": int(len(pm)),
        "seed": seed,
        "obs_mean_pct": obs_mean,
        "obs_median_pct": obs_med,
        "null_mean_pct": float(np.mean(pm)),
        "null_std_pct": float(np.std(pm)),
        "p_mean_onesided": float(np.mean(pm >= obs_mean)),
        "p_median_onesided": float(np.mean(pmd >= obs_med)),
    }


# ---------------------------------------------------------------------------
# 組合模擬（日 mark-to-market，未做 beta 對沖 → 反映真實帳戶淨值）
# ---------------------------------------------------------------------------

def portfolio_mtm(trades: list[dict], ohlc: dict, calendar: list[str],
                  initial_capital: float) -> dict:
    """trades: 需含 stock_id/entry_date/entry_open/exit_date/exit_price/notional。"""
    if not trades:
        return {"n": 0}
    by_entry: dict[str, list[dict]] = {}
    by_exit: dict[str, list[dict]] = {}
    for t in trades:
        by_entry.setdefault(t["entry_date"], []).append(t)
        by_exit.setdefault(t["exit_date"], []).append(t)
    cash = initial_capital
    open_pos: list[dict] = []
    curve = []
    peak_deployed = 0.0
    for d in calendar:
        for t in by_entry.get(d, []):
            cash -= t["notional"]
            open_pos.append(dict(t, shares=t["notional"] / t["entry_open"]))
        for t in by_exit.get(d, []):
            for op in list(open_pos):
                if op is t or (op["stock_id"] == t["stock_id"]
                               and op["entry_date"] == t["entry_date"]
                               and op["exit_date"] == t["exit_date"]):
                    proceeds = op["shares"] * t["exit_price"] * (1 - COST)
                    cash += proceeds
                    open_pos.remove(op)
                    break
        mtm = cash
        deployed = 0.0
        for op in open_pos:
            p = ohlc[op["stock_id"]]
            i = p["idx"].get(d)
            px = float(p["c"][i]) if i is not None else op["entry_open"]
            mtm += op["shares"] * px
            deployed += op["notional"]
        peak_deployed = max(peak_deployed, deployed)
        curve.append((d, mtm, len(open_pos), deployed))
    eq = np.array([c[1] for c in curve])
    run_max = np.maximum.accumulate(eq)
    dd = eq - run_max
    i_mdd = int(np.argmin(dd))
    return {
        "n_trades": len(trades),
        "initial_capital": initial_capital,
        "final_equity": float(eq[-1]),
        "total_pnl": float(eq[-1] - initial_capital),
        "total_return_pct": float((eq[-1] / initial_capital - 1) * 100),
        "max_drawdown_twd": float(dd[i_mdd]),
        "max_drawdown_pct_of_peak": float(dd[i_mdd] / run_max[i_mdd] * 100),
        "mdd_date": curve[i_mdd][0],
        "peak_concurrent": int(max(c[2] for c in curve)),
        "avg_concurrent": float(np.mean([c[2] for c in curve])),
        "peak_deployed_twd": peak_deployed,
        "avg_deployed_twd": float(np.mean([c[3] for c in curve])),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print(f"[INFO] DB(read-only) = {DEFAULT_DB_PATH}")
    conn = connect_ro(DEFAULT_DB_PATH)
    mega = M9217.load_mega(M9217.MEGA_PATH)
    calendar = M9217.load_calendar(conn, STUDY_START, STUDY_END)

    section("(0) 基準母體重算（含 2026-08-17 凌晨補進的 255 檔價格）")
    events, grid = M9217.build_5d_net95_events(conn, mega)
    events = events.reset_index(drop=True)
    print(f"[RESULT] rising-edge 事件 n = {len(events)}，涵蓋 {events['stock_id'].nunique()} 檔")
    old = pd.read_csv(OUT_DIR / "whale_9217_round10_events.csv", dtype={"stock_id": str})
    old_keys = set(zip(old["stock_id"], old["signal_date"]))
    new_keys = set(zip(events["stock_id"], events["signal_date"]))
    print(f"[RESULT] 舊 round10 檔 n={len(old)}；新增 {len(new_keys - old_keys)} 筆、消失 {len(old_keys - new_keys)} 筆")
    added = sorted(new_keys - old_keys, key=lambda x: x[1])
    removed = sorted(old_keys - new_keys, key=lambda x: x[1])
    for k in added:
        print(f"    + {k[0]} {k[1]}")
    for k in removed:
        print(f"    - {k[0]} {k[1]}")
    events.to_csv(OUT_DIR / "songshan_m5_events.csv", index=False)

    sids = sorted(events["stock_id"].unique())
    ohlc = load_ohlc(conn, sids)
    ix = load_ix(conn)
    branch = load_branch(conn)
    conn.close()
    sim = Sim(ohlc, ix, branch)

    # ---- 規則定義 ---------------------------------------------------------
    rules: dict[str, callable] = {}
    for h in (3, 5, 7, 10, 14, 20):
        rules[f"A_fixed_H{h}"] = (lambda h: lambda s, d, b: sim.fixed(s, d, h))(h)
    for f in (0.10, 0.25, 0.50, 1.00):
        rules[f"B_branch_sell_daily_{int(f*100)}pct"] = (
            lambda f: lambda s, d, b: sim.branch_sell(s, d, b, f, "daily"))(f)
    for f in (0.25, 0.50):
        rules[f"B_branch_cumnet_{int(f*100)}pct"] = (
            lambda f: lambda s, d, b: sim.branch_sell(s, d, b, f, "cumnet"))(f)
    for x in (3, 5, 8, 10):
        rules[f"C_trail_{x}pct"] = (lambda x: lambda s, d, b: sim.trail(s, d, x))(x)
    for x in (5, 8):
        rules[f"C_trail_{x}pct_intrabar"] = (
            lambda x: lambda s, d, b: sim.trail(s, d, x, intrabar=True))(x)

    # ---- 共同可評估子集（所有規則都算得出來，避免右設限造成的規則間不可比）----
    section("(1) H-E1 出場規則比較")
    per_rule_raw: dict[str, list[dict]] = {}
    for name, fn in rules.items():
        per_rule_raw[name] = [
            t for t in (fn(r.stock_id, r.signal_date, float(r.buy_5d))
                        for r in events.itertuples(index=False)) if t
        ]
    common = None
    for name, ts in per_rule_raw.items():
        ks = {(t["stock_id"], t["signal_date"]) for t in ts}
        common = ks if common is None else (common & ks)
    print(f"[INFO] 共同可評估事件 n = {len(common)}（需 {MAXH} 根 bar；全事件 n={len(events)}）")
    ev_common = events[[(r.stock_id, r.signal_date) in common
                        for r in events.itertuples(index=False)]].reset_index(drop=True)

    rows = []
    trade_rows = []
    for name, ts in per_rule_raw.items():
        sub = [t for t in ts if (t["stock_id"], t["signal_date"]) in common]
        st = stats_block(np.array([t["r_adj_pct"] for t in sub]), name,
                         np.array([t["hold_bars"] for t in sub]))
        st["n_all_evaluable"] = len(ts)
        st_all = stats_block(np.array([t["r_adj_pct"] for t in ts]), name + "_allEval")
        st["mean_pct_all_evaluable"] = st_all.get("mean_pct")
        rows.append(st)
        for t in sub:
            trade_rows.append(dict(t, rule=name))
    cmp_df = pd.DataFrame(rows).sort_values("mean_pct", ascending=False)
    cols = ["label", "n", "mean_pct", "median_pct", "win_rate_pct", "max_loss_pct",
            "std_pct", "sharpe_per_trade", "mean_hold_bars", "mean_pct_per_bar",
            "time_normalized_sharpe", "t_p_twosided", "mean_pct_all_evaluable"]
    print(cmp_df[cols].to_string(index=False, float_format=lambda x: f"{x:8.3f}"))
    cmp_df.to_csv(OUT_DIR / "songshan_m5_exit_rule_comparison.csv", index=False)
    pd.DataFrame(trade_rows).to_csv(OUT_DIR / "songshan_m5_exit_rule_trades.csv", index=False)

    # ---- permutation（全部規則都跑，才能誠實做 BH-FDR）--------------------
    section("(2) permutation / placebo（同股票隨機時機 null）+ BH-FDR")
    pools: dict[str, list[str]] = {}
    real_dates = {}
    for r in events.itertuples(index=False):
        real_dates.setdefault(r.stock_id, set()).add(r.signal_date)
    for sid in sids:
        p = ohlc[sid]
        cal = p["cal"]
        pool = [d for i, d in enumerate(cal)
                if STUDY_START <= d <= STUDY_END and i + MAXH < len(cal)
                and d not in real_dates.get(sid, set())]
        pools[sid] = pool
    perm_out: dict[str, dict] = {}
    p_for_fdr: dict[str, float] = {}
    for name, fn in rules.items():
        seeds = []
        for sd in PERM_SEEDS:
            seeds.append(permutation_rule(sim, ev_common, fn, pools, N_PERM, sd))
        worst = max(s["p_mean_onesided"] for s in seeds)
        perm_out[name] = {"per_seed": seeds, "p_mean_worst": worst,
                          "p_median_worst": max(s["p_median_onesided"] for s in seeds)}
        p_for_fdr[name] = worst
        print(f"  {name:34s} obs_mean={seeds[0]['obs_mean_pct']:+7.3f}%  "
              f"null_mean={seeds[0]['null_mean_pct']:+6.3f}%  p_worst={worst:.4f}")
    fdr = bh_fdr(p_for_fdr)
    print("\n  BH-FDR (alpha=0.05, N=%d hypotheses):" % len(p_for_fdr))
    for r in fdr:
        print(f"    rank{r['rank']:2d} {r['rule']:34s} p={r['p']:.4f}  q={r['q_bh']:.4f}  "
              f"{'PASS' if r['pass'] else 'fail'}")

    # ---- 天期形狀（用同一套標準化）---------------------------------------
    section("(3) 天期形狀（同一批事件、同一標準化）")
    shape = []
    for h in (3, 5, 7, 10, 14, 20):
        name = f"A_fixed_H{h}"
        sub = [t for t in per_rule_raw[name] if (t["stock_id"], t["signal_date"]) in common]
        v = np.array([t["r_adj_pct"] for t in sub])
        shape.append({
            "H": h, "n": len(v), "mean_pct": float(np.mean(v)), "median_pct": float(np.median(v)),
            "std_pct": float(np.std(v, ddof=1)), "win_rate_pct": float(np.mean(v > 0) * 100),
            "max_loss_pct": float(np.min(v)),
            "mean_per_bar": float(np.mean(v) / h),
            "mean_over_sqrtH": float(np.mean(v) / np.sqrt(h)),
            "sharpe_per_trade": float(np.mean(v) / np.std(v, ddof=1)),
            "t_stat": float(sps.ttest_1samp(v, 0.0).statistic),
            "perm_p_worst": p_for_fdr[name],
        })
    shape_df = pd.DataFrame(shape)
    print(shape_df.to_string(index=False, float_format=lambda x: f"{x:8.3f}"))
    shape_df.to_csv(OUT_DIR / "songshan_m5_horizon_shape.csv", index=False)

    # =======================================================================
    # H-E2
    # =======================================================================
    section("(4) H-E2(a) 加碼：持有期內再次觸發 vs C18acc underwater_rebound 移植")
    # 4a-1 再次觸發加碼（用 grid 的 triggered 狀態，PIT：D 日狀態 → D+1 開盤加碼）
    trig = grid.set_index(["stock_id", "trade_date"])["triggered"].to_dict()
    base = {(t["stock_id"], t["signal_date"]): t
            for t in per_rule_raw[f"A_fixed_H{BASE_HOLD}"]}
    pyr_rows = []
    for (sid, sig), t in base.items():
        p = ohlc[sid]
        ei = p["idx"][t["entry_date"]]
        xi = p["idx"][t["exit_date"]]
        add = None
        for j in range(ei, xi):  # 進場日 .. 出場前一日
            if trig.get((sid, p["cal"][j]), False) and j + 1 <= xi:
                add = (p["cal"][j + 1], float(p["o"][j + 1]))
                break
        row = dict(t)
        row["add_date"] = add[0] if add else None
        row["add_price"] = add[1] if add else None
        if add:
            blended_entry = 0.5 * t["entry_open"] + 0.5 * add[1]
            r = t["exit_price"] / blended_entry - 1 - COST
            row["r_adj_pyramid_pct"] = (r - BETA * t["r_ix_pct"] / 100) * 100
        else:
            row["r_adj_pyramid_pct"] = t["r_adj_pct"]
        pyr_rows.append(row)
    pyr = pd.DataFrame(pyr_rows)
    elig = pyr[pyr["add_date"].notna()]
    print(f"  再次觸發加碼：n_eligible={len(elig)}/{len(pyr)}")
    if len(elig):
        print(f"    eligible 子集 baseline mean={elig['r_adj_pct'].mean():+.3f}%  "
              f"加碼後 mean={elig['r_adj_pyramid_pct'].mean():+.3f}%  "
              f"delta={elig['r_adj_pyramid_pct'].mean()-elig['r_adj_pct'].mean():+.3f}pp")
        nu_base, nu_pyr = len(pyr), len(pyr) + len(elig)
        pnl_b = pyr["r_adj_pct"].sum()
        pnl_p = (pyr["r_adj_pct"].where(pyr["add_date"].isna(),
                                        pyr["r_adj_pyramid_pct"] * 2)).sum()
        print(f"    單位加權（eligible 吃 2 單位）：avg/unit {pnl_b/nu_base:+.3f}% → "
              f"{pnl_p/nu_pyr:+.3f}%  總單位 {nu_base}→{nu_pyr}")

    # 4a-2 C18acc underwater_rebound 移植（日 bar 版，門檻 30%/10%/5%）
    ub_rows = []
    for th in (0.30, 0.10, 0.05):
        elig2 = []
        for (sid, sig), t in base.items():
            p = ohlc[sid]
            ei, xi = p["idx"][t["entry_date"]], p["idx"][t["exit_date"]]
            trough = p["l"][ei]
            hit = None
            for j in range(ei + 1, min(ei + 3, xi)):
                trough = min(trough, p["l"][j])
                if p["c"][j] < t["entry_open"] and trough > 0 and (p["c"][j] - trough) / trough >= th:
                    if j + 1 <= xi:
                        hit = (p["cal"][j + 1], float(p["o"][j + 1]))
                    break
            if hit:
                be = 0.5 * t["entry_open"] + 0.5 * hit[1]
                r = t["exit_price"] / be - 1 - COST
                elig2.append((t["r_adj_pct"], (r - BETA * t["r_ix_pct"] / 100) * 100))
        ub_rows.append({
            "threshold": th, "n_eligible": len(elig2),
            "base_mean_pct": float(np.mean([a for a, _ in elig2])) if elig2 else None,
            "pyramid_mean_pct": float(np.mean([b for _, b in elig2])) if elig2 else None,
            "delta_pp": float(np.mean([b - a for a, b in elig2])) if elig2 else None,
        })
    ub_df = pd.DataFrame(ub_rows)
    print("\n  C18acc underwater_rebound 移植（新母體重跑）：")
    print(ub_df.to_string(index=False))
    pyr.to_csv(OUT_DIR / "songshan_m5_pyramid_detail.csv", index=False)

    # ---- 4b 強度加權 ------------------------------------------------------
    section("(5) H-E2(b) 訊號強度加權（buy_5d）")
    bt = pd.DataFrame(list(base.values()))
    bt = bt.merge(events[["stock_id", "signal_date", "buy_5d", "net_ratio"]],
                  on=["stock_id", "signal_date"], how="left").sort_values("signal_date")

    def tier(b):
        return "T1_0.5-1e8" if b < 1e8 else ("T2_1-3e8" if b < 3e8 else "T3_>3e8")

    bt["tier"] = bt["buy_5d"].apply(tier)
    tier_desc = bt.groupby("tier")["r_adj_pct"].agg(["count", "mean", "median",
                                                    lambda s: (s > 0).mean() * 100, "min"])
    tier_desc.columns = ["n", "mean_pct", "median_pct", "win_rate_pct", "max_loss_pct"]
    print("  分檔描述統計（in-sample，僅供診斷）：")
    print(tier_desc.to_string(float_format=lambda x: f"{x:8.3f}"))
    rho = sps.spearmanr(bt["buy_5d"], bt["r_adj_pct"])
    print(f"  buy_5d vs r_adj Spearman rho={rho.statistic:+.3f}  p={rho.pvalue:.3f}")

    # walk-forward 三檔權重：只用 exit_date < signal_date 的已完成交易
    MIN_PRIOR = 5
    wf_w, wf_take = [], []
    for r in bt.itertuples(index=False):
        prior = bt[(bt["exit_date"] < r.signal_date)]
        w = 1.0
        take = True
        if len(prior) >= 3 * MIN_PRIOR:
            pm = prior.groupby("tier")["r_adj_pct"].agg(["count", "mean"])
            pm = pm[pm["count"] >= MIN_PRIOR]
            if r.tier in pm.index and len(pm) >= 2:
                order = pm["mean"].rank(ascending=False)
                rk = order.loc[r.tier]
                w = {1.0: 1.5, 2.0: 1.0, 3.0: 0.5}.get(float(rk), 1.0)
                take = bool(pm.loc[r.tier, "mean"] > 0)
        wf_w.append(w)
        wf_take.append(take)
    bt["w_wf_tier"] = wf_w
    bt["take_wf"] = wf_take
    bt["w_prop_buy5d"] = bt["buy_5d"] / bt["buy_5d"].mean()
    bt["w_log_buy5d"] = np.log(bt["buy_5d"]) / np.log(bt["buy_5d"]).mean()

    wrows = []
    for wname, wcol, in_sample in (("equal", None, False),
                                   ("in-sample ∝buy_5d", "w_prop_buy5d", True),
                                   ("in-sample ∝log(buy_5d)", "w_log_buy5d", True),
                                   ("walk-forward 三檔權重", "w_wf_tier", False)):
        w = np.ones(len(bt)) if wcol is None else bt[wcol].to_numpy()
        r = bt["r_adj_pct"].to_numpy()
        wrows.append({
            "scheme": wname, "in_sample_choice": in_sample, "n": len(bt),
            "weighted_mean_pct": float(np.sum(w * r) / np.sum(w)),
            "capital_weighted_total_pct": float(np.sum(w * r) / len(bt)),
            "notional_multiple": float(np.sum(w) / len(bt)),
        })
    sub_take = bt[bt["take_wf"]]
    wrows.append({
        "scheme": "walk-forward 分檔篩選（只做勝率為正的檔）", "in_sample_choice": False,
        "n": len(sub_take),
        "weighted_mean_pct": float(sub_take["r_adj_pct"].mean()) if len(sub_take) else None,
        "capital_weighted_total_pct": float(sub_take["r_adj_pct"].sum() / len(bt)),
        "notional_multiple": float(len(sub_take) / len(bt)),
    })
    wdf = pd.DataFrame(wrows)
    print("\n  加權方案對照（r_adj，H7 基準出場）：")
    print(wdf.to_string(index=False, float_format=lambda x: f"{x:8.3f}"))
    wdf.to_csv(OUT_DIR / "songshan_m5_weighting.csv", index=False)
    bt.to_csv(OUT_DIR / "songshan_m5_base_trades.csv", index=False)

    # ---- 4c 同時持有上限 --------------------------------------------------
    section("(6) H-E2(c) 同時持有上限與資金可行性")
    all_trades = sorted(base.values(), key=lambda t: (t["entry_date"], t["stock_id"]))
    cap_rows = []
    for cap in (1, 2, 3, 99):
        taken, open_until = [], []
        for t in all_trades:
            open_until = [x for x in open_until if x > t["entry_date"]]
            if len(open_until) >= cap:
                continue
            taken.append(t)
            open_until.append(t["exit_date"])
        cap_capital = BUDGET_PER_SLOT * (cap if cap < 99 else max(
            1, portfolio_mtm([dict(t, notional=BUDGET_PER_SLOT) for t in all_trades],
                             ohlc, calendar, 1e9)["peak_concurrent"]))
        pf = portfolio_mtm([dict(t, notional=BUDGET_PER_SLOT) for t in taken],
                           ohlc, calendar, cap_capital)
        v = np.array([t["r_adj_pct"] for t in taken])
        cap_rows.append({
            "cap": cap if cap < 99 else "∞",
            "n_taken": len(taken), "n_skipped": len(all_trades) - len(taken),
            "mean_r_adj_pct": float(np.mean(v)), "sum_r_adj_pct": float(np.sum(v)),
            "capital_twd": cap_capital,
            "total_pnl_twd": pf["total_pnl"], "total_return_pct": pf["total_return_pct"],
            "mdd_twd": pf["max_drawdown_twd"], "mdd_pct": pf["max_drawdown_pct_of_peak"],
            "peak_concurrent": pf["peak_concurrent"], "avg_concurrent": pf["avg_concurrent"],
            "avg_deployed_twd": pf["avg_deployed_twd"],
            "capital_utilization_pct": pf["avg_deployed_twd"] / cap_capital * 100,
        })
    cap_df = pd.DataFrame(cap_rows)
    print(cap_df.to_string(index=False, float_format=lambda x: f"{x:10.2f}"))
    cap_df.to_csv(OUT_DIR / "songshan_m5_concurrency.csv", index=False)

    # 歷史同時持有分布
    from collections import Counter
    day_count = Counter()
    for t in all_trades:
        p = ohlc[t["stock_id"]]
        i0, i1 = p["idx"][t["entry_date"]], p["idx"][t["exit_date"]]
        for j in range(i0, i1 + 1):
            day_count[p["cal"][j]] += 1
    dist = Counter(day_count.values())
    print("\n  歷史同時持有檔數分布（僅計有持倉的日子）：")
    for k in sorted(dist):
        print(f"    {k} 檔：{dist[k]} 天")
    print(f"  有持倉的交易日 = {len(day_count)} / 全期 {len(calendar)} 天 "
          f"({len(day_count)/len(calendar)*100:.1f}%)")

    # ---- (7) 每條出場規則的組合層風險（MDD 前置）---------------------------
    section("(7) 每條出場規則的組合層風險：最大單筆虧損 / 最大回撤 / 訊號貢獻度")
    risk_rows = []
    for name, ts in per_rule_raw.items():
        sub = [t for t in ts if (t["stock_id"], t["signal_date"]) in common]
        v = np.array([t["r_adj_pct"] for t in sub])
        vraw = np.array([t["r_pct"] for t in sub])
        pf_probe = portfolio_mtm([dict(t, notional=BUDGET_PER_SLOT) for t in sub],
                                 ohlc, calendar, 1e9)
        cap_needed = BUDGET_PER_SLOT * max(1, pf_probe["peak_concurrent"])
        pf = portfolio_mtm([dict(t, notional=BUDGET_PER_SLOT) for t in sub],
                           ohlc, calendar, cap_needed)
        pmean = perm_out[name]["per_seed"][0]
        risk_rows.append({
            "rule": name,
            "max_loss_radj_pct": float(np.min(v)),
            "max_loss_raw_pct": float(np.min(vraw)),
            "mdd_twd": pf["max_drawdown_twd"],
            "mdd_pct_of_peak": pf["max_drawdown_pct_of_peak"],
            "peak_concurrent": pf["peak_concurrent"],
            "capital_needed_twd": cap_needed,
            "total_pnl_twd": pf["total_pnl"],
            "mean_radj_pct": float(np.mean(v)),
            "median_radj_pct": float(np.median(v)),
            "win_rate_pct": float(np.mean(v > 0) * 100),
            "placebo_null_mean_pct": pmean["null_mean_pct"],
            "signal_contribution_pp": float(np.mean(v)) - pmean["null_mean_pct"],
            "perm_p_worst": perm_out[name]["p_mean_worst"],
        })
    risk_df = pd.DataFrame(risk_rows).sort_values("signal_contribution_pp", ascending=False)
    print(risk_df.to_string(index=False, float_format=lambda x: f"{x:10.3f}"))
    risk_df.to_csv(OUT_DIR / "songshan_m5_exit_rule_risk.csv", index=False)

    # ---- (8) 6449 案例研究 -------------------------------------------------
    section("(8) 6449 案例研究：跟隨分點出場 vs 移動停利（4 筆全負的關鍵測試）")
    case_rows = []
    for name, ts in per_rule_raw.items():
        for t in ts:
            if t["stock_id"] == "6449":
                case_rows.append({
                    "rule": name, "signal_date": t["signal_date"], "entry": t["entry_date"],
                    "entry_open": t["entry_open"], "exit": t["exit_date"],
                    "exit_price": t["exit_price"], "hold": t["hold_bars"],
                    "reason": t["exit_reason"], "r_pct": t["r_pct"], "r_adj_pct": t["r_adj_pct"],
                })
    case_df = pd.DataFrame(case_rows)
    piv = case_df.pivot_table(index="rule", columns="signal_date", values="r_adj_pct")
    piv["6449_sum"] = piv.sum(axis=1)
    print(piv.sort_values("6449_sum", ascending=False).to_string(float_format=lambda x: f"{x:9.2f}"))
    case_df.to_csv(OUT_DIR / "songshan_m5_case_6449.csv", index=False)
    print("\n  9217 在 6449 的分點 tape（買/賣，億元）：")
    p6 = ohlc["6449"]
    for d in p6["cal"]:
        if "2026-06-10" <= d <= "2026-07-20":
            b, s = branch.get(("6449", d), (0.0, 0.0))
            i = p6["idx"][d]
            print(f"    {d}  close={p6['c'][i]:7.1f}  buy={b/1e8:6.3f}億  sell={s/1e8:6.3f}億")

    # 排除 6449 的敏感度
    section("(9) 集中度敏感度：排除 6449 後各規則統計")
    conc_rows = []
    for name, ts in per_rule_raw.items():
        sub = [t for t in ts if (t["stock_id"], t["signal_date"]) in common]
        ex = [t for t in sub if t["stock_id"] != "6449"]
        v, ve = np.array([t["r_adj_pct"] for t in sub]), np.array([t["r_adj_pct"] for t in ex])
        conc_rows.append({
            "rule": name, "n_all": len(v), "mean_all": float(np.mean(v)),
            "median_all": float(np.median(v)),
            "n_ex6449": len(ve), "mean_ex6449": float(np.mean(ve)),
            "median_ex6449": float(np.median(ve)),
            "win_ex6449_pct": float(np.mean(ve > 0) * 100),
        })
    conc_df = pd.DataFrame(conc_rows).sort_values("mean_ex6449", ascending=False)
    print(conc_df.to_string(index=False, float_format=lambda x: f"{x:9.3f}"))
    conc_df.to_csv(OUT_DIR / "songshan_m5_ex6449.csv", index=False)

    # ---- (10) 資金可行性結論 ----------------------------------------------
    section("(10) 30 萬共用額度下的袖級可行性")
    h7 = [t for t in per_rule_raw["A_fixed_H7"] if (t["stock_id"], t["signal_date"]) in common]
    v7 = np.array([t["r_adj_pct"] for t in h7])
    vraw7 = np.array([t["r_pct"] for t in h7])
    n_years = 516 / 245.0
    feas = {
        "n_trades": len(v7),
        "trades_per_year": len(v7) / n_years,
        "mean_radj_pct": float(np.mean(v7)),
        "mean_raw_pct": float(np.mean(vraw7)),
        "expected_twd_per_trade_radj": float(np.mean(v7)) / 100 * BUDGET_PER_SLOT,
        "expected_twd_per_trade_raw": float(np.mean(vraw7)) / 100 * BUDGET_PER_SLOT,
        "median_twd_per_trade_raw": float(np.median(vraw7)) / 100 * BUDGET_PER_SLOT,
        "worst_trade_twd_raw": float(np.min(vraw7)) / 100 * BUDGET_PER_SLOT,
        "annual_expected_twd_cap1": float(np.mean(vraw7)) / 100 * BUDGET_PER_SLOT
        * (len(v7) / n_years) * 0.36,  # ×平均同時持有 1 檔的佔用率
        "capital_locked_when_open_twd": BUDGET_PER_SLOT,
        "pct_of_account": BUDGET_PER_SLOT / ACCOUNT_TOTAL * 100,
        "pct_days_with_position": len(day_count) / len(calendar) * 100,
    }
    for k, val in feas.items():
        print(f"  {k:38s} = {val:,.3f}" if isinstance(val, float) else f"  {k:38s} = {val}")

    # ---- 輸出 -------------------------------------------------------------
    summary = {
        "generated": "songshan_m5_exit_position_study",
        "db": str(DEFAULT_DB_PATH),
        "study_window": f"{STUDY_START}..{STUDY_END}",
        "protocol": {"cost": COST, "beta": BETA, "bench": BENCH_CODE,
                     "base_hold": BASE_HOLD, "max_hold": MAXH},
        "n_events_rising_edge": int(len(events)),
        "n_events_prev_round10": int(len(old)),
        "events_added_vs_round10": [{"stock_id": a, "signal_date": b} for a, b in added],
        "events_removed_vs_round10": [{"stock_id": a, "signal_date": b} for a, b in removed],
        "n_common_evaluable": int(len(common)),
        "exit_rule_comparison": rows,
        "permutation": perm_out,
        "bh_fdr": fdr,
        "horizon_shape": shape,
        "pyramid_retrigger": {
            "n_eligible": int(len(elig)), "n_total": int(len(pyr)),
            "eligible_base_mean_pct": float(elig["r_adj_pct"].mean()) if len(elig) else None,
            "eligible_pyramid_mean_pct": float(elig["r_adj_pyramid_pct"].mean()) if len(elig) else None,
        },
        "pyramid_underwater_rebound": ub_rows,
        "tier_descriptive": json.loads(tier_desc.reset_index().to_json(orient="records")),
        "spearman_buy5d_vs_radj": {"rho": float(rho.statistic), "p": float(rho.pvalue)},
        "weighting": wrows,
        "concurrency": cap_rows,
        "concurrency_distribution": {str(k): int(v) for k, v in sorted(dist.items())},
        "exit_rule_risk": risk_rows,
        "case_6449": case_rows,
        "concentration_ex6449": conc_rows,
        "capital_feasibility": feas,
        "capital_note": {
            "budget_per_slot_twd": BUDGET_PER_SLOT,
            "account_total_twd": ACCOUNT_TOTAL,
            "shared_with": ["tmf-channel", "dayflip-short", "momentum-rotation"],
        },
    }
    p = OUT_DIR / "songshan_m5_summary.json"
    p.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n[OK] {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
