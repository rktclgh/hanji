"""PPTX 크기 규칙 제목(스펙 §8.3): 런 크기·굵기 사슬, 도형 위쪽 좌표, 슬라이드마다 가장 위 문단 하나 고르기.

크기 규칙은 제목 자리 도형이 없는 슬라이드의 자리 표시자가 아닌 도형 문단에만 쓴다(자리 표시자는 종류로만 판정한다).
레이아웃·마스터 자리 대응은 쓰지 않고 a:normAutofit@fontScale도 적용하지 않는다."""

import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any

from ...pdf.group import BOLD_HEADING_RATIO, CONFIDENCE, HEADING_MAX_LINES, HEADING_RATIO
from ..drawing import A, P

# 사슬 끝(PowerPoint 기본 18pt). 런 크기가 모두 0pt인 문서는 크기 규칙이 꺼진다(DOCX와 같다).
DEFAULT_SIZE = 18.0
GROUP = f"{P}grpSp"


def level(p: ET.Element) -> int:
    """문단 수준(a:pPr@lvl, 0~8, 없거나 잘못되면 0)."""
    ppr = p.find(f"{A}pPr")
    try:
        value = int(ppr.get("lvl", "0")) if ppr is not None else 0
    except ValueError:
        return 0
    return value if 0 <= value <= 8 else 0


class Sizes:
    """런 크기(pt)·굵기 사슬: 런 a:rPr → 같은 문단 a:pPr/a:defRPr → 도형 a:lstStyle 수준 → 마스터 p:otherStyle 수준 →
    발표 p:defaultTextStyle 수준 → 18pt(굵기는 거짓). 직접 서식이 먼저다. 값은 따로 찾는다(크기와 굵기가 다른 단계에서
    올 수 있다). 크기는 정수 @sz(1/100pt, 0 이상)만 받는다."""

    def __init__(self, other: ET.Element | None, default: ET.Element | None) -> None:
        self.other = other  # 마스터 p:txStyles/p:otherStyle
        self.default = default  # 발표 p:defaultTextStyle

    def chain(self, run: ET.Element | None, p: ET.Element, body: ET.Element) -> list[ET.Element]:
        tag = f"{A}lvl{level(p) + 1}pPr/{A}defRPr"
        found = [run.find(f"{A}rPr") if run is not None else None, p.find(f"{A}pPr/{A}defRPr"),
                 body.find(f"{A}lstStyle/{tag}"), self.other.find(tag) if self.other is not None else None,
                 self.default.find(tag) if self.default is not None else None]
        return [e for e in found if e is not None]

    def run(self, run: ET.Element | None, p: ET.Element, body: ET.Element) -> tuple[float, bool]:
        size, bold = DEFAULT_SIZE, None
        for pr in reversed(self.chain(run, p, body)):  # 아래 단계부터 덮는다: 위 단계가 이긴다
            try:
                value = int(pr.get("sz", ""))
                size = value / 100 if value >= 0 else size
            except ValueError:
                pass
            if pr.get("b") is not None:
                bold = pr.get("b", "").lower() in ("1", "true")
        return size, bool(bold)


def _int(element: ET.Element | None, name: str) -> int | None:
    try:
        return int(element.get(name, "")) if element is not None else None
    except ValueError:
        return None


def top(element: ET.Element, chain: tuple[ET.Element, ...]) -> float | None:
    """도형 위쪽 좌표: p:spPr/a:xfrm/a:off@y에 바깥 그룹 변환(a:grpSpPr/a:xfrm의 off·ext·chOff·chExt의 y·cy)을 안쪽부터
    차례로 적용한 값. 값이 없거나 chExt@cy가 0이면 None(후보에서 뺀다)."""
    y: float | None = _int(element.find(f"{P}spPr/{A}xfrm/{A}off"), "y")
    for group in reversed([e for e in chain if e.tag == GROUP]):
        frame = group.find(f"{P}grpSpPr/{A}xfrm")
        if y is None or frame is None:
            return None
        off, ext = _int(frame.find(f"{A}off"), "y"), _int(frame.find(f"{A}ext"), "cy")
        start, span = _int(frame.find(f"{A}chOff"), "y"), _int(frame.find(f"{A}chExt"), "cy")
        if off is None or ext is None or start is None or not span:
            return None
        y = off + (y - start) * ext / span
    return y


def headings(specs: list[dict[str, Any]], sizes: Counter[float], slide_of: Any) -> None:
    """크기 규칙 제목: 본문 크기 = sizes(자리 표시자가 아닌 도형 런 크기 0.5pt 단위를 공백 아닌 글자 수로 센 것)의
    최빈값(같으면 작은 쪽). 후보(_cand) 가운데 대표 크기 ≥ 본문 × 1.15(굵으면 × 1.05)이고 줄 ≤ 2이며 위쪽 좌표가 있는
    문단에서 슬라이드마다 위쪽 좌표가 가장 작은 하나(같으면 앞)를 제목 단계 1, 신뢰도 0.6으로. slide_of(닻) → 슬라이드."""
    if not sizes:
        return
    body = max(sizes, key=lambda s: (sizes[s], -s))
    if body <= 0:
        return
    best: dict[int, dict[str, Any]] = {}
    for spec in specs:
        if not spec.get("_cand") or spec["_size"] is None or spec["_top"] is None or spec["_lines"] > HEADING_MAX_LINES:
            continue
        if spec["_size"] < body * (BOLD_HEADING_RATIO if spec["_bold"] else HEADING_RATIO):
            continue
        slide = slide_of(spec["_anchor"])
        if slide not in best or spec["_top"] < best[slide]["_top"]:
            best[slide] = spec
    for spec in best.values():
        spec.update(kind="heading", level=1, text=" ".join(spec["text"].split()), confidence=CONFIDENCE["heading"])
