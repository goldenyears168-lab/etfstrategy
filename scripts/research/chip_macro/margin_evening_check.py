#!/usr/bin/env python3
"""盤後融資餘額抓取 + 多殺多判斷 (TWSE 官方 MI_MARGN)。

用法: python margin_evening_check.py [YYYYMMDD]   # 預設今天
輸出: 單日失血排名 / z60 / 券資比走向 / 去化完成度 / 投降vs續殺 判斷。
資料源: https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN (selectType=MS 信用交易統計)
"""
import sys, datetime, requests
import pandas as pd

PANEL = "data/research/chip_macro/panel.parquet"
PEAK_2026 = 9618551  # 融資餘額(交易單位)全期峰值 07-03
H = {"User-Agent": "Mozilla/5.0"}


def fetch_twse(date_yyyymmdd: str) -> dict:
    url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={date_yyyymmdd}&selectType=MS&response=json"
    j = requests.get(url, headers=H, timeout=15).json()
    if j.get("stat") != "OK":
        return {"ok": False, "stat": j.get("stat"), "title": None}
    out = {"ok": True, "title": None}
    for t in j.get("tables", []):
        if t.get("title"):
            out["title"] = t["title"]
        for row in t.get("data", []):
            key = str(row[0])
            if key == "融資(交易單位)":
                out["margin_today"] = int(row[5].replace(",", ""))
                out["margin_prev"] = int(row[4].replace(",", ""))
            elif key == "融券(交易單位)":
                out["short_today"] = int(row[5].replace(",", ""))
                out["short_prev"] = int(row[4].replace(",", ""))
            elif key == "融資金額(仟元)":
                out["margin_amt_today"] = int(row[5].replace(",", ""))
                out["margin_amt_prev"] = int(row[4].replace(",", ""))
    return out


def z60_for(new_val: float) -> float:
    """用 panel 歷史 margin_bal 的最後 59 筆 + 今天新值算 z60。"""
    try:
        p = pd.read_parquet(PANEL).dropna(subset=["margin_bal"]).sort_values("date")
        hist = list(p["margin_bal"].values[-59:]) + [new_val]
        s = pd.Series(hist)
        return (new_val - s.mean()) / s.std()
    except Exception:
        return float("nan")


def main():
    date = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime("%Y%m%d")
    d = fetch_twse(date)
    print(f"=== TWSE 融資檢查 {date} ===")
    if not d.get("ok"):
        print(f"⚠️  當日無資料 (stat={d.get('stat')}) — 可能非交易日或尚未公布(晚間才更新)")
        return
    mt, mp = d["margin_today"], d["margin_prev"]
    d1 = mt - mp
    szr = d["short_today"] / mt * 100
    szr_prev = d["short_prev"] / mp * 100
    short_d1 = d["short_today"] - d["short_prev"]
    z = z60_for(mt)
    from_peak = (mt - PEAK_2026) / PEAK_2026 * 100
    amt_d1_yi = (d["margin_amt_today"] - d["margin_amt_prev"]) / 1e5  # 仟元→億

    print(d.get("title", ""))
    print(f"融資今日餘額 {mt:,} 張 | 前日 {mp:,} | 單日 {d1:+,} 張 ({amt_d1_yi:+.0f} 億)")
    print(f"融券今日餘額 {d['short_today']:,} 張 | 單日 {short_d1:+,}")
    print(f"券資比 {szr:.3f}% (前日 {szr_prev:.3f}%, {'升↑' if szr>szr_prev else '降↓'})")
    print(f"mb_z60 ≈ {z:+.2f} | 距 07-03 峰值 {from_peak:+.1f}% 去化")
    print(f"參考: 7/28 單日 -175,998 / 7/29 -331,140(投降量級)")

    # ---- 投降 vs 續殺 判斷 ----
    print("\n--- 判斷 ---")
    flags = []
    if d1 < -250000:
        flags.append("🔴 單日失血仍達投降量級(<-25萬張)→ 續殺/投降未竟")
    elif d1 < -120000:
        flags.append("🟠 失血仍大(-12~-25萬張)→ 去槓桿進行中,尚未收斂")
    elif d1 < -40000:
        flags.append("🟡 失血縮小(-4~-12萬張)→ 賣壓鈍化中")
    elif d1 < 40000:
        flags.append("🟢 失血急縮/持平 → 融資去化告一段落跡象")
    else:
        flags.append("🟢🟢 融資回升 → 槓桿回補,去化結束")

    if short_d1 < 0:
        flags.append("🟢 融券回補(空單減)→ 底部訊號之一")
    else:
        flags.append("🔴 融券續增(空單堆積)→ 空方未退,底部未確認")

    if z < -2:
        flags.append("🟢 z60<-2 深度洗淨區")
    elif z < -1:
        flags.append("🟡 z60 轉負偏低,槓桿明顯退潮")

    for f in flags:
        print(" ", f)

    green = sum("🟢" in f for f in flags)
    red = sum("🔴" in f for f in flags)
    print("\n綜合:", "偏『多殺多收尾』" if green > red else ("偏『續殺/未竟』" if red > green else "混合訊號,需再一日確認"))
    print("(市場層平均;個股層斷頭可能仍有零星。非投資建議。)")


if __name__ == "__main__":
    main()
