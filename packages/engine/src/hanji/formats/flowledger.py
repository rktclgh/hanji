"""글자 장부의 형식 무관 부분(HWPX·OOXML 함께, HWPX 스펙 §10). 블록 글자는 묶음 (출처, 글자)들을 이어 만들고, 장부는
같은 묶음 목록에서 출처마다 넣은 횟수와 넣은 공백 아닌 글자 수를 더한다. 묶음 하나는 출처 하나를 한 번 낸 것이다.
출처 없는 묶음(None)은 끼운 구분자다. 출처 열거(무엇이 출처이고 n이 얼마인가)는 형식 쪽에 둔다."""

import xml.etree.ElementTree as ET
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

Bundle = tuple[int | None, str]  # (출처 순번, 그 출처를 한 번 낸 글자) 또는 (None, 구분자)


def ink(text: str) -> int:
    """공백 아닌 코드 포인트 수."""
    return sum(not ch.isspace() for ch in text)


@dataclass(frozen=True, slots=True)
class Counts:
    """장부 단위(구역) 하나의 숫자(HWPX 스펙 §10.2)."""

    source: int
    in_blocks: int
    hidden: int
    rescued: int
    doubled: int
    partial: int

    @property
    def ok(self) -> bool:
        return self.source == self.in_blocks + self.hidden and self.doubled == 0 and self.partial == 0


@dataclass(slots=True)
class Source:
    """출처 하나: 요소, n(x), 닻 문단 순번, 출처가 든 가장 안쪽 문단의 id(살린 글자를 문단끼리 \n으로 잇는다), 숨은
    출처인가(HWPX 숨은 설명, OOXML 숨긴 글자·범위 밖: 블록에 넣으면 불일치이고 살리지 않는다)."""

    element: ET.Element
    n: int
    anchor: int
    para: int
    hidden: bool


@dataclass(slots=True)
class Ledger:
    sources: list[Source] = field(default_factory=list)
    index: dict[int, int] = field(default_factory=dict)  # id(요소) → 출처 순번
    times: list[int] = field(default_factory=list)
    chars: list[int] = field(default_factory=list)
    rescued: int = 0

    def add(self, element: ET.Element, n: int, anchor: int, para: int, hidden: bool) -> None:
        self.index[id(element)] = len(self.sources)
        self.sources.append(Source(element, n, anchor, para, hidden))
        self.times.append(0)
        self.chars.append(0)

    def source_of(self, element: ET.Element) -> int:
        return self.index[id(element)]

    def put(self, bundles: Iterable[Bundle]) -> None:
        """블록 하나에 넣은 묶음들을 센다(블록을 낼 때 한 번)."""
        for source, text in bundles:
            if source is not None:
                self.times[source] += 1
                self.chars[source] += ink(text)

    def unplaced(self) -> list[int]:
        """한 번도 넣지 않은 출처(n > 0, 숨은 출처 제외), 문서 순서."""
        return [k for k, s in enumerate(self.sources) if s.n > 0 and not s.hidden and self.times[k] == 0]

    def count(self, keys: Collection[int] | None = None) -> Counts:
        """스펙 §10.2: in_blocks = 블록에 넣은 공백 아닌 글자 합(잘못 넣은 숨은 출처 글자도 든다), hidden = 숨은 출처 n 합,
        doubled = 두 번 이상 넣은 보이는 출처 + 넣은 숨은 출처, partial = 넣은 글자 수가 n과 다른 보이는 출처. keys가 있으면
        그 출처들만 센다(HWPX 수식 스크립트를 인쇄 글자와 따로 센다. rescued는 장부 전체 값)."""
        picked = range(len(self.sources)) if keys is None else sorted(keys)
        visible = [k for k in picked if not self.sources[k].hidden]
        hidden = [k for k in picked if self.sources[k].hidden]
        return Counts(
            source=sum(self.sources[k].n for k in picked),
            in_blocks=sum(self.chars[k] for k in picked),
            hidden=sum(self.sources[k].n for k in hidden),
            rescued=self.rescued,
            doubled=sum(1 for k in visible if self.times[k] > 1) + sum(1 for k in hidden if self.times[k] > 0),
            partial=sum(1 for k in visible if self.chars[k] != self.sources[k].n),
        )
