#!/usr/bin/env python3
"""net_disp 多天期橫斷面驗證（research only，唯讀 DB）.

背景（前三輪既有結論，本腳本不重做）
------------------------------------
1. 單股時序（2303 聯電）：24+8 支訊號、三層校正後 0 支通過 |t|>2；主因「當日大漲→隔夜
   跳空」這個 confound 在時序設計裡控不乾淨，而它本身是 2025-09 之後才出現的 regime。
2. 橫斷面（35 檔個股期、10.5M 筆分點、496 天）：6 支通過訓練/測試同號 + |t 測試|>2。
   同日等權多空對沖把上述 confound 結構性消掉。
3. 但隔夜天期毛價差只有 +0.10~+0.32%，而多空兩腿進出 4 次穿價的成本 ≈ 0.77%
   → 隔夜不可交易。
4. 對照組 9217（凱基-松山）在 H7 是 mean +5.66% / median +6.28% / permutation p<0.0001。

→ 本腳本要回答的唯一問題：**成本不隨天期變，訊號幅度隨天期成長，那要幾天才跨過 0.77%？**

訊號定義（照抄，未改）
----------------------
對每個 (stock_id, trade_date)：
    net_disp = std(該股當日所有分點的淨買金額) / (當日成交量 × 當日收盤價)
分點淨買金額 = net × close；只納入「該分點在該股已有 >= 30 個交易日紀錄」者
（PIT：expanding 統計一律 shift(1)）。
方向假設（**預註記，非事後選方向**）：負向 —— net_disp 低 → 後續報酬高。
依據：單股時序 t=1.75~2.28、35 檔橫斷面 IC 訓練 −0.035 / 測試 −0.062（NW t=−3.50），
兩種獨立設計方向一致。

持有期協議（L1H*，沿用 src/research/branch_signal_validation.py 的語意）
------------------------------------------------------------------------
index-based，不查日曆：訊號日 index i → 進場 open[i+1]、出場 close[i+H]。
H=1 即「T+1 開盤進、T+1 收盤出」。右設限（i+H >= n）直接丟棄，不補 0。
**T 收盤→T+1 開盤的隔夜跳空刻意被排除在 L1H* 之外**：net_disp 需要當日分點資料，
台灣分點明細是收盤後才發布，T 收盤不可能進場。另外報一版 C2C（含跳空）純作診斷，
標註為 not tradable。

已知陷阱與本腳本的對應處理
--------------------------
* 重疊持有期 → 每日 IC 序列自相關。t 值一律 Newey-West，lag = max(5, H)；
  另報 lag = 2H 作敏感度（H 大時 max(5,H) 有可能仍偏低估變異數）。
* 「H14/H20 顯著但 H7 不顯著」是報酬 std 隨天期擴大造成的統計假象（RESEARCH_SUMMARY
  陷阱 #1）→ 必跑去極值敏感度（剔除 |日 IC| 最大 5% 交易日）與 IC>0 天數比例。
* 多重檢定：7 天期 × 2 標的 = 14 格，α=0.05 雙尾的隨機期望通過數 ≈ 0.64 格。
  每格 p 值全部落 CSV（陷阱 #7：網格搜尋每格都要存 p 值），供事後 BH-FDR。
* 不用測試期挑天期再報測試期（循環）→ 天期以訓練期挑、測試期驗，腳本明確分開印。
* null 用 **date-preserving cross-sectional permutation**（同日打散哪幾檔被選中，
  保留當日檔數與市場因子結構），不是 branch-level 那個「同股隨機時機」null ——
  後者會打散日期橫斷面結構、把已對沖掉的 confound 灌回 null。
* 成本 per-leg 參數化：單邊 tick 成本中位 0.192% of notional，多空兩腿進出 4 次穿價
  = 0.768%（報告寫 0.77%）。**不得用 branch protocol 的 COST_DEFAULT=0.003 蓋過去**。
* 有效樣本數 = distinct trade_date 數（同日多檔高度相關），不是 (stock, date) 筆數。

用法
----
    PYTHONPATH=src .venv/bin/python scripts/research/branch_netdisp_multihorizon_validation.py
    # 強制從 DB 重算 net_disp 面板（否則吃 /tmp/cs_panel.pkl 快取）
    PYTHONPATH=src .venv/bin/python scripts/research/branch_netdisp_multihorizon_validation.py --rebuild
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import stock_db  # noqa: E402

SPLIT = "2025-09-01"          # 訓練 < SPLIT <= 測試
MIN_HIST = 30                 # 分點在該股需累積的交易日數（PIT）
MIN_NAMES = 12                # 當日至少要有這麼多檔才計橫斷面 IC
HORIZONS = (1, 3, 5, 7, 10, 14, 20)
QUANTILE = 0.2                # 五分位
COST_PER_CROSS = 0.00192      # 單邊 tick 成本中位（35 檔宇宙）
N_CROSSINGS = 4               # 多空兩腿 × 進出
COST_LS = COST_PER_CROSS * N_CROSSINGS   # 0.00768
N_PERM = 2000
SEEDS = (20260809, 7, 424242)

PANEL_CACHE = Path("/tmp/cs_panel.pkl")
OUT = ROOT / "reports/research/branch-footprint-screen"
FUTUNIV = Path("/tmp/futuniv.pkl")
FUTJSON = OUT / "dayflip_gapup_short/futures_liquidity_screen.json"


# ---------------------------------------------------------------- universe

def universe() -> list[str]:
    if FUTUNIV.exists():
        return sorted(pd.read_pickle(FUTUNIV).index.astype(str).tolist())
    raw = json.loads(FUTJSON.read_text())
    rows = raw["rows"] if isinstance(raw, dict) and "rows" in raw else raw
    return sorted(str(r["stock_id"]) for r in rows if float(r.get("recent_avg_lots", 0)) >= 2000)


# ---------------------------------------------------------------- data

def load_prices(univ: list[str]) -> pd.DataFrame:
    conn = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    try:
        ids = ",".join(f"'{s}'" for s in univ)
        px = pd.read_sql(
            f"select stock_id,trade_date,open,close,volume from stock_daily_bars "
            f"where stock_id in ({ids}) and trade_date>='2024-06-01' and close>0 "
            f"order by stock_id,trade_date", conn)
    finally:
        conn.close()
    px["open"] = px["open"].where(px["open"] > 0, px["close"])   # open 缺值 fallback close
    return px.sort_values(["stock_id", "trade_date"]).reset_index(drop=True)


def build_netdisp(univ: list[str], px: pd.DataFrame) -> pd.DataFrame:
    """從 stock_broker_branch_daily 重算 net_disp（PIT）。"""
    conn = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    try:
        ids = ",".join(f"'{s}'" for s in univ)
        br = pd.read_sql(
            f"select trade_date,stock_id,securities_trader_id tid,net "
            f"from stock_broker_branch_daily where stock_id in ({ids}) and trade_date>='2024-07-01'",
            conn)
    finally:
        conn.close()
    br = br.merge(px[["stock_id", "trade_date", "close", "volume"]],
                  on=["stock_id", "trade_date"], how="left")
    n0 = len(br)
    br = br.dropna(subset=["close"])
    print(f"  分點×價格 join 成功率 {len(br)/n0:.1%}（A14 檢查）")
    br["amt"] = br.net * br.close
    br["tv"] = br.volume * br.close
    br = br.sort_values(["stock_id", "tid", "trade_date"])
    br["pn"] = br.groupby(["stock_id", "tid"], sort=False).cumcount()
    br = br[br.pn >= MIN_HIST]
    key = ["stock_id", "trade_date"]
    g = br.groupby(key)
    out = pd.DataFrame({"net_disp": g.amt.std() / g.tv.first()}).reset_index()
    return out


def load_panel(univ: list[str], px: pd.DataFrame, rebuild: bool) -> pd.DataFrame:
    if not rebuild and PANEL_CACHE.exists():
        p = pd.read_pickle(PANEL_CACHE)
        if "net_disp" in p.columns:
            print(f"  用快取面板 {PANEL_CACHE}（{len(p)} 列）")
            return p[["stock_id", "trade_date", "net_disp", "r0"]].copy()
    print("  從 DB 重算 net_disp …")
    nd = build_netdisp(univ, px)
    r0 = px.assign(r0=px.groupby("stock_id").close.pct_change())[["stock_id", "trade_date", "r0"]]
    return nd.merge(r0, on=["stock_id", "trade_date"], how="left")


def add_forward_returns(px: pd.DataFrame) -> pd.DataFrame:
    """L1H*：entry=open[i+1]、exit=close[i+H]；另加 C2C（含跳空，診斷用）。"""
    g = px.groupby("stock_id")
    px = px.copy()
    px["entry_open"] = g.open.shift(-1)
    px["gap"] = px.entry_open / px.close - 1
    for h in HORIZONS:
        exit_close = g.close.shift(-h)
        px[f"L1H{h}"] = exit_close / px.entry_open - 1
        px[f"C2C{h}"] = exit_close / px.close - 1
    return px


# ---------------------------------------------------------------- stats

def nw_t(x: np.ndarray, lag: int) -> tuple[float, float]:
    """Newey-West（Bartlett kernel）t 值 + 雙尾 p（常態近似）。"""
    x = np.asarray(pd.Series(x).dropna(), dtype=float)
    n = len(x)
    if n < 20:
        return np.nan, np.nan
    e = x - x.mean()
    v = (e ** 2).sum() / n
    for L in range(1, min(lag, n - 1) + 1):
        w = 1 - L / (lag + 1)
        v += 2 * w * (e[L:] * e[:-L]).sum() / n
    if v <= 0:
        return np.nan, np.nan
    t = x.mean() / np.sqrt(v / n)
    from math import erfc, sqrt
    return t, erfc(abs(t) / sqrt(2))


def daily_ic(df: pd.DataFrame, sig: str, tgt: str) -> pd.Series:
    d = df.dropna(subset=[sig, tgt])
    ic = d.groupby("trade_date").apply(
        lambda x: x[sig].corr(x[tgt], method="spearman") if len(x) >= MIN_NAMES else np.nan)
    return ic.dropna()


def daily_ls_spread(df: pd.DataFrame, sig: str, tgt: str) -> pd.Series:
    """每日五分位多空價差（訊號負向：long = 低 net_disp）。價差對同日 demean 不變。"""
    d = df.dropna(subset=[sig, tgt])
    def _one(x: pd.DataFrame) -> float:
        if len(x) < MIN_NAMES:
            return np.nan
        k = max(2, int(round(len(x) * QUANTILE)))
        o = x.sort_values(sig)
        return o[tgt].head(k).mean() - o[tgt].tail(k).mean()   # 低 − 高
    return d.groupby("trade_date").apply(_one).dropna()


def perm_pvalue(df: pd.DataFrame, sig: str, tgt: str, stat_obs: float,
                seed: int, n_perm: int = N_PERM) -> float:
    """date-preserving cross-sectional permutation：同日內打散 net_disp 對應到哪檔。

    保留：每日檔數、每日報酬橫斷面分布、同日市場因子。測的是「選股能力」。
    雙尾（方向雖已預註記為負，仍用雙尾以保守）。
    """
    d = df.dropna(subset=[sig, tgt])
    groups = [(g[sig].values, g[tgt].values) for _, g in d.groupby("trade_date") if len(g) >= MIN_NAMES]
    rng = np.random.default_rng(seed)
    from scipy.stats import rankdata
    ranked = [(rankdata(s), rankdata(y)) for s, y in groups]
    null = np.empty(n_perm)
    for i in range(n_perm):
        vals = []
        for rs, ry in ranked:
            p = rng.permutation(rs)
            vals.append(np.corrcoef(p, ry)[0, 1])
        null[i] = float(np.mean(vals))
    return float(np.mean(np.abs(null) >= abs(stat_obs)))


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="忽略 /tmp/cs_panel.pkl 快取")
    ap.add_argument("--perm", type=int, default=N_PERM)
    args = ap.parse_args()

    univ = universe()
    print(f"宇宙 {len(univ)} 檔（個股期日均量 ≥2,000 口）")
    px = load_prices(univ)
    print(f"價量 {len(px):,} 列　{px.trade_date.nunique()} 天")
    panel = load_panel(univ, px, args.rebuild)
    px = add_forward_returns(px)

    cols = ["stock_id", "trade_date", "entry_open", "gap"] \
        + [f"L1H{h}" for h in HORIZONS] + [f"C2C{h}" for h in HORIZONS]
    df = panel.merge(px[cols], on=["stock_id", "trade_date"], how="inner")
    cnt = df.groupby("trade_date").net_disp.transform("size")
    df = df[cnt >= MIN_NAMES].copy()
    # 同日橫斷面動能殘差化目標（y2 系列）：擋掉「當日大漲」這個 confound 的殘餘
    df["r0_cs"] = df.r0 - df.groupby("trade_date").r0.transform("mean")
    for h in HORIZONS:
        for pre in ("L1H", "C2C"):
            c = f"{pre}{h}"
            df[c + "_cs"] = df[c] - df.groupby("trade_date")[c].transform("mean")
            df[c + "_resid"] = df.groupby("trade_date", group_keys=False).apply(
                lambda d, c=c: d[c + "_cs"] - np.polyval(
                    np.polyfit(d.r0_cs, d[c + "_cs"], 1), d.r0_cs)
                if d[["r0_cs", c + "_cs"]].dropna().shape[0] >= 8 else d[c + "_cs"] * np.nan)
    print(f"分析面板 {len(df):,} 列　{df.trade_date.nunique()} 天　{df.stock_id.nunique()} 檔"
          f"　({df.trade_date.min()} ~ {df.trade_date.max()})")

    # --- sanity：重現前一輪「隔夜跳空」的橫斷面結果（IC 訓練 −0.035 / 測試 −0.062）---
    df["gap_cs"] = df.gap - df.groupby("trade_date").gap.transform("mean")
    ic_gap = daily_ic(df, "net_disp", "gap_cs")
    sp_gap = daily_ls_spread(df, "net_disp", "gap_cs")
    gt_tr, _ = nw_t(ic_gap[ic_gap.index < SPLIT].values, 5)
    gt_te, _ = nw_t(ic_gap[ic_gap.index >= SPLIT].values, 5)
    print(f"\n[sanity] 隔夜跳空（T 收→T+1 開，NOT TRADABLE）："
          f"IC 訓練 {ic_gap[ic_gap.index < SPLIT].mean():+.4f} (t={gt_tr:+.2f}) / "
          f"測試 {ic_gap[ic_gap.index >= SPLIT].mean():+.4f} (t={gt_te:+.2f})　"
          f"低−高毛價差 {sp_gap.mean()*100:+.3f}%　← 應為負 IC / 正價差，與前一輪結論一致")

    rows = []
    tables = {}
    for h in HORIZONS:
        for tgt_kind, suffix in (("hedged", ""), ("hedged+動能殘差", "_resid")):
            for pre, tradable in (("L1H", True), ("C2C", False)):
                tgt = f"{pre}{h}{suffix}"
                if tgt not in df.columns:
                    tgt = f"{pre}{h}_cs" if suffix == "" else tgt
                col = f"{pre}{h}_cs" if suffix == "" else f"{pre}{h}_resid"
                ic = daily_ic(df, "net_disp", col)
                sp = daily_ls_spread(df, "net_disp", col)
                if len(ic) < 60:
                    continue
                lag = max(5, h)
                rec = {"天期": f"H{h}", "報酬": pre, "可交易": tradable, "標的": tgt_kind, "lag": lag}
                for name, s in (("全", None), ("訓練", ic.index < SPLIT), ("測試", ic.index >= SPLIT)):
                    sub = ic if s is None else ic[s]
                    t, p = nw_t(sub.values, lag)
                    rec[f"IC_{name}"] = round(float(sub.mean()), 5)
                    rec[f"t_{name}"] = round(t, 2) if t == t else np.nan
                    rec[f"p_{name}"] = round(p, 5) if p == p else np.nan
                    rec[f"天數_{name}"] = int(len(sub))
                t2, _ = nw_t(ic.values, 2 * h)
                rec["t_全_lag2H"] = round(t2, 2) if t2 == t2 else np.nan
                for name, s in (("全", None), ("訓練", sp.index < SPLIT), ("測試", sp.index >= SPLIT)):
                    sub = sp if s is None else sp[s]
                    rec[f"毛價差%_{name}"] = round(float(sub.mean()) * 100, 4)
                    rec[f"淨價差%_{name}"] = round(float(sub.mean()) * 100 - COST_LS * 100, 4)
                for name, s in (("全", None), ("訓練", sp.index < SPLIT), ("測試", sp.index >= SPLIT)):
                    sub = sp if s is None else sp[s]
                    st, sp_p = nw_t(sub.values, lag)
                    rec[f"價差t_{name}"] = round(st, 2) if st == st else np.nan
                    rec[f"價差p_{name}"] = round(sp_p, 5) if sp_p == sp_p else np.nan
                # 非重疊子樣本（每 H 天取一天）：重疊持有期的最保守檢查
                nov = sp.iloc[::h]
                nt, npv = nw_t(nov.values, 1)
                rec["非重疊n"] = int(len(nov))
                rec["非重疊毛價差%"] = round(float(nov.mean()) * 100, 4)
                rec["非重疊t"] = round(nt, 2) if nt == nt else np.nan
                rec["非重疊p"] = round(npv, 4) if npv == npv else np.nan
                rec["IC>0天數比"] = round(float((ic > 0).mean()), 3)
                rec["同號"] = bool(np.sign(ic[ic.index < SPLIT].mean())
                                   == np.sign(ic[ic.index >= SPLIT].mean()))
                # 去極值：剔除 |日 IC| 最大 5% 交易日
                thr = ic.abs().quantile(0.95)
                keep = ic[ic.abs() <= thr]
                rec["IC_去極值"] = round(float(keep.mean()), 5)
                rec["IC保留率"] = round(float(keep.mean() / ic.mean()), 3) if ic.mean() != 0 else np.nan
                kt, _ = nw_t(keep.values, lag)
                rec["t_去極值"] = round(kt, 2) if kt == kt else np.nan
                spthr = sp.abs().quantile(0.95)
                spk = sp[sp.abs() <= spthr]
                rec["毛價差%_去極值"] = round(float(spk.mean()) * 100, 4)
                rows.append(rec)
                tables[(h, pre, tgt_kind)] = (ic, sp)

    R = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    csv_path = OUT / "branch_netdisp_multihorizon_ic.csv"
    R.to_csv(csv_path, index=False)

    main_tbl = R[(R["報酬"] == "L1H") & (R["標的"] == "hedged")]
    show = ["天期", "IC_訓練", "t_訓練", "IC_測試", "t_測試", "同號", "IC_全", "t_全", "t_全_lag2H",
            "p_全", "毛價差%_全", "淨價差%_全", "毛價差%_訓練", "毛價差%_測試",
            "IC>0天數比", "IC保留率", "t_去極值", "毛價差%_去極值", "天數_測試"]
    print(f"\n=== 主表：L1H*（T+1 開盤進、第 H 個交易日收盤出）· 同日等權多空對沖 ===")
    print(f"    成本基準：單邊 {COST_PER_CROSS*100:.3f}% × {N_CROSSINGS} 次穿價 = {COST_LS*100:.2f}%")
    print(main_tbl[show].to_string(index=False))

    resid_tbl = R[(R["報酬"] == "L1H") & (R["標的"] != "hedged")]
    print("\n=== 對照：再扣掉同日橫斷面動能（r0_cs 殘差化）===")
    print(resid_tbl[["天期", "IC_訓練", "t_訓練", "IC_測試", "t_測試", "同號", "IC_全", "t_全",
                     "毛價差%_全", "淨價差%_全"]].to_string(index=False))

    c2c = R[(R["報酬"] == "C2C") & (R["標的"] == "hedged")]
    print("\n=== 診斷（NOT TRADABLE：T 收盤進場，但分點資料收盤後才發布）===")
    print(c2c[["天期", "IC_全", "t_全", "毛價差%_全", "淨價差%_全"]].to_string(index=False))

    # 多重檢定
    k = len(R[R["報酬"] == "L1H"])
    hits = int((R[(R["報酬"] == "L1H")]["p_全"] < 0.05).sum())
    print(f"\n多重檢定：L1H 家族共 {k} 格（{len(HORIZONS)} 天期 × 2 標的），"
          f"α=0.05 隨機期望通過 {k*0.05:.2f} 格；實際 p_全<0.05 共 {hits} 格。")
    pv = R[R["報酬"] == "L1H"][["天期", "標的", "p_全"]].dropna().sort_values("p_全")
    m = len(pv)
    # BH step-up：q_i = min_{j>=i} (p_j * m / j)  → 由大到小 cummin
    adj = (pv.p_全.values * m / np.arange(1, m + 1))[::-1]
    pv["BH_q"] = np.minimum.accumulate(adj)[::-1].clip(max=1).round(4)
    print(pv.to_string(index=False))

    # 天期選擇：訓練期挑、測試期驗（不循環）
    tr_best = main_tbl.loc[main_tbl["t_訓練"].abs().idxmax()]
    print(f"\n以【訓練期】挑天期 → {tr_best['天期']}"
          f"（IC 訓練 {tr_best['IC_訓練']:+.4f} / t {tr_best['t_訓練']:+.2f}）"
          f"；該天期【測試期】表現：IC {tr_best['IC_測試']:+.4f} / t {tr_best['t_測試']:+.2f}"
          f" / 毛價差 {tr_best['毛價差%_測試']:+.3f}%")

    # 成本跨越點
    print("\n=== 毛價差 vs 成本（多空 4 次穿價 = 0.77%）===")
    for _, r in main_tbl.iterrows():
        h = int(r["天期"][1:])
        flag = "✓ 跨過成本" if r["淨價差%_全"] > 0 else ""
        print(f"  {r['天期']:>4}  毛 {r['毛價差%_全']:+7.3f}%  淨 {r['淨價差%_全']:+7.3f}%"
              f"  每日淨 {r['淨價差%_全']/h:+6.3f}%  {flag}")
    print("\n=== 價差顯著性與重疊持有期檢查（低 net_disp 做多、高做空；預註記負向）===")
    print(main_tbl[["天期", "毛價差%_全", "價差t_全", "價差p_全", "毛價差%_訓練", "價差t_訓練",
                    "毛價差%_測試", "價差t_測試", "非重疊n", "非重疊毛價差%", "非重疊t", "非重疊p"]]
          .to_string(index=False))
    print(f"  註：H 天期下有效獨立樣本 ≈ {main_tbl['天數_全'].iloc[0]} / H 天；"
          f"H20 僅約 {main_tbl['天數_全'].iloc[0]//20} 個不重疊區間。")

    pos = main_tbl[main_tbl["淨價差%_全"] > 0]
    if len(pos):
        print(f"  → 訊號幅度超過成本的最短天期：{pos.iloc[0]['天期']}")
    else:
        gm = main_tbl["毛價差%_全"].max()
        print(f"  → 全部天期毛價差最大僅 {gm:+.3f}%，**無任何天期跨過 0.77% 成本**")

    # leg 分解（RESEARCH_SUMMARY 陷阱 #1 的指定診斷）：把 H20 拆成 1-7 / 8-14 / 15-20 段，
    # 各段獨立檢定。若「H20 毛價差變大」只是報酬 std 隨天期擴大的假象，各段都不會顯著。
    print("\n=== leg 分解：H20 拆段（各段獨立多空價差，訊號仍為預註記負向）===")
    g = px.groupby("stock_id")
    legs = {"1-7 天": (0, 7), "8-14 天": (7, 14), "15-20 天": (14, 20)}
    legrows = []
    for name, (a, b) in legs.items():
        base = g.open.shift(-1) if a == 0 else g.close.shift(-a)
        col = f"leg_{a}_{b}"
        px[col] = g.close.shift(-b) / base - 1
        d = df.merge(px[["stock_id", "trade_date", col]], on=["stock_id", "trade_date"], how="left")
        d[col + "_cs"] = d[col] - d.groupby("trade_date")[col].transform("mean")
        sp = daily_ls_spread(d, "net_disp", col + "_cs")
        ic = daily_ic(d, "net_disp", col + "_cs")
        lag = max(5, b - a)
        st, sp_p = nw_t(sp.values, lag)
        it, ip = nw_t(ic.values, lag)
        legrows.append({"段": name, "毛價差%": round(sp.mean() * 100, 4), "價差t": round(st, 2),
                        "價差p": round(sp_p, 4), "IC": round(ic.mean(), 5),
                        "IC_t": round(it, 2), "IC_p": round(ip, 4),
                        "毛價差%_訓練": round(sp[sp.index < SPLIT].mean() * 100, 4),
                        "毛價差%_測試": round(sp[sp.index >= SPLIT].mean() * 100, 4)})
    LEG = pd.DataFrame(legrows)
    print(LEG.to_string(index=False))
    LEG.to_csv(OUT / "branch_netdisp_multihorizon_legs.csv", index=False)

    # permutation（對訓練期挑出的天期 + 最大毛價差天期）
    print("\n=== date-preserving cross-sectional permutation ===")
    print("  ⚠️ 此 null 只保留【同日】橫斷面結構，不保留【跨日】相依。H>1 的重疊持有期會使")
    print("     null 變異數被低估 → p 值過度樂觀。H>1 請以 Newey-West t 與非重疊子樣本為準。")
    cand = {tr_best["天期"]}
    cand.add(main_tbl.loc[main_tbl["毛價差%_全"].idxmax()]["天期"])
    perm_rows = []
    for hh in sorted(cand, key=lambda s: int(s[1:])):
        h = int(hh[1:])
        col = f"L1H{h}_cs"
        ic = daily_ic(df, "net_disp", col)
        obs = float(ic.mean())
        for seed in SEEDS:
            p = perm_pvalue(df, "net_disp", col, obs, seed, args.perm)
            perm_rows.append({"天期": hh, "seed": seed, "觀察IC": round(obs, 5),
                              "perm_p_雙尾": round(p, 5), "n_perm": args.perm})
            print(f"  permutation {hh} seed={seed}: 觀察 IC {obs:+.5f}　雙尾 p={p:.4f}")
    if perm_rows:
        pd.DataFrame(perm_rows).to_csv(OUT / "branch_netdisp_multihorizon_perm.csv", index=False)

    print(f"\n結果寫入 {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
