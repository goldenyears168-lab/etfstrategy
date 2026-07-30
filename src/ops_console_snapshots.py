"""Build payloads for private ops console ``ops.snapshots`` kinds.

Kinds: ``watch`` · ``risk`` · ``thermo`` · ``branches`` (and optional ``today``).
Reads local monorepo reports / order snapshots; does not call brokers.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from stock_db import PROJECT_ROOT

from ops_stock_heatmap import build_stock_heatmap_payload as _build_stock_heatmap_payload

ROOT = PROJECT_ROOT
EVENING_DIR = ROOT / "reports/research/branch-footprint-screen/evening_watch"
EXPERT_POOL_DIR = ROOT / "reports/research/branch-footprint-screen/expert_pool"
SECOND_DISP_DIR = (
    ROOT / "reports/research/branch-footprint-screen/second_disp_top30_l1h7"
)
THERMO_STABLE = ROOT / "reports/daily/crash_thermometer.md"
CHIP_MACRO_HISTORY = ROOT / "reports/research/chip-macro/signal_history.csv"
DETACH_LATEST = ROOT / "reports/order/snapshots/us_tw_5m_sell_gate_latest.json"
TIER_A_CFG = ROOT / "config/tier_a_evening_watch.json"
SECOND_DISP_CFG = ROOT / "config/second_disp_expert_pool_watch.json"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _latest_glob(dir_path: Path, pattern: str) -> Path | None:
    if not dir_path.is_dir():
        return None
    files = sorted(dir_path.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def _mtime_iso(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def _parse_evening_digest(text: str) -> dict[str, Any]:
    """Pull overview fires from evening digest markdown."""
    fires: list[dict[str, str]] = []
    quiet_n = 0
    asof = None
    m_asof = re.search(r"夜間觀測 digest · (\d{4}-\d{2}-\d{2})", text)
    if m_asof:
        asof = m_asof.group(1)
    m_n = re.search(r"項目數：(\d+)\s*·\s*有訊號：(\d+)", text)
    n_items = int(m_n.group(1)) if m_n else None
    n_fires = int(m_n.group(2)) if m_n else None
    in_overview = False
    for line in text.splitlines():
        s = line.strip()
        if "總覽" in s and s.startswith("——"):
            in_overview = True
            continue
        if not in_overview:
            continue
        if s.startswith("——") and "總覽" not in s:
            break
        if s.startswith("★"):
            label = s.lstrip("★").strip()
            name, _, detail = label.partition(":")
            fires.append({"label": name.strip(), "detail": detail.strip() or "觸發"})
        elif s.startswith("·"):
            quiet_n += 1
    return {
        "asof": asof,
        "n_items": n_items,
        "n_fires": n_fires if n_fires is not None else len(fires),
        "n_quiet": quiet_n,
        "fires": fires,
    }


def build_watch_payload() -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    evening_path = _latest_glob(EVENING_DIR, "digest_*.md")
    evening: dict[str, Any] = {"present": False}
    if evening_path:
        body = evening_path.read_text(encoding="utf-8", errors="replace")
        parsed = _parse_evening_digest(body)
        evening = {
            "present": True,
            "path": str(evening_path.relative_to(ROOT)),
            "mtime": _mtime_iso(evening_path),
            **parsed,
            "preview_md": "\n".join(body.splitlines()[:80]),
        }
        sources.append({"kind": "evening_digest", "path": evening["path"]})

    pools: list[dict[str, Any]] = []
    if EXPERT_POOL_DIR.is_dir():
        for spec_path in sorted(EXPERT_POOL_DIR.glob("*/watch_spec.json")):
            spec = _read_json(spec_path) or {}
            sid = str(spec.get("stock_id") or spec_path.parent.name)
            latest_watch = _latest_glob(spec_path.parent, "watch_*.md")
            pools.append(
                {
                    "stock_id": sid,
                    "stock_name": spec.get("stock_name"),
                    "watch_md": (
                        str(latest_watch.relative_to(ROOT)) if latest_watch else None
                    ),
                    "watch_mtime": _mtime_iso(latest_watch),
                }
            )
        sources.append({"kind": "expert_pool_specs", "n": len(pools)})

    second: dict[str, Any] = {"present": False}
    sd_json = _latest_glob(SECOND_DISP_DIR, "watch_*.json")
    if sd_json:
        raw = _read_json(sd_json) or {}
        second = {
            "present": True,
            "path": str(sd_json.relative_to(ROOT)),
            "mtime": _mtime_iso(sd_json),
            "asof": raw.get("asof"),
            "n_hits": raw.get("n_hits", len(raw.get("hits") or [])),
            "n_remind": raw.get("n_remind"),
            "n_improved": raw.get("n_improved"),
            "hits": (raw.get("hits") or [])[:30],
            "note": raw.get("note"),
        }
        sources.append({"kind": "second_disp_watch", "path": second["path"]})
    elif SECOND_DISP_CFG.is_file():
        cfg = _read_json(SECOND_DISP_CFG) or {}
        second = {
            "present": True,
            "path": str(SECOND_DISP_CFG.relative_to(ROOT)),
            "mtime": _mtime_iso(SECOND_DISP_CFG),
            "title": cfg.get("title"),
            "status": cfg.get("status"),
            "n_hits": 0,
            "hits": [],
            "note": "尚無 watch_*.json；僅設定檔",
        }
        sources.append({"kind": "second_disp_cfg", "path": second["path"]})

    tier_a: dict[str, Any] = {"present": False}
    if TIER_A_CFG.is_file():
        cfg = _read_json(TIER_A_CFG) or {}
        tier_a = {
            "present": True,
            "path": str(TIER_A_CFG.relative_to(ROOT)),
            "updated": cfg.get("updated"),
            "entry_watch": cfg.get("entry_watch") or [],
            "exit_watch": cfg.get("exit_watch") or [],
            "note": cfg.get("note"),
        }
        sources.append({"kind": "tier_a_cfg", "path": tier_a["path"]})

    return {
        "schema": "ops-watch-v1",
        "title": "自選／專家池 Watch",
        "sources": sources,
        "evening": evening,
        "expert_pools": pools,
        "n_expert_pools": len(pools),
        "second_disp": second,
        "tier_a": tier_a,
    }


def build_risk_payload() -> dict[str, Any]:
    detach_raw = _read_json(DETACH_LATEST)
    detach: dict[str, Any]
    if detach_raw:
        latest = detach_raw.get("latest") if isinstance(detach_raw.get("latest"), dict) else {}
        detach = {
            "present": True,
            "path": str(DETACH_LATEST.relative_to(ROOT)),
            "mtime": _mtime_iso(DETACH_LATEST),
            "ok": detach_raw.get("ok"),
            "level": detach_raw.get("level"),
            "action_hint": detach_raw.get("action_hint"),
            "session_date": detach_raw.get("session_date"),
            "strategy_id": detach_raw.get("strategy_id"),
            "as_of": detach_raw.get("as_of"),
            "first_red_poll": detach_raw.get("first_red_poll"),
            "first_yellow_poll": detach_raw.get("first_yellow_poll"),
            "auto_order": detach_raw.get("auto_order"),
            "rebuy": detach_raw.get("rebuy"),
            "rule": detach_raw.get("rule"),
            "latest": {
                "poll": latest.get("poll"),
                "tw_from_open": latest.get("tw_from_open"),
                "nq_from_open": latest.get("nq_from_open"),
                "spread_30m": latest.get("spread_30m"),
                "gap_pulse": latest.get("gap_pulse"),
                "sync_pulse": latest.get("sync_pulse"),
                "red_now": latest.get("red_now"),
                "yellow_now": latest.get("yellow_now"),
            },
        }
        level = str(detach.get("level") or "UNKNOWN")
    else:
        detach = {
            "present": False,
            "path": str(DETACH_LATEST.relative_to(ROOT)),
            "note": "尚無 us_tw_5m_sell_gate_latest.json（mini detach-gate 寫入後顯示）",
        }
        level = "UNKNOWN"

    severity = "info"
    if level in {"RED", "RED_LATCHED"}:
        severity = "red"
    elif level in {"YELLOW", "YELLOW_LATCHED"}:
        severity = "warn"

    notes: list[str] = []
    if detach.get("present") and detach.get("action_hint"):
        notes.append(str(detach["action_hint"]))
    if not detach.get("present"):
        notes.append("Detach gate 快照缺失")

    return {
        "schema": "ops-risk-v1",
        "title": "風控 · Detach Gate",
        "severity": severity,
        "level": level,
        "detach_gate": detach,
        "notes": notes,
    }


def _parse_thermo_md(text: str) -> dict[str, Any]:
    asof = None
    m_asof = re.search(r"大跌溫度計 · (\d{4}-\d{2}-\d{2})", text)
    if m_asof:
        asof = m_asof.group(1)
    temp_pct = None
    lamp = None
    m_temp = re.search(
        r"溫度百分位[：:]\s*([0-9.]+)%\s*([🔴🟠🟡🟢])\s*(\S+)",
        text,
    )
    if m_temp:
        temp_pct = float(m_temp.group(1))
        lamp = f"{m_temp.group(2)} {m_temp.group(3)}"
    consensus = None
    m_c = re.search(r"共識家數[：:]\s*([0-9]+/[0-9]+)", text)
    if m_c:
        consensus = m_c.group(1)
    score = None
    m_s = re.search(r"複合分數[^：\n]*[：:]\s*([0-9.]+)", text)
    if m_s:
        score = float(m_s.group(1))
    trend: list[dict[str, Any]] = []
    in_trend = False
    for line in text.splitlines():
        if "近30個交易日趨勢" in line:
            in_trend = True
            continue
        if in_trend and line.startswith("====="):
            break
        if not in_trend:
            continue
        m = re.match(
            r"(\d{4}-\d{2}-\d{2})\s+([0-9.]+)%\s+(\S+)\s+(\S+)\s+(\d+/\d+)\s+([0-9.]+)",
            line.strip(),
        )
        if m:
            trend.append(
                {
                    "date": m.group(1),
                    "temp_pct": float(m.group(2)),
                    "lamp": f"{m.group(3)} {m.group(4)}",
                    "consensus": m.group(5),
                    "score": float(m.group(6)),
                }
            )
    return {
        "asof_date": asof,
        "temp_pct": temp_pct,
        "lamp": lamp,
        "consensus": consensus,
        "score": score,
        "trend": trend[:14],
    }


THERMO_RETIRED_NOTE = (
    "2026-07-28 研究擱置：用五種獨立方法（嚴格walk-forward事件回測、反應vs領先因果拆解、"
    "跨權值股外資賣超廣度、5家外資大行分點歷史回補交叉驗證、40+組門檻參數穩健性grid）"
    "重新驗證，判別力介於31~48%，全部低於或貼著亂猜的50%門檻，沒有一種方法找到真實預測力。"
    "已從 Today 首頁總覽移除，本頁保留僅供歷史參考，不建議當作任何形式的風控依據。"
    "完整研究過程見 reports/research/branch-footprint-screen/crash_thermometer_lookahead_reaudit_20260727.md"
)


def build_thermo_payload() -> dict[str, Any]:
    stamped = _latest_glob(ROOT / "reports/daily", "*_crash_thermometer.md")
    path = THERMO_STABLE if THERMO_STABLE.is_file() else stamped
    if path is None or not path.is_file():
        return {
            "schema": "ops-thermo-v1",
            "title": "大跌溫度計",
            "present": False,
            "status": "retired_no_signal",
            "retired_note": THERMO_RETIRED_NOTE,
            "note": "尚無 reports/daily/crash_thermometer.md",
        }
    body = path.read_text(encoding="utf-8", errors="replace")
    parsed = _parse_thermo_md(body)
    return {
        "schema": "ops-thermo-v1",
        "title": "大跌溫度計",
        "present": True,
        "status": "retired_no_signal",
        "retired_note": THERMO_RETIRED_NOTE,
        "path": str(path.relative_to(ROOT)),
        "mtime": _mtime_iso(path),
        **parsed,
        "body_md": body[:12000],
    }


def build_branches_payload() -> dict[str, Any]:
    watch = build_watch_payload()
    evening = watch.get("evening") or {}
    second = watch.get("second_disp") or {}
    pools = watch.get("expert_pools") or []
    fired_labels = [f.get("label") for f in (evening.get("fires") or []) if f.get("label")]
    return {
        "schema": "ops-branches-v1",
        "title": "分點／處置／專家池",
        "n_expert_pools": len(pools),
        "evening_asof": evening.get("asof"),
        "evening_fires": evening.get("fires") or [],
        "n_evening_fires": evening.get("n_fires") or 0,
        "fired_labels": fired_labels,
        "second_disp": {
            "asof": second.get("asof"),
            "n_hits": second.get("n_hits"),
            "n_remind": second.get("n_remind"),
            "note": second.get("note"),
            "hits_preview": (second.get("hits") or [])[:15],
            "present": second.get("present"),
        },
        "tier_a_entry_n": len((watch.get("tier_a") or {}).get("entry_watch") or []),
        "sources": watch.get("sources") or [],
        "preview_md": evening.get("preview_md"),
    }


def build_today_payload() -> dict[str, Any]:
    watch = build_watch_payload()
    risk = build_risk_payload()
    # 2026-07-28 移除：大跌溫度計已用五種獨立方法（walk-forward事件回測、反應vs領先、
    # 跨權值股廣度、5家外資大行分點回補、門檻穩健性grid）驗證過，判別力介於31~48%，
    # 全部低於或貼著亂猜的50%門檻，沒有一種方法找到真實預測力。不再放進首頁總覽，
    # 避免被誤讀成有效的即時風控指標。細節見 build_thermo_payload()（現已加註退役說明），
    # 詳細研究過程見 reports/research/branch-footprint-screen/crash_thermometer_lookahead_reaudit_20260727.md
    return {
        "schema": "ops-today-v1",
        "title": "Today 總覽",
        "watch_fires": (watch.get("evening") or {}).get("n_fires"),
        "watch_asof": (watch.get("evening") or {}).get("asof"),
        "risk_level": risk.get("level"),
        "risk_severity": risk.get("severity"),
        "n_expert_pools": watch.get("n_expert_pools"),
        "links": {
            "watch": "/watch",
            "risk": "/risk",
            "branches": "/branches",
        },
    }


STAGE_HEATMAP_JSON = (
    ROOT / "reports/research/chip-overlays/2327_ta_adaptive/stage_heatmap_1y.json"
)


def build_stage_heatmap_payload() -> dict[str, Any]:
    """Weekly Weinstein Stage (30W SSOT) + S2 tier — latest per-stock state.

    Source: build_stage_heatmap_1y.py's JSON sidecar (same data backing
    stage_heatmap_1y.html). Research/observe only — not an Order signal.
    """
    data = _read_json(STAGE_HEATMAP_JSON)
    if data is None:
        return {
            "schema": "ops-stage-heatmap-v1",
            "title": "Weinstein Stage 熱力圖",
            "present": False,
            "note": "尚無 stage_heatmap_1y.json（跑 build_stage_heatmap_1y.py）",
        }
    rows = [
        {
            "sid": r.get("sid"),
            "name": r.get("name"),
            "stage": r.get("last_stage"),
            "slope_pct": r.get("last_slope"),
            "extension_pct": r.get("last_extension"),
            "s2_tier": r.get("last_s2_tier"),
            "pinned": bool(r.get("pinned")),
        }
        for r in (data.get("rows") or [])
    ]
    return {
        "schema": "ops-stage-heatmap-v1",
        "title": "Weinstein Stage 熱力圖 · 30W",
        "present": True,
        "field_ssot": data.get("field_ssot"),
        "engine": data.get("engine"),
        "confirm_days": data.get("confirm_days"),
        "asof": data.get("last_date"),
        "built_at": data.get("built_at"),
        "pin": data.get("pin"),
        "ix_stage": data.get("ix_stage"),
        "counts_30w": data.get("counts_30w"),
        "s2_tier_counts": data.get("s2_tier_counts"),
        "s2_gradient": data.get("s2_gradient"),
        "rows": rows,
        "note_zh": (
            "weinstein_stage（30週當量 SSOT，日更＋當天確認引擎）＋S2強度＝"
            "max(正規化MA斜率,正規化乖離)。研究觀察用，非下單訊號。"
        ),
    }


# 外資分點關鍵字（美系／港系外資券商分公司）。用於聚合外資單日淨買賣。
FOREIGN_BRANCH_KEYWORDS = (
    "美林", "摩根", "高盛", "瑞銀", "港商野村", "港麥格理", "花旗環球",
    "香港上海", "台灣摩根", "美商高盛", "法商", "德意志", "星展", "匯豐",
    "滙豐", "大和", "野村", "瑞士信貸",
)


def _load_stock_names(conn) -> dict[str, str]:
    try:
        rows = conn.execute(
            "SELECT stock_id, MAX(stock_name) FROM etf_holdings "
            "WHERE stock_name IS NOT NULL AND stock_name != '' GROUP BY stock_id"
        ).fetchall()
        return {str(r[0]): str(r[1]) for r in rows if r[1]}
    except Exception:  # noqa: BLE001 — names are best-effort
        return {}


def build_rotation_payload() -> dict[str, Any]:
    """外資分點單日輪動：latest trade_date 的外資淨買/淨賣 Top-N + 前日同向持續性.

    Source: stock_broker_branch_daily（每晚 backfill 全市場）。外資分點多為
    代理/造市，單日易反覆——研究觀察用，非個股買賣依據。
    """
    from stock_db import DEFAULT_DB_PATH, connect

    conn = connect(DEFAULT_DB_PATH)
    try:
        latest = conn.execute(
            "SELECT MAX(trade_date) FROM stock_broker_branch_daily"
        ).fetchone()[0]
        if not latest:
            return {
                "schema": "ops-rotation-v1",
                "title": "外資分點輪動",
                "present": False,
                "note": "尚無 stock_broker_branch_daily（等分點 backfill）",
            }
        prev = conn.execute(
            "SELECT MAX(trade_date) FROM stock_broker_branch_daily WHERE trade_date < ?",
            (latest,),
        ).fetchone()[0]
        names = _load_stock_names(conn)

        def foreign_net_by_stock(d: str) -> dict[str, float]:
            agg: dict[str, float] = {}
            for sid, tr, net in conn.execute(
                "SELECT stock_id, securities_trader, net "
                "FROM stock_broker_branch_daily WHERE trade_date = ?",
                (d,),
            ):
                if any(k in (tr or "") for k in FOREIGN_BRANCH_KEYWORDS):
                    agg[str(sid)] = agg.get(str(sid), 0.0) + float(net or 0.0)
            return agg

        cur = foreign_net_by_stock(latest)
        pre = foreign_net_by_stock(prev) if prev else {}
        universe_n = conn.execute(
            "SELECT COUNT(DISTINCT stock_id) FROM stock_broker_branch_daily "
            "WHERE trade_date = ?",
            (latest,),
        ).fetchone()[0]
    finally:
        conn.close()

    def row(sid: str) -> dict[str, Any]:
        f = cur.get(sid, 0.0)
        fp = pre.get(sid, 0.0)
        return {
            "stock_id": sid,
            "name": names.get(sid),
            "net_lots": round(f / 1000.0),       # 張（1 張 = 1000 股）
            "net_lots_prev": round(fp / 1000.0),
            "persist": (f > 0 and fp > 0) or (f < 0 and fp < 0),
        }

    ranked = sorted(cur.items(), key=lambda x: x[1], reverse=True)
    top_buy = [row(s) for s, v in ranked[:10] if v > 0]
    top_sell = [row(s) for s, v in ranked[::-1][:10] if v < 0]
    return {
        "schema": "ops-rotation-v1",
        "title": "外資分點輪動",
        "present": True,
        "asof": latest,
        "prev": prev,
        "universe_n": universe_n,
        "unit": "張",
        "top_buy": top_buy,
        "top_sell": top_sell,
        "note_zh": (
            "外資分點單日淨買/賣（聚合美系・港系外資分公司，單位：張）。"
            "外資分點多為代理／造市，單日易反覆、常含空單回補——"
            "研究觀察用，非個股買賣依據；跨日同向（persist ✓）較有參考性。"
        ),
    }


_SIZE_TXT = {0.0: "空手", 0.5: "半倉", 1.0: "全倉"}


def build_chip_macro_payload() -> dict[str, Any]:
    """外資期貨籌碼 × Weinstein 階段 風控燈號（L0×L2）。

    Source: ``daily_tracker.py`` → ``reports/research/chip-macro/signal_history.csv``.
    定位＝**風控 overlay 非 alpha**：近8年報酬 x1.66 < 買進持有 x3.66，勝在 Sharpe/回撤；
    空頭與基底無 edge。完整研究見 ``reports/research/chip-macro/RESEARCH_LOG.md``。
    """
    import csv

    if not CHIP_MACRO_HISTORY.is_file():
        return {
            "schema": "ops-chip-macro-v1",
            "title": "籌碼風控燈號",
            "present": False,
            "note": "尚無 signal_history.csv（等 chip-macro-tracker 首次執行）",
        }
    rows = list(csv.DictReader(CHIP_MACRO_HISTORY.read_text(encoding="utf-8").splitlines()))
    if not rows:
        return {
            "schema": "ops-chip-macro-v1",
            "title": "籌碼風控燈號",
            "present": False,
            "note": "signal_history.csv 尚無資料列",
        }

    def _f(v: Any, d: float = 0.0) -> float:
        try:
            return float(v)
        except (TypeError, ValueError):
            return d

    last = rows[-1]
    pos = _f(last.get("position"))
    z60 = _f(last.get("z60"))
    armed = str(last.get("armed", "")).strip().lower() in ("true", "1")
    # 融資斷頭底 (扣除ETF維持率, C3 重驗;純監控)
    _mx_raw = last.get("maint_exetf")
    maint_ex = round(_f(_mx_raw), 1) if _mx_raw not in (None, "") else None
    cover_streak = int(_f(last.get("fut_cover_streak")))
    dual_bottom = str(last.get("margin_dual_bottom", "")).strip().lower() in ("true", "1")
    if maint_ex is None:
        margin_light = None
    elif dual_bottom:
        margin_light = {"on": True, "label": f"雙確認底 (扣除ETF維持率 {maint_ex}% × 外資回補{cover_streak}日)"}
    elif maint_ex < 130:
        margin_light = {"on": True, "label": f"維持率追繳區 {maint_ex}% (待外資回補;現{cover_streak}日)"}
    elif maint_ex < 140:
        margin_light = {"on": True, "label": f"維持率斷頭壓力區 {maint_ex}% (<140觀察)"}
    else:
        margin_light = {"on": False, "label": f"扣除ETF維持率 {maint_ex}%"}
    return {
        "schema": "ops-chip-macro-v1",
        "title": "籌碼風控燈號",
        "present": True,
        "asof_date": last.get("date"),
        "headline": last.get("headline"),
        "position": pos,
        "position_txt": _SIZE_TXT.get(pos, f"{pos:.1f}"),
        "ix_close": _f(last.get("ix_close")),
        "ix_chg_pct": _f(last.get("ix_chg_pct")),
        "stage": last.get("stage"),
        "armed": armed,
        "z60": round(z60, 2),
        "maint_exetf": maint_ex,
        "fut_cover_streak": cover_streak,
        "margin_dual_bottom": dual_bottom,
        "lights": {
            "regime": {"on": armed, "label": last.get("stage")},
            "chip": {"on": _f(last.get("size")) > 0, "label": f"外資期貨 z60 {z60:+.2f}"},
            **({"margin": margin_light} if margin_light else {}),
        },
        "recent": [
            {
                "date": r.get("date"),
                "stage": r.get("stage"),
                "z60": round(_f(r.get("z60")), 2),
                "position_txt": _SIZE_TXT.get(_f(r.get("position")), ""),
            }
            for r in rows[-6:][::-1]
        ],
        "note": (
            "風控 overlay、非打敗大盤：近8年報酬 x1.66 < 買進持有 x3.66，勝在 Sharpe/回撤（−11% vs −32%）。"
            "空頭與基底無 edge；DSR 未過關量級勿過度信。研究觀察用，非個股買賣依據。"
        ),
        "dashboard_path": "reports/research/chip-macro/daily_dashboard.html",
    }


BUILDERS: dict[str, Any] = {
    "watch": build_watch_payload,
    "risk": build_risk_payload,
    "thermo": build_thermo_payload,
    "branches": build_branches_payload,
    "today": build_today_payload,
    "stage_heatmap": build_stage_heatmap_payload,
    "rotation": build_rotation_payload,
    "chip_macro": build_chip_macro_payload,
    "stock_heatmap": _build_stock_heatmap_payload,
}

KINDS = tuple(BUILDERS.keys())
