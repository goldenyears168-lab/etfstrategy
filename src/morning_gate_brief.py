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
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from report_paths import REPORTS_ROOT  # noqa: E402
from stock_db import connect  # noqa: E402

# ── 缺口迴歸係數（凍結；定期重新校準） ──────────────────────────
# 主：台指期夜盤收缺口（TW 自身隔夜，corr 0.88、σ 0.77，週二～五）；fallback：NQ 隔夜（週一/夜盤無資料）
GAP_TXN_A, GAP_TXN_B, GAP_TXN_SIGMA = 0.13, 0.91, 0.77
GAP_A, GAP_B, GAP_SIGMA, HIT = 0.23, 1.06, 0.99, 0.73
CALIB = "2026-08 · 55–58 日 May–Jul · PIT"
TPE = timezone(timedelta(hours=8))
OUT = REPORTS_ROOT / "daily" / "morning-gate"
MORNING_BRIEF_TO = "jack7701637@gmail.com"   # 收件人（可被 env MORNING_BRIEF_TO 覆寫）


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


def _asia_early_drift(sym: str) -> float | None:
    """亞洲市場（日經 ^N225 / 韓股 ^KS11）開盤後(08:00 TW 開)到 ~08:30 的早盤漂移%。

    日韓 08:00 同開、台股 09:00 較晚 → 日韓早盤是台股開盤前最早的亞洲現貨領先讀數。
    只取『開盤後漂移』而非開盤跳空——跳空與 NQ 冗餘(日經 corr 0.83/韓 0.68、零增量)；
    早盤漂移才有增量：日經 R² +0.09（主要）、韓 +0.035（日經已吸收、次要）。網路失敗回 None。
    """
    try:
        import warnings
        warnings.filterwarnings("ignore")
        import pandas as pd
        import yfinance as yf
        from zoneinfo import ZoneInfo
        tw = ZoneInfo("Asia/Taipei")
        h = yf.Ticker(sym).history(period="5d", interval="30m")
        if h.empty:
            return None
        h.index = pd.to_datetime(h.index).tz_convert(tw)
        by = {str(d): sub for d, sub in h.groupby(h.index.date)}
        today = by[sorted(by)[-1]]
        t = today.index.strftime("%H:%M")
        opn = float(today["Open"].iloc[0])          # 08:00 TW 開盤
        pre = today["Close"][t <= "08:30"]
        if pre.empty or not opn:
            return None
        return (float(pre.iloc[-1]) / opn - 1) * 100
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] {sym} 抓取失敗: {type(exc).__name__}: {exc}")
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


def predict_gap(value: float | None, a: float = GAP_A, b: float = GAP_B,
                sigma: float = GAP_SIGMA) -> dict | None:
    if value is None:
        return None
    p = a + b * value
    return {"pred": p, "lo": p - sigma, "hi": p + sigma,
            "dir": "偏多開高" if p > 0.15 else "偏空開低" if p < -0.15 else "約平盤"}


def _tx_night_gap(session: str) -> float | None:
    """台指期夜盤 05:00 收相對前一日日盤收的隔夜缺口%（TW 自身隔夜，最準的缺口預測子）。

    週二～五可用；週一無夜盤或 FinMind after_market 08:30 前未更新 → 回 None（build_brief 自動退回 NQ）。
    """
    try:
        from datetime import date, timedelta

        from finmind_client import fetch_finmind
        sess = date.fromisoformat(session)
        rows = fetch_finmind("TaiwanFuturesDaily", "TX", sess - timedelta(days=7), sess)
        rows = rows if isinstance(rows, list) else (rows.get("data") or [])
        best: dict = {}
        for r in rows:
            if "/" in str(r.get("contract_date", "")):   # 排除 spread 合約
                continue
            k = (r["date"], r["trading_session"])
            if r["volume"] > best.get(k, (-1, None))[0]:
                best[k] = (r["volume"], r)
        am = best.get((session, "after_market"))
        pos_dates = sorted({d for (d, s) in best if s == "position" and d < session})
        if not am or not pos_dates:
            return None
        prev_close = best[(pos_dates[-1], "position")][1]["close"]
        if not prev_close:
            return None
        return (am[1]["close"] / prev_close - 1) * 100
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] 台指期夜盤缺口抓取失敗（退回 NQ）: {type(exc).__name__}: {exc}")
        return None


def build_brief(conn, session: str) -> tuple[str, str]:
    """回傳 (subject, markdown/html body)。"""
    nq = _preopen_overnight("NQ=F")
    es = _preopen_overnight("ES=F")
    nk = _asia_early_drift("^N225")   # 日經（主要亞洲確認 · 增量最強）
    ks = _asia_early_drift("^KS11")   # 韓股（次要 · 日經已大致吸收）
    txn = _tx_night_gap(session)      # 主預測子：台指期夜盤收缺口（週二～五）
    # 穩健守衛（2026-08 用 0050 現貨實際缺口驗證後放寬）：
    #   · 夜盤缺口 >8% 才擋（只攔絕對離譜的資料錯誤；7/31 曾真跳空 +6.95%，真行情不擋）
    #   · 與 NQ 明顯反向(|txn|>1.5 且反號) → 退回 NQ（處理週一：週末無夜盤、after_market 為 stale 週五資料）
    txn_ok = (txn is not None and abs(txn) <= 8.0
              and (nq is None or abs(txn) < 1.5 or (txn > 0) == (nq > 0)))
    if txn_ok:
        g = predict_gap(txn, GAP_TXN_A, GAP_TXN_B, GAP_TXN_SIGMA)
        pred_formula = (f"{GAP_TXN_A:+.2f} + {GAP_TXN_B:.2f}×台指期夜盤收缺口({txn:+.2f}%) "
                        f"· σ={GAP_TXN_SIGMA:.2f} · corr 0.88（TW 自身隔夜·最準）")
    else:
        g = predict_gap(nq)
        rej = f"；台指期夜盤缺口 {txn:+.2f}% 異常/與美期反向、已略過" if txn is not None else "；週一/夜盤無資料"
        pred_formula = (f"{GAP_A:+.2f} + {GAP_B:.2f}×NQ隔夜({nq:+.2f}%) · σ={GAP_SIGMA:.2f}"
                        f"（NQ fallback{rej}）" if nq is not None else "—")
    reg = _regime(conn)
    vix_d, vix = reg["VIX"]
    vtw_d, vtw = reg["VIXTWN"]
    vix_lo = reg["vix_median"]

    if g:
        head = f"{'🟢' if g['dir']=='偏多開高' else '🔴' if g['dir']=='偏空開低' else '⚪'} 預測開盤 {g['pred']:+.2f}%（{g['dir']}）"
        gapline = (f"≈ **{g['pred']:+.2f}%**  信賴帶 {g['lo']:+.2f}% ~ {g['hi']:+.2f}% (±1σ)\n"
                   f"   公式 {pred_formula} · 方向命中 {HIT:.0%}（{CALIB}）")
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
        "### ◆ 亞洲確認（日韓 08:00 開盤後早盤漂移 · live · 台股 09:00 較晚）",
        (f"   日經 早盤  {nk:+.2f}%   "
         + ("→ 與美期同向 · 確認" if (nq is not None and (nk > 0) == (nq > 0)) else "→ 與美期背離 · 亞洲分歧、降信心")
         + "（主要亞洲增量 R²+0.09）"
         if nk is not None else "   日經 早盤  —（抓取失敗）"),
        (f"   KOSPI 早盤 {ks:+.2f}%   (次要 · 日經已大致吸收)"
         if ks is not None else "   KOSPI 早盤 —（抓取失敗）"),
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


def _ai_interpretation(body: str) -> str | None:
    """headless `claude -p` 對機械數據寫『AI 淨判讀』。失敗回 None（本封自動退回純數據）。"""
    prompt = (
        "你是台股資深盤前分析師。下面是今天 08:30『盤前定調』的機械數據（純程式算的，不是你算的）。\n"
        "請用繁體中文寫『AI 淨判讀』，4–6 句，涵蓋：\n"
        "1) 綜合這些數字，大盤開盤怎麼看；2) 電子/台積電那條軸怎麼看；\n"
        "3) 訊號間有沒有矛盾（例如美期漲但日經跌）；4) 持現貨者要不要盤前避險/減碼。\n"
        "只根據下方數據、誠實、直接給結論、不客套寒暄、不做買賣張數建議。\n\n"
        "===== 機械數據 =====\n" + body
    )
    try:
        r = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=150, cwd="/tmp",
        )
        out = (r.stdout or "").strip()
        return out or None
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] AI 判讀失敗（退回純數據）: {type(exc).__name__}: {exc}")
        return None


def _send(subject: str, body: str, to: str) -> None:
    """直接走 Gmail SMTP 寄一封（繞過 RUN_ALERT_EMAIL，因本 brief 有自己的 gate）。"""
    from notify_email import _smtp_send
    html = ("<pre style='font-family:ui-monospace,monospace;white-space:pre-wrap'>"
            + body.replace("<", "&lt;") + "</pre>")
    _smtp_send(subject, body, html_body=html, to=to)


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
        to = os.environ.get("MORNING_BRIEF_TO") or MORNING_BRIEF_TO
        # 第一封：純數據（機械程式）
        try:
            _send(f"台股08:30盤前定調〔純數據〕· {session} · {subj.split('·')[-1].strip()}", body, to)
            print(f"✓ 寄出純數據信 → {to}")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 純數據信寄送失敗: {type(exc).__name__}: {exc}")
        # 第二封：AI 判讀（headless claude -p；失敗自動退回純數據）
        ai = _ai_interpretation(body)
        if ai:
            body_ai = ("# 🤖 AI 判讀（非決定性 · 僅供參考 · 由 claude -p 產生）\n\n"
                       + ai + "\n\n" + "=" * 40 + "\n以下為純數據（機械程式）：\n\n" + body)
        else:
            body_ai = "# 🤖 AI 判讀\n\n（AI 判讀失敗，本封自動退回純數據）\n\n" + body
        try:
            _send(f"台股08:30盤前定調〔AI判讀〕· {session}", body_ai, to)
            print(f"✓ 寄出 AI 判讀信 → {to}")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] AI 判讀信寄送失敗: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
