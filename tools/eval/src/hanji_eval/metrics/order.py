"""읽기 순서: 짝 블록의 예측 차례 대 원본 차례의 Kendall τ-b."""

import math
from collections.abc import Sequence


def kendall_tau(pred: Sequence[float], truth: Sequence[float]) -> float | None:
    """τ-b = (일치 − 불일치) ÷ √((n0 − n1)(n0 − n2)). 짝이 둘 미만이거나 한쪽이 모두 같으면 None(정의되지 않음)."""
    if len(pred) != len(truth):
        raise ValueError("pred and truth must have the same length")
    concordant = discordant = ties_pred = ties_truth = 0
    n = len(pred)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = pred[i] - pred[j], truth[i] - truth[j]
            if a == 0 and b == 0:
                ties_pred += 1
                ties_truth += 1
            elif a == 0:
                ties_pred += 1
            elif b == 0:
                ties_truth += 1
            elif (a > 0) == (b > 0):
                concordant += 1
            else:
                discordant += 1
    pairs = n * (n - 1) // 2
    denom = math.sqrt((pairs - ties_pred) * (pairs - ties_truth))
    return (concordant - discordant) / denom if denom else None
