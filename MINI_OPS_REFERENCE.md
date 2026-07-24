# Expert pool · Mac mini 操作參考（Research · 非 Order）

- 日期：2026-07-21
- 性質：**觀測／郵件參考** · **未採納** Strategy／Order · 不產生 intents
- Book 先改碼；sync mini 後才生效（本 agent **不** SSH mini）

## 一句話

**只跟 hard 專家綠燈進場、L1H7 持有；黃燈／軟共識不當買；H10／黃燈註記僅研究。**

## 凍結子彈（郵件 footer / 人工）

| # | 規則 | 來源 |
|---|------|------|
| 1 | **進場**：僅各股 champion **hard 綠燈**（回看1日／同日／OR × core×floor） | P7 watch · skill |
| 2 | **持有協議 SSOT**：**L1H7**（T+1 open → 第 7 交易日收）· 成本 30bps · β=1.15×IX0001 | funnel 協議 |
| 3 | **不買黃燈**：Top10 light2 黃燈 **≠ 買訊**；可選郵件註記（預設關） | `YELLOW_EMAIL_ANNOTATION.md` |
| 4 | **軟共識 ≠ 進場**：SoftOR／SoftHalf 不取代 hard | `H_SOFT_VS_HARD_GREEN.md` |
| 5 | **H10**：研究註記「較長持有可優於 H7」· **非**提前賣出規則、**非**條件延長 SSOT | `H_GREEN_HOLD_EXIT.md` · `H_GREEN_H7_EXTEND.md` |
| 6 | **H7 當天延長**：弱研究候選＝H7 仍有 softOR／softHalf（**≠進場**）；**未 promote** — 預設仍 H7 出 | `H_GREEN_H7_EXTEND.md` |

## Book 已接線

| 項 | 路徑 | 行為 |
|----|------|------|
| 單股／specialty 信 footer | `scripts/research/run_expert_pool_watch.py` → `format_ops_reference_footer()` | 每封主信末加「操作參考」段 |
| 20:00 digest footer | `scripts/research/run_evening_research_watch_digest.py` | 合併信**末尾一次**（區塊內 `include_ops_ref=False` 避免重複） |
| 黃燈註記 | `src/research/expert_pool_yellow_annotation.py` | **預設 OFF** |
| H7 延長研究 | `scripts/research/run_h_green_h7_extend.py` → `hypotheses/H_GREEN_H7_EXTEND.md` | Research only |

### Footer 文案（落地）

```text
—— 操作參考（Research 凍結 · 非自動送單）——
  · 進場：僅 hard 專家綠燈（champion 共識）· L1H7 為協議 SSOT
  · 共識密度：信內「今日觸發 n/N」＋各專家 L1H7 勝率／n（ledger）
  · 09:05：可選加權、非硬過濾；25m 非 fail 屬松山 sleeve，非專家池預設
  · 不買：黃燈／Top10 light2 不當買訊；軟共識≠進場
  · 持有：預設持到 H7；H10 僅研究註記（非提前賣出規則）
  · 黃燈註記：預設關；mini 可設 EXPERT_POOL_YELLOW_EMAIL_ANNOTATION=1（仍非買訊）
  · 詳見 reports/.../expert_pool/MINI_OPS_REFERENCE.md
```

## 郵件「共識密度」段（落地）

每檔專家池區塊開頭附近會印：

- `今日觸發：k/N 專家（p%）` — k＝主訊號在場 hard 專家數，N＝該股 core 池大小
- 每位 core 專家一行：`★/·` 是否今日在場、role、**L1H7 n／勝率／中位真實報酬**（來自 `knowledge/trades/{sid}.json`）

09:05 過濾**不會**在 20:00 信裡判定（尚無隔日開盤資料）；只在「進場註記」寫可選加權提示。

## 松山夜信進場提示（2026-07-22）

- **觸發尺（已改）**：`跟單松山·五日累積淨比95 ∩ !mega`（`R_5d_net95_xMega`）
  - 五日買進 ≥ **0.5 億（5,000 萬）** ∩ 五日淨比 ≥ **95%** ∩ 非 mega
- **≥1.5億**：改為「強印」標註，**不再**當唯一觸發
- 信內進場提示：`entry_25m_nonfail`（T+1≈09:25 人工；夜信不判定）
- 新店：暫維持 `R_1p5_xMega`
- 文案／掃描：`scripts/research/run_songshan_follow_watch.py`
- 說明書：`reports/research/branch-footprint-screen/凱基松山_凱基信義_跟單說明書.md` §0.1／§1.0／§1.3.1

Book 改完後 mini：`git pull` 或 rsync → 下次 20:00 digest 生效。

## 如何在 mini 啟用（人工 · 可選）

### A. 程式／文案（必做才看到 footer）

Book push／rsync 後，在 mini：

```bash
ssh mac-mini 'cd ~/Documents/ETF/股票研究 && git pull'
# 若用 launchd 模板重裝：scripts/install-launchd.sh（僅 mini；勿在 Book 裝 live）
```

20:00 job：`com.jackm4.etf.winbond-expert-pool-watch` → `run_evening_research_watch_digest.py`。

### B. 黃燈郵件註記（預設保持 OFF）

**不要強制開**，除非已讀 `hypotheses/YELLOW_EMAIL_ANNOTATION.md` 並接受誤報（多數黃燈不會很快變綠燈）。

在 **mini** `.env`（launcher 會 `source`；**不要**寫進 plist）：

```bash
EXPERT_POOL_YELLOW_EMAIL_ANNOTATION=1
```

單次驗證：

```bash
PYTHONPATH=src .venv/bin/python scripts/research/run_evening_research_watch_digest.py \
  --no-refresh --yellow-annotation --dry-run
```

關閉：刪除該行或設 `=0`。

Launchd 模板註解已寫在：

- `launchd/winbond-expert-pool-watch-launcher.sh.template`
- `launchd/specialty-expert-pool-watch-launcher.sh.template`
- `launchd/com.jackm4.etf.winbond-expert-pool-watch.plist.template`（XML comment）

## H7 → H10 條件延長（研究結論摘要）

見 `hypotheses/H_GREEN_H7_EXTEND.md`（2026-07-21 · n=187 H7∩H10）。

| 結果 | 數字 |
|------|------|
| 無條件 Δ(H10−H7) | ALL med **+1.82%** · OOS med **+3.35%** · P(Δ>0) 60% |
| **弱凍結候選** `soft_or7` | ON OOS medΔ **+6.53%** vs OFF **+2.00%**（on−off **+4.52%** · cover 32%） |
| 同門 `soft_half7` / `softish7` | ON OOS +5.22% vs OFF +2.39%（+2.84pp · cover 26%） |
| `green7`（續硬綠） | 最大 lift（OOS +8.70% vs +2.26%）但 cover **14%** → **不過門** |
| ts2／無重賣／>SMA5／浮盈 | OOS on−off **負或近零** → **不支持**延長 |

**Ops 預設**：仍 **H7 出場**。軟共識僅作「若已決定研究式續抱 H10」的弱提示；**不**改郵件進場規則、**不**進 Order。

## 明確不做

- Order intents／`config/strategy.yaml` 採納
- Book 裝 live `com.jackm4.etf.*`
- 預設打開黃燈註記
- 把 H10 或 H7 條件延長寫成自動賣出／續抱規則
