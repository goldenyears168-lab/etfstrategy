"""專案設定：ETF 代碼、Universe、Score 權重、排程預設（单一真相來源）。

bash 讀取：python project_config.py etf-codes-listed
"""

from __future__ import annotations

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

BENCHMARK_CODES: tuple[str, ...] = ("IX0001", "IR0002")

# --- Universe ---
DEFAULT_TOP_N = 10
DEFAULT_MAX_POOL = 20
DEFAULT_EVENT_WINDOW_DAYS = 7

# --- Sync 預設 ---
DEFAULT_MARKET_HISTORY_DAYS = 90
DEFAULT_ETF_SIGNAL_LOOKBACK_DAYS = 14
DEFAULT_STOCK_MARKET_LOOKBACK_DAYS = 60
DEFAULT_NEWS_LOOKBACK_DAYS = 7
DEFAULT_PERPLEXITY_VERIFY_MAX = 8

# --- Score Engine (p4-v2) ---
SCORE_VERSION = "p4-v2"
NEUTRAL_SUBSCORE = 50.0

WEIGHT_SMART_MONEY = 0.50
WEIGHT_CATALYST = 0.0  # 註解-only；保留 catalyst 子分供未來 Catalyst Attribution
WEIGHT_EXPECTATION = 0.15
WEIGHT_FUNDAMENTAL = 0.15
WEIGHT_RISK = 0.10

# --- Flow Events / Attribution (v0.3) ---
FLOW_VERSION = "flow-v1"
DEFAULT_FLOW_EVENT_LOOKBACK = 20
BASELINE_RANDOM_SEED = 42
FLOW_HORIZONS = (1, 3, 5, 10)
FLOW_PRIMARY_HORIZONS = (3, 5)

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
