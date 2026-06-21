export type BriefType =
  | "regime_daily"
  | "etf_daily"
  | "vcp_funnel_specs"
  | "rrg_mono_intraday";

export type BriefTab = "regime" | "etf" | "vcp";

export interface DailyBrief {
  id: string;
  trade_date: string;
  schedule_slot: "1300" | "1630";
  brief_type: BriefType;
  title: string;
  content_md: string;
  content_html: string | null;
  source_path: string | null;
  synced_at: string;
}

export interface RegimeTeaser {
  breadth200: string | null;
  breadthLabel: string | null;
  stage: string | null;
  rrgPct: string | null;
  passRate: string | null;
  synopsis: string | null;
}

export const BRIEF_LABELS: Record<BriefType, { zh: string; layer: string }> = {
  regime_daily: { zh: "市場結構日報", layer: "regime" },
  etf_daily: { zh: "ETF 持股日報", layer: "facts" },
  vcp_funnel_specs: { zh: "VCP 漏斗研究", layer: "research" },
  rrg_mono_intraday: { zh: "RRG 盤中監控", layer: "research" },
};

export const TAB_TO_BRIEF: Record<BriefTab, BriefType> = {
  regime: "regime_daily",
  etf: "etf_daily",
  vcp: "vcp_funnel_specs",
};
