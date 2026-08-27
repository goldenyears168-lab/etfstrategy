"""
Item AB: transplant C18acc's adopted structural pyramid-add rule (underwater_rebound,
config/order.yaml c18acc_pyramid_add) onto 9217 branch-follow L1H7 decisive trades.

Trade file: reports/research/branch-footprint-screen/whale_9217_5dnet95_trades.csv (n=36,
the scan_5d_net95 file used elsewhere this session — same file, reused exactly).

C18acc adopted rule (config/order.yaml c18acc_pyramid_add):
  trigger: ret_from_entry_pct < 0  AND  rebound_from_in_hold_trough >= 0.3 (30%, on a 30-min
           poll / "W3" intraday window scale)
  sizing:  equal_weight_second_unit, budget_fraction_of_initial=1.0 -> add doubles notional
  exit:    sync_exit -> both units exit at the SAME leg1 exit date/price

Daily-bar adaptation (we only have stock_daily_bars, no intraday):
  - Check trigger on trading days entry_date+1 and entry_date+2 (first 1-2 days of hold,
    per task spec), using info available as of that day's close only (lookahead forbidden).
  - trough = min(low) from entry_date..day_d inclusive
  - ret_from_entry = (close_d - entry_open)/entry_open
  - rebound_from_trough = (close_d - trough)/trough
  - Because C18acc's 0.3 threshold is calibrated to an intraday 30-min trough (much smaller
    absolute moves than a 1-2 daily-bar trough), we test BOTH the literal 30% threshold and a
    scaled-down 5%/10% sensitivity band, and report all three.
  - If triggered on day d, add unit fills at NEXT trading day's open (day d+1), consistent with
    the file's own convention of using next-day open for the original entry.
  - Both units exit at the trade's original exit_date/exit_close (sync_exit).
  - Friction: the trade file's own r_pct = raw_return_pct - 0.3pp (flat round-trip cost, backed
    out empirically from entry_open/exit_close vs r_pct). We apply the same flat -0.3pp to the
    blended return (does not double-count the extra buy commission on the 2nd unit; noted as a
    simplification -> if anything overstates pyramid's edge slightly).
"""
import csv
import json
import sqlite3
import sys

sys.path.insert(0, "src")
from stock_db import DEFAULT_DB_PATH  # noqa: E402

TRADES_CSV = "reports/research/branch-footprint-screen/whale_9217_5dnet95_trades.csv"
FEE_PP = 0.3  # flat round-trip friction, pp, matches file's embedded convention
OUT_DIR = "reports/research/c18acc_pyramid_9217_transplant"


def load_trades():
    with open(TRADES_CSV) as f:
        return list(csv.DictReader(f))


def get_bars(conn, stock_id, start, end):
    rows = conn.execute(
        """
        SELECT trade_date, open, high, low, close
        FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date BETWEEN ? AND ?
        ORDER BY trade_date
        """,
        (stock_id, start, end),
    ).fetchall()
    return rows


def main():
    trades = load_trades()
    conn = sqlite3.connect(f"file:{DEFAULT_DB_PATH}?mode=ro", uri=True)

    thresholds = [0.30, 0.10, 0.05]
    results = {th: [] for th in thresholds}

    detail_rows = []

    for t in trades:
        stock_id = t["stock_id"]
        entry_date = t["entry_date"]
        exit_date = t["exit_date"]
        entry_open = float(t["entry_open"])
        exit_close = float(t["exit_close"])
        orig_r_pct = float(t["r_pct"])

        bars = get_bars(conn, stock_id, entry_date, exit_date)
        if len(bars) < 3:
            detail_rows.append(
                dict(stock_id=stock_id, entry_date=entry_date, note="insufficient bars", n_bars=len(bars))
            )
            continue

        # bars[0] is entry_date itself. Candidates = first two trading days AFTER entry (idx 1,2)
        trough_low = bars[0][3]  # low of entry day
        trig_info = {th: None for th in thresholds}
        for idx in (1, 2):
            if idx >= len(bars) - 0:  # need at least this bar to exist
                break
            trade_date, o, h, low, close = bars[idx]
            trough_low = min(trough_low, low)
            ret_from_entry = (close - entry_open) / entry_open
            rebound = (close - trough_low) / trough_low if trough_low else None
            if ret_from_entry < 0 and rebound is not None:
                for th in thresholds:
                    if trig_info[th] is None and rebound >= th:
                        # fill on NEXT bar's open, if it exists
                        if idx + 1 < len(bars):
                            add_price = bars[idx + 1][1]
                            add_date = bars[idx + 1][0]
                        else:
                            # no bar left to fill at open before exit; skip (can't execute)
                            continue
                        trig_info[th] = dict(
                            trigger_date=trade_date,
                            ret_from_entry_pct=ret_from_entry * 100,
                            rebound_pct=rebound * 100,
                            add_date=add_date,
                            add_price=add_price,
                        )

        row = dict(
            stock_id=stock_id,
            entry_date=entry_date,
            exit_date=exit_date,
            entry_open=entry_open,
            exit_close=exit_close,
            orig_r_pct=orig_r_pct,
        )
        for th in thresholds:
            info = trig_info[th]
            if info is None:
                row[f"trig_{th}"] = False
            else:
                blended_entry = 0.5 * entry_open + 0.5 * info["add_price"]
                blended_raw = (exit_close / blended_entry - 1) * 100
                blended_r_pct = blended_raw - FEE_PP
                row[f"trig_{th}"] = True
                row[f"blend_entry_{th}"] = blended_entry
                row[f"blend_r_pct_{th}"] = blended_r_pct
                row[f"add_price_{th}"] = info["add_price"]
                row[f"rebound_pct_{th}"] = info["rebound_pct"]
                results[th].append(
                    dict(
                        stock_id=stock_id,
                        entry_date=entry_date,
                        orig_r_pct=orig_r_pct,
                        blend_r_pct=blended_r_pct,
                        delta_pp=blended_r_pct - orig_r_pct,
                    )
                )
        detail_rows.append(row)

    conn.close()

    with open(f"{OUT_DIR}/trade_detail_all_thresholds.csv", "w", newline="") as f:
        fieldnames = sorted({k for row in detail_rows for k in row})
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(detail_rows)

    summary = {"n_trades_total": len(trades), "fee_pp": FEE_PP, "by_threshold": {}}

    print(f"n_trades_total={len(trades)}")
    for th in thresholds:
        elig = results[th]
        print(f"\n--- threshold rebound>={th*100:.0f}% (and underwater) ---")
        print(f"n_eligible={len(elig)}")
        if not elig:
            continue
        n = len(elig)
        orig_mean = sum(r["orig_r_pct"] for r in elig) / n
        blend_mean = sum(r["blend_r_pct"] for r in elig) / n
        delta_mean = sum(r["delta_pp"] for r in elig) / n
        wins = sum(1 for r in elig if r["delta_pp"] > 0)
        for r in elig:
            print(
                f"  {r['stock_id']} {r['entry_date']}: orig={r['orig_r_pct']:.2f}%  "
                f"pyramid={r['blend_r_pct']:.2f}%  delta={r['delta_pp']:+.2f}pp"
            )
        print(f"  unweighted mean orig={orig_mean:.3f}%  pyramid_blend={blend_mean:.3f}%  delta={delta_mean:+.3f}pp  pyramid_wins={wins}/{n}")

        # portfolio / size-weighted view over ALL 36 trades: eligible trades get 2x units
        total_units_baseline = len(trades)
        total_units_pyramid = len(trades) + n  # eligible trades count as 2 units
        pnl_baseline = sum(float(t["r_pct"]) for t in trades)  # 1 unit each, sum of pct*1
        # pyramid pnl: non-eligible trades unchanged (1 unit); eligible trades use blend_r_pct * 2 units
        elig_ids = {(r["stock_id"], r["entry_date"]) for r in elig}
        pnl_pyramid = 0.0
        for t in trades:
            key = (t["stock_id"], t["entry_date"])
            if key in elig_ids:
                br = next(r["blend_r_pct"] for r in elig if (r["stock_id"], r["entry_date"]) == key)
                pnl_pyramid += br * 2
            else:
                pnl_pyramid += float(t["r_pct"])
        avg_baseline = pnl_baseline / total_units_baseline
        avg_pyramid = pnl_pyramid / total_units_pyramid
        print(
            f"  PORTFOLIO (size-weighted, all 36 trades, eligible=2x units): "
            f"avg_return_per_unit baseline={avg_baseline:.3f}%  with_pyramid={avg_pyramid:.3f}%  "
            f"delta={avg_pyramid-avg_baseline:+.3f}pp  total_units {total_units_baseline}->{total_units_pyramid}"
        )
        summary["by_threshold"][th] = dict(
            n_eligible=n,
            unweighted_mean_orig_pct=orig_mean,
            unweighted_mean_pyramid_pct=blend_mean,
            unweighted_delta_pp=delta_mean,
            pyramid_wins=wins,
            portfolio_avg_baseline_pct=avg_baseline,
            portfolio_avg_pyramid_pct=avg_pyramid,
            portfolio_delta_pp=avg_pyramid - avg_baseline,
            total_units_baseline=total_units_baseline,
            total_units_pyramid=total_units_pyramid,
            eligible_trades=elig,
        )

    with open(f"{OUT_DIR}/summary.json", "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
