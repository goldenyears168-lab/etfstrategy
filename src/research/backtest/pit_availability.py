"""台股財報 / 月營收的『市場可得日』（point-in-time）。

回測 PIT 鐵律：訊號日 T 只能用 **公告可得** 的資料。財報的 period_date 是期別標籤
（季報=季底 2026-03-31、月營收=月初 2026-06-01 代表該月），但要到公告後才可得，
若直接用 `period_date <= T` 判定會偷看未公開資料 10–90 天（2026-08 進階健檢發現）。

台股法定公告時程（採保守固定時滯）：
  - 季報 Q1–Q3：季底後 45 日內
  - 年報（Q4，12/31）：次年約 3/31（約 +90 日）
  - 月營收：次月 10 日

此為單一 SSOT，`finpilot_local_backtest` 與 `finmind_screener_common` 共用，避免時滯常數漂移。
"""

from __future__ import annotations

from datetime import date, timedelta


def financial_availability_date(period_date: str, period_type: str) -> str:
    """回傳該期別資料的市場可得日（ISO 字串）；availability <= 訊號日 才可用。"""
    p = str(period_date)
    y, m, d = int(p[:4]), int(p[5:7]), int(p[8:10])
    if period_type == "quarter":
        lag = 90 if m == 12 else 45  # 年報 ~90 日、Q1–Q3 ~45 日
        return (date(y, m, d) + timedelta(days=lag)).isoformat()
    # 月營收：次月 10 日公告
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return date(ny, nm, 10).isoformat()
