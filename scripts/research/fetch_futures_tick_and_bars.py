#!/usr/bin/env python3
"""抓任一 TAIFEX 期貨的逐筆並建成 1 分 K —— 為「廣度」而做。

動機（Grinold 1989 主動管理基本定律）：IR = IC × √N。過去九次介入全部在提高
IC（單一策略的每注技術），只成功一次；N（獨立注數）這個**乘性**槓桿一次都沒
碰過。用本專案自己的數字：每筆 SR 0.022 × √(29筆/日 × 250日) = 年化 1.87；
要靠 IC 翻倍很難，但三個彼此不相關、各自 1.87 的策略合起來就是 3.24。

前提是這套管線能套到別的商品，而在此之前它寫死在 TX 上。這支腳本補上資料端：
FinMind ``TaiwanFuturesTick``（與 TX 完全相同的 dataset 與欄位），落成
``cache/tmf_channel/finmind_{product}_tick_by_day/{day}.json``，再建 1 分 K 寫進
``bars.sqlite``，source 命名 ``{product}_1m_tick_built``。

刻意與既有 TX 資料完全同構（同欄位、同時間範圍 08:45–23:59、同 sess 判定），
這樣 tick_index / causal_engine / 三段 WF 都不必為新商品改任何一行。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finmind_client import fetch_finmind_json  # noqa: E402

SLEEP = 0.35
TX_SOURCE = "tx_1m_tick_built_582d"


def data_root() -> Path:
    try:
        import stock_db
        return Path(stock_db.DATA_DIR).parent
    except Exception:  # noqa: BLE001
        return Path.home() / "goldenstocks-data"


def bars_db() -> Path:
    return data_root() / "cache" / "tmf_channel" / "bars.sqlite"


def tick_dir(product: str) -> Path:
    d = data_root() / "cache" / "tmf_channel" / f"finmind_{product.lower()}_tick_by_day"
    d.mkdir(parents=True, exist_ok=True)
    return d


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


def trading_days(n: int) -> list[str]:
    """用 TX 已有的日曆當交易日清單——避免自己判斷休市。"""
    con = sqlite3.connect(f"file:{bars_db()}?mode=ro", uri=True)
    try:
        days = [r[0] for r in con.execute(
            "SELECT DISTINCT day FROM bars WHERE source=? ORDER BY day", (TX_SOURCE,))]
    finally:
        con.close()
    return days[-n:]


def front_month(rows: list[dict]) -> str | None:
    """成交量最大的**單一到期月**（排除日曆價差，其 price 是價差本身）。"""
    vol: dict[str, float] = defaultdict(float)
    for r in rows:
        cd = str(r.get("contract_date") or "")
        if not cd or "/" in cd:
            continue
        try:
            vol[cd] += float(r.get("volume") or 0)
        except (TypeError, ValueError):
            continue
    return max(vol, key=lambda k: vol[k]) if vol else None


def build_bars(rows: list[dict], product: str) -> list[tuple]:
    """逐筆 → 1 分 K。只留 08:45–23:59（與既有 TX source 同構）。"""
    fm = front_month(rows)
    if not fm:
        return []
    buckets: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for r in rows:
        if str(r.get("contract_date") or "") != fm:
            continue
        ts = str(r.get("date") or "")
        if len(ts) < 16:
            continue
        cal, hm = ts[:10], ts[11:16]
        if not ("08:45" <= hm <= "23:59"):
            continue
        try:
            buckets[(cal, hm)].append((float(r["price"]), float(r.get("volume") or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    out = []
    for (cal, hm), pv in sorted(buckets.items()):
        px = [p for p, _ in pv]
        sess = "day" if "08:45" <= hm <= "13:45" else "night"
        out.append((f"{product.lower()}_1m_tick_built", cal, hm,
                    px[0], max(px), min(px), px[-1], sum(v for _, v in pv), sess))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--product", required=True, help="FinMind futures_id，如 TE / TF / MTX")
    ap.add_argument("--days", type=int, default=180)
    ap.add_argument("--refetch", action="store_true", help="已存在的日期也重抓")
    args = ap.parse_args()
    prod = args.product.upper()

    days = trading_days(args.days)
    tdir = tick_dir(prod)
    log(f"{prod}: 目標 {len(days)} 個交易日  {days[0]} → {days[-1]}")

    con = sqlite3.connect(bars_db())
    con.execute("CREATE TABLE IF NOT EXISTS bars (source TEXT, day TEXT, t TEXT, "
                "o REAL, h REAL, l REAL, c REAL, v REAL, sess TEXT)")
    n_fetch = n_skip = n_empty = 0
    total_bars = 0
    for i, day in enumerate(days, 1):
        path = tdir / f"{day}.json"
        if path.exists() and not args.refetch:
            rows = json.loads(path.read_text(encoding="utf-8"))
            n_skip += 1
        else:
            try:
                payload = fetch_finmind_json(
                    {"dataset": "TaiwanFuturesTick", "data_id": prod, "start_date": day},
                    timeout=60)
                rows = payload.get("data") or []
            except Exception as exc:  # noqa: BLE001
                log(f"  {day} 抓取失敗: {str(exc)[:80]}")
                continue
            path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            n_fetch += 1
            time.sleep(SLEEP)
        if not rows:
            n_empty += 1
            continue
        bars = build_bars(rows, prod)
        if bars:
            con.execute("DELETE FROM bars WHERE source=? AND day IN ({})".format(
                ",".join("?" * len({b[1] for b in bars}))),
                [f"{prod.lower()}_1m_tick_built", *sorted({b[1] for b in bars})])
            con.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?,?)", bars)
            con.commit()
            total_bars += len(bars)
        if i % 20 == 0:
            log(f"  {i}/{len(days)}  抓{n_fetch} 跳過{n_skip} 空{n_empty}  累計 {total_bars} 根 K")
    con.close()
    log(f"{prod} 完成：抓 {n_fetch} 日 · 跳過 {n_skip} · 無資料 {n_empty} · 共 {total_bars} 根 1 分 K")
    log(f"  source = {prod.lower()}_1m_tick_built   逐筆 = {tdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
