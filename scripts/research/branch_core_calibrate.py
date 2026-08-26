#!/usr/bin/env python3
"""分點核心樣本校準 —— 排除離群後再反向工程。

分點是**多個客戶共用的通道**，把他們混在一起反向工程，得到的是所有大戶
可交易宇宙的交集（＝容量約束），不是任何一個人的選股邏輯。
本檔先切出行為一致的核心樣本，再重跑消融。

核心樣本定義（每一條都有理由，不是調參）：
  · 排除金額前 1%          —— 一次性大宗，多半是單一客戶的調節而非常規操作
  · 方向性 |net|/gross ≥ 0.7 —— 排除當沖與對敲，只留真正在建/減倉的
  · 參與率 1~15%           —— 太小是零星、太大是砸盤，都不是常規節奏
  · 該標的被交易 ≥10 次     —— 排除一次性標的

**成功判準先講在前面**：若「排除離群」真的分離出一個程式，籌碼因子在容量
之上的增量應該**變大**。若反而變小或變負，代表沒有可辨識的單一程式。
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
print(__doc__)
print("實作見 git log fae0295 之後的分析；核心樣本快取："
      "reports/research/chip-signal-daily-horizon/br9661_core.pkl")
