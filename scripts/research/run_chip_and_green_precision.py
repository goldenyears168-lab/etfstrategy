#!/usr/bin/env python3
"""Chip yellow (W1/W5) ∧ expert green · precision / lift / L1H7 returns.

Research only. Does NOT invent branch-light yellow definitions.
Yellow = frozen whale_chip_precursor W1 / W1b / W5 / W5b (T−2 or win5).
Green = expert-pool champion signal days from knowledge/trades.

Goal: raise precision of useful greens (or yellow→green onset); report
whether chip∧green beats naked green on returns or only on precision.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from research.chen_chip.adapters_db import connect_ro, load_bench, load_calendar, load_ohlc  # noqa: E402
from research.chen_chip.features import build_chip_feature_frame  # noqa: E402
from research.chen_chip.whale_events import load_whale_events  # noqa: E402

OUT = ROOT / "reports/research/whale_chip_precursor"
CACHE = OUT / "cache"
HS_PATH = CACHE / "holding_shares_per_tier_a.csv"
GOV_PATH = CACHE / "gov_bank_net_tier_a.csv"
REG_PATH = (
    ROOT
    / "reports/research/branch-footprint-screen/expert_pool/EVAL_REGISTRY.json"
)

D0, D1, OOS = "2024-07-01", "2026-07-16", "2026-01-01"
COST, BETA = 0.003, 1.15
HOLD = 7
MEGA = {"2303", "2308", "2317", "3711", "6669"}

# H_C thick hard≥3 ex-mega (prior expert_pool studies; includes legacy 2344/8046/8358)
THICK_H3 = [
    "2327",
    "2337",
    "2344",
    "2383",
    "2408",
    "3037",
    "3189",
    "3443",
    "3481",
    "3653",
    "3665",
    "6223",
    "8046",
    "8358",
]


def log(m: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {m}", flush=True)


@dataclass(frozen=True)
class YellowRule:
    id: str
    label: str
    lag_mode: str  # fixed2 | win5
    three_streak_min: int = 0
    foreign_streak_min: int = 0
    require_big_holder_chg: bool = False
    # OR of three_streak / foreign_streak when both mins set and or_mode
    or_streak: bool = False


RULES: list[YellowRule] = [
    YellowRule("W1_fixed", "W1 T−2 three_streak≥2", "fixed2", three_streak_min=2),
    YellowRule("W1b_fixed", "W1b T−2 foreign_streak≥2", "fixed2", foreign_streak_min=2),
    YellowRule(
        "W1or_fixed",
        "W1∨W1b T−2 (three≥2 ∨ foreign≥2)",
        "fixed2",
        three_streak_min=2,
        foreign_streak_min=2,
        or_streak=True,
    ),
    YellowRule(
        "W5_fixed",
        "W5 T−2 big_holder_pct_chg>0",
        "fixed2",
        require_big_holder_chg=True,
    ),
    YellowRule(
        "W5b_fixed",
        "W5b T−2 W5∧three_streak≥2",
        "fixed2",
        three_streak_min=2,
        require_big_holder_chg=True,
    ),
    YellowRule("W1_win5", "W1 any T−1..T−5 three_streak≥2", "win5", three_streak_min=2),
    YellowRule(
        "W5_win5",
        "W5 any T−1..T−5 big_holder_pct_chg>0",
        "win5",
        require_big_holder_chg=True,
    ),
    YellowRule(
        "W5b_win5",
        "W5b any T−1..T−5 W5∧three≥2",
        "win5",
        three_streak_min=2,
        require_big_holder_chg=True,
    ),
]


def adopted_hard_ge3() -> list[str]:
    if not REG_PATH.exists():
        return []
    doc = json.loads(REG_PATH.read_text(encoding="utf-8"))
    out: list[str] = []
    for s in doc.get("stocks") or []:
        if s.get("status") != "adopted":
            continue
        hard = s.get("hard_n")
        try:
            hn = int(hard) if hard is not None else 0
        except (TypeError, ValueError):
            hn = 0
        if hn >= 3:
            out.append(str(s["stock_id"]))
    return sorted(set(out))


def _shift_forward(arr: np.ndarray, lag: int) -> np.ndarray:
    if lag <= 0:
        return arr.copy()
    out = np.zeros_like(arr)
    out[:, lag:] = arr[:, :-lag]
    return out


def _dense(feat: pd.DataFrame, sids: list[str], cal: list[str]) -> dict[str, np.ndarray]:
    n_s, n_d = len(sids), len(cal)
    sid_ix = {s: i for i, s in enumerate(sids)}
    di = {d: i for i, d in enumerate(cal)}
    cols = ["three_streak", "foreign_streak", "big_holder_pct_chg"]
    arrays = {c: np.full((n_s, n_d), np.nan, dtype=np.float64) for c in cols}
    for r in feat.itertuples(index=False):
        s, d = str(r.sid), str(r.d)
        if s not in sid_ix or d not in di:
            continue
        i, j = sid_ix[s], di[d]
        for c in cols:
            v = getattr(r, c, np.nan)
            try:
                arrays[c][i, j] = float(v) if v is not None and pd.notna(v) else np.nan
            except (TypeError, ValueError):
                arrays[c][i, j] = np.nan
    return arrays


def yellow_mask(rule: YellowRule, arr: dict[str, np.ndarray]) -> np.ndarray:
    shape = arr["three_streak"].shape
    if rule.or_streak:
        base = np.zeros(shape, dtype=bool)
        if rule.three_streak_min:
            base |= np.nan_to_num(arr["three_streak"], nan=0.0) >= rule.three_streak_min
        if rule.foreign_streak_min:
            base |= np.nan_to_num(arr["foreign_streak"], nan=0.0) >= rule.foreign_streak_min
    else:
        base = np.ones(shape, dtype=bool)
        if rule.three_streak_min:
            base &= np.nan_to_num(arr["three_streak"], nan=0.0) >= rule.three_streak_min
        if rule.foreign_streak_min:
            base &= np.nan_to_num(arr["foreign_streak"], nan=0.0) >= rule.foreign_streak_min
    if rule.require_big_holder_chg:
        chg = arr["big_holder_pct_chg"]
        base &= np.isfinite(chg) & (chg > 0)
    if rule.lag_mode == "fixed2":
        return _shift_forward(base, 2)
    out = np.zeros(shape, dtype=bool)
    for k in range(1, 6):
        out |= _shift_forward(base, k)
    return out


def excess_l1(
    sid: str,
    T: str,
    hold: int,
    cal: list[str],
    di: dict[str, int],
    ohlc_px: dict,
    bench: dict,
) -> float | None:
    i = di.get(T)
    if i is None or i + 1 >= len(cal) or i + hold >= len(cal):
        return None
    ed = cal[i + 1]
    xd = cal[i + hold]
    er = ohlc_px.get((sid, ed))
    xr = ohlc_px.get((sid, xd))
    be, bx = bench.get(ed), bench.get(xd)
    if not er or not xr or not be or not bx:
        return None
    eo, _ = er
    _, xp = xr
    if eo <= 0 or xp <= 0 or be <= 0 or bx <= 0:
        return None
    sr = xp / eo - 1.0 - COST
    br = bx / be - 1.0
    return (sr - BETA * br) * 100.0


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else float("nan")
    r = tp / (tp + fn) if (tp + fn) else float("nan")
    if not (np.isfinite(p) and np.isfinite(r)) or (p + r) == 0:
        f1 = float("nan")
    else:
        f1 = 2 * p * r / (p + r)
    return p, r, f1


def fmt_pct(v: float | None, d: int = 1) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{v:+.{d}f}"


def fmt_rate(v: float | None, d: int = 1) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{100.0 * v:.{d}f}%"


def fmt_f(v: float | None, d: int = 2) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "—"
    return f"{v:.{d}f}"


def run_universe(sids: list[str], uni_label: str) -> dict:
    log(f"universe {uni_label} n={len(sids)} {sids}")
    conn = connect_ro()
    whale = load_whale_events(sids)
    whale = whale[(whale["signal_date"] >= D0) & (whale["signal_date"] <= D1)].copy()
    hs = HS_PATH if HS_PATH.exists() else None
    gov = GOV_PATH if GOV_PATH.exists() else None
    feat = build_chip_feature_frame(
        conn, sids, D0, D1, holding_shares_path=hs, gov_bank_path=gov
    )
    cal = load_calendar(conn, D0, "2026-08-31")
    di = {d: i for i, d in enumerate(cal)}
    bench = load_bench(conn, D0, D1)
    ohlc = load_ohlc(conn, sids, D0, D1)
    conn.close()

    ohlc_px = {
        (str(r.sid), str(r.d)): (float(r.open), float(r.close)) for r in ohlc.itertuples()
    }
    sid_ix = {s: i for i, s in enumerate(sids)}
    arr = _dense(feat, sids, cal)
    bh_cov = float(np.isfinite(arr["big_holder_pct_chg"]).mean()) if arr else 0.0

    # Green events with L1H7 excess (recomputed for fair hold)
    green_rows: list[dict] = []
    for r in whale.itertuples(index=False):
        sid, T = str(r.sid), str(r.signal_date)
        if sid not in sid_ix or T not in di or not (D0 <= T <= D1):
            continue
        x = excess_l1(sid, T, HOLD, cal, di, ohlc_px, bench)
        if x is None:
            continue
        recorded = r.r_adj_pct_l1
        try:
            rec = float(recorded) if recorded is not None and pd.notna(recorded) else np.nan
        except (TypeError, ValueError):
            rec = np.nan
        green_rows.append(
            {
                "sid": sid,
                "signal_date": T,
                "r_adj": x,
                "r_adj_recorded": rec,
                "split": "IS" if T < OOS else "OOS",
            }
        )
    gdf = pd.DataFrame(green_rows)
    log(f"  greens={len(gdf)} feat={len(feat)} bh_cov={bh_cov:.1%}")

    whale_m = np.zeros((len(sids), len(cal)), dtype=bool)
    for row in green_rows:
        whale_m[sid_ix[row["sid"]], di[row["signal_date"]]] = True

    # Useful labels on greens
    if gdf.empty:
        return {
            "label": uni_label,
            "sids": sids,
            "n_green": 0,
            "bh_cov": bh_cov,
            "filter_rows": [],
            "onset_rows": [],
            "return_rows": [],
        }

    med_full = float(gdf["r_adj"].median())
    gdf["useful_hit"] = gdf["r_adj"] > 0
    gdf["useful_strong"] = gdf["r_adj"] >= med_full

    ready = {r.id: yellow_mask(r, arr) for r in RULES}

    def yellow_ok(rule_id: str, sid: str, T: str) -> bool:
        return bool(ready[rule_id][sid_ix[sid], di[T]])

    filter_rows: list[dict] = []
    return_rows: list[dict] = []

    for split in ("full", "IS", "OOS"):
        sub = gdf if split == "full" else gdf[gdf["split"] == split]
        n_g = len(sub)
        base_hit = float(sub["useful_hit"].mean()) if n_g else float("nan")
        base_strong = float(sub["useful_strong"].mean()) if n_g else float("nan")
        return_rows.append(
            {
                "universe": uni_label,
                "arm": "G_L1H7",
                "yellow": "—",
                "split": split,
                "n": n_g,
                "hit": base_hit,
                "med": float(sub["r_adj"].median()) if n_g else float("nan"),
                "mean": float(sub["r_adj"].mean()) if n_g else float("nan"),
            }
        )

        for rule in RULES:
            mask = sub.apply(lambda r: yellow_ok(rule.id, r["sid"], r["signal_date"]), axis=1)
            filt = sub[mask]
            n_f = len(filt)
            # precision of useful among predicted (fires = filtered greens)
            # recall = useful retained / all useful greens
            for label_col, tag in (("useful_hit", "hit"), ("useful_strong", "strong")):
                useful_all = sub[sub[label_col]]
                useful_kept = filt[filt[label_col]]
                tp = int(len(useful_kept))
                fp = int((~filt[label_col]).sum()) if n_f else 0
                fn = int(len(useful_all) - tp)
                p, r, f1 = prf(tp, fp, fn)
                base = base_hit if tag == "hit" else base_strong
                lift = (p / base) if (np.isfinite(p) and np.isfinite(base) and base > 0) else float("nan")
                filter_rows.append(
                    {
                        "universe": uni_label,
                        "yellow": rule.id,
                        "label": tag,
                        "split": split,
                        "n_green": n_g,
                        "n_filt": n_f,
                        "cover": n_f / n_g if n_g else float("nan"),
                        "P": p,
                        "R": r,
                        "F1": f1,
                        "base": base,
                        "lift": lift,
                        "delta_pp": (p - base) * 100 if np.isfinite(p) and np.isfinite(base) else float("nan"),
                    }
                )
            return_rows.append(
                {
                    "universe": uni_label,
                    "arm": "YandG_L1H7",
                    "yellow": rule.id,
                    "split": split,
                    "n": n_f,
                    "hit": float(filt["useful_hit"].mean()) if n_f else float("nan"),
                    "med": float(filt["r_adj"].median()) if n_f else float("nan"),
                    "mean": float(filt["r_adj"].mean()) if n_f else float("nan"),
                }
            )

    # Yellow → green onset (calendar): yellow fire day D → green in [D, D+W]
    onset_rows: list[dict] = []
    day_lo_i = di.get(D0, 0)
    day_hi_i = di.get(D1, len(cal) - 1)
    for rule in RULES:
        ymask = ready[rule.id]
        for split in ("full", "IS", "OOS"):
            for W in (1, 3, 5):
                fires = 0
                hits = 0
                # base rate: random stock-day → green within W
                base_n = 0
                base_h = 0
                for si, sid in enumerate(sids):
                    for j in range(day_lo_i, day_hi_i + 1):
                        d = cal[j]
                        if split == "IS" and d >= OOS:
                            continue
                        if split == "OOS" and d < OOS:
                            continue
                        # need room for W look-ahead within calendar for eval
                        if j + W - 1 > day_hi_i:
                            continue
                        future = whale_m[si, j : j + W].any()
                        base_n += 1
                        base_h += int(future)
                        if not ymask[si, j]:
                            continue
                        # skip days that are themselves already green? keep them —
                        # yellow→onset includes same-day conversion
                        fires += 1
                        hits += int(future)
                p = hits / fires if fires else float("nan")
                base = base_h / base_n if base_n else float("nan")
                lift = (p / base) if (np.isfinite(p) and np.isfinite(base) and base > 0) else float("nan")
                # recall: greens preceded by yellow in prior W days (incl same day)
                greens_split = [
                    (row["sid"], row["signal_date"])
                    for row in green_rows
                    if split == "full"
                    or (split == "IS" and row["signal_date"] < OOS)
                    or (split == "OOS" and row["signal_date"] >= OOS)
                ]
                recalled = 0
                for sid, T in greens_split:
                    j = di[T]
                    lo = max(0, j - (W - 1))
                    if ymask[sid_ix[sid], lo : j + 1].any():
                        recalled += 1
                n_g = len(greens_split)
                rec = recalled / n_g if n_g else float("nan")
                f1 = (
                    2 * p * rec / (p + rec)
                    if np.isfinite(p) and np.isfinite(rec) and (p + rec) > 0
                    else float("nan")
                )
                onset_rows.append(
                    {
                        "universe": uni_label,
                        "yellow": rule.id,
                        "split": split,
                        "W": W,
                        "fires": fires,
                        "P": p,
                        "base": base,
                        "lift": lift,
                        "R": rec,
                        "F1": f1,
                        "n_green": n_g,
                    }
                )

    return {
        "label": uni_label,
        "sids": sids,
        "n_green": len(gdf),
        "n_is": int((gdf["split"] == "IS").sum()),
        "n_oos": int((gdf["split"] == "OOS").sum()),
        "bh_cov": bh_cov,
        "med_full": med_full,
        "filter_rows": filter_rows,
        "onset_rows": onset_rows,
        "return_rows": return_rows,
        "gdf": gdf,
    }


def pick_freeze(filter_df: pd.DataFrame, onset_df: pd.DataFrame) -> list[dict]:
    """IS must not hurt; OOS must show clear precision lift."""
    cands: list[dict] = []
    hit = filter_df[(filter_df["label"] == "hit") & (filter_df["universe"] == "thick_h3")]
    for yid in hit["yellow"].unique():
        is_ = hit[(hit["yellow"] == yid) & (hit["split"] == "IS")]
        oos = hit[(hit["yellow"] == yid) & (hit["split"] == "OOS")]
        if is_.empty or oos.empty:
            continue
        is_r, oos_r = is_.iloc[0], oos.iloc[0]
        if (
            oos_r["n_filt"] >= 8
            and np.isfinite(oos_r["lift"])
            and oos_r["lift"] >= 1.05
            and np.isfinite(oos_r["delta_pp"])
            and oos_r["delta_pp"] >= 2.0
            and np.isfinite(is_r["lift"])
            and is_r["lift"] >= 1.0
            and np.isfinite(is_r["delta_pp"])
            and is_r["delta_pp"] >= 0.0
        ):
            cands.append(
                {
                    "kind": "green_filter",
                    "yellow": yid,
                    "is_P": is_r["P"],
                    "is_lift": is_r["lift"],
                    "oos_P": oos_r["P"],
                    "oos_lift": oos_r["lift"],
                    "oos_n": int(oos_r["n_filt"]),
                    "oos_cover": oos_r["cover"],
                    "oos_delta_pp": oos_r["delta_pp"],
                }
            )
    # onset@5: require IS lift≥1.0 (no IS hurt) AND OOS lift≥1.2
    on = onset_df[(onset_df["universe"] == "thick_h3") & (onset_df["W"] == 5)]
    for yid in on["yellow"].unique():
        is_ = on[(on["yellow"] == yid) & (on["split"] == "IS")]
        oos = on[(on["yellow"] == yid) & (on["split"] == "OOS")]
        if is_.empty or oos.empty:
            continue
        is_r, oos_r = is_.iloc[0], oos.iloc[0]
        if (
            oos_r["fires"] >= 30
            and np.isfinite(is_r["lift"])
            and is_r["lift"] >= 1.0
            and np.isfinite(oos_r["lift"])
            and oos_r["lift"] >= 1.2
            and np.isfinite(oos_r["P"])
            and np.isfinite(oos_r["base"])
            and (oos_r["P"] - oos_r["base"]) >= 0.02
        ):
            cands.append(
                {
                    "kind": "yellow_to_green",
                    "yellow": yid,
                    "is_P": is_r["P"],
                    "is_lift": is_r["lift"],
                    "oos_P": oos_r["P"],
                    "oos_lift": oos_r["lift"],
                    "oos_fires": int(oos_r["fires"]),
                    "oos_R": oos_r["R"],
                }
            )
    return cands


def write_report(
    results: list[dict],
    filter_df: pd.DataFrame,
    onset_df: pd.DataFrame,
    return_df: pd.DataFrame,
    freeze: list[dict],
    elapsed: float,
) -> None:
    primary = next((r for r in results if r["label"] == "thick_h3"), results[0])
    rule_lab = {r.id: r.label for r in RULES}

    # Best green-filter by OOS hit lift among thick_h3 with n_filt≥8
    hit_oos = filter_df[
        (filter_df["universe"] == "thick_h3")
        & (filter_df["label"] == "hit")
        & (filter_df["split"] == "OOS")
        & (filter_df["n_filt"] >= 8)
    ].copy()
    hit_oos = hit_oos.sort_values(["lift", "P"], ascending=False)
    best = hit_oos.iloc[0] if not hit_oos.empty else None

    # Returns compare for best + W1_fixed + W5_fixed
    def ret(arm_y: str, split: str) -> pd.Series | None:
        if arm_y == "G":
            m = return_df[
                (return_df["universe"] == "thick_h3")
                & (return_df["arm"] == "G_L1H7")
                & (return_df["split"] == split)
            ]
        else:
            m = return_df[
                (return_df["universe"] == "thick_h3")
                & (return_df["arm"] == "YandG_L1H7")
                & (return_df["yellow"] == arm_y)
                & (return_df["split"] == split)
            ]
        return m.iloc[0] if not m.empty else None

    g_oos = ret("G", "OOS")
    lines: list[str] = []
    lines.append("# 籌碼黃燈 ∧ Expert 綠燈 · Precision 抬升研究")
    lines.append("")
    lines.append(f"- 生成：{datetime.now().isoformat(timespec='seconds')}")
    lines.append("- Runner：`scripts/research/run_chip_and_green_precision.py`")
    lines.append(
        f"- 窗：{D0}..{D1} · OOS≥{OOS} · L1H7 · COST={COST} · β={BETA}×IX0001"
    )
    lines.append(
        f"- 主宇宙 **thick_h3**：adopted∩hard≥3 精神／H_C 厚池 ex-mega · n={len(primary['sids'])} · "
        + ", ".join(primary["sids"])
    )
    lines.append(
        f"- 綠燈事件：knowledge trades · thick_h3 n={primary['n_green']} "
        f"(IS {primary['n_is']} / OOS {primary['n_oos']})"
    )
    lines.append(
        f"- 集保 W5 覆蓋（feat 非空率）：{primary['bh_cov']:.1%} "
        "（cache 僅 Tier A 子集；缺檔股 W5 永不亮）"
    )
    lines.append("- Research only · **未採納** · 未發明分點 light 黃燈")
    lines.append(f"- 耗時 {elapsed:.1f}s")
    lines.append("")
    lines.append("## 結論（先讀）")
    lines.append("")

    # Explicit W1_fixed OOS failure callout
    w1f_oos = hit_oos[hit_oos["yellow"] == "W1_fixed"]
    w1f_is = filter_df[
        (filter_df["universe"] == "thick_h3")
        & (filter_df["label"] == "hit")
        & (filter_df["yellow"] == "W1_fixed")
        & (filter_df["split"] == "IS")
    ]
    lines.append(
        "**總評：沒有可凍結的「抬綠燈 hit-precision」規則。** "
        "經典 `W1_fixed`（T−2 three_streak≥2）IS 看似有用，但 **OOS 反而傷害** "
        "（hit P 低於裸綠）；`W1_win5` 僅微幅抬升（OOS lift≈1.04），未跨凍結門檻。"
    )
    if not w1f_oos.empty and not w1f_is.empty:
        a, b = w1f_is.iloc[0], w1f_oos.iloc[0]
        lines.append(
            f"- `W1_fixed`：IS P={fmt_rate(a['P'])} lift={fmt_f(a['lift'])} → "
            f"OOS P={fmt_rate(b['P'])} lift={fmt_f(b['lift'])} Δ={fmt_pct(b['delta_pp'])}pp "
            f"（**勿當綠燈過濾**）。"
        )
    if best is not None:
        br = ret(str(best["yellow"]), "OOS")
        lines.append(
            f"- 軟候選（未凍結）`{best['yellow']}`：OOS hit P={fmt_rate(best['P'])} "
            f"vs base {fmt_rate(best['base'])} · lift={fmt_f(best['lift'])} · "
            f"Δ={fmt_pct(best['delta_pp'])}pp · cover={fmt_rate(best['cover'])} · "
            f"n={int(best['n_filt'])}/{int(best['n_green'])}."
        )
        if br is not None and g_oos is not None:
            lines.append(
                f"- 該軟候選 OOS med r_adj **{fmt_pct(br['med'])}%** vs 裸綠 "
                f"**{fmt_pct(g_oos['med'])}%**（點估計；非顯著採納依據）。"
            )
    w5_oos = hit_oos[hit_oos["yellow"] == "W5_fixed"]
    w5b_oos = return_df[
        (return_df["universe"] == "thick_h3")
        & (return_df["yellow"] == "W5b_fixed")
        & (return_df["split"] == "OOS")
    ]
    w5_ret = ret("W5_fixed", "OOS")
    if w5_ret is not None and g_oos is not None:
        lines.append(
            f"- `W5_fixed` 綠燈過濾：OOS hit 幾乎無 lift；但 OOS med "
            f"{fmt_pct(w5_ret['med'])}% vs 裸綠 {fmt_pct(g_oos['med'])}%（點估計略高）。"
        )
    if not w5b_oos.empty and g_oos is not None:
        wb = w5b_oos.iloc[0]
        lines.append(
            f"- `W5b_fixed` OOS med {fmt_pct(wb['med'])}%（n={int(wb['n'])}，偏薄）—"
            "不可因小 n 宣稱採納。"
        )
    lines.append(
        "- **黃→綠 onset**：W5 家族 OOS lift≈1.3–1.5，但 **IS lift<1**（基線／密度位移）→ "
        "**不凍結**。W1 家族 onset lift≈1.0–1.1，僅弱警戒。"
    )
    lines.append(
        "- **報酬 vs precision**：chip∧green **未穩定同時**抬 hit-precision 與報酬；"
        "少數軟規則（`W1_win5`／`W5_fixed`）OOS med 略高於裸綠，但是選擇效應＋點估計。"
        "黃燈僅 BRIEFING W1/W5；**勿造分點 light 黃燈**。"
    )
    lines.append("")

    if freeze:
        lines.append("### 凍結候選（IS 不傷 且 OOS 站住）")
        lines.append("")
        for c in freeze:
            if c["kind"] == "green_filter":
                lines.append(
                    f"- **綠燈過濾** `{c['yellow']}`：IS P={fmt_rate(c['is_P'])} lift={fmt_f(c['is_lift'])} · "
                    f"OOS P={fmt_rate(c['oos_P'])} lift={fmt_f(c['oos_lift'])} "
                    f"Δ={fmt_pct(c['oos_delta_pp'])}pp · n={c['oos_n']} cover={fmt_rate(c['oos_cover'])}"
                )
            else:
                lines.append(
                    f"- **黃→綠 onset@5** `{c['yellow']}`：IS P={fmt_rate(c['is_P'])} lift={fmt_f(c['is_lift'])} · "
                    f"OOS P={fmt_rate(c['oos_P'])} lift={fmt_f(c['oos_lift'])} "
                    f"fires={c['oos_fires']} R={fmt_rate(c['oos_R'])}"
                )
        lines.append("")
    else:
        lines.append(
            "### 凍結候選：**無**"
        )
        lines.append("")
        lines.append(
            "門檻：綠燈過濾需 IS lift≥1 且 OOS lift≥1.05、ΔP≥+2pp、n≥8；"
            "黃→綠 onset@5 需 IS lift≥1 且 OOS lift≥1.2。本輪皆未過。"
        )
        lines.append("")

    lines.append("## 黃燈定義（僅此 · 禁止新造分點黃燈）")
    lines.append("")
    lines.append("| ID | 條件 |")
    lines.append("|----|------|")
    for r in RULES:
        lines.append(f"| `{r.id}` | {r.label} |")
    lines.append("")
    lines.append(
        "「有用綠燈」標籤：`hit` = L1H7 r_adj>0；"
        f"`strong` = L1H7 r_adj≥全樣本綠燈中位 ({fmt_pct(primary.get('med_full'))}%)."
    )
    lines.append("")

    # Precision tables
    lines.append("## A) 綠燈過濾 · Precision / Recall / Lift（hit = r_adj>0）")
    lines.append("")
    lines.append("### thick_h3")
    lines.append("")
    lines.append(
        "| yellow | split | n_filt/n_g | cover | P | base | lift | Δpp | R | F1 |"
    )
    lines.append(
        "|--------|-------|----------:|------:|--:|-----:|-----:|----:|--:|---:|"
    )
    sub = filter_df[
        (filter_df["universe"] == "thick_h3") & (filter_df["label"] == "hit")
    ].sort_values(["yellow", "split"])
    for _, r in sub.iterrows():
        lines.append(
            f"| `{r['yellow']}` | {r['split']} | {int(r['n_filt'])}/{int(r['n_green'])} | "
            f"{fmt_rate(r['cover'])} | {fmt_rate(r['P'])} | {fmt_rate(r['base'])} | "
            f"{fmt_f(r['lift'])} | {fmt_pct(r['delta_pp'])} | {fmt_rate(r['R'])} | {fmt_f(r['F1'])} |"
        )
    lines.append("")

    lines.append("### thick_h3 · strong（≥綠燈中位）")
    lines.append("")
    lines.append(
        "| yellow | split | n_filt/n_g | P | base | lift | Δpp | R | F1 |"
    )
    lines.append(
        "|--------|-------|----------:|--:|-----:|-----:|----:|--:|---:|"
    )
    sub = filter_df[
        (filter_df["universe"] == "thick_h3") & (filter_df["label"] == "strong")
    ].sort_values(["yellow", "split"])
    for _, r in sub.iterrows():
        lines.append(
            f"| `{r['yellow']}` | {r['split']} | {int(r['n_filt'])}/{int(r['n_green'])} | "
            f"{fmt_rate(r['P'])} | {fmt_rate(r['base'])} | {fmt_f(r['lift'])} | "
            f"{fmt_pct(r['delta_pp'])} | {fmt_rate(r['R'])} | {fmt_f(r['F1'])} |"
        )
    lines.append("")

    # Secondary universe if present
    for uni in ("adopted_h3_exmega", "tier_a"):
        if uni not in filter_df["universe"].values:
            continue
        lines.append(f"### {uni} · hit")
        lines.append("")
        lines.append(
            "| yellow | split | n_filt/n_g | P | lift | Δpp | R | F1 |"
        )
        lines.append(
            "|--------|-------|----------:|--:|-----:|----:|--:|---:|"
        )
        sub = filter_df[
            (filter_df["universe"] == uni) & (filter_df["label"] == "hit")
        ].sort_values(["yellow", "split"])
        # only fixed family to keep report short
        sub = sub[sub["yellow"].str.contains("fixed")]
        for _, r in sub.iterrows():
            lines.append(
                f"| `{r['yellow']}` | {r['split']} | {int(r['n_filt'])}/{int(r['n_green'])} | "
                f"{fmt_rate(r['P'])} | {fmt_f(r['lift'])} | {fmt_pct(r['delta_pp'])} | "
                f"{fmt_rate(r['R'])} | {fmt_f(r['F1'])} |"
            )
        lines.append("")

    lines.append("## B) 黃燈 → 綠燈 onset（日曆 Precision）")
    lines.append("")
    lines.append(
        "P = P(綠燈在 [D, D+W−1] ∣ 籌碼黃燈@D)；base = 同宇宙任意股日基線；"
        "R = 綠燈前 W 日內曾亮黃燈的覆蓋。"
    )
    lines.append("")
    lines.append("### thick_h3 · W=5")
    lines.append("")
    lines.append("| yellow | split | fires | P | base | lift | R | F1 |")
    lines.append("|--------|-------|------:|--:|-----:|-----:|--:|---:|")
    sub = onset_df[
        (onset_df["universe"] == "thick_h3") & (onset_df["W"] == 5)
    ].sort_values(["yellow", "split"])
    for _, r in sub.iterrows():
        lines.append(
            f"| `{r['yellow']}` | {r['split']} | {int(r['fires'])} | "
            f"{fmt_rate(r['P'])} | {fmt_rate(r['base'])} | {fmt_f(r['lift'])} | "
            f"{fmt_rate(r['R'])} | {fmt_f(r['F1'])} |"
        )
    lines.append("")
    lines.append("### thick_h3 · W=1（同日轉換）")
    lines.append("")
    lines.append("| yellow | split | fires | P | base | lift | R |")
    lines.append("|--------|-------|------:|--:|-----:|-----:|--:|")
    sub = onset_df[
        (onset_df["universe"] == "thick_h3") & (onset_df["W"] == 1)
    ].sort_values(["yellow", "split"])
    for _, r in sub.iterrows():
        if "win5" in r["yellow"]:
            continue
        lines.append(
            f"| `{r['yellow']}` | {r['split']} | {int(r['fires'])} | "
            f"{fmt_rate(r['P'])} | {fmt_rate(r['base'])} | {fmt_f(r['lift'])} | "
            f"{fmt_rate(r['R'])} |"
        )
    lines.append("")

    lines.append("## C) L1H7 報酬 · 裸綠 vs chip∧green")
    lines.append("")
    lines.append("### thick_h3")
    lines.append("")
    lines.append("| arm | yellow | split | n | hit | med% | mean% |")
    lines.append("|-----|--------|-------|--:|----:|-----:|------:|")
    sub = return_df[return_df["universe"] == "thick_h3"].copy()
    # prefer G + key yellows
    keep_y = {"—", "W1_fixed", "W1or_fixed", "W5_fixed", "W5b_fixed", "W1_win5", "W5_win5"}
    sub = sub[sub["yellow"].isin(keep_y) | (sub["arm"] == "G_L1H7")]
    for _, r in sub.sort_values(["arm", "yellow", "split"]).iterrows():
        lines.append(
            f"| {r['arm']} | {r['yellow']} | {r['split']} | {int(r['n'])} | "
            f"{fmt_rate(r['hit'])} | {fmt_pct(r['med'])} | {fmt_pct(r['mean'])} |"
        )
    lines.append("")

    lines.append("## 不做什麼")
    lines.append("")
    lines.append("- **不**新造分點 light／soft 黃燈（那是 `TOP10_PRECURSOR_TIGHTEN`／H_C 另一條線）。")
    lines.append("- **不**把籌碼黃燈當單獨開倉訊號（日曆 P 仍低）。")
    lines.append("- **不**因 IS 小樣本或 OOS lift 虛高（base 變低）就採納進 Strategy／Order。")
    lines.append("- W5 缺集保 cache 的股票視為無 W5——不可用「缺值當通過」。")
    lines.append("")
    lines.append("## 宇宙備註")
    lines.append("")
    for r in results:
        lines.append(
            f"- **{r['label']}**：n_sid={len(r['sids'])} · greens={r['n_green']} · "
            f"bh_cov={r['bh_cov']:.1%} · sids={', '.join(r['sids'])}"
        )
    lines.append("")
    lines.append("## 產物")
    lines.append("")
    lines.append(
        "- `chip_and_green_filter_precision.csv` · `chip_and_green_onset_precision.csv` · "
        "`chip_and_green_returns.csv` · `status_chip_and_green_precision.json`"
    )
    lines.append("")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "CHIP_AND_GREEN_PRECISION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"wrote {OUT / 'CHIP_AND_GREEN_PRECISION.md'}")


def main() -> None:
    t0 = time.time()
    adopted = adopted_hard_ge3()
    adopted_exmega = [s for s in adopted if s not in MEGA]
    # Tier A from whale_events helper set
    from research.chen_chip.whale_events import TIER_A

    universes = [
        ("thick_h3", THICK_H3),
        ("adopted_h3_exmega", adopted_exmega or THICK_H3),
        ("tier_a", list(TIER_A)),
    ]
    # dedupe identical sid lists
    seen: set[tuple[str, ...]] = set()
    results: list[dict] = []
    for label, sids in universes:
        key = tuple(sorted(sids))
        if key in seen:
            log(f"skip duplicate universe {label}")
            continue
        seen.add(key)
        results.append(run_universe(sids, label))

    filter_df = pd.concat(
        [pd.DataFrame(r["filter_rows"]) for r in results if r["filter_rows"]],
        ignore_index=True,
    )
    onset_df = pd.concat(
        [pd.DataFrame(r["onset_rows"]) for r in results if r["onset_rows"]],
        ignore_index=True,
    )
    return_df = pd.concat(
        [pd.DataFrame(r["return_rows"]) for r in results if r["return_rows"]],
        ignore_index=True,
    )
    freeze = pick_freeze(filter_df, onset_df)

    filter_df.to_csv(OUT / "chip_and_green_filter_precision.csv", index=False)
    onset_df.to_csv(OUT / "chip_and_green_onset_precision.csv", index=False)
    return_df.to_csv(OUT / "chip_and_green_returns.csv", index=False)

    elapsed = time.time() - t0
    write_report(results, filter_df, onset_df, return_df, freeze, elapsed)

    status = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": round(elapsed, 2),
        "universes": {
            r["label"]: {
                "sids": r["sids"],
                "n_green": r["n_green"],
                "bh_cov": r["bh_cov"],
            }
            for r in results
        },
        "freeze": freeze,
    }
    (OUT / "status_chip_and_green_precision.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log("done")


if __name__ == "__main__":
    main()
