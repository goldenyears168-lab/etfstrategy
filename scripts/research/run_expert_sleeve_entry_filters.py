#!/usr/bin/env python3
"""8-sleeve opening / extension / gap / RRG filter study (research only).

Baseline = each sleeve's champion rule (lookback/same-day × floor), L1H7, 30bps,
r_adj = r_stock - 1.15 * r_IX0001.

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_sleeve_entry_filters.py
"""
from __future__ import annotations

import json
import sys
import types
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import sqlite3

START, END, OOS = "2024-07-01", "2026-07-17", "2026-01-01"
COST, HOLD, DEDUP, BETA = 0.003, 7, 5, 1.15
SOURCE = "finmind"
OUT = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "entry_filter_study_8sleeves.md"
)


def connect_ro():
    db = ROOT / "data" / "stocks.db"
    c = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=120)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=120000")
    return c


def load_pools():
    mod = types.ModuleType("w")
    mod.__file__ = str(ROOT / "scripts/research/run_expert_pool_watch.py")
    sys.modules["w"] = mod
    exec(compile(Path(mod.__file__).read_text(), mod.__file__, "exec"), mod.__dict__)
    sleeves = []
    for sid, p in sorted(mod.POOLS.items()):
        ws = ROOT / "reports/research/branch-footprint-screen/expert_pool" / sid / "watch_spec.json"
        mode, floor, core = "回看1日共識", p.floor_label, dict(p.core)
        if ws.exists():
            d = json.loads(ws.read_text(encoding="utf-8"))
            champ = d.get("champion") or {}
            mode = champ.get("mode") or mode
            floor = d.get("floor_label") or floor
            core = d.get("core") or core
        if sid == "2344":
            mode, floor = "同日共識", "≥1億"
        sleeves.append(
            {
                "sid": sid,
                "name": p.stock_name,
                "mode": mode,
                "floor": floor,
                "floor_amt": 5e7 if "0.5" in floor else 1e8,
                "core": core,
            }
        )
    return sleeves


def load_ix(con):
    pad_s = (datetime.strptime(START, "%Y-%m-%d") - timedelta(days=40)).strftime("%Y-%m-%d")
    pad_e = (datetime.strptime(END, "%Y-%m-%d") + timedelta(days=40)).strftime("%Y-%m-%d")
    rows = con.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code='IX0001' AND date BETWEEN ? AND ? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (pad_s, pad_e),
    ).fetchall()
    out: dict[str, tuple[float, float]] = {}
    for r in rows:
        out.setdefault(r["date"], (float(r["open"]), float(r["close"])))
    return [(d, o, c) for d, (o, c) in sorted(out.items())]


def load_stock_ohlc(con, sid: str):
    pad_s = (datetime.strptime(START, "%Y-%m-%d") - timedelta(days=40)).strftime("%Y-%m-%d")
    pad_e = (datetime.strptime(END, "%Y-%m-%d") + timedelta(days=40)).strftime("%Y-%m-%d")
    rows = con.execute(
        """
        SELECT trade_date, open, high, low, close FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (sid, SOURCE, pad_s, pad_e),
    ).fetchall()
    bars = []
    by = {}
    for r in rows:
        op = float(r["open"]) if r["open"] else float(r["close"] or 0)
        cl = float(r["close"] or 0)
        if cl <= 0:
            continue
        hi = float(r["high"]) if r["high"] else cl
        lo = float(r["low"]) if r["low"] else cl
        row = (r["trade_date"], op, hi, lo, cl)
        bars.append(row)
        by[r["trade_date"]] = row
    return bars, by


def load_rrg(con, sids: list[str]):
    """session_date -> stock_id -> (quadrant, rs_ratio, rs_momentum) for screen_kind=close."""
    ph = ",".join("?" * len(sids))
    rows = con.execute(
        f"""
        SELECT session_date, stock_id, quadrant, rs_ratio, rs_momentum
        FROM rrg_universe_scores
        WHERE screen_kind='close' AND stock_id IN ({ph})
          AND session_date BETWEEN ? AND ?
          AND rs_ratio IS NOT NULL
        ORDER BY session_date, stock_id
        """,
        (*sids, START, END),
    ).fetchall()
    out: dict[str, dict[str, tuple]] = defaultdict(dict)
    for r in rows:
        out[r["session_date"]][r["stock_id"]] = (
            r["quadrant"],
            float(r["rs_ratio"]),
            float(r["rs_momentum"]) if r["rs_momentum"] is not None else None,
        )
    # forward-fill per stock for PIT: use last available <= sig
    return out


def rrg_asof(rrg_by_day: dict, cal: list[str], sid: str, sig: str):
    """Last close-screen RRG on or before sig (PIT)."""
    # walk back on calendar
    for d in reversed([x for x in cal if x <= sig][-10:]):  # small window first
        if d in rrg_by_day and sid in rrg_by_day[d]:
            return rrg_by_day[d][sid]
    # broader
    for d in reversed([x for x in cal if x <= sig]):
        if d in rrg_by_day and sid in rrg_by_day[d]:
            return rrg_by_day[d][sid]
    return None


def next_open(bars, sig):
    for d, o, *_rest in bars:
        if d > sig and o > 0:
            return d, o
    return None, None


def exit_close(bars, entry, hold=HOLD):
    ordered = [x for x in bars if x[0] >= entry]
    if len(ordered) < hold:
        return None, None
    return ordered[hold - 1][0], ordered[hold - 1][4]


def prev_bar(by, date, bars):
    dates = [d for d, *_ in bars if d < date]
    if not dates:
        return None
    return by[dates[-1]]


def floor_lookback(by_date, cal, floor, amts):
    out = []
    idx = {d: i for i, d in enumerate(cal)}
    for d in cal:
        today = {t for t in by_date.get(d, set()) if amts.get((t, d), 0) >= floor}
        if not today:
            continue
        i = idx[d]
        yday = set()
        if i > 0:
            yd = cal[i - 1]
            yday = {t for t in by_date.get(yd, set()) if amts.get((t, yd), 0) >= floor}
        if len(today | yday) >= 2 and today:
            out.append(d)
    return out


def same_day(by_date, cal, floor, amts):
    return [
        d
        for d in cal
        if len({t for t in by_date.get(d, set()) if amts.get((t, d), 0) >= floor}) >= 2
    ]


def dedupe(sigs):
    out, last = [], None
    for d in sigs:
        if last is None or (
            datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")
        ).days > DEDUP:
            out.append(d)
            last = d
    return out


def stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "med": None, "mean": None, "win": None}
    return {
        "n": len(xs),
        "med": round(median(xs) * 100, 2),
        "mean": round(mean(xs) * 100, 2),
        "win": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1),
    }


def main() -> int:
    con = connect_ro()
    sleeves = load_pools()
    ix = load_ix(con)
    sids = [s["sid"] for s in sleeves]
    rrg_by_day = load_rrg(con, sids)
    print(f"IX points={len(ix)} RRG days={len(rrg_by_day)} sleeves={len(sleeves)}")

    # Build trades per sleeve
    all_trades = []  # dicts
    for sl in sleeves:
        sid = sl["sid"]
        bars, by = load_stock_ohlc(con, sid)
        if len(bars) < 40:
            print("skip thin bars", sid)
            continue
        cal = [d for d, *_ in bars if START <= d <= END]
        ph = ",".join("?" * len(sl["core"]))
        raw = con.execute(
            f"""
            SELECT b.securities_trader_id, b.trade_date, b.net, p.close
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p
              ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
            WHERE b.stock_id=? AND b.source=? AND b.trade_date BETWEEN ? AND ?
              AND b.securities_trader_id IN ({ph}) AND b.net>0 AND p.close>0
            """,
            (SOURCE, sid, SOURCE, START, END, *sl["core"].keys()),
        ).fetchall()
        amts = {}
        by_date: dict[str, set[str]] = defaultdict(set)
        for tid, d, net, close in raw:
            a = float(net) * float(close)
            amts[(tid, d)] = a
            if a >= 5e7:
                by_date[d].add(tid)
        floor = sl["floor_amt"]
        if "回看" in sl["mode"]:
            sigs = floor_lookback(by_date, cal, floor, amts)
        else:
            sigs = same_day(by_date, cal, floor, amts)
        sigs = dedupe(sigs)

        for sig in sigs:
            if sig not in by:
                continue
            sig_close = by[sig][4]
            prev = prev_bar(by, sig, bars)
            ed, eo = next_open(bars, sig)
            if not ed or not eo:
                continue
            xd, xc = exit_close(bars, ed)
            if not xc:
                continue
            ret = xc / eo - 1 - COST
            ix_bars = [(d, o, o, o, c) for d, o, c in ix]
            be, bo = next_open(ix_bars, sig)
            if not be or not bo:
                continue
            _, bc = exit_close(ix_bars, be)
            if not bc:
                continue
            bret = bc / bo - 1
            radj = ret - BETA * bret

            # filters inputs
            sig_day_ret = None
            if prev:
                sig_day_ret = sig_close / prev[4] - 1
            entry_prev = prev_bar(by, ed, bars)
            gap = (eo / entry_prev[4] - 1) if entry_prev else None
            chase = eo / sig_close - 1  # 訊號收 → 進場開

            rg = rrg_asof(rrg_by_day, cal, sid, sig)
            quad = rg[0] if rg else None
            rs = rg[1] if rg else None

            all_trades.append(
                {
                    "sid": sid,
                    "name": sl["name"],
                    "sig": sig,
                    "entry": ed,
                    "ret": ret,
                    "radj": radj,
                    "oos": sig >= OOS,
                    "sig_day_ret": sig_day_ret,
                    "gap": gap,
                    "chase": chase,
                    "quad": quad,
                    "rs": rs,
                }
            )

    print(f"total trades={len(all_trades)}")

    # Filter definitions (keep = pass filter)
    def F_none(t):
        return True

    def F_chase(max_pct):
        thr = max_pct / 100

        def f(t):
            return t["chase"] is not None and t["chase"] <= thr

        return f

    def F_gap(max_pct):
        thr = max_pct / 100

        def f(t):
            return t["gap"] is not None and t["gap"] <= thr

        return f

    def F_sigday(max_pct):
        thr = max_pct / 100

        def f(t):
            return t["sig_day_ret"] is not None and t["sig_day_ret"] <= thr

        return f

    def F_rrg_lead_imp(t):
        return t["quad"] in ("leading", "improving")

    def F_rrg_lead(t):
        return t["quad"] == "leading"

    def F_rs100(t):
        return t["rs"] is not None and t["rs"] >= 100

    def F_not_lag(t):
        return t["quad"] is not None and t["quad"] != "lagging"

    def AND(*fs):
        def f(t):
            return all(fn(t) for fn in fs)

        return f

    filters = [
        ("A 無濾網（基準）", F_none),
        ("B1 追價≤2%（訊號收→進場開）", F_chase(2)),
        ("B2 追價≤3%", F_chase(3)),
        ("B3 追價≤5%", F_chase(5)),
        ("C1 缺口≤2%（進場日開/昨收）", F_gap(2)),
        ("C2 缺口≤3%", F_gap(3)),
        ("C3 缺口≤5%", F_gap(5)),
        ("D1 訊號日漲≤3%", F_sigday(3)),
        ("D2 訊號日漲≤5%", F_sigday(5)),
        ("D3 訊號日漲≤8%", F_sigday(8)),
        ("E1 RRG leading|improving", F_rrg_lead_imp),
        ("E2 RRG leading only", F_rrg_lead),
        ("E3 RRG rs_ratio≥100", F_rs100),
        ("E4 RRG 非 lagging", F_not_lag),
        ("F1 追價≤3% ∩ 缺口≤3%", AND(F_chase(3), F_gap(3))),
        ("F2 追價≤3% ∩ rs≥100", AND(F_chase(3), F_rs100)),
        ("F3 追價≤3% ∩ lead|imp", AND(F_chase(3), F_rrg_lead_imp)),
        ("F4 訊號日≤5% ∩ 追價≤3%", AND(F_sigday(5), F_chase(3))),
    ]

    def eval_filter(name, pred, trades):
        kept = [t for t in trades if pred(t)]
        radj = [t["radj"] for t in kept]
        rets = [t["ret"] for t in kept]
        oos = [t["radj"] for t in kept if t["oos"]]
        base_n = len(trades)
        st = stats(radj)
        st_ret = stats(rets)
        st_oos = stats(oos)
        return {
            "filter": name,
            "n": st["n"],
            "keep_pct": round(100 * st["n"] / base_n, 1) if base_n else 0,
            "med_radj": st["med"],
            "mean_radj": st["mean"],
            "win_radj": st["win"],
            "med_ret": st_ret["med"],
            "oos_n": st_oos["n"],
            "oos_med_radj": st_oos["med"],
            "delta_med": None,  # fill vs baseline
        }

    # Pooled
    base = eval_filter("A 無濾網（基準）", F_none, all_trades)
    base_med = base["med_radj"] or 0

    lines = [
        "# 8 檔監測池 · 進場濾網／RRG 回測",
        "",
        f"> Research · `{START}`..`{END}` · OOS `{OOS}` · L1H7 · 30bps · "
        f"`r_adj = r − {BETA}×IX0001` · 冠軍規則（回看／同日×門檻）· 去重{DEDUP}日",
        "",
        "## 濾網定義（白話）",
        "",
        "| 代號 | 意義 | 怎麼篩 |",
        "|---|---|---|",
        "| **追價** | 訊號日收盤 → 次日開盤已漲多少 | 超過門檻 → **不跟** |",
        "| **缺口** | 進場日開盤相對前一日收盤 | 高開超過門檻 → **不跟** |",
        "| **訊號日漲** | 訊號當天本身漲幅 | 當天已瘋漲 → **不跟**（延伸） |",
        "| **RRG** | 訊號日（含以前最近）象限／RS | leading／improving／rs≥100 等 |",
        "",
        "> 註：本回測進場固定為**訊號次一交易日開盤**，此時「追價」= 開／訊號收，「缺口」= 開／昨收，"
        "而昨收＝訊號收 → **B 與 C 數列相同**。若改成延遲數日進場，兩者才會分開。",
        "",
        f"基準池合計訊號 **{base['n']}** 筆 · 超額中位（β調）**{base_med}%**",
        "",
        "## 合計（8 檔合併）",
        "",
        "| 濾網 | n | 保留% | 超額中位% | vs基準 | 超額勝% | 絕對中位% | OOS n/中位 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]

    pooled_rows = []
    for name, pred in filters:
        row = eval_filter(name, pred, all_trades)
        row["delta_med"] = (
            round(row["med_radj"] - base_med, 2)
            if row["med_radj"] is not None
            else None
        )
        pooled_rows.append(row)
        oos = (
            f"{row['oos_n']}/{row['oos_med_radj']}"
            if row["oos_med_radj"] is not None
            else f"{row['oos_n']}/—"
        )
        dlt = f"{row['delta_med']:+.2f}" if row["delta_med"] is not None else "—"
        lines.append(
            f"| {row['filter']} | {row['n']} | {row['keep_pct']} | {row['med_radj']} | "
            f"{dlt} | {row['win_radj']} | {row['med_ret']} | {oos} |"
        )

    # Per-sleeve baseline vs best practical filters
    lines += [
        "",
        "## 分檔：基準 vs 建議候選濾網",
        "",
        "比較：`無濾網` / `追價≤3%` / `追價≤3%∩缺口≤3%` / `RRG lead|imp` / `追價≤3%∩rs≥100`",
        "",
    ]
    key_filters = [
        ("無", F_none),
        ("追≤3%", F_chase(3)),
        ("追∩缺≤3%", AND(F_chase(3), F_gap(3))),
        ("RRG L|I", F_rrg_lead_imp),
        ("追∩rs100", AND(F_chase(3), F_rs100)),
    ]
    for sl in sleeves:
        sid = sl["sid"]
        sub = [t for t in all_trades if t["sid"] == sid]
        if not sub:
            continue
        lines.append(f"### {sid} {sl['name']} · {sl['mode']} · {sl['floor']} · n₀={len(sub)}")
        lines.append("")
        lines.append("| 濾網 | n | 超額中位 | 勝% | OOS中位 |")
        lines.append("|---|---:|---:|---:|---:|")
        for lab, pred in key_filters:
            row = eval_filter(lab, pred, sub)
            lines.append(
                f"| {lab} | {row['n']} | {row['med_radj']} | {row['win_radj']} | {row['oos_med_radj']} |"
            )
        lines.append("")

    # Verdict
    # pick best by med_radj among keep_pct >= 40% and n>=30
    cands = [
        r
        for r in pooled_rows
        if r["n"] >= 30 and r["keep_pct"] >= 35 and r["med_radj"] is not None
    ]
    cands.sort(key=lambda r: (-(r["med_radj"] or -999), -r["n"]))
    lines += [
        "## 結論（合計）",
        "",
    ]
    if cands:
        best = cands[0]
        lines.append(
            f"- 在「保留≥35% 且 n≥30」約束下，合計超額中位最高："
            f"**{best['filter']}** → n={best['n']} 中位 {best['med_radj']}% "
            f"（vs 基準 {base_med}% · Δ {best['delta_med']:+.2f}pp）"
        )
    # RRG alone
    rrg_row = next(r for r in pooled_rows if r["filter"].startswith("E1"))
    chase_row = next(r for r in pooled_rows if r["filter"].startswith("B2"))
    lines += [
        f"- **RRG leading|improving**：中位 {rrg_row['med_radj']}% "
        f"（Δ {rrg_row['delta_med']:+.2f}pp）· 保留 {rrg_row['keep_pct']}% —— "
        + (
            "有提升但幅度有限"
            if (rrg_row["delta_med"] or 0) < 2
            else "提升較明顯"
        )
        + "。",
        f"- **追價≤3%**：中位 {chase_row['med_radj']}% "
        f"（Δ {chase_row['delta_med']:+.2f}pp）· 保留 {chase_row['keep_pct']}%。",
        "",
        "### 建議（通知／人工判斷）",
        "",
        "1. **郵件可先標註**（不自動砍信）：追價%、缺口%、訊號日漲%、RRG 象限。",
        "2. **人工優先跟**：追價≤3% 且缺口≤3%（或再加 rs≥100）。",
        "3. **RRG 單獨當硬濾網**：目前合計提升有限，適合當加分項而非唯一門檻。",
        "",
    ]

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote", OUT)
    # also print pooled table to stdout for chat
    print("\n".join(lines[lines.index("## 合計（8 檔合併）") : lines.index("## 分檔：基準 vs 建議候選濾網")]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
