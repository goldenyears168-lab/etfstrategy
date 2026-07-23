"""Tests for intraday_open_digest."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT / "src"))

from intraday_1300_digest import DigestInput, parse_vcp_brief  # noqa: E402
from intraday_open_digest import (  # noqa: E402
    OpenDigestInput,
    format_intraday_open_digest,
    parse_c18acc_extended,
    parse_gate_report,
    parse_morning_brief,
    parse_sell_log_for_session,
)

GATE_SAMPLE = """\
# 09:05 組合閘門 · 2026-06-30
**portfolio_exit_mode=—（略過 · 無有效 09:05 價）**
- 1m K sync：**0** bars
- 略過：**no order_holdings_snapshot for session**
"""

MORNING_SAMPLE = """\
## 資料來源
- 結構／昨收：PIT · bar 2026-06-26 · RRG 2026-06-26
## 隔夜／台指 gap
- TX gap **+5.81%** @ 47161.0
- ⚠ 台指 gap 偏大 (+5.81%) → 開盤波動風險
## 開盤前優先盯
- **結構弱**：2327(國巨*)
- **帳面大虧**：2449 -2,625、6223 -2,080
"""

C18_SAMPLE = """\
> pool_as_of **2026-06-26**
- kbar sync：**0** bars · watchlist **1** 檔
- slots：**1 / 3**
## fresh mono 全池（昨收 PIT · 2026-06-26）
| # | 代號 | 名稱 | fr | seg_last | 位移 |
|---|------|------|----|----------|------|
| 1 | 2404 | 漢唐 | ★ | 0.920 | 1.62 |
## 持倉（C18acc state）
| 槽 | 代號 | 名稱 | 進場 | hold |
|---|------|------|------|------|
| 1 | 2404 | 漢唐 | 2026-06-29 | 2026-06-29 |
"""

SELL_LOG_SAMPLE = """\
════════════════════════════════════════════════════
【賣出觀測】2026-06-29 09:10
掃描時間：2026-06-29T09:11:00+08:00
宇宙：監測 149 檔 · 當日有 1m K 16 檔
持倉：11 檔
portfolio_exit_mode=OFF · holdings_stress=OFF · extension 宇宙 0 檔

→ 本輪無持倉交集 extension 觸發
寄信：否
════════════════════════════════════════════════════
"""

VCP_SAMPLE = """\
> 上一收盤 K 線 **2026-06-26** + 今日 tick
| 代號 | 名稱 | composite | state | pivot | dist% | stop |
| 2404 | 漢唐 | 57.1 | Pre-breakout | 1430.00 | -4.5 | 1207.80 |
| 3044 | 健鼎科技 | 54.4 | Pre-breakout | 540.00 | -7.2 | 449.46 |
"""


class IntradayOpenDigestTests(unittest.TestCase):
    def test_parse_gate(self) -> None:
        g = parse_gate_report(GATE_SAMPLE)
        self.assertEqual(g.kbar_sync_n, 0)
        self.assertIn("no order_holdings", g.skip_reason or "")

    def test_parse_gate_5m(self) -> None:
        g = parse_gate_report(
            "# gate\n- 5m K sync：**42** bars\n- 略過：**no 09:05 kbar for 2330**"
        )
        self.assertEqual(g.kbar_sync_n, 42)

    def test_parse_sell_log_5m(self) -> None:
        s = parse_sell_log_for_session(
            "【賣出觀測】2026-07-08 09:10\n宇宙：監測 149 檔 · 當日有 5m K 120 檔\n",
            "2026-07-08",
        )
        self.assertTrue(s.log_found)
        self.assertEqual(s.universe_kbar_n, 120)

    def test_parse_morning(self) -> None:
        m = parse_morning_brief(MORNING_SAMPLE)
        self.assertEqual(m.tx_gap_pct, "+5.81%")
        self.assertIn("2327", m.weak_line or "")

    def test_parse_c18acc_extended(self) -> None:
        c = parse_c18acc_extended(C18_SAMPLE)
        self.assertEqual(c.slots_used, 1)
        self.assertEqual(len(c.fresh_rows), 1)
        self.assertAlmostEqual(c.fresh_rows[0].seg_last, 0.920)

    def test_parse_sell_log(self) -> None:
        s = parse_sell_log_for_session(SELL_LOG_SAMPLE, "2026-06-29")
        self.assertTrue(s.log_found)
        self.assertEqual(s.universe_n, 149)
        self.assertFalse(s.holdings_stress)
        self.assertEqual(len(s.structural), 0)

    def test_format_open_digest(self) -> None:
        core = DigestInput(session_date="2026-06-29")
        core.vcp, _, core.last_close_date = parse_vcp_brief(VCP_SAMPLE)
        inp = OpenDigestInput(session_date="2026-06-29", core=core)
        inp.gate = parse_gate_report(GATE_SAMPLE)
        inp.morning = parse_morning_brief(MORNING_SAMPLE)
        inp.c18acc = parse_c18acc_extended(C18_SAMPLE)
        inp.sell = parse_sell_log_for_session(SELL_LOG_SAMPLE, "2026-06-29")
        inp.vcp_source = "DB close · 2026-06-26"

        text = format_intraday_open_digest(inp)
        self.assertIn("開盤解讀", text)
        self.assertIn("5m K 同步", text)
        self.assertIn("TX gap +5.81%", text)
        self.assertIn("2404 漢唐", text)
        self.assertIn("今日開盤優先關注", text)


if __name__ == "__main__":
    unittest.main()
