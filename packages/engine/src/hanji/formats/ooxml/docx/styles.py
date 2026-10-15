"""styles.xml·numbering.xml에서 파서가 쓰는 작은 표만 뽑는다(스펙 §5.3): 스타일 id → basedOn·개요 수준·numId·이름·
런 크기·굵기·숨김. basedOn 사슬은 64단까지, 순환이면 그 자리에서 끊는다. docDefaults의 런 속성이 맨 아래 기본값이다.
문단에 pStyle이 없으면 기본 문단 스타일(w:default="1")의 사슬을 쓴다(Word와 같다)."""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MAX_CHAIN = 64  # basedOn 사슬 상한
DEFAULT_SIZE = 10.0  # 어디에도 크기가 없을 때(Word 기본, pt)
MAX_HEADING = 6
HEADING_NAME = re.compile(r"heading ([1-9])")  # Word 내부 이름(한국어 Word도 영어)
FALSE = ("0", "false", "off")


def on(element: ET.Element | None) -> bool | None:
    """켜고 끄는 속성(w:b·w:vanish 등): 없으면 None, w:val이 0·false·off면 False, 그 밖은 True."""
    if element is None:
        return None
    return element.get(f"{W}val", "true").lower() not in FALSE


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class RunProps:
    """런 속성 가운데 쓰는 것(없으면 None): 크기(pt), 굵기, 숨김."""

    size: float | None = None
    bold: bool | None = None
    vanish: bool | None = None


def run_props(rpr: ET.Element | None) -> RunProps:
    if rpr is None:
        return RunProps()
    sz = rpr.find(f"{W}sz")
    half = _int(sz.get(f"{W}val")) if sz is not None else None
    return RunProps(half / 2 if half is not None and half > 0 else None, on(rpr.find(f"{W}b")),
                    on(rpr.find(f"{W}vanish")))  # w:specVanish는 숨김이 아니다(문단 표시 기호 속성)


@dataclass(frozen=True, slots=True)
class Style:
    based: str | None
    name: str
    outline: int | None  # pPr/outlineLvl(정수가 아니면 None)
    num: str | None  # pPr/numPr/numId
    run: RunProps


@dataclass(slots=True)
class Styles:
    styles: dict[str, Style] = field(default_factory=dict)
    default_paragraph: str | None = None
    defaults: RunProps = RunProps()
    numbers: frozenset[str] = frozenset()  # numbering.xml의 w:num@numId

    def chain(self, key: str | None) -> list[Style]:
        """key부터 basedOn을 따라간 스타일들(최대 64단, 이미 본 id에서 끊는다, 없는 id에서 끝난다)."""
        out: list[Style] = []
        seen: set[str] = set()
        while key is not None and key in self.styles and key not in seen and len(out) < MAX_CHAIN:
            seen.add(key)
            out.append(self.styles[key])
            key = self.styles[key].based
        return out

    def paragraph_chain(self, p: ET.Element) -> list[Style]:
        style = p.find(f"{W}pPr/{W}pStyle")
        return self.chain(style.get(f"{W}val") if style is not None else self.default_paragraph)

    def heading(self, p: ET.Element) -> int | None:
        """제목 단계(스펙 §5.3) 또는 None. 개요 수준(문단 직접, 없으면 스타일 사슬에서 처음 나오는 정수)이 있으면 0~8만
        제목(단계 = 값 + 1, 6에서 접음). 개요 수준이 어디에도 없으면 사슬에서 이름이 heading 1~9·Title인 스타일."""
        chain = self.paragraph_chain(p)
        direct = p.find(f"{W}pPr/{W}outlineLvl")
        levels = [_int(direct.get(f"{W}val")) if direct is not None else None] + [s.outline for s in chain]
        level = next((v for v in levels if v is not None), None)
        if level is not None:
            return min(level + 1, MAX_HEADING) if 0 <= level <= 8 else None
        for style in chain:
            name = style.name.lower()
            if name == "title":
                return 1
            if match := HEADING_NAME.fullmatch(name):
                return min(int(match.group(1)), MAX_HEADING)
        return None

    def listed(self, p: ET.Element) -> bool:
        """목록 항목: 문단 numPr/numId(없으면 스타일 사슬의 것)가 0이 아니고 numbering.xml에 있다."""
        direct = p.find(f"{W}pPr/{W}numPr/{W}numId")
        nums = [direct.get(f"{W}val") if direct is not None else None] + [s.num for s in self.paragraph_chain(p)]
        num = next((n for n in nums if n is not None), None)
        return num is not None and num != "0" and num in self.numbers

    def run(self, p: ET.Element, r: ET.Element | None) -> tuple[float, bool, bool]:
        """런의 (크기 pt, 굵기, 숨김): 런 rPr → 런 스타일 사슬 → 문단 스타일 사슬 → docDefaults → (10pt, 굵지 않음,
        보임). r이 None이면 문단 기본값."""
        rpr = r.find(f"{W}rPr") if r is not None else None
        rstyle = rpr.find(f"{W}rStyle") if rpr is not None else None
        layers = [run_props(rpr)] + [s.run for s in self.chain(rstyle.get(f"{W}val") if rstyle is not None else None)]
        layers += [s.run for s in self.paragraph_chain(p)] + [self.defaults]
        size = next((x.size for x in layers if x.size is not None), DEFAULT_SIZE)
        bold = next((x.bold for x in layers if x.bold is not None), False)
        vanish = next((x.vanish for x in layers if x.vanish is not None), False)
        return size, bold, vanish


def read_styles(root: ET.Element | None, numbering: ET.Element | None) -> Styles:
    """두 파트 모두 없을 수 있다(그러면 기본값)."""
    styles = Styles()
    if numbering is not None:
        styles.numbers = frozenset(n.get(f"{W}numId", "") for n in numbering.iterfind(f"{W}num"))
    if root is None:
        return styles
    styles.defaults = run_props(root.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr"))
    for element in root.iterfind(f"{W}style"):
        key = element.get(f"{W}styleId", "")
        name, based = element.find(f"{W}name"), element.find(f"{W}basedOn")
        outline, num = element.find(f"{W}pPr/{W}outlineLvl"), element.find(f"{W}pPr/{W}numPr/{W}numId")
        styles.styles[key] = Style(
            based=based.get(f"{W}val") if based is not None else None,
            name=name.get(f"{W}val", "") if name is not None else "",
            outline=_int(outline.get(f"{W}val")) if outline is not None else None,
            num=num.get(f"{W}val") if num is not None else None,
            run=run_props(element.find(f"{W}rPr")))
        default = element.get(f"{W}default", "").lower() in ("1", "true", "on")
        if element.get(f"{W}type") == "paragraph" and default and styles.default_paragraph is None:
            styles.default_paragraph = key
    return styles
