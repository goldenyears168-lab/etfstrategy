#!/usr/bin/env python3
"""Build expert-pool trade knowledge base (L1H7 ledgers + A/B/C tier notes).

  PYTHONPATH=src .venv/bin/python scripts/research/build_expert_pool_trade_knowledge.py

Writes:
  reports/.../expert_pool/knowledge/TIER_NOTES.md
  reports/.../expert_pool/knowledge/INDEX.md
  reports/.../expert_pool/knowledge/trades/{sid}.json|.md
"""
from __future__ import annotations

import importlib.util
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402

SOURCE = "finmind"
START, END = "2024-07-01", "2026-07-17"
COST, HOLD, DEDUP, BETA = 0.003, 7, 5, 1.15
KB = (
    ROOT
    / "reports"
    / "research"
    / "branch-footprint-screen"
    / "expert_pool"
    / "knowledge"
)
REPORT_ROOT = KB.parent
EVAL_REGISTRY = REPORT_ROOT / "EVAL_REGISTRY.json"

TIER_NOTES = r"""# 專家池信任分級備註（郵件／知識庫 SSOT）

協議：L1H7 · 30bps · \(r_{adj}=r_{股}-1.15\times r_{IX0001}\) · 採納中位 ≥＋2%。
""" + """
## A · 強專家＋高超額（優先認真跟）

hard≥4 且 med 多半 ≥5%：專家池厚、共識規則穩。

| 代號 | 名稱 | hard | med r_adj | n | 冠軍規則 | 研究一句話 |
|------|------|-----:|----------:|--:|----------|------------|
| 2327 | 國巨 | 6 | +8.7% | 12 | 回看·0.5億·H10 skip | 被動元件專家池最完整之一；Step0 仍穩 |
| 2303 | 聯電 | 6 | +8.1% | 12 | 回看·0.5億·H10 extend | 代工權值但超額仍厚；安和／建國等常客 |
| 3443 | 創意 | 5 | +9.0% | 4 | 回看·1億·H7 skip | 超額漂亮但 n=4 偏薄 |
| 6669 | 緯穎 | 5 | +5.4% | 15 | 回看·0.5億·H10 extend | 伺服器主軸；凱基系多分點共現 |
| 2383 | 台光電 | 6 | +5.1% | 11 | 回看·0.5億·H7 extend | CCL／AI 材料鏈；硬專家多 |
| 2408 | 南亞科 | 10 | +6.3% | 17 | 回看·0.5億·H7 skip | hard 最多；記憶體專家密度最高 |
| 3189 | 景碩 | 4 | +6.9% | 12 | 回看·1億·H10 skip | 載板；門檻用 1億 較嚴 |
| 3037 | 欣興 | 4 | +5.0% | 18 | 回看·0.5億·H10 skip | 載板；樣本夠、貼「高超額」下沿 |

## B · 共識池、超額仍佳（好觀察）

hard 2～3，med 多 ≥5%。

| 代號 | 名稱 | hard | med | 重點 |
|------|------|-----:|----:|------|
| 2337 | 旺宏 | 3 | +11.4% | 原池；記憶體，超額頂尖 |
| 3481 | 群創 | 3 | +11.3% | 面板；高超額但產業β不同 |
| 3653 | 健策 | 3 | +10.0% | 散熱；本輪 U2 明星之一 |
| 6239 | 力成 | 2 | +10.7% | 封測；n=4 略薄 |
| 6223 | 旺矽 | 3 | +6.5% | n=25 最密；測試座，OR 冠軍 |
| 3665 | 貿聯 | 3 | +5.8% | 高速線纜／AI 連線 |
| 6515 | 穎崴 | 2 | +5.6% | 測試座；與旺矽同類 |

## C · 共識有、但貼門檻（權值／大型）

有共識，超額剛過 2～5%——「跟得到、超額薄」。

| 代號 | 名稱 | hard | med | 註 |
|------|------|-----:|----:|-----|
| 2317 | 鴻海 | 7 | +2.0% | hard 很高，超額幾乎貼門檻 |
| 2308 | 台達電 | 7 | +2.8% | 同上；n=31 很密 |
| 3711 | 日月光 | 3 | +3.0% | 大型封測 |
| 6274 | 台燿 | 2 | +3.9% | CCL；n=28，OR |
| 3105 | 穩懋 | 2 | +4.5% | 化合物 |
| 4958 | 臻鼎 | 2 | +4.5% | 載板 |
| 2344 | 華邦電 | — | +3.6% | 原池；Step0 最貼門檻的舊檔 |

## D · 單薄 OR（降權）

hard=1 或 OR 單席：研究可看，實務降權；極端小樣本（如雷虎）不當主倉邏輯。

逐筆交易見 `knowledge/trades/{sid}.md`。
"""


def connect_ro(db_path: Path = DEFAULT_DB_PATH):
    import sqlite3

    c = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=120)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=120000")
    return c


def load_watch_mod():
    path = ROOT / "scripts/research/run_expert_pool_watch.py"
    spec = importlib.util.spec_from_file_location("epw_kb", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["epw_kb"] = mod
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def load_ix(conn):
    pad_s = "2024-05-01"
    pad_e = "2026-08-31"
    rows = conn.execute(
        """
        SELECT date, open, close FROM daily_bars
        WHERE code='IX0001' AND date BETWEEN ? AND ? AND open>0 AND close>0
        ORDER BY date,
          CASE source WHEN 'yahoo' THEN 0 WHEN 'tej' THEN 1 WHEN 'finmind' THEN 2 ELSE 3 END
        """,
        (pad_s, pad_e),
    ).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r["date"], (float(r["open"]), float(r["close"])))
    return [(d, o, c) for d, (o, c) in sorted(out.items())]


def load_stock(conn, sid: str):
    rows = conn.execute(
        """
        SELECT trade_date, open, close FROM stock_daily_bars
        WHERE stock_id=? AND source=? AND trade_date BETWEEN ? AND ?
          AND close>0
        ORDER BY trade_date
        """,
        (sid, SOURCE, "2024-05-01", "2026-08-31"),
    ).fetchall()
    bars = []
    for r in rows:
        o = float(r["open"]) if r["open"] else float(r["close"])
        bars.append((r["trade_date"], o, float(r["close"])))
    return bars


def next_open(bars, sig):
    for d, o, _c in bars:
        if d > sig and o > 0:
            return d, o
    return None, None


def exit_close(bars, entry, hold=HOLD):
    ordered = [x for x in bars if x[0] >= entry]
    if len(ordered) < hold:
        return None, None
    return ordered[hold - 1][0], ordered[hold - 1][2]


def prev_day(bars, date):
    prev = None
    for d, *_ in bars:
        if d >= date:
            break
        prev = d
    return prev


def build_signals(mode, floor, by_date, cal, amts):
    if "回看" in mode:
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
    if "同日" in mode:
        return [
            d
            for d in cal
            if len({t for t in by_date.get(d, set()) if amts.get((t, d), 0) >= floor}) >= 2
        ]
    return [
        d
        for d in cal
        if any(amts.get((t, d), 0) >= floor for t in by_date.get(d, set()))
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


def branch_snapshot(core, by_date, amts, sig, yday, floor):
    """Which core branches were on the signal (today and/or yesterday)."""
    rows = []
    for tid, name in core.items():
        today_a = amts.get((tid, sig), 0.0)
        yday_a = amts.get((tid, yday), 0.0) if yday else 0.0
        roles = []
        if today_a >= floor:
            roles.append("今")
        if yday and yday_a >= floor:
            roles.append("昨")
        if not roles:
            continue
        rows.append(
            {
                "tid": tid,
                "name": name,
                "role": "+".join(roles),
                "amt_today": round(today_a, 0),
                "amt_yday": round(yday_a, 0),
                "amt_yi": round(today_a / 1e8, 3),
                "amt_yday_yi": round(yday_a / 1e8, 3),
            }
        )
    rows.sort(key=lambda x: -(x["amt_today"] or x["amt_yday"]))
    return rows


def build_ledger(conn, sid, name, core, mode, floor, floor_label, policy, thin, tier, blurb):
    bars = load_stock(conn, sid)
    ix = load_ix(conn)
    if len(bars) < 40:
        return None
    cal = [d for d, *_ in bars if START <= d <= END]
    ph = ",".join("?" * len(core))
    raw = conn.execute(
        f"""
        SELECT b.securities_trader_id, b.trade_date, b.net, p.close
        FROM stock_broker_branch_daily b
        JOIN stock_daily_bars p
          ON p.stock_id=b.stock_id AND p.trade_date=b.trade_date AND p.source=?
        WHERE b.stock_id=? AND b.source=? AND b.trade_date BETWEEN ? AND ?
          AND b.securities_trader_id IN ({ph}) AND b.net>0 AND p.close>0
        """,
        (SOURCE, sid, SOURCE, START, END, *core.keys()),
    ).fetchall()
    amts = {}
    by_date: dict[str, set[str]] = defaultdict(set)
    for tid, d, net, close in raw:
        a = float(net) * float(close)
        amts[(tid, d)] = a
        if a >= 5e7:
            by_date[d].add(tid)

    sigs = dedupe(build_signals(mode, floor, by_date, cal, amts))
    trades = []
    for sig in sigs:
        ed, eo = next_open(bars, sig)
        if not ed or not eo:
            continue
        xd, xc = exit_close(bars, ed, HOLD)
        if not xd or not xc:
            continue
        r_s = xc / eo - 1 - COST
        be, bo = next_open(ix, sig)
        if not be or not bo:
            continue
        _, bc = exit_close(ix, be, HOLD)
        if not bc:
            continue
        r_ix = bc / bo - 1
        r_adj = r_s - BETA * r_ix
        yday = prev_day(bars, sig)
        branches = branch_snapshot(core, by_date, amts, sig, yday, floor)
        trades.append(
            {
                "signal_date": sig,
                "yday": yday,
                "entry_date": ed,
                "entry_open": round(eo, 4),
                "exit_date": xd,
                "exit_close": round(xc, 4),
                "hold_bars": HOLD,
                "r_pct": round(r_s * 100, 2),
                "r_ix_pct": round(r_ix * 100, 2),
                "r_adj_pct": round(r_adj * 100, 2),
                "branches": branches,
            }
        )

    rs = [t["r_pct"] for t in trades]
    ra = [t["r_adj_pct"] for t in trades]
    return {
        "stock_id": sid,
        "stock_name": name,
        "tier": tier,
        "blurb": blurb,
        "thin": thin,
        "protocol": {
            "window": f"{START}..{END}",
            "mode": mode,
            "floor": floor_label,
            "floor_amt": floor,
            "policy": policy,
            "hold": f"L1H{HOLD}",
            "cost_bps": int(COST * 10000),
            "dedupe_days": DEDUP,
            "beta": BETA,
            "bench": "IX0001",
        },
        "core": core,
        "summary": {
            "n": len(trades),
            "med_r_pct": round(median(rs), 2) if rs else None,
            "med_radj_pct": round(median(ra), 2) if ra else None,
            "win_r": round(sum(1 for x in rs if x > 0) / len(rs) * 100, 1) if rs else None,
            "win_radj": round(sum(1 for x in ra if x > 0) / len(ra) * 100, 1) if ra else None,
        },
        "trades": trades,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def write_trade_md(ledger: dict) -> str:
    p = ledger["protocol"]
    s = ledger["summary"]
    lines = [
        f"# {ledger['stock_name']}（{ledger['stock_id']}）· L1H7 交易知識庫",
        "",
        f"- 分級：**{ledger['tier']}** · {ledger.get('blurb') or ''}",
        f"- 規則：{p['mode']} · {p['floor']} · {p['policy']}",
        f"- 協議：{p['window']} · {p['hold']} · {p['cost_bps']}bps · "
        f"r_adj=r−{p['beta']}×{p['bench']} · 去重{p['dedupe_days']}日",
        f"- 核心：{' / '.join(ledger['core'].values())}",
        f"- 摘要：n={s['n']} · 中位真實報酬={s['med_r_pct']}% · "
        f"中位超額={s['med_radj_pct']}% · 勝率(r)={s['win_r']}% · 勝率(adj)={s['win_radj']}%",
        "",
        "| # | 訊號日 | 進場開 | 出場收 | 真實% | 大盤% | 超額% | 觸發分點（今/昨達標） |",
        "|--:|--------|--------|--------|------:|------:|------:|----------------------|",
    ]
    for i, t in enumerate(ledger["trades"], 1):
        br = "；".join(
            f"{b['name']}[{b['role']}]"
            + (f"今{b['amt_yi']:.2f}億" if b["amt_today"] else "")
            + (f"/昨{b['amt_yday_yi']:.2f}億" if b["amt_yday"] else "")
            for b in t["branches"]
        ) or "—"
        lines.append(
            f"| {i} | {t['signal_date']} | {t['entry_date']}@{t['entry_open']} | "
            f"{t['exit_date']}@{t['exit_close']} | {t['r_pct']:+.2f} | "
            f"{t['r_ix_pct']:+.2f} | {t['r_adj_pct']:+.2f} | {br} |"
        )
    lines += ["", f"_updated {ledger['updated_at']}_", ""]
    return "\n".join(lines)


def main() -> int:
    mod = load_watch_mod()
    registry = mod.load_registry_by_sid()
    conn = connect_ro()
    KB.mkdir(parents=True, exist_ok=True)
    (KB / "trades").mkdir(exist_ok=True)
    (KB / "TIER_NOTES.md").write_text(TIER_NOTES, encoding="utf-8")

    index_rows = []
    for sid, spec in sorted(mod.POOLS.items()):
        row = registry.get(sid) or {}
        thin = "單薄" in (spec.origin_note or "") or bool(row.get("thin"))
        tier = mod.research_tier(row, thin_flag=thin)
        blurb = mod.RESEARCH_BLURBS.get(sid, "")
        # champion from watch_spec if present
        ws = REPORT_ROOT / sid / "watch_spec.json"
        mode, floor_label, policy = "回看1日共識", spec.floor_label, "H10_skip"
        if ws.exists():
            w = json.loads(ws.read_text(encoding="utf-8"))
            ch = w.get("champion") or {}
            mode = ch.get("mode") or mode
            floor_label = w.get("floor_label") or ch.get("floor") or floor_label
            policy = ch.get("policy") or policy
        elif row.get("champion_mode"):
            mode = row.get("champion_mode") or mode
            floor_label = row.get("champion_floor") or floor_label
            policy = row.get("champion_policy") or policy
        # 2344 hardcoded same-day in earlier study — keep watch_spec if any
        floor = 1e8 if "1億" in str(floor_label) else 5e7
        print(f"ledger {sid} {spec.stock_name} tier={tier} mode={mode}")
        ledger = build_ledger(
            conn,
            sid,
            spec.stock_name,
            dict(spec.core),
            mode,
            floor,
            floor_label,
            policy,
            thin,
            tier,
            blurb,
        )
        if not ledger:
            print(f"  skip thin bars {sid}")
            continue
        jp = KB / "trades" / f"{sid}.json"
        mp = KB / "trades" / f"{sid}.md"
        jp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        mp.write_text(write_trade_md(ledger), encoding="utf-8")
        s = ledger["summary"]
        index_rows.append(
            {
                "sid": sid,
                "name": spec.stock_name,
                "tier": tier,
                "n": s["n"],
                "med_r": s["med_r_pct"],
                "med_adj": s["med_radj_pct"],
                "path": f"trades/{sid}.md",
            }
        )

    index_rows.sort(key=lambda x: ({"A": 0, "B": 1, "C": 2, "D": 3}.get(x["tier"], 9), -(x["med_adj"] or -999)))
    idx = [
        "# 專家池交易知識庫 INDEX",
        "",
        f"- updated: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- stocks: **{len(index_rows)}**",
        "- tier notes: `TIER_NOTES.md`",
        "",
        "| 分級 | 代號 | 名稱 | n | 中位真實% | 中位超額% | 明細 |",
        "|---|---|---|--:|--:|--:|---|",
    ]
    for r in index_rows:
        idx.append(
            f"| {r['tier']} | {r['sid']} | {r['name']} | {r['n']} | "
            f"{r['med_r']} | {r['med_adj']} | [`{r['path']}`]({r['path']}) |"
        )
    idx.append("")
    (KB / "INDEX.md").write_text("\n".join(idx) + "\n", encoding="utf-8")
    # machine index
    (KB / "index.json").write_text(
        json.dumps({"stocks": index_rows, "updated_at": datetime.now().isoformat()}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(f"KB → {KB} ({len(index_rows)} stocks)")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
