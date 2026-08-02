#!/usr/bin/env python3
"""每日『台指VIX投降抄底』訊號 — 最多N版 (2026-08 研究收尾,①+②+③三層)。

擇時三層 (OR 疊加,任一觸發即進場;20日波段持倉):
  ① 日收盤層  台指VIX>30  且  VIX>前一日  且  加權指數收盤<前一日
  ② 盤中深度  盤中最低≤前收×0.985(-1.5%)  且  盤中VIX最高>前一日VIX  且  盤中VIX>30
  ③ 盤中速度  任一60分窗內指數≤-0.8%  且  同窗VIX升≥0.3  且  VIX>30
加碼 (投降日,權重加倍):  VIX創20日高 + 指數創20日低
選股:  當日 RRG leading ∩ 高beta 前5
出場 (非進場條件):  近20日VIX曾破30、今VIX<25 → 恐慌正常化,獲利了結

回測 (2026-03~07 崩盤窗,105日,真盤中VIX):三層UNION=60進場日、未來20日+7.12%、勝率81%。
分層(未來20日):①日規則33天+7.42%/83%、②深度+9天+9.45%/86%、③速度+22天+6.64%/82%。
⚠️限制:①有11年樣本(55訊號+7.62%/89%);②③盤中VIX僅2026-03起,只單一危機regime、無法11年驗證。
   ③速度層報酬全靠20日耐心——砍到7日僅+0.6%/勝43%(接刀),短持倉勿用③。詳見 memory zhezhe-sentiment-signal。

用法:
  python3 daily_capitulation_signal.py            # 最新一日,含盤中三層 (盤中資料 cache-or-FinMind)
  python3 daily_capitulation_signal.py 2026-07-28 # 指定日回看
  python3 daily_capitulation_signal.py --no-intraday  # 只跑①日層 (不抓盤中,最快)
"""
import sqlite3, sys, os, json, glob, datetime as dt

sys.path.insert(0, os.path.expanduser("~/goldenstocks/src"))

from stock_db import DATA_DIR, DEFAULT_DB_PATH  # noqa: E402  (state root SSOT, portable across machines)

DB = str(DEFAULT_DB_PATH)
CACHE_DIR = str(DATA_DIR / "_intra_cache")
SEED_GLOB = "/private/tmp/claude-501/-Users-jackm4-goldenstocks-data/*/scratchpad/intra_cache"
VIX_FLOOR = 30.0
EXIT_VIX = 25.0
DEPTH_PCT = -1.5           # ② 盤中深度門檻 (%)
VELO_WIN = 12             # ③ 速度窗 = 12 根 5分K = 60 分
VELO_PCT = -0.8           # ③ 速度跌幅門檻 (%)
VELO_VIX_UP = 0.3         # ③ 同窗 VIX 至少升 (點)
TOPN = 5
BETA_BENCH = "^TWII"
EXIT_TRANCHES = [6, 7, 8]   # 分批出場:進場後第 6/7/8 交易日收盤各出 1/3 (避開單日運氣)


# ── 日線序列 ────────────────────────────────────────────────
def _idx_series(cur):
    return cur.execute("SELECT date, MAX(close) FROM daily_bars WHERE code='IX0001' "
                       "GROUP BY date ORDER BY date").fetchall()

def _idx_low(cur):
    """IX0001 盤中最低 (finmind 優先, yahoo 補)。"""
    lo = {}
    for src in ("yahoo", "finmind"):
        for d, l in cur.execute("SELECT date, low FROM daily_bars WHERE code='IX0001' "
                                "AND source=? AND low IS NOT NULL", (src,)):
            lo[d] = l
    return lo

def _vix_series(cur):
    return dict(cur.execute("SELECT date, close FROM market_vix_daily "
                            "WHERE symbol='VIXTWN' AND source='computed'"))

def _vix_intraday_high(cur):
    """finmind 來源的盤中 VIX 最高 (2026-03+)。"""
    return dict(cur.execute("SELECT date, high FROM market_vix_daily "
                            "WHERE symbol='VIXTWN' AND source='finmind' AND high IS NOT NULL"))


# ── 盤中 5 分 K (cache → seed → FinMind) ─────────────────────
def _bin5(t):
    h, m = int(t[:2]), int(t[3:5])
    return f"{h:02d}:{(m // 5) * 5:02d}"

def load_intraday(date):
    """回傳當日 5分K [{'hm','vix','px'}] (09:00-13:30)。無資料回 []。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    stable = os.path.join(CACHE_DIR, f"{date}.json")
    if os.path.exists(stable):
        return json.load(open(stable))
    # seed:沿用先前 session 已抓的 cache
    for seed in glob.glob(os.path.join(SEED_GLOB, f"{date}.json")):
        data = json.load(open(seed))
        json.dump(data, open(stable, "w"))
        return data
    # live fetch
    try:
        from finmind_client import fetch_finmind_json
    except Exception:
        return []
    try:
        jv = fetch_finmind_json({"dataset": "TaiwanOptionVix", "start_date": date, "end_date": date})
        vb = {_bin5(r["time"]): r["vix"] for r in jv.get("data", [])}
        jf = fetch_finmind_json({"dataset": "TaiwanFuturesTick", "data_id": "TX",
                                 "start_date": date, "end_date": date})
        fut = jf.get("data", [])
        if not fut or not vb:
            json.dump([], open(stable, "w")); return []
        near = sorted(set(r["contract_date"] for r in fut))[0]
        pb = {}
        for r in fut:
            if r["contract_date"] != near or not r.get("price") or r["price"] <= 0:
                continue
            t = r.get("time") or (r["date"][11:] if len(r["date"]) > 11 else "")
            if t:
                pb[_bin5(t)] = r["price"]
        bins = sorted(set(vb) & set(pb))
        out = [{"hm": b, "vix": vb[b], "px": pb[b]} for b in bins if "09:00" <= b <= "13:30"]
        json.dump(out, open(stable, "w"))
        return out
    except Exception as e:
        print(f"  (盤中抓取失敗 {date}: {str(e)[:60]})", file=sys.stderr)
        return []


def depth_trigger(bars, prev_close, prev_vix, db_low=None, db_vix_high=None):
    """② 盤中深度。優先用 DB 的現貨低/finmind VIX高;缺則用盤中路徑。"""
    if not bars and db_low is None:
        return False, None, None
    lo = db_low if db_low is not None else min(b["px"] for b in bars)
    vh = db_vix_high if db_vix_high is not None else (max(b["vix"] for b in bars) if bars else None)
    if lo is None or vh is None:
        return False, lo, vh
    fired = lo <= prev_close * (1 + DEPTH_PCT / 100) and vh > prev_vix and vh > VIX_FLOOR
    return fired, round(lo, 2), round(vh, 2)


def velocity_trigger(bars):
    """③ 盤中速度:任一60分窗內 px≤-0.8% 且 VIX升≥0.3 且 VIX>30。回傳首次觸發時間。"""
    for i in range(VELO_WIN, len(bars)):
        p0, p1 = bars[i - VELO_WIN]["px"], bars[i]["px"]
        v0, v1 = bars[i - VELO_WIN]["vix"], bars[i]["vix"]
        if (p1 / p0 - 1) * 100 <= VELO_PCT and (v1 - v0) >= VELO_VIX_UP and v1 > VIX_FLOOR:
            return True, bars[i]["hm"], round((p1 / p0 - 1) * 100, 2), round(v1, 2)
    return False, None, None, None


# ── 主評估 ──────────────────────────────────────────────────
def evaluate(cur, asof=None, use_intraday=True):
    idx = _idx_series(cur)
    vix = _vix_series(cur)
    rows = [(d, c, vix[d]) for d, c in idx if d in vix]
    if len(rows) < 21:
        raise SystemExit("資料不足")
    if asof is None:
        i = len(rows) - 1
    else:
        m = [j for j, r in enumerate(rows) if r[0] == asof]
        if not m:
            raise SystemExit(f"{asof} 無指數+VIX 資料")
        i = m[0]
    if i < 20:
        raise SystemExit("前方歷史不足 20 日")

    date, close, v = rows[i]
    pdate, pclose, pv = rows[i - 1]
    win20 = rows[i - 19:i + 1]
    vix20_max = max(r[2] for r in win20)
    px20_min = min(r[1] for r in win20)

    # ① 日收盤層
    g_floor, g_rising, g_pxdown = v > VIX_FLOOR, v > pv, close < pclose
    fired_daily = g_floor and g_rising and g_pxdown

    # ②③ 盤中層
    fired_depth = fired_velo = False
    depth_lo = depth_vh = velo_hm = velo_drop = None
    if use_intraday:
        bars = load_intraday(date)
        db_low = _idx_low(cur).get(date)
        db_vh = _vix_intraday_high(cur).get(date)
        fired_depth, depth_lo, depth_vh = depth_trigger(bars, pclose, pv, db_low, db_vh)
        fired_velo, velo_hm, velo_drop, _ = velocity_trigger(bars)

    layers = []
    if fired_daily: layers.append("daily")
    if fired_depth: layers.append("depth")
    if fired_velo:  layers.append("velocity")
    triggered = bool(layers)
    capitulation = triggered and v >= vix20_max and close <= px20_min

    recent_peak = max(r[2] for r in rows[max(0, i - 20):i + 1])
    exit_sig = (recent_peak >= VIX_FLOOR) and (v < EXIT_VIX)

    return {
        "date": date, "close": close, "vix": round(v, 2),
        "prev_date": pdate, "prev_close": pclose, "prev_vix": round(pv, 2),
        "vix20_max": round(vix20_max, 2), "px20_min": px20_min,
        "g_floor": g_floor, "g_rising": g_rising, "g_pxdown": g_pxdown,
        "fired_daily": fired_daily, "fired_depth": fired_depth, "fired_velo": fired_velo,
        "trigger_type": ",".join(layers), "triggered": triggered,
        "capitulation": capitulation, "exit_signal": exit_sig,
        "recent_peak_vix": round(recent_peak, 2),
        "depth_low": depth_lo, "depth_vix_high": depth_vh,
        "velo_time": velo_hm, "velo_drop": velo_drop,
    }


def pick_stocks(cur, session_date):
    sess = cur.execute("SELECT MAX(session_date) FROM rrg_universe_scores "
                       "WHERE screen_kind='close' AND session_date<=?", (session_date,)).fetchone()[0]
    if not sess:
        return sess, []
    rows = cur.execute(
        """SELECT r.stock_id, r.stock_name, b.beta, r.rs_ratio, r.rs_momentum
           FROM rrg_universe_scores r
           JOIN stock_beta b ON b.stock_id=r.stock_id AND b.benchmark=?
           WHERE r.session_date=? AND r.screen_kind='close'
             AND r.quadrant LIKE '%leading%' AND b.beta IS NOT NULL
           ORDER BY b.beta DESC LIMIT ?""", (BETA_BENCH, sess, TOPN)).fetchall()
    return sess, rows


def ensure_tables(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS signal_capitulation_daily(
        date TEXT PRIMARY KEY, taiex_close REAL, tw_vix REAL, prev_close REAL, prev_vix REAL,
        gate_vix_floor INTEGER, gate_vix_rising INTEGER, gate_price_down INTEGER,
        fired_daily INTEGER, fired_depth INTEGER, fired_velocity INTEGER,
        trigger_type TEXT, triggered INTEGER, capitulation_day INTEGER, exit_signal INTEGER,
        depth_low REAL, depth_vix_high REAL, velocity_time TEXT,
        rrg_session TEXT, computed_at TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS signal_capitulation_picks(
        date TEXT, rank INTEGER, stock_id TEXT, stock_name TEXT,
        beta REAL, rs_ratio REAL, rs_momentum REAL, PRIMARY KEY(date, rank))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS signal_capitulation_exits(
        entry_date TEXT, hold_days INTEGER, exit_date TEXT, weight REAL,
        entry_close REAL, exit_close REAL, ret_pct REAL,
        PRIMARY KEY(entry_date, hold_days))""")


def _trading_dates(cur):
    return [r[0] for r in cur.execute(
        "SELECT DISTINCT date FROM daily_bars WHERE code='IX0001' ORDER BY date")]


def build_exit_ledger(cur):
    """為所有觸發日排 6/7/8 日分批出場,回填已知收盤的實現報酬。回傳今日到期清單需另查。"""
    cal = _trading_dates(cur)
    pos = {d: cal.index(d) for d in cal}
    idx = dict(cur.execute("SELECT date, MAX(close) FROM daily_bars WHERE code='IX0001' GROUP BY date"))
    trig = cur.execute("SELECT date, taiex_close FROM signal_capitulation_daily WHERE triggered=1").fetchall()
    w = round(1.0 / len(EXIT_TRANCHES), 4)
    cur.execute("DELETE FROM signal_capitulation_exits")
    for ed, ec in trig:
        if ed not in pos:
            continue
        i = pos[ed]
        for h in EXIT_TRANCHES:
            j = i + h
            xd = cal[j] if j < len(cal) else None
            xc = idx.get(xd) if xd else None
            ret = round(100 * (xc / ec - 1), 3) if (xc and ec) else None
            cur.execute("INSERT OR REPLACE INTO signal_capitulation_exits VALUES (?,?,?,?,?,?,?)",
                        (ed, h, xd, w, round(ec, 2) if ec else None, round(xc, 2) if xc else None, ret))


def exits_due(cur, run_date):
    """run_date 當天該出場的批次 (某個進場日的 6/7/8 日剛好落在今天)。"""
    return cur.execute(
        "SELECT entry_date, hold_days, weight, ret_pct FROM signal_capitulation_exits "
        "WHERE exit_date=? ORDER BY entry_date", (run_date,)).fetchall()


def persist(cur, ev, sess, picks, now):
    cur.execute("INSERT OR REPLACE INTO signal_capitulation_daily VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ev["date"], ev["close"], ev["vix"], ev["prev_close"], ev["prev_vix"],
                 int(ev["g_floor"]), int(ev["g_rising"]), int(ev["g_pxdown"]),
                 int(ev["fired_daily"]), int(ev["fired_depth"]), int(ev["fired_velo"]),
                 ev["trigger_type"], int(ev["triggered"]), int(ev["capitulation"]),
                 int(ev["exit_signal"]), ev["depth_low"], ev["depth_vix_high"], ev["velo_time"],
                 sess, now))
    cur.execute("DELETE FROM signal_capitulation_picks WHERE date=?", (ev["date"],))
    if ev["triggered"]:
        for rk, (sid, name, beta, rsr, rsm) in enumerate(picks, 1):
            cur.execute("INSERT INTO signal_capitulation_picks VALUES (?,?,?,?,?,?,?)",
                        (ev["date"], rk, sid, name, beta, rsr, rsm))


def report(ev, sess, picks, due):
    yn = lambda b: "✅" if b else "—"
    print(f"\n{'='*62}\n  台指VIX 投降抄底訊號 (三層)   {ev['date']}\n{'='*62}")
    print(f"  加權指數 {ev['close']:.0f} (前日 {ev['prev_close']:.0f})   "
          f"台指VIX {ev['vix']:.2f} (前日 {ev['prev_vix']:.2f})")
    print(f"\n  三層擇時:")
    print(f"    {yn(ev['fired_daily'])} ① 日收盤 (VIX>30升 & 指數收黑)")
    d = f"  盤中低{ev['depth_low']}/VIX高{ev['depth_vix_high']}" if ev['depth_low'] else ""
    print(f"    {yn(ev['fired_depth'])} ② 盤中深度 (≤-1.5% & VIX破前收>30){d}")
    vinfo = f"  {ev['velo_time']} 一小時{ev['velo_drop']}%" if ev['fired_velo'] else ""
    print(f"    {yn(ev['fired_velo'])} ③ 盤中速度 (60分≤-0.8% & VIX同步噴){vinfo}")
    if ev["triggered"]:
        tag = "🔴 投降日 — 權重加倍" if ev["capitulation"] else "🟢 進場訊號成立"
        print(f"\n  >>> {tag}   [觸發層: {ev['trigger_type']}] <<<")
        print(f"\n  選股 (RRG leading ∩ 高beta 前{TOPN},掃描日 {sess}):")
        for rk, (sid, name, beta, rsr, rsm) in enumerate(picks, 1):
            print(f"    {rk}. {sid} {name or ''}  β={beta:.2f}  RS比{rsr:.1f}/動能{rsm:.1f}")
        if not picks:
            print("    (當日無 leading∩beta 標的)")
    else:
        print(f"\n  >>> 今日不進場 (三層皆未觸發) <<<")
    if due:
        print(f"\n  📤 今日分批出場 (6-7-8日排程):")
        for ed, h, wt, ret in due:
            rs = f"{ret:+.2f}%" if ret is not None else "—"
            print(f"    賣出 {ed} 進場部位的 {wt*100:.0f}% (持{h}日)  該役報酬 {rs}")
    if ev["exit_signal"]:
        print(f"\n  ⚠️  VIX出場訊號:近期VIX曾破30 (尖峰{ev['recent_peak_vix']})、今<25 → 持倉了結")
    print(f"{'='*62}\n")


def main():
    argv = [a for a in sys.argv[1:] if a]
    use_intraday = "--no-intraday" not in argv
    asof = next((a for a in argv if not a.startswith("--")), None)
    now = dt.datetime.now().isoformat(timespec="seconds")
    con = sqlite3.connect(DB); cur = con.cursor()
    ensure_tables(cur)
    ev = evaluate(cur, asof, use_intraday)
    sess, picks = pick_stocks(cur, ev["date"]) if ev["triggered"] else (None, [])
    persist(cur, ev, sess, picks, now)
    build_exit_ledger(cur)
    due = exits_due(cur, ev["date"])
    con.commit()
    report(ev, sess, picks, due)
    con.close()


if __name__ == "__main__":
    main()
