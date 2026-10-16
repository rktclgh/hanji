"""PPTX(PresentationML, zip + XML) → ParsedSource(스펙 §8). PDF로 바꾸지 않고 원본 구조를 결정론으로 읽는다.

presentation.xml의 p:sldIdLst 차례가 슬라이드 차례다(1부터). 슬라이드 안은 제목 자리 도형(title·ctrTitle)을 먼저, 그다음
p:spTree의 도형 트리 앞 순회 차례(그룹은 펼친다, 좌표로 다시 정렬하지 않는다). 위치는 SlideLocator(슬라이드, 펼친 트리
순번: 그룹 자신도 하나로 센다). 도형 글 몸통의 a:p 하나가 블록 하나다: 제목 자리는 문단을 공백으로 이은 제목(단계 1),
dt·ftr·sldNum 자리는 꼬리말·hdr는 머리말 블록 하나, 그 밖은 글머리(a:buChar·a:buAutoNum·a:buBlip, 없으면 body·obj
자리)면 목록 항목, 아니면 문단. 제목 자리가 없는 슬라이드는 자리 표시자가 아닌 도형 문단에 크기 규칙(shapes.py)을 써서
가장 위 문단 하나를 제목으로 고른다. 숨긴 도형(p:cNvPr@hidden, 그룹이면 그 아래 전부)은 블록이 없고 글자는 장부 숨김이다.
슬라이드가 장부 단위다: ooxml/ledger.py의 Walker가 출처를 따로 열거해 보통·숨김·범위 밖으로 표시하고, 블록에는 보통 출처만
넣는다. 한 번도 넣지 않은 보통 출처는 슬라이드 끝 구조 문단(신뢰도 0.3, unassigned_text)으로 살린다."""

import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from hanji_contracts import SlideLocator

from ....errors import ParseError
from ...base import ParsedSource
from ...flowdoc import Region, finish, gate, ledger_gate
from ...flowledger import Bundle, Counts, Ledger, ink
from ...pdf.group import RESCUED_CONFIDENCE, step
from ...pdf.parser import COVERAGE_MISMATCH, UNASSIGNED_TEXT
from ...zipxml import Prefixes
from ..drawing import A, A_BR, A_P, A_T, P, SHAPES, Images, hidden_shape
from ..ledger import NORMAL, Note, Story, Walker
from ..package import MC, READ, R, Package, choose
from .shapes import Sizes, headings, top

MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MAIN = f"{MIME}.main+xml"
M_T = "{http://schemas.openxmlformats.org/officeDocument/2006/math}t"
SP, GROUP, PIC, FRAME, CXN = f"{P}sp", f"{P}grpSp", f"{P}pic", f"{P}graphicFrame", f"{P}cxnSp"
ALTERNATE = f"{MC}AlternateContent"
CONTAINERS = frozenset({f"{P}cSld", f"{P}spTree"})  # 벗겨 자식을 읽는 슬라이드 층 요소
RUNS = frozenset({f"{A}r", f"{A}fld"})  # 글자(a:t)를 담는 런(a:fld는 저장된 결과)
TITLES = frozenset({"title", "ctrTitle"})
MARGINS = {"dt": "page_footer", "ftr": "page_footer", "sldNum": "page_footer", "hdr": "page_header"}
LISTED = frozenset({"body", "obj"})  # 글머리 표시가 없으면 목록 항목인 자리 표시자(마스터 기본 글머리)
BULLETS = frozenset({f"{A}buChar", f"{A}buAutoNum", f"{A}buBlip"})
HIDDEN_SLIDE = "hidden_slide"  # 발표에서 감춘 슬라이드(글자는 블록에 넣는다)
SHIFT = 26  # 닻 = 슬라이드 << SHIFT | (제목 자리가 아니면 OTHER) | 도형 순번: 블록 차례대로 커진다
OTHER = 1 << (SHIFT - 1)
MASK = OTHER - 1
DOC_ANCHOR = 1 << SHIFT  # 문서 단위 기록의 닻(슬라이드 1, 순번 0)


def anchor_of(slide: int, index: int, title: bool = False) -> int:
    """(슬라이드 번호, 도형 순번) → 닻. 제목 자리 도형은 그 슬라이드의 다른 도형보다 앞이다."""
    return (slide << SHIFT) | (0 if title else OTHER) | index


class Slides:
    """PPTX 위치 짓기(flowdoc.Places): 닻 → SlideLocator, 처리 이력 id는 slide{n}-sh{도형 순번}-{꼬리표}."""

    def locator(self, path: tuple[str, ...], anchor: int) -> SlideLocator:
        return SlideLocator(slide=anchor >> SHIFT, shape_index=anchor & MASK)

    def region(self, section: int, anchor: int, tag: str) -> str:
        return f"slide{anchor >> SHIFT}-sh{anchor & MASK}-{tag}"


@dataclass(frozen=True, slots=True)
class Shape:
    """펼친 도형 트리의 도형 하나: 요소, 순번, p:sld부터 바로 위까지의 조상들(감싼 요소·그룹), 숨김(자신·바깥 그룹)."""

    element: ET.Element
    index: int
    chain: tuple[ET.Element, ...]
    hidden: bool


def placeholder(shape: ET.Element) -> str | None:
    """자리 표시자 종류(p:nv*Pr/p:nvPr/p:ph@type, type이 없으면 obj), 자리 표시자가 아니면 None."""
    for nv in shape:
        if nv.tag.startswith(f"{P}nv"):
            ph = nv.find(f"{P}nvPr/{P}ph")
            return None if ph is None else ph.get("type", "obj")
    return None


def titles_of(shapes: list[Shape]) -> set[int]:
    """슬라이드의 보이는 제목 자리 도형(id). 숨긴 제목 자리는 제목 자리가 있는 슬라이드로 치지 않는다(스펙 §17)."""
    return {id(s.element) for s in shapes if s.element.tag == SP and not s.hidden and placeholder(s.element) in TITLES}


def layout(root: ET.Element, prefixes: Prefixes) -> tuple[list[Shape], list[tuple[ET.Element, tuple, int | None]]]:
    """슬라이드 파트 → (도형들(앞 순회 차례, 순번), Walker가 훑을 단위들 (요소, 조상들, 도형 순번 또는 None)). 단위는
    잎 도형 통째, 그룹 안 도형 아닌 자식(그룹 속성 등), 고르지 않은 mc 쪽 통째, 읽지 않는 이름공간 요소 통째, 슬라이드
    층의 그 밖 요소다(그룹 안이면 가장 안쪽 그룹의 순번, 아니면 None: 슬라이드 첫 자리에 닻을 둔다). 고른 mc 쪽·p:cSld·
    p:spTree는 벗긴다(스택 순회)."""
    shapes: list[Shape] = []
    units: list[tuple[ET.Element, tuple, int | None]] = []
    stack: list[tuple[ET.Element, tuple[ET.Element, ...], bool]] = [(c, (root,), False) for c in reversed(root)]
    while stack:
        element, chain, hidden = stack.pop()
        tag = element.tag
        if tag in SHAPES:
            hidden = hidden or hidden_shape(element)
            shapes.append(Shape(element, len(shapes), chain, hidden))
            if tag == GROUP:
                stack.extend((c, (*chain, element), hidden) for c in reversed(element))
            else:
                units.append((element, chain, len(shapes) - 1))
        elif tag == ALTERNATE:
            chosen = choose(element, prefixes)
            for child in reversed(element):
                if child is chosen:
                    stack.extend((c, (*chain, element, child), hidden) for c in reversed(child))
                else:
                    stack.append((child, (*chain, element), hidden))  # 단위가 된다(아래 그 밖)
        elif tag in CONTAINERS:
            stack.extend((c, (*chain, element), hidden) for c in reversed(element))
        else:
            units.append((element, chain, None))
    return shapes, _anchor_units(shapes, units)


def _anchor_units(shapes: list[Shape], units: list[tuple[ET.Element, tuple, int | None]],
                  ) -> list[tuple[ET.Element, tuple, int | None]]:
    """도형 순번이 없는 단위는 그것을 품은 가장 안쪽 그룹의 순번(없으면 None)."""
    order = {id(s.element): s.index for s in shapes}
    out = []
    for element, chain, index in units:
        if index is None:
            index = max((order[id(e)] for e in chain if id(e) in order), default=None)
        out.append((element, chain, index))
    return out


@dataclass(slots=True)
class _Doc:
    """문서 전체에서 모으는 것: 블록 명세(내부 키는 finish가 지운다), 처리 이력, 장부 숫자, 읽은 파트."""

    name: str
    package: Package
    walker: Walker
    images: Images
    specs: list[dict[str, Any]] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    counts: list[Counts] = field(default_factory=list)
    # 슬라이드 뿌리: Walker·장부가 id(a:p)로 문단을 가리키므로 파싱이 끝날 때까지 붙잡아 id 재사용을 막는다
    held: list[ET.Element] = field(default_factory=list)
    sizes: Counter[float] = field(default_factory=Counter)  # 자리 표시자가 아닌 도형 런 크기(0.5pt)별 글자 수
    parts: dict[str, ET.Element | None] = field(default_factory=dict)  # 읽은 레이아웃·마스터 등(한 번만 읽는다)

    def read(self, member: str, root: str) -> ET.Element | None:
        """보조 파트(레이아웃·마스터)를 한 번만 읽는다. 깨졌거나 뿌리가 다르면 None(그 단계를 건너뛴다)."""
        if member not in self.parts:
            try:
                self.parts[member] = self.package.part(member, root)[0]
            except ParseError:
                self.parts[member] = None
        return self.parts[member]

    def master(self, slide: str) -> ET.Element | None:
        """슬라이드 → 레이아웃 → 마스터 파트 뿌리(관계가 없거나 깨졌으면 None)."""
        for layout in self.package.related(slide, "slideLayout")[:1]:
            if self.read(layout, f"{P}sldLayout") is not None:
                for master in self.package.related(layout, "slideMaster")[:1]:
                    return self.read(master, f"{P}sldMaster")
        return None


class _Slide:
    """슬라이드 하나를 블록 명세로 바꾼다."""

    def __init__(self, doc: _Doc, number: int, root: ET.Element, prefixes: Prefixes, member: str,
                 shapes: list[Shape], ledger: Ledger, sizes: Sizes) -> None:
        self.doc = doc
        self.number = number  # 1부터
        self.root = root
        self.prefixes = prefixes
        self.host = member  # 그림·차트 관계를 이 파트에서 푼다
        self.shapes = shapes
        self.ledger = ledger
        self.titles = titles_of(shapes)
        self.sizes = sizes
        self.late = False

    def anchor(self, shape: Shape) -> int:
        return anchor_of(self.number, shape.index, id(shape.element) in self.titles)

    def region(self, tag: str, kind: str, reason: str, anchor: int, result: Any = None) -> None:
        self.doc.regions.append(Region(tag, kind, reason, self.number - 1, anchor, result))

    def emit(self, kind: str, bundles: list[Bundle], anchor: int, **extra: Any) -> bool:
        """블록 하나를 낸다(공백만이면 내지 않는다). 낸 묶음을 장부에 넣는다."""
        text = "".join(t for _, t in bundles).strip()
        if not text:
            return False
        self.doc.specs.append({"kind": kind, "text": text, "confidence": extra.pop("confidence", 1.0), "state": "det",
                               "text_source": "native", "_anchor": anchor, "_late": self.late, **extra})
        self.ledger.put(bundles)
        return True

    def runs(self, p: ET.Element, title: bool = False) -> tuple[list[Bundle], list[tuple[ET.Element | None, int]], int]:
        """a:p 글자(스펙 §8.2): 묶음, (런 요소, 공백 아닌 글자 수)들, 줄 수(a:br + 1). 보통 출처(a:t·m:t)만 넣고,
        a:br은 \\n(제목이면 공백), 고른 mc 쪽만 본다."""
        out: list[Bundle] = []
        weights: list[tuple[ET.Element | None, int]] = []
        lines = 1
        stack: list[tuple[ET.Element, ET.Element | None]] = [(c, None) for c in reversed(p)]
        while stack:
            element, run = stack.pop()
            tag = element.tag
            if tag in (A_T, M_T):
                if self.doc.walker.marks.get(id(element)) == NORMAL:
                    text = "".join(element.itertext())
                    out.append((self.ledger.source_of(element), text))
                    weights.append((run, ink(text)))
            elif tag == A_BR:
                out.append((None, " " if title else "\n"))
                lines += 1
            elif tag == ALTERNATE:
                chosen = choose(element, self.prefixes)
                stack.extend((c, run) for c in reversed(chosen if chosen is not None else ()))
            elif tag in RUNS:
                stack.extend((c, element) for c in reversed(element))
            elif tag[1:].partition("}")[0] in READ or not tag.startswith("{"):
                stack.extend((c, run) for c in reversed(element))
        return out, weights, lines

    def build(self) -> None:
        for shape in sorted(self.shapes, key=lambda s: id(s.element) not in self.titles):
            if not shape.hidden:
                self.shape(shape)
        self.late = True
        self.rescue()
        counts = self.ledger.count()
        self.doc.counts.append(counts)
        if not counts.ok:
            self.doc.regions.append(Region("coverage", "paragraph", COVERAGE_MISMATCH, self.number - 1,
                                           anchor_of(self.number, 0, True), ledger_gate(counts),
                                           f"slide{self.number}-coverage"))

    def shape(self, shape: Shape) -> None:
        """도형 하나의 블록(그룹 자신과 잉크 p:contentPart는 블록이 없다)."""
        if shape.element.tag in (SP, CXN):
            self.text(shape)

    def text(self, shape: Shape) -> None:
        """도형 글 몸통: 제목 자리는 제목 하나, dt·ftr·sldNum·hdr 자리는 꼬리말·머리말 하나, 그 밖은 문단마다."""
        body = shape.element.find(f"{P}txBody")
        if body is None:
            return
        kind = placeholder(shape.element)
        anchor = self.anchor(shape)
        paragraphs = body.findall(A_P)
        if kind in TITLES or kind in MARGINS:
            bundles: list[Bundle] = []
            for p in paragraphs:
                more = self.runs(p, title=kind in TITLES)[0]
                if any(ink(t) for _, t in more):
                    bundles += [(None, " " if kind in TITLES else "\n")] * bool(bundles) + more
            if kind in TITLES:
                self.emit("heading", bundles, anchor, level=1)
            else:
                self.emit(MARGINS[kind], bundles, anchor)
            return
        for p in paragraphs:
            bundles, weights, lines = self.runs(p)
            if kind is not None:
                self.emit("list_item" if listed(p, kind) else "paragraph", bundles, anchor)
                continue
            by_size, bold, total = Counter[float](), 0, 0  # 자리 표시자가 아닌 도형: 크기 규칙 재료
            for run, n in weights:
                size, heavy = self.sizes.run(run, p, body)
                by_size[step(size)] += n
                bold, total = bold + n * heavy, total + n
            self.doc.sizes.update(by_size)
            if listed(p, kind):
                self.emit("list_item", bundles, anchor)
                continue
            self.emit("paragraph", bundles, anchor, _cand=not self.titles, _lines=lines, _bold=bold * 2 > total,
                      _size=max(by_size, key=lambda v: (by_size[v], v)) if by_size else None,
                      _top=top(shape.element, shape.chain))

    def rescue(self) -> None:
        """한 번도 넣지 않은 보통 출처를 닻별로 모아 구조 문단으로(문서 순서). 같은 문단 출처는 잇고 문단은 \\n."""
        groups: dict[int, list[int]] = {}
        for k in self.ledger.unplaced():
            groups.setdefault(self.ledger.sources[k].anchor, []).append(k)
        rescued = 0
        for anchor, keys in groups.items():
            bundles: list[Bundle] = []
            for i, k in enumerate(keys):
                source = self.ledger.sources[k]
                if i and source.para != self.ledger.sources[keys[i - 1]].para:
                    bundles.append((None, "\n"))
                bundles.append((k, "".join(source.element.itertext())))
            if self.emit("paragraph", bundles, anchor, confidence=RESCUED_CONFIDENCE):
                rescued += sum(ink(text) for _, text in bundles)
        if rescued:
            self.ledger.rescued = rescued
            self.region("unassigned-text", "paragraph", UNASSIGNED_TEXT, next(iter(groups)),
                        gate("rescued", rescued, "==0"))


def listed(p: ET.Element, kind: str | None) -> bool:
    """목록 항목인가(스펙 §8.2): a:pPr의 글머리 표시가 먼저(a:buNone이면 아님), 없으면 body·obj 자리 표시자."""
    ppr = p.find(f"{A}pPr")
    if ppr is not None:
        if any(child.tag in BULLETS for child in ppr):
            return True
        if ppr.find(f"{A}buNone") is not None:
            return False
    return kind in LISTED


def _region(note: Note) -> Region:
    """Walker 기록 → 처리 이력. 문서 단위 기록(닻 None)의 위치는 첫 슬라이드 첫 자리다."""
    result = gate(note.name, note.value, None) if note.name is not None else None
    if note.anchor is None:
        return Region(note.tag, "paragraph", note.reason, 0, DOC_ANCHOR, result, f"doc-{note.tag}")
    return Region(note.tag, "paragraph", note.reason, note.section, note.anchor, result)


class PptxParser:
    mimes: tuple[str, ...] = (MIME,)
    extensions: tuple[str, ...] = (".pptx",)

    def parse(self, data: bytes, name: str) -> ParsedSource:
        return self._parse(data, name)[0]

    def _parse(self, data: bytes, name: str) -> tuple[ParsedSource, tuple[Counts, ...]]:
        """ParsedSource와 슬라이드별 장부 숫자(테스트가 본다)."""
        package = Package(data, name, "PPTX", MAIN)
        root, _ = package.part(package.main, f"{P}presentation")
        doc = _Doc(name, package, Walker(None), Images(package))
        default = root.find(f"{P}defaultTextStyle")
        slides = []
        for number, member in enumerate(self._slides(package, root), start=1):
            slide, prefixes = package.part(member, f"{P}sld")
            doc.held.append(slide)
            shapes, units = layout(slide, prefixes)
            ledger = Ledger()
            doc.walker.part = member
            titles = titles_of(shapes)
            for element, chain, index in units:
                title = index is not None and id(shapes[index].element) in titles
                anchor = anchor_of(number, index if index is not None else 0, title or index is None)
                doc.walker.walk(element, prefixes, ledger, number - 1, anchor, Story(), chain)
            slides.append((number, slide, prefixes, member, shapes, ledger))
        doc.walker.finish()
        for number, slide, prefixes, member, shapes, ledger in slides:
            if slide.get("show", "").lower() in ("0", "false"):
                doc.regions.append(Region("hidden-slide", "paragraph", HIDDEN_SLIDE, number - 1,
                                          anchor_of(number, 0, True)))
            master = doc.master(member)
            other = master.find(f"{P}txStyles/{P}otherStyle") if master is not None else None
            _Slide(doc, number, slide, prefixes, member, shapes, ledger, Sizes(other, default)).build()
        headings(doc.specs, doc.sizes, lambda anchor: anchor >> SHIFT)
        doc.regions.extend(_region(note) for note in doc.walker.notes)
        blocks, regions = finish(doc.specs, doc.regions, True, Slides())
        parsed = ParsedSource(mime=MIME, blocks=blocks, assets=doc.images.budget.assets, regions=regions)
        return parsed, tuple(doc.counts)

    @staticmethod
    def _slides(package: Package, root: ET.Element) -> list[str]:
        """p:sldIdLst/p:sldId@r:id 차례의 슬라이드 멤버. 관계가 없거나 슬라이드 관계가 아니거나 멤버가 없으면 ParseError."""
        rels = package.rels(package.main)
        location = f"{package.name}:{package.main}"
        out = []
        for ref in root.iterfind(f"{P}sldIdLst/{P}sldId"):
            rel = rels.get(ref.get(f"{{{R}}}id", ""))
            if rel is None or rel.type != f"{R}/slide" or rel.target is None:
                raise ParseError("slide relationship not found", location)
            if not package.has(rel.target):
                raise ParseError("missing zip member", f"{package.name}:{rel.target}")
            out.append(rel.target)
        return out
