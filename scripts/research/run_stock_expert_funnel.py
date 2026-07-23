#!/usr/bin/env python3
"""單股專家漏斗 P4→P5→P6（股票優先）.

  PYTHONPATH=src python3 scripts/research/run_stock_expert_funnel.py --stock 2327
  PYTHONPATH=src python3 scripts/research/run_stock_expert_funnel.py --stock 2327 --end 2026-07-17

Outputs under reports/research/branch-footprint-screen/expert_pool/{S}/:
  p4_four_gate.json/.md · p5_pool.json/.md · p6_grid.json/.md · funnel_summary.md
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402


def connect_ro(db_path: Path):
    """Read-only SQLite — safe for research funnels (no schema migrate)."""
    import sqlite3

    uri = f"file:{db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=120000")
    return conn

SOURCE = "finmind"
BENCH = "0050"  # legacy load only; P7 excess uses IX0001
BENCH_IX = "IX0001"
BETA = 1.15
P7_RADJ_MED_MIN = 0.02  # +2% median r_adj gate
START_DEFAULT = "2024-07-01"
OOS_DEFAULT = "2026-01-01"
MIN_AMT = 5e7
DEDUP = 5
COST = 0.003
HOLD_BASE = 7
FOREIGN_KW = (
    "美商", "摩根", "瑞銀", "美林", "港商", "野村", "瑞士", "花旗", "渣打",
    "香港", "新加坡", "大和", "法銀", "德意志", "巴克萊", "高盛", "美銀",
)
OUT_ROOT = (
    ROOT / "reports" / "research" / "branch-footprint-screen" / "expert_pool"
)
NAME_FALLBACK = {
    "2327": "國巨",
    "2408": "南亞科",
    "8299": "群聯",
    "2337": "旺宏",
    "3017": "奇鋐",
    "3189": "景碩",
    "3443": "創意",
    "4979": "華星光",
    "6770": "力積電",
    "2344": "華邦電",
    "8046": "南電",
    "8358": "金居",
    "6488": "環球晶",
}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="單股專家漏斗 P4-P6")
    ap.add_argument("--stock", required=True)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--start", default=START_DEFAULT)
    ap.add_argument("--end", default="")
    ap.add_argument("--oos-cut", default=OOS_DEFAULT)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--pool-size", type=int, default=5, help="Max hard passers in P5")
    return ap.parse_args()


def is_foreign(name: str) -> bool:
    return any(k in (name or "") for k in FOREIGN_KW)


def half_key(d: str) -> str:
    return f"{d[:4]}H{'1' if int(d[5:7]) <= 6 else '2'}"


def load_traders(conn) -> dict[str, str]:
    return {
        r[0]: r[1]
        for r in conn.execute(
            """
            SELECT DISTINCT securities_trader_id, securities_trader
            FROM stock_broker_branch_daily WHERE source=?
            """,
            (SOURCE,),
        )
    }


def stock_name(conn, sid: str) -> str:
    if sid in NAME_FALLBACK:
        return NAME_FALLBACK[sid]
    try:
        r = conn.execute(
            "SELECT name FROM stock_beta WHERE stock_id=? LIMIT 1", (sid,)
        ).fetchone()
        if r and r[0]:
            return str(r[0]).rstrip("*")
    except Exception:
        pass
    return sid


def load_bars(conn, sids: list[str], start: str, end: str) -> dict[str, list[tuple]]:
    pad_s = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=40)).strftime(
        "%Y-%m-%d"
    )
    pad_e = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=40)).strftime(
        "%Y-%m-%d"
    )
    out: dict[str, list[tuple]] = defaultdict(list)
    ph = ",".join("?" * len(sids))
    for r in conn.execute(
        f"""
        SELECT stock_id, trade_date, open, close FROM stock_daily_bars
        WHERE source=? AND stock_id IN ({ph}) AND trade_date BETWEEN ? AND ?
        ORDER BY stock_id, trade_date
        """,
        (SOURCE, *sids, pad_s, pad_e),
    ):
        op = float(r[2]) if r[2] else float(r[3] or 0)
        cl = float(r[3] or 0)
        if cl > 0:
            out[r[0]].append((r[1], op, cl))
    return out


def load_ix0001(conn, start: str, end: str) -> list[tuple[str, float, float]]:
    """IX0001 open/close; prefer yahoo → tej → finmind."""
    pad_s = (datetime.strptime(start, "%Y-%m-%d") - timedelta(days=40)).strftime(
        "%Y-%m-%d"
    )
    pad_e = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=40)).strftime(
        "%Y-%m-%d"
    )
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code=? AND date BETWEEN ? AND ? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (BENCH_IX, pad_s, pad_e),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for d, o, c in rows:
        out.setdefault(str(d), (float(o), float(c)))
    return [(d, o, c) for d, (o, c) in sorted(out.items())]


def l1h_pair(
    stock_bars: list[tuple], ix_bars: list[tuple], sig: str, hold: int = HOLD_BASE
) -> tuple[float | None, float | None]:
    """Stock L1H (cost on stock only) and matching IX L1H (no cost)."""
    r_s = l1h(stock_bars, sig, hold)
    if r_s is None:
        return None, None
    r_ix = l1h(ix_bars, sig, hold)
    # undo COST on IX path: l1h always subtracts COST; rebuild bare IX return
    ed, eo = next_open(ix_bars, sig)
    if not ed or not eo:
        return r_s, None
    _xd, xc = exit_close(ix_bars, ed, hold)
    if not xc:
        return r_s, None
    return r_s, xc / eo - 1


def champion_l1h7_radj_median(
    *,
    mode: str,
    floor_amt: float,
    by_date_core: dict[str, set[str]],
    cal: list[str],
    events_amt: dict[tuple[str, str], float],
    tbars: list[tuple],
    ix_bars: list[tuple],
) -> dict:
    """P7 gate: champion signal rule × L1H7 × r_adj = r − β·r_IX; median."""
    if "回看" in mode:
        sigs = build_lookback_signals(by_date_core, cal, floor_amt, events_amt)
    elif "同日" in mode:
        sigs = [
            d
            for d in cal
            if len(
                {
                    tid
                    for tid in by_date_core.get(d, set())
                    if events_amt.get((tid, d), 0) >= floor_amt
                }
            )
            >= 2
        ]
    else:
        sigs = [
            d
            for d in cal
            if any(
                events_amt.get((tid, d), 0) >= floor_amt
                for tid in by_date_core.get(d, set())
            )
        ]
    # 5d dedupe on signal dates (align entry-filter / frozen episode)
    deduped: list[str] = []
    last = None
    for d in sigs:
        if last is None or (
            datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")
        ).days > DEDUP:
            deduped.append(d)
            last = d
    radjs: list[float] = []
    bare: list[float] = []
    for sig in deduped:
        r_s, r_ix = l1h_pair(tbars, ix_bars, sig, HOLD_BASE)
        if r_s is None or r_ix is None:
            continue
        bare.append(r_s)
        radjs.append(r_s - BETA * r_ix)
    return {
        "n": len(radjs),
        "n_sig": len(deduped),
        "med_r_pct": round(median(bare) * 100, 2) if bare else None,
        "med_radj_pct": round(median(radjs) * 100, 2) if radjs else None,
        "pass": bool(radjs) and median(radjs) >= P7_RADJ_MED_MIN,
        "beta": BETA,
        "bench": BENCH_IX,
    }


def next_open(bars: list[tuple], sig: str):
    for d, o, _c in bars:
        if d > sig and o > 0:
            return d, o
    return None, None


def exit_close(bars: list[tuple], entry: str, hold: int):
    ordered = [x for x in bars if x[0] >= entry]
    if len(ordered) < hold:
        return None, None
    d, _o, c = ordered[hold - 1]
    return d, c


def l1h(bars: list[tuple], sig: str, hold: int = HOLD_BASE) -> float | None:
    ed, eo = next_open(bars, sig)
    if not ed:
        return None
    _xd, xc = exit_close(bars, ed, hold)
    if not xc or not eo:
        return None
    return xc / eo - 1 - COST


def trading_dates(bars: list[tuple], start: str, end: str) -> list[str]:
    return [d for d, _o, _c in bars if start <= d <= end]


def simulate_policy(
    signal_dates: list[str],
    bars: list[tuple],
    *,
    hold: int,
    policy: str,
    start: str,
    end: str,
) -> dict:
    """policy: restart | skip | extend. Returns trades + occupy + compound."""
    cal = trading_dates(bars, start, end)
    if not cal:
        return {"trades": [], "occupy": 0, "compound": 0.0, "n": 0}
    cal_i = {d: i for i, d in enumerate(cal)}
    sigs = sorted(d for d in signal_dates if d in cal_i or any(x > d for x in cal))
    # map signal -> entry trading day index
    entries: list[tuple[str, int]] = []  # sig, entry_idx
    for sig in sigs:
        ed, _eo = next_open(bars, sig)
        if not ed or ed not in cal_i:
            continue
        entries.append((sig, cal_i[ed]))

    trades: list[dict] = []
    occupy_days: set[int] = set()
    compound = 1.0

    if policy == "restart":
        last_sig = None
        for sig, ei in entries:
            if last_sig is not None:
                gap = (
                    datetime.strptime(sig, "%Y-%m-%d")
                    - datetime.strptime(last_sig, "%Y-%m-%d")
                ).days
                if gap < DEDUP:
                    continue
            ret = l1h(bars, sig, hold)
            if ret is None:
                continue
            for j in range(hold):
                if ei + j < len(cal):
                    occupy_days.add(ei + j)
            trades.append({"sig": sig, "ret": ret})
            compound *= 1 + ret
            last_sig = sig

    elif policy == "skip":
        busy_until = -1
        last_sig = None
        for sig, ei in entries:
            if ei <= busy_until:
                continue
            if last_sig is not None:
                gap = (
                    datetime.strptime(sig, "%Y-%m-%d")
                    - datetime.strptime(last_sig, "%Y-%m-%d")
                ).days
                if gap < DEDUP:
                    continue
            ret = l1h(bars, sig, hold)
            if ret is None:
                continue
            for j in range(hold):
                if ei + j < len(cal):
                    occupy_days.add(ei + j)
            trades.append({"sig": sig, "ret": ret})
            compound *= 1 + ret
            busy_until = ei + hold - 1
            last_sig = sig

    elif policy == "extend":
        # consecutive signals within hold extend exit; one trade per stretch
        i = 0
        while i < len(entries):
            sig0, ei = entries[i]
            exit_i = ei + hold - 1
            j = i + 1
            while j < len(entries):
                sigj, ej = entries[j]
                if ej <= exit_i:
                    exit_i = max(exit_i, ej + hold - 1)
                    j += 1
                else:
                    break
            ed, eo = next_open(bars, sig0)
            if not ed or not eo:
                i = j
                continue
            # exit at close of cal[min(exit_i, last)]
            exit_i = min(exit_i, len(cal) - 1)
            if exit_i < cal_i.get(ed, 10**9):
                i = j
                continue
            xc = None
            for d, _o, c in bars:
                if d == cal[exit_i]:
                    xc = c
                    break
            if not xc:
                i = j
                continue
            ret = xc / eo - 1 - COST
            for k in range(ei, exit_i + 1):
                occupy_days.add(k)
            trades.append({"sig": sig0, "ret": ret})
            compound *= 1 + ret
            i = j
    else:
        raise ValueError(policy)

    rets = [t["ret"] for t in trades]
    occupy = len(occupy_days)
    comp_pct = (compound - 1) * 100
    eff = (comp_pct / occupy * 100) if occupy else 0.0  # bps/day-ish (pct points / day * 100)
    return {
        "trades": trades,
        "occupy": occupy,
        "compound": comp_pct,
        "n": len(trades),
        "med": median(rets) if rets else None,
        "win": sum(1 for r in rets if r > 0) / len(rets) if rets else None,
        "eff": eff,
    }


def build_lookback_signals(
    by_date_core: dict[str, set[str]],
    dates_sorted: list[str],
    floor: float,
    events_amt: dict[tuple[str, str], float],
) -> list[str]:
    """回看1日：D−1∪D 在場≥2 且今日≥1 家達 floor。"""
    out = []
    idx = {d: i for i, d in enumerate(dates_sorted)}
    for d in dates_sorted:
        today = {
            tid
            for tid in by_date_core.get(d, set())
            if events_amt.get((tid, d), 0) >= floor
        }
        if not today:
            continue
        i = idx[d]
        yday = set()
        if i > 0:
            yd = dates_sorted[i - 1]
            yday = {
                tid
                for tid in by_date_core.get(yd, set())
                if events_amt.get((tid, yd), 0) >= floor
            }
        if len(today | yday) >= 2 and len(today) >= 1:
            out.append(d)
    return out


def champion_score(row: dict) -> float:
    """Higher better. Prefer lookback/skip-extend, penalize thin OOS / restart."""
    mode_w = {"回看1日共識": 3.0, "同日共識": 2.0, "OR": 0.4}.get(row["mode"], 1.0)
    pol_w = 0.2 if "restart" in row["policy"] else 1.3
    oos_n = row.get("oos_n") or 0
    oos_med = row.get("oos_med")
    if oos_n >= 2 and oos_med is not None:
        oos_term = max(oos_med, -5.0) * 0.5 + min(oos_n, 8) * 0.3
    elif oos_n == 1:
        oos_term = -1.0
    else:
        oos_term = -3.0
    med = row.get("med") or 0.0
    eff = row.get("eff") or 0.0
    return mode_w * pol_w * (1 + med / 20) * (1 + min(eff, 250) / 400) + oos_term


def pick_champion(grid_rows: list[dict]) -> dict | None:
    """Skill: 回看1日 > 同日 ≫ OR；skip/extend ≫ restart."""
    if not grid_rows:
        return None
    ranked = sorted(grid_rows, key=lambda x: -x["score"])
    for mode in ("回看1日共識", "同日共識"):
        pref = [
            r
            for r in ranked
            if r["mode"] == mode and "restart" not in r["policy"] and (r.get("n") or 0) >= 3
        ]
        if pref:
            return pref[0]
    pref = [r for r in ranked if "restart" not in r["policy"] and (r.get("n") or 0) >= 3]
    return pref[0] if pref else ranked[0]


def upsert_eval_registry(
    *,
    sid: str,
    sname: str,
    status: str,
    action: str,
    hard_n: int | None,
    thin: bool | None = None,
    notes: str = "",
    out_dir: Path | None = None,
    champion: dict | None = None,
    radj: dict | None = None,
    protocol: str | None = None,
) -> None:
    """Append/update EVAL_REGISTRY so the same stock is not re-funneled blindly."""
    reg_path = OUT_ROOT / "EVAL_REGISTRY.json"
    md_path = OUT_ROOT / "EVAL_REGISTRY.md"
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "schema": "expert_pool_eval_registry-v1",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "Avoid re-running P4–P7 on stocks already evaluated",
        "counts": {},
        "stocks": [],
    }
    if reg_path.exists():
        try:
            payload = json.loads(reg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    by = {str(r.get("stock_id")): r for r in payload.get("stocks", []) if r.get("stock_id")}
    row = by.get(sid, {"stock_id": sid})
    row.update(
        {
            "stock_id": sid,
            "stock_name": sname,
            "status": status,
            "action": action,
            "hard_n": hard_n,
            "thin": thin,
            "notes": notes,
            "artifacts": str(out_dir) if out_dir else str(OUT_ROOT / sid),
            "evaluated_at": datetime.now().isoformat(timespec="seconds"),
            "protocol": protocol
            or (
                f"2024-07-01.. · ≥0.5億 · L1H7 · hard4 · "
                f"r_adj=r−{BETA}×{BENCH_IX} · med≥{P7_RADJ_MED_MIN*100:.0f}%"
            ),
        }
    )
    if champion:
        row["champion_mode"] = champion.get("mode")
        row["champion_floor"] = champion.get("floor")
        row["champion_policy"] = champion.get("policy")
    if radj:
        row["med_radj_pct"] = radj.get("med_radj_pct")
        row["med_r_pct"] = radj.get("med_r_pct")
        row["radj_n"] = radj.get("n")
    by[sid] = row
    stocks = sorted(by.values(), key=lambda r: str(r["stock_id"]))
    from collections import Counter

    payload["stocks"] = stocks
    payload["counts"] = dict(Counter(str(r.get("status")) for r in stocks))
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    reg_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 專家漏斗 · 已檢驗登錄（EVAL_REGISTRY）",
        "",
        f"> 更新：`{payload['updated_at']}` · 避免重複 P4–P7",
        "",
        f"**合計 {len(stocks)} 檔** · "
        + " · ".join(f"{k}={v}" for k, v in sorted(payload["counts"].items())),
        "",
        "| 代號 | 名稱 | 狀態 | hard | r_adj中位 | 備註 |",
        "|---|---|---|---:|---:|---|",
    ]
    for r in stocks:
        radj_s = (
            f"{r['med_radj_pct']:+.2f}%"
            if r.get("med_radj_pct") is not None
            else "—"
        )
        lines.append(
            f"| {r.get('stock_id')} | {r.get('stock_name','')} | {r.get('status')} | "
            f"{r.get('hard_n') if r.get('hard_n') is not None else '—'} | "
            f"{radj_s} | {(r.get('notes') or '')[:50]} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    sid = args.stock.strip()
    end = args.end or date.today().isoformat()
    out_dir = args.out_dir or (OUT_ROOT / sid)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = connect_ro(args.db)
    traders = load_traders(conn)
    sname = stock_name(conn, sid)

    # --- load all domestic large buys for this stock ---
    raw = conn.execute(
        """
        SELECT b.securities_trader_id, b.trade_date, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.stock_id=? AND b.source=? AND b.trade_date BETWEEN ? AND ?
          AND b.net > 0 AND p.close > 0 AND (b.net * p.close) >= ?
        ORDER BY b.securities_trader_id, b.trade_date
        """,
        (SOURCE, sid, SOURCE, args.start, end, MIN_AMT),
    ).fetchall()

    # episodes per tid
    eps_dates: dict[str, list[str]] = defaultdict(list)
    events_amt: dict[tuple[str, str], float] = {}
    for tid, d, net, close in raw:
        if is_foreign(traders.get(tid, "")):
            continue
        amt = float(net) * float(close)
        events_amt[(tid, d)] = amt
        lst = eps_dates[tid]
        if not lst or (
            datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(lst[-1], "%Y-%m-%d")
        ).days > DEDUP:
            lst.append(d)

    # bars: target + all other stocks appearing in ¬S for candidates later
    # First score with target bars only; load more as needed
    bars_map = load_bars(conn, [sid, BENCH], args.start, end)
    tbars = bars_map[sid]
    if len(tbars) < 30:
        print(f"ERROR: insufficient bars for {sid}", file=sys.stderr)
        return 1

    # Precompute L1H7 for each episode
    ret_map: dict[str, list[tuple[str, float, str]]] = {}
    for tid, dates in eps_dates.items():
        out = []
        for d in dates:
            r = l1h(tbars, d, HOLD_BASE)
            if r is not None:
                out.append((d, r, half_key(d)))
        if len(out) >= 8:
            ret_map[tid] = out

    if not ret_map:
        summary = {
            "stock_id": sid,
            "stock_name": sname,
            "hard_n": 0,
            "action": "skip_no_candidates",
        }
        (out_dir / "funnel_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out_dir / "funnel_summary.md").write_text(
            f"# {sid} {sname}\n\n無 n≥8 本土候選 → **暫緩 P7**\n",
            encoding="utf-8",
        )
        upsert_eval_registry(
            sid=sid,
            sname=sname,
            status="skipped_no_candidates",
            action="skip_no_candidates",
            hard_n=0,
            notes="無 n≥8 本土候選",
            out_dir=out_dir,
        )
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    univ = median([median([r for _, r, _ in rs]) for rs in ret_map.values()])

    # ¬S specialty: load all episodes for candidate tids on other stocks (amt threshold)
    cand_tids = list(ret_map.keys())
    ph = ",".join("?" * len(cand_tids))
    other_raw = conn.execute(
        f"""
        SELECT b.securities_trader_id, b.stock_id, b.trade_date, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.securities_trader_id IN ({ph}) AND b.stock_id != ?
          AND b.source=? AND b.trade_date BETWEEN ? AND ?
          AND b.net > 0 AND p.close > 0 AND (b.net * p.close) >= ?
        ORDER BY b.securities_trader_id, b.stock_id, b.trade_date
        """,
        (SOURCE, *cand_tids, sid, SOURCE, args.start, end, MIN_AMT),
    ).fetchall()

    other_eps: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    other_sids: set[str] = set()
    for tid, osid, d, net, close in other_raw:
        other_sids.add(osid)
        lst = other_eps[tid][osid]
        if not lst or (
            datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(lst[-1], "%Y-%m-%d")
        ).days > DEDUP:
            lst.append(d)

    # batch load other bars
    need = [BENCH, sid] + sorted(other_sids)
    # chunk to avoid huge IN
    for i in range(0, len(need), 80):
        chunk = need[i : i + 80]
        more = load_bars(conn, chunk, args.start, end)
        bars_map.update(more)

    tid_other_rets: dict[str, list[float]] = defaultdict(list)
    for tid, by_s in other_eps.items():
        for osid, dates in by_s.items():
            ob = bars_map.get(osid, [])
            for d in dates:
                r = l1h(ob, d, HOLD_BASE)
                if r is not None:
                    tid_other_rets[tid].append(r)

    p4_rows = []
    for tid, rs in ret_map.items():
        rets = [r for _, r, _ in rs]
        med_s = median(rets)
        by_h: dict[str, list[float]] = defaultdict(list)
        for _d, r, h in rs:
            by_h[h].append(r)
        halves = {h: median(v) for h, v in sorted(by_h.items())}
        cross = len(by_h) >= 2 and not all(median(v) <= 0 for v in by_h.values())
        same_pp = (med_s - univ) * 100
        other = tid_other_rets.get(tid, [])
        ns_n = len(other)
        ns_med = median(other) if other else None
        spec_pp = (med_s - ns_med) * 100 if ns_med is not None else None
        g_n = len(rs) >= 8
        g_same = same_pp > 1.0
        g_spec = (spec_pp > 1.0) if ns_n >= 8 else None
        g_cross = cross
        if g_spec is None:
            hard = g_n and g_same and g_cross
        else:
            hard = g_n and g_same and bool(g_spec) and g_cross
        p4_rows.append(
            {
                "tid": tid,
                "name": traders.get(tid, tid),
                "n": len(rs),
                "med_pct": round(med_s * 100, 2),
                "same_pp": round(same_pp, 2),
                "spec_pp": round(spec_pp, 2) if spec_pp is not None else None,
                "ns_n": ns_n,
                "halves": {k: round(v * 100, 1) for k, v in halves.items()},
                "gates": {
                    "n": g_n,
                    "same": g_same,
                    "spec": g_spec,
                    "cross": g_cross,
                },
                "hard": hard,
            }
        )
    p4_rows.sort(key=lambda x: (-x["hard"], -x["same_pp"], -x["med_pct"]))
    hard_rows = [r for r in p4_rows if r["hard"]]
    hard_n = len(hard_rows)

    p4_payload = {
        "protocol": {
            "window": [args.start, end],
            "oos_cut": args.oos_cut,
            "min_amt": MIN_AMT,
            "dedupe": DEDUP,
            "hold": HOLD_BASE,
            "cost_bps": 30,
        },
        "stock_id": sid,
        "stock_name": sname,
        "univ_med_pct": round(univ * 100, 2),
        "candidates": len(p4_rows),
        "hard_n": hard_n,
        "rows": p4_rows,
    }
    (out_dir / "p4_four_gate.json").write_text(
        json.dumps(p4_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        f"# P4 四門 · {sid} {sname}",
        "",
        f"- window `{args.start}`..`{end}` · ≥0.5億 · 去重5日 · L1H7 · hard",
        f"- 候選 {len(p4_rows)} · **hard {hard_n}** · 宇宙中位 {univ*100:.2f}%",
        "",
        "| hard | 分點 | tid | n | 中位% | 同股pp | 專長pp | ¬Sn | 門 |",
        "|------|------|-----|---|-------|--------|--------|-----|----|",
    ]
    for r in p4_rows[:20]:
        g = r["gates"]
        gs = "".join(
            "Y" if g[k] is True else ("N" if g[k] is False else "?")
            for k in ("n", "same", "spec", "cross")
        )
        sp = f"{r['spec_pp']:.1f}" if r["spec_pp"] is not None else "—"
        lines.append(
            f"| {'Y' if r['hard'] else 'N'} | {r['name']} | `{r['tid']}` | {r['n']} | "
            f"{r['med_pct']:+.1f} | {r['same_pp']:+.1f} | {sp} | {r['ns_n']} | {gs} |"
        )
    (out_dir / "p4_four_gate.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    if hard_n == 0:
        summary = {
            "stock_id": sid,
            "stock_name": sname,
            "hard_n": 0,
            "action": "skip_hard0",
            "thin": False,
        }
        (out_dir / "funnel_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (out_dir / "funnel_summary.md").write_text(
            f"# {sid} {sname}\n\n**hard=0 → 暫緩 P7**（不上排程）\n",
            encoding="utf-8",
        )
        upsert_eval_registry(
            sid=sid,
            sname=sname,
            status="skipped_hard0",
            action="skip_hard0",
            hard_n=0,
            notes="P4 hard=0",
            out_dir=out_dir,
        )
        print(json.dumps(summary, ensure_ascii=False))
        return 0

    # --- P5 ---
    pool = hard_rows[: max(1, min(args.pool_size, 5))]
    thin = hard_n == 1
    core = {r["tid"]: r["name"] for r in pool}
    p5 = {
        "stock_id": sid,
        "stock_name": sname,
        "thin": thin,
        "core": core,
        "hard_n": hard_n,
        "note": "單薄" if thin else "hard≥2",
    }
    (out_dir / "p5_pool.json").write_text(
        json.dumps(p5, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    p5_md = [
        f"# P5 監測池 · {sid} {sname}",
        "",
        f"- hard={hard_n}" + (" · **單薄**" if thin else ""),
        f"- 主池：{', '.join(f'{n} `{t}`' for t, n in core.items())}",
        "",
    ]
    (out_dir / "p5_pool.md").write_text("\n".join(p5_md), encoding="utf-8")

    # --- P6 grid ---
    core_ids = list(core.keys())
    # calendar from stock bars in window
    cal = [d for d, _o, _c in tbars if args.start <= d <= end]
    by_date_core: dict[str, set[str]] = defaultdict(set)
    for tid in core_ids:
        for d in eps_dates.get(tid, []):
            if args.start <= d <= end:
                by_date_core[d].add(tid)

    floors = [("≥0.5億", 5e7), ("≥1億", 1e8)]
    modes = ["OR", "同日共識", "回看1日共識"]
    policies = [
        ("H7_restart", 7, "restart"),
        ("H7_skip", 7, "skip"),
        ("H10_restart", 10, "restart"),
        ("H10_skip", 10, "skip"),
        ("H10_extend", 10, "extend"),
        ("H7_extend", 7, "extend"),
    ]

    grid_rows = []
    for floor_label, floor in floors:
        # build signal date lists per mode
        or_dates = []
        same_dates = []
        for d in cal:
            present = {
                tid
                for tid in by_date_core.get(d, set())
                if events_amt.get((tid, d), 0) >= floor
            }
            if present:
                or_dates.append(d)
            if len(present) >= 2:
                same_dates.append(d)
        lb_dates = build_lookback_signals(by_date_core, cal, floor, events_amt)
        mode_sigs = {
            "OR": or_dates,
            "同日共識": same_dates,
            "回看1日共識": lb_dates,
        }
        for mode, sigs in mode_sigs.items():
            for pol_name, hold, pol in policies:
                sim = simulate_policy(
                    sigs, tbars, hold=hold, policy=pol, start=args.start, end=end
                )
                trades = sim["trades"]
                is_t = [t for t in trades if t["sig"] < args.oos_cut]
                oos_t = [t for t in trades if t["sig"] >= args.oos_cut]
                row = {
                    "mode": mode,
                    "floor": floor_label,
                    "policy": pol_name,
                    "n": sim["n"],
                    "med": round(sim["med"] * 100, 2) if sim["med"] is not None else None,
                    "win": round(sim["win"] * 100, 1) if sim["win"] is not None else None,
                    "compound": round(sim["compound"], 1),
                    "occupy": sim["occupy"],
                    "eff": round(sim["eff"], 2),
                    "is_n": len(is_t),
                    "is_med": round(median([t["ret"] for t in is_t]) * 100, 2)
                    if is_t
                    else None,
                    "oos_n": len(oos_t),
                    "oos_med": round(median([t["ret"] for t in oos_t]) * 100, 2)
                    if oos_t
                    else None,
                    "oos_win": round(
                        sum(1 for t in oos_t if t["ret"] > 0) / len(oos_t) * 100, 1
                    )
                    if oos_t
                    else None,
                }
                row["score"] = round(champion_score(row), 3)
                grid_rows.append(row)

    for r in grid_rows:
        r["score"] = round(champion_score(r), 3)
    grid_rows.sort(key=lambda x: -x["score"])
    champ = pick_champion(grid_rows)
    score_leader = grid_rows[0] if grid_rows else None

    # map floor to float for watch
    floor_amt = 5e7 if champ and champ["floor"] == "≥0.5億" else 1e8

    # --- P7 gate: champion signals × L1H7 × r_adj = r − 1.15×IX0001 ---
    ix_bars = load_ix0001(conn, args.start, end)
    radj_gate = (
        champion_l1h7_radj_median(
            mode=champ["mode"],
            floor_amt=floor_amt,
            by_date_core=by_date_core,
            cal=cal,
            events_amt=events_amt,
            tbars=tbars,
            ix_bars=ix_bars,
        )
        if champ
        else {"n": 0, "pass": False, "med_radj_pct": None, "med_r_pct": None}
    )
    gate_ok = bool(champ) and bool(radj_gate.get("pass"))

    p6 = {
        "stock_id": sid,
        "stock_name": sname,
        "core": core,
        "champion": champ,
        "score_leader": score_leader,
        "net_floor": floor_amt,
        "floor_label": champ["floor"] if champ else "≥0.5億",
        "playbook_policy": champ["policy"] if champ else "H10_skip",
        "grid_top": grid_rows[:15],
        "grid_n": len(grid_rows),
        "p7_radj": radj_gate,
        "p7_pass": gate_ok,
    }
    (out_dir / "p6_grid.json").write_text(
        json.dumps(p6, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    md = [
        f"# P6 網格 · {sid} {sname}",
        "",
        f"- 主池：{', '.join(f'{n} `{t}`' for t, n in core.items())}",
        f"- **冠軍**：`{champ['mode']}` · `{champ['floor']}` · `{champ['policy']}`"
        if champ
        else "- 無冠軍",
        f"- **P7 r_adj**（β={BETA}×{BENCH_IX} · L1H7）："
        f"n={radj_gate.get('n')} · med_r={radj_gate.get('med_r_pct')}% · "
        f"med_radj={radj_gate.get('med_radj_pct')}% · "
        f"{'PASS' if gate_ok else 'FAIL'}",
        "",
        "| score | 模式 | 門檻 | 政策 | n | 中位 | 勝 | OOS n/中位 | 複利 | 佔用 | 效率 |",
        "|------|------|------|------|---|------|----|-----------|------|------|------|",
    ]
    for r in grid_rows[:12]:
        oos = (
            f"{r['oos_n']}/{r['oos_med']}"
            if r["oos_med"] is not None
            else f"{r['oos_n']}/—"
        )
        md.append(
            f"| {r['score']:.2f} | {r['mode']} | {r['floor']} | {r['policy']} | {r['n']} | "
            f"{r['med']} | {r['win']} | {oos} | {r['compound']}% | {r['occupy']} | {r['eff']} |"
        )
    (out_dir / "p6_grid.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    # watch registration blob — only when P7 passes
    pol = champ["policy"] if champ else "H10_skip"
    playbook = [
        "1. 進場：見上方「進場註記」· 1 槽（非無腦次日開）",
        f"2. 預設：{pol}（P6 冠軍）",
        f"3. 規則：{champ['mode'] if champ else '回看1日共識'} · {champ['floor'] if champ else '≥0.5億'}",
        "4. observe only · 不下單",
    ]
    if gate_ok:
        action = "register_watch"
        status = "adopted"
        notes = (
            f"P7 pass · med_radj={radj_gate.get('med_radj_pct')}% "
            f"(n={radj_gate.get('n')}) · β={BETA}"
        )
    else:
        action = "retire_excess_lt_2pct"
        status = "retired"
        notes = (
            f"P7 fail · med_radj={radj_gate.get('med_radj_pct')}% "
            f"(n={radj_gate.get('n')}) · need ≥+{P7_RADJ_MED_MIN*100:.0f}% vs β={BETA}×{BENCH_IX}"
        )

    watch_spec = {
        "stock_id": sid,
        "stock_name": sname,
        "core": core,
        "net_floor": floor_amt,
        "floor_label": champ["floor"] if champ else "≥0.5億",
        "playbook": playbook,
        "thin": thin,
        "champion": champ,
        "p7_radj": radj_gate,
        "action": action,
    }
    (out_dir / "watch_spec.json").write_text(
        json.dumps(watch_spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    summary = {
        "stock_id": sid,
        "stock_name": sname,
        "hard_n": hard_n,
        "thin": thin,
        "core": core,
        "champion": champ,
        "p7_radj": radj_gate,
        "p7_pass": gate_ok,
        "action": action,
        "out_dir": str(out_dir),
    }
    (out_dir / "funnel_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out_dir / "funnel_summary.md").write_text(
        f"# Funnel · {sid} {sname}\n\n"
        f"- hard={hard_n}" + (" · **單薄**" if thin else "") + "\n"
        f"- 池：{core}\n"
        f"- 冠軍：{champ}\n"
        f"- P7 r_adj med={radj_gate.get('med_radj_pct')}% (n={radj_gate.get('n')}) · "
        f"β={BETA}×{BENCH_IX}\n"
        f"- 動作：**{'登錄 P7 watch' if gate_ok else '不上排程（超額門檻未過）'}**\n",
        encoding="utf-8",
    )
    upsert_eval_registry(
        sid=sid,
        sname=sname,
        status=status,
        action=action,
        hard_n=hard_n,
        thin=thin,
        notes=notes,
        out_dir=out_dir,
        champion=champ,
        radj=radj_gate,
    )
    print(json.dumps(summary, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
