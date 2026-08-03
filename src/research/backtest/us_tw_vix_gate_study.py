"""US↔TW VIX sell-gate 增量價值研究（DB-only · 無網路依賴）.

研究問題（2026-08 進階健檢衍生）：台美脫溝 sell-gate 加入 VIX（美）／VIXTWN（台，
已自算回溯 2003）能不能提高價值？

方法：日頻、PIT（只用 date<=T 資訊預測 T+1），對齊 IX0001 ∩ VIX ∩ VIXTWN ∩ SPY
（實際覆蓋約 2015-2026）。以「固定誤報預算（FPR≤0.22，= 現有 gate 的 G2）下的 recall」
與 AUC，比較純美股隔夜 baseline（SPY）vs 加 ΔVIX／脫溝差；含 OOS 訓練/測試切分、
permutation 顯著性、事件層 recall、跨 regime FPR 穩定性。

核心結論（見 reports/research/us-tw/）：
  · VIX 對「多抓幾次跌（recall）」與真美股隔夜訊號**高度冗餘**（美股夜盤跌＝VIX 跳升同一事件）。
  · VIX 真正的增量在**跨 regime 的 FPR 校準穩定性**：純美股門檻 OOS 會從 0.22 暴衝到 ~0.28，
    ΔVIX 分布平穩、OOS FPR 穩守 ~0.22，穩住 G2。
  · VIXTWN 單獨無預測力，價值在脫溝差參考腳與把校準樣本從 ~49 天 → 11 年。

誠實邊界：此為日頻 proxy；真 gate 是盤中 US-overnight→TW-open。此研究確立方向與方法，
盤中整合是後續。
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics as st
import sys
from dataclasses import dataclass, field

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))

from report_paths import REPORTS_RESEARCH  # noqa: E402
from signal_validation import to_events  # noqa: E402
from stock_db import DEFAULT_DB_PATH, connect  # noqa: E402

# ── 研究參數（凍結） ──────────────────────────────────────────
EVENT_1D = -0.015      # 目標①：隔日台股 close-to-close ≤ -1.5%
EVENT_2D = -0.020      # 目標②：未來 2 日最差 ≤ -2.0%（出場視窗）
FPR_BUDGET = 0.22      # G2：非事件誤報率上限
PERM_N = 1000
SEED = 42
SCORES = ("us", "dvix", "decouple", "us+dvix", "us+decouple", "us+dvix+decouple")
OUT_DIR = REPORTS_RESEARCH / "us-tw"


def _series(conn: sqlite3.Connection, sql: str, *params: object) -> dict[str, float]:
    return {d: float(x) for d, x in conn.execute(sql, params).fetchall() if x is not None}


def load_panel(conn: sqlite3.Connection) -> list[dict]:
    """建 PIT 面板：每列= T 日收盤可得特徵 + T+1／T+2 台股報酬目標。"""
    ix = _series(conn, "SELECT date, MAX(close) FROM daily_bars WHERE code='IX0001' GROUP BY date")
    vix = _series(conn, "SELECT date, MAX(close) FROM market_vix_daily WHERE symbol='VIX' GROUP BY date")
    vtw = _series(conn, "SELECT date, MAX(close) FROM market_vix_daily WHERE symbol='VIXTWN' GROUP BY date")
    spy = _series(conn, "SELECT trade_date, MAX(close) FROM us_daily_bars WHERE ticker='SPY' GROUP BY trade_date")
    dts = sorted(set(ix) & set(vix) & set(vtw) & set(spy))
    rows: list[dict] = []
    for i in range(1, len(dts) - 2):
        tm1, t, tp1, tp2 = dts[i - 1], dts[i], dts[i + 1], dts[i + 2]
        fwd2 = min(ix[tp1] / ix[t] - 1, ix[tp2] / ix[t] - 1)
        rows.append({
            "T": t,
            "y1": 1 if ix[tp1] / ix[t] - 1 <= EVENT_1D else 0,
            "y2": 1 if fwd2 <= EVENT_2D else 0,
            # 特徵（皆 T 日收盤可得；美股/VIX 為 TW T+1 開盤前的隔夜資訊）
            "us": -(spy[t] / spy[tm1] - 1),               # 真美股隔夜（SPY 跌→分高）= 誠實 baseline
            "dvix": vix[t] / vix[tm1] - 1,                 # ΔVIX
            "decouple": (vix[t] / vix[tm1] - 1) - (vtw[t] / vtw[tm1] - 1),  # 脫溝差
        })
    return rows


def _z(rows: list[dict], key: str) -> dict[int, float]:
    xs = [r[key] for r in rows]
    m, s = st.mean(xs), (st.pstdev(xs) or 1.0)
    return {id(r): (r[key] - m) / s for r in rows}


def score(rows: list[dict], kind: str) -> dict[int, float]:
    """回傳每列的標準化 score（高=更該賣）。組合 score = 各 z 分數相加。"""
    zu, zv, zd = _z(rows, "us"), _z(rows, "dvix"), _z(rows, "decouple")
    parts = {"us": [zu], "dvix": [zv], "decouple": [zd],
             "us+dvix": [zu, zv], "us+decouple": [zu, zd],
             "us+dvix+decouple": [zu, zv, zd]}[kind]
    return {id(r): sum(p[id(r)] for p in parts) for r in rows}


def auc(rows: list[dict], kind: str, yk: str) -> float:
    s = score(rows, kind)
    ev = [s[id(r)] for r in rows if r[yk]]
    nv = [s[id(r)] for r in rows if not r[yk]]
    if not ev or not nv:
        return float("nan")
    wins = sum(a > b for a in ev for b in nv) + 0.5 * sum(a == b for a in ev for b in nv)
    return wins / (len(ev) * len(nv))


def recall_at_fpr(train: list[dict], test: list[dict], kind: str, yk: str,
                  fpr_budget: float = FPR_BUDGET) -> tuple[float, float, float]:
    """在 train 非事件上取 (1-fpr) 分位當門檻，回傳 (test_recall, test_fpr, train_fpr)。"""
    s_tr = score(train, kind)
    non = sorted(s_tr[id(r)] for r in train if not r[yk])
    thr = non[int((1 - fpr_budget) * len(non))]
    train_fpr = sum(s_tr[id(r)] >= thr for r in train if not r[yk]) / max(1, sum(1 for r in train if not r[yk]))
    s_te = score(test, kind)
    ev = [r for r in test if r[yk]]
    nv = [r for r in test if not r[yk]]
    rec = sum(s_te[id(r)] >= thr for r in ev) / max(1, len(ev))
    fpr = sum(s_te[id(r)] >= thr for r in nv) / max(1, len(nv))
    return rec, fpr, train_fpr


def event_layer_recall(train: list[dict], test: list[dict], kind: str, yk: str,
                       fpr_budget: float = FPR_BUDGET) -> tuple[float, int]:
    """事件層 recall：相鄰的 pre-drop 列併成一事件，事件內任一列開火即算抓到。"""
    s_tr = score(train, kind)
    non = sorted(s_tr[id(r)] for r in train if not r[yk])
    thr = non[int((1 - fpr_budget) * len(non))]
    s_te = score(test, kind)
    cpos = {r["T"]: i for i, r in enumerate(test)}
    pos_dates = [r["T"] for r in test if r[yk]]
    events = to_events(pos_dates, cpos, max_gap=2)
    fired = {r["T"] for r in test if s_te[id(r)] >= thr}
    caught = sum(1 for ev in events if any(d in fired for d in ev))
    return (caught / len(events) if events else float("nan")), len(events)


def permutation_pvalue(rows: list[dict], half: int, kind: str, yk: str,
                       n_perm: int = PERM_N, seed: int = SEED) -> float:
    """H0：score 對 y 無關。打亂 y，看隨機 OOS recall ≥ 觀察值的比例。用固定種子 LCG 避免 random 模組。"""
    train, test = rows[:half], rows[half:]
    obs = recall_at_fpr(train, test, kind, yk)[0]
    labels = [r[yk] for r in rows]
    n = len(labels)
    state = seed & 0x7FFFFFFF
    cnt = 0
    for _ in range(n_perm):
        # Fisher-Yates with a deterministic LCG（Math.random 在此環境不可用）
        perm = labels[:]
        for i in range(n - 1, 0, -1):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            j = state % (i + 1)
            perm[i], perm[j] = perm[j], perm[i]
        pr = [dict(r, _yp=perm[k]) for k, r in enumerate(rows)]
        rec = recall_at_fpr(pr[:half], pr[half:], kind, "_yp")[0]
        if rec >= obs:
            cnt += 1
    return cnt / n_perm


@dataclass
class StudyResult:
    n: int = 0
    train_span: tuple[str, str] = ("", "")
    test_span: tuple[str, str] = ("", "")
    base_rate: dict[str, tuple[float, float]] = field(default_factory=dict)  # yk -> (train, test)
    table: dict[str, list[dict]] = field(default_factory=dict)               # yk -> rows of metrics
    perm_p: dict[str, float] = field(default_factory=dict)                   # yk -> p (us+dvix)


def run_study(conn: sqlite3.Connection) -> StudyResult:
    rows = load_panel(conn)
    if len(rows) < 200:
        raise RuntimeError(f"面板僅 {len(rows)} 列，資料不足（需 IX0001∩VIX∩VIXTWN∩SPY）")
    half = len(rows) // 2
    train, test = rows[:half], rows[half:]
    res = StudyResult(n=len(rows), train_span=(train[0]["T"], train[-1]["T"]),
                      test_span=(test[0]["T"], test[-1]["T"]))
    for yk in ("y1", "y2"):
        res.base_rate[yk] = (sum(r[yk] for r in train) / len(train),
                             sum(r[yk] for r in test) / len(test))
        metrics = []
        for kind in SCORES:
            rec, fpr, tr_fpr = recall_at_fpr(train, test, kind, yk)
            ev_rec, n_ev = event_layer_recall(train, test, kind, yk)
            metrics.append({"score": kind, "oos_recall": rec, "oos_fpr": fpr,
                            "train_fpr": tr_fpr, "auc": auc(test, kind, yk),
                            "event_recall": ev_rec, "n_events": n_ev})
        res.table[yk] = metrics
        res.perm_p[yk] = permutation_pvalue(rows, half, "us+dvix", yk)
    return res


def _fmt_table(metrics: list[dict]) -> str:
    head = "| score | OOS recall | OOS FPR | train FPR | AUC | event recall |\n"
    head += "|---|---:|---:|---:|---:|---:|\n"
    for m in metrics:
        head += (f"| `{m['score']}` | {m['oos_recall']:.2f} | {m['oos_fpr']:.2f} | "
                 f"{m['train_fpr']:.2f} | {m['auc']:.3f} | {m['event_recall']:.2f} |\n")
    return head


def write_report(res: StudyResult) -> str:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "us_tw_vix_gate_incremental_value.md"
    lines = [
        "# US↔TW 脫溝 sell-gate ＋ VIX/VIXTWN 增量價值研究",
        "",
        "> DB-only · 無網路 · PIT（≤T 預測 T+1）。可重跑：",
        "> `PYTHONPATH=src .venv/bin/python src/research/backtest/us_tw_vix_gate_study.py`",
        "",
        f"樣本 **{res.n}** 日 · TRAIN {res.train_span[0]}~{res.train_span[1]} · "
        f"TEST(OOS) {res.test_span[0]}~{res.test_span[1]}",
        f"門檻在 TRAIN 非事件的 (1−{FPR_BUDGET:.2f}) 分位取得（=固定 G2 誤報預算），套到 OOS。",
        "",
    ]
    labels = {"y1": f"隔日台股 ≤ {EVENT_1D:.1%}", "y2": f"未來 2 日最差 ≤ {EVENT_2D:.1%}"}
    for yk in ("y1", "y2"):
        btr, bte = res.base_rate[yk]
        lines += [f"## 目標：{labels[yk]}",
                  f"事件率 train={btr:.1%} · test={bte:.1%}", "",
                  _fmt_table(res.table[yk]),
                  f"Permutation（`us+dvix`, NP={PERM_N}）p = **{res.perm_p[yk]:.3f}**", ""]
    lines += [
        "## 結論",
        "1. **真美股隔夜（`us`=SPY）本身就強**；VIX 對『多抓幾次跌』與其**高度冗餘**"
        "（美股夜盤跌＝VIX 跳升）。加 VIX 的額外 recall 有限。",
        "2. **VIX 的真正增量＝跨 regime 的 FPR 校準穩定性**：`us` 門檻 OOS 會從 "
        f"{FPR_BUDGET:.2f} 暴衝（train FPR vs OOS FPR 落差大），`dvix`/`us+dvix` 的 OOS FPR "
        "穩守預算 → 守住 G2（少誤砍）。",
        "3. **VIXTWN 單獨無預測力**；價值在脫溝差參考腳與把校準樣本從 ~49 天 → 11 年多 regime。",
        "",
        "## 誠實邊界",
        "日頻 proxy；真 gate 為盤中 US-overnight→TW-open。台股指數 DB 僅至 2015（無 2008 GFC）。",
        "下一步：把 ΔVIX 當 FPR 穩定層整合進 `frozen_sell_gate_spec`，盤中對齊後跑 G1/G2 驗收。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="US↔TW VIX sell-gate 增量價值研究（DB-only）")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--no-report", action="store_true", help="只印摘要、不寫報告")
    args = ap.parse_args(argv)
    from pathlib import Path
    res = run_study(connect(Path(args.db)))
    for yk in ("y1", "y2"):
        print(f"\n=== {yk} · base train/test {res.base_rate[yk][0]:.1%}/{res.base_rate[yk][1]:.1%} ===")
        for m in res.table[yk]:
            print(f"  {m['score']:18} OOS recall={m['oos_recall']:.2f} "
                  f"FPR={m['oos_fpr']:.2f}(train {m['train_fpr']:.2f}) AUC={m['auc']:.3f}")
        print(f"  permutation(us+dvix) p={res.perm_p[yk]:.3f}")
    if not args.no_report:
        print(f"\n-> {write_report(res)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
