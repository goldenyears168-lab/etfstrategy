"""dayflip-futures-short v1 · order sleeve（單口、單筆、保證金<=30萬）.

規格：reports/research/branch-footprint-screen/dayflip_gapup_short/FROZEN_SPEC_V1.json
研究狀態：G2 OOS holdout 尚未過關（前推僅 5/40 訊號日）。2026-08-07 使用者明確
指示直接建置真實下單管道並授權跳過 G2；同時明確要求「建好就直接能送實單，不要
預設 dry-run」——本模組因此不比照其餘 sleeve 的 dry-run-first 慣例。

安全閥仍然存在且不可繞過：
  - live_submit_guard.assert_live_submit_allowed()：ORDER_MASTER_ENABLED=0 時
    即使呼叫端傳 dry_run=False 也會 fail-closed（見 fubon_futopt_orders.py）
  - 本檔自己的 sleeve 旗標 ORDER_DAYFLIP_SHORT_AUTO_SUBMIT（見 config_flags()）
  - 保證金硬上限 30 萬、1 口、每個交易日只進場一次

單一交易日狀態機（存於 data/order/dayflip_short_ledger.json）：
  idle → (08:45 觸發) → no_signal | entered → covered | force_closed
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import stock_db
from order.config import load_order_config
from order.dayflip_short_signal import (
    FGAP_MIN,
    Candidate,
    build_candidates,
    estimate_margin_ntd,
    futures_root,
    last_close,
)
from order.fubon_futopt_orders import (
    FutOptResolvedOrder,
    get_futopt_order_results,
    is_tmf_acct_symbol,  # noqa: F401  (re-export for callers needing TMF check)
    pick_futopt_account,
    place_futopt_order,
)
from order.fubon_session import FubonSession
from order.json_state import atomic_write_json, load_json_or_default

_TZ = ZoneInfo("Asia/Taipei")
_LEDGER_PATH = stock_db.PROJECT_ROOT / "data/order/dayflip_short_ledger.json"
_TRUE = frozenset({"1", "true", "yes", "on"})


def _dayflip_yaml_block() -> dict[str, Any]:
    """讀 config/order.yaml 的 strategies.dayflip-futures-short 區塊。

    2026-08-08 code review 發現：本檔原本完全不讀 config/order.yaml，
    MARGIN_CAP_NTD/COVER_TARGET_PCT/FORCE_CLOSE_HHMM 等常數是寫死在 python 裡，
    剛好跟 yaml 裡的數字一樣，但改 yaml 對這個 sleeve 完全沒有作用——是個真的
    SSOT 缺口。這裡改成在 module 載入時讀一次 yaml，缺 key 或讀取失敗時 fallback
    回原本寫死的數值（下面每個常數都有），不會因為 yaml 壞掉而改變行為。
    """
    cfg = load_order_config()
    block = (cfg.get("strategies") or {}).get("dayflip-futures-short")
    return block if isinstance(block, dict) else {}


_DAYFLIP_YAML = _dayflip_yaml_block()

MARGIN_CAP_NTD = float(_DAYFLIP_YAML.get("margin_cap_twd") or 300_000)
LOT_SIZE = 2000  # 個股期貨每口股數，市場常數非策略參數，不進 yaml
LOTS = int(_DAYFLIP_YAML.get("lots") or 1)
COVER_TARGET_PCT = float(_DAYFLIP_YAML.get("cover_target_pct") or 0.02)
ENTRY_WINDOW_START = str(_DAYFLIP_YAML.get("entry_at") or "08:45")
ENTRY_WINDOW_END = str(
    _DAYFLIP_YAML.get("entry_window_end") or "09:05"
)  # 觸發偵測允許的最晚時間（避免錯過開盤仍硬闖）
FORCE_CLOSE_HHMM = str(_DAYFLIP_YAML.get("force_close_at") or "13:40")

# 比照 detach-gate 的 frozen rule string 慣例（見 config/order.yaml 的 freeze_rule）：
# 把散落在 dayflip_short_signal.py／本檔的門檻常數濃縮成一條可稽核、可grep的字串，
# 寫進 ledger 每一列，讓事後複盤能直接知道「這筆記錄是在哪組門檻下產生的」。任何門檻
# 常數變動時，連帶把版本號(dfs<N>)遞增——不做語意比對，純粹靠人工同步維護。
FREEZE_RULE = "dfs1_g6_buy30m_adv800_acc60r30_hf40_m30w_l1_cov2_e0845-0905_fc1340"


def _hm_now() -> str:
    return datetime.now(tz=_TZ).strftime("%H:%M")


def _today() -> str:
    return datetime.now(tz=_TZ).strftime("%Y-%m-%d")


def in_dayflip_short_trade_window(hm: str | None = None, *, weekday: int | None = None) -> bool:
    """``weekday``: ``date.weekday()`` convention, Monday=0 .. Sunday=6.

    2026-08-08：搬進 KeepAlive worker（見 dayflip_short_worker_loop.py）前，唯一擋週末
    的防線是 shell launcher 的 `date '+%u'` 檢查，只在 StartInterval 冷啟動時每次跑一次；
    KeepAlive worker 常駐後內部 loop 不會每個 tick 重新跑 shell，必須自己知道週末不開盤。
    TMF 的 in_tmf_trade_window() 就是在同一天（2026-08-08）發現只查 HH:MM、沒查星期幾，
    導致週六 login-thrash（見該函式 docstring）——這裡直接把星期檢查內建進來，不重蹈。
    TAIFEX 個股期貨日盤週一至週五，沒有夜盤（跟 TMF 不同，不用管跨夜尾段）。
    """
    hm = hm or _hm_now()
    if weekday is None:
        weekday = datetime.now(tz=_TZ).weekday()
    if weekday > 4:
        return False
    return "08:44" <= hm <= "13:41"


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in _TRUE


def sleeve_auto_submit_enabled() -> bool:
    """本 sleeve 專屬旗標。預設 1（依使用者 2026-08-07 明確指示，不同於其餘 sleeve 的
    dry-run-first 慣例）。ORDER_MASTER_ENABLED 仍是唯一無法被此旗標繞過的總開關。"""
    raw = os.environ.get("ORDER_DAYFLIP_SHORT_AUTO_SUBMIT", "1")
    return str(raw).strip().lower() in _TRUE


@dataclass
class LedgerState:
    date: str
    status: str = "idle"  # idle|no_signal|entered|covered|force_closed|error
    stock_id: str | None = None
    futures_symbol: str | None = None
    entry_order_no: str | None = None
    entry_price: float | None = None
    cover_order_no: str | None = None
    cover_target_price: float | None = None
    fgap_pct: float | None = None
    margin_ntd: float | None = None
    margin_source: str | None = None  # "broker_api" | "estimate_fallback"
    last_action: str | None = None
    updated_at: str | None = None
    rule_version: str | None = None  # FREEZE_RULE at time of write，供事後複盤歸因


def load_ledger() -> LedgerState:
    today = _today()
    raw = load_json_or_default(_LEDGER_PATH, {})
    if raw.get("date") == today:
        return LedgerState(**{**LedgerState(date=today).__dict__, **raw})
    return LedgerState(date=today)


def save_ledger(state: LedgerState) -> None:
    state.updated_at = datetime.now(tz=_TZ).isoformat()
    state.rule_version = FREEZE_RULE
    atomic_write_json(_LEDGER_PATH, asdict(state), indent=1)


def query_real_margin_ntd(session: FubonSession, account: Any, live_symbol: str) -> float | None:
    """用富邦 futopt.query_estimate_margin() 查真實保證金（優於 estimate_margin_ntd
    的價格×2000×13.5%粗估）。2026-08-07 實測此API存在且盤中應回傳真實數字；
    非交易時段測試回傳 estimate_margin=0.0（無即時報價可算），視為「查不到」，
    由呼叫端 fallback 回粗估，不當成 0 元使用。"""
    from order.fubon_futopt_orders import build_futopt_order

    try:
        order = build_futopt_order(FutOptResolvedOrder(
            symbol=live_symbol, buy_sell="Sell", lot=LOTS, price=None,
            price_type="market", time_in_force="ioc", order_type="New",
            market_type="future", user_def="dfmargin",
        ))
        res = session.sdk.futopt.query_estimate_margin(account, order)
    except Exception:
        return None
    ok = getattr(res, "is_success", None)
    if ok is None:
        ok = getattr(res, "isSuccess", False)
    if not ok:
        return None
    data = getattr(res, "data", None)
    margin = getattr(data, "estimate_margin", None)
    try:
        margin = float(margin) if margin is not None else None
    except (TypeError, ValueError):
        return None
    return margin if margin and margin > 0 else None


def resolve_live_futures_symbol(session: FubonSession, root: str) -> tuple[str, str] | None:
    """即時解析富邦／Fugle 行情＋下單用的完整月合約代碼（如 CYFH6）。

    ⚠️ 這不是 FinMind 式代碼（CYF）——兩者是不同商業體系。2026-08-07 實測發現
    Fugle marketdata 的 intraday.candles/quote 對 FinMind 式代碼一律 404，
    必須先用 tickers(product=root) 查出當下真正在交易的完整月合約代碼。
    邏輯比照 tmf_channel_marketdata.resolve_front_symbol，參數化任意 2 碼根代碼。
    """
    from order.tmf_channel_marketdata import _ensure_realtime_once

    _ensure_realtime_once(session)
    fut = session.sdk.marketdata.rest_client.futopt
    data = fut.intraday.tickers(type="FUTURE", exchange="TAIFEX", product=root)
    rows = data if isinstance(data, list) else (data.get("data") or data.get("tickers") or [])
    today = _today()
    cands = []
    for r in rows or []:
        sym = str(r.get("symbol") or "")
        end = str(r.get("endDate") or r.get("end_date") or "")[:10]
        name = str(r.get("name") or sym)
        if not sym.startswith(root):
            continue
        if end and end < today:
            continue
        cands.append((end or "9999-99-99", sym, name))
    if not cands:
        return None
    cands.sort()
    _end, sym, name = cands[0]
    return sym, name


def fetch_open_price(session: FubonSession, live_symbol: str) -> float | None:
    """T+1 08:45 期貨開盤（第一根 1 分鐘 K 的 open）。live_symbol 須為已解析的
    完整月合約代碼（resolve_live_futures_symbol 的回傳值），不是 FinMind 式代碼。"""
    from order.tmf_channel_marketdata import fetch_1m_bars

    bars = fetch_1m_bars(session, live_symbol)
    day_bars = [b for b in bars if b.get("t", "").split("T")[-1] >= "08:45"]
    if not day_bars:
        return None
    return float(day_bars[0]["o"])


def fetch_last_price(session: FubonSession, live_symbol: str) -> float | None:
    from order.tmf_channel_marketdata import fetch_1m_bars

    bars = fetch_1m_bars(session, live_symbol)
    if not bars:
        return None
    return float(bars[-1]["c"])


def pick_signal(session: FubonSession, candidates: list[Candidate], t0: str) -> dict[str, Any] | None:
    """對每個候選解析即時月合約代碼、查開盤價、算 fgap，回傳「跳空最小且保證金
    <=上限」的合格候選。scored 項目的 live_symbol 才是後續下單要用的代碼。

    排序理由：single-pick 研究（GAPUP_SHORT_SIZING.md）顯示「跳空最小」的合格標的
    在 11 種排序法則中勝率最高(74.3%)、MaxDD 最小(-8.9%)，優於隨機/其餘任何排序。
    """
    account = pick_futopt_account(session)
    scored = []
    for c in candidates:
        t0_close = last_close(c.stock_id, t0)
        if t0_close is None or t0_close <= 0:
            continue
        root = futures_root(c.stock_id)
        if not root:
            continue
        resolved = resolve_live_futures_symbol(session, root)
        if resolved is None:
            continue
        live_symbol, live_name = resolved
        open_px = fetch_open_price(session, live_symbol)
        if open_px is None or open_px <= 0:
            continue
        fgap = open_px / t0_close - 1
        if fgap < FGAP_MIN:
            continue
        real_margin = query_real_margin_ntd(session, account, live_symbol)
        margin = real_margin if real_margin is not None else estimate_margin_ntd(open_px)
        margin_source = "broker_api" if real_margin is not None else "estimate_fallback"
        if margin > MARGIN_CAP_NTD:
            continue
        scored.append(dict(candidate=c, live_symbol=live_symbol, live_name=live_name,
                           open_px=open_px, t0_close=t0_close, fgap=fgap, margin=margin,
                           margin_source=margin_source))
    if not scored:
        return None
    scored.sort(key=lambda s: s["fgap"])
    return scored[0]


def _place_cover_order(
    session: FubonSession, account: Any, live_symbol: str, entry_price: float, live: bool,
) -> tuple[float, dict[str, Any]] | None:
    """下-2%限價回補單。失敗時吞掉例外回傳 None，呼叫端維持 cover_order_no=None，
    交給下一次 poll 的 entered 分支重試（見 reconcile_once）。不重新拋出例外，
    避免整個 process 崩潰導致已經送出的進場單在 ledger 上失去追蹤。"""
    cover_price = round(entry_price * (1 - COVER_TARGET_PCT), 1)
    cover_order = FutOptResolvedOrder(
        symbol=live_symbol, buy_sell="Buy", lot=LOTS,
        price=cover_price, price_type="limit", time_in_force="rod",
        order_type="Close", market_type="future", user_def="dfshortcv",
        session_date=_today(),
    )
    try:
        cover_res = place_futopt_order(session, cover_order, acc=account, dry_run=not live)
    except Exception:  # noqa: BLE001
        return None
    return cover_price, cover_res


def reconcile_once(
    *,
    dry_run: bool | None = None,
    override_hm: str | None = None,
    use_session_pool: bool = False,
    session: FubonSession | None = None,
) -> dict[str, Any]:
    """單次 reconcile。dry_run=None 時依 sleeve_auto_submit_enabled() 決定。

    override_hm：測試專用，模擬「現在是 HH:MM」（例如 08:45），繞過真實時鐘，
    讓使用者能在盤前完整走一次候選→查報價→（dry-run）下單→回補判斷的流程，
    不用真的等到開盤才能測試。正式排程呼叫時不會傳這個參數。

    use_session_pool：2026-08-08 加，KeepAlive worker（dayflip_short_worker_loop.py）
    傳 True 重用長駐 Fubon session，取代原本每次呼叫都 connect_fubon() 全新登入的
    StartInterval 冷啟動路徑；跟 order.tmf_channel_order.reconcile_once 同一套手法。
    ``session``：測試/手動注入，優先於 use_session_pool。
    """
    state = load_ledger()
    hm = override_hm or _hm_now()
    live = (not dry_run) if dry_run is not None else sleeve_auto_submit_enabled()
    result: dict[str, Any] = {"hm": hm, "state_before": state.status, "live": live}

    if state.status in ("no_signal", "covered", "force_closed", "error"):
        result["action"] = "noop_terminal"
        return result

    def _acquire_session() -> FubonSession:
        nonlocal session
        if session is not None:
            return session
        if use_session_pool:
            from order.dayflip_short_session_pool import get_fubon_session

            session = get_fubon_session()
            return session
        from order.fubon_session import connect_fubon

        session = connect_fubon()
        return session

    if state.status == "idle":
        if hm < ENTRY_WINDOW_START:
            result["action"] = "noop_too_early"
            return result
        if hm > ENTRY_WINDOW_END:
            state.status = "no_signal"
            state.last_action = f"missed_entry_window(hm={hm})"
            save_ledger(state)
            result["action"] = "no_signal_window_missed"
            return result

        t0 = _prior_trading_day()
        candidates = build_candidates(t0)
        if not candidates:
            state.status = "no_signal"
            state.last_action = "no_candidates_from_t0_screen"
            save_ledger(state)
            result["action"] = "no_signal_no_candidates"
            return result

        session = _acquire_session()
        picked = pick_signal(session, candidates, t0)
        if picked is None:
            state.status = "no_signal"
            state.last_action = "no_candidate_met_gap_or_margin"
            save_ledger(state)
            result["action"] = "no_signal_gap_not_met"
            return result

        c: Candidate = picked["candidate"]
        live_symbol = picked["live_symbol"]
        entry_price = picked["open_px"]
        account = pick_futopt_account(session)
        entry_order = FutOptResolvedOrder(
            symbol=live_symbol, buy_sell="Sell", lot=LOTS,
            price=None, price_type="market", time_in_force="ioc",
            order_type="New", market_type="future", user_def="dfshort",
            session_date=_today(),
        )
        try:
            entry_res = place_futopt_order(session, entry_order, acc=account, dry_run=not live)
        except Exception as ex:  # noqa: BLE001
            # 進場單送出本身拋例外（尚未寫入任何持倉欄位）——維持 idle 讓下一次
            # poll 重新嘗試是安全的。刻意不 raise：讓整個 process 因未捕捉例外
            # 崩潰，會讓 shell launcher 的 email alert 比對不到任何已知 action
            # 字串而完全靜默（見 launcher template 的告警邏輯，已同步修正成
            # 非零 exit 也會通知，但這裡仍以不崩潰為第一道防線）。
            state.last_action = f"entry_order_exception:{ex}"
            save_ledger(state)
            result["action"] = "entry_exception"
            result["error"] = str(ex)
            return result

        state.stock_id = c.stock_id
        state.futures_symbol = live_symbol  # 富邦/Fugle 完整月合約代碼，非 FinMind 式
        state.entry_price = entry_price
        state.fgap_pct = round(picked["fgap"] * 100, 3)
        state.margin_ntd = round(picked["margin"])
        state.margin_source = picked["margin_source"]
        state.entry_order_no = str((entry_res.get("data") or {}).get("order_no") or "") or None
        if not entry_res.get("ok"):
            state.status = "error"
            state.last_action = f"entry_failed:{entry_res.get('message')}"
            save_ledger(state)
            result["action"] = "entry_failed"
            result["entry_res"] = entry_res
            return result

        # ⚠️ 進場單已確認送出——立刻轉 entered 並存檔，即使接下來的回補單
        # 下單失敗或 process 崩潰，下一次 poll 也會從 entered 狀態接手（見下方
        # entered 分支對 cover_order_no is None 的補下重試邏輯），不會誤判成
        # idle 而重複送出第二張進場單，也不會讓這筆真實部位從 ledger 上消失。
        state.status = "entered"
        state.last_action = "entry_placed_cover_pending"
        save_ledger(state)

        cover_placed = _place_cover_order(session, account, live_symbol, entry_price, live)
        if cover_placed is None:
            state.last_action = "entry_placed_cover_failed_will_retry"
            save_ledger(state)
            result.update(action="entered_cover_pending", entry_res=entry_res,
                          picked_stock=c.stock_id, fgap_pct=state.fgap_pct,
                          margin_ntd=state.margin_ntd)
            return result

        cover_price, cover_res = cover_placed
        state.cover_target_price = cover_price
        state.cover_order_no = str((cover_res.get("data") or {}).get("order_no") or "") or None
        state.last_action = "entered_and_cover_placed"
        save_ledger(state)
        result.update(action="entered", entry_res=entry_res, cover_res=cover_res,
                      picked_stock=c.stock_id, fgap_pct=state.fgap_pct,
                      margin_ntd=state.margin_ntd)
        return result

    if state.status == "entered":
        session = _acquire_session()
        account = pick_futopt_account(session)

        # 進場後回補單曾經下單失敗（cover_order_no 是 None）——每次 poll 都補試
        # 一次。就算一直失敗也不影響安全性：force-close 不依賴 cover_order_no
        # 存在，13:40 到了照樣會嘗試強制平倉兜底（見下方）。
        if not state.cover_order_no and state.futures_symbol and state.entry_price:
            cover_placed = _place_cover_order(session, account, state.futures_symbol,
                                              state.entry_price, live)
            if cover_placed is not None:
                cover_price, cover_res = cover_placed
                state.cover_target_price = cover_price
                state.cover_order_no = str((cover_res.get("data") or {}).get("order_no") or "") or None
                state.last_action = "cover_retry_succeeded"
            else:
                state.last_action = "cover_retry_failed"
            save_ledger(state)

        # 先查回補限價單是否已成交，不論是否已過 13:40 強制平倉時間——避免
        # 邊界 race：回補單剛好在上一次 poll 跟這次 poll 之間成交，若不先查
        # 就直接送強制平倉市價單，會對著已經平倉的部位再送一張 Buy/Close，
        # 行為未經驗證（取決於富邦新倉/平倉語意）。查詢失敗一律視為「尚未
        # 確認成交」繼續往下走，維持 fail-safe：只要時間到了還是會送強制
        # 平倉，不會因為查詢失敗而漏平倉。
        query_error: str | None = None
        try:
            order_results = get_futopt_order_results(session, acc=account)
        except Exception as ex:  # noqa: BLE001
            order_results = None
            query_error = str(ex)

        if order_results is not None and _order_appears_filled(order_results, state.cover_order_no):
            state.status = "covered"
            state.last_action = "cover_filled"
            save_ledger(state)
            result["action"] = "covered"
            return result

        if hm >= FORCE_CLOSE_HHMM:
            close_order = FutOptResolvedOrder(
                symbol=state.futures_symbol, buy_sell="Buy", lot=LOTS,
                price=None, price_type="market", time_in_force="ioc",
                order_type="Close", market_type="future", user_def="dfshortfc",
                session_date=_today(),
            )
            try:
                close_res = place_futopt_order(session, close_order, acc=account, dry_run=not live)
            except Exception as ex:  # noqa: BLE001
                # 強制平倉單送出失敗——status 刻意維持 entered（絕不誤標成
                # force_closed），下一次 poll（若還在 13:41 窗口內）會重試；
                # 就算窗口關閉，dayflip-short-watch（13:50 檢查點）看到過了
                # 13:41 還是 entered 會標記成最嚴重異常，這是設計好的最後防線。
                state.last_action = f"force_close_exception:{ex}"
                save_ledger(state)
                result["action"] = "force_close_exception"
                result["error"] = str(ex)
                return result
            state.status = "force_closed"
            state.last_action = "force_closed_at_1340"
            save_ledger(state)
            result.update(action="force_closed", close_res=close_res)
            return result

        if order_results is None:
            result["action"] = "poll_query_failed"
            result["error"] = query_error
            return result

        result["action"] = "waiting_cover"
        return result

    result["action"] = "noop_unknown_state"
    return result


def _order_appears_filled(results: list[Any], order_no: str | None) -> bool:
    if not order_no:
        return False
    for r in results:
        no = str(getattr(r, "order_no", "") or getattr(r, "orderNo", "") or "")
        if order_no not in no:
            continue
        status = str(getattr(r, "status", "") or getattr(r, "status_code", "") or "").lower()
        filled_qty = getattr(r, "filled_qty", None) or getattr(r, "filledQty", None)
        if "filled" in status or "matched" in status or "成交" in status:
            return True
        try:
            if filled_qty is not None and int(filled_qty) >= LOTS:
                return True
        except (TypeError, ValueError):
            pass
    return False


def _prior_trading_day() -> str:
    """T0＝執行日之前最近一個有交易資料的日期，直接查 DB（非猜週末），
    正確處理國定連假（不只是週六日）。"""
    import sqlite3

    today = _today()
    con = sqlite3.connect(f"file:{stock_db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT MAX(trade_date) FROM stock_daily_bars "
            "WHERE source='finmind' AND trade_date<?", (today,),
        ).fetchone()
    finally:
        con.close()
    if not row or not row[0]:
        raise RuntimeError(f"找不到 {today} 之前的交易日資料，無法判定 T0")
    return str(row[0])
