#!/usr/bin/env python3
"""CX digest: expert-pool signal replay with K-line charts → real Gmail.

Yahoo does not provide embeddable TA chart images via API; we build candlesticks
from local ``stock_daily_bars`` (finmind), same OHLC Yahoo would use.

  PYTHONPATH=src .venv/bin/python scripts/research/send_expert_pool_chart_digest.py \\
    --dates 2026-07-16,2026-07-17,2026-07-20
  # 排程：最近 1 個交易日、無達標則不寄
  PYTHONPATH=src .venv/bin/python scripts/research/send_expert_pool_chart_digest.py \\
    --lookback-days 1 --skip-if-empty
"""
from __future__ import annotations

import argparse
import html
import importlib.util
import os
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.lines import Line2D

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from notify_email import send_alert  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import connect  # noqa: E402

SOURCE = "finmind"
OUT = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "email_sim"
    / "charts"
)


def _load_watch():
    path = ROOT / "scripts/research/run_expert_pool_watch.py"
    spec = importlib.util.spec_from_file_location("epw_chart", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_chart"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _load_holdings_rrg():
    """Reuse holdings-pulse RRG (WMA20/5/3) renderer — do not reinvent math."""
    path = ROOT / "scripts/order/render_holdings_rrg.py"
    spec = importlib.util.spec_from_file_location("holdings_rrg_digest", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["holdings_rrg_digest"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def load_ohlc(conn, sid: str, end: str, *, n: int = 60):
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close
        FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date<=? AND close>0
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (sid, SOURCE, end, n),
    ).fetchall()
    out = []
    for r in reversed(rows):
        d, o, h, l, c = r
        o = float(o) if o else float(c)
        h = float(h) if h else float(c)
        l = float(l) if l else float(c)
        out.append((str(d), o, h, l, float(c)))
    return out


def _setup_cjk_font() -> None:
    from matplotlib import font_manager

    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in candidates:
        if Path(path).is_file():
            font_manager.fontManager.addfont(path)
            prop = font_manager.FontProperties(fname=path)
            name = prop.get_name()
            matplotlib.rcParams["font.family"] = name
            matplotlib.rcParams["axes.unicode_minus"] = False
            return
    matplotlib.rcParams["axes.unicode_minus"] = False


def render_kline(
    bars: list[tuple],
    *,
    title: str,
    signal_date: str,
    out_path: Path,
) -> Path:
    """Candlestick from local OHLC; mark signal day + SMA5 (light theme)."""
    _setup_cjk_font()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    face = "#ffffff"
    if len(bars) < 5:
        # fallback blank
        fig, ax = plt.subplots(figsize=(8, 3.2), dpi=140)
        fig.patch.set_facecolor(face)
        ax.set_facecolor(face)
        ax.text(0.5, 0.5, "日K不足", ha="center", va="center", color="#1a1b26")
        ax.axis("off")
        fig.savefig(out_path, bbox_inches="tight", facecolor=face)
        plt.close(fig)
        return out_path

    dates = [mdates.datestr2num(d) for d, *_ in bars]
    opens = [b[1] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    closes = [b[4] for b in bars]
    # SMA5 on close
    sma5 = []
    for i in range(len(closes)):
        w = closes[max(0, i - 4) : i + 1]
        sma5.append(sum(w) / len(w))

    fig, ax = plt.subplots(figsize=(9.2, 3.6), dpi=140)
    fig.patch.set_facecolor(face)
    ax.set_facecolor(face)
    width = 0.6
    for i, (x, o, h, l, c) in enumerate(zip(dates, opens, highs, lows, closes)):
        up = c >= o
        color = "#2e7d32" if up else "#c62828"
        ax.plot([x, x], [l, h], color=color, lw=1.0, solid_capstyle="round")
        bottom = min(o, c)
        height = abs(c - o) or (h - l) * 0.02 or 0.01
        ax.add_patch(
            mpatches.Rectangle(
                (x - width / 2, bottom),
                width,
                height,
                facecolor=color,
                edgecolor=color,
                lw=0.5,
                alpha=0.95,
            )
        )

    ax.plot(dates, sma5, color="#1565c0", lw=1.2, alpha=0.9, label="SMA5")
    # signal marker
    if signal_date in {b[0] for b in bars}:
        sx = mdates.datestr2num(signal_date)
        ax.axvline(sx, color="#ef6c00", ls="--", lw=1.2, alpha=0.95)
        # zoom hint: shade last 3 sessions including signal if possible
        idx = next(i for i, b in enumerate(bars) if b[0] == signal_date)
        lo = max(0, idx - 2)
        ax.axvspan(dates[lo], dates[idx], color="#ef6c00", alpha=0.08)

    ax.set_title(title, color="#1a1b26", fontsize=11, pad=8, loc="left")
    ax.tick_params(colors="#565f89", labelsize=8)
    for spine in ax.spines.values():
        spine.set_color("#d0d5de")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=8))
    ax.grid(True, color="#e8eaef", lw=0.6)
    ax.legend(
        handles=[
            Line2D([0], [0], color="#1565c0", lw=1.2, label="SMA5"),
            Line2D([0], [0], color="#ef6c00", ls="--", lw=1.2, label="訊號日"),
        ],
        facecolor="#ffffff",
        edgecolor="#d0d5de",
        labelcolor="#1a1b26",
        fontsize=8,
        loc="upper left",
    )
    fig.autofmt_xdate(rotation=0, ha="center")
    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def collect_fires(m, conn, dates: list[str]):
    registry = m.load_registry_by_sid()
    items = []
    for asof in dates:
        for sid, spec in sorted(m.POOLS.items()):
            yday = m.prev_trading_day(conn, spec.stock_id, asof)
            et = m.core_nets_on(conn, spec, asof)
            ey = m.core_nets_on(conn, spec, yday) if yday else []
            fired = m.evaluate_rules(spec, et, ey, yday=yday)
            email_hits = [f for f in fired if f.get("email")]
            if not email_hits:
                continue
            thin = "單薄" in (spec.origin_note or "")
            tier = m.research_tier(registry.get(sid), thin_flag=thin)
            sig_close = m.close_on(conn, sid, asof)
            closes = m.closes_through(conn, sid, asof, n=5)
            sma5 = (sum(closes) / len(closes)) if len(closes) >= 5 else None
            body = m.format_alert_body(
                spec=spec,
                asof=asof,
                fired=fired,
                tape_note="chart-digest",
                sig_close=sig_close,
                sma5=sma5,
                registry=registry,
                client_facing=True,
                include_trades=False,  # HTML table owns full trade list
            )
            subject = m.signal_subject(spec=spec, asof=asof, registry=registry)
            items.append(
                {
                    "asof": asof,
                    "sid": sid,
                    "name": spec.stock_name,
                    "tier": tier,
                    "subject": subject,
                    "body": body,
                    "hit": email_hits[0],
                }
            )
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "?": 4}
    items.sort(key=lambda x: (x["asof"], order.get(x["tier"], 9), x["sid"]))
    return items


def _compact_summary(body: str) -> str:
    """Keep hard / 規則 / 回測 / 一句話 only — no trade bullets, paths, or tier 備註 (trust note owns that)."""
    keep_starts = (
        "hard=",
        "規則：",
        "規則=",
        "回測：",
        "回測=",
        "一句話",
        "回測摘要：",
        "（尚無回測",
    )
    skip_markers = (
        "另有",
        "完整檔",
        "knowledge/",
        "reports/",
        "scripts/",
        "—— 歷史",
        "備註：",
    )
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(m in s for m in skip_markers):
            continue
        if s.startswith("· "):
            continue
        if any(s.startswith(p) for p in keep_starts):
            out.append(html.escape(s))
    return "<br/>".join(out)


def _entry_numeric_block(body: str) -> str:
    """Rules once + numeric 訊號收 / 限價 / 開盤上限 only."""
    rule_lines = [
        "明日開盤後：",
        "· 追價＝開盤÷訊號收−1；≤3% → 開盤／市價可做",
        "· 追價>3% → 限價掛「訊號收×1.01」（同價最多約3個交易日；未成則棄本次）",
        "· 軟例外：開盤已在 SMA5≤＋8% → 可開盤，不必死等限價",
        "· 不要：限價沒成就隔日開市價追；不要為觸均線而空等",
    ]
    numeric_keys = ("訊號收：", "建議限價：", "追價≤", "SMA5", "軟例外開盤", "（訊號收盤價缺失")
    nums: list[str] = []
    capture = False
    for line in body.splitlines():
        if "進場註記" in line:
            capture = True
            continue
        if capture and line.startswith("——"):
            break
        if not capture:
            continue
        s = line.strip()
        if any(s.startswith(k) for k in numeric_keys):
            nums.append(html.escape(s))
    parts = [html.escape(x) for x in rule_lines] + nums
    return "<br/>".join(parts)


def _full_trade_table(
    sid: str,
    hit_branches: list[dict] | None,
    *,
    filter_fn,
) -> str:
    import json as _json

    muted = "#565f89"
    text = "#1a1b26"
    border = "#e0e3ea"
    led_path = (
        ROOT
        / "reports/research/branch-footprint-screen/expert_pool/knowledge/trades"
        / f"{sid}.json"
    )
    if not led_path.is_file():
        return (
            f"<div style='margin-top:10px;font-size:12px;color:{muted}'>"
            "③ 回測每一筆：尚無歷史交易樣本</div>"
        )
    led = _json.loads(led_path.read_text(encoding="utf-8"))
    all_trades = led.get("trades") or []
    if not all_trades:
        return (
            f"<div style='margin-top:10px;font-size:12px;color:{muted}'>"
            "③ 回測每一筆：尚無歷史交易樣本</div>"
        )
    trades = filter_fn(all_trades, hit_branches)
    title = (
        f"<div style='margin-top:12px;font-size:12px;color:{text}'>"
        f"<strong>③ 回測每一筆（與當日分點交集 · 共 {len(trades)} 筆）</strong>"
    )
    if not trades:
        return (
            f"{title}"
            f"<div style='margin-top:6px;font-size:12px;color:{muted}'>"
            "無與當日分點交集之歷史筆</div></div>"
        )
    rows_html = []
    for t in trades:
        br = "；".join(
            f"{html.escape(b['name'])}[{html.escape(str(b.get('role') or ''))}]"
            for b in (t.get("branches") or [])
        ) or "—"
        rows_html.append(
            "<tr>"
            f"<td style='padding:3px 6px;border-bottom:1px solid {border}'>"
            f"{html.escape(str(t['signal_date']))}</td>"
            f"<td style='padding:3px 6px;border-bottom:1px solid {border}'>"
            f"{html.escape(str(t['entry_date']))}</td>"
            f"<td style='padding:3px 6px;border-bottom:1px solid {border}'>"
            f"{html.escape(str(t['exit_date']))}</td>"
            f"<td style='padding:3px 6px;border-bottom:1px solid {border};"
            f"text-align:right'>{t['r_pct']:+.2f}</td>"
            f"<td style='padding:3px 6px;border-bottom:1px solid {border};"
            f"text-align:right'>{t['r_ix_pct']:+.2f}</td>"
            f"<td style='padding:3px 6px;border-bottom:1px solid {border};"
            f"text-align:right'>{t['r_adj_pct']:+.2f}</td>"
            f"<td style='padding:3px 6px;border-bottom:1px solid {border};"
            f"font-size:11px'>{br}</td>"
            "</tr>"
        )
    return (
        f"{title}"
        "<table style='width:100%;border-collapse:collapse;margin-top:6px;"
        f"font-size:11px;color:{text}'>"
        f"<thead><tr style='color:{muted};text-align:left'>"
        "<th style='padding:3px 6px'>訊號日</th><th style='padding:3px 6px'>進場</th>"
        "<th style='padding:3px 6px'>出場</th><th style='padding:3px 6px'>真實%</th>"
        "<th style='padding:3px 6px'>大盤%</th><th style='padding:3px 6px'>超額%</th>"
        "<th style='padding:3px 6px'>分點</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></div>"
    )


def build_html(
    items: list[dict],
    *,
    rrg_cids: set[str] | None = None,
    filter_fn=None,
) -> str:
    rrg_cids = rrg_cids or set()
    text = "#1a1b26"
    muted = "#565f89"
    border = "#d0d5de"
    page_bg = "#f7f8fa"
    card_bg = "#ffffff"
    blocks = [
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        f"background:{page_bg};color:{text};padding:20px;line-height:1.45'>",
        f"<h1 style='color:{text};font-size:20px;margin:0 0 8px'>專家池達標通知</h1>",
        f"<p style='color:{muted};margin:0 0 16px;font-size:13px'>"
        "以下為專家分點共識主訊號達標摘要：分級、當日觸發席位、進場參考價、"
        "以及與當日分點交集之回測每一筆。含 K 線（訊號日／SMA5）與 RRG 三腿（WMA20／5／3）。"
        "觀測用，非自動下單。"
        "</p>",
    ]
    by_day: dict[str, list] = {}
    for it in items:
        by_day.setdefault(it["asof"], []).append(it)
    for asof, rows in by_day.items():
        blocks.append(
            f"<h2 style='color:{text};font-size:16px;border-bottom:1px solid {border};"
            f"padding-bottom:6px'>{html.escape(asof)} · 達標 {len(rows)} 檔</h2>"
        )
        for it in rows:
            stamp = it["asof"].replace("-", "")
            cid = f"chart_{stamp}_{it['sid']}"
            rrg_cid = f"rrg_{stamp}_{it['sid']}"
            hit = it["hit"]
            hit_branches = hit.get("branches") or []
            branches = "".join(
                f"<li>{html.escape(b['name'])} "
                f"{b['net_amt'] / 1e8:.2f}億</li>"
                for b in hit_branches
            )
            trust = {
                "A": "優先認真跟（專家池厚、超額穩）",
                "B": "值得觀察（共識有、超額仍佳）",
                "C": "可跟但預期超額薄",
                "D": "降權（單薄或單一席位風格）",
            }.get(it["tier"], "尚無分級 · 先當觀察")
            summary = _compact_summary(it["body"])
            entry_html = _entry_numeric_block(it["body"])
            trade_table = _full_trade_table(
                it["sid"], hit_branches, filter_fn=filter_fn
            )
            tier_color = {
                "A": "#2e7d32",
                "B": "#1565c0",
                "C": "#ef6c00",
                "D": "#c62828",
            }.get(it["tier"], "#565f89")
            if rrg_cid in rrg_cids:
                rrg_block = (
                    f"<img src='cid:{rrg_cid}' alt='RRG {it['sid']}' "
                    f"style='max-width:100%;border-radius:6px;border:1px solid {border};"
                    f"display:block;background:#ffffff'/>"
                    f"<p style='font-size:11px;color:{muted};margin:6px 0 12px'>"
                    f"RRG 三腿 WMA20／5／3（訊號日收盤口徑）· "
                    f"X＝RS-Ratio（相對強弱）· Y＝RS-Momentum（相對動能）· "
                    f"基準 100 · 彩線＝尾跡 · 灰虛線＝長→短期限結構</p>"
                )
            else:
                rrg_block = (
                    f"<p style='font-size:11px;color:{muted};margin:6px 0 12px'>"
                    "RRG 三腿：此檔當日缺日線／基準，無法繪製</p>"
                )
            blocks.append(
                f"<div style='margin:18px 0 28px;padding:14px;border:1px solid {border};"
                f"border-radius:10px;background:{card_bg}'>"
                f"<div style='font-size:15px;margin-bottom:4px'>"
                f"<span style='background:{tier_color};color:#ffffff;padding:2px 8px;"
                f"border-radius:6px;font-weight:700;margin-right:8px'>{it['tier']}</span>"
                f"<strong>{html.escape(it['name'])}</strong> "
                f"<span style='color:{muted}'>({it['sid']})</span></div>"
                f"<div style='font-size:12px;color:{muted};margin-bottom:8px'>"
                f"{html.escape(trust)}</div>"
                f"<div style='font-size:12px;color:{text};margin-bottom:10px'>{summary}</div>"
                f"<img src='cid:{cid}' alt='K線 {it['sid']}' "
                f"style='max-width:100%;border-radius:6px;border:1px solid {border};"
                f"background:#ffffff'/>"
                f"<p style='font-size:11px;color:{muted};margin:6px 0 12px'>"
                f"近約 60 日 K 線 · 橘虛線＝訊號日 · 藍線＝SMA5</p>"
                f"{rrg_block}"
                f"<div style='font-size:12px;margin-bottom:10px;color:{text}'>"
                f"<strong>① 當日觸發分點</strong>"
                f"<ul style='margin:4px 0 0 18px'>{branches}</ul></div>"
                f"<div style='font-size:12px;color:{text};margin-bottom:4px'>"
                f"<strong>② 建議怎麼進</strong><br/>{entry_html}</div>"
                f"{trade_table}"
                f"</div>"
            )
    if not items:
        blocks.append("<p>這幾天沒有主訊號達標。</p>")
    blocks.append(
        f"<p style='font-size:11px;color:{muted};margin-top:24px'>"
        "觀測通知 · 非投資建議 · 非自動下單</p></div>"
    )
    return "\n".join(blocks)


def _latest_trading_days(conn, n: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE stock_id='2330' AND source=? AND close>0
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (SOURCE, max(1, n)),
    ).fetchall()
    days = [str(r[0])[:10] for r in rows]
    days.reverse()
    return days


def main() -> int:
    load_project_dotenv()
    ap = argparse.ArgumentParser(description="專家池達標 HTML 圖文 digest")
    ap.add_argument(
        "--dates",
        default="",
        help="逗號分隔 YYYY-MM-DD；空則用 --lookback-days",
    )
    ap.add_argument(
        "--lookback-days",
        type=int,
        default=1,
        help="未指定 --dates 時，取最近 N 個交易日（預設 1）",
    )
    ap.add_argument("--to", default="", help="收件；空則用 notify_email 預設")
    ap.add_argument("--bars", type=int, default=60)
    ap.add_argument(
        "--skip-if-empty",
        action="store_true",
        help="無達標檔時不寄信（排程用）",
    )
    ap.add_argument("--dry-run", action="store_true", help="只產圖／預覽，不寄信")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.dates.strip():
            dates = [d.strip()[:10] for d in args.dates.split(",") if d.strip()]
        else:
            dates = _latest_trading_days(conn, int(args.lookback_days))
        if not dates:
            print("no trading dates resolved", file=sys.stderr)
            return 1

        to_raw = (args.to or os.environ.get("EXPERT_POOL_CHART_TO") or "").strip()
        to = to_raw.replace("＠", "@").strip() or None

        m = _load_watch()
        rrg = _load_holdings_rrg()
        items = collect_fires(m, conn, dates)
        if not items and args.skip_if_empty:
            print(f"no fires in {dates[0]}～{dates[-1]}; skip email")
            return 0

        OUT.mkdir(parents=True, exist_ok=True)
        inline: list[tuple[str, Path]] = []
        rrg_cids: set[str] = set()

        for it in items:
            bars = load_ohlc(conn, it["sid"], it["asof"], n=args.bars)
            stamp = it["asof"].replace("-", "")
            cid = f"chart_{stamp}_{it['sid']}"
            path = OUT / f"{cid}.png"
            title = f"{it['name']}（{it['sid']}）· {it['asof']} 訊號 · 分級 {it['tier']}"
            render_kline(bars, title=title, signal_date=it["asof"], out_path=path)
            inline.append((cid, path))
            print(f"chart {path.name} bars={len(bars)}")

        by_day: dict[str, list] = {}
        for it in items:
            by_day.setdefault(it["asof"], []).append(it)
        for asof, rows in by_day.items():
            sids = [it["sid"] for it in rows]
            names = {it["sid"]: it["name"] for it in rows}
            positions, caliber = rrg.compute_close_rrg_positions_asof(
                conn, sids, asof=asof, names=names
            )
            stamp = asof.replace("-", "")
            for it in rows:
                info = positions.get(it["sid"])
                rrg_cid = f"rrg_{stamp}_{it['sid']}"
                rrg_path = OUT / f"{rrg_cid}.png"
                if not info:
                    print(f"rrg miss {rrg_cid} (no position)")
                    continue
                ok = rrg.save_stock_rrg_png(
                    info,
                    caliber,
                    rrg_path,
                    title=f"{it['name']}（{it['sid']}）· {asof} RRG WMA20/5/3",
                    theme="light",
                )
                if ok:
                    inline.append((rrg_cid, rrg_path))
                    rrg_cids.add(rrg_cid)
                    print(f"rrg {rrg_path.name} panel_last={caliber.get('panel_last')}")
                else:
                    print(f"rrg miss {rrg_cid} (insufficient bars)")

        plain_parts = [
            "專家池達標通知（詳見 HTML 圖文）",
            "觀測用，非自動下單。含 K 線與 RRG 三腿。",
            "",
        ]
        for it in items:
            plain_parts.append(
                f"=== {it['asof']} [{it['tier']}] {it['sid']} {it['name']} ==="
            )
            for line in it["body"].splitlines():
                s = line.strip()
                if not s:
                    continue
                if any(
                    x in s
                    for x in (
                        "knowledge/",
                        "reports/",
                        "scripts/",
                        "另有",
                        "完整檔",
                        "Tape：",
                    )
                ):
                    continue
                plain_parts.append(s)
            plain_parts.append("")
        if not items:
            plain_parts.append("這幾天沒有主訊號達標。")
        plain_parts.append("觀測通知 · 非投資建議 · 非自動下單")
        plain = "\n".join(plain_parts)
        html_body = build_html(
            items,
            rrg_cids=rrg_cids,
            filter_fn=m.filter_trades_by_hit_branches,
        )

        subject = (
            f"[股票研究] 專家池達標通知 · {dates[0]}～{dates[-1]} · "
            f"{len(items)}檔"
        )
        preview = OUT.parent / "chart_digest_preview.html"
        prev = html_body
        for cid, path in inline:
            prev = prev.replace(f"cid:{cid}", path.as_uri())
        preview.write_text(prev, encoding="utf-8")
        print(f"preview {preview}")

        if args.dry_run:
            print(f"dry-run: would send {subject} ({len(items)} items)")
            return 0

        flag = os.environ.get("RUN_EXPERT_POOL_CHART_EMAIL", "1").strip()
        if flag in ("0", "false", "False"):
            print("email skipped: RUN_EXPERT_POOL_CHART_EMAIL=0")
            return 0

        send_alert(
            subject,
            plain,
            html_body=html_body,
            inline_images=inline,
            to=to,
        )
        print(f"sent: {subject}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
