#!/usr/bin/env python3
"""每週五晚間·隔日沖席位名單刷新 + 個別席位漂移監控（research only · 唯讀）.

不改動 FROZEN_SPEC_V1.json（v1 凍結，任何門檻變更需開 v2）。
本腳本只做：
  1. 用最新資料重算 24 席全期 flip（交叉驗證 v1 凍結表未失真）
  2. 算每席近3個月/近1個月 flip，比對全期值，標記結構性漂移
  3. 產出當週五收盤後的候選標的清單（供週一 08:45 驗證用）
  4. 寫一份 markdown 週報，只做記錄與監控，不自動修改任何規格或執行任何下單
  5. 2026-08-13 新增：寫 pit_seat_flip_latest.json——build_candidates() 的
     HIGH_FLIP_MIN 門檻檢查改讀這份檔案，不再讀 FROZEN_SPEC_V1.json 的
     seat_flip_table_frozen（後者是全樣本中位數，spec 自己在 caveat 欄位承認
     「含輕微look-ahead...以PIT版為上線基準」，但live code一直讀的是前者，
     不是spec自己指定的PIT版本——見2026-08-13健檢發現）。這裡算的PIT值用的
     seat_events[tid]本身就已經是「只含T0+1資料已經發生的完整事件」（第77行
     `i+1>=len(ds)`過濾保證），對median()取值不需要另外切time cutoff。

  PYTHONPATH=src .venv/bin/python scripts/research/run_dayflip_weekly_seat_refresh.py
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path
from statistics import mean, median

import stock_db

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "reports/research/branch-footprint-screen"
OUT = BASE / "dayflip_gapup_short"
WEEKLY_OUT = OUT / "weekly_reports"

NAME = {"920M": "凱基-宜蘭", "7008": "兆豐-三重", "989g": "元大-嘉義", "981j": "元大-士林",
        "5851": "統一-高雄", "9217": "凱基松山", "980h": "元大-台北", "918e": "群益-大安",
        "989X": "元大-民生三民", "779Z": "國票安和", "913R": "群益-北高雄", "9661": "富邦新店",
        "585Y": "統一土城", "9875": "元大-土城永寧", "918X": "群益-台北", "5383": "第一金-高雄",
        "1360": "港麥格理", "9A9R": "永豐金信義", "9325": "華南-忠孝", "9A81": "永豐金-匯立",
        "779n": "國票南京", "9227": "凱基城中", "9216": "凱基-信義", "920F": "凱基-站前"}
DRIFT_THRESHOLD = 0.10
WEAK_SEATS = {"7008", "913R"}  # 已知flip分數幾乎是台積電代理，僅供標記參考


def log(m: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}", flush=True)


def main() -> None:
    spec = json.loads((OUT / "FROZEN_SPEC_V1.json").read_text())
    STATIC_FLIP = dict(spec["seat_flip_table_frozen"]["values"])
    MANUAL = {tuple(x) for x in spec["signal"]["step2_seat_filters"]["manual_pair_exclusion"]}
    mega = set(json.loads((BASE / "ab58_xMega_copytrade/mega_blacklist_v1.json")
                          .read_text())["symbols"])
    futmap = json.loads((OUT / "stock_futures_universe.json").read_text())["map"]

    today = date.today().isoformat()
    log(f"執行日 {today}")

    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    px: dict[tuple[str, str], float] = {}
    for sid, d, c in con.execute(
        "SELECT stock_id,trade_date,close FROM stock_daily_bars "
        "WHERE source='finmind' AND trade_date BETWEEN '2024-06-01' AND ? AND close>0",
        (today,)):
        px[(str(sid), str(d))] = float(c)

    seat_events: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for tid in STATIC_FLIP:
        rows = list(con.execute(
            "SELECT trade_date,stock_id,buy,sell FROM stock_broker_branch_daily "
            "WHERE securities_trader_id=? AND trade_date BETWEEN '2024-06-01' AND ? "
            "ORDER BY trade_date", (tid, today)))
        by_stock: dict[str, dict[str, tuple]] = defaultdict(dict)
        for d, sid, b, s in rows:
            by_stock[str(sid)][str(d)] = (float(b or 0), float(s or 0))
        for sid, m in by_stock.items():
            ds = sorted(m)
            for i, d in enumerate(ds):
                b, s = m[d]
                p = px.get((sid, d))
                if p is None or b <= 0 or b * p < 0.3e8 or i + 1 >= len(ds):
                    continue
                nb, ns = m[ds[i + 1]]
                seat_events[tid].append((d, ns / b if b > 0 else 0))
    for tid in seat_events:
        seat_events[tid].sort()
    log(f"分點事件載入完成 · {sum(len(v) for v in seat_events.values()):,} 筆")

    end = date.fromisoformat(today)
    r3_start = (end - timedelta(days=90)).isoformat()
    r1_start = (end - timedelta(days=30)).isoformat()

    rows = []
    drifted = []
    for tid, static_flip in STATIC_FLIP.items():
        es = seat_events[tid]
        full = [f for _, f in es]
        r3 = [f for d, f in es if d >= r3_start]
        r1 = [f for d, f in es if d >= r1_start]
        fm = round(median(full), 3) if full else None
        r3m = round(median(r3), 3) if len(r3) >= 5 else None
        r1m = round(median(r1), 3) if len(r1) >= 3 else None
        flag = ""
        if fm is not None and r3m is not None and r1m is not None:
            if (static_flip - r3m > DRIFT_THRESHOLD) and (static_flip - r1m > DRIFT_THRESHOLD):
                flag = "轉抱↓"
                drifted.append((tid, "轉抱", static_flip, r3m, r1m))
            elif (r3m - static_flip > DRIFT_THRESHOLD) and (r1m - static_flip > DRIFT_THRESHOLD):
                flag = "轉沖↑"
                drifted.append((tid, "轉沖", static_flip, r3m, r1m))
        rows.append(dict(tid=tid, name=NAME.get(tid, tid), n_full=len(full),
                         static_flip=static_flip, recomputed_full=fm,
                         n_r3=len(r3), r3=r3m, n_r1=len(r1), r1=r1m, flag=flag,
                         weak_flagged=tid in WEAK_SEATS))

    # 2026-08-13新增：PIT-safe flip 表——每席用「至今為止全部已完整發生的事件」
    # 算中位數（seat_events[tid]本身已保證每筆事件的T0+1資料已存在，見上方
    # build流程），供 build_candidates() 取代 seat_flip_table_frozen 那個
    # spec自己承認含look-ahead的全樣本表。缺門檻(<30筆)的席位明確設 None，
    # 呼叫端要 fail-closed（視同不合格），不能 fallback 回frozen值——那樣等於
    # 沒修。
    pit_flip: dict[str, float | None] = {}
    for tid in STATIC_FLIP:
        es = seat_events.get(tid) or []
        vals = [f for _, f in es]
        pit_flip[tid] = round(median(vals), 6) if len(vals) >= 30 else None
    n_usable = sum(1 for v in pit_flip.values() if v is not None)
    log(f"PIT flip 表算完 · {n_usable}/{len(STATIC_FLIP)} 席可用(>=30筆事件)")
    (OUT / "pit_seat_flip_latest.json").write_text(
        json.dumps(
            dict(
                computed_at=today,
                method="expanding-window median of seat's own historical "
                "(T0 buy -> T0+1 sell)/T0 buy flip, using only fully-realized "
                "events (T0+1 already occurred) as of computed_at; None if <30 samples",
                values=pit_flip,
            ),
            ensure_ascii=False, indent=1,
        ),
        encoding="utf-8",
    )
    log(f"→ {OUT / 'pit_seat_flip_latest.json'}")

    # 今日(執行日=週五)盤後候選（若非交易日/尚無資料則為空）
    candidates = []
    todays = defaultdict(list)
    for tid in STATIC_FLIP:
        for d, sid, b, s in con.execute(
            "SELECT trade_date,stock_id,buy,sell FROM stock_broker_branch_daily "
            "WHERE securities_trader_id=? AND trade_date=?", (tid, today)):
            p = px.get((str(sid), today))
            if p is None or not b or b <= 0:
                continue
            amt = float(b) * p
            if amt >= 0.3e8:
                todays[str(sid)].append(dict(tid=tid, amt=amt))
    for sid, es in todays.items():
        if sid in mega or sid not in futmap:
            continue
        keep = [e for e in es if (e["tid"], sid) not in MANUAL]
        if not keep or not any(STATIC_FLIP.get(e["tid"], 0) >= 0.40 for e in keep):
            continue
        candidates.append(dict(
            sid=sid, futures=futmap[sid], n_seats=len(set(e["tid"] for e in keep)),
            total_amt_yi=round(sum(e["amt"] for e in keep) / 1e8, 2),
            seats=sorted({e["tid"] for e in keep})))
    con.close()
    candidates.sort(key=lambda c: -c["n_seats"])

    WEEKLY_OUT.mkdir(parents=True, exist_ok=True)
    report_path = WEEKLY_OUT / f"seat_refresh_{today}.md"
    lines = [
        f"# 隔日沖席位週報 · {today}",
        "",
        "> Research only · 唯讀監控，本報告不會自動修改 FROZEN_SPEC_V1.json 或觸發任何下單。",
        "",
        "## 一、24席 flip 交叉驗證（凍結表 vs 重算全期 vs 近3月 vs 近1月）",
        "",
        "| 席位 | 名稱 | 凍結表 | 重算全期(n) | 近3月(n) | 近1月(n) | 漂移標記 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for r in sorted(rows, key=lambda x: -x["static_flip"]):
        weak = " ⚠️假象" if r["weak_flagged"] else ""
        lines.append(
            f"| {r['tid']} | {r['name']} | {r['static_flip']:.3f} | "
            f"{r['recomputed_full']}({r['n_full']}) | {r['r3']}({r['n_r3']}) | "
            f"{r['r1']}({r['n_r1']}) | {r['flag']}{weak} |"
        )
    lines += ["", "## 二、結構性漂移警示（連續2窗同向偏離 >0.10）", ""]
    if drifted:
        for tid, direction, fm, r3m, r1m in drifted:
            lines.append(f"- **{tid}（{NAME.get(tid,tid)}）{direction}**：全期{fm:.3f} → "
                        f"近3月{r3m:.3f} → 近1月{r1m:.3f}")
    else:
        lines.append("（本週無新增漂移，或既有漂移仍在observe中）")
    lines += ["", "## 三、本次執行日盤後候選（待下個交易日 08:45 驗證期貨跳空 >=6%）", ""]
    if candidates:
        lines.append("| 股票 | 期貨代碼 | 合格席數 | 合計買進(億) | 參與席位 |")
        lines.append("|---|---|---:|---:|---|")
        for c in candidates:
            lines.append(f"| {c['sid']} | {c['futures']} | {c['n_seats']} | "
                        f"{c['total_amt_yi']} | {','.join(c['seats'])} |")
    else:
        lines.append("（無符合條件事件，或執行日非交易日）")
    lines += [
        "", "## 四、行動建議（人工review用，不自動執行）", "",
        "- 若某席連續2週都出現同向漂移標記，考慮列入 v2 規格修訂候選（需人工決定，不自動改spec）",
        "- ⚠️標記席位（7008/913R）的flip分數主要由台積電貢獻，權重應打折參考",
        "- 本報告僅供人工review，凍結規格 FROZEN_SPEC_V1.json 的任何變更需另行手動決定並走 v2 流程",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")

    (WEEKLY_OUT / f"seat_refresh_{today}.json").write_text(
        json.dumps(dict(date=today, seats=rows, drifted=drifted, candidates=candidates),
                   ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"→ {report_path}")
    print(f"\n漂移警示 {len(drifted)} 席 · 候選標的 {len(candidates)} 檔")

    # 2026-08-08 code review 發現：futures_daily_cache.json（build_candidates() 的
    # ADV 20日流動性濾網用）沒有任何排程刷新，曾靜默停在舊資料近3週。這裡當作
    # 週五收盤後的順手更新，確保下週一開盤前至少有一次新鮮資料；不影響本檔案
    # 主要的席位漂移報告，失敗只記錄不中斷（try/except 隔離）。
    try:
        import subprocess
        import sys

        log("順帶刷新 futures_daily_cache.json（ADV 流動性濾網資料）...")
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/research/refresh_dayflip_futures_daily_cache.py")],
            check=True, timeout=1800,
        )
    except Exception as ex:  # noqa: BLE001
        log(f"⚠️ futures_daily_cache 刷新失敗（不影響本檔案主要輸出）：{ex}")


if __name__ == "__main__":
    main()
