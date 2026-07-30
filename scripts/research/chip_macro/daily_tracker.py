"""每日籌碼→大盤風控燈號追蹤器 (chip-macro daily tracker).

Refreshes the panel from FinMind, computes the FROZEN L0xL2 system + component
lights, and writes a daily dashboard (Markdown + self-contained HTML) plus a
history CSV. Framing is honest: this is a RISK/regime overlay, not an alpha engine.

Lights:
  綜合 (SYSTEM)  : position = L0_armed x L2_size  -> 空手 / 半倉 / 全倉
  階段 (L0)      : Weinstein stage + armed? (close>MA150, MA150 rising, 6m TSMOM)
  籌碼 (L2)      : 外資期貨 z60 -> size ladder
  價量           : 成交金額 vs 50d 均量 (context)
  夜盤 (L3)      : TX after_market vs day close  (informational, UNVALIDATED)

Run:  .venv/bin/python scripts/research/chip_macro/daily_tracker.py [--no-refresh]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

PANEL_DIR = ROOT / "data" / "research" / "chip_macro"
OUT = ROOT / "reports" / "research" / "chip-macro"
HIST = OUT / "signal_history.csv"

TPE = timezone(timedelta(hours=8))


def load_panel() -> pd.DataFrame:
    """Prefer parquet, fall back to CSV (mini has no pyarrow)."""
    pq = PANEL_DIR / "panel.parquet"
    if pq.exists():
        try:
            return pd.read_parquet(pq)
        except Exception:
            pass
    return pd.read_csv(PANEL_DIR / "panel.csv")


def refresh_panel() -> None:
    import build_panel
    build_panel.main()


def fetch_night_bias():
    """TX near-month after_market close vs day-session close (informational)."""
    try:
        from finmind_client import fetch_finmind
        end = date.today()
        start = end - timedelta(days=10)
        raw = fetch_finmind("TaiwanFuturesDaily", "TX", start, end, timeout=60)
        rows = [r for r in raw if "/" not in str(r.get("contract_date", ""))]
        df = pd.DataFrame(rows)
        if df.empty:
            return None
        df["date"] = df["date"].astype(str).str[:10]
        # near-month = smallest contract_date per date
        df["cd"] = df["contract_date"].astype(str)
        near = df.sort_values(["date", "cd"]).groupby(["date", "trading_session"]).first().reset_index()
        piv = near.pivot_table(index="date", columns="trading_session", values="close", aggfunc="first")
        piv = piv.dropna(subset=[c for c in ["position"] if c in piv.columns])
        if "after_market" not in piv.columns or "position" not in piv.columns:
            return None
        last = piv.dropna().iloc[-1]
        bias = last["after_market"] / last["position"] - 1.0
        return {"date": piv.dropna().index[-1], "day_close": last["position"],
                "night_close": last["after_market"], "bias_pct": bias * 100}
    except Exception as e:
        return {"error": str(e)[:120]}


def load_maintenance() -> pd.DataFrame | None:
    """真實市場融資維持率 (FinMind TaiwanTotalExchangeMarginMaintenance)。"""
    try:
        import requests
        from project_dotenv import finmind_token_from_env
        tok = finmind_token_from_env()
        r = requests.get("https://api.finmindtrade.com/api/v4/data",
                         params={"dataset": "TaiwanTotalExchangeMarginMaintenance",
                                 "start_date": "2024-01-01", "token": tok}, timeout=40).json()
        d = pd.DataFrame(r.get("data", []))
        if d.empty:
            return None
        d["date"] = pd.to_datetime(d["date"]).astype(str).str[:10]
        return d.rename(columns={"TotalExchangeMarginMaintenance": "maint"})[["date", "maint"]]
    except Exception:
        return None


def compute_margin_light(p: pd.DataFrame) -> dict | None:
    """融資斷頭底監控燈 (C3 重驗結論): 維持率<145 深斷頭 × 外資期貨 doi 回補 雙確認。

    純監控、非交易訊號。維持率<145 是 IS 驗證(2018/20/22 崩盤 h20 hit 84%)的低頻底訊號;
    融資餘額投降本身無效(已證死);維持率5日急降不穩健(僅近期顯著)故不納入判準。
    """
    if "maint" not in p.columns or p["maint"].notna().sum() < 20:
        return None
    maint = p["maint"].astype(float)
    maint_last = maint.iloc[-1]
    if pd.isna(maint_last):
        return None
    # 外資期貨 doi 回補連續天數 (doi>0 = 淨空回補方向)
    doi = p["fut_foreign_doi"]
    streak = 0
    for v in doi.iloc[::-1]:
        if pd.notna(v) and v > 0:
            streak += 1
        else:
            break
    # 融資 froth 情境 (panel 直接算)
    mb = p["margin_bal"].astype(float)
    mb_z60 = ((mb - mb.rolling(60).mean()) / mb.rolling(60).std()).iloc[-1]
    mb_d1 = mb.diff().iloc[-1]
    szr = (p["short_bal"].astype(float) / mb * 100).iloc[-1]
    deep = bool(maint_last < 145)       # IS 驗證的深斷頭底
    near = bool(maint_last < 150)       # 觀察區
    cover = bool(streak >= 2)           # champion 前緣確認
    dual = deep and cover
    return {"maint": float(maint_last), "deep": deep, "near": near,
            "cover_streak": int(streak), "cover": cover, "dual": dual,
            "gap_to_145": float(maint_last - 145),
            "mb_z60": float(mb_z60) if pd.notna(mb_z60) else None,
            "mb_d1": float(mb_d1) if pd.notna(mb_d1) else None,
            "szr": float(szr) if pd.notna(szr) else None}


VIX_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?interval=1d&range=6mo"


def load_vix() -> dict | None:
    """VIX 恐慌 gate — 真·正交於 champion 的 L0 風控燈 (dashboard-completeness 驗證)。

    研究 (reports/research/dashboard-completeness/global_macro.md): VIX 60日 z-score>+2.0
    → 次日 TAIEX 均報酬 −0.099% vs 平時 +0.088%,同曝險 permutation p=0.035;z>+1.5 p=0.069
    邊際。與 champion(外資期貨 fut_foreign_oi) 相關僅 ±0.03 → 非冗餘,是獨立 risk-off 否決燈。
    純風控、非交易訊號;連續因子(level/Δ)本身無 alpha,只有事件式 spike 有弱前兆。
    """
    try:
        import requests
        r = requests.get(VIX_CHART, headers={"User-Agent": "Mozilla/5.0"}, timeout=30).json()
        res = r["chart"]["result"][0]
        closes = res["indicators"]["quote"][0]["close"]
        s = pd.Series([c for c in closes if c is not None])
        if len(s) < 61:
            return None
        z = (s - s.rolling(60, min_periods=60).mean()) / s.rolling(60, min_periods=60).std()
        z60 = float(z.iloc[-1])
        return {"vix": float(s.iloc[-1]), "vix_z60": z60,
                "spike": bool(z60 > 2.0), "warn": bool(z60 > 1.5)}
    except Exception:
        return None


def _vix_verdict(v: dict) -> tuple[str, str]:
    """(light emoji, state text)."""
    if v["spike"]:
        return "🔴", (f"恐慌 gate 觸發（VIX {v['vix']:.1f}, z60 {v['vix_z60']:+.2f}>2）"
                      "→ risk-off，建議不重新武裝多單")
    if v["warn"]:
        return "🟡", f"VIX 升溫（{v['vix']:.1f}, z60 {v['vix_z60']:+.2f}>1.5，邊際警戒）"
    return "🟢", f"VIX 平穩（{v['vix']:.1f}, z60 {v['vix_z60']:+.2f}）"


def compute(p: pd.DataFrame) -> dict:
    ma150 = p["ix_close"].rolling(150, min_periods=150).mean()
    slope = ma150 - ma150.shift(25)
    slope_up = slope > 0
    tsmom = (p["ix_close"] / p["ix_close"].shift(126) - 1.0)
    above = p["ix_close"] > ma150
    armed = (above & slope_up & (tsmom > 0))
    stage = np.where(above & slope_up, "S2 上升",
             np.where(above & ~slope_up, "S3 頭部",
             np.where(~above & ~slope_up, "S4 下降", "S1 基底")))
    z60 = (p["fut_foreign_oi"] - p["fut_foreign_oi"].rolling(60, min_periods=60).mean()) \
        / p["fut_foreign_oi"].rolling(60, min_periods=60).std()
    size = np.where(z60 <= 0, 0.0, np.where(z60 >= 1, 1.0, 0.5))
    pos = armed.astype(float) * size
    volm = p["ix_money"]
    vol_ratio = volm / volm.rolling(50, min_periods=50).mean()
    p = p.assign(ma150=ma150, slope25=slope, tsmom126=tsmom, armed=armed,
                 stage=stage, z60=z60, size=size, position=pos, vol_ratio=vol_ratio)
    return {"panel": p}


SIZE_TXT = {0.0: "空手", 0.5: "半倉", 1.0: "全倉"}


def _margin_verdict(m: dict) -> tuple[str, str, str]:
    """(light emoji, dot-bool-for-html, state text)."""
    if m["dual"]:
        return "🟢", "green", "雙確認底訊號亮起（維持率斷頭 × 外資回補）"
    if m["deep"]:
        return "🟡", "amber", "維持率進 145% 斷頭區，待外資期貨回補確認"
    if m["cover"]:
        return "⚪", "gray", f"外資回補 {m['cover_streak']} 日，但維持率未到斷頭區"
    return "⚪", "gray", "未觸發（監控中）"


def build_dashboard(p: pd.DataFrame, night, margin=None, vix=None) -> tuple[str, str, dict]:
    last = p.iloc[-1]
    prev = p.iloc[-2]
    d = str(last["date"])
    ix = last["ix_close"]
    ixchg = (last["ix_close"] / prev["ix_close"] - 1.0) * 100
    pos = last["position"]; sz = last["size"]
    stage = last["stage"]; armed = bool(last["armed"])
    z60 = last["z60"]

    def light(v):  # traffic dot
        return "🟢" if v else "🔴"
    # component states
    l0_ok = armed
    l2_ok = sz > 0
    vol_hot = last["vol_ratio"] > 1.0 if pd.notna(last["vol_ratio"]) else None

    verdict = SIZE_TXT.get(pos, f"{pos:.1f}")
    if pos > 0:
        headline = f"做多（{verdict}）"
    else:
        if not l0_ok:
            headline = "空手 — 趨勢環境不利（regime 未武裝）"
        elif not l2_ok:
            headline = "空手 — 趨勢OK，但外資期貨未確認（籌碼 risk-off）"
        else:
            headline = "空手"

    # markdown
    md = []
    md.append(f"# 籌碼→大盤 風控燈號 ｜ {d}")
    md.append(f"_更新時間 {datetime.now(TPE):%Y-%m-%d %H:%M} (TPE) ｜ 風控 overlay，非投資建議_\n")
    md.append(f"## 綜合部位：**{headline}**\n")
    md.append(f"> 大盤 {ix:,.0f}（{ixchg:+.2f}%）｜ 系統部位 = {pos:.1f}（L0 階段 × L2 籌碼）\n")
    md.append("| 燈 | 狀態 | 細節 |")
    md.append("|---|---|---|")
    md.append(f"| {light(l0_ok)} 階段 (L0) | {stage}{'，已武裝' if armed else '，未武裝'} | "
              f"MA150={last['ma150']:,.0f}（{'站上' if ix>last['ma150'] else '跌破'}）｜"
              f"斜率{'↑' if last['slope25']>0 else '↓'}｜6月動能{'+' if last['tsmom126']>0 else '−'} |")
    md.append(f"| {light(l2_ok)} 籌碼 (L2) | 外資期貨 z60={z60:+.2f} → {SIZE_TXT[sz]} | "
              f"淨未平倉 {last['fut_foreign_oi']:,.0f} 口（日{last['fut_foreign_doi']:+,.0f}）｜"
              f"現貨外資 {last['foreign']:+.0f} 億 |")
    vtxt = ('放量' if vol_hot else '量縮') if vol_hot is not None else 'n/a'
    md.append(f"| {'🟡' if vol_hot else '⚪'} 價量 | {vtxt}（{last['vol_ratio']:.2f}×50日均） | "
              f"投信 {last['trust']:+.0f}｜自營 {last['dealer']:+.0f} 億 |")
    if margin:
        ml, _, mstate = _margin_verdict(margin)
        froth = (f"融資 froth z60={margin['mb_z60']:+.2f}｜券資比 {margin['szr']:.2f}%"
                 if margin.get("mb_z60") is not None else "")
        md.append(f"| {ml} 融資斷頭底 (監控) | {mstate} | "
                  f"維持率 {margin['maint']:.1f}%（距145斷頭區 {margin['gap_to_145']:+.1f}pp）｜"
                  f"外資期貨回補 {margin['cover_streak']} 日｜{froth} |")
    if vix:
        vl, vstate = _vix_verdict(vix)
        md.append(f"| {vl} 恐慌 gate (L0, 監控) | {vstate} | "
                  f"VIX {vix['vix']:.1f}｜60日 z-score {vix['vix_z60']:+.2f}（>2 觸發, perm p=0.035） |")
    if night and "error" not in night:
        nb = night["bias_pct"]
        md.append(f"| {'🟢' if nb>0 else '🔴'} 夜盤 (L3, 參考) | 夜盤{nb:+.2f}% vs 日盤 | "
                  f"日{night['day_close']:,.0f}→夜{night['night_close']:,.0f}（未驗證，僅參考） |")
    md.append("")
    # last 6 days
    md.append("### 近 6 日燈號")
    md.append("| 日期 | 大盤 | 漲跌 | 階段 | z60 | 部位 |")
    md.append("|---|---|---|---|---|---|")
    tail = p.tail(6).iloc[::-1]
    for _, r in tail.iterrows():
        ptxt = SIZE_TXT.get(r["position"], f"{r['position']:.1f}")
        md.append(f"| {r['date']} | {r['ix_close']:,.0f} | | {r['stage']} | {r['z60']:+.2f} | {ptxt} |")
    md.append("")
    md.append("_定位：**風控工具、非打敗大盤**。近8年系統總報酬 x1.78 < 買進持有 x3.66（賺約一半），"
              "但 Sharpe +1.31>0.94、回撤 −10% vs −32%——用放棄上漲換平穩。84% 時間空手，"
              "只在趨勢×外資期貨同時確認才做多。DSR 未過關量級勿過度信；2022 空頭為弱點；訊號會衰減。_")
    md_txt = "\n".join(md)

    # html (self-contained, theme-aware traffic-light)
    def dot(v, on="#16a34a", off="#dc2626"):
        return f'<span style="color:{on if v else off};font-size:1.4em">●</span>'
    night_row = ""
    if night and "error" not in night:
        nb = night["bias_pct"]
        night_row = f'<tr><td>{dot(nb>0)} 夜盤 <small>(L3參考)</small></td><td>{nb:+.2f}% vs 日盤</td><td>{night["day_close"]:,.0f}→{night["night_close"]:,.0f}（未驗證）</td></tr>'
    margin_row = ""
    if margin:
        _dotmap = {"green": "🟢", "amber": "🟡", "gray": "⚪"}
        _e, _c, mstate = _margin_verdict(margin)
        froth = (f"融資 froth z60 {margin['mb_z60']:+.2f}・券資比 {margin['szr']:.2f}%"
                 if margin.get("mb_z60") is not None else "")
        margin_row = (f'<tr><td>{_dotmap[_c]} 融資斷頭底 <small>(監控)</small></td><td>{mstate}</td>'
                      f'<td>維持率 {margin["maint"]:.1f}%（距145斷頭區 {margin["gap_to_145"]:+.1f}pp）・'
                      f'外資期貨回補 {margin["cover_streak"]}日・{froth}</td></tr>')
    vix_row = ""
    if vix:
        _ve, _vstate = _vix_verdict(vix)
        vix_row = (f'<tr><td>{_ve} 恐慌 gate <small>(L0監控)</small></td><td>{_vstate}</td>'
                   f'<td>VIX {vix["vix"]:.1f}・60日 z {vix["vix_z60"]:+.2f}（&gt;2 觸發, perm p=0.035）</td></tr>')
    poscolor = "#16a34a" if pos > 0 else "#6b7280"
    html = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>籌碼→大盤 風控燈號 {d}</title>
<style>
:root{{color-scheme:light dark}}
body{{font-family:-apple-system,'PingFang TC',sans-serif;max-width:760px;margin:24px auto;padding:0 16px;line-height:1.5}}
h1{{font-size:1.3em;margin:.2em 0}} .sub{{color:#888;font-size:.85em}}
.verdict{{font-size:1.8em;font-weight:700;color:{poscolor};margin:.4em 0}}
.ix{{font-size:1.1em;color:#666;margin-bottom:1em}}
table{{border-collapse:collapse;width:100%;margin:.6em 0}}
td,th{{border:1px solid #8883;padding:8px 10px;text-align:left;font-size:.95em}}
th{{background:#8881}} small{{color:#999}}
.note{{color:#888;font-size:.82em;margin-top:1em;border-top:1px solid #8883;padding-top:.8em}}
</style></head><body>
<h1>籌碼 → 大盤 風控燈號</h1>
<div class="sub">{d} ｜ 更新 {datetime.now(TPE):%H:%M} TPE ｜ 風控 overlay，非投資建議</div>
<div class="verdict">{headline}</div>
<div class="ix">大盤 {ix:,.0f} <span style="color:{'#16a34a' if ixchg>=0 else '#dc2626'}">({ixchg:+.2f}%)</span> ｜ 系統部位 {pos:.1f}</div>
<table>
<tr><th>燈</th><th>狀態</th><th>細節</th></tr>
<tr><td>{dot(l0_ok)} 階段 <small>(L0)</small></td><td>{stage}{'・已武裝' if armed else '・未武裝'}</td><td>MA150 {last['ma150']:,.0f}・斜率{'↑' if last['slope25']>0 else '↓'}・6月動能{'+' if last['tsmom126']>0 else '−'}</td></tr>
<tr><td>{dot(l2_ok)} 籌碼 <small>(L2)</small></td><td>外資期貨 z60 {z60:+.2f} → {SIZE_TXT[sz]}</td><td>淨未平倉 {last['fut_foreign_oi']:,.0f} 口 (日{last['fut_foreign_doi']:+,.0f})・現貨外資 {last['foreign']:+.0f}億</td></tr>
<tr><td>{'🟡' if vol_hot else '⚪'} 價量</td><td>{vtxt} ({last['vol_ratio']:.2f}×)</td><td>投信 {last['trust']:+.0f}・自營 {last['dealer']:+.0f}億</td></tr>
{margin_row}
{vix_row}
{night_row}
</table>
<div class="note"><b>定位：風控工具、非打敗大盤。</b>近8年系統總報酬 x1.78 &lt; 買進持有 x3.66（約賺一半），但 Sharpe +1.31&gt;0.94、最大回撤 −10% vs −32%——用放棄上漲換平穩。84% 時間空手，只在趨勢 × 外資期貨同時確認才做多。Deflated Sharpe 未過關 → 量級勿過度相信；2022 空頭為已知弱點；訊號會衰減需持續監控。</div>
</body></html>"""

    state = {"date": d, "ix_close": ix, "ix_chg_pct": round(ixchg, 2),
             "stage": stage, "armed": armed, "z60": round(float(z60), 3),
             "size": sz, "position": pos, "headline": headline}
    if margin:
        state.update({"maint": round(margin["maint"], 1),
                      "maint_deep145": margin["deep"],
                      "fut_cover_streak": margin["cover_streak"],
                      "margin_dual_bottom": margin["dual"]})
    if vix:
        state.update({"vix": round(vix["vix"], 1), "vix_z60": round(vix["vix_z60"], 2),
                      "vix_spike": vix["spike"]})
    return md_txt, html, state


def push_ops_console() -> None:
    """Best-effort push of the chip_macro snapshot to Supabase ops_snapshots.

    Done directly here (not only via evening-sync) because evening-sync runs at the
    same 20:00 slot -> pushing here guarantees the console shows today's fresh state.
    """
    try:
        from ops_console_sync import insert_snapshot, supabase_ops_configured
        from ops_console_snapshots import build_chip_macro_payload
        if not supabase_ops_configured():
            print("(ops-console: Supabase 未設定，略過推送)")
            return
        insert_snapshot(kind="chip_macro", payload=build_chip_macro_payload())
        print("(ops-console: chip_macro snapshot 已推送)")
    except Exception as e:  # never fail the tracker on ops-console issues
        print(f"(ops-console 推送略過: {type(e).__name__}: {e})")


def append_history(state: dict):
    row = pd.DataFrame([state])
    if HIST.exists():
        old = pd.read_csv(HIST)
        old = old[old["date"] != state["date"]]
        row = pd.concat([old, row], ignore_index=True)
    row.to_csv(HIST, index=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-refresh", action="store_true", help="skip daily-panel FinMind pull, use existing panel (morning run)")
    ap.add_argument("--no-night", action="store_true", help="skip TX night-session fetch")
    ap.add_argument("--no-maint", action="store_true", help="skip 融資維持率 fetch (斷頭底監控燈)")
    ap.add_argument("--no-vix", action="store_true", help="skip VIX 恐慌 gate fetch (L0 風控燈)")
    ap.add_argument("--no-ops", action="store_true", help="skip Supabase ops-console push")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    if not args.no_refresh:
        print("Refreshing panel from FinMind ...")
        refresh_panel()
    p = load_panel().sort_values("date").reset_index(drop=True)
    res = compute(p)
    # night fetch is cheap + independent of the daily panel -> always attempt (unless --no-night)
    # so the 06:00 morning run can update the night light on the prior close's dashboard.
    night = None if args.no_night else fetch_night_bias()
    vix = None if args.no_vix else load_vix()
    md, html, state = build_dashboard(res["panel"], night, vix=vix)

    (OUT / "daily_dashboard.md").write_text(md, encoding="utf-8")
    (OUT / "daily_dashboard.html").write_text(html, encoding="utf-8")
    append_history(state)
    if not args.no_ops:
        push_ops_console()
    print("\n" + md)
    print(f"\n-> {OUT/'daily_dashboard.md'}\n-> {OUT/'daily_dashboard.html'}\n-> {HIST}")


if __name__ == "__main__":
    main()
