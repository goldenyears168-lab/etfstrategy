#!/usr/bin/env python3
"""stock_daily_bars 每日覆蓋率斷言（read-only · fail-loud）.

為什麼要有這支：`stock_daily_bars` 是大量訊號腳本的價格來源，而它們幾乎都用
`... INNER JOIN stock_daily_bars ...` 算金額。缺價的標的會被**靜默丟掉、不報錯**
——訊號不會變少一筆「錯的」，而是整檔消失。2026-08-17 已證實至少吃掉一筆真實
訊號（7610 @ 2026-07-30，9217 五日買超 1.456 億／淨比 1.000，live watch 完全沒看到）。

同一天量到的退化：每日有價檔數在 2026-07-13 前穩定 540~541，2026-07-14 起變成
419~611 劇烈震盪，且至少 45 檔的最後一根 bar 停在 2026-07-13、但它們的分點 tape
仍每日更新。既有排程對這件事**沒有任何斷言**。

本腳本只做量測與斷言，不寫 DB、不改資料：
  1. 最近一個交易日的有價檔數 vs 前 N 個交易日中位數 → 低於 ratio 門檻即 FAIL
  2. 「活躍但沒價」cohort：分點 tape 近期有活動、但 stock_daily_bars 已 stale 的標的
  3. 關鍵分點（預設 9217）當日 tape 的價格覆蓋率 → 低於門檻即 WARN

退出碼：0=OK、1=FAIL（launchd launcher 會據此送失敗告警）。
注意 2026-08-14 起 Gmail SMTP 憑證被拒（535 BadCredentials），信件管道不可信，
因此本腳本的主要告警手段是**退出碼＋報告檔**，email 只是加分。

  PYTHONPATH=src .venv/bin/python scripts/ops/check_stock_daily_bars_coverage.py
  PYTHONPATH=src .venv/bin/python scripts/ops/check_stock_daily_bars_coverage.py --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from stock_db import DEFAULT_DB_PATH  # noqa: E402

SOURCE = "finmind"
DEFAULT_LOOKBACK = 20
DEFAULT_MIN_RATIO = 0.90
DEFAULT_BRANCH = "9217"
DEFAULT_BRANCH_MIN_COVERAGE = 0.35  # 2026-08 實測長期落在 0.44~0.61 缺失（即 0.39~0.56 覆蓋）
STALE_TRADING_DAYS = 5


def connect_ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def recent_trade_dates(conn: sqlite3.Connection, n: int) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT trade_date FROM stock_daily_bars "
        "WHERE source=? ORDER BY trade_date DESC LIMIT ?",
        (SOURCE, n),
    ).fetchall()
    return [r[0] for r in rows]


def priced_counts(conn: sqlite3.Connection, dates: list[str]) -> dict[str, int]:
    if not dates:
        return {}
    ph = ",".join("?" * len(dates))
    rows = conn.execute(
        f"SELECT trade_date, COUNT(DISTINCT stock_id) AS n FROM stock_daily_bars "
        f"WHERE source=? AND close>0 AND trade_date IN ({ph}) GROUP BY trade_date",
        (SOURCE, *dates),
    ).fetchall()
    return {r["trade_date"]: int(r["n"]) for r in rows}


def branch_price_coverage(conn: sqlite3.Connection, branch_id: str, asof: str) -> dict:
    """該分點當日 tape 觸及的標的中，有多少比例查得到當日價格。"""
    base = """
        FROM stock_broker_branch_daily b
        WHERE b.source=? AND b.securities_trader_id=? AND b.trade_date=?
          AND length(b.stock_id)=4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
    """
    tape = conn.execute(
        f"SELECT COUNT(DISTINCT b.stock_id) {base}", (SOURCE, branch_id, asof)
    ).fetchone()[0]
    priced = conn.execute(
        f"""SELECT COUNT(DISTINCT b.stock_id) {base}
              AND EXISTS (SELECT 1 FROM stock_daily_bars p
                          WHERE p.stock_id=b.stock_id AND p.trade_date=b.trade_date
                            AND p.source=? AND p.close>0)""",
        (SOURCE, branch_id, asof, SOURCE),
    ).fetchone()[0]
    return {
        "branch_id": branch_id,
        "tape_stocks": int(tape),
        "priced_stocks": int(priced),
        "coverage": (priced / tape) if tape else None,
    }


def stale_active_cohort(conn: sqlite3.Connection, dates: list[str]) -> list[dict]:
    """分點 tape 近期仍活躍、但 stock_daily_bars 已停更的標的（回傳完整清單，不截斷）。"""
    if len(dates) <= STALE_TRADING_DAYS:
        return []
    recent = dates[:STALE_TRADING_DAYS]
    cutoff = recent[-1]
    ph = ",".join("?" * len(recent))
    rows = conn.execute(
        f"""
        SELECT b.stock_id,
               COUNT(DISTINCT b.trade_date) AS tape_days,
               (SELECT MAX(p.trade_date) FROM stock_daily_bars p
                 WHERE p.stock_id=b.stock_id AND p.source=?) AS last_bar
        FROM stock_broker_branch_daily b
        WHERE b.source=? AND b.trade_date IN ({ph})
          AND length(b.stock_id)=4 AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND b.stock_id NOT GLOB '00*'
        GROUP BY b.stock_id
        HAVING last_bar IS NOT NULL AND last_bar < ?
        ORDER BY tape_days DESC, last_bar ASC, b.stock_id
        """,
        (SOURCE, SOURCE, *recent, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def evaluate(conn: sqlite3.Connection, args: argparse.Namespace) -> dict:
    dates = recent_trade_dates(conn, args.lookback + 1)
    if not dates:
        return {"status": "FAIL", "reason": "stock_daily_bars 查無任何交易日", "checks": []}

    counts = priced_counts(conn, dates)
    latest = dates[0]

    # 收盤 sync 每輪回填一個 ~7 日滾動窗，所以最新一天的檔數還會長大。
    # 斷言要打在「已經沉澱」的那天（預設 T-3），否則每天都會誤報。
    settle_i = min(args.settle_index, len(dates) - 1)
    settled = dates[settle_i]
    settled_n = counts.get(settled, 0)
    prior = sorted(counts[d] for d in dates[settle_i + 1 :] if d in counts)
    baseline = prior[len(prior) // 2] if prior else 0
    ratio = (settled_n / baseline) if baseline else None

    checks: list[dict] = []
    checks.append(
        {
            "id": "daily_priced_count",
            "status": "FAIL" if (ratio is not None and ratio < args.min_ratio) else "OK",
            "settled_date": settled,
            "settled_count": settled_n,
            "latest_date": latest,
            "latest_count": counts.get(latest, 0),
            "baseline_median": baseline,
            "ratio": round(ratio, 4) if ratio is not None else None,
            "min_ratio": args.min_ratio,
            "note": "baseline 是滾動中位數：若退化持續夠久，基準會自己下沉。絕對水位看 history。",
        }
    )

    cov = branch_price_coverage(conn, args.branch, latest)
    cov_ok = cov["coverage"] is None or cov["coverage"] >= args.branch_min_coverage
    checks.append(
        {
            "id": "branch_price_coverage",
            "status": "OK" if cov_ok else "WARN",
            "min_coverage": args.branch_min_coverage,
            **cov,
        }
    )

    cohort = stale_active_cohort(conn, dates)
    checks.append(
        {
            "id": "stale_but_active",
            "status": "WARN" if cohort else "OK",
            "n": len(cohort),
            "sample": cohort[:15],
        }
    )

    status = "FAIL" if any(c["status"] == "FAIL" for c in checks) else (
        "WARN" if any(c["status"] == "WARN" for c in checks) else "OK"
    )
    return {
        "status": status,
        "latest_date": latest,
        "history": [{"trade_date": d, "priced": counts.get(d, 0)} for d in dates],
        "checks": checks,
    }


def render(result: dict) -> str:
    lines = [f"stock_daily_bars 覆蓋率斷言 · {result['status']} · asof={result.get('latest_date')}", ""]
    for c in result["checks"]:
        if c["id"] == "daily_priced_count":
            lines.append(
                f"[{c['status']}] 已沉澱日 {c['settled_date']} 有價檔數 {c['settled_count']} / "
                f"其前各日中位數 {c['baseline_median']}（ratio={c['ratio']}，門檻 {c['min_ratio']}）"
                f"　※最新日 {c['latest_date']}={c['latest_count']}（仍會回填，不納入斷言）"
            )
        elif c["id"] == "branch_price_coverage":
            covtxt = f"{c['coverage']:.1%}" if c["coverage"] is not None else "n/a"
            lines.append(
                f"[{c['status']}] 分點 {c['branch_id']} 當日 tape {c['tape_stocks']} 檔，"
                f"其中有價 {c['priced_stocks']} 檔（覆蓋 {covtxt}，門檻 {c['min_coverage']:.0%}）"
            )
        elif c["id"] == "stale_but_active":
            lines.append(f"[{c['status']}] 近 {STALE_TRADING_DAYS} 日 tape 活躍但 bars 已停更：{c['n']} 檔")
            for s in c["sample"]:
                lines.append(f"    {s['stock_id']}  tape_days={s['tape_days']}  last_bar={s['last_bar']}")
    lines += ["", "最近有價檔數："]
    for h in result["history"][:10]:
        lines.append(f"    {h['trade_date']}  {h['priced']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(DEFAULT_DB_PATH))
    ap.add_argument("--lookback", type=int, default=DEFAULT_LOOKBACK)
    ap.add_argument("--min-ratio", type=float, default=DEFAULT_MIN_RATIO)
    ap.add_argument("--branch", default=DEFAULT_BRANCH)
    ap.add_argument("--branch-min-coverage", type=float, default=DEFAULT_BRANCH_MIN_COVERAGE)
    ap.add_argument("--settle-index", type=int, default=3, help="斷言打在第幾個交易日前（預設 T-3，避開回填窗）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", type=Path, default=None, help="寫出報告檔路徑（預設不寫）")
    args = ap.parse_args()

    conn = connect_ro(args.db)
    try:
        result = evaluate(conn, args)
    finally:
        conn.close()

    out = json.dumps(result, ensure_ascii=False, indent=2) if args.json else render(result)
    print(out)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(out + "\n", encoding="utf-8")

    return 1 if result["status"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
