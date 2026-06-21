#!/usr/bin/env python3
"""
ETF 操盤策略假設檢定（分 ETF · 含宏觀脈絡）。

科學流程：
  1. 對每檔 ETF 提出可證偽假設（策略目的，非跟單訊號）
  2. 僅用變動當日及以前特徵（股價/法人/TEJ 指數/tech_risk 夜盤）
  3. 事件組 vs 同日未變動成分股（per-ETF universe）
  4. Permutation test 驗證；樣本不足標記 INSUFFICIENT

宏觀層（緩解「動能 vs 左側」矛盾）：
  - tx_gap_pct：台指期相對現貨 gap（FinMind 盤後/夜盤，sync_tech_risk_context）
  - te_overnight_pct：電子期隔夜漲跌
  - tsm/sox：美股半導體隔夜

用法：
  python src/etf_flow_hypothesis.py --run --write-report
  python src/etf_flow_hypothesis.py --run --sync-context --write-report
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .etf_entry_ta_study import permutation_pvalue
from .etf_flow_factor_screen import (
    FACTOR_SPECS,
    FactorEffect,
    FlowLeg,
    build_feature_rows,
    collect_flow_legs,
    screen_factors,
    unique_stock_days,
)
from project_config import ETF_CODES_LISTED
from report_paths import REPORTS_RESEARCH
from stock_db import PROJECT_ROOT, connect

DEFAULT_MAIN_DB = PROJECT_ROOT / "data" / "stocks.db"
DEFAULT_REPORT = REPORTS_RESEARCH / "etf_flow_hypothesis.md"
PERMUTATION_ITERS = 5000
MIN_N_EVENT = 8
MIN_N_CTRL = 20


@dataclass(frozen=True)
class EtfProfile:
    code: str
    name: str
    manager: str
    archetype: str  # stock_picker | index_rebalance | broad_active
    intent: str


ETF_PROFILES: dict[str, EtfProfile] = {
    "00981A": EtfProfile(
        "00981A", "主動科技", "統一", "stock_picker",
        "在科技成分內挑相對強勢標的；受美股半導體與電子期夜盤影響。",
    ),
    "00403A": EtfProfile(
        "00403A", "主動台股", "統一", "broad_active",
        "主動選股偏大型成長；參考台指期夜盤與大盤環境。",
    ),
    "009816": EtfProfile(
        "009816", "金融高息", "凱基", "index_rebalance",
        "追蹤金融/高息指數，調倉接近成分再平衡，非典型動能選股。",
    ),
    "00980A": EtfProfile(
        "00980A", "野村台股", "野村", "stock_picker",
        "主動選股；樣本少，宜與 00981A 對照。",
    ),
    "00982A": EtfProfile(
        "00982A", "凱基主動", "凱基", "stock_picker",
        "主動選股；觀察是否追蹤半導體相對強度。",
    ),
    "00992A": EtfProfile(
        "00992A", "凱基台股", "凱基", "broad_active",
        "主動台股；樣本偏少。",
    ),
}


@dataclass(frozen=True)
class HypothesisSpec:
    id: str
    title: str
    prediction: str
    factor_key: str
    direction: str  # higher | lower | bool_higher
    etf_codes: frozenset[str] | None = None  # None = all
    macro_condition: str | None = None  # tx_gap_neg | tx_gap_pos | te_overnight_neg | None


HYPOTHESES: tuple[HypothesisSpec, ...] = (
    HypothesisSpec(
        "H1_rs14",
        "相對成分股 14 日超額為正",
        "加碼標的事前跑贏同 ETF 成分股中位數",
        "rs_univ14", "higher", None, None,
    ),
    HypothesisSpec(
        "H2_ma_turn",
        "MA20 轉強但未站上均線",
        "ma20_rising 高於控制、above_ma20 低於控制",
        "ma20_rising", "bool_higher", None, None,
    ),
    HypothesisSpec(
        "H3_ir_alpha",
        "相對電子指超額（半導體選股）",
        "加碼股 excess_ir14 > 控制",
        "excess_ir14", "higher",
        frozenset({"00981A", "00980A", "00982A"}), None,
    ),
    HypothesisSpec(
        "H4_not_momentum_816",
        "009816 非動能驅動",
        "009816 加碼 ret14 與控制無顯著差",
        "ret14", "no_diff",
        frozenset({"009816"}), None,
    ),
    HypothesisSpec(
        "H5_dip_rs",
        "夜盤跌日仍買相對強",
        "tx_gap<0 當日加碼 rs_univ14 仍 > 控制",
        "rs_univ14", "higher", None, "tx_gap_neg",
    ),
    HypothesisSpec(
        "H6_dip_not_chase",
        "夜盤跌日非追高",
        "tx_gap<0 當日加碼 ret5 不顯著高於控制",
        "ret5", "no_diff", None, "tx_gap_neg",
    ),
    HypothesisSpec(
        "H7_semi_overnight",
        "SOX 跌後買電子超額",
        "sox 跌日加碼 excess_ir14 > 控制",
        "excess_ir14", "higher",
        frozenset({"00981A", "00980A", "00982A"}), "sox_neg",
    ),
)


@dataclass
class HypothesisResult:
    etf_code: str
    hypothesis_id: str
    title: str
    prediction: str
    n_add: int
    n_ctrl: int
    mean_add: float | None
    mean_ctrl: float | None
    delta: float | None
    p_value: float | None
    verdict: str
    macro_note: str | None = None


@dataclass
class EtfStudyResult:
    profile: EtfProfile
    n_add_legs: int
    n_reduce_legs: int
    n_add_unique: int
    hypothesis_results: list[HypothesisResult] = field(default_factory=list)
    top_effects: list[FactorEffect] = field(default_factory=list)
    intent_summary: str = ""


@dataclass
class MacroSlice:
    label: str
    n_add_days: int
    add_ret14: float | None
    ctrl_ret14: float | None
    add_rs14: float | None
    ctrl_rs14: float | None
    add_ret5: float | None
    ctrl_ret5: float | None


def load_tech_risk_map(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT session_date, tx_gap_pct, te_overnight_pct,
               tsm_daily_return_pct, sox_daily_return_pct, tx_futures_session
        FROM tech_risk_daily_snapshot
        """
    ).fetchall()
    return {str(r["session_date"]): dict(r) for r in rows}


def ensure_tech_risk_coverage(conn: sqlite3.Connection, dates: list[str], *, sync: bool) -> list[str]:
    """回傳仍缺 macro 的 session_date。"""
    have = {
        r[0]
        for r in conn.execute(
            "SELECT session_date FROM tech_risk_daily_snapshot WHERE tx_gap_pct IS NOT NULL"
        ).fetchall()
    }
    missing = sorted(d for d in dates if d not in have)
    if missing and sync:
        from sync_tech_risk_context import sync_tech_risk

        sync_tech_risk(DEFAULT_MAIN_DB, history_days=120, session_limit=60, quiet=True)
        return ensure_tech_risk_coverage(conn, dates, sync=False)
    return missing


def etf_universe(conn: sqlite3.Connection, etf_code: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT stock_id FROM etf_holdings WHERE etf_code = ?",
        (etf_code,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def legs_for_etf(legs: list[FlowLeg], etf_code: str) -> list[FlowLeg]:
    return [l for l in legs if l.etf_code == etf_code]


def _mean(vals: list[float]) -> float | None:
    return round(statistics.mean(vals), 2) if vals else None


def _get_vals(rows, side: str, key: str, dates: set[str] | None = None) -> list[float]:
    out: list[float] = []
    for r in rows:
        if r.side != side:
            continue
        if dates is not None and r.event_date not in dates:
            continue
        v = r.values.get(key)
        if v is not None:
            out.append(float(v))
    return out


def _macro_dates(tech: dict[str, dict], condition: str) -> set[str]:
    dates: set[str] = set()
    for d, row in tech.items():
        if condition == "tx_gap_neg":
            g = row.get("tx_gap_pct")
            if g is not None and float(g) < 0:
                dates.add(d)
        elif condition == "tx_gap_pos":
            g = row.get("tx_gap_pct")
            if g is not None and float(g) >= 0:
                dates.add(d)
        elif condition == "te_overnight_neg":
            g = row.get("te_overnight_pct")
            if g is not None and float(g) < 0:
                dates.add(d)
        elif condition == "sox_neg":
            g = row.get("sox_daily_return_pct")
            if g is not None and float(g) < 0:
                dates.add(d)
    return dates


def test_hypothesis(
    *,
    etf_code: str,
    spec: HypothesisSpec,
    event_rows,
    ctrl_rows,
    tech: dict[str, dict],
) -> HypothesisResult | None:
    if spec.etf_codes is not None and etf_code not in spec.etf_codes:
        return None

    dates: set[str] | None = None
    macro_note = None
    if spec.macro_condition:
        dates = _macro_dates(tech, spec.macro_condition)
        macro_note = f"條件：{spec.macro_condition}（{len(dates)} 日）"
        if len(dates) < 2:
            return HypothesisResult(
                etf_code=etf_code,
                hypothesis_id=spec.id,
                title=spec.title,
                prediction=spec.prediction,
                n_add=0,
                n_ctrl=0,
                mean_add=None,
                mean_ctrl=None,
                delta=None,
                p_value=None,
                verdict="INSUFFICIENT",
                macro_note=macro_note,
            )

    is_bool = spec.factor_key in ("ma20_rising", "above_ma20", "above_ma60")
    a = _get_vals(event_rows, "add", spec.factor_key, dates)
    c = _get_vals(ctrl_rows, "control", spec.factor_key, dates)

    if is_bool:
        a = [float(int(v)) for v in a]
        c = [float(int(v)) for v in c]

    n_a, n_c = len(a), len(c)
    m_a, m_c = _mean(a), _mean(c)
    delta = round(m_a - m_c, 2) if m_a is not None and m_c is not None else None

    if n_a < MIN_N_EVENT or n_c < MIN_N_CTRL:
        verdict = "INSUFFICIENT"
        p = None
    elif spec.direction == "no_diff":
        p = permutation_pvalue(a, c, iterations=PERMUTATION_ITERS) if a and c else 1.0
        verdict = "SUPPORTED" if p is not None and p >= 0.05 else "REJECTED"
    else:
        p = permutation_pvalue(a, c, iterations=PERMUTATION_ITERS) if a and c else 1.0
        if p is None or p >= 0.05:
            verdict = "REJECTED"
        elif spec.direction == "higher" and delta is not None and delta > 0:
            verdict = "SUPPORTED"
        elif spec.direction == "bool_higher" and delta is not None and delta > 0:
            verdict = "SUPPORTED"
        elif spec.direction == "lower" and delta is not None and delta < 0:
            verdict = "SUPPORTED"
        else:
            verdict = "REJECTED"

    return HypothesisResult(
        etf_code=etf_code,
        hypothesis_id=spec.id,
        title=spec.title,
        prediction=spec.prediction,
        n_add=n_a,
        n_ctrl=n_c,
        mean_add=m_a,
        mean_ctrl=m_c,
        delta=delta,
        p_value=p,
        verdict=verdict,
        macro_note=macro_note,
    )


def test_ma_contradiction(event_rows, ctrl_rows, etf_code: str) -> HypothesisResult:
    """H2 延伸：同時檢定 ma20_rising↑ 且 above_ma20↓。"""
    rising = test_hypothesis(
        etf_code=etf_code,
        spec=HypothesisSpec("H2a", "MA20上行", "", "ma20_rising", "bool_higher"),
        event_rows=event_rows,
        ctrl_rows=ctrl_rows,
        tech={},
    )
    above = test_hypothesis(
        etf_code=etf_code,
        spec=HypothesisSpec("H2b", "站MA20下", "", "above_ma20", "lower"),
        event_rows=event_rows,
        ctrl_rows=ctrl_rows,
        tech={},
    )
    if rising.verdict == "SUPPORTED" and above.verdict == "SUPPORTED":
        verdict = "SUPPORTED"
    elif rising.verdict == "INSUFFICIENT" or above.verdict == "INSUFFICIENT":
        verdict = "INSUFFICIENT"
    else:
        verdict = "PARTIAL"

    return HypothesisResult(
        etf_code=etf_code,
        hypothesis_id="H2_ma_turn",
        title="MA20 轉強但未站上（矛盾緩解）",
        prediction="ma20_rising↑ 且 above_ma20↓",
        n_add=rising.n_add,
        n_ctrl=rising.n_ctrl,
        mean_add=rising.mean_add,
        mean_ctrl=rising.mean_ctrl,
        delta=rising.delta,
        p_value=rising.p_value,
        verdict=verdict,
        macro_note=f"above_ma20 Δ={above.delta} p={above.p_value}",
    )


def infer_intent(profile: EtfProfile, results: list[HypothesisResult], effects: list[FactorEffect]) -> str:
    by_id = {r.hypothesis_id: r for r in results}
    parts: list[str] = []

    h1 = by_id.get("H1_rs14")
    h4 = by_id.get("H4_not_momentum_816")
    h2 = by_id.get("H2_ma_turn")
    h5 = by_id.get("H5_dip_rs")

    if profile.archetype == "index_rebalance":
        if h4 and h4.verdict == "SUPPORTED":
            parts.append("調倉主導，事前絕對動能與控制組無差，**非追漲選股**。")
        elif h4 and h4.verdict == "REJECTED":
            parts.append("雖標榜指數型，但加碼股事前動能仍偏高，可能含主動 overlay 或資料期內特殊調整。")
        else:
            parts.append("樣本不足以區分再平衡 vs 主動。")

    if h1 and h1.verdict == "SUPPORTED":
        parts.append(f"成分內**相對強勢選股**（rs14 Δ={h1.delta}，p={h1.p_value}）。")
    elif h1 and h1.verdict == "REJECTED":
        parts.append("未見穩定相對強勢選股特徵。")

    if h2 and h2.verdict == "SUPPORTED":
        parts.append("均線型態：**MA 轉彎但價在均線下**（拉回轉強，非突破追價）。")
    elif h2 and h2.verdict == "PARTIAL":
        parts.append("均線訊號混合，不宜簡化為單一型態。")

    if h5 and h5.verdict == "SUPPORTED":
        parts.append("**夜盤跌日仍買相對強** → 矛盾緩解：環境弱時選強股，非 blind 追高。")

    top = sorted(effects, key=lambda e: abs(e.delta_add_ctrl or 0), reverse=True)[:2]
    if top and top[0].delta_add_ctrl is not None:
        parts.append(f"最強事前因子：{top[0].label}（Δ加-控={top[0].delta_add_ctrl}）。")

    if not parts:
        return "資料不足，無法歸納策略目的。"
    return " ".join(parts)


def macro_slices(event_rows, ctrl_rows, tech: dict[str, dict]) -> list[MacroSlice]:
    slices: list[MacroSlice] = []
    for label, cond in [
        ("夜盤跌 (tx_gap<0)", "tx_gap_neg"),
        ("夜盤漲 (tx_gap≥0)", "tx_gap_pos"),
        ("SOX跌", "sox_neg"),
        ("電子期跌 (te<0)", "te_overnight_neg"),
    ]:
        dates = _macro_dates(tech, cond)
        if not dates:
            continue
        slices.append(
            MacroSlice(
                label=label,
                n_add_days=len({r.event_date for r in event_rows if r.side == "add" and r.event_date in dates}),
                add_ret14=_mean(_get_vals(event_rows, "add", "ret14", dates)),
                ctrl_ret14=_mean(_get_vals(ctrl_rows, "control", "ret14", dates)),
                add_rs14=_mean(_get_vals(event_rows, "add", "rs_univ14", dates)),
                ctrl_rs14=_mean(_get_vals(ctrl_rows, "control", "rs_univ14", dates)),
                add_ret5=_mean(_get_vals(event_rows, "add", "ret5", dates)),
                ctrl_ret5=_mean(_get_vals(ctrl_rows, "control", "ret5", dates)),
            )
        )
    return slices


def _strategy_archetype_appendix() -> list[str]:
    """附錄：策略原型理解校正版（靜態說明，不依當次數值重算）。"""
    return [
        "",
        "## 附錄 A · 策略原型理解校正版",
        "",
        "> 本附錄說明五種已驗證策略原型之**正確解讀**。常見誤區：把「ETF 之間選基」"
        "誤當「經理人在成分股之間選股」，或把**觀察到的加碼特徵**當成**可執行的交易規則**。",
        "",
        "### 總體校正",
        "",
        "| 常見誤解 | 本研究實際量測 |",
        "|----------|----------------|",
        "| 池子 = 00403A、00981A 等 ETF | 池子 = **各 ETF 成分股**（個股） |",
        "| 規則 = 挑哪檔 ETF 買 | 結論 = **被加碼個股**之事前特徵 |",
        "| 可當跟單 SOP | **Revealed preference** 描述，非投資建議 |",
        "",
        "---",
        "",
        "### A.1 Cross-sectional RS Selection",
        "",
        "**一句話**：經理人在自己的成分池裡，傾向加碼事前 14 日**跑贏同池同儕**的股票。",
        "",
        "- **Cross-sectional**：變動當日橫向比較成分股，非 ETF 之間比較。",
        "- **rs_univ14** = 個股 14 日報酬 − **同日、同 ETF universe 成分股中位數**（非大盤、非 ETF NAV）。",
        "- **顯著 ETF**：00403A、00981A、009816（成分內皆成立；金融池 vs 科技池含義不同）。",
        "",
        "**正確例子**：00981A 成分上百檔 → 算每檔 rs_univ14 → 被**加碼**者顯著高於同日**未變動**成分股。",
        "",
        "**不是**：挑「贏 009816 的 ETF」；也不是保證事後漲。",
        "",
        "---",
        "",
        "### A.2 Sector Alpha (Electronic)",
        "",
        "**一句話**：00981A 傾向加碼事前**跑贏電子指 IR0002** 的個股；**SOX 隔夜下跌**日此 pattern 仍顯著。",
        "",
        "- **excess_ir14** = 個股 14 日報酬 − **IR0002** 14 日報酬（產業 alpha 基準）。",
        "- **SOX** = **宏觀 regime**（美股半導體隔夜），用於 H7 條件檢定，**不是** excess_ir14 的減數。",
        "- 池子 = **00981A 成分股**，非「電子 ETF 之間」比較。",
        "",
        "| 指標 | 問的問題 |",
        "|------|----------|",
        "| rs_univ14 | 誰贏**同 ETF 成分中位數**？ |",
        "| excess_ir14 | 誰贏**電子產業指數**？ |",
        "",
        "**不是**：看空 SOX 才買；僅述「SOX 弱時仍做電子內選強」。",
        "",
        "---",
        "",
        "### A.3 MA Regime: Turn-up Below MA",
        "",
        "**一句話**：009816 加碼標的常處 **MA20 斜率已轉正、收盤仍多在 MA20 下方** 的技術區間。",
        "",
        "- **ma20_rising ↑**：加碼組 MA20 五日上行比例顯著高於控制。",
        "- **above_ma20 ↓**：加碼組「站上 MA20」比例顯著**低於**控制（橫截面，非「站上天數隨時間下降」）。",
        "- **主驅動**：009816；pooled 全樣本結論多由此而來，**非**跨 ETF 通用。",
        "",
        "**型態**：均線拐頭、價在均線下 → 修復期／轉強初期，**非**深跌左側抄底（多數加碼股 14 日報酬仍為正）。",
        "",
        "---",
        "",
        "### A.4 Macro-conditioned Picking",
        "",
        "**一句話**：台指期隔夜 gap 偏弱（tx_gap<0）日，經理人加碼的個股仍呈現**成分內相對強勢**。",
        "",
        "- **tx_gap_pct**：FinMind TX 盤後／夜盤相對前日 IX0001 收盤之 gap。",
        "- **H5**：條件子樣本內 rs_univ14 加碼組仍 > 控制（00403A、009816 顯著）。",
        "- 全樣本描述：夜盤跌日加碼股 ret5 ≈ **−3.1%**、rs14 ≈ **+7.4%** → 環境弱時**選強不選弱**。",
        "",
        "**不是**：你先判斷 tx_gap<0 再啟動「防禦選基」；而是**事後觀察**弱日加碼股的特徵。",
        "",
        "---",
        "",
        "### A.5 Aggressive Momentum Overlay",
        "",
        "**一句話**：00403A 在宏觀偏弱日，除成分內選強外，加碼標的**短線動能（ret5）仍偏高**——積極順勢 overlay。",
        "",
        "- **ret5** = **被加碼個股** 5 日報酬，非 00403A 這檔 ETF 的 NAV 報酬。",
        "- **H6**（夜盤跌日 ret5 不顯著高於控制）：00403A **REJECTED** → 否認「弱市不追高」。",
        "- 009816 同條件 H6 邊際通過 → 較像弱市選強、**不追短線**。",
        "",
        "| ETF | 夜盤弱仍 RS 高 (H5) | 夜盤弱 ret5 仍高 (H6) |",
        "|-----|---------------------|------------------------|",
        "| 00403A | ✓ | ✓ 積極 overlay |",
        "| 009816 | ✓ | ✗ 較防守 |",
        "",
        "**不是**：夜盤跌就加碼 00403A 這檔 ETF。",
        "",
        "---",
        "",
        "### A.6 五原型對照",
        "",
        "| 原型 | 正確解讀 | 主要 ETF | 核心指標（個股層級） |",
        "|------|----------|----------|----------------------|",
        "| Cross-sectional RS | 成分內贏同儕 | 00403A、00981A、009816 | rs_univ14 |",
        "| Sector Alpha (Elec) | 贏電子指 IR0002 | 00981A | excess_ir14；SOX↓ 子樣本 |",
        "| MA Turn-up Below MA | 均線轉強、價在均線下 | 009816 | ma20_rising↑, above_ma20↓ |",
        "| Macro-conditioned | 夜盤弱仍選相對強 | 00403A、009816 | tx_gap<0 時 rs_univ14 |",
        "| Aggressive Overlay | 夜盤弱且短線仍強 | 00403A | tx_gap<0 時 ret5 |",
        "",
        "### A.7 研究口吻總結",
        "",
        "在樣本期（約 2026/6 初）內，多檔 ETF 經理人加碼時標的普遍呈現**成分內相對強勢**；"
        "00981A 另具**電子指超額**；009816 常見**均線轉強但價在均線下**；"
        "夜盤 gap 偏弱時 00403A／009816 仍做相對強度選股，且 **00403A 另疊加短線動能**。"
        "以上描述經理人決策特徵，**不構成**可複製之交易 alpha 或跟單規則。",
    ]


def study_etf(
    conn: sqlite3.Connection,
    etf_code: str,
    all_legs: list[FlowLeg],
    tech: dict[str, dict],
) -> EtfStudyResult:
    profile = ETF_PROFILES[etf_code]
    raw = legs_for_etf(all_legs, etf_code)
    unique = unique_stock_days(raw)
    universe = etf_universe(conn, etf_code)
    if not universe:
        return EtfStudyResult(
            profile=profile,
            n_add_legs=0,
            n_reduce_legs=0,
            n_add_unique=0,
            intent_summary="無持股成分資料。",
        )

    ev, ctrl = build_feature_rows(conn, unique, universe)
    effects = screen_factors(ev, ctrl)

    results: list[HypothesisResult] = []
    for spec in HYPOTHESES:
        if spec.id == "H2_ma_turn":
            continue
        r = test_hypothesis(
            etf_code=etf_code,
            spec=spec,
            event_rows=ev,
            ctrl_rows=ctrl,
            tech=tech,
        )
        if r:
            results.append(r)
    results.append(test_ma_contradiction(ev, ctrl, etf_code))

    intent = infer_intent(profile, results, effects)
    return EtfStudyResult(
        profile=profile,
        n_add_legs=sum(1 for l in raw if l.side == "add"),
        n_reduce_legs=sum(1 for l in raw if l.side == "reduce"),
        n_add_unique=sum(1 for l in unique if l.side == "add"),
        hypothesis_results=results,
        top_effects=sorted(effects, key=lambda e: abs(e.delta_add_ctrl or 0), reverse=True)[:5],
        intent_summary=intent,
    )


def build_report(
    *,
    studies: list[EtfStudyResult],
    macro_all: list[MacroSlice],
    snapshot_dates: list[str],
    missing_macro: list[str],
    tech: dict[str, dict],
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# ETF 操盤策略假設檢定（分 ETF）",
        "",
        f"> 產出 {now} · **目的：理解經理人策略，非跟單訊號** · 僅事前特徵",
        "",
        "## 方法論",
        "",
        "1. **假設**：每檔 ETF 依產品定位提出可證偽預測（見下表 `H*`）。",
        "2. **事件**：該 ETF 持股 snapshot 間加碼/減碼 leg（去重 stock-day）。",
        "3. **控制**：同日、同 ETF 成分股內**未變動**標的。",
        "4. **檢定**：Permutation test（5000 次）；p<0.05 且方向符合 → SUPPORTED。",
        "5. **宏觀層**：`tech_risk_daily_snapshot`（台指期 TX gap、電子期 TE 隔夜、TSM/SOX）"
        " — 用於解釋「大盤強但個股在均線下」等矛盾。",
        "",
        "### 為何引入台指期夜盤？",
        "",
        "| 矛盾 | 夜盤可提供的解釋 |",
        "|------|------------------|",
        "| 加碼股絕對動能高 vs 未站上 MA | 可能在前夜 gap 跌後日間挑**相對強**股（H5/H6） |",
        "| 009816 動能 vs 再平衡敘事 | 金融 ETF 主導全樣本；分 ETF 後檢定 H4 |",
        "| 半導體選股 vs 大盤 beta | excess_ir14 與 excess_ix14 分離；SOX 跌日檢定 H7 |",
        "",
        "## 資料覆蓋",
        "",
        f"- 持股 snapshot：**{snapshot_dates[0]} ~ {snapshot_dates[-1]}**（{len(snapshot_dates)} 日）",
        f"- tech_risk 列數：**{len(tech)}**",
    ]
    if missing_macro:
        lines.append(f"- ⚠ 缺夜盤 macro 日期：{', '.join(missing_macro)}（可 `--sync-context` 補）")
    else:
        lines.append("- tech_risk 已覆蓋主要變動日")

    lines.extend(["", "## 宏觀分層（全 ETF 加碼合計）", ""])
    lines.append("| 環境 | 加碼日數 | 加碼 ret14 | 控制 ret14 | 加碼 rs14 | 控制 rs14 | 加碼 ret5 |")
    lines.append("|------|----------|------------|------------|-----------|-----------|-----------|")
    for s in macro_all:
        lines.append(
            f"| {s.label} | {s.n_add_days} | {s.add_ret14 or '—'} | {s.ctrl_ret14 or '—'} "
            f"| {s.add_rs14 or '—'} | {s.ctrl_rs14 or '—'} | {s.add_ret5 or '—'} |"
        )

    lines.extend([
        "",
        "> **解讀**：若夜盤跌日 ret5 接近控制但 rs14 仍高 → 「環境弱時選強股」成立，緩解追高矛盾。",
        "",
        "## 分 ETF 策略歸納",
        "",
    ])

    for study in studies:
        p = study.profile
        lines.append(f"### {p.code} · {p.name}（{p.manager}）")
        lines.append("")
        lines.append(f"**定位**：{p.archetype} — {p.intent}")
        lines.append("")
        lines.append(
            f"樣本：加碼 leg {study.n_add_legs} · 減碼 {study.n_reduce_legs} · "
            f"去重加碼 stock-day {study.n_add_unique}"
        )
        lines.append("")
        lines.append(f"**策略歸納**：{study.intent_summary}")
        lines.append("")
        lines.append("| 假設 | 預測 | n加/n控 | Δ加-控 | p | 裁決 |")
        lines.append("|------|------|---------|--------|---|------|")
        for r in study.hypothesis_results:
            pstr = f"{r.p_value:.3f}" if r.p_value is not None else "—"
            note = f" ({r.macro_note})" if r.macro_note else ""
            lines.append(
                f"| {r.hypothesis_id} {r.title}{note} | {r.prediction[:30]}… | "
                f"{r.n_add}/{r.n_ctrl} | {r.delta if r.delta is not None else '—'} | {pstr} | **{r.verdict}** |"
            )
        lines.append("")
        lines.append("| 事前因子 | Δ加-控 |")
        lines.append("|----------|--------|")
        for ef in study.top_effects[:5]:
            u = "pp" if ef.kind == "bool" else ""
            lines.append(f"| {ef.label} | {ef.delta_add_ctrl}{u} |")
        lines.append("")

    lines.extend([
        "---",
        "",
        "### 重新產出",
        "",
        "```bash",
        "python src/etf_flow_hypothesis.py --run --sync-context --write-report",
        "```",
    ])
    lines.extend(_strategy_archetype_appendix())
    lines.extend([
        "",
        "### 侷限",
        "",
        "- 樣本期約 2 週；減碼與小 ETF 常 INSUFFICIENT。",
        "- 重複加碼同一股票會膨脹 leg 數；策略解讀以 stock-day 為準。",
        "- 本報告不解釋**為何**調整權重（需搭配持倉比例與指數規則）。",
    ])
    return "\n".join(lines)


def run_study(main_db: Path, *, sync_context: bool) -> tuple[list[EtfStudyResult], list[MacroSlice], list[str], list[str], dict]:
    with connect(main_db) as conn:
        all_legs = collect_flow_legs(conn)
        dates = sorted(
            {r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM etf_holdings").fetchall()}
        )
        missing = ensure_tech_risk_coverage(conn, dates, sync=sync_context)
        tech = load_tech_risk_map(conn)

        studies = [study_etf(conn, code, all_legs, tech) for code in ETF_CODES_LISTED]

        # pooled macro (all unique add stock-days)
        unique_all = unique_stock_days(all_legs)
        universe_all = [r[0] for r in conn.execute("SELECT DISTINCT stock_id FROM etf_holdings").fetchall()]
        ev_all, ctrl_all = build_feature_rows(conn, unique_all, universe_all)
        macro_all = macro_slices(ev_all, ctrl_all, tech)

        return studies, macro_all, dates, missing, tech


def main() -> int:
    parser = argparse.ArgumentParser(description="ETF 操盤策略假設檢定（分 ETF）")
    parser.add_argument("--main-db", type=Path, default=DEFAULT_MAIN_DB)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--sync-context", action="store_true", help="缺 tech_risk 時從 FinMind/Yahoo 補")
    parser.add_argument("--write-report", action="store_true")
    args = parser.parse_args()

    if not args.run and not args.write_report:
        args.run = args.write_report = True

    studies, macro_all, dates, missing, tech = run_study(args.main_db, sync_context=args.sync_context)

    for s in studies:
        sup = sum(1 for r in s.hypothesis_results if r.verdict == "SUPPORTED")
        print(f"{s.profile.code}: add_unique={s.n_add_unique} hypotheses_supported={sup}/{len(s.hypothesis_results)}")

    if args.write_report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        text = build_report(
            studies=studies,
            macro_all=macro_all,
            snapshot_dates=dates,
            missing_macro=missing,
            tech=tech,
        )
        args.report.write_text(text, encoding="utf-8")
        print(f"Report → {args.report}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
