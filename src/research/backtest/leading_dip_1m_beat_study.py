"""Leading Dip · 1m / 1-min poll beat study (Research only).

Question: on a recent window (focus 2026-07-16 morning), can 1m K + every-minute
first-hit variants beat frozen ``leading_dip_cov`` (5m first_hit)?

Win criteria (preregistered):
  * Historical days with T+3: higher OOS-style med_ex3 / hit vs 5m baseline
    on the same date set (coverage slots).
  * Same-day morning (no T+3 yet): matched-name better entry (lower px / deeper
    ex0) without worse +15m / +30m adverse bounce; or strictly better slot set
    under same day-cap.

Not a Strategy adoption path until G2 hold-out clears.
"""

from __future__ import annotations

import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from research.backtest.leading_dip_sleeve_validate import (
    EXCESS_MAX,
    FROZEN_SLOT_POLICY,
    GAP3_MAX,
    HOLD_DAYS,
    MAX_ABS_EX0,
    TOTAL_OPEN_CAP,
    apply_slot_policy,
    apply_total_open_cap,
    build_path_frame,
    collect_leading_dip_events,
    first_hit_index,
    is_t2_minute,
    load_universe_by_baseline,
    passes_structure_filters,
    summarize_trades,
)
from market_benchmark import load_benchmark_close
from research.backtest.finpilot_local_backtest import load_price_panels
from rrg_mono_daily_brief import LOOKBACK, _feat
from rrg_rotation import compute_rrg_panel

SCHEMA = "leading_dip_1m_beat-v1"
TZ = ZoneInfo("Asia/Taipei")
BENCH_IDS = ("IX0001", "0050")
DEFAULT_FOCUS_DAY = "2026-07-15"
DEFAULT_WINDOW_START = "2026-07-13"
DEFAULT_WINDOW_END = "2026-07-15"
EntryMode = Literal["first_hit", "confirm", "wait_deepen"]


@dataclass(frozen=True)
class VariantSpec:
    id: str
    label: str
    bar: Literal["5m", "1m"]
    mode: EntryMode = "first_hit"
    confirm_bars: int = 1
    wait_minutes: int = 0
    gap3_max: float = GAP3_MAX
    excess_max: float = EXCESS_MAX
    # For bar=1m: ``5m`` forces smoothed bench (ffill onto 1m stock minutes).
    bench_bar: Literal["auto", "5m", "1m"] = "auto"
    # Local dip vs t−K minutes (not yday / not open). None = use yday excess col ``ex``.
    lookback_minutes: int | None = None
    # When lookback set: require gap≤gap3_max as well (default off — gap is separate suddenness).
    require_gap: bool = False


DEFAULT_VARIANTS: tuple[VariantSpec, ...] = (
    VariantSpec("ldip_5m_cov", "Baseline · Leading Dip cov · 5m first_hit", "5m"),
    VariantSpec("naive_1m", "Naive port · same gates · 1m first_hit", "1m"),
    VariantSpec(
        "confirm3_1m",
        "1m · need 3 consecutive bars under gate",
        "1m",
        mode="confirm",
        confirm_bars=3,
    ),
    VariantSpec(
        "wait5_1m",
        "1m · first touch then wait≤5m for local deepest ex",
        "1m",
        mode="wait_deepen",
        wait_minutes=5,
    ),
    VariantSpec(
        "wait15_1m",
        "1m · first touch then wait≤15m for local deepest ex",
        "1m",
        mode="wait_deepen",
        wait_minutes=15,
    ),
    VariantSpec(
        "gap120_1m",
        "1m · tighter gap≤−1.20 ∩ ex≤−4 (noise trim)",
        "1m",
        gap3_max=-1.20,
    ),
)


def _minute_to_mins(hhmm: str) -> int:
    h, m = str(hhmm)[:5].split(":")
    return int(h) * 60 + int(m)


def _mins_to_minute(mins: int) -> str:
    return f"{mins // 60:02d}:{mins % 60:02d}"


def confirm_hit_index(mask: pd.Series, *, confirm_bars: int) -> int | None:
    """First index where ``confirm_bars`` consecutive True values end."""
    k = max(1, int(confirm_bars))
    hits = mask.fillna(False).astype(bool).to_numpy()
    if k == 1:
        return first_hit_index(mask)
    run = 0
    for i, ok in enumerate(hits):
        run = run + 1 if ok else 0
        if run >= k:
            return i
    return None


def wait_deepen_hit_index(
    mask: pd.Series,
    excess: pd.Series,
    minutes: pd.Series,
    *,
    wait_minutes: int,
) -> int | None:
    """On first gate touch, enter at the deepest excess bar within wait window."""
    i0 = first_hit_index(mask)
    if i0 is None:
        return None
    t0 = _minute_to_mins(str(minutes.iloc[i0]))
    t1 = t0 + max(0, int(wait_minutes))
    best_i = i0
    best_ex = float(excess.iloc[i0])
    for i in range(i0, len(mask)):
        mi = _minute_to_mins(str(minutes.iloc[i]))
        if mi > t1:
            break
        if not bool(mask.iloc[i]):
            continue
        ex = float(excess.iloc[i])
        if ex < best_ex:
            best_ex = ex
            best_i = i
    return int(best_i)


def attach_lookback_excess(f: pd.DataFrame, lookback_minutes: int) -> pd.DataFrame:
    """Additive excess vs price K bars ago (1m ≈ K minutes): not yday / not open."""
    out = f.copy()
    k = max(1, int(lookback_minutes))
    s0 = out["S"].shift(k)
    b0 = out["B"].shift(k)
    out["ex_lb"] = (out["S"] / s0 - 1.0) * 100.0 - (out["B"] / b0 - 1.0) * 100.0
    return out


def resolve_entry_index(
    f: pd.DataFrame,
    *,
    mode: EntryMode,
    confirm_bars: int,
    wait_minutes: int,
    gap3_max: float,
    excess_max: float,
    ex_col: str = "ex",
    require_gap: bool = True,
) -> int | None:
    mask = f[ex_col].astype(float) <= float(excess_max)
    if require_gap:
        mask = mask & (f["gap"].astype(float) <= float(gap3_max))
    if mode == "first_hit":
        return first_hit_index(mask)
    if mode == "confirm":
        return confirm_hit_index(mask, confirm_bars=confirm_bars)
    if mode == "wait_deepen":
        return wait_deepen_hit_index(
            mask, f[ex_col], f["minute"], wait_minutes=wait_minutes
        )
    raise ValueError(f"unknown mode={mode!r}")


def _load_kbar_table(
    con: sqlite3.Connection, sid: str, start: str, *, table: str
) -> dict[str, pd.Series]:
    df = pd.read_sql(
        f"""SELECT trade_date, substr(minute,1,5) AS minute, close FROM {table}
            WHERE stock_id=? AND trade_date>=? ORDER BY trade_date, minute""",
        con,
        params=(sid, start),
    )
    if df.empty:
        return {}
    df = df[(df.minute >= "09:00") & (df.minute <= "13:30")].drop_duplicates(
        ["trade_date", "minute"], keep="last"
    )
    return {d: g.set_index("minute")["close"] for d, g in df.groupby("trade_date")}


def coverage_report(
    con: sqlite3.Connection,
    *,
    start: str,
    end: str,
    universe: Sequence[str],
) -> dict[str, Any]:
    ids = sorted({str(x) for x in universe} | set(BENCH_IDS))
    out: dict[str, Any] = {"start": start, "end": end, "universe_n": len(universe), "days": {}}
    days = [
        str(r[0])
        for r in con.execute(
            """SELECT DISTINCT trade_date FROM stock_kbar_5m
               WHERE trade_date BETWEEN ? AND ? ORDER BY 1""",
            (start, end),
        ).fetchall()
    ]
    if not days:
        days = [
            str(r[0])
            for r in con.execute(
                """SELECT DISTINCT trade_date FROM stock_daily_bars
                   WHERE trade_date BETWEEN ? AND ? ORDER BY 1""",
                (start, end),
            ).fetchall()
        ]
    for d in days:
        row: dict[str, Any] = {}
        for table, key in (("stock_kbar_1m", "n_1m"), ("stock_kbar_5m", "n_5m")):
            q = (
                f"SELECT COUNT(DISTINCT stock_id) FROM {table} "
                f"WHERE trade_date=? AND stock_id IN ({','.join('?' * len(ids))})"
            )
            row[key] = int(con.execute(q, (d, *ids)).fetchone()[0])
        row["daily_n"] = int(
            con.execute(
                f"""SELECT COUNT(DISTINCT stock_id) FROM stock_daily_bars
                    WHERE trade_date=? AND stock_id IN ({','.join('?' * len(ids))})""",
                (d, *ids),
            ).fetchone()[0]
        )
        out["days"][d] = row
    return out


def leading_ids_for_day(con: sqlite3.Connection, day: str) -> list[str]:
    lead_by = load_universe_by_baseline(con, universe_mode="leading")
    bases = sorted(b for b in lead_by if b < day)
    if not bases:
        return []
    return sorted(lead_by[bases[-1]])


def backfill_recent_1m(
    con: sqlite3.Connection,
    *,
    start: str,
    end: str,
    sleep_sec: float = 0.12,
    sync_daily: bool = True,
    sync_5m: bool = True,
) -> dict[str, Any]:
    """Yahoo pull · Leading∪bench · daily(T−1) + 1m (+ optional 5m refresh)."""
    from stock_db.kbar import upsert_stock_kbar_1m, upsert_yahoo_5m_rows
    from stock_db.market import upsert_stock_daily_bars
    from yahoo_chart_sync import (
        fetch_tw_intraday_kbar_rows,
        stock_daily_bars_rows_from_yahoo,
    )

    days = [
        str(r[0])
        for r in con.execute(
            """SELECT DISTINCT trade_date FROM stock_kbar_5m
               WHERE trade_date BETWEEN ? AND ? ORDER BY 1""",
            (start, end),
        ).fetchall()
    ]
    # Include focus end even if 5m thin (intraday morning).
    if end not in days:
        days.append(end)
        days = sorted(set(days))

    ids: set[str] = set(BENCH_IDS)
    for d in days:
        ids.update(leading_ids_for_day(con, d))
    id_list = sorted(ids)

    stats = {
        "days": days,
        "universe_ids": len(id_list),
        "daily_upserts": 0,
        "kbar_1m_upserts": 0,
        "kbar_5m_upserts": 0,
        "errors": 0,
    }

    if sync_daily and id_list:
        # Need closes through last completed session for excess(昨收).
        end_d = date.fromisoformat(end)
        today = datetime.now(TZ).date()
        daily_end = min(end_d, today) - timedelta(days=0)
        # During session, do not trust same-day Yahoo daily as T close.
        if daily_end >= today:
            daily_end = today - timedelta(days=1)
        daily_start = date.fromisoformat(start) - timedelta(days=14)
        for sid in id_list:
            if sid == "IX0001":
                continue
            try:
                rows, _ = stock_daily_bars_rows_from_yahoo(sid, daily_start, daily_end)
                if rows:
                    stats["daily_upserts"] += int(upsert_stock_daily_bars(con, rows) or 0)
            except Exception:
                stats["errors"] += 1
            if sleep_sec > 0:
                time.sleep(min(sleep_sec, 0.08))

    today = datetime.now(TZ).date().isoformat()
    now_hhmm = datetime.now(TZ).strftime("%H:%M")
    # Complete session ≈ 270 one-minute bars; morning uses elapsed minutes from 09:00.
    def _need_1m(day: str) -> int:
        if day < today:
            return 200
        elapsed = max(0, _minute_to_mins(now_hhmm) - _minute_to_mins("09:00"))
        return max(30, min(200, elapsed - 2))

    stats["skipped_fresh_1m"] = 0
    for d in days:
        d_date = date.fromisoformat(d)
        day_ids = sorted(set(BENCH_IDS) | set(leading_ids_for_day(con, d)))
        need = _need_1m(d)
        for sid in day_ids:
            try:
                n1 = int(
                    con.execute(
                        "SELECT COUNT(*) FROM stock_kbar_1m WHERE stock_id=? AND trade_date=?",
                        (sid, d),
                    ).fetchone()[0]
                )
                if n1 >= need:
                    stats["skipped_fresh_1m"] += 1
                else:
                    rows_1m, _ = fetch_tw_intraday_kbar_rows(
                        sid, d_date, d_date, interval="1m"
                    )
                    if rows_1m:
                        stats["kbar_1m_upserts"] += int(
                            upsert_stock_kbar_1m(con, rows_1m) or 0
                        )
                    if sleep_sec > 0:
                        time.sleep(sleep_sec)
                if sync_5m:
                    n5 = int(
                        con.execute(
                            "SELECT COUNT(*) FROM stock_kbar_5m WHERE stock_id=? AND trade_date=?",
                            (sid, d),
                        ).fetchone()[0]
                    )
                    need5 = max(6, need // 5)
                    if n5 < need5:
                        rows_5m, _ = fetch_tw_intraday_kbar_rows(
                            sid, d_date, d_date, interval="5m"
                        )
                        if rows_5m:
                            stats["kbar_5m_upserts"] += int(
                                upsert_yahoo_5m_rows(con, sid, rows_5m) or 0
                            )
                        if sleep_sec > 0:
                            time.sleep(sleep_sec)
            except Exception:
                stats["errors"] += 1
    return stats


def _path_adverse(
    f: pd.DataFrame, entry_i: int, *, horizons: Sequence[int] = (15, 30)
) -> dict[str, float | None]:
    """Max excess rebound (pp) over next N minutes after entry (higher = worse)."""
    out: dict[str, float | None] = {}
    t0 = _minute_to_mins(str(f["minute"].iloc[entry_i]))
    ex0 = float(f["ex"].iloc[entry_i])
    for h in horizons:
        t1 = t0 + int(h)
        window = f[
            (f["minute"].map(_minute_to_mins) > t0)
            & (f["minute"].map(_minute_to_mins) <= t1)
        ]
        if window.empty:
            out[f"adverse_ex_p{h}"] = None
            continue
        out[f"adverse_ex_p{h}"] = float(window["ex"].max() - ex0)
    return out


def collect_variant_events(
    con: sqlite3.Connection,
    variant: VariantSpec,
    *,
    start: str,
    end: str,
    minute_hi: str | None = None,
    require_exit_anchor: bool = False,
) -> tuple[pd.DataFrame, list[str]]:
    """Collect coverage-style first-entry events for one variant."""
    if variant.bar == "5m" and variant.mode == "first_hit":
        # Reuse frozen collector (any gap/ex threshold); filter end / minute_hi after.
        events, trading = collect_leading_dip_events(
            con,
            start=start,
            apply_structure=True,
            universe_mode="leading",
            trigger="gap_excess",
            excess_anchor="prev_close",
            gap3_max=variant.gap3_max,
            excess_max=variant.excess_max,
            min_path_bars=3,
            require_exit_anchor=require_exit_anchor,
            hold_days=HOLD_DAYS,
        )
        if events.empty:
            return events, [d for d in trading if start <= d <= end]
        events = events[events["date"].astype(str).between(start, end)].copy()
        if minute_hi is not None:
            events = events[events["minute"].astype(str) <= str(minute_hi)].copy()
        trading = [d for d in trading if start <= d <= end]
        events["variant"] = variant.id
        return events, trading

    table = "stock_kbar_1m" if variant.bar == "1m" else "stock_kbar_5m"
    lb_min = int(variant.lookback_minutes or 0)
    min_bars = max(3, lb_min + 2)
    try:
        lookback = (date.fromisoformat(start) - timedelta(days=21)).isoformat()
    except ValueError:
        lookback = start
    close, _, _ = load_price_panels(con)
    bench = load_benchmark_close(con).reindex(close.index).astype(float)
    rv20, mv20, _ = compute_rrg_panel(close, bench, length=20)
    rv5, mv5, _ = compute_rrg_panel(close, bench, length=5)
    full_dates = close.index.astype(str).tolist()
    fidx = {d: i for i, d in enumerate(full_dates)}

    ix = _load_kbar_table(con, "IX0001", lookback, table=table)
    p50 = _load_kbar_table(con, "0050", lookback, table=table)
    # Bench smoothing: force 5m bench onto 1m stock path (only stock "drifts").
    if variant.bar == "1m":
        ix5 = _load_kbar_table(con, "IX0001", lookback, table="stock_kbar_5m")
        p505 = _load_kbar_table(con, "0050", lookback, table="stock_kbar_5m")
        if variant.bench_bar == "5m":
            ix, p50 = ix5, p505
        elif variant.bench_bar == "1m":
            pass
        else:
            # auto: prefer denser source (usually 5m for IX0001)
            if len(ix) < len(ix5):
                ix = ix5
            if len(p50) < len(p505):
                p50 = p505

    bench_days: dict[str, pd.Series] = {}
    bench_src: dict[str, str] = {}
    for d in sorted(set(ix) | set(p50)):
        if d < lookback:
            continue
        need = 20 if d < start else min_bars
        if d in ix and len(ix[d]) >= need:
            bench_days[d] = ix[d]
            bench_src[d] = "IX0001"
        elif d in p50 and len(p50[d]) >= need:
            bench_days[d] = p50[d]
            bench_src[d] = "0050"
    trading_all = sorted(d for d in bench_days if start <= d <= end)
    td_idx = {d: i for i, d in enumerate(sorted(bench_days))}

    lead_by = load_universe_by_baseline(con, universe_mode="leading")
    bases = sorted(lead_by)

    def leading(td: str) -> set[str]:
        c = [b for b in bases if b < td]
        return lead_by[c[-1]] if c else set()

    daily = pd.read_sql(
        "SELECT stock_id,trade_date,close FROM stock_daily_bars WHERE trade_date>=?",
        con,
        params=("2024-11-01",),
    )
    dm: dict[str, dict[str, float]] = {}
    for r in daily.itertuples(index=False):
        dm.setdefault(str(r.stock_id), {})[str(r.trade_date)] = float(r.close)

    def prev_close(sid: str, td: str) -> float | None:
        p = sorted(d for d in dm.get(sid, {}) if d < td)
        return dm[sid][p[-1]] if p else None

    def bprev(td: str) -> float | None:
        src = bench_src[td]
        pr = [d for d in sorted(bench_days) if d < td and bench_src.get(d) == src]
        return float(bench_days[pr[-1]].iloc[-1]) if pr else None

    def fwd(td: str, n: int) -> str | None:
        i = td_idx.get(td)
        keys = sorted(bench_days)
        return keys[i + n] if i is not None and i + n < len(keys) else None

    def tm1(td: str) -> str | None:
        i = fidx.get(td)
        if i is None or i <= 0:
            c = [d for d in full_dates if d < td]
            return c[-1] if c else None
        return full_dates[i - 1]

    ever: set[str] = set()
    for td in trading_all:
        ever |= leading(td)
    if not ever:
        return pd.DataFrame(), trading_all

    qmarks = ",".join("?" * len(ever))
    stock = pd.read_sql(
        f"""SELECT stock_id,trade_date,substr(minute,1,5) AS minute,close FROM {table}
            WHERE trade_date BETWEEN ? AND ? AND stock_id IN ({qmarks})""",
        con,
        params=(start, end, *sorted(ever)),
    )
    if stock.empty:
        return pd.DataFrame(), trading_all
    stock = stock[(stock.minute >= "09:00") & (stock.minute <= "13:30")].drop_duplicates(
        ["stock_id", "trade_date", "minute"], keep="last"
    )
    by = {sid: g for sid, g in stock.groupby("stock_id")}

    LOOKBACK = 60
    rows: list[dict[str, Any]] = []
    for td in trading_all:
        leads = leading(td)
        bp = bprev(td)
        if not leads or bp is None:
            continue
        btoday = bench_days[td]
        # Align 1m stock to denser bench: reindex stock minutes onto bench when needed.
        rb_min = (float(btoday.min()) / bp - 1.0) * 100.0
        t_prev = tm1(td)
        d3e = fwd(td, HOLD_DAYS)
        if require_exit_anchor:
            if (
                not d3e
                or d3e not in bench_days
                or bench_src.get(td) != bench_src.get(d3e)
            ):
                continue
        B3 = float(bench_days[d3e].iloc[-1]) if d3e and d3e in bench_days else float("nan")

        for sid in leads:
            g = by.get(sid)
            if g is None:
                continue
            today = g[g.trade_date == td].sort_values("minute").drop_duplicates("minute")
            if len(today) < min_bars:
                continue
            S0 = prev_close(sid, td)
            if not S0:
                continue
            if t_prev is None or sid not in rv5.columns:
                continue
            si = fidx.get(t_prev)
            if si is None or si < LOOKBACK - 1:
                continue
            f20 = _feat(rv20, mv20, full_dates, si, sid, lb=LOOKBACK)
            if f20 is None:
                continue
            m5 = float(mv5.at[t_prev, sid])
            if m5 != m5:
                continue
            if not passes_structure_filters(
                not_ur=f20["trend"] != "up_right",
                w5_weak=m5 < 100.0,
            ):
                continue

            # Align S onto bench minutes (ffill limited).
            s = today.set_index("minute")["close"].astype(float)
            m = pd.DataFrame({"S": s, "B": btoday.astype(float)}).dropna(how="all")
            # If bench is 5m and stock is 1m, forward-fill bench onto stock minutes.
            if len(m.dropna()) < min_bars:
                aligned = pd.DataFrame({"S": s})
                aligned["B"] = btoday.reindex(aligned.index).ffill().bfill()
                m = aligned.dropna()
            else:
                m = m.dropna()
            if len(m) < min_bars:
                continue
            m = m.reset_index(names="minute")
            if minute_hi is not None:
                m = m[m["minute"].astype(str) <= str(minute_hi)]
                if len(m) < min_bars:
                    continue
            f = build_path_frame(m, S0, bp)
            ex_col = "ex"
            require_gap = True
            if lb_min > 0:
                f = attach_lookback_excess(f, lb_min)
                ex_col = "ex_lb"
                require_gap = bool(variant.require_gap)
            trig = f[ex_col].dropna()
            if trig.empty or float(trig.abs().max()) > MAX_ABS_EX0 * 3:
                continue
            i = resolve_entry_index(
                f,
                mode=variant.mode,
                confirm_bars=variant.confirm_bars,
                wait_minutes=variant.wait_minutes,
                gap3_max=variant.gap3_max,
                excess_max=variant.excess_max,
                ex_col=ex_col,
                require_gap=require_gap,
            )
            if i is None:
                continue
            minute = str(f.loc[i, "minute"])
            ex0 = float(f.loc[i, ex_col])
            if abs(ex0) > MAX_ABS_EX0:
                continue
            px0 = float(f.loc[i, "S"])
            adverse = _path_adverse(f, i)
            ex3 = float("nan")
            px3 = float("nan")
            if d3e and sid in dm and d3e in dm[sid] and d3e in bench_days:
                px3 = float(dm[sid][d3e])
                ex3 = (px3 / S0 - 1.0) * 100.0 - (B3 / bp - 1.0) * 100.0
            if require_exit_anchor and (ex3 != ex3 or px3 != px3):
                continue
            rows.append(
                {
                    "date": td,
                    "sid": sid,
                    "minute": minute,
                    "ex0": ex0,
                    "ex0_yday": float(f.loc[i, "ex"]),
                    "gap0": float(f.loc[i, "gap"]),
                    "px0": px0,
                    "ex3": ex3,
                    "px3": px3,
                    "exit_date": d3e,
                    "T2": is_t2_minute(minute),
                    "rB_min": rb_min,
                    "mild_down": False,
                    "variant": variant.id,
                    "lookback_minutes": lb_min or None,
                    **adverse,
                }
            )

    return pd.DataFrame(rows), trading_all


def _slot_picks(events: pd.DataFrame, trading: list[str]) -> pd.DataFrame:
    if events is None or events.empty:
        return pd.DataFrame()
    day = apply_slot_policy(events, policy=FROZEN_SLOT_POLICY, require_t2=False)
    return apply_total_open_cap(day, trading, max_open=TOTAL_OPEN_CAP)


def _matched_vs_base(
    base: pd.DataFrame, other: pd.DataFrame
) -> list[dict[str, Any]]:
    if base is None or other is None or base.empty or other.empty:
        return []
    b = base.set_index(["date", "sid"], drop=False)
    o = other.set_index(["date", "sid"], drop=False)
    keys = sorted(set(b.index) & set(o.index))
    out: list[dict[str, Any]] = []
    for k in keys:
        br = b.loc[k]
        or_ = o.loc[k]
        if isinstance(br, pd.DataFrame):
            br = br.iloc[0]
        if isinstance(or_, pd.DataFrame):
            or_ = or_.iloc[0]
        dm = _minute_to_mins(str(or_["minute"])) - _minute_to_mins(str(br["minute"]))
        row = {
            "date": str(br["date"]),
            "sid": str(br["sid"]),
            "base_minute": str(br["minute"]),
            "other_minute": str(or_["minute"]),
            "delta_minutes": int(dm),
            "base_ex0": float(br["ex0"]),
            "other_ex0": float(or_["ex0"]),
            "delta_ex0": float(or_["ex0"]) - float(br["ex0"]),
        }
        if "px0" in br and "px0" in or_ and br["px0"] == br["px0"] and or_["px0"] == or_["px0"]:
            row["delta_px_pct"] = (float(or_["px0"]) / float(br["px0"]) - 1.0) * 100.0
        if "ex3" in br and "ex3" in or_:
            be, oe = float(br["ex3"]), float(or_["ex3"])
            if be == be and oe == oe:
                row["base_ex3"] = be
                row["other_ex3"] = oe
                row["delta_ex3"] = oe - be
        out.append(row)
    return out


def _variant_scorecard(
    picks: pd.DataFrame, *, oos_cut: str = "2099-01-01"
) -> dict[str, Any]:
    if picks is None or picks.empty:
        return {"n": 0, "hit": None, "med_ex3": None, "med_ex0": None, "mean_minute": None}
    s = summarize_trades(picks, oos_cut=oos_cut)
    return {
        "n": int(len(picks)),
        "hit": s.get("hit"),
        "med_ex3": s.get("med"),
        "med_ex0": float(picks["ex0"].median()) if "ex0" in picks else None,
        "mean_minute": (
            float(picks["minute"].map(_minute_to_mins).mean()) if "minute" in picks else None
        ),
        "mean_adverse_p15": (
            float(pd.to_numeric(picks.get("adverse_ex_p15"), errors="coerce").mean())
            if "adverse_ex_p15" in picks.columns
            else None
        ),
        "mean_adverse_p30": (
            float(pd.to_numeric(picks.get("adverse_ex_p30"), errors="coerce").mean())
            if "adverse_ex_p30" in picks.columns
            else None
        ),
    }


def run_study(
    con: sqlite3.Connection,
    *,
    start: str = DEFAULT_WINDOW_START,
    end: str = DEFAULT_FOCUS_DAY,
    focus_day: str = DEFAULT_FOCUS_DAY,
    minute_hi: str | None = None,
    variants: Sequence[VariantSpec] = DEFAULT_VARIANTS,
) -> dict[str, Any]:
    if minute_hi is None and focus_day == datetime.now(TZ).date().isoformat():
        # morning case: cut at current minute (rounded)
        minute_hi = datetime.now(TZ).strftime("%H:%M")

    lead = leading_ids_for_day(con, focus_day)
    cov = coverage_report(con, start=start, end=end, universe=lead)

    by_variant: dict[str, Any] = {}
    picks_by: dict[str, pd.DataFrame] = {}
    for v in variants:
        events, trading = collect_variant_events(
            con,
            v,
            start=start,
            end=end,
            minute_hi=minute_hi if v.bar == "1m" or focus_day == end else None,
            require_exit_anchor=False,
        )
        # For 5m baseline on focus morning, also apply minute_hi for fair compare.
        if minute_hi is not None and not events.empty:
            events = events[events["minute"].astype(str) <= str(minute_hi)].copy()
        picks = _slot_picks(events, trading)
        picks_by[v.id] = picks
        focus_picks = (
            picks[picks["date"].astype(str) == focus_day].copy() if not picks.empty else picks
        )
        by_variant[v.id] = {
            "label": v.label,
            "spec": {
                "bar": v.bar,
                "mode": v.mode,
                "confirm_bars": v.confirm_bars,
                "wait_minutes": v.wait_minutes,
                "gap3_max": v.gap3_max,
                "excess_max": v.excess_max,
            },
            "window": _variant_scorecard(picks),
            "focus": _variant_scorecard(focus_picks),
            "focus_picks": (
                focus_picks[
                    [
                        c
                        for c in (
                            "sid",
                            "minute",
                            "ex0",
                            "gap0",
                            "px0",
                            "ex3",
                            "adverse_ex_p15",
                            "adverse_ex_p30",
                        )
                        if c in focus_picks.columns
                    ]
                ].to_dict("records")
                if not focus_picks.empty
                else []
            ),
            "n_raw_events": int(len(events)) if events is not None else 0,
        }

    base = picks_by.get("ldip_5m_cov", pd.DataFrame())
    matched: dict[str, Any] = {}
    for vid, picks in picks_by.items():
        if vid == "ldip_5m_cov":
            continue
        pairs = _matched_vs_base(base, picks)
        if not pairs:
            matched[vid] = {"n_matched": 0}
            continue
        df = pd.DataFrame(pairs)
        matched[vid] = {
            "n_matched": int(len(df)),
            "mean_delta_minutes": float(df["delta_minutes"].mean()),
            "pct_earlier": float((df["delta_minutes"] < 0).mean()),
            "mean_delta_ex0": float(df["delta_ex0"].mean()),
            "pct_deeper_ex0": float((df["delta_ex0"] < 0).mean()),
            "mean_delta_ex3": (
                float(df["delta_ex3"].mean()) if "delta_ex3" in df.columns else None
            ),
            "pairs": pairs[:40],
        }

    verdict = _verdict(by_variant, matched, focus_day=focus_day)
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "start": start,
        "end": end,
        "focus_day": focus_day,
        "minute_hi": minute_hi,
        "leading_n": len(lead),
        "coverage": cov,
        "variants": by_variant,
        "matched_vs_5m": matched,
        "verdict": verdict,
        "notes": [
            "Research only · Book DB · not Order live",
            "Baseline = leading_dip_cov (5m first_hit · gap≤−0.80 ∩ ex≤−4 · drop T2)",
            "1m WMA3 is a different timescale than 5m WMA3 — naive port is a new signal",
        ],
    }


def _verdict(
    by_variant: dict[str, Any],
    matched: dict[str, Any],
    *,
    focus_day: str,
) -> dict[str, Any]:
    base = by_variant.get("ldip_5m_cov", {}).get("focus", {})
    winners: list[str] = []
    reasons: list[str] = []
    for vid, block in by_variant.items():
        if vid == "ldip_5m_cov":
            continue
        sc = block.get("focus") or {}
        m = matched.get(vid) or {}
        # Beat heuristics for morning case / short window
        deeper = (m.get("mean_delta_ex0") is not None) and m["mean_delta_ex0"] < -0.15
        earlier = (m.get("mean_delta_minutes") is not None) and m[
            "mean_delta_minutes"
        ] <= -2
        ex3_ok = m.get("mean_delta_ex3")
        if ex3_ok is not None and ex3_ok > 0.25 and m.get("n_matched", 0) >= 3:
            winners.append(vid)
            reasons.append(f"{vid}: matched mean Δex3={ex3_ok:+.2f}")
        elif deeper and earlier and m.get("n_matched", 0) >= 2:
            # provisional — only if adverse not worse
            adv = sc.get("mean_adverse_p15")
            base_adv = base.get("mean_adverse_p15")
            if adv is None or base_adv is None or adv <= base_adv + 0.5:
                winners.append(vid)
                reasons.append(
                    f"{vid}: earlier ({m['mean_delta_minutes']:+.1f}m) + deeper "
                    f"ex0 ({m['mean_delta_ex0']:+.2f}) · provisional same-day"
                )
    if not winners:
        reasons.append(
            f"{focus_day}: no 1m variant clears beat heuristics vs ldip_5m_cov yet"
        )
    return {
        "beat_baseline": bool(winners),
        "winner_ids": winners,
        "reasons": reasons,
        "recommendation": (
            "promote_candidate_for_longer_wfa"
            if winners
            else "keep_5m_leading_dip · continue 1m research only"
        ),
    }


BACKTEST_START = "2025-01-02"
BACKTEST_END = "2026-07-15"
BACKTEST_OOS_CUT = "2025-10-01"

BACKTEST_VARIANTS: tuple[VariantSpec, ...] = (
    VariantSpec("5m_g080_e4", "5m · frozen cov -0.80/-4", "5m"),
    VariantSpec(
        "5m_g090_e6",
        "5m · -0.90/-6 (same thr control)",
        "5m",
        gap3_max=-0.90,
        excess_max=-6.0,
    ),
    VariantSpec("1m_g080_e4", "1m · -0.80/-4 naive", "1m"),
    VariantSpec(
        "1m_g090_e6",
        "1m · -0.90/-6 (candidate)",
        "1m",
        gap3_max=-0.90,
        excess_max=-6.0,
    ),
    VariantSpec(
        "1m_g100_e6",
        "1m · -1.00/-6",
        "1m",
        gap3_max=-1.00,
        excess_max=-6.0,
    ),
    VariantSpec(
        "1m_g090_e6_w15",
        "1m · -0.90/-6 + wait<=15m deepen",
        "1m",
        mode="wait_deepen",
        wait_minutes=15,
        gap3_max=-0.90,
        excess_max=-6.0,
    ),
)


def _lb(
    vid: str,
    label: str,
    *,
    k: int,
    thr: float,
    require_gap: bool = False,
    gap3_max: float = GAP3_MAX,
) -> VariantSpec:
    return VariantSpec(
        vid,
        label,
        "1m",
        excess_max=thr,
        gap3_max=gap3_max,
        lookback_minutes=k,
        require_gap=require_gap,
        bench_bar="5m",
    )


# Local-dip design: depth vs t−K (not yday/open) · deeper thr · 5m-smoothed bench
LOOKBACK_BACKTEST_VARIANTS: tuple[VariantSpec, ...] = (
    VariantSpec("5m_g080_e4", "5m · frozen cov -0.80/-4 (baseline)", "5m"),
    _lb("1m_lb5_e3", "1m · vs t-5m ≤-3", k=5, thr=-3.0),
    _lb("1m_lb5_e4", "1m · vs t-5m ≤-4", k=5, thr=-4.0),
    _lb("1m_lb5_e5", "1m · vs t-5m ≤-5", k=5, thr=-5.0),
    _lb("1m_lb15_e4", "1m · vs t-15m ≤-4", k=15, thr=-4.0),
    _lb("1m_lb15_e5", "1m · vs t-15m ≤-5", k=15, thr=-5.0),
    _lb("1m_lb15_e6", "1m · vs t-15m ≤-6", k=15, thr=-6.0),
    _lb("1m_lb30_e5", "1m · vs t-30m ≤-5", k=30, thr=-5.0),
    _lb("1m_lb30_e6", "1m · vs t-30m ≤-6", k=30, thr=-6.0),
    _lb(
        "1m_lb15_e5_g09",
        "1m · vs t-15m ≤-5 ∧ gap≤-0.90",
        k=15,
        thr=-5.0,
        require_gap=True,
        gap3_max=-0.90,
    ),
)


def run_threshold_backtest(
    con: sqlite3.Connection,
    *,
    start: str = BACKTEST_START,
    end: str = BACKTEST_END,
    oos_cut: str = BACKTEST_OOS_CUT,
    variants: Sequence[VariantSpec] = BACKTEST_VARIANTS,
) -> dict[str, Any]:
    """Full-window coverage slots · T+3 · compare 1m tight thr vs 5m baseline."""
    rows: list[dict[str, Any]] = []
    by_id: dict[str, Any] = {}
    for v in variants:
        print(f"collect {v.id} …", flush=True)
        events, trading = collect_variant_events(
            con,
            v,
            start=start,
            end=end,
            minute_hi=None,
            require_exit_anchor=True,
        )
        picks = _slot_picks(events, trading)
        # Drop rows still missing ex3 (safety)
        if not picks.empty and "ex3" in picks.columns:
            picks = picks[picks["ex3"].notna()].copy()
        summ = summarize_trades(picks, oos_cut=oos_cut)
        med_ex0 = float(picks["ex0"].median()) if len(picks) else None
        block = {
            "label": v.label,
            "spec": {
                "bar": v.bar,
                "mode": v.mode,
                "wait_minutes": v.wait_minutes,
                "gap3_max": v.gap3_max,
                "excess_max": v.excess_max,
                "lookback_minutes": v.lookback_minutes,
                "require_gap": v.require_gap,
                "bench_bar": v.bench_bar,
            },
            "n_raw": int(len(events)) if events is not None else 0,
            "n": int(summ.get("n") or 0),
            "hit": summ.get("hit"),
            "med_ex3": summ.get("med"),
            "mean_ex3": summ.get("mean"),
            "med_ex0": med_ex0,
            "oos_n": summ.get("oos_n"),
            "oos_hit": summ.get("oos_hit"),
            "oos_med": summ.get("oos_med"),
            "days": summ.get("days"),
        }
        by_id[v.id] = block
        rows.append({"id": v.id, **block})
        print(
            f"  {v.id}: n={block['n']} hit={block['hit']} med={block['med_ex3']} "
            f"oos_n={block['oos_n']} oos_hit={block['oos_hit']} oos_med={block['oos_med']}",
            flush=True,
        )

    base = by_id.get("5m_g080_e4") or {}
    # Best non-baseline by OOS med (min n=20)
    cand_id = None
    cand: dict[str, Any] = {}
    best_med = float("-inf")
    for vid, block in by_id.items():
        if vid == "5m_g080_e4":
            continue
        if int(block.get("n") or 0) < 20 or block.get("oos_med") is None:
            continue
        m = float(block["oos_med"])
        if m > best_med:
            best_med = m
            cand_id = vid
            cand = block
    beat = False
    reasons: list[str] = []
    if (
        cand_id
        and base.get("oos_med") is not None
        and cand.get("oos_med") is not None
        and float(cand["oos_med"]) > float(base["oos_med"]) + 0.25
        and float(cand.get("oos_hit") or 0) >= float(base.get("oos_hit") or 0) - 0.02
    ):
        beat = True
        reasons.append(
            f"{cand_id} OOS med {cand['oos_med']:+.2f} > baseline "
            f"{base['oos_med']:+.2f} · hit {cand.get('oos_hit')}"
        )
    if not beat:
        reasons.append(
            "no 1m variant clears OOS med/hit vs 5m frozen −0.80∩−4 "
            f"(best={cand_id} oos_med={cand.get('oos_med')} n={cand.get('n')} · "
            f"base oos_med={base.get('oos_med')} n={base.get('n')})"
        )

    return {
        "schema": "leading_dip_1m_threshold_backtest-v1",
        "generated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "start": start,
        "end": end,
        "oos_cut": oos_cut,
        "hold_days": HOLD_DAYS,
        "variants": by_id,
        "table": rows,
        "verdict": {
            "beat_baseline": beat,
            "best_candidate": cand_id,
            "recommendation": (
                f"promote_{cand_id}_for_wfa" if beat else "keep_5m_leading_dip"
            ),
            "reasons": reasons,
        },
    }


def render_backtest_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    schema = str(payload.get("schema") or "")
    title = (
        "# Leading Dip · 1m lookback local-dip backtest（vs t−K · vs 5m cov）"
        if "lookback" in schema
        else "# Leading Dip · 1m threshold backtest（vs 5m cov）"
    )
    lines = [
        title,
        "",
        f"日期：{payload.get('generated_at', '')[:10]} · Research only · **未採納**",
        "",
        "## Verdict",
        "",
        f"**{'beat_candidate' if v.get('beat_baseline') else 'no_beat'}** — "
        f"{v.get('recommendation')}",
        "",
    ]
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    if payload.get("design"):
        lines.extend(["", f"- Design: {payload['design']}"])
    lines.extend(
        [
            "",
            "## Contract",
            "",
            f"- Window `{payload.get('start')}` … `{payload.get('end')}` · "
            f"OOS cut `{payload.get('oos_cut')}` · hold T+{payload.get('hold_days')}",
            "- Structure = Leading ∧ notUR ∧ W5_MV<100 · coverage（drop T2）· "
            "日槽 deepest · 總在外≤6",
            "- 1m = first_hit on `stock_kbar_1m`（lookback 變體：bench 強制 5m）",
            "",
            "## Results",
            "",
            "| id | bar | lb | thr | gap? | n | hit | med_ex3 | med_ex0 | OOS n/hit/med |",
            "|--|--|--:|--:|--|--:|--:|--:|--:|--|",
        ]
    )
    for row in payload.get("table") or []:
        sp = row.get("spec") or {}
        oos = (
            f"{row.get('oos_n')}/"
            f"{_fmt_pct(row.get('oos_hit'))}/"
            f"{_fmt(row.get('oos_med'))}"
        )
        lb = sp.get("lookback_minutes")
        gap_s = (
            f"≤{sp.get('gap3_max')}"
            if sp.get("require_gap") or lb is None
            else "—"
        )
        lines.append(
            f"| {row.get('id')} | {sp.get('bar')} | {lb if lb is not None else '—'} | "
            f"{sp.get('excess_max')} | {gap_s} | {row.get('n')} | "
            f"{_fmt_pct(row.get('hit'))} | {_fmt(row.get('med_ex3'))} | "
            f"{_fmt(row.get('med_ex0'))} | {oos} |"
        )
    lines.extend(
        [
            "",
            "## Re-run",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_1m_beat_study.py --lookback-backtest --write",
            "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_1m_beat_study.py --backtest --write",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def render_md(payload: dict[str, Any]) -> str:
    v = payload.get("verdict") or {}
    lines = [
        "# Leading Dip · 1m / 1-min poll beat study",
        "",
        f"日期：{payload.get('generated_at', '')[:10]} · Research only · **未採納**",
        "",
        "## Verdict",
        "",
        f"**{'beat_candidate' if v.get('beat_baseline') else 'no_beat_yet'}** — "
        f"{v.get('recommendation')}",
        "",
    ]
    for r in v.get("reasons") or []:
        lines.append(f"- {r}")
    lines.extend(
        [
            "",
            "## Scope",
            "",
            f"- Window `{payload.get('start')}` … `{payload.get('end')}` · "
            f"focus `{payload.get('focus_day')}` · minute_hi=`{payload.get('minute_hi')}`",
            f"- Leading universe n={payload.get('leading_n')}",
            "",
            "## Coverage",
            "",
            "| day | n_1m | n_5m | daily_n |",
            "|--|--:|--:|--:|",
        ]
    )
    for d, row in (payload.get("coverage") or {}).get("days", {}).items():
        lines.append(
            f"| {d} | {row.get('n_1m')} | {row.get('n_5m')} | {row.get('daily_n')} |"
        )
    lines.extend(
        [
            "",
            "## Focus-day scorecard",
            "",
            "| variant | n | med_ex0 | mean_min | adv+15 | adv+30 | med_ex3 |",
            "|--|--:|--:|--:|--:|--:|--:|",
        ]
    )
    for vid, block in (payload.get("variants") or {}).items():
        sc = block.get("focus") or {}
        lines.append(
            f"| {vid} | {sc.get('n')} | {_fmt(sc.get('med_ex0'))} | "
            f"{_fmt_min(sc.get('mean_minute'))} | {_fmt(sc.get('mean_adverse_p15'))} | "
            f"{_fmt(sc.get('mean_adverse_p30'))} | {_fmt(sc.get('med_ex3'))} |"
        )
    lines.extend(
        [
            "",
            "## Matched vs 5m baseline",
            "",
            "| variant | n | mean Δmin | %earlier | mean Δex0 | %deeper | mean Δex3 |",
            "|--|--:|--:|--:|--:|--:|--:|",
        ]
    )
    for vid, m in (payload.get("matched_vs_5m") or {}).items():
        lines.append(
            f"| {vid} | {m.get('n_matched')} | {_fmt(m.get('mean_delta_minutes'))} | "
            f"{_fmt_pct(m.get('pct_earlier'))} | {_fmt(m.get('mean_delta_ex0'))} | "
            f"{_fmt_pct(m.get('pct_deeper_ex0'))} | {_fmt(m.get('mean_delta_ex3'))} |"
        )
    lines.extend(["", "## Focus picks (baseline)", ""])
    base_picks = (
        (payload.get("variants") or {}).get("ldip_5m_cov", {}).get("focus_picks") or []
    )
    if not base_picks:
        lines.append("_none_")
    else:
        for r in base_picks:
            lines.append(
                f"- `{r.get('sid')}` @{r.get('minute')} ex0={_fmt(r.get('ex0'))} "
                f"gap={_fmt(r.get('gap0'))}"
            )
    lines.extend(
        [
            "",
            "## Re-run",
            "",
            "```bash",
            "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_1m_beat_study.py --backfill",
            "PYTHONPATH=src .venv/bin/python scripts/research/run_leading_dip_1m_beat_study.py --write",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def _fmt(x: Any) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    try:
        return f"{float(x):+.2f}"
    except (TypeError, ValueError):
        return str(x)


def _fmt_pct(x: Any) -> str:
    if x is None:
        return "—"
    try:
        return f"{100 * float(x):.0f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_min(x: Any) -> str:
    if x is None:
        return "—"
    try:
        return _mins_to_minute(int(round(float(x))))
    except (TypeError, ValueError):
        return "—"
