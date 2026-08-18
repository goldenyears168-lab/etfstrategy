"""每筆交易的完整軌跡與因子紀錄（append-only），以及「符合預期」的量化定義。

為什麼需要這個
--------------
現有的 live log 是**每輪一張快照**，不是**每筆交易一條軌跡**：要回答「第 37 筆
到底怎麼走的、當下盤口長怎樣、哪些因子在作用」得自己把幾百行 JSON 重新縫起
來，而且盤口（五檔）根本不在下單路徑裡——它由另一個行程寫到另一個檔案，從來
沒有跟成交對齊過。所有先前的診斷都是**事後從逐筆資料反推**，不是系統當下真正
看到的東西。這個模組補上那條軌跡。

「符合預期」能怎麼定義——以及它的極限
------------------------------------
基準來自 60 日 tick 回放的實測 markout 曲線（4,073 筆，fill_model="through"，
見 reports/research/channel_lab/tmf_markout_exit_futility.json）。關鍵在於同時
記下**每筆的離散程度**，而不只是平均：

    視野    期望 markout    每筆標準差    訊噪比
     1分       +2.76          ±47.4       0.058
     5分       +2.41          ±86.4       0.028
    10分       +1.72          ±114        0.015

單筆交易要偏離到 2σ 才稱得上「明顯不符預期」——1 分鐘內要走 95 點。等那個
訊號出現，交易早就結束了。**所以本模組計算並記錄單筆 z 值，但明確不把它接到
任何出場動作上**：那會是第五次重演「in-sample 好看、樣本外反轉」的劇本
（有界停損、關 struct_break、換錨點、秒級 OFI 濾網已經各失敗一次）。

可以支撐動作的是**滾動窗口**：20 筆的平均標準誤是 47.4/√20 ≈ 10.6 點，所以
「最近 20 筆的平均 markout 偏離期望 21 點以上」是真正的 2σ 事件。那不是「這筆
不對勁」，是「這個策略不像它自己了」——對應的動作是**停止開新倉**，不是砍現有
部位。rolling_conformance() 算的就是這個。

本模組對下單路徑是唯讀且 fail-safe：任何例外都吞掉，絕不讓寫紀錄這件事害到
對帳或送單。
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Taipei")

#: 實測基準：{持有分鐘: (期望 markout 點數, 每筆標準差)}
#: 標準差 = 報告裡的 se × √n（se 是平均值的標準誤，單筆離散要還原回去）
BASELINE_MARKOUT: dict[int, tuple[float, float]] = {
    1: (2.760, 47.4),
    2: (1.696, 60.7),
    5: (2.407, 86.4),
    10: (1.724, 114.1),
    20: (-0.045, 154.8),
    30: (0.333, 185.0),
    60: (-2.042, 254.2),
}
BASELINE_SOURCE = "tmf_markout_exit_futility.json · 60d · 4073 trades · fill_model=through"
#: 滾動窗口大小與告警門檻（σ）。20 筆 → SE≈10.6 點
ROLLING_N = 20
ROLLING_SIGMA_ALERT = 2.0
#: 滾動偵測用 1 分鐘視野：它的訊噪比最好（2.76/47.4），視野越長離散度長得
#: 比訊號快，檢定力反而掉。h1 在 n=20 時 SE≈10.6 點，2σ 門檻是平均 −18.5 點；
#: 同樣 n 在 h5 的 SE 是 19.3 點，連「20 筆平均 −20 點」都只有 z=−1.16。
ROLLING_HORIZON_MIN = 1


def journal_path(day: str | None = None) -> Path:
    try:
        import stock_db

        root = Path(stock_db.LOGS_DIR)
    except Exception:  # noqa: BLE001
        root = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or Path.home() / "goldenstocks-data") / "logs"
    d = root / "intraday"
    d.mkdir(parents=True, exist_ok=True)
    stamp = day or datetime.now(tz=_TZ).strftime("%Y%m%d")
    return d / f"tmf_trade_journal_{stamp}.jsonl"


def _interp_baseline(hold_min: float) -> tuple[float, float] | None:
    """線性內插期望值與標準差；超出 60 分就沿用 60 分那格。"""
    if hold_min < 0:
        return None
    ks = sorted(BASELINE_MARKOUT)
    if hold_min <= ks[0]:
        return BASELINE_MARKOUT[ks[0]]
    if hold_min >= ks[-1]:
        return BASELINE_MARKOUT[ks[-1]]
    for a, b in zip(ks, ks[1:]):
        if a <= hold_min <= b:
            w = (hold_min - a) / (b - a)
            m0, s0 = BASELINE_MARKOUT[a]
            m1, s1 = BASELINE_MARKOUT[b]
            return (m0 + w * (m1 - m0), s0 + w * (s1 - s0))
    return None


def conformance(*, side: str, entry_px: float, spot: float, hold_min: float) -> dict[str, Any]:
    """單筆的「符合預期」量化——**只記錄，不觸發動作**（見模組 docstring）。"""
    base = _interp_baseline(hold_min)
    if base is None or side not in ("L", "S"):
        return {"ok": False, "reason": "no_baseline"}
    expected, sd = base
    actual = (spot - entry_px) if side == "L" else (entry_px - spot)
    z = (actual - expected) / sd if sd > 0 else None
    return {
        "ok": True,
        "hold_min": round(hold_min, 2),
        "actual_pts": round(actual, 1),
        "expected_pts": round(expected, 2),
        "per_trade_sd": round(sd, 1),
        "z": None if z is None else round(z, 3),
        # 明確標註：單筆 z 的訊噪比是 0.058，不足以支撐出場決策
        "actionable": False,
        "note": "single-trade z is noise-dominated (SNR≈0.06); use rolling_conformance for action",
    }


def record(event: str, payload: dict[str, Any], *, day: str | None = None) -> None:
    """Append 一行。對下單路徑 fail-safe：任何錯誤都吞掉。"""
    try:
        row = {"ts": datetime.now(tz=_TZ).isoformat(timespec="milliseconds"),
               "event": event, **payload}
        with journal_path(day).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 — 寫紀錄永遠不該害到對帳
        pass


def rolling_conformance(markouts: list[float], *, horizon_min: int = ROLLING_HORIZON_MIN,
                        n: int = ROLLING_N) -> dict[str, Any]:
    """最近 n 筆的平均 markout vs 期望——**這一層才有統計檢定力**。

    ``markouts``：每筆在 ``horizon_min`` 分鐘時的實際 markout（點數），時間序。
    回傳的 ``breached`` 為真時，對應的動作是**停止開新倉**（策略不像自己了），
    不是砍掉手上的部位——後者是單筆決策，本來就沒有檢定力。
    """
    base = BASELINE_MARKOUT.get(horizon_min)
    if base is None or len(markouts) < n:
        return {"ok": False, "reason": "insufficient", "have": len(markouts), "need": n}
    expected, sd = base
    win = markouts[-n:]
    mean = sum(win) / n
    se = sd / math.sqrt(n)
    z = (mean - expected) / se if se > 0 else 0.0
    return {
        "ok": True, "n": n, "horizon_min": horizon_min,
        "mean_actual": round(mean, 2), "expected": round(expected, 2),
        "se": round(se, 2), "z": round(z, 2),
        "breached": z <= -ROLLING_SIGMA_ALERT,
        "action_if_breached": "halt_new_entries",
        "baseline_source": BASELINE_SOURCE,
    }
