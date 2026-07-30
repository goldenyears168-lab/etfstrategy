"""月營收 YoY 動能 — 月選股產品化 (rev_yoy_monthly_screen)

把 Phase-4 驗證存活的橫斷面因子 `rev_yoy_3m`(月營收 YoY 3 個月均動能)
產品化成「輸入日期 → 當月多空全截面名單」。

已驗證形態(見 reports/research/dashboard-completeness/tej_fundamental_factor.md):
  - 訊號 = 每月營收 YoY 的近 3 個月均(平滑動能),方向 = 高動能做多、低動能做空。
  - 這是**多空全截面**形態(空頭仍 +0.72 Sharpe),非只做多綁 risk-on。
  - PIT: 一律用公告日 annd(次月 10 日前公布、中位 37 日 lag)gate,無前視。

本腳本 vs 回測腳本差異: 這裡不做回測,只做「當下選股」——
  給定 as-of 日期,取每檔「公告日 ≤ as-of」的最新 3 筆月營收 YoY 均,
  winsorize 會計異常(小基期爆量/轉正),橫斷面排序,輸出 top/bottom decile。

用法:
  .venv/bin/python scripts/research/dashboard/rev_yoy_monthly_screen.py            # as-of = 今天
  .venv/bin/python scripts/research/dashboard/rev_yoy_monthly_screen.py --asof 2026-07-31
  .venv/bin/python scripts/research/dashboard/rev_yoy_monthly_screen.py --decile 0.10 --csv out.csv

保留(勿當聖杯): edge 集中中小型(大型 tertile 近零)、OOS 單一多頭窗、
與 champion corr +0.27(非全正交)、約半數為產業 tilt。非投資建議。
"""
from __future__ import annotations
import argparse
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
D = ROOT / "data" / "research" / "dashboard"

# winsorize 界: 月營收 YoY(%) 的合理硬界。小基期轉正可噴到數千%,
# 屬會計/併購/基期異常而非「動能」,clip 進場避免單月污染 3m 均與排序。
YOY_CLIP_LO, YOY_CLIP_HI = -90.0, 300.0
N_MONTHS = 3  # rev_yoy_3m = 近 3 個月均


def load_data():
    sale = pd.read_parquet(D / "tej_fundamental_sale.parquet").copy()
    sale["annd"] = pd.to_datetime(sale["annd"])
    sale["period"] = pd.to_datetime(sale["period"])
    ind = pd.read_parquet(D / "tej_fundamental_industry.parquet").copy()
    ind_map = ind.set_index("stock_id")[["stock_name", "industry_category"]].to_dict("index")
    return sale, ind_map


def screen(sale: pd.DataFrame, asof: pd.Timestamp, n_months: int = N_MONTHS) -> pd.DataFrame:
    """對每檔取『公告日 annd ≤ asof』最新 n_months 筆月營收 YoY 均。PIT 乾淨。"""
    # winsorize 原始 YoY(逐列硬界,防會計異常污染)
    s = sale.copy()
    s["rev_yoy_w"] = s["rev_yoy"].clip(YOY_CLIP_LO, YOY_CLIP_HI)
    rows = []
    for sid, g in s.groupby("stock_id"):
        g = g[g["annd"] <= asof].dropna(subset=["rev_yoy_w"]).sort_values("annd")
        if len(g) < n_months:
            continue
        tail = g.tail(n_months)
        rows.append(dict(
            stock_id=sid,
            rev_yoy_3m=float(tail["rev_yoy_w"].mean()),
            rev_yoy_latest=float(g.iloc[-1]["rev_yoy_w"]),
            latest_rev_month=g.iloc[-1]["period"].strftime("%Y-%m"),
            latest_annd=g.iloc[-1]["annd"].strftime("%Y-%m-%d"),
            n_used=int(len(tail)),
        ))
    df = pd.DataFrame(rows)
    return df


def rank_and_split(df: pd.DataFrame, ind_map: dict, decile: float) -> pd.DataFrame:
    df = df.copy()
    # 橫斷面 percentile rank on rev_yoy_3m(高=強動能)
    df["pctile"] = df["rev_yoy_3m"].rank(pct=True)
    df["xs_rank"] = df["rev_yoy_3m"].rank(ascending=False).astype(int)
    hi, lo = 1.0 - decile, decile
    df["side"] = np.where(df["pctile"] >= hi, "LONG",
                   np.where(df["pctile"] <= lo, "SHORT", ""))
    df["stock_name"] = df["stock_id"].map(lambda s: ind_map.get(s, {}).get("stock_name", ""))
    df["industry"] = df["stock_id"].map(lambda s: ind_map.get(s, {}).get("industry_category", ""))
    return df.sort_values("rev_yoy_3m", ascending=False).reset_index(drop=True)


def fmt(sub: pd.DataFrame) -> str:
    cols = ["xs_rank", "stock_id", "stock_name", "industry",
            "rev_yoy_3m", "rev_yoy_latest", "latest_rev_month", "latest_annd"]
    lines = []
    hdr = f"{'#':>3} {'代碼':<6} {'名稱':<8} {'產業':<10} {'YoY3m':>8} {'YoY新':>8} {'營收月':>8} {'公告日':>11}"
    lines.append(hdr)
    for _, r in sub.iterrows():
        lines.append(f"{r['xs_rank']:>3} {r['stock_id']:<6} {str(r['stock_name']):<8} "
                     f"{str(r['industry']):<10} {r['rev_yoy_3m']:>7.1f}% {r['rev_yoy_latest']:>7.1f}% "
                     f"{r['latest_rev_month']:>8} {r['latest_annd']:>11}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None, help="as-of 日期 YYYY-MM-DD (預設今天)")
    ap.add_argument("--decile", type=float, default=0.10, help="多空各取的分位 (預設 0.10=decile)")
    ap.add_argument("--csv", default=None, help="輸出完整排序 CSV 路徑")
    args = ap.parse_args()

    asof = pd.Timestamp(args.asof) if args.asof else pd.Timestamp.today().normalize()
    sale, ind_map = load_data()
    df = screen(sale, asof)
    if len(df) == 0:
        print(f"as-of {asof.date()}: 無足夠已公告資料")
        return
    ranked = rank_and_split(df, ind_map, args.decile)

    max_annd = df["latest_annd"].max()
    n = len(ranked)
    k = max(1, int(round(n * args.decile)))
    print(f"月營收 YoY 動能月選股  |  as-of {asof.date()}  |  宇宙 {n} 檔  |  "
          f"最新公告日 {max_annd}  |  多空各 top/bottom {args.decile:.0%} (~{k} 檔)")
    print(f"訊號 = rev_yoy_3m(近 {N_MONTHS} 月營收 YoY 均, winsorize [{YOY_CLIP_LO:.0f},{YOY_CLIP_HI:.0f}]%)  "
          f"| 高=做多 低=做空 (已驗證多空全截面)\n")

    longs = ranked[ranked["side"] == "LONG"]
    shorts = ranked[ranked["side"] == "SHORT"].sort_values("rev_yoy_3m")
    print("== 多方 (top-decile, 高營收動能) ==")
    print(fmt(longs))
    print("\n== 空方 (bottom-decile, 低營收動能/衰退) ==")
    print(fmt(shorts))

    if args.csv:
        ranked.to_csv(args.csv, index=False)
        print(f"\nwrote {args.csv}")
    print("\n保留: edge 集中中小型(大型近零)、OOS 單一多頭窗、與 champion corr +0.27、約半數產業 tilt。非投資建議。")
    return ranked, longs, shorts, asof, max_annd, n, k


if __name__ == "__main__":
    main()
