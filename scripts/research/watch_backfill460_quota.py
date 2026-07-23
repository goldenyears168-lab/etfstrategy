#!/usr/bin/env python3
"""Quota-aware scheduler for universe460 FinMind branch-tape backfill (Book only).

Sponsor limit ≈ 6000 req/hour. This watcher:
  - waits until usage <= --resume-below
  - skips weekday 08:30–14:00 Asia/Taipei (leave headroom for mini live)
  - runs one backfill wave (workers=1), then waits again on exit 4 (quota) / 3 (ban)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import requests  # noqa: E402
from finmind_client import finmind_token  # noqa: E402
from stock_db import connect  # noqa: E402

TW = ZoneInfo("Asia/Taipei")
USER_INFO = "https://api.web.finmindtrade.com/v2/user_info"


def now_tw() -> datetime:
    return datetime.now(tz=TW)


def usage() -> tuple[int, int]:
    tok = finmind_token()
    if not tok:
        raise RuntimeError("FINMIND_TOKEN unset")
    r = requests.get(
        USER_INFO,
        headers={"Authorization": f"Bearer {tok}"},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    return int(j.get("user_count") or 0), int(j.get("api_request_limit") or 6000)


def in_live_blackout(ts: datetime) -> bool:
    """Weekday 08:30–14:00 TW — protect mini live FinMind sleeves."""
    if ts.weekday() >= 5:
        return False
    minutes = ts.hour * 60 + ts.minute
    return 8 * 60 + 30 <= minutes < 14 * 60


def write_status(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def reclassify_and_write_abc(
    *,
    master_universe: Path,
    probe_start: str,
    probe_end: str,
    classification_csv: Path,
    abc_universe: Path,
) -> dict[str, int]:
    """Re-score the 40-day footprint and emit only A/B/C for deep backfill."""
    import numpy as np
    import pandas as pd

    universe = json.loads(master_universe.read_text(encoding="utf-8"))
    conn = connect()

    def score_branch(item: dict) -> dict:
        tid = str(item["id"])
        name = str(item["name"])
        region = str(item.get("region") or "")
        df = pd.read_sql_query(
            """
            SELECT b.trade_date, b.stock_id, b.buy,
                   b.buy * p.close AS buy_amt
            FROM stock_broker_branch_daily b
            JOIN stock_daily_bars p
              ON p.stock_id=b.stock_id
             AND p.trade_date=b.trade_date
             AND p.source='finmind'
            WHERE b.securities_trader_id=? AND b.source='finmind'
              AND b.trade_date >= ? AND b.trade_date <= ?
              AND p.close > 0
              AND length(b.stock_id)=4
              AND b.stock_id GLOB '[0-9][0-9][0-9][0-9]'
            """,
            conn,
            params=(tid, probe_start, probe_end),
        )
        base = {"region": region, "id": tid, "name": name}
        if df.empty:
            return {
                **base,
                "n_days": 0,
                "share_med": np.nan,
                "hhi_med": np.nan,
                "top1_p75_億": np.nan,
                "frac_share25": np.nan,
                "frac_share40": np.nan,
                "n_fix2e_sh25": 0,
                "n_fix5e_sh40": 0,
                "score": np.nan,
                "tier": "Z 無資料",
            }
        df = df.sort_values(
            ["trade_date", "buy_amt"], ascending=[True, False]
        )
        df["day_buy"] = df.groupby("trade_date")["buy_amt"].transform("sum")
        df["buy_share"] = df["buy_amt"] / df["day_buy"].replace(0, np.nan)
        df["rk"] = df.groupby("trade_date")["buy_amt"].rank(
            method="first", ascending=False
        )
        top1 = df[df["rk"] == 1].copy()
        hhi = df.groupby("trade_date").apply(
            lambda g: float((g.buy_amt / g.buy_amt.sum()).pow(2).sum())
            if g.buy_amt.sum() > 0
            else np.nan,
            include_groups=False,
        )
        abs_p75 = float(top1.buy_amt.quantile(0.75))
        share_med = float(top1.buy_share.median())
        hhi_med = float(hhi.median())
        frac25 = float((top1.buy_share >= 0.25).mean())
        frac40 = float((top1.buy_share >= 0.40).mean())

        def n_ev(abs_threshold: float, share_threshold: float) -> int:
            return int(
                (
                    (top1.buy_amt >= abs_threshold)
                    & (top1.buy_share >= share_threshold)
                ).sum()
            )

        score = (
            share_med * 25
            + hhi_med * 15
            + frac25 * 8
            + frac40 * 12
            + min(abs_p75 / 1e8, 5) * 0.8
            - (5.0 if abs_p75 < 0.3e8 else 0.0)
        )
        if share_med >= 0.25 and abs_p75 >= 1e8:
            tier = "A 明顯集中+夠大"
        elif share_med >= 0.15 and frac25 >= 0.20 and abs_p75 >= 0.5e8:
            tier = "B 中度集中"
        elif share_med >= 0.12 and abs_p75 >= 0.3e8:
            tier = "C 弱訊號"
        else:
            tier = "D 分散/零售型"
        return {
            **base,
            "n_days": int(top1.trade_date.nunique()),
            "share_med": share_med,
            "hhi_med": hhi_med,
            "top1_p75_億": abs_p75 / 1e8,
            "frac_share25": frac25,
            "frac_share40": frac40,
            "n_fix2e_sh25": n_ev(2e8, 0.25),
            "n_fix5e_sh40": n_ev(5e8, 0.40),
            "score": score,
            "tier": tier,
        }

    try:
        scored = pd.DataFrame(score_branch(item) for item in universe)
    finally:
        conn.close()
    tier_order = {
        "A 明顯集中+夠大": 0,
        "B 中度集中": 1,
        "C 弱訊號": 2,
        "D 分散/零售型": 3,
        "Z 無資料": 4,
    }
    scored["_tier_order"] = scored["tier"].map(tier_order)
    scored = scored.sort_values(
        ["_tier_order", "score"], ascending=[True, False], na_position="last"
    ).drop(columns="_tier_order")
    scored.insert(0, "rank", range(1, len(scored) + 1))
    classification_csv.parent.mkdir(parents=True, exist_ok=True)
    scored.to_csv(classification_csv, index=False)

    abc_ids = set(
        scored.loc[
            scored["tier"].str.startswith(("A", "B", "C")), "id"
        ].astype(str)
    )
    abc = [item for item in universe if str(item["id"]) in abc_ids]
    abc_universe.write_text(
        json.dumps(abc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    counts = {
        str(tier): int(count)
        for tier, count in scored["tier"].value_counts().items()
    }
    counts["ABC"] = len(abc)
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume-below", type=int, default=800)
    ap.add_argument("--poll-sec", type=int, default=300)
    ap.add_argument("--delay", type=float, default=0.55)
    ap.add_argument("--start", default="2024-07-01")
    ap.add_argument("--end", default="2026-07-16")
    ap.add_argument(
        "--universe-json",
        type=Path,
        default=ROOT
        / "reports/research/branch-footprint-screen/_universe460.json",
    )
    ap.add_argument(
        "--progress",
        type=Path,
        default=ROOT
        / "reports/research/branch-footprint-screen/_backfill460_progress.json",
    )
    ap.add_argument(
        "--status",
        type=Path,
        default=ROOT
        / "reports/research/branch-footprint-screen/_backfill460_watch_status.json",
    )
    ap.add_argument(
        "--staged-z-first",
        action="store_true",
        help="After the Z 40-day probe, reclassify all branches then deep-fill A/B/C",
    )
    ap.add_argument(
        "--master-universe",
        type=Path,
        default=ROOT
        / "reports/research/branch-footprint-screen/_universe460.json",
    )
    ap.add_argument(
        "--classification-csv",
        type=Path,
        default=ROOT
        / "reports/research/branch-footprint-screen/"
        "taiwan_branch_footprint_rank40d_refreshed.csv",
    )
    ap.add_argument(
        "--abc-universe",
        type=Path,
        default=ROOT
        / "reports/research/branch-footprint-screen/_abc_2y_universe.json",
    )
    ap.add_argument(
        "--abc-progress",
        type=Path,
        default=ROOT
        / "reports/research/branch-footprint-screen/_abc_2y_progress.json",
    )
    ap.add_argument("--abc-start", default="2024-07-01")
    ap.add_argument("--abc-end", default="2026-07-16")
    ap.add_argument("--abc-resume-below", type=int, default=800)
    ap.add_argument("--max-waves", type=int, default=0, help="0 = unlimited")
    args = ap.parse_args()

    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    wave = 0
    stage = "z40" if args.staged_z_first else "backfill"
    print(
        f"watch start resume_below={args.resume_below} poll={args.poll_sec}s "
        f"blackout=weekday 08:30-14:00 TW delay={args.delay}",
        flush=True,
    )

    while True:
        wave += 1
        if args.max_waves and wave > args.max_waves:
            print("max waves reached", flush=True)
            return 0

        while True:
            ts = now_tw()
            try:
                used, limit = usage()
            except Exception as exc:  # noqa: BLE001
                print(f"usage_probe_error: {exc}", flush=True)
                write_status(
                    args.status,
                    {
                        "state": "usage_probe_error",
                        "error": str(exc),
                        "at": ts.isoformat(timespec="seconds"),
                        "wave": wave,
                        "stage": stage,
                    },
                )
                time.sleep(args.poll_sec)
                continue

            blackout = in_live_blackout(ts)
            ready = (not blackout) and used <= args.resume_below
            write_status(
                args.status,
                {
                    "state": "waiting" if not ready else "ready",
                    "at": ts.isoformat(timespec="seconds"),
                    "usage": used,
                    "limit": limit,
                    "resume_below": args.resume_below,
                    "blackout": blackout,
                    "wave": wave,
                    "stage": stage,
                    "next_poll_sec": args.poll_sec,
                },
            )
            print(
                f"[{ts.strftime('%m-%d %H:%M:%S')}] usage={used}/{limit} "
                f"blackout={blackout} ready={ready} wave={wave}",
                flush=True,
            )
            if ready:
                break
            time.sleep(args.poll_sec)

        stamp = now_tw().strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"backfill_{stage}_wave_{stamp}.log"
        latest = (
            ROOT
            / "reports/research/branch-footprint-screen/_backfill460_latest_log.txt"
        )
        latest.write_text(str(log_path.relative_to(ROOT)) + "\n", encoding="utf-8")
        cmd = [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "scripts/research/backfill_broker_branch_tape.py"),
            "--universe-json",
            str(args.universe_json),
            "--progress",
            str(args.progress),
            "--start",
            args.start,
            "--end",
            args.end,
            "--delay",
            str(args.delay),
            "--fetch-workers",
            "1",
        ]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PYTHONUNBUFFERED"] = "1"
        print(f"WAVE {wave} start log={log_path}", flush=True)
        write_status(
            args.status,
            {
                "state": "running",
                "at": now_tw().isoformat(timespec="seconds"),
                "wave": wave,
                "stage": stage,
                "log": str(log_path),
            },
        )
        with log_path.open("w", encoding="utf-8") as fh:
            proc = subprocess.run(cmd, cwd=str(ROOT), env=env, stdout=fh, stderr=fh)
        code = int(proc.returncode)
        print(f"WAVE {wave} exit={code}", flush=True)
        write_status(
            args.status,
            {
                "state": "wave_done",
                "at": now_tw().isoformat(timespec="seconds"),
                "wave": wave,
                "stage": stage,
                "exit": code,
                "log": str(log_path),
            },
        )
        if code == 0:
            if stage == "z40":
                print("Z40 complete; reclassifying all branches", flush=True)
                counts = reclassify_and_write_abc(
                    master_universe=args.master_universe,
                    probe_start=args.start,
                    probe_end=args.end,
                    classification_csv=args.classification_csv,
                    abc_universe=args.abc_universe,
                )
                print(f"reclassified counts={counts}", flush=True)
                stage = "abc2y"
                args.universe_json = args.abc_universe
                args.progress = args.abc_progress
                args.start = args.abc_start
                args.end = args.abc_end
                args.resume_below = args.abc_resume_below
                write_status(
                    args.status,
                    {
                        "state": "reclassified",
                        "at": now_tw().isoformat(timespec="seconds"),
                        "stage": stage,
                        "counts": counts,
                        "abc_universe": str(args.abc_universe),
                    },
                )
                continue
            print("backfill complete", flush=True)
            return 0
        # 3=ip ban, 4=quota — wait for recovery either way
        time.sleep(args.poll_sec)


if __name__ == "__main__":
    raise SystemExit(main())
