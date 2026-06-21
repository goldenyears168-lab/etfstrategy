import { Link } from "react-router-dom";

interface Props {
  date: string;
  dates: string[];
  onDateChange: (d: string) => void;
  showDatePicker?: boolean;
}

export function AppShell({
  date,
  dates,
  onDateChange,
  showDatePicker = true,
  children,
}: Props & { children: React.ReactNode }) {
  return (
    <div className="shell">
      <header className="topbar">
        <h1>
          <Link to="/">ETF 股市研究</Link>
        </h1>
        <nav>
          <Link to="/">專案</Link>
          {date && <Link to={`/layers/facts/${date}`}>日報</Link>}
          <Link to="/layers/website">Supabase</Link>
        </nav>
        {showDatePicker && dates.length > 0 && (
          <select
            className="date-select"
            value={date}
            onChange={(e) => onDateChange(e.target.value)}
            aria-label="Trade date"
          >
            {dates.map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        )}
      </header>
      {children}
    </div>
  );
}
