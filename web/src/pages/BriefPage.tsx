import { LayerBadge } from "../components/LayerBadge";
import { RegimeEmbed } from "../components/RegimeEmbed";
import { MarkdownBrief } from "../components/MarkdownBrief";
import type { DailyBrief, BriefTab } from "../types";
import { BRIEF_LABELS } from "../types";

interface Props {
  tab: BriefTab;
  brief: DailyBrief | null;
}

const LAYER_MAP = {
  regime: "regime" as const,
  etf: "facts" as const,
  vcp: "research" as const,
};

export function BriefPage({ tab, brief }: Props) {
  if (!brief) {
    return <div className="error">此日期尚無 {tab} brief 資料</div>;
  }

  const meta = BRIEF_LABELS[brief.brief_type];
  const layer = LAYER_MAP[tab];

  return (
    <>
      <LayerBadge layer={layer} />
      <h2 style={{ marginTop: 0 }}>{meta.zh}</h2>
      {tab === "regime" && brief.content_html ? (
        <RegimeEmbed html={brief.content_html} />
      ) : (
        <MarkdownBrief md={brief.content_md} />
      )}
      <p className="footer-meta">
        source: {brief.source_path ?? "—"} · synced {brief.synced_at}
      </p>
    </>
  );
}
