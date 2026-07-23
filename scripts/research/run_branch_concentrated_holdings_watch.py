#!/usr/bin/env python3
"""重倉持股每日監控 · 具大跌預測經驗分點的當前重倉股是否被賣出.

Research / monitoring diagnostic · 非正式風控規則 · 非可下單訊號 ·
尚未登錄 `config/research.yaml`。

## 背景與方法（2026-07-23 凍結）

延伸自大跌溫度計（`run_market_crash_thermometer_dashboard.py`）的固定8家
panel，改用兩個可重現的篩選條件在全市場 297 家分點中挑「更寬」的候選：

1. **有預測經驗**：對 297 家分點分別計算「大跌事件前5個交易日內，是否曾落在
   自己歷史後10%重賣超」的命中率，vs. 用該分點自身歷史（PIT-safe rolling
   OR）算出的**經驗基期**（非理論獨立假設，因為賣超行為本身有序列相關性，
   獨立假設會嚴重高估基期、稀釋真訊號）。取 lift>=1.8 且 binomial p<0.15
   （15個單日<=-3%大跌事件，2024-07~2026-07 grid 覆蓋範圍）。
2. **有重倉持股**：該分點近120個交易日內，至少一檔股票的累積淨買賣量
   達到該股當期日均量的3倍以上（概念上=用「流量估算存量」，不是真實庫存，
   但相對大小夠明顯，賣起來才可能對股價有實質影響——見對話中同日驗證：
   賣超落在自身歷史後2%尾端且賣超量>=該股當日均量15%時，當天平均跌1.07%，
   t=-9.57 p<0.0001；但次日無領先性，純粹同步）。

同時符合兩者的分點：美商高盛(1480)、港商野村(1560)、統一(5850)、
港麥格理(1360)、土銀台南(1032)、群益館前(913Y)、康和台北(845U)、
兆豐台中港(7005)、第一金高雄(5383)。

## 已知限制（务必读）

- 「重倉持股」清單用**流量估算存量**（累積淨買賣量/該股日均量），不是
  真實庫位數字——分點交易資料本身沒有庫存快照，這是所有分點研究的共同限制。
- 這是**同步指標**，不是領先指標：驗證顯示分點重賣超與股價當日走弱同步，
  次日沒有額外領先性。用途是「盤後儘快知道這些有經驗的分點是否在動他們的
  重倉」，不是預測明天大盤。
- 候選名單本身是用全歷史資料一次性篩出來的（非嚴格逐日 walk-forward），
  部分分點（尤其兆豐台中港、第一金高雄）p值在0.1~0.15之間，屬於「還算
  但不到強證據」等級，解讀時請留意最後一欄的顯著性標記。
- 「重倉」門檻(3倍ADV)、「重賣超」門檻(20百分位)為研究階段選定的合理值，
  未做過如大跌溫度計那樣完整的門檻回測；本腳本用途是「監控清單」，
  不是校準過的警報系統。
- `stock_daily_bars`（用來算日均量ADV）只覆蓋精選約277檔股票池，不是全市場；
  部分股票在這個池子裡歷史資料極少（甚至只有1天）。若直接拿來算ADV會被
  單日爆量嚴重扭曲、造成集中度虛高（2026-07-23發現：兆豐台中港x7706曾
  算出260倍ADV，其實是ADV只用1天資料算出來的假象）。已修正為要求該股
  至少有 `ADV_MIN_SAMPLE_DAYS` 天資料才採計，資料不足的股票會被排除，
  不會出現在重倉清單中——這也代表清單會變短，是預期中的結果，不是bug。

輸出：
  reports/daily/{YYYYMMDD}_branch_holdings_watch.md
  reports/daily/branch_holdings_watch.md（穩定路徑）

用法：
  PYTHONPATH=src .venv/bin/python \\
    scripts/research/run_branch_concentrated_holdings_watch.py [--asof YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

DB_PATH = ROOT / "data" / "stocks.db"
OUT = ROOT / "reports" / "daily"
SOURCE = "finmind"

# 分點ID → (名稱, lift, p_value) —— 見上方 docstring 篩選方法
PANEL: dict[str, tuple[str, float, float]] = {
    "1480": ("美商高盛", 2.83, 0.0029),
    "1560": ("港商野村", 2.59, 0.0103),
    "5850": ("統一", 2.10, 0.0314),
    "1360": ("港麥格理", 2.01, 0.0600),
    "1032": ("土銀台南", 2.30, 0.0333),
    "913Y": ("群益館前", 2.17, 0.0431),
    "845U": ("康和台北", 1.90, 0.0516),
    "7005": ("兆豐台中港", 1.85, 0.1173),
    "5383": ("第一金高雄", 1.83, 0.1219),
}

CONCENTRATION_WINDOW_DAYS = 120
CONCENTRATION_MIN_ADV = 3.0
ADV_MIN_SAMPLE_DAYS = 60
TOP_N_HOLDINGS = 3
SELF_RANK_MIN_DAYS = 15
SELL_FLAG_TAIL_PCT = 0.20


def sig_note(p: float) -> str:
    if p < 0.05:
        return "顯著"
    if p < 0.10:
        return "尚可"
    return "邊緣"


def build_current_holdings(
    conn: sqlite3.Connection, branch_ids: list[str], asof: str
) -> pd.DataFrame:
    """近 CONCENTRATION_WINDOW_DAYS 個交易日（含 asof 前一日為止）的重倉估算。

    PK 為 (trade_date, ...)，查詢務必帶明確的 trade_date 下界（曆日近似值，
    多留緩衝天數），否則在 8600萬列的大表上會變成全表掃描。
    """
    placeholders = ",".join("?" for _ in branch_ids)
    window_start = (
        date.fromisoformat(asof) - timedelta(days=int(CONCENTRATION_WINDOW_DAYS * 1.6) + 10)
    ).isoformat()

    raw = pd.read_sql_query(
        f"""
        SELECT securities_trader_id, stock_id, trade_date, net
        FROM stock_broker_branch_daily
        WHERE source=? AND securities_trader_id IN ({placeholders})
          AND stock_id != '__EMPTY__' AND length(stock_id)=4
          AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
          AND trade_date >= ? AND trade_date <= ?
        """,
        conn,
        params=[SOURCE, *branch_ids, window_start, asof],
    )
    raw["net"] = raw["net"].astype(float)
    # keep only the trailing CONCENTRATION_WINDOW_DAYS distinct trading days
    keep_dates = sorted(raw["trade_date"].unique())[-CONCENTRATION_WINDOW_DAYS:]
    if keep_dates:
        raw = raw[raw["trade_date"].isin(keep_dates)]
        window_start = min(keep_dates)

    # stock_daily_bars 只覆蓋精選 ~277 檔股票池（非全市場），且部分股票在這個
    # 池子裡只有寥寥幾天資料——樣本太少時 AVG(volume) 會被單日爆量嚴重扭曲，
    # 造成「集中度」虛高。要求至少 ADV_MIN_SAMPLE_DAYS 天資料才採用該股 ADV，
    # 不足的股票直接視為「無法驗證集中度」而排除，不是當成 0 或忽略下限。
    vol = pd.read_sql_query(
        """
        SELECT stock_id, AVG(volume) AS adv, COUNT(*) AS adv_n
        FROM stock_daily_bars
        WHERE source='finmind' AND trade_date >= ? AND trade_date <= ?
          AND length(stock_id)=4 AND stock_id GLOB '[0-9][0-9][0-9][0-9]'
        GROUP BY stock_id
        HAVING COUNT(*) >= ?
        """,
        conn,
        params=[window_start, asof, ADV_MIN_SAMPLE_DAYS],
    )

    cum = raw.groupby(["securities_trader_id", "stock_id"])["net"].sum().reset_index()
    cum = cum.merge(vol, on="stock_id", how="left")
    cum["conc_adv_days"] = cum["net"].abs() / cum["adv"].replace(0, pd.NA)
    cum = cum.dropna(subset=["conc_adv_days"])

    rows = []
    for bid in branch_ids:
        sub = cum[cum["securities_trader_id"] == bid]
        sub = sub[sub["conc_adv_days"] >= CONCENTRATION_MIN_ADV]
        top = sub.sort_values("conc_adv_days", ascending=False).head(TOP_N_HOLDINGS)
        for _, r in top.iterrows():
            rows.append(
                {
                    "branch_id": bid,
                    "stock_id": r["stock_id"],
                    "cum_net_shares": r["net"],
                    "stock_adv": r["adv"],
                    "conc_adv_days": r["conc_adv_days"],
                    "direction": "buy_heavy" if r["net"] > 0 else "sell_heavy",
                }
            )
    return pd.DataFrame(rows)


def check_today_flow(
    conn: sqlite3.Connection, holdings: pd.DataFrame, asof: str
) -> pd.DataFrame:
    """對每個(分點,重倉股) pair，取今日淨額，並在該pair自身歷史中排百分位。

    一次性依 stock_id 分批拉取（PK 為 trade_date 開頭，無法用
    securities_trader_id+stock_id 高效索引，逐筆查詢在大表上會是全表掃描），
    改成 stock_id IN (...) 一次撈，再用 pandas 依 (branch,stock) 分組排百分位。
    """
    if holdings.empty:
        return holdings
    stock_ids = holdings["stock_id"].unique().tolist()
    branch_ids = holdings["branch_id"].unique().tolist()
    placeholders_s = ",".join("?" for _ in stock_ids)
    placeholders_b = ",".join("?" for _ in branch_ids)
    # 帶明確 trade_date 下界（曆日近似，留緩衝），否則在8600萬列大表上全表掃描。
    hist_start = (
        date.fromisoformat(asof) - timedelta(days=int(SELF_RANK_MIN_DAYS * 1.8) + 60)
    ).isoformat()
    hist = pd.read_sql_query(
        f"""
        SELECT trade_date, securities_trader_id, stock_id, net
        FROM stock_broker_branch_daily
        WHERE source=? AND stock_id IN ({placeholders_s})
          AND securities_trader_id IN ({placeholders_b})
          AND trade_date >= ? AND trade_date <= ?
        """,
        conn,
        params=[SOURCE, *stock_ids, *branch_ids, hist_start, asof],
    )
    hist["net"] = hist["net"].astype(float)

    out_rows = []
    for _, h in holdings.iterrows():
        bid, sid = h["branch_id"], h["stock_id"]
        sub = hist[(hist["securities_trader_id"] == bid) & (hist["stock_id"] == sid)]
        sub = sub.sort_values("trade_date")
        if len(sub) < SELF_RANK_MIN_DAYS:
            # 歷史樣本不足，無法排百分位（非「今天沒資料」，是這個 pair 交易天數太少）
            today_net = None
            sell_pctile = None
            has_today = False
        elif sub["trade_date"].iloc[-1] != asof:
            # 表內沒有 asof 那天的列＝當天沒買賣這支股票（淨額視為0，非資料缺漏）
            today_net = 0.0
            sell_pctile = float((sub["net"] <= 0.0).mean())
            has_today = True
        else:
            today_net = float(sub["net"].iloc[-1])
            sell_pctile = float((sub["net"] <= today_net).mean())
            has_today = True
        out_rows.append(
            {
                **h.to_dict(),
                "today_net": today_net,
                "today_sell_pctile": sell_pctile,
                "has_today_data": has_today,
                "sell_flag": (
                    has_today
                    and today_net is not None
                    and today_net < 0
                    and sell_pctile is not None
                    and sell_pctile <= SELL_FLAG_TAIL_PCT
                ),
            }
        )
    return pd.DataFrame(out_rows)


# stock_daily_bars 沒有股票中文名稱欄位；只列出目前重倉候選清單中出現過的代號，
# 缺的代號會直接顯示股票代號本身（不影響監控邏輯，只影響顯示可讀性）。
STOCK_NAMES: dict[str, str] = {
    "2385": "群光", "2867": "三商壽", "3078": "僑威", "2731": "雄獅",
    "2606": "裕民", "1229": "聯華", "3040": "遠見", "2543": "皇昌",
    "1101": "台泥", "2633": "台灣高鐵", "2912": "統一超", "2501": "國建",
    "2017": "官田鋼", "1454": "台富", "1220": "台榮", "3158": "腦波",
    "5511": "德昌", "2890": "永豐金",
}


def load_names(conn: sqlite3.Connection, stock_ids: list[str]) -> dict[str, str]:
    return {sid: STOCK_NAMES.get(sid, "") for sid in stock_ids}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD，預設今天")
    args = ap.parse_args()
    asof = args.asof or date.today().isoformat()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    branch_ids = list(PANEL.keys())
    holdings = build_current_holdings(conn, branch_ids, asof)
    print(f"重倉候選: {len(holdings)} 筆 (asof={asof})", flush=True)
    scored = check_today_flow(conn, holdings, asof)

    stock_names = load_names(conn, scored["stock_id"].unique().tolist()) if len(scored) else {}
    conn.close()

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = asof.replace("-", "")

    lines = [
        f"分點重倉持股監控 · {asof}",
        "",
        "Research / monitoring diagnostic · 非正式風控規則 · 非可下單訊號。",
        "",
        "===== 今日賣超異常提示 =====",
        "",
    ]

    flagged = scored[scored["sell_flag"] == True] if len(scored) else pd.DataFrame()  # noqa: E712
    if flagged.empty:
        lines.append("今日無任何分點對其重倉股出現異常賣超（自身歷史後20%尾端）。")
    else:
        for _, r in flagged.sort_values("today_sell_pctile").iterrows():
            name, lift, pval = PANEL[r["branch_id"]]
            sname = stock_names.get(r["stock_id"], "")
            lines.append(
                f"⚠️ {name}({r['branch_id']}) 賣 {r['stock_id']}{sname}："
                f"今日淨額 {r['today_net']:+,.0f} 股，落在自身歷史後"
                f"{r['today_sell_pctile']*100:.0f}%（重倉集中度 {r['conc_adv_days']:.1f}x日均量）"
                f"[分點驗證 lift={lift} p={pval} {sig_note(pval)}]"
            )

    lines += ["", "===== 9家分點目前重倉持股明細（近120個交易日滾動窗口） =====", ""]
    lines.append("分點(驗證lift/p)          重倉股      持倉方向  集中度(x日均量)  今日淨額         今日百分位")
    for bid, (name, lift, pval) in sorted(PANEL.items(), key=lambda kv: -kv[1][1]):
        sub = scored[scored["branch_id"] == bid] if len(scored) else pd.DataFrame()
        if sub.empty:
            lines.append(f"{name}({bid}) lift={lift}/p={pval}{sig_note(pval)}    (近半年無達3x日均量的重倉股)")
            continue
        for _, r in sub.sort_values("conc_adv_days", ascending=False).iterrows():
            sname = stock_names.get(r["stock_id"], "")
            net_s = f"{r['today_net']:+,.0f}" if r["today_net"] is not None else "無today資料"
            pct_s = f"{r['today_sell_pctile']*100:.0f}%" if r["today_sell_pctile"] is not None else "-"
            flag_s = " ⚠️" if r["sell_flag"] else ""
            lines.append(
                f"{name}({bid}) lift={lift}/p={pval}{sig_note(pval)}  "
                f"{r['stock_id']}{sname}  {r['direction']}  {r['conc_adv_days']:.1f}x  "
                f"{net_s}  {pct_s}{flag_s}"
            )

    lines += [
        "",
        "===== 怎麼看 =====",
        "",
        "1. 「重倉股」= 該分點近半年累積淨買賣量估算，達到該股當期日均量3倍以上的持股",
        "   （用流量估算存量，非真實庫位快照，是所有分點研究的共同限制）。",
        "2. 「今日百分位」= 今日淨額在該分點對這支股票自己歷史賣超分布中的排名，",
        "   越低代表越罕見的重賣超（≤20%＝觸發⚠️提示）。",
        "3. 這是同步指標，不是領先指標：驗證顯示分點重賣超與股價當日走弱同步，",
        "   不代表能預測次日或未來走勢，用途是盤後儘快掌握這些分點的動作。",
        "4. lift/p 為該分點在15個單日大跌事件(2024-07~2026-07)前5日累計賣超",
        "   命中率相對自身歷史基期的倍數與顯著性，p<0.05顯著／<0.10尚可／",
        "   <0.15邊緣，數字越邊緣代表證據強度越弱。",
        "5. 若「今日淨額」顯示 nan，代表這個分點對這支股票的歷史交易天數太少",
        "   （<15天），不足以排百分位，通常發生在很久才交易一次的重倉股上，",
        "   仍會顯示絕對金額供參考，但異常提示不會對這類 pair 觸發。",
    ]

    text = "\n".join(lines) + "\n"
    (OUT / f"{stamp}_branch_holdings_watch.md").write_text(text, encoding="utf-8")
    (OUT / "branch_holdings_watch.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
