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
from stock_db.kbar import KbarBar, load_kbar_day_bars, load_kbar_day_closes, price_at_or_before_minute

CandidatePool = Literal["fresh", "mono_tier2", "mono_up", "mono_up_fresh", "fresh_union_accel", "fresh_union_mono"]
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
SwapMarginKey = Literal["seg_last", "sort_key", "none", "mom_minus_ratio"]
EntryLeg = Literal["A", "C0"]
SwapTarget = Literal["worst_held", "each_held"]
TimingMode = Literal["close", "poll_5m", "snapshot_1300"]
ExitLoopMode = Literal[
    "day_first_poll",
    "intraday_loop",
    "intraday_loop_full",
    "intraday_loop_phase3",
]
SortKey = Literal["seg_last", "rs_momentum", "seg_step_delta", "accel_decel", "avg_accel_decel"]
BuySortKey = Literal["seg_last", "rs_momentum", "accel_decel", "avg_accel_decel"]
FillMode = Literal["legacy_sequential", "batch_topk"]
AccelMetric = Literal["dot", "avg"]
BreadthPoolMode = Literal[
    "always_fresh",
    "mono_in_hot_zones",
    "swap_mono_in_hot_zones",
    "swap_union_accel_in_hot_zones",
]
BreadthChallengerPoolMode = Literal["entry_day", "swap_day"]
BreadthSwapZoneDate = Literal["entry_day", "swap_day"]
QuadForceExitMode = Literal["none", "weakening", "lagging", "weakening_or_lagging"]
SellQuadGate = Literal["none", "weakening_ge2", "lead_to_weak_or_weak_ge2"]
StructForceExitMode = Literal["none", "drs_neg", "spread_lost"]
EntryGateMode = Literal["none", "omega_kinematic", "hybrid_top_decile", "hybrid_top_k"]
ChallengerRankBonus = Literal["none", "kin", "hybrid"]
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
    exit_loop_mode: ExitLoopMode = "day_first_poll"  # phase3=+hard_stop 1m · swap 盤中重排
    poll_interval_min: int = 5
    no_trade_before: str = "09:30"
    # snapshot_1300：True=全部 snap_ids 需 13:00；False=缺價保留 EOD（研究／kbar 稀疏）
    snapshot_strict_kbar: bool = True
    swap_target: SwapTarget = "worst_held"
    max_swaps_per_day: int = 1
    sort_key: SortKey = "seg_last"
    score_margin: float | None = None  # None → 使用 seg_margin（向後相容）
    swap_margin_key: SwapMarginKey = "seg_last"  # 買方門檻度量：seg_last（冠軍）/ sort_key / none
    swap_accel_margin: float | None = None  # 額外相對加速門檻（held_accel+δ）；None=關閉
    swap_confirm_days: int = 1  # 同一 sell→buy 配對連續 N 日通過門檻才換倉
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
    sell_accel_jerk_days: int | None = None  # 連續 N 日 avg_accel 下降才可賣（H-SELL-1）
    sell_rel_weak_gap: float | None = None  # 最弱 vs 次弱 accel 差距 ≥δ 才換（H-SELL-3）
    sell_quad_gate: SellQuadGate = "none"  # 象限路徑賣閘（H-SELL-2）· 與 S2 force-exit 分離
    swap_min_challenger_n: int | None = None  # 非持倉 challenger 數 <N → 禁止 score_swap（H-CTX-2）
    breadth_entry_zones: list[str] | None = None  # None → 不限制進場
    breadth_swap_zones: list[str] | None = None  # None → 不限制換倉
    breadth_pool_mode: BreadthPoolMode = "always_fresh"
    breadth_challenger_pool_mode: BreadthChallengerPoolMode = "entry_day"
    # swap gate 用哪日 zone：swap_day=as_of（換倉日）；entry_day=held leg signal_date
    breadth_swap_zone_date: BreadthSwapZoneDate = "swap_day"
    n_slots: int = MAX_SLOTS
    fill_mode: FillMode = "legacy_sequential"
    fill_repeat: bool = False  # True → 空槽可重複買同一支（加碼補滿，不留閒置資金）
    gain_waiver_threshold: float | None = None  # 隔日 close vs entry_px ≥ 此值 → 縮短 min_hold
    gain_waiver_min_hold: int = 2  # waiver 生效時 effective min_hold（交易日）
    quad_weakening_sell_days: int | None = None  # 連續 weakening N 日 → 忽略 accel_sell_negative_only
    quad_lagging_sell_days: int | None = None  # 連續 lagging N 日 → 忽略 accel_sell_negative_only
    accel_sell_bypass_on_spread_lost: bool = False  # spread_rs≤0 → 正加速 held 亦可賣
    loss_waiver_threshold: float | None = None  # close vs entry_px ≤ 此值（負）→ 縮短 min_hold
    loss_waiver_min_hold: int = 2  # loss waiver 生效時 effective min_hold（交易日）
    swap_margin_relax_neg_accel_days: int | None = None  # worst_held 連續 N 日 avg_accel<0
    swap_margin_relax_to: float | None = None  # base margin 無 challenger 時降至
    quad_force_exit_mode: QuadForceExitMode = "none"  # 象限強制平倉（不需 challenger）
    quad_force_exit_min_days: int = 1  # 連續 N 日於目標象限後可強制平倉
    quad_force_exit_requires_min_hold: bool = True  # 尊重 min_hold（loss_waiver 仍生效）
    quad_force_exit_loss_pct: float | None = None  # (close/entry_px-1) ≤ 此值才強制平倉；None=不檢查
    struct_force_exit_mode: StructForceExitMode = "none"  # drs / extension 結構強制平倉
    struct_force_exit_min_days: int = 2
    struct_force_exit_requires_min_hold: bool = True
    struct_force_exit_loss_pct: float | None = None
    hard_stop_loss_pct: float | None = None  # (close/entry_px-1) ≤ 此值 → 硬停損；None=關閉
    hard_stop_requires_min_hold: bool = False  # True → 尊重 min_hold（預設可早於 min_hold 觸發）
    hard_stop_intraday: bool = False  # True → 1m low 觸及 stop_px（優先於收盤判定）
    portfolio_loss_cap_pct: float | None = None  # 組合權益 ≤ initial×(1−cap) → 全平並停止新進場
    # G_R5_12：進場／換倉買前 N 日報酬 > max → 擋（None=關閉）
    entry_prior_ret_days: int = 5
    entry_prior_ret_max: float | None = None
    quad_force_exit_pause_spread_rs_positive: bool = False  # W5>W20 close → pause quad force-exit
    quad_force_exit_pause_spread_rs_streak_min: int = 0  # N consecutive spread_rs>0 days → pause
    quad_force_exit_pause_hybrid_mom_positive: bool = False  # hybrid Y (W20 ratio · WMA5 mom) >100 → pause
    entry_gate_mode: EntryGateMode = "none"  # Ω′ / hybrid top-K entry+swap gate (lookup passed at sim)
    hybrid_top_k: int = 10  # hybrid_top_k gate · top N per day in Ω′
    intraday_swap_confirm: bool = False  # swap challenger must pass 13:30 imp_extension strict
    intraday_swap_tiebreak: bool = False  # prefer 13:30 extension among margin-pass challengers
    challenger_rank_bonus: ChallengerRankBonus = "none"  # soft rank boost · no filter
    challenger_rank_bonus_weight: float = 0.05  # bonus = weight × normalized score
    challenger_rank_bonus_margin_only: bool = False  # bonus only within seg_last margin band
    rrg_length: int = 20  # WMA length for pool calendars (research sweep metadata)

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
        "四日加速 · 卖转弱 · 买转强 · margin=0.05 · fresh∪accel",
        entry_leg="C0",
        min_hold_days=5,
        max_hold_days=10,
        timing_mode="poll_5m",
        sort_key="avg_accel_decel",
        score_margin=0.05,
        accel_sell_negative_only=True,
        buy_sort_key="avg_accel_decel",
        candidate_pool="fresh_union_accel",
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
        # E@13:00 開窗 · 20260714 採納（ops ≥30m-to-close · SNAP_1300≈1320 · confirm_1250 +0.42pp）
        "no_trade_before": "13:00",
        "sort_key": "avg_accel_decel",
        "score_margin": 0.05,
        "accel_sell_negative_only": True,
        "buy_sort_key": "avg_accel_decel",
        "accel_lookback": 4,
        "candidate_lookback": 4,
        # G_R5_12 · 20260712_c18acc_r5_12_g3 · GO_ADOPT_G_R5_12
        "entry_prior_ret_days": 5,
        "entry_prior_ret_max": 0.12,
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


def _neg_avg_accel_streak_days(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    as_of: str,
    stock_id: str,
    entry_date: str,
    lb: int,
) -> int:
    """自 as_of 往回 · 連續 avg_accel<0 交易日數（含 as_of）。"""
    if as_of not in full_dates or entry_date not in full_dates:
        return 0
    si = full_dates.index(as_of)
    ei = full_dates.index(entry_date)
    streak = 0
    for j in range(si, ei - 1, -1):
        scalar = _avg_accel_scalar(
            rs_ratio, rs_mom, full_dates, full_dates[j], stock_id, lb=lb
        )
        if scalar is not None and scalar < 0:
            streak += 1
        else:
            break
    return streak


def _avg_accel_decline_streak_days(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    as_of: str,
    stock_id: str,
    lb: int,
    max_lookback: int = 10,
) -> int:
    """自 as_of 往回 · 連續「今日 avg_accel < 昨」日數（jerk 轉弱）。"""
    if as_of not in full_dates:
        return 0
    si = full_dates.index(as_of)
    streak = 0
    for j in range(si, max(0, si - max_lookback), -1):
        if j < 1:
            break
        a_now = _avg_accel_scalar(
            rs_ratio, rs_mom, full_dates, full_dates[j], stock_id, lb=lb
        )
        a_prev = _avg_accel_scalar(
            rs_ratio, rs_mom, full_dates, full_dates[j - 1], stock_id, lb=lb
        )
        if a_now is None or a_prev is None:
            break
        if float(a_now) < float(a_prev):
            streak += 1
        else:
            break
    return streak


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


def _mom_minus_ratio_row(row: ScanRow) -> float:
    """Δ = RS-Momentum − RS-Ratio（動能相對位置；>0 偏未過熱）。"""
    return float(row.rs_momentum) - float(row.rs_ratio)


def _mom_minus_ratio_pos(pos: dict[str, Any]) -> float:
    """進場時凍結的 Mom−Ratio（與 `_buy_threshold_score` 同：用入場快照）。"""
    if pos.get("mom_minus_ratio") is not None:
        return float(pos["mom_minus_ratio"])
    rs = pos.get("rs_ratio")
    mom = pos.get("rs_momentum")
    if rs is not None and mom is not None:
        return float(mom) - float(rs)
    return 0.0


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


def _rank_rows_for_fill(
    rows: list[ScanRow],
    config: ScoreSwapCConfig,
    *,
    challenger_va_dot: dict[str, float],
    challenger_avg_accel: dict[str, float],
) -> list[ScanRow]:
    """空槽補倉排序 · batch_topk 用 buy_sort_key（預設 avg_accel_decel）。"""
    buy_key: BuySortKey = config.buy_sort_key or config.sort_key  # type: ignore[assignment]
    if buy_key in ("accel_decel", "avg_accel_decel"):
        scored: list[tuple[float, ScanRow]] = []
        for row in rows:
            score = _buy_row_score(
                row,
                buy_key,
                challenger_va_dot=challenger_va_dot,
                challenger_avg_accel=challenger_avg_accel,
            )
            if score is not None:
                scored.append((score, row))
        scored.sort(key=lambda x: (-x[0], x[1].stock_id))
        return [row for _, row in scored]
    return sorted(rows, key=lambda r: (-r.seg_last, r.stock_id))


def _trading_days_between(full_dates: list[str], start: str, end: str) -> int:
    if start > end:
        return 0
    return sum(1 for d in full_dates if start < d <= end)


def _day1_return(
    close: pd.DataFrame,
    full_dates: list[str],
    *,
    entry_date: str,
    entry_px: float,
    stock_id: str,
    as_of: str,
) -> float | None:
    """進場後第 1 個交易日收盤 vs entry_px · PIT：as_of 須 ≥ day1。"""
    if entry_date not in full_dates or as_of not in full_dates:
        return None
    si = full_dates.index(entry_date)
    if si + 1 >= len(full_dates):
        return None
    day1 = full_dates[si + 1]
    if as_of < day1:
        return None
    if stock_id not in close.columns:
        return None
    c = close.at[day1, stock_id]
    if c != c or entry_px <= 0:
        return None
    return float(c) / entry_px - 1.0


def _close_return_vs_entry(
    close: pd.DataFrame,
    *,
    entry_date: str,
    entry_px: float,
    stock_id: str,
    as_of: str,
) -> float | None:
    """as_of 收盤 vs entry_px · PIT。"""
    if as_of not in close.index or stock_id not in close.columns:
        return None
    c = close.at[as_of, stock_id]
    if c != c or entry_px <= 0:
        return None
    return float(c) / entry_px - 1.0


def _effective_min_hold_days(
    config: ScoreSwapCConfig,
    close: pd.DataFrame,
    full_dates: list[str],
    pos: dict[str, Any],
    as_of: str,
) -> int:
    base = config.min_hold_days
    th = config.gain_waiver_threshold
    if th is not None:
        d1 = _day1_return(
            close,
            full_dates,
            entry_date=str(pos["entry_date"]),
            entry_px=float(pos["entry_px"]),
            stock_id=str(pos["stock_id"]),
            as_of=as_of,
        )
        if d1 is not None and d1 >= th:
            return config.gain_waiver_min_hold
    lw = config.loss_waiver_threshold
    if lw is not None:
        ret = _close_return_vs_entry(
            close,
            entry_date=str(pos["entry_date"]),
            entry_px=float(pos["entry_px"]),
            stock_id=str(pos["stock_id"]),
            as_of=as_of,
        )
        if ret is not None and ret <= lw:
            return config.loss_waiver_min_hold
    return base


def _stock_end_q(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
    stock_id: str,
) -> str | None:
    from research.backtest.rrg_mono_swap_exit_b import _daily_feat

    feat = _daily_feat(rs_ratio, rs_mom, full_dates, as_of, stock_id)
    if feat is None:
        return None
    q = str(feat.get("end_q") or "").lower()
    return q or None


def _quad_streak_days(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    as_of: str,
    stock_id: str,
    target_quad: str,
    entry_date: str,
) -> int:
    """自 entry 後至 as_of · 連續 target_quad 天數（含 as_of）。"""
    if as_of not in full_dates or entry_date not in full_dates:
        return 0
    streak = 0
    si = full_dates.index(as_of)
    ei = full_dates.index(entry_date)
    for j in range(si, ei - 1, -1):
        d = full_dates[j]
        q = _stock_end_q(rs_ratio, rs_mom, full_dates, d, stock_id)
        if q == target_quad:
            streak += 1
        else:
            break
    return streak


def _quad_sell_bypass_accel(
    config: ScoreSwapCConfig,
    *,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
    stock_id: str,
    entry_date: str,
) -> bool:
    if config.quad_weakening_sell_days is not None:
        ws = _quad_streak_days(
            rs_ratio,
            rs_mom,
            full_dates,
            as_of=as_of,
            stock_id=stock_id,
            target_quad="weakening",
            entry_date=entry_date,
        )
        if ws >= config.quad_weakening_sell_days:
            return True
    if config.quad_lagging_sell_days is not None:
        ls = _quad_streak_days(
            rs_ratio,
            rs_mom,
            full_dates,
            as_of=as_of,
            stock_id=stock_id,
            target_quad="lagging",
            entry_date=entry_date,
        )
        if ls >= config.quad_lagging_sell_days:
            return True
    return False


def _sell_quad_gate_ok(
    gate: SellQuadGate,
    *,
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
    stock_id: str,
    entry_date: str,
) -> bool:
    """H-SELL-2 · 象限路徑賣閘（不觸發 force-exit，只限制 rotation 賣池）。"""
    if gate == "none":
        return True
    weak_streak = _quad_streak_days(
        rs_ratio,
        rs_mom,
        full_dates,
        as_of=as_of,
        stock_id=stock_id,
        target_quad="weakening",
        entry_date=entry_date,
    )
    if gate == "weakening_ge2":
        return weak_streak >= 2
    # lead_to_weak_or_weak_ge2
    if weak_streak >= 2:
        return True
    if as_of not in full_dates:
        return False
    si = full_dates.index(as_of)
    if si < 1:
        return False
    q_now = _stock_end_q(rs_ratio, rs_mom, full_dates, as_of, stock_id)
    q_prev = _stock_end_q(rs_ratio, rs_mom, full_dates, full_dates[si - 1], stock_id)
    return q_now == "weakening" and q_prev == "leading"


def quad_force_exit_streak_days(
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    *,
    as_of: str,
    stock_id: str,
    entry_date: str,
    mode: QuadForceExitMode,
) -> int:
    """自 entry 後至 as_of · 連續符合 force-exit 象限的天數（含 as_of）。"""
    if mode == "none":
        return 0
    if mode in ("weakening", "lagging"):
        return _quad_streak_days(
            rs_ratio,
            rs_mom,
            full_dates,
            as_of=as_of,
            stock_id=stock_id,
            target_quad=mode,
            entry_date=entry_date,
        )
    if mode == "weakening_or_lagging":
        if as_of not in full_dates or entry_date not in full_dates:
            return 0
        streak = 0
        si = full_dates.index(as_of)
        ei = full_dates.index(entry_date)
        for j in range(si, ei - 1, -1):
            d = full_dates[j]
            q = _stock_end_q(rs_ratio, rs_mom, full_dates, d, stock_id)
            if q in ("weakening", "lagging"):
                streak += 1
            else:
                break
        return streak
    return 0


def drs_neg_streak_days(
    rs_ratio: pd.DataFrame,
    full_dates: list[str],
    *,
    as_of: str,
    stock_id: str,
    entry_date: str,
) -> int:
    """連續 drs_rsr_1d ≤ 0 天數（含 as_of）。"""
    if as_of not in full_dates or entry_date not in full_dates:
        return 0
    if stock_id not in rs_ratio.columns:
        return 0
    si = full_dates.index(as_of)
    ei = full_dates.index(entry_date)
    streak = 0
    for j in range(si, ei - 1, -1):
        d = full_dates[j]
        if j <= 0:
            break
        dp = full_dates[j - 1]
        rv = float(rs_ratio.at[d, stock_id])
        rv_p = float(rs_ratio.at[dp, stock_id])
        if pd.notna(rv) and pd.notna(rv_p) and rv - rv_p <= 0:
            streak += 1
        else:
            break
    return streak


def spread_lost_streak_days(
    spread_rs_panel: pd.DataFrame,
    full_dates: list[str],
    *,
    as_of: str,
    stock_id: str,
    entry_date: str,
) -> int:
    """連續 spread_rs ≤ 0（W5 不再領先 W20）天數（含 as_of）。"""
    if as_of not in full_dates or entry_date not in full_dates:
        return 0
    if stock_id not in spread_rs_panel.columns:
        return 0
    si = full_dates.index(as_of)
    ei = full_dates.index(entry_date)
    streak = 0
    for j in range(si, ei - 1, -1):
        d = full_dates[j]
        srs = float(spread_rs_panel.at[d, stock_id])
        if pd.notna(srs) and srs <= 0:
            streak += 1
        else:
            break
    return streak


def struct_force_exit_streak_days(
    config: ScoreSwapCConfig,
    *,
    rs_ratio: pd.DataFrame,
    spread_rs_panel: pd.DataFrame | None,
    full_dates: list[str],
    as_of: str,
    stock_id: str,
    entry_date: str,
) -> int:
    if config.struct_force_exit_mode == "drs_neg":
        return drs_neg_streak_days(
            rs_ratio, full_dates, as_of=as_of, stock_id=stock_id, entry_date=entry_date
        )
    if config.struct_force_exit_mode == "spread_lost" and spread_rs_panel is not None:
        return spread_lost_streak_days(
            spread_rs_panel,
            full_dates,
            as_of=as_of,
            stock_id=stock_id,
            entry_date=entry_date,
        )
    return 0


def struct_force_exit_eligible(
    config: ScoreSwapCConfig,
    *,
    hold_days: int,
    effective_min_hold: int,
    streak_days: int,
    unrealized_ret: float | None = None,
) -> bool:
    if config.struct_force_exit_mode == "none":
        return False
    req_hold = effective_min_hold if config.struct_force_exit_requires_min_hold else 0
    if hold_days < req_hold:
        return False
    if streak_days < config.struct_force_exit_min_days:
        return False
    if config.struct_force_exit_loss_pct is not None:
        if unrealized_ret is None:
            return False
        if unrealized_ret > config.struct_force_exit_loss_pct:
            return False
    return True


def quad_force_exit_eligible(
    config: ScoreSwapCConfig,
    *,
    hold_days: int,
    effective_min_hold: int,
    streak_days: int,
    unrealized_ret: float | None = None,
) -> bool:
    if config.quad_force_exit_mode == "none":
        return False
    req_hold = effective_min_hold if config.quad_force_exit_requires_min_hold else 0
    if hold_days < req_hold:
        return False
    if streak_days < config.quad_force_exit_min_days:
        return False
    if config.quad_force_exit_loss_pct is not None:
        if unrealized_ret is None:
            return False
        if unrealized_ret > config.quad_force_exit_loss_pct:
            return False
    return True


def hard_stop_exit_eligible(
    config: ScoreSwapCConfig,
    *,
    hold_days: int,
    effective_min_hold: int,
    unrealized_ret: float | None = None,
) -> bool:
    if config.hard_stop_loss_pct is None:
        return False
    if config.hard_stop_requires_min_hold and hold_days < effective_min_hold:
        return False
    if unrealized_ret is None:
        return False
    return unrealized_ret <= config.hard_stop_loss_pct


def _norm_kbar_minute(minute: str) -> str:
    parts = str(minute).strip().split(":")
    if len(parts) >= 2:
        return f"{int(parts[0]):02d}:{parts[1][:2]}"
    return str(minute)


def intraday_hard_stop_trigger(
    conn: sqlite3.Connection,
    *,
    stock_id: str,
    trade_date: str,
    entry_px: float,
    entry_date: str,
    entry_minute: str | None,
    stop_pct: float,
    bars_cache: dict[tuple[str, str], tuple[KbarBar, ...]],
) -> tuple[float | None, str | None]:
    """Scan 1m bars on trade_date · return (exit_px, exit_minute) if low ≤ stop_px."""
    if entry_px <= 0:
        return None, None
    stop_px = entry_px * (1.0 + stop_pct)
    key = (stock_id, trade_date)
    if key not in bars_cache:
        bars_cache[key] = load_kbar_day_bars(conn, stock_id, trade_date)
    bars = bars_cache[key]
    if not bars:
        return None, None
    is_entry_day = trade_date == entry_date
    start_min = _norm_kbar_minute(entry_minute) if is_entry_day and entry_minute else "00:00"
    for bar in bars:
        if _norm_kbar_minute(bar.minute) < start_min:
            continue
        if bar.low <= stop_px:
            exit_px = stop_px if bar.open >= stop_px else float(bar.open)
            return exit_px, bar.minute
    return None, None


def _portfolio_equity(
    book: dict[str, Any],
    slots: list[dict[str, Any]],
    close: pd.DataFrame,
    as_of: str,
) -> float:
    equity = float(book["cash"])
    for pos in slots:
        alloc = float(pos.get("allocation") or book["slot_cap"])
        entry_px = float(pos["entry_px"])
        sid = str(pos["stock_id"])
        if entry_px <= 0 or as_of not in close.index or sid not in close.columns:
            equity += alloc
            continue
        px = float(close.at[as_of, sid])
        equity += alloc * (px / entry_px) if px > 0 else alloc
    return equity


def _portfolio_tag_new_entries(book: dict[str, Any], slots: list[dict[str, Any]]) -> None:
    for pos in slots:
        if pos.get("_portfolio_booked"):
            continue
        alloc = min(float(book["slot_cap"]), float(book["cash"]))
        if alloc <= 0:
            continue
        pos["allocation"] = alloc
        pos["_portfolio_booked"] = True
        book["cash"] -= alloc


def _portfolio_credit_exit(
    book: dict[str, Any] | None,
    pos: dict[str, Any],
    exit_px: float,
) -> None:
    if book is None:
        return
    alloc = float(pos.get("allocation") or book["slot_cap"])
    entry_px = float(pos["entry_px"])
    if entry_px > 0 and exit_px > 0:
        book["cash"] += alloc * (exit_px / entry_px)


def build_daily_spread_rs_panel(
    close: pd.DataFrame,
    bench: pd.Series,
    *,
    stock_ids: list[str] | tuple[str, ...] | None = None,
    lookback_bars: int | None = None,
) -> pd.DataFrame:
    """Daily close · WMA(5) − WMA(20) spread_rs.

    Optional ``stock_ids`` / ``lookback_bars`` narrow computation without changing
    rolling-WMA terminal values when lookback ≥ ~3×WMA_LONG (live screen hot path).
    """
    from rrg_universe_intraday_panel import WMA_LONG_LENGTH, WMA_SHORT_LENGTH

    panel = close
    if stock_ids is not None:
        cols = [sid for sid in stock_ids if sid in panel.columns]
        if not cols:
            return pd.DataFrame(index=panel.index)
        panel = panel[cols]
    if lookback_bars is not None and lookback_bars > 0 and len(panel.index) > lookback_bars:
        panel = panel.iloc[-lookback_bars:]
    bench_p = bench.reindex(panel.index).astype(float)
    rs5, _, _ = compute_rrg_panel(panel, bench_p, length=WMA_SHORT_LENGTH)
    rs20, _, _ = compute_rrg_panel(panel, bench_p, length=WMA_LONG_LENGTH)
    common = rs5.columns.intersection(rs20.columns)
    return rs5[common].subtract(rs20[common], axis=0)


def held_accel_bypass_on_spread_lost(
    *,
    config: ScoreSwapCConfig,
    spread_rs_panel: pd.DataFrame | None,
    as_of: str,
    slot_stock_ids: list[str],
) -> set[str]:
    """spread_rs≤0 → held names bypass accel_sell_negative_only (rotation sell)."""
    if not config.accel_sell_bypass_on_spread_lost or spread_rs_panel is None:
        return set()
    bypass: set[str] = set()
    for sid in slot_stock_ids:
        if not spread_rs_positive_at(spread_rs_panel, as_of=as_of, stock_id=sid):
            bypass.add(sid)
    return bypass


def spread_rs_positive_at(
    spread_rs_panel: pd.DataFrame | None,
    *,
    as_of: str,
    stock_id: str,
) -> bool:
    """Daily close · WMA(5) spread_rs > 0 (W5 leading W20)."""
    if spread_rs_panel is None or as_of not in spread_rs_panel.index:
        return False
    if stock_id not in spread_rs_panel.columns:
        return False
    srs = float(spread_rs_panel.at[as_of, stock_id])
    return pd.notna(srs) and srs > 0


def spread_rs_positive_streak_days(
    spread_rs_panel: pd.DataFrame | None,
    *,
    as_of: str,
    stock_id: str,
    full_dates: list[str] | None = None,
) -> int:
    """Consecutive trading days ending at as_of with spread_rs > 0 (inclusive)."""
    if spread_rs_panel is None:
        return 0
    if full_dates is not None:
        dates = [d for d in full_dates if d <= as_of]
    else:
        dates = [str(d) for d in spread_rs_panel.index if str(d) <= as_of]
    streak = 0
    for d in reversed(dates):
        if spread_rs_positive_at(spread_rs_panel, as_of=d, stock_id=stock_id):
            streak += 1
        else:
            break
    return streak


def quad_force_exit_paused_by_positive_spread(
    config: ScoreSwapCConfig,
    *,
    spread_rs_panel: pd.DataFrame | None,
    as_of: str,
    stock_id: str,
    full_dates: list[str] | None = None,
) -> bool:
    """Track 2 · skip quad force-exit when held leg has positive spread_rs streak."""
    streak_min = int(config.quad_force_exit_pause_spread_rs_streak_min or 0)
    if streak_min > 0:
        streak = spread_rs_positive_streak_days(
            spread_rs_panel,
            as_of=as_of,
            stock_id=stock_id,
            full_dates=full_dates,
        )
        return streak >= streak_min
    if not config.quad_force_exit_pause_spread_rs_positive:
        return False
    return spread_rs_positive_at(spread_rs_panel, as_of=as_of, stock_id=stock_id)


def quad_force_exit_paused_by_hybrid_mom(
    config: ScoreSwapCConfig,
    *,
    hybrid_mom_panel: pd.DataFrame | None,
    as_of: str,
    stock_id: str,
) -> bool:
    """Pause quad force-exit when hybrid Y-momentum (W20 ratio · WMA5) is still >100.

    Rationale (20260707 forward-return study): W20-leading legs whose hybrid Y
    momentum is still positive keep positive forward excess (~+0.4% h3), so defer
    the S2 weakening force-exit while the fast momentum has not rolled over.
    """
    if not config.quad_force_exit_pause_hybrid_mom_positive:
        return False
    if hybrid_mom_panel is None:
        return False
    if as_of not in hybrid_mom_panel.index or stock_id not in hybrid_mom_panel.columns:
        return False
    val = hybrid_mom_panel.at[as_of, stock_id]
    return bool(pd.notna(val) and float(val) > 100.0)


def filter_scan_rows_by_gate(
    rows: list[ScanRow],
    gate_ids: set[str] | None,
) -> list[ScanRow]:
    """Intersect candidate shortlist with per-day gate set."""
    if gate_ids is None:
        return list(rows)
    if not gate_ids:
        return []
    return [r for r in rows if r.stock_id in gate_ids]


def apply_gate_by_date(
    rows: list[ScanRow],
    as_of: str,
    gate_by_date: dict[str, set[str]] | None,
) -> list[ScanRow]:
    if gate_by_date is None:
        return list(rows)
    return filter_scan_rows_by_gate(rows, gate_by_date.get(as_of, set()))


def prior_n_day_return(
    close: pd.DataFrame,
    stock_id: str,
    full_dates: list[str],
    *,
    as_of: str,
    n_days: int,
) -> float | None:
    """PIT prior return ending yesterday · close[as_of-1]/close[as_of-1-n] - 1."""
    if n_days <= 0 or as_of not in full_dates or stock_id not in close.columns:
        return None
    i = full_dates.index(as_of)
    if i < n_days + 1:
        return None
    end_d = full_dates[i - 1]
    start_d = full_dates[i - 1 - n_days]
    try:
        c1 = float(close.at[end_d, stock_id])
        c0 = float(close.at[start_d, stock_id])
    except (KeyError, TypeError, ValueError):
        return None
    if not (c0 > 0 and c1 == c1 and c0 == c0):
        return None
    return c1 / c0 - 1.0


def prior_ret_gate_allows(
    close: pd.DataFrame,
    stock_id: str,
    full_dates: list[str],
    *,
    as_of: str,
    n_days: int,
    max_ret: float | None,
) -> bool:
    """True if stock may enter/swap-buy · False when prior N-day return > max_ret."""
    if max_ret is None:
        return True
    ret = prior_n_day_return(
        close, stock_id, full_dates, as_of=as_of, n_days=int(n_days)
    )
    if ret is None:
        return True
    return float(ret) <= float(max_ret)


def filter_shortlist_by_prior_ret(
    rows: list[ScanRow],
    *,
    close: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
    config: ScoreSwapCConfig,
) -> list[ScanRow]:
    max_ret = config.entry_prior_ret_max
    if max_ret is None:
        return list(rows)
    n_days = max(1, int(config.entry_prior_ret_days or 5))
    return [
        r
        for r in rows
        if prior_ret_gate_allows(
            close,
            r.stock_id,
            full_dates,
            as_of=as_of,
            n_days=n_days,
            max_ret=float(max_ret),
        )
    ]


def intraday_swap_confirm_ok(
    intraday_extension_by_date: dict[str, set[str]] | None,
    *,
    as_of: str,
    stock_id: str,
) -> bool:
    """Track 3 · challenger must be in 13:30 imp_extension strict set."""
    if intraday_extension_by_date is None:
        return True
    return stock_id in intraday_extension_by_date.get(as_of, set())


def _challenger_rank_bonus_delta(
    config: ScoreSwapCConfig,
    row: ScanRow,
    *,
    rank_bonus_by_sid: dict[str, float] | None,
    beats: list[ScanRow],
) -> float:
    """Soft rank boost · never filters candidates."""
    if config.challenger_rank_bonus == "none" or not rank_bonus_by_sid:
        return 0.0
    bonus = float(rank_bonus_by_sid.get(row.stock_id, 0.0)) * config.challenger_rank_bonus_weight
    if config.challenger_rank_bonus_margin_only and beats:
        max_seg = max(float(r.seg_last) for r in beats)
        if float(row.seg_last) < max_seg - config.effective_margin:
            return 0.0
    return bonus


def _pick_best_challenger(
    beats: list[ScanRow],
    config: ScoreSwapCConfig,
    *,
    buy_key: BuySortKey | None,
    row_score,
    challenger_va_dot: dict[str, float],
    challenger_avg_accel: dict[str, float],
    rank_bonus_by_sid: dict[str, float] | None,
    intraday_extension_by_date: dict[str, set[str]] | None,
    as_of: str | None,
) -> ScanRow | None:
    """Pick buy row · optional intraday tiebreak + soft rank bonus."""
    if not beats:
        return None
    pool = list(beats)
    if config.intraday_swap_tiebreak and intraday_extension_by_date is not None and as_of:
        ext_ids = intraday_extension_by_date.get(as_of, set())
        with_ext = [r for r in pool if r.stock_id in ext_ids]
        if with_ext:
            pool = with_ext
    if buy_key is not None:
        scored: list[tuple[float, ScanRow]] = []
        for row in pool:
            base = _buy_row_score(
                row,
                buy_key,
                challenger_va_dot=challenger_va_dot,
                challenger_avg_accel=challenger_avg_accel,
            )
            if base is None:
                continue
            scored.append(
                (
                    float(base) + _challenger_rank_bonus_delta(
                        config, row, rank_bonus_by_sid=rank_bonus_by_sid, beats=beats
                    ),
                    row,
                )
            )
        return max(scored, key=lambda x: (x[0], x[1].stock_id))[1] if scored else None

    def ranked_score(row: ScanRow) -> float:
        return float(row_score(row)) + _challenger_rank_bonus_delta(
            config, row, rank_bonus_by_sid=rank_bonus_by_sid, beats=beats
        )

    return max(pool, key=lambda r: (ranked_score(r), r.stock_id))


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
    if config.timing_mode == "snapshot_1300":
        from research.backtest.c18acc_snapshot_1300 import SNAPSHOT_1300_MINUTE, snapshot_px_strict

        px = snapshot_px_strict(
            conn, stock_id, trade_date, kbar_cache=kbar_cache
        )
        if px is None:
            return None, None
        return px, SNAPSHOT_1300_MINUTE
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
    first_poll = str(config.no_trade_before or "09:30")
    return close_px, first_poll


def _finite_mean_excess_pct(periods: list[dict[str, Any]]) -> float | None:
    vals: list[float] = []
    for p in periods:
        ex = p.get("excess_pct")
        if ex is None:
            continue
        try:
            v = float(ex)
        except (TypeError, ValueError):
            continue
        if v == v:
            vals.append(v)
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _settle_leg(
    conn: sqlite3.Connection,
    *,
    pos: dict[str, Any],
    exit_date: str,
    exit_px: float,
    exit_reason: str,
    config: ScoreSwapCConfig,
    full_dates: list[str],
    portfolio_book: dict[str, Any] | None = None,
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
    _portfolio_credit_exit(portfolio_book, pos, exit_px)
    return {
        "stock_id": sid,
        "stock_name": pos.get("stock_name", ""),
        "signal_date": str(pos.get("signal_date") or entry),
        "entry_date": entry,
        "exit_date": exit_date,
        "entry_px": round(entry_px, 4),
        "exit_px": round(exit_px, 4),
        "entry_minute": pos.get("entry_minute"),
        "exit_minute": pos.get("exit_minute"),
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


def _fresh_union_mono_pool(
    fresh_mono: list[ScanRow],
    mono_rows: list[ScanRow],
) -> list[ScanRow]:
    """fresh ∪ mono_tier2 · 週五 research · 不篩 accel>0。"""
    by_id: dict[str, ScanRow] = {r.stock_id: r for r in fresh_mono}
    for row in mono_rows:
        if row.stock_id not in by_id:
            by_id[row.stock_id] = row
    return sorted(by_id.values(), key=lambda r: (-r.seg_last, r.stock_id))


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
    if pool == "fresh_union_mono":
        mono_rows = (mono_by_date or {}).get(as_of, [])
        return _fresh_union_mono_pool(fresh_mono, mono_rows)
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
    held_accel_bypass: set[str] | None = None,
    as_of: str | None = None,
    intraday_extension_by_date: dict[str, set[str]] | None = None,
    challenger_rank_bonus_by_sid: dict[str, float] | None = None,
    rs_ratio: pd.DataFrame | None = None,
    rs_mom: pd.DataFrame | None = None,
    full_dates: list[str] | None = None,
    swap_margin: float | None = None,
) -> tuple[dict[str, Any] | None, ScanRow | None]:
    """回傳 (sell_pos, buy_row) · challenger 須 score > held + margin。"""
    eligible = [r for r in candidates if r.stock_id not in held_ids]
    if not eligible or not slots:
        return None, None

    margin = config.effective_margin if swap_margin is None else float(swap_margin)
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
    bypass = held_accel_bypass or set()
    if config.accel_sell_negative_only and key in ("accel_decel", "avg_accel_decel"):
        sell_pool = [
            p
            for p in sell_pool
            if held_score(p) < 0 or str(p["stock_id"]) in bypass
        ]
    jerk_need = config.sell_accel_jerk_days
    if (
        jerk_need is not None
        and int(jerk_need) > 0
        and key in ("accel_decel", "avg_accel_decel")
        and rs_ratio is not None
        and rs_mom is not None
        and full_dates is not None
        and as_of is not None
    ):
        need = int(jerk_need)
        lb = int(config.accel_lookback)
        sell_pool = [
            p
            for p in sell_pool
            if str(p["stock_id"]) in bypass
            or _avg_accel_decline_streak_days(
                rs_ratio,
                rs_mom,
                full_dates,
                as_of=as_of,
                stock_id=str(p["stock_id"]),
                lb=lb,
            )
            >= need
        ]
    if (
        config.sell_quad_gate != "none"
        and rs_ratio is not None
        and rs_mom is not None
        and full_dates is not None
        and as_of is not None
    ):
        sell_pool = [
            p
            for p in sell_pool
            if str(p["stock_id"]) in bypass
            or _sell_quad_gate_ok(
                config.sell_quad_gate,
                rs_ratio=rs_ratio,
                rs_mom=rs_mom,
                full_dates=full_dates,
                as_of=as_of,
                stock_id=str(p["stock_id"]),
                entry_date=str(p.get("entry_date") or as_of),
            )
        ]
    if _structural_gate_active(config.structural_gate):
        sell_pool = [p for p in sell_pool if trends.get(str(p["stock_id"])) == "down_left"]
    if not sell_pool:
        return None, None

    gap_need = config.sell_rel_weak_gap
    if (
        gap_need is not None
        and config.swap_target == "worst_held"
        and len(sell_pool) >= 2
    ):
        ranked = sorted(sell_pool, key=held_score)
        if held_score(ranked[1]) - held_score(ranked[0]) < float(gap_need):
            return None, None

    eligible = [r for r in eligible if challenger_ok(r)]
    if not eligible:
        return None, None

    use_seg_buy = key in ("accel_decel", "avg_accel_decel")
    buy_key = config.buy_sort_key

    def pick_buy(beats: list[ScanRow]) -> ScanRow | None:
        return _pick_best_challenger(
            beats,
            config,
            buy_key=buy_key,
            row_score=row_score,
            challenger_va_dot=va_dots,
            challenger_avg_accel=avg_accels,
            rank_bonus_by_sid=challenger_rank_bonus_by_sid,
            intraday_extension_by_date=intraday_extension_by_date,
            as_of=as_of,
        )

    def try_swap(margin: float, sell: dict[str, Any]) -> ScanRow | None:
        gate = config.swap_margin_key
        if gate == "none":
            beats = list(eligible)
        elif gate == "sort_key":
            threshold = held_score(sell) + margin
            if key in ("accel_decel", "avg_accel_decel"):
                buy_metric: BuySortKey = (
                    "avg_accel_decel" if key == "avg_accel_decel" else "accel_decel"
                )
                beats = []
                for r in eligible:
                    sc = _buy_row_score(
                        r,
                        buy_metric,
                        challenger_va_dot=va_dots,
                        challenger_avg_accel=avg_accels,
                    )
                    if sc is not None and float(sc) > threshold:
                        beats.append(r)
            else:
                beats = [r for r in eligible if row_score(r) > threshold]
        elif gate == "mom_minus_ratio":
            threshold = _mom_minus_ratio_pos(sell) + margin
            beats = [r for r in eligible if _mom_minus_ratio_row(r) > threshold]
        elif buy_key is not None or use_seg_buy:
            threshold = _buy_threshold_score(sell, key) + margin
            beats = [r for r in eligible if float(r.seg_last) > threshold]
        else:
            threshold = held_score(sell) + margin
            beats = [r for r in eligible if row_score(r) > threshold]
        extra = config.swap_accel_margin
        if extra is not None and key in ("accel_decel", "avg_accel_decel"):
            th_a = held_score(sell) + float(extra)
            kept: list[ScanRow] = []
            buy_metric: BuySortKey = (
                "avg_accel_decel" if key == "avg_accel_decel" else "accel_decel"
            )
            for r in beats:
                sc = _buy_row_score(
                    r,
                    buy_metric,
                    challenger_va_dot=va_dots,
                    challenger_avg_accel=avg_accels,
                )
                if sc is not None and float(sc) > th_a:
                    kept.append(r)
            beats = kept
        return pick_buy(beats)

    if config.swap_target == "worst_held":
        sell = min(sell_pool, key=held_score)
        buy = try_swap(margin, sell)
        if buy:
            return sell, buy
        relax_days = config.swap_margin_relax_neg_accel_days
        relax_to = config.swap_margin_relax_to
        if (
            buy is None
            and relax_days is not None
            and relax_to is not None
            and relax_to < margin
            and rs_ratio is not None
            and rs_mom is not None
            and full_dates is not None
            and as_of is not None
        ):
            streak = _neg_avg_accel_streak_days(
                rs_ratio,
                rs_mom,
                full_dates,
                as_of=as_of,
                stock_id=str(sell["stock_id"]),
                entry_date=str(sell["entry_date"]),
                lb=config.accel_lookback,
            )
            if streak >= relax_days:
                buy = try_swap(relax_to, sell)
                if buy:
                    return sell, buy
        return None, None

    for sell in sorted(sell_pool, key=held_score):
        buy = try_swap(margin, sell)
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
    spread_rs_panel: pd.DataFrame | None = None,
    hybrid_mom_panel: pd.DataFrame | None = None,
    zone_filter: str | None = None,
    entry_c_config: Any | None = None,
    slot_snapshots: dict[str, list[dict[str, Any]]] | None = None,
    swap_block_audit: dict[str, list[dict[str, Any]]] | None = None,
    entry_gate_by_date: dict[str, set[str]] | None = None,
    swap_gate_by_date: dict[str, set[str]] | None = None,
    intraday_extension_by_date: dict[str, set[str]] | None = None,
    challenger_rank_bonus_by_date: dict[str, dict[str, float]] | None = None,
    capital_ntd_per_slot: float = 10_000.0,
    weekday_gate: Any | None = None,
    snapshot_full_rrg_cache: dict[tuple[Any, ...], list[ScanRow]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from research.backtest.c18acc_weekday_gate import (
        WeekdayGateConfig,
        entry_target_date,
        flush_pending_entries,
        is_weekday_gate_active,
        pending_row_to_scan_row,
        queue_entry_candidates,
        should_anti_weekend_force_exit,
        should_defer_max_hold_default_to_friday,
        should_defer_exit_to_friday,
        swap_allowed_today,
        swap_margin_for_day,
        friday_swap_pool_override,
        max_swaps_for_day,
        week_friday_key,
    )

    cache = kbar_cache if kbar_cache is not None else {}
    bars_cache: dict[tuple[str, str], tuple[KbarBar, ...]] = {}
    kbar_stats = {"hits": 0, "checks": 0}
    if (
        entry_c_config is None
        and config.entry_leg == "C0"
        and config.timing_mode == "poll_5m"
        and config.sort_key == "avg_accel_decel"
    ):
        from research.backtest.rrg_mono_intraday_ab import champion_entry_c_config

        entry_c_config = champion_entry_c_config()
    slots: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    swaps = 0
    force_exits = 0
    swap_pair_streak: dict[tuple[str, str], int] = {}
    swap_confirm_need = max(1, int(getattr(config, "swap_confirm_days", 1) or 1))
    hard_stop_exits = 0
    quad_force_exit_paused = 0
    intraday_swap_blocked = 0
    max_hold_exits = 0
    n_slots = max(1, int(config.n_slots))
    slot_util_days = 0
    entry_zone_days = 0
    pool_shortfall_days = 0
    fresh_fill_days = 0       # free_k>0 且 fresh 池足夠，不需 tier2 補
    tier2_supplement_days = 0  # free_k>0 且觸發 tier2 降級補倉
    fill_repeat_days = 0      # fill_repeat=True 且發生加碼（同一支佔多槽）
    waiver_eligible_legs = 0  # 隔日漲幅 ≥ gain_waiver_threshold 的 leg 數（去重計一次）
    day1_observed_legs = 0
    early_waiver_swaps = 0    # waiver 下 hold < min_hold_days 的換倉
    early_waiver_excess: list[float] = []
    total_entries = 0
    pending_entries: list[dict[str, Any]] = []
    signals_skipped_expired = 0
    signals_deferred = 0
    swaps_deferred = 0
    swaps_lost = 0
    swap_deferred_this_week = 0
    snapshot_days_skipped = 0
    current_week_key: str | None = None
    wd_gate_active = weekday_gate is not None and is_weekday_gate_active(weekday_gate)
    portfolio_book: dict[str, Any] | None = None
    if config.portfolio_loss_cap_pct is not None:
        initial = n_slots * float(capital_ntd_per_slot)
        portfolio_book = {
            "initial": initial,
            "cash": initial,
            "slot_cap": float(capital_ntd_per_slot),
            "halted": False,
            "cap_triggers": 0,
        }

    def _settle(
        pos: dict[str, Any],
        *,
        exit_date: str,
        exit_px: float,
        exit_reason: str,
    ) -> dict[str, Any] | None:
        return _settle_leg(
            conn,
            pos=pos,
            exit_date=exit_date,
            exit_px=exit_px,
            exit_reason=exit_reason,
            config=config,
            full_dates=full_dates,
            portfolio_book=portfolio_book,
        )

    def _update_gain_waiver_stats(pos: dict[str, Any]) -> None:
        nonlocal waiver_eligible_legs, day1_observed_legs
        if config.gain_waiver_threshold is None or pos.get("gain_waiver_checked"):
            return
        d1 = _day1_return(
            close,
            full_dates,
            entry_date=str(pos["entry_date"]),
            entry_px=float(pos["entry_px"]),
            stock_id=str(pos["stock_id"]),
            as_of=as_of,
        )
        if d1 is None:
            return
        pos["gain_waiver_checked"] = True
        day1_observed_legs += 1
        pos["day1_return_pct"] = round(d1 * 100.0, 4)
        if d1 >= config.gain_waiver_threshold:
            pos["gain_waiver_eligible"] = True
            waiver_eligible_legs += 1
        else:
            pos["gain_waiver_eligible"] = False

    # 模式 C 填倉沿用 B 的 entry helper（需 structural gate 無）
    from research.backtest.rrg_mono_swap_exit_b import SwapExitBConfig, _daily_feat

    fill_cfg = SwapExitBConfig(
        variant_id=config.variant_id,
        label=config.label,
        entry_leg=config.entry_leg,
    )

    for as_of in trade_dates:
        if wd_gate_active and weekday_gate is not None:
            wk = week_friday_key(as_of)
            if current_week_key is not None and wk != current_week_key:
                if weekday_gate.swap_mode == "S2" and swap_deferred_this_week > 0:
                    swaps_lost += swap_deferred_this_week
                swap_deferred_this_week = 0
            current_week_key = wk

        if portfolio_book is not None and config.portfolio_loss_cap_pct is not None:
            if portfolio_book.get("halted"):
                continue
            floor = float(portfolio_book["initial"]) * (1.0 - float(config.portfolio_loss_cap_pct))
            if _portfolio_equity(portfolio_book, slots, close, as_of) <= floor:
                for pos in list(slots):
                    sell_px, sell_min = _swap_px(
                        conn,
                        close=close,
                        stock_id=str(pos["stock_id"]),
                        trade_date=as_of,
                        config=config,
                        kbar_cache=cache,
                    )
                    if sell_px is None:
                        continue
                    if sell_min:
                        pos["exit_minute"] = sell_min
                    leg = _settle(
                        pos,
                        exit_date=as_of,
                        exit_px=sell_px,
                        exit_reason="portfolio_loss_cap",
                    )
                    if leg is None:
                        continue
                    leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                    periods.append(leg)
                    slots.remove(pos)
                    force_exits += 1
                portfolio_book["halted"] = True
                portfolio_book["cap_triggers"] = int(portfolio_book.get("cap_triggers") or 0) + 1
                continue

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
        if wd_gate_active and weekday_gate is not None:
            swap_pool_type = friday_swap_pool_override(weekday_gate, swap_pool_type, as_of)  # type: ignore[assignment]
        entry_pool_rows = _candidate_pool(as_of, pool_type=entry_pool_type, **pool_kwargs)
        swap_pool_rows = _candidate_pool(as_of, pool_type=swap_pool_type, **pool_kwargs)
        day_rs_ratio = rs_ratio
        day_rs_mom = rs_mom
        day_spread_rs = spread_rs_panel
        if config.timing_mode == "snapshot_1300":
            from research.backtest.c18acc_snapshot_1300 import (
                collect_snapshot_stock_ids,
                rerank_shortlist_snapshot_1300,
                resolve_snapshot_1300_panels,
            )

            snap_ids = collect_snapshot_stock_ids(slots, entry_pool_rows, swap_pool_rows)
            # Empty snap_ids (no pool · no holdings): keep EOD panels — do not skip the day.
            # Missing 13:00 on required ids still skips (backfill kbar first).
            if snap_ids:
                snap_panels = resolve_snapshot_1300_panels(
                    conn,
                    close=close,
                    bench=bench,
                    as_of=as_of,
                    stock_ids=snap_ids,
                    rs_ratio=rs_ratio,
                    rs_mom=rs_mom,
                    spread_rs_panel=spread_rs_panel,
                    kbar_cache=cache,
                    strict_kbar=bool(
                        getattr(config, "snapshot_strict_kbar", True)
                    ),
                )
                if snap_panels is None:
                    snapshot_days_skipped += 1
                    continue
                day_rs_ratio = snap_panels.rs_ratio
                day_rs_mom = snap_panels.rs_mom
                day_spread_rs = snap_panels.spread_rs
        signal_zone = zone_by_date.get(as_of, "unknown")
        for pos in slots:
            _update_gain_waiver_stats(pos)
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
        if need_feat and day_rs_ratio is not None and day_rs_mom is not None:
            for pos in slots:
                sid = str(pos["stock_id"])
                entry_date = str(pos["entry_date"])
                skip_feat = False
                if config.sort_key == "accel_decel":
                    dot = _last_va_dot(day_rs_ratio, day_rs_mom, full_dates, as_of, sid)
                    if dot is not None:
                        held_today[sid] = float(dot)
                    skip_feat = config.structural_gate == "none"
                elif config.sort_key == "avg_accel_decel":
                    scalar = _avg_accel_scalar(
                        day_rs_ratio, day_rs_mom, full_dates, as_of, sid, lb=config.accel_lookback
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
                        rs_ratio=day_rs_ratio,
                        rs_mom=day_rs_mom,
                        full_dates=full_dates,
                        as_of=as_of,
                        stock_id=sid,
                        entry_date=entry_date,
                    )
                    continue
                feat = _daily_feat(day_rs_ratio, day_rs_mom, full_dates, as_of, sid)
                if not feat:
                    continue
                held_today[sid] = _seg_step_delta(feat["segs"])
                held_trend[sid] = _held_structural_trend(
                    structural_gate=config.structural_gate,
                    config=config,
                    feat=feat,
                    rs_ratio=day_rs_ratio,
                    rs_mom=day_rs_mom,
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
        if need_challenger_kin and day_rs_ratio is not None and day_rs_mom is not None:
            kin_rows = swap_pool_rows if swap_pool_rows else []
            for row in kin_rows:
                sid = row.stock_id
                if config.challenger_gate == "recent_accel_up":
                    t = _avg_acceleration_trend(
                        day_rs_ratio, day_rs_mom, full_dates, as_of, sid, lb=config.accel_lookback
                    )
                    if t:
                        challenger_trend[sid] = t
                if config.challenger_gate == "v_dot_positive" or config.buy_sort_key == "accel_decel":
                    dot = _last_va_dot(day_rs_ratio, day_rs_mom, full_dates, as_of, sid)
                    if dot is not None:
                        challenger_va_dot[sid] = float(dot)
                if (
                    config.candidate_rank_key == "avg_accel_decel"
                    or config.candidate_require_positive_accel
                    or config.buy_sort_key == "avg_accel_decel"
                ):
                    scalar = _avg_accel_scalar(
                        day_rs_ratio, day_rs_mom, full_dates, as_of, sid, lb=config.accel_lookback
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
        swap_shortlist = apply_gate_by_date(swap_shortlist, as_of, swap_gate_by_date)
        entry_shortlist = apply_gate_by_date(entry_shortlist, as_of, entry_gate_by_date)
        swap_shortlist = filter_shortlist_by_prior_ret(
            swap_shortlist,
            close=close,
            full_dates=full_dates,
            as_of=as_of,
            config=config,
        )
        entry_shortlist = filter_shortlist_by_prior_ret(
            entry_shortlist,
            close=close,
            full_dates=full_dates,
            as_of=as_of,
            config=config,
        )
        if config.timing_mode == "snapshot_1300":
            from research.backtest.c18acc_snapshot_1300 import rerank_shortlist_snapshot_1300

            swap_shortlist = rerank_shortlist_snapshot_1300(
                swap_shortlist,
                conn=conn,
                close=close,
                bench=bench,
                trade_date=as_of,
                kbar_cache=cache,
                full_rrg_cache=snapshot_full_rrg_cache,
            )
            entry_shortlist = rerank_shortlist_snapshot_1300(
                entry_shortlist,
                conn=conn,
                close=close,
                bench=bench,
                trade_date=as_of,
                kbar_cache=cache,
                full_rrg_cache=snapshot_full_rrg_cache,
            )
        swaps_today = 0

        for pos in list(slots):
            hold_days = _trading_days_between(full_dates, str(pos["entry_date"]), as_of)
            if hold_days < config.max_hold_days:
                continue
            if (
                wd_gate_active
                and weekday_gate is not None
                and should_defer_max_hold_default_to_friday(weekday_gate, as_of)
            ):
                continue
            if (
                wd_gate_active
                and weekday_gate is not None
                and should_defer_exit_to_friday(weekday_gate, as_of, full_dates)
            ):
                continue
            if config.exit_loop_mode in ("intraday_loop_full", "intraday_loop_phase3") and config.timing_mode == "poll_5m":
                from research.backtest.c18acc_intraday_exit_loop import poll_px_last

                px, minute = poll_px_last(
                    conn,
                    close=close,
                    stock_id=str(pos["stock_id"]),
                    trade_date=as_of,
                    config=config,
                    kbar_cache=cache,
                )
            else:
                px, minute = _swap_px(
                    conn,
                    close=close,
                    stock_id=str(pos["stock_id"]),
                    trade_date=as_of,
                    config=config,
                    kbar_cache=cache,
                )
            if px is None:
                continue
            if minute:
                pos["exit_minute"] = minute
            leg = _settle(pos, exit_date=as_of, exit_px=px, exit_reason="max_hold")
            if leg:
                leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                periods.append(leg)
                slots.remove(pos)
                max_hold_exits += 1

        zone_ok = zone_filter is None or zone_by_date.get(as_of) == zone_filter
        if not zone_ok:
            continue

        swap_allowed = _breadth_zone_ok(signal_zone, config.breadth_swap_zones)
        held_accel_bypass: set[str] = set()
        if (
            day_rs_ratio is not None
            and day_rs_mom is not None
            and (config.quad_weakening_sell_days is not None or config.quad_lagging_sell_days is not None)
        ):
            for pos in slots:
                sid = str(pos["stock_id"])
                if _quad_sell_bypass_accel(
                    config,
                    rs_ratio=day_rs_ratio,
                    rs_mom=day_rs_mom,
                    full_dates=full_dates,
                    as_of=as_of,
                    stock_id=sid,
                    entry_date=str(pos["entry_date"]),
                ):
                    held_accel_bypass.add(sid)
        if config.accel_sell_bypass_on_spread_lost and day_spread_rs is not None:
            for pos in slots:
                sid = str(pos["stock_id"])
                if not spread_rs_positive_at(day_spread_rs, as_of=as_of, stock_id=sid):
                    held_accel_bypass.add(sid)
        skip_swap_today = (
            wd_gate_active
            and weekday_gate is not None
            and not swap_allowed_today(weekday_gate, as_of)
        )
        day_swap_margin = (
            swap_margin_for_day(weekday_gate, config.effective_margin, as_of)
            if wd_gate_active and weekday_gate is not None
            else config.effective_margin
        )
        day_max_swaps = (
            max_swaps_for_day(weekday_gate, config.max_swaps_per_day, as_of)
            if wd_gate_active and weekday_gate is not None
            else config.max_swaps_per_day
        )
        crowd_block = False
        if config.swap_min_challenger_n is not None and len(slots) >= n_slots:
            held0 = {str(p["stock_id"]) for p in slots}
            n_ch = sum(1 for r in swap_shortlist if r.stock_id not in held0)
            crowd_block = n_ch < int(config.swap_min_challenger_n)
        if skip_swap_today and len(slots) >= n_slots and swap_allowed:
            probe_sell, probe_buy = _pick_swap_pair(
                slots,
                swap_shortlist,
                held_ids={str(p["stock_id"]) for p in slots},
                config=config,
                held_today=held_today,
                held_trend=held_trend,
                challenger_trend=challenger_trend,
                challenger_va_dot=challenger_va_dot,
                challenger_avg_accel=challenger_avg_accel,
                held_accel_bypass=held_accel_bypass,
                as_of=as_of,
                intraday_extension_by_date=intraday_extension_by_date,
                challenger_rank_bonus_by_sid=(
                    challenger_rank_bonus_by_date.get(as_of) if challenger_rank_bonus_by_date else None
                ),
                rs_ratio=day_rs_ratio,
                rs_mom=day_rs_mom,
                full_dates=full_dates,
                swap_margin=day_swap_margin,
            )
            if probe_sell is not None and probe_buy is not None:
                swaps_deferred += 1
                swap_deferred_this_week += 1
        while swaps_today < day_max_swaps and not skip_swap_today and not crowd_block:
            held = {str(p["stock_id"]) for p in slots}
            if len(slots) < n_slots:
                break
            sell_px: float | None = None
            sell_min: str | None = None
            buy_px: float | None = None
            buy_min: str | None = None
            sell: dict[str, Any] | None = None
            buy: Any = None

            if config.exit_loop_mode == "intraday_loop_phase3" and config.timing_mode == "poll_5m":
                from research.backtest.c18acc_intraday_exit_loop import try_intraday_swap_repick

                sell, buy, sell_px, sell_min, buy_px, buy_min = try_intraday_swap_repick(
                    conn,
                    slots=slots,
                    swap_shortlist=swap_shortlist,
                    held_ids=held,
                    close=close,
                    as_of=as_of,
                    config=config,
                    kbar_cache=cache,
                    held_accel_bypass=held_accel_bypass,
                    full_dates=full_dates,
                )
                if sell is None or buy is None or sell_px is None or buy_px is None:
                    break
            else:
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
                    held_accel_bypass=held_accel_bypass,
                    as_of=as_of,
                    intraday_extension_by_date=intraday_extension_by_date,
                    challenger_rank_bonus_by_sid=(
                        challenger_rank_bonus_by_date.get(as_of) if challenger_rank_bonus_by_date else None
                    ),
                    rs_ratio=day_rs_ratio,
                    rs_mom=day_rs_mom,
                    full_dates=full_dates,
                    swap_margin=day_swap_margin,
                )
                if sell is None or buy is None:
                    swap_pair_streak.clear()
                    break
                if (
                    config.intraday_swap_confirm
                    and not config.intraday_swap_tiebreak
                    and not intraday_swap_confirm_ok(
                        intraday_extension_by_date,
                        as_of=as_of,
                        stock_id=buy.stock_id,
                    )
                ):
                    intraday_swap_blocked += 1
                    break
                if not _swap_allowed_for_leg(as_of, sell, zone_by_date, config):
                    break
                hold_days = _trading_days_between(full_dates, str(sell["entry_date"]), as_of)
                eff_min_hold = _effective_min_hold_days(config, close, full_dates, sell, as_of)
                if hold_days < eff_min_hold:
                    break

                if config.exit_loop_mode in ("intraday_loop_full", "intraday_loop_phase3") and config.timing_mode == "poll_5m":
                    from research.backtest.c18acc_intraday_exit_loop import try_intraday_score_swap_px

                    sell_px, sell_min, buy_px, buy_min = try_intraday_score_swap_px(
                        conn,
                        sell=sell,
                        buy=buy,
                        close=close,
                        as_of=as_of,
                        config=config,
                        kbar_cache=cache,
                    )
                    if sell_px is None or buy_px is None:
                        sell_px, sell_min = _swap_px(
                            conn,
                            close=close,
                            stock_id=str(sell["stock_id"]),
                            trade_date=as_of,
                            config=config,
                            kbar_cache=cache,
                        )
                        buy_px, buy_min = _swap_px(
                            conn,
                            close=close,
                            stock_id=buy.stock_id,
                            trade_date=as_of,
                            config=config,
                            kbar_cache=cache,
                        )
                else:
                    sell_px, sell_min = _swap_px(
                        conn,
                        close=close,
                        stock_id=str(sell["stock_id"]),
                        trade_date=as_of,
                        config=config,
                        kbar_cache=cache,
                    )
                    buy_px, buy_min = _swap_px(
                        conn,
                        close=close,
                        stock_id=buy.stock_id,
                        trade_date=as_of,
                        config=config,
                        kbar_cache=cache,
                    )
                if sell_px is None or buy_px is None:
                    break

            if (
                config.intraday_swap_confirm
                and not config.intraday_swap_tiebreak
                and buy is not None
                and not intraday_swap_confirm_ok(
                    intraday_extension_by_date,
                    as_of=as_of,
                    stock_id=buy.stock_id,
                )
            ):
                intraday_swap_blocked += 1
                break
            if sell is None or buy is None:
                swap_pair_streak.clear()
                break
            if not _swap_allowed_for_leg(as_of, sell, zone_by_date, config):
                break
            hold_days = _trading_days_between(full_dates, str(sell["entry_date"]), as_of)
            eff_min_hold = _effective_min_hold_days(config, close, full_dates, sell, as_of)
            if hold_days < eff_min_hold:
                break
            if sell_px is None or buy_px is None:
                break
            if swap_confirm_need > 1:
                pair = (str(sell["stock_id"]), str(buy.stock_id))
                for k in list(swap_pair_streak.keys()):
                    if k != pair:
                        del swap_pair_streak[k]
                swap_pair_streak[pair] = int(swap_pair_streak.get(pair, 0)) + 1
                if swap_pair_streak[pair] < swap_confirm_need:
                    break
                swap_pair_streak.clear()
            if sell_min:
                sell["exit_minute"] = sell_min

            leg = _settle(sell, exit_date=as_of, exit_px=sell_px, exit_reason="score_swap")
            if leg is None:
                break
            leg["breadth_zone_200"] = zone_by_date.get(str(sell.get("signal_date")), "unknown")
            leg["challenger_id"] = buy.stock_id
            leg["challenger_seg_last"] = buy.seg_last
            if config.gain_waiver_threshold is not None and hold_days < config.min_hold_days:
                leg["gain_waiver_swap"] = True
                early_waiver_swaps += 1
                early_waiver_excess.append(float(leg["excess_pct"]))
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
                    "rs_ratio": float(buy.rs_ratio),
                    "rs_momentum": float(buy.rs_momentum),
                    "mom_minus_ratio": round(_mom_minus_ratio_row(buy), 4),
                    "seg_step_delta": _seg_step_delta(buy.segs),
                    "entry_minute": buy_min,
                    "entry_leg": config.entry_leg,
                }
            )
            total_entries += 1
            swaps += 1
            swaps_today += 1
            if portfolio_book is not None:
                _portfolio_tag_new_entries(portfolio_book, slots)

        if (
            config.quad_force_exit_mode != "none"
            and day_rs_ratio is not None
            and day_rs_mom is not None
        ):
            for pos in list(slots):
                if swaps_today >= day_max_swaps:
                    break
                hold_days = _trading_days_between(full_dates, str(pos["entry_date"]), as_of)
                eff_min_hold = _effective_min_hold_days(config, close, full_dates, pos, as_of)
                if quad_force_exit_paused_by_positive_spread(
                    config,
                    spread_rs_panel=day_spread_rs,
                    as_of=as_of,
                    stock_id=str(pos["stock_id"]),
                    full_dates=full_dates,
                ):
                    quad_force_exit_paused += 1
                    continue
                if quad_force_exit_paused_by_hybrid_mom(
                    config,
                    hybrid_mom_panel=hybrid_mom_panel,
                    as_of=as_of,
                    stock_id=str(pos["stock_id"]),
                ):
                    quad_force_exit_paused += 1
                    continue
                sell_px: float | None = None
                sell_min: str | None = None
                if config.exit_loop_mode in ("intraday_loop", "intraday_loop_full", "intraday_loop_phase3") and config.timing_mode == "poll_5m":
                    from research.backtest.c18acc_intraday_exit_loop import (
                        try_intraday_quad_force_exit,
                    )

                    sell_px, sell_min, streak = try_intraday_quad_force_exit(
                        conn,
                        close=close,
                        bench=bench,
                        full_dates=full_dates,
                        rs_ratio=day_rs_ratio,
                        rs_mom=day_rs_mom,
                        spread_rs_panel=day_spread_rs,
                        pos=pos,
                        as_of=as_of,
                        config=config,
                        kbar_cache=cache,
                    )
                    if sell_px is None:
                        continue
                else:
                    streak = quad_force_exit_streak_days(
                        day_rs_ratio,
                        day_rs_mom,
                        full_dates,
                        as_of=as_of,
                        stock_id=str(pos["stock_id"]),
                        entry_date=str(pos["entry_date"]),
                        mode=config.quad_force_exit_mode,
                    )
                    unrealized_ret: float | None = None
                    if config.quad_force_exit_loss_pct is not None:
                        entry_px = float(pos["entry_px"])
                        if entry_px > 0 and as_of in close.index and str(pos["stock_id"]) in close.columns:
                            asof_close = float(close.at[as_of, str(pos["stock_id"])])
                            if asof_close > 0:
                                unrealized_ret = asof_close / entry_px - 1.0
                    if not quad_force_exit_eligible(
                        config,
                        hold_days=hold_days,
                        effective_min_hold=eff_min_hold,
                        streak_days=streak,
                        unrealized_ret=unrealized_ret,
                    ):
                        continue
                    if (
                        wd_gate_active
                        and weekday_gate is not None
                        and should_defer_exit_to_friday(weekday_gate, as_of, full_dates)
                    ):
                        continue
                    sell_px, sell_min = _swap_px(
                        conn,
                        close=close,
                        stock_id=str(pos["stock_id"]),
                        trade_date=as_of,
                        config=config,
                        kbar_cache=cache,
                    )
                    if sell_px is None:
                        continue
                if sell_min:
                    pos["exit_minute"] = sell_min
                leg = _settle(
                    pos,
                    exit_date=as_of,
                    exit_px=sell_px,
                    exit_reason="quad_force_exit",
                )
                if leg is None:
                    continue
                leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                leg["quad_force_exit_streak"] = streak
                periods.append(leg)
                slots.remove(pos)
                force_exits += 1
                swaps_today += 1

        if (
            config.struct_force_exit_mode != "none"
            and day_rs_ratio is not None
            and (config.struct_force_exit_mode != "spread_lost" or day_spread_rs is not None)
        ):
            for pos in list(slots):
                if swaps_today >= day_max_swaps:
                    break
                hold_days = _trading_days_between(full_dates, str(pos["entry_date"]), as_of)
                eff_min_hold = _effective_min_hold_days(config, close, full_dates, pos, as_of)
                streak = struct_force_exit_streak_days(
                    config,
                    rs_ratio=day_rs_ratio,
                    spread_rs_panel=day_spread_rs,
                    full_dates=full_dates,
                    as_of=as_of,
                    stock_id=str(pos["stock_id"]),
                    entry_date=str(pos["entry_date"]),
                )
                unrealized_ret: float | None = None
                if config.struct_force_exit_loss_pct is not None:
                    entry_px = float(pos["entry_px"])
                    if entry_px > 0 and as_of in close.index and str(pos["stock_id"]) in close.columns:
                        asof_close = float(close.at[as_of, str(pos["stock_id"])])
                        if asof_close > 0:
                            unrealized_ret = asof_close / entry_px - 1.0
                if not struct_force_exit_eligible(
                    config,
                    hold_days=hold_days,
                    effective_min_hold=eff_min_hold,
                    streak_days=streak,
                    unrealized_ret=unrealized_ret,
                ):
                    continue
                sell_px, sell_min = _swap_px(
                    conn,
                    close=close,
                    stock_id=str(pos["stock_id"]),
                    trade_date=as_of,
                    config=config,
                    kbar_cache=cache,
                )
                if sell_px is None:
                    continue
                if sell_min:
                    pos["exit_minute"] = sell_min
                leg = _settle(
                    pos,
                    exit_date=as_of,
                    exit_px=sell_px,
                    exit_reason="struct_force_exit",
                )
                if leg is None:
                    continue
                leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                leg["struct_force_exit_streak"] = streak
                leg["struct_force_exit_mode"] = config.struct_force_exit_mode
                periods.append(leg)
                slots.remove(pos)
                force_exits += 1
                swaps_today += 1

        if config.hard_stop_loss_pct is not None:
            for pos in list(slots):
                if swaps_today >= day_max_swaps:
                    break
                hold_days = _trading_days_between(full_dates, str(pos["entry_date"]), as_of)
                eff_min_hold = _effective_min_hold_days(config, close, full_dates, pos, as_of)
                entry_px = float(pos["entry_px"])
                sell_px: float | None = None
                exit_minute: str | None = None
                exit_reason = "hard_stop"

                if config.hard_stop_intraday:
                    if config.hard_stop_requires_min_hold and hold_days < eff_min_hold:
                        continue
                    hit_px, hit_min = intraday_hard_stop_trigger(
                        conn,
                        stock_id=str(pos["stock_id"]),
                        trade_date=as_of,
                        entry_px=entry_px,
                        entry_date=str(pos["entry_date"]),
                        entry_minute=pos.get("entry_minute"),
                        stop_pct=config.hard_stop_loss_pct,
                        bars_cache=bars_cache,
                    )
                    if hit_px is None:
                        continue
                    sell_px = hit_px
                    exit_minute = hit_min
                    exit_reason = "hard_stop_intraday"
                else:
                    unrealized_ret: float | None = None
                    if entry_px > 0 and as_of in close.index and str(pos["stock_id"]) in close.columns:
                        asof_close = float(close.at[as_of, str(pos["stock_id"])])
                        if asof_close > 0:
                            unrealized_ret = asof_close / entry_px - 1.0
                    if not hard_stop_exit_eligible(
                        config,
                        hold_days=hold_days,
                        effective_min_hold=eff_min_hold,
                        unrealized_ret=unrealized_ret,
                    ):
                        continue
                    sell_px, exit_minute = _swap_px(
                        conn,
                        close=close,
                        stock_id=str(pos["stock_id"]),
                        trade_date=as_of,
                        config=config,
                        kbar_cache=cache,
                    )
                if sell_px is None:
                    continue
                leg = _settle(
                    pos,
                    exit_date=as_of,
                    exit_px=sell_px,
                    exit_reason=exit_reason,
                )
                if leg is None:
                    continue
                if exit_minute:
                    leg["exit_minute"] = exit_minute
                leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                periods.append(leg)
                slots.remove(pos)
                hard_stop_exits += 1
                force_exits += 1
                swaps_today += 1

        if swap_block_audit is not None and day_rs_ratio is not None and day_rs_mom is not None:
            from research.backtest.c18acc_swap_block_audit import record_swap_block_audit_day

            record_swap_block_audit_day(
                swap_block_audit,
                as_of=as_of,
                slots=slots,
                swap_shortlist=swap_shortlist,
                config=config,
                close=close,
                full_dates=full_dates,
                rs_ratio=day_rs_ratio,
                rs_mom=day_rs_mom,
                held_today=held_today,
                held_accel_bypass=held_accel_bypass,
                swaps_today=swaps_today,
                swap_zone_ok=swap_allowed,
            )

        if _breadth_zone_ok(signal_zone, config.breadth_entry_zones):
            entry_zone_days += 1
            fill_shortlist = entry_shortlist
            free_k = n_slots - len(slots)
            _used_tier2 = False
            if config.entry_fallback_pool is not None and free_k > 0:
                # 部分補滿：fresh 不足 free_k 時，從 fallback 池補足（partial supplement）
                held_ids_temp = {str(p["stock_id"]) for p in slots}
                fresh_avail = sum(1 for r in fill_shortlist if r.stock_id not in held_ids_temp)
                if fresh_avail < free_k:
                    fallback_rows = _candidate_pool(
                        as_of,
                        pool_type=config.entry_fallback_pool,
                        **pool_kwargs,
                    )
                    fallback_shortlist = _candidate_shortlist(
                        fallback_rows,
                        config,
                        challenger_trend=challenger_trend,
                        challenger_va_dot=challenger_va_dot,
                        challenger_avg_accel=challenger_avg_accel,
                    )
                    fallback_shortlist = apply_gate_by_date(
                        fallback_shortlist, as_of, entry_gate_by_date
                    )
                    fallback_shortlist = filter_shortlist_by_prior_ret(
                        fallback_shortlist,
                        close=close,
                        full_dates=full_dates,
                        as_of=as_of,
                        config=config,
                    )
                    fresh_ids = {r.stock_id for r in fill_shortlist}
                    supplement = [r for r in fallback_shortlist if r.stock_id not in fresh_ids]
                    if supplement:
                        fill_shortlist = list(fill_shortlist) + supplement
                        _used_tier2 = True
                        tier2_supplement_days += 1
            if free_k > 0 and fill_shortlist and not _used_tier2:
                fresh_fill_days += 1
            fill_pool = fill_shortlist
            if config.fill_mode == "batch_topk" and free_k > 0 and fill_shortlist:
                held_ids = {str(p["stock_id"]) for p in slots}
                ranked = _rank_rows_for_fill(
                    fill_shortlist,
                    config,
                    challenger_va_dot=challenger_va_dot,
                    challenger_avg_accel=challenger_avg_accel,
                )
                if config.fill_repeat and ranked:
                    # 加碼補滿：不限制重複，循環取 top-k 直到填滿所有空槽
                    fill_pool = [ranked[i % len(ranked)] for i in range(free_k)]
                else:
                    fill_pool = [r for r in ranked if r.stock_id not in held_ids][:free_k]
                    if free_k > len(fill_pool):
                        pool_shortfall_days += 1

            if (
                wd_gate_active
                and weekday_gate is not None
                and weekday_gate.entry_mode not in ("baseline", "E0")
            ):
                pending_entries, exp_n, ready = flush_pending_entries(
                    pending_entries,
                    gate=weekday_gate,
                    as_of=as_of,
                    full_dates=full_dates,
                )
                signals_skipped_expired += exp_n
                held_pending = {str(p["stock_id"]) for p in slots}
                for pe in ready:
                    if len(slots) >= n_slots:
                        break
                    row = pending_row_to_scan_row(pe["row"])
                    if row.stock_id in held_pending:
                        continue
                    slots_before_pe = len(slots)
                    _fill_empty_slots(
                        conn,
                        as_of=as_of,
                        fresh_mono=[row],
                        slots=slots,
                        close=close,
                        bench=bench,
                        full_dates=full_dates,
                        config=fill_cfg,
                        kbar_cache=cache,
                        kbar_stats=kbar_stats,
                        entry_c_config=entry_c_config,
                        n_slots=n_slots,
                        rs_ratio=day_rs_ratio,
                        rs_mom=day_rs_mom,
                        accel_lookback=int(config.accel_lookback),
                    )
                    for pos in slots[slots_before_pe:]:
                        pos["signal_date"] = pe["signal_date"]
                        held_pending.add(str(pos["stock_id"]))
                    total_entries += len(slots) - slots_before_pe

                entry_target = entry_target_date(weekday_gate, as_of, full_dates)
                if free_k > 0 and fill_pool and entry_target != as_of:
                    deferred_n, _ = queue_entry_candidates(
                        pending_entries,
                        gate=weekday_gate,
                        signal_date=as_of,
                        full_dates=full_dates,
                        rows=fill_pool,
                        max_n=free_k,
                    )
                    signals_deferred += deferred_n
                    fill_pool = []

            if config.timing_mode == "snapshot_1300" and fill_pool:
                from research.backtest.c18acc_snapshot_1300 import fill_empty_slots_snapshot_1300

                added = fill_empty_slots_snapshot_1300(
                    conn,
                    as_of=as_of,
                    rows=fill_pool if not config.fill_repeat else fill_pool[:free_k],
                    slots=slots,
                    close=close,
                    bench=bench,
                    kbar_cache=cache,
                    n_slots=n_slots,
                    entry_leg=config.entry_leg,
                    full_rrg_cache=snapshot_full_rrg_cache,
                )
                total_entries += added
            elif config.fill_repeat and fill_pool:
                # 直接寫入 slots，允許同一 stock_id 佔多個槽位
                slot_used = {int(p["slot"]) for p in slots}
                free_slot_ids = [i for i in range(n_slots) if i not in slot_used]
                _had_repeat = False
                _newly_added: set[str] = set()
                for slot_id, row in zip(free_slot_ids, fill_pool):
                    px = (
                        float(close.at[as_of, row.stock_id])
                        if as_of in close.index and row.stock_id in close.columns
                        else None
                    )
                    if px is None or px <= 0:
                        continue
                    if row.stock_id in _newly_added:
                        _had_repeat = True  # 本日已為此股補過一槽，再補屬加碼
                    _newly_added.add(row.stock_id)
                    slots.append(
                        {
                            "slot": slot_id,
                            "stock_id": row.stock_id,
                            "stock_name": row.stock_name,
                            "signal_date": as_of,
                            "entry_date": as_of,
                            "entry_px": px,
                            "seg_last": round(row.seg_last, 4),
                            "disp": round(row.disp, 4),
                            "rs_ratio": float(row.rs_ratio),
                            "rs_momentum": float(row.rs_momentum),
                            "mom_minus_ratio": round(_mom_minus_ratio_row(row), 4),
                            "seg_step_delta": _seg_step_delta(row.segs),
                            "entry_minute": str(config.no_trade_before or "09:30"),
                            "entry_leg": config.entry_leg,
                        }
                    )
                    total_entries += 1
                if _had_repeat:
                    fill_repeat_days += 1
                if portfolio_book is not None:
                    _portfolio_tag_new_entries(portfolio_book, slots)
            else:
                slots_before = len(slots)
                _fill_empty_slots(
                    conn,
                    as_of=as_of,
                    fresh_mono=fill_pool,
                    slots=slots,
                    close=close,
                    bench=bench,
                    full_dates=full_dates,
                    config=fill_cfg,
                    kbar_cache=cache,
                    kbar_stats=kbar_stats,
                    entry_c_config=entry_c_config,
                    n_slots=n_slots,
                    rs_ratio=day_rs_ratio,
                    rs_mom=day_rs_mom,
                    accel_lookback=int(config.accel_lookback),
                )
                total_entries += len(slots) - slots_before
            if portfolio_book is not None:
                _portfolio_tag_new_entries(portfolio_book, slots)
            if len(slots) >= n_slots:
                slot_util_days += 1
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
        if (
            wd_gate_active
            and weekday_gate is not None
            and weekday_gate.exit_mode == "X3"
        ):
            for pos in list(slots):
                hold_days = _trading_days_between(full_dates, str(pos["entry_date"]), as_of)
                eff_min_hold = _effective_min_hold_days(config, close, full_dates, pos, as_of)
                if not should_anti_weekend_force_exit(
                    weekday_gate,
                    as_of,
                    hold_days=hold_days,
                    eff_min_hold=eff_min_hold,
                ):
                    continue
                sell_px, sell_min = _swap_px(
                    conn,
                    close=close,
                    stock_id=str(pos["stock_id"]),
                    trade_date=as_of,
                    config=config,
                    kbar_cache=cache,
                )
                if sell_px is None:
                    continue
                if sell_min:
                    pos["exit_minute"] = sell_min
                leg = _settle(
                    pos,
                    exit_date=as_of,
                    exit_px=sell_px,
                    exit_reason="weekday_anti_weekend",
                )
                if leg is None:
                    continue
                leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                periods.append(leg)
                slots.remove(pos)
                force_exits += 1
        if slot_snapshots is not None:
            slot_snapshots[as_of] = [dict(p) for p in slots]

    if trade_dates:
        last = trade_dates[-1]
        for pos in list(slots):
            _update_gain_waiver_stats(pos)
            px = _entry_px(close, str(pos["stock_id"]), last)
            if px is None:
                continue
            leg = _settle(pos, exit_date=last, exit_px=px, exit_reason="window_end")
            if leg:
                leg["breadth_zone_200"] = zone_by_date.get(str(pos.get("signal_date")), "unknown")
                periods.append(leg)

    summary = summarize_periods(periods)
    n = len(periods)
    if n:
        summary["mean_excess_pct"] = _finite_mean_excess_pct(periods)
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
            "force_exits": force_exits,
            "hard_stop_exits": hard_stop_exits,
            "portfolio_loss_cap_pct": config.portfolio_loss_cap_pct,
            "portfolio_loss_cap_triggers": (
                int(portfolio_book.get("cap_triggers") or 0) if portfolio_book else 0
            ),
            "portfolio_halted": bool(portfolio_book.get("halted")) if portfolio_book else False,
            "quad_force_exit_paused": quad_force_exit_paused,
            "intraday_swap_blocked": intraday_swap_blocked,
            "quad_force_exit_pause_spread_rs_positive": config.quad_force_exit_pause_spread_rs_positive,
            "quad_force_exit_pause_spread_rs_streak_min": config.quad_force_exit_pause_spread_rs_streak_min,
            "entry_gate_mode": config.entry_gate_mode,
            "entry_prior_ret_days": config.entry_prior_ret_days,
            "entry_prior_ret_max": config.entry_prior_ret_max,
            "intraday_swap_confirm": config.intraday_swap_confirm,
            "intraday_swap_tiebreak": config.intraday_swap_tiebreak,
            "challenger_rank_bonus": config.challenger_rank_bonus,
            "challenger_rank_bonus_weight": config.challenger_rank_bonus_weight,
            "challenger_rank_bonus_margin_only": config.challenger_rank_bonus_margin_only,
            "quad_force_exit_mode": config.quad_force_exit_mode,
            "quad_force_exit_min_days": config.quad_force_exit_min_days,
            "quad_force_exit_loss_pct": config.quad_force_exit_loss_pct,
            "max_hold_exits": max_hold_exits,
            "n_periods": n,
            "n_slots": n_slots,
            "fill_mode": config.fill_mode,
            "slot_util_pct": round(100.0 * slot_util_days / entry_zone_days, 2) if entry_zone_days else None,
            "pool_shortfall_days": pool_shortfall_days,
            "fresh_fill_days": fresh_fill_days,
            "tier2_supplement_days": tier2_supplement_days,
            "tier2_supplement_pct": round(100.0 * tier2_supplement_days / entry_zone_days, 2) if entry_zone_days else None,
            "fill_repeat_days": fill_repeat_days,
            "fill_repeat": config.fill_repeat,
            "entry_zone_days": entry_zone_days,
            "gain_waiver_threshold": config.gain_waiver_threshold,
            "gain_waiver_min_hold": config.gain_waiver_min_hold,
            "total_entries": total_entries,
            "day1_observed_legs": day1_observed_legs,
            "waiver_eligible_legs": waiver_eligible_legs,
            "waiver_rate_pct": (
                round(100.0 * waiver_eligible_legs / day1_observed_legs, 2) if day1_observed_legs else None
            ),
            "early_waiver_swaps": early_waiver_swaps,
            "early_waiver_mean_excess_pct": (
                round(sum(early_waiver_excess) / len(early_waiver_excess), 4) if early_waiver_excess else None
            ),
            "weekday_gate": weekday_gate.to_dict() if weekday_gate is not None else None,
            "signals_skipped_expired": signals_skipped_expired if wd_gate_active else 0,
            "signals_deferred": signals_deferred if wd_gate_active else 0,
            "swaps_deferred": swaps_deferred if wd_gate_active else 0,
            "swaps_lost": swaps_lost if wd_gate_active else 0,
            "snapshot_days_skipped": snapshot_days_skipped,
        }
    )
    if wd_gate_active and weekday_gate is not None and weekday_gate.swap_mode == "S2" and swap_deferred_this_week > 0:
        summary["swaps_lost"] = int(summary.get("swaps_lost") or 0) + swap_deferred_this_week
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


def _timeline_bundle_from_period(
    period: dict[str, Any],
    *,
    capital_ntd: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map one settled score-swap period → timeline leg + executed row."""
    slot_id = int(period.get("slot") or 0)
    entry = str(period["entry_date"])
    exit_d = str(period["exit_date"])
    sid = str(period["stock_id"])
    ret = float(period["return_pct"])
    excess = period.get("excess_pct")
    pnl = capital_ntd * ret / 100.0
    alpha_ntd = capital_ntd * float(excess) / 100.0 if excess is not None else None
    leg = {
        "leg_id": f"{entry}|{sid}|{slot_id}",
        "signal_date": str(period.get("signal_date") or entry),
        "stock_id": sid,
        "stock_name": str(period.get("stock_name") or ""),
        "action": str(period.get("exit_reason") or "c18acc"),
        "entry_date": entry,
        "exit_date": exit_d,
        "entry_px": period.get("entry_px"),
        "exit_px": period.get("exit_px"),
        "allocated_ntd": capital_ntd,
        "return_pct": ret,
        "pnl_ntd": pnl,
        "slot_id": slot_id,
        "seg_last": period.get("seg_last"),
        "exit_reason": period.get("exit_reason"),
    }
    for key in (
        "entry_minute",
        "exit_minute",
        "orig_exit_date",
        "orig_exit_px",
        "orig_return_pct",
        "save_vs_orig_pct",
        "ext_exit",
        "overlay_variant",
    ):
        if key in period:
            leg[key] = period[key]
    executed = {
        "signal_date": str(period.get("signal_date") or entry),
        "entry_date": entry,
        "exit_date": exit_d,
        "slot_id": slot_id,
        "n_legs": 1,
        "stock_id": sid,
        "stock_name": str(period.get("stock_name") or ""),
        "seg_last": period.get("seg_last"),
        "entry_px": period.get("entry_px"),
        "exit_px": period.get("exit_px"),
        "entry_minute": period.get("entry_minute"),
        "exit_minute": period.get("exit_minute"),
        "deployed_ntd": capital_ntd,
        "pnl_ntd": pnl,
        "return_pct": ret,
        "bench_return_pct": period.get("bench_return_pct"),
        "alpha_ntd": alpha_ntd,
        "exit_reason": period.get("exit_reason"),
    }
    return leg, executed


def build_c18acc_executed_legs_for_timeline(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
    config: ScoreSwapCConfig | None = None,
    use_close_timing: bool = True,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """C18acc score-swap periods → legs compatible with render_l1h9_slots_timeline_html."""
    from dataclasses import replace

    from market_breadth_ma import build_breadth_panel
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar

    if not dates:
        raise ValueError("dates must not be empty")

    base = close_champion_score_swap_c_config() if use_close_timing else champion_score_swap_c_config()
    cfg = replace(base, n_slots=max(1, int(n_slots)))

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in dates if d in set(full_dates)]
    if len(trade_dates) < 2:
        raise ValueError(f"need ≥2 trade dates in panel, got {len(trade_dates)}")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    slot_snapshots: dict[str, list[dict[str, Any]]] = {}

    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        slot_snapshots=slot_snapshots,
    )

    legs_out: list[dict] = []
    executed_signals: list[dict] = []
    for period in periods:
        leg, executed = _timeline_bundle_from_period(period, capital_ntd=capital_ntd)
        legs_out.append(leg)
        executed_signals.append(executed)

    peak = max((len(v) for v in slot_snapshots.values()), default=0)
    n_executed = len(executed_signals)
    total_capital = cfg.n_slots * capital_ntd
    meta = {
        "n_slots": cfg.n_slots,
        "capital_ntd": capital_ntd,
        "total_capital_ntd": total_capital,
        "cost_bps": 0.0,
        "hold_trading_days": cfg.max_hold_days,
        "min_hold_days": cfg.min_hold_days,
        "n_signals": n_executed,
        "n_executed": n_executed,
        "n_skipped": 0,
        "signal_capture_pct": None,
        "peak_concurrent_slots": peak,
        "strategy_id": RRG_MONO_SWAP_ACCEL_SLUG,
        "strategy_title": f"C18acc · {cfg.n_slots}槽 · swap-accel",
        "strategy_rule": (
            f"min_hold {cfg.min_hold_days} / max_hold {cfg.max_hold_days} · "
            f"margin {cfg.effective_margin:g} · "
            f"{'close' if use_close_timing else cfg.timing_mode} 換倉"
        ),
        "strategy_filter": "fresh · 四日加速换仓 · score_swap_c",
        "display_code": RRG_MONO_SWAP_ACCEL_SHORT,
        "table_mode": "mono",
        "entry_price_mode": "close",
        "swaps_total": summary.get("swaps_total"),
        "variant_id": cfg.variant_id,
        "timing_mode": cfg.timing_mode,
    }
    return legs_out, executed_signals, [], meta


def _finalize_c18acc_s2_timeline_meta(
    meta: dict,
    summary: dict[str, Any],
    cfg: ScoreSwapCConfig,
) -> dict:
    meta = dict(meta)
    meta.update(
        {
            "strategy_title": f"C18acc+S2 · {cfg.n_slots}槽 · swap-accel+rotation-exit",
            "strategy_rule": (
                f"min_hold {cfg.min_hold_days} / max_hold {cfg.max_hold_days} · "
                f"S2 weakening {cfg.quad_force_exit_min_days}d + loss≥"
                f"{abs(cfg.quad_force_exit_loss_pct or 0) * 100:.0f}% · close"
            ),
            "strategy_filter": "fresh · 四日加速换仓 · score_swap_c · S2 rotation exit",
            "display_code": "C18acc+S2",
            "variant_id": "S2",
            "parent_variant_id": close_champion_score_swap_c_config().variant_id,
            "force_exits": summary.get("force_exits"),
            "mean_excess_pct": summary.get("mean_excess_pct"),
        }
    )
    return meta


def build_c18acc_s2_executed_legs_for_timeline(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
    use_close_timing: bool = True,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """C18acc + S2 selective quad force-exit → timeline legs."""
    from dataclasses import replace

    from market_breadth_ma import build_breadth_panel
    from research.backtest.c18acc_rotation_selective_exit_sweep import (
        rotation_selective_exit_sweep_configs,
    )
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar

    if not dates:
        raise ValueError("dates must not be empty")

    configs = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}
    s2_base = configs["S2"]
    if not use_close_timing:
        base = champion_score_swap_c_config()
        s2_base = replace(
            s2_base,
            timing_mode=base.timing_mode,
            entry_price_mode=base.entry_price_mode,
            variant_id="S2",
        )
    cfg = replace(s2_base, n_slots=max(1, int(n_slots)))

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in dates if d in set(full_dates)]
    if len(trade_dates) < 2:
        raise ValueError(f"need ≥2 trade dates in panel, got {len(trade_dates)}")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    slot_snapshots: dict[str, list[dict[str, Any]]] = {}

    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        slot_snapshots=slot_snapshots,
    )

    legs_out: list[dict] = []
    executed_signals: list[dict] = []
    for period in periods:
        period = dict(period)
        period["overlay_variant"] = "S2"
        leg, executed = _timeline_bundle_from_period(period, capital_ntd=capital_ntd)
        legs_out.append(leg)
        executed_signals.append(executed)

    peak = max((len(v) for v in slot_snapshots.values()), default=0)
    n_executed = len(executed_signals)
    total_capital = cfg.n_slots * capital_ntd
    meta = {
        "n_slots": cfg.n_slots,
        "capital_ntd": capital_ntd,
        "total_capital_ntd": total_capital,
        "cost_bps": 0.0,
        "hold_trading_days": cfg.max_hold_days,
        "min_hold_days": cfg.min_hold_days,
        "n_signals": n_executed,
        "n_executed": n_executed,
        "n_skipped": 0,
        "signal_capture_pct": None,
        "peak_concurrent_slots": peak,
        "strategy_id": RRG_MONO_SWAP_ACCEL_SLUG,
        "table_mode": "mono",
        "entry_price_mode": "close",
        "swaps_total": summary.get("swaps_total"),
        "timing_mode": cfg.timing_mode,
    }
    meta = _finalize_c18acc_s2_timeline_meta(meta, summary, cfg)
    return legs_out, executed_signals, [], meta


def _finalize_c18acc_pool1_timeline_meta(
    meta: dict,
    summary: dict[str, Any],
    cfg: ScoreSwapCConfig,
    *,
    display_code: str = "POOL1-P5M",
) -> dict:
    meta = dict(meta)
    meta.update(
        {
            "strategy_title": f"POOL1 · fresh∪accel + S2 · {cfg.n_slots}槽 · poll_5m",
            "strategy_rule": (
                f"min_hold {cfg.min_hold_days} / max_hold {cfg.max_hold_days} · "
                f"S2 weakening {cfg.quad_force_exit_min_days}d + loss≥"
                f"{abs(cfg.quad_force_exit_loss_pct or 0) * 100:.0f}% · poll_5m"
            ),
            "strategy_filter": "fresh∪accel · 四日加速 · S2 rotation exit · poll_5m",
            "display_code": display_code,
            "variant_id": cfg.variant_id,
            "candidate_pool": cfg.candidate_pool,
            "parent_variant_id": "S2-P5M",
            "force_exits": summary.get("force_exits"),
            "mean_excess_pct": summary.get("mean_excess_pct"),
        }
    )
    return meta


def _pool_tag_for_signal(
    stock_id: str,
    signal_date: str,
    fresh_by_date: dict[str, list[ScanRow]],
) -> str:
    fresh_ids = {r.stock_id for r in fresh_by_date.get(signal_date, [])}
    return "fresh" if stock_id in fresh_ids else "accel-only"


def _poll5m_needs_spread_rs_panel(cfg: ScoreSwapCConfig) -> bool:
    return bool(
        cfg.accel_sell_bypass_on_spread_lost
        or cfg.quad_force_exit_pause_spread_rs_positive
        or int(cfg.quad_force_exit_pause_spread_rs_streak_min or 0) > 0
    )


def build_c18acc_poll5m_s2_timeline_legs(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    candidate_pool: CandidatePool = "fresh_union_accel",
    variant_id: str = "POOL1-P5M",
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
    accel_sell_bypass_on_spread_lost: bool | None = None,
    extra_config: dict[str, Any] | None = None,
    entry_gate_by_date: dict[str, set[str]] | None = None,
    swap_gate_by_date: dict[str, set[str]] | None = None,
    confirm_bars: int = 1,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """C18acc poll_5m · S2 exit · fresh or fresh_union_accel pool → timeline legs."""
    from dataclasses import replace

    from market_breadth_ma import build_breadth_panel
    from research.backtest.c18acc_rotation_selective_exit_sweep import (
        rotation_selective_exit_sweep_configs,
    )
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
    from research.backtest.rrg_mono_intraday_ab import champion_entry_c_config
    from research.backtest.rrg_mono_swap_exit_b import build_mono_tier2_calendar

    if not dates:
        raise ValueError("dates must not be empty")

    configs = {c.variant_id: c for c in rotation_selective_exit_sweep_configs()}
    s2_base = configs["S2"]
    champ = champion_score_swap_c_config()
    bypass = (
        champ.accel_sell_bypass_on_spread_lost
        if accel_sell_bypass_on_spread_lost is None
        else accel_sell_bypass_on_spread_lost
    )
    cfg = replace(
        s2_base,
        timing_mode=champ.timing_mode,
        no_trade_before=champ.no_trade_before,
        candidate_pool=candidate_pool,
        variant_id=variant_id,
        n_slots=max(1, int(n_slots)),
        sort_key=champ.sort_key,
        buy_sort_key=champ.buy_sort_key,
        score_margin=champ.score_margin,
        accel_sell_negative_only=champ.accel_sell_negative_only,
        accel_lookback=champ.accel_lookback,
        candidate_lookback=champ.candidate_lookback,
        min_hold_days=champ.min_hold_days,
        max_hold_days=champ.max_hold_days,
        accel_sell_bypass_on_spread_lost=bypass,
        # G_R5_12 · champion chase gate（进场／换仓买）
        entry_prior_ret_days=champ.entry_prior_ret_days,
        entry_prior_ret_max=champ.entry_prior_ret_max,
    )
    if extra_config:
        cfg = replace(cfg, **extra_config)
    # Entry SSOT: C3a avg_accel + no_swap_before 對齊 champion no_trade_before（現行 13:00）
    c0_cfg = replace(
        champion_entry_c_config(confirm_bars=int(confirm_bars)),
        no_swap_before=str(champ.no_trade_before or cfg.no_trade_before or "13:00"),
    )

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in dates if d in set(full_dates)]
    if len(trade_dates) < 2:
        raise ValueError(f"need ≥2 trade dates in panel, got {len(trade_dates)}")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict = {}
    slot_snapshots: dict[str, list[dict[str, Any]]] = {}
    spread_rs_panel = (
        build_daily_spread_rs_panel(close, bench)
        if _poll5m_needs_spread_rs_panel(cfg)
        else None
    )

    periods, summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        mono_by_date=mono_by_date,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        spread_rs_panel=spread_rs_panel,
        kbar_cache=kbar_cache,
        entry_c_config=c0_cfg,
        slot_snapshots=slot_snapshots,
        entry_gate_by_date=entry_gate_by_date,
        swap_gate_by_date=(
            swap_gate_by_date
            if swap_gate_by_date is not None
            else entry_gate_by_date
        ),
    )

    legs_out: list[dict] = []
    executed_signals: list[dict] = []
    run_variant_id = str(cfg.variant_id or variant_id)
    for period in periods:
        period = dict(period)
        period["overlay_variant"] = run_variant_id
        sig = str(period.get("signal_date") or period["entry_date"])
        pool_tag = _pool_tag_for_signal(str(period["stock_id"]), sig, fresh_by_date)
        leg, executed = _timeline_bundle_from_period(period, capital_ntd=capital_ntd)
        leg["pool_tag"] = pool_tag
        leg["showcase_variant"] = run_variant_id
        executed["pool_tag"] = pool_tag
        executed["exit_reason"] = period.get("exit_reason")
        legs_out.append(leg)
        executed_signals.append(executed)

    peak = max((len(v) for v in slot_snapshots.values()), default=0)
    n_executed = len(executed_signals)
    total_capital = cfg.n_slots * capital_ntd
    meta = {
        "n_slots": cfg.n_slots,
        "capital_ntd": capital_ntd,
        "total_capital_ntd": total_capital,
        "cost_bps": 0.0,
        "hold_trading_days": cfg.max_hold_days,
        "min_hold_days": cfg.min_hold_days,
        "n_signals": n_executed,
        "n_executed": n_executed,
        "n_skipped": 0,
        "signal_capture_pct": None,
        "peak_concurrent_slots": peak,
        "strategy_id": RRG_MONO_SWAP_ACCEL_SLUG,
        "table_mode": "mono",
        "entry_price_mode": "intraday_c0",
        "swaps_total": summary.get("swaps_total"),
        "timing_mode": cfg.timing_mode,
    }
    display_code = run_variant_id
    if cfg.candidate_pool == "fresh_union_accel" or run_variant_id.startswith("POOL1"):
        meta = _finalize_c18acc_pool1_timeline_meta(meta, summary, cfg, display_code=display_code)
    else:
        meta = _finalize_c18acc_s2_timeline_meta(meta, summary, cfg)
        meta["display_code"] = display_code
        meta["candidate_pool"] = candidate_pool
    meta["variant_id"] = run_variant_id
    meta["fill_repeat"] = cfg.fill_repeat
    meta["max_swaps_per_day"] = cfg.max_swaps_per_day
    meta["accel_sell_bypass_on_spread_lost"] = cfg.accel_sell_bypass_on_spread_lost
    meta["quad_force_exit_pause_spread_rs_positive"] = (
        cfg.quad_force_exit_pause_spread_rs_positive
    )
    if entry_gate_by_date is not None:
        meta["avoid_spread_mixed"] = True
    meta["confirm_bars"] = int(confirm_bars)
    meta["entry_variant_id"] = str(c0_cfg.variant_id)
    meta["no_trade_before"] = str(cfg.no_trade_before or "")
    meta["buy_sort_key"] = cfg.buy_sort_key
    meta["sort_key"] = cfg.sort_key
    meta["entry_sort_key"] = cfg.buy_sort_key or cfg.sort_key
    meta["entry_prior_ret_days"] = cfg.entry_prior_ret_days
    meta["entry_prior_ret_max"] = cfg.entry_prior_ret_max
    meta["candidate_pool"] = cfg.candidate_pool
    return legs_out, executed_signals, [], meta


def build_c18acc_i36_executed_legs_for_timeline(
    conn: sqlite3.Connection,
    dates: list[str],
    *,
    n_slots: int = 3,
    capital_ntd: float = 10_000.0,
    use_close_timing: bool = True,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """C18acc close 進場/換倉 + I36 combo_spike extension 提早出場 → timeline legs。"""
    from dataclasses import replace

    from market_breadth_ma import build_breadth_panel
    from research.backtest.c18acc_intraday_1m_hold import (
        _i36_live_config,
        apply_intraday_1m_hold,
    )
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar

    if not dates:
        raise ValueError("dates must not be empty")

    base = close_champion_score_swap_c_config() if use_close_timing else champion_score_swap_c_config()
    cfg = replace(base, n_slots=max(1, int(n_slots)))

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in dates if d in set(full_dates)]
    if len(trade_dates) < 2:
        raise ValueError(f"need ≥2 trade dates in panel, got {len(trade_dates)}")

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=trade_dates[0], date_end=trade_dates[-1])
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    slot_snapshots: dict[str, list[dict[str, Any]]] = {}

    base_periods, base_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=cfg,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        slot_snapshots=slot_snapshots,
    )

    i36_cfg = _i36_live_config(
        "I36",
        "combo_spike · spike≥4% · fade≥1% · 1m hybrid",
    )
    overlay_periods, overlay_summary = apply_intraday_1m_hold(
        conn,
        base_periods=base_periods,
        close=close,
        full_dates=full_dates,
        config=i36_cfg,
    )

    legs_out: list[dict] = []
    executed_signals: list[dict] = []
    for period in overlay_periods:
        period = dict(period)
        period["overlay_variant"] = "I36"
        leg, executed = _timeline_bundle_from_period(period, capital_ntd=capital_ntd)
        legs_out.append(leg)
        executed_signals.append(executed)

    peak = max((len(v) for v in slot_snapshots.values()), default=0)
    n_executed = len(executed_signals)
    total_capital = cfg.n_slots * capital_ntd
    meta = {
        "n_slots": cfg.n_slots,
        "capital_ntd": capital_ntd,
        "total_capital_ntd": total_capital,
        "cost_bps": 0.0,
        "hold_trading_days": cfg.max_hold_days,
        "min_hold_days": cfg.min_hold_days,
        "n_signals": n_executed,
        "n_executed": n_executed,
        "n_skipped": 0,
        "signal_capture_pct": None,
        "peak_concurrent_slots": peak,
        "strategy_id": RRG_MONO_SWAP_ACCEL_SLUG,
        "strategy_title": f"C18acc+I36 · {cfg.n_slots}槽 · swap-accel+extension",
        "strategy_rule": (
            f"C18acc min_hold {cfg.min_hold_days} / max_hold {cfg.max_hold_days} · "
            f"I36 combo_spike spike≥4% fade≥1% · 1m"
        ),
        "strategy_filter": "fresh · 四日加速换仓 · score_swap_c · extension overlay",
        "display_code": "C18acc+I36",
        "table_mode": "mono",
        "entry_price_mode": "close",
        "swaps_total": base_summary.get("swaps_total"),
        "variant_id": "I36",
        "parent_variant_id": cfg.variant_id,
        "timing_mode": cfg.timing_mode,
        "ext_exits": overlay_summary.get("ext_exits"),
        "ext_unchanged": overlay_summary.get("unchanged_legs"),
        "median_hold_days": overlay_summary.get("median_hold_days"),
        "mean_excess_pct": overlay_summary.get("mean_excess_pct"),
        "mean_save_vs_orig_ext_pct": overlay_summary.get("mean_save_vs_orig_ext_pct"),
    }
    return legs_out, executed_signals, [], meta


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
    """Research champion · rrg-mono-swap-accel（C18acc）· live / backtest SSOT.

  Score keys (two-metric funnel — do not collapse to one key everywhere):
    - Pool merge / shortlist passthrough: ``candidate_rank_key=seg_last``
    - Entry poll: ``avg_accel_decel`` (四日 · PIT as-of pool lock · C3a confirm=2)
    - Swap sell: weakest ``avg_accel_decel`` when < 0 (spread_lost bypass optional)
    - Swap buy: ``seg_last > held + 0.05`` then max ``avg_accel_decel``
    - Swap margin threshold always uses ``seg_last`` via ``_buy_threshold_score``

  Entry window (2026-07-14): ``no_trade_before=13:00`` · same-day near-close pool
  (ops ≥30m-to-close · SNAP_1300≈SNAP_1320 · confirm_1250 OOS +0.42pp).
  Prior near-close thesis (2026-07-13): LIVE_MATCHED_1320 OOS +1.01pp vs E+1 09:30.
  Entry-rank adoption (2026-07-12): R2 avg_accel vs C0×seg_last OOS +0.66pp · GO_ADOPT_R2.
  Chase gate (2026-07-12): G_R5_12 prior_5d_ret_max=0.12 · OOS +1.97pp · G3 PASS.
    """
    return ScoreSwapCConfig(
        CHAMPION_SCORE_SWAP_C_VARIANT_ID,
        "fresh mono · S2 · E@13:00 · prior5d≤12%",
        candidate_pool="fresh",
        quad_force_exit_mode="weakening",
        quad_force_exit_min_days=2,
        quad_force_exit_loss_pct=-0.05,
        quad_force_exit_requires_min_hold=True,
        accel_sell_bypass_on_spread_lost=True,
        **_champion_accel_fields(),
    )


def build_pit_candidate_pool(
    *,
    fresh_mono: list[ScanRow],
    mono_rows: list[ScanRow],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    full_dates: list[str],
    as_of: str,
    config: ScoreSwapCConfig | None = None,
) -> list[ScanRow]:
    """Live / daily brief · PIT candidate pool aligned with backtest candidate_pool."""
    cfg = config or champion_score_swap_c_config()
    return _candidate_pool(
        as_of,
        fresh_mono=fresh_mono,
        mono_by_date={as_of: mono_rows},
        mono_up_by_date=None,
        mono_up_fresh_by_date=None,
        config=cfg,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        full_dates=full_dates,
    )


def close_champion_score_swap_c_config() -> ScoreSwapCConfig:
    """C18acc champion frozen · timing_mode=close（無盤中 RRG poll · 收盤換倉）。"""
    cfg = champion_score_swap_c_config()
    fields = cfg.to_dict()
    fields["variant_id"] = f"{CHAMPION_SCORE_SWAP_C_VARIANT_ID}-close"
    fields["label"] = f"{cfg.label} · close 換倉"
    fields["timing_mode"] = "close"
    return ScoreSwapCConfig(**fields)


def pure_fixed_hold_score_swap_c_config() -> ScoreSwapCConfig:
    """H-PURE SSG：fresh mono · close · max_swaps=0 · 僅 max_hold 到期重填。"""
    cfg = close_champion_score_swap_c_config()
    fields = cfg.to_dict()
    fields["variant_id"] = "C18acc-pure"
    fields["label"] = f"{cfg.label} · no swap · max_hold only"
    fields["max_swaps_per_day"] = 0
    return ScoreSwapCConfig(**fields)


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


C18ACC_SLOT_GRID: tuple[int, ...] = (3, 5, 7, 9)


def c18acc_slot_config(n_slots: int, *, fill_mode: FillMode = "batch_topk") -> ScoreSwapCConfig:
    """C18acc 槽位矩陣 variant · 凍結 champion 規則 · 僅掃 n_slots / fill_mode。"""
    from dataclasses import replace

    base = champion_score_swap_c_config()
    return replace(
        base,
        variant_id=f"C18acc-S{n_slots}",
        label=f"C18acc · {n_slots}槽 · {fill_mode}",
        n_slots=n_slots,
        fill_mode=fill_mode,
    )


def _year_slices(trade_dates: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for d in trade_dates:
        out.setdefault(d[:4], []).append(d)
    return out


def _summarize_period_subset(periods: list[dict[str, Any]]) -> dict[str, Any]:
    s = summarize_periods(periods)
    n = len(periods)
    if n:
        s["n_periods"] = n
        s["mean_excess_pct"] = round(sum(p["excess_pct"] for p in periods) / n, 4)
    else:
        s["n_periods"] = 0
        s["mean_excess_pct"] = None
    return s


def run_c18acc_slot_matrix(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-22",
    slot_grid: tuple[int, ...] = C18ACC_SLOT_GRID,
    comparison_notional_ntd: float = 100_000.0,
) -> dict[str, Any]:
    """C18acc 槽位矩陣 · batch_topk 補滿 · Research topic rrg-mono-swap-accel-slots。"""
    from market_breadth_ma import build_breadth_panel
    from research.backtest.finpilot_local_backtest import load_price_panels
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar, simulate_mono_hold7
    from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP

    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    entry_c_config = c0_cfg

    rows: list[dict[str, Any]] = []
    hold7_refs: list[dict[str, Any]] = []

    for n in slot_grid:
        print(f"C18acc slot matrix · S{n} batch_topk ...", flush=True)
        cfg = c18acc_slot_config(n, fill_mode="batch_topk")
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
            rs_mom=rs_mom,
            rs_ratio=rs_ratio,
            entry_c_config=entry_c_config,
        )
        port = portfolio_metrics_for_periods(
            conn,
            periods,
            trade_dates,
            total_capital=comparison_notional_ntd,
            n_slots=n,
            close=close,
        )
        summary["interval_excess_pct"] = port.get("interval_excess_pct")
        summary["total_return_pct"] = port.get("total_return_pct")
        summary["sharpe_ratio"] = port.get("sharpe_ratio")
        summary["max_drawdown_pct"] = port.get("max_drawdown_pct")
        summary["capital_per_slot_ntd"] = round(comparison_notional_ntd / n, 2)
        by_year = {y: _summarize_period_subset([p for p in periods if str(p.get("entry_date", "")).startswith(y)]) for y in sorted(_year_slices(trade_dates))}
        summary["by_year"] = by_year
        rows.append(summary)
        print(
            f"  S{n}: mean_excess={summary.get('mean_excess_pct')}% "
            f"swaps={summary.get('swaps_total')} util={summary.get('slot_util_pct')}% "
            f"shortfall_days={summary.get('pool_shortfall_days')}",
            flush=True,
        )

        _, h7 = simulate_mono_hold7(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            zone_by_date=zone_by_date,
            fresh_by_date=fresh_by_date,
            n_slots=n,
        )
        hold7_refs.append(
            {
                "variant_id": f"rrg-mono-hold7-S{n}",
                "n_slots": n,
                "mean_excess_pct": h7.get("mean_excess_pct"),
                "n_periods": h7.get("n_periods"),
            }
        )

    # Drift check: S3 legacy vs champion
    print("C18acc slot matrix · S3 legacy_sequential (drift check) ...", flush=True)
    legacy_cfg = c18acc_slot_config(3, fill_mode="legacy_sequential")
    _, legacy_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=legacy_cfg,
        kbar_cache=kbar_cache,
        rs_mom=rs_mom,
        rs_ratio=rs_ratio,
        entry_c_config=entry_c_config,
    )

    champion_ref = champion_score_swap_c_config()
    _, champ_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=champion_ref,
        kbar_cache=kbar_cache,
        rs_mom=rs_mom,
        rs_ratio=rs_ratio,
        entry_c_config=entry_c_config,
    )

    ranked = sorted(rows, key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)))
    s3 = next((r for r in rows if r.get("n_slots") == 3), None)
    return {
        "topic": "rrg-mono-swap-accel-slots",
        "slug": RRG_MONO_SWAP_ACCEL_SLUG,
        "short_name": RRG_MONO_SWAP_ACCEL_SHORT,
        "date_start": date_start,
        "date_end": date_end,
        "comparison_notional_ntd": comparison_notional_ntd,
        "fill_mode_primary": "batch_topk",
        "slot_grid": list(slot_grid),
        "summaries": rows,
        "best": ranked[0] if ranked else None,
        "hold7_reference": hold7_refs,
        "drift_check": {
            "champion_C18acc": {
                "mean_excess_pct": champ_summary.get("mean_excess_pct"),
                "swaps_total": champ_summary.get("swaps_total"),
                "n_periods": champ_summary.get("n_periods"),
            },
            "S3_legacy_sequential": {
                "mean_excess_pct": legacy_summary.get("mean_excess_pct"),
                "swaps_total": legacy_summary.get("swaps_total"),
                "n_periods": legacy_summary.get("n_periods"),
            },
            "S3_batch_topk": {
                "mean_excess_pct": (s3 or {}).get("mean_excess_pct"),
                "swaps_total": (s3 or {}).get("swaps_total"),
                "n_periods": (s3 or {}).get("n_periods"),
            },
        },
        "hypotheses_note": "H-SLOT-1..5 · see config/research.yaml rrg-mono-swap-accel-slots",
    }


def render_c18acc_slot_matrix_md(payload: dict[str, Any]) -> str:
    lines = [
        f"# C18acc 槽位矩陣 · {payload.get('date_start')} ~ {payload.get('date_end')}",
        "",
        f"- fill_mode：**{payload.get('fill_mode_primary')}** · notional **{payload.get('comparison_notional_ntd'):,.0f}** NTD",
        "",
        "## 主表",
        "",
        "| variant | n_slots | 成交笔 | swaps | 均超额% | interval_excess% | 胜率% | slot_util% | pool_shortfall_days | Sharpe | MaxDD% |",
        "|---------|---------|--------|-------|---------|------------------|-------|------------|---------------------|--------|--------|",
    ]
    for s in payload.get("summaries") or []:
        lines.append(
            f"| {s.get('variant_id')} | {s.get('n_slots')} | {s.get('n_periods')} | {s.get('swaps_total')} | "
            f"{s.get('mean_excess_pct')} | {s.get('interval_excess_pct')} | {s.get('win_rate_vs_bench_pct')} | "
            f"{s.get('slot_util_pct')} | {s.get('pool_shortfall_days')} | {s.get('sharpe_ratio')} | {s.get('max_drawdown_pct')} |"
        )
    lines.extend(["", "## hold7 對照（同槽位 · 無換倉）", ""])
    lines.append("| variant | n_slots | 成交笔 | 均超额% |")
    lines.append("|---------|---------|--------|---------|")
    for h in payload.get("hold7_reference") or []:
        lines.append(
            f"| {h.get('variant_id')} | {h.get('n_slots')} | {h.get('n_periods')} | {h.get('mean_excess_pct')} |"
        )
    dc = payload.get("drift_check") or {}
    lines.extend(["", "## Drift check（S3 對照 champion）", ""])
    for key, val in dc.items():
        lines.append(f"- **{key}**：均超额 {val.get('mean_excess_pct')}% · swaps {val.get('swaps_total')} · n={val.get('n_periods')}")
    best = payload.get("best") or {}
    lines.extend(["", f"**Research champion（本 sweep）**：{best.get('variant_id')} · 均超额 **{best.get('mean_excess_pct')}%**", ""])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# C18acc 全槽率研究 · rrg-mono-swap-accel-fullslot
# ---------------------------------------------------------------------------

FillPolicy = Literal["fresh_only", "fresh_then_tier2", "tier2_all"]
C18ACC_FULLSLOT_GRID: tuple[int, ...] = (3, 5, 7, 9)


def c18acc_fullslot_config(
    n_slots: int,
    fill_policy: FillPolicy = "fresh_then_tier2",
) -> ScoreSwapCConfig:
    """C18acc 全槽率研究 variant · 凍結換倉規則 · 掃 n_slots × fill_policy。"""
    from dataclasses import replace

    base = champion_score_swap_c_config()
    if fill_policy == "fresh_only":
        cfg = replace(
            base,
            variant_id=f"C18acc-S{n_slots}-fresh",
            label=f"C18acc · {n_slots}槽 · fresh_only",
            n_slots=n_slots,
            fill_mode="batch_topk",
            entry_pool="fresh",
            entry_fallback_pool=None,
        )
    elif fill_policy == "fresh_then_tier2":
        cfg = replace(
            base,
            variant_id=f"C18acc-S{n_slots}-tier2sup",
            label=f"C18acc · {n_slots}槽 · fresh→tier2 補",
            n_slots=n_slots,
            fill_mode="batch_topk",
            entry_pool="fresh",
            entry_fallback_pool="mono_tier2",
        )
    else:  # tier2_all
        cfg = replace(
            base,
            variant_id=f"C18acc-S{n_slots}-tier2all",
            label=f"C18acc · {n_slots}槽 · tier2_all",
            n_slots=n_slots,
            fill_mode="batch_topk",
            candidate_pool="mono_tier2",
            entry_pool=None,
            entry_fallback_pool=None,
        )
    return cfg


def run_c18acc_fullslot_matrix(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-22",
    slot_grid: tuple[int, ...] = C18ACC_FULLSLOT_GRID,
    comparison_notional_ntd: float = 100_000.0,
) -> dict[str, Any]:
    """C18acc 全槽率研究 · 3 種 fill policy × 4 槽位 · Research topic rrg-mono-swap-accel-fullslot。"""
    from market_breadth_ma import build_breadth_panel
    from research.backtest.finpilot_local_backtest import load_price_panels
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar, simulate_mono_hold7
    from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]

    # 預建 calendars
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    mono_by_date = build_mono_tier2_calendar(conn, trade_dates, close=close, bench=bench)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP

    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    entry_c_config = c0_cfg

    policies: list[FillPolicy] = ["fresh_only", "fresh_then_tier2", "tier2_all"]
    rows: list[dict[str, Any]] = []

    for policy in policies:
        for n in slot_grid:
            # fresh_only S3/S5 作為對照錨（其餘可略跑，保留給拒絕登錄用）
            label_tag = f"S{n}-{policy}"
            print(f"C18acc fullslot · {label_tag} ...", flush=True)
            cfg = c18acc_fullslot_config(n, fill_policy=policy)
            periods, summary = simulate_score_swap_c(
                conn,
                trade_dates=trade_dates,
                full_dates=full_dates,
                close=close,
                bench=bench,
                fresh_by_date=fresh_by_date,
                mono_by_date=mono_by_date,
                zone_by_date=zone_by_date,
                config=cfg,
                kbar_cache=kbar_cache,
                rs_mom=rs_mom,
                rs_ratio=rs_ratio,
                entry_c_config=entry_c_config,
            )
            port = portfolio_metrics_for_periods(
                conn,
                periods,
                trade_dates,
                total_capital=comparison_notional_ntd,
                n_slots=n,
                close=close,
            )
            summary["interval_excess_pct"] = port.get("interval_excess_pct")
            summary["total_return_pct"] = port.get("total_return_pct")
            summary["sharpe_ratio"] = port.get("sharpe_ratio")
            summary["max_drawdown_pct"] = port.get("max_drawdown_pct")
            summary["capital_per_slot_ntd"] = round(comparison_notional_ntd / n, 2)
            summary["fill_policy"] = policy
            by_year = {
                y: _summarize_period_subset([p for p in periods if str(p.get("entry_date", "")).startswith(y)])
                for y in sorted(_year_slices(trade_dates))
            }
            summary["by_year"] = by_year
            rows.append(summary)
            print(
                f"  {label_tag}: mean_excess={summary.get('mean_excess_pct')}% "
                f"swaps={summary.get('swaps_total')} util={summary.get('slot_util_pct')}% "
                f"tier2_sup={summary.get('tier2_supplement_days')} "
                f"shortfall={summary.get('pool_shortfall_days')}",
                flush=True,
            )

    # hold7 靜態對照（fresh_only · S5/S7 補充）
    hold7_refs: list[dict[str, Any]] = []
    for n in slot_grid:
        _, h7 = simulate_mono_hold7(
            conn,
            trade_dates=trade_dates,
            full_dates=full_dates,
            close=close,
            zone_by_date=zone_by_date,
            fresh_by_date=fresh_by_date,
            n_slots=n,
        )
        hold7_refs.append(
            {
                "variant_id": f"rrg-mono-hold7-S{n}",
                "n_slots": n,
                "mean_excess_pct": h7.get("mean_excess_pct"),
                "n_periods": h7.get("n_periods"),
            }
        )

    # Drift check：S3-fresh_only 須對齊 Phase 1 champion
    drift_cfg = c18acc_fullslot_config(3, fill_policy="fresh_only")
    s3_fresh = next((r for r in rows if r.get("variant_id") == drift_cfg.variant_id), None)
    champion_ref = champion_score_swap_c_config()
    _, champ_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=champion_ref,
        kbar_cache=kbar_cache,
        rs_mom=rs_mom,
        rs_ratio=rs_ratio,
        entry_c_config=entry_c_config,
    )

    ranked = sorted(rows, key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)))
    return {
        "topic": "rrg-mono-swap-accel-fullslot",
        "slug": RRG_MONO_SWAP_ACCEL_SLUG,
        "short_name": RRG_MONO_SWAP_ACCEL_SHORT,
        "date_start": date_start,
        "date_end": date_end,
        "comparison_notional_ntd": comparison_notional_ntd,
        "slot_grid": list(slot_grid),
        "fill_policies": policies,
        "summaries": rows,
        "best": ranked[0] if ranked else None,
        "hold7_reference": hold7_refs,
        "drift_check": {
            "champion_C18acc": {
                "mean_excess_pct": champ_summary.get("mean_excess_pct"),
                "swaps_total": champ_summary.get("swaps_total"),
                "n_periods": champ_summary.get("n_periods"),
            },
            "S3_fresh_only": {
                "mean_excess_pct": (s3_fresh or {}).get("mean_excess_pct"),
                "swaps_total": (s3_fresh or {}).get("swaps_total"),
                "n_periods": (s3_fresh or {}).get("n_periods"),
                "slot_util_pct": (s3_fresh or {}).get("slot_util_pct"),
                "tier2_supplement_days": (s3_fresh or {}).get("tier2_supplement_days"),
            },
        },
        "hypotheses_note": "H-FULL-1..5 · see config/research.yaml rrg-mono-swap-accel-fullslot",
    }


def render_c18acc_fullslot_matrix_md(payload: dict[str, Any]) -> str:
    lines = [
        f"# C18acc 全槽率矩陣 · {payload.get('date_start')} ~ {payload.get('date_end')}",
        "",
        f"- notional **{payload.get('comparison_notional_ntd'):,.0f}** NTD · policies: {', '.join(payload.get('fill_policies') or [])}",
        "",
        "## 主表",
        "",
        "| variant | n_slots | fill_policy | 成交笔 | swaps | 均超额% | 胜率% | slot_util% | tier2_sup天 | tier2_sup% | shortfall天 | Sharpe | MaxDD% |",
        "|---------|---------|-------------|--------|-------|---------|-------|------------|------------|------------|------------|--------|--------|",
    ]
    for s in payload.get("summaries") or []:
        lines.append(
            f"| {s.get('variant_id')} | {s.get('n_slots')} | {s.get('fill_policy')} "
            f"| {s.get('n_periods')} | {s.get('swaps_total')} "
            f"| {s.get('mean_excess_pct')} | {s.get('win_rate_vs_bench_pct')} "
            f"| {s.get('slot_util_pct')} | {s.get('tier2_supplement_days')} "
            f"| {s.get('tier2_supplement_pct')} | {s.get('pool_shortfall_days')} "
            f"| {s.get('sharpe_ratio')} | {s.get('max_drawdown_pct')} |"
        )
    lines.extend(["", "## hold7 對照（fresh_only · 無換倉）", ""])
    lines.append("| variant | n_slots | 成交笔 | 均超额% |")
    lines.append("|---------|---------|--------|---------|")
    for h in payload.get("hold7_reference") or []:
        lines.append(
            f"| {h.get('variant_id')} | {h.get('n_slots')} | {h.get('n_periods')} | {h.get('mean_excess_pct')} |"
        )
    dc = payload.get("drift_check") or {}
    lines.extend(["", "## Drift check（S3-fresh_only 對照 champion）", ""])
    for key, val in dc.items():
        lines.append(
            f"- **{key}**：均超额 {val.get('mean_excess_pct')}% · swaps {val.get('swaps_total')} "
            f"· n={val.get('n_periods')} · util={val.get('slot_util_pct')}% · tier2_sup={val.get('tier2_supplement_days')}"
        )
    best = payload.get("best") or {}
    lines.extend(["", f"**Research champion（本 sweep）**：{best.get('variant_id')} · fill_policy={best.get('fill_policy')} · 均超额 **{best.get('mean_excess_pct')}%**", ""])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# C18acc 加碼補滿研究 · rrg-mono-swap-accel-repeat
# ---------------------------------------------------------------------------

C18ACC_REPEAT_SLOT_GRID: tuple[int, ...] = (3, 5, 7)


def c18acc_repeat_config(n_slots: int, *, fill_repeat: bool = True) -> ScoreSwapCConfig:
    """C18acc 加碼補滿 variant：空槽可重複買同一支直到填滿，不留閒置資金。"""
    from dataclasses import replace

    base = champion_score_swap_c_config()
    tag = "repeat" if fill_repeat else "fresh"
    return replace(
        base,
        variant_id=f"C18acc-S{n_slots}-{tag}",
        label=f"C18acc · {n_slots}槽 · {'加碼補滿' if fill_repeat else 'fresh_only'}",
        n_slots=n_slots,
        fill_mode="batch_topk",
        fill_repeat=fill_repeat,
        entry_pool="fresh",
        entry_fallback_pool=None,
    )


def run_c18acc_repeat_matrix(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-22",
    slot_grid: tuple[int, ...] = C18ACC_REPEAT_SLOT_GRID,
    comparison_notional_ntd: float = 100_000.0,
) -> dict[str, Any]:
    """C18acc 加碼補滿研究 · repeat vs no-repeat × 3/5/7 槽 · topic rrg-mono-swap-accel-repeat。"""
    from market_breadth_ma import build_breadth_panel
    from research.backtest.finpilot_local_backtest import load_price_panels
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
    from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]

    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP

    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    entry_c_config = c0_cfg

    rows: list[dict[str, Any]] = []

    for n in slot_grid:
        for repeat in (False, True):
            tag = "repeat" if repeat else "fresh"
            print(f"C18acc repeat · S{n}-{tag} ...", flush=True)
            cfg = c18acc_repeat_config(n, fill_repeat=repeat)
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
                rs_mom=rs_mom,
                rs_ratio=rs_ratio,
                entry_c_config=entry_c_config,
            )
            port = portfolio_metrics_for_periods(
                conn,
                periods,
                trade_dates,
                total_capital=comparison_notional_ntd,
                n_slots=n,
                close=close,
            )
            summary["interval_excess_pct"] = port.get("interval_excess_pct")
            summary["total_return_pct"] = port.get("total_return_pct")
            summary["sharpe_ratio"] = port.get("sharpe_ratio")
            summary["max_drawdown_pct"] = port.get("max_drawdown_pct")
            summary["capital_per_slot_ntd"] = round(comparison_notional_ntd / n, 2)
            by_year = {
                y: _summarize_period_subset([p for p in periods if str(p.get("entry_date", "")).startswith(y)])
                for y in sorted(_year_slices(trade_dates))
            }
            summary["by_year"] = by_year
            rows.append(summary)
            print(
                f"  S{n}-{tag}: mean_excess={summary.get('mean_excess_pct')}% "
                f"swaps={summary.get('swaps_total')} util={summary.get('slot_util_pct')}% "
                f"repeat_days={summary.get('fill_repeat_days')} "
                f"shortfall={summary.get('pool_shortfall_days')}",
                flush=True,
            )

    ranked = sorted(rows, key=lambda s: (-(s.get("mean_excess_pct") or -999.0), -(s.get("n_periods") or 0)))
    s3_fresh = next((r for r in rows if r.get("n_slots") == 3 and not r.get("fill_repeat")), None)
    s3_repeat = next((r for r in rows if r.get("n_slots") == 3 and r.get("fill_repeat")), None)
    return {
        "topic": "rrg-mono-swap-accel-repeat",
        "slug": RRG_MONO_SWAP_ACCEL_SLUG,
        "short_name": RRG_MONO_SWAP_ACCEL_SHORT,
        "date_start": date_start,
        "date_end": date_end,
        "comparison_notional_ntd": comparison_notional_ntd,
        "slot_grid": list(slot_grid),
        "summaries": rows,
        "best": ranked[0] if ranked else None,
        "comparison": {
            "S3_fresh": {
                "mean_excess_pct": (s3_fresh or {}).get("mean_excess_pct"),
                "sharpe_ratio": (s3_fresh or {}).get("sharpe_ratio"),
                "slot_util_pct": (s3_fresh or {}).get("slot_util_pct"),
                "pool_shortfall_days": (s3_fresh or {}).get("pool_shortfall_days"),
            },
            "S3_repeat": {
                "mean_excess_pct": (s3_repeat or {}).get("mean_excess_pct"),
                "sharpe_ratio": (s3_repeat or {}).get("sharpe_ratio"),
                "slot_util_pct": (s3_repeat or {}).get("slot_util_pct"),
                "fill_repeat_days": (s3_repeat or {}).get("fill_repeat_days"),
            },
        },
        "hypotheses_note": "H-REP-1..4 · see config/research.yaml rrg-mono-swap-accel-repeat",
    }


def render_c18acc_repeat_matrix_md(payload: dict[str, Any]) -> str:
    lines = [
        f"# C18acc 加碼補滿研究 · {payload.get('date_start')} ~ {payload.get('date_end')}",
        "",
        f"- notional **{payload.get('comparison_notional_ntd'):,.0f}** NTD · repeat=True → 空槽加碼同一支直到填滿",
        "",
        "## 主表",
        "",
        "| variant | n_slots | fill_repeat | 成交笔 | swaps | 均超额% | 胜率% | slot_util% | 加碼天數 | shortfall天 | Sharpe | MaxDD% |",
        "|---------|---------|-------------|--------|-------|---------|-------|------------|---------|------------|--------|--------|",
    ]
    for s in payload.get("summaries") or []:
        lines.append(
            f"| {s.get('variant_id')} | {s.get('n_slots')} | {s.get('fill_repeat')} "
            f"| {s.get('n_periods')} | {s.get('swaps_total')} "
            f"| {s.get('mean_excess_pct')} | {s.get('win_rate_vs_bench_pct')} "
            f"| {s.get('slot_util_pct')} | {s.get('fill_repeat_days')} "
            f"| {s.get('pool_shortfall_days')} "
            f"| {s.get('sharpe_ratio')} | {s.get('max_drawdown_pct')} |"
        )
    cmp = payload.get("comparison") or {}
    lines.extend(["", "## S3 對照（fresh vs repeat）", ""])
    for key, val in cmp.items():
        lines.append(
            f"- **{key}**：均超额 {val.get('mean_excess_pct')}% · Sharpe {val.get('sharpe_ratio')} "
            f"· util={val.get('slot_util_pct')}%"
        )
    best = payload.get("best") or {}
    lines.extend(["", f"**Research champion（本 sweep）**：{best.get('variant_id')} · 均超额 **{best.get('mean_excess_pct')}%**", ""])
    return "\n".join(lines) + "\n"


# C18acc · 隔日漲幅豁免 min_hold · diagnostic sweep
C18ACC_GAIN_WAIVER_DIAG: list[ScoreSwapCConfig] = [
    champion_score_swap_c_config(),
    ScoreSwapCConfig(
        "C18acc-g6-d2",
        "隔日≥6% · waiver min_hold=2",
        gain_waiver_threshold=0.06,
        gain_waiver_min_hold=2,
        **_champion_accel_fields(),
    ),
    ScoreSwapCConfig(
        "C18acc-g6-d1",
        "隔日≥6% · waiver min_hold=1",
        gain_waiver_threshold=0.06,
        gain_waiver_min_hold=1,
        **_champion_accel_fields(),
    ),
    ScoreSwapCConfig(
        "C18acc-g5-d2",
        "隔日≥5% · waiver min_hold=2",
        gain_waiver_threshold=0.05,
        gain_waiver_min_hold=2,
        **_champion_accel_fields(),
    ),
    ScoreSwapCConfig(
        "C18acc-mh4-05",
        "無條件 min_hold=4（機械對照）",
        **({**_champion_accel_fields(), "min_hold_days": 4}),
    ),
]


def _day1_stats_from_periods(
    close: pd.DataFrame,
    full_dates: list[str],
    periods: list[dict[str, Any]],
    *,
    threshold: float = 0.06,
) -> dict[str, Any]:
    """Baseline 進場 leg · 隔日漲幅分布（PIT · close vs entry_px）。"""
    rets: list[float] = []
    for p in periods:
        d1 = _day1_return(
            close,
            full_dates,
            entry_date=str(p["entry_date"]),
            entry_px=float(p["entry_px"]),
            stock_id=str(p["stock_id"]),
            as_of=str(p["exit_date"]),
        )
        if d1 is None:
            continue
        rets.append(d1)
    if not rets:
        return {"n": 0, "pct_ge_threshold": None, "mean_day1_pct": None, "median_day1_pct": None}
    ge = sum(1 for r in rets if r >= threshold)
    sorted_rets = sorted(rets)
    mid = len(sorted_rets) // 2
    median = sorted_rets[mid] if len(sorted_rets) % 2 else (sorted_rets[mid - 1] + sorted_rets[mid]) / 2
    return {
        "n": len(rets),
        "pct_ge_threshold": round(100.0 * ge / len(rets), 2),
        "count_ge_threshold": ge,
        "mean_day1_pct": round(sum(rets) / len(rets) * 100.0, 4),
        "median_day1_pct": round(median * 100.0, 4),
        "threshold_pct": round(threshold * 100.0, 2),
    }


def run_c18acc_gain_waiver_diagnostic(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2026-01-01",
    date_end: str = "2026-06-22",
    comparison_notional_ntd: float = 60_000.0,
    configs: list[ScoreSwapCConfig] | None = None,
) -> dict[str, Any]:
    """C18acc 隔日漲幅豁免 min_hold · Research diagnostic。"""
    from market_breadth_ma import build_breadth_panel
    from research.backtest.finpilot_local_backtest import load_price_panels
    from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
    from research.backtest.slot_portfolio_metrics import portfolio_metrics_for_periods

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    kbar_cache: dict[tuple[str, str], tuple[tuple[str, float], ...]] = {}
    from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP

    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")
    grid = configs or C18ACC_GAIN_WAIVER_DIAG
    rows: list[dict[str, Any]] = []
    baseline_periods: list[dict[str, Any]] = []

    for cfg in grid:
        print(f"C18acc gain waiver · {cfg.variant_id} ...", flush=True)
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
            rs_mom=rs_mom,
            rs_ratio=rs_ratio,
            entry_c_config=c0_cfg,
        )
        if cfg.variant_id == CHAMPION_SCORE_SWAP_C_VARIANT_ID:
            baseline_periods = periods
        port = portfolio_metrics_for_periods(
            conn,
            periods,
            trade_dates,
            total_capital=comparison_notional_ntd,
            n_slots=3,
            close=close,
        )
        summary["sharpe_ratio"] = port.get("sharpe_ratio")
        summary["max_drawdown_pct"] = port.get("max_drawdown_pct")
        summary["interval_excess_pct"] = port.get("interval_excess_pct")
        rows.append(summary)
        print(
            f"  {cfg.variant_id}: mean_excess={summary.get('mean_excess_pct')}% "
            f"swaps={summary.get('swaps_total')} hold={summary.get('mean_hold_days')} "
            f"early_swaps={summary.get('early_waiver_swaps')}",
            flush=True,
        )

    baseline = next((r for r in rows if r.get("variant_id") == CHAMPION_SCORE_SWAP_C_VARIANT_ID), {})
    day1_stats = _day1_stats_from_periods(close, full_dates, baseline_periods, threshold=0.06)
    comparisons: list[dict[str, Any]] = []
    base_ex = baseline.get("mean_excess_pct")
    for r in rows:
        ex = r.get("mean_excess_pct")
        comparisons.append(
            {
                "variant_id": r.get("variant_id"),
                "delta_mean_excess_pp": round(float(ex) - float(base_ex), 4) if ex is not None and base_ex is not None else None,
                "delta_swaps": (r.get("swaps_total") or 0) - (baseline.get("swaps_total") or 0),
            }
        )

    return {
        "topic": "rrg-mono-swap-accel-gain-waiver",
        "diagnostic": True,
        "date_start": date_start,
        "date_end": date_end,
        "comparison_notional_ntd": comparison_notional_ntd,
        "baseline_variant": CHAMPION_SCORE_SWAP_C_VARIANT_ID,
        "day1_stats_baseline": day1_stats,
        "summaries": rows,
        "vs_baseline": comparisons,
        "note": "隔日漲幅 = 進場後第1交易日收盤 vs entry_px · waiver 自 day2 起生效",
    }


def render_c18acc_gain_waiver_diagnostic_md(payload: dict[str, Any]) -> str:
    d1 = payload.get("day1_stats_baseline") or {}
    lines = [
        f"# C18acc 隔日漲幅豁免 min_hold · Diagnostic · {payload.get('date_start')} ~ {payload.get('date_end')}",
        "",
        "## Baseline 隔日分布（C18acc 進場 leg）",
        "",
        f"- 可觀測 leg **{d1.get('n')}** · 隔日 ≥ **{d1.get('threshold_pct')}%** 占比 **{d1.get('pct_ge_threshold')}%** "
        f"（{d1.get('count_ge_threshold')} 筆）",
        f"- 隔日均漲 **{d1.get('mean_day1_pct')}%** · 中位 **{d1.get('median_day1_pct')}%**",
        "",
        "## 變體對照",
        "",
        "| variant | 均超額% | Δ vs C18acc | swaps | early_swaps | mean_hold | waiver_rate% | Sharpe |",
        "|---------|---------|-------------|-------|-------------|-----------|--------------|--------|",
    ]
    base_ex = next(
        (s.get("mean_excess_pct") for s in payload.get("summaries") or [] if s.get("variant_id") == CHAMPION_SCORE_SWAP_C_VARIANT_ID),
        None,
    )
    for s in payload.get("summaries") or []:
        ex = s.get("mean_excess_pct")
        delta = round(float(ex) - float(base_ex), 4) if ex is not None and base_ex is not None else None
        lines.append(
            f"| {s.get('variant_id')} | {ex} | {delta} | {s.get('swaps_total')} "
            f"| {s.get('early_waiver_swaps')} | {s.get('mean_hold_days')} "
            f"| {s.get('waiver_rate_pct')} | {s.get('sharpe_ratio')} |"
        )
    lines.extend(["", "_Research diagnostic · 非 Strategy SSOT · 未 preregister OOS gate_", ""])
    return "\n".join(lines) + "\n"
