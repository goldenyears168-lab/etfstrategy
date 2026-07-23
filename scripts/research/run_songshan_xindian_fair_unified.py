#!/usr/bin/env python3
"""凱基松山 vs 富邦新店 · unified excess fair table (Research-only).

Shared protocol (mandatory):
  L1 entry (T → next session open), cost 30bps,
  excess = stock − 1.15 × IX0001, mega-weight exclude,
  window ~2024-07-01 .. 2026-07-16, signal cut ~2026-07-03.

Writes:
  reports/research/branch-footprint-screen/chen_overnight_20260720/
    SONGSHAN_XINDIAN_FAIR.md
    songshan_xindian_fair.csv
"""
from __future__ import annotations

import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from stock_db.util import DEFAULT_DB_PATH  # noqa: E402

OUT = ROOT / "reports/research/branch-footprint-screen/chen_overnight_20260720"
OUT.mkdir(parents=True, exist_ok=True)

BRANCHES = (("9217", "凱基松山"), ("9661", "富邦新店"))
WEIGHT = {
    "2330",
    "2317",
    "2454",
    "2308",
    "2881",
    "2412",
    "3045",
    "2303",
    "2382",
    "2882",
    "2891",
    "2886",
}
COST = 0.003
BETA = 1.15
D0, D1 = "2024-07-01", "2026-07-16"
SIG_CUT = "2026-07-03"
SOURCE = "finmind"
HOLDS = (14, 20)
OOS_CUTS = ("2025-10-01", "2026-01-01")

# Published overnight-hunt medians (bare excess vs IX0001, no β) for gap note.
HUNT_PUBLISHED = {
    ("凱基松山", "frozen_150M_share25", 14): 8.42,  # SONGSHAN_STABILITY H14
    ("凱基松山", "frozen_150M_share25", 20): 15.98,  # SONGSHAN_STABILITY H20
    ("凱基松山", "frozen_150M_share25_wf", 14): 8.60,  # FROZEN_WF
    ("凱基松山", "frozen_150M_share25_wf", 20): 16.38,
    ("富邦新店", "frozen_200M_share40_wf", 14): 4.26,  # FROZEN_WF train80
    ("富邦新店", "frozen_200M_share40_wf", 20): 3.73,
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def connect_ro() -> sqlite3.Connection:
    uri = f"file:{DEFAULT_DB_PATH.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=120.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-64000")
    return conn


def year_windows(d0: str, d1: str) -> list[tuple[str, str]]:
    y0, y1 = int(d0[:4]), int(d1[:4])
    out: list[tuple[str, str]] = []
    for y in range(y0, y1 + 1):
        a = max(d0, f"{y}-01-01")
        b = min(d1, f"{y}-12-31")
        if a <= b:
            out.append((a, b))
    return out


def load_branch_top1(conn: sqlite3.Connection, branch: str) -> pd.DataFrame:
    parts: list[pd.DataFrame] = []
    for a, b in year_windows(D0, D1):
        log(f"  top1 {branch} chunk {a}..{b}")
        buys = pd.read_sql_query(
            """
            SELECT trade_date AS d, stock_id AS sid, buy
            FROM stock_broker_branch_daily
            WHERE securities_trader_id=? AND source=?
              AND trade_date>=? AND trade_date<=?
              AND stock_id != '__EMPTY__'
              AND length(stock_id)=4
              AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
              AND buy>0
            """,
            conn,
            params=[branch, SOURCE, a, b],
        )
        if buys.empty:
            continue
        buys["d"] = buys["d"].astype(str)
        buys["sid"] = buys["sid"].astype(str)
        sids = sorted(buys["sid"].unique())
        ph = ",".join("?" * len(sids))
        px = pd.read_sql_query(
            f"""
            SELECT stock_id AS sid, trade_date AS d, close
            FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph})
              AND trade_date>=? AND trade_date<=?
              AND close>0
            """,
            conn,
            params=[SOURCE, *sids, a, b],
        )
        if px.empty:
            continue
        px["d"] = px["d"].astype(str)
        px["sid"] = px["sid"].astype(str)
        raw = buys.merge(px, on=["sid", "d"], how="inner")
        raw["buy_amt"] = raw["buy"] * raw["close"]
        day_buy = raw.groupby("d")["buy_amt"].transform("sum")
        raw["buy_share"] = raw["buy_amt"] / day_buy.replace(0, np.nan)
        raw["rk"] = raw.groupby("d")["buy_amt"].rank(method="first", ascending=False)
        parts.append(raw.loc[raw["rk"] == 1, ["d", "sid", "buy_amt", "buy_share"]].copy())
        log(f"    kept top1 days={len(parts[-1])}")
    if not parts:
        return pd.DataFrame(columns=["d", "sid", "buy_amt", "buy_share"])
    return pd.concat(parts, ignore_index=True)


def summarize(legs: list[dict], min_n: int = 5) -> dict:
    if len(legs) < min_n:
        return {
            "n": len(legs),
            "ok": False,
            "excess_med": np.nan,
            "excess_mean": np.nan,
            "stock_med": np.nan,
            "win": np.nan,
            "max1": np.nan,
            "slot_n": 0,
            "slot_med": np.nan,
            "slot_dd": np.nan,
        }
    xs = np.array([x["excess_pct"] for x in legs], float)
    ss = np.array([x["stock_pct"] for x in legs], float)
    ctr = Counter(x["stock_id"] for x in legs)
    taken: list[dict] = []
    busy = None
    for lg in sorted(legs, key=lambda z: z["entry_date"]):
        if busy is not None and lg["entry_date"] <= busy:
            continue
        taken.append(lg)
        busy = lg["exit_date"]
    txs = np.array([x["excess_pct"] for x in taken], float) if taken else np.array([])
    trs = np.array([x["stock_pct"] for x in taken], float) / 100.0 if taken else np.array([])
    if len(trs):
        eq = np.cumprod(1.0 + trs)
        dd = float((eq / np.maximum.accumulate(eq) - 1.0).min() * 100.0)
        slot_med = float(np.median(txs))
    else:
        dd, slot_med = np.nan, np.nan
    return {
        "n": len(legs),
        "ok": True,
        "excess_med": float(np.median(xs)),
        "excess_mean": float(np.mean(xs)),
        "stock_med": float(np.median(ss)),
        "win": float((xs > 0).mean() * 100.0),
        "max1": max(ctr.values()) / len(legs),
        "slot_n": len(taken),
        "slot_med": slot_med,
        "slot_dd": dd,
    }


def rule_mask(df: pd.DataFrame, rule: str) -> pd.Series:
    base = ~df["sid"].isin(WEIGHT)
    if rule == "frozen_150M_share25":
        return base & (df["buy_amt"] >= 1.5e8) & (df["buy_share"] >= 0.25)
    if rule == "80M":
        return base & (df["buy_amt"] >= 8e7)
    if rule == "80M_W20":
        return base & (df["buy_amt"] >= 8e7) & df["above20"]
    if rule == "200M_share40":
        return base & (df["buy_amt"] >= 2e8) & (df["buy_share"] >= 0.40)
    if rule == "500M_share40":
        return base & (df["buy_amt"] >= 5e8) & (df["buy_share"] >= 0.40)
    if rule == "shuanghong_200M_share25":
        return (
            base
            & (df["buy_amt"] >= 2e8)
            & (df["buy_share"] >= 0.25)
            & df["above20"]
            & df["above60"]
            & (df["ret60"] > 0)
        )
    raise KeyError(rule)


RULES = (
    "frozen_150M_share25",
    "80M",
    "80M_W20",
    "200M_share40",
    "500M_share40",
    "shuanghong_200M_share25",
)


def main() -> int:
    t0 = time.time()
    conn = connect_ro()
    log("load calendar + benchmark")
    cal = [
        str(r[0])
        for r in conn.execute(
            """
            SELECT DISTINCT trade_date FROM stock_daily_bars
            WHERE source=? AND trade_date>=? AND trade_date<=date(?, '+40 day')
            ORDER BY trade_date
            """,
            (SOURCE, D0, D1),
        )
    ]
    di = {d: i for i, d in enumerate(cal)}
    bench_rows = conn.execute(
        """
        SELECT date, close FROM daily_bars
        WHERE code='IX0001' AND date>=? AND date<=date(?, '+40 day')
        ORDER BY date
        """,
        (D0, D1),
    ).fetchall()
    if not bench_rows:
        bench_rows = conn.execute(
            """
            SELECT trade_date, close FROM stock_daily_bars
            WHERE stock_id='IX0001' AND source=? AND trade_date>=?
            ORDER BY trade_date
            """,
            (SOURCE, D0),
        ).fetchall()
    bench = {str(a): float(b) for a, b in bench_rows if b}

    annotated: dict[str, pd.DataFrame] = {}
    ohlc_px: dict[tuple[str, str], tuple[float, float]] = {}

    for bid, name in BRANCHES:
        log(f"load {name} ({bid}) Top1")
        top1 = load_branch_top1(conn, bid)
        top1 = top1[top1["d"] <= SIG_CUT].copy()
        sids = sorted(top1["sid"].unique())
        if not sids:
            annotated[bid] = pd.DataFrame()
            continue
        log(f"  load ohlc for {len(sids)} stocks")
        ph = ",".join("?" * len(sids))
        ohlc_df = pd.read_sql_query(
            f"""
            SELECT stock_id AS sid, trade_date AS d, open, close
            FROM stock_daily_bars
            WHERE source=? AND stock_id IN ({ph})
              AND trade_date>=date(?, '-120 day') AND trade_date<=date(?, '+40 day')
              AND open>0 AND close>0
            ORDER BY stock_id, trade_date
            """,
            conn,
            params=[SOURCE, *sids, D0, D1],
        )
        ohlc_df["sid"] = ohlc_df["sid"].astype(str)
        ohlc_df["d"] = ohlc_df["d"].astype(str)
        for r in ohlc_df.itertuples():
            ohlc_px[(r.sid, r.d)] = (float(r.open), float(r.close))
        feat_rows = []
        for sid, g in ohlc_df.groupby("sid"):
            g = g.sort_values("d").copy()
            g["sma20"] = g["close"].rolling(20).mean()
            g["sma60"] = g["close"].rolling(60).mean()
            g["ret60"] = g["close"].pct_change(60)
            feat_rows.append(g)
        feat = pd.concat(feat_rows, ignore_index=True)
        feat_map = {(r.sid, r.d): r for r in feat.itertuples()}
        rows = []
        for r in top1.itertuples():
            f = feat_map.get((r.sid, r.d))
            if f is None or pd.isna(getattr(f, "sma60", np.nan)):
                continue
            rows.append(
                {
                    "d": r.d,
                    "sid": r.sid,
                    "buy_amt": float(r.buy_amt),
                    "buy_share": float(r.buy_share) if pd.notna(r.buy_share) else np.nan,
                    "above20": bool(f.close > f.sma20) if pd.notna(f.sma20) else False,
                    "above60": bool(f.close > f.sma60) if pd.notna(f.sma60) else False,
                    "ret60": float(f.ret60) if pd.notna(f.ret60) else np.nan,
                }
            )
        annotated[bid] = pd.DataFrame(rows)
        log(f"  annotated={len(annotated[bid])}")
        del ohlc_df, feat, feat_rows, feat_map, top1

    conn.close()

    def make_leg(sid: str, sig: str, hold: int) -> dict | None:
        i = di.get(sig)
        if i is None:
            return None
        ei, xi = i + 1, i + hold
        if xi >= len(cal):
            return None
        ed, xd = cal[ei], cal[xi]
        er = ohlc_px.get((sid, ed))
        xr = ohlc_px.get((sid, xd))
        be, bx = bench.get(ed), bench.get(xd)
        if not er or not xr or not be or not bx:
            # fallback: entry close if open missing (exit still needs close)
            return None
        entry_px = er[0]
        exit_px = xr[1]
        if entry_px <= 0 or exit_px <= 0 or be <= 0 or bx <= 0:
            return None
        sr = exit_px / entry_px - 1.0 - COST
        br = bx / be - 1.0
        return {
            "signal_date": sig,
            "entry_date": ed,
            "exit_date": xd,
            "stock_id": sid,
            "stock_pct": sr * 100.0,
            "excess_pct": (sr - BETA * br) * 100.0,
        }

    rows_out: list[dict] = []
    for bid, name in BRANCHES:
        adf = annotated.get(bid)
        if adf is None or adf.empty:
            log(f"SKIP {name}: no annotated tape")
            continue
        for rule in RULES:
            sub = adf.loc[rule_mask(adf, rule)].copy()
            for hold in HOLDS:
                # full window
                legs = []
                for r in sub.itertuples():
                    lg = make_leg(r.sid, r.d, hold)
                    if lg:
                        legs.append(lg)
                st = summarize(legs, min_n=5)
                rows_out.append(
                    {
                        "branch": name,
                        "branch_id": bid,
                        "rule": rule,
                        "hold": hold,
                        "window": "full",
                        "cut": "",
                        **st,
                    }
                )
                log(
                    f"{name} {rule} H{hold} full: n={st['n']} "
                    + (
                        f"med={st['excess_med']:+.2f} slot={st['slot_med']:+.2f}"
                        if st["ok"]
                        else "(thin)"
                    )
                )
                for cut in OOS_CUTS:
                    oos_legs = [lg for lg in legs if lg["signal_date"] >= cut]
                    st2 = summarize(oos_legs, min_n=5)
                    rows_out.append(
                        {
                            "branch": name,
                            "branch_id": bid,
                            "rule": rule,
                            "hold": hold,
                            "window": "oos",
                            "cut": cut,
                            **st2,
                        }
                    )

    rdf = pd.DataFrame(rows_out)
    csv_path = OUT / "songshan_xindian_fair.csv"
    rdf.to_csv(csv_path, index=False)

    def pick(branch: str, rule: str, hold: int, window: str = "full", cut: str = "") -> dict | None:
        m = (
            (rdf["branch"] == branch)
            & (rdf["rule"] == rule)
            & (rdf["hold"] == hold)
            & (rdf["window"] == window)
            & (rdf["cut"].fillna("") == cut)
        )
        sub = rdf.loc[m]
        if sub.empty:
            return None
        return sub.iloc[0].to_dict()

    # Key comparison gaps under unified scoring
    gap_lines: list[str] = []
    for hold in HOLDS:
        ss = pick("凱基松山", "frozen_150M_share25", hold)
        xd = pick("富邦新店", "200M_share40", hold)
        xd80 = pick("富邦新店", "80M", hold)
        if ss and xd and ss.get("ok") and xd.get("ok"):
            gap = float(ss["excess_med"]) - float(xd["excess_med"])
            gap_lines.append(
                f"- H{hold} 松山 `frozen_150M∩25%` − 新店 `200M∩40%` = "
                f"**{gap:+.2f}pp** "
                f"(松山 {ss['excess_med']:+.2f}% n={int(ss['n'])} · "
                f"新店 {xd['excess_med']:+.2f}% n={int(xd['n'])})"
            )
        if ss and xd80 and ss.get("ok") and xd80.get("ok"):
            gap80 = float(ss["excess_med"]) - float(xd80["excess_med"])
            gap_lines.append(
                f"- H{hold} 松山 `frozen_150M∩25%` − 新店 `80M` = "
                f"**{gap80:+.2f}pp** "
                f"(新店 {xd80['excess_med']:+.2f}% n={int(xd80['n'])})"
            )

    # Same-rule gaps
    same_rule_lines: list[str] = []
    for rule in RULES:
        for hold in HOLDS:
            ss = pick("凱基松山", rule, hold)
            xd = pick("富邦新店", rule, hold)
            if not ss or not xd or not ss.get("ok") or not xd.get("ok"):
                continue
            same_rule_lines.append(
                f"- H{hold} `{rule}`: 松山 {ss['excess_med']:+.2f}% (n={int(ss['n'])}) "
                f"− 新店 {xd['excess_med']:+.2f}% (n={int(xd['n'])}) = "
                f"**{float(ss['excess_med']) - float(xd['excess_med']):+.2f}pp**"
            )

    hunt_note = []
    ss14 = pick("凱基松山", "frozen_150M_share25", 14)
    ss20 = pick("凱基松山", "frozen_150M_share25", 20)
    if ss14 and ss14.get("ok"):
        pub = HUNT_PUBLISHED[("凱基松山", "frozen_150M_share25", 14)]
        hunt_note.append(
            f"- 松山 H14 frozen: hunt published bare-excess med **+{pub:.2f}%** → "
            f"unified β=1.15 med **{ss14['excess_med']:+.2f}%** "
            f"(Δ {float(ss14['excess_med']) - pub:+.2f}pp)"
        )
    if ss20 and ss20.get("ok"):
        pub = HUNT_PUBLISHED[("凱基松山", "frozen_150M_share25", 20)]
        hunt_note.append(
            f"- 松山 H20 frozen: hunt published bare-excess med **+{pub:.2f}%** → "
            f"unified β=1.15 med **{ss20['excess_med']:+.2f}%** "
            f"(Δ {float(ss20['excess_med']) - pub:+.2f}pp)"
        )
    xd14 = pick("富邦新店", "200M_share40", 14)
    if xd14 and xd14.get("ok"):
        pub = HUNT_PUBLISHED[("富邦新店", "frozen_200M_share40_wf", 14)]
        hunt_note.append(
            f"- 新店 H14 `200M∩40%`: hunt WF published bare **+{pub:.2f}%** → "
            f"unified full-window **{xd14['excess_med']:+.2f}%** "
            f"(not identical sample; protocol shift)"
        )

    # Verdict
    verdict = "inconclusive"
    if ss14 and xd14 and ss14.get("ok") and xd14.get("ok"):
        gap = float(ss14["excess_med"]) - float(xd14["excess_med"])
        if gap >= 3.0 and float(ss14["excess_med"]) > float(xd14["excess_med"]):
            verdict = (
                f"**仍成立（弱化）**：松山 frozen H14 中位仍高於新店 200M∩40% "
                f"（Δ={gap:+.2f}pp），但絕對中位已從 hunt 裸超額 ~+8% 壓到 "
                f"β=1.15 的 {ss14['excess_med']:+.2f}%。"
            )
        elif gap > 0:
            verdict = (
                f"**方向仍偏松山，但優勢縮小**：H14 Δ={gap:+.2f}pp "
                f"（松山 {ss14['excess_med']:+.2f}% vs 新店 {xd14['excess_med']:+.2f}%）。"
            )
        else:
            verdict = (
                f"**不再成立**：統一超額下 H14 松山 {ss14['excess_med']:+.2f}% "
                f"≤ 新店 {xd14['excess_med']:+.2f}%（Δ={gap:+.2f}pp）。"
            )

    lines = [
        "# 凱基松山 vs 富邦新店 · Unified Excess Fair Table\n\n",
        f"- 生成：{datetime.now().isoformat(timespec='seconds')}\n",
        f"- 窗：`{D0}`..`{D1}` · 訊號截止 `{SIG_CUT}`\n",
        f"- 協議：**L1**（訊號日 T → 次日開）· 成本 **30bps** · "
        f"excess = 股 − **{BETA}×IX0001** · 剔 mega 權值 "
        f"`{','.join(sorted(WEIGHT))}`\n",
        f"- SQLite：read-only URI `{DEFAULT_DB_PATH.name}`\n",
        f"- 耗時：{(time.time() - t0) / 60:.1f} 分\n",
        "- Research only · 不採納 Strategy／Order\n\n",
        "## Verdict\n\n",
        f"{verdict}\n\n",
        "### 相對 hunt 發表數字\n\n",
    ]
    lines.extend(f"{x}\n" for x in hunt_note)
    lines.append("\n### 統一計分下的 松山−新店 gap\n\n")
    lines.extend(f"{x}\n" for x in gap_lines)
    lines.append("\n### 同規則 gap（若兩邊皆有樣本）\n\n")
    if same_rule_lines:
        lines.extend(f"{x}\n" for x in same_rule_lines)
    else:
        lines.append("- （無兩邊皆 ok 的同規則列）\n")

    lines += [
        "\n## 規則定義\n\n",
        "| rule | 含義 |\n|------|------|\n",
        "| frozen_150M_share25 | overnight hunt 凍結：≥1.5億∩25%（松山 champion） |\n",
        "| 80M | Top1 ≥8000萬（新店常見密度規則） |\n",
        "| 80M_W20 | ≥80M 且收盤 > SMA20 |\n",
        "| 200M_share40 | ≥2億∩40%（新店高門檻） |\n",
        "| 500M_share40 | ≥5億∩40% |\n",
        "| shuanghong_200M_share25 | ≥2億∩25% + >SMA20∧>SMA60∧ret60>0 |\n\n",
        "## Full window · H14 / H20\n\n",
    ]

    for hold in HOLDS:
        lines.append(f"### H{hold}\n\n")
        lines.append(
            "| branch | rule | n | excess中位% | win% | excess均值% | slot_n | slot中位% |\n"
            "|--------|------|--:|-----------:|-----:|------------:|-------:|----------:|\n"
        )
        for bid, name in BRANCHES:
            for rule in RULES:
                r = pick(name, rule, hold)
                if r is None:
                    continue
                if not r.get("ok"):
                    lines.append(
                        f"| {name} | `{rule}` | {int(r['n'])} | thin | — | — | "
                        f"{int(r.get('slot_n') or 0)} | — |\n"
                    )
                else:
                    lines.append(
                        f"| {name} | `{rule}` | {int(r['n'])} | {r['excess_med']:+.2f} | "
                        f"{r['win']:.0f} | {r['excess_mean']:+.2f} | {int(r['slot_n'])} | "
                        f"{r['slot_med']:+.2f} |\n"
                    )
        lines.append("\n")

    lines.append("## Calendar OOS\n\n")
    for cut in OOS_CUTS:
        lines.append(f"### 自 `{cut}` · H14 / H20\n\n")
        lines.append(
            "| branch | rule | H | n | excess中位% | win% | slot_n | slot中位% |\n"
            "|--------|------|--:|--:|-----------:|-----:|-------:|----------:|\n"
        )
        for hold in HOLDS:
            for bid, name in BRANCHES:
                for rule in (
                    "frozen_150M_share25",
                    "80M",
                    "80M_W20",
                    "200M_share40",
                    "500M_share40",
                    "shuanghong_200M_share25",
                ):
                    r = pick(name, rule, hold, window="oos", cut=cut)
                    if r is None:
                        continue
                    if not r.get("ok"):
                        lines.append(
                            f"| {name} | `{rule}` | {hold} | {int(r['n'])} | thin | — | "
                            f"{int(r.get('slot_n') or 0)} | — |\n"
                        )
                    else:
                        lines.append(
                            f"| {name} | `{rule}` | {hold} | {int(r['n'])} | "
                            f"{r['excess_med']:+.2f} | {r['win']:.0f} | "
                            f"{int(r['slot_n'])} | {r['slot_med']:+.2f} |\n"
                        )
        lines.append("\n")

    lines += [
        "## 1-slot skip-when-full（已併入上表）\n\n",
        "- `slot_n` / `slot_med` / `slot_dd`（CSV 欄）：訊號依進場日排序，"
        "持倉未平倉則跳過新訊號（1 槽）。\n",
        "- 決策主看 **excess_med（全訊號）**；slot 指標作可部署密度參考。\n\n",
        "## 產物\n\n",
        f"- `{csv_path.name}`\n",
        "- `SONGSHAN_XINDIAN_FAIR.md`（本檔）\n",
    ]
    md_path = OUT / "SONGSHAN_XINDIAN_FAIR.md"
    md_path.write_text("".join(lines), encoding="utf-8")
    log(f"WROTE {md_path}")
    log(f"WROTE {csv_path}")
    log(f"DONE elapsed={(time.time() - t0) / 60:.1f}m")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
