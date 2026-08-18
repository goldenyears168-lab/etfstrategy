#!/usr/bin/env python3
"""songshan_m1 追加診斷：覆蓋率乾淨子宇宙 + 6449 歸屬 + floor 效果分解.

回答三件事（皆為 songshan_m1_threshold_dimensions.py 跑完後的追問）：

A) **覆蓋率乾淨子宇宙**（本輪最重要的一項）
   9217 tape 的價格覆蓋率整體僅 ~50%，且 2024（39%）遠低於 2026（72%），
   代表整個研究母體都建立在「被 INNER JOIN 靜默丟掉一半活動」的資料上，
   而且會隨每次 backfill 事後變動。
   解法：只保留「研究窗內價格幾乎不缺」的股票（priced_days / calendar_days >= COV_MIN）。
   對這批股票，buy_5d/net_ratio 的計算本來就是完整的、不受 backfill 影響——
   這是唯一一個「母體不會再變」的子樣本。floor 效果若只在髒的那半邊存在，就是偏誤。

B) 6449 那 4 筆災難事件落在哪些分層（floor / net_ratio / 集中度）——濾網事前避得開嗎？

C) floor 0.5→0.75 億到底刪掉了什麼：被刪掉的那批事件自己的表現。

DB 唯讀 · 不改任何 config。
  PYTHONPATH=src .venv/bin/python scripts/research/songshan_m1_coverage_clean_diagnostics.py
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
from scipy import stats  # noqa: E402

from research.branch_signal_validation import build_l1h7_signal_dict  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "m1", ROOT / "scripts" / "research" / "songshan_m1_threshold_dimensions.py"
)
M = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(M)

COV_MIN = 0.95           # 預先宣告：priced_days / 該股在窗內應有的交易日 >= 95% 才算乾淨
OUT_DIR = M.OUT_DIR
PREFIX = M.PREFIX


def main() -> int:
    conn = M.connect_ro()
    mega = M.load_mega()
    calendar = M.load_calendar(conn)
    cal_set = set(calendar)
    raw = M.load_raw_activity(conn)
    panel = M.build_panel(raw, calendar)
    ix = M.load_ix(conn)
    ix_dict = build_l1h7_signal_dict(ix)
    out: dict = {"cov_min": COV_MIN, "n_calendar_days": len(calendar)}

    # ---------------- A) 覆蓋率乾淨子宇宙 ----------------
    M.section("A) 覆蓋率乾淨子宇宙（priced_days / calendar_days >= 95%）")
    # 每檔股票在研究窗內的有價天數（以 2330 交易日曆為分母）
    cov = pd.read_sql_query(
        """
        SELECT stock_id, COUNT(*) AS priced_days,
               MIN(trade_date) AS first_bar, MAX(trade_date) AS last_bar
        FROM stock_daily_bars
        WHERE source=? AND trade_date BETWEEN ? AND ? AND close>0
          AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND stock_id NOT GLOB '00*'
        GROUP BY stock_id
        """,
        conn, params=(M.SOURCE, M.STUDY_START, M.STUDY_END),
    )
    cov["cov"] = cov["priced_days"] / len(calendar)
    clean = set(cov.loc[cov["cov"] >= COV_MIN, "stock_id"].astype(str))
    tape_stocks = set(raw["stock_id"].astype(str))
    print(f"  研究窗交易日 {len(calendar)} 天；有價股票 {len(cov)} 檔；"
          f"覆蓋率>= {COV_MIN:.0%} 的乾淨股票 {len(clean)} 檔")
    print(f"  9217 tape 碰過的股票 {len(tape_stocks)} 檔，其中乾淨的 {len(tape_stocks & clean)} 檔")
    out["n_clean_stocks"] = len(clean)
    out["n_tape_stocks"] = len(tape_stocks)
    out["n_tape_clean"] = len(tape_stocks & clean)

    base_ev_all = M.events_for(panel, mega, 0.5e8, 0.95)
    sids = sorted(set(base_ev_all["stock_id"]))
    bars_cache = {s: M.load_stock_bars(conn, s) for s in sids}
    stock_dicts = {s: build_l1h7_signal_dict(bars_cache[s]) for s in sids}

    ev_clean_flag = base_ev_all["stock_id"].isin(clean)
    print(f"\n  基準母體 n=53 中，落在乾淨股票的 = {int(ev_clean_flag.sum())}，"
          f"落在髒（曾缺價）股票的 = {int((~ev_clean_flag).sum())}")

    rows = []
    clean_floor_trades = {}
    for f in M.FLOORS_YI:
        ev = M.events_for(panel, mega, f * 1e8, 0.95)
        ev_c = ev[ev["stock_id"].isin(clean)].reset_index(drop=True)
        tr, _ = M.build_trades(ev_c, bars_cache, ix)
        clean_floor_trades[f] = tr
        st = M.desc(tr)
        rows.append({"floor_yi": f, "n": st["n"], "mean": st["mean"], "median": st["median"],
                     "win_rate": st["win_rate"], "trim3_mean": st["trim3_mean"],
                     "t_p": st["t_p"], "wil_p": st["wil_p"],
                     "insufficient": st["insufficient_sample"]})
        print(f"  [乾淨] floor {f}億: n={st['n']} mean={st['mean']} med={st['median']} "
              f"win={st['win_rate']} trim3={st['trim3_mean']} t_p={st['t_p']}"
              f"{'  ⚠樣本不足' if st['insufficient_sample'] else ''}")
    # 髒子宇宙對照
    rows_dirty = []
    for f in M.FLOORS_YI:
        ev = M.events_for(panel, mega, f * 1e8, 0.95)
        ev_d = ev[~ev["stock_id"].isin(clean)].reset_index(drop=True)
        tr, _ = M.build_trades(ev_d, bars_cache, ix)
        st = M.desc(tr)
        rows_dirty.append({"floor_yi": f, "n": st["n"], "mean": st["mean"],
                           "median": st["median"], "win_rate": st["win_rate"],
                           "trim3_mean": st["trim3_mean"], "t_p": st["t_p"],
                           "insufficient": st["insufficient_sample"]})
        print(f"  [髒  ] floor {f}億: n={st['n']} mean={st['mean']} med={st['median']} "
              f"win={st['win_rate']} trim3={st['trim3_mean']}"
              f"{'  ⚠樣本不足' if st['insufficient_sample'] else ''}")
    out["clean_floor_sweep"] = rows
    out["dirty_floor_sweep"] = rows_dirty
    pd.DataFrame(rows).assign(universe="clean").to_csv(
        OUT_DIR / f"{PREFIX}coverage_clean_floor_sweep.csv", index=False)
    pd.DataFrame(rows_dirty).assign(universe="dirty").to_csv(
        OUT_DIR / f"{PREFIX}coverage_dirty_floor_sweep.csv", index=False)

    # 乾淨子宇宙的 permutation（只跑 0.5 / 0.75 / 1.0）
    print("\n  乾淨子宇宙 permutation（20000×3 seeds）：")
    out["clean_perm"] = {}
    for f in (0.5, 0.75, 1.0):
        pm = M.multi_seed_perm(clean_floor_trades[f], stock_dicts, ix_dict)
        out["clean_perm"][str(f)] = pm
        print(f"    floor {f}億: n={len(clean_floor_trades[f])} "
              f"obs_mean={pm.get('obs_mean')} placebo_mean={pm.get('placebo_mean')} "
              f"obs_med={pm.get('obs_median')} placebo_med={pm.get('placebo_median')} "
              f"p_mean={pm.get('p_mean_worst')} p_med={pm.get('p_median_worst')}")

    # 乾淨子宇宙的 WF-A OOS
    cut = "2025-10-07"
    print(f"\n  乾淨子宇宙 WF-A（OOS >= {cut}）：")
    out["clean_wf_a"] = {}
    for f in M.FLOORS_YI:
        tr = clean_floor_trades[f]
        i_, o_ = M.desc(tr[tr.signal_date < cut]), M.desc(tr[tr.signal_date >= cut])
        out["clean_wf_a"][str(f)] = {"IS": i_, "OOS": o_}
        print(f"    floor {f}億 IS n={i_['n']} mean={i_['mean']} | "
              f"OOS n={o_['n']} mean={o_['mean']} med={o_['median']}")

    # ---------------- B) 6449 歸屬 ----------------
    M.section("B) 6449 四筆災難事件落在哪些分層（濾網事前避得開嗎？）")
    base_tr, _ = M.build_trades(base_ev_all, bars_cache, ix)
    med_conc = float(base_tr["conc"].median())
    s = base_tr[base_tr.stock_id == "6449"].copy()
    s["net_stratum"] = np.where(s.net_ratio >= 0.99999, "==1.000",
                                np.where(s.net_ratio >= 0.98, "[0.98,1.00)", "[0.95,0.98)"))
    s["conc_group"] = np.where(s.conc >= med_conc, "high", "low")
    s["floor_pass_0.75"] = s.buy_5d >= 0.75e8
    s["floor_pass_1.0"] = s.buy_5d >= 1.0e8
    s["floor_pass_2.0"] = s.buy_5d >= 2.0e8
    print(f"  （集中度中位數切點 = {med_conc:.3f}；6449 在 tape 覆蓋率 = "
          f"{float(cov.loc[cov.stock_id=='6449','cov'].iloc[0]) if (cov.stock_id=='6449').any() else float('nan'):.2f}）")
    print(s[["signal_date", "buy_5d", "net_ratio", "conc", "net_stratum", "conc_group",
             "floor_pass_0.75", "floor_pass_1.0", "floor_pass_2.0", "r_adj_pct"]].to_string(index=False))
    out["6449_attribution"] = json.loads(s.to_json(orient="records"))
    # 6449 也是「髒」股票嗎
    out["6449_is_clean"] = "6449" in clean

    # ---------------- C) floor 0.5→0.75 刪掉了什麼 ----------------
    M.section("C) floor 0.5 → 0.75 億：被刪掉的事件自己表現如何")
    ev75 = M.events_for(panel, mega, 0.75e8, 0.95)
    k75 = set(zip(ev75.stock_id, ev75.signal_date))
    dropped = base_tr[[(r.stock_id, r.signal_date) not in k75
                       for r in base_tr.itertuples(index=False)]]
    kept = base_tr[[(r.stock_id, r.signal_date) in k75
                    for r in base_tr.itertuples(index=False)]]
    print(f"  被刪 n={len(dropped)}: mean={dropped.r_adj_pct.mean():.3f} "
          f"med={dropped.r_adj_pct.median():.3f} win={(dropped.r_adj_pct>0).mean()*100:.1f}%")
    print(f"  留下 n={len(kept)}: mean={kept.r_adj_pct.mean():.3f} "
          f"med={kept.r_adj_pct.median():.3f} win={(kept.r_adj_pct>0).mean()*100:.1f}%")
    print("\n  被刪明細：")
    print(dropped[["signal_date", "stock_id", "buy_5d", "net_ratio", "r_adj_pct"]]
          .sort_values("r_adj_pct").to_string(index=False))
    if len(dropped) >= 3 and len(kept) >= 3:
        print("\n  留下 vs 被刪 差異：Welch t p=%.4f  MannWhitney p=%.4f" % (
            stats.ttest_ind(kept.r_adj_pct, dropped.r_adj_pct, equal_var=False).pvalue,
            stats.mannwhitneyu(kept.r_adj_pct, dropped.r_adj_pct, alternative="two-sided").pvalue))
        out["floor_step_0p5_to_0p75"] = {
            "dropped_n": len(dropped), "dropped_mean": round(float(dropped.r_adj_pct.mean()), 3),
            "kept_n": len(kept), "kept_mean": round(float(kept.r_adj_pct.mean()), 3),
            "welch_p": round(float(stats.ttest_ind(kept.r_adj_pct, dropped.r_adj_pct,
                                                   equal_var=False).pvalue), 4),
            "mw_p": round(float(stats.mannwhitneyu(kept.r_adj_pct, dropped.r_adj_pct,
                                                   alternative="two-sided").pvalue), 4),
        }
    # 排除 6449 之後 floor 效果還在嗎
    M.section("C2) 完全剔除 6449 之後的 floor sweep（檢查 floor 效果是不是只有 6449 移除）")
    rows_no6449 = []
    for f in M.FLOORS_YI:
        ev = M.events_for(panel, mega, f * 1e8, 0.95)
        ev = ev[ev.stock_id != "6449"].reset_index(drop=True)
        tr, _ = M.build_trades(ev, bars_cache, ix)
        st = M.desc(tr)
        rows_no6449.append({"floor_yi": f, "n": st["n"], "mean": st["mean"],
                            "median": st["median"], "win_rate": st["win_rate"],
                            "trim3_mean": st["trim3_mean"], "t_p": st["t_p"]})
        print(f"  floor {f}億(無6449): n={st['n']} mean={st['mean']} med={st['median']} "
              f"win={st['win_rate']} trim3={st['trim3_mean']} t_p={st['t_p']}")
    out["floor_sweep_ex6449"] = rows_no6449

    (OUT_DIR / f"{PREFIX}coverage_clean_diagnostics.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n[OK] wrote {OUT_DIR / (PREFIX + 'coverage_clean_diagnostics.json')}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
