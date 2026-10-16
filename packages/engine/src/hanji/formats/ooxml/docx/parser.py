"""DOCX(WordprocessingML, zip + XML) → ParsedSource(스펙 §5). PDF로 바꾸지 않고 원본 구조를 결정론으로 읽는다.

본문 블록 항목(w:body 아래 w:p·w:tbl, w:sdt·w:customXml은 벗긴다)마다 블록을 낸다. 문단은 개요 수준·제목 스타일 →
제목, numPr → 목록 항목, 그 밖 문단(제목 스타일이 하나도 없는 문서는 PDF와 같은 글꼴 크기 규칙). 위치는 FlowLocator(제목
경로, 본문 항목 순번). 구역(w:sectPr)이 장부 단위다. ooxml/ledger.py의 Walker가 출처를 따로 열거해 보통·숨김·범위 밖으로
표시하고, 블록에는 보통 출처만 넣는다. 한 번도 넣지 않은 보통 출처는 구역 끝 구조 문단(신뢰도 0.3, unassigned_text)으로
살린다. 문단 안 개체(w:drawing·w:pict·w:object)는 문단 글자 뒤에 블록으로 낸다(스펙 §7.1, §7.4): 그림은 PNG 자산(관계 →
미디어 파트, drawing.Images), 글상자는 안 문단마다 문단 블록(제목·목록 판정 없음)과 shape_text, 묶음·캔버스는 자식을 차례로.
칸 안 개체는 표 블록 뒤에 낸다. 머리말·꼬리말(스펙 §7.2)은 구역이 처음 참조한 파트마다 구역 첫 항목 자리에 블록 하나(파트
문단 글자를 \\n으로)와 그 안 표·개체, 각주·미주(스펙 §7.3)는 구역 끝에 각주 전체·미주 전체를 참조 순서대로 낸다. 차트는
이미지 없는 그림과 풍부한 글자 문단(chart_text), SmartArt는 도형 글자 문단(smartart_text)이다(스펙 §8.4와 같은 규칙)."""

import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from hanji_contracts import Table

from ....errors import ParseError
from ...base import ParsedSource
from ...flowdoc import Region, finish, gate, ledger_gate
from ...flowledger import Bundle, Counts, Ledger, ink
from ...hwpx.parser import EMBEDDED_OBJECT, SHAPE_TEXT
from ...hwpx.tables import RawCell, build
from ...pdf.group import RESCUED_CONFIDENCE, step
from ...pdf.parser import COVERAGE_MISMATCH, UNASSIGNED_TEXT
from ...zipxml import Prefixes
from ..ledger import NORMAL, SOURCES, Note, Story, Walker, part_chars, sym_char
from ..drawing import (A, A_BR, A_P, A_T, CHART, CHART_ROOT, CHART_TEXT, DATA_ROOT, DRAWN_ROOT, FRAMES, PIC, R_DM, R_ID,
                       SMARTART, SMARTART_TEXT, WP, Images, blip, cache_chars, drawn_rel, emu, hidden_frame,
                       smartart_shapes)
from ..package import MC, READ, R, Package, choose, ignorable
from . import tables
from .styles import Styles, read_styles

MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAIN = f"{MIME}.main+xml"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
P, TBL, RUN, SDT, SDT_CONTENT = f"{W}p", f"{W}tbl", f"{W}r", f"{W}sdt", f"{W}sdtContent"
CUSTOM_XML, SECT_PR, BODY = f"{W}customXml", f"{W}sectPr", f"{W}body"
T, INSTR, SYM, BR, CR = f"{W}t", f"{W}instrText", f"{W}sym", f"{W}br", f"{W}cr"
ALTERNATE = f"{MC}AlternateContent"
DRAWING, PICT, OBJECT, TXBX = f"{W}drawing", f"{W}pict", f"{W}object", f"{W}txbxContent"
OBJECTS = frozenset({DRAWING, PICT, OBJECT})  # 문단 안 개체 뿌리(문단 글자 뒤 블록)
DELETED = frozenset({f"{W}del", f"{W}moveFrom"})
WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
WPG = "{http://schemas.microsoft.com/office/word/2010/wordprocessingGroup}"
WPC = "{http://schemas.microsoft.com/office/word/2010/wordprocessingCanvas}"
V = "{urn:schemas-microsoft-com:vml}"
GROUPS = frozenset({f"{WPG}wgp", f"{WPG}grpSp", f"{WPC}wpc"})  # 자식을 차례로 읽는 묶음·캔버스
ITEMS = frozenset({P, TBL})
Chain = tuple[ET.Element, ...]  # w:body와 본문 층 요소 사이의 감싼 요소들(위에서 아래로)
# 문단 안에서 벗기는 감싼 요소(스펙 §5.2). w:sdt는 w:sdtContent만 읽는다
WRAPPERS = frozenset(f"{W}{n}" for n in ("hyperlink", "smartTag", "customXml", "ins", "fldSimple", "moveTo", "dir",
                                          "bdo", "sdtContent"))
MATH_RUN = f"{M}r"
SEPARATORS = {f"{W}tab": "\t", f"{W}ptab": "\t", BR: "\n", CR: "\n", f"{W}noBreakHyphen": "‑",
              f"{W}softHyphen": ""}
# 글자가 없는 아는 런 자식(개체·필드 표시·주석 참조는 글자가 아니다. 개체 안 글자는 Walker가 따로 센다)
SILENT = frozenset(f"{W}{n}" for n in (
    "rPr", "fldChar", "lastRenderedPageBreak", "footnoteReference", "endnoteReference", "footnoteRef", "endnoteRef",
    "separator", "continuationSeparator", "annotationRef", "commentReference", "delText", "delInstrText", "drawing",
    "pict", "object", "pgNum", "dayShort", "dayLong", "monthShort", "monthLong", "yearShort", "yearLong",
    "contentPart", "noProof"))
NOTE_REFS = {f"{W}footnoteReference": "footnotes", f"{W}endnoteReference": "endnotes"}
MARGIN_REFS = {f"{W}headerReference": ("header", f"{W}hdr"), f"{W}footerReference": ("footer", f"{W}ftr")}
NOTES = {"footnotes": (f"{W}footnotes", f"{W}footnote"), "endnotes": (f"{W}endnotes", f"{W}endnote")}
SPECIAL_NOTES = frozenset({"separator", "continuationSeparator", "continuationNotice"})
UNKNOWN_INLINE = "unknown_inline_element"  # 모르는 런 자식 안 글자를 그 자리에 넣었다
COMMENTS_SKIPPED = "comments_skipped"  # 메모(인쇄되지 않음)는 범위 밖
DRAWN_TYPE = "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing"
UNREFERENCED = "unreferenced_part_skipped"  # 참조되지 않은 머리말·꼬리말 파트와 주석은 범위 밖
MARGIN_KINDS = {"header": "page_header", "footer": "page_footer"}
Story_ = tuple[str, ET.Element, str, Prefixes]  # (블록 종류·주석 종류, 요소, 파트 멤버, 그 파트 접두어)
NESTED_TABLE = "nested_table_flattened"  # 칸 안 표를 칸 글자로 펼쳤다(계약에 중첩 표가 없다)
MERGE_REPAIRED = "table_merge_repaired"  # 짝 없는 세로 병합 continue를 보통 칸으로 두었다
CELLS_PADDED = "table_cells_padded"  # gridBefore·gridAfter·짧은 행의 빈자리를 빈 칸으로 채웠다(근사)
MERGED_TEXT = "merged_cell_text_kept"  # 이어진 칸의 글자를 시작 칸 글자 뒤에 이었다


@dataclass(slots=True)
class _Doc:
    """문서 전체에서 모으는 것: 블록 명세(내부 키 _anchor 등은 finish가 지운다), 처리 이력, 장부 숫자."""

    name: str
    package: Package
    styles: Styles
    walker: Walker
    prefixes: Prefixes
    images: Images
    specs: list[dict[str, Any]] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    counts: list[Counts] = field(default_factory=list)
    outline: bool = False  # 개요 수준·제목 스타일 제목을 하나라도 썼나(그러면 크기 규칙을 쓰지 않는다)
    # _stories가 연 파트 뿌리: Walker·장부가 id(w:p)로 문단을 가리키므로 파싱이 끝날 때까지 붙잡아 id 재사용을 막는다
    held: list[ET.Element] = field(default_factory=list)
    margins: dict[int, list[tuple[int, Story_]]] = field(default_factory=dict)  # 구역 → (닻, 머리말·꼬리말 파트)
    notes: dict[int, list[tuple[int, Story_]]] = field(default_factory=dict)  # 구역 → (닻, 주석) 참조 차례
    unreferenced: int = 0  # 참조되지 않은 머리말·꼬리말 파트·주석의 글자
    charts: dict[int, ET.Element] = field(default_factory=dict)  # id(c:chart) → 그 호스트 자리에서 연 차트 파트 뿌리
    smartart: dict[int, ET.Element] = field(default_factory=dict)  # id(dgm:relIds) → 고른 파트(그려진 파트·데이터) 뿌리


def _text(element: ET.Element) -> str:
    """블록에 넣는 출처 글자(장부와 따로 만든다): w:sym은 장부와 같은 코드 범위 규칙(ledger.sym_char: 0·대리 영역·
    범위 밖은 빈 글자), 그 밖은 요소 안 글자."""
    if element.tag != SYM:
        return "".join(element.itertext())
    return sym_char(element)


def _join(bundles: list[Bundle]) -> str:
    return "".join(text for _, text in bundles)


def sections(body: ET.Element, prefixes: Prefixes | None = None,
             ancestors: tuple[ET.Element, ...] = ()) -> list[tuple[list[ET.Element], ET.Element | None]]:
    """본문 블록 항목을 구역마다 (항목들, 그 구역의 w:sectPr). prefixes는 mc:Choice@Requires·mc:Ignorable 접두어를 푸는
    데 쓰고, ancestors는 w:body 위 요소들(파트 뿌리: 그 mc:Ignorable이 본문 층에 미친다)."""
    return [([e for e, _, item in entries if item], sect) for entries, sect in _layout(body, prefixes or {}, ancestors)]


Entry = tuple[ET.Element, Chain, bool]  # (본문 층 요소, 감싼 요소들, 본문 블록 항목인가)


def _layout(body: ET.Element, prefixes: Prefixes,
            ancestors: tuple[ET.Element, ...] = ()) -> list[tuple[list[Entry], ET.Element | None]]:
    """본문 층을 구역마다 ([(요소, w:body와 요소 사이 감싼 요소들, 본문 블록 항목인가)], w:sectPr)로(스택 순회). 요소는
    본문 블록 항목(w:p·w:tbl), 고르지 않은 mc 쪽 통째, 무시 표시(mc:Ignorable, ProcessContent 아님) 요소 통째, 항목 밖
    출처다(Walker가 감싼 요소들을 조상으로 모두 훑어 통째 요소 안 글자를 범위 밖으로 표시한다). 통째 요소는 항목 순번을
    받지 않고 그 안 w:sectPr은 구역을 나누지 않는다. w:sdt/w:sdtContent·w:customXml·고른 mc 쪽·ProcessContent 요소·그 밖
    요소는 벗긴다."""
    out: list[tuple[list[Entry], ET.Element | None]] = []
    entries: list[Entry] = []
    last: ET.Element | None = None
    ignored: frozenset[str] = frozenset()
    process: frozenset[tuple[str, str]] = frozenset()
    for element in (*ancestors, body):
        more, also = ignorable(element, prefixes)
        ignored, process = ignored | more, process | also
    Marks = tuple[frozenset[str], frozenset[tuple[str, str]]]
    # (요소, 감싼 요소들, 벗기지 않고 통째로 내는 고르지 않은 mc 쪽인가, 위에서 이어받은 무시 표시)
    stack: list[tuple[ET.Element, Chain, bool, Marks]] = [(child, (), False, (ignored, process))
                                                         for child in reversed(body)]
    while stack:
        element, chain, whole, (ignored, process) = stack.pop()
        tag = element.tag
        more, also = ignorable(element, prefixes)
        marks = (ignored | more, process | also)
        uri, _, local = tag[1:].partition("}") if tag.startswith("{") else ("", "", tag)
        if not whole and uri in marks[0] and (uri, local) not in marks[1] and (uri, "*") not in marks[1]:
            whole = True  # 무시하는 요소: 안까지 읽지 않는다(글자는 Walker가 범위 밖으로)
        if whole or tag in SOURCES:
            entries.append((element, chain, False))
        elif tag in ITEMS:
            entries.append((element, chain, True))
            sect = element.find(f"{W}pPr/{SECT_PR}") if tag == P else None
            if sect is not None:
                out.append((entries, sect))
                entries = []
        elif tag == SECT_PR:
            last = element
        elif tag == SDT:
            content = element.find(SDT_CONTENT)
            if content is not None:
                stack.extend((child, (*chain, element, content), False, marks) for child in reversed(content))
        elif tag == ALTERNATE:
            chosen = choose(element, prefixes)
            for child in reversed(element):
                if child is chosen:
                    stack.extend((inner, (*chain, element, child), False, marks) for inner in reversed(child))
                else:
                    stack.append((child, (*chain, element), True, marks))
        else:
            stack.extend((child, (*chain, element), False, marks) for child in reversed(element))
    out.append((entries, last))
    return out


class _Section:
    """구역 하나를 블록 명세로 바꾼다. items는 (본문 항목 순번, 항목)."""

    def __init__(self, doc: _Doc, index: int, items: list[tuple[int, ET.Element]], ledger: Ledger) -> None:
        self.doc = doc
        self.index = index
        self.items = items
        self.ledger = ledger
        self.late = False  # 구역 끝 블록을 내는 중(닻 문단 자리의 제목 경로를 쓴다)
        self.inner = False  # 글상자·머리말·각주 안(문단은 제목·목록 판정 없이, 1×1 상자 안도)
        self.after: list[ET.Element] = []  # 지금 만드는 표의 칸 안 개체(표 블록 뒤에 낸다)
        self.host = doc.package.main  # 지금 읽는 파트(그림 관계를 이 파트에서 푼다)
        self.prefixes = doc.prefixes  # 그 파트의 접두어 선언(mc:Choice@Requires)

    def region(self, tag: str, kind: str, reason: str, anchor: int, result: Any = None) -> None:
        self.doc.regions.append(Region(tag, kind, reason, self.index, anchor, result))

    def emit(self, kind: str, bundles: list[Bundle], anchor: int, **extra: Any) -> bool:
        """블록 하나를 낸다(공백만이면 내지 않는다). 낸 묶음을 장부에 넣는다."""
        text = _join(bundles).strip()
        if not text:
            return False
        self.doc.specs.append({"kind": kind, "text": text, "confidence": extra.pop("confidence", 1.0), "state": "det",
                               "text_source": "native", "_anchor": anchor, "_late": self.late, **extra})
        self.ledger.put(bundles)
        return True

    def content(self, p: ET.Element, anchor: int) -> tuple[list[Bundle], Counter[tuple[float, bool]], int]:
        """문단 글자(스펙 §5.2): 묶음, (0.5pt 크기, 굵기)별 공백 아닌 글자 수, 줄 수(줄 나눔 + 1). 보통 출처만 넣고, 구분
        글자(탭·줄바꿈 등)는 숨긴 런·고르지 않은 mc 쪽에서는 넣지 않는다. 모르는 런 자식 안 출처는 그 자리에 넣는다."""
        out: list[Bundle] = []
        sizes = Counter[tuple[float, bool]]()
        lines = 1
        unknown = False
        stack: list[tuple[ET.Element, ET.Element | None]] = [(child, None) for child in reversed(p)]
        while stack:
            element, run = stack.pop()
            tag = element.tag
            if run is None:  # 문단 층
                if tag == RUN:
                    stack.extend((child, element) for child in reversed(element))
                elif tag in WRAPPERS:
                    stack.extend((child, None) for child in reversed(element))
                elif tag == SDT:
                    content = element.find(SDT_CONTENT)
                    if content is not None:
                        stack.extend((child, None) for child in reversed(content))
                elif tag == ALTERNATE:
                    chosen = choose(element, self.prefixes)
                    if chosen is not None:
                        stack.extend((child, None) for child in reversed(chosen))
                elif tag == MATH_RUN:
                    stack.extend((child, element) for child in reversed(element))
                elif tag.startswith(M):
                    # 수식 구조(m:oMath·m:f·m:num 등)는 벗긴다: 안의 m:r과 w:ins·w:hyperlink·w:sdt 런이 제자리에 든다
                    stack.extend((child, None) for child in reversed(element))
                elif tag.startswith("{") and tag[1:].partition("}")[0] not in READ:
                    # 읽지 않는 이름공간 요소는 벗긴다: 무시 표시면 안 출처가 범위 밖이라 들지 않고, ProcessContent면 든다
                    stack.extend((child, None) for child in reversed(element))
                continue
            if tag in SOURCES:
                self.piece(element, p, run, out, sizes)
            elif tag in SEPARATORS:
                if not self.doc.styles.run(p, run)[2]:
                    out.append((None, SEPARATORS[tag]))
                    if tag == CR or (tag == BR and element.get(f"{W}type", "textWrapping") == "textWrapping"):
                        lines += 1
            elif tag == ALTERNATE:
                chosen = choose(element, self.prefixes)
                if chosen is not None:
                    stack.extend((child, run) for child in reversed(chosen))
            elif tag not in SILENT:
                found = [e for e in element.iter() if e.tag in SOURCES]
                before = len(out)
                for source in found:
                    self.piece(source, p, run, out, sizes)
                if any(text.strip() for _, text in out[before:]):
                    unknown = True
        if unknown:
            self.region("unknown-inline", "paragraph", UNKNOWN_INLINE, anchor)
        return out, sizes, lines

    def piece(self, source: ET.Element, p: ET.Element, run: ET.Element, out: list[Bundle],
              sizes: Counter[tuple[float, bool]]) -> None:
        """보통 출처 하나를 묶음으로 넣고 크기·굵기별 글자 수를 센다(숨김·범위 밖은 넣지 않는다)."""
        if self.doc.walker.marks.get(id(source)) != NORMAL:
            return
        text = _text(source)
        out.append((self.ledger.source_of(source), text))
        if n := ink(text):
            size, bold, _ = self.doc.styles.run(p, run)
            sizes[(step(size), bold)] += n

    def paragraph(self, p: ET.Element, anchor: int) -> None:
        """본문 항목 문단: 개요 수준·제목 스타일 → 제목, 목록 → 목록 항목, 그 밖 문단(크기 규칙 후보). 그 뒤 박힌
        개체. 글상자·머리말·각주 안이면 판정 없는 문단."""
        if self.inner:
            self.plain(p, anchor)
            return
        bundles, sizes, lines = self.content(p, anchor)
        level = self.doc.styles.heading(p)
        if level is not None:
            if self.emit("heading", bundles, anchor, level=level):
                self.doc.outline = True
        elif self.doc.styles.listed(p):
            self.emit("list_item", bundles, anchor, _sizes=sizes)
        else:
            bold = sum(n for (_, b), n in sizes.items() if b)
            by_size = Counter[float]()
            for (size, _), n in sizes.items():
                by_size[size] += n
            size = max(by_size, key=lambda s: (by_size[s], s)) if by_size else None
            self.emit("paragraph", bundles, anchor, _sizes=sizes, _size=size, _bold=bold * 2 > sum(sizes.values()),
                      _lines=lines)
        self.place(self.objects(p), anchor)

    def plain(self, p: ET.Element, anchor: int) -> None:
        """제목·목록 판정 없는 문단(글상자·머리말·각주 안) 하나와 그 안 개체."""
        self.emit("paragraph", self.content(p, anchor)[0], anchor)
        self.place(self.objects(p), anchor)

    def objects(self, p: ET.Element) -> list[ET.Element]:
        """문단 안 개체 뿌리(w:drawing·w:pict·w:object)를 문서 순서로. 고른 mc 쪽만 보고, 변경 추적 삭제·읽지 않는
        이름공간 요소 안은 보지 않는다(그 안 글자는 Walker가 범위 밖으로 표시한다)."""
        out: list[ET.Element] = []
        stack = list(reversed(p))
        while stack:
            element = stack.pop()
            tag = element.tag
            if tag in OBJECTS:
                out.append(element)
            elif tag == ALTERNATE:
                chosen = choose(element, self.prefixes)
                if chosen is not None:
                    stack.extend(reversed(chosen))
            elif tag not in DELETED and tag[1:].partition("}")[0] in READ:
                stack.extend(reversed(element))
        return out

    def place(self, found: list[ET.Element], anchor: int) -> None:
        """개체 블록을 나타난 순서대로. 숨긴 그림 틀(wp:docPr@hidden)은 내지 않는다."""
        for element in found:
            if element.tag != DRAWING:
                self.vml(element, anchor)
                continue
            for frame in element:
                data = frame.find(f"{A}graphic/{A}graphicData")
                if frame.tag not in FRAMES or hidden_frame(frame) or data is None:
                    continue
                extent = frame.find(f"{WP}extent")
                for child in data:
                    self.graphic(child, anchor, emu(extent.get("cx")) if extent is not None else None, True)

    def graphic(self, element: ET.Element, anchor: int, cx: int | None, top: bool) -> None:
        """DrawingML 개체 하나(재귀는 묶음 중첩만큼, XML 깊이가 묶는다). top은 a:graphicData 바로 아래인가: 거기서
        모르는 개체는 이미지 없는 그림 + embedded_object, 묶음 안 모르는 요소(묶음 속성 등)는 건너뛴다."""
        tag = element.tag
        if tag == f"{PIC}pic":
            if cx is None:  # 묶음 안 그림: 그림 자신의 표시 너비
                ext = element.find(f"{PIC}spPr/{A}xfrm/{A}ext")
                cx = emu(ext.get("cx")) if ext is not None else None
            embed, link = blip(element)
            self.image(self.doc.images.figure(self.host, embed, link, cx), anchor)
        elif tag == f"{WPS}wsp":
            content = element.find(f"{WPS}txbx/{TXBX}")
            if content is not None:
                self.textbox(content, anchor)
        elif tag in GROUPS:
            for child in element:
                self.graphic(child, anchor, None, False)
        elif tag == f"{WPG}graphicFrame":
            data = element.find(f"{A}graphic/{A}graphicData")
            for child in data if data is not None else ():
                self.graphic(child, anchor, None, True)
        elif tag == ALTERNATE:
            chosen = choose(element, self.prefixes)
            for child in chosen if chosen is not None else ():
                self.graphic(child, anchor, cx, top)
        elif tag == CHART:
            self.chart(element, anchor)
        elif tag == SMARTART and id(element) in self.doc.smartart:
            self.smartart(self.doc.smartart[id(element)], anchor)
        elif top:
            self.figure(None, anchor)
            self.region("object", "figure", EMBEDDED_OBJECT, anchor)

    def vml(self, element: ET.Element, anchor: int) -> None:
        """w:pict·w:object 안 VML 도형(v:group은 펼친다): v:imagedata → 그림, v:textbox → 글상자. w:object(OLE)는
        미리보기 그림(없으면 이미지 없는 그림)과 embedded_object."""
        ole = element.tag == OBJECT
        shown = False
        stack = list(reversed(element))
        while stack:
            shape = stack.pop()
            if shape.tag == f"{V}group":
                stack.extend(reversed(shape))
                continue
            if not shape.tag.startswith(V) or shape.tag == f"{V}shapetype":
                continue
            data = shape.find(f"{V}imagedata")
            if data is not None:
                self.image(self.doc.images.figure(self.host, data.get(R_ID), None, None), anchor)
                if ole:
                    self.region("object", "figure", EMBEDDED_OBJECT, anchor)
                shown = True
            content = shape.find(f"{V}textbox/{TXBX}")
            if content is not None:
                self.textbox(content, anchor)
        if ole and not shown:
            self.figure(None, anchor)
            self.region("object", "figure", EMBEDDED_OBJECT, anchor)

    def chart(self, element: ET.Element, anchor: int) -> None:
        """차트: 이미지 없는 그림 + embedded_object, 차트 파트가 있으면 풍부한 글자(a:p를 \\n으로) 문단 하나와
        chart_text(근거 cache_chars: 캐시 문자열 글자 수, 범위 밖). 글자 없는 a:p(c:txPr의 빈 문단 등)는 건너뛴다."""
        self.figure(None, anchor)
        self.region("object", "figure", EMBEDDED_OBJECT, anchor)
        root = self.doc.charts.get(id(element))
        if root is None:
            return
        bundles: list[Bundle] = []
        for paragraph in (self.rich(p) for p in root.iter(A_P)):
            if sum(ink(text) for _, text in paragraph):
                bundles += [(None, "\n")] * bool(bundles) + paragraph
        self.emit("paragraph", bundles, anchor)
        self.region("chart", "paragraph", CHART_TEXT, anchor, gate("cache_chars", cache_chars(root), None))

    def smartart(self, root: ET.Element, anchor: int) -> None:
        """SmartArt: 고른 파트의 도형(글 몸통)마다 a:p 문단 블록, 보통 글자가 있는 도형마다 smartart_text."""
        for body in smartart_shapes(root):
            if self.has_text(body):
                self.region("smartart", "paragraph", SMARTART_TEXT, anchor)
            for p in body.iterfind(A_P):
                self.emit("paragraph", self.rich(p), anchor)

    def rich(self, p: ET.Element) -> list[Bundle]:
        """DrawingML 문단(a:p) 글자: 보통 a:t를 차례로, a:br은 \\n."""
        out: list[Bundle] = []
        for element in p.iter():
            if element.tag == A_T and self.doc.walker.marks.get(id(element)) == NORMAL:
                out.append((self.ledger.source_of(element), _text(element)))
            elif element.tag == A_BR:
                out.append((None, "\n"))
        return out

    def image(self, result: dict[str, Any] | str, anchor: int) -> None:
        """그림 블록(figure 값) 또는 이미지 없는 그림과 그 사유."""
        if isinstance(result, str):
            self.figure(None, anchor)
            self.region("figure", "figure", result, anchor)
        else:
            self.figure(result, anchor)

    def figure(self, figure: dict[str, Any] | None, anchor: int) -> None:
        """그림 블록(글자 없음). figure가 None이면 이미지 없는 그림."""
        spec: dict[str, Any] = {"kind": "figure", "text": "", "confidence": 1.0, "state": "det",
                                "text_source": "native", "_anchor": anchor, "_late": self.late}
        if figure is not None:
            spec["figure"] = figure
        self.doc.specs.append(spec)

    def textbox(self, content: ET.Element, anchor: int) -> None:
        """글상자 내용(w:txbxContent): 항목마다 문단(판정 없음)·표 블록. 보통 글자가 있으면 shape_text."""
        if self.has_text(content):
            self.region("shape", "paragraph", SHAPE_TEXT, anchor)
        inner, self.inner = self.inner, True
        for item in tables.cell_items(content):
            (self.paragraph if item.tag == P else self.table)(item, anchor)
        self.inner = inner

    def has_text(self, element: ET.Element) -> bool:
        ledger = self.ledger
        return any(ledger.sources[k].n and not ledger.sources[k].hidden for e in element.iter()
                   if (k := ledger.index.get(id(e))) is not None)

    def table(self, tbl: ET.Element, anchor: int) -> None:
        """1×1은 상자: 칸 항목을 이 자리의 본문 항목처럼(문단은 제목·목록 판정, 글상자·머리말·각주 안이면 판정
        없이). 그 밖은 표 블록, 검증 실패·상한 초과는 칸 글자를 문서 순서로 \\n 이은 문단과 처리 이력. 칸 안 개체는
        그 뒤에 블록으로."""
        box = tables.is_box(tbl)
        if box is not None:
            for item in tables.cell_items(box):
                (self.paragraph if item.tag == P else self.table)(item, anchor)
            return
        after, self.after = self.after, []
        self.grid(tbl, anchor)
        found, self.after = self.after, after
        self.place(found, anchor)

    def grid(self, tbl: ET.Element, anchor: int) -> None:
        tcs = [tc for _, row in tables.rows(tbl) for tc in row]
        parts = {id(tc): self.cell(tc, anchor) for tc in tcs}  # 칸 글자는 문서 순서로 한 번만 만든다
        shape = tables.grid(tbl)
        table, kept = (shape, False) if isinstance(shape, str) else _assemble(shape, parts)
        if isinstance(table, Table) and not isinstance(shape, str):
            self.doc.specs.append({"kind": "table", "table": table, "text": table.plain_text(), "confidence": 1.0,
                                   "state": "det", "text_source": "native", "_anchor": anchor, "_late": self.late})
            for tc in tcs:
                self.ledger.put(parts[id(tc)])
            for flag, reason in ((shape.repaired, MERGE_REPAIRED), (shape.padded, CELLS_PADDED), (kept, MERGED_TEXT)):
                if flag:
                    self.region("table", "table", reason, anchor)
            return
        joined: list[Bundle] = []
        for i, tc in enumerate(tcs):
            joined += [(None, "\n")] * bool(i) + parts[id(tc)]
        self.emit("paragraph", joined, anchor)
        self.region("table", "table", str(table), anchor)

    def cell(self, tc: ET.Element, anchor: int) -> list[Bundle]:
        """칸 글자: 칸 항목(문단·표)을 \\n으로 잇는다(제목 판정 없음). 칸 안 표는 펼친다."""
        out: list[Bundle] = []
        for i, item in enumerate(tables.cell_items(tc)):
            if i:
                out.append((None, "\n"))
            if item.tag == P:
                out += self.content(item, anchor)[0]
                self.after += self.objects(item)
            else:
                out += self.flatten(item, anchor)
        return out

    def flatten(self, tbl: ET.Element, anchor: int) -> list[Bundle]:
        """칸 안 표의 글자: 1×1은 그 칸 글자(기록 없음), 그 밖은 칸 \\t, 행 \\n(w:tr·w:tc 문서 순서)과
        nested_table_flattened."""
        box = tables.is_box(tbl)
        if box is not None:
            return self.cell(box, anchor)
        self.region("nested-table", "table", NESTED_TABLE, anchor)
        out: list[Bundle] = []
        for r, (_, tcs) in enumerate(tables.rows(tbl)):
            if r:
                out.append((None, "\n"))
            for c, tc in enumerate(tcs):
                out += [(None, "\t")] * bool(c) + self.cell(tc, anchor)
        return out

    def use(self, member: str, prefixes: Prefixes) -> tuple[str, Prefixes]:
        """읽는 파트를 바꾸고 앞의 (파트, 접두어)를 돌려준다."""
        before = (self.host, self.prefixes)
        self.host, self.prefixes = member, prefixes
        return before

    def margins(self) -> None:
        """머리말·꼬리말: 파트 문단 글자를 \\n으로 이은 블록 하나, 그 뒤 안의 표·개체(판정 없음)."""
        inner, self.inner = self.inner, True
        for anchor, (kind, root, member, prefixes) in self.doc.margins.get(self.index, []):
            before = self.use(member, prefixes)
            items = tables.cell_items(root)
            bundles: list[Bundle] = []
            for i, p in enumerate(item for item in items if item.tag == P):
                bundles += [(None, "\n")] * bool(i) + self.content(p, anchor)[0]
            self.emit(kind, bundles, anchor)
            for item in items:
                if item.tag == P:
                    self.place(self.objects(item), anchor)
                else:
                    self.table(item, anchor)
            self.use(*before)
        self.inner = inner

    def end_notes(self) -> None:
        """구역 끝: 각주 전체, 그다음 미주 전체(참조 순서). 주석 항목마다 문단(판정 없음)·표, 그 뒤 안의 개체."""
        inner, self.inner = self.inner, True
        for wanted in NOTES:
            for anchor, (kind, note, member, prefixes) in self.doc.notes.get(self.index, []):
                if kind != wanted:
                    continue
                before = self.use(member, prefixes)
                for item in tables.cell_items(note):
                    (self.paragraph if item.tag == P else self.table)(item, anchor)
                self.use(*before)
        self.inner = inner

    def build(self) -> None:
        self.margins()
        for anchor, item in self.items:
            if item.tag == P:
                self.paragraph(item, anchor)
            else:
                self.table(item, anchor)
        self.late = True
        self.end_notes()
        self.rescue()
        counts = self.ledger.count()
        self.doc.counts.append(counts)
        if not counts.ok:
            base = self.items[0][0] if self.items else 0
            self.doc.regions.append(Region("coverage", "paragraph", COVERAGE_MISMATCH, self.index, base,
                                           ledger_gate(counts), f"s{self.index}-coverage"))

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
                bundles.append((k, _text(source.element)))
            if self.emit("paragraph", bundles, anchor, confidence=RESCUED_CONFIDENCE):
                rescued += sum(ink(text) for _, text in bundles)
        if rescued:
            self.ledger.rescued = rescued
            self.region("unassigned-text", "paragraph", UNASSIGNED_TEXT, next(iter(groups)),
                        gate("rescued", rescued, "==0"))


def _assemble(shape: tables.Grid, parts: dict[int, list[Bundle]]) -> tuple[Table | str, bool]:
    """격자 칸 → 계약 Table(또는 사유)과 이어진 칸 글자를 이었는지. 이어진 칸 글자는 시작 칸 글자 뒤에 \\n으로."""
    cells: list[RawCell] = []
    kept = False
    for slot in shape.slots:
        text = _join(parts[id(slot.tc)]).strip() if slot.tc is not None else ""
        for tc in slot.merged:
            more = _join(parts[id(tc)]).strip()
            if more:
                kept = True
                text = f"{text}\n{more}" if text else more
        cells.append(RawCell(slot.row, slot.col, slot.rowspan, slot.colspan, slot.header, False, text))
    return build(shape.rows, shape.cols, cells), kept


def _region(note: Note) -> Region:
    result = gate(note.name, note.value, None) if note.name is not None else None
    if note.anchor is None:
        return Region(note.tag, "paragraph", note.reason, 0, 0, result, f"doc-{note.tag}")
    return Region(note.tag, "paragraph", note.reason, note.section, note.anchor, result)


class DocxParser:
    mimes: tuple[str, ...] = (MIME,)
    extensions: tuple[str, ...] = (".docx",)

    def parse(self, data: bytes, name: str) -> ParsedSource:
        return self._parse(data, name)[0]

    def _parse(self, data: bytes, name: str) -> tuple[ParsedSource, tuple[Counts, ...]]:
        """ParsedSource와 구역별 장부 숫자(테스트가 본다)."""
        package = Package(data, name, "DOCX", MAIN)
        root, prefixes = package.part(package.main, f"{W}document")
        body = root.find(BODY)
        if body is None:
            raise ParseError("missing w:body", f"{name}:{package.main}")
        styles = read_styles(self._optional(package, "styles", f"{W}styles"),
                             self._optional(package, "numbering", f"{W}numbering"))
        doc = _Doc(name, package, styles, Walker(styles), prefixes, Images(package))
        layout = _layout(body, prefixes, (root,))
        parts = [([e for e, _, item in entries if item], sect) for entries, sect in layout]
        ledgers = [Ledger() for _ in parts]
        story = Story()  # 본문은 이야기 하나(필드가 문단·구역을 넘어 이어진다)
        numbered: list[list[tuple[int, ET.Element]]] = []
        base = 0
        for k, (entries, _) in enumerate(layout):
            numbered.append([])
            for element, chain, item in entries:
                if item:
                    numbered[-1].append((base + len(numbered[-1]), element))
                # 항목 밖 요소(고르지 않은 mc 쪽·떠도는 출처)의 닻은 앞 항목(없으면 구역 첫 순번)
                anchor = numbered[-1][-1][0] if numbered[-1] else base
                doc.walker.part = package.main
                doc.walker.walk(element, prefixes, ledgers[k], k, anchor, story, (root, body, *chain))
            base += len(numbered[-1])
        doc.walker.close(story)
        self._stories(doc, parts, numbered, ledgers)
        doc.walker.finish()
        for k, items in enumerate(numbered):
            _Section(doc, k, items, ledgers[k]).build()
        doc.regions.extend(_region(note) for note in doc.walker.notes)
        chars = sum(part_chars(package.part(m, f"{W}comments")[0]) for m in package.related(package.main, "comments"))
        if chars:
            doc.regions.append(Region("comments", "paragraph", COMMENTS_SKIPPED, 0, 0, gate("chars", chars, None),
                                      "doc-comments"))
        if doc.unreferenced:
            doc.regions.append(Region("unreferenced-part", "paragraph", UNREFERENCED, 0, 0,
                                      gate("chars", doc.unreferenced, None), "doc-unreferenced-part"))
        blocks, regions = finish(doc.specs, doc.regions, doc.outline)
        parsed = ParsedSource(mime=MIME, blocks=blocks, assets=doc.images.budget.assets, regions=regions)
        return parsed, tuple(doc.counts)

    @staticmethod
    def _optional(package: Package, kind: str, root: str) -> ET.Element | None:
        members = package.related(package.main, kind)
        return package.part(members[0], root)[0] if members else None

    def _stories(self, doc: _Doc, parts: list[tuple[list[ET.Element], ET.Element | None]],
                 numbered: list[list[tuple[int, ET.Element]]], ledgers: list[Ledger]) -> None:
        """본문 밖 이야기의 출처를 장부에 올리고 블록 재료를 doc에 둔다: 구역이 처음 참조한 머리말·꼬리말 파트(닻 =
        구역 첫 항목, 항목 없는 구역은 앞 항목), 보이는 참조가 처음 가리킨 각주·미주(닻 = 참조 항목). 특수 주석은 범위
        밖 출처(구역 0), 참조되지 않은 파트·주석은 글자 수만(unreferenced_part_skipped)."""
        package = doc.package
        rels = package.rels(package.main)
        seen: set[str] = set()
        base = 0
        for k, (items, sect) in enumerate(parts):
            anchor = base if items else max(base - 1, 0)
            for ref in sect if sect is not None else ():
                if ref.tag not in MARGIN_REFS:
                    continue
                kind, root_tag = MARGIN_REFS[ref.tag]
                rel = rels.get(ref.get(R_ID, ""))
                if rel is None or rel.type.rpartition("/")[2] != kind or rel.target is None or rel.target in seen \
                        or not package.has(rel.target):
                    continue
                seen.add(rel.target)
                root, prefixes = package.part(rel.target, root_tag)
                doc.held.append(root)
                story = Story()
                doc.walker.part = rel.target
                doc.walker.walk(root, prefixes, ledgers[k], k, anchor, story, (root,))
                doc.walker.close(story)
                doc.margins.setdefault(k, []).append((anchor, (MARGIN_KINDS[kind], root, rel.target, prefixes)))
            base += len(items)
        for kind, root_tag in (("header", f"{W}hdr"), ("footer", f"{W}ftr")):
            for member in dict.fromkeys(package.related(package.main, kind)):  # 관계 id가 달라도 같은 파트는 한 번
                if member not in seen:
                    doc.unreferenced += part_chars(package.part(member, root_tag)[0])
        notes: dict[str, tuple[ET.Element, str, Prefixes, dict[str, ET.Element]]] = {}
        dropped: dict[str, list[ET.Element]] = {kind: [] for kind in NOTES}  # w:id가 겹친 뒤 주석
        for kind, (root_tag, note_tag) in NOTES.items():
            members = package.related(package.main, kind)
            if members:
                root, prefixes = package.part(members[0], root_tag)
                doc.held.append(root)
                doc.walker.part = members[0]
                found: dict[str, ET.Element] = {}
                for note in root.iterfind(note_tag):
                    if note.get(f"{W}type") in SPECIAL_NOTES:
                        doc.walker.walk(note, prefixes, ledgers[0], 0, 0, Story(), (root,), forced="special")
                    elif note.get(f"{W}id", "") in found:
                        dropped[kind].append(note)  # 같은 w:id의 뒤 주석: 블록 없이 글자 수만 센다
                    else:
                        found[note.get(f"{W}id", "")] = note
                notes[kind] = (root, members[0], prefixes, found)
        done: set[tuple[str, str]] = set()
        for section, anchor, tag, key in doc.walker.refs:  # 주석 안 참조가 뒤에 붙어도 돈다
            kind = NOTE_REFS[tag]
            if kind not in notes or (kind, key) in done or key not in notes[kind][3]:
                continue
            done.add((kind, key))
            root, member, prefixes, found = notes[kind]
            story = Story()
            doc.walker.part = member
            doc.walker.walk(found[key], prefixes, ledgers[section], section, anchor, story, (root,))
            doc.walker.close(story)
            doc.notes.setdefault(section, []).append((anchor, (kind, found[key], member, prefixes)))
        for kind, (_, _, _, found) in notes.items():
            doc.unreferenced += sum(part_chars(note) for key, note in found.items() if (kind, key) not in done)
            doc.unreferenced += sum(part_chars(note) for note in dropped[kind])
        self._graphics(doc, ledgers)

    @staticmethod
    def _graphics(doc: _Doc, ledgers: list[Ledger]) -> None:
        """차트·SmartArt 파트의 출처를 호스트 자리마다 따로 올린다(같은 파트를 두 호스트가 가리키면 두 번 연다).
        관계는 호스트 파트의 것이다. SmartArt는 그려진 파트가 있으면 그것, 없으면 데이터 파트를 읽고 고르지 않은 파트
        글자는 범위 밖(alternate_content_skipped에 센다). 호스트가 범위 밖·숨김이면 파트 글자도 그 표시다."""
        package = doc.package
        for section, anchor, host, element, out in doc.walker.graphics:
            rels = package.rels(host)
            if element.tag == CHART:
                rel = rels.get(element.get(R_ID, ""))
                if rel is None or rel.type != f"{R}/chart" or rel.target is None or not package.has(rel.target):
                    continue  # 풀 수 없거나 종류가 다른 관계: 이 자리는 이미지 없는 그림만
                root, prefixes = package.part(rel.target, CHART_ROOT)
                doc.charts[id(element)] = root
                read = [(root, prefixes, out)]
            else:
                rel = rels.get(element.get(R_DM, ""))
                if rel is None or rel.type != f"{R}/diagramData" or rel.target is None or not package.has(rel.target):
                    continue
                data, data_prefixes = package.part(rel.target, DATA_ROOT)
                drawn = rels.get(drawn_rel(data) or "")
                read = [(data, data_prefixes, out)]
                if drawn is not None and drawn.type == DRAWN_TYPE and drawn.target is not None \
                        and package.has(drawn.target):
                    root, prefixes = package.part(drawn.target, DRAWN_ROOT)
                    # 고르지 않은 데이터 파트는 호스트가 숨김이어도 항상 범위 밖(스펙 §9)
                    read = [(root, prefixes, out), (data, data_prefixes, "alt" if out in (None, "hidden") else out)]
                doc.smartart[id(element)] = read[0][0]
            for root, prefixes, forced in read:
                doc.held.append(root)
                doc.walker.walk(root, prefixes, ledgers[section], section, anchor, Story(), (root,), forced=forced)
