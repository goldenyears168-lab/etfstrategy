"""dayflip-futures-short v1 · 即時訊號計算（研究規格見 FROZEN_SPEC_V1.json）.

盤前（08:44前）呼叫 build_candidates() 算出候選清單（分點過濾，不含跳空條件）；
08:45 觸發後對每個候選查即時期貨開盤價，算 fgap，交給 order 層做最終篩選/下單。

本模組只讀 DB／FinMind，不碰 Fubon session，可離線單元測試。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from statistics import mean, median

import stock_db
from order.config import load_order_config

_SPEC_PATH = (
    stock_db.PROJECT_ROOT
    / "reports/research/branch-footprint-screen/dayflip_gapup_short/FROZEN_SPEC_V1.json"
)
_UNIVERSE_PATH = (
    stock_db.PROJECT_ROOT
    / "reports/research/branch-footprint-screen/dayflip_gapup_short/stock_futures_universe.json"
)
_MEGA_PATH = (
    stock_db.PROJECT_ROOT
    / "reports/research/branch-footprint-screen/ab58_xMega_copytrade/mega_blacklist_v1.json"
)

def _dayflip_fgap_min() -> float:
    """FGAP_MIN 讀 config/order.yaml 的 strategies.dayflip-futures-short.fgap_min，
    缺 key 或讀取失敗時 fallback 回原本寫死的 0.06（2026-08-08 SSOT缺口修正，
    理由同 dayflip_short_order.py 的 _dayflip_yaml_block()）。其餘門檻常數
    （MIN_BUY_NTD/ADV_MIN_LOTS/ACC_*/HIGH_FLIP_MIN）屬於 FROZEN_SPEC_V1.json
    凍結研究規格的一部分，不是下單層執行參數，刻意不搬進 order.yaml。"""
    cfg = load_order_config()
    block = (cfg.get("strategies") or {}).get("dayflip-futures-short")
    if not isinstance(block, dict):
        return 0.06
    try:
        return float(block.get("fgap_min") or 0.06)
    except (TypeError, ValueError):
        return 0.06


FGAP_MIN = _dayflip_fgap_min()
MIN_BUY_NTD = 30_000_000.0
ADV_MIN_LOTS = 800.0
ACC_WINDOW_DAYS = 60
ACC_NET_RATIO_MAX = 0.30
ACC_MIN_WINDOW_BUY_NTD = 100_000_000.0
HIGH_FLIP_MIN = 0.40
# 2026-08-07 席位剖析新增（見 GAPUP_SHORT_SIZING.md §14席分級報告）：
# 9217×2308 / 9217×3653 為建倉型持股，flip<0.21，不應計入合格判定。
EXTRA_MANUAL_PAIRS = {("9217", "2308"), ("9217", "3653")}


@dataclass(frozen=True)
class Candidate:
    stock_id: str
    futures_symbol: str
    seats: tuple[str, ...]
    n_seats: int
    total_amt_ntd: float
    max_flip: float


def _load_spec() -> dict:
    return json.loads(_SPEC_PATH.read_text())


def _load_universe() -> dict[str, str]:
    return json.loads(_UNIVERSE_PATH.read_text())["map"]


def _load_mega() -> set[str]:
    return set(json.loads(_MEGA_PATH.read_text())["symbols"])


def futures_root(stock_id: str) -> str | None:
    """2碼期貨根代碼（供富邦即時行情 tickers(product=root) 用，非FinMind式代碼）。"""
    code = _load_universe().get(stock_id)
    if not code:
        return None
    return code if len(code) == 2 else code[:-1]


def resolve_futures_symbol(stock_id: str, as_of: str) -> tuple[str, float] | None:
    """驗證過『確實有資料』的期貨代碼 + 收盤價，供下單與訊號共用一套來源。

    stock_futures_universe.json 部分條目是舊式「根碼+1」（如 IR1），FinMind
    現行有效代碼多為「根碼+F」（IRF）。2026-08-07 發現 157/381 條目非 2 碼、
    疑似同類問題。fail-closed：兩種寫法都查不到當天資料就回傳 None——呼叫端
    必須把該候選整個排除，絕不可以拿一個沒驗證過的代碼去下單。
    """
    futmap = _load_universe()
    code = futmap.get(stock_id)
    if not code:
        return None
    tried = [code + "F" if len(code) == 2 else code]
    if len(code) > 2 and not code.endswith("F"):
        tried.append(code[:-1] + "F")

    from datetime import date as _date

    from finmind_client import fetch_finmind

    for fid in tried:
        try:
            rows = fetch_finmind("TaiwanFuturesDaily", fid, _date.fromisoformat(as_of),
                                 _date.fromisoformat(as_of), timeout=60)
        except Exception:
            continue
        day_rows = [r for r in rows
                    if r.get("trading_session") == "position"
                    and "/" not in str(r.get("contract_date", ""))
                    and float(r.get("open") or 0) > 0]
        if not day_rows:
            continue
        near = max(day_rows, key=lambda r: float(r.get("volume") or 0))
        close = float(near.get("close") or 0)
        if close > 0:
            return fid, close
    return None


def build_candidates(as_of: str, *, db_path: str | None = None) -> list[Candidate]:
    """T0=as_of 收盤後可算的候選清單（分點+席位過濾，尚未套用跳空條件）。

    as_of: 'YYYY-MM-DD'，須為已收盤的交易日（T0）。
    """
    spec = _load_spec()
    static_flip: dict[str, float] = dict(spec["seat_flip_table_frozen"]["values"])
    manual = {tuple(x) for x in spec["signal"]["step2_seat_filters"]["manual_pair_exclusion"]}
    manual |= EXTRA_MANUAL_PAIRS
    mega = _load_mega()
    futmap = _load_universe()

    path = db_path or stock_db.DEFAULT_DB_PATH
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        lo = (date.fromisoformat(as_of) - timedelta(days=200)).isoformat()
        px: dict[tuple[str, str], float] = {}
        for sid, d, c in con.execute(
            "SELECT stock_id,trade_date,close FROM stock_daily_bars "
            "WHERE source='finmind' AND trade_date BETWEEN ? AND ? AND close>0",
            (lo, as_of),
        ):
            px[(str(sid), str(d))] = float(c)

        today_events: dict[str, list[dict]] = defaultdict(list)
        for tid in static_flip:
            for d, sid, b, s in con.execute(
                "SELECT trade_date,stock_id,buy,sell FROM stock_broker_branch_daily "
                "WHERE securities_trader_id=? AND trade_date=?", (tid, as_of),
            ):
                p = px.get((str(sid), as_of))
                if p is None or not b or float(b) <= 0:
                    continue
                amt = float(b) * p
                if amt >= MIN_BUY_NTD:
                    today_events[str(sid)].append({"tid": tid, "amt": amt})

        # 60日建倉排除需要的分點逐日買賣
        br: dict[str, dict[str, dict[str, tuple]]] = defaultdict(lambda: defaultdict(dict))
        for tid in static_flip:
            for d, sid, b, s in con.execute(
                "SELECT trade_date,stock_id,buy,sell FROM stock_broker_branch_daily "
                "WHERE securities_trader_id=? AND trade_date BETWEEN ? AND ?",
                (tid, lo, as_of),
            ):
                br[tid][str(sid)][str(d)] = (float(b or 0), float(s or 0))
        cal = sorted({d for (_, d) in px if lo <= d <= as_of})
        ci = {d: i for i, d in enumerate(cal)}

        def net_ratio(tid: str, sid: str) -> float | None:
            i = ci.get(as_of)
            if i is None or i < ACC_WINDOW_DAYS:
                return None
            tb = ts = 0.0
            for d in cal[i - ACC_WINDOW_DAYS:i]:
                b, s = (br[tid].get(sid) or {}).get(d, (0.0, 0.0))
                p = px.get((sid, d))
                if p is None:
                    continue
                tb += b * p
                ts += s * p
            return None if tb < ACC_MIN_WINDOW_BUY_NTD else (tb - ts) / tb

        # 期貨 20 日均量（liquidity gate）
        futures_cache_path = (
            stock_db.PROJECT_ROOT
            / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
        )
        fut_cache = json.loads(futures_cache_path.read_text()) if futures_cache_path.exists() else {}

        out: list[Candidate] = []
        for sid, es in today_events.items():
            if sid in mega or sid.startswith("00") or sid not in futmap:
                continue
            keep = []
            for e in es:
                if (e["tid"], sid) in manual:
                    continue
                nr = net_ratio(e["tid"], sid)
                if nr is not None and nr >= ACC_NET_RATIO_MAX:
                    continue
                keep.append(e)
            if not keep or not any(static_flip.get(e["tid"], 0) >= HIGH_FLIP_MIN for e in keep):
                continue
            m = fut_cache.get(sid) or {}
            ds = sorted(m)
            adv = None
            if as_of in ds:
                i = ds.index(as_of)
                if i >= 20:
                    adv = mean([m[x][4] for x in ds[i - 20:i]])
            # fail-closed（2026-08-08 code review 修正）：as_of 不在快取裡或歷史
            # 不足 20 天，一律視為「查不到流動性資料」直接排除，不放行。原本是
            # fail-open（adv is None 時跳過檢查直接通過），發現快取停在 07-20
            # 沒人刷新，讓這個濾網靜默失效近3週——跟同檔案 resolve_futures_symbol()
            # 的 fail-closed 哲學不一致。快取新鮮度靠
            # scripts/research/refresh_dayflip_futures_daily_cache.py 維護。
            if adv is None or adv < ADV_MIN_LOTS:
                continue
            resolved = resolve_futures_symbol(sid, as_of)
            if resolved is None:
                continue  # 代碼查無當天資料，fail-closed 排除，不用未驗證代碼下單
            fid, _t0_close = resolved
            out.append(Candidate(
                stock_id=sid, futures_symbol=fid,
                seats=tuple(sorted({e["tid"] for e in keep})),
                n_seats=len({e["tid"] for e in keep}),
                total_amt_ntd=sum(e["amt"] for e in keep),
                max_flip=max(static_flip.get(e["tid"], 0) for e in keep),
            ))
    finally:
        con.close()
    out.sort(key=lambda c: -c.n_seats)
    return out


def last_close(stock_id: str, as_of: str, *, db_path: str | None = None) -> float | None:
    """T0 期貨收盤價：先查靜態快取，否則透過 resolve_futures_symbol() 驗證後取得。

    T0 收盤數小時前就已結束，FinMind TaiwanFuturesDaily 在下單當下（T+1 08:45）
    一定拿得到 as_of 的資料——查不到視為硬錯誤，由呼叫端（order 層）跳過該候選，
    不用陳舊/未驗證的代碼或估計值下單。
    """
    futures_cache_path = (
        stock_db.PROJECT_ROOT
        / "reports/research/branch-footprint-screen/dayflip_gapup_short/futures_daily_cache.json"
    )
    if futures_cache_path.exists():
        cache = json.loads(futures_cache_path.read_text())
        row = (cache.get(stock_id) or {}).get(as_of)
        if row:
            return float(row[1])
    resolved = resolve_futures_symbol(stock_id, as_of)
    return resolved[1] if resolved else None


def estimate_margin_ntd(price: float, *, margin_rate: float = 0.135, lot_size: int = 2000) -> float:
    """個股期貨保證金粗估（近似值，非官方逐檔試算表）."""
    return price * lot_size * margin_rate
