#!/usr/bin/env python3
"""2492: which FOREIGN broker desks predict better than aggregate 外資?

Motivation
  Official ``foreign_net`` mixes many accounts. Foreign *broker* desks
  (美林/高盛/瑞銀/摩通…) are observable in ``stock_broker_branch_daily`` and
  are the usual practitioner proxy for 「會動的外資」. This study ranks those
  desks by forward predictive power and compares each to:
    - official foreign_net
    - sum of all foreign desks
    - FT (= foreign + investment trust)

Hard limits (state upfront)
  - 外資券商 ≠ 全部外資：domestic brokers also execute foreign orders.
  - Foreign desks are a SUBSET of foreign_net → high collinearity; lift must
    beat the aggregate, not merely correlate with it.
  - 投信 has NO clean branch ID in this tape; we only probe a few domestic
    「總公司／經紀」seats as weak IT proxies and mark them as exploratory.
  - Branch tape for 2492 starts 2021-06; OOS = 2026.

Protocol
  - Features ≤ T. Outcome = β-excess vs IX0001 (β from past 60d), T→T+h.
  - Desk signal = net shares × close (NTD); also normalised by stock ADV20.
  - IS < 2026-01-01; OOS ≥ 2026. Primary h = 5.
  - Rank desks by IS forward Spearman and by buy-day median excess lift;
    require same-sign OOS to KEEP.

Out: reports/research/chip-overlays/2492_knowledge/foreign_desk_bakeoff/
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports/research/chip-overlays/2492_knowledge/foreign_desk_bakeoff"
SID = "2492"
SOURCE = "finmind"
BRANCH_START = "2021-06-01"
IS_START = "2021-07-01"  # after branch warmup
OOS = "2026-01-01"
HORIZONS = (1, 5, 10)
PRIMARY_H = 5
BETA_WIN = 60
PCTL_WIN = 252
PCTL_MIN = 60
MIN_ACTIVE_DAYS = 80  # desk must trade this stock often enough

# Practitioner map of foreign broker IDs (TWSE / CMoney lists).
FOREIGN_DESKS: dict[str, str] = {
    "1360": "港商麥格理",
    "1380": "東方匯理",
    "1440": "美林",
    "1470": "摩根士丹利",
    "1480": "美商高盛",
    "1520": "瑞士信貸",
    "1530": "港商德意志",
    "1560": "港商野村",
    "1570": "港商法興",
    "1590": "花旗環球",
    "1650": "新加坡商瑞銀",
    "8440": "摩根大通",
    "8890": "大和國泰",
    "8900": "法銀巴黎",
    "8910": "台灣巴克萊",
    "8960": "香港上海匯豐",
}
FOREIGN_KW = (
    "美商",
    "摩根",
    "瑞銀",
    "美林",
    "港商",
    "港麥",
    "麥格理",
    "野村",
    "瑞士",
    "花旗",
    "渣打",
    "香港",
    "新加坡",
    "大和",
    "法銀",
    "德意志",
    "巴克萊",
    "高盛",
    "美銀",
    "東方匯理",
    "聯昌",
)
# Weak IT proxies — exploratory only (投信 often via domestic HQ seats).
IT_PROXY_DESKS: dict[str, str] = {
    "9100": "群益金鼎-總公司",  # may vary; resolved from tape names
    "9600": "富邦-台北",
    "9800": "元大-台北",
    "7000": "兆豐-台北",
}


def load_price_inst() -> pd.DataFrame:
    uri = f"file:{DB.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        bars = pd.read_sql_query(
            """SELECT trade_date AS date, open, high, low, close, volume
               FROM stock_daily_bars
               WHERE source=? AND stock_id=? AND trade_date>=?
               ORDER BY trade_date""",
            conn,
            params=[SOURCE, SID, "2019-01-01"],
        )
        inst = pd.read_sql_query(
            """SELECT trade_date AS date, foreign_net, investment_trust_net
               FROM stock_institutional_daily
               WHERE source=? AND stock_id=? AND trade_date>=?
               ORDER BY trade_date""",
            conn,
            params=[SOURCE, SID, "2019-01-01"],
        )
        ix = pd.read_sql_query(
            """SELECT date, close AS ix FROM daily_bars
               WHERE code='IX0001' AND date>=?
               ORDER BY date,
                 CASE source WHEN 'yahoo' THEN 1 WHEN 'finmind' THEN 2 ELSE 9 END""",
            conn,
            params=["2019-01-01"],
        ).drop_duplicates("date", keep="first")
    for d in (bars, inst, ix):
        d["date"] = pd.to_datetime(d["date"])
    bars = bars[bars["close"] > 0]
    df = bars.merge(inst, on="date", how="left").merge(ix, on="date", how="left")
    return df.sort_values("date").reset_index(drop=True)


def load_branches() -> pd.DataFrame:
    """Date-bounded scan (PK starts with trade_date)."""
    uri = f"file:{DB.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        df = pd.read_sql_query(
            """SELECT trade_date AS date, securities_trader_id AS bid,
                      securities_trader AS bname, buy, sell, net
               FROM stock_broker_branch_daily
               WHERE source=? AND stock_id=?
                 AND trade_date>=? AND trade_date<=?""",
            conn,
            params=[SOURCE, SID, BRANCH_START, "2026-12-31"],
        )
    df["date"] = pd.to_datetime(df["date"])
    return df


def classify_desks(branches: pd.DataFrame) -> dict[str, dict]:
    """Strict allowlist for foreign desks — keyword matching false-positives
    domestic seats (e.g. 元大和平). Extra foreign-looking names are reported
    but not scored unless on the allowlist.
    """
    names = (
        branches.groupby("bid")["bname"]
        .agg(lambda s: s.dropna().mode().iloc[0] if len(s.dropna()) else "")
        .to_dict()
    )
    out: dict[str, dict] = {}
    for bid, name in names.items():
        kw_hit = any(k in str(name) for k in FOREIGN_KW)
        is_f = bid in FOREIGN_DESKS  # allowlist only
        label = FOREIGN_DESKS.get(bid) or str(name)[:24]
        out[bid] = {
            "name": label,
            "raw_name": str(name),
            "is_foreign": is_f,
            "kw_hit_not_allowlisted": bool(kw_hit and not is_f),
        }
    return out


def _rolling_beta(y: pd.Series, x: pd.Series, win: int = BETA_WIN) -> pd.Series:
    out = np.full(len(y), np.nan)
    ya, xa = y.to_numpy(float), x.to_numpy(float)
    for i in range(win, len(y)):
        yy, xx = ya[i - win : i], xa[i - win : i]
        m = np.isfinite(yy) & np.isfinite(xx)
        if m.sum() < 40:
            continue
        xc, yc = xx[m] - xx[m].mean(), yy[m] - yy[m].mean()
        v = float(np.dot(xc, xc))
        if v > 0:
            out[i] = float(np.dot(xc, yc) / v)
    return pd.Series(out, index=y.index).clip(0.2, 3.0)


def build_panel(price: pd.DataFrame, branches: pd.DataFrame, meta: dict) -> tuple[pd.DataFrame, list[str]]:
    d = price.copy()
    d["ret"] = d["close"].pct_change(fill_method=None)
    d["mret"] = d["ix"].pct_change(fill_method=None)
    d["beta"] = _rolling_beta(d["ret"], d["mret"]).shift(1)
    d["exret"] = d["ret"] - d["beta"] * d["mret"]
    d["turnover"] = d["close"] * d["volume"]
    d["adv20"] = d["turnover"].shift(1).rolling(20, min_periods=15).mean()

    d["f_ntd"] = d["foreign_net"] * d["close"]
    d["it_ntd"] = d["investment_trust_net"] * d["close"]
    d["ft_ntd"] = d["f_ntd"] + d["it_ntd"]
    d["f_part"] = d["f_ntd"] / d["adv20"]
    d["ft_part"] = d["ft_ntd"] / d["adv20"]

    # Pivot branch nets → NTD
    b = branches.copy()
    b["ntd"] = b["net"] * b["date"].map(d.set_index("date")["close"])
    # Foreign desk columns
    foreign_ids = sorted({bid for bid, m in meta.items() if m["is_foreign"]})
    # Keep desks with enough activity
    counts = b.groupby("bid").size()
    foreign_ids = [i for i in foreign_ids if counts.get(i, 0) >= MIN_ACTIVE_DAYS]
    # Also include known map even if slightly under (if present at all)
    for i in FOREIGN_DESKS:
        if i in counts.index and i not in foreign_ids and counts[i] >= 40:
            foreign_ids.append(i)
    foreign_ids = sorted(set(foreign_ids))

    piv = b.pivot_table(index="date", columns="bid", values="ntd", aggfunc="sum")
    for bid in foreign_ids:
        if bid not in piv.columns:
            piv[bid] = np.nan
        d[f"desk_{bid}"] = d["date"].map(piv[bid]).fillna(0.0)
        d[f"desk_{bid}_part"] = d[f"desk_{bid}"] / d["adv20"]

    # Aggregates
    d["foreign_desks_sum"] = d[[f"desk_{i}" for i in foreign_ids]].sum(axis=1)
    d["foreign_desks_part"] = d["foreign_desks_sum"] / d["adv20"]
    # Coverage: what fraction of official foreign_net is explained by desks
    d["desk_vs_foreign"] = d["foreign_desks_sum"] / d["f_ntd"].replace(0, np.nan)

    for h in HORIZONS:
        fwd_s = d["close"].shift(-h) / d["close"] - 1.0
        fwd_m = d["ix"].shift(-h) / d["ix"] - 1.0
        d[f"x{h}"] = fwd_s - d["beta"] * fwd_m
    return d, foreign_ids


def _spearman(a: pd.Series, b: pd.Series) -> float | None:
    x = pd.concat([a, b], axis=1).dropna()
    if len(x) < 40:
        return None
    return round(float(x.iloc[:, 0].corr(x.iloc[:, 1], method="spearman")), 4)


def _buy_lift(signal: pd.Series, target: pd.Series, train_q: float | None = None) -> dict:
    """If train_q given, buy = signal ≥ train quantile (PIT via caller). Else signal>0."""
    y = target.dropna()
    s = signal.reindex(y.index)
    if train_q is not None:
        mask = s >= train_q
    else:
        mask = s > 0
    buy = y[mask.fillna(False)]
    base = y
    if len(buy) < 15:
        return {"n": int(len(buy))}
    return {
        "n": int(len(buy)),
        "med_pct": round(float(buy.median() * 100), 3),
        "lift_pp": round(float((buy.median() - base.median()) * 100), 3),
        "win": round(float((buy > 0).mean() * 100), 1),
    }


def evaluate_series(
    d: pd.DataFrame, col: str, label: str, is_mask: pd.Series, oos_mask: pd.Series
) -> dict:
    target = f"x{PRIMARY_H}"
    row: dict = {"id": col, "label": label}
    # diagnostics on full usable sample
    row["same_day"] = _spearman(d[col], d["exret"])
    row["fwd"] = {f"x{h}": _spearman(d[col], d[f"x{h}"]) for h in HORIZONS}
    row["corr_vs_foreign_net"] = _spearman(d[col], d["f_ntd"])
    # IS / OOS buy-day lifts (sign>0) and high-tail (IS-threshold for OOS)
    for split, mask in (("IS", is_mask), ("OOS", oos_mask)):
        sub = d[mask]
        row[f"{split}_sign"] = _buy_lift(sub[col], sub[target])
    # OOS high-tail using IS 80th pct of the signal
    is_cut = d.loc[is_mask, col].quantile(0.8)
    row["OOS_hi_tail"] = _buy_lift(d.loc[oos_mask, col], d.loc[oos_mask, target], train_q=is_cut)
    row["IS_hi_tail"] = _buy_lift(d.loc[is_mask, col], d.loc[is_mask, target], train_q=is_cut)
    # Verdict
    i = row["IS_sign"]
    o = row["OOS_sign"]
    i_ic = row["fwd"].get(f"x{PRIMARY_H}")
    o_ic = _spearman(d.loc[oos_mask, col], d.loc[oos_mask, target])
    row["OOS_fwd_ic"] = o_ic
    if not i or i.get("n", 0) < 40:
        row["verdict"] = "INSUFFICIENT"
    elif i_ic is None:
        row["verdict"] = "INSUFFICIENT"
    elif i.get("lift_pp", 0) > 0.3 and (o.get("lift_pp") or 0) > 0 and (i_ic or 0) > 0:
        row["verdict"] = "KEEP"
    elif i.get("lift_pp", 0) < -0.3 and (o.get("lift_pp") or 0) < 0:
        row["verdict"] = "REVERSE"
    elif (i_ic or 0) < -0.05 and (o_ic or 0) < 0:
        row["verdict"] = "REVERSE_IC"
    else:
        row["verdict"] = "WEAK_OR_FLIP"
    return row


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("[1/4] load price/inst…", flush=True)
    price = load_price_inst()
    print("[2/4] load branches (slow PK scan)…", flush=True)
    branches = load_branches()
    meta = classify_desks(branches)
    print(f"  branch rows={len(branches)} desks={len(meta)}", flush=True)
    print("[3/4] build panel…", flush=True)
    d, foreign_ids = build_panel(price, branches, meta)
    # Restrict to branch era
    d = d[d["date"] >= BRANCH_START].copy()
    is_m = (d["date"] >= IS_START) & (d["date"] < OOS)
    oos_m = d["date"] >= OOS

    # Coverage diagnostic
    cov = d.loc[is_m, "desk_vs_foreign"].replace([np.inf, -np.inf], np.nan).dropna()
    coverage = {
        "foreign_desks_n": len(foreign_ids),
        "foreign_desks": {i: meta[i]["name"] for i in foreign_ids if i in meta},
        "desk_sum_over_foreign_net_median": round(float(cov.median()), 3) if len(cov) else None,
        "desk_sum_over_foreign_net_p25_p75": (
            [round(float(x), 3) for x in cov.quantile([0.25, 0.75])] if len(cov) else None
        ),
        "note": "ratio near 1 = desks ≈ official foreign; <<1 = foreign via domestic brokers",
    }

    print("[4/4] evaluate…", flush=True)
    candidates: list[tuple[str, str]] = [
        ("f_ntd", "官方外資 foreign_net×P"),
        ("ft_ntd", "官方FT 外資+投信"),
        ("f_part", "官方外資 / ADV20"),
        ("foreign_desks_sum", "外資分點合計"),
        ("foreign_desks_part", "外資分點合計 / ADV20"),
    ]
    for bid in foreign_ids:
        name = meta.get(bid, {}).get("name", bid)
        candidates.append((f"desk_{bid}", f"{bid} {name}"))
        candidates.append((f"desk_{bid}_part", f"{bid} {name} /ADV"))

    results = []
    for col, label in candidates:
        if col not in d.columns:
            continue
        results.append(evaluate_series(d, col, label, is_m, oos_m))

    # Rank foreign desks (not aggregates) by IS forward IC then lift
    desk_only = [
        r
        for r in results
        if r["id"].startswith("desk_")
        and not r["id"].endswith("_part")
        and r["id"] not in ("desk_vs_foreign",)
    ]
    desk_only.sort(
        key=lambda r: (
            -(r["fwd"].get(f"x{PRIMARY_H}") or -9),
            -(r.get("IS_sign", {}).get("lift_pp") or -9),
        )
    )

    # Best single desk vs official foreign: matched days where BOTH positive
    best = desk_only[0] if desk_only else None
    head_to_head = {}
    if best:
        col = best["id"]

        def summ(m: pd.Series, tgt: pd.Series) -> dict:
            x = tgt[m.fillna(False)].dropna()
            if len(x) < 10:
                return {"n": int(len(x))}
            return {
                "n": int(len(x)),
                "med_pct": round(float(x.median() * 100), 3),
                "lift_pp": round(float((x.median() - tgt.median()) * 100), 3),
                "win": round(float((x > 0).mean() * 100), 1),
            }

        for split, mask in (("IS", is_m), ("OOS", oos_m)):
            sub = d[mask]
            both = (sub[col] > 0) & (sub["f_ntd"] > 0)
            desk_only_buy = (sub[col] > 0) & (sub["f_ntd"] <= 0)
            foreign_only = (sub[col] <= 0) & (sub["f_ntd"] > 0)
            tgt = sub[f"x{PRIMARY_H}"]
            head_to_head[split] = {
                "best_desk": best["label"],
                "both_buy": summ(both, tgt),
                "desk_buy_foreign_flat": summ(desk_only_buy, tgt),
                "foreign_buy_desk_flat": summ(foreign_only, tgt),
            }

    # Also rank by ≥0.5億 buy-day lift (expert-pool floor)
    FLOOR = 5e7
    floor_ranks = []
    for bid in foreign_ids:
        col = f"desk_{bid}"
        for split, mask in (("IS", is_m), ("OOS", oos_m)):
            sub = d[mask]
            buy = sub[col] >= FLOOR
            tgt = sub[f"x{PRIMARY_H}"]
            x = tgt[buy.fillna(False)].dropna()
            entry = {
                "bid": bid,
                "label": meta[bid]["name"],
                "split": split,
                "n": int(len(x)),
            }
            if len(x) >= 15:
                entry.update(
                    {
                        "med_pct": round(float(x.median() * 100), 3),
                        "lift_pp": round(float((x.median() - tgt.median()) * 100), 3),
                        "win": round(float((x > 0).mean() * 100), 1),
                    }
                )
            floor_ranks.append(entry)
    # official foreign ≥0.5億 for comparison
    for split, mask in (("IS", is_m), ("OOS", oos_m)):
        sub = d[mask]
        buy = sub["f_ntd"] >= FLOOR
        tgt = sub[f"x{PRIMARY_H}"]
        x = tgt[buy.fillna(False)].dropna()
        floor_ranks.append(
            {
                "bid": "FOREIGN_NET",
                "label": "官方外資",
                "split": split,
                "n": int(len(x)),
                "med_pct": round(float(x.median() * 100), 3) if len(x) else None,
                "lift_pp": (
                    round(float((x.median() - tgt.median()) * 100), 3) if len(x) else None
                ),
                "win": round(float((x > 0).mean() * 100), 1) if len(x) else None,
            }
        )

    payload = {
        "protocol": {
            "sid": SID,
            "branch_start": BRANCH_START,
            "is": [IS_START, OOS],
            "oos": [OOS, str(d["date"].max().date())],
            "primary_h": PRIMARY_H,
            "outcome": "β-excess vs IX0001",
            "foreign_desk_rule": "strict TWSE/CMoney allowlist IDs only",
            "caveats": [
                "外資券商 ≠ 全部外資（coverage median shows most flow via domestic）",
                "分點是 foreign_net 的子集，高共線",
                "投信無乾淨分點 ID，本研究不宣稱投信分點替代",
                "pseudo-replication: consecutive buy days are not independent",
                "OOS 2026 華新科多頭，小 n 不可單獨 KEEP",
            ],
        },
        "coverage": coverage,
        "kw_hits_excluded": {
            bid: meta[bid]["raw_name"]
            for bid, m in meta.items()
            if m.get("kw_hit_not_allowlisted")
        },
        "benchmarks_and_desks": results,
        "desk_ranking_by_IS_fwd_ic": [
            {
                "rank": i + 1,
                "id": r["id"],
                "label": r["label"],
                "IS_fwd_ic": r["fwd"].get(f"x{PRIMARY_H}"),
                "OOS_fwd_ic": r.get("OOS_fwd_ic"),
                "IS_sign_lift": r.get("IS_sign", {}).get("lift_pp"),
                "OOS_sign_lift": r.get("OOS_sign", {}).get("lift_pp"),
                "corr_vs_foreign": r.get("corr_vs_foreign_net"),
                "verdict": r["verdict"],
            }
            for i, r in enumerate(desk_only)
        ],
        "floor_0p5yi_lifts": floor_ranks,
        "head_to_head_best_vs_foreign": head_to_head,
    }
    (OUT / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT / 'results.json'}")
    print("coverage:", coverage)
    print("TOP desks:")
    for r in payload["desk_ranking_by_IS_fwd_ic"][:10]:
        print(
            f"  #{r['rank']:2d} {r['label'][:28]:28s} "
            f"IS_ic={r['IS_fwd_ic']} OOS_ic={r['OOS_fwd_ic']} "
            f"IS_lift={r['IS_sign_lift']} OOS_lift={r['OOS_sign_lift']} → {r['verdict']}"
        )
    # print official baselines
    for r in results:
        if r["id"] in ("f_ntd", "ft_ntd", "foreign_desks_sum"):
            print(
                f"  BASE {r['label'][:28]:28s} "
                f"IS_ic={r['fwd'].get(f'x{PRIMARY_H}')} "
                f"IS_lift={r.get('IS_sign',{}).get('lift_pp')} "
                f"OOS_lift={r.get('OOS_sign',{}).get('lift_pp')} → {r['verdict']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
