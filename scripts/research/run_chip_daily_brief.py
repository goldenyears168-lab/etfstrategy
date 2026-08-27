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
from datetime import date, datetime, timedelta
from importlib.machinery import SourceFileLoader
from pathlib import Path

import numpy as np
import pandas as pd

from stock_db import connect_ro

ROOT = Path(__file__).resolve().parents[2]
PY = str(ROOT / ".venv" / "bin" / "python")

# 2026-08-27：排序基準從「全市場截面」改為「同業相對」，權重同步重選。
# 借券費率/借券水位有強烈的產業結構差異（半導體/金融/傳產的券源市場天差地遠），
# 全市場排序主要在排**產業**不是排個股。框架層級檢定：11 個籌碼因子事先固定
# 方向全測，平均超額 −0.85% → +3.56%，8/11 變好，Wilcoxon 配對 p=0.0137。
# 換基準後重掃 w：0.5~0.8 是高原（+24~29%/年），取等權點 0.50（非曲線上挑的）。
#   w=0.25 舊值 +20.35%/年 IR 1.66 t=+1.80（前半 t 僅 +0.65）
#   w=0.50 新值 +29.04%/年 IR 2.39 t=+2.59（前半 +1.57、後半 +2.06 都穩）
#
# ⚠️ 誠實的帳（上線前必讀，別把下面的數字當成淨 alpha）：
# 簡報實際取前 30/467 ≈ 6%，比研究驗證的 20% 窄得多。在 6% 寬度下：
#                        對同層等權       對「各檔自己的產業」
#   全市場截面（舊）        +19.26% t=1.25   +22.50% t=+1.58
#   同業內重排（新）        +43.92% t=2.62   +31.28% t=+1.81
#                        差 24.7pp        差  8.8pp
# → 改善**約 2/3 來自科技偏離**（科技權重 94.6% vs 宇宙 75.3%，+19.3pp），
#   只有 1/3 是真正的同業內選股。超額沒消失但只到邊緣顯著（t=+1.81）。
#   名單會系統性偏向電子/半導體 —— 這是已知且刻意接受的性質，不是 bug。
# ⚠️ 合成分數的 OOS 只涵蓋 2025–2026（集保資料起始較晚，250 日暖身吃掉 2024），
#   而這兩年都是台股科技大年。樣本很薄。
#
# 另測過但未採用：「全市場百分位 − 同業均值」（無組大小偏誤，理論上較乾淨），
# 6% 寬度 +35.35% t=+2.18、產業配對 +25.07% t=+1.48 —— 三項都輸同業內重排，故不用。
HS_W_ZP = 0.50          # 綜合分數裡 zp 的權重（其餘給散戶持股）
# 2026-08-27 二次修正：門檻由 3 提高到 40。查「+18.43% 是選股還是押科技」時發現
# 好處幾乎全部來自把**電子工業與半導體業**從全市場排序裡分離出來 —— 只對這 16%
# 的宇宙做同業相對就拿到 93% 的好處，而對小產業做同業相對不但沒幫助還有害：
#   非科技組  舊基準 +11.85% t=2.20 → 門檻3 **+5.47% t=1.48** → 門檻40 +11.85% t=2.20
# 門檻 40 把那個傷害完全還原（非科技產業全部 <40 檔，一律退回全市場）。
# 全宇宙 淨/年 +19.84%(門檻3) → +20.47%(門檻40)、NW t +3.78 → +3.95，
# 且科技權重偏離由 **+17.3pp 翻成 −9.2pp**（宇宙 55.2%）—— 不再是偽裝的科技押注。
# ⚠️ 40 是依**產業檔數穩定性**挑的，不是依績效曲線（26/50/100 績效都在 +18.5~19.4%、
#    差異在雜訊內）：門檻 50 時半導體只有 36% 的日子符合、會每天進出；門檻 40 是
#    電子工業 100%／半導體 88%／光電 6%，最乾淨。
MIN_IND_N = 40          # 產業內少於此檔數退回全市場排序（實務上只有電子工業與半導體業符合）
IND_CACHE_DAYS = 30
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


def industry() -> pd.DataFrame:
    """stock_id → 產業別（FinMind TaiwanStockInfo，快取於 DATA_DIR）。

    產業分類極少變動，故快取 30 天。抓不到時回空表 → 呼叫端自動退回全市場排序。
    """
    from stock_db import DATA_DIR
    cache = Path(DATA_DIR) / "cache" / "stock_industry.parquet"
    if cache.exists():
        age = (datetime.now().timestamp() - cache.stat().st_mtime) / 86400
        if age < IND_CACHE_DAYS:
            return pd.read_parquet(cache)
    try:
        import requests
        r = requests.get("https://api.finmindtrade.com/api/v4/data",
                         params={"dataset": "TaiwanStockInfo",
                                 "token": os.environ.get("FINMIND_TOKEN", "")}, timeout=120)
        r.raise_for_status()
        df = pd.DataFrame(r.json()["data"])[["stock_id", "industry_category"]]
        df = df.drop_duplicates("stock_id").rename(columns={"industry_category": "ind"})
        cache.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(cache)
        return df
    except Exception:
        return pd.read_parquet(cache) if cache.exists() else pd.DataFrame(columns=["stock_id", "ind"])


def _ind_rank(df: pd.DataFrame, col: str) -> pd.Series:
    """同業內百分位；產業檔數 < MIN_IND_N 的退回全市場百分位。"""
    mkt = df[col].rank(pct=True)
    if "ind" not in df.columns or df["ind"].nunique() < 2:
        return mkt
    ind = df.groupby("ind")[col].rank(pct=True)
    return ind.where(df.groupby("ind")["ind"].transform("size") >= MIN_IND_N, mkt)


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

    stats = {
        "n": len(m), "n_etf": n_all - len(m),
        "score_sd": m.score.std(),
        "zero_frac": (m.score.abs() < 1e-9).mean() * 100,
        "avg_active": m.n_active.mean(),
        "comp_cover": {COMPONENTS[c]: (m[c].abs() > 1e-9).mean() * 100 for c in comp},
        "hold_cover": m.ret_pct.notna().mean() * 100,
        "hold_asof": m.as_of.dropna().max() if "as_of" in m else None,
    }
    # ---- 綜合分數 HS = 25% zp + 75% 散戶持股（橫斷面 rank z，越大越偏空）----
    # 2026-08-26 檢定：散戶持股單獨用在後半段已翻負（t=+1.20、淨值 −4.62%/年），
    # 加入 zp（借券佔股本水位，v4 裡唯一有持續性的分項）後兩個半段都穩
    # （t=+2.99 / +2.98、淨值 +4.07% / +6.92%）。w=0.15~0.35 全區間為正，
    # 是高原不是尖峰，故取中段 0.25。
    # ⚠️ 不可改用整包 v4：它會把多頭腿換手從 12.3% 炸到 47.2%，一年虧 35.8%。
    hs = m.dropna(subset=["ret_pct"]).copy()
    if len(hs) >= 60:
        ind = industry()
        if len(ind):
            hs = hs.merge(ind, on="stock_id", how="left")
            hs["ind"] = hs["ind"].fillna("未分類")
        for src, dst in (("s_zp", "_nzp"), ("ret_pct", "_nret")):
            hs[dst] = (_ind_rank(hs, src) - 0.5) * 2
        hs["hs"] = HS_W_ZP * hs._nzp + (1 - HS_W_ZP) * hs._nret
        hs["hs_pct"] = hs.hs.rank(pct=True) * 100
        srt = hs.sort_values("hs")
        stats["hs_n"] = len(hs)
        if "ind" in hs.columns:
            big_ind = hs.groupby("ind")["ind"].transform("size") >= MIN_IND_N
            stats["ind_cover"] = big_ind.mean() * 100
            stats["ind_n"] = hs["ind"].nunique()
        return srt.head(top).copy(), srt.tail(top).iloc[::-1].copy(), stats
    stats["hs_n"] = 0
    return pd.DataFrame(), pd.DataFrame(), stats


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
    head = ("<tr><th>代號</th><th>名稱</th><th>收盤</th><th>HS分數</th><th>百分位</th>"
            "<th>散戶持股%</th><th>大戶持股%</th><th>zp</th>"
            "<th>v4分數</th><th>v4主要驅動</th></tr>")
    rows = "".join(
        f"<tr><td>{r.stock_id}</td><td>{r.nm}</td><td align=right>{r.close:.2f}</td>"
        f"<td align=right>{r.hs:+.3f}</td><td align=right>{r.hs_pct:.0f}</td>"
        f"<td align=right>{r.ret_pct:.1f}</td><td align=right>{r.big_pct:.1f}</td>"
        f"<td align=right>{r.zp:+.2f}</td><td align=right>{r.score:+.2f}</td>"
        f"<td>{r.driver}</td></tr>" for r in df.itertuples())
    return (f"<table border=1 cellpadding=4 cellspacing=0 "
            f"style='border-collapse:collapse;font-size:13px'>{head}{rows}</table>")


CAVEATS = """
<b>0. ⚠️ 持有 5 日，不要每日換 —— 這是本表最重要的一條。</b>
2026-08-27 在新基準上重跑控制檢定（最嚴：vol60/gap/mcap/週轉率/股價），
多腿淨值隨持有天數：<b>K=1 −8.66%／年、K=3 +12.85%、K=5 +18.43%、K=10 +22.31%</b>。
每日換的話 12.8% 日換手攤不掉，必虧；持有 5 日攤成 4.7%/日就站得住。
K=5 最嚴控制下：檔數 10~150 <b>9/9 全為正</b>、偏空腿 NW t=−2.51、
多空價差 +0.7992%/趟 NW t=+3.98。

<b>0c. 同業相對只套用在電子工業與半導體業。</b>
2026-08-27 追查「這是選股還是押科技」時發現：好處幾乎全部來自把這兩個巨型產業
從全市場排序裡分離出來，對小產業做同業相對<b>不但沒幫助還有害</b>
（非科技組 +11.85% → +5.47%）。把門檻提到 40 檔後非科技組完全還原，
全宇宙淨值 +20.47%／年、NW t=+3.95，且科技權重偏離由 <b>+17.3pp 翻成 −9.2pp</b>。
名單不再是偽裝的科技押注。

<b>0b. 下面第 1、2 點是 2026-08-26 在舊基準、且在 K=1 口徑下寫的。</b>
其「淨值站不住／參數脆弱」在 <b>K=1 仍然成立</b>（舊基準 K=1 最嚴控制下檔數 0/9 為正），
但在 <b>K≥3 不成立</b>。改基準後同一批檢定：K=5 時新基準每一個檔數都優於舊基準。
仍未解的：合成分數 OOS 僅涵蓋 2025–2026 <b>兩個科技大年</b>，樣本很薄；
科技組（+23.73% t=3.73）仍明顯強於非科技組（+11.85% t=2.20）。

<b>1. 訊號是真的，但淨值站不住。</b>（以下為舊基準的測定值）2026-08-26 對抗檢定：
HS 通過了自我相關（HAC t=+4.25 vs 一般 +4.23）、產業中性化（t 反升到 +4.45）、
PIT 緩衝拉到 14 天（t=+3.92）、開→收與收→收皆成立（+0.086%／+0.093%）、
81% 的月份為正（剔除最好那月仍 t=+3.79）。
<b>但再加控週轉率與股價後，gross 從 +0.086% 掉到 +0.052%/日</b>，
低於損益兩平的 0.064%/日。<b>它有真實的橫斷面預測力，卻沒有可實現的淨值。</b>

<b>2. 「+5.4%/年」那個數字取決於兩個選擇。</b>（a）不控制週轉率與股價；
（b）檔數落在 15~40 之間。全控之下 10~150 檔<b>沒有任何一個檔數淨值為正</b>，
最好的 29 檔也是 −2.9%/年。本表用 30 檔，正好在未控版本的峰值上——
<b>這是參數脆弱的訊號，不是穩健的邊際。</b>

<b>3. 最可能的真相：它大部分是低週轉（流動性）溢酬的代理。</b>
散戶持股低的股票週轉率也低。若你認為流動性溢酬是可收割的，那它是真的，
但那樣就<b>不是籌碼 alpha、而是「買冷門股」</b>，而且冷門股的實際衝擊成本
遠高於本信採用的 0.471%。兩種讀法下結論都一樣：<b>不要照著交易。</b>

<b>4. 舊的 v4 五項分數已被否證，不再用於排序。</b> 中性化後 t=−0.92，
多頭腿甚至是 −0.037%/日。它表面好看的收→收 t=+10.55 有 96% 消失在開盤那一瞬間。
v4 分數與驅動項仍列在表格內<b>供對照</b>，不參與排序。

<b>5. 偏空側完全沒有證據。</b> 空頭腿中性後 +0.028%/日、<b>t=+1.30（不顯著）</b>，
加計借券費後淨值 −12.2%/年。多空合計也是負的。<b>不要照偏空名單放空。</b>

<b>6. 真正在解釋隔日報酬的是兩個非籌碼效應。</b>
低波動 − 高波動（開→收）+0.334%/日 t=+7.03；跳空回歸（低開−高開）+0.278%/日
t=+11.03。<b>要做多找低開、要放空找高開</b>——但兩者換手都近 100%，一樣付不起成本，
只作為「已經決定要下單時」的進場時點參考。

<b>7. 樣本只有 535 天、一個多頭週期，且多重檢定嚴重。</b>
集保資料起於 2024-06，沒經歷過空頭。這條研究線至今測過 9 個因子、12 個權重值、
11 個檔數設定與多種中性化寫法。<b>單日對錯不代表任何事</b>；個股離散度是整體傾斜的
20 倍以上，照名單押單檔＝押雜訊。
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

    parts += [
        f"<h3>B. 明日名單（訊號日 {sig_d}）</h3>",
        f"<p style='font-size:13px'>唯一排序分數 <b>HS ＝ {HS_W_ZP:.0%} 借券佔股本水位"
        f"（zp）＋ {1 - HS_W_ZP:.0%} 散戶持股水位</b>（集保 &lt;50 張），"
        "兩者各自轉成 <b>同業內</b>百分位（產業檔數 &lt; "
        f"{MIN_IND_N} 的退回全市場排序 —— 實務上只有電子工業與半導體業享有同業相對，"
        "其餘產業與舊版完全相同），分數越低越偏多。"
        f"{'（本日 %.0f%% 用同業相對、%d 個產業）' % (stats['ind_cover'], stats['ind_n']) if stats.get('ind_cover') else ''}"
        "<br>2026-08-27 起由全市場截面改為同業相對：借券水位有強烈產業結構差異，"
        "全市場排序主要在排產業不是排個股。"
        "<b>已知性質</b>：名單會偏向電子/半導體（科技權重約 95% vs 宇宙 75%），"
        "回測改善約 2/3 來自這個偏離、1/3 是同業內選股；對產業配對基準的 t=+1.81"
        "（邊緣顯著），且 OOS 只涵蓋 2025–2026 兩個科技大年。</p>",
        "<p style='background:#fff3d0;padding:8px;border-left:4px solid #d90'>"
        f"<b>偏多 Top {top}</b>（HS 最低）　·　訊號是真的，<b>但淨值站不住</b>："
        "只控波動／跳空／市值時 +0.086%/日 t=+4.23（淨 +5.4%/年）；"
        "<b>再加控週轉率與股價後掉到 +0.052%/日</b>（t=+2.75），"
        "低於損益兩平的 0.064%/日 → <b>淨值 −2.9%/年</b>。"
        "且淨值為正的檔數區間只有 15~40 檔（本表 30 檔正好在峰值），"
        "全控之下 10~150 檔<b>沒有任何一個檔數為正</b>。</p>",
        _tbl(bull, "多"),
        "<p style='background:#fff3d0;padding:8px;border-left:4px solid #d90;"
        "margin-top:12px'>"
        f"<b>偏空 Top {top}</b>（HS 最高）　·　⚠️ <b>空頭腿沒有證據支撐</b>："
        "風險中性後 +0.028%/日、<b>t=+1.30（不顯著）</b>，加計借券費後"
        "<b>淨值 −12.2%/年</b>。列出僅供對照與累積紀錄，<b>不要照著放空</b>。</p>",
        _tbl(bear, "空")]
    if stats.get("hold_asof"):
        parts.append(f"<p style='font-size:12px;color:#555'>集保資料週別 "
                     f"{stats['hold_asof']}（週五結算，已扣 4 天公布緩衝）　·　"
                     f"覆蓋 {stats.get('hold_cover', 0):.0f}%　·　"
                     f"可評分 {stats.get('hs_n', 0)} 檔</p>")
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
    txt.append(f"分數 HS = {HS_W_ZP:.0%} 借券佔股本水位 + {1 - HS_W_ZP:.0%} 散戶持股水位"
               f"（同業內百分位，產業 < {MIN_IND_N} 檔退回全市場）。"
               "⚠️ 持有 5 日、不要每日換：最嚴控制下多腿淨值 K=1 −8.66%/年、"
               "K=3 +12.85%、K=5 +18.43%、K=10 +22.31%（新基準日換手 12.8%，K=1 攤不掉）。"
               "K=5 最嚴控制：檔數 10~150 全為正、空腿 t=-2.51、多空價差 t=+3.98。"
               "同業相對只套用在電子工業與半導體業（其餘產業與舊版相同）—— 對小產業做同業相對"
               "有害，非科技組會從 +11.85% 掉到 +5.47%，提高門檻後已還原。"
               "仍未解：OOS 僅 2025-2026 兩個科技大年，且科技組(+23.73% t=3.73)仍強於"
               "非科技組(+11.85% t=2.20)。"
               "不要照著交易。")
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
