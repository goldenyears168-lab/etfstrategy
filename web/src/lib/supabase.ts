import { createClient } from "@supabase/supabase-js";
import type { BriefType, DailyBrief } from "../types";

const url = import.meta.env.VITE_PUBLIC_SUPABASE_URL;
const key = import.meta.env.VITE_PUBLIC_SUPABASE_ANON_KEY;

if (!url || !key) {
  console.warn("Missing VITE_PUBLIC_SUPABASE_URL or VITE_PUBLIC_SUPABASE_ANON_KEY");
}

export const supabase = createClient(url ?? "", key ?? "", {
  db: { schema: "stock_research" },
});

export async function fetchTradeDates(): Promise<string[]> {
  const { data, error } = await supabase
    .from("daily_briefs")
    .select("trade_date")
    .order("trade_date", { ascending: false });
  if (error) throw error;
  const set = new Set((data ?? []).map((r) => r.trade_date as string));
  return [...set];
}

export async function fetchBriefsForDate(date: string): Promise<DailyBrief[]> {
  const { data, error } = await supabase
    .from("daily_briefs")
    .select(
      "id, trade_date, schedule_slot, brief_type, title, content_md, content_html, source_path, synced_at",
    )
    .eq("trade_date", date)
    .order("schedule_slot");
  if (error) throw error;
  return (data ?? []) as DailyBrief[];
}

export async function fetchBrief(
  date: string,
  briefType: BriefType,
  columns?: string,
): Promise<DailyBrief | null> {
  const select =
    columns ??
    "id, trade_date, schedule_slot, brief_type, title, content_md, content_html, source_path, synced_at";
  const { data, error } = await supabase
    .from("daily_briefs")
    .select(select)
    .eq("trade_date", date)
    .eq("brief_type", briefType)
    .maybeSingle();
  if (error) throw error;
  return (data as unknown as DailyBrief) ?? null;
}

export async function fetchRegimeTeaserRow(date: string): Promise<DailyBrief | null> {
  return fetchBrief(date, "regime_daily", "trade_date, content_md, synced_at, title");
}
