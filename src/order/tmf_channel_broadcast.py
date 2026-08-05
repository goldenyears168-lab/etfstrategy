"""TMF channel · live commentator snapshot (A–D) for Order poll + paper UI.

Writes ``data/order/tmf_channel_broadcast.json`` each poll so :8770 can show
intent / near-trigger progress — not only post-trade results.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from stock_db import DATA_DIR

_TZ = ZoneInfo("Asia/Taipei")
SCHEMA = "tmf-channel-broadcast-v1"
DEFAULT_PATH = DATA_DIR / "order" / "tmf_channel_broadcast.json"


def broadcast_path() -> Path:
    return DEFAULT_PATH


def _fmt_px(x: Any) -> str:
    if x is None:
        return "—"
    try:
        return str(int(round(float(x))))
    except (TypeError, ValueError):
        return str(x)


def _side_zh(s: str | None) -> str:
    if s == "S":
        return "空"
    if s == "L":
        return "多"
    return "—"


def _pos_line(pos: dict[str, Any] | None, *, label: str) -> str:
    if not pos or not pos.get("s"):
        return f"{label}：空手"
    n = int(pos.get("n") or 1)
    u = pos.get("u_pnl")
    u_s = ""
    if u is not None:
        try:
            uf = float(u)
            u_s = f" · 浮盈 {uf:+.0f}pt"
        except (TypeError, ValueError):
            pass
    return f"{label}：{_side_zh(str(pos.get('s')))} {n} 口 @{_fmt_px(pos.get('ep'))}{u_s}"


def _rail_progress(spot: float | None, want_s: Any, want_l: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if spot is None:
        return out
    try:
        sp = float(spot)
    except (TypeError, ValueError):
        return out
    if want_s is not None:
        try:
            ws = float(want_s)
            dist = ws - sp
            out.append(
                {
                    "id": "rail_S",
                    "label": f"距空掛 S {_fmt_px(ws)}",
                    "value": round(dist, 1),
                    "unit": "pt",
                    "hint": f"還差 {dist:.0f} 點觸及" if dist > 0 else "已觸及／穿越空掛",
                    "pct": None,
                    "near": 0 < dist <= 25,
                }
            )
        except (TypeError, ValueError):
            pass
    if want_l is not None:
        try:
            wl = float(want_l)
            dist = sp - wl
            out.append(
                {
                    "id": "rail_L",
                    "label": f"距多掛 L {_fmt_px(wl)}",
                    "value": round(dist, 1),
                    "unit": "pt",
                    "hint": f"還差 {dist:.0f} 點觸及" if dist > 0 else "已觸及／穿越多掛",
                    "pct": None,
                    "near": 0 < dist <= 25,
                }
            )
        except (TypeError, ValueError):
            pass
    return out


def build_broadcast(
    summary: dict[str, Any],
    *,
    cfg: Any | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build commentator payload from one poll summary (+ optional cfg/ledger)."""
    now = datetime.now(tz=_TZ)
    ledger = ledger or {}
    dry = bool(summary.get("dry_run", True))
    reason = str(summary.get("reason") or "")
    hm = str(summary.get("hhmm") or now.strftime("%H:%M"))
    spot = summary.get("spot")
    want_s = summary.get("want_s")
    want_l = summary.get("want_l")
    open_pos = summary.get("open_pos")
    broker = summary.get("broker_live") or ledger.get("broker_pos")
    actions = list(summary.get("actions") or [])
    flatten_why = summary.get("flatten_why")
    regime = None
    last_d = ledger.get("last_desired") if isinstance(ledger.get("last_desired"), dict) else {}
    if isinstance(summary.get("regime"), str):
        regime = summary.get("regime")
    elif last_d:
        regime = last_d.get("regime")

    max_api = int(getattr(cfg, "max_api_per_day", 120) if cfg else 120)
    api_day = int(summary.get("api_calls_day") or ledger.get("api_calls_day") or 0)
    if max_api <= 0:
        api_pct = None
        api_hint = "日額度不限"
        api_near = False
        api_label = f"日 API {api_day}/∞"
    else:
        api_pct = max(0.0, min(100.0, 100.0 * api_day / max_api))
        api_hint = "接近上限將熔斷" if api_day >= max_api * 0.8 else "額度健康"
        api_near = api_day >= max_api * 0.8
        api_label = f"日 API {api_day}/{max_api}"
    kill_pts = float(getattr(cfg, "kill_day_loss_pts", 400.0) if cfg else 400.0)
    max_lots = int(getattr(cfg, "max_lots", 1) if cfg else 1)
    trail_arm = 50.0
    if cfg is not None:
        recipe = getattr(cfg, "recipe", None) or {}
        if isinstance(recipe, dict) and recipe.get("trail_arm_pts") is not None:
            trail_arm = float(recipe["trail_arm_pts"])

    day_pnl = ledger.get("day_pnl_pts")
    try:
        day_pnl_f = float(day_pnl) if day_pnl is not None else 0.0
    except (TypeError, ValueError):
        day_pnl_f = 0.0
    killed = bool(ledger.get("killed") or reason.startswith("killed"))

    # --- A headline / situation ---
    situation: list[str] = []
    if reason == "outside_session":
        headline = f"盤外待命 · 現 {hm} · 等下一交易窗再開 poll 送單"
        situation.append("時段：盤外（日 08:45–13:45／夜 15:00–05:00）")
        next_act = "等待開盤後第一輪：若券商超額／孤兒倉 → 先 flatten，再掛策略軌"
    elif killed:
        headline = f"已熔斷 · 停止送單 · {ledger.get('kill_reason') or reason}"
        situation.append("時段：熔斷鎖定")
        next_act = "人工確認後清 kill 旗標才可恢復"
    elif flatten_why:
        headline = f"優先平倉 · {flatten_why}"
        situation.append("時段：交易窗內 · 先處理券商倉再掛策略")
        next_act = f"本輪送出市價平倉 {_side_zh((broker or {}).get('s'))} {int((broker or {}).get('n') or 0)} 口"
    elif reason in ("reconciled", "flatten_first") and actions:
        kinds = [str(a.get("kind")) for a in actions]
        if "exit_market" in kinds:
            headline = "正在／已送出場市價 · 對齊 sim／券商"
        elif "place" in kinds and "cancel" in kinds:
            headline = "撤換掛單中 · 對齊 want rails"
        elif "place" in kinds:
            sides = [a.get("side") for a in actions if a.get("kind") == "place"]
            headline = "準備／已送限價掛單 · " + ("+".join(_side_zh(str(s)) for s in sides) or "軌")
        elif "cancel" in kinds:
            headline = "撤單對齊中 · 取消不符 want 的 resting"
        else:
            headline = "本輪有委託動作"
        next_act = "見下方 actions；下一輪再 diff"
        situation.append("時段：交易窗內 · reconciler 作用中")
    elif reason == "reconciled":
        headline = "意圖已對齊 · 本輪無新委託"
        situation.append("時段：交易窗內 · resting 與 want 一致（或預算用盡）")
        next_act = "持續盯距離／出場條件；觸及才成交"
    elif reason == "bars_lt_20":
        headline = "K 棒不足 · 暫不模擬／不送單"
        next_act = "等 1m K 累積 ≥20"
        situation.append("時段：資料暖機")
    else:
        headline = f"狀態 · {reason or 'idle'}"
        next_act = "見 reason"
        situation.append(f"reason={reason}")

    situation.append(f"模式：{'DRY（不下真單）' if dry else 'LIVE 實盤'}")
    situation.append(f"上限 max_lots={max_lots}")
    situation.append(_pos_line(broker if isinstance(broker, dict) else None, label="券商"))
    situation.append(_pos_line(open_pos if isinstance(open_pos, dict) else None, label="策略 sim"))
    if spot is not None:
        situation.append(f"策略現價（末 K）：{_fmt_px(spot)}")
    if want_s is not None or want_l is not None:
        situation.append(f"意圖掛單 S={_fmt_px(want_s)} · L={_fmt_px(want_l)}")
    else:
        situation.append("意圖掛單：本輪無雙軌（可能持倉保護／濾網／盤外）")
    if regime:
        situation.append(f"regime={regime}")

    # --- B narrative ---
    narrative: list[str] = []
    if dry:
        narrative.append("四鎖未全開或 dry_run=1 → 只對帳預覽，不打券商。")
    else:
        narrative.append("四鎖已開 → 本輪 place/cancel/exit 會打富邦 futopt。")
    if flatten_why:
        narrative.append(f"判斷：{flatten_why} → 禁止先掛進場軌，避免疊倉。")
    if broker and open_pos is None and not flatten_why and reason == "reconciled":
        narrative.append("注意：券商有倉但 sim 空手且未觸發 flatten — 檢查 broker 查詢／商品碼。")
    if want_s is None and want_l is None and reason == "reconciled" and not open_pos:
        narrative.append("空倉但無 want → 可能 skip_quiet／regime／place_every 冷卻。")
    if want_s is None and want_l is not None:
        narrative.append("單邊多掛：常見於強漲 regime（不掛空）或持倉保護。")
    if want_l is None and want_s is not None:
        narrative.append("單邊空掛：常見於強跌 regime（不掛多）或持倉保護。")
    if want_s is not None and want_l is not None and open_pos is None:
        narrative.append("空倉雙掛意圖：等價觸及限價才進場（非市價追價）。")
    if isinstance(open_pos, dict) and open_pos.get("s"):
        try:
            u = float(open_pos.get("u_pnl") or 0)
            if u >= trail_arm:
                narrative.append(f"浮盈 {u:.0f}≥ trail 啟動 {trail_arm:.0f} → 進入讓利保護區。")
            else:
                narrative.append(f"浮盈 {u:.0f}／trail 啟動 {trail_arm:.0f} → 還差 {trail_arm - u:.0f} pt 才武裝 trail。")
        except (TypeError, ValueError):
            pass
    narrative.append(f"下一動作：{next_act}")

    # --- D progress ---
    progress = _rail_progress(spot if spot is not None else last_d.get("spot"), want_s, want_l)
    if isinstance(open_pos, dict) and open_pos.get("u_pnl") is not None:
        try:
            u = float(open_pos["u_pnl"])
            pct = max(0.0, min(100.0, 100.0 * u / trail_arm)) if trail_arm else None
            progress.append(
                {
                    "id": "trail_arm",
                    "label": f"Trail 武裝（門檻 {trail_arm:.0f}）",
                    "value": round(u, 1),
                    "unit": "pt",
                    "hint": f"進度 {pct:.0f}%" if pct is not None else "",
                    "pct": pct,
                    "near": trail_arm * 0.7 <= u < trail_arm,
                }
            )
        except (TypeError, ValueError):
            pass
    progress.append(
        {
            "id": "api_day",
            "label": api_label,
            "value": api_day,
            "unit": "calls",
            "hint": api_hint,
            "pct": api_pct,
            "near": api_near,
        }
    )
    kill_pct = max(0.0, min(100.0, 100.0 * abs(min(0.0, day_pnl_f)) / kill_pts)) if kill_pts else None
    progress.append(
        {
            "id": "day_loss",
            "label": f"日已實現 {day_pnl_f:+.0f}／熔斷 −{kill_pts:.0f}",
            "value": round(day_pnl_f, 1),
            "unit": "pt",
            "hint": "接近日虧熔斷" if day_pnl_f <= -0.7 * kill_pts else "日虧控管中",
            "pct": kill_pct,
            "near": day_pnl_f <= -0.7 * kill_pts,
        }
    )

    # --- C actions ---
    action_lines = []
    for a in actions[-12:]:
        ok = a.get("ok")
        mark = "✓" if ok else ("✗" if ok is False else "·")
        bits = [mark, str(a.get("kind")), _side_zh(str(a.get("side") or ""))]
        if a.get("price") is not None:
            bits.append(f"@{_fmt_px(a.get('price'))}")
        if a.get("lot") is not None:
            bits.append(f"x{a.get('lot')}")
        if a.get("why"):
            bits.append(str(a.get("why")))
        if a.get("error"):
            bits.append(str(a.get("error"))[:80])
        action_lines.append(" ".join(bits))

    return {
        "schema": SCHEMA,
        "asof": now.isoformat(timespec="seconds"),
        "headline": headline,
        "next_action": next_act,
        "live": not dry,
        "dry_run": dry,
        "reason": reason,
        "hhmm": hm,
        "symbol": summary.get("symbol") or ledger.get("last_symbol"),
        "situation": situation,
        "narrative": narrative,
        "progress": progress,
        "actions": actions[-12:],
        "action_lines": action_lines,
        "want_s": want_s,
        "want_l": want_l,
        "spot": spot,
        "open_pos": open_pos,
        "broker_live": broker,
        "flatten_why": flatten_why,
        "regime": regime,
        "api_calls_day": api_day,
        "max_api_per_day": max_api,
        "day_pnl_pts": day_pnl_f,
        "killed": killed,
        "order_enabled": summary.get("order_enabled"),
        "auto_submit": summary.get("auto_submit"),
    }


def save_broadcast(payload: dict[str, Any], path: Path | None = None) -> Path:
    fp = path or broadcast_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(fp.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(fp)
    return fp


def load_broadcast(path: Path | None = None, *, max_age_sec: float = 180.0) -> dict[str, Any]:
    fp = path or broadcast_path()
    if not fp.exists():
        return {
            "schema": SCHEMA,
            "ok": False,
            "stale": True,
            "headline": "尚無 LIVE 播報 · 等 poll 寫入",
            "situation": ["broadcast 檔不存在"],
            "narrative": ["確認 tmf-channel poll 是否在跑"],
            "progress": [],
            "action_lines": [],
            "actions": [],
        }
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        return {
            "schema": SCHEMA,
            "ok": False,
            "stale": True,
            "headline": "播報檔讀取失敗",
            "situation": [str(e)[:160]],
            "narrative": [],
            "progress": [],
            "action_lines": [],
            "actions": [],
        }
    asof = str(data.get("asof") or "")
    stale = True
    age = None
    try:
        ts = datetime.fromisoformat(asof)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_TZ)
        age = (datetime.now(tz=_TZ) - ts).total_seconds()
        stale = age > max_age_sec
    except Exception:
        stale = True
    data["ok"] = True
    data["stale"] = stale
    data["age_sec"] = age
    if stale:
        data["headline"] = f"⚠ 播報過舊（{int(age or 0)}s）· " + str(data.get("headline") or "")
    return data


def emit_from_summary(
    summary: dict[str, Any],
    *,
    cfg: Any | None = None,
    ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = build_broadcast(summary, cfg=cfg, ledger=ledger)
    save_broadcast(payload)
    return payload
