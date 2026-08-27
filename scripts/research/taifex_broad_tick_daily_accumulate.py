#!/usr/bin/env python3
"""2026-08-13：使用者要求擴大蒐集——micro VCP（量縮盤整+趨勢+爆量）今天在
12檔momentum-rotation個股期貨上驗證，一路把coil/趨勢條件疊起來就撞上樣本量
瓶頸（4折裡常有3折崩潰到個位數~十幾筆，risk-adj變成不可信的-inf）。75天×
12檔的訊號密度撐不起這麼多層條件同時成立的統計檢定力，這裡照稍早給的建議
第二條路：擴大標的池，不只12檔，抓TAIFEX全部個股期貨。

跟taifex_tick_daily_accumulate.py（只認FUTURES_ROOT那12檔+TX，檔名用sid）
不同，這支不需要sid對照表——直接把TAIFEX每日檔裡「2~3碼英數+F」這個個股
期貨的商品代號pattern（今天實測：這個pattern涵蓋825K行全交易所資料裡的
601K行，跟現有12檔已知的RAF/FFF/...等代號完全吻合這個命名慣例）全部收下來，
每個商品代號各自存一個CSV，檔名就是商品代號本身。

⚠️ 同樣誠實提醒：這份資料一樣沒有買賣方向欄位（見
taifex_tick_daily_accumulate.py檔頭docstring同一段說明），不是真正的order
flow imbalance資料，純粹是「完整、不被Fubon 50筆上限沖掉的逐筆成交」，這裡
只是把涵蓋標的從12檔擴大到全市場個股期貨。

規模提醒：單日全部個股期貨約60萬筆，累積數月後單一天的zip解析會變成
主要瓶頸（每天仍是重新下載當天+trail_days內的zip、只解析一次，不是重複
解析歷史），磁碟空間預估：60萬行/天 × 40 bytes/行 ≈ 24MB/天，一年約6GB，
在可接受範圍。

免費視窗一樣只保留「前30個交易日」，隔天不跑會永久漏掉當天。

輸出：${GOLDENSTOCKS_DATA_DIR}/cache/momentum_rotation/taifex_tick_daily_broad/
  {product_code}.csv，欄位跟taifex_tick_daily_accumulate.py一致：
  date,futures_id,contract_date,price,volume（futures_id=product_code本身）

執行：PYTHONPATH=src .venv/bin/python scripts/research/taifex_broad_tick_daily_accumulate.py
     [--days N]（預設5，回補近5個日曆日）
"""

from __future__ import annotations

import csv
import io
import os
import re
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, "scripts/research")

from taifex_tick_daily_accumulate import _download_daily_zip  # noqa: E402

_DATA_DIR = Path(os.environ.get("GOLDENSTOCKS_DATA_DIR") or os.path.expanduser("~/goldenstocks-data"))
ARCHIVE_DIR = _DATA_DIR / "cache" / "momentum_rotation" / "taifex_tick_daily_broad"

# 個股期貨商品代號pattern：2~3碼英數字尾巴是F（2026-08-13實測涵蓋率73%行數，
# 跟既有12檔FUTURES_ROOT命名慣例(root+F)完全吻合）
STOCK_FUTURES_CODE_RE = re.compile(r"^[A-Z][A-Z0-9]F$")


def _parse_daily_zip_broad(zip_bytes: bytes) -> dict[str, list[tuple[str, str, str, str, str]]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        name = zf.namelist()[0]
        raw = zf.read(name).decode("big5", errors="ignore")
    out: dict[str, list[tuple[str, str, str, str, str]]] = {}
    reader = csv.reader(io.StringIO(raw))
    next(reader, None)  # header: 成交日期,商品代號,到期月份(週別),成交時間,成交價格,成交數量(B+S),...
    for row in reader:
        if len(row) < 6:
            continue
        trade_date, product_code, contract_month, trade_time, price, volume = (
            row[0].strip(), row[1].strip(), row[2].strip(), row[3].strip(), row[4].strip(), row[5].strip(),
        )
        if not STOCK_FUTURES_CODE_RE.match(product_code):
            continue
        if not price or price in ("-", ""):
            continue
        # 2026-08-14發現：同一個商品代號底下混著outright(單一到期月)跟
        # calendar spread(跨月價差組合，到期月份欄位用"/"分隔兩個月份，如
        # "202608/202609")兩種成交類型——價差合約的"價格"是月間價差，常常
        # 接近0甚至負值，混進個股期貨價格序列會嚴重污染下游統計(甚至讓
        # fill=0觸發除以0)。只收outright列，在累積源頭就濾掉，不留給下游
        # 每個consumer各自過濾。
        if "/" in contract_month:
            continue
        try:
            if float(price) <= 0:
                continue
        except ValueError:
            continue
        out.setdefault(product_code, []).append((trade_date, trade_time, contract_month, price, volume))
    return out


def _to_csv_rows(product_code: str, rows: list[tuple[str, str, str, str, str]]) -> list[dict[str, str]]:
    out = []
    for trade_date, trade_time, contract_month, price, volume in rows:
        if len(trade_date) != 8 or len(trade_time) != 6:
            continue
        dt_str = (
            f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} "
            f"{trade_time[:2]}:{trade_time[2:4]}:{trade_time[4:6]}"
        )
        out.append({
            "date": dt_str, "futures_id": product_code, "contract_date": contract_month,
            "price": price, "volume": volume,
        })
    return out


def _archive_path(product_code: str) -> Path:
    return ARCHIVE_DIR / f"{product_code}.csv"


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


def _append_rows(product_code: str, new_rows: list[dict[str, str]]) -> int:
    if not new_rows:
        return 0
    path = _archive_path(product_code)
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
    by_code = _parse_daily_zip_broad(zip_bytes)
    counts: dict[str, int] = {}
    d_str = d.isoformat()
    for product_code, rows in by_code.items():
        existing_dates = _load_existing_dates(_archive_path(product_code))
        if d_str in existing_dates:
            continue
        csv_rows = _to_csv_rows(product_code, rows)
        n = _append_rows(product_code, csv_rows)
        if n:
            counts[product_code] = n
    return counts


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="TAIFEX全市場個股期貨逐筆成交每日累積")
    ap.add_argument("--days", type=int, default=5, help="回補最近幾個日曆日，預設5")
    args = ap.parse_args(argv)

    today = date.today()
    print(f"回補最近{args.days}個日曆日的TAIFEX全市場個股期貨逐筆成交...")
    total_by_code: dict[str, int] = {}
    n_products_seen: set[str] = set()
    for i in range(args.days):
        d = today - timedelta(days=i)
        counts = accumulate_day(d)
        if counts:
            n_products_seen.update(counts.keys())
            total_rows = sum(counts.values())
            print(f"  {d.isoformat()}: {len(counts)}檔個股期貨、共{total_rows}筆")
            for code, n in counts.items():
                total_by_code[code] = total_by_code.get(code, 0) + n
        else:
            print(f"  {d.isoformat()}: 無新資料（非交易日/已存過/期交所無檔）")

    print(f"\n累積結果：{len(list(ARCHIVE_DIR.glob('*.csv'))) if ARCHIVE_DIR.is_dir() else 0}檔個股期貨已有存檔")
    print(f"本次新增涵蓋{len(n_products_seen)}檔，共{sum(total_by_code.values())}筆新資料")
    top10 = sorted(total_by_code.items(), key=lambda kv: -kv[1])[:10]
    if top10:
        print("本次筆數最多的10檔：")
        for code, n in top10:
            print(f"  {code}: {n}筆")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
