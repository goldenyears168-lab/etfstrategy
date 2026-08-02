#!/usr/bin/env python3
"""可複用的訊號驗證 harness — 固化本專案的誠實檢定方法 (2026-08 血淚教訓)。

三大鐵律 (違反其一,績效必自欺):
  1. 事件層,不是日層。相鄰 ≤max_gap 交易日的訊號併成一個獨立事件,取代重複計數。
     (51 個訊號日常常只是 ~8 個獨立事件;日層 t=3~6、勝率 100% 幾乎都是叢集假象。)
  2. 相對同期 control,不是絕對報酬。擇時訊號一律報「對同期未觸發日的超額」;
     否則 V 彈 drift 會被當成 alpha (2026 連 11 年硬證的指數規則對同期 control 都是負超額)。
  3. Point-in-time,不是快照。beta / 任何因子都用訊號日「往回」算,禁用未來快照 (前視會無中生有 alpha)。
額外穩健三關:permutation 顯著性、leave-one-event-out 集中度、beta×{1.0,1.2,1.5} 凸性 band。
前瞻窗不足的訊號 (forward 未成熟) 一律回 None 並排除,不進勝率 (防倖存者偏誤)。

用法:
  # CLI 示範 (複現本專案兩個關鍵結論)
  python3 signal_validation.py timing                 # 指數擇時:事件層 vs control + permutation
  python3 signal_validation.py selection              # 恐慌日 leading 選股:pit-beta 事件層 vs placebo
  python3 signal_validation.py timing --db /path/to/stocks.db

  # 當 library
  from signal_validation import validate_timing, validate_selection, to_events, pit_beta
"""
import sqlite3, sys, math, argparse, random
import statistics as st

from stock_db import DEFAULT_DB_PATH  # state root SSOT, portable across machines

DEFAULT_DB = str(DEFAULT_DB_PATH)


# ── 共用原語 ────────────────────────────────────────────────
def trading_calendar(cur, index_code="IX0001"):
    """回傳 (cal 升冪, cpos 位置索引, close 收盤 dict)。同日多來源取 MAX。"""
    rows = cur.execute("SELECT date, MAX(close) FROM daily_bars WHERE code=? "
                       "GROUP BY date ORDER BY date", (index_code,)).fetchall()
    cal = [d for d, _ in rows]
    return cal, {d: i for i, d in enumerate(cal)}, {d: c for d, c in rows}


def index_returns(cal, close):
    return {cal[i]: close[cal[i]] / close[cal[i - 1]] - 1 for i in range(1, len(cal))}


def to_events(dates, cpos, max_gap=2):
    """相鄰 ≤max_gap 交易日的訊號併為一事件。回傳 list[list[date]] (每事件日期升冪)。"""
    ds = sorted(d for d in dates if d in cpos)
    events = []
    for d in ds:
        if events and cpos[d] - cpos[events[-1][-1]] <= max_gap:
            events[-1].append(d)
        else:
            events.append([d])
    return events


def fwd_pct(close, cpos, cal, date, h):
    """從 date 收盤持有 h 交易日的 % 報酬。前瞻不足回 None (未成熟)。"""
    j = cpos.get(date, -1) + h
    if date not in cpos or j >= len(cal):
        return None
    return (close[cal[j]] / close[date] - 1) * 100


def pit_beta(px, iret, cal, cpos, date, win=250, minobs=60):
    """point-in-time beta:date 往回 win 交易日,個股 vs 指數日報酬的 cov/var。不足回 None。"""
    j = cpos.get(date, -1)
    if j < 0:
        return None
    sp, ip = [], []
    for k in range(max(1, j - win + 1), j + 1):
        d0, d1 = cal[k - 1], cal[k]
        if d0 in px and d1 in px and d1 in iret:
            sp.append(px[d1] / px[d0] - 1)
            ip.append(iret[d1])
    if len(sp) < minobs:
        return None
    mi = sum(ip) / len(ip)
    var = sum((x - mi) ** 2 for x in ip)
    if var <= 0:
        return None
    ms = sum(sp) / len(sp)
    cov = sum((sp[t] - ms) * (ip[t] - mi) for t in range(len(sp)))
    return cov / var


def summarize(vals):
    xs = [v for v in vals if v is not None]
    if not xs:
        return dict(n=0, mean=None, win=None, std=None)
    return dict(n=len(xs), mean=sum(xs) / len(xs),
                win=100 * sum(x > 0 for x in xs) / len(xs),
                std=st.pstdev(xs) if len(xs) > 1 else 0.0)


def permutation_diff(sample, pool_other, nperm=5000, seed=42):
    """H0:sample 與 pool_other 同分布。回傳 P(隨機超額 ≥ 觀察超額)。"""
    a = [v for v in sample if v is not None]
    b = [v for v in pool_other if v is not None]
    if not a or not b:
        return None
    obs = sum(a) / len(a) - sum(b) / len(b)
    pool = a + b
    k = len(a)
    rnd = random.Random(seed)
    cnt = 0
    for _ in range(nperm):
        rnd.shuffle(pool)
        if (sum(pool[:k]) / k - sum(pool[k:]) / len(pool[k:])) >= obs:
            cnt += 1
    return cnt / nperm


def leave_one_event_out(event_vals):
    """回傳 (base_mean, 最傷事件idx, 剔除後mean)。event_vals=每事件一個代表值。"""
    xs = [v for v in event_vals if v is not None]
    if len(xs) < 2:
        return (xs[0] if xs else None, None, None)
    base = sum(xs) / len(xs)
    worst_i, worst_after = None, base
    for i in range(len(xs)):
        rest = xs[:i] + xs[i + 1:]
        m = sum(rest) / len(rest)
        if abs(m - base) > abs(worst_after - base):
            worst_after, worst_i = m, i
    return (base, worst_i, worst_after)


# ── 驗證 1:指數擇時訊號 ────────────────────────────────────
def validate_timing(cur, signal_dates, index_code="IX0001",
                    horizons=(7, 20), max_gap=2, nperm=5000):
    cal, cpos, close = trading_calendar(cur, index_code)
    sigset = set(d for d in signal_dates if d in cpos)
    events = to_events(sigset, cpos, max_gap)
    control = [d for d in cal if d not in sigset]

    print(f"\n{'='*66}\n 指數擇時驗證  訊號日 {len(sigset)} → 獨立事件 {len(events)} (gap≤{max_gap})"
          f"\n 同期 control = 未觸發 {len(control)} 天\n{'='*66}")
    out = {"n_signal": len(sigset), "n_events": len(events), "horizons": {}}
    for h in horizons:
        ev_first = [fwd_pct(close, cpos, cal, e[0], h) for e in events]
        ctrl = [fwd_pct(close, cpos, cal, d, h) for d in control]
        se, sc = summarize(ev_first), summarize(ctrl)
        p = permutation_diff(ev_first, ctrl, nperm)
        base, wi, after = leave_one_event_out(ev_first)
        exc = se["mean"] - sc["mean"] if se["mean"] is not None and sc["mean"] is not None else None
        print(f"\n  fwd{h}:  事件層 {se['mean']:+.2f}%/勝{se['win']:.0f}% (n={se['n']})"
              f"   control {sc['mean']:+.2f}%   超額 {exc:+.2f}pp   P={p:.3f}"
              f"   {'✅顯著' if p is not None and p < 0.05 else '✗不顯著'}")
        print(f"          leave-one-event-out: base {base:+.2f}% → 剔最傷事件 {after:+.2f}%")
        out["horizons"][h] = dict(event=se, control=sc, excess=exc, p=p, loo_after=after)
    return out


# ── 驗證 2:橫斷面選股訊號 ──────────────────────────────────
def validate_selection(cur, signal_dates, universe_fn, pick_key="rs_momentum",
                       topn=10, index_code="IX0001", horizons=("6-8", 20),
                       beta_bands=(1.0, 1.2, 1.5), max_gap=2, nplacebo=400, seed=1):
    """universe_fn(cur, day) -> list[dict(stock_id, <features…>)] 當日可選池 (如 leading)。
       pick = 依 pick_key 由高到低取 topn;None 表等權全池。placebo=同池隨機抽同檔數。
       超額用 point-in-time beta 調整,並附 beta×band 凸性穩健。"""
    cal, cpos, close = trading_calendar(cur, index_code)
    iret = index_returns(cal, close)
    px_cache = {}

    def load_px(sid):
        if sid not in px_cache:
            px_cache[sid] = dict(cur.execute(
                "SELECT trade_date, COALESCE(adj_close, close) FROM stock_daily_bars "
                "WHERE stock_id=? AND COALESCE(adj_close, close) > 0", (sid,)).fetchall())
        return px_cache[sid]

    def sfwd(sid, day, h):
        p = load_px(sid); j = cpos.get(day, -1) + h
        if day not in cpos or j >= len(cal):
            return None
        s0, s1 = p.get(day), p.get(cal[j])
        return (s1 / s0 - 1) * 100 if s0 and s1 else None

    def hlist(h):
        return (6, 7, 8) if h == "6-8" else (h,)

    def excess(sid, day, h, band):
        b = pit_beta(load_px(sid), iret, cal, cpos, day)
        if b is None:
            return None
        srs = [sfwd(sid, day, x) for x in hlist(h)]
        irs = [fwd_pct(close, cpos, cal, day, x) for x in hlist(h)]
        if any(v is None for v in srs + irs):
            return None
        return sum(srs) / len(srs) - band * b * (sum(irs) / len(irs))

    sigset = sorted(d for d in signal_dates if d in cpos)
    events = to_events(sigset, cpos, max_gap)
    ev_of = {d: i for i, e in enumerate(events) for d in e}

    print(f"\n{'='*66}\n 橫斷面選股驗證  訊號日 {len(sigset)} → 事件 {len(events)}"
          f"   pick={'等權全池' if topn is None else f'{pick_key} 前{topn}'}\n{'='*66}")
    out = {"n_events": len(events), "horizons": {}}
    for h in horizons:
        for band in beta_bands:
            # 每訊號日:picks 的平均 excess → 收斂到事件層
            day_pick, day_pool = {}, {}
            for day in sigset:
                uni = universe_fn(cur, day)
                if not uni:
                    continue
                ex = [(u["stock_id"], excess(u["stock_id"], day, h, band)) for u in uni]
                ex = [(s, e) for s, e in ex if e is not None]
                if not ex:
                    continue
                if topn is None:
                    chosen = [e for _, e in ex]
                else:
                    order = sorted(uni, key=lambda u: (u.get(pick_key) or -1e9), reverse=True)
                    keys = [u["stock_id"] for u in order[:topn]]
                    exd = dict(ex)
                    chosen = [exd[k] for k in keys if k in exd]
                if chosen:
                    day_pick[day] = sum(chosen) / len(chosen)
                    day_pool[day] = [e for _, e in ex]
            # 事件層
            ev_pick = []
            for e in events:
                vals = [day_pick[d] for d in e if d in day_pick]
                if vals:
                    ev_pick.append(sum(vals) / len(vals))
            sp = summarize(ev_pick)
            base, wi, after = leave_one_event_out(ev_pick)
            # placebo:同池隨機抽同檔數 (僅 band=1.0、topn 有值時算,省時)
            pct = None
            if topn is not None and abs(band - 1.0) < 1e-9 and ev_pick:
                rnd = random.Random(seed)
                worse = 0
                for _ in range(nplacebo):
                    ev_rand = []
                    for e in events:
                        vals = []
                        for d in e:
                            pool = day_pool.get(d)
                            if pool and len(pool) >= 1:
                                k = min(topn, len(pool))
                                vals.append(sum(rnd.sample(pool, k)) / k)
                        if vals:
                            ev_rand.append(sum(vals) / len(vals))
                    if ev_rand and sum(ev_rand) / len(ev_rand) >= (sp["mean"] or -1e9):
                        worse += 1
                pct = 100 * (1 - worse / nplacebo)  # picks 贏過隨機的百分位
            tag = f"  placebo百分位={pct:.0f}%" if pct is not None else ""
            conv = "  ⚠️×1.2下轉負=不可交易" if (band == 1.2 and sp["mean"] is not None and sp["mean"] < 0) else ""
            print(f"  fwd{h:>3} beta×{band}:  事件層 {sp['mean']:+.2f}%/勝{sp['win']:.0f}% "
                  f"(n={sp['n']})  LOO→{after:+.2f}%{tag}{conv}")
            out["horizons"].setdefault(str(h), {})[band] = dict(summary=sp, loo=after, placebo_pct=pct)
        print()
    return out


# ── CLI 示範 ────────────────────────────────────────────────
def _demo_timing(cur):
    cal, cpos, close = trading_calendar(cur)
    vix = dict(cur.execute("SELECT date, close FROM market_vix_daily "
                           "WHERE symbol='VIXTWN' AND source='computed'"))
    sig = [cal[i] for i in range(1, len(cal))
           if cal[i] in vix and cal[i - 1] in vix
           and vix[cal[i]] > 30 and vix[cal[i]] > vix[cal[i - 1]] and close[cal[i]] < close[cal[i - 1]]]
    print("示範訊號 = 台指VIX>30 且 升 且 指數收黑 (規則①)")
    validate_timing(cur, sig, horizons=(7, 20))


def _demo_selection(cur):
    cal, cpos, close = trading_calendar(cur)
    vix = dict(cur.execute("SELECT date, close FROM market_vix_daily "
                           "WHERE symbol='VIXTWN' AND source='computed'"))
    sig = [cal[i] for i in range(1, len(cal))
           if cal[i] in vix and cal[i - 1] in vix
           and vix[cal[i]] > 30 and vix[cal[i]] > vix[cal[i - 1]] and close[cal[i]] < close[cal[i - 1]]
           and cal[i] >= "2026-01-01"]

    def leading_universe(cur, day):
        sess = cur.execute("SELECT MAX(session_date) FROM rrg_universe_scores "
                           "WHERE screen_kind='close' AND session_date<=?", (day,)).fetchone()[0]
        if not sess:
            return []
        return [dict(stock_id=s, rs_ratio=rr, rs_momentum=rm) for s, rr, rm in cur.execute(
            "SELECT stock_id, rs_ratio, rs_momentum FROM rrg_universe_scores "
            "WHERE session_date=? AND screen_kind='close' AND quadrant LIKE '%leading%'", (sess,))]

    print("示範:恐慌日(規則①) leading 選股。對比 等權全池 / rs_momentum前10。")
    print("\n### 等權全體 leading (唯一存活候選) ###")
    validate_selection(cur, sig, leading_universe, topn=None, horizons=("6-8", 20))
    print("### rs_momentum 前10 (影子版) ###")
    validate_selection(cur, sig, leading_universe, pick_key="rs_momentum", topn=10, horizons=("6-8", 20))


def main():
    ap = argparse.ArgumentParser(description="訊號驗證 harness (事件層+control+pit-beta+placebo)")
    ap.add_argument("mode", choices=["timing", "selection"], help="示範模式")
    ap.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args()
    con = sqlite3.connect(a.db)
    cur = con.cursor()
    (_demo_timing if a.mode == "timing" else _demo_selection)(cur)
    con.close()


if __name__ == "__main__":
    main()
