"""專案設定：ETF 代碼、Universe、Score 權重、排程預設（单一真相來源）。

bash 讀取：python project_config.py etf-codes-listed
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable

# --- ETF 代碼 ---
ETF_CODES_LISTED: tuple[str, ...] = (
    "00981A",
    "00403A",
    "009816",
    "00980A",
    "00982A",
    "00992A",
)
# 00407A 未掛牌 TEJ 日線，但凱基 KGIFUND 持股仍追蹤
ETF_CODES_HOLDINGS: tuple[str, ...] = ETF_CODES_LISTED + ("00407A",)

ETF_CODES_BY_SOURCE: dict[str, tuple[str, ...]] = {
    "ezmoney": ("00981A", "00403A"),
    "kgifund": ("009816", "00407A"),
    "capitalfund": ("00982A", "00992A"),
    "nomura": ("00980A",),
}

DEFAULT_ETF_CODES = ETF_CODES_LISTED

# 併入成分股 watchlist 的境內基金（最新月前十大 + 最新季完整聯集）
MUTUAL_FUND_WATCHLIST_CODES: tuple[str, ...] = ("ACDD04",)  # 安聯台灣科技基金

# 併入成分股 watchlist 的基準 ETF（僅擴充 K 線 universe，不進 flow 訊號）
BENCHMARK_ETF_WATCHLIST_CODES: tuple[str, ...] = (
    "0050",  # 元大台灣50
    "0056",  # 元大高股息
)

BENCHMARK_CODES: tuple[str, ...] = ("IX0001", "IR0002")

# --- Universe ---
DEFAULT_TOP_N = 10
DEFAULT_MAX_POOL = 20
DEFAULT_EVENT_WINDOW_DAYS = 7

# --- Sync 預設 ---
DEFAULT_MARKET_HISTORY_DAYS = 90
DEFAULT_ETF_SIGNAL_LOOKBACK_DAYS = 14
DEFAULT_STOCK_MARKET_LOOKBACK_DAYS = 60
# 歷史 backfill（約 2 年 ≈ 504 交易日；730 曆日較保守）
DEFAULT_BACKFILL_CALENDAR_DAYS = 730
DEFAULT_BACKFILL_CHUNK_DAYS = 90
# --- Score Engine ---
SCORE_VERSION_P4 = "p4-v2"
SCORE_VERSION_P5 = "p5-v1"
SCORE_VERSION_P5_V2 = "p5-v2"
SCORE_VERSION_P6 = "p6-tier"
SCORE_VERSION_DEFAULT = SCORE_VERSION_P6
# 向後相容：模組 import 時快照；執行期請用 active_score_version()
SCORE_VERSION = os.environ.get("SCORE_VERSION", SCORE_VERSION_DEFAULT).strip() or SCORE_VERSION_DEFAULT

NEUTRAL_SUBSCORE = 50.0

# p4-v2
WEIGHT_SMART_MONEY = 0.50
WEIGHT_CATALYST = 0.0  # 註解-only；保留 catalyst 子分供未來 Catalyst Attribution
WEIGHT_EXPECTATION = 0.15
WEIGHT_FUNDAMENTAL = 0.15
WEIGHT_RISK = 0.10

# p5-v1（資金面 70% / 基本面 20% / 風險 10%）
P5_WEIGHT_ETF_FLOW = 0.30
P5_WEIGHT_INSTITUTIONAL = 0.20
P5_WEIGHT_SHORT_FAVOR = 0.10  # 100 − short_pressure_score
P5_WEIGHT_CROWD = 0.10
P5_WEIGHT_CATALYST = 0.10
P5_WEIGHT_EXPECTATION = 0.10
P5_WEIGHT_FUNDAMENTAL = 0.05
P5_WEIGHT_RISK = 0.05
P5_FLOW_GATE_MIN = 72.0
P5_SCORE_GATE_MIN = 75.0
P5_RISK_GATE_MIN = 50.0  # risk 子分低於此 → 降級（p5-v2 / P5_RISK_AS_GATE）

# p5-v2：Risk 移出 Alpha 加總（權重重新正規化至 100%）
P5_V2_WEIGHT_ETF_FLOW = P5_WEIGHT_ETF_FLOW / (1.0 - P5_WEIGHT_RISK)
P5_V2_WEIGHT_INSTITUTIONAL = P5_WEIGHT_INSTITUTIONAL / (1.0 - P5_WEIGHT_RISK)
P5_V2_WEIGHT_SHORT_FAVOR = P5_WEIGHT_SHORT_FAVOR / (1.0 - P5_WEIGHT_RISK)
P5_V2_WEIGHT_CROWD = P5_WEIGHT_CROWD / (1.0 - P5_WEIGHT_RISK)
P5_V2_WEIGHT_CATALYST = P5_WEIGHT_CATALYST / (1.0 - P5_WEIGHT_RISK)
P5_V2_WEIGHT_EXPECTATION = P5_WEIGHT_EXPECTATION / (1.0 - P5_WEIGHT_RISK)
P5_V2_WEIGHT_FUNDAMENTAL = P5_WEIGHT_FUNDAMENTAL / (1.0 - P5_WEIGHT_RISK)

# p6-tier：Flow+Expectation 加權排序；籌碼／風險／價位為分層 Gate（不進加權）
# 2026-06 研究軌校準：各層目標通過率約 60–75%（原 72/75 過嚴）
P6_WEIGHT_FLOW = 0.70
P6_WEIGHT_EXPECTATION = 0.30
P6_FLOW_GATE_MIN = 55.0
P6_SCORE_GATE_MIN = 65.0
P6_FLOW_CANDIDATE_MIN = 25.0
P6_CROWD_GATE_MIN = 30.0
P6_SHORT_FAVOR_GATE_MIN = 20.0
P6_TIMING_GATE_MIN = 45.0
P6_RISK_GATE_MIN = 45.0

FLOW_PYRAMID_THIRD_WEIGHT_FACTOR = 0.5
FLOW_PRE_EVENT_DAYS = 10
FLOW_REPEAT_LOOKBACK_EVENTS = 30
FLOW_REGIME_ALPHA_LAYOUT_MAX = 2.0
FLOW_REGIME_ALPHA_MOMENTUM_MIN = 5.0
FLOW_PYRAMID_PRIOR_RETAIN = 0.80
FLOW_RS_REPEAT_MIN = 50.0
FLOW_L2_STRONG_BOOST = 2.0
FLOW_SINGLE_FLOW_MIN_PRIMARY = 68.0
FLOW_TIMING_LAYOUT_MIN = 45.0

# --- Flow Events / Attribution (v0.3) ---
FLOW_VERSION = "flow-v1"
DEFAULT_FLOW_EVENT_LOOKBACK = 20
BASELINE_RANDOM_SEED = 42
FLOW_HORIZONS = (1, 3, 5, 10, 20)
SIGNAL_LAYER_HORIZONS = (1, 3, 5, 10)
NAIVE_BASELINE_HORIZONS = (1, 3, 5, 10)
NAIVE_BASELINE_PRIMARY_H = 5
FLOW_PRIMARY_HORIZONS = (3, 5)
BASELINE_ROUND_TRIP_COST_PCT = 0.3
BASELINE_TOP_N = 10

SMART_MONEY_FLOW_BLEND = 0.55
SMART_MONEY_CHIP_BLEND = 0.45

ROLE_WEIGHT: dict[str, float] = {"CORE": 1.0, "THEMATIC": 0.75, "SATELLITE": 0.5}
CATALYST_BASELINE_MONEY = 45.0
CATALYST_BASELINE_EVENT = 40.0
FLOW_NO_SIGNAL = 38.0
CATALYST_UNCONFIRMED_CAP = 55.0
CATALYST_LOW_CONF_CAP = 65.0

NON_TECH_THEMES = frozenset(
    {
        "FINANCIAL",
        "DEFENSIVE_TELCO",
        "CYCLE_CHEM",
        "CYCLE_STEEL",
        "CONSUMER",
        "HARDWARE",
        "SHIPPING",
        "UNKNOWN",
    }
)

# --- Portfolio ---
DEFAULT_CAPITAL_NTD = 100_000.0

# --- L8 / L8.5 ---
GAP_SCALE_ROE_PP = 2.5
GAP_SCALE_EPS_PCT = 1.0
ACCEL_SCALE_PP = 3.0


def active_score_version() -> str:
    """執行期 Score 版本（預設 p6-tier；env SCORE_VERSION 可回退 p5/p4）。"""
    raw = os.environ.get("SCORE_VERSION", SCORE_VERSION_DEFAULT).strip()
    if raw in (
        SCORE_VERSION_P4,
        SCORE_VERSION_P5,
        SCORE_VERSION_P5_V2,
        SCORE_VERSION_P6,
    ):
        return raw
    return SCORE_VERSION_DEFAULT


def is_tier_score_version(version: str | None = None) -> bool:
    """p6-tier：三軸分層（Flow / Expectation / Timing），籌碼為 Gate。"""
    return (version or active_score_version()) == SCORE_VERSION_P6


def risk_as_gate_enabled() -> bool:
    """Risk 不進 Alpha 加總、改為風控 Gate（p6-tier / p5-v2 或 env P5_RISK_AS_GATE=1）。"""
    if os.environ.get("P5_RISK_AS_GATE", "0").strip() == "1":
        return True
    return active_score_version() in (SCORE_VERSION_P5_V2, SCORE_VERSION_P6)


def score_weights(version: str | None = None) -> dict[str, float]:
    v = version or active_score_version()
    if v == SCORE_VERSION_P6:
        return {
            "etf_flow": P6_WEIGHT_FLOW,
            "expectation": P6_WEIGHT_EXPECTATION,
            "risk": 0.0,
        }
    if v in (SCORE_VERSION_P5, SCORE_VERSION_P5_V2):
        if v == SCORE_VERSION_P5_V2 or risk_as_gate_enabled():
            return {
                "etf_flow": P5_V2_WEIGHT_ETF_FLOW,
                "institutional": P5_V2_WEIGHT_INSTITUTIONAL,
                "short_favor": P5_V2_WEIGHT_SHORT_FAVOR,
                "crowd": P5_V2_WEIGHT_CROWD,
                "catalyst": P5_V2_WEIGHT_CATALYST,
                "expectation": P5_V2_WEIGHT_EXPECTATION,
                "fundamental": P5_V2_WEIGHT_FUNDAMENTAL,
                "risk": 0.0,
            }
        return {
            "etf_flow": P5_WEIGHT_ETF_FLOW,
            "institutional": P5_WEIGHT_INSTITUTIONAL,
            "short_favor": P5_WEIGHT_SHORT_FAVOR,
            "crowd": P5_WEIGHT_CROWD,
            "catalyst": P5_WEIGHT_CATALYST,
            "expectation": P5_WEIGHT_EXPECTATION,
            "fundamental": P5_WEIGHT_FUNDAMENTAL,
            "risk": P5_WEIGHT_RISK,
        }
    return {
        "smart_money": WEIGHT_SMART_MONEY,
        "catalyst": WEIGHT_CATALYST,
        "expectation": WEIGHT_EXPECTATION,
        "fundamental": WEIGHT_FUNDAMENTAL,
        "risk": WEIGHT_RISK,
    }


def csv_codes(codes: tuple[str, ...]) -> str:
    return ",".join(codes)


def parse_etf_codes(
    arg: str | None,
    *,
    default: tuple[str, ...] | None = None,
) -> tuple[str, ...]:
    """將逗號分隔代碼字串轉 tuple；空值回 default（預設 ETF_CODES_LISTED）。"""
    if not arg:
        return default if default is not None else ETF_CODES_LISTED
    return tuple(c.strip().upper() for c in arg.split(",") if c.strip())


def parse_etf_codes_arg(
    etf_code: str | None,
    etf_codes: str | None,
    *,
    default: tuple[str, ...] = ("00981A",),
) -> tuple[str, ...]:
    """CLI 相容：--etf-code 單檔或 --etf-codes 多檔。"""
    if etf_codes:
        return parse_etf_codes(etf_codes, default=())
    if etf_code:
        return (etf_code.upper(),)
    return default


def shell_export() -> str:
    """輸出 bash eval 用變數（供 daily_sync.sh source）。"""
    lines = [
        f'ETF_CODES="{csv_codes(ETF_CODES_LISTED)}"',
        f'ETF_CODES_HOLDINGS="{csv_codes(ETF_CODES_HOLDINGS)}"',
        f'ETF_CODES_EZMONEY="{csv_codes(ETF_CODES_BY_SOURCE["ezmoney"])}"',
        f'ETF_CODES_KGIFUND="{csv_codes(ETF_CODES_BY_SOURCE["kgifund"])}"',
        f'ETF_CODES_CAPITALFUND="{csv_codes(ETF_CODES_BY_SOURCE["capitalfund"])}"',
        f'ETF_CODES_NOMURA="{csv_codes(ETF_CODES_BY_SOURCE["nomura"])}"',
        f'BENCHMARK_CODES="{csv_codes(BENCHMARK_CODES)}"',
    ]
    return "\n".join(lines)


_CLI_COMMANDS: dict[str, Callable[[], None]] = {
    "etf-codes-listed": lambda: print(csv_codes(ETF_CODES_LISTED)),
    "etf-codes-holdings": lambda: print(csv_codes(ETF_CODES_HOLDINGS)),
    "etf-codes-ezmoney": lambda: print(csv_codes(ETF_CODES_BY_SOURCE["ezmoney"])),
    "etf-codes-kgifund": lambda: print(csv_codes(ETF_CODES_BY_SOURCE["kgifund"])),
    "etf-codes-capitalfund": lambda: print(csv_codes(ETF_CODES_BY_SOURCE["capitalfund"])),
    "etf-codes-nomura": lambda: print(csv_codes(ETF_CODES_BY_SOURCE["nomura"])),
    "benchmark-codes": lambda: print(csv_codes(BENCHMARK_CODES)),
    "shell-export": lambda: print(shell_export()),
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or args[0] not in _CLI_COMMANDS:
        names = ", ".join(sorted(_CLI_COMMANDS))
        print(f"Usage: python project_config.py <{names}>", file=sys.stderr)
        return 2
    _CLI_COMMANDS[args[0]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
