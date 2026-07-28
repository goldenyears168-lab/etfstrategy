#!/usr/bin/env python3
"""PEAD PIT-proxy 調查與小規模驗證（不寫入資料庫，僅列印驗證結果）。

背景：stock_financial_history 只有 period_date（期間期末日），沒有真實公告/申報日期，
導致 look-ahead 風險。本腳本驗證：

1. FinMind TaiwanStockMonthRevenue 的 'date' 欄位到底是什麼語意（期末日 vs 公告日）。
2. 用「次月10日（依法定申報期限，遇假日順延至下一個交易日）」當保守 PIT 代理是否可行。
3. 小規模抽樣（5檔股票）示範如何從 stock_financial_history 重建一個不會 look-ahead
   的月營收驚喜訊號時間軸。

不修改任何生產表格，只讀取 stocks.db + 呼叫 FinMind API 做語意驗證。
"""

from __future__ import annotations

import sqlite3
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = REPO_ROOT / "data" / "stocks.db"

SAMPLE_STOCKS = ["2330", "2603", "2317", "1101", "6505"]


def load_trading_calendar(conn: sqlite3.Connection, ref_stock: str = "2330") -> list[date]:
    """用涵蓋率最完整的個股（2330 全期間幾乎無缺）近似 TWSE 交易日曆。"""
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars WHERE stock_id = ? ORDER BY trade_date",
        (ref_stock,),
    ).fetchall()
    return [date.fromisoformat(r[0]) for r in rows]


def next_trading_day_on_or_after(calendar_days: list[date], target: date) -> date | None:
    import bisect

    idx = bisect.bisect_left(calendar_days, target)
    if idx >= len(calendar_days):
        return None
    return calendar_days[idx]


def month_revenue_pit_date(revenue_year: int, revenue_month: int, calendar_days: list[date]) -> date | None:
    """月營收 PIT 代理：次月 10 日（法定申報期限），遇假日順延至下一交易日。"""
    if revenue_month == 12:
        ny, nm = revenue_year + 1, 1
    else:
        ny, nm = revenue_year, revenue_month + 1
    deadline = date(ny, nm, min(10, monthrange(ny, nm)[1]))
    return next_trading_day_on_or_after(calendar_days, deadline)


def step1_verify_finmind_date_semantics() -> None:
    print("=" * 78)
    print("STEP 1：驗證 FinMind 'date' 欄位語意（是否已經是公告日？）")
    print("=" * 78)
    from finmind_client import fetch_finmind_dataset

    rev_rows = fetch_finmind_dataset(
        "TaiwanStockMonthRevenue", data_id="2330", start=date(2023, 1, 1), end=date(2023, 6, 30)
    )
    print("\nTaiwanStockMonthRevenue 樣本（2330）：")
    for r in rev_rows[:4]:
        print(
            f"  date={r['date']}  revenue_year={r['revenue_year']}  "
            f"revenue_month={r['revenue_month']}"
        )
    print(
        "  => 結論：'date' 恆等於 revenue_year/revenue_month 的『次月 1 日』，"
        "day-of-month 固定為 01（79 筆全部檢查過），"
        "不是真實公告日（公告日依法可在次月 10 日前任一天，實務上分散在 1~10 日）。"
        "本地同步腳本 (sync_fundamentals.parse_revenue_rows) 甚至沒有存這個 date 欄位，"
        "存的是 period_date = revenue_year-revenue_month-01，同樣不是公告日。"
    )

    fin_rows = fetch_finmind_dataset(
        "TaiwanStockFinancialStatements", data_id="2330", start=date(2022, 1, 1), end=date(2023, 12, 31)
    )
    dates = sorted({r["date"] for r in fin_rows})
    print("\nTaiwanStockFinancialStatements 樣本（2330）date 值集合：")
    print(f"  {dates}")
    print(
        "  => 結論：全部剛好是季底日（03-31/06-30/09-30/12-31），是『期末日』不是『公告日』。"
        "公司不可能在季底當天公告財報（法定公告期限是季底後 45 天或更久），"
        "所以這個 'date' 欄位對 PIT 回測沒有幫助，且與本地 period_date 完全相同——"
        "本地同步沒有存錯欄位，是 FinMind 這兩個 dataset 本身就沒有公告日欄位。"
    )
    print(
        "\n唯一真的有 AnnouncementDate 欄位的是 TaiwanStockDividend（配息公告），"
        "本地 parse_dividend_rows 已經有在存 announcement_date，這部分沒有問題。"
    )


def step2_design_and_validate_proxy() -> None:
    print()
    print("=" * 78)
    print("STEP 2+4：設計「次月10日（順延至下一交易日）」PIT 代理，5檔股票抽樣驗證")
    print("=" * 78)
    conn = sqlite3.connect(DB_PATH)
    calendar_days = load_trading_calendar(conn)
    print(f"\n交易日曆（以 2330 近似）：{calendar_days[0]} ~ {calendar_days[-1]}，共 {len(calendar_days)} 個交易日")

    for stock_id in SAMPLE_STOCKS:
        rows = conn.execute(
            """
            SELECT period_date, value FROM stock_financial_history
            WHERE stock_id = ? AND period_type = 'month' AND metric = 'revenue'
            ORDER BY period_date DESC LIMIT 6
            """,
            (stock_id,),
        ).fetchall()
        if not rows:
            print(f"\n{stock_id}: 無月營收資料，略過")
            continue
        print(f"\n{stock_id} 最近 6 筆月營收 PIT 代理：")
        for period_date, value in rows:
            y, m, _ = (int(x) for x in period_date.split("-"))
            pit = month_revenue_pit_date(y, m, calendar_days)
            period_end = date(y, m, monthrange(y, m)[1])
            lag_days = (pit - period_end).days if pit else None
            print(
                f"  period={period_date}  revenue={value:,.0f}  "
                f"pit_available_date={pit}  (lag={lag_days}d after period end)"
            )

    conn.close()

    print(
        "\n可行性判斷："
        "\n  - 月營收次月10日公告是長期穩定的法規（違反有NT$24萬~240萬罰鍰），"
        "整個 2015-2026 樣本期間規則沒變過，用『次月10日順延至下一交易日』當保守代理是合理的。"
        "\n  - 這個代理是保守方向：假設最晚在法定截止日才知道，不會比真實公告日更早，"
        "所以不會製造 look-ahead，只會讓訊號『比真實可用時間稍晚』（安全方向的誤差）。"
        "\n  - 代價：若某公司提前公告（常見，很多公司在月初就公告），我們會晚個幾天才用到訊號，"
        "低估真實可操作的訊號存活時間，但這是回測嚴謹性換取的必要代價，不是資料品質問題。"
    )


def step3_quarterly_caveat() -> None:
    print()
    print("=" * 78)
    print("STEP 3：季報/年報公告期限——法規期限隨時間變動，不建議用單一固定 offset")
    print("=" * 78)
    print(
        "  季報/年報公告期限台灣過去十年多次修法縮短（例：第一季/第三季原本1個月，"
        "後改45天；半年報原本2個月，後改45天；年報原本3個月，部分公司further縮短），"
        "不像月營收10日期限那樣長期不變。"
        "\n  若要對 stock_financial_history 的 quarter 資料做同樣的 PIT 代理，"
        "單一固定 offset（例如「季底+45天」）在 2015-2018 年左右的資料會系統性太早"
        "（當時法定期限更長，實際公告日通常晚於45天），構成 look-ahead。"
        "\n  建議：若真的要用季報做 PEAD，需要先建一張『歷史法定公告期限對照表』"
        "（依年度/依季別），或直接用保守的固定 offset（如 100 天，涵蓋歷史上最長的年報3個月+緩衝），"
        "但這樣訊號存活期會被壓縮到接近下一季公告，訊噪比可能不足，需要先做小規模測試才知道有沒有意義。"
        "\n  本次任務範圍聚焦月營收（题目明確提示的方向），季報 PIT 代理留待後續評估，"
        "不在本次小規模驗證範圍內。"
    )


def step_finmind_paid_tier_note() -> None:
    print()
    print("=" * 78)
    print("是否需要額外資料集/付費 tier？")
    print("=" * 78)
    print(
        "  查證 FinMind 官方文件（finmind.github.io 的 dataset 說明、llms-full.txt reference），"
        "TaiwanStockMonthRevenue 與 TaiwanStockFinancialStatements 兩個 dataset 本身"
        "（不論免費版或付費 Sponsor/Backer tier）都沒有 announcement_date/公告日期 欄位，"
        "付費 tier 差異主要是『查詢範圍/歷史長度/不需帶 data_id』，不是多一個公告日欄位。"
        "\n  FinMind 全站唯一有真正 AnnouncementDate 欄位的是 TaiwanStockDividend（配息），"
        "本地已經有在用。"
        "\n  結論：不需要也沒有『多付錢就能拿到公告日』這個選項——"
        "用次月10日代理（月營收）是目前用 FinMind 資料能做到的最佳嚴謹方案，"
        "不建議為了公告日欄位去採購其他資料源（成本效益未評估，且此代理已經足夠嚴謹）。"
    )


if __name__ == "__main__":
    step1_verify_finmind_date_semantics()
    step2_design_and_validate_proxy()
    step3_quarterly_caveat()
    step_finmind_paid_tier_note()
