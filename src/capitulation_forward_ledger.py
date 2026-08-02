#!/usr/bin/env python3
"""投降抄底策略 — 前瞻 OOS 帳本。累積「樣本外事件」以突破單一 2026 regime 限制。

每次執行:
  1. 重算①訊號日(台指VIX>30 且 升 且 指數收黑),事件化(相鄰≤2交易日併)。
  2. 對三種候選選股規則各記一列:beta_top5(現有腳本)、rsmom_top10(最穩)、equal_leading。
  3. 用 point-in-time beta 算 beta 調整超額(6-8日分批 與 fwd20);前瞻未成熟填 NULL、mature=0。
  4. INSERT OR REPLACE → 每天重跑自動把「昨天還沒成熟、今天成熟」的報酬補上。
  5. 印「累積事件層記分板」(只計成熟事件),三規則並列,附 leave-one-event-out。

設計原則同 signal_validation.py(事件層/相對/point-in-time)。此為量測帳本,非交易規則。
排程用:每日跑 `python3 capitulation_forward_ledger.py --db <canonical stocks.db>`。
"""
import sqlite3, sys, os, json, argparse, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from signal_validation import (trading_calendar, index_returns, to_events,
                               fwd_pct, pit_beta, leave_one_event_out, summarize)

DEFAULT_DB = "/Users/jackm4/goldenstocks-data/data/stocks.db"
RULES = ("beta_top5", "rsmom_top10", "equal_leading")


def signal_days(cur, cal, cpos, close, since="2015-01-01"):
    vix = dict(cur.execute("SELECT date, close FROM market_vix_daily "
                           "WHERE symbol='VIXTWN' AND source='computed'"))
    return [cal[i] for i in range(1, len(cal))
            if cal[i] in vix and cal[i - 1] in vix and cal[i] >= since
            and vix[cal[i]] > 30 and vix[cal[i]] > vix[cal[i - 1]] and close[cal[i]] < close[cal[i - 1]]]


def leading(cur, day):
    sess = cur.execute("SELECT MAX(session_date) FROM rrg_universe_scores "
                       "WHERE screen_kind='close' AND session_date<=?", (day,)).fetchone()[0]
    if not sess:
        return []
    return [dict(stock_id=s, rs_momentum=rm, beta=b) for s, rm, b in cur.execute(
        """SELECT r.stock_id, r.rs_momentum, b.beta FROM rrg_universe_scores r
           LEFT JOIN stock_beta b ON b.stock_id=r.stock_id AND b.benchmark='^TWII'
           WHERE r.session_date=? AND r.screen_kind='close' AND r.quadrant LIKE '%leading%'""", (sess,))]


def pick(rule, uni):
    if rule == "equal_leading":
        return [u["stock_id"] for u in uni]
    key = "beta" if rule == "beta_top5" else "rs_momentum"
    n = 5 if rule == "beta_top5" else 10
    order = sorted(uni, key=lambda u: (u.get(key) if u.get(key) is not None else -1e9), reverse=True)
    return [u["stock_id"] for u in order[:n]]


def ensure_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS signal_capitulation_forward(
        signal_date TEXT, event_id INTEGER, trigger_type TEXT, rule TEXT,
        n_picks INTEGER, pick_ids TEXT,
        fwd68_excess REAL, fwd20_excess REAL, mature68 INTEGER, mature20 INTEGER,
        computed_at TEXT, PRIMARY KEY(signal_date, rule))""")


def stock_px(cur, sid, _cache={}):
    if (cur, sid) not in _cache:
        _cache[(cur, sid)] = dict(cur.execute(
            "SELECT trade_date, COALESCE(adj_close, close) FROM stock_daily_bars "
            "WHERE stock_id=? AND COALESCE(adj_close, close) > 0", (sid,)))
    return _cache[(cur, sid)]


def beta_adj_excess(cur, sid, day, hs, cal, cpos, close, iret):
    b = pit_beta(stock_px(cur, sid), iret, cal, cpos, day)
    if b is None:
        return None
    px = stock_px(cur, sid)
    srs, irs = [], []
    for h in hs:
        j = cpos.get(day, -1) + h
        if day not in cpos or j >= len(cal):
            return None
        s0, s1 = px.get(day), px.get(cal[j])
        i = fwd_pct(close, cpos, cal, day, h)
        if not (s0 and s1) or i is None:
            return None
        srs.append((s1 / s0 - 1) * 100); irs.append(i)
    return sum(srs) / len(srs) - b * (sum(irs) / len(irs))


def populate(cur):
    cal, cpos, close = trading_calendar(cur)
    iret = index_returns(cal, close)
    sig = signal_days(cur, cal, cpos, close)
    events = to_events(sig, cpos)
    ev_of = {d: i for i, e in enumerate(events) for d in e}
    now = dt.datetime.now().isoformat(timespec="seconds")
    ensure_table(cur)
    for day in sig:
        uni = leading(cur, day)
        if not uni:
            continue
        for rule in RULES:
            ids = pick(rule, uni)
            ex68 = [beta_adj_excess(cur, s, day, (6, 7, 8), cal, cpos, close, iret) for s in ids]
            ex20 = [beta_adj_excess(cur, s, day, (20,), cal, cpos, close, iret) for s in ids]
            v68 = [x for x in ex68 if x is not None]
            v20 = [x for x in ex20 if x is not None]
            f68 = sum(v68) / len(v68) if v68 else None
            f20 = sum(v20) / len(v20) if v20 else None
            cur.execute("INSERT OR REPLACE INTO signal_capitulation_forward VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (day, ev_of[day], None, rule, len(ids), json.dumps(ids),
                         f68, f20, int(f68 is not None), int(f20 is not None), now))
    return len(sig), len(events)


def scoreboard(cur):
    print(f"\n{'='*70}\n 累積前瞻記分板 (事件層,只計成熟)\n{'='*70}")
    for rule in RULES:
        rows = cur.execute("SELECT event_id, fwd68_excess, fwd20_excess FROM "
                           "signal_capitulation_forward WHERE rule=?", (rule,)).fetchall()
        for h, col in (("6-8", 1), ("fwd20", 2)):
            byev = {}
            for r in rows:
                if r[col] is not None:
                    byev.setdefault(r[0], []).append(r[col])
            evvals = [sum(v) / len(v) for v in byev.values()]
            s = summarize(evvals)
            if s["n"]:
                base, _, after = leave_one_event_out(evvals)
                print(f"  {rule:<14} {h:>5}: 事件層 {s['mean']:+.2f}%/勝{s['win']:.0f}% "
                      f"(事件n={s['n']})  LOO→{after:+.2f}%")
        print()
    imm = cur.execute("SELECT COUNT(*) FROM signal_capitulation_forward WHERE mature20=0").fetchone()[0]
    print(f"  未成熟(fwd20前瞻未足,待未來補) 列數: {imm}  ← 這些是真正的樣本外,隨時間成熟")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args()
    con = sqlite3.connect(a.db); cur = con.cursor()
    ns, ne = populate(cur)
    con.commit()
    print(f"已更新前瞻帳本:{ns} 訊號日 / {ne} 事件 × {len(RULES)} 規則")
    scoreboard(cur)
    con.close()


if __name__ == "__main__":
    main()
