#!/usr/bin/env python3
"""Rebuild 1y Weinstein stage heatmap (30W only); pin 景碩/健策/台塑化/華新科.

Watchlist mode (independent report, does not overwrite expert-pool heatmap)::

  python scripts/research/build_stage_heatmap_1y.py \\
    --symbols 2002,2327,... \\
    --out reports/research/watchlist_stage/stage_heatmap_1y_YYYYMMDD.html
"""
from __future__ import annotations

import argparse
import colorsys
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from stage_analysis import stage_series_daily  # noqa: E402
from stock_db import DEFAULT_DB_PATH  # noqa: E402

DEFAULT_OUT = ROOT / "reports/research/chip-overlays/2327_ta_adaptive/stage_heatmap_1y.html"
DEFAULT_END = "2026-07-24"
SOURCE = "finmind"
DEFAULT_PIN = ["2492", "3189", "3653", "6505", "6147"]
PIN_NAMES = {"2492": "華新科", "3189": "景碩", "3653": "健策", "6505": "台塑化", "6147": "頎邦"}
MA_PERIOD = 30  # weinstein_stage SSOT
MA_WEEKS = MA_PERIOD  # trading-day equivalent (MA_WEEKS*5) inside stage_series_daily
CONFIRM_DAYS = 1  # same-day flip (was 2 for anti-whipsaw; 2026-07-30 → 1)
# Heatmap uses stage_series_daily (daily-native rolling + confirm_days +
# higher_lows tolerance), not the old per-week classify_weinstein_stage() loop.

# Mutable run config (set in main)
OUT = DEFAULT_OUT
END = DEFAULT_END
START = (datetime.fromisoformat(DEFAULT_END) - timedelta(days=365)).strftime("%Y-%m-%d")
PIN: list[str] = list(DEFAULT_PIN)
REPORT_TITLE = "Weinstein Stage 熱力圖 · 30W"
REPORT_KIND = "expert_pool"
STAGE_COLOR = {0: "#e5e7eb", 1: "#93c5fd", 2: "#4ade80", 3: "#fbbf24", 4: "#f87171"}
STAGE_LABEL = {0: "—", 1: "S1 打底", 2: "S2 多頭", 3: "S3 走緩", 4: "S4 下跌"}
# S2 強度＝MA 斜率（4週%）與乖離（extension_pct）兩訊號正規化後取較強者。
# 只看 slope 會漏掉「噴出型」行情：30W 均線很遲鈍，單月噴出 60%+ 時 slope 可能還
# 只有個位數（均線來不及反應），但 extension 已經衝很高——2026-07 台塑化(6505)
# 就是這樣被錯判成最弱檔（S2+）。兩者各自對 cap 正規化到 0–1 取 max 再對應回 5 階。
# 個股門檻＝全樣本池 S2 樣本分位（約 p25/p50/p75/p90）；指數另外用自己的歷史分位
# 校準（見 INDEX_PROFILE）——指數是幾百檔股票平均後的結果，slope/extension 天生
# 比個股小很多，套個股門檻只會永遠卡在最淺一階（實測過去一年 slope 從沒超過 7.3%）。
S2_TIER_LABEL = ["S2+", "S2++", "S2+++", "S2++++", "S2+++++"]
S2_COLOR_LOW = "#dcfce7"  # 強度 0 端點
S2_COLOR_HIGH = "#14532d"  # 強度飽和端點

STOCK_PROFILE = {
    "slope_bins": [6.0, 11.0, 16.0, 22.0],  # ma_slope_pct，個股 S2 樣本分位
    "slope_cap": 24.0,
    "ext_bins": [35.0, 55.0, 85.0, 115.0],  # extension_pct，個股 S2 樣本分位
    "ext_cap": 120.0,
}
INDEX_PROFILE = {
    "slope_bins": [3.5, 4.5, 5.5, 6.8],  # ma_slope_pct，IX0001 自己的 S2 樣本分位
    "slope_cap": 7.2,
    "ext_bins": [14.0, 19.0, 26.0, 30.0],  # extension_pct，IX0001 自己的 S2 樣本分位
    "ext_cap": 33.0,
}
# 保留舊名供其他既有引用/相容用（僅代表個股門檻）。
S2_SLOPE_BINS = STOCK_PROFILE["slope_bins"]
S2_SLOPE_CAP = STOCK_PROFILE["slope_cap"]


def profile_for(sid: str) -> dict:
    return INDEX_PROFILE if sid == "IX0001" else STOCK_PROFILE


def _hex_to_rgb01(h: str) -> tuple[float, float, float]:
    h = h.lstrip("#")
    return tuple(int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _rgb01_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in rgb)


def lerp_hex_hsl(c1: str, c2: str, t: float) -> str:
    """Interpolate two hex colors in HLS space (constant-ish hue, smooth L/S ramp)."""
    t = max(0.0, min(1.0, t))
    h1, l1, s1 = colorsys.rgb_to_hls(*_hex_to_rgb01(c1))
    h2, l2, s2 = colorsys.rgb_to_hls(*_hex_to_rgb01(c2))
    h = h1 + (h2 - h1) * t
    l = l1 + (l2 - l1) * t
    s = s1 + (s2 - s1) * t
    return _rgb01_to_hex(colorsys.hls_to_rgb(h, l, s))


def s2_color_at_t(t: float) -> str:
    return lerp_hex_hsl(S2_COLOR_LOW, S2_COLOR_HIGH, t)


def s2_intensity_t(
    slope: float | None, extension: float | None, profile: dict
) -> float:
    t_slope = 0.0 if slope is None else max(0.0, slope) / profile["slope_cap"]
    t_ext = 0.0 if extension is None else max(0.0, extension) / profile["ext_cap"]
    return max(0.0, min(1.0, max(t_slope, t_ext)))


def s2_continuous_color(
    slope: float | None, extension: float | None, profile: dict
) -> str:
    return s2_color_at_t(s2_intensity_t(slope, extension, profile))


def s2_tier(
    slope: float | None,
    extension: float | None = None,
    profile: dict | None = None,
) -> int:
    """Bucket by whichever signal (slope or extension) is dominant, using
    *that* signal's own quantile-calibrated bin edges — not slope's edges
    for everyone. Extension-dominant (breakout) samples must be judged
    against the extension distribution, or their tier is meaningless
    (was dead-code: ext_bins/ext_cap were defined but never read here)."""
    prof = profile or STOCK_PROFILE
    t_slope = 0.0 if slope is None else max(0.0, slope) / prof["slope_cap"]
    t_ext = 0.0 if extension is None else max(0.0, extension) / prof["ext_cap"]
    if t_ext > t_slope:
        t, edges_t = min(1.0, t_ext), [b / prof["ext_cap"] for b in prof["ext_bins"]]
    else:
        t, edges_t = min(1.0, t_slope), [b / prof["slope_cap"] for b in prof["slope_bins"]]
    for i, te in enumerate(edges_t):
        if t < te:
            return i
    return len(edges_t)


# Legend swatches (stock scale): sample the continuous scale at each bin's
# midpoint so the legend matches what the cells actually render.
_bin_edges = [0.0, *STOCK_PROFILE["slope_bins"], STOCK_PROFILE["slope_cap"]]
S2_GRADIENT = [
    s2_color_at_t((_bin_edges[i] + _bin_edges[i + 1]) / 2 / STOCK_PROFILE["slope_cap"])
    for i in range(5)
]


def cell_color(
    stage: int,
    slope: float | None,
    extension: float | None = None,
    profile: dict | None = None,
) -> str:
    if stage == 2:
        return s2_continuous_color(slope, extension, profile or STOCK_PROFILE)
    return STAGE_COLOR.get(stage, "#e5e7eb")


def stage_badge_label(
    stage: int,
    slope: float | None,
    extension: float | None = None,
    profile: dict | None = None,
) -> str:
    if stage == 2:
        return S2_TIER_LABEL[s2_tier(slope, extension, profile or STOCK_PROFILE)]
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
    """Daily-confirmed PIT (stage, ma_slope_pct, extension_pct), one row per trading
    day — stage_series_daily (rolling MA directly on daily bars + N-day confirmation
    + higher_lows tolerance), not the old per-week classify_weinstein_stage() loop."""
    min_daily = max(80, MA_WEEKS * 6)
    if len(df) < min_daily:
        return []
    out_df = stage_series_daily(df, ma_weeks=MA_WEEKS, confirm_days=CONFIRM_DAYS)
    out_df = out_df[(out_df["date"] >= START) & (out_df["date"] <= END)]
    out: list[tuple[str, int, float | None, float | None]] = []
    for r in out_df.itertuples(index=False):
        sl = None if pd.isna(r.ma_slope_pct) else float(r.ma_slope_pct)
        ex = None if pd.isna(r.extension_pct) else float(r.extension_pct)
        out.append((str(r.date)[:10], int(r.stage), sl, ex))
    return out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Weinstein Stage 1y heatmap (30W SSOT)")
    p.add_argument(
        "--symbols",
        metavar="IDS",
        help="Watchlist mode：逗號分隔代號（獨立報告；略過專家池宇宙）",
    )
    p.add_argument(
        "--out",
        type=Path,
        help="輸出 HTML 路徑（watchlist 預設 reports/research/watchlist_stage/…）",
    )
    p.add_argument("--end", metavar="YYYY-MM-DD", help="面板末日（預設 2026-07-24 或 IX 最新）")
    p.add_argument("--names-json", type=Path, help="可選：holdings_pulse JSON（取 stock_name）")
    p.add_argument("--open", dest="open_html", action="store_true", help="產出後開啟 HTML")
    return p.parse_args(argv)


def _parse_symbols(raw: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.replace("\n", ",").replace(" ", ",").split(","):
        s = part.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _names_from_pulse(path: Path | None) -> dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    names: dict[str, str] = {}
    for row in payload.get("holdings") or []:
        sid = str(row.get("stock_id") or "").strip()
        name = str(row.get("stock_name") or "").strip()
        if sid and name and name != sid:
            names[sid] = name
    return names


def _configure_run(args: argparse.Namespace) -> list[str] | None:
    """Apply CLI to module globals. Returns watchlist symbols or None for expert-pool mode."""
    global OUT, END, START, PIN, REPORT_TITLE, REPORT_KIND
    watch = _parse_symbols(args.symbols) if args.symbols else None
    if args.end:
        END = args.end
    else:
        # Always resolve to the latest available IX0001 date (both expert-pool and
        # watchlist mode) — DEFAULT_END is a fixed fallback only, otherwise a
        # scheduled/cron run would keep regenerating the same frozen date range
        # forever instead of advancing with each new trading day.
        conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH.resolve()}?mode=ro", uri=True)
        try:
            ix_end = conn.execute(
                "SELECT MAX(date) FROM daily_bars WHERE code='IX0001' AND close>0"
            ).fetchone()[0]
        finally:
            conn.close()
        if ix_end:
            END = str(ix_end)[:10]
    START = (datetime.fromisoformat(END) - timedelta(days=365)).strftime("%Y-%m-%d")
    if watch:
        PIN = []
        REPORT_KIND = "watchlist"
        REPORT_TITLE = "Weinstein Stage 熱力圖 · 監控清單 · 30W"
        stamp = END.replace("-", "")
        OUT = (
            args.out
            if args.out
            else ROOT / "reports" / "research" / "watchlist_stage" / f"stage_heatmap_1y_{stamp}.html"
        )
    else:
        PIN = list(DEFAULT_PIN)
        REPORT_KIND = "expert_pool"
        REPORT_TITLE = "Weinstein Stage 熱力圖 · 30W"
        OUT = args.out if args.out else DEFAULT_OUT
    return watch


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    watch = _configure_run(args)
    name_map = _names_from_pulse(args.names_json)
    name_map.update(PIN_NAMES)

    universe: list[tuple[str, str]] = [("IX0001", "加權指數")]
    seen = {"IX0001"}
    if watch is None:
        mod = load_watch_mod()
        for sid in PIN:
            name = name_map.get(sid, sid)
            if sid in mod.POOLS:
                name = mod.POOLS[sid].stock_name
            universe.append((sid, name))
            seen.add(sid)
        for sid, sp in sorted(mod.POOLS.items()):
            if sid in seen:
                continue
            universe.append((sid, sp.stock_name))
            seen.add(sid)
    else:
        for sid in watch:
            if sid in seen:
                continue
            universe.append((sid, name_map.get(sid, sid)))
            seen.add(sid)

    conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH.resolve()}?mode=ro", uri=True)
    print(
        f"kind={REPORT_KIND} universe={len(universe)} pin={PIN or '—'} "
        f"ma={MA_PERIOD}W · {START}→{END}"
    )
    series: dict[str, list[tuple[str, int, float | None, float | None]]] = {}
    meta: list[dict] = []
    for i, (sid, name) in enumerate(universe):
        df = load_stock(conn, sid)
        st = stages_for(df)
        series[sid] = st
        last = st[-1][1] if st else 0
        last_sl = st[-1][2] if st else None
        last_ex = st[-1][3] if st else None
        meta.append(
            {
                "sid": sid,
                "name": name,
                "n": len(st),
                "last_stage": last,
                "last_slope": last_sl,
                "last_extension": last_ex,
                "last_s2_tier": (
                    S2_TIER_LABEL[s2_tier(last_sl, last_ex, profile_for(sid))]
                    if last == 2
                    else None
                ),
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
            sl, ex = info.get(m["sid"], {}).get(last_d, (None, None))
            s2_tier_counts[s2_tier(sl, ex, profile_for(m["sid"]))] += 1

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
        profile = profile_for(sid)
        cells = []
        for d in dates:
            s = mat.get(d, 0)
            sl, ex = inf.get(d, (None, None))
            c = cell_color(s, sl, ex, profile)
            note = ""
            if s == 2:
                sl_s = f"{sl:+.1f}%" if sl is not None else "?"
                ex_s = f"{ex:+.1f}%" if ex is not None else "?"
                note = f"（{S2_TIER_LABEL[s2_tier(sl, ex, profile)]} · MA斜率{sl_s}/4週 · 乖離{ex_s}）"
            title = f"{sid} {name} · {d} · weinstein_stage(30W當量·日更當天確認)={STAGE_LABEL.get(s, s)}{note}"
            cells.append(
                f'<td class="c" title="{esc(title)}" data-s="{s}" '
                f'style="background:{c}"></td>'
            )
        last = mat.get(last_d, 0)
        lsl, lex = inf.get(last_d, (None, None))
        pin_cls = " pin" if (sid in PIN or sid == "IX0001") else ""
        rows_html.append(
            f'<tr class="r{pin_cls}" data-sid="{esc(sid)}" data-s="{last}">'
            f'<th class="lab"><span class="sid">{esc(sid)}</span> {esc(name)}'
            f'<span class="badge" style="background:{cell_color(last, lsl, lex, profile)}">'
            f"{esc(stage_badge_label(last, lsl, lex, profile))}</span></th>"
            + "".join(cells)
            + "</tr>"
        )

    mh = ['<tr class="months"><th class="lab"></th>']
    tick_map = {mi: mlab[5:] for mi, mlab in month_ticks}
    for i, _d in enumerate(dates):
        mh.append(f'<td class="mh">{tick_map.get(i, "")}</td>')
    mh.append("</tr>")

    pin_note = (
        "、".join(f"{name_map.get(s, PIN_NAMES.get(s, s))}({s})" for s in PIN)
        if PIN
        else "無（監控清單模式）"
    )
    ix_lab = stage_badge_label(
        mats.get("IX0001", {}).get(last_d, 0),
        info.get("IX0001", {}).get(last_d, (None, None))[0],
        info.get("IX0001", {}).get(last_d, (None, None))[1],
        INDEX_PROFILE,
    )
    pin_stats = (
        " · ".join(
            f"{name_map.get(s, PIN_NAMES.get(s, s))} <b>"
            f"{esc(stage_badge_label(mats.get(s, {}).get(last_d, 0), *(info.get(s, {}).get(last_d, (None, None))), profile_for(s)))}"
            f"</b>"
            for s in PIN
        )
        if PIN
        else f"監控清單 <b>{len(universe) - 1}</b> 檔"
    )
    pin_js = json.dumps(["IX0001"] + PIN)
    built = datetime.now().strftime("%Y-%m-%d %H:%M")
    bins = S2_SLOPE_BINS
    filter_pin_label = "大盤＋置頂檔" if PIN else "僅大盤"

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{esc(REPORT_TITLE)}</title>
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
  <h1>{esc(REPORT_TITLE)}</h1>
  <div class="sub">weinstein_stage（30週當量 SSOT · 日更＋當天確認，confirm_days={CONFIRM_DAYS}，2026-07-30起）· S2 綠色漸層＝MA斜率／乖離正規化取較強者 · 大盤(IX0001)套用獨立分位校準（見 tooltip，legend 為個股尺度） · 置頂：{esc(pin_note)} · {esc(START)} → {esc(END)} · 建於 {esc(built)} · kind={esc(REPORT_KIND)}</div>
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
    <button type="button" data-f="pin">{esc(filter_pin_label)}</button>
  </div>
</header>
<div class="wrap">
<table class="heat">
  <thead>{"".join(mh)}</thead>
  <tbody>{"".join(rows_html)}</tbody>
</table>
</div>
<p class="hint">weinstein_stage（30週當量，日更＋當天確認 confirm_days={CONFIRM_DAYS}，higher_lows 容忍近期低點多跌1%內仍算未破底）。獨立於持倉脈動／RRG 報告。S2 五階綠（+~+++++）＝MA 斜率（4週%）：&lt;{bins[0]} + · {bins[0]}–{bins[1]} ++ · {bins[1]}–{bins[2]} +++ · {bins[2]}–{bins[3]} ++++ · &gt;{bins[3]} +++++（乖離見 tooltip）。左側綠邊＝置頂。Research 觀察用，非下單訊號。</p>
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
                "kind": REPORT_KIND,
                "start": START,
                "end": END,
                "last_date": last_d,
                "pin": PIN,
                "symbols": [s for s, _ in universe if s != "IX0001"],
                "ma_period": MA_PERIOD,
                "field_ssot": "weinstein_stage",
                "engine": "stage_series_daily (daily-native + confirm_days, merged 2026-07-27)",
                "confirm_days": CONFIRM_DAYS,
                "s2_gradient": {
                    "metric": "max(ma_slope_pct, extension_pct) normalized to each profile's cap",
                    "labels": S2_TIER_LABEL,
                    "colors_stock_scale": S2_GRADIENT,
                    "stock_profile": STOCK_PROFILE,
                    "index_profile": INDEX_PROFILE,
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
    for s in ["IX0001"] + (PIN or [m["sid"] for m in meta_sorted if m["sid"] != "IX0001"][:8]):
        stg = mats.get(s, {}).get(last_d, 0)
        sl, ex = info.get(s, {}).get(last_d, (None, None))
        print(s, stage_badge_label(stg, sl, ex, profile_for(s)))
    if args.open_html:
        import subprocess

        subprocess.run(["open", str(OUT)], check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
