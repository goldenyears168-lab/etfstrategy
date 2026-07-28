#!/usr/bin/env python3
"""一次性寄出「個股專家池 44 檔體檢」四層摘要到使用者信箱（送給自己）。

讀 RETEST_ADOPTED44_new_data.csv，分四層(保留/觀察/搁置/退池)，用 notify_email.send_alert 寄出。
收件人 = GMAIL_NOTIFY_TO / GMAIL_USER（使用者本人）。需 email 環境變數已載入。

用法(mini,已 source order.env)：
  RUN_ALERT_EMAIL=1 PYTHONPATH=src .venv/bin/python scripts/research/email_adopted44_retest_digest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from notify_email import send_alert  # noqa: E402

CSV = ROOT / "reports/research/branch-footprint-screen/expert_pool/RETEST_ADOPTED44_new_data.csv"


def bucket(verdict: str, n) -> str:
    v = str(verdict); n = int(n) if pd.notna(n) else 0
    if v.startswith("HOLD"):
        return "keep" if n >= 12 else "watch"
    if v.startswith("THIN"):
        return "shelf"
    if v.startswith("WEAKEN") or v.startswith("FAIL"):
        return "retire"
    return "other"


def main() -> int:
    df = pd.read_csv(CSV, dtype={"sid": str})
    df["bucket"] = [bucket(v, n) for v, n in zip(df["verdict"], df["new_n"])]

    def fmt(sid, name, r):
        nm = "" if pd.isna(r["new_med"]) else f"{r['new_med']:+.1f}%"
        return f"{sid} {name}(體檢{nm}·n={int(r['new_n']) if pd.notna(r['new_n']) else 0})"

    order = [("keep", "① 保留·扎實(med≥2% 且 n≥12)"),
             ("retire", "④ 退池·轉弱或翻負"),
             ("watch", "② 觀察·薄樣本勿加碼"),
             ("shelf", "③ 搁置·太薄無法判(n<6)")]
    lines = ["個股專家池 44 檔 · 用 831 家新數據 + adj_close 體檢(2026-07-28, data_max 07-27)", ""]
    counts = df["bucket"].value_counts().to_dict()
    lines.append(f"分佈：保留 {counts.get('keep',0)} · 退池 {counts.get('retire',0)} · 觀察 {counts.get('watch',0)} · 搁置 {counts.get('shelf',0)}（合計 {len(df)}）")
    lines.append("")
    for key, title in order:
        sub = df[df["bucket"] == key].sort_values("new_med", ascending=False, na_position="last")
        lines.append(f"{title} — {len(sub)} 檔")
        for _, r in sub.iterrows():
            lines.append("  · " + fmt(r["sid"], r["name"], r))
        lines.append("")
    lines.append("結論：真正經得起新數據約 14 檔(含 3 live 2344/8046/8358)；退 8 檔；其餘 22 檔灰色地帶。")
    lines.append("樣本一多，勉強過 +2% 的邊際案例現形，同 980T 病。名單縮水不增長。")
    body = "\n".join(lines)
    send_alert("[個股專家池] 44檔體檢 · 831家新數據 · 保留14/退8/觀察15/搁置7", body)
    print("[SENT] digest email · subject=[個股專家池] 44檔體檢")
    print(body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
