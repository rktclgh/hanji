"""불확실성: 2단계 군집 부트스트랩(발행처 → 문서, 같은 문서의 단위는 함께 움직인다)과 사건이 적을 때의 한쪽
Clopper-Pearson 정확 상한(스펙 §6.4·§7.6). 난수는 random.Random(seed)의 randrange다. 파이썬이 판 사이에 보장하는 것은
random()뿐이라 표본·구간 값은 파이썬 판(3.12)에 매인다: 시험이 고정 값으로 그 변화를 드러낸다."""

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

REPS = 2000
SEED = 0
MIN_PUBLISHERS = 8  # 이보다 적으면 1단계(발행처)가 무너져 문서 단위 재표집으로 내린다(few_clusters)
SMALL_EVENTS = 10  # 비율의 분자나 여집합이 이 수 이하면 부트스트랩 대신 Clopper-Pearson


@dataclass(frozen=True, slots=True)
class Unit:
    """재표집 단위의 값 하나. 같은 doc의 단위(쪽·블록·같은 원쪽의 두 렌더)는 늘 함께 뽑힌다."""

    publisher: str
    doc: str
    value: float
    weight: float = 1.0


@dataclass(frozen=True, slots=True)
class Interval:
    lo: float
    hi: float
    flags: tuple[str, ...] = ()


def weighted_mean(units: Sequence[Unit]) -> float:
    total = math.fsum(u.weight for u in units)
    return math.fsum(u.value * u.weight for u in units) / total if total else math.nan


def _docs(units: Sequence[Unit]) -> tuple[list[str], dict[str, list[str]], dict[str, list[Unit]]]:
    by_doc: dict[str, list[Unit]] = {}
    pub_of: dict[str, str] = {}
    for u in units:
        if pub_of.setdefault(u.doc, u.publisher) != u.publisher:
            raise ValueError(f"document {u.doc} is in two publishers")
        by_doc.setdefault(u.doc, []).append(u)
    pubs: dict[str, list[str]] = {}
    for doc in sorted(by_doc):
        pubs.setdefault(pub_of[doc], []).append(doc)
    return sorted(pubs), pubs, by_doc


def resamples(units: Sequence[Unit], reps: int = REPS, seed: int = SEED) -> tuple[list[list[str]], bool]:
    """(표본마다 뽑힌 문서 목록, few_clusters). 발행처를 복원 추출하고 뽑힌 발행처 안에서 문서를 복원 추출한다."""
    order, pubs, _ = _docs(units)
    rng = random.Random(seed)
    few = len(order) < MIN_PUBLISHERS
    all_docs = [d for p in order for d in pubs[p]]
    out = []
    for _ in range(reps):
        if few:
            out.append([all_docs[rng.randrange(len(all_docs))] for _ in all_docs])
            continue
        drawn: list[str] = []
        for _ in order:
            docs = pubs[order[rng.randrange(len(order))]]
            drawn += [docs[rng.randrange(len(docs))] for _ in docs]
        out.append(drawn)
    return out, few


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """선형 보간 백분위(numpy 기본과 같은 셈)."""
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _sorted_replicates(values: list[float]) -> list[float]:
    """NaN 표본(가중치 합이 0인 재표집 등)이 하나라도 있으면 ValueError: NaN을 정렬하면 백분위가 조용히 틀린다."""
    if any(math.isnan(v) for v in values):
        raise ValueError("a bootstrap replicate is NaN (a resample has zero total weight)")
    return sorted(values)


def cluster_ci(units: Sequence[Unit], stat: Callable[[Sequence[Unit]], float] = weighted_mean,
               reps: int = REPS, seed: int = SEED) -> Interval:
    """백분위 95% 구간."""
    if not units:
        return Interval(math.nan, math.nan, ("no_units",))
    _, _, by_doc = _docs(units)
    samples, few = resamples(units, reps, seed)
    values = _sorted_replicates([stat([u for d in s for u in by_doc[d]]) for s in samples])
    return Interval(percentile(values, 0.025), percentile(values, 0.975), ("few_clusters",) if few else ())


def paired_ci(a: Sequence[Unit], b: Sequence[Unit], stat: Callable[[Sequence[Unit]], float] = weighted_mean,
              reps: int = REPS, seed: int = SEED) -> Interval:
    """stat(a) − stat(b)의 구간. 두 도구·설정이 같은 재표집 표본(같은 문서)을 쓴다. 문서마다 발행처도 같아야 한다."""
    if not a and not b:
        return Interval(math.nan, math.nan, ("no_units",))
    _, _, doc_a = _docs(a)
    _, _, doc_b = _docs(b)
    if doc_a.keys() != doc_b.keys():
        raise ValueError("paired units must cover the same documents")
    if any(doc_a[d][0].publisher != doc_b[d][0].publisher for d in doc_a):
        raise ValueError("paired units must give each document the same publisher")
    samples, few = resamples(a, reps, seed)
    values = _sorted_replicates([stat([u for d in s for u in doc_a[d]]) - stat([u for d in s for u in doc_b[d]])
                                 for s in samples])
    return Interval(percentile(values, 0.025), percentile(values, 0.975), ("few_clusters",) if few else ())


def _log_pmf(k: int, n: int, p: float) -> float:
    if p <= 0.0:
        return 0.0 if k == 0 else -math.inf
    if p >= 1.0:
        return 0.0 if k == n else -math.inf
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1) + k * math.log(p) + (n - k) * math.log1p(-p)


def binom_cdf(x: int, n: int, p: float) -> float:
    return min(1.0, math.fsum(math.exp(_log_pmf(k, n, p)) for k in range(x + 1)))


def cp_upper(x: int, n: int, alpha: float = 0.05) -> float:
    """한쪽 Clopper-Pearson 상한(수준 1 − α): P(X ≤ x | n, p) = α를 푸는 p. x = 0이면 1 − α^(1/n), x = n이면 1.
    그 밖은 이항 누적 확률을 구간 [0, 1]에서 200번 이분해 구한다."""
    if not 0 <= x <= n or n < 1:
        raise ValueError("need 0 <= x <= n and n >= 1")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    if x == n:
        return 1.0
    if x == 0:
        return 1.0 - alpha ** (1.0 / n)
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if binom_cdf(x, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def min_trials(x: int, alpha: float, bound: float) -> int:
    """오류 x건일 때 상한이 bound 이하가 되는 가장 작은 시행 수(손 점검 표본 크기, 스펙 §6.6)."""
    if not 0.0 < bound < 1.0:
        raise ValueError("bound must be in (0, 1)")  # 0 이하면 끝나지 않는다
    n = max(x, 1)
    while cp_upper(x, n, alpha) > bound:
        n += 1
    return n


def rate_interval(units: Sequence[Unit], alpha: float = 0.05, reps: int = REPS, seed: int = SEED) -> Interval:
    """0/1 사건 비율의 구간. 단위는 문서(뽑기)마다 하나, 값은 0 또는 1, 무게 1이어야 한다(컨트롤러 판정 R8: 그래야
    두 길이 같은 비율을 잰다). 사건이나 그 여집합이 10건 이하면 한쪽 Clopper-Pearson(사건이 적으면 [0, 상한], 여집합이
    적으면 [하한, 1]; n ≤ 20이라 둘 다 10건 이하면 사건 쪽 상한), 아니면 군집 부트스트랩."""
    if len({u.doc for u in units}) != len(units):
        raise ValueError("rate_interval needs exactly one unit per document")
    if any(u.value not in (0.0, 1.0) or u.weight != 1.0 for u in units):
        raise ValueError("rate_interval values must be 0 or 1 with weight 1")
    n = len(units)
    events = sum(1 for u in units if u.value == 1.0)
    if n and events <= SMALL_EVENTS:
        return Interval(0.0, cp_upper(events, n, alpha), ("one_sided_upper",))
    if n and n - events <= SMALL_EVENTS:
        return Interval(1.0 - cp_upper(n - events, n, alpha), 1.0, ("one_sided_lower",))
    return cluster_ci(units, reps=reps, seed=seed)
