"""header.xml에서 파서가 쓰는 작은 표만 뽑는다: 문단 모양(개요·번호·글머리), 글자 모양(크기·굵기), 테두리 채움."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from .container import HC, HH


@dataclass(frozen=True, slots=True)
class CharShape:
    """글자 모양: 크기(pt) = height / 100 × relSz@hangul / 100, 굵기 = hh:bold가 있음."""

    size: float
    bold: bool


@dataclass(slots=True)
class Styles:
    """paraPr id → (heading@type, heading@level), charPr id → CharShape, borderFill id → 채움 여부."""

    headings: dict[str, tuple[str, int]] = field(default_factory=dict)
    chars: dict[str, CharShape] = field(default_factory=dict)
    fills: dict[str, bool] = field(default_factory=dict)


BOUND = 10**6  # 속성 정수를 이 범위로 묶는다(아주 큰 수가 실수 계산·반올림에서 OverflowError가 되지 않게)


def _int(value: str | None, default: int) -> int:
    try:
        return max(-BOUND, min(BOUND, int(value))) if value is not None else default
    except ValueError:
        return default


def _filled(fill: ET.Element) -> bool:
    """칸 배경이 있다: winBrush@faceColor가 none·흰색이 아니거나 gradation·imgBrush가 있다."""
    for brush in fill.iter(f"{HC}winBrush"):
        if brush.get("faceColor", "none").lower() not in ("none", "#ffffff"):
            return True
    return any(True for _ in fill.iter(f"{HC}gradation")) or any(True for _ in fill.iter(f"{HC}imgBrush"))


def read_styles(root: ET.Element) -> Styles:
    styles = Styles()
    for para in root.iter(f"{HH}paraPr"):
        heading = para.find(f"{HH}heading")
        if heading is not None:
            styles.headings[para.get("id", "")] = (heading.get("type", "NONE"), _int(heading.get("level"), 0))
    for char in root.iter(f"{HH}charPr"):
        rel = char.find(f"{HH}relSz")
        scale = _int(rel.get("hangul") if rel is not None else None, 100)
        size = _int(char.get("height"), 1000) / 100 * scale / 100
        styles.chars[char.get("id", "")] = CharShape(size, char.find(f"{HH}bold") is not None)
    for fill in root.iter(f"{HH}borderFill"):
        styles.fills[fill.get("id", "")] = _filled(fill)
    return styles
