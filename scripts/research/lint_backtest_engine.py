#!/usr/bin/env python3
"""Static keyword/regex lint for backtest scripts — first-line defense only.

見 docs/research-integrity-checklist.md。這支腳本對一支回測腳本的**原始碼文字**做
關鍵字／正則掃描，尋找本 repo 研究史反覆踩到的方法論 bug 類型的「明顯缺席」訊號：

  1. Session/窗口結尾未平倉部位是否有結算（force-close / session_end 對帳）
  2. `causal_*` 命名的函式附近有沒有出現位移語法（look-ahead 的弱訊號）
  3. 有沒有做過自相關校正的顯著性檢定（HAC / Newey-West），還是只有 naive p-value
  4. 成交價賦值附近有沒有可疑的 min()/max() favorable-price 選擇
  5. 自動生成的 verdict/robustness 標籤（存在即應提醒人工複核判斷邏輯）
  6. 對帳不變量（n_signals/n_fills/n_entered 等 assert）是否存在
  7. deadline/時限觸發的動作（如強制平倉）是否寫在「查詢情況是否已自行解決」之前
     （state-check-ordering race condition）
  8. 只用 mean()/.mean() 判定 edge/顯著性，附近沒有 median() 或 win_rate/hit_rate
     交叉檢查
  9. DSR/Deflated-Sharpe 等多重比較校正，有沒有留下「參考族群本身沒被 regime
     灌高」的人工核對痕跡（最弱的一條檢查，見下方限制）

**重要限制（誠實揭露，不是佯裝完整）**：
  - 純字串/正則比對，不理解程式語意，會有 false positive 也會有 false negative。
  - 完全無法偵測 fold-aggregate 後見之明（bug 類型 3，見 checklist）——那需要理解
    「這個統計量有沒有用到未來資料」的語意，regex 做不到。
  - 無法驗證「因果函式真的沒有 look-ahead」，只能檢查「附近有沒有位移語法」這種弱
    訊號；位移量算錯、或只有分子位移分母沒位移這類真正的 bug 抓不到。
  - 無法偵測敘事與數據矛盾、樣本 n 計算錯誤、fold 切法不一致——這些需要交叉比對實際
    跑出來的數字，不是靜態掃描能做的事。
  - 只看單一檔案的原始碼文字，不執行程式、不看實際輸出。
  - Check 7（deadline vs 狀態查詢順序）只看「同一個函式主體」這麼淺的範圍：真正的
    狀態查詢如果寫在別的函式/模組，或透過回呼間接發生，這條規則會誤判成 WARN；
    也完全不理解「動作」有沒有真的送出（可能被更外層 guard 擋掉）。
  - Check 8（mean-only）只在同檔案有 edge/顯著性/p_value 這類關鍵字時才觸發，避免
    對純統計描述腳本洗版；但也代表它會漏掉「其實有算 median，只是沒放進判定邏輯」
    這種語意層問題，看到 median()/win_rate 字樣就判 OK，不驗證兩者是否真的互相佐證。
  - Check 9（DSR 參考母體）是全部 9 條裡**最弱**的一條：純關鍵字比對完全不理解
    「trial 母體有沒有包含被拒絕(rejected)的變體、兩者 Sharpe 分布有沒有系統性差異」
    這種語意，幾乎肯定會 under-detect。WARN 不代表一定有問題，OK 也不保證真的乾淨
    ——這條唯一可靠的用法是把它當「提醒人工去確認」的觸發器，不是判決。

用法：
  PYTHONPATH=src .venv/bin/python scripts/research/lint_backtest_engine.py <script.py> [more.py ...]

Exit code：永遠是 0（這是提醒工具，不是 CI gate）；用 --strict 讓「有任何 WARN/MISSING」
時 exit code 為 1，方便串進其他自動化流程，但預設不建議這樣用——通過 lint 不代表回測
誠信沒問題，見 docs/research-integrity-checklist.md 文末的免責聲明。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CheckResult:
    check_id: str
    title: str
    status: str  # "ok" | "warn" | "missing" | "info"
    detail: str
    matched_lines: list[tuple[int, str]] = field(default_factory=list)


STATUS_LABEL = {
    "ok": "[OK]     ",
    "info": "[INFO]   ",
    "warn": "[WARN]   ",
    "missing": "[MISSING]",
}


def _find_lines(lines: list[str], pattern: str, flags: int = re.IGNORECASE) -> list[tuple[int, str]]:
    rx = re.compile(pattern, flags)
    hits = []
    for i, line in enumerate(lines, start=1):
        if rx.search(line):
            hits.append((i, line.strip()))
    return hits


def check_session_end_accounting(lines: list[str]) -> CheckResult:
    """BUG-1: session/窗口結尾未平倉部位是否有結算."""
    hits = _find_lines(
        lines,
        r"force[_\s-]?close|session_end|forceclose|強制平倉|flat[_-]?default",
    )
    if hits:
        return CheckResult(
            "session_end_accounting",
            "Session 結尾強制平倉 / session_end 結算",
            "ok",
            f"找到 {len(hits)} 處 force-close / session_end 相關字樣，"
            "但仍需人工確認：(a) 是否每個 (day,session,sleeve) 區塊都保證恰好一筆結算、"
            "(b) 是否真的把未平倉部位計入損益（而非只是標記/略過）。",
            hits[:5],
        )
    return CheckResult(
        "session_end_accounting",
        "Session 結尾強制平倉 / session_end 結算",
        "missing",
        "沒找到 force-close/session_end/強制平倉 相關字樣——"
        "若這是 always-in（持續反手）系統，每個窗口結尾幾乎必然留有未平倉部位；"
        "務必人工確認結尾部位是否被計入損益，見 checklist BUG-1。",
    )


def check_reconciliation_assert(lines: list[str]) -> CheckResult:
    """BUG-1 輔助: n_signals/n_fills/n_entered 對帳 assert."""
    accounting_names = r"n_signals|n_fills|n_entered|n_trades|n_skipped|n_events"
    assert_hits = _find_lines(lines, rf"assert.*({accounting_names})")
    mention_hits = _find_lines(lines, accounting_names)
    if assert_hits:
        return CheckResult(
            "reconciliation_assert",
            "對帳不變量 assert（n_signals/n_fills/n_entered...）",
            "ok",
            f"找到 {len(assert_hits)} 處 assert 涉及計數變數，人工確認不變量是否"
            "同時涵蓋『已平倉交易』與『期末強制平倉』兩邊，不能只 assert 其中一半。",
            assert_hits[:5],
        )
    if mention_hits:
        return CheckResult(
            "reconciliation_assert",
            "對帳不變量 assert（n_signals/n_fills/n_entered...）",
            "warn",
            f"有提到計數變數（{len(mention_hits)} 處）但沒找到對應的 assert 陳述式，"
            "計數本身可能只是列印/記錄用，沒有被拿來驗證不變量。",
            mention_hits[:5],
        )
    return CheckResult(
        "reconciliation_assert",
        "對帳不變量 assert（n_signals/n_fills/n_entered...）",
        "missing",
        "完全沒找到任何對帳計數變數或 assert——建議至少對每個 (day,session) 區塊"
        "斷言『訊號數 == 已平倉數 + 未平倉數』，見 checklist BUG-1。",
    )


def check_causal_shift(lines: list[str]) -> CheckResult:
    """BUG-2: causal_* 函式附近是否有位移語法."""
    text = "\n".join(lines)
    causal_fn_defs = list(re.finditer(r"def\s+(\w*causal\w*|\w*ex_ante\w*)\s*\(", text, re.IGNORECASE))
    if not causal_fn_defs:
        return CheckResult(
            "causal_shift_hint",
            "causal_* 函式的位移語法（look-ahead 弱訊號）",
            "info",
            "沒找到命名包含 causal/ex_ante 的函式——若這支腳本本來就不宣稱自己是"
            "因果版本，此檢查不適用；若有因果特徵但函式命名不同，本 lint 抓不到，"
            "需人工確認。",
        )
    warnings = []
    ok_count = 0
    for m in causal_fn_defs:
        fn_name = m.group(1)
        start = m.end()
        # 粗略抓函式主體：抓到下一個同縮排等級的 def/class，或往後 60 行為界
        remainder = text[start:]
        next_def = re.search(r"\ndef\s|\nclass\s", remainder)
        body = remainder[: next_def.start()] if next_def else remainder[:3000]
        body_lines = body.count("\n")
        if body_lines > 80:
            body = "\n".join(body.split("\n")[:80])
        has_shift = bool(re.search(r"shift\(\s*1\)|shift\(-?\d+\)|\[\s*[a-zA-Z_]\s*-\s*1\s*\]|t\s*-\s*1|i\s*-\s*1", body))
        if has_shift:
            ok_count += 1
        else:
            line_no = lines_upto(text, m.start())
            warnings.append((line_no, f"def {fn_name}(...) — 函式主體內找不到 shift(1)/[t-1] 這類位移語法"))
    if warnings and ok_count == 0:
        return CheckResult(
            "causal_shift_hint",
            "causal_* 函式的位移語法（look-ahead 弱訊號）",
            "warn",
            f"{len(causal_fn_defs)} 個 causal 命名函式裡，{len(warnings)} 個找不到位移語法"
            "——不代表一定有 look-ahead（可能用了別的寫法），但建議做擾動測試"
            "（改動 t 的資料，確認 t 之前的輸出不變）逐一驗證，見 checklist BUG-2。",
            warnings[:5],
        )
    if warnings:
        return CheckResult(
            "causal_shift_hint",
            "causal_* 函式的位移語法（look-ahead 弱訊號）",
            "warn",
            f"{ok_count}/{len(causal_fn_defs)} 個 causal 函式找到位移語法，"
            f"{len(warnings)} 個沒找到，逐一人工確認。",
            warnings[:5],
        )
    return CheckResult(
        "causal_shift_hint",
        "causal_* 函式的位移語法（look-ahead 弱訊號）",
        "ok",
        f"{len(causal_fn_defs)} 個 causal 命名函式都找到位移語法字樣——"
        "這只是弱訊號，仍強烈建議實際做擾動測試（見 checklist BUG-2），"
        "regex 找到 shift() 不保證位移量正確、也不保證分子分母都有位移。",
    )


def lines_upto(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def check_significance_testing(lines: list[str]) -> CheckResult:
    """BUG-4: HAC/Newey-West 校正 vs naive-only p-value."""
    hac_hits = _find_lines(lines, r"newey|hac[_\s-]?robust|newey[_-]?west|maxlags")
    pvalue_hits = _find_lines(lines, r"p[_\s-]?value|\bp_val\b|ttest|t_test|significan")
    if not pvalue_hits:
        return CheckResult(
            "significance_testing",
            "顯著性檢定（HAC / Newey-West 自相關校正）",
            "info",
            "沒找到任何顯著性檢定相關字樣——若這支腳本只是資料處理/畫圖，此檢查"
            "不適用；若它會下 passed/rejected 判定卻完全沒做統計檢定，這正是"
            "checklist BUG-4 的典型案例（H-SC-STAT-SIGNIFICANCE-GAP），應補做。",
        )
    if hac_hits:
        return CheckResult(
            "significance_testing",
            "顯著性檢定（HAC / Newey-West 自相關校正）",
            "ok",
            f"找到 p-value 相關字樣 {len(pvalue_hits)} 處、HAC/Newey-West 相關字樣 "
            f"{len(hac_hits)} 處——人工確認：(a) 是否掃過多個 maxlags、(b) 正負向"
            "結果是否套用同一套顯著性門檻，見 checklist BUG-4。",
            hac_hits[:5],
        )
    return CheckResult(
        "significance_testing",
        "顯著性檢定（HAC / Newey-West 自相關校正）",
        "warn",
        f"找到 p-value 相關字樣（{len(pvalue_hits)} 處）但沒找到 HAC/Newey-West/"
        "maxlags 字樣——很可能只做了 naive t-test，沒有做自相關校正。逐日/逐筆"
        "序列若有正自相關，naive p 值會虛高（本 repo 案例：p=0.029 校正後回升到"
        "0.058~0.091），見 checklist BUG-4。",
        pvalue_hits[:5],
    )


def check_favorable_price_clamp(lines: list[str]) -> CheckResult:
    """BUG-5: fill price 附近的可疑 min()/max() 選擇."""
    price_vars = r"fill_price|exit_price|entry_price|fill_px|exec_price|成交價"
    hits = []
    for i, line in enumerate(lines, start=1):
        if re.search(price_vars, line, re.IGNORECASE) and re.search(r"\bmin\(|\bmax\(", line):
            hits.append((i, line.strip()))
    if hits:
        return CheckResult(
            "favorable_price_clamp",
            "成交價 min()/max() 可疑選擇（favorable-price clamp）",
            "warn",
            f"找到 {len(hits)} 處成交價變數附近有 min()/max()——這**可能**只是正常"
            "的 clip/bound 邏輯，但也可能是本 repo 已知案例（H-SC-EDGE-REVERT，"
            "88% 宣稱獲利來自 clamp）的同一種 bug。逐一人工確認：是否在多個候選"
            "價格間，單方向偏袒交易方向；建議做『強制用 next_open 誠實成交』的"
            "對照重跑，見 checklist BUG-5。",
            hits[:8],
        )
    return CheckResult(
        "favorable_price_clamp",
        "成交價 min()/max() 可疑選擇（favorable-price clamp）",
        "ok",
        "沒找到成交價變數附近的 min()/max() pattern——注意這只是字面比對，"
        "clamp 邏輯也可能寫成 if/else 條件分支而非 min()/max()，regex 抓不到，"
        "仍建議人工確認成交價來源。",
    )


def check_auto_verdict_labels(lines: list[str]) -> CheckResult:
    """BUG-6: 自動生成 verdict/robustness 標籤."""
    hits = _find_lines(lines, r"verdict|robustness_verdict|claim_survives|robust_label")
    if hits:
        return CheckResult(
            "auto_verdict_labels",
            "自動生成 verdict/robustness 標籤",
            "warn",
            f"找到 {len(hits)} 處自動生成標籤賦值——這類標籤在本 repo 至少有 2 次"
            "被證實判斷邏輯本身有 bug（正負號邊界條件沒處理，見"
            "H-SC-TICK-TRIGGER）。務必人工重算判斷邏輯，尤其測試『其中一邊為負數』"
            "的邊界情況，不要只憑標籤文字的自信程度採信，見 checklist BUG-6。",
            hits[:5],
        )
    return CheckResult(
        "auto_verdict_labels",
        "自動生成 verdict/robustness 標籤",
        "info",
        "沒找到自動生成 verdict/robustness 標籤——若這支腳本本來就不產生"
        "自動判定文字，此檢查不適用。",
    )


def check_deadline_before_status_check(lines: list[str]) -> CheckResult:
    """BUG-7: deadline/時限判斷觸發的動作，是否寫在『查詢情況是否已自行解決』之前.

    真實案例：src/order/dayflip_short_order.py 修復前，`reconcile_once()` 的
    `entered` 分支先判斷 `hm >= FORCE_CLOSE_HHMM` 就送強制平倉市價單，才去查回補
    限價單是否已成交——若回補單剛好在兩次 poll 之間成交，會對已平倉部位再送一張
    Buy/Close，行為未經驗證。修復後改成：不論是否過了 deadline，先查
    `get_futopt_order_results()`／`_order_appears_filled()`，已成交就直接
    return，只有『還沒成交』且時間到了才送強制平倉單。

    本檢查在每個函式主體內找「deadline 比較 + 附近有下單/狀態變更動作」的樣式，
    再看同一函式主體裡『狀態查詢』字樣（query_*/get_*_result/is_filled/
    appears_filled/...）最早出現的行號是否早於 deadline 判斷的行號。
    """
    text = "\n".join(lines)
    deadline_rx = re.compile(
        r"if\s+.{0,80}(>=|>)\s*.{0,60}"
        r"(deadline|cutoff|force_close|hhmm|expiry|expire"
        r"|[\"']\d{1,2}:\d{2}[\"'])",
        re.IGNORECASE,
    )
    action_rx = re.compile(
        r"place_\w*order|submit_\w*order|send_\w*order|create_\w*order"
        r"|\.place\(|\.submit\(|force_close|forceclose|status\s*=\s*[\"']",
        re.IGNORECASE,
    )
    status_check_rx = re.compile(
        r"query_\w+|get_\w*(result|status|order)|fetch_\w*(status|state)|is_filled"
        r"|appears_filled|already_(filled|closed|covered|resolved|matched)"
        r"|check_\w*(status|state)|order_results|position_status|order_status",
        re.IGNORECASE,
    )
    func_defs = list(re.finditer(r"\bdef\s+(\w+)\s*\(", text))

    warn_hits: list[tuple[int, str]] = []
    ok_hits: list[tuple[int, str]] = []
    for idx, m in enumerate(func_defs):
        fn_name = m.group(1)
        start = m.end()
        end = func_defs[idx + 1].start() if idx + 1 < len(func_defs) else len(text)
        body = text[start:end]
        if len(body) > 6000:
            body = body[:6000]
        deadline_m = deadline_rx.search(body)
        if not deadline_m:
            continue
        # 只有 deadline 判斷附近（400 字元內）真的觸發下單/狀態變更動作，才算數，
        # 否則像「if bar[-1] >= '08:45'」這種單純過濾條件會被誤判。
        window = body[deadline_m.start(): deadline_m.start() + 400]
        if not action_rx.search(window):
            continue
        deadline_line = lines_upto(text, start + deadline_m.start())
        status_m = status_check_rx.search(body)
        if status_m:
            status_line = lines_upto(text, start + status_m.start())
            if status_line < deadline_line:
                ok_hits.append(
                    (deadline_line, f"def {fn_name}(...) — 狀態查詢 (L{status_line}) 在 deadline 判斷 (L{deadline_line}) 之前，順序正確")
                )
                continue
            warn_hits.append(
                (deadline_line, f"def {fn_name}(...) — deadline 判斷 (L{deadline_line}) 出現在狀態查詢 (L{status_line}) 之前，可能對已解決的情況重複觸發動作")
            )
            continue
        warn_hits.append(
            (deadline_line, f"def {fn_name}(...) — 有 deadline 判斷 (L{deadline_line}) 但函式內找不到狀態查詢字樣，人工確認『情況是否已解決』有沒有在別處先查過")
        )

    if not warn_hits and not ok_hits:
        return CheckResult(
            "deadline_before_status_check",
            "Deadline 觸發動作 vs 狀態查詢的順序（state-check-ordering race）",
            "info",
            "沒找到『deadline 判斷 + 附近有下單/狀態變更動作』的樣式——若這支腳本"
            "不是狀態機式的下單/執行邏輯，此檢查不適用。",
        )
    if warn_hits:
        return CheckResult(
            "deadline_before_status_check",
            "Deadline 觸發動作 vs 狀態查詢的順序（state-check-ordering race）",
            "warn",
            f"{len(warn_hits)} 處 deadline 觸發的動作，寫在（或找不到）狀態查詢之前"
            "——本 repo 已知案例（dayflip_short_order.py 修復前）：先判斷過了強制平倉"
            "時間就送單，才去查部位是否已經自己成交，中間有 race window。逐一確認："
            "deadline 觸發的動作前面，是否已無條件先查過『情況是否已自行解決』。",
            warn_hits[:5],
        )
    return CheckResult(
        "deadline_before_status_check",
        "Deadline 觸發動作 vs 狀態查詢的順序（state-check-ordering race）",
        "ok",
        f"{len(ok_hits)} 個函式的 deadline 觸發動作，前面都先找到狀態查詢字樣——"
        "這只是行號先後的弱訊號，不驗證查詢結果真的被拿去 short-circuit 動作，"
        "仍建議人工確認查詢失敗（exception）時的 fail-safe 方向是否正確。",
        ok_hits[:5],
    )


def check_mean_only_point_estimate(lines: list[str]) -> CheckResult:
    """BUG-8: 只用 mean()/.mean() 判定 edge/顯著性，沒有 median() 或 win_rate 交叉檢查.

    真實案例：reports/research/wma20_bounce_generalize/FINDINGS.md——平均值差異
    +1.50pp、permutation p=0.038（名義上顯著），但中位數差異 p=0.69（不顯著），
    而且 confirmed 組的勝率反而比 not_confirmed 組更低（46.3% vs 47.2%），唯一真的
    live 的分點上中位數與勝率還雙雙反轉。只看平均值的點估計，容易被少數離群交易騙。
    """
    text = "\n".join(lines)
    mean_hits = _find_lines(lines, r"\.mean\(|(?<![\w.])mean\(")
    if not mean_hits:
        return CheckResult(
            "mean_only_point_estimate",
            "只用 mean() 判定 edge，沒有 median()/win_rate 交叉檢查",
            "info",
            "沒找到 mean()/.mean() 用法——此檢查不適用。",
        )
    edge_context_hits = _find_lines(
        lines, r"edge|signific|p_value|p_val\b|\bpassed\b|\brejected\b|graduat|採納|顯著|畢業"
    )
    if not edge_context_hits:
        return CheckResult(
            "mean_only_point_estimate",
            "只用 mean() 判定 edge，沒有 median()/win_rate 交叉檢查",
            "info",
            f"找到 {len(mean_hits)} 處 mean()，但沒找到 edge/顯著性/passed/rejected 這類"
            "判定情境字樣——很可能只是單純統計描述，此檢查判斷不適用；若這支腳本其實"
            "會用 mean() 下判定但用詞不同，本 lint 抓不到，需人工確認。",
        )
    median_hits = _find_lines(lines, r"\.median\(|(?<![\w.])median\(")
    winrate_hits = _find_lines(lines, r"win_rate|hit_rate|勝率|win[_\s-]?ratio")
    if median_hits or winrate_hits:
        return CheckResult(
            "mean_only_point_estimate",
            "只用 mean() 判定 edge，沒有 median()/win_rate 交叉檢查",
            "ok",
            f"找到 mean() {len(mean_hits)} 處，也找到 median()/win_rate 交叉檢查"
            f"（{len(median_hits) + len(winrate_hits)} 處）——仍需人工確認兩者方向"
            "是否一致：本 repo 案例（wma20_bounce_generalize）平均值顯著但中位數不"
            "顯著、confirmed 組勝率反而更低，光是『有算』不代表『沒矛盾』。",
            (median_hits + winrate_hits)[:5],
        )
    return CheckResult(
        "mean_only_point_estimate",
        "只用 mean() 判定 edge，沒有 median()/win_rate 交叉檢查",
        "warn",
        f"找到 {len(mean_hits)} 處 mean() 用在疑似 edge/顯著性判定情境，但沒找到"
        "median()/win_rate/hit_rate 交叉檢查——本 repo 已知案例：mean 顯著"
        "(p=0.038) 但 median 不顯著 (p=0.69)、且『顯著』那組的勝率反而更低"
        "（wma20_bounce_generalize）。純看平均值 point estimate 容易被少數離群"
        "交易騙，建議至少加報 median 與 win_rate/hit_rate 做交叉檢查。",
        mean_hits[:8],
    )


def check_dsr_reference_population(lines: list[str]) -> CheckResult:
    """BUG-9: DSR/Deflated-Sharpe 等多重比較校正，參考族群是否本身就被 regime 灌高.

    真實案例：reports/research/c18acc_trial_count_audit/FINDINGS.md——用 54 個
    sweep artifact 的 Sharpe 離散度當 var_trials，代入 Bailey & Lopez de Prado
    DSR 公式，冠軍 Sharpe=5.27 在任何 N/n 假設下都輕鬆過關（DSR≈1.000）；但參考
    族群裡連 config/research.yaml 明講 rejected 的變體，Sharpe 也一樣落在 4-7
    區間——代表校正的是「整族 trial 共享的多頭 regime beta」，不是「贏過雜訊」的
    技能，DSR 過關不可信。

    **這是全部檢查裡最弱的一條**：純關鍵字比對完全不理解「trial 母體有沒有包含
    rejected 變體、兩者 Sharpe 分布有沒有系統性差異」這種語意，幾乎肯定會
    under-detect；即使判定 OK 也不保證真的乾淨，見上方 docstring 的限制說明。
    """
    hits = _find_lines(
        lines, r"deflated[_\s-]?sharpe|\bDSR\b|probabilistic[_\s-]?sharpe|\bPSR\b|var_trials"
    )
    if not hits:
        return CheckResult(
            "dsr_reference_population",
            "DSR/Deflated-Sharpe 參考族群是否自我灌高",
            "info",
            "沒找到 Deflated Sharpe / DSR / PSR / var_trials 相關字樣——此檢查不適用。",
        )
    manual_check_hits = _find_lines(
        lines,
        r"rejected.*sharpe|sharpe.*rejected|self[_\s-]?inflat|reference[_\s-]?population"
        r"|參考族群|已拒絕.*sharpe|殘差化|beta[_\s-]?resid",
    )
    if manual_check_hits:
        return CheckResult(
            "dsr_reference_population",
            "DSR/Deflated-Sharpe 參考族群是否自我灌高",
            "ok",
            f"找到 DSR/Deflated-Sharpe 相關字樣（{len(hits)} 處），也找到疑似『已核對"
            f"參考族群是否自我灌高』的字樣（{len(manual_check_hits)} 處）——這只是關鍵字"
            "層級的弱訊號，務必人工確認：var_trials 的樣本裡有沒有包含被拒絕(rejected)"
            "的變體、兩者 Sharpe 分布有沒有系統性差異，見 checklist。",
            manual_check_hits[:5],
        )
    return CheckResult(
        "dsr_reference_population",
        "DSR/Deflated-Sharpe 參考族群是否自我灌高",
        "warn",
        f"找到 DSR/Deflated-Sharpe/PSR/var_trials 相關字樣（{len(hits)} 處），但沒找到"
        "任何『核對參考族群本身是否自我灌高』的字樣——本檢查已知很弱（純字面比對抓不到"
        "『有沒有把 rejected 變體也算進 var_trials、且比較過兩者 Sharpe 分布』這種語意），"
        "但這正是本 repo 已證實的真案例（c18acc_trial_count_audit）：DSR 用的參考族群"
        "連被拒絕的變體 Sharpe 都跟冠軍同樣落在 4-7 區間，代表 var_trials 被整族 trial"
        "共享的 regime beta 灌高，DSR≈1.000『輕鬆過關』不代表安心。人工必查：var_trials"
        "的樣本裡有沒有包含 rejected 變體、兩者 Sharpe 分布是否有系統性差異，見 checklist。",
        hits[:5],
    )


CHECKS = [
    check_session_end_accounting,
    check_reconciliation_assert,
    check_causal_shift,
    check_significance_testing,
    check_favorable_price_clamp,
    check_auto_verdict_labels,
    check_deadline_before_status_check,
    check_mean_only_point_estimate,
    check_dsr_reference_population,
]


def lint_file(path: Path) -> list[CheckResult]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    return [check(lines) for check in CHECKS]


def print_report(path: Path, results: list[CheckResult]) -> None:
    print(f"\n=== {path} ===")
    for r in results:
        print(f"{STATUS_LABEL[r.status]} {r.title}")
        print(f"           {r.detail}")
        for line_no, snippet in r.matched_lines:
            print(f"             L{line_no}: {snippet[:100]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="+", help="要掃描的回測腳本路徑（可多個）")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="有任何 warn/missing 就以 exit code 1 結束（預設 0，不建議當 CI gate）",
    )
    args = parser.parse_args(argv)

    any_warn = False
    for raw_path in args.paths:
        path = Path(raw_path)
        if not path.exists():
            print(f"[ERROR] 找不到檔案: {path}", file=sys.stderr)
            any_warn = True
            continue
        results = lint_file(path)
        print_report(path, results)
        if any(r.status in ("warn", "missing") for r in results):
            any_warn = True

    print(
        "\n注意：這是靜態關鍵字掃描，偵測能力有限（不理解程式語意、抓不到"
        "fold-aggregate 後見之明等語意層 bug），不能取代人工／agent review。"
        "完整檢查清單見 docs/research-integrity-checklist.md。"
    )
    return 1 if (args.strict and any_warn) else 0


if __name__ == "__main__":
    raise SystemExit(main())
