from __future__ import annotations

import unittest

from sync_mutual_fund_holdings import (
    ALLIANZ_TW_TECH,
    month_end_from_ym,
    parse_sitca_fund_block,
    ym_values,
)
from datetime import date

SITCA_QUARTER_SAMPLE = """
<tr><td class='DTeven' align='left' rowspan='2'>安聯台灣科技基金</td>
<td class='DTeven' align='left'>國內上市</td>
<td class='DTeven' align='left'>2330</td><td class='DTeven' align='left'>台積電</td>
<td class='DTeven' align='right'>4,730,000,000</td><td class='DTeven' align='left'></td>
<td class='DTeven' align='left'></td><td class='DTeven' align='right'>0</td>
<td class='DTeven' align='right'>9.39</td></tr>
<tr><td class='DTodd' align='left'>國內上市</td>
<td class='DTodd' align='left'>2345</td><td class='DTodd' align='left'>智邦</td>
<td class='DTodd' align='right'>2,860,100,000</td><td class='DTodd' align='left'></td>
<td class='DTodd' align='left'></td><td class='DTodd' align='right'>0</td>
<td class='DTodd' align='right'>5.68</td></tr>
<tr><td class='DTsubtotal' align='center' colspan='8'>合計</td>
<td class='DTsubtotal' align='right'>15.07</td></tr>
</table>
"""

SITCA_MONTH_SAMPLE = """
<tr><td class='DTeven' align='left' rowspan='3'>安聯台灣科技基金</td>
<td class='DTeven' align='right'>1</td><td class='DTeven' align='left'>國內上櫃</td>
<td class='DTeven' align='left'>6223</td><td class='DTeven' align='left'>旺矽</td>
<td class='DTeven' align='right'>18,000,000,000</td><td class='DTeven' align='right'>8.02</td></tr>
<tr><td class='DTodd' align='right'>2</td><td class='DTodd' align='left'>國內上市</td>
<td class='DTodd' align='left'>2330</td><td class='DTodd' align='left'>台積電</td>
<td class='DTodd' align='right'>8,000,000,000</td><td class='DTodd' align='right'>7.05</td></tr>
<tr><td class='DTsubtotal' align='center' colspan='9'>合計</td>
<td class='DTsubtotal' align='right'>15.07</td></tr>
<tr><td class='DTeven' align='left' rowspan='2'>安聯台灣智慧基金</td>
<td class='DTeven' align='right'>1</td><td class='DTeven' align='left'>國內上市</td>
<td class='DTeven' align='left'>2454</td><td class='DTeven' align='left'>聯發科</td>
<td class='DTeven' align='right'>1,000,000,000</td><td class='DTeven' align='right'>5.00</td></tr>
</table>
"""


class MutualFundHoldingsTests(unittest.TestCase):
    def test_month_end_from_ym(self) -> None:
        self.assertEqual(month_end_from_ym("202402"), "2024-02-29")
        self.assertEqual(month_end_from_ym("202405"), "2024-05-31")

    def test_ym_values_two_years_window(self) -> None:
        months = ym_values(date(2024, 6, 1), date(2026, 5, 1))
        self.assertEqual(months[0], "202406")
        self.assertEqual(months[-1], "202605")
        self.assertEqual(len(months), 24)

    def test_parse_sitca_fund_block_filters_target_fund(self) -> None:
        rows = parse_sitca_fund_block(SITCA_MONTH_SAMPLE, ALLIANZ_TW_TECH.fund_name)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["stock_id"], "6223")
        self.assertEqual(rows[0]["stock_name"], "旺矽")
        self.assertEqual(rows[0]["weight_pct"], 8.02)
        self.assertEqual(rows[1]["stock_id"], "2330")

    def test_parse_sitca_quarter_block_without_rank_column(self) -> None:
        rows = parse_sitca_fund_block(SITCA_QUARTER_SAMPLE, ALLIANZ_TW_TECH.fund_name)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["rank_no"], 1)
        self.assertEqual(rows[0]["stock_id"], "2330")
        self.assertEqual(rows[1]["stock_id"], "2345")

    def test_allianz_profile_constants(self) -> None:
        self.assertEqual(ALLIANZ_TW_TECH.fund_code, "ACDD04")
        self.assertEqual(ALLIANZ_TW_TECH.sitca_company_id, "A0036")


if __name__ == "__main__":
    unittest.main()
