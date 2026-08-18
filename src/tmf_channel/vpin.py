"""VPIN — Volume-Synchronized Probability of Informed Trading。

Easley, López de Prado & O'Hara (2012)《Flow Toxicity and Liquidity in a
High-Frequency World》RFS 25(5)。作者的核心主張：做市商真正需要的不是「價格
往哪走」的預測，而是**「此刻的成交流有多毒」**——當知情單佔比升高時，被動報價
的期望損益轉負，正確的動作是**退出市場**而不是調整方向。

與本專案先前試過的 tick-rule OFI 是不同的建構，差別在三處（都照原文）：
  1. **成交量時鐘**：不按時間分桶，按等量成交分桶。作者的論點是資訊到達與
     成交量同步、與時鐘不同步，時間分桶會在活躍時段稀釋訊號。
  2. **Bulk volume classification**：不用 tick rule 逐筆分買賣，而是用該桶價格
     變動除以其標準差、過標準常態 CDF，把整桶量按比例拆成買方／賣方發動。
     這對逐筆分類誤差穩健得多。
  3. **取絕對值後再平均**：VPIN 不分方向，衡量的是失衡的**幅度**——這正是
     「毒性」而非「方向」的意思。先前那個 signed OFI 濾網量的是方向。

VPIN = (1/n) · Σ |V_buy − V_sell| / V   ，取值 [0, 1]

本模組只做計算，不做任何交易決策。是否可用必須先過三段不重疊窗口 + DSR，
理由見這個 repo 過去七次樣本內漂亮、樣本外反轉的紀錄。
"""

from __future__ import annotations

import math
import statistics as st

#: 作者原文用「一天成交量的 1/50」當桶大小；台指日盤量能差異大，
#: 這裡改成由呼叫端依當日總量給定，預設 1/50 與原文一致。
DEFAULT_BUCKETS_PER_DAY = 50
#: 滾動幾個桶算一個 VPIN 值（原文 n=50）
DEFAULT_WINDOW = 50


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def volume_buckets(px: list[float], vol: list[float], bucket_size: float
                   ) -> list[tuple[float, float]]:
    """把逐筆切成等量桶，回傳每桶的 (價格變動, 桶量)。

    價格變動取桶內最後一筆減前一桶最後一筆（成交量時鐘下的報酬）。
    最後一個不滿的桶丟掉——半個桶的失衡沒有可比性。
    """
    out: list[tuple[float, float]] = []
    acc = 0.0
    last_px = px[0] if px else 0.0
    start_px = last_px
    for p, v in zip(px, vol):
        acc += float(v or 0.0)
        last_px = p
        if acc >= bucket_size:
            out.append((last_px - start_px, acc))
            start_px = last_px
            acc = 0.0
    return out


def vpin_series(px: list[float], vol: list[float], *,
                buckets_per_day: int = DEFAULT_BUCKETS_PER_DAY,
                window: int = DEFAULT_WINDOW) -> list[float | None]:
    """每個成交量桶對應一個 VPIN 值（前 ``window`` 個桶為 None）。

    回傳長度等於桶數，與 ``volume_buckets`` 對齊。
    """
    total = sum(float(v or 0.0) for v in vol)
    if total <= 0 or len(px) < 10:
        return []
    bucket_size = total / max(1, buckets_per_day)
    buckets = volume_buckets(px, vol, bucket_size)
    if len(buckets) < 2:
        return []
    dps = [d for d, _ in buckets]
    sd = st.pstdev(dps) if len(dps) > 1 else 0.0
    if sd <= 0:
        return [None] * len(buckets)

    imbalances: list[float] = []
    for dp, v in buckets:
        z = _norm_cdf(dp / sd)
        v_buy = v * z
        v_sell = v - v_buy
        imbalances.append(abs(v_buy - v_sell) / v if v > 0 else 0.0)

    out: list[float | None] = []
    for i in range(len(imbalances)):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(sum(imbalances[i + 1 - window: i + 1]) / window)
    return out


def vpin_at(px: list[float], vol: list[float], upto_idx: int, *,
            buckets_per_day: int = DEFAULT_BUCKETS_PER_DAY,
            window: int = DEFAULT_WINDOW) -> float | None:
    """只用 ``upto_idx`` 之前（不含）的逐筆算 VPIN —— **嚴格因果**。

    這個 repo 最貴的一次教訓就是 NQ 閘門的同日 look-ahead（讓 CELL_TUNE_V2 的
    「5/5 顯著」變成 0/5），所以這裡把切片邊界寫死在函式裡，而不是交給呼叫端
    自己記得。
    """
    if upto_idx <= 10:
        return None
    s = vpin_series(px[:upto_idx], vol[:upto_idx],
                    buckets_per_day=buckets_per_day, window=window)
    for v in reversed(s):
        if v is not None:
            return v
    return None
