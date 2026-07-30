"""月營收 YoY 動能 — 大型股多頭傾斜月選股 (rev_yoy_monthly_screen)

把 rev_yoy_3m(月營收 YoY 3 個月均動能)產品化成「輸入日期 → 大型股多頭傾斜名單」。

★可交易性裁決(Phase-6 rev_family_tradability.md,決定此工具形態):
  - rev_yoy_3m 扣真台股成本(0.3% 賣稅+滑價)+ 市場中性後,中小型 edge 被
    高成本+小容量(~$0.5M)自我抵銷、**反轉為大型股**(大型是唯一兩窗皆正、
    容量 ~$30M 的 bucket)。
  - 故本工具**限大型股**(--large-only,預設 on)並以**多頭傾斜(long-side)**呈現,
    而非中小型 dollar-neutral 多空書(那個扣成本後 net Sharpe 為負/DSR fail)。
  - 保留空方於 CSV 供研究,但正式產出用大型股多頭 top-decile。

PIT: 一律用公告日 annd(次月 10 日前公布、中位 37 日 lag)gate,無前視。
size proxy = 近 60 交易日中位日成交值(tval, ≤ as-of),top tertile = 大型。

用法:
  .venv/bin/python scripts/research/dashboard/rev_yoy_monthly_screen.py            # as-of=今天, 大型股
  .venv/bin/python scripts/research/dashboard/rev_yoy_monthly_screen.py --asof 2026-07-31
  .venv/bin/python scripts/research/dashboard/rev_yoy_monthly_screen.py --all-caps  # 不限大型(研究用)

保留(勿當聖杯): rev_yoy_3m 過 permutation 但**過不了 Deflated-Sharpe**(扣成本後 0.11);
  弱、統計脆弱、OOS 單一多頭窗、與 champion corr +0.2~0.3(殘餘 beta 傾斜)、約半數產業 tilt。
  這是「選股傾斜」非可獨立部署 alpha,只作系統 C(champion 綠燈)內大型股多頭 overlay。非投資建議。
"""
from __future__ import annotations
import argparse
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"
CURRENT_LONG = D / "rev_yoy_current_long.csv"   # tracker 讀這份

YOY_CLIP_LO, YOY_CLIP_HI = -90.0, 300.0
N_MONTHS = 3
SIZE_WIN = 60          # 近 60 交易日中位成交值當 size proxy
LARGE_TERTILE = 1 / 3  # top 1/3 by tval = 大型


def load_data():
    sale = pd.read_parquet(D / "tej_fundamental_sale.parquet").copy()
    sale["annd"] = pd.to_datetime(sale["annd"])
    sale["period"] = pd.to_datetime(sale["period"])
    ind = pd.read_parquet(D / "tej_fundamental_industry.parquet").copy()
    ind_map = ind.set_index("stock_id")[["stock_name", "industry_category"]].to_dict("index")
    adv = None
    apath = D / "tej_revfam_advalue.parquet"
    if apath.exists():
        adv = pd.read_parquet(apath).copy()
        adv["date"] = pd.to_datetime(adv["date"])
        adv["stock_id"] = adv["stock_id"].astype(str)
    return sale, ind_map, adv


def size_proxy(adv: pd.DataFrame | None, asof: pd.Timestamp) -> dict:
    """近 SIZE_WIN 交易日(≤ asof)中位日成交值,當大型/流動性 proxy。無前視。"""
    if adv is None:
        return {}
    a = adv[adv["date"] <= asof]
    if a.empty:
        return {}
    last_dates = sorted(a["date"].unique())[-SIZE_WIN:]
    a = a[a["date"].isin(last_dates)]
    return a.groupby("stock_id")["tval"].median().to_dict()


def screen(sale, asof, size_map, n_months=N_MONTHS):
    s = sale.copy()
    s["rev_yoy_w"] = s["rev_yoy"].clip(YOY_CLIP_LO, YOY_CLIP_HI)
    rows = []
    for sid, g in s.groupby("stock_id"):
        g = g[g["annd"] <= asof].dropna(subset=["rev_yoy_w"]).sort_values("annd")
        if len(g) < n_months:
            continue
        tail = g.tail(n_months)
        rows.append(dict(
            stock_id=str(sid),
            rev_yoy_3m=float(tail["rev_yoy_w"].mean()),
            rev_yoy_latest=float(g.iloc[-1]["rev_yoy_w"]),
            latest_rev_month=g.iloc[-1]["period"].strftime("%Y-%m"),
            latest_annd=g.iloc[-1]["annd"].strftime("%Y-%m-%d"),
            size_tval=float(size_map.get(str(sid), np.nan)),
        ))
    return pd.DataFrame(rows)


def rank_and_split(df, ind_map, decile, large_only):
    df = df.copy()
    df["is_large"] = False
    if large_only and df["size_tval"].notna().sum() >= 6:
        cut = df["size_tval"].quantile(1 - LARGE_TERTILE)
        df["is_large"] = df["size_tval"] >= cut
        uni = df[df["is_large"]].copy()
    else:
        uni = df.copy()
    uni["pctile"] = uni["rev_yoy_3m"].rank(pct=True)
    uni["xs_rank"] = uni["rev_yoy_3m"].rank(ascending=False).astype(int)
    hi, lo = 1.0 - decile, decile
    uni["side"] = np.where(uni["pctile"] >= hi, "LONG",
                    np.where(uni["pctile"] <= lo, "SHORT", ""))
    uni["stock_name"] = uni["stock_id"].map(lambda s: ind_map.get(s, {}).get("stock_name", ""))
    uni["industry"] = uni["stock_id"].map(lambda s: ind_map.get(s, {}).get("industry_category", ""))
    return uni.sort_values("rev_yoy_3m", ascending=False).reset_index(drop=True)


def fmt(sub):
    lines = [f"{'#':>3} {'代碼':<6} {'名稱':<9} {'產業':<10} {'YoY3m':>8} {'YoY新':>8} {'營收月':>8} {'日成交值(億)':>10}"]
    for _, r in sub.iterrows():
        tv = f"{r['size_tval']/1e8:.1f}" if pd.notna(r['size_tval']) else "n/a"
        lines.append(f"{r['xs_rank']:>3} {r['stock_id']:<6} {str(r['stock_name']):<9} "
                     f"{str(r['industry']):<10} {r['rev_yoy_3m']:>7.1f}% {r['rev_yoy_latest']:>7.1f}% "
                     f"{r['latest_rev_month']:>8} {tv:>10}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None)
    ap.add_argument("--decile", type=float, default=0.10)
    ap.add_argument("--all-caps", action="store_true", help="不限大型股(研究用;預設限大型)")
    ap.add_argument("--csv", default=None)
    args = ap.parse_args()

    asof = pd.Timestamp(args.asof) if args.asof else pd.Timestamp.today().normalize()
    large_only = not args.all_caps
    sale, ind_map, adv = load_data()
    size_map = size_proxy(adv, asof)
    df = screen(sale, asof, size_map)
    if len(df) == 0:
        print(f"as-of {asof.date()}: 無足夠已公告資料"); return
    ranked = rank_and_split(df, ind_map, args.decile, large_only)
    max_annd = df["latest_annd"].max()
    n = len(ranked); k = max(1, int(round(n * args.decile)))
    scope = "大型股(top 1/3 成交值)" if large_only else "全宇宙"
    print(f"月營收 YoY 動能 · {'大型股多頭傾斜' if large_only else '多空全截面'}月選股  |  as-of {asof.date()}  |  "
          f"{scope} {n} 檔  |  最新公告 {max_annd}  |  多空各 {args.decile:.0%} (~{k} 檔)")
    print(f"訊號 = rev_yoy_3m(近 {N_MONTHS} 月營收 YoY 均, winsorize [{YOY_CLIP_LO:.0f},{YOY_CLIP_HI:.0f}]%) | 高=傾斜做多\n")

    longs = ranked[ranked["side"] == "LONG"]
    shorts = ranked[ranked["side"] == "SHORT"].sort_values("rev_yoy_3m")
    print(f"== ★多頭傾斜 (top-decile 大型高營收動能, 系統C綠燈時 overlay) ==")
    print(fmt(longs))
    print(f"\n== 空方 (bottom-decile, 研究參考; 扣成本後非可交易) ==")
    print(fmt(shorts))

    # 供 daily_tracker 讀的穩定多頭清單
    longs.assign(asof=str(asof.date()), max_annd=max_annd)[
        ["asof", "max_annd", "stock_id", "stock_name", "industry", "rev_yoy_3m", "size_tval"]
    ].to_csv(CURRENT_LONG, index=False)
    if args.csv:
        ranked.to_csv(args.csv, index=False); print(f"\nwrote {args.csv}")
    print(f"\n-> 多頭傾斜清單 {CURRENT_LONG}")
    print("保留: 過permutation但過不了Deflated-Sharpe(扣成本0.11)、殘餘beta傾斜、單一多頭窗、半數產業tilt。"
          "選股傾斜非alpha,僅系統C綠燈時大型股多頭overlay。非投資建議。")
    return ranked, longs, shorts, asof, max_annd, n, k


if __name__ == "__main__":
    main()
