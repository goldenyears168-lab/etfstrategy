"""§4.4 跟單 vs 直接買 ETF（同 entry/exit · 配對檢定）。"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import date

from .copytrade_backtest import (
    DEFAULT_SIGNAL_CAPITAL_NTD,
    _paired_significance,
    select_executed_signal_days,
)
from flow_returns import return_pct

ENTRY_PRICE_MODE = "open"


@dataclass(frozen=True)
class EtfCompareRow:
    compare_mode: str
    n_paired: int
    n_missing_etf: int
    win_rate_pct: float | None
    mean_diff_return_pct: float | None
    p_value_ttest: float | None
    p_value_wilcoxon: float | None
    cum_copytrade_pnl_ntd: float
    cum_etf_pnl_ntd: float
    diff_gross_ntd: float
    cum_alpha_tw_ntd: float
    n_executed: int | None = None
    signal_capture_pct: float | None = None
    peak_slots: int | None = None


def _etf_bar(
    conn: sqlite3.Connection,
    etf_code: str,
    trade_date: str,
    field: str,
) -> float | None:
    row = conn.execute(
        """
        SELECT open, close FROM daily_bars
        WHERE code = ? AND date = ?
        ORDER BY CASE source WHEN 'tej' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END
        LIMIT 1
        """,
        (etf_code, trade_date),
    ).fetchone()
    if row is None:
        return None
    if field == "open":
        if row["open"] is not None and float(row["open"]) > 0:
            return float(row["open"])
        return float(row["close"]) if row["close"] is not None else None
    if row["close"] is not None:
        return float(row["close"])
    return None


def etf_return_entry_to_exit(
    conn: sqlite3.Connection,
    etf_code: str,
    entry_date: str,
    exit_date: str,
    *,
    entry_price_mode: str = ENTRY_PRICE_MODE,
) -> float | None:
    if entry_price_mode == "close":
        px0 = _etf_bar(conn, etf_code, entry_date, "close")
    else:
        px0 = _etf_bar(conn, etf_code, entry_date, "open")
    px1 = _etf_bar(conn, etf_code, exit_date, "close")
    if px0 is None or px1 is None:
        return None
    return return_pct(px0, px1)


def _last_etf_close_on_or_before(
    conn: sqlite3.Connection,
    etf_code: str,
    trade_date: str,
) -> tuple[str, float] | None:
    row = conn.execute(
        """
        SELECT date, close FROM daily_bars
        WHERE code = ? AND date <= ? AND close IS NOT NULL AND close > 0
        ORDER BY date DESC
        LIMIT 1
        """,
        (etf_code, trade_date),
    ).fetchone()
    if row is None:
        return None
    return str(row["date"]), float(row["close"])


def buy_hold_etf_summary(
    conn: sqlite3.Connection,
    etf_code: str,
    entry_date: str,
    exit_date: str,
    *,
    capital_ntd: float,
    entry_price_mode: str = ENTRY_PRICE_MODE,
) -> dict[str, float | str | None]:
    adj_exit = exit_date
    if _etf_bar(conn, etf_code, exit_date, "close") is None:
        last = _last_etf_close_on_or_before(conn, etf_code, exit_date)
        if last is None:
            return {
                "entry_date": entry_date,
                "exit_date": None,
                "return_pct": None,
                "pnl_ntd": None,
            }
        adj_exit, _ = last
    ret = etf_return_entry_to_exit(
        conn, etf_code, entry_date, adj_exit, entry_price_mode=entry_price_mode
    )
    if ret is None:
        return {
            "entry_date": entry_date,
            "exit_date": adj_exit,
            "return_pct": None,
            "pnl_ntd": None,
        }
    return {
        "entry_date": entry_date,
        "exit_date": adj_exit,
        "return_pct": round(ret, 4),
        "pnl_ntd": round(capital_ntd * ret / 100.0, 2),
    }


def compare_copytrade_vs_etf(
    conn: sqlite3.Connection,
    signal_days: list[dict],
    *,
    etf_code: str,
    per_signal_ntd: float,
    base_per_signal_ntd: float = DEFAULT_SIGNAL_CAPITAL_NTD,
    compare_mode: str = "all_signals",
    rotation_slots: int | None = None,
) -> EtfCompareRow:
    scale = per_signal_ntd / base_per_signal_ntd
    days = [
        d
        for d in signal_days
        if d.get("status") == "complete"
    ]
    days.sort(key=lambda d: str(d["signal_date"]))

    n_executed: int | None = None
    capture: float | None = None
    peak: int | None = None
    if compare_mode == "rotation_executed":
        slots = rotation_slots or _infer_hold_days_from_conn(conn, days)
        executed, slot_meta = select_executed_signal_days(days, n_slots=slots)
        days = executed
        n_executed = int(slot_meta["recycled_n_cycles"] or 0)
        capture = (
            float(slot_meta["signal_capture_pct"])
            if slot_meta["signal_capture_pct"] is not None
            else None
        )
        peak = int(slot_meta["peak_concurrent_slots"] or 0)

    paired_ret: list[float] = []
    paired_pnl: list[float] = []
    wins = 0
    missing = 0
    cum_ct = 0.0
    cum_etf = 0.0
    cum_alpha = 0.0
    for d in days:
        entry = str(d["entry_date"])
        exit_d = str(d["exit_date"])
        etf_ret = etf_return_entry_to_exit(conn, etf_code, entry, exit_d)
        if etf_ret is None:
            missing += 1
            continue
        ct_ret = float(d["return_pct"])
        if ct_ret > etf_ret:
            wins += 1
        paired_ret.append(ct_ret - etf_ret)
        ct_pnl = float(d["pnl_ntd"]) * scale
        etf_pnl = per_signal_ntd * etf_ret / 100.0
        paired_pnl.append(ct_pnl - etf_pnl)
        cum_ct += ct_pnl
        cum_etf += etf_pnl
        cum_alpha += float(d.get("alpha_ntd") or 0) * scale

    n = len(paired_ret)
    sig = _paired_significance(paired_ret)
    return EtfCompareRow(
        compare_mode=compare_mode,
        n_paired=n,
        n_missing_etf=missing,
        win_rate_pct=round(100.0 * wins / n, 2) if n else None,
        mean_diff_return_pct=sig.get("mean_diff"),
        p_value_ttest=sig.get("p_value_ttest"),
        p_value_wilcoxon=sig.get("p_value_wilcoxon"),
        cum_copytrade_pnl_ntd=round(cum_ct, 2),
        cum_etf_pnl_ntd=round(cum_etf, 2),
        diff_gross_ntd=round(cum_ct - cum_etf, 2),
        cum_alpha_tw_ntd=round(cum_alpha, 2),
        n_executed=n_executed,
        signal_capture_pct=capture,
        peak_slots=peak,
    )


def _infer_hold_days_from_conn(
    conn: sqlite3.Connection, days: list[dict]
) -> int:
    if not days:
        return 20
    from .copytrade_backtest import count_hold_trading_days

    sample = days[0]
    return max(
        1,
        count_hold_trading_days(
            conn, str(sample["entry_date"]), str(sample["exit_date"])
        ),
    )


def _parse_hold_from_strategy(strategy_id: str) -> int | None:
    m = re.search(r"H(\d+)$", strategy_id.upper())
    return int(m.group(1)) if m else None


def _resolve_run(
    conn: sqlite3.Connection,
    etf_code: str,
    strategy_id: str,
    run_id: str | None,
) -> sqlite3.Row:
    from stock_db import load_copytrade_runs

    runs = [
        r
        for r in load_copytrade_runs(conn, etf_code=etf_code)
        if str(r["strategy_id"]) == strategy_id
    ]
    if run_id:
        for row in runs:
            if str(row["run_id"]) == run_id:
                return row
        raise ValueError(f"run_id not found: {run_id}")
    if not runs:
        raise ValueError(f"no copytrade run for {etf_code} {strategy_id}")
    return runs[0]


def run_etf_compare_analysis(
    conn: sqlite3.Connection,
    *,
    etf_code: str,
    strategy_id: str,
    batch_id: str,
    run_id: str | None = None,
    capital_ntd: float = 100_000.0,
    per_signal_ntd: float | None = None,
    slots_mode: str = "rotation",
    persist: bool = True,
) -> dict[str, object]:
    from stock_db import load_copytrade_signal_days_for_run, persist_copytrade_etf_compare

    run = _resolve_run(conn, etf_code, strategy_id, run_id)
    rid = str(run["run_id"])
    hold_h = int(run["hold_trading_days"] or _parse_hold_from_strategy(strategy_id) or 20)
    base_per = float(run["capital_ntd"] or DEFAULT_SIGNAL_CAPITAL_NTD)
    if per_signal_ntd is None:
        if slots_mode == "rotation":
            per_signal_ntd = capital_ntd / hold_h
        else:
            per_signal_ntd = base_per

    signal_days = [dict(d) for d in load_copytrade_signal_days_for_run(conn, rid)]
    complete = [d for d in signal_days if d["status"] == "complete"]
    window_start = complete[0]["signal_date"] if complete else None
    window_end = complete[-1]["signal_date"] if complete else None
    first_entry = min(str(d["entry_date"]) for d in complete) if complete else None
    last_exit = max(str(d["exit_date"]) for d in complete) if complete else None

    all_row = compare_copytrade_vs_etf(
        conn,
        signal_days,
        etf_code=etf_code,
        per_signal_ntd=float(per_signal_ntd),
        base_per_signal_ntd=base_per,
        compare_mode="all_signals",
    )
    rot_row: EtfCompareRow | None = None
    if slots_mode == "rotation":
        rot_row = compare_copytrade_vs_etf(
            conn,
            signal_days,
            etf_code=etf_code,
            per_signal_ntd=float(per_signal_ntd),
            base_per_signal_ntd=base_per,
            compare_mode="rotation_executed",
            rotation_slots=hold_h,
        )
    elif slots_mode != "unconstrained":
        raise ValueError(f"unknown slots_mode: {slots_mode}")

    bh = (
        buy_hold_etf_summary(
            conn,
            etf_code,
            first_entry,
            last_exit,
            capital_ntd=capital_ntd,
        )
        if first_entry and last_exit
        else {"entry_date": None, "exit_date": None, "return_pct": None, "pnl_ntd": None}
    )

    primary = rot_row if rot_row is not None else all_row
    verdict = _verdict(primary)

    summary = {
        "batch_id": batch_id,
        "etf_code": etf_code,
        "strategy_id": strategy_id,
        "run_id": rid,
        "capital_ntd": capital_ntd,
        "per_signal_ntd": float(per_signal_ntd),
        "hold_trading_days": hold_h,
        "slots_mode": slots_mode,
        "window_start": window_start,
        "window_end": window_end,
        "verdict": verdict,
        "all_signals": all_row,
        "rotation_executed": rot_row,
        "buy_hold": bh,
    }
    if persist:
        persist_copytrade_etf_compare(conn, batch_id, summary)
    return summary


def _verdict(row: EtfCompareRow) -> str:
    p = row.p_value_wilcoxon
    if p is None:
        return "inconclusive"
    if p < 0.05 and (row.mean_diff_return_pct or 0) > 0:
        return "support"
    if p < 0.05 and (row.mean_diff_return_pct or 0) < 0:
        return "reject"
    return "inconclusive"


def format_etf_compare_markdown(
    *,
    etf_code: str,
    strategy_id: str,
    batch_id: str,
    summary: dict[str, object],
) -> str:
    all_row: EtfCompareRow = summary["all_signals"]  # type: ignore[assignment]
    rot: EtfCompareRow | None = summary.get("rotation_executed")  # type: ignore[assignment]
    bh: dict = summary["buy_hold"]  # type: ignore[assignment]
    primary = rot if rot is not None else all_row
    capital = float(summary["capital_ntd"])
    per = float(summary["per_signal_ntd"])
    hold_h = int(summary["hold_trading_days"])
    lines = [
        f"# {etf_code} 跟單 vs 買 ETF（§4.4）",
        "",
        f"> batch `{batch_id}` · 策略 **{strategy_id}** · "
        f"報告日 {date.today().strftime('%Y%m%d')}",
        "",
        "## 研究設計",
        "",
        "| 項目 | 設定 |",
        "|------|------|",
        f"| Copytrade | {strategy_id} · 等權 · T+1 開盤進 · H{hold_h} 收盤出 |",
        f"| ETF 對照 | 同期買 **{etf_code}**（同 entry/exit · 同 deploy） |",
        f"| 資金模型 | {summary['slots_mode']} · 總本金 {capital:,.0f} · "
        f"每訊號 {per:,.0f} |",
        f"| 樣本窗 | {summary.get('window_start')} ～ {summary.get('window_end')} |",
        "| 檢定 | 配對 t 檢定 + Wilcoxon（差值 = copytrade − ETF） |",
        "| 成本 | 0 bps |",
        "",
        "## 結論",
        "",
        f"- **判決（Primary）**：**{summary['verdict']}** — "
        f"勝率 {primary.win_rate_pct}% · "
        f"均超額 {_fmt_pp(primary.mean_diff_return_pct)} · "
        f"Wilcoxon p={primary.p_value_wilcoxon}",
        f"- **累計 gross 差**（Primary）：{primary.diff_gross_ntd:+,.0f} NTD "
        f"（CT {primary.cum_copytrade_pnl_ntd:+,.0f} vs ETF "
        f"{primary.cum_etf_pnl_ntd:+,.0f}）",
        "",
    ]

    def _row_table(row: EtfCompareRow, title: str) -> list[str]:
        extra = ""
        if row.n_executed is not None:
            extra = (
                f" · 執行 {row.n_executed} 輪 · 捕獲 {row.signal_capture_pct}%"
                f" · peak {row.peak_slots} 槽"
            )
        return [
            f"### {title}{extra}",
            "",
            "| 指標 | Copytrade | ETF | 差值 |",
            "|------|-----------|-----|------|",
            f"| 配對 n | {row.n_paired} | {row.n_paired} | 缺 ETF 價 {row.n_missing_etf} |",
            f"| 勝率（gross%） | {row.win_rate_pct}% | — | — |",
            f"| 均配對超額% | {_fmt_pp(row.mean_diff_return_pct)} | 0 | — |",
            f"| Wilcoxon p | — | — | {row.p_value_wilcoxon} |",
            f"| t 檢定 p | — | — | {row.p_value_ttest} |",
            f"| 累計 gross | {row.cum_copytrade_pnl_ntd:+,.0f} | "
            f"{row.cum_etf_pnl_ntd:+,.0f} | **{row.diff_gross_ntd:+,.0f}** |",
            f"| vs 台指 α | {row.cum_alpha_tw_ntd:+,.0f} | — | — |",
            "",
        ]

    if rot is not None:
        lines.extend(_row_table(rot, "Primary · Rotation 執行輪"))
        lines.extend(_row_table(all_row, "對照 · 全部 complete 訊號日"))
    else:
        lines.extend(_row_table(all_row, "Primary · 全部 complete 訊號日"))

    if bh.get("return_pct") is not None:
        lines.extend(
            [
                "### 買入持有 ETF（非配對 · 全倉一次）",
                "",
                f"- **{bh['entry_date']}** 開盤買入 → **{bh['exit_date']}** 收盤 "
                f"· 報酬 **{bh['return_pct']:+.2f}%** · "
                f"{capital:,.0f} NTD → PnL **{bh['pnl_ntd']:+,.0f}**",
                f"- 同期 Rotation copytrade 累計 gross：**"
                f"{primary.cum_copytrade_pnl_ntd:+,.0f}**（多筆重疊 deploy，不可直接比報酬率）",
                "",
            ]
        )

    lines.extend(
        [
            "## 解讀",
            "",
            "- **配對檢定**回答：每個訊號窗口內，跟成分股是否優於同期買 ETF。",
            "- **買入持有**回答：整段牛市是否「拿着 ETF 更賺」；與配對結論可並存。",
            "- 未扣成本；copytrade 多檔成分股成本通常高於單買 ETF。",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt_pp(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:+.4f} pp"
