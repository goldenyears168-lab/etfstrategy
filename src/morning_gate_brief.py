"""台股 08:30 盤前定調簡報產生器（morning gate brief）· 不下單、只寄信.

用 08:30 當下**真 live** 的美股期貨（NQ/ES）預測台股**開盤缺口**（開盤價 vs 上次收盤），
加恐慌 regime（VIX/VIXTWN 前收）。

研究邊界（us_tw_vix_gate_study 系列，5–7月 55 日回測）：
  · 08:30 訊號預測「開盤缺口」：corr 0.79、方向命中 73% —— 強、可信。
  · 對「開盤後第一小時」：corr −0.09 —— 雜訊、常反彈。**本簡報只定調開盤缺口，不預測盤後。**

缺口迴歸（2026-08 校準 · 55 日 May–Jul · PIT）：
  台股開盤缺口% ≈ 0.23 + 1.06 × NQ隔夜% · 殘差σ ≈ 0.99%
  ⚠️ 係數會隨市場漂移，建議每季用 us_tw_vix_gate_study 系列資料重新校準。

不放費半/TSM ADR：那是**現貨指數**，08:30 只有週五收、且其半導體訊號已在 NQ（盤中連續）內。

Run: PYTHONPATH=src .venv/bin/python src/morning_gate_brief.py [--no-email]
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from report_paths import REPORTS_ROOT  # noqa: E402
from stock_db import connect  # noqa: E402

# ── 缺口迴歸係數（凍結；定期重新校準） ──────────────────────────
GAP_A, GAP_B, GAP_SIGMA, HIT = 0.23, 1.06, 0.99, 0.73
CALIB = "2026-08 · 55 日 May–Jul · PIT"
TPE = timezone(timedelta(hours=8))
OUT = REPORTS_ROOT / "daily" / "morning-gate"


def _preopen_overnight(sym: str) -> float | None:
    """NQ/ES 盤前（≤08:30 TW）相對上一 TW 交易日收盤時段（12:00–13:00）的隔夜%。

    與 us_tw_vix_gate_intraday_study 的訊號定義一致（1h、TW 時區）。網路失敗回 None。
    """
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import pandas as pd
        import yfinance as yf
        from zoneinfo import ZoneInfo
        tw = ZoneInfo("Asia/Taipei")
        h = yf.Ticker(sym).history(period="7d", interval="1h")
        if h.empty:
            return None
        h.index = pd.to_datetime(h.index).tz_convert(tw)
        by = {str(d): sub for d, sub in h.groupby(h.index.date)}
        days = sorted(by)
        today = days[-1]
        t = by[today].index.strftime("%H:%M")
        pre = by[today]["Close"][t <= "08:30"]
        if pre.empty:
            return None
        # 上一交易日的收盤時段參考（12:00–13:00）
        for d in reversed(days[:-1]):
            tp = by[d].index.strftime("%H:%M")
            ref = by[d]["Close"][(tp >= "12:00") & (tp <= "13:00")]
            if not ref.empty:
                return (float(pre.iloc[-1]) / float(ref.iloc[-1]) - 1) * 100
        return None
    except Exception as exc:  # noqa: BLE001 — 網路/資料失敗不應炸掉排程
        print(f"[warn] {sym} 盤前抓取失敗: {type(exc).__name__}: {exc}")
        return None


def _regime(conn) -> dict:
    conn.row_factory = None
    out: dict = {}
    for sym in ("VIX", "VIXTWN"):
        row = conn.execute(
            "SELECT date, close FROM market_vix_daily WHERE symbol=? ORDER BY date DESC LIMIT 1", (sym,)
        ).fetchone()
        out[sym] = (str(row[0]), float(row[1])) if row else (None, None)
    # VIX 中位（regime 高/低判準）
    vals = [float(x) for (x,) in conn.execute("SELECT close FROM market_vix_daily WHERE symbol='VIX'") if x]
    out["vix_median"] = sorted(vals)[len(vals) // 2] if vals else None
    return out


def predict_gap(nq_overnight: float | None) -> dict | None:
    if nq_overnight is None:
        return None
    p = GAP_A + GAP_B * nq_overnight
    return {"pred": p, "lo": p - GAP_SIGMA, "hi": p + GAP_SIGMA,
            "dir": "偏多開高" if p > 0.15 else "偏空開低" if p < -0.15 else "約平盤"}


def build_brief(conn, session: str) -> tuple[str, str]:
    """回傳 (subject, markdown/html body)。"""
    nq = _preopen_overnight("NQ=F")
    es = _preopen_overnight("ES=F")
    g = predict_gap(nq)
    reg = _regime(conn)
    vix_d, vix = reg["VIX"]
    vtw_d, vtw = reg["VIXTWN"]
    vix_lo = reg["vix_median"]

    if g:
        head = f"{'🟢' if g['dir']=='偏多開高' else '🔴' if g['dir']=='偏空開低' else '⚪'} 預測開盤 {g['pred']:+.2f}%（{g['dir']}）"
        gapline = (f"≈ **{g['pred']:+.2f}%**  信賴帶 {g['lo']:+.2f}% ~ {g['hi']:+.2f}% (±1σ)\n"
                   f"   公式 {GAP_A:+.2f} + {GAP_B:.2f}×NQ隔夜({nq:+.2f}%) · 方向命中 {HIT:.0%}（{CALIB}）")
    else:
        head = "⚠️ 無法取得美期，僅提供 regime 背景"
        gapline = "（美期盤前抓取失敗，缺口無法預測；見下 regime）"

    spread = f"{nq - es:+.2f}pp" if (nq is not None and es is not None) else "—"
    vix_tag = ("低·risk-on" if (vix and vix_lo and vix < vix_lo) else "偏高·留意" if vix else "—")

    lines = [
        f"# 台股盤前定調 · {session} 08:30",
        "",
        f"## {head}",
        "",
        "### ◆ 開盤缺口預測（開盤價 vs 上次收盤）",
        f"   {gapline}",
        "",
        "### ◆ Live 隔夜（08:30 當下 · 真在動）",
        f"   NQ 那斯達克期  {nq:+.2f}%   ← 科技/台積電軸" if nq is not None else "   NQ 那斯達克期  —（抓取失敗）",
        f"   ES 標普期      {es:+.2f}%   ← 大盤軸" if es is not None else "   ES 標普期      —",
        f"   NQ−ES 價差    {spread}   （NQ 弱於 ES=科技單獨被打）",
        "",
        "### ◆ Regime 背景（前收 · 慢速不變）",
        f"   VIX {vix:.2f}（{vix_tag}） @ {vix_d}" if vix else "   VIX —",
        f"   VIXTWN {vtw:.2f} @ {vtw_d}" if vtw else "",
        "",
        "### ⚠️ 使用邊界",
        "   只定調「開盤那一刻」的缺口（可信）。**開盤後第一小時 = 雜訊（55日 corr −0.09、常反彈），勿追殺。**",
        "   要避缺口 → 08:45 期貨/夜盤盤前處理；開盤後交給盤中防守。",
        "",
        "_不下單 · 僅供參考_",
    ]
    body = "\n".join(x for x in lines if x is not None)
    subj = f"台股08:30盤前定調 · {session} · " + (f"預測缺口 {g['pred']:+.2f}%" if g else "美期抓取失敗")
    return subj, body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="台股 08:30 盤前定調簡報（不下單）")
    ap.add_argument("--no-email", action="store_true", help="只印/寫檔、不寄信")
    ap.add_argument("--session", default=None, help="YYYY-MM-DD（預設今天 TPE）")
    args = ap.parse_args(argv)
    session = args.session or datetime.now(TPE).strftime("%Y-%m-%d")

    conn = connect()
    subj, body = build_brief(conn, session)
    print(subj + "\n\n" + body)

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"morning_gate_{session}.md").write_text(body, encoding="utf-8")
    (OUT / "latest.md").write_text(body, encoding="utf-8")

    if not args.no_email:
        try:
            from notify_email import send_alert
            html = "<pre style='font-family:ui-monospace,monospace'>" + body.replace("<", "&lt;") + "</pre>"
            send_alert(subj, body, html_body=html)  # 受 RUN_ALERT_EMAIL gate
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 寄信失敗（不影響簡報產出）: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
