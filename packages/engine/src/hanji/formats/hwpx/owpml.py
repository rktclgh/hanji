"""OWPML 구역 요소 트리(HWPX 이름공간 hs·hp·hc) → 블록 명세·처리 이력. HWPX 파서와 HWP 파서(레코드를 같은 모양의 트리로
옮겨 쓴다)가 함께 쓰는 처리부다(OWPML은 HWPX의 어휘라 hwpx 패키지에 둔다). 그림 바이트는 Pictures 프로토콜(HWPX는 zip
꾸러미, HWP는 BinData)로 읽는다.

구역마다 트리를 받아 블록 명세로 바꾼 뒤 버린다. 최상위 문단(hs:sec 바로 아래 hp:p)마다 문단 글자 블록(개요 수준 → 제목,
번호·글머리 → 목록 항목, 그 밖 문단)을 낸다. 위치는 FlowLocator(제목 경로, 최상위 문단 순번). 글자 장부(ledger.py)가
모든 hp:t·글맵시·덧말(본말·덧말)·글자 겹침 글자를 세고, 한 번도 넣지 않은 글자는 구역 끝 구조 문단(신뢰도 0.3)으로
살린다(처리 이력 unassigned_text). 덧말은 그 자리에 본말(덧말), 글자 겹침은 겹친 글자(원 모양이면 유니코드 원문자)로
문단 글자에 든다. 수식 스크립트(hp:script)도 출처지만 인쇄 글자가 아니라 따로 센다(같은 장부의 다른 숫자). 장부가 맞지
않으면 처리 이력 coverage_mismatch(파싱은 실패하지 않는다, 스크립트 숫자는 s{구역}-script-coverage). 개요 수준을 쓰지 않는
문서는 PDF와 같은 글꼴 크기 규칙으로 제목을 고른다(_finish).

문단에 박힌 개체는 문단 글자 블록 뒤에 나타난 순서대로 블록이 된다(HWPX 스펙 §5.2): 표(1×1은 상자라 풀고, 칸 안 개체는
칸 글자로, 칸 안 그림은 표 뒤 그림 블록), 그림과 캡션(PNG 자산), 머리말·꼬리말, 글상자·도형 글자, 수식 스크립트,
글맵시, OLE 등은 이미지 없는 그림. 수식·도형·묶음·글맵시·OLE 등의 캡션은 표 캡션처럼 TOP·LEFT면 개체 블록 앞, 아니면
뒤의 캡션 블록(연결 없음, 칸 안이면 칸 글자). 각주·미주는 구역 끝(닻 문단 위치). 개체 조립은 개체 중첩만큼만 재귀한다(XML 깊이
상한 256이 묶는다). 이름 앞 _는 형식 패키지(hwpx·hwp) 안에서만 쓴다는 뜻이다."""

import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from hanji_contracts import GateResult, RegionRecord, Table

from ...errors import ParseError
from ..flowdoc import Region, finish, gate, ledger_gate
from ..pdf.figures import AssetBudget
from ..pdf.group import MAX_LEVEL, RESCUED_CONFIDENCE, step
from ..pdf.parser import COVERAGE_MISMATCH, UNASSIGNED_TEXT
from . import images, tables
from .container import HC, HP, Item, OverBudget, TooLarge
from .header import CharShape, Styles
from .ledger import INLINE, SOURCE_CHARS, T, Bundle, Counts, Ledger, circled, ink, source_chars
from .tables import RawCell, number

P, RUN, SUBLIST = f"{HP}p", f"{HP}run", f"{HP}subList"
LINESEGS, LINESEG, LINE_BREAK = f"{HP}linesegarray", f"{HP}lineseg", f"{HP}lineBreak"
TEXTART, HIDDEN = f"{HP}textart", f"{HP}hiddenComment"
TBL, TR, TC, CELL_ADDR, CELL_SPAN = f"{HP}tbl", f"{HP}tr", f"{HP}tc", f"{HP}cellAddr", f"{HP}cellSpan"
PIC, IMG, CUR_SZ, CAPTION, CONTAINER = f"{HP}pic", f"{HC}img", f"{HP}curSz", f"{HP}caption", f"{HP}container"
ORG_SZ = f"{HP}orgSz"
DUTMAL, MAIN_TEXT, SUB_TEXT, COMPOSE = f"{HP}dutmal", f"{HP}mainText", f"{HP}subText", f"{HP}compose"
PRINTED = frozenset((f"{HP}t", MAIN_TEXT, SUB_TEXT, COMPOSE))  # 글상자 글자(shape_text)로 보는 출처
CTRL, DRAW_TEXT, EQUATION, SCRIPT = f"{HP}ctrl", f"{HP}drawText", f"{HP}equation", f"{HP}script"
SHAPES = frozenset(f"{HP}{n}" for n in ("rect", "ellipse", "arc", "polygon", "curve", "connectLine", "line"))
EMBEDDED = frozenset(f"{HP}{n}" for n in ("ole", "chart", "video"))  # 그림 성격 개체(이미지 없는 그림)
# 캡션을 표 캡션처럼 앞뒤에 내는 개체(그림은 이미지와 잇는 picture, 표는 table이 따로 한다)
CAPTIONED = SHAPES | EMBEDDED | {EQUATION, CONTAINER, TEXTART}
MARGINS = {f"{HP}header": "page_header", f"{HP}footer": "page_footer"}
NOTES = (f"{HP}footNote", f"{HP}endNote")  # 구역 끝에 이 차례로
FIGURE_LINKED = "figure_image_linked"  # isEmbeded="0"(외부 연결 그림: 경로를 따라가지 않는다)
FIGURE_MISSING = "figure_image_missing"  # id가 manifest에 없거나 href 멤버가 zip에 없다
FIGURE_BUDGET = "figure_image_budget"  # MAX_BINARY_TOTAL 또는 문서 그림 바이트 상한을 넘었다
EMBEDDED_OBJECT = "embedded_object"
NESTED_TABLE = "nested_table_flattened"  # 칸 안 표를 칸 글자로 펼쳤다(계약에 중첩 표가 없다)
SHAPE_TEXT = "shape_text"  # 글상자·도형·글맵시 글자를 문단으로 냈다(v1 범위 밖)
EQUATION_SCRIPT = "equation_script"  # 수식 스크립트 원문(한컴 수식 문법)을 문단으로 냈다
UNKNOWN_INLINE = "unknown_inline_element"  # hp:t 안 모르는 자식 요소의 글자를 그 자리에 넣었다
HIDDEN_COMMENT = "hidden_comment"  # 숨은 설명(인쇄되지 않음): 블록에 넣지 않았다
MASTERPAGE_TEXT = "masterpage_text_skipped"  # 마스터 쪽 글자(v1은 읽지 않는다)
NO_SHAPE = CharShape(10.0, False)  # 모르는 charPrIDRef


class Pictures(Protocol):
    """그림 원천: items는 그림 id → 항목(href, embedded), has·binary는 정확한 href로. binary는 상한 초과면 TooLarge·
    OverBudget, 읽기 오류면 ParseError(그 그림만 잃는다). HWPX는 container.Package가 구현한다."""

    items: Mapping[str, Item]

    def has(self, member: str) -> bool: ...

    def binary(self, member: str) -> bytes: ...


@dataclass(slots=True)
class _Doc:
    """문서 전체에서 모으는 것: 블록 명세(내부 키 _anchor 등은 _finish가 지운다), 처리 이력, 장부 숫자."""

    name: str
    styles: Styles
    package: Pictures
    budget: AssetBudget = field(default_factory=AssetBudget)
    pictures: dict[str, images.Png | str] = field(default_factory=dict)  # href → PNG 또는 이미지 없음 사유
    specs: list[dict[str, Any]] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    counts: list[Counts] = field(default_factory=list)
    outline: bool = False  # 개요 수준 제목을 하나라도 썼나(그러면 크기 규칙을 쓰지 않는다)


def _t_text(t: ET.Element) -> tuple[str, bool]:
    """hp:t의 글자(스펙 §5.1)와 모르는 자식 요소에 글자가 있었는지."""
    parts = [t.text or ""]
    unknown = False
    for child in t:
        # 아는 인라인 요소는 대치 글자를 넣고, 안에 글자가 있으면(모르는 자식처럼) 그 자리에 잇고 기록한다
        inner = "".join(child.itertext())
        parts.append(INLINE.get(child.tag, "") + inner)
        unknown = unknown or bool(inner.strip())
        parts.append(child.tail or "")
    return "".join(parts), unknown


def _source_text(element: ET.Element) -> str:
    """장부 출처 하나의 블록 글자(블록·살리기가 함께 쓴다): hp:t는 _t_text, 글맵시는 속성, 글자 겹침은 원 모양이면
    원문자(circled), 수식 스크립트는 text(처리부가 늘 읽던 자리), 덧말 본말·덧말은 안 글자. 장부 n은 ledger.SOURCE_CHARS가
    따로 센다."""
    if element.tag == SCRIPT:
        return element.text or ""
    if element.tag == T:
        return _t_text(element)[0]
    if element.tag == TEXTART:
        return element.get("text", "")
    if element.tag == COMPOSE:
        return circled(element.get("composeText", ""), element.get("circleType"))
    return "".join(element.itertext())


def _lines(p: ET.Element) -> int:
    """저장된 표시 줄 수(linesegarray의 lineseg 수), 없으면 줄바꿈 수 + 1."""
    segs = p.find(LINESEGS)
    if segs is not None and (count := len(segs.findall(LINESEG))):
        return count
    return sum(1 for run in p.iterfind(RUN) for t in run.iterfind(T) for _ in t.iterfind(LINE_BREAK)) + 1


def _join(bundles: list[Bundle]) -> str:
    return "".join(text for _, text in bundles)


def _box(tbl: ET.Element) -> ET.Element | None:
    """정말 1×1인 표(rowCnt·colCnt 1, 칸 하나, 주소 (0,0), span (1,1))의 칸 subList(없으면 빈 subList: 낼 글자가 없다).
    아니면 None(보통 표 검증으로)."""
    if number(tbl.get("rowCnt")) != 1 or number(tbl.get("colCnt")) != 1:
        return None
    cells = tbl.findall(f"{TR}/{TC}")
    if len(cells) != 1:
        return None
    addr, span = cells[0].find(CELL_ADDR), cells[0].find(CELL_SPAN)
    where = (number(addr.get("rowAddr")), number(addr.get("colAddr"))) if addr is not None else (None, None)
    size = (number(span.get("rowSpan"), 1), number(span.get("colSpan"), 1)) if span is not None else (1, 1)
    if where != (0, 0) or size != (1, 1):
        return None
    sub = cells[0].find(SUBLIST)
    return ET.Element(SUBLIST) if sub is None else sub


class _Section:
    """구역 하나를 블록 명세로 바꾼다. base는 이 구역 첫 최상위 문단의 문서 전체 순번."""

    def __init__(self, doc: _Doc, index: int, root: ET.Element, base: int) -> None:
        self.doc = doc
        self.index = index
        self.root = root
        self.base = base
        self.ledger = Ledger()
        self.late = False  # 구역 끝 블록을 내는 중(닻 문단 자리의 제목 경로를 쓴다)
        self.unknown: set[int] = set()  # 모르는 인라인 요소 기록을 남긴 문단(id)
        self.notes: list[tuple[ET.Element, int]] = []  # (각주·미주, 닻)
        self.inner = False  # 머리말·꼬리말·각주·미주·글상자 안(1×1 상자 문단도 제목 판정 없이 plain)
        self.scripts: set[int] = set()  # 수식 스크립트 출처(인쇄 글자와 따로 센다)
        self._sources()

    def _sources(self) -> None:
        """장부 출처를 문서 순서로 센다(스택 순회, 재귀 없음). 숨은 설명마다 처리 이력을 남긴다. 수식 스크립트는 그 출처
        순번을 scripts에 둔다."""
        anchor = self.base - 1
        for top in self.root:
            if top.tag == P:
                anchor += 1
            stack: list[tuple[ET.Element, bool, int]] = [(top, False, id(top))]
            while stack:
                element, hidden, para = stack.pop()
                if element.tag == P:
                    para = id(element)
                if element.tag == T:
                    self.ledger.add(element, source_chars(element), max(anchor, self.base), para, hidden)
                    continue
                if element.tag == SCRIPT:
                    self.scripts.add(len(self.ledger.sources))
                count = SOURCE_CHARS.get(element.tag)
                if count is not None:
                    self.ledger.add(element, count(element), max(anchor, self.base), para, hidden)
                if element.tag == HIDDEN and not hidden:
                    chars = sum(source_chars(t) for t in element.iter(T))
                    self.region("hidden-comment", "paragraph", HIDDEN_COMMENT, max(anchor, self.base),
                                gate("hidden_chars", chars, None))
                    hidden = True
                stack.extend((child, hidden, para) for child in reversed(element))

    def region(self, tag: str, kind: str, reason: str, anchor: int, gate: GateResult | None = None) -> None:
        self.doc.regions.append(Region(tag, kind, reason, self.index, anchor, gate))

    def bundles(self, p: ET.Element, anchor: int) -> list[Bundle]:
        """문단 hp:run들의 hp:t·덧말·글자 겹침 글자를 순서대로(개체 안 글자는 들지 않는다). 출처마다 묶음 하나."""
        out: list[Bundle] = []
        for run in p.iterfind(RUN):
            for child in run:
                if child.tag == T:
                    out.append(self.source_bundle(child, anchor, p))
                elif child.tag in (DUTMAL, COMPOSE):
                    out += self.ruby(child)
        return out

    def ruby(self, element: ET.Element) -> list[Bundle]:
        """덧말은 본말 뒤 괄호 안 덧말(덧말이 공백뿐이면 본말만), 글자 겹침은 겹친 글자(원 모양이면 원문자). 글자 요소가
        출처다(괄호는 끼운 구분자)."""
        if element.tag == COMPOSE:
            return [(self.ledger.source_of(element), _source_text(element))]
        out: list[Bundle] = []
        main, sub = element.find(MAIN_TEXT), element.find(SUB_TEXT)
        if main is not None:
            out.append((self.ledger.source_of(main), _source_text(main)))
        if sub is not None and _source_text(sub).strip():
            out += [(None, "("), (self.ledger.source_of(sub), _source_text(sub)), (None, ")")]
        return out

    def source_bundle(self, t: ET.Element, anchor: int, p: ET.Element) -> Bundle:
        text, unknown = _t_text(t)
        if unknown and id(p) not in self.unknown:
            self.unknown.add(id(p))
            self.region("unknown-inline", "paragraph", UNKNOWN_INLINE, anchor)
        return self.ledger.source_of(t), text

    def emit(self, kind: str, bundles: list[Bundle], anchor: int, **extra: Any) -> bool:
        """블록 하나를 낸다(공백만이면 내지 않는다). 낸 묶음을 장부에 넣는다."""
        text = _join(bundles).strip()
        if not text:
            return False
        self.doc.specs.append({"kind": kind, "text": text, "confidence": extra.pop("confidence", 1.0), "state": "det",
                               "text_source": "native", "_anchor": anchor, "_late": self.late, **extra})
        self.ledger.put(bundles)
        return True

    def paragraph(self, p: ET.Element, anchor: int) -> None:
        """최상위·1×1 상자 문단: 개요 수준 → 제목, 번호·글머리 → 목록 항목, 그 밖 문단(크기 규칙 후보). 그 뒤 박힌
        개체."""
        bundles = self.bundles(p, anchor)
        kind, level = self.doc.styles.headings.get(p.get("paraPrIDRef", ""), ("NONE", 0))
        if kind == "OUTLINE":
            if self.emit("heading", bundles, anchor, level=max(1, min(level + 1, MAX_LEVEL))):  # 음수 수준도 1~6 안으로
                self.doc.outline = True
        elif kind in ("NUMBER", "BULLET"):
            self.emit("list_item", bundles, anchor, _sizes=self.sizes(p))
        else:
            sizes = self.sizes(p)
            bold = sum(n for (_, b), n in sizes.items() if b)
            total = sum(sizes.values())
            by_size = Counter[float]()
            for (size, _), n in sizes.items():
                by_size[size] += n
            size = max(by_size, key=lambda s: (by_size[s], s)) if by_size else None
            self.emit("paragraph", bundles, anchor, _sizes=sizes, _size=size, _bold=bold * 2 > total,
                      _lines=_lines(p))
        self.objects(p, anchor)

    def plain(self, p: ET.Element, anchor: int) -> None:
        """제목·목록 판정 없는 문단(각주·미주·글상자) 하나와 그 안 개체."""
        self.emit("paragraph", self.bundles(p, anchor), anchor)
        self.objects(p, anchor)

    def objects(self, p: ET.Element, anchor: int) -> None:
        """문단에 박힌 개체를 나타난 순서대로 블록으로."""
        for run in p.iterfind(RUN):
            for child in run:
                self.obj(child, anchor)

    def obj(self, element: ET.Element, anchor: int) -> None:
        tag = element.tag
        caption = element.find(CAPTION) if tag in CAPTIONED else None
        before = caption is not None and caption.get("side") in ("TOP", "LEFT")
        if caption is not None and before:
            self.caption(caption, anchor)
        if tag == TBL:
            self.table(element, anchor)
        elif tag == PIC:
            self.picture(element, anchor)
        elif tag == CONTAINER:
            for child in element:
                self.obj(child, anchor)
        elif tag in SHAPES:
            self.shape(element, anchor)
        elif tag == EQUATION:
            if self.emit("paragraph", self.script(element), anchor):
                self.region("equation", "paragraph", EQUATION_SCRIPT, anchor)
        elif tag == TEXTART:
            if self.emit("paragraph", [(self.ledger.source_of(element), element.get("text", ""))], anchor):
                self.region("shape", "paragraph", SHAPE_TEXT, anchor)
        elif tag in EMBEDDED:
            self.figure(None, anchor)
            self.region("object", "figure", EMBEDDED_OBJECT, anchor)
        elif tag in MARGINS:
            self.margin(element, anchor)
        elif tag in NOTES:
            self.notes.append((element, anchor))
        elif tag == CTRL:
            for child in element:
                if child.tag in MARGINS or child.tag in NOTES:
                    self.obj(child, anchor)
        if caption is not None and not before:
            self.caption(caption, anchor)

    def script(self, equation: ET.Element) -> list[Bundle]:
        """수식의 스크립트 묶음(첫 hp:script, 장부 출처). 없으면 빈 목록."""
        script = equation.find(SCRIPT)
        return [] if script is None else [(self.ledger.source_of(script), _source_text(script))]

    def has_text(self, element: ET.Element) -> bool:
        """그 아래 인쇄 글자 출처(hp:t·덧말·글자 겹침) 가운데 n > 0인 것이 있나(shape_text 기록)."""
        index = self.ledger.index
        return any(self.ledger.sources[index[id(e)]].n for e in element.iter() if e.tag in PRINTED and id(e) in index)

    def shape(self, element: ET.Element, anchor: int) -> None:
        """도형의 글상자(drawText/subList) 문단마다 문단 블록. 글자가 있는 도형마다 shape_text."""
        sub = element.find(f"{DRAW_TEXT}/{SUBLIST}")
        if sub is None:
            return
        if self.has_text(sub):
            self.region("shape", "paragraph", SHAPE_TEXT, anchor)
        inner, self.inner = self.inner, True
        for p in sub.iterfind(P):
            self.plain(p, anchor)
        self.inner = inner

    def margin(self, element: ET.Element, anchor: int) -> None:
        """머리말·꼬리말: subList 문단 글자를 \\n으로 이은 블록 하나(놓인 자리에 한 번), 그 뒤 안의 개체."""
        sub = element.find(SUBLIST)
        paras = sub.findall(P) if sub is not None else []
        bundles: list[Bundle] = []
        for i, p in enumerate(paras):
            if i:
                bundles.append((None, "\n"))
            bundles += self.bundles(p, anchor)
        self.emit(MARGINS[element.tag], bundles, anchor)
        inner, self.inner = self.inner, True
        for p in paras:
            self.objects(p, anchor)
        self.inner = inner

    def end_notes(self) -> None:
        """구역 끝: 각주 전체, 그다음 미주 전체(본문 참조 순서). 문단마다 블록, 그 뒤 안의 개체."""
        inner, self.inner = self.inner, True
        seen: set[int] = set()
        while len(seen) < len(self.notes):  # 미주 안 각주처럼 지난 차례 뒤에 붙은 주석은 한 바퀴 더 돈다
            for tag in NOTES:
                k = 0
                while k < len(self.notes):  # 각주 안 각주가 뒤에 붙어도 돈다
                    note, anchor = self.notes[k]
                    k += 1
                    if note.tag != tag or k in seen:
                        continue
                    seen.add(k)
                    sub = note.find(SUBLIST)
                    if sub is not None:
                        for p in sub.iterfind(P):
                            self.plain(p, anchor)
        self.inner = inner

    def figure(self, figure: dict[str, Any] | None, anchor: int) -> dict[str, Any]:
        """그림 블록(글자 없음). figure가 None이면 이미지 없는 그림."""
        spec: dict[str, Any] = {"kind": "figure", "text": "", "confidence": 1.0, "state": "det",
                                "text_source": "native", "_anchor": anchor, "_late": self.late}
        if figure is not None:
            spec["figure"] = figure
        self.doc.specs.append(spec)
        return spec

    def caption(self, caption: ET.Element, anchor: int) -> dict[str, Any] | None:
        """캡션 subList 문단 글자를 \\n으로 이은 caption 블록(낸 명세, 공백뿐이면 None). 캡션 문단에 박힌 개체는
        캡션 블록 뒤에 블록으로 낸다(조용히 잃지 않는다)."""
        sub = caption.find(SUBLIST)
        paras = list(sub.iterfind(P)) if sub is not None else []
        bundles: list[Bundle] = []
        for i, p in enumerate(paras):
            if i:
                bundles.append((None, "\n"))
            bundles += self.bundles(p, anchor)
        found = self.doc.specs[-1] if self.emit("caption", bundles, anchor) else None
        for p in paras:
            self.objects(p, anchor)
        return found

    def picture(self, pic: ET.Element, anchor: int) -> None:
        """그림 블록과 캡션(TOP이면 앞, 아니면 뒤). 이미지가 있을 때만 caption_ref로 잇는다(스펙 §7.4)."""
        caption = pic.find(CAPTION)
        top = caption is not None and caption.get("side") == "TOP"
        found = self.caption(caption, anchor) if caption is not None and top else None
        image = self.image(pic, anchor)
        spec = self.figure(image, anchor)
        if caption is not None and not top:
            found = self.caption(caption, anchor)
        if image is not None and found is not None:
            spec["_caption"] = found

    def image(self, pic: ET.Element, anchor: int) -> dict[str, Any] | None:
        """hc:img → manifest → 멤버 → PNG 자산. 못 담으면 처리 이력을 남기고 None."""
        img = pic.find(IMG)
        item = self.doc.package.items.get(img.get("binaryItemIDRef", "") if img is not None else "")
        reason: str
        if item is not None and not item.embedded:
            reason = FIGURE_LINKED
        elif item is None or not self.doc.package.has(item.href):
            reason = FIGURE_MISSING
        else:
            result = self.doc.pictures.get(item.href)
            if result is None:
                try:
                    result = images.to_png(self.doc.package.binary(item.href))
                except OverBudget:
                    result = FIGURE_BUDGET
                except TooLarge:
                    result = images.TOO_LARGE
                except ParseError:  # 그림 멤버의 zip 오류(CRC 등): 그 그림만 잃는다
                    result = images.UNREADABLE
                self.doc.pictures[item.href] = result
            if isinstance(result, images.Png):
                key = self.doc.budget.add(result.data)
                if key is not None:
                    size, org = pic.find(CUR_SZ), pic.find(ORG_SZ)
                    shown = images.dpi(result.width, size.get("width") if size is not None else None,
                                       org.get("width") if org is not None else None)
                    return {"asset": key, "mime": "image/png", "width_px": result.width, "height_px": result.height,
                            "dpi": shown, "category": "image"}
                reason = FIGURE_BUDGET
                self.doc.pictures[item.href] = reason  # 담지 못한 PNG 바이트는 들고 있지 않는다(used는 줄지 않는다)
            else:
                reason = result
        self.region("figure", "figure", reason, anchor)
        return None

    def table(self, tbl: ET.Element, anchor: int) -> None:
        """1×1은 상자: 칸 문단을 이 자리의 본문 문단처럼(컨트롤·글상자 안이면 plain). 그 밖은 표 블록(칸 안 그림 등은
        표 뒤). 캡션은 TOP·LEFT면 앞."""
        caption = tbl.find(CAPTION)
        before = caption is not None and caption.get("side") in ("TOP", "LEFT")
        if caption is not None and before:
            self.caption(caption, anchor)
        sub = _box(tbl)
        if sub is not None:
            for p in sub.iterfind(P):
                (self.plain if self.inner else self.paragraph)(p, anchor)  # 컨트롤·글상자 안 상자는 제목 판정 밖
        else:
            self.grid(tbl, number(tbl.get("rowCnt")), number(tbl.get("colCnt")), anchor)
        if caption is not None and not before:
            self.caption(caption, anchor)

    def grid(self, tbl: ET.Element, rows: int | None, cols: int | None, anchor: int) -> None:
        after: list[ET.Element] = []
        cells: list[RawCell] = []
        parts: list[list[Bundle]] = []
        for tc in tbl.iterfind(f"{TR}/{TC}"):
            addr, span, sub = tc.find(CELL_ADDR), tc.find(CELL_SPAN), tc.find(SUBLIST)
            bundles = self.cell(sub, anchor, after) if sub is not None else []
            parts.append(bundles)
            cells.append(RawCell(
                row=number(addr.get("rowAddr") if addr is not None else None),
                col=number(addr.get("colAddr") if addr is not None else None),
                rowspan=number(span.get("rowSpan") if span is not None else None, 1),
                colspan=number(span.get("colSpan") if span is not None else None, 1),
                header=tc.get("header") == "1", filled=self.doc.styles.fills.get(tc.get("borderFillIDRef", ""), False),
                text=_join(bundles).strip()))
        table = tables.build(rows, cols, cells)
        if isinstance(table, Table):
            self.doc.specs.append({"kind": "table", "table": table, "text": table.plain_text(), "confidence": 1.0,
                                   "state": "det", "text_source": "native", "_anchor": anchor, "_late": self.late})
            for bundles in parts:
                self.ledger.put(bundles)
        else:
            joined: list[Bundle] = []
            for i, bundles in enumerate(parts):
                joined += [(None, "\n")] * bool(i) + bundles
            self.emit("paragraph", joined, anchor)
            self.region("table", "table", table, anchor)
        for element in after:
            self.obj(element, anchor)

    def cell(self, sub: ET.Element, anchor: int, after: list[ET.Element]) -> list[Bundle]:
        """칸 글자: 문단을 \\n으로 잇고, 박힌 개체는 문서 순서대로 그 자리에 글자로(앞뒤 \\n). 그림·OLE·머리말은 after로
        (표 뒤 블록), 각주·미주는 구역 끝."""
        out: list[Bundle] = []
        for i, p in enumerate(sub.iterfind(P)):
            if i:
                out.append((None, "\n"))
            for run in p.iterfind(RUN):
                for child in run:
                    if child.tag == T:
                        out.append(self.source_bundle(child, anchor, p))
                    elif child.tag in (DUTMAL, COMPOSE):
                        out += self.ruby(child)
                    else:
                        self.inline(child, anchor, after, out)
        return out

    def inline(self, element: ET.Element, anchor: int, after: list[ET.Element], out: list[Bundle]) -> None:
        """칸 안 개체 하나의 글자(앞뒤 \\n). 캡션 글자는 개체 글자 앞(TOP·LEFT)이나 뒤에 따로 묶는다(OLE 등은 after가
        표 뒤 블록으로)."""
        tag = element.tag
        inner: list[Bundle] = []
        caption = element.find(CAPTION) if tag in CAPTIONED and tag not in EMBEDDED else None
        lines = caption.find(SUBLIST) if caption is not None else None
        words = self.cell(lines, anchor, after) if lines is not None else []
        before = caption is not None and caption.get("side") in ("TOP", "LEFT")
        if before and _join(words).strip():
            out += [(None, "\n"), *words, (None, "\n")]
        if tag == TBL:
            inner = self.flatten(element, anchor, after)
        elif tag == CONTAINER:
            for child in element:
                self.inline(child, anchor, after, out)
        elif tag in SHAPES:
            sub = element.find(f"{DRAW_TEXT}/{SUBLIST}")
            if sub is not None:
                if self.has_text(sub):
                    self.region("shape", "paragraph", SHAPE_TEXT, anchor)
                inner = self.cell(sub, anchor, after)
        elif tag == EQUATION:
            script = self.script(element)
            if _join(script).strip():
                self.region("equation", "paragraph", EQUATION_SCRIPT, anchor)
                inner = script
        elif tag == TEXTART:
            if element.get("text", "").strip():
                self.region("shape", "paragraph", SHAPE_TEXT, anchor)
            inner = [(self.ledger.source_of(element), element.get("text", ""))]
        elif tag == PIC or tag in EMBEDDED or tag in MARGINS:
            after.append(element)
        elif tag in NOTES:
            self.notes.append((element, anchor))
        elif tag == CTRL:
            for child in element:
                self.inline(child, anchor, after, out)
        if inner:
            out += [(None, "\n"), *inner, (None, "\n")]
        if not before and _join(words).strip():
            out += [(None, "\n"), *words, (None, "\n")]

    def flatten(self, tbl: ET.Element, anchor: int, after: list[ET.Element]) -> list[Bundle]:
        """칸 안 표의 글자: 1×1은 그 문단 글자(기록 없음), 그 밖은 Table.plain_text()와 같은 모양(칸 \\t, 행 \\n)과
        nested_table_flattened. 캡션 글자는 TOP·LEFT면 앞, 아니면 뒤."""
        out: list[Bundle] = []
        caption = tbl.find(CAPTION)
        words = self.cell(caption.find(SUBLIST), anchor, after) if caption is not None and caption.find(
            SUBLIST) is not None else []
        before = caption is not None and caption.get("side") in ("TOP", "LEFT")
        if before and words:
            out += words + [(None, "\n")]
        sub = _box(tbl)
        if sub is not None:
            out += self.cell(sub, anchor, after)
        else:
            self.region("nested-table", "table", NESTED_TABLE, anchor)
            rows: dict[int, list[tuple[int, list[Bundle]]]] = {}
            for tc in tbl.iterfind(f"{TR}/{TC}"):
                addr, sub = tc.find(CELL_ADDR), tc.find(SUBLIST)
                row = number(addr.get("rowAddr") if addr is not None else None) or 0
                col = number(addr.get("colAddr") if addr is not None else None) or 0
                rows.setdefault(row, []).append((col, self.cell(sub, anchor, after) if sub is not None else []))
            for i, row in enumerate(sorted(rows)):
                if i:
                    out.append((None, "\n"))
                for k, (_, bundles) in enumerate(sorted(rows[row], key=lambda item: item[0])):
                    out += [(None, "\t")] * bool(k) + bundles
        if not before and words:
            out += [(None, "\n")] + words
        return out

    def sizes(self, p: ET.Element) -> Counter[tuple[float, bool]]:
        """문단 런들의 (0.5pt 단위 크기, 굵기)별 공백 아닌 글자 수."""
        out = Counter[tuple[float, bool]]()
        for run in p.iterfind(RUN):
            shape = self.doc.styles.chars.get(run.get("charPrIDRef", ""), NO_SHAPE)
            n = sum(ink(_t_text(t)[0]) for t in run.iterfind(T))
            if n:
                out[(step(shape.size), shape.bold)] += n
        return out

    def build(self) -> None:
        anchor = self.base - 1
        for top in self.root:
            if top.tag != P:
                continue
            anchor += 1
            self.paragraph(top, anchor)
        self.late = True
        self.end_notes()
        self.rescue()
        counts = self.ledger.count([k for k in range(len(self.ledger.sources)) if k not in self.scripts])
        self.doc.counts.append(counts)
        if not counts.ok:
            self.doc.regions.append(Region("coverage", "paragraph", COVERAGE_MISMATCH, self.index, self.base,
                                           ledger_gate(counts), f"s{self.index}-coverage"))
        scripts = self.ledger.count(self.scripts)  # 수식 스크립트: 빠지면 살리고 두 번 들면 이 숫자가 맞지 않는다
        if not scripts.ok:
            self.doc.regions.append(Region("script-coverage", "paragraph", COVERAGE_MISMATCH, self.index, self.base,
                                           ledger_gate(scripts), f"s{self.index}-script-coverage"))

    def rescue(self) -> None:
        """한 번도 넣지 않은 출처를 닻 문단별로 모아 구조 문단으로(문서 순서). 같은 문단 출처는 잇고 문단은 \\n."""
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
                bundles.append((k, _source_text(source.element)))
            if self.emit("paragraph", bundles, anchor, confidence=RESCUED_CONFIDENCE):
                rescued += sum(ink(text) for _, text in bundles)
        if rescued:
            self.ledger.rescued = rescued
            first = next(iter(groups))
            self.region("unassigned-text", "paragraph", UNASSIGNED_TEXT, first, gate("rescued", rescued, "==0"))


def _finish(doc: _Doc) -> tuple[tuple[dict[str, Any], ...], tuple[RegionRecord, ...]]:
    """공용 finish에 이 문서의 명세·처리 이력·개요 사용 여부를 넘긴다(테스트가 doc을 엿보는 자리)."""
    return finish(doc.specs, doc.regions, doc.outline)
