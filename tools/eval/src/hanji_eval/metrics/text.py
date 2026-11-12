"""글자 지표: NID, CER, 한글 음절 다중집합 정밀도·재현율, 순서 차이, 예측 분류·빠진 글자 비율, 집계."""

from collections import Counter
from collections.abc import Iterable, Mapping

from rapidfuzz.distance import Indel, Levenshtein

PREDICTION_CLASSES = ("outside_gt", "insertion", "duplicate", "ambiguous")


def nid(pred: str, truth: str) -> float:
    """1 − Indel(P, G) / (|P| + |G|). 둘 다 비면 1."""
    return Indel.normalized_similarity(pred, truth)


def cer(pred: str, truth: str) -> float:
    """min(편집 거리 ÷ |G|, 1). G가 비면 P도 비었을 때 0, 아니면 1."""
    if not truth:
        return 0.0 if not pred else 1.0
    return min(Levenshtein.distance(pred, truth) / len(truth), 1.0)


def hangul(text: str) -> Counter[str]:
    """한글 음절(U+AC00~U+D7A3) 다중집합."""
    return Counter(ch for ch in text if "가" <= ch <= "힣")


def multiset_pr(pred: Counter[str], truth: Counter[str]) -> tuple[float | None, float | None]:
    """(정밀도 |P∩G|/|P|, 재현율 |P∩G|/|G|). 분모가 0이면 None."""
    common = sum((pred & truth).values())
    n_pred, n_truth = sum(pred.values()), sum(truth.values())
    return (common / n_pred if n_pred else None, common / n_truth if n_truth else None)


def hangul_pr(pred: str, truth: str) -> tuple[float | None, float | None]:
    return multiset_pr(hangul(pred), hangul(truth))


def f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def header_footer_pr(pred: list[str], truth: list[str]) -> tuple[float | None, float | None]:
    """머리말·꼬리말 찾기: 예측 page_header·page_footer 블록 글자와 원본 머리말·꼬리말 글자(정규화 뒤)의 글자 다중집합
    (정밀도, 재현율)."""
    return multiset_pr(Counter("".join(pred)), Counter("".join(truth)))


def order_gap(pred: str, truth: str) -> float | None:
    """순서 없는 한글 F1 − 순서 있는 NID(클수록 순서 오류)."""
    score = f1(*hangul_pr(pred, truth))
    return None if score is None else score - nid(pred, truth)


def class_rates(counts: Mapping[str, int], n_pred_blocks: int) -> dict[str, float | None]:
    """예측 블록 분류(outside_gt·insertion·duplicate·ambiguous)의 비율. 분모는 예측 블록 수."""
    unknown = set(counts) - set(PREDICTION_CLASSES)
    if unknown:
        raise ValueError(f"unknown prediction classes: {sorted(unknown)}")
    if any(v < 0 for v in counts.values()) or sum(counts.values()) > n_pred_blocks:
        raise ValueError("class counts must be between 0 and the prediction blocks")
    return {c: (counts.get(c, 0) / n_pred_blocks if n_pred_blocks else None) for c in PREDICTION_CLASSES}


def missing_rates(truth_chars: int, recorded: int, unrecorded: int) -> dict[str, float | None]:
    """빠진 원본 글자율과 그 가운데 기록된(처리 이력·쪽 상태가 있는) 손실·기록 없는 손실의 비율."""
    if recorded < 0 or unrecorded < 0 or recorded + unrecorded > truth_chars:
        raise ValueError("missing characters must be between 0 and the truth characters")
    if not truth_chars:
        return {"missing": None, "recorded": None, "unrecorded": None}
    return {"missing": (recorded + unrecorded) / truth_chars, "recorded": recorded / truth_chars,
            "unrecorded": unrecorded / truth_chars}


def mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def weighted_mean(values: Iterable[float], weights: Iterable[float]) -> float | None:
    """글자 수 가중 평균 같은 가중 평균. 무게 합이 0이면 None."""
    pairs = list(zip(values, weights, strict=True))
    if any(w < 0 for _, w in pairs):
        raise ValueError("negative weight")
    total = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / total if total else None
