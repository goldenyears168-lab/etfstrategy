import fs from "node:fs";
import path from "node:path";
import type { Plugin } from "vite";

const PUBLISH_ROOT = path.resolve(__dirname, "..", "reports", "publish");

type BriefType = "etf_daily" | "regime_daily" | "vcp_funnel_specs";

interface DailyBrief {
  id: string;
  trade_date: string;
  schedule_slot: "1300" | "1630";
  brief_type: BriefType;
  title: string;
  content_md: string;
  content_html: string | null;
  source_path: string | null;
  synced_at: string;
}

function parseStamp(name: string): string | null {
  if (!/^\d{8}$/.test(name)) return null;
  return `${name.slice(0, 4)}-${name.slice(4, 6)}-${name.slice(6, 8)}`;
}

function extractTitle(md: string, briefType: BriefType): string {
  const line = md.split("\n").find((l) => l.startsWith("# "));
  if (line) return line.slice(2).trim();
  const defaults: Record<BriefType, string> = {
    etf_daily: "ETF 持股日報",
    regime_daily: "市場結構日報",
    vcp_funnel_specs: "VCP 漏斗研究",
  };
  return defaults[briefType];
}

function extractDate(md: string, fallback: string): string {
  const m = md.slice(0, 500).match(/(\d{4}-\d{2}-\d{2})/);
  return m?.[1] ?? fallback;
}

function statIso(filePath: string): string {
  return new Date(fs.statSync(filePath).mtimeMs).toISOString();
}

function discoverDates(): string[] {
  const found = new Set<string>();

  const regimeSnaps = path.join(PUBLISH_ROOT, "regime", "snapshots");
  if (fs.existsSync(regimeSnaps)) {
    for (const name of fs.readdirSync(regimeSnaps)) {
      const iso = parseStamp(name);
      if (iso && fs.existsSync(path.join(regimeSnaps, name, "daily_brief.md"))) {
        found.add(iso);
      }
    }
  }

  const vcpDir = path.join(PUBLISH_ROOT, "research", "vcp_funnel_specs");
  if (fs.existsSync(vcpDir)) {
    for (const name of fs.readdirSync(vcpDir)) {
      if (!name.endsWith(".md")) continue;
      const iso = parseStamp(name.replace(/\.md$/, ""));
      if (iso) found.add(iso);
    }
  }

  const etfDir = path.join(PUBLISH_ROOT, "facts", "etf-daily");
  if (fs.existsSync(etfDir)) {
    for (const name of fs.readdirSync(etfDir)) {
      if (name === "daily_brief.md") continue;
      if (!name.endsWith(".md")) continue;
      const iso = parseStamp(name.replace(/\.md$/, ""));
      if (iso) found.add(iso);
    }
  }

  return [...found].sort().reverse();
}

function loadBrief(tradeDate: string, briefType: BriefType): DailyBrief | null {
  const stamp = tradeDate.replace(/-/g, "");
  let mdPath: string | null = null;
  let htmlPath: string | null = null;
  let slot: "1300" | "1630" = "1630";

  if (briefType === "etf_daily") {
    const dated = path.join(PUBLISH_ROOT, "facts", "etf-daily", `${stamp}.md`);
    const latest = path.join(PUBLISH_ROOT, "facts", "etf-daily", "daily_brief.md");
    mdPath = fs.existsSync(dated) ? dated : fs.existsSync(latest) ? latest : null;
  } else if (briefType === "regime_daily") {
    const snap = path.join(PUBLISH_ROOT, "regime", "snapshots", stamp, "daily_brief.md");
    const latest = path.join(PUBLISH_ROOT, "regime", "daily_brief.md");
    mdPath = fs.existsSync(snap) ? snap : fs.existsSync(latest) ? latest : null;
    const embedSnap = path.join(PUBLISH_ROOT, "regime", "snapshots", stamp, "daily_brief.embed.html");
    const embedLatest = path.join(PUBLISH_ROOT, "regime", "daily_brief.embed.html");
    htmlPath = fs.existsSync(embedSnap) ? embedSnap : fs.existsSync(embedLatest) ? embedLatest : null;
  } else if (briefType === "vcp_funnel_specs") {
    mdPath = path.join(PUBLISH_ROOT, "research", "vcp_funnel_specs", `${stamp}.md`);
    slot = "1300";
    if (!fs.existsSync(mdPath)) mdPath = null;
  }

  if (!mdPath || !fs.existsSync(mdPath)) return null;

  const content_md = fs.readFileSync(mdPath, "utf-8");
  const day = extractDate(content_md, tradeDate);
  if (day !== tradeDate) return null;

  const content_html =
    htmlPath && fs.existsSync(htmlPath) ? fs.readFileSync(htmlPath, "utf-8") : null;

  return {
    id: `${day}-${briefType}`,
    trade_date: day,
    schedule_slot: slot,
    brief_type: briefType,
    title: extractTitle(content_md, briefType),
    content_md,
    content_html,
    source_path: path.relative(path.resolve(__dirname, ".."), mdPath),
    synced_at: statIso(mdPath),
  };
}

function loadBriefsForDate(tradeDate: string): DailyBrief[] {
  const types: BriefType[] = ["etf_daily", "regime_daily", "vcp_funnel_specs"];
  return types
    .map((t) => loadBrief(tradeDate, t))
    .filter((b): b is DailyBrief => b !== null)
    .sort((a, b) => a.schedule_slot.localeCompare(b.schedule_slot));
}

function sendJson(res: import("http").ServerResponse, data: unknown, status = 200) {
  res.statusCode = status;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(JSON.stringify(data));
}

/** Vite dev middleware: read reports/publish/ (website layer VFP). */
export function localBriefsPlugin(): Plugin {
  return {
    name: "local-briefs",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if (!req.url?.startsWith("/api/briefs")) return next();

        try {
          if (req.url === "/api/briefs/dates" || req.url.startsWith("/api/briefs/dates?")) {
            sendJson(res, discoverDates());
            return;
          }

          const m = req.url.match(/^\/api\/briefs\/(\d{4}-\d{2}-\d{2})\/?$/);
          if (m) {
            sendJson(res, loadBriefsForDate(m[1]));
            return;
          }
        } catch (err) {
          sendJson(res, { error: String(err) }, 500);
          return;
        }

        next();
      });
    },
  };
}
