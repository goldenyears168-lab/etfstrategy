import { Link } from "react-router-dom";
import type { LayerId } from "../data/layers";
import { LAYERS } from "../data/layers";

interface Props {
  active?: LayerId;
  date?: string;
}

export function LayerTabs({ active, date }: Props) {
  return (
    <div className="tabs layer-tabs">
      {LAYERS.map((l) => {
        const to = date && l.hasDaily ? `/layers/${l.id}/${date}` : `/layers/${l.id}`;
        return (
          <Link
            key={l.id}
            to={to}
            className={active === l.id ? "tab active" : "tab"}
          >
            {l.zh}
            <span className="tab-en">{l.en}</span>
          </Link>
        );
      })}
    </div>
  );
}
