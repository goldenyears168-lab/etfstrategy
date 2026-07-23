#!/usr/bin/env python3
"""Yellow precursor × Green branch-consensus · same-day L0 entry sweep.

Research only. Goal: maximize chance that when expert-pool consensus
fires on T, a yellow chip precursor already lit on T−k (so we can enter
L0 same day as the branch consensus).

對照 lift 說明見報告開頭。
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.adapters_db import connect_ro, load_bench, load_calendar, load_ohlc  # noqa: E402
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402
from research.chen_chip.whale_events import TIER_A, load_whale_events  # noqa: E402

OUT = ROOT / "reports/research/whale_chip_precursor"
CACHE = OUT / "cache"
HS_PATH = CACHE / "holding_shares_per_tier_a.csv"
GOV_PATH = CACHE / "gov_bank_net_tier_a.csv"
D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
HOLD = 10


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


@dataclass(frozen=True)
class YellowSpec:
    name: str
    lag: int  # T-lag
    foreign_streak_min: int = 0
    three_streak_min: int = 0
    require_foreign_sum5: bool = False
    require_three_sum5: bool = False
    require_big_holder_chg: bool = False
    require_trust_pos: bool = False  # evaluated at T-1 if lag!=1
    require_lending_drop: bool = False
    trust_lag: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


def build_yellow_grid() -> list[YellowSpec]:
    """Thorough but pruned: frozen W-family + lag×streak×sum×holder×trust×lend."""
    specs: list[YellowSpec] = []

    def add(**kw) -> None:
        lag = kw["lag"]
        fs = kw.get("foreign_streak_min", 0)
        ts = kw.get("three_streak_min", 0)
        f5 = kw.get("require_foreign_sum5", False)
        t5 = kw.get("require_three_sum5", False)
        bh = kw.get("require_big_holder_chg", False)
        tr = kw.get("require_trust_pos", False)
        ld = kw.get("require_lending_drop", False)
        if fs == 0 and ts == 0 and not f5 and not t5 and not bh and not ld:
            return
        name = f"L{lag}_fs{fs}_ts{ts}_f5{int(f5)}_t5{int(t5)}_bh{int(bh)}_tr{int(tr)}_ld{int(ld)}"
        specs.append(
            YellowSpec(
                name=name,
                lag=lag,
                foreign_streak_min=fs,
                three_streak_min=ts,
                require_foreign_sum5=f5,
                require_three_sum5=t5,
                require_big_holder_chg=bh,
                require_trust_pos=tr,
                require_lending_drop=ld,
            )
        )

    # Frozen W1–W6 family across lags
    for lag in (1, 2, 3, 4, 5):
        add(lag=lag, three_streak_min=2)
        add(lag=lag, foreign_streak_min=2)
        add(lag=lag, require_foreign_sum5=True)
        add(lag=lag, require_three_sum5=True)
        add(lag=lag, require_foreign_sum5=True, three_streak_min=2)  # W3
        add(lag=lag, require_big_holder_chg=True)  # W5
        add(lag=lag, require_big_holder_chg=True, three_streak_min=2)  # W5b
        add(lag=lag, require_lending_drop=True)  # W6-ish
        add(lag=lag, three_streak_min=2, require_trust_pos=True)
        add(lag=lag, foreign_streak_min=2, require_trust_pos=True)
        add(lag=lag, require_foreign_sum5=True, three_streak_min=2, require_trust_pos=True)
        add(lag=lag, require_big_holder_chg=True, three_streak_min=2, require_trust_pos=True)

    # Full factorial on high-signal knobs (pruned streaks)
    for lag, fs, ts, f5, bh, tr in product(
        (1, 2, 3),
        (0, 2, 3),
        (0, 2, 3),
        (False, True),
        (False, True),
        (False, True),
    ):
        add(
            lag=lag,
            foreign_streak_min=fs,
            three_streak_min=ts,
            require_foreign_sum5=f5,
            require_big_holder_chg=bh,
            require_trust_pos=tr,
        )

    # Wide OR-like: any 5d sum positive
    for lag in (1, 2, 3, 4, 5):
        add(lag=lag, require_foreign_sum5=True, require_three_sum5=True)
        add(lag=lag, foreign_streak_min=1)
        add(lag=lag, three_streak_min=1)
        add(lag=lag, foreign_streak_min=1, three_streak_min=1)
        add(lag=lag, foreign_streak_min=1, three_streak_min=1, require_foreign_sum5=True)

    seen: set[str] = set()
    out: list[YellowSpec] = []
    for s in specs:
        if s.name in seen:
            continue
        seen.add(s.name)
        out.append(s)
    return out


def l0_excess(
    sid: str,
    T: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
) -> float | None:
    i = di.get(T)
    if i is None or i + hold >= len(cal):
        return None
    er = ohlc_px.get((sid, T))
    xr = ohlc_px.get((sid, cal[i + hold]))
    be, bx = bench.get(T), bench.get(cal[i + hold])
    if not er or not xr or not be or not bx:
        return None
    _, ep = er
    _, xp = xr
    if ep <= 0 or xp <= 0 or be <= 0 or bx <= 0:
        return None
    sr = xp / ep - 1.0 - COST
    br = bx / be - 1.0
    return (sr - BETA * br) * 100


def _shift_forward(arr: np.ndarray, lag: int) -> np.ndarray:
    """Value at day i comes from day i-lag (precursor → T)."""
    if lag <= 0:
        return arr.copy()
    out = np.zeros_like(arr)
    out[:, lag:] = arr[:, :-lag]
    return out


def _future_or(whale_m: np.ndarray, W: int) -> np.ndarray:
    """True at T if whale fires on any of [T, T+W-1]."""
    out = whale_m.copy()
    for w in range(1, W):
        padded = np.zeros_like(whale_m)
        padded[:, :-w] = whale_m[:, w:]
        out |= padded
    return out


def _dense_feat_arrays(
    feat: pd.DataFrame, sids: list[str], cal: list[str]
) -> dict[str, np.ndarray]:
    """(n_sid, n_day) float arrays for chip columns."""
    n_s, n_d = len(sids), len(cal)
    sid_ix = {s: i for i, s in enumerate(sids)}
    di = {d: i for i, d in enumerate(cal)}
    cols = [
        "foreign_streak",
        "three_streak",
        "foreign_sum_5d",
        "three_sum_5d",
        "big_holder_pct_chg",
        "trust_net",
        "lending_change",
    ]
    arrays = {c: np.full((n_s, n_d), np.nan, dtype=np.float64) for c in cols}
    for r in feat.itertuples(index=False):
        s = str(r.sid)
        d = str(r.d)
        if s not in sid_ix or d not in di:
            continue
        i, j = sid_ix[s], di[d]
        for c in cols:
            v = getattr(r, c, np.nan)
            try:
                arrays[c][i, j] = float(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else np.nan
            except (TypeError, ValueError):
                arrays[c][i, j] = np.nan
    return arrays


def yellow_chip_mask(spec: YellowSpec, arr: dict[str, np.ndarray]) -> np.ndarray:
    """Boolean (n_sid, n_day): chip conditions on precursor day (no lag/trust)."""
    shape = arr["foreign_streak"].shape
    m = np.ones(shape, dtype=bool)
    if spec.foreign_streak_min:
        m &= np.nan_to_num(arr["foreign_streak"], nan=0.0) >= spec.foreign_streak_min
    if spec.three_streak_min:
        m &= np.nan_to_num(arr["three_streak"], nan=0.0) >= spec.three_streak_min
    if spec.require_foreign_sum5:
        m &= np.nan_to_num(arr["foreign_sum_5d"], nan=0.0) > 0
    if spec.require_three_sum5:
        m &= np.nan_to_num(arr["three_sum_5d"], nan=0.0) > 0
    if spec.require_big_holder_chg:
        chg = arr["big_holder_pct_chg"]
        m &= np.isfinite(chg) & (chg > 0)
    if spec.require_lending_drop:
        m &= np.nan_to_num(arr["lending_change"], nan=0.0) < 0
    return m


def eval_spec(
    spec: YellowSpec,
    arr: dict[str, np.ndarray],
    whale_m: np.ndarray,
    day_mask: np.ndarray,
    sids: list[str],
    sid_ix: dict[str, int],
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
    whale_list: list[tuple[str, str]],
) -> dict:
    yc = yellow_chip_mask(spec, arr)
    trust_pos = np.nan_to_num(arr["trust_net"], nan=0.0) > 0
    tl = spec.trust_lag

    def ready_at_lag(lag: int) -> np.ndarray:
        y_at_t = _shift_forward(yc, lag)
        if spec.require_trust_pos:
            tr = _shift_forward(trust_pos, tl)
            # need enough history for trust_lag
            ok = np.zeros_like(y_at_t)
            if tl > 0:
                ok[:, tl:] = True
            else:
                ok[:] = True
            return y_at_t & tr & ok
        return y_at_t

    ready_fixed = ready_at_lag(spec.lag)
    ready_win = np.zeros_like(yc)
    for k in range(1, 6):
        ready_win |= ready_at_lag(k)

    whales_split = whale_m & day_mask[None, :]
    n_whales = int(whales_split.sum())
    if n_whales == 0:
        return {
            **spec.to_dict(),
            "whale_n": 0,
            "yellow_fire_n": 0,
            "cov_fixed": 0,
            "cov_win5": 0,
            "recall_fixed": 0.0,
            "recall_win5": 0.0,
            "same_day_conversion": 0.0,
            "conv_w2": 0.0,
            "conv_w3": 0.0,
            "precision_yellow_to_T": 0.0,
            "hit_l0h10_n": 0,
            "hit_l0h10_med": np.nan,
            "score": 0.0,
        }

    cov_fixed = int((ready_fixed & whales_split).sum())
    cov_win = int((ready_win & whales_split).sum())
    rec_fixed = cov_fixed / n_whales
    rec_win = cov_win / n_whales

    # Yellow on day d → T=d+lag; both d and T in split
    lag = spec.lag
    y_at_t = _shift_forward(yc, lag)
    if spec.require_trust_pos:
        tr = _shift_forward(trust_pos, tl)
        ok_hist = np.zeros_like(y_at_t)
        if tl > 0:
            ok_hist[:, tl:] = True
        else:
            ok_hist[:] = True
        y_at_t = y_at_t & tr & ok_hist
    d_in = np.zeros(len(cal), dtype=bool)
    if lag > 0:
        d_in[lag:] = day_mask[:-lag]
    else:
        d_in[:] = day_mask
    fire = y_at_t & day_mask[None, :] & d_in[None, :]
    wait_n = int(fire.sum())
    tp = int((fire & whale_m).sum())
    prec = tp / wait_n if wait_n else 0.0
    same_day_conv = prec  # W=1 by construction
    conv_w2 = int((fire & _future_or(whale_m, 2)).sum()) / wait_n if wait_n else 0.0
    conv_w3 = int((fire & _future_or(whale_m, 3)).sum()) / wait_n if wait_n else 0.0

    xs: list[float] = []
    for s, t in whale_list:
        i = di.get(t)
        si = sid_ix.get(s)
        if i is None or si is None:
            continue
        if not ready_fixed[si, i]:
            continue
        x = l0_excess(s, t, HOLD, cal, di, ohlc_px, bench)
        if x is not None:
            xs.append(x)
    med = float(np.median(xs)) if len(xs) >= 5 else np.nan

    score = 100.0 * rec_win + 50.0 * rec_fixed + 30.0 * same_day_conv + 15.0 * prec
    if not np.isnan(med):
        score += 10.0 * min(max(med, 0), 12) / 12

    return {
        **spec.to_dict(),
        "whale_n": n_whales,
        "yellow_fire_n": wait_n,
        "cov_fixed": cov_fixed,
        "cov_win5": cov_win,
        "recall_fixed": rec_fixed,
        "recall_win5": rec_win,
        "same_day_conversion": same_day_conv,
        "conv_w2": conv_w2,
        "conv_w3": conv_w3,
        "precision_yellow_to_T": prec,
        "hit_l0h10_n": len(xs),
        "hit_l0h10_med": med,
        "score": score,
    }


def main() -> int:
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    if not HS_PATH.exists() or not GOV_PATH.exists():
        log("cache missing — building via enriched backfill helpers")
        # Import lazily; mirrors run_whale_chip_precursor_enriched.py
        sys.path.insert(0, str(ROOT / "scripts" / "research"))
        from run_whale_chip_precursor_enriched import (  # type: ignore
            backfill_gov_bank,
            backfill_holding_shares,
        )

        conn0 = connect_ro()
        cal0 = load_calendar(conn0, D0, D1)
        conn0.close()
        if not HS_PATH.exists():
            backfill_holding_shares(TIER_A, D0, D1)
        if not GOV_PATH.exists():
            backfill_gov_bank(TIER_A, [d for d in cal0 if D0 <= d <= D1])

    log("load data (enriched feature frame + caches)")
    conn = connect_ro()
    whale = load_whale_events(TIER_A)
    whale = whale[(whale["signal_date"] >= D0) & (whale["signal_date"] <= D1)].copy()
    feat = build_chip_feature_frame(
        conn,
        TIER_A,
        D0,
        D1,
        holding_shares_path=HS_PATH if HS_PATH.exists() else None,
        gov_bank_path=GOV_PATH if GOV_PATH.exists() else None,
    )
    if "lending_change" not in feat.columns:
        feat["lending_change"] = np.nan
    cal = load_calendar(conn, D0, D1)
    di = {d: i for i, d in enumerate(cal)}
    bench = load_bench(conn, D0, D1)
    ohlc = load_ohlc(conn, TIER_A, D0, D1)
    conn.close()
    ohlc_px = {(r.sid, r.d): (float(r.open), float(r.close)) for r in ohlc.itertuples()}
    whale = whale[whale["signal_date"].isin(di)].copy()
    whale_set = set(zip(whale["sid"].astype(str), whale["signal_date"].astype(str)))

    log(f"feat rows={len(feat)} cols={len(feat.columns)} whale={len(whale)}")
    log("densify feature arrays")
    arr = _dense_feat_arrays(feat, TIER_A, cal)
    n_s, n_d = len(TIER_A), len(cal)
    whale_m = np.zeros((n_s, n_d), dtype=bool)
    sid_ix = {s: i for i, s in enumerate(TIER_A)}
    for s, t in whale_set:
        if s in sid_ix and t in di:
            whale_m[sid_ix[s], di[t]] = True
    is_day = np.array([d < OOS for d in cal], dtype=bool)
    oos_day = ~is_day

    grid = build_yellow_grid()
    log(f"yellow grid size={len(grid)}")

    rows: list[dict] = []
    for wi, spec in enumerate(grid):
        if (wi + 1) % 50 == 0 or wi == 0:
            log(f"  sweep {wi+1}/{len(grid)}")
        for split, day_mask in (("IS", is_day), ("OOS", oos_day)):
            whale_list = [(s, t) for s, t in whale_set if (t < OOS) == (split == "IS")]
            r = eval_spec(
                spec,
                arr,
                whale_m,
                day_mask,
                TIER_A,
                sid_ix,
                cal,
                di,
                ohlc_px,
                bench,
                whale_list,
            )
            r["split"] = split
            rows.append(r)

    rdf = pd.DataFrame(rows)
    rdf.to_csv(OUT / "yellow_green_sameday_sweep.csv", index=False)

    is_df = rdf[rdf.split == "IS"].sort_values("score", ascending=False)
    oos_df = rdf[rdf.split == "OOS"]

    picks = []
    for label, mask in [
        ("max_score", is_df.head(1).index),
        ("max_recall_win5", is_df.sort_values("recall_win5", ascending=False).head(1).index),
        ("max_recall_fixed", is_df.sort_values("recall_fixed", ascending=False).head(1).index),
        (
            "balanced_R0.5_P",
            is_df[is_df.recall_win5 >= 0.5]
            .sort_values(["precision_yellow_to_T", "recall_win5"], ascending=False)
            .head(1)
            .index
            if len(is_df[is_df.recall_win5 >= 0.5])
            else is_df.head(1).index,
        ),
        (
            "high_R_fixed_ge0.45",
            is_df[is_df.recall_fixed >= 0.45]
            .sort_values("score", ascending=False)
            .head(1)
            .index
            if len(is_df[is_df.recall_fixed >= 0.45])
            else is_df.head(1).index,
        ),
    ]:
        if len(mask) == 0:
            continue
        r = is_df.loc[mask[0]]
        o = oos_df[oos_df.name == r["name"]]
        picks.append({"pick": label, "is": r.to_dict(), "oos": o.iloc[0].to_dict() if len(o) else {}})

    (OUT / "yellow_green_picks.json").write_text(
        json.dumps(picks, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    lines = []
    lines.append("# 黃燈前兆 × 綠燈分點共識 · 同日進場掃參\n\n")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}\n")
    lines.append(f"- Tier A 事件 n={len(whale)} · OOS≥{OOS} · 進場 **L0**（與共識同一日收盤）\n\n")

    lines.append("## 「對照 lift」是什麼？\n\n")
    lines.append("在前兆研究裡：\n\n")
    lines.append("$$\n")
    lines.append(
        r"\mathrm{lift} = \frac{P(\text{條件成立}\mid\text{大戶事件日前 }T{-}k)}"
        r"{P(\text{條件成立}\mid\text{同股安靜對照日})}" + "\n"
    )
    lines.append("$$\n\n")
    lines.append("- **lift = 1.64** 表示：事件前出現該條件的機率，是安靜日的 **1.64 倍**。\n")
    lines.append("- 不是報酬倍數，也不是勝率；只是「相對對照日有多異常地常出現」。\n")
    lines.append("- 例：事件日 37%、安靜日 22% → lift = 37/22 ≈ **1.68**（BRIEFING 頂部 `L2:three_streak≥2` 為 **1.64**）。\n\n")

    lines.append("## 本輪設計\n\n")
    lines.append("| 燈 | 意義 |\n|----|------|\n")
    lines.append("| **黃燈** | 籌碼前兆（掃參：lag、連買天數、五日累計、集保週增、投信、借券） |\n")
    lines.append("| **綠燈** | expert-pool 分點共識訊號日（既有冠軍規則事件） |\n")
    lines.append("| **進場** | 綠燈當日 L0 收盤（與分點同一天） |\n\n")
    lines.append("核心指標：\n\n")
    lines.append("- **recall_win5**：綠燈日之前 1–5 日內是否曾亮黃燈（「有機會同日跟上」的覆蓋率）\n")
    lines.append("- **recall_fixed**：剛好在指定 T−lag 亮黃燈\n")
    lines.append("- **same_day_conversion**：黃燈對應到的 T，當天就是綠燈的比例\n")
    lines.append("- **precision_yellow_to_T**：黃燈→對應 T 為綠燈的精確度（防過寬）\n")
    lines.append("- **hit_l0h10_med**：黃∧綠 的 L0H10 超額中位\n\n")
    lines.append(f"掃參組合數：{len(grid)} × IS/OOS\n\n")

    lines.append("## IS Top（依 score）\n\n")
    lines.append("| name | R_win5 | R_fixed | sameDayConv | P | L0H10 med | score |\n")
    lines.append("|------|-------:|--------:|------------:|--:|----------:|------:|\n")
    for r in is_df.head(15).itertuples():
        med = f"{r.hit_l0h10_med:+.1f}" if pd.notna(r.hit_l0h10_med) else "—"
        lines.append(
            f"| `{r.name}` | {r.recall_win5:.0%} | {r.recall_fixed:.0%} | "
            f"{r.same_day_conversion:.1%} | {r.precision_yellow_to_T:.1%} | {med} | {r.score:.1f} |\n"
        )

    lines.append("\n## 凍結候選（IS 選、附 OOS）\n\n")
    for p in picks:
        iso, ooso = p["is"], p["oos"]
        lines.append(f"### {p['pick']}\n\n")
        lines.append(f"- 規則：`{iso.get('name')}`\n")
        lines.append(
            f"- IS：R_win5={iso.get('recall_win5',0):.0%} · R_fixed={iso.get('recall_fixed',0):.0%} · "
            f"sameDayConv={iso.get('same_day_conversion',0):.1%} · P={iso.get('precision_yellow_to_T',0):.1%}\n"
        )
        if ooso:
            med_o = ooso.get("hit_l0h10_med")
            med_s = f"{float(med_o):+.1f}" if med_o is not None and pd.notna(med_o) else "—"
            lines.append(
                f"- OOS：R_win5={ooso.get('recall_win5',0):.0%} · R_fixed={ooso.get('recall_fixed',0):.0%} · "
                f"sameDayConv={ooso.get('same_day_conversion',0):.1%} · P={ooso.get('precision_yellow_to_T',0):.1%} · "
                f"L0H10 med={med_s}\n\n"
            )
        else:
            lines.append("- OOS：無\n\n")

    best = is_df.iloc[0]
    lines.append("## 建議用法（Research）\n\n")
    lines.append("```\n")
    lines.append("黃燈：籌碼前兆（見凍結候選）\n")
    lines.append("綠燈：分點共識（expert-pool 冠軍規則）同一天觸發\n")
    lines.append("進場：僅綠燈日 L0；無綠燈不開倉\n")
    lines.append("```\n\n")
    lines.append(
        f"若目標是「綠燈來時多半已亮過黃燈」：看 **recall_win5** 最高者"
        f"（IS 頂部約 {float(is_df.iloc[0].recall_win5):.0%}）。\n"
    )
    lines.append(
        "若目標是「黃燈對應日當天就是綠燈」：看 **same_day_conversion**／**precision**"
        "（通常較低，需容忍漏報或加嚴）。\n"
    )

    (OUT / "YELLOW_GREEN_SAMEDAY.md").write_text("".join(lines), encoding="utf-8")
    status = {
        "phase": "done",
        "at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_min": round((time.time() - t0) / 60, 2),
        "grid": len(grid),
        "best_is_name": str(best["name"]),
        "best_is_recall_win5": float(best["recall_win5"]),
    }
    (OUT / "status_yellow_green.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"DONE best={best['name']} R_win5={best['recall_win5']:.2f} → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
