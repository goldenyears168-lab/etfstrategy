"""ABC v3+F1 · shape validation framework (RP-6 方法論基礎設施).

這個模組不驗證任何特定交易假說，而是提供一套可重複使用的「驗證紀律」，
供 RP-5（多訊號組合）與未來任何新 shape 研究直接引用。核心組件：

1. ``_split_dates_three_way``  — 三段式資料切分（IS / Validation / Holdout），
   時間先後排列、互不重疊、無 lookahead。擴充自
   ``abc_v3_f1_entry_structure_sweep._split_dates_is_oos``。
   紀律：Holdout 只能跑一次，跑完即封存結果，不可回頭修改 shape 定義。
2. ``apply_fdr_correction`` — 多重檢定校正（Benjamini-Hochberg step-up FDR，
   另附 Bonferroni 選項）。同批測試 N 個候選 shape 時，不能單看「哪個均報酬
   最高」，必須先把 N 攤在檯面上做校正。
3. ``min_n_for_power`` — 給定目標效果量與報酬變異數，估算在指定檢定力
   （預設 80%）與信心水準（預設 95%）下能偵測到該效果的最低樣本數。
4. ``walk_forward_recheck`` — 對已通過 Holdout 的 shape 定期重驗，連續兩次
   低於門檻自動標記 ``decayed``（黏性標記：一旦 decayed 不自動復活，須重走
   完整驗證流程）。

輔助組件（RP-5 可直接使用）：

- ``one_sample_t_test`` / ``welch_t_test`` / ``p_value_from_correlation``：
  純 stdlib 實作的 t 檢定（t 分布 CDF 用不完全 Beta 函數連分式展開，
  不依賴 scipy）。
- ``evaluate_shape_candidate``：單一候選 shape 的組合把關（最低 n、
  多重檢定校正後 p-value、目標均報酬三道門檻），輸出可稽核的 verdict。
- ``sigma_from_tail_fraction``：只有 summary 統計（均值 + 尾端占比）時
  反推報酬標準差的粗略常態近似 fallback。
- ``load_shape_registry`` / ``validate_shape_registry``：
  ``config/abc_v3_f1_shape_registry.yaml`` 的載入與 schema 驗證。

典型 RP-5 用法::

    from research.backtest.abc_v3_f1_shape_validation_framework import (
        _split_dates_three_way, apply_fdr_correction, min_n_for_power,
        walk_forward_recheck, evaluate_shape_candidate,
    )

    is_d, val_d, hold_d = _split_dates_three_way(all_dates, is_ratio=0.5, val_ratio=0.25)
    # 1) 在 is_d 上探索候選 → 2) 在 val_d 上對全部 N 個候選做 t 檢定
    fdr = apply_fdr_correction([c["p"] for c in cands], q=0.10, labels=[c["id"] for c in cands])
    # 3) 只有 FDR 存活者才允許動用 hold_d（一次性）
    # 4) 上線後每季 walk_forward_recheck(...)
"""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# t 分布數學基礎（純 stdlib，不依賴 scipy）
# ---------------------------------------------------------------------------

_MAX_BETACF_ITER = 300
_BETACF_EPS = 3e-12
_FPMIN = 1e-300


def _betacf(a: float, b: float, x: float) -> float:
    """不完全 Beta 函數的連分式展開（Numerical Recipes 形式）。"""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _MAX_BETACF_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _BETACF_EPS:
            break
    return h


def _incomplete_beta(a: float, b: float, x: float) -> float:
    """正則化不完全 Beta 函數 I_x(a, b)。"""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def _t_sf(t: float, df: float) -> float:
    """Student-t 生存函數 P(T > t)。"""
    if df <= 0:
        raise ValueError(f"df must be positive, got {df}")
    if math.isinf(t):
        return 0.0 if t > 0 else 1.0
    x = df / (df + t * t)
    p = 0.5 * _incomplete_beta(df / 2.0, 0.5, x)
    return p if t >= 0 else 1.0 - p


def t_two_sided_p(t: float, df: float) -> float:
    """雙尾 t 檢定 p-value。"""
    return min(1.0, 2.0 * _t_sf(abs(t), df))


def _t_quantile(prob: float, df: float) -> float:
    """t 分布分位數 t_{prob, df}（P(T <= t) = prob），二分法求解。"""
    if not 0.0 < prob < 1.0:
        raise ValueError(f"prob must be in (0, 1), got {prob}")
    if prob == 0.5:
        return 0.0
    if prob < 0.5:
        return -_t_quantile(1.0 - prob, df)
    lo, hi = 0.0, 1e3
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if 1.0 - _t_sf(mid, df) < prob:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10:
            break
    return (lo + hi) / 2.0


def _z_quantile(prob: float) -> float:
    return NormalDist().inv_cdf(prob)


# ---------------------------------------------------------------------------
# t 檢定（供 Validation / Holdout 階段使用）
# ---------------------------------------------------------------------------


def one_sample_t_test(values: Sequence[float], mu0: float) -> dict[str, Any]:
    """單樣本雙尾 t 檢定：樣本均值 vs 已知 baseline mu0。

    回傳 ``{"n", "mean", "std", "t", "df", "p_two_sided"}``；n<2 或零變異時
    p 記為 None（無法檢定）。
    """
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "std": None, "t": None, "df": None, "p_two_sided": None}
    mean = sum(values) / n
    if n < 2:
        return {"n": n, "mean": mean, "std": None, "t": None, "df": None, "p_two_sided": None}
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    std = math.sqrt(var)
    if std < 1e-12:
        return {"n": n, "mean": mean, "std": 0.0, "t": None, "df": n - 1, "p_two_sided": None}
    t = (mean - mu0) / (std / math.sqrt(n))
    return {
        "n": n,
        "mean": mean,
        "std": std,
        "t": t,
        "df": n - 1,
        "p_two_sided": t_two_sided_p(t, n - 1),
    }


def welch_t_test(a: Sequence[float], b: Sequence[float]) -> dict[str, Any]:
    """Welch 兩樣本雙尾 t 檢定（不假設等變異）：候選 shape vs baseline 樣本。"""
    n1, n2 = len(a), len(b)
    if n1 < 2 or n2 < 2:
        return {"n1": n1, "n2": n2, "t": None, "df": None, "p_two_sided": None}
    m1, m2 = sum(a) / n1, sum(b) / n2
    v1 = sum((v - m1) ** 2 for v in a) / (n1 - 1)
    v2 = sum((v - m2) ** 2 for v in b) / (n2 - 1)
    se2 = v1 / n1 + v2 / n2
    if se2 < 1e-24:
        return {"n1": n1, "n2": n2, "mean1": m1, "mean2": m2, "t": None, "df": None, "p_two_sided": None}
    t = (m1 - m2) / math.sqrt(se2)
    df = se2**2 / ((v1 / n1) ** 2 / (n1 - 1) + (v2 / n2) ** 2 / (n2 - 1))
    return {
        "n1": n1,
        "n2": n2,
        "mean1": m1,
        "mean2": m2,
        "t": t,
        "df": df,
        "p_two_sided": t_two_sided_p(t, df),
    }


def p_value_from_correlation(r: float, n: int) -> float | None:
    """Pearson/Spearman 相關係數的雙尾 p-value（t 近似，df=n-2）。

    用於 H-ENTRY-NORM-1 這類「同時比較多個因子相關性」的多重檢定場景。
    """
    if n < 3 or not -1.0 <= r <= 1.0:
        return None
    if abs(r) >= 1.0:
        return 0.0
    t = r * math.sqrt((n - 2) / (1.0 - r * r))
    return t_two_sided_p(t, n - 2)


# ---------------------------------------------------------------------------
# 1) 三段式資料切分（IS / Validation / Holdout）
# ---------------------------------------------------------------------------


def _split_dates_three_way(
    dates: Iterable[str], *, is_ratio: float = 0.5, val_ratio: float = 0.25
) -> tuple[list[str], list[str], list[str]]:
    """時間先後三段切分：IS / Validation / Holdout，互不重疊、無 lookahead。

    - 輸入日期（ISO 字串）會先去重並按時間排序，確保三段嚴格依時間先後：
      max(IS) < min(Validation) < ... < max(Holdout)。
    - Holdout 恆為時間最晚的一段（絕不能重複使用：跑一次即封存）。
    - 邊界行為：n=0 → 三段皆空；n=1 → 全給 IS；n=2 → IS 1 天 + Holdout 1 天；
      n>=3 → 三段各至少 1 天。
    """
    if not (0.0 < is_ratio < 1.0):
        raise ValueError(f"is_ratio must be in (0, 1), got {is_ratio}")
    if not (0.0 < val_ratio < 1.0):
        raise ValueError(f"val_ratio must be in (0, 1), got {val_ratio}")
    if is_ratio + val_ratio >= 1.0:
        raise ValueError(
            f"is_ratio + val_ratio must be < 1 (holdout must be non-empty share), "
            f"got {is_ratio} + {val_ratio}"
        )
    ordered = sorted(set(dates))
    n = len(ordered)
    if n == 0:
        return [], [], []
    if n == 1:
        return ordered, [], []
    if n == 2:
        return ordered[:1], [], ordered[1:]
    n_is = int(round(n * is_ratio))
    n_val = int(round(n * val_ratio))
    n_is = max(1, min(n_is, n - 2))
    n_val = max(1, min(n_val, n - 1 - n_is))
    return ordered[:n_is], ordered[n_is : n_is + n_val], ordered[n_is + n_val :]


# ---------------------------------------------------------------------------
# 2) 多重檢定校正（Benjamini-Hochberg FDR / Bonferroni）
# ---------------------------------------------------------------------------


def apply_fdr_correction(
    p_values: Sequence[float],
    *,
    q: float = 0.10,
    method: str = "bh",
    labels: Sequence[str] | None = None,
) -> dict[str, Any]:
    """對同一批 N 個候選的 p-value 做多重檢定校正。

    - ``method="bh"``：Benjamini-Hochberg step-up。回傳的 ``p_adjusted`` 為
      BH 調整後 p-value（單調化的 min(1, p_(j)·m/j)），``significant`` 為
      p_adjusted <= q（等價於經典「最大 k 使 p_(k) <= q·k/m」判準）。
    - ``method="bonferroni"``：p_adjusted = min(1, p·m)，significant 為
      p_adjusted <= q。更保守，適合 N 小或作 MVP 用。
    - p-value 為 None 的候選（無法檢定，例如 n<2）一律判 not significant，
      但仍計入校正基準 m —— 「本輪總共測試了幾個候選」必須完整申報，
      不能只把算得出 p 的拿來校正。

    回傳 ``{"method", "q", "m", "n_significant", "results": [...]}``，
    ``results`` 保持輸入順序，逐項含
    ``{"label", "p", "p_adjusted", "rank", "significant"}``。
    """
    if method not in ("bh", "bonferroni"):
        raise ValueError(f"method must be 'bh' or 'bonferroni', got {method!r}")
    if not (0.0 < q < 1.0):
        raise ValueError(f"q must be in (0, 1), got {q}")
    m = len(p_values)
    if labels is not None and len(labels) != m:
        raise ValueError(f"labels length {len(labels)} != p_values length {m}")
    for p in p_values:
        if p is not None and not (0.0 <= p <= 1.0):
            raise ValueError(f"p-value out of [0, 1]: {p}")
    labels = list(labels) if labels is not None else [f"cand_{i}" for i in range(m)]

    results: list[dict[str, Any]] = [
        {"label": labels[i], "p": p_values[i], "p_adjusted": None, "rank": None, "significant": False}
        for i in range(m)
    ]
    testable = [(i, float(p_values[i])) for i in range(m) if p_values[i] is not None]

    if method == "bonferroni":
        for i, p in testable:
            adj = min(1.0, p * m)
            results[i]["p_adjusted"] = adj
            results[i]["significant"] = adj <= q
    else:  # bh
        testable.sort(key=lambda ip: ip[1])
        k = len(testable)
        adj_sorted = [0.0] * k
        running_min = 1.0
        for j in range(k - 1, -1, -1):  # step-up：由大到小做單調化
            raw_adj = testable[j][1] * m / (j + 1)
            running_min = min(running_min, raw_adj)
            adj_sorted[j] = min(1.0, running_min)
        for j, (i, p) in enumerate(testable):
            results[i]["p_adjusted"] = adj_sorted[j]
            results[i]["rank"] = j + 1
            results[i]["significant"] = adj_sorted[j] <= q

    return {
        "method": method,
        "q": q,
        "m": m,
        "n_significant": sum(1 for r in results if r["significant"]),
        "results": results,
    }


# ---------------------------------------------------------------------------
# 3) 最低樣本量的檢定力計算
# ---------------------------------------------------------------------------


def achieved_power(
    n: int,
    effect_pp: float,
    sigma_pp: float,
    *,
    alpha: float = 0.05,
    two_sided: bool = True,
    design: str = "one_sample",
) -> float:
    """給定樣本數 n，估算偵測 effect_pp 的檢定力（常態近似 + t 臨界值）。

    ``design="one_sample"``：shape 均報酬 vs 已知 baseline。
    ``design="two_sample"``：shape vs baseline 兩組各 n（等組數 Welch 近似）。
    """
    if design not in ("one_sample", "two_sample"):
        raise ValueError(f"design must be 'one_sample' or 'two_sample', got {design!r}")
    if n < 2:
        return 0.0
    if design == "one_sample":
        df = n - 1
        ncp = abs(effect_pp) / (sigma_pp / math.sqrt(n))
    else:
        df = 2 * n - 2
        ncp = abs(effect_pp) / (sigma_pp * math.sqrt(2.0 / n))
    a = alpha / 2.0 if two_sided else alpha
    t_crit = _t_quantile(1.0 - a, df)
    # 常態位移近似：T ≈ N(ncp, 1)
    return NormalDist().cdf(ncp - t_crit)


def min_n_for_power(
    effect_pp: float,
    sigma_pp: float,
    *,
    power: float = 0.80,
    alpha: float = 0.05,
    two_sided: bool = True,
    design: str = "one_sample",
    max_n: int = 100_000,
) -> int:
    """偵測 effect_pp（百分點）所需的最低樣本數（80% power / 95% 信心預設）。

    以常態近似公式 n0 = ((z_{1-α/2}+z_{power})·σ/δ)² 起步，再逐一遞增 n 直到
    ``achieved_power`` >= power（把 t 臨界值放回去，比純 z 公式略保守）。

    ``design="two_sample"`` 回傳的是**每組**所需 n。
    效果量必須為正（無效果就沒有「偵測」可言）。
    """
    if effect_pp <= 0:
        raise ValueError(f"effect_pp must be positive, got {effect_pp}")
    if sigma_pp <= 0:
        raise ValueError(f"sigma_pp must be positive, got {sigma_pp}")
    if not (0.0 < power < 1.0):
        raise ValueError(f"power must be in (0, 1), got {power}")
    a = alpha / 2.0 if two_sided else alpha
    za, zb = _z_quantile(1.0 - a), _z_quantile(power)
    ratio = sigma_pp / effect_pp
    n0 = ((za + zb) * ratio) ** 2
    if design == "two_sample":
        n0 *= 2.0
    n = max(2, int(math.floor(n0)) - 2)
    while n <= max_n:
        if achieved_power(n, effect_pp, sigma_pp, alpha=alpha, two_sided=two_sided, design=design) >= power:
            return n
        n += 1
    raise ValueError(f"required n exceeds max_n={max_n} (effect too small vs sigma)")


def sigma_from_tail_fraction(mean_pct: float, cut_pct: float, frac_ge_cut: float) -> float | None:
    """由「均值 + P(ret >= cut) 占比」反推標準差的常態近似 fallback。

    僅在拿不到逐筆報酬、只有 summary 統計時使用；實際報酬多為厚尾，
    此估計偏低（低估 σ → 低估最低 n），使用時應搭配敏感度區間。
    """
    if not (0.0 < frac_ge_cut < 1.0):
        return None
    z = _z_quantile(1.0 - frac_ge_cut)
    if abs(z) < 1e-9:
        return None
    sigma = (cut_pct - mean_pct) / z
    return sigma if sigma > 0 else None


# ---------------------------------------------------------------------------
# 4) Walk-forward 再驗證
# ---------------------------------------------------------------------------


def walk_forward_recheck(
    checks: Iterable[dict[str, Any]],
    *,
    threshold_pct: float,
    min_n: int = 1,
    fail_streak_to_decay: int = 2,
) -> dict[str, Any]:
    """定期重驗已通過 Holdout 的 shape，連續失敗達門檻即標記 decayed。

    ``checks``：每次檢查一筆 ``{"check_date": ISO 日期, "n": int,
    "mean_ret_pct": float}``（自動按 check_date 排序）。

    判定規則：
    - 單次檢查通過 = ``n >= min_n`` 且 ``mean_ret_pct >= threshold_pct``。
      樣本不足視同失敗（保守：拿不出證據就不能算通過）。
    - 連續 ``fail_streak_to_decay`` 次失敗 → ``status="decayed"``（黏性：
      一旦 decayed 即應從組合移除，之後即使回升也不自動復活，須重走
      IS/Validation/Holdout 完整流程重新掛牌）。
    - 目前失敗 streak > 0 但未達門檻 → ``status="warning"``。
    - 無任何檢查紀錄 → ``status="unchecked"``。

    回傳 ``{"status", "fail_streak", "decayed_on", "last_check_date",
    "n_checks", "checks": [逐筆加註 passed/fail_streak]}``。
    """
    if fail_streak_to_decay < 1:
        raise ValueError(f"fail_streak_to_decay must be >= 1, got {fail_streak_to_decay}")
    ordered = sorted(checks, key=lambda c: str(c.get("check_date") or ""))
    annotated: list[dict[str, Any]] = []
    streak = 0
    decayed_on: str | None = None
    for c in ordered:
        n = int(c.get("n") or 0)
        mean = c.get("mean_ret_pct")
        passed = n >= min_n and mean is not None and float(mean) >= threshold_pct
        streak = 0 if passed else streak + 1
        if streak >= fail_streak_to_decay and decayed_on is None:
            decayed_on = str(c.get("check_date"))
        annotated.append({**c, "passed": passed, "fail_streak": streak})
    if decayed_on is not None:
        status = "decayed"
    elif not annotated:
        status = "unchecked"
    elif streak > 0:
        status = "warning"
    else:
        status = "active"
    return {
        "status": status,
        "fail_streak": streak,
        "decayed_on": decayed_on,
        "last_check_date": str(ordered[-1].get("check_date")) if ordered else None,
        "n_checks": len(annotated),
        "checks": annotated,
    }


# ---------------------------------------------------------------------------
# 候選 shape 組合把關（三道門檻：最低 n、校正後 p、目標均報酬）
# ---------------------------------------------------------------------------


def evaluate_shape_candidate(
    *,
    shape_id: str,
    n: int,
    mean_ret_pct: float,
    baseline_mean_pct: float,
    sigma_pct: float,
    n_candidates_tested: int = 1,
    target_pct: float = 8.0,
    alpha: float = 0.05,
    power: float = 0.80,
    p_value: float | None = None,
) -> dict[str, Any]:
    """單一候選 shape 的驗證判定（summary 統計版，供回溯檢驗與快篩用）。

    三道門檻，全過才回 ``passed=True``：

    1. **最低 n**：以觀測效果量（mean − baseline）計算 ``min_n_for_power``，
       n 不足 → ``insufficient_n``。同時回報以「目標效果量」
       （target − baseline）計的最低 n 供設計參考。
    2. **多重檢定校正後 p-value**：未提供 ``p_value`` 時以
       t = (mean − baseline)/(σ/√n)、df=n−1 估計；再乘上
       ``n_candidates_tested`` 做 Bonferroni 校正（單一候選快篩用保守
       校正；整批候選請改用 ``apply_fdr_correction`` 做 BH）。
       校正後 p > alpha → ``not_significant_after_correction``。
    3. **目標均報酬**：mean < target → ``below_target``。

    注意：這是快篩/回溯工具。正式流程仍須逐筆報酬 + 三段切分 +
    整批 FDR 校正 + Holdout 一次性驗證。
    """
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if n_candidates_tested < 1:
        raise ValueError(f"n_candidates_tested must be >= 1, got {n_candidates_tested}")
    if sigma_pct <= 0:
        raise ValueError(f"sigma_pct must be positive, got {sigma_pct}")
    reasons: list[str] = []
    effect_observed = mean_ret_pct - baseline_mean_pct
    effect_target = target_pct - baseline_mean_pct

    def _min_n(effect_pp: float) -> int | None:
        """min_n_for_power；效果量小到 n 需求爆表（> max_n）時回 None。"""
        try:
            return min_n_for_power(effect_pp, sigma_pct, power=power, alpha=alpha)
        except ValueError:
            return None

    min_n_observed = _min_n(effect_observed) if effect_observed > 0 else None
    min_n_target = _min_n(effect_target) if effect_target > 0 else None

    if effect_observed <= 0:
        reasons.append("no_positive_effect")
    elif min_n_observed is None or n < min_n_observed:
        # min_n_observed=None 表示以觀測效果量根本達不到檢定力（需求 n 爆表）
        reasons.append("insufficient_n")

    p_raw = p_value
    if p_raw is None and n >= 2 and sigma_pct > 0:
        t = effect_observed / (sigma_pct / math.sqrt(n))
        p_raw = t_two_sided_p(t, n - 1)
    p_adjusted = min(1.0, p_raw * n_candidates_tested) if p_raw is not None else None
    if p_adjusted is None:
        reasons.append("p_value_unavailable")
    elif p_adjusted > alpha:
        reasons.append("not_significant_after_correction")

    if mean_ret_pct < target_pct:
        reasons.append("below_target")

    passed = not reasons
    return {
        "shape_id": shape_id,
        "passed": passed,
        "status_suggestion": "validation_candidate" if passed else "not_passed",
        "reasons": reasons,
        "n": n,
        "mean_ret_pct": mean_ret_pct,
        "baseline_mean_pct": baseline_mean_pct,
        "effect_observed_pp": round(effect_observed, 4),
        "effect_target_pp": round(effect_target, 4),
        "sigma_pct": sigma_pct,
        "min_n_observed_effect": min_n_observed,
        "min_n_target_effect": min_n_target,
        "n_candidates_tested": n_candidates_tested,
        "p_raw": p_raw,
        "p_adjusted": p_adjusted,
        "alpha": alpha,
        "target_pct": target_pct,
    }


# ---------------------------------------------------------------------------
# Shape registry（config/abc_v3_f1_shape_registry.yaml）
# ---------------------------------------------------------------------------

REGISTRY_SCHEMA_ID = "abc_v3_f1_shape_registry-v1"
VALID_SHAPE_STATUSES = ("discovery", "validated", "holdout_passed", "decayed", "rejected")
_REQUIRED_SHAPE_KEYS = ("id", "definition", "stages", "status")
_STAGE_KEYS = ("discovery", "validation", "holdout")


def validate_shape_registry(payload: dict[str, Any]) -> list[str]:
    """檢查 registry dict 是否符合 schema，回傳錯誤訊息清單（空 = 合格）。"""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["registry payload must be a mapping"]
    if payload.get("schema") != REGISTRY_SCHEMA_ID:
        errors.append(f"schema must be {REGISTRY_SCHEMA_ID!r}, got {payload.get('schema')!r}")
    shapes = payload.get("shapes")
    if not isinstance(shapes, list):
        errors.append("shapes must be a list")
        return errors
    seen_ids: set[str] = set()
    for idx, shape in enumerate(shapes):
        where = f"shapes[{idx}]"
        if not isinstance(shape, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for key in _REQUIRED_SHAPE_KEYS:
            if key not in shape:
                errors.append(f"{where}: missing required key {key!r}")
        sid = shape.get("id")
        if isinstance(sid, str):
            if sid in seen_ids:
                errors.append(f"{where}: duplicate id {sid!r}")
            seen_ids.add(sid)
        status = shape.get("status")
        if status is not None and status not in VALID_SHAPE_STATUSES:
            errors.append(
                f"{where}: invalid status {status!r} (must be one of {VALID_SHAPE_STATUSES})"
            )
        stages = shape.get("stages")
        if isinstance(stages, dict):
            for sk in _STAGE_KEYS:
                if sk not in stages:
                    errors.append(f"{where}.stages: missing stage {sk!r}")
        elif stages is not None:
            errors.append(f"{where}.stages: must be a mapping")
        holdout = (stages or {}).get("holdout") if isinstance(stages, dict) else None
        if status == "holdout_passed" and isinstance(holdout, dict):
            if not holdout.get("run_once_done"):
                errors.append(
                    f"{where}: status=holdout_passed but holdout.run_once_done is not true"
                )
    return errors


def load_shape_registry(path: str) -> dict[str, Any]:
    """載入並驗證 shape registry YAML；schema 不符時 raise ValueError。"""
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)
    errors = validate_shape_registry(payload)
    if errors:
        raise ValueError(f"invalid shape registry {path}: " + "; ".join(errors))
    return payload


# ---------------------------------------------------------------------------
# 回溯檢驗（RP-6 驗收：把框架套回本輪既有發現，確認判定為「尚未通過」）
# ---------------------------------------------------------------------------

RETROCHECK_MULTIFACTOR_REPORTS = (
    # (label, path)：H-ENTRY-MF-1 兩個窗口的 overlay 掃描 summary
    ("3m_2026-04-01..2026-07-07", "reports/research/rrg/20260710_abc_v3_f1_entry_multifactor_tp_hold5d.json"),
    ("10m_2025-09-01..2026-07-01", "reports/research/rrg/20260710_abc_v3_f1_entry_multifactor_tp_hold5d_10m.json"),
)
RETROCHECK_FACTOR_SCAN_REPORT = (
    # H-ENTRY-NORM-1：12 個因子變體同批比較相關性
    "reports/research/rrg/20260710_abc_v3_f1_entry_factor_scan_hold5d.json"
)
RETROCHECK_SIGMA_GRID_PP = (8.0, 10.0, 12.0)  # summary-only 報告無逐筆報酬，σ 用敏感度區間


def retrocheck_prior_findings(root: str = ".") -> dict[str, Any]:
    """把驗證框架回溯套用到 H-ENTRY-MF-1 / H-ENTRY-NORM-1 的既有 summary 結果。

    - H-ENTRY-MF-1（overlay 掃描）：對每個窗口的最佳 overlay ``W3 MV>100``
      跑 ``evaluate_shape_candidate``（Bonferroni × 該窗口實際測試的 overlay
      數）。報告只有 summary 統計、無逐筆報酬，σ 以
      ``RETROCHECK_SIGMA_GRID_PP`` 做敏感度，另附 ``sigma_from_tail_fraction``
      的常態近似估計供對照。
    - H-ENTRY-NORM-1（12 因子變體）：以 ``p_value_from_correlation`` 取得
      每個變體的 p-value，整批做 BH FDR（q=0.10）與 Bonferroni 對照。
    """
    import json
    import os

    out: dict[str, Any] = {
        "schema": "abc_v3_f1_shape_validation_retrocheck-v1",
        "sigma_grid_pp": list(RETROCHECK_SIGMA_GRID_PP),
        "multifactor": [],
        "factor_scan": None,
    }

    # --- H-ENTRY-MF-1：W3 MV>100 overlay（兩個窗口） -----------------------
    for label, rel in RETROCHECK_MULTIFACTOR_REPORTS:
        payload = json.load(open(os.path.join(root, rel), encoding="utf-8"))
        base = payload["base_gate_cell"]
        # 校正基準 m = 申報的 overlay 候選總數（含 skipped——宣告過就要計入；
        # 排除 overlay_id="none" 的 base gate 對照列，它是 baseline 不是候選）
        cells = [c for c in payload.get("cells") or [] if c.get("overlay_id") != "none"]
        m = len(cells)
        best = next(
            c for c in cells if c.get("overlay_id") == "w3_improving" and not c.get("skipped")
        )
        sigma_tail = sigma_from_tail_fraction(
            float(best["tp_only_mean_pct"]), 8.0, float(best["pct_ge_8"]) / 100.0
        )
        sigma_tail_base = sigma_from_tail_fraction(
            float(base["tp_only_mean_pct"]), 8.0, float(base["pct_ge_8"]) / 100.0
        )
        verdicts = []
        for sigma in RETROCHECK_SIGMA_GRID_PP:
            verdicts.append(
                evaluate_shape_candidate(
                    shape_id=f"w3_mv_gt_100@{label}",
                    n=int(best["n"]),
                    mean_ret_pct=float(best["tp_only_mean_pct"]),
                    baseline_mean_pct=float(base["tp_only_mean_pct"]),
                    sigma_pct=sigma,
                    n_candidates_tested=m,
                    target_pct=float(payload.get("target_pct") or 8.0),
                )
            )
        out["multifactor"].append(
            {
                "window": label,
                "source": rel,
                "n_overlay_candidates_tested": m,
                "baseline": {"n": base["n"], "tp_only_mean_pct": base["tp_only_mean_pct"]},
                "best_overlay": {
                    "overlay_id": best["overlay_id"],
                    "overlay_label": best["overlay_label"],
                    "n": best["n"],
                    "tp_only_mean_pct": best["tp_only_mean_pct"],
                    "pct_ge_8": best["pct_ge_8"],
                },
                "sigma_tail_estimate_pp": sigma_tail,
                "sigma_tail_estimate_baseline_pp": sigma_tail_base,
                "verdicts_by_sigma": verdicts,
                "framework_verdict": (
                    "not_passed" if all(not v["passed"] for v in verdicts) else "mixed"
                ),
            }
        )

    # --- H-ENTRY-NORM-1：12 個因子變體的相關性多重檢定 --------------------
    payload = json.load(open(os.path.join(root, RETROCHECK_FACTOR_SCAN_REPORT), encoding="utf-8"))
    rows = payload["factor_scan"]
    labels = [r["factor"] for r in rows]
    fdr_by_measure: dict[str, Any] = {}
    for measure in ("pearson_r", "spearman_rho"):
        pvals = [p_value_from_correlation(float(r[measure]), int(r["n"])) for r in rows]
        fdr_by_measure[measure] = {
            "bh_q10": apply_fdr_correction(pvals, q=0.10, method="bh", labels=labels),
            "bonferroni_q05": apply_fdr_correction(pvals, q=0.05, method="bonferroni", labels=labels),
        }
    out["factor_scan"] = {
        "source": RETROCHECK_FACTOR_SCAN_REPORT,
        "n_factor_variants_tested": len(rows),
        "by_measure": fdr_by_measure,
        "framework_verdict": (
            "not_passed"
            if all(
                blk["n_significant"] == 0
                for mm in fdr_by_measure.values()
                for blk in mm.values()
            )
            else "mixed"
        ),
    }

    # --- 設計參考：偵測「8% vs baseline ~3%」需要的最低 n ------------------
    out["min_n_reference_8_vs_3pct"] = {
        f"sigma_{int(s)}pp": min_n_for_power(5.0, s) for s in RETROCHECK_SIGMA_GRID_PP
    }
    return out


if __name__ == "__main__":  # pragma: no cover — 回溯檢驗一次性入口
    import json
    import sys

    root = sys.argv[1] if len(sys.argv) > 1 else "."
    result = retrocheck_prior_findings(root)
    out_path = f"{root}/reports/research/rrg/20260710_abc_v3_f1_shape_validation_retrocheck.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2)
    print(f"wrote {out_path}")
