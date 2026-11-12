"""제목 일치 P·R·F1: 예측 제목 블록이 원본 제목 블록과 정렬 짝이면 맞음(hanji-ko에서는 정확도가 아니라 크기 규칙의
일치도다, 스펙 §6.6). 단계 일치율은 짝인 제목끼리 단계가 같은 비율."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .text import f1


@dataclass(frozen=True, slots=True)
class HeadingScore:
    precision: float | None
    recall: float | None
    f1: float | None
    level_agreement: float | None


def heading_scores(pred_levels: Mapping[int, int], truth_levels: Mapping[int, int],
                   pairs: Iterable[tuple[int, int]]) -> HeadingScore:
    """pred_levels·truth_levels: 제목 블록 차례 → 단계. pairs: 정렬 짝(예측 차례, 원본 차례), 제목 아닌 블록 포함.
    정밀도·재현율은 짝이 된 제목 집합으로, 단계 일치율은 짝 단위로 센다(원본 제목 하나가 예측 조각 둘과 짝이면 두 번)."""
    both = sorted({(p, t) for p, t in pairs if p in pred_levels and t in truth_levels})
    hit_pred = {p for p, _ in both}
    hit_truth = {t for _, t in both}
    precision = len(hit_pred) / len(pred_levels) if pred_levels else None
    recall = len(hit_truth) / len(truth_levels) if truth_levels else None
    agree = sum(pred_levels[p] == truth_levels[t] for p, t in both) / len(both) if both else None
    return HeadingScore(precision, recall, f1(precision, recall), agree)
