#!/usr/bin/env python3
"""過離散度 var/mean 是**活躍度的代理**，不是規律性的度量 —— 這支給出尺度不變的替代品。

實測：Spearman(每日當沖檔數 dt_n, overdisp) = +0.829（760 個分點）。
機制：日檔數 ~ Poisson(λ_t)，λ_t 帶共同市場活躍因子 → var/mean = 1 + mean·CV²。
所以 overdisp 天生隨 mean 上升，最小的分點自動排到前面。

尺度不變版：CV = sqrt((overdisp − 1) / mean) = 分點自身每日下單強度的變異係數。
用它重排，前 7 名是 4 個自營商 + 9217 凱基松山 + 9661 富邦新店 + 984K 元大館前 —— 也就是
已知的程式；而原始 overdisp 前 20 名裡的盈溢三分點（5860/5861/5862）掉到 564/389/735。
"""
import numpy as np, pandas as pd
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "reports/research/chip-signal-daily-horizon/branch_overdisp.pkl"


def main() -> int:
    d = pd.read_pickle(SRC)
    d = d[d.dt_n >= 3].copy()
    d["cv"] = np.sqrt(((d.overdisp - 1) / d.dt_n).clip(lower=0)) * 100
    d["cv_rank"] = d.cv.rank().astype(int)
    d["od_rank"] = d.overdisp.rank().astype(int)
    print(f"Spearman(dt_n, overdisp) = {d.dt_n.corr(d.overdisp, method='spearman'):+.3f}")
    print(f"Spearman(dt_n, cv^2)     = {d.dt_n.corr(d.cv**2, method='spearman'):+.3f}   ← 尺度不變")
    pd.set_option("display.width", 200)
    print("\nCV 最小的 20 個（真正規律的候選）：")
    print(d.nsmallest(20, "cv")[["name", "dt_n", "overdisp", "cv", "od_rank", "cv_rank"]].round(2).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
