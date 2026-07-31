"""個股趨勢追蹤器 + 階段熱力圖 — the stock-level companion to daily_tracker.py.

daily_tracker.py covers the MARKET (L0xL2 regime). This covers the STOCKS you copy-trade
(景碩/頎邦 + watchlist), showing the ONE thing the research proved is real & usable:
the individual-stock TREND layer (Weinstein 30W stage + RS vs TAIEX), gated by the market
regime — NOT chip-alpha (which we showed is unreliable to follow).

Reuses:
  - stage_series_daily  (same 30W engine as the expert-pool heatmap)
  - daily_tracker.compute()  (single source of truth for the 大盤 regime)

Output: reports/research/chip-macro/stock_tracker.html  (self-contained)
Usage:  .venv/bin/python scripts/research/chip_macro/stock_tracker.py [--symbols 3189,6147,...]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage_analysis import stage_series_daily  # noqa: E402
import daily_tracker as dt  # noqa: E402  (reuse market regime + panel loader)

DB = ROOT / "data" / "replica" / "stocks.db"
OUT = ROOT / "reports" / "research" / "chip-macro" / "stock_tracker.html"
MA_WEEKS, CONFIRM, HEAT_DAYS = 30, 1, 252
TPE = timezone(timedelta(hours=8))
DEFAULT = ["3189", "6147", "2492", "3653", "6505"]
NAMES = {"3189": "景碩", "6147": "頎邦", "2492": "華新科", "3653": "健策",
         "6505": "台塑化", "IX0001": "加權指數"}
STAGE_LABEL = {1: "S1 打底", 2: "S2 多頭", 3: "S3 走緩", 4: "S4 下跌"}
STAGE_COLOR = {1: "#5b6b86", 2: "#16a34a", 3: "#d19a1e", 4: "#c0392b", 0: "#3a3a3a"}


def s2_green(slope, ext):
    t = 0.0
    for v, cap in ((slope, 25.0), (ext, 20.0)):
        if v is not None:
            t = max(t, max(0.0, v) / cap)
    t = min(1.0, t)
    lo, hi = (0x14, 0x53, 0x2d), (0x4a, 0xde, 0x80)
    rgb = tuple(round(lo[i] + (hi[i] - lo[i]) * t) for i in range(3))
    return f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"


def cell_color(stage, slope, ext):
    return s2_green(slope, ext) if stage == 2 else STAGE_COLOR.get(stage, "#3a3a3a")


def load_prices(sid):
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    df = pd.read_sql_query(
        "select trade_date as date, open, high, low, close from stock_daily_bars "
        "where stock_id=? and close>0 order by trade_date", con, params=(sid,))
    con.close()
    df = df.dropna(subset=["close"]).drop_duplicates("date", keep="last")
    for c in ("open", "high", "low"):
        df[c] = df[c].fillna(df["close"])
    return df.reset_index(drop=True)


def stage_frame(df):
    if len(df) < max(80, MA_WEEKS * 6):
        return pd.DataFrame()
    return stage_series_daily(df, ma_weeks=MA_WEEKS, confirm_days=CONFIRM)


def dot(color):
    return f'<span class="lt" style="background:{color}"></span>'


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols")
    a = ap.parse_args(argv)
    watch = [s.strip() for s in a.symbols.split(",")] if a.symbols else DEFAULT

    # --- 大盤 regime (reuse daily_tracker) ---
    mp = dt.compute(dt.load_panel().sort_values("date").reset_index(drop=True))["panel"]
    m = mp.iloc[-1]
    mkt_stage, z60, armed = str(m["stage"]), float(m["z60"]), bool(m["armed"])
    last_date = str(m["date"])[:10]
    tx_close = pd.Series(mp["ix_close"].values, index=mp["date"].astype(str))

    rows, heat_rows, heat_dates = [], [], None
    for sid in ["IX0001"] + watch:
        if sid == "IX0001":
            sf = stage_frame(mp.rename(columns={"ix_open": "open", "ix_high": "high",
                                                "ix_low": "low", "ix_close": "close"})
                             [["date", "open", "high", "low", "close"]])
            price, rs_up, rs_txt = mp["ix_close"].iloc[-1], None, "—"
        else:
            df = load_prices(sid)
            sf = stage_frame(df)
            if sf.empty:
                continue
            price = df["close"].iloc[-1]
            rsm = df.set_index("date")["close"].to_frame("c").join(tx_close.rename("tx")).dropna()
            rs = rsm["c"] / rsm["tx"]
            rs_up = bool(rs.iloc[-1] > rs.rolling(50, min_periods=50).mean().iloc[-1]) if len(rs) >= 50 else None
            rs_txt = ("↑ 強於大盤" if rs_up else "↓ 弱於大盤") if rs_up is not None else "n/a"
        sf = sf.tail(HEAT_DAYS)
        cur = sf.iloc[-1]
        stg = int(cur["stage"])
        slope = None if pd.isna(cur["ma_slope_pct"]) else float(cur["ma_slope_pct"])
        ext = None if pd.isna(cur["extension_pct"]) else float(cur["extension_pct"])
        # 綜合順勢 light: needs stock S2 + RS up, AND market not risk-off
        if stg == 2 and rs_up and armed:
            cc, ct = "#16a34a", "順勢（趨勢站你這邊）"
        elif stg == 2 and rs_up and not armed:
            cc, ct = "#d19a1e", "個股順勢，但大盤 risk-off"
        elif stg in (1, 4):
            cc, ct = "#c0392b", "逆勢（避開/別追）"
        else:
            cc, ct = "#8b9bb4", "中性（觀望）"
        nm = NAMES.get(sid, sid)
        if sid != "IX0001":
            det = f"斜率{slope:+.0f}／乖離{ext:+.0f}%" if slope is not None else ""
            rows.append(
                f"<tr><td class='nm'>{nm} <span class='sid'>{sid}</span></td>"
                f"<td class='px'>{price:,.1f}</td>"
                f"<td>{dot(cell_color(stg, slope, ext))}{STAGE_LABEL.get(stg,'—')} "
                f"<span class='sub'>{det}</span></td>"
                f"<td>{rs_txt}</td><td>{dot(cc)}{ct}</td></tr>")
        if heat_dates is None:
            heat_dates = [str(d)[:10] for d in sf["date"]]
        cells = "".join(
            f"<td class='hc' style='background:{cell_color(int(r.stage), None if pd.isna(r.ma_slope_pct) else float(r.ma_slope_pct), None if pd.isna(r.extension_pct) else float(r.extension_pct))}' "
            f"title='{str(r.date)[:10]} {STAGE_LABEL.get(int(r.stage),'?')}'></td>"
            for r in sf.itertuples(index=False))
        pin = " pin" if sid in ("3189", "6147") else ""
        heat_rows.append(f"<tr class='hr{pin}'><th class='hl'>{nm} <span class='sid'>{sid}</span></th>{cells}</tr>")

    mlabels, prev = [], ""
    for d in heat_dates or []:
        mo = d[:7]
        mlabels.append(f"<td class='mh'>{d[5:7]}</td>" if mo != prev else "<td class='mh'></td>")
        prev = mo

    tc = STAGE_COLOR.get({"S2 上升": 2, "S3 頭部": 3, "S4 下降": 4, "S1 基底": 1}.get(mkt_stage, 0))
    banner = (
        f"<div class='card'><div class='ct'>大盤趨勢</div>{dot(tc)}{mkt_stage}"
        f"<div class='sub2'>加權 {mp['ix_close'].iloc[-1]:,.0f}</div></div>"
        f"<div class='card'><div class='ct'>外資期貨籌碼</div>{dot('#16a34a' if z60>0 else '#c0392b')}{'偏多' if z60>0 else '偏空/不確認'}"
        f"<div class='sub2'>z60 = {z60:+.2f}</div></div>"
        f"<div class='card'><div class='ct'>綜合 regime</div>{dot('#16a34a' if armed else '#c0392b')}{'可承擔風險' if armed else '站旁邊'}"
        f"<div class='sub2'>{'趨勢×籌碼皆確認' if armed else '趨勢或籌碼未同時確認 → 個股順勢也降級'}</div></div>")

    html = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>個股趨勢追蹤器 · {last_date}</title>
<style>
:root{{--bg:#0f1419;--panel:#1a2332;--text:#e7ecf3;--muted:#8b9bb4;--border:#2a3548;color-scheme:dark}}
*{{box-sizing:border-box}} body{{margin:0;font-family:"PingFang TC","Noto Sans TC",system-ui,sans-serif;background:var(--bg);color:var(--text);line-height:1.5}}
header{{padding:1.2rem 1.4rem .6rem;border-bottom:1px solid var(--border)}} h1{{margin:0 0 .2rem;font-size:1.3rem}}
.sub{{color:var(--muted);font-size:.8rem}} .warn{{color:#e8b84b}}
.banner{{display:flex;gap:.8rem;flex-wrap:wrap;padding:1rem 1.4rem}}
.card{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:.7rem .9rem;min-width:215px}}
.ct{{color:var(--muted);font-size:.76rem;margin-bottom:.35rem}} .sub2{{color:var(--muted);font-size:.74rem;margin-top:.3rem}}
.lt{{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:.45rem;vertical-align:middle}}
h2{{margin:1rem 1.4rem .2rem;font-size:1rem}} .hint{{color:var(--muted);font-size:.79rem;padding:0 1.4rem .5rem}}
table.tbl{{border-collapse:collapse;margin:.3rem 1.4rem 1rem;font-size:.86rem;min-width:640px}}
table.tbl th{{color:var(--muted);font-weight:500;text-align:left;padding:.4rem .8rem;border-bottom:1px solid var(--border);font-size:.77rem}}
table.tbl td{{padding:.5rem .8rem;border-bottom:1px solid var(--border);white-space:nowrap}}
td.nm{{font-weight:600}} .sid{{color:var(--muted);font-size:.78em}} .sub{{color:var(--muted);font-size:.74em}}
.hwrap{{overflow-x:auto;padding:.3rem 1.4rem 1rem}}
table.heat{{border-collapse:separate;border-spacing:0;font-size:10px}}
th.hl{{position:sticky;left:0;background:var(--panel);text-align:left;padding:3px 8px;white-space:nowrap;border-right:1px solid var(--border);min-width:135px;font-weight:500}}
tr.pin th.hl{{box-shadow:inset 3px 0 0 #4ade80}}
td.hc{{width:4px;min-width:4px;height:15px;padding:0}} td.mh{{color:var(--muted);font-size:9px;padding:2px 0;height:16px}}
.foot{{color:var(--muted);font-size:.75rem;padding:.6rem 1.4rem 2rem;border-top:1px solid var(--border);margin-top:.4rem}}
</style></head><body>
<header><h1>個股趨勢追蹤器 · <span style="color:var(--muted)">{last_date}</span></h1>
<div class="sub">Weinstein 30W 階段 + RS(相對大盤)，由大盤 regime 把關。<span class="warn">定位：趨勢濾網，不是選股 alpha。</span>研究已證跟單分點的選股 edge 不可靠——這層趨勢才是真正可用的。｜更新 {datetime.now(TPE):%H:%M} TPE</div></header>
<div class="banner">{banner}</div>
<h2>觀察清單 · 個股趨勢燈</h2>
<div class="hint">🟢 個股 S2 + RS 強於大盤 <b>且</b> 大盤 regime 武裝＝順勢可跟；🟡 個股順勢但大盤 risk-off＝降級；🔴 S1/S4＝逆勢避開；灰＝中性。跟單分點時，用這盞燈決定「順不順勢」，別相信分點會選股。</div>
<table class="tbl"><tr><th>標的</th><th>現價</th><th>個股階段(30W)</th><th>RS vs 大盤</th><th>綜合順勢</th></tr>
{''.join(rows)}</table>
<h2>階段熱力圖 · 近 {len(heat_dates or [])} 交易日</h2>
<div class="hint">綠＝S2多頭(越亮越強)｜藍灰＝S1打底｜黃＝S3走緩｜紅＝S4下跌。綠色左標＝你跟單的景碩(3189)/頎邦(6147)。</div>
<div class="hwrap"><table class="heat"><tr><th class="hl">月</th>{''.join(mlabels)}</tr>{''.join(heat_rows)}</table></div>
<div class="foot">引擎：stage_series_daily（30週當量 MA＋當天確認，與專家池熱力圖同源）｜RS＝個股收盤/加權，rs_up＝高於自身50日均｜大盤 regime 取自 daily_tracker（外資台指期 z60 × 加權階段）。<br>
誠實聲明：趨勢/風控濾網，非買賣建議。分點跟單(含富邦新店/凱基松山)選股 edge 經嚴格檢定不可靠(動能假象或彩票分布)；自營台平均雖打敗成本但中位數為負、不可收割。此處只答「趨勢是否站你這邊」。</div>
</body></html>"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"  date={last_date} 大盤={mkt_stage} z60={z60:+.2f} armed={armed} watch={watch}")


if __name__ == "__main__":
    main()
