import { Link } from "react-router-dom";
import { LAYERS } from "../data/layers";

export function LayerTable() {
  return (
    <div className="layer-table-wrap content-panel">
      <table className="layer-table">
        <thead>
          <tr>
            <th>層</th>
            <th>角色</th>
            <th>資料來源</th>
            <th>網站 v1</th>
          </tr>
        </thead>
        <tbody>
          {LAYERS.map((l) => (
            <tr key={l.id}>
              <td>
                <Link to={`/layers/${l.id}`} className="layer-link">
                  {l.zh}
                  <span className="layer-link-en">{l.en}</span>
                </Link>
              </td>
              <td>{l.role}</td>
              <td>
                <code>{l.sources}</code>
              </td>
              <td>{l.webV1}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
