"""RRG Universe 雙 WMA 四情境訊號盤中時間軸 HTML。"""

from __future__ import annotations

import json

from render_rrg_universe_html import (
    QUADRANT_COLORS,
    QUADRANT_LABEL_ZH,
    TRADE_SIGNAL_COLORS,
    TRADE_SIGNAL_ORDER,
    UNIVERSE_INTRADAY_PLAY_MS,
    UNIVERSE_INTRADAY_TAIL_FRAMES,
    UNIVERSE_TIMELINE_AXIS_WMA5_MAX,
    UNIVERSE_TIMELINE_AXIS_WMA5_MIN,
    UNIVERSE_TIMELINE_CHART_H,
    UNIVERSE_TIMELINE_CHART_W,
    _dual_timeline_projection,
    _projection_meta,
    _svg_timeline_background,
)
from rrg_universe_intraday_panel import DUAL_WMA_TRADE_SIGNALS


def render_universe_dual_wma_signals_intraday_html(
    *,
    frames: list[dict[str, str]],
    trajectories: list[dict],
    etf_codes: tuple[str, ...],
    meta: dict | None = None,
    short_length: int = 5,
    long_length: int = 20,
) -> str:
    dates = sorted({f["date"] for f in frames})
    date_label = f"{dates[0]} → {dates[-1]}"
    date_short = f"{dates[0][5:]} → {dates[-1][5:]}"
    avg_priced = (meta or {}).get("avg_priced_per_frame", "—")
    axis_lo, axis_hi = UNIVERSE_TIMELINE_AXIS_WMA5_MIN, UNIVERSE_TIMELINE_AXIS_WMA5_MAX
    axis_label = f"{int(axis_lo)}–{int(axis_hi)}"
    tail_frames = UNIVERSE_INTRADAY_TAIL_FRAMES

    signal_legend = "".join(
        f'<span class="legend-item"><i style="background:{TRADE_SIGNAL_COLORS[sid]}"></i>'
        f'{DUAL_WMA_TRADE_SIGNALS[sid]["label_zh"]} · {DUAL_WMA_TRADE_SIGNALS[sid]["action"]}</span>'
        for sid in TRADE_SIGNAL_ORDER
    )

    proj = _dual_timeline_projection(trajectories, fixed_bounds=(axis_lo, axis_hi))
    title = f"RRG 四情境訊號 · WMA({short_length}/{long_length}) · {date_short}"
    svg = _svg_timeline_background(
        proj, title=title, subtitle=frames[0]["label"], large_ui=True, clip_dynamic=False
    )

    payload = json.dumps(trajectories, ensure_ascii=False)
    frames_json = json.dumps(frames, ensure_ascii=False)
    proj_json = json.dumps(_projection_meta(proj))
    signal_colors_json = json.dumps(TRADE_SIGNAL_COLORS)
    signal_meta_json = json.dumps(DUAL_WMA_TRADE_SIGNALS, ensure_ascii=False)
    signal_order_json = json.dumps(list(TRADE_SIGNAL_ORDER))
    quad_colors_json = json.dumps(QUADRANT_COLORS)
    quad_labels_json = json.dumps(QUADRANT_LABEL_ZH, ensure_ascii=False)

    filter_buttons = "".join(
        f'<button data-signal="{sid}">{DUAL_WMA_TRADE_SIGNALS[sid]["label_zh"]}</button>'
        for sid in TRADE_SIGNAL_ORDER
    )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8"/>
  <title>RRG 四情境訊號 · 雙 WMA 盤中 · {date_label}</title>
  <style>
    body {{ margin:0; background:#141414; color:#e4e4e4; font-family:-apple-system,sans-serif; padding:20px; }}
    .wrap {{ max-width:{UNIVERSE_TIMELINE_CHART_W + 80}px; margin:0 auto; }}
    h1 {{ font-size:18px; margin:0 0 6px; }}
    .sub {{ color:#999; font-size:13px; margin-bottom:16px; line-height:1.5; }}
    .panel {{ background:#181818; border:1px solid #333; border-radius:8px; padding:12px; margin-bottom:16px; }}
    .panel.chart-panel {{ overflow:visible; padding:8px; position:relative; z-index:2; }}
    .chart-hint {{ font-size:12px; color:#888; margin:0 0 10px; }}
    .legend {{ display:flex; flex-wrap:wrap; gap:10px 16px; margin:8px 0 12px; font-size:12px; }}
    .legend-item i {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; }}
    .timeline-controls {{
      display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center; margin:12px 0 8px; font-size:13px;
    }}
    .timeline-controls input[type=range] {{ flex:1; min-width:180px; accent-color:#888; }}
    .timeline-controls button {{
      background:#222; color:#ccc; border:1px solid #444; border-radius:4px; padding:5px 12px; cursor:pointer; font-size:12px;
    }}
    .timeline-controls button:hover {{ background:#2a2a2a; color:#fff; }}
  #btn-universe-focus {{ display:none; border-color:#4A90D9; color:#9ec5f0; }}
    .timeline-controls input[type=text] {{
      background:#222; color:#eee; border:1px solid #444; border-radius:4px; padding:4px 8px; font-size:12px; width:96px;
    }}
    #frame-date {{ font-weight:600; color:#ddd; min-width:110px; }}
    .chart-layout {{ display:flex; flex-direction:column; gap:12px; }}
    .frame-insight {{
      background:#1a1a1a; border:1px solid #333; border-radius:8px; padding:14px 16px; font-size:13px;
      display:grid; grid-template-columns:1fr 1fr; gap:16px 24px;
    }}
    .frame-insight .filters {{ grid-column:1 / -1; }}
    .filters button {{
      background:#222; color:#ccc; border:1px solid #444; border-radius:4px; padding:4px 10px; cursor:pointer; font-size:12px; margin:0 4px 4px 0;
    }}
    .filters button.active {{ background:#333; color:#fff; border-color:#666; }}
    .signal-list {{ font-size:12px; line-height:1.55; max-height:200px; overflow-y:auto; }}
    .signal-list div {{ margin:3px 0; cursor:pointer; }}
    #tooltip {{
      display:none; position:fixed; z-index:99; background:#222; border:1px solid #555;
      border-radius:6px; padding:8px 10px; font-size:12px; color:#eee; pointer-events:none; max-width:300px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>RRG Universe 四情境訊號 · 僅顯示可交易組合</h1>
    <p class="sub">
      期間 <b>{date_label}</b> · <b>{len(frames)}</b> 幀 · 刻度 <b>{axis_label}</b> · 僅 <b>fresh K</b> 且符合四情境者顯示<br/>
      預設只畫 <b>WMA(20)</b> · 點擊個股顯示 <b>WMA(5)</b> 衛星向量 · 其餘標的全隱藏
    </p>
    <div class="legend">{signal_legend}</div>
    <div class="panel">
      <p class="chart-hint">點擊 WMA(20) 圓圈聚焦個股並顯示 WMA(5) · Esc 或「返回 Universe」回到清單視圖</p>
      <div class="timeline-controls">
        <button type="button" id="btn-prev">◀</button>
        <button type="button" id="btn-play">▶ 逐步</button>
        <button type="button" id="btn-next">▶</button>
        <button type="button" id="btn-universe-focus">← 返回 Universe</button>
        <input type="range" id="frame-slider" min="0" max="{len(frames) - 1}" value="0" step="1"/>
        <span id="frame-date">{frames[0]["label"]}</span>
        <input type="text" id="stock-search" placeholder="代號篩選…"/>
      </div>
      <div class="chart-layout">
        <div class="panel chart-panel" style="margin:0;border:none">{svg}</div>
        <aside class="frame-insight">
          <div class="filters" id="signal-filters">
            <button class="active" data-signal="all">全部情境</button>
            {filter_buttons}
          </div>
          <div>
            <h3 style="margin:0 0 8px;font-size:14px">當幀摘要</h3>
            <div id="insight-stats">—</div>
          </div>
          <div>
            <h3 style="margin:0 0 8px;font-size:14px">標的清單</h3>
            <div class="signal-list" id="insight-list">—</div>
          </div>
        </aside>
      </div>
    </div>
  </div>
  <div id="tooltip"></div>
  <script>
    const FRAMES = {frames_json};
    const TRAJECTORIES = {payload};
    let PROJ = {proj_json};
    const SIGNAL_COLORS = {signal_colors_json};
    const SIGNAL_META = {signal_meta_json};
    const SIGNAL_ORDER = {signal_order_json};
    const TAIL_FRAMES = {tail_frames};
    const PLAY_MS = {UNIVERSE_INTRADAY_PLAY_MS};
    const FRAME_INDEX = Object.fromEntries(FRAMES.map((f, i) => [f.id, i]));
    TRAJECTORIES.forEach(t => {{ t._byFrame = Object.fromEntries(t.points.map(p => [p.frame_id, p])); }});

    const layer = document.getElementById('dynamic-layer');
    const slider = document.getElementById('frame-slider');
    const frameDate = document.getElementById('frame-date');
    const frameLabel = document.getElementById('frame-label');
    const tooltip = document.getElementById('tooltip');
    const stockSearch = document.getElementById('stock-search');
    let frameIdx = 0, playing = false, playTimer = null, focusId = null, signalFilter = 'all';

    function sx(v) {{ return PROJ.margin.l + (v - PROJ.xmin) / (PROJ.xmax - PROJ.xmin) * PROJ.plot_w; }}
    function sy(v) {{ return PROJ.margin.t + PROJ.plot_h - (v - PROJ.ymin) / (PROJ.ymax - PROJ.ymin) * PROJ.plot_h; }}
    function pointAt(t, idx) {{ const fid = FRAMES[idx]?.id; return fid ? t._byFrame[fid] : null; }}
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
        if (!p || !p.trade_signal) return false;
        if (focusId && t.stock_id !== focusId) return false;
        if (signalFilter !== 'all' && p.trade_signal !== signalFilter) return false;
        if (q && !t.stock_id.includes(q)) return false;
        return true;
      }});
    }}
    function updateInsight(idx) {{
      const fr = FRAMES[idx];
      const visible = visibleTrajectories(idx);
      const buckets = Object.fromEntries(SIGNAL_ORDER.map(s => [s, []]));
      for (const t of visible) {{
        const p = pointAt(t, idx);
        if (p?.trade_signal) buckets[p.trade_signal].push({{ t, p }});
      }}
      const total = visible.length;
      const parts = SIGNAL_ORDER.map(s => `${{SIGNAL_META[s].label_zh}}: <b>${{buckets[s].length}}</b>`).join(' · ');
      document.getElementById('insight-stats').innerHTML =
        `<b>${{fr.date}}</b> ${{fr.minute}} · 第 ${{idx + 1}}/${{FRAMES.length}} 幀<br/>` +
        `可見 <b>${{total}}</b> 檔<br/>${{parts}}`;
      const list = [];
      for (const s of SIGNAL_ORDER) {{
        for (const {{ t, p }} of buckets[s]) {{
          list.push(`<div data-id="${{t.stock_id}}" style="color:${{SIGNAL_COLORS[s]}}">` +
            `<b>${{t.stock_id}}</b> ${{SIGNAL_META[s].label_zh}} · Δ${{p.spread_dist.toFixed(1)}} · ${{SIGNAL_META[s].action}}</div>`);
        }}
      }}
      document.getElementById('insight-list').innerHTML = list.join('') || '<span style="color:#666">本幀無符合標的</span>';
      document.querySelectorAll('#insight-list [data-id]').forEach(el => {{
        el.addEventListener('click', () => {{ focusId = el.dataset.id; updateFocusUI(); renderFrame(idx); }});
      }});
    }}
    function renderFrame(idx) {{
      frameIdx = idx;
      slider.value = String(idx);
      const fr = FRAMES[idx];
      frameDate.textContent = fr.label;
      if (frameLabel) frameLabel.textContent = fr.date + ' ' + fr.minute + ' · frame ' + (idx + 1) + '/' + FRAMES.length;
      updateInsight(idx);
      const parts = [];
      const ordered = visibleTrajectories(idx).slice().sort((a, b) => a.stock_id.localeCompare(b.stock_id));
      for (const t of ordered) {{
        const p = pointAt(t, idx);
        if (!p?.w20) continue;
        const sig = p.trade_signal;
        const color = SIGNAL_COLORS[sig] || '#aaa';
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
        const meta = SIGNAL_META[sig] || {{}};
        const label = `${{shortLabel(t.stock_id, t.stock_name)}} · ${{meta.label_zh || sig}} · ${{meta.action || ''}} · spread ${{p.spread_dist}}`;
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
    document.querySelectorAll('#signal-filters button').forEach(btn => {{
      btn.addEventListener('click', () => {{
        document.querySelectorAll('#signal-filters button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        signalFilter = btn.dataset.signal;
        renderFrame(frameIdx);
      }});
    }});
    stockSearch.addEventListener('input', () => renderFrame(frameIdx));
    document.getElementById('btn-universe-focus').addEventListener('click', clearFocus);
    document.addEventListener('keydown', ev => {{
      if (ev.target.tagName === 'INPUT') return;
      if (ev.key === 'ArrowLeft') {{ stopPlay(); renderFrame(Math.max(0, frameIdx - 1)); }}
      if (ev.key === 'ArrowRight') {{ stopPlay(); renderFrame(Math.min(FRAMES.length - 1, frameIdx + 1)); }}
      if (ev.key === 'Escape') clearFocus();
    }});
    renderFrame(0);
  </script>
</body>
</html>"""
