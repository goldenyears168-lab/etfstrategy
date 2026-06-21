import { STRATEGIES, STRATEGY_PRINCIPLES } from "../data/strategies";

export function StrategyCatalog() {
  return (
    <section className="strategy-catalog">
      <div className="content-panel">
        <h3>採納原則</h3>
        <ul className="principle-list">
          {STRATEGY_PRINCIPLES.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
        <p className="muted-note">
          SSOT：<code>config/strategy.yaml</code> · 回測 JSON 為證據 · 全部{" "}
          <code>enabled: false</code>（手動 / launchd 啟用）
        </p>
      </div>

      <div className="card-grid">
        {STRATEGIES.map((s) => (
          <article key={s.id} className="card strategy-card">
            <div className="strategy-card-head">
              <h3>{s.title}</h3>
              <span className={`chip ${s.enabled ? "up" : "down"}`}>
                {s.enabled ? "enabled" : "frozen"}
              </span>
            </div>
            <p>{s.description}</p>
            <dl className="strategy-specs">
              <div>
                <dt>schedule</dt>
                <dd>{s.schedule}</dd>
              </div>
              {s.nSlots != null && (
                <div>
                  <dt>slots · hold</dt>
                  <dd>
                    {s.nSlots} · {s.holdDays}d
                  </dd>
                </div>
              )}
              {s.etfCode && (
                <div>
                  <dt>ETF</dt>
                  <dd>{s.etfCode}</dd>
                </div>
              )}
            </dl>
            {s.sourceSummary && (
              <p className="footer-meta">
                backtest: <code>{s.sourceSummary}</code>
              </p>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}
