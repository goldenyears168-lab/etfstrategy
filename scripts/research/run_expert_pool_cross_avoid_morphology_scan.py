#!/usr/bin/env python3
"""Cross expert-pool avoid-morphology factor scan (research only).

  PYTHONPATH=src .venv/bin/python scripts/research/run_expert_pool_cross_avoid_morphology_scan.py

Reads: reports/.../expert_pool/knowledge/trades/*.json
Writes: reports/.../hypotheses/avoid_morphology/H_CROSS_AVOID_SCAN.{json,md}
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402

TRADES_DIR = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "knowledge"
    / "trades"
)
OUT_DIR = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "hypotheses"
    / "avoid_morphology"
)
SOURCE = "finmind"
PERM_N = 10_000
PERM_SEED = 42


def connect_ro(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=120)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=120000")
    return c


def load_all_trades() -> list[dict]:
    rows: list[dict] = []
    for p in sorted(TRADES_DIR.glob("*.json")):
        data = json.loads(p.read_text())
        sid = str(data["stock_id"])
        tier = data.get("tier")
        for t in data.get("trades") or []:
            rows.append(
                {
                    "stock_id": sid,
                    "stock_name": data.get("stock_name") or "",
                    "tier": tier,
                    "signal_date": t["signal_date"],
                    "entry_date": t["entry_date"],
                    "entry_open": float(t["entry_open"]),
                    "r_adj_pct": float(t["r_adj_pct"]),
                }
            )
    return rows


def fetch_daily_bars(
    conn: sqlite3.Connection, pairs: set[tuple[str, str]]
) -> dict[tuple[str, str], dict]:
    if not pairs:
        return {}
    out: dict[tuple[str, str], dict] = {}
    by_sid: dict[str, set[str]] = defaultdict(set)
    for sid, d in pairs:
        by_sid[sid].add(d)
    for sid, dates in by_sid.items():
        ph = ",".join("?" * len(dates))
        q = f"""
        SELECT trade_date, open, high, low, close
        FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date IN ({ph})
          AND open>0 AND close>0 AND high>=low
        """
        for r in conn.execute(q, (sid, SOURCE, *sorted(dates))):
            out[(sid, r["trade_date"])] = {
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
            }
    return out


def fetch_calendars(
    conn: sqlite3.Connection, sids: set[str]
) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sid in sids:
        rows = conn.execute(
            """
            SELECT trade_date FROM stock_daily_bars
            WHERE stock_id=? AND source=? AND close>0
            ORDER BY trade_date
            """,
            (sid, SOURCE),
        ).fetchall()
        out[sid] = [r[0] for r in rows]
    return out


def fetch_1m_morning(
    conn: sqlite3.Connection, pairs: set[tuple[str, str]]
) -> dict[tuple[str, str], list[dict]]:
    if not pairs:
        return {}
    out: dict[tuple[str, str], list[dict]] = {}
    by_sid: dict[str, set[str]] = defaultdict(set)
    for sid, d in pairs:
        by_sid[sid].add(d)
    for sid, dates in by_sid.items():
        ph = ",".join("?" * len(dates))
        q = f"""
        SELECT trade_date, minute, open, high, low, close, source
        FROM stock_kbar_1m
        WHERE stock_id=? AND trade_date IN ({ph}) AND minute <= '09:30:00'
        ORDER BY trade_date,
                 CASE source WHEN 'finmind' THEN 0 WHEN 'yahoo' THEN 1 ELSE 2 END,
                 minute
        """
        raw = conn.execute(q, (sid, *sorted(dates))).fetchall()
        by_day: dict[str, list] = defaultdict(list)
        for r in raw:
            by_day[r["trade_date"]].append(r)
        for day, rs in by_day.items():
            seen: set[str] = set()
            bars = []
            for r in rs:
                m = str(r["minute"])
                if m in seen:
                    continue
                seen.add(m)
                bars.append(
                    {
                        "minute": m,
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                    }
                )
            if bars:
                out[(sid, day)] = bars
    return out


def idx_on(cal: list[str], day: str) -> int | None:
    try:
        return cal.index(day)
    except ValueError:
        return None


def close_pos(o: float, h: float, l: float, c: float) -> float:
    rng = h - l
    if rng <= 1e-9:
        return 0.5
    return (c - l) / rng


def intraday_feats(
    bars: list[dict] | None, entry_open: float, t_close: float
) -> dict:
    na = {
        "has_1m": False,
        "gap_chase": None,
        "open5_weak": None,
        "no_recovery30": None,
        "fail_break30": None,
        "weak_vs_t": None,
        "gap_open5": None,
    }
    if not bars or entry_open <= 0 or t_close <= 0:
        return na
    o = entry_open
    hi30 = max(b["high"] for b in bars)
    lo30 = min(b["low"] for b in bars)
    c30 = bars[-1]["close"]
    ret30 = c30 / o - 1.0
    bars5 = [b for b in bars if b["minute"] <= "09:05:00"]
    if not bars5:
        return na
    lo5 = min(b["low"] for b in bars5)
    c5 = bars5[-1]["close"]
    ret5 = c5 / o - 1.0
    lo5_ret = lo5 / o - 1.0
    gap = o / t_close - 1.0
    out = {
        "has_1m": True,
        "gap_chase": gap >= 0.03,
        "open5_weak": ret5 < -0.01 and lo5_ret < -0.02,
        "no_recovery30": ret30 < 0,
        "fail_break30": hi30 > o * 1.01 and c30 < o,
        "weak_vs_t": c30 < t_close and ret30 < 0,
        "gap_open5": None,
    }
    out["gap_open5"] = bool(out["gap_chase"] and out["open5_weak"])
    return out


def enrich_trades(conn: sqlite3.Connection, trades: list[dict]) -> list[dict]:
    sids = {t["stock_id"] for t in trades}
    cals = fetch_calendars(conn, sids)

    daily_pairs: set[tuple[str, str]] = set()
    entry_pairs: set[tuple[str, str]] = set()
    for t in trades:
        sid = t["stock_id"]
        T = t["signal_date"]
        daily_pairs.add((sid, T))
        entry_pairs.add((sid, t["entry_date"]))
        cal = cals.get(sid) or []
        i = idx_on(cal, T)
        if i is not None:
            for j in range(max(0, i - 25), i):
                daily_pairs.add((sid, cal[j]))

    daily = fetch_daily_bars(conn, daily_pairs)
    kb1m = fetch_1m_morning(conn, entry_pairs)

    hi20_cache: dict[tuple[str, str], float | None] = {}

    def high20(sid: str, T: str) -> float | None:
        key = (sid, T)
        if key in hi20_cache:
            return hi20_cache[key]
        cal = cals.get(sid) or []
        i = idx_on(cal, T)
        if i is None or i < 19:
            hi20_cache[key] = None
            return None
        hs = []
        for d in cal[i - 19 : i + 1]:
            bar = daily.get((sid, d))
            if bar:
                hs.append(bar["high"])
        hi20_cache[key] = max(hs) if len(hs) >= 20 else None
        return hi20_cache[key]

    out = []
    for t in trades:
        sid = t["stock_id"]
        T = t["signal_date"]
        cal = cals.get(sid) or []
        i = idx_on(cal, T)
        bar_t = daily.get((sid, T))
        rec = dict(t)
        rec["features"] = {}
        rec["intraday_ok"] = False
        if not bar_t or i is None:
            rec["features"]["daily_ok"] = False
            out.append(rec)
            continue
        rec["features"]["daily_ok"] = True
        o, h, l, c = bar_t["open"], bar_t["high"], bar_t["low"], bar_t["close"]
        hot5 = False
        if i >= 5:
            b5 = daily.get((sid, cal[i - 5]))
            b1 = daily.get((sid, cal[i - 1]))
            if b5 and b1 and b5["close"] > 0:
                hot5 = b1["close"] / b5["close"] - 1.0 >= 0.20
        h20 = high20(sid, T)
        at_high = bool(h20 and c >= h20 * 0.99)
        t_black = c < o
        t_weak_pos = close_pos(o, h, l, c) < 0.33
        t_body_reject = c / o - 1.0 < -0.03
        rec["features"].update(
            {
                "HOT5": hot5,
                "AT_HIGH": at_high,
                "T_BLACK": t_black,
                "T_WEAK_POS": t_weak_pos,
                "T_BODY_REJECT": t_body_reject,
                "T_REJECT_COMBO": t_black and t_weak_pos,
                "EXT_WEAK": hot5 and t_black,
            }
        )
        bars = kb1m.get((sid, t["entry_date"]))
        intra = intraday_feats(bars, t["entry_open"], c)
        rec["intraday_ok"] = intra["has_1m"]
        if intra["has_1m"]:
            rec["features"]["GAP_CHASE"] = intra["gap_chase"]
            rec["features"]["OPEN5_WEAK"] = intra["open5_weak"]
            rec["features"]["NO_RECOVERY30"] = intra["no_recovery30"]
            rec["features"]["FAIL_BREAK30"] = intra["fail_break30"]
            rec["features"]["WEAK_VS_T"] = intra["weak_vs_t"]
            rec["features"]["GAP_OPEN5"] = intra["gap_open5"]
            rec["features"]["HOT5_GAP"] = hot5 and intra["gap_chase"]
        out.append(rec)
    return out


FACTORS = [
    ("HOT5", "daily", "T-5→T-1 漲幅≥+20%"),
    ("AT_HIGH", "daily", "T close 距 20 日高≥−1%"),
    ("T_BLACK", "daily", "T 收黑"),
    ("T_WEAK_POS", "daily", "T close_pos<0.33"),
    ("T_BODY_REJECT", "daily", "T body=(c/o−1)<−3%"),
    ("T_REJECT_COMBO", "daily", "T_BLACK∧T_WEAK_POS"),
    ("EXT_WEAK", "daily", "HOT5∧T_BLACK（聯電型）"),
    ("GAP_CHASE", "intraday", "entry open vs T close≥+3%"),
    ("OPEN5_WEAK", "intraday", "09:05 ret<−1% 且 low<−2%"),
    ("NO_RECOVERY30", "intraday", "09:30 ret<0"),
    ("FAIL_BREAK30", "intraday", "30m high>open×1.01 且 30m close<open"),
    ("WEAK_VS_T", "intraday", "09:30 close<T close 且 09:30 ret<0"),
    ("GAP_OPEN5", "intraday", "GAP_CHASE∧OPEN5_WEAK"),
    ("HOT5_GAP", "intraday", "HOT5∧GAP_CHASE"),
]


def summarize(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0, "med_r_adj": None, "win_rate": None}
    return {
        "n": len(xs),
        "med_r_adj": round(median(xs), 2),
        "win_rate": round(sum(1 for x in xs if x > 0) / len(xs) * 100, 1),
    }


def permutation_p_avoid(hit: list[float], miss: list[float], *, n_perm: int = PERM_N) -> float | None:
    """One-sided: H1 hit median < miss median (avoid factor)."""
    if len(hit) < 2 or len(miss) < 2:
        return None
    pooled = np.asarray(hit + miss, dtype=float)
    n_hit = len(hit)
    obs = float(np.median(hit) - np.median(miss))
    rng = np.random.default_rng(PERM_SEED)
    cnt = 0
    for _ in range(n_perm):
        perm = rng.permutation(pooled)
        d = float(np.median(perm[:n_hit]) - np.median(perm[n_hit:]))
        if d <= obs:
            cnt += 1
    return round(cnt / n_perm, 4)


def analyze_factor(trades: list[dict], key: str, scope: str) -> dict:
    hit, miss = [], []
    by_sid: dict[str, dict[str, list[float]]] = defaultdict(lambda: {"hit": [], "miss": []})
    na = 0
    for t in trades:
        feats = t.get("features") or {}
        if scope == "daily" and not feats.get("daily_ok"):
            na += 1
            continue
        if scope == "intraday":
            if not t.get("intraday_ok"):
                na += 1
                continue
        if key not in feats:
            na += 1
            continue
        v = feats[key]
        r = t["r_adj_pct"]
        sid = t["stock_id"]
        if v:
            hit.append(r)
            by_sid[sid]["hit"].append(r)
        else:
            miss.append(r)
            by_sid[sid]["miss"].append(r)
    sh = summarize(hit)
    sm = summarize(miss)
    delta = None
    if sh["med_r_adj"] is not None and sm["med_r_adj"] is not None:
        delta = round(sh["med_r_adj"] - sm["med_r_adj"], 2)
    sid_neg = 0
    sid_pos = 0
    for sid, g in by_sid.items():
        if not g["hit"]:
            continue
        dh = median(g["hit"]) if g["hit"] else None
        dm = median(g["miss"]) if g["miss"] else None
        if dh is not None and dm is not None:
            if dh < dm:
                sid_neg += 1
            elif dh > dm:
                sid_pos += 1
    return {
        "factor": key,
        "scope": scope,
        "n_na": na,
        "hit": sh,
        "miss": sm,
        "delta_med": delta,
        "delta_win_rate": (
            round(sh["win_rate"] - sm["win_rate"], 1)
            if sh["win_rate"] is not None and sm["win_rate"] is not None
            else None
        ),
        "perm_p_avoid": permutation_p_avoid(hit, miss),
        "n_sids_hit": sum(1 for s, g in by_sid.items() if g["hit"]),
        "n_sids_delta_neg": sid_neg,
        "n_sids_delta_pos": sid_pos,
    }


def render_md(payload: dict) -> str:
    proto = payload["protocol"]
    lines = [
        "# 跨專家池 · 應避免圖形因子掃描",
        "",
        f"- 生成：{payload['generated']}",
        f"- 樣本：{proto['n_trades']} 筆 L1H7 · {proto['n_ledgers']} 檔 ledger · "
        f"績效欄位 `r_adj_pct`（β={proto['beta']}×{proto['bench']} · {proto['cost_bps']}bps）",
        f"- 日K：`stock_daily_bars` source={SOURCE} · 訊號日 T",
        f"- 盤中：`stock_kbar_1m` 09:00–09:30 · 進場日 T+1（缺 K 則盤中因子 NA）",
        "",
        "## 1. 因子排序（Δmed = 命中中位 − 未命中中位；越負越像「應避」）",
        "",
        "| 因子 | 說明 | 域 | n_hit | med_hit | win_hit | n_miss | med_miss | win_miss | Δmed | Δwin | perm_p |",
        "|------|------|----|------:|--------:|--------:|-------:|---------:|---------:|-----:|-----:|-------:|",
    ]
    for r in payload["ranked"]:
        desc = next(d for k, _, d in FACTORS if k == r["factor"])
        lines.append(
            f"| {r['factor']} | {desc} | {r['scope']} | {r['hit']['n']} | "
            f"{r['hit']['med_r_adj'] if r['hit']['med_r_adj'] is not None else '—'} | "
            f"{r['hit']['win_rate'] if r['hit']['win_rate'] is not None else '—'} | "
            f"{r['miss']['n']} | "
            f"{r['miss']['med_r_adj'] if r['miss']['med_r_adj'] is not None else '—'} | "
            f"{r['miss']['win_rate'] if r['miss']['win_rate'] is not None else '—'} | "
            f"{r['delta_med'] if r['delta_med'] is not None else '—'} | "
            f"{r['delta_win_rate'] if r['delta_win_rate'] is not None else '—'} | "
            f"{r['perm_p_avoid'] if r['perm_p_avoid'] is not None else '—'} |"
        )

    lines += [
        "",
        "註：`perm_p` 為 permutation 單尾（H1：命中組 med 更低）。",
        "",
        "## 2. 跨組候選共通避圖形（n_hit≥30）",
        "",
    ]
    cands = payload["cross_candidates"]
    if not cands:
        lines.append("_無因子同時滿足 n_hit≥30 且 Δmed≤−2%。_")
    else:
        for i, c in enumerate(cands, 1):
            lines.append(
                f"{i}. **{c['factor']}**（{c['description']}）— "
                f"n_hit={c['hit']['n']} · med_hit={c['hit']['med_r_adj']}% · "
                f"med_miss={c['miss']['med_r_adj']}% · Δmed={c['delta_med']}% · "
                f"perm_p={c['perm_p_avoid']} · "
                f"跨股一致（命中更差）{c['n_sids_delta_neg']}/{c['n_sids_hit']} 檔"
            )
    lines += [
        "",
        "## 3. 覆蓋與缺 K",
        "",
        f"- 日K特徵可用：{payload['coverage']['daily_ok']} / {proto['n_trades']}",
        f"- 盤中 1m 可用：{payload['coverage']['intraday_ok']} / {proto['n_trades']} "
        f"（{payload['coverage']['intraday_missing']} 筆缺 K）",
        "",
        "## 4. 限制",
        "",
        "- **倖存者偏差**：樣本來自已採納／在跑專家池 champion ledger，"
        "正向超額先被篩進池；負向因子可能被低估。",
        "- **多重檢定**：14 條因子未做 FDR/Bonferroni；"
        "perm_p 僅作探索排序，不宜逐條當 confirmatory。",
        "- **缺 K 偏差**：盤中因子僅覆蓋有 1m 的子樣本；"
        "若缺 K 非隨機（小票／較早日期），命中率先驗可能偏移。",
        "- **Δmed 解讀**：負值代表「命中該避形」的超額中位較低；"
        "仍需 OOS／freeze 與 per-sid 穩健性再談採納。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    trades_raw = load_all_trades()
    conn = connect_ro()
    try:
        enriched = enrich_trades(conn, trades_raw)
    finally:
        conn.close()

    results = []
    for key, scope, desc in FACTORS:
        r = analyze_factor(enriched, key, scope)
        r["description"] = desc
        results.append(r)

    ranked = sorted(
        results,
        key=lambda x: (x["delta_med"] if x["delta_med"] is not None else 999),
    )
    cross_candidates = [
        r
        for r in ranked
        if r["hit"]["n"] >= 30 and r["delta_med"] is not None and r["delta_med"] <= -2.0
    ][:5]
    if len(cross_candidates) < 3:
        extra = [
            r
            for r in ranked
            if r["hit"]["n"] >= 30
            and r not in cross_candidates
            and r["delta_med"] is not None
        ]
        for r in extra:
            if len(cross_candidates) >= 5:
                break
            cross_candidates.append(r)

    n_daily = sum(1 for t in enriched if (t.get("features") or {}).get("daily_ok"))
    n_intra = sum(1 for t in enriched if t.get("intraday_ok"))

    payload = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "protocol": {
            "n_trades": len(enriched),
            "n_ledgers": len(list(TRADES_DIR.glob("*.json"))),
            "trades_dir": str(TRADES_DIR.relative_to(ROOT)),
            "outcome": "r_adj_pct",
            "beta": 1.15,
            "bench": "IX0001",
            "cost_bps": 30,
            "hold": "L1H7",
            "daily_source": SOURCE,
            "intraday_window": "09:00-09:30",
            "perm_n": PERM_N,
        },
        "coverage": {
            "daily_ok": n_daily,
            "intraday_ok": n_intra,
            "intraday_missing": len(enriched) - n_intra,
        },
        "factors": results,
        "ranked": ranked,
        "cross_candidates": cross_candidates,
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUT_DIR / "H_CROSS_AVOID_SCAN.json"
    md_path = OUT_DIR / "H_CROSS_AVOID_SCAN.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    md_path.write_text(render_md(payload))

    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(f"trades={len(enriched)} daily_ok={n_daily} intraday_ok={n_intra}")
    for r in ranked[:5]:
        print(
            f"  {r['factor']:16s} n_hit={r['hit']['n']:3d} "
            f"Δmed={r['delta_med']} perm_p={r['perm_p_avoid']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
