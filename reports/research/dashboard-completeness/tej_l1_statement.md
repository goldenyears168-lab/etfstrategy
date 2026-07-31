# L1 財報基本面因子(非營收族) — 證偽研究

**維度**: `tej_l1_statement` · TEJ EWIFINQ 季報單季數 · 2026-07-31
**宇宙**: 90 檔流動性個股(與 rev_yoy 研究同宇宙,還原價 PIT 已備;毛利/營益率金融股天生缺,315/1889 列 null)
**期間**: 2021-01-04 → 2026-07-30(1351 交易日)· IS/OOS 70/30 切於 2025-01-07
**方法**: PIT 用財報發布日 a0003(annd,中位 lag 72 日)→ 每股單季數 QoQ/YoY 差分 → annd-gated 日 ffill;方向由 IS Rank-IC 定;週頻 20% 多空 t+1 4bps;permutation 同曝險洗牌 N=500;Deflated-Sharpe 誠實 n_trials=8;對 champion(fut_foreign_oi)與 rev_yoy_3m 雙共線檢定。非投資建議。

## 結論(TL;DR)

**財報面沒有任何過 DSR 的獨立 L1 腳。** 4 個受測趨勢因子全數證偽:

| 因子 | 意義 | dir | IC_is | Sh_is | Sh_oos | perm_p_oos | DSR_oos | DSR_full | corr_rev3m | 判定 |
|---|---|---|---|---|---|---|---|---|---|---|
| **gm_qoq** | 毛利率 QoQ趨勢 | −1 | −0.0004 | −0.30 | −0.56 | 0.557 | 0.03 | — | −0.35 | **NULL**(季節噪音) |
| **opm_qoq** | 營業利益率 QoQ趨勢 | −1 | −0.0001 | −0.37 | −1.43 | 0.890 | 0.00 | — | −0.38 | **NULL/反向** |
| **roe_qoq** | ROE QoQ趨勢 | +1 | +0.012 | +0.33 | +2.15 | **0.002** | 0.925 | **0.514** | +0.39 | **證偽**(IS≈0、DSR全樣0.51、僅多頭) |
| **eps_yoy** | EPS 年增動能 | +1 | +0.012 | +0.38 | +1.41 | 0.028 | 0.709 | 0.315 | **+0.62** | **證偽**(rev_yoy_3m重述) |

季節性乾淨 robustness 變體(margins/roe 的 QoQ 有季別週期,補做 YoY 版 + EPS QoQ):gm_yoy DSR_full 0.28、opm_yoy 0.28、roe_yoy 0.46、eps_qoq 0.51 — 全數不過。

## 三個致命證據

1. **DSR 全樣本崩壞**。表面 roe_qoq OOS Sharpe +2.15、perm_p 0.002、DSR_oos 0.925 看似逼近門檻;但 OOS 窗(2025-01→2026-07)是**低波多頭**——同期宇宙等權 B&H OOS Sharpe 就有 +2.12、rev_yoy_3m 有 +2.32,幾乎任何做多傾向都好看。改算全樣本 DSR:roe_qoq 掉到 **0.514**、eps_qoq 0.505,離 0.95 極遠。OOS-only DSR 被有利 regime 灌水。

2. **In-sample 幾乎沒有證據**。所有正因子 IS Sharpe 僅 +0.33~+0.38(含 2022 空頭的 IS 才是誠實區間),力道全集中在薄 OOS。且 **bull/bear 極不對稱**(roe_qoq bull +1.20 / bear −0.06),典型「品質因子只在多頭有效」= regime/beta tilt,非獨立 alpha。

3. **EPS 動能 = 營收動能重述**。eps_yoy 對 rev_yoy_3m 相關 +0.62;把 EPS 訊號**橫斷面對 rev_yoy_3m 正交化**後直接崩(IC_is −0.005、Sh_is −0.31、Sh_oos −0.42)。roe_yoy 正交後同樣崩(IC_is −0.007)。EPS/ROE-YoY 沒有超出營收動能的獨立內容。

## roe_qoq 的「差一點」與為何仍否決

roe_qoq 是唯一對 rev_yoy_3m 正交後**仍留殘餘訊號**者(正交後 IC_is +0.007、Sh_oos +1.29),與 champion 也僅 +0.14 弱相關 → 確實不是營收動能也不是 champion 的重述。但否決理由充分:全樣 DSR 0.514、IS Sharpe 正交後僅 +0.14(近零)、純多頭。它是「ROE 環比改善的股票在多頭跑贏」,edge 由 regime 承載,非可獨立部署腳。

## 與既有結論一致性

- 對齊 Phase-4:ROE **水位**=null;本研究補證 ROE **趨勢**、毛利/營益率趨勢、EPS 動能亦全不過 DSR。
- 對齊 rev_yoy_3m 為唯一存活橫斷面 alpha:財報季報族無法在其之外新增獨立腳;EPS 動能被證實只是營收動能的財報側投影。
- 對齊「champion 仍唯一領先 + 淨新增獨立 alpha=0」的儀表板總結。

## 產出

- 資料:`data/research/dashboard/tej_l1_statement.parquet`(90 檔 1889 列,gm/opm/roe/eps/npm 單季 + annd)
- 腳本:`scripts/research/dashboard/tej_l1_statement_fetch.py` · `tej_l1_statement_study.py`
- 指標:`reports/research/dashboard-completeness/tej_l1_statement_metrics.csv`

**覆蓋誠實**:宇宙僅 90 檔流動大中型(受限於既有還原價 PIT),偏大型;rev_yoy_3m 已知 alpha 集中中小型,故本研究對「小型股財報趨勢」證據力偏弱——但受測方向皆 IS≈0 或負,擴宇宙翻案機率低。TEJ E-SHOP 財報自 2021 起,OOS 僅 ~1.5 年且為多頭,為結構性限制。
