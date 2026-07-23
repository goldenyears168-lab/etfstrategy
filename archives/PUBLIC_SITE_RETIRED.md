# 公開站退役（2026-07-23）

依問卷：備份後清空 Supabase `stock_research.*` / `yahoo_*`，專案殼改 `ops.*`。

- 舊前端已移出工作樹 → `~/Documents/股市資料備份封存_20260723/舊站原始碼/`
- 新站：獨立 repo `haoshi-quant-ops` · Cloudflare `https://haoshi-quant-ops.pages.dev`
- 本 monorepo：`RUN_SUPABASE_*_SYNC` 視為退役；勿再推 `site_content` / briefs 到雲端
- 殘留 `src/supabase_*.py` 可之後刪（已無表可寫）

**勿動** Supabase「好時系統」專案。

## mini 寫入（2026-07-23+）

見 [`scripts/ops/README.md`](../scripts/ops/README.md)「Private ops console」。核心：

```bash
# Live TA（2492 預設）
PYTHONPATH=src .venv/bin/python scripts/order/run_ops_live_ta_poll.py

# 持倉（需 Fubon）
.venv-fubon/bin/python scripts/order/run_ops_holdings_sync.py

# 袖狀態
PYTHONPATH=src .venv/bin/python scripts/order/write_ops_sleeve_status.py
```
