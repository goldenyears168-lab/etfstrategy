import { Link } from "react-router-dom";
import { LayerBadge } from "../components/LayerBadge";
import type { DailyBrief } from "../types";
import { parseEtfTeaser, parseRegimeTeaser, countVcpVariants } from "../lib/parseTeaser";

interface Props {
  date: string;
  briefs: DailyBrief[];
}

export function HubPage({ date, briefs }: Props) {
  const regime = briefs.find((b) => b.brief_type === "regime_daily");
  const etf = briefs.find((b) => b.brief_type === "etf_daily");
  const vcp = briefs.find((b) => b.brief_type === "vcp_funnel_specs");

  const regimeTeaser = regime ? parseRegimeTeaser(regime.content_md) : null;
  const etfTeaser = etf ? parseEtfTeaser(etf.content_md) : null;
  const vcpCount = vcp ? countVcpVariants(vcp.content_md) : 0;

  return (
    <>
      <div className="kpi-grid">
        <div className="kpi">
          <p className="kpi-label">Breadth zone · 200MA</p>
          <p className="kpi-value">{regimeTeaser?.breadth200 ?? "—"}</p>
          <p className="kpi-sub">{regimeTeaser?.breadthLabel ?? "無 regime 資料"}</p>
        </div>
        <div className="kpi">
          <p className="kpi-label">Stage-2 participation</p>
          <p className="kpi-value">{regimeTeaser?.stage ?? "—"}</p>
          <p className="kpi-sub">Pass {regimeTeaser?.passRate ?? "—"}</p>
        </div>
        <div className="kpi">
          <p className="kpi-label">RRG Leading+Improving</p>
          <p className="kpi-value">{regimeTeaser?.rrgPct ?? "—"}</p>
          <p className="kpi-sub">環境層 Regime</p>
        </div>
        <div className="kpi">
          <p className="kpi-label">ETF 持股同步</p>
          <p className="kpi-value">{etfTeaser?.sync ?? "—"}</p>
          <p className="kpi-sub">
            {etfTeaser?.changed.length
              ? `變化 ${etfTeaser.changed.join(", ")}`
              : "事實層 Facts"}
          </p>
        </div>
      </div>

      {regimeTeaser?.synopsis && (
        <p style={{ color: "var(--muted)", fontSize: "0.92rem", marginBottom: 20 }}>
          {regimeTeaser.synopsis}
        </p>
      )}

      <div className="card-grid">
        <article className="card">
          <LayerBadge layer="regime" />
          <h3>市場結構日報</h3>
          <p>四軸體制診斷：Breadth · Trend · RRG · Stage-2</p>
          <Link className="btn" to={`/briefs/${date}/regime`}>
            閱讀
          </Link>
        </article>
        <article className="card">
          <LayerBadge layer="facts" />
          <h3>ETF 持股日報</h3>
          <p>六檔主動 ETF 持股與成分變化</p>
          <Link className="btn" to={`/briefs/${date}/etf`}>
            閱讀
          </Link>
        </article>
        <article className="card">
          <LayerBadge layer="research" />
          <h3>VCP 漏斗研究</h3>
          <p>{vcpCount > 0 ? `${vcpCount} 組規格摘要` : "漏斗篩選研究 brief"}</p>
          <Link className="btn" to={`/briefs/${date}/vcp`}>
            閱讀
          </Link>
        </article>
      </div>
    </>
  );
}
