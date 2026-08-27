"""2026-08-13: trade-size-distribution-shape vs MAE, single-day slice
(2024-04-09) for a multi-agent parallel sweep. Untested angle: SHAPE of the
trade-size distribution in a trailing window before entry (Hill tail-index
proxy, top-1%-by-size volume share, Herfindahl index of size shares) as a
predictor of MAE on the live baseline recipe's main hang_anchor=O entries.
Read-only, no config/live-path writes.
"""
from __future__ import annotations

import sys
from datetime import timedelta

sys.path.insert(0, "src")
sys.path.insert(0, "scripts/research")

import numpy as np
import pandas as pd

DAY = "2024-04-09"
WINDOW_MIN = 5


def hill_tail_index(sizes: np.ndarray, k_frac: float = 0.25) -> float | None:
    """Hill estimator on the upper tail of trade sizes. Needs k>=5 order
    statistics to be even nominally meaningful; returns None (proxy used
    instead) below that -- trade counts in a 5-min window can be small."""
    s = np.sort(sizes[sizes > 0])[::-1]
    n = len(s)
    k = max(5, int(n * k_frac))
    if n < 10 or k >= n:
        return None
    top = s[:k]
    x_k1 = s[k]
    if x_k1 <= 0:
        return None
    logs = np.log(top / x_k1)
    return float(k / logs.sum()) if logs.sum() > 0 else None


def cv_proxy(sizes: np.ndarray) -> float | None:
    """Fallback tail-heaviness proxy when Hill is impractical (n too
    small): coefficient of variation of trade sizes. Higher CV = fatter/
    more concentrated size tail, same qualitative direction as a low Hill
    index (lower Hill alpha = fatter tail)."""
    s = sizes[sizes > 0]
    if len(s) < 3 or s.mean() == 0:
        return None
    return float(s.std(ddof=1) / s.mean())


def top1pct_share(sizes: np.ndarray) -> float | None:
    s = np.sort(sizes[sizes > 0])
    n = len(s)
    if n < 3:
        return None
    k = max(1, int(np.ceil(n * 0.01)))
    total = s.sum()
    if total <= 0:
        return None
    return float(s[-k:].sum() / total)


def herfindahl(sizes: np.ndarray) -> float | None:
    s = sizes[sizes > 0]
    total = s.sum()
    if total <= 0 or len(s) < 2:
        return None
    shares = s / total
    return float((shares ** 2).sum())


def features_before(ticks: pd.DataFrame, entry_iso: str) -> dict:
    entry_ts = pd.Timestamp(entry_iso).tz_localize(None)
    lo = entry_ts - timedelta(minutes=WINDOW_MIN)
    window = ticks[(ticks["dt"] >= lo) & (ticks["dt"] < entry_ts)]
    sizes = window["volume"].to_numpy(dtype=float)
    hill = hill_tail_index(sizes)
    proxy_used = hill is None
    tail_metric = hill if hill is not None else cv_proxy(sizes)
    return {
        "n_ticks_in_window": int(len(sizes)),
        "hill_or_proxy": tail_metric,
        "used_proxy": proxy_used,
        "top1pct_share": top1pct_share(sizes),
        "herfindahl": herfindahl(sizes),
    }


def main() -> None:
    import os
    os.environ["ORDER_TMF_CHANNEL_NIGHT_USES_DAY_RECIPE"] = "1"

    from tmf_walkforward_harness import run_batch
    from order.tmf_channel_pv16_book import specialized_cell_book
    from tx_channel_tick_validation import load_front_month_ticks

    result = run_batch([DAY], session_pv_book=specialized_cell_book(), label="tradesize_mae")
    trades = result.get("trades", [])
    print(f"n_days_processed={result.get('n_days_processed')} n_days_with_data={result.get('n_days_with_data')}")
    print(f"n_trades={len(trades)}")

    ticks = load_front_month_ticks(DAY)
    if ticks is None:
        print("NO TICKS FOR DAY -- aborting")
        return

    rows = []
    for tr in trades:
        et = tr.get("et")
        feats = features_before(ticks, et)
        row = {"et": et, "s": tr.get("s"), "pnl": tr.get("pnl"), "mae": tr.get("mae"), "mfe": tr.get("mfe"), **feats}
        rows.append(row)
        print(row)

    df = pd.DataFrame(rows)
    if len(df) >= 4:
        from scipy.stats import spearmanr
        for col in ["hill_or_proxy", "top1pct_share", "herfindahl"]:
            sub = df.dropna(subset=[col, "mae"])
            if len(sub) >= 4:
                rho, p = spearmanr(sub[col], sub["mae"])
                print(f"spearman({col}, mae) rho={rho:.3f} p={p:.3f} n={len(sub)}")
            else:
                print(f"spearman({col}, mae): insufficient non-NaN n={len(sub)}")
    else:
        print(f"n_trades={len(df)} < 4 -- no correlation computed, raw pairs only (see rows above)")


if __name__ == "__main__":
    main()
