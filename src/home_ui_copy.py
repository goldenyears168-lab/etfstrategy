"""首頁靜態殼層文案 SSOT · 同步至 readdy-490731/src/lib/uiCopy.generated.ts"""

from __future__ import annotations

from lens_ui_copy import PIT_FOOTNOTE_ZH

HOME_HERO_BADGE_ZH = "每日台股觀察"
HOME_HERO_TITLE_ZH = "每日看懂台股強弱與策略訊號"
HOME_HERO_SUBTITLE_ZH = (
    "每天收盤後更新，快速整理大盤趨勢、類股輪動、ETF 持股變化，"
    "以及今天有哪些策略出現候選股。"
)
HOME_HERO_PIT_ZH = PIT_FOOTNOTE_ZH
HOME_HERO_CTA_PRIMARY_ZH = "先看今天總覽"
HOME_HERO_CTA_SECONDARY_ZH = "了解策略怎麼選股"

HOME_OVERVIEW_TITLE_ZH = "今天市場總覽"
HOME_OVERVIEW_DATE_PREFIX_ZH = "最新更新日"
HOME_OVERVIEW_TECH_LABEL_ZH = "技術狀態"
HOME_OVERVIEW_BODY_ZH = (
    "這裡先看今天市場的大方向。首頁適合快速掃描，日報適合細看圖表與依據。"
)
HOME_OVERVIEW_CTA_ZH = "查看完整日報"

HOME_BRIEF_PICKER_TITLE_ZH = "你想先看哪一種資訊？"
HOME_BRIEF_PICKER_BODY_ZH = "依照你的需求，直接進到對應的日報或策略頁面。"

HOME_BRIEF_HINT_ZH: dict[str, str] = {
    "etf_daily": "看 ETF 今天增減碼了哪些股票，以及持股結構怎麼變。",
    "regime_daily": "看今天整體大盤偏強還是偏弱，搭配多個指標一起判讀。",
    "rrg_mono_daily": "看主要族群或個股，目前落在輪動圖的哪個象限與方向。",
    "copytrade_l1h9": "從 ETF 成分與持股變化，整理出值得追蹤的標的清單。",
    "vcp_pivot_gate": "找出整理後接近突破、條件已接近完成的股票。",
    "vcp_coil_close": "收盤後確認今天有哪些 VCP 訊號正式成立。",
    "vcp_funnel_specs": "VCP 漏斗研究對照，觀察哪些股票正在靠近 VCP 條件的門口。",
    "rrg_mono_intraday": "盤中先看輪動方向的變化，觀察強弱是否有轉折跡象。",
    "rrg_mono_swap_accel_daily": "收盤後看隔日 fresh mono 候選、持倉四日加速與換倉門檻接近度。",
    "rrg_c18acc_screen": "盤中 C0 scale 進場與 5 分鐘 poll 換倉 live screen（C18acc）。",
}


def attach_brief_display_hint(
    snapshot_json: dict[str, object] | None,
    brief_type: str,
) -> dict[str, object] | None:
    """Inject display.hint_zh for homepage brief picker · SSOT HOME_BRIEF_HINT_ZH."""
    if snapshot_json is None:
        return None
    hint = HOME_BRIEF_HINT_ZH.get(brief_type)
    if not hint:
        return snapshot_json
    display = snapshot_json.get("display")
    merged_display: dict[str, object] = (
        dict(display) if isinstance(display, dict) else {}
    )
    merged_display["hint_zh"] = hint
    out = dict(snapshot_json)
    out["display"] = merged_display
    return out

HOME_REGIME_TITLE_ZH = "今天適合積極做多嗎？"
HOME_REGIME_BODY_ZH = (
    "這一區用幾個常見的市場指標，幫你判斷現在是偏強、偏弱，還是已經有點過熱。"
)
HOME_REGIME_CTA_ZH = "查看市場環境完整內容"
HOME_REGIME_MIGRATION_TITLE_ZH = "今日輪動位置變化"

HOME_REGIME_CHART_TITLE_ZH: dict[str, str] = {
    "rrg": "強勢輪動比例",
    "breadth": "站上長期均線的股票比例",
    "trend": "大盤趨勢階段",
    "stage2": "符合偏強條件的股票比例",
}

HOME_STRATEGY_TITLE_ZH = "今天有哪些策略有出現機會？"
HOME_STRATEGY_BODY_ZH = "每套策略獨立產出；這裡讓你快速看到今天有沒有跑出候選名單。"

HOME_LENS_INTRO_ZH = (
    "今天和昨天相比，有哪些重點變化？"
    "這裡只挑出值得注意的新變化，包含 ETF 持股異動、輪動位置改變，"
    "或策略條件變得更完整的股票。"
)
HOME_LENS_EMPTY_BODY_ZH = (
    "今天沒有新增明顯的市場結構變化，可以參考日報中的整體強弱指標。"
)
HOME_LENS_MORE_ZH = "查看完整亮點清單"

HOME_KPI_HINT_ZH: dict[str, str] = {
    "breadth_200": "站上長期均線的股票比例；太高通常代表市場偏熱。",
    "trend_stage": "用 Weinstein Stage Analysis（威斯坦階段）看大盤是整理、走多還是轉弱。",
    "rrg_health": "有多少族群仍在相對強勢區；數值越高，輪動結構通常越健康。",
    "stage2_participation": "有多少股票符合 Minervini 中期強勢條件；比例越高，可操作標的通常較多。",
}
