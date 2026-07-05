#!/usr/bin/env python3
"""Research · Leading + Weinstein Stage 2 · -6% intraday crash entry backtest."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stage_analysis import calculate_minervini_trend_template, classify_weinstein_stage  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

CORE4 = frozenset({"2327", "2449", "3264", "3211"})
BENCH = "2330"
IX = "IX0001"
MIN_MINUTE = "09:05"
DIP_PCTS = (0.05, 0.06, 0.07)
HOLD_DAYS = (3, 5, 10)


@dataclass(frozen=True)
class Event:
    stock_id: str
    session: str
    dip_pct: float
    hold_days: int
    ret_pct: float
    leading: bool
    stage2: bool
    minervini7: bool
    gate_on: bool | None
    idiosyncratic: bool
    gap_up_fade: bool


def _summarize(rets: list[float]) -> str:
    if not rets:
        return "n=0"
    win = sum(1 for r in rets if r > 0) / len(rets) * 100
    return f"n={len(rets)} win={win:.1f}% med={median(rets):+.2f}% avg={sum(rets)/len(rets):+.2f}%"


def _load_daily(conn: sqlite3.Connection, sid: str) -> pd.DataFrame | None:
    rows = conn.execute(
        "SELECT trade_date, open, high, low, close FROM stock_daily_bars WHERE stock_id=? ORDER BY trade_date",
        (sid,),
    ).fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "Open", "High", "Low", "Close"])
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")


def _rrg_map(conn: sqlite3.Connection, sid: str) -> dict[str, str]:
    rows = conn.execute(
        """
        SELECT session_date, quadrant FROM rrg_universe_scores
        WHERE stock_id=? AND screen_kind='close' AND quadrant IS NOT NULL
        """,
        (sid,),
    ).fetchall()
    return {str(r[0]): str(r[1]) for r in rows}


def _kbar_by_session(conn: sqlite3.Connection, sid: str) -> dict[str, list[tuple[str, float]]]:
    rows = conn.execute(
        "SELECT trade_date, minute, close FROM stock_kbar_1m WHERE stock_id=? ORDER BY trade_date, minute",
        (sid,),
    ).fetchall()
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for d, m, c in rows:
        if c is not None:
            out[str(d)].append((str(m), float(c)))
    return dict(out)


def _price_at(bars: list[tuple[str, float]], minute: str) -> float | None:
    last = None
    for m, px in bars:
        if m[:5] >= minute[:5]:
            return px if m[:5] == minute[:5] else last
        last = px
    return last


def _scalar_close(df: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    if ts not in df.index:
        return None
    val = df.loc[ts, "Close"]
    return float(val.iloc[0] if hasattr(val, "iloc") else val)


def _pit_labels(df: pd.DataFrame, session_dates: set[str]) -> dict[str, tuple[bool, bool]]:
    labels: dict[str, tuple[bool, bool]] = {}
    date_list = [d.strftime("%Y-%m-%d") for d in df.index]
    pos = {d: i for i, d in enumerate(date_list)}
    for session in session_dates:
        i = pos.get(session)
        if i is None or i == 0:
            labels[session] = (False, False)
            continue
        prev = pd.Timestamp(date_list[i - 1])
        sub = df.loc[:prev]
        if len(sub) < 260:
            labels[session] = (False, False)
            continue
        w = classify_weinstein_stage(sub)
        m = calculate_minervini_trend_template(sub)
        labels[session] = (int(w.get("stage") or 0) == 2, int(m.get("score") or 0) >= 7)
    return labels


def run_backtest(conn: sqlite3.Connection, stock_ids: list[str]) -> list[Event]:
    events: list[Event] = []
    bench = _load_daily(conn, BENCH)
    ix = _load_daily(conn, IX)
    core_daily = {sid: _load_daily(conn, sid) for sid in CORE4}
    core_kbar = {sid: _kbar_by_session(conn, sid) for sid in CORE4}
    bench_kbar = _kbar_by_session(conn, BENCH)

    for sid in stock_ids:
        df = _load_daily(conn, sid)
        kb = _kbar_by_session(conn, sid)
        if df is None or not kb:
            continue
        dates = [d.strftime("%Y-%m-%d") for d in df.index]
        idx = {d: i for i, d in enumerate(dates)}
        rrg = _rrg_map(conn, sid)
        pit = _pit_labels(df, set(kb.keys()))

        for session, bars in kb.items():
            if session not in idx or len(bars) < 30:
                continue
            i = idx[session]
            if i == 0:
                continue
            prev_date = dates[i - 1]
            prev_close = _scalar_close(df, pd.Timestamp(prev_date))
            if not prev_close or prev_close <= 0:
                continue

            leading = rrg.get(prev_date) == "leading"
            stage2, minervini7 = pit.get(session, (False, False))

            prev_ts = pd.Timestamp(prev_date)
            gate = None
            if bench is not None:
                bench_prev = _scalar_close(bench, prev_ts)
                bench_bars = bench_kbar.get(session)
                if bench_prev and bench_bars:
                    bench_px = _price_at(bench_bars, "09:05")
                    if bench_px is not None:
                        gate_b = bench_px <= round(bench_prev * 0.995, 2)
                        gate_a = 0
                        for cs in CORE4:
                            cdf = core_daily.get(cs)
                            prev_c = _scalar_close(cdf, prev_ts) if cdf is not None else None
                            px = _price_at(core_kbar.get(cs, {}).get(session, []), "09:05")
                            if prev_c and px and px <= round(prev_c * 0.98, 2):
                                gate_a += 1
                        gate = gate_a >= 2 and gate_b

            ts = pd.Timestamp(session)
            day_open = _scalar_close(df, ts)  # wrong - need open
            try:
                day_open = float(df.loc[ts, "Open"].iloc[0] if hasattr(df.loc[ts, "Open"], "iloc") else df.loc[ts, "Open"])
            except KeyError:
                day_open = bars[0][1]
            day_hi = max(px for _, px in bars)
            idx_pct = None
            if ix is not None and ts in ix.index and i > 0:
                ix_prev = _scalar_close(ix, pd.Timestamp(prev_date))
                ix_close = _scalar_close(ix, ts)
                if ix_prev and ix_close:
                    idx_pct = (ix_close / ix_prev - 1) * 100
            idiosyncratic = bool(idx_pct is not None and idx_pct > -1.0)
            gap_up_fade = bool(day_hi >= prev_close * 1.01)

            for dip in DIP_PCTS:
                th = round(prev_close * (1 - dip), 2)
                entry = next((px for m, px in bars if m[:5] >= MIN_MINUTE and px <= th), None)
                if entry is None:
                    continue
                for hold in HOLD_DAYS:
                    if i + hold >= len(dates):
                        continue
                    exit_px = _scalar_close(df, pd.Timestamp(dates[i + hold]))
                    if not exit_px:
                        continue
                    events.append(
                        Event(
                            stock_id=sid,
                            session=session,
                            dip_pct=dip,
                            hold_days=hold,
                            ret_pct=(exit_px / entry - 1) * 100,
                            leading=leading,
                            stage2=stage2,
                            minervini7=minervini7,
                            gate_on=gate,
                            idiosyncratic=idiosyncratic,
                            gap_up_fade=gap_up_fade,
                        )
                    )
    return events


def _pick(events: list[Event], *, dip: float, hold: int, **conds) -> list[Event]:
    out = [e for e in events if abs(e.dip_pct - dip) < 1e-9 and e.hold_days == hold]
    for k, v in conds.items():
        out = [e for e in out if getattr(e, k) == v]
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dip", type=float, default=0.06)
    parser.add_argument("--hold", type=int, default=5)
    args = parser.parse_args()

    conn = connect(DEFAULT_DB_PATH)
    stock_ids = [
        r[0]
        for r in conn.execute("SELECT DISTINCT stock_id FROM stock_kbar_1m ORDER BY stock_id").fetchall()
        if r[0] not in {BENCH, IX}
    ]
    print(f"Building events for {len(stock_ids)} names …")
    events = run_backtest(conn, stock_ids)
    conn.close()

    dip, hold = args.dip, args.hold
    print(f"\nExpanded sample · dip -{dip*100:.0f}% · hold {hold}d\n{'='*72}")

    rows = [
        ("H0 · leading only", _pick(events, dip=dip, hold=hold, leading=True)),
        ("H1 · leading + Stage 2", _pick(events, dip=dip, hold=hold, leading=True, stage2=True)),
        ("H2 · H1 + Minervini≥7", _pick(events, dip=dip, hold=hold, leading=True, stage2=True, minervini7=True)),
        ("H3 · H2 + gate OFF", _pick(events, dip=dip, hold=hold, leading=True, stage2=True, minervini7=True, gate_on=False)),
        (
            "H4 · H3 + 加權≥-1%（個股急跌）",
            [e for e in _pick(events, dip=dip, hold=hold, leading=True, stage2=True, minervini7=True, gate_on=False) if e.idiosyncratic],
        ),
        (
            "H5 · H3 + 開高後殺（high≥+1%）",
            [e for e in _pick(events, dip=dip, hold=hold, leading=True, stage2=True, minervini7=True, gate_on=False) if e.gap_up_fade],
        ),
    ]
    for label, subset in rows:
        print(f"{label:42} {_summarize([e.ret_pct for e in subset])}")

    print(f"\n## H3 dip sweep · hold {hold}d")
    for d in DIP_PCTS:
        sub = _pick(events, dip=d, hold=hold, leading=True, stage2=True, minervini7=True, gate_on=False)
        print(f"  -{d*100:.0f}%: {_summarize([e.ret_pct for e in sub])}")

    print(f"\n## H3 hold sweep · dip -{dip*100:.0f}%")
    for h in HOLD_DAYS:
        sub = _pick(events, dip=dip, hold=h, leading=True, stage2=True, minervini7=True, gate_on=False)
        print(f"  hold {h}d: {_summarize([e.ret_pct for e in sub])}")

    print("\n## 3008/5536/6223 · H3")
    for sid in ("3008", "5536", "6223"):
        sub = _pick(events, dip=dip, hold=hold, leading=True, stage2=True, minervini7=True, gate_on=False)
        sub = [e for e in sub if e.stock_id == sid]
        print(f"  {sid}: {_summarize([e.ret_pct for e in sub])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
