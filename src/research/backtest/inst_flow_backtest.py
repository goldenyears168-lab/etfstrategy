"""法人連買訊號回測（ETF 成分聯集 universe · 日頻 · L1 進場）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from statistics import median

from .copytrade_backtest import (
    CopytradeRunResult,
    CopytradeSignal,
    compute_excess_significance,
    compute_signal_day,
    compute_win_rate_stats,
    group_signals_by_date,
    run_copytrade_backtest,
    simulate_capital_recycling,
    summarize_capital_cycle_insights,
    summarize_decay_insights,
    _lag_label,
)
from holdings_research import ADD_ACTIONS, TW_SPOT_CODE
from rank_stats import max_drawdown_pct
from stock_db import (
    ETF_CODES_INTRADAY_DEFAULT,
    compute_etf_holdings_changes,
    list_etf_snapshot_dates,
    load_etf_constituent_watchlist,
    load_stock_beta_map,
)

INST_FLOW_VERSION = "inst-flow-v1"
CONFLUENCE_SUFFIX = "+etf"
DEFAULT_CAPITAL_CYCLE_MAX_H = 20
UNIVERSE_TAG = "ETF_UNION"
DEFAULT_LOOKBACK_DAYS = 5
DEFAULT_HORIZONS = (5, 9, 14)
ENTRY_LAG_DAYS = 0  # L1 = T+1 開盤


@dataclass(frozen=True)
class SignalProfile:
    profile_id: str
    label: str
    lookback_days: int = DEFAULT_LOOKBACK_DAYS


SIGNAL_PROFILES: tuple[SignalProfile, ...] = (
    SignalProfile("foreign5_pos", "外資5日淨買>0 且當日淨買>0"),
    SignalProfile("foreign5_top30", "外資5日累計橫截面前30% 且當日淨買>0"),
    SignalProfile("sync_buy2", "外資+投信連2日淨買>0"),
    SignalProfile("sync_buy3", "外資+投信連3日淨買>0"),
)


@dataclass(frozen=True)
class InstRow:
    trade_date: str
    foreign_net: float
    investment_trust_net: float


def _inst_rows_for_stocks(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    window_start: str | None,
    window_end: str | None,
) -> dict[str, list[InstRow]]:
    if not stock_ids:
        return {}
    placeholders = ",".join("?" * len(stock_ids))
    clauses = [f"stock_id IN ({placeholders})", "source = 'finmind'"]
    params: list[object] = list(stock_ids)
    if window_start:
        clauses.append("trade_date >= ?")
        params.append(window_start)
    if window_end:
        clauses.append("trade_date <= ?")
        params.append(window_end)
    sql = f"""
        SELECT stock_id, trade_date, foreign_net, investment_trust_net
        FROM stock_institutional_daily
        WHERE {' AND '.join(clauses)}
        ORDER BY stock_id, trade_date
    """
    out: dict[str, list[InstRow]] = {sid: [] for sid in stock_ids}
    for row in conn.execute(sql, params):
        sid = str(row["stock_id"])
        if sid not in out:
            continue
        fn = row["foreign_net"]
        tn = row["investment_trust_net"]
        if fn is None or tn is None:
            continue
        out[sid].append(
            InstRow(
                trade_date=str(row["trade_date"])[:10],
                foreign_net=float(fn),
                investment_trust_net=float(tn),
            )
        )
    return out


def _sum_last(rows: list[InstRow], end_idx: int, days: int, attr: str) -> float | None:
    start = end_idx - days + 1
    if start < 0:
        return None
    chunk = rows[start : end_idx + 1]
    if len(chunk) < days:
        return None
    return sum(getattr(r, attr) for r in chunk)


def _streak_positive(rows: list[InstRow], end_idx: int, days: int, attr: str) -> bool:
    start = end_idx - days + 1
    if start < 0:
        return False
    chunk = rows[start : end_idx + 1]
    if len(chunk) < days:
        return False
    return all(getattr(r, attr) > 0 for r in chunk)


def _top_pct_threshold(values: list[float], top_pct: float) -> float:
    """top_pct=30 → 取第 70 百分位門檻（含）。"""
    if not values:
        return float("inf")
    sorted_vals = sorted(values)
    rank = max(0, int(len(sorted_vals) * (1.0 - top_pct / 100.0)) - 1)
    return sorted_vals[rank]


def _profile_match(
    profile: SignalProfile,
    row: InstRow,
    rows: list[InstRow],
    idx: int,
    *,
    foreign5_by_stock: dict[str, float] | None = None,
) -> bool:
    if profile.profile_id == "foreign5_pos":
        total = _sum_last(rows, idx, profile.lookback_days, "foreign_net")
        return total is not None and total > 0 and row.foreign_net > 0

    if profile.profile_id == "foreign5_top30":
        total = _sum_last(rows, idx, profile.lookback_days, "foreign_net")
        if total is None or row.foreign_net <= 0:
            return False
        if foreign5_by_stock is None:
            return False
        threshold = _top_pct_threshold(list(foreign5_by_stock.values()), 30.0)
        return total >= threshold

    if profile.profile_id == "sync_buy2":
        return (
            _streak_positive(rows, idx, 2, "foreign_net")
            and _streak_positive(rows, idx, 2, "investment_trust_net")
        )

    if profile.profile_id == "sync_buy3":
        return (
            _streak_positive(rows, idx, 3, "foreign_net")
            and _streak_positive(rows, idx, 3, "investment_trust_net")
        )

    raise ValueError(f"unknown profile: {profile.profile_id}")


def _apply_daily_rank_filter(
    day_candidates: list[tuple[float, CopytradeSignal]],
    *,
    top_k: int | None,
    rank_from: int | None,
    rank_to: int | None,
) -> list[tuple[float, CopytradeSignal]]:
    if not day_candidates:
        return day_candidates
    day_candidates.sort(key=lambda x: x[0], reverse=True)
    if rank_from is not None and rank_to is not None:
        if rank_from < 1 or rank_to < rank_from:
            raise ValueError(f"invalid rank band: {rank_from}-{rank_to}")
        return day_candidates[rank_from - 1 : rank_to]
    if top_k is not None and top_k > 0 and len(day_candidates) > top_k:
        return day_candidates[:top_k]
    return day_candidates


def rank_band_label(rank_from: int, rank_to: int) -> str:
    return f"rank{rank_from}_{rank_to}"


def scan_inst_flow_signals(
    conn: sqlite3.Connection,
    *,
    profile: SignalProfile,
    stock_ids: list[str],
    name_by_id: dict[str, str],
    window_start: str | None = None,
    window_end: str | None = None,
    top_k: int | None = None,
    rank_from: int | None = None,
    rank_to: int | None = None,
) -> list[CopytradeSignal]:
    """掃描 universe 內每日法人連買觸發 → CopytradeSignal（action=profile_id）。

    top_k：訊號日僅保留外資 lookback 累計淨買最高的 K 檔（conviction 排序）。
    rank_from / rank_to：1-based 排名區間（先全排序再切片，如 6–10 = 次熱門）。
    """
    series = _inst_rows_for_stocks(
        conn, stock_ids, window_start=window_start, window_end=window_end
    )
    date_set: set[str] = set()
    for rows in series.values():
        for r in rows:
            date_set.add(r.trade_date)
    signals: list[CopytradeSignal] = []

    for trade_date in sorted(date_set):
        foreign5_today: dict[str, float] = {}
        if profile.profile_id == "foreign5_top30":
            for sid, rows in series.items():
                idx = next((i for i, r in enumerate(rows) if r.trade_date == trade_date), None)
                if idx is None:
                    continue
                total = _sum_last(rows, idx, profile.lookback_days, "foreign_net")
                if total is not None:
                    foreign5_today[sid] = total

        day_candidates: list[tuple[float, CopytradeSignal]] = []
        for sid, rows in series.items():
            idx = next((i for i, r in enumerate(rows) if r.trade_date == trade_date), None)
            if idx is None:
                continue
            row = rows[idx]
            if not _profile_match(
                profile,
                row,
                rows,
                idx,
                foreign5_by_stock=foreign5_today if profile.profile_id == "foreign5_top30" else None,
            ):
                continue
            foreign5 = _sum_last(rows, idx, profile.lookback_days, "foreign_net") or 0.0
            day_candidates.append(
                (
                    foreign5,
                    CopytradeSignal(
                        signal_date=trade_date,
                        stock_id=sid,
                        stock_name=name_by_id.get(sid, sid),
                        action=profile.profile_id,
                        share_delta=row.foreign_net,
                        weight_delta=foreign5,
                        weight_pct_curr=None,
                    ),
                )
            )
        day_candidates = _apply_daily_rank_filter(
            day_candidates,
            top_k=top_k,
            rank_from=rank_from,
            rank_to=rank_to,
        )
        signals.extend(sig for _, sig in day_candidates)
    return signals


def load_etf_add_index(
    conn: sqlite3.Connection,
    etf_codes: tuple[str, ...] = ETF_CODES_INTRADAY_DEFAULT,
) -> dict[tuple[str, str], frozenset[str]]:
    """(signal_date, stock_id) → 當日觸發新进/加码 的 ETF 集合。"""
    index: dict[tuple[str, str], set[str]] = {}
    for etf in etf_codes:
        dates = sorted(list_etf_snapshot_dates(conn, etf))
        for i in range(1, len(dates)):
            prev, curr = dates[i - 1], dates[i]
            for row in compute_etf_holdings_changes(conn, etf, curr, prev):
                action = str(dict(row).get("action", ""))
                if action not in ADD_ACTIONS:
                    continue
                sid = str(row["stock_id"])
                key = (curr, sid)
                index.setdefault(key, set()).add(etf)
    return {k: frozenset(v) for k, v in index.items()}


def confluence_profile_id(base_id: str, etf_codes: tuple[str, ...]) -> str:
    if len(etf_codes) == 1:
        return f"{base_id}+{etf_codes[0].lower()}"
    return f"{base_id}{CONFLUENCE_SUFFIX}"


def confluence_action_suffix(etf_codes: tuple[str, ...]) -> str:
    if len(etf_codes) == 1:
        return f"+{etf_codes[0].lower()}"
    return CONFLUENCE_SUFFIX


def confluence_profile_label(base_label: str, etf_codes: tuple[str, ...]) -> str:
    if len(etf_codes) == 1:
        return f"{base_label} ∩ {etf_codes[0]}新进/加码"
    return f"{base_label} ∩ ETF新进/加码"


def apply_etf_confluence(
    signals: list[CopytradeSignal],
    etf_add_index: dict[tuple[str, str], frozenset[str]],
    *,
    action_suffix: str = CONFLUENCE_SUFFIX,
) -> list[CopytradeSignal]:
    """僅保留訊號日 T 同日有 ETF 新进/加码 的 leg。"""
    out: list[CopytradeSignal] = []
    for sig in signals:
        etfs = etf_add_index.get((sig.signal_date, sig.stock_id))
        if not etfs:
            continue
        out.append(
            CopytradeSignal(
                signal_date=sig.signal_date,
                stock_id=sig.stock_id,
                stock_name=sig.stock_name,
                action=f"{sig.action}{action_suffix}",
                share_delta=sig.share_delta,
                weight_delta=sig.weight_delta,
                weight_pct_curr=sig.weight_pct_curr,
            )
        )
    return out


def resolve_inst_flow_window(
    conn: sqlite3.Connection,
    stock_ids: list[str],
    *,
    window_start: str | None,
    window_end: str | None,
) -> tuple[str | None, str | None]:
    if not stock_ids:
        return window_start, window_end
    placeholders = ",".join("?" * len(stock_ids))
    row = conn.execute(
        f"""
        SELECT MIN(trade_date) AS dmin, MAX(trade_date) AS dmax
        FROM stock_institutional_daily
        WHERE stock_id IN ({placeholders}) AND source = 'finmind'
        """,
        stock_ids,
    ).fetchone()
    if row is None or row["dmin"] is None:
        return window_start, window_end
    dmin, dmax = str(row["dmin"]), str(row["dmax"])
    start = max(dmin, window_start) if window_start else dmin
    end = min(dmax, window_end) if window_end else dmax
    if start > end:
        return None, None
    return start, end


def run_inst_flow_backtest(
    conn: sqlite3.Connection,
    *,
    profile: SignalProfile,
    hold_trading_days: int,
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    window_start: str | None = None,
    window_end: str | None = None,
    stock_ids: list[str] | None = None,
    name_by_id: dict[str, str] | None = None,
    batch_id: str | None = None,
    top_k: int | None = None,
) -> CopytradeRunResult:
    watchlist = load_etf_constituent_watchlist(conn, ETF_CODES_INTRADAY_DEFAULT)
    if not watchlist:
        raise RuntimeError("ETF 成分聯集為空：請先同步持股")
    if stock_ids is None:
        stock_ids = [w["stock_id"] for w in watchlist]
    if name_by_id is None:
        name_by_id = {w["stock_id"]: w.get("stock_name") or w["stock_id"] for w in watchlist}

    w_start, w_end = resolve_inst_flow_window(
        conn, stock_ids, window_start=window_start, window_end=window_end
    )
    signals = scan_inst_flow_signals(
        conn,
        profile=profile,
        stock_ids=stock_ids,
        name_by_id=name_by_id,
        window_start=w_start,
        window_end=w_end,
        top_k=top_k,
    )
    grouped = group_signals_by_date(signals)
    strategy_id = f"L1H{hold_trading_days}"
    suffix = date.today().strftime("%Y%m%d")
    run_id = f"inst-flow-{profile.profile_id}-{strategy_id}-{suffix}"

    return run_copytrade_backtest(
        conn,
        UNIVERSE_TAG,
        strategy_id=strategy_id,
        strategy_label=f"{profile.label} · L1H{hold_trading_days}",
        entry_lag_days=ENTRY_LAG_DAYS,
        hold_trading_days=hold_trading_days,
        entry_price_mode="open",
        capital_ntd=capital_ntd,
        cost_bps=cost_bps,
        window_start=w_start,
        window_end=w_end,
        run_id=run_id,
        batch_id=batch_id,
        grouped=grouped,
    )


def run_inst_flow_matrix(
    conn: sqlite3.Connection,
    *,
    profiles: tuple[SignalProfile, ...] = SIGNAL_PROFILES,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    window_start: str | None = None,
    window_end: str | None = None,
    batch_id: str | None = None,
    top_k: int | None = None,
    rank_from: int | None = None,
    rank_to: int | None = None,
    confluence: bool = False,
    confluence_etf_codes: tuple[str, ...] = ETF_CODES_INTRADAY_DEFAULT,
) -> list[CopytradeRunResult]:
    watchlist = load_etf_constituent_watchlist(conn, ETF_CODES_INTRADAY_DEFAULT)
    stock_ids = [w["stock_id"] for w in watchlist]
    name_by_id = {w["stock_id"]: w.get("stock_name") or w["stock_id"] for w in watchlist}
    w_start, w_end = resolve_inst_flow_window(
        conn, stock_ids, window_start=window_start, window_end=window_end
    )

    etf_add_index = (
        load_etf_add_index(conn, confluence_etf_codes) if confluence else None
    )
    conf_action_suffix = confluence_action_suffix(confluence_etf_codes) if confluence else CONFLUENCE_SUFFIX

    grouped_by_profile: dict[str, dict[str, list[CopytradeSignal]]] = {}
    for profile in profiles:
        signals = scan_inst_flow_signals(
            conn,
            profile=profile,
            stock_ids=stock_ids,
            name_by_id=name_by_id,
            window_start=w_start,
            window_end=w_end,
            top_k=top_k,
            rank_from=rank_from,
            rank_to=rank_to,
        )
        grouped_by_profile[profile.profile_id] = group_signals_by_date(signals)
        if confluence and etf_add_index is not None:
            cid = confluence_profile_id(profile.profile_id, confluence_etf_codes)
            conf_signals = apply_etf_confluence(
                signals,
                etf_add_index,
                action_suffix=conf_action_suffix,
            )
            grouped_by_profile[cid] = group_signals_by_date(conf_signals)

    run_profiles: list[tuple[str, str, dict[str, list[CopytradeSignal]]]] = []
    for profile in profiles:
        run_profiles.append((profile.profile_id, profile.label, grouped_by_profile[profile.profile_id]))
        if confluence:
            cid = confluence_profile_id(profile.profile_id, confluence_etf_codes)
            run_profiles.append(
                (
                    cid,
                    confluence_profile_label(profile.label, confluence_etf_codes),
                    grouped_by_profile[cid],
                )
            )

    return run_grouped_profiles_matrix(
        conn,
        run_profiles,
        horizons=horizons,
        capital_ntd=capital_ntd,
        cost_bps=cost_bps,
        window_start=w_start,
        window_end=w_end,
        batch_id=batch_id,
    )


def run_grouped_profiles_matrix(
    conn: sqlite3.Connection,
    run_profiles: list[tuple[str, str, dict[str, list[CopytradeSignal]]]],
    *,
    horizons: tuple[int, ...],
    capital_ntd: float,
    cost_bps: float,
    window_start: str,
    window_end: str,
    batch_id: str | None = None,
    etf_code: str = UNIVERSE_TAG,
    entry_lag_days: int = ENTRY_LAG_DAYS,
) -> list[CopytradeRunResult]:
    """對預先分組的 profile 跑 L×H 矩陣。"""
    beta_map, _ = load_stock_beta_map(conn)
    results: list[CopytradeRunResult] = []
    suffix = date.today().strftime("%Y%m%d")
    lag = _lag_label(entry_lag_days)
    for profile_id, label, grouped in run_profiles:
        for hold in horizons:
            strategy_id = f"{profile_id}-{lag}H{hold}"
            day_results = [
                compute_signal_day(
                    conn,
                    signal_date,
                    grouped[signal_date],
                    capital_ntd=capital_ntd,
                    entry_lag_days=entry_lag_days,
                    hold_trading_days=hold,
                    cost_bps=cost_bps,
                    entry_price_mode="open",
                    beta_map=beta_map,
                )
                for signal_date in sorted(grouped)
            ]
            complete = [d for d in day_results if d.status == "complete"]
            total_deployed = sum(d.deployed_ntd for d in complete)
            total_pnl = sum(d.pnl_ntd for d in complete)
            total_ret = total_pnl / total_deployed * 100.0 if total_deployed > 0 else None
            day_returns = [d.return_pct for d in complete]
            avg_day = sum(day_returns) / len(day_returns) if day_returns else None
            wins = sum(1 for d in complete if d.pnl_ntd > 0)
            win_rate = wins / len(complete) * 100.0 if complete else None
            mdd = max_drawdown_pct(day_returns)
            total_bench = sum(d.bench_return_pct for d in complete)
            total_alpha = sum(d.alpha_ntd for d in complete)
            total_capm_alpha = sum(d.capm_alpha_ntd for d in complete)
            sig = compute_excess_significance(day_results)
            wr = compute_win_rate_stats(day_results)
            results.append(
                CopytradeRunResult(
                    run_id=f"inst-flow-{strategy_id}-{suffix}",
                    etf_code=etf_code,
                    strategy_id=strategy_id,
                    strategy_label=f"{label} · {lag}H{hold}",
                    capital_ntd=capital_ntd,
                    entry_lag_days=entry_lag_days,
                    hold_trading_days=hold,
                    entry_price_mode="open",
                    cost_bps=cost_bps,
                    window_start=window_start,
                    window_end=window_end,
                    signal_days=day_results,
                    n_signal_days=len(day_results),
                    n_complete_days=len(complete),
                    total_deployed_ntd=total_deployed,
                    total_pnl_ntd=total_pnl,
                    total_return_pct=round(total_ret, 4) if total_ret is not None else None,
                    avg_day_return_pct=round(avg_day, 4) if avg_day is not None else None,
                    win_rate_pct=round(win_rate, 2) if win_rate is not None else None,
                    max_drawdown_pct=mdd,
                    total_bench_return_pct=round(total_bench, 4) if complete else None,
                    total_alpha_ntd=round(total_alpha, 2),
                    total_capm_alpha_ntd=round(total_capm_alpha, 2),
                    mean_excess_pct=sig["mean_excess_pct"],
                    p_value_ttest=sig["p_value_ttest"],
                    p_value_wilcoxon=sig["p_value_wilcoxon"],
                    t_stat=sig["t_stat"],
                    batch_id=batch_id,
                )
            )
            results[-1].__dict__["win_rate_vs_bench_pct"] = wr["win_rate_vs_bench_pct"]
    return results


RANK_BAND_SPECS: tuple[tuple[str, str, int | None, int | None, int | None], ...] = (
    ("all", "無 cap", None, None, None),
    ("top10", "Top-10（rank 1–10）", 10, None, None),
    ("rank1_5", "rank 1–5", None, 1, 5),
    ("rank6_10", "rank 6–10", None, 6, 10),
    ("rank11_15", "rank 11–15", None, 11, 15),
)


def run_sync_buy3_rank_band_study(
    conn: sqlite3.Connection,
    *,
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    window_start: str | None = None,
    window_end: str | None = None,
    batch_id: str | None = None,
) -> dict[str, object]:
    """sync_buy3 · 外資5日累計排名帶對照（驗證 rank 6–10 假說）。"""
    profile = next(p for p in SIGNAL_PROFILES if p.profile_id == "sync_buy3")
    watchlist = load_etf_constituent_watchlist(conn, ETF_CODES_INTRADAY_DEFAULT)
    stock_ids = [w["stock_id"] for w in watchlist]
    name_by_id = {w["stock_id"]: w.get("stock_name") or w["stock_id"] for w in watchlist}
    w_start, w_end = resolve_inst_flow_window(
        conn, stock_ids, window_start=window_start, window_end=window_end
    )
    run_profiles: list[tuple[str, str, dict[str, list[CopytradeSignal]]]] = []
    leg_stats: dict[str, dict[str, float | int]] = {}
    for band_id, label, top_k, rank_from, rank_to in RANK_BAND_SPECS:
        signals = scan_inst_flow_signals(
            conn,
            profile=profile,
            stock_ids=stock_ids,
            name_by_id=name_by_id,
            window_start=w_start,
            window_end=w_end,
            top_k=top_k,
            rank_from=rank_from,
            rank_to=rank_to,
        )
        grouped = group_signals_by_date(signals)
        pid = f"sync_buy3_{band_id}"
        run_profiles.append((pid, f"sync_buy3 · {label}", grouped))
        leg_stats[pid] = _legs_per_day_stats(grouped)

    bid = batch_id or f"inst-flow-rankband-{date.today().strftime('%Y%m%d')}"
    results = run_grouped_profiles_matrix(
        conn,
        run_profiles,
        horizons=horizons,
        capital_ntd=capital_ntd,
        cost_bps=cost_bps,
        window_start=w_start,
        window_end=w_end,
        batch_id=bid,
    )
    return {
        "batch_id": bid,
        "window_start": w_start,
        "window_end": w_end,
        "universe_n": len(stock_ids),
        "horizons": horizons,
        "capital_ntd": capital_ntd,
        "leg_stats": leg_stats,
        "results": results,
        "bands": RANK_BAND_SPECS,
    }


def format_sync_buy3_rank_band_report(payload: dict[str, object]) -> str:
    results: list[CopytradeRunResult] = payload["results"]  # type: ignore[assignment]
    leg_stats: dict[str, dict] = payload["leg_stats"]  # type: ignore[assignment]
    horizons: tuple[int, ...] = payload["horizons"]  # type: ignore[assignment]
    by_id = {r.strategy_id: r for r in results}
    band_ids = [f"sync_buy3_{b[0]}" for b in payload["bands"]]  # type: ignore[index]

    lines = [
        "# sync_buy3 外資排名帶研究（rank band）",
        "",
        f"> {INST_FLOW_VERSION} · batch `{payload['batch_id']}` · "
        f"universe **{payload['universe_n']}** 檔 · 每日 {float(payload['capital_ntd']):,.0f} NTD",
        "",
        "## 假說",
        "",
        "- **H0**：rank 6–10（次熱門）因市場忽略，α 優於 Top-10 或 rank 1–5",
        "- 排序鍵：訊號日橫截面 **外資 5 日累計淨買**（與 Top-K 相同）",
        "- Profile：`sync_buy3`（外資+投信連 3 日同步買超）",
        "",
        f"- 資料窗：**{payload['window_start']}** ～ **{payload['window_end']}**",
        "",
        "## 訊號密度",
        "",
        "| band | 訊號日 | 總 leg | 日均 leg |",
        "|------|--------|--------|----------|",
    ]
    for band_id, label, *_ in payload["bands"]:  # type: ignore[misc]
        pid = f"sync_buy3_{band_id}"
        st = leg_stats.get(pid, {})
        lines.append(
            f"| {label} | {st.get('n_days', 0)} | {st.get('n_signals', 0)} | "
            f"{st.get('avg_legs', 0)} |"
        )

    for h in horizons:
        lines.extend(["", f"## L1H{h} 對照", ""])
        lines.append("| band | complete 日 | 勝率% | 累計α | Wilcoxon p |")
        lines.append("|------|------------|---------|-------|------------|")
        for band_id, label, *_ in payload["bands"]:  # type: ignore[misc]
            pid = f"sync_buy3_{band_id}"
            r = by_id.get(f"{pid}-L1H{h}")
            if r is None:
                continue
            wr = getattr(r, "win_rate_vs_bench_pct", r.win_rate_pct)
            p_w = r.p_value_wilcoxon
            p_txt = f"{p_w:.4f}" if p_w is not None else "—"
            star = "*" if p_w is not None and p_w < 0.05 else ""
            lines.append(
                f"| {label} | {r.n_complete_days} | {wr:.2f}% | "
                f"{r.total_alpha_ntd:+,.0f} | {p_txt}{star} |"
            )

    h_focus = 9 if 9 in horizons else horizons[len(horizons) // 2]
    top10 = by_id.get(f"sync_buy3_top10-L1H{h_focus}")
    r610 = by_id.get(f"sync_buy3_rank6_10-L1H{h_focus}")
    r15 = by_id.get(f"sync_buy3_rank1_5-L1H{h_focus}")
    lines.extend(["", "## 假說檢定（H" + str(h_focus) + "）", ""])
    if r610 and top10:
        lines.append(
            f"- rank 6–10 vs Top-10：α {r610.total_alpha_ntd:+,.0f} vs "
            f"{top10.total_alpha_ntd:+,.0f}（Δ {r610.total_alpha_ntd - top10.total_alpha_ntd:+,.0f}）"
        )
    if r610 and r15:
        wr6 = getattr(r610, "win_rate_vs_bench_pct", r610.win_rate_pct)
        wr1 = getattr(r15, "win_rate_vs_bench_pct", r15.win_rate_pct)
        lines.append(
            f"- rank 6–10 vs rank 1–5：α {r610.total_alpha_ntd:+,.0f} vs "
            f"{r15.total_alpha_ntd:+,.0f} · 勝率 {wr6:.1f}% vs {wr1:.1f}%"
        )
    if r610 and r610.p_value_wilcoxon is not None:
        verdict = "支持假說" if (
            r610.p_value_wilcoxon < 0.05
            and top10
            and r610.total_alpha_ntd > top10.total_alpha_ntd
        ) else "不支持假說"
        lines.append(f"- **結論**：rank 6–10 單獨看 p={r610.p_value_wilcoxon:.4f} → **{verdict}**")
    lines.append("")
    return "\n".join(lines)


ENTRY_LAG_SPECS: tuple[tuple[int, str], ...] = (
    (0, "L1 · T+1 開盤（訊號日隔天 · 法人收盤後可知）"),
    (1, "L2 · T+2 開盤（再多等 1 日）"),
    (2, "L3 · T+3 開盤（再多等 2 日）"),
)


def run_sync_buy3_entry_lag_study(
    conn: sqlite3.Connection,
    *,
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    top_k: int = 10,
    window_start: str | None = None,
    window_end: str | None = None,
    batch_id: str | None = None,
) -> dict[str, object]:
    """sync_buy3 · L1/L2/L3 進場列對照（驗證『隔天跟單』= L1）。"""
    profile = next(p for p in SIGNAL_PROFILES if p.profile_id == "sync_buy3")
    watchlist = load_etf_constituent_watchlist(conn, ETF_CODES_INTRADAY_DEFAULT)
    stock_ids = [w["stock_id"] for w in watchlist]
    name_by_id = {w["stock_id"]: w.get("stock_name") or w["stock_id"] for w in watchlist}
    w_start, w_end = resolve_inst_flow_window(
        conn, stock_ids, window_start=window_start, window_end=window_end
    )
    signals = scan_inst_flow_signals(
        conn,
        profile=profile,
        stock_ids=stock_ids,
        name_by_id=name_by_id,
        window_start=w_start,
        window_end=w_end,
        top_k=top_k,
    )
    grouped = group_signals_by_date(signals)
    bid = batch_id or f"inst-flow-entrylag-{date.today().strftime('%Y%m%d')}"
    all_results: list[CopytradeRunResult] = []
    for lag_days, label in ENTRY_LAG_SPECS:
        lag = _lag_label(lag_days)
        pid = f"sync_buy3_{lag.lower()}"
        chunk = run_grouped_profiles_matrix(
            conn,
            [(pid, f"sync_buy3 · {label}", grouped)],
            horizons=horizons,
            capital_ntd=capital_ntd,
            cost_bps=cost_bps,
            window_start=w_start,
            window_end=w_end,
            batch_id=bid,
            entry_lag_days=lag_days,
        )
        all_results.extend(chunk)
    return {
        "batch_id": bid,
        "window_start": w_start,
        "window_end": w_end,
        "universe_n": len(stock_ids),
        "top_k": top_k,
        "horizons": horizons,
        "capital_ntd": capital_ntd,
        "entry_lags": ENTRY_LAG_SPECS,
        "results": all_results,
        "n_signal_days": len(grouped),
    }


def format_sync_buy3_entry_lag_report(payload: dict[str, object]) -> str:
    results: list[CopytradeRunResult] = payload["results"]  # type: ignore[assignment]
    horizons: tuple[int, ...] = payload["horizons"]  # type: ignore[assignment]
    by_id = {r.strategy_id: r for r in results}

    lines = [
        "# sync_buy3 進場列研究（L1 vs L2 vs L3）",
        "",
        f"> {INST_FLOW_VERSION} · batch `{payload['batch_id']}` · Top-{payload['top_k']}/日 · "
        f"universe **{payload['universe_n']}** 檔",
        "",
        "## 時間軸",
        "",
        "```",
        "T-2  T-1   T（訊號日）   T+1        T+2        T+3",
        " │    │    │           │          │          │",
        "外資+投信 連3日買超完成 → 隔日可進場",
        "                  ↑           ↑          ↑",
        "            收盤後才確認    L1 開盤買   L2 開盤買  L3 開盤買",
        "```",
        "",
        "- **訊號日 T**：外資+投信連 3 日同步買超的最後一天（T 收盤後法人資料公布）",
        "- **L1 = 隔天跟單**：T+1 開盤買入（前五輪預設即此）",
        "- **L2/L3**：再多等 1–2 個交易日才開盤買",
        "",
        f"- 資料窗：**{payload['window_start']}** ～ **{payload['window_end']}**",
        f"- 訊號日數：**{payload['n_signal_days']}**",
        "",
    ]
    for h in horizons:
        lines.extend([f"## {h} 交易日持有（H{h}）", ""])
        lines.append("| 進場 | complete 日 | 勝率% | 累計α | Wilcoxon p |")
        lines.append("|------|------------|---------|-------|------------|")
        for lag_days, label in payload["entry_lags"]:  # type: ignore[assignment]
            lag = _lag_label(lag_days)
            pid = f"sync_buy3_{lag.lower()}"
            r = by_id.get(f"{pid}-{lag}H{h}")
            if r is None:
                continue
            wr = getattr(r, "win_rate_vs_bench_pct", r.win_rate_pct)
            p_w = r.p_value_wilcoxon
            p_txt = f"{p_w:.4f}" if p_w is not None else "—"
            star = "*" if p_w is not None and p_w < 0.05 else ""
            short = label.split("·", 1)[0].strip()
            lines.append(
                f"| {short} | {r.n_complete_days} | {wr:.2f}% | "
                f"{r.total_alpha_ntd:+,.0f} | {p_txt}{star} |"
            )
        lines.append("")

    h9 = 9 if 9 in horizons else horizons[0]
    l1 = by_id.get(f"sync_buy3_l1-L1H{h9}")
    l2 = by_id.get(f"sync_buy3_l2-L2H{h9}")
    l3 = by_id.get(f"sync_buy3_l3-L3H{h9}")
    lines.extend(["## 解讀（H" + str(h9) + "）", ""])
    if l1 and l2:
        lines.append(
            f"- L1 vs L2：α {l1.total_alpha_ntd:+,.0f} vs {l2.total_alpha_ntd:+,.0f} "
            f"（Δ {l2.total_alpha_ntd - l1.total_alpha_ntd:+,.0f}）"
        )
    if l1 and l3:
        lines.append(
            f"- L1 vs L3：α {l1.total_alpha_ntd:+,.0f} vs {l3.total_alpha_ntd:+,.0f}"
        )
    if l1:
        lines.append(
            f"- **結論**：前五輪所用 **L1 = 訊號日隔天 T+1 開盤**，"
            f"即法人確認後最早可執行進場；延後至 L2/L3 "
            f"{'通常削弱 α' if l2 and l2.total_alpha_ntd < l1.total_alpha_ntd else '需對照上表'}。"
        )
    lines.append("")
    return "\n".join(lines)


SYNC_BUY_COMPARE_IDS = ("sync_buy2", "sync_buy3")


def run_sync_buy_streak_study(
    conn: sqlite3.Connection,
    *,
    capital_ntd: float = 10_000.0,
    cost_bps: float = 0.0,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    top_k: int = 10,
    window_start: str | None = None,
    window_end: str | None = None,
    batch_id: str | None = None,
) -> dict[str, object]:
    """sync_buy2 vs sync_buy3 · L1 隔天進場 · Top-K。"""
    watchlist = load_etf_constituent_watchlist(conn, ETF_CODES_INTRADAY_DEFAULT)
    stock_ids = [w["stock_id"] for w in watchlist]
    name_by_id = {w["stock_id"]: w.get("stock_name") or w["stock_id"] for w in watchlist}
    w_start, w_end = resolve_inst_flow_window(
        conn, stock_ids, window_start=window_start, window_end=window_end
    )
    run_profiles: list[tuple[str, str, dict[str, list[CopytradeSignal]]]] = []
    leg_stats: dict[str, dict[str, float | int]] = {}
    for pid in SYNC_BUY_COMPARE_IDS:
        profile = next(p for p in SIGNAL_PROFILES if p.profile_id == pid)
        signals = scan_inst_flow_signals(
            conn,
            profile=profile,
            stock_ids=stock_ids,
            name_by_id=name_by_id,
            window_start=w_start,
            window_end=w_end,
            top_k=top_k,
        )
        grouped = group_signals_by_date(signals)
        run_profiles.append((pid, profile.label, grouped))
        leg_stats[pid] = _legs_per_day_stats(grouped)

    bid = batch_id or f"inst-flow-syncbuy-{date.today().strftime('%Y%m%d')}"
    results = run_grouped_profiles_matrix(
        conn,
        run_profiles,
        horizons=horizons,
        capital_ntd=capital_ntd,
        cost_bps=cost_bps,
        window_start=w_start,
        window_end=w_end,
        batch_id=bid,
        entry_lag_days=ENTRY_LAG_DAYS,
    )
    return {
        "batch_id": bid,
        "window_start": w_start,
        "window_end": w_end,
        "universe_n": len(stock_ids),
        "top_k": top_k,
        "horizons": horizons,
        "capital_ntd": capital_ntd,
        "leg_stats": leg_stats,
        "results": results,
    }


def format_sync_buy_streak_report(payload: dict[str, object]) -> str:
    results: list[CopytradeRunResult] = payload["results"]  # type: ignore[assignment]
    leg_stats: dict[str, dict] = payload["leg_stats"]  # type: ignore[assignment]
    horizons: tuple[int, ...] = payload["horizons"]  # type: ignore[assignment]
    by_id = {r.strategy_id: r for r in results}

    lines = [
        "# sync_buy2 vs sync_buy3（L1 隔天進場）",
        "",
        f"> {INST_FLOW_VERSION} · batch `{payload['batch_id']}` · Top-{payload['top_k']}/日 · "
        f"universe **{payload['universe_n']}** 檔 · 每日 {float(payload['capital_ntd']):,.0f} NTD",
        "",
        "## 訊號定義",
        "",
        "| profile | 規則 | 進場 |",
        "|---------|------|------|",
        "| `sync_buy2` | 外資+投信 **連 2 日**淨買>0 | L1 · T+1 開盤 |",
        "| `sync_buy3` | 外資+投信 **連 3 日**淨買>0 | L1 · T+1 開盤 |",
        "",
        f"- 資料窗：**{payload['window_start']}** ～ **{payload['window_end']}**",
        "",
        "## 訊號密度",
        "",
        "| profile | 訊號日 | 總 leg | 日均 leg |",
        "|---------|--------|--------|----------|",
    ]
    for pid in SYNC_BUY_COMPARE_IDS:
        st = leg_stats.get(pid, {})
        lines.append(
            f"| `{pid}` | {st.get('n_days', 0)} | {st.get('n_signals', 0)} | "
            f"{st.get('avg_legs', 0)} |"
        )

    for h in horizons:
        lines.extend(["", f"## L1H{h} 對照", ""])
        lines.append("| profile | complete 日 | 勝率% | 累計α | Wilcoxon p |")
        lines.append("|---------|------------|---------|-------|------------|")
        row2 = by_id.get(f"sync_buy2-L1H{h}")
        row3 = by_id.get(f"sync_buy3-L1H{h}")
        for pid, r in (("sync_buy2", row2), ("sync_buy3", row3)):
            if r is None:
                continue
            wr = getattr(r, "win_rate_vs_bench_pct", r.win_rate_pct)
            p_w = r.p_value_wilcoxon
            p_txt = f"{p_w:.4f}" if p_w is not None else "—"
            star = "*" if p_w is not None and p_w < 0.05 else ""
            lines.append(
                f"| `{pid}` | {r.n_complete_days} | {wr:.2f}% | "
                f"{r.total_alpha_ntd:+,.0f} | {p_txt}{star} |"
            )
        if row2 and row3:
            d_alpha = row2.total_alpha_ntd - row3.total_alpha_ntd
            wr2 = getattr(row2, "win_rate_vs_bench_pct", row2.win_rate_pct)
            wr3 = getattr(row3, "win_rate_vs_bench_pct", row3.win_rate_pct)
            lines.append("")
            lines.append(
                f"- Δα（buy2 − buy3）：**{d_alpha:+,.0f}** · "
                f"Δ勝率：**{wr2 - wr3:+.2f} pp**"
            )

    h9 = 9 if 9 in horizons else horizons[len(horizons) // 2]
    b2 = by_id.get(f"sync_buy2-L1H{h9}")
    b3 = by_id.get(f"sync_buy3-L1H{h9}")
    lines.extend(["", f"## 解讀（H{h9}）", ""])
    if b2 and b3:
        st2 = leg_stats.get("sync_buy2", {})
        st3 = leg_stats.get("sync_buy3", {})
        more_days = int(st2.get("n_days", 0)) - int(st3.get("n_days", 0))
        lines.append(
            f"- buy2 訊號日多 **{more_days}** 天、leg 多 "
            f"**{int(st2.get('n_signals', 0)) - int(st3.get('n_signals', 0))}** 筆"
        )
        if b2.p_value_wilcoxon is not None and b2.p_value_wilcoxon < 0.05:
            if b2.total_alpha_ntd > b3.total_alpha_ntd:
                lines.append("- buy2 **顯著**且 α 更高 → 放寬至 2 日可能更好")
            else:
                lines.append("- buy2 顯著但 α 低於 buy3 → 多出的訊號品質較差")
        elif b3.p_value_wilcoxon is not None and b3.p_value_wilcoxon < 0.05:
            if b2.p_value_wilcoxon is None or b2.p_value_wilcoxon >= 0.05:
                lines.append("- **僅 buy3 達顯著** → 第 3 日確認有篩選價值，建議維持 buy3")
            else:
                lines.append("- 兩者皆顯著，對照上表 α 與勝率取捨")
        else:
            lines.append("- 兩者均未達顯著，需更長樣本或更嚴格 universe")
    lines.append("")
    return "\n".join(lines)


def _legs_per_day_stats(grouped: dict[str, list[CopytradeSignal]]) -> dict[str, float | int]:
    if not grouped:
        return {"n_days": 0, "n_signals": 0, "avg_legs": 0.0, "median_legs": 0.0, "max_legs": 0}
    counts = [len(v) for v in grouped.values()]
    return {
        "n_days": len(counts),
        "n_signals": sum(counts),
        "avg_legs": round(sum(counts) / len(counts), 1),
        "median_legs": float(median(counts)),
        "max_legs": max(counts),
    }


def build_inst_flow_capital_cycle_rows(
    conn: sqlite3.Connection,
    results: list[CopytradeRunResult],
    profile_id: str,
    *,
    capital_ntd: float = 10_000.0,
) -> list[dict]:
    """由 matrix 結果建單池輪動 H1–Hn 表（不需寫入 copytrade DB）。"""
    sub = sorted(
        [r for r in results if r.strategy_id.startswith(f"{profile_id}-L1H")],
        key=lambda r: r.hold_trading_days,
    )
    prev_uncon = 0.0
    prev_recycled = 0.0
    out: list[dict] = []
    for r in sub:
        h = r.hold_trading_days
        signal_dicts = [
            {
                "signal_date": d.signal_date,
                "entry_date": d.entry_date,
                "exit_date": d.exit_date,
                "alpha_ntd": d.alpha_ntd,
                "pnl_ntd": d.pnl_ntd,
                "status": d.status,
            }
            for d in r.signal_days
        ]
        sim = simulate_capital_recycling(conn, signal_dicts, capital_ntd=capital_ntd)
        uncon = float(r.total_alpha_ntd or 0)
        recycled = float(sim["recycled_total_alpha_ntd"] or 0)
        p_w = r.p_value_wilcoxon
        out.append(
            {
                "etf_code": UNIVERSE_TAG,
                "entry_row": "L1",
                "horizon": h,
                "capital_ntd": capital_ntd,
                "strategy_id": r.strategy_id,
                "run_id": r.run_id,
                "n_signals": int(sim["n_signals"] or 0),
                "unconstrained_total_alpha_ntd": uncon,
                "unconstrained_alpha_per_day": round(uncon / h, 4) if h else None,
                "marginal_unconstrained_alpha_ntd": round(uncon - prev_uncon, 2),
                "p_value_wilcoxon": p_w,
                "is_significant": int(p_w is not None and p_w < 0.05),
                "recycled_n_cycles": int(sim["recycled_n_cycles"] or 0),
                "recycled_total_alpha_ntd": recycled,
                "recycled_total_pnl_ntd": sim["recycled_total_pnl_ntd"],
                "recycled_locked_days": int(sim["recycled_locked_days"] or 0),
                "alpha_per_locked_day": sim["alpha_per_locked_day"],
                "alpha_per_cycle": sim["alpha_per_cycle"],
                "signal_capture_pct": sim["signal_capture_pct"],
                "marginal_recycled_alpha_ntd": round(recycled - prev_recycled, 2),
            }
        )
        prev_uncon = uncon
        prev_recycled = recycled
    return out


def format_inst_flow_round4_report(
    results: list[CopytradeRunResult],
    cycle_rows_by_profile: dict[str, list[dict]],
    *,
    profiles: tuple[SignalProfile, ...],
    horizons: tuple[int, ...],
    capital_ntd: float,
    batch_id: str,
    universe_n: int,
    confluence_etf_codes: tuple[str, ...],
    leg_stats_by_profile: dict[str, dict[str, float | int]],
    top_k: int | None = None,
) -> str:
    etf_tag = ",".join(confluence_etf_codes)
    top_k_note = f" · Top-{top_k}/日" if top_k else ""
    lines = [
        "# 法人連買第四輪：sync_buy3 × 00981A confluence · 資金輪動",
        "",
        f"> {INST_FLOW_VERSION} · batch `{batch_id}` · universe **{universe_n}** 檔 · "
        f"每日 {capital_ntd:,.0f} NTD{top_k_note}",
        "",
        "## 第四輪設計",
        "",
        "- **Profile**：`sync_buy3`（外資+投信連 3 日同步買超）",
        f"- **Confluence ETF**：{etf_tag} 當日新进/加码（非六檔聯集）",
        f"- **H 掃描**：H1–H{max(horizons)} 單池輪動（上一筆 exit 前不接新訊號）",
        "- **對照**：standalone `sync_buy3` vs `sync_buy3 ∩ 00981A`",
        "",
        "## 訊號密度",
        "",
        "| profile | 訊號日 | 總 leg | 日均 leg |",
        "|---------|--------|--------|----------|",
    ]
    for p in profiles:
        st = leg_stats_by_profile.get(p.profile_id, {})
        lines.append(
            f"| `{p.profile_id}` | {st.get('n_days', 0)} | {st.get('n_signals', 0)} | "
            f"{st.get('avg_legs', 0)} |"
        )
        cid = confluence_profile_id(p.profile_id, confluence_etf_codes)
        stc = leg_stats_by_profile.get(cid, {})
        lines.append(
            f"| `{cid}` | {stc.get('n_days', 0)} | {stc.get('n_signals', 0)} | "
            f"{stc.get('avg_legs', 0)} |"
        )

    by_id = {r.strategy_id: r for r in results}
    h9, h14 = 9, 14
    if h9 in horizons or h14 in horizons:
        lines.extend(["", "## 關鍵 H 對照（無約束累計 α）", ""])
        lines.append("| profile | H9 α | H9 p | H14 α | H14 p |")
        lines.append("|---------|------|------|-------|-------|")
        for p in profiles:
            cid = confluence_profile_id(p.profile_id, confluence_etf_codes)
            for pid in (p.profile_id, cid):
                r9 = by_id.get(f"{pid}-L1H{h9}")
                r14 = by_id.get(f"{pid}-L1H{h14}")
                a9 = f"{r9.total_alpha_ntd:+,.0f}" if r9 else "—"
                p9 = f"{r9.p_value_wilcoxon:.4f}" if r9 and r9.p_value_wilcoxon is not None else "—"
                a14 = f"{r14.total_alpha_ntd:+,.0f}" if r14 else "—"
                p14 = f"{r14.p_value_wilcoxon:.4f}" if r14 and r14.p_value_wilcoxon is not None else "—"
                lines.append(f"| `{pid}` | {a9} | {p9} | {a14} | {p14} |")

    lines.extend(["", "## 單池資金輪動（L1）", ""])
    for p in profiles:
        for pid, title in (
            (p.profile_id, f"### `{p.profile_id}` standalone"),
            (
                confluence_profile_id(p.profile_id, confluence_etf_codes),
                f"### `{confluence_profile_id(p.profile_id, confluence_etf_codes)}` "
                f"∩ {etf_tag}",
            ),
        ):
            pool = cycle_rows_by_profile.get(pid, [])
            if not pool:
                continue
            ins = summarize_capital_cycle_insights(pool, "L1")
            lines.append(title)
            lines.append("")
            if ins:
                lines.append(
                    f"- **Optimal hold (H*) H{ins['sweet_spot_h']}**：實現超額 "
                    f"{ins['sweet_spot_recycled_alpha_ntd']:+,.0f} NTD · "
                    f"{ins['sweet_spot_n_cycles']} 輪 · "
                    f"鎖倉日均 {ins['sweet_spot_alpha_per_locked_day']:.1f} NTD"
                )
                eff_h = ins.get("best_efficiency_h")
                if eff_h and eff_h != ins["sweet_spot_h"]:
                    eff_row = next(
                        (r for r in pool if int(r["horizon"]) == int(eff_h)),
                        None,
                    )
                    if eff_row:
                        lines.append(
                            f"- **效率峰值 H{eff_h}**：α/鎖倉日 "
                            f"{eff_row['alpha_per_locked_day']:.1f} NTD"
                        )
                lines.append(
                    f"- 建議持有至 **H{ins['hold_through_h']}**（邊際實現超額 遞減）"
                )
            lines.append("")
            lines.append(
                "| H | 無約束α | 實現超額 | 成交筆數 | 捕獲% | α/鎖倉日 | Δ實現超額 |"
            )
            lines.append("|---|--------|-------|------|-------|---------|--------|")
            sweet_h = int(ins["sweet_spot_h"]) if ins else -1
            for r in sorted(pool, key=lambda x: int(x["horizon"])):
                mark = " **" if int(r["horizon"]) == sweet_h else ""
                mark_end = "**" if mark else ""
                lines.append(
                    f"| {mark}H{r['horizon']}{mark_end} | "
                    f"{r['unconstrained_total_alpha_ntd']:+,.0f} | "
                    f"{r['recycled_total_alpha_ntd']:+,.0f} | "
                    f"{r['recycled_n_cycles']} | "
                    f"{r['signal_capture_pct']}% | "
                    f"{r['alpha_per_locked_day'] or '—'} | "
                    f"{r['marginal_recycled_alpha_ntd']:+,.0f} |"
                )
            lines.append("")

    w_start = next((r.window_start for r in results if r.window_start), None)
    w_end = next((r.window_end for r in results if r.window_end), None)
    lines.extend(
        [
            "## 解讀",
            "",
            f"- 資料窗：**{w_start}** ～ **{w_end}**",
            f"- 第三輪用六檔 ETF confluence；第四輪收窄至 **{etf_tag}** 檢驗與主研究標的對齊",
            "- 單池輪動下，H 過長會降低訊號捕獲率；Optimal hold (H*) 為 **實現超額 總量** 與 **捕獲率** 的折衷",
            "",
        ]
    )
    return "\n".join(lines)


def format_inst_flow_report(
    results: list[CopytradeRunResult],
    *,
    profiles: tuple[SignalProfile, ...],
    horizons: tuple[int, ...],
    capital_ntd: float,
    cost_bps: float,
    batch_id: str,
    universe_n: int,
    leg_stats_by_profile: dict[str, dict[str, float | int]],
    top_k: int | None = None,
    confluence: bool = False,
    confluence_etf_codes: tuple[str, ...] = ETF_CODES_INTRADAY_DEFAULT,
    base_profile_ids: tuple[str, ...] = (),
) -> str:
    top_k_note = f" · **Top-{top_k}**/日（外資{DEFAULT_LOOKBACK_DAYS}日累計排序）" if top_k else ""
    conf_note = " · **∩ETF新进/加码**" if confluence else ""
    lines = [
        "# 法人連買回測（ETF 成分聯集）",
        "",
        f"> {INST_FLOW_VERSION}{top_k_note}{conf_note} · universe **{universe_n}** 檔 · "
        f"每日 {capital_ntd:,.0f} NTD · 成本 {cost_bps:.0f} bps · "
        f"基準 {TW_SPOT_CODE} · batch `{batch_id}`",
        "",
        "訊號日 **T** = 法人資料公布日（收盤後）；**L1** = T+1 開盤買；"
        f"**H** ∈ {{{', '.join(str(h) for h in horizons)}}} 收盤賣。",
        "α = 當日 basket 損益 − 同期台指（同進出規則）。",
        "",
        "## 訊號定義",
        "",
        "| profile | 規則 |",
        "|---------|------|",
    ]
    for p in profiles:
        lines.append(f"| `{p.profile_id}` | {p.label} |")
    if confluence:
        etf_list = ", ".join(confluence_etf_codes)
        lines.append(f"| `*+etf` | 同上且訊號日 T 有 ETF 新进/加码（{etf_list}） |")

    lines.extend(["", "## 訊號密度（掃描窗內）", ""])
    lines.append("| profile | 訊號日 | 總 leg | 日均 leg | 中位 leg | 最大單日 |")
    lines.append("|---------|--------|--------|----------|----------|----------|")
    by_id = {r.strategy_id: r for r in results}
    for p in profiles:
        st = leg_stats_by_profile.get(p.profile_id, {})
        lines.append(
            f"| `{p.profile_id}` | {st.get('n_days', 0)} | {st.get('n_signals', 0)} | "
            f"{st.get('avg_legs', 0)} | {st.get('median_legs', 0)} | {st.get('max_legs', 0)} |"
        )
        if confluence:
            cid = confluence_profile_id(p.profile_id, confluence_etf_codes)
            stc = leg_stats_by_profile.get(cid, {})
            lines.append(
                f"| `{cid}` | {stc.get('n_days', 0)} | {stc.get('n_signals', 0)} | "
                f"{stc.get('avg_legs', 0)} | {stc.get('median_legs', 0)} | {stc.get('max_legs', 0)} |"
            )

    if confluence and base_profile_ids:
        lines.extend(["", "## Confluence 對照（standalone vs ∩ETF · H9 為主）", ""])
        lines.append("| profile | standalone α | ∩ETF α | Δα | 勝率% standalone | ∩ETF | Δ pp |")
        lines.append("|---------|-------------|--------|-----|-------------------|------|------|")
        h_focus = 9 if 9 in horizons else horizons[len(horizons) // 2]
        for pid in base_profile_ids:
            base = by_id.get(f"{pid}-L1H{h_focus}")
            conf = by_id.get(
                f"{confluence_profile_id(pid, confluence_etf_codes)}-L1H{h_focus}"
            )
            if base is None or conf is None:
                continue
            wr_b = getattr(base, "win_rate_vs_bench_pct", None) or base.win_rate_pct or 0.0
            wr_c = getattr(conf, "win_rate_vs_bench_pct", None) or conf.win_rate_pct or 0.0
            lines.append(
                f"| `{pid}` | {base.total_alpha_ntd:+,.0f} | {conf.total_alpha_ntd:+,.0f} | "
                f"{conf.total_alpha_ntd - base.total_alpha_ntd:+,.0f} | "
                f"{wr_b:.2f}% | {wr_c:.2f}% | {wr_c - wr_b:+.2f} |"
            )
        lines.append("")

    lines.extend(["", "## L1 持有期比較", ""])
    lines.append(
        "| profile | H | 訊號日 | complete | 勝率% | 累計α (NTD) | 累計 gross | Wilcoxon p |"
    )
    lines.append("|---------|---|--------|----------|---------|-------------|------------|------------|")
    report_profile_ids: list[str] = []
    for p in profiles:
        report_profile_ids.append(p.profile_id)
        if confluence:
            report_profile_ids.append(
                confluence_profile_id(p.profile_id, confluence_etf_codes)
            )
    for pid in report_profile_ids:
        for h in horizons:
            sid = f"{pid}-L1H{h}"
            r = by_id.get(sid)
            if r is None:
                continue
            wr_bench = getattr(r, "win_rate_vs_bench_pct", None) or r.win_rate_pct
            p_w = r.p_value_wilcoxon
            p_txt = f"{p_w:.4f}" if p_w is not None else "—"
            star = "*" if p_w is not None and p_w < 0.05 else ""
            lines.append(
                f"| `{pid}` | H{h} | {r.n_signal_days} | {r.n_complete_days} | "
                f"{wr_bench:.2f}% | {r.total_alpha_ntd:+,.0f} | {r.total_pnl_ntd:+,.0f} | "
                f"{p_txt}{star} |"
            )

    lines.extend(["", "## α Decay（各 profile · L1）", ""])
    for pid in report_profile_ids:
        sub = [r for r in results if r.strategy_id.startswith(f"{pid}-L1H")]
        if not sub:
            continue
        decay_rows = [
            {
                "entry_row": "L1",
                "horizon": r.hold_trading_days,
                "total_alpha_ntd": r.total_alpha_ntd,
                "p_value_wilcoxon": r.p_value_wilcoxon,
            }
            for r in sorted(sub, key=lambda x: x.hold_trading_days)
        ]
        insight = summarize_decay_insights(decay_rows, "L1")
        lines.append(f"### `{pid}`")
        if insight:
            lines.append(
                f"- α 峰值：**H{insight['peak_h']}** "
                f"（{insight['peak_alpha_ntd']:+,.0f} NTD）"
            )
            if insight.get("all_horizons_insignificant"):
                lines.append("- 全 H 與台指無顯著差異（Wilcoxon p>0.05）")
        for r in sorted(sub, key=lambda x: x.hold_trading_days):
            p_w = r.p_value_wilcoxon
            sig = " *" if p_w is not None and p_w < 0.05 else ""
            lines.append(
                f"- H{r.hold_trading_days}: α {r.total_alpha_ntd:+,.0f} · "
                f"勝率 {getattr(r, 'win_rate_vs_bench_pct', r.win_rate_pct)}% · "
                f"p={p_w if p_w is not None else '—'}{sig}"
            )
        lines.append("")

    w_start = next((r.window_start for r in results if r.window_start), None)
    w_end = next((r.window_end for r in results if r.window_end), None)
    lines.extend(
        [
            "## 解讀備註",
            "",
            f"- 資料窗：**{w_start}** ～ **{w_end}**（`stock_institutional_daily`）",
            f"- Universe：`ETF_CODES_INTRADAY_DEFAULT` 最新持股聯集（{universe_n} 檔）",
            "- `foreign5_pos` 為最寬鬆基準；`foreign5_top30` 為橫截面收緊版",
            "- 與 00981A ETF flow 跟單不同：此處法人連買為 **standalone 主訊號**",
        ]
    )
    if top_k:
        lines.append(
            f"- Top-{top_k}：每訊號日僅保留外資 {DEFAULT_LOOKBACK_DAYS} 日累計淨買最高的 {top_k} 檔"
        )
    if confluence:
        etf_list = ", ".join(confluence_etf_codes)
        lines.append(
            f"- Confluence：法人訊號 leg 須與同日（T）ETF 新进/加码 重疊（{etf_list}）"
        )
    lines.append("")
    return "\n".join(lines)
