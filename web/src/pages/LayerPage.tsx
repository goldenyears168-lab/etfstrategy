import { LayerIntro } from "../components/LayerIntro";
import { StrategyCatalog } from "../components/StrategyCatalog";
import { SupabaseIntro } from "../components/SupabaseIntro";
import { ExecutionIntro } from "../components/ExecutionIntro";
import { RegimeEmbed } from "../components/RegimeEmbed";
import { MarkdownBrief } from "../components/MarkdownBrief";
import type { LayerId } from "../data/layers";
import { layerById, LAYER_TO_BRIEF } from "../data/layers";
import type { DailyBrief } from "../types";
import { BRIEF_LABELS } from "../types";

interface Props {
  layerId: LayerId;
  date?: string;
  brief: DailyBrief | null;
}

export function LayerPage({ layerId, date, brief }: Props) {
  const layer = layerById(layerId);
  if (!layer) return <div className="error">未知層級</div>;

  return (
    <>
      <LayerIntro layer={layer} />

      {layer.hasDaily && (
        <section className="layer-content">
          <h3 className="section-title">
            日報內容
            {date ? ` · ${date}` : " · 請選擇日期"}
          </h3>
          {!date && (
            <p className="muted-note">使用上方日期選擇器載入 Supabase 已同步 brief。</p>
          )}
          {date && !brief && (
            <div className="error">此日期尚無 {layer.webV1} 資料</div>
          )}
          {date && brief && (
            <>
              <h4>{BRIEF_LABELS[brief.brief_type]?.zh ?? brief.title}</h4>
              {layerId === "regime" && brief.content_html ? (
                <RegimeEmbed html={brief.content_html} />
              ) : (
                <MarkdownBrief md={brief.content_md} />
              )}
              <p className="footer-meta">
                source: {brief.source_path ?? "—"} · synced {brief.synced_at}
              </p>
            </>
          )}
        </section>
      )}

      {layerId === "strategy" && <StrategyCatalog />}
      {layerId === "execution" && <ExecutionIntro />}
      {layerId === "website" && <SupabaseIntro />}
    </>
  );
}

export function briefForLayer(
  layerId: LayerId,
  briefs: DailyBrief[],
): DailyBrief | null {
  const briefType = LAYER_TO_BRIEF[layerId as keyof typeof LAYER_TO_BRIEF];
  if (!briefType) return null;
  return briefs.find((b) => b.brief_type === briefType) ?? null;
}
