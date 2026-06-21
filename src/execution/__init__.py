"""Execution layer · 執行層（券商連線 · 帳務 · 委託）。

依賴方向：strategy / research 不得 import 本 package。
"""

__all__ = [
    "FubonSession",
    "account_snapshot",
    "check_python_version",
    "connect_fubon",
]
