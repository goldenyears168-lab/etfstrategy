#!/usr/bin/env python3
"""vwap_zscore_fade — 30分鐘VWAP均值回歸方向偏向公式 · PIT directed-hit eval.

**研究用，僅本次任務新增的獨立腳本，未修改任何既有程式檔案。**

公式（外部agent設計，見任務說明）
--------------------------------
1. Session VWAP（累積、PIT）：typical=(H+L+C)/3，VWAP_t=Σ(typical·vol)/Σvol
2. dev_t = close_t - VWAP_t；σ_t = stdev(dev) either expanding 或 rolling12（IS網格）
3. z_t = dev_t / max(σ_t, close_t*0.0005)；READY_BARS=10 才開始算 z
4. signal_raw：z_t ∈ (Z_ENTRY, Z_MAX) → -1（貼上緣回落）；
   z_t ∈ (-Z_MAX, -Z_ENTRY) → +1（貼下緣反彈）；Z_ENTRY ∈ {1.5,2.0,2.5}（IS網格），Z_MAX=3.0 固定
5. confirm：sign(z_t)==sign(z_{t-1}) AND |z_t|<|z_{t-1}|（收斂確認）
6. thrust_block：run_len(同向收紅/黑,≤6根)≥5 AND vol_ratio_t≥1.3 AND vol_ratio_{t-1}≥1.3
   → 判定趨勢日，signal歸零
7. 時段守衛（IS網格互斥假設）：
   - "full"：open_guard=6根(09:00-09:30不發訊)＋close_guard=13:15後不新開訊號
   - "midday"：僅10:00-12:30發訊
8. temp = signal_raw if (confirm AND NOT thrust_block AND time_guard_ok) else 0

評估口徑（比照 run_ta_30m_bias_backtest.py，不另立標準）
--------------------------------------------------------
- Forward: close[i+H]/close[i]-1，H=6（≈30m @ 5m bars）
- PIT：因子僅用當日已完成 bars ≤ i
- Directed hit：temp∈{±1} 且 |fwd|≥flat_eps(0.05%) 中，sign(temp)==sign(fwd) 之比例
- IS ≤ 2025-09-30 / OOS > 2025-09-30；champion 只在 IS 選（n≥200 中 IS hit 最高者），
  OOS 只驗證、不可回頭挑
- Gates：OOS directed hit≥70% 且 n≥500；≥70%個股 OOS hit≥55%(n≥50 each)；單股佔比≤40%
- Universe：8檔全宇宙 2327,8046,3189,6451,2330,2303,2454,0050（不縮小名單）

Example
-------
  PYTHONPATH=src .venv/bin/python scripts/research/run_vwap_zscore_fade_backtest.py
"""

from __future__ import annotations

import argparse
import json
import statistics
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.intraday_direction_thermometer import Bar  # noqa: E402

DEFAULT_STOCKS = (
    "2327",
    "8046",
    "3189",
    "6451",
    "2330",
    "2303",
    "2454",
    "0050",
)
DEFAULT_IS_END = "2025-09-30"
FLAT_EPS = 0.0005  # 0.05%
MIN_BARS_PER_DAY = 30
MIN_OOS_N = 500
MIN_NAME_N = 50
NAME_HIT_FLOOR = 55.0
NAME_PASS_FRAC = 0.70
MAX_NAME_SHARE = 0.40
HORIZON = 6  # ~30m @ 5m bars

# Fixed constants per spec (not part of IS grid).
READY_BARS = 10
Z_MAX = 3.0
VOL_RATIO_THRESH = 1.3
RUN_LEN_LOOKBACK = 6
RUN_LEN_THRESH = 5
OPEN_GUARD_BARS = 6
CLOSE_GUARD_HM = 13 * 60 + 15
MIDDAY_START_HM = 10 * 60
MIDDAY_END_HM = 12 * 60 + 30
ROLLING_WINDOW = 12


def _minute_hhmm(minute: str) -> str:
    parts = str(minute).split(":")
    return f"{parts[0]}:{parts[1]}"


def load_days(
    conn: sqlite3.Connection, stock_id: str, *, start: str, end: str
) -> dict[str, list[Bar]]:
    rows = conn.execute(
        """
        SELECT trade_date, minute, open, high, low, close, volume
        FROM stock_kbar_5m
        WHERE stock_id = ? AND trade_date >= ? AND trade_date <= ?
        ORDER BY trade_date, minute
        """,
        (stock_id, start, end),
    ).fetchall()
    by_day: dict[str, list[Bar]] = defaultdict(list)
    for td, minute, o, h, l, c, v in rows:
        if c is None:
            continue
        hhmm = _minute_hhmm(minute)
        hm = int(hhmm[:2]) * 60 + int(hhmm[3:5])
        if hm < 9 * 60 or hm > 13 * 60 + 25:
            continue
        ts = datetime.strptime(f"{td} {hhmm}", "%Y-%m-%d %H:%M")
        by_day[str(td)].append(
            Bar(
                ts=ts,
                open=float(o or c),
                high=float(h or c),
                low=float(l or c),
                close=float(c),
                volume=float(v or 0),
            )
        )
    return {d: bars for d, bars in by_day.items() if len(bars) >= MIN_BARS_PER_DAY}


@dataclass
class DayCtx:
    bars: list[Bar]
    closes: list[float]
    hm: list[int]
    dev: list[float]
    z_expanding: list[float | None]
    z_rolling12: list[float | None]
    vol_ratio: list[float]
    run_len: list[int]
    thrust_block: list[bool]


def _sign(x: float) -> int:
    if x > 0:
        return 1
    if x < 0:
        return -1
    return 0


def build_day_ctx(bars: list[Bar]) -> DayCtx:
    n = len(bars)
    closes = [b.close for b in bars]
    hms = [b.ts.hour * 60 + b.ts.minute for b in bars]

    # Step1: session VWAP (cumulative, PIT) + dev.
    vwap_num = vwap_den = 0.0
    dev: list[float] = []
    for b in bars:
        tp = (b.high + b.low + b.close) / 3.0
        v = float(b.volume or 0.0)
        if v <= 0:
            v = 1.0
        vwap_num += tp * v
        vwap_den += v
        vwap_t = vwap_num / vwap_den if vwap_den > 0 else b.close
        dev.append(b.close - vwap_t)

    # Step2: z-score, two sigma-window variants.
    z_expanding: list[float | None] = [None] * n
    z_rolling12: list[float | None] = [None] * n
    for i in range(n):
        if i + 1 < READY_BARS:
            continue
        floor = max(1e-9, closes[i] * 0.0005)
        exp_window = dev[: i + 1]
        if len(exp_window) >= 2:
            sigma_e = statistics.stdev(exp_window)
            z_expanding[i] = dev[i] / max(sigma_e, floor)
        start = max(0, i + 1 - ROLLING_WINDOW)
        roll_window = dev[start : i + 1]
        if len(roll_window) >= 2:
            sigma_r = statistics.stdev(roll_window)
            z_rolling12[i] = dev[i] / max(sigma_r, floor)

    # Step3: volume ratio (window t-5..t, 6 bars incl current).
    vols = [float(b.volume or 0.0) for b in bars]
    vol_ratio: list[float] = [1.0] * n
    for i in range(n):
        start = max(0, i - 5)
        window = vols[start : i + 1]
        mean_v = sum(window) / len(window) if window else 0.0
        vol_ratio[i] = (vols[i] / mean_v) if mean_v > 0 else 1.0

    # Step4: run length of same-colour (red/black) closes, cap lookback=6.
    colors = [1 if b.close > b.open else (-1 if b.close < b.open else 0) for b in bars]
    run_len: list[int] = [0] * n
    for i in range(n):
        if colors[i] == 0:
            run_len[i] = 0
            continue
        cnt = 0
        j = i
        while j >= 0 and colors[j] == colors[i] and cnt < RUN_LEN_LOOKBACK:
            cnt += 1
            j -= 1
        run_len[i] = cnt

    # Step5: thrust block (trend-day exclusion).
    thrust_block: list[bool] = [False] * n
    for i in range(n):
        if i == 0:
            continue
        thrust_block[i] = (
            run_len[i] >= RUN_LEN_THRESH
            and vol_ratio[i] >= VOL_RATIO_THRESH
            and vol_ratio[i - 1] >= VOL_RATIO_THRESH
        )

    return DayCtx(
        bars=bars,
        closes=closes,
        hm=hms,
        dev=dev,
        z_expanding=z_expanding,
        z_rolling12=z_rolling12,
        vol_ratio=vol_ratio,
        run_len=run_len,
        thrust_block=thrust_block,
    )


@dataclass(frozen=True)
class Variant:
    name: str
    z_entry: float
    sigma_mode: str  # "expanding" | "rolling12"
    session_guard: str  # "full" | "midday"
    use_confirm: bool
    use_thrust_block: bool
    family: str = "grid"


def _time_guard_ok(i: int, hm: int, session_guard: str) -> bool:
    if session_guard == "midday":
        return MIDDAY_START_HM <= hm <= MIDDAY_END_HM
    # "full": open guard + close guard
    if i < OPEN_GUARD_BARS:
        return False
    if hm >= CLOSE_GUARD_HM:
        return False
    return True


def signal_temp_at(ctx: DayCtx, i: int, variant: Variant) -> int:
    z_series = ctx.z_expanding if variant.sigma_mode == "expanding" else ctx.z_rolling12
    z_t = z_series[i]
    if z_t is None:
        return 0

    signal_raw = 0
    if variant.z_entry <= z_t < Z_MAX:
        signal_raw = -1
    elif -Z_MAX < z_t <= -variant.z_entry:
        signal_raw = 1
    if signal_raw == 0:
        return 0

    if variant.use_confirm:
        z_prev = z_series[i - 1] if i > 0 else None
        if z_prev is None:
            return 0
        confirm = _sign(z_t) == _sign(z_prev) and abs(z_t) < abs(z_prev)
        if not confirm:
            return 0

    if variant.use_thrust_block and ctx.thrust_block[i]:
        return 0

    if not _time_guard_ok(i, ctx.hm[i], variant.session_guard):
        return 0

    return signal_raw


# Pre-registered grid (locked before OOS read for champion selection).
# Z_ENTRY x sigma_mode x session_guard x confirm x thrust_block = 3*2*2*2*2 = 48
VARIANTS: tuple[Variant, ...] = tuple(
    Variant(
        name=(
            f"z{z_entry}_{sigma_mode}_{session_guard}"
            f"_{'cf' if use_confirm else 'noc'}_{'tb' if use_thrust_block else 'not'}"
        ),
        z_entry=z_entry,
        sigma_mode=sigma_mode,
        session_guard=session_guard,
        use_confirm=use_confirm,
        use_thrust_block=use_thrust_block,
    )
    for z_entry in (1.5, 2.0, 2.5)
    for sigma_mode in ("expanding", "rolling12")
    for session_guard in ("full", "midday")
    for use_confirm in (True, False)
    for use_thrust_block in (True, False)
)


def eval_day(ctx: DayCtx, variant: Variant, *, horizon: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    n = len(ctx.bars)
    for i in range(n):
        if i + horizon >= n:
            break
        temp = signal_temp_at(ctx, i, variant)
        if temp == 0:
            continue
        c0 = ctx.closes[i]
        if not c0:
            continue
        fwd = ctx.closes[i + horizon] / c0 - 1.0
        out.append(
            {
                "temp": int(temp),
                "fwd": fwd,
                "hhmm": ctx.bars[i].ts.strftime("%H:%M"),
                "bars_elapsed": i + 1,
            }
        )
    return out


def summarize(rows: list[dict[str, Any]], *, flat_eps: float) -> dict[str, Any]:
    directed_hit = directed_tot = 0
    long_hit = 0
    signed_pnl: list[float] = []
    for r in rows:
        t = int(r["temp"])
        fr = float(r["fwd"])
        if t == 0 or abs(fr) < flat_eps:
            continue
        directed_tot += 1
        if (t > 0 and fr > 0) or (t < 0 and fr < 0):
            directed_hit += 1
        if fr > 0:
            long_hit += 1
        signed_pnl.append(fr if t > 0 else -fr)

    def pct(a: int, b: int) -> float | None:
        return round(100.0 * a / b, 2) if b else None

    d_pct = pct(directed_hit, directed_tot)
    al = pct(long_hit, directed_tot)
    return {
        "n_signals": len(rows),
        "n_directed": directed_tot,
        "n_hit": directed_hit,
        "directed_hit_pct": d_pct,
        "always_long_pct": al,
        "expectancy_signed_pct": (
            round(100.0 * sum(signed_pnl) / len(signed_pnl), 4) if signed_pnl else None
        ),
        "edge_vs_coin_pp": round(d_pct - 50.0, 2) if d_pct is not None else None,
        "edge_vs_always_long_pp": (
            round(d_pct - al, 2) if d_pct is not None and al is not None else None
        ),
    }


def run_stock(
    conn: sqlite3.Connection,
    stock_id: str,
    *,
    start: str,
    end: str,
    is_end: str,
    variants: Sequence[Variant],
    horizon: int,
    flat_eps: float,
) -> dict[str, Any]:
    days = load_days(conn, stock_id, start=start, end=end)
    if not days:
        return {"stock_id": stock_id, "error": "no_5m", "n_days": 0}

    by_var: dict[str, dict[str, list[dict[str, Any]]]] = {
        v.name: {"IS": [], "OOS": []} for v in variants
    }
    for td, bars in sorted(days.items()):
        split = "IS" if td <= is_end else "OOS"
        ctx = build_day_ctx(bars)
        for v in variants:
            by_var[v.name][split].extend(eval_day(ctx, v, horizon=horizon))

    out_vars: dict[str, Any] = {}
    for v in variants:
        is_rows = by_var[v.name]["IS"]
        oos_rows = by_var[v.name]["OOS"]
        out_vars[v.name] = {
            "IS": summarize(is_rows, flat_eps=flat_eps),
            "OOS": summarize(oos_rows, flat_eps=flat_eps),
        }
    return {
        "stock_id": stock_id,
        "n_days": len(days),
        "date_min": min(days),
        "date_max": max(days),
        "variants": out_vars,
    }


def pool_from_stocks(
    stock_results: list[dict[str, Any]], variant: str, split: str
) -> dict[str, Any]:
    n_dir = hit = long_hit = n_sig = 0
    per_name: list[dict[str, Any]] = []
    for s in stock_results:
        if s.get("error"):
            continue
        m = s["variants"][variant][split]
        nd = int(m["n_directed"] or 0)
        nh = int(m.get("n_hit") or 0)
        ns = int(m["n_signals"] or 0)
        n_sig += ns
        n_dir += nd
        hit += nh
        if m["always_long_pct"] is not None and nd:
            long_hit += int(round(m["always_long_pct"] / 100.0 * nd))
        per_name.append(
            {
                "stock_id": s["stock_id"],
                "n_directed": nd,
                "directed_hit_pct": m["directed_hit_pct"],
                "expectancy_signed_pct": m["expectancy_signed_pct"],
            }
        )

    def pct(a: int, b: int) -> float | None:
        return round(100.0 * a / b, 2) if b else None

    d_pct = pct(hit, n_dir)
    al = pct(long_hit, n_dir)
    return {
        "n_signals": n_sig,
        "n_directed": n_dir,
        "n_hit": hit,
        "directed_hit_pct": d_pct,
        "always_long_pct": al,
        "edge_vs_coin_pp": round(d_pct - 50.0, 2) if d_pct is not None else None,
        "edge_vs_always_long_pp": (
            round(d_pct - al, 2) if d_pct is not None and al is not None else None
        ),
        "per_name": per_name,
    }


def stability_gates(pool_oos: dict[str, Any]) -> dict[str, Any]:
    names = [p for p in pool_oos.get("per_name") or [] if (p.get("n_directed") or 0) > 0]
    n_names = len(names)
    pass_names = [
        p
        for p in names
        if (p.get("n_directed") or 0) >= MIN_NAME_N
        and (p.get("directed_hit_pct") or 0) >= NAME_HIT_FLOOR
    ]
    frac = (len(pass_names) / n_names) if n_names else 0.0
    max_share = 0.0
    max_sid = None
    nd_pool = int(pool_oos.get("n_directed") or 0)
    if nd_pool:
        for p in names:
            share = (p.get("n_directed") or 0) / nd_pool
            if share > max_share:
                max_share = share
                max_sid = p["stock_id"]
    hit = pool_oos.get("directed_hit_pct")
    gate_hit = bool(hit is not None and hit >= 70.0 and nd_pool >= MIN_OOS_N)
    gate_stab = bool(n_names > 0 and frac >= NAME_PASS_FRAC)
    gate_dom = bool(max_share <= MAX_NAME_SHARE)
    return {
        "gate_oos_hit_ge70_n500": gate_hit,
        "gate_name_stability": gate_stab,
        "gate_no_megacap_dom": gate_dom,
        "all_gates_pass": gate_hit and gate_stab and gate_dom,
        "name_pass_frac": round(frac, 3),
        "n_names": n_names,
        "n_names_pass": len(pass_names),
        "max_name_share": round(max_share, 3),
        "max_name_sid": max_sid,
        "names_pass": [p["stock_id"] for p in pass_names],
        "names_fail": [
            {
                "stock_id": p["stock_id"],
                "hit": p.get("directed_hit_pct"),
                "n": p.get("n_directed"),
            }
            for p in names
            if p["stock_id"] not in {x["stock_id"] for x in pass_names}
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(ROOT / "data" / "stocks.db"))
    ap.add_argument("--stocks", default=",".join(DEFAULT_STOCKS))
    ap.add_argument("--start", default="2024-01-02")
    ap.add_argument("--end", default="")
    ap.add_argument("--is-end", default=DEFAULT_IS_END)
    ap.add_argument("--horizon", type=int, default=HORIZON)
    ap.add_argument("--flat-eps", type=float, default=FLAT_EPS)
    ap.add_argument(
        "--out-json",
        default=str(
            ROOT
            / "reports"
            / "research"
            / "intraday_direction_thermometer"
            / "vwap_zscore_fade_backtest.json"
        ),
    )
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    end = args.end or conn.execute(
        "SELECT MAX(trade_date) FROM stock_kbar_5m"
    ).fetchone()[0]
    stocks = [s.strip() for s in args.stocks.split(",") if s.strip()]
    variants = VARIANTS

    print(f"universe={stocks}", flush=True)
    print(f"window={args.start}→{end} IS≤{args.is_end}", flush=True)
    print(f"grid size={len(variants)} variants", flush=True)

    results: list[dict[str, Any]] = []
    for sid in stocks:
        print(f"eval {sid} ...", flush=True)
        results.append(
            run_stock(
                conn,
                sid,
                start=args.start,
                end=end,
                is_end=args.is_end,
                variants=variants,
                horizon=args.horizon,
                flat_eps=args.flat_eps,
            )
        )

    pool_rows: list[dict[str, Any]] = []
    for v in variants:
        is_m = pool_from_stocks(results, v.name, "IS")
        oos_m = pool_from_stocks(results, v.name, "OOS")
        pool_rows.append(
            {
                "variant": v.name,
                "params": {
                    "z_entry": v.z_entry,
                    "sigma_mode": v.sigma_mode,
                    "session_guard": v.session_guard,
                    "use_confirm": v.use_confirm,
                    "use_thrust_block": v.use_thrust_block,
                },
                "IS": {k: is_m[k] for k in is_m if k != "per_name"},
                "OOS": {k: oos_m[k] for k in oos_m if k != "per_name"},
                "_is_full": is_m,
                "_oos_full": oos_m,
            }
        )

    # IS champion: max IS directed hit among variants with IS n >= 200.
    selectable = [
        r
        for r in pool_rows
        if (r["IS"].get("n_directed") or 0) >= 200
        and r["IS"].get("directed_hit_pct") is not None
    ]
    selectable.sort(
        key=lambda r: (
            -(r["IS"]["directed_hit_pct"] or 0),
            -(r["IS"]["n_directed"] or 0),
        )
    )
    if selectable:
        champ_row = selectable[0]
        champ_selectable = True
    else:
        champ_row = pool_rows[0]
        champ_selectable = False
    champ = {
        "variant": champ_row["variant"],
        "params": champ_row["params"],
        "IS": champ_row["_is_full"],
        "OOS": champ_row["_oos_full"],
    }
    gates = stability_gates(champ["OOS"])

    best_oos = max(
        pool_rows,
        key=lambda r: (
            r["OOS"].get("directed_hit_pct") or 0,
            r["OOS"].get("n_directed") or 0,
        ),
    )

    leaderboard = []
    for r in sorted(
        pool_rows,
        key=lambda x: (
            -(x["OOS"].get("directed_hit_pct") or 0),
            -(x["OOS"].get("n_directed") or 0),
        ),
    ):
        leaderboard.append(
            {
                "variant": r["variant"],
                "params": r["params"],
                "IS": r["IS"],
                "OOS": r["OOS"],
            }
        )

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "layer": "research",
        "status": "observe_only",
        "metric": {
            "name": "directed_hit_rate_vwap_zscore_fade",
            "horizon": args.horizon,
            "flat_eps": args.flat_eps,
            "is_end": args.is_end,
            "start": args.start,
            "end": end,
            "stocks": stocks,
            "grid_size": len(variants),
        },
        "champion_selectable": champ_selectable,
        "is_champion": {
            "variant": champ["variant"],
            "params": champ["params"],
            "IS": {k: champ["IS"][k] for k in champ["IS"] if k != "per_name"},
            "OOS": champ["OOS"],
        },
        "best_oos": {
            "variant": best_oos["variant"],
            "params": best_oos["params"],
            "OOS": best_oos["OOS"],
            "IS": best_oos["IS"],
        },
        "gates": gates,
        "pool_leaderboard": leaderboard,
        "per_stock": [
            {
                "stock_id": s["stock_id"],
                "n_days": s.get("n_days"),
                "date_min": s.get("date_min"),
                "date_max": s.get("date_max"),
                "error": s.get("error"),
                "champion": (s.get("variants") or {}).get(champ["variant"]),
            }
            for s in results
        ],
    }

    out_json = Path(args.out_json)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(
        f"VERDICT gates={gates['all_gates_pass']} "
        f"champ={champ['variant']} params={champ['params']} "
        f"IS={champ['IS'].get('directed_hit_pct')}% (n={champ['IS'].get('n_directed')}) "
        f"OOS={champ['OOS'].get('directed_hit_pct')}% (n={champ['OOS'].get('n_directed')}) "
        f"best_oos={best_oos['variant']}:{best_oos['OOS'].get('directed_hit_pct')}%"
        f"(n={best_oos['OOS'].get('n_directed')})",
        flush=True,
    )
    print(f"wrote {out_json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
