#!/usr/bin/env python3
"""Clean v3: fair core-universe soft vs expert + correct chip overlays.

Fixes vs prior bakeoff:
- Soft = lower floor / top10 consensus on SAME seat universe (core or DOM_A)
- No IS-L1H7 seat picking; no antidt look-ahead
- Baseline = any core ≥0.5億 (not every top10 day)
- Chip via build_relative_chip_panel: ¬rel_dump veto, scoop_resid OR
- Dedup 5d all arms; net×close; OOS cut 2026-01-01
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "src"))
from research.chip_relative.panel import build_relative_chip_panel  # noqa: E402

OUT = ROOT / "reports/research/chip-overlays/method_vs_expert"
OUT.mkdir(parents=True, exist_ok=True)
START, END, OOS = "2024-07-01", "2026-07-22", "2026-01-01"
WARMUP = "2024-01-01"
SOURCE = "finmind"
BETA, COST, HARD, SOFT_FL = 1.15, 0.003, 5e7, 3e6
DEDUPE = 5
FOREIGN_KW = (
    "美商",
    "摩根",
    "瑞銀",
    "美林",
    "港商",
    "野村",
    "瑞士",
    "花旗",
    "渣打",
    "香港",
    "新加坡",
    "大和",
    "法銀",
    "德意志",
    "巴克萊",
    "高盛",
    "美銀",
)

STOCKS = {
    "2327": {
        "name": "國巨",
        "core": ["981j", "779Z", "8888", "8840", "918e"],
        "status": "adopted",
        "holds": [7, 10],
    },
    "8046": {
        "name": "南電",
        "core": ["9216", "9227", "5850"],
        "status": "adopted",
        "holds": [7],
    },
    "2492": {
        "name": "華新科",
        "core": ["9239", "983Z", "5850", "9654", "9666"],  # frozen DOM_A
        "status": "skipped_hard0",
        "holds": [7],
        "note": "DOM_A from domestic_watchlist.json — not IS L1H7 soft seed",
    },
}


def is_foreign(n: str) -> bool:
    return any(k in (n or "") for k in FOREIGN_KW)


def build_fwd(sbars: list, ixbars: list, hold: int) -> dict[str, tuple[float, float, float]]:
    ix_idx = {d: i for i, (d, _, _) in enumerate(ixbars)}
    out: dict[str, tuple[float, float, float]] = {}
    for i in range(len(sbars) - 1):
        sig = sbars[i][0]
        ed, eo = sbars[i + 1][0], sbars[i + 1][1]
        j = i + 1 + hold - 1
        if j >= len(sbars) or eo <= 0:
            continue
        xc = sbars[j][2]
        if xc <= 0 or ed not in ix_idx:
            continue
        ii = ix_idx[ed]
        jj = ii + hold - 1
        if jj >= len(ixbars) or ixbars[ii][1] <= 0:
            continue
        r = xc / eo - 1 - COST
        rix = ixbars[jj][2] / ixbars[ii][1] - 1.0
        out[sig] = (r, rix, r - BETA * rix)
    return out


def build_fwd_t2(sbars: list, ixbars: list, hold: int) -> dict[str, tuple[float, float, float]]:
    """Signal T → entry T+2 open (latency-robust)."""
    ix_idx = {d: i for i, (d, _, _) in enumerate(ixbars)}
    out: dict[str, tuple[float, float, float]] = {}
    for i in range(len(sbars) - 2):
        sig = sbars[i][0]
        ed, eo = sbars[i + 2][0], sbars[i + 2][1]
        j = i + 2 + hold - 1
        if j >= len(sbars) or eo <= 0:
            continue
        xc = sbars[j][2]
        if xc <= 0 or ed not in ix_idx:
            continue
        ii = ix_idx[ed]
        jj = ii + hold - 1
        if jj >= len(ixbars) or ixbars[ii][1] <= 0:
            continue
        r = xc / eo - 1 - COST
        rix = ixbars[jj][2] / ixbars[ii][1] - 1.0
        out[sig] = (r, rix, r - BETA * rix)
    return out


def dedupe_dates(dates: list[pd.Timestamp], gap: int = DEDUPE) -> list[pd.Timestamp]:
    dates = sorted(set(dates))
    kept: list[pd.Timestamp] = []
    last = None
    for d in dates:
        if last is None or (d - last).days >= gap:
            kept.append(d)
            last = d
    return kept


def lookback1(cal, core, amt, floor: float) -> list[pd.Timestamp]:
    out = []
    for i, d in enumerate(cal):
        today = {t for t in core if amt.get((t, d), 0) >= floor}
        if not today:
            continue
        yday = set()
        if i > 0:
            yd = cal[i - 1]
            yday = {t for t in core if amt.get((t, yd), 0) >= floor}
        if len(today | yday) >= 2:
            out.append(d)
    return out


def same_day_k(cal, core, amt, floor: float, k: int = 2) -> list[pd.Timestamp]:
    return [d for d in cal if sum(1 for t in core if amt.get((t, d), 0) >= floor) >= k]


def or_hard(cal, core, amt, floor: float) -> list[pd.Timestamp]:
    return [d for d in cal if any(amt.get((t, d), 0) >= floor for t in core)]


def top10_core_k(T10: dict, core: set, k: int = 2) -> list[pd.Timestamp]:
    return [d for d, s in T10.items() if len(s & core) >= k]


def summarize(days, fwd) -> dict | None:
    rows = []
    for d in days:
        ds = d.strftime("%Y-%m-%d")
        hit = fwd.get(ds)
        if hit is None:
            continue
        rows.append((ds, "OOS" if ds >= OOS else "IS", hit[2]))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["date", "split", "r_adj"])
    out = {}
    for sp in ("IS", "OOS", "ALL"):
        sub = df if sp == "ALL" else df[df.split == sp]
        if len(sub) == 0:
            continue
        out[sp] = {
            "n": int(len(sub)),
            "med": float(sub.r_adj.median()),
            "win": float((sub.r_adj > 0).mean()),
        }
    return out


def main() -> None:
    db = str((ROOT / "data/stocks.db").resolve())
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    mkt = pd.read_csv(
        ROOT / "reports/research/chip-overlays/2492_knowledge/cache_market_ft_totals.csv"
    )
    mkt["date"] = pd.to_datetime(mkt["date"]).dt.strftime("%Y-%m-%d")

    pad_s = (datetime.strptime(START, "%Y-%m-%d") - timedelta(days=40)).strftime("%Y-%m-%d")
    pad_e = (datetime.strptime(END, "%Y-%m-%d") + timedelta(days=40)).strftime("%Y-%m-%d")
    ix: dict[str, tuple[float, float]] = {}
    for d, o, c in conn.execute(
        """SELECT date,open,close FROM daily_bars WHERE code='IX0001' AND date BETWEEN ? AND ?
           AND open>0 AND close>0 ORDER BY date,
           CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END""",
        (pad_s, pad_e),
    ):
        ix.setdefault(str(d), (float(o), float(c)))
    ixbars = [(d, o, c) for d, (o, c) in sorted(ix.items())]

    all_scores: list[dict] = []
    meta_out: dict = {"protocol": "v3_clean", "stocks": {}}

    for sid, meta in STOCKS.items():
        print("===", sid, meta["name"], flush=True)
        core = list(meta["core"])
        core_set = set(core)
        sbars = []
        for d, o, c in conn.execute(
            "SELECT trade_date,open,close FROM stock_daily_bars WHERE source=? AND stock_id=? AND trade_date BETWEEN ? AND ? ORDER BY 1",
            (SOURCE, sid, pad_s, pad_e),
        ):
            op = float(o) if o else float(c or 0)
            cl = float(c or 0)
            if cl > 0:
                sbars.append((str(d), op, cl))
        bars = pd.read_sql_query(
            "SELECT trade_date AS date, open, close FROM stock_daily_bars WHERE source=? AND stock_id=? AND trade_date BETWEEN ? AND ? ORDER BY 1",
            conn,
            params=[SOURCE, sid, START, END],
        )
        br = pd.read_sql_query(
            "SELECT trade_date AS date, securities_trader_id AS tid, securities_trader AS tname, net FROM stock_broker_branch_daily WHERE source=? AND stock_id=? AND trade_date BETWEEN ? AND ?",
            conn,
            params=[SOURCE, sid, START, END],
        )
        bars["date"] = pd.to_datetime(bars["date"])
        br["date"] = pd.to_datetime(br["date"])
        br["tid"] = br["tid"].astype(str)
        br = br.merge(bars[["date", "close", "open"]], on="date", how="left")
        br["buy_amt"] = br["net"] * br["close"]
        br["foreign"] = br["tname"].fillna("").map(is_foreign)
        dom = br[(~br.foreign) & (br.net > 0)].copy()
        dom["rank"] = dom.groupby("date")["buy_amt"].rank(ascending=False, method="first")
        top10 = dom[dom["rank"] <= 10]
        T10 = {d: set(g.tid.tolist()) for d, g in top10.groupby("date")}
        cal = sorted(bars["date"].tolist())
        px = bars.set_index("date").copy()
        px["gap"] = px["open"] / px["close"].shift(1) - 1
        amt = {
            (r.tid, r.date): float(r.buy_amt)
            for r in br.itertuples()
            if r.buy_amt and r.buy_amt > 0
        }

        # chip panel
        chip = build_relative_chip_panel(
            conn,
            sid=sid,
            start=START,
            end=END,
            warmup=WARMUP,
            market_totals=mkt,
            rs_anchor=START,
        )
        chip = chip.set_index(pd.to_datetime(chip["date"]))
        scoop = (chip["rel_scoop"].fillna(False)) & (chip["FT_resid_z"].fillna(-9) >= 0.5)
        dump = chip["rel_dump"].fillna(False)
        scoop_days = set(scoop[scoop].index)
        dump_days = set(dump[dump].index)

        fwds = {h: build_fwd(sbars, ixbars, h) for h in meta["holds"]}
        fwd7_t2 = build_fwd_t2(sbars, ixbars, 7)

        arms: dict[str, list[pd.Timestamp]] = {}
        # baselines / expert
        arms["B_or_hard"] = or_hard(cal, core_set, amt, HARD)
        arms["E_lb1_hard"] = lookback1(cal, core_set, amt, HARD)
        arms["E_same_hard"] = same_day_k(cal, core_set, amt, HARD, 2)
        # fair soft on SAME core
        arms["S_lb1_soft30m"] = lookback1(cal, core_set, amt, SOFT_FL)
        arms["S_same_soft30m"] = same_day_k(cal, core_set, amt, SOFT_FL, 2)
        arms["S_top10_k2"] = top10_core_k(T10, core_set, 2)
        arms["S_top10_k2_nogap"] = [
            d
            for d in arms["S_top10_k2"]
            if d in px.index and (pd.isna(px.at[d, "gap"]) or px.at[d, "gap"] <= 0.03)
        ]
        # chip overlays on expert
        arms["E_lb1_not_dump"] = [d for d in arms["E_lb1_hard"] if d not in dump_days]
        arms["E_lb1_or_scoop"] = sorted(set(arms["E_lb1_hard"]) | scoop_days)
        arms["scoop_only"] = sorted(scoop_days & set(cal))
        # soft + chip veto
        arms["S_lb1_soft30m_not_dump"] = [
            d for d in arms["S_lb1_soft30m"] if d not in dump_days
        ]
        arms["S_top10_k2_not_dump"] = [d for d in arms["S_top10_k2"] if d not in dump_days]

        # dedupe all
        arms = {k: dedupe_dates(v) for k, v in arms.items()}

        meta_out["stocks"][sid] = {
            "name": meta["name"],
            "status": meta["status"],
            "core": core,
            "n_chip_scoop": int(scoop.sum()),
            "n_chip_dump": int(dump.sum()),
            "arm_n_days": {k: len(v) for k, v in arms.items()},
        }

        for name, days in arms.items():
            for h in meta["holds"]:
                sm = summarize(days, fwds[h])
                if not sm:
                    continue
                for sp, st in sm.items():
                    all_scores.append(
                        dict(sid=sid, arm=name, hold=h, entry="T1", split=sp, **st)
                    )
            # T+2 latency check on key arms H7
            if name in ("E_lb1_hard", "S_lb1_soft30m", "E_lb1_not_dump", "S_top10_k2"):
                sm2 = summarize(days, fwd7_t2)
                if sm2:
                    for sp, st in sm2.items():
                        all_scores.append(
                            dict(sid=sid, arm=name, hold=7, entry="T2", split=sp, **st)
                        )
            print(f"  {name}: {len(days)}", flush=True)

    conn.close()
    sc = pd.DataFrame(all_scores)
    sc.to_csv(OUT / "v3_clean_scores.csv", index=False)
    (OUT / "v3_clean_meta.json").write_text(
        json.dumps(meta_out, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    def get(sid, arm, split, hold=7, entry="T1"):
        r = sc[
            (sc.sid == sid)
            & (sc.arm == arm)
            & (sc.split == split)
            & (sc.hold == hold)
            & (sc.entry == entry)
        ]
        return r.iloc[0] if len(r) else None

    lines = [
        "# METHOD v3 乾淨重跑 · 正確籌碼 + 公平 soft",
        "",
        "> 對齊 [`CHIP_CORRECT_USAGE.md`](CHIP_CORRECT_USAGE.md)",
        "> Soft = **同 core／DOM_A 降門檻** · 禁 IS 選席 · 禁 antidt · 基線 `B_or_hard` · 去重 5d · `net×close`",
        "",
        "## Soft／Core 宇宙",
        "",
    ]
    for sid, m in meta_out["stocks"].items():
        lines.append(
            f"- **{sid} {m['name']}**（{m['status']}）core=`{','.join(m['core'])}` · scoop日={m['n_chip_scoop']} · dump日={m['n_chip_dump']}"
        )

    lines += [
        "",
        "## OOS 主表（T+1 進場）",
        "",
        "| sid | arm | H | n | med | win | vs B | thin |",
        "|-----|-----|--:|--:|----:|----:|-----:|------|",
    ]
    for sid in STOCKS:
        b = get(sid, "B_or_hard", "OOS", hold=7)
        bm = b.med if b is not None else np.nan
        for arm in [
            "B_or_hard",
            "E_lb1_hard",
            "E_same_hard",
            "S_lb1_soft30m",
            "S_same_soft30m",
            "S_top10_k2",
            "S_top10_k2_nogap",
            "E_lb1_not_dump",
            "E_lb1_or_scoop",
            "scoop_only",
            "S_lb1_soft30m_not_dump",
            "S_top10_k2_not_dump",
        ]:
            for h in STOCKS[sid]["holds"]:
                o = get(sid, arm, "OOS", hold=h)
                if o is None:
                    continue
                thin = "Y" if int(o.n) < 8 else ""
                vs = "" if b is None else f"{(o.med - bm) * 100:+.2f}pp"
                lines.append(
                    f"| {sid} | `{arm}` | {h} | {int(o.n)} | **{o.med * 100:+.2f}%** | {o.win:.0%} | {vs} | {thin} |"
                )

    lines += ["", "## T+2 進場敏感度（H7 · 公告延遲）", "", "| sid | arm | n | med | vs T1 |", "|-----|-----|--:|----:|------:|"]
    for sid in STOCKS:
        for arm in ("E_lb1_hard", "S_lb1_soft30m", "E_lb1_not_dump", "S_top10_k2"):
            t1 = get(sid, arm, "OOS", hold=7, entry="T1")
            t2 = get(sid, arm, "OOS", hold=7, entry="T2")
            if t2 is None:
                continue
            dlt = "" if t1 is None else f"{(t2.med - t1.med) * 100:+.2f}pp"
            lines.append(f"| {sid} | `{arm}` | {int(t2.n)} | **{t2.med * 100:+.2f}%** | {dlt} |")

    lines += ["", "## 每股判決", ""]
    for sid, meta in STOCKS.items():
        lines.append(f"### {sid} {meta['name']}（{meta['status']}）")

        def fmt(arm, h=7, entry="T1"):
            o = get(sid, arm, "OOS", h, entry)
            i = get(sid, arm, "IS", h, entry)
            if o is None:
                return "—"
            ispart = f" · IS {i.med * 100:+.2f}% n={int(i.n)}" if i is not None else ""
            thin = " 〔thin〕" if int(o.n) < 8 else ""
            return f"OOS {o.med * 100:+.2f}% n={int(o.n)}{thin}{ispart}"

        b = get(sid, "B_or_hard", "OOS")
        e = get(sid, "E_lb1_hard", "OOS")
        s = get(sid, "S_lb1_soft30m", "OOS")
        ev = get(sid, "E_lb1_not_dump", "OOS")
        lines.append(f"- B_or_hard: {fmt('B_or_hard')}")
        lines.append(f"- E_lb1_hard: {fmt('E_lb1_hard')}")
        if sid == "2327":
            lines.append(f"- E_lb1_hard H10: {fmt('E_lb1_hard', 10)}")
        lines.append(f"- S_lb1_soft30m（公平 soft）: {fmt('S_lb1_soft30m')}")
        lines.append(f"- S_top10_k2: {fmt('S_top10_k2')}")
        lines.append(f"- E ¬dump: {fmt('E_lb1_not_dump')}")
        lines.append(f"- E ∨ scoop: {fmt('E_lb1_or_scoop')}")
        lines.append(f"- scoop_only: {fmt('scoop_only')}")

        # verdict logic
        if e is not None and int(e.n) >= 8 and s is not None:
            if s.med + 0.01 < e.med and int(s.n) >= 8:
                lines.append("- **判決**：同宇宙降門檻 soft **打不贏** 專家 lb1 → soft 非替代。")
            elif int(s.n) < 8:
                lines.append("- **判決**：公平 soft OOS 過薄 → 不能宣稱優於專家。")
            else:
                lines.append("- **判決**：soft 接近／略優需看 thin 與 IS；主線仍看專家閘。")
        elif meta["status"] == "skipped_hard0":
            # 2492: hard events may be rare
            if e is None or int(e.n) < 8:
                st = get(sid, "S_top10_k2", "OOS")
                sv = get(sid, "S_top10_k2_not_dump", "OOS")
                if st is not None and int(st.n) >= 8:
                    lines.append(
                        "- **判決**：hard 共識過稀；**DOM_A top10 k≥2** 可作 hard0 備援（非採納專家）。"
                    )
                    if sv is not None and int(sv.n) >= 8 and sv.med >= (st.med - 0.005):
                        lines.append("- 籌碼 ¬dump 疊在 DOM_A 上：**可接受的正確用法**（否決濾網）。")
                else:
                    lines.append("- **判決**：DOM_A 備援仍不足；勿吹捧 soft METHOD。")
            else:
                lines.append("- **判決**：DOM_A 上意外出現 hard 級訊號 — 另查金額分佈。")
        else:
            lines.append("- **判決**：樣本不足，不下強結論。")

        if ev is not None and e is not None and int(ev.n) >= 8 and int(e.n) >= 8:
            if ev.med >= e.med + 0.005:
                lines.append("- **籌碼加值**：¬rel_dump 否決抬升 Expert med → **正確互補**。")
            elif ev.med + 0.01 < e.med:
                lines.append("- **籌碼加值**：否決傷害中位（過度過濾或期間不匹配）。")
            else:
                lines.append("- **籌碼加值**：¬dump 中性（減筆數、med 接近）。")
        lines.append("")

    lines += [
        "## 總答",
        "",
        "1. **正確籌碼用法**＝相對 FT 毛額特徵當 **否決／OR**，不是另找一批席跟單。",
        "2. **公平 soft**＝專家同一批席降門檻；若仍打不贏 lb1 hard，則 2492 式「另池共識」不是通用升級。",
        "3. hard0 股用 **凍結 DOM_A + ¬dump** 當備援；已採納股以 Expert lb1（±籌碼否決）為主。",
        "",
        "CSV: `v3_clean_scores.csv` · meta: `v3_clean_meta.json`",
        "",
    ]
    (OUT / "METHOD_V3_CLEAN.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print("wrote", OUT / "METHOD_V3_CLEAN.md")


if __name__ == "__main__":
    main()
