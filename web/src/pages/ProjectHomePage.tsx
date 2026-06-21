import { Link } from "react-router-dom";
import { LayerTable } from "../components/LayerTable";
import type { DailyBrief } from "../types";
import { parseEtfTeaser, parseRegimeTeaser } from "../lib/parseTeaser";

interface Props {
  latestDate: string | null;
  briefs: DailyBrief[];
}

export function ProjectHomePage({ latestDate, briefs }: Props) {
  const regime = briefs.find((b) => b.brief_type === "regime_daily");
  const etf = briefs.find((b) => b.brief_type === "etf_daily");
  const regimeTeaser = regime ? parseRegimeTeaser(regime.content_md) : null;
  const etfTeaser = etf ? parseEtfTeaser(etf.content_md) : null;

  return (
    <>
      <section className="hero content-panel">
        <h2>ETF 股市研究 · 六層架構</h2>
        <p>
          本專案以 <strong>facts → regime → research → strategy → execution → website</strong>{" "}
          分層：下層產出 VFP（檔案、DB 狀態），上層消費已發布結果。網站層唯讀展示，不碰 SQLite。
        </p>
        {latestDate && (
          <p className="hero-cta">
            最新訊號日{" "}
            <Link to={`/layers/facts/${latestDate}`}>{latestDate}</Link>
            {" · "}
            <Link to={`/layers/regime/${latestDate}`}>環境層</Link>
            {" · "}
            <Link to={`/layers/research/${latestDate}`}>研究層</Link>
          </p>
        )}
      </section>

      {latestDate && regimeTeaser && (
        <div className="kpi-grid">
          <div className="kpi">
            <p className="kpi-label">Breadth · 200MA</p>
            <p className="kpi-value">{regimeTeaser.breadth200 ?? "—"}</p>
          </div>
          <div className="kpi">
            <p className="kpi-label">Stage-2</p>
            <p className="kpi-value">{regimeTeaser.stage ?? "—"}</p>
          </div>
          <div className="kpi">
            <p className="kpi-label">RRG L+I</p>
            <p className="kpi-value">{regimeTeaser.rrgPct ?? "—"}</p>
          </div>
          <div className="kpi">
            <p className="kpi-label">ETF 同步</p>
            <p className="kpi-value">{etfTeaser?.sync ?? "—"}</p>
          </div>
        </div>
      )}

      <h3 className="section-title">六層對照表</h3>
      <LayerTable />

      <p className="footer-meta">
        術語 SSOT：<code>docs/terminology.md</code> · 規格{" "}
        <code>docs/readdy-stock-intelligence-spec.md</code>
      </p>
    </>
  );
}
