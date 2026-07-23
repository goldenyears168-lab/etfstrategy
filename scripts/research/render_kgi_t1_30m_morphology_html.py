#!/usr/bin/env python3
"""Render KGI Xinyi / Songshan T+1 first-30m morphology HTML galleries."""
from __future__ import annotations

import json
import sqlite3
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "reports/research/branch-footprint-screen"
DB = ROOT / "data/stocks.db"
SOURCE = "finmind"

CSS = """
:root {
  --bg0:#0c1118; --bg1:#141c27; --bg2:#1c2736; --line:#2a3a4f;
  --text:#e8eef6; --muted:#8fa3bb; --gold:#e2b657; --teal:#3ecfb2;
  --rose:#e07a7a; --ok:#6bcf8e;
  --font-d:"Iowan Old Style","Palatino Linotype",Palatino,"Times New Roman",serif;
  --font-b:"IBM Plex Sans","Segoe UI","PingFang TC","Noto Sans TC",sans-serif;
  --font-m:"IBM Plex Mono",ui-monospace,Menlo,Consolas,monospace;
}
* { box-sizing:border-box; }
body {
  margin:0; color:var(--text); font-family:var(--font-b);
  background:
    radial-gradient(1100px 520px at 8% -8%, #1e3348 0%, transparent 55%),
    radial-gradient(900px 480px at 100% 0%, #2a1818 0%, transparent 48%),
    linear-gradient(180deg, var(--bg0), #0a0e14 120%);
  min-height:100vh; line-height:1.55;
}
.wrap { max-width:1180px; margin:0 auto; padding:2.2rem 1.2rem 4rem; }
.kicker { font-family:var(--font-m); font-size:0.72rem; letter-spacing:0.12em; text-transform:uppercase; color:var(--gold); margin:0 0 0.5rem; }
h1 { font-family:var(--font-d); font-weight:600; font-size:clamp(1.55rem,3.6vw,2.2rem); margin:0 0 0.45rem; }
.sub { color:var(--muted); max-width:50rem; margin:0; }
.meta { display:flex; flex-wrap:wrap; gap:0.55rem 1.1rem; margin-top:0.9rem; font-family:var(--font-m); font-size:0.78rem; color:var(--muted); }
.meta b { color:var(--text); font-weight:500; }
header.hero { border-bottom:1px solid var(--line); padding-bottom:1.3rem; margin-bottom:1.8rem; }
section { margin:2.1rem 0; }
section > h2 { font-family:var(--font-d); font-size:1.28rem; margin:0 0 0.3rem; border-left:3px solid var(--gold); padding-left:0.65rem; }
.section-lead { color:var(--muted); margin:0 0 1rem; max-width:46rem; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:1rem; }
.grid4 { display:grid; grid-template-columns:repeat(4,1fr); gap:0.9rem; }
@media (max-width:900px) { .grid2,.grid4 { grid-template-columns:1fr; } }
.card {
  background:linear-gradient(165deg,var(--bg2),var(--bg1));
  border:1px solid var(--line); border-radius:10px; padding:1rem 1.05rem;
}
.card.good { border-color:#2f6b4f; }
.card.bad { border-color:#6b3a3a; }
.card h3 { margin:0 0 0.35rem; font-size:0.95rem; }
.hero-metric { font-family:var(--font-d); font-size:1.7rem; margin:0.25rem 0; }
.up { color:#e07a7a; } .down { color:var(--teal); } .ok { color:var(--ok); } .no { color:var(--rose); }
.subm { color:var(--muted); font-size:0.8rem; font-family:var(--font-m); }
.case-grid { display:grid; grid-template-columns:1fr 1fr; gap:1rem; }
@media (max-width:850px) { .case-grid { grid-template-columns:1fr; } }
.case { background:var(--bg1); border:1px solid var(--line); border-radius:10px; overflow:hidden; }
.case .hd { padding:0.7rem 0.85rem 0.35rem; display:flex; justify-content:space-between; gap:0.6rem; flex-wrap:wrap; }
.case .hd b { font-size:0.95rem; }
.case .bd { padding:0 0.5rem 0.7rem; }
.pill { display:inline-block; padding:0.12rem 0.45rem; border-radius:999px; border:1px solid var(--line); font-size:0.72rem; color:var(--muted); margin-right:0.25rem; }
.pill.ok { border-color:#2f6b4f; color:var(--ok); }
.pill.no { border-color:#6b3a3a; color:var(--rose); }
table { width:100%; border-collapse:collapse; font-size:0.8rem; }
th,td { padding:0.4rem 0.5rem; border-bottom:1px solid var(--line); text-align:left; }
th { color:var(--muted); font-weight:500; }
td.n, th.n { text-align:right; font-variant-numeric:tabular-nums; font-family:var(--font-m); }
.note { font-size:0.82rem; color:var(--muted); }
.checklist li { margin:0.35rem 0; }
a.link { color:var(--gold); }
"""


def pct(x, digits: int = 1) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x * 100:+.{digits}f}%"


def win_txt(x) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "—"
    return f"{x * 100:.0f}%"


def load_5m(conn: sqlite3.Connection, sid: str, day: str) -> pd.DataFrame:
    # Prefer finmind, then yahoo / any other source with most morning bars.
    q = """
    SELECT minute, open, high, low, close, volume, source
    FROM stock_kbar_5m
    WHERE stock_id=? AND trade_date=?
    ORDER BY CASE source WHEN 'finmind' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END,
             minute
    """
    raw = pd.read_sql_query(q, conn, params=(str(sid), str(day)))
    if raw.empty:
        return raw
    # pick best source by morning bar count
    raw["minute"] = raw["minute"].astype(str)
    best_src = None
    best_n = -1
    for src, g in raw.groupby("source"):
        n = int((g["minute"].str.slice(0, 8) <= "09:30:00").sum())
        if n > best_n:
            best_n = n
            best_src = src
    df = raw[raw["source"] == best_src].drop(columns=["source"]).copy()
    return df


def load_daily(conn: sqlite3.Connection, sid: str, day: str) -> dict | None:
    row = conn.execute(
        """SELECT open, high, low, close FROM stock_daily_bars
           WHERE stock_id=? AND trade_date=? AND source=?""",
        (str(sid), str(day), SOURCE),
    ).fetchone()
    if not row:
        row = conn.execute(
            """SELECT open, high, low, close FROM stock_daily_bars
               WHERE stock_id=? AND trade_date=?
               ORDER BY CASE source WHEN 'finmind' THEN 0 ELSE 1 END
               LIMIT 1""",
            (str(sid), str(day)),
        ).fetchone()
    if not row:
        return None
    return {"o": float(row[0]), "h": float(row[1]), "l": float(row[2]), "c": float(row[3])}


def morph_30m(kb: pd.DataFrame, open_px: float) -> dict | None:
    if kb is None or kb.empty or not open_px or open_px <= 0:
        return None
    m = kb[kb["minute"].str.slice(0, 8) <= "09:30:00"].copy()
    if len(m) < 2:
        return None
    o = float(open_px)
    hi = float(m["high"].max())
    lo = float(m["low"].min())
    c30 = float(m.iloc[-1]["close"])
    t_hi = int(m["high"].values.argmax() * 5)
    t_lo = int(m["low"].values.argmin() * 5)
    if t_hi < t_lo:
        path = "開高走低"
    elif t_lo < t_hi:
        path = "開低走高"
    else:
        path = "平盤"
    dipped = lo < o * 0.998
    popped = hi > o * 1.01
    rng = hi - lo
    close_pos30 = (c30 - lo) / rng if rng > 1e-9 else 0.5
    series = []
    for _, r in m.iterrows():
        series.append(
            {
                "t": str(r["minute"])[:5],
                "o": float(r["open"]) / o - 1,
                "h": float(r["high"]) / o - 1,
                "l": float(r["low"]) / o - 1,
                "c": float(r["close"]) / o - 1,
            }
        )
    return {
        "path30": path,
        "ret30": c30 / o - 1.0,
        "max_up30": hi / o - 1,
        "max_dn30": lo / o - 1,
        "t_hi": t_hi,
        "t_lo": t_lo,
        "dipped": bool(dipped),
        "reclaim": bool(dipped and c30 >= o),
        "popped": bool(popped),
        "fail_break": bool(popped and c30 < o),
        "close_pos30": float(close_pos30),
        "locked30": bool((rng / o) < 0.005),
        "range30": rng / o,
        "series": series,
    }


def build_rows(conn: sqlite3.Connection, episodes_path: Path, ret_key: str) -> tuple[pd.DataFrame, dict]:
    data = json.loads(episodes_path.read_text())
    rows = []
    for ep in data["episodes"]:
        sid = str(ep["stock_id"])
        name = ep.get("stock_name") or ""
        L0 = ep.get("whale_day") or ep.get("signal_date")
        T1 = ep.get("protocol_entry")
        ret = ep.get("returns", {}).get(ret_key)
        if ret is None or T1 is None or L0 is None:
            continue
        d0 = load_daily(conn, sid, L0)
        d1 = load_daily(conn, sid, T1)
        if not d0 or not d1:
            continue
        kb = load_5m(conn, sid, T1)
        mo = morph_30m(kb, d1["o"]) if not kb.empty else None
        row = {
            "sid": sid,
            "name": name,
            "L0": L0,
            "T1": T1,
            "ret": float(ret),
            "buy_amt": float(ep.get("buy_amt_5d") or 0),
            "net_r": float(ep.get("net_ratio") or np.nan),
            "L0h": d0["h"],
            "L0c": d0["c"],
            "T1o": d1["o"],
            "T1c": d1["c"],
            "gap": d1["o"] / d0["c"] - 1,
            "ovh": d1["o"] / d0["h"] - 1,
            "has_kb": mo is not None,
        }
        if mo:
            for k, v in mo.items():
                row[k] = v
        rows.append(row)
    return pd.DataFrame(rows), data.get("meta", {})


def summarize(df: pd.DataFrame, mask, label: str) -> dict:
    s = df.loc[mask, "ret"].dropna()
    return {
        "label": label,
        "n": int(len(s)),
        "med": float(s.median()) if len(s) else float("nan"),
        "win": float((s > 0).mean()) if len(s) else float("nan"),
    }


def candle_svg(series: list, w: int = 420, h: int = 160, title: str = "") -> str:
    if not series:
        return "<svg></svg>"
    vals = []
    for b in series:
        vals.extend([b["h"], b["l"], b["c"], b["o"]])
    ymin, ymax = min(vals), max(vals)
    pad = max((ymax - ymin) * 0.12, 0.004)
    ymin -= pad
    ymax += pad
    n = len(series)
    left, right, top, bot = 36, 12, 18, 28
    iw, ih = w - left - right, h - top - bot

    def xy(i, v):
        x = left + (i + 0.5) / n * iw
        y = top + (ymax - v) / (ymax - ymin) * ih
        return x, y

    z_y = top + (ymax - 0) / (ymax - ymin) * ih
    parts = [
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" aria-label="{escape(title)}">',
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#141c27" rx="8"/>',
        f'<text x="{left}" y="14" fill="#8fa3bb" font-size="11" font-family="IBM Plex Sans,sans-serif">{escape(title)}</text>',
        f'<line x1="{left}" y1="{z_y:.1f}" x2="{w - right}" y2="{z_y:.1f}" stroke="#2a3a4f" stroke-dasharray="3 3"/>',
        f'<text x="4" y="{z_y + 3:.1f}" fill="#8fa3bb" font-size="9">0</text>',
    ]
    for i, b in enumerate(series):
        x, _ = xy(i, 0)
        y_h = xy(i, b["h"])[1]
        y_l = xy(i, b["l"])[1]
        y_o = xy(i, b["o"])[1]
        y_c = xy(i, b["c"])[1]
        up = b["c"] >= b["o"]
        col = "#e07a7a" if up else "#3ecfb2"
        bw = max(iw / n * 0.55, 3)
        y1, y2 = min(y_o, y_c), max(y_o, y_c)
        if abs(y2 - y1) < 1:
            y2 = y1 + 1
        parts.append(
            f'<line x1="{x:.1f}" y1="{y_h:.1f}" x2="{x:.1f}" y2="{y_l:.1f}" stroke="{col}" stroke-width="1.2"/>'
        )
        parts.append(
            f'<rect x="{x - bw / 2:.1f}" y="{y1:.1f}" width="{bw:.1f}" height="{y2 - y1:.1f}" fill="{col}" opacity="0.9"/>'
        )
        if i % max(1, n // 4) == 0 or i == n - 1:
            parts.append(
                f'<text x="{x:.1f}" y="{h - 8}" fill="#8fa3bb" font-size="9" text-anchor="middle">{escape(b["t"])}</text>'
            )
    x_end, y_end = xy(n - 1, series[-1]["c"])
    parts.append(f'<circle cx="{x_end:.1f}" cy="{y_end:.1f}" r="3.2" fill="#e2b657"/>')
    parts.append("</svg>")
    return "".join(parts)


def schematic_go(w: int = 440, h: int = 200) -> str:
    return f"""
<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">
  <rect width="{w}" height="{h}" rx="10" fill="#121a24"/>
  <text x="16" y="22" fill="#6bcf8e" font-size="13" font-family="IBM Plex Sans,sans-serif">最佳 · 開≤L0高 → 開低走高／站回 → 收區間上方</text>
  <line x1="40" y1="150" x2="400" y2="150" stroke="#2a3a4f"/>
  <text x="40" y="168" fill="#8fa3bb" font-size="10">開盤</text>
  <line x1="40" y1="95" x2="400" y2="95" stroke="#e2b657" stroke-dasharray="4 3" opacity="0.7"/>
  <text x="310" y="90" fill="#e2b657" font-size="10">L0 最高價</text>
  <path d="M50 145 C90 165, 120 170, 160 155 S240 100, 290 85 S360 70, 390 65"
        fill="none" stroke="#6bcf8e" stroke-width="3"/>
  <circle cx="50" cy="145" r="4" fill="#e2b657"/>
  <circle cx="390" cy="65" r="5" fill="#6bcf8e"/>
  <text x="50" y="140" fill="#8fa3bb" font-size="10" text-anchor="middle">09:00</text>
  <text x="390" y="55" fill="#6bcf8e" font-size="10" text-anchor="end">~09:30 收強</text>
  <text x="160" y="185" fill="#8fa3bb" font-size="10">先探低（可買）</text>
  <text x="300" y="185" fill="#8fa3bb" font-size="10">30m 收&gt;開且偏上</text>
</svg>"""


def schematic_bad(w: int = 440, h: int = 200) -> str:
    return f"""
<svg viewBox="0 0 {w} {h}" width="100%" height="{h}">
  <rect width="{w}" height="{h}" rx="10" fill="#1a1214"/>
  <text x="16" y="22" fill="#e07a7a" font-size="13" font-family="IBM Plex Sans,sans-serif">最差 · Fail break：衝高後 30m 跌破開盤</text>
  <line x1="40" y1="110" x2="400" y2="110" stroke="#2a3a4f"/>
  <text x="40" y="128" fill="#8fa3bb" font-size="10">開盤</text>
  <path d="M50 110 C90 70, 130 55, 180 50 S250 80, 300 130 S360 155, 390 160"
        fill="none" stroke="#e07a7a" stroke-width="3"/>
  <circle cx="50" cy="110" r="4" fill="#e2b657"/>
  <circle cx="180" cy="50" r="4" fill="#e07a7a"/>
  <circle cx="390" cy="160" r="5" fill="#e07a7a"/>
  <text x="180" y="42" fill="#e07a7a" font-size="10" text-anchor="middle">假突破</text>
  <text x="390" y="178" fill="#e07a7a" font-size="10" text-anchor="end">09:30 破開盤</text>
  <text x="220" y="190" fill="#8fa3bb" font-size="10">或：30m 收綠且貼區間底部</text>
</svg>"""


def case_cards(frame: pd.DataFrame, kind: str, hold_label: str) -> str:
    bits = []
    for _, r in frame.iterrows():
        series = r.get("series")
        if not isinstance(series, list):
            series = []
        tag = "ok" if kind == "best" else "no"
        label = "最佳樣本" if kind == "best" else "最差樣本"
        ret_cls = "up" if r["ret"] > 0 else "down"
        cp = r.get("close_pos30", float("nan"))
        cp_txt = "—" if (isinstance(cp, float) and np.isnan(cp)) else f"{cp:.0%}"
        bits.append(
            f"""
<article class="case">
  <div class="hd">
    <div>
      <span class="pill {tag}">{label}</span>
      <b>{escape(str(r['sid']))} {escape(str(r['name']))}</b>
      <div class="subm">L0 {escape(str(r['L0']))} → T+1 {escape(str(r['T1']))} · {escape(str(r.get('path30','')))}</div>
    </div>
    <div style="text-align:right">
      <div class="hero-metric {ret_cls}" style="font-size:1.25rem">{pct(r['ret'])}</div>
      <div class="subm">{escape(hold_label)} · 30m {pct(r.get('ret30'))}</div>
    </div>
  </div>
  <div class="bd">
    {candle_svg(series, title=f"T+1 前30分 · gap {pct(r.get('gap'))} · vs L0高 {pct(r.get('ovh'))}")}
    <div class="subm" style="margin-top:0.35rem">
      max↑ {pct(r.get('max_up30'))} · max↓ {pct(r.get('max_dn30'))} ·
      收區間位 {cp_txt} ·
      fail_break={'Y' if r.get('fail_break') else 'N'} · reclaim={'Y' if r.get('reclaim') else 'N'}
    </div>
  </div>
</article>"""
        )
    return "\n".join(bits) if bits else '<p class="note">樣本不足</p>'


def render_html(
    *,
    title: str,
    branch_name: str,
    branch_id: str,
    hold_label: str,
    df: pd.DataFrame,
    meta: dict,
    out_path: Path,
    sister_link: str,
    episodes_link: str,
) -> tuple[int, int, list[dict]]:
    kb = df[df["has_kb"]].copy()
    n_all = len(df)
    n_kb = len(kb)

    go = (kb["ovh"] <= 0) & (kb["ret30"] > 0.01) & (kb["close_pos30"] >= 0.6)
    go2 = (kb["ovh"] <= 0) & (kb["path30"] == "開低走高") & (kb["ret30"] > 0)
    fb = kb["fail_break"] == True  # noqa: E712
    weak = (kb["close_pos30"] <= 0.3) & (kb["ret30"] < 0)
    path_hi = kb["path30"] == "開高走低"
    path_lo = kb["path30"] == "開低走高"

    stats = [
        summarize(kb, kb.index == kb.index, "有5m樣本全體"),
        summarize(kb, path_hi, "開高走低"),
        summarize(kb, path_lo, "開低走高"),
        summarize(kb, go, "GO：開≤L0高 & 30m>+1% & 收區間≥60%"),
        summarize(kb, go2, "GO′：開≤L0高 & 開低走高 & 30m>開"),
        summarize(kb, fb, "NO：Fail break（衝高後破開）"),
        summarize(kb, weak, "NO：弱勢收（綠+貼底）"),
        summarize(kb, ~fb, "非 Fail break"),
    ]

    best = kb.loc[go].sort_values("ret", ascending=False).head(4)
    if len(best) < 2:
        best = kb.loc[go2].sort_values("ret", ascending=False).head(4)
    worst = kb.loc[fb].sort_values("ret", ascending=True).head(4)
    if len(worst) < 2:
        worst = kb.loc[weak].sort_values("ret", ascending=True).head(4)

    path_rows = []
    for s in stats:
        path_rows.append(
            "<tr>"
            f"<td>{escape(s['label'])}</td>"
            f"<td class='n'>{s['n']}</td>"
            f"<td class='n'>{pct(s['med'])}</td>"
            f"<td class='n'>{win_txt(s['win'])}</td>"
            "</tr>"
        )

    s0, s3, s5 = stats[0], stats[3], stats[5]
    hi_share = float(path_hi.mean() * 100) if n_kb else 0.0
    lo_share = float(path_lo.mean() * 100) if n_kb else 0.0
    meta_snip = {k: meta.get(k) for k in list(meta)[:6]}

    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="hero">
  <p class="kicker">Research · T+1 · First 30 minutes</p>
  <h1>{escape(branch_name)}（{escape(branch_id)}）· 開盤後 30 分鐘圖形</h1>
  <p class="sub">用 5 分K 還原進場日（T+1）前 30 分鐘路徑，對照 {escape(hold_label)}（真實報酬−30bps）。示意圖＋真實案例；樣本僅含有盤中K棒者。</p>
  <div class="meta">
    <span>總 episodes <b>{n_all}</b></span>
    <span>有 5m <b>{n_kb}</b></span>
    <span>持有 <b>{escape(hold_label)}</b></span>
    <span>姊妹頁 <b><a class="link" href="{escape(sister_link)}">{escape(sister_link)}</a></b></span>
    <span>圖廊 <b><a class="link" href="{escape(episodes_link)}">{escape(episodes_link)}</a></b></span>
  </div>
</header>

<section>
  <h2>一、一眼看懂：最佳 vs 最差</h2>
  <p class="section-lead">路徑標籤（開高走低／開低走高）alone 不夠；要看「開盤相對 L0 高」＋「30 分鐘收斂位置」。</p>
  <div class="grid2">
    <article class="card good">
      <h3 class="ok">最佳表現圖形</h3>
      {schematic_go()}
      <p class="subm" style="margin-top:0.6rem">條件：開盤 ≤ L0 最高價；30m 收 &gt;+1% 且收在 30m 高低區間偏上（≥60%）。或：開低走高且 30m 收在開盤上方。</p>
    </article>
    <article class="card bad">
      <h3 class="no">最差表現圖形</h3>
      {schematic_bad()}
      <p class="subm" style="margin-top:0.6rem">條件：先衝高約 +1% 以上，但 30m 收跌破開盤（Fail break）；或 30m 收綠且貼在區間底部（≤30%）。</p>
    </article>
  </div>
</section>

<section>
  <h2>二、數字對照（有 5m 子集）</h2>
  <div class="grid4">
    <article class="card"><h3>有5m全體中位</h3><p class="hero-metric {'up' if s0['med']>0 else 'down'}">{pct(s0['med'])}</p><p class="subm">勝率 {win_txt(s0['win'])} · n={s0['n']}</p></article>
    <article class="card good"><h3>GO 組合</h3><p class="hero-metric ok">{pct(s3['med'])}</p><p class="subm">勝率 {win_txt(s3['win'])} · n={s3['n']}</p></article>
    <article class="card bad"><h3>Fail break</h3><p class="hero-metric no">{pct(s5['med'])}</p><p class="subm">勝率 {win_txt(s5['win'])} · n={s5['n']}</p></article>
    <article class="card"><h3>開低走高占比</h3><p class="hero-metric">{lo_share:.0f}%</p><p class="subm">開高走低 {hi_share:.0f}%</p></article>
  </div>
  <div class="card" style="margin-top:1rem; overflow:auto">
    <table>
      <thead><tr><th>分組</th><th class="n">n</th><th class="n">{escape(hold_label)} 中位</th><th class="n">勝率</th></tr></thead>
      <tbody>{''.join(path_rows)}</tbody>
    </table>
  </div>
  <p class="note">注意：無 5m 的舊事件未納入路徑統計；鎖死漲停幾乎無振幅，不當作「開高走低」解讀。</p>
</section>

<section>
  <h2>三、真實案例 · 最佳</h2>
  <p class="section-lead">自 GO 組挑 {escape(hold_label)} 最高的幾筆；K 線為相對開盤％。</p>
  <div class="case-grid">{case_cards(best, 'best', hold_label)}</div>
</section>

<section>
  <h2>四、真實案例 · 最差（絕對少碰）</h2>
  <p class="section-lead">自 Fail break／弱勢收組挑最差；見圖後應不追或提高停損警覺。</p>
  <div class="case-grid">{case_cards(worst, 'worst', hold_label)}</div>
</section>

<section>
  <h2>五、開盤後該怎麼看</h2>
  <div class="grid2">
    <article class="card">
      <h3>時間窗</h3>
      <ul class="checklist">
        <li><b>0–5 分</b>：開盤是否已遠高於 L0 高／大跳空 → 直接不追</li>
        <li><b>~15 分</b>：尚早，單看紅綠不夠</li>
        <li><b>~30 分（主決策）</b>：是否 fail break、收在區間哪裡</li>
        <li><b>60 分</b>：弱勢收斂通常不必再等幻想翻臉</li>
      </ul>
    </article>
    <article class="card">
      <h3>口令</h3>
      <ul class="checklist">
        <li class="ok">開≤L0高 → 30m 開低走高／站回 → 收區間上方 → <b>跟／留</b></li>
        <li class="no">衝高回落破開盤 → <b>不跟</b>；已買當警報</li>
        <li class="no">30m 收綠且貼底部 → <b>不跟</b></li>
        <li>開盤已否決（大跳空／開在 L0 高上）→ <b>不必等 30m</b></li>
      </ul>
    </article>
  </div>
</section>

<footer class="note" style="margin-top:2rem; border-top:1px solid var(--line); padding-top:1rem">
  產出自 episodes JSON + stock_kbar_5m。meta：{escape(json.dumps(meta_snip, ensure_ascii=False))}
</footer>
</div>
</body>
</html>"""
    out_path.write_text(html, encoding="utf-8")
    return n_all, n_kb, stats


def main() -> None:
    conn = sqlite3.connect(str(DB))

    x_df, x_meta = build_rows(conn, OUT / "kgi_xinyi_105_episodes.json", "l1h14")
    x_flat = x_df.drop(columns=["series"], errors="ignore")
    x_flat.to_csv(OUT / "kgi_xinyi_t1_30m_morphology.csv", index=False)
    x_n, x_nkb, x_stats = render_html(
        title="凱基信義 · T+1 前30分圖形",
        branch_name="凱基信義",
        branch_id="9216",
        hold_label="L1H14",
        df=x_df,
        meta=x_meta,
        out_path=OUT / "kgi_xinyi_t1_30m_morphology.html",
        sister_link="kgi_songshan_t1_30m_morphology.html",
        episodes_link="kgi_xinyi_105_episodes.html",
    )

    s_df, s_meta = build_rows(conn, OUT / "kgi_songshan_58_episodes.json", "l1h7")
    s_flat = s_df.drop(columns=["series"], errors="ignore")
    s_flat.to_csv(OUT / "kgi_songshan_t1_30m_morphology.csv", index=False)
    s_n, s_nkb, s_stats = render_html(
        title="凱基松山 · T+1 前30分圖形",
        branch_name="凱基松山",
        branch_id="9217",
        hold_label="L1H7",
        df=s_df,
        meta=s_meta,
        out_path=OUT / "kgi_songshan_t1_30m_morphology.html",
        sister_link="kgi_xinyi_t1_30m_morphology.html",
        episodes_link="kgi_songshan_58_episodes.html",
    )

    print(f"Xinyi n={x_n} kb={x_nkb}")
    for s in x_stats:
        print(f"  X {s['label']}: n={s['n']} med={pct(s['med'])} win={win_txt(s['win'])}")
    print(f"Songshan n={s_n} kb={s_nkb}")
    for s in s_stats:
        print(f"  S {s['label']}: n={s['n']} med={pct(s['med'])} win={win_txt(s['win'])}")
    print("wrote", OUT / "kgi_xinyi_t1_30m_morphology.html")
    print("wrote", OUT / "kgi_songshan_t1_30m_morphology.html")


if __name__ == "__main__":
    main()
