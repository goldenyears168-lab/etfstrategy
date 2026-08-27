"""Item AC · 9217 branch-follow candidate screen 拆成多層漏斗，測每層邊際貢獻.

背景：leading-dip-orthogonal-compact 發現三層正交漏斗設計本身有效（B2簡化≡baseline，
notUR/W5 各自貢獻非冗餘資訊）。這支腳本把同一套「逐層加減、比較倖存者 forward-return
分布」方法論，套用在 9217（dayflip-futures-short-v1，FROZEN_SPEC_V1.json）的候選篩選上，
看目前「金額門檻 + flip門檻」兩腿的簡單篩選，加上 60日建倉排除／流動性(ADV)／
多席同步確認 這幾個「已計算但未當獨立漏斗層」的特徵，是否各自提供非冗餘的邊際選擇力。

唯讀 DB；不改 config/order.yaml、config/strategy.yaml、src/order/。輸出只寫
reports/research/branch_follow_funnel_architecture/。
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
import stock_db  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[2] / "reports/research/branch_follow_funnel_architecture"
OUT_DIR.mkdir(parents=True, exist_ok=True)

TRADER_ID = "9217"
DATE_START = "2024-07-01"
DATE_END = "2026-07-24"  # 留出 forward T+5 buffer（資料到 2026-08-07）

MIN_BUY_NTD = 30_000_000.0
FLIP_MIN = 0.40
ACC_WINDOW_DAYS = 60
ACC_NET_RATIO_MAX = 0.30
ACC_MIN_WINDOW_BUY_NTD = 100_000_000.0
ADV_MIN_LOTS = 800.0

SPEC_PATH = Path(__file__).resolve().parents[2] / "reports/research/branch-footprint-screen/dayflip_gapup_short/FROZEN_SPEC_V1.json"
UNIVERSE_PATH = Path(__file__).resolve().parents[2] / "reports/research/branch-footprint-screen/dayflip_gapup_short/stock_futures_universe.json"
MEGA_PATH = Path(__file__).resolve().parents[2] / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
FUT_CACHE_PATH = Path(__file__).resolve().parents[2] / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"


def load_json(p: Path) -> dict:
    return json.loads(p.read_text())


def main() -> int:
    spec = load_json(SPEC_PATH)
    seat_flip_table: dict[str, float] = dict(spec["seat_flip_table_frozen"]["values"])
    other_seats = set(seat_flip_table) - {TRADER_ID}
    mega = set(load_json(MEGA_PATH)["symbols"])
    futmap = load_json(UNIVERSE_PATH)["map"]
    fut_cache = load_json(FUT_CACHE_PATH) if FUT_CACHE_PATH.exists() else {}

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)

    lo = (date.fromisoformat(DATE_START) - timedelta(days=200)).isoformat()
    hi = (date.fromisoformat(DATE_END) + timedelta(days=15)).isoformat()

    print("[INFO] 讀 stock_daily_bars close 全市場面板...")
    px = pd.read_sql_query(
        "SELECT stock_id, trade_date, close FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date BETWEEN ? AND ? AND close>0",
        con, params=(lo, hi),
    )
    px["trade_date"] = px["trade_date"].astype(str)
    cal = sorted(px["trade_date"].unique().tolist())
    ci = {d: i for i, d in enumerate(cal)}
    close_map = {(r.stock_id, r.trade_date): r.close for r in px.itertuples()}

    print("[INFO] 讀 9217 全部 branch daily 交易...")
    tr9217 = pd.read_sql_query(
        "SELECT trade_date, stock_id, buy, sell FROM stock_broker_branch_daily "
        "WHERE securities_trader_id=? AND trade_date BETWEEN ? AND ?",
        con, params=(TRADER_ID, lo, DATE_END),
    )
    tr9217["trade_date"] = tr9217["trade_date"].astype(str)
    tr9217["stock_id"] = tr9217["stock_id"].astype(str)

    print("[INFO] 讀其他 23 席 branch daily 交易（同步確認用）...")
    other_seats_df = pd.read_sql_query(
        f"SELECT trade_date, securities_trader_id, stock_id, buy FROM stock_broker_branch_daily "
        f"WHERE securities_trader_id IN ({','.join('?' for _ in other_seats)}) "
        "AND trade_date BETWEEN ? AND ?",
        con, params=(*other_seats, lo, DATE_END),
    )
    other_seats_df["trade_date"] = other_seats_df["trade_date"].astype(str)
    other_seats_df["stock_id"] = other_seats_df["stock_id"].astype(str)

    # buy/sell dict for fast lookup: (trader_or_9217, stock, date) -> shares
    tr9217_idx = tr9217.set_index(["stock_id", "trade_date"])

    # ---- G0: baseline universe = 9217 T0 buy_amt >= 30M, ex-mega, ex-ETF, in futures universe
    rows = []
    for r in tr9217.itertuples():
        sid, d, b, s = r.stock_id, r.trade_date, r.buy, r.sell
        if not b or b <= 0:
            continue
        if sid in mega or sid.startswith("00") or sid not in futmap:
            continue
        p0 = close_map.get((sid, d))
        if p0 is None or d not in ci:
            continue
        amt = b * p0
        if amt < MIN_BUY_NTD:
            continue
        i = ci[d]
        if i + 1 >= len(cal) or i + 5 >= len(cal):
            continue  # 沒有足夠 forward 資料
        d1 = cal[i + 1]
        d5 = cal[i + 5]
        p1 = close_map.get((sid, d1))
        p5 = close_map.get((sid, d5))
        if p1 is None:
            continue
        s1 = float((tr9217_idx.loc[(sid, d1)]["sell"]) if (sid, d1) in tr9217_idx.index else 0.0)
        flip = s1 / b if b > 0 else np.nan
        rows.append({
            "stock_id": sid, "t0": d, "t1": d1,
            "buy_amt_ntd": amt, "buy_shares_t0": b,
            "flip_t1": flip,
            "ret_t0_t1": (p1 / p0) - 1.0,
            "ret_t0_t5": (p5 / p0 - 1.0) if p5 is not None else np.nan,
        })
    g0 = pd.DataFrame(rows)
    print(f"[INFO] G0（金額>=3000萬 · ex-mega/ETF · 在期貨宇宙）候選事件數：{len(g0)}")

    # ---- feature: 60d accumulation net_ratio (以 T0 前 60 交易日, 不含 T0)
    def net_ratio_60d(sid: str, d: str) -> float | None:
        i = ci.get(d)
        if i is None or i < ACC_WINDOW_DAYS:
            return None
        win_dates = cal[i - ACC_WINDOW_DAYS:i]
        sub = tr9217_idx.loc[(sid,)] if sid in tr9217_idx.index.get_level_values(0) else None
        if sub is None:
            return None
        sub = sub[sub.index.isin(win_dates)]
        if sub.empty:
            return None
        tb = ts = 0.0
        for dd, row in sub.iterrows():
            p = close_map.get((sid, dd))
            if p is None:
                continue
            tb += row["buy"] * p
            ts += row["sell"] * p
        if tb < ACC_MIN_WINDOW_BUY_NTD:
            return None
        return (tb - ts) / tb

    print("[INFO] 算 60 日建倉 net_ratio（每筆事件；可能較慢）...")
    g0["net_ratio_60d"] = [net_ratio_60d(sid, d) for sid, d in zip(g0["stock_id"], g0["t0"])]

    # ---- feature: ADV (期貨20日均量, 僅 cache 涵蓋的 83 檔有值)
    def adv_20d(sid: str, d: str) -> float | None:
        m = fut_cache.get(sid)
        if not m:
            return None
        ds = sorted(m)
        if d not in ds:
            return None
        i = ds.index(d)
        if i < 20:
            return None
        return float(np.mean([m[x][4] for x in ds[i - 20:i]]))

    g0["adv_20d"] = [adv_20d(sid, d) for sid, d in zip(g0["stock_id"], g0["t0"])]
    print(f"[INFO] ADV cache 覆蓋率：{g0['adv_20d'].notna().mean():.1%}（僅 futures_daily_cache 有的 83 檔含值）")

    # ---- feature: 多席同步確認（同日同股，其他高沖席也有 >=3000萬 買進事件）
    other_seats_df["amt"] = other_seats_df.apply(
        lambda r: r["buy"] * close_map.get((r["stock_id"], r["trade_date"]), np.nan), axis=1
    )
    other_hits = other_seats_df[other_seats_df["amt"] >= MIN_BUY_NTD]
    other_pairs = set(zip(other_hits["stock_id"], other_hits["trade_date"]))
    g0["multiseat_confirm"] = [(sid, d) in other_pairs for sid, d in zip(g0["stock_id"], g0["t0"])]
    print(f"[INFO] 多席同步確認命中率：{g0['multiseat_confirm'].mean():.1%}")

    g0.to_csv(OUT_DIR / "g0_candidate_events_with_features.csv", index=False)

    # ---- Gate boolean masks (現有兩腿 baseline: amount 已經在 G0，flip 為第二腿)
    gate_flip = g0["flip_t1"] >= FLIP_MIN
    gate_acc = g0["net_ratio_60d"].isna() | (g0["net_ratio_60d"] < ACC_NET_RATIO_MAX)
    has_adv = g0["adv_20d"].notna()
    gate_adv = has_adv & (g0["adv_20d"] >= ADV_MIN_LOTS)
    gate_seat = g0["multiseat_confirm"]

    def summarize(mask: pd.Series, label: str) -> dict:
        sub = g0[mask]
        r1 = sub["ret_t0_t1"].dropna()
        r5 = sub["ret_t0_t5"].dropna()
        return {
            "stage": label, "n": int(mask.sum()),
            "mean_ret_t1_pct": round(float(r1.mean() * 100), 3) if len(r1) else None,
            "median_ret_t1_pct": round(float(r1.median() * 100), 3) if len(r1) else None,
            "short_win_rate_t1_pct": round(float((r1 < 0).mean() * 100), 1) if len(r1) else None,
            "mean_ret_t5_pct": round(float(r5.mean() * 100), 3) if len(r5) else None,
            "median_ret_t5_pct": round(float(r5.median() * 100), 3) if len(r5) else None,
        }

    # ---- (A) 序列漏斗：G0 -> +flip -> +acc -> +seat（ADV 另列，因覆蓋率低）
    stages = []
    m = pd.Series(True, index=g0.index)
    stages.append(summarize(m, "G0_amount_only(baseline universe)"))
    m = m & gate_flip
    stages.append(summarize(m, "+flip>=0.40"))
    m = m & gate_acc
    stages.append(summarize(m, "+acc_excl(60d net_ratio<0.30)"))
    m = m & gate_seat
    stages.append(summarize(m, "+multiseat_confirm"))
    seq_df = pd.DataFrame(stages)
    seq_df.to_csv(OUT_DIR / "sequential_funnel_stages.csv", index=False)
    print("\n=== 序列漏斗 (amount -> flip -> acc_excl -> multiseat) ===")
    print(seq_df.to_string(index=False))

    # ADV as separate track (coverage-limited)
    adv_stage_base = summarize(has_adv, "ADV_covered_subset(pre-gate, amount-only)")
    adv_stage_gated = summarize(has_adv & gate_adv, "ADV_covered_subset(+adv>=800lots)")
    adv_df = pd.DataFrame([adv_stage_base, adv_stage_gated])
    adv_df.to_csv(OUT_DIR / "adv_gate_coverage_subset.csv", index=False)
    print("\n=== ADV 子集（僅 cache 覆蓋 83 檔）===")
    print(adv_df.to_string(index=False))

    # ---- (B) Leave-one-out 邊際貢獻測試（mirrors leading-dip B2）
    # full funnel（不含 ADV，因覆蓋率過低不適合當全樣本 gate）= amount ∩ flip ∩ acc ∩ seat
    full_mask = gate_flip & gate_acc & gate_seat
    gates = {"flip": gate_flip, "acc_excl": gate_acc, "multiseat": gate_seat}
    loo_rows = []
    full_summary = summarize(full_mask, "full_funnel(flip+acc+seat)")
    loo_rows.append(full_summary)
    for name, gmask in gates.items():
        without_mask = pd.Series(True, index=g0.index)
        for other_name, other_mask in gates.items():
            if other_name != name:
                without_mask = without_mask & other_mask
        loo_rows.append(summarize(without_mask, f"full_minus_{name}"))
        # statistical test: full vs without-this-gate, on ret_t0_t1
        a = g0.loc[full_mask, "ret_t0_t1"].dropna()
        b = g0.loc[without_mask, "ret_t0_t1"].dropna()
        if len(a) > 1 and len(b) > 1:
            u, p = stats.mannwhitneyu(a, b, alternative="two-sided")
        else:
            u, p = None, None
        loo_rows[-1]["mannwhitney_p_vs_full"] = round(float(p), 4) if p is not None else None
    loo_df = pd.DataFrame(loo_rows)
    loo_df.to_csv(OUT_DIR / "leave_one_out_marginal_gates.csv", index=False)
    print("\n=== Leave-one-out 邊際貢獻（vs full_funnel, MWU p-value on ret_t0_t1）===")
    print(loo_df.to_string(index=False))

    # ---- (C) Gate overlap / redundancy check（各 gate 各自「淘汰」的事件，Jaccard）
    excl = {
        "flip": set(g0.index[~gate_flip]),
        "acc_excl": set(g0.index[~gate_acc]),
        "multiseat": set(g0.index[~gate_seat]),
    }
    if has_adv.sum() > 20:
        excl["adv"] = set(g0.index[has_adv & ~gate_adv])
    jac_rows = []
    names = list(excl.keys())
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            inter = len(excl[a] & excl[b])
            union = len(excl[a] | excl[b])
            jac_rows.append({
                "gate_a": a, "gate_b": b,
                "n_excluded_a": len(excl[a]), "n_excluded_b": len(excl[b]),
                "n_overlap": inter, "jaccard": round(inter / union, 3) if union else None,
            })
    jac_df = pd.DataFrame(jac_rows)
    jac_df.to_csv(OUT_DIR / "gate_exclusion_overlap.csv", index=False)
    print("\n=== Gate 淘汰對象重疊度（Jaccard，越高越冗餘）===")
    print(jac_df.to_string(index=False))

    print(f"\n[DONE] 全部輸出寫入 {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
