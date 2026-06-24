"""Regime daily · rule-based interpretation copy (author-native framing)."""

from __future__ import annotations

from typing import Any

from regime_daily_guide import quadrant_display, stage_display
from stage_analysis import STAGE_NAMES


def _delta_phrase(val: float | None, *, suffix: str = "pp") -> str:
    if val is None:
        return "變化資料不足"
    if val > 0.5:
        return f"近 5 日上升 {val:.1f}{suffix}"
    if val < -0.5:
        return f"近 5 日下降 {abs(val):.1f}{suffix}"
    return f"近 5 日大致持平（{val:+.1f}{suffix}）"


def interpret_market_structure(
    b: dict[str, Any],
    t: dict[str, Any],
    r: dict[str, Any],
    s: dict[str, Any],
    *,
    bench: str = "IX0001",
) -> str:
    parts: list[str] = []
    if t.get("available"):
        stage = t.get("stage")
        name = t.get("stage_name") or STAGE_NAMES.get(int(stage or 0), "unknown")
        parts.append(f"{bench} · Weinstein {stage_display(stage, name)}")
    if b.get("available"):
        parts.append(
            f"% above 200-day MA {b.get('pct_above_200')}%（{b.get('display')}）"
        )
        rhythm = b.get("rhythm") or {}
        if rhythm.get("available"):
            parts.append(
                f"Zweig EMA rhythm {rhythm.get('zweig_ema_pct')}%（{rhythm.get('display')}）"
            )
        impulse = b.get("impulse") or {}
        if impulse.get("available"):
            if impulse.get("thrust_active"):
                rem = int(impulse.get("thrust_days_remaining") or 0)
                parts.append(f"Thrust 窗口 active（剩 {rem} 日）")
            elif impulse.get("zweig_thrust_today") or impulse.get("deemer_bam_today"):
                parts.append("Thrust 事件今日觸發")
    if r.get("available"):
        health = float(r.get("rotation_health_pct") or 0)
        parts.append(f"RRG Leading + Improving {health:.1f}%")
    if s.get("available"):
        parts.append(f"Minervini template pass rate {s.get('pass_pct')}%")
    if not parts:
        return "資料不足，無法合成 Daily synopsis。"
    body = "；".join(parts) + "。"
    if b.get("available") and s.get("available"):
        gap = float(b.get("participation_gap") or 0)
        if str(b.get("breadth_zone_200")) == "overbought" and float(s.get("pass_pct") or 0) >= 45:
            body += " 200-day 廣度偏高但 Minervini pass rate 仍高，漲幅擴散較廣。"
        elif str(b.get("breadth_zone_200")) == "overbought" and gap < -5:
            body += " 50-day 廣度低於 200-day，短線擴散略收窄。"
    composite = interpret_breadth_composite(b)
    if composite:
        body += f" {composite}"
    return body


def interpret_breadth_level(b: dict[str, Any]) -> str:
    if not b.get("available"):
        return b.get("error", "N/A")
    zone = str(b.get("breadth_zone_200") or "")
    p50 = float(b.get("pct_above_50") or 0)
    p200 = float(b.get("pct_above_200") or 0)
    gap = float(b.get("participation_gap") or 0)
    lines = [
        f"**% above 200-day MA {p200:.1f}%**（{b.get('display')}）；"
        f"**% above 50-day MA {p50:.1f}%**。"
        f"50 vs 200 spread **{gap:+.1f} pp**"
        + ("，50-day 低於 200-day。" if gap < -3 else "。")
    ]
    lines.append(
        f"50-day MA {_delta_phrase(b.get('pct50_delta_5d'))}；"
        f"200-day MA {_delta_phrase(b.get('pct200_delta_5d'))}。"
    )
    if b.get("divergence_flag"):
        lines.append(
            "⚠ **Advance/decline divergence**：指數近 20 日向上而 50-day 廣度走弱。"
        )
    else:
        lines.append("未見指數漲／50-day 廣度降之背離。")
    if zone == "overbought":
        lines.append(
            "200-day 廣度處高位區間；屬環境描述，請搭配 Weinstein Stage 與 RRG 閱讀。"
        )
    elif zone == "oversold":
        lines.append("200-day 廣度處低位，常見於修正或恐慌段。")
    elif zone == "strong":
        lines.append("200-day 廣度偏強，仍宜確認是否集中少數族群。")
    return " ".join(lines)


def interpret_breadth_rhythm(rhythm: dict[str, Any]) -> str:
    if not rhythm.get("available"):
        return rhythm.get("error", "")
    z = rhythm.get("zweig_ema_pct")
    tier = rhythm.get("display") or rhythm.get("zweig_ema_tier")
    parts = [f"**Zweig EMA rhythm tier** · adv/decl 10-day EMA **{z}%**（{tier}）。"]
    parts.append(f"5d Δ {_delta_phrase(rhythm.get('zweig_ema_delta_5d'))}。")
    tier_id = str(rhythm.get("zweig_ema_tier") or "")
    if tier_id == "high":
        parts.append("Rhythm 偏強，代表 adv/decl 慣性高；仍須對照 Level 是否過熱。")
    elif tier_id == "mid":
        parts.append("Rhythm 中等，市場參與節奏尚可。")
    elif tier_id == "low":
        parts.append("Rhythm 偏低，adv/decl 慣性弱。")
    elif tier_id == "off":
        parts.append("Rhythm 關閉區間，adv/decl 慣性極弱。")
    return " ".join(parts)


def interpret_breadth(b: dict[str, Any]) -> str:
    """Level-only notes (legacy section body)."""
    return interpret_breadth_level(b)


def interpret_breadth_composite(b: dict[str, Any]) -> str:
    if not b.get("available"):
        return ""
    zone = str(b.get("breadth_zone_200") or "")
    rhythm = b.get("rhythm") or {}
    impulse = b.get("impulse") or {}
    tier = str(rhythm.get("zweig_ema_tier") or "")
    thrust_active = bool(impulse.get("thrust_active"))
    if zone == "overbought" and not thrust_active and tier in ("mid", "high"):
        return "綜合：200MA Level 偏高、Zweig EMA rhythm 中等偏強，但 Thrust 窗口未 active → 高位慣性，非剛點火。"
    if zone in ("oversold", "weak") and thrust_active:
        return "綜合：廣度 Level 偏低但 Thrust 窗口 active → 留意底部推力事件。"
    if thrust_active and tier == "high":
        return "綜合：Rhythm 偏強且 Thrust 窗口進行中 → 廣度動能與事件同向。"
    return ""


def interpret_breadth_impulse(imp: dict[str, Any]) -> str:
    if not imp.get("available"):
        return imp.get("error", "") if imp else ""
    parts: list[str] = []
    if imp.get("zweig_thrust_today"):
        parts.append("今日 **Zweig Breadth Thrust** 觸發")
    if imp.get("deemer_bam_today"):
        dr = imp.get("deemer_ratio")
        parts.append(f"今日 **Deemer BAM**（10-day adv/decl={dr}）")
    if imp.get("thrust_active"):
        rem = int(imp.get("thrust_days_remaining") or 0)
        hold = int(imp.get("thrust_hold_days") or 0)
        parts.append(f"Thrust 窗口進行中（剩 {rem}/{hold} 交易日）")
    elif imp.get("zweig_thrust_today") or imp.get("deemer_bam_today"):
        parts.append("Thrust 事件剛觸發")
    else:
        parts.append("Thrust 窗口未 active")
    return " ".join(parts)


def interpret_trend(t: dict[str, Any], *, bench: str) -> str:
    if not t.get("available"):
        return t.get("error", "N/A")
    w = t.get("weinstein") or {}
    stage = t.get("stage")
    name = t.get("stage_name") or STAGE_NAMES.get(int(stage or 0), "unknown")
    ext = float(w.get("extension_pct") or 0)
    slope = float(w.get("ma_slope_pct") or 0)
    parts = [
        f"**{bench}** 週線 **{stage_display(stage, name)}**："
        f"收盤 {'在' if w.get('price_above_ma30') else '不在'} **30-week MA** 上，"
        f"MA 斜率 {slope:+.2f}%，"
        f"偏離 30-week MA **{ext:+.1f}%**，"
        f"higher lows {'成立' if w.get('higher_lows') else '未成立'}。"
    ]
    if stage == 2 and ext > 25:
        parts.append("偏離 MA 較大，屬 Stage 2 後段（Weinstein topping 觀察區，非賣出訊號）。")
    elif stage == 2:
        parts.append("結構符合 Stage 2 advancing 描述。")
    elif stage == 3:
        parts.append("Stage 3：趨勢減速或橫盤築頂常見區。")
    elif stage == 4:
        parts.append("Stage 4：主要趨勢向下。")
    m = t.get("minervini") or {}
    met = m.get("criteria_met")
    total = m.get("criteria_total")
    if met is not None and total:
        parts.append(
            f"**Minervini Trend Template**（指數）**{met}/{total}** passed；"
            f"{'Stage 2 型結構仍完整' if met >= 6 else '部分條件未滿足'}。"
        )
    return " ".join(parts)


def interpret_rrg(r: dict[str, Any]) -> str:
    if not r.get("available"):
        return r.get("error", "N/A")
    health = float(r.get("rotation_health_pct") or 0)
    parts = [
        f"**Leading + Improving** 占樣本 **{health:.1f}%**"
        f"（{quadrant_display('leading')} {r.get('leading_pct')}% · "
        f"{quadrant_display('improving')} {r.get('improving_pct')}%）。"
        f"{quadrant_display('weakening')} {r.get('weakening_pct')}% · "
        f"{quadrant_display('lagging')} {r.get('lagging_pct')}%。"
    ]
    if health >= 55:
        parts.append("Leading／Improving 占多數，相對輪動偏強。")
    elif health >= 45:
        parts.append("四象限分散，宜看 migration 與 symbol table，不宜只看最大象限。")
    else:
        parts.append("Lagging 占比高，留意 Improving → Leading 遷移。")
    mig = r.get("migrations") or {}
    imp_lead = int(mig.get("improving_to_leading") or 0)
    lead_weak = int(mig.get("leading_to_weakening") or 0)
    lag_imp = int(mig.get("lagging_to_improving") or 0)
    weak_lag = int(mig.get("weakening_to_lagging") or 0)
    if imp_lead or lead_weak or lag_imp or weak_lag:
        parts.append(
            f"1-day migration：Improving→Leading **{imp_lead}** · "
            f"Leading→Weakening **{lead_weak}** · "
            f"Lagging→Improving **{lag_imp}** · "
            f"Weakening→Lagging **{weak_lag}**。"
        )
    return " ".join(parts)


def interpret_stage2(s: dict[str, Any], b: dict[str, Any] | None = None) -> str:
    if not s.get("available"):
        return s.get("error", "N/A")
    pct = float(s.get("pass_pct") or 0)
    parts = [
        f"樣本 **{s.get('universe_n')}** 檔中 **{pct:.1f}%** 通過 "
        f"**Minervini Trend Template**（≥{s.get('min_criteria')}/{s.get('criteria_total')}，RS omitted）。"
        f" {_delta_phrase(s.get('pass_delta_5d'))}。"
    ]
    if b and b.get("available"):
        zone = str(b.get("breadth_zone_200") or "")
        if zone in ("overbought", "strong") and pct >= 50:
            parts.append("% above MA 高且 pass rate >50% → 廣泛 Stage 2 參與。")
        elif zone in ("overbought", "strong") and pct < 40:
            parts.append("廣度高但 pass rate 偏低 → 可能少數 leadership 拉指數。")
        elif zone in ("oversold", "weak") and pct < 30:
            parts.append("廣度與 pass rate 均偏低。")
    return " ".join(parts)


def interpret_overview_plain_zh(
    b: dict[str, Any],
    t: dict[str, Any],
    r: dict[str, Any],
    s: dict[str, Any],
) -> str:
    """首頁／日報一句話結論（白話優先 · 術語首用 English（中文））。"""
    if not any(x.get("available") for x in (b, t, r, s)):
        return "資料不足，尚無法整理今日市場總覽。"

    clauses: list[str] = []

    if t.get("available"):
        stage = t.get("stage")
        name = str(t.get("stage_name") or "")
        if stage == 2 and name == "advancing":
            clauses.append(
                "大盤仍在 Weinstein Stage 2（第 2 階段）多頭結構"
            )
        elif stage is not None:
            stage_name_zh = {
                "advancing": "上升",
                "topping": "築頂",
                "declining": "下降",
                "basing": "築底",
            }.get(name, name)
            clauses.append(f"大盤趨勢階段為 Stage {stage}（{stage_name_zh}）")

    if b.get("available"):
        p200 = float(b.get("pct_above_200") or 0)
        zone = str(b.get("breadth_zone_200") or "")
        if zone == "overbought" or p200 > 80:
            tone = "偏熱"
        elif p200 > 60:
            tone = "偏強"
        else:
            tone = "偏弱"
        clauses.append(f"Market breadth（市場廣度）{p200:.0f}%，整體{tone}")

    if r.get("available"):
        health = round(
            float(r.get("leading_pct") or 0) + float(r.get("improving_pct") or 0)
        )
        if health >= 50:
            clauses.append(
                f"Relative Rotation Graph（RRG）健康度約 {health}%，輪動結構尚可"
            )
        else:
            clauses.append(
                f"RRG 健康度約 {health}%，強勢族群占比偏低"
            )
        mig = r.get("migrations") if isinstance(r.get("migrations"), dict) else {}
        n = int(mig.get("improving_to_leading") or 0)
        if n > 0:
            clauses.append(f"今日有 {n} 個族群由轉強進入領先象限")

    if s.get("available") and not clauses:
        pct = float(s.get("pass_pct") or 0)
        clauses.append(f"Stage 2 participation（第 2 階段參與率）{pct:.0f}%")

    if not clauses:
        return "今日市場資料有限，請至日報查看完整圖表。"

    zone = str(b.get("breadth_zone_200") or "") if b.get("available") else ""
    if zone == "overbought" and t.get("stage") == 2:
        lead = "目前市場仍偏強，但位置已不低，屬於高檔延續而不是剛起漲。"
        return f"{lead}{'；'.join(clauses)}。"
    if zone in ("oversold", "weak"):
        return f"目前市場偏弱，宜保守看待風險。{'；'.join(clauses)}。"

    return f"{'；'.join(clauses)}。"
