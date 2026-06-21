import type { BriefType, DailyBrief } from "../types";
import {
  fetchBriefsForDate as fetchSupabaseBriefs,
  fetchTradeDates as fetchSupabaseDates,
} from "./supabase";

const useLocal =
  import.meta.env.VITE_USE_LOCAL_BRIEFS === "1" ||
  (import.meta.env.VITE_USE_LOCAL_BRIEFS !== "0" &&
    !import.meta.env.VITE_PUBLIC_SUPABASE_ANON_KEY);

async function fetchLocal<T>(path: string): Promise<T> {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`Local briefs API ${res.status}: ${path}`);
  return res.json() as Promise<T>;
}

export function isLocalBriefsMode(): boolean {
  return useLocal;
}

export async function fetchTradeDates(): Promise<string[]> {
  if (useLocal) return fetchLocal<string[]>("/api/briefs/dates");
  return fetchSupabaseDates();
}

export async function fetchBriefsForDate(date: string): Promise<DailyBrief[]> {
  if (useLocal) return fetchLocal<DailyBrief[]>(`/api/briefs/${date}`);
  return fetchSupabaseBriefs(date);
}

export async function fetchBrief(
  date: string,
  briefType: BriefType,
): Promise<DailyBrief | null> {
  const briefs = await fetchBriefsForDate(date);
  return briefs.find((b) => b.brief_type === briefType) ?? null;
}
