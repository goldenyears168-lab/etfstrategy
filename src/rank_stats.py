"""Rank correlation and evaluation metric utilities."""

from __future__ import annotations

from statistics import mean, pstdev


def spearman_correlation(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2 or n != len(ys):
        return None

    def _ranks(vals: list[float]) -> list[float]:
        order = sorted(range(n), key=lambda i: vals[i])
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg_rank = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                ranks[order[k]] = avg_rank
            i = j + 1
        return ranks

    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den_x = sum((rx[i] - mx) ** 2 for i in range(n)) ** 0.5
    den_y = sum((ry[i] - my) ** 2 for i in range(n)) ** 0.5
    if den_x == 0 or den_y == 0:
        return None
    return num / (den_x * den_y)


def icir(ic_series: list[float]) -> float | None:
    """ICIR = mean(IC) / stdev(IC); requires n >= 2 and non-zero dispersion."""
    if len(ic_series) < 2:
        return None
    mu = mean(ic_series)
    sigma = pstdev(ic_series)
    if sigma == 0:
        return None
    return mu / sigma


def max_drawdown_pct(returns_pct: list[float]) -> float | None:
    """Max drawdown (%) from a sequence of period returns (compounded equity)."""
    if not returns_pct:
        return None
    equity = 100.0
    peak = equity
    max_dd = 0.0
    for ret in returns_pct:
        equity *= 1.0 + ret / 100.0
        if equity > peak:
            peak = equity
        if peak > 0:
            dd = (peak - equity) / peak * 100.0
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 2)
