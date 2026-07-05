"""Tests for intraday_1300_digest."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT / "src"))

from intraday_1300_digest import (  # noqa: E402
    DigestInput,
    format_intraday_1300_digest,
    load_digest_input,
    parse_buy_radar,
    parse_c18acc_screen,
    parse_rrg_intraday,
    parse_sell_radar,
    parse_vcp_brief,
)

VCP_SAMPLE = """\
# VCP Pivot Gate · daily brief · 2026-06-29
> tick 覆蓋：**149 / 149** · 大盤 tick：沿用昨收
> 上一收盤 K 線 **2026-06-26** + 今日 tick 合成 provisional close
## 候選 Top 15（near pivot −8%～+5% · Pre/Breakout/Early · composite≥45）
| 代號 | 名稱 | composite | state | pivot | dist% | stop |
|------|------|-----------|-------|-------|-------|------|
| 2404 | 漢唐 | 57.1 | Pre-breakout | 1430.00 | -4.5 | 1207.80 |
| 3044 | 健鼎科技 | 54.4 | Pre-breakout | 540.00 | -7.2 | 449.46 |
"""

RRG_SAMPLE = """\
# RRG mono 收盤前預警 · 2026-06-29
- tick 覆蓋：**149 / 180** 檔 · 大盤 tick：沿用昨收
- 目前空槽：**3 / 3**
## ★ mono fresh 候選（seg_last 排序）
| # | 代號 | 名稱 | seg_last | 位移 | 當日% | RV | MV |
|---|------|------|----------|------|-------|----|----|
| 1 | 6191 | 精成科 | 0.969 | 1.59 | +4.0% | 100.3 | 101.7 |
## 所有 mono 候選（含非 fresh）
| # | 代號 | 名稱 | fr | seg_last | 位移 | 當日% |
|---|------|------|----|----------|------|-------|
| 1 | 6191 | 精成科 | ★ | 0.969 | 1.59 | +4.0% |
"""

C18_SAMPLE = """\
## fresh mono 全池（昨收 PIT · 2026-06-26）
| # | 代號 | 名稱 | fr | seg_last | 位移 |
|---|------|------|----|----------|------|
| 1 | 2404 | 漢唐 | ★ | 0.920 | 1.62 |
## 持倉（C18acc state）
| 槽 | 代號 | 名稱 | 進場 | hold |
|---|------|------|------|------|
| 1 | 2404 | 漢唐 | 2026-06-29 | 2026-06-29 |
"""

BUY_SAMPLE = """\
## Buy signals（C0 entry）
- **2404** 漢唐 · **buy** · ref **1415.00** · 來源 **RRG fresh mono + RRG mono tier2** · 多軌重疊
"""

SELL_SAMPLE = """\
## Sell signals（extension · universe）
- **2344** 華邦電子 · **sell** · ref **204.50** · **watch_s0b** · S0b watch
"""


class Intraday1300DigestTests(unittest.TestCase):
    def test_parse_vcp_brief(self) -> None:
        rows, tick, last_close = parse_vcp_brief(VCP_SAMPLE)
        self.assertEqual(tick, "149/149")
        self.assertEqual(last_close, "2026-06-26")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].stock_id, "2404")

    def test_parse_rrg_intraday(self) -> None:
        fresh, all_rows, tick, slots = parse_rrg_intraday(RRG_SAMPLE)
        self.assertEqual(tick, "149/180")
        self.assertEqual(slots, "3/3")
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0].stock_id, "6191")

    def test_format_digest_overlap_and_sections(self) -> None:
        inp = DigestInput(session_date="2026-06-29")
        inp.vcp, inp.vcp_tick, inp.last_close_date = parse_vcp_brief(VCP_SAMPLE)
        inp.rrg_fresh, inp.rrg_all, inp.rrg_tick, inp.rrg_free_slots = parse_rrg_intraday(RRG_SAMPLE)
        inp.c18acc_held, inp.c18acc_fresh = parse_c18acc_screen(C18_SAMPLE)
        inp.buy_radar = parse_buy_radar(BUY_SAMPLE)
        inp.sell_radar = parse_sell_radar(SELL_SAMPLE)
        inp.held_ids = {"2344", "2404"}

        text = format_intraday_1300_digest(inp)
        self.assertIn("2404 漢唐", text)
        self.assertIn("多軌重疊", text)
        self.assertIn("6191 精成科", text)
        self.assertIn("3044 健鼎科技", text)
        self.assertIn("2344 華邦電子", text)
        self.assertIn("持倉監控", text)

    def test_load_digest_input_from_reports_dir(self) -> None:
        reports = ROOT / "reports" / "daily"
        if not (reports / "20260629_vcp_pivot_gate_daily_brief.md").is_file():
            self.skipTest("20260629 briefs not present")
        inp = load_digest_input(session_date="2026-06-29", reports_dir=reports)
        self.assertTrue(inp.vcp or inp.rrg_fresh or inp.rrg_all)


if __name__ == "__main__":
    unittest.main()
