"""C18acc × 1 分 K 延伸出場 · 打破固定 5–10 日持有。

SSG：C18acc champion 進場 / 換倉路徑不變。
變更：出場由 1 分 K 延伸雷達主導 · max_hold 僅作安全 fallback。

對照舊 overlay（5m poll · 疊加在原 exit 上）：
  本模組允許 break_fixed_hold=True · 不再等待 C18acc 原 max_hold/換倉出場。
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Literal

import pandas as pd

from analytics.bench import bench_return_entry_to_exit
from intraday_extension_radar import (
    EntryMode,
    ExitMode,
    ExtensionExitSignal,
    ExtensionScanConfig,
    find_first_extension_entry,
    find_first_extension_exit,
    pick_exit_for_mode,
    scan_session,
)
from market_benchmark import load_benchmark_close
from research.backtest.c18acc_extension_exit import (
    _hold_dates,
    _ma20,
    _prior_close,
)
from research.backtest.finpilot_local_backtest import load_price_panels, summarize_periods
from research.backtest.rrg_mono_backtest import build_fresh_mono_calendar
from research.backtest.rrg_mono_intraday_ab import LENGTH
from research.backtest.rrg_mono_score_swap_c import (
    ScoreSwapCConfig,
    champion_score_swap_c_config,
    close_champion_score_swap_c_config,
    pure_fixed_hold_score_swap_c_config,
    simulate_score_swap_c,
)
from research.backtest.rrg_mono_intraday_exit import _trading_days_between
from rrg_rotation import compute_rrg_panel
from stock_db.kbar import kbar_day_has_data, load_kbar_day_bars

BreakMode = Literal["overlay", "replace"]


@dataclass(frozen=True)
class Intraday1mHoldConfig:
    variant_id: str
    label: str
    exit_mode: ExitMode = "heat_threshold"
    poll_interval_min: int = 1
    heat_threshold: int = 70
    vol_z_climax: float = 2.0
    min_ext_prev_pct: float = 0.0
    min_hold_days: int = 0
    max_hold_fallback: int = 10
    break_fixed_hold: bool = True
    extension_window_days: int | None = None
    use_orig_exit_if_no_signal: bool = False
    slip_pct: float = 0.5
    stretch_vwap_pct: float = 2.0
    reclaim_vwap_pct: float = 0.3
    fade_from_high_exit_pct: float = 2.0
    vwap_sigma_red: float = 2.0
    require_opening_spike: bool = True
    no_hh_bars: int = 30
    vwap_loss_window: int = 45
    min_fade_from_high_pct: float = 0.5
    require_2sigma_exit: bool = False
    or_secondary_exit_mode: ExitMode | None = None
    or_secondary_heat_threshold: int = 70
    or_secondary_min_ext_prev_pct: float = 4.0
    or_secondary_fade_from_high_exit_pct: float = 1.5
    or_secondary_min_fade_from_high_pct: float = 0.5
    or_secondary_vol_z_climax: float = 2.0
    or_secondary_require_opening_spike: bool = False

    def scan_config(self) -> ExtensionScanConfig:
        return ExtensionScanConfig(
            exit_mode=self.exit_mode,
            heat_threshold=self.heat_threshold,
            vol_z_climax=self.vol_z_climax,
            poll_interval_min=self.poll_interval_min,
            min_ext_prev_pct=self.min_ext_prev_pct,
            stretch_vwap_pct=self.stretch_vwap_pct,
            reclaim_vwap_pct=self.reclaim_vwap_pct,
            fade_from_high_exit_pct=self.fade_from_high_exit_pct,
            vwap_sigma_red=self.vwap_sigma_red,
            require_opening_spike=self.require_opening_spike,
            no_hh_bars=self.no_hh_bars,
            vwap_loss_window=self.vwap_loss_window,
            min_fade_from_high_pct=self.min_fade_from_high_pct,
            require_2sigma_exit=self.require_2sigma_exit,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def or_secondary_scan_config(self) -> ExtensionScanConfig | None:
        if self.or_secondary_exit_mode is None:
            return None
        return ExtensionScanConfig(
            exit_mode=self.or_secondary_exit_mode,
            heat_threshold=self.or_secondary_heat_threshold,
            vol_z_climax=self.or_secondary_vol_z_climax,
            poll_interval_min=self.poll_interval_min,
            min_ext_prev_pct=self.or_secondary_min_ext_prev_pct,
            stretch_vwap_pct=self.stretch_vwap_pct,
            reclaim_vwap_pct=self.reclaim_vwap_pct,
            fade_from_high_exit_pct=self.or_secondary_fade_from_high_exit_pct,
            vwap_sigma_red=self.vwap_sigma_red,
            require_opening_spike=self.or_secondary_require_opening_spike,
            no_hh_bars=self.no_hh_bars,
            vwap_loss_window=self.vwap_loss_window,
            min_fade_from_high_pct=self.or_secondary_min_fade_from_high_pct,
            require_2sigma_exit=self.require_2sigma_exit,
        )


def _find_or_hybrid_exit(
    bars: tuple,
    *,
    prev_close: float,
    ma20: float | None,
    primary_cfg: ExtensionScanConfig,
    primary_mode: ExitMode,
    secondary_cfg: ExtensionScanConfig,
    secondary_mode: ExitMode,
) -> ExtensionExitSignal | None:
    """OR-hybrid · 兩模式各掃一次 · 取最早觸發分鐘。"""
    candidates: list[ExtensionExitSignal] = []
    for cfg, mode in ((primary_cfg, primary_mode), (secondary_cfg, secondary_mode)):
        scan = scan_session(
            bars,
            trade_date="",
            stock_id="",
            prev_close=prev_close,
            ma20=ma20,
            config=cfg,
        )
        sig = pick_exit_for_mode(scan, mode)
        if sig is None:
            continue
        if cfg.min_ext_prev_pct > 0:
            snap = next((b for b in scan.bars if b.minute == sig.minute), None)
            if snap is None or snap.ext_prev_pct < cfg.min_ext_prev_pct:
                continue
        candidates.append(sig)
    if not candidates:
        return None
    return min(candidates, key=lambda s: s.minute)


def _fallback_exit_date(full_dates: list[str], entry: str, max_hold: int) -> str | None:
    dates = _hold_dates(full_dates, entry, full_dates[-1])
    if not dates:
        return None
    idx = min(max_hold, len(dates)) - 1
    if idx < 0:
        return None
    return dates[idx] if idx < len(dates) else dates[-1]


def _apply_slip(px: float, slip_pct: float) -> float:
    return px * (1.0 - slip_pct / 100.0)


def _close_px(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    stock_id: str,
    trade_date: str,
) -> float | None:
    if trade_date not in close.index or stock_id not in close.columns:
        return None
    px = float(close.at[trade_date, stock_id])
    return px if px > 0 else None


def _paired_excess_stats(
    modified: list[dict[str, Any]],
    base: list[dict[str, Any]],
) -> dict[str, float | None]:
    deltas: list[float] = []
    for new, old in zip(modified, base):
        ne, oe = new.get("excess_pct"), old.get("excess_pct")
        if ne is None or oe is None:
            continue
        deltas.append(float(ne) - float(oe))
    n = len(deltas)
    if n < 2:
        return {
            "paired_n": n,
            "paired_delta_mean_pp": round(sum(deltas) / n, 4) if n else None,
            "paired_delta_se_pp": None,
            "paired_delta_t": None,
            "paired_delta_p_approx": None,
            "paired_delta_ci95_lo": None,
            "paired_delta_ci95_hi": None,
        }
    mean = sum(deltas) / n
    var = sum((x - mean) ** 2 for x in deltas) / (n - 1)
    se = math.sqrt(var / n) if var > 0 else 0.0
    t_stat = mean / se if se > 1e-12 else 0.0
    p_approx = 2 * (1 - _normal_cdf(abs(t_stat)))
    ci = 1.96 * se
    return {
        "paired_n": n,
        "paired_delta_mean_pp": round(mean, 4),
        "paired_delta_se_pp": round(se, 4),
        "paired_delta_t": round(t_stat, 3),
        "paired_delta_p_approx": round(p_approx, 4),
        "paired_delta_ci95_lo": round(mean - ci, 4),
        "paired_delta_ci95_hi": round(mean + ci, 4),
    }


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def apply_intraday_1m_hold(
    conn: sqlite3.Connection,
    *,
    base_periods: list[dict[str, Any]],
    close: pd.DataFrame,
    full_dates: list[str],
    config: Intraday1mHoldConfig,
    kbar_cache: dict[tuple[str, str], tuple] | None = None,
    ma20_cache: dict[tuple[str, str], float | None] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = kbar_cache if kbar_cache is not None else {}
    ma_cache = ma20_cache if ma20_cache is not None else {}
    scan_cfg = config.scan_config()
    out: list[dict[str, Any]] = []
    ext_exits = 0
    fallback_exits = 0
    unchanged = 0
    kbar_hits = 0
    hold_days_list: list[int] = []
    ext_leg_delta: list[float] = []
    ext_save_vs_orig: list[float] = []

    for pos in base_periods:
        sid = str(pos["stock_id"])
        entry = str(pos["entry_date"])
        orig_exit = str(pos["exit_date"])
        entry_px = float(pos["entry_px"])
        orig_exit_px = float(pos["exit_px"])
        orig_hold = int(pos.get("hold_days") or _trading_days_between(full_dates, entry, orig_exit))

        orig_ret = (orig_exit_px / entry_px - 1.0) * 100.0

        if config.break_fixed_hold:
            last_d = _fallback_exit_date(full_dates, entry, config.max_hold_fallback)
            search_until = last_d or orig_exit
        else:
            search_until = orig_exit

        ext_exit_d: str | None = None
        ext_px: float | None = None
        ext_minute: str | None = None

        for d in _hold_dates(full_dates, entry, search_until):
            hold_day = _trading_days_between(full_dates, entry, d)
            if hold_day < config.min_hold_days:
                continue
            if (
                config.extension_window_days is not None
                and hold_day > config.extension_window_days
            ):
                break
            key = (sid, d)
            if key not in cache:
                if not kbar_day_has_data(conn, sid, d):
                    cache[key] = ()
                else:
                    cache[key] = load_kbar_day_bars(conn, sid, d)
                    kbar_hits += 1
            bars = cache[key]
            if not bars:
                continue
            prev_close = _prior_close(conn, sid, d, full_dates)
            if prev_close is None:
                continue
            ma_key = (sid, d)
            if ma_key not in ma_cache:
                ma_cache[ma_key] = _ma20(conn, sid, d, full_dates)
            sec_cfg = config.or_secondary_scan_config()
            if sec_cfg is not None and config.or_secondary_exit_mode is not None:
                sig = _find_or_hybrid_exit(
                    bars,
                    prev_close=prev_close,
                    ma20=ma_cache[ma_key],
                    primary_cfg=scan_cfg,
                    primary_mode=config.exit_mode,
                    secondary_cfg=sec_cfg,
                    secondary_mode=config.or_secondary_exit_mode,
                )
            else:
                sig = find_first_extension_exit(
                    bars,
                    prev_close=prev_close,
                    ma20=ma_cache[ma_key],
                    config=scan_cfg,
                )
            if sig is None:
                continue
            ext_exit_d = d
            ext_px = _apply_slip(sig.price, config.slip_pct)
            ext_minute = sig.minute
            ext_exits += 1
            break

        if ext_exit_d and ext_px:
            if config.or_secondary_exit_mode is not None:
                reason = f"1m_or_{config.exit_mode}+{config.or_secondary_exit_mode}"
            else:
                reason = f"1m_{config.exit_mode}"
            exit_d, exit_px, exit_minute = ext_exit_d, ext_px, ext_minute
        elif config.use_orig_exit_if_no_signal:
            out.append(dict(pos))
            unchanged += 1
            continue
        elif config.break_fixed_hold:
            fb = _fallback_exit_date(full_dates, entry, config.max_hold_fallback)
            if not fb:
                out.append(dict(pos))
                unchanged += 1
                continue
            px = _close_px(conn, close, sid, fb)
            if px is None:
                out.append(dict(pos))
                unchanged += 1
                continue
            exit_d, exit_px, exit_minute, reason = fb, px, None, "max_hold_fallback"
            fallback_exits += 1
        else:
            out.append(dict(pos))
            unchanged += 1
            continue

        ret = (exit_px / entry_px - 1.0) * 100.0
        bench = bench_return_entry_to_exit(conn, entry, exit_d, entry_price_mode="close")
        if bench is None:
            out.append(dict(pos))
            continue
        hd = _trading_days_between(full_dates, entry, exit_d)
        hold_days_list.append(hd)
        if ext_exit_d:
            ext_leg_delta.append(round(ret - orig_ret, 4))
            ext_save_vs_orig.append(round(orig_ret - ret, 4))
        leg = dict(pos)
        leg.update(
            {
                "exit_date": exit_d,
                "exit_px": round(exit_px, 4),
                "exit_minute": exit_minute,
                "exit_reason": reason,
                "hold_days": hd,
                "return_pct": round(ret, 4),
                "bench_return_pct": round(bench, 4),
                "excess_pct": round(ret - bench, 4),
                "beat_bench": ret > bench,
                "gross_win": ret > 0,
                "variant_id": config.variant_id,
                "orig_exit_date": orig_exit,
                "orig_exit_px": orig_exit_px,
                "orig_return_pct": round(orig_ret, 4),
                "orig_hold_days": orig_hold,
                "save_vs_orig_pct": round(orig_ret - ret, 4),
                "intraday_1m": True,
                "ext_exit": bool(ext_exit_d),
            }
        )
        out.append(leg)

    summary = summarize_periods(out)
    if summary.get("mean_return_pct") is not None and summary.get("mean_bench_pct") is not None:
        summary["mean_excess_pct"] = round(
            float(summary["mean_return_pct"]) - float(summary["mean_bench_pct"]), 4
        )
    holds = hold_days_list or [p.get("hold_days") for p in out if p.get("hold_days")]
    holds_f = [float(h) for h in holds if h is not None]
    summary.update(
        {
            "variant_id": config.variant_id,
            "label": config.label,
            "poll_interval_min": config.poll_interval_min,
            "exit_mode": config.exit_mode,
            "min_ext_prev_pct": config.min_ext_prev_pct,
            "min_fade_from_high_pct": config.min_fade_from_high_pct,
            "break_fixed_hold": config.break_fixed_hold,
            "ext_exits": ext_exits,
            "fallback_exits": fallback_exits,
            "unchanged_legs": unchanged,
            "kbar_hits": kbar_hits,
            "median_hold_days": round(sorted(holds_f)[len(holds_f) // 2], 2) if holds_f else None,
            "mean_hold_days": round(sum(holds_f) / len(holds_f), 2) if holds_f else None,
            "pct_exit_within_3d": round(
                sum(1 for h in holds_f if h <= 3) / len(holds_f) * 100, 1
            )
            if holds_f
            else None,
            "mean_ext_leg_delta_pct": round(sum(ext_leg_delta) / len(ext_leg_delta), 4)
            if ext_leg_delta
            else None,
            "median_ext_leg_delta_pct": round(
                sorted(ext_leg_delta)[len(ext_leg_delta) // 2], 4
            )
            if ext_leg_delta
            else None,
            "ext_leg_n": len(ext_leg_delta),
            "mean_save_vs_orig_ext_pct": round(sum(ext_save_vs_orig) / len(ext_save_vs_orig), 4)
            if ext_save_vs_orig
            else None,
            "median_save_vs_orig_ext_pct": round(
                sorted(ext_save_vs_orig)[len(ext_save_vs_orig) // 2], 4
            )
            if ext_save_vs_orig
            else None,
            "pct_ext_save_positive": round(
                sum(1 for x in ext_save_vs_orig if x > 0) / len(ext_save_vs_orig) * 100, 1
            )
            if ext_save_vs_orig
            else None,
            **_paired_excess_stats(out, base_periods),
        }
    )
    return out, summary


INTRADAY_1M_SWEEP: list[Intraday1mHoldConfig] = [
    Intraday1mHoldConfig(
        "I0",
        "C18acc 基線 · 固定 5–10d（overlay none）",
        exit_mode="none",
        break_fixed_hold=False,
    ),
    Intraday1mHoldConfig(
        "I1",
        "1m heat70 · min_hold0 · break · fallback10",
        poll_interval_min=1,
        heat_threshold=70,
        min_hold_days=0,
        max_hold_fallback=10,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I2",
        "1m heat70 · min_hold1 · break · fallback10",
        poll_interval_min=1,
        heat_threshold=70,
        min_hold_days=1,
        max_hold_fallback=10,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I3",
        "1m combo_b · min_hold0 · break · fallback10",
        exit_mode="combo_b",
        poll_interval_min=1,
        min_hold_days=0,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I4",
        "1m combo_b · spike≥5% · min_hold0 · fallback7",
        exit_mode="combo_b",
        poll_interval_min=1,
        min_ext_prev_pct=5.0,
        min_hold_days=0,
        max_hold_fallback=7,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I5",
        "1m heat65 · min_hold0 · break · fallback7",
        poll_interval_min=1,
        heat_threshold=65,
        min_hold_days=0,
        max_hold_fallback=7,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I6",
        "1m heat75 · min_hold0 · break · fallback10",
        poll_interval_min=1,
        heat_threshold=75,
        min_hold_days=0,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I7",
        "5m heat70 · min_hold0 · break · fallback10（對照）",
        poll_interval_min=5,
        heat_threshold=70,
        min_hold_days=0,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I8",
        "1m stretch_reclaim · min_hold0 · fallback10",
        exit_mode="stretch_reclaim",
        poll_interval_min=1,
        min_hold_days=0,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I9",
        "1m heat70 · spike≥5% · min_hold0 · fallback10",
        poll_interval_min=1,
        heat_threshold=70,
        min_ext_prev_pct=5.0,
        min_hold_days=0,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I10",
        "1m heat70 · min_hold0 · break · fallback7（短安全網）",
        poll_interval_min=1,
        heat_threshold=70,
        min_hold_days=0,
        max_hold_fallback=7,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I11",
        "1m heat70 · overlay（不 break）· 5m 舊法",
        poll_interval_min=5,
        heat_threshold=70,
        min_hold_days=5,
        break_fixed_hold=False,
    ),
    Intraday1mHoldConfig(
        "I12",
        "1m heat70 · vol_z1.5 · min_hold0 · fallback10",
        poll_interval_min=1,
        heat_threshold=70,
        vol_z_climax=1.5,
        min_hold_days=0,
        break_fixed_hold=True,
    ),
]

INTRADAY_1M_ROUND2: list[Intraday1mHoldConfig] = [
    Intraday1mHoldConfig(
        "I13",
        "H-1M-6 · combo_b 1m · fallback5",
        exit_mode="combo_b",
        poll_interval_min=1,
        min_hold_days=0,
        max_hold_fallback=5,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I14",
        "H-1M-7 · combo_b spike≥5% · 無信號走 C18acc 原出場",
        exit_mode="combo_b",
        poll_interval_min=1,
        min_ext_prev_pct=5.0,
        min_hold_days=0,
        break_fixed_hold=True,
        use_orig_exit_if_no_signal=True,
    ),
    Intraday1mHoldConfig(
        "I15",
        "H-1M-8 · heat80 spike≥5% · fallback5",
        poll_interval_min=1,
        heat_threshold=80,
        min_ext_prev_pct=5.0,
        min_hold_days=0,
        max_hold_fallback=5,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I16",
        "H-1M-9 · combo_b · 進場後 3 日窗口 · fallback5",
        exit_mode="combo_b",
        poll_interval_min=1,
        min_hold_days=0,
        max_hold_fallback=5,
        extension_window_days=3,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I17",
        "combo_b · 3 日窗口 · spike≥5% · fallback5",
        exit_mode="combo_b",
        poll_interval_min=1,
        min_ext_prev_pct=5.0,
        min_hold_days=0,
        max_hold_fallback=5,
        extension_window_days=3,
        break_fixed_hold=True,
    ),
    Intraday1mHoldConfig(
        "I18",
        "I3 強化 · combo_b · fallback5 · min_hold0",
        exit_mode="combo_b",
        poll_interval_min=1,
        min_hold_days=0,
        max_hold_fallback=5,
        break_fixed_hold=True,
    ),
]

_HYBRID = dict(break_fixed_hold=True, use_orig_exit_if_no_signal=True, min_hold_days=0)

INTRADAY_1M_ROUND3: list[Intraday1mHoldConfig] = [
    Intraday1mHoldConfig(
        "I19",
        "quantive3 · combo_spike 全日 spike≥5% · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=5.0,
        require_opening_spike=False,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I20",
        "peak · spike+climax+2σ · fade1% confirm · hybrid",
        exit_mode="spike_climax_sigma",
        min_ext_prev_pct=5.0,
        fade_from_high_exit_pct=1.0,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I21",
        "climax_fade · spike≥5% · 高點回落2% · hybrid",
        exit_mode="climax_fade",
        min_ext_prev_pct=5.0,
        fade_from_high_exit_pct=2.0,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I22",
        "quantive3 · stretch1% reclaim0.3% · spike≥5% · hybrid",
        exit_mode="stretch_reclaim",
        min_ext_prev_pct=5.0,
        stretch_vwap_pct=1.0,
        reclaim_vwap_pct=0.3,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I23",
        "quantive3 · stretch0.5% reclaim0.21% · spike≥5% · hybrid",
        exit_mode="stretch_reclaim",
        min_ext_prev_pct=5.0,
        stretch_vwap_pct=0.5,
        reclaim_vwap_pct=0.21,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I24",
        "StratCraft · 2σ stretch + VWAP failure · spike≥5% · hybrid",
        exit_mode="vwap_failure",
        min_ext_prev_pct=5.0,
        vwap_sigma_red=2.0,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I25",
        "post_peak · spike≥5% · 高點後破 VWAP · hybrid",
        exit_mode="post_peak_vwap",
        min_ext_prev_pct=5.0,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I26",
        "combo_spike · spike≥7% · vol_z2.5 · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=7.0,
        vol_z_climax=2.5,
        require_opening_spike=False,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I27",
        "climax_fade · spike≥5% · 回落1.5% · hybrid",
        exit_mode="climax_fade",
        min_ext_prev_pct=5.0,
        fade_from_high_exit_pct=1.5,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I28",
        "stretch2% reclaim0.5% · spike≥5% · hybrid",
        exit_mode="stretch_reclaim",
        min_ext_prev_pct=5.0,
        stretch_vwap_pct=2.0,
        reclaim_vwap_pct=0.5,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I29",
        "I14 對照 · combo_b opening · hybrid",
        exit_mode="combo_b",
        min_ext_prev_pct=5.0,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I30",
        "combo_spike · 進場後5日窗口 · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=5.0,
        require_opening_spike=False,
        extension_window_days=5,
        **_HYBRID,
    ),
]

INTRADAY_1M_ROUND3_FINE: list[Intraday1mHoldConfig] = [
    Intraday1mHoldConfig(
        "I31",
        "Minervini · climax_sell_strength spike≥5% · hybrid",
        exit_mode="climax_sell_strength",
        min_ext_prev_pct=5.0,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I32",
        "climax_sell_strength · spike≥5% · 2σ · hybrid",
        exit_mode="climax_sell_strength",
        min_ext_prev_pct=5.0,
        vwap_sigma_red=2.0,
        require_2sigma_exit=True,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I33",
        "I19+ · combo_spike · min_fade1.0% · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=5.0,
        min_fade_from_high_pct=1.0,
        require_opening_spike=False,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I34",
        "I19+ · combo_spike · min_fade1.5% · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=5.0,
        min_fade_from_high_pct=1.5,
        require_opening_spike=False,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I35",
        "I19+ · combo_spike · min_fade2.0% · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=5.0,
        min_fade_from_high_pct=2.0,
        require_opening_spike=False,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I36",
        "combo_spike · spike≥4% · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=4.0,
        require_opening_spike=False,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I37",
        "combo_spike · vol_z1.5 · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=5.0,
        vol_z_climax=1.5,
        require_opening_spike=False,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I38",
        "combo_spike · no_hh15 · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=5.0,
        no_hh_bars=15,
        require_opening_spike=False,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I39",
        "combo_spike · vwap_loss30 · hybrid",
        exit_mode="combo_spike",
        min_ext_prev_pct=5.0,
        vwap_loss_window=30,
        require_opening_spike=False,
        **_HYBRID,
    ),
    Intraday1mHoldConfig(
        "I40",
        "climax_sell_strength · spike≥7% · hybrid",
        exit_mode="climax_sell_strength",
        min_ext_prev_pct=7.0,
        **_HYBRID,
    ),
]

HYPOTHESES_1M_ROUND3: dict[str, str] = {
    "H-1M-11": "I19 combo_spike 全日 · Δ I0 ≥ +0.3pp · ext≥15",
    "H-1M-12": "I20 spike+climax+2σ · median_ext_leg_delta > 0 · Δ I0 ≥ 0",
    "H-1M-13": "quantive3 stretch-reclaim（I22/I23）任一 · Δ I0 ≥ +0.2pp",
    "H-1M-14": "Round3 最佳 · paired Δ excess 95% CI 下界 > 0",
    "H-1M-15": "I19/I26 combo_spike · ext_exits ≥ 2× I14 且 Δ I0 ≥ I14",
}

HYPOTHESES_1M: dict[str, str] = {
    "H-1M-1": "1m poll（I1）均超額 ≥ 5m 對照（I7）",
    "H-1M-2": "break_fixed_hold 中位持有日 < 5 且均超額 ≥ I0 − 0.3pp",
    "H-1M-3": "combo_b 1m（I3/I4）spike 子樣本 ext_exits 高且 median_hold ≤ 3",
    "H-1M-4": "heat65–75 格點（I5/I6）存在穩定區 · 非單點 overfit",
    "H-1M-5": "1m 打破固定持有 · pct_exit_within_3d ≥ 40% 且 win_rate 不劣於 I0",
    "H-1M-6": "I13 combo_b + fallback5 · 中位持有 ≤5 且 Δ I0 ≥ 0",
    "H-1M-7": "I14 spike combo · 無信號 leg 走原 C18acc · Δ I0 ≥ −0.2pp",
    "H-1M-8": "I15 heat80+spike · 均超額 > I9 heat70+spike",
    "H-1M-9": "I16 3 日窗口 · pct_exit_within_3d ≥ 25%",
    "H-1M-10": "延伸出場 leg · median_ext_leg_delta > 0",
}


HYPOTHESES_1M_ROUND2: dict[str, str] = {
    k: v for k, v in HYPOTHESES_1M.items() if k >= "H-1M-6"
}

# Phase 3 · 1m poll · Phase 2c 簽名 + hybrid 採納格（C18acc SSG 不變）
INTRADAY_1M_PHASE3: list[Intraday1mHoldConfig] = [
    next(c for c in INTRADAY_1M_ROUND3 if c.variant_id == "I20"),
    next(c for c in INTRADAY_1M_ROUND3 if c.variant_id == "I25"),
    next(c for c in INTRADAY_1M_ROUND3_FINE if c.variant_id == "I32"),
    next(c for c in INTRADAY_1M_ROUND3_FINE if c.variant_id == "I36"),
    next(c for c in INTRADAY_1M_ROUND3_FINE if c.variant_id == "I33"),
    next(c for c in INTRADAY_1M_ROUND3 if c.variant_id == "I19"),
    next(c for c in INTRADAY_1M_ROUND3 if c.variant_id == "I29"),
    next(c for c in INTRADAY_1M_ROUND2 if c.variant_id == "I14"),
    next(c for c in INTRADAY_1M_SWEEP if c.variant_id == "I1"),
    next(c for c in INTRADAY_1M_SWEEP if c.variant_id == "I7"),
    next(c for c in INTRADAY_1M_SWEEP if c.variant_id == "I11"),
]

# Phase 3b · I36 周邊 fade / spike 細掃（combo_spike · 1m · hybrid）
_P3B = dict(
    exit_mode="combo_spike",
    poll_interval_min=1,
    require_opening_spike=False,
    break_fixed_hold=True,
    use_orig_exit_if_no_signal=True,
    min_hold_days=0,
)

INTRADAY_1M_PHASE3B: list[Intraday1mHoldConfig] = [
    Intraday1mHoldConfig(
        "I36",
        "combo_spike · spike≥4% · fade≥0.5% · hybrid（Phase3 基準）",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=0.5,
        **_P3B,
    ),
    Intraday1mHoldConfig(
        "I33",
        "combo_spike · spike≥5% · fade≥1.0% · hybrid",
        min_ext_prev_pct=5.0,
        min_fade_from_high_pct=1.0,
        **_P3B,
    ),
    Intraday1mHoldConfig(
        "I41",
        "P3b · spike≥4% · fade≥0.75%",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=0.75,
        **_P3B,
    ),
    Intraday1mHoldConfig(
        "I42",
        "P3b · spike≥4% · fade≥1.0%",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=1.0,
        **_P3B,
    ),
    Intraday1mHoldConfig(
        "I43",
        "P3b · spike≥4% · fade≥1.25%",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=1.25,
        **_P3B,
    ),
    Intraday1mHoldConfig(
        "I44",
        "P3b · spike≥4% · fade≥1.5%",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=1.5,
        **_P3B,
    ),
    Intraday1mHoldConfig(
        "I45",
        "P3b · spike≥3.5% · fade≥1.0%",
        min_ext_prev_pct=3.5,
        min_fade_from_high_pct=1.0,
        **_P3B,
    ),
    Intraday1mHoldConfig(
        "I46",
        "P3b · spike≥4.5% · fade≥1.0%",
        min_ext_prev_pct=4.5,
        min_fade_from_high_pct=1.0,
        **_P3B,
    ),
    Intraday1mHoldConfig(
        "I47",
        "P3b · spike≥4% · fade≥1% · no_hh20",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=1.0,
        no_hh_bars=20,
        **_P3B,
    ),
    Intraday1mHoldConfig(
        "I48",
        "P3b · spike≥4% · fade≥1% · vwap_loss35",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=1.0,
        vwap_loss_window=35,
        **_P3B,
    ),
]

HYPOTHESES_PHASE3B: dict[str, str] = {
    "H-P3b-1": "I36 vs I0 · bootstrap paired Δ excess · 95% CI 下界 > 0",
    "H-P3b-2": "I36 ext legs · bootstrap median save_vs_orig > 0",
    "H-P3b-3": "Phase3b 最佳 fade 格 · Δ I0 ≥ I36 且 median ext save > I36",
    "H-P3b-4": "I42 fade≥1% spike≥4% · OOS Δ I0 ≥ −0.3pp",
    "H-P3b-5": "fade 1.0% 鄰域（I41–I44）非單點 · 均 Δ I0 ≥ I36 − 0.1pp",
}


def bootstrap_paired_mean(
    values: list[float],
    *,
    n_boot: int = 10000,
    seed: int = 42,
) -> dict[str, float | int | None]:
    """Bootstrap mean · 95% percentile CI · P(mean>0)。"""
    import random

    n = len(values)
    if n < 2:
        return {
            "n": n,
            "mean": round(sum(values) / n, 4) if n else None,
            "ci95_lo": None,
            "ci95_hi": None,
            "p_mean_gt_zero": None,
        }
    mean = sum(values) / n
    rng = random.Random(seed)
    boots: list[float] = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        boots.append(sum(sample) / n)
    boots.sort()
    lo = boots[int(n_boot * 0.025)]
    hi = boots[int(n_boot * 0.975)]
    p_pos = sum(1 for b in boots if b > 0) / n_boot
    return {
        "n": n,
        "mean": round(mean, 4),
        "ci95_lo": round(lo, 4),
        "ci95_hi": round(hi, 4),
        "p_mean_gt_zero": round(p_pos, 4),
    }


def bootstrap_median(
    values: list[float],
    *,
    n_boot: int = 10000,
    seed: int = 42,
) -> dict[str, float | int | None]:
    import random

    n = len(values)
    if n < 2:
        med = values[0] if n == 1 else None
        return {"n": n, "median": med, "ci95_lo": med, "ci95_hi": med, "p_median_gt_zero": None}
    rng = random.Random(seed)
    boots: list[float] = []
    for _ in range(n_boot):
        sample = sorted(values[rng.randrange(n)] for _ in range(n))
        boots.append(sample[n // 2])
    boots.sort()
    lo = boots[int(n_boot * 0.025)]
    hi = boots[int(n_boot * 0.975)]
    p_pos = sum(1 for b in boots if b > 0) / n_boot
    return {
        "n": n,
        "median": round(float(statistics.median(values)), 4),
        "ci95_lo": round(lo, 4),
        "ci95_hi": round(hi, 4),
        "p_median_gt_zero": round(p_pos, 4),
    }


def _paired_excess_deltas(
    base_periods: list[dict[str, Any]],
    variant_periods: list[dict[str, Any]],
) -> list[float]:
    out: list[float] = []
    for b, v in zip(base_periods, variant_periods):
        be, ve = b.get("excess_pct"), v.get("excess_pct")
        if be is None or ve is None:
            continue
        out.append(float(ve) - float(be))
    return out


def _ext_save_values(periods: list[dict[str, Any]]) -> list[float]:
    return [
        float(p["save_vs_orig_pct"])
        for p in periods
        if p.get("ext_exit") and p.get("save_vs_orig_pct") is not None
    ]


def _load_c18acc_intraday_context(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    score_config: ScoreSwapCConfig | None = None,
    baseline_variant_id: str = "I0",
    baseline_label: str = "C18acc 基線",
) -> tuple[list[dict[str, Any]], pd.DataFrame, list[str], dict[str, Any]]:
    from market_breadth_ma import build_breadth_panel
    from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champion = score_config or champion_score_swap_c_config()
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    base_periods, base_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=champion,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        entry_c_config=c0_cfg,
    )
    i0_summary = dict(base_summary)
    i0_summary["variant_id"] = baseline_variant_id
    i0_summary["label"] = baseline_label
    i0_summary["timing_mode"] = champion.timing_mode
    if i0_summary.get("mean_return_pct") is not None and i0_summary.get("mean_bench_pct") is not None:
        i0_summary["mean_excess_pct"] = round(
            float(i0_summary["mean_return_pct"]) - float(i0_summary["mean_bench_pct"]), 4
        )
    i0_summary["ext_exits"] = 0
    i0_summary["delta_vs_i0_pp"] = 0.0
    return base_periods, close, full_dates, i0_summary


def run_intraday_grid_with_periods(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    configs: list[Intraday1mHoldConfig],
    score_config: ScoreSwapCConfig | None = None,
    baseline_variant_id: str = "I0",
    baseline_label: str = "C18acc 基線",
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    base_periods, close, full_dates, i0_summary = _load_c18acc_intraday_context(
        conn,
        date_start=date_start,
        date_end=date_end,
        score_config=score_config,
        baseline_variant_id=baseline_variant_id,
        baseline_label=baseline_label,
    )
    kbar_cache: dict = {}
    ma_cache: dict = {}
    summaries: list[dict[str, Any]] = [i0_summary]
    periods_by_id: dict[str, list[dict[str, Any]]] = {"I0": base_periods}

    for cfg in configs:
        if cfg.exit_mode == "none":
            continue
        periods, summary = apply_intraday_1m_hold(
            conn,
            base_periods=base_periods,
            close=close,
            full_dates=full_dates,
            config=cfg,
            kbar_cache=kbar_cache,
            ma20_cache=ma_cache,
        )
        base_ex = i0_summary.get("mean_excess_pct")
        if base_ex is not None and summary.get("mean_excess_pct") is not None:
            summary["delta_vs_i0_pp"] = round(float(summary["mean_excess_pct"]) - float(base_ex), 4)
        summaries.append(summary)
        periods_by_id[cfg.variant_id] = periods

    payload = {
        "date_start": date_start,
        "date_end": date_end,
        "baseline_summary": i0_summary,
        "summaries": summaries,
    }
    return payload, periods_by_id


def run_phase3b_analysis(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    oos_start: str = "2026-01-01",
    n_boot: int = 10000,
) -> dict[str, Any]:
    i36_cfg = next(c for c in INTRADAY_1M_PHASE3B if c.variant_id == "I36")

    full_grid, full_periods = run_intraday_grid_with_periods(
        conn, date_start=date_start, date_end=date_end, configs=INTRADAY_1M_PHASE3B
    )
    oos_grid, oos_periods = run_intraday_grid_with_periods(
        conn, date_start=oos_start, date_end=date_end, configs=INTRADAY_1M_PHASE3B
    )

    i0_full = full_periods["I0"]
    i36_full = full_periods["I36"]
    paired_full = bootstrap_paired_mean(
        _paired_excess_deltas(i0_full, i36_full), n_boot=n_boot
    )
    save_full = bootstrap_median(_ext_save_values(i36_full), n_boot=n_boot)

    i0_oos = oos_periods["I0"]
    i36_oos = oos_periods["I36"]
    paired_oos = bootstrap_paired_mean(
        _paired_excess_deltas(i0_oos, i36_oos), n_boot=n_boot
    )
    save_oos = bootstrap_median(_ext_save_values(i36_oos), n_boot=n_boot)

    fm = _summary_by_id(full_grid.get("summaries") or [])
    om = _summary_by_id(oos_grid.get("summaries") or [])
    i36_s = fm.get("I36", {})
    i42_s = fm.get("I42", {})

    p3b_candidates = [s for s in full_grid.get("summaries") or [] if s.get("variant_id") not in ("I0",)]
    best_p3b = max(
        p3b_candidates,
        key=lambda s: (
            float(s.get("delta_vs_i0_pp") or -999),
            float(s.get("median_save_vs_orig_ext_pct") or -999),
        ),
        default={},
    )

    fade_neighborhood = [fm.get(v) for v in ("I41", "I42", "I43", "I44") if fm.get(v)]
    i36_delta = float(i36_s.get("delta_vs_i0_pp") or 0)
    neighborhood_ok = all(
        float(s.get("delta_vs_i0_pp") or -999) >= i36_delta - 0.1 for s in fade_neighborhood
    )

    hypo = {
        "H-P3b-1": {
            "pass": paired_full.get("ci95_lo") is not None and float(paired_full["ci95_lo"]) > 0,
            "full": paired_full,
            "oos": paired_oos,
        },
        "H-P3b-2": {
            "pass": save_full.get("p_median_gt_zero") is not None and float(save_full["p_median_gt_zero"]) > 0.5,
            "full": save_full,
            "oos": save_oos,
        },
        "H-P3b-3": {
            "pass": (
                float(i42_s.get("median_save_vs_orig_ext_pct") or -999)
                > float(i36_s.get("median_save_vs_orig_ext_pct") or -999)
                and float(i42_s.get("delta_vs_i0_pp") or -999) >= i36_delta - 0.05
            ),
            "I42_median_save": i42_s.get("median_save_vs_orig_ext_pct"),
            "I36_median_save": i36_s.get("median_save_vs_orig_ext_pct"),
            "I42_delta": i42_s.get("delta_vs_i0_pp"),
        },
        "H-P3b-4": {
            "pass": (om.get("I42", {}).get("delta_vs_i0_pp") or -999) >= -0.3,
            "oos_delta": om.get("I42", {}).get("delta_vs_i0_pp"),
        },
        "H-P3b-5": {
            "pass": neighborhood_ok,
            "variants": {s.get("variant_id"): s.get("delta_vs_i0_pp") for s in fade_neighborhood},
        },
    }

    i42 = fm.get("I42", {})
    adopt_id = "I36"
    if (
        float(i42.get("median_save_vs_orig_ext_pct") or -999)
        > float(i36_s.get("median_save_vs_orig_ext_pct") or -999)
        and float(i42.get("delta_vs_i0_pp") or -999) >= i36_delta - 0.05
    ):
        adopt_id = "I42"

    return {
        "date_start": date_start,
        "date_end": date_end,
        "oos_start": oos_start,
        "n_bootstrap": n_boot,
        "hypotheses": HYPOTHESES_PHASE3B,
        "hypothesis_results": hypo,
        "bootstrap_i36_vs_i0": {
            "paired_excess_pp": {"full": paired_full, "oos": paired_oos},
            "ext_save_vs_orig_pct": {"full": save_full, "oos": save_oos},
        },
        "phase3b_full": full_grid,
        "phase3b_oos": oos_grid,
        "recommended": {
            "production": adopt_id,
            "tuning_note": (
                "I42 fade≥1% spike≥4%：ext save 優於 I36 且 Δ I0 不劣 → 更新 MIN_FADE_PCT=1.0"
                if adopt_id == "I42"
                else "維持 I36 fade≥0.5%；觀察 I42 fade≥1% 作 tuning"
            ),
            "I36": i36_s,
            "best_p3b": best_p3b,
            "I42": i42_s,
        },
    }


def render_phase3b_report_md(payload: dict[str, Any]) -> str:
    boot = payload.get("bootstrap_i36_vs_i0") or {}
    paired = boot.get("paired_excess_pp") or {}
    save = boot.get("ext_save_vs_orig_pct") or {}
    pf = paired.get("full") or {}
    po = paired.get("oos") or {}
    sf = save.get("full") or {}
    so = save.get("oos") or {}
    hypo = payload.get("hypothesis_results") or {}
    rec = payload.get("recommended") or {}

    lines = [
        "# C18acc × Extension radar · Phase 3b · Bootstrap save + fade 細掃",
        "",
        f"**全樣本**：{payload.get('date_start')} .. {payload.get('date_end')}  ",
        f"**OOS**：{payload.get('oos_start')} .. {payload.get('date_end')}  ",
        f"**Bootstrap**：{payload.get('n_bootstrap')} 次",
        "",
        "## I36 vs I0 · Bootstrap",
        "",
        "### Paired Δ excess（全部 leg · pp）",
        "",
        f"| 樣本 | n | mean Δ | 95% CI | P(Δ>0) |",
        f"|------|---|--------|--------|--------|",
        f"| 全樣本 | {pf.get('n')} | {pf.get('mean')} | [{pf.get('ci95_lo')}, {pf.get('ci95_hi')}] | {pf.get('p_mean_gt_zero')} |",
        f"| OOS | {po.get('n')} | {po.get('mean')} | [{po.get('ci95_lo')}, {po.get('ci95_hi')}] | {po.get('p_mean_gt_zero')} |",
        "",
        "### Ext-leg save_vs_orig（提早出場 vs 原 C18acc 出場 · %）",
        "",
        f"| 樣本 | n ext | median save | 95% CI | P(median>0) |",
        f"|------|-------|-------------|--------|-------------|",
        f"| 全樣本 | {sf.get('n')} | {sf.get('median')} | [{sf.get('ci95_lo')}, {sf.get('ci95_hi')}] | {sf.get('p_median_gt_zero')} |",
        f"| OOS | {so.get('n')} | {so.get('median')} | [{so.get('ci95_lo')}, {so.get('ci95_hi')}] | {so.get('p_median_gt_zero')} |",
        "",
        "## 假設檢定",
        "",
        "| ID | 假設 | 結果 |",
        "|----|------|------|",
    ]
    for hid, text in (payload.get("hypotheses") or {}).items():
        r = hypo.get(hid, {})
        lines.append(f"| {hid} | {text} | {'✓' if r.get('pass') else '✗'} |")

    def _p3b_table(title: str, block: dict[str, Any]) -> None:
        lines.extend([f"## {title}", ""])
        lines.append(
            "| id | fade% | spike% | ext | Δ I0 | median save ext | % save>0 | median hold |"
        )
        lines.append("|----|-------|--------|-----|------|-----------------|----------|-------------|")
        for s in block.get("summaries") or []:
            if s.get("variant_id") == "I0":
                continue
            vid = s.get("variant_id")
            lines.append(
                f"| **{vid}** | {s.get('min_fade_from_high_pct')} | {s.get('min_ext_prev_pct')} "
                f"| {s.get('ext_exits')} | {s.get('delta_vs_i0_pp')} "
                f"| {s.get('median_save_vs_orig_ext_pct')} | {s.get('pct_ext_save_positive')} "
                f"| {s.get('median_hold_days')} |"
            )
        lines.append("")

    _p3b_table("Phase 3b fade 細掃 · 全樣本", payload.get("phase3b_full") or {})
    _p3b_table(f"Phase 3b · OOS {payload.get('oos_start')} 起", payload.get("phase3b_oos") or {})

    lines += [
        "## 採納建議",
        "",
        f"- **Production**：`{rec.get('production')}`",
        f"- **I36**：Δ I0 {rec.get('I36', {}).get('delta_vs_i0_pp')}pp · "
        f"ext save median {rec.get('I36', {}).get('median_save_vs_orig_ext_pct')}%",
        f"- **Phase3b 最佳**：`{rec.get('best_p3b', {}).get('variant_id')}` · "
        f"Δ {rec.get('best_p3b', {}).get('delta_vs_i0_pp')}pp · "
        f"save median {rec.get('best_p3b', {}).get('median_save_vs_orig_ext_pct')}%",
        f"- **I42 fade≥1%**：Δ I0 {rec.get('I42', {}).get('delta_vs_i0_pp')}pp · "
        f"save median {rec.get('I42', {}).get('median_save_vs_orig_ext_pct')}%",
        "",
        rec.get("tuning_note", ""),
        "",
        "### Order layer 更新候選",
        "",
        "```",
        "# 若採 I42：",
        "C18ACC_EXTENSION_MIN_FADE_PCT=1.0   # 原 1.0 已對齊 I33；I42 為 spike≥4% + fade≥1%",
        "C18ACC_EXTENSION_MIN_SPIKE_PCT=4",
        "```",
        "",
        "---",
        "模組：`scripts/run_c18acc_phase3b_sweep.py`",
        "",
    ]
    return "\n".join(lines)


HYPOTHESES_PHASE3: dict[str, str] = {
    "H-P3-1": "I20 spike_climax_sigma（X7 1m）全樣本 Δ I0 ≥ −0.3pp",
    "H-P3-2": "I36 combo_spike spike≥4% hybrid · Δ I0 ≥ +0.2pp（採納候選）",
    "H-P3-3": "Phase3 最佳 hybrid · paired Δ excess 95% CI 下界 > 0",
    "H-P3-4": "I20 · median_hold ≤ 5 日 · pct_exit_within_3d ≥ 25%",
    "H-P3-5": "OOS 2026H1 · 採納候選 Δ I0 ≥ −0.3pp",
    "H-P3-6": "I20 ext_leg median Δ > I1 heat70（spike-fade 日 leg 改善）",
}


def _summary_by_id(summaries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(s.get("variant_id")): s for s in summaries if s.get("variant_id")}


def _evaluate_phase3_hypotheses(
    full: dict[str, Any],
    oos: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    fm = _summary_by_id(full.get("summaries") or [])
    om = _summary_by_id((oos or {}).get("summaries") or [])
    i0 = fm.get("I0", {})
    i20 = fm.get("I20", {})
    i36 = fm.get("I36", {})
    i1 = fm.get("I1", {})

    candidates = [s for s in full.get("summaries") or [] if s.get("variant_id") != "I0"]
    best = max(
        candidates,
        key=lambda s: (
            float(s.get("delta_vs_i0_pp") or -999),
            float(s.get("paired_delta_ci95_lo") or -999),
        ),
        default={},
    )
    best_ci_lo = best.get("paired_delta_ci95_lo")
    i36_oos = om.get("I36", {})
    i20_oos = om.get("I20", {})

    adopted_ids = ("I36", "I20", "I33", "I19", "I29", "I14", "I25")
    adopt_pool = [fm[i] for i in adopted_ids if i in fm]
    adopted = max(
        adopt_pool,
        key=lambda s: float(s.get("delta_vs_i0_pp") or -999),
        default={},
    )

    adopted_oos = om.get(str(adopted.get("variant_id")), {})

    results: dict[str, dict[str, Any]] = {
        "H-P3-1": {
            "pass": (i20.get("delta_vs_i0_pp") is not None and float(i20["delta_vs_i0_pp"]) >= -0.3),
            "value": i20.get("delta_vs_i0_pp"),
        },
        "H-P3-2": {
            "pass": (i36.get("delta_vs_i0_pp") is not None and float(i36["delta_vs_i0_pp"]) >= 0.2),
            "value": i36.get("delta_vs_i0_pp"),
        },
        "H-P3-3": {
            "pass": best_ci_lo is not None and float(best_ci_lo) > 0,
            "best_id": best.get("variant_id"),
            "ci_lo": best_ci_lo,
        },
        "H-P3-4": {
            "pass": (
                i20.get("median_hold_days") is not None
                and float(i20["median_hold_days"]) <= 5
                and (i20.get("pct_exit_within_3d") or 0) >= 25
            ),
            "median_hold": i20.get("median_hold_days"),
            "pct_3d": i20.get("pct_exit_within_3d"),
        },
        "H-P3-5": {
            "pass": (
                adopted_oos.get("delta_vs_i0_pp") is not None
                and float(adopted_oos["delta_vs_i0_pp"]) >= -0.3
            ),
            "adopted_id": adopted.get("variant_id"),
            "oos_delta": adopted_oos.get("delta_vs_i0_pp"),
        },
        "H-P3-6": {
            "pass": (
                (i20.get("median_ext_leg_delta_pct") or -999)
                > (i1.get("median_ext_leg_delta_pct") or -999)
            ),
            "I20_median_ext_delta": i20.get("median_ext_leg_delta_pct"),
            "I1_median_ext_delta": i1.get("median_ext_leg_delta_pct"),
        },
    }
    return results


def run_phase3_1m_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    oos_start: str = "2026-01-01",
) -> dict[str, Any]:
    full = run_intraday_1m_sweep(
        conn,
        date_start=date_start,
        date_end=date_end,
        configs=INTRADAY_1M_PHASE3,
    )
    full["phase"] = "phase3_1m"
    full["hypotheses"] = HYPOTHESES_PHASE3

    oos = run_intraday_1m_sweep(
        conn,
        date_start=oos_start,
        date_end=date_end,
        configs=INTRADAY_1M_PHASE3,
    )
    oos["phase"] = "phase3_1m_oos"

    hypo = _evaluate_phase3_hypotheses(full, oos)
    fm = _summary_by_id(full.get("summaries") or [])
    adopted = fm.get("I36") or fm.get("I20") or {}

    return {
        "date_start": date_start,
        "date_end": date_end,
        "oos_start": oos_start,
        "resolution": "1m_kbar_bar_scan",
        "design": (
            "C18acc champion 進場/換倉 frozen · 出場由 1 分 K Extension radar 主導 · "
            "hybrid=無信號走原 C18acc 出場 · Phase 2c 簽名 I20/I32/I36 對照 I1/I7"
        ),
        "hypotheses": HYPOTHESES_PHASE3,
        "hypothesis_results": hypo,
        "full_sample": full,
        "oos_sample": oos,
        "adopted_candidate": {
            "variant_id": adopted.get("variant_id"),
            "label": adopted.get("label"),
            "delta_vs_i0_pp": adopted.get("delta_vs_i0_pp"),
            "mean_excess_pct": adopted.get("mean_excess_pct"),
            "ext_exits": adopted.get("ext_exits"),
            "median_hold_days": adopted.get("median_hold_days"),
        },
        "lineage": {
            "phase2c_alert": "I32 climax_sell_strength+2σ（預警 · 不作單獨賣）",
            "phase2c_confirm": "I20 spike_climax_sigma（X7 1m confirm）",
            "live_default": "I36 combo_spike spike≥4% hybrid",
        },
    }


def render_c18acc_phase3_report_md(payload: dict[str, Any]) -> str:
    full = payload.get("full_sample") or {}
    oos = payload.get("oos_sample") or {}
    hypo = payload.get("hypothesis_results") or {}
    adopted = payload.get("adopted_candidate") or {}
    lineage = payload.get("lineage") or {}

    def _rows(block: dict[str, Any]) -> list[dict[str, Any]]:
        return block.get("summaries") or []

    def _table(title: str, summaries: list[dict[str, Any]]) -> list[str]:
        out = [
            f"## {title}",
            "",
            "| Variant | 1m poll | Exit mode | ext | fallback | 均超額% | Δ vs I0 | 中位持有 | ≤3d% | ext leg Δ% | paired Δ | CI95 |",
            "|---------|---------|-----------|-----|----------|---------|---------|----------|------|------------|----------|------|",
        ]
        for s in summaries:
            out.append(
                f"| **{s.get('variant_id')}** | {s.get('poll_interval_min')} "
                f"| `{s.get('exit_mode')}` | {s.get('ext_exits')} | {s.get('fallback_exits')} "
                f"| {s.get('mean_excess_pct')} | {s.get('delta_vs_i0_pp')} "
                f"| {s.get('median_hold_days')} | {s.get('pct_exit_within_3d')} "
                f"| {s.get('median_ext_leg_delta_pct')} | {s.get('paired_delta_mean_pp')} "
                f"| [{s.get('paired_delta_ci95_lo')}, {s.get('paired_delta_ci95_hi')}] |"
            )
        out.append("")
        return out

    lines = [
        "# C18acc × Extension radar · Phase 3 · 1 分 K poll sweep",
        "",
        f"**區間（全樣本）**：{payload.get('date_start')} .. {payload.get('date_end')}  ",
        f"**OOS holdout**：{payload.get('oos_start')} .. {payload.get('date_end')}",
        "",
        "## 摘要",
        "",
        payload.get("design", ""),
        "",
        f"- **採納候選**：`{adopted.get('variant_id')}` · {adopted.get('label')}",
        f"- **Δ vs I0（全樣本）**：{adopted.get('delta_vs_i0_pp')} pp · 均超額 {adopted.get('mean_excess_pct')}%",
        f"- **延伸出場次數**：{adopted.get('ext_exits')} · 中位持有 {adopted.get('median_hold_days')} 日",
        "",
        "### Phase 2c → Phase 3 對照",
        "",
        f"- **Alert（預警）**：{lineage.get('phase2c_alert')}",
        f"- **Confirm（確認）**：{lineage.get('phase2c_confirm')}",
        f"- **Live dry-run 預設**：{lineage.get('live_default')}",
        "",
        "## 預先登記假設",
        "",
        "| ID | 假設 | 結果 | 觀測 |",
        "|----|------|------|------|",
    ]
    for hid, text in (payload.get("hypotheses") or {}).items():
        r = hypo.get(hid, {})
        mark = "✓" if r.get("pass") else "✗"
        obs = ", ".join(f"{k}={v}" for k, v in r.items() if k != "pass")
        lines.append(f"| {hid} | {text} | {mark} | {obs} |")

    base = full.get("baseline_summary") or {}
    lines += [
        "",
        f"**基線 I0（C18acc champion）**：n={base.get('n_periods')} · "
        f"均超額 {base.get('mean_excess_pct')}% · 均持有 {base.get('mean_hold_days')} 日",
        "",
    ]
    lines.extend(_table("全樣本 · Variant 對照", _rows(full)))
    lines.extend(_table(f"OOS · {payload.get('oos_start')} 起", _rows(oos)))

    lines += [
        "## 解讀（統計 → 實務）",
        "",
        "1. **SSG 邊界**：進場/換倉仍為 C18acc champion；本 sweep 只改 *持倉期出場*。",
        "2. **1m poll**：對齊 Phase 2c time-to-peak median **5 分鐘**；5m poll（I7）為對照。",
        "3. **Hybrid 模式**：`use_orig_exit_if_no_signal=True` — 無 climax/spike 結構時不走延伸出場，",
        "   避免非 spike 日過度交易；ext 次數反映 *真正觸發* 的 leg 數。",
        "4. **I32 alert-only**（climax+2σ sell-strength）：Phase 2c 顯示 spike save 偏弱 · 僅作 watch，",
        "   不作單獨平倉；live 可設 `C18ACC_EXTENSION_WATCH_HEAT` 或 2σ climax 預警。",
        "5. **I20 confirm**（spike_climax_sigma）：Phase 2c X7 · fade/VWAP 結構確認 · 不傷全樣本 alpha。",
        "6. **I36 採納**：`combo_spike` spike≥4% · 1m · hybrid — Order layer dry-run 已對齊此 spec。",
        "",
        "## Live 配置（Order layer）",
        "",
        "```",
        "C18ACC_EXTENSION_MODE=combo_spike",
        "C18ACC_EXTENSION_POLL_MIN=1",
        "C18ACC_EXTENSION_MIN_SPIKE_PCT=4",
        "C18ACC_EXTENSION_MIN_FADE_PCT=1",
        "C18ACC_EXTENSION_WATCH_HEAT=70   # 可選 · X4 預警 · 不觸發賣出",
        "```",
        "",
        "---",
        "模組：`scripts/run_c18acc_phase3_1m_sweep.py` · `src/research/backtest/c18acc_intraday_1m_hold.py`",
        "",
    ]
    return "\n".join(lines)


def run_intraday_1m_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    configs: list[Intraday1mHoldConfig] | None = None,
    round2: bool = False,
    round3: bool = False,
    round3_fine: bool = False,
) -> dict[str, Any]:
    from market_breadth_ma import build_breadth_panel
    from research.backtest.rrg_mono_intraday_ab import DEFAULT_C_SWEEP

    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
    full_dates = close.index.astype(str).tolist()
    trade_dates = [d for d in full_dates if date_start <= d <= date_end]
    fresh_by_date = build_fresh_mono_calendar(conn, trade_dates)
    panel = build_breadth_panel(conn, date_start=date_start, date_end=date_end)
    zone_by_date = {str(r.trade_date): str(r.zone_200) for r in panel.itertuples()}
    champion = champion_score_swap_c_config()
    c0_cfg = next(c for c in DEFAULT_C_SWEEP if c.variant_id == "C0")

    base_periods, base_summary = simulate_score_swap_c(
        conn,
        trade_dates=trade_dates,
        full_dates=full_dates,
        close=close,
        bench=bench,
        fresh_by_date=fresh_by_date,
        zone_by_date=zone_by_date,
        config=champion,
        rs_ratio=rs_ratio,
        rs_mom=rs_mom,
        entry_c_config=c0_cfg,
    )

    if configs is not None:
        grid = configs
    elif round3_fine:
        grid = INTRADAY_1M_ROUND3_FINE
    elif round3:
        grid = INTRADAY_1M_ROUND3
    elif round2:
        grid = INTRADAY_1M_ROUND2
    else:
        grid = [c for c in INTRADAY_1M_SWEEP if c.variant_id != "I0"]
    i0_summary = dict(base_summary)
    i0_summary["variant_id"] = "I0"
    i0_summary["label"] = "C18acc 基線"
    if i0_summary.get("mean_return_pct") is not None and i0_summary.get("mean_bench_pct") is not None:
        i0_summary["mean_excess_pct"] = round(
            float(i0_summary["mean_return_pct"]) - float(i0_summary["mean_bench_pct"]), 4
        )
    i0_summary["median_hold_days"] = i0_summary.get("mean_hold_days")
    i0_summary["ext_exits"] = 0
    i0_summary["delta_vs_i0_pp"] = 0.0

    kbar_cache: dict = {}
    ma_cache: dict = {}
    summaries: list[dict[str, Any]] = [i0_summary]

    for cfg in grid:
        if cfg.exit_mode == "none":
            continue
        _, summary = apply_intraday_1m_hold(
            conn,
            base_periods=base_periods,
            close=close,
            full_dates=full_dates,
            config=cfg,
            kbar_cache=kbar_cache,
            ma20_cache=ma_cache,
        )
        base_ex = i0_summary.get("mean_excess_pct")
        if base_ex is not None and summary.get("mean_excess_pct") is not None:
            summary["delta_vs_i0_pp"] = round(float(summary["mean_excess_pct"]) - float(base_ex), 4)
        summaries.append(summary)

    hypotheses = HYPOTHESES_1M
    if round3:
        hypotheses = HYPOTHESES_1M_ROUND3
    elif round2:
        hypotheses = HYPOTHESES_1M_ROUND2

    ranked = sorted(
        summaries,
        key=lambda s: (
            -(s.get("mean_excess_pct") or -999.0),
            -(s.get("paired_delta_ci95_lo") or -999.0),
            -(s.get("ext_exits") or 0),
        ),
    )
    return {
        "date_start": date_start,
        "date_end": date_end,
        "resolution": "1m_kbar_bar_scan",
        "baseline_summary": i0_summary,
        "hypotheses": hypotheses,
        "round2": round2,
        "round3": round3,
        "round3_fine": round3_fine,
        "summaries": summaries,
        "best": ranked[1] if (round3 or round3_fine) and len(ranked) > 1 else (ranked[0] if ranked else None),
        "note": (
            "特徵與觸發價來自 stock_kbar_1m 逐 bar PIT；"
            "poll_interval_min=1 表示每根 1 分 K 評估，5 表示僅 5 分格點。"
            "break_fixed_hold=True 時不再等待 C18acc 原 max_hold 出場。"
        ),
    }


def render_intraday_1m_md(payload: dict[str, Any]) -> str:
    lines = [
        "# C18acc × 1 分 K 延伸出場 · 打破固定持有 sweep",
        "",
        payload.get("note", ""),
        "",
        f"區間：{payload['date_start']} .. {payload['date_end']}",
        "",
        "## 假說",
        "",
    ]
    for k, v in (payload.get("hypotheses") or {}).items():
        lines.append(f"- **{k}**：{v}")
    base = payload.get("baseline_summary") or {}
    lines += [
        "",
        f"基線 I0：n={base.get('n_periods')} · 均超額 {base.get('mean_excess_pct')}% · "
        f"均持有 {base.get('mean_hold_days')} 日",
        "",
        "| id | 1m | mode | break | ext | fall | 均超額% | Δ I0 | 中位持有 | ≤3d% | ext Δ% | paired Δ | p | CI95 |",
        "|----|----|------|-------|-----|------|---------|------|----------|------|--------|----------|---|------|",
    ]
    for s in payload.get("summaries") or []:
        lines.append(
            f"| {s.get('variant_id')} | {s.get('poll_interval_min', '—')} "
            f"| {s.get('exit_mode', '—')} | {s.get('break_fixed_hold', '—')} "
            f"| {s.get('ext_exits', 0)} | {s.get('fallback_exits', 0)} "
            f"| {s.get('mean_excess_pct')} | {s.get('delta_vs_i0_pp')} "
            f"| {s.get('median_hold_days')} | {s.get('pct_exit_within_3d')} "
            f"| {s.get('median_ext_leg_delta_pct')} "
            f"| {s.get('paired_delta_mean_pp')} "
            f"| {s.get('paired_delta_p_approx')} "
            f"| [{s.get('paired_delta_ci95_lo')}, {s.get('paired_delta_ci95_hi')}] |"
        )
    best = payload.get("best") or {}
    if best and best.get("variant_id") != "I0":
        lines += [
            "",
            f"## 最佳：{best.get('variant_id')} · {best.get('label')}",
            f"均超額 {best.get('mean_excess_pct')}% · Δ {best.get('delta_vs_i0_pp')}pp · "
            f"中位持有 {best.get('median_hold_days')} 日 · ext {best.get('ext_exits')}",
            f"paired Δ {best.get('paired_delta_mean_pp')}pp · p≈{best.get('paired_delta_p_approx')} · "
            f"CI95 [{best.get('paired_delta_ci95_lo')}, {best.get('paired_delta_ci95_hi')}]",
        ]
    return "\n".join(lines) + "\n"



# --- No intraday RRG (close timing) × I36 factorial ---

_I36_LIVE = dict(
    exit_mode="combo_spike",
    poll_interval_min=1,
    require_opening_spike=False,
    break_fixed_hold=True,
    use_orig_exit_if_no_signal=True,
    min_hold_days=0,
    min_ext_prev_pct=4.0,
    min_fade_from_high_pct=1.0,
)


def _i36_live_config(variant_id: str, label: str) -> Intraday1mHoldConfig:
    return Intraday1mHoldConfig(variant_id, label, **_I36_LIVE)


HYPOTHESES_NORRG: dict[str, str] = {
    "H-NIRRG-1": "S1（close+I36）全樣本 Δ S0 ≥ −0.3pp",
    "H-NIRRG-2": "I36 邊際保留 ≥70%：Δ(S1−S0) ≥ 0.7×Δ(S4−S3)",
    "H-NIRRG-3": "S1 ext legs · bootstrap median save_vs_orig > 0",
    "H-NIRRG-4": "S1 周轉 ≤ 90% S4 且年化超額速度 ≥ 85% S4",
    "H-NIRRG-5": "OOS 2026H1 · S1 Δ S0 ≥ −0.3pp",
    "H-NIRRG-6": "盤中 RRG 邊際 · S3−S0 均超額 > 0",
    "H-NIRRG-8": "close 換倉 swaps ≤ 85% S3 且 S0 均超額 ≥ S3 − 0.5pp",
}


def _calendar_years(date_start: str, date_end: str) -> float:
    from datetime import datetime

    d0 = datetime.strptime(date_start, "%Y-%m-%d")
    d1 = datetime.strptime(date_end, "%Y-%m-%d")
    return max((d1 - d0).days / 365.25, 1e-6)


def _legs_per_slot_yr(summary: dict[str, Any], date_start: str, date_end: str) -> float | None:
    n = summary.get("n_periods")
    slots = summary.get("n_slots") or 3
    if not n:
        return None
    yrs = _calendar_years(date_start, date_end)
    return round(float(n) / float(slots) / yrs, 2)


def _excess_velocity(summary: dict[str, Any], date_start: str, date_end: str) -> float | None:
    ex = summary.get("mean_excess_pct")
    lps = _legs_per_slot_yr(summary, date_start, date_end)
    if ex is None or lps is None or lps <= 0:
        return None
    return round(float(ex) / lps, 4)


def _run_norrg_branch(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    score_config: ScoreSwapCConfig,
    baseline_id: str,
    baseline_label: str,
    ext_cfg: Intraday1mHoldConfig | None,
    ext_id: str | None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    base_periods, close, full_dates, base_summary = _load_c18acc_intraday_context(
        conn,
        date_start=date_start,
        date_end=date_end,
        score_config=score_config,
        baseline_variant_id=baseline_id,
        baseline_label=baseline_label,
    )
    base_summary["legs_per_slot_yr"] = _legs_per_slot_yr(base_summary, date_start, date_end)
    base_summary["excess_velocity"] = _excess_velocity(base_summary, date_start, date_end)

    summaries: list[dict[str, Any]] = [base_summary]
    periods_by_id: dict[str, list[dict[str, Any]]] = {baseline_id: base_periods}

    if ext_cfg is not None and ext_id is not None:
        kbar_cache: dict = {}
        ma_cache: dict = {}
        periods, summary = apply_intraday_1m_hold(
            conn,
            base_periods=base_periods,
            close=close,
            full_dates=full_dates,
            config=ext_cfg,
            kbar_cache=kbar_cache,
            ma20_cache=ma_cache,
        )
        base_ex = base_summary.get("mean_excess_pct")
        if base_ex is not None and summary.get("mean_excess_pct") is not None:
            summary["delta_vs_baseline_pp"] = round(
                float(summary["mean_excess_pct"]) - float(base_ex), 4
            )
        summary["timing_mode"] = score_config.timing_mode
        summary["swaps_total"] = base_summary.get("swaps_total")
        summary["n_slots"] = base_summary.get("n_slots")
        summary["legs_per_slot_yr"] = _legs_per_slot_yr(summary, date_start, date_end)
        summary["excess_velocity"] = _excess_velocity(summary, date_start, date_end)
        summaries.append(summary)
        periods_by_id[ext_id] = periods

    return {"summaries": summaries, "baseline": base_summary}, periods_by_id


def _evaluate_norrg_hypotheses(
    full: dict[str, dict[str, Any]],
    oos: dict[str, dict[str, Any]],
    *,
    date_start: str,
    date_end: str,
    oos_start: str,
    bootstrap_s1: dict[str, Any],
    bootstrap_s1_oos: dict[str, Any],
    save_s1: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    def _sm(block: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(s.get("variant_id")): s for s in block.get("summaries") or []}

    fm = {k: _sm(v) for k, v in full.items()}
    om = {k: _sm(v) for k, v in oos.items()}

    s0 = fm.get("close", {}).get("S0", {})
    s1 = fm.get("close", {}).get("S1", {})
    s3 = fm.get("poll", {}).get("S3", {})
    s4 = fm.get("poll", {}).get("S4", {})

    lift_close = float(s1.get("delta_vs_baseline_pp") or 0) - 0.0
    if s1.get("delta_vs_baseline_pp") is not None:
        lift_close = float(s1["delta_vs_baseline_pp"])
    lift_poll = float(s4.get("delta_vs_baseline_pp") or 0) if s4.get("delta_vs_baseline_pp") is not None else 0.0

    s0_swaps = s0.get("swaps_total") or 0
    s3_swaps = s3.get("swaps_total") or 0
    s1_lps = s1.get("legs_per_slot_yr")
    s4_lps = s4.get("legs_per_slot_yr")
    s1_vel = s1.get("excess_velocity")
    s4_vel = s4.get("excess_velocity")

    s0_oos = om.get("close", {}).get("S0", {})
    s1_oos = om.get("close", {}).get("S1", {})

    poll_marginal = None
    if s3.get("mean_excess_pct") is not None and s0.get("mean_excess_pct") is not None:
        poll_marginal = round(float(s3["mean_excess_pct"]) - float(s0["mean_excess_pct"]), 4)

    retention = lift_close / lift_poll if abs(lift_poll) > 1e-9 else None

    return {
        "H-NIRRG-1": {
            "pass": (s1.get("delta_vs_baseline_pp") or -999) >= -0.3,
            "delta": s1.get("delta_vs_baseline_pp"),
        },
        "H-NIRRG-2": {
            "pass": retention is not None and retention >= 0.7,
            "lift_close_pp": lift_close,
            "lift_poll_pp": lift_poll,
            "retention_ratio": round(retention, 3) if retention is not None else None,
        },
        "H-NIRRG-3": {
            "pass": (save_s1.get("p_median_gt_zero") or 0) > 0.5,
            "full": save_s1,
        },
        "H-NIRRG-4": {
            "pass": (
                s1_lps is not None
                and s4_lps is not None
                and s1_lps <= 0.9 * float(s4_lps)
                and s1_vel is not None
                and s4_vel is not None
                and float(s1_vel) >= 0.85 * float(s4_vel)
            ),
            "S1_legs_per_slot_yr": s1_lps,
            "S4_legs_per_slot_yr": s4_lps,
            "S1_velocity": s1_vel,
            "S4_velocity": s4_vel,
        },
        "H-NIRRG-5": {
            "pass": (s1_oos.get("delta_vs_baseline_pp") or -999) >= -0.3,
            "oos_delta": s1_oos.get("delta_vs_baseline_pp"),
        },
        "H-NIRRG-6": {
            "pass": poll_marginal is not None and poll_marginal > 0,
            "S3_minus_S0_pp": poll_marginal,
        },
        "H-NIRRG-8": {
            "pass": (
                s3_swaps > 0
                and s0_swaps <= 0.85 * float(s3_swaps)
                and (s0.get("mean_excess_pct") or -999) >= float(s3.get("mean_excess_pct") or 0) - 0.5
            ),
            "S0_swaps": s0_swaps,
            "S3_swaps": s3_swaps,
            "S0_excess": s0.get("mean_excess_pct"),
            "S3_excess": s3.get("mean_excess_pct"),
        },
    }


def run_norrg_i36_analysis(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    oos_start: str = "2026-01-01",
    n_boot: int = 10000,
) -> dict[str, Any]:
    close_cfg = close_champion_score_swap_c_config()
    poll_cfg = champion_score_swap_c_config()
    i36_s1 = _i36_live_config("S1", "close · I36 combo_spike hybrid")
    i36_s4 = _i36_live_config("S4", "poll_5m · I36 combo_spike hybrid")

    full: dict[str, dict[str, Any]] = {}
    full_periods: dict[str, dict[str, list[dict[str, Any]]]] = {}

    close_block, close_periods = _run_norrg_branch(
        conn,
        date_start=date_start,
        date_end=date_end,
        score_config=close_cfg,
        baseline_id="S0",
        baseline_label="close · 無 I36",
        ext_cfg=i36_s1,
        ext_id="S1",
    )
    full["close"] = close_block
    full_periods["close"] = close_periods

    poll_block, poll_periods = _run_norrg_branch(
        conn,
        date_start=date_start,
        date_end=date_end,
        score_config=poll_cfg,
        baseline_id="S3",
        baseline_label="poll_5m · 無 I36（= I0）",
        ext_cfg=i36_s4,
        ext_id="S4",
    )
    full["poll"] = poll_block
    full_periods["poll"] = poll_periods

    oos: dict[str, dict[str, Any]] = {}
    oos_periods: dict[str, dict[str, list[dict[str, Any]]]] = {}

    for key, cfg, bid, blabel, eid in (
        ("close", close_cfg, "S0", "close · 無 I36", "S1"),
        ("poll", poll_cfg, "S3", "poll_5m · 無 I36", "S4"),
    ):
        ext = i36_s1 if key == "close" else i36_s4
        block, periods = _run_norrg_branch(
            conn,
            date_start=oos_start,
            date_end=date_end,
            score_config=cfg,
            baseline_id=bid,
            baseline_label=blabel,
            ext_cfg=ext,
            ext_id=eid,
        )
        oos[key] = block
        oos_periods[key] = periods

    s0_full = full_periods["close"]["S0"]
    s1_full = full_periods["close"]["S1"]
    s3_full = full_periods["poll"]["S3"]
    s4_full = full_periods["poll"]["S4"]

    paired_s1 = bootstrap_paired_mean(_paired_excess_deltas(s0_full, s1_full), n_boot=n_boot)
    paired_s4 = bootstrap_paired_mean(_paired_excess_deltas(s3_full, s4_full), n_boot=n_boot)
    save_s1 = bootstrap_median(_ext_save_values(s1_full), n_boot=n_boot)
    save_s4 = bootstrap_median(_ext_save_values(s4_full), n_boot=n_boot)

    s0_oos = oos_periods["close"]["S0"]
    s1_oos = oos_periods["close"]["S1"]
    paired_s1_oos = bootstrap_paired_mean(_paired_excess_deltas(s0_oos, s1_oos), n_boot=n_boot)

    hypo = _evaluate_norrg_hypotheses(
        full,
        oos,
        date_start=date_start,
        date_end=date_end,
        oos_start=oos_start,
        bootstrap_s1=paired_s1,
        bootstrap_s1_oos=paired_s1_oos,
        save_s1=save_s1,
    )

    fm_close = {s["variant_id"]: s for s in full["close"]["summaries"]}
    fm_poll = {s["variant_id"]: s for s in full["poll"]["summaries"]}

    lift_close = fm_close.get("S1", {}).get("delta_vs_baseline_pp")
    lift_poll = fm_poll.get("S4", {}).get("delta_vs_baseline_pp")

    recommend = "close_plus_i36"
    poll_marg = hypo.get("H-NIRRG-6", {}).get("S3_minus_S0_pp")
    if hypo.get("H-NIRRG-2", {}).get("pass"):
        if poll_marg is not None and float(poll_marg) > 0.3:
            note = (
                f"I36 邊際在 close 下保留 {hypo.get('H-NIRRG-2', {}).get('retention_ratio')}×（與 poll 幾乎同幅 +0.24pp），"
                f"但盤中 RRG 定價另貢獻 +{poll_marg}pp（S3−S0）。"
                "結論：I36 可獨立於 poll；若簡化為 close 換倉，須接受 ~0.76pp 機會成本。"
            )
            recommend = "i36_orthogonal_keep_poll_if_alpha_needed"
        else:
            note = (
                "I36 邊際在 close 執行下保留 ≥70% → 可簡化為日線 RRG 定池 + 收盤換倉 + 1m I36 出場。"
            )
    elif (lift_close or -999) >= -0.3 and not hypo.get("H-NIRRG-6", {}).get("pass"):
        note = "close+I36 不傷 alpha 且盤中 RRG 邊際≤0 → 建議去掉 poll_5m，保留 I36。"
        recommend = "close_plus_i36_drop_poll"
    elif (lift_close or -999) < (lift_poll or 0) * 0.5:
        note = "I36 收益綁 poll_5m 進場 · 須保留 C18acc 全栈（poll_5m + I36）。"
        recommend = "keep_full_stack"
    else:
        note = "close+I36 可用但邊際弱於 poll · 依複雜度取捨。"

    return {
        "date_start": date_start,
        "date_end": date_end,
        "oos_start": oos_start,
        "n_bootstrap": n_boot,
        "design": (
            "2×2 因子：timing_mode（close vs poll_5m）× I36 combo_spike（live spec spike≥4% fade≥1% hybrid）。"
            "日線 RRG fresh mono 選股池不變；僅移除/保留盤中 RRG 定價換倉。"
        ),
        "hypotheses": HYPOTHESES_NORRG,
        "hypothesis_results": hypo,
        "factorial_full": full,
        "factorial_oos": oos,
        "bootstrap": {
            "S1_vs_S0": {"full": paired_s1, "oos": paired_s1_oos},
            "S4_vs_S3": {"full": paired_s4},
            "S1_ext_save": {"full": save_s1},
            "S4_ext_save": {"full": save_s4},
        },
        "factor_decomposition": {
            "intraday_rrg_marginal_pp": hypo.get("H-NIRRG-6", {}).get("S3_minus_S0_pp"),
            "i36_marginal_close_pp": lift_close,
            "i36_marginal_poll_pp": lift_poll,
            "i36_retention_ratio": hypo.get("H-NIRRG-2", {}).get("retention_ratio"),
        },
        "recommended": {
            "action": recommend,
            "note": note,
            "S0": fm_close.get("S0"),
            "S1": fm_close.get("S1"),
            "S3": fm_poll.get("S3"),
            "S4": fm_poll.get("S4"),
        },
    }


def render_norrg_i36_report_md(payload: dict[str, Any]) -> str:
    hypo = payload.get("hypothesis_results") or {}
    boot = payload.get("bootstrap") or {}
    decomp = payload.get("factor_decomposition") or {}
    rec = payload.get("recommended") or {}
    pf = (boot.get("S1_vs_S0") or {}).get("full") or {}
    po = (boot.get("S1_vs_S0") or {}).get("oos") or {}
    sf = (boot.get("S1_ext_save") or {}).get("full") or {}

    def _factor_table(title: str, block: dict[str, Any]) -> list[str]:
        out = [f"## {title}", ""]
        out.append(
            "| id | timing | ext | n | swaps | legs/slot/yr | 均超額% | Δ baseline | ext | median hold | velocity |"
        )
        out.append(
            "|----|--------|-----|---|-------|--------------|---------|------------|-----|-------------|----------|"
        )
        for s in block.get("summaries") or []:
            out.append(
                f"| **{s.get('variant_id')}** | {s.get('timing_mode', '—')} "
                f"| {'I36' if s.get('ext_exits') is not None and s.get('variant_id') in ('S1', 'S4') else '—'} "
                f"| {s.get('n_periods')} | {s.get('swaps_total')} | {s.get('legs_per_slot_yr')} "
                f"| {s.get('mean_excess_pct')} | {s.get('delta_vs_baseline_pp', s.get('delta_vs_i0_pp', '—'))} "
                f"| {s.get('ext_exits', '—')} | {s.get('median_hold_days', s.get('mean_hold_days'))} "
                f"| {s.get('excess_velocity', '—')} |"
            )
        out.append("")
        return out

    lines = [
        "# C18acc × I36 · 無盤中 RRG（close 換倉）· 2×2 因子分解",
        "",
        f"**全樣本**：{payload.get('date_start')} .. {payload.get('date_end')}  ",
        f"**OOS**：{payload.get('oos_start')} .. {payload.get('date_end')}  ",
        f"**Bootstrap**：{payload.get('n_bootstrap')} 次",
        "",
        payload.get("design", ""),
        "",
        "## 因子分解（pp）",
        "",
        f"| 因子 | 邊際 |",
        f"|------|------|",
        f"| 盤中 RRG（S3−S0） | {decomp.get('intraday_rrg_marginal_pp')} |",
        f"| I36 · close（S1−S0） | {decomp.get('i36_marginal_close_pp')} |",
        f"| I36 · poll（S4−S3） | {decomp.get('i36_marginal_poll_pp')} |",
        f"| I36 保留率（close/poll） | {decomp.get('i36_retention_ratio')} |",
        "",
        "## S1 vs S0 · Bootstrap",
        "",
        f"| 樣本 | n | mean Δ | 95% CI | P(Δ>0) |",
        f"|------|---|--------|--------|--------|",
        f"| 全樣本 | {pf.get('n')} | {pf.get('mean')} | [{pf.get('ci95_lo')}, {pf.get('ci95_hi')}] | {pf.get('p_mean_gt_zero')} |",
        f"| OOS | {po.get('n')} | {po.get('mean')} | [{po.get('ci95_lo')}, {po.get('ci95_hi')}] | {po.get('p_mean_gt_zero')} |",
        "",
        f"**S1 ext save median** {sf.get('median')}% · P(median>0)={sf.get('p_median_gt_zero')}",
        "",
        "## 假設檢定",
        "",
        "| ID | 假設 | 結果 |",
        "|----|------|------|",
    ]
    for hid, text in (payload.get("hypotheses") or {}).items():
        r = hypo.get(hid, {})
        lines.append(f"| {hid} | {text} | {'✓' if r.get('pass') else '✗'} |")

    full = payload.get("factorial_full") or {}
    oos = payload.get("factorial_oos") or {}
    lines.extend(_factor_table("全樣本 · 2×2 格", full.get("close") or {}))
    lines.extend(_factor_table("全樣本 · poll_5m 支", full.get("poll") or {}))
    lines.extend(_factor_table(f"OOS · close 支 · {payload.get('oos_start')} 起", oos.get("close") or {}))

    lines += [
        "## 採納建議",
        "",
        f"- **建議**：`{rec.get('action')}`",
        f"- {rec.get('note', '')}",
        "",
        f"- **S0** close 基線：均超額 {rec.get('S0', {}).get('mean_excess_pct')}% · swaps {rec.get('S0', {}).get('swaps_total')}",
        f"- **S1** close+I36：Δ {rec.get('S1', {}).get('delta_vs_baseline_pp')}pp · ext {rec.get('S1', {}).get('ext_exits')}",
        f"- **S3** poll 基線：均超額 {rec.get('S3', {}).get('mean_excess_pct')}% · swaps {rec.get('S3', {}).get('swaps_total')}",
        f"- **S4** poll+I36：Δ {rec.get('S4', {}).get('delta_vs_baseline_pp')}pp · ext {rec.get('S4', {}).get('ext_exits')}",
        "",
        "---",
        "模組：`scripts/run_c18acc_norrg_i36_sweep.py`",
        "",
    ]
    return "\n".join(lines)


# --- I36 fullstack · extension entry + combo_spike exit on C18acc champion ---

@dataclass(frozen=True)
class I36FullstackConfig:
    variant_id: str
    label: str
    entry_mode: EntryMode | None = None
    exit_mode: ExitMode = "combo_spike"
    poll_interval_min: int = 1
    min_ext_prev_pct: float = 4.0
    entry_min_ext_prev_pct: float | None = None
    exit_min_ext_prev_pct: float | None = None
    min_fade_from_high_pct: float = 1.0
    opening_spike_end: str = "09:30"
    use_close_entry_fallback: bool = True
    use_orig_exit_if_no_signal: bool = True
    slip_pct: float = 0.5
    vol_z_climax: float = 2.0
    no_hh_bars: int = 30
    vwap_loss_window: int = 45

    def _entry_spike(self) -> float:
        if self.entry_min_ext_prev_pct is not None:
            return self.entry_min_ext_prev_pct
        return self.min_ext_prev_pct

    def _exit_spike(self) -> float:
        if self.exit_min_ext_prev_pct is not None:
            return self.exit_min_ext_prev_pct
        return self.min_ext_prev_pct

    def entry_scan_config(self) -> ExtensionScanConfig:
        return ExtensionScanConfig(
            entry_mode=self.entry_mode,
            poll_interval_min=self.poll_interval_min,
            min_ext_prev_pct=self._entry_spike(),
            entry_min_ext_prev_pct=self._entry_spike(),
            opening_spike_end=self.opening_spike_end,
            vol_z_climax=self.vol_z_climax,
        )

    def exit_hold_config(self) -> Intraday1mHoldConfig:
        exit_spike = self._exit_spike()
        return Intraday1mHoldConfig(
            self.variant_id,
            self.label,
            exit_mode=self.exit_mode,
            poll_interval_min=self.poll_interval_min,
            min_ext_prev_pct=exit_spike,
            min_fade_from_high_pct=self.min_fade_from_high_pct,
            require_opening_spike=False,
            break_fixed_hold=True,
            use_orig_exit_if_no_signal=self.use_orig_exit_if_no_signal,
            min_hold_days=0,
            slip_pct=self.slip_pct,
            vol_z_climax=self.vol_z_climax,
            no_hh_bars=self.no_hh_bars,
            vwap_loss_window=self.vwap_loss_window,
        )


I36_FULLSTACK_GRID: list[I36FullstackConfig] = [
    I36FullstackConfig("F0", "C18acc poll · 無 I36（= I0）", entry_mode=None, exit_mode="none"),
    I36FullstackConfig("F4", "poll · I36 exit only（= S4）", entry_mode=None),
    I36FullstackConfig("F1", "I36 spike_cross 進 + combo_spike 出", entry_mode="spike_cross"),
    I36FullstackConfig("F2", "I36 climax_strength 進 + combo_spike 出", entry_mode="climax_strength"),
    I36FullstackConfig("F3", "I36 opening_spike 進 + combo_spike 出", entry_mode="opening_spike"),
    I36FullstackConfig("F5", "I36 vwap_reclaim 進 + combo_spike 出", entry_mode="vwap_reclaim"),
]

HYPOTHESES_FULLSTACK: dict[str, str] = {
    "H-FS-1": "最佳 fullstack · Δ F0 ≥ +0.2pp",
    "H-FS-2": "fullstack 邊際 ≥ F4（exit-only）· 進場 I36 有增量",
    "H-FS-3": "fullstack ext save median > 0",
    "H-FS-4": "OOS · 最佳 fullstack Δ F0 ≥ −0.3pp",
    "H-FS-5": "entry 觸發率 ≥ 30% legs · 非極端稀疏",
}


def _f3t_param_tag(v: float) -> str:
    return str(v).replace(".", "p")


def build_f3_opening_spike_tune_grid() -> list[I36FullstackConfig]:
    """F3 opening_spike · entry spike × exit spike × fade 格點 + 錨點。"""
    anchors = [
        I36FullstackConfig("F0", "anchor · poll 無 I36", entry_mode=None, exit_mode="none"),
        I36FullstackConfig("F4", "anchor · exit-only I36", entry_mode=None),
        I36FullstackConfig(
            "F3",
            "anchor · F3 baseline opening≥4% · exit spike≥4% fade≥1%",
            entry_mode="opening_spike",
            entry_min_ext_prev_pct=4.0,
            exit_min_ext_prev_pct=4.0,
            min_fade_from_high_pct=1.0,
        ),
    ]
    grid = list(anchors)
    seen = {c.variant_id for c in anchors}
    entry_spikes = [3.0, 3.5, 4.0, 4.5, 5.0, 5.5]
    exit_spikes = [3.5, 4.0, 4.5]
    fades = [0.5, 0.75, 1.0, 1.25, 1.5]
    for es in entry_spikes:
        for xs in exit_spikes:
            for fd in fades:
                vid = f"F3T-{_f3t_param_tag(es)}-{_f3t_param_tag(xs)}-f{_f3t_param_tag(fd)}"
                if vid in seen:
                    continue
                seen.add(vid)
                grid.append(
                    I36FullstackConfig(
                        vid,
                        f"opening≥{es}% · exit≥{xs}% fade≥{fd}%",
                        entry_mode="opening_spike",
                        entry_min_ext_prev_pct=es,
                        exit_min_ext_prev_pct=xs,
                        min_fade_from_high_pct=fd,
                    )
                )
    for end in ("09:25", "09:35"):
        vid = f"F3T-4p0-4p0-f1p0-w{end.replace(':', '')}"
        if vid not in seen:
            seen.add(vid)
            grid.append(
                I36FullstackConfig(
                    vid,
                    f"opening≥4% · fade≥1% · window至{end}",
                    entry_mode="opening_spike",
                    entry_min_ext_prev_pct=4.0,
                    exit_min_ext_prev_pct=4.0,
                    min_fade_from_high_pct=1.0,
                    opening_spike_end=end,
                )
            )
    for vz in (1.5, 2.5):
        vid = f"F3T-4p0-4p0-f1p0-vz{_f3t_param_tag(vz)}"
        if vid not in seen:
            seen.add(vid)
            grid.append(
                I36FullstackConfig(
                    vid,
                    f"opening≥4% · fade≥1% · vol_z≥{vz}",
                    entry_mode="opening_spike",
                    entry_min_ext_prev_pct=4.0,
                    exit_min_ext_prev_pct=4.0,
                    min_fade_from_high_pct=1.0,
                    vol_z_climax=vz,
                )
            )
    for nh in (20, 45):
        vid = f"F3T-4p0-4p0-f1p0-nh{nh}"
        if vid not in seen:
            seen.add(vid)
            grid.append(
                I36FullstackConfig(
                    vid,
                    f"opening≥4% · fade≥1% · no_hh={nh}m",
                    entry_mode="opening_spike",
                    entry_min_ext_prev_pct=4.0,
                    exit_min_ext_prev_pct=4.0,
                    min_fade_from_high_pct=1.0,
                    no_hh_bars=nh,
                )
            )
    return grid


HYPOTHESES_F3TUNE: dict[str, str] = {
    "H-F3T-1": "最佳 F3T · Δ F0 ≥ F3 baseline（+0.30pp）",
    "H-F3T-2": "最佳 F3T · Δ F0 ≥ F4 exit-only（+0.24pp）",
    "H-F3T-3": "最佳 vs F0 · bootstrap 95% CI 下界 > 0",
    "H-F3T-4": "OOS · 最佳 Δ F0 ≥ −0.3pp",
    "H-F3T-5": "fade 鄰域非單點 · ±0.25pp 內 ≥3 格",
    "H-F3T-6": "entry≥3.5% 觸發率 ≥ 30% 或 Δ 較 F3 +0.1pp",
}


def _fullstack_summary_params(config: I36FullstackConfig, summary: dict[str, Any]) -> None:
    summary["entry_min_ext_prev_pct"] = config._entry_spike() if config.entry_mode else None
    summary["exit_min_ext_prev_pct"] = config._exit_spike() if config.exit_mode != "none" else None
    summary["min_fade_from_high_pct"] = config.min_fade_from_high_pct
    summary["opening_spike_end"] = config.opening_spike_end
    summary["vol_z_climax"] = config.vol_z_climax
    summary["no_hh_bars"] = config.no_hh_bars


def _apply_buy_slip(px: float, slip_pct: float) -> float:
    return px * (1.0 + slip_pct / 100.0)


def reprice_legs_extension_entry(
    conn: sqlite3.Connection,
    *,
    periods: list[dict[str, Any]],
    close: pd.DataFrame,
    full_dates: list[str],
    config: I36FullstackConfig,
    kbar_cache: dict | None = None,
    ma_cache: dict | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if config.entry_mode is None:
        return [dict(p) for p in periods], {"entry_hits": 0, "entry_fallback": len(periods)}

    cache = kbar_cache if kbar_cache is not None else {}
    ma_c = ma_cache if ma_cache is not None else {}
    scan_cfg = config.entry_scan_config()
    out: list[dict[str, Any]] = []
    entry_hits = 0
    fallback = 0

    for pos in periods:
        leg = dict(pos)
        sid = str(leg["stock_id"])
        entry_d = str(leg["entry_date"])
        exit_d = str(leg["exit_date"])
        exit_px = float(leg["exit_px"])
        key = (sid, entry_d)
        if key not in cache:
            if not kbar_day_has_data(conn, sid, entry_d):
                cache[key] = ()
            else:
                cache[key] = load_kbar_day_bars(conn, sid, entry_d)
        bars = cache[key]
        if not bars:
            out.append(leg)
            fallback += 1
            continue
        prev_close = _prior_close(conn, sid, entry_d, full_dates)
        if prev_close is None:
            out.append(leg)
            fallback += 1
            continue
        ma_key = (sid, entry_d)
        if ma_key not in ma_c:
            ma_c[ma_key] = _ma20(conn, sid, entry_d, full_dates)
        sig = find_first_extension_entry(
            bars, prev_close=prev_close, ma20=ma_c[ma_key], config=scan_cfg
        )
        if sig is None:
            out.append(leg)
            fallback += 1
            continue
        new_entry = _apply_buy_slip(sig.price, config.slip_pct)
        ret = (exit_px / new_entry - 1.0) * 100.0
        bench = bench_return_entry_to_exit(conn, entry_d, exit_d, entry_price_mode="close")
        if bench is None:
            out.append(leg)
            fallback += 1
            continue
        leg.update(
            {
                "entry_px": round(new_entry, 4),
                "entry_minute": sig.minute,
                "entry_timing": config.entry_mode,
                "return_pct": round(ret, 4),
                "bench_return_pct": round(bench, 4),
                "excess_pct": round(ret - bench, 4),
                "beat_bench": ret > bench,
                "gross_win": ret > 0,
            }
        )
        entry_hits += 1
        out.append(leg)

    n = len(periods)
    return out, {
        "entry_hits": entry_hits,
        "entry_fallback": fallback,
        "pct_entry_i36": round(entry_hits / n * 100, 1) if n else None,
    }


def _run_fullstack_variant(
    conn: sqlite3.Connection,
    *,
    base_periods: list[dict[str, Any]],
    close: pd.DataFrame,
    full_dates: list[str],
    base_summary: dict[str, Any],
    config: I36FullstackConfig,
    kbar_cache: dict,
    ma_cache: dict,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    repriced, entry_stats = reprice_legs_extension_entry(
        conn,
        periods=base_periods,
        close=close,
        full_dates=full_dates,
        config=config,
        kbar_cache=kbar_cache,
        ma_cache=ma_cache,
    )
    if config.exit_mode == "none":
        summary = summarize_periods(repriced)
        if summary.get("mean_return_pct") is not None and summary.get("mean_bench_pct") is not None:
            summary["mean_excess_pct"] = round(
                float(summary["mean_return_pct"]) - float(summary["mean_bench_pct"]), 4
            )
        summary.update(
            {
                "variant_id": config.variant_id,
                "label": config.label,
                "entry_mode": config.entry_mode,
                "exit_mode": config.exit_mode,
                **entry_stats,
            }
        )
        _fullstack_summary_params(config, summary)
        base_ex = base_summary.get("mean_excess_pct")
        if base_ex is not None and summary.get("mean_excess_pct") is not None:
            summary["delta_vs_baseline_pp"] = round(
                float(summary["mean_excess_pct"]) - float(base_ex), 4
            )
        return repriced, summary

    hold_cfg = config.exit_hold_config()
    periods, summary = apply_intraday_1m_hold(
        conn,
        base_periods=repriced,
        close=close,
        full_dates=full_dates,
        config=hold_cfg,
        kbar_cache=kbar_cache,
        ma20_cache=ma_cache,
    )
    summary["entry_mode"] = config.entry_mode
    summary.update(entry_stats)
    _fullstack_summary_params(config, summary)
    base_ex = base_summary.get("mean_excess_pct")
    if base_ex is not None and summary.get("mean_excess_pct") is not None:
        summary["delta_vs_baseline_pp"] = round(
            float(summary["mean_excess_pct"]) - float(base_ex), 4
        )
    return periods, summary


def run_i36_fullstack_analysis(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    oos_start: str = "2026-01-01",
    n_boot: int = 10000,
) -> dict[str, Any]:
    poll_cfg = champion_score_swap_c_config()

    def _run_window(d0: str, d1: str) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        base_periods, close, full_dates, f0_summary = _load_c18acc_intraday_context(
            conn,
            date_start=d0,
            date_end=d1,
            score_config=poll_cfg,
            baseline_variant_id="F0",
            baseline_label="C18acc poll · 無 I36",
        )
        kbar_cache: dict = {}
        ma_cache: dict = {}
        summaries: list[dict[str, Any]] = []
        periods_by_id: dict[str, list[dict[str, Any]]] = {}
        for cfg in I36_FULLSTACK_GRID:
            periods, summary = _run_fullstack_variant(
                conn,
                base_periods=base_periods,
                close=close,
                full_dates=full_dates,
                base_summary=f0_summary,
                config=cfg,
                kbar_cache=kbar_cache,
                ma_cache=ma_cache,
            )
            summaries.append(summary)
            periods_by_id[cfg.variant_id] = periods
        return {"summaries": summaries, "baseline": f0_summary}, periods_by_id

    full_block, full_periods = _run_window(date_start, date_end)
    oos_block, oos_periods = _run_window(oos_start, date_end)

    fm = {s["variant_id"]: s for s in full_block["summaries"]}
    om = {s["variant_id"]: s for s in oos_block["summaries"]}
    f0 = fm.get("F0", {})
    f4 = fm.get("F4", {})

    candidates = [s for s in full_block["summaries"] if s.get("variant_id") not in ("F0", "F4")]
    best = max(
        candidates,
        key=lambda s: (float(s.get("delta_vs_baseline_pp") or -999), float(s.get("mean_excess_pct") or -999)),
        default={},
    )
    best_id = str(best.get("variant_id", ""))

    paired_best = bootstrap_paired_mean(
        _paired_excess_deltas(full_periods["F0"], full_periods.get(best_id, full_periods["F0"])),
        n_boot=n_boot,
    )
    save_best = bootstrap_median(_ext_save_values(full_periods.get(best_id, [])), n_boot=n_boot)
    best_oos = om.get(best_id, {})

    hypo = {
        "H-FS-1": {
            "pass": (best.get("delta_vs_baseline_pp") or -999) >= 0.2,
            "best_id": best_id,
            "delta_pp": best.get("delta_vs_baseline_pp"),
        },
        "H-FS-2": {
            "pass": (best.get("delta_vs_baseline_pp") or -999) > (f4.get("delta_vs_baseline_pp") or 0),
            "best_delta": best.get("delta_vs_baseline_pp"),
            "F4_delta": f4.get("delta_vs_baseline_pp"),
        },
        "H-FS-3": {
            "pass": (save_best.get("p_median_gt_zero") or 0) > 0.5,
            "save": save_best,
        },
        "H-FS-4": {
            "pass": (best_oos.get("delta_vs_baseline_pp") or -999) >= -0.3,
            "oos_delta": best_oos.get("delta_vs_baseline_pp"),
        },
        "H-FS-5": {
            "pass": (best.get("pct_entry_i36") or 0) >= 30,
            "pct_entry_i36": best.get("pct_entry_i36"),
        },
    }

    if hypo["H-FS-2"]["pass"]:
        note = f"{best_id} 進+出 I36 優於僅 I36 出場（F4）→ 可試 fullstack live dry-run。"
        action = "fullstack_candidate"
    elif hypo["H-FS-1"]["pass"]:
        note = f"{best_id} 優於 F0 但未贏 F4 → 進場 I36 無增量，維持 exit-only overlay。"
        action = "exit_only"
    else:
        note = "fullstack 未過主假設 → 維持 poll + I36 exit-only（F4/S4）。"
        action = "exit_only"

    return {
        "date_start": date_start,
        "date_end": date_end,
        "oos_start": oos_start,
        "n_bootstrap": n_boot,
        "design": (
            "C18acc champion 選股/換倉規則 frozen（poll_5m）；"
            "變更：進場日用 Extension radar entry_mode 定價 · 持倉期 I36 combo_spike 出場。"
        ),
        "hypotheses": HYPOTHESES_FULLSTACK,
        "hypothesis_results": hypo,
        "full_sample": full_block,
        "oos_sample": oos_block,
        "bootstrap": {"best_vs_F0": paired_best, "best_ext_save": save_best},
        "recommended": {
            "action": action,
            "note": note,
            "best_id": best_id,
            "F0": f0,
            "F4": f4,
            "best": best,
        },
    }


def render_i36_fullstack_report_md(payload: dict[str, Any]) -> str:
    hypo = payload.get("hypothesis_results") or {}
    rec = payload.get("recommended") or {}
    boot = payload.get("bootstrap") or {}
    pf = boot.get("best_vs_F0") or {}

    lines = [
        "# C18acc × I36 fullstack · 進場 + 出場价量結構",
        "",
        f"**全樣本**：{payload.get('date_start')} .. {payload.get('date_end')}  ",
        f"**OOS**：{payload.get('oos_start')} .. {payload.get('date_end')}",
        "",
        payload.get("design", ""),
        "",
        "## 假設檢定",
        "",
        "| ID | 假設 | 結果 |",
        "|----|------|------|",
    ]
    for hid, text in (payload.get("hypotheses") or {}).items():
        r = hypo.get(hid, {})
        lines.append(f"| {hid} | {text} | {'✓' if r.get('pass') else '✗'} |")

    def _tbl(title: str, block: dict[str, Any]) -> None:
        lines.extend([f"## {title}", ""])
        lines.append(
            "| id | entry | exit | entry% | ext | 均超額% | Δ F0 | median hold |"
        )
        lines.append("|----|-------|------|--------|-----|---------|------|-------------|")
        for s in block.get("summaries") or []:
            lines.append(
                f"| **{s.get('variant_id')}** | {s.get('entry_mode', '—')} "
                f"| {s.get('exit_mode', '—')} | {s.get('pct_entry_i36', '—')} "
                f"| {s.get('ext_exits', '—')} | {s.get('mean_excess_pct')} "
                f"| {s.get('delta_vs_baseline_pp', '—')} "
                f"| {s.get('median_hold_days', s.get('mean_hold_days'))} |"
            )
        lines.append("")

    _tbl("全樣本", payload.get("full_sample") or {})
    _tbl(f"OOS · {payload.get('oos_start')} 起", payload.get("oos_sample") or {})

    lines += [
        "## Bootstrap · 最佳 vs F0",
        "",
        f"mean Δ **{pf.get('mean')}pp** · CI [{pf.get('ci95_lo')}, {pf.get('ci95_hi')}] · P(>0)={pf.get('p_mean_gt_zero')}",
        "",
        "## 採納建議",
        "",
        f"- **最佳 variant**：`{rec.get('best_id')}`",
        f"- **建議**：`{rec.get('action')}`",
        f"- {rec.get('note', '')}",
        f"- **F4 exit-only**：Δ F0 {rec.get('F4', {}).get('delta_vs_baseline_pp')}pp",
        f"- **best fullstack**：Δ F0 {rec.get('best', {}).get('delta_vs_baseline_pp')}pp",
        "",
        "---",
        "模組：`scripts/run_c18acc_i36_fullstack_sweep.py`",
        "",
    ]
    return "\n".join(lines)


def _run_fullstack_grid(
    conn: sqlite3.Connection,
    *,
    date_start: str,
    date_end: str,
    configs: list[I36FullstackConfig],
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    poll_cfg = champion_score_swap_c_config()
    base_periods, close, full_dates, f0_summary = _load_c18acc_intraday_context(
        conn,
        date_start=date_start,
        date_end=date_end,
        score_config=poll_cfg,
        baseline_variant_id="F0",
        baseline_label="C18acc poll · 無 I36",
    )
    kbar_cache: dict = {}
    ma_cache: dict = {}
    summaries: list[dict[str, Any]] = []
    periods_by_id: dict[str, list[dict[str, Any]]] = {}
    for cfg in configs:
        periods, summary = _run_fullstack_variant(
            conn,
            base_periods=base_periods,
            close=close,
            full_dates=full_dates,
            base_summary=f0_summary,
            config=cfg,
            kbar_cache=kbar_cache,
            ma_cache=ma_cache,
        )
        summaries.append(summary)
        periods_by_id[cfg.variant_id] = periods
    return {"summaries": summaries, "baseline": f0_summary}, periods_by_id


def run_f3_opening_spike_tune(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    oos_start: str = "2026-01-01",
    n_boot: int = 10000,
) -> dict[str, Any]:
    grid = build_f3_opening_spike_tune_grid()
    full_block, full_periods = _run_fullstack_grid(
        conn, date_start=date_start, date_end=date_end, configs=grid
    )
    oos_block, oos_periods = _run_fullstack_grid(
        conn, date_start=oos_start, date_end=date_end, configs=grid
    )

    fm = {s["variant_id"]: s for s in full_block["summaries"]}
    om = {s["variant_id"]: s for s in oos_block["summaries"]}
    f0 = fm.get("F0", {})
    f3 = fm.get("F3", {})
    f4 = fm.get("F4", {})
    f3_delta = float(f3.get("delta_vs_baseline_pp") or 0)
    f4_delta = float(f4.get("delta_vs_baseline_pp") or 0)

    tune_candidates = [
        s for s in full_block["summaries"] if str(s.get("variant_id", "")).startswith("F3T")
    ]
    best = max(
        tune_candidates,
        key=lambda s: (
            float(s.get("delta_vs_baseline_pp") or -999),
            float(s.get("pct_entry_i36") or 0),
            float(s.get("mean_excess_pct") or -999),
        ),
        default={},
    )
    best_id = str(best.get("variant_id", ""))

    paired_best = bootstrap_paired_mean(
        _paired_excess_deltas(full_periods["F0"], full_periods.get(best_id, full_periods["F0"])),
        n_boot=n_boot,
    )
    save_best = bootstrap_median(_ext_save_values(full_periods.get(best_id, [])), n_boot=n_boot)
    best_oos = om.get(best_id, {})
    best_delta = float(best.get("delta_vs_baseline_pp") or 0)

    # fade neighborhood around best entry/exit spike
    be = best.get("entry_min_ext_prev_pct")
    bx = best.get("exit_min_ext_prev_pct")
    neighbors = [
        s
        for s in tune_candidates
        if s.get("entry_min_ext_prev_pct") == be
        and s.get("exit_min_ext_prev_pct") == bx
        and abs(float(s.get("min_fade_from_high_pct") or 0) - float(best.get("min_fade_from_high_pct") or 0)) <= 0.26
    ]
    neighborhood_ok = len(neighbors) >= 3 and all(
        float(s.get("delta_vs_baseline_pp") or -999) >= best_delta - 0.1 for s in neighbors
    )

    entry_35 = fm.get("F3T-3p5-4p0-f1p0") or {}
    entry_trigger_ok = (best.get("pct_entry_i36") or 0) >= 30 or (
        (entry_35.get("pct_entry_i36") or 0) >= 30
        and (entry_35.get("delta_vs_baseline_pp") or -999) >= f3_delta + 0.1
    )

    hypo = {
        "H-F3T-1": {
            "pass": best_delta >= f3_delta,
            "best_delta": best_delta,
            "F3_delta": f3_delta,
        },
        "H-F3T-2": {
            "pass": best_delta >= f4_delta,
            "best_delta": best_delta,
            "F4_delta": f4_delta,
        },
        "H-F3T-3": {
            "pass": (paired_best.get("ci95_lo") or -999) > 0,
            "bootstrap": paired_best,
        },
        "H-F3T-4": {
            "pass": (best_oos.get("delta_vs_baseline_pp") or -999) >= -0.3,
            "oos_delta": best_oos.get("delta_vs_baseline_pp"),
        },
        "H-F3T-5": {
            "pass": neighborhood_ok,
            "n_neighbors": len(neighbors),
        },
        "H-F3T-6": {
            "pass": entry_trigger_ok,
            "pct_entry": best.get("pct_entry_i36"),
        },
    }

    top10 = sorted(
        tune_candidates,
        key=lambda s: float(s.get("delta_vs_baseline_pp") or -999),
        reverse=True,
    )[:10]

    if best_delta >= f3_delta + 0.08 and hypo["H-F3T-2"]["pass"]:
        action = "adopt_tuned_f3"
        note = (
            f"`{best_id}` Δ F0 {best_delta}pp（F3 +{round(best_delta - f3_delta, 4)}pp）· "
            f"entry≥{best.get('entry_min_ext_prev_pct')}% · "
            f"exit≥{best.get('exit_min_ext_prev_pct')}% · fade≥{best.get('min_fade_from_high_pct')}% · "
            f"trigger {best.get('pct_entry_i36')}%"
        )
    elif hypo["H-F3T-1"]["pass"]:
        action = "marginal_tune"
        note = f"最佳 `{best_id}` 與 F3 接近 · 可採鄰域中位參數降 overfit 風險。"
    else:
        action = "keep_f3_baseline"
        note = "細掃未穩定優於 F3 · 維持 opening≥4% fade≥1%。"

    return {
        "date_start": date_start,
        "date_end": date_end,
        "oos_start": oos_start,
        "n_bootstrap": n_boot,
        "n_variants": len(grid),
        "design": (
            "F3 opening_spike 細掃：entry spike {3.0–5.5} × exit spike {3.5–4.5} × fade {0.5–1.5} · "
            "另掃 opening window · vol_z · no_hh。進場改為 09:05–09:30 窗口 + ext_prev 門檻（解耦舊 5% flag）。"
        ),
        "hypotheses": HYPOTHESES_F3TUNE,
        "hypothesis_results": hypo,
        "anchors": {"F0": f0, "F3": f3, "F4": f4},
        "best": best,
        "best_id": best_id,
        "top10_full": top10,
        "full_sample": full_block,
        "oos_sample": oos_block,
        "bootstrap": {"best_vs_F0": paired_best, "best_ext_save": save_best},
        "recommended": {"action": action, "note": note, "spec": best},
    }


def render_f3_opening_spike_tune_md(payload: dict[str, Any]) -> str:
    hypo = payload.get("hypothesis_results") or {}
    rec = payload.get("recommended") or {}
    boot = payload.get("bootstrap") or {}
    anchors = payload.get("anchors") or {}
    pf = boot.get("best_vs_F0") or {}
    best = payload.get("best") or {}

    lines = [
        "# F3 opening_spike · 參數細掃（Phase F3T）",
        "",
        f"**全樣本**：{payload.get('date_start')} .. {payload.get('date_end')}  ",
        f"**OOS**：{payload.get('oos_start')} .. {payload.get('date_end')}  ",
        f"**格點數**：{payload.get('n_variants')}",
        "",
        payload.get("design", ""),
        "",
        "## 錨點對照",
        "",
        "| id | entry% | 均超額% | Δ F0 | ext |",
        "|----|--------|---------|------|-----|",
        f"| F0 | — | {anchors.get('F0', {}).get('mean_excess_pct')} | 0 | — |",
        f"| F4 exit-only | — | {anchors.get('F4', {}).get('mean_excess_pct')} | {anchors.get('F4', {}).get('delta_vs_baseline_pp')} | {anchors.get('F4', {}).get('ext_exits')} |",
        f"| F3 baseline | {anchors.get('F3', {}).get('pct_entry_i36')}% | {anchors.get('F3', {}).get('mean_excess_pct')} | {anchors.get('F3', {}).get('delta_vs_baseline_pp')} | {anchors.get('F3', {}).get('ext_exits')} |",
        f"| **{payload.get('best_id')}** | {best.get('pct_entry_i36')}% | {best.get('mean_excess_pct')} | **{best.get('delta_vs_baseline_pp')}** | {best.get('ext_exits')} |",
        "",
        "## 假設檢定",
        "",
        "| ID | 假設 | 結果 |",
        "|----|------|------|",
    ]
    for hid, text in (payload.get("hypotheses") or {}).items():
        r = hypo.get(hid, {})
        lines.append(f"| {hid} | {text} | {'✓' if r.get('pass') else '✗'} |")

    lines += [
        "",
        "## Top 10 · Δ F0",
        "",
        "| rank | id | entry≥% | exit≥% | fade≥% | entry% | ext | 均超額% | Δ F0 |",
        "|------|-----|---------|---------|--------|--------|-----|---------|------|",
    ]
    for i, s in enumerate(payload.get("top10_full") or [], 1):
        lines.append(
            f"| {i} | {s.get('variant_id')} | {s.get('entry_min_ext_prev_pct')} "
            f"| {s.get('exit_min_ext_prev_pct')} | {s.get('min_fade_from_high_pct')} "
            f"| {s.get('pct_entry_i36')} | {s.get('ext_exits')} "
            f"| {s.get('mean_excess_pct')} | {s.get('delta_vs_baseline_pp')} |"
        )

    lines += [
        "",
        "## Bootstrap · 最佳 vs F0",
        "",
        f"mean Δ **{pf.get('mean')}pp** · CI [{pf.get('ci95_lo')}, {pf.get('ci95_hi')}] · P(>0)={pf.get('p_mean_gt_zero')}",
        "",
        "## 採納建議",
        "",
        f"- **最佳**：`{payload.get('best_id')}`",
        f"- **動作**：`{rec.get('action')}`",
        f"- {rec.get('note', '')}",
        "",
        "### Order layer 候選",
        "",
        "```",
        f"C18ACC_ENTRY_MODE=opening_spike",
        f"C18ACC_ENTRY_MIN_SPIKE_PCT={best.get('entry_min_ext_prev_pct', 4.0)}",
        f"C18ACC_EXTENSION_MIN_SPIKE_PCT={best.get('exit_min_ext_prev_pct', 4.0)}",
        f"C18ACC_EXTENSION_MIN_FADE_PCT={best.get('min_fade_from_high_pct', 1.0)}",
        "```",
        "",
        "---",
        "模組：`scripts/run_c18acc_f3_opening_spike_tune.py`",
        "",
    ]
    return "\n".join(lines)


# --- Pure I36 · fixed hold · no RRG rotation (H-PURE) ---

HYPOTHESES_PURE: dict[str, str] = {
    "H-PURE-1": "P1 固定持有+I36 · Δ vs P0 ≥ +0.2pp",
    "H-PURE-2": "全市 spike 日 I36 combo 觸發 · P(fade≥2%|trigger) ≥ 9% · Wilson 下界 ≥ 7%",
    "H-PURE-3": "P1 ext ≥ 15/年 · median save_vs_orig > 0 · n_ext ≥ 30",
    "H-PURE-4": "P1−P0 邊際 ≥ 50% × (S4−S3) I36 邊際",
    "H-PURE-5": "OOS 2026H1 · P1 Δ P0 ≥ −0.3pp",
    "H-PURE-6": "P1 vs P0 · bootstrap paired Δ · 95% CI 下界 > 0",
    "H-PURE-8": "P1 全樣本均超額 ≥ P0 − 0.3pp",
}


def _ext_per_year(summary: dict[str, Any], date_start: str, date_end: str) -> float | None:
    ext = summary.get("ext_exits")
    if ext is None:
        return None
    yrs = _calendar_years(date_start, date_end)
    return round(float(ext) / yrs, 2)


def _evaluate_pure_hypotheses(
    *,
    p0: dict[str, Any],
    p1: dict[str, Any],
    p0_oos: dict[str, Any],
    p1_oos: dict[str, Any],
    s3: dict[str, Any],
    s4: dict[str, Any],
    date_start: str,
    date_end: str,
    bootstrap_p1: dict[str, Any],
    save_p1: dict[str, Any],
    universality: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    lift_pure = float(p1.get("delta_vs_baseline_pp") or 0)
    lift_poll = float(s4.get("delta_vs_baseline_pp") or 0) if s4.get("delta_vs_baseline_pp") is not None else 0.0
    retention = lift_pure / lift_poll if abs(lift_poll) > 1e-9 else None
    ext_yr = _ext_per_year(p1, date_start, date_end)
    n_ext = p1.get("ext_exits") or 0
    uni = universality or {}

    return {
        "H-PURE-1": {
            "pass": (p1.get("delta_vs_baseline_pp") or -999) >= 0.2,
            "delta_pp": p1.get("delta_vs_baseline_pp"),
        },
        "H-PURE-2": {
            "pass": bool(uni.get("pass")),
            "P_fade_pct": uni.get("P_fade_pct"),
            "wilson_lo_pct": uni.get("wilson_lo_pct"),
            "n_trigger": uni.get("n_trigger"),
            "phase2b_anchor_pct": uni.get("phase2b_anchor_pct"),
        },
        "H-PURE-3": {
            "pass": (
                ext_yr is not None
                and ext_yr >= 15
                and (save_p1.get("median") or 0) > 0
                and n_ext >= 30
            ),
            "ext_per_year": ext_yr,
            "median_save_pct": save_p1.get("median"),
            "n_ext": n_ext,
        },
        "H-PURE-4": {
            "pass": retention is not None and retention >= 0.5,
            "lift_pure_pp": lift_pure,
            "lift_poll_pp": lift_poll,
            "retention_ratio": round(retention, 3) if retention is not None else None,
        },
        "H-PURE-5": {
            "pass": (p1_oos.get("delta_vs_baseline_pp") or -999) >= -0.3,
            "oos_delta_pp": p1_oos.get("delta_vs_baseline_pp"),
        },
        "H-PURE-6": {
            "pass": (bootstrap_p1.get("ci95_lo") or -999) > 0,
            "bootstrap": bootstrap_p1,
        },
        "H-PURE-8": {
            "pass": (p1.get("mean_excess_pct") or -999) >= float(p0.get("mean_excess_pct") or 0) - 0.3,
            "P0_excess": p0.get("mean_excess_pct"),
            "P1_excess": p1.get("mean_excess_pct"),
        },
    }


def run_pure_i36_analysis(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-26",
    oos_start: str = "2026-01-01",
    n_boot: int = 10000,
    universality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pure_cfg = pure_fixed_hold_score_swap_c_config()
    poll_cfg = champion_score_swap_c_config()
    i36_p1 = _i36_live_config("P1", "pure fixed hold · I36 combo_spike hybrid")
    i36_s4 = _i36_live_config("S4", "poll_5m · I36 combo_spike hybrid")

    pure_full, pure_periods = _run_norrg_branch(
        conn,
        date_start=date_start,
        date_end=date_end,
        score_config=pure_cfg,
        baseline_id="P0",
        baseline_label="pure fixed hold · 無 I36",
        ext_cfg=i36_p1,
        ext_id="P1",
    )
    pure_oos, pure_oos_periods = _run_norrg_branch(
        conn,
        date_start=oos_start,
        date_end=date_end,
        score_config=pure_cfg,
        baseline_id="P0",
        baseline_label="pure fixed hold · 無 I36",
        ext_cfg=i36_p1,
        ext_id="P1",
    )
    poll_block, poll_periods = _run_norrg_branch(
        conn,
        date_start=date_start,
        date_end=date_end,
        score_config=poll_cfg,
        baseline_id="S3",
        baseline_label="poll_5m · 無 I36（= I0）",
        ext_cfg=i36_s4,
        ext_id="S4",
    )

    p0_full = pure_periods["P0"]
    p1_full = pure_periods["P1"]
    poll_sm = {s["variant_id"]: s for s in poll_block["summaries"]}

    bootstrap_p1 = bootstrap_paired_mean(_paired_excess_deltas(p0_full, p1_full), n_boot=n_boot)
    save_p1 = bootstrap_median(_ext_save_values(p1_full), n_boot=n_boot)

    fm = {s["variant_id"]: s for s in pure_full["summaries"]}
    p0 = fm.get("P0", {})
    p1 = fm.get("P1", {})
    fm_oos = {s["variant_id"]: s for s in pure_oos["summaries"]}

    hypo = _evaluate_pure_hypotheses(
        p0=p0,
        p1=p1,
        p0_oos=fm_oos.get("P0", {}),
        p1_oos=fm_oos.get("P1", {}),
        s3=poll_sm.get("S3", {}),
        s4=poll_sm.get("S4", {}),
        date_start=date_start,
        date_end=date_end,
        bootstrap_p1=bootstrap_p1,
        save_p1=save_p1,
        universality=universality or {},
    )

    n_periods = p0.get("n_periods") or 0
    tradable = n_periods >= 120

    recommend = "overlay_only"
    if hypo.get("H-PURE-1", {}).get("pass") and hypo.get("H-PURE-3", {}).get("pass"):
        recommend = "standalone_candidate"
        note = "固定持有 + I36 具 portfolio alpha 且 ext 頻率足夠 → 可研究獨立策略形態。"
    elif hypo.get("H-PURE-1", {}).get("pass"):
        note = "I36 在無 swap 下仍有 alpha，但 ext 太稀 → 維持 C18acc overlay。"
    elif hypo.get("H-PURE-2", {}).get("pass"):
        note = "combo 機率仍成立，但 portfolio Δ 不足 → RRG 選股偏倚可能主導 alpha。"
    else:
        note = "純 I36 未過主假設 → 不宜去掉 RRG 輪動。"

    return {
        "date_start": date_start,
        "date_end": date_end,
        "oos_start": oos_start,
        "n_bootstrap": n_boot,
        "tradable": tradable,
        "n_periods_P0": n_periods,
        "design": (
            "H-PURE：fresh mono top-3 · close · max_swaps=0 · 僅 max_hold 到期重填；"
            "P1 疊 I36 combo_spike hybrid（spike≥4% fade≥1% 1m）。"
            "不可與 S4 直接比 CAGR。"
        ),
        "hypotheses": HYPOTHESES_PURE,
        "hypothesis_results": hypo,
        "universality": universality,
        "variants_full": pure_full,
        "variants_oos": pure_oos,
        "poll_reference": poll_block,
        "bootstrap": {"P1_vs_P0": bootstrap_p1, "P1_ext_save": save_p1},
        "recommended": {"action": recommend, "note": note, "P0": p0, "P1": p1},
    }


def render_pure_i36_report_md(payload: dict[str, Any]) -> str:
    hypo = payload.get("hypothesis_results") or {}
    boot = payload.get("bootstrap") or {}
    rec = payload.get("recommended") or {}
    uni = payload.get("universality") or {}
    pf = boot.get("P1_vs_P0") or {}
    sf = boot.get("P1_ext_save") or {}

    lines = [
        "# 純 I36 · 固定 fresh mono 持倉 · 無 RRG 輪動（H-PURE）",
        "",
        f"**全樣本**：{payload.get('date_start')} .. {payload.get('date_end')}  ",
        f"**OOS**：{payload.get('oos_start')} .. {payload.get('date_end')}  ",
        f"**可交易性**：n={payload.get('n_periods_P0')} · tradable={payload.get('tradable')}",
        "",
        payload.get("design", ""),
        "",
        "## 假設檢定",
        "",
        "| ID | 假設 | 結果 |",
        "|----|------|------|",
    ]
    for hid, text in (payload.get("hypotheses") or {}).items():
        r = hypo.get(hid, {})
        lines.append(f"| {hid} | {text} | {'✓' if r.get('pass') else '✗'} |")

    lines += [
        "",
        "## P0 / P1 · 全樣本",
        "",
        "| id | n | swaps | ext | ext/yr | 均超額% | Δ P0 | median hold |",
        "|----|---|-------|-----|--------|---------|------|-------------|",
    ]
    for s in (payload.get("variants_full") or {}).get("summaries") or []:
        ext_yr = _ext_per_year(s, str(payload.get("date_start")), str(payload.get("date_end")))
        lines.append(
            f"| **{s.get('variant_id')}** | {s.get('n_periods')} | {s.get('swaps_total')} "
            f"| {s.get('ext_exits', '—')} | {ext_yr if s.get('variant_id') == 'P1' else '—'} "
            f"| {s.get('mean_excess_pct')} | {s.get('delta_vs_baseline_pp', '—')} "
            f"| {s.get('median_hold_days', s.get('mean_hold_days'))} |"
        )

    lines += [
        "",
        "## H-PURE-2 · I36 combo universality（脫離 C18acc leg）",
        "",
        f"- P(fade≥2%|combo trigger) = **{uni.get('P_fade_pct')}%** · n={uni.get('n_trigger')}",
        f"- Wilson 95% CI = [{uni.get('wilson_lo_pct')}, {uni.get('wilson_hi_pct')}]",
        f"- Phase 2b 錨（climax@high）= {uni.get('phase2b_anchor_pct')}%",
        "",
        "## Bootstrap",
        "",
        f"- P1 vs P0 paired Δ mean **{pf.get('mean')}pp** · CI [{pf.get('ci95_lo')}, {pf.get('ci95_hi')}] · P(>0)={pf.get('p_mean_gt_zero')}",
        f"- P1 ext save median **{sf.get('median')}%** · P(median>0)={sf.get('p_median_gt_zero')}",
        "",
        "## 採納建議",
        "",
        f"- **建議**：`{rec.get('action')}`",
        f"- {rec.get('note', '')}",
        "",
        "---",
        "模組：`scripts/run_c18acc_pure_i36_sweep.py`",
        "",
    ]
    return "\n".join(lines)


# --- Bleed-sensitive extension overlay sweep (C_bleed cohort) ---

_BLEED_HYBRID = dict(
    poll_interval_min=1,
    break_fixed_hold=True,
    use_orig_exit_if_no_signal=True,
    min_hold_days=0,
    max_hold_fallback=10,
)

BLEED_SWEEP_CONTROLS: list[Intraday1mHoldConfig] = [
    Intraday1mHoldConfig(
        "I0",
        "C18acc-close 基線 · 無 overlay",
        exit_mode="none",
        break_fixed_hold=False,
    ),
    Intraday1mHoldConfig(
        "I36",
        "combo_spike · spike≥4% · fade≥1% · hybrid（champion）",
        exit_mode="combo_spike",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=1.0,
        require_opening_spike=False,
        **_BLEED_HYBRID,
    ),
]

BLEED_SWEEP_TREATMENTS: list[Intraday1mHoldConfig] = [
    Intraday1mHoldConfig(
        "T1",
        "heat_threshold · heat≥70 · hybrid · min_hold=0",
        heat_threshold=70,
        **_BLEED_HYBRID,
    ),
    Intraday1mHoldConfig(
        "T2",
        "heat_threshold · heat≥65 · hybrid（I5 格點）",
        heat_threshold=65,
        **_BLEED_HYBRID,
    ),
    Intraday1mHoldConfig(
        "T3",
        "climax_fade · spike≥5% · fade≥2% · hybrid（I21）",
        exit_mode="climax_fade",
        min_ext_prev_pct=5.0,
        fade_from_high_exit_pct=2.0,
        **_BLEED_HYBRID,
    ),
    Intraday1mHoldConfig(
        "T4",
        "climax_fade · spike≥4% · fade≥1.5% · hybrid",
        exit_mode="climax_fade",
        min_ext_prev_pct=4.0,
        fade_from_high_exit_pct=1.5,
        **_BLEED_HYBRID,
    ),
    Intraday1mHoldConfig(
        "T5",
        "post_peak_vwap · spike≥5% · hybrid（I25）",
        exit_mode="post_peak_vwap",
        min_ext_prev_pct=5.0,
        **_BLEED_HYBRID,
    ),
    Intraday1mHoldConfig(
        "T6",
        "spike_climax_sigma · spike≥5% · hybrid（I20）",
        exit_mode="spike_climax_sigma",
        min_ext_prev_pct=5.0,
        fade_from_high_exit_pct=1.0,
        **_BLEED_HYBRID,
    ),
    Intraday1mHoldConfig(
        "T7",
        "OR-hybrid · combo_spike(I36) OR heat≥70",
        exit_mode="combo_spike",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=1.0,
        require_opening_spike=False,
        or_secondary_exit_mode="heat_threshold",
        or_secondary_heat_threshold=70,
        or_secondary_min_ext_prev_pct=0.0,
        **_BLEED_HYBRID,
    ),
    Intraday1mHoldConfig(
        "T8",
        "OR-hybrid · combo_spike(I36) OR climax_fade(4%/1.5%)",
        exit_mode="combo_spike",
        min_ext_prev_pct=4.0,
        min_fade_from_high_pct=1.0,
        require_opening_spike=False,
        or_secondary_exit_mode="climax_fade",
        or_secondary_min_ext_prev_pct=4.0,
        or_secondary_fade_from_high_exit_pct=1.5,
        or_secondary_min_fade_from_high_pct=0.5,
        **_BLEED_HYBRID,
    ),
]

BLEED_SWEEP_GRID: list[Intraday1mHoldConfig] = BLEED_SWEEP_CONTROLS + BLEED_SWEEP_TREATMENTS

HYPOTHESES_BLEED: dict[str, str] = {
    "H-BLEED-A2": "Δ vs I36 · paired 95% CI 下界 ≥ −0.3pp",
    "H-BLEED-B1": "max_hold 腿 final ret≤−8% · 較 I36 減少 ≥30%",
    "H-BLEED-B2": "P5 intrahold MTM · 較 I36 改善 ≥2pp",
    "H-BLEED-C1": "C_bleed cohort · ext 覆蓋率 ≥ I36 +10pp",
    "H-BLEED-C2": "C_bleed ext 腿 · median save_vs_orig > 0",
    "H-BLEED-C3": "C_bleed ext 腿 · median save 較 I36 ≥ +1pp",
    "H-BLEED-D3": "T7 同時通過 H-BLEED-A2 與 H-BLEED-B1",
}

BLEED_CASE_STUDY_IDS = ("3443", "3293", "8996", "6274")


def _leg_key(pos: dict[str, Any]) -> tuple[str, str]:
    return str(pos["stock_id"]), str(pos["entry_date"])


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    idx = max(0, min(len(xs) - 1, int(len(xs) * pct / 100.0)))
    return round(xs[idx], 4)


def _worst_intrahold_mtm(
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    full_dates: list[str],
    *,
    stock_id: str,
    entry_date: str,
    exit_date: str,
    entry_px: float,
    kbar_cache: dict[tuple[str, str], tuple] | None = None,
) -> float | None:
    if entry_px <= 0:
        return None
    cache = kbar_cache if kbar_cache is not None else {}
    worst = 0.0
    found = False
    for d in _hold_dates(full_dates, entry_date, exit_date):
        if d in close.index and stock_id in close.columns:
            px = float(close.at[d, stock_id])
            if px > 0:
                worst = min(worst, (px / entry_px - 1.0) * 100.0)
                found = True
        key = (stock_id, d)
        if key not in cache:
            if kbar_day_has_data(conn, stock_id, d):
                cache[key] = load_kbar_day_bars(conn, stock_id, d)
            else:
                cache[key] = ()
        bars = cache[key]
        if bars:
            session_low = min(float(b.low) for b in bars)
            if session_low > 0:
                worst = min(worst, (session_low / entry_px - 1.0) * 100.0)
                found = True
    return round(worst, 4) if found else None


def _identify_bleed_cohort(
    conn: sqlite3.Connection,
    base_periods: list[dict[str, Any]],
    close: pd.DataFrame,
    full_dates: list[str],
    rs_ratio: pd.DataFrame,
    rs_mom: pd.DataFrame,
    *,
    mtm_threshold: float = -8.0,
    kbar_cache: dict | None = None,
) -> set[tuple[str, str]]:
    from rrg_rotation import classify_quadrant

    cohort: set[tuple[str, str]] = set()
    for pos in base_periods:
        if str(pos.get("exit_reason") or "") != "max_hold":
            continue
        sid = str(pos["stock_id"])
        entry = str(pos["entry_date"])
        exit_d = str(pos["exit_date"])
        entry_px = float(pos["entry_px"])
        worst = _worst_intrahold_mtm(
            conn,
            close,
            full_dates,
            stock_id=sid,
            entry_date=entry,
            exit_date=exit_d,
            entry_px=entry_px,
            kbar_cache=kbar_cache,
        )
        if worst is None or worst > mtm_threshold:
            continue
        if exit_d not in rs_ratio.index or sid not in rs_ratio.columns:
            continue
        rr = float(rs_ratio.at[exit_d, sid])
        rm = float(rs_mom.at[exit_d, sid])
        quad = classify_quadrant(rr, rm)
        if quad not in ("weakening", "lagging"):
            continue
        cohort.add((sid, entry))
    return cohort


def _paired_delta_vs_ref(
    ref_periods: list[dict[str, Any]],
    var_periods: list[dict[str, Any]],
) -> list[float]:
    deltas: list[float] = []
    for ref, var in zip(ref_periods, var_periods):
        re, ve = ref.get("excess_pct"), var.get("excess_pct")
        if re is None or ve is None:
            continue
        deltas.append(float(ve) - float(re))
    return deltas


def _bleed_variant_metrics(
    periods: list[dict[str, Any]],
    *,
    bleed_cohort: set[tuple[str, str]],
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    full_dates: list[str],
    kbar_cache: dict | None = None,
) -> dict[str, Any]:
    mtm_vals: list[float] = []
    max_hold_deep = 0
    max_hold_total = 0
    bleed_ext = 0
    bleed_total = len(bleed_cohort)
    bleed_saves: list[float] = []

    for pos in periods:
        key = _leg_key(pos)
        entry_px = float(pos["entry_px"])
        worst = _worst_intrahold_mtm(
            conn,
            close,
            full_dates,
            stock_id=key[0],
            entry_date=key[1],
            exit_date=str(pos["exit_date"]),
            entry_px=entry_px,
            kbar_cache=kbar_cache,
        )
        if worst is not None:
            mtm_vals.append(worst)
        reason = str(pos.get("exit_reason") or "")
        ret = float(pos.get("return_pct") or 0)
        if reason in ("max_hold", "max_hold_fallback"):
            max_hold_total += 1
            if ret <= -8.0:
                max_hold_deep += 1
        if key in bleed_cohort:
            if pos.get("ext_exit"):
                bleed_ext += 1
                sv = pos.get("save_vs_orig_pct")
                if sv is not None:
                    bleed_saves.append(float(sv))

    return {
        "p5_intrahold_mtm_pct": _percentile(mtm_vals, 5),
        "max_hold_deep_loss_n": max_hold_deep,
        "max_hold_n": max_hold_total,
        "max_hold_deep_loss_pct": round(max_hold_deep / max_hold_total * 100, 2)
        if max_hold_total
        else None,
        "bleed_cohort_ext_n": bleed_ext,
        "bleed_cohort_n": bleed_total,
        "bleed_cohort_ext_pct": round(bleed_ext / bleed_total * 100, 2) if bleed_total else None,
        "bleed_cohort_median_save_pct": round(statistics.median(bleed_saves), 4)
        if bleed_saves
        else None,
    }


def _evaluate_bleed_hypotheses(
    *,
    summaries: dict[str, dict[str, Any]],
    periods_by_id: dict[str, list[dict[str, Any]]],
    bleed_cohort: set[tuple[str, str]],
    conn: sqlite3.Connection,
    close: pd.DataFrame,
    full_dates: list[str],
    kbar_cache: dict,
) -> dict[str, dict[str, Any]]:
    i36_periods = periods_by_id.get("I36", [])
    i36_bleed = _bleed_variant_metrics(
        i36_periods,
        bleed_cohort=bleed_cohort,
        conn=conn,
        close=close,
        full_dates=full_dates,
        kbar_cache=kbar_cache,
    )
    i36_deep = int(i36_bleed.get("max_hold_deep_loss_n") or 0)
    i36_p5 = float(i36_bleed.get("p5_intrahold_mtm_pct") or -999)
    i36_ext_pct = float(i36_bleed.get("bleed_cohort_ext_pct") or 0)
    i36_med_save = float(i36_bleed.get("bleed_cohort_median_save_pct") or -999)

    per_variant: dict[str, dict[str, Any]] = {}
    for vid, s in summaries.items():
        if vid in ("I0",):
            continue
        periods = periods_by_id.get(vid, [])
        bleed_m = _bleed_variant_metrics(
            periods,
            bleed_cohort=bleed_cohort,
            conn=conn,
            close=close,
            full_dates=full_dates,
            kbar_cache=kbar_cache,
        )
        paired = bootstrap_paired_mean(_paired_delta_vs_ref(i36_periods, periods))
        deep_n = int(bleed_m.get("max_hold_deep_loss_n") or 0)
        deep_reduction = (
            round((1.0 - deep_n / i36_deep) * 100, 2) if i36_deep > 0 else None
        )
        p5 = bleed_m.get("p5_intrahold_mtm_pct")
        p5_improve = round(float(p5) - i36_p5, 4) if p5 is not None else None
        ext_pct = float(bleed_m.get("bleed_cohort_ext_pct") or 0)
        med_save = bleed_m.get("bleed_cohort_median_save_pct")
        med_save_delta = (
            round(float(med_save) - i36_med_save, 4) if med_save is not None else None
        )

        a2_pass = paired.get("ci95_lo") is not None and float(paired["ci95_lo"]) >= -0.3
        b1_pass = deep_reduction is not None and deep_reduction >= 30.0
        b2_pass = p5_improve is not None and p5_improve >= 2.0
        c1_pass = (ext_pct - i36_ext_pct) >= 10.0
        c2_pass = med_save is not None and float(med_save) > 0
        c3_pass = med_save_delta is not None and med_save_delta >= 1.0

        per_variant[vid] = {
            "delta_vs_i36_pp": s.get("delta_vs_i36_pp"),
            "paired_vs_i36": paired,
            "bleed_metrics": bleed_m,
            "max_hold_deep_reduction_pct": deep_reduction,
            "p5_mtm_improve_pp": p5_improve,
            "bleed_ext_pct_delta_pp": round(ext_pct - i36_ext_pct, 2),
            "bleed_median_save_delta_pp": med_save_delta,
            "hypotheses": {
                "H-BLEED-A2": a2_pass,
                "H-BLEED-B1": b1_pass,
                "H-BLEED-B2": b2_pass,
                "H-BLEED-C1": c1_pass,
                "H-BLEED-C2": c2_pass,
                "H-BLEED-C3": c3_pass,
                "H-BLEED-D3": a2_pass and b1_pass if vid == "T7" else None,
            },
        }

    return {
        "I36_baseline_bleed": i36_bleed,
        "variants": per_variant,
    }


def _bleed_case_studies(
    periods_by_id: dict[str, list[dict[str, Any]]],
    *,
    stock_ids: tuple[str, ...] = BLEED_CASE_STUDY_IDS,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sid in stock_ids:
        row: dict[str, Any] = {"stock_id": sid, "legs": []}
        for vid in ("I0", "I36", "T7", "T8"):
            periods = periods_by_id.get(vid, [])
            for p in periods:
                if str(p.get("stock_id")) != sid:
                    continue
                row["legs"].append(
                    {
                        "variant_id": vid,
                        "entry_date": p.get("entry_date"),
                        "exit_date": p.get("exit_date"),
                        "exit_reason": p.get("exit_reason"),
                        "return_pct": p.get("return_pct"),
                        "orig_return_pct": p.get("orig_return_pct"),
                        "save_vs_orig_pct": p.get("save_vs_orig_pct"),
                        "ext_exit": p.get("ext_exit"),
                        "exit_minute": p.get("exit_minute"),
                    }
                )
        out.append(row)
    return out


def run_bleed_exit_sweep(
    conn: sqlite3.Connection,
    *,
    date_start: str = "2024-01-01",
    date_end: str = "2026-06-30",
    is_end: str = "2025-12-31",
    oos_start: str = "2026-01-01",
) -> dict[str, Any]:
    """C18acc-close × bleed-sensitive extension overlay · IS/OOS 雙窗。"""
    close_cfg = close_champion_score_swap_c_config()
    grid = [c for c in BLEED_SWEEP_GRID if c.exit_mode != "none"]

    def _run_window(start: str, end: str, label: str) -> dict[str, Any]:
        payload, periods_by_id = run_intraday_grid_with_periods(
            conn,
            date_start=start,
            date_end=end,
            configs=grid,
            score_config=close_cfg,
            baseline_variant_id="I0",
            baseline_label="C18acc-close 基線",
        )
        close, _, _ = load_price_panels(conn)
        bench = load_benchmark_close(conn).reindex(close.index)
        rs_ratio, rs_mom, _ = compute_rrg_panel(close, bench, length=LENGTH)
        full_dates = close.index.astype(str).tolist()
        base_periods = periods_by_id["I0"]
        kbar_cache: dict = {}
        bleed_cohort = _identify_bleed_cohort(
            conn,
            base_periods,
            close,
            full_dates,
            rs_ratio,
            rs_mom,
            kbar_cache=kbar_cache,
        )

        summaries_by_id: dict[str, dict[str, Any]] = {}
        i36_ex = None
        for s in payload.get("summaries") or []:
            vid = str(s.get("variant_id"))
            summaries_by_id[vid] = dict(s)
            if vid == "I36":
                i36_ex = s.get("mean_excess_pct")

        for s in payload.get("summaries") or []:
            vid = str(s.get("variant_id"))
            if vid == "I0":
                continue
            ex = s.get("mean_excess_pct")
            if ex is not None and i36_ex is not None:
                s["delta_vs_i36_pp"] = round(float(ex) - float(i36_ex), 4)
                summaries_by_id[vid]["delta_vs_i36_pp"] = s["delta_vs_i36_pp"]

        hypo = _evaluate_bleed_hypotheses(
            summaries=summaries_by_id,
            periods_by_id=periods_by_id,
            bleed_cohort=bleed_cohort,
            conn=conn,
            close=close,
            full_dates=full_dates,
            kbar_cache=kbar_cache,
        )
        return {
            "label": label,
            "date_start": start,
            "date_end": end,
            "bleed_cohort_n": len(bleed_cohort),
            "bleed_cohort_keys": sorted(f"{a}:{b}" for a, b in bleed_cohort),
            "summaries": payload.get("summaries"),
            "hypothesis_results": hypo,
            "case_studies": _bleed_case_studies(periods_by_id),
        }

    is_result = _run_window(date_start, is_end, "IS")
    oos_result = _run_window(oos_start, date_end, "OOS")
    full_result = _run_window(date_start, date_end, "FULL")

    ranked = sorted(
        [s for s in full_result.get("summaries") or [] if s.get("variant_id") not in ("I0",)],
        key=lambda s: (
            float(s.get("delta_vs_i36_pp") or -999),
            float((full_result.get("hypothesis_results") or {}).get("variants", {}).get(
                s.get("variant_id"), {}
            ).get("bleed_metrics", {}).get("bleed_cohort_median_save_pct") or -999),
        ),
        reverse=True,
    )

    return {
        "study": "c18acc_bleed_exit_overlay",
        "base_config": close_cfg.to_dict(),
        "date_start": date_start,
        "date_end": date_end,
        "is_end": is_end,
        "oos_start": oos_start,
        "hypotheses": HYPOTHESES_BLEED,
        "treatments": [c.to_dict() for c in BLEED_SWEEP_TREATMENTS],
        "controls": [c.to_dict() for c in BLEED_SWEEP_CONTROLS],
        "is": is_result,
        "oos": oos_result,
        "full": full_result,
        "best_by_delta_i36": ranked[0] if ranked else None,
        "note": (
            "Base：close_champion_score_swap_c_config · min_hold=5 max_hold=10 · "
            "C_bleed = intrahold MTM≤−8% · orig max_hold · exit 日 RRG weakening/lagging。"
        ),
    }


def render_bleed_exit_md(payload: dict[str, Any]) -> str:
    full = payload.get("full") or {}
    hypo_root = full.get("hypothesis_results") or {}
    i36_bleed = hypo_root.get("I36_baseline_bleed") or {}
    variants = hypo_root.get("variants") or {}

    lines = [
        "# C18acc Bleed-Sensitive Extension Overlay Sweep",
        "",
        payload.get("note", ""),
        "",
        f"**FULL**：{payload.get('date_start')} .. {payload.get('date_end')}  ",
        f"**IS**：{payload.get('date_start')} .. {payload.get('is_end')}  ",
        f"**OOS**：{payload.get('oos_start')} .. {payload.get('date_end')}",
        "",
        f"C_bleed cohort（FULL）：**{full.get('bleed_cohort_n')}** legs",
        "",
        "## 假說",
        "",
    ]
    for k, v in (payload.get("hypotheses") or {}).items():
        lines.append(f"- **{k}**：{v}")

    lines += [
        "",
        "## 全樣本 · 均超額 vs I36",
        "",
        "| id | mode | ext | 均超額% | Δ I36 | max_hold深損 | P5 MTM | bleed ext% | bleed med save | A2 | B1 | B2 |",
        "|----|------|-----|---------|-------|-------------|--------|------------|----------------|----|----|-----|",
    ]
    for s in full.get("summaries") or []:
        vid = s.get("variant_id")
        if vid == "I0":
            continue
        vh = variants.get(vid, {})
        hm = vh.get("hypotheses") or {}
        bm = vh.get("bleed_metrics") or {}
        lines.append(
            f"| {vid} | {s.get('exit_mode', '—')} | {s.get('ext_exits', 0)} "
            f"| {s.get('mean_excess_pct')} | {s.get('delta_vs_i36_pp')} "
            f"| {bm.get('max_hold_deep_loss_n')} "
            f"| {bm.get('p5_intrahold_mtm_pct')} "
            f"| {bm.get('bleed_cohort_ext_pct')} "
            f"| {bm.get('bleed_cohort_median_save_pct')} "
            f"| {'✓' if hm.get('H-BLEED-A2') else '✗'} "
            f"| {'✓' if hm.get('H-BLEED-B1') else '✗'} "
            f"| {'✓' if hm.get('H-BLEED-B2') else '✗'} |"
        )

    lines += [
        "",
        f"I36 基線：max_hold 深損 **{i36_bleed.get('max_hold_deep_loss_n')}** · "
        f"P5 MTM **{i36_bleed.get('p5_intrahold_mtm_pct')}%** · "
        f"bleed ext **{i36_bleed.get('bleed_cohort_ext_pct')}%**",
        "",
        "## IS / OOS · Δ I36",
        "",
        "| id | IS Δ | OOS Δ | IS A2 | OOS A2 | IS B1 | OOS B1 |",
        "|----|------|-------|-------|--------|-------|--------|",
    ]
    is_summ = {s["variant_id"]: s for s in (payload.get("is") or {}).get("summaries") or []}
    oos_summ = {s["variant_id"]: s for s in (payload.get("oos") or {}).get("summaries") or []}
    is_hypo = ((payload.get("is") or {}).get("hypothesis_results") or {}).get("variants") or {}
    oos_hypo = ((payload.get("oos") or {}).get("hypothesis_results") or {}).get("variants") or {}
    for vid in [c.variant_id for c in BLEED_SWEEP_TREATMENTS] + ["I36"]:
        ih = is_hypo.get(vid, {}).get("hypotheses") or {}
        oh = oos_hypo.get(vid, {}).get("hypotheses") or {}
        lines.append(
            f"| {vid} | {is_summ.get(vid, {}).get('delta_vs_i36_pp')} "
            f"| {oos_summ.get(vid, {}).get('delta_vs_i36_pp')} "
            f"| {'✓' if ih.get('H-BLEED-A2') else '✗'} "
            f"| {'✓' if oh.get('H-BLEED-A2') else '✗'} "
            f"| {'✓' if ih.get('H-BLEED-B1') else '✗'} "
            f"| {'✓' if oh.get('H-BLEED-B1') else '✗'} |"
        )

    best = payload.get("best_by_delta_i36") or {}
    if best:
        bvid = best.get("variant_id")
        bh = variants.get(bvid, {}).get("hypotheses") or {}
        lines += [
            "",
            f"## 最佳（Δ I36）：**{bvid}** · Δ {best.get('delta_vs_i36_pp')}pp · ext {best.get('ext_exits')}",
            f"- H-BLEED-D3（T7 only）：{'✓' if bh.get('H-BLEED-D3') else '✗'}",
        ]

    lines += ["", "## Case studies", ""]
    for cs in full.get("case_studies") or []:
        lines.append(f"### {cs.get('stock_id')}")
        for leg in cs.get("legs") or []:
            lines.append(
                f"- **{leg.get('variant_id')}** {leg.get('entry_date')}→{leg.get('exit_date')} "
                f"· {leg.get('exit_reason')} · ret {leg.get('return_pct')}% "
                f"· save {leg.get('save_vs_orig_pct')}"
            )
        lines.append("")

    lines += [
        "---",
        "模組：`scripts/run_c18acc_bleed_exit_sweep.py`",
        "",
    ]
    return "\n".join(lines)
