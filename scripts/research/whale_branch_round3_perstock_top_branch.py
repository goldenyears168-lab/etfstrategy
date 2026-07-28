"""
個股專屬分點排行：對指定股票池，找出每檔股票的「頭號候選分點」
(sample n>=3, 依 median r_adj_h14 或 h20 排序).
"""
import pandas as pd
from pathlib import Path
from scipy import stats

ROOT = Path("/Users/jackm4/Documents/ETF/股票研究")
EV = ROOT / "reports/research/branch-footprint-screen/whale_branch_top3_multihorizon_events.csv"

STOCK_NAMES = {
    "2344": "華邦電", "8046": "南電", "8358": "金居",
    "3290": "東洋", "6147": "頎邦", "2337": "旺宏", "2408": "南亞科",
    "1447": "力鵬", "3006": "晶豪科", "8996": "高力", "3260": "威剛",
    "2441": "超豐", "7769": "展宏", "6696": "汎銓", "2327": "國巨",
    "3189": "景碩", "2379": "瑞昱", "2454": "聯發科", "3711": "日月光投控",
    "2059": "川湖",
}

STOCK_POOL = list(STOCK_NAMES.keys())

KNOWN_BRANCHES = {
    "9217": "凱基-松山 (既有live)",
    "9227": "凱基-城中 (既有)",
    "9801": "元大-松江 (既有)",
    "9661": "富邦-新店 (既有)",
    "9216": "凱基-信義 (第三輪候選)",
    "9B2a": "第三輪候選",
    "5852": "第三輪候選",
}

MM_EXCLUDE = {"980T","8440","1650","1440","8890","9600","1590","7000","8900","9100","980C","779Z"}

def main():
    df = pd.read_csv(EV, dtype={"stock_id": str, "securities_trader_id": str})
    df = df[df["stock_id"].isin(STOCK_POOL)].copy()
    print(f"Total events for pool: {len(df)} across {df['stock_id'].nunique()} stocks")

    rows = []
    detail_rows = []
    for sid in STOCK_POOL:
        sub = df[df["stock_id"] == sid]
        if sub.empty:
            rows.append({"stock_id": sid, "stock_name": STOCK_NAMES[sid], "note": "無事件"})
            continue
        # group by branch
        g = sub.groupby("securities_trader_id")
        cand = []
        for bid, gdf in g:
            n = len(gdf)
            if n < 3:
                continue
            if bid in MM_EXCLUDE:
                continue
            med14 = gdf["r_adj_h14_pct"].median()
            med20 = gdf["r_adj_h20_pct"].median()
            mean14 = gdf["r_adj_h14_pct"].mean()
            mean20 = gdf["r_adj_h20_pct"].mean()
            win14 = (gdf["r_adj_h14_pct"] > 0).mean() * 100
            win20 = (gdf["r_adj_h20_pct"] > 0).mean() * 100
            # rank score: prefer h20 median (per instructions), fallback h14
            score = med20 if pd.notna(med20) else med14
            cand.append({
                "stock_id": sid, "stock_name": STOCK_NAMES[sid],
                "branch": bid, "n": n,
                "median_h14": med14, "mean_h14": mean14, "win_h14": win14,
                "median_h20": med20, "mean_h20": mean20, "win_h20": win20,
                "score": score,
            })
        if not cand:
            rows.append({"stock_id": sid, "stock_name": STOCK_NAMES[sid], "note": "無 n>=3 分點(排除造市)"})
            continue
        cand_df = pd.DataFrame(cand).sort_values("score", ascending=False)
        top = cand_df.iloc[0]
        rows.append({
            "stock_id": sid, "stock_name": STOCK_NAMES[sid],
            "top_branch": top["branch"], "n": int(top["n"]),
            "median_h14": round(top["median_h14"], 2) if pd.notna(top["median_h14"]) else None,
            "mean_h14": round(top["mean_h14"], 2) if pd.notna(top["mean_h14"]) else None,
            "win_h14": round(top["win_h14"], 1),
            "median_h20": round(top["median_h20"], 2) if pd.notna(top["median_h20"]) else None,
            "mean_h20": round(top["mean_h20"], 2) if pd.notna(top["mean_h20"]) else None,
            "win_h20": round(top["win_h20"], 1),
            "known_branch": KNOWN_BRANCHES.get(top["branch"], ""),
            "n_candidates_in_stock": len(cand_df),
        })
        # keep top-3 candidates per stock for context
        for _, r in cand_df.head(3).iterrows():
            detail_rows.append(r.to_dict())

    out = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 20)
    print("\n=== 各股頭號候選分點 ===")
    print(out.to_string(index=False))

    out.to_csv(ROOT / "reports/research/branch-footprint-screen/whale_branch_round3_perstock_top_branch_summary.csv", index=False)
    detail_df = pd.DataFrame(detail_rows)
    detail_df.to_csv(ROOT / "reports/research/branch-footprint-screen/whale_branch_round3_perstock_top3_candidates.csv", index=False)

    # cross-stock branch consistency check
    print("\n=== 分點跨股票出現次數 (在多股票中都是頭號候選) ===")
    valid = out[out.get("top_branch").notna()] if "top_branch" in out.columns else pd.DataFrame()
    if not valid.empty:
        cnt = valid["top_branch"].value_counts()
        print(cnt[cnt > 1])
        print("\n(頭號候選分點清單)")
        print(valid[["stock_id","stock_name","top_branch","n","median_h20","win_h20","known_branch"]].to_string(index=False))

    return out, detail_df

if __name__ == "__main__":
    main()
