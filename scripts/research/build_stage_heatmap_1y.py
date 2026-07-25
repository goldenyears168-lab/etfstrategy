#!/usr/bin/env python3
"""Rebuild 1y Weinstein stage heatmap (30W only); pin 景碩/健策/台塑化/華新科."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from stage_analysis import classify_weinstein_stage  # noqa: E402

OUT = ROOT / "reports/research/chip-overlays/2327_ta_adaptive/stage_heatmap_1y.html"
START = (datetime(2026, 7, 24) - timedelta(days=365)).strftime("%Y-%m-%d")
END = "2026-07-24"
SOURCE = "finmind"
PIN = ["2492", "3189", "3653", "6505"]
PIN_NAMES = {"2492": "華新科", "3189": "景碩", "3653": "健策", "6505": "台塑化"}
MA_PERIOD = 30  # weinstein_stage SSOT
STAGE_COLOR = {0: "#e5e7eb", 1: "#93c5fd", 2: "#4ade80", 3: "#fbbf24", 4: "#f87171"}
STAGE_LABEL = {0: "—", 1: "S1 打底", 2: "S2 多頭", 3: "S3 走緩", 4: "S4 下跌"}
# S2 強度：MA 斜率（4 週%）分五階（≈ 專家池 S2 樣本分位），越陡越深綠、+ 越多
S2_SLOPE_BINS = [6.0, 11.0, 16.0, 22.0]  # <6 · 6–11 · 11–16 · 16–22 · >22
S2_GRADIENT = ["#dcfce7", "#bbf7d0", "#4ade80", "#16a34a", "#14532d"]
S2_TIER_LABEL = ["S2+", "S2++", "S2+++", "S2++++", "S2+++++"]


def s2_tier(slope: float | None) -> int:
    if slope is None:
        return 1
    for i, th in enumerate(S2_SLOPE_BINS):
        if slope < th:
            return i
    return len(S2_SLOPE_BINS)


def cell_color(stage: int, slope: float | None) -> str:
    if stage == 2:
        return S2_GRADIENT[s2_tier(slope)]
    return STAGE_COLOR.get(stage, "#e5e7eb")


def stage_badge_label(stage: int, slope: float | None) -> str:
    if stage == 2:
        return S2_TIER_LABEL[s2_tier(slope)]
    return STAGE_LABEL.get(stage, "—")


def esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def load_watch_mod():
    path = ROOT / "scripts/research/run_expert_pool_watch.py"
    spec = importlib.util.spec_from_file_location("epw_hm", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_hm"] = mod
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def load_stock(conn: sqlite3.Connection, sid: str) -> pd.DataFrame:
    if sid == "IX0001":
        rows = conn.execute(
            """
            SELECT date, open, high, low, close FROM daily_bars
            WHERE code='IX0001' AND date<=? AND close>0
              AND open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL
            ORDER BY date,
              CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
                   WHEN 'finmind' THEN 2 ELSE 3 END
            """,
            (END,),
        ).fetchall()
        seen_d: set[str] = set()
        out = []
        for r in rows:
            d = str(r[0])[:10]
            if d in seen_d:
                continue
            seen_d.add(d)
            out.append(
                {
                    "date": d,
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                }
            )
        return pd.DataFrame(out)
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date<=? AND close>0
        ORDER BY trade_date
        """,
        (sid, SOURCE, END),
    ).fetchall()
    return pd.DataFrame(
        [
            {
                "date": str(r[0]),
                "open": float(r[1] or r[4]),
                "high": float(r[2] or r[4]),
                "low": float(r[3] or r[4]),
                "close": float(r[4]),
            }
            for r in rows
        ]
    )


def stages_for(
    df: pd.DataFrame, *, ma_period: int = MA_PERIOD
) -> list[tuple[str, int, float | None, float | None]]:
    """Weekly PIT (stage, ma_slope_pct, extension_pct); ffill within week."""
    min_daily = max(80, ma_period * 6)
    if len(df) < min_daily:
        return []
    cal = [d for d in df["date"].tolist() if START <= d <= END]
    df2 = df.copy()
    df2["dt"] = pd.to_datetime(df2["date"])
    iso = df2["dt"].dt.isocalendar()
    df2["week"] = iso.week.astype(int)
    df2["year"] = iso.year.astype(int)
    week_ends = df2.groupby(["year", "week"], sort=True)["date"].max().tolist()
    week_ends = [d for d in week_ends if d <= END]
    stage_at: dict[str, tuple[int, float | None, float | None]] = {}
    for d in week_ends:
        hist = df[df["date"] <= d]
        if len(hist) < min_daily:
            continue
        r = classify_weinstein_stage(hist, ma_period=ma_period)
        stage_at[d] = (
            int(r.get("stage") or 0),
            r.get("ma_slope_pct"),
            r.get("extension_pct"),
        )
    ends_sorted = sorted(stage_at.keys())
    out: list[tuple[str, int, float | None, float | None]] = []
    j = -1
    cur: tuple[int, float | None, float | None] = (0, None, None)
    for d in cal:
        while j + 1 < len(ends_sorted) and ends_sorted[j + 1] <= d:
            j += 1
            cur = stage_at[ends_sorted[j]]
        if j >= 0:
            out.append((d, cur[0], cur[1], cur[2]))
    return out


def main() -> None:
    mod = load_watch_mod()
    universe: list[tuple[str, str]] = [("IX0001", "加權指數")]
    seen = {"IX0001"}
    for sid in PIN:
        name = PIN_NAMES.get(sid, sid)
        if sid in mod.POOLS:
            name = mod.POOLS[sid].stock_name
        universe.append((sid, name))
        seen.add(sid)
    for sid, sp in sorted(mod.POOLS.items()):
        if sid in seen:
            continue
        universe.append((sid, sp.stock_name))
        seen.add(sid)

    conn = sqlite3.connect(f"file:{(ROOT / 'data' / 'stocks.db').resolve()}?mode=ro", uri=True)
    print(f"universe={len(universe)} pin={PIN} ma={MA_PERIOD}W only")
    series: dict[str, list[tuple[str, int, float | None, float | None]]] = {}
    meta: list[dict] = []
    for i, (sid, name) in enumerate(universe):
        df = load_stock(conn, sid)
        st = stages_for(df)
        series[sid] = st
        last = st[-1][1] if st else 0
        last_sl = st[-1][2] if st else None
        meta.append(
            {
                "sid": sid,
                "name": name,
                "n": len(st),
                "last_stage": last,
                "last_slope": last_sl,
                "last_s2_tier": S2_TIER_LABEL[s2_tier(last_sl)] if last == 2 else None,
                "pinned": sid in PIN or sid == "IX0001",
            }
        )
        print(f"  [{i + 1}/{len(universe)}] {sid} {name} n={len(st)} 30W={last}")
    conn.close()

    dates = [d for d, *_ in series.get("IX0001", [])]
    last_d = dates[-1] if dates else ""
    mats = {sid: {d: s for d, s, _sl, _ex in st} for sid, st in series.items()}
    info = {sid: {d: (sl, ex) for d, _s, sl, ex in st} for sid, st in series.items()}

    def row_key(m: dict) -> tuple:
        sid = m["sid"]
        if sid == "IX0001":
            return (0, 0, sid)
        if sid in PIN:
            return (1, PIN.index(sid), sid)
        ls = m["last_stage"] or 9
        pri = {2: 1, 1: 2, 3: 3, 4: 4}.get(ls, 5)
        return (2, pri, sid)

    meta_sorted = sorted(meta, key=row_key)
    counts = {1: 0, 2: 0, 3: 0, 4: 0}
    s2_tier_counts = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
    for m in meta_sorted:
        if m["sid"] == "IX0001":
            continue
        s = mats.get(m["sid"], {}).get(last_d, 0)
        if s in counts:
            counts[s] += 1
        if s == 2:
            sl, _ = info.get(m["sid"], {}).get(last_d, (None, None))
            s2_tier_counts[s2_tier(sl)] += 1

    month_ticks: list[tuple[int, str]] = []
    prev_m = None
    for i, d in enumerate(dates):
        mm = d[:7]
        if mm != prev_m:
            month_ticks.append((i, mm))
            prev_m = mm

    rows_html: list[str] = []
    for m in meta_sorted:
        sid, name = m["sid"], m["name"]
        mat = mats.get(sid, {})
        inf = info.get(sid, {})
        cells = []
        for d in dates:
            s = mat.get(d, 0)
            sl, ex = inf.get(d, (None, None))
            c = cell_color(s, sl)
            note = ""
            if s == 2:
                sl_s = f"{sl:+.1f}%" if sl is not None else "?"
                ex_s = f"{ex:+.1f}%" if ex is not None else "?"
                note = f"（{S2_TIER_LABEL[s2_tier(sl)]} · MA斜率{sl_s}/4週 · 乖離{ex_s}）"
            title = f"{sid} {name} · {d} · weinstein_stage(30W)={STAGE_LABEL.get(s, s)}{note}"
            cells.append(
                f'<td class="c" title="{esc(title)}" data-s="{s}" '
                f'style="background:{c}"></td>'
            )
        last = mat.get(last_d, 0)
        lsl, _ = inf.get(last_d, (None, None))
        pin_cls = " pin" if (sid in PIN or sid == "IX0001") else ""
        rows_html.append(
            f'<tr class="r{pin_cls}" data-sid="{esc(sid)}" data-s="{last}">'
            f'<th class="lab"><span class="sid">{esc(sid)}</span> {esc(name)}'
            f'<span class="badge" style="background:{cell_color(last, lsl)}">'
            f"{esc(stage_badge_label(last, lsl))}</span></th>"
            + "".join(cells)
            + "</tr>"
        )

    mh = ['<tr class="months"><th class="lab"></th>']
    tick_map = {mi: mlab[5:] for mi, mlab in month_ticks}
    for i, _d in enumerate(dates):
        mh.append(f'<td class="mh">{tick_map.get(i, "")}</td>')
    mh.append("</tr>")

    pin_note = "、".join(f"{PIN_NAMES[s]}({s})" for s in PIN)
    ix_lab = stage_badge_label(
        mats.get("IX0001", {}).get(last_d, 0),
        info.get("IX0001", {}).get(last_d, (None, None))[0],
    )
    pin_stats = " · ".join(
        f"{PIN_NAMES[s]} <b>"
        f"{esc(stage_badge_label(mats.get(s, {}).get(last_d, 0), info.get(s, {}).get(last_d, (None, None))[0]))}"
        f"</b>"
        for s in PIN
    )
    pin_js = json.dumps(["IX0001"] + PIN)
    built = datetime.now().strftime("%Y-%m-%d %H:%M")
    bins = S2_SLOPE_BINS

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Weinstein Stage 熱力圖 · 30W</title>
<style>
  :root {{ --bg:#0f1419; --panel:#1a2332; --text:#e7ecf3; --muted:#8b9bb4; --border:#2a3548; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:"SF Pro Text","PingFang TC","Noto Sans TC",system-ui,sans-serif;
    background:var(--bg); color:var(--text); line-height:1.45; }}
  header {{ padding:1.25rem 1.5rem 0.75rem; border-bottom:1px solid var(--border);
    background:linear-gradient(180deg,#1a2332 0%,var(--bg) 100%); }}
  h1 {{ margin:0 0 0.35rem; font-size:1.35rem; font-weight:650; }}
  .sub {{ color:var(--muted); font-size:0.9rem; }}
  .legend {{ display:flex; flex-wrap:wrap; gap:0.75rem 1.25rem; margin-top:0.9rem; align-items:center; }}
  .leg {{ display:inline-flex; align-items:center; gap:0.4rem; font-size:0.85rem; }}
  .sw {{ width:14px; height:14px; border-radius:3px; border:1px solid #0003; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:0.5rem 1rem; margin-top:0.75rem; font-size:0.85rem; color:var(--muted); }}
  .stats b {{ color:var(--text); }}
  .wrap {{ overflow:auto; padding:0.75rem 1rem 2rem; max-height:calc(100vh - 220px); }}
  table.heat {{ border-collapse:separate; border-spacing:0; font-size:11px; }}
  th.lab {{ position:sticky; left:0; z-index:3; background:var(--panel); text-align:left;
    padding:4px 10px 4px 8px; white-space:nowrap; border-right:1px solid var(--border); min-width:200px; font-weight:500; }}
  tr.months th.lab {{ z-index:4; top:0; position:sticky; }}
  tr.months {{ position:sticky; top:0; z-index:5; }}
  td.mh {{ background:var(--panel); color:var(--muted); font-size:10px; text-align:left;
    padding:4px 0 4px 1px; height:22px; border-bottom:1px solid var(--border); }}
  td.c {{ width:4px; min-width:4px; max-width:4px; height:18px; padding:0; border:none; }}
  tr.r:hover td.c {{ outline:1px solid #fff4; }}
  tr.pin th.lab {{ background:#243044; box-shadow: inset 3px 0 0 #4ade80; }}
  .sid {{ color:var(--muted); font-variant-numeric:tabular-nums; margin-right:4px; }}
  .badge {{ display:inline-block; margin-left:6px; padding:1px 6px; border-radius:999px; font-size:10px; color:#111; font-weight:600; }}
  .hint {{ padding:0 1.5rem 1rem; color:var(--muted); font-size:0.8rem; }}
  .filters {{ display:flex; flex-wrap:wrap; gap:0.5rem; margin-top:0.75rem; }}
  .filters button {{ background:var(--panel); color:var(--text); border:1px solid var(--border);
    border-radius:6px; padding:4px 10px; cursor:pointer; font-size:0.8rem; }}
  .filters button.on {{ border-color:#4ade80; color:#4ade80; }}
</style>
</head>
<body>
<header>
  <h1>Weinstein Stage 熱力圖 · 30W</h1>
  <div class="sub">weinstein_stage（30W SSOT）· S2 綠色漸層＝MA 斜率強度 · 置頂：{esc(pin_note)} · {esc(START)} → {esc(END)} · 建於 {esc(built)}</div>
  <div class="legend">
    <span class="leg"><span class="sw" style="background:{STAGE_COLOR[1]}"></span>S1 打底</span>
    <span class="leg">S2 多頭：
      <span class="sw" style="background:{S2_GRADIENT[0]}" title="&lt;{bins[0]}%/4週"></span>+
      <span class="sw" style="background:{S2_GRADIENT[1]}" title="{bins[0]}–{bins[1]}%"></span>++
      <span class="sw" style="background:{S2_GRADIENT[2]}" title="{bins[1]}–{bins[2]}%"></span>+++
      <span class="sw" style="background:{S2_GRADIENT[3]}" title="{bins[2]}–{bins[3]}%"></span>++++
      <span class="sw" style="background:{S2_GRADIENT[4]}" title="&gt;{bins[3]}%"></span>+++++
    </span>
    <span class="leg"><span class="sw" style="background:{STAGE_COLOR[3]}"></span>S3 走緩</span>
    <span class="leg"><span class="sw" style="background:{STAGE_COLOR[4]}"></span>S4 下跌</span>
  </div>
  <div class="stats">
    最新 <b>{esc(last_d)}</b> · 大盤 <b>{esc(ix_lab)}</b> ·
    個股：S2 <b>{counts[2]}</b>（+{s2_tier_counts[0]}／++{s2_tier_counts[1]}／+++{s2_tier_counts[2]}／++++{s2_tier_counts[3]}／+++++{s2_tier_counts[4]}）
    S1 <b>{counts[1]}</b> S3 <b>{counts[3]}</b> S4 <b>{counts[4]}</b>
    · 置頂：{pin_stats}
  </div>
  <div class="filters">
    <button type="button" class="on" data-f="all">全部列</button>
    <button type="button" data-f="s2">僅 S2</button>
    <button type="button" data-f="s4">僅 S4</button>
    <button type="button" data-f="pin">大盤＋置頂檔</button>
  </div>
</header>
<div class="wrap">
<table class="heat">
  <thead>{"".join(mh)}</thead>
  <tbody>{"".join(rows_html)}</tbody>
</table>
</div>
<p class="hint">僅 weinstein_stage（30W）。S2 五階綠（+~+++++）＝MA 斜率（4週%）：&lt;{bins[0]} + · {bins[0]}–{bins[1]} ++ · {bins[1]}–{bins[2]} +++ · {bins[2]}–{bins[3]} ++++ · &gt;{bins[3]} +++++（乖離見 tooltip）。左側綠邊＝置頂。Research 觀察用，非下單訊號。</p>
<script>
  const PIN = new Set({pin_js});
  const rows = [...document.querySelectorAll('tr.r')];
  document.querySelectorAll('.filters button[data-f]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      document.querySelectorAll('.filters button[data-f]').forEach(b => b.classList.remove('on'));
      btn.classList.add('on');
      const f = btn.dataset.f;
      rows.forEach(tr => {{
        const sid = tr.dataset.sid;
        const s = Number(tr.dataset.s || 0);
        let show = true;
        if (f === 's2') show = sid === 'IX0001' || s === 2;
        if (f === 's4') show = sid === 'IX0001' || s === 4;
        if (f === 'pin') show = PIN.has(sid);
        tr.style.display = show ? '' : 'none';
      }});
    }});
  }});
</script>
</body>
</html>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    OUT.with_suffix(".json").write_text(
        json.dumps(
            {
                "built_at": datetime.now().isoformat(timespec="seconds"),
                "start": START,
                "end": END,
                "last_date": last_d,
                "pin": PIN,
                "ma_period": MA_PERIOD,
                "field_ssot": "weinstein_stage",
                "s2_gradient": {
                    "metric": "ma_slope_pct (4-week %)",
                    "bins": S2_SLOPE_BINS,
                    "colors": S2_GRADIENT,
                    "labels": S2_TIER_LABEL,
                },
                "ix_stage": mats.get("IX0001", {}).get(last_d),
                "pin_stages": {s: mats.get(s, {}).get(last_d) for s in PIN},
                "counts_30w": counts,
                "s2_tier_counts": s2_tier_counts,
                "rows": meta_sorted,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("wrote", OUT)
    for s in ["IX0001"] + PIN:
        stg = mats.get(s, {}).get(last_d, 0)
        sl = info.get(s, {}).get(last_d, (None, None))[0]
        print(s, stage_badge_label(stg, sl))


if __name__ == "__main__":
    main()
