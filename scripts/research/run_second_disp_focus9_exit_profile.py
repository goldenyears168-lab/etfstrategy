#!/usr/bin/env python3
"""處置股專家池 Focus9 · 分點賣出軌跡 × 跟單出場比較.

Research only. For each seat's balanced L1H7 legs:
  - When does the branch sell back ≥50% of signal-day buy shares?
  - Dayflip (T+1 sell / T buy), hold style tags
  - Copy exits vs whale: L1H7, follow-50% next open, fixed H, ADR

  PYTHONPATH=src:scripts/research .venv/bin/python \\
    scripts/research/run_second_disp_focus9_exit_profile.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[2]
OUT = (
    ROOT
    / "reports/research/branch-footprint-screen"
    / "second_disp_top30_l1h7"
    / "focus9_exit_profile"
)
SOURCE = "finmind"
COST = 0.003
BETA = 1.15
HOLD_GRID = (1, 2, 3, 5, 7, 10, 14)
FOCUS = [
    ("1360", "港麥格理", 0.3),
    ("9A9R", "永豐金信義", 0.15),
    ("9325", "華南永昌-忠孝", 0.1),
    ("9A81", "永豐金-匯立", 0.7),
    ("9217", "凱基松山", 0.3),
    ("779n", "國票南京", 0.1),
    ("9227", "凱基城中", 0.3),
    ("9216", "凱基-信義", 0.15),
    ("920F", "凱基-站前", 0.1),
]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_legs() -> list[dict]:
    base = OUT.parent
    legs: list[dict] = []
    for p in list(base.glob("shard_*_of_5_w100_v3_bal_abs.json")) + list(
        base.glob("shard_*_of_6_w300x_v3_bal_abs.json")
    ):
        legs.extend(json.loads(p.read_text(encoding="utf-8")).get("legs") or [])
    want = {b for b, _, _ in FOCUS}
    return [x for x in legs if x["branch_id"] in want]


def load_calendar(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    return [
        str(r[0])
        for r in conn.execute(
            """
            SELECT trade_date FROM stock_daily_bars
            WHERE source=? AND stock_id='2330'
              AND trade_date BETWEEN ? AND ?
            ORDER BY trade_date
            """,
            (SOURCE, start, end),
        )
    ]


def load_ohlc(
    conn: sqlite3.Connection, sids: list[str], start: str, end: str
) -> dict[tuple[str, str], tuple[float, float]]:
    if not sids:
        return {}
    q = ",".join("?" * len(sids))
    out: dict[tuple[str, str], tuple[float, float]] = {}
    for sid, d, o, c in conn.execute(
        f"""
        SELECT stock_id, trade_date, open, close FROM stock_daily_bars
        WHERE source=? AND stock_id IN ({q})
          AND trade_date BETWEEN ? AND ?
          AND open>0 AND close>0
        """,
        (SOURCE, *sids, start, end),
    ):
        out[(str(sid), str(d))] = (float(o), float(c))
    return out


def load_ix(
    conn: sqlite3.Connection, start: str, end: str
) -> dict[str, tuple[float, float]]:
    best: dict[str, tuple[float, float, int]] = {}
    for d, o, c, src in conn.execute(
        """
        SELECT date, open, close, source FROM daily_bars
        WHERE code='IX0001' AND date BETWEEN ? AND ?
          AND open>0 AND close>0
        """,
        (start, end),
    ):
        rank = {"yahoo": 0, "tej": 1, "finmind": 2}.get(str(src), 9)
        prev = best.get(str(d))
        if prev is None or rank < prev[2]:
            best[str(d)] = (float(o), float(c), rank)
    return {d: (v[0], v[1]) for d, v in best.items()}


def load_tape(
    conn: sqlite3.Connection,
    branch_id: str,
    stock_id: str,
    start: str,
    end: str,
) -> list[dict]:
    rows = []
    for r in conn.execute(
        """
        SELECT trade_date, buy, sell FROM stock_broker_branch_daily
        WHERE source=? AND securities_trader_id=? AND stock_id=?
          AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (SOURCE, branch_id, stock_id, start, end),
    ):
        rows.append(
            {
                "date": str(r["trade_date"]),
                "buy": float(r["buy"] or 0),
                "sell": float(r["sell"] or 0),
            }
        )
    return rows


def ret_path(
    entry_open: float,
    exit_px: float,
    ix_entry_open: float,
    ix_exit_close: float,
) -> dict:
    stock_r = exit_px / entry_open - 1.0 - COST
    bench_r = ix_exit_close / ix_entry_open - 1.0
    excess = stock_r - BETA * bench_r
    return {
        "stock_pct": stock_r * 100.0,
        "bench_pct": bench_r * 100.0,
        "excess_pct": excess * 100.0,
    }


def summarize(xs: list[float], holds: list[int] | None = None) -> dict:
    if not xs:
        return {"n": 0}
    out = {
        "n": len(xs),
        "med": round(median(xs), 3),
        "mean": round(mean(xs), 3),
        "win": round(100.0 * mean(1 if x > 0 else 0 for x in xs), 1),
    }
    if holds and len(holds) == len(xs) and all(h > 0 for h in holds):
        adrs = [x / h for x, h in zip(xs, holds)]
        out["adr_mean"] = round(mean(adrs), 3)
        out["adr_med"] = round(median(adrs), 3)
        out["hold_med"] = round(median(holds), 2)
        out["hold_mean"] = round(mean(holds), 2)
    return out


def tag_style(dayflip_med: float | None, sell50_med: float | None, dayflip_rate: float) -> str:
    tags = []
    if (dayflip_med is not None and dayflip_med >= 0.4) or dayflip_rate >= 0.45:
        tags.append("偏隔日沖")
    elif (dayflip_med is not None and dayflip_med >= 0.2) or dayflip_rate >= 0.3:
        tags.append("隔日有出清傾向")
    if sell50_med is not None:
        if sell50_med <= 2:
            tags.append("快出（≤2日達半出）")
        elif sell50_med <= 5:
            tags.append("波段短抱（約3–5日半出）")
        elif sell50_med <= 10:
            tags.append("波段中抱（約1–2週半出）")
        else:
            tags.append("偏長抱（>10日才半出）")
    return "・".join(tags) if tags else "風格不明"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    db = ROOT / "data/stocks.db"
    if not db.exists():
        raise FileNotFoundError(db)
    legs = load_legs()
    log(f"focus legs={len(legs)}")
    conn = connect(db)

    sids = sorted({x["stock_id"] for x in legs})
    dates = [x["signal_date"] for x in legs] + [x["exit_date"] for x in legs]
    start, end = min(dates), max(dates)
    # room for H14
    cal = load_calendar(conn, "2025-12-01", "2026-07-22")
    cidx = {d: i for i, d in enumerate(cal)}
    ohlc = load_ohlc(conn, sids, "2025-12-01", "2026-07-22")
    ix = load_ix(conn, "2025-12-01", "2026-07-22")

    by_branch: dict[str, list[dict]] = defaultdict(list)
    for leg in legs:
        by_branch[leg["branch_id"]].append(leg)

    profiles: list[dict] = []
    all_leg_detail: dict[str, list[dict]] = {}

    for bid, bname, thr in FOCUS:
        bl = sorted(by_branch.get(bid, []), key=lambda x: x["signal_date"])
        details = []
        dayflip_ratios = []
        sell50_days = []
        exits: dict[str, list[tuple[float, int]]] = defaultdict(list)

        for leg in bl:
            sid = leg["stock_id"]
            sig = leg["signal_date"]
            si = cidx.get(sig)
            if si is None:
                continue
            buy0 = float(leg.get("buy_shares") or 0)
            # tape from signal through +20 trading days
            end_i = min(si + 20, len(cal) - 1)
            tape = load_tape(conn, bid, sid, sig, cal[end_i])
            # signal day buy confirm
            t0 = next((t for t in tape if t["date"] == sig), None)
            if t0 and buy0 <= 0:
                buy0 = t0["buy"]
            # cumulative sell after signal (include T0 sell? for dayflip use T+1 only)
            # 50% unwind: cum sell from T0 onward / buy0 (branch may sell same day)
            cum_sell = 0.0
            sell50_date = None
            sell50_lag = None  # trading days after signal (0=same day)
            for t in tape:
                cum_sell += t["sell"]
                if buy0 > 0 and sell50_date is None and cum_sell / buy0 >= 0.5:
                    sell50_date = t["date"]
                    ti = cidx.get(t["date"])
                    sell50_lag = None if ti is None else ti - si
            # dayflip: T+1 sell / T buy
            t1 = cal[si + 1] if si + 1 < len(cal) else None
            t1_row = next((t for t in tape if t["date"] == t1), None) if t1 else None
            dayflip = (t1_row["sell"] / buy0) if (t1_row and buy0 > 0) else None
            if dayflip is not None:
                dayflip_ratios.append(dayflip)
            if sell50_lag is not None:
                sell50_days.append(sell50_lag)

            # entry always T+1 open (copytrade)
            if si + 1 >= len(cal):
                continue
            entry_d = cal[si + 1]
            er = ohlc.get((sid, entry_d))
            ie = ix.get(entry_d)
            if not er or not ie:
                continue

            # --- exit variants ---
            # L1H7
            if si + 1 + 7 - 1 < len(cal):
                xd = cal[si + 1 + 7 - 1]
                xr = ohlc.get((sid, xd))
                ixr = ix.get(xd)
                if xr and ixr:
                    r = ret_path(er[0], xr[1], ie[0], ixr[1])
                    exits["L1H7"].append((r["excess_pct"], 7))
                    leg_l1 = r
                else:
                    leg_l1 = None
            else:
                leg_l1 = None

            # fixed H
            for h in HOLD_GRID:
                xi = si + 1 + h - 1
                if xi >= len(cal):
                    continue
                xd = cal[xi]
                xr = ohlc.get((sid, xd))
                ixr = ix.get(xd)
                if not xr or not ixr:
                    continue
                r = ret_path(er[0], xr[1], ie[0], ixr[1])
                exits[f"H{h}"].append((r["excess_pct"], h))

            # follow 50% sell → exit next open after sell50_date (or sell50 close if next missing)
            # If sell50 on signal day (lag0), exit still T+1 open (=entry) → 0-day weird; use T+1 close
            follow = None
            if sell50_date is not None and sell50_lag is not None:
                s50i = cidx[sell50_date]
                # exit open of next day after 50% day; if that is <= entry, use entry close (same-day flatten)
                exit_i = s50i + 1
                if exit_i <= si + 1:
                    # whale already half-out by entry morning → exit entry day close
                    xd = entry_d
                    xr = ohlc.get((sid, xd))
                    ixr = ix.get(xd)
                    if xr and ixr:
                        r = ret_path(er[0], xr[1], ie[0], ixr[1])
                        hold = 1
                        exits["follow50_next"].append((r["excess_pct"], hold))
                        follow = {**r, "hold": hold, "mode": "entry_close_early50"}
                elif exit_i < len(cal):
                    xd = cal[exit_i]
                    xr = ohlc.get((sid, xd))
                    ixr = ix.get(xd)
                    if xr and ixr:
                        # enter T+1 open, exit next open after 50% → use that day's open as exit? 
                        # For ADR fairness use open→open; research uses open→close for L1H7.
                        # Use exit day open as exit px for "sold overnight after seeing tape"
                        r = ret_path(er[0], xr[0], ie[0], ix.get(xd)[0] if ix.get(xd) else ixr[0])
                        # bench: entry open → exit open
                        if ix.get(xd):
                            stock_r = xr[0] / er[0] - 1.0 - COST
                            bench_r = ix[xd][0] / ie[0] - 1.0
                            excess = (stock_r - BETA * bench_r) * 100.0
                            hold = exit_i - (si + 1) + 1
                            if hold < 1:
                                hold = 1
                            exits["follow50_next"].append((excess, hold))
                            follow = {
                                "stock_pct": stock_r * 100.0,
                                "excess_pct": excess,
                                "hold": hold,
                                "mode": "next_open_after50",
                                "exit_date": xd,
                            }

            # if dayflip>=50%: exit T+1 close (same day as entry) OR T+2 open
            dayflip_exit = None
            if dayflip is not None and dayflip >= 0.5 and t1:
                xr = ohlc.get((sid, t1))
                ixr = ix.get(t1)
                if xr and ixr:
                    r = ret_path(er[0], xr[1], ie[0], ixr[1])
                    exits["dayflip50_T1close"].append((r["excess_pct"], 1))
                    dayflip_exit = {**r, "hold": 1}

            details.append(
                {
                    "signal_date": sig,
                    "stock_id": sid,
                    "buy_shares": buy0,
                    "buy_amt": leg.get("buy_amt"),
                    "l1h7_excess": leg.get("excess_pct"),
                    "l1h7_stock": leg.get("stock_pct"),
                    "dayflip_ratio": None if dayflip is None else round(dayflip, 3),
                    "sell50_date": sell50_date,
                    "sell50_lag_td": sell50_lag,
                    "follow50": follow,
                    "dayflip50_exit": dayflip_exit,
                    "l1h7_recomputed": leg_l1,
                }
            )

        # aggregates
        df_med = median(dayflip_ratios) if dayflip_ratios else None
        df_rate = mean(1 if x >= 0.5 else 0 for x in dayflip_ratios) if dayflip_ratios else None
        s50_med = median(sell50_days) if sell50_days else None
        s50_cov = len(sell50_days) / len(bl) if bl else 0

        exit_table = {}
        best_adr = None
        for name, pairs in exits.items():
            xs = [p[0] for p in pairs]
            hs = [p[1] for p in pairs]
            st = summarize(xs, hs)
            exit_table[name] = st
            if st.get("n", 0) >= max(3, len(bl) // 2) and "adr_mean" in st:
                cand = (st["adr_mean"], st["mean"], st["n"], name)
                if best_adr is None or cand > best_adr:
                    best_adr = cand

        # also pick best total mean among exits with n>=3
        best_mean = None
        for name, st in exit_table.items():
            if st.get("n", 0) >= max(3, len(bl) // 2) and "mean" in st:
                cand = (st["mean"], st.get("adr_mean", -999), st["n"], name)
                if best_mean is None or cand > best_mean:
                    best_mean = cand

        profile = {
            "branch_id": bid,
            "branch_name": bname,
            "threshold_yi": thr,
            "n_legs": len(bl),
            "dayflip_med": None if df_med is None else round(df_med, 3),
            "dayflip_ge50_rate": None if df_rate is None else round(100 * df_rate, 1),
            "sell50_lag_med": None if s50_med is None else round(s50_med, 2),
            "sell50_coverage": round(100 * s50_cov, 1),
            "style_tag": tag_style(df_med, s50_med, df_rate or 0),
            "exits": exit_table,
            "best_adr": None
            if best_adr is None
            else {"rule": best_adr[3], "adr_mean": best_adr[0], "excess_mean": best_adr[1], "n": best_adr[2]},
            "best_excess_mean": None
            if best_mean is None
            else {
                "rule": best_mean[3],
                "excess_mean": best_mean[0],
                "adr_mean": best_mean[1],
                "n": best_mean[2],
            },
            "whale_vs_copy": {
                "note": "whale path approximated by follow50; copy baseline L1H7",
                "L1H7": exit_table.get("L1H7"),
                "follow50_next": exit_table.get("follow50_next"),
                "dayflip50_T1close": exit_table.get("dayflip50_T1close"),
            },
        }
        profiles.append(profile)
        all_leg_detail[bid] = details
        (OUT / f"{bid}_legs.json").write_text(
            json.dumps({"profile": profile, "legs": details}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log(
            f"{bid} {bname} n={len(bl)} style={profile['style_tag']} "
            f"best_adr={profile['best_adr']} best_mean={profile['best_excess_mean']}"
        )

    payload = {
        "protocol": {
            "universe": "second-disp 2026 focus9 balanced legs",
            "entry": "T+1 open",
            "cost_bps": 30,
            "beta": BETA,
            "sell50": "first day cum_sell/signal_buy>=0.5",
            "dayflip": "T+1 sell / T buy shares",
            "adr": "excess_pct / hold_trading_days (simple, not compounded)",
            "follow50_next": "exit next open after sell50 day (or entry close if early)",
        },
        "profiles": profiles,
    }
    (OUT / "FOCUS9_EXIT_PROFILES.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # markdown
    lines = [
        "# Focus9 · 分點賣出軌跡 × 跟單出場",
        "",
        "Research only · 未採納",
        "",
        "## 協議摘要",
        "",
        "- 進場：訊號翌日開盤（T+1 open）· 成本 30bps · 超額 β=1.15",
        "- 半出：累計賣股數 / 訊號日買股數 ≥50% 首日",
        "- 隔日沖：T+1 賣 / T 買",
        "- ADR：超額% / 持有交易日（簡單均攤）",
        "",
        "## 分點總覽",
        "",
        "|分點|N|風格|隔日賣回中位|隔日≥50%|半出中位日|半出覆蓋|L1H7超額均|最佳ADR規則|ADR均|最佳總超額規則|超額均|",
        "|---|---:|---|---:|---:|---:|---:|---:|---|---:|---|---:|",
    ]
    for p in profiles:
        l1 = p["exits"].get("L1H7") or {}
        ba = p["best_adr"] or {}
        bm = p["best_excess_mean"] or {}
        lines.append(
            f"|{p['branch_name']} (`{p['branch_id']}`)|{p['n_legs']}|{p['style_tag']}|"
            f"{p['dayflip_med'] if p['dayflip_med'] is not None else '—'}|"
            f"{p['dayflip_ge50_rate'] if p['dayflip_ge50_rate'] is not None else '—'}%|"
            f"{p['sell50_lag_med'] if p['sell50_lag_med'] is not None else '—'}|"
            f"{p['sell50_coverage']}%|"
            f"{l1.get('mean', '—')}|"
            f"{ba.get('rule', '—')}|{ba.get('adr_mean', '—')}|"
            f"{bm.get('rule', '—')}|{bm.get('excess_mean', '—')}|"
        )
    (OUT / "FOCUS9_EXIT_PROFILES.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"wrote {OUT}")


if __name__ == "__main__":
    main()
