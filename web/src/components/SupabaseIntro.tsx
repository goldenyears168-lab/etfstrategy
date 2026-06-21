import {
  SUPABASE_BRIEF_TYPES,
  SUPABASE_PROJECT,
  SYNC_PIPELINE,
} from "../data/supabaseMeta";

export function SupabaseIntro() {
  return (
    <section className="supabase-intro">
      <div className="content-panel">
        <h3>Supabase 專案</h3>
        <dl className="layer-meta-grid">
          <div>
            <dt>專案名稱</dt>
            <dd>{SUPABASE_PROJECT.name}</dd>
          </div>
          <div>
            <dt>Project ref</dt>
            <dd>
              <code>{SUPABASE_PROJECT.ref}</code>
            </dd>
          </div>
          <div>
            <dt>API URL</dt>
            <dd>
              <code>{SUPABASE_PROJECT.url}</code>
            </dd>
          </div>
          <div>
            <dt>Schema · Table</dt>
            <dd>
              <code>
                {SUPABASE_PROJECT.schema}.{SUPABASE_PROJECT.table}
              </code>
            </dd>
          </div>
          <div>
            <dt>RLS</dt>
            <dd>
              <code>{SUPABASE_PROJECT.rlsPolicy}</code> — anon / authenticated SELECT only
            </dd>
          </div>
          <div>
            <dt>Dashboard</dt>
            <dd>
              <a href={SUPABASE_PROJECT.dashboardUrl} target="_blank" rel="noreferrer">
                開啟 Supabase Editor
              </a>
            </dd>
          </div>
        </dl>
      </div>

      <div className="content-panel">
        <h3>與官網預約分離</h3>
        <p className="muted-note">
          同一 Supabase 專案、不同 schema：<code>public.*</code> 為官網預約；{" "}
          <code>stock_research.*</code> 為股市研究 daily brief。
        </p>
      </div>

      <div className="content-panel">
        <h3>同步 brief 類型</h3>
        <table className="layer-table">
          <thead>
            <tr>
              <th>排程</th>
              <th>brief_type</th>
              <th>產品層</th>
            </tr>
          </thead>
          <tbody>
            {SUPABASE_BRIEF_TYPES.map((row) => (
              <tr key={`${row.slot}-${row.type}`}>
                <td>{row.slot}</td>
                <td>
                  <code>{row.type}</code>
                </td>
                <td>{row.layer}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="content-panel">
        <h3>同步管線</h3>
        <ol className="pipeline-list">
          {SYNC_PIPELINE.map((step) => (
            <li key={step}>{step}</li>
          ))}
        </ol>
        <p className="muted-note">
          查詢範例：
          <code>
            {" "}
            select trade_date, brief_type, title from stock_research.daily_briefs order by
            trade_date desc;
          </code>
        </p>
      </div>
    </section>
  );
}
