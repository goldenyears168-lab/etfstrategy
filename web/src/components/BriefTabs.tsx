import { NavLink } from "react-router-dom";
import type { BriefTab } from "../types";

const TABS: { id: BriefTab; label: string; layer: string }[] = [
  { id: "regime", label: "市場結構", layer: "環境層" },
  { id: "etf", label: "ETF 持股", layer: "事實層" },
  { id: "vcp", label: "VCP 漏斗", layer: "研究層" },
];

interface Props {
  date: string;
}

export function BriefTabs({ date }: Props) {
  return (
    <div className="tabs">
      {TABS.map((t) => (
        <NavLink
          key={t.id}
          to={`/briefs/${date}/${t.id}`}
          className={({ isActive }) => (isActive ? "tab active" : "tab")}
        >
          {t.label}
          <span style={{ opacity: 0.6, marginLeft: 6, fontSize: "0.75rem" }}>{t.layer}</span>
        </NavLink>
      ))}
    </div>
  );
}
