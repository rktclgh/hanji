"""흐름 문서(쪽 없음: HWPX·DOCX·PPTX) 블록 명세를 마무리하는 공용 도우미. 블록 명세는 내부 키(_anchor·_late·_sizes 등)를
달고 모이고, finish가 제목(개요 수준이 없으면 PDF와 같은 글꼴 크기 규칙)·제목 경로·위치(기본 FlowLocator, PPTX는 자기
Places)를 채워 계약 블록 명세와 처리 이력으로 바꾼다."""

import bisect
from collections import Counter
from dataclasses import dataclass
from typing import Any, Protocol

from hanji_contracts import Attempt, FlowLocator, GateCheck, GateResult, Locator, RegionRecord

from .flowledger import Counts
from .pdf.group import BOLD_HEADING_RATIO, CONFIDENCE, HEADING_MAX_LINES, HEADING_RATIO, MAX_LEVEL


class Places(Protocol):
    """닻 → 위치(locator)와 처리 이력 id 앞부분(region, 뒤에 -{순번}이 붙는다)."""

    def locator(self, path: tuple[str, ...], anchor: int) -> Locator: ...

    def region(self, section: int, anchor: int, tag: str) -> str: ...


class Flow:
    """HWPX·DOCX: 닻 = 본문 항목 순번(FlowLocator), 처리 이력 id는 s{구역}-p{닻}-{꼬리표}."""

    def locator(self, path: tuple[str, ...], anchor: int) -> Locator:
        return FlowLocator(section_path=path, paragraph_index=anchor)

    def region(self, section: int, anchor: int, tag: str) -> str:
        return f"s{section}-p{anchor}-{tag}"


@dataclass(slots=True)
class Region:
    """처리 이력 한 줄(제목 경로는 문서를 다 읽은 뒤 닻 문단의 것으로 채운다)."""

    tag: str
    kind: str
    reason: str
    section: int
    anchor: int
    gate: GateResult | None = None
    region_id: str | None = None  # 정해진 id(구역 단위 기록)


def gate(name: str, value: int, threshold: str | None) -> GateResult:
    """근거 숫자 하나. 기준이 있으면 실패 검사(버그 신호), 없으면 기록용 통과 검사."""
    passed = threshold is None
    return GateResult(passed=passed, checks=(GateCheck(name=name, passed=passed, value=value, threshold=threshold),))


def ledger_gate(counts: Counts) -> GateResult:
    """coverage_mismatch 근거: in_blocks(기준 source − hidden), doubled·partial(기준 0), hidden(기록)."""
    expected = counts.source - counts.hidden
    checks = (GateCheck(name="in_blocks", passed=counts.in_blocks == expected, value=counts.in_blocks,
                        threshold=f"=={expected}"),
              GateCheck(name="doubled", passed=counts.doubled == 0, value=counts.doubled, threshold="==0"),
              GateCheck(name="partial", passed=counts.partial == 0, value=counts.partial, threshold="==0"),
              GateCheck(name="hidden", passed=True, value=counts.hidden))
    return GateResult(passed=all(c.passed for c in checks), checks=checks)


def headings_by_size(specs: list[dict[str, Any]]) -> None:
    """개요 수준(제목 스타일)이 없는 문서: PDF와 같은 상수로 제목을 고른다. 본문 크기 = 대상 문단 글자 수로 센 크기 최빈값(같으면
    작은 쪽), 제목 = 대표 크기 ≥ 본문 × 1.15(굵으면 × 1.05)이고 표시 줄 ≤ 2. 단계는 큰 크기부터 1, 2, …(6에서 접음)."""
    counts = Counter[float]()
    for spec in specs:
        for (size, _), n in spec.get("_sizes", Counter()).items():
            counts[size] += n
    if not counts:
        return
    body = max(counts, key=lambda s: (counts[s], -s))
    if body <= 0:  # 크기가 0 이하인 문서는 크기 규칙을 쓰지 않는다(모든 문단이 제목이 되지 않게)
        return
    chosen = [spec for spec in specs if spec.get("_size") is not None and spec["_lines"] <= HEADING_MAX_LINES
              and spec["_size"] >= body * (BOLD_HEADING_RATIO if spec["_bold"] else HEADING_RATIO)]
    # 크기 → 단계(큰 크기부터 1). 제목마다 목록에서 찾으면 서로 다른 크기가 많을 때 이차 시간이다
    ranks = {size: k for k, size in enumerate(sorted({spec["_size"] for spec in chosen}, reverse=True))}
    for spec in chosen:
        # 제목은 한 줄로(PDF처럼 줄을 공백으로 잇는다): 줄바꿈이 목록·제목 경로·마크다운 제목을 깨지 않게
        spec.update(kind="heading", level=min(ranks[spec["_size"]] + 1, MAX_LEVEL),
                    text=" ".join(spec["text"].split()), confidence=CONFIDENCE["heading"])


def finish(specs: list[dict[str, Any]], regions: list[Region], outline: bool, places: Places = Flow(),
           ) -> tuple[tuple[dict[str, Any], ...], tuple[RegionRecord, ...]]:
    """제목을 정하고 제목 경로를 채운다. 구역 끝 블록(_late)과 처리 이력은 닻 문단 자리의 제목 경로를 쓴다. places가
    닻으로 위치와 처리 이력 id를 짓는다(닻은 블록 차례대로 줄지 않는 정수여야 한다: 제목 경로를 이분 탐색으로 찾는다)."""
    if not outline:
        headings_by_size(specs)
    index = {id(spec): k for k, spec in enumerate(specs)}
    stack: list[tuple[int, str]] = []
    marks: list[tuple[int, tuple[str, ...]]] = []  # (닻, 그 닻까지 처리한 뒤 제목 경로)
    blocks: list[dict[str, Any]] = []
    for spec in specs:
        anchor = spec["_anchor"]
        if spec["_late"]:
            path = path_at(marks, anchor)
        else:
            if spec["kind"] == "heading":
                while stack and stack[-1][0] >= spec["level"]:
                    stack.pop()
            path = tuple(text for _, text in stack)
            if spec["kind"] == "heading":
                stack.append((spec["level"], spec["text"]))
            marks.append((anchor, tuple(text for _, text in stack)))
        block = {k: v for k, v in spec.items() if not k.startswith("_")}
        if "_caption" in spec:  # 짝 캡션 명세의 순번(core/build.py가 caption_block_id로 바꾼다)
            block["figure"] = {**block["figure"], "caption_ref": index[id(spec["_caption"])]}
        block["section_path"] = path
        block["locator"] = places.locator(path, anchor)
        blocks.append(block)
    seen = Counter[str]()
    records = []
    for region in regions:
        region_id = region.region_id
        if region_id is None:
            key = places.region(region.section, region.anchor, region.tag)
            region_id = f"{key}-{seen[key]}"
            seen[key] += 1
        records.append(RegionRecord(region_id=region_id, kind=region.kind, chosen="det",
                                    attempts=(Attempt(layer="det"),), fallback_reason=region.reason, gate=region.gate,
                                    locator=places.locator(path_at(marks, region.anchor), region.anchor)))
    return tuple(blocks), tuple(records)


def path_at(marks: list[tuple[int, tuple[str, ...]]], anchor: int) -> tuple[str, ...]:
    """닻 문단까지 처리한 뒤의 제목 경로(그 닻 이하의 마지막 표시)."""
    k = bisect.bisect_right(marks, anchor, key=lambda m: m[0])
    return marks[k - 1][1] if k else ()
