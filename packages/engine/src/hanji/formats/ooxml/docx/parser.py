"""DOCX(WordprocessingML, zip + XML) → ParsedSource(스펙 §5). PDF로 바꾸지 않고 원본 구조를 결정론으로 읽는다.

본문 블록 항목(w:body 아래 w:p·w:tbl, w:sdt·w:customXml은 벗긴다)마다 블록을 낸다. 문단은 개요 수준·제목 스타일 →
제목, numPr → 목록 항목, 그 밖 문단(제목 스타일이 하나도 없는 문서는 PDF와 같은 글꼴 크기 규칙). 위치는 FlowLocator(제목
경로, 본문 항목 순번). 구역(w:sectPr)이 장부 단위다. ooxml/ledger.py의 Walker가 출처를 따로 열거해 보통·숨김·범위 밖으로
표시하고, 블록에는 보통 출처만 넣는다. 한 번도 넣지 않은 보통 출처는 구역 끝 구조 문단(신뢰도 0.3, unassigned_text)으로
살린다. 그림·글상자·머리말·꼬리말·각주·미주는 아직 블록을 내지 않아 그 글자가 살리기로 나온다(OX-B가 블록으로 낸다)."""

import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ....errors import ParseError
from ...base import ParsedSource
from ...flowdoc import Region, finish, gate, ledger_gate
from ...flowledger import Bundle, Counts, Ledger, ink
from ...pdf.group import RESCUED_CONFIDENCE, step
from ...pdf.parser import COVERAGE_MISMATCH, UNASSIGNED_TEXT
from ...zipxml import Prefixes
from ..ledger import NORMAL, SOURCES, Note, Story, Walker, part_chars, sym_char
from ..package import MC, READ, Package, choose
from .styles import Styles, read_styles

MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAIN = f"{MIME}.main+xml"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
R_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
P, TBL, RUN, SDT, SDT_CONTENT = f"{W}p", f"{W}tbl", f"{W}r", f"{W}sdt", f"{W}sdtContent"
CUSTOM_XML, SECT_PR, BODY = f"{W}customXml", f"{W}sectPr", f"{W}body"
T, INSTR, SYM, BR, CR = f"{W}t", f"{W}instrText", f"{W}sym", f"{W}br", f"{W}cr"
ALTERNATE = f"{MC}AlternateContent"
ITEMS = frozenset({P, TBL})
Chain = tuple[ET.Element, ...]  # w:body와 본문 층 요소 사이의 감싼 요소들(위에서 아래로)
# 문단 안에서 벗기는 감싼 요소(스펙 §5.2). w:sdt는 w:sdtContent만 읽는다
WRAPPERS = frozenset(f"{W}{n}" for n in ("hyperlink", "smartTag", "customXml", "ins", "fldSimple", "moveTo", "dir",
                                          "bdo", "sdtContent"))
MATH = frozenset({f"{M}oMath", f"{M}oMathPara"})
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


@dataclass(slots=True)
class _Doc:
    """문서 전체에서 모으는 것: 블록 명세(내부 키 _anchor 등은 finish가 지운다), 처리 이력, 장부 숫자."""

    name: str
    package: Package
    styles: Styles
    walker: Walker
    prefixes: Prefixes
    specs: list[dict[str, Any]] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    counts: list[Counts] = field(default_factory=list)
    outline: bool = False  # 개요 수준·제목 스타일 제목을 하나라도 썼나(그러면 크기 규칙을 쓰지 않는다)
    # _stories가 연 파트 뿌리: Walker·장부가 id(w:p)로 문단을 가리키므로 파싱이 끝날 때까지 붙잡아 id 재사용을 막는다
    held: list[ET.Element] = field(default_factory=list)


def _text(element: ET.Element) -> str:
    """블록에 넣는 출처 글자(장부와 따로 만든다): w:sym은 장부와 같은 코드 범위 규칙(ledger.sym_char: 0·대리 영역·
    범위 밖은 빈 글자), 그 밖은 요소 안 글자."""
    if element.tag != SYM:
        return "".join(element.itertext())
    return sym_char(element)


def _join(bundles: list[Bundle]) -> str:
    return "".join(text for _, text in bundles)


def sections(body: ET.Element, prefixes: Prefixes | None = None) -> list[tuple[list[ET.Element], ET.Element | None]]:
    """본문 블록 항목을 구역마다 (항목들, 그 구역의 w:sectPr). prefixes는 mc:Choice@Requires를 푸는 데 쓴다."""
    return [([e for e, _, item in entries if item], sect) for entries, sect in _layout(body, prefixes or {})]


Entry = tuple[ET.Element, Chain, bool]  # (본문 층 요소, 감싼 요소들, 본문 블록 항목인가)


def _layout(body: ET.Element, prefixes: Prefixes) -> list[tuple[list[Entry], ET.Element | None]]:
    """본문 층을 구역마다 ([(요소, w:body와 요소 사이 감싼 요소들, 본문 블록 항목인가)], w:sectPr)로(스택 순회). 요소는
    본문 블록 항목(w:p·w:tbl), 고르지 않은 mc 쪽 통째, 항목 밖 출처다(Walker가 감싼 요소들을 조상으로 모두 훑는다).
    w:sdt/w:sdtContent·w:customXml·고른 mc 쪽·그 밖 요소는 벗긴다: 무시 표시 요소 안 항목의 글자는 Walker가 범위 밖으로
    표시하고, 고르지 않은 쪽 글자는 alternate_content_skipped로 센다."""
    out: list[tuple[list[Entry], ET.Element | None]] = []
    entries: list[Entry] = []
    last: ET.Element | None = None
    # (요소, 감싼 요소들, 벗기지 않고 통째로 내는 고르지 않은 mc 쪽인가)
    stack: list[tuple[ET.Element, Chain, bool]] = [(child, (), False) for child in reversed(body)]
    while stack:
        element, chain, whole = stack.pop()
        tag = element.tag
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
                stack.extend((child, (*chain, element, content), False) for child in reversed(content))
        elif tag == ALTERNATE:
            chosen = choose(element, prefixes)
            for child in reversed(element):
                if child is chosen:
                    stack.extend((inner, (*chain, element, child), False) for inner in reversed(child))
                else:
                    stack.append((child, (*chain, element), True))
        else:
            stack.extend((child, (*chain, element), False) for child in reversed(element))
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
                    chosen = choose(element, self.doc.prefixes)
                    if chosen is not None:
                        stack.extend((child, None) for child in reversed(chosen))
                elif tag in MATH:
                    for mr in element.iter(f"{M}r"):
                        for mt in mr.iterfind(f"{M}t"):
                            self.piece(mt, p, mr, out, sizes)
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
                chosen = choose(element, self.doc.prefixes)
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
        """본문 항목 문단: 개요 수준·제목 스타일 → 제목, 목록 → 목록 항목, 그 밖 문단(크기 규칙 후보)."""
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

    def build(self) -> None:
        for anchor, item in self.items:
            if item.tag == P:
                self.paragraph(item, anchor)
        self.late = True
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
        doc = _Doc(name, package, styles, Walker(styles), prefixes)
        layout = _layout(body, prefixes)
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
        blocks, regions = finish(doc.specs, doc.regions, doc.outline)
        return ParsedSource(mime=MIME, blocks=blocks, regions=regions), tuple(doc.counts)

    @staticmethod
    def _optional(package: Package, kind: str, root: str) -> ET.Element | None:
        members = package.related(package.main, kind)
        return package.part(members[0], root)[0] if members else None

    def _stories(self, doc: _Doc, parts: list[tuple[list[ET.Element], ET.Element | None]],
                 numbered: list[list[tuple[int, ET.Element]]], ledgers: list[Ledger]) -> None:
        """본문 밖 이야기의 출처를 장부에 올린다: 구역이 처음 참조한 머리말·꼬리말 파트(닻 = 구역 첫 항목), 본문이
        처음 참조한 각주·미주(닻 = 참조 항목). 아직 블록을 내지 않으므로 보통 출처는 살리기로 나온다."""
        package = doc.package
        rels = package.rels(package.main)
        seen: set[str] = set()
        base = 0
        for k, (items, sect) in enumerate(parts):
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
                doc.walker.walk(root, prefixes, ledgers[k], k, base, story, (root,))
                doc.walker.close(story)
            base += len(items)
        notes: dict[str, tuple[ET.Element, Prefixes, dict[str, ET.Element]]] = {}
        for kind, (root_tag, note_tag) in NOTES.items():
            members = package.related(package.main, kind)
            if members:
                root, prefixes = package.part(members[0], root_tag)
                doc.held.append(root)
                found = {n.get(f"{W}id", ""): n for n in root.iterfind(note_tag)
                         if n.get(f"{W}type") not in SPECIAL_NOTES}
                notes[kind] = (root, prefixes, found)
        done: set[tuple[str, str]] = set()
        for section, anchor, tag, key in doc.walker.refs:
            kind = NOTE_REFS[tag]
            if kind not in notes or (kind, key) in done or key not in notes[kind][2]:
                continue
            done.add((kind, key))
            root, prefixes, found = notes[kind]
            story = Story()
            doc.walker.walk(found[key], prefixes, ledgers[section], section, anchor, story, (root,))
            doc.walker.close(story)
