#!/usr/bin/env python3
"""分點專家池 · 回看1日共識夜間觀測（統一入口）.

Research / observe only · 不下單。同一套規則骨架，依登錄池套用：

  2344 華邦電：統一/城中/安和 · ≥1億 · H7 skip / H10 extend
  8046 南電：信義/城中/統一 · ≥0.5億 · H10 extend
  8358 金居：土城永寧/城中/台北 · ≥1億 · H10 skip
  2327 國巨：士林/安和/敦南/玉山/大安 · ≥0.5億 · H10 skip（P6 偏好回看1日）

  PYTHONPATH=src python3 scripts/research/run_expert_pool_watch.py
  PYTHONPATH=src python3 scripts/research/run_expert_pool_watch.py --stock 2344
  PYTHONPATH=src python3 scripts/research/run_expert_pool_watch.py --no-refresh --bootstrap-only

相容：run_winbond_expert_pool_watch.py / run_specialty_expert_pool_watch.py 為 thin wrapper。
新股經 run_stock_expert_funnel.py 後登錄下方 POOLS；launchd 仍 --stock all。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from notify_email import send_alert  # noqa: E402
from project_dotenv import load_project_dotenv  # noqa: E402
from research.expert_pool_pattern_risk import (  # noqa: E402
    detect_pattern_risk,
    format_pattern_risk_lines,
)
# expert_pool_stage_quality imports pandas at module load. Keep it OUT of this
# module's import chain so lightweight callers that only need POOLS /
# refresh_stock_tape (e.g. the .venv-fubon branch-tape prewarm) don't require
# pandas installed. Resolve the real implementations lazily on first use.
def stage_quality_flag_enabled(*args, **kwargs):  # noqa: E402
    from research.expert_pool_stage_quality import flag_enabled

    return flag_enabled(*args, **kwargs)


def detect_stage_quality(*args, **kwargs):  # noqa: E402
    from research.expert_pool_stage_quality import (
        detect_stage_quality as _detect_stage_quality,
    )

    return _detect_stage_quality(*args, **kwargs)


def format_stage_quality_lines(*args, **kwargs):  # noqa: E402
    from research.expert_pool_stage_quality import (
        format_stage_quality_lines as _format_stage_quality_lines,
    )

    return _format_stage_quality_lines(*args, **kwargs)
from research.expert_pool_yellow_annotation import (  # noqa: E402
    detect_top10_light2_yellow,
    eligible_sid as yellow_eligible_sid,
    flag_enabled as yellow_flag_enabled,
    format_annotation_lines as yellow_annotation_lines,
)
from stock_db import DATA_DIR, DEFAULT_DB_PATH, connect  # noqa: E402

SOURCE = "finmind"
# Master flag; legacy aliases still accepted (first present wins).
ENV_FLAGS = (
    "RUN_EXPERT_POOL_EMAIL",
    "RUN_WINBOND_EXPERT_POOL_EMAIL",
    "RUN_SPECIALTY_EXPERT_POOL_EMAIL",
)
REPORT_ROOT = (
    ROOT / "reports" / "research" / "branch-footprint-screen" / "expert_pool"
)
EVAL_REGISTRY_PATH = REPORT_ROOT / "EVAL_REGISTRY.json"

# One-line research blurbs for inbox skim (A/B/C tiers + notable D).
RESEARCH_BLURBS: dict[str, str] = {
    "2327": "被動元件專家池最完整之一；Step0 仍穩",
    "2303": "代工權值但超額仍厚；安和／建國等常客",
    "3443": "超額漂亮但 n 偏薄，別過度外推",
    "6669": "伺服器主軸；凱基系多分點共現",
    "2383": "CCL／AI 材料鏈；硬專家多",
    "2408": "hard 最多；記憶體專家密度最高",
    "3189": "載板；門檻用 1億 較嚴",
    "3037": "載板；樣本夠、貼高超額下沿",
    "2337": "原池；記憶體，超額頂尖",
    "3481": "面板；高超額但產業 β 不同",
    "3653": "散熱；U2 明星之一",
    "6239": "封測；樣本略薄",
    "6223": "訊號最密；測試座（OR 冠軍）",
    "3665": "高速線纜／AI 連線",
    "6515": "測試座；與旺矽同類",
    "2317": "hard 很高，超額幾乎貼門檻",
    "2308": "權值電源；訊號密、超額薄",
    "3711": "大型封測；超額薄",
    "6274": "CCL；OR、樣本密",
    "3105": "化合物半導體",
    "4958": "載板",
    "2344": "原池；Step0 最貼門檻的舊檔",
    "8046": "原池載板",
    "8358": "原池銅箔；超額佳",
    "8033": "極端小樣本；當故事不當主倉邏輯",
    "6213": "單薄＋高中位，打折看",
    "6147": "單薄＋高中位，打折看",
    "3491": "單薄＋高中位，打折看",
    "2367": "單薄＋高中位，打折看",
}


@dataclass(frozen=True)
class PoolSpec:
    stock_id: str
    stock_name: str
    core: dict[str, str]
    net_floor: float
    floor_label: str
    playbook: tuple[str, ...]
    state_name: str
    origin_note: str
    report_dir: Path  # absolute or under REPORT_ROOT relative name handled at write


POOLS: dict[str, PoolSpec] = {
    "2344": PoolSpec(
        stock_id="2344",
        stock_name="華邦電",
        core={
            "5850": "統一",
            "5854": "統一-城中",
            "779Z": "國票安和",
        },
        net_floor=1e8,
        floor_label="≥1億",
        playbook=(
            "1. 進場：見上方「進場註記」· 1 槽（非無腦次日開）",
            "2. 預設：H7 · 持倉期間忽略新訊號（效率最佳）",
            "3. 波段：H10 · 連日訊號可延長持有（勿每日重進）",
            "4. 不要：同一波每天賣了再買；不要單跟 OR≥1億",
        ),
        state_name="winbond_expert_pool_watch_state.json",
        origin_note="監測池＝華邦電四門通過專家（非凱基台北／新店總榜）",
        report_dir=ROOT
        / "reports"
        / "research"
        / "branch-footprint-screen"
        / "winbond_expert_pool",
    ),
    "8046": PoolSpec(
        stock_id="8046",
        stock_name="南電",
        core={
            "9216": "凱基-信義",
            "9227": "凱基城中",
            "5850": "統一",
        },
        net_floor=5e7,
        floor_label="≥0.5億",
        playbook=(
            "1. 進場：見上方「進場註記」· 1 槽（非無腦次日開）",
            "2. 預設：H10 · 連日訊號可延長持有（P6網格候選，未經多重比較校正，勿讀成已驗證）",
            "3. 備選：H10 skip（持倉忽略新訊號）",
            "4. 不要：每日 restart；不要單跟 OR≥2億（無共識）當主線",
        ),
        state_name="specialty_8046_expert_pool_watch_state.json",
        origin_note="富邦新店為探針已出局；監測 P5 真專家池（非新店）",
        report_dir=REPORT_ROOT / "8046",
    ),
    "8358": PoolSpec(
        stock_id="8358",
        stock_name="金居",
        core={
            "9875": "元大-土城永寧",
            "9227": "凱基城中",
            "9268": "凱基台北",
        },
        net_floor=1e8,
        floor_label="≥1億",
        playbook=(
            "1. 進場：見上方「進場註記」· 1 槽（非無腦次日開）",
            "2. 預設：H10 skip · 持倉期間忽略新訊號（P6網格候選，mean/median顯著性不一致，勿讀成已驗證）",
            "3. 備選：H10 extend（樣本更薄）",
            "4. 不要：每日 restart；不要單跟 OR≥2億（無共識）",
        ),
        state_name="specialty_8358_expert_pool_watch_state.json",
        origin_note="富邦新店為探針已出局；監測 P5 真專家池（非新店）",
        report_dir=REPORT_ROOT / "8358",
    ),
}


def _pool_from_watch_spec(data: dict) -> PoolSpec:
    sid = str(data["stock_id"])
    champ = data.get("champion") or {}
    thin = bool(data.get("thin"))
    pol = champ.get("policy") or "H10_skip"
    mode = champ.get("mode") or "回看1日共識"
    floor_label = data.get("floor_label") or "≥0.5億"
    note = "股票優先漏斗；監測四門專家池"
    if thin:
        note += " · 單薄（hard=1）"
    playbook = tuple(
        data.get("playbook")
        or (
            "1. 進場：見上方「進場註記」· 1 槽（非無腦次日開）",
            f"2. 預設：{pol} · 觀測訊號仍為回看1日共識{floor_label}",
            f"3. P6網格候選模式：{mode}（未經多重比較校正，勿讀成已驗證）",
            "4. observe only · 不下單",
        )
    )
    return PoolSpec(
        stock_id=sid,
        stock_name=str(data.get("stock_name") or sid),
        core={str(k): str(v) for k, v in (data.get("core") or {}).items()},
        net_floor=float(data.get("net_floor") or 5e7),
        floor_label=str(floor_label),
        playbook=playbook,
        state_name=f"expert_pool_{sid}_watch_state.json",
        origin_note=note,
        report_dir=REPORT_ROOT / sid,
    )


def _merge_funnel_watch_specs(base: dict[str, PoolSpec]) -> dict[str, PoolSpec]:
    """Auto-register expert_pool/*/watch_spec.json (skips legacy 2344 path)."""
    out = dict(base)
    if not REPORT_ROOT.is_dir():
        return out
    for path in sorted(REPORT_ROOT.glob("*/watch_spec.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("action") != "register_watch":
            continue
        sid = str(data.get("stock_id") or path.parent.name)
        if sid in ("2344", "8046", "8358"):
            continue  # keep hand-tuned legacy specs
        if not data.get("core"):
            continue
        out[sid] = _pool_from_watch_spec(data)
    return out


POOLS = _merge_funnel_watch_specs(POOLS)


def _email_enabled() -> bool:
    off = ("0", "false", "False")
    for key in ENV_FLAGS:
        if key in os.environ:
            return os.environ[key].strip() not in off
    return True


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="專家池回看1日共識觀測（POOLS 動態）")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--stock",
        choices=[*sorted(POOLS.keys()), "all"],
        default="all",
        help="觀測標的（預設 all）",
    )
    ap.add_argument("--asof", default="", help="Signal date YYYY-MM-DD")
    ap.add_argument("--refresh-days", type=int, default=5)
    ap.add_argument("--no-refresh", action="store_true")
    ap.add_argument(
        "--state-dir",
        type=Path,
        default=DATA_DIR / "scratch",
        help="state JSON 目錄",
    )
    ap.add_argument("--force-email", action="store_true")
    ap.add_argument("--bootstrap-only", action="store_true")
    ap.add_argument(
        "--simulate-dates",
        default="",
        help="Comma YYYY-MM-DD · 回放達標信（不送出）寫入 expert_pool/email_sim/",
    )
    ap.add_argument(
        "--yellow-annotation",
        action="store_true",
        help=(
            "Research-only：若 Top10 light2 黃燈成立，於既有郵件加註記"
            "（或設 EXPERT_POOL_YELLOW_EMAIL_ANNOTATION=1；預設關）"
        ),
    )
    return ap.parse_args()


def load_registry_by_sid() -> dict[str, dict]:
    if not EVAL_REGISTRY_PATH.is_file():
        return {}
    try:
        data = json.loads(EVAL_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, dict] = {}
    for row in data.get("stocks") or []:
        sid = str(row.get("stock_id") or "")
        if sid:
            out[sid] = row
    return out


def research_tier(row: dict | None, *, thin_flag: bool = False) -> str:
    """A/B/C/D inbox trust tier from funnel registry stats."""
    if not row:
        return "D" if thin_flag else "?"
    hard = row.get("hard_n")
    med = row.get("med_radj_pct")
    thin = bool(row.get("thin")) or thin_flag
    try:
        hard_i = int(hard) if hard is not None else None
    except (TypeError, ValueError):
        hard_i = None
    try:
        med_f = float(med) if med is not None else None
    except (TypeError, ValueError):
        med_f = None
    # Legacy adopted without hard_n in registry: infer from med only.
    if hard_i is None:
        if thin:
            return "D"
        if med_f is None:
            return "?"
        if med_f >= 5.0:
            return "B"
        if med_f >= 2.0:
            return "C"
        return "D"
    if hard_i <= 1 or thin:
        return "D"
    if med_f is None:
        return "C" if hard_i >= 2 else "D"
    if hard_i >= 4 and med_f >= 5.0:
        return "A"
    if hard_i >= 2 and med_f >= 5.0:
        return "B"
    if hard_i >= 2 and med_f >= 2.0:
        return "C"
    return "D"


def retest_note(row: dict | None) -> str | None:
    """新數據體檢注記（EVAL_REGISTRY.retest_20260728）· 常態顯示於池 email。"""
    rt = (row or {}).get("retest_20260728")
    if not rt:
        return None
    med = rt.get("new_med_radj_pct")
    ms = f"{med:+.1f}%" if med is not None else "—"
    om = rt.get("orig_med_radj_pct")
    trend = ""
    if om is not None and med is not None:
        trend = f"（採納時 {om:+.1f}% → 現 {ms}）"
    return f"體檢2026-07-28：{rt.get('action','?')} · med {ms}·n={rt.get('new_n', 0)}{trend}"


def tier_cx_hint(tier: str) -> str:
    return {
        "A": "A · 強專家＋高超額 → 優先認真跟",
        "B": "B · 共識池、超額仍佳 → 好觀察",
        "C": "C · 共識有、貼門檻 → 跟得到但超額薄",
        "D": "D · 單薄／OR → 研究可看、實務降權",
        "?": "? · 尚無 registry 成績單 → 先當觀察",
    }.get(tier, "? · 尚無分級")


def load_trade_ledger(sid: str) -> dict | None:
    path = REPORT_ROOT / "knowledge" / "trades" / f"{sid}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def branch_id_name_sets(branches: list[dict] | None) -> tuple[set[str], set[str]]:
    """Extract branch ids + names for robust intersection matching."""
    ids: set[str] = set()
    names: set[str] = set()
    for b in branches or []:
        tid = b.get("tid") or b.get("id") or b.get("branch_id")
        if tid is not None and str(tid).strip():
            ids.add(str(tid).strip())
        name = str(b.get("name") or "").strip()
        if name:
            names.add(name)
    return ids, names


def filter_trades_by_hit_branches(
    trades: list[dict],
    hit_branches: list[dict] | None,
) -> list[dict]:
    """Keep ledger trades whose branch set intersects today's triggering branches."""
    hit_ids, hit_names = branch_id_name_sets(hit_branches)
    if not hit_ids and not hit_names:
        return []
    out: list[dict] = []
    for t in trades:
        tids, names = branch_id_name_sets(t.get("branches") or [])
        if (hit_ids and tids & hit_ids) or (hit_names and names & hit_names):
            out.append(t)
    return out


def per_core_l1h7_stats(
    core: dict[str, str],
    trades: list[dict] | None,
) -> list[dict]:
    """Per-core-expert L1H7 real-return stats from knowledge ledger trades.

    A trade counts for an expert if that tid (or exact name) appears in the
    trade's branch list. Returns one row per core expert (stable core order).
    """
    rows: list[dict] = []
    trades = trades or []
    for tid, name in core.items():
        rets: list[float] = []
        for t in trades:
            hit = False
            for b in t.get("branches") or []:
                bt = str(b.get("tid") or b.get("id") or b.get("branch_id") or "").strip()
                bn = str(b.get("name") or "").strip()
                if bt == tid or (bn and bn == name):
                    hit = True
                    break
            if not hit:
                continue
            try:
                rets.append(float(t["r_pct"]))
            except (KeyError, TypeError, ValueError):
                continue
        n = len(rets)
        if n == 0:
            rows.append(
                {
                    "tid": tid,
                    "name": name,
                    "n": 0,
                    "win_pct": None,
                    "med_r_pct": None,
                }
            )
            continue
        win_pct = 100.0 * sum(1 for r in rets if r > 0) / n
        rows.append(
            {
                "tid": tid,
                "name": name,
                "n": n,
                "win_pct": round(win_pct, 1),
                "med_r_pct": round(float(statistics.median(rets)), 2),
            }
        )
    return rows


def format_consensus_density_lines(
    *,
    spec: PoolSpec,
    hit_branches: list[dict] | None,
    ledger: dict | None = None,
) -> list[str]:
    """Show n_hit/n_core consensus ratio + each core expert's L1H7 win/n."""
    n_core = len(spec.core)
    hit_by_tid: dict[str, dict] = {}
    hit_by_name: dict[str, dict] = {}
    for b in hit_branches or []:
        tid = str(b.get("tid") or b.get("id") or "").strip()
        name = str(b.get("name") or "").strip()
        if tid:
            hit_by_tid[tid] = b
        if name:
            hit_by_name[name] = b
    n_hit = 0
    for tid, name in spec.core.items():
        if tid in hit_by_tid or name in hit_by_name:
            n_hit += 1
    pct = (100.0 * n_hit / n_core) if n_core else 0.0
    lines = [
        "—— 共識密度 ——",
        f"  今日觸發：{n_hit}/{n_core} 專家（{pct:.0f}%）"
        f" · 主訊號門檻≥2家（含昨買在場）",
    ]
    trades = list((ledger or {}).get("trades") or [])
    stats = per_core_l1h7_stats(spec.core, trades)
    for row in stats:
        b = hit_by_tid.get(row["tid"]) or hit_by_name.get(row["name"])
        if b is not None:
            role = str(b.get("role") or "在場").strip()
            mark = "★"
            role_s = f"[{role}]"
        else:
            mark = "·"
            role_s = "[未]"
        if row["n"] <= 0:
            stat_s = "L1H7 n=0（尚無樣本）"
        else:
            stat_s = (
                f"L1H7 n={row['n']} 勝率{row['win_pct']:.0f}% "
                f"中位{row['med_r_pct']:+.1f}%"
            )
        lines.append(
            f"  {mark} {row['name']} ({row['tid']}) {role_s} · {stat_s}"
        )
    lines.append("")
    return lines


def format_research_card_lines(
    *,
    sid: str,
    thin_flag: bool = False,
    registry: dict[str, dict] | None = None,
    max_trades: int | None = None,
    client_facing: bool = True,
    include_trades: bool = True,
    hit_branches: list[dict] | None = None,
) -> list[str]:
    """Research card for alerts.

    client_facing=True: never emit repo paths or truncated「另有 N 筆」.
    include_trades=False: omit trade bullets (HTML digest owns the full table).
    When trades are included alongside a live hit (hit_branches set), keep only
    ledger trades whose branches intersect today's triggering set.
    """
    reg = registry if registry is not None else load_registry_by_sid()
    row = reg.get(sid)
    tier = research_tier(row, thin_flag=thin_flag)
    lines = [
        "—— 研究成績單 ——",
        f"分級：{tier_cx_hint(tier)}",
    ]
    _rt = retest_note(row)
    if _rt:
        lines.append(f"◆ {_rt}")
    if not row:
        lines.append("  （尚無回測統計 · 僅依當日分點判斷）")
    else:
        hard = row.get("hard_n")
        med = row.get("med_radj_pct")
        n = row.get("radj_n")
        mode = row.get("champion_mode") or "—"
        floor = row.get("champion_floor") or "—"
        pol = row.get("champion_policy") or "—"
        med_s = f"{float(med):+.1f}%" if med is not None else "—"
        hard_s = str(hard) if hard is not None else "—"
        n_s = str(n) if n is not None else "—"
        lines.append(f"  hard={hard_s} · 中位超額 r_adj={med_s} · 樣本 n={n_s}")
        lines.append(f"  規則：{mode} · {floor} · {pol}")
        lines.append("  回測：訊號次日開→持有7日收 · 成本0.3% · 超額＝個股−1.15×大盤")
        blurb = RESEARCH_BLURBS.get(sid)
        if not blurb and row.get("thin"):
            blurb = "單薄（僅1家硬專家）；中位再高也請降權"
        if blurb:
            lines.append(f"  一句話：{blurb}")

    note_map = {
        "A": "備註：優先認真跟（專家池厚、超額穩）",
        "B": "備註：值得觀察（共識有、超額仍佳）",
        "C": "備註：可跟但預期超額薄（權值／大型常見）",
        "D": "備註：降權（單薄或單一席位風格）",
    }
    if tier in note_map:
        lines.append(f"  {note_map[tier]}")

    ledger = load_trade_ledger(sid)
    if ledger and ledger.get("trades"):
        s = ledger.get("summary") or {}
        lines.append(
            f"  回測摘要：共 {s.get('n')} 筆 · 中位真實報酬 {s.get('med_r_pct')}% · "
            f"中位超額 {s.get('med_radj_pct')}%"
        )
        if include_trades:
            trades = list(ledger["trades"])
            intersect_mode = hit_branches is not None
            if intersect_mode:
                trades = filter_trades_by_hit_branches(trades, hit_branches)
            if max_trades is not None and max_trades > 0:
                trades = trades[-max_trades:]
            if intersect_mode:
                lines.append(
                    f"  —— 歷史交易（與當日分點交集 · 共 {len(trades)} 筆）——"
                )
                if not trades:
                    lines.append("  （無與當日分點交集之歷史筆）")
            else:
                lines.append("  —— 歷史交易（訊號日 → 進出｜真實%｜超額%｜分點）——")
            for t in trades:
                br = ",".join(
                    f"{b['name']}[{b['role']}]" for b in (t.get("branches") or [])[:4]
                ) or "—"
                lines.append(
                    f"  · {t['signal_date']} → {t['entry_date']}…{t['exit_date']} "
                    f"真實 {t['r_pct']:+.1f}% · 超額 {t['r_adj_pct']:+.1f}%｜{br}"
                )
            # Never emit path refs or「另有 N 筆」for client mail.
            if (
                not client_facing
                and not intersect_mode
                and max_trades is not None
                and len(ledger["trades"]) > max_trades
            ):
                lines.append(f"  …另有 {len(ledger['trades']) - max_trades} 筆")
    lines.append("")
    return lines


def _float(v: object) -> float:
    if v is None or v == "":
        return 0.0
    return float(v)


def aggregate_stock_day_rows(
    raw: list[dict], *, trade_date: str, stock_id: str
) -> list[dict]:
    by_tid: dict[str, dict[str, float | str]] = {}
    for item in raw:
        tid = str(item.get("securities_trader_id") or "").strip()
        sid = str(item.get("stock_id") or item.get("data_id") or "").strip()
        if not tid:
            continue
        if sid and sid != stock_id:
            continue
        buy = _float(item.get("buy") or item.get("Buy"))
        sell = _float(item.get("sell") or item.get("Sell"))
        name = str(item.get("securities_trader") or "")
        slot = by_tid.get(tid)
        if slot is None:
            by_tid[tid] = {"buy": buy, "sell": sell, "securities_trader": name}
        else:
            slot["buy"] = float(slot["buy"]) + buy
            slot["sell"] = float(slot["sell"]) + sell
            if name and not slot.get("securities_trader"):
                slot["securities_trader"] = name
    out: list[dict] = []
    for tid, agg in by_tid.items():
        buy = float(agg["buy"])
        sell = float(agg["sell"])
        out.append(
            {
                "trade_date": trade_date,
                "securities_trader_id": tid,
                "securities_trader": str(agg.get("securities_trader") or tid),
                "stock_id": stock_id,
                "buy": buy,
                "sell": sell,
                "net": buy - sell,
                "source": SOURCE,
            }
        )
    return out


def list_recent_trading_days(conn, *, stock_id: str, n: int) -> list[str]:
    """近 n 個交易日（新到舊）。

    來源＝stock_daily_bars（尊重實際台股假日），另外強制併入最近 n 個
    「日曆平日」（週一至五），即使該股 daily bar 還沒同步進 DB 也會嘗試。
    daily bar 同步常常慢 1 天以上，若只依賴它會讓分點 tape 永遠補不到
    「今天」。FinMind 對還沒公布的日期只會回空 list，不會報錯，多試幾天
    成本很低、可自我修復。
    """
    today = date.today()
    rows = conn.execute(
        """
        SELECT DISTINCT trade_date FROM stock_daily_bars
        WHERE source = ? AND stock_id = ? AND trade_date <= ? AND close > 0
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (SOURCE, stock_id, today.isoformat(), max(1, n)),
    ).fetchall()
    days = [str(r[0]) for r in rows]

    forced: list[str] = []
    d = today
    while len(forced) < max(1, n):
        if d.weekday() < 5:  # Mon–Fri
            forced.append(d.isoformat())
        d -= timedelta(days=1)
    for day in forced:
        if day not in days:
            days.append(day)
    days.sort(reverse=True)
    return days[: max(1, n)]


def refresh_stock_tape(conn, stock_id: str, days: list[str], *, delay: float = 0.5) -> dict[str, int]:
    from finmind_client import fetch_taiwan_stock_trading_daily_report
    from stock_db import upsert_stock_broker_branch_daily

    counts: dict[str, int] = {}
    for day in days:
        try:
            raw = fetch_taiwan_stock_trading_daily_report(
                trade_date=day,
                data_id=stock_id,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  refresh {stock_id} {day}: FAIL {exc}")
            counts[day] = -1
            time.sleep(delay)
            continue
        rows = aggregate_stock_day_rows(raw, trade_date=day, stock_id=stock_id)
        try:
            n = upsert_stock_broker_branch_daily(conn, rows)
        except Exception as exc:  # noqa: BLE001 — e.g. DB locked by concurrent job
            print(f"  refresh {stock_id} {day}: WRITE FAIL {exc}")
            counts[day] = -1
            time.sleep(delay)
            continue
        print(f"  refresh {stock_id} {day}: rows={n} traders={len(rows)}")
        counts[day] = n
        time.sleep(delay)
    return counts


def close_on(conn, stock_id: str, trade_date: str) -> float | None:
    row = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND trade_date = ? AND source = ? AND close > 0
        """,
        (stock_id, trade_date, SOURCE),
    ).fetchone()
    return float(row[0]) if row else None


def tw_tick(price: float) -> float:
    """台股整股常見最小跳動（簡化）。"""
    p = abs(price)
    if p < 10:
        return 0.01
    if p < 50:
        return 0.05
    if p < 100:
        return 0.1
    if p < 500:
        return 0.5
    if p < 1000:
        return 1.0
    return 5.0


def round_tick(price: float) -> float:
    tick = tw_tick(price)
    return round(round(price / tick) * tick, 6)


def closes_through(conn, stock_id: str, asof: str, *, n: int = 5) -> list[float]:
    """Last n closes on or before asof (oldest→newest)."""
    rows = conn.execute(
        """
        SELECT close FROM stock_daily_bars
        WHERE stock_id = ? AND source = ? AND trade_date <= ? AND close > 0
        ORDER BY trade_date DESC
        LIMIT ?
        """,
        (stock_id, SOURCE, asof, max(1, n)),
    ).fetchall()
    return [float(r[0]) for r in reversed(rows)]


def format_ops_reference_footer() -> list[str]:
    """Frozen research bullets for ops emails (not Order intents).

    SSOT narrative: expert_pool/MINI_OPS_REFERENCE.md · hypotheses H_*.
    """
    return [
        "—— 操作參考（Research 凍結 · 非自動送單）——",
        "  · 進場：僅 hard 專家綠燈（champion 共識）· L1H7 為協議 SSOT",
        "  · 共識密度：信內「今日觸發 n/N」＋各專家 L1H7 勝率／n（ledger）",
        "  · 09:05：可選加權、非硬過濾；25m 非 fail 屬松山 sleeve，非專家池預設",
        "  · 不買：黃燈／Top10 light2 不當買訊；軟共識≠進場",
        "  · 持有：預設持到 H7；H10 僅研究註記（非提前賣出規則）",
        "  · 賣方：同日≥2 core HARD（W0_hard）僅作觀測附註（綠燈信內／持倉 20:10）；"
        "非出場、不擋綠燈、無綠燈不單獨寄",
        "  · 黃燈註記：預設關；mini 可設 EXPERT_POOL_YELLOW_EMAIL_ANNOTATION=1（仍非買訊）",
        "  · 圖形風險：主訊號觸發時若 T 日 HOT5／T_REJECT／單席≥5億等亮起 → 信內「共識但圖形有風險」",
        "  · 詳見 reports/.../expert_pool/MINI_OPS_REFERENCE.md",
        "",
    ]


def format_entry_hint(*, sig_close: float | None, sma5: float | None) -> list[str]:
    """Overnight annotate for T+1 human entry (skill 觀測進場建議)."""
    lines = [
        "—— 進場註記（人工判斷 · 非自動送單）——",
        "明日開盤後：",
        "  · 追價＝開盤÷訊號收−1；≤3% → 開盤／市價可做",
        "  · 追價>3% → 限價掛「訊號收×1.01」（同價最多約3個交易日；未成則棄本次）",
        "  · 軟例外：開盤已在 SMA5≤＋8% → 可開盤，不必死等限價",
        "  · 可選加權（非硬門檻）：09:05 相對開盤為綠或 >+1% → 加權跟；否則仍可開盤跟",
        "    （專家池級約 +1～2pp；勿把松山「25m 非 fail」當本池硬過濾）",
        "  · 不要：限價沒成就隔日開市價追；不要為觸均線而空等",
    ]
    if sig_close is None or sig_close <= 0:
        lines.append("  （訊號收盤價缺失 · 請自行對照上表規則）")
        lines.append("")
        return lines

    limit_px = round_tick(sig_close * 1.01)
    open_ok_px = round_tick(sig_close * 1.03)  # chase≤3% ceiling on open
    lines.append(f"  訊號收：{sig_close:.2f}")
    lines.append(f"  建議限價：{limit_px:.2f}（收×1.01）")
    lines.append(f"  追價≤3% 開盤上限約：{open_ok_px:.2f}")
    if sma5 is not None and sma5 > 0:
        soft_px = round_tick(sma5 * 1.08)
        ext = sig_close / sma5 - 1
        lines.append(f"  SMA5（含訊號日）：{sma5:.2f} · 訊號收相對均線 {ext*100:+.1f}%")
        lines.append(f"  軟例外開盤上限約：{soft_px:.2f}（SMA5×1.08）")
    else:
        lines.append("  SMA5：資料不足")
    lines.append("")
    return lines


def core_nets_on(conn, spec: PoolSpec, trade_date: str) -> list[dict]:
    close = close_on(conn, spec.stock_id, trade_date)
    if close is None:
        return []
    placeholders = ",".join("?" * len(spec.core))
    rows = conn.execute(
        f"""
        SELECT securities_trader_id, securities_trader, net, buy, sell
        FROM stock_broker_branch_daily
        WHERE stock_id = ? AND trade_date = ? AND source = ?
          AND securities_trader_id IN ({placeholders})
          AND net > 0
        """,
        (spec.stock_id, trade_date, SOURCE, *spec.core.keys()),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        net = float(r["net"])
        tid = str(r["securities_trader_id"])
        out.append(
            {
                "tid": tid,
                "name": spec.core.get(tid, str(r["securities_trader"] or tid)),
                "net": net,
                "net_amt": net * close,
                "close": close,
                "date": trade_date,
            }
        )
    return sorted(out, key=lambda x: -x["net_amt"])


def core_sell_nets_on(conn, spec: PoolSpec, trade_date: str) -> list[dict]:
    """Core seats with net < 0 on trade_date（金額用收盤價）."""
    close = close_on(conn, spec.stock_id, trade_date)
    if close is None:
        return []
    placeholders = ",".join("?" * len(spec.core))
    rows = conn.execute(
        f"""
        SELECT securities_trader_id, securities_trader, net, buy, sell
        FROM stock_broker_branch_daily
        WHERE stock_id = ? AND trade_date = ? AND source = ?
          AND securities_trader_id IN ({placeholders})
          AND net < 0
        """,
        (spec.stock_id, trade_date, SOURCE, *spec.core.keys()),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        net = float(r["net"])
        tid = str(r["securities_trader_id"])
        net_amt = net * close
        out.append(
            {
                "tid": tid,
                "name": spec.core.get(tid, str(r["securities_trader"] or tid)),
                "net": net,
                "net_amt": net_amt,
                "sell_amt": abs(net_amt),
                "close": close,
                "date": trade_date,
            }
        )
    return sorted(out, key=lambda x: -x["sell_amt"])


def detect_w0_hard(conn, spec: PoolSpec, asof: str) -> dict | None:
    """Same-day ≥2 core HARD sells（H_EXPERT_SELL_OBS_DESIGN primary tag）.

    Epistemic: least-bad hard lookback window · not a strong wind / exit signal.
    """
    hard = float(spec.net_floor)
    events = [e for e in core_sell_nets_on(conn, spec, asof) if e["sell_amt"] >= hard]
    if len(events) < 2:
        return None
    return {
        "tag": "W0_hard",
        "n_hard": len(events),
        "hard_floor": hard,
        "floor_label": spec.floor_label,
        "branches": events,
    }


def format_w0_hard_lines(w0: dict | None) -> list[str]:
    """Annotate sell observation only when present（caller gates on green mail）."""
    if not w0:
        return []
    lines = [
        "—— 賣方觀測（W0_hard · 同檔附註 · 非出場）——",
        "  · 定義：同日≥2家 core 淨賣≥hard；定位＝候選窗中最不糟硬標籤（非強風向）",
        "  · 用途：情境意識；hold 仍 L1H7；不取消綠燈、不擋進場、不單獨為賣寄信",
        f"  · 門檻：{w0.get('floor_label') or 'hard'} · 觸發 {w0['n_hard']} 家",
    ]
    for b in w0.get("branches") or []:
        lines.append(
            f"  · {b['name']} ({b['tid']}) 淨賣 "
            f"{b['sell_amt'] / 1e8:.2f} 億（{b['net']:,.0f} 股 @ {b['close']:.2f}）"
        )
    lines.append("")
    return lines


def prev_trading_day(conn, stock_id: str, trade_date: str) -> str | None:
    row = conn.execute(
        """
        SELECT trade_date FROM stock_daily_bars
        WHERE stock_id = ? AND source = ? AND trade_date < ? AND close > 0
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        (stock_id, SOURCE, trade_date),
    ).fetchone()
    return str(row[0]) if row else None


def rule_labels(spec: PoolSpec) -> tuple[str, str, str]:
    return (
        f"共識·回看1日{spec.floor_label}",
        f"共識·同日{spec.floor_label}",
        f"OR·{spec.floor_label}",
    )


def evaluate_rules(
    spec: PoolSpec,
    events_today: list[dict],
    events_yday: list[dict],
    *,
    yday: str | None,
) -> list[dict]:
    floor = spec.net_floor
    rule_lb, rule_same, rule_or = rule_labels(spec)
    big_today = [e for e in events_today if e["net_amt"] >= floor]
    big_yday = [e for e in events_yday if e["net_amt"] >= floor]
    today_ids = {e["tid"] for e in big_today}
    yday_ids = {e["tid"] for e in big_yday}
    present_ids = today_ids | yday_ids

    by_tid: dict[str, dict] = {}
    for e in big_yday:
        by_tid[e["tid"]] = {
            **e,
            "role": f"昨{spec.floor_label}",
            "yday_amt": e["net_amt"],
            "today_amt": 0.0,
        }
    for e in big_today:
        if e["tid"] in by_tid:
            by_tid[e["tid"]].update(
                {
                    **e,
                    "role": f"今+昨{spec.floor_label}",
                    "today_amt": e["net_amt"],
                }
            )
        else:
            by_tid[e["tid"]] = {
                **e,
                "role": f"今{spec.floor_label}",
                "yday_amt": 0.0,
                "today_amt": e["net_amt"],
            }
    present_branches = sorted(by_tid.values(), key=lambda x: -x["net_amt"])

    fired: list[dict] = []
    if len(present_ids) >= 2 and len(today_ids) >= 1:
        fired.append(
            {
                "rule": rule_lb,
                "priority": 1,
                "n_branches": len(present_ids),
                "branches": present_branches,
                "note": (
                    f"回看1日在場≥2家（含自己昨買仍算在場）"
                    f"{f' · 昨={yday}' if yday else ''}；今天至少1家{spec.floor_label}"
                    " · 資金效率首選"
                ),
                "email": True,
            }
        )
    if len(big_today) >= 2:
        fired.append(
            {
                "rule": rule_same,
                "priority": 2,
                "n_branches": len(big_today),
                "branches": big_today,
                "note": f"同日≥2家{spec.floor_label}（回看規則的子集；僅附註）",
                "email": False,
            }
        )
    if big_today:
        fired.append(
            {
                "rule": rule_or,
                "priority": 3,
                "n_branches": len(big_today),
                "branches": big_today,
                "note": f"OR{spec.floor_label}僅附註 · 不單獨寄信",
                "email": False,
            }
        )
    return sorted(fired, key=lambda x: x["priority"])


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {"notified": {}, "updated_at": None}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"notified": {}, "updated_at": None}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def signal_key(rule: str, asof: str) -> str:
    return f"{rule}|{asof}"


def format_alert_body(
    *,
    spec: PoolSpec,
    asof: str,
    fired: list[dict],
    tape_note: str,
    sig_close: float | None = None,
    sma5: float | None = None,
    registry: dict[str, dict] | None = None,
    client_facing: bool = True,
    include_trades: bool = True,
    yellow: dict | None = None,
    pattern_risk: dict | None = None,
    stage_quality: dict | None = None,
    include_ops_ref: bool = True,
    w0_hard: dict | None = None,
) -> str:
    rule_lb, _, _ = rule_labels(spec)
    core_s = " / ".join(spec.core.values())
    reg = registry if registry is not None else load_registry_by_sid()
    thin_flag = "單薄" in (spec.origin_note or "")
    tier = research_tier(reg.get(spec.stock_id), thin_flag=thin_flag)
    _rt = retest_note(reg.get(spec.stock_id))
    lines = [
        f"{spec.stock_name}（{spec.stock_id}）專家池共識觀測 · {asof}",
        "",
        f"【怎麼看這封信】{tier_cx_hint(tier)}",
    ]
    if _rt:
        lines.append(f"◆ {_rt}")
    lines += [
        f"專家核心：{core_s}",
        f"主訊號：{rule_lb}（含自己昨買仍算在場）",
        "動作：觀測通知（不下單）",
        f"說明：{spec.origin_note}",
    ]
    if not client_facing:
        lines.append(f"Tape：{tape_note}")
    lines.append("")
    email_hit = next((h for h in fired if h.get("email")), None)
    hit_branches = (
        list(email_hit.get("branches") or []) if email_hit is not None else None
    )
    ledger = load_trade_ledger(spec.stock_id)
    lines += format_consensus_density_lines(
        spec=spec,
        hit_branches=hit_branches,
        ledger=ledger,
    )
    lines += format_research_card_lines(
        sid=spec.stock_id,
        thin_flag=thin_flag,
        registry=reg,
        client_facing=client_facing,
        include_trades=include_trades,
        hit_branches=hit_branches,
    )
    for hit in fired:
        tag = "主" if hit.get("email") else "附"
        lines.append(f"【{tag}·{hit['rule']}】{hit['note']}")
        lines.append(f"  觸發分點數：{hit['n_branches']}")
        for b in hit["branches"]:
            role = b.get("role")
            role_s = f" [{role}]" if role else ""
            lines.append(
                f"  · {b['name']} ({b['tid']}){role_s} 淨買 "
                f"{b['net_amt'] / 1e8:.2f} 億（{b['net']:,.0f} 股 @ {b['close']:.2f}）"
            )
        lines.append("")
    lines += yellow_annotation_lines(yellow)
    lines += format_pattern_risk_lines(pattern_risk)
    lines += format_stage_quality_lines(stage_quality)
    # Sell annotation only when caller passes w0（綠燈主信附註；禁止 sell-only 寄信）
    lines += format_w0_hard_lines(w0_hard)
    lines += format_entry_hint(sig_close=sig_close, sma5=sma5)
    if include_ops_ref:
        lines += format_ops_reference_footer()
    if not client_facing:
        lines += ["—— 跟單 playbook（資金效率）——", *spec.playbook, ""]
        lines.append("腳本：scripts/research/run_expert_pool_watch.py")
    else:
        lines.append("觀測通知 · 非投資建議 · 非自動下單")
    return "\n".join(lines)


def signal_subject(*, spec: PoolSpec, asof: str, registry: dict[str, dict] | None = None) -> str:
    reg = registry if registry is not None else load_registry_by_sid()
    thin_flag = "單薄" in (spec.origin_note or "")
    tier = research_tier(reg.get(spec.stock_id), thin_flag=thin_flag)
    return f"[股票研究][{tier}] {spec.stock_name}共識觸發 · {asof}"


def write_daily_report(
    *,
    spec: PoolSpec,
    asof: str,
    fired: list[dict],
    body: str,
) -> Path:
    out_dir = spec.report_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"watch_{asof.replace('-', '')}.md"
    rule_lb, _, _ = rule_labels(spec)
    primary = [f for f in fired if f.get("email")]
    status = "TRIGGERED" if primary else "no_signal"
    md = [
        f"# {spec.stock_name}專家池觀測 · {asof}",
        "",
        f"- status: `{status}`",
        f"- primary: `{rule_lb}`",
        f"- generated: `{datetime.now().isoformat(timespec='seconds')}`",
        "",
        "```",
        body,
        "```",
        "",
    ]
    path.write_text("\n".join(md), encoding="utf-8")
    return path


def resolve_asof(conn, spec: PoolSpec, explicit: str) -> str | None:
    if explicit:
        return explicit[:10]
    placeholders = ",".join("?" * len(spec.core))
    row = conn.execute(
        f"""
        SELECT MAX(trade_date) FROM stock_broker_branch_daily
        WHERE stock_id = ? AND source = ?
          AND securities_trader_id IN ({placeholders})
        """,
        (spec.stock_id, SOURCE, *spec.core.keys()),
    ).fetchone()
    return str(row[0]) if row and row[0] else None


def maybe_detect_yellow(
    conn,
    spec: PoolSpec,
    *,
    asof: str,
    yday: str | None,
    registry: dict[str, dict],
    enabled: bool,
) -> dict | None:
    """Top10 light2 yellow annotation; no-op unless flag on + eligible sid."""
    if not enabled:
        return None
    if not yellow_eligible_sid(spec.stock_id, registry):
        return None
    return detect_top10_light2_yellow(
        conn,
        stock_id=spec.stock_id,
        asof=asof,
        yday=yday,
        close_asof=close_on(conn, spec.stock_id, asof),
        close_yday=close_on(conn, spec.stock_id, yday) if yday else None,
    )


def run_one(
    conn,
    spec: PoolSpec,
    *,
    asof_arg: str,
    refresh_n: int,
    state_path: Path,
    force_email: bool,
    bootstrap_only: bool,
    yellow_annotation: bool = False,
) -> int:
    rule_lb, _, _ = rule_labels(spec)
    tape_note = "db-only"
    if refresh_n > 0:
        days = list(reversed(list_recent_trading_days(conn, stock_id=spec.stock_id, n=refresh_n)))
        print(f"[{spec.stock_id}] refresh tape days={days}")
        counts = refresh_stock_tape(conn, spec.stock_id, days)
        ok = sum(1 for v in counts.values() if v >= 0)
        tape_note = f"finmind refresh {ok}/{len(days)} days"

    asof = resolve_asof(conn, spec, asof_arg)
    if not asof:
        print(f"[{spec.stock_id}] no core-branch tape in DB; skip")
        return 0

    yday = prev_trading_day(conn, spec.stock_id, asof)
    events_today = core_nets_on(conn, spec, asof)
    events_yday = core_nets_on(conn, spec, yday) if yday else []
    fired = evaluate_rules(spec, events_today, events_yday, yday=yday)
    email_hits = [f for f in fired if f.get("email")]
    print(
        f"[{spec.stock_id}] asof={asof} yday={yday} core_pos={len(events_today)} "
        f"fired={[f['rule'] for f in fired] or ['(none)']} "
        f"email={[f['rule'] for f in email_hits] or ['(none)']}"
    )
    for e in events_today:
        mark = "*" if e["net_amt"] >= spec.net_floor else " "
        print(f"  today{mark} {e['name']:12s} {e['net_amt'] / 1e8:6.2f}億")
    for e in events_yday:
        mark = "*" if e["net_amt"] >= spec.net_floor else " "
        print(f"  yday {mark} {e['name']:12s} {e['net_amt'] / 1e8:6.2f}億")

    state = load_state(state_path)
    notified: dict = state.setdefault("notified", {})
    is_bootstrap = not notified and not force_email

    new_hits: list[dict] = []
    for hit in email_hits:
        key = signal_key(hit["rule"], asof)
        if force_email or key not in notified:
            new_hits.append(hit)

    sig_close = close_on(conn, spec.stock_id, asof)
    closes = closes_through(conn, spec.stock_id, asof, n=5)
    sma5 = (sum(closes) / len(closes)) if len(closes) >= 5 else None
    registry = load_registry_by_sid()
    yellow_on = yellow_flag_enabled(cli=yellow_annotation)
    yellow = maybe_detect_yellow(
        conn,
        spec,
        asof=asof,
        yday=yday,
        registry=registry,
        enabled=yellow_on,
    )
    if yellow_on:
        print(
            f"[{spec.stock_id}] yellow_annotation="
            f"{'FIRE' if yellow else 'quiet'} (research-only)"
        )
    pattern_risk = None
    if email_hits:
        pattern_risk = detect_pattern_risk(
            conn,
            stock_id=spec.stock_id,
            asof=asof,
            today_core_events=events_today,
        )
        print(
            f"[{spec.stock_id}] pattern_risk="
            f"{[f['id'] for f in (pattern_risk or {}).get('flags', [])] or ['(none)']}"
        )
    w0 = detect_w0_hard(conn, spec, asof)
    if w0:
        print(f"[{spec.stock_id}] W0_hard n={w0['n_hard']} (annotate only if green mail)")
    stage_quality = None
    if email_hits and stage_quality_flag_enabled():
        stage_quality = detect_stage_quality(conn, stock_id=spec.stock_id, asof=asof)
        print(
            f"[{spec.stock_id}] stage_quality="
            f"{(stage_quality or {}).get('ta_quality', 'n/a')} (research-only)"
        )
    body = format_alert_body(
        spec=spec,
        asof=asof,
        fired=fired,
        tape_note=tape_note,
        sig_close=sig_close,
        sma5=sma5,
        registry=registry,
        yellow=yellow,
        pattern_risk=pattern_risk,
        stage_quality=stage_quality,
        # 僅綠燈主訊號信附註；禁止 sell-only 寄信（H_EXPERT_SELL_OBS_DESIGN）
        w0_hard=w0 if email_hits else None,
    )
    report = write_daily_report(
        spec=spec,
        asof=asof,
        fired=fired,
        body=body,
    )
    print(f"[{spec.stock_id}] report={report}")

    digest_key = f"digest|{asof}"

    if bootstrap_only or is_bootstrap:
        for hit in email_hits:
            notified[signal_key(hit["rule"], asof)] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "bootstrap": True,
            }
        notified[digest_key] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "bootstrap": True,
            "fired": bool(email_hits),
        }
        state["bootstrapped_asof"] = asof
        state["rule_version"] = rule_lb
        state["stock_id"] = spec.stock_id
        save_state(state_path, state)
        print(
            f"[{spec.stock_id}] bootstrap state written"
            + (" (no email)" if email_hits else " (quiet day)")
        )
        return 0

    # CX: with 40+ pools, only email on primary signal (no quiet-day spam).
    # Set EXPERT_POOL_QUIET_EMAIL=1 to restore per-stock quiet digests.
    quiet_email = os.environ.get("EXPERT_POOL_QUIET_EMAIL", "0").strip() not in (
        "0",
        "false",
        "False",
        "",
    )
    need_signal_mail = bool(new_hits)
    need_digest_mail = quiet_email and (force_email or digest_key not in notified)
    if not need_signal_mail and not need_digest_mail:
        if email_hits and not new_hits:
            print(f"[{spec.stock_id}] signal already notified for {asof}; skip")
        elif not email_hits:
            print(f"[{spec.stock_id}] quiet {asof}; no email (signal-only mode)")
        return 0

    if not _email_enabled():
        print(f"[{spec.stock_id}] email skipped: expert-pool email flag=0")
        for hit in new_hits:
            notified[signal_key(hit["rule"], asof)] = {
                "at": datetime.now().isoformat(timespec="seconds"),
                "skipped_email": True,
            }
        notified[digest_key] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "skipped_email": True,
            "fired": bool(email_hits),
        }
        save_state(state_path, state)
        return 0

    if need_signal_mail:
        subject = signal_subject(spec=spec, asof=asof, registry=registry)
    else:
        thin_flag = "單薄" in (spec.origin_note or "")
        tier = research_tier(registry.get(spec.stock_id), thin_flag=thin_flag)
        subject = f"[股票研究][{tier}] {spec.stock_name}專家池觀測 · {asof} · 今日無訊號"
        # quiet-day body: keep full format_alert_body context + explicit no-signal line
        body = (
            f"{spec.stock_name}（{spec.stock_id}）專家池觀測 · {asof}\n\n"
            f"【今日無訊號】排程已執行；主規則未觸發。\n"
            f"主訊號：{rule_lb}\n"
            f"Tape：{tape_note}\n\n"
            + body
        )

    send_alert(subject, body)
    print(f"[{spec.stock_id}] email sent: {subject}")
    for hit in new_hits:
        notified[signal_key(hit["rule"], asof)] = {
            "at": datetime.now().isoformat(timespec="seconds"),
        }
    notified[digest_key] = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "fired": bool(email_hits),
    }
    state["rule_version"] = rule_lb
    state["stock_id"] = spec.stock_id
    save_state(state_path, state)
    return 0


def simulate_signal_emails(
    conn, dates: list[str], *, yellow_annotation: bool = False
) -> Path:
    """Replay primary fires for adopted POOLS; write digest + per-stock .txt (no send)."""
    out_dir = REPORT_ROOT / "email_sim"
    out_dir.mkdir(parents=True, exist_ok=True)
    registry = load_registry_by_sid()
    yellow_on = yellow_flag_enabled(cli=yellow_annotation)
    index_lines = [
        "# 專家池達標信模擬（不送出）",
        "",
        f"- generated: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- pools: `{len(POOLS)}`",
        f"- yellow_annotation: `{int(yellow_on)}`",
        "",
    ]
    for asof in dates:
        fires: list[tuple[str, PoolSpec, str, list[dict]]] = []
        for sid, spec in sorted(POOLS.items()):
            yday = prev_trading_day(conn, spec.stock_id, asof)
            events_today = core_nets_on(conn, spec, asof)
            events_yday = core_nets_on(conn, spec, yday) if yday else []
            fired = evaluate_rules(spec, events_today, events_yday, yday=yday)
            email_hits = [f for f in fired if f.get("email")]
            if not email_hits:
                continue
            sig_close = close_on(conn, spec.stock_id, asof)
            closes = closes_through(conn, spec.stock_id, asof, n=5)
            sma5 = (sum(closes) / len(closes)) if len(closes) >= 5 else None
            yellow = maybe_detect_yellow(
                conn,
                spec,
                asof=asof,
                yday=yday,
                registry=registry,
                enabled=yellow_on,
            )
            pattern_risk = detect_pattern_risk(
                conn,
                stock_id=spec.stock_id,
                asof=asof,
                today_core_events=events_today,
            )
            stage_quality = (
                detect_stage_quality(conn, stock_id=spec.stock_id, asof=asof)
                if stage_quality_flag_enabled()
                else None
            )
            w0 = detect_w0_hard(conn, spec, asof)
            body = format_alert_body(
                spec=spec,
                asof=asof,
                fired=fired,
                tape_note="simulate db-only",
                sig_close=sig_close,
                sma5=sma5,
                registry=registry,
                yellow=yellow,
                pattern_risk=pattern_risk,
                stage_quality=stage_quality,
                w0_hard=w0 if email_hits else None,
            )
            subject = signal_subject(spec=spec, asof=asof, registry=registry)
            thin_flag = "單薄" in (spec.origin_note or "")
            tier = research_tier(registry.get(sid), thin_flag=thin_flag)
            fires.append((tier, spec, subject, fired))
            path = out_dir / f"sim_{asof.replace('-', '')}_{sid}.txt"
            path.write_text(f"Subject: {subject}\n\n{body}\n", encoding="utf-8")

        fires.sort(key=lambda x: ({"A": 0, "B": 1, "C": 2, "D": 3, "?": 4}.get(x[0], 9), x[1].stock_id))
        digest_path = out_dir / f"digest_{asof.replace('-', '')}.md"
        # data coverage note
        tape_n = conn.execute(
            "SELECT COUNT(*) FROM stock_broker_branch_daily WHERE source=? AND trade_date=?",
            (SOURCE, asof),
        ).fetchone()[0]
        dlines = [
            f"# 達標日報模擬 · {asof}",
            "",
            f"主訊號觸發：**{len(fires)}** / {len(POOLS)} 檔",
            f"當日 finmind 分點列數：`{tape_n}`"
            + (" · ⚠️ tape 明顯不全，達標數可能低估" if tape_n < 100_000 else ""),
            "",
            "| 分級 | 代號 | 名稱 | Subject |",
            "|---|---|---|---|",
        ]
        for tier, spec, subject, _fired in fires:
            dlines.append(
                f"| {tier} | {spec.stock_id} | {spec.stock_name} | `{subject}` |"
            )
        dlines += ["", "## 信件全文（依 A→D）", ""]
        for tier, spec, subject, _fired in fires:
            txt = (out_dir / f"sim_{asof.replace('-', '')}_{spec.stock_id}.txt").read_text(
                encoding="utf-8"
            )
            dlines += [f"### {tier} · {spec.stock_name}（{spec.stock_id}）", "", "```", txt.strip(), "```", ""]
        if not fires:
            dlines.append("_當日無任何主訊號達標。_")
        digest_path.write_text("\n".join(dlines) + "\n", encoding="utf-8")
        index_lines += [
            f"## {asof}",
            f"- 達標：{len(fires)} 檔 → `{digest_path.name}`",
            "",
        ]
        for tier, spec, subject, _fired in fires:
            index_lines.append(f"  - [{tier}] {spec.stock_id} {spec.stock_name}")
        index_lines.append("")
        print(f"[simulate] {asof}: fires={len(fires)} → {digest_path}")

    index_path = out_dir / "README.md"
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    return index_path


def main() -> int:
    load_project_dotenv()
    args = parse_args()
    if args.simulate_dates.strip():
        dates = [d.strip()[:10] for d in args.simulate_dates.split(",") if d.strip()]
        conn = connect(args.db)
        try:
            path = simulate_signal_emails(
                conn, dates, yellow_annotation=bool(args.yellow_annotation)
            )
            print(f"simulate index: {path}")
            return 0
        finally:
            conn.close()

    refresh_n = 0 if args.no_refresh else max(0, int(args.refresh_days))
    targets = list(POOLS) if args.stock == "all" else [args.stock]

    conn = connect(args.db)
    try:
        rc = 0
        for sid in targets:
            spec = POOLS[sid]
            state_path = args.state_dir / spec.state_name
            try:
                run_one(
                    conn,
                    spec,
                    asof_arg=args.asof,
                    refresh_n=refresh_n,
                    state_path=state_path,
                    force_email=args.force_email,
                    bootstrap_only=args.bootstrap_only,
                    yellow_annotation=bool(args.yellow_annotation),
                )
            except Exception as exc:  # noqa: BLE001 — keep sibling stock running
                print(f"[{sid}] ERROR: {exc}", file=sys.stderr)
                rc = 1
        return rc
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
