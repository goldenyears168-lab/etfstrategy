"""部落客/專欄「預測記分板」— append-only, 前視安全, 對本地價格DB自動打分。

用結構化、事後打分的軌跡紀錄，取代訂閱數/社群聲量，回答「哪位作者真有 edge」。
設計見 docs/wantgoo-intelligence-audit.md §10。核心護欄：

  1. append-only：一列預測寫入後**永不編輯/刪除**（只允許 status/outcome 等打分欄事後填）
     → 每個失敗 call 都留在紀錄上（打敗倖存者偏誤）。
  2. entry_ref_price 在**抽取當下凍結**（made_date 收盤）→ 殺前視、殺事後改價。
  3. benchmark 相對 alpha（個股 = 報酬 − 同期 TAIEX）→ 區分 skill vs 「只是喊對多頭」。
  4. specificity = 可計分列 / 見文數 → 懲罰只寫格言的作者。
  5. 排名需 min n 且過顯著性，非只看 hit_rate。

價格來源（本地、唯讀）：
  · TAIEX / benchmark → data/research/chip_macro/panel.parquet (ix_*)
  · 個股 → data/stocks.db  stock_daily_bars(stock_id, trade_date, close, high, low)
交易日曆以 panel 日期為準（同一市場，個股共用）。

CLI:
  init                    建表
  seed                    寫入 2026-07-31 活範例的真實前瞻 call（可重跑, 去重）
  score [--asof YYYY-MM-DD]  對 check_date 已到的 open 列打分
  report                  印出 author_scorecard
  selftest                用真 TAIEX 資料驗算打分數學
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
DB = ROOT / "data" / "wantgoo_loop.db"
PANEL = ROOT / "data" / "research" / "chip_macro" / "panel.parquet"
STOCKS_DB = ROOT / "data" / "stocks.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS author_predictions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  member_id     TEXT NOT NULL,
  author_name   TEXT,
  post_id       TEXT,
  post_url      TEXT,
  made_date     TEXT NOT NULL,          -- T0, publish date
  asset_type    TEXT NOT NULL,          -- 'stock' | 'index'
  symbol        TEXT NOT NULL,          -- '2303' | 'TAIEX'
  direction     TEXT NOT NULL,          -- 'up' | 'down' | 'range'
  horizon_days  INTEGER NOT NULL,       -- trading days
  check_date    TEXT,                   -- made_date + horizon (trading calendar)
  condition     TEXT,
  target_level  REAL,
  claim_text    TEXT,
  confidence    TEXT,                   -- high|med|low
  entry_ref_price REAL,                 -- close(made_date), FROZEN at insert
  extracted_by  TEXT,
  extracted_at  TEXT,
  status        TEXT DEFAULT 'open',    -- open|scored|void
  outcome       TEXT,                   -- hit|miss|partial|manual|void
  realized_return  REAL,
  benchmark_return REAL,
  alpha         REAL,
  touched_target INTEGER,
  scored_at     TEXT,
  -- append-only guarantee: no natural dup of the same claim
  UNIQUE(member_id, post_id, symbol, direction, horizon_days)
);
CREATE INDEX IF NOT EXISTS idx_ap_status ON author_predictions(status, check_date);
"""


# ---------- price access (read-only) ----------
def _taiex() -> pd.Series:
    """TAIEX close by date. parquet 優先, fall back to CSV (mini 無 pyarrow, 比照 daily_tracker)。"""
    try:
        p = pd.read_parquet(PANEL)
    except Exception:
        p = pd.read_csv(PANEL.with_suffix(".csv"))
    p = p.assign(_d=pd.to_datetime(p["date"]).astype(str).str[:10]).dropna(subset=["ix_close"])
    return p.drop_duplicates("_d", keep="last").set_index("_d")["ix_close"].astype(float).sort_index()


def _trading_days(tx: pd.Series) -> list[str]:
    return list(tx.index)


def _stock_bars(stock_id: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{STOCKS_DB}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            "SELECT trade_date, close, high, low FROM stock_daily_bars "
            "WHERE stock_id=? ORDER BY trade_date", con, params=(stock_id,))
    finally:
        con.close()
    df["_d"] = df["trade_date"].astype(str).str[:10]
    return df.set_index("_d")


def _on_or_before(days: list[str], d: str) -> str | None:
    """last trading day <= d."""
    lo, hi, ans = 0, len(days) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if days[mid] <= d:
            ans = days[mid]; lo = mid + 1
        else:
            hi = mid - 1
    return ans


def _check_date(days: list[str], made: str, horizon: int) -> str | None:
    """trading day `horizon` steps after `made` (made counted at its on/before position)."""
    base = _on_or_before(days, made)
    if base is None:
        return None
    j = days.index(base) + horizon
    return days[j] if j < len(days) else None


# ---------- core ----------
def init_db() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA); con.commit(); con.close()
    print(f"OK  init -> {DB}")


def add_prediction(*, member_id, author_name, made_date, asset_type, symbol, direction,
                   horizon_days, condition=None, target_level=None, claim_text=None,
                   confidence=None, post_id=None, post_url=None, extracted_by="manual") -> str:
    """Insert ONE prediction. entry_ref_price frozen from made_date close. Append-only:
    a duplicate (member,post,symbol,direction,horizon) is ignored, never overwritten."""
    tx = _taiex(); days = _trading_days(tx)
    check = _check_date(days, made_date, horizon_days)
    # freeze entry ref
    if asset_type == "index":
        b = _on_or_before(days, made_date)
        entry = float(tx[b]) if b else None
    else:
        bars = _stock_bars(symbol)
        b = _on_or_before(list(bars.index), made_date)
        entry = float(bars.loc[b, "close"]) if b else None
    con = sqlite3.connect(DB)
    try:
        con.execute(
            "INSERT OR IGNORE INTO author_predictions "
            "(member_id,author_name,post_id,post_url,made_date,asset_type,symbol,direction,"
            "horizon_days,check_date,condition,target_level,claim_text,confidence,"
            "entry_ref_price,extracted_by,extracted_at,status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')",
            (str(member_id), author_name, post_id, post_url, made_date, asset_type, symbol,
             direction, int(horizon_days), check, condition, target_level, claim_text,
             confidence, entry, extracted_by, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        con.commit()
        changed = con.total_changes
    finally:
        con.close()
    return f"{'inserted' if changed else 'dup-ignored'}: {author_name} {symbol} {direction} h{horizon_days} check={check} entry={entry}"


def score_open(asof: str | None = None) -> int:
    """Score every open row whose check_date has arrived AND whose prices exist.
    Only fills scoring columns — never touches the immutable claim fields."""
    asof = asof or date.today().isoformat()
    tx = _taiex(); days = _trading_days(tx)
    con = sqlite3.connect(DB)
    rows = con.execute(
        "SELECT id,asset_type,symbol,direction,made_date,check_date,target_level,entry_ref_price,horizon_days "
        "FROM author_predictions WHERE status='open'").fetchall()
    n = 0
    for rid, atype, sym, direction, made, check, target, entry, horizon in rows:
        if not check:  # was un-computable at insert (made_date beyond data) — retry now
            check = _check_date(days, made, horizon)
            if check:
                con.execute("UPDATE author_predictions SET check_date=? WHERE id=?", (check, rid))
        if not check or check > asof:
            continue
        # benchmark = TAIEX over [made, check]
        bm0 = _on_or_before(days, made); bm1 = _on_or_before(days, check)
        if not bm0 or not bm1:
            continue
        bench = tx[bm1] / tx[bm0] - 1.0
        if atype == "index":
            realized = bench
            hi = tx[bm0:bm1].max(); lo = tx[bm0:bm1].min()
        else:
            bars = _stock_bars(sym)
            s0 = _on_or_before(list(bars.index), made); s1 = _on_or_before(list(bars.index), check)
            if not s0 or not s1 or s1 < check:  # need actual close at/after check_date
                continue
            realized = float(bars.loc[s1, "close"]) / float(bars.loc[s0, "close"]) - 1.0
            win = bars.loc[s0:s1]
            hi = float(win["high"].max()); lo = float(win["low"].min())
        alpha = realized - bench if atype == "stock" else realized  # index: benchmark==self
        touched = None
        if target is not None:
            if direction == "up":
                touched = bool(hi >= target)
            elif direction == "down":
                touched = bool(lo <= target)
        # outcome
        if direction == "range":
            outcome = "manual"                       # band needs manual/2-target parse
        elif target is not None:
            outcome = "hit" if touched else "miss"
        else:
            metric = alpha if atype == "stock" else realized
            outcome = "hit" if ((direction == "up" and metric > 0) or
                                (direction == "down" and metric < 0)) else "miss"
        con.execute(
            "UPDATE author_predictions SET status='scored', outcome=?, realized_return=?, "
            "benchmark_return=?, alpha=?, touched_target=?, scored_at=? WHERE id=?",
            (outcome, round(realized, 5), round(bench, 5), round(alpha, 5),
             None if touched is None else int(touched), datetime.now(timezone.utc).isoformat(timespec="seconds"), rid))
        n += 1
    con.commit(); con.close()
    return n


def scorecard() -> pd.DataFrame:
    con = sqlite3.connect(DB)
    df = pd.read_sql_query("SELECT * FROM author_predictions", con); con.close()
    if df.empty:
        return df
    seen = df.groupby(["author_name"]).size().rename("posts_rows")
    sc = df[df["status"] == "scored"]
    if sc.empty:
        agg = pd.DataFrame(columns=["author_name", "asset_type", "horizon_days",
                                    "n", "hit_rate", "mean_alpha", "median_alpha"])
    else:
        g = sc.groupby(["author_name", "asset_type", "horizon_days"])
        agg = g.agg(n=("id", "size"),
                    hit_rate=("outcome", lambda s: (s == "hit").mean()),
                    mean_alpha=("alpha", "mean"),
                    median_alpha=("alpha", "median")).reset_index()
    # specificity = scorable(non-range) rows / total rows per author
    spec = (df.assign(scorable=(df["direction"] != "range").astype(int))
              .groupby("author_name")["scorable"].mean().rename("specificity"))
    return agg, spec


SEED_20260731 = [  # 真實前瞻 call, 抽自 2026-07-31 活範例 (reports/.../wantgoo-daily-loop)
    dict(member_id=23864, author_name="KuraHoshi", post_id="476", made_date="2026-07-30",
         asset_type="index", symbol="TAIEX", direction="down", horizon_days=20, target_level=40000,
         condition="八月底前回測40000以下", confidence="med",
         claim_text="台股可能在八月底前就回測40000以下", extracted_by="workflow-20260731"),
    dict(member_id=203663, author_name="玩股摸金", post_id="1379", made_date="2026-07-30",
         asset_type="index", symbol="TAIEX", direction="up", horizon_days=5, target_level=None,
         condition="守住42376大頸線支撐則多方反彈; 收盤破42376且次日未收復則空頭延續", confidence="med",
         claim_text="主要趨勢線支撐42376，守住季線上緣", extracted_by="workflow-20260731"),
]


def seed() -> None:
    for row in SEED_20260731:
        print(" ", add_prediction(**row))


def selftest() -> None:
    """End-to-end 驗算：對真 TAIEX 已收窗建 index down-call, 跑真正的 score_open, 對照手算。"""
    global DB
    import tempfile, os
    tx = _taiex(); days = _trading_days(tx)
    made = "2026-07-22"; h = 5
    check = _check_date(days, made, h)
    realized = tx[check] / tx[made] - 1.0
    exp = "hit" if realized < 0 else "miss"   # down-call, no target → hit iff realized<0
    print(f"selftest: TAIEX {made}->{check} realized={realized:+.4%} → down-call expect '{exp}'")
    orig = DB
    DB = Path(tempfile.mktemp(suffix=".db"))
    try:
        init_db()
        add_prediction(member_id="T", author_name="__selftest__", made_date=made, asset_type="index",
                       symbol="TAIEX", direction="down", horizon_days=h, extracted_by="selftest")
        n = score_open(asof="2026-07-31")
        con = sqlite3.connect(DB)
        got = con.execute("SELECT outcome,realized_return,alpha,status FROM author_predictions").fetchone()
        con.close()
        assert n == 1 and got[0] == exp, f"scorer mismatch: {got} vs {exp}"
        assert abs(got[1] - realized) < 1e-4, f"realized mismatch {got[1]} vs {realized}"
        print(f"selftest PASS ✓  score_open→ outcome={got[0]} realized={got[1]:+.4%} status={got[3]}")
    finally:
        if DB.exists():
            os.unlink(DB)
        DB = orig


def main():
    ap = argparse.ArgumentParser(description="author prediction scoreboard")
    ap.add_argument("cmd", choices=["init", "seed", "score", "report", "selftest"])
    ap.add_argument("--asof", default=None)
    a = ap.parse_args()
    if a.cmd == "init":
        init_db()
    elif a.cmd == "seed":
        init_db(); seed()
    elif a.cmd == "score":
        n = score_open(a.asof); print(f"scored {n} row(s) as of {a.asof or date.today().isoformat()}")
    elif a.cmd == "report":
        res = scorecard()
        if isinstance(res, pd.DataFrame) and res.empty:
            print("(no predictions yet — run `seed` then `score`)"); return
        agg, spec = res
        print("=== author_scorecard (scored rows) ===")
        print(agg.to_string(index=False) if len(agg) else "(nothing scored yet — check_date not reached)")
        print("\n=== specificity (scorable / total rows) ===")
        print(spec.to_string())
    elif a.cmd == "selftest":
        selftest()


if __name__ == "__main__":
    main()
