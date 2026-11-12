"""신뢰도 지표(스펙 §8.5): 같은 폭 15구간 ECE(정수 구간), 같은 값 ECE, Brier, AURC·선택 위험(동점 묶음 안 기대값).
모두 출력 블록 조건부다(실패 문서는 블록이 없다): 실패율·빠진 글자율과 함께 보고한다."""

import math
from collections.abc import Sequence

BINS = 15


def _check(conf: Sequence[float], labels: Sequence[int]) -> None:
    if len(conf) != len(labels) or not conf:
        raise ValueError("need the same, non-zero number of confidences and labels")
    if any(not 0.0 <= c <= 1.0 for c in conf) or any(y not in (0, 1) for y in labels):
        raise ValueError("confidences must be in [0, 1] and labels 0 or 1")


def bin_of(confidence: float) -> int:
    """c_m = round(신뢰도 × 1000), k = min(14, ⌊c_m × 15 ÷ 1000⌋)(정수 나눗셈)."""
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be in [0, 1]")
    return min(BINS - 1, round(confidence * 1000) * BINS // 1000)


def reliability(conf: Sequence[float], labels: Sequence[int]) -> list[tuple[int, int, float, float]]:
    """신뢰도 도표 자료: 빈 구간을 뺀 (구간, n, 평균 신뢰도, 정답률)."""
    _check(conf, labels)
    bins: dict[int, list[int]] = {}
    for i, c in enumerate(conf):
        bins.setdefault(bin_of(c), []).append(i)
    return [(k, len(ix), math.fsum(conf[i] for i in ix) / len(ix), sum(labels[i] for i in ix) / len(ix))
            for k, ix in sorted(bins.items())]


def ece(conf: Sequence[float], labels: Sequence[int]) -> float:
    """Σ_k (n_k ÷ N)·|평균 신뢰도_k − 정답률_k|, 같은 폭 15구간."""
    n = len(conf)
    return math.fsum(m / n * abs(c - a) for _, m, c, a in reliability(conf, labels))


def ece_same_value(conf: Sequence[float], labels: Sequence[int]) -> float:
    """서로 다른 신뢰도 값마다 한 구간(기록용)."""
    _check(conf, labels)
    groups: dict[float, list[int]] = {}
    for c, y in zip(conf, labels):
        groups.setdefault(c, []).append(y)
    n = len(conf)
    return math.fsum(len(ys) / n * abs(c - sum(ys) / len(ys)) for c, ys in groups.items())


def brier(conf: Sequence[float], labels: Sequence[int]) -> float:
    _check(conf, labels)
    return math.fsum((c - y) ** 2 for c, y in zip(conf, labels)) / len(conf)


def _risks(conf: Sequence[float], labels: Sequence[int]) -> list[float]:
    """신뢰도 내림차순 i번째(1부터)의 기대 위험. 같은 신뢰도 묶음 g(크기 m, 오류 e, 앞 누적 오류 E) 안 j번째는
    (E + j·e ÷ m) ÷ i(묶음 안 차례를 모두 평균한 값)."""
    _check(conf, labels)
    groups: dict[float, list[int]] = {}
    for c, y in zip(conf, labels):
        groups.setdefault(c, []).append(y)
    risks, before, count = [], 0, 0
    for c in sorted(groups, reverse=True):
        ys = groups[c]
        errors = sum(1 - y for y in ys)
        for j in range(1, len(ys) + 1):
            count += 1
            risks.append((before + j * errors / len(ys)) / count)
        before += errors
    return risks


def aurc(conf: Sequence[float], labels: Sequence[int]) -> float:
    risks = _risks(conf, labels)
    return math.fsum(risks) / len(risks)


def selective_risk(conf: Sequence[float], labels: Sequence[int], coverage: float) -> float:
    """범위 c에서 i = ⌈c·N⌉의 기대 위험."""
    if not 0.0 < coverage <= 1.0:
        raise ValueError("coverage must be in (0, 1]")
    risks = _risks(conf, labels)
    # 0.1 × 30 = 3.0000000000000004 같은 부동소수 잡음이 한 칸 밀지 않게 아주 작은 여유를 뺀다
    return risks[max(1, math.ceil(coverage * len(risks) - 1e-9)) - 1]
