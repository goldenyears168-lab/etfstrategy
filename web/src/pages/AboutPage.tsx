export function AboutPage() {
  return (
    <div className="about content-panel">
      <h2>六層架構</h2>
      <ul>
        <li>
          <strong>事實層 Facts</strong> — ETF 持股、行情 ingest；網站讀 <code>etf_daily</code>
        </li>
        <li>
          <strong>環境層 Regime</strong> — 四軸體制診斷（非 alpha）；網站讀{" "}
          <code>regime_daily</code> embed HTML
        </li>
        <li>
          <strong>研究層 Research</strong> — 探索性 topic / sweep；網站讀{" "}
          <code>vcp_funnel_specs</code>
        </li>
        <li>
          <strong>策略層 Strategy</strong> — 已採納 frozen spec；v1 尚未上線
        </li>
        <li>
          <strong>執行層 Execution</strong> — 下單 intent / 券商；不在此網站
        </li>
        <li>
          <strong>網站層 Website</strong> — 唯讀展示已發布 VFP；不碰 SQLite
        </li>
      </ul>
      <p style={{ color: "var(--muted)", fontSize: "0.88rem" }}>
        資料來源：Supabase <code>stock_research.daily_briefs</code>，由本地{" "}
        <code>reports/daily/**</code> 同步。
      </p>
    </div>
  );
}
