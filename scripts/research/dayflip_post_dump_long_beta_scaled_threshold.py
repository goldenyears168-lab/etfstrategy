#!/usr/bin/env python3
"""設計D：把rolling_relative_dip的固定LAG_THRESHOLD_PCT=0.3%改成beta縮放門檻。

使用者假設：現行門檻(0.3%)全股共用，沒考慮到beta不同的股票"正常"局部落後
幅度本來就不同(beta越高，同樣大盤波動下個股相對變動理應更大)。這裡測試：
beta_scaled_threshold = 0.3% * max(beta, 0.3)，用這個取代固定0.3%重跑
find_rolling_dip_signal()的判定邏輯，看重新判定出的訊號子集整體表現
（勝率/平均報酬/sharpe_like）是否優於固定門檻版(219筆，fgap>=4%子集)。

beta算法：T0之前60個交易日，個股日報酬對0050日報酬OLS回歸斜率
(跟其他分支的"設計A"同一套算法，np.polyfit次數1)。

比較方法（非IC，是群體比較）：
  - 用bootstrap對「訊號日」重抽3000次，比較 sharpe_like = mean(ret)/std(ret)
    的分布：beta縮放版 - 固定門檻版。看差值分布的方向與>0比例(當雙尾p值用)。
  - train(entry_day排序前70%) / test(後30%) 分開各跑一次bootstrap，兩者
    都要同方向支持才算SUPPORTED。
  - IC相關欄位留空/填N/A，這個設計本質是比較兩個訊號判定規則產生的群體，
    不是單一連續特徵對報酬的秩相關。

PIT注意：beta只用T0之前的日線資料(不含T0本身)，訊號本身用當天(entry_day)
1分K，這跟原始219筆訊號的資料時窗一致，沒有引入未來資訊。

PYTHONPATH=src .venv/bin/python scripts/research/dayflip_post_dump_long_beta_scaled_threshold.py
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np

import stock_db
from stock_db.kbar import load_kbar_day_bars

ROOT = Path(__file__).resolve().parents[2]
RESULTS_CACHE = ROOT / "reports/research/dayflip_fgap_calibration/post_dump_long_rolling_dip_results.json"

BENCH = "0050"
ROLLING_WINDOW_MIN = 15  # 跟現行rolling_relative_dip同一個窗口，才可比較
CONFIRM_MINUTES = 10
BASE_LAG_THRESHOLD_PCT = 0.3  # 現行固定門檻
BETA_LOOKBACK_DAYS = 60  # T0之前60個交易日算beta
BETA_FLOOR = 0.3  # max(beta, 0.3)，避免低beta股門檻被壓到近零
N_BOOTSTRAP = 3000
SEED = 20260811


def load_minute_closes(con: sqlite3.Connection, stock_id: str, day: str) -> dict[str, float]:
    raw = load_kbar_day_bars(con, stock_id, day)
    return {
        b.minute[:5]: b.close
        for b in raw
        if "09:00" <= b.minute[:5] <= "13:30" and b.close and b.close > 0
    }


def compute_beta(con: sqlite3.Connection, stock_id: str, t0: str) -> float | None:
    """T0之前(不含T0) BETA_LOOKBACK_DAYS個交易日的日報酬beta(對0050 OLS斜率)。
    嚴格PIT：只用trade_date < t0的資料，訊號當天(entry_day=T0+1)盤中不會
    用到未來資訊。"""
    stock_rows = con.execute(
        "SELECT trade_date, close FROM stock_daily_bars "
        "WHERE stock_id=? AND source='finmind' AND close>0 AND trade_date<? "
        "ORDER BY trade_date DESC LIMIT ?",
        (stock_id, t0, BETA_LOOKBACK_DAYS + 1),
    ).fetchall()
    bench_rows = con.execute(
        "SELECT trade_date, close FROM stock_daily_bars "
        "WHERE stock_id=? AND source='finmind' AND close>0 AND trade_date<? "
        "ORDER BY trade_date DESC LIMIT ?",
        (BENCH, t0, BETA_LOOKBACK_DAYS + 1),
    ).fetchall()
    if len(stock_rows) < 30 or len(bench_rows) < 30:
        return None
    stock_map = {r["trade_date"]: r["close"] for r in stock_rows}
    bench_map = {r["trade_date"]: r["close"] for r in bench_rows}
    dates = sorted(set(stock_map) & set(bench_map))
    if len(dates) < 30:
        return None
    s_close = [stock_map[d] for d in dates]
    b_close = [bench_map[d] for d in dates]
    s_ret = np.diff(s_close) / np.asarray(s_close[:-1])
    b_ret = np.diff(b_close) / np.asarray(b_close[:-1])
    if len(s_ret) < 20 or np.std(b_ret) == 0:
        return None
    beta = float(np.polyfit(b_ret, s_ret, 1)[0])
    return beta


def find_rolling_dip_signal(
    stock_closes: dict[str, float], bench_closes: dict[str, float], lag_threshold_pct: float,
) -> tuple[str, float] | None:
    """跟dayflip_short_rolling_relative_dip_signal.py的find_rolling_dip_signal()
    完全同一套邏輯，只是lag_threshold_pct改為外部傳入的beta縮放值。"""
    minutes = sorted(set(stock_closes) & set(bench_closes))
    if len(minutes) < 50:
        return None

    rolling_lag = {}
    for i, m in enumerate(minutes):
        if i < ROLLING_WINDOW_MIN:
            continue
        m0 = minutes[i - ROLLING_WINDOW_MIN]
        stock_ret = (stock_closes[m] / stock_closes[m0] - 1) * 100
        bench_ret = (bench_closes[m] / bench_closes[m0] - 1) * 100
        rolling_lag[m] = stock_ret - bench_ret

    lag_minutes = sorted(rolling_lag)
    if not lag_minutes:
        return None

    worst_idx = None
    worst_val = 0.0
    for i, m in enumerate(lag_minutes):
        if rolling_lag[m] < -lag_threshold_pct and rolling_lag[m] < worst_val:
            worst_val = rolling_lag[m]
            worst_idx = i
    if worst_idx is None:
        return None

    worst_minute = lag_minutes[worst_idx]
    for i in range(worst_idx + 1, len(lag_minutes)):
        m = lag_minutes[i]
        elapsed = i - worst_idx
        if elapsed >= CONFIRM_MINUTES and rolling_lag[m] > rolling_lag[worst_minute] * 0.5:
            return m, stock_closes[m]
    return None


def sharpe_like(rets: list[float]) -> float:
    if len(rets) < 2:
        return float("nan")
    arr = np.array(rets)
    std = arr.std(ddof=1)
    return float(arr.mean() / std) if std > 0 else float("nan")


def bootstrap_sharpe_diff(scaled_rets: list[float], fixed_rets: list[float], rng: np.random.Generator) -> np.ndarray:
    """對「訊號日」（即該群體的交易列表）重抽，各自算sharpe_like，回傳
    N_BOOTSTRAP次(scaled - fixed)的差值陣列。兩群體樣本數可能不同(因為
    beta縮放門檻會改變誰觸發訊號)，各自獨立重抽，樣本數維持各自原始n。"""
    scaled_arr = np.array(scaled_rets)
    fixed_arr = np.array(fixed_rets)
    diffs = np.empty(N_BOOTSTRAP)
    for i in range(N_BOOTSTRAP):
        s_samp = rng.choice(scaled_arr, size=len(scaled_arr), replace=True)
        f_samp = rng.choice(fixed_arr, size=len(fixed_arr), replace=True)
        s_std = s_samp.std(ddof=1) if len(s_samp) > 1 else 0.0
        f_std = f_samp.std(ddof=1) if len(f_samp) > 1 else 0.0
        s_sh = s_samp.mean() / s_std if s_std > 0 else 0.0
        f_sh = f_samp.mean() / f_std if f_std > 0 else 0.0
        diffs[i] = s_sh - f_sh
    return diffs


def main() -> None:
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    trades = json.loads(RESULTS_CACHE.read_text(encoding="utf-8"))
    sub = [t for t in trades if t["fgap"] >= 4.0]
    print(f"候選集(fgap>=4%): {len(sub)}筆")

    prepared = []
    n_no_beta = 0
    n_no_kbar = 0
    for t in sub:
        sid, t0, entry_day = t["stock_id"], t["t0"], t["entry_day"]
        beta = compute_beta(con, sid, t0)
        if beta is None:
            n_no_beta += 1
            continue
        stock_closes = load_minute_closes(con, sid, entry_day)
        bench_closes = load_minute_closes(con, BENCH, entry_day)
        if len(stock_closes) < 50 or len(bench_closes) < 50:
            n_no_kbar += 1
            continue
        prepared.append({
            "stock_id": sid, "t0": t0, "entry_day": entry_day, "ret": t["ret"],
            "beta": beta, "stock_closes": stock_closes, "bench_closes": bench_closes,
        })

    print(f"可算beta且有1分K: {len(prepared)}/{len(sub)}筆 (無beta={n_no_beta}, 無1分K={n_no_kbar})\n")

    betas = np.array([p["beta"] for p in prepared])
    print(f"beta分布: min={betas.min():.2f} median={np.median(betas):.2f} max={betas.max():.2f} "
          f"(<{BETA_FLOOR}地板值比例={float(np.mean(betas < BETA_FLOOR))*100:.0f}%)\n")

    # 固定門檻版：直接用JSON裡現成的ret（因為219筆本來就是固定0.3%門檻判定出的訊號）
    fixed_group = [{"entry_day": p["entry_day"], "ret": p["ret"]} for p in prepared]

    # beta縮放版：重新用find_rolling_dip_signal()判定訊號是否觸發，
    # 觸發才算入這個群體（觸發用的還是同一筆t["ret"]，因為出場邏輯沒有變，
    # 只是「進場門檻判定」用beta縮放版重算一次，判定結果=是否進場）
    scaled_group = []
    n_scaled_no_trigger = 0
    for p in prepared:
        th = BASE_LAG_THRESHOLD_PCT * max(p["beta"], BETA_FLOOR)
        sig = find_rolling_dip_signal(p["stock_closes"], p["bench_closes"], th)
        if sig is None:
            n_scaled_no_trigger += 1
            continue
        scaled_group.append({"entry_day": p["entry_day"], "ret": p["ret"]})

    print(f"固定0.3%門檻版（原始基準）: n={len(fixed_group)}")
    print(f"beta縮放門檻版: n={len(scaled_group)} (未觸發={n_scaled_no_trigger}/{len(prepared)}, "
          f"重疊率={len(scaled_group)/len(prepared)*100:.0f}%)\n")

    def summarize(label: str, group: list[dict]) -> None:
        rets = [g["ret"] for g in group]
        if not rets:
            print(f"[{label}] n=0")
            return
        arr = np.array(rets)
        print(f"[{label}] n={len(arr)} 勝率={float(np.mean(arr>0))*100:.0f}% "
              f"均報酬={arr.mean():+.3f}% std={arr.std(ddof=1):.3f} sharpe_like={sharpe_like(rets):.3f}")

    print("=== 全樣本 ===")
    summarize("固定0.3%門檻", fixed_group)
    summarize("beta縮放門檻", scaled_group)

    # train/test split：用entry_day排序前70%/後30%（對prepared全集切，
    # 兩個群體各自取落在該日期集合內的子集）
    dates_sorted = sorted({p["entry_day"] for p in prepared})
    split_idx = int(len(dates_sorted) * 0.7)
    train_dates = set(dates_sorted[:split_idx])
    test_dates = set(dates_sorted[split_idx:])

    fixed_train = [g for g in fixed_group if g["entry_day"] in train_dates]
    fixed_test = [g for g in fixed_group if g["entry_day"] in test_dates]
    scaled_train = [g for g in scaled_group if g["entry_day"] in train_dates]
    scaled_test = [g for g in scaled_group if g["entry_day"] in test_dates]

    print(f"\n=== Train (entry_day前70%, {len(train_dates)}個日期) ===")
    summarize("固定0.3%門檻", fixed_train)
    summarize("beta縮放門檻", scaled_train)

    print(f"\n=== Test (entry_day後30%, {len(test_dates)}個日期) ===")
    summarize("固定0.3%門檻", fixed_test)
    summarize("beta縮放門檻", scaled_test)

    rng = np.random.default_rng(SEED)

    def bootstrap_report(label: str, scaled_g: list[dict], fixed_g: list[dict]) -> tuple[float, float]:
        scaled_rets = [g["ret"] for g in scaled_g]
        fixed_rets = [g["ret"] for g in fixed_g]
        if len(scaled_rets) < 5 or len(fixed_rets) < 5:
            print(f"[{label}] 樣本數過少(scaled={len(scaled_rets)}, fixed={len(fixed_rets)})，跳過bootstrap")
            return float("nan"), float("nan")
        diffs = bootstrap_sharpe_diff(scaled_rets, fixed_rets, rng)
        mean_diff = float(diffs.mean())
        pct_positive = float(np.mean(diffs > 0))
        two_sided_p = float(2 * min(pct_positive, 1 - pct_positive))
        ci_lo, ci_hi = np.percentile(diffs, [2.5, 97.5])
        print(f"[{label}] bootstrap(sharpe_like差 = beta縮放-固定): mean={mean_diff:+.3f} "
              f"95%CI=[{ci_lo:+.3f},{ci_hi:+.3f}] P(diff>0)={pct_positive*100:.1f}% two_sided_p={two_sided_p:.3f}")
        return mean_diff, two_sided_p

    print("\n=== Bootstrap比較 (N=3000, 對訊號日重抽) ===")
    full_diff, full_p = bootstrap_report("全樣本", scaled_group, fixed_group)
    train_diff, train_p = bootstrap_report("Train", scaled_train, fixed_train)
    test_diff, test_p = bootstrap_report("Test", scaled_test, fixed_test)

    print(
        "\n⚠️ 限制：\n"
        "  1) 用0050取代台指期（同其他輪，FinMind無期貨1分K資料集）。\n"
        "  2) beta縮放版的「訊號子集」是固定0.3%版219筆(prepared子集)的子集合再篩選——\n"
        "     不是獨立重新掃描全市場找新訊號，因為只有這219(可算子集)筆有現成entry_px/ret\n"
        "     可用。若beta縮放門檻理應讓『原本沒觸發』的股票新增訊號，這裡驗證不到，\n"
        "     只能驗證『原本有觸發的訊號裡，哪些換了門檻後會被濾掉/留下，留下的表現如何』。\n"
        "  3) beta用60個交易日OLS，只測1組(無額外掃描回看天數)；BETA_FLOOR=0.3為單一\n"
        "     合理值，沒有sweep。\n"
        "  4) train/test日期集合下的訊號數可能偏少，bootstrap在小樣本下區間會偏寬。"
    )

    train_support = train_diff > 0 if not np.isnan(train_diff) else False
    test_support = test_diff > 0 if not np.isnan(test_diff) else False
    both_same_direction = train_support and test_support

    if both_same_direction and train_p < 0.10 and test_p < 0.10:
        verdict = "SUPPORTED"
    elif np.isnan(train_diff) or np.isnan(test_diff):
        verdict = "INCONCLUSIVE"
    elif train_support != test_support:
        verdict = "NOT_SUPPORTED"
    else:
        verdict = "INCONCLUSIVE"

    print(f"\n=== 結論 ===")
    print(f"Train方向: {'beta縮放較好' if train_support else '固定門檻較好或持平'} (p={train_p:.3f})")
    print(f"Test方向: {'beta縮放較好' if test_support else '固定門檻較好或持平'} (p={test_p:.3f})")
    print(f"Verdict: {verdict}")

    out = {
        "n_prepared": len(prepared),
        "n_fixed": len(fixed_group),
        "n_scaled": len(scaled_group),
        "full_sample_sharpe_diff": full_diff,
        "full_sample_p": full_p,
        "train_sharpe_diff": train_diff,
        "train_p": train_p,
        "test_sharpe_diff": test_diff,
        "test_p": test_p,
        "verdict": verdict,
    }
    out_path = ROOT / "reports/research/dayflip_fgap_calibration/beta_scaled_threshold_result.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n結果已寫入 {out_path}")


if __name__ == "__main__":
    main()
