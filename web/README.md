# 網站層（Website layer）

## 方案 A · 本地 dev 讀檔案（現行）

```
reports/publish/     ← SSOT
web dev              VITE_USE_LOCAL_BRIEFS=1 → /api/briefs/*
```

Supabase 同步**已自 launchd 排程移除**；將來上線再手動 backfill。

## 開發

```bash
# .env
VITE_USE_LOCAL_BRIEFS=1

python scripts/mirror_to_publish.py   # 首次或補齊 publish/
cd web && npm run dev
```

## 將來恢復 Supabase（手動）

```bash
# .env 設 RUN_SUPABASE_RESEARCH_SYNC=1 + service_role key
python scripts/backfill_supabase_research.py --days 14
# 或
./scripts/research_supabase_sync.sh 1630
```

資料夾說明：[`reports/publish/README.md`](../reports/publish/README.md)
