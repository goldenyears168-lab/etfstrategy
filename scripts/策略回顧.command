#!/usr/bin/env bash
# 已退役：p6-tier signal_review / track_evaluation 跨軌審計已移除。
# Copytrade 回測：scripts/run_00981a_copytrade_backtest.py

set -euo pipefail

echo "策略回顧（signal_review）已自 Research OS 移除。"
echo "請改用手動回測："
echo "  scripts/run_00981a_copytrade_backtest.py --strategy L1H9"
echo "  scripts/write_copytrade_slot_summary.py"
read -r -p "按 Enter 關閉…"
exit 0
