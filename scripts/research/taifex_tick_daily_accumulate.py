#!/usr/bin/env python3
"""2026-08-13：使用者問「秒級/order flow資料去哪補」，查完Fubon SDK（REST/
WebSocket都沒有Level-2訂單簿深度、也沒有歷史tick回放API）跟TAIFEX（免費前30
交易日逐筆成交下載）後，這裡把TAIFEX那條路接起來。

⚠️ 誠實澄清：TAIFEX這份免費資料（Daily_YYYY_MM_DD.zip，逐筆成交檔）**沒有
買賣方向欄位**——「成交數量(B+S)」欄位名稱本身就寫明是買賣合併量，不分邊，
這不是真正的order flow imbalance資料，跟我們現在用的Fubon intraday.trades()
資訊量是同一個等級（時間/價格/量）。它解決的是另一個獨立問題：

1. Fubon REST intraday.trades()有~50筆上限的已知風險（見
   momentum_rotation_tick_marketdata.py檔頭docstring），TAIFEX這份是官方完整
   當日逐筆檔，不會漏。
2. Fubon SDK完全沒有歷史tick回放（只有daily/candles聚合K線），TAIFEX這份
   可以每天下載累積成真正的歷史逐筆資料庫，不像現有reports/research/
   expert_pool_futures_tick/*.csv那樣是研究當下手動存的固定快照。
3. TX大盤跟12檔個股期貨都在同一份檔案裡，格式一致。

免費下載只保留「前30個交易日」，過舊的抓不到——這支腳本必須**每天執行**才能
累積出長期歷史，抓一次只能拿到當天+回補最近30天內還沒存過的日子，沒辦法
一次補回今天以前30天以上的資料（那要付費申請，見期交所資作部）。

輸出：${GOLDENSTOCKS_DATA_DIR}/cache/momentum_rotation/taifex_tick_daily/
  {sid}_{root}.csv （單一標的、逐日append、去重），欄位比照現有
  expert_pool_futures_tick/*.csv慣例：date,futures_id,contract_date,price,volume
  ——這樣未來累積夠天數後，可以直接讓load_day_bars_with_times/load_window讀取，
  不用另外寫新的loader。

安全設計：純讀取公開資料（HTTP GET），不寫入任何order/live相關檔案，失敗
（網路/期交所改格式）不拋例外中斷整支流程，單一symbol/單一天失敗只跳過。

執行：PYTHONPATH=src .venv/bin/python scripts/research/taifex_tick_daily_accumulate.py
     [--days N]（預設回補最近5個交易日，含今天；--days 30 可以把免費視窗全部掃過一次）
"""

from __future__ import annotations

import csv
import io
import os
import sys
import urllib.request
import zipfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, "src")

from order.momentum_rotation_signal import FUTURES_ROOT  # noqa: E402

_DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or os.path.expanduser("~/goldenstocks-data"))
ARCHIVE_DIR = _DATA_DIR / "cache" / "momentum_rotation" / "taifex_tick_daily"

# TAIFEX商品代號(不含空白padding) -> sid；額外把TX(大盤台指期貨)也一起收
PRODUCT_TO_SID: dict[str, str] = {f"{root}F": sid for sid, root in FUTURES_ROOT.items()}
PRODUCT_TO_SID["TX"] = "TX"
ROOT_BY_SID: dict[str, str] = {**{sid: f"{root}F" for sid, root in FUTURES_ROOT.items()}, "TX": "TX"}

_UA = "Mozilla/5.0 (goldenstocks research; read-only public data fetch)"


def _download_daily_zip(d: date) -> bytes | None:
    url = f"https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{d:%Y}_{d:%m}_{d:%d}.zip"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
    except Exception:  # noqa: BLE001 -- network/holiday/no-file, caller skips this day
        return None
    if len(data) < 1000:  # 期交所非交易日回傳的錯誤頁很小，不是真的zip
        return None
    return data


def _parse_daily_zip(zip_bytes: bytes) -> dict[str, list[tuple[str, str, str, str, str]]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = zf.namelist()[0]
        raw = zf.read(name).decode("big5", errors="ignore")
    out: dict[str, list[tuple[str, str, str, str, str]]] = {sid: [] for sid in PRODUCT_TO_SID.values()}
    reader = csv.reader(io.StringIO(raw))
    next(reader, None)  # header: 成交日期,商品代號,到期月份(週別),成交時間,成交價格,成交數量(B+S),...
    for row in reader:
        if len(row) < 6:
            continue
        trade_date, product_code, contract_month, trade_time, price, volume = (
            row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip(), row[4].strip(), row[5].strip(),
        )
        sid = PRODUCT_TO_SID.get(product_code)
        if sid is None or not price or price in ("-", ""):
            continue
        # 2026-08-14發現（見taifex_broad_tick_daily_accumulate.py同一處說明）：
        # 同一個商品代號底下混著outright跟calendar spread(到期月份欄位用"/"
        # 分隔兩個月份)兩種成交，價差合約的價格常近0甚至負值，會污染價格
        # 序列，只收outright列。
        if "/" in contract_month:
            continue
        try:
            if float(price) <= 0:
                continue
        except ValueError:
            continue
        out[sid].append((trade_date, trade_time, contract_month, price, volume))
    return out


def _to_csv_rows(sid: str, rows: list[tuple[str, str, str, str, str]]) -> list[dict[str, str]]:
    root = ROOT_BY_SID[sid]
    out = []
    for trade_date, trade_time, contract_month, price, volume in rows:
        # trade_date=YYYYMMDD, trade_time=HHMMSS -> "YYYY-MM-DD HH:MM:SS"（比照
        # 現有expert_pool_futures_tick/*.csv的date欄位格式）
        if len(trade_date) != 8 or len(trade_time) != 6:
            continue
        dt_str = (
            f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} "
            f"{trade_time[:2]}:{trade_time[2:4]}:{trade_time[4:6]}"
        )
        out.append({
            "date": dt_str, "futures_id": root, "contract_date": contract_month,
            "price": price, "volume": volume,
        })
    return out


def _archive_path(sid: str) -> Path:
    root = ROOT_BY_SID[sid]
    return ARCHIVE_DIR / f"{sid}_{root}.csv"


def _load_existing_dates(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    dates: set[str] = set()
    with path.open() as f:
        for row in csv.DictReader(f):
            d = row.get("date", "")
            if d:
                dates.add(d[:10])
    return dates


def _append_rows(sid: str, new_rows: list[dict[str, str]]) -> int:
    if not new_rows:
        return 0
    path = _archive_path(sid)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = path.is_file()
    with path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "futures_id", "contract_date", "price", "volume"])
        if not file_exists:
            writer.writeheader()
        writer.writerows(new_rows)
    return len(new_rows)


def accumulate_day(d: date) -> dict[str, int]:
    zip_bytes = _download_daily_zip(d)
    if zip_bytes is None:
        return {}
    by_sid = _parse_daily_zip(zip_bytes)
    counts: dict[str, int] = {}
    for sid, rows in by_sid.items():
        existing_dates = _load_existing_dates(_archive_path(sid))
        d_str = d.isoformat()
        if d_str in existing_dates:
            continue  # 已經存過，避免重複append
        csv_rows = _to_csv_rows(sid, rows)
        n = _append_rows(sid, csv_rows)
        if n:
            counts[sid] = n
    return counts


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="TAIFEX前30日逐筆成交每日累積")
    ap.add_argument("--days", type=int, default=5, help="回補最近幾個日曆日（含週末假日會自動略過空檔），預設5")
    args = ap.parse_args(argv)

    today = date.today()
    print(f"回補最近{args.days}個日曆日的TAIFEX逐筆成交...")
    total_by_sid: dict[str, int] = {}
    for i in range(args.days):
        d = today - timedelta(days=i)
        counts = accumulate_day(d)
        if counts:
            print(f"  {d.isoformat()}: {counts}")
            for sid, n in counts.items():
                total_by_sid[sid] = total_by_sid.get(sid, 0) + n
        else:
            print(f"  {d.isoformat()}: 無新資料（非交易日/已存過/期交所無檔）")

    print("\n累積結果（各標的目前總筆數）：")
    for sid in sorted(PRODUCT_TO_SID.values(), key=lambda x: (x == "TX", x)):
        path = _archive_path(sid)
        n_lines = sum(1 for _ in path.open()) - 1 if path.is_file() else 0
        print(f"  {sid}({ROOT_BY_SID[sid]}): {n_lines}筆（存於 {path}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
