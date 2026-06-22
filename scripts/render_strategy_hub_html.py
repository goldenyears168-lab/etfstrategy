#!/usr/bin/env python3
"""Phase 1 strategy hub — lightweight tab shell for research HTML + daily tracks."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from report_paths import (  # noqa: E402
    RESEARCH_BREADTH,
    RESEARCH_COPYTRADE_00981A,
    RESEARCH_RRG,
    RESEARCH_STRATEGY_HUB,
    RESEARCH_VCP,
    REPORTS_DAILY,
    REPORTS_RESEARCH,
    REPORTS_ROOT,
    daily_track_dir,
    ensure_research_dir,
    latest_research_html,
)
from strategy_config import load_strategy_config  # noqa: E402
from stock_db import PROJECT_ROOT  # noqa: E402

CONFIG = PROJECT_ROOT / "config"

# research_os.yaml retired — hub uses strategies.yaml + strategy.yaml adopted specs
TRACK_ARTIFACTS: dict[str, list[str]] = {
    "vcp-pivot-gate": [
        "*_vcp_pivot_gate_daily_brief.md",
        "vcp_pivot_gate_daily_brief.md",
        "*_vcp_funnel_specs_daily_brief.md",
        "vcp_funnel_specs_daily_brief.md",
    ],
    "vcp-coil-close": [
        "*_vcp_coil_close_daily_brief.md",
        "vcp_coil_close_daily_brief.md",
        "*_vcp_funnel_specs_daily_brief.md",
        "vcp_funnel_specs_daily_brief.md",
    ],
    "rrg-mono-hold7": ["*_rrg_mono_daily.md", "rrg_mono_daily.md"],
}

TIMELINE_TABS: dict[str, dict[str, str | list[str]]] = {
    "00981a-l1h9-2025": {
        "label": "00981A L1H9 '25",
        "title": "00981A L1H9 多槽跟單 · RRG 時間軸 · 2025",
        "parent_track": "00981a-l1h9",
        "category": "00981a-copytrade",
        "patterns": [
            "*_00981a_l1h9_slots_rrg_timeline_2025.html",
            "*l1h9*timeline*_2025.html",
        ],
    },
    "00981a-l1h9-2026": {
        "label": "00981A L1H9 '26",
        "title": "00981A L1H9 多槽跟單 · RRG 時間軸 · 2026",
        "parent_track": "00981a-l1h9",
        "category": "00981a-copytrade",
        "patterns": [
            "*_00981a_l1h9_slots_rrg_timeline_2026.html",
            "*l1h9*timeline*_2026.html",
            "*_00981a_l1h9_slots_rrg_timeline.html",
            "*l1h9*timeline*.html",
        ],
    },
    "rrg-mono-hold7-2025": {
        "label": "RRG mono H7 '25",
        "title": "RRG mono · seg_last · 3槽 hold7 · RRG 時間軸 · 2025",
        "parent_track": "rrg-mono-hold7",
        "category": "rrg",
        "patterns": [
            "*_rrg_mono_hold7_slots_rrg_timeline_2025.html",
            "*rrg_mono*timeline*_2025.html",
        ],
    },
    "rrg-mono-hold7-2026": {
        "label": "RRG mono H7 '26",
        "title": "RRG mono · seg_last · 3槽 hold7 · RRG 時間軸 · 2026",
        "parent_track": "rrg-mono-hold7",
        "category": "rrg",
        "patterns": [
            "*_rrg_mono_hold7_slots_rrg_timeline_2026.html",
            "*rrg_mono*timeline*_2026.html",
            "*_rrg_mono_hold7_slots_rrg_timeline.html",
            "*rrg_mono*timeline*.html",
        ],
    },
    "vcp-chunge-funnel-2025": {
        "label": "VCP funnel H7 '25",
        "title": "VCP funnel · composite · 3槽 hold7 · RRG 時間軸 · 2025",
        "parent_track": "vcp-pivot-gate",
        "category": "vcp",
        "patterns": [
            "*_chunge_funnel_hold7_slots_rrg_timeline_2025.html",
            "*_vcp_pivot_gate*_slots_rrg_timeline_2025.html",
            "*_chunge_funnel_near_pivot_breakout_close_s5_h20_slots_rrg_timeline_2025.html",
            "*chunge*funnel*timeline*_2025.html",
            "*vcp_pivot_gate*timeline*_2025.html",
        ],
    },
    "vcp-pivot-gate-2026": {
        "label": "Pivot Gate '26",
        "title": "VCP Pivot Gate · near pivot · breakout close · 5槽 hold20 · RRG 時間軸 · 2026",
        "parent_track": "vcp-pivot-gate",
        "category": "vcp",
        "patterns": [
            "*_vcp_pivot_gate_s5_h20_slots_rrg_timeline_2026.html",
            "*_vcp_pivot_gate*_slots_rrg_timeline_2026.html",
            "*_chunge_funnel_near_pivot_breakout_close_s5_h20_slots_rrg_timeline_2026.html",
            "*_chunge_funnel_near_pivot_breakout_close_s5_h20_slots_rrg_timeline.html",
        ],
    },
    "vcp-coil-close-2026": {
        "label": "Coil Close '26",
        "title": "VCP Coil Close · near pivot · 訊號日 close · 5槽 hold20 · RRG 時間軸 · 2026",
        "parent_track": "vcp-pivot-gate",
        "category": "vcp",
        "patterns": [
            "*_vcp_coil_close_s5_h20_slots_rrg_timeline_2026.html",
            "*_vcp_coil_close*_slots_rrg_timeline_2026.html",
        ],
    },
    "vcp-chunge-funnel-2026": {
        "label": "VCP funnel H7 '26",
        "title": "VCP funnel · composite · 3槽 hold7 · RRG 時間軸 · 2026",
        "parent_track": "vcp-pivot-gate",
        "category": "vcp",
        "patterns": [
            "*_chunge_funnel_entry_ready_hold20_slots_rrg_timeline_2026.html",
            "*_chunge_funnel_hold7_slots_rrg_timeline_2026.html",
            "*chunge*funnel*timeline*_2026.html",
            "*_chunge_funnel_entry_ready_hold20_slots_rrg_timeline.html",
            "*_chunge_funnel_hold7_slots_rrg_timeline.html",
            "*chunge*funnel*timeline*.html",
        ],
    },
}

TIMELINE_TRACK_IDS = frozenset(
    {"00981a-l1h9", "rrg-mono-hold7", "vcp-pivot-gate"}
)


def _default_timeline_tab(track_id: str) -> str:
    if track_id == "vcp-pivot-gate":
        return "timeline-vcp-pivot-gate-2026"
    return f"timeline-{track_id}-2026"


def _resolve_timeline_file(tab_id: str, meta: dict) -> Path | None:
    category = meta.get("category")
    patterns = meta.get("patterns") or []
    dirs: list[Path] = []
    if category == "00981a-copytrade":
        dirs = [RESEARCH_COPYTRADE_00981A]
    elif category == "rrg":
        dirs = [RESEARCH_RRG]
    elif category == "vcp":
        dirs = [RESEARCH_VCP]
    else:
        dirs = [REPORTS_RESEARCH]

    year: str | None = None
    if tab_id.endswith("-2025"):
        year = "2025"
    elif tab_id.endswith("-2026"):
        year = "2026"

    for pat in patterns:
        hits: list[Path] = []
        for d in dirs:
            if d.is_dir():
                hits.extend(d.glob(pat))
        if not hits:
            continue
        if year == "2025":
            hits = [h for h in hits if "_2025" in h.stem]
        elif year == "2026":
            tagged = [h for h in hits if "_2026" in h.stem]
            legacy = [h for h in hits if "_2025" not in h.stem and "_2026" not in h.stem]
            hits = tagged or legacy
        if hits:
            return max(hits, key=lambda p: p.stat().st_mtime)
    return None


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _latest_file(patterns: list[str], *, dirs: list[Path]) -> Path | None:
    hits: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        for pat in patterns:
            hits.extend(d.glob(pat))
    if not hits:
        return None
    return max(hits, key=lambda p: p.stat().st_mtime)


def _rel_href(from_dir: Path, target: Path) -> str:
    try:
        rel = target.relative_to(from_dir).as_posix()
    except ValueError:
        try:
            rel = (Path("..") / target.relative_to(REPORTS_ROOT)).as_posix()
        except ValueError:
            rel = target.as_posix()
    return html.escape(rel, quote=True)


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=True)


def _track_panel_html(
    track_id: str,
    *,
    title: str,
    description: str,
    artifacts: list[tuple[str, Path]],
    timeline_tab: str | None,
    hub_dir: Path,
) -> str:
    links = ""
    if artifacts:
        items = "".join(
            f'<li><a href="{_rel_href(hub_dir, p)}" target="_blank" rel="noopener">'
            f'{_esc(label)}</a></li>'
            for label, p in artifacts
        )
        links = f'<h3>最新產物</h3><ul class="artifact-list">{items}</ul>'
    else:
        links = '<p class="muted">尚無 daily brief · 見回測時間軸分頁</p>'

    tl_btn = ""
    if timeline_tab:
        tl_btn = (
            f'<button type="button" class="btn" data-goto="{_esc(timeline_tab)}">'
            f'開啟 RRG 互動時間軸</button>'
        )

    desc = _esc(description.strip()) if description else ""
    desc_block = f'<p class="desc">{desc}</p>' if desc else ""

    return f"""
    <section class="panel-page" id="page-{track_id}" hidden>
      <header class="page-head">
        <h2>{_esc(title)}</h2>
        <p class="id-tag">{_esc(track_id)}</p>
      </header>
      {desc_block}
      {tl_btn}
      {links}
    </section>"""


def render_strategy_hub_html(
    *,
    hub_dir: Path,
    breadth_html: Path | None,
    timeline_files: dict[str, Path | None],
    research_os: dict,
    strategies: dict,
) -> str:
    tracks: dict = research_os.get("tracks") or {}
    strat_map: dict = strategies.get("strategies") or {}

    search_dirs = [REPORTS_ROOT, REPORTS_DAILY, REPORTS_RESEARCH]
    for track_id in tracks:
        td = daily_track_dir(track_id)
        if td.is_dir() and td not in search_dirs:
            search_dirs.insert(0, td)
    stamp = date.today().strftime("%Y-%m-%d")

    tabs: list[dict] = [
        {"id": "overview", "label": "概覽", "kind": "overview"},
        {"id": "breadth", "label": "Breadth zone", "kind": "iframe"},
    ]

    for tid, meta in TIMELINE_TABS.items():
        tabs.append(
            {
                "id": f"timeline-{tid}",
                "label": meta["label"],
                "kind": "iframe",
                "title": meta["title"],
            }
        )

    for track_id in tracks:
        tabs.append({"id": track_id, "label": tracks[track_id].get("title", track_id), "kind": "panel"})

    # --- overview cards ---
    cards: list[str] = []
    for track_id, tr in tracks.items():
        st = strat_map.get(track_id, {})
        goto = _default_timeline_tab(track_id) if track_id in TIMELINE_TRACK_IDS else track_id
        kind = "回測時間軸" if track_id in TIMELINE_TRACK_IDS else "Daily track"
        desc_raw = str(st.get("description", "")).strip().replace("\n", " ")
        if len(desc_raw) > 160:
            desc_raw = desc_raw[:157] + "…"
        cards.append(
            f'<article class="card" data-goto="{_esc(goto)}">'
            f'<h3>{_esc(tr.get("title", track_id))}</h3>'
            f'<p class="card-id">{_esc(track_id)} · {kind}</p>'
            f'<p class="card-desc">{_esc(desc_raw)}</p>'
            f"</article>"
        )
    cards.append(
        f'<article class="card" data-goto="breadth">'
        f"<h3>200MA Breadth zone</h3>"
        f'<p class="card-id">market_breadth_ma · 策略適用情境</p>'
        f'<p class="card-desc">五區間廣度圖 · Regime 四軸之一</p></article>'
    )

    # --- track panels ---
    panels: list[str] = []
    for track_id, tr in tracks.items():
        st = strat_map.get(track_id, {})
        pats = TRACK_ARTIFACTS.get(track_id, [])
        found: list[tuple[str, Path]] = []
        seen: set[Path] = set()
        track_dirs = [daily_track_dir(track_id), *search_dirs]
        for pat in pats:
            p = _latest_file([pat], dirs=track_dirs)
            if p and p not in seen:
                seen.add(p)
                found.append((p.name, p))
        panels.append(
            _track_panel_html(
                track_id,
                title=str(tr.get("title") or track_id),
                description=str(st.get("description") or ""),
                artifacts=found,
                timeline_tab=_default_timeline_tab(track_id) if track_id in TIMELINE_TRACK_IDS else None,
                hub_dir=hub_dir,
            )
        )

    # --- iframes ---
    breadth_src = ""
    if breadth_html and breadth_html.is_file():
        breadth_src = _rel_href(hub_dir, breadth_html)

    iframe_pages: list[str] = []
    if breadth_src:
        iframe_pages.append(
            f'<section class="iframe-page" id="page-breadth" hidden>'
            f'<iframe title="Breadth zone" data-src="{breadth_src}" loading="lazy"></iframe>'
            f"</section>"
        )

    for tid, meta in TIMELINE_TABS.items():
        tf = timeline_files.get(tid)
        if tf and tf.is_file():
            src = _rel_href(hub_dir, tf)
            iframe_pages.append(
                f'<section class="iframe-page" id="page-timeline-{tid}" hidden>'
                f'<iframe title="{_esc(meta["title"])}" data-src="{src}" loading="lazy"></iframe>'
                f"</section>"
            )

    tab_buttons = "".join(
        f'<button type="button" class="tab{" active" if i == 0 else ""}" '
        f'data-tab="{_esc(t["id"])}" role="tab">{_esc(t["label"])}</button>'
        for i, t in enumerate(tabs)
    )

    tabs_json = json.dumps([t["id"] for t in tabs], ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>策略入口 · 多軌並行 · {stamp}</title>
  <style>
    :root {{
      --bg:#0f0f0f; --panel:#181818; --border:#333; --text:#e4e4e4; --muted:#888; --accent:#6B8CAE;
    }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:var(--bg); color:var(--text);
      font-family:-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif; }}
    .hub-header {{
      position:sticky; top:0; z-index:20; background:#121212; border-bottom:1px solid var(--border);
      padding:10px 16px 0;
    }}
    .hub-header h1 {{ margin:0 0 4px; font-size:16px; font-weight:600; }}
    .hub-header .sub {{ margin:0 0 10px; font-size:12px; color:var(--muted); line-height:1.45; }}
    .tab-bar {{
      display:flex; flex-wrap:wrap; gap:6px; padding-bottom:8px; max-height:120px; overflow-y:auto;
    }}
    .tab {{
      background:#222; color:#bbb; border:1px solid #444; border-radius:6px;
      padding:6px 11px; font-size:12px; cursor:pointer; white-space:nowrap;
    }}
    .tab:hover {{ background:#2a2a2a; color:#fff; }}
    .tab.active {{ background:#1a2430; border-color:var(--accent); color:#fff; }}
    main {{ min-height:calc(100vh - 120px); }}
    .overview {{ padding:16px; max-width:1100px; margin:0 auto; }}
    .overview h2 {{ font-size:14px; color:var(--muted); margin:0 0 12px; font-weight:500; }}
    .card-grid {{
      display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:12px;
    }}
    .card {{
      background:var(--panel); border:1px solid var(--border); border-radius:8px;
      padding:12px 14px; cursor:pointer; transition:border-color .15s;
    }}
    .card:hover {{ border-color:#555; }}
    .card h3 {{ margin:0 0 4px; font-size:14px; }}
    .card-id {{ margin:0 0 8px; font-size:11px; color:var(--muted); }}
    .card-desc {{ margin:0; font-size:12px; color:#aaa; line-height:1.45; }}
    .panel-page {{ padding:16px 20px 32px; max-width:900px; margin:0 auto; }}
    .page-head h2 {{ margin:0 0 4px; font-size:18px; }}
    .id-tag {{ margin:0 0 12px; font-size:12px; color:var(--muted); }}
    .desc {{ font-size:13px; color:#aaa; line-height:1.55; white-space:pre-wrap; }}
    .btn {{
      background:#222; color:#ccc; border:1px solid #555; border-radius:6px;
      padding:8px 14px; font-size:12px; cursor:pointer; margin:8px 0 16px;
    }}
    .btn:hover {{ background:#2a2a2a; color:#fff; }}
    .artifact-list {{ margin:8px 0 0; padding-left:18px; font-size:13px; }}
    .artifact-list a {{ color:var(--accent); }}
    .muted {{ color:var(--muted); font-size:13px; }}
    .iframe-page {{ height:calc(100vh - 130px); min-height:480px; }}
    .iframe-page iframe {{
      width:100%; height:100%; border:none; background:#141414; display:block;
    }}
    .foot {{ padding:12px 16px 20px; font-size:11px; color:#555; text-align:center; }}
  </style>
</head>
<body>
  <header class="hub-header">
    <h1>策略入口 · 多軌並行</h1>
    <p class="sub">
      沒有完美策略 · 並行 alpha tracks · Breadth zone 判斷適用情境 ·
      產生：<code>scripts/render_strategy_hub_html.py</code>
    </p>
    <nav class="tab-bar" role="tablist">{tab_buttons}</nav>
  </header>
  <main>
    <section class="overview" id="page-overview">
      <h2>Research OS tracks（{len(tracks)}）+ Breadth + 回測時間軸</h2>
      <div class="card-grid">{"".join(cards)}</div>
    </section>
    {"".join(iframe_pages)}
    {"".join(panels)}
  </main>
  <p class="foot">Research OS v8 · strategy.yaml · {stamp}</p>
  <script>
    const TAB_IDS = {tabs_json};
    const loadedIframes = new Set();

    function showTab(id) {{
      if (!TAB_IDS.includes(id)) id = 'overview';
      document.querySelectorAll('[id^="page-"]').forEach(el => {{
        el.hidden = el.id !== 'page-' + id;
      }});
      document.querySelectorAll('.tab').forEach(btn => {{
        btn.classList.toggle('active', btn.dataset.tab === id);
      }});
      const page = document.getElementById('page-' + id);
      if (page && page.classList.contains('iframe-page')) {{
        const iframe = page.querySelector('iframe');
        if (iframe && iframe.dataset.src && !loadedIframes.has(id)) {{
          iframe.src = iframe.dataset.src;
          loadedIframes.add(id);
        }}
      }}
      history.replaceState(null, '', '#' + id);
    }}

    document.querySelectorAll('.tab').forEach(btn => {{
      btn.addEventListener('click', () => showTab(btn.dataset.tab));
    }});
    document.querySelectorAll('[data-goto]').forEach(el => {{
      el.addEventListener('click', () => showTab(el.dataset.goto));
    }});

    const hash = location.hash.replace(/^#/, '');
    showTab(hash && TAB_IDS.includes(hash) ? hash : 'overview');
  </script>
</body>
</html>"""


def main() -> int:
    if os.environ.get("RUN_RESEARCH_HTML_GEN", "0").strip() != "1":
        print(
            "SKIP: research HTML generation disabled "
            "(set RUN_RESEARCH_HTML_GEN=1 to enable)",
            file=sys.stderr,
        )
        return 0
    parser = argparse.ArgumentParser(description="Render Phase 1 strategy hub HTML")
    parser.add_argument(
        "--output",
        type=Path,
        default=RESEARCH_STRATEGY_HUB,
    )
    parser.add_argument(
        "--breadth-html",
        type=Path,
        default=None,
        help="Breadth zone HTML（預設：research/breadth 下最新 market_breadth_ma*.html）",
    )
    parser.add_argument(
        "--l1h9-html",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--mono-html",
        type=Path,
        default=None,
    )
    args = parser.parse_args()

    ensure_research_dir()

    breadth = args.breadth_html
    if breadth is None:
        breadth = latest_research_html("breadth", "*market_breadth_ma*.html")
        if breadth is None:
            breadth = _latest_file(
                ["*market_breadth_ma*.html"],
                dirs=[REPORTS_RESEARCH, REPORTS_ROOT],
            )

    hub_dir = args.output.resolve().parent
    hub_dir.mkdir(parents=True, exist_ok=True)

    strategy_cfg = load_strategy_config()
    strategies = _load_yaml(CONFIG / "strategies.yaml")

    timeline_files: dict[str, Path | None] = {}
    for tab_id, meta in TIMELINE_TABS.items():
        timeline_files[tab_id] = _resolve_timeline_file(tab_id, meta)

    if args.l1h9_html and args.l1h9_html.is_file():
        timeline_files["00981a-l1h9-2026"] = args.l1h9_html
    if args.mono_html and args.mono_html.is_file():
        timeline_files["rrg-mono-hold7-2026"] = args.mono_html

    html_out = render_strategy_hub_html(
        hub_dir=hub_dir,
        breadth_html=breadth,
        timeline_files=timeline_files,
        research_os={"tracks": strategy_cfg.hub_strategies()},
        strategies=strategies,
    )
    args.output.write_text(html_out, encoding="utf-8")
    print(f"Wrote {args.output}")
    if breadth:
        print(f"  breadth → {breadth.name}")
    for tid, p in timeline_files.items():
        print(f"  timeline {tid} → {p.name if p else 'MISSING'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
