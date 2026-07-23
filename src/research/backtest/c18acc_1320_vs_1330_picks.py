"""C18acc · fresh+mono 選股：13:20 vs 13:30（近 N 月日清單）。

對每個交易日，用當日 kbar 覆寫 close 後重算 RRG，套用與
``build_fresh_mono_calendar`` 相同的 fresh+mono_tier2 規則，依 seg_last 排序。
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from research.backtest.rrg_mono_backtest import LOOKBACK
from research.backtest.rrg_mono_intraday_ab import LENGTH, _feat, _fresh_mono, _mono_tier2
from rrg_mono_daily_brief import DEFAULT_ETF_CODES, ScanRow, load_etf_constituent_watchlist
from rrg_rotation import compute_rrg_panel
from rrg_universe_snapshot import _intraday_benchmark_price, kbar_close_at_minute

MINUTE_A = "13:20"
MINUTE_B = "13:30"
TOP_N_SHOW = 10


def _patch_day_prices(
    conn: sqlite3.Connection,
    *,
    close,
    bench,
    trade_date: str,
    minute: str,
    universe: list[str],
) -> tuple[Any, Any, int, int]:
    """Return (prov_close, prov_bench, n_priced, n_universe)."""
    if trade_date not in close.index:
        return close, bench, 0, len(universe)
    prov = close.copy()
    bench_p = bench.reindex(prov.index).astype(float)
    n_priced = 0
    for sid in universe:
        if sid not in prov.columns:
            continue
        px = kbar_close_at_minute(conn, sid, trade_date, minute)
        if px is not None and px > 0:
            prov.at[trade_date, sid] = float(px)
            n_priced += 1
    bpx = _intraday_benchmark_price(conn, trade_date, minute, price_mode="kbar")
    if bpx is not None and bpx > 0 and trade_date in bench_p.index:
        bench_p.at[trade_date] = float(bpx)
    return prov, bench_p, n_priced, len(universe)


def _fresh_ranked(
    *,
    rs_ratio,
    rs_mom,
    full_dates: list[str],
    as_of: str,
    universe: list[str],
    name_map: dict[str, str],
    lookback: int = LOOKBACK,
) -> list[dict[str, Any]]:
    if as_of not in full_dates:
        return []
    si = full_dates.index(as_of)
    lb = max(2, int(lookback))
    if si < lb:
        return []
    rows: list[ScanRow] = []
    for sid in universe:
        f = _feat(rs_ratio, rs_mom, full_dates, si, sid, lb=lb)
        if not _mono_tier2(f):
            continue
        if not _fresh_mono(rs_ratio, rs_mom, full_dates, si, sid, lb=lb):
            continue
        rows.append(
            ScanRow(
                stock_id=sid,
                stock_name=name_map.get(sid, ""),
                fresh=True,
                mono=True,
                seg_last=float(f["seg_last"]),
                disp=float(f["disp"]),
                segs=[float(x) for x in f["segs"]],
                quadrants=[q or "?" for q in f["quadrants"]],
                rs_ratio=float(f["rs_ratio"]),
                rs_momentum=float(f["rs_momentum"]),
                daily_pct=None,
            )
        )
    rows.sort(key=lambda r: (-r.seg_last, r.stock_id))
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rows, 1):
        out.append(
            {
                "rank": i,
                "stock_id": r.stock_id,
                "stock_name": r.stock_name,
                "seg_last": round(r.seg_last, 6),
                "rs_ratio": round(r.rs_ratio, 4) if r.rs_ratio is not None else None,
                "rs_momentum": round(r.rs_momentum, 4) if r.rs_momentum is not None else None,
                "disp": round(r.disp, 4) if r.disp is not None else None,
            }
        )
    return out


def _ids(rows: list[dict[str, Any]], *, top_n: int | None = None) -> list[str]:
    xs = rows if top_n is None else rows[:top_n]
    return [str(r["stock_id"]) for r in xs]


def run_1320_vs_1330_picks(
    conn: sqlite3.Connection,
    *,
    date_start: str | None = None,
    date_end: str | None = None,
    months: int = 3,
    top_n: int = TOP_N_SHOW,
) -> dict[str, Any]:
    close, _, _ = load_price_panels(conn)
    bench = load_benchmark_close(conn).reindex(close.index)
    full_dates = close.index.astype(str).tolist()
    end = date_end or full_dates[-1]
    if date_start:
        start = date_start
    else:
        end_d = date.fromisoformat(end)
        # ~3 calendar months
        start_d = end_d - timedelta(days=max(28, int(months) * 31))
        start = start_d.isoformat()
        # snap to first available trade date ≥ start
        start = next((d for d in full_dates if d >= start), start)

    trade_dates = [d for d in full_dates if start <= d <= end]
    watch = load_etf_constituent_watchlist(conn, DEFAULT_ETF_CODES)
    name_map = {w["stock_id"]: w.get("stock_name", "") for w in watch}
    universe = [w["stock_id"] for w in watch]

    days: list[dict[str, Any]] = []
    n_diff_set = 0
    n_diff_top3 = 0
    n_diff_topn = 0
    n_skip = 0

    for i, d in enumerate(trade_dates, 1):
        print(f"  [{i}/{len(trade_dates)}] {d} …", flush=True)
        day_out: dict[str, Any] = {"trade_date": d, "minutes": {}}
        ranked_by_min: dict[str, list[dict[str, Any]]] = {}
        priced_meta: dict[str, Any] = {}
        ok = True
        for minute in (MINUTE_A, MINUTE_B):
            prov, bench_p, n_priced, n_uni = _patch_day_prices(
                conn,
                close=close,
                bench=bench,
                trade_date=d,
                minute=minute,
                universe=universe,
            )
            priced_meta[minute] = {"n_priced": n_priced, "n_universe": n_uni}
            if n_priced < max(20, int(0.3 * n_uni)):
                ok = False
            rs_r, rs_m, _ = compute_rrg_panel(prov, bench_p, length=LENGTH)
            ranked = _fresh_ranked(
                rs_ratio=rs_r,
                rs_mom=rs_m,
                full_dates=prov.index.astype(str).tolist(),
                as_of=d,
                universe=universe,
                name_map=name_map,
            )
            ranked_by_min[minute] = ranked
            day_out["minutes"][minute] = {
                "n": len(ranked),
                "top": ranked[:top_n],
                "all_ids": _ids(ranked),
                "priced": priced_meta[minute],
            }

        if not ok:
            day_out["status"] = "low_kbar_coverage"
            n_skip += 1
            days.append(day_out)
            continue

        a = ranked_by_min[MINUTE_A]
        b = ranked_by_min[MINUTE_B]
        set_a, set_b = set(_ids(a)), set(_ids(b))
        only_a = sorted(set_a - set_b)
        only_b = sorted(set_b - set_a)
        top3_a, top3_b = _ids(a, top_n=3), _ids(b, top_n=3)
        topn_a, topn_b = _ids(a, top_n=top_n), _ids(b, top_n=top_n)
        rank_a = {r["stock_id"]: r["rank"] for r in a}
        rank_b = {r["stock_id"]: r["rank"] for r in b}
        common = sorted(set_a & set_b)
        rank_moves = []
        for sid in common:
            da = rank_a[sid] - rank_b[sid]
            if da != 0:
                rank_moves.append(
                    {
                        "stock_id": sid,
                        "stock_name": name_map.get(sid, ""),
                        "rank_1320": rank_a[sid],
                        "rank_1330": rank_b[sid],
                        "delta_rank": da,  # + = worse at 13:20 vs 13:30
                    }
                )
        rank_moves.sort(key=lambda x: (-abs(x["delta_rank"]), x["stock_id"]))

        day_out["status"] = "ok"
        day_out["diff"] = {
            "set_equal": set_a == set_b,
            "top3_equal": top3_a == top3_b,
            "topn_equal": topn_a == topn_b,
            "only_1320": only_a,
            "only_1330": only_b,
            "top3_1320": top3_a,
            "top3_1330": top3_b,
            "topn_1320": topn_a,
            "topn_1330": topn_b,
            "rank_moves": rank_moves[:20],
            "jaccard": round(len(set_a & set_b) / len(set_a | set_b), 4)
            if (set_a or set_b)
            else 1.0,
        }
        if set_a != set_b:
            n_diff_set += 1
        if top3_a != top3_b:
            n_diff_top3 += 1
        if topn_a != topn_b:
            n_diff_topn += 1
        days.append(day_out)

    n_ok = sum(1 for d in days if d.get("status") == "ok")
    return {
        "schema": "c18acc_1320_vs_1330_picks-v1",
        "date_start": trade_dates[0] if trade_dates else start,
        "date_end": trade_dates[-1] if trade_dates else end,
        "minute_a": MINUTE_A,
        "minute_b": MINUTE_B,
        "pool_rule": "fresh + mono_tier2 · rank by seg_last (same as build_fresh_mono_calendar)",
        "universe": "ETF constituent watchlist",
        "top_n": top_n,
        "summary": {
            "n_trade_days": len(trade_dates),
            "n_ok": n_ok,
            "n_low_coverage": n_skip,
            "n_days_set_diff": n_diff_set,
            "n_days_top3_diff": n_diff_top3,
            "n_days_topn_diff": n_diff_topn,
            "pct_days_set_diff": round(100.0 * n_diff_set / n_ok, 2) if n_ok else None,
            "pct_days_top3_diff": round(100.0 * n_diff_top3 / n_ok, 2) if n_ok else None,
        },
        "days": days,
    }


def render_1320_vs_1330_md(payload: dict[str, Any]) -> str:
    s = payload.get("summary") or {}
    lines = [
        f"# C18acc · {payload.get('minute_a')} vs {payload.get('minute_b')} 選股名單",
        "",
        f"窗口：**{payload.get('date_start')}** .. **{payload.get('date_end')}**",
        f"規則：`{payload.get('pool_rule')}`",
        "",
        "## 摘要",
        "",
        f"- 交易日：**{s.get('n_trade_days')}** · 有效：**{s.get('n_ok')}** · "
        f"kbar 覆蓋不足：**{s.get('n_low_coverage')}**",
        f"- fresh **集合**不同天數：**{s.get('n_days_set_diff')}** "
        f"（{s.get('pct_days_set_diff')}%）",
        f"- **top-3** 不同天數：**{s.get('n_days_top3_diff')}** "
        f"（{s.get('pct_days_top3_diff')}%）",
        f"- **top-{payload.get('top_n')}** 不同天數：**{s.get('n_days_topn_diff')}**",
        "",
        "## 每日名單",
        "",
    ]
    for day in payload.get("days") or []:
        d = day["trade_date"]
        lines.append(f"### {d}")
        if day.get("status") != "ok":
            lines.append(f"- status: **{day.get('status')}**（略）")
            lines.append("")
            continue
        diff = day.get("diff") or {}
        ma = (day.get("minutes") or {}).get(MINUTE_A) or {}
        mb = (day.get("minutes") or {}).get(MINUTE_B) or {}
        flag = "相同" if diff.get("set_equal") else "**有差**"
        lines.append(
            f"- 集合：{flag} · Jaccard={diff.get('jaccard')} · "
            f"top3={'同' if diff.get('top3_equal') else '**異**'}"
        )
        lines.append(
            f"- only@{MINUTE_A}: `{diff.get('only_1320') or []}` · "
            f"only@{MINUTE_B}: `{diff.get('only_1330') or []}`"
        )
        lines.append(f"- top3@{MINUTE_A}: `{diff.get('top3_1320')}`")
        lines.append(f"- top3@{MINUTE_B}: `{diff.get('top3_1330')}`")
        lines.append("")
        lines.append(f"| rank | {MINUTE_A} | seg | {MINUTE_B} | seg |")
        lines.append("|-----:|-----------|----:|-----------|----:|")
        top_a = {r["rank"]: r for r in (ma.get("top") or [])}
        top_b = {r["rank"]: r for r in (mb.get("top") or [])}
        for rk in range(1, int(payload.get("top_n") or TOP_N_SHOW) + 1):
            ra, rb = top_a.get(rk), top_b.get(rk)
            a_s = f"{ra['stock_id']} {ra.get('stock_name') or ''}".strip() if ra else "—"
            b_s = f"{rb['stock_id']} {rb.get('stock_name') or ''}".strip() if rb else "—"
            a_seg = ra.get("seg_last") if ra else "—"
            b_seg = rb.get("seg_last") if rb else "—"
            lines.append(f"| {rk} | {a_s} | {a_seg} | {b_s} | {b_seg} |")
        moves = diff.get("rank_moves") or []
        if moves:
            lines.append("")
            lines.append("名次變動（|Δrank| 最大前幾名）：")
            for m in moves[:8]:
                lines.append(
                    f"- {m['stock_id']} {m.get('stock_name') or ''}: "
                    f"{MINUTE_A}#{m['rank_1320']} → {MINUTE_B}#{m['rank_1330']} "
                    f"(Δ={m['delta_rank']:+d})"
                )
        lines.append("")

    lines.extend(
        [
            "## 解讀",
            "",
            f"若多數交易日 top-3 / 集合幾乎相同，代表用 {MINUTE_A} 近似 {MINUTE_B} 收盤選股"
            "資訊損失很小；差異集中日才值得人工對照。",
            "",
            "模組：`src/research/backtest/c18acc_1320_vs_1330_picks.py`",
            "",
        ]
    )
    return "\n".join(lines)
