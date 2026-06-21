import { useEffect, useState } from "react";
import { Navigate, Route, Routes, useNavigate, useParams } from "react-router-dom";
import { AppShell } from "./components/AppShell";
import { PitBanner } from "./components/PitBanner";
import { LayerTabs } from "./components/LayerTabs";
import { ProjectHomePage } from "./pages/ProjectHomePage";
import { LayerPage, briefForLayer } from "./pages/LayerPage";
import { fetchBriefsForDate, fetchTradeDates } from "./lib/briefs";
import type { LayerId } from "./data/layers";
import { layerById } from "./data/layers";
import type { DailyBrief } from "./types";

const LAYER_IDS: LayerId[] = [
  "facts",
  "regime",
  "research",
  "strategy",
  "execution",
  "website",
];

function useTradeDates() {
  const [dates, setDates] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchTradeDates()
      .then(setDates)
      .catch((e) => setError(String(e)));
  }, []);

  return { dates, error };
}

function useBriefs(date: string) {
  const [briefs, setBriefs] = useState<DailyBrief[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!date) {
      setBriefs([]);
      return;
    }
    setLoading(true);
    setError(null);
    fetchBriefsForDate(date)
      .then(setBriefs)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [date]);

  const latestSync = briefs.reduce<string | null>((acc, b) => {
    if (!acc || b.synced_at > acc) return b.synced_at;
    return acc;
  }, null);

  return { briefs, loading, error, latestSync };
}

function HomeRoute() {
  const { dates, error: datesError } = useTradeDates();
  const latestDate = dates[0] ?? null;
  const { briefs, error: briefsError } = useBriefs(latestDate ?? "");

  return (
    <AppShell date={latestDate ?? ""} dates={dates} onDateChange={() => {}} showDatePicker={false}>
      {(datesError || briefsError) && (
        <div className="error">{datesError ?? briefsError}</div>
      )}
      <LayerTabs date={latestDate ?? undefined} />
      <ProjectHomePage latestDate={latestDate} briefs={briefs} />
    </AppShell>
  );
}

function LayerRoute() {
  const { layerId, date } = useParams<{ layerId: string; date?: string }>();
  const navigate = useNavigate();
  const id = layerId as LayerId;

  if (!LAYER_IDS.includes(id)) {
    return <Navigate to="/" replace />;
  }

  const layer = layerById(id)!;
  const { dates, error: datesError } = useTradeDates();
  const effectiveDate = layer.hasDaily ? (date ?? dates[0] ?? "") : "";
  const { briefs, loading, error: briefsError, latestSync } = useBriefs(
    layer.hasDaily ? effectiveDate : "",
  );

  useEffect(() => {
    if (layer.hasDaily && !date && dates.length > 0) {
      navigate(`/layers/${id}/${dates[0]}`, { replace: true });
    }
  }, [layer.hasDaily, date, dates, id, navigate]);

  const onDateChange = (d: string) => {
    navigate(`/layers/${id}/${d}`);
  };

  const brief = layer.hasDaily ? briefForLayer(id, briefs) : null;
  const err = datesError ?? briefsError;

  return (
    <AppShell date={effectiveDate || dates[0] || ""} dates={dates} onDateChange={onDateChange}>
      <LayerTabs active={id} date={effectiveDate || dates[0]} />
      {layer.hasDaily && effectiveDate && (
        <PitBanner date={effectiveDate} syncedAt={latestSync} />
      )}
      {err && <div className="error">{err}</div>}
      {loading && layer.hasDaily && effectiveDate && (
        <div className="loading">載入中…</div>
      )}
      {(!loading || !layer.hasDaily) && (
        <LayerPage layerId={id} date={effectiveDate || undefined} brief={brief} />
      )}
    </AppShell>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<HomeRoute />} />
      <Route path="/layers/:layerId" element={<LayerRoute />} />
      <Route path="/layers/:layerId/:date" element={<LayerRoute />} />
      <Route path="/briefs" element={<Navigate to="/layers/facts" replace />} />
      <Route path="/briefs/:date" element={<LegacyBriefRedirect />} />
      <Route path="/briefs/:date/:tab" element={<LegacyBriefRedirect />} />
      <Route path="/about" element={<Navigate to="/layers/website" replace />} />
    </Routes>
  );
}

function LegacyBriefRedirect() {
  const { date, tab } = useParams<{ date: string; tab?: string }>();
  const map: Record<string, LayerId> = {
    etf: "facts",
    regime: "regime",
    vcp: "research",
    strategy: "strategy",
  };
  const layer = map[tab ?? ""] ?? "facts";
  if (layer === "strategy" || !date) {
    return <Navigate to={`/layers/${layer}`} replace />;
  }
  return <Navigate to={`/layers/${layer}/${date}`} replace />;
}
