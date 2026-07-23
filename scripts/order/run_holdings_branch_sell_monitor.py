#!/usr/bin/env python3
"""持倉 · 專家分點淨賣 + 全池分點投票預警（賣方 · 不下單）.

週一至五 20:10（mini launchd）。對富邦 API 持倉：

  1) 有採納專家池 → 該股 core × pool floor（soft/hard）另列一行
  2) 每一檔持倉 → 跨池不重複專家分點面板投票：K/N · 淨賣≥5000萬

主標籤（對齊 H_EXPERT_SELL_OBS_DESIGN · W0_hard）：
  W0_HARD（≥2 家 core HARD）> HARD > SOFT2（≥2 soft+）> SOFT > PANEL

  · W0_HARD = 研究凍結主標；soft／panel 不得升格同級
  · 觀測註記 · 非出場 · hold 參考專家池 L1H7

淺色 HTML 郵件風格對齊 send_expert_pool_chart_digest（手機預設淺色）.

  .venv-fubon/bin/python scripts/order/run_holdings_branch_sell_monitor.py
  .venv-fubon/bin/python scripts/order/run_holdings_branch_sell_monitor.py \\
    --holdings 2327:50,2330:100 --no-fubon --dry-run --no-refresh --write

Observe / email only · 不送 sell intents。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "research"))

from notify_email import send_alert  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

import run_expert_pool_watch as pool  # noqa: E402

SOURCE = pool.SOURCE
ENV_FLAG = "RUN_HOLDINGS_BRANCH_SELL_EMAIL"
REPORT_DIR = (
    ROOT
    / "reports"
    / "order"
    / "snapshots"
    / "holdings_branch_sell"
)
SOFT_FLOOR_MIN = 2.5e7  # 2,500 萬下限
PANEL_SELL_FLOOR = 5e7  # 固定 5000 萬（全檔投票）


@dataclass(frozen=True)
class HoldingRow:
    stock_id: str
    shares: int
    stock_name: str = ""


@dataclass(frozen=True)
class SellHit:
    stock_id: str
    stock_name: str
    shares: int
    tid: str
    branch_name: str
    trade_date: str
    net_shares: float
    net_amt: float
    sell_amt: float
    soft_floor: float
    hard_floor: float
    level: str  # SOFT | HARD
    floor_label: str


@dataclass
class StockMonitor:
    holding: HoldingRow
    stock_name: str
    asof: str | None
    has_pool: bool
    expert_hits: list[SellHit] = field(default_factory=list)
    expert_w0_hard: bool = False  # ≥2 HARD · SSOT primary tag
    expert_soft2: bool = False  # ≥2 soft+ · secondary only
    panel_k: int = 0
    panel_n: int = 0
    panel_hits: list[dict] = field(default_factory=list)  # sell≥50M
    note: str = ""

    @property
    def expert_consensus(self) -> bool:
        """Backward-compat alias · prefer expert_w0_hard for primary badge."""
        return self.expert_w0_hard


def _email_enabled() -> bool:
    if ENV_FLAG in os.environ:
        return os.environ[ENV_FLAG].strip() not in ("0", "false", "False")
    return True


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="持倉專家＋37分點面板淨賣監控（observe / email · 不下單）"
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--asof", default="", help="Tape date YYYY-MM-DD（預設最新）")
    ap.add_argument(
        "--holdings",
        default="",
        help="離線持倉 SID:shares[,…]（搭配 --no-fubon）",
    )
    ap.add_argument("--no-fubon", action="store_true", help="不呼叫富邦 API")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="只印 stdout／寫報告 · 不寄信",
    )
    ap.add_argument("--force-email", action="store_true", help="無預警也寄 digest")
    ap.add_argument("--refresh-days", type=int, default=0, help="FinMind 補 tape 天數")
    ap.add_argument(
        "--no-refresh",
        action="store_true",
        help="不補 tape（預設；20:00 專家池通常已 refresh）",
    )
    ap.add_argument(
        "--write",
        action="store_true",
        help=f"寫入 {REPORT_DIR}/branch_sell_YYYYMMDD.md",
    )
    return ap.parse_args()


def parse_holdings_cli(raw: str) -> list[HoldingRow]:
    out: list[HoldingRow] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"持倉格式須為 SID:shares · 收到 {part!r}")
        sid, qty_s = part.split(":", 1)
        sid = sid.strip()
        shares = int(float(qty_s.strip()))
        if sid and shares > 0:
            out.append(HoldingRow(stock_id=sid, shares=shares))
    return out


def fetch_fubon_holdings() -> list[HoldingRow]:
    from order.fubon_account import _held_qty, account_snapshot
    from order.fubon_session import check_python_version, connect_fubon

    check_python_version()
    session = connect_fubon()
    snap = account_snapshot(session)
    out: list[HoldingRow] = []
    for h in snap.get("holdings") or []:
        sid = str(h.get("stock_no") or "").strip()
        if not sid:
            continue
        qty = int(_held_qty(h))
        if qty <= 0:
            continue
        out.append(HoldingRow(stock_id=sid, shares=qty))
    return out


def build_union_panel(pools: dict[str, pool.PoolSpec]) -> dict[str, str]:
    """All unique expert-pool branch ids across adopted POOLS (≈37)."""
    out: dict[str, str] = {}
    for spec in pools.values():
        for tid, name in (spec.core or {}).items():
            tid_s = str(tid)
            if tid_s not in out:
                out[tid_s] = str(name)
    return out


def soft_hard_floors(spec: pool.PoolSpec) -> tuple[float, float]:
    hard = float(spec.net_floor)
    soft = max(hard * 0.5, SOFT_FLOOR_MIN)
    if soft > hard:
        soft = hard
    return soft, hard


def branch_nets_on(
    conn,
    *,
    stock_id: str,
    trade_date: str,
    branch_map: dict[str, str],
) -> list[dict]:
    """Branches in map with net≠0; amounts use close × net."""
    if not branch_map:
        return []
    close = pool.close_on(conn, stock_id, trade_date)
    if close is None:
        return []
    placeholders = ",".join("?" * len(branch_map))
    rows = conn.execute(
        f"""
        SELECT securities_trader_id, securities_trader, net, buy, sell
        FROM stock_broker_branch_daily
        WHERE stock_id = ? AND trade_date = ? AND source = ?
          AND securities_trader_id IN ({placeholders})
        """,
        (stock_id, trade_date, SOURCE, *branch_map.keys()),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        net = float(r["net"] or 0)
        if net >= 0:
            continue
        tid = str(r["securities_trader_id"])
        net_amt = net * close
        out.append(
            {
                "tid": tid,
                "name": branch_map.get(tid, str(r["securities_trader"] or tid)),
                "net": net,
                "net_amt": net_amt,
                "sell_amt": abs(net_amt),
                "close": close,
                "date": trade_date,
            }
        )
    return sorted(out, key=lambda x: -x["sell_amt"])


def classify_expert_hits(
    holding: HoldingRow,
    spec: pool.PoolSpec,
    events: list[dict],
) -> list[SellHit]:
    soft, hard = soft_hard_floors(spec)
    hits: list[SellHit] = []
    for e in events:
        sell_amt = float(e["sell_amt"])
        if sell_amt < soft:
            continue
        level = "HARD" if sell_amt >= hard else "SOFT"
        hits.append(
            SellHit(
                stock_id=holding.stock_id,
                stock_name=spec.stock_name or holding.stock_name or holding.stock_id,
                shares=holding.shares,
                tid=str(e["tid"]),
                branch_name=str(e["name"]),
                trade_date=str(e["date"]),
                net_shares=float(e["net"]),
                net_amt=float(e["net_amt"]),
                sell_amt=sell_amt,
                soft_floor=soft,
                hard_floor=hard,
                level=level,
                floor_label=spec.floor_label,
            )
        )
    return hits


def fmt_yi(amt: float, *, signed: bool = True) -> str:
    a = abs(amt)
    if a >= 1e8:
        return f"{amt / 1e8:+.2f}億" if signed else f"{a / 1e8:.2f}億"
    return f"{amt / 1e4:+.0f}萬" if signed else f"{a / 1e4:.0f}萬"


def resolve_sid_asof(conn, stock_id: str, explicit: str) -> str | None:
    if explicit:
        return explicit
    row = conn.execute(
        """
        SELECT MAX(trade_date) FROM stock_broker_branch_daily
        WHERE stock_id = ? AND source = ?
        """,
        (stock_id, SOURCE),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def resolve_global_asof(conn, explicit: str, stock_ids: list[str]) -> str | None:
    if explicit:
        return explicit
    best: str | None = None
    for sid in stock_ids:
        d = resolve_sid_asof(conn, sid, "")
        if d and (best is None or d > best):
            best = d
    if best:
        return best
    row = conn.execute(
        "SELECT MAX(trade_date) FROM stock_broker_branch_daily WHERE source = ?",
        (SOURCE,),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def format_summary(
    *,
    asof: str,
    rows: list[StockMonitor],
    panel_n: int,
    tape_note: str,
) -> str:
    n_exp = sum(len(r.expert_hits) for r in rows)
    n_panel = sum(1 for r in rows if r.panel_k > 0)
    lines = [
        f"持倉分點淨賣監控 · {asof}",
        f"generated: {datetime.now().isoformat(timespec='seconds')}",
        f"holdings: {len(rows)} 檔 · 專家預警列 {n_exp} · 面板有票 {n_panel} 檔",
        f"面板：跨池不重複專家分點 N={panel_n} · 淨賣≥{fmt_yi(PANEL_SELL_FLOOR, signed=False)}",
        f"tape: {tape_note}",
        "observe only · 主標 W0_HARD（≥2 HARD）· SOFT2／面板次級 · 非出場 · hold 參考 L1H7",
        "",
    ]
    for r in rows:
        name = r.stock_name or r.holding.stock_id
        sid = r.holding.stock_id
        lines.append(f"{name}（{sid}）持有 {r.holding.shares} 股 · tape {r.asof or '—'}")
        if r.note:
            lines.append(f"  註：{r.note}")
        if r.has_pool:
            tags: list[str] = []
            if r.expert_w0_hard:
                tags.append("W0_HARD")
            elif r.expert_soft2:
                tags.append("SOFT2")
            tag_s = f" · {'/'.join(tags)}" if tags else ""
            if r.expert_hits:
                lines.append(f"  【專家】{len(r.expert_hits)} 家達 soft+{tag_s}")
                for h in r.expert_hits:
                    vs = (
                        f"≥hard {fmt_yi(h.hard_floor, signed=False)}"
                        if h.level == "HARD"
                        else (
                            f"≥soft {fmt_yi(h.soft_floor, signed=False)}"
                            f"（hard {fmt_yi(h.hard_floor, signed=False)}）"
                        )
                    )
                    lines.append(
                        f"    [{h.level}] {h.branch_name}（{h.tid}）"
                        f" 淨賣 {fmt_yi(-h.sell_amt)} · {vs} · pool {h.floor_label}"
                    )
            else:
                lines.append("  【專家】今日無 soft／hard 淨賣")
        else:
            lines.append("  【專家】無採納池")
        lines.append(
            f"  【面板】{r.panel_k}/{r.panel_n} · 淨賣≥"
            f"{fmt_yi(PANEL_SELL_FLOOR, signed=False)}"
        )
        for e in r.panel_hits[:12]:
            lines.append(
                f"    · {e['name']}（{e['tid']}） 淨賣 {fmt_yi(-e['sell_amt'])}"
            )
        if len(r.panel_hits) > 12:
            lines.append(f"    …另 {len(r.panel_hits) - 12} 家")
        lines.append("")

    hold_s = ", ".join(f"{r.holding.stock_id}:{r.holding.shares}" for r in rows) or "(empty)"
    lines.append(f"持倉清單：{hold_s}")
    lines.append("腳本：scripts/order/run_holdings_branch_sell_monitor.py")
    return "\n".join(lines)


def build_html(
    *,
    asof: str,
    rows: list[StockMonitor],
    panel_n: int,
    tape_note: str,
) -> str:
    """Light theme aligned with send_expert_pool_chart_digest (mobile default)."""
    text = "#1a1b26"
    muted = "#565f89"
    border = "#d0d5de"
    page_bg = "#f7f8fa"
    card_bg = "#ffffff"
    hard_c = "#c62828"
    soft_c = "#ef6c00"
    panel_c = "#1565c0"
    ok_c = "#2e7d32"

    blocks = [
        "<div style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        f"background:{page_bg};color:{text};padding:20px;line-height:1.45'>",
        f"<h1 style='color:{text};font-size:20px;margin:0 0 8px'>持倉分點淨賣監控</h1>",
        f"<p style='color:{muted};margin:0 0 16px;font-size:13px'>"
        f"{html.escape(asof)} · 持倉 {len(rows)} 檔 · 面板 N={panel_n} · "
        f"淨賣≥{html.escape(fmt_yi(PANEL_SELL_FLOOR, signed=False))} · "
        f"{html.escape(tape_note)}<br/>"
        "有採納池另列【專家】；主標 W0_HARD（≥2 HARD）＝研究凍結；"
        "SOFT2／面板為次級。觀測用，非出場；面板≠本股專家；hold 參考 L1H7。"
        "</p>",
    ]

    for r in rows:
        name = r.stock_name or r.holding.stock_id
        sid = r.holding.stock_id
        if r.expert_w0_hard:
            badge = ("W0_HARD", hard_c)
        elif any(h.level == "HARD" for h in r.expert_hits):
            badge = ("EXPERT HARD", hard_c)
        elif r.expert_soft2:
            badge = ("SOFT2", soft_c)
        elif r.expert_hits:
            badge = ("EXPERT SOFT", soft_c)
        elif r.panel_k >= 3:
            badge = (f"PANEL {r.panel_k}/{r.panel_n}", panel_c)
        elif r.panel_k >= 1:
            badge = (f"PANEL {r.panel_k}/{r.panel_n}", muted)
        else:
            badge = ("平靜", ok_c)

        body_bits = [
            f"<div style='margin:14px 0 20px;padding:14px;border:1px solid {border};"
            f"border-radius:10px;background:{card_bg}'>",
            f"<div style='font-size:15px;margin-bottom:6px'>"
            f"<span style='background:{badge[1]};color:#ffffff;padding:2px 8px;"
            f"border-radius:6px;font-weight:700;margin-right:8px'>"
            f"{html.escape(badge[0])}</span>"
            f"<strong>{html.escape(name)}</strong> "
            f"<span style='color:{muted}'>({html.escape(sid)}) · "
            f"{r.holding.shares} 股 · tape {html.escape(r.asof or '—')}</span></div>",
        ]
        if r.note:
            body_bits.append(
                f"<div style='font-size:12px;color:{muted};margin-bottom:8px'>"
                f"{html.escape(r.note)}</div>"
            )

        # Expert block
        body_bits.append(
            f"<div style='font-size:13px;margin:10px 0 6px;color:{text}'>"
            f"<strong>① 專家池</strong></div>"
        )
        if not r.has_pool:
            body_bits.append(
                f"<p style='font-size:12px;color:{muted};margin:0 0 8px'>"
                "無採納專家池</p>"
            )
        elif not r.expert_hits:
            body_bits.append(
                f"<p style='font-size:12px;color:{muted};margin:0 0 8px'>"
                "今日無 soft／hard 淨賣</p>"
            )
        else:
            body_bits.append("<ul style='margin:4px 0 10px 18px;font-size:12px'>")
            for h in r.expert_hits:
                col = hard_c if h.level == "HARD" else soft_c
                body_bits.append(
                    f"<li><span style='color:{col};font-weight:700'>"
                    f"[{html.escape(h.level)}]</span> "
                    f"{html.escape(h.branch_name)}（{html.escape(h.tid)}） "
                    f"淨賣 {html.escape(fmt_yi(-h.sell_amt))} · "
                    f"pool {html.escape(h.floor_label)}</li>"
                )
            body_bits.append("</ul>")

        # Panel block
        body_bits.append(
            f"<div style='font-size:13px;margin:10px 0 6px;color:{text}'>"
            f"<strong>② 面板投票</strong> "
            f"<span style='color:{panel_c};font-weight:700'>"
            f"{r.panel_k}/{r.panel_n}</span> "
            f"<span style='color:{muted};font-size:12px'>"
            f"· 淨賣≥{html.escape(fmt_yi(PANEL_SELL_FLOOR, signed=False))} "
            f"· 跨池常客 · ≠本股專家</span></div>"
        )
        if not r.panel_hits:
            body_bits.append(
                f"<p style='font-size:12px;color:{muted};margin:0'>今日面板無票</p>"
            )
        else:
            body_bits.append("<ul style='margin:4px 0 0 18px;font-size:12px'>")
            for e in r.panel_hits[:15]:
                body_bits.append(
                    f"<li>{html.escape(e['name'])}（{html.escape(e['tid'])}） "
                    f"淨賣 {html.escape(fmt_yi(-e['sell_amt']))}</li>"
                )
            if len(r.panel_hits) > 15:
                body_bits.append(
                    f"<li style='color:{muted}'>…另 {len(r.panel_hits) - 15} 家</li>"
                )
            body_bits.append("</ul>")

        body_bits.append("</div>")
        blocks.extend(body_bits)

    blocks.append(
        f"<p style='font-size:11px;color:{muted};margin-top:20px'>"
        "觀測通知 · 非投資建議 · 非自動下單 · "
        "scripts/order/run_holdings_branch_sell_monitor.py</p></div>"
    )
    return "\n".join(blocks)


def subject_line(*, asof: str, rows: list[StockMonitor]) -> str:
    exp_hits = [h for r in rows for h in r.expert_hits]
    n_hard = sum(1 for h in exp_hits if h.level == "HARD")
    n_soft = sum(1 for h in exp_hits if h.level == "SOFT")
    n_w0 = sum(1 for r in rows if r.expert_w0_hard)
    n_soft2 = sum(1 for r in rows if r.expert_soft2 and not r.expert_w0_hard)
    panel_hot = [r for r in rows if r.panel_k > 0]
    max_k = max((r.panel_k for r in rows), default=0)
    panel_n = rows[0].panel_n if rows else 0

    names: list[str] = []
    seen: set[str] = set()
    for r in rows:
        if not r.expert_hits and r.panel_k <= 0:
            continue
        if r.holding.stock_id in seen:
            continue
        seen.add(r.holding.stock_id)
        names.append(r.stock_name or r.holding.stock_id)
        if len(names) >= 3:
            break
    if not names:
        return f"[股票研究][持倉賣方] 無預警 · {asof}"
    name_s = "／".join(names)
    if len(seen) > 3:
        name_s += f"等{len(seen)}檔"
    if n_w0:
        tag = "W0_HARD"
    elif n_hard:
        tag = "HARD"
    elif n_soft2:
        tag = "SOFT2"
    elif n_soft:
        tag = "SOFT"
    else:
        tag = "PANEL"
    return (
        f"[股票研究][持倉賣方][{tag}] {name_s} · "
        f"H{n_hard}/S{n_soft} · 面板max {max_k}/{panel_n} "
        f"({len(panel_hot)}檔) · {asof}"
    )


def write_report(asof: str, body: str, html_body: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"branch_sell_{asof.replace('-', '')}.md"
    path.write_text(
        f"# 持倉分點淨賣監控 · {asof}\n\n```\n{body}\n```\n",
        encoding="utf-8",
    )
    hp = REPORT_DIR / f"branch_sell_{asof.replace('-', '')}.html"
    hp.write_text(html_body, encoding="utf-8")
    return path


def main() -> int:
    load_project_dotenv()
    args = parse_args()

    holdings: list[HoldingRow] = []
    if args.holdings:
        try:
            holdings = parse_holdings_cli(args.holdings)
        except ValueError as exc:
            print(f"錯誤：{exc}", file=sys.stderr)
            return 2
    elif not args.no_fubon:
        try:
            holdings = fetch_fubon_holdings()
        except (FileNotFoundError, ValueError, RuntimeError, ImportError) as exc:
            print(f"錯誤：富邦持倉無法取得 — {exc}", file=sys.stderr)
            print(
                "提示：確認 .venv-fubon、CAFubon／.env 憑證；"
                "或用 --holdings SID:qty --no-fubon 離線測",
                file=sys.stderr,
            )
            return 1
    else:
        print("錯誤：--no-fubon 時須提供 --holdings", file=sys.stderr)
        return 2

    if not holdings:
        msg = "持倉為空 · 無事可監控"
        print(msg)
        if args.force_email and not args.dry_run and _email_enabled():
            send_alert(f"[股票研究][持倉賣方] 空倉 · {datetime.now().date()}", msg)
        return 0

    pools: dict[str, pool.PoolSpec] = dict(pool.POOLS)
    panel = build_union_panel(pools)
    panel_n = len(panel)
    if panel_n == 0:
        print("錯誤：POOLS 無任何 core 分點", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        refresh_n = 0 if args.no_refresh else max(0, int(args.refresh_days))
        tape_bits: list[str] = []
        all_sids = [h.stock_id for h in holdings]
        if refresh_n > 0:
            for sid in all_sids:
                days = list(
                    reversed(
                        list(
                            pool.list_recent_trading_days(
                                conn, stock_id=sid, n=refresh_n
                            )
                        )
                    )
                )
                print(f"[{sid}] refresh tape days={days}")
                counts = pool.refresh_stock_tape(conn, sid, days)
                ok = sum(1 for v in counts.values() if v > 0)
                tape_bits.append(f"{sid}:{ok}/{len(days)}")
            tape_note = "finmind refresh " + ", ".join(tape_bits)
        else:
            tape_note = "no-refresh（沿用 DB／20:00 已補 tape）"

        asof = resolve_global_asof(conn, args.asof.strip(), all_sids)
        if not asof:
            print("錯誤：DB 無 finmind 分點 tape", file=sys.stderr)
            return 1

        rows: list[StockMonitor] = []
        for h in holdings:
            sid_asof = resolve_sid_asof(conn, h.stock_id, args.asof.strip())
            spec = pools.get(h.stock_id)
            name = (spec.stock_name if spec else "") or h.stock_name or h.stock_id
            mon = StockMonitor(
                holding=h,
                stock_name=name,
                asof=sid_asof,
                has_pool=spec is not None,
                panel_n=panel_n,
            )
            if not sid_asof:
                mon.note = "無分點 tape"
                rows.append(mon)
                print(f"[{h.stock_id}] no tape")
                continue

            # Panel vote (every holding)
            panel_events = branch_nets_on(
                conn, stock_id=h.stock_id, trade_date=sid_asof, branch_map=panel
            )
            panel_hits = [
                e for e in panel_events if e["sell_amt"] >= PANEL_SELL_FLOOR
            ]
            mon.panel_hits = panel_hits
            mon.panel_k = len(panel_hits)

            # Expert row
            if spec is not None:
                soft_fl, hard_fl = soft_hard_floors(spec)
                core_events = branch_nets_on(
                    conn,
                    stock_id=h.stock_id,
                    trade_date=sid_asof,
                    branch_map=dict(spec.core),
                )
                hits = classify_expert_hits(h, spec, core_events)
                mon.expert_hits = hits
                n_hard_hits = sum(1 for x in hits if x.level == "HARD")
                n_soft_plus = sum(1 for x in hits if x.sell_amt >= soft_fl)
                mon.expert_w0_hard = n_hard_hits >= 2
                mon.expert_soft2 = n_soft_plus >= 2
                print(
                    f"[{h.stock_id}] asof={sid_asof} shares={h.shares} "
                    f"expert_warn={len(hits)} "
                    f"W0={int(mon.expert_w0_hard)} soft2={int(mon.expert_soft2)} "
                    f"panel={mon.panel_k}/{panel_n}"
                )
                for e in core_events:
                    if e["sell_amt"] < soft_fl:
                        continue
                    mark = "H" if e["sell_amt"] >= hard_fl else "S"
                    print(
                        f"  [E{mark}] {e['name']}({e['tid']}) "
                        f"net_amt={fmt_yi(e['net_amt'])}"
                    )
            else:
                print(
                    f"[{h.stock_id}] asof={sid_asof} shares={h.shares} "
                    f"no-pool panel={mon.panel_k}/{panel_n}"
                )
            for e in panel_hits[:8]:
                print(
                    f"  [P] {e['name']}({e['tid']}) "
                    f"net_amt={fmt_yi(e['net_amt'])}"
                )
            rows.append(mon)

        body = format_summary(
            asof=asof, rows=rows, panel_n=panel_n, tape_note=tape_note
        )
        html_body = build_html(
            asof=asof, rows=rows, panel_n=panel_n, tape_note=tape_note
        )
        print()
        print(body)

        if args.write:
            path = write_report(asof, body, html_body)
            print(f"wrote {path}")
            print(f"wrote {path.with_suffix('.html')}")

        has_signal = any(r.expert_hits or r.panel_k > 0 for r in rows)
        should_mail = (has_signal or args.force_email) and not args.dry_run
        if should_mail:
            if not _email_enabled():
                print(f"email skipped: {ENV_FLAG}=0")
            else:
                subj = subject_line(asof=asof, rows=rows)
                send_alert(subj, body, html_body=html_body)
                print(f"email sent: {subj}")
        elif args.dry_run:
            print("dry-run: email skipped")

        payload = {
            "asof": asof,
            "panel_n": panel_n,
            "panel_floor": PANEL_SELL_FLOOR,
            "holdings": [
                {
                    "stock_id": r.holding.stock_id,
                    "shares": r.holding.shares,
                    "has_pool": r.has_pool,
                    "expert_hits": len(r.expert_hits),
                    "expert_w0_hard": r.expert_w0_hard,
                    "expert_soft2": r.expert_soft2,
                    "expert_consensus": r.expert_consensus,  # alias = w0
                    "panel_k": r.panel_k,
                    "panel_n": r.panel_n,
                }
                for r in rows
            ],
            "expert_warnings": [
                {
                    "stock_id": h.stock_id,
                    "branch": h.branch_name,
                    "tid": h.tid,
                    "sell_amt": h.sell_amt,
                    "level": h.level,
                }
                for r in rows
                for h in r.expert_hits
            ],
        }
        if args.write:
            jp = REPORT_DIR / f"branch_sell_{asof.replace('-', '')}.json"
            jp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"wrote {jp}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
