#!/usr/bin/env python3
"""每日籌碼簡報 —— 偏多／偏空名單 ＋ 今日回顧 ＋ 提醒事項，收盤後寄一封信。

三段結構：

  A. **今日回顧**  用昨日籌碼分數對今日實際報酬做五分位驗算，並寫進
     ``chip_score_forward_track``（前瞻樣本外紀錄，設計凍結於 2026-08-23）。
  B. **明日名單**  用今日籌碼算 v4 連續分數，列出兩端各 N 檔。
  C. **提醒事項**  固定風險聲明。**這不是買賣建議**——可執行邊際仍低於成本。

⚠️ **時點**：籌碼於 T 日約 21:00 才公布，故最早可行動時點是 **T+1 開盤**。
本簡報的一切報酬口徑都以 open(T+1) 為起點；用 close(T) 起算是偷看。

⚠️ **分數方向**：``score`` 越高＝越偏空、越低＝越偏多（沿用 branch_score 的
「正＝偏空」慣例）。改動任何排序前先確認這點，弄反會讓整封信的結論顛倒。

用法::

    # 看內容不寄信
    PYTHONPATH=src .venv/bin/python scripts/research/run_chip_daily_brief.py --dry-run

    # 正常（launchd 每日 21:00 走這條）
    PYTHONPATH=src .venv/bin/python scripts/research/run_chip_daily_brief.py
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

ROOT = Path(__file__).resolve().parents[2]
PY = str(ROOT / ".venv" / "bin" / "python")

MIN_VOL_LOTS = 500
MIN_CLOSE = 10.0
# 價格多來源；同一 stock-day 取排名最小者（finmind 最完整、其餘補全市場）
PRICE_RANK = {"finmind": 0, "twse_mi_index": 1, "tpex_daily": 2}

# v4 的五個分項：欄名 → (人看得懂的名稱, 正值代表的方向)
COMPONENTS = {
    "s_z1": "Δ借券賣出餘額",
    "s_zp": "借券佔股本水位",
    "s_zu": "Δ借券使用率",
    "s_zf": "借券費率水位",
    "s_z6": "分點買賣家數差",
}


def _mod(name: str):
    return SourceFileLoader(name, str(ROOT / "scripts" / "research" / f"{name}.py")).load_module()


# ---------------------------------------------------------------- 資料補檔


def refresh(d: str, log: list[str]) -> None:
    """把當日籌碼與全市場價格補進 DB。任一步失敗只記錄不中斷——
    寧可寄一封標明「某來源缺」的信，也不要整封不寄。"""
    since = (date.fromisoformat(d) - timedelta(days=6)).isoformat()
    steps = [
        ("借券餘額／借券賣出餘額／融券", [
            PY, str(ROOT / "scripts" / "backfill_stock_chip_extras.py"),
            "--stock-ids", "ALL", "--recent-days", "6",
            "--skip-dispersion", "--skip-daytrade-fix"]),
        ("借券費率 t13sa710", [
            PY, str(ROOT / "scripts" / "backfill_twse_sbl_fee.py"),
            "--start-year", d[:4], "--end-year", d[:4]]),
        ("上市全市場日線", [
            PY, str(ROOT / "scripts" / "backfill_twse_daily_prices.py"),
            "--start", since, "--end", d]),
        ("上櫃全市場日線", [
            PY, str(ROOT / "scripts" / "backfill_tpex_daily_prices.py"),
            "--start", since, "--end", d]),
        ("集保股權分散（週）", [
            PY, str(ROOT / "scripts" / "backfill_tdcc_dispersion.py"), "--weekly"]),
    ]
    for label, cmd in steps:
        try:
            # 必須繼承 os.environ —— GOLDENSTOCKS_DATA_DIR 決定 DB 路徑，
            # 用白名單 env 會讓補檔靜默寫進 repo 內的空 DB。
            env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
            r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                               timeout=1500, env=env)
            log.append(f"{'✓' if r.returncode == 0 else '✗'} {label}"
                       + ("" if r.returncode == 0 else f"（exit={r.returncode}）"))
        except Exception as exc:  # noqa: BLE001
            log.append(f"✗ {label}（{type(exc).__name__}）")


def refresh_prices_only(d: str) -> None:
    """只補當日日線——用來判斷「今天到底有沒有開市」，不動籌碼。"""
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    for script in ("backfill_twse_daily_prices.py", "backfill_tpex_daily_prices.py"):
        try:
            subprocess.run([PY, str(ROOT / "scripts" / script), "--start", d, "--end", d],
                           cwd=ROOT, capture_output=True, text=True, timeout=600, env=env)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- 讀資料


def prices(dates: list[str]) -> pd.DataFrame:
    """多來源日線去重（同 stock-day 只留來源排名最小的一列）。"""
    q = ",".join("?" * len(dates))
    df = pd.read_sql_query(
        f"""SELECT stock_id, trade_date, source, open, high, low, close,
                   volume/1000.0 AS vol
              FROM stock_daily_bars WHERE trade_date IN ({q}) AND close IS NOT NULL""",
        connect_ro(), params=dates)
    if df.empty:
        return df
    df["rk"] = df.source.map(PRICE_RANK).fillna(9)
    return (df.sort_values("rk").drop_duplicates(["stock_id", "trade_date"])
              .drop(columns=["rk"]))


def names() -> dict[str, str]:
    c = connect_ro()
    out = {}
    for tbl, col in (("rrg_universe_scores", "stock_name"), ("stock_beta", "name")):
        try:
            for sid, nm in c.execute(
                    f"SELECT stock_id, {col} FROM {tbl} WHERE {col} IS NOT NULL AND {col}<>''"):
                out.setdefault(sid, nm)
        except Exception:  # noqa: BLE001
            pass
    return out


def holding_structure(d: str) -> pd.DataFrame:
    """集保持股結構。散戶＝級距 1–8（<50 張）、大戶＝12–15（>400 張）。

    ⚠️ PIT：as_of_date 是週五結算，集保下週一二才公布 → 只取
    ``as_of_date <= d − 4 天`` 的最新一週，否則會用到當下還看不到的資料。
    """
    cutoff = (date.fromisoformat(d) - timedelta(days=4)).isoformat()
    c = connect_ro()
    # ⚠️ 表的 PK 含 source，同一檔同一週可能同時有 tdcc 與 finmind 兩列。
    # 直接 SUM 會把百分比加成兩倍（實測大戶出現 191%），而且只有回補過
    # finmind 的 893 檔會被雙計、其餘 3,141 檔不會 —— 排名被系統性扭曲。
    # 必須先選定單一 source（tdcc 為全市場權威來源，優先）再聚合。
    df = pd.read_sql_query(
        """WITH pick AS (
              SELECT stock_id, as_of_date, source,
                     ROW_NUMBER() OVER (
                       PARTITION BY stock_id
                       ORDER BY as_of_date DESC,
                                CASE source WHEN 'tdcc' THEN 0 ELSE 1 END) AS rn
                FROM (SELECT DISTINCT stock_id, as_of_date, source
                        FROM stock_holding_dispersion_weekly
                       WHERE as_of_date <= ?))
           SELECT p.stock_id, p.as_of_date AS as_of,
                  SUM(CASE WHEN w.level IN ('1','2','3','4','5','6','7','8')
                           THEN w.percent ELSE 0 END) AS ret_pct,
                  SUM(CASE WHEN w.level IN ('12','13','14','15')
                           THEN w.percent ELSE 0 END) AS big_pct
             FROM pick p
             JOIN stock_holding_dispersion_weekly w
               ON w.stock_id=p.stock_id AND w.as_of_date=p.as_of_date
              AND w.source=p.source
            WHERE p.rn = 1
            GROUP BY p.stock_id, p.as_of_date""", c, params=(cutoff,))
    bad = df[(df.ret_pct + df.big_pct) > 101]
    if len(bad):
        raise RuntimeError(f"持股結構百分比異常（{len(bad)} 檔 >101%），疑似仍在雙計")
    return df[df.ret_pct > 0]


def freshness() -> dict[str, str | None]:
    c = connect_ro()
    def mx(t, extra=""):
        try:
            return c.execute(f"SELECT MAX(trade_date) FROM {t} {extra}").fetchone()[0]
        except Exception:  # noqa: BLE001
            return None
    return {
        "借券賣出餘額": mx("stock_short_interest_daily"),
        "借券費率": mx("stock_sbl_fee_daily"),
        "分點進出": mx("stock_broker_branch_daily"),
        "日線價格": mx("stock_daily_bars"),
    }


# ---------------------------------------------------------------- 名單


def build_lists(sig_d: str, top: int) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """回傳（偏多名單, 偏空名單, 統計）。score 低＝偏多、高＝偏空。"""
    mk = _mod("stock_chip_snapshot").market_scores(sig_d)
    if mk is None or mk.empty:
        return pd.DataFrame(), pd.DataFrame(), {}
    px = prices([sig_d])
    m = mk.merge(px[["stock_id", "close", "vol"]], on="stock_id", how="inner")
    m = m[(m.vol >= MIN_VOL_LOTS) & (m.close >= MIN_CLOSE)].copy()
    # ETF（代號 00 開頭）排除**於顯示名單**：槓桿／反向 ETF 的借券與分點行為和
    # 個股不同，列進來會誤導。⚠️ 前瞻紀錄 chip_score_forward_track 的宇宙
    # 凍結於 2026-08-23、**含 ETF**，那邊刻意不動——改了就不再是同一個檢定。
    n_all = len(m)
    m = m[~m.stock_id.str.startswith("00")]
    if len(m) < 50:
        return pd.DataFrame(), pd.DataFrame(), {"n": len(m)}
    hold = holding_structure(sig_d)
    m = m.merge(hold, on="stock_id", how="left")
    m["ret_rank"] = m.ret_pct.rank(pct=True) * 100
    m["pct"] = m.score.rank(pct=True) * 100
    m["nm"] = m.stock_id.map(names()).fillna("")

    comp = [c for c in COMPONENTS if c in m.columns]
    def driver(row):
        vals = {c: row[c] for c in comp if abs(row[c]) > 1e-9}
        if not vals:
            return "—"
        k = max(vals, key=lambda x: abs(vals[x]))
        return f"{COMPONENTS[k]} {vals[k]:+.2f}"
    m["driver"] = m.apply(driver, axis=1)
    m["n_active"] = (m[comp].abs() > 1e-9).sum(axis=1)

    s = m.sort_values("score")
    stats = {
        "n": len(m), "n_etf": n_all - len(m),
        "score_sd": m.score.std(),
        "zero_frac": (m.score.abs() < 1e-9).mean() * 100,
        "avg_active": m.n_active.mean(),
        "comp_cover": {COMPONENTS[c]: (m[c].abs() > 1e-9).mean() * 100 for c in comp},
        "hold_cover": m.ret_pct.notna().mean() * 100,
        "hold_asof": m.as_of.dropna().max() if "as_of" in m else None,
    }
    # 散戶持股最低 —— 這是本研究線唯一淨值為正的組態（見 C 段第 2 條）
    hl = m.dropna(subset=["ret_pct"]).sort_values("ret_pct").head(top).copy()
    stats["retail_low"] = hl
    return s.head(top).copy(), s.tail(top).iloc[::-1].copy(), stats


# ---------------------------------------------------------------- 回顧


def review(ret_d: str) -> tuple[dict | None, pd.DataFrame]:
    trk = _mod("chip_score_daily_track")
    row = trk.record(ret_d)
    if row:
        trk.upsert(row)
    return row, trk.load_track()


# ---------------------------------------------------------------- 排版


def _tbl(df: pd.DataFrame, side: str) -> str:
    if df.empty:
        return "<p>（無）</p>"
    head = ("<tr><th>代號</th><th>名稱</th><th>收盤</th><th>分數</th>"
            "<th>百分位</th><th>主要驅動</th><th>有效項</th>"
            "<th>散戶持股%</th><th>分位</th></tr>")
    def _f(v, fmt="{:.1f}"):
        return "—" if pd.isna(v) else fmt.format(v)
    rows = "".join(
        f"<tr><td>{r.stock_id}</td><td>{r.nm}</td><td align=right>{r.close:.2f}</td>"
        f"<td align=right>{r.score:+.2f}</td><td align=right>{r.pct:.0f}</td>"
        f"<td>{r.driver}</td><td align=center>{int(r.n_active)}/5</td>"
        f"<td align=right>{_f(r.ret_pct)}</td><td align=right>{_f(r.ret_rank, '{:.0f}')}</td></tr>"
        for r in df.itertuples())
    return (f"<table border=1 cellpadding=4 cellspacing=0 "
            f"style='border-collapse:collapse;font-size:13px'>{head}{rows}</table>")


CAVEATS = """
<b>1. B 段名單的預測力，在風險中性後是零。</b> 2026-08-26 檢定（45 萬 stock-day）：
v4 五項對波動／跳空／市值中性化後 <b>t = −0.92（連方向都沒有）</b>；
表面上好看的收→收 t=+10.55 有 <b>96% 消失在開盤那一瞬間</b>——
Δ借券、Δ使用率、分點家數三項貢獻的全是隔夜跳空，你拿不到。
<b>先前信中「可執行邊際 +0.051%/日」的說法已被否證</b>，那個數字是波動曝險。
B 段保留是為了持續累積前瞻紀錄，<b>它只描述部位結構，不預測漲跌</b>。

<b>2. B2 段是唯一淨值為正的組態，但幅度小到不構成生意。</b>
只做多、散戶持股最低 5.8%：中性後 +0.058%/日、t=+2.61、換手 11%，
損益兩平成本 0.528% vs 多頭腿來回 0.471% → <b>淨 +1.6%/年</b>，
扣掉滑價大概就沒了；且前半 t=2.56 → 後半 t=1.43 <b>正在衰減</b>，
樣本只有 535 天（集保資料起於 2024-06）、一個多頭週期，
而那一輪同時測了 9 個因子，多重檢定會讓 t 看起來比實際強。

<b>3. 真正在解釋隔日報酬的是兩個非籌碼效應。</b>
低波動 − 高波動（開→收）+0.334%/日 t=+7.03；跳空回歸（低開−高開）
+0.278%/日 t=+11.03。兩者獨立且加乘，高波動×高開那格是 −0.633%/日。
機制：高波動股平均跳空 +0.471% 後開高走低，收→收 反而是 −0.093%——
<b>純日內現象</b>。但兩者換手都近 100%，一樣付不起 1.17%/日 的成本。

<b>4. 個股離散度是整體傾斜的 20 倍以上。</b> 名單裡任一檔的個別事件就能蓋過整組傾斜。
實例：2026-08-25 偏空 Top30 平均 +1.125%，但<b>中位數是 −0.362%</b>——
靠台虹 +10.00%、光鼎 +9.87%、富世達 +7.89% 三檔翻正。<b>照名單押單檔＝押雜訊。</b>

<b>5. 單日對錯不代表任何事。</b> 多空價差單日標準差 0.456%，歷史上 60.5% 的日子為正。
單日落在 80 百分位跟丟銅板連對兩次差不多。

<b>6. 集保資料是週頻且落後。</b> as_of 是週五結算、下週一二才公布，本信已扣 4 天緩衝。
它反映的是<b>已完成的過戶結果</b>，不是盤中籌碼；適合週～月尺度，不適合當隔日訊號。
"""


def compose(d: str, sig_d: str, bull, bear, stats, row, track, fresh, log, top):
    # 兩種未對齊要分開講，否則措辭會反過來：
    #   比 sig_d 舊 → 該分項當日缺，退為 0（少一項訊號）
    #   比 sig_d 新 → 借券還沒公布，整封信用的是**舊訊號日**（更嚴重）
    behind = [k for k, v in fresh.items() if v and v < sig_d]
    ahead = [k for k, v in fresh.items() if v and v > sig_d]
    warn = ""
    if ahead:
        warn += ("<p style='background:#ffe0e0;padding:8px;border-left:4px solid #c00'>"
                 f"⚠️ <b>借券資料尚未更新到最新交易日</b>，本信用的訊號日是 {sig_d}，"
                 "但 " + "、".join(f"{k} 已到 {fresh[k]}" for k in ahead)
                 + "。<b>這是一份落後的名單</b>，請等借券公布後重跑。</p>")
    if behind:
        warn += ("<p style='background:#fff3d0;padding:8px;border-left:4px solid #d90'>"
                 f"⚠️ 下列來源落後訊號日 {sig_d}："
                 + "、".join(f"{k}→{fresh[k]}" for k in behind)
                 + "，對應分項退為 0，名單參考價值下降。</p>")

    parts = [f"<h2>籌碼簡報 {d}</h2>",
             f"<p>訊號日 <b>{sig_d}</b>　·　可行動時點 <b>{d} 之後的下一個交易日開盤</b>"
             f"　·　標的 <b>{stats.get('n', 0)}</b> 檔（成交 ≥{MIN_VOL_LOTS} 張、"
             f"股價 ≥{MIN_CLOSE} 元、已排除 {stats.get('n_etf', 0)} 檔 ETF）</p>", warn]

    parts.append("<h3>A. 今日回顧</h3>")
    if row:
        f = track[track.regime == "forward"]
        parts.append(
            f"<p>用 {row['signal_date']} 的分數對 {row['return_date']} 實際報酬驗算"
            f"（{row['n']} 檔）：<br>"
            f"多空價差 收→收 <b>{row['spread_cc']:+.4f}%</b>（歷史平均 +0.115%）"
            f"　·　開→收 <b>{row['spread_oc']:+.4f}%</b>"
            f"（歷史平均 +0.051%，<b>但風險中性後為零</b>）<br>"
            f"　Q1 偏多組 {row['q1_cc']:+.4f}%　·　Q5 偏空組 {row['q5_cc']:+.4f}%"
            f"　·　大盤 {row['mkt_cc']:+.4f}%<br>"
            f"跳空回歸（低開−高開）<b>{row['gap_rev']:+.4f}%</b>（歷史 +0.493%）</p>"
            f"<p style='font-size:12px;color:#666'>⚠️ 回顧用的是 <b>{row['n']} 檔</b>"
            f"（finmind 價格宇宙），比 B 段名單的 {stats.get('n', 0)} 檔窄。"
            "訊號算法完全相同、只有覆蓋範圍不同——追蹤宇宙<b>凍結於 2026-08-23</b>，"
            "看過結果後再放寬就不再是乾淨的樣本外檢定，因此刻意不動。</p>")
        if len(f) >= 2:
            v = f.spread_cc.dropna()
            t = v.mean() / (v.std(ddof=1) / np.sqrt(len(v)))
            parts.append(f"<p>前瞻累積 <b>{len(f)}</b> 日：多空價差 {v.mean():+.4f}%/日 · "
                         f"t={t:+.2f} · 為正 {(v > 0).mean() * 100:.0f}%"
                         f"　（距最低判斷樣本 60 日還差 <b>{max(0, 60 - len(f))}</b> 日）</p>")
        else:
            parts.append(f"<p>前瞻累積 <b>{len(f)}</b> 日"
                         f"（距最低判斷樣本 60 日還差 <b>{max(0, 60 - len(f))}</b> 日）"
                         "——<b>樣本不足，尚不能做任何判斷</b>。</p>")
    else:
        parts.append("<p>今日無法驗算（價格或籌碼未進 DB）。</p>")

    parts += [f"<h3>B. 明日名單（訊號日 {sig_d}）</h3>",
              f"<p><b>偏多 Top {top}</b>（分數最低）</p>", _tbl(bull, "多"),
              f"<p style='margin-top:12px'><b>偏空 Top {top}</b>（分數最高）</p>", _tbl(bear, "空")]
    hl = stats.get("retail_low")
    if hl is not None and not hl.empty:
        rows = "".join(
            f"<tr><td>{r.stock_id}</td><td>{r.nm}</td><td align=right>{r.close:.2f}</td>"
            f"<td align=right>{r.ret_pct:.1f}</td><td align=right>{r.big_pct:.1f}</td>"
            f"<td align=right>{r.score:+.2f}</td></tr>" for r in hl.itertuples())
        parts.append(
            f"<h3>B2. 散戶持股最低 Top {top}（訊號日 {sig_d}）</h3>"
            "<p style='font-size:13px'>這是本研究線<b>唯一淨值為正</b>的組態："
            "只做多、散戶持股最低那 5.8%，風險中性後 +0.058%/日（t=+2.61）、"
            "換手僅 11%。<b>但淨值只有 +1.6%/年，而且在衰減</b>"
            "（前半 t=2.56 → 後半 t=1.43）。列在這裡是為了累積前瞻紀錄，不是建議。</p>"
            "<table border=1 cellpadding=4 cellspacing=0 style='border-collapse:collapse;"
            "font-size:13px'><tr><th>代號</th><th>名稱</th><th>收盤</th>"
            "<th>散戶持股%</th><th>大戶持股%</th><th>v4分數</th></tr>" + rows + "</table>")
        if stats.get("hold_asof"):
            parts.append(f"<p style='font-size:12px;color:#555'>集保資料週別 "
                         f"{stats['hold_asof']}（週五結算，已扣 4 天公布緩衝）　·　"
                         f"覆蓋 {stats.get('hold_cover', 0):.0f}%</p>")
    if stats.get("comp_cover"):
        cov = "　".join(f"{k} {v:.0f}%" for k, v in stats["comp_cover"].items())
        parts.append(f"<p style='font-size:12px;color:#555'>分項覆蓋率：{cov}<br>"
                     f"平均有效項 {stats['avg_active']:.2f}/5　·　"
                     f"分數標準差 {stats['score_sd']:.2f}　·　"
                     f"全零（無意見）{stats['zero_frac']:.1f}%</p>")

    parts += ["<h3>C. 提醒事項</h3>",
              f"<div style='font-size:13px;line-height:1.7'>{CAVEATS}</div>"]
    parts.append("<hr><p style='font-size:11px;color:#888'>資料補檔："
                 + "　".join(log) + "<br>"
                 + "　".join(f"{k}={v}" for k, v in fresh.items())
                 + "<br>chip-daily-brief · 唯讀研究 · 不下單</p>")
    html = "\n".join(p for p in parts if p)

    txt = [f"籌碼簡報 {d}（訊號日 {sig_d}）"]
    if ahead:
        txt.append("⚠️ 借券未更新，這是落後名單：" + "、".join(f"{k}→{fresh[k]}" for k in ahead))
    if behind:
        txt.append("⚠️ 分項缺漏：" + "、".join(f"{k}→{fresh[k]}" for k in behind))
    if row:
        txt.append(f"回顧：多空價差 收→收 {row['spread_cc']:+.4f}% / "
                   f"開→收 {row['spread_oc']:+.4f}%")
    for lab, df in (("偏多", bull), ("偏空", bear)):
        if not df.empty:
            txt.append(f"{lab}：" + "、".join(f"{r.stock_id} {r.nm}" for r in df.itertuples()))
    txt.append("這不是買賣建議：B 段名單在風險中性後 t=-0.92（無預測力）；"
               "B2 段是唯一淨值為正的組態，但只有 +1.6%/年且在衰減。")
    return "\n".join(txt), html


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", default=None, help="報酬日／訊號日（預設 DB 最新交易日）")
    # 30 檔/邊 ≈ 全市場 478 檔的 6.3%，仍落在五分位最極端那一段內
    # （單邊 20% 才是回測的分位邊界），不會稀釋掉訊號強度。
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--no-refresh", action="store_true", help="不補檔，只用 DB 現有資料")
    ap.add_argument("--dry-run", action="store_true", help="印出內容，不寄信")
    ap.add_argument("--skip-if-market-closed", action="store_true",
                    help="休市日靜默跳過（launchd 用）。判準是『今日有無日線』——"
                         "有價無籌碼＝TWSE 延遲，仍會寄警告信；兩者皆無＝休市，不寄。")
    args = ap.parse_args()

    log: list[str] = []
    d = args.date or date.today().isoformat()
    if args.skip_if_market_closed and not args.date:
        # 補檔前先問一次：今天有沒有成交？沒有就是休市，直接退出，
        # 否則會把昨天的名單原封不動再寄一次。
        px_today = connect_ro().execute(
            "SELECT COUNT(*) FROM stock_daily_bars WHERE trade_date=?", (d,)).fetchone()[0]
        if not px_today:
            refresh_prices_only(d)
            px_today = connect_ro().execute(
                "SELECT COUNT(*) FROM stock_daily_bars WHERE trade_date=?", (d,)).fetchone()[0]
        if not px_today:
            print(f"{d} 無日線成交紀錄 → 休市，不寄信")
            return 0
    if not args.no_refresh:
        refresh(d, log)
    else:
        log.append("（略過補檔）")

    fresh = freshness()
    sig_d = fresh["借券賣出餘額"]
    if not sig_d:
        print("借券資料完全空白，無法出簡報", file=sys.stderr)
        return 1
    ret_d = min(d, fresh["日線價格"] or d)

    row, track = review(ret_d)
    bull, bear, stats = build_lists(sig_d, args.top)
    txt, html = compose(ret_d, sig_d, bull, bear, stats, row, track, fresh, log, args.top)

    subject = f"[籌碼簡報] {sig_d} · 偏多/偏空各 {len(bull)} 檔"
    if any(v and v > sig_d for v in fresh.values()):
        subject += " · ⚠️訊號日落後"
    elif any(v and v < sig_d for v in fresh.values()):
        subject += " · ⚠️分項缺漏"
    if args.dry_run:
        print(subject); print(); print(txt)
        out = ROOT / "reports" / "research" / "chip-signal-daily-horizon" / "brief_preview.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"\nHTML 預覽 → {out}")
        return 0

    from notify_email import send_alert
    send_alert(subject, txt, html_body=html)
    print(f"已寄出：{subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
