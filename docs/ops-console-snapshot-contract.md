# Ops Console Snapshot 契約（後端↔前端 SSOT）

私人 ops console（<https://haoshi-quant-ops.pages.dev>）的**唯一資料匯流排**是 Supabase
表 `ops_snapshots`。後端（本 repo）寫、前端（`goldenyears168-lab/haoshi-quant-ops`）讀。
兩個 repo 沒有共享型別，**本文件就是防漂移的契約**：任何一邊改 payload 形狀，先改這裡。

> 權威來源（SSOT）：payload 由 `src/ops_console_snapshots.py` 的 `build_<kind>_payload()`
> 產生（`commentary` 例外，由雲端 routine 產生）。本文件與程式碼衝突時**以程式碼為準**，
> 並回頭修本文件。

## 架構（誰寫、誰讀）

```
mini evening-sync（週一~五 20:40）              雲端 Routine（週一~五 21:30）
  scripts/launchd/ops-console-evening-sync.command   trig_01DiYNwiUUpj1V9AodZ7HXnj
  → write_ops_console_snapshot.py --kind all         → 讀 rotation/risk snapshot + 新聞
  → BUILDERS[kind]() → ops_snapshots                 → 寫 kind=commentary → ops_snapshots
        │                                                     │
        └──────────────► Supabase ops_snapshots ◄────────────┘
                                 ▲ 讀
                    前端 loadKindSnapshot(kind)
                    haoshi-quant-ops/src/pages/OpsPages.tsx
```

- mini 與雲端 AI **從不直接通訊**，全靠 Supabase。這是它不需 API key、雲端也不需連進 mini 的原因。
- Supabase 專案 ref：`lzaomqzsiqudkojokevr`；anon key 為 public（前端 bundle 內即有），且此表 anon 可寫（unauthenticated ops console）。

## 表結構

**`ops_snapshots`**：`id`（int）· `kind`（text）· `asof`（timestamptz）· `payload`（jsonb）。
前端每個頁面取 `kind=eq.<kind>&order=asof.desc&limit=1`（**取最新一列；非 upsert，每次 run 新增一列**）。

**`ops_digests`**（Inbox 牆／email 行，選配）：`id` · `digest_key` · `channel` · `subject` ·
`body_md` · `severity` · `asof`。由 `write_ops_console_snapshot.py --also-digest` 的
`_digest_for(kind, payload)` 產生。

## 各 kind 的 payload

| kind | schema | 寫入者 | 前端頁 |
|---|---|---|---|
| `watch` | ops-watch-v1 | mini evening-sync | /watch 自選 |
| `risk` | ops-risk-v1 | mini evening-sync | /risk 風控 |
| `thermo` | ops-thermo-v1 | mini evening-sync | /thermo（**已退役·無訊號**，僅顯示） |
| `branches` | ops-branches-v1 | mini evening-sync | /branches 分點 |
| `today` | ops-today-v1 | mini evening-sync | / Today |
| `stage_heatmap` | ops-stage-heatmap-v1 | mini evening-sync | （前端未做頁；資料有） |
| `rotation` | ops-rotation-v1 | mini evening-sync | /rotation 外資輪動 |
| `commentary` | —（無 schema 欄） | **雲端 Routine** | /commentary 每日評論 |

### rotation（ops-rotation-v1）
```
{ present, asof, prev, universe_n, unit:"張",
  top_buy:  [ { stock_id, name|null, net_lots, net_lots_prev, persist:bool } … ≤10 ],
  top_sell: [ …同上… ],
  note_zh, title }
```
`persist=true` 表跨日同向（唯一「有意義」訊號）；`net_lots` 為外資分點聚合淨額÷1000（張）。

### commentary（雲端 Routine 產生）
```
{ commentary_md:"<繁中 markdown 評論>", sources:[ "<新聞URL>" … ], rotation_asof:"<rotation.asof>" }
```
Routine 只讀 `rotation` + `risk`（**不讀 thermo**，已退役）；護欄：數字只來自 snapshot 或有連結新聞、
persist 才算訊號、非投資建議。

### risk（ops-risk-v1）
```
{ level, severity, notes:[], title,
  detach_gate:{ present, level, action_hint, session_date, strategy_id, as_of,
    first_red_poll, first_yellow_poll, rule:{red,yellow},
    latest:{ poll, tw_from_open, nq_from_open, spread_30m, gap_pulse, sync_pulse, red_now, yellow_now } } }
```
欄位人話對照見 `reports/cx/risk_labels.js`。gap_pulse∈WORSEN/IMPROVE/FLAT/NA；
sync_pulse∈FOLLOW/OPPOSE/ONE_SIDE/QUIET/NA；level∈GREEN/YELLOW(_LATCHED)/RED(_LATCHED)/UNKNOWN。

### thermo（ops-thermo-v1）— ⚠ 已退役
```
{ present, asof_date, temp_pct, lamp, consensus, score, trend:[{date,temp_pct,lamp,consensus}], body_md, title, mtime, path }
```
研究判定對新事件判別力 31–35%（低於亂猜），**非可靠訊號**。mini 仍計算、`/thermo` 仍在，
但雲端 AI 已不讀。若要完全退役：從 evening-sync 移除、前端隱藏 nav。

### watch（ops-watch-v1）
```
{ title, sources:[], n_expert_pools,
  evening:{ present, path, mtime, asof, n_items, n_fires, n_quiet, fires:[{label,detail}], preview_md },
  expert_pools:[{stock_id,stock_name,watch_md,watch_mtime}],
  second_disp:{ present, asof, n_hits, hits:[…], note }, tier_a:{ present, entry_watch:[], exit_watch:[], note } }
```

### branches（ops-branches-v1）
```
{ title, evening_asof, evening_fires, n_evening_fires, fired_labels, n_expert_pools, preview_md, second_disp, sources, tier_a_entry_n }
```

### today（ops-today-v1）
```
{ title, links:{risk,watch,thermo,branches}, risk_level, risk_severity,
  thermo_asof, thermo_lamp, thermo_temp_pct, watch_asof, watch_fires, n_expert_pools }
```

### stage_heatmap（ops-stage-heatmap-v1）
```
{ present, field_ssot, engine, confirm_days, asof, built_at, pin, ix_stage,
  counts_30w, s2_tier_counts, s2_gradient,
  rows:[{sid,name,stage,slope_pct,extension_pct,s2_tier,pinned}], note_zh, title }
```
資料有寫入 Supabase，但**前端尚未做對應頁**（待補 /stage 或併入他頁）。

## 新增一個 kind 的檢查清單（防漏防漂移）

1. **後端**：`src/ops_console_snapshots.py` 加 `build_<kind>_payload()`，註冊進 `BUILDERS`
   （evening-sync 的 `--kind all` 會自動涵蓋，**不用改排程**）。
2. **Inbox/email 行（選配）**：`scripts/order/write_ops_console_snapshot.py` 的 `_digest_for` 加 case。
3. **更新本契約文件**（本檔）加該 kind 的 payload。
4. **部署後端**：commit + push；mini `git pull` 或 patch（mini 跑 evening-sync 才會產生）。
5. **前端**（`haoshi-quant-ops`）：`src/pages/OpsPages.tsx` 加頁（用 `loadKindSnapshot('<kind>')`）、
   `App.tsx` 加 route、`components/Shell.tsx` 加 nav。
6. **部署前端**：`npm run build`（需 `.env.local` 內 VITE_SUPABASE_*）→
   `npx wrangler pages deploy dist --project-name=haoshi-quant-ops --branch=main`
   （**Pages 非 git-auto，git push 不會上線**）。本機工作副本：`~/Documents/好時系統 copy/haoshi-quant-ops`。

相關：`docs/src-map.md`、mini 部署見 `deploy/mac-mini/`。
