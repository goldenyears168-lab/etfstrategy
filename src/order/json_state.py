"""共用 JSON 狀態檔讀寫 helper —— atomic write，供各 sleeve ledger 共用.

2026-08-08 code review 發現：order層7個sleeve各自重新實作ledger讀寫，其中6份
（dayflip/detach-gate/songshan/c18acc/leading-dip/expert-pool-staged-gate）都是
直接 ``Path.write_text()``，只有 tmf_channel_ledger.py 是 atomic write（先寫暫存檔
再 os-level replace）。直接 write_text() 在寫到一半被中斷（SIGKILL / 斷電 / crash）
時理論上會截斷或損毀檔案——這正是本次 code review 修掉的那幾個「部位在 ledger 上
消失蹤跡」bug 的同源風險類別。這裡抽成純 I/O helper（零業務邏輯、零行為改動），
讓所有 sleeve 都改用 atomic write，直接降低這類問題的整體暴露面。

不在這裡做的事（刻意）：不強制統一各 sleeve 的 ledger schema 或 dataclass 形狀——
五個 sleeve 的狀態機語意本來就不同（單筆單日 / 全持倉掃描 / 多部位 / per-job），
硬套一個共用形狀是過早抽象，不是這次要解決的問題。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json_or_default(path: Path, default: Any) -> Any:
    """讀取 JSON 狀態檔；檔案不存在、OSError 或內容不是合法 JSON 時回傳 default
    （不拋例外）。呼叫端仍應自行驗證回傳型別／欄位是否符合預期（例如是不是 dict、
    有沒有必要欄位）——這裡只保證「讀不到就給預設值」，不做 schema 驗證。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def atomic_write_json(
    path: Path, data: Any, *, indent: int | None = 2, trailing_newline: bool = False,
) -> None:
    """Atomic write：先寫暫存檔（同目錄，同檔名+.tmp），成功後用 os-level rename
    覆蓋正式檔。POSIX 上 ``Path.replace()`` 是原子操作，中途被中斷最壞情況是留下
    孤兒 .tmp 檔，正式檔內容維持中斷前的最後一次成功寫入，不會被截斷成半個 JSON。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    if trailing_newline:
        text += "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
