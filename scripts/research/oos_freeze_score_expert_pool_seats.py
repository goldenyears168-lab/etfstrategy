#!/usr/bin/env python3
"""處置股專家池 · OOS 凍結評分（破除 in-sample 選擇偏誤）.

問題：現行 9→8 席是從 2026 第二次處置資料「選出來的」，在同一批資料上檢驗＝循環驗證。
本腳本凍結「今日的候選名單＋門檻」，之後只在**凍結日之後才公告**的處置窗上評分，
才是真正的前推 OOS。每次重跑會重新抓 TWSE 處置清單、把 p_start > freeze_date 的
episodes 累積進 OOS 樣本；隨新處置窗到來，OOS n 逐步變厚。

同時修正驗證盲點：
  * 報酬用 adj_close 口徑（除權息調整；raw 會在除息日造成假跌）。
  * 套 LIVE 規則：T+1 開盤 gap>+5% 追價 → skip（open_filters.hard_skip）；
    T0 soft_flags（r3≥15%&ix≤0 等）記為 flagged 但不硬性排除（與 live 一致）。
  * 候選名單包含「已移除的 9227」與「降權前 920F@0.1億」當**審計影子**，
    這樣 OOS 能回頭驗證移除/降權兩個決定是否正確。
  * 快照鉤子：replica mtime / data_max 寫進 manifest。

用法（凍結日首次跑＝建立 freeze manifest；之後每次跑＝累積 OOS）：
  PYTHONPATH=src .venv/bin/python scripts/research/oos_freeze_score_expert_pool_seats.py
  # 強制重設凍結日：--freeze-date 2026-07-28
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from stock_db import DATA_DIR, DEFAULT_DB_PATH

ROOT = Path(__file__).resolve().parents[2]
REPLICA_DB = DATA_DIR / "replica" / "stocks.db"
LOCAL_DB = DEFAULT_DB_PATH
# 分點 tape 來源：優先 SSOT data/stocks.db（mini 上為權威且當前；處置窗全 2026，local 2026 覆蓋完整）。
# Book 上 replica 為 fallback。OOS 累積需要「當前」資料，故不用凍結時的 replica 快照。
BRANCH_DB = LOCAL_DB if LOCAL_DB.exists() else REPLICA_DB
CFG = ROOT / "config/second_disp_expert_pool_watch.json"
EP_JSON = ROOT / "reports/research/branch-footprint-screen/second_disp_top30_l1h7/twse_second_disp_2026.json"
OUT = ROOT / "reports/research/branch-footprint-screen/expanded/oos_freeze"
FREEZE_MANIFEST = OUT / "freeze_manifest.json"

COST, HOLD, BETA, DEDUP_DAYS = 0.003, 7, 1.15, 5
GAP_SKIP_PCT = 5.0  # open_filters.hard_skip gap_t1_gt5
FROZEN_DECISION_DATE = "2026-07-28"  # 名單定案日；OOS=announce晚於此的處置窗（固定，不隨 data_max 漂移）

# 審計影子（凍結時一併評分，回頭驗證 2026-07-28 的 floor 決定）。
# seats 已 REVERT 回原始 floor（920F@0.1、9227@0.3）；影子改追「曾短暫改成的替代 floor」，
# 讓 OOS 回頭驗證「revert 是否正確」（若替代 floor 在 OOS 上更好，代表 revert 錯了）。
AUDIT_SHADOWS = [
    {"id": "9227", "abs_floor_yi": 0.5, "role": "審計·曾擬改floor0.5(已revert回0.3)"},
    {"id": "920F", "abs_floor_yi": 0.3, "role": "審計·曾降權floor0.3(已revert回0.1)"},
]


def months(lo, hi):
    y, m = int(lo[:4]), int(lo[5:7]); out = []
    while f"{y:04d}-{m:02d}-01" <= hi:
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        out.append((f"{y:04d}-{m:02d}-01", f"{ny:04d}-{nm:02d}-01")); y, m = ny, nm
    return out


def load_candidates():
    """凍結時的候選名單：現行 seats + 審計影子（以 id+floor 區分，永遠納入以回頭驗證去留/降權）。"""
    cfg = json.loads(CFG.read_text("utf-8"))
    cands = [{"id": str(s["id"]), "abs_floor_yi": float(s["abs_floor_yi"]),
              "name": s.get("name", "?"), "role": "seat"} for s in cfg["seats"]]
    have = {(c["id"].upper(), round(c["abs_floor_yi"], 4)) for c in cands}
    for sh in AUDIT_SHADOWS:
        key = (sh["id"].upper(), round(float(sh["abs_floor_yi"]), 4))
        if key not in have:  # 只在「同 id 同 floor」已存在時才略過，floor 不同＝獨立審計候選
            cands.append({**sh, "name": sh.get("name", sh["id"])})
    return cands


def load_episodes():
    ep = json.loads(EP_JSON.read_text("utf-8"))
    # NOTE: 目前 cache 為 2026 第二次處置；未來第三次窗到來時，需先更新此 cache
    #（run_second_disp_top30_l1h7.fetch_twse_second_2026 或等值抓取）再重跑本腳本。
    return ep


def load_prices(sids):
    frames = []
    for db, pref in [(LOCAL_DB, 0), (REPLICA_DB, 1)]:
        if not db.exists():
            continue
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        ph = ",".join("?" * len(sids))
        d = pd.read_sql_query(
            f"""SELECT stock_id,trade_date,open,close,adj_close FROM stock_daily_bars
                WHERE source='finmind' AND close>0 AND trade_date>='2025-11-01' AND stock_id IN ({ph})""",
            c, params=list(sids))
        c.close(); d["pref"] = pref; frames.append(d)
    px = pd.concat(frames, ignore_index=True)
    px["stock_id"] = px.stock_id.astype(str)
    px["open"] = px["open"].where(px["open"] > 0, px["close"])
    px = px.sort_values("pref").drop_duplicates(["stock_id", "trade_date"], keep="first")
    px["fac"] = np.where(px["adj_close"].notna() & (px["adj_close"] > 0), px["adj_close"] / px["close"], 1.0)
    return px


def load_ix():
    c = sqlite3.connect(f"file:{LOCAL_DB}?mode=ro", uri=True)
    ix = pd.read_sql_query("""SELECT date,open,close FROM daily_bars WHERE code='IX0001'
        AND date>='2025-11-01' AND open>0 AND close>0""", c)
    c.close()
    ix = ix.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    return ix


def score(cands, episodes, freeze_date, oos_only):
    wins = defaultdict(list)
    for e in episodes:
        wins[e["sid"]].append((e["p_start"], e["p_end"], e.get("announce", e["p_start"])))
    sids = sorted(wins)
    if not sids:
        return pd.DataFrame(), {"n_episodes": 0}

    conn = sqlite3.connect(f"file:{BRANCH_DB}?mode=ro", uri=True)
    data_max = conn.execute("SELECT MAX(trade_date) FROM stock_broker_branch_daily").fetchone()[0]
    lo_scan = min(e["p_start"] for e in episodes)
    ph = ",".join("?" * len(sids)); frames = []
    for lo, hi in months(lo_scan[:7] + "-01", data_max):
        d = pd.read_sql_query(
            f"""SELECT trade_date,stock_id,securities_trader_id,buy FROM stock_broker_branch_daily
                WHERE source='finmind' AND buy>0 AND trade_date>=? AND trade_date<? AND trade_date<=? AND stock_id IN ({ph})""",
            conn, params=[lo, hi, data_max, *sids])
        if len(d):
            frames.append(d)
    conn.close()
    tape = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if tape.empty:
        return pd.DataFrame(), {"n_episodes": len(episodes), "data_max": data_max}
    tape["stock_id"] = tape.stock_id.astype(str)

    def win_hit(sid, td):
        for a, b, ann in wins.get(sid, []):
            if a <= td <= b and (not oos_only or ann > freeze_date):
                return True
        return False
    tape = tape[[win_hit(s, t) for s, t in zip(tape.stock_id, tape.trade_date)]]

    px = load_prices(sids)
    ix = load_ix()
    ixd = ix["date"].tolist(); ixo = ix["open"].tolist(); ixc = ix["close"].tolist()
    ixpos = {d: i for i, d in enumerate(ixd)}
    cmap = {(s, d): c for s, d, c in zip(px.stock_id, px.trade_date, px.close)}
    bars = {}
    for sid, g in px.sort_values("trade_date").groupby("stock_id"):
        bars[sid] = (g.trade_date.tolist(), g.open.tolist(), g.close.tolist(), g.fac.tolist())

    def eval_event(sid, sig):
        b = bars.get(sid)
        if not b or sig not in ixpos:
            return None
        dts, ops, cls, facs = b
        if sig not in dts:
            return None
        i = dts.index(sig); e, x = i + 1, i + HOLD
        if x >= len(dts):
            return {"missing_exit": True}
        j = ixpos[sig]
        if j + HOLD >= len(ixd):
            return None
        entry_open, exit_close, prev_close = ops[e], cls[x], cls[i]
        gap = (entry_open / prev_close - 1) * 100 if prev_close > 0 else 0.0
        aeo = entry_open * (facs[e] if facs[e] > 0 else 1.0)
        axc = exit_close * (facs[x] if facs[x] > 0 else 1.0)
        r_s = axc / aeo - 1 - COST
        r_ix = ixc[j + HOLD] / ixo[j + 1] - 1
        return {"missing_exit": False, "gap": gap, "r_adj": (r_s - BETA * r_ix) * 100}

    def dedupe(evs):
        evs = sorted(evs, key=lambda r: (r[0], r[1])); keep = []; last = {}
        for sid, sig in evs:
            lk = last.get(sid)
            if lk is None or (pd.Timestamp(sig) - pd.Timestamp(lk)).days > DEDUP_DAYS:
                keep.append((sid, sig)); last[sid] = sig
        return keep

    rows = []
    for c in cands:
        floor = c["abs_floor_yi"] * 1e8
        sub = tape[tape.securities_trader_id.str.upper() == c["id"].upper()]
        evs = [(r.stock_id, r.trade_date) for r in sub.itertuples(index=False)
               if (cmap.get((r.stock_id, r.trade_date)) or 0) * r.buy >= floor]
        evs = dedupe(evs)
        res = [eval_event(s, sig) for s, sig in evs]
        res = [x for x in res if x]
        miss = sum(1 for x in res if x.get("missing_exit"))
        good = [x for x in res if not x.get("missing_exit")]
        live = [x["r_adj"] for x in good if x["gap"] <= GAP_SKIP_PCT]  # LIVE 規則
        ra = np.array(live)
        n = len(ra)
        rows.append({
            "id": c["id"], "name": c["name"], "role": c["role"], "abs_floor_yi": c["abs_floor_yi"],
            "n_live": n, "gap_skipped": sum(1 for x in good if x["gap"] > GAP_SKIP_PCT),
            "halt_dropped": miss,
            "median_r_adj_pct": float(np.median(ra)) if n else None,
            "mean_r_adj_pct": float(np.mean(ra)) if n else None,
            "win_rate_pct": float((ra > 0).mean() * 100) if n else None,
            "p_value": float(stats.ttest_1samp(ra, 0.0)[1]) if n >= 5 else None,
        })
    meta = {"n_episodes": len(episodes), "data_max": data_max,
            "oos_episodes": sum(1 for e in episodes if e.get("announce", e["p_start"]) > freeze_date)}
    return pd.DataFrame(rows), meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refreeze", action="store_true",
                    help="重設凍結：以當前 config seats+審計影子重建 manifest 與 frozen reference（僅在名單定案變更時用）")
    ap.add_argument("--freeze-date", default=None, help="YYYY-MM-DD；配 --refreeze 使用，預設 data_max")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    episodes = load_episodes()
    snap_mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(BRANCH_DB.stat().st_mtime))
    REF_CSV = OUT / "frozen_reference_live_rule.csv"

    refreeze = args.refreeze or not FREEZE_MANIFEST.exists()
    if refreeze:
        # 凍結：以當前 config 定案名單建立 manifest + 一次性 frozen reference（之後不再重算）
        conn = sqlite3.connect(f"file:{BRANCH_DB}?mode=ro", uri=True)
        dm = conn.execute("SELECT MAX(trade_date) FROM stock_broker_branch_daily").fetchone()[0]
        conn.close()
        cands = load_candidates()
        freeze_date = args.freeze_date or FROZEN_DECISION_DATE
        ref, m_ref = score(cands, episodes, freeze_date, oos_only=False)
        ref.to_csv(REF_CSV, index=False)
        man = {"freeze_date": freeze_date, "frozen_at_branch_mtime": snap_mtime,
               "frozen_at_data_max": dm, "candidates": cands,
               "rule": {"hold": "L1H7", "cost_bps": 30, "beta": 1.15, "gap_skip_pct": GAP_SKIP_PCT,
                        "price": "adj_close-corrected", "dedupe_cal_days": DEDUP_DAYS},
               "note": "OOS 只算 p_start > freeze_date 的處置窗，且以此 candidates（含審計影子）為準，不隨 config 後續變動。第三次窗到來先更新 twse cache 再重跑。"}
        FREEZE_MANIFEST.write_text(json.dumps(man, ensure_ascii=False, indent=2), "utf-8")
        print(f"[FREEZE] 建立凍結：freeze_date={freeze_date} candidates={len(cands)} → {FREEZE_MANIFEST.name}")
    else:
        man = json.loads(FREEZE_MANIFEST.read_text("utf-8"))
    freeze_date = man["freeze_date"]
    cands = man["candidates"]  # 一律用凍結名單，不用當前 config

    # OOS：只算凍結日之後公告/開始的處置窗，用凍結名單
    oos, m_oos = score(cands, episodes, freeze_date, oos_only=True)
    oos.to_csv(OUT / "oos_scores.csv", index=False)

    pd.set_option("display.width", 240, "display.max_columns", 20)
    print(f"[SNAPSHOT] branch_mtime={snap_mtime} freeze_date={freeze_date} data_max={m_oos.get('data_max')}")
    print(f"[INFO] episodes={m_oos.get('n_episodes','?')}  OOS episodes(p_start>freeze)={m_oos.get('oos_episodes',0)}  frozen_candidates={len(cands)}")
    if REF_CSV.exists():
        print("\n=== 凍結參考（凍結時計算，LIVE 規則 gap>5%skip+adj_close；不隨後續重算）===")
        print(pd.read_csv(REF_CSV, dtype={"id": str}).to_string(index=False))
    if m_oos.get("oos_episodes", 0) == 0 or oos.empty or oos["n_live"].sum() == 0:
        print("\n=== OOS ===\n(尚無可評分的 OOS 事件：或無凍結日後的處置窗，或分點 tape 尚未涵蓋新窗日期。"
              "分點資料前進到新窗期間後，本項自動開始累積。)")
    else:
        print("\n=== OOS（僅凍結日後處置窗 · 用凍結名單）===")
        print(oos.to_string(index=False))
    print(f"\n[DONE] → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
