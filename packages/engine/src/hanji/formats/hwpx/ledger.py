"""글자 장부(스펙 §10). 출처는 구역 XML의 모든 hp:t와 hp:textart@text다. 블록 글자는 묶음 (출처, 글자)들을 이어 만들고,
장부는 같은 묶음 목록에서 출처마다 넣은 횟수와 넣은 공백 아닌 글자 수를 더한다. 묶음 하나는 출처 하나를 한 번 낸
것이다(<t>A<tab/>B</t>는 묶음 하나). 출처 없는 묶음(None)은 끼운 구분자다."""

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field

from .container import HP

T = f"{HP}t"
INLINE = {f"{HP}tab": "\t", f"{HP}lineBreak": "\n", f"{HP}nbSpace": " ", f"{HP}fwSpace": " ",
          f"{HP}titleMark": ""}  # hp:t 안 요소 → 대치 글자(모두 공백이거나 없음)

Bundle = tuple[int | None, str]  # (출처 순번, 그 출처를 한 번 낸 글자) 또는 (None, 구분자)


def ink(text: str) -> int:
    """공백 아닌 코드 포인트 수."""
    return sum(not ch.isspace() for ch in text)


def source_chars(t: ET.Element) -> int:
    """hp:t 출처의 n(x): t.text + 자식 요소 안 글자(문서 순서) + 자식 뒤 글자에서 공백 아닌 코드 포인트 수.
    대치 글자는 모두 공백이라 들지 않는다. 블록 글자를 만드는 쪽과 따로 센다(같은 함수를 쓰면 빠뜨림이 감춰진다)."""
    count = ink(t.text or "")
    for child in t:
        count += ink("".join(child.itertext()))  # 아는 인라인 요소 안 글자도 센다(대치 글자는 공백이라 들지 않는다)
        count += ink(child.tail or "")
    return count


@dataclass(frozen=True, slots=True)
class Counts:
    """구역 하나의 장부 숫자(스펙 §10.2)."""

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
    """출처 하나: 요소, n(x), 닻 문단 순번, 출처가 든 가장 안쪽 hp:p(살린 글자를 문단끼리 \n으로 잇는다), 숨은 설명
    안인가."""

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
        """한 번도 넣지 않은 출처(n > 0, 숨은 설명 제외), 문서 순서."""
        return [k for k, s in enumerate(self.sources) if s.n > 0 and not s.hidden and self.times[k] == 0]

    def count(self) -> Counts:
        """스펙 §10.2: in_blocks = 블록에 넣은 공백 아닌 글자 합(잘못 넣은 숨은 출처 글자도 든다), hidden = 숨은 출처 n 합,
        doubled = 두 번 이상 넣은 보이는 출처 + 넣은 숨은 출처, partial = 넣은 글자 수가 n과 다른 보이는 출처."""
        visible = [k for k, s in enumerate(self.sources) if not s.hidden]
        hidden = [k for k, s in enumerate(self.sources) if s.hidden]
        return Counts(
            source=sum(s.n for s in self.sources),
            in_blocks=sum(self.chars),
            hidden=sum(self.sources[k].n for k in hidden),
            rescued=self.rescued,
            doubled=sum(1 for k in visible if self.times[k] > 1) + sum(1 for k in hidden if self.times[k] > 0),
            partial=sum(1 for k in visible if self.chars[k] != self.sources[k].n),
        )
