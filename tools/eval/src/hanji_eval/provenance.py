"""출처 전파: 하네스가 만드는 값은 데이터 출처 집합(데이터셋 identity, 분할)을 달고 다니고, 값을 합치면 출처도 합친다.
출구(커밋용 보고서·공개 내보내기, 보정 표 쓰기, 고르기·채택 게이트)는 경로·이름·병합과 상관없이 출처로 검사한다."""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .datasets import Registry, RegistryError, Source


class ProvenanceError(PermissionError):
    """이 출구에 들 수 없는 출처가 섞였다."""


@dataclass(frozen=True, slots=True)
class Traced:
    """출처 집합을 단 값."""

    value: Any
    sources: frozenset[Source]


def trace(value: Any, *sources: Source) -> Traced:
    return Traced(value, frozenset(sources))


def combine(fn: Callable[..., Any], *items: Traced) -> Traced:
    """값들을 fn으로 합치고 출처 집합의 합집합을 단다(병합·보정 결과도 이 길로 만든다)."""
    return Traced(fn(*(t.value for t in items)), frozenset().union(*(t.sources for t in items)))


def sources_of(items: Iterable[Traced | Mapping[str, Any]]) -> frozenset[Source]:
    """Traced 값들이나 보고서 결과 줄(sources 필드)의 출처를 모은다."""
    out: set[Source] = set()
    for item in items:
        if isinstance(item, Traced):
            out |= item.sources
        else:
            out |= {Source(s["dataset"], s["split"]) for s in item["sources"]}
    return frozenset(out)


def _dataset(registry: Registry, source: Source, exit_name: str):
    """출처의 데이터셋. 모르는 identity, 그 데이터셋에 없는 분할(예: 개발·보류를 합친 "all")은 거부한다."""
    try:
        dataset = registry.by_identity(source.dataset)
    except RegistryError:
        raise ProvenanceError(f"{exit_name}: unknown dataset identity {source.dataset}") from None
    if source.split not in dataset.splits:
        raise ProvenanceError(f"{exit_name}: {dataset.name} has no split {source.split!r}")
    return dataset


def check_report(sources: Iterable[Source], registry: Registry, holdout_open: bool) -> None:
    """커밋용 보고서·공개 내보내기: publishable=false 출처 거부, 봉인 분할은 보류 세트를 연 실행에서만."""
    for source in sorted(set(sources), key=lambda s: (s.dataset, s.split)):
        dataset = _dataset(registry, source, "report")
        if not dataset.publishable:
            raise ProvenanceError(f"report: {dataset.name} is not publishable")
        if source.split in dataset.sealed_splits and not holdout_open:
            raise ProvenanceError(f"report: {dataset.name}/{source.split} is sealed (needs --open-holdout)")


def _role(sources: Iterable[Source], registry: Registry, role: str, exit_name: str) -> None:
    items = sorted(set(sources), key=lambda s: (s.dataset, s.split))
    if not items:
        raise ProvenanceError(f"{exit_name}: input has no sources")
    for source in items:
        dataset = _dataset(registry, source, exit_name)
        if role not in dataset.roles:
            raise ProvenanceError(f"{exit_name}: {dataset.name} may not be used for {role}")
        if source.split in dataset.sealed_splits:
            raise ProvenanceError(f"{exit_name}: {dataset.name}/{source.split} is sealed")


def check_calibration(sources: Iterable[Source], registry: Registry) -> None:
    """보정 표 쓰기: 보정 역할 데이터셋의 봉인되지 않은 분할(hanji-ko 개발 분할)만."""
    _role(sources, registry, "calibration", "calibration")


def check_selection(sources: Iterable[Source], registry: Registry) -> None:
    """고르기·채택 게이트 입력: 고르기 역할 데이터셋의 봉인되지 않은 분할만(DP-Bench·보류 세트 거부)."""
    _role(sources, registry, "selection", "selection")
