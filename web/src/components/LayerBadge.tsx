type Layer = "facts" | "regime" | "research" | "strategy";

const LABELS: Record<Layer, string> = {
  facts: "事實層 Facts",
  regime: "環境層 Regime",
  research: "研究層 Research",
  strategy: "策略層 Strategy",
};

export function LayerBadge({ layer }: { layer: Layer }) {
  return <span className={`layer-badge ${layer}`}>{LABELS[layer]}</span>;
}
