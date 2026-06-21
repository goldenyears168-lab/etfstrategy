import type { RegimeTeaser } from "../types";

export function parseRegimeTeaser(md: string): RegimeTeaser {
  const breadthMatch = md.match(/% above 200-day MA\s+([\d.]+)%/i);
  const labelMatch = md.match(
    /(Oversold|Overbought|Neutral|Recovery|Low|Mid|High|過熱|過冷|中性)[^·\n]*/i,
  );
  const stageMatch = md.match(/Weinstein Stage\s+(\d)/i);
  const rrgMatch = md.match(/Leading \+ Improving[:\s]+([\d.]+)%/i);
  const passMatch = md.match(/(?:Pass rate|template pass rate)\s+([\d.]+)%/i);

  let synopsis: string | null = null;
  const synSection = md.match(/## Daily synopsis[^#]*\n+([\s\S]*?)(?=\n---|\n## )/);
  if (synSection) {
    synopsis = synSection[1]
      .replace(/^>\s*/gm, "")
      .replace(/\*\*/g, "")
      .trim()
      .split("\n")[0]
      ?.slice(0, 200) ?? null;
  }

  return {
    breadth200: breadthMatch?.[1] ? `${breadthMatch[1]}%` : null,
    breadthLabel: labelMatch?.[0]?.trim() ?? null,
    stage: stageMatch ? `Stage ${stageMatch[1]}` : null,
    rrgPct: rrgMatch?.[1] ? `${rrgMatch[1]}%` : null,
    passRate: passMatch?.[1] ? `${passMatch[1]}%` : null,
    synopsis,
  };
}

export function parseEtfTeaser(md: string): { sync: string | null; changed: string[] } {
  const sync = md.match(/持股同步[^*]*\*\*(\d+\/\d+)\*\*/)?.[1] ?? null;
  const changedBlock = md.match(/今日有成分變化[：:]\s*(.+)/);
  const changed = changedBlock
    ? changedBlock[1].split(/[,，]/).map((s) => s.trim()).filter(Boolean)
    : [];
  return { sync, changed };
}

export function countVcpVariants(md: string): number {
  return (md.match(/^# VCP /gm) ?? []).length || 1;
}
