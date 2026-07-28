#!/usr/bin/env python3
"""H-BRANCH-PAIR-CONSENSUS step1 · 從 master_top3_buyer 事件中挖「分點配對共識」候選。

邏輯：
  對每個 (trade_date, stock_id)，取出當日 rn<=3（金額>=500萬）的分點集合。
  若集合內 >=2 個分點，代表這天這檔股票有多個大額買方同時出手 -> 產生所有兩兩配對。
  統計哪些配對在不同 (trade_date, stock_id) 組合中最常同時出現。

輸出（給 step2 在 mini 上算 forward return 用）：
  - whale_branch_round3_pairconsensus_candidates.csv：Top pairs 摘要（raw freq，dedup前）
  - whale_branch_round3_pairconsensus_joint_events.csv：每個 top pair 的「共識日」(dedup後，5天內只取一次)
  - whale_branch_round3_pairconsensus_solo_events.csv：每個 top pair 涉及分點各自的「單獨日」(對方不在場，dedup後)
  - whale_branch_round3_pairconsensus_stock_ids.txt：需要查 forward return 的股票清單（給 step2 SSH mini 用）

本腳本純本地跑（不用 SSH mini），只需要 master CSV + 排除清單。
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "reports/research/branch-footprint-screen"
MASTER_CSV = OUT_DIR / "master_top3_buyer_2024h2_2026.csv"
MEGA_BLACKLIST_PATH = OUT_DIR / "ab58_xMega_copytrade/mega_blacklist_v1.json"
MARKET_MAKER_EXCLUSION_PATH = OUT_DIR / "market_maker_branch_exclusion_v1.json"
MIN_AMT = 5_000_000.0
MAX_RANK = 3
MIN_PAIR_FREQ = 5
TOP_N_PAIRS = 15
DEDUP_DAYS = 5
# 分點整體事件數極高（近乎每日交易，遍布數百檔股票），跟其他任何分點的「共現」多半只是
# base-rate 巧合而非真正的共識訊號。這批分點的整體事件數見下方 [INFO] print，門檻抓 >=3000。
HYPERACTIVE_BRANCHES = {"9268", "9800", "1480", "1470", "8888", "9A00", "1560", "5850", "9200", "8880"}


def load_mega_blacklist() -> set[str]:
    data = json.loads(MEGA_BLACKLIST_PATH.read_text(encoding="utf-8"))
    return set(data["symbols"])


def load_market_maker_exclusion() -> set[str]:
    data = json.loads(MARKET_MAKER_EXCLUSION_PATH.read_text(encoding="utf-8"))
    return {s["trader_id"] for s in data["symbols"]}


def dedup_events(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """rows = list of (stock_id, trade_date). Dedup: for each stock_id, keep first
    occurrence then require >DEDUP_DAYS gap from last kept (same logic as
    study_whale_branch_l1h7_top3_multihorizon.py)."""
    by_stock: dict[str, list[str]] = defaultdict(list)
    for sid, d in rows:
        by_stock[sid].append(d)
    kept: list[tuple[str, str]] = []
    for sid, dates in by_stock.items():
        dates_sorted = sorted(set(dates))
        last_kept = None
        for d in dates_sorted:
            if last_kept is None or (pd.Timestamp(d) - pd.Timestamp(last_kept)).days > DEDUP_DAYS:
                kept.append((sid, d))
                last_kept = d
    return kept


def main() -> int:
    master = pd.read_csv(MASTER_CSV, dtype={"stock_id": str, "securities_trader_id": str})
    mega = load_mega_blacklist()
    mm = load_market_maker_exclusion()

    master = master[~master["stock_id"].isin(mega)]
    master = master[~master["stock_id"].str.startswith("00")]
    master = master[master["buy_amt"] >= MIN_AMT]
    master = master[master["rn"] <= MAX_RANK]
    master = master[~master["securities_trader_id"].isin(mm)]
    print(f"[INFO] filtered master events: {len(master):,}")
    print(f"[INFO] distinct branches: {master['securities_trader_id'].nunique()}")
    print(f"[INFO] distinct (date,stock): {master.groupby(['trade_date','stock_id']).ngroups:,}")

    # Build per (trade_date, stock_id) -> set of trader_ids
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in master.itertuples(index=False):
        groups[(row.trade_date, row.stock_id)].add(row.securities_trader_id)

    multi_groups = {k: v for k, v in groups.items() if len(v) >= 2}
    print(f"[INFO] (date,stock) groups with >=2 top3 buyers: {len(multi_groups):,} / {len(groups):,}")

    pair_counter: Counter[frozenset] = Counter()
    pair_events: dict[frozenset, list[tuple[str, str]]] = defaultdict(list)  # pair -> [(stock_id, date)]
    for (d, sid), traders in multi_groups.items():
        for a, b in combinations(sorted(traders), 2):
            pair = frozenset((a, b))
            pair_counter[pair] += 1
            pair_events[pair].append((sid, d))

    print(f"[INFO] distinct pairs (raw freq>=1): {len(pair_counter):,}")
    qualifying = [(p, c) for p, c in pair_counter.items() if c >= MIN_PAIR_FREQ]
    qualifying.sort(key=lambda x: -x[1])
    print(f"[INFO] pairs with raw freq >= {MIN_PAIR_FREQ}: {len(qualifying)}")

    top_raw_pairs = qualifying[:TOP_N_PAIRS]

    # "Boutique" ranking: exclude pairs where either branch is a hyperactive/omnipresent
    # branch (base rate so high that co-occurrence with almost anything is expected by chance,
    # not a genuine consensus signal). This surfaces pairs of more selective branches.
    boutique_qualifying = [
        (p, c) for p, c in qualifying if not (p & HYPERACTIVE_BRANCHES)
    ]
    top_boutique_pairs = boutique_qualifying[:TOP_N_PAIRS]
    print(f"[INFO] boutique pairs (excl. hyperactive branches, freq>=5): {len(boutique_qualifying)}")

    # Union, tagging provenance, de-duplicated by pair identity.
    selected: dict[frozenset, dict] = {}
    for p, c in top_raw_pairs:
        selected.setdefault(p, {"pair": p, "raw_freq": c, "tags": set()})["tags"].add("top_raw_freq")
    for p, c in top_boutique_pairs:
        selected.setdefault(p, {"pair": p, "raw_freq": c, "tags": set()})["tags"].add("top_boutique")

    top_pairs = [(d["pair"], d["raw_freq"]) for d in selected.values()]
    tag_lookup = {d["pair"]: ";".join(sorted(d["tags"])) for d in selected.values()}
    top_pairs.sort(key=lambda x: -x[1])
    print(f"[INFO] union of selected pairs to score: {len(top_pairs)}")

    cand_rows = []
    joint_rows = []
    solo_rows = []
    all_stock_ids: set[str] = set()

    # per-branch: (stock_id,date) -> present (for solo lookup), branch-level occurrence
    branch_events: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for row in master.itertuples(index=False):
        branch_events[row.securities_trader_id].append((row.stock_id, row.trade_date))

    for pair_id, (pair, raw_freq) in enumerate(top_pairs, start=1):
        a, b = sorted(pair)
        raw_joint = pair_events[pair]  # list of (stock_id, date) both present
        joint_dedup = dedup_events(raw_joint)

        joint_stock_dates = set(raw_joint)  # for excluding from solo pool (undeduped, full precision)

        # solo for A: A's events where B NOT present that (stock,date)
        solo_a_raw = [(sid, d) for (sid, d) in branch_events[a] if (sid, d) not in joint_stock_dates]
        solo_b_raw = [(sid, d) for (sid, d) in branch_events[b] if (sid, d) not in joint_stock_dates]
        solo_a_dedup = dedup_events(solo_a_raw)
        solo_b_dedup = dedup_events(solo_b_raw)

        cand_rows.append(
            {
                "pair_id": pair_id,
                "trader_a": a,
                "trader_b": b,
                "selection": tag_lookup[pair],
                "raw_joint_freq": raw_freq,
                "joint_freq_dedup": len(joint_dedup),
                "solo_a_freq_dedup": len(solo_a_dedup),
                "solo_b_freq_dedup": len(solo_b_dedup),
                "n_stocks_joint": len({sid for sid, _ in joint_dedup}),
                "stocks_joint": ";".join(sorted({sid for sid, _ in joint_dedup})),
            }
        )
        for sid, d in joint_dedup:
            joint_rows.append({"pair_id": pair_id, "trader_a": a, "trader_b": b, "stock_id": sid, "signal_date": d})
            all_stock_ids.add(sid)
        for sid, d in solo_a_dedup:
            solo_rows.append({"pair_id": pair_id, "trader_a": a, "trader_b": b, "solo_trader": a, "stock_id": sid, "signal_date": d})
            all_stock_ids.add(sid)
        for sid, d in solo_b_dedup:
            solo_rows.append({"pair_id": pair_id, "trader_a": a, "trader_b": b, "solo_trader": b, "stock_id": sid, "signal_date": d})
            all_stock_ids.add(sid)

    cand_df = pd.DataFrame(cand_rows)
    joint_df = pd.DataFrame(joint_rows)
    solo_df = pd.DataFrame(solo_rows)

    cand_df.to_csv(OUT_DIR / "whale_branch_round3_pairconsensus_candidates.csv", index=False)
    joint_df.to_csv(OUT_DIR / "whale_branch_round3_pairconsensus_joint_events.csv", index=False)
    solo_df.to_csv(OUT_DIR / "whale_branch_round3_pairconsensus_solo_events.csv", index=False)
    (OUT_DIR / "whale_branch_round3_pairconsensus_stock_ids.txt").write_text(
        "\n".join(sorted(all_stock_ids)), encoding="utf-8"
    )

    print(f"\n[OK] top {len(top_pairs)} pairs -> whale_branch_round3_pairconsensus_candidates.csv")
    print(f"[OK] joint events (dedup): {len(joint_df)} rows -> whale_branch_round3_pairconsensus_joint_events.csv")
    print(f"[OK] solo events (dedup): {len(solo_df)} rows -> whale_branch_round3_pairconsensus_solo_events.csv")
    print(f"[OK] {len(all_stock_ids)} distinct stock_ids needed -> whale_branch_round3_pairconsensus_stock_ids.txt")
    print("\n[SELECTED PAIRS by co-occurrence freq]")
    print(cand_df[["pair_id", "trader_a", "trader_b", "selection", "raw_joint_freq", "joint_freq_dedup",
                    "solo_a_freq_dedup", "solo_b_freq_dedup", "n_stocks_joint"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
