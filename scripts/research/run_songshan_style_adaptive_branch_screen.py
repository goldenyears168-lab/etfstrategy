#!/usr/bin/env python3
"""Songshan-style adaptive 5d-net95 · L1H7 screen across whale branches.

Frozen protocol (research only · not Order):
  signal: 5d cum buy ≥ adaptive_floor ∩ 5d net_ratio ≥ 0.95 ∩ !mega
  adaptive_floor: per-branch density-match to 凱基松山 IS n under fixed 0.5億
  episode: per (branch, stock) keep first within 5 calendar days
  day pick: among same-day passers keep max buy_5d (Songshan primary)
  return: L1H7 T+1 open → H7 close · cost 30bps on stock only
  excess: r_adj = r_stock − 1.15 × r_IX0001
  primary rank: median r_adj (full window); OOS≥2026-01-01 reported
  note: entry_25m_nonfail is Order-layer only (no full intraday history here)

  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_songshan_style_adaptive_branch_screen.py \\
    --db data/replica/stocks.db --top-branches 400 --top-n 30
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = (
    ROOT
    / "reports/research/branch-footprint-screen"
    / "songshan_style_adaptive_top30"
)
MEGA_JSON = (
    ROOT
    / "reports/research/branch-footprint-screen"
    / "ab58_xMega_copytrade"
    / "mega_blacklist_v1.json"
)

SOURCE = "finmind"
BENCHMARK = "IX0001"
ANCHOR_ID = "9217"
ANCHOR_FLOOR = 0.5e8
NET_MIN = 0.95
COST = 0.003
BETA = 1.15
HOLD = 7
EPISODE_CAL_DAYS = 5
START = "2024-07-01"
OOS_CUT = "2026-01-01"
THIN_N = 8

# Adaptive buy-floor grid (NTD). Covers retail → foreign whale scales.
FLOOR_GRID = (
    0.1e8,
    0.2e8,
    0.3e8,
    0.5e8,
    0.7e8,
    1.0e8,
    1.5e8,
    2.0e8,
    3.0e8,
    5.0e8,
    8.0e8,
    10.0e8,
    15.0e8,
    20.0e8,
    30.0e8,
    50.0e8,
)

# Name heuristics for seat tagging (report only).
FOREIGN_TOKENS = (
    "摩根",
    "高盛",
    "美林",
    "瑞銀",
    "瑞信",
    "野村",
    "大和",
    "花旗",
    "匯豐",
    "瑞士",
    "香港上海",
    "法銀",
    "德商",
    "美商",
    "港商",
    "新加坡商",
    "日商",
    "英商",
    "法商",
)
PROP_TOKENS = ("自營",)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def connect_ro(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-200000")
    return conn


def load_mega() -> set[str]:
    raw = json.loads(MEGA_JSON.read_text(encoding="utf-8"))
    return {str(x) for x in raw["symbols"]}


def seat_tag(name: str) -> str:
    n = name or ""
    if any(t in n for t in PROP_TOKENS):
        return "自營"
    if any(t in n for t in FOREIGN_TOKENS):
        return "外資/外資券商"
    return "營業分點"


def month_slices(start: str, end: str) -> list[tuple[str, str]]:
    cur = pd.Timestamp(start).to_period("M").to_timestamp()
    end_ts = pd.Timestamp(end)
    out: list[tuple[str, str]] = []
    while cur <= end_ts:
        m_end = (cur + pd.offsets.MonthEnd(0)).normalize()
        a = max(cur, pd.Timestamp(start)).strftime("%Y-%m-%d")
        b = min(m_end, end_ts).strftime("%Y-%m-%d")
        out.append((a, b))
        cur = (cur + pd.offsets.MonthBegin(1)).normalize()
    return out


def load_calendar(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT trade_date FROM stock_daily_bars
        WHERE source=? AND stock_id='2330'
          AND trade_date>=? AND trade_date<=?
          AND open>0 AND close>0
        ORDER BY trade_date
        """,
        (SOURCE, start, end),
    ).fetchall()
    return [str(r[0]) for r in rows]


def load_benchmark(
    conn: sqlite3.Connection, start: str, end: str
) -> dict[str, tuple[float, float]]:
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code=? AND date>=? AND date<=? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1
               WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (BENCHMARK, start, end),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for r in rows:
        out.setdefault(str(r[0]), (float(r[1]), float(r[2])))
    if not out:
        raise RuntimeError("no IX0001 bars")
    return out


def load_ohlc(
    conn: sqlite3.Connection, start: str, end: str
) -> dict[tuple[str, str], tuple[float, float]]:
    rows = conn.execute(
        """
        SELECT stock_id, trade_date, open, close
        FROM stock_daily_bars
        WHERE source=? AND trade_date>=? AND trade_date<=?
          AND length(stock_id)=4
          AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND open>0 AND close>0
        """,
        (SOURCE, start, end),
    ).fetchall()
    return {
        (str(r[0]), str(r[1])): (float(r[2]), float(r[3])) for r in rows
    }


def pick_whale_branches(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    top_n: int,
    whale_floor: float,
) -> pd.DataFrame:
    """Top-N branches by priced whale-day hits (buy×close ≥ whale_floor)."""
    log(f"selecting top {top_n} whale branches (floor={whale_floor/1e8:.2f}億) …")
    parts: list[pd.DataFrame] = []
    for a, b in month_slices(start, end):
        df = pd.read_sql_query(
            """
            SELECT b.securities_trader_id AS id,
                   MAX(b.securities_trader) AS name,
                   SUM(CASE WHEN b.buy * p.close >= ? THEN 1 ELSE 0 END) AS whale_hits,
                   MAX(b.buy * p.close) AS max_buy_amt,
                   COUNT(DISTINCT b.trade_date) AS n_days
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p
              ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
            WHERE b.source=?
              AND b.trade_date>=? AND b.trade_date<=?
              AND b.stock_id != '__EMPTY__'
              AND length(b.stock_id)=4
              AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
              AND b.stock_id NOT GLOB '00*'
              AND p.close>0 AND b.buy>0
            GROUP BY b.securities_trader_id
            """,
            conn,
            params=[whale_floor, SOURCE, SOURCE, a, b],
        )
        if not df.empty:
            parts.append(df)
        log(f"  whale chunk {a}..{b} branches={len(df)}")
    if not parts:
        raise RuntimeError("no whale branch rows")
    raw = pd.concat(parts, ignore_index=True)
    agg = (
        raw.groupby("id", as_index=False)
        .agg(
            name=("name", "last"),
            whale_hits=("whale_hits", "sum"),
            max_buy_amt=("max_buy_amt", "max"),
            n_days=("n_days", "sum"),
        )
    )
    agg["seat"] = agg["name"].map(lambda x: seat_tag(str(x)))
    # Prefer 營業分點 (Songshan-like); fill remainder only if short.
    retail = agg[agg["seat"] == "營業分點"].sort_values(
        ["whale_hits", "max_buy_amt", "n_days"], ascending=False
    )
    # Prefer long-coverage seats for fair 2y compare; keep a short-tape reserve.
    retail_long = retail[retail["n_days"] >= 200]
    retail_short = retail[retail["n_days"] < 200]
    other = agg[agg["seat"] != "營業分點"].sort_values(
        ["whale_hits", "max_buy_amt", "n_days"], ascending=False
    )
    top = retail_long.head(top_n).copy()
    if len(top) < top_n:
        need = top_n - len(top)
        top = pd.concat([top, retail_short.head(need)], ignore_index=True)
    if len(top) < top_n:
        need = top_n - len(top)
        top = pd.concat([top, other.head(need)], ignore_index=True)
    # Always keep Songshan anchor.
    if ANCHOR_ID not in set(top["id"].astype(str)):
        row = agg[agg["id"].astype(str) == ANCHOR_ID]
        if len(row):
            top = pd.concat([top, row], ignore_index=True)
    log(
        f"whale universe n={len(top)} retail={int((top.seat=='營業分點').sum())} "
        f"hits_range={int(top.whale_hits.min())}..{int(top.whale_hits.max())}"
    )
    return top


def load_branch_day_amts(
    conn: sqlite3.Connection,
    branch_ids: list[str],
    *,
    start: str,
    end: str,
) -> pd.DataFrame:
    """Load daily buy/sell NTD for branches (month chunks)."""
    if not branch_ids:
        return pd.DataFrame(
            columns=["branch_id", "d", "sid", "buy_amt", "sell_amt"]
        )
    parts: list[pd.DataFrame] = []
    for a, b in month_slices(start, end):
        ph = ",".join("?" * len(branch_ids))
        df = pd.read_sql_query(
            f"""
            SELECT b.securities_trader_id AS branch_id,
                   b.trade_date AS d,
                   b.stock_id AS sid,
                   b.buy * p.close AS buy_amt,
                   b.sell * p.close AS sell_amt
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p
              ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
            WHERE b.source=?
              AND b.securities_trader_id IN ({ph})
              AND b.trade_date>=? AND b.trade_date<=?
              AND b.stock_id != '__EMPTY__'
              AND length(b.stock_id)=4
              AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
              AND b.stock_id NOT GLOB '00*'
              AND p.close>0
              AND (b.buy>0 OR b.sell>0)
            """,
            conn,
            params=[SOURCE, SOURCE, *branch_ids, a, b],
        )
        if not df.empty:
            parts.append(df)
        log(f"  tape {a}..{b} rows={len(df):,}")
    if not parts:
        return pd.DataFrame(
            columns=["branch_id", "d", "sid", "buy_amt", "sell_amt"]
        )
    out = pd.concat(parts, ignore_index=True)
    out["branch_id"] = out["branch_id"].astype(str)
    out["d"] = out["d"].astype(str)
    out["sid"] = out["sid"].astype(str)
    out["buy_amt"] = out["buy_amt"].astype(float)
    out["sell_amt"] = out["sell_amt"].astype(float)
    return out


def build_5d_panel(
    day_amt: pd.DataFrame, calendar: list[str]
) -> pd.DataFrame:
    """Trading-day rolling 5d buy/sell + net ratio per (branch, stock, day).

    Missing sessions in the 5-trading-day window count as 0 (same as Songshan
    last_n_trading_days join). Computed via searchsorted on sparse activity —
    no dense (stock × calendar) materialization.
    """
    cols = ["branch_id", "d", "sid", "buy_5d", "sell_5d", "net_5d"]
    if day_amt.empty:
        return pd.DataFrame(columns=cols)
    cal_idx = {d: i for i, d in enumerate(calendar)}
    day_amt = day_amt[day_amt["d"].isin(cal_idx)]
    if day_amt.empty:
        return pd.DataFrame(columns=cols)
    g = (
        day_amt.groupby(["branch_id", "sid", "d"], as_index=False)[
            ["buy_amt", "sell_amt"]
        ]
        .sum()
    )
    g["di"] = g["d"].map(cal_idx).astype(int)
    g = g.sort_values(["branch_id", "sid", "di"])

    out_bid: list[str] = []
    out_d: list[str] = []
    out_sid: list[str] = []
    out_buy: list[float] = []
    out_sell: list[float] = []
    out_net: list[float] = []

    for (bid, sid), sub in g.groupby(["branch_id", "sid"], sort=False):
        dis = sub["di"].to_numpy(dtype=np.int64)
        buys = sub["buy_amt"].to_numpy(dtype=np.float64)
        sells = sub["sell_amt"].to_numpy(dtype=np.float64)
        for i, di in enumerate(dis):
            if di < 4:
                continue
            lo = int(di) - 4
            j0 = int(np.searchsorted(dis, lo, side="left"))
            buy5 = float(buys[j0 : i + 1].sum())
            if buy5 <= 0:
                continue
            sell5 = float(sells[j0 : i + 1].sum())
            out_bid.append(str(bid))
            out_d.append(calendar[int(di)])
            out_sid.append(str(sid))
            out_buy.append(buy5)
            out_sell.append(sell5)
            out_net.append((buy5 - sell5) / buy5)

    if not out_bid:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(
        {
            "branch_id": out_bid,
            "d": out_d,
            "sid": out_sid,
            "buy_5d": out_buy,
            "sell_5d": out_sell,
            "net_5d": out_net,
        }
    )


def episode_dedupe(events: pd.DataFrame) -> pd.DataFrame:
    """Keep first (branch, stock) within 5 calendar days."""
    if events.empty:
        return events
    events = events.sort_values(["branch_id", "sid", "d", "buy_5d"], ascending=[True, True, True, False])
    kept_rows: list[dict] = []
    last: dict[tuple[str, str], date] = {}
    for r in events.itertuples(index=False):
        key = (str(r.branch_id), str(r.sid))
        d = date.fromisoformat(str(r.d))
        prev = last.get(key)
        if prev is not None and (d - prev).days <= EPISODE_CAL_DAYS:
            continue
        last[key] = d
        kept_rows.append(
            {
                "branch_id": str(r.branch_id),
                "d": str(r.d),
                "sid": str(r.sid),
                "buy_5d": float(r.buy_5d),
                "sell_5d": float(r.sell_5d),
                "net_5d": float(r.net_5d),
            }
        )
    return pd.DataFrame(kept_rows)


def primary_per_day(cands: pd.DataFrame) -> pd.DataFrame:
    """One stock per (branch, day) = max buy_5d (Songshan primary)."""
    if cands.empty:
        return cands
    cands = cands.sort_values(
        ["branch_id", "d", "buy_5d", "sid"],
        ascending=[True, True, False, True],
    )
    return cands.groupby(["branch_id", "d"], as_index=False).head(1)


def filter_signals(
    panel: pd.DataFrame,
    *,
    floor: float,
    mega: set[str],
    net_min: float = NET_MIN,
) -> pd.DataFrame:
    if panel.empty:
        return panel.iloc[0:0].copy()
    m = (
        (panel["buy_5d"] >= floor)
        & (panel["net_5d"] >= net_min)
        & (~panel["sid"].isin(mega))
    )
    cands = panel.loc[m].copy()
    if cands.empty:
        return cands
    prim = primary_per_day(cands)
    return episode_dedupe(prim)


def outcome_legs(
    events: pd.DataFrame,
    *,
    calendar: list[str],
    date_index: dict[str, int],
    ohlc: dict[tuple[str, str], tuple[float, float]],
    benchmark: dict[str, tuple[float, float]],
) -> list[dict]:
    legs: list[dict] = []
    for r in events.itertuples(index=False):
        si = date_index.get(str(r.d))
        if si is None:
            continue
        ei, xi = si + 1, si + HOLD
        if xi >= len(calendar):
            continue
        ed, xd = calendar[ei], calendar[xi]
        er = ohlc.get((str(r.sid), ed))
        xr = ohlc.get((str(r.sid), xd))
        be = benchmark.get(ed)
        bx = benchmark.get(xd)
        if not er or not xr or not be or not bx:
            continue
        stock_r = xr[1] / er[0] - 1.0 - COST
        ix_r = bx[1] / be[0] - 1.0
        radj = stock_r - BETA * ix_r
        legs.append(
            {
                "branch_id": str(r.branch_id),
                "signal_date": str(r.d),
                "entry_date": ed,
                "exit_date": xd,
                "stock_id": str(r.sid),
                "buy_5d": float(r.buy_5d),
                "net_5d": float(r.net_5d),
                "stock_pct": stock_r * 100.0,
                "ix_pct": ix_r * 100.0,
                "radj_pct": radj * 100.0,
            }
        )
    return legs


def summarize(legs: list[dict], oos_cut: str) -> dict:
    def pack(xs: list[dict], prefix: str) -> dict:
        if not xs:
            return {
                f"n_{prefix}": 0,
                f"med_radj_{prefix}": float("nan"),
                f"mean_radj_{prefix}": float("nan"),
                f"win_radj_{prefix}": float("nan"),
                f"med_stock_{prefix}": float("nan"),
            }
        radj = [x["radj_pct"] for x in xs]
        stock = [x["stock_pct"] for x in xs]
        return {
            f"n_{prefix}": len(xs),
            f"med_radj_{prefix}": float(median(radj)),
            f"mean_radj_{prefix}": float(mean(radj)),
            f"win_radj_{prefix}": float(sum(1 for v in radj if v > 0) / len(radj)),
            f"med_stock_{prefix}": float(median(stock)),
        }

    is_legs = [x for x in legs if x["signal_date"] < oos_cut]
    oos_legs = [x for x in legs if x["signal_date"] >= oos_cut]
    out: dict = {}
    out.update(pack(legs, "all"))
    out.update(pack(is_legs, "is"))
    out.update(pack(oos_legs, "oos"))
    ctr = Counter(x["stock_id"] for x in legs)
    out["top3_stocks"] = "|".join(f"{s}:{n}" for s, n in ctr.most_common(3))
    out["uniq_stocks"] = len(ctr)
    out["flag_thin_all"] = int(out["n_all"] < THIN_N)
    out["flag_thin_oos"] = int(out["n_oos"] < THIN_N)
    return out


def pick_adaptive_floor(
    panel: pd.DataFrame,
    *,
    mega: set[str],
    target_is_n: int,
    oos_cut: str,
    net_min: float = NET_MIN,
) -> tuple[float, dict]:
    """Pick floor whose IS episode count is closest to Songshan target."""
    best_f = FLOOR_GRID[0]
    best_dist = math.inf
    best_n = -1
    grid: list[dict] = []
    for f in FLOOR_GRID:
        ev = filter_signals(panel, floor=f, mega=mega, net_min=net_min)
        n_is = int((ev["d"] < oos_cut).sum()) if len(ev) else 0
        dist = abs(n_is - target_is_n)
        grid.append({"floor": f, "n_is": n_is, "dist": dist})
        if dist < best_dist or (dist == best_dist and n_is > best_n):
            best_dist = dist
            best_f = f
            best_n = n_is
    return best_f, {"grid": grid, "picked_floor": best_f, "picked_n_is": best_n}


def fmt_pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return ""
    return f"{x:+.2f}"


def write_report(
    rows: pd.DataFrame,
    *,
    protocol: dict,
    top_n: int,
    out_dir: Path,
    net_min: float,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "adaptive_l1h7_leaderboard.csv"
    json_path = out_dir / "adaptive_l1h7_protocol.json"
    md_path = out_dir / "TOP30.md"
    rows.to_csv(csv_path, index=False)
    json_path.write_text(
        json.dumps(protocol, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    ranked = rows.sort_values(
        ["flag_thin_all", "med_radj_all"], ascending=[True, False]
    ).reset_index(drop=True)
    top = ranked.head(top_n)
    net_tag = f"net{int(round(net_min * 100))}"

    lines = [
        f"# Songshan-style adaptive 5d-{net_tag} · L1H7 · Top{top_n}",
        "",
        f"> Generated {datetime.now().isoformat(timespec='seconds')} · Research only",
        "",
        "## 凍結協議",
        "",
        f"- window: `{protocol['start']}` → `{protocol['end']}`",
        f"- signal: 五日累積買進 ≥ **adaptive_floor** ∩ 五日淨比 ≥ {net_min:.0%} ∩ !mega",
        f"- adaptive: density-match IS n to 凱基松山 `{ANCHOR_ID}` under fixed {ANCHOR_FLOOR/1e8:.1f}億"
        f"（target_is_n={protocol['target_is_n']}）",
        f"- return: L1H{HOLD} · cost {COST*1e4:.0f}bps · β={BETA}×IX0001",
        f"- primary metric: **median r_adj**（full）；OOS≥`{protocol.get('oos_cut', OOS_CUT)}` 附列",
        "- Order-only（未回測）: entry_25m_nonfail · chase_ask · 1張",
        "",
        f"## Top {top_n}（按 full med r_adj；thin n<{THIN_N} 排後）",
        "",
        "| rank | id | name | seat | floor億 | n | med_r_adj% | win% | n_oos | med_oos% | top3 |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, r in enumerate(top.itertuples(index=False), 1):
        lines.append(
            f"| {i} | `{r.id}` | {r.name} | {r.seat} | {r.floor_yi:.2f} | "
            f"{int(r.n_all)} | {fmt_pct(r.med_radj_all)} | "
            f"{'' if pd.isna(r.win_radj_all) else f'{r.win_radj_all:.0%}'} | "
            f"{int(r.n_oos)} | {fmt_pct(r.med_radj_oos)} | {r.top3_stocks} |"
        )

    retail = ranked[ranked["seat"] == "營業分點"].head(top_n)
    lines += [
        "",
        f"## Top {top_n} · 營業分點 only",
        "",
        "| rank | id | name | floor億 | n | med_r_adj% | win% | n_oos | med_oos% | top3 |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, r in enumerate(retail.itertuples(index=False), 1):
        lines.append(
            f"| {i} | `{r.id}` | {r.name} | {r.floor_yi:.2f} | "
            f"{int(r.n_all)} | {fmt_pct(r.med_radj_all)} | "
            f"{'' if pd.isna(r.win_radj_all) else f'{r.win_radj_all:.0%}'} | "
            f"{int(r.n_oos)} | {fmt_pct(r.med_radj_oos)} | {r.top3_stocks} |"
        )

    # Songshan anchor row
    anc = ranked[ranked["id"].astype(str) == ANCHOR_ID]
    lines += ["", "## 錨點 · 凱基松山 9217", ""]
    if len(anc):
        r = anc.iloc[0]
        lines.append(
            f"- floor={r.floor_yi:.2f}億 · n={int(r.n_all)} · "
            f"med_r_adj={fmt_pct(r.med_radj_all)}% · "
            f"oos_n={int(r.n_oos)} · med_oos={fmt_pct(r.med_radj_oos)}% · "
            f"rank_all={int(ranked.index[ranked.id.astype(str)==ANCHOR_ID][0])+1 if ANCHOR_ID in set(ranked.id.astype(str)) else '?'}"
        )
    else:
        lines.append("- missing from scanned universe")

    lines += [
        "",
        "## 檔案",
        "",
        f"- `{csv_path.relative_to(ROOT)}`",
        f"- `{json_path.relative_to(ROOT)}`",
        "",
    ]
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"md": md_path, "csv": csv_path, "json": json_path}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Songshan-style adaptive 5d-net95 L1H7 whale-branch screen"
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=ROOT / "data/replica/stocks.db",
    )
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=None, help="default: max 2330 bar date")
    ap.add_argument("--oos-cut", default=OOS_CUT)
    ap.add_argument("--top-branches", type=int, default=400)
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument(
        "--whale-floor",
        type=float,
        default=0.5e8,
        help="priced buy×close floor to score whale activity",
    )
    ap.add_argument(
        "--batch-size",
        type=int,
        default=40,
        help="branches per tape load batch",
    )
    ap.add_argument(
        "--universe-csv",
        type=Path,
        default=None,
        help="optional precomputed whale universe CSV (id,name,...)",
    )
    ap.add_argument(
        "--net-min",
        type=float,
        default=NET_MIN,
        help="5d net_ratio minimum (default 0.95; use 0.80 for larger sample)",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="report dir (default: songshan_style_adaptive_top30 or _netXX)",
    )
    args = ap.parse_args()
    net_min = float(args.net_min)
    if args.out_dir is not None:
        out_dir = args.out_dir
    elif abs(net_min - NET_MIN) < 1e-9:
        out_dir = OUT
    else:
        out_dir = (
            ROOT
            / "reports/research/branch-footprint-screen"
            / f"songshan_style_adaptive_top30_net{int(round(net_min * 100))}"
        )

    t0 = time.time()
    if not args.db.exists():
        raise SystemExit(f"missing db: {args.db}")

    conn = connect_ro(args.db)
    try:
        if args.end is None:
            row = conn.execute(
                """
                SELECT MAX(trade_date) FROM stock_daily_bars
                WHERE source=? AND stock_id='2330'
                """,
                (SOURCE,),
            ).fetchone()
            end = str(row[0])
        else:
            end = args.end
        # Need hold buffer beyond last signal; calendar includes end.
        cal_end = end
        log(f"db={args.db} window={args.start}..{end}")

        mega = load_mega()
        calendar = load_calendar(conn, args.start, cal_end)
        # Extend calendar for H7 exits after last signal.
        cal_ext = load_calendar(
            conn,
            args.start,
            (
                pd.Timestamp(end) + pd.Timedelta(days=40)
            ).strftime("%Y-%m-%d"),
        )
        if len(cal_ext) > len(calendar):
            calendar = cal_ext
        date_index = {d: i for i, d in enumerate(calendar)}
        log(f"calendar n={len(calendar)} last={calendar[-1]}")

        log("loading benchmark + OHLC …")
        benchmark = load_benchmark(conn, calendar[0], calendar[-1])
        ohlc = load_ohlc(conn, calendar[0], calendar[-1])
        log(f"ohlc keys={len(ohlc):,} bench days={len(benchmark)}")

        if args.universe_csv and args.universe_csv.exists():
            uni = pd.read_csv(args.universe_csv)
            uni["id"] = uni["id"].astype(str)
            if "seat" not in uni.columns:
                uni["seat"] = uni["name"].map(lambda x: seat_tag(str(x)))
            uni = uni.head(args.top_branches)
            log(f"universe from csv n={len(uni)}")
        else:
            uni = pick_whale_branches(
                conn,
                start=args.start,
                end=end,
                top_n=args.top_branches,
                whale_floor=args.whale_floor,
            )
            out_dir.mkdir(parents=True, exist_ok=True)
            uni.to_csv(out_dir / "whale_universe.csv", index=False)

        name_map = dict(zip(uni["id"].astype(str), uni["name"].astype(str)))
        seat_map = dict(zip(uni["id"].astype(str), uni["seat"].astype(str)))
        ids = uni["id"].astype(str).tolist()

        # ---- Pass 1: build 5d panels in batches; cache per-branch panels ----
        panels: dict[str, pd.DataFrame] = {}
        for i in range(0, len(ids), args.batch_size):
            batch = ids[i : i + args.batch_size]
            log(
                f"batch {i//args.batch_size+1}/"
                f"{(len(ids)+args.batch_size-1)//args.batch_size} "
                f"n={len(batch)}"
            )
            day_amt = load_branch_day_amts(
                conn, batch, start=args.start, end=end
            )
            # Restrict panel calendar to signal window (+4 sessions for warm-up).
            warm_start = args.start
            if args.start in date_index and date_index[args.start] >= 4:
                warm_start = calendar[date_index[args.start] - 4]
            cal_sig = [d for d in calendar if warm_start <= d <= end]
            panel = build_5d_panel(day_amt, cal_sig)
            panel = panel[panel["d"] >= args.start]
            for bid, sub in panel.groupby("branch_id"):
                panels[str(bid)] = sub.reset_index(drop=True)
            del day_amt, panel

        if ANCHOR_ID not in panels:
            raise RuntimeError(f"anchor {ANCHOR_ID} panel missing")

        # ---- Anchor target IS n under fixed 0.5億 ----
        anchor_ev = filter_signals(
            panels[ANCHOR_ID], floor=ANCHOR_FLOOR, mega=mega, net_min=net_min
        )
        target_is_n = int((anchor_ev["d"] < args.oos_cut).sum()) if len(anchor_ev) else 0
        log(
            f"anchor 9217 fixed {ANCHOR_FLOOR/1e8:.1f}億 net>={net_min:.0%} → "
            f"IS n={target_is_n} full n={len(anchor_ev)}"
        )

        rows: list[dict] = []
        for j, bid in enumerate(ids, 1):
            panel = panels.get(bid)
            if panel is None or panel.empty:
                rows.append(
                    {
                        "id": bid,
                        "name": name_map.get(bid, bid),
                        "seat": seat_map.get(bid, ""),
                        "floor": float("nan"),
                        "floor_yi": float("nan"),
                        "n_all": 0,
                        "med_radj_all": float("nan"),
                        "mean_radj_all": float("nan"),
                        "win_radj_all": float("nan"),
                        "med_stock_all": float("nan"),
                        "n_is": 0,
                        "med_radj_is": float("nan"),
                        "n_oos": 0,
                        "med_radj_oos": float("nan"),
                        "win_radj_oos": float("nan"),
                        "flag_thin_all": 1,
                        "flag_thin_oos": 1,
                        "top3_stocks": "",
                        "uniq_stocks": 0,
                    }
                )
                continue

            if bid == ANCHOR_ID:
                floor = ANCHOR_FLOOR
                floor_meta = {"picked_floor": floor, "note": "anchor_fixed"}
            else:
                floor, floor_meta = pick_adaptive_floor(
                    panel,
                    mega=mega,
                    target_is_n=target_is_n,
                    oos_cut=args.oos_cut,
                    net_min=net_min,
                )

            events = filter_signals(
                panel, floor=floor, mega=mega, net_min=net_min
            )
            legs = outcome_legs(
                events,
                calendar=calendar,
                date_index=date_index,
                ohlc=ohlc,
                benchmark=benchmark,
            )
            stats = summarize(legs, args.oos_cut)
            rows.append(
                {
                    "id": bid,
                    "name": name_map.get(bid, bid),
                    "seat": seat_map.get(bid, ""),
                    "floor": floor,
                    "floor_yi": floor / 1e8,
                    "whale_hits": (
                        float(uni.loc[uni.id.astype(str) == bid, "whale_hits"].iloc[0])
                        if "whale_hits" in uni.columns
                        and bid in set(uni.id.astype(str))
                        else float("nan")
                    ),
                    **stats,
                    "adaptive_n_is_picked": floor_meta.get("picked_n_is"),
                }
            )
            if j % 20 == 0 or bid == ANCHOR_ID:
                log(
                    f"  [{j}/{len(ids)}] {bid} {name_map.get(bid,'')} "
                    f"floor={floor/1e8:.2f}億 n={stats['n_all']} "
                    f"med={fmt_pct(stats['med_radj_all'])}"
                )

        board = pd.DataFrame(rows)
        protocol = {
            "start": args.start,
            "end": end,
            "oos_cut": args.oos_cut,
            "signal": f"5d_buy>=adaptive_floor ∩ net>={net_min:.2f} ∩ !mega",
            "net_min": net_min,
            "anchor_id": ANCHOR_ID,
            "anchor_floor": ANCHOR_FLOOR,
            "target_is_n": target_is_n,
            "hold": HOLD,
            "cost": COST,
            "beta": BETA,
            "benchmark": BENCHMARK,
            "episode_cal_days": EPISODE_CAL_DAYS,
            "floor_grid": list(FLOOR_GRID),
            "top_branches": args.top_branches,
            "whale_floor": args.whale_floor,
            "db": str(args.db),
            "entry_25m_nonfail": "order_layer_only_not_backtested",
            "primary_metric": "median_r_adj_full",
            "elapsed_sec": round(time.time() - t0, 1),
        }
        paths = write_report(
            board,
            protocol=protocol,
            top_n=args.top_n,
            out_dir=out_dir,
            net_min=net_min,
        )
        log(f"wrote {paths['md']}")
        log(f"wrote {paths['csv']}")
        log(f"done in {time.time()-t0:.1f}s")

        # Print top30 to stdout for chat.
        ranked = board.sort_values(
            ["flag_thin_all", "med_radj_all"], ascending=[True, False]
        )
        print(f"\n=== TOP{args.top_n} (net>={net_min:.0%} · full med r_adj) ===")
        for i, r in enumerate(ranked.head(args.top_n).itertuples(index=False), 1):
            print(
                f"{i:2d}. {r.id} {r.name} seat={r.seat} "
                f"floor={r.floor_yi:.2f}億 n={int(r.n_all)} "
                f"med={fmt_pct(r.med_radj_all)}% win="
                f"{'' if pd.isna(r.win_radj_all) else f'{r.win_radj_all:.0%}'} "
                f"oos_n={int(r.n_oos)} oos_med={fmt_pct(r.med_radj_oos)}%"
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
