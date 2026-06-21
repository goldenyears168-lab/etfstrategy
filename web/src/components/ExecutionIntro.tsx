export function ExecutionIntro() {
  return (
    <section className="content-panel execution-intro">
      <h3>執行層 · 不在網站</h3>
      <p>
        執行層（Execution）處理下單 intent、富邦 Neo API session、帳戶與委託。程式位於{" "}
        <code>src/execution/</code>，僅本機運行。
      </p>
      <ul>
        <li>
          <code>src/execution/intent.py</code> — order intent 結構
        </li>
        <li>
          <code>src/execution/fubon_session.py</code> — 券商 session
        </li>
        <li>
          <code>scripts/execution/submit_intents.py</code> — 提交委託
        </li>
      </ul>
      <p className="muted-note">
        安全考量：憑證、帳戶、即時下單不進公開網站層。策略層產出 signal / screen 後，由本機執行層接手。
      </p>
    </section>
  );
}
