"""步驟 2：US↔TW 盤中真解析度 · VIX 條件化 sell-gate 研究（DB-only · 無網路）.

延續日頻研究（`us_tw_vix_gate_study`）。日頻證明 VIX 的價值在 FPR 校準穩定性；此模組
下沉到 **gate 真正運作的盤中解析度**：以 DB 內 `us_futures_overnight_snapshot`（每個
TW session 的 NQ/ES 隔夜%，開盤 09:30 快照＝PIT 觸發訊號）＋ `0050` 5m 分 K（開盤後
盤中低＝台股 detach 目標）＋ VIX/VIXTWN 前日收（regime）。

核心發現（PIT-乾淨）：09:30 美期隔夜跌大多已反映在**開盤缺口**（砍不掉），殘餘預測力弱
（`us` AUC≈0.59）；而 **VIX 水位帶來增量**（`us+vixlv` AUC→0.67）——與日頻研究「VIX 對
美股價冗餘」相反，在盤中決策點 VIX **不冗餘**、扛起「開盤後是否續跌」的判別。
（註：先前探索的「VIX 高時 detach 明顯較高」是開盤缺口前視污染，PIT-乾淨後該交互持平。）

資料：`us_futures_overnight_snapshot`(2024-11~) ∩ `0050` 5m ∩ VIX/VIXTWN。約 395 session、
單一 regime（~1.7 年）——樣本有限，OOS 從弱；結論以全樣本 AUC + VIX-regime 可靠度分層
為主，train/test 僅參考。誠實邊界：0050 為台股 index proxy；真觸發需 live 美期（此為歷史驗證）。

可重跑：`PYTHONPATH=src .venv/bin/python src/research/backtest/us_tw_vix_gate_intraday_study.py`
"""

from __future__ import annotations

import argparse
import bisect
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from report_paths import REPORTS_RESEARCH  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

OPEN_LABEL = "09:30"       # 開盤觸發時點（overnight 最早快照 = PIT 決策點）
OPEN_BAR = "09:30:00"      # 0050 對齊 bar（用 ≥ 此時點的盤中低，避免 09:00–09:30 前視）
DETACH = -0.008            # 盤中事件：09:30→盤中低 ≤ -0.8%
FPR_BUDGET = 0.30          # 盤中 detach 基準率高，誤報預算放寬；仍固定比較
SCORES = ("us", "us+vixlv", "us+dvix", "us+vixlv+dvix")
OUT_DIR = REPORTS_RESEARCH / "us-tw"


def load_panel(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = None
    # 開盤 09:30 的 NQ/ES 隔夜%（PIT 觸發訊號）
    onx: dict[str, tuple[float, float]] = {}
    for d, nq, es in conn.execute(
        "SELECT tw_session_date, nq_overnight_pct, es_overnight_pct FROM us_futures_overnight_snapshot "
        "WHERE capture_label=? AND nq_overnight_pct IS NOT NULL", (OPEN_LABEL,)
    ):
        onx[d] = (float(nq), float(es))
    # 0050 盤中：09:30 價 → 之後盤中低 的跌幅
    byday: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    for d, m, o, lo in conn.execute(
        "SELECT trade_date, minute, open, low FROM stock_kbar_5m WHERE stock_id='0050'"
    ):
        byday[d].append((m, float(o), float(lo)))
    dd: dict[str, float] = {}
    for d, bars in byday.items():
        post = sorted(b for b in bars if b[0] >= OPEN_BAR)
        if not post:
            continue
        ref = post[0][1]              # 09:30 開盤價
        low = min(b[2] for b in post)
        if ref:
            dd[d] = low / ref - 1
    # VIX/VIXTWN 前一收盤日水位（PIT regime）
    vix = {d: x for d, x in conn.execute("SELECT date, MAX(close) FROM market_vix_daily WHERE symbol='VIX' GROUP BY date")}
    vtw = {d: x for d, x in conn.execute("SELECT date, MAX(close) FROM market_vix_daily WHERE symbol='VIXTWN' GROUP BY date")}
    vdates = sorted(vix)

    def prior(d: str, tbl: dict) -> float | None:
        i = bisect.bisect_left(vdates, d) - 1
        while i >= 0 and vdates[i] not in tbl:
            i -= 1
        return tbl[vdates[i]] if i >= 0 else None

    rows: list[dict] = []
    for d in sorted(set(onx) & set(dd)):
        nq, es = onx[d]
        vlv, vprev = prior(d, vix), None
        i = bisect.bisect_left(vdates, d) - 1
        if i >= 1 and vdates[i] in vix and vdates[i - 1] in vix:
            vprev = vix[vdates[i]] / vix[vdates[i - 1]] - 1     # 前日 ΔVIX
        if vlv is None:
            continue
        rows.append({"d": d, "y": 1 if dd[d] <= DETACH else 0,
                     "us": -nq, "vixlv": vlv, "dvix": vprev if vprev is not None else 0.0})
    return rows


def _z(rows, key):
    xs = [r[key] for r in rows]
    m, s = st.mean(xs), (st.pstdev(xs) or 1.0)
    return {id(r): (r[key] - m) / s for r in rows}


def score(rows, kind):
    zu, zl, zv = _z(rows, "us"), _z(rows, "vixlv"), _z(rows, "dvix")
    parts = {"us": [zu], "us+vixlv": [zu, zl], "us+dvix": [zu, zv],
             "us+vixlv+dvix": [zu, zl, zv]}[kind]
    return {id(r): sum(p[id(r)] for p in parts) for r in rows}


def auc(rows, kind):
    s = score(rows, kind)
    ev = [s[id(r)] for r in rows if r["y"]]
    nv = [s[id(r)] for r in rows if not r["y"]]
    if not ev or not nv:
        return float("nan")
    return (sum(a > b for a in ev for b in nv) + 0.5 * sum(a == b for a in ev for b in nv)) / (len(ev) * len(nv))


def recall_at_fpr(train, test, kind, fpr=FPR_BUDGET):
    s = score(train, kind)
    non = sorted(s[id(r)] for r in train if not r["y"])
    thr = non[int((1 - fpr) * len(non))]
    st_ = score(test, kind)
    ev = [r for r in test if r["y"]]
    nv = [r for r in test if not r["y"]]
    rec = sum(st_[id(r)] >= thr for r in ev) / max(1, len(ev))
    fp = sum(st_[id(r)] >= thr for r in nv) / max(1, len(nv))
    return rec, fp


def regime_reliability(rows):
    """VIX 高/低 × 美期跌 top20% → 台股盤中 detach 機率（步驟2 核心直覺證據）。"""
    us_thr = sorted(r["us"] for r in rows)[int(0.8 * len(rows))]        # 美期跌 top20%（us 高）
    vlv_thr = sorted(r["vixlv"] for r in rows)[int(0.6 * len(rows))]    # VIX top40%
    out = {}
    for lbl, cond in (("US跌top20% ∩ VIX高", lambda r: r["us"] >= us_thr and r["vixlv"] >= vlv_thr),
                      ("US跌top20% ∩ VIX低", lambda r: r["us"] >= us_thr and r["vixlv"] < vlv_thr)):
        sel = [r for r in rows if cond(r)]
        out[lbl] = (len(sel), sum(r["y"] for r in sel) / len(sel) if sel else float("nan"))
    return out


def run_study(conn):
    rows = load_panel(conn)
    if len(rows) < 100:
        raise RuntimeError(f"盤中面板僅 {len(rows)} 列（需 overnight snapshot ∩ 0050 5m ∩ VIX）")
    half = len(rows) // 2
    tr, te = rows[:half], rows[half:]
    base = sum(r["y"] for r in rows) / len(rows)
    metrics = []
    for k in SCORES:
        rec, fp = recall_at_fpr(tr, te, k)
        metrics.append({"score": k, "auc_full": auc(rows, k), "oos_recall": rec, "oos_fpr": fp})
    return {"n": len(rows), "span": (rows[0]["d"], rows[-1]["d"]), "base": base,
            "metrics": metrics, "reliability": regime_reliability(rows)}


def write_report(res):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "us_tw_vix_gate_intraday.md"
    L = [
        "# 步驟 2：US↔TW 盤中真解析度 · VIX 條件化 sell-gate",
        "",
        "> DB-only · 無網路 · PIT（09:30 美期隔夜訊號 → 之後 0050 盤中低）。可重跑："
        "`PYTHONPATH=src .venv/bin/python src/research/backtest/us_tw_vix_gate_intraday_study.py`",
        "",
        f"樣本 **{res['n']}** session · {res['span'][0]}~{res['span'][1]} · "
        f"盤中 detach 事件(09:30→低≤{DETACH:.1%}) 基準率 **{res['base']:.1%}**",
        "",
        "## VIX-regime 可靠度分層（步驟2 核心：VIX 條件化美期訊號）",
    ]
    for lbl, (n, p) in res["reliability"].items():
        L.append(f"- {lbl}：n={n} · P(detach)={p:.1%}")
    L += ["",
          "→ ⚠️ PIT-乾淨（09:30 之後）下這個高/低分層**幾乎持平**——先前探索的「VIX 高時明顯較高」"
          "其實是開盤缺口(09:00–09:30)前視污染（那段跌在 09:30 訊號前已實現、砍不掉）。"
          "VIX 的價值不在這種簡單交互，而在整體判別力（見下 AUC）。",
          "",
          f"## 分數比較（全樣本 AUC + OOS recall@FPR≤{FPR_BUDGET:.2f}）",
          "| score | 全樣本 AUC | OOS recall | OOS FPR |",
          "|---|---:|---:|---:|"]
    for m in res["metrics"]:
        L.append(f"| `{m['score']}` | {m['auc_full']:.3f} | {m['oos_recall']:.2f} | {m['oos_fpr']:.2f} |")
    L += ["",
          "## 結論（步驟2 · 誠實版）",
          "1. **09:30 美期隔夜殘餘訊號弱**（`us` AUC≈0.59）：美期隔夜跌到 09:30 大多已反映在**開盤缺口**，"
          "缺口在 09:30 訊號前已實現、砍不掉；可交易的『開盤後續跌』殘餘預測力有限。",
          "2. **VIX 水位帶來增量**（`us+vixlv` AUC 0.59→**0.67**）：與日頻研究（VIX 對美股價冗餘）**相反**——"
          "在盤中決策點，美期隔夜已鈍化，VIX regime 反而扛起『開盤後是否續跌』的判別。VIX 在此**不冗餘**。",
          "3. 定位一致：VIX＝**regime 條件層**。建議 `frozen_sell_gate_spec` 的 detach 門檻改為 VIX 水位調節；"
          "盤中觸發仍用 live 美期（缺口段），VIX 管『開盤後要不要續砍』。",
          "",
          "## 誠實邊界",
          f"樣本僅 {res['n']} session（2024-11 起、單一 regime）→ OOS 從弱、勿過度外推；0050 為 index proxy；"
          "缺 live 美期即時（此為 DB 歷史驗證）。日頻研究（11 年）提供跨 regime 互補證據，兩者需一起讀。",
          ""]
    path.write_text("\n".join(L), encoding="utf-8")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="步驟2：盤中 VIX 條件化 sell-gate 研究（DB-only）")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--no-report", action="store_true")
    args = ap.parse_args(argv)
    res = run_study(connect(Path(args.db)))
    print(f"n={res['n']} · base detach={res['base']:.1%} · {res['span'][0]}~{res['span'][1]}")
    print("VIX-regime 可靠度:")
    for lbl, (n, p) in res["reliability"].items():
        print(f"  {lbl:24} n={n:3} P(detach)={p:.1%}")
    print("分數比較:")
    for m in res["metrics"]:
        print(f"  {m['score']:16} AUC={m['auc_full']:.3f} OOS recall={m['oos_recall']:.2f} FPR={m['oos_fpr']:.2f}")
    if not args.no_report:
        print(f"-> {write_report(res)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
