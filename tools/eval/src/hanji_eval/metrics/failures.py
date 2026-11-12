"""실패 문서(parse_error·timeout·crashed)의 지표별 처리(스펙 §7.4). 원칙: 실패가 점수를 올리지 않는다. 조건부 지표는
실패 문서를 빼고 n_failed를 함께 낸다."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# 실패 문서를 가장 나쁜 값으로 넣고 분모에 포함하는 지표
WORST = {
    "nid": 0.0, "hangul_f1": 0.0, "hangul_recall": 0.0, "teds": 0.0, "teds_s": 0.0, "grits_con": 0.0,
    "grits_top": 0.0, "table_perfect": 0.0, "heading_f1": 0.0, "header_footer_precision": 0.0,
    "header_footer_recall": 0.0, "number_exact": 0.0, "cer": 1.0, "nid_ws": 0.0, "heading_recall": 0.0,
    "number_recall": 0.0, "missing_rate": 1.0,
}
# 낼 출력이 없어 값이 없는 지표(실패 문서 제외, n_failed를 함께 낸다)
EXCLUDE = frozenset({
    "hangul_precision", "table_precision", "teds_matched", "teds_s_matched", "kendall_tau", "ece", "ece_same_value",
    "brier", "aurc", "seconds_per_page", "max_rss_mib", "order_gap", "heading_precision", "heading_level_agreement",
    "number_precision",
})

# 문서마다의 값이 아니라 세는 값(분자·분모를 모아 나눈다: 실패 문서의 몫은 집계하는 쪽이 센다. 표 찾기 재현율은
# table_recall이 실패 문서의 원본 표를 모두 놓친 것으로 센다)
COUNTED = frozenset({
    "number_unverified_rate", "missing_recorded_rate", "missing_unrecorded_rate", "outside_gt_rate",
    "insertion_rate", "duplicate_rate", "ambiguous_rate", "failure_rate", "table_recall",
})


@dataclass(frozen=True, slots=True)
class Applied:
    """문서 차례의 (문서, 값), 실패 문서 수, 값이 정의되지 않아 뺀 문서 수(예: τ 짝 둘 미만)."""

    values: tuple[tuple[str, float], ...]
    n_failed: int
    n_undefined: int


def apply(metric: str, per_doc: Mapping[str, float | None], failed: Iterable[str]) -> Applied:
    """성공 문서의 값(per_doc)과 실패 문서 목록에서 집계할 값을 만든다. 실패 문서가 per_doc에 있으면 오류."""
    failed_docs = sorted(set(failed))
    if set(failed_docs) & per_doc.keys():
        raise ValueError("a failed document must not have a value")
    defined = [(d, v) for d, v in sorted(per_doc.items()) if v is not None]
    undefined = len(per_doc) - len(defined)
    if metric in WORST:
        rows = sorted(defined + [(d, WORST[metric]) for d in failed_docs])
        return Applied(tuple(rows), len(failed_docs), undefined)
    if metric in EXCLUDE:
        return Applied(tuple(defined), len(failed_docs), undefined)
    raise ValueError(f"no failure rule for metric {metric}")


def table_recall(per_doc: Mapping[str, tuple[int, int]], failed_truth_tables: Mapping[str, int]) -> tuple[int, int]:
    """(짝지은 원본 표 합, 원본 표 합). 실패 문서의 원본 표는 모두 놓친 것으로 센다."""
    if per_doc.keys() & failed_truth_tables.keys():
        raise ValueError("a failed document must not have a value")
    matched = sum(m for m, _ in per_doc.values())
    truth = sum(t for _, t in per_doc.values()) + sum(failed_truth_tables.values())
    return matched, truth
