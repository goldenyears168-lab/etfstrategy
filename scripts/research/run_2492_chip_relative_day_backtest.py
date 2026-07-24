#!/usr/bin/env python3
"""華新科(2492) relative-chip day backtest + rule learning.

Research only. Writes knowledge under reports/research/chip-overlays/2492_knowledge/.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chip_relative.panel import (  # noqa: E402
    build_relative_chip_panel,
    load_market_ft_totals,
)
from research.chip_relative.rules import (  # noqa: E402
    daily_signal_table,
    learn_rules,
)

SID = "2492"
WARMUP = "2025-04-01"
START = "2025-07-24"
OOS_CUT = "2026-01-01"
END = "2026-07-22"  # last day with chip in local DB typically
OUT = ROOT / "reports/research/chip-overlays/2492_knowledge"
CACHE = OUT / "cache_market_ft_totals.csv"


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def write_learned_md(scores: list, path: Path) -> None:
    kept = [s for s in scores if s.kept]
    lines = [
        "# 2492 LEARNED_RULES · OOS-gated",
        "",
        f"- Built: {datetime.now().isoformat(timespec='seconds')}",
        f"- Window: {START} → {END} · OOS cut `{OOS_CUT}`",
        f"- Target: close→close `fwd1_r` / `fwd5_r`（籌碼 T 收盤後可知 → 預測未來路徑）",
        f"- Kept: **{len(kept)}** / {len(scores)}",
        "",
        "## Pass（可當作預測情境）",
        "",
    ]
    if not kept:
        lines.append("_None under current gates._")
    else:
        lines.append(
            "| rule | side | H | n_IS | hit_IS | n_OOS | hit_OOS | lift | med_OOS | note |"
        )
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---|")
        for s in sorted(kept, key=lambda x: (-(x.lift_oos or 0), x.rule_id)):
            lines.append(
                f"| `{s.rule_id}` | {s.side} | {s.horizon} | {s.n_is} | "
                f"{s.hit_is:.0%} | {s.n_oos} | {s.hit_oos:.0%} | "
                f"{s.lift_oos:+.0%} | {s.med_oos*100:+.2f}% | {s.description} |"
            )
    lines += [
        "",
        "## Reject（樣本內好看但 OOS 不過）",
        "",
        "| rule | side | H | hit_IS | hit_OOS | reason |",
        "|---|---|---|---:|---:|---|",
    ]
    for s in sorted(scores, key=lambda x: x.rule_id):
        if s.kept:
            continue
        hi = f"{s.hit_is:.0%}" if s.hit_is == s.hit_is else "nan"
        ho = f"{s.hit_oos:.0%}" if s.hit_oos == s.hit_oos else "nan"
        lines.append(
            f"| `{s.rule_id}` | {s.side} | {s.horizon} | {hi} | {ho} | `{s.reason}` |"
        )
    lines += [
        "",
        "## How to raise hit rate（原則）",
        "",
        "1. **寧缺勿濫**：只在規則觸發時出手（稀疏高精密度），不要每天都預測。",
        "2. **組合濾網**：單一條件（如同向買）通常≈基準；要疊「高占比／殘差z／價籌背離」。",
        "3. **空頭比多頭穩**：相對出貨、價漲籌賣 在 OOS 較常抬升空頭命中。",
        "4. **T+5 更難**：多數 T+1 規則拉到 5 日會回歸；只保留 OOS 仍過關者。",
        "5. **禁止用 net/net 占比**；分位必須 expanding／過去窗（本腳本已 PIT）。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_briefing(scores: list, daily: pd.DataFrame, path: Path) -> None:
    kept = [s for s in scores if s.kept]
    # Tier A: sparse high-lift — prefer n_oos<=16 and lift>=0.14, or hit>=0.68
    tier_a, tier_b = [], []
    for s in kept:
        sparse_hi = ((s.n_oos <= 16 and (s.lift_oos or 0) >= 0.14) or (s.hit_oos or 0) >= 0.68) and (
            s.n_oos <= 20
        )
        (tier_a if sparse_hi else tier_b).append(s)

    def fmt(xs: list) -> str:
        return ", ".join(f"`{s.rule_id}`" for s in xs) or "（無）"

    t1b_a = [s for s in tier_a if s.horizon == "t1" and s.side == "bear"]
    t1u_a = [s for s in tier_a if s.horizon == "t1" and s.side == "bull"]
    t5b_a = [s for s in tier_a if s.horizon == "t5" and s.side == "bear"]
    t5u_a = [s for s in tier_a if s.horizon == "t5" and s.side == "bull"]
    fired = daily[daily["n_fired"] > 0]
    # Sparse-only fire count
    a_ids = {s.rule_id for s in tier_a}
    sparse_days = 0
    for _, r in daily.iterrows():
        fr = set((r.get("fired_rules") or "").split("|")) - {""}
        if fr & a_ids:
            sparse_days += 1
    lines = [
        "# 華新科(2492) 相對籌碼知識庫 · BRIEFING",
        "",
        f"Updated: {datetime.now().date().isoformat()} · Research only · 未採納",
        "",
        "## 方法（v2）",
        "",
        "- 相對價：滾動 β 超額 + log RS + RS_mom10",
        "- 相對籌：FT淨額 ÷ 市場 FT **總買賣**（毛額）· 市場毛額地板 NT$500億",
        "- 同向／背離：`sign(個股FT)==sign(市場FT)`",
        "- 殘差：個股 FT 對市場 FT 淨額滾動回歸殘差 z",
        "- **禁用** 個股淨額÷市場淨額",
        "- 規則 id 含 `__ex`：預測標的是 **β 超額**方向，不是絕對漲跌",
        "- 絕對價預測較難：OOS 上 `scoop_resid_pos` 對 `fwd1_r` 約 70%；空頭規則多在 **相對弱** 上成立",
        "",
        "## Tier A · 稀疏高精（優先用於「預測」）",
        "",
        f"- **T+1 利空**: {fmt(t1b_a)}",
        f"- **T+1 利多**: {fmt(t1u_a)}",
        f"- **T+5 利空**: {fmt(t5b_a)}",
        f"- **T+5 利多**: {fmt(t5u_a)}",
        "",
        f"Tier A 觸發日約 **{sparse_days}** / {len(daily)}（其餘日子不強制預測）。",
        "",
        "## Tier B · 軟濾網（提高覆蓋、命中較接近基準）",
        "",
        f"{fmt(tier_b)}",
        "",
        f"全部 kept 觸發日 {len(fired)} / {len(daily)} — **勿**用 Tier B 每天喊多空。",
        "",
        "詳見 [LEARNED_RULES.md](LEARNED_RULES.md) · 逐日：`daily_signals.csv` · skill：`.cursor/skills/chip-relative-vs-market/`",
        "",
        "## 操作口訣",
        "",
        "1. **相對出貨**（市場買、它賣）且相對動能弱 → T+1 **β超額**偏空（勿追）。",
        "2. **相對吸籌**且殘差 z≥0.5 → 少數日子的 T+1／T+5 利多（樣本薄，權重謹慎）。",
        "3. 同向買 alone 不夠；要高占比且避開連續雙強高潮。",
        "4. 要提高命中：只做 Tier A，接受低交易頻率。",
        "",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    log("load market FT totals (cache or FinMind)")
    mkt = load_market_ft_totals(
        start=date.fromisoformat(WARMUP),
        end=date.fromisoformat(END),
        cache_path=CACHE,
        refresh=False,
    )
    log(f"market rows={len(mkt)}")

    db = ROOT / "data" / "stocks.db"
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    try:
        log(f"build panel sid={SID}")
        panel = build_relative_chip_panel(
            conn,
            sid=SID,
            start=START,
            end=END,
            warmup=WARMUP,
            market_totals=mkt,
        )
    finally:
        conn.close()

    panel_path = OUT / "panel_daily.csv"
    cols = [
        "date",
        "close",
        "ix",
        "r",
        "rm",
        "beta",
        "excess_beta",
        "RS100",
        "RS_mom10",
        "FT_ntd",
        "net_FT",
        "gross_FT",
        "FT_part_bp",
        "FT_part_ok",
        "FT_part_pct",
        "align_FT",
        "FT_z",
        "FT_resid_z",
        "rel_dump",
        "aligned_buy",
        "bull_div",
        "bear_div",
        "fwd1_r",
        "fwd1_oc",
        "fwd5_r",
        "fwd5_ex",
    ]
    show = panel[panel["date"] >= pd.Timestamp(START)]
    show[cols].to_csv(panel_path, index=False)
    log(f"wrote {panel_path} n={len(show)}")

    log("learn rules (strict → maybe relax)")
    strict, final = learn_rules(panel, start=START, oos_cut=OOS_CUT, end=END)
    used = final
    n_pass = sum(1 for s in used if s.kept)
    log(f"pass={n_pass} (strict_pass={sum(1 for s in strict if s.kept)})")

    scores_path = OUT / "rule_scores.json"
    scores_path.write_text(
        json.dumps([s.to_dict() for s in used], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_learned_md(used, OUT / "LEARNED_RULES.md")

    kept = [s for s in used if s.kept]
    daily = daily_signal_table(panel, kept, start=START, end=END)
    daily.to_csv(OUT / "daily_signals.csv", index=False)
    write_briefing(used, daily, OUT / "BRIEFING.md")

    # Quick console summary
    print("\n======== KEPT RULES ========")
    if not kept:
        print("(none)")
    for s in kept:
        print(
            f"{s.rule_id:28s} {s.side:4s} {s.horizon}  "
            f"OOS hit={s.hit_oos:.0%} lift={s.lift_oos:+.0%} "
            f"n={s.n_oos} med={s.med_oos*100:+.2f}%  | {s.description}"
        )

    # Baseline
    oos = show[show["date"] >= pd.Timestamp(OOS_CUT)]
    print("\n======== BASELINES (OOS) ========")
    print(f"P(fwd1_r>0)={ (oos['fwd1_r']>0).mean():.1%}  P(fwd5_r>0)={(oos['fwd5_r']>0).mean():.1%}")
    print(f"artifacts → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
