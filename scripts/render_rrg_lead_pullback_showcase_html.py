"""強勢回踩 · 成果展 HTML（lead_pullback_buy only）。"""

from __future__ import annotations

import json

from render_rrg_universe_html import (
    TRADE_SIGNAL_COLORS,
    UNIVERSE_INTRADAY_PLAY_MS,
    UNIVERSE_INTRADAY_TAIL_FRAMES,
    UNIVERSE_TIMELINE_AXIS_WMA5_MAX,
    UNIVERSE_TIMELINE_AXIS_WMA5_MIN,
    UNIVERSE_TIMELINE_CHART_W,
    _dual_timeline_projection,
    _projection_meta,
    _svg_timeline_background,
)
from rrg_universe_intraday_panel import DUAL_WMA_TRADE_SIGNALS, SPREAD_SIGNAL_MIN

SIGNAL_ID = "lead_pullback_buy"
SIGNAL_COLOR = TRADE_SIGNAL_COLORS[SIGNAL_ID]
SIGNAL_META = DUAL_WMA_TRADE_SIGNALS[SIGNAL_ID]


def _anatomy_svg() -> str:
    return """
<svg viewBox="0 0 420 200" width="420" height="200" style="max-width:100%;background:#121212;border-radius:6px">
  <rect x="210" y="8" width="200" height="90" fill="#1F8A65" fill-opacity="0.12"/>
  <text x="310" y="24" text-anchor="middle" fill="#1F8A65" font-size="11" opacity="0.7">Leading</text>
  <circle cx="300" cy="55" r="14" fill="none" stroke="#1F8A65" stroke-width="2.5"/>
  <text x="300" y="59" text-anchor="middle" fill="#ccc" font-size="10">W20</text>
  <circle cx="268" cy="78" r="6" fill="#1F8A65" stroke="#111"/>
  <text x="268" y="96" text-anchor="middle" fill="#aaa" font-size="10">W5</text>
  <line x1="300" y1="55" x2="268" y2="78" stroke="#1F8A65" stroke-width="1.8" stroke-dasharray="4,3"/>
  <text x="284" y="62" fill="#888" font-size="10">spread≥2</text>
  <text x="12" y="120" fill="#aaa" font-size="12">空心環 = WMA(20) 錨點 · 實心點 = WMA(5) 衛星 · 連線 = pullback spread</text>
  <text x="12" y="142" fill="#6a6" font-size="12">✓ W20 Leading + pullback + fresh K</text>
  <text x="12" y="162" fill="#888" font-size="12">✗ 深回踩 wait（W5 Lagging 大 spread）· 轉強超前追價</text>
</svg>"""


def render_lead_pullback_showcase_html(
    *,
    frames: list[dict[str, str]],
    trajectories: list[dict],
    showcase_stats: dict,
    meta: dict | None = None,
    short_length: int = 5,
    long_length: int = 20,
) -> str:
    dates = sorted({f["date"] for f in frames})
    date_label = f"{dates[0]} → {dates[-1]}"
    axis_lo, axis_hi = UNIVERSE_TIMELINE_AXIS_WMA5_MIN, UNIVERSE_TIMELINE_AXIS_WMA5_MAX
    axis_label = f"{int(axis_lo)}–{int(axis_hi)}"
    tail_frames = UNIVERSE_INTRADAY_TAIL_FRAMES
    avg_priced = (meta or {}).get("avg_priced_per_frame", "—")

    proj = _dual_timeline_projection(trajectories, fixed_bounds=(axis_lo, axis_hi))
    date_short = f"{dates[0][5:]} → {dates[-1][5:]}"
    title = f"強勢回踩 · WMA({short_length}/{long_length}) · {date_short}"
    svg = _svg_timeline_background(
        proj, title=title, subtitle=frames[0]["label"], large_ui=True, clip_dynamic=False
    )

    spread_med = showcase_stats.get("spread_median")
    spread_p75 = showcase_stats.get("spread_p75")
    spread_med_s = f"{spread_med:.1f}" if spread_med is not None else "—"
    spread_p75_s = f"{spread_p75:.1f}" if spread_p75 is not None else "—"

    case_chips = []
    for i, c in enumerate(showcase_stats.get("top_cases") or [], 1):
        label = f"#{i} {c['stock_id']} Δ{c['spread_dist']:.1f}"
        case_chips.append(
            f'<button type="button" class="case-chip" data-idx="{c["frame_idx"]}" '
            f'data-id="{c["stock_id"]}">{label}</button>'
        )
    case_chips_html = "".join(case_chips) or '<span style="color:#666">—</span>'

    payload = json.dumps(trajectories, ensure_ascii=False)
    frames_json = json.dumps(frames, ensure_ascii=False)
    proj_json = json.dumps(_projection_meta(proj))
    signal_frames_json = json.dumps(showcase_stats.get("signal_frame_indices") or [])
    top_cases_json = json.dumps(showcase_stats.get("top_cases") or [], ensure_ascii=False)
    quad_labels = {
        "leading": "Leading",
        "weakening": "Weakening",
        "lagging": "Lagging",
        "improving": "Improving",
    }
    quad_labels_json = json.dumps(quad_labels, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <title>強勢回踩 · 成果展 · {date_label}</title>
  <style>
    body {{ margin:0; background:#141414; color:#e4e4e4; font-family:-apple-system,sans-serif; padding:20px; }}
    .wrap {{ max-width:{UNIVERSE_TIMELINE_CHART_W + 80}px; margin:0 auto; }}
    h1 {{ font-size:20px; margin:0 0 6px; color:#1F8A65; }}
    .sub {{ color:#999; font-size:13px; margin-bottom:12px; line-height:1.5; }}
    .kpi-banner {{
      display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:12px 16px;
      background:#1a1f1c; border:1px solid #2a4a3a; border-radius:8px; padding:14px 16px; margin-bottom:14px;
    }}
    .kpi-block .label {{ color:#888; font-size:11px; text-transform:uppercase; letter-spacing:0.04em; }}
    .kpi-block .value {{ font-size:22px; font-weight:700; color:#1F8A65; margin-top:2px; }}
    .kpi-block .hint {{ font-size:11px; color:#666; margin-top:2px; }}
    details.guide {{ background:#181818; border:1px solid #333; border-radius:8px; padding:10px 14px; margin-bottom:14px; }}
    details.guide summary {{ cursor:pointer; color:#ccc; font-weight:600; }}
    .panel {{ background:#181818; border:1px solid #333; border-radius:8px; padding:12px; margin-bottom:16px; }}
    .panel.chart-panel {{ overflow:visible; padding:8px; position:relative; z-index:2; }}
    .chart-hint {{ font-size:12px; color:#888; margin:0 0 10px; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:10px 16px; margin:8px 0 12px; font-size:12px; }}
    .legend-item i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; background:{SIGNAL_COLOR}; }}
    .timeline-controls {{
      display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; margin:12px 0 8px; font-size:13px;
    }}
    .timeline-controls input[type=range] {{ flex:1; min-width:180px; accent-color:#1F8A65; }}
    .timeline-controls button {{
      background:#222; color:#ccc; border:1px solid #444; border-radius:4px; padding:5px 12px; cursor:pointer; font-size:12px;
    }}
    .timeline-controls button:hover {{ background:#2a2a2a; color:#fff; }}
    #btn-universe-focus {{ display:none; border-color:#1F8A65; color:#9ec5f0; }}
    .timeline-controls input[type=text] {{
      background:#222; color:#eee; border:1px solid #444; border-radius:4px; padding:4px 8px; font-size:12px; width:96px;
    }}
    #frame-date {{ font-weight:600; color:#ddd; min-width:110px; }}
    .case-row {{ display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 4px; align-items:center; font-size:12px; color:#888; }}
    .case-chip {{
      background:#1a2a22; color:#9fd4b8; border:1px solid #1F8A65; border-radius:14px;
      padding:3px 10px; cursor:pointer; font-size:11px;
    }}
    .case-chip:hover {{ background:#243830; }}
    .chart-layout {{ display:flex; flex-direction:column; gap:12px; }}
    .frame-insight {{
      background:#1a1a1a; border:1px solid #333; border-radius:8px; padding:14px 16px; font-size:13px;
      display:grid; grid-template-columns:1fr 1fr; gap:16px 24px;
    }}
    .signal-list {{ font-size:12px; line-height:1.55; max-height:220px; overflow-y:auto; }}
    .signal-list div {{ margin:3px 0; cursor:pointer; color:{SIGNAL_COLOR}; }}
    .footer-note {{ font-size:12px; color:#888; line-height:1.6; margin-top:8px; padding:12px 14px;
      background:#181818; border:1px solid #333; border-radius:8px; }}
    #tooltip {{
      display:none; position:fixed; z-index:99; background:#222; border:1px solid #555;
      border-radius:6px; padding:8px 10px; font-size:12px; color:#eee; pointer-events:none; max-width:300px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>強勢回踩 · 成果展</h1>
    <p class="sub">
      期間 <b>{date_label}</b> · <b>{len(frames)}</b> 幀 · 刻度 <b>{axis_label}</b> ·
      僅 <b>fresh K</b> · 預設 spread≥{SPREAD_SIGNAL_MIN:g} · 平均每幀有 K <b>{avg_priced}</b> 檔
    </p>
    <div class="kpi-banner">
      <div class="kpi-block">
        <div class="label">回踩事件</div>
        <div class="value">{showcase_stats.get("n_events", 0)}</div>
        <div class="hint">fresh · lead_pullback</div>
      </div>
      <div class="kpi-block">
        <div class="label">涉及標的</div>
        <div class="value">{showcase_stats.get("n_stocks", 0)}</div>
        <div class="hint">unique stocks</div>
      </div>
      <div class="kpi-block">
        <div class="label">spread 中位</div>
        <div class="value">{spread_med_s}</div>
        <div class="hint">P75 {spread_p75_s}</div>
      </div>
      <div class="kpi-block">
        <div class="label">有訊號幀</div>
        <div class="value">{showcase_stats.get("n_signal_frames", 0)}</div>
        <div class="hint">/ {showcase_stats.get("n_frames_total", len(frames))} poll</div>
      </div>
      <div class="kpi-block">
        <div class="label">Advisory</div>
        <div class="value" style="font-size:14px">observe</div>
        <div class="hint">buy-signal-radar · 預設規格</div>
      </div>
    </div>
    <details class="guide" open>
      <summary>幾何說明 · W20 錨點 + W5 衛星 + spread 向量</summary>
      <div style="margin-top:10px">{_anatomy_svg()}</div>
    </details>
    <div class="legend">
      <span class="legend-item"><i></i>{SIGNAL_META["label_zh"]} · {SIGNAL_META["detail"]} · {SIGNAL_META["action"]}</span>
    </div>
    <div class="panel">
      <p class="chart-hint">點 WMA(20) 空心環聚焦 · 顯示 W5 衛星與 spread · Esc 返回 · [ ] 上下回踩幀</p>
      <div class="case-row">
        <span>精選案例：</span>{case_chips_html}
      </div>
      <div class="timeline-controls">
        <button type="button" id="btn-prev-signal" title="[">⏮ 回踩</button>
        <button type="button" id="btn-prev">◀</button>
        <button type="button" id="btn-play">▶ 逐步</button>
        <button type="button" id="btn-next">▶</button>
        <button type="button" id="btn-next-signal" title="]">回踩 ⏭</button>
        <button type="button" id="btn-universe-focus">← 返回 Universe</button>
        <input type="range" id="frame-slider" min="0" max="{len(frames) - 1}" value="0" step="1"/>
        <span id="frame-date">{frames[0]["label"]}</span>
        <input type="text" id="stock-search" placeholder="代號篩選…"/>
      </div>
      <div class="chart-layout">
        <div class="panel chart-panel" style="margin:0;border:none">{svg}</div>
        <aside class="frame-insight">
          <div>
            <h3 style="margin:0 0 8px;font-size:14px">當幀摘要</h3>
            <div id="insight-stats">—</div>
          </div>
          <div>
            <h3 style="margin:0 0 8px;font-size:14px">回踩清單（spread↓）</h3>
            <div class="signal-list" id="insight-list">—</div>
          </div>
        </aside>
      </div>
    </div>
    <div class="footer-note">
      <b>應用定位</b><br/>
      · <b>主倉</b>：C18acc+S2 不變<br/>
      · <b>回踩</b>：buy-signal-radar 觀測池 <code>dual-wma-lead-pullback</code>（notify=false）· 第二意見<br/>
      · <b>勿誤用</b>：調參 pb 3–5 長窗未過 · 不作獨立 3 槽 slot 策略
    </div>
  </div>
  <div id="tooltip"></div>
  <script>
    const FRAMES = {frames_json};
    const TRAJECTORIES = {payload};
    let PROJ = {proj_json};
    const SIGNAL_COLOR = {json.dumps(SIGNAL_COLOR)};
    const SIGNAL_META = {json.dumps(SIGNAL_META, ensure_ascii=False)};
    const SIGNAL_FRAMES = {signal_frames_json};
    const TOP_CASES = {top_cases_json};
    const QUAD_ZH = {quad_labels_json};
    const TAIL_FRAMES = {tail_frames};
    const PLAY_MS = {UNIVERSE_INTRADAY_PLAY_MS};
    const FRAME_INDEX = Object.fromEntries(FRAMES.map((f, i) => [f.id, i]));
    TRAJECTORIES.forEach(t => {{ t._byFrame = Object.fromEntries(t.points.map(p => [p.frame_id, p])); }});

    const layer = document.getElementById('dynamic-layer');
    const slider = document.getElementById('frame-slider');
    const frameDate = document.getElementById('frame-date');
    const tooltip = document.getElementById('tooltip');
    const stockSearch = document.getElementById('stock-search');
    let frameIdx = 0, playing = false, playTimer = null, focusId = null;

    function sx(v) {{ return PROJ.margin.l + (v - PROJ.xmin) / (PROJ.xmax - PROJ.xmin) * PROJ.plot_w; }}
    function sy(v) {{ return PROJ.margin.t + PROJ.plot_h - (v - PROJ.ymin) / (PROJ.ymax - PROJ.ymin) * PROJ.plot_h; }}
    function pointAt(t, idx) {{ const fid = FRAMES[idx]?.id; return fid ? t._byFrame[fid] : null; }}
    function isPullbackPoint(p) {{
      return p && p.fresh && p.trade_signal === 'lead_pullback_buy';
    }}
    function tailPointsW20(t, idx) {{
      const out = [];
      for (const p of t.points) {{
        const pi = FRAME_INDEX[p.frame_id];
        if (pi !== undefined && pi <= idx) out.push(p);
      }}
      return out.slice(-TAIL_FRAMES);
    }}
    function updateFocusUI() {{
      const btn = document.getElementById('btn-universe-focus');
      if (btn) btn.style.display = focusId ? 'inline-block' : 'none';
    }}
    function clearFocus() {{ focusId = null; updateFocusUI(); renderFrame(frameIdx); }}
    function shortLabel(id, name) {{
      let n = (name || '').trim();
      if (n.length > 8) n = n.slice(0, 7) + '…';
      return (id + ' ' + n).trim();
    }}
    function visibleTrajectories(idx) {{
      const q = stockSearch.value.trim();
      return TRAJECTORIES.filter(t => {{
        const p = pointAt(t, idx);
        if (!isPullbackPoint(p)) return false;
        if (focusId && t.stock_id !== focusId) return false;
        if (q && !t.stock_id.includes(q)) return false;
        return true;
      }});
    }}
    function jumpSignalFrame(dir) {{
      if (!SIGNAL_FRAMES.length) return;
      stopPlay();
      let pos = SIGNAL_FRAMES.findIndex(i => i >= frameIdx);
      if (dir < 0) {{
        const prev = SIGNAL_FRAMES.filter(i => i < frameIdx);
        renderFrame(prev.length ? prev[prev.length - 1] : SIGNAL_FRAMES[SIGNAL_FRAMES.length - 1]);
      }} else {{
        if (pos === -1 || (pos === frameIdx && frameIdx === SIGNAL_FRAMES[pos])) pos++;
        if (pos >= SIGNAL_FRAMES.length) pos = 0;
        renderFrame(SIGNAL_FRAMES[pos]);
      }}
    }}
    function updateInsight(idx) {{
      const fr = FRAMES[idx];
      const visible = visibleTrajectories(idx);
      const rows = visible.map(t => {{ const p = pointAt(t, idx); return {{ t, p }}; }})
        .sort((a, b) => (b.p.spread_dist || 0) - (a.p.spread_dist || 0));
      document.getElementById('insight-stats').innerHTML =
        `<b>${{fr.date}}</b> ${{fr.minute}} · 第 ${{idx + 1}}/${{FRAMES.length}} 幀<br/>` +
        `回踩 <b>${{rows.length}}</b> 檔 · ${{SIGNAL_META.label_zh}}`;
      const list = rows.map(({{ t, p }}) => {{
        const w5q = QUAD_ZH[p.w5?.quadrant] || p.w5?.quadrant || '—';
        const w20q = QUAD_ZH[p.w20?.quadrant] || p.w20?.quadrant || '—';
        return `<div data-id="${{t.stock_id}}"><b>${{t.stock_id}}</b> ${{t.stock_name || ''}} · ` +
          `spread ${{Number(p.spread_dist).toFixed(1)}} · W20 ${{w20q}} · W5 ${{w5q}} · ${{SIGNAL_META.action}}</div>`;
      }});
      document.getElementById('insight-list').innerHTML = list.join('') || '<span style="color:#666">本幀無回踩</span>';
      document.querySelectorAll('#insight-list [data-id]').forEach(el => {{
        el.addEventListener('click', () => {{ focusId = el.dataset.id; updateFocusUI(); renderFrame(idx); }});
      }});
    }}
    function renderFrame(idx) {{
      frameIdx = idx;
      slider.value = String(idx);
      const fr = FRAMES[idx];
      frameDate.textContent = fr.label;
      updateInsight(idx);
      const parts = [];
      const ordered = visibleTrajectories(idx).slice().sort((a, b) => a.stock_id.localeCompare(b.stock_id));
      for (const t of ordered) {{
        const p = pointAt(t, idx);
        if (!p?.w20) continue;
        const color = SIGNAL_COLOR;
        const dim = focusId && t.stock_id !== focusId;
        const op = dim ? 0.12 : (focusId ? 1.0 : 0.9);
        const showW5 = focusId === t.stock_id;
        const tailPts = tailPointsW20(t, idx);
        if (tailPts.length >= 2) {{
          for (let i = 0; i < tailPts.length - 1; i++) {{
            const a = tailPts[i].w20, b = tailPts[i + 1].w20;
            const segOp = (dim ? 0.08 : 0.35) * (0.5 + 0.5 * (i / Math.max(tailPts.length - 1, 1)));
            parts.push(`<line x1="${{sx(a.rs_ratio).toFixed(1)}}" y1="${{sy(a.rs_momentum).toFixed(1)}}" ` +
              `x2="${{sx(b.rs_ratio).toFixed(1)}}" y2="${{sy(b.rs_momentum).toFixed(1)}}" stroke="${{color}}" ` +
              `stroke-width="${{showW5 ? 2.2 : 1.4}}" opacity="${{segOp.toFixed(2)}}" stroke-linecap="round"/>`);
          }}
        }}
        const w20 = p.w20, w5 = p.w5;
        const x20 = sx(w20.rs_ratio), y20 = sy(w20.rs_momentum);
        const r20 = showW5 ? 9 : 7;
        const label = `${{shortLabel(t.stock_id, t.stock_name)}} · spread ${{p.spread_dist}} · ${{SIGNAL_META.action}}`;
        if (showW5 && w5) {{
          const x5 = sx(w5.rs_ratio), y5 = sy(w5.rs_momentum);
          parts.push(`<line x1="${{x20.toFixed(1)}}" y1="${{y20.toFixed(1)}}" x2="${{x5.toFixed(1)}}" y2="${{y5.toFixed(1)}}" ` +
            `stroke="${{color}}" stroke-width="1.6" opacity="0.75" stroke-linecap="round"/>`);
          parts.push(`<circle cx="${{x5.toFixed(1)}}" cy="${{y5.toFixed(1)}}" r="5" fill="${{color}}" stroke="#111" stroke-width="1"/>`);
          parts.push(`<text x="${{(x5 + 8).toFixed(1)}}" y="${{(y5 + 4).toFixed(1)}}" fill="#ddd" font-size="13" font-weight="600">${{shortLabel(t.stock_id, t.stock_name)}}</text>`);
        }}
        parts.push(
          `<circle class="sig-hit" cx="${{x20.toFixed(1)}}" cy="${{y20.toFixed(1)}}" r="16" fill="transparent" data-id="${{t.stock_id}}" data-label="${{label}}"/>` +
          `<circle cx="${{x20.toFixed(1)}}" cy="${{y20.toFixed(1)}}" r="${{r20}}" fill="none" stroke="${{color}}" stroke-width="2.5" opacity="${{op.toFixed(2)}}" pointer-events="none"/>`
        );
      }}
      layer.innerHTML = parts.join('');
      layer.querySelectorAll('circle.sig-hit').forEach(el => {{
        el.style.cursor = 'pointer';
        el.addEventListener('click', () => {{ focusId = focusId === el.dataset.id ? null : el.dataset.id; updateFocusUI(); renderFrame(idx); }});
        el.addEventListener('mouseenter', ev => {{
          tooltip.innerHTML = el.dataset.label || el.dataset.id;
          tooltip.style.display = 'block';
          tooltip.style.left = (ev.clientX + 12) + 'px';
          tooltip.style.top = (ev.clientY + 12) + 'px';
        }});
        el.addEventListener('mousemove', ev => {{
          tooltip.style.left = (ev.clientX + 12) + 'px';
          tooltip.style.top = (ev.clientY + 12) + 'px';
        }});
        el.addEventListener('mouseleave', () => {{ tooltip.style.display = 'none'; }});
      }});
      updateFocusUI();
    }}
    function stopPlay() {{
      playing = false;
      if (playTimer) {{ clearInterval(playTimer); playTimer = null; }}
      document.getElementById('btn-play').textContent = '▶ 逐步';
    }}
    slider.addEventListener('input', () => {{ stopPlay(); renderFrame(parseInt(slider.value, 10)); }});
    document.getElementById('btn-prev').addEventListener('click', () => {{ stopPlay(); renderFrame(Math.max(0, frameIdx - 1)); }});
    document.getElementById('btn-next').addEventListener('click', () => {{ stopPlay(); renderFrame(Math.min(FRAMES.length - 1, frameIdx + 1)); }});
    document.getElementById('btn-prev-signal').addEventListener('click', () => jumpSignalFrame(-1));
    document.getElementById('btn-next-signal').addEventListener('click', () => jumpSignalFrame(1));
    document.getElementById('btn-play').addEventListener('click', () => {{
      if (playing) {{ stopPlay(); return; }}
      if (frameIdx >= FRAMES.length - 1) renderFrame(0);
      playing = true;
      document.getElementById('btn-play').textContent = '⏸ 暫停';
      playTimer = setInterval(() => {{
        if (frameIdx >= FRAMES.length - 1) {{ stopPlay(); return; }}
        renderFrame(frameIdx + 1);
      }}, PLAY_MS);
    }});
    document.querySelectorAll('.case-chip').forEach(btn => {{
      btn.addEventListener('click', () => {{
        stopPlay();
        focusId = btn.dataset.id;
        renderFrame(parseInt(btn.dataset.idx, 10));
      }});
    }});
    stockSearch.addEventListener('input', () => renderFrame(frameIdx));
    document.getElementById('btn-universe-focus').addEventListener('click', clearFocus);
    document.addEventListener('keydown', ev => {{
      if (ev.target.tagName === 'INPUT') return;
      if (ev.key === 'ArrowLeft') {{ stopPlay(); renderFrame(Math.max(0, frameIdx - 1)); }}
      if (ev.key === 'ArrowRight') {{ stopPlay(); renderFrame(Math.min(FRAMES.length - 1, frameIdx + 1)); }}
      if (ev.key === '[') jumpSignalFrame(-1);
      if (ev.key === ']') jumpSignalFrame(1);
      if (ev.key === 'Escape') clearFocus();
    }});
    if (TOP_CASES.length) {{
      focusId = TOP_CASES[0].stock_id;
      renderFrame(TOP_CASES[0].frame_idx);
    }} else {{
      renderFrame(0);
    }}
  </script>
</body>
</html>"""
