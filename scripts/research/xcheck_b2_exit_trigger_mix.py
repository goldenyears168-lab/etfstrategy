#!/usr/bin/env python3
"""對抗性複核 B2：出場觸發方向的組成（決定「市價出場滑價」該用哪個參考點）。

B2 把市價出場成本量成「隨機時刻的 mid→對側半價差」＝1.6 點。但 causal_engine
的 close_side(t, px) 把出場記在**觸發那一筆成交的價格 px** 上，不是 mid。
所以 backtest 少算的其實是 (px − bid)（多單出場）或 (ask − px)（空單出場），
而那個量取決於觸發方向。本腳本只做一件事：統計 why 的組成。
"""
from __future__ import annotations
import sys, json, sqlite3
from collections import Counter
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from tmf_channel import tick_index as _ti           # noqa: E402
from tmf_channel.cache_store import load_day        # noqa: E402
from tmf_channel.engine import simulate, load_vixtwn_delta  # noqa: E402
from tmf_channel.tick_index import available_days, build_tick_index  # noqa: E402
from order.tmf_channel_config import PAPER_RECIPE  # noqa: E402

BAR_SOURCE = "tx_1m_tick_built_582d"


def bars_db() -> Path:
    import stock_db
    return Path(stock_db.DATA_DIR).parent / "cache" / "tmf_channel" / "bars.sqlite"


def overlap_days() -> list[str]:
    con = sqlite3.connect(f"file:{bars_db()}?mode=ro", uri=True)
    try:
        bd = [r[0] for r in con.execute(
            "SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day", (BAR_SOURCE,))]
    finally:
        con.close()
    have = set(available_days())
    return [d for d in bd if d in have]


def scaled_recipe(mult: float) -> dict:
    r = deepcopy(PAPER_RECIPE)
    r.update({"hang_anchor": "O", "eod_flatten": True, "tick_native": True,
              "fill_model": "through"})
    for key in ("hang_lo", "hang_hi", "night_hang_lo", "night_hang_hi"):
        if r.get(key):
            r[key] = float(r[key]) * mult
    book = r.get("session_pv_book")
    if isinstance(book, dict):
        for sess in book.values():
            for cell in sess.values():
                for key in ("hang_lo", "hang_hi"):
                    if cell.get(key):
                        cell[key] = float(cell[key]) * mult
    return r


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    days = overlap_days()[-n:]
    vix = load_vixtwn_delta() or {}
    rec = scaled_recipe(2.0)
    why = Counter()
    why_pnl = Counter()
    for i, d in enumerate(days):
        rows = load_day(d, source=BAR_SOURCE)
        if not rows:
            continue
        O = [float(r["o"]) for r in rows]; H = [float(r["h"]) for r in rows]
        L = [float(r["l"]) for r in rows]; C = [float(r["c"]) for r in rows]
        V = [float(r.get("v") or 0) for r in rows]
        T = [f"{r['cal']}T{r['t']}:00+08:00" for r in rows]
        idx = build_tick_index(T)
        if idx is None:
            continue
        trades, *_ = simulate(O, H, L, C, V, T, rec, vix_delta=vix, tick_index=idx)
        for t in trades:
            w = str(t.get("why"))
            why[w] += 1
            why_pnl[w] += float(t["pnl"])
        _ti._load_raw.cache_clear()
        if i % 10 == 0:
            print(f"  {i}/{len(days)} {d}", flush=True)
    tot = sum(why.values())
    print(f"\n{len(days)} 天 · {tot} 筆")
    for w, c in why.most_common():
        print(f"  {w:<16} {c:>6}  {100*c/tot:5.1f}%   pnl/筆(含COST3) {why_pnl[w]/c:+7.2f}")
    print(json.dumps({"days": len(days), "n_trades": tot, "why": dict(why)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
