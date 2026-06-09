"""執行評估時間軸（單一入口 execution_eval.py · 不另建 .command）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")


@dataclass(frozen=True)
class ExecutionLayer:
    mode: str
    time_window: str
    title: str
    anchor: str
    needs_prices: bool = False


EXECUTION_LAYERS: tuple[ExecutionLayer, ...] = (
    ExecutionLayer(
        "pre_open",
        "08:25–08:40",
        "昨收定價初稿",
        "昨收 last_close",
    ),
    ExecutionLayer(
        "auction",
        "08:45–08:59",
        "試撮重算定稿",
        "試撮價",
        needs_prices=True,
    ),
    ExecutionLayer(
        "open",
        "09:00–09:05",
        "開盤 ROD 限價／市價",
        "開盤價",
        needs_prices=True,
    ),
    ExecutionLayer(
        "intraday",
        "09:05+",
        "未成交預覽",
        "盤中價",
        needs_prices=True,
    ),
)

_LAYER_BY_MODE = {layer.mode: layer for layer in EXECUTION_LAYERS}


def layer_for_mode(mode: str) -> ExecutionLayer | None:
    return _LAYER_BY_MODE.get(mode)


def layer_heading(mode: str) -> str:
    layer = _LAYER_BY_MODE.get(mode)
    if layer is None:
        return f"【{mode}】"
    return f"【{layer.time_window} · {mode}】{layer.title}"


def suggest_mode_by_clock(now: datetime | None = None) -> str:
    """依台北時間提示建議模式（僅參考，不強制）。"""
    now = now or datetime.now(TZ)
    hm = now.hour * 100 + now.minute
    if hm < 845:
        return "pre_open"
    if hm < 900:
        return "auction"
    if hm < 915:
        return "open"
    return "intraday"


def _cli_base(trade_date: str = "today") -> str:
    return (
        f".venv/bin/python src/execution_eval.py --trade-date {trade_date}"
    )


def next_step_lines(
    current_mode: str,
    *,
    trade_date: str = "today",
) -> list[str]:
    """本輪結束後的下一階段（同一 CLI）。"""
    if current_mode == "pre_open":
        return [
            "下一階段  08:45–08:59 · auction（試撮後定稿）",
            (
                f"  {_cli_base(trade_date)} --mode auction "
                "--prices 2330=2310,6223=5775 --persist"
            ),
            "  或 --price-source auto（需 FINMIND_TOKEN）",
            "  定稿後  --approve  核准 draft",
        ]
    if current_mode == "auction":
        return [
            "下一階段  09:00–09:05 · open（開盤執行方式）",
            (
                f"  {_cli_base(trade_date)} --mode open --apply-open "
                "--prices 2330=2310,6223=5775"
            ),
        ]
    if current_mode == "open":
        return [
            "下一階段  09:05+ · intraday（未成交預覽，預設不寫 DB）",
            (
                f"  {_cli_base(trade_date)} --mode intraday --preview "
                "--prices 2330=2310"
            ),
            "  09:00 於券商手動下單（尚未接 NEO API）",
        ]
    if current_mode == "intraday":
        return [
            "本日執行鏈結束；未成交可調整限價後於券商手動處理",
        ]
    return []


def print_execution_timeline(
    current_mode: str,
    *,
    trade_date: str = "today",
    price_source: str | None = None,
) -> None:
    suggested = suggest_mode_by_clock()
    print("")
    print("=== ① 執行時間軸（單一入口 · 不另建 .command）===")
    for layer in EXECUTION_LAYERS:
        if layer.mode == current_mode:
            mark = "▶ 本輪"
        elif layer.mode == suggested and layer.mode != current_mode:
            mark = "◎ 建議"
        else:
            mark = "○"
        price_note = " · 需 --prices" if layer.needs_prices else ""
        print(
            f"  {mark}  {layer.time_window}  {layer.mode:<10}  "
            f"{layer.title}（{layer.anchor}{price_note}）"
        )
    if current_mode != suggested:
        print(
            f"  提示  依台北時間建議模式為 {suggested}（本輪為 {current_mode}）"
        )
    if price_source:
        print(f"  本輪 price_source={price_source}")
    for line in next_step_lines(current_mode, trade_date=trade_date):
        print(f"  {line}")
