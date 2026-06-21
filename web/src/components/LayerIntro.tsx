import type { LayerMeta } from "../data/layers";

export function LayerIntro({ layer }: { layer: LayerMeta }) {
  return (
    <section className="layer-intro content-panel">
      <div className="layer-intro-head">
        <h2>
          {layer.zh} · {layer.en}
        </h2>
        <p className="layer-summary">{layer.summary}</p>
      </div>
      <dl className="layer-meta-grid">
        <div>
          <dt>角色</dt>
          <dd>{layer.role}</dd>
        </div>
        <div>
          <dt>資料來源</dt>
          <dd>
            <code>{layer.sources}</code>
          </dd>
        </div>
        <div>
          <dt>網站 v1</dt>
          <dd>{layer.webV1}</dd>
        </div>
      </dl>
    </section>
  );
}
