# Retired VCP daily tracks（2026-06）

已退役、移出主線排程：

| 模組 | 原用途 |
|------|--------|
| `vcp_screen.py` | vcp-tm / Minervini daily screen |
| `vcp_intraday_watch.py` | 13:00 tick 盤中 watch |

對應腳本見 `scripts/research/archive/`。

主線改為：

- **Screen**：`src/vcp_funnel_screen.py` → DB `vcp-funnel`
- **13:00 brief**：Pivot Gate / Coil Close（`vcp_funnel_specs_daily.py`）
