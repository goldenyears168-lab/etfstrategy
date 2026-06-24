"""RRG mono hold7 · 模式 C：純 seg_last 分數換倉（不要求左下／象限）。

滿槽時：challenger seg_last 勝過 held 門檻 → 賣最弱腿、買 challenger。
空槽：fresh mono 依 seg_last 填倉（A 收盤 / C0 盤中）。
"""

from __future__ import annotations

# Research champion · 對外 slug · 回測簡稱（SSOT）
RRG_MONO_SWAP_ACCEL_SLUG = "rrg-mono-swap-accel"
RRG_MONO_SWAP_ACCEL_SHORT = "C18acc"
RRG_MONO_SWAP_ACCEL_LEGACY_VARIANT_IDS = (
    "C18-acc4-05",
    "C18-acel3-5-bavg",
)
CHAMPION_SCORE_SWAP_C_VARIANT_ID = RRG_MONO_SWAP_ACCEL_SHORT

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Literal

import pandas as pd

from analytics.bench import bench_return_entry_to_exit
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_swap_exit_b import (
    _entry_px,
    _fill_empty_slots,
    build_mono_tier2_calendar,
)
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from rrg_mono_daily_brief import HOLD_DAYS, LENGTH, LOOKBACK, MAX_SLOTS, TOP_N, ScanRow
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import load_kbar_day_closes, price_at_or_before_minute

CandidatePool = Literal["fresh", "mono_tier2", "mono_up", "mono_up_fresh", "fresh_union_accel"]
StructuralGate = Literal[
    "none",
    "down_left",
    "step_down_left",
    "avg_accel_down_left",
    "entry_window_avg_accel_down_left",
    "entry_split_avg_accel_down_left",
    "disp_accel_confirm",
    "accel_lead_decel",
]
EntryAccelGateMode = Literal["post_down_left", "both_down_left"]
ChallengerGate = Literal["none", "recent_accel_up", "v_dot_positive"]
CandidateRankKey = Literal["seg_last", "avg_accel_decel"]
EntryLeg = Literal["A", "C0"]
SwapTarget = Literal["worst_held", "each_held"]
TimingMode = Literal["close", "poll_5m"]
SortKey = Literal["seg_last", "rs_momentum", "seg_step_delta", "accel_decel", "avg_accel_decel"]
BuySortKey = Literal["seg_last", "rs_momentum", "accel_decel", "avg_accel_decel"]
AccelMetric = Literal["dot", "avg"]
BreadthPoolMode = Literal[
    "always_fresh",
    "mono_in_hot_zones",
    "swap_mono_in_hot_zones",
    "swap_union_accel_in_hot_zones",
]
BreadthChallengerPoolMode = Literal["entry_day", "swap_day"]
BreadthSwapZoneDate = Literal["entry_day", "swap_day"]
DEFAULT_BREADTH_HOT_ZONES: tuple[str, ...] = ("strong", "overbought")


@dataclass
class ScoreSwapCConfig:
    variant_id: str = "C1"
    label: str = "fresh 池 · A 進場 · 收盤換"
    entry_leg: EntryLeg = "A"
    candidate_pool: CandidatePool = "fresh"
    entry_pool: CandidatePool | None = None  # None → candidate_pool（空槽填倉）
    swap_pool: CandidatePool | None = None  # None → candidate_pool（換倉 challenger）
    entry_fallback_pool: CandidatePool | None = None  # None → 不降級；設定後 entry_shortlist 空時降級補倉
    candidate_top_n: int = TOP_N
    candidate_rank_key: CandidateRankKey = "seg_last"  # top-N 排序：seg_last 或四日加速
    candidate_require_positive_accel: bool = False  # top-N 须四日平均加速 > 0
    seg_margin: float = 0.0
    min_hold_days: int = 2
    max_hold_days: int = HOLD_DAYS
    timing_mode: TimingMode = "close"
    poll_interval_min: int = 5
    no_trade_before: str = "09:30"
    swap_target: SwapTarget = "worst_held"
    max_swaps_per_day: int = 1
    sort_key: SortKey = "seg_last"
    score_margin: float | None = None  # None → 使用 seg_margin（向後相容）
    decel_gate: bool = False  # seg_step_delta：僅換「最後一步減數」的 held
    structural_gate: StructuralGate = "none"
    entry_accel_pre_days: int = 4  # 進場前交易日數（含窗）
    entry_accel_post_days: int = 2  # 持有後交易日數（含窗）
    entry_accel_min_hold: int = 2  # 持滿 N 日才啟用 entry 窗加速度 gate
    entry_accel_gate_mode: EntryAccelGateMode = "post_down_left"
    challenger_gate: ChallengerGate = "none"
    buy_sort_key: BuySortKey | None = None  # None → 與 sort_key 同邏輯（accel 賣時買仍 seg_last）
    accel_lookback: int = LOOKBACK  # 四日加速计算窗（日）
    candidate_lookback: int = LOOKBACK  # 未持倉 challenger · fresh mono 軌跡窗（日）
    accel_sell_negative_only: bool = False  # pure accel：僅 v·a<0 的 held 可賣
    breadth_entry_zones: list[str] | None = None  # None → 不限制進場
    breadth_swap_zones: list[str] | None = None  # None → 不限制換倉
    breadth_pool_mode: BreadthPoolMode = "always_fresh"
    breadth_challenger_pool_mode: BreadthChallengerPoolMode = "entry_day"
    # swap gate 用哪日 zone：swap_day=as_of（換倉日）；entry_day=held leg signal_date
    breadth_swap_zone_date: BreadthSwapZoneDate = "swap_day"

    @property
    def effective_margin(self) -> float:
        return self.seg_margin if self.score_margin is None else self.score_margin

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_SCORE_C_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig("C1", "fresh · A 進 · 收盤換"),
    ScoreSwapCConfig("C2", "fresh · C0 進 · 收盤換", entry_leg="C0"),
    ScoreSwapCConfig("C3", "mono_tier2 · A 進 · 收盤換", candidate_pool="mono_tier2"),
    ScoreSwapCConfig("C4", "mono_tier2 · C0 進 · 收盤換", entry_leg="C0", candidate_pool="mono_tier2"),
    ScoreSwapCConfig("C5", "fresh · C0 · seg_margin=0.1", entry_leg="C0", seg_margin=0.1),
    ScoreSwapCConfig("C6", "fresh · C0 · 5m 盤中換", entry_leg="C0", timing_mode="poll_5m"),
    ScoreSwapCConfig(
        "C7",
        "fresh · C0 · max_swaps=2/日",
        entry_leg="C0",
        max_swaps_per_day=2,
    ),
    ScoreSwapCConfig(
        "C8",
        "fresh · C0 · min_hold=5",
        entry_leg="C0",
        min_hold_days=5,
    ),
    ScoreSwapCConfig(
        "C9",
        "fresh · C0 · min_hold=5 · seg_margin=0.1",
        entry_leg="C0",
        min_hold_days=5,
        seg_margin=0.1,
    ),
    ScoreSwapCConfig(
        "C10",
        "fresh · A · min_hold=5",
        entry_leg="A",
        min_hold_days=5,
    ),
    ScoreSwapCConfig(
        "C11",
        "fresh · C0 · min_hold=5 · max_hold=10",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
    ),
    ScoreSwapCConfig(
        "C12",
        "fresh · C0 · min_hold=5 · max_hold=10 · margin=0.1",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        seg_margin=0.1,
    ),
    ScoreSwapCConfig(
        "C13",
        "fresh · C0 · min_hold=5 · max_hold=10 · 5m 換",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
    ),
]

# C13 鄰域 sweep · 基準 champion = C13
C13_NEIGHBORHOOD_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C13",
        "baseline champion · min5 max10 · 5m 換",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
    ),
    ScoreSwapCConfig(
        "C14",
        "earlier swap · min_hold=4",
        entry_leg="C0",
        min_hold_days=4,
        max_hold_days=10,
        timing_mode="poll_5m",
    ),
    ScoreSwapCConfig(
        "C15",
        "more conservative · min_hold=6",
        entry_leg="C0",
        min_hold_days=6,
        max_hold_days=10,
        timing_mode="poll_5m",
    ),
    ScoreSwapCConfig(
        "C16",
        "shorter window · max_hold=8",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=8,
        timing_mode="poll_5m",
    ),
    ScoreSwapCConfig(
        "C17",
        "longer window · max_hold=12",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=12,
        timing_mode="poll_5m",
    ),
    ScoreSwapCConfig(
        "C18",
        "reduce churn · seg_margin=0.1",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        seg_margin=0.1,
        timing_mode="poll_5m",
    ),
    ScoreSwapCConfig(
        "C13e",
        "close swap execution (vs 5m)",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="close",
    ),
    ScoreSwapCConfig(
        "C13f",
        "no swap · max_hold exit only",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        max_swaps_per_day=0,
    ),
    # optional min_hold × max_hold grid corners
    ScoreSwapCConfig(
        "C19",
        "grid · min4 max8",
        entry_leg="C0",
        min_hold_days=4,
        max_hold_days=8,
        timing_mode="poll_5m",
    ),
    ScoreSwapCConfig(
        "C20",
        "grid · min4 max12",
        entry_leg="C0",
        min_hold_days=4,
        max_hold_days=12,
        timing_mode="poll_5m",
    ),
    ScoreSwapCConfig(
        "C21",
        "grid · min6 max8",
        entry_leg="C0",
        min_hold_days=6,
        max_hold_days=8,
        timing_mode="poll_5m",
    ),
    ScoreSwapCConfig(
        "C22",
        "grid · min6 max12",
        entry_leg="C0",
        min_hold_days=6,
        max_hold_days=12,
        timing_mode="poll_5m",
    ),
]


DEFAULT_C18_MOM_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18",
        "seg_last · margin=0.1（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        seg_margin=0.0,
        score_margin=0.1,
        timing_mode="poll_5m",
        sort_key="seg_last",
    ),
    ScoreSwapCConfig(
        "C18-mom1a",
        "M1 rs_momentum 水位 · margin=0.5",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="rs_momentum",
        score_margin=0.5,
    ),
    ScoreSwapCConfig(
        "C18-mom1b",
        "M1 rs_momentum 水位 · margin=1.0",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="rs_momentum",
        score_margin=1.0,
    ),
    ScoreSwapCConfig(
        "C18-mom2a",
        "M2 RRG 位移減數 · seg_step_delta · margin=0.05 · decel_gate",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_step_delta",
        score_margin=0.05,
        decel_gate=True,
    ),
    ScoreSwapCConfig(
        "C18-mom2b",
        "M2 RRG 位移減數 · seg_step_delta · margin=0.1 · decel_gate",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_step_delta",
        score_margin=0.1,
        decel_gate=True,
    ),
    ScoreSwapCConfig(
        "C18-mom2c",
        "M2 RRG 位移減數 · seg_step_delta · margin=0.2 · decel_gate",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_step_delta",
        score_margin=0.2,
        decel_gate=True,
    ),
    ScoreSwapCConfig(
        "C18-mom2d",
        "M2 seg_step_delta · 無 decel_gate（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_step_delta",
        score_margin=0.05,
        decel_gate=False,
    ),
    ScoreSwapCConfig(
        "C18-dl1",
        "減速 + down_left · seg_step_delta · margin=0.05",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_step_delta",
        score_margin=0.05,
        decel_gate=True,
        structural_gate="down_left",
    ),
    ScoreSwapCConfig(
        "C18-dl2",
        "減速 + down_left · seg_step_delta · margin=0.1",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_step_delta",
        score_margin=0.1,
        decel_gate=True,
        structural_gate="down_left",
    ),
    ScoreSwapCConfig(
        "C18-dl3",
        "減速 + down_left · seg_last 買方 · margin=0.1",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.1,
        decel_gate=True,
        structural_gate="down_left",
    ),
]

# C18-dl1 鄰域 · 減速 + down_left · margin 以 0.08 為中心
C18_DL_NEIGHBORHOOD_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18",
        "seg_last · margin=0.1（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        score_margin=0.1,
        timing_mode="poll_5m",
        sort_key="seg_last",
    ),
    ScoreSwapCConfig(
        "C18-dl1",
        "dl baseline · seg_step_delta · margin=0.05",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_step_delta",
        score_margin=0.05,
        decel_gate=True,
        structural_gate="down_left",
    ),
    *[
        ScoreSwapCConfig(
            f"C18-dl{m}",
            f"減速+down_left · seg_step_delta · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="seg_step_delta",
            score_margin=round(m / 100, 2),
            decel_gate=True,
            structural_gate="down_left",
        )
        for m in (6, 7, 8, 9, 10)
    ],
    ScoreSwapCConfig(
        "C18-dls1",
        "down_left only · seg_last · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        decel_gate=False,
        structural_gate="down_left",
    ),
    *[
        ScoreSwapCConfig(
            f"C18-dls{m}",
            f"減速+down_left · seg_last · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="seg_last",
            score_margin=round(m / 100, 2),
            decel_gate=True,
            structural_gate="down_left",
        )
        for m in (6, 7, 8, 9, 10)
    ],
    ScoreSwapCConfig(
        "C18-dla1",
        "最後一步加速度 down_left · seg_last · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        decel_gate=False,
        structural_gate="step_down_left",
    ),
]

# C18-dla · a=Δv/Δt · 最後一步 step_down_left · seg_last margin 0.06–0.10
C18_DLA_MARGIN_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18",
        "seg_last · margin=0.1（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        score_margin=0.1,
        timing_mode="poll_5m",
        sort_key="seg_last",
    ),
    ScoreSwapCConfig(
        "C18-dls1",
        "4日位移 down_left · seg_last · margin=0.08（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        decel_gate=False,
        structural_gate="down_left",
    ),
    *[
        ScoreSwapCConfig(
            f"C18-dla{m}",
            f"step_down_left · seg_last · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="seg_last",
            score_margin=round(m / 100, 2),
            decel_gate=False,
            structural_gate="step_down_left",
        )
        for m in (6, 7, 8, 9, 10)
    ],
]


# C18-dlb · 4 日回看平均加速度 ā=mean(Δv/Δt) · avg_accel_down_left · margin 0.06–0.10
C18_DLB_MARGIN_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18",
        "seg_last · margin=0.1（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        score_margin=0.1,
        timing_mode="poll_5m",
        sort_key="seg_last",
    ),
    ScoreSwapCConfig(
        "C18-dls1",
        "4日位移 down_left · seg_last · margin=0.08（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        decel_gate=False,
        structural_gate="down_left",
    ),
    ScoreSwapCConfig(
        "C18-dla6",
        "最後一步 step_down_left · margin=0.06（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.06,
        decel_gate=False,
        structural_gate="step_down_left",
    ),
    *[
        ScoreSwapCConfig(
            f"C18-dlb{m}",
            f"4日平均加速度 down_left · seg_last · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="seg_last",
            score_margin=round(m / 100, 2),
            decel_gate=False,
            structural_gate="avg_accel_down_left",
        )
        for m in (6, 7, 8, 9, 10)
    ],
]

# 進場前 4 日 → 持有後 2 日 · 固定窗 ā · margin 0.06–0.10
C18_DLW_MARGIN_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18",
        "seg_last · margin=0.1（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        score_margin=0.1,
        timing_mode="poll_5m",
        sort_key="seg_last",
    ),
    ScoreSwapCConfig(
        "C18-dls1",
        "4日位移 down_left · seg_last · margin=0.08（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
    ),
    *[
        ScoreSwapCConfig(
            f"C18-dlw{m}",
            f"進場前4日→持後2日 ā down_left · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="seg_last",
            score_margin=round(m / 100, 2),
            structural_gate="entry_window_avg_accel_down_left",
            entry_accel_pre_days=4,
            entry_accel_post_days=2,
            entry_accel_min_hold=2,
        )
        for m in (6, 7, 8, 9, 10)
    ],
]

# 進場前 3 日 / 持有後 3 日 · 各自 ā · margin 0.06–0.10
C18_DLX_MARGIN_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18",
        "seg_last · margin=0.1（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        score_margin=0.1,
        timing_mode="poll_5m",
        sort_key="seg_last",
    ),
    ScoreSwapCConfig(
        "C18-dls1",
        "4日位移 down_left · seg_last · margin=0.08（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
    ),
    *[
        ScoreSwapCConfig(
            f"C18-dlx{m}",
            f"買前3日/持後3日 ā · post down_left · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="seg_last",
            score_margin=round(m / 100, 2),
            structural_gate="entry_split_avg_accel_down_left",
            entry_accel_pre_days=3,
            entry_accel_post_days=3,
            entry_accel_min_hold=3,
            entry_accel_gate_mode="post_down_left",
        )
        for m in (6, 7, 8, 9, 10)
    ],
    ScoreSwapCConfig(
        "C18-dlx8b",
        "買前3日+持後3日 ā · pre&post both down_left · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="entry_split_avg_accel_down_left",
        entry_accel_pre_days=3,
        entry_accel_post_days=3,
        entry_accel_min_hold=3,
        entry_accel_gate_mode="both_down_left",
    ),
]

# C18 + 當日加速度領先 · as_of 決策（非進場窗）
C18_ACCEL_LEAD_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18",
        "seg_last · margin=0.1（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        score_margin=0.1,
        timing_mode="poll_5m",
        sort_key="seg_last",
    ),
    ScoreSwapCConfig(
        "C18-dls1",
        "4日位移 down_left · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
    ),
    ScoreSwapCConfig(
        "C18-dlap",
        "dla+ · 位移+ā 確認 down_left · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="disp_accel_confirm",
    ),
    ScoreSwapCConfig(
        "C18-lead",
        "lead · ā down_left 且 a·v<0 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="accel_lead_decel",
    ),
    ScoreSwapCConfig(
        "C18-leadp",
        "lead+ · lead 賣 + challenger ā up · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="accel_lead_decel",
        challenger_gate="recent_accel_up",
    ),
    *[
        ScoreSwapCConfig(
            f"C18-lead{m}",
            f"lead · a·v<0 · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="seg_last",
            score_margin=round(m / 100, 2),
            structural_gate="accel_lead_decel",
        )
        for m in (6, 7, 9, 10)
    ],
]

# pure 加速度 · 賣 v·a 最負（減速最強）· 買 seg_last + margin
C18_PURE_ACCEL_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18",
        "seg_last · margin=0.1（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        score_margin=0.1,
        timing_mode="poll_5m",
        sort_key="seg_last",
    ),
    ScoreSwapCConfig(
        "C18-dls1",
        "4日位移 down_left · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
    ),
    ScoreSwapCConfig(
        "C18-acel1",
        "pure · v·a 最負 · 僅 v·a<0 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="accel_decel",
        score_margin=0.08,
        accel_sell_negative_only=True,
    ),
    ScoreSwapCConfig(
        "C18-acel2",
        "pure · v·a 最負 · 不限負 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="accel_decel",
        score_margin=0.08,
        accel_sell_negative_only=False,
    ),
    ScoreSwapCConfig(
        "C18-acel3",
        "pure · 4日窗 ā 最負 · 僅負 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.08,
        accel_sell_negative_only=True,
    ),
    *[
        ScoreSwapCConfig(
            f"C18-acel1-{m}",
            f"pure v·a · 僅負 · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="accel_decel",
            score_margin=round(m / 100, 2),
            accel_sell_negative_only=True,
        )
        for m in (6, 7, 9, 10)
    ],
]

# acel3 margin sweep · ā 最負 + down_left gate 組合
C18_ACEL3_FOLLOWUP_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18-dls1",
        "4日位移 down_left · margin=0.08（冠軍對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
    ),
    ScoreSwapCConfig(
        "C18-acel3",
        "pure · 4日窗 ā 最負 · 僅負 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.08,
        accel_sell_negative_only=True,
    ),
    *[
        ScoreSwapCConfig(
            f"C18-acel3-{m}",
            f"pure ā 最負 · 僅負 · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="avg_accel_decel",
            score_margin=round(m / 100, 2),
            accel_sell_negative_only=True,
        )
        for m in (6, 7, 8, 9, 10)
    ],
    *[
        ScoreSwapCConfig(
            f"C18-acel3-dl-{m}",
            f"down_left gate · ā 最負 · 僅負 · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="avg_accel_decel",
            score_margin=round(m / 100, 2),
            structural_gate="down_left",
            accel_sell_negative_only=True,
        )
        for m in (6, 7, 8, 9, 10)
    ],
    ScoreSwapCConfig(
        "C18-acel3-dlap",
        "disp∧ā down_left · ā 最負賣 · 僅負 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.08,
        structural_gate="disp_accel_confirm",
        accel_sell_negative_only=True,
    ),
    *[
        ScoreSwapCConfig(
            f"C18-acel3-dlap-{m}",
            f"disp∧ā confirm · ā 最負 · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="avg_accel_decel",
            score_margin=round(m / 100, 2),
            structural_gate="disp_accel_confirm",
            accel_sell_negative_only=True,
        )
        for m in (6, 7, 9, 10)
    ],
]

# Phase 1 · 買方「最近加速向上」gate · 賣法不變
C18_BUY_ACCEL_PHASE1_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18-acel3-5",
        "四日加速卖 · 买 seg_last · margin=0.05（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
    ),
    *[
        ScoreSwapCConfig(
            f"C18-acel3-5-up-{m}",
            f"四日加速卖 · 买加速向上 gate · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="avg_accel_decel",
            score_margin=round(m / 100, 2),
            accel_sell_negative_only=True,
            challenger_gate="recent_accel_up",
        )
        for m in (5, 6, 8)
    ],
    ScoreSwapCConfig(
        "C18-dls1",
        "down_left 賣 · 買 seg_last · margin=0.08（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
    ),
    *[
        ScoreSwapCConfig(
            f"C18-dls1-up-{m}",
            f"down_left 卖 · 买加速向上 gate · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="seg_last",
            score_margin=round(m / 100, 2),
            structural_gate="down_left",
            challenger_gate="recent_accel_up",
        )
        for m in (6, 8)
    ],
]

# Phase 2 · buy_sort_key + v_dot_positive gate
C18_BUY_ACCEL_PHASE2_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18-acel3-5",
        "四日加速卖 · 买 seg_last · margin=0.05（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
    ),
    ScoreSwapCConfig(
        "C18acc",
        "四日加速 · 卖转弱 · 买转强 · margin=0.05（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        buy_sort_key="avg_accel_decel",
    ),
    ScoreSwapCConfig(
        "C18acc-vdot",
        "四日加速卖 · 买按瞬时 v·a 最大 · margin=0.05",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        buy_sort_key="accel_decel",
    ),
    ScoreSwapCConfig(
        "C18-acel3-5-vdot",
        "四日加速卖 · 买 v·a>0 gate · seg_last 排 · margin=0.05",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        challenger_gate="v_dot_positive",
    ),
    ScoreSwapCConfig(
        "C18-acel3-5-vdot-acc4",
        "四日加速卖 · v·a>0 · 买按四日加速最大 · margin=0.05",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        challenger_gate="v_dot_positive",
        buy_sort_key="avg_accel_decel",
    ),
    *[
        ScoreSwapCConfig(
            f"C18-acel3-5-vdot-acc4-{m}",
            f"四日加速卖 · v·a>0 · 买按四日加速 · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="avg_accel_decel",
            score_margin=round(m / 100, 2),
            accel_sell_negative_only=True,
            challenger_gate="v_dot_positive",
            buy_sort_key="avg_accel_decel",
        )
        for m in (5, 6, 8)
    ],
    ScoreSwapCConfig(
        "C18-dls1",
        "down_left 賣 · 買 seg_last · margin=0.08（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
    ),
    ScoreSwapCConfig(
        "C18-dls1-acc4",
        "down_left 卖 · 买按四日加速最大 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
        buy_sort_key="avg_accel_decel",
    ),
    ScoreSwapCConfig(
        "C18-dls1-vdot",
        "down_left 賣 · v·a>0 · seg_last 排 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
        challenger_gate="v_dot_positive",
    ),
    ScoreSwapCConfig(
        "C18-dls1-vdot-acc4",
        "down_left 卖 · v·a>0 · 买按四日加速 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
        challenger_gate="v_dot_positive",
        buy_sort_key="avg_accel_decel",
    ),
]

# 四日加速 · 加速窗 3 日 vs 4 日（fresh mono 候選仍 4 日軌跡 · seg_last 門檻不變）
C18_ACC4_LB_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18acc",
        "四日加速 · 4 日窗 · margin=0.05（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        buy_sort_key="avg_accel_decel",
        accel_lookback=4,
    ),
    ScoreSwapCConfig(
        "C18-acc3-05",
        "四日加速 · 3 日窗 · margin=0.05",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        buy_sort_key="avg_accel_decel",
        accel_lookback=3,
    ),
    ScoreSwapCConfig(
        "C18-acc3-05-mh4",
        "四日加速 · 3 日窗 · min_hold=4 · margin=0.05",
        entry_leg="C0",
        min_hold_days=4,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        buy_sort_key="avg_accel_decel",
        accel_lookback=3,
    ),
    ScoreSwapCConfig(
        "C18-dls1",
        "down_left 4 日位移 · margin=0.08（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
    ),
]

# 提早 1 日换仓（min_hold=4）· 提高 seg_last 门槛 margin · 四日加速窗仍 4 日
C18_ACC4_EARLY_MARGIN_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18acc",
        "四日加速 · min_hold=5 · margin=0.05（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        buy_sort_key="avg_accel_decel",
        accel_lookback=4,
    ),
    *[
        ScoreSwapCConfig(
            f"C18acc-mh4-{m}",
            f"min_hold=4 · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=4,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="avg_accel_decel",
            score_margin=round(m / 100, 2),
            accel_sell_negative_only=True,
            buy_sort_key="avg_accel_decel",
            accel_lookback=4,
        )
        for m in (5, 6, 7, 8, 9, 10)
    ],
    ScoreSwapCConfig(
        "C18-dls1",
        "dls1 · min_hold=5 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
    ),
]

# 未买进候选 · fresh 轨迹 3 日 vs 4 日 · 可提高换仓 margin
C18_ACC4_CAND_LB_SWEEP: list[ScoreSwapCConfig] = [
    ScoreSwapCConfig(
        "C18acc",
        "候选 4 日 fresh · margin=0.05（對照）",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        buy_sort_key="avg_accel_decel",
        accel_lookback=4,
        candidate_lookback=4,
    ),
    *[
        ScoreSwapCConfig(
            f"C18acc-clb3-{m}",
            f"候选 3 日 fresh · margin={m/100:.2f}",
            entry_leg="C0",
            min_hold_days=5,
            max_hold_days=10,
            timing_mode="poll_5m",
            sort_key="avg_accel_decel",
            score_margin=round(m / 100, 2),
            accel_sell_negative_only=True,
            buy_sort_key="avg_accel_decel",
            accel_lookback=4,
            candidate_lookback=3,
        )
        for m in (5, 6, 7, 8, 9, 10)
    ],
    ScoreSwapCConfig(
        "C18-dls1",
        "dls1 · 候选 4 日 · margin=0.08",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="seg_last",
        score_margin=0.08,
        structural_gate="down_left",
        candidate_lookback=4,
    ),
]


def _champion_accel_fields() -> dict[str, Any]:
    return {
        "entry_leg": "C0",
        "min_hold_days": 5,
        "max_hold_days": 10,
        "timing_mode": "poll_5m",
        "sort_key": "avg_accel_decel",
        "score_margin": 0.05,
        "accel_sell_negative_only": True,
        "buy_sort_key": "avg_accel_decel",
        "accel_lookback": 4,
        "candidate_lookback": 4,
    }


def champion_config_for_candidate_pool(
    candidate_pool: CandidatePool,
    *,
    variant_id: str,
    label: str,
) -> ScoreSwapCConfig:
    return ScoreSwapCConfig(
        variant_id,
        label,
        candidate_pool=candidate_pool,
        **_champion_accel_fields(),
    )


# C18acc · 候选池：fresh leading vs mono_up（无 leading）· 换仓规则不变
C18acc_CANDIDATE_POOL_SWEEP: list[ScoreSwapCConfig] = [
    champion_config_for_candidate_pool(
        "fresh",
        variant_id=CHAMPION_SCORE_SWAP_C_VARIANT_ID,
        label="fresh mono · leading（对照）",
    ),
    champion_config_for_candidate_pool(
        "mono_up",
        variant_id=f"{CHAMPION_SCORE_SWAP_C_VARIANT_ID}-mu",
        label="mono_up · 无 leading · 全池",
    ),
    champion_config_for_candidate_pool(
        "mono_up_fresh",
        variant_id=f"{CHAMPION_SCORE_SWAP_C_VARIANT_ID}-muf",
        label="mono_up fresh · 无 leading · 新进",
    ),
    champion_config_for_candidate_pool(
        "mono_tier2",
        variant_id=f"{CHAMPION_SCORE_SWAP_C_VARIANT_ID}-mono",
        label="mono tier2 · leading · 全池",
    ),
]


def _nla_accel_sweep_config(
    suffix: str,
    label: str,
    *,
    candidate_pool: CandidatePool = "mono_up_fresh",
    **overrides: Any,
) -> ScoreSwapCConfig:
    fields = {
        **_champion_accel_fields(),
        "candidate_pool": candidate_pool,
        "candidate_rank_key": "seg_last",
        "candidate_require_positive_accel": False,
        "challenger_gate": "none",
        "candidate_top_n": TOP_N,
    }
    fields.update(overrides)
    return ScoreSwapCConfig(f"C18acc-nla-{suffix}", label, **fields)


# C18acc · 无 leading top10 · 加速度筛选 sweep（mono_up / mono_up_fresh 基池）
C18acc_NO_LEAD_ACCEL_SWEEP: list[ScoreSwapCConfig] = [
    champion_config_for_candidate_pool(
        "fresh",
        variant_id=CHAMPION_SCORE_SWAP_C_VARIANT_ID,
        label="fresh leading · 对照",
    ),
    _nla_accel_sweep_config("muf-sg", "muf · seg_last top10"),
    _nla_accel_sweep_config("muf-ar", "muf · 四日加速排序 top10", candidate_rank_key="avg_accel_decel"),
    _nla_accel_sweep_config("muf-ap", "muf · 四日加速>0", candidate_require_positive_accel=True),
    _nla_accel_sweep_config(
        "muf-arap",
        "muf · 加速排序 + 加速>0",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
    ),
    _nla_accel_sweep_config("muf-vd", "muf · v·a>0", challenger_gate="v_dot_positive"),
    _nla_accel_sweep_config(
        "muf-arap-vd",
        "muf · 加速排序+>0 · v·a>0",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
        challenger_gate="v_dot_positive",
    ),
    _nla_accel_sweep_config(
        "muf-rau",
        "muf · 最近加速向上",
        challenger_gate="recent_accel_up",
    ),
    _nla_accel_sweep_config(
        "muf-arap-rau",
        "muf · 加速排序+>0 · 最近加速向上",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
        challenger_gate="recent_accel_up",
    ),
    _nla_accel_sweep_config(
        "muf-arap-t5",
        "muf · 加速排序+>0 · top5",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
        candidate_top_n=5,
    ),
    _nla_accel_sweep_config(
        "muf-arap-t3",
        "muf · 加速排序+>0 · top3",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
        candidate_top_n=3,
    ),
    _nla_accel_sweep_config(
        "mu-arap",
        "mu 全池 · 加速排序+>0",
        candidate_pool="mono_up",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
    ),
    _nla_accel_sweep_config(
        "muf-arap-m6",
        "muf · 加速排序+>0 · margin=0.06",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
        score_margin=0.06,
    ),
    _nla_accel_sweep_config(
        "muf-arap-m8",
        "muf · 加速排序+>0 · margin=0.08",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
        score_margin=0.08,
    ),
    _nla_accel_sweep_config(
        "muf-arap-m10",
        "muf · 加速排序+>0 · margin=0.10",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
        score_margin=0.10,
    ),
    _nla_accel_sweep_config(
        "muf-arap-lb3",
        "muf · 加速排序+>0 · 3日窗",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
        accel_lookback=3,
    ),
    _nla_accel_sweep_config(
        "muf-arap-vd-m8",
        "muf · 加速排序+>0 · v·a>0 · margin=0.08",
        candidate_rank_key="avg_accel_decel",
        candidate_require_positive_accel=True,
        challenger_gate="v_dot_positive",
        score_margin=0.08,
    ),
]


def _pool_merge_champion(**overrides: Any) -> ScoreSwapCConfig:
    fields = {**_champion_accel_fields(), "candidate_pool": "fresh"}
    fields.update(overrides)
    return ScoreSwapCConfig(CHAMPION_SCORE_SWAP_C_VARIANT_ID, "C18acc champion", **fields)


def _pool_merge_variant(variant_id: str, label: str, **overrides: Any) -> ScoreSwapCConfig:
    fields = {**_champion_accel_fields(), "candidate_pool": "fresh"}
    fields.update(overrides)
    return ScoreSwapCConfig(variant_id, label, **fields)


# C18acc · 三方向漏斗合并 sweep（entry/swap 分池 · union · 加强 C）
C18acc_POOL_MERGE_SWEEP: list[ScoreSwapCConfig] = [
    _pool_merge_champion(),
    ScoreSwapCConfig(
        "C18acc-entry-fresh-swap-mono",
        "空槽 fresh · 换仓 mono_tier2",
        candidate_pool="fresh",
        entry_pool="fresh",
        swap_pool="mono_tier2",
        **_champion_accel_fields(),
    ),
    ScoreSwapCConfig(
        "C18acc-fresh-union-accel",
        "fresh ∪ (mono_tier2 ∧ 四日加速>0)",
        candidate_pool="fresh_union_accel",
        **_champion_accel_fields(),
    ),
    _pool_merge_variant("C18acc-pos-accel", "fresh · challenger 须四日加速>0", candidate_require_positive_accel=True),
    _pool_merge_variant("C18acc-m6", "fresh · margin=0.06", score_margin=0.06),
    _pool_merge_variant("C18acc-m7", "fresh · margin=0.07", score_margin=0.07),
    _pool_merge_variant("C18acc-m8", "fresh · margin=0.08", score_margin=0.08),
    _pool_merge_variant("C18acc-rau", "fresh · challenger 最近加速向上", challenger_gate="recent_accel_up"),
    _pool_merge_variant("C18acc-vdot", "fresh · challenger v·a>0", challenger_gate="v_dot_positive"),
]


def _breadth_funnel_variant(variant_id: str, label: str, **overrides: Any) -> ScoreSwapCConfig:
    fields = {**_champion_accel_fields(), "candidate_pool": "fresh"}
    fields.update(overrides)
    return ScoreSwapCConfig(variant_id, label, **fields)


_HOT_BREADTH_ZONES: list[str] = list(DEFAULT_BREADTH_HOT_ZONES)

# C18acc · Market breadth（廣度）zone 漏斗層 sweep（單一 spec · 非雙策略交替）
C18acc_BREADTH_FUNNEL_SWEEP: list[ScoreSwapCConfig] = [
    _pool_merge_champion(),
    _breadth_funnel_variant(
        "C18acc-breadth-entry-gate",
        "進場 gate · strong/overbought",
        breadth_entry_zones=_HOT_BREADTH_ZONES,
    ),
    _breadth_funnel_variant(
        "C18acc-breadth-swap-gate",
        "換倉 gate · strong/overbought",
        breadth_swap_zones=_HOT_BREADTH_ZONES,
    ),
    _breadth_funnel_variant(
        "C18acc-breadth-pool-mono-hot",
        "池切換 · hot=mono_tier2 else fresh",
        breadth_pool_mode="mono_in_hot_zones",
    ),
    _breadth_funnel_variant(
        "C18acc-breadth-swap-pool-mono",
        "進場 fresh · hot 日換 mono_tier2",
        breadth_pool_mode="swap_mono_in_hot_zones",
    ),
    _breadth_funnel_variant(
        "C18acc-breadth-union-hot",
        "進場 fresh · hot 日 swap∪(mono∧ā>0)",
        breadth_pool_mode="swap_union_accel_in_hot_zones",
    ),
]


def _swapday_breadth_variant(variant_id: str, label: str, **overrides: Any) -> ScoreSwapCConfig:
    fields = {**_champion_accel_fields(), "candidate_pool": "fresh"}
    fields.update(overrides)
    return ScoreSwapCConfig(variant_id, label, **fields)


# C18acc · 換倉日（as_of）廣度 zone 路由 vs 進場日路由 · 20260624 swap-day sweep
C18acc_BREADTH_SWAPDAY_SWEEP: list[ScoreSwapCConfig] = [
    _pool_merge_champion(),
    _swapday_breadth_variant(
        "C18acc-swapday-pool-mono",
        "換倉日 pool · 進 fresh · hot 日換 mono_tier2",
        breadth_pool_mode="swap_mono_in_hot_zones",
        breadth_challenger_pool_mode="swap_day",
    ),
    _swapday_breadth_variant(
        "C18acc-swapday-pool-union",
        "換倉日 pool · hot 日 swap∪(mono∧ā>0)",
        breadth_pool_mode="swap_union_accel_in_hot_zones",
        breadth_challenger_pool_mode="swap_day",
    ),
    _swapday_breadth_variant(
        "C18acc-swapday-gate",
        "換倉日 gate · 僅 hot 日執行換倉",
        breadth_swap_zones=_HOT_BREADTH_ZONES,
        breadth_swap_zone_date="swap_day",
    ),
    _swapday_breadth_variant(
        "C18acc-entryday-gate",
        "進場日 gate · 僅 held leg 進場日 hot 才換",
        breadth_swap_zones=_HOT_BREADTH_ZONES,
        breadth_swap_zone_date="entry_day",
    ),
    _breadth_funnel_variant(
        "C18acc-breadth-pool-mono-hot",
        "進場日 pool · hot=mono_tier2 else fresh（+0.53pp 對照）",
        breadth_pool_mode="mono_in_hot_zones",
        breadth_challenger_pool_mode="entry_day",
    ),
]


def _breadth_zone_ok(zone: str, allowed: list[str] | None) -> bool:
    if not allowed:
        return True
    return zone in allowed


def breadth_zone_on_date(zone_by_date: dict[str, str], date: str) -> str:
    """PIT zone lookup · 換倉日 as_of 或進場 signal_date。"""
    return zone_by_date.get(date, "unknown")


def _is_hot_breadth_zone(
    zone: str,
    hot_zones: tuple[str, ...] = DEFAULT_BREADTH_HOT_ZONES,
) -> bool:
    return zone in hot_zones


def _uses_swap_day_pool_routing(config: ScoreSwapCConfig) -> bool:
    return config.breadth_challenger_pool_mode == "swap_day" or config.breadth_pool_mode in (
        "swap_mono_in_hot_zones",
        "swap_union_accel_in_hot_zones",
    )


def _swap_gate_zone_date(
    as_of: str,
    sell_pos: dict[str, Any],
    config: ScoreSwapCConfig,
) -> str:
    if config.breadth_swap_zone_date == "entry_day":
        return str(sell_pos.get("signal_date") or sell_pos.get("entry_date") or as_of)
    return as_of


def _swap_allowed_for_leg(
    as_of: str,
    sell_pos: dict[str, Any],
    zone_by_date: dict[str, str],
    config: ScoreSwapCConfig,
) -> bool:
    if not config.breadth_swap_zones:
        return True
    ref = _swap_gate_zone_date(as_of, sell_pos, config)
    zone = breadth_zone_on_date(zone_by_date, ref)
    return _breadth_zone_ok(zone, config.breadth_swap_zones)


def _resolve_breadth_pool_types(
    as_of: str,
    zone_by_date: dict[str, str],
    config: ScoreSwapCConfig,
) -> tuple[CandidatePool, CandidatePool]:
    """breadth_pool_mode + challenger_pool_mode → entry/swap pool types。

    entry_day：進場與換倉 challenger 池皆依 as_of zone（原 funnel sweep）。
    swap_day：進場恒 fresh；換倉 challenger 池依 as_of（換倉日）zone。
    """
    zone = breadth_zone_on_date(zone_by_date, as_of)
    base_entry = config.entry_pool or config.candidate_pool
    base_swap = config.swap_pool or config.candidate_pool
    mode = config.breadth_pool_mode
    if mode == "always_fresh":
        return base_entry, base_swap
    hot = _is_hot_breadth_zone(zone)
    swap_day = _uses_swap_day_pool_routing(config)
    if mode == "mono_in_hot_zones":
        if swap_day:
            return "fresh", "mono_tier2" if hot else "fresh"
        pool: CandidatePool = "mono_tier2" if hot else "fresh"
        return pool, pool
    if mode == "swap_mono_in_hot_zones":
        return "fresh", "mono_tier2" if hot else "fresh"
    if mode == "swap_union_accel_in_hot_zones":
        return "fresh", "fresh_union_accel" if hot else "fresh"
    return base_entry, base_swap


def _seg_step_delta(segs: list[float]) -> float:
    """RRG 平面（ratio+mom）相鄰兩段位移差 · 負值 = 最後一步減數（減速）。"""
    if len(segs) < 2:
        return 0.0
    return round(float(segs[-1]) - float(segs[-2]), 4)


def _step_trend(dr: float, dm: float) -> str:
    """單日 (Δratio, Δmom) 方向 · 與 _feat trend 分類一致。"""
    if dr > 0 and dm > 0:
        return "up_right"
    if dr > 0:
        return "down_right"
    if dm > 0:
        return "up_left"
    return "down_left"


def _last_step_trend(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    trade_date: str,
    stock_id: str,
) -> str | None:
    """最後交易日 RRG 一步向量方向（加速度 down_left = 當日 Δratio≤0 且 Δmom≤0）。"""
    if trade_date not in full_dates:
        return None
    si = full_dates.index(trade_date)
    if si < 1 or stock_id not in rs_ratio.columns:
        return None
    d1 = full_dates[si]
    d0 = full_dates[si - 1]
    dr = float(rs_ratio.at[d1, stock_id]) - float(rs_ratio.at[d0, stock_id])
    dm = float(rs_mom.at[d1, stock_id]) - float(rs_mom.at[d0, stock_id])
    if dr != dr or dm != dm:
        return None
    return _step_trend(dr, dm)


def _daily_velocity(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    d1: str,
    d0: str,
    stock_id: str,
) -> tuple[float, float] | None:
    if stock_id not in rs_ratio.columns:
        return None
    dr = float(rs_ratio.at[d1, stock_id]) - float(rs_ratio.at[d0, stock_id])
    dm = float(rs_mom.at[d1, stock_id]) - float(rs_mom.at[d0, stock_id])
    if dr != dr or dm != dm:
        return None
    return dr, dm


def _avg_accel_trend_from_velocities(velocities: list[tuple[float, float]]) -> str | None:
    if len(velocities) < 2:
        return None
    accels = [
        (velocities[k][0] - velocities[k - 1][0], velocities[k][1] - velocities[k - 1][1])
        for k in range(1, len(velocities))
    ]
    avg_dr = sum(a[0] for a in accels) / len(accels)
    avg_dm = sum(a[1] for a in accels) / len(accels)
    return _step_trend(avg_dr, avg_dm)


def _avg_acceleration_trend(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    trade_date: str,
    stock_id: str,
    *,
    lb: int = LOOKBACK,
) -> str | None:
    """4 日回看 · v=日步向量 · a=Δv/Δt(=1d) · ā 分量平均後分象限。"""
    if trade_date not in full_dates:
        return None
    si = full_dates.index(trade_date)
    if si < lb - 1 or stock_id not in rs_ratio.columns:
        return None
    velocities: list[tuple[float, float]] = []
    for j in range(si - lb + 2, si + 1):
        d1 = full_dates[j]
        d0 = full_dates[j - 1]
        v = _daily_velocity(rs_ratio, rs_mom, full_dates, d1, d0, stock_id)
        if v is None:
            return None
        velocities.append(v)
    return _avg_accel_trend_from_velocities(velocities)


def _avg_accel_scalar(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    trade_date: str,
    stock_id: str,
    *,
    lb: int = LOOKBACK,
) -> float | None:
    """4 日窗 · 平均加速度分量之和 · 越负 = 转弱越强。"""
    if trade_date not in full_dates:
        return None
    si = full_dates.index(trade_date)
    if si < lb - 1 or stock_id not in rs_ratio.columns:
        return None
    velocities: list[tuple[float, float]] = []
    for j in range(si - lb + 2, si + 1):
        v = _daily_velocity(
            rs_ratio,
            rs_mom,
            full_dates,
            full_dates[j],
            full_dates[j - 1],
            stock_id,
        )
        if v is None:
            return None
        velocities.append(v)
    if len(velocities) < 2:
        return None
    accels = [
        (velocities[k][0] - velocities[k - 1][0], velocities[k][1] - velocities[k - 1][1])
        for k in range(1, len(velocities))
    ]
    avg_dr = sum(a[0] for a in accels) / len(accels)
    avg_dm = sum(a[1] for a in accels) / len(accels)
    return round(avg_dr + avg_dm, 6)


def _window_avg_accel_trend(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    si_start: int,
    si_end: int,
    stock_id: str,
) -> str | None:
    """區間 [si_start, si_end]  inclusive 端點 · 各自算 ā。"""
    if si_start < 1 or si_end <= si_start:
        return None
    velocities: list[tuple[float, float]] = []
    for j in range(si_start + 1, si_end + 1):
        v = _daily_velocity(
            rs_ratio,
            rs_mom,
            full_dates,
            full_dates[j],
            full_dates[j - 1],
            stock_id,
        )
        if v is None:
            return None
        velocities.append(v)
    return _avg_accel_trend_from_velocities(velocities)


def _split_entry_avg_accel_trends(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    entry_date: str,
    as_of: str,
    stock_id: str,
    pre_days: int,
    post_days: int,
    min_hold_days: int,
) -> tuple[str | None, str | None]:
    """買前 pre_days 與持後 post_days 各自平均加速度方向 · PIT：as_of≥entry+post。"""
    if entry_date not in full_dates or as_of not in full_dates:
        return None, None
    if _trading_days_between(full_dates, entry_date, as_of) < min_hold_days:
        return None, None
    si_e = full_dates.index(entry_date)
    si_asof = full_dates.index(as_of)
    if si_e + post_days > si_asof:
        return None, None
    si_pre_start = si_e - pre_days
    if si_pre_start < 1:
        return None, None
    pre = _window_avg_accel_trend(
        rs_ratio, rs_mom, full_dates, si_start=si_pre_start, si_end=si_e, stock_id=stock_id
    )
    post = _window_avg_accel_trend(
        rs_ratio, rs_mom, full_dates, si_start=si_e, si_end=si_e + post_days, stock_id=stock_id
    )
    return pre, post


def _split_accel_gate_trend(
    pre: str | None,
    post: str | None,
    *,
    mode: EntryAccelGateMode,
) -> str:
    if mode == "both_down_left":
        if pre == "down_left" and post == "down_left":
            return "down_left"
        return ""
    if post == "down_left":
        return "down_left"
    return ""


def _last_va_dot(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    trade_date: str,
    stock_id: str,
) -> float | None:
    """v·a at as_of · 負值 = 沿 RRG 軌跡減速。"""
    if trade_date not in full_dates:
        return None
    si = full_dates.index(trade_date)
    if si < 2 or stock_id not in rs_ratio.columns:
        return None
    d1 = full_dates[si]
    d0 = full_dates[si - 1]
    d_1 = full_dates[si - 2]
    v = _daily_velocity(rs_ratio, rs_mom, full_dates, d1, d0, stock_id)
    v_prev = _daily_velocity(rs_ratio, rs_mom, full_dates, d0, d_1, stock_id)
    if v is None or v_prev is None:
        return None
    a = (v[0] - v_prev[0], v[1] - v_prev[1])
    return v[0] * a[0] + v[1] * a[1]


def _challenger_accel_ok(
    trend: str | None,
    *,
    gate: ChallengerGate,
    va_dot: float | None = None,
) -> bool:
    if gate == "none":
        return True
    if gate == "recent_accel_up":
        return trend in ("up_right", "down_right")
    if gate == "v_dot_positive":
        return va_dot is not None and va_dot > 0
    return True


def candidate_shortlist_is_passthrough(config: ScoreSwapCConfig) -> bool:
    """C18acc 冠军：无 top-N / gate / 再排序 · 池子已在 fresh mono 建好。"""
    return (
        not config.candidate_require_positive_accel
        and config.challenger_gate == "none"
        and config.candidate_rank_key == "seg_last"
    )


def _candidate_shortlist(
    pool: list[ScanRow],
    config: ScoreSwapCConfig,
    *,
    challenger_trend: dict[str, str],
    challenger_va_dot: dict[str, float],
    challenger_avg_accel: dict[str, float],
) -> list[ScanRow]:
    """候选 shortlist · 研究 sweep 可加筛序；冠军 passthrough 用整池。"""
    if candidate_shortlist_is_passthrough(config):
        return list(pool)
    rows = list(pool)
    if config.candidate_require_positive_accel:
        rows = [
            r
            for r in rows
            if challenger_avg_accel.get(r.stock_id) is not None and challenger_avg_accel[r.stock_id] > 0
        ]
    if config.challenger_gate != "none":
        rows = [
            r
            for r in rows
            if _challenger_accel_ok(
                challenger_trend.get(r.stock_id),
                gate=config.challenger_gate,
                va_dot=challenger_va_dot.get(r.stock_id),
            )
        ]
    if config.candidate_rank_key == "avg_accel_decel":
        rows.sort(
            key=lambda r: (-(challenger_avg_accel.get(r.stock_id) or -999.0), r.stock_id)
        )
    else:
        rows.sort(key=lambda r: (-r.seg_last, r.stock_id))
    return rows[: max(1, int(config.candidate_top_n))]


def _entry_window_avg_accel_trend(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    entry_date: str,
    as_of: str,
    stock_id: str,
    pre_days: int = 4,
    post_days: int = 2,
    min_hold_days: int = 2,
) -> str | None:
    """進場前 pre_days → 持有後 post_days 固定窗 · ā=mean(Δv/Δt) · PIT：需 as_of≥entry+post。"""
    if entry_date not in full_dates or as_of not in full_dates:
        return None
    if _trading_days_between(full_dates, entry_date, as_of) < min_hold_days:
        return None
    si_e = full_dates.index(entry_date)
    si_asof = full_dates.index(as_of)
    si_end = si_e + post_days
    if si_end > si_asof:
        return None
    si_start = si_e - pre_days
    if si_start < 1:
        return None
    velocities: list[tuple[float, float]] = []
    for j in range(si_start + 1, si_end + 1):
        v = _daily_velocity(
            rs_ratio,
            rs_mom,
            full_dates,
            full_dates[j],
            full_dates[j - 1],
            stock_id,
        )
        if v is None:
            return None
        velocities.append(v)
    return _avg_accel_trend_from_velocities(velocities)


def _held_structural_trend(
    *,
    structural_gate: StructuralGate,
    config: ScoreSwapCConfig,
    feat: dict[str, Any],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
    stock_id: str,
    entry_date: str,
) -> str:
    if structural_gate == "step_down_left":
        return _last_step_trend(rs_ratio, rs_mom, full_dates, as_of, stock_id) or ""
    if structural_gate == "avg_accel_down_left":
        return _avg_acceleration_trend(rs_ratio, rs_mom, full_dates, as_of, stock_id) or ""
    if structural_gate == "entry_window_avg_accel_down_left":
        return (
            _entry_window_avg_accel_trend(
                rs_ratio,
                rs_mom,
                full_dates,
                entry_date=entry_date,
                as_of=as_of,
                stock_id=stock_id,
                pre_days=config.entry_accel_pre_days,
                post_days=config.entry_accel_post_days,
                min_hold_days=config.entry_accel_min_hold,
            )
            or ""
        )
    if structural_gate == "entry_split_avg_accel_down_left":
        pre, post = _split_entry_avg_accel_trends(
            rs_ratio,
            rs_mom,
            full_dates,
            entry_date=entry_date,
            as_of=as_of,
            stock_id=stock_id,
            pre_days=config.entry_accel_pre_days,
            post_days=config.entry_accel_post_days,
            min_hold_days=config.entry_accel_min_hold,
        )
        return _split_accel_gate_trend(pre, post, mode=config.entry_accel_gate_mode)
    if structural_gate == "disp_accel_confirm":
        disp = str(feat.get("trend") or "")
        accel = _avg_acceleration_trend(rs_ratio, rs_mom, full_dates, as_of, stock_id) or ""
        if disp == "down_left" and accel == "down_left":
            return "down_left"
        return ""
    if structural_gate == "accel_lead_decel":
        accel = _avg_acceleration_trend(rs_ratio, rs_mom, full_dates, as_of, stock_id) or ""
        dot = _last_va_dot(rs_ratio, rs_mom, full_dates, as_of, stock_id)
        if accel == "down_left" and dot is not None and dot < 0:
            return "down_left"
        return ""
    if structural_gate == "down_left":
        return str(feat.get("trend") or "")
    return ""


def _structural_gate_active(gate: StructuralGate) -> bool:
    return gate in (
        "down_left",
        "step_down_left",
        "avg_accel_down_left",
        "entry_window_avg_accel_down_left",
        "entry_split_avg_accel_down_left",
        "disp_accel_confirm",
        "accel_lead_decel",
    )


def _position_score(
    pos: dict[str, Any],
    sort_key: SortKey,
    *,
    held_today: dict[str, float] | None = None,
) -> float:
    if sort_key == "seg_step_delta":
        sid = str(pos.get("stock_id") or "")
        if held_today is not None and sid in held_today:
            return float(held_today[sid])
        return float(pos.get("seg_step_delta") or 0.0)
    if sort_key == "rs_momentum":
        return float(pos.get("rs_momentum") or 0.0)
    if sort_key in ("accel_decel", "avg_accel_decel"):
        sid = str(pos.get("stock_id") or "")
        if held_today is not None and sid in held_today:
            return float(held_today[sid])
        return 0.0
    return float(pos.get("seg_last") or 0.0)


def _buy_threshold_score(pos: dict[str, Any], sort_key: SortKey) -> float:
    """買方門檻一律用進場 seg_last（accel 只決定賣誰）。"""
    return float(pos.get("seg_last") or 0.0)


def _row_score(row: ScanRow, sort_key: SortKey) -> float:
    if sort_key == "rs_momentum":
        return float(row.rs_momentum)
    if sort_key == "seg_step_delta":
        return _seg_step_delta(row.segs)
    return float(row.seg_last)


def _buy_row_score(
    row: ScanRow,
    buy_sort_key: BuySortKey,
    *,
    challenger_va_dot: dict[str, float] | None = None,
    challenger_avg_accel: dict[str, float] | None = None,
) -> float | None:
    if buy_sort_key == "rs_momentum":
        return float(row.rs_momentum)
    if buy_sort_key == "seg_last":
        return float(row.seg_last)
    if buy_sort_key == "accel_decel":
        dot = (challenger_va_dot or {}).get(row.stock_id)
        return float(dot) if dot is not None else None
    scalar = (challenger_avg_accel or {}).get(row.stock_id)
    return float(scalar) if scalar is not None else None


def _trading_days_between(full_dates: list[str], start: str, end: str) -> int:
    if start > end:
        return 0
    return sum(1 for d in full_dates if start < d <= end)


def _swap_px(
    conn: sqlite3.Connection,
    *,
    close: pd.DataFrame,
    stock_id: str,
    trade_date: str,
    config: ScoreSwapCConfig,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]],
) -> tuple[float | None, str | None]:
    if trade_date not in close.index or stock_id not in close.columns:
        return None, None
    close_px = float(close.at[trade_date, stock_id])
    if config.timing_mode == "close":
        return close_px, None
    from research.backtest.rrg_lens_score_swap import _rebalance_minutes

    key = (stock_id, trade_date)
    if key not in kbar_cache:
        kbar_cache[key] = load_kbar_day_closes(conn, stock_id, trade_date)
    for minute in _rebalance_minutes(
        interval_min=config.poll_interval_min,
        no_swap_before=config.no_trade_before,
    ):
        px = price_at_or_before_minute(kbar_cache[key], minute)
        if px is not None and px > 0:
            return float(px), minute
    return close_px, None


def _settle_leg(
    conn: sqlite3.Connection,
    *,
    pos: dict[str, Any],
    exit_date: str,
    exit_px: float,
    exit_reason: str,
    config: ScoreSwapCConfig,
    full_dates: list[str],
) -> dict[str, Any] | None:
    sid = str(pos["stock_id"])
    entry = str(pos["entry_date"])
    entry_px = float(pos["entry_px"])
    if entry_px <= 0 or exit_px <= 0:
        return None
    ret = (exit_px / entry_px - 1.0) * 100.0
    bench = bench_return_entry_to_exit(conn, entry, exit_date, entry_price_mode="close")
    if bench is None:
        return None
    return {
        "stock_id": sid,
        "stock_name": pos.get("stock_name", ""),
        "signal_date": str(pos.get("signal_date") or entry),
        "entry_date": entry,
        "exit_date": exit_date,
        "entry_px": round(entry_px, 4),
        "exit_px": round(exit_px, 4),
        "exit_reason": exit_reason,
        "hold_days": _trading_days_between(full_dates, entry, exit_date),
        "variant_id": config.variant_id,
        "entry_leg": config.entry_leg,
        "return_pct": round(ret, 4),
        "bench_return_pct": round(bench, 4),
        "excess_pct": round(ret - bench, 4),
        "beat_bench": ret > bench,
        "gross_win": ret > 0,
        "seg_last": pos.get("seg_last"),
        "slot": pos.get("slot"),
    }


def _fresh_union_accel_pool(
    fresh_mono: list[ScanRow],
    mono_rows: list[ScanRow],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
    *,
    lb: int = LOOKBACK,
) -> list[ScanRow]:
    """fresh ∪ (mono_tier2 ∧ 四日平均加速>0) · 依 seg_last 排序。"""
    by_id: dict[str, ScanRow] = {r.stock_id: r for r in fresh_mono}
    for row in mono_rows:
        if row.stock_id in by_id:
            continue
        scalar = _avg_accel_scalar(rs_ratio, rs_mom, full_dates, as_of, row.stock_id, lb=lb)
        if scalar is not None and scalar > 0:
            by_id[row.stock_id] = row
    return sorted(by_id.values(), key=lambda r: (-r.seg_last, r.stock_id))


def _candidate_pool(
    as_of: str,
    *,
    fresh_mono: list[ScanRow],
    mono_by_date: dict[str, list[ScanRow]] | None,
    mono_up_by_date: dict[str, list[ScanRow]] | None,
    mono_up_fresh_by_date: dict[str, list[ScanRow]] | None,
    config: ScoreSwapCConfig,
    pool_type: CandidatePool | None = None,
    rs_ratio: pd.DataFrame | None = None,
    rs_mom: pd.DataFrame | None = None,
    full_dates: list[str] | None = None,
) -> list[ScanRow]:
    pool = pool_type or config.candidate_pool
    if pool == "fresh_union_accel":
        mono_rows = (mono_by_date or {}).get(as_of, [])
        if rs_ratio is not None and rs_mom is not None and full_dates is not None:
            return _fresh_union_accel_pool(
                fresh_mono,
                mono_rows,
                rs_ratio,
                rs_mom,
                full_dates,
                as_of,
                lb=config.accel_lookback,
            )
        return list(fresh_mono)
    if pool == "mono_tier2" and mono_by_date is not None:
        return mono_by_date.get(as_of, [])
    if pool == "mono_up" and mono_up_by_date is not None:
        return mono_up_by_date.get(as_of, [])
    if pool == "mono_up_fresh" and mono_up_fresh_by_date is not None:
        return mono_up_fresh_by_date.get(as_of, [])
    return fresh_mono


def _pick_swap_pair(
    slots: list[dict[str, Any]],
    candidates: list[ScanRow],
    *,
    held_ids: set[str],
    config: ScoreSwapCConfig,
    held_today: dict[str, float] | None = None,
    held_trend: dict[str, str] | None = None,
    challenger_trend: dict[str, str] | None = None,
    challenger_va_dot: dict[str, float] | None = None,
    challenger_avg_accel: dict[str, float] | None = None,
) -> tuple[dict[str, Any] | None, ScanRow | None]:
    """回傳 (sell_pos, buy_row) · challenger 須 score > held + margin。"""
    eligible = [r for r in candidates if r.stock_id not in held_ids]
    if not eligible or not slots:
        return None, None

    margin = config.effective_margin
    key = config.sort_key
    today = held_today or {}
    trends = held_trend or {}
    chall = challenger_trend or {}
    va_dots = challenger_va_dot or {}
    avg_accels = challenger_avg_accel or {}

    def held_score(pos: dict[str, Any]) -> float:
        return _position_score(pos, key, held_today=today)

    def row_score(row: ScanRow) -> float:
        return _row_score(row, key)

    def challenger_ok(row: ScanRow) -> bool:
        return _challenger_accel_ok(
            chall.get(row.stock_id),
            gate=config.challenger_gate,
            va_dot=va_dots.get(row.stock_id),
        )

    sell_pool = list(slots)
    if config.decel_gate:
        sell_pool = [p for p in sell_pool if today.get(str(p["stock_id"]), 0.0) < 0]
    if config.accel_sell_negative_only and key in ("accel_decel", "avg_accel_decel"):
        sell_pool = [p for p in sell_pool if held_score(p) < 0]
    if _structural_gate_active(config.structural_gate):
        sell_pool = [p for p in sell_pool if trends.get(str(p["stock_id"])) == "down_left"]
    if not sell_pool:
        return None, None

    eligible = [r for r in eligible if challenger_ok(r)]
    if not eligible:
        return None, None

    use_seg_buy = key in ("accel_decel", "avg_accel_decel")
    buy_key = config.buy_sort_key

    def pick_buy(beats: list[ScanRow]) -> ScanRow | None:
        if not beats:
            return None
        if buy_key is not None:
            scored = [
                (r, s)
                for r in beats
                if (s := _buy_row_score(
                    r,
                    buy_key,
                    challenger_va_dot=va_dots,
                    challenger_avg_accel=avg_accels,
                ))
                is not None
            ]
            return max(scored, key=lambda x: x[1])[0] if scored else None
        return max(beats, key=row_score)

    if config.swap_target == "worst_held":
        sell = min(sell_pool, key=held_score)
        if buy_key is not None or use_seg_buy:
            threshold = _buy_threshold_score(sell, key) + margin
            beats = [r for r in eligible if float(r.seg_last) > threshold]
        else:
            threshold = held_score(sell) + margin
            beats = [r for r in eligible if row_score(r) > threshold]
        buy = pick_buy(beats)
        return (sell, buy) if buy else (None, None)

    for sell in sorted(sell_pool, key=held_score):
        if buy_key is not None or use_seg_buy:
            threshold = _buy_threshold_score(sell, key) + margin
            beats = [r for r in eligible if float(r.seg_last) > threshold]
        else:
            threshold = held_score(sell) + margin
            beats = [r for r in eligible if row_score(r) > threshold]
        buy = pick_buy(beats)
        if buy:
            return sell, buy
    return None, None


def simulate_score_swap_c(
    conn: sqlite3.Connection,
    *,
    trade_dates: list[str],
    full_dates: list[str],
    close: pd.DataFrame,
    bench: pd.Series,
    fresh_by_date: dict[str, list[ScanRow]],
    zone_by_date: dict[str, str],
    config: ScoreSwapCConfig,
    mono_by_date: dict[str, list[ScanRow]] | None = None,
    mono_up_by_date: dict[str, list[ScanRow]] | None = None,
    mono_up_fresh_by_date: dict[str, list[ScanRow]] | None = None,
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] | None = None,
    rs_mom: pd.DataFrame | None = None,
    rs_ratio: pd.DataFrame | None = None,
    zone_filter: str | None = None,
    entry_c_config: Any | None = None,
    slot_snapshots: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = kbar_cache if kbar_cache is not None else {}
    kbar_stats = {"hits": 0, "checks": 0}
    slots: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    swaps = 0
    max_hold_exits = 0

    # 模式 C 填倉沿用 B 的 entry helper（需 structural gate 無）
    from research.backtest.rrg_mono_swap_exit_b import SwapExitBConfig, _daily_feat

    fill_cfg = SwapExitBConfig(
        variant_id=config.variant_id,
        label=config.label,
        entry_leg=config.entry_leg,
    )

    for as_of in trade_dates:
        fresh_mono = fresh_by_date.get(as_of, [])
        pool_kwargs = dict(
            fresh_mono=fresh_mono,
            mono_by_date=mono_by_date,
            mono_up_by_date=mono_up_by_date,
            mono_up_fresh_by_date=mono_up_fresh_by_date,
            config=config,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            full_dates=full_dates,
        )
        entry_pool_type, swap_pool_type = _resolve_breadth_pool_types(as_of, zone_by_date, config)
        entry_pool_rows = _candidate_pool(as_of, pool_type=entry_pool_type, **pool_kwargs)
        swap_pool_rows = _candidate_pool(as_of, pool_type=swap_pool_type, **pool_kwargs)
        signal_zone = zone_by_date.get(as_of, "unknown")
        held_today: dict[str, float] = {}
        held_trend: dict[str, str] = {}
        challenger_trend: dict[str, str] = {}
        challenger_va_dot: dict[str, float] = {}
        challenger_avg_accel: dict[str, float] = {}
        need_feat = (
            config.decel_gate
            or config.structural_gate != "none"
            or config.sort_key in ("seg_step_delta", "accel_decel", "avg_accel_decel")
        )
        if need_feat and rs_ratio is not None and rs_mom is not None:
            for pos in slots:
                sid = str(pos["stock_id"])
                entry_date = str(pos["entry_date"])
                skip_feat = False
                if config.sort_key == "accel_decel":
                    dot = _last_va_dot(rs_ratio, rs_mom, full_dates, as_of, sid)
                    if dot is not None:
                        held_today[sid] = float(dot)
                    skip_feat = config.structural_gate == "none"
                elif config.sort_key == "avg_accel_decel":
                    scalar = _avg_accel_scalar(
                        rs_ratio, rs_mom, full_dates, as_of, sid, lb=config.accel_lookback
                    )
                    if scalar is not None:
                        held_today[sid] = float(scalar)
                    skip_feat = config.structural_gate == "none"
                if skip_feat:
                    continue
                if config.structural_gate in (
                    "entry_window_avg_accel_down_left",
                    "entry_split_avg_accel_down_left",
                ):
                    held_trend[sid] = _held_structural_trend(
                        structural_gate=config.structural_gate,
                        config=config,
                        feat={},
                        rs_ratio=rs_ratio,
                        rs_mom=rs_mom,
                        full_dates=full_dates,
                        as_of=as_of,
                        stock_id=sid,
                        entry_date=entry_date,
                    )
                    continue
                feat = _daily_feat(rs_ratio, rs_mom, full_dates, as_of, sid)
                if not feat:
                    continue
                held_today[sid] = _seg_step_delta(feat["segs"])
                held_trend[sid] = _held_structural_trend(
                    structural_gate=config.structural_gate,
                    config=config,
                    feat=feat,
                    rs_ratio=rs_ratio,
                    rs_mom=rs_mom,
                    full_dates=full_dates,
                    as_of=as_of,
                    stock_id=sid,
                    entry_date=entry_date,
                )
        need_challenger_kin = (
            config.candidate_rank_key == "avg_accel_decel"
            or config.candidate_require_positive_accel
            or config.challenger_gate != "none"
            or config.buy_sort_key in ("accel_decel", "avg_accel_decel")
        )
        if need_challenger_kin and rs_ratio is not None and rs_mom is not None:
            kin_rows = swap_pool_rows if swap_pool_rows else []
            for row in kin_rows:
                sid = row.stock_id
                if config.challenger_gate == "recent_accel_up":
                    t = _avg_acceleration_trend(
                        rs_ratio, rs_mom, full_dates, as_of, sid, lb=config.accel_lookback
                    )
                    if t:
                        challenger_trend[sid] = t
                if config.challenger_gate == "v_dot_positive" or config.buy_sort_key == "accel_decel":
                    dot = _last_va_dot(rs_ratio, rs_mom, full_dates, as_of, sid)
                    if dot is not None:
                        challenger_va_dot[sid] = float(dot)
                if (
                    config.candidate_rank_key == "avg_accel_decel"
                    or config.candidate_require_positive_accel
                    or config.buy_sort_key == "avg_accel_decel"
                ):
                    scalar = _avg_accel_scalar(
                        rs_ratio, rs_mom, full_dates, as_of, sid, lb=config.accel_lookback
                    )
                    if scalar is not None:
                        challenger_avg_accel[sid] = float(scalar)
        swap_shortlist = _candidate_shortlist(
            swap_pool_rows,
            config,
            challenger_trend=challenger_trend,
            challenger_va_dot=challenger_va_dot,
            challenger_avg_accel=challenger_avg_accel,
        )
        entry_shortlist = _candidate_shortlist(
            entry_pool_rows,
            config,
            challenger_trend=challenger_trend,
            challenger_va_dot=challenger_va_dot,
            challenger_avg_accel=challenger_avg_accel,
        )
        swaps_today = 0

        for pos in list(slots):
            hold_days = _trading_days_between(full_dates, str(pos["entry_date"]), as_of)
            if hold_days < config.max_hold_days:
                continue
            px, minute = _swap_px(conn, close=close, stock_id=str(pos["stock_id"]), trade_date=as_of, config=config, kbar_cache=cache)
            if px is None:
                continue
            if minute:
                pos["exit_minute"] = minute
            leg = _settle_leg(conn, pos=pos, exit_date=as_of, exit_px=px, exit_reason="max_hold", config=config, full_dates=full_dates)
            if leg:
                leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                periods.append(leg)
                slots.remove(pos)
                max_hold_exits += 1

        zone_ok = zone_filter is None or zone_by_date.get(as_of) == zone_filter
        if not zone_ok:
            continue

        swap_allowed = _breadth_zone_ok(signal_zone, config.breadth_swap_zones)
        while swaps_today < config.max_swaps_per_day:
            held = {str(p["stock_id"]) for p in slots}
            if len(slots) < MAX_SLOTS:
                break
            sell, buy = _pick_swap_pair(
                slots,
                swap_shortlist,
                held_ids=held,
                config=config,
                held_today=held_today,
                held_trend=held_trend,
                challenger_trend=challenger_trend,
                challenger_va_dot=challenger_va_dot,
                challenger_avg_accel=challenger_avg_accel,
            )
            if sell is None or buy is None:
                break
            if not _swap_allowed_for_leg(as_of, sell, zone_by_date, config):
                break
            hold_days = _trading_days_between(full_dates, str(sell["entry_date"]), as_of)
            if hold_days < config.min_hold_days:
                break

            sell_px, _ = _swap_px(conn, close=close, stock_id=str(sell["stock_id"]), trade_date=as_of, config=config, kbar_cache=cache)
            buy_px, buy_min = _swap_px(conn, close=close, stock_id=buy.stock_id, trade_date=as_of, config=config, kbar_cache=cache)
            if sell_px is None or buy_px is None:
                break

            leg = _settle_leg(conn, pos=sell, exit_date=as_of, exit_px=sell_px, exit_reason="score_swap", config=config, full_dates=full_dates)
            if leg is None:
                break
            leg["breadth_zone_200"] = zone_by_date.get(str(sell.get("signal_date")), "unknown")
            leg["challenger_id"] = buy.stock_id
            leg["challenger_seg_last"] = buy.seg_last
            periods.append(leg)
            slots.remove(sell)
            slot_id = sell.get("slot", len(slots))
            slots.append(
                {
                    "slot": slot_id,
                    "stock_id": buy.stock_id,
                    "stock_name": buy.stock_name,
                    "signal_date": as_of,
                    "entry_date": as_of,
                    "entry_px": float(buy_px),
                    "seg_last": round(buy.seg_last, 4),
                    "disp": round(buy.disp, 4),
                    "rs_momentum": float(buy.rs_momentum),
                    "seg_step_delta": _seg_step_delta(buy.segs),
                    "entry_minute": buy_min,
                    "entry_leg": config.entry_leg,
                }
            )
            swaps += 1
            swaps_today += 1

        if _breadth_zone_ok(signal_zone, config.breadth_entry_zones):
            fill_shortlist = entry_shortlist
            if not fill_shortlist and config.entry_fallback_pool is not None:
                fallback_rows = _candidate_pool(
                    as_of,
                    pool_type=config.entry_fallback_pool,
                    **pool_kwargs,
                )
                fill_shortlist = _candidate_shortlist(
                    fallback_rows,
                    config,
                    challenger_trend=challenger_trend,
                    challenger_va_dot=challenger_va_dot,
                    challenger_avg_accel=challenger_avg_accel,
                )
            _fill_empty_slots(
                conn,
                as_of=as_of,
                fresh_mono=fill_shortlist,
                slots=slots,
                close=close,
                bench=bench,
                full_dates=full_dates,
                config=fill_cfg,
                kbar_cache=cache,
                kbar_stats=kbar_stats,
                entry_c_config=entry_c_config,
            )
        for pos in slots:
            if pos.get("rs_momentum") is not None and pos.get("seg_step_delta") is not None:
                continue
            row = next(
                (r for r in entry_pool_rows + swap_pool_rows if r.stock_id == str(pos["stock_id"])),
                None,
            )
            if row is None:
                continue
            pos.setdefault("seg_last", round(row.seg_last, 4))
            pos.setdefault("rs_momentum", float(row.rs_momentum))
            pos.setdefault("seg_step_delta", _seg_step_delta(row.segs))
        if slot_snapshots is not None:
            slot_snapshots[as_of] = [dict(p) for p in slots]

    if trade_dates:
        last = trade_dates[-1]
        for pos in list(slots):
            px = _entry_px(close, str(pos["stock_id"]), last)
            if px is None:
                continue
            leg = _settle_leg(conn, pos=pos, exit_date=last, exit_px=px, exit_reason="window_end", config=config, full_dates=full_dates)
            if leg:
                leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                periods.append(leg)

    summary = summarize_periods(periods)
    n = len(periods)
    if n:
        summary["mean_excess_pct"] = round(sum(p["excess_pct"] for p in periods) / n, 4)
        summary["mean_hold_days"] = round(sum(p["hold_days"] for p in periods) / n, 2)
    else:
        summary["mean_excess_pct"] = None
        summary["mean_hold_days"] = None
    summary.update(
        {
            "variant_id": config.variant_id,
            "label": config.label,
            "entry_leg": config.entry_leg,
            "candidate_pool": config.candidate_pool,
            "entry_pool": config.entry_pool,
            "swap_pool": config.swap_pool,
            "entry_fallback_pool": config.entry_fallback_pool,
            "candidate_top_n": config.candidate_top_n,
            "candidate_rank_key": config.candidate_rank_key,
            "candidate_require_positive_accel": config.candidate_require_positive_accel,
            "sort_key": config.sort_key,
            "decel_gate": config.decel_gate,
            "structural_gate": config.structural_gate,
            "challenger_gate": config.challenger_gate,
            "buy_sort_key": config.buy_sort_key,
            "accel_lookback": config.accel_lookback,
            "candidate_lookback": config.candidate_lookback,
            "min_hold_days": config.min_hold_days,
            "accel_sell_negative_only": config.accel_sell_negative_only,
            "seg_margin": config.seg_margin,
            "score_margin": config.score_margin,
            "effective_margin": config.effective_margin,
            "max_swaps_per_day": config.max_swaps_per_day,
            "breadth_entry_zones": config.breadth_entry_zones,
            "breadth_swap_zones": config.breadth_swap_zones,
            "breadth_pool_mode": config.breadth_pool_mode,
            "breadth_challenger_pool_mode": config.breadth_challenger_pool_mode,
            "breadth_swap_zone_date": config.breadth_swap_zone_date,
            "swaps_total": swaps,
            "max_hold_exits": max_hold_exits,
            "n_periods": n,
        }
    )
    return periods, summary


def _pooled_by_entry_zone(periods: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    from market_breadth_ma import BREADTH_ZONES_ORDER

    buckets: dict[str, list[dict[str, Any]]] = {z: [] for z in BREADTH_ZONES_ORDER}
    for p in periods:
        z = p.get("breadth_zone_200")
        if z in buckets:
            buckets[z].append(p)
    out: dict[str, dict[str, Any]] = {}
    for zone, sub in buckets.items():
        if sub:
            s = summarize_periods(sub)
            n = len(sub)
            s["n_periods"] = n
            s["mean_excess_pct"] = round(sum(p["excess_pct"] for p in sub) / n, 4)
            out[zone] = s
        else:
            out[zone] = {"n_periods": 0, "mean_excess_pct": None}
    return out


def run_swap_accel_breadth_zone_comparison(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-22",
    config: ScoreSwapCConfig | None = None,
) -> dict[str, Any]:
    """rrg-mono-swap-accel（C18acc）× Market breadth（廣度）zone · graduation hold-out。"""
    from market_breadth_ma import BREADTH_ZONE_ZH, BREADTH_ZONES_ORDER, build_breadth_panel
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar, simulate_mono_hold7

    cfg = config or champion_score_swap_c_config()
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}

    results: dict[str, Any] = {
        "slug": RRG_MONO_SWAP_ACCEL_SLUG,
        "short_name": RRG_MONO_SWAP_ACCEL_SHORT,
        "variant_id": cfg.variant_id,
        "date_start": date_start,
        "date_end": date_end,
        "by_zone": {},
        "pooled_by_entry_zone": {},
        "references": {},
    }

    for zone in BREADTH_ZONES_ORDER:
        periods, summary = simulate_score_swap_c(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            fresh_by_date=fresh_by_date,
            zone_by_date=zone_by_date,
            config=cfg,
            kbar_cache=kbar_cache,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
            zone_filter=zone,
        )
        results["by_zone"][zone] = {
            "summary": summary,
            "zh": BREADTH_ZONE_ZH[zone],
            "n_periods": summary.get("n_periods", len(periods)),
        }

    champ_periods, champ_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        kbar_cache=kbar_cache,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
    )
    results["pooled_all"] = champ_summary
    results["pooled_by_entry_zone"] = _pooled_by_entry_zone(champ_periods)

    hold7_periods, hold7_summary = simulate_mono_hold7(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date,
    )
    hold7_pooled = _pooled_by_entry_zone(hold7_periods)
    results["references"]["rrg_mono_hold7"] = {
        "pooled_all": hold7_summary,
        "pooled_by_entry_zone": hold7_pooled,
    }

    pooled = results["pooled_by_entry_zone"]
    strong_ex = pooled.get("strong", {}).get("mean_excess_pct")
    ob_ex = pooled.get("overbought", {}).get("mean_excess_pct")
    gate_strong_ob = (
        strong_ex is not None
        and ob_ex is not None
        and float(strong_ex) > 0
        and float(ob_ex) > 0
    )
    thin_buckets = [
        f"{z}: n={pooled[z].get('n_periods', 0)}"
        for z in BREADTH_ZONES_ORDER
        if 0 < (pooled.get(z, {}).get("n_periods") or 0) < 15
    ]
    results["graduation_gate"] = {
        "strong_overbought_positive": gate_strong_ob,
        "passed": gate_strong_ob,
        "thin_buckets": thin_buckets,
        "note": "採納門檻：訊號日 zone_200 · 強勢+過熱均超額>0（全樣本進場後分桶）",
    }
    return results


def render_swap_accel_breadth_markdown(results: dict[str, Any]) -> str:
    from market_breadth_ma import BREADTH_ZONE_ZH, BREADTH_ZONES_ORDER

    ds, de = results["date_start"], results["date_end"]
    slug = results.get("slug", RRG_MONO_SWAP_ACCEL_SLUG)
    short = results.get("short_name", RRG_MONO_SWAP_ACCEL_SHORT)
    gate = results.get("graduation_gate") or {}
    lines = [
        f"# {slug}（{short}）× 200MA 廣度區間 · {ds}～{de}",
        "",
        "策略：**RRG mono fresh · 四日加速对称换仓 · C0 盘中进 · 5m poll · 3 槽 · hold 5–10**",
        "",
        "方法：",
        "- **區間獨立**：僅在該日 `zone_200` 符合時允許新進倉／換倉；`max_hold` 出場照常。",
        "- **全樣本分桶**：全程模擬後按**訊號日** `zone_200` 分組（graduation hold-out 主判據）。",
        "",
        f"**Graduation gate（強勢+過熱均超額>0）**：{'通过' if gate.get('passed') else '未通过'}",
        "",
        "## 區間獨立回測",
        "",
        "| 200MA 區間 | n | 均超額% | swaps |",
        "|-----------|---|---------|-------|",
    ]
    for zone in BREADTH_ZONES_ORDER:
        s = results["by_zone"][zone]["summary"]
        lines.append(
            f"| {BREADTH_ZONE_ZH[zone]} | {s.get('n_periods', 0)} | "
            f"{s.get('mean_excess_pct', '—')} | {s.get('swaps_total', '—')} |"
        )

    lines.extend(
        [
            "",
            "## 全樣本進場 · 依訊號日分桶（hold-out 主表）",
            "",
            "| 200MA 區間 | n | C18acc 均超額% | hold7 均超額% | Δ pp |",
            "|-----------|---|----------------|---------------|------|",
        ]
    )
    pooled = results["pooled_by_entry_zone"]
    ref = (results.get("references") or {}).get("rrg_mono_hold7", {}).get("pooled_by_entry_zone", {})
    for zone in BREADTH_ZONES_ORDER:
        a = pooled.get(zone, {})
        h = ref.get(zone, {})
        a_ex, h_ex = a.get("mean_excess_pct"), h.get("mean_excess_pct")
        delta = round(float(a_ex) - float(h_ex), 4) if a_ex is not None and h_ex is not None else "—"
        lines.append(
            f"| {BREADTH_ZONE_ZH[zone]} | {a.get('n_periods', 0)} | "
            f"{a_ex if a_ex is not None else '—'} | {h_ex if h_ex is not None else '—'} | {delta} |"
        )

    pa = results.get("pooled_all") or {}
    lines.extend(
        [
            "",
            f"全樣本 C18acc：n={pa.get('n_periods')} · 均超額 {pa.get('mean_excess_pct')}% · swaps={pa.get('swaps_total')}",
            "",
        ]
    )
    if gate.get("thin_buckets"):
        lines.append(f"小樣本桶（n<15）：{', '.join(gate['thin_buckets'])}")
        lines.append("")
    lines.extend(
        [
            "---",
            "模組：`rrg_mono_score_swap_c.run_swap_accel_breadth_zone_comparison`",
        ]
    )
    return "\n".join(lines) + "\n"


def run_swap_accel_candidate_pool_comparison(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-22",
    configs: list[ScoreSwapCConfig] | None = None,
) -> dict[str, Any]:
    """C18acc · 候选池对照：fresh leading vs mono_up（无 leading）· 四日加速换仓规则不变。"""
    from research.backtest.rrg_mono_backtest import (
        build_fresh_mono_calendar,
        build_mono_up_calendar,
        build_mono_up_fresh_calendar,
    )

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    mono_up_by_date = build_mono_up_calendar(conn, trade_dates, require_disp=True)
    mono_up_fresh_by_date = build_mono_up_fresh_calendar(conn, trade_dates)
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}

    pool_sizes = {
        "fresh_mean": round(
            sum(len(fresh_by_date.get(d, [])) for d in trade_dates) / len(trade_dates), 2
        ),
        "mono_tier2_mean": round(
            sum(len(mono_by_date.get(d, [])) for d in trade_dates) / len(trade_dates), 2
        ),
        "mono_up_mean": round(
            sum(len(mono_up_by_date.get(d, [])) for d in trade_dates) / len(trade_dates), 2
        ),
        "mono_up_fresh_mean": round(
            sum(len(mono_up_fresh_by_date.get(d, [])) for d in trade_dates) / len(trade_dates),
            2,
        ),
        "n_trade_days": len(trade_dates),
    }

    grid = configs or C18acc_CANDIDATE_POOL_SWEEP
    by_variant: dict[str, dict[str, Any]] = {}
    summaries: list[dict[str, Any]] = []

    for cfg in grid:
        print(f"pool sweep {cfg.variant_id} · {cfg.label} ...", flush=True)
        periods, summary = simulate_score_swap_c(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            fresh_by_date=fresh_by_date,
            zone_by_date={},
            config=cfg,
            mono_by_date=mono_by_date,
            mono_up_by_date=mono_up_by_date,
            mono_up_fresh_by_date=mono_up_fresh_by_date,
            kbar_cache=kbar_cache,
            rs_ratio=rs_ratio,
            rs_mom=rs_mom,
        )
        entry = {"summary": summary, "n_periods": len(periods)}
        by_variant[cfg.variant_id] = entry
        summaries.append(summary)
        print(
            f"  done {cfg.variant_id}: n={summary.get('n_periods')} "
            f"swaps={summary.get('swaps_total')} mean_excess={summary.get('mean_excess_pct')}",
            flush=True,
        )

    champ = by_variant.get(CHAMPION_SCORE_SWAP_C_VARIANT_ID, {}).get("summary") or {}
    ranked = sorted(
        summaries,
        key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)),
    )
    deltas: dict[str, float | None] = {}
    for s in summaries:
        vid = str(s.get("variant_id"))
        if vid == CHAMPION_SCORE_SWAP_C_VARIANT_ID:
            continue
        ce = champ.get("mean_excess_pct")
        se = s.get("mean_excess_pct")
        deltas[vid] = round(float(se) - float(ce), 4) if ce is not None and se is not None else None

    return {
        "slug": RRG_MONO_SWAP_ACCEL_SLUG,
        "short_name": RRG_MONO_SWAP_ACCEL_SHORT,
        "date_start": date_start,
        "date_end": date_end,
        "pool_sizes": pool_sizes,
        "gate_definitions": {
            "fresh_mono": "up_right + mono_up + disp∈[1,2) + leading + 今日新进 mono tier2",
            "mono_up": "up_right + mono_up + disp∈[1,2) · 不要求 leading",
            "mono_up_fresh": "mono_up 条件 + 昨日未过 · 不要求 leading",
            "mono_tier2": "up_right + mono_up + disp∈[1,2) + leading · 含非 fresh",
        },
        "by_variant": by_variant,
        "delta_vs_champion_pp": deltas,
        "best": ranked[0] if ranked else None,
        "champion": champ,
    }


def render_swap_accel_candidate_pool_markdown(results: dict[str, Any]) -> str:
    ds, de = results["date_start"], results["date_end"]
    ps = results["pool_sizes"]
    lines = [
        f"# {results['short_name']} · 候选池对照（无 leading）· {ds}～{de}",
        "",
        "换仓规则固定：**四日加速对称换仓** · C0 盘中进 · 5m poll · min_hold=5 · max_hold=10 · margin=0.05",
        "",
        "## 候选池定义",
        "",
        "| 池 | 条件 |",
        "|----|------|",
        "| **fresh mono**（对照） | up_right + mono_up + disp∈[1,2) + **leading** + 今日新进 |",
        "| **mono_up** | up_right + mono_up + disp∈[1,2) · **不要求 leading** |",
        "| **mono_up fresh** | mono_up 条件 + 昨日未过 · **不要求 leading** |",
        "| **mono tier2** | 同 fresh 三轴 + **leading** · 含非 fresh 全池 |",
        "",
        f"日均候选数：fresh **{ps['fresh_mean']}** · mono_up **{ps['mono_up_mean']}** · "
        f"mono_up fresh **{ps['mono_up_fresh_mean']}** · mono tier2 **{ps['mono_tier2_mean']}**",
        "",
        "## 回测结果",
        "",
        "| 变体 | 候选池 | 成交笔 | swaps | 胜台指% | 均超额 | Δ vs C18acc |",
        "|------|--------|--------|-------|---------|--------|-------------|",
    ]
    champ_ex = (results.get("champion") or {}).get("mean_excess_pct")
    ranked = sorted(
        results["by_variant"].items(),
        key=lambda x: x[1]["summary"].get("mean_excess_pct") or -9999,
        reverse=True,
    )
    pool_zh = {
        "fresh": "fresh · leading",
        "mono_up": "mono_up · 无 leading",
        "mono_up_fresh": "mono_up fresh",
        "mono_tier2": "mono tier2 · leading",
    }
    for vid, row in ranked:
        s = row["summary"]
        pool = pool_zh.get(str(s.get("candidate_pool")), str(s.get("candidate_pool")))
        delta = results.get("delta_vs_champion_pp", {}).get(vid)
        if vid == CHAMPION_SCORE_SWAP_C_VARIANT_ID:
            delta_s = "—"
        elif delta is not None:
            delta_s = f"{delta:+.2f}pp"
        else:
            delta_s = "—"
        lines.append(
            f"| {vid} | {pool} | {s.get('n_periods', 0)} | {s.get('swaps_total', 0)} | "
            f"{s.get('win_rate_vs_bench_pct', '—')} | {s.get('mean_excess_pct', '—')}% | {delta_s} |"
        )
    lines.extend(
        [
            "",
            f"冠军对照（C18acc · fresh leading）：均超额 **{champ_ex}%**",
            "",
            "---",
            "模組：`rrg_mono_score_swap_c.run_swap_accel_candidate_pool_comparison`",
        ]
    )
    return "\n".join(lines) + "\n"


def champion_score_swap_c_config() -> ScoreSwapCConfig:
    """Research champion · rrg-mono-swap-accel（C18acc）· live / backtest SSOT。"""
    for cfg in (
        C18_BUY_ACCEL_PHASE2_SWEEP
        + C18_ACC4_LB_SWEEP
        + C18_ACC4_EARLY_MARGIN_SWEEP
    ):
        if cfg.variant_id == CHAMPION_SCORE_SWAP_C_VARIANT_ID:
            return cfg
    return ScoreSwapCConfig(
        CHAMPION_SCORE_SWAP_C_VARIANT_ID,
        "四日加速 · 卖转弱 · 买转强 · margin=0.05",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        buy_sort_key="avg_accel_decel",
        accel_lookback=4,
    )


def run_score_swap_c_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    configs: list[ScoreSwapCConfig] | None = None,
) -> dict[str, Any]:
    from market_breadth_ma import build_breadth_panel
    from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP, simulate_leg_c_variant

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date_default = build_fresh_mono_calendar(conn, trade_dates)
    fresh_by_lookback: dict[int, dict[str, list]] = {LOOKBACK: fresh_by_date_default}

    def _fresh_for_config(cfg: ScoreSwapCConfig) -> dict[str, list]:
        lb = int(cfg.candidate_lookback)
        if lb not in fresh_by_lookback:
            fresh_by_lookback[lb] = build_fresh_mono_calendar(conn, trade_dates, lookback=lb)
        return fresh_by_lookback[lb]

    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    from research.backtest.rrg_mono_backtest import build_mono_up_calendar, build_mono_up_fresh_calendar

    mono_up_by_date = build_mono_up_calendar(conn, trade_dates, require_disp=True)
    mono_up_fresh_by_date = build_mono_up_fresh_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}

    from research.backtest.rrg_mono_backtest import simulate_mono_hold7

    _, a_hold7 = simulate_mono_hold7(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date_default,
    )
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    _, c0_hold7 = simulate_leg_c_variant(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        zone_by_date=zone_by_date,
        fresh_by_date=fresh_by_date_default,
        config=c0_cfg,
    )

    grid = configs or DEFAULT_SCORE_C_SWEEP
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    summaries: list[dict[str, Any]] = []

    for cfg in grid:
        print(f"sweep {cfg.variant_id} · {cfg.label} ...", flush=True)
        _, summary = simulate_score_swap_c(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            bench=bench,
            fresh_by_date=_fresh_for_config(cfg),
            zone_by_date=zone_by_date,
            config=cfg,
            mono_by_date=mono_by_date,
            mono_up_by_date=mono_up_by_date,
            mono_up_fresh_by_date=mono_up_fresh_by_date,
            kbar_cache=kbar_cache,
            rs_mom=rs_mom,
            rs_ratio=rs_ratio,
        )
        summary["delta_vs_a_hold7_pp"] = round(
            float(summary["mean_excess_pct"] or 0) - float(a_hold7.get("mean_excess_pct") or 0), 4
        )
        summary["delta_vs_c0_hold7_pp"] = round(
            float(summary["mean_excess_pct"] or 0) - float(c0_hold7.get("mean_excess_pct") or 0), 4
        )
        summaries.append(summary)
        print(
            f"  done {cfg.variant_id}: n={summary.get('n_periods')} "
            f"swaps={summary.get('swaps_total')} mean_excess={summary.get('mean_excess_pct')}",
            flush=True,
        )

    ranked = sorted(summaries, key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)))
    return {
        "date_start": date_start,
        "date_end": date_end,
        "reference_a_hold7": {"mean_excess_pct": a_hold7.get("mean_excess_pct"), "n_periods": a_hold7.get("n_periods")},
        "reference_c0_hold7": {"mean_excess_pct": c0_hold7.get("mean_excess_pct"), "n_periods": c0_hold7.get("n_periods")},
        "ssg_note": "純 seg_last 分數換倉 · 無左下／象限 gate · 賣最弱腿",
        "summaries": summaries,
        "best": ranked[0] if ranked else None,
    }
