#!/usr/bin/env python3
"""個股期貨五檔＋逐筆收集器——與指數核心組**分開一條 websocket 連線**跑。

為什麼要拆成兩個行程
--------------------
2026-08-21 事故：ROOTS 從 8 檔擴到 27 檔（8 指數 + 19 個股期貨）之後，
27 × 4 訂閱（books/trades × 日盤/夜盤）＝ 108 個訂閱擠在同一條連線上。當天 11:08
出現「Maximum number of connections reached → authentication timeout」，重啟後
載入 27 檔的版本，復原花了 2 小時 48 分、48 個重啟循環（11:11 → 13:59），而那段
時間正好是日盤——TMF/MXF/TXF 三檔實測掉了 11:08–13:45 共 157 分鐘 ＝ 300 分鐘
日盤的 52%。

拆成兩個 launchd job、各持一條連線之後，訂閱數對半分，指數核心組的資料品質不再
被個股期貨的訂閱量拖累。這支**不修改** ``collect_ccf_books_websocket``，只是 import
它並覆寫模組層級的 ``ROOTS``——那支目前同時帶著多個 session 的未提交改動，能不碰
就不碰。

**唯讀，無任何送單路徑。**

用法
----
    PYTHONPATH=src .venv-fubon/bin/python scripts/research/collect_stock_futures_books.py
    # 覆寫收集清單（逗號分隔的 root 代碼）
    FUTOPT_STOCK_ROOTS=CKF,DQF,PWF PYTHONPATH=src .venv-fubon/bin/python \\
        scripts/research/collect_stock_futures_books.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collect_ccf_books_websocket as base  # noqa: E402


def main() -> int:
    override = os.environ.get("FUTOPT_STOCK_ROOTS")
    if override:
        roots = [x.strip().upper() for x in override.split(",") if x.strip()]
    else:
        # 預設＝ momentum-rotation 舊 universe（12）＋ 2026-08-20 成本篩選白名單（7）。
        # CCF（聯電）**刻意不在這裡**：它留在指數核心組那條連線上，已連續收了 9 天，
        # 是目前唯一有長序列的個股期貨，不要為了搬家中斷它。
        roots = list(base._STOCK_FUTURES_PAUSED_20260821)
    if not roots:
        print("no roots configured", flush=True)
        return 1
    base.ROOTS = roots
    print(f"stock-futures collector · {len(roots)} roots: {','.join(roots)}", flush=True)
    return base.main()


if __name__ == "__main__":
    raise SystemExit(main())
